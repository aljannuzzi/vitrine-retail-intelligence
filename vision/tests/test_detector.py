"""No dependencies or model downloads: run with python -B -m unittest discovery."""

import contextlib
import json
import math
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from vision.detector import DEFAULT_MODEL_PATH, MODEL_ID, MODEL_REVISION, Detector, normalize_detections


class NormalizationTests(unittest.TestCase):
    def normalize(self, scores, labels, boxes, threshold=0.5, id2label=None):
        return normalize_detections(
            {"scores": scores, "labels": labels, "boxes": boxes},
            (200, 100),
            {0: "person", 1: "bicycle"} if id2label is None else id2label,
            threshold,
        )

    def test_person_threshold_and_xyxy_contract(self):
        self.assertEqual(
            self.normalize(
                [0.75, 0.5, 0.49, 0.99], [0, 0, 0, 1],
                [[20, 10, 100, 90]] * 4,
            ),
            [
                {"label": "person", "score": 0.75, "box": [0.1, 0.1, 0.5, 0.9]},
                {"label": "person", "score": 0.5, "box": [0.1, 0.1, 0.5, 0.9]},
            ],
        )

    def test_clips_partial_boxes(self):
        self.assertEqual(
            self.normalize([1], [0], [[-50, -10, 250, 110]]),
            [{"label": "person", "score": 1.0, "box": [0.0, 0.0, 1.0, 1.0]}],
        )

    def test_skips_bad_boxes(self):
        boxes = [
            [0, 0, 0, 10], [20, 0, 10, 20], [0, 20, 10, 10],
            [-20, 0, -1, 10], [201, 0, 250, 10], [0, 101, 10, 120],
            [0, -20, 10, -1], [0, 0, math.nan, 10], [0, 0, 10, math.inf],
            [0, 0, -math.inf, 10], [0, 0, 10], None, ["bad", 0, 10, 10],
        ]
        self.assertEqual(self.normalize([0.9] * len(boxes), [0] * len(boxes), boxes), [])

    def test_skips_bad_scores(self):
        scores = [math.nan, math.inf, -math.inf, -1, 1.1, None, "bad", True]
        self.assertEqual(self.normalize(scores, [0] * len(scores), [[0, 0, 10, 10]] * len(scores)), [])

    def test_uses_model_label_mapping_not_hardcoded_class(self):
        self.assertEqual(
            self.normalize([0.9] * 5, [0, 7, 99, 7.5, True], [[0, 0, 20, 10]] * 5,
                           id2label={"7": "person", "0": "cat"}),
            [{"label": "person", "score": 0.9, "box": [0.0, 0.0, 0.1, 0.1]}],
        )

    def test_empty_detections(self):
        self.assertEqual(self.normalize([], [], []), [])

    def test_threshold_endpoints(self):
        for threshold in (0, 1):
            self.assertEqual(len(self.normalize([threshold], [0], [[0, 0, 10, 10]], threshold)), 1)

    def test_rejects_invalid_threshold(self):
        for threshold in (-0.1, 1.1, math.nan, math.inf, -math.inf, None, "bad", True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.normalize([], [], [], threshold)

    def test_rejects_invalid_dimensions(self):
        for size in ((0, 1), (1, 0), (-1, 1), (1.5, 1), (True, 1), (math.inf, 1)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                normalize_detections({"scores": [], "labels": [], "boxes": []}, size, {})

    def test_mismatched_arrays_fail_instead_of_silent_truncation(self):
        with self.assertRaises(ValueError):
            self.normalize([0.9], [], [[0, 0, 20, 10]])


class FakeImage:
    def __init__(self, size=(200, 100)):
        self.size = size
        self.convert = MagicMock(return_value=self)


class FakeArray:
    def __init__(self, shape=(100, 200, 3), dtype="uint8"):
        self.shape = shape
        self.ndim = len(shape)
        self.dtype = dtype


def tensor(value, **kwargs):
    result = MagicMock()
    result.detach.return_value = result
    result.cpu.return_value = result
    result.to.return_value = result
    result.tolist.return_value = value
    return result


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.torch = ModuleType("torch")
        self.torch.set_num_threads = MagicMock()
        self.torch.get_num_interop_threads = MagicMock(return_value=1)
        self.torch.set_num_interop_threads = MagicMock()
        self.torch.tensor = MagicMock(side_effect=tensor)
        self.torch.inference_mode = MagicMock(side_effect=contextlib.nullcontext)
        self.transformers = ModuleType("transformers")
        self.transformers.RTDetrImageProcessor = MagicMock()
        self.transformers.RTDetrV2ForObjectDetection = MagicMock()
        self.processor = self.transformers.RTDetrImageProcessor.from_pretrained.return_value
        self.model = self.transformers.RTDetrV2ForObjectDetection.from_pretrained.return_value
        self.model.config.id2label = {0: "person", 1: "bicycle"}
        self.input = tensor(None)
        self.processor.return_value = {"pixel_values": self.input}
        self.processor.post_process_object_detection.return_value = [{
            "scores": tensor([0.9]),
            "labels": tensor([0]),
            "boxes": tensor([[20, 10, 100, 90]]),
        }]
        self.numpy = ModuleType("numpy")
        self.numpy.ndarray = FakeArray
        self.numpy.uint8 = "uint8"
        self.pil = ModuleType("PIL")
        self.pil.Image = SimpleNamespace(
            Image=FakeImage,
            fromarray=MagicMock(side_effect=lambda array: FakeImage((array.shape[1], array.shape[0]))),
        )
        self.enterContext(patch.dict(sys.modules, {
            "torch": self.torch, "transformers": self.transformers,
            "numpy": self.numpy, "PIL": self.pil,
        }))
        self.enterContext(patch.dict("os.environ", {}, clear=True))
        self.enterContext(patch("vision.detector.Path.is_dir", return_value=True))
        self.validate_manifest = self.enterContext(patch("vision.detector.validate_model_manifest"))

    def test_loads_default_artifact_locally_without_remote_code(self):
        detector = Detector()
        path = str(Path(DEFAULT_MODEL_PATH).resolve())
        self.assertEqual(str(detector.model_path), path)
        self.validate_manifest.assert_called_once_with(detector.model_path)
        self.transformers.RTDetrImageProcessor.from_pretrained.assert_called_once_with(
            path, local_files_only=True, trust_remote_code=False
        )
        self.transformers.RTDetrV2ForObjectDetection.from_pretrained.assert_called_once_with(
            path, local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        self.model.to.assert_called_once_with("cpu")
        self.model.eval.assert_called_once_with()
        self.assertEqual(detector.threshold, 0.5)

    def test_explicit_path_and_threshold_override_environment(self):
        with patch.dict("os.environ", {"MODEL_PATH": "environment-model", "DETECTION_THRESHOLD": "0.8"}):
            detector = Detector(Path("pilot-model"), threshold=0.6)
        self.assertEqual(detector.model_path, Path("pilot-model").resolve())
        self.assertEqual(detector.threshold, 0.6)

    def test_environment_configuration(self):
        with patch.dict("os.environ", {"MODEL_PATH": "baked-model", "DETECTION_THRESHOLD": "0.7"}):
            detector = Detector()
        self.assertEqual(detector.model_path, Path("baked-model").resolve())
        self.assertEqual(detector.threshold, 0.7)

    def test_missing_folder_does_not_attempt_load(self):
        with patch("vision.detector.Path.is_dir", return_value=False), self.assertRaises(FileNotFoundError):
            Detector("missing-model")
        self.transformers.RTDetrV2ForObjectDetection.from_pretrained.assert_not_called()
        self.transformers.RTDetrImageProcessor.from_pretrained.assert_not_called()

    def test_invalid_threshold_rejected_before_loading(self):
        for threshold in (-1, 2, math.nan, math.inf, True, "bad"):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                Detector(threshold=threshold)
        self.transformers.RTDetrV2ForObjectDetection.from_pretrained.assert_not_called()

    def test_invalid_environment_threshold_rejected(self):
        with patch.dict("os.environ", {"DETECTION_THRESHOLD": "bad"}), self.assertRaises(ValueError):
            Detector()

    def test_threads_bounded_even_on_large_hosts(self):
        for count, expected in ((128, 4), (2, 2), (None, 1), (0, 1)):
            with self.subTest(count=count), patch("vision.detector.os.cpu_count", return_value=count):
                Detector()
                self.torch.set_num_threads.assert_called_with(expected)

    def test_interop_threads_are_bounded_without_reconfiguring_on_reload(self):
        self.torch.get_num_interop_threads.return_value = 128
        with patch("vision.detector.os.cpu_count", return_value=8):
            Detector()
        self.torch.set_num_interop_threads.assert_called_once_with(4)
        self.torch.get_num_interop_threads.return_value = 4
        with patch("vision.detector.os.cpu_count", return_value=8):
            Detector()
        self.torch.set_num_interop_threads.assert_called_once_with(4)

    def test_initialized_interop_pool_does_not_prevent_loading(self):
        self.torch.get_num_interop_threads.return_value = 128
        self.torch.set_num_interop_threads.side_effect = RuntimeError("already initialized")
        with self.assertWarnsRegex(RuntimeWarning, "already initialized"):
            Detector()
        self.model.eval.assert_called_once_with()
        self.assertEqual(len(self.torch.set_num_interop_threads.call_args_list), 1)

    def test_pil_inference_is_cpu_and_uses_original_height_width(self):
        detector = Detector(threshold=0.6)
        image = FakeImage()
        self.assertEqual(detector.detect(image), [
            {"label": "person", "score": 0.9, "box": [0.1, 0.1, 0.5, 0.9]},
        ])
        image.convert.assert_called_once_with("RGB")
        self.processor.assert_called_once_with(
            images=image, return_tensors="pt", input_data_format="channels_last"
        )
        self.input.to.assert_called_once_with("cpu")
        self.model.assert_called_once_with(pixel_values=self.input)
        self.torch.inference_mode.assert_called_once_with()
        self.torch.tensor.assert_called_once_with([[100, 200]], device="cpu")
        self.processor.post_process_object_detection.assert_called_once()
        call = self.processor.post_process_object_detection.call_args
        self.assertEqual(call.args, (self.model.return_value,))
        self.assertEqual(call.kwargs["target_sizes"].tolist(), [[100, 200]])
        self.assertEqual(call.kwargs["threshold"], 0.6)

    def test_numpy_rgb_array(self):
        array = FakeArray()
        self.assertEqual(len(Detector().detect(array)), 1)
        self.pil.Image.fromarray.assert_called_once_with(array)

    def test_invalid_input_never_runs_inference(self):
        detector = Detector()
        for image in (None, "image.jpg", FakeArray((10, 10)), FakeArray((10, 10, 4)),
                      FakeArray(dtype="float32"), FakeArray((0, 10, 3)), FakeImage((0, 10))):
            with self.subTest(image=image), self.assertRaises((TypeError, ValueError)):
                detector.detect(image)
        self.model.assert_not_called()

    def test_normalization_helper_is_patchable(self):
        with patch("vision.detector.normalize_detections", return_value=["sentinel"]) as normalize:
            self.assertEqual(Detector().detect(FakeImage()), ["sentinel"])
        normalize.assert_called_once_with(
            {"scores": [0.9], "labels": [0], "boxes": [[20, 10, 100, 90]]},
            (200, 100), {0: "person", 1: "bicycle"}, 0.5,
        )

    def test_inference_errors_propagate_not_fake_empty_results(self):
        self.model.side_effect = RuntimeError("inference failed")
        with self.assertRaisesRegex(RuntimeError, "inference failed"):
            Detector().detect(FakeImage())

    def test_loading_errors_propagate(self):
        self.transformers.RTDetrV2ForObjectDetection.from_pretrained.side_effect = OSError("bad artifact")
        with self.assertRaisesRegex(OSError, "bad artifact"):
            Detector()

    def test_seed_protocol_with_fake_dependencies_and_no_writes(self):
        from vision.seed_model import MANIFEST_FILENAME, seed_model

        with (
            patch("vision.seed_model.Path.is_file", return_value=False),
            patch("vision.seed_model._source_snapshot", return_value=Path("cached-snapshot")),
            patch("vision.seed_model.Path.mkdir") as mkdir,
            patch("vision.seed_model.Path.write_text") as write,
            patch("vision.seed_model.artifact_sha256", return_value="a" * 64) as digest,
        ):
            destination = seed_model("pilot-model")
        self.assertEqual(destination, Path("pilot-model").resolve())
        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.transformers.RTDetrImageProcessor.from_pretrained.assert_called_once_with(
            "cached-snapshot", local_files_only=True, trust_remote_code=False
        )
        self.transformers.RTDetrV2ForObjectDetection.from_pretrained.assert_called_once_with(
            "cached-snapshot", local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        self.processor.save_pretrained.assert_called_once_with(str(destination))
        self.model.save_pretrained.assert_called_once_with(str(destination), safe_serialization=True)
        self.assertEqual(MANIFEST_FILENAME, "manifest.json")
        filenames = ("config.json", "preprocessor_config.json", "model.safetensors")
        self.assertEqual([call.args[0] for call in digest.call_args_list],
                         [destination / filename for filename in filenames])
        self.assertEqual(json.loads(write.call_args.args[0]), {
            "modelId": MODEL_ID,
            "revision": MODEL_REVISION,
            "artifactSha256": {filename: "a" * 64 for filename in filenames},
        })
        self.assertEqual(write.call_args.kwargs, {"encoding": "utf-8"})


class ImportTests(unittest.TestCase):
    def test_imports_work_without_third_party_packages(self):
        script = """
import sys
class BlockThirdParty:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'transformers', 'PIL', 'numpy', 'huggingface_hub'}:
            raise ImportError('third party imports forbidden')
sys.meta_path.insert(0, BlockThirdParty())
import vision
import vision.detector
import vision.seed_model
assert vision.MODEL_ID == 'PekingU/rtdetr_v2_r18vd'
assert vision.MODEL_REVISION == '5650961749fa93567c0d46fc7f43ea4f9e914107'
"""
        result = subprocess.run(
            [sys.executable, "-B", "-S", "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
