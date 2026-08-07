#!/usr/bin/env python3
"""通过同一条网关 WebSocket 串行执行两轮候选 Responses 请求。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HANDSHAKE_BYTES = 64 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_TURN_EVENTS = 512
RESPONSE_ID_PATTERN = re.compile(r"^resp_[A-Za-z0-9._:-]+$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SUCCESS_TERMINALS = {"response.completed", "response.done"}
FAILURE_TERMINALS = {
    "response.failed",
    "response.incomplete",
    "response.cancelled",
    "response.canceled",
    "error",
}


class WebSocketProtocolError(RuntimeError):
    """表示候选网关违反了本次验收冻结的 WebSocket 协议边界。"""


@dataclass(frozen=True)
class WebSocketFrame:
    """保存一条已解码的 RFC 6455 帧。"""

    fin: bool
    opcode: int
    payload: bytes


@dataclass(frozen=True)
class TurnResult:
    """保存一轮业务响应的完整事件与终态响应 ID。"""

    response_id: str
    terminal_event: str
    events: tuple[dict[str, Any], ...]


class BufferedSocket:
    """保留 HTTP 101 响应之后同一次读取中已经到达的 WebSocket 字节。"""

    def __init__(self, stream: socket.socket, initial: bytes = b"") -> None:
        self.stream = stream
        self.buffer = bytearray(initial)

    def receive_exact(self, length: int, deadline: float) -> bytes:
        """在绝对截止时间前精确读取指定字节数。"""

        if length < 0:
            raise WebSocketProtocolError("WebSocket 读取长度不能为负数。")
        while len(self.buffer) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket 读取超过本轮绝对截止时间。")
            self.stream.settimeout(remaining)
            chunk = self.stream.recv(min(65536, length - len(self.buffer)))
            if not chunk:
                raise WebSocketProtocolError("WebSocket 连接在消息完成前关闭。")
            self.buffer.extend(chunk)
        payload = bytes(self.buffer[:length])
        del self.buffer[:length]
        return payload


class GatewayWebSocket:
    """实现候选验收所需的最小、严格 RFC 6455 客户端。"""

    def __init__(self, stream: socket.socket, buffered: bytes = b"") -> None:
        self.stream = stream
        self.reader = BufferedSocket(stream, buffered)
        self.fragment_opcode: int | None = None
        self.fragments = bytearray()

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        """发送一条 FIN=1 且按客户端要求掩码的帧。"""

        if opcode >= 0x8 and len(payload) > 125:
            raise WebSocketProtocolError("WebSocket 控制帧不能超过 125 字节。")
        mask = secrets.token_bytes(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        header.extend(
            byte ^ mask[index % len(mask)] for index, byte in enumerate(payload)
        )
        self.stream.sendall(header)

    def send_json(self, payload: dict[str, Any]) -> None:
        """以紧凑 UTF-8 TEXT 消息发送 response.create。"""

        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError("客户端 response.create 超过验收大小上限。")
        self.send_frame(0x1, encoded)

    def receive_frame(self, deadline: float) -> WebSocketFrame:
        """读取并严格校验一条未压缩、未掩码的服务端帧。"""

        first, second = self.reader.receive_exact(2, deadline)
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if rsv:
            raise WebSocketProtocolError("未协商压缩时服务端帧不得设置 RSV 位。")
        if masked:
            raise WebSocketProtocolError("服务端 WebSocket 帧不得设置掩码位。")
        if length == 126:
            length = struct.unpack("!H", self.reader.receive_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.reader.receive_exact(8, deadline))[0]
            if length & (1 << 63):
                raise WebSocketProtocolError("WebSocket 64 位长度的最高位必须为零。")
        if length > MAX_FRAME_BYTES:
            raise WebSocketProtocolError("服务端 WebSocket 帧超过验收大小上限。")
        if opcode >= 0x8 and (not fin or length > 125):
            raise WebSocketProtocolError("服务端 WebSocket 控制帧格式非法。")
        return WebSocketFrame(
            fin=fin,
            opcode=opcode,
            payload=self.reader.receive_exact(length, deadline),
        )

    def receive_message(self, deadline: float) -> bytes:
        """重组一条 TEXT 消息，并在分片之间正确处理控制帧。"""

        while True:
            frame = self.receive_frame(deadline)
            if frame.opcode == 0x8:
                code = 1005
                reason = ""
                if len(frame.payload) == 1:
                    raise WebSocketProtocolError("服务端 CLOSE 帧状态码长度非法。")
                if len(frame.payload) >= 2:
                    code = struct.unpack("!H", frame.payload[:2])[0]
                    reason = frame.payload[2:].decode("utf-8", "replace")
                raise WebSocketProtocolError(
                    "服务端在业务终态前关闭连接："
                    f"code={code} reason_bytes={len(reason.encode('utf-8'))}"
                )
            if frame.opcode == 0x9:
                self.send_frame(0xA, frame.payload)
                continue
            if frame.opcode == 0xA:
                continue
            if frame.opcode in {0x1, 0x2}:
                if self.fragment_opcode is not None:
                    raise WebSocketProtocolError("分片消息结束前出现新的数据帧。")
                if frame.opcode != 0x1:
                    raise WebSocketProtocolError("候选网关只能下发 TEXT JSON 消息。")
                if frame.fin:
                    return frame.payload
                self.fragment_opcode = frame.opcode
                self.fragments.extend(frame.payload)
            elif frame.opcode == 0x0:
                if self.fragment_opcode is None:
                    raise WebSocketProtocolError("收到没有起始数据帧的 CONTINUATION。")
                self.fragments.extend(frame.payload)
                if len(self.fragments) > MAX_MESSAGE_BYTES:
                    raise WebSocketProtocolError("服务端分片消息超过验收大小上限。")
                if frame.fin:
                    payload = bytes(self.fragments)
                    self.fragments.clear()
                    self.fragment_opcode = None
                    return payload
            else:
                raise WebSocketProtocolError(
                    f"收到不支持的 WebSocket opcode：{frame.opcode}。"
                )
            if len(self.fragments) > MAX_MESSAGE_BYTES:
                raise WebSocketProtocolError("服务端分片消息超过验收大小上限。")

    def close(self) -> None:
        """尽力发送正常关闭帧，再立即释放本地套接字。"""

        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
        except (OSError, RuntimeError):
            pass
        finally:
            self.stream.close()


def _reject_header_control(name: str, value: str) -> str:
    """拒绝任何可能改变握手头边界的控制字符。"""

    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise ValueError(f"{name} 为空或包含非法控制字符。")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} 不是合法的 HTTP/1.1 头值。") from error
    return value


def _read_limited_text(path: Path, label: str, limit: int = 64 * 1024) -> str:
    """读取受控 sidecar，并限制其大小与头注入字符。"""

    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError(f"{label} sidecar 超过大小上限。")
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} sidecar 不是 UTF-8。") from error
    return _reject_header_control(label, value)


def read_api_key(stream: BinaryIO) -> str:
    """只从匿名文件描述符读取 API Key，不把它写入参数、环境或摘要。"""

    raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("API Key 超过验收读取上限。")
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as error:
        raise ValueError("API Key 不是 UTF-8。") from error
    return _reject_header_control("API Key", value)


def _load_request_body(path: Path, label: str) -> dict[str, Any]:
    """读取并校验尚未携带续链 ID 的 response.create 模板。"""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 请求模板不是有效 JSON。") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 请求模板必须是 JSON 对象。")
    if "previous_response_id" in payload:
        raise ValueError(f"{label} 请求模板不得预置 previous_response_id。")
    if payload.get("type") not in {None, "response.create"}:
        raise ValueError(f"{label} 请求模板 type 必须为空或 response.create。")
    if not isinstance(payload.get("model"), str) or not payload["model"].strip():
        raise ValueError(f"{label} 请求模板缺少 model。")
    if not isinstance(payload.get("input"), list):
        raise ValueError(f"{label} 请求模板 input 必须是数组。")
    normalized: dict[str, Any] = {"type": "response.create"}
    normalized.update(payload)
    normalized["type"] = "response.create"
    return normalized


def _open_gateway(
    *,
    host: str,
    port: int,
    path: str,
    api_key: str,
    codex_version: str,
    first_body: Path,
    session_affinity: str,
    timeout: float,
) -> GatewayWebSocket:
    """完成严格 HTTP/1.1 Upgrade，并返回保留尾随字节的 WebSocket。"""

    for label, value in {
        "host": host,
        "path": path,
        "session affinity": session_affinity,
    }.items():
        _reject_header_control(label, value)
    if not VERSION_PATTERN.fullmatch(codex_version):
        raise ValueError("Codex 版本必须是完整的 x.y.z 版本。")
    if not path.startswith("/") or " " in path:
        raise ValueError("WebSocket path 必须是绝对路径且不能包含空格。")
    turn_metadata = _read_limited_text(
        Path(str(first_body) + ".turn-metadata"),
        "X-Codex-Turn-Metadata",
    )
    thread_id = _read_limited_text(
        Path(str(first_body) + ".thread-id"),
        "Thread-Id",
    )
    window_id = _read_limited_text(
        Path(str(first_body) + ".window-id"),
        "X-Codex-Window-Id",
    )
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host_header = f"{host}:{port}"
    request_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_header}",
        "Connection: Upgrade",
        "Upgrade: websocket",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Key: {key}",
        f"Authorization: Bearer {api_key}",
        "User-Agent: sub2apiplus-candidate-capture/1.0",
        "Originator: sub2apiplus_candidate_capture",
        f"Version: {codex_version}",
        "X-Codex-Terminal: unknown",
        "Session-Id: 11111111-1111-4111-8111-111111111111",
        f"Thread-Id: {thread_id}",
        f"X-Client-Request-Id: {thread_id}",
        f"X-Codex-Window-Id: {window_id}",
        f"X-Codex-Turn-Metadata: {turn_metadata}",
        f"X-Session-Affinity: {session_affinity}",
        "OpenAI-Beta: responses_websockets=2026-02-06",
        "",
        "",
    ]
    request = "\r\n".join(request_lines).encode("latin-1")
    deadline = time.monotonic() + timeout
    stream = socket.create_connection((host, port), timeout=timeout)
    try:
        stream.sendall(request)
        response = bytearray()
        marker = -1
        while marker < 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket 握手超过绝对截止时间。")
            stream.settimeout(remaining)
            chunk = stream.recv(4096)
            if not chunk:
                raise WebSocketProtocolError("候选网关在 WebSocket 握手完成前关闭。")
            response.extend(chunk)
            marker = response.find(b"\r\n\r\n")
            if marker < 0 and len(response) > MAX_HANDSHAKE_BYTES:
                raise WebSocketProtocolError("WebSocket 握手响应头超过 64 KiB。")
        if marker + 4 > MAX_HANDSHAKE_BYTES:
            raise WebSocketProtocolError("WebSocket 握手响应头超过 64 KiB。")
        head = bytes(response[:marker])
        buffered = bytes(response[marker + 4 :])
        lines = head.split(b"\r\n")
        status_line = lines[0].decode("ascii", "replace") if lines else ""
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[0] != "HTTP/1.1" or parts[1] != "101":
            status_code = parts[1] if len(parts) >= 2 else "invalid"
            raise WebSocketProtocolError(f"候选网关 WebSocket 握手失败：HTTP {status_code}")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if b":" not in line:
                raise WebSocketProtocolError("候选网关 WebSocket 握手头格式非法。")
            raw_name, raw_value = line.split(b":", 1)
            name = raw_name.decode("ascii", "strict").strip().lower()
            value = raw_value.decode("latin-1").strip()
            headers.setdefault(name, []).append(value)
        connection_tokens = {
            token.strip().lower()
            for value in headers.get("connection", [])
            for token in value.split(",")
        }
        upgrade_tokens = {
            token.strip().lower()
            for value in headers.get("upgrade", [])
            for token in value.split(",")
        }
        if "upgrade" not in connection_tokens or "websocket" not in upgrade_tokens:
            raise WebSocketProtocolError("候选网关 101 响应缺少 WebSocket Upgrade 令牌。")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != [expected_accept]:
            raise WebSocketProtocolError("候选网关 Sec-WebSocket-Accept 校验失败。")
        if "sec-websocket-extensions" in headers:
            raise WebSocketProtocolError("客户端未提议扩展，服务端不得协商 WebSocket 扩展。")
        return GatewayWebSocket(stream, buffered)
    except BaseException:
        stream.close()
        raise


def _event_response_id(event: dict[str, Any]) -> str:
    """从 Responses 事件的公开位置读取响应 ID。"""

    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        return response["id"].strip()
    for key in ("response_id", "id"):
        value = event.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _collect_turn(client: GatewayWebSocket, timeout: float) -> TurnResult:
    """收集一轮直到成功终态；失败终态与缺失响应 ID 都立即拒绝。"""

    deadline = time.monotonic() + timeout
    events: list[dict[str, Any]] = []
    for _ in range(MAX_TURN_EVENTS):
        raw = client.receive_message(deadline)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise WebSocketProtocolError("服务端 JSON 消息超过验收大小上限。")
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WebSocketProtocolError("服务端 WebSocket 消息不是有效 UTF-8 JSON。") from error
        if not isinstance(event, dict):
            raise WebSocketProtocolError("服务端 WebSocket 事件必须是 JSON 对象。")
        events.append(event)
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise WebSocketProtocolError("服务端 WebSocket 事件缺少 type。")
        if event_type in FAILURE_TERMINALS:
            raise WebSocketProtocolError(f"业务轮以失败事件终止：{event_type}。")
        if event_type in SUCCESS_TERMINALS:
            response_id = _event_response_id(event)
            if not RESPONSE_ID_PATTERN.fullmatch(response_id):
                raise WebSocketProtocolError("成功终态缺少合法的 resp_* response.id。")
            return TurnResult(
                response_id=response_id,
                terminal_event=event_type,
                events=tuple(events),
            )
    raise WebSocketProtocolError("单轮 WebSocket 事件数量超过验收上限。")


def _write_private(path: Path, content: bytes) -> None:
    """以 0600 原子写入验收产物，并拒绝覆盖已有文件。"""

    if path.exists():
        raise FileExistsError(f"拒绝覆盖已有验收产物：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_sse(path: Path, result: TurnResult) -> None:
    """把已验证成功的一轮事件写成现有验收解析器可读的 SSE。"""

    lines = []
    for event in result.events:
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"data: {encoded}\n\n")
    lines.append("data: [DONE]\n\n")
    _write_private(path, "".join(lines).encode("utf-8"))


def run_session(
    *,
    host: str,
    port: int,
    path: str,
    api_key: str,
    codex_version: str,
    first_body_path: Path,
    second_body_path: Path,
    first_output_path: Path,
    second_output_path: Path,
    summary_path: Path,
    session_affinity: str,
    timeout: float,
) -> dict[str, Any]:
    """在同一连接内串行执行首轮与带真实 previous_response_id 的续轮。"""

    if timeout <= 0 or timeout > 600:
        raise ValueError("timeout 必须在 0..600 秒内。")
    if port < 1 or port > 65535:
        raise ValueError("port 必须在 1..65535。")
    for output in (first_output_path, second_output_path, summary_path):
        if output.exists():
            raise FileExistsError(f"拒绝覆盖已有验收产物：{output}")
    first = _load_request_body(first_body_path, "首轮")
    second = _load_request_body(second_body_path, "续轮")
    client = _open_gateway(
        host=host,
        port=port,
        path=path,
        api_key=api_key,
        codex_version=codex_version,
        first_body=first_body_path,
        session_affinity=session_affinity,
        timeout=timeout,
    )
    try:
        client.send_json(first)
        first_result = _collect_turn(client, timeout)

        second["previous_response_id"] = first_result.response_id
        client.send_json(second)
        second_result = _collect_turn(client, timeout)
        if second_result.response_id == first_result.response_id:
            raise WebSocketProtocolError("续轮 response.id 不得与首轮相同。")
        secret = api_key.encode("utf-8")
        for result in (first_result, second_result):
            encoded_events = json.dumps(
                result.events,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if secret in encoded_events:
                raise WebSocketProtocolError("服务端事件意外包含入口凭据，拒绝落盘。")

        _write_sse(first_output_path, first_result)
        _write_sse(second_output_path, second_result)

        summary = {
            "schema_version": "candidate-gateway-ws/v1",
            "codex_version": codex_version,
            "status": "complete",
            "transport": "websocket",
            "http_status": 101,
            "same_connection": True,
            "session_affinity": session_affinity,
            "turns": [
                {
                    "turn": 1,
                    "response_id": first_result.response_id,
                    "terminal_event": first_result.terminal_event,
                    "event_types": [event["type"] for event in first_result.events],
                },
                {
                    "turn": 2,
                    "previous_response_id": first_result.response_id,
                    "response_id": second_result.response_id,
                    "terminal_event": second_result.terminal_event,
                    "event_types": [event["type"] for event in second_result.events],
                },
            ],
        }
        _write_private(
            summary_path,
            (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return summary
    finally:
        client.close()


def main() -> int:
    """解析参数并执行双轮候选网关 WebSocket 验收。"""

    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path", default="/v1/responses")
    parser.add_argument("--codex-version", required=True)
    parser.add_argument("--first-body", type=Path, required=True)
    parser.add_argument("--second-body", type=Path, required=True)
    parser.add_argument("--first-output", type=Path, required=True)
    parser.add_argument("--second-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--session-affinity", default="candidate-core-a06")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--api-key-fd",
        type=int,
        default=0,
        help="只读 API Key 文件描述符；默认 stdin。",
    )
    arguments = parser.parse_args()
    try:
        with os.fdopen(os.dup(arguments.api_key_fd), "rb") as key_stream:
            api_key = read_api_key(key_stream)
        summary = run_session(
            host=arguments.host,
            port=arguments.port,
            path=arguments.path,
            api_key=api_key,
            codex_version=arguments.codex_version,
            first_body_path=arguments.first_body,
            second_body_path=arguments.second_body,
            first_output_path=arguments.first_output,
            second_output_path=arguments.second_output,
            summary_path=arguments.summary,
            session_affinity=arguments.session_affinity,
            timeout=arguments.timeout,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"候选网关 WebSocket 双轮驱动失败：{error}", file=sys.stderr)
        return 1
    finally:
        api_key = ""
    print(
        json.dumps(
            {
                "status": summary["status"],
                "same_connection": summary["same_connection"],
                "turn_count": len(summary["turns"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
