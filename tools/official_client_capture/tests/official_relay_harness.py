"""官方定向样本的中继集成测试夹具：起一个真实的 ``upstream_byte_relay.py`` 子进程，用 TLS 客户端按字节驱动。

为什么需要它
------------
定向样本的受控应答（流内故障、429／503 重试探针等）写在中继的连接处理器里，单元测试只能直接调用其中的辅助函数
或 WS 服务端方法；HTTP 层的分支（首个请求怎么应答、之后的请求怎么标记、同连接的多次重试怎么循环）只有经过真实的
TLS 连接、请求头解析和 relay.json 落盘才算被测到。官方客户端本身在本机跑不起来，这个夹具是离线能做到的最接近
真实链路的核对：中继进程、证书、SNI、h1 字节流、帧编码、relay.json 与干预日志都是真实的，只有「客户端」换成了
按需发字节的测试代码。

约束
----
* 只用标准库与 ``openssl`` 命令（生成一次性自签证书）；没有 openssl 时调用方应跳过。
* 中继固定连上游的 443 端口：需要「转发生产」的请求会连 ``--upstream-ip`` 指定的回环 443，这里没有服务监听，
  连接失败被中继记入 ``meta['error']``，不影响本夹具关心的字段（应答、标记、计时）。
* 每个实例独占一个随机端口与临时目录，退出时发 SIGTERM 让中继写 relay.json 后读回。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
RELAY_PY = TOOL_ROOT / "upstream_byte_relay.py"
RELAY_SCRIPT = TOOL_ROOT / "run_official_relay_scenario.sh"
OPENSSL = shutil.which("openssl")
WEBSOCKET_KEY = "dGhlIHNhbXBsZSBub25jZQ=="


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """一次性自签证书（CN 随意：客户端不校验）。"""

    cert, key = directory / "relay.crt", directory / "relay.key"
    subprocess.run(
        [str(OPENSSL), "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-subj", "/CN=chatgpt.com"],
        check=True, capture_output=True,
    )
    return cert, key


class RelayProcess:
    """真实中继子进程。用法：``with RelayProcess(cert, key, "--stream-fault", ...) as relay:``。"""

    def __init__(self, cert: Path, key: Path, *relay_args: str) -> None:
        self.cert, self.key, self.relay_args = cert, key, relay_args
        self.port = free_port()
        self.output: Path | None = None
        self._temp: tempfile.TemporaryDirectory | None = None
        self._process: subprocess.Popen | None = None
        self._stderr_path: Path | None = None
        self._stderr_handle = None

    def __enter__(self) -> "RelayProcess":
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.output = root / "relay-out"
        self._stderr_path = root / "relay.stderr"
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self._stderr_handle = self._stderr_path.open("wb")
        self._process = subprocess.Popen(
            [sys.executable, "-B", str(RELAY_PY),
             "--cert", str(self.cert), "--key", str(self.key), "--mode", "direct", "--port", str(self.port),
             "--upstream-host", "chatgpt.com", "--upstream-ip", "127.0.0.1",
             "--output", str(self.output), "--timeout", "120", *self.relay_args],
            stdout=subprocess.DEVNULL, stderr=self._stderr_handle, env=environment,
        )
        self._wait_until_listening()
        return self

    def _wait_until_listening(self) -> None:
        assert self._process is not None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"中继提前退出：{self.stderr_text()}")
            try:
                # 只做 TCP 探测（不握手）：中继会把它记成一条没有 TLS 的空连接，测试按字段选取记录，不依赖连接编号。
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"中继 15 秒内没有开始监听：{self.stderr_text()}")

    def stderr_text(self) -> str:
        if self._stderr_path is None or not self._stderr_path.exists():
            return ""
        return self._stderr_path.read_text(encoding="utf-8", errors="replace")

    def connect(self) -> ssl.SSLSocket:
        """一条新的 TLS 连接（SNI=chatgpt.com，不协商 ALPN，不校验证书）。"""

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        connection = context.wrap_socket(raw, server_hostname="chatgpt.com")
        connection.settimeout(10)
        return connection

    def stop(self) -> tuple[list[dict], list[dict]]:
        """发 SIGTERM 让中继写 relay.json，返回 (连接记录, 干预日志事件)。"""

        assert self._process is not None and self.output is not None
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        relay_json = self.output / "relay.json"
        connections = json.loads(relay_json.read_text(encoding="utf-8"))["connections"] if relay_json.exists() else []
        log = self.output / "intervention.jsonl"
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return connections, events

    def __exit__(self, *_exc: object) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.kill()
            self._process.wait(timeout=5)
        if self._stderr_handle is not None:
            self._stderr_handle.close()
        if self._temp is not None:
            self._temp.cleanup()


# ── 采集脚本里的核对块 ──────────────────────────────────────────────────────────────

def script_check_code(marker: str) -> str:
    """取出采集脚本里以 ``marker`` 开头的 heredoc 核对块的 Python 源码（原样，不改一个字）。

    marker 是核对块所在命令替换的起始文本（变量名、``$(docker exec -i "$capture_container" python3 -`` 与行尾续行符，
    再加换行），块以 ``<<'PY'`` 开始、以独占一行的 ``PY`` 结束。
    """

    source = RELAY_SCRIPT.read_text(encoding="utf-8")
    start = source.index(marker)
    body_start = source.index("<<'PY'\n", start) + len("<<'PY'\n")
    return source[body_start:source.index("\nPY\n", body_start) + 1]


def run_script_check(code: str, relay_json: Path, *arguments: str) -> tuple[int, dict]:
    """像采集脚本那样执行核对块：``python3 - <relay.json> <参数…>``，返回 (退出码, 标准输出里的 JSON 报告)。"""

    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(relay_json), *arguments],
        text=True, capture_output=True, check=False,
    )
    return result.returncode, json.loads(result.stdout)


# ── h1／WS 字节辅助 ─────────────────────────────────────────────────────────────

def read_until_close(connection: ssl.SSLSocket, *, limit: int = 1 << 20) -> bytes:
    """读到对端关闭（或超时）为止；中继的受控应答都带 ``connection: close``。"""

    data = b""
    while len(data) < limit:
        try:
            chunk = connection.recv(65536)
        except (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout):
            break
        if not chunk:
            break
        data += chunk
    return data


def read_http_response(connection: ssl.SSLSocket) -> tuple[bytes, bytes]:
    """读一个带 content-length 的 h1 响应，返回 (响应头, 响应体)；对端提前关闭则返回已读到的部分。"""

    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return data, b""
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = connection.recv(65536)
        if not chunk:
            break
        body += chunk
    return head, body[:length]


def post_responses(*, body: bytes = b'{"model":"gpt-5.5","input":[]}', extra_headers: str = "") -> bytes:
    return (
        b"POST /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\ncontent-type: application/json\r\n"
        + extra_headers.encode("ascii")
        + f"content-length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


def ws_upgrade_request() -> bytes:
    return (
        b"GET /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\nupgrade: websocket\r\n"
        b"connection: Upgrade\r\nsec-websocket-version: 13\r\n"
        + f"sec-websocket-key: {WEBSOCKET_KEY}\r\n\r\n".encode("ascii")
    )


def client_text_frame(message: dict) -> bytes:
    """客户端文本帧（掩码、不压缩：握手不提出 permessage-deflate，服务端不会协商它）。"""

    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    mask = b"\x0a\x0b\x0c\x0d"
    if len(payload) < 126:
        head = bytes((0x81, 0x80 | len(payload)))
    else:
        head = bytes((0x81, 0x80 | 126)) + struct.pack(">H", len(payload))
    return head + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def server_text_messages(data: bytes) -> list[dict]:
    """解析服务端（不掩码、不压缩）文本帧序列里的 JSON 消息；遇到控制帧或半帧即停。"""

    messages: list[dict] = []
    position = 0
    while position + 2 <= len(data):
        first, second = data[position], data[position + 1]
        length = second & 0x7F
        header = 2
        if length == 126:
            length = struct.unpack(">H", data[position + 2:position + 4])[0]
            header = 4
        elif length == 127:
            length = struct.unpack(">Q", data[position + 2:position + 10])[0]
            header = 10
        if position + header + length > len(data):
            break
        payload = data[position + header:position + header + length]
        position += header + length
        if first & 0x0F == 0x1:
            messages.append(json.loads(payload))
    return messages


def read_ws_messages_until(connection: ssl.SSLSocket, terminal_types: set[str]) -> list[dict]:
    """从升级响应之后读服务端帧，直到出现任一终态事件类型（或对端关闭／超时）。"""

    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            return []
        data += chunk
    _head, _, rest = data.partition(b"\r\n\r\n")
    messages: list[dict] = []
    while True:
        messages = server_text_messages(rest)
        if any(item.get("type") in terminal_types for item in messages):
            return messages
        try:
            chunk = connection.recv(65536)
        except (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout):
            return messages
        if not chunk:
            return messages
        rest += chunk


def sse_events(body: bytes) -> list[dict]:
    """解析受控 SSE 应答体（event 行 + data 行）里的 JSON 事件。"""

    events = []
    for block in body.decode("utf-8").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events
