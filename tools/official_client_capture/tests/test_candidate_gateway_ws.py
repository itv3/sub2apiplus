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
                    "version": "0.147.0",
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
    def test_invalid_campaign_version_fails_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            payload = {"model": "gpt-5.5", "input": []}
            first.write_text(json.dumps(payload), encoding="utf-8")
            second.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "完整的 x.y.z 版本"):
                driver.run_session(
                    host="127.0.0.1",
                    port=1,
                    path="/v1/responses",
                    api_key="unit-test-key",
                    codex_version="",
                    first_body_path=first,
                    second_body_path=second,
                    first_output_path=root / "first.sse",
                    second_output_path=root / "second.sse",
                    summary_path=root / "summary.json",
                    session_affinity="candidate-version-test",
                    timeout=1,
                )

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
                "--codex-version",
                "0.147.0",
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
            self.assertEqual(receipt["codex_version"], "0.147.0")
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


class PlanGateway:
    """计划模式的测试网关：一条连接，按序接收 response.create，逐帧回受控 id（0001 视为网关预热，业务帧从 0002 起）。

    收到含 compaction_trigger 的帧时回一个 compaction 输出项（同 A21 的受控上游）。``skip_ids`` 用来模拟网关多补了
    一个预热（id 序列出现缺口）。"""

    def __init__(self, prefix: str, *, skip_ids: bool = False, drop_compaction: bool = False) -> None:
        self.prefix = prefix
        self.skip_ids = skip_ids
        self.drop_compaction = drop_compaction
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.port = self.listener.getsockname()[1]
        self.handshake: dict[str, str] = {}
        self.frames: list[dict[str, object]] = []
        self.connections = 0
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> None:
        self.thread.join(timeout=8)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError("测试网关未按时退出。")

    def _serve(self) -> None:
        try:
            stream, _ = self.listener.accept()
            self.connections += 1
            stream.settimeout(5)
            with stream:
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    request.extend(stream.recv(4096))
                lines = bytes(request).split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
                self.handshake = {
                    name.strip().lower(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:])
                }
                accept = base64.b64encode(
                    hashlib.sha1((self.handshake["sec-websocket-key"] + driver.WEBSOCKET_GUID).encode("ascii")).digest()
                ).decode("ascii")
                stream.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode("ascii")
                )
                ordinal = 1  # 0001 是网关预热，不经过驱动
                while True:
                    try:
                        opcode, payload, _ = receive_client_frame(stream)
                    except (RuntimeError, OSError):
                        return
                    if opcode == 0x8:
                        return
                    if opcode != 0x1:
                        continue
                    frame = json.loads(payload)
                    self.frames.append(frame)
                    ordinal += 1
                    if self.skip_ids and len(self.frames) == 2:
                        ordinal += 1
                    response_id = f"{self.prefix}{ordinal:04d}"
                    items: list[dict[str, object]] = []
                    triggers = [item for item in frame.get("input", []) if item.get("type") == "compaction_trigger"]
                    if triggers and not self.drop_compaction:
                        items.append({"type": "compaction", "id": f"cmp_{ordinal:04d}", "encrypted_content": "opaque"})
                    events = [{"type": "response.created", "response": {"id": response_id}}]
                    for item in items:
                        events.append({"type": "response.output_item.done", "item": item})
                    events.append({"type": "response.completed", "response": {"id": response_id, "output": items}})
                    for event in events:
                        stream.sendall(encode_server_frame(json.dumps(event, separators=(",", ":")).encode()))
        except BaseException as error:  # noqa: BLE001 - 交给测试线程断言
            self.error = error


def _run_plan(plan: str, prefix: str, root: Path, **gateway_options: object) -> tuple[subprocess.CompletedProcess, PlanGateway]:
    gateway = PlanGateway(prefix, **gateway_options)  # type: ignore[arg-type]
    gateway.start()
    output_dir = root / "plan"
    output_dir.mkdir()
    command = [
        sys.executable, str(TOOLS_ROOT / "drive_candidate_gateway_ws.py"),
        "--host", "127.0.0.1", "--port", str(gateway.port), "--codex-version", "0.165.0",
        "--plan", plan, "--main-model", "gpt-6.1-sol", "--second-model", "gpt-6-astra",
        "--no-update-model", "gpt-5.6-sol", "--plan-output-dir", str(output_dir),
        "--expected-id-prefix", prefix, "--summary", str(root / "summary.json"),
        "--session-affinity", "candidate-core-test", "--timeout", "5",
    ]
    completed = subprocess.run(command, input="sk-test-plan-key", text=True, capture_output=True, check=False, timeout=15)
    gateway.finish()
    return completed, gateway


class PlanModeTests(unittest.TestCase):
    def test_build_plan_shapes(self) -> None:
        handshake, steps = driver.build_plan("model-switch", main_model="gpt-6.1-sol", second_model="gpt-6-astra")
        self.assertEqual([s.body["model"] for s in steps], ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-sol"])
        self.assertFalse(any(s.chain for s in steps), "官方换模型的整段重放帧不续链")
        for step in steps:
            self.assertNotIn("previous_response_id", step.body)
            self.assertNotIn("generate", step.body)
            self.assertEqual(step.body["input"][0]["type"], "additional_tools", "整段重放里载体仍在 input 最前")
        self.assertEqual(handshake.window_id, handshake.session_id + ":0")
        self.assertEqual(json.loads(handshake.turn_metadata)["window_number"], 0)
        # 每帧回合 id 互不相同，窗口号恒 0
        turn_ids = [s.body["client_metadata"]["turn_id"] for s in steps]
        self.assertEqual(len(set(turn_ids)), 3)
        self.assertEqual({json.loads(s.body["client_metadata"]["x-codex-turn-metadata"])["window_number"] for s in steps}, {0})

    def test_compaction_plan_advances_window_only_after_trigger(self) -> None:
        _, steps = driver.build_plan("compact-continue", main_model="gpt-6.1-sol")
        self.assertEqual([s.label for s in steps], ["turn-1", "turn-2", "compaction", "replay-after-compaction", "turn-after-replay"])
        windows = [json.loads(s.body["client_metadata"]["x-codex-turn-metadata"])["window_number"] for s in steps]
        self.assertEqual(windows, [0, 0, 0, 1, 1])
        for step, window in zip(steps, windows):
            self.assertTrue(step.body["client_metadata"]["x-codex-window-id"].endswith(f":{window}"))
        self.assertEqual([s.chain for s in steps], [False, True, True, False, True])
        self.assertEqual([s.request_kind for s in steps], ["turn", "turn", "compaction", "turn", "turn"])
        self.assertEqual(steps[2].body["input"], [{"type": "compaction_trigger"}])
        marker = driver.PLAN_COMPACTION_MARKER
        replay_marks = [item for item in steps[3].body["input"] if marker in item]
        self.assertEqual(len(replay_marks), 1, "整段重放帧恰有一个压缩项占位，且位于新用户消息之前")
        self.assertEqual(steps[3].body["input"].index(replay_marks[0]), len(steps[3].body["input"]) - 2)

    def test_effort_change_plan_changes_only_effort(self) -> None:
        _, steps = driver.build_plan("effort-change", main_model="x", no_update_model="gpt-5.6-sol")
        self.assertEqual([s.body["reasoning"]["effort"] for s in steps], ["low", "high"])
        self.assertEqual({s.body["model"] for s in steps}, {"gpt-5.6-sol"})
        self.assertFalse(any(s.chain for s in steps))

    def test_plan_argument_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "互不相同"):
            driver.build_plan("model-switch", main_model="a", second_model="a")
        with self.assertRaisesRegex(ValueError, "no-update-model"):
            driver.build_plan("effort-change", main_model="a")
        with self.assertRaisesRegex(ValueError, "只能包含"):
            driver.build_plan("compact-continue", main_model="a b")
        with self.assertRaisesRegex(ValueError, "未知计划"):
            driver.build_plan("nope", main_model="a")

    def test_model_switch_session_runs_on_one_connection_with_consecutive_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed, gateway = _run_plan("model-switch", "resp_candidate_core_a20_", root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(gateway.connections, 1)
            self.assertIsNone(gateway.error)
            self.assertEqual([f["model"] for f in gateway.frames], ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-sol"])
            self.assertTrue(all("previous_response_id" not in f for f in gateway.frames))
            # 握手身份来自计划自己的会话，而不是 A06 的固定会话
            self.assertEqual(gateway.handshake["session-id"], driver.PLAN_SESSION_IDS["model-switch"])
            self.assertEqual(gateway.handshake["x-codex-window-id"], driver.PLAN_SESSION_IDS["model-switch"] + ":0")
            receipt = json.loads((root / "summary.json").read_text())
            self.assertEqual(receipt["schema_version"], "candidate-gateway-ws-plan/v1")
            self.assertEqual([t["response_id"] for t in receipt["turns"]],
                             [f"resp_candidate_core_a20_{n:04d}" for n in (2, 3, 4)])
            self.assertEqual(sorted(p.name for p in (root / "plan").iterdir()),
                             ["switch-back.sse", "switch-to-second.sse", "turn-1.sse"])

    def test_compaction_session_replays_the_upstream_compaction_item_and_advances_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed, gateway = _run_plan("compact-continue", "resp_candidate_core_a21_", root)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(gateway.connections, 1)
            frames = gateway.frames
            self.assertEqual(len(frames), 5)
            self.assertEqual(frames[1]["previous_response_id"], "resp_candidate_core_a21_0002")
            self.assertEqual(frames[2]["previous_response_id"], "resp_candidate_core_a21_0003")
            self.assertEqual(frames[2]["input"], [{"type": "compaction_trigger"}])
            self.assertNotIn("previous_response_id", frames[3])
            replay_types = [item["type"] for item in frames[3]["input"]]
            self.assertEqual(replay_types[0], "additional_tools")
            self.assertEqual(replay_types.count("compaction"), 1)
            self.assertEqual(replay_types.index("compaction"), len(replay_types) - 2)
            compaction = next(item for item in frames[3]["input"] if item["type"] == "compaction")
            self.assertEqual(compaction["id"], "cmp_0004", "替换进来的是上游在压缩帧响应里真实回的那一项")
            self.assertTrue(frames[3]["client_metadata"]["x-codex-window-id"].endswith(":1"))
            self.assertEqual(frames[4]["previous_response_id"], "resp_candidate_core_a21_0005")
            self.assertTrue(frames[4]["client_metadata"]["x-codex-window-id"].endswith(":1"))
            self.assertNotIn("previous_response_id", frames[0])
            # 握手仍是建连时的窗口 0（复用连接不重发握手）
            self.assertTrue(gateway.handshake["x-codex-window-id"].endswith(":0"))

    def test_effort_change_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed, gateway = _run_plan("effort-change", "resp_candidate_core_a22_", Path(temporary))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual([f["reasoning"]["effort"] for f in gateway.frames], ["low", "high"])
            self.assertTrue(all("previous_response_id" not in f for f in gateway.frames))

    def test_id_gap_means_the_gateway_added_a_frame_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed, _ = _run_plan("model-switch", "resp_candidate_core_a20_", Path(temporary), skip_ids=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("多发或少发了上游帧", completed.stderr)
            self.assertFalse((Path(temporary) / "summary.json").exists(), "失败时不留成功摘要")

    def test_missing_compaction_item_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completed, _ = _run_plan("compact-continue", "resp_candidate_core_a21_", Path(temporary), drop_compaction=True)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("compaction 输出项", completed.stderr)

    def test_plan_and_two_turn_arguments_are_exclusive(self) -> None:
        base = [sys.executable, str(TOOLS_ROOT / "drive_candidate_gateway_ws.py"), "--port", "1", "--codex-version", "0.165.0",
                "--summary", "/nonexistent/summary.json"]
        mixed = subprocess.run(base + ["--plan", "model-switch", "--first-body", "/x"], input="k", text=True,
                               capture_output=True, check=False)
        self.assertEqual(mixed.returncode, 2)
        self.assertIn("不能与", mixed.stderr)
        missing = subprocess.run(base + ["--plan", "model-switch"], input="k", text=True, capture_output=True, check=False)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--plan-output-dir", missing.stderr)
        two_turn_missing = subprocess.run(base, input="k", text=True, capture_output=True, check=False)
        self.assertEqual(two_turn_missing.returncode, 2)
        self.assertIn("双轮模式必须给", two_turn_missing.stderr)


if __name__ == "__main__":
    unittest.main()
