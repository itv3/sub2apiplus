#!/usr/bin/env python3
"""从官方字节中继原件生成并校验模型条件成功收据。

收据只允许成功态：必须同时看到 `/models` 原始响应里的目标模型元数据，以及
Responses／legacy compact 原始请求体里的同一模型。任一字段缺失、Lite 条件不符或
实际请求模型发生 fallback，都不产出收据，由 capture job 失败关闭。
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path
from typing import Any, Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.capturelib.security import secure_write_json
from tools.official_client_capture.relay_extract import decompress_zstd


SCHEMA_VERSION = "codex-egress-model-condition-receipt/v2"
PREWARM_SCHEMA_VERSION = "codex-model-catalog-prewarm/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
TRACKS = {"main", "lite"}
BINDING_ROLES = frozenset(
    {
        "relay_manifest",
        "model_catalog_prewarm",
        "models_request",
        "models_response",
        "responses_request",
    }
)
REQUIRED_BINDING_ROLES = frozenset(
    {"relay_manifest", "models_request", "models_response", "responses_request"}
)
MAX_FILE_BYTES = 512 * 1024 * 1024


class ModelConditionReceiptError(ValueError):
    """模型条件证据不足、矛盾或收据结构非法。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bind(root: Path, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ModelConditionReceiptError(f"模型条件证据不是普通文件：{path}")
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise ModelConditionReceiptError(f"模型条件证据越过 evidence root：{path}") from error
    size = path.stat().st_size
    if not 0 < size <= MAX_FILE_BYTES:
        raise ModelConditionReceiptError(f"模型条件证据大小非法：{path}")
    return {
        "path": relative,
        "sha256": _sha256(path),
        "bytes": size,
    }


def _role_binding(root: Path, path: Path, *roles: str) -> dict[str, Any]:
    """给原始证据绑定明确角色，供后续编目按语义选择而不是猜连接号。"""

    normalized = sorted(set(roles))
    if not normalized or any(role not in BINDING_ROLES for role in normalized):
        raise ModelConditionReceiptError(f"模型条件证据角色非法：{normalized}")
    return {**_bind(root, path), "roles": normalized}


def _merge_role_bindings(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一路径可能同时承担多个角色；摘要坐标必须一致后才能合并。"""

    merged: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        path = binding["path"]
        existing = merged.get(path)
        if existing is None:
            merged[path] = {
                "path": path,
                "sha256": binding["sha256"],
                "bytes": binding["bytes"],
                "roles": sorted(set(binding["roles"])),
            }
            continue
        if (
            existing["sha256"] != binding["sha256"]
            or existing["bytes"] != binding["bytes"]
        ):
            raise ModelConditionReceiptError(f"同一路径模型条件绑定不一致：{path}")
        existing["roles"] = sorted(set(existing["roles"]) | set(binding["roles"]))
    return [merged[path] for path in sorted(merged)]


def _split_head(payload: bytes, start: int) -> tuple[list[str], int] | None:
    end = payload.find(b"\r\n\r\n", start)
    if end < 0:
        return None
    return payload[start:end].decode("latin-1", "replace").split("\r\n"), end + 4


def _headers(lines: list[str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            output.setdefault(name.strip().lower(), value.strip())
    return output


def _body(payload: bytes, headers: dict[str, str], start: int) -> tuple[bytes, int]:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        output = bytearray()
        cursor = start
        while True:
            line_end = payload.find(b"\r\n", cursor)
            if line_end < 0:
                raise ModelConditionReceiptError("chunked 报文缺少块长度行。")
            try:
                size = int(payload[cursor:line_end].split(b";", 1)[0], 16)
            except ValueError as error:
                raise ModelConditionReceiptError("chunked 报文块长度非法。") from error
            cursor = line_end + 2
            if size == 0:
                trailer_end = payload.find(b"\r\n\r\n", cursor)
                return bytes(output), trailer_end + 4 if trailer_end >= 0 else cursor + 2
            end = cursor + size
            if end + 2 > len(payload) or payload[end:end + 2] != b"\r\n":
                raise ModelConditionReceiptError("chunked 报文块不完整。")
            output.extend(payload[cursor:end])
            cursor = end + 2
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as error:
        raise ModelConditionReceiptError("Content-Length 非法。") from error
    end = start + length
    if end > len(payload):
        raise ModelConditionReceiptError("HTTP 报文体短于 Content-Length。")
    return payload[start:end], end


def _iter_messages(payload: bytes, *, response: bool) -> Iterator[dict[str, Any]]:
    cursor = 0
    while cursor < len(payload):
        parsed = _split_head(payload, cursor)
        if parsed is None:
            return
        lines, body_start = parsed
        if not lines:
            return
        if response:
            if not lines[0].startswith("HTTP/1."):
                return
            parts = lines[0].split(" ")
            if len(parts) < 2 or not parts[1].isdigit():
                return
            identity: dict[str, Any] = {"status": int(parts[1])}
        else:
            if " HTTP/1." not in lines[0]:
                return
            parts = lines[0].split(" ")
            if len(parts) < 3:
                return
            identity = {"method": parts[0], "target": parts[1]}
        headers = _headers(lines)
        body, cursor = _body(payload, headers, body_start)
        yield {**identity, "headers": headers, "body": body}
        if cursor <= body_start:
            cursor = body_start


def _json_body(message: dict[str, Any]) -> Any:
    raw = message["body"]
    encoding = message["headers"].get("content-encoding", "").lower()
    if "zstd" in encoding and raw:
        raw = decompress_zstd(raw)
    elif "gzip" in encoding and raw:
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ModelConditionReceiptError("模型条件 HTTP body 不是 UTF-8 JSON。") from error


def _ws_request_models(raw: bytes) -> list[str]:
    """从 WS 帧里取出客户端声明的模型。

    `relay_extract.parse_ws_frames` 刻意只保留结构、不留取值（payload 含对话
    内容），因此这里必须自己解一遍帧。解法与那边逐字一致：客户端帧带 mask 要
    先解掩码；permessage-deflate 默认**上下文接管**，滑动窗口跨帧共享，所以整条
    连接只能用一个解压器——逐帧新建会让第 2 帧起全部失败。

    只取顶层 `model` 字段的值。取不到就返回空列表，由调用方决定是否失败关闭。
    """

    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    models: list[str] = []
    # 跳过握手，帧区从第一个空行之后开始。
    head_end = raw.find(b"\r\n\r\n")
    if head_end < 0:
        return models
    data = raw[head_end + 4:]
    pos = 0
    payload_buffer = bytearray()
    message_compressed = False
    collecting = False

    def flush() -> None:
        nonlocal collecting, message_compressed
        if not collecting:
            return
        body = bytes(payload_buffer)
        if message_compressed:
            try:
                body = inflater.decompress(body + b"\x00\x00\xff\xff")
            except zlib.error:
                payload_buffer.clear()
                collecting = False
                message_compressed = False
                return
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            model = obj.get("model")
            if isinstance(model, str) and model:
                models.append(model)
        payload_buffer.clear()
        collecting = False
        message_compressed = False

    while pos + 2 <= len(data):
        b0, b1 = data[pos], data[pos + 1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        rsv1 = bool(b0 & 0x40)
        masked, length = bool(b1 & 0x80), b1 & 0x7F
        cur = pos + 2
        if length == 126:
            if cur + 2 > len(data):
                break
            length = int.from_bytes(data[cur:cur + 2], "big")
            cur += 2
        elif length == 127:
            if cur + 8 > len(data):
                break
            length = int.from_bytes(data[cur:cur + 8], "big")
            cur += 8
        mask = b""
        if masked:
            if cur + 4 > len(data):
                break
            mask = data[cur:cur + 4]
            cur += 4
        if cur + length > len(data):
            break
        payload = data[cur:cur + length]
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x1:
            payload_buffer.clear()
            payload_buffer.extend(payload)
            message_compressed = rsv1
            collecting = True
            if fin:
                flush()
        elif opcode == 0x0 and collecting:
            payload_buffer.extend(payload)
            if fin:
                flush()
        pos = cur + length
    return models


def _responses_request_models(raw: bytes) -> list[str]:
    """从一份客户端原始连接中提取 HTTP／WS Responses 请求模型。"""

    models: list[str] = []
    for request in _iter_messages(raw, response=False):
        path = request["target"].split("?", 1)[0]
        if request["method"] != "POST" or path not in {
            "/backend-api/codex/responses",
            "/backend-api/codex/responses/compact",
        }:
            continue
        body = _json_body(request)
        model = body.get("model") if isinstance(body, dict) else None
        if not isinstance(model, str) or not model:
            raise ModelConditionReceiptError("Responses 请求体缺少字符串 model。")
        models.append(model)
    # WS 传输下业务请求位于 Upgrade 后的帧中，与 HTTP 结果取并集。
    models.extend(_ws_request_models(raw))
    return models


def _load_relay(root: Path) -> dict[str, Any]:
    path = root / "relay" / "relay.json"
    if path.is_symlink() or not path.is_file():
        raise ModelConditionReceiptError("缺少 relay/relay.json。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelConditionReceiptError(f"relay/relay.json 不可读：{error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("connections"), list):
        raise ModelConditionReceiptError("relay/relay.json connections 非法。")
    return payload


def _manifest_proves_request_only_connection(
    connection: dict[str, Any],
    *,
    request_path: Path,
    response_path: Path,
) -> bool:
    """确认单向请求是 relay 明确记录的零响应事实，而不是原始文件丢失。"""

    if (
        connection.get("valid") is not True
        or "error" in connection
        or request_path.is_symlink()
        or not request_path.is_file()
        or response_path.exists()
        or response_path.is_symlink()
    ):
        return False
    byte_counts = connection.get("bytes")
    digests = connection.get("sha256")
    segments = connection.get("segments")
    if (
        not isinstance(byte_counts, dict)
        or set(byte_counts) != {"client_to_upstream"}
        or not isinstance(digests, dict)
        or set(digests) != {"client_to_upstream"}
        or not isinstance(segments, list)
        or not segments
    ):
        return False
    expected_offset = 0
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or segment.get("direction") != "client_to_upstream"
            or isinstance(segment.get("offset"), bool)
            or not isinstance(segment.get("offset"), int)
            or segment["offset"] != expected_offset
            or isinstance(segment.get("length"), bool)
            or not isinstance(segment.get("length"), int)
            or segment["length"] <= 0
        ):
            return False
        expected_offset += segment["length"]
    return (
        byte_counts["client_to_upstream"] == expected_offset
        and request_path.stat().st_size == expected_offset
        and digests["client_to_upstream"] == _sha256(request_path)
    )


def _manifest_proves_scrubbed_connection(
    relay: dict[str, Any],
    connection_id: int,
    *,
    request_binding: dict[str, Any],
    response_binding: dict[str, Any],
) -> bool:
    """确认预热原件只发生了 manifest 声明的等长凭据脱敏。"""

    if relay.get("credential_scrubbing") != {
        "method": "equal_length_replacement",
        "byte_offsets_preserved": True,
        "hashes_recomputed": True,
    }:
        return False
    matches = [
        item
        for item in relay.get("connections", [])
        if isinstance(item, dict) and item.get("connection_id") == connection_id
    ]
    if len(matches) != 1:
        return False
    connection = matches[0]
    expected_bytes = {
        "client_to_upstream": request_binding["bytes"],
        "upstream_to_client": response_binding["bytes"],
    }
    expected_sha256 = {
        "client_to_upstream": request_binding["sha256"],
        "upstream_to_client": response_binding["sha256"],
    }
    if (
        connection.get("valid") is not True
        or connection.get("error") is not None
        or connection.get("bytes") != expected_bytes
        or connection.get("sha256") != expected_sha256
    ):
        return False
    segments = connection.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    offsets = {"client_to_upstream": 0, "upstream_to_client": 0}
    for segment in segments:
        if not isinstance(segment, dict):
            return False
        direction = segment.get("direction")
        offset = segment.get("offset")
        length = segment.get("length")
        if (
            direction not in offsets
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset != offsets[direction]
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length <= 0
        ):
            return False
        offsets[direction] += length
    return offsets == expected_bytes


def _load_prewarm_catalog(
    root: Path,
    path: Path,
    *,
    relay: dict[str, Any],
    expected_model: str,
    expected_lite: bool,
) -> tuple[int, Path, list[dict[str, Any]]]:
    """重放预热摘要绑定的唯一完整模型目录响应。

    app-server 初始化时会并发拉取多个大目录；`model/list` 完成后关闭隔离进程，其他
    连接可能只收到响应头。预热摘要已经绑定其中一份完整 HTTP 200，因此最终收据只
    重放这条明确坐标，避免把同进程主动结束形成的半响应误判为原始证据丢失。
    """

    if not path.is_absolute():
        path = root / path
    binding = _role_binding(root, path, "model_catalog_prewarm")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelConditionReceiptError(f"模型目录预热摘要不可读：{error}") from error
    required = {
        "schema_version",
        "status",
        "codex_version",
        "model_id",
        "use_responses_lite",
        "protocol_record_count",
        "model_count",
        "capture",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ModelConditionReceiptError("模型目录预热摘要字段不闭合。")
    if (
        payload.get("schema_version") != PREWARM_SCHEMA_VERSION
        or payload.get("status") != "success"
        or not VERSION_RE.fullmatch(str(payload.get("codex_version", "")))
        or payload.get("model_id") != expected_model
        or payload.get("use_responses_lite") is not expected_lite
    ):
        raise ModelConditionReceiptError("模型目录预热摘要坐标不匹配。")
    for field in ("protocol_record_count", "model_count"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ModelConditionReceiptError(f"模型目录预热摘要 {field} 非法。")

    capture = payload.get("capture")
    capture_fields = {
        "connection_id",
        "request_path",
        "request_sha256",
        "response_path",
        "response_sha256",
        "use_responses_lite",
    }
    if not isinstance(capture, dict) or set(capture) != capture_fields:
        raise ModelConditionReceiptError("模型目录预热 capture 字段不闭合。")
    connection_id = capture.get("connection_id")
    if (
        isinstance(connection_id, bool)
        or not isinstance(connection_id, int)
        or connection_id <= 0
        or capture.get("use_responses_lite") is not expected_lite
    ):
        raise ModelConditionReceiptError("模型目录预热 capture 坐标非法。")
    expected_request = f"relay/conn{connection_id:03d}.client_to_upstream.bin"
    expected_response = f"relay/conn{connection_id:03d}.upstream_to_client.bin"
    if (
        capture.get("request_path") != expected_request
        or capture.get("response_path") != expected_response
        or not SHA256_RE.fullmatch(str(capture.get("request_sha256", "")))
        or not SHA256_RE.fullmatch(str(capture.get("response_sha256", "")))
    ):
        raise ModelConditionReceiptError("模型目录预热 capture 路径或摘要非法。")

    request_path = root / expected_request
    response_path = root / expected_response
    request_binding = _role_binding(root, request_path, "models_request")
    response_binding = _role_binding(root, response_path, "models_response")
    pre_scrub_digests_match = (
        request_binding["sha256"] == capture["request_sha256"]
        and response_binding["sha256"] == capture["response_sha256"]
    )
    if not pre_scrub_digests_match and not _manifest_proves_scrubbed_connection(
        relay,
        connection_id,
        request_binding=request_binding,
        response_binding=response_binding,
    ):
        raise ModelConditionReceiptError(
            "模型目录预热原始字节摘要不一致，且最终 manifest 未证明等长脱敏。"
        )
    requests = list(_iter_messages(request_path.read_bytes(), response=False))
    if not any(
        request.get("method") == "GET"
        and str(request.get("target", "")).split("?", 1)[0]
        == "/backend-api/codex/models"
        for request in requests
    ):
        raise ModelConditionReceiptError("模型目录预热请求不是 /models。")
    responses = list(_iter_messages(response_path.read_bytes(), response=True))
    if len(responses) != 1 or responses[0].get("status") != 200:
        raise ModelConditionReceiptError("模型目录预热响应不是唯一完整 HTTP 200。")
    models_payload = _json_body(responses[0])
    models = models_payload.get("models") if isinstance(models_payload, dict) else None
    matches = [
        item
        for item in models or []
        if isinstance(item, dict) and item.get("slug") == expected_model
    ]
    if (
        len(matches) != 1
        or matches[0].get("use_responses_lite") is not expected_lite
    ):
        raise ModelConditionReceiptError("模型目录预热响应未证明目标模型 Lite 条件。")
    return connection_id, response_path, [binding, request_binding, response_binding]


def build_receipt(
    *,
    root: Path,
    job_id: str,
    run_id: str,
    track: str,
    expected_model: str,
    expected_lite: bool,
    model_catalog_prewarm: Path | None = None,
) -> dict[str, Any]:
    """从原始 relay 字节建立成功收据，条件不成立即抛错。"""

    if root.is_symlink() or not root.is_dir():
        raise ModelConditionReceiptError("evidence root 必须是非符号链接目录。")
    if not SAFE_ID_RE.fullmatch(job_id) or not SAFE_ID_RE.fullmatch(run_id):
        raise ModelConditionReceiptError("job_id 或 run_id 非法。")
    if track not in TRACKS:
        raise ModelConditionReceiptError("track 只能是 main 或 lite。")
    if not isinstance(expected_model, str) or not expected_model.strip():
        raise ModelConditionReceiptError("expected_model 不能为空。")
    if not isinstance(expected_lite, bool):
        raise ModelConditionReceiptError("expected_lite 必须是布尔值。")

    relay = _load_relay(root)
    validated_connections: list[tuple[int, dict[str, Any]]] = []
    connection_ids: set[int] = set()
    for connection in relay["connections"]:
        connection_id = (
            connection.get("connection_id") if isinstance(connection, dict) else None
        )
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id <= 0
            or connection_id in connection_ids
        ):
            raise ModelConditionReceiptError(
                "relay/relay.json connection_id 非法或重复。"
            )
        connection_ids.add(connection_id)
        validated_connections.append((connection_id, connection))
    bindings: list[dict[str, Any]] = [
        _role_binding(root, root / "relay" / "relay.json", "relay_manifest")
    ]
    models_response_paths: list[Path] = []
    actual_lites: list[bool] = []
    if model_catalog_prewarm is not None:
        prewarm_connection_id, response_path, prewarm_bindings = _load_prewarm_catalog(
            root,
            model_catalog_prewarm,
            relay=relay,
            expected_model=expected_model,
            expected_lite=expected_lite,
        )
        if prewarm_connection_id not in connection_ids:
            raise ModelConditionReceiptError(
                "模型目录预热连接未登记在 relay manifest。"
            )
        models_response_paths.append(response_path)
        actual_lites.append(expected_lite)
        bindings.extend(prewarm_bindings)
    else:
        model_connections: list[tuple[int, Path, dict[str, Any]]] = []
        for connection_id, connection in validated_connections:
            request_path = (
                root
                / "relay"
                / f"conn{connection_id:03d}.client_to_upstream.bin"
            )
            if request_path.is_symlink() or not request_path.is_file():
                # 客户端会短暂建立后立即放弃一条 TLS 连接；中继仍会登记连接的打开／
                # 关闭时间，但没有任何方向的字节、摘要或 segment，因此不会生成 .bin。
                # 这种严格意义上的空连接不承载模型条件事实，应从分母中排除。
                response_path = (
                    root
                    / "relay"
                    / f"conn{connection_id:03d}.upstream_to_client.bin"
                )
                if (
                    isinstance(connection, dict)
                    and connection.get("bytes") == {}
                    and connection.get("sha256") == {}
                    and connection.get("segments") == []
                    and not response_path.exists()
                    and not response_path.is_symlink()
                ):
                    continue
                raise ModelConditionReceiptError(
                    f"缺少连接 {connection_id} 的原始请求字节。"
                )
            for request in _iter_messages(request_path.read_bytes(), response=False):
                request_target = request["target"].split("?", 1)[0]
                if (
                    request["method"] == "GET"
                    and request_target == "/backend-api/codex/models"
                ):
                    model_connections.append((connection_id, request_path, connection))
        if not model_connections:
            raise ModelConditionReceiptError("至少需要一条 /models 原始连接。")
        # relay manifest 是单向连接排除判据的一部分，必须和原始请求一起进入收据绑定。
        for connection_id, models_request_path, connection in sorted(
            model_connections,
            key=lambda item: item[0],
        ):
            response_path = (
                root
                / "relay"
                / f"conn{connection_id:03d}.upstream_to_client.bin"
            )
            if response_path.is_symlink() or not response_path.is_file():
                if _manifest_proves_request_only_connection(
                    connection,
                    request_path=models_request_path,
                    response_path=response_path,
                ):
                    bindings.append(
                        _role_binding(root, models_request_path, "models_request")
                    )
                    continue
                raise ModelConditionReceiptError(
                    f"缺少连接 {connection_id} 的原始响应字节。"
                )
            responses = list(_iter_messages(response_path.read_bytes(), response=True))
            if len(responses) != 1 or responses[0]["status"] != 200:
                raise ModelConditionReceiptError(
                    "每条 /models 连接必须恰好返回一个 HTTP 200。"
                )
            models_payload = _json_body(responses[0])
            models = (
                models_payload.get("models")
                if isinstance(models_payload, dict)
                else None
            )
            matches = [
                item
                for item in models or []
                if isinstance(item, dict) and item.get("slug") == expected_model
            ]
            if (
                len(matches) != 1
                or not isinstance(matches[0].get("use_responses_lite"), bool)
            ):
                raise ModelConditionReceiptError(
                    "/models 未唯一给出目标模型的 Lite 元数据。"
                )
            actual_lites.append(matches[0]["use_responses_lite"])
            models_response_paths.append(response_path)
            bindings.extend(
                (
                    _role_binding(root, models_request_path, "models_request"),
                    _role_binding(root, response_path, "models_response"),
                )
            )
    if not models_response_paths:
        raise ModelConditionReceiptError("没有取得完整的 /models HTTP 200 原始响应。")
    if any(actual_lite is not expected_lite for actual_lite in actual_lites):
        raise ModelConditionReceiptError(
            f"目标模型 use_responses_lite={sorted(set(actual_lites))}，预期为 {expected_lite}。"
        )
    actual_lite = expected_lite
    request_models: list[str] = []
    for request_path in sorted((root / "relay").glob("conn*.client_to_upstream.bin")):
        raw = request_path.read_bytes()
        models = _responses_request_models(raw)
        request_models.extend(models)
        if models:
            bindings.append(_role_binding(root, request_path, "responses_request"))
    if not request_models:
        raise ModelConditionReceiptError(
            "未见可绑定的 Responses／compact 原始请求模型（HTTP 与 WS 帧均未取到）。"
        )
    fallback = any(model != expected_model for model in request_models)
    if fallback:
        raise ModelConditionReceiptError(
            f"实际请求模型发生 fallback：{sorted(set(request_models))}。"
        )
    bindings = _merge_role_bindings(bindings)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "job_id": job_id,
        "run_id": run_id,
        "track": track,
        "model_id": expected_model,
        # 多 provider 初始化可能各拉一次 models；字段绑定按连接号最小的首份响应，
        # 所有其余响应仍在 evidence_bindings 中，且上面逐份验证 Lite 元数据一致。
        "models_response_sha256": _sha256(models_response_paths[0]),
        "use_responses_lite": actual_lite,
        "model_fallback": False,
        "observed_request_models": sorted(set(request_models)),
        "evidence_root": str(root.resolve()),
        "evidence_bindings": bindings,
    }


def validate_receipt(
    payload: Any,
    *,
    root: Path,
    job_id: str,
    track: str,
    model_id: str,
    use_responses_lite: bool,
) -> dict[str, Any]:
    """严格校验收据结构、期望坐标、角色语义与所有 evidence binding。"""

    required = {
        "schema_version",
        "status",
        "job_id",
        "run_id",
        "track",
        "model_id",
        "models_response_sha256",
        "use_responses_lite",
        "model_fallback",
        "observed_request_models",
        "evidence_root",
        "evidence_bindings",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ModelConditionReceiptError("模型条件收据字段不闭合。")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "job_id": job_id,
        "track": track,
        "model_id": model_id,
        "use_responses_lite": use_responses_lite,
        "model_fallback": False,
        "evidence_root": str(root.resolve()),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ModelConditionReceiptError(f"模型条件收据 {key} 不一致。")
    if not SAFE_ID_RE.fullmatch(str(payload.get("run_id", ""))):
        raise ModelConditionReceiptError("模型条件收据 run_id 非法。")
    if payload.get("track") not in TRACKS:
        raise ModelConditionReceiptError("模型条件收据 track 非法。")
    if not isinstance(payload.get("model_id"), str) or not payload["model_id"]:
        raise ModelConditionReceiptError("模型条件收据 model_id 非法。")
    if not isinstance(payload.get("use_responses_lite"), bool):
        raise ModelConditionReceiptError("模型条件收据 use_responses_lite 非法。")
    if not SHA256_RE.fullmatch(str(payload.get("models_response_sha256", ""))):
        raise ModelConditionReceiptError("models_response_sha256 非法。")
    if payload.get("observed_request_models") != [model_id]:
        raise ModelConditionReceiptError("observed_request_models 不匹配目标模型。")
    bindings = payload.get("evidence_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ModelConditionReceiptError("模型条件收据 evidence_bindings 不能为空。")
    seen: set[str] = set()
    by_role: dict[str, list[tuple[Path, dict[str, Any]]]] = {
        role: [] for role in BINDING_ROLES
    }
    response_match = False
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "bytes",
            "roles",
        }:
            raise ModelConditionReceiptError("模型条件收据 binding 字段不闭合。")
        path_value = item["path"]
        roles = item["roles"]
        if (
            not isinstance(path_value, str)
            or not path_value
            or path_value.startswith("/")
            or ".." in Path(path_value).parts
            or path_value in seen
        ):
            raise ModelConditionReceiptError("模型条件收据 binding 路径非法或重复。")
        if (
            not isinstance(roles, list)
            or not roles
            or roles != sorted(set(roles))
            or any(role not in BINDING_ROLES for role in roles)
        ):
            raise ModelConditionReceiptError("模型条件收据 binding 角色非法。")
        seen.add(path_value)
        path = root / path_value
        actual = _bind(root, path)
        expected_binding = {key: item[key] for key in ("path", "sha256", "bytes")}
        if actual != expected_binding:
            raise ModelConditionReceiptError(f"模型条件证据摘要不一致：{path_value}")
        for role in roles:
            by_role[role].append((path, item))
        if "models_response" in roles:
            response_match = (
                response_match
                or item["sha256"] == payload["models_response_sha256"]
            )
    missing_roles = sorted(
        role for role in REQUIRED_BINDING_ROLES if not by_role[role]
    )
    if missing_roles:
        raise ModelConditionReceiptError(
            f"模型条件收据缺少必需证据角色：{missing_roles}"
        )
    relay_manifests = by_role["relay_manifest"]
    if len(relay_manifests) != 1 or relay_manifests[0][1]["path"] != "relay/relay.json":
        raise ModelConditionReceiptError("relay_manifest 角色坐标非法。")
    for path, _binding in by_role["models_request"]:
        requests = list(_iter_messages(path.read_bytes(), response=False))
        if not any(
            request.get("method") == "GET"
            and str(request.get("target", "")).split("?", 1)[0]
            == "/backend-api/codex/models"
            for request in requests
        ):
            raise ModelConditionReceiptError(
                f"models_request 角色未绑定 /models 请求：{path}"
            )
    for path, _binding in by_role["models_response"]:
        responses = list(_iter_messages(path.read_bytes(), response=True))
        if len(responses) != 1 or responses[0].get("status") != 200:
            raise ModelConditionReceiptError(
                f"models_response 角色不是唯一 HTTP 200：{path}"
            )
        models_payload = _json_body(responses[0])
        models = models_payload.get("models") if isinstance(models_payload, dict) else None
        matches = [
            item
            for item in models or []
            if isinstance(item, dict) and item.get("slug") == model_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("use_responses_lite") is not use_responses_lite
        ):
            raise ModelConditionReceiptError(
                f"models_response 角色未证明目标模型条件：{path}"
            )
    role_request_models: list[str] = []
    for path, _binding in by_role["responses_request"]:
        models = _responses_request_models(path.read_bytes())
        if not models:
            raise ModelConditionReceiptError(
                f"responses_request 角色未绑定业务请求：{path}"
            )
        role_request_models.extend(models)
    if sorted(set(role_request_models)) != [model_id]:
        raise ModelConditionReceiptError("responses_request 角色模型不匹配。")
    if not response_match:
        raise ModelConditionReceiptError("models_response_sha256 未绑定原始响应文件。")
    return dict(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--track", choices=tuple(sorted(TRACKS)), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--expect-use-responses-lite", choices=("true", "false"), required=True)
    parser.add_argument("--model-catalog-prewarm", type=Path)
    arguments = parser.parse_args()
    expected_lite = arguments.expect_use_responses_lite == "true"
    receipt = build_receipt(
        root=arguments.run_root,
        job_id=arguments.job_id,
        run_id=arguments.run_id,
        track=arguments.track,
        expected_model=arguments.model,
        expected_lite=expected_lite,
        model_catalog_prewarm=arguments.model_catalog_prewarm,
    )
    secure_write_json(arguments.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
