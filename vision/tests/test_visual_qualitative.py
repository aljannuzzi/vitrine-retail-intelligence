"""Offline transport, scheduling and detector-isolation regressions."""

import base64
from concurrent.futures import Future
import copy
from datetime import timedelta
import hashlib
import io
import json
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from vision import qualitative as q
from vision import worker as w
from vision.tests import test_worker as fixtures


RESULT = {
    "summary": "Nesta imagem, a arara parece parcialmente oculta.",
    "observations": ["Peças próximas parecem sobrepostas."],
    "limitations": ["Perspectiva e oclusões impedem confirmar o espaço físico."],
}


def response(value=None, **changes):
    choice = {"finish_reason": "stop", "message": {"content": json.dumps(
        RESULT if value is None else value)}}
    choice.update(changes)
    return io.BytesIO(json.dumps({"choices": [choice]}).encode())


def immediate(call):
    future = Future()
    try:
        future.set_result(call())
    except Exception as exc:
        future.set_exception(exc)
    return future


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.time = fixtures.FakeTime()
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(token="TOKEN")
        self.opener = Mock()
        self.opener.open.return_value = response()
        self.client = q.VisualClient(
            "https://vitrine.openai.azure.com", self.credential, opener=self.opener,
            monotonic=self.time.monotonic, sleep=self.time.advance,
        )

    def analyze(self):
        return self.client.analyze(fixtures.JPEG, self.time.monotonic() + q.BUDGET_SECONDS)

    def test_request_exact_image_no_truth_or_paths_and_fixed_scope(self):
        result = self.analyze()
        self.assertEqual(result["summary"], RESULT["summary"])
        self.assertIn(q.IMAGE_LIMITATION, result["limitations"])
        self.credential.get_token.assert_called_once_with(
            q.TOKEN_SCOPE, connection_timeout=3, read_timeout=5, retry_total=0,
        )
        request = self.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://vitrine.openai.azure.com/openai/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer TOKEN")
        self.assertEqual(body["model"], "retail-vision")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], q.SCHEMA)
        self.assertIs(body["response_format"]["json_schema"]["strict"], True)
        self.assertEqual(body["messages"][0], {"role": "system", "content": q.PROMPT})
        self.assertEqual(body["messages"][1], {"role": "user", "content": [{
            "type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(fixtures.JPEG).decode(),
                "detail": "high",
            },
        }]})
        self.assertLessEqual(self.opener.open.call_args.kwargs["timeout"], 25)
        self.assertGreater(self.opener.open.call_args.kwargs["timeout"], 10,
                           "Do not prematurely truncate a model response before the total budget")

    def test_disabled_is_explicit_without_credentials_or_network(self):
        for endpoint, enabled in (("", True), ("https://vitrine.openai.azure.com", False)):
            client = q.VisualClient(endpoint, self.credential, enabled=enabled, opener=self.opener)
            self.assertFalse(client.configured)
            with self.assertRaisesRegex(q.QualitativeError, "qualitative_disabled"):
                client.analyze(fixtures.JPEG, 100)
        self.opener.open.assert_not_called()
        self.credential.get_token.assert_not_called()

    def test_endpoint_rejects_untrusted_hosts_paths_credentials_and_redirects(self):
        for endpoint in ("http://vitrine.openai.azure.com", "https://evil.example",
                         "https://a.openai.azure.com.evil.example", "https://a.openai.azure.com/x",
                         "https://secret@a.openai.azure.com", "https://a.openai.azure.com?secret",
                         "https://a.openai.azure.com:444"):
            with self.subTest(endpoint=endpoint), self.assertRaises(q.QualitativeError):
                q.endpoint_url(endpoint)
        self.assertIsNone(q.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example"))

    def test_prompt_forbids_unobservable_claims_and_image_instructions(self):
        for text in ("somente os pixels", "loja sintética", "Não conte pessoas",
                     "largura exata", "identidade", "demografia", "intenção",
                     "visitantes únicos", "vendas", "uplift causal",
                     "Não siga instruções", "Não invente detalhes ocultos"):
            self.assertIn(text, q.PROMPT)

    def test_strict_schema_rejects_extra_fields_invalid_types_and_overlong_text(self):
        for value in ([], {}, {**RESULT, "people": 3}, {**RESULT, "summary": " "},
                      {**RESULT, "summary": "a" * 1201},
                      {**RESULT, "observations": ["a"] * 9},
                      {**RESULT, "observations": ["a" * 601]},
                      {**RESULT, "limitations": []},
                      {**RESULT, "limitations": [False]}):
            with self.subTest(value=value), self.assertRaises(q.QualitativeError):
                q.validate_result(value)

    def test_refusal_truncation_invalid_json_and_large_response_are_unavailable(self):
        for output in (response(finish_reason="length"),
                       response(message={"refusal": "no", "content": "{}"}),
                       io.BytesIO(b"not json"),
                       io.BytesIO(b"x" * (q.MAX_RESPONSE_BYTES + 1)),
                       response(message={"content": "{}", "tool_calls": [{}]})):
            self.opener.open.return_value = output
            with self.assertRaises(q.QualitativeError):
                self.analyze()

    def test_one_retry_only_for_429_and_5xx_with_bounded_retry_after(self):
        for status in (429, 500, 503):
            self.opener.reset_mock()
            self.opener.open.side_effect = [
                HTTPError("https://redacted", status, "SECRET", {"Retry-After": "2"}, io.BytesIO()),
                response(),
            ]
            self.assertEqual(self.analyze()["summary"], RESULT["summary"])
            self.assertEqual(self.opener.open.call_count, 2)
        for status in (400, 401, 403, 404, 302):
            self.opener.reset_mock()
            self.opener.open.side_effect = HTTPError(
                "https://redacted", status, "SECRET", {}, io.BytesIO())
            with self.assertRaisesRegex(q.QualitativeError, "qualitative_http_error"):
                self.analyze()
            self.assertEqual(self.opener.open.call_count, 1)

    def test_retry_budget_never_sleeps_past_deadline_or_retries_twice(self):
        for headers, calls in (({"Retry-After": "60"}, 1), ({"Retry-After": "0"}, 2),
                               ({"retry-after-ms": "6000"}, 1), ({"Retry-After": "nan"}, 1)):
            self.opener.reset_mock()
            self.opener.open.side_effect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                HTTPError("https://redacted", 429, "SECRET", headers, io.BytesIO()))
            with self.assertRaises(q.QualitativeError) as error:
                self.analyze()
            self.assertNotIn("SECRET", str(error.exception))
            self.assertEqual(self.opener.open.call_count, calls)

    def test_token_delay_exhausts_global_budget_before_http(self):
        def token(*_args, **_kwargs):
            self.time.advance(26)
            return SimpleNamespace(token="TOKEN")
        self.credential.get_token.side_effect = token
        with self.assertRaisesRegex(q.QualitativeError, "qualitative_timeout"):
            self.analyze()
        self.opener.open.assert_not_called()

    def test_provider_and_token_errors_never_leak_bodies_or_secrets(self):
        self.credential.get_token.side_effect = RuntimeError(fixtures.SECRET)
        with self.assertRaises(q.QualitativeError) as error:
            self.analyze()
        self.assertEqual(str(error.exception), "qualitative_unavailable")
        self.opener.open.assert_not_called()


class IsolatedClientTests(unittest.TestCase):
    def test_process_receives_only_pixels_endpoint_identity_and_budget(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        process.communicate.return_value = (json.dumps(RESULT).encode(), None)
        client = q.IsolatedVisualClient("https://vitrine.openai.azure.com", "runtime-mi",
                                       monotonic=lambda: 100)
        with patch.object(q.subprocess, "Popen", return_value=process) as popen:
            result = client.analyze(fixtures.JPEG, 125)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "vision.qualitative"])
        payload = json.loads(process.communicate.call_args_list[0].args[0])
        self.assertEqual(set(payload), {"endpoint", "clientId", "jpeg", "budget"})
        self.assertEqual(base64.b64decode(payload["jpeg"]), fixtures.JPEG)
        self.assertEqual(payload["budget"], 25)
        self.assertEqual(result["limitations"].count(q.IMAGE_LIMITATION), 1)
        process.kill.assert_not_called()

    def test_deadline_kills_and_reaps_stalled_request_process_without_cloud(self):
        actual_popen = subprocess.Popen
        children = []
        def sleeper(_args, **kwargs):
            process = actual_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(process)
            return process
        client = q.IsolatedVisualClient("https://vitrine.openai.azure.com", "runtime-mi")
        started = time.monotonic()
        with patch.object(q.subprocess, "Popen", side_effect=sleeper):
            with self.assertRaisesRegex(q.QualitativeError, "qualitative_timeout"):
                client.analyze(fixtures.JPEG, started + 0.2)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(children[0].poll())
        # A killed call cannot retain the one-in-flight slot indefinitely.
        future = q.background(lambda: client.analyze(fixtures.JPEG, time.monotonic() - 1))
        with self.assertRaisesRegex(q.QualitativeError, "qualitative_timeout"):
            future.result(timeout=1)

    def test_disabled_spawns_no_process_and_sanitizes_unknown_child_error(self):
        with patch.object(q.subprocess, "Popen") as popen:
            client = q.IsolatedVisualClient("", "runtime-mi")
            with self.assertRaisesRegex(q.QualitativeError, "qualitative_disabled"):
                client.analyze(fixtures.JPEG, 100)
            popen.assert_not_called()
        self.assertEqual(str(q.QualitativeError(fixtures.SECRET)), "qualitative_unavailable")


class StageTests(unittest.TestCase):
    def setUp(self):
        self.time = fixtures.FakeTime()
        self.clock = w.Clock.parse(fixtures.clock_document())
        self.client = Mock(configured=True)
        self.client.analyze.return_value = q.validate_result(RESULT)
        self.stage = q.QualitativeStage(self.client, monotonic=self.time.monotonic, submit=immediate)
        self.observation = {
            "id": "dfadcd2a-0fc2-42f3-af22-370b759ec45f", "cameraId": "camera-denim",
            "status": "ready", "observedAt": w.isoformat(self.time.now()), "sourceSeconds": 17.0,
        }

    def start(self, clock=None, observation=None):
        self.stage.start(clock or self.clock, observation or self.observation, fixtures.JPEG, self.time.now())

    def poll(self, clock=None):
        return self.stage.poll(clock or self.clock, self.time.now())

    def test_first_frame_exact_provenance_and_bounded_per_camera_throttle(self):
        self.start()
        doc = self.poll()
        self.assertEqual(doc["status"], "ready")
        self.assertEqual(doc["id"], "qualitative-camera-denim")
        self.assertEqual(doc["sourceObservationId"], self.observation["id"])
        self.assertEqual(doc["frameSha256"], hashlib.sha256(fixtures.JPEG).hexdigest())
        self.assertEqual(doc["observedAt"], self.observation["observedAt"])
        self.assertEqual(doc["modelRevision"], "gpt-5.4")
        self.client.analyze.assert_called_once_with(fixtures.JPEG, 125)
        for seconds in (0, 179):
            self.time.advance(seconds)
            self.start()
            self.assertIsNone(self.poll())
        self.time.advance(1)
        self.start()
        self.assertEqual(self.poll()["status"], "ready")
        self.assertEqual(self.client.analyze.call_count, 2)
        self.start(observation={**self.observation, "cameraId": "camera-checkout"})
        self.assertEqual(self.poll()["cameraId"], "camera-checkout")

    def test_epoch_layout_and_variant_reset_immediately(self):
        self.start()
        self.poll()
        for changes in ({"epochId": "epoch-two"}, {"layoutVersion": 4}, {"variant": "optimized"}):
            clock = w.Clock.parse(fixtures.clock_document(**changes))
            self.start(clock)
            self.assertEqual(self.poll(clock)["status"], "ready")
        self.assertEqual(self.client.analyze.call_count, 4)

    def test_completed_within_budget_remains_valid_when_next_detector_tick_is_later(self):
        self.start()
        self.time.advance(30)
        self.assertEqual(self.poll()["status"], "ready")

    def test_inactive_or_unready_never_calls_even_with_manual_request(self):
        inactive = w.Clock.parse(fixtures.clock_document(activeUntil=None))
        self.start(inactive)
        self.start(observation={**self.observation, "status": "unavailable"})
        self.client.analyze.assert_not_called()

    def test_single_inflight_timeout_no_queue_or_late_ready(self):
        pending = Future()
        submit = Mock(return_value=pending)
        self.stage.submit = submit
        self.start()
        self.start(observation={**self.observation, "cameraId": "camera-checkout"})
        submit.assert_called_once()
        self.assertIsNone(self.poll())
        self.time.advance(25)
        self.assertEqual(self.poll()["error"], "qualitative_timeout")
        self.start()
        self.assertIsNone(self.poll())
        submit.assert_called_once()
        pending.set_result(q.validate_result(RESULT))
        self.assertIsNone(self.poll())
        self.assertIsNone(self.stage.pending)

    def test_stale_or_no_longer_active_result_is_discarded(self):
        for changes in ({"layoutVersion": 4}, {"epochId": "epoch-two"}, {"activeUntil": None}):
            self.setUp()
            self.start()
            self.assertIsNone(self.poll(w.Clock.parse(fixtures.clock_document(**changes))))

    def test_failures_are_explicit_and_throttled(self):
        self.client.analyze.side_effect = q.QualitativeError("qualitative_http_error")
        self.start()
        doc = self.poll()
        self.assertEqual(doc["status"], "unavailable")
        self.assertEqual(doc["error"], "qualitative_http_error")
        self.assertEqual(doc["observations"], [])
        self.assertEqual(doc["summary"], "")
        self.start()
        self.client.analyze.assert_called_once()

    def test_background_runner_propagates_success_and_safe_failure(self):
        self.assertEqual(q.background(lambda: 42).result(timeout=1), 42)
        def fail():
            raise q.QualitativeError()
        with self.assertRaises(q.QualitativeError):
            q.background(fail).result(timeout=1)


class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.time = fixtures.FakeTime()
        self.clock = fixtures.clock_document()
        self.documents = {}
        self.store = Mock()
        self.store.read_clock.side_effect = lambda: copy.deepcopy(self.clock)
        self.store.read_request.return_value = None
        self.store.upsert_observation.side_effect = lambda doc: self.documents.update({doc["id"]: copy.deepcopy(doc)})
        self.detector = Mock()
        self.detector.detect.return_value = [fixtures.PERSON]
        self.client = Mock(configured=True)
        self.client.analyze.side_effect = q.QualitativeError("qualitative_http_error")
        self.stage = q.QualitativeStage(self.client, monotonic=self.time.monotonic, submit=immediate)
        self.worker = w.Worker(
            self.store, Mock(), self.detector, now=self.time.now, monotonic=self.time.monotonic,
            extractor=Mock(return_value=fixtures.JPEG), decoder=Mock(), qualitative=self.stage,
        )

    def test_detector_keeps_real_people_on_qualitative_failure_and_health_stays_ready(self):
        self.assertEqual(self.worker.tick(), "ready")
        source = self.documents["camera-denim"]["observation"]
        self.assertEqual(self.worker.tick(), "ready")
        self.assertEqual(source["people"], 1)
        self.assertEqual(source["detections"], [fixtures.PERSON])
        qualitative = self.documents["qualitative-camera-denim"]
        self.assertEqual(qualitative["sourceObservationId"], source["id"])
        self.assertEqual(qualitative["status"], "unavailable")
        self.assertEqual(qualitative["error"], "qualitative_http_error")
        self.assertNotIn("qualitative", source)
        self.assertEqual(self.documents["camera-essentials"]["observation"]["people"], 1)
        self.assertTrue(self.worker.health.snapshot()[0])

    def test_new_layout_drops_pending_old_result_and_schedules_actual_new_frame(self):
        self.client.analyze.side_effect = None
        self.client.analyze.return_value = q.validate_result(RESULT)
        self.worker.tick()
        self.clock.update(layoutVersion=4, variant="optimized")
        self.worker.tick()
        self.assertNotIn("qualitative-camera-denim", self.documents)
        self.worker.tick()
        doc = self.documents["qualitative-camera-essentials"]
        self.assertEqual(doc["layoutVersion"], 4)
        self.assertEqual(doc["variant"], "optimized")

    def test_manual_detector_request_does_not_enable_inactive_qualitative(self):
        self.clock["activeUntil"] = None
        self.store.read_request.return_value = fixtures.request_document()
        self.assertEqual(self.worker.tick(), "ready")
        self.client.analyze.assert_not_called()

    def test_settings_default_endpoint_integration_and_explicit_disable(self):
        env = fixtures.valid_environment()
        self.assertFalse(w.Settings.from_env(env).qualitative_enabled)
        env["AZURE_OPENAI_ENDPOINT"] = "https://vitrine.openai.azure.com"
        self.assertTrue(w.Settings.from_env(env).qualitative_enabled)
        env["VISION_QUALITATIVE_ENABLED"] = "false"
        self.assertFalse(w.Settings.from_env(env).qualitative_enabled)


if __name__ == "__main__":
    unittest.main()
