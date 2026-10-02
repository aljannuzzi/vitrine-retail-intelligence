"""Record reproducible provenance for original outputs and public inputs."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATED = ROOT / "assets" / "generated"


def fingerprint(relative):
    path = ROOT / relative
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path.relative_to(ROOT)), "sha256": digest, "bytes": path.stat().st_size}


def main():
    model = json.loads((ROOT / ".local" / "model" / "manifest.json").read_text())
    blender = fingerprint(".local/tools/blender.zip")
    official = (ROOT / ".local" / "tools" / "blender-4.5.3.sha256").read_text()
    assert blender["sha256"] in official
    manifest = {
        "schemaVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceFootage": {
            "type": "original-synthetic-3d",
            "downloadedVideoOrPhotographedPeople": False,
            "description": "Original authored store, fixtures, garment display patterns, lighting, cameras and motion. Anonymous CC0 anatomical human meshes wear CC0 clothing; no real people's photographs or identities.",
            "originalGeometry": ["store shell", "floor", "rugs", "apparel racks", "hangers",
                                 "display jeans and shirts", "folded clothing", "checkout counter",
                                 "unbranded shopping bags", "plants", "light fixtures"],
            "authoringScripts": [fingerprint("vision/authoring/" + name) for name in (
                "render_store.py", "prepare_humans.py", "validate_authored_scene.py",
                "evaluate_pilot.py", "finalize_media.py", "validate_media_worker.py",
                "validate_native_frames.py")],
            "outputHashes": "manifest.json / videos[*].sha256",
            "outputRights": "Original task-generated scene/render output; underlying third-party meshes, clothing, rig and textures are CC0. No claim that GPL covers rendered output.",
        },
        "humanAssets": {
            **fingerprint("assets/generated/human-license-manifest.json"),
            "license": "CC0-1.0",
            "licenseUrls": [
                "https://github.com/makehumancommunity/mpfb2/blob/afb9f530a7c2741dedb8df0ebae2e0b183caec21/LICENSE.md",
                "https://github.com/makehumancommunity/mpfb2/blob/afb9f530a7c2741dedb8df0ebae2e0b183caec21/LICENSE.ASSETS.md",
                "https://creativecommons.org/publicdomain/zero/1.0/",
            ],
            "policy": "The referenced manifest includes hashes and source URLs for each archive and selected graphical input. MPFB GPL authoring code and all raw meshes stay under .local and are excluded from the detector image.",
        },
        "authoringTools": [
            {**blender, "name": "Blender", "version": "4.5.3 LTS",
             "downloadUrl": "https://download.blender.org/release/Blender4.5/blender-4.5.3-windows-x64.zip",
             "checksumUrl": "https://download.blender.org/release/Blender4.5/blender-4.5.3.sha256",
             "officialChecksumVerified": True, "license": "GPL-3.0-or-later",
             "licenseUrl": "https://www.blender.org/about/license/",
             "packagedInApplication": False},
            {**fingerprint(".local/tools/ffmpeg.exe"), "name": "FFmpeg",
             "version": "7.1-essentials_build-www.gyan.dev", "license": "GPL-3.0-or-later",
             "downloadSource": "imageio-ffmpeg 0.6.0 PyPI wheel (Gyan public FFmpeg build)",
             "sourceUrl": "https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0",
             "licenseUrl": "https://ffmpeg.org/legal.html", "packagedInApplication": False,
             "note": "Local authoring encoder only. Cloud image installs Debian FFmpeg with its package licenses."},
        ],
        "detector": {
            "id": model["modelId"], "revision": model["revision"],
            "license": "Apache-2.0",
            "licenseUrls": [
                "https://huggingface.co/PekingU/rtdetr_v2_r18vd/blob/5650961749fa93567c0d46fc7f43ea4f9e914107/README.md",
                "https://github.com/lyuwenyu/RT-DETR/blob/main/LICENSE",
                "https://github.com/huggingface/transformers/blob/v4.53.3/LICENSE",
            ],
            "source": "Pinned public Hugging Face checkpoint; safetensors reserialization without fine-tuning",
            "bakedArtifactManifest": model,
            "licenseText": fingerprint("vision/licenses/rt-detr-apache-2.0.txt"),
            "attribution": fingerprint("vision/licenses/MODEL-NOTICE.txt"),
            "privateDataUploaded": False, "trustRemoteCode": False,
        },
        "distributionBoundary": "Application receives rendered MP4s, JSON provenance and detector runtime. Portable Blender, MPFB sources, meshes, texture source files, local Python, caches and evaluation frames remain local authoring inputs.",
    }
    (GENERATED / "asset-license-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Verified official Blender checksum and wrote asset-license-manifest.json")


if __name__ == "__main__":
    main()
