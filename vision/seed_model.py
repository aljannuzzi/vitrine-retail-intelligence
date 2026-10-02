"""Explicit build-time download; importing this module never accesses the hub."""

import argparse
import json
import os
from pathlib import Path

from .detector import (
    DEFAULT_MODEL_PATH,
    MANIFEST_FILENAME,
    MODEL_ARTIFACTS,
    MODEL_ID,
    MODEL_REVISION,
    artifact_sha256,
    validate_model_manifest,
)


def _source_snapshot():
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    paths = []
    for filename in MODEL_ARTIFACTS:
        try:
            artifact = hf_hub_download(
                MODEL_ID, filename, revision=MODEL_REVISION, local_files_only=True
            )
        except LocalEntryNotFoundError:
            artifact = hf_hub_download(MODEL_ID, filename, revision=MODEL_REVISION)
        paths.append(Path(artifact))
    if len({path.parent for path in paths}) != 1:
        raise ValueError("Pinned model artifacts must belong to the same cached snapshot")
    return paths[0].parent


def seed_model(model_path=None):
    destination = Path(
        model_path if model_path is not None else os.environ.get("MODEL_PATH", DEFAULT_MODEL_PATH)
    ).expanduser().resolve()
    if (destination / MANIFEST_FILENAME).is_file():
        validate_model_manifest(destination)
        return destination

    import torch
    from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection

    # Load only the three required pinned files, never probe optional Hub artifacts.
    source = str(_source_snapshot())
    destination.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(min(4, max(1, os.cpu_count() or 1)))
    processor = RTDetrImageProcessor.from_pretrained(
        source, local_files_only=True, trust_remote_code=False
    )
    model = RTDetrV2ForObjectDetection.from_pretrained(
        source, local_files_only=True, trust_remote_code=False, use_safetensors=True
    )
    model.to("cpu")
    model.eval()
    processor.save_pretrained(str(destination))
    model.save_pretrained(str(destination), safe_serialization=True)
    manifest = {
        "modelId": MODEL_ID,
        "revision": MODEL_REVISION,
        "artifactSha256": {
            filename: artifact_sha256(destination / filename)
            for filename in MODEL_ARTIFACTS
        },
    }
    (destination / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Local model directory (default: MODEL_PATH or /opt/vitrine/model)")
    args = parser.parse_args()
    seed_model(args.output)


if __name__ == "__main__":
    main()
