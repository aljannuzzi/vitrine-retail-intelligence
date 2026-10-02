import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import re
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse, JSONResponse

from .auth import COOKIE, SESSION_SECONDS, LoginBudget, Sessions
from .config import Settings
from .engine import Engine, ObservationBatch
from .foundry import FoundryService
from .media import BlobMedia
from .models import (AgentRequest, Approval, Control, Login, QualitativeObservation,
                     VisionObservation, qualitative_doc_id, strip_known_cosmos_metadata)
from .simulation import CAMERAS, iso, public_run, unavailable_vision, variant
from .storage import Conflict, create_repository

logger = logging.getLogger("retail")


def create_app(settings=None, repository=None, foundry=None, media=None, now=time.time, ticker=True):
    @asynccontextmanager
    async def lifespan(app):
        cfg = settings or Settings.from_env()
        cfg.validate()
        app.state.settings = cfg
        app.state.sessions = Sessions(cfg)
        app.state.login_budget = LoginBudget()
        app.state.initialized = False
        repo = repository or create_repository(cfg)
        agents = foundry or FoundryService(cfg, repo)
        blobs = media or BlobMedia(cfg)
        app.state.repo, app.state.foundry, app.state.media = repo, agents, blobs
        engine = Engine(repo, agents, blobs, now)
        app.state.engine = engine
        refresh_task = None
        try:
            async def initialize_services():
                await repo.initialize()
                await engine.initialize()
                await blobs.initialize()
                await agents.initialize()
                app.state.initialized = True
                if ticker:
                    engine.start()
            try:
                await initialize_services()
            except Exception:
                logger.error("Retail dependency initialization failed; no fallback activated")

            async def refresh_dependencies():
                while True:
                    await asyncio.sleep(5)
                    try:
                        if not app.state.initialized:
                            await initialize_services()
                        elif not agents.available:
                            await agents.initialize()
                    except Exception:
                        logger.warning("Retail dependency retry unavailable; no fallback activated")
            refresh_task = asyncio.create_task(refresh_dependencies())
            yield
        finally:
            if refresh_task:
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)
            await engine.close()
            await agents.close()
            await blobs.close()
            await repo.close()

    app = FastAPI(title="VITRINE", docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code,
                            headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Validation errors may contain passwords/questions; never echo the input.
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict_error(request, exc):
        return JSONResponse({"error": "state_writer_conflict"}, status_code=503)

    @app.middleware("http")
    async def security(request, call_next):
        path = request.url.path
        try:
            if path.startswith("/api/"):
                cfg = request.app.state.settings
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if request.headers.get("origin") != cfg.public_origin:
                        return JSONResponse({"error": "origin_forbidden"}, status_code=403)
                    body = bytearray()
                    async for chunk in request.stream():
                        body.extend(chunk)
                        if len(body) > 16384:
                            return JSONResponse({"error": "request_too_large"}, status_code=413)
                    request._body = bytes(body)
                public = path in ("/api/config", "/api/session")
                if not public and not request.app.state.sessions.valid(
                        request.cookies.get(COOKIE), now()):
                    return JSONResponse({"error": "unauthenticated"}, status_code=401)
                if not public and not request.app.state.initialized:
                    return JSONResponse({"error": "external_service_unavailable"}, status_code=503)
            response = await call_next(request)
        except Exception:
            logger.error("Retail request failed; details withheld from public response")
            response = JSONResponse({"error": "external_service_unavailable"}, status_code=503)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    async def observations():
        engine = app.state.engine
        async with engine.lock:
            epoch = engine.state["epochId"]
            since = engine.state["activeSince"]
            layout = engine.state["layoutVersion"]
            current_variant = variant(engine.state)
            if engine.state["interventions"]:
                since = engine.state["interventions"][-1]["at"]
        epoch_start = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        current_time = now()

        async def read_detector(camera):
            unavailable = unavailable_vision(camera)
            try:
                doc = await app.state.repo.read_document(camera, "vision")
                if not doc:
                    return unavailable
                if doc.get("epochId", epoch) != epoch or doc.get("layoutVersion", layout) != layout:
                    return unavailable
                value = VisionObservation.model_validate(doc["observation"]).model_dump()
                if value["cameraId"] != camera:
                    return unavailable
                if value["status"] == "ready":
                    stamp = datetime.fromisoformat(value["observedAt"].replace("Z", "+00:00")).timestamp()
                    if stamp < epoch_start or not 0 <= current_time - stamp <= 120:
                        return unavailable
                if value["error"]:
                    value["error"] = "Inferência real indisponível."
                return value
            except Exception:
                return unavailable

        async def read_qualitative(camera):
            try:
                doc = await app.state.repo.read_document(qualitative_doc_id(camera), "vision")
                if not doc:
                    return None
                value = QualitativeObservation.model_validate(
                    strip_known_cosmos_metadata(doc)).model_dump()
                if (value["cameraId"] != camera or value["epochId"] != epoch or
                        value["layoutVersion"] != layout or value["variant"] != current_variant):
                    return None
                stamp = datetime.fromisoformat(value["observedAt"].replace("Z", "+00:00")).timestamp()
                if stamp < epoch_start or not 0 <= current_time - stamp <= 300:
                    return None
                return value
            except Exception:
                return None

        values, qualitative = await asyncio.gather(
            asyncio.gather(*(read_detector(camera) for camera in CAMERAS)),
            asyncio.gather(*(read_qualitative(camera) for camera in CAMERAS)),
        )
        return ObservationBatch(
            dict(zip(CAMERAS, values)),
            epoch,
            layout,
            qualitative={camera: value for camera, value in zip(CAMERAS, qualitative) if value is not None},
        )

    @app.get("/health")
    async def health():
        ok = app.state.initialized and app.state.engine.ready
        if app.state.settings.environment == "azure":
            ok = ok and app.state.media.ready and app.state.foundry.available
        return JSONResponse({"status": "ready" if ok else "unavailable"},
                            status_code=200 if ok else 503)

    @app.get("/api/config")
    async def config():
        return {"name": "VITRINE", "authRequired": True, "mode": app.state.settings.environment,
                "simulation": True, "model": "GPT-4.1 agents + GPT-5.4 vision + RT-DETRv2",
                "agents": [{"kind": kind, "name": "vitrine-" + kind}
                           for kind in ("merchandising", "experiment")]}

    @app.get("/api/session")
    async def session(request: Request):
        return {"authenticated": app.state.sessions.valid(request.cookies.get(COOKIE), now())}

    @app.post("/api/session")
    async def login(body: Login, request: Request):
        ip = request.client.host if request.client else "unknown"
        if not app.state.login_budget.allow(ip, now()):
            raise HTTPException(429, "login_rate_limit")
        if not app.state.sessions.password_valid(body.password):
            raise HTTPException(401, "invalid_credentials")
        response = JSONResponse({"authenticated": True})
        response.set_cookie(COOKIE, app.state.sessions.issue(now()), max_age=SESSION_SECONDS,
                            secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.delete("/api/session")
    async def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/snapshot")
    async def get_snapshot():
        return await app.state.engine.view(await observations())

    @app.post("/api/scenario/control")
    async def control(body: Control):
        await app.state.engine.control(body.action, body.speed)
        return await app.state.engine.view(await observations(), touch=False)

    @app.post("/api/agents/run", status_code=202)
    async def run_agent(body: AgentRequest):
        return await app.state.engine.run_agent(body.agent, body.question, await observations())

    @app.get("/api/agents/runs/{id}")
    async def get_run(id: str):
        async with app.state.engine.transaction(viewer=True) as state:
            run = next((r for r in state["agentRuns"] if r["id"] == id), None)
            if not run:
                raise HTTPException(404, "run_not_found")
            return public_run(run)

    @app.post("/api/interventions")
    async def intervention(body: Approval):
        return await app.state.engine.approve(body.runId, body.recommendationId, body.actionId)

    @app.post("/api/vision/analyze", status_code=202)
    async def analyze():
        if not app.state.media.ready:
            raise HTTPException(503, "vision_unavailable")
        async with app.state.engine.transaction(viewer=True) as state:
            if now() - state.get("lastVisionRequest", 0) < 10:
                raise HTTPException(429, "vision_rate_limit")
            state["lastVisionRequest"] = now()
            await app.state.repo.write_document({"id": "vision-request", "pk": "store-demo",
                                                "requestedAt": iso(now()), "epochId": state["epochId"]})
        return {"requested": True}

    @app.api_route("/api/media/{cameraId}/{variant}.mp4", methods=["GET", "HEAD"])
    async def video(cameraId: str, variant: str, request: Request):
        if cameraId not in CAMERAS or variant not in ("baseline", "optimized"):
            raise HTTPException(404, "media_not_found")
        async with app.state.engine.transaction(viewer=True):
            pass
        return await app.state.media.stream(f"media/{cameraId}/{variant}.mp4", request, "video/mp4")

    @app.api_route("/api/vision/frames/{cameraId}.jpg", methods=["GET", "HEAD"])
    async def frame(cameraId: str, request: Request):
        if cameraId not in CAMERAS:
            raise HTTPException(404, "frame_not_found")
        observation = request.query_params.get("observation", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", observation):
            raise HTTPException(400, "observation_id_required")
        return await app.state.media.stream(f"frames/{cameraId}.jpg", request, "image/jpeg")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "not_found")
        root = Path(app.state.settings.frontend_dist).resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or any(part.startswith(".") for part in Path(path).parts):
            raise HTTPException(404, "not_found")
        if candidate.is_file():
            return FileResponse(candidate)
        if Path(path).suffix:
            raise HTTPException(404, "not_found")
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(503, "frontend_build_missing")
        return FileResponse(index)

    return app


app = create_app()
