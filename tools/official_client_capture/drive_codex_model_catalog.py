#!/usr/bin/env python3
"""通过官方 Codex debug models 预热并核验一次在线模型目录。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture.model_condition_receipts import (
    ModelConditionReceiptError,
    _iter_messages,
    _json_body,
)
from tools.official_client_capture.run_codex_compact_scenario import secure_write


SCHEMA_VERSION = "codex-model-catalog-prewarm/v1"
MODEL_REQUEST = re.compile(r"^conn(?P<connection_id>[0-9]{3})\.client_to_upstream\.bin$")


class ModelCatalogPrewarmError(RuntimeError):
    """模型目录命令或对应原始在线证据不完整。"""


def build_debug_models_command(codex_bin: str, codex_version: str) -> list[str]:
    """构造不带 bundled 回退的官方在线模型目录命令。"""

    if not re.fullmatch(r"\d+\.\d+\.\d+", codex_version):
        raise ModelCatalogPrewarmError("Codex 版本必须是三段数字。")
    values = (
        "check_for_update_on_startup=false",
        "analytics.enabled=false",
        "feedback.enabled=false",
        'otel.exporter="none"',
        "otel.log_user_prompt=false",
    )
    # debug models 必须保留 CLI 内置的 openai provider。自定义 provider 会改变
    # ModelsEndpointClient 的 Codex backend 判定，OnlineIfUncached 可能只返回内置
    # 目录而不发出 /models；版本由 0.149.1 二进制自身写入 client_version。
    command = [codex_bin, "debug", "models"]
    for value in values:
        command.extend(["-c", value])
    return command


def parse_debug_models_catalog(
    raw: str,
    *,
    expected_model: str,
    expected_lite: bool,
) -> list[dict[str, Any]]:
    """严格解析 debug models JSON，并核验目标模型的 Lite 条件。"""

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise ModelCatalogPrewarmError("codex debug models 未输出合法 JSON。") from error
    if not isinstance(payload, dict) or set(payload) != {"models"}:
        raise ModelCatalogPrewarmError("codex debug models 输出字段不闭合。")
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ModelCatalogPrewarmError("codex debug models 未输出模型目录。")
    matches = [
        item
        for item in models
        if isinstance(item, dict) and item.get("slug") == expected_model
    ]
    if (
        len(matches) != 1
        or matches[0].get("use_responses_lite") is not expected_lite
    ):
        raise ModelCatalogPrewarmError(
            "codex debug models 未给出目标模型及预期 Lite 条件。"
        )
    return [item for item in models if isinstance(item, dict)]


def wait_for_mitm_model_catalog(
    path: Path,
    *,
    expected_model: str,
    expected_lite: bool,
    deadline: float,
) -> dict[str, Any]:
    """保持 app-server 存活，直到 MITM 刷盘完整的目标模型目录响应。"""

    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            try:
                records = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError):
                records = []
            for record in records:
                request = record.get("request") or {}
                response = record.get("response") or {}
                payload = (response.get("body") or {}).get("json") or {}
                models = payload.get("models") or []
                matches = [
                    item
                    for item in models
                    if isinstance(item, dict)
                    and item.get("slug") == expected_model
                    and item.get("use_responses_lite") is expected_lite
                ]
                if (
                    request.get("method") == "GET"
                    and str(request.get("path", "")).split("?", 1)[0]
                    == "/backend-api/codex/models"
                    and response.get("status") == 200
                    and matches
                ):
                    return {
                        "source": "mitm_models_http",
                        "path": str(path),
                        "sha256": file_sha256(path),
                        "use_responses_lite": expected_lite,
                    }
        time.sleep(0.05)
    raise ModelCatalogPrewarmError("等待 MITM 模型目录 HTTP 200 超时。")


def find_captured_model_catalog(
    relay_dir: Path,
    *,
    expected_model: str,
    expected_lite: bool,
) -> dict[str, Any]:
    """从已刷盘的 relay 字节中定位一份完整且匹配的模型目录响应。"""

    if relay_dir.is_symlink() or not relay_dir.is_dir():
        raise ModelCatalogPrewarmError("relay 目录不存在或不是可信目录。")
    completed_responses = 0
    for request_path in sorted(relay_dir.glob("conn*.client_to_upstream.bin")):
        matched_name = MODEL_REQUEST.fullmatch(request_path.name)
        if matched_name is None or request_path.is_symlink():
            continue
        model_requests = [
            request
            for request in _iter_messages(request_path.read_bytes(), response=False)
            if request.get("method") == "GET"
            and str(request.get("target", "")).split("?", 1)[0]
            == "/backend-api/codex/models"
        ]
        if not model_requests:
            continue
        connection_id = int(matched_name.group("connection_id"))
        response_path = relay_dir / f"conn{connection_id:03d}.upstream_to_client.bin"
        if response_path.is_symlink() or not response_path.is_file():
            continue
        completed_responses += 1
        try:
            responses = list(_iter_messages(response_path.read_bytes(), response=True))
            if len(responses) != 1 or responses[0].get("status") != 200:
                continue
            payload = _json_body(responses[0])
        except (OSError, ModelConditionReceiptError):
            continue
        models = payload.get("models") if isinstance(payload, dict) else None
        matches = [
            item
            for item in models or []
            if isinstance(item, dict) and item.get("slug") == expected_model
        ]
        if (
            len(matches) == 1
            and matches[0].get("use_responses_lite") is expected_lite
        ):
            return {
                "connection_id": connection_id,
                "request_path": f"relay/{request_path.name}",
                "request_sha256": file_sha256(request_path),
                "response_path": f"relay/{response_path.name}",
                "response_sha256": file_sha256(response_path),
                "use_responses_lite": expected_lite,
            }
    if completed_responses:
        raise ModelCatalogPrewarmError(
            "完整 /models 响应未给出目标模型及预期 Lite 条件。"
        )
    raise ModelCatalogPrewarmError("尚未捕获完整的 /models HTTP 200 原始响应。")


def wait_for_relay_model_catalog(
    relay_dir: Path,
    *,
    expected_model: str,
    expected_lite: bool,
    deadline: float,
) -> dict[str, Any]:
    """保持 app-server 存活，直到字节中继刷盘完整的模型目录响应。"""

    last_error: ModelCatalogPrewarmError | None = None
    while time.monotonic() < deadline:
        try:
            return find_captured_model_catalog(
                relay_dir,
                expected_model=expected_model,
                expected_lite=expected_lite,
            )
        except ModelCatalogPrewarmError as error:
            last_error = error
        time.sleep(0.05)
    raise ModelCatalogPrewarmError(
        "等待 relay 模型目录 HTTP 200 超时。"
    ) from last_error


def run_prewarm(
    *,
    codex_bin: str,
    codex_version: str,
    model: str,
    expected_lite: bool,
    relay_dir: Path,
    output: Path,
    timeout: int,
    mitm_models_http: Path | None = None,
) -> dict[str, Any]:
    """执行阻塞式 debug models，并把 JSON 结果与原始字节交叉核验。"""

    if not re.fullmatch(r"\d+\.\d+\.\d+", codex_version):
        raise ModelCatalogPrewarmError("Codex 版本必须是三段数字。")
    if not model.strip() or timeout <= 0:
        raise ModelCatalogPrewarmError("模型或超时参数非法。")

    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("SUB2API_API_KEY", None)
    deadline = time.monotonic() + timeout
    try:
        completed = subprocess.run(
            build_debug_models_command(codex_bin, codex_version),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ModelCatalogPrewarmError("codex debug models 执行超时。") from error
    if completed.returncode != 0:
        raise ModelCatalogPrewarmError(
            f"codex debug models 以 {completed.returncode} 退出。"
        )
    models = parse_debug_models_catalog(
        completed.stdout,
        expected_model=model,
        expected_lite=expected_lite,
    )

    # debug models 在冷缓存上同步等待 OnlineIfUncached 完成；命令成功仍不能单独
    # 证明它没有回退到内置目录，必须再看到同一轮原始 HTTP 200 才允许成功。
    if mitm_models_http is not None:
        captured = wait_for_mitm_model_catalog(
            mitm_models_http,
            expected_model=model,
            expected_lite=expected_lite,
            deadline=deadline,
        )
    else:
        captured = wait_for_relay_model_catalog(
            relay_dir,
            expected_model=model,
            expected_lite=expected_lite,
            deadline=deadline,
        )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "codex_version": codex_version,
        "model_id": model,
        "use_responses_lite": expected_lite,
        # 保留 v1 收据字段名；一次完整 debug models JSON 输出记为一条驱动记录。
        "protocol_record_count": 1,
        "model_count": len(models),
        "capture": captured,
    }
    secure_write(output, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--codex-version", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--expect-use-responses-lite", choices=("true", "false"), required=True)
    parser.add_argument("--relay-dir", type=Path, required=True)
    parser.add_argument(
        "--mitm-models-http",
        type=Path,
        help="等待该 MITM JSONL 出现目标模型 HTTP 200 后再关闭 app-server。",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=120)
    arguments = parser.parse_args()
    summary = run_prewarm(
        codex_bin=arguments.codex_bin,
        codex_version=arguments.codex_version,
        model=arguments.model,
        expected_lite=arguments.expect_use_responses_lite == "true",
        relay_dir=arguments.relay_dir,
        output=arguments.output,
        timeout=arguments.timeout,
        mitm_models_http=arguments.mitm_models_http,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
