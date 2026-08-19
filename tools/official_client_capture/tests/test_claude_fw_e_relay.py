"""Claude FW-E 受管 R 通道的恢复、脱敏与双轨闭集测试。"""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.capturelib.identity import (
    CAPTURE_SOURCE_RELATIVE_PATHS,
)
from tools.official_client_capture.claude_fw_e_relay import (
    HostsOverride,
    RelayEvidenceError,
    _build_index,
    _build_parser,
    _capture,
    _parse_pcap,
    _scrub_relay,
    _start_pcap,
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


def client_reset_handshake_connection(connection_id: int = 4) -> dict:
    """构造官方并发连接在 TLS 前主动 reset 的精确零字节终态。"""

    return {
        "connection_id": connection_id,
        "client_alpn_offer": ["http/1.1"],
        "alpn_source": "assumed",
        "error": "ConnectionResetError: [Errno 104] Connection reset by peer",
        "valid": False,
        "bytes": {},
        "sha256": {},
        "segments": [],
        "opened_at_unix_ms": 1000,
        "closed_at_unix_ms": 1100,
    }


def synthetic_messages_request(model: str, *, stream: bool | None = True) -> bytes:
    payload = {"model": model, "messages": []}
    if stream is not None:
        payload["stream"] = stream
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return (
        b"POST /v1/messages?beta=true HTTP/1.1\r\n"
        b"Host: api.anthropic.com\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


def write_synthetic_relay(
    root: Path,
    *,
    plan: str,
    requests: list[bytes],
    actions: list[str],
    responses: list[bytes] | None = None,
) -> None:
    responses = responses or [b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"] * len(
        requests
    )
    connections = []
    interventions = []
    for index, (request, response, action) in enumerate(
        zip(requests, responses, actions, strict=True), start=1
    ):
        (root / f"conn{index:03d}.client_to_upstream.bin").write_bytes(request)
        (root / f"conn{index:03d}.upstream_to_client.bin").write_bytes(response)
        connections.append(
            {
                "connection_id": index,
                "request_line": "POST /v1/messages?beta=true HTTP/1.1",
                "client_alpn": "http/1.1",
                "upstream_alpn": "http/1.1",
                "valid": True,
                "production_forwarded": False,
                "bytes": {
                    "client_to_upstream": len(request),
                    "upstream_to_client": len(response),
                },
                "sha256": {
                    "client_to_upstream": hashlib.sha256(request).hexdigest(),
                    "upstream_to_client": hashlib.sha256(response).hexdigest(),
                },
            }
        )
        interventions.append(
            {
                "type": "synthetic_claude_response",
                "profile": "claude-fw-f-v3",
                "plan": plan,
                "action": action,
                "connection_id": index,
                "request_line": "POST /v1/messages?beta=true HTTP/1.1",
                "message_ordinal": index,
                "production_forwarded": False,
            }
        )
    write_json(
        root / "relay.json",
        {
            "schema_version": "byte-relay/v1",
            "mode": "direct",
            "upstream_host": "api.anthropic.com",
            "synthetic_profile": "claude-fw-f-v3",
            "claude_fault_plan": plan,
            "production_forwarding_enabled": False,
            "credential_scrubbing": {"byte_offsets_preserved": True},
            "connections": connections,
        },
    )
    (root / "intervention.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in interventions),
        encoding="utf-8",
    )


class ClaudeFWERelayTests(unittest.TestCase):
    def test_execution_source_binds_relay_and_scrubber(self) -> None:
        self.assertIn("claude_fw_e_complete_campaign.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("claude_fw_f_complete_runner.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("runtime_host_receipt.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("claude_fw_e_relay.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("upstream_byte_relay.py", CAPTURE_SOURCE_RELATIVE_PATHS)
        self.assertIn("scrub_raw_bytes.py", CAPTURE_SOURCE_RELATIVE_PATHS)

    def test_tls_p_channel_parses_target_clienthello(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pcap = Path(directory) / "tls-clienthello.pcap"
            pcap.write_bytes(b"pcap-header-and-one-packet-00")
            with (
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.iter_packets",
                    return_value=[(1, b"packet")],
                ),
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.tcp_payload",
                    return_value=("127.0.0.1", 443, b"clienthello"),
                ),
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.parse_client_hello",
                    return_value=(
                        "api.anthropic.com",
                        [0, 11, 10, 35, 16],
                        [4865, 4866],
                        ["http/1.1"],
                    ),
                ),
            ):
                result = _parse_pcap(pcap, "api.anthropic.com")

            self.assertTrue(result["parsed"])
            self.assertEqual(result["target_client_hello_count"], 1)
            self.assertEqual(
                result["observations"][0]["alpn_offer"], ["http/1.1"]
            )

    def test_tls_pcap_keeps_root_for_private_campaign_directory(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 123
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tcpdump = root / "tcpdump"
            tcpdump.write_bytes(b"binary")
            tcpdump.chmod(0o700)
            with (
                mock.patch(
                    "tools.official_client_capture.claude_fw_e_relay.shutil.which",
                    return_value=str(tcpdump),
                ),
                mock.patch(
                    "tools.official_client_capture.claude_fw_e_relay.subprocess.Popen",
                    return_value=process,
                ) as popen,
                mock.patch(
                    "tools.official_client_capture.claude_fw_e_relay.time.sleep"
                ),
            ):
                returned, receipt = _start_pcap(
                    root / "clienthello.pcap",
                    root / "tcpdump.log",
                )

            command = popen.call_args.args[0]
            self.assertEqual(returned, process)
            self.assertEqual(command[command.index("-Z") + 1], "root")
            self.assertEqual(receipt["privilege_user"], "root")

    def test_tls_p_channel_rejects_wrong_sni(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pcap = Path(directory) / "tls-clienthello.pcap"
            pcap.write_bytes(b"pcap-header-and-one-packet-00")
            with (
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.iter_packets",
                    return_value=[(1, b"packet")],
                ),
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.tcp_payload",
                    return_value=("127.0.0.1", 443, b"clienthello"),
                ),
                mock.patch(
                    "tools.official_client_capture.pcap_clienthello.parse_client_hello",
                    return_value=("wrong.example", [0], [4865], []),
                ),
            ):
                with self.assertRaisesRegex(RelayEvidenceError, "目标 host"):
                    _parse_pcap(pcap, "api.anthropic.com")

    def test_oauth_refresh_integrity_is_one_synthetic_request_and_zero_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b"grant_type=refresh_token&refresh_token=<secret>XXXX"
            request = (
                b"POST /v1/oauth/token HTTP/1.1\r\n"
                b"Host: platform.claude.com\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            response = (
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 25\r\n\r\n"
                b'{"error":"invalid_grant"}'
            )
            (root / "conn001.client_to_upstream.bin").write_bytes(request)
            (root / "conn001.upstream_to_client.bin").write_bytes(response)
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "platform.claude.com",
                    "synthetic_profile": "claude-fw-f-v4",
                    "claude_fault_plan": "oauth-refresh-reject",
                    "production_forwarding_enabled": False,
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [
                        {
                            "connection_id": 1,
                            "client_alpn": "http/1.1",
                            "upstream_alpn": "http/1.1",
                            "valid": True,
                            "production_forwarded": False,
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
            (root / "intervention.jsonl").write_text(
                json.dumps(
                    {
                        "type": "synthetic_claude_response",
                        "profile": "claude-fw-f-v4",
                        "plan": "oauth-refresh-reject",
                        "action": "oauth_refresh_rejected",
                        "connection_id": 1,
                        "request_line": "POST /v1/oauth/token HTTP/1.1",
                        "message_ordinal": 0,
                        "production_forwarded": False,
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            result = _validate_relay_integrity(
                root,
                synthetic_plan="oauth-refresh-reject",
                synthetic_profile="claude-fw-f-v4",
                message_request_expectation="zero",
                target_host="platform.claude.com",
            )

            self.assertEqual(result["messages_request_count"], 0)
            self.assertEqual(
                result["synthetic_plan"]["attempts"][0]["kind"],
                "oauth_refresh",
            )
            self.assertFalse(
                result["synthetic_plan"]["production_forwarding_enabled"]
            )

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
            reset_without_errno = client_reset_handshake_connection(5)
            reset_without_errno["error"] = "ConnectionResetError: "
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
                        client_reset_handshake_connection(),
                        reset_without_errno,
                        valid,
                    ],
                },
            )

            result = _validate_relay_integrity(root)

            self.assertEqual(result["connection_count"], 1)
            self.assertEqual(result["total_connection_count"], 5)
            self.assertEqual(result["excluded_handshake_connection_count"], 4)
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
                    {
                        "connection_id": 4,
                        "reason": "client_reset_transport_before_tls_handshake",
                        "manifest_error": "ConnectionResetError: [Errno 104] Connection reset by peer",
                    },
                    {
                        "connection_id": 5,
                        "reason": "client_reset_transport_before_tls_handshake",
                        "manifest_error": "ConnectionResetError: ",
                    },
                ],
            )

    def test_integrity_rejects_non_exact_client_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = client_reset_handshake_connection()
            connection["error"] = "ConnectionResetError: unexpected"
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

            with self.assertRaisesRegex(RelayEvidenceError, "非受管的无效连接"):
                _validate_relay_integrity(root)

    def test_real_integrity_accepts_exact_selected_alpn_mirroring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wires = (
                (
                    b"GET /api/claude_code/policy_limits HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n",
                    None,
                ),
                (
                    b"POST /v1/messages?beta=true HTTP/1.1\r\nContent-Length: 0\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
                    "http/1.1",
                ),
            )
            connections = []
            for index, (request, response, alpn) in enumerate(wires, start=1):
                (root / f"conn{index:03d}.client_to_upstream.bin").write_bytes(
                    request
                )
                (root / f"conn{index:03d}.upstream_to_client.bin").write_bytes(
                    response
                )
                connections.append(
                    {
                        "connection_id": index,
                        "client_alpn": alpn,
                        "upstream_alpn": alpn,
                        "upstream_alpn_offer": [alpn] if alpn else None,
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
                )
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "mirror_selected_alpn": True,
                    "production_forwarding_enabled": True,
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": connections,
                },
            )

            result = _validate_relay_integrity(root)

            self.assertEqual(result["connection_count"], 2)
            self.assertEqual(result["messages_request_count"], 1)

    def test_real_integrity_accepts_exact_complete_one_sided_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = (
                b"GET /api/claude_code/policy_limits HTTP/1.1\r\n"
                b"Host: api.anthropic.com\r\n"
                b"Connection: keep-alive\r\n\r\n"
            )
            (root / "conn001.client_to_upstream.bin").write_bytes(request)
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "mirror_selected_alpn": True,
                    "production_forwarding_enabled": True,
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [
                        {
                            "connection_id": 1,
                            "client_alpn": None,
                            "upstream_alpn": None,
                            "upstream_alpn_offer": None,
                            "valid": True,
                            "termination_reason": (
                                "relay_shutdown_after_complete_client_request_"
                                "before_upstream_response"
                            ),
                            "relay_stop_requested": True,
                            "bytes": {"client_to_upstream": len(request)},
                            "sha256": {
                                "client_to_upstream": hashlib.sha256(
                                    request
                                ).hexdigest()
                            },
                            "segments": [
                                {
                                    "direction": "client_to_upstream",
                                    "offset": 0,
                                    "length": len(request),
                                }
                            ],
                        }
                    ],
                },
            )

            result = _validate_relay_integrity(
                root,
                message_request_expectation="zero",
            )

            self.assertEqual(result["connection_count"], 1)
            self.assertEqual(result["one_sided_shutdown_connection_count"], 1)
            self.assertEqual(result["messages_request_count"], 0)
            self.assertEqual(
                result["connections"][0]["request_lines"],
                ["GET /api/claude_code/policy_limits HTTP/1.1"],
            )

    def test_real_integrity_rejects_incomplete_one_sided_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = (
                b"POST /v1/messages?beta=true HTTP/1.1\r\n"
                b"Content-Length: 4\r\n\r\n{}"
            )
            (root / "conn001.client_to_upstream.bin").write_bytes(request)
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "mirror_selected_alpn": True,
                    "production_forwarding_enabled": True,
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [
                        {
                            "connection_id": 1,
                            "client_alpn": None,
                            "upstream_alpn": None,
                            "upstream_alpn_offer": None,
                            "valid": True,
                            "termination_reason": (
                                "relay_shutdown_after_complete_client_request_"
                                "before_upstream_response"
                            ),
                            "relay_stop_requested": True,
                            "bytes": {"client_to_upstream": len(request)},
                            "sha256": {
                                "client_to_upstream": hashlib.sha256(
                                    request
                                ).hexdigest()
                            },
                            "segments": [
                                {
                                    "direction": "client_to_upstream",
                                    "offset": 0,
                                    "length": len(request),
                                }
                            ],
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(RelayEvidenceError, "body 不完整"):
                _validate_relay_integrity(root)

    def test_local_rejection_requires_exactly_zero_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = (
                b"GET /api/claude_code/settings HTTP/1.1\r\n"
                b"Host: api.anthropic.com\r\n\r\n"
            )
            response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
            (root / "conn001.client_to_upstream.bin").write_bytes(request)
            (root / "conn001.upstream_to_client.bin").write_bytes(response)
            write_json(
                root / "relay.json",
                {
                    "schema_version": "byte-relay/v1",
                    "mode": "direct",
                    "upstream_host": "api.anthropic.com",
                    "mirror_selected_alpn": True,
                    "production_forwarding_enabled": True,
                    "credential_scrubbing": {"byte_offsets_preserved": True},
                    "connections": [
                        {
                            "connection_id": 1,
                            "client_alpn": None,
                            "upstream_alpn": None,
                            "upstream_alpn_offer": None,
                            "valid": True,
                            "bytes": {
                                "client_to_upstream": len(request),
                                "upstream_to_client": len(response),
                            },
                            "sha256": {
                                "client_to_upstream": hashlib.sha256(
                                    request
                                ).hexdigest(),
                                "upstream_to_client": hashlib.sha256(
                                    response
                                ).hexdigest(),
                            },
                        }
                    ],
                },
            )

            result = _validate_relay_integrity(
                root, message_request_expectation="zero"
            )

            self.assertEqual(result["messages_request_count"], 0)
            self.assertEqual(result["message_request_expectation"], "zero")
            with self.assertRaisesRegex(RelayEvidenceError, "没有官方 messages"):
                _validate_relay_integrity(root)

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

    def test_synthetic_integrity_closes_retry_plan_without_production_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_relay(
                root,
                plan="retry-500",
                requests=[
                    synthetic_messages_request("claude-sonnet-5"),
                    synthetic_messages_request("claude-sonnet-5"),
                ],
                actions=["retry-500_fault", "retry-500_success"],
            )

            result = _validate_relay_integrity(
                root, synthetic_plan="retry-500"
            )

            self.assertEqual(result["messages_request_count"], 2)
            self.assertEqual(
                result["synthetic_plan"]["message_attempt_count"], 2
            )
            self.assertFalse(
                result["synthetic_plan"]["production_forwarding_enabled"]
            )

    def test_synthetic_integrity_accepts_zero_byte_controlled_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_relay(
                root,
                plan="disconnect-retry",
                requests=[
                    synthetic_messages_request("claude-sonnet-5"),
                    synthetic_messages_request("claude-sonnet-5"),
                ],
                responses=[
                    b"",
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
                ],
                actions=["disconnect_without_response", "disconnect_retry_success"],
            )

            result = _validate_relay_integrity(
                root, synthetic_plan="disconnect-retry"
            )

            self.assertEqual(result["connection_count"], 2)

    def test_synthetic_integrity_records_nonstream_fallback_as_omitted_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_relay(
                root,
                plan="stream-404-fallback",
                requests=[
                    synthetic_messages_request("claude-sonnet-5", stream=True),
                    synthetic_messages_request("claude-sonnet-5", stream=None),
                ],
                actions=["stream_404", "nonstream_fallback_success"],
            )

            result = _validate_relay_integrity(
                root, synthetic_plan="stream-404-fallback"
            )

            attempts = result["synthetic_plan"]["attempts"]
            self.assertTrue(attempts[0]["stream_present"])
            self.assertFalse(attempts[1]["stream_present"])

    def test_synthetic_integrity_closes_disabled_fallback_and_timeout(self) -> None:
        cases = (
            (
                "stream-interrupt-no-fallback",
                "stream_interrupted",
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n\r\n",
            ),
            ("stall", "stall_without_response", b""),
        )
        for plan, action, response in cases:
            with self.subTest(plan=plan), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_synthetic_relay(
                    root,
                    plan=plan,
                    requests=[synthetic_messages_request("claude-sonnet-5")],
                    responses=[response],
                    actions=[action],
                )

                result = _validate_relay_integrity(root, synthetic_plan=plan)

                self.assertEqual(
                    result["synthetic_plan"]["message_attempt_count"], 1
                )

    def test_synthetic_integrity_rejects_forwarding_or_unknown_intervention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_synthetic_relay(
                root,
                plan="nonretry-400",
                requests=[synthetic_messages_request("claude-sonnet-5")],
                actions=["nonretry_400"],
            )
            manifest = json.loads((root / "relay.json").read_text())
            manifest["production_forwarding_enabled"] = True
            write_json(root / "relay.json", manifest)
            with self.assertRaisesRegex(RelayEvidenceError, "合成计划"):
                _validate_relay_integrity(root, synthetic_plan="nonretry-400")

            manifest["production_forwarding_enabled"] = False
            write_json(root / "relay.json", manifest)
            intervention = json.loads(
                (root / "intervention.jsonl").read_text().strip()
            )
            intervention["type"] = "unmanaged"
            (root / "intervention.jsonl").write_text(
                json.dumps(intervention) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RelayEvidenceError, "数量不一致|非受管"):
                _validate_relay_integrity(root, synthetic_plan="nonretry-400")

    def test_fw_f_v3_dry_run_has_no_live_or_production_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-run"
            arguments = _build_parser().parse_args(
                [
                    "capture",
                    "--dry-run",
                    "--run-id",
                    "v3-retry-500",
                    "--probe-id",
                    "v3-retry-500",
                    "--fw-f-probe",
                    "v3-retry-500",
                    "--output-root",
                    str(output),
                    "--claude-bin",
                    "/opt/claude",
                    "--expected-version",
                    "2.1.226",
                    "--expected-sha256",
                    "a" * 64,
                    "--claude-credentials-file",
                    "/run/credentials.json",
                    "--ca-signing-pem",
                    "/run/ca.pem",
                    "--ca-cert",
                    "/run/ca.crt",
                    "--host-runtime-receipt",
                    "/run/receipt.json",
                    "--host-runtime-receipt-sha256",
                    "b" * 64,
                    "--run-nonce",
                    "c" * 64,
                ]
            )

            result = _capture(arguments)

            self.assertEqual(result["capture_mode"], "fw-f-v3")
            self.assertEqual(result["response_plan"], "retry-500")
            self.assertEqual(result["synthetic_success_marker"], "FW_F_V3_OK")
            self.assertFalse(result["live_requests"])
            self.assertFalse(result["production_forwarding_enabled"])
            self.assertIsNone(result["upstream_ip"])
            self.assertEqual(result["message_request_expectation"], "at-least-one")

    def test_fw_f_v4_oauth_refresh_is_synthetic_and_host_is_frozen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-run"
            arguments = _build_parser().parse_args(
                [
                    "capture",
                    "--dry-run",
                    "--run-id",
                    "v4-oauth-refresh",
                    "--probe-id",
                    "v4-oauth-refresh",
                    "--fw-f-v4-probe",
                    "v4-oauth-refresh",
                    "--output-root",
                    str(output),
                    "--claude-bin",
                    "/opt/claude",
                    "--expected-version",
                    "2.1.226",
                    "--expected-sha256",
                    "a" * 64,
                    "--claude-credentials-file",
                    "/run/credentials.json",
                    "--ca-signing-pem",
                    "/run/ca.pem",
                    "--ca-cert",
                    "/run/ca.crt",
                    "--host-runtime-receipt",
                    "/run/receipt.json",
                    "--host-runtime-receipt-sha256",
                    "b" * 64,
                    "--run-nonce",
                    "c" * 64,
                ]
            )

            result = _capture(arguments)

            self.assertEqual(result["capture_mode"], "fw-f-v4")
            self.assertEqual(result["upstream_host"], "platform.claude.com")
            self.assertEqual(result["response_plan"], "oauth-refresh-reject")
            self.assertEqual(result["synthetic_success_marker"], "FW_F_V4_OK")
            self.assertEqual(result["message_request_expectation"], "zero")
            self.assertFalse(result["live_requests"])
            self.assertFalse(result["production_forwarding_enabled"])

    def test_fw_f_v4_replay_keeps_v3_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = _build_parser().parse_args(
                [
                    "capture",
                    "--dry-run",
                    "--run-id",
                    "v4-replay-disconnect-retry",
                    "--probe-id",
                    "v4-replay-disconnect-retry",
                    "--fw-f-v4-probe",
                    "v4-replay-disconnect-retry",
                    "--output-root",
                    str(Path(directory) / "new-run"),
                    "--claude-bin",
                    "/opt/claude",
                    "--expected-version",
                    "2.1.226",
                    "--expected-sha256",
                    "a" * 64,
                    "--claude-credentials-file",
                    "/run/credentials.json",
                    "--ca-signing-pem",
                    "/run/ca.pem",
                    "--ca-cert",
                    "/run/ca.crt",
                    "--host-runtime-receipt",
                    "/run/receipt.json",
                    "--host-runtime-receipt-sha256",
                    "b" * 64,
                    "--run-nonce",
                    "c" * 64,
                ]
            )

            result = _capture(arguments)

            self.assertEqual(result["response_plan"], "disconnect-retry")
            self.assertEqual(result["synthetic_success_marker"], "FW_F_V3_OK")
            self.assertFalse(result["production_forwarding_enabled"])

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
