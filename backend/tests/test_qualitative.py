import json
import unittest
from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient

from retail.app import create_app
from retail.engine import (Engine, ObservationBatch, authorized_observations,
                           authorized_qualitative)
from retail import foundry
from retail.models import QualitativeObservation, qualitative_doc_id
from retail.simulation import CAMERAS, iso
from retail.storage import LocalRepository
from test_backend import (Clock, FakeFoundry, FakeMedia, ORIGIN, PASSWORD, START,
                          finish_runs, ready_observation, settings)


def qualitative_doc(state, *, camera=CAMERAS[0], source_id=None, observed_at=None, **changes):
    source_id = source_id or str(uuid4())
    observed_at = iso(START) if observed_at is None else observed_at
    value = {
        "id": qualitative_doc_id(camera),
        "pk": "vision",
        "cameraId": camera,
        "epochId": state["epochId"],
        "layoutVersion": state["layoutVersion"],
        "variant": "optimized" if state["interventions"] else "baseline",
        "sourceObservationId": source_id,
        "sourceSeconds": 12.5,
        "observedAt": observed_at,
        "frameSha256": "a" * 64,
        "model": "retail-vision",
        "modelRevision": "gpt-5.4",
        "version": 1,
        "provenance": "model-inference",
        "source": "synthetic-store-video",
        "configured": True,
        "status": "ready",
        "error": None,
        "summary": "Visibilidade parcial da arara e obstrução aparente do corredor.",
        "observations": ["Peças frontais parecem comprimidas na arara."],
        "limitations": ["Leitura restrita ao quadro amostrado do vídeo sintético."],
    }
    value.update(changes)
    return value


class QualitativeApiTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.repo = LocalRepository(":memory:")
        self.foundry = FakeFoundry(available=True)
        self.media = FakeMedia()
        self.app = create_app(settings(), self.repo, self.foundry, self.media,
                              now=self.clock, ticker=False)
        self.client = self.enterContext(TestClient(self.app, base_url=ORIGIN))

    def login(self):
        response = self.client.post("/api/session", json={"password": PASSWORD},
                                    headers={"Origin": ORIGIN})
        self.assertEqual(response.status_code, 200, response.text)

    def write(self, path, data=None, method="POST"):
        return self.client.request(method, path, json=data, headers={"Origin": ORIGIN})

    def read_document(self, id, pk="store-demo"):
        return self.client.portal.call(self.repo.read_document, id, pk)

    def put_observation(self, observation, **metadata):
        state = self.read_document("state")
        self.client.portal.call(self.repo.write_document, {
            "id": observation["cameraId"],
            "pk": "vision",
            "epochId": state["epochId"],
            "layoutVersion": state["layoutVersion"],
            "observation": observation,
            **metadata,
        })

    def put_qualitative(self, payload):
        self.client.portal.call(self.repo.write_document, payload)

    def run_context(self):
        response = self.write("/api/agents/run",
                              {"agent": "merchandising", "question": "Avalie apenas as evidências disponíveis."})
        self.assertEqual(response.status_code, 202, response.text)
        self.client.portal.call(finish_runs, self.app.state.engine)
        return self.foundry.calls[-1][2]

    def test_invalid_or_unavailable_qualitative_docs_are_ignored(self):
        self.login()
        state = self.read_document("state")
        source_id = str(uuid4())
        cases = {
            "malformed": lambda doc: {**doc, "frameSha256": "XYZ"},
            "stale": lambda doc: {**doc, "observedAt": iso(self.clock() - 301)},
            "future": lambda doc: {**doc, "observedAt": iso(self.clock() + 1)},
            "naive": lambda doc: {**doc, "observedAt": "2026-10-02T09:40:31"},
            "crosslayout": lambda doc: {**doc, "layoutVersion": doc["layoutVersion"] + 1},
            "crossepoch": lambda doc: {**doc, "epochId": "stale-epoch"},
            "variant": lambda doc: {**doc, "variant": "optimized"},
            "disabled": lambda doc: {
                **doc,
                "configured": False,
                "status": "unavailable",
                "error": "qualitative_disabled",
                "summary": "",
                "observations": [],
            },
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                self.foundry.calls.clear()
                detector = {**ready_observation(now=self.clock()), "id": source_id}
                self.put_observation(detector)
                self.put_qualitative(mutate(qualitative_doc(state, source_id=source_id,
                                                            observed_at=iso(self.clock()))))
                self.assertEqual(self.client.get("/api/snapshot").json()["cameras"][0]["vision"], detector)
                context = self.run_context()
                evidence = next(item for item in context["evidence"] if item["id"] == source_id)
                self.assertEqual(evidence["kind"], "model-inference")
                self.assertNotIn("qualitative", evidence)
                self.clock.advance(61)

    def test_qualitative_uses_real_source_id_even_when_detector_has_newer_observation(self):
        self.login()
        state = self.read_document("state")
        latest_id = str(uuid4())
        source_id = str(uuid4())
        self.put_observation({**ready_observation(now=self.clock()), "id": latest_id})
        self.put_qualitative(qualitative_doc(state, source_id=source_id, observed_at=iso(self.clock())))
        context = self.run_context()
        evidence = {item["id"]: item for item in context["evidence"]}
        self.assertEqual(evidence[latest_id]["kind"], "model-inference")
        self.assertEqual(evidence[source_id]["kind"], "visual-qualitative")
        self.assertEqual(evidence[source_id]["qualitative"]["sourceObservationId"], source_id)

    def test_duplicate_source_observation_id_merges_detector_and_qualitative_evidence(self):
        self.login()
        state = self.read_document("state")
        source_id = str(uuid4())
        self.put_observation({**ready_observation(now=self.clock()), "id": source_id})
        self.put_qualitative(qualitative_doc(state, source_id=source_id, observed_at=iso(self.clock())))
        context = self.run_context()
        evidence = [item for item in context["evidence"] if item["id"] == source_id]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["kind"], "visual-qualitative")
        self.assertIn("observation", evidence[0])
        self.assertIn("qualitative", evidence[0])
        # The two models share a source frame, never a model identity.
        evidence[0]["qualitative"]["sourceSeconds"] = evidence[0]["observation"]["sourceSeconds"]
        serialized, ids, _ = foundry._context(context)
        merged = next(item for item in json.loads(serialized)["evidence"] if item["id"] == source_id)
        self.assertEqual(merged["observation"]["model"], ready_observation()["model"])
        self.assertEqual(merged["qualitative"]["model"], "retail-vision")
        self.assertIn(source_id, ids)


class QualitativeEngineTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_snapshot_authority_race_removes_stale_qualitative_context(self):
        state = self.engine.state
        source_id = str(uuid4())
        batch = ObservationBatch(
            {CAMERAS[0]: {**ready_observation(now=self.clock()), "id": source_id}},
            state["epochId"],
            state["layoutVersion"],
            qualitative={CAMERAS[0]: qualitative_doc(state, source_id=source_id, observed_at=iso(self.clock()))},
        )
        async with self.engine.transaction() as working:
            working["layoutVersion"] += 1
        await self.engine.run_agent("merchandising", "Use somente o retrato atual.", batch)
        await finish_runs(self.engine)
        context = self.foundry.calls[-1][2]
        self.assertEqual(context["observations"], [])
        self.assertFalse(any(item.get("kind") == "visual-qualitative" for item in context["evidence"]))

    async def test_unavailable_qualitative_status_is_not_treated_as_ready(self):
        state = self.engine.state
        source_id = str(uuid4())
        batch = ObservationBatch(
            {CAMERAS[0]: {**ready_observation(now=self.clock()), "id": source_id}},
            state["epochId"],
            state["layoutVersion"],
            qualitative={CAMERAS[0]: qualitative_doc(
                state,
                source_id=source_id,
                observed_at=iso(self.clock()),
                configured=False,
                status="unavailable",
                error="qualitative_disabled",
                summary="",
                observations=[],
            )},
        )
        await self.engine.run_agent("merchandising", "Considere só evidência pronta.", batch)
        await finish_runs(self.engine)
        evidence = next(item for item in self.foundry.calls[-1][2]["evidence"] if item["id"] == source_id)
        self.assertEqual(evidence["kind"], "model-inference")
        self.assertNotIn("qualitative", evidence)

    async def test_authorized_observations_preserves_empty_batch(self):
        state = self.engine.state
        values = ObservationBatch({}, state["epochId"], state["layoutVersion"])
        authorized = authorized_observations(values, state)
        self.assertIsInstance(authorized, ObservationBatch)
        self.assertEqual(dict(authorized), {})
        self.assertEqual(authorized.qualitative, {})

    async def test_authorized_qualitative_ignores_camera_mismatch_and_malformed_mapping(self):
        state = self.engine.state
        source_id = str(uuid4())
        malformed = ObservationBatch({}, state["epochId"], state["layoutVersion"], qualitative="bad")
        self.assertEqual(authorized_qualitative(malformed, state, self.clock()), [])
        mismatched = ObservationBatch(
            {},
            state["epochId"],
            state["layoutVersion"],
            qualitative={CAMERAS[0]: qualitative_doc(
                state,
                camera=CAMERAS[1],
                source_id=source_id,
                observed_at=iso(self.clock()),
            )},
        )
        self.assertEqual(authorized_qualitative(mismatched, state, self.clock()), [])


class QualitativeFoundryTests(unittest.TestCase):
    def test_context_bounds_qualitative_payload_and_preserves_true_ids(self):
        source_id = str(uuid4())
        state = {"epochId": "epoch-1", "layoutVersion": 1, "interventions": []}
        payload = {
            "epochId": "epoch-1",
            "evidence": [{
                "id": source_id,
                "kind": "visual-qualitative",
                "provenance": "model-inference",
                "qualitative": qualitative_doc(
                    state,
                    camera=CAMERAS[0],
                    source_id=source_id,
                    observed_at=iso(START),
                    frameSha256="b" * 64,
                    summary="S" * 50,
                    observations=[f"obs-{index}" for index in range(8)],
                    limitations=[f"lim-{index}" for index in range(8)],
                ),
            }],
            "availableActions": [{"id": "reposition-denim", "title": "Reposicionar", "applied": False}],
            "metrics": {"provenance": "simulated-pos"},
            "zones": [],
            "experiment": {"status": "not-started"},
            "interventions": [],
            "observations": [deepcopy(ready_observation()) for _ in range(25)],
        }
        serialized, evidence_ids, _ = foundry._context(payload)
        context = json.loads(serialized)
        self.assertEqual(evidence_ids, {source_id})
        self.assertEqual(len(context["observations"]), foundry.MAX_OBSERVATIONS)
        self.assertEqual(len(context["evidence"][0]["qualitative"]["observations"]), 8)
        self.assertEqual(len(context["evidence"][0]["qualitative"]["limitations"]), 8)

    def test_context_rejects_invalid_shape_max_bytes_and_source_mismatches(self):
        source_id = str(uuid4())

        def payload():
            return {
                "epochId": "epoch-1",
                "evidence": [{
                    "id": source_id,
                    "kind": "visual-qualitative",
                    "provenance": "model-inference",
                    "observation": {
                        "id": source_id,
                        "cameraId": CAMERAS[0],
                        "status": "ready",
                        "observedAt": iso(START),
                        "sourceSeconds": 12.5,
                        "people": 1,
                        "detections": [{"label": "person", "score": 0.9, "box": [0.1, 0.1, 0.2, 0.2]}],
                        "latencyMs": 50.0,
                        "model": "PekingU/rtdetr_v2_r50vd",
                        "modelRevision": "revision",
                        "provenance": "model-inference",
                        "error": None,
                    },
                    "qualitative": qualitative_doc({"epochId": "epoch-1", "layoutVersion": 1, "interventions": []},
                                                   camera=CAMERAS[0], source_id=source_id, observed_at=iso(START)),
                }],
                "availableActions": [{"id": "reposition-denim", "title": "Reposicionar", "applied": False}],
                "metrics": {"provenance": "simulated-pos"},
                "zones": [],
                "experiment": {"status": "not-started"},
                "interventions": [],
                "observations": [],
            }

        foundry._context(payload())
        cases = {
            "invalid-shape": lambda value: value["evidence"][0].__setitem__("qualitative", "bad"),
            "naive-date": lambda value: value["evidence"][0]["qualitative"].__setitem__("observedAt", "2026-10-02T09:40:31"),
            "source-id-mismatch": lambda value: value["evidence"][0].__setitem__("id", str(uuid4())),
            "merged-id-mismatch": lambda value: value["evidence"][0]["observation"].__setitem__("id", str(uuid4())),
            "merged-camera-mismatch": lambda value: value["evidence"][0]["observation"].__setitem__("cameraId", CAMERAS[1]),
            "oversize": lambda value: value["metrics"].__setitem__("text", "x" * foundry.MAX_CONTEXT_BYTES),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                value = payload()
                mutate(value)
                with self.assertRaises(foundry.FoundryError):
                    foundry._context(value)

    def test_prompt_explicitly_restricts_qualitative_inference_scope(self):
        for text in ("visual-qualitative", "substitua ausência de evidência visual",
                     "visitantes únicos", "uplift causal", "Não estime largura física",
                     "não a substitua por contagens", "cite os evidenceIds reais usados"):
            with self.subTest(text=text):
                self.assertIn(text, foundry.COMMON_INSTRUCTIONS)

    def test_detector_detection_truncation_never_changes_real_people_count(self):
        observation = ready_observation()
        observation["people"] = 40
        observation["detections"] = [deepcopy(observation["detections"][0]) for _ in range(40)]
        for length in (40, 32):
            observation["detections"] = observation["detections"][:length]
            for trim in (foundry._trim_visual_observation, foundry._trim_context_observation):
                result = trim(observation)
                self.assertEqual(result["people"], 40)
                self.assertEqual(len(result["detections"]), 32)

    def test_qualitative_schema_accepts_ready_and_disabled_contracts(self):
        state = {"epochId": "epoch-1", "layoutVersion": 1, "interventions": []}
        ready = qualitative_doc(state, source_id=str(uuid4()))
        disabled = qualitative_doc(
            state,
            source_id=str(uuid4()),
            configured=False,
            status="unavailable",
            error="qualitative_disabled",
            summary="",
            observations=[],
        )
        self.assertEqual(QualitativeObservation.model_validate(ready).model_dump(), ready)
        self.assertEqual(QualitativeObservation.model_validate(disabled).model_dump(), disabled)

    def test_actual_worker_stage_document_roundtrips_to_agent_context_without_torch(self):
        from concurrent.futures import Future
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from vision.qualitative import QualitativeStage, validate_result

        clock = SimpleNamespace(epoch_id="epoch-1", layout_version=1, variant="baseline",
                                identity=("epoch-1", 1, "baseline"), active=lambda _: True)
        observation = {**ready_observation(), "id": str(uuid4())}
        def submit(call):
            future = Future()
            future.set_result(call())
            return future
        client = SimpleNamespace(configured=True, analyze=lambda *_: validate_result({
            "summary": "A arara parece parcialmente oculta nesta imagem.",
            "observations": ["Peças parecem sobrepostas."],
            "limitations": ["Uma imagem de vídeo sintético, não medidas físicas."],
        }))
        stage = QualitativeStage(client, monotonic=lambda: 100, submit=submit)
        now = datetime.fromtimestamp(START, timezone.utc)
        stage.start(clock, observation, b"\xff\xd8actual-sampled-bytes\xff\xd9", now)
        doc = stage.poll(clock, now)
        QualitativeObservation.model_validate(doc)
        payload = {
            "epochId": clock.epoch_id,
            "evidence": [{
                "id": observation["id"], "kind": "visual-qualitative",
                "provenance": "model-inference", "observation": observation, "qualitative": doc,
            }],
            "availableActions": [], "metrics": {}, "zones": [],
            "experiment": {}, "interventions": [], "observations": [observation],
        }
        serialized, ids, _ = foundry._context(payload)
        evidence = json.loads(serialized)["evidence"][0]
        self.assertEqual(ids, {observation["id"]})
        self.assertEqual(evidence["qualitative"]["sourceObservationId"], observation["id"])
        self.assertNotEqual(evidence["observation"]["model"], evidence["qualitative"]["model"])

    def test_qualitative_schema_rejects_loose_hash_bool_version_and_missing_error(self):
        state = {"epochId": "epoch-1", "layoutVersion": 1, "interventions": []}
        cases = {
            "hash-suffix": {"frameSha256": ("a" * 64) + "ff"},
            "bool-version": {"version": True},
            "missing-error": {
                "configured": True,
                "status": "unavailable",
                "error": None,
                "summary": "",
                "observations": [],
            },
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(Exception):
                    QualitativeObservation.model_validate(qualitative_doc(state, source_id=str(uuid4()), **changes))

    def test_validate_advice_accepts_real_uuid_and_rejects_unknown_evidence(self):
        source_id = str(uuid4())
        payload = {
            "summary": "Avaliação qualitativa disponível.",
            "recommendations": [{
                "actionId": "reposition-denim",
                "title": "Reposicionar denim",
                "rationale": "A apresentação observada pede ajuste visual.",
                "evidenceIds": [source_id],
                "caveats": ["Sem prova causal de vendas."],
                "priority": "medium",
                "requiresApproval": True,
            }],
            "limitations": ["Amostra sintética e parcial."],
        }
        result = foundry._validate_advice(json.dumps(payload), {source_id}, {"reposition-denim"})
        self.assertEqual(result["recommendations"][0]["evidenceIds"], [source_id])
        payload["recommendations"][0]["evidenceIds"] = [source_id, "unknown-evidence"]
        with self.assertRaises(foundry.FoundryError):
            foundry._validate_advice(json.dumps(payload), {source_id}, {"reposition-denim"})

    def test_persisted_instructions_require_actual_qualitative_evidence(self):
        instructions = foundry.COMMON_INSTRUCTIONS
        for text in (
            "evidenceIds existentes",
            "não a substitua por contagens",
        ):
            with self.subTest(text=text):
                self.assertIn(text, instructions)


if __name__ == "__main__":
    unittest.main()
