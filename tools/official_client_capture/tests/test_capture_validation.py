"""抓包边界与传输形态的离线门禁测试。"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.official_client_capture.capture import (
    _client_info,
    _container_id_from_mountinfo,
    _load_host_runtime_receipt,
    _validate_mitm_shape,
    _validate_static_file,
)
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture.runtime_host_receipt import build_receipt
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

    def test_codex_only_capture_does_not_require_claude_binary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            codex = Path(directory) / "codex"
            codex.write_text("binary", encoding="utf-8")
            codex.chmod(0o700)
            with patch(
                "tools.official_client_capture.capture._command_output",
                return_value="codex-cli 0.146.0",
            ), patch(
                "tools.official_client_capture.capture.file_sha256",
                return_value="b" * 64,
            ):
                result = _client_info(
                    claude_bin=Path(directory) / "missing-claude",
                    codex_bin=codex,
                    expected_claude_version="2.1.220",
                    expected_codex_version="0.146.0",
                    expected_claude_sha256="a" * 64,
                    expected_codex_sha256="b" * 64,
                    api_key_env="SUB2API_CAPTURE_API_KEY",
                    subjects=("codex-http", "codex-ws"),
                )
            self.assertEqual(set(result), {"codex"})

    def test_static_security_asset_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "hook.py"
            target.write_text("pass\n", encoding="utf-8")
            link = Path(directory) / "hook-link.py"
            link.symlink_to(target)
            with self.assertRaises(ConfigurationError):
                _validate_static_file(link, "Codex hook", executable=False)

    def test_host_runtime_receipt_binds_nonce_image_container_and_sources(self) -> None:
        source_identity = {
            "algorithm": "canonical-json-sha256",
            "files": [{"path": "capture.py", "size": 1, "sha256": "a" * 64}],
            "sha256": "b" * 64,
        }
        hostname = "capture-cli"
        container_id = "c" * 64
        nonce = "d" * 64
        runtime_image = "capture.example/tool@sha256:" + "e" * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            payload = {
                "schema_version": "official-client-runtime-host-receipt/v1",
                "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "run_nonce": nonce,
                "container": {
                    "name": "capture-cli",
                    "id": container_id,
                    "hostname": hostname,
                    "started_at_utc": "2026-08-01T00:00:00Z",
                },
                "runtime_image_reference": runtime_image,
                "runtime_image_id": "sha256:" + "1" * 64,
                "repo_digest_verified": True,
                "capture_source_bundle": source_identity,
                "producer": {"sha256": "2" * 64},
                "docker_server": {"version": "29.6.1"},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            path.chmod(0o600)
            arguments = SimpleNamespace(
                host_runtime_receipt=path,
                host_runtime_receipt_sha256=file_sha256(path),
                run_nonce=nonce,
                runtime_image=runtime_image,
                require_complete_m=True,
            )
            with patch(
                "tools.official_client_capture.capture.socket.gethostname",
                return_value=hostname,
            ), patch(
                "tools.official_client_capture.capture._container_id_from_mountinfo",
                return_value=container_id,
            ):
                result = _load_host_runtime_receipt(arguments, source_identity)
            self.assertEqual(result["runtime_image_id"], "sha256:" + "1" * 64)
            self.assertTrue(result["container_runtime_binding"]["verified"])

            arguments.run_nonce = "0" * 64
            with patch(
                "tools.official_client_capture.capture.socket.gethostname",
                return_value=hostname,
            ), patch(
                "tools.official_client_capture.capture._container_id_from_mountinfo",
                return_value=container_id,
            ), self.assertRaises(ConfigurationError):
                _load_host_runtime_receipt(arguments, source_identity)

    def test_container_id_is_recovered_from_docker_managed_mounts(self) -> None:
        container_id = "9" * 64
        with tempfile.TemporaryDirectory() as directory:
            mountinfo = Path(directory) / "mountinfo"
            mountinfo.write_text(
                "1 2 8:1 /var/lib/docker/containers/"
                + container_id
                + "/hostname /etc/hostname rw - ext4 /dev/sda1 rw\n"
                "2 2 8:1 /var/lib/docker/containers/"
                + container_id
                + "/hosts /etc/hosts rw - ext4 /dev/sda1 rw\n",
                encoding="utf-8",
            )
            self.assertEqual(_container_id_from_mountinfo(mountinfo), container_id)

    def test_host_receipt_producer_verifies_repo_digest(self) -> None:
        runtime_image = "capture.example/tool@sha256:" + "a" * 64
        container_id = "b" * 64
        hostname = "capture-cli"
        image_payload = [
            {
                "RepoDigests": [runtime_image],
            }
        ]
        docker_server = {"Version": "29.6.1", "Os": "linux", "Arch": "amd64"}
        tool_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            hosts = Path(directory) / "hosts"
            resolv = Path(directory) / "resolv.conf"
            hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
            resolv.write_text("nameserver 127.0.0.11\n", encoding="utf-8")
            container_payload = [
                {
                    "Id": container_id,
                    "Image": "sha256:" + "c" * 64,
                    "State": {
                        "Running": True,
                        "StartedAt": "2026-08-01T00:00:00Z",
                    },
                    "Config": {"Hostname": hostname},
                    "HostConfig": {"NetworkMode": "capture-network"},
                    "NetworkSettings": {
                        "Networks": {
                            "capture-network": {
                                "NetworkID": "e" * 64,
                                "EndpointID": "f" * 64,
                                "Gateway": "172.20.0.1",
                                "IPAddress": "172.20.0.2",
                            }
                        }
                    },
                    "HostsPath": str(hosts),
                    "ResolvConfPath": str(resolv),
                }
            ]
            with patch(
                "tools.official_client_capture.runtime_host_receipt._docker_json",
                side_effect=[container_payload, image_payload, docker_server],
            ):
                receipt = build_receipt(
                    container="capture-cli",
                    runtime_image=runtime_image,
                    tool_root=tool_root,
                    run_nonce="d" * 64,
                )
        self.assertTrue(receipt["repo_digest_verified"])
        self.assertEqual(receipt["container"]["hostname"], hostname)
        self.assertEqual(
            receipt["container"]["network"]["bindings"][0]["name"],
            "capture-network",
        )
        self.assertEqual(len(receipt["capture_source_bundle"]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
