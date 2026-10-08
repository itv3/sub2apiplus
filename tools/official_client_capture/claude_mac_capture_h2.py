"""仅用于回归夹具的 H2 双向增量观察器，不发送或重建任何协议字节。

按 RFC 9113 / RFC 7541 保存原生 stream 身份、帧位置和 HPACK 动态表。
本批尚未接入监听服务，也不能证明 TLS、真实产品或正式来源已就绪。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import re
from collections import deque
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies

CLIENT_DIRECTION = "client_to_upstream"
SERVER_DIRECTION = "upstream_to_client"
DIRECTIONS = (CLIENT_DIRECTION, SERVER_DIRECTION)
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
LIMIT_FIELDS = {"max_header_bytes", "max_body_bytes", "max_wire_bytes", "max_streams",
                "max_frame_bytes", "max_hpack_table_bytes", "max_pending_settings",
                "max_frames", "max_settings_entries"}
_TOKEN = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FIELD = re.compile(rb"^[!#$%&'*+.^_`|~0-9a-z-]+$")
_FORBIDDEN = {b"connection", b"proxy-connection", b"keep-alive", b"transfer-encoding", b"upgrade"}


def _integer(block: bytes, offset: int, prefix: int) -> tuple[int, int]:
    c.require(offset < len(block), "HPACK 整数缺首字节")
    maximum = (1 << prefix) - 1
    value = block[offset] & maximum
    offset += 1
    if value < maximum:
        return value, offset
    # 4.1.0 官方整数循环无界；本观察器按 RFC 7541 §5.1 先执行 uint32 和长度界。
    for shift in range(0, 35, 7):
        c.require(offset < len(block), "HPACK 整数未终结")
        current = block[offset]
        offset += 1
        value += (current & 127) << shift
        c.require(value <= 0xFFFFFFFF, "HPACK 整数超过 uint32 上界")
        if not current & 128:
            return value, offset
    raise c.CaptureError("HPACK 整数扩展超过五字节上界")


def validate_hpack_integer_bounds(block: bytes) -> list[int]:
    """按表示语法跳过字符串原字节；不会把合法字符串中的 FF 误判成整数攻击。"""
    def string(offset: int) -> int:
        length, offset = _integer(block, offset, 7)
        c.require(length <= len(block) - offset, "HPACK 字符串超过完整 field block")
        return offset + length
    offset, fields, updates = 0, False, []
    while offset < len(block):
        first = block[offset]
        if first & 128:
            _value, offset = _integer(block, offset, 7)
            fields = True
        elif first & 64 or not first & 32:
            name, offset = _integer(block, offset, 6 if first & 64 else 4)
            if name == 0:
                offset = string(offset)
            offset = string(offset)
            fields = True
        else:
            c.require(not fields, "HPACK 表大小更新出现在字段之后")
            value, offset = _integer(block, offset, 5)
            updates.append(value)
            c.require(len(updates) <= 2, "HPACK 表大小更新超过两次")
    return updates


class H2Parser:
    """被动解析两个方向；请求顺序按首个 HEADERS，响应按 stream_id 配对。"""

    source_scope = c.SCOPE
    formal_ready = False
    launch_allowed = False

    def __init__(self, connection_id: str, limits: Mapping[str, Any],
                 on_header: Callable[[dict[str, Any]], None] | None = None, *,
                 dependencies: FrozenDependencies, source_scope: str = c.SCOPE):
        c.require(source_scope == c.SCOPE and isinstance(connection_id, str) and bool(connection_id),
                  "H2 观察器只接受回归夹具与固定连接身份")
        self.limits = c.exact(dict(limits), LIMIT_FIELDS, "H2 冻结上界")
        c.require(all(type(value) is int and value > 0 for value in self.limits.values()),
                  "H2 上界必须为正整数")
        c.require(16384 <= self.limits["max_frame_bytes"] <= 0xFFFFFF
                  and 4096 <= self.limits["max_hpack_table_bytes"] <= 0xFFFFFFFF,
                  "H2 帧或 HPACK 表上界不支持协议初始值")
        c.require(isinstance(dependencies, FrozenDependencies)
                  and dependencies.kind == "production_decoder", "H2 必须绑定生产解码闭包")
        self.dependencies = dependencies
        self.decoders = {direction: dependencies.new_hpack_decoder(self.limits["max_header_bytes"])
                         for direction in DIRECTIONS}
        self.connection_id, self.on_header = connection_id, on_header
        self.requests: list[dict[str, Any]] = []
        self.streams: dict[int, dict[str, Any]] = {}
        self.errors: list[str] = []
        self.frames: list[dict[str, Any]] = []
        self.blocks: list[dict[str, Any]] = []
        self.states = {direction: {"buffer": bytearray(), "offset": 0, "block": None,
                                  "preface": direction == SERVER_DIRECTION,
                                  "first_frame": False, "eof": False}
                       for direction in DIRECTIONS}
        self.pending_settings = {direction: deque() for direction in DIRECTIONS}
        self.frame_sizes = {direction: 16384 for direction in DIRECTIONS}
        self.minimum_table_update: dict[str, int | None] = {direction: None for direction in DIRECTIONS}
        self.goaway_last: dict[str, int | None] = {direction: None for direction in DIRECTIONS}
        self._wire_bytes, self._highest_stream = 0, 0
        self._fatal = False
        self._admission_error: str | None = None

    def _note(self, reason: str) -> None:
        if reason not in self.errors:
            self.errors.append(reason)

    def _require(self, condition: Any, reason: str) -> None:
        if not condition:
            self._fatal = True
            self._note(reason)
            raise c.CaptureError(reason)

    def _opposite(self, direction: str) -> str:
        return SERVER_DIRECTION if direction == CLIENT_DIRECTION else CLIENT_DIRECTION

    def _consume(self, state: dict[str, Any], amount: int) -> bytes:
        raw = bytes(state["buffer"][:amount])
        del state["buffer"][:amount]
        state["offset"] += amount
        return raw

    def feed(self, direction: str, raw: bytes, observed_at_utc: str) -> None:
        self._require(direction in DIRECTIONS and isinstance(raw, bytes), "H2 输入方向或字节类型非法")
        c.timestamp(observed_at_utc)
        self._require(not self._fatal and not self.states[direction]["eof"], "H2 已失败或 EOF 后不能继续输入")
        self._wire_bytes += len(raw)
        self._require(self._wire_bytes <= self.limits["max_wire_bytes"], "H2 wire 字节超过冻结上界")
        state = self.states[direction]
        state["buffer"].extend(raw)
        if not state["preface"]:
            prefix = bytes(state["buffer"][:len(PREFACE)])
            self._require(PREFACE.startswith(prefix), "H2 客户端 preface 非法；不能降级 H1")
            if len(state["buffer"]) < len(PREFACE):
                return
            self._consume(state, len(PREFACE))
            state["preface"] = True
        while len(state["buffer"]) >= 9:
            header = bytes(state["buffer"][:9])
            length, kind, flags = int.from_bytes(header[:3], "big"), header[3], header[4]
            stream_id = int.from_bytes(header[5:9], "big") & 0x7FFFFFFF
            self._require(length <= min(self.frame_sizes[direction], self.limits["max_frame_bytes"]),
                          "H2 帧超过已确认或冻结大小上界")
            if len(state["buffer"]) < 9 + length:
                if kind == 1 and direction == CLIENT_DIRECTION and stream_id not in self.streams:
                    self._require(state["first_frame"] and state["block"] is None,
                                  "H2 首帧或未完成 block 后不能提前开启请求")
                    self._new_stream(stream_id, {"wire_offset": state["offset"],
                                                "observed_at_utc": observed_at_utc})
                break
            self._require(len(self.frames) < self.limits["max_frames"], "H2 帧数超过冻结上界")
            offset = state["offset"]
            frame = self._consume(state, 9 + length)
            reference = {"direction": direction, "stream_id": stream_id, "frame_type": kind,
                         "flags": flags, "wire_offset": offset, "bytes": len(frame),
                         "complete": True, "declared_payload_bytes": length,
                         "sha256": hashlib.sha256(frame).hexdigest(),
                         "observed_at_utc": observed_at_utc}
            self.frames.append(reference)
            self._process(direction, kind, flags, stream_id, frame[9:], reference)
        # 预算拒绝保留同片后续 DATA，再把拒绝交给转发控制面；本模块从不发送字节。
        if self._admission_error is not None:
            raise c.CaptureError(self._admission_error)

    def _process(self, direction: str, kind: int, flags: int, stream_id: int,
                 payload: bytes, reference: dict[str, Any]) -> None:
        state = self.states[direction]
        if not state["first_frame"]:
            self._require(kind == 4 and not flags & 1 and stream_id == 0,
                          "H2 两方向首帧必须为非 ACK SETTINGS")
            state["first_frame"] = True
        if state["block"] is not None:
            self._require(kind == 9 and stream_id == state["block"]["stream_id"],
                          "H2 field block 中途插帧或 CONTINUATION 换 stream")
        if kind == 4:
            self._settings(direction, flags, stream_id, payload)
        elif kind in {1, 9}:
            self._headers(direction, kind, flags, stream_id, payload, reference)
        elif kind == 0:
            self._data(direction, flags, stream_id, payload, reference)
        elif kind == 3:
            self._require(stream_id in self.streams and len(payload) == 4, "H2 RST_STREAM 长度或 stream 非法")
            row = self.streams[stream_id]
            row["terminated"] = True
            row["reset_code"] = int.from_bytes(payload, "big")
            row["failure"] = "RST_STREAM"
        elif kind == 7:
            self._require(stream_id == 0 and len(payload) >= 8, "H2 GOAWAY 长度或 stream 非法")
            last = int.from_bytes(payload[:4], "big") & 0x7FFFFFFF
            previous = self.goaway_last[direction]
            self._require(previous is None or last <= previous, "H2 GOAWAY last_stream_id 回升")
            self.goaway_last[direction] = last
            error = int.from_bytes(payload[4:8], "big")
            for row in self.requests:
                if direction == SERVER_DIRECTION and (row["stream_id"] > last or error):
                    if not row["response_complete"]:
                        row.update(terminated=True, failure="GOAWAY", goaway_error=error)
        elif kind == 6:
            self._require(stream_id == 0 and len(payload) == 8, "H2 PING 长度或 stream 非法")
        elif kind == 8:
            self._require(len(payload) == 4 and int.from_bytes(payload, "big") & 0x7FFFFFFF,
                          "H2 WINDOW_UPDATE 长度或增量非法")
        elif kind == 2:
            self._require(stream_id != 0 and len(payload) == 5
                          and int.from_bytes(payload[:4], "big") & 0x7FFFFFFF != stream_id,
                          "H2 PRIORITY 长度、stream 或依赖非法")
        elif kind == 5:
            self._require(False, "本批 H2 不支持 PUSH_PROMISE；不能当成普通请求")
        # 未知扩展帧只记原字节与元数据；field block 内仍由上方连续性规则拒绝。

    def _settings(self, direction: str, flags: int, stream_id: int, payload: bytes) -> None:
        self._require(stream_id == 0 and len(payload) % 6 == 0, "H2 SETTINGS stream 或长度非法")
        if flags & 1:
            self._require(not payload, "H2 SETTINGS ACK 不得带 payload")
            pending = self.pending_settings[self._opposite(direction)]
            self._require(bool(pending), "H2 SETTINGS ACK 没有对应来源")
            # ACK 对应另一方向最早未确认 SETTINGS；修改 ACK 发送者的编码方向 Decoder。
            for identifier, value in pending.popleft():
                if identifier == 1:
                    decoder = self.decoders[direction]
                    if value < decoder.header_table_size:
                        old = self.minimum_table_update[direction]
                        self.minimum_table_update[direction] = value if old is None else min(old, value)
                    decoder.max_allowed_table_size = value
                elif identifier == 5:
                    self.frame_sizes[direction] = value
            return
        self._require(len(payload) // 6 <= self.limits["max_settings_entries"]
                      and len(self.pending_settings[direction]) < self.limits["max_pending_settings"],
                      "H2 SETTINGS 项或未确认帧数超过冻结上界")
        values = []
        for offset in range(0, len(payload), 6):
            identifier = int.from_bytes(payload[offset:offset + 2], "big")
            value = int.from_bytes(payload[offset + 2:offset + 6], "big")
            if identifier == 1:
                self._require(value <= self.limits["max_hpack_table_bytes"], "H2 HPACK 表设置超过冻结上界")
            elif identifier == 2:
                self._require(value in {0, 1} and (direction == CLIENT_DIRECTION or value == 0),
                              "H2 ENABLE_PUSH 来源或值非法")
            elif identifier == 4:
                self._require(value <= 0x7FFFFFFF, "H2 INITIAL_WINDOW_SIZE 超过协议上界")
            elif identifier == 5:
                self._require(16384 <= value <= 0xFFFFFF, "H2 MAX_FRAME_SIZE 非法")
            values.append((identifier, value))
        self.pending_settings[direction].append(values)

    def _unpadded(self, flags: int, payload: bytes) -> bytes:
        if flags & 8:
            self._require(bool(payload) and payload[0] < len(payload), "H2 padding 长度非法")
            return payload[1:len(payload) - payload[0]] if payload[0] else payload[1:]
        return payload

    def _new_stream(self, stream_id: int, reference: dict[str, Any]) -> dict[str, Any]:
        self._require(stream_id > self._highest_stream and stream_id % 2 == 1,
                      "H2 客户端 stream_id 非奇数递增或被复用")
        self._require(len(self.requests) < self.limits["max_streams"], "H2 stream 数超过冻结上界")
        self._highest_stream = stream_id
        row = {"connection_id": self.connection_id, "stream_id": stream_id,
               "request_ordinal": len(self.requests) + 1,
               "observed_at_utc": reference["observed_at_utc"], "host": "", "method": "", "path": "",
               "request_body": bytearray(), "response_body": bytearray(),
               "request_headers_seen": False, "response_seen": False,
               "request_complete": False, "response_complete": False, "terminated": False,
               "request_length": None, "response_length": None, "response_status": None,
               "request_wire_start": reference["wire_offset"], "request_wire_end": None,
               "response_wire_start": None, "response_wire_end": None, "failure": None}
        self.requests.append(row)
        self.streams[stream_id] = row
        return row

    def _headers(self, direction: str, kind: int, flags: int, stream_id: int,
                 payload: bytes, reference: dict[str, Any]) -> None:
        state = self.states[direction]
        self._require(stream_id != 0, "H2 HEADERS／CONTINUATION 使用零 stream")
        if kind == 1:
            self._require(state["block"] is None, "H2 重叠 HEADERS block")
            if direction == CLIENT_DIRECTION and stream_id not in self.streams:
                self._new_stream(stream_id, reference)
            self._require(stream_id in self.streams, "H2 响应没有原生请求 stream")
            row = self.streams[stream_id]
            self._require(direction == CLIENT_DIRECTION or row["request_headers_seen"],
                          "H2 响应早于完整请求字段")
            self._require(not row["terminated"] and not row["request_complete" if direction == CLIENT_DIRECTION else "response_complete"],
                          "H2 已关闭方向又收到 HEADERS")
            fragment = self._unpadded(flags, payload)
            if flags & 32:
                self._require(len(fragment) >= 5
                              and int.from_bytes(fragment[:4], "big") & 0x7FFFFFFF != stream_id,
                              "H2 HEADERS priority 字段非法")
                fragment = fragment[5:]
            state["block"] = {"stream_id": stream_id, "raw": bytearray(fragment),
                              "end_stream": bool(flags & 1), "references": [reference]}
        else:
            self._require(state["block"] is not None, "H2 孤立 CONTINUATION")
            state["block"]["raw"].extend(payload)
            state["block"]["references"].append(reference)
        self._require(len(state["block"]["raw"]) <= self.limits["max_header_bytes"],
                      "H2 压缩 field block 超过冻结上界")
        if flags & 4:
            block = state["block"]
            state["block"] = None
            self._finish_headers(direction, block)

    def _finish_headers(self, direction: str, block: dict[str, Any]) -> None:
        row = self.streams[block["stream_id"]]
        raw = bytes(block["raw"])
        try:
            updates = validate_hpack_integer_bounds(raw)
            minimum = self.minimum_table_update[direction]
            self._require(minimum is None or (updates and updates[0] <= minimum),
                          "H2 ACK 后缺少最小 HPACK 表缩减信号")
            self.dependencies.revalidate()
            headers = list(self.decoders[direction].decode(raw, raw=True))
        except c.CaptureError as error:
            self._require(False, str(error))
        except Exception as error:
            self._require(False, "HPACK 官方解码失败：" + type(error).__name__)
        self.minimum_table_update[direction] = None
        material = {"connection_id": self.connection_id, "stream_id": row["stream_id"],
                    "request_ordinal": row["request_ordinal"], "direction": direction,
                    "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "frame_references": list(block["references"]),
                    "headers": [{"name_base64": base64.b64encode(name).decode("ascii"),
                                 "value_base64": base64.b64encode(value).decode("ascii")}
                                for name, value in headers]}
        self.blocks.append(material)
        first = not row["request_headers_seen"] if direction == CLIENT_DIRECTION else not row["response_seen"]
        pseudo_values: dict[bytes, list[bytes]] = {}
        for name, value in headers:
            if name.startswith(b":"):
                pseudo_values.setdefault(name, []).append(value)
        # 先保留唯一可识别的 POST 身份，再验证其它 HTTP 语义；非法正文或失败不能漏计。
        if direction == CLIENT_DIRECTION and first:
            for field, key in ((b":method", "method"), (b":path", "path")):
                values = pseudo_values.get(field, [])
                if len(values) == 1:
                    try:
                        row[key] = values[0].decode("ascii")
                    except UnicodeError:
                        pass
            authorities = pseudo_values.get(b":authority", [])
            hosts = [value for name, value in headers if name == b"host"]
            authority = authorities[0] if len(authorities) == 1 else (hosts[0] if len(hosts) == 1 else b"")
            try:
                row["host"] = urlsplit("//" + authority.decode("ascii")).hostname or ""
            except (ValueError, UnicodeError):
                pass
            if row["method"] and row["path"] and self.on_header is not None:
                try:
                    self.on_header(row)
                except Exception as error:
                    reason = str(error) if isinstance(error, c.CaptureError) else type(error).__name__
                    self._admission_error = "H2 请求预算准入拒绝：" + reason
                    self._note(self._admission_error)
        pseudo = self._validate_fields(headers)
        if direction == CLIENT_DIRECTION and first:
            self._require(set(pseudo) <= {b":method", b":path", b":scheme", b":authority"}
                          and {b":method", b":path", b":scheme"} <= set(pseudo)
                          and bool(_TOKEN.fullmatch(pseudo[b":method"]))
                          and bool(re.fullmatch(rb"[A-Za-z][A-Za-z0-9+.-]*", pseudo[b":scheme"]))
                          and row["method"] != "CONNECT"
                          and (row["path"].startswith("/") or (row["method"] == "OPTIONS" and row["path"] == "*")),
                          "H2 请求伪头缺失、非法或属于未支持协议")
            authority = pseudo.get(b":authority", b"")
            hosts = [value for name, value in headers if name == b"host"]
            self._require(len(hosts) <= 1, "H2 Host 重复")
            if authority and hosts:
                self._require(authority.lower() == hosts[0].lower(), "H2 authority 与 Host 冲突")
            try:
                target = urlsplit("//" + (authority or (hosts[0] if hosts else b"")).decode("ascii"))
                self._require(target.username is None and target.password is None, "H2 authority 含 userinfo")
                target.port
                row["host"] = target.hostname or ""
            except (ValueError, UnicodeError):
                self._require(False, "H2 authority 非法")
            row["request_headers_seen"] = True
            row["request_length"] = self._content_length(headers)
            goaway = self.goaway_last[SERVER_DIRECTION]
            self._require(goaway is None or row["stream_id"] <= goaway, "H2 GOAWAY 后仍开启新请求")
        elif direction == SERVER_DIRECTION and first:
            self._require(set(pseudo) == {b":status"} and len(pseudo[b":status"]) == 3
                          and pseudo[b":status"].isdigit(), "H2 响应伪头非法")
            status = int(pseudo[b":status"])
            self._require(100 <= status <= 599 and status != 101, "H2 响应状态不支持")
            if status < 200:
                self._require(not block["end_stream"], "H2 1xx 响应不得 END_STREAM")
                return
            row.update(response_seen=True, response_status=status,
                       response_length=self._content_length(headers),
                       response_wire_start=block["references"][0]["wire_offset"])
        else:
            self._require(not pseudo and block["end_stream"], "H2 trailers 含伪头或缺 END_STREAM")
            self._require(not any(name == b"content-length" for name, _value in headers),
                          "H2 trailers 不得重定义 Content-Length")
        if block["end_stream"]:
            self._finish_message(direction, row, block["references"][-1])

    def _validate_fields(self, headers) -> dict[bytes, bytes]:
        pseudo: dict[bytes, bytes] = {}
        normal_seen = False
        for name, value in headers:
            self._require(bool(name) and not any((byte < 32 and byte != 9) or byte == 127 for byte in value)
                          and (not value or value[:1] not in {b" ", b"\t"})
                          and (not value or value[-1:] not in {b" ", b"\t"}),
                          "H2 字段值含非法边界或控制字节")
            if name.startswith(b":"):
                self._require(not normal_seen and name not in pseudo, "H2 伪头重复或排在普通字段之后")
                pseudo[name] = value
            else:
                normal_seen = True
                self._require(bool(_FIELD.fullmatch(name)) and name not in _FORBIDDEN
                              and (name != b"te" or value == b"trailers"), "H2 普通字段名或连接专属字段非法")
        return pseudo

    def _content_length(self, headers) -> int | None:
        values = [value for name, value in headers if name == b"content-length"]
        if not values:
            return None
        self._require(all(value.isdigit() for value in values) and len(set(values)) == 1,
                      "H2 Content-Length 非法或冲突")
        self._require(len(values[0]) <= 10, "H2 Content-Length 超过冻结长度界")
        amount = int(values[0])
        self._require(amount <= self.limits["max_body_bytes"], "H2 正文声明超过冻结上界")
        return amount

    def _data(self, direction: str, flags: int, stream_id: int, payload: bytes,
              reference: dict[str, Any]) -> None:
        self._require(stream_id in self.streams, "H2 DATA 没有已观察请求 stream")
        row = self.streams[stream_id]
        side = "request" if direction == CLIENT_DIRECTION else "response"
        self._require(not row["terminated"] and not row[side + "_complete"]
                      and row["request_headers_seen" if side == "request" else "response_seen"],
                      "H2 DATA 出现在头部之前或已关闭方向")
        entity = self._unpadded(flags, payload)
        self._require(side != "response" or not entity
                      or (row["method"] != "HEAD" and row["response_status"] not in {204, 304}),
                      "H2 无正文响应携带 DATA")
        body = row[side + "_body"]
        self._require(len(body) + len(entity) <= self.limits["max_body_bytes"], "H2 正文超过冻结上界")
        body.extend(entity)
        if flags & 1:
            self._finish_message(direction, row, reference)

    def _finish_message(self, direction: str, row: dict[str, Any], reference: dict[str, Any]) -> None:
        side = "request" if direction == CLIENT_DIRECTION else "response"
        expected = row[side + "_length"]
        no_body_response = side == "response" and (row["method"] == "HEAD" or row["response_status"] == 304)
        self._require(expected is None or no_body_response or expected == len(row[side + "_body"]),
                      "H2 END_STREAM 的 Content-Length 与实体字节不符")
        row[side + "_complete"] = True
        row[side + "_wire_end"] = reference["wire_offset"] + reference["bytes"]

    def eof(self, direction: str) -> None:
        self._require(direction in DIRECTIONS, "H2 EOF 方向非法")
        state = self.states[direction]
        if state["eof"]:
            return
        state["eof"] = True
        if state["buffer"]:
            raw = bytes(state["buffer"])
            partial = {"direction": direction, "wire_offset": state["offset"],
                       "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "complete": False}
            if len(raw) >= 9:
                length = int.from_bytes(raw[:3], "big")
                stream_id = int.from_bytes(raw[5:9], "big") & 0x7FFFFFFF
                partial.update(stream_id=stream_id, frame_type=raw[3], flags=raw[4],
                               declared_payload_bytes=length)
                # 只收取已观察、能定位到已知 DATA stream 的实体前缀，不伪造 END_STREAM。
                row = self.streams.get(stream_id)
                side = "request" if direction == CLIENT_DIRECTION else "response"
                if (raw[3] == 0 and row is not None and not row["terminated"]
                        and not row[side + "_complete"]
                        and row["request_headers_seen" if side == "request" else "response_seen"]):
                    payload = raw[9:]
                    if raw[4] & 8:
                        entity = payload[1:1 + max(0, length - 1 - payload[0])] if payload else b""
                    else:
                        entity = payload
                    remaining = max(0, self.limits["max_body_bytes"] - len(row[side + "_body"]))
                    row[side + "_body"].extend(entity[:remaining])
            self.frames.append(partial)
        if state["buffer"] or state["block"] is not None or not state["preface"] or not state["first_frame"]:
            self._require(False, "H2 EOF 带未完成 preface、帧或 field block")
        side = "request" if direction == CLIENT_DIRECTION else "response"
        for row in self.requests:
            if not row[side + "_complete"] and not row["terminated"]:
                row.update(terminated=True, failure="EOF")
                self._note("H2 EOF 带未完成 stream")

    def has_pending(self) -> bool:
        return (any(state["buffer"] or state["block"] is not None or not state["preface"]
                    or not state["first_frame"] for state in self.states.values())
                or any(self.pending_settings.values())
                or any(not row["terminated"] and not (row["request_complete"] and row["response_complete"])
                       for row in self.requests))

    def request_rows(self) -> list[dict[str, Any]]:
        return [{"schema_version": "claude-upgrade-h2-native-request/v1", "protocol": "h2",
                 **{name: row[name] for name in ("connection_id", "stream_id", "request_ordinal",
                                                "observed_at_utc", "host", "method", "path")},
                 "request_body_base64": base64.b64encode(row["request_body"]).decode("ascii"),
                 "response_body_base64": base64.b64encode(row["response_body"]).decode("ascii")}
                for row in self.requests]

    def outcome_rows(self) -> list[dict[str, Any]]:
        return [{"schema_version": "claude-upgrade-h2-native-outcome/v1", "protocol": "h2",
                 **{name: row[name] for name in ("connection_id", "stream_id", "request_ordinal",
                     "request_complete", "response_complete", "response_seen", "request_wire_start",
                     "request_wire_end", "response_wire_start", "response_wire_end", "failure")},
                 "status": "failed" if row["failure"] else
                 ("complete" if row["request_complete"] and row["response_complete"] else "truncated")}
                for row in self.requests]

    def frame_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.frames)

    def header_block_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.blocks)

    def dependency_receipt(self) -> dict[str, Any]:
        return self.dependencies.receipt()
