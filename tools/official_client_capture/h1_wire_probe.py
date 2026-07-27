#!/usr/bin/env python3
"""HTTP/1.1 线形探针：记录候选出站在 h1 上的原始请求字节。

存在的理由
----------
官方 OpenAI 直连画像是空 ALPN，`ProfileNegotiatesH2` 判定为假，实际走 HTTP/1.1；
而 MITM 抓包必须经代理，经代理时 Sub2API 换用 ALPN 含 h2 的代理画像。HTTP/2 的
HPACK 强制小写并重排 header，因此**只在 h1 上可见的差异（header 名大小写、header
顺序）在所有 MITM 证据里都不存在**。

本探针不做代理，而是用抓包 CA 签发 chatgpt.com 证书、由 hosts 把域名指向自身。
Sub2API 认为自己在直连，于是使用直连画像（空 ALPN），服务端据此协商 HTTP/1.1，
探针即可读到未经任何改写的原始请求行与 header 字节。

它只验证请求形态，不转发到真实上游，因此不消耗配额、不产生真实业务请求。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import threading
from pathlib import Path

# 读满 header 段即可判定形态；body 只在需要时继续读取以便正常回应。
HEADER_TERMINATOR = b"\r\n\r\n"
MAX_HEADER_BYTES = 256 * 1024
READ_CHUNK = 65536

SSE_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"content-type: text/event-stream\r\n"
    b"connection: close\r\n"
    b"\r\n"
    b"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_h1_probe\"}}\n\n"
)

# 观测官方 CLI 时，模型清单必须回一份能解析的载荷，否则 CLI 在清单阶段就退出，
# 后续的 /responses 请求根本不会发出，也就采集不到真正要对比的 POST 形态。
MODELS_BODY = (
    b'{"models":[{"slug":"gpt-5.6-luna","display_name":"GPT-5.6 Luna",'
    b'"visibility":"list","use_responses_lite":true,'
    b'"supports_parallel_tool_calls":true}]}'
)


# 官方在 WS 握手失败时会调用 force_http_fallback 降级到 HTTP POST（client.rs:509）。
# 探针无法完成 WS 升级，若回 200 客户端会当成协议异常并重试三次，把采集配额耗光；
# 明确回 400 让它一次就降级，POST /responses 的 h1 形态才采得到。
WS_REJECT = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"content-length: 0\r\n"
    b"connection: close\r\n"
    b"\r\n"
)


def build_response(request_line: str, header_names: list[str] | None = None) -> bytes:
    if header_names and any(name.strip().lower() == "upgrade" for name in header_names):
        return WS_REJECT
    if "/codex/models" in request_line:
        return (
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: application/json\r\n"
            b"content-length: " + str(len(MODELS_BODY)).encode() + b"\r\n"
            b"connection: close\r\n"
            b"\r\n" + MODELS_BODY
        )
    if "/ps/" in request_line or "/plugins" in request_line:
        body = b"{}"
        return (
            b"HTTP/1.1 200 OK\r\n"
            b"content-type: application/json\r\n"
            b"content-length: " + str(len(body)).encode() + b"\r\n"
            b"connection: close\r\n"
            b"\r\n" + body
        )
    return SSE_RESPONSE


def secure_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)


# 本探针的用途是核对 header 的名称大小写与顺序，取值只在少数静态项上有意义。
# 认证与动态身份必须脱敏：产物会被拉回本地归档，绝不能落明文凭据。
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "chatgpt-account-id",
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-turn-metadata",
    "x-codex-window-id",
    "x-codex-installation-id",
    "x-codex-turn-state",
}


def redact(name: str, value: str) -> str:
    if name.strip().lower() in SENSITIVE_HEADERS:
        return f"<redacted len={len(value)}>"
    return value


def parse_head(raw: bytes) -> dict:
    """按原始字节解析请求行与 header，保留出现顺序和大小写。"""

    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    request_line = lines[0] if lines else ""
    headers: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers.append({"name": name, "value": redact(name, value.strip())})
    return {
        "request_line": request_line,
        "headers": headers,
        "header_names_in_order": [item["name"] for item in headers],
    }


def handle(connection: ssl.SSLSocket, records: list, lock: threading.Lock) -> None:
    buffer = b""
    try:
        while HEADER_TERMINATOR not in buffer and len(buffer) < MAX_HEADER_BYTES:
            chunk = connection.recv(READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
        head_bytes = buffer.split(HEADER_TERMINATOR, 1)[0]
        record = parse_head(head_bytes)
        record["negotiated_alpn"] = connection.selected_alpn_protocol()
        record["tls_version"] = connection.version()
        with lock:
            records.append(record)
        connection.sendall(build_response(record.get("request_line", ""),
                                          record.get("header_names_in_order")))
    except (OSError, ssl.SSLError):
        pass
    finally:
        try:
            connection.close()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cert", required=True, help="chatgpt.com 证书链 PEM")
    parser.add_argument("--key", required=True, help="对应私钥 PEM")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--output", required=True, help="记录写入路径（JSON）")
    parser.add_argument("--expect", type=int, default=1, help="收满多少个请求后退出")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--idle-timeout", type=int, default=8,
                        help="收到首个请求后等待后续请求的秒数")
    args = parser.parse_args()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=args.cert, keyfile=args.key)
    # 刻意不调用 set_alpn_protocols：客户端 offer 为空时不协商，服务端按 HTTP/1.1 处理。

    records: list = []
    lock = threading.Lock()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", args.port))
    listener.listen(16)
    listener.settimeout(args.timeout)

    threads: list[threading.Thread] = []
    try:
        while len(records) < args.expect:
            try:
                raw_conn, _ = listener.accept()
            except socket.timeout:
                break
            try:
                tls_conn = context.wrap_socket(raw_conn, server_side=True)
            except (OSError, ssl.SSLError):
                raw_conn.close()
                continue
            thread = threading.Thread(target=handle, args=(tls_conn, records, lock), daemon=True)
            thread.start()
            threads.append(thread)
            thread.join(timeout=args.timeout)
            # 收到首个请求后缩短等待：后续请求若存在会紧随其后，
            # 没有则不必空等完整超时，让编排脚本尽快读到产物。
            listener.settimeout(args.idle_timeout)
    finally:
        listener.close()

    secure_write(
        Path(args.output),
        json.dumps({"schema_version": "h1-wire-probe/v1", "requests": records},
                   ensure_ascii=False, indent=2) + "\n",
    )
    print(json.dumps({"captured": len(records), "output": args.output}, ensure_ascii=False))


if __name__ == "__main__":
    main()
