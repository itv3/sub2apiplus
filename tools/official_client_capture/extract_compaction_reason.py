#!/usr/bin/env python3
"""从官方出站原始字节中提取压缩原因的最小脱敏证据。

输出只保留固定枚举、模型名与 ``input`` 项类型，不保存请求 ID、账号 ID、会话 ID、
OAuth 凭据或对话正文。默认 Responses WebSocket 使用 permessage-deflate，因此必须按
连接复用同一个解压器，不能逐帧独立解压。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import pathlib
import re
import sys
import zlib

try:
    from relay_extract import decompress_zstd
except ImportError:
    from tools.official_client_capture.relay_extract import decompress_zstd


ALLOWED_METADATA = {
    "trigger": {"auto", "manual"},
    "reason": {
        "user_requested",
        "context_limit",
        "model_downshift",
        "comp_hash_changed",
    },
    "implementation": {
        "responses",
        "responses_compaction_v2",
        "responses_compact",
    },
    "phase": {"standalone_turn", "pre_turn", "mid_turn"},
    "strategy": {"memento", "summarization"},
}

# 必须覆盖 capturelib.model 的 MAIN_TRACK_MODELS 与 LITE_TRACK_MODELS，
# 否则换主线模型后 compact 证据会被判为未知模型而整条丢弃；
# tests/test_main_track_models.py 锁定这层覆盖关系。
ALLOWED_MODELS = {
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
}
ALLOWED_INPUT_TYPES = {
    "additional_tools",
    "compaction",
    "compaction_trigger",
    "custom_tool_call",
    "custom_tool_call_output",
    "function_call",
    "function_call_output",
    "local_shell_call",
    "message",
    "reasoning",
    "shell_call",
    "shell_call_output",
}

EXPECTED_REASON_PROFILES = {
    "user_requested": {
        "trigger": "manual",
        "phases": {"standalone_turn"},
    },
    "context_limit": {
        "trigger": "auto",
        "phases": {"pre_turn", "mid_turn"},
    },
    "model_downshift": {
        "trigger": "auto",
        "phases": {"pre_turn"},
    },
    "comp_hash_changed": {
        "trigger": "auto",
        "phases": {"pre_turn"},
    },
}


def safe_enum(field: str, value: object) -> str:
    """只允许生产枚举进入外发摘要，意外字符串一律不原样输出。"""
    if isinstance(value, str) and value in ALLOWED_METADATA[field]:
        return value
    return "<unexpected>"


def safe_match(obj: object, source: str, transport: str) -> dict | None:
    """从 response.create 中抽取与压缩触发有关的非敏感字段。"""
    if not isinstance(obj, dict) or obj.get("type") != "response.create":
        return None
    items = obj.get("input")
    if not isinstance(items, list):
        return None
    raw_input_types = [
        item.get("type")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    ]
    if "compaction_trigger" not in raw_input_types:
        return None
    input_types = [
        item_type if item_type in ALLOWED_INPUT_TYPES else "<unexpected>"
        for item_type in raw_input_types
    ]

    client_metadata = obj.get("client_metadata")
    if not isinstance(client_metadata, dict):
        return None
    raw_metadata = client_metadata.get("x-codex-turn-metadata")
    if not isinstance(raw_metadata, str):
        return None
    try:
        metadata = json.loads(raw_metadata)
    except ValueError:
        return None
    compaction = metadata.get("compaction") if isinstance(metadata, dict) else None
    if not isinstance(compaction, dict):
        return None

    model = obj.get("model")
    return {
        "source": source,
        "transport": transport,
        "event_type": "response.create",
        "model": model if model in ALLOWED_MODELS else "<unexpected>",
        "input_types": input_types,
        "request_kind": (
            "compaction" if metadata.get("request_kind") == "compaction" else "<unexpected>"
        ),
        "compaction": {
            field: safe_enum(field, compaction.get(field))
            for field in ("trigger", "reason", "implementation", "phase", "strategy")
        },
    }


def ws_json_objects(data: bytes):
    """解析一条客户端 WS 字节流，产出解掩码、解压后的 JSON 对象。"""
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    pos = 0
    message_opcode: int | None = None
    message_compressed = False
    message = bytearray()

    def decode_message(payload: bytes, compressed: bool):
        if compressed:
            try:
                payload = inflater.decompress(payload + b"\x00\x00\xff\xff")
            except zlib.error:
                return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    while pos + 2 <= len(data):
        first, second = data[pos], data[pos + 1]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        compressed = bool(first & 0x40)
        masked = bool(second & 0x80)
        length = second & 0x7F
        cursor = pos + 2
        if length == 126:
            if cursor + 2 > len(data):
                return
            length = int.from_bytes(data[cursor:cursor + 2], "big")
            cursor += 2
        elif length == 127:
            if cursor + 8 > len(data):
                return
            length = int.from_bytes(data[cursor:cursor + 8], "big")
            cursor += 8
        mask = b""
        if masked:
            if cursor + 4 > len(data):
                return
            mask = data[cursor:cursor + 4]
            cursor += 4
        if cursor + length > len(data):
            return
        payload = data[cursor:cursor + length]
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x1:
            message_opcode = opcode
            message_compressed = compressed
            message = bytearray(payload)
            if final:
                decoded = decode_message(bytes(message), message_compressed)
                if decoded is not None:
                    yield decoded
                message_opcode = None
                message.clear()
        elif opcode == 0x0 and message_opcode == 0x1:
            message.extend(payload)
            if final:
                decoded = decode_message(bytes(message), message_compressed)
                if decoded is not None:
                    yield decoded
                message_opcode = None
                message.clear()
        pos = cursor + length


def http_json_objects(data: bytes):
    """解析连续 HTTP/1 请求；用于 WS 回退或显式 HTTP provider 的对照。"""
    pos = 0
    while True:
        end = data.find(b"\r\n\r\n", pos)
        if end < 0:
            return
        head = data[pos:end].decode("latin-1", "replace")
        lines = head.split("\r\n")
        if not lines or " HTTP/1." not in lines[0]:
            return
        content_length = 0
        content_encoding = ""
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator:
                continue
            if name.lower() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = 0
            elif name.lower() == "content-encoding":
                content_encoding = value.strip().lower()
        body_start = end + 4
        body = data[body_start:body_start + content_length]
        if content_encoding == "zstd" and body:
            try:
                body = decompress_zstd(body)
            except Exception:  # noqa: BLE001
                body = b""
        if body:
            try:
                yield json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                pass
        pos = body_start + content_length


def collect(relay_dir: pathlib.Path) -> list[dict]:
    """扫描全部上行连接，同时兼容默认 WS 与 HTTP 回退。"""
    matches: list[dict] = []
    pattern = str(relay_dir / "*client_to_upstream.bin")
    for name in sorted(glob.glob(pattern)):
        data = pathlib.Path(name).read_bytes()
        source = os.path.basename(name)
        head_end = data.find(b"\r\n\r\n")
        if head_end >= 0 and b"upgrade: websocket" in data[:head_end].lower():
            objects = ws_json_objects(data[head_end + 4:])
            transport = "websocket"
        else:
            objects = http_json_objects(data)
            transport = "http"
        for obj in objects:
            match = safe_match(obj, source, transport)
            if match:
                matches.append(match)
    return matches


def connection_integrity(relay_dir: pathlib.Path) -> dict:
    """复算每条连接两向字节是否齐全，拒绝残缺样本取得 complete。"""
    connections: dict[str, dict[str, int]] = {}
    for path in relay_dir.glob("*.bin"):
        match = re.fullmatch(
            r"(conn\d+)\.(client_to_upstream|upstream_to_client)\.bin",
            path.name,
        )
        if not match:
            continue
        connections.setdefault(match.group(1), {})[match.group(2)] = path.stat().st_size
    both = upstream_only = downstream_only = idle = 0
    for directions in connections.values():
        upstream = directions.get("client_to_upstream", 0)
        downstream = directions.get("upstream_to_client", 0)
        if upstream and downstream:
            both += 1
        elif upstream:
            upstream_only += 1
        elif downstream:
            downstream_only += 1
        else:
            idle += 1
    total = len(connections)
    return {
        "total": total,
        "both": both,
        "upstream_only": upstream_only,
        "downstream_only": downstream_only,
        "idle": idle,
        "clean": total > 0 and both == total,
    }


def matches_expected_profile(match: dict, expected_reason: str) -> bool:
    """按源码允许的 trigger／phase 组合核对指定压缩原因。"""
    profile = EXPECTED_REASON_PROFILES[expected_reason]
    compaction = match.get("compaction", {})
    return (
        match.get("request_kind") == "compaction"
        and compaction.get("trigger") == profile["trigger"]
        and compaction.get("reason") == expected_reason
        and compaction.get("implementation") == "responses_compaction_v2"
        and compaction.get("phase") in profile["phases"]
        and compaction.get("strategy") == "memento"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-reason", choices=sorted(ALLOWED_METADATA["reason"]),
                        required=True)
    args = parser.parse_args()

    relay_dir = pathlib.Path(args.relay_dir)
    matches = collect(relay_dir)
    integrity = connection_integrity(relay_dir)
    exact = [
        match
        for match in matches
        if matches_expected_profile(match, args.expected_reason)
    ]
    unexpected = [
        match for match in matches
        if match["compaction"].get("reason") != args.expected_reason
    ]
    complete = bool(exact) and integrity["clean"] and not unexpected
    result = {
        "schema_version": "compaction-reason-extract/v1",
        "expected_reason": args.expected_reason,
        "status": "complete" if complete else "incomplete",
        "exact_match_count": len(exact),
        "unexpected_reason_count": len(unexpected),
        "connection_integrity": integrity,
        "matches": matches,
    }
    output = pathlib.Path(args.output)
    fd = os.open(output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
