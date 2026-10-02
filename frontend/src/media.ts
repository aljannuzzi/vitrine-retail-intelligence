import type { Camera, Detection, Snapshot, VisionObservation, ZoneId } from "./types";

export const ZONE_NAMES: Record<ZoneId, string> = {
  denim: "Denim",
  essentials: "Essenciais",
  checkout: "Caixa",
};

export function loopSeconds(seconds: number, duration: number): number | null {
  if (!Number.isFinite(seconds) || !Number.isFinite(duration) || duration <= 0) return null;
  return ((seconds % duration) + duration) % duration;
}

export function seekTarget(current: number, source: number, duration: number): number | null {
  const target = loopSeconds(source, duration);
  const position = loopSeconds(current, duration);
  if (target === null || position === null) return null;
  const difference = Math.abs(target - position);
  return Math.min(difference, duration - difference) > 2 ? target : null;
}

/** Allow only the contract's cookie-authenticated, same-origin media routes. */
export function apiAssetUrl(
  uri: string | null,
  cameraId: string,
  kind: "video" | "frame",
  origin: string,
  variant: Camera["variant"] = "baseline",
): string | null {
  if (!uri || !cameraId) return null;
  try {
    const base = new URL(origin);
    const url = new URL(uri, base.origin);
    const id = encodeURIComponent(cameraId);
    const expected = kind === "video"
      ? `/api/media/${id}/${variant}.mp4`
      : `/api/vision/frames/${id}.jpg`;
    if (url.origin !== base.origin || url.username || url.password ||
        url.pathname !== expected || url.search || url.hash) return null;
    return url.pathname;
  } catch {
    return null;
  }
}

export function observationIdentity(vision: VisionObservation): string {
  return JSON.stringify([
    vision.id, vision.cameraId, vision.status, vision.frameUri, vision.sourceSeconds,
    vision.observedAt, vision.model, vision.modelRevision, vision.people,
    vision.latencyMs, vision.detections, vision.error,
  ]);
}

export function matchingObservation(
  snapshot: Snapshot,
  camera: Camera,
  scenario: Snapshot["scenario"],
): boolean {
  if (snapshot.scenario.id !== scenario.id || snapshot.scenario.activeSince !== scenario.activeSince) return false;
  const latest = snapshot.cameras.find((item) => item.id === camera.id);
  return !!latest && latest.variant === camera.variant &&
    latest.mediaUri === camera.mediaUri &&
    observationIdentity(latest.vision) === observationIdentity(camera.vision);
}

export function validDetections(detections: Detection[]): Detection[] {
  return detections.filter(({ label, score, box }) =>
    label === "person" && Number.isFinite(score) && score >= 0 && score <= 1 &&
    box.length === 4 && box.every((value) => Number.isFinite(value) && value >= 0 && value <= 1) &&
    box[2] > box[0] && box[3] > box[1],
  );
}

export function observationAge(observedAt: string | null, serverTime: string): number | null {
  if (!observedAt) return null;
  const age = (Date.parse(serverTime) - Date.parse(observedAt)) / 1000;
  return Number.isFinite(age) && age >= 0 ? age : null;
}

export function sourceTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "Indisponível";
  const tenths = Math.round(seconds * 10);
  const minutes = Math.floor(tenths / 600);
  return `${String(minutes).padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
}

export function denimLayout(snapshot: Snapshot): "baseline" | "optimized" | null {
  const camera = snapshot.cameras.find((item) => item.zoneId === "denim");
  if (camera) return camera.variant;
  return snapshot.interventions.some((item) => item.actionId === "reposition-denim")
    ? "optimized" : null;
}
