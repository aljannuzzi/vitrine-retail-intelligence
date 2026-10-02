"""Encode and verify completed native 15fps renders. Never contacts Azure."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
FFMPEG = ROOT / ".local" / "tools" / "ffmpeg.exe"
GENERATED = ROOT / "assets" / "generated"
RENDERS = ROOT / ".local" / "renders"


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(args):
    result = subprocess.run([str(FFMPEG), "-hide_banner", "-nostdin", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-4000:])
    return result


def verify(path):
    output = run(["-v", "info", "-xerror", "-err_detect", "explode",
                  "-i", str(path), "-map", "0:v:0", "-an",
                  "-progress", "pipe:1", "-nostats", "-f", "null", "-"])
    stderr, progress = output.stderr.decode(errors="replace"), output.stdout.decode()
    assert "1280x720" in stderr and "15 fps" in stderr, stderr
    assert re.search(r"Duration: 00:01:00\.00", stderr), stderr
    assert "h264" in stderr and "yuv420p" in stderr, stderr
    assert "Audio:" not in stderr, "Unexpected audio track"
    frames = int(re.findall(r"^frame=(\d+)$", progress, re.M)[-1])
    assert frames == 900, (path, frames)
    return {"decodedFrames": frames, "durationSeconds": 60, "fps": 15,
            "width": 1280, "height": 720, "codec": "h264", "pixelFormat": "yuv420p",
            "audio": False, "fullDecodeVerified": True}


def main():
    started = time.perf_counter()
    pending = [(zone, variant) for zone in ("denim", "essentials", "checkout")
               for variant in ("baseline", "optimized")]
    deadline = time.monotonic() + 3 * 60 * 60
    records = []
    while pending:
        completed = False
        for zone, variant in pending[:]:
            camera = "camera-" + zone
            stem = f"{camera}-{variant}"
            source = RENDERS / stem
            last = source / "0899.png"
            path = GENERATED / (stem + ".mp4")
            if not path.exists():
                if not last.exists() or len(list(source.glob("*.png"))) != 900:
                    continue
                # A PNG may still be in the middle of its last write.
                if time.time() - last.stat().st_mtime < 5:
                    continue
                partial = GENERATED / (stem + ".encoding.mp4")
                run(["-v", "error", "-y", "-framerate", "15", "-start_number", "0",
                     "-i", str(source / "%04d.png"), "-frames:v", "900", "-an",
                     "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                     "-pix_fmt", "yuv420p", "-r", "15", "-g", "30", "-keyint_min", "15",
                     "-threads", "4", "-movflags", "+faststart",
                     "-metadata", "title=Original synthetic VITRINE store / " + zone,
                     "-metadata", "comment=Original 3D render; anonymous synthetic adults; not real surveillance",
                     str(partial)])
                partial.replace(path)
            metadata = verify(path)
            records.append({
                "filename": path.name, "cameraId": camera, "zoneId": zone,
                "variant": variant, "provenance": "synthetic-3d", "footage": "synthetic-3d",
                "blobName": f"media/{camera}/{variant}.mp4", "sha256": sha256(path),
                "bytes": path.stat().st_size, **metadata,
            })
            pending.remove((zone, variant))
            completed = True
            print(json.dumps({"completed": path.name, "bytes": path.stat().st_size,
                              "remaining": len(pending)}), flush=True)
        if pending and not completed:
            if time.monotonic() > deadline:
                raise TimeoutError("Render completion deadline exceeded: " + repr(pending))
            time.sleep(15)
    records.sort(key=lambda record: record["filename"])
    validations = {}
    for name in ("scene-validation.json", "native-frame-validation.json", "media-worker-validation.json"):
        report_path = GENERATED / name
        if report_path.exists():
            validations[name] = {"sha256": sha256(report_path), "bytes": report_path.stat().st_size}
    manifest = {
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "provenance": "synthetic-3d", "durationSeconds": 60, "fps": 15,
        "width": 1280, "height": 720, "nativeRenderedFramesPerVideo": 900,
        "videos": records,
        "authoring": {
            "renderer": "Blender 4.5.3 EEVEE", "sceneSeed": 712,
            "script": "vision/authoring/render_store.py",
            "scriptSha256": sha256(ROOT / "vision" / "authoring" / "render_store.py"),
            "syntheticAdultActorsInStore": 9,
            "control": "Identical 60-second actor paths and poses in both variants; denim display rotates from 12 to 95 degrees.",
            "loop": "Cyclic continuous positions and turn poses; frame 900 equals frame 0 and is excluded from the 900-frame MP4.",
            "noRetailLogos": True, "noRealPersonAssets": True, "noDownloadedFootage": True,
            "noAudio": True, "noLocalRuntimeComponent": True,
        },
        "licenses": {"humanAssets": "human-license-manifest.json",
                     "humanAssetsLicense": "CC0-1.0",
                     "sceneAndRenderRights": "Original task-generated scene and renders; underlying human assets CC0-1.0.",
                     "rawGplAuthoringAddonsPackaged": False,
                     "originalAssetsAndTools": "asset-license-manifest.json"},
        "detectionPilot": "pilot-report.json",
        "validationReports": validations,
        "finalizationElapsedSeconds": round(time.perf_counter() - started, 3),
        "limitations": [
            "Visible per-frame detector counts are not unique visitors, tracking, dwell, or conversion.",
            "Camera fields of view overlap; never sum counts as unique people.",
            "Synthetic layout change is not proof of real-world sales uplift.",
        ],
    }
    (GENERATED / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("All six MP4s decoded and verified; manifest.json written.", flush=True)


if __name__ == "__main__":
    main()
