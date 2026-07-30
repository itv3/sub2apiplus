"""候选网关 WebSocket 双轮驱动器的纯标准库协议测试。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from queue import Queue


TOOLS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(TOOLS_ROOT))

import drive_candidate_gateway_ws as driver  # noqa: E402


def receive_exact(stream: socket.socket, length: int) -> bytes:
    """从测试套接字精确读取指定长度。"""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise RuntimeError("测试连接提前关闭。")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_server_frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    """编码一条未掩码的服务端测试帧。"""

    first = opcode | (0x80 if fin else 0)
    length = len(payload)
    header = bytearray([first])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    header.extend(payload)
    return bytes(header)


def receive_client_frame(stream: socket.socket) -> tuple[int, bytes, bool]:
    """读取客户端帧，并返回 opcode、解掩码 payload 与 MASK 标志。"""

    first, second = receive_exact(stream, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    masked = bool(second & 0x80)
    if length == 126:
        length = struct.unpack("!H", receive_exact(stream, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(stream, 8))[0]
    mask = receive_exact(stream, 4) if masked else b""
    payload = receive_exact(stream, length)
    if mask:
        payload = bytes(
            byte ^ mask[index % len(mask)] for index, byte in enumerate(payload)
        )
    return opcode, payload, masked


class FakeGateway:
    """提供只接受一条 TCP 连接的严格双轮 WebSocket 测试服务。"""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.results: Queue[dict[str, object] | BaseException] = Queue()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        """启动后台测试服务。"""

        self.thread.start()

    def finish(self) -> dict[str, object]:
        """等待服务结束，并把后台异常重新抛给测试线程。"""

        self.thread.join(timeout=5)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("测试 WebSocket 服务未按时退出。")
        result = self.results.get_nowait()
        if isinstance(result, BaseException):
            raise result
        return result

    def _serve(self) -> None:
        """完成握手、两轮帧交换和掩码断言。"""

        try:
            stream, _ = self.listener.accept()
            stream.settimeout(5)
            with stream:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(stream.recv(4096))
                    if len(request) > 65536:
                        raise AssertionError("测试握手请求过大。")
                head = bytes(request).split(b"\r\n\r\n", 1)[0]
                lines = head.decode("latin-1").split("\r\n")
                if lines[0] != "GET /v1/responses HTTP/1.1":
                    raise AssertionError(f"握手路径错误：{lines[0]}")
                headers = {
                    name.strip().lower(): value.strip()
                    for name, value in (line.split(":", 1) for line in lines[1:])
                }
                required = {
                    "authorization": f"Bearer {self.api_key}",
                    "connection": "Upgrade",
                    "upgrade": "websocket",
                    "sec-websocket-version": "13",
                    "user-agent": "sub2apiplus-candidate-capture/1.0",
                    "originator": "sub2apiplus_candidate_capture",
                    "version": "0.145.0",
                    "x-session-affinity": "candidate-core-a06",
                }
                for name, expected in required.items():
                    if headers.get(name) != expected:
                        raise AssertionError(f"握手头 {name} 不匹配。")
                if "sec-websocket-extensions" in headers:
                    raise AssertionError("驱动器不应协商入站压缩。")
                key = headers["sec-websocket-key"]
                accept = base64.b64encode(
                    hashlib.sha1((key + driver.WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                response = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Connection: keep-alive, Upgrade\r\n"
                    "Upgrade: websocket\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
                # 101 与 PING 一次写出，验证驱动器保留握手后的尾随字节。
                stream.sendall(response + encode_server_frame(b"head-ping", 0x9))

                first_request: dict[str, object] | None = None
                pongs: list[bytes] = []
                while first_request is None or b"head-ping" not in pongs:
                    opcode, payload, masked = receive_client_frame(stream)
                    if not masked:
                        raise AssertionError("客户端帧必须设置 MASK。")
                    if opcode == 0xA:
                        pongs.append(payload)
                    elif opcode == 0x1:
                        first_request = json.loads(payload)
                    else:
                        raise AssertionError(f"首轮出现意外 opcode：{opcode}")
                if first_request.get("type") != "response.create":
                    raise AssertionError("首轮不是 response.create。")
                if "previous_response_id" in first_request:
                    raise AssertionError("首轮不得携带 previous_response_id。")
                if first_request.get("model") != "gpt-5.5":
                    raise AssertionError("首轮模型未保留。")

                created = json.dumps(
                    {
                        "type": "response.created",
                        "response": {"id": "resp_candidate_core_a06_0002"},
                    },
                    separators=(",", ":"),
                ).encode()
                completed = json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_candidate_core_a06_0002",
                            "output": [],
                        },
                    },
                    separators=(",", ":"),
                ).encode()
                split = len(completed) // 2
                stream.sendall(encode_server_frame(created))
                stream.sendall(encode_server_frame(completed[:split], 0x1, False))
                stream.sendall(encode_server_frame(b"fragment-ping", 0x9))
                stream.sendall(encode_server_frame(completed[split:], 0x0, True))

                second_request: dict[str, object] | None = None
                while second_request is None or b"fragment-ping" not in pongs:
                    opcode, payload, masked = receive_client_frame(stream)
                    if not masked:
                        raise AssertionError("续轮客户端帧必须设置 MASK。")
                    if opcode == 0xA:
                        pongs.append(payload)
                    elif opcode == 0x1:
                        second_request = json.loads(payload)
                    else:
                        raise AssertionError(f"续轮出现意外 opcode：{opcode}")
                if (
                    second_request.get("previous_response_id")
                    != "resp_candidate_core_a06_0002"
                ):
                    raise AssertionError("续轮未注入首轮真实 response.id。")
                second_input = second_request.get("input")
                if not isinstance(second_input, list) or len(second_input) != 2:
                    raise AssertionError("续轮业务前缀未保留并扩展。")

                second_completed = json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_candidate_core_a06_0003",
                            "output": [],
                            "padding": "x" * 180,
                        },
                    },
                    separators=(",", ":"),
                ).encode()
                stream.sendall(encode_server_frame(second_completed))
                close_opcode, close_payload, close_masked = receive_client_frame(stream)
                if close_opcode != 0x8 or close_payload != struct.pack("!H", 1000):
                    raise AssertionError("客户端未发送正常 CLOSE。")
                if not close_masked:
                    raise AssertionError("客户端 CLOSE 必须设置 MASK。")
                self.results.put(
                    {
                        "connection_count": 1,
                        "first_request": first_request,
                        "second_request": second_request,
                        "pongs": pongs,
                    }
                )
        except BaseException as error:
            self.results.put(error)


class CandidateGatewayWebSocketDriverTest(unittest.TestCase):
    def test_cli_runs_two_turns_on_one_connection_without_persisting_key(self) -> None:
        """真实套接字覆盖握手尾字节、分片、PING/PONG、续链与密钥门禁。"""

        api_key = "sk-candidate-ws-canary-never-persist"
        gateway = FakeGateway(api_key)
        gateway.start()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_body = root / "first.json"
            second_body = root / "second.json"
            first_output = root / "first.sse"
            second_output = root / "second.sse"
            summary = root / "summary.json"
            first_body.write_text(
                json.dumps(
                    {
                        "model": "gpt-5.5",
                        "input": [
                            {
                                "type": "additional_tools",
                                "role": "developer",
                                "tools": [{"type": "custom", "name": "exec"}],
                            },
                            {"type": "message", "role": "user", "content": "one"},
                        ],
                        "stream": True,
                    }
                ),
                encoding="utf-8",
            )
            second_body.write_text(
                json.dumps(
                    {
                        "model": "gpt-5.5",
                        "input": [
                            {"type": "message", "role": "user", "content": "one"},
                            {"type": "message", "role": "user", "content": "two"},
                        ],
                        "stream": True,
                    }
                ),
                encoding="utf-8",
            )
            metadata = json.dumps(
                {
                    "installation_id": "33333333-3333-4333-8333-333333333333",
                    "session_id": "11111111-1111-4111-8111-111111111111",
                    "thread_id": "11111111-1111-4111-8111-111111111111",
                    "turn_id": "22222222-2222-4222-8222-222222222222",
                    "window_id": "11111111-1111-4111-8111-111111111111:0",
                },
                separators=(",", ":"),
            )
            Path(str(first_body) + ".turn-metadata").write_text(
                metadata + "\n", encoding="utf-8"
            )
            Path(str(first_body) + ".thread-id").write_text(
                "11111111-1111-4111-8111-111111111111\n", encoding="utf-8"
            )
            Path(str(first_body) + ".window-id").write_text(
                "11111111-1111-4111-8111-111111111111:0\n", encoding="utf-8"
            )
            command = [
                sys.executable,
                str(TOOLS_ROOT / "drive_candidate_gateway_ws.py"),
                "--host",
                "127.0.0.1",
                "--port",
                str(gateway.port),
                "--first-body",
                str(first_body),
                "--second-body",
                str(second_body),
                "--first-output",
                str(first_output),
                "--second-output",
                str(second_output),
                "--summary",
                str(summary),
                "--timeout",
                "5",
            ]
            completed = subprocess.run(
                command,
                input=api_key,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
            server_result = gateway.finish()
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(server_result["connection_count"], 1)
            self.assertEqual(
                server_result["pongs"],
                [b"head-ping", b"fragment-ping"],
            )
            receipt = json.loads(summary.read_text(encoding="utf-8"))
            self.assertTrue(receipt["same_connection"])
            self.assertEqual(
                [turn["response_id"] for turn in receipt["turns"]],
                ["resp_candidate_core_a06_0002", "resp_candidate_core_a06_0003"],
            )
            self.assertIn("resp_candidate_core_a06_0002", first_output.read_text())
            self.assertIn("resp_candidate_core_a06_0003", second_output.read_text())
            for path in (first_output, second_output, summary):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            persisted = completed.stdout + completed.stderr
            persisted += "".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(api_key, persisted)
            self.assertNotIn(api_key, "\0".join(command))

    def test_client_frames_are_masked_across_length_boundaries(self) -> None:
        """覆盖短长度、16 位长度和 64 位长度编码。"""

        for length in (0, 125, 126, 65535, 65536):
            left, right = socket.socketpair()
            try:
                client = driver.GatewayWebSocket(left)
                payload = b"x" * length
                send_errors: Queue[BaseException] = Queue()

                def send() -> None:
                    try:
                        client.send_frame(0x1, payload)
                    except BaseException as error:
                        send_errors.put(error)

                sender = threading.Thread(target=send, daemon=True)
                sender.start()
                opcode, decoded, masked = receive_client_frame(right)
                sender.join(timeout=2)
                self.assertFalse(sender.is_alive(), "客户端帧发送线程未结束。")
                if not send_errors.empty():
                    raise send_errors.get_nowait()
                self.assertEqual(opcode, 0x1)
                self.assertTrue(masked)
                self.assertEqual(decoded, payload)
            finally:
                left.close()
                right.close()

    def test_templates_must_not_preseed_previous_response_id(self) -> None:
        """续链 ID 只能来自同一连接首轮成功终态。"""

        with tempfile.TemporaryDirectory() as temporary:
            body = Path(temporary) / "body.json"
            body.write_text(
                json.dumps(
                    {
                        "model": "gpt-5.5",
                        "input": [],
                        "previous_response_id": "resp_forged",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "不得预置"):
                driver._load_request_body(body, "测试")


if __name__ == "__main__":
    unittest.main()
