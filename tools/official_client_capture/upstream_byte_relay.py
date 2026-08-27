#!/usr/bin/env python3
"""真实上游应用字节中继（验证方案 §4.3 的 R 类观测通道）。

存在的理由
----------
此前两种通道各有硬伤：

  - **MITM 代理**必然协商 h2，HPACK 会把 header 强制小写并重排，因此 h1 的大小写
    与顺序**完全不可见**；mitmproxy 还会用自己的 h2 栈重建连接，客户端原始的
    SETTINGS 集合与帧内顺序在转发后丢失。
  - **终结型协议探针**自己应答、不转发上游，客户端
    拿不到真实响应就不会有后续动作，凡是依赖模型自主决策的场景（工具调用、生图、
    上下文压缩）请求根本发不出来。

本中继两条 TLS 腿之间**只复制明文应用字节**——不解析、不修改、不重建 HTTP。
因此既有真实交互（能触发完整状态链），又完整保留 h1 的字面大小写与顺序、h2 的
原始帧与 HPACK 动态表演进、WS 的握手与分帧。

它能证明什么、不能证明什么
--------------------------
**能**：h1 请求行/header 字面量/顺序/重复项/body 原始字节；h2 preface、帧序、
SETTINGS、WINDOW_UPDATE、HPACK 原始块；WS 握手与帧；真实上游响应与多轮交互。

**不能**：客户端直连真实上游时的 ServerHello、证书、record 分片与 TCP 时序，
以及 TLS session resumption——这些仍须由被动 pcap（N0）负责。

ALPN 镜像
---------
中继**不得**固定向上游 offer 一个协议列表。必须先窥探客户端实际 offer，再用
**同一列表**与上游握手；客户端没 offer 就不 offer。任一侧协商结果不一致即终止
连接并把该次运行标记为无效——否则会把客户端逼上它本来不走的协议，这本身就是污染。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import email.utils
import hashlib
import json
import os
import re
import signal
import ssl
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# ClientHello 的 ALPN 扩展编号（RFC 7301）。
_EXT_ALPN = 0x0010

# 受控候选辅助抓包使用固定、无凭据语义的关联值。值本身只用于证明第一跳响应驱动
# 第二跳请求，验收产物对外只保留 SHA-256，不把它们当成生产对象标识。
_SYNTHETIC_REALTIME_CALL_ID = "call_candidate_aux_0145"
_SYNTHETIC_FILE_ID = "file_candidate_aux_0145"
_SYNTHETIC_FILE_HOST = "region-candidate-0145.oaiusercontent.com"
_SYNTHETIC_FILE_QUERY = "sv=candidate0145&sig=local-only"
_SYNTHETIC_AUX_TURN_STATE = "turn-state-candidate-aux-0145"
_SYNTHETIC_AUX_CFUV_COOKIE = (
    "_cfuvid=candidate-aux-0145; Path=/; Domain=.chatgpt.com; "
    "Secure; HttpOnly; SameSite=None"
)
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_SYNTHETIC_CORE_TURN_STATE = "turn-state-candidate-core-0145"
_SYNTHETIC_CORE_CFUV_COOKIE = (
    "_cfuvid=candidate-core-0145; Path=/; Domain=.chatgpt.com; "
    "Secure; HttpOnly; SameSite=None"
)
_SYNTHETIC_CORE_SCENARIOS = frozenset(
    {"A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"}
)
_SYNTHETIC_CORE_WS_SCENARIOS = frozenset({"A05", "A06", "A07"})
_SYNTHETIC_CORE_HTTP_SCENARIOS = frozenset(
    {"A03", "A04", "A07", "A08", "A10", "A15"}
)
_SYNTHETIC_CLAUDE_PLANS = frozenset(
    {
        "always-529",
        "disconnect-retry",
        "fallback-model",
        "nonretry-400",
        "nonretry-403",
        "retry-401",
        "retry-408",
        "retry-409",
        "retry-429",
        "retry-429-after-date",
        "retry-429-after-seconds",
        "retry-500",
        "retry-502",
        "retry-503",
        "retry-529",
        "stall",
        "stream-404-fallback",
        "stream-interrupt-fallback",
        "stream-interrupt-no-fallback",
    }
)
_SYNTHETIC_CLAUDE_V4_AUX_PLANS = frozenset({"oauth-refresh-reject"})
_ALL_SYNTHETIC_CLAUDE_PLANS = (
    _SYNTHETIC_CLAUDE_PLANS | _SYNTHETIC_CLAUDE_V4_AUX_PLANS
)
_SYNTHETIC_CLAUDE_SUCCESS_MARKERS = frozenset({"FW_F_V3_OK", "FW_F_V4_OK"})


@dataclass(frozen=True)
class SyntheticAuxResponse:
    """候选辅助端点的一次纯本地响应。

    ``wire`` 是发给候选服务的完整 H1 响应。``terminal_ws_frame`` 只用于 realtime
    sideband：101 完成后立即发送 ``session.ended``，让生产 observer 走真实终止清理。
    """

    action: str
    wire: bytes
    terminal_ws_frame: bytes = b""


@dataclass(frozen=True)
class SyntheticCoreResponse:
    """候选核心场景的一次纯本地 HTTP 或 WS 握手响应。"""

    action: str
    wire: bytes
    websocket: bool = False
    set_cookie_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class SyntheticClaudeResponse:
    """Claude 官方二进制故障探针的一次纯本地响应。"""

    action: str
    wire: bytes
    delay_seconds: float = 0.0


def _h1_response(
    status: int,
    reason: str,
    body: bytes = b"",
    *,
    content_type: str = "application/json",
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    """构造长度明确且主动断连的受控 H1 响应。"""

    lines = [f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")]
    if content_type:
        lines.append(f"content-type: {content_type}\r\n".encode("ascii"))
    for name, value in headers:
        lines.append(f"{name}: {value}\r\n".encode("ascii"))
    lines.extend((
        f"content-length: {len(body)}\r\n".encode("ascii"),
        b"connection: close\r\n\r\n",
        body,
    ))
    return b"".join(lines)


def _claude_error_response(
    status: int,
    error_type: str,
    message: str,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    """构造 Anthropic SDK 能识别的冻结错误响应。"""

    body = json.dumps(
        {
            "type": "error",
            "error": {"type": error_type, "message": message},
            "request_id": f"req_fw_f_v3_{status}",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    reasons = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        408: "Request Timeout",
        409: "Conflict",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
        529: "Overloaded",
    }
    return _h1_response(
        status,
        reasons[status],
        body,
        headers=(("request-id", f"req_fw_f_v3_{status}"), *headers),
    )


def _claude_stream_success(model: str, text: str = "FW_F_V3_OK") -> bytes:
    """返回最小合法 Anthropic Messages SSE，供状态机完成一次真实 SDK 调用。"""

    message_id = "msg_fw_f_v3_synthetic"
    events = (
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    body = b"".join(
        f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode(
            "utf-8"
        )
        for event, payload in events
    )
    return _h1_response(
        200,
        "OK",
        body,
        content_type="text/event-stream",
        headers=(("request-id", "req_fw_f_v3_success"),),
    )


def _claude_nonstream_success(model: str, text: str = "FW_F_V3_OK") -> bytes:
    """返回流式降级后的最小非流式 Messages 响应。"""

    body = json.dumps(
        {
            "id": "msg_fw_f_v3_nonstream",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return _h1_response(
        200,
        "OK",
        body,
        headers=(("request-id", "req_fw_f_v3_nonstream"),),
    )


def _synthetic_claude_response(
    plan: str,
    host: str,
    request_line: str,
    body: bytes,
    ordinal: int,
    success_marker: str = "FW_F_V3_OK",
) -> SyntheticClaudeResponse | None:
    """按冻结计划响应 Claude `/v1/messages`，未知端点和计划一律拒绝。"""

    if plan not in _ALL_SYNTHETIC_CLAUDE_PLANS:
        return None
    if success_marker not in _SYNTHETIC_CLAUDE_SUCCESS_MARKERS:
        return None
    normalized_host = host.lower().rstrip(".")
    if plan == "oauth-refresh-reject":
        if (
            normalized_host != "platform.claude.com"
            or request_line != "POST /v1/oauth/token HTTP/1.1"
        ):
            return None
        body = json.dumps(
            {
                "error": "invalid_grant",
                "error_description": "fw-f v4 controlled refresh rejection",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return SyntheticClaudeResponse(
            "oauth_refresh_rejected",
            _h1_response(400, "Bad Request", body),
        )
    if normalized_host != "api.anthropic.com":
        return None
    if request_line == "HEAD /api/hello HTTP/1.1":
        return SyntheticClaudeResponse(
            "hello_success",
            _h1_response(200, "OK", b"", content_type=""),
        )
    auxiliary_absent = {
        "GET /api/claude_code/policy_limits HTTP/1.1": "policy_limits_absent",
        "GET /api/claude_code/settings HTTP/1.1": "remote_settings_absent",
    }
    if request_line in auxiliary_absent:
        # 2.1.88 源码明确把 404 定义为“没有组织 policy／远程托管设置”，
        # 客户端会按空对象继续启动。这里只为让合成故障探针到达 messages；请求
        # 本身仍完整落入 R，响应不含任何账号事实，也不会连接生产控制面。
        return SyntheticClaudeResponse(
            auxiliary_absent[request_line],
            _claude_error_response(
                404,
                "not_found_error",
                "fw-f v3 controlled auxiliary absence",
            ),
        )
    if request_line != "POST /v1/messages?beta=true HTTP/1.1":
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    model = str(payload.get("model", "claude-sonnet-5"))
    streaming = payload.get("stream") is True

    retry_status = {
        "retry-401": (401, "authentication_error"),
        "retry-408": (408, "timeout_error"),
        "retry-409": (409, "api_error"),
        "retry-429": (429, "rate_limit_error"),
        "retry-429-after-date": (429, "rate_limit_error"),
        "retry-429-after-seconds": (429, "rate_limit_error"),
        "retry-500": (500, "api_error"),
        "retry-502": (502, "api_error"),
        "retry-503": (503, "api_error"),
        "retry-529": (529, "overloaded_error"),
    }
    if plan in retry_status and ordinal == 1:
        status, error_type = retry_status[plan]
        headers: tuple[tuple[str, str], ...] = ()
        if plan == "retry-429-after-seconds":
            headers = (("retry-after", "1"),)
        elif plan == "retry-429-after-date":
            headers = (("retry-after", email.utils.formatdate(time.time() + 2, usegmt=True)),)
        return SyntheticClaudeResponse(
            f"{plan}_fault",
            _claude_error_response(status, error_type, f"fw-f v3 {plan}", headers=headers),
        )
    if plan in retry_status:
        return SyntheticClaudeResponse(
            f"{plan}_success", _claude_stream_success(model, success_marker)
        )

    if plan == "nonretry-400":
        return SyntheticClaudeResponse(
            "nonretry_400",
            _claude_error_response(400, "invalid_request_error", "fw-f v3 400"),
        )
    if plan == "nonretry-403":
        return SyntheticClaudeResponse(
            "nonretry_403",
            _claude_error_response(403, "permission_error", "fw-f v3 403"),
        )
    if plan == "always-529":
        return SyntheticClaudeResponse(
            "always_529",
            _claude_error_response(529, "overloaded_error", "fw-f v3 retry limit"),
        )
    if plan == "fallback-model":
        if "haiku" in model.lower():
            return SyntheticClaudeResponse(
                "fallback_model_success", _claude_stream_success(model, success_marker)
            )
        return SyntheticClaudeResponse(
            "fallback_primary_529",
            _claude_error_response(529, "overloaded_error", "fw-f v3 fallback"),
        )
    if plan == "stream-404-fallback":
        if streaming:
            return SyntheticClaudeResponse(
                "stream_404",
                _claude_error_response(404, "not_found_error", "fw-f v3 stream 404"),
            )
        return SyntheticClaudeResponse(
            "nonstream_fallback_success",
            _claude_nonstream_success(model, success_marker),
        )
    if plan in {"stream-interrupt-fallback", "stream-interrupt-no-fallback"}:
        if ordinal == 1:
            partial = (
                b"HTTP/1.1 200 OK\r\n"
                b"content-type: text/event-stream\r\n"
                b"connection: close\r\n\r\n"
                b"event: message_start\n"
                b'data: {"type":"message_start","message":'
            )
            return SyntheticClaudeResponse("stream_interrupted", partial)
        if streaming:
            return SyntheticClaudeResponse(
                "stream_retry_404",
                _claude_error_response(404, "not_found_error", "fw-f v3 force fallback"),
            )
        return SyntheticClaudeResponse(
            "interrupt_nonstream_success",
            _claude_nonstream_success(model, success_marker),
        )
    if plan == "disconnect-retry":
        if ordinal == 1:
            return SyntheticClaudeResponse("disconnect_without_response", b"")
        return SyntheticClaudeResponse(
            "disconnect_retry_success", _claude_stream_success(model, success_marker)
        )
    if plan == "stall":
        return SyntheticClaudeResponse("stall_without_response", b"", delay_seconds=3.0)
    return None


def _request_header_value(head: bytes, name: str) -> str:
    """大小写不敏感地读取一项 H1 header；只供受控响应器使用。"""

    prefix = name.lower().encode("ascii") + b":"
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip().decode("latin-1", "replace")
    return ""


def _redact_oauth_refresh_body(body: bytes) -> tuple[bytes, bool]:
    """在落盘前等长遮蔽 form/json 中的 refresh_token 值。

    A13 的 dummy token 只用于触发 ``invalid_grant``，不应成为持久化数据。替换不
    改变 body 长度，所以原请求的 Content-Length、字段顺序和字节偏移仍可复核。
    """

    patterns = (
        re.compile(rb"(?i)(^|&)(refresh_token=)([^&]*)"),
        re.compile(rb'(?i)("refresh_token"\s*:\s*")([^"]*)(")'),
    )
    redacted = body
    changed = False

    def placeholder(length: int) -> bytes:
        marker = b"<secret>"
        if length >= len(marker):
            return marker + b"X" * (length - len(marker))
        return b"X" * length

    def replace_form(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        changed = True
        return match.group(1) + match.group(2) + placeholder(len(match.group(3)))

    def replace_json(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        changed = True
        return match.group(1) + placeholder(len(match.group(2))) + match.group(3)

    redacted = patterns[0].sub(replace_form, redacted)
    redacted = patterns[1].sub(replace_json, redacted)
    return redacted, changed


def _synthetic_aux_response(
    host: str,
    request_line: str,
    head: bytes,
    body: bytes,
    codex_version: str,
    legacy_compact_ordinal: int = 0,
) -> SyntheticAuxResponse | None:
    """返回候选 A09/A11/A12/A13/A14 的白名单受控响应。

    返回 ``None`` 表示请求不在冻结白名单。调用方必须在这种情况下本地拒绝，绝不
    连接真实上游；这个 fail-closed 边界是 consume、OAuth 与文件 PUT 的安全前提。
    """

    del body  # 响应内容不由请求值派生，避免把凭据或预签名参数反射回产物。
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        return None
    method, target, _ = parts
    parsed = urlsplit(target)
    path = parsed.path
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    host = host.lower().rstrip(".")

    if host == "chatgpt.com":
        if (method, path, query_pairs) == (
            "GET",
            "/backend-api/codex/models",
            [("client_version", codex_version)],
        ):
            payload = (
                b'{"models":[{"slug":"gpt-5.6-luna","display_name":"GPT-5.6 Luna",'
                b'"use_responses_lite":true}]}'
            )
            return SyntheticAuxResponse(
                "models_manifest",
                _h1_response(200, "OK", payload, headers=(("etag", 'W/"candidate-aux-0145"'),)),
            )
        if method == "POST" and path == "/backend-api/codex/responses/compact" and not query_pairs:
            payload = (
                b'{"id":"cmp_candidate_aux","status":"completed","output":[],'
                b'"usage":{"input_tokens":7,"output_tokens":1,"total_tokens":8,'
                b'"input_tokens_details":{"cached_tokens":0}}}'
            )
            # compact 按 prime → default → beta → turn-state 顺序触发。models 端点
            # 不绑定账号 Cookie jar，故只能由同一 jar 的 prime compact 下发 Cookie；
            # beta 响应再下发 turn-state，使下一次请求由生产状态仓库自然回放。
            #
            # prime 只能按**到达序号**识别，不能看 turn-metadata 里的 capture_variant：
            # 网关按画像重新生成该 header，客户端塞进去的字段不会出现在出站字节里，
            # 基于它的判定恒为假，Cookie 因此从未下发过（k56 实测 A09 九个连接全无
            # cookie，EP-015／EP-022 的头序判据随之必败）。core 侧一直用 ordinal，
            # 这里对齐同一做法。
            headers: tuple[tuple[str, str], ...] = ()
            if legacy_compact_ordinal == 1:
                headers = (("set-cookie", _SYNTHETIC_AUX_CFUV_COOKIE),)
            elif _request_header_value(head, "x-codex-beta-features"):
                # turn-state 仍按 beta 头下发：该头是画像条件槽位的真实产物，
                # 出站确实存在（与 capture_variant 那种客户端自造字段不同）。
                headers = (("x-codex-turn-state", _SYNTHETIC_AUX_TURN_STATE),)
            return SyntheticAuxResponse(
                "legacy_compact",
                _h1_response(200, "OK", payload, headers=headers),
            )
        if method == "POST" and path == "/backend-api/codex/alpha/search" and not query_pairs:
            payload = b'{"encrypted_output":"candidate-aux","output":"ok","results":[]}'
            return SyntheticAuxResponse("alpha_search", _h1_response(200, "OK", payload))
        if method == "POST" and path in {
            "/backend-api/codex/images/generations",
            "/backend-api/codex/images/edits",
        } and not query_pairs:
            payload = (
                b'{"created":1710000145,"data":[{"b64_json":"Y2FuZGlkYXRlLWF1eA=="}],'
                b'"output_format":"png","usage":{"input_tokens":1,"output_tokens":1}}'
            )
            action = "images_generation" if path.endswith("generations") else "images_edit"
            return SyntheticAuxResponse(action, _h1_response(200, "OK", payload))
        if (method, path, query_pairs) == (
            "POST",
            "/backend-api/codex/realtime/calls",
            [("intent", "quicksilver"), ("architecture", "avas")],
        ):
            location = (
                "https://chatgpt.com/backend-api/codex/realtime/calls/"
                + _SYNTHETIC_REALTIME_CALL_ID
            )
            return SyntheticAuxResponse(
                "realtime_first_hop",
                _h1_response(
                    200,
                    "OK",
                    b"v=0\r\n",
                    content_type="application/sdp",
                    headers=(("location", location),),
                ),
            )
        if method == "GET" and path == "/backend-api/wham/usage" and not query_pairs:
            payload = (
                b'{"user_id":"candidate","account_id":"candidate","plan_type":"team",'
                b'"rate_limit":{"allowed":true,"limit_reached":false},'
                b'"rate_limit_reset_credits":{"available_count":1}}'
            )
            return SyntheticAuxResponse("wham_usage", _h1_response(200, "OK", payload))
        if method == "GET" and path == "/backend-api/wham/settings/user" and not query_pairs:
            # 目标画像存在该端点时，配额链路会在 usage 前读取一次用户设置。
            # 生产侧只把这次调用作为官方客户端行为收据，不消费响应字段，因此
            # 合成面返回最小 JSON 对象即可；路径、方法与无 query 仍严格白名单化。
            return SyntheticAuxResponse(
                "wham_settings_user",
                _h1_response(200, "OK", b"{}"),
            )
        if (method == "GET"
                and path == "/backend-api/wham/rate-limit-reset-credits"
                and not query_pairs):
            payload = b'{"available_count":1,"credits":[{"expires_at":"2099-01-01T00:00:00Z"}]}'
            return SyntheticAuxResponse("wham_credit_details", _h1_response(200, "OK", payload))
        if (method == "POST"
                and path == "/backend-api/wham/rate-limit-reset-credits/consume"
                and not query_pairs):
            payload = b'{"code":"ok","windows_reset":1,"credit":{"status":"redeemed"}}'
            return SyntheticAuxResponse("wham_safe_consume", _h1_response(200, "OK", payload))
        if method == "POST" and path == "/backend-api/files" and not query_pairs:
            upload_url = (
                f"https://{_SYNTHETIC_FILE_HOST}/candidate-aux/{_SYNTHETIC_FILE_ID}"
                f"?{_SYNTHETIC_FILE_QUERY}"
            )
            payload = json.dumps(
                {"file_id": _SYNTHETIC_FILE_ID, "upload_url": upload_url},
                separators=(",", ":"),
            ).encode("ascii")
            return SyntheticAuxResponse("files_create", _h1_response(200, "OK", payload))
        if (method == "POST"
                and path == f"/backend-api/files/{_SYNTHETIC_FILE_ID}/uploaded"
                and not query_pairs):
            payload = (
                b'{"status":"success","download_url":"https://download.invalid/candidate-aux",'
                b'"file_name":"candidate-aux.txt","mime_type":"text/plain"}'
            )
            return SyntheticAuxResponse("files_uploaded", _h1_response(200, "OK", payload))

    if host == "api.openai.com" and method == "GET" and path == "/v1/realtime":
        if query_pairs != [
            ("intent", "quicksilver"),
            ("call_id", _SYNTHETIC_REALTIME_CALL_ID),
        ]:
            return None
        if _request_header_value(head, "upgrade").lower() != "websocket":
            return None
        key = _request_header_value(head, "sec-websocket-key")
        if not key:
            return None
        accept = base64.b64encode(
            hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        response = (
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"upgrade: websocket\r\n"
            b"connection: Upgrade\r\n"
            + f"sec-websocket-accept: {accept}\r\n\r\n".encode("ascii")
        )
        terminal = _encode_server_text_frame('{"type":"session.ended"}')
        return SyntheticAuxResponse("realtime_sideband", response, terminal)

    if host == "auth.openai.com" and method == "POST" and path == "/oauth/token" and not query_pairs:
        payload = b'{"error":"invalid_grant","error_description":"synthetic acceptance response"}'
        return SyntheticAuxResponse(
            "oauth_dummy_invalid_grant",
            _h1_response(400, "Bad Request", payload),
        )

    if (host == _SYNTHETIC_FILE_HOST
            and method == "PUT"
            and path == f"/candidate-aux/{_SYNTHETIC_FILE_ID}"
            and query_pairs == [("sv", "candidate0145"), ("sig", "local-only")]):
        return SyntheticAuxResponse(
            "files_blob_put",
            _h1_response(201, "Created", b"", content_type=""),
        )
    return None


def _synthetic_core_models_response() -> SyntheticCoreResponse:
    """返回覆盖本轮 Lite/非 Lite 模型的冻结 manifest。"""

    payload = (
        b'{"models":['
        b'{"slug":"gpt-5.5","display_name":"GPT-5.5",'
        b'"use_responses_lite":false,"supports_parallel_tool_calls":true},'
        b'{"slug":"gpt-5.6-luna","display_name":"GPT-5.6 Luna",'
        b'"use_responses_lite":true,"supports_parallel_tool_calls":true},'
        b'{"slug":"gpt-5.6-sol","display_name":"GPT-5.6 Sol",'
        b'"use_responses_lite":true,"supports_parallel_tool_calls":true}'
        b']}'
    )
    return SyntheticCoreResponse(
        "models_manifest",
        _h1_response(
            200,
            "OK",
            payload,
            headers=(("etag", 'W/"candidate-core-0145"'),),
        ),
    )


def _synthetic_core_sse_response(
    scenario: str,
    ordinal: int,
) -> SyntheticCoreResponse:
    """返回最小但完整的 Responses SSE 成功终态。"""

    model = (
        "gpt-5.6-sol"
        if scenario == "A03" and ordinal >= 3
        else "gpt-5.5"
    )
    response_id = f"resp_candidate_core_{scenario.lower()}_{ordinal:04d}"
    completed = json.dumps(
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "model": model,
                "status": "completed",
                "output": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "total_tokens": 2,
                },
            },
        },
        separators=(",", ":"),
    ).encode("ascii")
    body = b"data: " + completed + b"\n\ndata: [DONE]\n\n"
    headers: tuple[tuple[str, str], ...] = ()
    set_cookie_names: tuple[str, ...] = ()
    if scenario == "A03" and ordinal == 1:
        # A03 先用一次非 Lite 请求建立真实冷 Cookie jar；只下发生产代码明确
        # allowlist 的 Cloudflare Cookie，后续请求必须由候选进程自己回放。
        headers = (("set-cookie", _SYNTHETIC_CORE_CFUV_COOKIE),)
        set_cookie_names = ("_cfuvid",)
    elif scenario == "A03" and ordinal == 3:
        # 前两次非 Lite 请求用于 prime/default；第三次才是 Lite 首轮，故 turn-state
        # 必须在这里下发，由第四次 Lite 请求证明回放闭环。
        headers = (("x-codex-turn-state", _SYNTHETIC_CORE_TURN_STATE),)
    action = "responses_http_fallback_success" if scenario == "A07" else "responses_http_success"
    return SyntheticCoreResponse(
        action,
        _h1_response(
            200,
            "OK",
            body,
            content_type="text/event-stream",
            headers=headers,
        ),
        set_cookie_names=set_cookie_names,
    )


def _synthetic_core_ws_handshake(
    scenario: str,
    head: bytes,
) -> SyntheticCoreResponse | None:
    """构造必须协商 permessage-deflate 的 Responses WS 101。"""

    if scenario not in _SYNTHETIC_CORE_WS_SCENARIOS:
        return None
    key = _request_header_value(head, "sec-websocket-key")
    extensions = _request_header_value(head, "sec-websocket-extensions")
    if (
        _request_header_value(head, "upgrade").lower() != "websocket"
        or not key
        or "permessage-deflate" not in extensions.lower()
    ):
        return None
    accept = base64.b64encode(
        hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    wire = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"upgrade: websocket\r\n"
        b"connection: Upgrade\r\n"
        + f"sec-websocket-accept: {accept}\r\n".encode("ascii")
        + b"sec-websocket-extensions: permessage-deflate\r\n\r\n"
    )
    return SyntheticCoreResponse("responses_ws_handshake_success", wire, websocket=True)


def _synthetic_core_response(
    scenario: str,
    host: str,
    request_line: str,
    head: bytes,
    body: bytes,
    ordinal: int,
    codex_version: str,
) -> SyntheticCoreResponse | None:
    """返回 A03/A04/A05/A06/A07/A08/A10/A15 的冻结白名单响应。"""

    del body
    if scenario not in _SYNTHETIC_CORE_SCENARIOS:
        return None
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1":
        return None
    method, target, _ = parts
    parsed = urlsplit(target)
    host = host.lower().rstrip(".")
    if host != "chatgpt.com":
        return None
    if (
        method == "GET"
        and parsed.path == "/backend-api/codex/models"
        and parse_qsl(parsed.query, keep_blank_values=True)
        == [("client_version", codex_version)]
    ):
        return _synthetic_core_models_response()
    if parsed.query or parsed.path != "/backend-api/codex/responses":
        return None
    if method == "GET":
        return _synthetic_core_ws_handshake(scenario, head)
    if method == "POST" and scenario in _SYNTHETIC_CORE_HTTP_SCENARIOS:
        return _synthetic_core_sse_response(scenario, ordinal)
    return None


def _encode_server_text_frame(text: str) -> bytes:
    """编码一条服务端→客户端的未压缩、未掩码 WS 文本帧。"""
    payload = text.encode("utf-8")
    length = len(payload)
    if length <= 125:
        head = bytes((0x81, length))
    elif length <= 0xFFFF:
        head = bytes((0x81, 126)) + struct.pack(">H", length)
    else:
        head = bytes((0x81, 127)) + struct.pack(">Q", length)
    return head + payload


def _decode_client_text_frame(frame: bytes) -> str | None:
    """解出一条完整客户端文本帧；仅用于核验受控注入的触发边界。"""
    if len(frame) < 6:
        return None
    first, second = frame[0], frame[1]
    fin = bool(first & 0x80)
    rsv1 = bool(first & 0x40)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    if not fin or opcode != 0x1 or not masked:
        return None

    length = second & 0x7F
    pos = 2
    if length == 126:
        if len(frame) < pos + 2:
            return None
        length = struct.unpack(">H", frame[pos:pos + 2])[0]
        pos += 2
    elif length == 127:
        if len(frame) < pos + 8:
            return None
        length = struct.unpack(">Q", frame[pos:pos + 8])[0]
        pos += 8
    if len(frame) != pos + 4 + length:
        return None
    mask = frame[pos:pos + 4]
    pos += 4
    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(frame[pos:]))
    if rsv1:
        try:
            inflater = zlib.decompressobj(wbits=-15)
            payload = inflater.decompress(payload + b"\x00\x00\xff\xff")
        except zlib.error:
            return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decode_websocket_frame_payload(
    frame: bytes,
) -> tuple[int, bool, bool, bytes] | None:
    """解出完整 WS 帧的 opcode、FIN、RSV1 与 payload。"""

    if len(frame) < 2:
        return None
    first, second = frame[0], frame[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    rsv1 = bool(first & 0x40)
    length = second & 0x7F
    pos = 2
    if length == 126:
        if len(frame) < pos + 2:
            return None
        length = struct.unpack(">H", frame[pos:pos + 2])[0]
        pos += 2
    elif length == 127:
        if len(frame) < pos + 8:
            return None
        length = struct.unpack(">Q", frame[pos:pos + 8])[0]
        pos += 8
    masked = bool(second & 0x80)
    mask = b""
    if masked:
        if len(frame) < pos + 4:
            return None
        mask = frame[pos:pos + 4]
        pos += 4
    if len(frame) != pos + length:
        return None
    payload = frame[pos:]
    if masked:
        payload = bytes(
            value ^ mask[index % 4] for index, value in enumerate(payload)
        )
    return opcode, fin, rsv1, payload


class _SyntheticCoreWebSocketDecoder:
    """按连接重组分片消息，并保留 permessage-deflate 上下文接管状态。"""

    def __init__(self) -> None:
        self._inflater = zlib.decompressobj(wbits=-15)
        self._opcode: int | None = None
        self._compressed = False
        self._payload = bytearray()

    def text(self, frame: bytes) -> str | None:
        parsed = _decode_websocket_frame_payload(frame)
        if parsed is None:
            raise ValueError("WS 帧结构无效")
        opcode, fin, rsv1, payload = parsed
        if opcode in {0x8, 0x9, 0xA}:
            if not fin or rsv1 or len(payload) > 125:
                raise ValueError("WS 控制帧结构无效")
            return None
        if opcode in {0x1, 0x2}:
            if self._opcode is not None:
                raise ValueError("上一条 WS 分片消息尚未结束")
            if opcode != 0x1:
                raise ValueError("核心合成响应只接受文本业务消息")
            self._opcode = opcode
            self._compressed = rsv1
            self._payload.extend(payload)
        elif opcode == 0x0:
            if self._opcode is None or rsv1:
                raise ValueError("WS continuation 缺少起始帧或错误设置 RSV1")
            self._payload.extend(payload)
        else:
            raise ValueError(f"不支持的 WS opcode: {opcode}")

        if not fin:
            return None
        if self._opcode is None:
            raise ValueError("WS 消息终帧缺少起始帧")
        message = bytes(self._payload)
        compressed = self._compressed
        self._opcode = None
        self._compressed = False
        self._payload.clear()
        if compressed:
            try:
                message = self._inflater.decompress(
                    message + b"\x00\x00\xff\xff"
                )
            except zlib.error as error:
                raise ValueError("permessage-deflate 解压失败") from error
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("WS 文本消息不是 UTF-8") from error


def _encode_server_control_frame(opcode: int, payload: bytes = b"") -> bytes:
    """编码服务端未掩码控制帧。"""

    if opcode not in {0x8, 0xA} or len(payload) > 125:
        raise ValueError("只允许长度不超过 125 的 CLOSE/PONG 控制帧")
    return bytes((0x80 | opcode, len(payload))) + payload


async def _read_websocket_frame(reader: asyncio.StreamReader) -> bytes:
    """从字节流读取一条完整 WS 帧，不跨帧插入受控数据。"""
    head = await reader.readexactly(2)
    length = head[1] & 0x7F
    extended = b""
    if length == 126:
        extended = await reader.readexactly(2)
        length = struct.unpack(">H", extended)[0]
    elif length == 127:
        extended = await reader.readexactly(8)
        length = struct.unpack(">Q", extended)[0]
    mask = await reader.readexactly(4) if head[1] & 0x80 else b""
    payload = await reader.readexactly(length)
    return head + extended + mask + payload


def parse_client_hello_alpn(data: bytes) -> list[str] | None:
    """从 ClientHello 原始字节里取出 ALPN offer 列表。

    返回 None 表示客户端未携带 ALPN 扩展——此时上游腿也必须不发 ALPN。
    解析失败同样返回 None：宁可不 offer，也不能臆造一个客户端没给的列表。
    """

    try:
        # TLS record: type(1) version(2) length(2)，随后是 handshake
        if len(data) < 43 or data[0] != 0x16:
            return None
        pos = 5
        if data[pos] != 0x01:  # handshake type: client_hello
            return None
        pos += 4  # handshake header
        pos += 2 + 32  # client_version + random
        pos += 1 + data[pos]  # session_id
        pos += 2 + struct.unpack(">H", data[pos:pos + 2])[0]  # cipher_suites
        pos += 1 + data[pos]  # compression_methods
        if pos + 2 > len(data):
            return None
        ext_end = pos + 2 + struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        while pos + 4 <= min(ext_end, len(data)):
            ext_type, ext_len = struct.unpack(">HH", data[pos:pos + 4])
            pos += 4
            if ext_type == _EXT_ALPN:
                body = data[pos:pos + ext_len]
                inner = struct.unpack(">H", body[:2])[0]
                out, cur = [], 2
                while cur < 2 + inner and cur < len(body):
                    n = body[cur]
                    out.append(body[cur + 1:cur + 1 + n].decode("ascii", "replace"))
                    cur += 1 + n
                return out or None
            pos += ext_len
    except (struct.error, IndexError, ValueError):
        return None
    return None


def _upstream_alpn_offer(
    assumed_offer: list[str] | None,
    selected_protocol: str | None,
    *,
    mirror_selected: bool,
) -> list[str] | None:
    """决定上游腿 ALPN；Claude 混合连接可显式按实际协商结果镜像。"""

    if not mirror_selected:
        return assumed_offer
    return [selected_protocol] if selected_protocol else None


def _should_synthesize_realtime_call(
    *,
    immediate: bool,
    after_live_attempts: int | None,
    attempt: int,
) -> bool:
    """判断本次 realtime 第一跳是否应由中继受控应答。

    旧开关保持「第一次立即合成」的兼容语义；延迟开关只允许先把指定次数的请求
    原样转发生产上游，再对紧随其后的那一次请求合成响应。A11 正式场景固定先转发
    一次自然请求，只有确认自然分支失败后才运行第二次驱动。
    """

    if attempt < 1:
        raise ValueError("realtime attempt 必须从 1 开始")
    if immediate:
        return attempt == 1
    return after_live_attempts is not None and attempt == after_live_attempts + 1


class ByteRecorder:
    """按方向分别落盘原始字节，并记录分片边界与哈希。

    分片边界要单独记：应用字节流本身不含"这是第几次 write"的信息，而分帧行为
    （例如 header 是否与 body 同一次写出）本身就是要观测的形态之一。
    """

    def __init__(self, out_dir: Path, conn_id: int):
        self.dir = out_dir
        self.conn_id = conn_id
        self.files: dict[str, object] = {}
        self.digests: dict[str, "hashlib._Hash"] = {}
        self.offsets: dict[str, int] = {}
        self.segments: list[dict] = []
        self.t0 = time.monotonic()
        self.opened_at_unix_ms = round(time.time() * 1000)

    def _stream(self, direction: str):
        if direction not in self.files:
            path = self.dir / f"conn{self.conn_id:03d}.{direction}.bin"
            fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            self.files[direction] = os.fdopen(fd, "wb")
            self.digests[direction] = hashlib.sha256()
            self.offsets[direction] = 0
        return self.files[direction]

    def write(self, direction: str, chunk: bytes) -> None:
        f = self._stream(direction)
        f.write(chunk)
        # **每次写入即落盘。** 旧版采集脚本用 pkill 停中继时，SIGTERM 不执行
        # finally 里的 close()，缓冲区字节就此丢失——表现为文件存在但长度为 0。这曾把 19 条
        # "上行丢字节"的连接误判成"官方预建的空闲连接"，据此写出了 SPEC-CONN-001
        # 的错误结论。吞吐损失可忽略，正确性优先。
        f.flush()
        self.digests[direction].update(chunk)
        self.segments.append({
            "direction": direction,
            "t_ms": round((time.monotonic() - self.t0) * 1000, 3),
            "offset": self.offsets[direction],
            "length": len(chunk),
        })
        self.offsets[direction] += len(chunk)

    def close(self) -> dict:
        for f in self.files.values():
            f.flush()
            f.close()
        return {
            "connection_id": self.conn_id,
            "bytes": {d: self.offsets[d] for d in self.offsets},
            "sha256": {d: h.hexdigest() for d, h in self.digests.items()},
            "segments": self.segments,
            # 绝对时刻。segments 里的 t_ms 是相对 monotonic 起点，无法与 pcap 的捕获
            # 时间比较；A14 要证明 create → 区域 PUT → uploaded 的先后，而区域 PUT
            # 直连不经中继、只在 pcap 里可见，必须有共同的墙钟基准才能跨两侧排序。
            "opened_at_unix_ms": self.opened_at_unix_ms,
            "closed_at_unix_ms": round(time.time() * 1000),
        }


@dataclass
class PreconnectedUpstream:
    """在官方客户端 5 秒模型目录超时开始前建好的真实上游 TLS 连接。"""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    target_host: str
    target_ip: str
    target_port: int
    alpn_offer: tuple[str, ...] | None
    selected_alpn: str | None
    connect_duration_ms: float


def _annotate_relay_stop_after_client_request(
    metadata: dict,
    *,
    stop_requested: bool,
) -> bool:
    """标记中继停机恰好落在单向请求完成窗口的候选终态。

    这里仅记录可验证事实，不判断请求是否完整。完整 H1 语法、文件哈希和方向缺失
    必须由取证 wrapper 在脱敏后重新核验；任何不满足精确形态的连接都保持原状并
    由门禁失败关闭。
    """

    byte_counts = metadata.get("bytes")
    digests = metadata.get("sha256")
    if (
        not stop_requested
        or metadata.get("valid") is not True
        or "error" in metadata
        or not isinstance(byte_counts, dict)
        or set(byte_counts) != {"client_to_upstream"}
        or not isinstance(byte_counts.get("client_to_upstream"), int)
        or byte_counts["client_to_upstream"] <= 0
        or not isinstance(digests, dict)
        or set(digests) != {"client_to_upstream"}
    ):
        return False
    metadata["termination_reason"] = (
        "relay_shutdown_after_complete_client_request_before_upstream_response"
    )
    metadata["relay_stop_requested"] = True
    return True


async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter,
               rec: ByteRecorder, direction: str) -> None:
    """单向复制。逐块转发并落盘，不做任何解析或缓冲重组。

    背压由 drain() 提供——不能无限缓存，否则慢消费端会把中继撑爆。
    """

    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                break
            rec.write(direction, chunk)
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
        pass
    finally:
        # 半关闭：只关写端，让对向继续把剩余数据送完。
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except (OSError, ConnectionError):
            pass


class Relay:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output)
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conn_seq = 0
        self.records: list[dict] = []
        self._forced_ws_fallback = False
        self._turn_state_injected = False
        self._ws_turn_state_injected = False
        self._synthesized_realtime_call = False
        self._realtime_call_attempts = 0
        self._realtime_call_lock = asyncio.Lock()
        self._core_http_responses = 0
        self._core_ws_handshakes = 0
        self._core_ws_response_creates = 0
        self._core_claude_messages = 0
        # aux 侧 legacy compact 的到达序号。prime 轮必须靠它识别：网关按画像重新
        # 生成 x-codex-turn-metadata，客户端放进去的 capture_variant 不会出现在
        # 出站字节里，任何基于该字段的判定都恒为假。
        self._core_aux_legacy_compacts = 0
        self._core_counter_lock = asyncio.Lock()
        self._stop_requested = False
        self._stop_event: asyncio.Event | None = None
        self._active_client_writers: set[asyncio.StreamWriter] = set()
        # CONN-001 受控 retry 探针的 attempt 计数必须跨 TCP 连接共享：
        # keepalive-500 预期 attempt 1/2 落在同一连接；disconnect 预期 attempt 1
        # 主动断开、attempt 2 落到新连接。锁用于避免两个并发连接抢到同一编号。
        self._retry_probe_attempts = 0
        self._retry_probe_lock = asyncio.Lock()
        self._preconnected_upstream: PreconnectedUpstream | None = None
        self._preconnected_upstream_lock = asyncio.Lock()
        self._preconnect_duration_ms: float | None = None

        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)

        # **SNI 路由**：一个中继同时覆盖多个域名。
        #
        # 验 SPEC-EP-002（域名分布）与 SPEC-EP-012（live 是双出站：
        # chatgpt.com 建会话 + api.openai.com 承载 sideband）都要求同时看到两个
        # 域名——单域名 hosts 劫持做不到，而那正是原证据成为循环论证的原因。
        #
        # 早前以为拿不到 SNI（asyncio 的 TransportSocket 不暴露 recv()，
        # 无法在握手前窥探 ClientHello），故只能 --assume-alpn 写死。
        # 实际上 `sni_callback` 在**握手期间**就会被调用并给出 servername，
        # 不需要窥探原始字节。
        self._last_sni: str | None = None
        # host=ip 映射由采集脚本在**劫持 hosts 之前**解析好传入
        self._dns_cache: dict[str, str] = {}
        for pair in (args.upstream_map or "").split(","):
            if "=" in pair:
                h, _, i = pair.partition("=")
                if h.strip() and i.strip():
                    self._dns_cache[h.strip()] = i.strip()
        self.ctx.sni_callback = self._on_sni

    async def _prepare_preconnected_upstream(self) -> None:
        """预建一次真实上游 TLS，避开官方模型目录请求的 5 秒硬超时。"""

        if not self.args.preconnect_upstream:
            return
        target_host = self.args.upstream_host
        target_ip = await self._resolve(target_host)
        target_port = 443
        offered = tuple(self.args.assume_alpn.split(",")) if self.args.assume_alpn else None
        context = ssl.create_default_context()
        if offered:
            context.set_alpn_protocols(list(offered))
        started = time.monotonic()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host=target_ip,
                port=target_port,
                ssl=context,
                server_hostname=target_host,
            ),
            timeout=self.args.preconnect_timeout,
        )
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        ssl_object = writer.get_extra_info("ssl_object")
        selected_alpn = ssl_object.selected_alpn_protocol() if ssl_object else None
        self._preconnected_upstream = PreconnectedUpstream(
            reader=reader,
            writer=writer,
            target_host=target_host,
            target_ip=target_ip,
            target_port=target_port,
            alpn_offer=offered,
            selected_alpn=selected_alpn,
            connect_duration_ms=duration_ms,
        )
        self._preconnect_duration_ms = duration_ms

    async def _take_preconnected_upstream(
        self,
        *,
        target_host: str,
        target_ip: str,
        target_port: int,
        alpn_offer: list[str] | None,
    ) -> PreconnectedUpstream | None:
        """只把预建连接交给主机、IP、端口与 ALPN 完全一致的首个客户端连接。"""

        async with self._preconnected_upstream_lock:
            candidate = self._preconnected_upstream
            if candidate is None:
                return None
            expected_offer = tuple(alpn_offer) if alpn_offer else None
            if (
                candidate.target_host != target_host
                or candidate.target_ip != target_ip
                or candidate.target_port != target_port
                or candidate.alpn_offer != expected_offer
                or candidate.writer.is_closing()
                or candidate.reader.at_eof()
            ):
                return None
            self._preconnected_upstream = None
            return candidate

    def _write_preconnect_ready(self) -> None:
        """在监听端口与预建 TLS 都就绪后写入无凭据就绪收据。"""

        if not self.args.preconnect_upstream:
            return
        path = self.out / "preconnect-ready.json"
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": "byte-relay-preconnect-ready/v1",
                    "status": "ready",
                    "upstream_host": self.args.upstream_host,
                    "connect_duration_ms": self._preconnect_duration_ms,
                },
                stream,
                ensure_ascii=False,
                sort_keys=True,
            )
            stream.write("\n")

    async def _close_unused_preconnected_upstream(self) -> None:
        """关闭未被客户端连接消费的预建上游，避免 relay 退出后遗留 TLS 会话。"""

        async with self._preconnected_upstream_lock:
            candidate = self._preconnected_upstream
            self._preconnected_upstream = None
        if candidate is None:
            return
        candidate.writer.close()
        try:
            await asyncio.wait_for(candidate.writer.wait_closed(), timeout=0.5)
        except (asyncio.TimeoutError, ConnectionError, ssl.SSLError, OSError):
            try:
                candidate.writer.transport.abort()
            except (AttributeError, OSError, RuntimeError):
                pass

    async def _claim_core_counter(self, name: str) -> int:
        """为并发核心连接分配稳定序号。"""

        async with self._core_counter_lock:
            attribute = f"_core_{name}"
            value = int(getattr(self, attribute)) + 1
            setattr(self, attribute, value)
            return value

    async def _claim_realtime_call_attempt(self) -> tuple[int, bool]:
        """原子分配 realtime 第一跳序号，避免并发连接抢占受控分支。"""

        async with self._realtime_call_lock:
            self._realtime_call_attempts += 1
            attempt = self._realtime_call_attempts
            synthesize = (
                not self._synthesized_realtime_call
                and _should_synthesize_realtime_call(
                    immediate=self.args.synthesize_realtime_call,
                    after_live_attempts=self.args.synthesize_realtime_call_after,
                    attempt=attempt,
                )
            )
            if synthesize:
                self._synthesized_realtime_call = True
        return attempt, synthesize

    def _log_synthetic_core(
        self,
        *,
        action: str,
        conn_id: int,
        request_line: str,
        **extra,
    ) -> None:
        self._log_intervention({
            "type": "synthetic_core_response",
            "profile": "candidate-core-v1",
            "scenario": self.args.candidate_core_scenario,
            "action": action,
            "connection_id": conn_id,
            "request_line": request_line,
            "production_forwarded": False,
            **extra,
        })

    async def _serve_synthetic_core_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        rec: ByteRecorder,
        conn_id: int,
        request_line: str,
        meta: dict,
    ) -> None:
        """核验真实 response.create 后回送最小成功事件，不制造客户端事实。"""

        decoder = _SyntheticCoreWebSocketDecoder()
        local_create_count = 0
        while local_create_count < 16:
            try:
                frame = await asyncio.wait_for(
                    _read_websocket_frame(reader),
                    timeout=45.0,
                )
            except asyncio.TimeoutError:
                if local_create_count:
                    return
                meta["error"] = "WS 101 后 45 秒内未收到 response.create"
                meta["valid"] = False
                return
            except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
                if local_create_count or self._stop_requested:
                    return
                meta["error"] = "WS 101 后连接在 response.create 前关闭"
                meta["valid"] = False
                return

            rec.write("client_to_upstream", frame)
            parsed = _decode_websocket_frame_payload(frame)
            if parsed is None:
                meta["error"] = "候选发送了不可解析的 WS 帧"
                meta["valid"] = False
                return
            opcode, fin, rsv1, payload = parsed
            if opcode == 0x9:
                if not fin or rsv1 or len(payload) > 125:
                    meta["error"] = "候选发送了结构无效的 WS PING"
                    meta["valid"] = False
                    return
                pong = _encode_server_control_frame(0xA, payload)
                rec.write("upstream_to_client", pong)
                writer.write(pong)
                await writer.drain()
                continue
            if opcode == 0x8:
                close = _encode_server_control_frame(0x8, payload[:125])
                rec.write("upstream_to_client", close)
                writer.write(close)
                await writer.drain()
                return
            try:
                text = decoder.text(frame)
            except ValueError as error:
                meta["error"] = f"候选 WS 消息无法解码：{error}"
                meta["valid"] = False
                return
            if text is None:
                continue
            try:
                payload_json = json.loads(text)
            except json.JSONDecodeError:
                meta["error"] = "候选 WS 文本帧不是 JSON"
                meta["valid"] = False
                return
            if not isinstance(payload_json, dict) or payload_json.get("type") != "response.create":
                meta["error"] = (
                    "核心 WS 首个业务文本事件不是 response.create: "
                    f"{payload_json.get('type') if isinstance(payload_json, dict) else None!r}"
                )
                meta["valid"] = False
                return

            local_create_count += 1
            ordinal = await self._claim_core_counter("ws_response_creates")
            model = str(payload_json.get("model") or "gpt-5.5")
            response_id = (
                f"resp_candidate_core_{self.args.candidate_core_scenario.lower()}_"
                f"{ordinal:04d}"
            )
            self._log_synthetic_core(
                action="responses_ws_response_create",
                conn_id=conn_id,
                request_line=request_line,
                ordinal=ordinal,
                frame_sha256=hashlib.sha256(frame).hexdigest(),
            )
            events = [
                {
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "model": model,
                        "status": "in_progress",
                        "output": [],
                    },
                }
            ]
            if self.args.candidate_core_scenario == "A05":
                events.append({
                    "type": "response.metadata",
                    "headers": {
                        "x-codex-turn-state": _SYNTHETIC_CORE_TURN_STATE,
                    },
                })
            events.append({
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "model": model,
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            })
            for event in events:
                server_frame = _encode_server_text_frame(
                    json.dumps(event, separators=(",", ":"), ensure_ascii=True)
                )
                rec.write("upstream_to_client", server_frame)
                writer.write(server_frame)
            await writer.drain()
            meta["ws_response_create_count"] = local_create_count

    def request_stop(self) -> None:
        """请求监听器尽快停止，并让 ``asyncio.run`` 正常进入任务收尾。"""

        self._stop_requested = True
        # wrapper 只会在场景触发完成后发停止信号。主动 abort 尚未完成 TLS
        # close_notify 的客户端腿，避免 asyncio.run 因 SSL shutdown timeout 再拖
        # 30 秒；连接协程仍会执行 finally，ByteRecorder 元数据不会丢。
        for writer in tuple(self._active_client_writers):
            try:
                writer.transport.abort()
            except (AttributeError, OSError, RuntimeError):
                pass
        if self._stop_event is not None:
            self._stop_event.set()

    def _log_intervention(self, event: dict) -> None:
        """立即记录受控干预，避免 SIGTERM 使退出期汇总丢失。"""
        event = {"t_unix_ms": round(time.time() * 1000), **event}
        try:
            with open(self.out / "intervention.jsonl", "a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False) + "\n")
                file.flush()
        except OSError:
            pass

    async def _claim_retry_probe_attempt(
        self,
        conn_id: int,
        request_line: str,
    ) -> int:
        """为受控 retry 请求分配全局 attempt 编号并立即落盘归属关系。"""
        async with self._retry_probe_lock:
            self._retry_probe_attempts += 1
            attempt = self._retry_probe_attempts
        action = "forward_to_production"
        if attempt == 1:
            action = (
                "synthetic_500_keepalive"
                if self.args.retry_probe == "keepalive-500"
                else "disconnect"
            )
        self._log_intervention({
            "type": "conn_retry_probe",
            "mode": self.args.retry_probe,
            "target": self.args.retry_probe_target,
            "attempt": attempt,
            "connection_id": conn_id,
            "action": action,
            "request_line": request_line,
        })
        return attempt

    def _is_retry_probe_target(self, request_line: str) -> bool:
        if self.args.retry_probe_target == "models":
            return request_line.startswith("GET /backend-api/codex/models?")
        return request_line == "POST /backend-api/codex/responses HTTP/1.1"

    @staticmethod
    async def _read_h1_body(reader: asyncio.StreamReader, head: bytes) -> bytes:
        """按显式 Content-Length 读完请求体；无 body 时返回空字节。"""
        length_match = re.search(rb"\r\ncontent-length:\s*(\d+)\r\n", head.lower())
        body_length = int(length_match.group(1)) if length_match else 0
        return await reader.readexactly(body_length) if body_length else b""

    async def _pump_response_with_turn_state(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        rec: ByteRecorder,
        conn_id: int,
    ) -> None:
        """在首个 HTTP 200 响应头里注入 turn-state，随后恢复透明转发。"""
        try:
            head = await src.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as error:
            if error.partial:
                rec.write("upstream_to_client", error.partial)
                dst.write(error.partial)
                await dst.drain()
            return
        except asyncio.LimitOverrunError:
            await pump(src, dst, rec, "upstream_to_client")
            return

        lower = head.lower()
        injected = False
        if (not self._turn_state_injected
                and head.startswith(b"HTTP/1.1 200")
                and b"\r\nx-codex-turn-state:" not in lower):
            value = self.args.inject_turn_state.encode("ascii")
            head = head[:-4] + b"\r\nx-codex-turn-state: " + value + b"\r\n\r\n"
            self._turn_state_injected = True
            injected = True
            self._log_intervention({
                "type": "inject_turn_state",
                "connection_id": conn_id,
                "value": self.args.inject_turn_state,
            })

        rec.write("upstream_to_client", head)
        dst.write(head)
        await dst.drain()
        if injected:
            # 首部已完整送达，body 不需要重写长度，继续按原始分块复制。
            pass
        await pump(src, dst, rec, "upstream_to_client")

    async def _pump_client_until_ws_response_create(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        rec: ByteRecorder,
        conn_id: int,
        ready: asyncio.Event,
        meta: dict,
    ) -> None:
        """先按完整帧转发并核验首个 response.create，再恢复透明复制。"""
        try:
            for _ in range(8):
                frame = await _read_websocket_frame(src)
                rec.write("client_to_upstream", frame)
                dst.write(frame)
                await dst.drain()
                text = _decode_client_text_frame(frame)
                if text is None:
                    continue
                try:
                    event_type = json.loads(text).get("type")
                except (json.JSONDecodeError, AttributeError):
                    event_type = None
                if event_type != "response.create":
                    meta["error"] = (
                        "WS turn-state 注入前首个文本消息不是 response.create: "
                        f"{event_type!r}"
                    )
                    meta["valid"] = False
                    return
                meta["ws_turn_state_trigger_frame_sha256"] = hashlib.sha256(frame).hexdigest()
                self._log_intervention({
                    "type": "ws_turn_state_trigger",
                    "connection_id": conn_id,
                    "event_type": event_type,
                    "frame_sha256": meta["ws_turn_state_trigger_frame_sha256"],
                })
                ready.set()
                await pump(src, dst, rec, "client_to_upstream")
                return
            meta["error"] = "WS turn-state 注入前 8 帧内未见 response.create"
            meta["valid"] = False
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError) as error:
            meta["error"] = f"等待 WS response.create 失败: {type(error).__name__}"
            meta["valid"] = False

    async def _pump_response_with_ws_turn_state(
        self,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        rec: ByteRecorder,
        conn_id: int,
        ready: asyncio.Event,
        meta: dict,
    ) -> None:
        """转发 101 后，在已核验的客户端消息边界注入 response.metadata。"""
        try:
            head = await src.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as error:
            if error.partial:
                rec.write("upstream_to_client", error.partial)
                dst.write(error.partial)
                await dst.drain()
            return
        except asyncio.LimitOverrunError:
            meta["error"] = "WS 101 响应头超过读取上限"
            meta["valid"] = False
            return

        rec.write("upstream_to_client", head)
        dst.write(head)
        await dst.drain()
        if not head.startswith(b"HTTP/1.1 101"):
            meta["error"] = "WS turn-state 注入目标未返回 101"
            meta["valid"] = False
            await pump(src, dst, rec, "upstream_to_client")
            return

        try:
            await asyncio.wait_for(ready.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            meta["error"] = "101 后未在 15 秒内核验到客户端 response.create"
            meta["valid"] = False
            await pump(src, dst, rec, "upstream_to_client")
            return

        payload = json.dumps({
            "type": "response.metadata",
            "headers": {"x-codex-turn-state": self.args.inject_ws_turn_state},
        }, separators=(",", ":"), ensure_ascii=True)
        frame = _encode_server_text_frame(payload)
        rec.write("upstream_to_client", frame)
        dst.write(frame)
        await dst.drain()
        self._ws_turn_state_injected = True
        meta["intervention"] = "inject_ws_turn_state"
        meta["ws_turn_state_injected_frame_sha256"] = hashlib.sha256(frame).hexdigest()
        self._log_intervention({
            "type": "inject_ws_turn_state",
            "connection_id": conn_id,
            "value": self.args.inject_ws_turn_state,
            "frame_sha256": meta["ws_turn_state_injected_frame_sha256"],
        })
        await pump(src, dst, rec, "upstream_to_client")

    async def _resolve(self, host: str) -> str:
        """把域名解析成 IP。

        ⚠ **不能在运行时解析**：容器的 /etc/hosts 已把目标域名劫持到 127.0.0.1，
        `getent`/`getaddrinfo` 都会返回它，中继于是连回自身——表现为所有连接
        「只有上行、零下行」。容器里也没有 nslookup 可绕。

        解法是**由调用方在劫持之前预解析好**，用 `--upstream-map host=ip`
        传进来。运行时只查这张表，不做任何解析。
        """
        if host in self._dns_cache:
            return self._dns_cache[host]
        # 表里没有就退回默认上游 IP——总比连回 127.0.0.1 强
        fallback = self.args.upstream_ip or host
        self._dns_cache[host] = fallback
        return fallback

    def _on_sni(self, sslobj, servername, sslctx):  # noqa: ANN001
        """握手期间记录客户端请求的域名，供后续选上游。

        ⚠ 同时**立即**追加到 sni.log。汇总 meta 要等进程退出才写，
        而采集脚本用 pkill 停中继——那份文件经常来不及生成。
        SNI 是判断"客户端访问了哪些域名"的直接证据，不能依赖退出路径。
        """
        if servername:
            # ⚠ 不能用 id(sslobj) 当键：回调收到的 SSLObject 与握手完成后
            # `writer.get_extra_info("ssl_object")` 返回的**不是同一个对象**，
            # 按 id 查会永远落空（表现为 sni 恒为 None）。
            # 回调在 start_tls 期间同步触发、且每条连接串行进入 handle()，
            # 所以用"最近一次"传递是安全的。
            self._last_sni = servername
            try:
                with open(self.out / "sni.log", "a", encoding="utf-8") as f:
                    f.write(servername + "\n")
            except OSError:
                pass
        return None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._active_client_writers.add(writer)
        self.conn_seq += 1
        conn_id = self.conn_seq
        meta: dict = {"connection_id": conn_id}
        rec = ByteRecorder(self.out, conn_id)
        up_r = up_w = None
        try:
            target_host, target_port = self.args.upstream_host, 443
            # 多域名模式下，真正的目标由握手时的 SNI 决定（见 _on_sni）

            # ── CONNECT 模式：先接下隧道 ──
            if self.args.mode == "connect":
                head = await reader.readuntil(b"\r\n\r\n")
                line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                meta["connect_request_line"] = line
                if not line.upper().startswith("CONNECT"):
                    meta["error"] = "非 CONNECT 请求"
                    return
                hostport = line.split()[1]
                target_host = hostport.rsplit(":", 1)[0]
                target_port = int(hostport.rsplit(":", 1)[1]) if ":" in hostport else 443
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

            # ── 取客户端 ALPN offer ──
            # asyncio 的 TransportSocket 不暴露 recv()，无法在握手前 MSG_PEEK 窥探
            # ClientHello。改由调用方按被测客户端的已知 ALPN 画像显式传入——该值
            # 必须与 N0 被动 pcap 的实测一致，给错等于把客户端逼上它本不走的协议。
            offered = self.args.assume_alpn.split(",") if self.args.assume_alpn else None
            meta["client_alpn_offer"] = offered
            meta["alpn_source"] = "assumed" if offered else "none"

            # ── 顺序很重要：先升级客户端腿，再连上游 ──
            # 客户端发出 ClientHello 后即等待 ServerHello。若此刻先去做上游 TLS
            # （握手可能耗时数百毫秒），客户端会因等不到响应而 reset——这正是
            # 联调时反复出现 ConnectionResetError 的原因。
            #
            # 代价：客户端腿必须在知道上游选定协议之前就定下 ALPN，故直接用调用方
            # 声明的 offer。这也是 --assume-alpn 必须与 N0 实测一致的原因之一。
            if offered:
                self.ctx.set_alpn_protocols(offered)

            # StreamWriter.start_tls 是**原地升级**：返回 None，并就地替换 writer
            # 自身的 transport。曾误以为它返回新 transport 并赋值 writer._transport，
            # 结果把 transport 置成 None，连接当场断。
            await writer.start_tls(self.ctx)
            ssl_obj = writer.get_extra_info("ssl_object")
            cli_alpn = ssl_obj.selected_alpn_protocol() if ssl_obj else None
            meta["client_alpn"] = cli_alpn

            # ── 按 SNI 选上游 ──
            # 握手已完成，_on_sni 里记下的 servername 现在可取。
            # 有它就用它当上游主机名，从而支持一个中继覆盖多个域名。
            sni, self._last_sni = self._last_sni, None
            if sni:
                meta["sni"] = sni
                if sni != target_host:
                    target_host = sni
                    if self.args.synthetic_profile:
                        # 合成画像连 DNS 查询也不应发向外部。后续白名单只使用
                        # target_host，且会在 open_connection 之前结束本连接。
                        target_ip = ""
                    else:
                        # 上游 IP 必须跟着换——否则会把 api.openai.com 的流量
                        # 送到 chatgpt.com 的地址上。逐域名解析，结果缓存。
                        target_ip = await self._resolve(sni)
                        meta["upstream_ip_used"] = target_ip
                else:
                    target_ip = self.args.upstream_ip or target_host
            else:
                target_ip = self.args.upstream_ip or target_host

            # 受控干预只支持未协商 ALPN 的 HTTP/1.1。默认路径不预读，仍保持原来的
            # 全透明字节泵；开启干预时只缓冲首部，用于识别 WS 握手或 HTTP POST。
            initial_head = None
            initial_body = b""
            initial_head_recorded = False
            request_line = ""
            is_responses_ws = False
            intervention_enabled = bool(
                self.args.force_ws_fallback_426
                or self.args.inject_turn_state
                or self.args.inject_ws_turn_state
                or self.args.synthesize_realtime_call
                or self.args.synthesize_realtime_call_after is not None
                or self.args.retry_probe
                or self.args.synthetic_profile
            )
            if intervention_enabled:
                if cli_alpn not in {None, "http/1.1"}:
                    meta["error"] = "受控 H1 干预只允许未协商 ALPN 或 HTTP/1.1"
                    meta["valid"] = False
                    return
                initial_head = await reader.readuntil(b"\r\n\r\n")
                request_line = initial_head.split(b"\r\n", 1)[0].decode(
                    "latin-1", "replace"
                )
                meta["request_line"] = request_line

                if self.args.synthetic_profile:
                    # 合成模式在这里终止：请求最多读到完整 H1 body，随后只调用冻结
                    # 白名单响应器。这个分支位于 open_connection 之前，因此即使收到
                    # 未知路径也没有任何机会连接生产 auth/wham/blob/ChatGPT 端点。
                    initial_body = await self._read_h1_body(reader, initial_head)
                    rec.write("client_to_upstream", initial_head)
                    recorded_body = initial_body
                    if (
                        self.args.synthetic_profile == "candidate-aux-v1"
                        and target_host.lower().rstrip(".") == "auth.openai.com"
                    ):
                        recorded_body, oauth_redacted = _redact_oauth_refresh_body(initial_body)
                        if not oauth_redacted:
                            meta["error"] = "OAuth 合成请求缺少可等长遮蔽的 refresh_token"
                            meta["valid"] = False
                            response = _h1_response(
                                400,
                                "Bad Request",
                                b'{"error":"invalid_request"}',
                            )
                            rec.write("upstream_to_client", response)
                            writer.write(response)
                            await writer.drain()
                            return
                        meta["oauth_refresh_token_persisted"] = False
                        meta["oauth_refresh_token_scrubbing"] = "equal_length_before_write"
                    if (
                        self.args.synthetic_profile == "claude-fw-f-v4"
                        and self.args.claude_fault_plan == "oauth-refresh-reject"
                        and target_host.lower().rstrip(".") == "platform.claude.com"
                    ):
                        recorded_body, oauth_redacted = _redact_oauth_refresh_body(initial_body)
                        if not oauth_redacted:
                            meta["error"] = "Claude OAuth 合成请求缺少可等长遮蔽的 refresh_token"
                            meta["valid"] = False
                            response = _h1_response(
                                400,
                                "Bad Request",
                                b'{"error":"invalid_request"}',
                            )
                            rec.write("upstream_to_client", response)
                            writer.write(response)
                            await writer.drain()
                            return
                        meta["oauth_refresh_token_persisted"] = False
                        meta["oauth_refresh_token_scrubbing"] = "equal_length_before_write"
                    if recorded_body:
                        rec.write("client_to_upstream", recorded_body)

                    if self.args.synthetic_profile == "candidate-aux-v1":
                        legacy_compact_ordinal = 0
                        if request_line == (
                            "POST /backend-api/codex/responses/compact HTTP/1.1"
                        ):
                            legacy_compact_ordinal = await self._claim_core_counter(
                                "aux_legacy_compacts"
                            )
                        synthetic = _synthetic_aux_response(
                            target_host,
                            request_line,
                            initial_head,
                            initial_body,
                            self.args.codex_version,
                            legacy_compact_ordinal,
                        )
                    elif self.args.synthetic_profile == "candidate-core-v1":
                        is_core_ws = (
                            request_line == "GET /backend-api/codex/responses HTTP/1.1"
                            and b"\r\nupgrade: websocket\r\n" in initial_head.lower()
                        )
                        if is_core_ws:
                            ws_attempt = await self._claim_core_counter("ws_handshakes")
                            if self.args.candidate_core_scenario == "A07":
                                if ws_attempt <= self.args.candidate_core_ws_failures:
                                    synthetic = SyntheticCoreResponse(
                                        "responses_ws_retryable_failure",
                                        _h1_response(
                                            502,
                                            "Bad Gateway",
                                            b'{"error":"candidate_core_retryable_ws_failure"}',
                                        ),
                                    )
                                else:
                                    synthetic = None
                            else:
                                synthetic = _synthetic_core_response(
                                    self.args.candidate_core_scenario,
                                    target_host,
                                    request_line,
                                    initial_head,
                                    initial_body,
                                    0,
                                    self.args.codex_version,
                                )
                        else:
                            http_ordinal = 0
                            if request_line == "POST /backend-api/codex/responses HTTP/1.1":
                                http_ordinal = await self._claim_core_counter("http_responses")
                            synthetic = _synthetic_core_response(
                                self.args.candidate_core_scenario,
                                target_host,
                                request_line,
                                initial_head,
                                initial_body,
                                http_ordinal,
                                self.args.codex_version,
                            )
                            if (
                                synthetic is not None
                                and self.args.candidate_core_scenario == "A07"
                                and self._core_ws_handshakes
                                != self.args.candidate_core_ws_failures
                            ):
                                synthetic = None
                    else:
                        claude_ordinal = 0
                        if request_line == "POST /v1/messages?beta=true HTTP/1.1":
                            claude_ordinal = await self._claim_core_counter(
                                "claude_messages"
                            )
                        synthetic = _synthetic_claude_response(
                            self.args.claude_fault_plan,
                            target_host,
                            request_line,
                            initial_body,
                            claude_ordinal,
                            self.args.claude_success_marker,
                        )
                    if synthetic is None:
                        response = _h1_response(
                            421,
                            "Misdirected Request",
                            b'{"error":"synthetic_target_not_allowlisted"}',
                        )
                        rec.write("upstream_to_client", response)
                        writer.write(response)
                        await writer.drain()
                        meta["error"] = "候选合成白名单或场景状态未命中"
                        meta["valid"] = False
                        meta["production_forwarded"] = False
                        self._log_intervention({
                            "type": "synthetic_target_rejected",
                            "profile": self.args.synthetic_profile,
                            "scenario": self.args.candidate_core_scenario or None,
                            "connection_id": conn_id,
                            "host": target_host,
                            "request_line": request_line,
                            "production_forwarded": False,
                        })
                        return

                    delay_seconds = getattr(synthetic, "delay_seconds", 0.0)
                    if delay_seconds and not synthetic.wire:
                        # stall 的客户端超时早于中继计划结束。必须在等待前建立 0 字节
                        # 响应文件、闭合干预日志并标记连接有效；否则 wrapper 停中继时
                        # 协程被取消，只会留下“未知无效连接”，丢掉这次受控超时事实。
                        rec.write("upstream_to_client", b"")
                        meta["valid"] = True
                        meta["upstream_alpn"] = "http/1.1"
                        meta["synthetic_profile"] = self.args.synthetic_profile
                        meta["codex_version"] = self.args.codex_version
                        meta["intervention"] = synthetic.action
                        meta["production_forwarded"] = False
                        self._log_intervention({
                            "type": "synthetic_claude_response",
                            "profile": self.args.synthetic_profile,
                            "plan": self.args.claude_fault_plan,
                            "action": synthetic.action,
                            "connection_id": conn_id,
                            "request_line": request_line,
                            "message_ordinal": claude_ordinal,
                            "delay_seconds": delay_seconds,
                            "production_forwarded": False,
                        })
                        await asyncio.sleep(delay_seconds)
                        return
                    if delay_seconds:
                        await asyncio.sleep(delay_seconds)
                    # 空响应代表受控断连／超时。仍显式建立 0 字节方向文件，
                    # 让 R 完整性门禁能区分“按计划没有响应”和“记录器漏写”。
                    rec.write("upstream_to_client", synthetic.wire)
                    writer.write(synthetic.wire)
                    terminal_ws_frame = getattr(synthetic, "terminal_ws_frame", b"")
                    if terminal_ws_frame:
                        rec.write("upstream_to_client", terminal_ws_frame)
                        writer.write(terminal_ws_frame)
                    await writer.drain()
                    if terminal_ws_frame:
                        # 给候选 observer 足够时间读取完整终止帧，再关闭受控连接。
                        await asyncio.sleep(0.2)
                    meta["valid"] = True
                    meta["upstream_alpn"] = "http/1.1"
                    meta["synthetic_profile"] = self.args.synthetic_profile
                    meta["codex_version"] = self.args.codex_version
                    meta["intervention"] = synthetic.action
                    meta["production_forwarded"] = False
                    if self.args.synthetic_profile == "candidate-core-v1":
                        core_evidence = {}
                        if synthetic.set_cookie_names:
                            core_evidence["set_cookie_names"] = list(
                                synthetic.set_cookie_names
                            )
                        self._log_synthetic_core(
                            action=synthetic.action,
                            conn_id=conn_id,
                            request_line=request_line,
                            host=target_host,
                            **core_evidence,
                        )
                        if synthetic.websocket:
                            await self._serve_synthetic_core_websocket(
                                reader,
                                writer,
                                rec,
                                conn_id,
                                request_line,
                                meta,
                            )
                    elif self.args.synthetic_profile == "candidate-aux-v1":
                        self._log_intervention({
                            "type": "synthetic_aux_response",
                            "profile": self.args.synthetic_profile,
                            "action": synthetic.action,
                            "connection_id": conn_id,
                            "host": target_host,
                            "request_line": request_line,
                            "production_forwarded": False,
                        })
                    else:
                        self._log_intervention({
                            "type": "synthetic_claude_response",
                            "profile": self.args.synthetic_profile,
                            "plan": self.args.claude_fault_plan,
                            "action": synthetic.action,
                            "connection_id": conn_id,
                            "request_line": request_line,
                            "message_ordinal": claude_ordinal,
                            "delay_seconds": delay_seconds,
                            "production_forwarded": False,
                        })
                    return

                is_retry_probe_target = self._is_retry_probe_target(request_line)
                if self.args.retry_probe and is_retry_probe_target:
                    attempt = await self._claim_retry_probe_attempt(
                        conn_id,
                        request_line,
                    )
                    meta.setdefault("retry_probe_attempts", []).append({
                        "attempt": attempt,
                        "request_line": request_line,
                    })

                    if attempt == 1:
                        rec.write("client_to_upstream", initial_head)
                        initial_head_recorded = True
                        initial_body = await self._read_h1_body(reader, initial_head)
                        if initial_body:
                            rec.write("client_to_upstream", initial_body)
                        meta["intervention"] = f"conn_retry_probe:{self.args.retry_probe}"

                        if self.args.retry_probe == "disconnect":
                            # 不发送任何 HTTP 响应，直接结束 TLS 连接，制造明确的
                            # transport error。该连接只有上行是**预期干预结果**，不是
                            # 记录器丢字节；attempt 2 必须由同一上层调用另建 TCP。
                            meta["valid"] = True
                            meta["expected_upstream_only"] = True
                            return

                        response = (
                            b"HTTP/1.1 500 Internal Server Error\r\n"
                            b"content-length: 0\r\n"
                            b"connection: keep-alive\r\n\r\n"
                        )
                        rec.write("upstream_to_client", response)
                        writer.write(response)
                        await writer.drain()

                        # reqwest 已完整读完 500 且连接仍可用；若同一个 Client/连接池
                        # 承载内部 retry，第二个 GET 会从这条 TLS 连接继续到达。
                        try:
                            retry_head = await asyncio.wait_for(
                                reader.readuntil(b"\r\n\r\n"),
                                timeout=self.args.retry_probe_wait,
                            )
                        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
                            meta["error"] = (
                                "500 keep-alive 后同连接未收到 retry: "
                                f"{type(error).__name__}"
                            )
                            meta["valid"] = False
                            return

                        retry_request_line = retry_head.split(b"\r\n", 1)[0].decode(
                            "latin-1", "replace"
                        )
                        if not self._is_retry_probe_target(retry_request_line):
                            rec.write("client_to_upstream", retry_head)
                            meta["error"] = (
                                "500 keep-alive 后同连接收到的不是 models retry: "
                                f"{retry_request_line}"
                            )
                            meta["valid"] = False
                            return

                        retry_attempt = await self._claim_retry_probe_attempt(
                            conn_id,
                            retry_request_line,
                        )
                        rec.write("client_to_upstream", retry_head)
                        retry_body = await self._read_h1_body(reader, retry_head)
                        if retry_body:
                            rec.write("client_to_upstream", retry_body)
                        meta["retry_probe_attempts"].append({
                            "attempt": retry_attempt,
                            "request_line": retry_request_line,
                        })
                        initial_head = retry_head
                        initial_body = retry_body
                        initial_head_recorded = True
                        request_line = retry_request_line

                is_responses_ws = (
                    request_line == "GET /backend-api/codex/responses HTTP/1.1"
                    and b"\r\nupgrade: websocket\r\n" in initial_head.lower()
                )
                if (self.args.force_ws_fallback_426 and is_responses_ws
                        and not self._forced_ws_fallback):
                    self._forced_ws_fallback = True
                    rec.write("client_to_upstream", initial_head)
                    response = (
                        b"HTTP/1.1 426 Upgrade Required\r\n"
                        b"content-length: 0\r\n"
                        b"connection: close\r\n\r\n"
                    )
                    rec.write("upstream_to_client", response)
                    writer.write(response)
                    await writer.drain()
                    meta["valid"] = True
                    meta["intervention"] = "force_ws_fallback_426"
                    self._log_intervention({
                        "type": "force_ws_fallback_426",
                        "connection_id": conn_id,
                        "request_line": request_line,
                    })
                    return

                is_realtime_call = request_line.startswith(
                    "POST /backend-api/codex/realtime/calls?"
                )
                synthesize_realtime = False
                if is_realtime_call and (
                    self.args.synthesize_realtime_call
                    or self.args.synthesize_realtime_call_after is not None
                ):
                    realtime_attempt, synthesize_realtime = (
                        await self._claim_realtime_call_attempt()
                    )
                    meta["realtime_call_attempt"] = realtime_attempt
                    meta["realtime_call_action"] = (
                        "synthetic_response"
                        if synthesize_realtime
                        else "forward_to_production"
                    )
                if synthesize_realtime:
                    rec.write("client_to_upstream", initial_head)
                    length_match = re.search(
                        rb"\r\ncontent-length:\s*(\d+)\r\n",
                        initial_head.lower(),
                    )
                    if length_match:
                        body_length = int(length_match.group(1))
                        if body_length:
                            body = await reader.readexactly(body_length)
                            rec.write("client_to_upstream", body)
                    sdp = b"v=0\r\n"
                    response = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"content-type: application/sdp\r\n"
                        b"location: https://chatgpt.com/backend-api/codex/realtime/calls/rtc_probe\r\n"
                        + f"content-length: {len(sdp)}\r\n".encode("ascii")
                        + b"connection: close\r\n\r\n"
                        + sdp
                    )
                    rec.write("upstream_to_client", response)
                    writer.write(response)
                    await writer.drain()
                    meta["valid"] = True
                    intervention = (
                        "synthesize_realtime_call"
                        if self.args.synthesize_realtime_call
                        else "synthesize_realtime_call_after_live_failure"
                    )
                    meta["intervention"] = intervention
                    self._log_intervention({
                        "type": intervention,
                        "connection_id": conn_id,
                        "call_id": "rtc_probe",
                        "realtime_call_attempt": realtime_attempt,
                        "request_line": request_line,
                    })
                    return

            # ── 上游腿：用**客户端同一份** ALPN 列表握手 ──
            up_ctx = ssl.create_default_context()
            upstream_offer = _upstream_alpn_offer(
                offered,
                cli_alpn,
                mirror_selected=self.args.mirror_selected_alpn,
            )
            meta["upstream_alpn_offer"] = upstream_offer
            preconnected = await self._take_preconnected_upstream(
                target_host=target_host,
                target_ip=target_ip,
                target_port=target_port,
                alpn_offer=upstream_offer,
            )
            if preconnected is not None:
                up_r, up_w = preconnected.reader, preconnected.writer
                up_alpn = preconnected.selected_alpn
                meta["upstream_preconnected"] = True
                meta["upstream_preconnect_duration_ms"] = (
                    preconnected.connect_duration_ms
                )
            else:
                if upstream_offer:
                    up_ctx.set_alpn_protocols(upstream_offer)
                up_r, up_w = await asyncio.open_connection(
                    host=target_ip, port=target_port,
                    ssl=up_ctx, server_hostname=target_host)
                up_alpn = up_w.get_extra_info("ssl_object").selected_alpn_protocol()
                meta["upstream_preconnected"] = False
            meta["upstream_alpn"] = up_alpn
            if cli_alpn != up_alpn:
                # 两侧不一致即污染：中继会把客户端逼上它本不走的协议。
                meta["error"] = f"ALPN 不一致 client={cli_alpn} upstream={up_alpn}"
                meta["valid"] = False
                return

            meta["valid"] = True
            if initial_head is not None:
                if not initial_head_recorded:
                    rec.write("client_to_upstream", initial_head)
                up_w.write(initial_head)
                if initial_body:
                    up_w.write(initial_body)
                await up_w.drain()

            inject_this_response = bool(
                self.args.inject_turn_state
                and not self._turn_state_injected
                and request_line == "POST /backend-api/codex/responses HTTP/1.1"
            )
            inject_this_ws_response = bool(
                self.args.inject_ws_turn_state
                and not self._ws_turn_state_injected
                and is_responses_ws
            )
            if inject_this_ws_response:
                ws_request_ready = asyncio.Event()
                await asyncio.gather(
                    self._pump_client_until_ws_response_create(
                        reader, up_w, rec, conn_id, ws_request_ready, meta
                    ),
                    self._pump_response_with_ws_turn_state(
                        up_r, writer, rec, conn_id, ws_request_ready, meta
                    ),
                )
            else:
                await asyncio.gather(
                    pump(reader, up_w, rec, "client_to_upstream"),
                    self._pump_response_with_turn_state(
                        up_r, writer, rec, conn_id
                    ) if inject_this_response else pump(
                        up_r, writer, rec, "upstream_to_client"
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 单连接失败不应终止整轮采集
            meta["error"] = f"{type(exc).__name__}: {exc}"
            meta.setdefault("valid", False)
        finally:
            meta.update(rec.close())
            _annotate_relay_stop_after_client_request(
                meta,
                stop_requested=self._stop_requested,
            )
            self.records.append(meta)
            for w in (writer, up_w):
                try:
                    if w:
                        w.close()
                        # TLS 客户端可能在读完 ``connection: close`` 响应后仍暂时
                        # 保留本地对象。必须让 writer 留在活动集合中直到关闭完成；
                        # 这样 wrapper 发 SIGTERM 时 request_stop() 仍可 abort 底层
                        # transport，不会被 asyncio 的 SSL shutdown timeout 拖住。
                        try:
                            await asyncio.wait_for(w.wait_closed(), timeout=0.5)
                        except (asyncio.TimeoutError, ConnectionError, ssl.SSLError, OSError):
                            try:
                                w.transport.abort()
                            except (AttributeError, OSError, RuntimeError):
                                pass
                except (OSError, RuntimeError):
                    pass
            self._active_client_writers.discard(writer)

    async def serve(self) -> None:
        # 始终以明文接受：TLS 握手必须发生在窥探 ClientHello 之后，
        # 否则 asyncio 会在回调前就完成握手，拿不到客户端的 ALPN offer。
        await self._prepare_preconnected_upstream()
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.args.port)
        self._write_preconnect_ready()
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()
        loop = asyncio.get_running_loop()
        loop_signal_registered = False
        try:
            # signal.signal 的 Python 回调可能因 PEP 475 自动重启 selector 而一直等到
            # wait_for timeout 才执行。loop.add_signal_handler 使用 asyncio 自唤醒管道，
            # SIGTERM 到达后会立刻调度 request_stop。
            loop.add_signal_handler(signal.SIGTERM, self.request_stop)
            loop_signal_registered = True
        except (NotImplementedError, RuntimeError):
            # Windows/非主线程环境保留 main() 安装的同步 handler；生产抓包容器是
            # Linux 主线程，会稳定走上面的自唤醒路径。
            pass
        try:
            async with server:
                try:
                    # start_server 创建后已开始接受连接；这里等待显式停止事件即可。
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.args.timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            if loop_signal_registered:
                loop.remove_signal_handler(signal.SIGTERM)
            await self._close_unused_preconnected_upstream()
        self._stop_event = None

    def dump(self) -> None:
        path = self.out / "relay.json"
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema_version": "byte-relay/v1",
                       "mode": self.args.mode,
                       "upstream_host": self.args.upstream_host,
                       "codex_version": self.args.codex_version or None,
                       "claude_version": self.args.claude_version or None,
                       "synthetic_profile": self.args.synthetic_profile or None,
                       "claude_fault_plan": self.args.claude_fault_plan or None,
                       "claude_success_marker": self.args.claude_success_marker or None,
                       "candidate_core_scenario": self.args.candidate_core_scenario or None,
                       "candidate_core_ws_failures": (
                           self.args.candidate_core_ws_failures
                           if self.args.synthetic_profile == "candidate-core-v1"
                           else None
                       ),
                       "production_forwarding_enabled": not bool(self.args.synthetic_profile),
                       "mirror_selected_alpn": self.args.mirror_selected_alpn,
                       "upstream_preconnect_enabled": self.args.preconnect_upstream,
                       "upstream_preconnect_duration_ms": self._preconnect_duration_ms,
                       "connections": self.records},
                      f, ensure_ascii=False, indent=2)
        print(json.dumps({"connections": len(self.records),
                          "valid": sum(1 for r in self.records if r.get("valid")),
                          "output": str(path)}, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cert", required=True, help="面向客户端的证书链 PEM")
    ap.add_argument("--key", required=True)
    ap.add_argument("--mode", choices=["direct", "connect"], default="direct",
                    help="direct=hosts 劫持；connect=显式 HTTP 代理")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--upstream-host", default="chatgpt.com")
    ap.add_argument("--codex-version", default="",
                    help="候选合成画像绑定的 Campaign 目标 Codex 版本")
    ap.add_argument("--upstream-map", default="",
                    help="多域名上游映射 host=ip[,host=ip…]，"
                         "**必须在劫持 hosts 之前解析好**")
    ap.add_argument("--upstream-ip", default="",
                    help="direct 模式必填：上游真实 IP，绕开被劫持的 hosts")
    ap.add_argument(
        "--preconnect-upstream",
        action="store_true",
        help=("在开始监听客户端前预建一次真实上游 TLS；仅供模型目录前置采集"
              "规避官方客户端的 5 秒硬超时，不修改任何应用字节"),
    )
    ap.add_argument(
        "--preconnect-timeout",
        type=float,
        default=15.0,
        help="预建真实上游 TLS 的独立超时秒数",
    )
    ap.add_argument("--output", required=True)
    ap.add_argument("--assume-alpn", default="",
                    help="客户端 ALPN offer（逗号分隔）。asyncio 无法窥探 ClientHello，"
                         "故由调用方按被测客户端的已知画像显式给出；留空表示不 offer。"
                         "给错会把客户端逼上它本不走的协议——须与 pcap 实测一致。")
    ap.add_argument(
        "--mirror-selected-alpn",
        action="store_true",
        help=("上游腿按客户端腿实际协商结果 offer；客户端未协商时上游也不发 ALPN。"
              "仅由明确观测到混合 ALPN 的 wrapper 启用"),
    )
    ap.add_argument("--force-ws-fallback-426", action="store_true",
                    help="仅一次：对内置 responses WS 握手返回 426，触发官方 HTTP 回退")
    ap.add_argument("--inject-turn-state", default="",
                    help="仅一次：向首个 HTTP /responses 的 200 响应注入该 turn-state")
    ap.add_argument("--inject-ws-turn-state", default="",
                    help="仅一次：在 responses WS 首个 response.create 后注入 response.metadata")
    realtime_synthesis = ap.add_mutually_exclusive_group()
    realtime_synthesis.add_argument(
        "--synthesize-realtime-call",
        action="store_true",
        help="兼容模式，仅第一次：合成 realtime/calls 200，用于触发 sideband 派生请求",
    )
    realtime_synthesis.add_argument(
        "--synthesize-realtime-call-after",
        type=int,
        choices=(1,),
        default=None,
        metavar="LIVE_ATTEMPTS",
        help="先真实转发一次 realtime/calls，再仅对下一次请求合成 200",
    )
    ap.add_argument(
        "--synthetic-profile",
        choices=(
            "candidate-aux-v1",
            "candidate-core-v1",
            "claude-fw-f-v3",
            "claude-fw-f-v4",
        ),
        default="",
        help=("冻结的候选响应画像；开启后所有请求只允许本地白名单响应，"
              "未知路径返回 421，绝不连接真实上游"),
    )
    ap.add_argument(
        "--allow-synthetic-responses",
        action="store_true",
        help="合成响应第二道显式确认；必须与 --synthetic-profile 同时提供",
    )
    ap.add_argument(
        "--candidate-core-scenario",
        choices=tuple(sorted(_SYNTHETIC_CORE_SCENARIOS)),
        default="",
        help="candidate-core-v1 必填的冻结验收场景",
    )
    ap.add_argument(
        "--candidate-core-ws-failures",
        type=int,
        default=6,
        help="A07 在允许 HTTP fallback 前返回的可重试 WS 握手失败次数",
    )
    ap.add_argument(
        "--claude-version",
        default="",
        help="Claude FW-F v3/v4 合成故障画像绑定的官方客户端版本",
    )
    ap.add_argument(
        "--claude-fault-plan",
        choices=tuple(sorted(_ALL_SYNTHETIC_CLAUDE_PLANS)),
        default="",
        help="claude-fw-f-v3/v4 必填的冻结响应计划",
    )
    ap.add_argument(
        "--claude-success-marker",
        choices=tuple(sorted(_SYNTHETIC_CLAUDE_SUCCESS_MARKERS)),
        default="",
        help="Claude 合成响应按 probe 冻结的成功 marker",
    )
    ap.add_argument(
        "--retry-probe",
        choices=("keepalive-500", "disconnect"),
        default="",
        help=("仅对首个 models GET：返回一次 500 并保持连接，或无响应断连；"
              "后续 attempt 转发真实上游，用于核验同一上层调用内的连接复用"),
    )
    ap.add_argument(
        "--retry-probe-target",
        choices=("models", "responses"),
        default="models",
        help="受控 retry 的目标端点；responses 用于内置 OpenAI HTTP fallback 分支",
    )
    ap.add_argument("--retry-probe-wait", type=float, default=15.0,
                    help="keepalive-500 后等待同连接 retry 的秒数")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()
    if args.inject_turn_state and not re.fullmatch(r"[A-Za-z0-9._-]+", args.inject_turn_state):
        ap.error("--inject-turn-state 只能包含字母、数字、点、下划线和连字符")
    if args.inject_ws_turn_state and not re.fullmatch(
        r"[A-Za-z0-9._-]+", args.inject_ws_turn_state
    ):
        ap.error("--inject-ws-turn-state 只能包含字母、数字、点、下划线和连字符")
    if bool(args.synthetic_profile) != bool(args.allow_synthetic_responses):
        ap.error("--synthetic-profile 与 --allow-synthetic-responses 必须同时提供")
    if args.codex_version and not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+",
        args.codex_version,
    ):
        ap.error("--codex-version 必须是完整的 x.y.z 版本")
    if args.claude_version and not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+",
        args.claude_version,
    ):
        ap.error("--claude-version 必须是完整的 x.y.z 版本")
    if args.synthetic_profile in {"candidate-aux-v1", "candidate-core-v1"} and not args.codex_version:
        ap.error("候选合成画像必须提供 --codex-version")
    if args.synthetic_profile and (args.upstream_ip or args.upstream_map):
        ap.error("候选合成模式禁止配置任何生产上游 IP/map")
    if args.preconnect_timeout <= 0:
        ap.error("--preconnect-timeout 必须大于 0")
    if args.preconnect_upstream and (
        args.mode != "direct"
        or args.synthetic_profile
        or args.mirror_selected_alpn
        or not args.upstream_ip
    ):
        ap.error(
            "--preconnect-upstream 只允许 direct 真实上游、固定 ALPN 的模型目录采集"
        )
    if args.synthetic_profile and any((
        args.force_ws_fallback_426,
        args.inject_turn_state,
        args.inject_ws_turn_state,
        args.synthesize_realtime_call,
        args.synthesize_realtime_call_after is not None,
        args.retry_probe,
    )):
        ap.error("候选合成模式不能与其他受控干预混用")
    if args.synthetic_profile == "candidate-core-v1":
        if not args.candidate_core_scenario:
            ap.error("candidate-core-v1 必须提供 --candidate-core-scenario")
        if not 1 <= args.candidate_core_ws_failures <= 20:
            ap.error("--candidate-core-ws-failures 必须在 1..20")
    elif args.candidate_core_scenario:
        ap.error("--candidate-core-scenario 只能与 candidate-core-v1 同时使用")
    if args.synthetic_profile in {"claude-fw-f-v3", "claude-fw-f-v4"}:
        if not args.claude_version:
            ap.error("Claude 合成画像必须提供 --claude-version")
        if not args.claude_fault_plan:
            ap.error("Claude 合成画像必须提供 --claude-fault-plan")
        if not args.claude_success_marker:
            ap.error("Claude 合成画像必须提供 --claude-success-marker")
        if args.codex_version:
            ap.error("Claude 合成画像禁止提供 --codex-version")
    elif args.claude_version or args.claude_fault_plan or args.claude_success_marker:
        ap.error("Claude 合成参数只能与 Claude 合成画像同时使用")
    relay = Relay(args)

    # docker exec 后台运行由 wrapper 通过 PID 发 SIGTERM。信号只设置 asyncio
    # stop event，不在 selector/wait_for 内抛异常；这样所有 Python 版本都能立即
    # 退出监听、取消连接任务，并稳定进入 finally 写 relay.json。
    def request_graceful_stop(_signum, _frame) -> None:
        relay.request_stop()

    signal.signal(signal.SIGTERM, request_graceful_stop)
    try:
        asyncio.run(relay.serve())
    except KeyboardInterrupt:
        relay.request_stop()
    finally:
        relay.dump()


if __name__ == "__main__":
    main()
