"""Read-only native PNG audit; all reports are authoring-only artifacts."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageFile

ROOT = Path(__file__).resolve().parents[2]
RENDERS = ROOT / ".local" / "renders"
FRAME_COUNT = 900
ImageFile.LOAD_TRUNCATED_IMAGES = False


def decode(path):
    before = path.stat()
    payload = path.read_bytes()
    with Image.open(BytesIO(payload)) as image:
        if image.format != "PNG" or image.size != (1280, 720) or image.mode != "RGB":
            raise ValueError(f"Expected PNG RGB 1280x720, got {image.format} {image.mode} {image.size}")
        if payload[24:26] != b"\x08\x02":
            raise ValueError("Expected native 8-bit truecolor PNG, without conversion")
        metadata = {key: image.info.get(key) for key in ("Frame", "Camera", "Time", "Date")}
        image.verify()  # Check PNG chunk integrity, then reopen for a complete raster decode.
    with Image.open(BytesIO(payload)) as image:
        image.load()
        pixels = np.array(image, dtype=np.uint8)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("File changed during verification")
    return pixels, {"fileSha256": hashlib.sha256(payload).hexdigest(),
                    "rgbSha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
                    "bytes": len(payload), "pngMetadata": metadata}


def pixel_difference(a, b):
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return {"meanAbsoluteChannelDifference": float(delta.mean()),
            "rootMeanSquareChannelDifference": float(np.sqrt(np.mean(np.square(delta, dtype=np.float64)))),
            "maxChannelDifference": int(delta.max()),
            "changedPixelFraction": float(np.any(delta, axis=2).mean()),
            "fractionPixelsAnyChannelAbove16": float(np.any(delta > 16, axis=2).mean())}


def comparison(value, population):
    median = float(np.median(population))
    return {"adjacentMedian": median, "ratioToAdjacentMedian": value / median if median else None,
            "adjacentPercentileRank": float(100 * np.mean(np.asarray(population) <= value))}


def validate_sequence(folder):
    started = time.perf_counter()
    expected = {f"{frame:04d}.png" for frame in range(FRAME_COUNT)}
    actual = {path.name for path in folder.iterdir() if path.suffix.lower() == ".png"}
    report = {"folder": str(folder.relative_to(ROOT)), "expectedFrames": FRAME_COUNT,
              "presentPngFiles": len(actual), "missing": sorted(expected - actual),
              "unexpected": sorted(actual - expected), "errors": [], "frames": [],
              "adjacentTransitions": []}
    first = previous = None
    previous_frame = None
    rgb_groups = defaultdict(list)
    file_digest = hashlib.sha256()
    rgb_digest = hashlib.sha256()
    for frame in range(FRAME_COUNT):
        path = folder / f"{frame:04d}.png"
        try:
            pixels, hashes = decode(path)
            stamped_frame = hashes["pngMetadata"]["Frame"]
            if not isinstance(stamped_frame, str) or not stamped_frame.isdecimal() or int(stamped_frame) != frame + 1:
                raise ValueError(f"PNG Frame metadata does not match filename: {hashes['pngMetadata']}")
            expected_camera = folder.name.rsplit("-", 1)[0]
            if hashes["pngMetadata"]["Camera"] != expected_camera:
                raise ValueError(f"PNG Camera metadata does not match folder: {hashes['pngMetadata']}")
            report["frames"].append({"frame": frame, **hashes})
            # Canonical sequence digest binds each hash to its filename and ordering.
            file_digest.update(f"{path.name} {hashes['fileSha256']}\n".encode("ascii"))
            rgb_digest.update(f"{path.name} {hashes['rgbSha256']}\n".encode("ascii"))
            rgb_groups[hashes["rgbSha256"]].append(frame)
            if frame == 0:
                first = pixels
            if previous is not None and previous_frame == frame - 1:
                report["adjacentTransitions"].append(
                    {"from": frame - 1, "to": frame, **pixel_difference(previous, pixels)})
            previous, previous_frame = pixels, frame
        except Exception as error:
            report["errors"].append({"frame": frame, "error": str(error)})
            previous, previous_frame = None, None
        if frame % 150 == 0:
            print(f"PNG {folder.name} {frame}/{FRAME_COUNT}", flush=True)
    report["decodedFrames"] = len(report["frames"])
    report["sequenceFileSha256"] = file_digest.hexdigest()
    report["sequenceRgbSha256"] = rgb_digest.hexdigest()
    report["duplicateRgbFrameGroups"] = [frames for frames in rgb_groups.values() if len(frames) > 1]
    transitions = report["adjacentTransitions"]
    means = [item["meanAbsoluteChannelDifference"] for item in transitions]
    if means:
        report["adjacentSummary"] = {
            "count": len(means), "mean": float(np.mean(means)),
            "percentiles": {str(p): float(np.percentile(means, p)) for p in (0, 50, 90, 95, 99, 100)},
            "largestTransitions": sorted(transitions, key=lambda x: x["meanAbsoluteChannelDifference"],
                                         reverse=True)[:20]}
        if first is not None and previous_frame == FRAME_COUNT - 1:
            seam = pixel_difference(previous, first)
            report["loopSeam899To0"] = {
                **seam, **comparison(seam["meanAbsoluteChannelDifference"], means)}
        if "checkout" in folder.name:
            left = 854 if folder.name.endswith("-optimized") else 856
            boundary = next((item for item in transitions if item["from"] == left), None)
            if boundary:
                local = [item["meanAbsoluteChannelDifference"] for item in transitions
                         if abs(item["from"] - left) <= 10 and item["from"] != left]
                report["resumeBoundary"] = {
                    **boundary, "globalComparison": comparison(boundary["meanAbsoluteChannelDifference"], means),
                    "localPlusMinus10Comparison": comparison(boundary["meanAbsoluteChannelDifference"], local),
                    "frameHashes": [item for item in report["frames"] if item["frame"] in (left, left + 1)]}
    report["passed"] = (report["decodedFrames"] == FRAME_COUNT and not report["missing"]
                        and not report["unexpected"] and not report["errors"])
    report["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    print(f"COMPLETE {folder.name}: {report['decodedFrames']} frames in {report['elapsedSeconds']}s", flush=True)
    return report


def validate_sentinels(scene_report):
    records = scene_report.get("endpointSentinels", [])
    results = []
    for zone in ("denim", "essentials", "checkout"):
        for variant in ("baseline", "optimized"):
            selected = {record["frame"]: record for record in records
                        if record["camera"] == zone and record["variant"] == variant}
            if set(selected) != {0, 900}:
                raise ValueError(f"Missing endpoint sentinels for {zone}/{variant}")
            zero, zero_hash = decode(ROOT / selected[0]["path"])
            end, end_hash = decode(ROOT / selected[900]["path"])
            if any(hashes["fileSha256"] != selected[frame]["fileSha256"]
                   for frame, hashes in ((0, zero_hash), (900, end_hash))):
                raise ValueError("Sentinel changed since scene verification")
            native, native_hash = decode(RENDERS / f"camera-{zone}-{variant}" / "0000.png")
            results.append({
                "camera": zone, "variant": variant, "frame0": zero_hash, "frame900": end_hash,
                "nativeFrame0": native_hash,
                "sentinel0To900": pixel_difference(zero, end),
                "native0ToSentinel0": pixel_difference(native, zero),
                "native0ToSentinel900": pixel_difference(native, end),
                "endpointRgbIdentical": zero_hash["rgbSha256"] == end_hash["rgbSha256"],
                "native0MatchesSentinel0": zero_hash["rgbSha256"] == native_hash["rgbSha256"],
            })
    return results


def main():
    started = time.perf_counter()
    report = {
        "purpose": "Authoring-only native PNG integrity and measured continuity; not detector/worker input",
        "validatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "validatorSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "expectedTotalFrames": 5400, "nativeFormat": "PNG RGB 1280x720",
        "sequenceRange": [0, 899], "fps": 15, "durationSeconds": 60,
        "digestDefinition": "SHA256 of ASCII lines: four-digit filename + space + per-frame SHA256 + LF, frame order 0..899; RGB hashes cover full decoded row-major uint8 RGB bytes",
        "pixelMetricUnits": "8-bit RGB channel levels (0..255), all 1280x720 pixels; no registration, cropping or masking",
        "metadataChecks": "Every native PNG must stamp Frame=filename index+1 and Camera=folder camera ID",
        "sequences": [], "errors": [],
        "limitations": [
            "Successful decode and ordered filenames cannot prove each native frame was generated by the current authoring source.",
            "Hashes and matching Blender frame/camera stamps establish current content consistency, not tamper-proof capture history.",
            "Global pixel metrics include lighting, occlusion and renderer variation; they do not establish natural motion or identical people from image appearance.",
            "The playback seam is native frame 899 to 0, not sentinel 900 to 0. Endpoint closure and smooth playback are distinct.",
            "Duplicate images and high-difference transitions are reported, not silently classified as valid or invalid motion.",
            "MP4 decoding and actual model validation are outside this audit."
        ],
    }
    for zone in ("denim", "essentials", "checkout"):
        for variant in ("baseline", "optimized"):
            folder = RENDERS / f"camera-{zone}-{variant}"
            try:
                report["sequences"].append(validate_sequence(folder))
            except Exception as error:
                report["errors"].append({"folder": str(folder.relative_to(ROOT)), "error": str(error)})
    scene_path = ROOT / "assets" / "generated" / "scene-validation.json"
    try:
        scene_bytes = scene_path.read_bytes()
        scene_report = json.loads(scene_bytes)
        report["sceneValidationSha256"] = hashlib.sha256(scene_bytes).hexdigest()
        report["sceneValidationPassed"] = scene_report.get("passed") is True
        report["sceneSourceHashesMatch"] = all(
            hashlib.sha256(path.read_bytes()).hexdigest() == scene_report.get(key)
            for key, path in (
                ("renderStoreSha256", ROOT / "vision" / "authoring" / "render_store.py"),
                ("humanAssetsSha256", ROOT / ".local" / "human-assets" / "humans.blend"),
                ("validatorSha256", ROOT / "vision" / "authoring" / "validate_authored_scene.py"),
            ))
        report["endpointSentinels"] = validate_sentinels(scene_report)
    except Exception as error:
        report["errors"].append({"endpointSentinels": str(error)})
    report["decodedTotalFrames"] = sum(item["decodedFrames"] for item in report["sequences"])
    report["nativeIntegrityPassed"] = (len(report["sequences"]) == 6
                                      and all(item["passed"] for item in report["sequences"]))
    sentinels = report.get("endpointSentinels", [])
    report["renderedEndpointsRgbIdentical"] = (len(sentinels) == 6
                                             and all(item["endpointRgbIdentical"] for item in sentinels))
    report["nativeFrame0AnchorsMatch"] = (len(sentinels) == 6
                                        and all(item["native0MatchesSentinel0"] for item in sentinels))
    report["passDefinition"] = ("Strict integrity plus passed authored-scene audit, exact decoded RGB endpoint "
                                "equality and exact native frame-0 anchors; never a smooth-motion assertion")
    report["passed"] = (not report["errors"] and report["nativeIntegrityPassed"]
                        and report.get("sceneValidationPassed") is True
                        and report.get("sceneSourceHashesMatch") is True
                        and report["renderedEndpointsRgbIdentical"] and report["nativeFrame0AnchorsMatch"])
    report["elapsedSeconds"] = round(time.perf_counter() - started, 3)
    output = ROOT / "assets" / "generated" / "native-frame-validation.json"
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "decodedTotalFrames", "elapsedSeconds", "errors")}),
          flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
