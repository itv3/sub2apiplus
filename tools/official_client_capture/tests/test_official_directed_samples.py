"""0.159.2 起的官方定向样本（0.160.0、0.163.0 目标清单承接）的无网络测试：中继受控合成、空闲关闭、参数校验、采集脚本接线与场景清单。

第 0b 步为 VC-2 需要实测确认的四类新条件补了官方定向样本：流内 flex_unavailable／interrupted（中继受控
应答）、空闲 WS 被服务端关闭后直接重建（中继补 CLOSE）、Retry-After 有值对照（受控 500 附带该头）、
数字推理等级（配置自定义档位）与生图透明背景／file_id 编辑（file_id 链由中继受控应答）。触发器属 wire 层，
必须在 VC-0 冻结前一次做对，这里逐项锁定：

- 合成事件形状与 0.159.2 源码的解析要求一致（response.id、usage 三个必填计数、错误码与 incomplete 原因）；
- WS 受控服务按预热／首个生成请求／续发三类应答，服务端帧不掩码；
- 空闲关闭只看业务帧，ping／pong 不刷新计时，且只在两个方向都停在帧边界时补 CLOSE；
- 中继与采集脚本都在任何请求之前拒绝非法取值与错误搭配；
- 0.160.0 场景清单（由 0.159.2 版改名）沿用作业的执行参数与 0.157.0 逐字相同，新增作业的环境能通过脚本校验；
  0.163.0 场景清单（由 0.160.0 版复制并替换版本号）同样如此。

0.163.0 在建 Campaign 之前又补了三个定向样本（提交 A）：exec 的 --cyber-access-program standard 固定 WS 帧末位的条件字段
access_programs（A05，Lite）；流内 response.incomplete(max_output_tokens) 的终止语义（HTTP、WS 各一，A07）——本轮结束、
不重试、不降级，采集脚本接受 codex exec 的非零退出码，并核对注入之后没有任何 Responses 请求。

提交 B 又补了失败路径的三个定向样本（都只记时间线，不对客户端行为设硬门限）：WS 升级回 429＋Retry-After 的重试与降级前等待
（A07，中继对每个 Responses WS 升级都回 429，降级 POST 转发生产）；HTTP 503 server_is_overloaded 带与不带 Retry-After 的一对
（A07／A08，conn-retry 同族）。后者中继对**每个** Responses POST 都回 503——传输层本来就对所有 5xx 重试 4 次，只有重试预算用尽之后
才暴露 0.163 的采样层差异（ServerOverloaded 带 Retry-After 才再重试）。
"""

from __future__ import annotations

import asyncio
import base64
import copy
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

from tools.official_client_capture.tests.official_relay_harness import run_script_check, script_check_code
from tools.official_client_capture.upstream_byte_relay import (
    _OFFICIAL_IMAGE_EDIT_CALL_ID,
    _OVERLOADED_503_BODY,
    Relay,
    _WsFrameActivity,
    _decoded_request_json,
    _image_edit_chain_decision,
    _official_image_response,
    _official_imagegen_call_events,
    _official_message_events,
    _official_sse_response,
    _official_stream_fault_events,
    _official_ws_handshake,
    _overloaded_503_response,
    _post_terminal_request_kind,
    _ws_upgrade_429_fallback_post,
    _ws_upgrade_429_response,
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
# 0.163.0 清单在 0.160.0 的基础上（建 Campaign 之前）新增的官方定向样本。
NEW_0163_JOBS = {
    "official-relay-cyber-access-ws",
    "official-relay-stream-incomplete-http",
    "official-relay-stream-incomplete-ws",
    "official-relay-ws-retry-after",
    "official-relay-server-overloaded-retry-after",
    "official-relay-server-overloaded",
}
# 含中继合成应答的作业（证据标签必须标 upstream_response=relay_synthetic）。
SYNTHETIC_JOBS = {
    "official-relay-stream-flex-unavailable",
    "official-relay-stream-interrupted-http",
    "official-relay-stream-interrupted-ws",
    "official-relay-image-edit-file-id",
}
SYNTHETIC_0163_JOBS = {
    "official-relay-stream-incomplete-http",
    "official-relay-stream-incomplete-ws",
    "official-relay-ws-retry-after",
    "official-relay-server-overloaded-retry-after",
    "official-relay-server-overloaded",
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
        "ws_upgrade_429_retry_after": "",
        "retry_probe": "",
        "retry_probe_target": "models",
        "retry_probe_retry_after": "",
        "retry_probe_wait": 15.0,
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

    def test_incomplete_terminal_is_incomplete_with_max_output_tokens(self) -> None:
        """0.163 起的终止型故障：created 之后 response.incomplete，reason 既非 interrupted 也非 content_filter。

        形状按 codex-api/src/sse/responses.rs 第 419-453 行 [0.163.0]：reason 取 incomplete_details.reason，
        response.id 与 usage 被读进 ResponseIncomplete（usage 解析失败只是丢弃，不影响分类）。
        """

        created, incomplete = _official_stream_fault_events("incomplete-terminal", "resp_t")
        self.assertEqual(created["type"], "response.created")
        self.assertEqual(incomplete["type"], "response.incomplete")
        self.assert_response_contract(incomplete["response"], "resp_t")
        self.assertEqual(incomplete["response"]["status"], "incomplete")
        self.assertEqual(incomplete["response"]["incomplete_details"], {"reason": "max_output_tokens"})
        self.assertNotIn(incomplete["response"]["incomplete_details"]["reason"], {"interrupted", "content_filter"})
        usage = incomplete["response"]["usage"]
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            self.assertIsInstance(usage[field], int, field)
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        # 与 interrupted 的差别只有 reason 与 usage 量：事件类型、状态、响应对象结构完全同构。
        _created, interrupted = _official_stream_fault_events("interrupted", "resp_t")
        self.assertEqual(set(incomplete["response"]), set(interrupted["response"]))
        self.assertEqual(incomplete["type"], interrupted["type"])

    def test_post_terminal_request_kind_marks_only_requests_after_the_fault(self) -> None:
        post = "POST /backend-api/codex/responses HTTP/1.1"
        upgrade = "GET /backend-api/codex/responses HTTP/1.1"
        # 终止型故障尚未注入：任何请求都不是「故障之后」。
        self.assertIsNone(_post_terminal_request_kind("incomplete-terminal", False, is_responses_ws=True, request_line=upgrade))
        self.assertIsNone(_post_terminal_request_kind("incomplete-terminal", False, is_responses_ws=False, request_line=post))
        # 注入之后：新的 WS 升级（重试）与 HTTP POST（降级或重试）分别标记。
        self.assertEqual(
            _post_terminal_request_kind("incomplete-terminal", True, is_responses_ws=True, request_line=upgrade), "ws_upgrade"
        )
        self.assertEqual(
            _post_terminal_request_kind("incomplete-terminal", True, is_responses_ws=False, request_line=post), "http_post"
        )
        # 其它请求（models、accounts/check、analytics 等）不是 Responses 请求，不标记。
        self.assertIsNone(_post_terminal_request_kind(
            "incomplete-terminal", True, is_responses_ws=False,
            request_line="GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1",
        ))
        self.assertIsNone(_post_terminal_request_kind(
            "incomplete-terminal", True, is_responses_ws=False,
            request_line="POST /backend-api/codex/analytics-events/events HTTP/1.1",
        ))
        # 其它故障种类（interrupted 的续发是预期行为）与未配置故障都不标记。
        for kind in ("interrupted", "flex-unavailable", ""):
            with self.subTest(kind=kind):
                self.assertIsNone(_post_terminal_request_kind(kind, True, is_responses_ws=True, request_line=upgrade))
                self.assertIsNone(_post_terminal_request_kind(kind, True, is_responses_ws=False, request_line=post))

    def test_message_and_imagegen_call_complete_with_id_and_usage(self) -> None:
        message_events = _official_message_events("resp_m", "EDIT-OK")
        self.assertEqual([event["type"] for event in message_events],
                         ["response.created", "response.output_item.done", "response.completed"])
        self.assertEqual(message_events[1]["item"]["content"][0]["text"], "EDIT-OK")
        call_events = _official_imagegen_call_events("resp_c")
        call = call_events[1]["item"]
        self.assertEqual((call["type"], call["name"], call["namespace"]), ("function_call", "imagegen", "image_gen"))
        self.assertEqual(call["call_id"], _OFFICIAL_IMAGE_EDIT_CALL_ID)
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


class FailurePathResponseTest(unittest.TestCase):
    """0.163.0 失败路径定向样本：受控 503／429 的字节形状与降级 POST 的时间线标记（纯函数，不起中继）。"""

    POST = "POST /backend-api/codex/responses HTTP/1.1"

    @staticmethod
    def split(response: bytes) -> tuple[list[str], dict[str, str], bytes]:
        head, _, body = response.partition(b"\r\n\r\n")
        lines = head.decode("ascii").split("\r\n")
        return lines, dict(line.split(": ", 1) for line in lines[1:]), body

    def test_overloaded_503_carries_the_server_is_overloaded_code_and_optional_retry_after(self) -> None:
        for retry_after in ("", "2"):
            with self.subTest(retry_after=retry_after):
                lines, headers, body = self.split(_overloaded_503_response(retry_after))
                self.assertEqual(lines[0], "HTTP/1.1 503 Service Unavailable")
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["connection"], "keep-alive", "保持连接：重试是否复用连接是 CONN-001 的核对对象")
                self.assertEqual(int(headers["content-length"]), len(body))
                # 客户端按 body 的 error.code 把 503 映射成 ServerOverloaded（codex-api/src/api_bridge.rs 第 105-110 行 [0.163.0]）。
                self.assertEqual(json.loads(body), {"error": {"code": "server_is_overloaded"}})
                self.assertEqual(body, _OVERLOADED_503_BODY)
                if retry_after:
                    self.assertEqual(headers["retry-after"], retry_after)
                else:
                    self.assertNotIn("retry-after", headers)

    def test_ws_upgrade_429_has_an_empty_body_a_retry_after_and_closes_the_connection(self) -> None:
        lines, headers, body = self.split(_ws_upgrade_429_response("2"))
        self.assertEqual(lines[0], "HTTP/1.1 429 Too Many Requests")
        self.assertEqual(headers, {"retry-after": "2", "content-length": "0", "connection": "close"})
        self.assertEqual(body, b"")

    def test_fallback_post_marker_records_the_wait_since_the_last_429(self) -> None:
        marker = _ws_upgrade_429_fallback_post(
            retry_after_seconds=2, upgrade_count=6, last_sent_unix_ms=1_000, head_received_unix_ms=3_050,
            request_line=self.POST,
        )
        self.assertEqual(marker, {
            "ws_upgrade_429_count": 6,
            "retry_after_seconds": 2,
            "last_ws_upgrade_429_sent_unix_ms": 1_000,
            "request_head_received_unix_ms": 3_050,
            "since_last_ws_upgrade_429_ms": 2_050,
        })

    def test_fallback_post_marker_only_marks_responses_posts_after_a_429(self) -> None:
        common = {"retry_after_seconds": 2, "upgrade_count": 3, "last_sent_unix_ms": 1_000, "head_received_unix_ms": 1_500}
        for label, overrides in (
            ("还没有升级 429", {"upgrade_count": 0, "last_sent_unix_ms": None}),
            ("有计数却没有时刻", {"last_sent_unix_ms": None}),
            ("WS 升级本身不是降级请求", {"request_line": "GET /backend-api/codex/responses HTTP/1.1"}),
            ("其它端点的 POST", {"request_line": "POST /backend-api/codex/responses/compact HTTP/1.1"}),
            ("models 查询", {"request_line": "GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1"}),
        ):
            with self.subTest(label):
                self.assertIsNone(_ws_upgrade_429_fallback_post(**{"request_line": self.POST, **common, **overrides}))


class ImageEditChainDecisionTest(unittest.TestCase):
    """file_id 编辑链按请求内容选择受控应答，不按到达序号猜测。"""

    FILE_ID = "file-c01592imageeditprobe"

    def user_turn(self) -> dict:
        return {"model": "gpt-5.5", "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "edit"}, {"type": "input_image", "file_id": self.FILE_ID}]}]}

    def test_user_turn_with_file_image_gets_the_edit_call_once(self) -> None:
        self.assertEqual(_image_edit_chain_decision(self.user_turn(), self.FILE_ID, False), "imagegen_edit_call")
        self.assertEqual(_image_edit_chain_decision(self.user_turn(), self.FILE_ID, True), "other_completed")

    def test_tool_output_for_the_controlled_call_gets_the_final_message(self) -> None:
        payload = self.user_turn()
        payload["input"] += [
            {"type": "function_call", "call_id": _OFFICIAL_IMAGE_EDIT_CALL_ID, "name": "imagegen"},
            {"type": "function_call_output", "call_id": _OFFICIAL_IMAGE_EDIT_CALL_ID, "output": "ok"},
        ]
        self.assertEqual(_image_edit_chain_decision(payload, self.FILE_ID, True), "final_message")
        other = {"input": [{"type": "function_call_output", "call_id": "call_other", "output": "x"}]}
        self.assertEqual(_image_edit_chain_decision(other, self.FILE_ID, True), "other_completed")

    def test_requests_without_the_file_image_do_not_advance_the_chain(self) -> None:
        payload = {"input": [{"type": "message", "role": "user", "content": "title please"}]}
        self.assertEqual(_image_edit_chain_decision(payload, self.FILE_ID, False), "other_completed")
        self.assertEqual(_image_edit_chain_decision(None, self.FILE_ID, False), "undecodable")

    def test_request_body_decoding_follows_content_encoding(self) -> None:
        payload = self.user_turn()
        raw = json.dumps(payload).encode("utf-8")
        plain = b"POST /backend-api/codex/responses HTTP/1.1\r\ncontent-type: application/json\r\n\r\n"
        self.assertEqual(_decoded_request_json(plain, raw), payload)
        gzip_head = b"POST /backend-api/codex/responses HTTP/1.1\r\ncontent-encoding: gzip\r\n\r\n"
        self.assertIsNone(_decoded_request_json(gzip_head, raw))
        self.assertIsNone(_decoded_request_json(plain, b"not json"))
        zstd_head = b"POST /backend-api/codex/responses HTTP/1.1\r\nContent-Encoding: zstd\r\n\r\n"
        try:
            import zstandard
        except ImportError:
            # 采集容器装有 zstandard；缺失的宿主只能失败关闭为无法解码，由调用方按到达序号兜底。
            self.assertIsNone(_decoded_request_json(zstd_head, raw))
        else:
            compressed = zstandard.ZstdCompressor().compress(raw)
            self.assertEqual(_decoded_request_json(zstd_head, compressed), payload)
            self.assertIsNone(_decoded_request_json(zstd_head, b"\x28\xb5\x2f\xfd broken"))


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

    def test_incomplete_terminal_faults_once_and_leaves_no_post_terminal_mark(self) -> None:
        """0.163 的终止语义：预热完成、首个生成请求回终止型 incomplete，之后客户端不再发生成请求。"""

        compressor = zlib.compressobj(wbits=-15)
        messages, meta, interventions = self.serve("incomplete-terminal", [
            client_text({"type": "response.create", "generate": False}, compressor=compressor),
            client_text({"type": "response.create", "input": []}, compressor=compressor),
        ])
        self.assertEqual(meta["stream_fault_ws_actions"], ["warmup_completed", "stream_fault_incomplete-terminal"])
        self.assertEqual(meta["intervention"], "stream_fault:incomplete-terminal:ws")
        self.assertIs(meta["production_forwarded"], False)
        self.assertNotIn("post_terminal_request", meta)
        terminal = [message for message in messages if message["type"] == "response.incomplete"]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["response"]["incomplete_details"], {"reason": "max_output_tokens"})
        self.assertTrue(all(event["production_forwarded"] is False for event in interventions))

    def test_incomplete_terminal_marks_a_generate_request_after_the_fault(self) -> None:
        """故障之后同连接又来生成请求：客户端把终止当成可续发，行为偏离，必须留标记（请求照常合成完成并记录）。"""

        messages, meta, _ = self.serve("incomplete-terminal", [
            client_text({"type": "response.create", "input": []}),
            client_text({"type": "response.create", "input": [], "previous_response_id": "x"}),
        ])
        self.assertEqual(meta["stream_fault_ws_actions"], ["stream_fault_incomplete-terminal", "continuation_completed"])
        self.assertEqual(meta["post_terminal_request"], "ws_generate")
        self.assertEqual([m["type"] for m in messages if m["type"] != "response.created"],
                         ["response.incomplete", "response.output_item.done", "response.completed"])

    def test_other_kinds_never_carry_the_post_terminal_mark(self) -> None:
        """interrupted 的续发是预期行为，不得被当成偏离。"""

        _messages, meta, _ = self.serve("interrupted", [
            client_text({"type": "response.create", "input": []}),
            client_text({"type": "response.create", "input": []}),
        ])
        self.assertEqual(meta["stream_fault_ws_actions"], ["stream_fault_interrupted", "continuation_completed"])
        self.assertNotIn("post_terminal_request", meta)

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
            ("--stream-fault", "incomplete-terminal"): "--stream-fault 与 --stream-fault-transport 必须同时提供",
            ("--stream-fault-transport", "ws"): "--stream-fault 与 --stream-fault-transport 必须同时提供",
            ("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http", "--force-ws-fallback-426"):
                "彼此互斥",
            ("--synthesize-image-edit-file-id", "file-short"): "--synthesize-image-edit-file-id 必须形如",
            ("--close-idle-ws-after", "0"): "--close-idle-ws-after 必须是 1～600 的整数秒",
            ("--close-idle-ws-after", "601"): "--close-idle-ws-after 必须是 1～600 的整数秒",
            ("--retry-probe", "disconnect", "--retry-probe-retry-after", "2"):
                "--retry-probe-retry-after 只能与 --retry-probe keepalive-500 或 persistent-503-overloaded 同用",
            ("--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses",
             "--retry-probe-retry-after", "soon"):
                "--retry-probe-retry-after 只能与 --retry-probe keepalive-500 或 persistent-503-overloaded 同用",
            ("--retry-probe", "persistent-503-overloaded"):
                "--retry-probe persistent-503-overloaded 只适用于 --retry-probe-target responses",
            ("--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "models"):
                "--retry-probe persistent-503-overloaded 只适用于 --retry-probe-target responses",
            ("--ws-upgrade-429-retry-after", "0"): "--ws-upgrade-429-retry-after 必须是 1～600 的整数秒",
            ("--ws-upgrade-429-retry-after", "601"): "--ws-upgrade-429-retry-after 必须是 1～600 的整数秒",
            ("--ws-upgrade-429-retry-after", "2s"): "--ws-upgrade-429-retry-after 必须是 1～600 的整数秒",
            ("--ws-upgrade-429-retry-after", "2", "--force-ws-fallback-426"): "彼此互斥",
            ("--ws-upgrade-429-retry-after", "2", "--retry-probe", "keepalive-500"): "彼此互斥",
            ("--ws-upgrade-429-retry-after", "2", "--stream-fault", "interrupted", "--stream-fault-transport", "ws"):
                "彼此互斥",
            ("--ws-upgrade-429-retry-after", "2", "--close-idle-ws-after", "30"): "彼此互斥",
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
        # 合法种类清单（argparse 在非法取值的报错里列出）含 0.163 起的终止型故障。
        self.assertIn("incomplete-terminal", result.stderr)


class OfficialDirectedScenarioWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = RELAY_SCRIPT.read_text(encoding="utf-8")

    def run_until_validation(self, scenario: str, **environment: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUN_ID": "unit-directed-samples",
            "SCENARIO": scenario,
            "CODEX_VERSION": "0.163.0",
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
             "RELAY_STREAM_FAULT 只能是 flex-unavailable、interrupted 或 incomplete-terminal"),
            ("stream-fault", {"RELAY_STREAM_FAULT": "interrupted", "RELAY_STREAM_FAULT_TRANSPORT": "h2"},
             "RELAY_STREAM_FAULT_TRANSPORT 只能是 http 或 ws"),
            ("stream-fault", {"RELAY_STREAM_FAULT": "incomplete-terminal"},
             "stream-fault 必须同时设置 RELAY_STREAM_FAULT 与 RELAY_STREAM_FAULT_TRANSPORT"),
            ("stream-fault", {"RELAY_STREAM_FAULT": "incomplete-terminal", "RELAY_STREAM_FAULT_TRANSPORT": "h2"},
             "RELAY_STREAM_FAULT_TRANSPORT 只能是 http 或 ws"),
            ("ws-default", {"RELAY_STREAM_FAULT": "incomplete-terminal", "RELAY_STREAM_FAULT_TRANSPORT": "ws"},
             "只用于对应的定向样本场景"),
            ("image-edit-file-id", {}, "image-edit-file-id 必须设置 RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID"),
            ("image-edit-file-id", {"RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID": "file-bad"},
             "RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID 必须形如"),
            ("ws-idle-close-tui", {}, "ws-idle-close-tui 必须设置 RELAY_CLOSE_IDLE_WS_AFTER"),
            ("ws-idle-close-tui", {"RELAY_CLOSE_IDLE_WS_AFTER": "0"}, "RELAY_CLOSE_IDLE_WS_AFTER 必须是 1～600 的整数秒"),
            ("ws-idle-close-tui", {"RELAY_CLOSE_IDLE_WS_AFTER": "900"}, "RELAY_CLOSE_IDLE_WS_AFTER 必须是 1～600 的整数秒"),
            ("ws-default", {"RELAY_CLOSE_IDLE_WS_AFTER": "30"}, "只用于对应的定向样本场景"),
            ("image", {"RELAY_SYNTHESIZE_IMAGE_EDIT_FILE_ID": "file-c01592imageeditprobe"}, "只用于对应的定向样本场景"),
            ("conn-retry", {"RELAY_RETRY_PROBE": "disconnect", "RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
             "RELAY_RETRY_PROBE_RETRY_AFTER 只能与 RELAY_RETRY_PROBE=keepalive-500 或 persistent-503-overloaded 同用"),
            ("conn-retry", {"RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
             "RELAY_RETRY_PROBE_RETRY_AFTER 只能与 RELAY_RETRY_PROBE=keepalive-500 或 persistent-503-overloaded 同用"),
            ("conn-retry", {"RELAY_RETRY_PROBE": "keepalive-503"},
             "RELAY_RETRY_PROBE 只能是 keepalive-500、disconnect 或 persistent-503-overloaded"),
            ("conn-retry", {"RELAY_RETRY_PROBE": "persistent-503-overloaded", "RELAY_RETRY_PROBE_TARGET": "responses",
                            "RELAY_RETRY_PROBE_RETRY_AFTER": "soon"},
             "RELAY_RETRY_PROBE_RETRY_AFTER 只能与 RELAY_RETRY_PROBE=keepalive-500 或 persistent-503-overloaded 同用"),
            ("ws-retry-after", {}, "ws-retry-after 必须设置 RELAY_WS_UPGRADE_429_RETRY_AFTER"),
            ("ws-retry-after", {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "0"},
             "RELAY_WS_UPGRADE_429_RETRY_AFTER 必须是 1～600 的整数秒"),
            ("ws-retry-after", {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "601"},
             "RELAY_WS_UPGRADE_429_RETRY_AFTER 必须是 1～600 的整数秒"),
            ("ws-default", {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "2"}, "只用于对应的定向样本场景"),
            ("conn-retry", {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "2"}, "只用于对应的定向样本场景"),
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

    def test_incomplete_terminal_requires_0163_and_passes_on_both_transports(self) -> None:
        for transport in ("http", "ws"):
            environment = {"RELAY_STREAM_FAULT": "incomplete-terminal", "RELAY_STREAM_FAULT_TRANSPORT": transport}
            with self.subTest(transport=transport):
                old = self.run_until_validation("stream-fault", CODEX_VERSION="0.160.0", **environment)
                self.assertEqual(old.returncode, 2, old.stderr)
                self.assertIn("RELAY_STREAM_FAULT=incomplete-terminal 需要 Codex >=0.163.0", old.stderr)
                self.assert_passes_validation(self.run_until_validation("stream-fault", **environment))
        # 既有两个故障种类不受 0.163 门限影响（0.159 起即可采）。
        for kind in ("flex-unavailable", "interrupted"):
            with self.subTest(kind=kind):
                self.assert_passes_validation(self.run_until_validation(
                    "stream-fault", CODEX_VERSION="0.160.0",
                    RELAY_STREAM_FAULT=kind, RELAY_STREAM_FAULT_TRANSPORT="ws",
                ))

    def test_cyber_access_scenario_requires_0161_and_fixes_the_program_on_exec(self) -> None:
        old = self.run_until_validation("cyber-access-ws", CODEX_VERSION="0.160.0")
        self.assertEqual(old.returncode, 2, old.stderr)
        self.assertIn("cyber-access-ws 需要 Codex >=0.161.0", old.stderr)
        self.assert_passes_validation(self.run_until_validation("cyber-access-ws"))
        case = self.source[self.source.index("  cyber-access-ws)\n"):]
        case = case[:case.index(";;")]
        self.assertIn("extra_args='--cyber-access-program standard'", case)
        self.assertNotIn("http_probe_provider", case, "access_programs 按 provider id 过滤：必须走内置 provider 的默认 WS")
        # 展开后是 codex exec 的两个词，取值属于 exec/src/cli.rs 的 ValueEnum（standard|daybreak_blue|daybreak_red）。
        words = subprocess.run(
            [BASH, "-c", "extra_args='--cyber-access-program standard'\nprintf '%s\\n' $extra_args\n"],
            text=True, capture_output=True, check=True,
        ).stdout.split("\n")
        self.assertEqual(words[:2], ["--cyber-access-program", "standard"])
        # 该场景不需要任何中继受控开关：误带故障开关时按「只用于对应的定向样本场景」拒绝。
        misuse = self.run_until_validation(
            "cyber-access-ws", RELAY_STREAM_FAULT="incomplete-terminal", RELAY_STREAM_FAULT_TRANSPORT="ws"
        )
        self.assertEqual(misuse.returncode, 2, misuse.stderr)
        self.assertIn("只用于对应的定向样本场景", misuse.stderr)

    def test_ws_retry_after_requires_0163_and_runs_on_the_builtin_ws_provider(self) -> None:
        environment = {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "2"}
        old = self.run_until_validation("ws-retry-after", CODEX_VERSION="0.160.0", **environment)
        self.assertEqual(old.returncode, 2, old.stderr)
        self.assertIn("ws-retry-after 需要 Codex >=0.163.0", old.stderr)
        self.assert_passes_validation(self.run_until_validation("ws-retry-after", **environment))
        case = self.source[self.source.index("  ws-retry-after)\n"):]
        case = case[:case.index(";;")]
        self.assertIn("prompt='请只回复 OK，不要做任何其他事。'", case)
        # 内置 provider 默认 WS：不换 provider、不关任何 feature，客户端字节与普通 WS 轮次相同，差别全在中继的 429。
        self.assertNotIn("extra_args", case)
        self.assertNotIn("http_probe_provider", case)

    def test_persistent_overloaded_probe_needs_the_responses_target_and_0163(self) -> None:
        base = {"RELAY_RETRY_PROBE": "persistent-503-overloaded", "RELAY_RETRY_PROBE_TARGET": "responses"}
        for environment in (
            base,
            {**base, "RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
            {**base, "RELAY_FORCE_WS_FALLBACK_426": "1", "RELAY_RETRY_PROBE_RETRY_AFTER": "2"},
        ):
            with self.subTest(environment=environment):
                self.assert_passes_validation(self.run_until_validation("conn-retry", **environment))
                old = self.run_until_validation("conn-retry", CODEX_VERSION="0.160.0", **environment)
                self.assertEqual(old.returncode, 2, old.stderr)
                self.assertIn("RELAY_RETRY_PROBE=persistent-503-overloaded 需要 Codex >=0.163.0", old.stderr)
        for extra in ({}, {"RELAY_RETRY_PROBE_TARGET": "models"}):
            with self.subTest(extra=extra):
                result = self.run_until_validation("conn-retry", RELAY_RETRY_PROBE="persistent-503-overloaded", **extra)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("只适用于 RELAY_RETRY_PROBE_TARGET=responses", result.stderr)
        # 既有两个探针取值与 Retry-After 的搭配不受影响（0.145 起即可采）。
        for mode in ("keepalive-500", "disconnect"):
            with self.subTest(mode=mode):
                self.assert_passes_validation(self.run_until_validation(
                    "conn-retry", CODEX_VERSION="0.160.0", RELAY_RETRY_PROBE=mode, RELAY_RETRY_PROBE_TARGET="responses",
                ))

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


class TerminalFaultCheckTest(unittest.TestCase):
    """采集脚本里的「终止型流内故障核对」：取出脚本内嵌的 Python 原样执行（假 relay.json），锁定判据。"""

    @classmethod
    def setUpClass(cls) -> None:
        source = RELAY_SCRIPT.read_text(encoding="utf-8")
        start = source.index('terminal_check=$(docker exec -i "$capture_container" python3 - \\\n')
        body_start = source.index("<<'PY'\n", start) + len("<<'PY'\n")
        cls.code = source[body_start:source.index("\nPY\n", body_start) + 1]

    def run_check(self, connections: list[dict], transport: str, exec_status: str = "1") -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            relay_json = Path(tmp) / "relay.json"
            relay_json.write_text(json.dumps({"connections": connections}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", "-c", self.code, str(relay_json), transport, exec_status],
                text=True, capture_output=True, check=False,
            )
        return result.returncode, json.loads(result.stdout)

    HTTP_FAULT = {"connection_id": 5, "intervention": "stream_fault:incomplete-terminal:http", "production_forwarded": False}
    WS_FAULT = {
        "connection_id": 4, "intervention": "stream_fault:incomplete-terminal:ws", "production_forwarded": False,
        "stream_fault_ws_actions": ["warmup_completed", "stream_fault_incomplete-terminal"],
    }
    OTHERS = [{"connection_id": 1, "request_line": "GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1"},
              {"connection_id": 2, "request_line": "GET /backend-api/wham/accounts/check HTTP/1.1"}]

    def test_passes_when_the_fault_is_injected_once_and_nothing_follows(self) -> None:
        for transport, fault in (("http", self.HTTP_FAULT), ("ws", self.WS_FAULT)):
            with self.subTest(transport=transport):
                code, report = self.run_check([*self.OTHERS, fault], transport)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["status"], "passed")
                self.assertEqual(report["failures"], [])
                self.assertEqual(report["fault_connection_ids"], [fault["connection_id"]])
                self.assertEqual(report["post_terminal_requests"], [])
                self.assertEqual(report["exec_exit_status"], 1)

    def test_exec_exit_status_is_recorded_but_never_judged(self) -> None:
        for status, expected in (("0", 0), ("1", 1), ("", None), ("abc", None)):
            with self.subTest(status=status):
                code, report = self.run_check([self.HTTP_FAULT], "http", status)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["exec_exit_status"], expected)

    def test_retry_or_downgrade_after_the_fault_fails_closed(self) -> None:
        for request in ("ws_upgrade", "http_post", "ws_generate"):
            with self.subTest(request=request):
                retry = {"connection_id": 9, "request_line": "POST /backend-api/codex/responses HTTP/1.1",
                         "post_terminal_request": request}
                code, report = self.run_check([*self.OTHERS, self.WS_FAULT, retry], "ws")
                self.assertEqual(code, 1, report)
                self.assertEqual(report["failures"], ["responses-request-after-terminal-fault"])
                self.assertEqual(report["post_terminal_requests"], [{"connection_id": 9, "request": request}])
        # 同连接续发：标记落在合成故障连接自己的元数据上。
        continued = {**self.WS_FAULT, "post_terminal_request": "ws_generate",
                     "stream_fault_ws_actions": ["warmup_completed", "stream_fault_incomplete-terminal", "continuation_completed"]}
        code, report = self.run_check([continued], "ws")
        self.assertEqual((code, report["failures"]), (1, ["responses-request-after-terminal-fault"]))

    def test_fault_must_be_injected_exactly_once_with_the_matching_transport(self) -> None:
        for connections, transport in (
            ([*self.OTHERS], "http"),
            ([self.HTTP_FAULT, {**self.HTTP_FAULT, "connection_id": 6}], "http"),
            ([self.HTTP_FAULT], "ws"),
            ([self.WS_FAULT], "http"),
            ([{**self.HTTP_FAULT, "intervention": "stream_fault:interrupted:http"}], "http"),
        ):
            with self.subTest(transport=transport, connections=connections):
                code, report = self.run_check(connections, transport)
                self.assertEqual(code, 1, report)
                self.assertEqual(report["failures"], ["fault-not-injected-exactly-once"])

    def test_ws_fault_connection_must_carry_exactly_one_terminal_action(self) -> None:
        for actions in ([], ["warmup_completed"], ["stream_fault_incomplete-terminal", "stream_fault_incomplete-terminal"]):
            with self.subTest(actions=actions):
                code, report = self.run_check([{**self.WS_FAULT, "stream_fault_ws_actions": actions}], "ws")
                self.assertEqual(code, 1, report)
                self.assertEqual(report["failures"], ["ws-fault-action-not-exactly-once"])


class EmbeddedCheckCase(unittest.TestCase):
    """从采集脚本里取出某个 heredoc 核对块的 Python，喂假 relay.json 原样执行。"""

    MARKER = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.code = script_check_code(cls.MARKER)

    def run_check(self, connections: list[dict], *arguments: str) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            relay_json = Path(tmp) / "relay.json"
            relay_json.write_text(json.dumps({"connections": connections}), encoding="utf-8")
            return run_script_check(self.code, relay_json, *arguments)


class WsUpgrade429CheckTest(EmbeddedCheckCase):
    """「升级 429 降级等待核对」：硬判据只有结构性的两条，等待时长与间隔只记事实。"""

    MARKER = 'ws_retry_after_report=$(docker exec -i "$capture_container" python3 - \\\n'

    @staticmethod
    def upgrade(connection_id: int, sequence: int, head_ms: int, sent_ms: int) -> dict:
        return {"connection_id": connection_id, "intervention": "ws_upgrade_429", "ws_upgrade_429": {
            "sequence": sequence, "retry_after_seconds": 2,
            "request_head_received_unix_ms": head_ms, "response_sent_unix_ms": sent_ms,
        }}

    @staticmethod
    def fallback(connection_id: int, head_ms: int, last_sent_ms: int) -> dict:
        return {"connection_id": connection_id, "request_line": "POST /backend-api/codex/responses HTTP/1.1",
                "ws_upgrade_429_fallback_post": {
                    "ws_upgrade_429_count": 3, "retry_after_seconds": 2,
                    "last_ws_upgrade_429_sent_unix_ms": last_sent_ms, "request_head_received_unix_ms": head_ms,
                    "since_last_ws_upgrade_429_ms": head_ms - last_sent_ms,
                }}

    def test_passes_and_records_the_timeline_when_the_client_waited(self) -> None:
        connections = [
            {"connection_id": 1, "request_line": "GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1"},
            self.upgrade(2, 1, 1_000, 1_001), self.upgrade(3, 2, 3_010, 3_011), self.upgrade(4, 3, 5_020, 5_021),
            self.fallback(5, 7_040, 5_021),
        ]
        code, report = self.run_check(connections, "2", "0")
        self.assertEqual(code, 0, report)
        self.assertEqual((report["status"], report["failures"]), ("passed", []))
        self.assertEqual(report["upgrade_attempt_count"], 3)
        self.assertEqual(report["gaps_between_upgrade_attempts_ms"], [2_009, 2_009])
        self.assertEqual(report["http_fallback_post_count"], 1)
        self.assertEqual(report["fallback_since_last_upgrade_429_ms"], 2_019)
        self.assertIs(report["fallback_waited_retry_after"], True)
        self.assertEqual((report["retry_after_seconds"], report["exec_exit_status"]), (2, 0))

    def test_a_client_that_did_not_wait_is_recorded_not_rejected(self) -> None:
        """降级前没等到截止时间也是证据（0.160 就是这样）：采集阶段不丢，由 VC-2 对着样本判。"""

        connections = [self.upgrade(2, 1, 1_000, 1_001), self.fallback(3, 1_300, 1_001)]
        code, report = self.run_check(connections, "2", "")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "passed")
        self.assertIs(report["fallback_waited_retry_after"], False)
        self.assertEqual(report["fallback_since_last_upgrade_429_ms"], 299)
        self.assertIsNone(report["exec_exit_status"])

    def test_requires_an_injected_429_and_a_following_fallback_post(self) -> None:
        for connections, failures in (
            ([], ["no-ws-upgrade-429-injected", "no-http-fallback-post-after-429"]),
            ([self.fallback(3, 5_000, 1_000)], ["no-ws-upgrade-429-injected"]),
            ([self.upgrade(2, 1, 1_000, 1_001)], ["no-http-fallback-post-after-429"]),
        ):
            with self.subTest(failures=failures):
                code, report = self.run_check(connections, "2", "1")
                self.assertEqual(code, 1, report)
                self.assertEqual((report["status"], report["failures"]), ("failed", failures))


class OverloadedTimelineCheckTest(EmbeddedCheckCase):
    """「过载 503 重试时间线核对」：硬判据是回过受控 503 且同连接没有非目标请求，请求数与间隔只记事实。"""

    MARKER = 'overloaded_report=$(docker exec -i "$capture_container" python3 - \\\n'

    @staticmethod
    def served(attempt: int, head_ms: int, sent_ms: int, *, retry_after: int | None = 2) -> dict:
        return {"attempt": attempt, "request_line": "POST /backend-api/codex/responses HTTP/1.1",
                "retry_after_seconds": retry_after,
                "request_head_received_unix_ms": head_ms, "response_sent_unix_ms": sent_ms}

    def test_records_request_count_and_gaps_across_connections(self) -> None:
        # 6 轮 × 5 个请求：同一轮内连接复用，轮间换新连接——按到达时刻排序后统计，与连接划分无关。
        first = [self.served(index + 1, 2_050 * index, 2_050 * index + 1) for index in range(3)]
        second = [self.served(index + 4, 6_150 + 2_050 * index, 6_151 + 2_050 * index) for index in range(2)]
        connections = [
            {"connection_id": 4, "overloaded_503_attempts": second, "overloaded_503_end": "client_closed"},
            {"connection_id": 2, "overloaded_503_attempts": first, "overloaded_503_end": "client_closed"},
        ]
        code, report = self.run_check(connections, "2", "1")
        self.assertEqual(code, 0, report)
        self.assertEqual((report["status"], report["failures"]), ("passed", []))
        self.assertEqual(report["request_count"], 5)
        self.assertEqual(report["connection_ids"], [2, 4])
        self.assertEqual(report["gaps_ms"], [2_049, 2_049, 2_049, 2_049])
        self.assertEqual(report["min_gap_ms"], 2_049)
        self.assertIs(report["all_gaps_at_least_retry_after"], True)
        self.assertEqual((report["retry_after_seconds"], report["exec_exit_status"]), (2, 1))

    def test_short_gaps_are_recorded_not_rejected(self) -> None:
        attempts = [self.served(1, 0, 1), self.served(2, 700, 701), self.served(3, 2_900, 2_901)]
        code, report = self.run_check(
            [{"connection_id": 2, "overloaded_503_attempts": attempts}], "2", "1"
        )
        self.assertEqual(code, 0, report)
        self.assertEqual(report["gaps_ms"], [699, 2_199])
        self.assertIs(report["all_gaps_at_least_retry_after"], False)

    def test_control_without_retry_after_has_no_gap_verdict(self) -> None:
        heads = [0, 220, 650, 1_500, 3_200]  # 指数退避的量级：200 ms 起每次约翻倍
        attempts = [self.served(index + 1, head, head + 1, retry_after=None) for index, head in enumerate(heads)]
        code, report = self.run_check([{"connection_id": 2, "overloaded_503_attempts": attempts}], "", "1")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["request_count"], 5)
        self.assertIsNone(report["retry_after_seconds"])
        self.assertIsNone(report["all_gaps_at_least_retry_after"])
        self.assertEqual(len(report["gaps_ms"]), 4)

    def test_requires_a_served_503_and_no_unexpected_request(self) -> None:
        code, report = self.run_check([{"connection_id": 1}], "2", "0")
        self.assertEqual((code, report["failures"]), (1, ["no-overloaded-503-served"]))
        self.assertEqual(report["request_count"], 0)
        self.assertIsNone(report["min_gap_ms"])
        connections = [{"connection_id": 2, "overloaded_503_attempts": [self.served(1, 0, 1)],
                        "overloaded_503_end": "unexpected_request"}]
        code, report = self.run_check(connections, "2", "0")
        self.assertEqual((code, report["failures"]), (1, ["unexpected-request-on-overloaded-connection"]))
        self.assertEqual(report["unexpected_request_connection_ids"], [2])


class ScriptCheckBlocksShellGlueTest(unittest.TestCase):
    """三个核对块（终止型流内故障、升级 429 降级等待、过载 503 时间线）在 bash 里的胶水：命令替换取报告、
    ``|| status=$?``、空输出判断、write_observation、失败时的提示与 ``exit 1``。

    Python 判据已由各自的测试锁定；这里把块原样抽出来，用一个只转发 ``docker exec -i <容器> python3 - …`` 的垫片在宿主上
    执行，补上「脚本这一层有没有写错」的核对——这些块第一次真实运行是在采集容器里，出错就是一轮白采（Campaign 建立后
    采集脚本冻结，发现晚了只能走新 Campaign）。
    """

    # 块名 → (起始文本, 下一段的起始文本, 观测文件名)
    BLOCKS = {
        "terminal": ('if [[ ${RELAY_STREAM_FAULT:-} == incomplete-terminal ]]; then\n  echo "=== 终止型流内故障核对 ==="',
                     "\n# 升级 429 降级等待核对", "stream-fault-terminal.json"),
        "ws": ('if [[ -n ${RELAY_WS_UPGRADE_429_RETRY_AFTER:-} ]]; then\n  echo "=== 升级 429 降级等待核对 ==="',
               "\n# 过载 503 重试时间线核对", "ws-retry-after-timeline.json"),
        "overloaded": ('if [[ ${RELAY_RETRY_PROBE:-} == persistent-503-overloaded ]]; then\n  echo "=== 过载 503 重试时间线核对 ==="',
                       "\n# tcpdump 此前只在 EXIT 陷阱里停", "server-overloaded-timeline.json"),
    }
    # 假 docker：只认 ``docker exec -i <容器> python3 - <参数…>``（标准输入是块里的 heredoc），把参数里的容器内路径换成宿主临时目录。
    DRIVER = r'''
set -Eeuo pipefail
capture_container=fake-capture
run_id=glue-run
last_exec_status=${LAST_EXEC_STATUS-}
write_observation() { printf '%s' "$2" > "$OBS_DIR/$1"; }
docker() {
  [[ $1 == exec && $2 == -i && $3 == "$capture_container" && $4 == python3 && $5 == - ]] || { echo "非预期的 docker 调用: $*" >&2; return 99; }
  shift 5
  local args=() arg
  for arg in "$@"; do args+=("${arg/#\/capture\/runs\/$run_id\//$RUN_ROOT/}"); done
  python3 - "${args[@]}"
}
'''

    @classmethod
    def setUpClass(cls) -> None:
        source = RELAY_SCRIPT.read_text(encoding="utf-8")
        cls.code = {}
        for name, (start_marker, end_marker, _observation) in cls.BLOCKS.items():
            start = source.index(start_marker)
            cls.code[name] = source[start:source.index(end_marker, start)]

    def run_block(self, name: str, connections: list[dict] | None, **environment: str):
        with tempfile.TemporaryDirectory() as tmp:
            root, observations = Path(tmp) / "run", Path(tmp) / "observations"
            (root / "relay").mkdir(parents=True)
            observations.mkdir()
            if connections is not None:
                (root / "relay" / "relay.json").write_text(json.dumps({"connections": connections}), encoding="utf-8")
            script = self.DRIVER + self.code[name] + '\necho "块之后的语句已执行"\n'
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1",
                   "OBS_DIR": str(observations), "RUN_ROOT": str(root), **environment}
            result = subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True, check=False)
            observation_path = observations / self.BLOCKS[name][2]
            observation = json.loads(observation_path.read_text(encoding="utf-8")) if observation_path.exists() else None
        return result, observation

    TERMINAL_ENV = {"RELAY_STREAM_FAULT": "incomplete-terminal", "RELAY_STREAM_FAULT_TRANSPORT": "http", "LAST_EXEC_STATUS": "1"}
    WS_ENV = {"RELAY_WS_UPGRADE_429_RETRY_AFTER": "2", "LAST_EXEC_STATUS": "0"}
    OVERLOADED_ENV = {"RELAY_RETRY_PROBE": "persistent-503-overloaded", "RELAY_RETRY_PROBE_RETRY_AFTER": "2", "LAST_EXEC_STATUS": "1"}
    HTTP_FAULT = {"connection_id": 5, "intervention": "stream_fault:incomplete-terminal:http"}
    UPGRADE = {"connection_id": 2, "ws_upgrade_429": {
        "sequence": 1, "retry_after_seconds": 2, "request_head_received_unix_ms": 1_000, "response_sent_unix_ms": 1_001}}
    FALLBACK = {"connection_id": 3, "ws_upgrade_429_fallback_post": {
        "ws_upgrade_429_count": 1, "retry_after_seconds": 2, "last_ws_upgrade_429_sent_unix_ms": 1_001,
        "request_head_received_unix_ms": 3_100, "since_last_ws_upgrade_429_ms": 2_099}}
    SERVED = {"connection_id": 2, "overloaded_503_attempts": [
        {"attempt": 1, "request_line": "POST /backend-api/codex/responses HTTP/1.1", "retry_after_seconds": 2,
         "request_head_received_unix_ms": 0, "response_sent_unix_ms": 1}]}

    def test_passing_check_writes_the_observation_and_lets_the_script_continue(self) -> None:
        for name, environment, connections in (
            ("terminal", self.TERMINAL_ENV, [self.HTTP_FAULT]),
            ("ws", self.WS_ENV, [self.UPGRADE, self.FALLBACK]),
            ("overloaded", self.OVERLOADED_ENV, [self.SERVED]),
        ):
            with self.subTest(name=name):
                result, observation = self.run_block(name, connections, **environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("块之后的语句已执行", result.stdout)
                self.assertEqual(observation["status"], "passed")
                self.assertEqual(observation["failures"], [])

    def test_failing_check_still_writes_the_observation_then_stops_the_script(self) -> None:
        for name, environment, connections, message in (
            ("terminal", self.TERMINAL_ENV, [self.HTTP_FAULT, {"connection_id": 9, "post_terminal_request": "http_post"}],
             "终止型流内故障核对不成立"),
            ("ws", self.WS_ENV, [self.UPGRADE], "升级 429 降级等待核对不成立"),
            ("overloaded", self.OVERLOADED_ENV, [{"connection_id": 1}], "过载 503 重试时间线核对不成立"),
        ):
            with self.subTest(name=name):
                result, observation = self.run_block(name, connections, **environment)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("块之后的语句已执行", result.stdout)
                self.assertIn(message, result.stderr)
                self.assertEqual(observation["status"], "failed", "失败也要留下观测文件，便于事后看原因")

    def test_a_check_that_prints_nothing_stops_the_script_without_an_observation(self) -> None:
        for name, environment, message in (
            ("terminal", self.TERMINAL_ENV, "终止型流内故障核对没有输出"),
            ("ws", self.WS_ENV, "升级 429 降级等待核对没有输出"),
            ("overloaded", self.OVERLOADED_ENV, "过载 503 重试时间线核对没有输出"),
        ):
            with self.subTest(name=name):
                result, observation = self.run_block(name, None, **environment)  # relay.json 不存在：核对块自己崩溃
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertIsNone(observation)

    def test_blocks_are_skipped_when_their_switch_is_not_set(self) -> None:
        for name in self.BLOCKS:
            with self.subTest(name=name):
                result, observation = self.run_block(name, [], LAST_EXEC_STATUS="0")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("块之后的语句已执行", result.stdout)
                self.assertNotIn("===", result.stdout)
                self.assertIsNone(observation)


class ScriptAssembledRelayArgumentsTest(unittest.TestCase):
    """采集脚本按作业环境拼出的中继参数，必须能被中继自己的参数解析接受。

    脚本里的开关名、取值与中继里的 argparse 各写各的，最容易对不上；这里取 0.163.0 清单里每个走中继脚本的官方作业，用它的环境
    把脚本里「拼中继参数」那一段原样跑一遍，再把拼出的参数交给真实的中继解析（证书文件故意不存在：参数通过之后中继会在加载
    证书时以 FileNotFoundError 退出，参数不合法则是 argparse 的退出码 2）。
    """

    SUBSTITUTIONS = {
        "{capture_container}": "capture-cli", "{capture_root}": "/root/oauth-capture", "{target_version}": "0.163.0",
        "{relay_codex_bin}": "/opt/codex/bin/codex", "{model}": "gpt-5.5", "{lite_model}": "gpt-6-astra",
        "{campaign_id}": "c01630-unit",
    }
    EXPECTED = {
        "official-relay-ws-retry-after": ["--ws-upgrade-429-retry-after", "2"],
        "official-relay-server-overloaded-retry-after": [
            "--force-ws-fallback-426", "--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses",
            "--retry-probe-retry-after", "2",
        ],
        "official-relay-server-overloaded": [
            "--force-ws-fallback-426", "--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses",
        ],
        "official-relay-conn-retry-after": [
            "--force-ws-fallback-426", "--retry-probe", "keepalive-500", "--retry-probe-target", "responses",
            "--retry-probe-retry-after", "2",
        ],
        "official-relay-stream-incomplete-http": ["--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http"],
        "official-relay-stream-incomplete-ws": ["--stream-fault", "incomplete-terminal", "--stream-fault-transport", "ws"],
        "official-relay-cyber-access-ws": [],
    }

    @classmethod
    def setUpClass(cls) -> None:
        source = RELAY_SCRIPT.read_text(encoding="utf-8")
        start = source.index("relay_intervention_args=()\n")
        cls.assembly = source[start:source.index("if [[ $capture_client_hello == 1 ]]; then", start)]
        manifest = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_163_0.json").read_text(encoding="utf-8"))
        cls.environments = {}
        for job in manifest["capture_jobs"]:
            step = job["steps"][0]
            if job["phase"] != "official" or not step["argv"][-1].endswith("run_official_relay_scenario.sh"):
                continue
            environment = {}
            for key, value in step["environment"].items():
                for placeholder, concrete in cls.SUBSTITUTIONS.items():
                    value = value.replace(placeholder, concrete)
                environment[key] = value
            cls.environments[job["id"]] = environment

    def assembled(self, environment: dict[str, str]) -> list[str]:
        script = (
            "set -Eeuo pipefail\n"
            f"scenario={environment.get('SCENARIO', '')}\n"
            f"require_model_receipt={environment.get('REQUIRE_MODEL_CONDITION_RECEIPT', '0')}\n"
            f"a14_c2pa_expectation={environment.get('A14_C2PA_EXPECTATION', '')}\n"
            + self.assembly
            + 'if (( ${#relay_intervention_args[@]} )); then printf "%s\\n" "${relay_intervention_args[@]}"; fi\n'
        )
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environment}
        result = subprocess.run([BASH, "-c", script], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def relay_parse(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            # 与脚本启动中继的命令行同形（证书路径换成不存在的文件）。
            return subprocess.run(
                [sys.executable, "-B", str(RELAY_PY),
                 "--cert", f"{tmp}/missing.crt", "--key", f"{tmp}/missing.key", "--mode", "direct", "--port", "443",
                 "--upstream-host", "chatgpt.com", "--upstream-ip", "127.0.0.1", "--upstream-map", "chatgpt.com=127.0.0.1",
                 "--codex-version", "0.163.0", "--output", f"{tmp}/out", "--timeout", "1800", *arguments],
                text=True, capture_output=True, check=False,
            )

    def test_every_official_relay_job_assembles_arguments_the_relay_accepts(self) -> None:
        self.assertGreaterEqual(len(self.environments), 40)
        for job_id, environment in sorted(self.environments.items()):
            with self.subTest(job_id=job_id):
                arguments = self.assembled(environment)
                result = self.relay_parse(arguments)
                self.assertNotEqual(result.returncode, 2, f"{arguments}\n{result.stderr}")
                self.assertNotIn("upstream_byte_relay.py: error:", result.stderr)
                self.assertIn("FileNotFoundError", result.stderr, "参数通过解析后应停在加载证书，而不是别处")

    def test_new_directed_jobs_assemble_exactly_their_switches(self) -> None:
        for job_id, expected in self.EXPECTED.items():
            with self.subTest(job_id=job_id):
                self.assertEqual(self.assembled(self.environments[job_id]), expected)

    def test_relay_still_rejects_the_script_level_misuse_the_script_does_not_catch(self) -> None:
        """脚本只在开头校验取值与搭配；开关之间的互斥由中继参数解析兜底（两层都在）。"""

        for arguments in (
            ["--ws-upgrade-429-retry-after", "2", "--force-ws-fallback-426"],
            ["--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses", "--ws-upgrade-429-retry-after", "2"],
        ):
            with self.subTest(arguments=arguments):
                result = self.relay_parse(arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("彼此互斥", result.stderr)


class Scenario0159ManifestParameterTest(unittest.TestCase):
    """0.160.0 清单（由 0.159.2 版改名）：沿用 0.157.0 的全部作业执行参数，新增 9 个官方定向样本作业。

    清单后缀、目标版本与单元用 Campaign 前缀写成类属性，0.163.0 由下面的子类复用同一组断言。
    """

    MANIFEST_SUFFIX = "0_160_0"
    TARGET_VERSION = "0.160.0"
    UNIT_CAMPAIGN_ID = "c01600-unit"
    # 相对 0.157.0 清单新增的官方定向作业，以及其中含中继合成应答（标签必须标 relay_synthetic）的子集。
    DIRECTED_JOBS = NEW_OFFICIAL_JOBS
    SYNTHETIC = SYNTHETIC_JOBS
    # 主线模型占位（只用于让脚本校验走到 CAPTURE_HOST_DATA_ROOT 这一关，不影响断言）。
    MAIN_MODEL = "gpt-5.5"
    # 与旧清单相比允许不同的步骤环境变量镜像：0.163.0 起主线是 Lite 模型，EXPECT_USE_RESPONSES_LITE 改值、
    # COMPACTION_FIRST_USE_RESPONSES_LITE 新增，其余执行参数不变。取值本身由 test_main_track_models 按声明
    # 逐版本锁定，这里比较旧清单时两侧都去掉这些键，免得把声明驱动的变化当成执行参数漂移。
    DECLARED_MIRROR_KEYS: tuple[str, ...] = ()

    def comparable_steps(self, steps: list) -> list:
        """去掉声明驱动的环境变量镜像之后的步骤，供与旧清单逐字比较。"""

        result = copy.deepcopy(steps)
        for step in result:
            for key in self.DECLARED_MIRROR_KEYS:
                step["environment"].pop(key, None)
        return result

    def assert_carried_fields_equal(self, current: dict, previous: dict) -> None:
        for field in ("phase", "suites", "scenario_ids", "steps", "evidence_roots", "covers"):
            if field == "steps":
                self.assertEqual(
                    self.comparable_steps(current[field]), self.comparable_steps(previous[field]), field
                )
            else:
                self.assertEqual(current[field], previous[field], field)

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (TOOL_ROOT / f"codex_upgrade_scenarios_{cls.MANIFEST_SUFFIX}.json").read_text(encoding="utf-8")
        )
        previous = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_157_0.json").read_text(encoding="utf-8"))
        cls.previous_jobs = {job["id"]: job for job in previous["capture_jobs"]}
        cls.jobs = {job["id"]: job for job in cls.manifest["capture_jobs"]}
        labels = json.loads(
            (TOOL_ROOT / f"codex_upgrade_evidence_labels_{cls.MANIFEST_SUFFIX}.json").read_text(encoding="utf-8")
        )
        cls.labels = {entry["job_id"]: entry for entry in labels["entries"]}

    def test_carried_jobs_keep_0157_execution_parameters(self) -> None:
        self.assertEqual(set(self.jobs) - set(self.previous_jobs), self.DIRECTED_JOBS)
        self.assertEqual(set(self.previous_jobs) - set(self.jobs), set())
        for job_id, previous in self.previous_jobs.items():
            with self.subTest(job_id=job_id):
                self.assert_carried_fields_equal(self.jobs[job_id], previous)

    def test_new_job_environments_pass_script_validation(self) -> None:
        substitutions = {
            "{capture_container}": "capture-cli",
            "{capture_root}": "/root/oauth-capture",
            "{target_version}": self.TARGET_VERSION,
            "{relay_codex_bin}": "/opt/codex/bin/codex",
            "{model}": self.MAIN_MODEL,
            "{lite_model}": "gpt-6-astra",
            "{campaign_id}": self.UNIT_CAMPAIGN_ID,
        }
        for job_id in sorted(self.DIRECTED_JOBS):
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
        synthetic = self.SYNTHETIC
        for job_id in sorted(self.DIRECTED_JOBS):
            with self.subTest(job_id=job_id):
                entry = self.labels[job_id]
                self.assertEqual(entry["side"], "official")
                for rule in entry["rules"]:
                    self.assertLessEqual(set(rule["scenario_ids"]), set(self.jobs[job_id]["scenario_ids"]))
                    self.assertEqual(rule["glob"], "relay/*.client_to_upstream.bin")
                    self.assertEqual(rule["labels"].get("upstream_response") == "relay_synthetic", job_id in synthetic)
                    self.assertNotEqual(rule["labels"].get("variant"), "http_default", "不得冒充 http_default 样本")


class Scenario0163ManifestParameterTest(Scenario0159ManifestParameterTest):
    """0.163.0 清单（由 0.160.0 版复制）：沿用 0.157.0 的全部作业执行参数，保留 9 个官方定向样本，另在建 Campaign 之前新增三个。"""

    MANIFEST_SUFFIX = "0_163_0"
    TARGET_VERSION = "0.163.0"
    UNIT_CAMPAIGN_ID = "c01630-unit"
    DIRECTED_JOBS = NEW_OFFICIAL_JOBS | NEW_0163_JOBS
    SYNTHETIC = SYNTHETIC_JOBS | SYNTHETIC_0163_JOBS
    # 0.163.0 起的主线模型（Lite，gpt-5.5 已下线）。
    MAIN_MODEL = "gpt-6.1-sol"
    DECLARED_MIRROR_KEYS = ("EXPECT_USE_RESPONSES_LITE", "COMPACTION_FIRST_USE_RESPONSES_LITE")

    def environment(self, job_id: str) -> dict:
        return self.jobs[job_id]["steps"][0]["environment"]

    def test_job_set_is_the_0160_set_plus_the_new_directed_jobs(self) -> None:
        """0.163.0 清单 = 0.160.0 清单 + 新增的官方定向作业；候选侧不新增 capture 作业，既有作业的执行参数不变。"""

        base = json.loads((TOOL_ROOT / "codex_upgrade_scenarios_0_160_0.json").read_text(encoding="utf-8"))
        base_jobs = {job["id"]: job for job in base["capture_jobs"]}
        self.assertEqual(set(self.jobs) - set(base_jobs), NEW_0163_JOBS)
        self.assertEqual(set(base_jobs) - set(self.jobs), set())
        for job_id, previous in base_jobs.items():
            with self.subTest(job_id=job_id):
                # 描述里的版本号表述已随 0.163.0 替换，执行参数（以及绑定的场景与规则）才是不变量；
                # 唯一允许的差异是声明驱动的 Lite 期望镜像（见 DECLARED_MIRROR_KEYS）。
                self.assert_carried_fields_equal(self.jobs[job_id], previous)
        # 镜像键只允许出现在已知的几个作业里，且取值已按 0.163.0 的主线 Lite 声明改成 true。
        mirrored = {
            job_id
            for job_id, job in self.jobs.items()
            if any(key in step["environment"] for step in job["steps"] for key in self.DECLARED_MIRROR_KEYS)
        }
        self.assertEqual(
            mirrored,
            {
                "official-relay-http-response",
                "official-relay-http-response-plain",
                "official-relay-legacy-compact-default",
                "official-lite-http-response",
                "official-lite-legacy-compact-default",
                "official-lite-ws-turnstate",
                "official-relay-comp-hash-changed",
                "official-relay-model-downshift",
            },
        )
        for job_id in mirrored:
            for key in self.DECLARED_MIRROR_KEYS:
                if key in self.environment(job_id):
                    self.assertEqual(self.environment(job_id)[key], "true", f"{job_id} {key}")
        phases = lambda jobs: [job["phase"] for job in jobs.values()]  # noqa: E731 - 局部小工具
        self.assertEqual(phases(self.jobs).count("candidate"), phases(base_jobs).count("candidate"))
        self.assertEqual(phases(self.jobs).count("official"), phases(base_jobs).count("official") + len(NEW_0163_JOBS))
        # 新作业按序插在官方作业段末尾（候选作业之前），与 0.159.2 的先例一致。
        order = [job["id"] for job in self.manifest["capture_jobs"]]
        last_official = max(index for index, job in enumerate(self.manifest["capture_jobs"]) if job["phase"] == "official")
        self.assertTrue(all(order.index(job_id) <= last_official for job_id in NEW_0163_JOBS))

    def test_0163_directed_jobs_bind_scenarios_models_and_request_assertions(self) -> None:
        expected = {
            "official-relay-cyber-access-ws": (["A05"], "cyber-access-ws", ("GET", "/backend-api/codex/responses"),
                                               ["SPEC-BODY-001", "SPEC-WS-005"]),
            "official-relay-stream-incomplete-http": (["A07"], "stream-fault", ("POST", "/backend-api/codex/responses"),
                                                      ["SPEC-PROTO-002"]),
            "official-relay-stream-incomplete-ws": (["A07"], "stream-fault", ("GET", "/backend-api/codex/responses"),
                                                    ["SPEC-PROTO-002"]),
            "official-relay-ws-retry-after": (["A07"], "ws-retry-after", ("POST", "/backend-api/codex/responses"),
                                              ["SPEC-PROTO-002"]),
            "official-relay-server-overloaded-retry-after": (
                ["A07", "A08"], "conn-retry", ("POST", "/backend-api/codex/responses"),
                ["SPEC-CONN-001", "SPEC-PROTO-002"]),
            "official-relay-server-overloaded": (
                ["A07", "A08"], "conn-retry", ("POST", "/backend-api/codex/responses"),
                ["SPEC-CONN-001", "SPEC-PROTO-002"]),
        }
        scenarios = {item["scenario_id"]: set(item["covers"]) for item in self.manifest["evidence_scenarios"]}
        rules = set(json.loads((TOOL_ROOT / "codex_upgrade_rules_0_163_0.json").read_text(encoding="utf-8"))["required_rules"])
        for job_id, (scenario_ids, scenario, request, covers) in expected.items():
            with self.subTest(job_id=job_id):
                job = self.jobs[job_id]
                environment = self.environment(job_id)
                self.assertEqual(job["scenario_ids"], scenario_ids)
                self.assertEqual(environment["SCENARIO"], scenario)
                self.assertEqual((environment["REQUIRE_REQUEST_METHOD"], environment["REQUIRE_REQUEST_PATH"]), request)
                self.assertEqual(job["covers"], covers)
                # covers 属于批准的 43 条，且被其绑定的证据场景证明（plan 的升级审计按同一口径拒绝）。
                self.assertLessEqual(set(covers), rules)
                self.assertLessEqual(set(covers), set().union(*(scenarios[item] for item in scenario_ids)))
                self.assertEqual(job["phase"], "official")
                self.assertEqual(job["suites"], ["full"])
                self.assertEqual(job["required_scenario_receipts"], [])

    def test_cyber_access_job_mirrors_the_numeric_ws_lite_sample(self) -> None:
        """与 official-relay-reasoning-numeric-ws 同构：Lite 轨、内置 provider 默认 WS、不取模型收据，只差场景。"""

        cyber = self.jobs["official-relay-cyber-access-ws"]
        numeric = self.jobs["official-relay-reasoning-numeric-ws"]
        self.assertEqual(self.environment("official-relay-cyber-access-ws")["MODEL"], "{lite_model}")
        for field in ("track", "model_id", "expected_use_responses_lite", "required_model_receipt", "scenario_ids", "suites"):
            self.assertEqual(cyber[field], numeric[field], field)
        self.assertEqual(
            (cyber["track"], cyber["model_id"], cyber["expected_use_responses_lite"], cyber["required_model_receipt"]),
            ("lite", "{lite_model}", True, False),
        )
        differing = {
            key for key in set(self.environment("official-relay-cyber-access-ws")) | set(self.environment("official-relay-reasoning-numeric-ws"))
            if self.environment("official-relay-cyber-access-ws").get(key) != self.environment("official-relay-reasoning-numeric-ws").get(key)
        }
        self.assertEqual(differing, {"RUN_ID", "SCENARIO"})
        # 不带任何中继受控开关：请求字节全部转发生产，条件字段由客户端按显式程序自己写。
        self.assertFalse([key for key in self.environment("official-relay-cyber-access-ws") if key.startswith("RELAY_")])

    def test_incomplete_terminal_jobs_pair_with_the_interrupted_samples(self) -> None:
        """终止型故障作业与同传输的 interrupted 作业只差故障种类（与 RUN_ID）：行为差异只来自 reason。"""

        for transport in ("http", "ws"):
            with self.subTest(transport=transport):
                incomplete = self.environment(f"official-relay-stream-incomplete-{transport}")
                interrupted = self.environment(f"official-relay-stream-interrupted-{transport}")
                self.assertEqual(incomplete["RELAY_STREAM_FAULT"], "incomplete-terminal")
                self.assertEqual(incomplete["RELAY_STREAM_FAULT_TRANSPORT"], transport)
                self.assertEqual(
                    {key: value for key, value in incomplete.items() if key not in {"RUN_ID", "RELAY_STREAM_FAULT"}},
                    {key: value for key, value in interrupted.items() if key not in {"RUN_ID", "RELAY_STREAM_FAULT"}},
                )
                self.assertEqual(
                    self.jobs[f"official-relay-stream-incomplete-{transport}"]["covers"],
                    self.jobs[f"official-relay-stream-interrupted-{transport}"]["covers"],
                )

    def test_ws_retry_after_job_sets_only_the_429_switch_on_the_builtin_ws_provider(self) -> None:
        """不与 426 强制降级等其它干预同用（那会让客户端绕过重试等待直接降级），也不换 provider：差别全在中继的 429。"""

        job = self.jobs["official-relay-ws-retry-after"]
        environment = self.environment("official-relay-ws-retry-after")
        self.assertEqual(environment["RELAY_WS_UPGRADE_429_RETRY_AFTER"], "2")
        self.assertEqual([key for key in environment if key.startswith("RELAY_")], ["RELAY_WS_UPGRADE_429_RETRY_AFTER"])
        self.assertEqual(environment["MODEL"], "{model}")
        self.assertNotIn("track", job, "主线作业，不带 Lite 字段")
        self.assertEqual(job["suites"], ["full"])
        self.assertEqual(job["required_scenario_receipts"], [])

    def test_server_overloaded_pair_is_a_conn_retry_chain_that_differs_only_by_retry_after(self) -> None:
        with_retry_after = self.environment("official-relay-server-overloaded-retry-after")
        control = self.environment("official-relay-server-overloaded")
        self.assertEqual(with_retry_after["RELAY_RETRY_PROBE_RETRY_AFTER"], "2")
        self.assertNotIn("RELAY_RETRY_PROBE_RETRY_AFTER", control)
        self.assertEqual(
            {key: value for key, value in with_retry_after.items() if key not in {"RUN_ID", "RELAY_RETRY_PROBE_RETRY_AFTER"}},
            {key: value for key, value in control.items() if key != "RUN_ID"},
            "有值对照与无值样本只差 Retry-After 一个条件",
        )
        # 与 conn-retry 同族：同一场景、426 强制降级、responses 目标；差别只有探针取值与目标请求断言。
        conn_retry = self.environment("official-relay-conn-retry")
        self.assertEqual(
            {key for key in set(control) | set(conn_retry) if control.get(key) != conn_retry.get(key)},
            {"RUN_ID", "RELAY_RETRY_PROBE", "REQUIRE_REQUEST_METHOD", "REQUIRE_REQUEST_PATH"},
        )
        self.assertEqual(control["RELAY_RETRY_PROBE"], "persistent-503-overloaded")
        for job_id in ("official-relay-server-overloaded-retry-after", "official-relay-server-overloaded"):
            with self.subTest(job_id=job_id):
                self.assertEqual(self.jobs[job_id]["scenario_ids"], self.jobs["official-relay-conn-retry-after"]["scenario_ids"])
                self.assertEqual(set(self.jobs[job_id]["covers"]), set(self.jobs["official-relay-conn-retry-after"]["covers"]))

    def test_new_failure_path_descriptions_state_the_two_retry_layers(self) -> None:
        """描述要把「传输层照样重试、差异只在采样层」写明，避免被读成「无 Retry-After 就不重试」。"""

        control = self.jobs["official-relay-server-overloaded"]["description"]
        self.assertIn("传输层照样重试", control)
        self.assertIn("5 个请求", control)
        with_retry_after = self.jobs["official-relay-server-overloaded-retry-after"]["description"]
        self.assertIn("30 个请求", with_retry_after)
        self.assertIn("0.160 恒终止", with_retry_after)
        self.assertIn("降级前先等到最近一次 Retry-After 的截止时间", self.jobs["official-relay-ws-retry-after"]["description"])

    def test_0163_directed_labels_follow_job_parameters(self) -> None:
        # 0.163.0 起主线（track=main）是 Lite 模型，标签 mode 随之为 lite（旧版本的主线标 non_lite）。
        expected = {
            "official-relay-cyber-access-ws": ("websocket_trace", {
                "transport": "websocket", "track": "lite", "mode": "lite",
                "variant": "cyber_access_standard", "provider": "openai_oauth",
            }),
            "official-relay-stream-incomplete-http": ("process_trace", {
                "transport": "http", "track": "main", "mode": "lite", "variant": "incomplete_terminal",
                "upstream_response": "relay_synthetic", "provider": "openai_oauth",
            }),
            "official-relay-stream-incomplete-ws": ("websocket_trace", {
                "transport": "websocket", "track": "main", "mode": "lite", "variant": "incomplete_terminal",
                "upstream_response": "relay_synthetic", "provider": "openai_oauth",
            }),
            # 升级请求与降级 POST 同在 h1 请求流里，按 official-relay-conn-retry 的口径标 transport=http。
            "official-relay-ws-retry-after": ("process_trace", {
                "transport": "http", "track": "main", "mode": "lite", "variant": "ws_retry_after",
                "retry_after": "present", "upstream_response": "relay_synthetic", "provider": "openai_oauth",
            }),
            "official-relay-server-overloaded-retry-after": ("process_trace", {
                "transport": "http", "track": "main", "mode": "lite", "variant": "server_overloaded",
                "retry_after": "present", "upstream_response": "relay_synthetic", "provider": "openai_oauth",
            }),
            "official-relay-server-overloaded": ("process_trace", {
                "transport": "http", "track": "main", "mode": "lite", "variant": "server_overloaded",
                "upstream_response": "relay_synthetic", "provider": "openai_oauth",
            }),
        }
        for job_id, (derive_kind, labels) in expected.items():
            with self.subTest(job_id=job_id):
                rules = self.labels[job_id]["rules"]
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0]["labels"], labels)
                self.assertEqual(rules[0]["derive"], {"parser": "h1_request_stream", "kind": derive_kind})
                self.assertEqual(rules[0]["glob"], "relay/*.client_to_upstream.bin")
                self.assertEqual(rules[0]["scenario_ids"], self.jobs[job_id]["scenario_ids"])
                self.assertTrue(rules[0]["rationale"])
        # 过载 503 的一对只差 retry_after 标签（约定同 official-relay-conn-retry／-after：无值样本不带该标签）。
        with_retry_after = self.labels["official-relay-server-overloaded-retry-after"]["rules"][0]["labels"]
        control = self.labels["official-relay-server-overloaded"]["rules"][0]["labels"]
        self.assertEqual({k: v for k, v in with_retry_after.items() if k != "retry_after"}, control)
        # 终止型故障的标签与 interrupted 同构，只差 variant。
        for transport, suffix in (("http", "http"), ("ws", "ws")):
            incomplete = self.labels[f"official-relay-stream-incomplete-{suffix}"]["rules"][0]["labels"]
            interrupted = self.labels[f"official-relay-stream-interrupted-{suffix}"]["rules"][0]["labels"]
            self.assertEqual({k: v for k, v in incomplete.items() if k != "variant"},
                             {k: v for k, v in interrupted.items() if k != "variant"})


if __name__ == "__main__":
    unittest.main()
