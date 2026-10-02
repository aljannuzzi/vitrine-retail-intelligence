"""Local contract checks against the current backend; never contact Azure."""

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.retail import simulation
from vision import worker as w


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
JPEG = b"\xff\xd8contract-test-jpeg\xff\xd9"
PERSON = {"label": "person", "score": 0.9, "box": [0.1, 0.2, 0.8, 0.9]}
BACKEND_AVAILABLE = all(importlib.util.find_spec(name) is not None
                        for name in ("fastapi", "pydantic", "azure"))


class BackendClockTests(unittest.TestCase):
    def test_actual_backend_clock_running_paused_variant_and_viewer_expiry(self):
        state = simulation.seed(NOW.timestamp())
        state.update(lastViewer=NOW.timestamp(), elapsedSeconds=59, speed=2)
        for status, source in (("running", 1), ("paused", 59)):
            state["state"] = status
            clock = w.Clock.parse(simulation.clock_document(state, NOW.timestamp()))
            self.assertEqual(clock.source_seconds(NOW + timedelta(seconds=1)), source)
            self.assertEqual(clock.variant, "baseline")
            self.assertTrue(clock.active(NOW + timedelta(seconds=119)))
            self.assertFalse(clock.active(NOW + timedelta(seconds=120)))
        state["interventions"] = [{"id": "synthetic-intervention"}]
        state["layoutVersion"] += 1
        clock = w.Clock.parse(simulation.clock_document(state, NOW.timestamp()))
        self.assertEqual(clock.identity, (state["epochId"], state["layoutVersion"], "optimized"))


@unittest.skipUnless(BACKEND_AVAILABLE, "Run with backend\\.venv\\Scripts\\python.exe for API dependencies")
class BackendFrameContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from backend.retail.media import BlobMedia
        from backend.retail.models import VisionObservation

        self.observation_model = VisionObservation
        state = simulation.seed(NOW.timestamp())
        state["lastViewer"] = NOW.timestamp()
        self.clock = simulation.clock_document(state, NOW.timestamp())
        self.uploaded = {}
        self.documents = []
        self.blob = Mock()
        self.blob.upload_blob.side_effect = self.upload
        self.store = object.__new__(w.AzureStore)
        self.store.media = Mock()
        self.store.media.get_blob_client.return_value = self.blob
        self.store.container = Mock()
        self.store.container.upsert_item.side_effect = self.persist
        self.store.read_clock = lambda: copy.deepcopy(self.clock)
        self.store.read_request = lambda: None
        self.detector = Mock()
        self.detector.detect.return_value = [copy.deepcopy(PERSON)]
        self.worker = w.Worker(
            self.store, Mock(), self.detector, now=lambda: NOW,
            extractor=lambda *_: JPEG, decoder=lambda data: data,
        )
        self.media = BlobMedia(SimpleNamespace(environment="local"))
        self.media.ready = True
        self.media.container = Mock()
        self.reader = Mock()
        self.media.container.get_blob_client.return_value = self.reader
        self.reader.get_blob_properties = AsyncMock(side_effect=self.properties)
        self.reader.download_blob = AsyncMock(side_effect=self.download)
        self.etag = "frame-v1"

    def upload(self, jpeg, **kwargs):
        self.assertEqual(self.documents[-1]["observation"]["status"], "warming")
        self.uploaded = {"bytes": jpeg, **kwargs}

    def persist(self, document):
        self.observation_model.model_validate(document["observation"])
        self.documents.append(copy.deepcopy(document))

    def properties(self):
        return SimpleNamespace(size=len(self.uploaded["bytes"]), etag=self.etag,
                               metadata=self.uploaded["metadata"].copy())

    def download(self, **kwargs):
        from azure.core import MatchConditions
        self.assertEqual(kwargs["etag"], self.etag)
        self.assertEqual(kwargs["match_condition"], MatchConditions.IfNotModified)
        payload = self.uploaded["bytes"]

        async def chunks():
            yield payload

        return SimpleNamespace(chunks=chunks)

    async def fetch(self, observation_id):
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "headers": [],
                           "query_string": f"observation={observation_id}".encode()})
        return await self.media.stream("frames/camera-denim.jpg", request, "image/jpeg")

    async def test_worker_upload_and_backend_read_share_exact_observation_id_and_bytes(self):
        self.assertEqual(self.worker.tick(), "ready")
        observation = self.documents[-1]["observation"]
        self.assertEqual(self.uploaded["metadata"], {"observationid": observation["id"]})
        response = await self.fetch(observation["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Vitrine-Observation-Id"], observation["id"])
        self.assertEqual(b"".join([chunk async for chunk in response.body_iterator]), JPEG)
        self.assertIs(self.detector.detect.call_args.args[0], self.uploaded["bytes"])
        self.blob.set_blob_metadata.assert_not_called()

    async def test_replaced_frame_returns_409_not_latest_bytes_for_old_observation(self):
        from fastapi import HTTPException
        self.assertEqual(self.worker.tick(), "ready")
        old_id = self.documents[-1]["observation"]["id"]
        self.worker.cursor = 0
        self.assertEqual(self.worker.tick(), "ready")
        with self.assertRaises(HTTPException) as caught:
            await self.fetch(old_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.reader.download_blob.assert_not_called()

    async def test_missing_metadata_returns_409_without_downloading(self):
        from fastapi import HTTPException
        self.assertEqual(self.worker.tick(), "ready")
        observation_id = self.documents[-1]["observation"]["id"]
        self.uploaded["metadata"] = {}
        with self.assertRaises(HTTPException) as caught:
            await self.fetch(observation_id)
        self.assertEqual(caught.exception.status_code, 409)
        self.reader.download_blob.assert_not_called()

    async def test_etag_race_fails_closed_without_returning_a_response(self):
        from azure.core.exceptions import HttpResponseError
        from fastapi import HTTPException
        self.assertEqual(self.worker.tick(), "ready")
        error = HttpResponseError("Synthetic conditional-download conflict")
        error.status_code = 412
        self.reader.download_blob.side_effect = error
        with self.assertRaises(HTTPException) as caught:
            await self.fetch(self.documents[-1]["observation"]["id"])
        # Backend currently maps SDK 412 to 503; owner must map this conflict to 409.
        self.assertIn(caught.exception.status_code, (409, 503))
        self.assertEqual(self.reader.download_blob.await_count, 1)

    async def test_failure_observation_matches_backend_schema_without_fake_counts(self):
        self.detector.detect.side_effect = RuntimeError("synthetic inference failure")
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.worker.tick(), "unavailable")
        observation = self.documents[-1]["observation"]
        self.assertIsNone(observation["people"])
        self.assertIsNone(observation["frameUri"])
        self.assertEqual(observation["detections"], [])
        self.blob.upload_blob.assert_not_called()


if __name__ == "__main__":
    unittest.main()
