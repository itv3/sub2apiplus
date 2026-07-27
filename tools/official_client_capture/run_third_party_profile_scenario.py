#!/usr/bin/env python3
"""发送可验证官方画像归一化的第三方 HTTP、WebSocket 和 Anthropic 请求。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import struct
from pathlib import Path
from typing import Any


def secure_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以 0600 权限写入不含认证值的场景摘要。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def openai_payload(model: str) -> dict[str, Any]:
    """构造同时含业务字段与明确非官方固定字段的非 Lite 请求。"""

    return {
        "model": model,
        "instructions": "THIRD_PARTY_NONLITE_SYSTEM_V1",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "只回复 THIRD_PARTY_NONLITE_OK，不调用工具。",
                    }
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "name": "third_party_lookup",
                "description": "仅用于验证工具定义守恒。",
                "parameters": {
                    "type": "object",
                    # 保真探针：2^53+1 无法被 float64 精确表示，一旦转换阶段走 map 往返
                    # 就会变成 9007199254740992 或科学计数法；z_first 排在 a_second 之前，
                    # 用于检出被按字典序重排的嵌套键。两者都必须逐字节出现在出站请求中。
                    "z_first": 9007199254740993,
                    "a_second": 1,
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ],
        "tool_choice": "required",
        "max_output_tokens": 123,
        "parallel_tool_calls": False,
        "store": True,
        "stream": False,
        "include": ["message.output_text.logprobs"],
        "reasoning": {
            "effort": "low",
            "summary": "auto",
            "context": "none",
        },
        "text": {"verbosity": "high"},
    }


def anthropic_payload(model: str) -> dict[str, Any]:
    """构造三块 system 与 defer_loading 工具，强制触发动态 beta 门禁。"""

    return {
        "model": model,
        "max_tokens": 64,
        "stream": False,
        "system": [
            {
                "type": "text",
                "text": "THIRD_PARTY_ANTHROPIC_SYSTEM_A",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "# Text output (does not apply to tool calls)",
            },
            {
                "type": "text",
                "text": "THIRD_PARTY_ANTHROPIC_SYSTEM_B",
            },
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "只回复 THIRD_PARTY_ANTHROPIC_OK，不调用工具。",
                    }
                ],
            }
        ],
        "tools": [
            {
                "name": "deferred_lookup",
                "description": "仅用于验证动态 tool-search beta。",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                "custom": {"defer_loading": True},
            }
        ],
        "tool_choice": {"type": "auto"},
    }


def run_http(
    *,
    path: str,
    payload: dict[str, Any],
    api_key: str,
    anthropic: bool,
    timeout: int,
) -> dict[str, Any]:
    """通过 ingress 反向代理发送一次请求，并返回不含响应正文的摘要。"""

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "external-review-client/1.0",
        # 真实第三方客户端（VSCode 内的 Kilo/Cline/Cursor、Stainless 生成的 SDK）会带这些
        # 宿主环境头，官方 Claude Code / Codex CLI 从不发送。定向客户端必须一并携带，
        # 否则官方出站的剥离逻辑在抓包里根本不会被触发，contract_equal=true 也证明不了它。
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8",
        "sec-fetch-mode": "cors",
        "x-stainless-helper-method": "stream",
    }
    if anthropic:
        headers.update(
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        )
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    connection = http.client.HTTPConnection("127.0.0.1", 18081, timeout=timeout)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
    finally:
        connection.close()
    return {
        "status": response.status,
        "response_bytes": len(response_body),
        "response_sha256": hashlib.sha256(response_body).hexdigest(),
        "upstream_success": 200 <= response.status < 300,
        "request_completed": True,
    }


def websocket_frame(payload: bytes, opcode: int = 1) -> bytes:
    """编码一个符合 RFC 6455 的客户端掩码帧。"""

    mask = secrets.token_bytes(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header)


def receive_exact(stream: socket.socket, length: int) -> bytes:
    """读取指定长度，连接提前关闭时立即失败。"""

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise RuntimeError("WebSocket 连接在帧结束前关闭。")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def receive_websocket_frame(stream: socket.socket) -> tuple[int, bytes]:
    """读取一个服务端 WebSocket 帧。"""

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
            byte ^ mask[index % 4] for index, byte in enumerate(payload)
        )
    return opcode, payload


def run_websocket(
    *, payload: dict[str, Any], api_key: str, timeout: int
) -> dict[str, Any]:
    """用非 Codex 身份完成 WebSocket 握手并发送 response.create。"""

    stream = socket.create_connection(("127.0.0.1", 18081), timeout=timeout)
    stream.settimeout(timeout)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = "\r\n".join(
        [
            "GET /v1/responses HTTP/1.1",
            "Host: 127.0.0.1:18081",
            "Connection: Upgrade",
            "Upgrade: websocket",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {key}",
            f"Authorization: Bearer {api_key}",
            "User-Agent: external-review-ws/1.0",
            "OpenAI-Beta: responses_websockets=2026-02-06",
            "",
            "",
        ]
    ).encode("ascii")
    try:
        stream.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(stream.recv(4096))
            if len(response) > 65536:
                raise RuntimeError("WebSocket 握手响应过大。")
        status_line = bytes(response).split(b"\r\n", 1)[0].decode(
            "ascii", "replace"
        )
        if " 101 " not in status_line:
            raise RuntimeError(f"WebSocket 握手失败：{status_line}")
        frame_payload = dict(payload)
        frame_payload["type"] = "response.create"
        stream.sendall(
            websocket_frame(
                json.dumps(
                    frame_payload, ensure_ascii=False, separators=(",", ":")
                ).encode()
            )
        )
        event_types: list[str] = []
        terminal = ""
        for _ in range(512):
            opcode, content = receive_websocket_frame(stream)
            if opcode == 8:
                break
            if opcode == 9:
                stream.sendall(websocket_frame(content, opcode=10))
                continue
            if opcode != 1:
                continue
            try:
                event = json.loads(content)
            except ValueError:
                continue
            event_type = event.get("type") if isinstance(event, dict) else None
            if isinstance(event_type, str):
                event_types.append(event_type)
                if event_type in {
                    "response.completed",
                    "response.failed",
                    "error",
                }:
                    terminal = event_type
                    break
        return {
            "status": 101,
            "event_count": len(event_types),
            "terminal_event": terminal,
            "upstream_success": terminal == "response.completed",
            "request_completed": bool(terminal),
        }
    finally:
        stream.close()


def main() -> int:
    """解析参数、执行场景并持久化可审计摘要。"""

    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("openai-http", "openai-ws", "anthropic-http"),
        required=True,
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    arguments = parser.parse_args()
    api_key = os.environ.get("SUB2API_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("缺少 SUB2API_API_KEY。")

    if arguments.mode == "openai-http":
        result = run_http(
            path="/v1/responses",
            payload=openai_payload(arguments.model),
            api_key=api_key,
            anthropic=False,
            timeout=arguments.timeout,
        )
    elif arguments.mode == "openai-ws":
        result = run_websocket(
            payload=openai_payload(arguments.model),
            api_key=api_key,
            timeout=arguments.timeout,
        )
    else:
        result = run_http(
            path="/v1/messages",
            payload=anthropic_payload(arguments.model),
            api_key=api_key,
            anthropic=True,
            timeout=arguments.timeout,
        )

    summary = {
        "schema_version": "third-party-official-profile-scenario/v1",
        "mode": arguments.mode,
        "model": arguments.model,
        **result,
        "valid": bool(result.get("request_completed")),
    }
    secure_write_json(arguments.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
