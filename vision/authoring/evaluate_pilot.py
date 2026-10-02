"""Offline authoring acceptance test. The detector receives rendered RGB only."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import time

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vision.detector import Detector, MODEL_ID, MODEL_REVISION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", default=str(ROOT / ".local" / "renders" / "pilot" / "frames.json"))
    parser.add_argument("--output", default="pilot-report")
    args = parser.parse_args()
    records = json.loads(Path(args.frames).read_text(encoding="utf-8"))
    detector = Detector(ROOT / ".local" / "model")
    results, thumbnails, latencies = [], [], []
    for i, record in enumerate(records):
        path = ROOT / record["file"]
        with Image.open(path) as im:
            image = im.convert("RGB")
        start = time.perf_counter()
        detections = detector.detect(image)
        elapsed = (time.perf_counter() - start) * 1000
        latencies.append(elapsed)
        results.append({**record, "people": len(detections), "detections": detections,
                        "latencyMs": round(elapsed, 2), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        draw = ImageDraw.Draw(image)
        for detection in detections:
            x1, y1, x2, y2 = detection["box"]
            box = (x1 * image.width, y1 * image.height, x2 * image.width, y2 * image.height)
            draw.rectangle(box, outline="#20ffc2", width=3)
            draw.text((box[0], box[1] - 13), f"person {detection['score']:.2f}", fill="#20ffc2", stroke_width=1)
        image.thumbnail((480, 270))
        thumb = Image.new("RGB", (480, 300), "#0c1820")
        thumb.paste(image, (0, 30))
        ImageDraw.Draw(thumb).text((8, 8), f"{record['cameraId']} / {record['variant']} / {record['sourceSeconds']:g}s / {len(detections)} detections",
                                  fill="white")
        thumbnails.append(thumb)
        print(f"{i + 1}/{len(records)} {record['cameraId']} {len(detections)} people {elapsed:.0f}ms", flush=True)
    counts = [r["people"] for r in results]
    warm = latencies[1:] or latencies
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provenance": "actual-model-inference-on-original-synthetic-3d-renders",
        "model": MODEL_ID, "modelRevision": MODEL_REVISION, "threshold": detector.threshold,
        "frames": len(records), "framesWithPerson": sum(c > 0 for c in counts),
        "totalDetections": sum(counts), "minPeople": min(counts), "maxPeople": max(counts),
        "meanPeople": statistics.mean(counts),
        "confidenceRange": [min(d["score"] for r in results for d in r["detections"]),
                            max(d["score"] for r in results for d in r["detections"])],
        "cpuBenchmark": {
            "host": platform.platform(), "python": platform.python_version(),
            "torch": detector._torch.__version__, "device": "cpu",
            "intraOpThreads": detector._torch.get_num_threads(),
            "firstFrameMs": round(latencies[0], 2), "warmMedianMs": round(statistics.median(warm), 2),
            "warmP95Ms": round(sorted(warm)[min(len(warm) - 1, int(len(warm) * .95))], 2),
            "note": "Local CPU authoring benchmark with four threads, not an Azure Container Apps performance guarantee.",
        },
        "limitations": [
            "Detection totals are model outputs, not hand annotations or unique visitors.",
            "Adjacent zones may be visible in a camera; do not sum cameras as unique people.",
            "This pilot establishes person recognition, not precision/recall; no independently labeled ground truth.",
            "No tracking, identity, demographics, emotion, or inferred dwell.",
        ],
        "results": results,
    }
    generated = ROOT / "assets" / "generated"
    (generated / (args.output + ".json")).write_text(json.dumps(report, indent=2), encoding="utf-8")
    sheet = Image.new("RGB", (480 * 3, 300 * ((len(thumbnails) + 2) // 3)), "#0c1820")
    for i, image in enumerate(thumbnails):
        sheet.paste(image, ((i % 3) * 480, (i // 3) * 300))
    sheet.save(generated / (args.output + ".jpg"), quality=90)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    if report["frames"] < 20 or report["framesWithPerson"] < report["frames"] * .9:
        raise SystemExit("Pilot did not meet minimum person-recognition gate")


if __name__ == "__main__":
    main()
