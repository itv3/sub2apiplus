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
import urllib.parse
from pathlib import Path
from typing import Any

from mitmproxy import http


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


def _body_summary(content: bytes | None) -> dict[str, Any]:
    raw = content or b""
    text = raw.decode("utf-8", "replace")
    return {
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
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
        "_task": TASK,
        "_boundary": BOUNDARY,
        "_run_id": RUN_ID,
        "_subject": SUBJECT,
        "_scenario": SCENARIO,
    }


class OfficialClientCapture:
    """仅记录当前任务 allowlist 中的目标主机。"""

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.request.host.lower() not in TARGET_HOSTS:
            return
        category = _classify(flow.request.path)
        payload = {
            **_common_payload(),
            "_category": category,
            "request": {
                "method": flow.request.method,
                "scheme": flow.request.scheme,
                "host": flow.request.host,
                "port": flow.request.port,
                "path": _safe_path(flow.request.path),
                "http_version": flow.request.http_version,
                "headers": _headers_as_pairs(flow.request.headers),
                "body": _body_summary(flow.request.raw_content),
            },
            "response": (
                {
                    "status": flow.response.status_code,
                    "http_version": flow.response.http_version,
                    "headers": _headers_as_pairs(flow.response.headers),
                    "body": _body_summary(flow.response.raw_content),
                }
                if flow.response
                else None
            ),
        }
        _append_json_line(OUTPUT_DIR / f"{category}-http.jsonl", payload)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if flow.request.host.lower() not in TARGET_HOSTS or not flow.websocket:
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
            "host": flow.request.host,
            "path": _safe_path(flow.request.path),
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "text": text,
            "json": _parse_json(text),
        }
        _append_json_line(OUTPUT_DIR / "codex-ws.jsonl", payload)


_ensure_output_directory()
addons = [OfficialClientCapture()]
