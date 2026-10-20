"""官方定向样本的中继集成测试：真实中继子进程 + TLS 客户端（见 official_relay_harness.py 的说明）。

单元测试（test_official_directed_samples.py）锁定事件形状、WS 服务端方法和采集脚本接线；这里补上它们之间的
那一层——连接处理器如何读请求头、选分支、写应答、落 relay.json 与干预日志。覆盖：

* 终止型流内故障（incomplete-terminal，0.163 起）：HTTP 的首个 POST 与 WS 的首条升级由中继受控应答；故障之后又出现
  Responses 请求（HTTP POST、WS 升级）时在连接元数据里留 post_terminal_request 标记，请求照常转发并完整记录。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests.official_relay_harness import (
    OPENSSL,
    RelayProcess,
    client_text_frame,
    make_certificate,
    post_responses,
    read_http_response,
    read_ws_messages_until,
    sse_events,
    ws_upgrade_request,
)


@unittest.skipUnless(OPENSSL, "需要 openssl 命令生成一次性自签证书")
class RelayFlowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._certificate_dir = tempfile.TemporaryDirectory()
        cls.cert, cls.key = make_certificate(Path(cls._certificate_dir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._certificate_dir.cleanup()

    def relay(self, *arguments: str) -> RelayProcess:
        return RelayProcess(self.cert, self.key, *arguments)

    @staticmethod
    def records(connections: list[dict], **fields: object) -> list[dict]:
        return [item for item in connections if all(item.get(key) == value for key, value in fields.items())]


class IncompleteTerminalRelayFlowTest(RelayFlowCase):
    def test_http_first_post_gets_the_terminal_incomplete_and_is_not_forwarded(self) -> None:
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                head, body = read_http_response(connection)
            connections, events = relay.stop()
        self.assertTrue(head.startswith(b"HTTP/1.1 200 OK\r\n"), head)
        self.assertIn(b"text/event-stream", head.lower())
        stream = sse_events(body)
        self.assertEqual([event["type"] for event in stream], ["response.created", "response.incomplete"])
        self.assertEqual(stream[1]["response"]["incomplete_details"], {"reason": "max_output_tokens"})
        faults = self.records(connections, intervention="stream_fault:incomplete-terminal:http")
        self.assertEqual(len(faults), 1)
        self.assertIs(faults[0]["valid"], True)
        self.assertIs(faults[0]["production_forwarded"], False)
        self.assertEqual(faults[0]["request_line"], "POST /backend-api/codex/responses HTTP/1.1")
        self.assertNotIn("post_terminal_request", faults[0])
        self.assertEqual([item["type"] for item in events], ["stream_fault"])
        self.assertEqual(events[0]["kind"], "incomplete-terminal")
        self.assertFalse(self.records(connections, post_terminal_request="http_post"))

    def test_http_second_post_after_the_fault_is_marked_and_still_recorded(self) -> None:
        """客户端若把终止当成可重试（0.160 的行为），第二个 POST 必须被标记，字节照常记录。"""

        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
            with relay.connect() as retry:
                retry.sendall(post_responses(body=b'{"model":"gpt-5.5","input":[{"retry":true}]}'))
                try:
                    retry.recv(65536)  # 转发生产会失败（回环 443 无监听），读到关闭即可
                except OSError:
                    pass
            connections, events = relay.stop()
        marked = self.records(connections, post_terminal_request="http_post")
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["request_line"], "POST /backend-api/codex/responses HTTP/1.1")
        self.assertNotIn("production_forwarded", marked[0], "偏离的请求走普通转发路径，不是受控应答")
        self.assertEqual(
            [item["type"] for item in events], ["stream_fault", "stream_fault_post_terminal_request"]
        )
        self.assertEqual(events[1]["request"], "http_post")

    def test_other_requests_after_the_fault_are_not_marked(self) -> None:
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
            with relay.connect() as other:
                other.sendall(b"GET /backend-api/wham/accounts/check HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n")
                try:
                    other.recv(65536)
                except OSError:
                    pass
            connections, _events = relay.stop()
        self.assertFalse([item for item in connections if item.get("post_terminal_request")])

    def test_ws_first_upgrade_is_synthesized_and_a_second_upgrade_is_marked(self) -> None:
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "ws") as relay:
            with relay.connect() as connection:
                connection.sendall(ws_upgrade_request())
                connection.sendall(client_text_frame({"type": "response.create", "generate": False}))
                connection.sendall(client_text_frame({"type": "response.create", "input": []}))
                messages = read_ws_messages_until(connection, {"response.incomplete", "response.failed"})
            with relay.connect() as retry:
                retry.sendall(ws_upgrade_request())
                try:
                    retry.recv(65536)
                except OSError:
                    pass
            connections, events = relay.stop()
        self.assertEqual(
            [message["type"] for message in messages],
            ["response.created", "response.completed", "response.created", "response.incomplete"],
        )
        self.assertEqual(messages[-1]["response"]["incomplete_details"], {"reason": "max_output_tokens"})
        faults = self.records(connections, intervention="stream_fault:incomplete-terminal:ws")
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["stream_fault_ws_actions"], ["warmup_completed", "stream_fault_incomplete-terminal"])
        self.assertIs(faults[0]["production_forwarded"], False)
        self.assertNotIn("post_terminal_request", faults[0])
        marked = self.records(connections, post_terminal_request="ws_upgrade")
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["request_line"], "GET /backend-api/codex/responses HTTP/1.1")
        self.assertIn("stream_fault_post_terminal_request", [item["type"] for item in events])

    def test_ws_generate_request_after_the_fault_marks_the_synthesized_connection(self) -> None:
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "ws") as relay:
            with relay.connect() as connection:
                connection.sendall(ws_upgrade_request())
                connection.sendall(client_text_frame({"type": "response.create", "input": []}))
                connection.sendall(client_text_frame({"type": "response.create", "input": [], "previous_response_id": "x"}))
                messages = read_ws_messages_until(connection, {"response.completed"})
            connections, _events = relay.stop()
        self.assertEqual(messages[-1]["type"], "response.completed")
        faults = self.records(connections, intervention="stream_fault:incomplete-terminal:ws")
        self.assertEqual(len(faults), 1)
        self.assertEqual(faults[0]["post_terminal_request"], "ws_generate")


if __name__ == "__main__":
    unittest.main()
