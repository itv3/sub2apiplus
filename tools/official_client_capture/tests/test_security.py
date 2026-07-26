"""脱敏、权限、manifest 与差异测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.analysis import (
    compare_normalized,
    normalize_mitm_directory,
)
from tools.official_client_capture.capturelib.manifest import Manifest
from tools.official_client_capture.capturelib.model import build_campaign_plan
from tools.official_client_capture.capturelib.security import (
    normalize_json_shape,
    scan_for_secret,
    scrub_known_secret,
    secure_write_text,
)


class SecurityTest(unittest.TestCase):
    def test_secure_write_ignores_permissive_umask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "value.json"
            previous = os.umask(0)
            try:
                secure_write_text(path, "{}\n")
            finally:
                os.umask(previous)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_secret_scan_finds_canary_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secure_write_text(root / "bad.log", "prefix CANARY-SECRET suffix")
            self.assertEqual(scan_for_secret(root, "CANARY-SECRET"), ["bad.log"])
            self.assertEqual(scrub_known_secret(root, "CANARY-SECRET"), ["bad.log"])
            self.assertNotIn(
                "CANARY-SECRET", (root / "bad.log").read_text(encoding="utf-8")
            )

    def test_json_shape_keeps_structure_but_removes_text_and_ids(self) -> None:
        result = normalize_json_shape(
            {
                "model": "model-a",
                "session_id": "session-private",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        self.assertEqual(result["model"], "model-a")
        self.assertEqual(result["session_id"], "<dynamic:str>")
        self.assertEqual(result["messages"][0]["content"], "<text:5>")

    def test_mitm_normalization_redacts_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "_task": "api",
                "_boundary": "official_cli_to_sub2api",
                "_subject": "claude-http",
                "request": {
                    "method": "POST",
                    "host": "gateway.example.com",
                    "path": "/v1/messages",
                    "http_version": "HTTP/1.1",
                    "headers": [
                        ["Authorization", "Bearer CANARY-SECRET"],
                        ["User-Agent", "claude-cli/2.1.220"],
                        [
                            "X-Codex-Turn-Metadata",
                            '{"turn_id":"private-turn-id"}',
                        ],
                        ["X-Codex-Installation-Id", "private-installation-id"],
                    ],
                    "body": {"length": 12, "json": {"content": "hello"}},
                },
                "response": {"status": 200, "http_version": "HTTP/1.1"},
            }
            secure_write_text(root / "claude-http.jsonl", json.dumps(raw) + "\n")
            output = root / "analysis" / "normalized.json"
            payload = normalize_mitm_directory(root, output)
            headers = payload["records"][0]["request"]["headers"]
            self.assertEqual(headers[0], ["authorization", "<secret>"])
            self.assertEqual(headers[2], ["x-codex-turn-metadata", "<dynamic>"])
            self.assertEqual(headers[3], ["x-codex-installation-id", "<dynamic>"])
            self.assertNotIn("CANARY-SECRET", output.read_text(encoding="utf-8"))
            self.assertNotIn("private-turn-id", output.read_text(encoding="utf-8"))
            self.assertNotIn(
                "private-installation-id", output.read_text(encoding="utf-8")
            )

    def test_manifest_never_contains_api_key_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "api-run"
            run_dir.mkdir(mode=0o700)
            plan = build_campaign_plan(
                task="api",
                batch_id="manifest-test",
                scenarios=("s1",),
                evidence_modes=("direct",),
                sub2api_base_url="https://gateway.example.com",
                api_key_env="SUB2API_CAPTURE_API_KEY",
            )
            manifest = Manifest(plan, run_dir)
            manifest.finalize(
                status="complete",
                cleanup_successful=True,
                secret_matches=[],
                secret_scan_scope=["api_runtime_key_value"],
            )
            text = manifest.path.read_text(encoding="utf-8")
            self.assertNotIn("CANARY-SECRET", text)
            self.assertIn("SUB2API_CAPTURE_API_KEY", text)
            self.assertEqual(manifest.path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(manifest.data["secret_scan"]["performed"])

    def test_oauth_manifest_records_value_scan_limitation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "oauth-run"
            run_dir.mkdir(mode=0o700)
            plan = build_campaign_plan(
                task="oauth",
                batch_id="manifest-oauth",
                scenarios=("s1",),
                evidence_modes=("direct",),
                sub2api_base_url=None,
                api_key_env="SUB2API_CAPTURE_API_KEY",
            )
            manifest = Manifest(plan, run_dir)
            manifest.finalize(
                status="complete",
                cleanup_successful=True,
                secret_matches=[],
                secret_scan_scope=[],
                secret_scan_limitation="OAuth 凭据未读入编排器。",
            )
            self.assertFalse(manifest.data["secret_scan"]["performed"])
            self.assertEqual(manifest.data["secret_scan"]["scope"], [])
            self.assertIn("未读入", manifest.data["secret_scan"]["limitation"])

    def test_compare_uses_only_normalized_records(self) -> None:
        baseline = {"records": [{"kind": "http_exchange"}]}
        self.assertTrue(compare_normalized(baseline, baseline)["equal"])
        candidate = {"records": [{"kind": "websocket_frame"}]}
        self.assertFalse(compare_normalized(baseline, candidate)["equal"])


if __name__ == "__main__":
    unittest.main()
