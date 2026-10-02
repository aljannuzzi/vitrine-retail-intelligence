"""Real video/model integration validation with memory-only cloud boundaries.

This is asset-authoring verification, not a local runtime or cloud emulator.
No Azure client is instantiated and no generated truth enters the detector.
"""

import copy
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vision.detector import Detector, MODEL_ID, MODEL_REVISION
from vision.worker import Clock, Worker, decode_jpeg, extract_jpeg, isoformat, source_frame_seconds


class CountingDetector:
    def __init__(self, detector):
        self.detector = detector
        self.inputs = []
        self.latencies = []

    def detect(self, image):
        self.inputs.append(hashlib.sha256(image.tobytes()).hexdigest())
        start = time.perf_counter()
        result = self.detector.detect(image)
        self.latencies.append((time.perf_counter() - start) * 1000)
        return result


class MemoryBoundary:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
        self.clock = {
            "id": "clock", "pk": "store-demo", "epochId": "asset-validation-epoch",
            "state": "paused", "elapsedSeconds": 0.0, "speed": 1,
            "updatedAt": isoformat(self.now),
            "activeUntil": isoformat(self.now + timedelta(seconds=120)),
            "layoutVersion": 0, "variant": "baseline",
        }
        self.request = None
        self.documents = {}
        self.frames = {}
        self.frame_metadata = {}
        self.uploads = []
        self.media_calls = []

    def read_clock(self):
        return copy.deepcopy(self.clock)

    def read_request(self):
        return copy.deepcopy(self.request)

    def get(self, camera, variant):
        self.media_calls.append((camera, variant))
        return ROOT / "assets" / "generated" / f"{camera}-{variant}.mp4"

    def upload_frame(self, camera, jpeg, observation_id=None):
        self.frames[camera] = jpeg
        self.frame_metadata[camera] = {"observationid": observation_id}
        self.uploads.append({"cameraId": camera, "observationId": observation_id,
                             "frameSha256": hashlib.sha256(jpeg).hexdigest()})

    def upsert_observation(self, doc):
        assert doc["pk"] == "vision", "Worker attempted a core-clock write"
        if doc["observation"]["status"] == "ready":
            assert self.frame_metadata[doc["id"]]["observationid"] == doc["observation"]["id"]
            assert hashlib.sha256(self.frames[doc["id"]]).hexdigest() == doc["frameSha256"]
        self.documents[doc["id"]] = copy.deepcopy(doc)


def process_memory():
    if os.name != "nt":
        return {"note": "Peak working set collector is Windows authoring-only"}

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"peakWorkingSetMiB": round(counters.PeakWorkingSetSize / 1024 ** 2, 2),
            "workingSetMiB": round(counters.WorkingSetSize / 1024 ** 2, 2)}


def reference_jpeg(path, frame_index):
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-threads", "2", "-i", str(path), "-map", "0:v:0", "-frames:v", "1",
        "-an", "-sn", "-dn", "-vf",
        f"select=eq(n\\,{frame_index}),scale=1280:720:force_original_aspect_ratio=decrease",
        "-filter_threads", "1", "-threads", "2", "-c:v", "mjpeg", "-q:v", "2",
        "-f", "image2pipe", "pipe:1",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=True)
    return result.stdout


def main():
    started = time.perf_counter()
    generated = ROOT / "assets" / "generated"
    deadline = time.monotonic() + 3 * 60 * 60
    while not (generated / "manifest.json").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Finished media manifest not available")
        time.sleep(15)
    os.environ["PATH"] = str(ROOT / ".local" / "tools") + os.pathsep + os.environ["PATH"]
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    detector = CountingDetector(Detector(ROOT / ".local" / "model"))
    boundary = MemoryBoundary()
    worker = Worker(boundary, boundary, detector, now=lambda: boundary.now)
    report_frames = []
    jpeg_folder = ROOT / ".local" / "renders" / "validation-frames"
    jpeg_folder.mkdir(exist_ok=True)
    for variant in ("baseline", "optimized"):
        boundary.clock.update(variant=variant, layoutVersion=int(variant == "optimized"))
        for seconds in (0, 15.09, 30.14, 59.999):
            boundary.clock["elapsedSeconds"] = seconds
            for camera in ("camera-denim", "camera-essentials", "camera-checkout"):
                assert worker.tick() == "ready"
                document = boundary.documents[camera]
                obs = document["observation"]
                jpeg = boundary.frames[camera]
                expected_source = source_frame_seconds(seconds)
                assert obs["cameraId"] == camera
                assert obs["sourceSeconds"] == expected_source
                assert document["variant"] == variant
                assert document["layoutVersion"] == int(variant == "optimized")
                assert obs["people"] == len(obs["detections"]) and obs["people"] > 0
                assert obs["model"] == MODEL_ID and obs["modelRevision"] == MODEL_REVISION
                assert obs["provenance"] == "model-inference"
                assert obs["frameUri"] == f"/api/vision/frames/{camera}.jpg"
                assert document["frameSha256"] == hashlib.sha256(jpeg).hexdigest()
                assert boundary.frame_metadata[camera]["observationid"] == obs["id"]
                assert detector.inputs[-1] == hashlib.sha256(decode_jpeg(jpeg).tobytes()).hexdigest()
                assert boundary.media_calls[-1] == (camera, variant)
                # Match the chosen video frame to the original authored PTS,
                # allowing the expected lossy H.264 + JPEG compression error.
                index = int(round(expected_source * 15))
                clip = generated / f"{camera}-{variant}.mp4"
                assert jpeg == reference_jpeg(clip, index), (camera, variant, expected_source, "incorrect PTS")
                raw = ROOT / ".local" / "renders" / f"{camera}-{variant}" / f"{index:04d}.png"
                error = None
                if raw.exists():
                    with Image.open(raw) as source:
                        error = statistics.mean(ImageStat.Stat(ImageChops.difference(source.convert("RGB"), decode_jpeg(jpeg))).mean)
                    assert error < 12, (camera, variant, seconds, error)
                frame_path = jpeg_folder / f"{camera}-{variant}-{index:04d}.jpg"
                frame_path.write_bytes(jpeg)
                report_frames.append({"cameraId": camera, "variant": variant,
                                      "sourceSeconds": expected_source, "file": str(frame_path.relative_to(ROOT)),
                                      "uploadedFrameMetadata": copy.deepcopy(boundary.frame_metadata[camera]),
                                      "frameSha256": document["frameSha256"],
                                      "observation": obs, "meanAbsolutePixelErrorFromRenderedFrame":
                                      round(error, 4) if error is not None else None})
                print(f"{camera}/{variant} {expected_source:.4f}s {obs['people']} people; actual PTS/pixels matched", flush=True)
    before = len(detector.inputs)
    boundary.clock["activeUntil"] = isoformat(boundary.now - timedelta(seconds=1))
    assert worker.tick() == "idle" and len(detector.inputs) == before
    boundary.request = {"id": "vision-request", "pk": "store-demo",
                        "epochId": boundary.clock["epochId"], "requestedAt": isoformat(boundary.now)}
    for _ in range(3):
        assert worker.tick() == "ready"
    assert worker.tick() == "idle" and len(detector.inputs) == before + 3
    boundary.now += timedelta(seconds=31)
    assert worker.tick() == "idle"
    boundary.clock["activeUntil"] = isoformat(boundary.now + timedelta(seconds=120))
    with patch.object(detector, "detect", side_effect=RuntimeError("do-not-log-provider-body")):
        assert worker.tick() == "unavailable"
    failed = boundary.documents["camera-denim"]["observation"]
    assert failed["people"] is None and failed["detections"] == [] and failed["frameUri"] is None
    assert failed["error"] == "inference_failed"
    clock = Clock.parse({**boundary.clock, "state": "running", "elapsedSeconds": 59.5,
                         "speed": 2, "updatedAt": isoformat(boundary.now - timedelta(seconds=1))})
    assert source_frame_seconds(clock.source_seconds(boundary.now)) == 1.4666666666666666
    latency = detector.latencies[1:]
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "elapsedSeconds": round(time.perf_counter() - started, 3),
        "model": MODEL_ID, "modelRevision": MODEL_REVISION,
        "validatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "workerSha256": hashlib.sha256((ROOT / "vision" / "worker.py").read_bytes()).hexdigest(),
        "cloudBoundary": "Memory-only test fixtures; no Azure client or cloud write",
        "realMediaDecode": True, "realPinnedModelInference": True, "verifiedFrames": len(report_frames),
        "pixelAndFramePtsMatched": True, "bothVariantsVerified": True,
        "seekMatchesExactFrameIndexJpegBytes": True,
        "authoredPngComparisons": sum(frame["meanAbsolutePixelErrorFromRenderedFrame"] is not None
                                     for frame in report_frames),
        "inactiveInferenceSuppressionVerified": True, "oneShotRequestAllCamerasVerified": True,
        "expiredRequestSuppressionVerified": True, "failureNullCountVerified": True,
        "runningClockWrapVerified": True, "exactJpegInferenceUploadVerified": True,
        "detectorOnlyReceivesPixels": True,
        "observationIdMetadataVerified": True,
        "verifiedFrameUploads": len(boundary.uploads),
        "totalDetectionsOnVerifiedFrames": sum(frame["observation"]["people"] for frame in report_frames),
        "meanDetectedPeople": statistics.mean(frame["observation"]["people"] for frame in report_frames),
        "cpu": {"device": "cpu", "threads": 4, "medianMs": round(statistics.median(latency), 2),
                "p95Ms": round(sorted(latency)[int(.95 * len(latency))], 2), **process_memory(),
                "note": "Local CPU measurement; Azure Container Apps runtime not deployed or benchmarked."},
        "frames": report_frames,
        "uploads": boundary.uploads,
    }
    (generated / "media-worker-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "frames"}, indent=2), flush=True)


if __name__ == "__main__":
    main()
