"""Opt-in real CPU inference: set VITRINE_TEST_MODEL to a seeded local folder."""

import hashlib
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch

from vision.detector import Detector, validate_model_manifest
from vision import worker as w
from vision.tests.test_worker import JPEG, NOW, clock_document


@unittest.skipUnless(os.environ.get("VITRINE_TEST_MODEL"), "Set VITRINE_TEST_MODEL for real offline inference")
class OfflineRuntimeTests(unittest.TestCase):
    def test_real_cpu_model_measures_the_uploaded_jpeg_without_network(self):
        import torch

        model_path = Path(os.environ["VITRINE_TEST_MODEL"]).resolve()
        validate_model_manifest(model_path)
        self.assertIsNone(torch.version.cuda)
        store = object.__new__(w.AzureStore)
        store.media = Mock()
        store.container = Mock()
        store.read_clock = lambda: clock_document()
        store.read_request = lambda: None
        with (
            patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")),
            patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")),
        ):
            detector = Detector(model_path)
            measured = []
            original_detect = detector.detect

            def detect(image):
                measured.append((image.size, image.mode, image.tobytes()))
                return original_detect(image)

            detector.detect = detect
            worker = w.Worker(store, Mock(), detector, now=lambda: NOW, extractor=lambda *_: JPEG)
            self.assertEqual(worker.tick(), "ready")
            self.assertTrue(worker.health.snapshot()[0])
        blob = store.media.get_blob_client.return_value
        blob.upload_blob.assert_called_once()
        args, kwargs = blob.upload_blob.call_args
        self.assertIs(args[0], JPEG)
        image = w.decode_jpeg(args[0])
        self.assertEqual(measured, [(image.size, image.mode, image.tobytes())])
        document = store.container.upsert_item.call_args.args[0]
        observation = document["observation"]
        self.assertEqual(kwargs["metadata"], {"observationid": observation["id"]})
        self.assertEqual(document["frameSha256"], hashlib.sha256(JPEG).hexdigest())
        self.assertEqual(observation["people"], len(observation["detections"]))
        self.assertIsNone(observation["error"])
        self.assertEqual(next(detector.model.parameters()).device.type, "cpu")
        self.assertLessEqual(torch.get_num_threads(), 4)


if __name__ == "__main__":
    unittest.main()
