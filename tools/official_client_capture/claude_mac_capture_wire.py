"""固定上游的 H1 原始字节记录与增量配对，不重建 HTTP 请求。

HTTP framing 移除后的 entity bytes 用于逐请求原件；双向 wire 原件另外保留。
H2、协议升级和未知 framing 必须保留失败，不能降级为完整 H1 成功。
"""

from __future__ import annotations

import base64
import os
import re
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_client_receipt_contract import MODEL_ENDPOINTS
from tools.official_client_capture.upstream_byte_relay import ByteRecorder

CLIENT_DIRECTION = "client_to_upstream"
SERVER_DIRECTION = "upstream_to_client"
DIRECTIONS = (CLIENT_DIRECTION, SERVER_DIRECTION)
MODEL_PATHS = MODEL_ENDPOINTS
TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class DurableByteRecorder(ByteRecorder):
    """只在独占新连接目录创建原件；每片 fsync 后才能转发。"""

    def __init__(self, out_dir: Path, conn_id: int):
        c.private_directory(out_dir)
        super().__init__(out_dir, conn_id)
        for direction in DIRECTIONS:
            self._stream(direction)
            os.fsync(self.files[direction].fileno())
        c.fsync_directory(out_dir)

    def _stream(self, direction: str):
        c.require(direction in DIRECTIONS, "未知原始字节方向")
        if direction not in self.files:
            import hashlib
            descriptor = os.open(self.dir / f"conn{self.conn_id:03d}.{direction}.bin", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            self.files[direction] = os.fdopen(descriptor, "wb")
            self.digests[direction] = hashlib.sha256()
            self.offsets[direction] = 0
        return self.files[direction]

    def write(self, direction: str, chunk: bytes) -> None:
        super().write(direction, chunk)
        os.fsync(self.files[direction].fileno())


class H1Parser:
    """按实际请求头到达顺序分配 ordinal；响应始终用 H1 FIFO 配对。"""

    def __init__(self, connection_id: str, limits: dict[str, Any], on_header: Callable[[dict[str, Any]], None] | None = None):
        self.connection_id = connection_id
        self.limits = limits
        self.on_header = on_header
        self.requests: list[dict[str, Any]] = []
        self.pending: deque[dict[str, Any]] = deque()
        self.errors: list[str] = []
        self.states = {direction: {"buffer": bytearray(), "offset": 0, "current": None, "framing": "header", "remaining": 0, "chunk_remaining": None, "trailers": False, "eof": False} for direction in DIRECTIONS}

    def _error(self, reason: str) -> None:
        if reason not in self.errors:
            self.errors.append(reason)
        raise c.CaptureError(reason)

    def _headers(self, raw: bytes) -> tuple[bytes, dict[bytes, list[bytes]]]:
        lines = raw.split(b"\r\n")
        headers: dict[bytes, list[bytes]] = {}
        for line in lines[1:]:
            if b":" not in line:
                self._error("H1 header 缺冒号或含折行")
            name, value = line.split(b":", 1)
            if not TOKEN.fullmatch(name):
                self._error("H1 header 名称非法")
            if any(item < 32 and item != 9 for item in value) or b"\x7f" in value:
                self._error("H1 header 值含非法控制字节")
            headers.setdefault(name.lower(), []).append(value.strip())
        return lines[0], headers

    def _framing(self, headers: dict[bytes, list[bytes]], *, response: bool, no_body: bool = False) -> tuple[str, int]:
        if no_body:
            return "none", 0
        lengths = headers.get(b"content-length", [])
        transfer = headers.get(b"transfer-encoding", [])
        if lengths and transfer:
            self._error("H1 framing 同时出现 Content-Length 和 Transfer-Encoding")
        if transfer:
            if b",".join(transfer).lower() != b"chunked":
                self._error("不支持的 H1 Transfer-Encoding")
            return "chunked", 0
        if lengths:
            values = [item.strip() for value in lengths for item in value.split(b",")]
            if not values or any(not value.isdigit() for value in values) or len(set(values)) != 1:
                self._error("H1 Content-Length 冲突或非法")
            amount = int(values[0])
            if amount > self.limits["max_body_bytes"]:
                self._error("H1 正文超过冻结上界")
            return "length", amount
        return ("close", 0) if response else ("none", 0)

    def _start(self, direction: str, raw: bytes, observed_at_utc: str, offset: int) -> tuple[dict[str, Any] | None, str, int]:
        # 原生请求身份先于正文与 framing 验证生成；非法 body 不能漏掉已到达的 POST。
        if direction == CLIENT_DIRECTION:
            first = raw.split(b"\r\n", 1)[0].split(b" ")
            if len(first) != 3 or not TOKEN.fullmatch(first[0]) or first[2] not in {b"HTTP/1.0", b"HTTP/1.1"}:
                self._error("不支持的请求协议或 H1 请求行")
            try:
                method, target = first[0].decode("ascii"), first[1].decode("ascii")
            except UnicodeError:
                self._error("H1 请求行不是合法 ASCII")
            parsed = urlsplit(target) if target.startswith(("http://", "https://")) else None
            path = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "") if parsed else target
            if not path.startswith("/") and method != "CONNECT":
                self._error("不支持的 H1 request-target")
            row = {"connection_id": self.connection_id, "request_ordinal": len(self.requests) + 1, "observed_at_utc": observed_at_utc, "host": "", "method": method, "path": path, "request_body": bytearray(), "response_body": bytearray(), "request_complete": False, "response_complete": False, "response_seen": False, "request_wire_start": offset, "request_wire_end": None, "response_wire_start": None, "response_wire_end": None}
            self.requests.append(row)
            self.pending.append(row)
            _, headers = self._headers(raw)
            hosts = headers.get(b"host", [])
            if len(hosts) == 1:
                try:
                    row["host"] = urlsplit("//" + hosts[0].decode("ascii")).hostname or ""
                except (UnicodeError, ValueError):
                    self._error("H1 Host 非法")
            if len(hosts) > 1:
                self._error("H1 Host 重复")
            if self.on_header:
                self.on_header(row)
            if method == "CONNECT" or headers.get(b"upgrade") or b"upgrade" in b",".join(headers.get(b"connection", [])).lower().split(b","):
                self._error("本机采集尚不支持 CONNECT 或协议升级")
            framing, length = self._framing(headers, response=False)
            return row, framing, length
        first, headers = self._headers(raw)
        pieces = first.split(b" ", 2)
        if len(pieces) < 2 or pieces[0] not in {b"HTTP/1.0", b"HTTP/1.1"} or len(pieces[1]) != 3 or not pieces[1].isdigit():
            self._error("H1 响应行非法")
        status = int(pieces[1])
        if not self.pending:
            self._error("H1 响应缺少原生请求")
        if status == 101:
            self._error("本机采集尚不支持响应协议升级")
        if 100 <= status < 200:
            return None, "none", 0
        row = self.pending[0]
        row["response_seen"] = True
        row["response_wire_start"] = offset
        framing, length = self._framing(headers, response=True, no_body=row["method"] == "HEAD" or status in {204, 304})
        return row, framing, length

    def _consume(self, state: dict[str, Any], amount: int) -> bytes:
        raw = bytes(state["buffer"][:amount])
        del state["buffer"][:amount]
        state["offset"] += amount
        return raw

    def _body(self, direction: str, state: dict[str, Any], raw: bytes) -> None:
        row = state["current"]
        if row is not None:
            body = row["request_body" if direction == CLIENT_DIRECTION else "response_body"]
            if len(body) + len(raw) > self.limits["max_body_bytes"]:
                self._error("H1 正文超过冻结上界")
            body.extend(raw)

    def _finish(self, direction: str, state: dict[str, Any]) -> None:
        row = state["current"]
        if row is not None:
            if direction == CLIENT_DIRECTION:
                row["request_complete"] = True
                row["request_wire_end"] = state["offset"]
            else:
                row["response_complete"] = True
                row["response_wire_end"] = state["offset"]
                self.pending.popleft()
        state.update(current=None, framing="header", remaining=0, chunk_remaining=None, trailers=False)

    def feed(self, direction: str, raw: bytes, observed_at_utc: str) -> None:
        c.require(direction in DIRECTIONS and not self.states[direction]["eof"], "EOF 后不能再追加 wire 字节")
        state = self.states[direction]
        state["buffer"].extend(raw)
        while True:
            if state["framing"] == "header":
                boundary = state["buffer"].find(b"\r\n\r\n")
                if boundary < 0:
                    if len(state["buffer"]) > self.limits["max_header_bytes"]:
                        self._error("H1 header 超过冻结上界")
                    return
                if boundary + 4 > self.limits["max_header_bytes"]:
                    self._error("H1 header 超过冻结上界")
                offset = state["offset"]
                head = self._consume(state, boundary + 4)[:-4]
                row, framing, amount = self._start(direction, head, observed_at_utc, offset)
                state.update(current=row, framing=framing, remaining=amount, chunk_remaining=None, trailers=False)
            framing = state["framing"]
            if framing == "none" or framing == "length" and state["remaining"] == 0:
                self._finish(direction, state)
                continue
            if framing == "length":
                amount = min(len(state["buffer"]), state["remaining"])
                self._body(direction, state, self._consume(state, amount))
                state["remaining"] -= amount
                if state["remaining"]:
                    return
                self._finish(direction, state)
                continue
            if framing == "close":
                self._body(direction, state, self._consume(state, len(state["buffer"])))
                return
            if framing == "chunked":
                if state["trailers"]:
                    if state["buffer"].startswith(b"\r\n"):
                        self._consume(state, 2)
                        self._finish(direction, state)
                        continue
                    boundary = state["buffer"].find(b"\r\n\r\n")
                    if boundary < 0:
                        if len(state["buffer"]) > self.limits["max_header_bytes"]:
                            self._error("H1 trailer 超过冻结上界")
                        return
                    self._headers(b"trailer\r\n" + self._consume(state, boundary + 4)[:-4])
                    self._finish(direction, state)
                    continue
                if state["chunk_remaining"] is None:
                    boundary = state["buffer"].find(b"\r\n")
                    if boundary < 0:
                        if len(state["buffer"]) > self.limits["max_header_bytes"]:
                            self._error("H1 chunk-size 超过冻结上界")
                        return
                    size = self._consume(state, boundary + 2)[:-2].split(b";", 1)[0]
                    if not re.fullmatch(rb"[0-9a-fA-F]+", size):
                        self._error("H1 chunk-size 非法")
                    state["chunk_remaining"] = int(size, 16)
                    if state["chunk_remaining"] == 0:
                        state["trailers"] = True
                        continue
                amount = min(len(state["buffer"]), state["chunk_remaining"])
                if amount:
                    self._body(direction, state, self._consume(state, amount))
                    state["chunk_remaining"] -= amount
                if state["chunk_remaining"]:
                    return
                if len(state["buffer"]) < 2:
                    return
                if self._consume(state, 2) != b"\r\n":
                    self._error("H1 chunk 结束符非法")
                state["chunk_remaining"] = None
                continue
            self._error("H1 parser 未定义的 framing")

    def eof(self, direction: str) -> None:
        state = self.states[direction]
        if state["eof"]:
            return
        state["eof"] = True
        if state["framing"] == "close":
            self._finish(direction, state)
        elif state["framing"] != "header" or state["buffer"]:
            self._error("H1 wire EOF 带未完成消息")

    def has_pending(self) -> bool:
        return bool(self.pending) or any(state["buffer"] or state["framing"] != "header" for state in self.states.values())

    def request_rows(self) -> list[dict[str, Any]]:
        return [{**{name: row[name] for name in ("connection_id", "request_ordinal", "observed_at_utc", "host", "method", "path")}, "request_body_base64": base64.b64encode(row["request_body"]).decode("ascii"), "response_body_base64": base64.b64encode(row["response_body"]).decode("ascii")} for row in self.requests]

    def outcome_rows(self) -> list[dict[str, Any]]:
        return [{**{name: row[name] for name in ("connection_id", "request_ordinal", "request_complete", "response_complete", "response_seen", "request_wire_start", "request_wire_end", "response_wire_start", "response_wire_end")}, "status": "complete" if row["request_complete"] and row["response_complete"] else "truncated"} for row in self.requests]


def peek_client_hello(connection: socket.socket, timeout_seconds: float) -> tuple[list[str], bytes]:
    """完整重组 ClientHello 后解析 ALPN；解析失败与没有 ALPN 严格区分。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            raw = connection.recv(65536, socket.MSG_PEEK)
        except socket.timeout:
            continue
        c.require(bool(raw), "TLS ClientHello 前连接关闭")
        position, fragments = 0, bytearray()
        complete = False
        while position + 5 <= len(raw):
            c.require(raw[position] == 22, "TLS 首消息不是 ClientHello handshake")
            length = int.from_bytes(raw[position + 3:position + 5], "big")
            c.require(0 < length <= 18432, "TLS handshake record 长度非法")
            if position + 5 + length > len(raw):
                break
            fragments.extend(raw[position + 5:position + 5 + length])
            position += 5 + length
            if len(fragments) >= 4:
                c.require(fragments[0] == 1, "TLS 首握手不是 ClientHello")
                expected = 4 + int.from_bytes(fragments[1:4], "big")
                c.require(expected <= 65531, "TLS ClientHello 超过有界能力")
                if len(fragments) >= expected:
                    complete = True
                    body = bytes(fragments[4:expected])
                    break
        if complete:
            def take(position: int, length: int) -> bytes:
                c.require(position + length <= len(body), "TLS ClientHello 字段截断")
                return body[position:position + length]
            at = 34
            at += 1 + take(at, 1)[0]
            at += 2 + int.from_bytes(take(at, 2), "big")
            at += 1 + take(at, 1)[0]
            if at == len(body):
                return [], raw[:position]
            extension_length = int.from_bytes(take(at, 2), "big")
            at += 2
            c.require(at + extension_length == len(body), "TLS 扩展长度不闭合")
            offers, seen = [], set()
            while at < len(body):
                extension = int.from_bytes(take(at, 2), "big")
                length = int.from_bytes(take(at + 2, 2), "big")
                at += 4
                value = take(at, length)
                at += length
                c.require(extension not in seen, "TLS ClientHello 扩展重复")
                seen.add(extension)
                if extension == 16:
                    c.require(len(value) >= 2 and int.from_bytes(value[:2], "big") == len(value) - 2, "TLS ALPN 列表截断")
                    current = 2
                    while current < len(value):
                        amount = value[current]
                        current += 1
                        c.require(amount > 0 and current + amount <= len(value), "TLS ALPN 名称截断")
                        try:
                            offers.append(value[current:current + amount].decode("ascii"))
                        except UnicodeError as error:
                            raise c.CaptureError("TLS ALPN 名称非法") from error
                        current += amount
            return offers, raw[:position]
        c.require(len(raw) < 65536, "TLS ClientHello 不完整或超过有界能力")
        time.sleep(0.005)
    raise c.CaptureError("TLS ClientHello 未在冻结等待窗口内完成")
