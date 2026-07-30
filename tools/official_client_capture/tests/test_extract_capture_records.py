"""最小采集记录提取器的无网络测试。"""

from __future__ import annotations

import argparse
import json
import struct
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.extract_capture_records import extract_ep024_negative


def masked_text_frame(value: dict) -> bytes:
    """构造未压缩的客户端 WS 文本帧。"""
    payload = json.dumps(value, separators=(",", ":")).encode()
    mask = b"\x11\x22\x33\x44"
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    if len(payload) <= 125:
        header = bytes((0x81, 0x80 | len(payload)))
    else:
        header = bytes((0x81, 0x80 | 126)) + struct.pack(">H", len(payload))
    return header + mask + masked


def server_text_frame(value: dict) -> bytes:
    """构造未压缩的服务端 WS 文本帧。"""
    payload = json.dumps(value, separators=(",", ":")).encode()
    if len(payload) <= 125:
        header = bytes((0x81, len(payload)))
    else:
        header = bytes((0x81, 126)) + struct.pack(">H", len(payload))
    return header + payload


class Ep024NegativeRecordTest(unittest.TestCase):
    def make_relay(self, *, compaction_trigger: bool = False, compact_http: bool = False):
        temporary = tempfile.TemporaryDirectory()
        relay = Path(temporary.name)
        input_items = [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "/compact"}],
        }]
        if compaction_trigger:
            input_items.append({"type": "compaction_trigger"})
        request = (
            b"GET /backend-api/codex/responses HTTP/1.1\r\n"
            b"host: chatgpt.com\r\n"
            b"originator: codex_exec\r\n"
            b"upgrade: websocket\r\n\r\n"
        ) + masked_text_frame({"type": "response.create", "input": input_items})
        response = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"upgrade: websocket\r\n\r\n"
        ) + server_text_frame({"type": "response.completed"})
        (relay / "conn001.client_to_upstream.bin").write_bytes(request)
        (relay / "conn001.upstream_to_client.bin").write_bytes(response)
        connections = [{"connection_id": 1, "valid": True}]
        if compact_http:
            (relay / "conn002.client_to_upstream.bin").write_bytes(
                b"POST /backend-api/codex/responses/compact HTTP/1.1\r\n"
                b"host: chatgpt.com\r\ncontent-length: 0\r\n\r\n"
            )
            (relay / "conn002.upstream_to_client.bin").write_bytes(
                b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\n\r\n"
            )
            connections.append({"connection_id": 2, "valid": True})
        (relay / "relay.json").write_text(
            json.dumps({"connections": connections}),
            encoding="utf-8",
        )
        return temporary, relay

    def test_accepts_exact_exec_message_without_compaction(self) -> None:
        temporary, relay = self.make_relay()
        self.addCleanup(temporary.cleanup)
        result = extract_ep024_negative(argparse.Namespace(
            relay_dir=str(relay),
            run_id="ep024-negative-test",
        ))
        observation = result["business_observation"]
        self.assertEqual(observation["ordinary_message_match_count"], 1)
        self.assertEqual(observation["compaction_trigger_count"], 0)
        self.assertEqual(observation["responses_compact_request_count"], 0)
        self.assertEqual(
            observation["ordinary_message_matches"][0]["originators"],
            ["codex_exec"],
        )

    def test_rejects_compaction_trigger(self) -> None:
        temporary, relay = self.make_relay(compaction_trigger=True)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "compaction_trigger"):
            extract_ep024_negative(argparse.Namespace(
                relay_dir=str(relay),
                run_id="ep024-negative-test",
            ))

    def test_rejects_responses_compact_endpoint(self) -> None:
        temporary, relay = self.make_relay(compact_http=True)
        self.addCleanup(temporary.cleanup)
        with self.assertRaisesRegex(ValueError, "/responses/compact"):
            extract_ep024_negative(argparse.Namespace(
                relay_dir=str(relay),
                run_id="ep024-negative-test",
            ))


if __name__ == "__main__":
    unittest.main()
