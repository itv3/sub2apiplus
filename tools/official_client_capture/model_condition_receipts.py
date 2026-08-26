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


SCHEMA_VERSION = "codex-egress-model-condition-receipt/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TRACKS = {"main", "lite"}
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


def build_receipt(
    *,
    root: Path,
    job_id: str,
    run_id: str,
    track: str,
    expected_model: str,
    expected_lite: bool,
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
    model_connections: list[tuple[int, Path]] = []
    connection_ids: set[int] = set()
    for connection in relay["connections"]:
        connection_id = connection.get("connection_id") if isinstance(connection, dict) else None
        if (
            not isinstance(connection_id, int)
            or isinstance(connection_id, bool)
            or connection_id <= 0
            or connection_id in connection_ids
        ):
            raise ModelConditionReceiptError("relay/relay.json connection_id 非法或重复。")
        connection_ids.add(connection_id)
        request_path = root / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
        if request_path.is_symlink() or not request_path.is_file():
            # 客户端会短暂建立后立即放弃一条 TLS 连接；中继仍会登记连接的打开／
            # 关闭时间，但没有任何方向的字节、摘要或 segment，因此不会生成 .bin。
            # 这种严格意义上的空连接不承载模型条件事实，应从分母中排除。此前把它
            # 当成“原始请求丢失”，导致同一份完整 Responses 样本随机失败。
            response_path = (
                root / "relay" / f"conn{connection_id:03d}.upstream_to_client.bin"
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
            raise ModelConditionReceiptError(f"缺少连接 {connection_id} 的原始请求字节。")
        for request in _iter_messages(request_path.read_bytes(), response=False):
            path = request["target"].split("?", 1)[0]
            if request["method"] == "GET" and path == "/backend-api/codex/models":
                model_connections.append((connection_id, request_path))
    if not model_connections:
        raise ModelConditionReceiptError("至少需要一条 /models 原始连接。")
    bindings: list[dict[str, Any]] = []
    models_response_paths: list[Path] = []
    actual_lites: list[bool] = []
    for connection_id, models_request_path in sorted(model_connections):
        response_path = root / "relay" / f"conn{connection_id:03d}.upstream_to_client.bin"
        if response_path.is_symlink() or not response_path.is_file():
            raise ModelConditionReceiptError(f"缺少连接 {connection_id} 的原始响应字节。")
        responses = list(_iter_messages(response_path.read_bytes(), response=True))
        if len(responses) != 1 or responses[0]["status"] != 200:
            raise ModelConditionReceiptError("每条 /models 连接必须恰好返回一个 HTTP 200。")
        models_payload = _json_body(responses[0])
        models = models_payload.get("models") if isinstance(models_payload, dict) else None
        matches = [
            item
            for item in models or []
            if isinstance(item, dict) and item.get("slug") == expected_model
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("use_responses_lite"), bool):
            raise ModelConditionReceiptError("/models 未唯一给出目标模型的 Lite 元数据。")
        actual_lites.append(matches[0]["use_responses_lite"])
        models_response_paths.append(response_path)
        bindings.extend((_bind(root, models_request_path), _bind(root, response_path)))
    if any(actual_lite is not expected_lite for actual_lite in actual_lites):
        raise ModelConditionReceiptError(
            f"目标模型 use_responses_lite={sorted(set(actual_lites))}，预期为 {expected_lite}。"
        )
    actual_lite = expected_lite
    request_models: list[str] = []
    for request_path in sorted((root / "relay").glob("conn*.client_to_upstream.bin")):
        raw = request_path.read_bytes()
        used = False
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
            request_models.append(model)
            used = True
        # WS 传输下 Responses 不是 HTTP POST：握手是 GET + Upgrade，真正的请求体在
        # WS 帧里，而官方协商了 permessage-deflate，帧 payload 是 raw deflate，
        # 明文搜不到 model。因此 HTTP 路径取不到时再走帧路径，两者取并集后一起
        # 参与 fallback 判定——判据强度不变，只是把 WS 形态也纳入证明范围。
        for model in _ws_request_models(raw):
            request_models.append(model)
            used = True
        if used:
            bindings.append(_bind(root, request_path))
    if not request_models:
        raise ModelConditionReceiptError(
            "未见可绑定的 Responses／compact 原始请求模型（HTTP 与 WS 帧均未取到）。"
        )
    fallback = any(model != expected_model for model in request_models)
    if fallback:
        raise ModelConditionReceiptError(
            f"实际请求模型发生 fallback：{sorted(set(request_models))}。"
        )
    bindings = sorted({item["path"]: item for item in bindings}.values(), key=lambda item: item["path"])
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
    """严格校验收据结构、期望坐标与所有 evidence binding。"""

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
    if not SHA256_RE.fullmatch(str(payload.get("models_response_sha256", ""))):
        raise ModelConditionReceiptError("models_response_sha256 非法。")
    if payload.get("observed_request_models") != [model_id]:
        raise ModelConditionReceiptError("observed_request_models 不匹配目标模型。")
    bindings = payload.get("evidence_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise ModelConditionReceiptError("模型条件收据 evidence_bindings 不能为空。")
    seen: set[str] = set()
    response_match = False
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise ModelConditionReceiptError("模型条件收据 binding 字段不闭合。")
        path_value = item["path"]
        if (
            not isinstance(path_value, str)
            or not path_value
            or path_value.startswith("/")
            or ".." in Path(path_value).parts
            or path_value in seen
        ):
            raise ModelConditionReceiptError("模型条件收据 binding 路径非法或重复。")
        seen.add(path_value)
        actual = _bind(root, root / path_value)
        if actual != item:
            raise ModelConditionReceiptError(f"模型条件证据摘要不一致：{path_value}")
        if path_value.endswith(".upstream_to_client.bin"):
            response_match = response_match or item["sha256"] == payload["models_response_sha256"]
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
    arguments = parser.parse_args()
    expected_lite = arguments.expect_use_responses_lite == "true"
    receipt = build_receipt(
        root=arguments.run_root,
        job_id=arguments.job_id,
        run_id=arguments.run_id,
        track=arguments.track,
        expected_model=arguments.model,
        expected_lite=expected_lite,
    )
    secure_write_json(arguments.output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
