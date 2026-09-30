"""0.159.2 官方定向样本的无网络测试：中继受控合成、空闲关闭、参数校验、采集脚本接线与场景清单。

第 0b 步为 VC-2 需要实测确认的四类新条件补了官方定向样本：流内 flex_unavailable／interrupted（中继受控
应答）、空闲 WS 被服务端关闭后直接重建（中继补 CLOSE）、Retry-After 有值对照（受控 500 附带该头）、
数字推理等级（配置自定义档位）与生图透明背景／file_id 编辑（file_id 链由中继受控应答）。触发器属 wire 层，
必须在 VC-0 冻结前一次做对，这里逐项锁定：

- 合成事件形状与 0.159.2 源码的解析要求一致（response.id、usage 三个必填计数、错误码与 incomplete 原因）；
- WS 受控服务按预热／首个生成请求／续发三类应答，服务端帧不掩码；
- 空闲关闭只看业务帧，ping／pong 不刷新计时，且只在两个方向都停在帧边界时补 CLOSE；
- 中继与采集脚本都在任何请求之前拒绝非法取值与错误搭配；
- 0.159.2 场景清单沿用作业的执行参数与 0.157.0 逐字相同，新增作业的环境能通过脚本校验。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

from tools.official_client_capture.upstream_byte_relay import (
    Relay,
    _WsFrameActivity,
    _official_image_response,
    _official_imagegen_call_events,
    _official_message_events,
    _official_sse_response,
    _official_stream_fault_events,
    _official_ws_handshake,
)

TOOL_ROOT = Path(__file__).resolve().parents[1]
RELAY_SCRIPT = TOOL_ROOT / "run_official_relay_scenario.sh"
RELAY_PY = TOOL_ROOT / "upstream_byte_relay.py"
BASH = shutil.which("bash") or "/bin/bash"
NEW_OFFICIAL_JOBS = {
    "official-relay-conn-retry-after",
    "official-relay-reasoning-numeric-http",
    "official-relay-reasoning-numeric-ws",
    "official-relay-stream-flex-unavailable",
    "official-relay-stream-interrupted-http",
    "official-relay-stream-interrupted-ws",
    "official-relay-ws-idle-close",
    "official-relay-image-transparent",
    "official-relay-image-edit-file-id",
}


def frame(opcode: int, payload: bytes, *, masked: bool, fin: bool = True, rsv1: bool = False) -> bytes:
    first = opcode | (0x80 if fin else 0) | (0x40 if rsv1 else 0)
    if len(payload) < 126:
        head = bytes((first, (0x80 if masked else 0) | len(payload)))
    elif len(payload) <= 0xFFFF:
        head = bytes((first, (0x80 if masked else 0) | 126)) + struct.pack(">H", len(payload))
    else:
        head = bytes((first, (0x80 if masked else 0) | 127)) + struct.pack(">Q", len(payload))
    if not masked:
        return head + payload
    mask = b"\x0a\x0b\x0c\x0d"
    return head + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def client_text(message: dict, *, compressor: zlib.Compress | None = None) -> bytes:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if compressor is None:
        return frame(0x1, payload, masked=True)
    compressed = compressor.compress(payload) + compressor.flush(zlib.Z_SYNC_FLUSH)
    return frame(0x1, compressed[:-4], masked=True, rsv1=True)


def server_frames(data: bytes) -> list[tuple[int, bytes]]:
    """解析服务端（不掩码、不压缩）帧序列。"""

    frames: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(data):
        first, second = data[pos], data[pos + 1]
        if second & 0x80:
            raise AssertionError("服务端帧不得掩码")
        length = second & 0x7F
        pos += 2
        if length == 126:
            length = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2
        elif length == 127:
            length = struct.unpack(">Q", data[pos:pos + 8])[0]
            pos += 8
        frames.append((first & 0x0F, data[pos:pos + length]))
        pos += length
    return frames


class FakeWriter:
    def __init__(self, on_close=None) -> None:
        self.data = bytearray()
        self.closed = False
        self._on_close = on_close

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self._on_close is not None:
                self._on_close()


class FakeRecorder:
    def __init__(self) -> None:
        self.chunks: list[tuple[str, bytes]] = []

    def write(self, direction: str, chunk: bytes) -> None:
        self.chunks.append((direction, bytes(chunk)))


def relay_with(tmp: str, **overrides) -> Relay:
    """只取被测方法用到的状态：参数与干预日志目录；不加载证书、不监听端口。"""

    fields = {
        "output": tmp,
        "stream_fault": "",
        "stream_fault_transport": "",
        "synthesize_image_edit_file_id": "",
        "close_idle_ws_after": "",
    }
    fields.update(overrides)
    relay = Relay.__new__(Relay)
    relay.args = SimpleNamespace(**fields)
    relay.out = Path(tmp)
    return relay


class WsFrameActivityTest(unittest.TestCase):
    def test_ping_pong_do_not_refresh_idle_timer(self) -> None:
        tracker = _WsFrameActivity(http_head=False)
        self.assertFalse(tracker.feed(frame(0x9, b"hi", masked=True)))
        self.assertFalse(tracker.feed(frame(0xA, b"", masked=True)))
        self.assertEqual((tracker.data_frames, tracker.ping_pong_frames), (0, 2))
        self.assertTrue(tracker.feed(frame(0x1, b'{"type":"response.create"}', masked=True)))
        self.assertEqual(tracker.data_frames, 1)

    def test_headers_split_across_chunks_and_large_payload(self) -> None:
        wire = frame(0x2, b"y" * 70000, masked=True) + frame(0x9, b"p", masked=True)
        tracker = _WsFrameActivity(http_head=False)
        results = [tracker.feed(wire[index:index + 3]) for index in range(0, len(wire), 3)]
        self.assertFalse(results[0], "帧头未收齐时不算活动")
        self.assertEqual((tracker.data_frames, tracker.ping_pong_frames, tracker.broken), (1, 1, False))
        self.assertFalse(results[-1], "尾部只有 ping 帧")

    def test_upstream_direction_skips_101_head(self) -> None:
        head = b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\r\n"
        tracker = _WsFrameActivity(http_head=True)
        self.assertTrue(tracker.feed(head[:10]))
        self.assertTrue(tracker.feed(head[10:] + frame(0x9, b"", masked=False)))
        self.assertEqual((tracker.data_frames, tracker.ping_pong_frames), (0, 1))
        self.assertFalse(tracker.feed(frame(0x9, b"x", masked=False)))
        self.assertTrue(tracker.feed(frame(0x1, b"{}", masked=False)))

    def test_unparseable_streams_degrade_to_byte_activity(self) -> None:
        refused = _WsFrameActivity(http_head=True)
        self.assertTrue(refused.feed(b"HTTP/1.1 426 Upgrade Required\r\ncontent-length: 0\r\n\r\n"))
        self.assertTrue(refused.broken)
        self.assertTrue(refused.feed(frame(0x9, b"", masked=False)), "解析失败后每个字节都算活动")
        invalid = _WsFrameActivity(http_head=False)
        self.assertTrue(invalid.feed(bytes((0x83, 0x80)) + b"\x00" * 4))
        self.assertTrue(invalid.broken)
        oversized_control = _WsFrameActivity(http_head=False)
        self.assertTrue(oversized_control.feed(bytes((0x89, 0x80 | 126, 0x00, 0x80)) + b"\x00" * 4))
        self.assertTrue(oversized_control.broken)


class OfficialSyntheticEventsTest(unittest.TestCase):
    def assert_response_contract(self, response: dict, response_id: str) -> None:
        self.assertEqual(response["id"], response_id)
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["model"], "gpt-5.5")

    def test_flex_unavailable_is_response_failed_with_error_code(self) -> None:
        created, failed = _official_stream_fault_events("flex-unavailable", "resp_x")
        self.assertEqual(created["type"], "response.created")
        self.assertEqual(failed["type"], "response.failed")
        self.assert_response_contract(failed["response"], "resp_x")
        self.assertEqual(failed["response"]["status"], "failed")
        self.assertEqual(failed["response"]["error"]["code"], "flex_unavailable")

    def test_interrupted_is_incomplete_with_reason_and_required_usage(self) -> None:
        _created, incomplete = _official_stream_fault_events("interrupted", "resp_y")
        self.assertEqual(incomplete["type"], "response.incomplete")
        self.assert_response_contract(incomplete["response"], "resp_y")
        self.assertEqual(incomplete["response"]["incomplete_details"], {"reason": "interrupted"})
        usage = incomplete["response"]["usage"]
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIsInstance(usage[field], int, field)
        with self.assertRaises(ValueError):
            _official_stream_fault_events("unknown", "resp_z")

    def test_message_and_imagegen_call_complete_with_id_and_usage(self) -> None:
        message_events = _official_message_events("resp_m", "EDIT-OK")
        self.assertEqual([event["type"] for event in message_events],
                         ["response.created", "response.output_item.done", "response.completed"])
        self.assertEqual(message_events[1]["item"]["content"][0]["text"], "EDIT-OK")
        call_events = _official_imagegen_call_events("resp_c")
        call = call_events[1]["item"]
        self.assertEqual((call["type"], call["name"], call["namespace"]), ("function_call", "imagegen", "image_gen"))
        arguments = json.loads(call["arguments"])
        self.assertEqual(arguments["num_last_images_to_include"], 1)
        self.assertTrue(arguments["prompt"])
        for events in (message_events, call_events):
            completed = events[-1]["response"]
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["usage"]["total_tokens"],
                             completed["usage"]["input_tokens"] + completed["usage"]["output_tokens"])

    def test_sse_response_uses_event_and_data_lines(self) -> None:
        wire = _official_sse_response(_official_message_events("resp_s", "OK"))
        head, _, body = wire.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b"content-type: text/event-stream", head.lower())
        blocks = [block for block in body.split(b"\n\n") if block]
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            event_line, data_line = block.split(b"\n")
            self.assertTrue(event_line.startswith(b"event: "))
            payload = json.loads(data_line[len(b"data: "):])
            self.assertEqual(payload["type"].encode("ascii"), event_line[len(b"event: "):])

    def test_image_response_carries_a_png(self) -> None:
        wire = _official_image_response()
        body = json.loads(wire.partition(b"\r\n\r\n")[2])
        self.assertIsInstance(body["created"], int)
        png = base64.b64decode(body["data"][0]["b64_json"])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_ws_handshake_accepts_key_and_permessage_deflate(self) -> None:
        key = base64.b64encode(b"0123456789abcdef").decode("ascii")
        head = (
            "GET /backend-api/codex/responses HTTP/1.1\r\nupgrade: websocket\r\n"
            f"sec-websocket-key: {key}\r\nsec-websocket-extensions: permessage-deflate; client_max_window_bits\r\n\r\n"
        ).encode("ascii")
        wire = _official_ws_handshake(head)
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        self.assertIn(f"sec-websocket-accept: {accept}\r\n".encode("ascii"), wire)
        self.assertIn(b"sec-websocket-extensions: permessage-deflate\r\n", wire)
        self.assertIsNone(_official_ws_handshake(b"GET / HTTP/1.1\r\nhost: x\r\n\r\n"))


class StreamFaultWebSocketTest(unittest.TestCase):
    HEAD = (
        b"GET /backend-api/codex/responses HTTP/1.1\r\nupgrade: websocket\r\n"
        b"sec-websocket-key: dGhlIHNhbXBsZSBub25jZQ==\r\nsec-websocket-extensions: permessage-deflate\r\n\r\n"
    )

    def serve(self, kind: str, client_frames: list[bytes]) -> tuple[list[dict], dict, list[dict]]:
        async def exercise() -> tuple[bytes, dict]:
            with tempfile.TemporaryDirectory() as tmp:
                relay = relay_with(tmp, stream_fault=kind, stream_fault_transport="ws")
                reader = asyncio.StreamReader()
                for item in client_frames:
                    reader.feed_data(item)
                reader.feed_eof()
                writer = FakeWriter()
                meta: dict = {}
                await relay._serve_official_stream_fault_websocket(
                    reader, writer, FakeRecorder(), 1, meta, self.HEAD
                )
                log = Path(tmp) / "intervention.jsonl"
                events = [
                    json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
                ] if log.exists() else []
                return bytes(writer.data), meta, events

        wire, meta, interventions = asyncio.run(exercise())
        head, _, frames_wire = wire.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 101 Switching Protocols"))
        messages = [
            json.loads(payload) for opcode, payload in server_frames(frames_wire) if opcode == 0x1
        ]
        return messages, meta, interventions

    def test_interrupted_answers_warmup_fault_then_continuation(self) -> None:
        compressor = zlib.compressobj(wbits=-15)
        messages, meta, interventions = self.serve("interrupted", [
            client_text({"type": "response.create", "generate": False}, compressor=compressor),
            frame(0x9, b"k", masked=True),
            client_text({"type": "response.create", "input": []}, compressor=compressor),
            client_text({"type": "response.create", "input": [], "previous_response_id": "x"}, compressor=compressor),
        ])
        self.assertEqual(meta["stream_fault_ws_actions"],
                         ["warmup_completed", "stream_fault_interrupted", "continuation_completed"])
        self.assertEqual(meta["intervention"], "stream_fault:interrupted:ws")
        self.assertIs(meta["production_forwarded"], False)
        terminal = [message["type"] for message in messages if message["type"] != "response.created"]
        self.assertEqual(terminal, ["response.completed", "response.incomplete",
                                    "response.output_item.done", "response.completed"])
        self.assertTrue(all(event["production_forwarded"] is False for event in interventions))

    def test_flex_unavailable_faults_only_the_first_generate_request(self) -> None:
        messages, meta, _ = self.serve("flex-unavailable", [
            client_text({"type": "response.create", "input": []}),
            client_text({"type": "response.create", "input": []}),
        ])
        self.assertEqual(meta["stream_fault_ws_actions"], ["stream_fault_flex-unavailable", "continuation_completed"])
        failed = next(message for message in messages if message["type"] == "response.failed")
        self.assertEqual(failed["response"]["error"]["code"], "flex_unavailable")

    def test_non_response_create_message_is_recorded_as_error(self) -> None:
        _messages, meta, _ = self.serve("interrupted", [client_text({"type": "session.update"})])
        self.assertEqual(meta["error"], "受控流内故障 WS 只接受 response.create")
        self.assertEqual(meta["stream_fault_ws_actions"], [])


class IdleCloseTest(unittest.TestCase):
    HANDSHAKE = b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\n\r\n"

    def pump(self, script) -> tuple[bytes, dict, float]:
        """驱动一次空闲关闭泵：脚本结束后才给两侧读端送 EOF，关闭时刻由客户端写端的 close 记录。"""

        async def exercise() -> tuple[bytes, dict, float]:
            with tempfile.TemporaryDirectory() as tmp:
                relay = relay_with(tmp, close_idle_ws_after="1")
                client_reader = asyncio.StreamReader()
                upstream_reader = asyncio.StreamReader()
                closed = asyncio.Event()
                loop = asyncio.get_running_loop()
                started = loop.time()
                closed_at: list[float] = []

                def on_close() -> None:
                    closed_at.append(loop.time() - started)
                    closed.set()

                client_writer = FakeWriter(on_close=on_close)
                upstream_writer = FakeWriter()
                meta: dict = {}
                pump = asyncio.create_task(relay._pump_ws_with_idle_close(
                    client_reader, client_writer, upstream_reader, upstream_writer,
                    FakeRecorder(), 7, meta,
                ))
                await script(client_reader, upstream_reader, closed)
                client_reader.feed_eof()
                upstream_reader.feed_eof()
                await asyncio.wait_for(pump, timeout=10)
                return bytes(client_writer.data), meta, (closed_at[0] if closed_at else -1.0)

        return asyncio.run(exercise())

    def test_server_pings_do_not_prevent_idle_close(self) -> None:
        async def script(client: asyncio.StreamReader, upstream: asyncio.StreamReader, closed: asyncio.Event) -> None:
            upstream.feed_data(self.HANDSHAKE)
            client.feed_data(client_text({"type": "response.create"}))
            upstream.feed_data(frame(0x1, b'{"type":"response.completed"}', masked=False))
            for _ in range(8):
                if closed.is_set():
                    break
                await asyncio.sleep(0.25)
                upstream.feed_data(frame(0x9, b"", masked=False))
                client.feed_data(frame(0xA, b"", masked=True))
            await asyncio.wait_for(closed.wait(), timeout=5)

        wire, meta, closed_at = self.pump(script)
        self.assertIn(b"\x88\x02\x03\xe8", wire, "应补发 CLOSE(1000)")
        self.assertEqual(meta["intervention"], "ws_idle_close")
        counts = meta["ws_idle_close"]["frames_before_close"]
        self.assertEqual(counts["client_to_upstream"]["data_frames"], 1)
        self.assertGreaterEqual(counts["upstream_to_client"]["ping_pong_frames"], 1)
        self.assertGreaterEqual(counts["client_to_upstream"]["ping_pong_frames"], 1)
        self.assertTrue(1.0 <= closed_at < 2.0, closed_at)

    def test_no_close_before_client_sends_a_business_frame(self) -> None:
        async def script(client: asyncio.StreamReader, upstream: asyncio.StreamReader, closed: asyncio.Event) -> None:
            upstream.feed_data(self.HANDSHAKE)
            client.feed_data(frame(0x9, b"", masked=True))
            await asyncio.sleep(1.6)

        wire, meta, closed_at = self.pump(script)
        self.assertNotIn("ws_idle_close", meta)
        self.assertNotIn(b"\x88\x02\x03\xe8", wire)
        self.assertEqual(closed_at, -1.0)

    def test_waits_for_frame_boundary_before_closing(self) -> None:
        partial = frame(0x1, b"z" * 300, masked=False)

        async def script(client: asyncio.StreamReader, upstream: asyncio.StreamReader, closed: asyncio.Event) -> None:
            upstream.feed_data(self.HANDSHAKE)
            client.feed_data(client_text({"type": "response.create"}))
            upstream.feed_data(partial[:10])
            await asyncio.sleep(1.6)
            self.assertFalse(closed.is_set(), "半帧期间不得关闭")
            upstream.feed_data(partial[10:])
            await asyncio.wait_for(closed.wait(), timeout=5)

        wire, meta, closed_at = self.pump(script)
        self.assertIn("ws_idle_close", meta)
        self.assertTrue(wire.endswith(partial + b"\x88\x02\x03\xe8"), "CLOSE 不得插进半帧")
        self.assertGreaterEqual(closed_at, 2.5)


class RelayArgumentValidationTest(unittest.TestCase):
    BASE = ["--cert", "missing.crt", "--key", "missing.key", "--output", "missing-output"]

    def run_relay(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RELAY_PY), *self.BASE, *extra],
            text=True, capture_output=True, check=False,
        )

    def test_invalid_official_directed_arguments_exit_2(self) -> None:
        cases = {
            ("--stream-fault", "interrupted"): "--stream-fault 与 --stream-fault-transport 必须同时提供",
            ("--stream-fault-transport", "ws"): "--stream-fault 与 --stream-fault-transport 必须同时提供",
            ("--synthesize-image-edit-file-id", "file-short"): "--synthesize-image-edit-file-id 必须形如",
            ("--close-idle-ws-after", "0"): "--close-idle-ws-after 必须是 1～600 的整数秒",
            ("--close-idle-ws-after", "601"): "--close-idle-ws-after 必须是 1～600 的整数秒",
            ("--retry-probe", "disconnect", "--retry-probe-retry-after", "2"):
                "--retry-probe-retry-after 只能与 --retry-probe keepalive-500 同用",
            ("--stream-fault", "interrupted", "--stream-fault-transport", "ws", "--close-idle-ws-after", "30"):
                "彼此互斥",
            ("--close-idle-ws-after", "30", "--force-ws-fallback-426"): "彼此互斥",
            ("--synthesize-image-edit-file-id", "file-c01592imageeditprobe", "--retry-probe", "keepalive-500"):
                "彼此互斥",
        }
        for arguments, message in cases.items():
            with self.subTest(arguments=arguments):
                result = self.run_relay(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)
        result = self.run_relay("--stream-fault", "throttled", "--stream-fault-transport", "ws")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)


class OfficialDirectedScenarioWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = RELAY_SCRIPT.read_text(encoding="utf-8")

    def run_until_validation(self, scenario: str, **environment: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUN_ID": "unit-directed-samples",
            "SCENARIO": scenario,
            "CODEX_VERSION": "0.159.2",
            "CAPTURE_HOST_DATA_ROOT": "/nonexistent-capture-root",
            **environment,
        }
        return subprocess.run([BASH, str(RELAY_SCRIPT)], env=env, text=True, capture_output=True, check=False)

    def assert_passes_validation(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("CAPTURE_HOST_DATA_ROOT 必须是可信的非根绝对目录", result.stderr)

    def test_illegal_combinations_exit_before_any_request(self) -> None:
        cases = [
            ("stream-fault", {}, "stream-fault 必须同时设置 RELAY_STREAM_FAULT 与 RELAY_STREAM_FAULT_TRANSPORT"),
            ("stream-fault", {"RELAY_STREAM_FAULT": "throttled", "RELAY_STREAM_FAULT_TRANSPORT": "ws"},
             "RELAY_STREAM_FAULT 只能是 flex-unavailable 或 interrupted"),
            ("stream-fault", {"RELAY_STREAM_FAULT": "interrupted", "RELAY_STREAM_FAULT_TRANSPORT": "h2"},
             "RELAY_STREAM_FAULT_TRANSPORT 只能是 http 或 ws"),
            ("image-edit-file-id", {}, "image-edit-file-id 必须设置 RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID"),
            ("image-edit-file-id", {"RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID": "file-bad"},
             "RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID 必须形如"),
            ("ws-idle-close-tui", {}, "ws-idle-close-tui 必须设置 RELAY_CLOSE_IDLE_WS_AFTER"),
            ("ws-idle-close-tui", {"RELAY_CLOSE_IDLE_WS_AFTER": "0"}, "RELAY_CLOSE_IDLE_WS_AFTER 必须是 1～600 的整数秒"),
            ("ws-idle-close-tui", {"RELAY_CLOSE_IDLE_WS_AFTER": "900"}, "RELAY_CLOSE_IDLE_WS_AFTER 必须是 1～600 的整数秒"),
            ("ws-default", {"RELAY_CLOSE_IDLE_WS_AFTER": "30"}, "只用于对应的定向样本场景"),
            ("image", {"RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID": "file-c01592imageeditprobe"}, "只用于对应的定向样本场景"),
            ("conn-retry", {"RELAY_RETRY_PROBE": "disconnect", "RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
             "RELAY_RETRY_PROBE_RETRY_AFTER 只能与 RELAY_RETRY_PROBE=keepalive-500 同用"),
            ("conn-retry", {"RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
             "RELAY_RETRY_PROBE_RETRY_AFTER 只能与 RELAY_RETRY_PROBE=keepalive-500 同用"),
        ]
        for scenario, environment, message in cases:
            with self.subTest(scenario=scenario, environment=environment):
                result = self.run_until_validation(scenario, **environment)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)

    def test_image_scenarios_require_0158(self) -> None:
        for scenario, environment in (
            ("image-transparent", {}),
            ("image-edit-file-id", {"RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID": "file-c01592imageeditprobe"}),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_until_validation(scenario, CODEX_VERSION="0.157.0", **environment)
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"{scenario} 需要 Codex >=0.158.0", result.stderr)
                self.assert_passes_validation(self.run_until_validation(scenario, **environment))

    def test_numeric_reasoning_effort_prompts(self) -> None:
        http_case = self.source[self.source.index("  reasoning-numeric-http)\n"):]
        http_case = http_case[:http_case.index(";;")]
        self.assertIn('extra_args="-c model_reasoning_effort=\\"3\\"$http_probe_provider_args"', http_case)
        ws_case = self.source[self.source.index("  reasoning-numeric-ws)\n"):]
        ws_case = ws_case[:ws_case.index(";;")]
        self.assertIn("extra_args='-c model_reasoning_effort=\"3\"'", ws_case)
        self.assertNotIn("http_probe_provider", ws_case)
        # 两种写法展开后都是 codex 的一个 -c 参数，取值是 TOML 字符串 "3"。
        script = (
            'http_probe_provider_args=" -c model_provider=openai-http-probe"\n'
            'extra_args="-c model_reasoning_effort=\\"3\\"$http_probe_provider_args"\n'
            "printf '%s\\n' $extra_args\n"
        )
        words = subprocess.run([BASH, "-c", script], text=True, capture_output=True, check=True).stdout.split("\n")
        self.assertEqual(words[:4], ["-c", 'model_reasoning_effort="3"', "-c", "model_provider=openai-http-probe"])

    def test_http_probe_provider_matches_http_response_overrides(self) -> None:
        start = self.source.index("http_probe_provider_config=(\n") + len("http_probe_provider_config=(\n")
        items = [line.strip().strip('"') for line in self.source[start:self.source.index("\n)\n", start)].splitlines()]
        http_response = next(
            line for line in self.source.splitlines()
            if line.strip().startswith('extra_args="-c model_provider=openai-http-probe')
        )
        self.assertEqual(" ".join(f"-c {item}" for item in items), http_response.strip()[len('extra_args="'):-len('" ;;')])


class Scenario0159ManifestParameterTest(unittest.TestCase):
    """0.159.2 清单：沿用 0.157.0 的全部作业执行参数，新增 9 个官方定向样本作业。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_159_2.json").read_text(encoding="utf-8"))
        previous = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_157_0.json").read_text(encoding="utf-8"))
        cls.previous_jobs = {job["id"]: job for job in previous["capture_jobs"]}
        cls.jobs = {job["id"]: job for job in cls.manifest["capture_jobs"]}
        labels = json.loads((TOOL_ROOT / "codex_upgrade_evidence_labels_0_159_2.json").read_text(encoding="utf-8"))
        cls.labels = {entry["job_id"]: entry for entry in labels["entries"]}

    def test_carried_jobs_keep_0157_execution_parameters(self) -> None:
        self.assertEqual(set(self.jobs) - set(self.previous_jobs), NEW_OFFICIAL_JOBS)
        self.assertEqual(set(self.previous_jobs) - set(self.jobs), set())
        for job_id, previous in self.previous_jobs.items():
            with self.subTest(job_id=job_id):
                current = self.jobs[job_id]
                for field in ("phase", "suites", "scenario_ids", "steps", "evidence_roots", "covers"):
                    self.assertEqual(current[field], previous[field], field)

    def test_new_job_environments_pass_script_validation(self) -> None:
        substitutions = {
            "{capture_container}": "capture-cli",
            "{capture_root}": "/root/oauth-capture",
            "{target_version}": "0.159.2",
            "{relay_codex_bin}": "/opt/codex/bin/codex",
            "{model}": "gpt-5.5",
            "{lite_model}": "gpt-6-astra",
            "{campaign_id}": "c01592-unit",
        }
        for job_id in sorted(NEW_OFFICIAL_JOBS):
            with self.subTest(job_id=job_id):
                job = self.jobs[job_id]
                self.assertEqual(job["phase"], "official")
                self.assertEqual(len(job["steps"]), 1)
                environment = {}
                for key, value in job["steps"][0]["environment"].items():
                    for placeholder, concrete in substitutions.items():
                        value = value.replace(placeholder, concrete)
                    environment[key] = value
                environment["CAPTURE_HOST_DATA_ROOT"] = "/nonexistent-capture-root"
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environment}
                result = subprocess.run([BASH, str(RELAY_SCRIPT)], env=env, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("CAPTURE_HOST_DATA_ROOT 必须是可信的非根绝对目录", result.stderr)
                suffix = job["steps"][0]["environment"]["RUN_ID"].removeprefix("{campaign_id}")
                self.assertEqual(job["evidence_roots"], ["{capture_root}/runs/{campaign_id}" + suffix])

    def test_new_jobs_bind_scenarios_models_and_request_assertions(self) -> None:
        expected = {
            "official-relay-conn-retry-after": (["A07", "A08"], "conn-retry", None),
            "official-relay-reasoning-numeric-http": (["A04"], "reasoning-numeric-http", ("POST", "/backend-api/codex/responses")),
            "official-relay-reasoning-numeric-ws": (["A05"], "reasoning-numeric-ws", ("GET", "/backend-api/codex/responses")),
            "official-relay-stream-flex-unavailable": (["A07"], "stream-fault", ("GET", "/backend-api/codex/responses")),
            "official-relay-stream-interrupted-http": (["A07"], "stream-fault", ("POST", "/backend-api/codex/responses")),
            "official-relay-stream-interrupted-ws": (["A07"], "stream-fault", ("GET", "/backend-api/codex/responses")),
            "official-relay-ws-idle-close": (["A07"], "ws-idle-close-tui", ("GET", "/backend-api/codex/responses")),
            "official-relay-image-transparent": (["A09"], "image-transparent", ("POST", "/backend-api/codex/images/generations")),
            "official-relay-image-edit-file-id": (["A09"], "image-edit-file-id", ("POST", "/backend-api/codex/images/edits")),
        }
        for job_id, (scenarios, scenario, request) in expected.items():
            with self.subTest(job_id=job_id):
                job = self.jobs[job_id]
                environment = job["steps"][0]["environment"]
                self.assertEqual(job["scenario_ids"], scenarios)
                self.assertEqual(environment["SCENARIO"], scenario)
                if request is None:
                    self.assertNotIn("REQUIRE_REQUEST_PATH", environment)
                else:
                    self.assertEqual((environment["REQUIRE_REQUEST_METHOD"], environment["REQUIRE_REQUEST_PATH"]), request)
        numeric_ws = self.jobs["official-relay-reasoning-numeric-ws"]
        self.assertEqual(numeric_ws["steps"][0]["environment"]["MODEL"], "{lite_model}")
        self.assertEqual((numeric_ws["track"], numeric_ws["expected_use_responses_lite"], numeric_ws["required_model_receipt"]),
                         ("lite", True, False))
        numeric_http = self.jobs["official-relay-reasoning-numeric-http"]
        self.assertEqual((numeric_http["track"], numeric_http["required_model_receipt"]), ("main", False))
        retry_after = self.jobs["official-relay-conn-retry-after"]["steps"][0]["environment"]
        retry = self.jobs["official-relay-conn-retry"]["steps"][0]["environment"]
        self.assertEqual(retry_after["RELAY_RETRY_PROBE_RETRY_AFTER"], "2")
        self.assertEqual(
            {key: value for key, value in retry_after.items() if key not in {"RUN_ID", "RELAY_RETRY_PROBE_RETRY_AFTER"}},
            {key: value for key, value in retry.items() if key != "RUN_ID"},
            "有值对照与无值样本只差 Retry-After 一个条件",
        )
        idle = self.jobs["official-relay-ws-idle-close"]["steps"][0]["environment"]
        self.assertLess(int(idle["RELAY_CLOSE_IDLE_WS_AFTER"]), int(idle["TUI_HOLD"]))
        a05 = next(item for item in self.manifest["evidence_scenarios"] if item["scenario_id"] == "A05")
        self.assertIn("SPEC-BODY-006", a05["covers"])

    def test_new_jobs_have_label_entries_marking_synthetic_upstream(self) -> None:
        synthetic = {
            "official-relay-stream-flex-unavailable",
            "official-relay-stream-interrupted-http",
            "official-relay-stream-interrupted-ws",
            "official-relay-image-edit-file-id",
        }
        for job_id in sorted(NEW_OFFICIAL_JOBS):
            with self.subTest(job_id=job_id):
                entry = self.labels[job_id]
                self.assertEqual(entry["side"], "official")
                for rule in entry["rules"]:
                    self.assertLessEqual(set(rule["scenario_ids"]), set(self.jobs[job_id]["scenario_ids"]))
                    self.assertEqual(rule["glob"], "relay/*.client_to_upstream.bin")
                    self.assertEqual(rule["labels"].get("upstream_response") == "relay_synthetic", job_id in synthetic)
                    self.assertNotEqual(rule["labels"].get("variant"), "http_default", "不得冒充 http_default 样本")


if __name__ == "__main__":
    unittest.main()
