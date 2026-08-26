"""字节中继 WS 受控注入的无网络测试。"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.official_client_capture.candidate_evidence_guard import scan_files_for_secrets
from tools.official_client_capture.relay_extract import parse_ws_frames
from tools.official_client_capture.scrub_raw_bytes import (
    GENERIC_TOKEN,
    count_unscrubbed_credentials,
    rewrite_relay_manifest,
    scrub,
)
from tools.official_client_capture.upstream_byte_relay import (
    _SYNTHETIC_CLAUDE_PLANS,
    _synthetic_aux_response,
    _SYNTHETIC_AUX_CFUV_COOKIE,
    _SYNTHETIC_AUX_TURN_STATE,
    _SYNTHETIC_FILE_HOST,
    _SYNTHETIC_FILE_ID,
    _SYNTHETIC_FILE_QUERY,
    _SYNTHETIC_CORE_CFUV_COOKIE,
    _SYNTHETIC_CORE_TURN_STATE,
    _SYNTHETIC_REALTIME_CALL_ID,
    _SyntheticCoreWebSocketDecoder,
    _annotate_relay_stop_after_client_request,
    _decode_client_text_frame,
    _encode_server_text_frame,
    _redact_oauth_refresh_body,
    _should_synthesize_realtime_call,
    _synthetic_claude_response,
    _synthetic_aux_response,
    _synthetic_core_response,
    _upstream_alpn_offer,
)


def _masked_client_frame(
    payload: bytes,
    *,
    opcode: int,
    fin: bool,
    rsv1: bool = False,
) -> bytes:
    first = opcode | (0x80 if fin else 0) | (0x40 if rsv1 else 0)
    mask = b"\x11\x22\x33\x44"
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    if len(payload) <= 125:
        head = bytes((first, 0x80 | len(payload)))
    elif len(payload) <= 0xFFFF:
        head = bytes((first, 0x80 | 126)) + struct.pack(">H", len(payload))
    else:
        head = bytes((first, 0x80 | 127)) + struct.pack(">Q", len(payload))
    return head + mask + masked


def _masked_client_text_frame(text: str, *, compressed: bool) -> bytes:
    payload = text.encode("utf-8")
    if compressed:
        compressor = zlib.compressobj(wbits=-15)
        payload = compressor.compress(payload) + compressor.flush(zlib.Z_SYNC_FLUSH)
        payload = payload[:-4]
    return _masked_client_frame(
        payload,
        opcode=0x1,
        fin=True,
        rsv1=compressed,
    )


def _fragmented_compressed_text_frames(
    compressor: zlib.Compress,
    text: str,
) -> list[bytes]:
    payload = compressor.compress(text.encode("utf-8"))
    payload += compressor.flush(zlib.Z_SYNC_FLUSH)
    payload = payload[:-4]
    split = max(1, len(payload) - 4)
    return [
        _masked_client_frame(
            payload[:split],
            opcode=0x1,
            fin=False,
            rsv1=True,
        ),
        _masked_client_frame(payload[split:], opcode=0x0, fin=False),
        _masked_client_frame(b"", opcode=0x0, fin=True),
    ]


class UpstreamByteRelayWebSocketTest(unittest.TestCase):
    def test_realtime_延迟合成先放行一次自然请求(self) -> None:
        self.assertFalse(
            _should_synthesize_realtime_call(
                immediate=False,
                after_live_attempts=1,
                attempt=1,
            )
        )
        self.assertTrue(
            _should_synthesize_realtime_call(
                immediate=False,
                after_live_attempts=1,
                attempt=2,
            )
        )
        self.assertFalse(
            _should_synthesize_realtime_call(
                immediate=False,
                after_live_attempts=1,
                attempt=3,
            )
        )

    def test_realtime_旧开关仍只合成第一次(self) -> None:
        self.assertTrue(
            _should_synthesize_realtime_call(
                immediate=True,
                after_live_attempts=None,
                attempt=1,
            )
        )
        self.assertFalse(
            _should_synthesize_realtime_call(
                immediate=True,
                after_live_attempts=None,
                attempt=2,
            )
        )

    def test_relay_stop_marks_only_exact_client_only_valid_connection(self) -> None:
        metadata = {
            "valid": True,
            "bytes": {"client_to_upstream": 384},
            "sha256": {"client_to_upstream": "a" * 64},
        }

        marked = _annotate_relay_stop_after_client_request(
            metadata,
            stop_requested=True,
        )

        self.assertTrue(marked)
        self.assertTrue(metadata["relay_stop_requested"])
        self.assertEqual(
            metadata["termination_reason"],
            "relay_shutdown_after_complete_client_request_before_upstream_response",
        )

    def test_relay_stop_does_not_mark_ambiguous_one_sided_connections(self) -> None:
        cases = (
            (
                "未请求停机",
                False,
                {
                    "valid": True,
                    "bytes": {"client_to_upstream": 1},
                    "sha256": {"client_to_upstream": "a" * 64},
                },
            ),
            (
                "连接含错误",
                True,
                {
                    "valid": True,
                    "error": "reset",
                    "bytes": {"client_to_upstream": 1},
                    "sha256": {"client_to_upstream": "a" * 64},
                },
            ),
            (
                "已有响应方向",
                True,
                {
                    "valid": True,
                    "bytes": {
                        "client_to_upstream": 1,
                        "upstream_to_client": 0,
                    },
                    "sha256": {
                        "client_to_upstream": "a" * 64,
                        "upstream_to_client": "b" * 64,
                    },
                },
            ),
        )
        for label, stop_requested, metadata in cases:
            with self.subTest(label=label):
                self.assertFalse(
                    _annotate_relay_stop_after_client_request(
                        metadata,
                        stop_requested=stop_requested,
                    )
                )
                self.assertNotIn("termination_reason", metadata)

    def test_selected_alpn_mirroring_preserves_h1_and_no_alpn_connections(self) -> None:
        assumed = ["http/1.1"]
        self.assertEqual(
            _upstream_alpn_offer(
                assumed, "http/1.1", mirror_selected=True
            ),
            ["http/1.1"],
        )
        self.assertIsNone(
            _upstream_alpn_offer(assumed, None, mirror_selected=True)
        )
        self.assertEqual(
            _upstream_alpn_offer(assumed, None, mirror_selected=False), assumed
        )

    def test_server_injection_frame_is_unmasked_and_uncompressed(self) -> None:
        text = json.dumps({
            "type": "response.metadata",
            "headers": {"x-codex-turn-state": "probe-ws-turn-state-0145"},
        }, separators=(",", ":"))
        frame = _encode_server_text_frame(text)
        self.assertEqual(frame[0], 0x81)
        self.assertEqual(frame[0] & 0x40, 0)
        self.assertEqual(frame[1] & 0x80, 0)
        self.assertTrue(frame.endswith(text.encode("utf-8")))

    def test_decodes_masked_plain_response_create(self) -> None:
        text = '{"type":"response.create","generate":false}'
        frame = _masked_client_text_frame(text, compressed=False)
        self.assertEqual(_decode_client_text_frame(frame), text)

    def test_decodes_masked_deflate_response_create(self) -> None:
        text = '{"type":"response.create","input":[{"type":"message"}]}'
        frame = _masked_client_text_frame(text, compressed=True)
        self.assertEqual(_decode_client_text_frame(frame), text)

    def test_reassembles_coder_fragmented_context_takeover_messages(self) -> None:
        compressor = zlib.compressobj(wbits=-15)
        decoder = _SyntheticCoreWebSocketDecoder()
        wire = bytearray()
        texts = [
            json.dumps(
                {
                    "type": "response.create",
                    "model": "gpt-5.6-sol",
                    "input": [{"type": "message", "content": "a" * 512}],
                },
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "type": "response.create",
                    "model": "gpt-5.6-sol",
                    "input": [{"type": "message", "content": "b" * 512}],
                },
                separators=(",", ":"),
            ),
        ]
        for text in texts:
            frames = _fragmented_compressed_text_frames(compressor, text)
            wire.extend(b"".join(frames))
            self.assertIsNone(decoder.text(frames[0]))
            self.assertIsNone(decoder.text(frames[1]))
            self.assertEqual(decoder.text(frames[2]), text)

        parsed = parse_ws_frames(bytes(wire))
        text_frames = [frame for frame in parsed if frame["opcode"] == "TEXT"]
        self.assertEqual(
            [frame.get("event_type") for frame in text_frames],
            ["response.create", "response.create"],
        )
        self.assertTrue(all(frame["rsv1_deflate"] for frame in text_frames))
        self.assertTrue(
            all(frame.get("compressed") == "permessage-deflate" for frame in text_frames)
        )
        self.assertEqual(
            [frame.get("message_fragment_count") for frame in text_frames],
            [3, 3],
        )


class UpstreamByteRelaySyntheticAuxTest(unittest.TestCase):
    @staticmethod
    def response(
        host: str,
        line: str,
        headers: bytes = b"",
        body: bytes = b"",
        legacy_compact_ordinal: int = 0,
    ):
        head = line.encode("ascii") + b"\r\n" + headers + b"\r\n"
        return _synthetic_aux_response(
            host, line, head, body, "0.147.0", legacy_compact_ordinal
        )

    def test_a09_auxiliary_endpoints_are_allowlisted(self) -> None:
        cases = (
            (
                "GET /backend-api/codex/models?client_version=0.147.0 HTTP/1.1",
                "models_manifest",
            ),
            ("POST /backend-api/codex/responses/compact HTTP/1.1", "legacy_compact"),
            ("POST /backend-api/codex/alpha/search HTTP/1.1", "alpha_search"),
            (
                "POST /backend-api/codex/images/generations HTTP/1.1",
                "images_generation",
            ),
            ("POST /backend-api/codex/images/edits HTTP/1.1", "images_edit"),
        )
        for request_line, action in cases:
            with self.subTest(action=action):
                result = self.response("chatgpt.com", request_line)
                self.assertIsNotNone(result)
                self.assertEqual(result.action, action)
                self.assertTrue(result.wire.startswith(b"HTTP/1.1 200 OK\r\n"))

    def test_a09_legacy_compact_response_has_complete_responses_usage(self) -> None:
        result = self.response(
            "chatgpt.com",
            "POST /backend-api/codex/responses/compact HTTP/1.1",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "legacy_compact")

        response_head, response_body = result.wire.split(b"\r\n\r\n", 1)
        payload = json.loads(response_body)
        self.assertEqual(payload["id"], "cmp_candidate_aux")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"], [])
        self.assertEqual(
            payload["usage"],
            {
                "input_tokens": 7,
                "output_tokens": 1,
                "total_tokens": 8,
                "input_tokens_details": {"cached_tokens": 0},
            },
        )
        self.assertIn(
            f"content-length: {len(response_body)}".encode("ascii"),
            response_head.lower(),
        )

    def test_a09_compact_prime_sets_cookie_and_beta_sets_turn_state(self) -> None:
        # prime 按到达序号识别。这里刻意**不**喂 capture_variant：网关按画像重新
        # 生成 x-codex-turn-metadata，该字段不会出现在出站字节里，用它构造请求
        # 等于测试一个真实链路上不存在的输入——旧版本正是这样通过的，而实际采集
        # 中 Cookie 从未下发过（k56 实测 A09 九个连接全无 cookie）。
        prime = self.response(
            "chatgpt.com",
            "POST /backend-api/codex/responses/compact HTTP/1.1",
            legacy_compact_ordinal=1,
        )
        self.assertIsNotNone(prime)
        self.assertIn(
            f"set-cookie: {_SYNTHETIC_AUX_CFUV_COOKIE}\r\n".encode("ascii"),
            prime.wire,
        )

        default = self.response(
            "chatgpt.com",
            "POST /backend-api/codex/responses/compact HTTP/1.1",
            legacy_compact_ordinal=2,
        )
        beta = self.response(
            "chatgpt.com",
            "POST /backend-api/codex/responses/compact HTTP/1.1",
            b"x-codex-beta-features: candidate_aux_beta\r\n",
            legacy_compact_ordinal=3,
        )
        self.assertIsNotNone(default)
        self.assertIsNotNone(beta)
        self.assertNotIn(b"set-cookie:", default.wire.lower())
        self.assertNotIn(b"set-cookie:", beta.wire.lower())
        self.assertNotIn(_SYNTHETIC_AUX_TURN_STATE.encode("ascii"), default.wire)
        self.assertIn(
            f"x-codex-turn-state: {_SYNTHETIC_AUX_TURN_STATE}\r\n".encode("ascii"),
            beta.wire,
        )

    def test_realtime_two_hop_is_linked_and_terminal(self) -> None:
        first = self.response(
            "chatgpt.com",
            "POST /backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas HTTP/1.1",
        )
        self.assertIsNotNone(first)
        self.assertIn(_SYNTHETIC_REALTIME_CALL_ID.encode("ascii"), first.wire)

        second = self.response(
            "api.openai.com",
            (
                "GET /v1/realtime?intent=quicksilver&call_id="
                f"{_SYNTHETIC_REALTIME_CALL_ID} HTTP/1.1"
            ),
            b"Upgrade: websocket\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n",
        )
        self.assertIsNotNone(second)
        self.assertEqual(second.action, "realtime_sideband")
        self.assertIn(
            b"sec-websocket-accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
            second.wire,
        )
        self.assertIn(b'{"type":"session.ended"}', second.terminal_ws_frame)

    def test_wham_consume_is_a_local_synthetic_response(self) -> None:
        for request_line, action in (
            (
                "GET /backend-api/wham/settings/user HTTP/1.1",
                "wham_settings_user",
            ),
            ("GET /backend-api/wham/usage HTTP/1.1", "wham_usage"),
            (
                "GET /backend-api/wham/rate-limit-reset-credits HTTP/1.1",
                "wham_credit_details",
            ),
            (
                "POST /backend-api/wham/rate-limit-reset-credits/consume HTTP/1.1",
                "wham_safe_consume",
            ),
        ):
            with self.subTest(action=action):
                result = self.response("chatgpt.com", request_line)
                self.assertIsNotNone(result)
                self.assertEqual(result.action, action)

    def test_oauth_dummy_is_redacted_before_persistence(self) -> None:
        dummy = b"dummy-refresh-must-not-persist"
        body = b"grant_type=refresh_token&refresh_token=" + dummy + b"&scope=openid"
        redacted, changed = _redact_oauth_refresh_body(body)
        self.assertTrue(changed)
        self.assertEqual(len(redacted), len(body))
        self.assertNotIn(dummy, redacted)
        self.assertIn(b"refresh_token=<secret>", redacted)

        response = self.response(
            "auth.openai.com",
            "POST /oauth/token HTTP/1.1",
            body=body,
        )
        self.assertIsNotNone(response)
        self.assertEqual(response.action, "oauth_dummy_invalid_grant")
        self.assertTrue(response.wire.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn(b'"error":"invalid_grant"', response.wire)

    def test_files_chain_uses_response_returned_regional_host(self) -> None:
        created = self.response(
            "chatgpt.com",
            "POST /backend-api/files HTTP/1.1",
        )
        self.assertIsNotNone(created)
        self.assertIn(_SYNTHETIC_FILE_HOST.encode("ascii"), created.wire)
        self.assertIn(_SYNTHETIC_FILE_ID.encode("ascii"), created.wire)
        self.assertIn(_SYNTHETIC_FILE_QUERY.encode("ascii"), created.wire)

        uploaded = self.response(
            _SYNTHETIC_FILE_HOST,
            f"PUT /candidate-aux/{_SYNTHETIC_FILE_ID}?{_SYNTHETIC_FILE_QUERY} HTTP/1.1",
        )
        self.assertIsNotNone(uploaded)
        self.assertEqual(uploaded.action, "files_blob_put")

        finalized = self.response(
            "chatgpt.com",
            f"POST /backend-api/files/{_SYNTHETIC_FILE_ID}/uploaded HTTP/1.1",
        )
        self.assertIsNotNone(finalized)
        self.assertEqual(finalized.action, "files_uploaded")

    def test_unknown_or_wrong_query_is_fail_closed(self) -> None:
        self.assertIsNone(
            self.response("chatgpt.com", "POST /backend-api/wham/unknown HTTP/1.1")
        )
        self.assertIsNone(
            self.response(
                "chatgpt.com",
                "POST /backend-api/codex/realtime/calls?intent=other&architecture=avas HTTP/1.1",
            )
        )
        self.assertIsNone(
            self.response(
                _SYNTHETIC_FILE_HOST,
                f"PUT /candidate-aux/{_SYNTHETIC_FILE_ID}?sig=wrong HTTP/1.1",
            )
        )

    def test_synthetic_profile_requires_second_explicit_switch(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cert",
                "missing.crt",
                "--key",
                "missing.key",
                "--output",
                "missing-output",
                "--synthetic-profile",
                "candidate-aux-v1",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("必须同时提供", result.stderr)

    def test_synthetic_profile_requires_valid_campaign_version(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        base = [
            sys.executable,
            str(script),
            "--cert",
            "missing.crt",
            "--key",
            "missing.key",
            "--output",
            "missing-output",
            "--synthetic-profile",
            "candidate-aux-v1",
            "--allow-synthetic-responses",
        ]
        missing = subprocess.run(
            base,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("必须提供 --codex-version", missing.stderr)

        invalid = subprocess.run(
            [*base, "--codex-version", "not-a-version"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("完整的 x.y.z 版本", invalid.stderr)

    def test_synthetic_profile_rejects_production_upstream_map(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cert",
                "missing.crt",
                "--key",
                "missing.key",
                "--output",
                "missing-output",
                "--synthetic-profile",
                "candidate-aux-v1",
                "--allow-synthetic-responses",
                "--codex-version",
                "0.147.0",
                "--upstream-ip",
                "203.0.113.10",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("禁止配置任何生产上游", result.stderr)


class UpstreamByteRelaySyntheticClaudeTest(unittest.TestCase):
    BODY = json.dumps(
        {"model": "claude-sonnet-5", "stream": True, "messages": []},
        separators=(",", ":"),
    ).encode("utf-8")
    LINE = "POST /v1/messages?beta=true HTTP/1.1"

    def response(
        self,
        plan: str,
        ordinal: int,
        body: bytes | None = None,
        success_marker: str = "FW_F_V3_OK",
    ):
        return _synthetic_claude_response(
            plan,
            "api.anthropic.com",
            self.LINE,
            self.BODY if body is None else body,
            ordinal,
            success_marker,
        )

    def test_success_marker_is_bound_to_synthetic_profile(self) -> None:
        v3 = self.response("disconnect-retry", 2)
        v4 = self.response(
            "disconnect-retry", 2, success_marker="FW_F_V4_OK"
        )
        self.assertIn(b"FW_F_V3_OK", v3.wire)
        self.assertNotIn(b"FW_F_V4_OK", v3.wire)
        self.assertIn(b"FW_F_V4_OK", v4.wire)
        self.assertNotIn(b"FW_F_V3_OK", v4.wire)

    def test_every_retry_once_plan_faults_then_returns_valid_sse(self) -> None:
        plans = _SYNTHETIC_CLAUDE_PLANS - {
            "always-529",
            "disconnect-retry",
            "fallback-model",
            "nonretry-400",
            "nonretry-403",
            "stall",
            "stream-404-fallback",
            "stream-interrupt-fallback",
            "stream-interrupt-no-fallback",
        }
        for plan in sorted(plans):
            with self.subTest(plan=plan):
                first = self.response(plan, 1)
                second = self.response(plan, 2)
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)
                self.assertFalse(first.wire.startswith(b"HTTP/1.1 200"))
                self.assertIn(b'"type":"error"', first.wire)
                self.assertTrue(second.wire.startswith(b"HTTP/1.1 200 OK"))
                self.assertIn(b"event: message_stop", second.wire)

    def test_retry_after_variants_emit_the_frozen_header(self) -> None:
        seconds = self.response("retry-429-after-seconds", 1)
        dated = self.response("retry-429-after-date", 1)
        self.assertIn(b"retry-after: 1\r\n", seconds.wire.lower())
        self.assertRegex(
            dated.wire.lower(), rb"retry-after: [a-z]{3}, \d{2} [a-z]{3} \d{4}"
        )

    def test_nonretry_disconnect_stall_and_retry_limit_are_distinct(self) -> None:
        self.assertTrue(
            self.response("nonretry-400", 1).wire.startswith(b"HTTP/1.1 400")
        )
        self.assertTrue(
            self.response("nonretry-403", 1).wire.startswith(b"HTTP/1.1 403")
        )
        self.assertTrue(
            self.response("always-529", 4).wire.startswith(b"HTTP/1.1 529")
        )
        disconnected = self.response("disconnect-retry", 1)
        stalled = self.response("stall", 1)
        self.assertEqual(disconnected.wire, b"")
        self.assertEqual(disconnected.delay_seconds, 0)
        self.assertEqual(stalled.wire, b"")
        self.assertEqual(stalled.delay_seconds, 3.0)

    def test_fallback_model_and_nonstream_fallback_use_request_body(self) -> None:
        primary = self.response("fallback-model", 1)
        fallback_body = json.dumps(
            {"model": "claude-haiku-4-5", "stream": True},
            separators=(",", ":"),
        ).encode("utf-8")
        fallback = self.response("fallback-model", 2, fallback_body)
        self.assertTrue(primary.wire.startswith(b"HTTP/1.1 529"))
        self.assertIn(b"claude-haiku-4-5", fallback.wire)

        nonstream_body = json.dumps(
            {"model": "claude-sonnet-5", "stream": False},
            separators=(",", ":"),
        ).encode("utf-8")
        stream_fault = self.response("stream-404-fallback", 1)
        nonstream = self.response("stream-404-fallback", 2, nonstream_body)
        self.assertTrue(stream_fault.wire.startswith(b"HTTP/1.1 404"))
        self.assertIn(b'"type":"message"', nonstream.wire)
        self.assertNotIn(b"event: message_start", nonstream.wire)

    def test_unknown_host_path_and_invalid_body_fail_closed(self) -> None:
        self.assertIsNone(
            _synthetic_claude_response(
                "retry-500", "example.com", self.LINE, self.BODY, 1
            )
        )
        self.assertIsNone(
            _synthetic_claude_response(
                "retry-500",
                "api.anthropic.com",
                "POST /v1/unknown HTTP/1.1",
                self.BODY,
                1,
            )
        )
        self.assertIsNone(self.response("retry-500", 1, b"not-json"))

    def test_control_plane_auxiliary_endpoints_are_locally_closed_as_absent(self) -> None:
        for request_line, action in (
            (
                "GET /api/claude_code/policy_limits HTTP/1.1",
                "policy_limits_absent",
            ),
            (
                "GET /api/claude_code/settings HTTP/1.1",
                "remote_settings_absent",
            ),
        ):
            with self.subTest(request_line=request_line):
                result = _synthetic_claude_response(
                    "retry-500",
                    "api.anthropic.com",
                    request_line,
                    b"",
                    0,
                )
                self.assertIsNotNone(result)
                self.assertEqual(result.action, action)
                self.assertTrue(result.wire.startswith(b"HTTP/1.1 404"))

    def test_oauth_refresh_is_allowlisted_only_on_platform_host(self) -> None:
        body = b"grant_type=refresh_token&refresh_token=dummy"
        result = _synthetic_claude_response(
            "oauth-refresh-reject",
            "platform.claude.com",
            "POST /v1/oauth/token HTTP/1.1",
            body,
            0,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "oauth_refresh_rejected")
        self.assertTrue(result.wire.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn(b'"error":"invalid_grant"', result.wire)
        self.assertIsNone(
            _synthetic_claude_response(
                "oauth-refresh-reject",
                "api.anthropic.com",
                "POST /v1/oauth/token HTTP/1.1",
                body,
                0,
            )
        )
        self.assertIsNone(
            _synthetic_claude_response(
                "oauth-refresh-reject",
                "platform.claude.com",
                "POST /v1/messages?beta=true HTTP/1.1",
                body,
                0,
            )
        )

    def test_claude_profile_rejects_codex_or_production_parameters(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        base = [
            sys.executable,
            str(script),
            "--cert",
            "missing.crt",
            "--key",
            "missing.key",
            "--output",
            "missing-output",
            "--synthetic-profile",
            "claude-fw-f-v3",
            "--allow-synthetic-responses",
            "--claude-version",
            "2.1.226",
            "--claude-fault-plan",
            "retry-500",
            "--claude-success-marker",
            "FW_F_V3_OK",
        ]
        for extra, expected in (
            (["--codex-version", "0.147.0"], "禁止提供 --codex-version"),
            (["--upstream-ip", "203.0.113.10"], "禁止配置任何生产上游"),
        ):
            with self.subTest(extra=extra):
                result = subprocess.run(
                    [*base, *extra], text=True, capture_output=True, check=False
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)

    def test_claude_profile_requires_probe_bound_success_marker(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cert",
                "missing.crt",
                "--key",
                "missing.key",
                "--output",
                "missing-output",
                "--synthetic-profile",
                "claude-fw-f-v4",
                "--allow-synthetic-responses",
                "--claude-version",
                "2.1.226",
                "--claude-fault-plan",
                "disconnect-retry",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("必须提供 --claude-success-marker", result.stderr)


class UpstreamByteRelaySyntheticCoreTest(unittest.TestCase):
    @staticmethod
    def response(
        scenario: str,
        line: str,
        headers: bytes = b"",
        body: bytes = b"",
        ordinal: int = 1,
    ):
        head = line.encode("ascii") + b"\r\n" + headers + b"\r\n"
        return _synthetic_core_response(
            scenario,
            "chatgpt.com",
            line,
            head,
            body,
            ordinal,
            "0.147.0",
        )

    def test_a03_four_response_cookie_and_turn_state_sequence(self) -> None:
        results = [
            self.response(
                "A03",
                "POST /backend-api/codex/responses HTTP/1.1",
                ordinal=ordinal,
            )
            for ordinal in range(1, 5)
        ]
        self.assertTrue(all(result is not None for result in results))
        wires = [result.wire for result in results if result is not None]
        self.assertEqual(
            [result.action for result in results if result is not None],
            ["responses_http_success"] * 4,
        )
        self.assertEqual(
            [result.set_cookie_names for result in results if result is not None],
            [("_cfuvid",), (), (), ()],
        )
        self.assertIn(
            f"set-cookie: {_SYNTHETIC_CORE_CFUV_COOKIE}\r\n".encode("ascii"),
            wires[0],
        )
        self.assertNotIn(b"set-cookie:", b"".join(wires[1:]).lower())
        self.assertNotIn(_SYNTHETIC_CORE_TURN_STATE.encode("ascii"), wires[0])
        self.assertNotIn(_SYNTHETIC_CORE_TURN_STATE.encode("ascii"), wires[1])
        self.assertIn(_SYNTHETIC_CORE_TURN_STATE.encode("ascii"), wires[2])
        self.assertNotIn(_SYNTHETIC_CORE_TURN_STATE.encode("ascii"), wires[3])
        self.assertIn(b'"model":"gpt-5.5"', wires[0])
        self.assertIn(b'"model":"gpt-5.5"', wires[1])
        self.assertIn(b'"model":"gpt-5.6-sol"', wires[2])
        self.assertIn(b'"model":"gpt-5.6-sol"', wires[3])
        for wire in wires:
            self.assertIn(b"content-type: text/event-stream", wire)
            self.assertIn(b'"type":"response.completed"', wire)
            self.assertIn(b"data: [DONE]", wire)

    def test_a03_cookie_is_redacted_from_public_relay_bytes(self) -> None:
        first = self.response(
            "A03",
            "POST /backend-api/codex/responses HTTP/1.1",
            ordinal=1,
        )
        self.assertIsNotNone(first)
        private_bytes = (
            first.wire
            + b"POST /backend-api/codex/responses HTTP/1.1\r\n"
            + f"cookie: {_SYNTHETIC_CORE_CFUV_COOKIE.split(';', 1)[0]}\r\n\r\n".encode(
                "ascii"
            )
        )
        public_bytes, replacements = scrub(private_bytes)
        self.assertEqual(len(public_bytes), len(private_bytes))
        # 现有等长 scrub 规则会先按 Cookie 命中 Set-Cookie 的后缀，再由
        # Set-Cookie 专用规则复核替换；另一次命中来自后续请求的 Cookie。
        self.assertEqual(replacements, 3)
        self.assertNotIn(b"_cfuvid", public_bytes)
        self.assertNotIn(b"candidate-core-0145", public_bytes)
        self.assertEqual(public_bytes.count(b"<secret>"), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a03-public-relay.bin"
            path.write_bytes(public_bytes)
            scan = scan_files_for_secrets([("candidate/A03/relay.bin", path)])
        self.assertTrue(scan["passed"], scan["findings"])

    def test_models_manifest_covers_lite_and_non_lite(self) -> None:
        result = self.response(
            "A04",
            "GET /backend-api/codex/models?client_version=0.147.0 HTTP/1.1",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "models_manifest")
        self.assertIn(b'"slug":"gpt-5.5"', result.wire)
        self.assertIn(b'"use_responses_lite":false', result.wire)
        self.assertIn(b'"slug":"gpt-5.6-sol"', result.wire)
        self.assertIn(b'"use_responses_lite":true', result.wire)

    def test_ws_requires_permessage_deflate_and_builds_valid_accept(self) -> None:
        headers = (
            b"Upgrade: websocket\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
        )
        result = self.response(
            "A05",
            "GET /backend-api/codex/responses HTTP/1.1",
            headers,
        )
        self.assertIsNotNone(result)
        self.assertTrue(result.websocket)
        self.assertIn(
            b"sec-websocket-accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
            result.wire,
        )
        self.assertIn(b"sec-websocket-extensions: permessage-deflate", result.wire)
        self.assertIsNone(
            self.response(
                "A05",
                "GET /backend-api/codex/responses HTTP/1.1",
                b"Upgrade: websocket\r\nSec-WebSocket-Key: key\r\n",
            )
        )

    def test_scenario_path_matrix_is_fail_closed(self) -> None:
        self.assertIsNone(
            self.response("A05", "POST /backend-api/codex/responses HTTP/1.1")
        )
        self.assertIsNone(
            self.response("A03", "POST /backend-api/codex/responses/compact HTTP/1.1")
        )
        self.assertIsNone(
            _synthetic_core_response(
                "A03",
                "auth.openai.com",
                "POST /oauth/token HTTP/1.1",
                b"POST /oauth/token HTTP/1.1\r\n\r\n",
                b"",
                1,
                "0.147.0",
            )
        )

    def test_context_takeover_decoder_reads_two_compressed_messages(self) -> None:
        compressor = zlib.compressobj(wbits=-15)
        decoder = _SyntheticCoreWebSocketDecoder()
        for ordinal in (1, 2):
            text = json.dumps(
                {"type": "response.create", "ordinal": ordinal},
                separators=(",", ":"),
            )
            payload = compressor.compress(text.encode("utf-8"))
            payload += compressor.flush(zlib.Z_SYNC_FLUSH)
            payload = payload[:-4]
            mask = b"\x11\x22\x33\x44"
            masked = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
            frame = bytes((0xC1, 0x80 | len(payload))) + mask + masked
            self.assertEqual(decoder.text(frame), text)

    def test_core_profile_requires_scenario_and_rejects_upstream(self) -> None:
        script = Path(__file__).parents[1] / "upstream_byte_relay.py"
        missing_scenario = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cert",
                "missing.crt",
                "--key",
                "missing.key",
                "--output",
                "missing-output",
                "--synthetic-profile",
                "candidate-core-v1",
                "--allow-synthetic-responses",
                "--codex-version",
                "0.147.0",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(missing_scenario.returncode, 2)
        self.assertIn("必须提供 --candidate-core-scenario", missing_scenario.stderr)

        production_map = subprocess.run(
            [
                sys.executable,
                str(script),
                "--cert",
                "missing.crt",
                "--key",
                "missing.key",
                "--output",
                "missing-output",
                "--synthetic-profile",
                "candidate-core-v1",
                "--allow-synthetic-responses",
                "--codex-version",
                "0.147.0",
                "--candidate-core-scenario",
                "A03",
                "--upstream-map",
                "chatgpt.com=203.0.113.10",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(production_map.returncode, 2)
        self.assertIn("禁止配置任何生产上游", production_map.stderr)


class ScrubbedRelayEvidenceTest(unittest.TestCase):
    def test_manifest_copy_creates_destination_without_wire_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private"
            destination = root / "public"
            source.mkdir()
            (source / "relay.json").write_text(
                json.dumps({"connections": []}),
                encoding="utf-8",
            )
            self.assertTrue(rewrite_relay_manifest(source, destination))
            self.assertTrue((destination / "relay.json").is_file())

    def test_equal_length_placeholder_passes_candidate_secret_guard(self) -> None:
        source = (
            b"POST /oauth/token HTTP/1.1\r\n"
            b"authorization: Bearer real-token-value-123456789\r\n"
            b"cookie: session=real-cookie-value-123456789\r\n\r\n"
            b'{"refresh_token":"real-refresh-value-123456789"}'
        )
        scrubbed, replacements = scrub(source)
        self.assertEqual(len(scrubbed), len(source))
        self.assertEqual(replacements, 3)
        self.assertNotIn(b"real-token", scrubbed)
        self.assertGreaterEqual(scrubbed.count(b"<secret>"), 3)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.bin"
            path.write_bytes(scrubbed)
            result = scan_files_for_secrets([("candidate/A13/relay.bin", path)])
        self.assertTrue(result["passed"], result["findings"])

    def test_zstd_body_is_never_scanned_as_plaintext(self) -> None:
        """压缩体内偶然出现 query 形态字节时，不得破坏 zstd 帧。"""

        body = b"\x28\xb5\x2f\xfdcompressed-random&uI=secret-bytes"
        source = (
            b"POST /backend-api/codex/responses HTTP/1.1\r\n"
            b"authorization: Bearer real-token-value-123456789\r\n"
            b"content-encoding: zstd\r\n"
            + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        scrubbed, replacements = scrub(source)
        self.assertEqual(replacements, 1)
        self.assertNotIn(b"real-token-value", scrubbed)
        self.assertEqual(scrubbed[-len(body):], body)

    def test_permessage_deflate_frames_are_never_scanned_as_plaintext(self) -> None:
        """WS 压缩帧中的偶然 query 字节不得被误改而破坏 deflate 流。"""

        frame_bytes = b"\xc1\x10binary?sig=random-compressed-bytes"
        source = (
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"upgrade: websocket\r\n"
            b"sec-websocket-extensions: permessage-deflate; client_max_window_bits\r\n"
            b"authorization: Bearer real-token-value-123456789\r\n\r\n"
            + frame_bytes
        )
        scrubbed, replacements = scrub(source)
        self.assertEqual(replacements, 1)
        self.assertNotIn(b"real-token-value", scrubbed)
        self.assertEqual(scrubbed[-len(frame_bytes):], frame_bytes)
        self.assertEqual(count_unscrubbed_credentials(scrubbed), (0, 0))

    def test_identity_signal_token_is_scrubbed_in_header_and_body(self) -> None:
        """A13 刷新响应里的 identity-signal 令牌必须与 access_token 同等脱敏。

        它的形态是 `ois1.eyJ<载荷>.<签名>`，JWT 主体被 `ois1.` 前缀隔开：首版规则
        既不认 `x-oai-is-update` 头也不认 `oai_is` 字段，令牌明文留在 relay 原始
        字节里，直到 evidence guard 的 jwt-shape 才拦下（k52）。
        """

        jwt_body = b"eyJ" + b"A" * 60 + b"." + b"B" * 40 + b"." + b"C" * 30
        source = (
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: application/json\r\n"
            b"x-oai-is-update: ois1." + jwt_body + b"\r\n\r\n"
            b'{"oai_is": "ois1.' + jwt_body + b'"}'
        )
        scrubbed, replacements = scrub(source)
        self.assertEqual(len(scrubbed), len(source))
        self.assertEqual(replacements, 2)
        self.assertNotIn(b"eyJ", scrubbed)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "relay.bin"
            path.write_bytes(scrubbed)
            result = scan_files_for_secrets([("official/A13/relay.bin", path)])
        self.assertTrue(result["passed"], result["findings"])

    def test_rescan_flags_identity_signal_token_left_in_clear(self) -> None:
        """复扫判据必须与 evidence guard 对齐，否则漏网的是真凭据而非告警。"""

        raw = b"x-oai-is-update: ois1.eyJ" + b"A" * 60 + b"." + b"B" * 40
        self.assertTrue(GENERIC_TOKEN.search(raw))


class AuxLegacyCompactPrimeTest(unittest.TestCase):
    """prime 轮只能按到达序号识别，不能看 turn-metadata 里的客户端自造字段。

    网关按画像重新生成 `x-codex-turn-metadata`，采集脚本塞进去的 capture_variant
    不会出现在出站字节里。旧实现据此判定 prime，因而 Cookie 从未下发过——k56 实测
    A09 九个连接全部无 cookie，EP-015／EP-022 的头序判据随之必败。
    """

    LINE = "POST /backend-api/codex/responses/compact HTTP/1.1"

    def _head(self, *, beta: bool = False, capture_variant: str = "") -> bytes:
        metadata = '{"installation_id":"dcaa827b","session_id":"019ff289"'
        if capture_variant:
            metadata += f',"capture_variant":"{capture_variant}"'
        metadata += "}"
        rows = [self.LINE, "host: chatgpt.com"]
        if beta:
            rows.append("x-codex-beta-features: candidate_aux_beta")
        rows.append(f"x-codex-turn-metadata: {metadata}")
        return ("\r\n".join(rows) + "\r\n\r\n").encode("ascii")

    def _wire(self, head: bytes, ordinal: int) -> bytes:
        response = _synthetic_aux_response(
            "chatgpt.com", self.LINE, head, b"{}", "0.147.0", ordinal
        )
        self.assertIsNotNone(response)
        return response.wire.lower()

    def test_first_compact_sets_cookie(self):
        self.assertIn(b"set-cookie", self._wire(self._head(), 1))

    def test_later_compacts_do_not_set_cookie(self):
        for ordinal in (2, 3, 4):
            self.assertNotIn(b"set-cookie", self._wire(self._head(), ordinal))

    def test_capture_variant_alone_never_triggers_cookie(self):
        """出站不可能带 capture_variant；即便带了也不得作为判定依据。"""
        head = self._head(capture_variant="prime")
        self.assertNotIn(b"set-cookie", self._wire(head, 2))

    def test_beta_header_still_drives_turn_state(self):
        wire = self._wire(self._head(beta=True), 3)
        self.assertIn(b"x-codex-turn-state", wire)
        self.assertNotIn(b"set-cookie", wire)


if __name__ == "__main__":
    unittest.main()
