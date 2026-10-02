"""Offline backend invariants using unittest, SQLite, and injected services."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from retail.app import create_app
from retail.auth import COOKIE, SESSION_SECONDS, LoginBudget, Sessions
from retail.config import Settings
from retail.engine import Engine, PK
from retail.media import BlobMedia, byte_range
from retail.models import VisionObservation
from retail.simulation import (
    CAMERAS, advance, clock_document, experiment, iso, seed, snapshot, step,
    unavailable_vision,
)
from retail.storage import Conflict, CosmosRepository, LocalRepository, create_repository


ORIGIN = "https://testserver"
PASSWORD = "test-only-password-not-a-secret"
START = 1_800_000_000.0


def settings(**changes):
    return replace(Settings(
        environment="local", local_mode=True, local_database=":memory:",
        app_password=PASSWORD, session_secret="test-only-signing-key-" * 2,
        public_origin=ORIGIN,
    ), **changes)


class Clock:
    def __init__(self, value=START):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds
        return self.value


class FakeFoundry:
    def __init__(self, available=False, blocked=False, fail=False):
        self.available = available
        self.blocked = blocked
        self.fail = fail
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    async def execute(self, kind, question, context, audit):
        self.calls.append((kind, question, deepcopy(context)))
        self.started.set()
        await audit({"agentVersion": "test-1", "responseId": "response-test",
                     "conversationId": "conversation-test", "toolCalls": ["read_demo_context"]})
        if self.blocked:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("private upstream error must not reach the API")
        return {
            "summary": "Test-only simulated comparison.",
            "recommendations": [{
                "id": f"rec-{action['id']}", "actionId": action["id"],
                "title": action["title"], "rationale": "Synthetic evidence only.",
                "evidenceIds": [context["evidence"][0]["id"]],
                "caveats": ["Not a real-world causal result."],
                "priority": "medium", "requiresApproval": True,
            } for action in context["availableActions"] if not action["applied"]],
            "limitations": ["Synthetic data; offline service double."],
        }


class FakeMedia:
    def __init__(self, ready=False):
        self.ready = ready
        self.calls = []
        self.closed = False

    async def initialize(self):
        pass

    async def close(self):
        self.closed = True

    async def footage_ready(self, variant):
        return self.ready

    async def stream(self, path, request, content_type):
        self.calls.append(path)
        if not self.ready:
            raise HTTPException(503, "media_unavailable")
        data = b"0123456789"
        offset, length, partial = byte_range(request.headers.get("range"), len(data))
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
        if partial:
            headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{len(data)}"
        return Response(
            b"" if request.method == "HEAD" else data[offset:offset + length],
            status_code=206 if partial else 200, headers=headers, media_type=content_type,
        )


def ready_observation(now=START, camera=CAMERAS[0]):
    return {
        "id": f"{camera}:test-observation", "cameraId": camera, "status": "ready",
        "observedAt": iso(now), "sourceSeconds": 5.0, "people": 1,
        "detections": [{"label": "person", "score": 0.9, "box": [0.1, 0.2, 0.5, 0.8]}],
        "frameUri": f"/api/vision/frames/{camera}.jpg", "latencyMs": 20.0,
        "model": "RT-DETRv2", "modelRevision": "test-revision",
        "provenance": "model-inference", "error": None,
    }


async def finish_runs(engine):
    if engine.tasks:
        await asyncio.wait_for(asyncio.gather(*list(engine.tasks)), timeout=3)
    await asyncio.sleep(0)


class SessionTests(unittest.TestCase):
    def test_signed_cookie_expiry_and_future_issue_boundary(self):
        sessions = Sessions(settings())
        token = sessions.issue(START)
        self.assertFalse(sessions.valid(token, START - 1))
        self.assertTrue(sessions.valid(token, START))
        self.assertTrue(sessions.valid(token, START + SESSION_SECONDS - 1))
        self.assertFalse(sessions.valid(token, START + SESSION_SECONDS))
        self.assertNotEqual(token, sessions.issue(START))

    def test_tampered_and_malformed_cookies_are_rejected(self):
        sessions = Sessions(settings())
        token = sessions.issue(START)
        body, signature = token.split(".")
        forged = base64.urlsafe_b64encode(json.dumps({
            "iat": START, "exp": START + SESSION_SECONDS * 2,
        }).encode()).decode().rstrip("=")
        altered_signature = ("1" if signature[0] == "0" else "0") + signature[1:]
        for value in (None, "", "garbage", "." * 3, "x" * 513,
                      body + "." + altered_signature, forged + "." + signature):
            with self.subTest(token=value):
                self.assertFalse(sessions.valid(value, START))

    def test_signed_malformed_payloads_fail_closed(self):
        sessions = Sessions(settings())
        for payload in ({}, [], {"iat": START, "exp": START + SESSION_SECONDS + 1},
                        {"iat": START, "exp": "tomorrow"}):
            with self.subTest(payload=payload):
                body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
                signature = hmac.new(sessions.key, body.encode(), hashlib.sha256).hexdigest()
                self.assertFalse(sessions.valid(body + "." + signature, START))

    def test_password_rotation_revokes_existing_sessions_even_with_fixed_secret(self):
        old = Sessions(settings())
        new = Sessions(settings(app_password=PASSWORD + "-rotated"))
        self.assertTrue(old.password_valid(PASSWORD))
        self.assertFalse(old.password_valid(PASSWORD.upper()))
        self.assertFalse(new.password_valid(PASSWORD))
        self.assertFalse(new.valid(old.issue(START), START))
        self.assertTrue(new.valid(new.issue(START), START))

    def test_login_budget_per_client_global_and_sliding_window(self):
        budget = LoginBudget()
        for _ in range(8):
            self.assertTrue(budget.allow("client", START))
        self.assertFalse(budget.allow("client", START + 59))
        self.assertTrue(budget.allow("client", START + 60))
        budget = LoginBudget()
        for index in range(30):
            self.assertTrue(budget.allow(str(index), START))
        self.assertFalse(budget.allow("new-client", START))
        self.assertTrue(budget.allow("new-client", START + 60))


class ConfigurationTests(unittest.TestCase):
    def test_local_requires_both_explicit_switches(self):
        for environment, enabled in (("local", False), ("azure", True), ("demo", True)):
            with self.subTest(environment=environment, enabled=enabled):
                with self.assertRaises(ValueError):
                    settings(environment=environment, local_mode=enabled).validate()
        self.assertIsInstance(create_repository(settings()), LocalRepository)

    def test_environment_loading_does_not_default_to_local(self):
        env = {"ENVIRONMENT": "local", "APP_PASSWORD": PASSWORD, "PUBLIC_ORIGIN": ORIGIN}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()
        with patch.dict(os.environ, {**env, "LOCAL_MODE": "true"}, clear=True):
            self.assertTrue(Settings.from_env().local_mode)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_missing_azure_configuration_never_selects_local_repository(self):
        with self.assertRaisesRegex(ValueError, "no local fallback"):
            create_repository(settings(environment="azure", local_mode=False))
        cfg = settings(environment="azure", local_mode=False,
                       cosmos_endpoint="https://example.documents.azure.com",
                       azure_storage_account="testaccount",
                       foundry_project_endpoint="https://example.invalid/project")
        repository = create_repository(cfg)
        self.assertIsInstance(repository, CosmosRepository)
        self.assertIsNone(repository.client)

    def test_password_and_signing_secret_minimums(self):
        for change in ({"app_password": "short"}, {"session_secret": "short"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                settings(**change).validate()
        settings(session_secret="").validate()

    def test_only_exact_https_origin_is_valid_configuration(self):
        for origin in ("", "http://testserver", "https://testserver/",
                       "https://testserver/path", "https://testserver?q=1",
                       "https://testserver#fragment", "https://user:pass@testserver"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                settings(public_origin=origin).validate()
        settings().validate()


class SimulationTests(unittest.TestCase):
    def test_no_viewer_does_not_advance_or_autopause_before_first_visit(self):
        state = seed(START)
        advance(state, START + 1000)
        self.assertEqual(state["elapsedSeconds"], 0)
        self.assertEqual(state["state"], "running")
        self.assertEqual(state["posEvents"], [])

    def test_supported_speeds_pause_and_no_backward_clock_replay(self):
        for speed in (1, 2, 5):
            with self.subTest(speed=speed):
                state = seed(START)
                state.update(lastViewer=START, speed=speed)
                advance(state, START + 10)
                self.assertEqual(state["elapsedSeconds"], 10 * speed)
                before = deepcopy(state["posEvents"])
                advance(state, START + 5)
                self.assertEqual(state["elapsedSeconds"], 10 * speed)
                self.assertEqual(state["posEvents"], before)
                self.assertEqual(state["lastWall"], START + 10)
                state["state"] = "paused"
                advance(state, START + 20)
                self.assertEqual(state["elapsedSeconds"], 10 * speed)
                state["state"] = "running"
                advance(state, START + 25)
                self.assertEqual(state["elapsedSeconds"], 15 * speed)

    def test_inactivity_caps_progress_then_pauses_without_catchup(self):
        state = seed(START)
        state.update(lastViewer=START, speed=2)
        advance(state, START + 300)
        self.assertEqual(state["elapsedSeconds"], 240)
        self.assertEqual(state["state"], "running")
        advance(state, START + 301)
        self.assertEqual(state["state"], "paused")
        state["lastViewer"] = START + 400
        advance(state, START + 400)
        self.assertEqual(state["elapsedSeconds"], 240)
        state["state"] = "running"
        advance(state, START + 405)
        self.assertEqual(state["elapsedSeconds"], 250)

    def test_pos_net_revenue_units_and_sale_only_transaction_denominator(self):
        state = seed(START)
        for second in range(5, 301, 5):
            step(state, second, START + second)
        events = state["posEvents"]
        sales = [event for event in events if event["type"] == "sale"]
        returns = [event for event in events if event["type"] == "return"]
        self.assertTrue(sales)
        self.assertTrue(returns)
        metrics = state["metrics"]
        self.assertEqual(metrics["revenueCents"], sum(e["amountCents"] for e in events))
        self.assertEqual(metrics["units"], sum(e["units"] for e in events))
        self.assertEqual(metrics["transactions"], len(sales))
        self.assertEqual(metrics["averageBasketCents"],
                         round(metrics["revenueCents"] / len(sales)))
        for zone, totals in state["zoneTotals"].items():
            selected = [event for event in events if event["zoneId"] == zone]
            self.assertEqual(totals["revenueCents"], sum(e["amountCents"] for e in selected))
            self.assertEqual(totals["units"], sum(e["units"] for e in selected))
        prior_sale = None
        for event in events:
            self.assertEqual(event["provenance"], "simulated-pos")
            if event["type"] == "sale":
                self.assertGreater(event["amountCents"], 0)
                prior_sale = event
            else:
                self.assertIsNotNone(prior_sale)
                self.assertEqual(event["sku"], prior_sale["sku"])
                self.assertEqual(event["amountCents"], -prior_sale["amountCents"])
                self.assertEqual(event["units"], -prior_sale["units"])
                self.assertNotEqual(event["id"], prior_sale["id"])
                prior_sale = None

    def test_histories_bounded_but_lifetime_totals_preserved(self):
        state = seed(START)
        all_events = []
        for second in range(5, 10001, 5):
            previous_sequence = state["sequence"]
            step(state, second, START + second)
            if state["sequence"] != previous_sequence:
                all_events.append(deepcopy(state["posEvents"][-1]))
        self.assertEqual(len(state["posEvents"]), 100)
        self.assertEqual(len(state["series"]), 240)
        self.assertEqual(len(state["samples"]), 720)
        self.assertGreater(len(all_events), 100)
        self.assertEqual(len({event["id"] for event in all_events}), len(all_events))
        self.assertEqual(state["metrics"]["revenueCents"],
                         sum(event["amountCents"] for event in all_events))
        self.assertEqual(state["metrics"]["transactions"],
                         sum(event["type"] == "sale" for event in all_events))
        self.assertEqual(state["metrics"]["units"], sum(event["units"] for event in all_events))
        self.assertEqual(state["series"][-1]["revenueCents"], state["metrics"]["revenueCents"])

    def test_each_approved_action_changes_sales_not_visit_or_control_denominators(self):
        for action, zone in (("reposition-denim", "denim"),
                             ("replenish-essentials", "essentials")):
            with self.subTest(action=action):
                baseline = seed(START)
                treated = deepcopy(baseline)
                treated["interventions"] = [{"actionId": action, "elapsedSeconds": 0, "at": iso(START)}]
                for second in range(5, 601, 5):
                    step(baseline, second, START + second)
                    step(treated, second, START + second)
                self.assertEqual(treated["metrics"]["simulatedVisits"],
                                 baseline["metrics"]["simulatedVisits"])
                self.assertEqual(treated["controlRevenue"], baseline["controlRevenue"])
                self.assertNotEqual(treated["zoneTotals"][zone]["revenueCents"],
                                    baseline["zoneTotals"][zone]["revenueCents"],
                                    "Approved action must affect its programmed synthetic sales.")

    def test_reset_seed_changes_epoch_and_event_ids(self):
        old, new = seed(START), seed(START, revision=42)
        for state in (old, new):
            step(state, 15, START + 15)
        self.assertNotEqual(old["epochId"], new["epochId"])
        self.assertNotEqual(old["posEvents"][0]["id"], new["posEvents"][0]["id"])
        self.assertEqual(new["revision"], 42)

    def comparison_state(self):
        state = seed(START)
        state["elapsedSeconds"] = 120
        state["interventions"] = [{"elapsedSeconds": 60, "at": iso(START + 60)}]
        state["samples"] = [
            {"second": second, "visits": 10 if second <= 60 else 20,
             "revenue": 100 if second <= 60 else 300,
             "control": 50 if second <= 60 else 100, "transactions": 1}
            for second in range(5, 121, 5)
        ]
        return state

    def test_experiment_uses_complete_equal_windows_and_visit_normalization(self):
        state = self.comparison_state()
        state["samples"].extend([
            {"second": second, "visits": 99999, "revenue": 99999,
             "control": 99999, "transactions": 99999} for second in (0, 125)
        ])
        result = experiment(state)
        self.assertEqual(result["status"], "simulated-comparison")
        self.assertEqual(result["beforeRevenuePer100Visits"], 1000)
        self.assertEqual(result["afterRevenuePer100Visits"], 1500)
        self.assertEqual(result["controlChangePercent"], 0)
        self.assertEqual(result["relativeChangePercent"], 50)
        self.assertEqual(result["beforeTransactions"], 12)
        self.assertEqual(result["afterTransactions"], 12)
        self.assertTrue(result["caveat"])

    def test_experiment_refuses_incomplete_or_unsafe_denominators(self):
        variants = []
        state = self.comparison_state()
        state["elapsedSeconds"] = 119
        variants.append(("not elapsed", state))
        state = self.comparison_state()
        state["samples"].pop()
        variants.append(("missing sample", state))
        state = self.comparison_state()
        state["interventions"][0]["elapsedSeconds"] = 55
        variants.append(("no before window", state))
        for half in ("before", "after"):
            for key, value in (("visits", 1), ("control", 0)):
                state = self.comparison_state()
                for sample in state["samples"]:
                    if (sample["second"] <= 60) == (half == "before"):
                        sample[key] = value
                variants.append((f"{half} {key}", state))
        for revenue in (0, -100):
            state = self.comparison_state()
            for sample in state["samples"][:12]:
                sample["revenue"] = revenue
            variants.append(("nonpositive baseline", state))
        for label, state in variants:
            with self.subTest(label=label):
                result = experiment(state)
                self.assertEqual(result["status"], "collecting")
                for field in ("beforeRevenuePer100Visits", "afterRevenuePer100Visits",
                              "controlChangePercent", "relativeChangePercent"):
                    self.assertIsNone(result[field])

    def test_experiment_negative_after_revenue_is_not_hidden(self):
        state = self.comparison_state()
        for sample in state["samples"][12:]:
            sample["revenue"] = -100
        result = experiment(state)
        self.assertEqual(result["status"], "simulated-comparison")
        self.assertEqual(result["afterRevenuePer100Visits"], -500)
        self.assertEqual(result["relativeChangePercent"], -150)
        self.assertEqual(experiment(seed(START))["status"], "not-started")

    def test_clock_document_and_snapshot_provenance_are_explicit(self):
        state = seed(START)
        state.update(lastViewer=START, elapsedSeconds=65, speed=5)
        clock = clock_document(state, START)
        self.assertEqual(clock["epochId"], state["epochId"])
        self.assertEqual(clock["activeUntil"], iso(START + 120))
        self.assertEqual(clock["updatedAt"], iso(START))
        self.assertEqual(clock["variant"], "baseline")
        snap = snapshot(state, START)
        self.assertTrue(snap["scenario"]["simulation"])
        self.assertEqual(snap["metrics"]["provenance"], "simulated-pos")
        for camera in snap["cameras"]:
            self.assertEqual(camera["sourceSeconds"], 5)
            self.assertEqual(camera["footage"], "synthetic-3d")
            self.assertIsNone(camera["vision"]["people"])
            self.assertEqual(camera["vision"]["provenance"], "model-inference")
        snap["metrics"]["units"] = 999
        snap["zones"][0]["revenueCents"] = 999
        self.assertEqual(state["metrics"]["units"], 0)
        self.assertEqual(state["zoneTotals"]["denim"]["revenueCents"], 0)


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = LocalRepository(":memory:")
        await self.repo.initialize()
        self.addAsyncCleanup(self.repo.close)

    async def test_compare_and_swap_rejects_duplicate_create_and_stale_etag(self):
        doc = {"id": "test", "pk": PK, "value": 1}
        etag = await self.repo.compare_write(doc, None)
        self.assertEqual((await self.repo.read_document("test", PK))["value"], 1)
        with self.assertRaises(Conflict):
            await self.repo.compare_write({**doc, "value": 2}, None)
        new_etag = await self.repo.compare_write({**doc, "value": 3}, etag)
        self.assertNotEqual(new_etag, etag)
        with self.assertRaises(Conflict):
            await self.repo.compare_write({**doc, "value": 4}, etag)
        self.assertEqual((await self.repo.read_document("test", PK))["value"], 3)

    async def test_partition_isolation_and_private_metadata_not_persisted(self):
        for pk, value in (("one", 1), ("two", 2)):
            await self.repo.write_document({"id": "same", "pk": pk, "value": value,
                                            "_private": "must-not-persist", "_etag": "forged"})
        for pk, value in (("one", 1), ("two", 2)):
            result = await self.repo.read_document("same", pk)
            self.assertEqual(result["value"], value)
            self.assertNotIn("_private", result)
            self.assertNotEqual(result["_etag"], "forged")
        self.assertIsNone(await self.repo.read_document("same", "missing"))

    async def test_missing_compare_update_does_not_insert(self):
        with self.assertRaises(Conflict):
            await self.repo.compare_write({"id": "missing", "pk": PK}, "1")
        self.assertIsNone(await self.repo.read_document("missing", PK))

    async def test_sqlite_restart_preserves_document_and_cas_version(self):
        path = Path(__file__).parent / f"unittest-{uuid4().hex}.sqlite"
        first = LocalRepository(str(path))
        second = LocalRepository(str(path))
        try:
            await first.initialize()
            etag = await first.compare_write({"id": "persisted", "pk": PK, "value": 7}, None)
            await first.close()
            await second.initialize()
            saved = await second.read_document("persisted", PK)
            self.assertEqual(saved["value"], 7)
            self.assertEqual(saved["_etag"], etag)
            await second.compare_write({**saved, "value": 8}, etag)
            with self.assertRaises(Conflict):
                await second.compare_write(saved, etag)
        finally:
            await first.close()
            await second.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.repo = LocalRepository(":memory:")
        await self.repo.initialize()
        self.addAsyncCleanup(self.repo.close)
        self.foundry = FakeFoundry(available=True)
        self.media = FakeMedia()
        self.engine = Engine(self.repo, self.foundry, self.media, self.clock)
        await self.engine.initialize()
        self.addAsyncCleanup(self.engine.close)

    async def completed_run(self):
        run = await self.engine.run_agent("merchandising", "Assess synthetic layout.", {})
        await finish_runs(self.engine)
        return next(r for r in self.engine.state["agentRuns"] if r["id"] == run["id"])

    async def test_pause_resume_speed_and_persisted_clock(self):
        await self.engine.control("resume", 1)
        self.clock.advance(10)
        await self.engine.control("pause", None)
        self.assertEqual(self.engine.state["elapsedSeconds"], 10)
        self.clock.advance(10)
        await self.engine.control("resume", 2)
        self.clock.advance(10)
        await self.engine.control("pause", None)
        self.assertEqual(self.engine.state["elapsedSeconds"], 30)
        await self.engine.control("resume", 5)
        self.clock.advance(10)
        await self.engine.view()
        self.assertEqual(self.engine.state["elapsedSeconds"], 80)
        clock = await self.repo.read_document("clock", PK)
        for key in ("epochId", "state", "elapsedSeconds", "speed", "layoutVersion"):
            self.assertEqual(clock[key], self.engine.state[key])
        self.assertEqual(clock["activeUntil"], iso(self.clock() + 120))

    async def test_active_lease_rejects_second_owner(self):
        contender = Engine(self.repo, self.foundry, self.media, self.clock)
        before = await self.repo.read_document("state", PK)
        with self.assertRaises(Conflict):
            await contender.initialize()
        self.assertEqual(await self.repo.read_document("state", PK), before)
        self.assertFalse(contender.ready)

    async def test_expired_owner_is_replaced_without_losing_epoch_or_history(self):
        await self.engine.control("resume", 1)
        self.clock.advance(15)
        await self.engine.view()
        before = deepcopy(self.engine.state)
        self.clock.advance(31)
        contender = Engine(self.repo, self.foundry, self.media, self.clock)
        self.addAsyncCleanup(contender.close)
        await contender.initialize()
        self.assertTrue(contender.ready)
        self.assertEqual(contender.state["owner"], contender.owner)
        self.assertEqual(contender.state["epochId"], before["epochId"])
        self.assertGreaterEqual(contender.state["elapsedSeconds"], before["elapsedSeconds"])
        self.assertTrue(set(e["id"] for e in before["posEvents"]).issubset(
            {e["id"] for e in contender.state["posEvents"]}))
        saved = await self.repo.read_document("state", PK)
        with self.assertRaises(Conflict):
            await self.engine.view()
        self.assertEqual(await self.repo.read_document("state", PK), saved)
        self.assertFalse(self.engine.ready)

    async def test_restart_recovers_interrupted_runs_and_keeps_scenario(self):
        async with self.engine.transaction() as state:
            state["agentRuns"].append({"id": "interrupted", "status": "running"})
        old_epoch = self.engine.state["epochId"]
        self.clock.advance(31)
        restarted = Engine(self.repo, self.foundry, self.media, self.clock)
        self.addAsyncCleanup(restarted.close)
        await restarted.initialize()
        self.assertEqual(restarted.state["epochId"], old_epoch)
        run = restarted.state["agentRuns"][0]
        self.assertEqual(run["status"], "failed")
        self.assertIsNotNone(run["completedAt"])
        self.assertTrue(run["error"])
        self.assertEqual(restarted.state["owner"], restarted.owner)

    async def test_transaction_body_failure_rolls_back_memory_and_storage(self):
        before_memory = deepcopy(self.engine.state)
        before_disk = await self.repo.read_document("state", PK)
        before_clock = await self.repo.read_document("clock", PK)
        with self.assertRaises(HTTPException):
            async with self.engine.transaction(viewer=True) as state:
                state["metrics"]["units"] = 100
                raise HTTPException(429, "test-busy")
        self.assertEqual(self.engine.state, before_memory)
        self.assertEqual(await self.repo.read_document("state", PK), before_disk)
        self.assertEqual(await self.repo.read_document("clock", PK), before_clock)

    async def test_cas_failure_does_not_publish_uncommitted_memory_or_clock(self):
        before_memory = deepcopy(self.engine.state)
        before_clock = await self.repo.read_document("clock", PK)
        with patch.object(self.repo, "compare_write", AsyncMock(side_effect=Conflict("test"))):
            with self.assertRaises(Conflict):
                await self.engine.control("reset", 5)
        self.assertEqual(self.engine.state, before_memory)
        self.assertEqual(await self.repo.read_document("clock", PK), before_clock)
        self.assertFalse(self.engine.ready)

    async def test_unavailable_foundry_never_generates_fallback_runs(self):
        self.foundry.available = False
        before = await self.repo.read_document("state", PK)
        with self.assertRaises(HTTPException) as raised:
            await self.engine.run_agent("merchandising", "Question", {})
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.foundry.calls, [])
        self.assertEqual(await self.repo.read_document("state", PK), before)

    async def test_agent_completion_requires_explicit_approval_and_hides_private_fields(self):
        run = await self.completed_run()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["agentVersion"], "test-1")
        self.assertEqual(run["execution"], "azure-foundry-agent")
        self.assertEqual(self.engine.state["interventions"], [])
        view = await self.engine.view(touch=False)
        self.assertFalse(any(key.startswith("_") for key in view["agentRuns"][0]))
        self.assertTrue(all(not a["applied"] for a in view["availableActions"]))

    async def test_forged_approval_and_stale_layout_are_rejected_repeat_is_idempotent(self):
        run = await self.completed_run()
        first, second = run["recommendations"]
        for run_id, rec_id, action in (
            ("forged-run", first["id"], first["actionId"]),
            (run["id"], "forged-rec", first["actionId"]),
            (run["id"], first["id"], second["actionId"]),
        ):
            with self.subTest(run=run_id, rec=rec_id, action=action):
                before = await self.repo.read_document("state", PK)
                with self.assertRaises(HTTPException) as raised:
                    await self.engine.approve(run_id, rec_id, action)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(await self.repo.read_document("state", PK), before)
        approval = await self.engine.approve(run["id"], first["id"], first["actionId"])
        repeated = await self.engine.approve(run["id"], first["id"], first["actionId"])
        self.assertEqual(approval, repeated)
        self.assertEqual(len(self.engine.state["interventions"]), 1)
        self.assertEqual(self.engine.state["layoutVersion"], 2)
        self.assertEqual(approval["provenance"], "approved-simulation")
        with self.assertRaises(HTTPException) as raised:
            await self.engine.approve(run["id"], second["id"], second["actionId"])
        self.assertEqual(raised.exception.detail, "stale_layout")
        clock = await self.repo.read_document("clock", PK)
        self.assertEqual(clock["variant"], "optimized")
        self.assertEqual(clock["layoutVersion"], 2)

    async def test_incomplete_and_unapproved_recommendations_cannot_execute(self):
        run = await self.completed_run()
        rec = run["recommendations"][0]
        for status, approval in (("queued", True), ("running", True),
                                 ("failed", True), ("completed", False)):
            async with self.engine.transaction() as state:
                state["agentRuns"][0]["status"] = status
                state["agentRuns"][0]["recommendations"][0]["requiresApproval"] = approval
            with self.subTest(status=status, requiresApproval=approval):
                with self.assertRaises(HTTPException) as raised:
                    await self.engine.approve(run["id"], rec["id"], rec["actionId"])
                self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.engine.state["interventions"], [])

    async def test_reset_clears_only_current_scenario_and_preserves_definitions_and_budget(self):
        await self.repo.write_document({"id": "foundry-config", "pk": PK, "agents": {"test": 1}})
        await self.repo.write_document({"id": "media-manifest", "pk": "media", "ready": True})
        await self.engine.control("resume", 5)
        self.clock.advance(15)
        await self.engine.view()
        run = await self.completed_run()
        rec = run["recommendations"][0]
        await self.engine.approve(run["id"], rec["id"], rec["actionId"])
        before = deepcopy(self.engine.state)
        await self.engine.control("reset", None)
        current = self.engine.state
        self.assertNotEqual(current["epochId"], before["epochId"])
        self.assertGreater(current["revision"], before["revision"])
        self.assertEqual(current["elapsedSeconds"], 0)
        self.assertEqual(current["layoutVersion"], 1)
        self.assertEqual(current["speed"], 1)
        self.assertEqual(current["metrics"]["revenueCents"], 0)
        for key in ("agentRuns", "interventions", "posEvents", "series", "samples"):
            self.assertEqual(current[key], [])
        self.assertEqual(current["agentStarts"], before["agentStarts"])
        self.assertEqual((await self.repo.read_document("foundry-config", PK))["agents"], {"test": 1})
        self.assertTrue((await self.repo.read_document("media-manifest", "media"))["ready"])
        with self.assertRaises(HTTPException) as raised:
            await self.engine.approve(run["id"], rec["id"], rec["actionId"])
        self.assertEqual(raised.exception.status_code, 409)
        self.clock.advance(15)
        await self.engine.view()
        self.assertTrue({e["id"] for e in before["posEvents"]}.isdisjoint(
            {e["id"] for e in self.engine.state["posEvents"]}))

    async def test_busy_request_is_atomic_and_reset_inflight_result_never_resurrects(self):
        self.foundry.blocked = True
        run = await self.engine.run_agent("merchandising", "Question", {})
        await asyncio.wait_for(self.foundry.started.wait(), 3)
        before_memory = deepcopy(self.engine.state)
        before_disk = await self.repo.read_document("state", PK)
        before_clock = await self.repo.read_document("clock", PK)
        self.clock.advance(5)
        with self.assertRaises(HTTPException) as raised:
            await self.engine.run_agent("experiment", "Second question", {})
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(self.engine.state, before_memory)
        self.assertEqual(await self.repo.read_document("state", PK), before_disk)
        self.assertEqual(await self.repo.read_document("clock", PK), before_clock)
        await self.engine.control("reset", None)
        epoch = self.engine.state["epochId"]
        with self.assertRaises(HTTPException) as raised:
            await self.engine.run_agent("experiment", "Still busy", {})
        self.assertEqual(raised.exception.status_code, 429)
        self.foundry.release.set()
        await finish_runs(self.engine)
        self.assertEqual(self.engine.state["epochId"], epoch)
        self.assertEqual(self.engine.state["agentRuns"], [])
        self.assertEqual(self.engine.state["interventions"], [])
        self.assertNotEqual(self.foundry.calls[0][2]["epochId"], epoch)
        self.assertFalse(self.engine.tasks)
        self.assertFalse(any(r["id"] == run["id"] for r in self.engine.state["agentRuns"]))

    async def test_background_error_is_sanitized_and_cannot_approve(self):
        self.foundry.fail = True
        run = await self.completed_run()
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["recommendations"], [])
        self.assertNotIn("private upstream", run["error"])
        self.assertEqual(run["responseId"], "response-test")
        with self.assertRaises(HTTPException):
            await self.engine.approve(run["id"], "forged", "reposition-denim")

    async def test_agent_rate_limit_survives_reset_and_expires(self):
        for _ in range(3):
            await self.completed_run()
        await self.engine.control("reset", None)
        with self.assertRaises(HTTPException) as raised:
            await self.engine.run_agent("experiment", "Fourth request", {})
        self.assertEqual(raised.exception.status_code, 429)
        self.clock.advance(61)
        self.assertEqual((await self.completed_run())["status"], "completed")

    async def test_agent_evidence_excludes_unavailable_counts_and_frame_paths(self):
        observations = {CAMERAS[0]: ready_observation(),
                        CAMERAS[1]: unavailable_vision(CAMERAS[1])}
        await self.engine.run_agent("merchandising", "Question", observations)
        await finish_runs(self.engine)
        context = self.foundry.calls[0][2]
        self.assertEqual(len(context["observations"]), 1)
        self.assertNotIn("frameUri", context["observations"][0])
        self.assertEqual(context["observations"][0]["people"], 1)
        self.assertIn("frameUri", observations[CAMERAS[0]])
        self.assertEqual({item["kind"] for item in context["evidence"]},
                         {"simulated-aggregates", "virtual-comparison", "model-inference"})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.repo = LocalRepository(":memory:")
        self.foundry = FakeFoundry()
        self.media = FakeMedia()
        self.app = create_app(settings(), self.repo, self.foundry, self.media,
                              now=self.clock, ticker=False)
        self.client = self.enterContext(TestClient(self.app, base_url=ORIGIN))

    def login(self):
        response = self.client.post("/api/session", json={"password": PASSWORD},
                                    headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def write(self, path, data=None, method="POST", origin=ORIGIN):
        return self.client.request(method, path, json=data, headers={"Origin": origin})

    def read_document(self, id, pk=PK):
        return self.client.portal.call(self.repo.read_document, id, pk)

    def put_observation(self, observation, **metadata):
        state = self.read_document("state")
        doc = {"id": CAMERAS[0], "pk": "vision", "epochId": state["epochId"],
               "layoutVersion": state["layoutVersion"], "observation": observation, **metadata}
        self.client.portal.call(self.repo.write_document, doc)

    def test_public_endpoints_and_anonymous_endpoints_have_no_auth_bypass(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        config = self.client.get("/api/config").json()
        self.assertTrue(config["authRequired"])
        self.assertEqual(config["mode"], "local")
        self.assertEqual(self.client.get("/api/session").json(), {"authenticated": False})
        routes = [
            ("GET", "/api/snapshot"),
            ("GET", "/api/snapshot?local=true&authenticated=true"),
            ("POST", "/api/scenario/control"),
            ("POST", "/api/agents/run"),
            ("GET", "/api/agents/runs/forged"),
            ("POST", "/api/interventions"),
            ("POST", "/api/vision/analyze"),
            ("GET", "/api/media/camera-denim/baseline.mp4"),
            ("HEAD", "/api/media/camera-denim/baseline.mp4"),
            ("GET", "/api/vision/frames/camera-denim.jpg"),
            ("GET", "/api/config/../snapshot"),
        ]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path, json={},
                    headers={"Origin": ORIGIN, "Authorization": "Bearer forged",
                             "X-Forwarded-User": "admin", "X-Local-Mode": "true"})
                self.assertEqual(response.status_code, 401, response.text)
                if method == "HEAD":
                    self.assertEqual(response.content, b"")
                else:
                    self.assertEqual(response.json()["error"], "unauthenticated")
        self.assertEqual(self.media.calls, [])
        self.assertEqual(self.foundry.calls, [])

    def test_origin_required_for_login_logout_and_every_write(self):
        self.login()
        routes = [
            ("POST", "/api/session", {"password": PASSWORD}),
            ("DELETE", "/api/session", None),
            ("POST", "/api/scenario/control", {"action": "reset"}),
            ("POST", "/api/agents/run", {"agent": "experiment", "question": "Question"}),
            ("POST", "/api/interventions", {}),
            ("POST", "/api/vision/analyze", None),
        ]
        for method, path, body in routes:
            for origin in (None, "null", "http://testserver", ORIGIN + "/",
                           "https://testserver.attacker.invalid"):
                with self.subTest(method=method, path=path, origin=origin):
                    response = self.client.request(
                        method, path, json=body,
                        headers={} if origin is None else {"Origin": origin})
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["error"], "origin_forbidden")
        self.assertTrue(self.client.get("/api/session").json()["authenticated"])
        response = self.write("/api/session", method="DELETE")
        self.assertEqual(response.json(), {"authenticated": False})
        self.assertEqual(self.client.get("/api/snapshot").status_code, 401)

    def test_secure_cookie_headers_expiry_and_tamper(self):
        response = self.login()
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/",
                     f"Max-Age={SESSION_SECONDS}", COOKIE):
            self.assertIn(flag, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("max-age=", response.headers["strict-transport-security"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(self.client.get("/api/snapshot").status_code, 200)
        token = self.client.cookies.get(COOKIE)
        self.client.cookies.clear()
        tampered = self.client.get("/api/snapshot", headers={"Cookie": f"{COOKIE}={token}x"})
        self.assertEqual(tampered.status_code, 401)
        self.clock.advance(SESSION_SECONDS)
        expired = self.client.get("/api/session", headers={"Cookie": f"{COOKIE}={token}"})
        self.assertEqual(expired.json(), {"authenticated": False})
        self.assertEqual(self.client.get("/api/snapshot",
                         headers={"Cookie": f"{COOKIE}={token}"}).status_code, 401)

    def test_login_errors_rate_limit_and_validation_do_not_echo_password(self):
        invalid = self.write("/api/session", {"password": PASSWORD, "unexpected": PASSWORD})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(PASSWORD, invalid.text)
        for _ in range(8):
            response = self.write("/api/session", {"password": "wrong"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("wrong", response.text)
        self.assertEqual(self.write("/api/session", {"password": PASSWORD}).status_code, 429)
        self.clock.advance(60)
        self.login()

    def test_control_strict_speed_validation_and_clock_progression(self):
        self.login()
        response = self.write("/api/scenario/control", {"action": "resume", "speed": 1})
        self.assertEqual(response.status_code, 200)
        elapsed = response.json()["scenario"]["elapsedSeconds"]
        for speed in (1, 2, 5):
            response = self.write("/api/scenario/control", {"action": "resume", "speed": speed})
            self.assertEqual(response.status_code, 200)
            self.clock.advance(10)
            paused = self.write("/api/scenario/control", {"action": "pause"}).json()
            elapsed += speed * 10
            self.assertEqual(paused["scenario"]["elapsedSeconds"], elapsed)
            self.clock.advance(10)
            self.assertEqual(self.client.get("/api/snapshot").json()["scenario"]["elapsedSeconds"],
                             elapsed)
        for speed in (0, 3, -1, True, 1.0, "2"):
            with self.subTest(speed=speed):
                response = self.write("/api/scenario/control", {"action": "resume", "speed": speed})
                self.assertEqual(response.status_code, 422, response.text)
        for body in ({"action": "delete"}, {"action": "reset", "epochId": "forged"}):
            self.assertEqual(self.write("/api/scenario/control", body).status_code, 422)

    def test_unavailable_services_remain_truthful_without_synthetic_fallbacks(self):
        self.login()
        snap = self.client.get("/api/snapshot").json()
        self.assertEqual(snap["services"]["foundry"], "unavailable")
        self.assertEqual(snap["services"]["vision"], "unavailable")
        self.assertFalse(snap["services"]["footageReady"])
        for camera in snap["cameras"]:
            self.assertIsNone(camera["vision"]["people"])
            self.assertEqual(camera["vision"]["detections"], [])
        self.assertEqual(self.write("/api/agents/run",
                         {"agent": "experiment", "question": "Question"}).status_code, 503)
        self.assertEqual(self.write("/api/vision/analyze").status_code, 503)
        self.assertIsNone(self.read_document("vision-request"))
        self.assertEqual(self.read_document("state")["agentRuns"], [])

    def test_request_body_limit_and_unknown_routes(self):
        self.login()
        response = self.write("/api/agents/run",
                              {"agent": "experiment", "question": "x" * 17000})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"], "request_too_large")
        response = self.client.get("/api/not-an-endpoint")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not_found")
        self.assertEqual(self.client.get("/api/agents/runs/unknown").status_code, 404)

    def test_api_agent_approval_reset_round_trip(self):
        self.foundry.available = True
        self.login()
        response = self.write("/api/agents/run",
                              {"agent": "merchandising", "question": "Assess layout."})
        self.assertEqual(response.status_code, 202, response.text)
        queued = response.json()
        self.assertEqual(queued["status"], "queued")
        self.client.portal.call(finish_runs, self.app.state.engine)
        run_response = self.client.get(f"/api/agents/runs/{queued['id']}")
        self.assertEqual(run_response.status_code, 200)
        run = run_response.json()
        self.assertEqual(run["status"], "completed")
        self.assertFalse(any(key.startswith("_") for key in run))
        rec = run["recommendations"][0]
        body = {"runId": run["id"], "recommendationId": rec["id"], "actionId": rec["actionId"]}
        for change in ({"runId": "forged"}, {"recommendationId": "forged"},
                       {"actionId": "replenish-essentials"}):
            self.assertEqual(self.write("/api/interventions", {**body, **change}).status_code, 409)
        approved = self.write("/api/interventions", body)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(self.write("/api/interventions", body).json(), approved.json())
        snap = self.client.get("/api/snapshot").json()
        self.assertEqual(len(snap["interventions"]), 1)
        old_epoch = snap["scenario"]["id"]
        reset = self.write("/api/scenario/control", {"action": "reset"}).json()
        self.assertNotEqual(reset["scenario"]["id"], old_epoch)
        self.assertEqual(reset["agentRuns"], [])
        self.assertEqual(reset["interventions"], [])
        self.assertEqual(self.client.get(f"/api/agents/runs/{run['id']}").status_code, 404)
        self.assertEqual(self.write("/api/interventions", body).status_code, 409)

    def test_analysis_writes_current_epoch_request_marker_without_fake_observation(self):
        self.media.ready = True
        self.login()
        response = self.write("/api/vision/analyze")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"requested": True})
        marker = self.read_document("vision-request")
        self.assertEqual(marker["epochId"], self.read_document("state")["epochId"])
        self.assertEqual(marker["requestedAt"], iso(self.clock()))
        for camera in CAMERAS:
            self.assertIsNone(self.read_document(camera, "vision"))
        self.assertEqual(self.write("/api/vision/analyze").status_code, 429)
        self.assertEqual(self.read_document("vision-request"), marker)
        self.clock.advance(10)
        self.assertEqual(self.write("/api/vision/analyze").status_code, 202)
        self.assertEqual(self.read_document("vision-request")["requestedAt"], iso(self.clock()))
        new_epoch = self.write("/api/scenario/control", {"action": "reset"}).json()["scenario"]["id"]
        self.clock.advance(10)
        self.assertEqual(self.write("/api/vision/analyze").status_code, 202)
        self.assertEqual(self.read_document("vision-request")["epochId"], new_epoch)

    def test_worker_observations_validated_for_epoch_layout_time_and_camera(self):
        self.login()
        valid = ready_observation()
        self.put_observation(valid)
        observation = self.client.get("/api/snapshot").json()["cameras"][0]["vision"]
        self.assertEqual(observation, valid)
        invalid_cases = [
            ({**valid, "people": 2}, {}),
            ({**valid, "cameraId": CAMERAS[1]}, {}),
            ({**valid, "observedAt": iso(START - 1)}, {}),
            ({**valid, "observedAt": iso(START + 1)}, {}),
            ({**valid, "observedAt": "not a timestamp"}, {}),
            ({**valid, "frameUri": "https://attacker.invalid/frame.jpg"}, {}),
            (valid, {"epochId": "stale-epoch"}),
            (valid, {"layoutVersion": 999}),
            ({**valid, "status": "unavailable"}, {}),
        ]
        for observation, metadata in invalid_cases:
            with self.subTest(observation=observation, metadata=metadata):
                self.put_observation(observation, **metadata)
                value = self.client.get("/api/snapshot").json()["cameras"][0]["vision"]
                self.assertEqual(value["status"], "unavailable")
                self.assertIsNone(value["people"])
                self.assertEqual(value["detections"], [])
                self.assertIsNone(value["frameUri"])

    def test_stale_worker_observation_and_error_details_are_not_exposed(self):
        self.login()
        self.put_observation(ready_observation())
        self.clock.advance(121)
        value = self.client.get("/api/snapshot").json()["cameras"][0]["vision"]
        self.assertIsNone(value["people"])
        unavailable = unavailable_vision(CAMERAS[0])
        unavailable["error"] = "private worker endpoint and credentials"
        self.put_observation(unavailable)
        response = self.client.get("/api/snapshot")
        self.assertNotIn("private worker", response.text)
        self.assertIsNone(response.json()["cameras"][0]["vision"]["people"])

    def test_real_zero_people_is_distinct_from_unavailable_inference(self):
        self.login()
        zero = {**ready_observation(), "people": 0, "detections": []}
        self.put_observation(zero)
        value = self.client.get("/api/snapshot").json()["cameras"][0]["vision"]
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["people"], 0)
        self.assertIsNotNone(value["frameUri"])

    def test_media_ranges_head_and_path_allowlist(self):
        self.media.ready = True
        self.login()
        url = "/api/media/camera-denim/baseline.mp4"
        full = self.client.get(url)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"0123456789")
        ranged = self.client.get(url, headers={"Range": "bytes=2-5"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(ranged.headers["content-range"], "bytes 2-5/10")
        head = self.client.head(url, headers={"Range": "bytes=-3"})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["content-length"], "3")
        bad_range = self.client.get(url, headers={"Range": "bytes=10-"})
        self.assertEqual(bad_range.status_code, 416)
        self.assertEqual(bad_range.headers["content-range"], "bytes */10")
        for path in ("/api/media/not-camera/baseline.mp4",
                     "/api/media/camera-denim/other.mp4",
                     "/api/vision/frames/not-camera.jpg"):
            with self.subTest(path=path):
                count = len(self.media.calls)
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertEqual(len(self.media.calls), count)
        self.assertEqual(self.client.get("/api/vision/frames/camera-denim.jpg").status_code, 400)
        self.assertEqual(self.client.get("/api/vision/frames/camera-denim.jpg?observation=frame-1").status_code, 200)
        self.assertEqual(self.media.calls[-1], "frames/camera-denim.jpg")


class DependencyFailureTests(unittest.TestCase):
    def test_dependency_failure_returns_503_and_does_not_construct_fallback_repository(self):
        repo = LocalRepository(":memory:")
        repo.initialize = AsyncMock(side_effect=RuntimeError("private connection detail"))
        cfg = settings(environment="azure", local_mode=False,
                       cosmos_endpoint="https://example.documents.azure.com",
                       azure_storage_account="testaccount",
                       foundry_project_endpoint="https://example.invalid/project")
        foundry, media = FakeFoundry(), FakeMedia()
        with patch("retail.app.create_repository") as factory:
            with self.assertLogs("retail", level="ERROR"):
                with TestClient(create_app(cfg, repo, foundry, media, now=Clock(), ticker=False),
                                base_url=ORIGIN) as client:
                    self.assertEqual(client.get("/health").status_code, 503)
                    self.assertEqual(client.post("/api/session", json={"password": PASSWORD},
                                     headers={"Origin": ORIGIN}).status_code, 200)
                    response = client.get("/api/snapshot")
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn("private connection detail", response.text)
            factory.assert_not_called()
        self.assertTrue(foundry.closed)
        self.assertTrue(media.closed)

    def test_azure_health_requires_real_dependency_readiness(self):
        cfg = settings(environment="azure", local_mode=False,
                       cosmos_endpoint="https://example.documents.azure.com",
                       azure_storage_account="testaccount",
                       foundry_project_endpoint="https://example.invalid/project")
        foundry, media = FakeFoundry(), FakeMedia()
        with TestClient(create_app(cfg, LocalRepository(":memory:"), foundry, media,
                                  now=Clock(), ticker=False), base_url=ORIGIN) as client:
            self.assertEqual(client.get("/health").status_code, 503)
            foundry.available = True
            self.assertEqual(client.get("/health").status_code, 503)
            media.ready = True
            self.assertEqual(client.get("/health").status_code, 200)


class ObservationSchemaTests(unittest.TestCase):
    def test_ready_observation_accepts_inference_not_simulator_ground_truth(self):
        valid = ready_observation()
        self.assertEqual(VisionObservation.model_validate(valid).model_dump(), valid)
        with self.assertRaises(ValidationError):
            VisionObservation.model_validate({**valid, "simulatorLocations": [[0.5, 0.5]]})
        with self.assertRaises(ValidationError):
            VisionObservation.model_validate({**valid, "provenance": "simulation"})

    def test_detection_bounds_order_finiteness_label_and_confidence(self):
        for changes in (
            {"box": [-0.1, 0.0, 0.5, 0.5]}, {"box": [0.0, 0.0, 1.1, 0.5]},
            {"box": [0.5, 0.0, 0.1, 0.5]}, {"box": [0.1, 0.2, 0.1, 0.8]},
            {"box": [0.1, 0.8, 0.5, 0.2]}, {"box": [0.1, 0.2, 0.5]},
            {"box": [0.1, 0.2, float("nan"), 0.8]},
            {"score": float("inf")}, {"score": -0.1}, {"score": 1.1},
            {"label": "face"}, {"score": "0.9"},
        ):
            with self.subTest(changes=changes):
                value = ready_observation()
                value["detections"][0].update(changes)
                with self.assertRaises(ValidationError):
                    VisionObservation.model_validate(value)

    def test_observation_count_frame_and_timestamp_coherence(self):
        for changes in (
            {"people": True}, {"people": -1}, {"people": 2}, {"people": None},
            {"sourceSeconds": 60.0}, {"sourceSeconds": -1.0},
            {"sourceSeconds": float("nan")}, {"latencyMs": float("inf")},
            {"observedAt": "2026-10-01T12:00:00"}, {"observedAt": "invalid"},
            {"frameUri": "/api/vision/frames/camera-checkout.jpg"},
            {"error": "inference failed"}, {"cameraId": "arbitrary"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                VisionObservation.model_validate({**ready_observation(), **changes})

    def test_unavailable_never_has_fake_zero_count_detections_or_frame(self):
        unavailable = unavailable_vision(CAMERAS[0])
        VisionObservation.model_validate(unavailable)
        for changes in ({"people": 0}, {"detections": ready_observation()["detections"]},
                        {"frameUri": ready_observation()["frameUri"]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                VisionObservation.model_validate({**unavailable, **changes})


class ByteRangeTests(unittest.TestCase):
    def test_full_open_closed_suffix_clamped_and_empty_ranges(self):
        for value, size, expected in (
            (None, 10, (0, 10, False)), ("", 10, (0, 10, False)),
            ("bytes=0-0", 10, (0, 1, True)), ("bytes=2-5", 10, (2, 4, True)),
            ("bytes=7-", 10, (7, 3, True)), ("bytes=7-999", 10, (7, 3, True)),
            ("bytes=-3", 10, (7, 3, True)), ("bytes=-999", 10, (0, 10, True)),
            (None, 0, (0, 0, False)),
        ):
            with self.subTest(value=value, size=size):
                self.assertEqual(byte_range(value, size), expected)

    def test_unsatisfiable_or_multiple_ranges_return_416_with_total(self):
        for value, size in (
            ("bytes=-0", 10), ("bytes=10-", 10), ("bytes=5-2", 10),
            ("bytes=-", 10), ("bytes=0-1,3-4", 10), ("items=0-1", 10),
            ("bytes=abc-3", 10), ("bytes=0-1\n", 10), ("bytes=0-", 0),
        ):
            with self.subTest(value=value, size=size):
                with self.assertRaises(HTTPException) as raised:
                    byte_range(value, size)
                self.assertEqual(raised.exception.status_code, 416)
                self.assertEqual(raised.exception.headers["Content-Range"], f"bytes */{size}")


class BlobStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Only protocol types are stubbed; no Azure client or credential is constructed.
        core = ModuleType("azure.core")
        core.MatchConditions = SimpleNamespace(IfNotModified="if-not-modified")
        errors = ModuleType("azure.core.exceptions")
        errors.ResourceNotFoundError = type("ResourceNotFoundError", (Exception,), {})
        errors.HttpResponseError = type("HttpResponseError", (Exception,), {})
        self.enterContext(patch.dict(sys.modules, {
            "azure.core": core, "azure.core.exceptions": errors,
        }))
        self.errors = errors
        self.media = BlobMedia(settings())
        self.media.ready = True
        self.blob = SimpleNamespace(
            get_blob_properties=AsyncMock(return_value=SimpleNamespace(size=10, etag='"test-etag"')),
            download_blob=AsyncMock(),
        )
        self.media.container = SimpleNamespace(get_blob_client=Mock(return_value=self.blob))

    def request(self, method="GET", range_value=None, observation=None):
        headers = [] if range_value is None else [(b"range", range_value.encode())]
        query = b"" if observation is None else f"observation={observation}".encode()
        return Request({"type": "http", "method": method, "path": "/", "headers": headers, "query_string": query})

    async def test_image_bytes_are_bound_to_observation_metadata_and_etag(self):
        self.blob.get_blob_properties.return_value = SimpleNamespace(
            size=10, etag='"frame-etag"', metadata={"observationid": "frame-1"})
        async def chunks():
            yield b"0123456789"
        self.blob.download_blob.return_value = SimpleNamespace(chunks=chunks)
        response = await self.media.stream(
            "frames/camera-denim.jpg", self.request(observation="frame-1"), "image/jpeg")
        self.assertEqual(response.headers["x-vitrine-observation-id"], "frame-1")
        self.assertEqual(b"".join([chunk async for chunk in response.body_iterator]), b"0123456789")
        self.blob.download_blob.assert_awaited_once_with(
            offset=0, length=10, etag='"frame-etag"', match_condition="if-not-modified", max_concurrency=1)

    async def test_newer_or_unidentified_frame_never_backs_old_detection_boxes(self):
        for metadata in ({}, {"observationid": "frame-2"}):
            self.blob.get_blob_properties.return_value = SimpleNamespace(
                size=10, etag='"new-frame"', metadata=metadata)
            with self.assertRaises(HTTPException) as raised:
                await self.media.stream(
                    "frames/camera-denim.jpg", self.request(observation="frame-1"), "image/jpeg")
            self.assertEqual(raised.exception.status_code, 409)
        self.blob.download_blob.assert_not_awaited()

    async def test_image_overwrite_between_metadata_and_download_is_a_conflict(self):
        self.blob.get_blob_properties.return_value = SimpleNamespace(
            size=10, etag='"old-frame"', metadata={"observationid": "frame-1"})
        conflict = self.errors.HttpResponseError("private storage details")
        conflict.status_code = 412
        self.blob.download_blob.side_effect = conflict
        with self.assertRaises(HTTPException) as raised:
            await self.media.stream(
                "frames/camera-denim.jpg", self.request(observation="frame-1"), "image/jpeg")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "observation_frame_changed")

    async def test_range_download_is_bounded_and_conditioned_on_blob_etag(self):
        async def chunks():
            yield b"23"
            yield b"45"
        self.blob.download_blob.return_value = SimpleNamespace(chunks=chunks)
        response = await self.media.stream("media/test.mp4", self.request(range_value="bytes=2-5"),
                                           "video/mp4")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(b"".join([chunk async for chunk in response.body_iterator]), b"2345")
        self.blob.download_blob.assert_awaited_once_with(
            offset=2, length=4, etag='"test-etag"', match_condition="if-not-modified",
            max_concurrency=1)

    async def test_head_and_empty_blob_do_not_download_content(self):
        response = await self.media.stream(
            "media/test.mp4", self.request("HEAD", "bytes=-3"), "video/mp4")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-length"], "3")
        self.assertEqual(response.body, b"")
        self.blob.download_blob.assert_not_awaited()
        self.blob.get_blob_properties.return_value = SimpleNamespace(size=0, etag='"empty"')
        response = await self.media.stream("media/test.mp4", self.request(), "video/mp4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-length"], "0")
        self.blob.download_blob.assert_not_awaited()

    async def test_blob_errors_are_translated_without_leaking_details(self):
        for error, status in ((self.errors.ResourceNotFoundError, 404),
                              (self.errors.HttpResponseError, 503)):
            with self.subTest(error=error):
                self.blob.get_blob_properties.side_effect = error("private endpoint detail")
                with self.assertRaises(HTTPException) as raised:
                    await self.media.stream("media/test.mp4", self.request(), "video/mp4")
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("private", raised.exception.detail)
        self.blob.download_blob.assert_not_awaited()

    async def test_unready_media_does_not_access_blob(self):
        self.media.ready = False
        with self.assertRaises(HTTPException) as raised:
            await self.media.stream("media/test.mp4", self.request(), "video/mp4")
        self.assertEqual(raised.exception.status_code, 503)
        self.media.container.get_blob_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
