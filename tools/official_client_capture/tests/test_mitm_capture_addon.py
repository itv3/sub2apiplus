from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HAS_MITMPROXY = importlib.util.find_spec("mitmproxy") is not None
HAS_ZSTANDARD = importlib.util.find_spec("zstandard") is not None


@unittest.skipUnless(HAS_MITMPROXY and HAS_ZSTANDARD, "需要抓包容器依赖")
class MitmCaptureAddonTests(unittest.TestCase):
    def test_zstd_request_body_is_decoded_before_json_parsing(self) -> None:
        import zstandard

        source = (
            Path(__file__).resolve().parents[1] / "addons" / "mitm_capture.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "CAPTURE_TASK": "oauth",
                "CAPTURE_BOUNDARY": "client_to_upstream",
                "CAPTURE_RUN_ID": "test-run",
                "CAPTURE_SUBJECT": "codex-http",
                "CAPTURE_SCENARIO": "s1",
                "CAPTURE_OUTPUT_DIR": directory,
                "CAPTURE_TARGET_HOSTS": "chatgpt.com",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                spec = importlib.util.spec_from_file_location(
                    "official_client_capture_mitm_test", source
                )
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

            payload = {"model": "gpt-test", "stream": True}
            raw = json.dumps(payload).encode("utf-8")
            compressed = zstandard.ZstdCompressor(level=3).compress(raw)
            summary = module._body_summary(compressed, "zstd")

            self.assertEqual(summary["json"], payload)
            self.assertEqual(summary["decoded_length"], len(raw))
            self.assertEqual(summary["content_encoding"], "zstd")
            self.assertEqual(summary["decode_error"], "")


if __name__ == "__main__":
    unittest.main()
