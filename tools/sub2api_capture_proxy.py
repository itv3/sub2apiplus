#!/usr/bin/env python3
"""Sub2API 官方客户端采样反代。

该脚本用于阶段二对照官方客户端真实请求形态。它只做三件事：
1. 记录脱敏后的入站请求和上游响应摘要；
2. 将带采样前缀的路径剥离后转发到本机 Sub2API；
3. 以流式方式回传上游响应，尽量不改变客户端行为。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any


SENSITIVE_HEADER_RE = re.compile(
    r"(authorization|api[-_]?key|x-api-key|x-goog-api-key|cookie|token|secret|password|credential|session)",
    re.I,
)
SENSITIVE_JSON_KEY_RE = re.compile(
    r"(api[-_]?key|access[-_]?token|refresh[-_]?token|id[-_]?token|authorization|cookie|secret|password|credential)",
    re.I,
)
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="milliseconds")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:80] or "capture"


def redact_scalar(value: Any) -> str:
    text = "" if value is None else str(value)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"<redacted:{len(text)}:{digest}>"


def sanitize_headers(headers: Any) -> dict[str, list[str]]:
    sanitized: dict[str, list[str]] = {}
    for key in headers.keys():
        values = headers.get_all(key) if hasattr(headers, "get_all") else [headers.get(key)]
        if SENSITIVE_HEADER_RE.search(key):
            sanitized[key] = [redact_scalar(v) for v in values if v is not None]
        else:
            sanitized[key] = [str(v) for v in values if v is not None]
    return sanitized


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_JSON_KEY_RE.search(str(key)):
                result[str(key)] = redact_scalar(item)
            else:
                result[str(key)] = sanitize_json(item)
        return result
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


def sanitize_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    text = body.decode("utf-8", "replace")
    if "json" in content_type.lower():
        try:
            return sanitize_json(json.loads(text))
        except Exception:
            return {"_decode": "json_parse_failed", "text": text}
    return {"text": text}


def sanitize_url_for_log(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_pairs: list[tuple[str, str]] = []
    for key, value in query_pairs:
        if SENSITIVE_JSON_KEY_RE.search(key):
            safe_pairs.append((key, redact_scalar(value)))
        else:
            safe_pairs.append((key, value))
    safe_query = urllib.parse.urlencode(safe_pairs)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, safe_query, parsed.fragment))


def event_types_from_sse(data: bytes) -> list[str]:
    seen: list[str] = []
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
            if event and event not in seen:
                seen.append(event)
        elif line.startswith("data:"):
            payload = line.split(":", 1)[1].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            event = obj.get("type")
            if isinstance(event, str) and event not in seen:
                seen.append(event)
    return seen


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class CaptureProxyHandler(BaseHTTPRequestHandler):
    server_version = "sub2api-capture-proxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        sys.stderr.write("%s %s\n" % (now_iso(), sanitize_url_for_log(message)))

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        requestline = f"{self.command} {sanitize_url_for_log(self.path)} {self.request_version}"
        sys.stderr.write(f"{now_iso()} {self.address_string()} \"{requestline}\" {code} {size}\n")

    @property
    def cfg(self) -> argparse.Namespace:
        return self.server.cfg  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        self.handle_proxy()

    def do_POST(self) -> None:
        self.handle_proxy()

    def do_PUT(self) -> None:
        self.handle_proxy()

    def do_PATCH(self) -> None:
        self.handle_proxy()

    def do_DELETE(self) -> None:
        self.handle_proxy()

    def do_OPTIONS(self) -> None:
        self.handle_proxy()

    def strip_prefix(self, path: str) -> tuple[str, str]:
        for prefix in self.cfg.prefix:
            normalized = prefix.rstrip("/")
            if path == normalized:
                return normalized, "/"
            if path.startswith(normalized + "/"):
                stripped = path[len(normalized) :]
                return normalized, stripped or "/"
        return "", path

    def read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        if length > self.cfg.max_body_bytes:
            raise ValueError(f"request body too large for capture: {length}")
        return self.rfile.read(length)

    def build_forward_headers(self, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key in self.headers.keys():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
                continue
            value = self.headers.get(key)
            if value is not None:
                headers[key] = value
        headers["Host"] = self.cfg.upstream_host_header
        headers["Content-Length"] = str(len(body))
        headers["X-Sub2API-Capture"] = "1"
        return headers

    def handle_proxy(self) -> None:
        started = time.time()
        capture_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{safe_name(self.command)}"
        request_body = b""
        response_prefix = bytearray()
        upstream_status = 502
        upstream_headers: dict[str, list[str]] = {}
        error: str | None = None

        parsed = urllib.parse.urlsplit(self.path)
        prefix, upstream_path = self.strip_prefix(parsed.path)
        upstream_url_path = urllib.parse.urlunsplit(("", "", upstream_path, parsed.query, ""))
        capture_path = Path(self.cfg.log_dir) / f"{capture_id}.json"

        try:
            request_body = self.read_request_body()
            conn = http.client.HTTPConnection(self.cfg.upstream_host, self.cfg.upstream_port, timeout=self.cfg.upstream_timeout)
            conn.request(self.command, upstream_url_path, body=request_body, headers=self.build_forward_headers(request_body))
            resp = conn.getresponse()
            upstream_status = resp.status
            upstream_headers = sanitize_headers(resp.headers)
            has_content_length = any(key.lower() == "content-length" for key, _ in resp.getheaders())
            if not has_content_length:
                self.close_connection = True

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            if not has_content_length:
                self.send_header("Connection", "close")
            self.send_header("X-Sub2API-Capture-ID", capture_id)
            self.end_headers()

            while True:
                chunk = resp.read(self.cfg.chunk_size)
                if not chunk:
                    break
                if len(response_prefix) < self.cfg.max_response_capture_bytes:
                    remain = self.cfg.max_response_capture_bytes - len(response_prefix)
                    response_prefix.extend(chunk[:remain])
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()
        except Exception as exc:
            error = repr(exc)
            if not self.wfile.closed:
                body = json.dumps({"error": "capture_proxy_error", "capture_id": capture_id}, ensure_ascii=False).encode()
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("X-Sub2API-Capture-ID", capture_id)
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    pass
        finally:
            content_type = self.headers.get("Content-Type", "")
            record = {
                "capture_id": capture_id,
                "captured_at": now_iso(),
                "duration_ms": round((time.time() - started) * 1000, 2),
                "client": {
                    "remote_addr": self.client_address[0],
                    "x_forwarded_for": self.headers.get("X-Forwarded-For", ""),
                },
                "request": {
                    "method": self.command,
                    "original_path": sanitize_url_for_log(self.path),
                    "matched_prefix": prefix,
                    "upstream_path": sanitize_url_for_log(upstream_url_path),
                    "headers": sanitize_headers(self.headers),
                    "body_sha256": hashlib.sha256(request_body).hexdigest() if request_body else "",
                    "body": sanitize_body(request_body, content_type),
                },
                "response": {
                    "status": upstream_status,
                    "headers": upstream_headers,
                    "captured_bytes": len(response_prefix),
                    "event_types": event_types_from_sse(bytes(response_prefix)),
                    "body_prefix": response_prefix.decode("utf-8", "replace"),
                },
                "error": error,
            }
            write_json(capture_path, record)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sub2API 官方客户端采样反代")
    parser.add_argument("--listen-host", default=os.getenv("CAPTURE_LISTEN_HOST", "127.0.0.1"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("CAPTURE_LISTEN_PORT", "38081")))
    parser.add_argument("--upstream-host", default=os.getenv("CAPTURE_UPSTREAM_HOST", "127.0.0.1"))
    parser.add_argument("--upstream-port", type=int, default=int(os.getenv("CAPTURE_UPSTREAM_PORT", "3001")))
    parser.add_argument("--upstream-host-header", default=os.getenv("CAPTURE_UPSTREAM_HOST_HEADER", "bwg.3ab.in"))
    parser.add_argument("--prefix", action="append", default=os.getenv("CAPTURE_PREFIXES", "/capture,/official-capture,/__capture").split(","))
    parser.add_argument("--log-dir", default=os.getenv("CAPTURE_LOG_DIR", "/var/log/sub2api-capture"))
    parser.add_argument("--chunk-size", type=int, default=int(os.getenv("CAPTURE_CHUNK_SIZE", "8192")))
    parser.add_argument("--max-body-bytes", type=int, default=int(os.getenv("CAPTURE_MAX_BODY_BYTES", str(64 * 1024 * 1024))))
    parser.add_argument("--max-response-capture-bytes", type=int, default=int(os.getenv("CAPTURE_MAX_RESPONSE_CAPTURE_BYTES", str(512 * 1024))))
    parser.add_argument("--upstream-timeout", type=int, default=int(os.getenv("CAPTURE_UPSTREAM_TIMEOUT", "900")))
    args = parser.parse_args()
    args.prefix = [p.strip() for p in args.prefix if p.strip()]
    return args


def main() -> None:
    args = parse_args()
    Path(args.log_dir).mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer((args.listen_host, args.listen_port), CaptureProxyHandler) as httpd:
        httpd.cfg = args  # type: ignore[attr-defined]
        sys.stderr.write(f"{now_iso()} listen={args.listen_host}:{args.listen_port} upstream={args.upstream_host}:{args.upstream_port}\n")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
