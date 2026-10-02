import asyncio
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from retail.engine import Engine, PK
from retail.simulation import clock_document, iso, seed
from retail.storage import Conflict, LocalRepository
from test_backend import Clock, FakeFoundry, FakeMedia, finish_runs


class AtomicEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = LocalRepository(":memory:")
        await self.repo.initialize()
        self.addAsyncCleanup(self.repo.close)
        self.clock = Clock()
        self.foundry = FakeFoundry(available=True)
        self.engine = self.new_engine()
        await self.engine.initialize()

    def new_engine(self):
        engine = Engine(self.repo, self.foundry, FakeMedia(), self.clock)
        self.addAsyncCleanup(engine.close)
        return engine

    async def assert_clock_matches(self, engine):
        state = await self.repo.read_document("state", PK)
        clock = await self.repo.read_document("clock", PK)
        self.assertEqual(state["owner"], engine.owner)
        self.assertEqual({k: v for k, v in clock.items() if k != "_etag"},
                         clock_document(state, state["lastWall"]))

    async def seed_retention_limit(self):
        history = [f"completed-{i}" for i in range(100)]
        for id in history:
            await self.repo.write_document({
                "id": id, "pk": "agent-runs",
                "run": {"id": id, "status": "completed", "responseId": f"receipt-{id}",
                        "toolCalls": ["read_demo_context"], "completedAt": iso(self.clock())},
            })
        async with self.engine.transaction() as state:
            state["runHistory"] = history
        return history

    def archives(self):
        return self.repo.db.execute(
            "SELECT id,body,version FROM documents WHERE pk='agent-runs' "
            "AND id!='archive-fence' ORDER BY id").fetchall()

    async def test_delayed_old_batch_cannot_overwrite_new_owner_reset(self):
        original = self.repo.compare_write
        entered, resume = asyncio.Event(), asyncio.Event()

        async def delayed(doc, etag, **batch):
            if doc["id"] == "state" and doc["owner"] == self.engine.owner:
                entered.set()
                await resume.wait()
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", delayed):
            old_publication = asyncio.create_task(self.engine.control("pause", None))
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.advance(31)
            successor = self.new_engine()
            await successor.initialize()
            await successor.control("reset", 5)
            new_clock = await self.repo.read_document("clock", PK)
            new_state = await self.repo.read_document("state", PK)
            resume.set()
            with self.assertRaises(Conflict):
                await asyncio.wait_for(old_publication, 1)
        self.assertEqual(await self.repo.read_document("clock", PK), new_clock)
        self.assertEqual(await self.repo.read_document("state", PK), new_state)
        await self.assert_clock_matches(successor)

    async def test_delayed_old_ack_has_no_second_unfenced_clock_write(self):
        original = self.repo.compare_write
        entered, resume = asyncio.Event(), asyncio.Event()

        async def delayed_ack(doc, etag, **batch):
            result = await original(doc, etag, **batch)
            if doc["id"] == "state" and doc["owner"] == self.engine.owner:
                entered.set()
                await resume.wait()
            return result

        with patch.object(self.repo, "compare_write", delayed_ack):
            old_publication = asyncio.create_task(self.engine.view())
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.advance(31)
            successor = self.new_engine()
            await successor.initialize()
            await successor.control("reset", None)
            new_clock = await self.repo.read_document("clock", PK)
            resume.set()
            await asyncio.wait_for(old_publication, 1)
        self.assertEqual(await self.repo.read_document("clock", PK), new_clock)
        with self.assertRaises(Conflict):
            await self.engine.view()
        await self.assert_clock_matches(successor)

    async def test_delayed_startup_creation_loses_atomically_to_new_epoch(self):
        repo = LocalRepository(":memory:")
        await repo.initialize()
        self.addAsyncCleanup(repo.close)
        first = Engine(repo, self.foundry, FakeMedia(), self.clock)
        second = Engine(repo, self.foundry, FakeMedia(), self.clock)
        self.addAsyncCleanup(first.close)
        self.addAsyncCleanup(second.close)
        original = repo.compare_write
        entered, resume = asyncio.Event(), asyncio.Event()

        async def delayed_create(doc, etag, **batch):
            if doc["id"] == "state" and doc["owner"] == first.owner:
                entered.set()
                await resume.wait()
            return await original(doc, etag, **batch)

        with patch.object(repo, "compare_write", delayed_create):
            starting = asyncio.create_task(first.initialize())
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.advance(31)
            await second.initialize()
            await second.control("reset", None)
            current = await repo.read_document("clock", PK)
            resume.set()
            with self.assertRaises(Conflict):
                await asyncio.wait_for(starting, 1)
        self.assertEqual(await repo.read_document("clock", PK), current)

    async def test_every_publication_path_uses_atomic_state_clock_batch(self):
        original = self.repo.compare_write
        calls = []

        async def checked(doc, etag, **batch):
            if doc["id"] == "state":
                self.assertEqual(len(batch["upserts"]), 1)
                self.assertEqual(batch["upserts"][0]["id"], "clock")
                self.assertEqual(batch["upserts"][0]["epochId"], doc["epochId"])
                calls.append(deepcopy(doc))
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", checked), patch.object(
                self.repo, "write_document", AsyncMock(side_effect=AssertionError("Unfenced write"))):
            await self.engine.close()
            engine = self.new_engine()
            await engine.initialize()
            await engine.control("pause", None)
            self.clock.advance(10)
            await engine.view()
            clock = await self.repo.read_document("clock", PK)
            self.assertEqual(clock["state"], "paused")
            self.assertEqual(clock["activeUntil"], iso(self.clock() + 120))
            async with engine.transaction(recover=True):
                pass
            await engine.control("reset", None)
            await engine.run_agent("merchandising", "Contexto", {})
            await finish_runs(engine)
            await engine.close()
        self.assertGreater(len(calls), 10)
        self.assertEqual(calls[-1]["leaseUntil"], 0)
        await self.assert_clock_matches(engine)

    async def test_reservation_cas_failure_at_100_preserves_every_completed_audit(self):
        await self.seed_retention_limit()
        archives = self.archives()
        before = await self.repo.read_document("state", PK)
        before_clock = await self.repo.read_document("clock", PK)
        with patch.object(self.repo, "compare_write", AsyncMock(side_effect=Conflict("lost CAS"))):
            with self.assertRaises(Conflict):
                await self.engine.run_agent("merchandising", "Must not execute", {})
        self.assertEqual(self.archives(), archives)
        self.assertEqual(len(self.archives()), 100)
        self.assertEqual(await self.repo.read_document("state", PK), before)
        self.assertEqual(await self.repo.read_document("clock", PK), before_clock)
        self.assertFalse(self.engine.tasks)
        self.assertFalse(self.foundry.calls)

    async def test_archive_outage_preserves_durable_intent_and_blocks_unbounded_reservations(self):
        history = await self.seed_retention_limit()
        before = self.archives()
        original = self.repo.compare_write

        async def unavailable(doc, etag, **batch):
            if doc["pk"] == "agent-runs":
                raise TimeoutError("Archive unavailable")
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", unavailable):
            with self.assertLogs("retail", level="WARNING"):
                run = await self.engine.run_agent("merchandising", "Contexto", {})
            await finish_runs(self.engine)
            saved = await self.repo.read_document("state", PK)
            self.assertEqual(saved["pendingArchives"][run["id"]]["status"], "queued")
            self.assertIsNone(saved["pendingArchives"][history[0]])
            self.assertEqual(len(saved["runHistory"]), 100)
            self.assertEqual(self.archives(), before)
            with self.assertRaises(TimeoutError):
                await self.engine.run_agent("merchandising", "Another", {})
            self.assertEqual(await self.repo.read_document("state", PK), saved)
        async with self.engine.transaction(recover=True):
            pass
        self.assertNotIn("pendingArchives", self.engine.state)
        self.assertEqual(len(self.archives()), 100)
        self.assertIsNone(await self.repo.read_document(history[0], "agent-runs"))
        archived = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(archived["run"]["status"], "failed")
        self.assertFalse(self.foundry.calls)

    async def test_applied_archive_with_lost_ack_is_replayed_once_on_restart(self):
        history = await self.seed_retention_limit()
        original = self.repo.compare_write
        applied = False
        failed = False

        async def lost_ack(doc, etag, **batch):
            nonlocal applied, failed
            if doc["id"] == "archive-fence":
                applied = True
            if applied and doc["id"] == "state" and "pendingArchives" not in doc and not failed:
                failed = True
                raise TimeoutError("Crash before outbox acknowledgment")
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", lost_ack):
            with self.assertLogs("retail", level="WARNING"):
                run = await self.engine.run_agent("merchandising", "Contexto", {})
            pending = (await self.repo.read_document("state", PK))["pendingArchives"]
            self.assertIn(run["id"], pending)
            self.assertEqual(len(self.archives()), 100)
            self.assertIsNone(await self.repo.read_document(history[0], "agent-runs"))
            for task in list(self.engine.tasks):
                task.cancel()
            await finish_runs_after_cancel(self.engine)
        self.clock.advance(31)
        successor = self.new_engine()
        with patch.object(self.repo, "compare_write", wraps=original) as writes:
            await successor.initialize()
        archive_batches = [call for call in writes.call_args_list
                           if call.args[0]["id"] == "archive-fence"]
        self.assertEqual(len(archive_batches), 1)  # recovery only, not the old queued batch
        self.assertEqual(len(self.archives()), 100)
        archived = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(archived["run"]["status"], "failed")
        self.assertNotIn("pendingArchives", successor.state)

    async def test_delayed_old_archive_batch_cannot_regress_reset_audit(self):
        original = self.repo.compare_write
        entered, resume = asyncio.Event(), asyncio.Event()
        delayed = False

        async def delayed_archive(doc, etag, **batch):
            nonlocal delayed
            if doc["id"] == "archive-fence" and not delayed:
                delayed = True
                entered.set()
                await resume.wait()
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", delayed_archive):
            reservation = asyncio.create_task(self.engine.run_agent("merchandising", "Contexto", {}))
            await asyncio.wait_for(entered.wait(), 1)
            self.clock.advance(31)
            successor = self.new_engine()
            await successor.initialize()
            await successor.control("reset", None)
            current = self.archives()
            current_clock = await self.repo.read_document("clock", PK)
            resume.set()
            with self.assertRaises(Conflict):
                await asyncio.wait_for(reservation, 1)
            await finish_runs(self.engine)
        self.assertEqual(self.archives(), current)
        self.assertEqual(await self.repo.read_document("clock", PK), current_clock)
        self.assertFalse(self.foundry.calls)

    async def lost_owner_during_running_archive_flush(self, timeout=False):
        original = self.repo.compare_write
        entered, resume = asyncio.Event(), asyncio.Event()
        delayed = False

        async def delayed_running(doc, etag, **batch):
            nonlocal delayed
            running = any(item.get("run", {}).get("status") == "running"
                          for item in batch.get("upserts", ()))
            if doc["id"] == "archive-fence" and running and not delayed:
                delayed = True
                entered.set()
                await resume.wait()
                if timeout:
                    raise TimeoutError("Ambiguous old-owner archive write")
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", delayed_running):
            run = await self.engine.run_agent("merchandising", "Contexto", {})
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(self.foundry.calls)
            self.clock.advance(31)
            successor = self.new_engine()
            await successor.initialize()
            await successor.control("reset", None)
            resume.set()
            if timeout:
                with self.assertLogs("retail", level="WARNING"):
                    await finish_runs(self.engine)
            else:
                await finish_runs(self.engine)
        self.assertFalse(self.foundry.calls)
        archived = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(archived["run"]["status"], "failed")
        self.assertEqual(successor.state["agentRuns"], [])

    async def test_lost_owner_during_running_archive_conflict_never_invokes_foundry(self):
        await self.lost_owner_during_running_archive_flush()

    async def test_lost_owner_during_running_archive_timeout_never_invokes_foundry(self):
        await self.lost_owner_during_running_archive_flush(timeout=True)

    async def test_running_audit_outage_fails_closed_even_without_takeover(self):
        original = self.repo.compare_write
        failed = False

        async def unavailable_running(doc, etag, **batch):
            nonlocal failed
            running = any(item.get("run", {}).get("status") == "running"
                          for item in batch.get("upserts", ()))
            if doc["id"] == "archive-fence" and running and not failed:
                failed = True
                raise TimeoutError("Running audit unavailable")
            return await original(doc, etag, **batch)

        with patch.object(self.repo, "compare_write", unavailable_running):
            run = await self.engine.run_agent("merchandising", "Contexto", {})
            with self.assertLogs("retail", level="WARNING"):
                await finish_runs(self.engine)
        self.assertTrue(failed)
        self.assertFalse(self.foundry.calls)
        archived = await self.repo.read_document(run["id"], "agent-runs")
        self.assertEqual(archived["run"]["status"], "failed")

    async def test_failed_reset_cannot_archive_uncommitted_status(self):
        run = await self.engine.run_agent("merchandising", "Contexto", {})
        await finish_runs(self.engine)
        before = await self.repo.read_document(run["id"], "agent-runs")
        with patch.object(self.repo, "compare_write", AsyncMock(side_effect=Conflict("lost CAS"))):
            with self.assertRaises(Conflict):
                await self.engine.control("reset", None)
        self.assertEqual(await self.repo.read_document(run["id"], "agent-runs"), before)


async def finish_runs_after_cancel(engine):
    await asyncio.gather(*list(engine.tasks), return_exceptions=True)
    await asyncio.sleep(0)


class DiskOutboxRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_audit_intent_survives_process_loss_and_database_reopen(self):
        path = Path(__file__).parent / f"outbox-{uuid4().hex}.sqlite"
        first = LocalRepository(str(path))
        second = LocalRepository(str(path))
        clock = Clock()
        engine = None
        try:
            await first.initialize()
            state = seed(clock())
            run = {"id": "completed-before-crash", "status": "completed",
                   "responseId": "real-receipt", "toolCalls": ["real-tool-call"]}
            state.update(owner="dead-process", leaseUntil=clock() + 30, revision=7,
                         runHistory=[run["id"]], agentRuns=[],
                         pendingArchives={run["id"]: run})
            await first.compare_write(state, None, upserts=[clock_document(state, clock())])
            await first.close()
            await second.initialize()
            clock.advance(31)
            engine = Engine(second, FakeFoundry(), FakeMedia(), clock)
            await engine.initialize()
            archived = await second.read_document(run["id"], "agent-runs")
            self.assertEqual(archived["run"], run)
            self.assertNotIn("pendingArchives", await second.read_document("state", PK))
        finally:
            if engine:
                await engine.close()
            await first.close()
            await second.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
