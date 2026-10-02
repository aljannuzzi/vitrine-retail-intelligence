"""Dependency-free provenance tests using in-memory artifacts, never real models."""

import hashlib
import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import MagicMock, patch

from vision.detector import (
    MANIFEST_FILENAME,
    MODEL_ARTIFACTS,
    MODEL_ID,
    MODEL_REVISION,
    Detector,
    artifact_sha256,
    validate_model_manifest,
)
from vision.seed_model import _source_snapshot, seed_model


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.destination = Path("provenance-test-model").resolve()
        self.artifacts = {
            "config.json": b'{"id2label": {"0": "person"}}',
            "preprocessor_config.json": b'{"size": {"height": 640, "width": 640}}',
            "model.safetensors": b"fake weights\x00\xff",
        }
        self.manifest = {
            "modelId": MODEL_ID,
            "revision": MODEL_REVISION,
            "artifactSha256": {
                filename: hashlib.sha256(data).hexdigest()
                for filename, data in self.artifacts.items()
            },
        }
        self.manifest_text = json.dumps(self.manifest)
        self.opened = []
        self.enterContext(patch.object(Path, "is_dir", return_value=True))
        self.enterContext(patch.object(Path, "read_text", autospec=True, side_effect=self.read_manifest))
        self.enterContext(patch.object(Path, "open", autospec=True, side_effect=self.open_artifact))

    def read_manifest(self, path, encoding):
        self.assertEqual(path, self.destination / MANIFEST_FILENAME)
        self.assertEqual(encoding, "utf-8")
        return self.manifest_text

    def open_artifact(self, path, mode):
        self.assertEqual(path.parent, self.destination)
        self.assertEqual(mode, "rb")
        self.opened.append(path.name)
        if path.name not in self.artifacts:
            raise FileNotFoundError(path)
        return io.BytesIO(self.artifacts[path.name])

    def assert_rejected_before_import(self, message):
        # None in sys.modules makes even an attempted dependency import fail.
        with patch.dict(sys.modules, {"torch": None, "transformers": None}):
            with self.assertRaisesRegex(ValueError, message):
                Detector(self.destination)

    def fake_dependencies(self):
        torch = ModuleType("torch")
        torch.set_num_threads = MagicMock()
        torch.get_num_interop_threads = MagicMock(return_value=1)
        transformers = ModuleType("transformers")
        transformers.RTDetrImageProcessor = MagicMock()
        transformers.RTDetrV2ForObjectDetection = MagicMock()
        self.enterContext(patch.dict(sys.modules, {"torch": torch, "transformers": transformers}))
        return transformers

    def test_valid_manifest_loads_only_after_all_artifacts_are_hashed(self):
        transformers = self.fake_dependencies()

        def check_hashes(*args, **kwargs):
            self.assertEqual(self.opened, list(MODEL_ARTIFACTS))
            self.assertTrue(kwargs["local_files_only"])
            return MagicMock()

        transformers.RTDetrImageProcessor.from_pretrained.side_effect = check_hashes
        transformers.RTDetrV2ForObjectDetection.from_pretrained.side_effect = check_hashes
        Detector(self.destination)
        transformers.RTDetrImageProcessor.from_pretrained.assert_called_once()
        transformers.RTDetrV2ForObjectDetection.from_pretrained.assert_called_once()

    def test_missing_or_unreadable_manifest_rejected(self):
        for error in (FileNotFoundError, PermissionError):
            with self.subTest(error=error), patch.object(Path, "read_text", side_effect=error):
                self.assert_rejected_before_import("Missing or invalid model manifest")
        self.assertEqual(self.opened, [])

    def test_malformed_manifest_rejected(self):
        for text in ("", "{", "null", "[]", "true", '"string"', "123"):
            with self.subTest(text=text):
                self.manifest_text = text
                self.assert_rejected_before_import("manifest")
        self.assertEqual(self.opened, [])

    def test_non_utf8_manifest_rejected(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with patch.object(Path, "read_text", side_effect=error):
            self.assert_rejected_before_import("Missing or invalid model manifest")

    def test_missing_or_wrong_identity_rejected_before_reading_artifacts(self):
        for field in ("modelId", "revision"):
            for value in (None, "", "another-model", "main", MODEL_REVISION[:8], 42, []):
                with self.subTest(field=field, value=value):
                    manifest = dict(self.manifest)
                    manifest[field] = value
                    self.manifest_text = json.dumps(manifest)
                    self.assert_rejected_before_import("pinned model ID and revision")
            manifest = dict(self.manifest)
            del manifest[field]
            self.manifest_text = json.dumps(manifest)
            self.assert_rejected_before_import("pinned model ID and revision")
        self.assertEqual(self.opened, [])

    def test_old_seed_manifest_without_hashes_rejected(self):
        del self.manifest["artifactSha256"]
        self.manifest_text = json.dumps(self.manifest)
        self.assert_rejected_before_import("SHA-256 hashes for all required artifacts")

    def test_invalid_artifact_maps_rejected_without_using_manifest_paths(self):
        hashes = self.manifest["artifactSha256"]
        invalid = [None, [], "", 42, {}]
        for filename in MODEL_ARTIFACTS:
            invalid.append({name: digest for name, digest in hashes.items() if name != filename})
        for filename in ("..\\outside", "../outside", "other.safetensors"):
            invalid.append({**hashes, filename: "0" * 64})
        for value in invalid:
            with self.subTest(value=value):
                self.manifest_text = json.dumps({**self.manifest, "artifactSha256": value})
                self.assert_rejected_before_import("SHA-256 hashes for all required artifacts")
        self.assertEqual(self.opened, [])

    def test_malformed_hashes_rejected_for_each_artifact(self):
        for filename in MODEL_ARTIFACTS:
            for value in (None, True, 12, [], {}, "", "0" * 63, "0" * 65, "g" * 64):
                with self.subTest(filename=filename, value=value):
                    hashes = {**self.manifest["artifactSha256"], filename: value}
                    self.manifest_text = json.dumps({**self.manifest, "artifactSha256": hashes})
                    self.assert_rejected_before_import("Invalid SHA-256")

    def test_each_tampered_artifact_rejected_before_loading(self):
        for filename in MODEL_ARTIFACTS:
            with self.subTest(filename=filename), patch.dict(
                self.artifacts, {filename: self.artifacts[filename] + b"tampered"}
            ):
                self.assert_rejected_before_import(f"SHA-256 mismatch: {filename}")

    def test_each_missing_artifact_rejected_before_loading(self):
        for filename in MODEL_ARTIFACTS:
            with self.subTest(filename=filename), patch.dict(self.artifacts):
                del self.artifacts[filename]
                self.assert_rejected_before_import(f"Missing or unreadable model artifact: {filename}")

    def test_unreadable_artifact_rejected_before_loading(self):
        with patch.object(Path, "open", side_effect=PermissionError):
            self.assert_rejected_before_import("Missing or unreadable model artifact")

    def test_environment_model_path_cannot_bypass_validation(self):
        self.manifest_text = "{}"
        with patch.dict("os.environ", {"MODEL_PATH": str(self.destination)}):
            with patch.dict(sys.modules, {"torch": None, "transformers": None}):
                with self.assertRaisesRegex(ValueError, "pinned model ID and revision"):
                    Detector()

    def test_hash_uses_all_bytes_in_bounded_chunks(self):
        data = b"a" * (2 * 1024 * 1024) + b"\x00\xfflast"
        self.artifacts["model.safetensors"] = data
        reads = []

        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        with patch.object(Path, "open", return_value=BoundedReader(data)):
            digest = artifact_sha256(self.destination / "model.safetensors")
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(reads, [1024 * 1024] * 4)

    def test_seed_hashes_saved_bytes_and_manifest_round_trips(self):
        self.enterContext(patch.object(Path, "is_file", return_value=False))
        self.enterContext(patch("vision.seed_model._source_snapshot", return_value=self.destination))
        transformers = self.fake_dependencies()
        processor = transformers.RTDetrImageProcessor.from_pretrained.return_value
        model = transformers.RTDetrV2ForObjectDetection.from_pretrained.return_value
        saved = dict(self.artifacts)
        self.artifacts.clear()

        def save_processor(*args, **kwargs):
            self.artifacts["preprocessor_config.json"] = saved["preprocessor_config.json"]

        def save_model(*args, **kwargs):
            for filename in ("config.json", "model.safetensors"):
                self.artifacts[filename] = saved[filename]

        processor.save_pretrained.side_effect = save_processor
        model.save_pretrained.side_effect = save_model
        with patch.object(Path, "mkdir"), patch.object(Path, "write_text", autospec=True) as write:
            self.assertEqual(seed_model(self.destination), self.destination)
        write.assert_called_once()
        path, text = write.call_args.args
        self.assertEqual(path, self.destination / MANIFEST_FILENAME)
        self.assertEqual(json.loads(text), self.manifest)
        self.assertEqual(self.opened, list(MODEL_ARTIFACTS))
        self.manifest_text = text
        validate_model_manifest(self.destination)
        processor.save_pretrained.assert_called_once_with(str(self.destination))
        model.save_pretrained.assert_called_once_with(str(self.destination), safe_serialization=True)

    def test_seed_does_not_publish_manifest_if_artifact_is_missing(self):
        self.enterContext(patch.object(Path, "is_file", return_value=False))
        self.enterContext(patch("vision.seed_model._source_snapshot", return_value=self.destination))
        self.fake_dependencies()
        del self.artifacts["model.safetensors"]
        with patch.object(Path, "mkdir"), patch.object(Path, "write_text") as write:
            with self.assertRaises(FileNotFoundError):
                seed_model(self.destination)
        write.assert_not_called()

    def test_seed_reuses_valid_output_without_importing_dependencies_or_writing(self):
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "mkdir") as mkdir,
            patch.object(Path, "write_text") as write,
            patch.dict(sys.modules, {"torch": None, "transformers": None, "huggingface_hub": None}),
        ):
            self.assertEqual(seed_model(self.destination), self.destination)
        self.assertEqual(self.opened, list(MODEL_ARTIFACTS))
        mkdir.assert_not_called()
        write.assert_not_called()

    def test_seed_rejects_tampered_output_without_downloading_or_overwriting(self):
        self.artifacts["model.safetensors"] += b"tampered"
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "write_text") as write,
            patch("vision.seed_model._source_snapshot") as source,
        ):
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                seed_model(self.destination)
        source.assert_not_called()
        write.assert_not_called()


class SeedCacheTests(unittest.TestCase):
    def setUp(self):
        hub = ModuleType("huggingface_hub")
        errors = ModuleType("huggingface_hub.errors")
        self.cache_miss = type("LocalEntryNotFoundError", (Exception,), {})
        errors.LocalEntryNotFoundError = self.cache_miss
        self.download = hub.hf_hub_download = MagicMock(
            side_effect=lambda repo, filename, **kwargs: str(Path("snapshot") / filename)
        )
        self.enterContext(patch.dict(sys.modules, {
            "huggingface_hub": hub, "huggingface_hub.errors": errors,
        }))

    def test_complete_cache_requires_no_network_or_optional_file_probes(self):
        self.assertEqual(_source_snapshot(), Path("snapshot"))
        self.assertEqual(self.download.call_count, len(MODEL_ARTIFACTS))
        for call, filename in zip(self.download.call_args_list, MODEL_ARTIFACTS, strict=True):
            self.assertEqual(call.args, (MODEL_ID, filename))
            self.assertEqual(call.kwargs, {"revision": MODEL_REVISION, "local_files_only": True})

    def test_only_missing_artifact_is_downloaded_from_pinned_revision(self):
        def download(repo, filename, **kwargs):
            if filename == "model.safetensors" and kwargs.get("local_files_only"):
                raise self.cache_miss("missing weights")
            return str(Path("snapshot") / filename)

        self.download.side_effect = download
        self.assertEqual(_source_snapshot(), Path("snapshot"))
        network_calls = [
            call for call in self.download.call_args_list if not call.kwargs.get("local_files_only")
        ]
        self.assertEqual(len(network_calls), 1)
        self.assertEqual(network_calls[0].args, (MODEL_ID, "model.safetensors"))
        self.assertEqual(network_calls[0].kwargs, {"revision": MODEL_REVISION})

    def test_download_failure_propagates_without_retry_loop(self):
        self.download.side_effect = [self.cache_miss("missing config"), OSError("offline")]
        with self.assertRaisesRegex(OSError, "offline"):
            _source_snapshot()
        self.assertEqual(self.download.call_count, 2)

    def test_cache_read_error_is_not_treated_as_permission_to_download(self):
        self.download.side_effect = PermissionError("cache unreadable")
        with self.assertRaises(PermissionError):
            _source_snapshot()
        self.assertEqual(self.download.call_count, 1)

    def test_inconsistent_snapshot_paths_rejected(self):
        self.download.side_effect = [
            str(Path(str(index)) / filename) for index, filename in enumerate(MODEL_ARTIFACTS)
        ]
        with self.assertRaisesRegex(ValueError, "same cached snapshot"):
            _source_snapshot()


if __name__ == "__main__":
    unittest.main()
