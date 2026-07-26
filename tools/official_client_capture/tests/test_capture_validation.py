"""抓包边界与传输形态的离线门禁测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.official_client_capture.capture import (
    _client_info,
    _validate_mitm_shape,
    _validate_static_file,
)
from tools.official_client_capture.capturelib.model import (
    ConfigurationError,
    build_campaign_plan,
)


class CaptureShapeValidationTest(unittest.TestCase):
    def _case(self, transport: str):
        plan = build_campaign_plan(
            task="api",
            batch_id="shape-test",
            scenarios=("s1",),
            evidence_modes=("mitm",),
            sub2api_base_url="https://gateway.example.com",
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        return next(
            case
            for case in plan.cases
            if case.product == "codex" and case.transport == transport
        )

    @staticmethod
    def _http_exchange(method: str = "POST") -> dict[str, object]:
        return {
            "kind": "http_exchange",
            "request": {
                "method": method,
                "path": "/v1/responses",
                "http_version": "HTTP/2",
            },
        }

    @staticmethod
    def _client_ws_frame() -> dict[str, object]:
        return {
            "kind": "websocket_frame",
            "from_client": True,
            "path": "/v1/responses",
        }

    def test_http_requires_post_and_rejects_websocket_frames(self) -> None:
        case = self._case("http")
        _validate_mitm_shape(case, {"records": [self._http_exchange()]})

        with self.assertRaises(RuntimeError):
            _validate_mitm_shape(case, {"records": [self._http_exchange("GET")]})
        with self.assertRaises(RuntimeError):
            _validate_mitm_shape(
                case,
                {
                    "records": [
                        self._http_exchange(),
                        self._client_ws_frame(),
                    ]
                },
            )

    def test_websocket_requires_client_frame_and_rejects_http_fallback(self) -> None:
        case = self._case("ws")
        _validate_mitm_shape(
            case,
            {"records": [self._http_exchange("GET"), self._client_ws_frame()]},
        )

        with self.assertRaises(RuntimeError):
            _validate_mitm_shape(case, {"records": [self._http_exchange("GET")]})
        with self.assertRaises(RuntimeError):
            _validate_mitm_shape(
                case,
                {
                    "records": [
                        self._http_exchange("GET"),
                        self._client_ws_frame(),
                        self._http_exchange("POST"),
                    ]
                },
            )

    def test_client_version_and_hash_are_exactly_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claude = Path(directory) / "claude"
            codex = Path(directory) / "codex"
            for path in (claude, codex):
                path.write_text("binary", encoding="utf-8")
                path.chmod(0o700)
            hashes = ["a" * 64, "b" * 64]
            with patch(
                "tools.official_client_capture.capture._command_output",
                side_effect=["2.1.220 (Claude Code)", "codex-cli 0.145.0"],
            ), patch(
                "tools.official_client_capture.capture.file_sha256",
                side_effect=hashes,
            ):
                result = _client_info(
                    claude_bin=claude,
                    codex_bin=codex,
                    expected_claude_version="2.1.220",
                    expected_codex_version="0.145.0",
                    expected_claude_sha256=hashes[0],
                    expected_codex_sha256=hashes[1],
                    api_key_env="SUB2API_CAPTURE_API_KEY",
                )
            self.assertEqual(result["claude"]["sha256"], hashes[0])

            with patch(
                "tools.official_client_capture.capture._command_output",
                side_effect=["2.1.2200 (Claude Code)", "codex-cli 0.145.0"],
            ), self.assertRaises(ConfigurationError):
                _client_info(
                    claude_bin=claude,
                    codex_bin=codex,
                    expected_claude_version="2.1.220",
                    expected_codex_version="0.145.0",
                    expected_claude_sha256=hashes[0],
                    expected_codex_sha256=hashes[1],
                    api_key_env="SUB2API_CAPTURE_API_KEY",
                )

    def test_static_security_asset_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hook.py"
            target.write_text("pass\n", encoding="utf-8")
            link = Path(directory) / "hook-link.py"
            link.symlink_to(target)
            with self.assertRaises(ConfigurationError):
                _validate_static_file(link, "Codex hook", executable=False)


if __name__ == "__main__":
    unittest.main()
