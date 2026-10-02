import test from "node:test";
import assert from "node:assert/strict";
import type { Camera, Snapshot, VisionObservation } from "./types";
import {
  apiAssetUrl, denimLayout, loopSeconds, matchingObservation,
  observationAge, observationIdentity, seekTarget, sourceTime, validDetections,
} from "./media";

const observation: VisionObservation = {
  id: "frame-1", cameraId: "denim", status: "ready",
  observedAt: "2026-10-01T20:00:00Z", sourceSeconds: 8, people: 1,
  detections: [{ label: "person", score: 0.95, box: [0.1, 0.2, 0.3, 0.8] }],
  frameUri: "/api/vision/frames/denim.jpg", latencyMs: 100,
  model: "detector", modelRevision: "revision-1", provenance: "model-inference", error: null,
};
const camera: Camera = {
  id: "denim", name: "Denim", zoneId: "denim", variant: "baseline",
  mediaUri: "/api/media/denim/baseline.mp4", durationSeconds: 20, sourceSeconds: 8,
  footage: "synthetic-3d", vision: observation,
};
const scenario: Snapshot["scenario"] = {
  id: "demo", activeSince: "2026-10-01T20:00:00Z", state: "running",
  elapsedSeconds: 8, speed: 1, layoutVersion: 1, simulation: true, label: "Demo",
};
const snapshot = { cameras: [camera], scenario, interventions: [] } as unknown as Snapshot;

test("cyclic synchronization seeks only when drift exceeds two seconds", () => {
  assert.equal(seekTarget(4, 6, 20), null);
  assert.ok(Math.abs(seekTarget(4, 6.01, 20)! - 6.01) < 1e-9);
  assert.equal(seekTarget(19.5, 0.5, 20), null);
  assert.equal(seekTarget(0.5, 19.5, 20), null);
  assert.equal(seekTarget(15, 0.5, 20), 0.5);
  assert.equal(seekTarget(3, 28, 20), 8);
});

test("invalid media clocks never request a seek", () => {
  for (const duration of [0, -1, Infinity, NaN]) assert.equal(seekTarget(1, 5, duration), null);
  assert.equal(seekTarget(NaN, 5, 20), null);
  assert.equal(seekTarget(1, Infinity, 20), null);
  assert.equal(loopSeconds(-1, 20), 19);
});

test("media URLs stay on exact same-origin contract routes without URL credentials", () => {
  const origin = "https://vitrine.example";
  assert.equal(apiAssetUrl(camera.mediaUri, camera.id, "video", origin), camera.mediaUri);
  assert.equal(apiAssetUrl(`${origin}${observation.frameUri}`, camera.id, "frame", origin), observation.frameUri);
  for (const uri of [
    "https://foreign.example/api/media/denim/baseline.mp4",
    "//foreign.example/api/media/denim/baseline.mp4",
    "https://user:secret@vitrine.example/api/media/denim/baseline.mp4",
    "/api/media/denim/baseline.mp4?token=secret",
    "/api/media/denim/baseline.mp4#fragment",
    "/api/media/denim/optimized.mp4",
    "/assets/denim.mp4",
    "data:video/mp4;base64,abc",
  ]) assert.equal(apiAssetUrl(uri, camera.id, "video", origin), null);
  assert.equal(apiAssetUrl(null, camera.id, "frame", origin), null);
});

test("mutable frame identity changes on source, model, boxes, time and status", () => {
  const original = observationIdentity(observation);
  for (const change of [
    { id: "frame-2" }, { sourceSeconds: 9 }, { modelRevision: "revision-2" },
    { observedAt: null }, { detections: [] }, { status: "unavailable" as const }, { frameUri: null },
  ]) assert.notEqual(observationIdentity({ ...observation, ...change }), original);
});

test("frame verification rejects a new observation, variant and reset scenario", () => {
  assert.equal(matchingObservation(snapshot, camera, scenario), true);
  assert.equal(matchingObservation({ ...snapshot, cameras: [] }, camera, scenario), false);
  assert.equal(matchingObservation({ ...snapshot, cameras: [{ ...camera, variant: "optimized" }] }, camera, scenario), false);
  assert.equal(matchingObservation({ ...snapshot, cameras: [{ ...camera, vision: { ...observation, id: "frame-2" } }] }, camera, scenario), false);
  assert.equal(matchingObservation(snapshot, camera, { ...scenario, activeSince: "2026-10-01T21:00:00Z" }), false);
});

test("only actual valid normalized xyxy detections can be rendered", () => {
  const actual = observation.detections[0];
  assert.deepEqual(validDetections([
    actual, { ...actual, box: [0.4, 0.2, 0.3, 0.8] },
    { ...actual, box: [-0.1, 0.2, 0.3, 0.8] },
    { ...actual, box: [0.1, 0.2, 0.3, 1.1] },
    { ...actual, box: [0.1, NaN, 0.3, 0.8] },
    { ...actual, score: NaN }, { ...actual, score: 1.2 },
  ]), [actual]);
  assert.deepEqual(validDetections([]), []);
});

test("null timestamps and invalid ages remain unavailable rather than fabricated zero", () => {
  assert.equal(sourceTime(null), "Indisponível");
  assert.equal(sourceTime(NaN), "Indisponível");
  assert.equal(sourceTime(0), "00:00.0");
  assert.equal(sourceTime(65.5), "01:05.5");
  assert.equal(sourceTime(59.99), "01:00.0");
  assert.equal(observationAge(null, "2026-10-01T20:00:10Z"), null);
  assert.equal(observationAge("invalid", "2026-10-01T20:00:10Z"), null);
  assert.equal(observationAge(observation.observedAt, "2026-10-01T20:00:10Z"), 10);
  assert.equal(observationAge(observation.observedAt, "2026-10-01T19:00:00Z"), null);
});

test("Denim orientation follows the actual camera variant before intervention fallback", () => {
  const interventions = [{ actionId: "reposition-denim" }] as Snapshot["interventions"];
  assert.equal(denimLayout(snapshot), "baseline");
  assert.equal(denimLayout({ ...snapshot, cameras: [{ ...camera, variant: "optimized" }] }), "optimized");
  assert.equal(denimLayout({ ...snapshot, interventions }), "baseline");
  assert.equal(denimLayout({ ...snapshot, cameras: [], interventions }), "optimized");
  assert.equal(denimLayout({ ...snapshot, cameras: [] }), null);
});
