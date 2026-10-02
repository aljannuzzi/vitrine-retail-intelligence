"""Azure-only, single-camera-at-a-time RT-DETR worker."""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from .detector import Detector, MODEL_ID, MODEL_REVISION
from .qualitative import IsolatedVisualClient, QualitativeStage

CAMERAS = ("camera-denim", "camera-essentials", "camera-checkout")
VARIANTS = ("baseline", "optimized")
STORE_PK = "store-demo"
ERROR_CODES = frozenset({
    "configuration_invalid", "model_unavailable", "clock_unavailable",
    "clock_invalid", "request_unavailable", "media_unavailable",
    "frame_extraction_failed", "inference_failed", "frame_upload_failed",
    "observation_write_failed", "worker_unavailable",
})


class WorkerError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else "worker_unavailable"
        super().__init__(self.code)


def log_error(code):
    print(json.dumps({"event": "vision_error", "code": WorkerError(code).code}), flush=True)


def utcnow():
    return datetime.now(timezone.utc)


def isoformat(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid_timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid_timestamp")
    return result.astimezone(timezone.utc)


def finite_number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("invalid_number")
    if not math.isfinite(value) or value < minimum:
        raise ValueError("invalid_number")
    return float(value)


@dataclass(frozen=True)
class Clock:
    epoch_id: str
    state: str
    elapsed_seconds: float
    speed: float
    updated_at: datetime
    active_until: datetime | None
    layout_version: int
    variant: str

    @classmethod
    def parse(cls, document):
        try:
            if not isinstance(document, dict):
                raise ValueError("invalid_clock")
            if document["id"] != "clock" or document["pk"] != STORE_PK:
                raise ValueError("invalid_clock")
            epoch = document["epochId"]
            layout = document["layoutVersion"]
            if not isinstance(epoch, str) or not epoch or len(epoch) > 128:
                raise ValueError("invalid_epoch")
            if isinstance(layout, bool) or not isinstance(layout, int) or layout < 0:
                raise ValueError("invalid_layout")
            state, variant = document["state"], document["variant"]
            if state not in ("running", "paused") or variant not in VARIANTS:
                raise ValueError("invalid_clock")
            return cls(
                epoch, state, finite_number(document["elapsedSeconds"]),
                finite_number(document["speed"]), timestamp(document["updatedAt"]),
                timestamp(document["activeUntil"]) if document["activeUntil"] is not None else None,
                layout, variant,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise WorkerError("clock_invalid") from None

    @property
    def identity(self):
        return self.epoch_id, self.layout_version, self.variant

    def source_seconds(self, now):
        elapsed = self.elapsed_seconds
        if self.state == "running":
            elapsed += max(0, (now - self.updated_at).total_seconds()) * self.speed
        if not math.isfinite(elapsed):
            raise WorkerError("clock_invalid")
        return elapsed % 60

    def active(self, now):
        return self.active_until is not None and now < self.active_until


@dataclass(frozen=True)
class Request:
    epoch_id: str
    requested_at: datetime

    @property
    def key(self):
        return self.epoch_id, self.requested_at

    @classmethod
    def fresh(cls, document, clock, now, ttl):
        try:
            if not isinstance(document, dict):
                return None
            if document["id"] != "vision-request" or document["pk"] != STORE_PK:
                return None
            if document["epochId"] != clock.epoch_id:
                return None
            requested = timestamp(document["requestedAt"])
            age = (now - requested).total_seconds()
            if not 0 <= age < ttl:
                return None
            return cls(clock.epoch_id, requested)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


@dataclass(frozen=True)
class Settings:
    client_id: str
    cosmos_endpoint: str
    storage_account: str
    cosmos_database: str = "vitrine"
    cosmos_container: str = "retail"
    storage_container: str = "retail"
    interval_seconds: float = 5
    request_ttl_seconds: float = 30
    cache_bytes: int = 256 * 1024 * 1024
    cache_item_bytes: int = 64 * 1024 * 1024
    cache_entries: int = 6
    cache_ttl_seconds: float = 300
    cache_path: Path = Path("vision") / ".cache" / "media"
    qualitative_endpoint: str = ""
    qualitative_enabled: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            if env.get("ENVIRONMENT") != "azure":
                raise ValueError("azure_required")
            client_id = str(uuid.UUID(env["AZURE_CLIENT_ID"]))
            endpoint = env["COSMOS_ENDPOINT"]
            parsed = urlsplit(endpoint)
            if (
                parsed.scheme != "https"
                or not re.fullmatch(r"[a-z0-9-]+\.documents\.azure\.com", parsed.hostname or "")
                or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")
            ):
                raise ValueError("invalid_endpoint")
            account = env["AZURE_STORAGE_ACCOUNT"]
            if not re.fullmatch(r"[a-z0-9]{3,24}", account):
                raise ValueError("invalid_account")
            names = [env.get(key, default) for key, default in (
                ("COSMOS_DATABASE", "vitrine"), ("COSMOS_CONTAINER", "retail"),
                ("AZURE_STORAGE_CONTAINER", "retail"),
            )]
            if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", name) for name in names):
                raise ValueError("invalid_name")
            interval = float(env.get("VISION_INTERVAL_SECONDS", "5"))
            ttl = float(env.get("VISION_REQUEST_TTL_SECONDS", "30"))
            if not math.isfinite(interval) or not 1 <= interval <= 60:
                raise ValueError("invalid_interval")
            if not math.isfinite(ttl) or not 15 <= ttl <= 120:
                raise ValueError("invalid_ttl")
            visual_endpoint = env.get("AZURE_OPENAI_ENDPOINT", "")
            visual_enabled = env.get("VISION_QUALITATIVE_ENABLED", "true" if visual_endpoint else "false")
            if visual_enabled.lower() not in ("true", "false"):
                raise ValueError("invalid_qualitative_flag")
            return cls(client_id, endpoint, account, *names, interval, ttl,
                       qualitative_endpoint=visual_endpoint,
                       qualitative_enabled=visual_enabled.lower() == "true")
        except (KeyError, ValueError, TypeError, OverflowError):
            raise WorkerError("configuration_invalid") from None


class AzureStore:
    """Only a user-assigned managed identity; never create cloud resources."""

    def __init__(self, settings):
        from azure.cosmos import CosmosClient
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        self.credential = ManagedIdentityCredential(client_id=settings.client_id)
        self.cosmos = CosmosClient(
            settings.cosmos_endpoint, credential=self.credential,
            connection_timeout=5, read_timeout=10, retry_total=2,
            enable_diagnostics_logging=False, logging_enable=False,
        )
        self.container = self.cosmos.get_database_client(
            settings.cosmos_database
        ).get_container_client(settings.cosmos_container)
        self.blobs = BlobServiceClient(
            account_url=f"https://{settings.storage_account}.blob.core.windows.net",
            credential=self.credential, connection_timeout=5, read_timeout=15,
            retry_total=2, logging_enable=False,
        )
        self.media = self.blobs.get_container_client(settings.storage_container)

    def read_clock(self):
        try:
            return self.container.read_item(item="clock", partition_key=STORE_PK)
        except Exception:
            raise WorkerError("clock_unavailable") from None

    def read_request(self):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            return self.container.read_item(item="vision-request", partition_key=STORE_PK)
        except CosmosResourceNotFoundError:
            return None
        except Exception:
            raise WorkerError("request_unavailable") from None

    def download_media(self, name, max_bytes):
        from azure.core import MatchConditions

        try:
            blob = self.media.get_blob_client(name)
            properties = blob.get_blob_properties()
            if not 0 < properties.size <= max_bytes:
                raise WorkerError("media_unavailable")
            stream = blob.download_blob(
                etag=properties.etag, match_condition=MatchConditions.IfNotModified,
                max_concurrency=1,
            )
            received = 0
            for chunk in stream.chunks():
                received += len(chunk)
                if received > max_bytes or received > properties.size:
                    raise WorkerError("media_unavailable")
                yield chunk
            if received != properties.size:
                raise WorkerError("media_unavailable")
        except Exception:
            raise WorkerError("media_unavailable") from None

    def upload_frame(self, camera, jpeg, observation_id):
        from azure.storage.blob import ContentSettings

        try:
            self.media.get_blob_client(f"frames/{camera}.jpg").upload_blob(
                jpeg, overwrite=True, max_concurrency=1,
                metadata={"observationid": observation_id},
                content_settings=ContentSettings(
                    content_type="image/jpeg", cache_control="private, no-store"
                ),
            )
        except Exception:
            raise WorkerError("frame_upload_failed") from None

    def upsert_observation(self, document):
        try:
            self.container.upsert_item(document)
        except Exception:
            raise WorkerError("observation_write_failed") from None

    def close(self):
        for client in (self.blobs, self.cosmos, self.credential):
            try:
                client.close()
            except Exception:
                pass


class MediaCache:
    """Disposable bytes only, never clock or observation truth; single-threaded."""

    def __init__(self, root, download, *, max_bytes=256 * 1024 * 1024,
                 max_item_bytes=64 * 1024 * 1024, max_entries=6, ttl=300,
                 monotonic=time.monotonic):
        if not 0 < max_item_bytes <= max_bytes or max_entries < 1 or ttl <= 0:
            raise ValueError("invalid_cache_limits")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.download = download
        self.max_bytes, self.max_item_bytes = max_bytes, max_item_bytes
        self.max_entries, self.ttl = max_entries, ttl
        self.monotonic = monotonic
        self.entries = OrderedDict()
        # Cache ownership is limited to hashed media artifacts in this directory.
        for path in self.root.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}\.(mp4|part)", path.name):
                path.unlink(missing_ok=True)

    @property
    def size(self):
        return sum(entry[1] for entry in self.entries.values())

    def _remove(self, key):
        path, _, _ = self.entries.pop(key)
        path.unlink(missing_ok=True)

    def get(self, camera, variant):
        if camera not in CAMERAS or variant not in VARIANTS:
            raise WorkerError("media_unavailable")
        key = f"media/{camera}/{variant}.mp4"
        if key in self.entries:
            path, size, expires = self.entries[key]
            if self.monotonic() < expires and path.is_file() and path.stat().st_size == size:
                self.entries.move_to_end(key)
                return path
            self._remove(key)
        while self.entries and (
            len(self.entries) >= self.max_entries or self.size + self.max_item_bytes > self.max_bytes
        ):
            self._remove(next(iter(self.entries)))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        final = self.root / f"{digest}.mp4"
        partial = self.root / f"{digest}.part"
        try:
            total = 0
            with partial.open("xb") as output:
                for chunk in self.download(key, self.max_item_bytes):
                    total += len(chunk)
                    if total > self.max_item_bytes:
                        raise WorkerError("media_unavailable")
                    output.write(chunk)
            if total == 0:
                raise WorkerError("media_unavailable")
            partial.replace(final)
            self.entries[key] = (final, total, self.monotonic() + self.ttl)
            return final
        except Exception:
            partial.unlink(missing_ok=True)
            raise WorkerError("media_unavailable") from None


def source_frame_seconds(seconds):
    """Floor the continuous clip clock to a 15 fps presentation timestamp."""
    try:
        seconds = finite_number(seconds)
        if seconds >= 60:
            raise ValueError("invalid_source_seconds")
        return min(899, math.floor(seconds * 15)) / 15
    except (ValueError, TypeError, OverflowError):
        raise WorkerError("clock_invalid") from None


def extract_jpeg(path, source_seconds):
    try:
        if not math.isfinite(source_seconds) or not 0 <= source_seconds < 60:
            raise ValueError("invalid_source_seconds")
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                # Stay just below the PTS so decimal rounding cannot skip a frame.
                "-ss", f"{max(0, source_seconds - 0.000001):.6f}",
                "-threads", "2", "-i", str(path),
                "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
                "-filter_threads", "1", "-threads", "2", "-c:v", "mjpeg",
                "-q:v", "2", "-f", "image2pipe", "pipe:1",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=20, check=False,
        )
        data = result.stdout
        if (
            result.returncode != 0 or not data or len(data) > 4 * 1024 * 1024
            or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9")
        ):
            raise ValueError("invalid_jpeg")
        return data
    except Exception:
        raise WorkerError("frame_extraction_failed") from None


def decode_jpeg(data):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.convert("RGB")


def validate_detections(detections):
    if not isinstance(detections, list):
        raise WorkerError("inference_failed")
    for detection in detections:
        try:
            if not isinstance(detection, dict) or detection["label"] != "person":
                raise ValueError("invalid_detection")
            score = finite_number(detection["score"])
            box = detection["box"]
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                raise ValueError("invalid_box")
            x1, y1, x2, y2 = [finite_number(value) for value in box]
            if score > 1 or not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
                raise ValueError("invalid_detection")
        except (TypeError, KeyError, ValueError, OverflowError):
            raise WorkerError("inference_failed") from None
    return detections


class Health:
    def __init__(self, monotonic=time.monotonic):
        self.monotonic = monotonic
        self.model_ready = False
        self.loop_ready = False
        self.stopped = False
        self.last_pulse = None
        self.error = "model_unavailable"

    def pulse(self, error=None):
        self.last_pulse = self.monotonic()
        self.loop_ready = error is None
        self.error = None if error is None else WorkerError(error).code

    def snapshot(self):
        live = self.last_pulse is not None and self.monotonic() - self.last_pulse < 120
        ready = self.model_ready and self.loop_ready and live and not self.stopped
        error = None if ready else (
            "model_unavailable" if not self.model_ready else self.error or "worker_unavailable"
        )
        return ready, {"ready": ready, "modelReady": self.model_ready, "loopReady": bool(
            self.loop_ready and live and not self.stopped
        ), "error": error}


def health_handler(health):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/healthz", "/health"):
                self.send_response(404)
                self.end_headers()
                return
            ready, payload = health.snapshot()
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


class Worker:
    def __init__(self, store, cache, detector, *, request_ttl=30, health=None,
                 now=utcnow, monotonic=time.monotonic, extractor=extract_jpeg,
                 decoder=decode_jpeg, qualitative=None):
        self.store, self.cache, self.detector = store, cache, detector
        self.request_ttl = request_ttl
        self.now, self.monotonic = now, monotonic
        self.extractor, self.decoder = extractor, decoder
        self.health = health if health is not None else Health(monotonic)
        self.health.model_ready = detector is not None
        self.cursor = 0
        self.request_key = None
        self.request_done = set()
        self.qualitative = qualitative

    def _qualitative(self, clock, observation=None, jpeg=None):
        if self.qualitative is None:
            return
        try:
            current = Clock.parse(self.store.read_clock())
            document = self.qualitative.poll(current, self.now())
            if document is not None:
                self.store.upsert_observation(document)
            if observation is not None and current.identity == clock.identity:
                self.qualitative.start(current, observation, jpeg, self.now())
        except Exception:
            # This optional evidence channel cannot change detector publication or health.
            print(json.dumps({"event": "qualitative_error", "code": "qualitative_unavailable"}),
                  flush=True)

    def _state(self):
        clock = Clock.parse(self.store.read_clock())
        request = Request.fresh(
            self.store.read_request(), clock, self.now(), self.request_ttl
        )
        return clock, request

    def _eligible(self, clock, request, camera):
        return clock.active(self.now()) or (
            request is not None
            and (request.key != self.request_key or camera not in self.request_done)
        )

    def _current(self, original, request, camera, request_only):
        clock, current_request = self._state()
        if clock.identity != original.identity:
            return None
        if request_only and (
            current_request is None or request is None or current_request.key != request.key
        ):
            return None
        # A new manual request supersedes an in-flight manual sample.
        if request is not None and current_request is not None and request.key != current_request.key:
            return None
        if not self._eligible(clock, current_request, camera):
            return None
        return clock

    def _observation(self, camera, source, started, *, detections=None, error=None,
                     warming=False):
        ready = error is None and not warming
        return {
            "id": str(uuid.uuid4()), "cameraId": camera,
            "status": "warming" if warming else "ready" if ready else "unavailable",
            "observedAt": isoformat(self.now()), "sourceSeconds": source,
            "people": len(detections) if ready else None,
            "detections": detections if ready else [],
            "frameUri": f"/api/vision/frames/{camera}.jpg" if ready else None,
            "latencyMs": max(0, round((self.monotonic() - started) * 1000)),
            "model": MODEL_ID, "modelRevision": MODEL_REVISION,
            "provenance": "model-inference",
            "error": None if error is None else WorkerError(error).code,
        }

    def tick(self):
        """Attempt at most one camera; return idle, ready, unavailable, stale or error."""
        try:
            clock, request = self._state()
            self.health.pulse()
            self._qualitative(clock)
            key = request.key if request is not None else None
            if key != self.request_key:
                self.request_key, self.request_done = key, set()
            camera = None
            for offset in range(len(CAMERAS)):
                index = (self.cursor + offset) % len(CAMERAS)
                if self._eligible(clock, request, CAMERAS[index]):
                    camera = CAMERAS[index]
                    self.cursor = (index + 1) % len(CAMERAS)
                    break
            if camera is None:
                return "idle"
            request_only = not clock.active(self.now())
            started = self.monotonic()
            source = None
            jpeg = None
            try:
                if self.detector is None:
                    raise WorkerError("model_unavailable")
                try:
                    path = self.cache.get(camera, clock.variant)
                except Exception:
                    raise WorkerError("media_unavailable") from None
                current = self._current(clock, request, camera, request_only)
                if current is None:
                    return "stale"
                source = source_frame_seconds(current.source_seconds(self.now()))
                try:
                    jpeg = self.extractor(path, source)
                    image = self.decoder(jpeg)
                except Exception:
                    raise WorkerError("frame_extraction_failed") from None
                if self._current(clock, request, camera, request_only) is None:
                    return "stale"
                try:
                    detections = validate_detections(self.detector.detect(image))
                except Exception:
                    raise WorkerError("inference_failed") from None
                observation = self._observation(camera, source, started, detections=detections)
            except WorkerError as exc:
                log_error(exc.code)
                observation = self._observation(camera, source, started, error=exc.code)
            # Invalidate old detections before replacing the fixed-path JPEG.
            if self._current(clock, request, camera, request_only) is None:
                return "stale"
            document = {
                "id": camera, "pk": "vision", "epochId": clock.epoch_id,
                "layoutVersion": clock.layout_version, "variant": clock.variant,
                "observation": observation,
            }
            if observation["status"] == "ready":
                self.store.upsert_observation({
                    **document,
                    "observation": self._observation(camera, source, started, warming=True),
                })
                if self._current(clock, request, camera, request_only) is None:
                    return "stale"
                try:
                    self.store.upload_frame(camera, jpeg, observation["id"])
                except Exception:
                    log_error("frame_upload_failed")
                    observation = self._observation(
                        camera, source, started, error="frame_upload_failed"
                    )
                if self._current(clock, request, camera, request_only) is None:
                    return "stale"
                if observation["status"] == "ready":
                    document["frameSha256"] = hashlib.sha256(jpeg).hexdigest()
            document["observation"] = observation
            self.store.upsert_observation(document)
            if observation["status"] == "ready":
                self._qualitative(clock, observation, jpeg)
            if request is not None:
                self.request_done.add(camera)
            self.health.pulse()
            return observation["status"]
        except Exception as exc:
            code = exc.code if isinstance(exc, WorkerError) else "worker_unavailable"
            self.health.pulse(code)
            log_error(code)
            return "error"


def main():
    # Never let third-party handlers emit exception bodies, endpoints or credentials.
    import logging

    logging.disable(logging.CRITICAL)
    health = Health()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", 8081), health_handler(health))
    except Exception:
        log_error("worker_unavailable")
        return 1
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stopped = threading.Event()

    def stop(*_):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store = None
    try:
        settings = Settings.from_env()
        store = AzureStore(settings)
        cache = MediaCache(
            settings.cache_path, store.download_media, max_bytes=settings.cache_bytes,
            max_item_bytes=settings.cache_item_bytes, max_entries=settings.cache_entries,
            ttl=settings.cache_ttl_seconds,
        )
        qualitative = QualitativeStage(IsolatedVisualClient(
            settings.qualitative_endpoint, settings.client_id, enabled=settings.qualitative_enabled,
        ))
        worker = Worker(store, cache, None, health=health, request_ttl=settings.request_ttl_seconds,
                        qualitative=qualitative)
        retry_model_at = 0
        while not stopped.is_set():
            started = time.monotonic()
            if worker.detector is None and started >= retry_model_at:
                try:
                    worker.detector = Detector()
                    health.model_ready = True
                except Exception:
                    log_error("model_unavailable")
                    retry_model_at = time.monotonic() + 60
            worker.tick()
            # Long inference never causes a burst of catch-up requests.
            stopped.wait(max(0, settings.interval_seconds - (time.monotonic() - started)))
    except Exception as exc:
        code = exc.code if isinstance(exc, WorkerError) else "worker_unavailable"
        health.pulse(code)
        log_error(code)
        return 1
    finally:
        health.stopped = True
        server.shutdown()
        server.server_close()
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
