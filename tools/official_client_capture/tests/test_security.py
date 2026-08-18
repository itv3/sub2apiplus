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
    argv_manifest_view,
    normalize_json_shape,
    scan_for_secret,
    scan_for_secrets,
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

    def test_multi_secret_scan_includes_binary_and_never_echoes_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "traffic.pcap").write_bytes(b"prefix\x00SECOND-SECRET\xff")
            secure_write_text(root / "safe.log", "nothing sensitive")
            report = scan_for_secrets(
                root,
                {
                    "oauth_access": "FIRST-SECRET",
                    "oauth_refresh": "SECOND-SECRET",
                },
            )
            self.assertTrue(report["performed"])
            self.assertFalse(report["passed"])
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(report["matches"][0]["path"], "traffic.pcap")
            self.assertEqual(
                report["matches"][0]["secret_sources"], ["oauth_refresh"]
            )
            self.assertNotIn("FIRST-SECRET", str(report))
            self.assertNotIn("SECOND-SECRET", str(report))

    def test_secret_scan_detects_value_across_stream_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = b"BOUNDARY-SECRET-VALUE"
            prefix = b"x" * (1024 * 1024 - 7)
            (root / "large.bin").write_bytes(prefix + secret + b"tail")
            report = scan_for_secrets(root, {"oauth_access": secret.decode("ascii")})
            self.assertFalse(report["passed"])
            self.assertEqual(report["matches"][0]["path"], "large.bin")
            self.assertEqual(report["byte_count"], len(prefix) + len(secret) + 4)

    def test_argv_manifest_redacts_inline_and_known_secret_values(self) -> None:
        view = argv_manifest_view(
            [
                "/bin/client",
                "--token=CANARY-SECRET",
                "--model",
                "model-a",
            ],
            {"oauth_access": "CANARY-SECRET"},
        )
        self.assertNotIn("CANARY-SECRET", str(view))
        self.assertEqual(
            view["argv_redacted"][1], "--token=<redacted-sensitive-argument>"
        )
        self.assertEqual(len(view["argv_sha256"]), 64)

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

    def test_mitm_normalization_verifies_turn_state_without_persisting_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = "private-turn-state"
            records = [
                {
                    "_task": "oauth",
                    "_boundary": "sub2api_egress",
                    "_subject": "codex-ws",
                    "request": {
                        "method": "GET",
                        "host": "chatgpt.com",
                        "path": "/backend-api/codex/responses",
                        "http_version": "HTTP/1.1",
                        "headers": [],
                        "body": {"length": 0, "json": None},
                    },
                    "response": {
                        "status": 101,
                        "http_version": "HTTP/1.1",
                        "headers": [["x-codex-turn-state", state]],
                    },
                },
                {
                    "_websocket": True,
                    "_task": "oauth",
                    "_boundary": "sub2api_egress",
                    "_subject": "codex-ws",
                    "from_client": True,
                    "host": "chatgpt.com",
                    "path": "/backend-api/codex/responses",
                    "length": 10,
                    "json": {
                        "type": "response.create",
                        "client_metadata": {"x-codex-turn-state": state},
                    },
                },
            ]
            secure_write_text(
                root / "codex-http.jsonl",
                "".join(json.dumps(item) + "\n" for item in records),
            )
            output = root / "analysis" / "normalized.json"
            payload = normalize_mitm_directory(root, output)
            self.assertEqual(
                payload["turn_state_lifecycle"],
                {
                    "response_state_count": 1,
                    "matched_client_frame_count": 1,
                    "unmatched_client_frame_count": 0,
                },
            )
            self.assertNotIn(state, output.read_text(encoding="utf-8"))

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

    def test_manifest_complete_m_requires_every_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "oauth-run"
            run_dir.mkdir(mode=0o700)
            plan = build_campaign_plan(
                task="oauth",
                batch_id="manifest-complete-m",
                scenarios=("s1",),
                evidence_modes=("mitm",),
                sub2api_base_url=None,
                api_key_env="SUB2API_CAPTURE_API_KEY",
                subjects=("claude-http",),
                oauth_claude_token_env="CLAUDE_CAPTURE_OAUTH_TOKEN",
            )
            manifest = Manifest(plan, run_dir)
            manifest.set_runtime(
                {
                    "runtime_image_verified": True,
                    "capture_tools": {
                        "execution_sources": {"sha256": "a" * 64}
                    },
                }
            )
            manifest.add_case_result(
                {
                    "scenario_result": {
                        "invocation": {
                            "argv_sha256": "b" * 64,
                            "environment": {"sha256": "c" * 64},
                        }
                    }
                }
            )
            manifest.finalize(
                status="complete",
                cleanup_successful=True,
                secret_matches=[],
                secret_scan_report={
                    "performed": True,
                    "passed": True,
                    "scope": ["oauth_access"],
                    "matches": [],
                    "limitation": None,
                },
                m_binding_required=True,
            )
            self.assertTrue(manifest.data["m_binding"]["complete"])

    def test_compare_uses_only_normalized_records(self) -> None:
        baseline = {"records": [{"kind": "http_exchange"}]}
        self.assertTrue(compare_normalized(baseline, baseline)["equal"])
        candidate = {"records": [{"kind": "websocket_frame"}]}
        self.assertFalse(compare_normalized(baseline, candidate)["equal"])


if __name__ == "__main__":
    unittest.main()
