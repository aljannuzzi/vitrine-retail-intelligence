import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
import logging
import time
from uuid import uuid4

from fastapi import HTTPException

from .models import QualitativeObservation
from .simulation import (ACTIONS, CAMERAS, advance, clock_document, iso, public_run, seed,
                         snapshot, variant)
from .storage import Conflict

PK = "store-demo"
logger = logging.getLogger("retail")


class ObservationBatch(dict):
    def __init__(self, values, epoch, layout, qualitative=None):
        super().__init__(values)
        self.epoch, self.layout = epoch, layout
        self.qualitative = deepcopy(qualitative or {})

    def authorized_for(self, state):
        return self.epoch == state["epochId"] and self.layout == state["layoutVersion"]


def authorized_observations(values, state):
    if isinstance(values, ObservationBatch):
        if not values.authorized_for(state):
            return ObservationBatch({}, state["epochId"], state["layoutVersion"])
        return values
    return values or {}


def evidence_fresh_since(state):
    return state["interventions"][-1]["at"] if state["interventions"] else state["activeSince"]


def timestamp_seconds(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def authorized_qualitative(values, state, now):
    if not isinstance(values, ObservationBatch) or not values.authorized_for(state):
        return []
    if not isinstance(values.qualitative, dict):
        return []
    current_variant = variant(state)
    minimum = timestamp_seconds(evidence_fresh_since(state))
    evidence = []
    for camera in CAMERAS:
        item = values.qualitative.get(camera)
        if item is None:
            continue
        try:
            value = QualitativeObservation.model_validate(item).model_dump()
        except Exception:
            continue
        if value["cameraId"] != camera:
            continue
        if (value["epochId"] != state["epochId"] or value["layoutVersion"] != state["layoutVersion"] or
                value["variant"] != current_variant or value["status"] != "ready"):
            continue
        stamp = timestamp_seconds(value["observedAt"])
        if stamp < minimum or not 0 <= now - stamp <= 300:
            continue
        evidence.append(value)
    return evidence


class Engine:
    def __init__(self, repository, foundry, media, now=time.time):
        self.repo, self.foundry, self.media, self.now = repository, foundry, media, now
        self.lock = asyncio.Lock()
        self.owner = str(uuid4())
        self.state = None
        self.etag = None
        self.ready = False
        self.tasks = set()
        self.job_ids = set()
        self.needs_job_recovery = False
        self.ticker = None

    async def initialize(self):
        state = await self.repo.read_document("state", PK)
        now = self.now()
        if state and state.get("leaseUntil", 0) > now and state.get("owner") != self.owner:
            raise Conflict("Another API owns the scenario; max one replica")
        self.state = state or seed(now)
        self.etag = self.state.pop("_etag", None)
        self.state["owner"] = self.owner
        async with self.transaction(recover=True):
            pass

    @asynccontextmanager
    async def transaction(self, viewer=False, recover=False, release=False):
        async with self.lock:
            if self.state.get("owner") not in (None, self.owner):
                raise Conflict("Scenario writer lease lost")
            # Drain the durable batch before accepting more audit work. An outage
            # cannot grow either the outbox or the retained archive without bound.
            await self._flush_archives()
            now = self.now()
            working = deepcopy(self.state)
            advance(working, now)
            for run in working["agentRuns"]:
                if ((recover or self.needs_job_recovery) and
                        run["status"] in ("queued", "running") and run["id"] not in self.job_ids):
                    run.update(status="failed", completedAt=iso(now), recommendations=[],
                               error="Execução interrompida antes da conclusão; solicite nova análise.")
                    self.archive(working, run)
            if viewer:
                working["lastViewer"] = max(working["lastViewer"], now)
            yield working
            working["owner"] = self.owner
            working["leaseUntil"] = 0 if release else now + 30
            working["revision"] += 1
            await self._commit(working, now)
            self.needs_job_recovery = False
            try:
                await self._flush_archives()
                self.ready = True
            except Conflict:
                self.ready = False
                raise
            except Exception:
                if self.state.get("owner") != self.owner:
                    self.ready = False
                    raise
                # Reservation is durable; don't strand its task on an archive
                # outage. Startup/ticks replay the unacknowledged batch.
                self.ready = False
                logger.warning("Audit batch pending; durable retry required")

    async def _commit(self, working, now):
        try:
            etag = await self.repo.compare_write(
                working, self.etag, upserts=[clock_document(working, now)])
        except Exception:
            self.ready = False
            self.needs_job_recovery = True
            # A timed-out batch may have committed both state and clock.
            try:
                persisted = await self.repo.read_document("state", PK)
                if persisted:
                    self.etag = persisted.pop("_etag", None)
                    self.state = persisted
            except Exception:
                pass
            raise
        self.state, self.etag = working, etag

    async def _flush_archives(self):
        pending = self.state.get("pendingArchives")
        if not pending:
            return
        revision = self.state["revision"]
        fence = await self.repo.read_document("archive-fence", "agent-runs")
        if fence is None or fence["revision"] < revision:
            # Fence and effects share a partition: a delayed former owner cannot
            # replay an older run over a newer receipt, reset or retention batch.
            await self.repo.compare_write(
                {"id": "archive-fence", "pk": "agent-runs", "revision": revision},
                fence["_etag"] if fence else None,
                upserts=[{"id": id, "pk": "agent-runs", "run": run}
                         for id, run in pending.items() if run is not None],
                deletes=[id for id, run in pending.items() if run is None])
        acknowledged = deepcopy(self.state)
        acknowledged.pop("pendingArchives")
        await self._commit(acknowledged, acknowledged["lastWall"])

    async def tick(self):
        while True:
            await asyncio.sleep(2)
            try:
                async with self.transaction(recover=True):
                    pass
            except Exception:
                self.ready = False

    def start(self):
        self.ticker = asyncio.create_task(self.tick())

    async def close(self):
        tasks = list(self.tasks)
        if self.ticker:
            tasks.append(self.ticker)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.state and self.state.get("owner") == self.owner:
            try:
                async with self.transaction(release=True) as doc:
                    for run in doc["agentRuns"]:
                        if run["status"] in ("queued", "running"):
                            run.update(status="failed", completedAt=iso(self.now()),
                                       error="Execução interrompida pelo encerramento.")
                            self.archive(doc, run)
            except Exception:
                pass

    async def view(self, observations=None, touch=True):
        if touch:
            async with self.transaction(viewer=True):
                pass
        async with self.lock:
            state = deepcopy(self.state)
            observations = authorized_observations(observations, state)
        try:
            footage = await self.media.footage_ready(variant(state))
        except Exception:
            footage = False
        return snapshot(state, self.now(), observations, self.foundry.available,
                        self.repo.mode, footage)

    async def control(self, action, speed):
        async with self.transaction(viewer=True) as state:
            if action == "reset":
                fresh = seed(self.now(), state["revision"])
                fresh["lastViewer"] = self.now()
                # Keep the global model budget across epochs; reset is not a rate-limit bypass.
                fresh["agentStarts"] = state["agentStarts"]
                fresh["lastVisionRequest"] = state.get("lastVisionRequest", 0)
                fresh["runHistory"] = state.get("runHistory", [])
                for run in state["agentRuns"]:
                    if run["status"] in ("queued", "running"):
                        run.update(status="failed", completedAt=iso(self.now()),
                                   recommendations=[], error="Cenário reiniciado; execução obsoleta.")
                    self.archive(state, run)
                if state.get("pendingArchives"):
                    fresh["pendingArchives"] = state["pendingArchives"]
                state.clear()
                state.update(fresh)
            else:
                state["state"] = "paused" if action == "pause" else "running"
            if speed is not None:
                state["speed"] = speed

    async def approve(self, run_id, recommendation_id, action_id):
        async with self.transaction(viewer=True) as state:
            run = next((r for r in state["agentRuns"] if r["id"] == run_id), None)
            if not run or run["_epochId"] != state["epochId"] or run["status"] != "completed":
                raise HTTPException(409, "stale_or_unknown_run")
            rec = next((r for r in run["recommendations"] if r["id"] == recommendation_id), None)
            if not rec or rec["actionId"] != action_id or rec["requiresApproval"] is not True:
                raise HTTPException(409, "unauthorized_recommendation")
            previous = next((i for i in state["interventions"] if i["actionId"] == action_id), None)
            if previous:
                if previous["sourceRunId"] == run_id and run.get("_approved", {}).get(
                        recommendation_id) == previous["id"]:
                    return deepcopy(previous)
                raise HTTPException(409, "action_already_applied")
            if run["_layoutVersion"] != state["layoutVersion"]:
                raise HTTPException(409, "stale_layout")
            action = next((a for a in ACTIONS if a["id"] == action_id), None)
            if not action:
                raise HTTPException(409, "unsupported_action")
            state["layoutVersion"] += 1
            state["comparison"] = None
            intervention = {"id": str(uuid4()), "actionId": action_id, "title": action["title"],
                            "at": iso(self.now()), "elapsedSeconds": state["elapsedSeconds"],
                            "layoutVersion": state["layoutVersion"], "sourceRunId": run_id,
                            "provenance": "approved-simulation"}
            state["interventions"].append(intervention)
            run.setdefault("_approved", {})[recommendation_id] = intervention["id"]
            return deepcopy(intervention)

    async def run_agent(self, kind, question, observations):
        if not self.foundry.available:
            raise HTTPException(503, "foundry_unavailable")
        async with self.transaction(viewer=True) as state:
            now = self.now()
            state["agentStarts"] = [s for s in state["agentStarts"] if s > now - 3600]
            if (len(state["agentStarts"]) >= 20 or
                    sum(s > now - 60 for s in state["agentStarts"]) >= 3):
                raise HTTPException(429, "agent_rate_limit")
            if any(r["status"] in ("queued", "running") for r in state["agentRuns"]) or self.tasks:
                raise HTTPException(429, "agent_busy")
            state["agentStarts"].append(now)
            observations = authorized_observations(observations, state)
            snap = snapshot(state, now, observations)
            qualitative = authorized_qualitative(observations, state, now)
            epoch = state["epochId"]
            evidence = [{"id": f"{epoch}:metrics", "kind": "simulated-aggregates",
                         "provenance": "simulation", "metrics": snap["metrics"]},
                        {"id": f"{epoch}:experiment", "kind": "virtual-comparison",
                         "provenance": "simulation", "experiment": snap["experiment"]}]
            evidence_index = {item["id"]: position for position, item in enumerate(evidence)}
            obs = [deepcopy(v) for v in observations.values() if v["status"] == "ready"]
            for value in obs:
                value.pop("frameUri", None)
                value["detections"] = value["detections"][:32]
                evidence_index[value["id"]] = len(evidence)
                evidence.append({"id": value["id"], "kind": "model-inference",
                                 "provenance": "model-inference", "observation": value})
            for value in qualitative:
                source_id = value["sourceObservationId"]
                position = evidence_index.get(source_id)
                if position is None:
                    evidence_index[source_id] = len(evidence)
                    evidence.append({"id": source_id, "kind": "visual-qualitative",
                                     "provenance": "model-inference",
                                     "qualitative": deepcopy(value)})
                    continue
                evidence[position]["kind"] = "visual-qualitative"
                evidence[position]["provenance"] = "model-inference"
                evidence[position]["qualitative"] = deepcopy(value)
            context = {"epochId": epoch, "evidence": evidence, "metrics": snap["metrics"],
                       "zones": snap["zones"], "availableActions": snap["availableActions"],
                       "experiment": snap["experiment"], "interventions": snap["interventions"],
                       "observations": obs}
            run = {"id": str(uuid4()), "agent": kind, "status": "queued", "createdAt": iso(now),
                   "completedAt": None, "agentName": f"vitrine-{kind}", "agentVersion": None,
                   "responseId": None, "conversationId": None, "toolCalls": [], "summary": None,
                   "recommendations": [], "limitations": [], "error": None,
                   "execution": "azure-foundry-agent", "_epochId": epoch,
                   "_layoutVersion": state["layoutVersion"]}
            state["agentRuns"].append(run)
            state["agentRuns"] = state["agentRuns"][-10:]
            history = state.setdefault("runHistory", [])
            history.append(run["id"])
            if len(history) > 100:
                state.setdefault("pendingArchives", {})[history.pop(0)] = None
            self.archive(state, run)
        task = asyncio.create_task(self._execute(run["id"], epoch, kind, question, context))
        self.tasks.add(task)
        self.job_ids.add(run["id"])
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(lambda completed: self.job_ids.discard(run["id"]))
        return public_run(run)

    def archive(self, state, run):
        state.setdefault("pendingArchives", {})[run["id"]] = deepcopy(run)

    async def _execute(self, id, epoch, kind, question, context):
        async def update(fields):
            obsolete = False
            async with self.transaction() as state:
                run = next((r for r in state["agentRuns"] if r["id"] == id), None)
                if state["epochId"] != epoch or run is None:
                    obsolete = True
                    archived = await self.repo.read_document(id, "agent-runs")
                    if archived:
                        old = archived["run"]
                        old.update({k: v for k, v in fields.items() if k in (
                            "agentVersion", "responseId", "conversationId", "toolCalls")})
                        old.update(status="failed", recommendations=[], completedAt=iso(self.now()),
                                   error="Cenário reiniciado; execução obsoleta.")
                        self.archive(state, old)
                else:
                    run.update(fields)
                    self.archive(state, run)
            if obsolete and "status" in fields:
                raise HTTPException(409, "scenario_reset")
        try:
            await update({"status": "running"})
            # A flush timeout can hide a successor's takeover. Never invoke a
            # model until the running audit and its fenced acknowledgment succeed.
            if not self.ready:
                raise Conflict("Execution requires a synchronized audit")
            async def audit(metadata):
                safe = {k: v for k, v in metadata.items() if k in (
                    "agentVersion", "responseId", "conversationId", "toolCalls")}
                await update(safe)
            result = await self.foundry.execute(kind, question, context, audit)
            await update({**result, "status": "completed", "completedAt": iso(self.now())})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                metadata = getattr(exc, "metadata", None) or {}
                safe = {k: v for k, v in metadata.items() if k in (
                    "agentVersion", "responseId", "conversationId", "toolCalls")}
                await update({**safe, "status": "failed", "completedAt": iso(self.now()),
                              "recommendations": [],
                              "error": "Agente gerenciado indisponível ou resposta inválida; nenhuma ação executada."})
            except Exception:
                pass
