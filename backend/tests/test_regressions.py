import asyncio
from copy import deepcopy
import json
import unittest

from retail.engine import Engine, ObservationBatch
from retail.app import create_app
from retail.simulation import experiment, iso, seed, step
from retail.storage import LocalRepository
from test_backend import (Clock, FakeFoundry, FakeMedia, START, finish_runs,
                          ready_observation, settings)


class ComparisonRegressions(unittest.TestCase):
    def test_control_is_net_and_matches_untreated_store_exactly(self):
        state = seed(START)
        for second in range(5, 7201, 5):
            step(state, second, START + second)
            self.assertEqual(state["controlRevenue"], state["metrics"]["revenueCents"])
        self.assertTrue(any(event["type"] == "return" for event in state["posEvents"]))

    def test_completed_comparison_survives_history_eviction(self):
        state = seed(START)
        for second in range(5, 61, 5):
            step(state, second, START + second)
        state["interventions"] = [{"actionId": "reposition-denim", "elapsedSeconds": 60,
                                   "at": iso(START + 60)}]
        for second in range(65, 121, 5):
            step(state, second, START + second)
        completed = experiment(state)
        self.assertEqual(completed["status"], "simulated-comparison")
        for second in range(125, 5001, 5):
            step(state, second, START + second)
        self.assertGreater(state["samples"][0]["second"], 120)
        self.assertEqual(experiment(state), completed)
        self.assertEqual(experiment(json.loads(json.dumps(state))), completed)


class EngineRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = LocalRepository(":memory:")
        await self.repo.initialize()
        self.clock = Clock()
        self.foundry = FakeFoundry(available=True)
        self.engine = Engine(self.repo, self.foundry, FakeMedia(), self.clock)
        await self.engine.initialize()

    async def asyncTearDown(self):
        await self.engine.close()
        await self.repo.close()

    async def test_old_observation_batch_cannot_cross_reset_snapshot_or_agent(self):
        batch = ObservationBatch({"camera-denim": ready_observation()},
                                 self.engine.state["epochId"], self.engine.state["layoutVersion"])
        await self.engine.control("reset", None)
        snap = await self.engine.view(batch)
        self.assertIsNone(snap["cameras"][0]["vision"]["people"])
        await self.engine.run_agent("merchandising", "Contexto atual", batch)
        await finish_runs(self.engine)
        context = self.foundry.calls[-1][2]
        self.assertEqual(context["epochId"], self.engine.state["epochId"])
        self.assertEqual(context["observations"], [])
        self.assertFalse(any(e["kind"] == "model-inference" for e in context["evidence"]))

    async def test_old_layout_batch_is_not_current_evidence(self):
        batch = ObservationBatch({"camera-denim": ready_observation()},
                                 self.engine.state["epochId"], self.engine.state["layoutVersion"])
        async with self.engine.transaction() as state:
            state["layoutVersion"] += 1
        snap = await self.engine.view(batch)
        self.assertEqual(snap["cameras"][0]["vision"]["status"], "unavailable")

    async def test_reset_preserves_vision_budget_and_execution_audit(self):
        self.foundry.blocked = True
        run = await self.engine.run_agent("merchandising", "Contexto", {})
        await asyncio.wait_for(self.foundry.started.wait(), 1)
        async with self.engine.transaction() as state:
            state["lastVisionRequest"] = self.clock()
        before = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(before["run"]["responseId"], "response-test")
        await self.engine.control("reset", None)
        self.assertEqual(self.engine.state["lastVisionRequest"], self.clock())
        self.foundry.release.set()
        await finish_runs(self.engine)
        after = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(after["run"]["responseId"], "response-test")
        self.assertEqual(after["run"]["conversationId"], "conversation-test")
        self.assertEqual(after["run"]["status"], "failed")
        self.assertEqual(after["run"]["recommendations"], [])
        self.assertEqual(self.engine.state["agentRuns"], [])

    async def test_archive_retention_is_bounded_across_reset(self):
        async with self.engine.transaction() as state:
            state["runHistory"] = [f"old-{i}" for i in range(100)]
        await self.repo.write_document({"id": "old-0", "pk": "agent-runs", "run": {}})
        await self.engine.run_agent("merchandising", "Contexto", {})
        await finish_runs(self.engine)
        self.assertEqual(len(self.engine.state["runHistory"]), 100)
        self.assertIsNone(await self.repo.read_document("old-0", "agent-runs"))
        await self.engine.control("reset", None)
        self.assertEqual(len(self.engine.state["runHistory"]), 100)

    async def test_background_ticker_publishes_clock_without_snapshot_poll(self):
        await self.engine.view()
        self.engine.start()
        self.clock.advance(5)
        await asyncio.sleep(2.1)
        doc = await self.repo.read_document("clock", "store-demo")
        self.assertEqual(doc["elapsedSeconds"], 5)
        self.assertEqual(doc["epochId"], self.engine.state["epochId"])
        self.assertEqual(doc["variant"], "baseline")

    async def test_restart_marks_interrupted_runs_failed_and_preserves_receipts(self):
        original = self.engine
        async with original.transaction() as state:
            state["agentRuns"] = [{
                "id": "interrupted", "status": "running", "responseId": "real-response",
                "toolCalls": ["real-tool-call"], "_epochId": state["epochId"]}]
        await original.close()
        restored = Engine(self.repo, self.foundry, FakeMedia(), self.clock)
        await restored.initialize()
        self.engine = restored
        self.assertEqual(restored.state["agentRuns"][0]["status"], "failed")
        archived = await self.repo.read_document("interrupted", "agent-runs")
        self.assertIsNotNone(archived)
        self.assertEqual(archived["run"]["responseId"], "real-response")
        self.assertEqual(archived["run"]["status"], "failed")

    async def test_lost_write_ack_reconciles_etag_without_duplicate_events(self):
        await self.engine.view()
        self.clock.advance(15)
        original = self.repo.compare_write
        fail = True

        async def ambiguous_write(doc, etag, **batch):
            nonlocal fail
            result = await original(doc, etag, **batch)
            if fail:
                fail = False
                raise TimeoutError("Committed but acknowledgment lost")
            return result
        self.repo.compare_write = ambiguous_write
        with self.assertRaises(TimeoutError):
            await self.engine.view()
        persisted = await self.repo.read_document("state", "store-demo")
        self.assertEqual(self.engine.etag, persisted["_etag"])
        ids = [p["id"] for p in persisted["posEvents"]]
        await self.engine.view()
        self.assertTrue(self.engine.ready)
        self.assertEqual([p["id"] for p in self.engine.state["posEvents"]], ids)

    async def test_lost_queue_ack_does_not_permanently_block_agent(self):
        original = self.repo.compare_write
        fail = True

        async def ambiguous_write(doc, etag, **batch):
            nonlocal fail
            result = await original(doc, etag, **batch)
            if fail:
                fail = False
                raise TimeoutError("Committed queued reservation")
            return result
        self.repo.compare_write = ambiguous_write
        with self.assertRaises(TimeoutError):
            await self.engine.run_agent("merchandising", "Primeira tentativa", {})
        self.assertEqual(len(self.engine.tasks), 0)
        run = await self.engine.run_agent("merchandising", "Nova tentativa", {})
        await finish_runs(self.engine)
        self.assertEqual(self.engine.state["agentRuns"][0]["status"], "failed")
        self.assertEqual(self.engine.state["agentRuns"][-1]["id"], run["id"])
        self.assertEqual(self.engine.state["agentRuns"][-1]["status"], "completed")
        self.assertEqual(len(self.foundry.calls), 1)

    async def test_state_clock_batch_failure_does_not_strand_job(self):
        original = self.repo.compare_write
        fail = True

        async def intermittent_batch(doc, etag, **batch):
            nonlocal fail
            if doc["id"] == "state" and fail:
                fail = False
                self.assertEqual(batch["upserts"][0]["id"], "clock")
                raise TimeoutError("State/clock batch temporarily unavailable")
            return await original(doc, etag, **batch)
        self.repo.compare_write = intermittent_batch
        before = await self.repo.read_document("clock", "store-demo")
        with self.assertRaises(TimeoutError):
            await self.engine.run_agent("merchandising", "Contexto", {})
        self.assertFalse(self.engine.tasks)
        self.assertEqual(self.engine.state["agentRuns"], [])
        self.assertEqual(await self.repo.read_document("clock", "store-demo"), before)
        run = await self.engine.run_agent("merchandising", "Contexto", {})
        await finish_runs(self.engine)
        self.assertEqual(self.engine.state["agentRuns"][-1]["id"], run["id"])
        self.assertEqual(self.engine.state["agentRuns"][-1]["status"], "completed")
        self.assertTrue(self.engine.ready)

    async def test_startup_retries_after_an_existing_writer_lease_expires(self):
        # Use a distinct repository so the fixture's Engine remains untouched.
        repo = LocalRepository(":memory:")
        await repo.initialize()
        state = seed(self.clock())
        state["owner"], state["leaseUntil"] = "old-process", self.clock() + 30
        await repo.compare_write(state, None)
        app = create_app(settings(), repo, FakeFoundry(), FakeMedia(), self.clock, ticker=False)
        async with app.router.lifespan_context(app):
            self.assertFalse(app.state.initialized)
            self.clock.advance(31)
            await asyncio.sleep(5.1)
            self.assertTrue(app.state.initialized)
            self.assertTrue(app.state.engine.ready)
            self.assertNotEqual(app.state.engine.state["owner"], "old-process")


if __name__ == "__main__":
    unittest.main()
