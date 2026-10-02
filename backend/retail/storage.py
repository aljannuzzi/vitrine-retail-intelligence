import json
from pathlib import Path
import sqlite3


class Conflict(RuntimeError):
    pass


def batch_bodies(doc, upserts, deletes):
    if any(item["pk"] != doc["pk"] for item in upserts):
        raise ValueError("Atomic writes require one partition")
    ids = [doc["id"], *(item["id"] for item in upserts), *deletes]
    if len(ids) != len(set(ids)):
        raise ValueError("Atomic writes require distinct document IDs")
    return [{k: v for k, v in item.items() if not k.startswith("_")}
            for item in (doc, *upserts)]


class LocalRepository:
    mode = "local"

    def __init__(self, path):
        self.path = path
        self.db = None

    async def initialize(self):
        if self.db is not None:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS documents "
                        "(pk TEXT, id TEXT, body TEXT, version INTEGER, PRIMARY KEY(pk,id))")
        self.db.commit()

    async def read_document(self, id, pk):
        row = self.db.execute("SELECT body,version FROM documents WHERE pk=? AND id=?",
                              (pk, id)).fetchone()
        if not row:
            return None
        return {**json.loads(row[0]), "_etag": str(row[1])}

    async def write_document(self, doc):
        body = {k: v for k, v in doc.items() if not k.startswith("_")}
        with self.db:
            self.db.execute(
                "INSERT INTO documents VALUES (?,?,?,1) ON CONFLICT(pk,id) DO UPDATE "
                "SET body=excluded.body,version=documents.version+1",
                (doc["pk"], doc["id"], json.dumps(body)))

    async def compare_write(self, doc, etag, *, upserts=(), deletes=()):
        bodies = batch_bodies(doc, upserts, deletes)
        body = json.dumps(bodies[0])
        try:
            with self.db:
                if etag is None:
                    self.db.execute("INSERT INTO documents VALUES (?,?,?,1)",
                                    (doc["pk"], doc["id"], body))
                else:
                    cursor = self.db.execute(
                        "UPDATE documents SET body=?,version=version+1 "
                        "WHERE pk=? AND id=? AND version=?",
                        (body, doc["pk"], doc["id"], int(etag)))
                    if cursor.rowcount != 1:
                        raise Conflict("Concurrent state writer")
                for item in bodies[1:]:
                    self.db.execute(
                        "INSERT INTO documents VALUES (?,?,?,1) ON CONFLICT(pk,id) DO UPDATE "
                        "SET body=excluded.body,version=documents.version+1",
                        (item["pk"], item["id"], json.dumps(item)))
                for id in deletes:
                    self.db.execute("DELETE FROM documents WHERE pk=? AND id=?", (doc["pk"], id))
            return "1" if etag is None else str(int(etag) + 1)
        except sqlite3.IntegrityError as exc:
            raise Conflict("Concurrent state creator") from exc

    async def delete_document(self, id, pk):
        with self.db:
            self.db.execute("DELETE FROM documents WHERE pk=? AND id=?", (pk, id))

    async def close(self):
        if self.db:
            self.db.close()
            self.db = None


class CosmosRepository:
    mode = "cosmos"

    def __init__(self, settings):
        self.settings = settings
        self.client = self.credential = None

    async def initialize(self):
        from azure.identity.aio import DefaultAzureCredential
        from azure.cosmos.aio import CosmosClient
        if self.client:
            await self.close()
        self.credential = DefaultAzureCredential(
            managed_identity_client_id=self.settings.azure_client_id or None,
            exclude_interactive_browser_credential=True)
        self.client = CosmosClient(self.settings.cosmos_endpoint, credential=self.credential,
                                   connection_timeout=10, timeout=20, retry_total=2)
        self.container = self.client.get_database_client(
            self.settings.cosmos_database).get_container_client(self.settings.cosmos_container)
        properties = await self.container.read()
        if properties.get("partitionKey", {}).get("paths") != ["/pk"]:
            raise ValueError("Cosmos container must use /pk")

    async def read_document(self, id, pk):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            return await self.container.read_item(id, partition_key=pk)
        except CosmosResourceNotFoundError:
            return None

    async def write_document(self, doc):
        await self.container.upsert_item({k: v for k, v in doc.items() if not k.startswith("_")})

    async def compare_write(self, doc, etag, *, upserts=(), deletes=()):
        from azure.core import MatchConditions
        from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError
        bodies = batch_bodies(doc, upserts, deletes)
        body = bodies[0]
        try:
            if upserts or deletes:
                operations = [
                    ("create", (body,)) if etag is None else
                    ("replace", (doc["id"], body), {"if_match_etag": etag})
                ]
                operations.extend(("upsert", (item,)) for item in bodies[1:])
                for id in deletes:
                    # Upsert+delete makes a missing archive an idempotent no-op,
                    # without exposing a placeholder outside the transaction.
                    operations.extend([("upsert", ({"id": id, "pk": doc["pk"]},)),
                                       ("delete", (id,))])
                results = await self.container.execute_item_batch(
                    batch_operations=operations, partition_key=doc["pk"])
                return results[0]["eTag"]
            result = (await self.container.create_item(body) if etag is None else
                      await self.container.replace_item(
                          doc["id"], body, etag=etag, match_condition=MatchConditions.IfNotModified))
            return result["_etag"]
        except (CosmosHttpResponseError, CosmosBatchOperationError) as exc:
            if exc.status_code in (409, 412):
                raise Conflict("Concurrent state writer") from exc
            raise

    async def delete_document(self, id, pk):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError
        try:
            await self.container.delete_item(id, partition_key=pk)
        except CosmosResourceNotFoundError:
            pass

    async def close(self):
        if self.client:
            await self.client.close()
        if self.credential:
            await self.credential.close()
        self.client = self.credential = None


def create_repository(settings, *, for_bootstrap=False):
    settings.validate(for_bootstrap=for_bootstrap)
    if settings.environment == "local" and settings.local_mode:
        return LocalRepository(settings.local_database)
    return CosmosRepository(settings)
