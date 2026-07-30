#!/usr/bin/env python3
"""在无外网环境里采集官方 app-server 生成的 Wham consume 请求。

这个 RPC 可能真实消耗账号的 rate-limit reset credit，因此不能把探针直接指向
生产上游。本驱动只使用虚构的 ChatGPT 凭据，并把 ``chatgpt.com`` 指向本地 TLS
终结器；调用方还必须把整个容器放进 ``--network none`` 网络命名空间。

终结器保存客户端发出的原始 HTTP/1.1 字节，并返回最小的 ``no_credit`` 响应。
这样可核验官方 0.145.0 的方法、路径、header 线序和 JSON body，同时从网络层保证
请求不可能触达真实服务，也不会改变真实账号状态。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time


TARGET_PATH = "/backend-api/wham/rate-limit-reset-credits/consume"
FAKE_AUTH = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": None,
    "tokens": {
        "id_token": "eyJhbGciOiJub25lIn0.e30.eA",
        "access_token": "probe-access-token",
        "refresh_token": "probe-refresh-token",
        "account_id": "probe-account-0145",
    },
    "last_refresh": "2099-01-01T00:00:00Z",
}


class CaptureState:
    def __init__(self, output: pathlib.Path):
        self.output = output
        self.raw: bytes | None = None
        self.event = threading.Event()
        self.lock = threading.Lock()

    def save_target(self, raw: bytes) -> None:
        with self.lock:
            if self.raw is not None:
                return
            self.raw = raw
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_bytes(raw)
            os.chmod(self.output, 0o600)
            self.event.set()


class RawRequestHandler(socketserver.BaseRequestHandler):
    """只接收请求并本地应答，类中没有任何连接上游的代码。"""

    def handle(self) -> None:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 1024 * 1024:
            chunk = self.request.recv(65536)
            if not chunk:
                return
            data.extend(chunk)
        if b"\r\n\r\n" not in data:
            return

        head, body = bytes(data).split(b"\r\n\r\n", 1)
        content_length = 0
        for line in head.split(b"\r\n")[1:]:
            name, separator, value = line.partition(b":")
            if separator and name.strip().lower() == b"content-length":
                content_length = int(value.strip())
                break
        while len(body) < content_length:
            chunk = self.request.recv(content_length - len(body))
            if not chunk:
                break
            body += chunk
        body = body[:content_length]
        raw = head + b"\r\n\r\n" + body

        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        target_line = f"POST {TARGET_PATH} HTTP/1.1"
        if request_line == target_line:
            self.server.capture_state.save_target(raw)
            response_body = b'{"code":"no_credit"}'
            status = b"HTTP/1.1 200 OK\r\n"
        else:
            response_body = b"{}"
            status = b"HTTP/1.1 404 Not Found\r\n"
        response = (
            status
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(response_body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + response_body
        )
        self.request.sendall(response)


class LocalTLSServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        capture_state: CaptureState,
        cert: str,
        key: str,
    ):
        self.capture_state = capture_state
        self.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls_context.load_cert_chain(certfile=cert, keyfile=key)
        super().__init__(address, RawRequestHandler)

    def get_request(self):  # noqa: ANN201
        sock, address = super().get_request()
        return self.tls_context.wrap_socket(sock, server_side=True), address


class AppServer:
    def __init__(self, argv: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self.responses: dict[int, dict] = {}
        self.lock = threading.Lock()
        self.next_id = 0
        self.stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                with self.lock:
                    self.responses[message["id"]] = message

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            text = line.strip()
            if text:
                self.stderr.append(text)

    def call(self, method: str, params: dict, timeout: float = 15.0) -> dict:
        self.next_id += 1
        request_id = self.next_id
        request = {"id": request_id, "method": method, "params": params}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                response = self.responses.pop(request_id, None)
            if response is not None:
                return response
            if self.proc.poll() is not None:
                break
            time.sleep(0.05)
        detail = self.stderr[-1] if self.stderr else "无 stderr"
        raise RuntimeError(f"{method} 未返回：{detail}")

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def header_names(raw: bytes) -> list[str]:
    head = raw.split(b"\r\n\r\n", 1)[0]
    names = []
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            names.append(line.split(b":", 1)[0].decode("latin-1"))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="/usr/local/bin/codex")
    parser.add_argument("--codex-version", default="0.145.0")
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--idempotency-key", default="probe-redemption-0145")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        parser.error("--codex-version 必须是三段数字")

    output = pathlib.Path(args.output)
    metadata = pathlib.Path(args.metadata)
    state = CaptureState(output)
    server = LocalTLSServer((args.listen, args.port), state, args.cert, args.key)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    app: AppServer | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="codex-wham-probe-") as home:
            auth_file = pathlib.Path(home) / "auth.json"
            auth_file.write_text(
                json.dumps(FAKE_AUTH, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(auth_file, 0o600)
            env = os.environ.copy()
            env["CODEX_HOME"] = home
            env["HOME"] = home
            for name in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
            ):
                env.pop(name, None)
            env["NO_PROXY"] = "127.0.0.1,localhost,chatgpt.com"
            env["no_proxy"] = env["NO_PROXY"]

            argv = [
                args.codex_bin,
                "app-server",
                "--disable", "plugins",
                "--disable", "apps",
                "-c", 'cli_auth_credentials_store="file"',
                "-c", 'chatgpt_base_url="https://chatgpt.com/backend-api"',
            ]
            app = AppServer(argv, env)
            initialize = app.call("initialize", {
                "clientInfo": {
                    "name": "codex_exec",
                    "title": "Codex",
                    "version": args.codex_version,
                },
            })
            if "error" in initialize:
                raise RuntimeError(f"initialize 失败：{initialize['error']}")
            consume = app.call("account/rateLimitResetCredit/consume", {
                "idempotencyKey": args.idempotency_key,
            })
            if consume.get("result") != {"outcome": "noCredit"}:
                raise RuntimeError(f"consume 响应不符：{consume}")
            if not state.event.wait(timeout=5):
                raise RuntimeError("没有捕获到 consume HTTP 请求")

            assert state.raw is not None
            head, body = state.raw.split(b"\r\n\r\n", 1)
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            expected_body = json.dumps(
                {"redeem_request_id": args.idempotency_key},
                separators=(",", ":"),
            ).encode("utf-8")
            if body != expected_body:
                raise RuntimeError(f"consume body 不符：{body!r}")
            capture_metadata = {
                "schema_version": "codex-wham-consume-safe/v1",
                "codex_version": args.codex_version,
                "network_isolation_required": "docker --network none",
                "credentials": "全部为虚构占位值",
                "upstream": "本地 TLS 终结器，不含转发逻辑",
                "request_line": request_line,
                "header_names": header_names(state.raw),
                "body": json.loads(body),
                "app_server_result": consume["result"],
            }
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(
                json.dumps(capture_metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.chmod(metadata, 0o600)
            print(json.dumps(capture_metadata, ensure_ascii=False, indent=2))
    finally:
        if app is not None:
            app.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
