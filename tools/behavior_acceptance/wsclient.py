# -*- coding: utf-8 -*-
"""模拟 Codex CLI 的 WebSocket 客户端（标准库）：握手、发 response.create、收事件直到终态或连接关闭。

只做行为验收需要的最小实现：请求头按 Codex 0.165 的形态伪装（网关据此识别官方客户端出站链路），
大帧发送用整数异或做掩码，避免对几 MiB 的首帧逐字节循环。"""
import base64, hashlib, json, os, socket, struct, time, uuid

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Turn:
    """一轮结果：事件类型序列、错误事件、完成编号、关闭信息。"""
    def __init__(self):
        self.events = []        # 事件 dict 列表
        self.types = []
        self.error = None       # 第一个 type=error 事件
        self.completed_id = None
        self.failed = False
        self.closed = None      # (code, reason) 或 ("eof", "")
        self.timed_out = False

    def summary(self):
        err = ""
        if self.error:
            e = self.error.get("error", {})
            err = " error.code=%s status=%s" % (e.get("code"), self.error.get("status"))
        close = " closed=%s" % (self.closed,) if self.closed else ""
        return "events=%s%s completed=%s%s%s" % (self.types, err, self.completed_id, close, " TIMEOUT" if self.timed_out else "")


class WSClient:
    def __init__(self, key, host="127.0.0.1", port=None, path="/v1/responses", headers=None, session_id=None):
        if port is None:
            port = int(os.environ.get("BHV_PORT", "28190"))   # 套件网关映射到宿主机的端口
        self.key, self.host, self.port, self.path = key, host, port, path
        self.session_id = session_id or str(uuid.uuid4())
        self.extra_headers = headers or {}
        self.sock = None
        self.status = None
        self.resp_headers = {}
        self.request_id = None
        self.client_request_id = None
        self.buf = b""

    def connect(self, timeout=15):
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        nonce = base64.b64encode(os.urandom(16)).decode()
        headers = {
            "Host": "%s:%d" % (self.host, self.port), "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Key": nonce, "Sec-WebSocket-Version": "13",
            "Authorization": "Bearer " + self.key,
            "User-Agent": "codex_cli_rs/0.165.0 (Mac OS 26.0.0; arm64) Terminal (codex-tui; 0.165.0)",
            "originator": "codex_cli_rs", "version": "0.165.0",
            "OpenAI-Beta": "responses_websockets=2026-02-06",
            "session_id": self.session_id, "x-client-request-id": str(uuid.uuid4()),
        }
        headers.update(self.extra_headers)
        req = "GET %s HTTP/1.1\r\n" % self.path + "".join("%s: %s\r\n" % kv for kv in headers.items()) + "\r\n"
        s.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split(" ")[1]) if lines and lines[0] else 0
        self.resp_headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        self.request_id = self.resp_headers.get("x-request-id")
        self.client_request_id = self.resp_headers.get("x-client-request-id")
        self.buf = rest
        self.sock = s
        if self.status != 101:
            body = rest.decode("utf-8", "replace")
            raise RuntimeError("握手失败 status=%s body=%s" % (self.status, body[:300]))
        return self

    def send_json(self, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        n = len(payload)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        # 大帧（几 MiB）用整数异或，避免逐字节 Python 循环
        if n:
            key = (mask * (n // 4 + 1))[:n]
            masked = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
        else:
            masked = b""
        self.sock.sendall(bytes(head) + mask + masked)

    def _recv(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_message(self):
        """读一条完整消息：返回 ("text", str) | ("close", (code, reason))。"""
        data, op0 = bytearray(), None
        while True:
            b1, b2 = self._recv(2)
            fin, op, length = bool(b1 & 0x80), b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv(8))[0]
            mask = self._recv(4) if b2 & 0x80 else None
            payload = self._recv(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                return "close", (code, payload[2:].decode("utf-8", "replace"))
            if op == 0x9:
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if op == 0xA:
                continue
            if op in (0x1, 0x2):
                op0, data = op, bytearray(payload)
            else:
                data.extend(payload)
            if fin:
                return "text", bytes(data).decode("utf-8", "replace")

    def run_turn(self, frame, timeout=25):
        """发一帧并收事件，直到 response.completed / response.failed / error 后一小段静默 / 连接关闭 / 超时。"""
        turn = Turn()
        self.send_json(frame)
        deadline = time.time() + timeout
        self.sock.settimeout(2.0)
        quiet_after_error = None
        while time.time() < deadline:
            try:
                kind, val = self.read_message()
            except socket.timeout:
                if quiet_after_error and time.time() - quiet_after_error > 1.5:
                    return turn
                continue
            except EOFError:
                turn.closed = ("eof", "")
                return turn
            except OSError as exc:
                turn.closed = ("oserror", repr(exc)[:80])
                return turn
            if kind == "close":
                turn.closed = val
                return turn
            try:
                ev = json.loads(val)
            except Exception:
                continue
            turn.events.append(ev)
            t = ev.get("type")
            turn.types.append(t)
            if t == "error" and turn.error is None:
                turn.error = ev
                quiet_after_error = time.time()
            if t == "response.completed":
                turn.completed_id = (ev.get("response") or {}).get("id")
                return turn
            if t == "response.failed":
                turn.failed = True
                return turn
        turn.timed_out = True
        return turn

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
