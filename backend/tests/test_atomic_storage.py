"""Offline atomic persistence contracts against SQLite and the installed Cosmos SDK."""

from copy import deepcopy
from dataclasses import replace
import json
import os
import sqlite3
import unittest
from unittest.mock import AsyncMock, create_autospec, patch

from azure.core import MatchConditions
from azure.cosmos import _base
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.aio._cosmos_client_connection_async import CosmosClientConnection
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError

from retail.config import Settings
from retail.storage import Conflict, CosmosRepository, LocalRepository, create_repository
from test_bootstrap_config import JOB_ENV


STATE = {"id": "state", "pk": "store", "revision": 1}
CLOCK = {"id": "clock", "pk": "store", "tick": 1}


def invalid_batches():
    return (
        ("partition", (dict(CLOCK, pk="other"),), (), "one partition"),
        ("primary-upsert", (STATE,), (), "distinct document IDs"),
        ("repeated-upsert", (CLOCK, dict(CLOCK, tick=2)), (), "distinct document IDs"),
        ("primary-delete", (), ("state",), "distinct document IDs"),
        ("repeated-delete", (), ("archive", "archive"), "distinct document IDs"),
        ("upsert-delete", (CLOCK,), ("clock",), "distinct document IDs"),
    )


class LocalAtomicStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = LocalRepository(":memory:")
        await self.repository.initialize()
        self.addAsyncCleanup(self.repository.close)

    def snapshot(self):
        return self.repository.db.execute(
            "SELECT pk,id,body,version FROM documents ORDER BY pk,id"
        ).fetchall()

    async def assert_document(self, body, etag):
        self.assertEqual(
            await self.repository.read_document(body["id"], body["pk"]),
            {**body, "_etag": etag},
        )

    async def test_two_argument_create_replace_and_conflicts_are_preserved(self):
        self.assertEqual(await self.repository.compare_write(STATE, None), "1")
        changed = dict(STATE, revision=2)
        self.assertEqual(await self.repository.compare_write(changed, "1"), "2")
        await self.assert_document(changed, "2")
        before = self.snapshot()
        for document, etag in ((STATE, None), (STATE, "1"), (dict(STATE, id="missing"), "1")):
            with self.subTest(document=document, etag=etag):
                with self.assertRaises(Conflict):
                    await self.repository.compare_write(document, etag)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.repository.db.in_transaction)

    async def test_state_and_clock_insert_replace_without_mutating_inputs(self):
        state = dict(STATE, _etag="ignored", _ts=123)
        clock = dict(CLOCK, _etag="ignored", _rid="ignored")
        original = deepcopy((state, clock))
        self.assertEqual(
            await self.repository.compare_write(state, None, upserts=(clock,)), "1"
        )
        await self.assert_document(STATE, "1")
        await self.assert_document(CLOCK, "1")
        self.assertEqual((state, clock), original)
        changed_state, changed_clock = dict(STATE, revision=2), dict(CLOCK, tick=2)
        self.assertEqual(
            await self.repository.compare_write(changed_state, "1", upserts=(changed_clock,)),
            "2",
        )
        await self.assert_document(changed_state, "2")
        await self.assert_document(changed_clock, "2")
        for _, _, body, _ in self.snapshot():
            self.assertFalse(any(key.startswith("_") for key in json.loads(body)))

    async def test_existing_state_can_insert_clock_and_new_state_can_replace_clock(self):
        for existing in (STATE, CLOCK):
            with self.subTest(existing=existing["id"]):
                with self.repository.db:
                    self.repository.db.execute("DELETE FROM documents")
                await self.repository.write_document(existing)
                state_etag = "1" if existing == STATE else None
                result = await self.repository.compare_write(STATE, state_etag, upserts=(CLOCK,))
                await self.assert_document(STATE, "2" if existing == STATE else "1")
                await self.assert_document(CLOCK, "2" if existing == CLOCK else "1")
                self.assertEqual(result, "2" if existing == STATE else "1")

    async def test_stale_etag_and_duplicate_create_leave_entire_batch_unchanged(self):
        await self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        archive = {"id": "archive", "pk": "store", "retained": True}
        await self.repository.write_document(archive)
        before = self.snapshot()
        for etag in ("0", None):
            with self.subTest(etag=etag):
                with self.assertRaises(Conflict):
                    await self.repository.compare_write(
                        dict(STATE, revision=99), etag,
                        upserts=(dict(CLOCK, tick=99), {"id": "new", "pk": "store"}),
                        deletes=("archive", "missing"),
                    )
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.repository.db.in_transaction)

    async def test_compare_write_has_no_suspension_inside_sqlite_transaction(self):
        statements = []
        connection = self.repository.db
        connection.set_trace_callback(statements.append)
        self.addCleanup(connection.set_trace_callback, None)
        operation = self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        try:
            # One send must finish: no await may yield between BEGIN and COMMIT.
            with self.assertRaises(StopIteration) as completed:
                operation.send(None)
            self.assertEqual(completed.exception.value, "1")
        finally:
            operation.close()
        self.assertIs(self.repository.db, connection)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(len(statements), 4)
        self.assertTrue(statements[0].startswith("BEGIN"))
        self.assertIn("'state'", statements[1])
        self.assertIn("'clock'", statements[2])
        self.assertEqual(statements[3], "COMMIT")
        await self.assert_document(STATE, "1")
        await self.assert_document(CLOCK, "1")

    async def assert_clock_trigger_rolls_back(self, existing):
        if existing:
            await self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        before = self.snapshot()
        connection = self.repository.db
        operation = "UPDATE" if existing else "INSERT"
        connection.execute(
            f"CREATE TRIGGER fail_clock AFTER {operation} ON documents "
            "WHEN NEW.id='clock' BEGIN "
            "SELECT RAISE(ABORT, 'clock second write failed'); END"
        )
        statements = []
        connection.set_trace_callback(statements.append)
        self.addCleanup(connection.set_trace_callback, None)
        with self.assertRaises(Conflict) as raised:
            await self.repository.compare_write(
                dict(STATE, revision=2), "1" if existing else None,
                upserts=(dict(CLOCK, tick=2),),
            )
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)
        self.assertIn("clock second write failed", str(raised.exception.__cause__))
        self.assertIs(self.repository.db, connection)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.snapshot(), before)
        state_write = next(i for i, sql in enumerate(statements) if "'state'" in sql)
        clock_write = next(i for i, sql in enumerate(statements) if "'clock'" in sql)
        self.assertLess(state_write, clock_write)
        self.assertIn("ROLLBACK", statements)
        self.assertNotIn("COMMIT", statements)
        connection.set_trace_callback(None)
        connection.execute("DROP TRIGGER fail_clock")
        result = await self.repository.compare_write(
            dict(STATE, revision=2), "1" if existing else None,
            upserts=(dict(CLOCK, tick=2),),
        )
        self.assertEqual(result, "2" if existing else "1")
        await self.assert_document(dict(CLOCK, tick=2), "2" if existing else "1")

    async def test_clock_insert_trigger_rolls_back_new_state(self):
        await self.assert_clock_trigger_rolls_back(existing=False)

    async def test_clock_update_trigger_rolls_back_replaced_state_and_versions(self):
        await self.assert_clock_trigger_rolls_back(existing=True)

    async def test_partition_and_duplicate_id_guards_do_not_write(self):
        await self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        before = self.snapshot()
        for name, upserts, deletes, error in invalid_batches():
            for etag in (None, "1"):
                with self.subTest(case=name, etag=etag):
                    with self.assertRaisesRegex(ValueError, error):
                        await self.repository.compare_write(
                            dict(STATE, revision=2), etag, upserts=upserts, deletes=deletes
                        )
                    self.assertEqual(self.snapshot(), before)
                    self.assertFalse(self.repository.db.in_transaction)

    async def test_upserts_and_deletions_are_atomic_partition_scoped_and_repeatable(self):
        await self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        other_partition = {"id": "archive", "pk": "other", "keep": True}
        survivor = {"id": "keep", "pk": "store", "keep": True}
        for doc in ({"id": "archive", "pk": "store"}, other_partition, survivor):
            await self.repository.write_document(doc)
        changed_state, changed_clock = dict(STATE, revision=2), dict(CLOCK, tick=2)
        inserted = {"id": "new", "pk": "store", "value": "inserted"}
        for etag, result in (("1", "2"), ("2", "3")):
            with self.subTest(etag=etag):
                self.assertEqual(
                    await self.repository.compare_write(
                        changed_state, etag, upserts=(changed_clock, inserted),
                        deletes=("archive", "missing"),
                    ),
                    result,
                )
                await self.assert_document(changed_state, result)
                await self.assert_document(changed_clock, result)
                await self.assert_document(inserted, str(int(result) - 1))
                await self.assert_document(other_partition, "1")
                await self.assert_document(survivor, "1")
                for id in ("archive", "missing"):
                    self.assertIsNone(await self.repository.read_document(id, "store"))
                self.assertEqual(len(self.snapshot()), 5)
                committed = self.snapshot()
                with self.assertRaises(Conflict):
                    await self.repository.compare_write(
                        changed_state, etag, upserts=(changed_clock, inserted),
                        deletes=("archive", "missing"),
                    )
                self.assertEqual(self.snapshot(), committed)

    async def test_delete_only_batch_including_missing_id_is_repeatable(self):
        await self.repository.write_document({"id": "archive", "pk": "store"})
        for etag, result in ((None, "1"), ("1", "2")):
            self.assertEqual(
                await self.repository.compare_write(
                    STATE, etag, deletes=("archive", "missing")
                ),
                result,
            )
            await self.assert_document(STATE, result)
            self.assertEqual(len(self.snapshot()), 1)

    async def test_late_delete_failure_rolls_back_state_upserts_and_earlier_delete(self):
        await self.repository.compare_write(STATE, None, upserts=(CLOCK,))
        for id in ("archive-a", "archive-b"):
            await self.repository.write_document({"id": id, "pk": "store", "value": id})
        before = self.snapshot()
        self.repository.db.execute(
            "CREATE TRIGGER fail_delete AFTER DELETE ON documents "
            "WHEN OLD.id='archive-b' BEGIN "
            "SELECT RAISE(ABORT, 'late deletion failed'); END"
        )
        with self.assertRaises(Conflict) as raised:
            await self.repository.compare_write(
                dict(STATE, revision=2), "1",
                upserts=(dict(CLOCK, tick=2), {"id": "new", "pk": "store"}),
                deletes=("archive-a", "archive-b", "missing"),
            )
        self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)
        self.assertIn("late deletion failed", str(raised.exception.__cause__))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.repository.db.in_transaction)


class CosmosAtomicStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = CosmosRepository(Settings())
        self.container = create_autospec(ContainerProxy, instance=True, spec_set=True)
        self.repository.container = self.container

    def assert_no_individual_writes(self):
        for method in ("create_item", "replace_item", "upsert_item", "delete_item"):
            getattr(self.container, method).assert_not_called()

    async def test_batch_create_replace_exact_sdk_tuples_and_response_etag(self):
        for etag in (None, '"original-etag"'):
            with self.subTest(etag=etag):
                self.container.reset_mock()
                self.container.execute_item_batch.return_value = [
                    {"statusCode": 201 if etag is None else 200, "eTag": '"state-etag"'},
                    {"statusCode": 200, "eTag": '"clock-etag"'},
                ]
                state, clock = dict(STATE, _etag="strip"), dict(CLOCK, _ts=123)
                before = deepcopy((state, clock))
                result = await self.repository.compare_write(state, etag, upserts=(clock,))
                expected = [
                    ("create", (STATE,)) if etag is None else
                    ("replace", ("state", STATE), {"if_match_etag": etag}),
                    ("upsert", (CLOCK,)),
                ]
                self.container.execute_item_batch.assert_awaited_once_with(
                    batch_operations=expected, partition_key="store"
                )
                self.assertEqual(result, '"state-etag"')
                self.assertEqual((state, clock), before)
                self.assert_no_individual_writes()
                wire = _base._format_batch_operations(deepcopy(expected))
                self.assertEqual(wire[0]["resourceBody"], STATE)
                if etag is None:
                    self.assertEqual(wire[0]["operationType"], "Create")
                    self.assertNotIn("ifMatch", wire[0])
                else:
                    self.assertEqual(wire[0]["operationType"], "Replace")
                    self.assertEqual(wire[0]["ifMatch"], etag)

    async def test_mixed_and_delete_only_batches_make_missing_deletes_idempotent(self):
        for etag in (None, '"old"'):
            for upserts in ((), (CLOCK,)):
                with self.subTest(etag=etag, upserts=upserts):
                    self.container.reset_mock()
                    self.container.execute_item_batch.return_value = [{"eTag": '"new"'}]
                    expected = [
                        ("create", (STATE,)) if etag is None else
                        ("replace", ("state", STATE), {"if_match_etag": etag}),
                    ]
                    if upserts:
                        expected.append(("upsert", (CLOCK,)))
                    for id in ("archive", "missing"):
                        expected.extend([
                            ("upsert", ({"id": id, "pk": "store"},)),
                            ("delete", (id,)),
                        ])
                    self.assertEqual(
                        await self.repository.compare_write(
                            STATE, etag, upserts=upserts, deletes=("archive", "missing")
                        ),
                        '"new"',
                    )
                    self.container.execute_item_batch.assert_awaited_once_with(
                        batch_operations=expected, partition_key="store"
                    )
                    self.assert_no_individual_writes()
                    wire = _base._format_batch_operations(deepcopy(expected))
                    self.assertEqual(
                        wire[-4:],
                        [
                            {"operationType": "Upsert",
                             "resourceBody": {"id": "archive", "pk": "store"}},
                            {"operationType": "Delete", "id": "archive"},
                            {"operationType": "Upsert",
                             "resourceBody": {"id": "missing", "pk": "store"}},
                            {"operationType": "Delete", "id": "missing"},
                        ],
                    )

    async def test_real_sdk_batch_failures_convert_only_409_and_412(self):
        for etag in (None, '"old"'):
            for status in (409, 412, 429, 500):
                for error_index in (0, 1, 3):
                    with self.subTest(etag=etag, status=status, error_index=error_index):
                        self.container.reset_mock()
                        responses = [
                            {"statusCode": status if i == error_index else 424}
                            for i in range(4)
                        ]
                        # Run the installed SDK's response/error handling; mock only its I/O.
                        connection = CosmosClientConnection.__new__(CosmosClientConnection)
                        connection._Batch = AsyncMock(return_value=(responses, {}))

                        async def execute_batch(*, batch_operations, partition_key):
                            return await connection.Batch(
                                "dbs/offline/colls/offline", batch_operations,
                                options={"partitionKey": partition_key},
                            )

                        self.container.execute_item_batch.side_effect = execute_batch
                        expected_error = Conflict if status in (409, 412) else CosmosBatchOperationError
                        with self.assertRaises(expected_error) as raised:
                            await self.repository.compare_write(
                                STATE, etag, upserts=(CLOCK,), deletes=("archive",)
                            )
                        error = (
                            raised.exception.__cause__ if expected_error is Conflict
                            else raised.exception
                        )
                        self.assertIs(type(error), CosmosBatchOperationError)
                        self.assertEqual(error.status_code, status)
                        self.assertEqual(error.error_index, error_index)
                        self.assertEqual(error.operation_responses, responses)
                        connection._Batch.assert_awaited_once()
                        self.container.execute_item_batch.assert_awaited_once()
                        self.assert_no_individual_writes()

    async def test_two_argument_sdk_create_replace_semantics_are_preserved(self):
        self.container.create_item.return_value = {"_etag": '"created"'}
        self.container.replace_item.return_value = {"_etag": '"replaced"'}
        self.assertEqual(
            await self.repository.compare_write(dict(STATE, _etag="strip"), None), '"created"'
        )
        self.container.create_item.assert_awaited_once_with(STATE)
        self.assertEqual(
            await self.repository.compare_write(dict(STATE, _etag="strip"), '"created"'),
            '"replaced"',
        )
        self.container.replace_item.assert_awaited_once_with(
            "state", STATE, etag='"created"', match_condition=MatchConditions.IfNotModified
        )
        self.container.execute_item_batch.assert_not_called()
        self.container.upsert_item.assert_not_called()
        self.container.delete_item.assert_not_called()

    async def test_http_errors_convert_only_409_and_412_without_retry_or_fallback(self):
        for etag, method in ((None, "create_item"), ('"old"', "replace_item")):
            for batch in (False, True):
                for status in (409, 412, 429, 500):
                    with self.subTest(etag=etag, batch=batch, status=status):
                        self.container.reset_mock(side_effect=True)
                        error = CosmosHttpResponseError(status_code=status, message="offline")
                        target = self.container.execute_item_batch if batch else getattr(
                            self.container, method
                        )
                        target.side_effect = error
                        expected_error = Conflict if status in (409, 412) else CosmosHttpResponseError
                        with self.assertRaises(expected_error) as raised:
                            await self.repository.compare_write(
                                STATE, etag, upserts=(CLOCK,) if batch else ()
                            )
                        if expected_error is Conflict:
                            self.assertIs(raised.exception.__cause__, error)
                        else:
                            self.assertIs(raised.exception, error)
                        target.assert_awaited_once()
                        if batch:
                            self.assert_no_individual_writes()
                        else:
                            self.container.execute_item_batch.assert_not_called()
                        self.assertEqual(len(self.container.mock_calls), 1)

    async def test_partition_and_duplicate_id_guards_reject_before_sdk_calls(self):
        for name, upserts, deletes, error in invalid_batches():
            for etag in (None, '"old"'):
                with self.subTest(case=name, etag=etag):
                    with self.assertRaisesRegex(ValueError, error):
                        await self.repository.compare_write(
                            STATE, etag, upserts=upserts, deletes=deletes
                        )
                    self.assertEqual(self.container.mock_calls, [])


class RepositoryBootstrapIntegrationTests(unittest.TestCase):
    def test_actual_factory_accepts_bootstrap_but_defaults_to_http_validation(self):
        with patch.dict(os.environ, JOB_ENV, clear=True):
            settings = Settings.from_env(for_bootstrap=True)
        with (
            patch("azure.cosmos.aio.CosmosClient",
                  side_effect=AssertionError("No Azure clients may be opened")) as cosmos,
            patch("azure.identity.aio.DefaultAzureCredential",
                  side_effect=AssertionError("No credentials may be opened")) as identity,
        ):
            repository = create_repository(settings, for_bootstrap=True)
            self.assertIsInstance(repository, CosmosRepository)
            self.assertIs(repository.settings, settings)
            self.assertIsNone(repository.client)
            self.assertIsNone(repository.credential)
            with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
                create_repository(settings)
            with self.assertRaisesRegex(ValueError, "APP_PASSWORD"):
                create_repository(settings, for_bootstrap=False)
            with self.assertRaisesRegex(ValueError, "PUBLIC_ORIGIN"):
                create_repository(replace(settings, app_password="offline-test-password"))
            with self.assertRaisesRegex(ValueError, "SESSION_SECRET"):
                create_repository(replace(
                    settings, app_password="offline-test-password",
                    public_origin="https://testserver", session_secret="short",
                ))
            cosmos.assert_not_called()
            identity.assert_not_called()


if __name__ == "__main__":
    unittest.main()
