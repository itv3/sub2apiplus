#!/usr/bin/env python3
"""组装 Codex CLI 0.145.0 候选侧正式 42 条验收证据。

本工具只消费已经完成的正式抓包、部署源码快照、Go JSONL 与人工审核配置；
不会连接服务、修改数据库或采集状态。它完成以下失败关闭工作：

1. 校验 A01、四轮 core、aux 的 run-summary 与固定连接布局；
2. 把双向 relay、五类 pcap 和 Go JSONL 复制到独立证据根；
3. 为上行响应字节生成仅承担绑定作用的完整性 trace；
4. 生成基础 manifest，再调用源码快照内的 candidate_test_trace 生成抽象事实；
5. 用源码快照内的冻结画像现场回放全部 42 条规则；
6. 校验并归档 rule metadata、official map 与 candidate identity。

重要：before 状态必须在运行本工具前真实采集；本工具完成后先执行正式 ``assert``，
再恢复环境并采集 after，最后执行 ``finalize``。不得把本工具的预校验代替正式门禁。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


CODEX_VERSION = "0.145.0"
RAW_MANIFEST_SCHEMA = "codex-candidate-capture-manifest/v1"
OBSERVATION_SCHEMA = "codex-candidate-observation/v1"
RULE_METADATA_SCHEMA = "codex-candidate-bundle-rule-metadata/v1"
OFFICIAL_MAP_SCHEMA = "codex-candidate-official-evidence-map/v1"

CORE_SCENARIOS = ("A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15")
AUX_SCENARIOS = ("A09", "A11", "A12", "A13", "A14")
TRACE_SCENARIOS = (
    "A03",
    "A04",
    "A05",
    "A06",
    "A07",
    "A08",
    "A09",
    "A10",
    "A11",
    "A14",
    "A15",
)

EXPECTED_CONNECTIONS = {
    "A03": 5,
    "A04": 7,
    "A05": 6,
    "A06": 1,
    "A07": 7,
    "A08": 4,
    "A09": 9,
    "A10": 4,
    "A11": 2,
    "A12": 3,
    "A13": 1,
    "A14": 3,
    "A15": 3,
}

EXPECTED_CORE_ACTIONS = {
    "A03": {"models_manifest": 1, "responses_http_success": 4},
    "A04": {"models_manifest": 3, "responses_http_success": 4},
    "A05": {
        "models_manifest": 1,
        "responses_ws_handshake_success": 5,
        "responses_ws_response_create": 2,
    },
    "A06": {
        "responses_ws_handshake_success": 1,
        "responses_ws_response_create": 3,
    },
    "A07": {
        "responses_http_fallback_success": 1,
        "responses_ws_retryable_failure": 6,
    },
    "A08": {"models_manifest": 1, "responses_http_success": 3},
    "A10": {"responses_http_success": 4},
    "A15": {"models_manifest": 3},
}

EXPECTED_AUX_ACTIONS = {
    "A09": {
        "alpha_search": 2,
        "images_edit": 1,
        "images_generation": 1,
        "legacy_compact": 4,
        "models_manifest": 1,
    },
    "A11": {"realtime_first_hop": 1, "realtime_sideband": 1},
    "A12": {
        "wham_credit_details": 1,
        "wham_safe_consume": 1,
        "wham_usage": 1,
    },
    "A13": {"oauth_dummy_invalid_grant": 1},
    "A14": {"files_blob_put": 1, "files_create": 1, "files_uploaded": 1},
}

VARIANTS = {
    "A03": ("no_cookie", "prime", "http_default", "lite_turn_1", "lite_turn_2"),
    "A04": (
        "models_baseline",
        "baseline",
        "models_residency_us",
        "residency_us",
        "memgen",
        "models_after_restart",
        "parent_thread",
    ),
    "A05": ("models", "default", "retry_1", "retry_2", "retry_3", "turn_2"),
    "A06": ("optional_missing",),
    "A07": ("retry_1", "retry_2", "retry_3", "retry_4", "retry_5", "retry_6", "http_fallback"),
    "A08": ("models", "call_1", "call_2", "call_3"),
    "A09": (
        "models",
        "prime",
        "default",
        "beta",
        "turn_state",
        "search_1",
        "search_2",
        "image_generation",
        "image_edit",
    ),
    "A10": ("user_requested", "context_limit", "model_downshift", "comp_hash_changed"),
    "A11": ("first_hop", "sideband"),
    "A12": ("usage", "credit_details", "consume"),
    "A13": ("invalid_grant",),
    "A14": ("create", "blob_put", "uploaded"),
    "A15": ("models_exec_suffix", "models_tui_suffix", "models_initial_no_suffix"),
}

EXPECTED_REQUEST_PATHS = {
    "A03": (
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
    ),
    "A04": (
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
    ),
    "A05": (
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
    ),
    "A06": ("/backend-api/codex/responses",),
    "A07": ("/backend-api/codex/responses",) * 7,
    "A08": (
        "/backend-api/codex/models",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
        "/backend-api/codex/responses",
    ),
    "A09": (
        "/backend-api/codex/models",
        "/backend-api/codex/responses/compact",
        "/backend-api/codex/responses/compact",
        "/backend-api/codex/responses/compact",
        "/backend-api/codex/responses/compact",
        "/backend-api/codex/alpha/search",
        "/backend-api/codex/alpha/search",
        "/backend-api/codex/images/generations",
        "/backend-api/codex/images/edits",
    ),
    "A10": ("/backend-api/codex/responses",) * 4,
    "A11": ("/backend-api/codex/realtime/calls", "/v1/realtime"),
    "A12": (
        "/backend-api/wham/usage",
        "/backend-api/wham/rate-limit-reset-credits",
        "/backend-api/wham/rate-limit-reset-credits/consume",
    ),
    "A13": ("/oauth/token",),
    "A14": (
        "/backend-api/files",
        "/candidate-aux/file_candidate_aux_0145",
        "/backend-api/files/file_candidate_aux_0145/uploaded",
    ),
    "A15": ("/backend-api/codex/models",) * 3,
}

OFFICIAL_EVIDENCE_KINDS = {
    "application_log",
    "database_trace",
    "filesystem_snapshot",
    "http_trace",
    "mitm_jsonl",
    "official_analysis",
    "official_index",
    "official_report",
    "pcap",
    "pcapng",
    "process_trace",
    "relay_binary",
    "source_excerpt",
    "stderr_log",
    "stdout_log",
    "tls_keylog",
    "websocket_trace",
    "wire_dump",
}


class AssemblyError(ValueError):
    """输入抓包或正式映射不满足闭合约束。"""


def unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 键，避免后值静默覆盖证据身份。"""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AssemblyError(f"JSON 对象包含重复字段：{key}")
        value[key] = item
    return value


def load_json(path: Path, description: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise AssemblyError(f"{description}必须是非符号链接普通文件：{path}")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AssemblyError(f"{description}不是合法 UTF-8 JSON：{error}") from error


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any, *, jsonl: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return payload + (b"\n" if jsonl else b"")


def require_root(path: Path, description: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise AssemblyError(f"{description}必须是非符号链接目录：{path}")
    return path.resolve(strict=True)


def require_file(path: Path, description: str) -> Path:
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 0:
        raise AssemblyError(f"{description}必须是非空非符号链接普通文件：{path}")
    return path.resolve(strict=True)


def relative_path(value: str, description: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise AssemblyError(f"{description}必须是不能逃逸的 POSIX 相对路径：{value!r}")
    return path


def output_path(root: Path, relative: str) -> Path:
    parts = relative_path(relative, "输出路径").parts
    current = root
    for part in parts[:-1]:
        current = current / part
        if current.exists() and (current.is_symlink() or not current.is_dir()):
            raise AssemblyError(f"输出父路径非法：{relative}")
    target = root.joinpath(*parts)
    if target.exists() or target.is_symlink():
        raise AssemblyError(f"拒绝覆盖既有正式证据：{relative}")
    return target


def write_exclusive(root: Path, relative: str, payload: bytes) -> Path:
    target = output_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.open("xb") as stream:
        stream.write(payload)
    os.chmod(target, 0o600)
    return target


def copy_exclusive(root: Path, relative: str, source: Path) -> Path:
    source = require_file(source, f"{relative} 输入")
    target = output_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as input_stream, target.open("xb") as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
    os.chmod(target, 0o600)
    if sha256_file(target) != sha256_file(source):
        raise AssemblyError(f"复制后摘要不一致：{relative}")
    return target


def artifact(
    path: str,
    absolute: Path,
    *,
    kind: str,
    parser: str,
    scenario: str | Sequence[str],
    labels: Mapping[str, Any],
    frame_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    scenarios = [scenario] if isinstance(scenario, str) else list(scenario)
    value: dict[str, Any] = {
        "path": path,
        "sha256": sha256_file(absolute),
        "kind": kind,
        "parser": parser,
        "scenario_ids": scenarios,
        "labels": dict(labels),
    }
    if frame_labels:
        value["frame_labels"] = {
            str(index): dict(items) for index, items in frame_labels.items()
        }
    return value


def validate_run_summary(
    run_root: Path,
    *,
    schema: str,
    scenarios: Sequence[str],
    expected_actions: Mapping[str, Mapping[str, int]],
    synthetic_profile: str,
    restoration_keys: Sequence[str],
) -> dict[str, Any]:
    value = load_json(run_root / "run-summary.json", f"{run_root.name} run-summary")
    if not isinstance(value, dict):
        raise AssemblyError(f"{run_root.name} run-summary 顶层必须是对象")
    if value.get("schema_version") != schema or value.get("status") != "complete":
        raise AssemblyError(f"{run_root.name} 未完成或 schema 不匹配")
    if value.get("run_id") != run_root.name:
        raise AssemblyError(f"{run_root.name} run_id 与目录名不一致")
    if value.get("synthetic_profile") != synthetic_profile:
        raise AssemblyError(f"{run_root.name} synthetic_profile 不匹配")
    if (
        value.get("exit_code") != 0
        or value.get("explicit_gate") is not True
        or value.get("production_forwarding_enabled") is not False
    ):
        raise AssemblyError(f"{run_root.name} 执行门禁或退出状态不完整")
    restoration = value.get("restoration")
    if not isinstance(restoration, dict) or any(
        restoration.get(key) is not True for key in restoration_keys
    ):
        raise AssemblyError(f"{run_root.name} 内层恢复证明不完整：{restoration!r}")
    raw_scenarios = value.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise AssemblyError(f"{run_root.name} scenarios 缺失")
    by_id = {
        item.get("scenario_id"): item
        for item in raw_scenarios
        if isinstance(item, dict) and isinstance(item.get("scenario_id"), str)
    }
    if set(by_id) != set(scenarios):
        raise AssemblyError(f"{run_root.name} 场景集合不闭合：{sorted(by_id)}")
    for scenario in scenarios:
        item = by_id[scenario]
        if item.get("actions") != dict(expected_actions[scenario]):
            raise AssemblyError(
                f"{run_root.name}/{scenario} 动作计数不匹配：{item.get('actions')!r}"
            )
        if item.get("production_forwarded") is not False:
            raise AssemblyError(f"{run_root.name}/{scenario} 发生了生产转发")
        pcap = require_file(
            run_root / "scenarios" / scenario / "egress.pcap",
            f"{run_root.name}/{scenario} pcap",
        )
        if item.get("pcap_sha256") != sha256_file(pcap):
            raise AssemblyError(f"{run_root.name}/{scenario} pcap 摘要不匹配")
    return value


def validate_a01_run(run_root: Path) -> Path:
    value = load_json(run_root / "run-summary.json", "A01 run-summary")
    if not isinstance(value, dict):
        raise AssemblyError("A01 run-summary 顶层必须是对象")
    if (
        value.get("schema_version") != "sub2api-direct-capture/v1"
        or value.get("status") != "complete"
        or value.get("run_id") != run_root.name
    ):
        raise AssemblyError("A01 direct run 未完成或身份不匹配")
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) != 1:
        raise AssemblyError("A01 必须只有 codex-http/s1 一个 direct case")
    case = cases[0]
    if not isinstance(case, dict) or {
        "subject": case.get("subject"),
        "scenario": case.get("scenario"),
        "valid": case.get("valid"),
    } != {"subject": "codex-http", "scenario": "s1", "valid": True}:
        raise AssemblyError("A01 direct case 不是有效 codex-http/s1")
    pcap = require_file(
        run_root / "direct" / "codex-http-s1" / "egress.pcap",
        "A01 direct pcap",
    )
    if case.get("pcap_sha256") != sha256_file(pcap):
        raise AssemblyError("A01 direct pcap 摘要不匹配")
    return pcap


def validate_aux_cleanup(aux_run: Path) -> None:
    """核对 A11 终态关闭及三类并发租约均已释放。"""

    path = require_file(
        aux_run / "scenarios" / "A11" / "trigger" / "live-cleanup.txt",
        "A11 Live 清理证明",
    )
    actual = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    expected = {
        "controller_closed=true",
        "account_lease_released=true",
        "user_lease_released=true",
        "api_key_lease_released=true",
    }
    if actual != expected:
        raise AssemblyError(f"A11 Live 清理证明不闭合：{sorted(actual)}")


def copy_aux_cleanup_evidence(
    aux_run: Path,
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
) -> None:
    """归档并结构化绑定 A11 的真实 Live 清理输出。"""

    source = aux_run / "scenarios" / "A11" / "trigger" / "live-cleanup.txt"
    raw_relative = "candidate/A11/live-cleanup.txt"
    raw_target = copy_exclusive(evidence_root, raw_relative, source)
    artifacts.append(
        artifact(
            raw_relative,
            raw_target,
            kind="stdout_log",
            parser="opaque_bound_source",
            scenario="A11",
            labels={"source": "candidate-aux-live-cleanup"},
        )
    )
    record = {
        "schema_version": OBSERVATION_SCHEMA,
        "record_id": "candidate-aux:a11:live-cleanup",
        "scenario_id": "A11",
        "record_type": "live_cleanup",
        "data": {
            "account_lease_released": True,
            "api_key_lease_released": True,
            "controller_closed": True,
            "user_lease_released": True,
        },
        "source_artifacts": [raw_relative],
    }
    trace_relative = "candidate/A11/live-cleanup.jsonl"
    trace_target = write_exclusive(
        evidence_root,
        trace_relative,
        canonical_json_bytes(record, jsonl=True),
    )
    artifacts.append(
        artifact(
            trace_relative,
            trace_target,
            kind="process_trace",
            parser="observation_jsonl",
            scenario="A11",
            labels={"generator": ".codex-formal-bundle-builder.py"},
        )
    )


def header_values(request: Mapping[str, Any], name: str) -> list[Any]:
    result: list[Any] = []
    for item in request.get("headers", []):
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            result.append(item.get("value"))
    return result


def request_path(request: Mapping[str, Any]) -> str:
    line = request.get("request_line")
    if not isinstance(line, str) or len(line.split(" ", 2)) != 3:
        raise AssemblyError(f"请求行无法解析：{line!r}")
    target = line.split(" ", 2)[1]
    return target.split("?", 1)[0]


def labels_for(scenario: str, ordinal: int, request: Mapping[str, Any]) -> dict[str, str]:
    connection_id = f"conn{ordinal:03d}"
    labels = {
        "connection_id": connection_id,
        "provider": "openai_oauth",
        "variant": VARIANTS[scenario][ordinal - 1],
    }
    names = [str(item) for item in request.get("header_names_in_order", [])]
    lower_names = [item.lower() for item in names]
    path = request_path(request)
    if len(lower_names) >= 2 and lower_names[-2:] == ["host", "content-length"]:
        labels["body_length"] = "automatic"
    if header_values(request, "content-encoding") == ["zstd"]:
        labels["compression"] = "zstd"
    if path in {"/backend-api/codex/responses", "/backend-api/codex/responses/compact"}:
        labels["session_header_scope"] = "responses_or_compact"
    if scenario == "A03":
        if ordinal in {2, 3}:
            labels["mode"] = "non_lite"
        elif ordinal in {4, 5}:
            labels["mode"] = "lite"
    elif scenario in {"A04", "A07", "A08", "A10"} and path == "/backend-api/codex/responses":
        labels["mode"] = "non_lite"
    if scenario == "A04":
        labels["residency"] = (
            "us" if header_values(request, "x-openai-internal-codex-residency") == ["us"] else "unset"
        )
    if scenario == "A09":
        labels["endpoint_class"] = "images" if "/images/" in path else "auxiliary"
    elif path == "/backend-api/codex/models":
        labels["endpoint_class"] = "auxiliary"
    if scenario == "A10":
        labels["compaction_mode"] = "manual" if ordinal == 1 else "auto"
    return labels


def validate_fixture_shape(scenario: str, ordinal: int, request: Mapping[str, Any]) -> None:
    actual_path = request_path(request)
    expected_path = EXPECTED_REQUEST_PATHS[scenario][ordinal - 1]
    if actual_path != expected_path:
        raise AssemblyError(
            f"{scenario}/conn{ordinal:03d} path 不匹配：{actual_path} != {expected_path}"
        )
    body = request.get("body")
    fields = body.get("top_level_fields_in_order") if isinstance(body, dict) else None
    if scenario == "A03" and ordinal in {4, 5}:
        if not isinstance(fields, list) or "instructions" in fields or "tools" in fields:
            raise AssemblyError(
                f"{scenario}/conn{ordinal:03d} Lite 顶层仍含 instructions/tools：{fields!r}"
            )
    if scenario == "A09" and ordinal == 3:
        expected = [
            "model",
            "input",
            "parallel_tool_calls",
            "reasoning",
            "prompt_cache_key",
            "text",
        ]
        if fields != expected:
            raise AssemblyError(f"A09 default compact body 槽位不匹配：{fields!r}")
    if scenario == "A09" and ordinal == 8:
        expected = ["prompt", "background", "model", "quality", "size"]
        if fields != expected:
            raise AssemblyError(f"A09 image generation body 槽位不匹配：{fields!r}")
    if scenario == "A09" and ordinal == 9:
        expected = ["images", "prompt", "background", "model", "quality", "size"]
        if fields != expected:
            raise AssemblyError(f"A09 image edit body 槽位不匹配：{fields!r}")


def relay_files(run_root: Path, scenario: str) -> list[tuple[Path, Path]]:
    relay_root = run_root / "scenarios" / scenario / "relay"
    clients = sorted(relay_root.glob("conn*.client_to_upstream.bin"))
    expected = EXPECTED_CONNECTIONS[scenario]
    if len(clients) != expected:
        raise AssemblyError(f"{run_root.name}/{scenario} 连接数 {len(clients)} != {expected}")
    result: list[tuple[Path, Path]] = []
    for ordinal, client in enumerate(clients, 1):
        expected_name = f"conn{ordinal:03d}.client_to_upstream.bin"
        if client.name != expected_name:
            raise AssemblyError(f"{run_root.name}/{scenario} 连接编号不连续：{client.name}")
        upstream = require_file(
            relay_root / f"conn{ordinal:03d}.upstream_to_client.bin",
            f"{run_root.name}/{scenario}/conn{ordinal:03d} 下行 relay",
        )
        result.append((require_file(client, "上行 relay"), upstream))
    return result


def parse_single_request(
    path: Path,
    description: str,
    parse_h1_stream: Callable[[bytes], list[dict[str, Any]]],
) -> dict[str, Any]:
    try:
        requests = parse_h1_stream(path.read_bytes())
    except SystemExit as error:
        raise AssemblyError(f"{description} H1 解析失败：{error}") from error
    if len(requests) != 1:
        raise AssemblyError(f"{description} 必须恰好含一个请求")
    return requests[0]


def add_integrity_trace(
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
    scenario: str,
    upstream_paths: Sequence[str],
) -> None:
    if not upstream_paths:
        return
    record = {
        "schema_version": OBSERVATION_SCHEMA,
        "record_id": f"capture-integrity:{scenario.lower()}:upstream",
        "scenario_id": scenario,
        "record_type": "capture_integrity",
        "data": {
            "artifact_count": len(upstream_paths),
            "direction": "upstream_to_client",
            "sha256s": [
                next(item["sha256"] for item in artifacts if item["path"] == path)
                for path in upstream_paths
            ],
        },
        "source_artifacts": list(upstream_paths),
    }
    relative = f"candidate/{scenario}/capture-integrity.jsonl"
    target = write_exclusive(evidence_root, relative, canonical_json_bytes(record, jsonl=True))
    artifacts.append(
        artifact(
            relative,
            target,
            kind="application_log",
            parser="observation_jsonl",
            scenario=scenario,
            labels={"generator": ".codex-formal-bundle-builder.py"},
        )
    )


def copy_relay_scenario(
    *,
    run_root: Path,
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
    scenario: str,
    parse_h1_stream: Callable[[bytes], list[dict[str, Any]]],
) -> None:
    upstream_paths: list[str] = []
    for ordinal, (client, upstream) in enumerate(relay_files(run_root, scenario), 1):
        request = parse_single_request(
            client,
            f"{run_root.name}/{scenario}/conn{ordinal:03d}",
            parse_h1_stream,
        )
        validate_fixture_shape(scenario, ordinal, request)
        base = f"candidate/{scenario}/relay/conn{ordinal:03d}"
        client_relative = f"{base}.client_to_upstream.bin"
        upstream_relative = f"{base}.upstream_to_client.bin"
        client_target = copy_exclusive(evidence_root, client_relative, client)
        upstream_target = copy_exclusive(evidence_root, upstream_relative, upstream)
        frame_labels = {"0": {"frame_role": "warmup"}} if scenario == "A06" else None
        artifacts.append(
            artifact(
                client_relative,
                client_target,
                kind="relay_binary",
                parser="h1_request_stream",
                scenario=scenario,
                labels=labels_for(scenario, ordinal, request),
                frame_labels=frame_labels,
            )
        )
        artifacts.append(
            artifact(
                upstream_relative,
                upstream_target,
                kind="relay_binary",
                parser="opaque_bound_source",
                scenario=scenario,
                labels={
                    "connection_id": f"conn{ordinal:03d}",
                    "direction": "upstream_to_client",
                },
            )
        )
        upstream_paths.append(upstream_relative)
    add_integrity_trace(evidence_root, artifacts, scenario, upstream_paths)


def copy_a01_relay_aliases(
    *,
    core_run: Path,
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
    parse_h1_stream: Callable[[bytes], list[dict[str, Any]]],
) -> None:
    # 可解析 artifact 多场景会产生重复 record_id；因此必须复制为 A01 独立路径。
    aliases = ((1, "models", "no_cookie"), (3, "responses", "http_default"))
    upstream_paths: list[str] = []
    relay_root = core_run / "scenarios" / "A03" / "relay"
    for ordinal, alias, variant in aliases:
        source_client = require_file(
            relay_root / f"conn{ordinal:03d}.client_to_upstream.bin",
            f"A01 {alias} 上行 relay",
        )
        source_upstream = require_file(
            relay_root / f"conn{ordinal:03d}.upstream_to_client.bin",
            f"A01 {alias} 下行 relay",
        )
        request = parse_single_request(
            source_client,
            f"A01 {alias} relay",
            parse_h1_stream,
        )
        client_relative = f"candidate/A01/relay/{alias}.client_to_upstream.bin"
        upstream_relative = f"candidate/A01/relay/{alias}.upstream_to_client.bin"
        client_target = copy_exclusive(evidence_root, client_relative, source_client)
        upstream_target = copy_exclusive(evidence_root, upstream_relative, source_upstream)
        labels = labels_for("A03", ordinal, request)
        labels["connection_id"] = f"a01-{alias}"
        labels["variant"] = variant
        artifacts.append(
            artifact(
                client_relative,
                client_target,
                kind="relay_binary",
                parser="h1_request_stream",
                scenario="A01",
                labels=labels,
            )
        )
        artifacts.append(
            artifact(
                upstream_relative,
                upstream_target,
                kind="relay_binary",
                parser="opaque_bound_source",
                scenario="A01",
                labels={"connection_id": f"a01-{alias}", "direction": "upstream_to_client"},
            )
        )
        upstream_paths.append(upstream_relative)
    add_integrity_trace(evidence_root, artifacts, "A01", upstream_paths)


def copy_pcap(
    *,
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
    source: Path,
    relative: str,
    scenario: str,
    labels: Mapping[str, Any],
    pcap_observations: Callable[..., list[Any]],
) -> None:
    target = copy_exclusive(evidence_root, relative, source)
    observations = pcap_observations(target, relative, [scenario], labels)
    if not observations:
        raise AssemblyError(f"pcap 未解析出 ClientHello：{relative}")
    artifacts.append(
        artifact(
            relative,
            target,
            kind="pcap",
            parser="pcap_client_hello",
            scenario=scenario,
            labels=labels,
        )
    )


def copy_go_test_log(
    evidence_root: Path,
    artifacts: list[dict[str, Any]],
    source: Path,
) -> str:
    relative = "candidate/go-test/candidate-go-test.jsonl"
    target = copy_exclusive(evidence_root, relative, source)
    artifacts.append(
        artifact(
            relative,
            target,
            kind="stdout_log",
            parser="opaque_bound_source",
            scenario=TRACE_SCENARIOS,
        labels={"command": "go test -json -count=1", "source": "deployed-final-snapshot"},
        )
    )
    return relative


def validate_rule_metadata(
    source_root: Path,
    metadata_path: Path,
    rule_ids: Sequence[str],
    load_rule_metadata: Callable[..., Any],
) -> dict[str, Any]:
    value = load_json(metadata_path, "rule metadata")
    if not isinstance(value, dict) or value.get("schema_version") != RULE_METADATA_SCHEMA:
        raise AssemblyError("rule metadata schema 不匹配")
    load_rule_metadata(metadata_path, source_root, rule_ids)
    return value


def validate_official_map(
    map_path: Path,
    official_root: Path,
    rule_ids: Sequence[str],
) -> dict[str, Any]:
    value = load_json(map_path, "official evidence map")
    if not isinstance(value, dict) or value.get("schema_version") != OFFICIAL_MAP_SCHEMA:
        raise AssemblyError("official evidence map schema 不匹配")
    if value.get("codex_version") != CODEX_VERSION:
        raise AssemblyError("official evidence map Codex 版本不匹配")
    rules = value.get("rules")
    if not isinstance(rules, list):
        raise AssemblyError("official evidence map rules 缺失")
    by_id = {
        item.get("rule_id"): item
        for item in rules
        if isinstance(item, dict) and isinstance(item.get("rule_id"), str)
    }
    if set(by_id) != set(rule_ids) or len(rules) != len(rule_ids):
        raise AssemblyError("official evidence map 未精确覆盖 42 条规则")
    official_root = require_root(official_root, "官方证据根")
    for rule_id in rule_ids:
        items = by_id[rule_id].get("artifacts")
        if not isinstance(items, list) or not items:
            raise AssemblyError(f"{rule_id} 缺少官方证据")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"path", "sha256", "kind"}:
                raise AssemblyError(f"{rule_id} 官方证据条目字段不闭合")
            relative = relative_path(str(item.get("path", "")), f"{rule_id} 官方证据")
            path = require_file(official_root.joinpath(*relative.parts), f"{rule_id} 官方证据")
            if item.get("kind") not in OFFICIAL_EVIDENCE_KINDS:
                raise AssemblyError(f"{rule_id} 官方证据 kind 非法")
            if item.get("sha256") != sha256_file(path):
                raise AssemblyError(f"{rule_id} 官方证据摘要不匹配：{relative}")
    return value


def validate_candidate_identity(
    source_root: Path,
    identity_path: Path,
    load_candidate_identity: Callable[[Path], dict[str, str]],
) -> dict[str, str]:
    value = load_candidate_identity(identity_path)
    bindings = (("GIT_COMMIT", "git_commit"), ("SOURCE_TREE_SHA256", "source_tree_sha256"))
    for filename, field in bindings:
        path = source_root / filename
        if path.is_file() and path.read_text(encoding="utf-8").strip() != value[field]:
            raise AssemblyError(f"candidate identity {field} 与源码快照 {filename} 不一致")
    return value


def archive_config(
    evidence_root: Path,
    name: str,
    value: Any,
) -> str:
    relative = f"config/{name}"
    write_exclusive(evidence_root, relative, canonical_json_bytes(value) + b"\n")
    return relative


def import_snapshot_tools(source_root: Path) -> dict[str, Any]:
    sys.path.insert(0, str(source_root))
    try:
        from tools.official_client_capture.candidate_acceptance_bundle import (
            _load_candidate_identity,
            _load_rule_metadata,
        )
        from tools.official_client_capture.candidate_rule_assertion import (
            _pcap_observations,
            evaluate_rule,
            load_observations,
            load_profile,
        )
        from tools.official_client_capture.candidate_test_trace import (
            DEFAULT_MAPPING_RELATIVE_PATH,
            DEFAULT_PROFILE_RELATIVE_PATH,
            generate_test_traces,
        )
        from tools.official_client_capture.relay_extract import parse_h1_stream
    except ImportError as error:
        raise AssemblyError(f"无法导入源码快照内候选验收工具：{error}") from error
    return {
        "load_candidate_identity": _load_candidate_identity,
        "load_rule_metadata": _load_rule_metadata,
        "pcap_observations": _pcap_observations,
        "evaluate_rule": evaluate_rule,
        "load_observations": load_observations,
        "load_profile": load_profile,
        "generate_test_traces": generate_test_traces,
        "mapping_relative": DEFAULT_MAPPING_RELATIVE_PATH,
        "profile_relative": DEFAULT_PROFILE_RELATIVE_PATH,
        "parse_h1_stream": parse_h1_stream,
    }


def assemble(args: argparse.Namespace) -> dict[str, Any]:
    if not args.capture_id or any(
        not (character.isascii() and (character.isalnum() or character in "._-"))
        for character in args.capture_id
    ):
        raise AssemblyError("capture-id 只能包含字母、数字、点、下划线和连字符")
    source_root = require_root(args.source_root, "最终部署源码快照")
    evidence_root = require_root(args.evidence_root, "正式证据根")
    a01_run = require_root(args.a01_run, "A01 run")
    core_runs = [require_root(path, "core run") for path in args.core_run]
    aux_run = require_root(args.aux_run, "aux run")
    if len(core_runs) != 4 or len(set(core_runs)) != 4:
        raise AssemblyError("A02 必须由四个不同 core run 提供四份独立 pcap")

    tools = import_snapshot_tools(source_root)
    profile_path = source_root / tools["profile_relative"]
    rule_manifest_path = source_root / "tools/official_client_capture/codex_upgrade_rules_0_145_0.json"
    profile = tools["load_profile"](profile_path, rule_manifest_path)
    rule_ids = [item["rule_id"] for item in profile["rules"]]
    if len(rule_ids) != 42 or len(set(rule_ids)) != 42:
        raise AssemblyError("冻结画像未精确包含 42 条唯一规则")

    a01_pcap = validate_a01_run(a01_run)
    for core_run in core_runs:
        validate_run_summary(
            core_run,
            schema="candidate-core-capture/v1",
            scenarios=CORE_SCENARIOS,
            expected_actions=EXPECTED_CORE_ACTIONS,
            synthetic_profile="candidate-core-v1",
            restoration_keys=(
                "account_proxy_equal",
                "account_extra_equal",
                "hosts_sha256_equal",
                "ca_bundle_sha256_equal",
            ),
        )
    aux_summary = validate_run_summary(
        aux_run,
        schema="candidate-aux-capture/v1",
        scenarios=AUX_SCENARIOS,
        expected_actions=EXPECTED_AUX_ACTIONS,
        synthetic_profile="candidate-aux-v1",
        restoration_keys=(
            "account_model_mapping_equal",
            "hosts_sha256_equal",
            "ca_bundle_sha256_equal",
        ),
    )
    # aux 内层的固定 schema 记录原始 proxy/fallback 值，而不像 core 另写
    # `account_proxy_equal`；外层排列等价复核与最终 before/after 再证明恢复。
    if aux_summary["restoration"].get("account_proxy_original") != "NULL|NULL":
        raise AssemblyError("aux 账号 proxy/fallback 原始值不是 NULL|NULL")
    validate_aux_cleanup(aux_run)

    artifacts: list[dict[str, Any]] = []
    copy_pcap(
        evidence_root=evidence_root,
        artifacts=artifacts,
        source=a01_pcap,
        relative="candidate/A01/egress.pcap",
        scenario="A01",
        labels={"transport": "http", "ca_mode": "system"},
        pcap_observations=tools["pcap_observations"],
    )
    copy_a01_relay_aliases(
        core_run=core_runs[0],
        evidence_root=evidence_root,
        artifacts=artifacts,
        parse_h1_stream=tools["parse_h1_stream"],
    )
    for index, core_run in enumerate(core_runs, 1):
        copy_pcap(
            evidence_root=evidence_root,
            artifacts=artifacts,
            source=core_run / "scenarios" / "A06" / "egress.pcap",
            relative=f"candidate/A02/core-{index:02d}-a06-egress.pcap",
            scenario="A02",
            labels={
                "transport": "websocket",
                "ca_mode": "system",
                "restart_ordinal": str(index),
            },
            pcap_observations=tools["pcap_observations"],
        )
    for scenario in CORE_SCENARIOS:
        copy_relay_scenario(
            run_root=core_runs[0],
            evidence_root=evidence_root,
            artifacts=artifacts,
            scenario=scenario,
            parse_h1_stream=tools["parse_h1_stream"],
        )
    for scenario in AUX_SCENARIOS:
        copy_relay_scenario(
            run_root=aux_run,
            evidence_root=evidence_root,
            artifacts=artifacts,
            scenario=scenario,
            parse_h1_stream=tools["parse_h1_stream"],
        )
    for scenario in ("A11", "A13", "A14"):
        copy_pcap(
            evidence_root=evidence_root,
            artifacts=artifacts,
            source=aux_run / "scenarios" / scenario / "egress.pcap",
            relative=f"candidate/{scenario}/egress.pcap",
            scenario=scenario,
            labels={"capture": "candidate-aux-v1"},
            pcap_observations=tools["pcap_observations"],
        )
    copy_aux_cleanup_evidence(aux_run, evidence_root, artifacts)
    go_artifact = copy_go_test_log(evidence_root, artifacts, args.go_test_jsonl)

    raw_manifest = {
        "schema_version": RAW_MANIFEST_SCHEMA,
        "codex_version": CODEX_VERSION,
        "capture_id": f"{args.capture_id}:raw",
        "status": "complete",
        "artifacts": artifacts,
    }
    raw_manifest_path = write_exclusive(
        evidence_root,
        "capture-manifest.raw.json",
        canonical_json_bytes(raw_manifest) + b"\n",
    )
    receipt = tools["generate_test_traces"](
        source_root=source_root,
        evidence_root=evidence_root,
        capture_manifest_path=raw_manifest_path,
        go_test_artifact=go_artifact,
        mapping_path=source_root / tools["mapping_relative"],
        profile_path=profile_path,
        trace_dir="candidate/test-traces",
        output_manifest="capture-manifest.json",
        output_receipt="candidate/test-trace-receipt.json",
    )
    final_manifest_path = evidence_root / "capture-manifest.json"
    parsed_manifest, observations = tools["load_observations"](
        final_manifest_path,
        evidence_root,
    )
    failed: list[str] = []
    for rule_id in rule_ids:
        checks = tools["evaluate_rule"](profile, rule_id, observations, parsed_manifest)
        if not checks or any(item.get("passed") is not True for item in checks):
            failed.append(rule_id)
    if failed:
        raise AssemblyError(f"42 条预回放仍有失败：{failed}")

    metadata_value = validate_rule_metadata(
        source_root,
        args.rule_metadata,
        rule_ids,
        tools["load_rule_metadata"],
    )
    official_value = validate_official_map(
        args.official_evidence_map,
        args.official_evidence_root,
        rule_ids,
    )
    identity_value = validate_candidate_identity(
        source_root,
        args.candidate_identity,
        tools["load_candidate_identity"],
    )
    metadata_relative = archive_config(evidence_root, "rule-metadata.json", metadata_value)
    official_relative = archive_config(evidence_root, "official-evidence-map.json", official_value)
    identity_relative = archive_config(evidence_root, "candidate-identity.json", identity_value)

    result = {
        "schema_version": "codex-formal-bundle-assembly/v1",
        "codex_version": CODEX_VERSION,
        "capture_id": args.capture_id,
        "status": "pass",
        "builder": {
            "name": Path(__file__).name,
            "sha256": sha256_file(Path(__file__).resolve(strict=True)),
        },
        "source_snapshot": {
            "git_commit": identity_value["git_commit"],
            "source_tree_sha256": identity_value["source_tree_sha256"],
        },
        "capture_runs": {
            "a01": a01_run.name,
            "core": [path.name for path in core_runs],
            "aux": aux_run.name,
        },
        "raw_artifact_count": len(artifacts),
        "generated_trace_count": len(receipt["generated"]["trace_artifacts"]),
        "prevalidated_rule_count": 42,
        "paths": {
            "capture_manifest": "capture-manifest.json",
            "raw_capture_manifest": "capture-manifest.raw.json",
            "test_trace_receipt": "candidate/test-trace-receipt.json",
            "rule_metadata": metadata_relative,
            "official_evidence_map": official_relative,
            "candidate_identity": identity_relative,
        },
        "bindings": {
            "a01_relay_copied_to_single_scenario_paths": True,
            "a02_independent_pcap_count": 4,
            "a06_warmup_frame_index": 0,
            "a09_connection_count": EXPECTED_CONNECTIONS["A09"],
            "go_test_opaque_bound_by_generated_traces": True,
            "upstream_opaque_bound_by_integrity_and_test_traces": True,
        },
    }
    write_exclusive(
        evidence_root,
        "assembly-receipt.json",
        canonical_json_bytes(result) + b"\n",
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--a01-run", required=True, type=Path)
    parser.add_argument(
        "--core-run",
        required=True,
        action="append",
        type=Path,
        help="按 core-01..04 顺序重复四次；core-01 提供 A03..A15 relay",
    )
    parser.add_argument("--aux-run", required=True, type=Path)
    parser.add_argument("--go-test-jsonl", required=True, type=Path)
    parser.add_argument("--rule-metadata", required=True, type=Path)
    parser.add_argument("--official-evidence-map", required=True, type=Path)
    parser.add_argument("--official-evidence-root", required=True, type=Path)
    parser.add_argument("--candidate-identity", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = assemble(args)
    except (AssemblyError, OSError, ValueError) as error:
        print(f"formal-bundle-builder: fail: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
