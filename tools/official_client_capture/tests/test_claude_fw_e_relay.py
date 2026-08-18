"""Claude FW-E 受管 R 通道的恢复、脱敏与双轨闭集测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.identity import (
    CAPTURE_SOURCE_RELATIVE_PATHS,
)
from tools.official_client_capture.claude_fw_e_relay import (
    HostsOverride,
    RelayEvidenceError,
    _build_index,
    _scrub_relay,
    _validate_relay_integrity,
)
from tools.official_client_capture.capturelib.security import scan_for_secrets


ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = ROOT / "tools/official_client_capture"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def relay_manifest(version: str, binary_sha: str, source_sha: str, probe: str) -> dict:
    return {
        "schema_version": "claude-code-fw-e-relay-run/v1",
        "status": "complete",
        "probe_id": probe,
        "scenario": "s1",
        "injected_probe_env": {},
        "client": {
            "version": f"{version} (Claude Code)",
            "sha256": binary_sha,
        },
        "runtime": {
            "capture_tools": {"execution_sources": {"sha256": source_sha}},
            "host_runtime_receipt": {
                "repo_digest_verified": True,
                "container_runtime_binding": {"verified": True},
                "runtime_image_reference": "capture@sha256:" + "3" * 64,
            },
        },
        "cleanup": {"relay_stopped": True, "hosts_restored": True},
        "secret_scan": {"passed": True, "matches": []},
        "m_binding": {"complete": True},
        "relay_integrity": {"result": "passed"},
    }


def empty_handshake_connection(connection_id: int = 2) -> dict:
    """构造唯一允许排除的无应用字节 TLS 握手终止连接。"""

    return {
        "connection_id": connection_id,
        "client_alpn_offer": ["http/1.1"],
        "alpn_source": "assumed",
        "client_alpn": None,
        "sni": "api.anthropic.com",
        "upstream_alpn": "http/1.1",
        "error": "ALPN 不一致 client=None upstream=http/1.1",
        "valid": False,
        "bytes": {},
        "sha256": {},
        "segments": [],
        "opened_at_unix_ms": 1000,
        "closed_at_unix_ms": 1100,
    }


def relay_shutdown_handshake_connection(connection_id: int = 3) -> dict:
    """构造 relay 停机时取消、尚未进入上游 ALPN 的空握手连接。"""

    value = empty_handshake_connection(connection_id)
    for key in ("upstream_alpn", "error", "valid"):
        value.pop(key)
    return value


class ClaudeFWERelayTests(unittest.TestCase):
    def test_execution_source_binds_relay_and_scrubber(self) -> None:
        self.assertIn("claude_fw_e_relay.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("upstream_byte_relay.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("scrub_raw_bytes.py", CAPTURE_SOURCE_RELATIVE_PATHS)

    def test_hosts_override_restores_exact_bytes_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = root / "hosts"
            before = b"127.0.0.1 localhost\n192.0.2.10 internal.example\n"
            hosts.write_bytes(before)
            backup = root / "recovery/hosts.before"
            override = HostsOverride(hosts, "api.anthropic.com", backup, "run-a")
            with self.assertRaisesRegex(RuntimeError, "probe failure"):
                with override:
                    self.assertIn(b"official-client-fw-e-relay", hosts.read_bytes())
                    raise RuntimeError("probe failure")
            self.assertEqual(hosts.read_bytes(), before)
            self.assertEqual(backup.read_bytes(), before)
            self.assertTrue(override.record["restored"])
            self.assertEqual(
                override.record["before_sha256"],
                override.record["after_sha256"],
            )

    def test_scrubbing_preserves_wire_and_removes_live_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            redacted = root / "relay"
            raw.mkdir()
            token = "eyJ" + "A" * 80
            request = (
                b"POST /v1/messages?beta=true HTTP/1.1\r\n"
                b"Host: api.anthropic.com\r\n"
                + f"Authorization: Bearer {token}\r\n".encode("ascii")
                + b"Content-Length: 0\r\n\r\n"
            )
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
            request_path = raw / "conn001.client_to_upstream.bin"
            response_path = raw / "conn001.upstream_to_client.bin"
            request_path.write_bytes(request)
            response_path.write_bytes(response)
            write_json(
                raw / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "connections": [
                        {
                            "connection_id": 1,
                            "valid": True,
                            "client_alpn": "http/1.1",
                            "upstream_alpn": "http/1.1",
                            "bytes": {
                                "client_to_upstream": len(request),
                                "upstream_to_client": len(response),
                            },
                            "sha256": {
                                "client_to_upstream": hashlib.sha256(request).hexdigest(),
                                "upstream_to_client": hashlib.sha256(response).hexdigest(),
                            },
                        }
                    ],
                },
            )
            result = _scrub_relay(TOOL_ROOT, raw, redacted, root / "scrub.log")
            self.assertTrue(result["verified"])
            self.assertFalse(raw.exists())
            rewritten = (redacted / request_path.name).read_bytes()
            self.assertEqual(len(rewritten), len(request))
            self.assertNotIn(token.encode("ascii"), rewritten)
            self.assertEqual(
                _validate_relay_integrity(redacted)["messages_request_count"], 1
            )
            self.assertTrue(
                scan_for_secrets(root, {"access_token": token})["passed"]
            )

    def test_integrity_excludes_only_exact_empty_handshake_termination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = b"POST /v1/messages?beta=true HTTP/1.1\r\nContent-Length: 0\r\n\r\n"
            response = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
            (root / "conn001.client_to_upstream.bin").write_bytes(request)
            (root / "conn001.upstream_to_client.bin").write_bytes(response)
            valid = {
                "connection_id": 1,
                "client_alpn": "http/1.1",
                "upstream_alpn": "http/1.1",
                "valid": True,
                "bytes": {
                    "client_to_upstream": len(request),
                    "upstream_to_client": len(response),
                },
                "sha256": {
                    "client_to_upstream": hashlib.sha256(request).hexdigest(),
                    "upstream_to_client": hashlib.sha256(response).hexdigest(),
                },
            }
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [
                        empty_handshake_connection(),
                        relay_shutdown_handshake_connection(),
                        valid,
                    ],
                },
            )

            result = _validate_relay_integrity(root)

            self.assertEqual(result["connection_count"], 1)
            self.assertEqual(result["total_connection_count"], 3)
            self.assertEqual(result["excluded_handshake_connection_count"], 2)
            self.assertEqual(
                result["excluded_connections"],
                [
                    {
                        "connection_id": 2,
                        "reason": "client_tls_handshake_terminated_before_application_data",
                        "manifest_error": "ALPN 不一致 client=None upstream=http/1.1",
                    },
                    {
                        "connection_id": 3,
                        "reason": "relay_shutdown_terminated_handshake_before_application_data",
                        "manifest_error": None,
                    },
                ],
            )

    def test_integrity_rejects_near_miss_invalid_connections(self) -> None:
        mutations = {
            "非空字节声明": ("bytes", {"client_to_upstream": 1}),
            "非空分段": (
                "segments",
                [{"direction": "client_to_upstream", "offset": 0, "length": 1}],
            ),
            "客户端协商了协议": ("client_alpn", "http/1.1"),
            "上游协议漂移": ("upstream_alpn", None),
            "错误原因漂移": ("error", "ConnectionResetError"),
            "停机形态混入 valid": ("valid", None),
        }
        for label, (key, value) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                connection = empty_handshake_connection()
                connection[key] = value
                write_json(
                    root / "relay.json",
                    {
                        "schema_version": "byte-relay/v1",
                        "mode": "direct",
                        "upstream_host": "api.anthropic.com",
                        "credential_scrubbing": {"byte_offsets_preserved": True},
                        "connections": [connection],
                    },
                )
                with self.assertRaisesRegex(RelayEvidenceError, "非受管"):
                    _validate_relay_integrity(root)

    def test_integrity_rejects_shutdown_shape_with_unmanaged_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = relay_shutdown_handshake_connection()
            connection["error"] = "CancelledError"
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [connection],
                },
            )
            with self.assertRaisesRegex(RelayEvidenceError, "非受管"):
                _validate_relay_integrity(root)

    def test_integrity_rejects_hidden_bytes_for_excluded_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [empty_handshake_connection()],
                },
            )
            (root / "conn002.client_to_upstream.bin").write_bytes(b"x")
            with self.assertRaisesRegex(RelayEvidenceError, "非受管"):
                _validate_relay_integrity(root)

    def test_index_requires_symmetric_probe_set_and_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "control"
            target = root / "target"
            source_sha = "b" * 64
            write_json(
                control / "baseline/relay-manifest.json",
                relay_manifest("2.1.220", "a" * 64, source_sha, "baseline"),
            )
            write_json(
                target / "baseline/relay-manifest.json",
                relay_manifest("2.1.226", "c" * 64, source_sha, "baseline"),
            )
            arguments = argparse.Namespace(
                control_root=control,
                target_root=target,
                control_version="2.1.220",
                target_version="2.1.226",
                control_sha256="a" * 64,
                target_sha256="c" * 64,
                expected_probes="baseline",
                output=root / "relay-index.json",
            )
            result = _build_index(arguments)
            self.assertEqual(result["result"], "passed")
            self.assertEqual(result["capture_source_sha256"], source_sha)

            drifted = relay_manifest("2.1.226", "c" * 64, "d" * 64, "baseline")
            write_json(target / "baseline/relay-manifest.json", drifted)
            arguments.output = root / "relay-index-drifted.json"
            with self.assertRaisesRegex(RelayEvidenceError, "同一冻结执行源"):
                _build_index(arguments)


if __name__ == "__main__":
    unittest.main()
