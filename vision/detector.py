"""Person detection using a baked RT-DETRv2 artifact, never a runtime download."""

import hashlib
import json
import math
import os
import warnings
from numbers import Integral
from pathlib import Path

MODEL_ID = "PekingU/rtdetr_v2_r18vd"
MODEL_REVISION = "5650961749fa93567c0d46fc7f43ea4f9e914107"
DEFAULT_MODEL_PATH = "/opt/vitrine/model"
MANIFEST_FILENAME = "manifest.json"
MODEL_ARTIFACTS = ("config.json", "preprocessor_config.json", "model.safetensors")


def artifact_sha256(path):
    """Hash saved bytes in bounded chunks, including large weight files."""
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_model_manifest(model_path):
    """Validate the seed's pinned identity and local bytes before model loading.

    The manifest is a build-time trust record, not a signature: deploy it and
    the artifacts together from a trusted seed in a read-only model directory.
    """
    try:
        manifest = json.loads((model_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Missing or invalid model manifest; rerun vision.seed_model") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("modelId") != MODEL_ID
        or manifest.get("revision") != MODEL_REVISION
    ):
        raise ValueError("Model manifest does not match the pinned model ID and revision")
    hashes = manifest.get("artifactSha256")
    if not isinstance(hashes, dict) or set(hashes) != set(MODEL_ARTIFACTS):
        raise ValueError("Model manifest must contain SHA-256 hashes for all required artifacts")
    for filename in MODEL_ARTIFACTS:
        expected = hashes[filename]
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise ValueError(f"Invalid SHA-256 in model manifest for {filename}")
        try:
            actual = artifact_sha256(model_path / filename)
        except OSError as exc:
            raise ValueError(f"Missing or unreadable model artifact: {filename}") from exc
        if actual != expected:
            raise ValueError(f"Model artifact SHA-256 mismatch: {filename}")


def _threshold(value):
    if isinstance(value, bool):
        raise ValueError("Detection threshold must be finite and between 0 and 1")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Detection threshold must be finite and between 0 and 1") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Detection threshold must be finite and between 0 and 1")
    return value


def normalize_detections(detections, image_size, id2label, threshold=0.5):
    """Convert plain postprocessed pixel boxes to the public normalized contract.

    ``image_size`` is (width, height); detections has scores, labels and boxes
    sequences. This function has no tensor, image or model dependencies.
    """
    threshold = _threshold(threshold)
    width, height = image_size
    if not all(
        isinstance(value, Integral) and not isinstance(value, bool) and value > 0
        for value in (width, height)
    ):
        raise ValueError("Image dimensions must be positive integers")
    result = []
    for score, label, box in zip(
        detections["scores"], detections["labels"], detections["boxes"], strict=True
    ):
        if isinstance(label, bool) or not isinstance(label, Integral):
            continue
        if id2label.get(label, id2label.get(str(label))) != "person":
            continue
        try:
            if isinstance(score, bool) or len(box) != 4:
                continue
            score = float(score)
            coordinates = [float(value) for value in box]
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(score)
            or not threshold <= score <= 1
            or not all(math.isfinite(value) for value in coordinates)
        ):
            continue
        x1, y1, x2, y2 = [
            min(1.0, max(0.0, coordinate / dimension))
            for coordinate, dimension in zip(coordinates, (width, height, width, height))
        ]
        if x2 <= x1 or y2 <= y1:
            continue
        result.append({"label": "person", "score": score, "box": [x1, y1, x2, y2]})
    return result


class Detector:
    """Load a local folder once; detect PIL images or H×W×3 uint8 RGB arrays.

    Explicit arguments override MODEL_PATH and DETECTION_THRESHOLD. Missing
    local artifacts or invalid seed manifests fail closed, so workers can
    publish unavailable rather than inventing a zero count.
    """

    def __init__(self, model_path=None, threshold=None):
        self.threshold = _threshold(
            os.environ.get("DETECTION_THRESHOLD", "0.5") if threshold is None else threshold
        )
        self.model_path = Path(
            model_path if model_path is not None else os.environ.get("MODEL_PATH", DEFAULT_MODEL_PATH)
        ).expanduser().resolve()
        if not self.model_path.is_dir():
            raise FileNotFoundError(f"Local model directory does not exist: {self.model_path}")
        validate_model_manifest(self.model_path)

        import torch
        from transformers import RTDetrImageProcessor, RTDetrV2ForObjectDetection

        self._torch = torch
        threads = min(4, max(1, os.cpu_count() or 1))
        torch.set_num_threads(threads)
        if torch.get_num_interop_threads() > threads:
            try:
                torch.set_num_interop_threads(threads)
            except RuntimeError:
                # PyTorch permits this process-global setting only before parallel work.
                warnings.warn(
                    "PyTorch inter-op threads are already initialized; retaining the existing pool",
                    RuntimeWarning,
                    stacklevel=2,
                )
        self.processor = RTDetrImageProcessor.from_pretrained(
            str(self.model_path), local_files_only=True, trust_remote_code=False
        )
        self.model = RTDetrV2ForObjectDetection.from_pretrained(
            str(self.model_path),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
        )
        self.model.to("cpu")
        self.model.eval()

    def detect(self, image):
        import numpy as np
        from PIL import Image

        if isinstance(image, Image.Image):
            image = image.convert("RGB")
        elif isinstance(image, np.ndarray):
            if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                raise ValueError("NumPy image must be an H×W×3 uint8 RGB array")
            if image.shape[0] <= 0 or image.shape[1] <= 0:
                raise ValueError("Image dimensions must be positive")
            image = Image.fromarray(image)
        else:
            raise TypeError("Image must be a PIL image or NumPy RGB array")
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("Image dimensions must be positive")

        with self._torch.inference_mode():
            inputs = self.processor(images=image, return_tensors="pt", input_data_format="channels_last")
            inputs = {name: tensor.to("cpu") for name, tensor in inputs.items()}
            outputs = self.model(**inputs)
            processed = self.processor.post_process_object_detection(
                outputs,
                target_sizes=self._torch.tensor([[height, width]], device="cpu"),
                threshold=self.threshold,
            )[0]
        plain = {
            name: processed[name].detach().cpu().tolist()
            for name in ("scores", "labels", "boxes")
        }
        return normalize_detections(plain, (width, height), self.model.config.id2label, self.threshold)
