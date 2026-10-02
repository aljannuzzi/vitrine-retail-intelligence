"""Deterministic worker tests: stdlib only, no cloud, model, FFmpeg or sleeps."""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from vision import worker as w


NOW = datetime(2026, 10, 1, 20, 10, 27, tzinfo=timezone.utc)
SECRET = "https://private.example/media?sig=SECRET_TOKEN raw provider message"
SECRET_FRAGMENTS = ("https://private.example", "SECRET_TOKEN", "raw provider message")
PERSON = {"label": "person", "score": 0.9, "box": [0.1, 0.2, 0.8, 0.9]}
# A complete baseline 1x1 grayscale JPEG. The single DC coefficient is zero,
# followed by EOB; no image library or downloaded fixture is needed.
JPEG = (
    b"\xff\xd8\xff\xdb\x00\x43\x00" + b"\x01" * 64
    + bytes.fromhex("ffc0000b080001000101011100")
    + bytes.fromhex("ffc400140001") + b"\x00" * 15 + b"\x00"
    + bytes.fromhex("ffc400141001") + b"\x00" * 15 + b"\x00"
    + bytes.fromhex("ffda0008010100003f003fffd9")
)


def clock_document(**changes):
    document = {
        "id": "clock", "pk": "store-demo", "epochId": "epoch-one",
        "state": "running", "elapsedSeconds": 17, "speed": 2,
        "updatedAt": w.isoformat(NOW),
        "activeUntil": w.isoformat(NOW + timedelta(minutes=10)),
        "layoutVersion": 3, "variant": "baseline",
    }
    document.update(changes)
    return document


def request_document(**changes):
    document = {
        "id": "vision-request", "pk": "store-demo",
        "epochId": "epoch-one", "requestedAt": w.isoformat(NOW),
    }
    document.update(changes)
    return document


def valid_environment():
    return {
        "ENVIRONMENT": "azure",
        "AZURE_CLIENT_ID": "12345678-1234-4321-8765-123456789abc",
        "COSMOS_ENDPOINT": "https://vitrine.documents.azure.com:443/",
        "AZURE_STORAGE_ACCOUNT": "vitrine123",
    }


class FakeTime:
    def __init__(self):
        self.wall = NOW
        self.ticks = 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.ticks

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.ticks += seconds


class ErrorAssertions:
    def assertWorkerError(self, code, call, *args, **kwargs):
        with self.assertRaises(w.WorkerError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertTrue(caught.exception.__suppress_context__)


class ClockTests(ErrorAssertions, unittest.TestCase):
    def test_running_extrapolation_speed_wrap_and_future_update(self):
        for elapsed, speed, delta, expected in (
            (17, 2, 3, 23), (59, 2, 2, 3), (120, 5, 12, 0),
            (12.5, 0.5, 3, 14), (17, 0, 300, 17), (17, 2, -10, 17),
        ):
            with self.subTest(elapsed=elapsed, speed=speed, delta=delta):
                clock = w.Clock.parse(clock_document(elapsedSeconds=elapsed, speed=speed))
                self.assertAlmostEqual(clock.source_seconds(NOW + timedelta(seconds=delta)), expected)

    def test_paused_clock_never_extrapolates_but_still_wraps(self):
        clock = w.Clock.parse(clock_document(state="paused", elapsedSeconds=123.25, speed=5))
        for delta in (-20, 0, 500):
            with self.subTest(delta=delta):
                self.assertEqual(clock.source_seconds(NOW + timedelta(seconds=delta)), 3.25)

    def test_active_until_is_exclusive_independent_of_paused_state(self):
        for state in ("running", "paused"):
            clock = w.Clock.parse(clock_document(state=state, activeUntil=w.isoformat(NOW)))
            self.assertTrue(clock.active(NOW - timedelta(microseconds=1)))
            self.assertFalse(clock.active(NOW))
            self.assertFalse(clock.active(NOW + timedelta(seconds=1)))
        self.assertFalse(w.Clock.parse(clock_document(activeUntil=None)).active(NOW))

    def test_timestamp_normalizes_offsets_and_isoformat_is_utc_milliseconds(self):
        self.assertEqual(w.timestamp("2026-10-01T17:10:27-03:00"), NOW)
        self.assertEqual(w.timestamp(w.isoformat(NOW)), NOW)
        self.assertEqual(w.isoformat(NOW.replace(microsecond=123456)), "2026-10-01T20:10:27.123Z")
        self.assertEqual(w.Clock.parse(clock_document()).identity, ("epoch-one", 3, "baseline"))

    def test_invalid_timestamps_fail_closed(self):
        for value in (None, 1, True, "", "yesterday", "2026-10-01", "2026-10-01T20:10:27",
                      "2026-02-30T00:00:00Z", "2026-10-01T00:00:00+25:00"):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                w.timestamp(value)
            for field in ("updatedAt", "activeUntil"):
                if field == "activeUntil" and value is None:
                    continue
                with self.subTest(field=field, value=value):
                    self.assertWorkerError("clock_invalid", w.Clock.parse, clock_document(**{field: value}))

    def test_invalid_clock_shapes_fields_and_missing_fields(self):
        for document in (None, [], "clock", {}, 1):
            with self.subTest(document=document):
                self.assertWorkerError("clock_invalid", w.Clock.parse, document)
        invalid = {
            "id": ["other", None], "pk": ["vision", None],
            "epochId": ["", None, 1, "x" * 129],
            "layoutVersion": [True, -1, 1.5, "1", None],
            "state": ["stopped", None], "variant": ["../../outside", "other", None],
            "elapsedSeconds": [-1, math.nan, math.inf, -math.inf, True, "1", None, 10**1000],
            "speed": [-1, math.nan, math.inf, -math.inf, False, "1", None, 10**1000],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertWorkerError("clock_invalid", w.Clock.parse, clock_document(**{field: value}))
        for field in clock_document():
            document = clock_document()
            del document[field]
            with self.subTest(missing=field):
                self.assertWorkerError("clock_invalid", w.Clock.parse, document)

    def test_finite_number_rejects_boolean_non_numeric_nonfinite_and_negative(self):
        for value in (True, False, None, "1", [], {}, -0.1, math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                w.finite_number(value)
        for value in (0, 0.25, 42, 1e308):
            self.assertEqual(w.finite_number(value), float(value))
        self.assertEqual(w.finite_number(-2, minimum=-2), -2.0)
        with self.assertRaises(ValueError):
            w.finite_number(-3, minimum=-2)

    def test_extrapolation_overflow_is_clock_invalid(self):
        clock = w.Clock.parse(clock_document(elapsedSeconds=1e308, speed=1e308))
        with self.assertRaises(w.WorkerError) as caught:
            clock.source_seconds(NOW + timedelta(seconds=10))
        self.assertEqual(caught.exception.code, "clock_invalid")


class SourceFrameTests(ErrorAssertions, unittest.TestCase):
    def test_clip_start_and_last_frame_are_floored(self):
        for seconds, expected in (
            (0, 0), (0.001, 0), (1 / 15, 1 / 15),
            (0.1, 1 / 15), (59.999, 59.93333333333333),
            (math.nextafter(60, 0), 59.93333333333333),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(w.source_frame_seconds(seconds), expected)

    def test_running_wrap_and_paused_clock_keep_continuous_helper(self):
        for state, elapsed, delta, continuous, expected in (
            ("running", 59, 0.5, 0, 0),
            ("running", 59, 0.55, 0.1, 1 / 15),
            ("paused", 123.25, 500, 3.25, 3.2),
            ("paused", 59.999, 500, 59.999, 59.93333333333333),
        ):
            with self.subTest(state=state, elapsed=elapsed, delta=delta):
                clock = w.Clock.parse(clock_document(state=state, elapsedSeconds=elapsed))
                seconds = clock.source_seconds(NOW + timedelta(seconds=delta))
                self.assertAlmostEqual(seconds, continuous)
                self.assertEqual(w.source_frame_seconds(seconds), expected)

    def test_invalid_source_is_rejected(self):
        for seconds in (-1, 60, math.nan, math.inf, None, "1", True, 10**1000):
            with self.subTest(seconds=seconds):
                self.assertWorkerError("clock_invalid", w.source_frame_seconds, seconds)


class RequestTests(unittest.TestCase):
    def test_ttl_boundaries_same_epoch_and_future_rejection(self):
        clock = w.Clock.parse(clock_document())
        for age, fresh in ((0, True), (29.999, True), (30, False), (31, False), (-0.001, False)):
            with self.subTest(age=age):
                document = request_document(requestedAt=w.isoformat(NOW - timedelta(seconds=age)))
                request = w.Request.fresh(document, clock, NOW, 30)
                self.assertEqual(request is not None, fresh)
                if fresh:
                    self.assertEqual(request.key, ("epoch-one", w.timestamp(document["requestedAt"])))
        self.assertIsNone(w.Request.fresh(request_document(epochId="old"), clock, NOW, 30))

    def test_invalid_requests_are_ignored_not_raised(self):
        clock = w.Clock.parse(clock_document())
        documents = [None, [], "", {}, 7]
        for field, values in {
            "id": ["clock", None], "pk": ["vision", None],
            "epochId": [None, [], "other"],
            "requestedAt": [None, "", "bad", 2, "2026-10-01T20:10:27"],
        }.items():
            documents.extend(request_document(**{field: value}) for value in values)
        for field in request_document():
            document = request_document()
            del document[field]
            documents.append(document)
        for document in documents:
            with self.subTest(document=document):
                self.assertIsNone(w.Request.fresh(document, clock, NOW, 30))


class WorkerTests(unittest.TestCase):
    def make_worker(self, *, active=True, request=None, detector_present=True):
        self.time = FakeTime()
        self.clock = clock_document(activeUntil=w.isoformat(NOW + timedelta(minutes=10)) if active else None)
        self.request = request
        self.store = Mock()
        self.store.read_clock.side_effect = lambda: copy.deepcopy(self.clock)
        self.store.read_request.side_effect = lambda: copy.deepcopy(self.request)
        self.documents = {}
        self.frames = {}
        self.frame_observations = {}
        self.store.upsert_observation.side_effect = self.persist_observation
        self.store.upload_frame.side_effect = self.persist_frame
        self.cache = Mock()
        self.cache.get.return_value = Path(__file__).parent / "not-created.mp4"
        self.detector = Mock()
        self.detector.detect.return_value = [copy.deepcopy(PERSON)]
        self.extractor = Mock(return_value=JPEG)
        self.image = object()
        self.decoder = Mock(return_value=self.image)
        self.worker = w.Worker(
            self.store, self.cache, self.detector if detector_present else None,
            now=self.time.now, monotonic=self.time.monotonic,
            extractor=self.extractor, decoder=self.decoder,
        )
        return self.worker

    def assert_no_writes(self):
        self.store.upload_frame.assert_not_called()
        self.store.upsert_observation.assert_not_called()

    def assert_no_work(self):
        self.cache.get.assert_not_called()
        self.extractor.assert_not_called()
        self.decoder.assert_not_called()
        self.detector.detect.assert_not_called()
        self.assert_no_writes()

    def observation(self):
        return self.store.upsert_observation.call_args.args[0]["observation"]

    def persist_observation(self, document):
        self.documents[document["id"]] = copy.deepcopy(document)

    def persist_frame(self, camera, jpeg, observation_id):
        self.frames[camera] = jpeg
        self.frame_observations[camera] = observation_id

    def seed_old_ready(self):
        self.assertEqual(self.worker.tick(), "ready")
        self.worker.cursor = 0
        self.store.reset_mock()
        self.extractor.return_value = JPEG + b"new-frame"

    def assert_warming(self, document):
        observation = document["observation"]
        self.assertEqual(observation["status"], "warming")
        self.assertIsNone(observation["people"])
        self.assertEqual(observation["detections"], [])
        self.assertIsNone(observation["frameUri"])
        self.assertIsNone(observation["error"])
        self.assertNotIn("frameSha256", document)
        self.assertEqual(
            (document["epochId"], document["layoutVersion"], document["variant"]),
            ("epoch-one", 3, "baseline"),
        )

    def test_inactivity_has_zero_download_extraction_inference_or_writes(self):
        requests = [None, request_document(epochId="other"),
                    request_document(requestedAt=w.isoformat(NOW - timedelta(seconds=30))),
                    request_document(requestedAt=w.isoformat(NOW + timedelta(seconds=1)))]
        for request in requests:
            with self.subTest(request=request):
                worker = self.make_worker(active=False, request=request)
                for _ in range(4):
                    self.assertEqual(worker.tick(), "idle")
                self.assert_no_work()
                self.assertTrue(worker.health.snapshot()[0])

    def test_fresh_inactive_request_serves_each_camera_once_then_stops(self):
        worker = self.make_worker(active=False, request=request_document())
        for count, camera in enumerate(w.CAMERAS, 1):
            self.assertEqual(worker.tick(), "ready")
            self.assertEqual(self.cache.get.call_count, count)
            self.assertEqual(self.store.upsert_observation.call_args.args[0]["id"], camera)
        for _ in range(5):
            self.assertEqual(worker.tick(), "idle")
        for mock in (self.cache.get, self.extractor, self.decoder, self.detector.detect,
                     self.store.upload_frame):
            self.assertEqual(mock.call_count, 3)
        self.assertEqual(self.store.upsert_observation.call_count, 6)
        self.assertEqual(worker.request_done, set(w.CAMERAS))
        self.time.advance(1)
        self.request = request_document(requestedAt=w.isoformat(self.time.now()))
        for _ in w.CAMERAS:
            self.assertEqual(worker.tick(), "ready")
        self.assertEqual(worker.tick(), "idle")
        self.assertEqual(self.store.upsert_observation.call_count, 12)

    def test_unavailable_manual_request_is_also_served_once_per_camera(self):
        worker = self.make_worker(active=False, request=request_document())
        self.detector.detect.side_effect = RuntimeError(SECRET)
        self.assertEqual([worker.tick() for _ in range(4)], ["unavailable"] * 3 + ["idle"])
        self.assertEqual(self.store.upsert_observation.call_count, 3)
        self.store.upload_frame.assert_not_called()
        self.assertIsNone(self.observation()["people"])

    def test_active_round_robin_exactly_one_camera_each_tick_even_when_paused(self):
        for state in ("running", "paused"):
            with self.subTest(state=state):
                worker = self.make_worker()
                self.clock["state"] = state
                for index in range(7):
                    self.assertEqual(worker.tick(), "ready")
                    camera = w.CAMERAS[index % 3]
                    self.cache.get.assert_called_with(camera, "baseline")
                    self.store.upload_frame.assert_called_with(camera, JPEG, self.observation()["id"])
                    for mock in (self.cache.get, self.extractor, self.detector.detect,
                                 self.store.upload_frame):
                        self.assertEqual(mock.call_count, index + 1)
                    self.assertEqual(self.store.upsert_observation.call_count, 2 * (index + 1))

    def test_optimized_variant_and_source_time_are_sampled_after_download(self):
        worker = self.make_worker()
        self.clock.update(variant="optimized", elapsedSeconds=59)

        def download(*args):
            self.time.advance(2)
            return self.cache.get.return_value

        self.cache.get.side_effect = download
        self.assertEqual(worker.tick(), "ready")
        self.cache.get.assert_called_once_with(w.CAMERAS[0], "optimized")
        self.extractor.assert_called_once_with(self.cache.get.return_value, 3)
        self.assertEqual(self.observation()["sourceSeconds"], 3)
        self.assertEqual(self.store.upsert_observation.call_args.args[0]["variant"], "optimized")

    def test_extraction_and_observations_use_actual_frame_pts(self):
        for state, elapsed, expected in (
            ("running", 0, 0), ("running", 60.1, 1 / 15),
            ("paused", 123.25, 3.2), ("running", 59.999, 59.93333333333333),
        ):
            with self.subTest(state=state, elapsed=elapsed):
                worker = self.make_worker()
                self.clock.update(state=state, elapsedSeconds=elapsed)
                self.assertEqual(worker.tick(), "ready")
                self.extractor.assert_called_once_with(self.cache.get.return_value, expected)
                for call in self.store.upsert_observation.call_args_list:
                    self.assertEqual(call.args[0]["observation"]["sourceSeconds"], expected)

    def test_envelope_fields_and_identical_jpeg_object_decoded_and_uploaded(self):
        worker = self.make_worker()

        def detect(image):
            self.assertIs(image, self.image)
            self.time.advance(0.125)
            return [copy.deepcopy(PERSON)]

        self.detector.detect.side_effect = detect
        self.assertEqual(worker.tick(), "ready")
        document = self.store.upsert_observation.call_args.args[0]
        self.assertEqual(set(document), {
            "id", "pk", "epochId", "layoutVersion", "variant", "frameSha256", "observation",
        })
        self.assertEqual(document["variant"], "baseline")
        self.assertEqual(document["frameSha256"], hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(self.store.upsert_observation.call_count, 2)
        self.assert_warming(self.store.upsert_observation.call_args_list[0].args[0])
        self.assertEqual({key: document[key] for key in ("id", "pk", "epochId", "layoutVersion")},
                         {"id": w.CAMERAS[0], "pk": "vision", "epochId": "epoch-one", "layoutVersion": 3})
        observation = document["observation"]
        self.assertEqual(set(observation), {
            "id", "cameraId", "status", "observedAt", "sourceSeconds", "people",
            "detections", "frameUri", "latencyMs", "model", "modelRevision", "provenance", "error",
        })
        self.assertEqual(str(uuid.UUID(observation["id"])), observation["id"])
        self.assertEqual(observation["cameraId"], document["id"])
        self.assertEqual(observation["status"], "ready")
        self.assertEqual(observation["observedAt"], w.isoformat(self.time.now()))
        self.assertEqual(observation["sourceSeconds"], 17)
        self.assertEqual(observation["people"], 1)
        self.assertEqual(observation["detections"], [PERSON])
        self.assertEqual(observation["frameUri"], "/api/vision/frames/camera-denim.jpg")
        self.assertEqual(observation["latencyMs"], 125)
        self.assertEqual(observation["model"], "PekingU/rtdetr_v2_r18vd")
        self.assertEqual(observation["modelRevision"], "5650961749fa93567c0d46fc7f43ea4f9e914107")
        self.assertEqual(observation["provenance"], "model-inference")
        self.assertIsNone(observation["error"])
        self.assertIs(self.decoder.call_args.args[0], JPEG)
        self.assertIs(self.store.upload_frame.call_args.args[1], self.decoder.call_args.args[0])
        self.assertEqual(self.store.upload_frame.call_args.args[2], observation["id"])
        self.assertEqual(self.frame_observations[document["id"]], observation["id"])
        self.assertEqual(json.loads(json.dumps(document, allow_nan=False)), document)

    def test_observation_id_exists_before_upload_and_ready_is_persisted_after_it(self):
        worker = self.make_worker()
        uploaded = {}

        def upload(camera, jpeg, observation_id):
            self.assertEqual(str(uuid.UUID(observation_id)), observation_id)
            self.assert_warming(self.documents[camera])
            uploaded.update(camera=camera, jpeg=jpeg, observation_id=observation_id)
            self.persist_frame(camera, jpeg, observation_id)

        def publish(document):
            if document["observation"]["status"] == "ready":
                self.assertEqual(document["observation"]["id"], uploaded["observation_id"])
                self.assertIs(uploaded["jpeg"], self.decoder.call_args.args[0])
            self.persist_observation(document)

        self.store.upload_frame.side_effect = upload
        self.store.upsert_observation.side_effect = publish
        self.assertEqual(worker.tick(), "ready")
        first_id = uploaded["observation_id"]
        worker.cursor = 0
        self.assertEqual(worker.tick(), "ready")
        self.assertNotEqual(first_id, uploaded["observation_id"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is required for pixel identity test")
    def test_real_jpeg_decoder_pixels_match_unchanged_uploaded_bytes(self):
        from PIL import Image
        worker = self.make_worker()
        worker.decoder = w.decode_jpeg
        measured = {}

        def detect(image):
            measured.update(size=image.size, mode=image.mode, pixels=image.tobytes())
            return [copy.deepcopy(PERSON)]

        self.detector.detect.side_effect = detect
        self.assertEqual(worker.tick(), "ready")
        uploaded = self.frames[w.CAMERAS[0]]
        self.assertIs(uploaded, JPEG)
        with Image.open(io.BytesIO(uploaded)) as image:
            image = image.convert("RGB")
            self.assertEqual((image.size, image.mode, image.tobytes()),
                             (measured["size"], measured["mode"], measured["pixels"]))

    def test_upload_timeout_after_commit_never_publishes_ready_or_zero(self):
        worker = self.make_worker()

        def upload(camera, jpeg, observation_id):
            self.persist_frame(camera, jpeg, observation_id)
            raise w.WorkerError("frame_upload_failed")

        self.store.upload_frame.side_effect = upload
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(worker.tick(), "unavailable")
        self.assertIsNone(self.observation()["people"])
        self.assertIsNone(self.observation()["frameUri"])
        self.assertEqual(self.observation()["detections"], [])
        self.assertNotEqual(self.observation()["id"], self.frame_observations[w.CAMERAS[0]])

    def test_legitimate_empty_detections_are_ready_zero_not_unavailable(self):
        worker = self.make_worker()
        self.detector.detect.return_value = []
        self.assertEqual(worker.tick(), "ready")
        self.assertEqual(self.observation()["people"], 0)
        self.assertEqual(self.observation()["detections"], [])
        self.assertIsNone(self.observation()["error"])
        self.assertIsNotNone(self.observation()["frameUri"])
        self.store.upload_frame.assert_called_once_with(w.CAMERAS[0], JPEG, self.observation()["id"])

    def test_warming_is_persisted_before_upload_then_ready_hash_matches_new_jpeg(self):
        worker = self.make_worker()
        self.seed_old_ready()
        camera = w.CAMERAS[0]

        def upload(camera, jpeg, observation_id):
            self.assert_warming(self.documents[camera])
            self.assertEqual(self.frames[camera], JPEG)
            self.persist_frame(camera, jpeg, observation_id)

        self.store.upload_frame.side_effect = upload
        self.assertEqual(worker.tick(), "ready")
        document = self.documents[camera]
        self.assertEqual(document["observation"]["status"], "ready")
        self.assertEqual(document["frameSha256"], hashlib.sha256(self.frames[camera]).hexdigest())
        self.assertEqual(self.frames[camera], self.extractor.return_value)
        self.assertEqual(self.frame_observations[camera], document["observation"]["id"])
        self.assertEqual(self.store.upsert_observation.call_count, 2)

    def test_initial_warming_failure_prevents_upload_and_preserves_old_ready_and_jpeg(self):
        worker = self.make_worker()
        self.seed_old_ready()
        previous = copy.deepcopy(self.documents)
        self.store.upsert_observation.side_effect = w.WorkerError("observation_write_failed")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(worker.tick(), "error")
        self.assertEqual(json.loads(output.getvalue()),
                         {"event": "vision_error", "code": "observation_write_failed"})
        self.store.upload_frame.assert_not_called()
        self.store.upsert_observation.assert_called_once()
        self.assertEqual(self.documents, previous)
        self.assertEqual(self.frames[w.CAMERAS[0]], JPEG)
        self.assertEqual(worker.request_done, set())
        self.assertFalse(worker.health.snapshot()[0])

    def test_stale_after_upload_replaces_old_ready_with_warming(self):
        for field, value in (
            ("epochId", "epoch-two"), ("layoutVersion", 4), ("variant", "optimized"),
            ("activeUntil", w.isoformat(NOW)),
        ):
            with self.subTest(field=field):
                worker = self.make_worker()
                self.seed_old_ready()
                camera = w.CAMERAS[0]

                def upload(camera, jpeg, observation_id):
                    self.assert_warming(self.documents[camera])
                    self.persist_frame(camera, jpeg, observation_id)
                    self.clock[field] = value

                self.store.upload_frame.side_effect = upload
                self.assertEqual(worker.tick(), "stale")
                self.assert_warming(self.documents[camera])
                self.assertEqual(self.frames[camera], self.extractor.return_value)
                self.store.upsert_observation.assert_called_once()
                self.assertEqual(worker.request_done, set())

    def test_stale_during_warming_prevents_upload_and_keeps_warming(self):
        worker = self.make_worker()
        self.seed_old_ready()

        def publish(document):
            self.persist_observation(document)
            self.clock["epochId"] = "epoch-two"

        self.store.upsert_observation.side_effect = publish
        self.assertEqual(worker.tick(), "stale")
        self.store.upload_frame.assert_not_called()
        self.assert_warming(self.documents[w.CAMERAS[0]])
        self.assertEqual(self.frames[w.CAMERAS[0]], JPEG)

    def test_final_cosmos_failure_leaves_warming_not_old_ready(self):
        worker = self.make_worker()
        self.seed_old_ready()

        def publish(document):
            if document["observation"]["status"] == "ready":
                raise w.WorkerError("observation_write_failed")
            self.persist_observation(document)

        self.store.upsert_observation.side_effect = publish
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(worker.tick(), "error")
        self.assertEqual(json.loads(output.getvalue()),
                         {"event": "vision_error", "code": "observation_write_failed"})
        self.assert_warming(self.documents[w.CAMERAS[0]])
        self.assertEqual(self.frames[w.CAMERAS[0]], self.extractor.return_value)
        self.assertEqual(self.store.upsert_observation.call_count, 2)
        self.assertEqual(worker.request_done, set())
        self.assertFalse(worker.health.snapshot()[0])

    def test_pipeline_failures_publish_unavailable_null_not_fake_zero(self):
        for stage, code in (
            ("media", "media_unavailable"), ("extraction", "frame_extraction_failed"),
            ("decode", "frame_extraction_failed"), ("model", "model_unavailable"),
            ("inference", "inference_failed"), ("upload", "frame_upload_failed"),
        ):
            with self.subTest(stage=stage):
                worker = self.make_worker(detector_present=stage != "model")
                targets = {"media": self.cache.get, "extraction": self.extractor,
                           "decode": self.decoder, "inference": self.detector.detect,
                           "upload": self.store.upload_frame}
                if stage in targets:
                    targets[stage].side_effect = RuntimeError(SECRET)
                with patch("sys.stdout", new_callable=io.StringIO) as output, \
                        patch("sys.stderr", new_callable=io.StringIO) as errors:
                    self.assertEqual(worker.tick(), "unavailable")
                self.assertEqual(self.store.upsert_observation.call_count, 2 if stage == "upload" else 1)
                observation = self.observation()
                self.assertEqual(observation["status"], "unavailable")
                self.assertIsNone(observation["people"])
                self.assertIsNone(observation["frameUri"])
                self.assertEqual(observation["detections"], [])
                self.assertEqual(observation["error"], code)
                captured = json.dumps(observation) + output.getvalue() + errors.getvalue()
                for fragment in SECRET_FRAGMENTS:
                    self.assertNotIn(fragment, captured)
                self.assertEqual(json.loads(output.getvalue()), {"event": "vision_error", "code": code})
                self.assertNotIn("frameSha256", self.store.upsert_observation.call_args.args[0])
                self.assertEqual(errors.getvalue(), "")
                if stage in ("media", "model"):
                    self.assertIsNone(observation["sourceSeconds"])
                    self.extractor.assert_not_called()
                if stage in ("media", "model", "extraction", "decode"):
                    self.detector.detect.assert_not_called()
                if stage != "upload":
                    self.store.upload_frame.assert_not_called()

    def test_invalid_model_outputs_are_unavailable_without_upload(self):
        for result in (None, {}, (), [None], [{"label": "cat", "score": 1, "box": [0, 0, 1, 1]}],
                       [{"label": "person", "score": math.nan, "box": [0, 0, 1, 1]}],
                       [{"label": "person", "score": 1, "box": [0, 0, 0, 1]}]):
            with self.subTest(result=result):
                worker = self.make_worker()
                self.detector.detect.return_value = result
                self.assertEqual(worker.tick(), "unavailable")
                self.assertEqual(self.observation()["error"], "inference_failed")
                self.assertIsNone(self.observation()["people"])
                self.store.upload_frame.assert_not_called()

    def test_identity_changes_at_every_expensive_boundary_discard_stale_records(self):
        for field, value in (("epochId", "epoch-two"), ("layoutVersion", 4), ("variant", "optimized")):
            for stage in ("download", "extraction", "decode", "inference", "preupload", "warming", "postupload"):
                with self.subTest(field=field, stage=stage):
                    worker = self.make_worker()

                    def mutate(result):
                        def hook(*args):
                            self.clock[field] = value
                            return result
                        return hook

                    targets = {
                        "download": (self.cache.get, self.cache.get.return_value),
                        "extraction": (self.extractor, JPEG), "decode": (self.decoder, self.image),
                        "inference": (self.detector.detect, [PERSON]),
                        "warming": (self.store.upsert_observation, None),
                        "postupload": (self.store.upload_frame, None),
                    }
                    if stage == "preupload":
                        original = worker._observation

                        def observation(*args, **kwargs):
                            result = original(*args, **kwargs)
                            self.clock[field] = value
                            return result

                        with patch.object(worker, "_observation", side_effect=observation):
                            self.assertEqual(worker.tick(), "stale")
                    else:
                        target, result = targets[stage]
                        target.side_effect = mutate(result)
                        self.assertEqual(worker.tick(), "stale")
                    if stage in ("warming", "postupload"):
                        self.store.upsert_observation.assert_called_once()
                        self.assert_warming(self.store.upsert_observation.call_args.args[0])
                    else:
                        self.store.upsert_observation.assert_not_called()
                    if stage == "postupload":
                        self.store.upload_frame.assert_called_once()
                    else:
                        self.store.upload_frame.assert_not_called()
                    if stage == "download":
                        self.extractor.assert_not_called()
                    if stage in ("download", "extraction", "decode"):
                        self.detector.detect.assert_not_called()
                    self.assertEqual(worker.request_done, set())

    def test_stale_failed_inference_does_not_publish_unavailable_for_old_epoch(self):
        worker = self.make_worker()

        def fail(image):
            self.clock["epochId"] = "new-epoch"
            raise RuntimeError(SECRET)

        self.detector.detect.side_effect = fail
        self.assertEqual(worker.tick(), "stale")
        self.assert_no_writes()

    def test_gating_expiry_before_inference_prevents_inference_and_writes(self):
        for mode in ("active", "request"):
            for stage in ("download", "extraction", "decode"):
                with self.subTest(mode=mode, stage=stage):
                    worker = self.make_worker(active=mode == "active",
                                              request=request_document() if mode == "request" else None)
                    self.clock["activeUntil"] = w.isoformat(NOW + timedelta(seconds=30)) if mode == "active" else None
                    target, result = {
                        "download": (self.cache.get, self.cache.get.return_value),
                        "extraction": (self.extractor, JPEG),
                        "decode": (self.decoder, self.image),
                    }[stage]

                    def expire(*args):
                        self.time.advance(30)
                        return result

                    target.side_effect = expire
                    self.assertEqual(worker.tick(), "stale")
                    self.detector.detect.assert_not_called()
                    self.assert_no_writes()

    def test_gating_expiry_during_inference_prevents_all_writes(self):
        for active in (True, False):
            with self.subTest(active=active):
                worker = self.make_worker(active=active, request=None if active else request_document())
                self.clock["activeUntil"] = w.isoformat(NOW + timedelta(seconds=30)) if active else None

                def infer(image):
                    self.time.advance(30)
                    return [PERSON]

                self.detector.detect.side_effect = infer
                self.assertEqual(worker.tick(), "stale")
                self.assert_no_writes()

    def test_new_or_removed_manual_request_discards_inflight_manual_sample(self):
        for replacement in (None, request_document(requestedAt=w.isoformat(NOW + timedelta(seconds=1)))):
            with self.subTest(replacement=replacement):
                worker = self.make_worker(active=False, request=request_document())

                def infer(image):
                    self.time.advance(1)
                    self.request = replacement
                    return [PERSON]

                self.detector.detect.side_effect = infer
                self.assertEqual(worker.tick(), "stale")
                self.assert_no_writes()

    def test_cloud_read_and_upsert_errors_are_sanitized_and_health_not_ready(self):
        for operation, expected, exception in (
            ("read_clock", "clock_unavailable", w.WorkerError("clock_unavailable")),
            ("read_request", "request_unavailable", w.WorkerError("request_unavailable")),
            ("upsert_observation", "observation_write_failed", w.WorkerError("observation_write_failed")),
            ("read_clock", "worker_unavailable", RuntimeError(SECRET)),
            ("read_request", "worker_unavailable", RuntimeError(SECRET)),
            ("upsert_observation", "worker_unavailable", RuntimeError(SECRET)),
        ):
            with self.subTest(operation=operation, expected=expected):
                worker = self.make_worker(active=False, request=request_document())
                getattr(self.store, operation).side_effect = exception
                with patch("sys.stdout", new_callable=io.StringIO) as output, \
                        patch("sys.stderr", new_callable=io.StringIO) as errors:
                    self.assertEqual(worker.tick(), "error")
                self.assertEqual(json.loads(output.getvalue()), {"event": "vision_error", "code": expected})
                self.assertEqual(errors.getvalue(), "")
                for fragment in SECRET_FRAGMENTS:
                    self.assertNotIn(fragment, output.getvalue() + errors.getvalue())
                self.assertFalse(worker.health.snapshot()[0])
                self.assertEqual(worker.health.snapshot()[1]["error"], expected)
                self.assertEqual(worker.request_done, set())
                if operation != "upsert_observation":
                    self.assert_no_work()

    def test_invalid_clock_logs_only_allowlisted_code_and_does_no_work(self):
        worker = self.make_worker()
        self.clock["updatedAt"] = SECRET
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(worker.tick(), "error")
        self.assertEqual(json.loads(output.getvalue()), {"event": "vision_error", "code": "clock_invalid"})
        self.assert_no_work()

    def test_health_does_not_recover_until_cloud_retry_succeeds(self):
        worker = self.make_worker(active=False)
        self.assertFalse(worker.health.snapshot()[0])

        def failing_read():
            self.assertFalse(worker.health.snapshot()[0])
            raise w.WorkerError("clock_unavailable")

        self.store.read_clock.side_effect = failing_read
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(worker.tick(), "error")
            self.assertEqual(worker.tick(), "error")
        self.assertFalse(worker.health.snapshot()[0])
        self.store.read_clock.side_effect = lambda: copy.deepcopy(self.clock)
        self.assertEqual(worker.tick(), "idle")
        self.assertTrue(worker.health.snapshot()[0])


class DetectionValidationTests(ErrorAssertions, unittest.TestCase):
    def test_valid_output_is_returned_unchanged(self):
        for detections in ([], [copy.deepcopy(PERSON)],
                           [{"label": "person", "score": 0, "box": [0, 0, 1, 1]}]):
            self.assertIs(w.validate_detections(detections), detections)

    def test_invalid_scores_boxes_and_shapes_fail_closed(self):
        invalid = [None, (), {}, [None], [{}], [dict(PERSON, label="car")]]
        invalid.extend([dict(PERSON, score=value)] for value in
                       (-1, 1.01, True, None, "0.9", math.nan, math.inf, -math.inf, 10**1000))
        invalid.extend([dict(PERSON, box=box)] for box in
                       (None, {}, "0001", [0, 0, 1], [0, 0, 1, 1, 1], [-0.1, 0, 1, 1],
                        [0, 0, 1.01, 1], [0, 0, 1, 1.01], [0, 0, 0, 1], [0, 1, 1, 0],
                        [0, 0, math.nan, 1], [0, 0, 1, math.inf], [True, 0, 1, 1],
                        [0, "0", 1, 1]))
        for result in invalid:
            with self.subTest(result=result):
                with self.assertRaises(w.WorkerError) as caught:
                    w.validate_detections(result)
                self.assertEqual(caught.exception.code, "inference_failed")


class MediaCacheTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / uuid.uuid4().hex
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.time = FakeTime()
        self.download = Mock(return_value=[b"1234"])
        self.cache = w.MediaCache(self.root, self.download, max_bytes=12, max_item_bytes=4,
                                  max_entries=3, ttl=10, monotonic=self.time.monotonic)

    def assert_bounds(self, cache=None):
        cache = self.cache if cache is None else cache
        self.assertLessEqual(cache.size, cache.max_bytes)
        self.assertLessEqual(len(cache.entries), cache.max_entries)
        self.assertEqual(cache.size, sum(path.stat().st_size for path, _, _ in cache.entries.values()))
        self.assertFalse(list(self.root.glob("*.part")))

    def test_cache_hit_same_path_hashed_name_and_exact_download_limit(self):
        path = self.cache.get(w.CAMERAS[0], "baseline")
        key = "media/camera-denim/baseline.mp4"
        self.assertEqual(path.parent, self.root)
        self.assertEqual(path.name, hashlib.sha256(key.encode()).hexdigest() + ".mp4")
        self.assertEqual(path.read_bytes(), b"1234")
        self.assertEqual(self.cache.get(w.CAMERAS[0], "baseline"), path)
        self.download.assert_called_once_with(key, 4)
        self.assert_bounds()

    def test_ttl_is_absolute_and_expiration_boundary_redownloads(self):
        path = self.cache.get(w.CAMERAS[0], "baseline")
        self.time.advance(9.999)
        self.assertEqual(self.cache.get(w.CAMERAS[0], "baseline"), path)
        self.assertEqual(self.download.call_count, 1)
        self.time.advance(0.001)
        self.download.return_value = [b"new!"]
        self.assertEqual(self.cache.get(w.CAMERAS[0], "baseline").read_bytes(), b"new!")
        self.assertEqual(self.download.call_count, 2)
        self.assert_bounds()

    def test_lru_touch_preserves_recent_entry_and_enforces_count(self):
        self.cache.max_entries = 2
        first = self.cache.get(w.CAMERAS[0], "baseline")
        second = self.cache.get(w.CAMERAS[1], "baseline")
        self.cache.get(w.CAMERAS[0], "baseline")
        third = self.cache.get(w.CAMERAS[2], "baseline")
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(third.exists())
        self.assertEqual(list(self.cache.entries), ["media/camera-denim/baseline.mp4",
                                                    "media/camera-checkout/baseline.mp4"])
        self.assert_bounds()

    def test_byte_reservation_evicts_before_incremental_download(self):
        self.cache.max_bytes = 7
        first = self.cache.get(w.CAMERAS[0], "baseline")
        seen = []

        def chunks(name, maximum):
            self.assertFalse(first.exists())
            self.assertEqual(self.cache.size, 0)
            for chunk in (b"12", b"34"):
                partials = list(self.root.glob("*.part"))
                self.assertEqual(len(partials), 1)
                self.assertLessEqual(self.cache.size + maximum, self.cache.max_bytes)
                seen.append(chunk)
                yield chunk

        self.download.side_effect = chunks
        second = self.cache.get(w.CAMERAS[1], "baseline")
        self.assertEqual(seen, [b"12", b"34"])
        self.assertEqual(second.read_bytes(), b"1234")
        self.assert_bounds()

    def test_total_byte_bound_and_variants_are_separate_cache_keys(self):
        self.cache.max_bytes = 8
        first = self.cache.get(w.CAMERAS[0], "baseline")
        second = self.cache.get(w.CAMERAS[0], "optimized")
        third = self.cache.get(w.CAMERAS[1], "baseline")
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        self.assertEqual(self.cache.size, 8)
        self.assert_bounds()

    def test_oversized_download_stops_consuming_and_removes_partial(self):
        consumed = []

        def chunks(*args):
            for chunk in (b"123", b"45", b"never consumed"):
                consumed.append(chunk)
                yield chunk

        self.download.side_effect = chunks
        self.assertWorkerError("media_unavailable", self.cache.get, w.CAMERAS[0], "baseline")
        self.assertEqual(consumed, [b"123", b"45"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.cache.size, 0)
        self.assertEqual(len(self.cache.entries), 0)

    def test_empty_partial_exception_and_bad_chunks_leave_no_artifacts(self):
        def interrupted(*args):
            yield b"12"
            raise RuntimeError(SECRET)

        for chunks in ([], [b""], [b"", b""], [b"12345"], [b"12", None], [b"12", "text"], interrupted):
            with self.subTest(chunks=chunks):
                self.download.side_effect = chunks if callable(chunks) else None
                self.download.return_value = chunks
                self.assertWorkerError("media_unavailable", self.cache.get, w.CAMERAS[0], "baseline")
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual(self.cache.size, 0)
                self.assertEqual(len(self.cache.entries), 0)
        self.download.side_effect = None
        self.download.return_value = [b"ok"]
        self.assertEqual(self.cache.get(w.CAMERAS[0], "baseline").read_bytes(), b"ok")

    def test_missing_or_size_changed_entry_is_refetched(self):
        for damage in ("missing", "truncated"):
            with self.subTest(damage=damage):
                path = self.cache.get(w.CAMERAS[0], "baseline")
                before = self.download.call_count
                if damage == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"x")
                self.assertEqual(self.cache.get(w.CAMERAS[0], "baseline").read_bytes(), b"1234")
                self.assertEqual(self.download.call_count, before + 1)
                self.assert_bounds()

    def test_startup_cleans_only_owned_hashed_residue(self):
        owned = [self.root / ("a" * 64 + ".mp4"), self.root / ("b" * 64 + ".part")]
        preserved = [self.root / "notes.txt", self.root / ("a" * 63 + ".mp4"),
                     self.root / ("c" * 64 + ".txt"), self.root / "camera-denim.mp4"]
        nested = self.root / "nested"
        nested.mkdir()
        nested_file = nested / ("d" * 64 + ".mp4")
        for path in owned + preserved + [nested_file]:
            path.write_bytes(b"residue")
        cache = w.MediaCache(self.root, self.download, monotonic=self.time.monotonic)
        self.assertTrue(all(not path.exists() for path in owned))
        self.assertTrue(all(path.read_bytes() == b"residue" for path in preserved + [nested_file]))
        self.assertEqual(cache.size, 0)
        self.download.assert_not_called()

    def test_bad_camera_or_variant_rejected_without_download_or_artifacts(self):
        for camera, variant in (
            ("../outside", "baseline"), ("..\\outside", "baseline"), ("camera-denim/../x", "baseline"),
            ("C:\\outside", "baseline"), ("camera-denim.jpg", "baseline"), ("unknown", "baseline"),
            ("camera-denim", "../optimized"), ("camera-denim", "optimized.mp4"),
            ("camera-denim", "https://private.example"), (None, "baseline"),
            ("camera-denim", None), ([], "baseline"), ("camera-denim", {}),
        ):
            with self.subTest(camera=camera, variant=variant):
                with self.assertRaises(w.WorkerError) as caught:
                    self.cache.get(camera, variant)
                self.assertEqual(caught.exception.code, "media_unavailable")
        self.download.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_invalid_limits_are_rejected_before_creating_cache_directory(self):
        for limits in ({"max_bytes": 0}, {"max_item_bytes": 0}, {"max_item_bytes": -1},
                       {"max_item_bytes": 5, "max_bytes": 4}, {"max_entries": 0},
                       {"max_entries": -1}, {"ttl": 0}, {"ttl": -1}):
            with self.subTest(limits=limits):
                destination = self.root / uuid.uuid4().hex
                with self.assertRaises(ValueError):
                    w.MediaCache(destination, self.download, **limits)
                self.assertFalse(destination.exists())


class FFmpegTests(ErrorAssertions, unittest.TestCase):
    def test_command_timeout_seek_and_exact_jpeg_bytes(self):
        path = Path(__file__).parent / "not-created media.mp4"
        with patch.object(w.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=JPEG)) as run:
            self.assertIs(w.extract_jpeg(path, 12.345678), JPEG)
        run.assert_called_once()
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[0], "ffmpeg")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "12.345677")
        self.assertEqual(command[command.index("-i") + 1], str(path))
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertEqual(command[-1], "pipe:1")
        self.assertEqual(command[command.index("-c:v") + 1], "mjpeg")
        for flag in ("-nostdin", "-an", "-sn", "-dn"):
            self.assertIn(flag, command)
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.DEVNULL, "timeout": 20, "check": False})

    def test_seek_rounding_never_advances_past_any_clip_frame_pts(self):
        with patch.object(w.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=JPEG)) as run:
            for index in range(900):
                pts = index / 15
                with self.subTest(frame=index):
                    self.assertIs(w.extract_jpeg("unused", pts), JPEG)
                    command = run.call_args.args[0]
                    seek = float(command[command.index("-ss") + 1])
                    self.assertGreaterEqual(seek, 0)
                    self.assertLessEqual(seek, pts)
                    self.assertLessEqual(pts - seek, 0.0000015)
                    if index:
                        self.assertGreater(seek, (index - 1) / 15)
            self.assertEqual(run.call_count, 900)

    def test_invalid_source_seconds_never_start_ffmpeg(self):
        with patch.object(w.subprocess, "run") as run:
            for source in (-1, 60, 60.01, math.nan, math.inf, -math.inf, None, "0"):
                with self.subTest(source=source):
                    self.assertWorkerError("frame_extraction_failed", w.extract_jpeg, "unused", source)
            run.assert_not_called()

    def test_timeout_missing_binary_nonzero_and_invalid_jpeg_are_sanitized(self):
        with patch.object(w.subprocess, "run") as run:
            for error in (subprocess.TimeoutExpired(SECRET, 20, stderr=SECRET),
                          FileNotFoundError(SECRET), RuntimeError(SECRET)):
                with self.subTest(error=type(error).__name__):
                    run.side_effect = error
                    self.assertWorkerError("frame_extraction_failed", w.extract_jpeg, "unused", 0)
            run.side_effect = None
            for status, data in ((1, JPEG), (0, b""), (0, None), (0, b"not jpeg"),
                                 (0, b"\xff\xd8missing end"), (0, b"missing start\xff\xd9"),
                                 (0, b"\xff\xd8" + b"x" * (4 * 1024 * 1024 - 3) + b"\xff\xd9")):
                with self.subTest(status=status, length=len(data) if data is not None else None):
                    run.return_value = SimpleNamespace(returncode=status, stdout=data)
                    self.assertWorkerError("frame_extraction_failed", w.extract_jpeg, "unused", 0)

    def test_jpeg_byte_limit_is_inclusive(self):
        data = b"\xff\xd8" + b"x" * (4 * 1024 * 1024 - 4) + b"\xff\xd9"
        with patch.object(w.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=data)):
            self.assertIs(w.extract_jpeg("unused", 59.999999), data)

    def test_decoder_loads_exact_bytes_and_converts_to_rgb_without_pillow_dependency(self):
        module = ModuleType("PIL")
        image = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=image)
        manager.__exit__ = Mock(return_value=False)
        module.Image = SimpleNamespace(open=Mock(return_value=manager))
        with patch.dict(sys.modules, {"PIL": module}):
            self.assertIs(w.decode_jpeg(JPEG), image.convert.return_value)
        stream = module.Image.open.call_args.args[0]
        self.assertIsInstance(stream, io.BytesIO)
        self.assertEqual(stream.getvalue(), JPEG)
        image.load.assert_called_once_with()
        image.convert.assert_called_once_with("RGB")
        manager.__exit__.assert_called_once()


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.time = FakeTime()
        self.health = w.Health(self.time.monotonic)

    def test_model_loop_and_fresh_pulse_are_all_required(self):
        self.assertEqual(self.health.snapshot(), (False, {
            "ready": False, "modelReady": False, "loopReady": False, "error": "model_unavailable",
        }))
        self.health.pulse()
        self.assertFalse(self.health.snapshot()[0])
        self.assertEqual(self.health.snapshot()[1]["error"], "model_unavailable")
        self.health.model_ready = True
        self.assertEqual(self.health.snapshot(), (True, {
            "ready": True, "modelReady": True, "loopReady": True, "error": None,
        }))
        self.health.loop_ready = False
        self.assertFalse(self.health.snapshot()[0])

    def test_model_without_any_loop_pulse_is_not_ready(self):
        self.health.model_ready = True
        self.assertFalse(self.health.snapshot()[0])
        self.assertFalse(self.health.snapshot()[1]["loopReady"])

    def test_stall_boundary_and_stopped_worker_are_not_ready(self):
        self.health.model_ready = True
        self.health.pulse()
        self.time.advance(119.999)
        self.assertTrue(self.health.snapshot()[0])
        self.time.advance(0.001)
        self.assertEqual(self.health.snapshot(), (False, {
            "ready": False, "modelReady": True, "loopReady": False, "error": "worker_unavailable",
        }))
        self.health.pulse()
        self.assertTrue(self.health.snapshot()[0])
        self.health.stopped = True
        self.assertFalse(self.health.snapshot()[0])
        self.assertFalse(self.health.snapshot()[1]["loopReady"])

    def test_errors_are_allowlisted_and_successful_pulse_recovers(self):
        self.health.model_ready = True
        for error, expected in (("clock_unavailable", "clock_unavailable"), (SECRET, "worker_unavailable")):
            with self.subTest(error=error):
                self.health.pulse(error)
                ready, payload = self.health.snapshot()
                self.assertFalse(ready)
                self.assertFalse(payload["loopReady"])
                self.assertEqual(payload["error"], expected)
                self.health.pulse()
                self.assertTrue(self.health.snapshot()[0])
                self.assertIsNone(self.health.snapshot()[1]["error"])

    def test_http_health_status_envelope_headers_and_unknown_path_without_socket(self):
        handler_type = w.health_handler(self.health)
        for ready in (False, True):
            self.health.model_ready = ready
            self.health.pulse()
            for path in ("/health", "/healthz", "/unknown"):
                with self.subTest(ready=ready, path=path):
                    handler = object.__new__(handler_type)
                    handler.path = path
                    handler.send_response = Mock()
                    handler.send_header = Mock()
                    handler.end_headers = Mock()
                    handler.wfile = io.BytesIO()
                    handler.do_GET()
                    handler.send_response.assert_called_once_with(
                        404 if path == "/unknown" else 200 if ready else 503)
                    handler.end_headers.assert_called_once_with()
                    if path == "/unknown":
                        self.assertEqual(handler.wfile.getvalue(), b"")
                        continue
                    data = handler.wfile.getvalue()
                    self.assertEqual(json.loads(data), self.health.snapshot()[1])
                    headers = dict(call.args for call in handler.send_header.call_args_list)
                    self.assertEqual(headers, {"Content-Type": "application/json",
                                               "Content-Length": str(len(data)), "Cache-Control": "no-store"})
                    with patch("sys.stderr", new_callable=io.StringIO) as output:
                        handler.log_message(SECRET)
                    self.assertEqual(output.getvalue(), "")

    def test_log_error_never_echoes_untrusted_messages(self):
        for code in (SECRET, "clock_unavailable", "inference_failed"):
            with self.subTest(code=code), patch("sys.stdout", new_callable=io.StringIO) as output:
                w.log_error(code)
                self.assertEqual(json.loads(output.getvalue()), {
                    "event": "vision_error", "code": code if code in w.ERROR_CODES else "worker_unavailable",
                })


class SettingsTests(ErrorAssertions, unittest.TestCase):
    def test_valid_azure_defaults_and_environment_override(self):
        env = valid_environment()
        with patch.dict(w.os.environ, env, clear=True):
            settings = w.Settings.from_env()
        self.assertEqual(settings.client_id, env["AZURE_CLIENT_ID"])
        self.assertEqual(settings.cosmos_endpoint, env["COSMOS_ENDPOINT"])
        self.assertEqual(settings.storage_account, "vitrine123")
        self.assertEqual((settings.cosmos_database, settings.cosmos_container, settings.storage_container),
                         ("vitrine", "retail", "retail"))
        self.assertEqual((settings.interval_seconds, settings.request_ttl_seconds), (5, 30))
        env.update(COSMOS_DATABASE="db_2", COSMOS_CONTAINER="items-2", AZURE_STORAGE_CONTAINER="media",
                   VISION_INTERVAL_SECONDS="1", VISION_REQUEST_TTL_SECONDS="120")
        settings = w.Settings.from_env(env)
        self.assertEqual((settings.cosmos_database, settings.cosmos_container, settings.storage_container),
                         ("db_2", "items-2", "media"))
        self.assertEqual((settings.interval_seconds, settings.request_ttl_seconds), (1, 120))
        env.update(VISION_INTERVAL_SECONDS="60", VISION_REQUEST_TTL_SECONDS="15")
        self.assertEqual(w.Settings.from_env(env).interval_seconds, 60)

    def test_azure_environment_and_user_assigned_identity_are_required_no_local_fallback(self):
        for field in valid_environment():
            env = valid_environment()
            del env[field]
            with self.subTest(missing=field):
                self.assertWorkerError("configuration_invalid", w.Settings.from_env, env)
        for environment in ("local", "Azure", "", None):
            env = valid_environment()
            env["ENVIRONMENT"] = environment
            with self.subTest(environment=environment):
                self.assertWorkerError("configuration_invalid", w.Settings.from_env, env)
        for client in ("", "system-assigned", SECRET, None):
            env = valid_environment()
            env["AZURE_CLIENT_ID"] = client
            with self.subTest(client=client):
                self.assertWorkerError("configuration_invalid", w.Settings.from_env, env)

    def test_endpoint_rejects_nonazure_credentials_tokens_ports_and_paths(self):
        for endpoint in (
            "http://vitrine.documents.azure.com", "https://private.example",
            "https://vitrine.documents.azure.com.attacker.example",
            "https://user:password@vitrine.documents.azure.com",
            "https://vitrine.documents.azure.com?sig=SECRET_TOKEN",
            "https://vitrine.documents.azure.com#SECRET_TOKEN",
            "https://vitrine.documents.azure.com/database",
            "https://vitrine.documents.azure.com:444",
            "https://vitrine.documents.azure.com:bad", "https://[invalid", "", None,
        ):
            with self.subTest(endpoint=endpoint):
                env = valid_environment()
                env["COSMOS_ENDPOINT"] = endpoint
                self.assertWorkerError("configuration_invalid", w.Settings.from_env, env)

    def test_storage_names_and_numeric_environment_settings_are_validated(self):
        invalid = {
            "AZURE_STORAGE_ACCOUNT": ["ab", "a" * 25, "MixedCase", "bad-name", "../path", "", None],
            "COSMOS_DATABASE": ["", "../db", "db/name", "x" * 64, None],
            "COSMOS_CONTAINER": ["", "bad name", "/root", None],
            "AZURE_STORAGE_CONTAINER": ["", "../media", "has space", None],
            "VISION_INTERVAL_SECONDS": ["0", "0.99", "60.01", "nan", "inf", "-inf", "bad", None],
            "VISION_REQUEST_TTL_SECONDS": ["14.99", "120.01", "nan", "inf", "-inf", "bad", None],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    env = valid_environment()
                    env[field] = value
                    self.assertWorkerError("configuration_invalid", w.Settings.from_env, env)


class AzureStoreTests(ErrorAssertions, unittest.TestCase):
    def setUp(self):
        names = ("azure", "azure.cosmos", "azure.cosmos.exceptions", "azure.identity",
                 "azure.storage", "azure.storage.blob", "azure.core")
        modules = {name: ModuleType(name) for name in names}
        for name, module in modules.items():
            module.__path__ = []
            if "." in name:
                parent, child = name.rsplit(".", 1)
                setattr(modules[parent], child, module)
        self.not_found = type("CosmosResourceNotFoundError", (Exception,), {})
        modules["azure.cosmos.exceptions"].CosmosResourceNotFoundError = self.not_found
        self.credential_factory = Mock()
        self.cosmos_factory = Mock()
        self.blob_factory = Mock()
        self.content_settings = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        modules["azure.identity"].ManagedIdentityCredential = self.credential_factory
        modules["azure.cosmos"].CosmosClient = self.cosmos_factory
        modules["azure.storage.blob"].BlobServiceClient = self.blob_factory
        modules["azure.storage.blob"].ContentSettings = self.content_settings
        self.match_condition = object()
        modules["azure.core"].MatchConditions = SimpleNamespace(IfNotModified=self.match_condition)
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.settings = w.Settings.from_env(valid_environment())
        self.store = w.AzureStore(self.settings)
        self.blob = self.store.media.get_blob_client.return_value

    def test_constructor_uses_only_user_assigned_mi_and_existing_cloud_resources(self):
        self.credential_factory.assert_called_once_with(client_id=self.settings.client_id)
        credential = self.credential_factory.return_value
        self.cosmos_factory.assert_called_once_with(
            self.settings.cosmos_endpoint, credential=credential, connection_timeout=5,
            read_timeout=10, retry_total=2, enable_diagnostics_logging=False, logging_enable=False)
        self.blob_factory.assert_called_once_with(
            account_url="https://vitrine123.blob.core.windows.net", credential=credential,
            connection_timeout=5, read_timeout=15, retry_total=2, logging_enable=False)
        self.store.cosmos.get_database_client.assert_called_once_with("vitrine")
        self.store.cosmos.get_database_client.return_value.get_container_client.assert_called_once_with("retail")
        self.store.blobs.get_container_client.assert_called_once_with("retail")
        for client in (self.store.cosmos, self.store.blobs):
            self.assertFalse(any("create" in call[0] for call in client.mock_calls))

    def test_read_clock_and_request_use_fixed_partition_and_missing_request_is_none(self):
        document = clock_document()
        self.store.container.read_item.return_value = document
        self.assertIs(self.store.read_clock(), document)
        self.store.container.read_item.assert_called_with(item="clock", partition_key="store-demo")
        request = request_document()
        self.store.container.read_item.return_value = request
        self.assertIs(self.store.read_request(), request)
        self.store.container.read_item.assert_called_with(item="vision-request", partition_key="store-demo")
        self.store.container.read_item.side_effect = self.not_found(SECRET)
        self.assertIsNone(self.store.read_request())
        self.assertWorkerError("clock_unavailable", self.store.read_clock)

    def test_cloud_read_upsert_and_upload_wrap_raw_exceptions_without_leaking(self):
        self.store.container.read_item.side_effect = RuntimeError(SECRET)
        self.store.container.upsert_item.side_effect = RuntimeError(SECRET)
        self.blob.upload_blob.side_effect = RuntimeError(SECRET)
        with patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertWorkerError("clock_unavailable", self.store.read_clock)
            self.assertWorkerError("request_unavailable", self.store.read_request)
            self.assertWorkerError("observation_write_failed", self.store.upsert_observation, {})
            self.assertWorkerError("frame_upload_failed", self.store.upload_frame,
                                   w.CAMERAS[0], JPEG, str(uuid.uuid4()))
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

    def test_download_is_incremental_bounded_and_etag_pinned(self):
        self.blob.get_blob_properties.return_value = SimpleNamespace(size=4, etag="version-one")
        consumed = []

        def chunks():
            for data in (b"12", b"34"):
                consumed.append(data)
                yield data

        self.blob.download_blob.return_value.chunks.side_effect = chunks
        stream = self.store.download_media("media/camera-denim/baseline.mp4", 4)
        self.assertEqual(consumed, [])
        self.assertEqual(next(stream), b"12")
        self.assertEqual(consumed, [b"12"])
        self.assertEqual(list(stream), [b"34"])
        self.store.media.get_blob_client.assert_called_once_with("media/camera-denim/baseline.mp4")
        self.blob.download_blob.assert_called_once_with(
            etag="version-one", match_condition=self.match_condition, max_concurrency=1)
        self.blob.download_blob.return_value.readall.assert_not_called()

    def test_invalid_declared_size_rejected_before_starting_download(self):
        for size in (0, -1, 5, None):
            with self.subTest(size=size):
                self.blob.get_blob_properties.return_value = SimpleNamespace(size=size, etag="v1")
                self.assertWorkerError("media_unavailable", list, self.store.download_media("media", 4))
        self.blob.download_blob.assert_not_called()

    def test_short_oversized_empty_and_interrupted_streams_fail_closed(self):
        def interrupted():
            yield b"12"
            raise RuntimeError(SECRET)

        for chunks in ([], [b""], [b"12"], [b"12345"], [b"12", b"345"], interrupted):
            with self.subTest(chunks=chunks):
                self.blob.get_blob_properties.return_value = SimpleNamespace(size=4, etag="v1")
                stream = self.blob.download_blob.return_value.chunks
                stream.side_effect = chunks if callable(chunks) else None
                stream.return_value = chunks
                self.assertWorkerError("media_unavailable", list, self.store.download_media("media", 8))

    def test_properties_and_download_failures_are_sanitized(self):
        self.blob.get_blob_properties.side_effect = RuntimeError(SECRET)
        self.assertWorkerError("media_unavailable", list, self.store.download_media("media", 4))
        self.blob.get_blob_properties.side_effect = None
        self.blob.get_blob_properties.return_value = SimpleNamespace(size=4, etag="v1")
        self.blob.download_blob.side_effect = RuntimeError(SECRET)
        self.assertWorkerError("media_unavailable", list, self.store.download_media("media", 4))

    def test_frame_upload_uses_exact_jpeg_and_private_nostore_content_settings(self):
        observation_id = str(uuid.uuid4())
        self.store.upload_frame(w.CAMERAS[0], JPEG, observation_id)
        self.store.media.get_blob_client.assert_called_once_with("frames/camera-denim.jpg")
        self.blob.upload_blob.assert_called_once()
        args, kwargs = self.blob.upload_blob.call_args
        self.assertIs(args[0], JPEG)
        self.assertEqual(len(args), 1)
        self.assertEqual(set(kwargs), {"overwrite", "max_concurrency", "metadata", "content_settings"})
        self.assertEqual(kwargs["metadata"], {"observationid": observation_id})
        self.blob.set_blob_metadata.assert_not_called()
        self.assertTrue(kwargs["overwrite"])
        self.assertEqual(kwargs["max_concurrency"], 1)
        self.assertEqual(vars(kwargs["content_settings"]),
                         {"content_type": "image/jpeg", "cache_control": "private, no-store"})
        document = {"id": "camera-denim", "pk": "vision", "observation": {"status": "unavailable"}}
        self.store.upsert_observation(document)
        self.assertIs(self.store.container.upsert_item.call_args.args[0], document)

    def test_close_attempts_every_client_even_when_one_close_raises(self):
        self.store.blobs.close.side_effect = RuntimeError(SECRET)
        self.store.cosmos.close.side_effect = RuntimeError(SECRET)
        self.store.close()
        for client in (self.store.blobs, self.store.cosmos, self.store.credential):
            client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
