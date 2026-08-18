"""记录官方 CLI 的 OAuth 或 API forward-MITM HTTP/WS 形态。

授权类 Header 会脱敏；正文和 WS 帧仍属于原始敏感证据，只能保存于 0700
运行目录，不能提交到 Git。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

from mitmproxy import http
from mitmproxy.net import encoding


SENSITIVE_HEADERS = {
    "anthropic-auth-token",
    "api-key",
    "authorization",
    "cookie",
    "openai-api-key",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-goog-api-key",
    "x-openai-actor-authorization",
}
SENSITIVE_NAME_RE = re.compile(
    r"(authorization|api[-_]?key|cookie|token|secret|password|credential)", re.I
)
TASK = os.environ["CAPTURE_TASK"]
BOUNDARY = os.environ["CAPTURE_BOUNDARY"]
RUN_ID = os.environ["CAPTURE_RUN_ID"]
SUBJECT = os.environ["CAPTURE_SUBJECT"]
SCENARIO = os.environ["CAPTURE_SCENARIO"]
OUTPUT_DIR = Path(os.environ["CAPTURE_OUTPUT_DIR"])
TARGET_HOSTS = {
    value.strip().lower()
    for value in os.environ["CAPTURE_TARGET_HOSTS"].split(",")
    if value.strip()
}
HOST_SCOPE = os.environ.get("CAPTURE_HOST_SCOPE", "targets")
if HOST_SCOPE not in {"all", "targets"}:
    raise RuntimeError("CAPTURE_HOST_SCOPE 只能是 all 或 targets")


def _should_record_host(host: str) -> bool:
    """FW-E 可记录官方进程的全部 host；普通抓包保持既有目标范围。"""

    return HOST_SCOPE == "all" or host.strip().lower() in TARGET_HOSTS


def _ensure_output_directory() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUTPUT_DIR.chmod(0o700)


def _append_json_line(path: Path, payload: dict[str, Any]) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            raise OSError("MITM 输出必须是当前用户拥有的独立普通文件。")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _redact_value(value: str) -> str:
    return f"<redacted len={len(value.strip())}>" if value.strip() else ""


def _headers_as_pairs(headers: Any) -> list[list[str]]:
    pairs: list[list[str]] = []
    for key, value in headers.items(multi=True):
        safe_value = (
            _redact_value(value)
            if key.lower() in SENSITIVE_HEADERS or SENSITIVE_NAME_RE.search(key)
            else value
        )
        pairs.append([key, safe_value])
    return pairs


def _safe_path(value: str) -> str:
    """保留路径结构，并清除 query 中疑似凭据的值。"""

    parsed = urllib.parse.urlsplit(value)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    safe_query = [
        (key, "<redacted>" if SENSITIVE_NAME_RE.search(key) else item_value)
        for key, item_value in query
    ]
    return urllib.parse.urlunsplit(
        ("", "", parsed.path, urllib.parse.urlencode(safe_query), "")
    )


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _body_summary(content: bytes | None, content_encoding: str = "") -> dict[str, Any]:
    raw = content or b""
    normalized_encoding = content_encoding.strip().lower()
    decoded = raw
    decode_error = ""
    if normalized_encoding:
        try:
            decoded = encoding.decode(raw, normalized_encoding)
        except Exception as error:  # pragma: no cover - 由真实 MITM 版本决定支持集
            decode_error = type(error).__name__
    text = decoded.decode("utf-8", "replace")
    return {
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_encoding": normalized_encoding,
        "decoded_length": len(decoded),
        "decoded_sha256": hashlib.sha256(decoded).hexdigest(),
        "decode_error": decode_error,
        "text": text,
        "json": _parse_json(text),
    }


def _classify(path: str) -> str:
    if "/responses" in path:
        return "codex"
    if "/messages" in path:
        return "claude"
    if "/models" in path:
        return "models"
    return "misc"


def _common_payload() -> dict[str, Any]:
    return {
        "_captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "_captured_monotonic_ns": time.monotonic_ns(),
        "_task": TASK,
        "_boundary": BOUNDARY,
        "_run_id": RUN_ID,
        "_subject": SUBJECT,
        "_scenario": SCENARIO,
        "_capture_host_scope": HOST_SCOPE,
    }


def _lifecycle_path(category: str) -> Path:
    """生命周期事件写独立文件。

    扩展名刻意不是 `.jsonl`：`analysis.normalize_mitm_directory` 会 glob 目录下所有
    `*.jsonl` 并按「一条 HTTP exchange」规范化，而生命周期记录没有完整的
    request/response 对，混进去会破坏既有的 J 规范化产物。
    """

    return OUTPUT_DIR / f"lifecycle-{category}.ndjson"


def _record_lifecycle(event: str, flow: Any, extra: dict[str, Any] | None = None) -> None:
    """记录一次流生命周期事件，带 flow ID 以便与 HTTP 记录关联。"""

    request = getattr(flow, "request", None)
    host = getattr(request, "host", "") or ""
    if not _should_record_host(host):
        return
    category = _classify(getattr(request, "path", "") or "")
    payload = {
        **_common_payload(),
        "_category": category,
        "_event": event,
        "_flow_id": getattr(flow, "id", None),
        "method": getattr(request, "method", None),
        "scheme": getattr(request, "scheme", None),
        "host": host,
        "port": getattr(request, "port", None),
        "path": _safe_path(getattr(request, "path", "") or ""),
        "http_version": getattr(request, "http_version", None),
    }
    if extra:
        payload.update(extra)
    _append_json_line(_lifecycle_path(category), payload)


def _record_connection(event: str, data: Any) -> None:
    """记录上游连接生命周期。没有 flow 可关联，只落地址与时间。"""

    conn = getattr(data, "server", None)
    address = getattr(conn, "address", None)
    host = str(address[0]).lower() if isinstance(address, tuple) and address else ""
    if host and TARGET_HOSTS and not any(host.endswith(t) or t in host for t in TARGET_HOSTS):
        # 地址可能是 IP，无法可靠匹配域名；匹配不上时仍记录，由分析侧判定。
        pass
    _append_json_line(OUTPUT_DIR / "lifecycle-connection.ndjson", {
        **_common_payload(),
        "_event": event,
        "server_address": str(address or ""),
    })


# 受控错误注入：CAPTURE_FAULT_SPEC 形如 "status=500,count=1" 或 "kill=1"。
# 重试链路无法靠自然采集获得——服务端不会按需返回 5xx，也不会按需断连。要给
# X-Stainless-Retry-Count 递增这类命题取正例，只能在 MITM 层受控注入故障。
# 注入产生的样本只能证明「客户端收到该输入后的反应」，不等于自然成功链，
# 引用时必须声明这一点。
_FAULT_SPEC: dict[str, str] = {}
for _item in (os.environ.get("CAPTURE_FAULT_SPEC") or "").split(","):
    if "=" in _item:
        _k, _, _v = _item.partition("=")
        _FAULT_SPEC[_k.strip()] = _v.strip()
_FAULT_BUDGET = int(_FAULT_SPEC.get("count") or 0)


def _is_fault_target(flow: http.HTTPFlow) -> bool:
    """只对目标主机的模型 POST 注入故障，避免后台设置请求消耗预算。"""

    request = getattr(flow, "request", None)
    if request is None:
        return False
    return (
        str(getattr(request, "host", "")).lower() in TARGET_HOSTS
        and str(getattr(request, "method", "")).upper() == "POST"
        and _classify(str(getattr(request, "path", ""))) in {"claude", "codex"}
    )


class OfficialClientCapture:
    """按显式 host scope 记录目标主机或官方进程全部代理出站。"""

    def __init__(self) -> None:
        self._faults_left = _FAULT_BUDGET

    def _maybe_inject_fault(self, flow: http.HTTPFlow) -> bool:
        """按预算注入一次故障，返回是否已注入。"""

        if self._faults_left <= 0 or not _is_fault_target(flow):
            return False
        self._faults_left -= 1
        if _FAULT_SPEC.get("kill"):
            _record_lifecycle("fault_kill", flow, {"remaining_budget": self._faults_left})
            flow.kill()
            return True
        status = int(_FAULT_SPEC.get("status") or 0)
        if status:
            _record_lifecycle("fault_status", flow,
                              {"injected_status": status, "remaining_budget": self._faults_left})
            flow.response = http.Response.make(
                status, b'{"type":"error","error":{"type":"api_error",'
                        b'"message":"injected fault for retry observation"}}',
                {"content-type": "application/json"})
            return True
        return False

    def request(self, flow: http.HTTPFlow) -> None:
        """请求离开客户端即落盘。

        只有 `response` 钩子时，任何没有收到完整响应的流——断连、超时、被中止的
        重试——都不会留下任何痕迹，retry 与连接生命周期命题因此无法取证。
        """

        _record_lifecycle("request", flow, {
            "header_names": [name for name, _ in _headers_as_pairs(flow.request.headers)],
            "content_length": flow.request.headers.get("content-length"),
            "retry_count": flow.request.headers.get("x-stainless-retry-count"),
        })
        self._maybe_inject_fault(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        """连接错误、超时或被中止时落盘，记录是否已有响应。"""

        error = getattr(flow, "error", None)
        _record_lifecycle("error", flow, {
            "error": str(getattr(error, "msg", error) or "")[:400],
            "had_response": bool(getattr(flow, "response", None)),
            "response_status": getattr(getattr(flow, "response", None), "status_code", None),
        })

    def server_connect(self, data: Any) -> None:
        """上游连接建立。

        mitmproxy 8.x 的 ServerConnectionHookData 不带 flow，因此这里不做 host
        allowlist 过滤也无法关联 flow，只记录地址与时间，用于判断连接复用次数。
        """

        _record_connection("server_connect", data)

    def server_disconnected(self, data: Any) -> None:
        """上游连接关闭，用于判断连接复用与提前断开。"""

        _record_connection("server_disconnected", data)

    def response(self, flow: http.HTTPFlow) -> None:
        if not _should_record_host(flow.request.host):
            return
        category = _classify(flow.request.path)
        payload = {
            **_common_payload(),
            "_category": category,
            # 与 lifecycle-*.ndjson 中同一条流的事件关联。
            "_flow_id": getattr(flow, "id", None),
            "request": {
                "method": flow.request.method,
                "scheme": flow.request.scheme,
                "host": flow.request.host,
                "port": flow.request.port,
                "path": _safe_path(flow.request.path),
                "http_version": flow.request.http_version,
                "headers": _headers_as_pairs(flow.request.headers),
                "body": _body_summary(
                    flow.request.raw_content,
                    flow.request.headers.get("content-encoding", ""),
                ),
            },
            "response": (
                {
                    "status": flow.response.status_code,
                    "http_version": flow.response.http_version,
                    "headers": _headers_as_pairs(flow.response.headers),
                    "body": _body_summary(
                        flow.response.raw_content,
                        flow.response.headers.get("content-encoding", ""),
                    ),
                }
                if flow.response
                else None
            ),
        }
        _append_json_line(OUTPUT_DIR / f"{category}-http.jsonl", payload)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not _should_record_host(flow.request.host) or not flow.websocket:
            return
        message = flow.websocket.messages[-1]
        raw = getattr(message, "content", b"") or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        text = raw.decode("utf-8", "replace")
        payload = {
            **_common_payload(),
            "_category": _classify(flow.request.path),
            "_websocket": True,
            "from_client": bool(getattr(message, "from_client", False)),
            "scheme": flow.request.scheme,
            "host": flow.request.host,
            "port": flow.request.port,
            "path": _safe_path(flow.request.path),
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": text,
            "json": _parse_json(text),
        }
        _append_json_line(OUTPUT_DIR / "codex-ws.jsonl", payload)


_ensure_output_directory()
addons = [OfficialClientCapture()]
