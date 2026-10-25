"""本机独立进程采集专项：真实回环字节与真实 fixture_only 总账。

任务里的产品制品只属于结构夹具；测试不注册来源，不启动真实产品。
成功凭证仅代表 native H1／TLS-H1 后端，不能替代完整 Mac v2 验收。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import http.client
import http.server
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as product_contract
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_wire as wire
from tools.official_client_capture import claude_upgrade_mac_budget as budget
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture.claude_upgrade_mac_capture_client import CaptureClient
from tools.official_client_capture.claude_upgrade_mac_capture_service import CaptureEngine, _mac, receive

SSE_BODY = b"event: message\ndata: {\"text\":\"real-loopback\"}\n\ndata: [DONE]\n\n"


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _rows(path: Path) -> list[dict]:
    return [c.parse_json(line) for line in c.read_bytes(path).splitlines()]


def _serve_upstream(arguments: argparse.Namespace) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_values: object) -> None:
            pass

        def do_POST(self) -> None:
            if self.headers.get("Transfer-Encoding") == "chunked":
                body = bytearray()
                while True:
                    amount = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                    if not amount:
                        while self.rfile.readline() != b"\r\n":
                            pass
                        break
                    body.extend(self.rfile.read(amount))
                    if self.rfile.read(2) != b"\r\n":
                        raise ValueError("测试上游收到坏 chunk")
                body = bytes(body)
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            event = {"method": self.command, "path": self.path, "body_base64": base64.b64encode(body).decode("ascii"), "received_at_utc": c.utc_now()}
            descriptor = os.open(arguments.log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, c.canonical(event) + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if arguments.mode == "stall":
                self.send_response(200)
                self.send_header("Content-Length", "6")
                self.end_headers()
                self.wfile.write(b"ab")
                self.wfile.flush()
                time.sleep(10)
                self.close_connection = True
                return
            if arguments.mode == "sse":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for offset in range(0, len(SSE_BODY), 7):
                    block = SSE_BODY[offset:offset + 7]
                    self.wfile.write(f"{len(block):x};test=1\r\n".encode() + block + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\nX-End: actual\r\n\r\n")
                self.wfile.flush()
                return
            response = b"rejected" if arguments.mode == "reject" else b"echo:" + body
            self.send_response(400 if arguments.mode == "reject" else 200)
            if arguments.mode != "close":
                self.send_header("Content-Length", str(len(response)))
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(response)
            self.wfile.flush()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler)
    server.daemon_threads = True
    if arguments.certificate:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(arguments.certificate, arguments.private_key)
        context.set_alpn_protocols(["http/1.1"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print("ready", flush=True)
    server.serve_forever(poll_interval=0.05)


class H1ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = {"max_header_bytes": 65536, "max_body_bytes": 1048576}

    def test_split_chunked_and_sse_preserve_entity_bytes(self) -> None:
        parser = wire.H1Parser("native-1", self.limits)
        request = b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n3\r\na\x00b\r\n0\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nx\x00y\r\n0\r\nX-A: b\r\n\r\n"
        for raw, direction in ((request, wire.CLIENT_DIRECTION), (response, wire.SERVER_DIRECTION)):
            for value in raw:
                parser.feed(direction, bytes([value]), c.utc_now())
        self.assertFalse(parser.has_pending())
        self.assertEqual(base64.b64decode(parser.request_rows()[0]["request_body_base64"]), b"a\x00b")
        self.assertEqual(base64.b64decode(parser.request_rows()[0]["response_body_base64"]), b"x\x00y")

    def test_interim_head_and_close_delimited_response(self) -> None:
        parser = wire.H1Parser("native-1", self.limits)
        parser.feed(wire.CLIENT_DIRECTION, b"HEAD /health HTTP/1.1\r\nHost: localhost\r\n\r\nPOST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\n\r\n", c.utc_now())
        parser.feed(wire.SERVER_DIRECTION, b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nHTTP/1.1 200 OK\r\nConnection: close\r\n\r\nactual", c.utc_now())
        parser.eof(wire.SERVER_DIRECTION)
        self.assertEqual([row["request_ordinal"] for row in parser.request_rows()], [1, 2])
        self.assertEqual(base64.b64decode(parser.request_rows()[1]["response_body_base64"]), b"actual")
        self.assertFalse(parser.has_pending())

    def test_bad_framing_preserves_known_post(self) -> None:
        parser = wire.H1Parser("native-1", self.limits)
        with self.assertRaises(c.CaptureError):
            parser.feed(wire.CLIENT_DIRECTION, b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}", c.utc_now())
        self.assertEqual(len(parser.request_rows()), 1)
        self.assertEqual(parser.request_rows()[0]["path"], "/v1/messages")

    def test_h2_and_websocket_are_not_complete_h1(self) -> None:
        for raw in (b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n", b"GET /ws HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n\r\n"):
            with self.subTest(raw=raw[:10]):
                parser = wire.H1Parser("native-1", self.limits)
                with self.assertRaises(c.CaptureError):
                    parser.feed(wire.CLIENT_DIRECTION, raw, c.utc_now())


class NativeCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ccap-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = c.private_directory(self.root / "d")
        self.staging = c.private_directory(self.root / "staging")
        self.ledger_root = self.staging / ledger.ledger_dir_name("claude")
        ledger.create_project_ledger(self.ledger_root, project_id="native-capture-fixture", client="claude", absolute_deadline_utc=_future(7200), deadline_approved_by="fixture", estimation_policy="none", estimation_policy_approved_by="fixture", fixture_only=True, live_request_budget=64, formal_open_limit=64)
        self.campaign = c.private_directory(self.staging / "native-campaign")
        c.write_json_once(self.campaign / "campaign.json", {"client": "claude", "campaign_id": "native-campaign", "campaign_mode": "formal", "target_version": "2.1.226"})
        with ledger.admission_scope(self.campaign, campaign_id="native-campaign", campaign_mode="formal", target_version="2.1.226", require=True, client="claude") as admission:
            admission.register(self.campaign, campaign_id="native-campaign", campaign_mode="formal", target_version="2.1.226", deadline_at_utc=_future(3600))
        self.listener_port, self.upstream_port = _port(), _port()
        while self.listener_port == self.upstream_port:
            self.upstream_port = _port()
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._stop_processes)
        self.task: dict = {}
        self.configuration: dict = {}
        self.client: CaptureClient | None = None

    def _stop_processes(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def _spawn(self, command: list[str], label: str) -> subprocess.Popen:
        output = open(self.root / (label + ".log"), "ab")
        self.addCleanup(output.close)
        process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[3], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        self.processes.append(process)
        return process

    def _upstream(self, mode: str = "normal", tls: bool = False) -> subprocess.Popen:
        # discover 会以短模块名导入本文件；独立进程从仓库根启动，
        # 始终使用完整模块名，避免把父进程临时 sys.path 当作启动合同。
        command = [sys.executable, "-B", "-m",
            "tools.official_client_capture.tests.test_claude_mac_capture_service",
            "--upstream", "--port", str(self.upstream_port), "--mode", mode,
            "--log", str(self.root / "upstream-requests.jsonl")]
        if tls:
            command.extend(["--certificate", str(self.data / "cert.pem"), "--private-key", str(self.data / "key.pem")])
        process = self._spawn(command, "upstream")
        self._wait(lambda: "ready" in (self.root / "upstream.log").read_text(), process)
        return process

    def _wait(self, predicate, process: subprocess.Popen | None = None) -> None:
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if process and process.poll() is not None:
                self.fail("独立进程提前退出：" + "\n".join(path.read_text() for path in self.root.glob("*.log")))
            try:
                if predicate():
                    return
            except (OSError, c.CaptureError):
                pass
            time.sleep(0.01)
        self.fail("有界等待超时：" + "\n".join(path.read_text() for path in self.root.glob("*.log")))

    def _certificate(self) -> None:
        # TLS 回环夹具可在 Linux 和 Darwin 执行；工具缺失必须失败，不能跳过真实 TLS 断言。
        openssl = shutil.which("openssl")
        self.assertIsNotNone(openssl, "TLS 回环夹具需要 PATH 中的 openssl")
        result = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(self.data / "key.pem"), "-out", str(self.data / "cert.pem"), "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0)
        for name in ("key.pem", "cert.pem"):
            (self.data / name).chmod(0o600)

    def _prepare(self, *, tls: bool = False, amount: int = 4) -> dict:
        artifacts = []
        for role, kind, version_kind in (("application", "app_bundle", "product"), ("application_executable", "host_executable", "product"), ("embedded_cli", "embedded_cli", "embedded_cli")):
            artifacts.append({"artifact_id": role, "kind": kind, "product": "claude-desktop", "role": role, "version": {"kind": version_kind, "value": "2.1.226"}, "platform": "darwin/arm64", "content": {"kind": "directory" if kind == "app_bundle" else "file", "path": str(self.root / role), "sha256": "c" * 64, "bytes": 0}, "official_source": {"path": "fixture-source.json", "sha256": "d" * 64, "bytes": 0}})
        self.task = {"schema_version": "claude-upgrade-client-task/v2", "client_id": "claude-desktop", "campaign_id": "native-campaign", "candidate_id": "candidate-a", "attempt_id": "attempt-a", "run_nonce": "original-run", "candidate_revision": 1, "candidate": {"source_tree_sha256": "a" * 64, "build_receipt_sha256": "a" * 64, "image_digest": "sha256:" + "a" * 64, "profile_digest": "a" * 64, "gateway_sha256": "a" * 64}, "catalog": {"sha256": "b" * 64, "entry": {"client_id": "claude-desktop", "product": "claude-desktop", "version": "2.1.226", "platform": "darwin/arm64", "binary_sha256": "c" * 64}}, "client_artifacts": artifacts, "scenarios": [{"id": "echo", "model": "fixture-model", "challenge": "native-real-loopback-challenge", "response_marker": "echo", "minimum_requests": 1}], "scenario_reuse": [], "execution": {"endpoint": f"{'https' if tls else 'http'}://127.0.0.1:{self.listener_port}", "credential_ref": "private-ref:fixture-unused", "timeout_seconds": 300, "request_budget": amount, "recorder_id": "not-registered", "capture_id": "native-capture", "producer_run_id": "original-producer", "source_scope": c.SCOPE, "capture_root": str(self.data / "capture"), "project_ledger": str(self.ledger_root), "source_deployments": {"mac": {"deployment_id": "fixture-mac", "tool_identity_sha256": "e" * 64}, "capture": {"deployment_id": "fixture-capture", "tool_identity_sha256": "f" * 64}}, "isolation_recipe": {"path": "fixture-only.json", "sha256": "e" * 64, "bytes": 0}, "request_policy": {"methods": ["POST"], "model_paths": sorted(wire.MODEL_PATHS), "retry_upper_bound": 0, "max_concurrency": 4, "deadline_utc": _future(240)}}, "issued_at_utc": c.utc_now(), "task_nonce": "native-task"}
        self.task["task_sha256"] = c.digest(self.task)
        c.write_json_once(self.data / "task.json", self.task)
        c.write_once(self.data / "channel.key", secrets.token_bytes(32))
        listener_tls = {"certificate": c.binding(self.data, self.data / "cert.pem"), "private_key": c.binding(self.data, self.data / "key.pem")} if tls else None
        upstream_tls = {"ca": c.binding(self.data, self.data / "cert.pem"), "server_hostname": "127.0.0.1"} if tls else None
        self.configuration = {"schema_version": c.CONFIG_SCHEMA, "source_scope": c.SCOPE, "data_root": str(self.data), "capture_relative": "capture", "task_binding": c.binding(self.data, self.data / "task.json"), "deployment": self.task["execution"]["source_deployments"]["capture"], "control_socket_relative": "control/rpc.sock", "channel_key_binding": c.binding(self.data, self.data / "channel.key"), "listener": {"host": "127.0.0.1", "port": self.listener_port, "tls": listener_tls}, "upstream": {"host": "127.0.0.1", "port": self.upstream_port, "tls": upstream_tls}, "limits": {"drain_seconds": 0.2, "socket_timeout_seconds": 1, "max_connections": 8, "max_header_bytes": 65536, "max_body_bytes": 1048576, "max_wire_bytes": 4194304}}
        c.write_json_once(self.data / "configuration.json", self.configuration)
        bound = {"schema_version": budget.BOUND_SCHEMA, "client": "claude", **{name: self.task[name] for name in ("campaign_id", "candidate_id", "attempt_id", "run_nonce", "candidate_revision", "task_sha256")}, **{name: self.task["execution"][name] for name in ("producer_run_id", "capture_id", "request_budget")}, "deadline_utc": self.task["execution"]["request_policy"]["deadline_utc"], "task_binding": c.binding(self.data, self.data / "task.json"), "attempt_reservation_binding": {"path": "fixture-attempt.json", "sha256": "a" * 64, "bytes": 0}}
        with ledger.project_lock(self.ledger_root):
            plan, _raw = ledger._load_plan(self.ledger_root)
            admission = ledger.RuntimeAdmission(self.ledger_root, plan, ledger.replay_head(self.ledger_root), ledger._campaign_plan(self.campaign / "ledger"))
            hold = budget.reserve_bound(admission, bound, expected_sequence=admission.head_sequence, expected_head_sha256=admission.head_sha256)
        self.client = CaptureClient(self.configuration, self.root / "received", timeout_seconds=8)
        return hold

    def _daemon(self) -> subprocess.Popen:
        process = self._spawn([sys.executable, "-B", "-m", "tools.official_client_capture.claude_upgrade_mac_capture_service", "--configuration", str(self.data / "configuration.json"), "--configuration-sha256", c.digest(self.configuration)], "daemon")
        self._wait(lambda: self.client.call("status", {})["task_sha256"] == self.task["task_sha256"], process)
        return process

    def _start(self, mode: str = "normal", *, tls: bool = False, amount: int = 4) -> subprocess.Popen:
        if tls:
            self._certificate()
        self._upstream(mode, tls)
        hold = self._prepare(tls=tls, amount=amount)
        process = self._daemon()
        self.client.reserve(self.task, hold)
        self.client.wait_ready(self.task)
        return process

    def _connection(self, *, tls: bool = False):
        if tls:
            context = ssl.create_default_context(cafile=str(self.data / "cert.pem"))
            context.set_alpn_protocols(["http/1.1"])
            connection = http.client.HTTPSConnection("127.0.0.1", self.listener_port, context=context, timeout=3)
        else:
            connection = http.client.HTTPConnection("127.0.0.1", self.listener_port, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def _post(self, connection, body: bytes = b'{"model":"fixture-model"}', *, chunked: bool = False, expect: bool = False) -> bytes:
        headers = {"Content-Type": "application/json"}
        if chunked:
            headers["Transfer-Encoding"] = "chunked"
        if expect:
            headers["Expect"] = "100-continue"
        connection.request("POST", "/v1/messages", body=[body[:3], body[3:]] if chunked else body, headers=headers, encode_chunked=chunked)
        return connection.getresponse().read()

    def _closed(self) -> tuple[dict, Path, dict]:
        result = self.client.close_and_sync(self.task)
        root = Path(result["root"])
        native = c.read_json(root / "native-capture-manifest.json")
        return result, root, native

    def test_real_reservation_ready_and_same_connection_ordinals(self) -> None:
        self._start()
        connection = self._connection()
        for body in (b'{"model":"fixture-model","n":1}', b"bad-json\x00"):
            self.assertEqual(self._post(connection, body), b"echo:" + body)
        result, root, native = self._closed()
        self.assertEqual(result["status"], "complete")
        requests = _rows(root / "native-requests.jsonl")
        self.assertEqual([row["request_ordinal"] for row in requests], [1, 2])
        self.assertEqual(len({row["connection_id"] for row in requests}), 1)
        upstream = _rows(self.root / "upstream-requests.jsonl")
        self.assertEqual([row["request_body_base64"] for row in requests], [row["body_base64"] for row in upstream])
        for row in _rows(root / "native-connections.jsonl"):
            self.assertIsNotNone(row["closed_at_utc"])
        self.assertFalse((root / "capture-manifest.json").exists())
        self.assertEqual(product_contract.SOURCE_ADAPTERS, {})
        c.verify_export(root, c.read_json(root / "authenticated-inventory.json"), task_sha256=self.task["task_sha256"])

    def test_two_connections_use_native_distinct_ids(self) -> None:
        self._start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(pool.map(lambda marker: self._post(self._connection(), marker), (b"one", b"two")))
        self.assertEqual(outputs, [b"echo:one", b"echo:two"])
        result, root, _native = self._closed()
        requests = _rows(root / "native-requests.jsonl")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len({row["connection_id"] for row in requests}), 2)
        self.assertEqual([row["request_ordinal"] for row in requests], [1, 1])

    def test_chunked_request_and_real_sse(self) -> None:
        self._start("sse")
        body = b'{"model":"fixture-model","text":"actual-chunked"}'
        self.assertEqual(self._post(self._connection(), body, chunked=True), SSE_BODY)
        result, root, _native = self._closed()
        row = _rows(root / "native-requests.jsonl")[0]
        self.assertEqual(result["status"], "complete")
        self.assertEqual(base64.b64decode(row["request_body_base64"]), body)
        self.assertEqual(base64.b64decode(row["response_body_base64"]), SSE_BODY)

    def test_real_tls_both_legs_and_alpn(self) -> None:
        self._start(tls=True)
        body = b'{"model":"fixture-model","tls":true}'
        self.assertEqual(self._post(self._connection(tls=True), body), b"echo:" + body)
        result, root, _native = self._closed()
        self.assertEqual(result["status"], "complete")
        observations = list(root.rglob("upstream-connection-observation.json"))
        self.assertEqual(len(observations), 1)
        facts = c.read_json(observations[0])["tls"]
        self.assertEqual(facts["client_negotiated_alpn"], "http/1.1")
        self.assertEqual(facts["upstream_negotiated_alpn"], "http/1.1")

    def test_h2_tls_offer_is_not_downgraded(self) -> None:
        self._start(tls=True)
        context = ssl.create_default_context(cafile=str(self.data / "cert.pem"))
        context.set_alpn_protocols(["h2", "http/1.1"])
        with socket.create_connection(("127.0.0.1", self.listener_port), timeout=3) as raw:
            with self.assertRaises((OSError, ssl.SSLError)):
                context.wrap_socket(raw, server_hostname="127.0.0.1")
        self._wait(lambda: self.client.call("status", {})["connections"] == 1)
        result, _root, native = self._closed()
        self.assertEqual(result["status"], "truncated")
        self.assertTrue(any("H2" in reason for reason in native["reasons"]))

    def test_interim_response_preserves_ordinal(self) -> None:
        self._start()
        self.assertEqual(self._post(self._connection(), b"actual", expect=True), b"echo:actual")
        result, root, _native = self._closed()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(_rows(root / "native-requests.jsonl")), 1)

    def test_close_delimited_body_is_complete_on_real_eof(self) -> None:
        self._start("close")
        self.assertEqual(self._post(self._connection(), b"eof"), b"echo:eof")
        result, root, _native = self._closed()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(base64.b64decode(_rows(root / "native-requests.jsonl")[0]["response_body_base64"]), b"echo:eof")

    def test_drain_timeout_keeps_partial_response_and_post(self) -> None:
        self._start("stall")
        connection = self._connection()
        connection.request("POST", "/v1/messages", body=b"actual")
        response = connection.getresponse()
        self.assertEqual(response.read(2), b"ab")
        result, root, native = self._closed()
        self.assertEqual(result["status"], "truncated")
        requests = _rows(root / "native-requests.jsonl")
        self.assertEqual(len(requests), 1)
        self.assertEqual(base64.b64decode(requests[0]["response_body_base64"]), b"ab")
        self.assertTrue(any("超时" in value for value in native["reasons"]))

    def test_failed_post_and_budget_overrun_are_not_lost(self) -> None:
        self._start("reject", amount=1)
        connection = self._connection()
        self.assertEqual(self._post(connection, b"not-json"), b"rejected")
        # http.client 会把请求头和请求体拆成两次 send；引擎读到超预算的请求头后即停读，
        # 迟到的请求体不会被读取（原件里该请求正文为空），结果取决于两次 send 的间隔，
        # 在 ARM64 满载下偶发失败（10-17 实测）。引擎只保证同一已持久化片段里的正文被保留，
        # 所以这里把整条 POST（头＋体）用一次 sendall 写出，让正文确定落在同一个片段里。
        head = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1:{self.listener_port}\r\n"
                "Content-Type: application/json\r\nContent-Length: 6\r\n\r\n").encode("ascii")
        connection.sock.sendall(head + b"second")
        connection.sock.settimeout(3)
        try:
            leftover = connection.sock.recv(4096)
        except OSError:
            # 连接被引擎复位也算“没有回包”，与干净关闭（EOF）等价。
            leftover = b""
        self.assertEqual(leftover, b"")
        result, root, _native = self._closed()
        self.assertEqual(result["status"], "truncated")
        requests = _rows(root / "native-requests.jsonl")
        self.assertEqual(len(requests), 2)
        self.assertEqual(base64.b64decode(requests[1]["request_body_base64"]), b"second")
        self.assertEqual(len(_rows(self.root / "upstream-requests.jsonl")), 1)

    def test_fake_budget_cannot_publish_reservation_or_ready(self) -> None:
        self._prepare()
        self._daemon()
        with self.assertRaises(c.CaptureError):
            self.client.reserve(self.task, {"status": "reserved"})
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)
        self.assertFalse((self.data / "capture" / "reservation.json").exists())
        self.assertFalse((self.data / "capture" / "ready.json").exists())
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.listener_port), timeout=0.1)  # time-threshold: allow 断言的是 OSError：连接被拒绝或超时（TimeoutError 也是 OSError）都满足，机器变慢不会抖红

    def _assert_capture_owner_lock_rejected(self, lock: Path) -> None:
        capture = c.private_directory(self.data / "capture")
        originals = {"daemon-owner.json": b'{"untouched":"owner"}\n', "reservation.json": b'{"untouched":"reservation"}\n'}
        for name, raw in originals.items():
            c.write_once(capture / name, raw)
        previous_head = ledger.replay_head(self.ledger_root)
        previous_lock = (lock.read_bytes(), lock.stat().st_mode, lock.stat().st_nlink)
        opened = []
        original_open = os.open
        def observed_open(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == lock:
                opened.append(descriptor)
            return descriptor
        # 仅观察真实 open 返回的 fd，不替换 fstat；拒绝必须关闭它且不能动原件。
        with mock.patch.object(os, "open", side_effect=observed_open):
            with self.assertRaisesRegex(c.CaptureError, "所有权锁"):
                CaptureEngine(self.configuration)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError):
            os.fstat(opened[0])
        self.assertEqual(previous_lock, (lock.read_bytes(), lock.stat().st_mode, lock.stat().st_nlink))
        for name, raw in originals.items():
            self.assertEqual(c.read_bytes(capture / name), raw)
        self.assertEqual(ledger.replay_head(self.ledger_root), previous_head)
        self.assertEqual({path.name for path in capture.iterdir()}, set(originals))

    def test_capture_owner_lock_bad_permissions_are_rejected(self) -> None:
        self._prepare()
        lock = c.private_directory(self.data / "control") / "capture-owner.lock"
        c.write_once(lock, b"existing-lock")
        lock.chmod(0o644)
        self._assert_capture_owner_lock_rejected(lock)

    def test_capture_owner_lock_hard_link_is_rejected(self) -> None:
        self._prepare()
        lock = c.private_directory(self.data / "control") / "capture-owner.lock"
        c.write_once(lock, b"existing-lock")
        os.link(lock, self.data / "capture-owner-alias.lock")
        self._assert_capture_owner_lock_rejected(lock)

    def test_close_is_immutable_and_replay_does_not_rerun(self) -> None:
        self._start()
        self._post(self._connection(), b"actual")
        first, root, _native = self._closed()
        snapshot = c.read_bytes(root / "native-capture-manifest.json")
        second = self.client.close_and_sync(self.task)
        self.assertEqual(first, second)
        self.assertEqual(snapshot, c.read_bytes(Path(second["root"]) / "native-capture-manifest.json"))
        self.assertEqual(len(_rows(self.root / "upstream-requests.jsonl")), 1)

    def test_sync_interruption_resumes_real_partial_without_new_request(self) -> None:
        self._start()
        self._post(self._connection(), b'{"text":"' + b"x" * 180000 + b'"}')
        inventory = self.client.call("close", {"task_sha256": self.task["task_sha256"], "reason": "normal"})
        original = self.client._chunk
        def interrupted(export_id, relative, offset, limit):
            if relative.endswith("client_to_upstream.bin") and offset >= c.MAX_CHUNK_BYTES:
                raise ConnectionError("测试通信中断")
            return original(export_id, relative, offset, limit)
        with mock.patch.object(self.client, "_chunk", side_effect=interrupted):
            with self.assertRaises(ConnectionError):
                self.client.sync_committed_export(self.task, inventory["export_id"])
        partials = list((self.root / "received" / "pending").rglob("*client_to_upstream.bin"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(len(c.read_bytes(partials[0])), c.MAX_CHUNK_BYTES)
        result = self.client.sync_committed_export(self.task, inventory["export_id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(_rows(self.root / "upstream-requests.jsonl")), 1)

    def test_sigkill_recovery_keeps_native_request_and_unknown_close(self) -> None:
        process = self._start()
        connection = self._connection()
        self._post(connection, b"before-kill")
        process.kill()
        process.wait(timeout=2)
        self._daemon()
        result = self.client.recover(self.task)
        root = Path(result["root"])
        self.assertEqual(result["status"], "truncated")
        self.assertEqual(base64.b64decode(_rows(root / "native-requests.jsonl")[0]["request_body_base64"]), b"before-kill")
        self.assertIsNone(_rows(root / "native-connections.jsonl")[0]["closed_at_utc"])
        with self.assertRaises(c.CaptureError):
            self.client.wait_ready(self.task)
        self.assertEqual(len(_rows(self.root / "upstream-requests.jsonl")), 1)

    def test_committed_recovery_keeps_exact_export(self) -> None:
        process = self._start()
        self._post(self._connection(), b"committed")
        first, root, _native = self._closed()
        manifest_bytes = c.read_bytes(root / "native-capture-manifest.json")
        process.kill()
        process.wait(timeout=2)
        self._daemon()
        recovered = self.client.recover(self.task)
        self.assertEqual(recovered, first)
        self.assertEqual(manifest_bytes, c.read_bytes(Path(recovered["root"]) / "native-capture-manifest.json"))
        self.assertEqual(self.client.call("status", {})["requests"], 1)

    def test_wrong_key_nonce_replay_and_file_escape_are_denied(self) -> None:
        self._start()
        original_key = self.client.key
        self.client.key = secrets.token_bytes(32)
        with self.assertRaises(c.CaptureError):
            self.client.call("status", {})
        self.client.key = original_key
        nonce = secrets.token_hex(24)
        core = {"protocol": c.RPC_PROTOCOL, "nonce": nonce, "action": "status", "payload": {}}
        request = c.canonical({**core, "mac": _mac(original_key, core)}) + b"\n"
        responses = []
        for _attempt in range(2):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(str(self.client.path))
                channel.sendall(request)
                responses.append(c.parse_json(receive(channel)))
        self.assertTrue(responses[0]["body"]["ok"])
        self.assertFalse(responses[1]["body"]["ok"])
        inventory = self.client.call("close", {"task_sha256": self.task["task_sha256"], "reason": "normal"})
        with self.assertRaises(c.CaptureError):
            self.client.call("export", {"task_sha256": self.task["task_sha256"], "export_id": inventory["export_id"], "path": "../channel.key", "offset": 0, "limit": 32})

    def test_export_tamper_is_not_replayed(self) -> None:
        self._start()
        self._post(self._connection(), b"tamper")
        inventory = self.client.call("close", {"task_sha256": self.task["task_sha256"], "reason": "normal"})
        original = self.data / "capture" / "native-requests.jsonl"
        original.write_bytes(c.read_bytes(original) + b" ")
        with self.assertRaises(c.CaptureError):
            self.client.sync_committed_export(self.task, inventory["export_id"])

    def test_concurrent_sync_uses_same_commit_without_duplicate_execution(self) -> None:
        self._start()
        self._post(self._connection(), b"concurrent-sync")
        inventory = self.client.call("close", {"task_sha256": self.task["task_sha256"], "reason": "normal"})
        second = CaptureClient(self.configuration, self.root / "received", timeout_seconds=8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(item.sync_committed_export, self.task, inventory["export_id"]) for item in (self.client, second)]
            results = [item.result(timeout=8) for item in pending]
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(_rows(self.root / "upstream-requests.jsonl")), 1)

    def test_missing_sync_commit_fails_replay(self) -> None:
        self._start()
        self._post(self._connection(), b"commit-required")
        result, root, _native = self._closed()
        (root / "sync-commit.json").unlink()
        with self.assertRaises((c.CaptureError, OSError)):
            self.client.sync_committed_export(self.task, result["export_id"])

    def test_partial_request_eof_keeps_known_post(self) -> None:
        self._start()
        with socket.create_connection(("127.0.0.1", self.listener_port), timeout=2) as connection:
            connection.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 8\r\n\r\nabc")
            connection.shutdown(socket.SHUT_WR)
            self._wait(lambda: self.client.call("status", {})["requests"] == 1)
        result, root, _native = self._closed()
        self.assertEqual(result["status"], "truncated")
        self.assertEqual(base64.b64decode(_rows(root / "native-requests.jsonl")[0]["request_body_base64"]), b"abc")

    def test_wal_torn_tail_is_preserved_and_hold_not_released(self) -> None:
        process = self._start()
        self._post(self._connection(), b"before-torn-tail")
        process.kill()
        process.wait(timeout=2)
        journal = self.data / "capture" / "journal.jsonl"
        torn = c.read_bytes(journal) + b'{"uncommitted"'
        journal.write_bytes(torn)
        process = self._spawn([sys.executable, "-B", "-m", "tools.official_client_capture.claude_upgrade_mac_capture_service", "--configuration", str(self.data / "configuration.json"), "--configuration-sha256", c.digest(self.configuration)], "torn-daemon")
        self.assertNotEqual(process.wait(timeout=3), 0)
        self.assertIn("裂尾", (self.root / "torn-daemon.log").read_text())
        self.assertEqual(c.read_bytes(journal), torn)
        self.assertFalse((self.data / "capture" / "native-capture-manifest.json").exists())
        self.assertEqual(ledger.replay_head(self.ledger_root)["reserved_live_requests"], self.task["execution"]["request_budget"])

    def test_real_upstream_certificate_failure_keeps_received_post(self) -> None:
        self._certificate()
        self._upstream(tls=True)
        hold = self._prepare(tls=True)
        self.configuration["upstream"]["tls"]["server_hostname"] = "wrong-hostname.example"
        configuration = self.data / "configuration.json"
        configuration.write_bytes(c.canonical(self.configuration) + b"\n")
        self.client = CaptureClient(self.configuration, self.root / "received", timeout_seconds=8)
        self._daemon()
        self.client.reserve(self.task, hold)
        self.client.wait_ready(self.task)
        with self.assertRaises((OSError, http.client.HTTPException)):
            self._post(self._connection(tls=True), b"received-before-tls-failure")
        result, root, native = self._closed()
        self.assertEqual(result["status"], "truncated")
        self.assertEqual(len(_rows(root / "native-requests.jsonl")), 1)
        self.assertTrue(any("SSLCertVerificationError" in reason for reason in native["reasons"]))

    def test_configuration_cannot_enable_formal_or_non_loopback(self) -> None:
        self._prepare()
        for field, value in (("source_scope", "formal"), ("listener", {"host": "0.0.0.0", "port": self.listener_port, "tls": None})):
            with self.subTest(field=field), self.assertRaises(c.CaptureError):
                c.validate_configuration({**self.configuration, field: value})
        alias = self.data / "alias.key"
        alias.symlink_to(self.data / "channel.key")
        with self.assertRaises(c.CaptureError):
            c.read_bytes(alias)


if __name__ == "__main__":
    if "--upstream" in sys.argv:
        parser = argparse.ArgumentParser(description="真实回环测试上游")
        parser.add_argument("--upstream", action="store_true")
        parser.add_argument("--port", type=int, required=True)
        parser.add_argument("--mode", choices=("normal", "sse", "stall", "close", "reject"), required=True)
        parser.add_argument("--log", type=Path, required=True)
        parser.add_argument("--certificate")
        parser.add_argument("--private-key")
        _serve_upstream(parser.parse_args())
    else:
        unittest.main()
