"""Produce a machine-readable handoff after all real validation has finished."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "assets" / "generated"


def read(name):
    return json.loads((GENERATED / name).read_text(encoding="utf-8"))


def main():
    media = read("manifest.json")
    pilot = read("pilot-report.json")
    integration = read("media-worker-validation.json")
    scene = read("scene-validation.json")
    assert len(media["videos"]) == 6
    assert integration["verifiedFrames"] == 24
    assert integration["seekMatchesExactFrameIndexJpegBytes"]
    assert scene["sharedActorTimelineVerified"]
    test = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover",
                           "-s", str(ROOT / "vision" / "tests"), "-p", "test*.py"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    if test.returncode:
        raise RuntimeError(test.stdout + test.stderr)
    total = int(re.search(r"Ran (\d+) tests", test.stderr).group(1))
    owned = [str(path.relative_to(ROOT)) for path in (ROOT / "vision").rglob("*")
             if path.is_file() and "__pycache__" not in path.parts and ".cache" not in path.parts]
    delivery = {
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "ownedFiles": sorted(owned),
        "ownedMediaDirectory": "assets\\generated",
        "ownedAuthoringDirectories": [
            ".local\\tools", ".local\\authoring-python", ".local\\human-assets",
            ".local\\huggingface", ".local\\model", ".local\\renders", ".local\\validation-frames",
        ],
        "otherComponentsChanged": False, "azureWritesPerformed": False, "gitCommitsCreated": False,
        "runtime": "Azure-only managed-identity worker; local execution is explicitly authoring and test code only",
        "renderArtifacts": media["videos"],
        "renderSummary": {"videos": 6, "durationEachSeconds": 60, "width": 1280, "height": 720,
                          "fps": 15, "nativeFramesEach": 900, "totalNativeFrames": 5400,
                          "bytes": sum(video["bytes"] for video in media["videos"]),
                          "layoutChange": "Physical denim rack rotates by 83 degrees; nine adults have identical paths and poses in both variants."},
        "realPilot": {key: pilot[key] for key in (
            "frames", "framesWithPerson", "totalDetections", "minPeople", "maxPeople",
            "confidenceRange", "model", "modelRevision", "threshold", "cpuBenchmark")},
        "realMp4WorkerValidation": {key: value for key, value in integration.items() if key != "frames"},
        "unitTestsPassed": total,
        "commandsFromProjectRoot": {
            "tests": ".\\.local\\authoring-python\\Scripts\\python.exe -B -m unittest discover -s vision\\tests -p test*.py",
            "renderPilot": ".\\.local\\tools\\blender-4.5.3-windows-x64\\blender.exe --background --factory-startup --python vision\\authoring\\render_store.py -- --pilot",
            "evaluatePilot": ".\\.local\\authoring-python\\Scripts\\python.exe vision\\authoring\\evaluate_pilot.py",
            "renderOneLoop": ".\\.local\\tools\\blender-4.5.3-windows-x64\\blender.exe --background --factory-startup --python vision\\authoring\\render_store.py -- --camera denim --variant baseline",
            "renderOneLoopNote": "Repeat for denim, essentials and checkout, with baseline and optimized; all six are already supplied.",
            "encodeAndVerify": ".\\.local\\authoring-python\\Scripts\\python.exe vision\\authoring\\finalize_media.py",
            "verifyRealMp4AndWorker": ".\\.local\\authoring-python\\Scripts\\python.exe vision\\authoring\\validate_media_worker.py",
            "imageBuild": "docker build -f vision\\Dockerfile -t vitrine-vision:demo .",
        },
        "expectedImage": {
            "tag": "vitrine-vision:demo", "python": "3.12", "cpuTorch": "2.7.1+cpu",
            "weights": "Pinned revision baked at build; verified SHA-256 local artifacts; offline model loads at runtime",
            "resources": {"cpu": 4, "memoryGiB": 8, "replicas": 1},
            "entrypoint": "python -m vision.worker", "readiness": "HTTP :8081/healthz",
            "buildVerified": False, "buildLimitation": "Docker CLI exists but the Docker Desktop Linux daemon was unavailable. Image build and live Azure MI access were not exercised.",
            "privateFilesExcluded": "vision\\Dockerfile.dockerignore prevents local credentials, tools, source meshes and authoring caches entering this build context",
        },
        "integration": {
            "cloudSettings": "vision\\run-config.json",
            "blobSeeding": "Each manifest videos[*].blobName is the authorized private Blob target; this task does not upload.",
            "clock": "Read-only store-demo/clock; never mutate the backend's clock.",
            "framePublication": "Warming fence before JPEG replacement, then ready observation with frameSha256 and epoch/layout/variant envelope.",
            "fixedUriCaveat": "A client can retain old detections while fetching a later fixed-URI JPEG. Atomic cross-request reads require backend validation or versioned URLs; no backend changes were made.",
            "qualitativeGpt": "Optional direct GPT qualitative vision omitted; backend hosted agents remain separate.",
        },
        "truthfulness": [
            "Model reports visible person boxes/counts only, not unique visitors, demographics, emotions, dwell or cross-camera identity.",
            "Pilot verifies person recognition, not precision/recall; there is no independently labeled ground truth.",
            "CPU timings and memory are real local measurements with four Torch threads, not Azure performance claims.",
            "Raw renderer state is never supplied to Detector; exact analyzed JPEG bytes are uploaded by the worker.",
        ],
    }
    output = ROOT / "vision" / "delivery.json"
    output.write_text(json.dumps(delivery, indent=2), encoding="utf-8")
    print(json.dumps({"delivery": str(output), "videos": 6, "testsPassed": total,
                      "nativeFrames": 5400}, indent=2))


if __name__ == "__main__":
    main()
