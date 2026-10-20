"""官方定向样本的中继集成测试：真实中继子进程 + TLS 客户端（见 official_relay_harness.py 的说明）。

单元测试（test_official_directed_samples.py）锁定事件形状、WS 服务端方法和采集脚本接线；这里补上它们之间的
那一层——连接处理器如何读请求头、选分支、写应答、落 relay.json 与干预日志。覆盖：

* 终止型流内故障（incomplete-terminal，0.163 起）：HTTP 的首个 POST 与 WS 的首条升级由中继受控应答；故障之后又出现
  Responses 请求（HTTP POST、WS 升级）时在连接元数据里留 post_terminal_request 标记，请求照常转发并完整记录。
* WS 升级 429（--ws-upgrade-429-retry-after，0.163 起）：每个 Responses WS 升级都回 429＋Retry-After，升级时间线记入
  ws_upgrade_429；之后到达的 Responses POST（降级）记入 ws_upgrade_429_fallback_post，请求照常转发。
* 持续过载 503（--retry-probe persistent-503-overloaded，0.163 起）：每个 Responses POST 都回 keep-alive 503
  （server_is_overloaded，可附 Retry-After），不转发生产；逐请求的到达／应答时刻记入 overloaded_503_attempts。
  同时回归既有的 keepalive-500 模式（动作命名与计时没有被新模式改坏）。
"""

from __future__ import annotations

import json
import tempfile
import time
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
    run_script_check,
    script_check_code,
    sse_events,
    ws_upgrade_request,
)

# 采集脚本里三个核对块的定位文本（命令替换的起始行）：下面的「脚本核对块读真实中继产出」测试用它们取出脚本里的 Python，
# 直接读真实中继写出的 relay.json——证明脚本读取的字段名与中继写出的字段名对得上，而不是各写各的。
TERMINAL_CHECK = 'terminal_check=$(docker exec -i "$capture_container" python3 - \\\n'
WS_RETRY_AFTER_CHECK = 'ws_retry_after_report=$(docker exec -i "$capture_container" python3 - \\\n'
OVERLOADED_CHECK = 'overloaded_report=$(docker exec -i "$capture_container" python3 - \\\n'


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


POST_LINE = "POST /backend-api/codex/responses HTTP/1.1"


class WsUpgrade429RelayFlowTest(RelayFlowCase):
    def test_every_responses_ws_upgrade_gets_a_429_with_retry_after_and_a_timeline(self) -> None:
        heads = []
        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            for _ in range(3):
                with relay.connect() as connection:
                    connection.sendall(ws_upgrade_request())
                    head, body = read_http_response(connection)
                heads.append((head, body))
                time.sleep(0.15)
            connections, events = relay.stop()
        for head, body in heads:
            self.assertTrue(head.startswith(b"HTTP/1.1 429 Too Many Requests\r\n"), head)
            self.assertIn(b"retry-after: 2\r\n", head)
            self.assertIn(b"connection: close", head)  # read_http_response 返回的响应头不含结尾空行
            self.assertEqual(body, b"")
        upgrades = sorted(
            self.records(connections, intervention="ws_upgrade_429"), key=lambda item: item["ws_upgrade_429"]["sequence"]
        )
        self.assertEqual([item["ws_upgrade_429"]["sequence"] for item in upgrades], [1, 2, 3])
        for item in upgrades:
            self.assertIs(item["valid"], True)
            self.assertIs(item["production_forwarded"], False)
            self.assertEqual(item["request_line"], "GET /backend-api/codex/responses HTTP/1.1")
            timeline = item["ws_upgrade_429"]
            self.assertEqual(timeline["retry_after_seconds"], 2)
            self.assertLessEqual(timeline["request_head_received_unix_ms"], timeline["response_sent_unix_ms"])
            # 双向字节都在：升级请求头与受控 429 都进了该连接的记录。
            self.assertGreater(item["bytes"]["client_to_upstream"], 0)
            self.assertGreater(item["bytes"]["upstream_to_client"], 0)
        # 相邻两次升级尝试：后一次的请求头读到不早于前一次 429 写完，间隔不小于客户端实际停顿（0.15 s，留抖动余量）。
        for before, after in zip(upgrades, upgrades[1:]):
            gap = after["ws_upgrade_429"]["request_head_received_unix_ms"] - before["ws_upgrade_429"]["response_sent_unix_ms"]
            self.assertGreaterEqual(gap, 100)
        self.assertEqual([item["type"] for item in events], ["ws_upgrade_429"] * 3)
        self.assertEqual([item["sequence"] for item in events], [1, 2, 3])

    def test_the_post_after_the_429s_is_recorded_as_the_fallback_with_its_wait(self) -> None:
        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            for _ in range(2):
                with relay.connect() as connection:
                    connection.sendall(ws_upgrade_request())
                    read_http_response(connection)
            time.sleep(0.3)
            with relay.connect() as fallback:
                fallback.sendall(post_responses())
                try:
                    fallback.recv(65536)  # 转发生产会失败（回环 443 无监听），读到关闭即可
                except OSError:
                    pass
            connections, events = relay.stop()
        marked = [item for item in connections if "ws_upgrade_429_fallback_post" in item]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["request_line"], POST_LINE)
        self.assertNotIn("production_forwarded", marked[0], "降级 POST 走普通转发路径，不是受控应答")
        timeline = marked[0]["ws_upgrade_429_fallback_post"]
        last_upgrade = max(
            (item["ws_upgrade_429"] for item in connections if "ws_upgrade_429" in item),
            key=lambda entry: entry["sequence"],
        )
        self.assertEqual(timeline["ws_upgrade_429_count"], 2)
        self.assertEqual(timeline["retry_after_seconds"], 2)
        self.assertEqual(timeline["last_ws_upgrade_429_sent_unix_ms"], last_upgrade["response_sent_unix_ms"])
        self.assertEqual(
            timeline["since_last_ws_upgrade_429_ms"],
            timeline["request_head_received_unix_ms"] - last_upgrade["response_sent_unix_ms"],
        )
        self.assertGreaterEqual(timeline["since_last_ws_upgrade_429_ms"], 250, "客户端实际停顿 0.3 s")
        self.assertEqual(
            [item["type"] for item in events],
            ["ws_upgrade_429", "ws_upgrade_429", "ws_upgrade_429_fallback_post"],
        )

    def test_a_post_before_any_429_is_not_marked_and_other_requests_are_untouched(self) -> None:
        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            for request in (
                post_responses(),
                b"GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n",
            ):
                with relay.connect() as connection:
                    connection.sendall(request)
                    try:
                        connection.recv(65536)
                    except OSError:
                        pass
            connections, events = relay.stop()
        self.assertFalse([item for item in connections if "ws_upgrade_429_fallback_post" in item or "ws_upgrade_429" in item])
        self.assertEqual(events, [])

    def test_a_non_websocket_get_to_the_responses_path_is_not_answered_with_a_429(self) -> None:
        """只有带 Upgrade: websocket 的 Responses 升级才回 429；同一路径的普通 GET 照常转发。"""

        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            with relay.connect() as connection:
                connection.sendall(
                    b"GET /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n"
                )
                try:
                    connection.recv(65536)
                except OSError:
                    pass
            connections, _events = relay.stop()
        self.assertFalse(self.records(connections, intervention="ws_upgrade_429"))


class PersistentOverloadedRelayFlowTest(RelayFlowCase):
    ARGS = ("--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses")

    def test_every_post_on_one_keepalive_connection_gets_a_503_and_nothing_is_forwarded(self) -> None:
        responses = []
        with self.relay(*self.ARGS) as relay:
            with relay.connect() as connection:
                for index in range(3):
                    connection.sendall(post_responses(body=b'{"model":"gpt-5.5","input":[{"attempt":%d}]}' % index))
                    responses.append(read_http_response(connection))
                    time.sleep(0.05)
            connections, events = relay.stop()
        for head, body in responses:
            self.assertTrue(head.startswith(b"HTTP/1.1 503 Service Unavailable\r\n"), head)
            self.assertNotIn(b"retry-after", head.lower())
            self.assertIn(b"connection: keep-alive", head)
            self.assertEqual(json.loads(body), {"error": {"code": "server_is_overloaded"}})
        served = self.records(connections, intervention="conn_retry_probe:persistent-503-overloaded")
        self.assertEqual(len(served), 1, "三个请求复用同一条连接")
        record = served[0]
        self.assertIs(record["valid"], True)
        self.assertIs(record["production_forwarded"], False)
        self.assertEqual(record["overloaded_503_end"], "client_closed")
        attempts = record["overloaded_503_attempts"]
        self.assertEqual([item["attempt"] for item in attempts], [1, 2, 3])
        self.assertEqual({item["request_line"] for item in attempts}, {POST_LINE})
        self.assertEqual({item["retry_after_seconds"] for item in attempts}, {None})
        self.assertEqual([item["attempt"] for item in record["retry_probe_attempts"]], [1, 2, 3])
        # 三个请求头与三个 503 都在该连接的记录里（请求体也记了：字节数 > 3 个请求头之和）。
        self.assertGreater(record["bytes"]["client_to_upstream"], 3 * len(post_responses()) - 100)
        self.assertEqual([item["type"] for item in events if item["type"] == "overloaded_503_served"], ["overloaded_503_served"] * 3)
        actions = [item["action"] for item in events if item["type"] == "conn_retry_probe"]
        self.assertEqual(actions, ["synthetic_503_server_is_overloaded"] * 3, "没有任何转发生产的 attempt")

    def test_retry_after_header_follows_the_switch(self) -> None:
        with self.relay(*self.ARGS, "--retry-probe-retry-after", "2") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                head, body = read_http_response(connection)
            connections, _events = relay.stop()
        self.assertIn(b"retry-after: 2\r\n", head)
        self.assertEqual(json.loads(body), {"error": {"code": "server_is_overloaded"}})
        record = self.records(connections, intervention="conn_retry_probe:persistent-503-overloaded")[0]
        self.assertEqual(record["overloaded_503_attempts"][0]["retry_after_seconds"], 2)

    def test_timeline_records_the_wait_between_requests(self) -> None:
        with self.relay(*self.ARGS, "--retry-probe-retry-after", "2") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
                time.sleep(0.4)  # 模拟客户端等待 Retry-After 后重试
                connection.sendall(post_responses())
                read_http_response(connection)
            connections, _events = relay.stop()
        first, second = self.records(connections, intervention="conn_retry_probe:persistent-503-overloaded")[0][
            "overloaded_503_attempts"
        ]
        self.assertLessEqual(first["request_head_received_unix_ms"], first["response_sent_unix_ms"])
        gap = second["request_head_received_unix_ms"] - first["response_sent_unix_ms"]
        self.assertGreaterEqual(gap, 350)
        self.assertLess(gap, 3_000)

    def test_a_retry_on_a_new_connection_is_answered_the_same_way_and_never_forwarded(self) -> None:
        with self.relay(*self.ARGS) as relay:
            for _ in range(2):
                with relay.connect() as connection:
                    connection.sendall(post_responses())
                    head, _body = read_http_response(connection)
                    self.assertTrue(head.startswith(b"HTTP/1.1 503 Service Unavailable\r\n"), head)
            connections, events = relay.stop()
        served = sorted(
            self.records(connections, intervention="conn_retry_probe:persistent-503-overloaded"),
            key=lambda item: item["overloaded_503_attempts"][0]["attempt"],
        )
        self.assertEqual([item["overloaded_503_attempts"][0]["attempt"] for item in served], [1, 2], "全局 attempt 跨连接递增")
        self.assertTrue(all(item.get("production_forwarded") is False for item in served))
        self.assertEqual(
            [item["action"] for item in events if item["type"] == "conn_retry_probe"],
            ["synthetic_503_server_is_overloaded"] * 2,
        )

    def test_a_non_target_request_on_the_connection_marks_it_invalid(self) -> None:
        with self.relay(*self.ARGS) as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
                connection.sendall(b"GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n")
                try:
                    connection.recv(65536)
                except OSError:
                    pass
            connections, _events = relay.stop()
        record = self.records(connections, overloaded_503_end="unexpected_request")[0]
        self.assertIs(record["valid"], False)
        self.assertIn("非目标请求", record["error"])
        self.assertEqual(len(record["overloaded_503_attempts"]), 1)

    def test_other_requests_are_forwarded_as_usual_and_not_counted(self) -> None:
        """models 等其它请求不属于目标：不回 503、不占 attempt 编号。"""

        with self.relay(*self.ARGS) as relay:
            with relay.connect() as connection:
                connection.sendall(b"GET /backend-api/codex/models?client_version=0.163.0 HTTP/1.1\r\nhost: chatgpt.com\r\n\r\n")
                try:
                    connection.recv(65536)
                except OSError:
                    pass
            with relay.connect() as connection:
                connection.sendall(post_responses())
                head, _body = read_http_response(connection)
            connections, _events = relay.stop()
        self.assertTrue(head.startswith(b"HTTP/1.1 503 Service Unavailable\r\n"), head)
        record = self.records(connections, intervention="conn_retry_probe:persistent-503-overloaded")[0]
        self.assertEqual(record["overloaded_503_attempts"][0]["attempt"], 1)


class KeepaliveFiveHundredRegressionTest(RelayFlowCase):
    """既有 keepalive-500 模式不受新增持续过载模式影响：首个目标请求回 500 并保持连接，同连接的重试转发生产。"""

    def test_first_post_gets_a_500_and_the_retry_on_the_same_connection_is_forwarded(self) -> None:
        with self.relay("--retry-probe", "keepalive-500", "--retry-probe-target", "responses",
                        "--retry-probe-retry-after", "2") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                head, body = read_http_response(connection)
                time.sleep(0.3)
                connection.sendall(post_responses())
                try:
                    connection.recv(65536)  # 转发生产会失败（回环 443 无监听）
                except OSError:
                    pass
            connections, events = relay.stop()
        self.assertTrue(head.startswith(b"HTTP/1.1 500 Internal Server Error\r\n"), head)
        self.assertIn(b"retry-after: 2\r\n", head)
        self.assertEqual(body, b"")
        record = self.records(connections, intervention="conn_retry_probe:keepalive-500")[0]
        self.assertEqual([item["attempt"] for item in record["retry_probe_attempts"]], [1, 2])
        timing = record["retry_probe_timing"]
        self.assertEqual(timing["retry_after_seconds"], 2)
        self.assertGreaterEqual(timing["retry_head_received_unix_ms"] - timing["synthetic_500_sent_unix_ms"], 250)
        self.assertNotIn("overloaded_503_attempts", record)
        self.assertEqual(
            [item["action"] for item in events if item["type"] == "conn_retry_probe"],
            ["synthetic_500_keepalive", "forward_to_production"],
        )


class ScriptChecksReadRealRelayOutputTest(RelayFlowCase):
    """采集脚本的核对块直接读真实中继写出的 relay.json（字段名对得上，不是手写的假数据）。"""

    def test_terminal_fault_check_over_a_real_http_run(self) -> None:
        code = script_check_code(TERMINAL_CHECK)
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http") as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "http", "1")
        self.assertEqual((status, report["status"], report["failures"]), (0, "passed", []))
        self.assertEqual(report["post_terminal_requests"], [])
        # 客户端把终止当成可重试：第二个 POST 被中继标记，核对失败关闭。
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "http") as relay:
            for _ in range(2):
                with relay.connect() as connection:
                    connection.sendall(post_responses())
                    try:
                        connection.recv(65536)
                    except OSError:
                        pass
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "http", "1")
        self.assertEqual((status, report["failures"]), (1, ["responses-request-after-terminal-fault"]))

    def test_terminal_fault_check_over_a_real_ws_run(self) -> None:
        code = script_check_code(TERMINAL_CHECK)
        with self.relay("--stream-fault", "incomplete-terminal", "--stream-fault-transport", "ws") as relay:
            with relay.connect() as connection:
                connection.sendall(ws_upgrade_request())
                connection.sendall(client_text_frame({"type": "response.create", "input": []}))
                read_ws_messages_until(connection, {"response.incomplete", "response.failed"})
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "ws", "1")
        self.assertEqual((status, report["status"], report["failures"]), (0, "passed", []))

    def test_ws_upgrade_429_check_over_a_real_run(self) -> None:
        code = script_check_code(WS_RETRY_AFTER_CHECK)
        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            for _ in range(3):
                with relay.connect() as connection:
                    connection.sendall(ws_upgrade_request())
                    read_http_response(connection)
                time.sleep(0.1)
            time.sleep(0.2)
            with relay.connect() as fallback:
                fallback.sendall(post_responses())
                try:
                    fallback.recv(65536)
                except OSError:
                    pass
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "2", "0")
        self.assertEqual((status, report["status"], report["failures"]), (0, "passed", []))
        self.assertEqual((report["upgrade_attempt_count"], report["http_fallback_post_count"]), (3, 1))
        self.assertEqual(len(report["gaps_between_upgrade_attempts_ms"]), 2)
        self.assertGreaterEqual(report["fallback_since_last_upgrade_429_ms"], 150)
        # 客户端只停了 0.2 s，远不到 Retry-After：只记事实，不判成败。
        self.assertIs(report["fallback_waited_retry_after"], False)
        # 没有降级 POST（客户端放弃或终止）：核对失败关闭。
        with self.relay("--ws-upgrade-429-retry-after", "2") as relay:
            with relay.connect() as connection:
                connection.sendall(ws_upgrade_request())
                read_http_response(connection)
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "2", "1")
        self.assertEqual((status, report["failures"]), (1, ["no-http-fallback-post-after-429"]))

    def test_overloaded_timeline_check_over_a_real_run(self) -> None:
        code = script_check_code(OVERLOADED_CHECK)
        arguments = ("--retry-probe", "persistent-503-overloaded", "--retry-probe-target", "responses")
        with self.relay(*arguments, "--retry-probe-retry-after", "2") as relay:
            with relay.connect() as connection:  # 一轮内复用同一条连接
                for _ in range(3):
                    connection.sendall(post_responses())
                    read_http_response(connection)
                    time.sleep(0.05)
            with relay.connect() as connection:  # 轮间换新连接
                for _ in range(2):
                    connection.sendall(post_responses())
                    read_http_response(connection)
                    time.sleep(0.05)
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "2", "1")
        self.assertEqual((status, report["status"], report["failures"]), (0, "passed", []))
        self.assertEqual(report["request_count"], 5)
        self.assertEqual(len(report["connection_ids"]), 2)
        self.assertEqual(len(report["gaps_ms"]), 4)
        # 客户端只停了几十毫秒：间隔远小于 Retry-After，只记事实。
        self.assertIs(report["all_gaps_at_least_retry_after"], False)
        self.assertEqual(report["retry_after_seconds"], 2)
        # 对照：不带 Retry-After 时没有间隔判据。
        with self.relay(*arguments) as relay:
            with relay.connect() as connection:
                connection.sendall(post_responses())
                read_http_response(connection)
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "", "1")
        self.assertEqual((status, report["request_count"]), (0, 1))
        self.assertIsNone(report["retry_after_seconds"])
        self.assertIsNone(report["all_gaps_at_least_retry_after"])
        # 一个受控 503 都没回过：失败关闭。
        with self.relay(*arguments) as relay:
            relay.stop()
            status, report = run_script_check(code, relay.output / "relay.json", "", "1")
        self.assertEqual((status, report["failures"]), (1, ["no-overloaded-503-served"]))


if __name__ == "__main__":
    unittest.main()
