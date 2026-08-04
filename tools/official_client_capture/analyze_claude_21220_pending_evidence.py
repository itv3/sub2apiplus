#!/usr/bin/env python3
"""严格复核 Claude Code 2.1.220 待补证采集矩阵。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "claude-code-2.1.220-pending-evidence-analysis/v1"
POST_RUN_SECRET_RECEIPT_SCHEMA = "official-client-post-run-secret-scan-receipt/v1"
POST_RUN_SECRET_TOOL_FILENAME = "post_run_secret_scan_receipt.py"
BASE_USER_AGENT = "claude-cli/2.1.220 (external, sdk-cli)"
EXPECTED_CLAUDE_BINARY_SHA256 = "674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863"
EXPECTED_HOST_RECEIPT_PRODUCER = {
    "name": "runtime_host_receipt.py",
    "path": "/root/oauth-capture/tools/official_client_capture/runtime_host_receipt.py",
    "sha256": "7723376d7e8ef1f832ad1e3032b53ecb06ed8526946412f42135f18b10014a8a",
    "version": "1",
}
CLIENT_APP = "probe-app-21220"
CONTAINER_ID = "probe-container-21220"
REMOTE_SESSION_ID = "probe-remote-session-21220"
MAX_SPAWN_DEPTH = "3"
MAX_RETRY_OVERHEAD_MS = 250.0
EXPECTED_SECRET_LABELS = [
    "claude_oauth_runtime_access_token_value",
    "operator_scan_env:CLAUDE_CAPTURE_REFRESH_TOKEN",
]

EXPECTED_ARGV_SHA256 = {
    "s1": "7662eb50de4026bfb79dd62399e453e89e899da4e98c9cc2a00aa0289136cd1a",
    "a1": "23527058fc2183be67dddf491b7518fce73031ee39c756ef6008b1f83bb744c8",
    "a2": "4594308bdb0dd609d02839a56e8f292fd0d5e928977909c0e6afab0976e3c7f5",
    "a3": "e07dabf5e600013c0c90735216e56ea89fda4125d1eb229785cb0030f9a1e757",
}
EXPECTED_AGENT_INPUT_SHA256 = {
    "a1": ("a60c959a038d02571abfee44ace3035ef7e2aeec8550f05f57ec7e50fef09902",),
    "a2": (
        "6e8a317ff7e3cd9b2eb2397ed7626e4083afb43cdb1328918ee9a5ab9a605f64",
        "e1ba3ebb0ed7b24a34c9cdc6dbec363ca21053586be374d8c7b1952e40a4845e",
    ),
    "a3": (
        "8d8f8f6e1fc05951c15e6696a170e111e4ef8451042cd5d27f33e930bcf01b26",
        "fe6082dfd5b22fd4316be75ef27cb1a6a0a5fb77ac2073316fa88034426ffcd1",
        "e74c2316ce694d296db801ad23949188a1f36f231abc6b24508e2e03bba01ec2",
    ),
}
M_REQUIREMENT_KEYS = {
    "campaign_status",
    "capture_execution_sources",
    "case_invocation_and_environment",
    "cleanup",
    "exact_secret_scan",
    "runtime_identity",
}
BASE_PROCESS_ENVIRONMENT = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_FEEDBACK_COMMAND": "1",
    "DISABLE_TELEMETRY": "1",
    "HOME": "/root",
    "HTTPS_PROXY": "http://127.0.0.1:18080",
    "HTTP_PROXY": "http://127.0.0.1:18080",
    "NODE_EXTRA_CA_CERTS": "/opt/mitm/mitmproxy-ca-cert.pem",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "http_proxy": "http://127.0.0.1:18080",
    "https_proxy": "http://127.0.0.1:18080",
}
OAUTH_ENVIRONMENT_VALUE = {
    "present": True,
    "reason": "credential",
    "redacted": True,
    "secret_sources": ["claude_oauth_runtime_access_token_value"],
}

CLIENT_APP_ENV = "CLAUDE_AGENT_SDK_CLIENT_APP"
CONTAINER_ID_ENV = "CLAUDE_CODE_CONTAINER_ID"
REMOTE_SESSION_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"
MAX_SPAWN_DEPTH_ENV = "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"

CONDITIONAL_HEADER_SLOTS = (
    "x-app",
    "x-claude-code-agent-id",
    "x-claude-code-parent-agent-id",
    "x-claude-remote-container-id",
    "x-claude-remote-session-id",
    "x-client-app",
    "x-client-request-id",
)

HEADER_ENVIRONMENTS = {
    "header-baseline": {},
    "header-client-app": {CLIENT_APP_ENV: CLIENT_APP},
    "header-container": {CONTAINER_ID_ENV: CONTAINER_ID},
    "header-remote-session": {REMOTE_SESSION_ENV: REMOTE_SESSION_ID},
    "header-combination": {
        CLIENT_APP_ENV: CLIENT_APP,
        CONTAINER_ID_ENV: CONTAINER_ID,
        REMOTE_SESSION_ENV: REMOTE_SESSION_ID,
        MAX_SPAWN_DEPTH_ENV: MAX_SPAWN_DEPTH,
    },
}
AGENT_ENVIRONMENT = {MAX_SPAWN_DEPTH_ENV: MAX_SPAWN_DEPTH}
EXPECTED_CATEGORY_COUNTS = {
    "header-baseline": 2,
    "header-client-app": 2,
    "header-container": 2,
    "header-remote-session": 2,
    "header-combination": 2,
    "agent-a1": 2,
    "agent-a2": 2,
    "agent-a3": 2,
    "retry-3": 2,
    "retry-5": 2,
    "retry-9": 2,
}


class EvidenceValidationError(ValueError):
    """表示证据缺失、歧义或相互矛盾。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceValidationError(message)


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceValidationError(f"JSON 出现重复键：{key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_object)
    _require(isinstance(value, dict), f"JSON 顶层必须是对象：{path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"JSONL 含空行：{path}:{line_number}")
        value = json.loads(line, object_pairs_hook=_no_duplicate_object)
        _require(isinstance(value, dict), f"JSONL 记录必须是对象：{path}:{line_number}")
        records.append(value)
    _require(bool(records), f"JSONL 不得为空：{path}")
    return records


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _secure_json_size(value: Any) -> int:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return len(payload.encode("utf-8"))


def _mapping(value: Any, label: str) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{label} 必须是对象")
    return value


def _list(value: Any, label: str) -> list[Any]:
    _require(isinstance(value, list), f"{label} 必须是数组")
    return value


def _string(value: Any, label: str) -> str:
    _require(isinstance(value, str) and bool(value), f"{label} 必须是非空字符串")
    return value


def _integer(value: Any, label: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool), f"{label} 必须是整数")
    return value


def _headers(record: Mapping[str, Any], label: str) -> tuple[list[str], dict[str, str]]:
    request = _mapping(record.get("request"), f"{label}.request")
    rows = _list(request.get("headers"), f"{label}.request.headers")
    names: list[str] = []
    values: dict[str, str] = {}
    for index, row in enumerate(rows):
        _require(isinstance(row, list) and len(row) == 2, f"{label} Header #{index} 形状错误")
        name = _string(row[0], f"{label} Header #{index} 名称").lower()
        value = _string(row[1], f"{label} Header #{index} 值")
        _require(name not in values, f"{label} Header 重名：{name}")
        names.append(name)
        values[name] = value
    return names, values


def _is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _validate_invocation(
    invocation: Mapping[str, Any],
    *,
    scenario: str,
    injected_env: Mapping[str, Any],
    run_id: str,
) -> dict[str, str]:
    expected_keys = {
        "argv_redacted",
        "argv_sha256",
        "cwd",
        "environment",
        "redaction_policy",
        "redactions",
        "schema_version",
        "shell",
        "stdin_mode",
    }
    _require(set(invocation) == expected_keys, f"invocation 字段集合错误：{run_id}")
    _require(invocation.get("schema_version") == "official-client-invocation/v1", f"invocation schema 错误：{run_id}")
    _require(invocation.get("shell") is False and invocation.get("stdin_mode") == "devnull", f"invocation shell/stdin 错误：{run_id}")
    _require(invocation.get("cwd") == "/work", f"invocation cwd 错误：{run_id}")
    _require(invocation.get("redaction_policy") == "known-secret-and-sensitive-option/v1", f"invocation 脱敏策略错误：{run_id}")
    _require(invocation.get("redactions") == [], f"本矩阵最终 argv 不应含敏感参数：{run_id}")
    argv = _list(invocation.get("argv_redacted"), f"{run_id}.argv_redacted")
    _require(all(isinstance(value, str) for value in argv), f"argv 必须全为字符串：{run_id}")
    argv_sha256 = _canonical_sha256(argv)
    _require(invocation.get("argv_sha256") == argv_sha256, f"argv_sha256 重算失败：{run_id}")
    _require(argv_sha256 == EXPECTED_ARGV_SHA256.get(scenario), f"最终 argv 不属于既定 scenario：{run_id}")

    environment = _mapping(invocation.get("environment"), f"{run_id}.environment")
    _require(set(environment) == {"schema_version", "values", "keys", "redacted_keys", "sha256"}, f"环境 manifest 字段集合错误：{run_id}")
    _require(environment.get("schema_version") == "official-client-environment/v1", f"环境 manifest schema 错误：{run_id}")
    values = _mapping(environment.get("values"), f"{run_id}.environment.values")
    expected_values: dict[str, Any] = dict(BASE_PROCESS_ENVIRONMENT)
    expected_values.update(injected_env)
    expected_values["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH_ENVIRONMENT_VALUE
    _require(values == dict(sorted(expected_values.items())), f"最终进程完整环境不符：{run_id}")
    _require(environment.get("keys") == sorted(values), f"环境 keys 未精确绑定 values：{run_id}")
    _require(environment.get("redacted_keys") == ["CLAUDE_CODE_OAUTH_TOKEN"], f"OAuth 环境脱敏项错误：{run_id}")
    environment_sha256 = _canonical_sha256(values)
    _require(environment.get("sha256") == environment_sha256, f"environment.sha256 重算失败：{run_id}")
    return {"argv_sha256": argv_sha256, "environment_sha256": environment_sha256}


def _validate_secret_scan_snapshot(
    run_dir: Path,
    manifest: Mapping[str, Any],
    secret_scan: Mapping[str, Any],
) -> dict[str, int]:
    artifacts = _list(manifest.get("artifacts"), f"{run_dir.name}.artifacts")
    artifact_sizes = {
        _string(_mapping(item, f"{run_dir.name}.artifact").get("path"), "artifact.path"):
        _integer(_mapping(item, f"{run_dir.name}.artifact").get("size"), "artifact.size")
        for item in artifacts
    }
    _require("recovery.json" in artifact_sizes, f"secret scan 快照缺 recovery：{run_dir.name}")
    recovery = _load_json(run_dir / "recovery.json")
    _require(
        recovery.get("status") == "complete"
        and recovery.get("active_resource") is None
        and recovery.get("cleanup_successful") is True,
        f"recovery 最终状态错误：{run_dir.name}",
    )
    pre_scan_recovery = dict(recovery)
    pre_scan_recovery["status"] = "running"

    pre_scan_manifest = copy.deepcopy(dict(manifest))
    pre_scan_manifest["status"] = "running"
    pre_scan_manifest["ended_at"] = None
    pre_scan_manifest["cleanup"] = {"attempted": False, "successful": None}
    pre_scan_manifest["secret_scan"] = {
        "performed": False,
        "scope": [],
        "matches": [],
        "limitation": None,
    }
    pre_scan_manifest["m_binding"] = {
        "required": False,
        "complete": False,
        "requirements": {},
        "limitations": [],
    }
    pre_scan_manifest["artifacts"] = []
    unchanged_bytes = sum(size for path, size in artifact_sizes.items() if path != "recovery.json")
    expected_byte_count = (
        unchanged_bytes
        + _secure_json_size(pre_scan_recovery)
        + _secure_json_size(pre_scan_manifest)
    )
    _require(secret_scan.get("file_count") == len(artifacts) + 1, f"secret scan 文件数不符：{run_dir.name}")
    _require(secret_scan.get("byte_count") == expected_byte_count, f"secret scan 扫描时字节快照无法重建：{run_dir.name}")
    return {
        "file_count": len(artifacts) + 1,
        "byte_count": expected_byte_count,
    }


def _validate_post_run_secret_receipt(
    capture_root: Path,
    run_dir: Path,
    manifest: Mapping[str, Any],
    secret_scan: Mapping[str, Any],
) -> dict[str, Any]:
    run_id = _string(manifest.get("run_id"), f"{run_dir.name}.run_id")
    receipt_path = capture_root / "post-run-secret-scan-receipts" / f"{run_id}.json"
    _require(receipt_path.is_file() and not receipt_path.is_symlink(), f"post-run secret receipt 缺失或为符号链接：{run_id}")
    receipt = _load_json(receipt_path)
    expected_fields = {
        "schema_version",
        "run_id",
        "scan_root",
        "algorithm",
        "secret_labels",
        "final_manifest_sha256",
        "files",
        "file_count",
        "byte_count",
        "inventory_sha256",
        "manifest_artifact_inventory_verified",
        "matches",
        "scan_errors",
        "passed",
        "tool_sha256",
    }
    _require(set(receipt) == expected_fields, f"post-run secret receipt 字段集错误：{run_id}")
    _require(receipt.get("schema_version") == POST_RUN_SECRET_RECEIPT_SCHEMA, f"post-run secret receipt schema 错误：{run_id}")
    _require(receipt.get("run_id") == run_id, f"post-run secret receipt run_id 不符：{run_id}")
    _require(receipt.get("scan_root") == ".", f"post-run secret receipt scan_root 不是相对根：{run_id}")
    _require(receipt.get("algorithm") == "exact-byte-match/v1", f"post-run secret receipt 算法错误：{run_id}")
    expected_labels = sorted(_list(secret_scan.get("scope"), f"{run_id}.secret_scan.scope"))
    _require(receipt.get("secret_labels") == expected_labels, f"post-run secret labels 未与 manifest 精确绑定：{run_id}")

    actual_files: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        _require(not path.is_symlink(), f"post-run secret scan 运行目录不得含符号链接：{path}")
        if path.is_file():
            actual_files.append({
                "path": path.relative_to(run_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            })
    _require(any(item["path"] == "manifest.json" for item in actual_files), f"post-run secret inventory 缺最终 manifest：{run_id}")
    receipt_files = _list(receipt.get("files"), f"{run_id}.post_run_secret.files")
    for index, item_value in enumerate(receipt_files):
        item = _mapping(item_value, f"{run_id}.post_run_secret.files[{index}]")
        _require(set(item) == {"path", "size", "sha256"}, f"post-run secret inventory 项字段错误：{run_id}.{index}")
    _require(receipt_files == actual_files, f"post-run secret inventory 与当前运行字节不一致：{run_id}")
    _require(receipt.get("file_count") == len(actual_files), f"post-run secret file_count 不符：{run_id}")
    _require(receipt.get("byte_count") == sum(item["size"] for item in actual_files), f"post-run secret byte_count 不符：{run_id}")
    _require(receipt.get("inventory_sha256") == _canonical_sha256(actual_files), f"post-run secret inventory_sha256 重算失败：{run_id}")
    manifest_sha256 = _sha256_file(run_dir / "manifest.json")
    _require(receipt.get("final_manifest_sha256") == manifest_sha256, f"post-run secret 未绑定最终 manifest：{run_id}")
    _require(receipt.get("manifest_artifact_inventory_verified") is True, f"post-run secret 未验证 manifest artifact inventory：{run_id}")
    _require(receipt.get("matches") == [] and receipt.get("scan_errors") == [], f"post-run secret scan 有命中或读取错误：{run_id}")
    _require(receipt.get("passed") is True, f"post-run secret scan 未通过：{run_id}")

    tool_path = Path(__file__).resolve().with_name(POST_RUN_SECRET_TOOL_FILENAME)
    _require(tool_path.is_file() and not tool_path.is_symlink(), f"post-run secret receipt 生成器缺失：{tool_path}")
    tool_sha256 = _sha256_file(tool_path)
    _require(receipt.get("tool_sha256") == tool_sha256, f"post-run secret receipt tool_sha256 未绑定当前生成器：{run_id}")
    return {
        "path": receipt_path,
        "receipt_sha256": _sha256_file(receipt_path),
        "inventory_sha256": receipt.get("inventory_sha256"),
        "tool_sha256": tool_sha256,
        "file_count": len(actual_files),
        "byte_count": receipt.get("byte_count"),
    }


def _artifact_inventory(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = _list(manifest.get("artifacts"), f"{run_dir.name}.artifacts")
    expected_paths: set[str] = set()
    for item_index, item_value in enumerate(artifacts):
        item = _mapping(item_value, f"{run_dir.name}.artifacts[{item_index}]")
        relative = _string(item.get("path"), f"{run_dir.name}.artifacts[{item_index}].path")
        relative_path = Path(relative)
        _require(
            "\\" not in relative
            and not relative_path.is_absolute()
            and ".." not in relative_path.parts
            and "." not in relative_path.parts
            and relative_path.as_posix() == relative,
            f"非法产物路径：{relative}",
        )
        _require(relative not in expected_paths, f"产物清单路径重复：{relative}")
        expected_paths.add(relative)
        path = run_dir / relative_path
        _require(path.is_file() and not path.is_symlink(), f"产物缺失或不是普通文件：{path}")
        _require(_sha256_file(path) == item.get("sha256"), f"产物 SHA-256 不符：{path}")
        _require(path.stat().st_size == item.get("size"), f"产物大小不符：{path}")
        expected_mode = f"{oct(stat.S_IMODE(path.stat().st_mode))}"
        _require(expected_mode == item.get("mode"), f"产物权限不符：{path}")

    actual_paths: set[str] = set()
    for path in run_dir.rglob("*"):
        _require(not path.is_symlink(), f"运行目录不得含符号链接：{path}")
        if path.is_file() and path.name != "manifest.json":
            actual_paths.add(path.relative_to(run_dir).as_posix())
    _require(expected_paths == actual_paths, f"产物清单与磁盘文件集合不一致：{run_dir.name}")
    return {"artifact_count": len(artifacts), "all_artifact_hashes_match": True}


def _validate_receipt(
    capture_root: Path,
    run_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    runtime = _mapping(manifest.get("runtime"), f"{run_dir.name}.runtime")
    snapshot = _mapping(runtime.get("host_runtime_receipt"), f"{run_dir.name}.host_runtime_receipt")
    batch_id = _string(manifest.get("batch_id"), f"{run_dir.name}.batch_id")
    expected_path = f"/capture/runtime-receipts/{batch_id}.json"
    _require(snapshot.get("path") == expected_path, f"host receipt 路径未绑定 batch：{run_dir.name}")
    receipt_path = capture_root / "runtime-receipts" / f"{batch_id}.json"
    _require(receipt_path.is_file() and not receipt_path.is_symlink(), f"host receipt 缺失：{receipt_path}")
    receipt_sha256 = _sha256_file(receipt_path)
    _require(snapshot.get("sha256") == receipt_sha256, f"host receipt SHA-256 不符：{run_dir.name}")
    receipt = _load_json(receipt_path)

    source_bundle = _mapping(receipt.get("capture_source_bundle"), f"{run_dir.name}.receipt.source_bundle")
    source_files = _list(source_bundle.get("files"), f"{run_dir.name}.receipt.source_bundle.files")
    _require(source_bundle.get("algorithm") == "canonical-json-sha256", f"源码聚合算法错误：{run_dir.name}")
    _require(source_bundle.get("sha256") == _canonical_sha256(source_files), f"源码聚合 SHA 自校验失败：{run_dir.name}")
    source_paths: set[str] = set()
    for index, source_value in enumerate(source_files):
        source = _mapping(source_value, f"{run_dir.name}.receipt.source_bundle.files[{index}]")
        source_path = _string(source.get("path"), f"{run_dir.name}.source.path")
        source_path_value = Path(source_path)
        _require(
            source_path not in source_paths
            and "\\" not in source_path
            and not source_path_value.is_absolute()
            and ".." not in source_path_value.parts
            and "." not in source_path_value.parts
            and source_path_value.as_posix() == source_path,
            f"源码身份路径非法或重复：{run_dir.name}.{source_path}",
        )
        source_paths.add(source_path)
        _require(re.fullmatch(r"[a-f0-9]{64}", str(source.get("sha256"))) is not None, f"源码文件 SHA 形状错误：{run_dir.name}.{source_path}")
        _require(_integer(source.get("size"), f"{run_dir.name}.{source_path}.size") >= 0, f"源码文件大小非法：{run_dir.name}.{source_path}")
    execution_sources = _mapping(
        _mapping(runtime.get("capture_tools"), f"{run_dir.name}.capture_tools").get("execution_sources"),
        f"{run_dir.name}.execution_sources",
    )
    _require(source_bundle == execution_sources, f"host 与容器执行源码身份不一致：{run_dir.name}")
    _require(snapshot.get("capture_source_bundle_sha256") == source_bundle.get("sha256"), f"源码聚合 SHA 未绑定：{run_dir.name}")

    equal_fields = (
        "schema_version",
        "producer",
        "issued_at_utc",
        "run_nonce",
        "runtime_image_id",
        "runtime_image_reference",
        "repo_digest_verified",
        "container",
        "docker_server",
    )
    for field in equal_fields:
        _require(snapshot.get(field) == receipt.get(field), f"host receipt 字段未原样绑定：{run_dir.name}.{field}")

    _require(receipt.get("repo_digest_verified") is True, f"镜像 RepoDigest 未验证：{run_dir.name}")
    _require(re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("run_nonce"))) is not None, f"run nonce 形状错误：{run_dir.name}")
    _require(re.fullmatch(r"sha256:[a-f0-9]{64}", str(receipt.get("runtime_image_id"))) is not None, f"镜像 ID 形状错误：{run_dir.name}")
    _require(re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", str(receipt.get("runtime_image_reference"))) is not None, f"镜像不可变引用形状错误：{run_dir.name}")
    producer = _mapping(receipt.get("producer"), f"{run_dir.name}.receipt.producer")
    _require(producer == EXPECTED_HOST_RECEIPT_PRODUCER, f"host receipt producer 身份不符：{run_dir.name}")

    container = _mapping(receipt.get("container"), f"{run_dir.name}.receipt.container")
    binding = _mapping(snapshot.get("container_runtime_binding"), f"{run_dir.name}.container_runtime_binding")
    _require(binding.get("verified") is True, f"容器运行身份未验证：{run_dir.name}")
    _require(binding.get("container_id") == container.get("id"), f"容器 ID 绑定失败：{run_dir.name}")
    _require(binding.get("hostname") == container.get("hostname"), f"容器 hostname 绑定失败：{run_dir.name}")
    _require(runtime.get("runtime_image_verified") is True, f"运行镜像未验证：{run_dir.name}")
    _require(runtime.get("runtime_image_limitation") is None, f"运行镜像仍有限制：{run_dir.name}")
    _require(runtime.get("runtime_image_claim") == receipt.get("runtime_image_reference"), f"运行镜像 claim 不一致：{run_dir.name}")
    return {
        "receipt_sha256": receipt_sha256,
        "run_nonce_sha256": hashlib.sha256(_string(receipt.get("run_nonce"), "run_nonce").encode()).hexdigest(),
        "source_bundle_sha256": source_bundle.get("sha256"),
        "runtime_image_id": receipt.get("runtime_image_id"),
        "runtime_image_reference": receipt.get("runtime_image_reference"),
        "container_id": container.get("id"),
        "receipt_filename": receipt_path.name,
        "source_sha256_by_path": {
            _string(_mapping(item, f"{run_dir.name}.source").get("path"), "source.path"):
            _string(_mapping(item, f"{run_dir.name}.source").get("sha256"), "source.sha256")
            for item in source_files
        },
    }


def _validate_manifest(capture_root: Path, run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    _require(manifest_path.is_file() and not manifest_path.is_symlink(), f"manifest 缺失：{run_dir}")
    manifest = _load_json(manifest_path)
    run_id = _string(manifest.get("run_id"), f"{run_dir.name}.run_id")
    _require(run_id == run_dir.name, f"run_id 与目录名不一致：{run_dir.name}")
    _require(manifest.get("schema_version") == "official-client-capture/v1", f"manifest 版本错误：{run_id}")
    _require(manifest.get("task") == "oauth", f"task 不是 oauth：{run_id}")
    _require(manifest.get("status") == "complete", f"运行未完成：{run_id}")

    cases = _list(manifest.get("cases"), f"{run_id}.cases")
    results = _list(manifest.get("case_results"), f"{run_id}.case_results")
    _require(len(cases) == len(results) == 1, f"运行必须恰有一个 case/result：{run_id}")
    case = _mapping(cases[0], f"{run_id}.case")
    result = _mapping(results[0], f"{run_id}.case_result")
    scenarios = _list(case.get("scenarios"), f"{run_id}.scenarios")
    _require(len(scenarios) == 1 and isinstance(scenarios[0], str), f"运行必须恰有一个 scenario：{run_id}")
    scenario = scenarios[0]
    expected_case = {
        "boundary": "official_cli_to_official_platform",
        "subject": "claude-http",
        "product": "claude",
        "transport": "http",
        "evidence": "mitm",
    }
    for field, expected in expected_case.items():
        _require(case.get(field) == expected and result.get(field) == expected, f"case 范围不符：{run_id}.{field}")
    _require(case.get("task") == "oauth", f"case task 不是 oauth：{run_id}")
    _require(case.get("run_id") == run_id, f"case.run_id 不符：{run_id}")
    _require(case.get("target_hosts") == ["api.anthropic.com"], f"case target_hosts 不符：{run_id}")
    _require(result.get("scenario") == scenario and result.get("status") == "complete", f"case_result 未完成：{run_id}")

    m_binding = _mapping(manifest.get("m_binding"), f"{run_id}.m_binding")
    requirements = _mapping(m_binding.get("requirements"), f"{run_id}.m_binding.requirements")
    _require(m_binding.get("required") is True and m_binding.get("complete") is True, f"M 绑定不完整：{run_id}")
    _require(set(requirements) == M_REQUIREMENT_KEYS, f"M 子要求字段集合错误：{run_id}")
    _require(all(value is True for value in requirements.values()), f"M 子要求不完整：{run_id}")
    _require(m_binding.get("limitations") == [], f"M 绑定仍有限制：{run_id}")

    secret_scan = _mapping(manifest.get("secret_scan"), f"{run_id}.secret_scan")
    _require(secret_scan.get("algorithm") == "exact-byte-match/v1", f"secret scan 算法错误：{run_id}")
    _require(secret_scan.get("included_root") == ".", f"secret scan 根目录错误：{run_id}")
    _require(secret_scan.get("performed") is True and secret_scan.get("passed") is True, f"secret scan 未通过：{run_id}")
    _require(secret_scan.get("matches") == [] and secret_scan.get("scan_errors") == [], f"secret scan 有命中或错误：{run_id}")
    _require(secret_scan.get("excluded") == [] and secret_scan.get("limitation") is None, f"secret scan 有排除或限制：{run_id}")
    _require(_integer(secret_scan.get("byte_count"), f"{run_id}.secret_scan.byte_count") > 0, f"secret scan 字节数必须大于 0：{run_id}")
    _require(
        _list(secret_scan.get("scope"), f"{run_id}.secret_scan.scope")
        == EXPECTED_SECRET_LABELS,
        f"secret scan 范围不完整或顺序不唯一：{run_id}",
    )

    cleanup = _mapping(manifest.get("cleanup"), f"{run_id}.cleanup")
    _require(cleanup == {"attempted": True, "successful": True}, f"清理未成功：{run_id}")
    clients = _mapping(manifest.get("clients"), f"{run_id}.clients")
    claude = _mapping(clients.get("claude"), f"{run_id}.clients.claude")
    _require(claude.get("version") == "2.1.220 (Claude Code)", f"Claude Code 版本不符：{run_id}")
    _require(
        claude.get("sha256") == claude.get("expected_sha256") == EXPECTED_CLAUDE_BINARY_SHA256,
        f"Claude Code 二进制 SHA 未命中独立预期值：{run_id}",
    )
    inventory = _artifact_inventory(run_dir, manifest)
    scan_snapshot = _validate_secret_scan_snapshot(run_dir, manifest, secret_scan)
    post_secret_receipt = _validate_post_run_secret_receipt(capture_root, run_dir, manifest, secret_scan)
    receipt = _validate_receipt(capture_root, run_dir, manifest)

    result_root = run_dir / "results" / "mitm" / "claude-http" / scenario
    raw_root = run_dir / "mitm" / "claude-http" / scenario
    summary_path = result_root / "summary.json"
    invocation_path = result_root / "invocation.json"
    stdout_path = result_root / "stdout.jsonl"
    raw_path = raw_root / "claude-http.jsonl"
    lifecycle_path = raw_root / "lifecycle-claude.ndjson"
    summary = _load_json(summary_path)
    invocation = _load_json(invocation_path)
    _require(summary == result.get("scenario_result"), f"summary 与 manifest 不一致：{run_id}")
    _require(summary.get("valid") is True and summary.get("return_code") == 0, f"scenario 校验失败：{run_id}")
    _require(summary.get("runtime_secret_exposed") is False, f"运行时 secret 暴露：{run_id}")
    _require(summary.get("result_count") == 1 and summary.get("success_result_count") == 1, f"成功结果数异常：{run_id}")
    summary_invocation = dict(_mapping(summary.get("invocation"), f"{run_id}.summary.invocation"))
    invocation_file_sha256 = summary_invocation.pop("file_sha256", None)
    _require(invocation == summary_invocation, f"invocation 与 summary 不一致：{run_id}")
    _require(invocation_file_sha256 == _sha256_file(invocation_path), f"invocation 文件 SHA 未绑定：{run_id}")

    runtime = _mapping(manifest.get("runtime"), f"{run_id}.runtime")
    auth = _mapping(_mapping(runtime.get("auth_preflight"), f"{run_id}.auth_preflight").get("claude"), f"{run_id}.auth_preflight.claude")
    _require(auth == {"api_provider": "firstParty", "auth_method": "runtime_oauth_token_override", "logged_in": True}, f"Claude OAuth 预检身份不符：{run_id}")
    _require(runtime.get("m_binding_requested") is True, f"运行未请求完整 M：{run_id}")
    injected_env = _mapping(runtime.get("injected_probe_env"), f"{run_id}.injected_probe_env")
    invocation_binding = _validate_invocation(
        invocation,
        scenario=scenario,
        injected_env=injected_env,
        run_id=run_id,
    )

    return {
        "run_id": run_id,
        "manifest": manifest,
        "manifest_sha256": _sha256_file(manifest_path),
        "scenario": scenario,
        "summary": summary,
        "injected_env": injected_env,
        "fault_spec": runtime.get("injected_fault_spec"),
        "raw_records": _load_jsonl(raw_path),
        "lifecycle_records": _load_jsonl(lifecycle_path),
        "stdout_records": _load_jsonl(stdout_path),
        "receipt": receipt,
        "inventory": inventory,
        "scan_snapshot": scan_snapshot,
        "post_secret_receipt": post_secret_receipt,
        "invocation": invocation,
        "invocation_binding": invocation_binding,
        "run_dir": run_dir,
        "receipt_path": capture_root / "runtime-receipts" / f"{manifest['batch_id']}.json",
        "post_secret_receipt_path": post_secret_receipt["path"],
        "invocation_path": invocation_path,
        "summary_path": summary_path,
        "stdout_path": stdout_path,
        "raw_path": raw_path,
        "lifecycle_path": lifecycle_path,
    }


def _classify_run(run: Mapping[str, Any]) -> str:
    scenario = run.get("scenario")
    injected_env = run.get("injected_env")
    fault_spec = run.get("fault_spec")
    if fault_spec is not None:
        match = re.fullmatch(r"status=500,count=(3|5|9)", str(fault_spec))
        _require(match is not None and scenario == "s1" and injected_env == {}, f"未知故障矩阵：{run.get('run_id')}")
        return f"retry-{match.group(1)}"
    for category, expected_env in HEADER_ENVIRONMENTS.items():
        expected_scenario = "a2" if category == "header-combination" else "s1"
        if scenario == expected_scenario and injected_env == expected_env:
            return category
    if scenario in {"a1", "a2", "a3"} and injected_env == AGENT_ENVIRONMENT:
        return f"agent-{scenario}"
    raise EvidenceValidationError(f"运行不属于既定矩阵：{run.get('run_id')}")


def _validate_http_records(run: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _string(run.get("run_id"), "run_id")
    injected_env = _mapping(run.get("injected_env"), f"{run_id}.injected_env")
    records = _list(run.get("raw_records"), f"{run_id}.raw_records")
    session_ids: list[str] = []
    request_ids: list[str] = []
    flow_ids: list[str] = []
    statuses: list[int] = []
    observed_user_agents: set[str] = set()
    observed_conditional_slots: set[str] = set()
    expected_conditionals = {
        "x-client-app": injected_env.get(CLIENT_APP_ENV),
        "x-claude-remote-container-id": injected_env.get(CONTAINER_ID_ENV),
        "x-claude-remote-session-id": injected_env.get(REMOTE_SESSION_ENV),
    }

    for index, record_value in enumerate(records):
        record = _mapping(record_value, f"{run_id}.raw[{index}]")
        label = f"{run_id}.raw[{index}]"
        _require(record.get("_run_id") == run_id, f"raw run_id 不符：{label}")
        _require(
            record.get("_task") == "oauth"
            and record.get("_subject") == "claude-http"
            and record.get("_boundary") == "official_cli_to_official_platform",
            f"raw 范围不符：{label}",
        )
        _require(record.get("_scenario") == run.get("scenario") and record.get("_category") == "claude", f"raw scenario/category 不符：{label}")
        request = _mapping(record.get("request"), f"{label}.request")
        _require(request.get("method") == "POST" and request.get("host") == "api.anthropic.com", f"主请求端点不符：{label}")
        _require(request.get("path") == "/v1/messages?beta=true" and request.get("http_version") == "HTTP/1.1", f"主请求 path/protocol 不符：{label}")
        names, headers = _headers(record, label)
        authorization = _string(headers.get("authorization"), f"{label}.authorization")
        _require(re.fullmatch(r"<redacted len=\d+>", authorization) is not None, f"Authorization 未安全脱敏：{label}")
        _require(headers.get("x-app") == "cli" and headers.get("x-stainless-retry-count") == "0", f"固定 Header 值异常：{label}")

        for name, expected in expected_conditionals.items():
            if expected is None:
                _require(name not in headers, f"负例出现条件 Header：{label}.{name}")
            else:
                _require(headers.get(name) == expected, f"条件 Header 值不符：{label}.{name}")

        expected_ua = BASE_USER_AGENT
        if CLIENT_APP_ENV in injected_env:
            expected_ua = f"claude-cli/2.1.220 (external, sdk-cli, client-app/{injected_env[CLIENT_APP_ENV]})"
        _require(headers.get("user-agent") == expected_ua, f"UA 与 x-client-app 未联动：{label}")
        observed_user_agents.add(expected_ua)

        actual_slots = [name for name in names if name in CONDITIONAL_HEADER_SLOTS]
        expected_slots = [name for name in CONDITIONAL_HEADER_SLOTS if name in headers]
        _require(actual_slots == expected_slots, f"条件 Header 七字段投影顺序错误：{label}")
        observed_conditional_slots.update(actual_slots)
        _require("x-claude-code-parent-agent-id" not in headers or "x-claude-code-agent-id" in headers, f"parent-agent-id 无当前 agent-id：{label}")

        session_id = _string(headers.get("x-claude-code-session-id"), f"{label}.session_id")
        request_id = _string(headers.get("x-client-request-id"), f"{label}.request_id")
        _require(_is_uuid(session_id) and _is_uuid(request_id), f"动态请求 ID 形状错误：{label}")
        session_ids.append(session_id)
        request_ids.append(request_id)
        flow_ids.append(_string(record.get("_flow_id"), f"{label}.flow_id"))
        response = _mapping(record.get("response"), f"{label}.response")
        statuses.append(_integer(response.get("status"), f"{label}.response.status"))

    _require(len(set(session_ids)) == 1, f"同一运行的 session-id 不稳定：{run_id}")
    _require(len(set(request_ids)) == len(request_ids), f"x-client-request-id 未逐请求更新：{run_id}")
    _require(len(set(flow_ids)) == len(flow_ids), f"raw flow_id 未逐请求唯一：{run_id}")
    if run.get("fault_spec") is None:
        _require(all(status == 200 for status in statuses), f"非 fault 运行出现非 200 响应：{run_id}")
    return {
        "request_count": len(records),
        "status_sequence": statuses,
        "user_agents": sorted(observed_user_agents),
        "conditional_headers": {name: value for name, value in expected_conditionals.items()},
        "conditional_header_projection_order_verified": True,
        "observed_conditional_header_projection": [
            name for name in CONDITIONAL_HEADER_SLOTS if name in observed_conditional_slots
        ],
        "session_id_stable": True,
        "session_id_sha256": hashlib.sha256(session_ids[0].encode()).hexdigest(),
        "_http_session_id": session_ids[0],
        "_client_request_ids": request_ids,
        "client_request_ids_unique": True,
    }


def _message_blocks(event: Mapping[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _content_texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return texts
    return []


def _analyze_agent_run(run: Mapping[str, Any]) -> dict[str, Any]:
    run_id = _string(run.get("run_id"), "run_id")
    scenario = _string(run.get("scenario"), f"{run_id}.scenario")
    _require(re.fullmatch(r"a[123]", scenario) is not None, f"不是 Agent scenario：{run_id}")
    depth = int(scenario[1:])
    events = _list(run.get("stdout_records"), f"{run_id}.stdout_records")
    summary = _mapping(run.get("summary"), f"{run_id}.summary")
    for field in ("tool_use_count", "tool_use_raw_count", "tool_result_count", "tool_result_raw_count"):
        _require(summary.get(field) == depth, f"{field} 与深度不符：{run_id}")
    _require(summary.get("tool_use_duplicate_count") == 0 and summary.get("tool_result_duplicate_count") == 0, f"Agent 工具事件有重复：{run_id}")
    _require(summary.get("markers_present") is True and summary.get("tool_block_conflict") is False, f"Agent marker/tool 冲突：{run_id}")

    tool_uses: list[tuple[Any, dict[str, Any]]] = []
    for event_value in events:
        event = _mapping(event_value, f"{run_id}.stdout.event")
        if event.get("type") != "assistant":
            continue
        for block in _message_blocks(event):
            if block.get("type") == "tool_use":
                tool_uses.append((event.get("parent_tool_use_id"), block))
    _require(len(tool_uses) == depth, f"Agent tool_use 数不符：{run_id}")
    tool_ids: list[str] = []
    tool_input_hashes: list[str] = []
    prompts: list[str] = []
    for index, (parent_tool_use_id, block) in enumerate(tool_uses, 1):
        tool_id = _string(block.get("id"), f"{run_id}.tool_use[{index}].id")
        _require(tool_id not in tool_ids, f"Agent tool_use ID 重复：{run_id}")
        expected_parent = tool_ids[index - 2] if index > 1 else None
        _require(parent_tool_use_id == expected_parent, f"parent_tool_use_id 链断裂：{run_id}.depth{index}")
        _require(block.get("name") == "Agent", f"工具名不是 Agent：{run_id}.depth{index}")
        tool_input = _mapping(block.get("input"), f"{run_id}.tool_use[{index}].input")
        _require(set(tool_input) == {"description", "subagent_type", "run_in_background", "prompt"}, f"Agent 参数集合错误：{run_id}.depth{index}")
        _require(tool_input.get("description") == f"probe depth{depth} child{index}", f"Agent description 错误：{run_id}.depth{index}")
        _require(tool_input.get("subagent_type") == "general-purpose" and tool_input.get("run_in_background") is False, f"Agent 执行参数错误：{run_id}.depth{index}")
        prompt = _string(tool_input.get("prompt"), f"{run_id}.tool_use[{index}].prompt")
        _require(f"D{depth}_C{index}_OK" in prompt, f"Agent prompt 缺目标 marker：{run_id}.depth{index}")
        if index < depth:
            _require(f"probe depth{depth} child{index + 1}" in prompt and f"D{depth}_C{index + 1}_OK" in prompt, f"Agent prompt 未定义下一层：{run_id}.depth{index}")
        tool_ids.append(tool_id)
        tool_input_hashes.append(_canonical_sha256(tool_input))
        prompts.append(prompt)
    _require(
        tuple(tool_input_hashes) == EXPECTED_AGENT_INPUT_SHA256[scenario],
        f"Agent 完整调用参数与既定 scenario 不符：{run_id}",
    )

    tool_results: dict[str, tuple[Any, dict[str, Any]]] = {}
    for event_value in events:
        event = _mapping(event_value, f"{run_id}.stdout.event")
        if event.get("type") != "user":
            continue
        for block in _message_blocks(event):
            if block.get("type") != "tool_result":
                continue
            tool_id = _string(block.get("tool_use_id"), f"{run_id}.tool_result.tool_use_id")
            _require(tool_id not in tool_results, f"Agent tool_result 重复：{run_id}")
            tool_results[tool_id] = (event.get("parent_tool_use_id"), block)
    _require(set(tool_results) == set(tool_ids), f"tool_use/tool_result 无法一一对应：{run_id}")

    agent_ids: list[str] = []
    for index, tool_id in enumerate(tool_ids, 1):
        parent_tool_use_id, block = tool_results[tool_id]
        expected_parent = tool_ids[index - 2] if index > 1 else None
        _require(parent_tool_use_id == expected_parent, f"tool_result 所属上下文错误：{run_id}.depth{index}")
        matches: list[str] = []
        for text_value in _content_texts(block.get("content")):
            matches.extend(re.findall(r"(?:^|\n)agentId: ([A-Za-z0-9_-]+)(?:\s|$)", text_value))
        _require(len(matches) == 1, f"tool_result 缺唯一 agentId：{run_id}.depth{index}")
        _require(matches[0] not in agent_ids, f"动态 agentId 跨层重复：{run_id}")
        agent_ids.append(matches[0])

        marker = f"D{depth}_C{index}_OK"
        marker_events = 0
        for event_value in events:
            event = _mapping(event_value, f"{run_id}.stdout.event")
            if event.get("type") != "assistant" or event.get("parent_tool_use_id") != tool_id:
                continue
            marker_events += sum(1 for block_value in _message_blocks(event) if block_value.get("type") == "text" and block_value.get("text") == marker)
        _require(marker_events == 1, f"子 Agent marker 不唯一：{run_id}.depth{index}")

    invocation = _mapping(run.get("invocation"), f"{run_id}.invocation")
    argv = _list(invocation.get("argv_redacted"), f"{run_id}.invocation.argv_redacted")
    invocation_text = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
    prompt_text = "\n".join(prompts)
    for dynamic_id in [*tool_ids, *agent_ids]:
        _require(
            dynamic_id not in invocation_text and dynamic_id not in prompt_text,
            f"动态 Agent/tool ID 被预置到 argv 或 prompt：{run_id}",
        )

    final_results = [
        event for event in events
        if event.get("type") == "result"
    ]
    _require(len(final_results) == 1, f"最终 result 数不为 1：{run_id}")
    final_result = final_results[0]
    _require(
        final_result.get("subtype") == "success"
        and final_result.get("is_error") is False
        and final_result.get("parent_tool_use_id") is None
        and final_result.get("result") == f"D{depth}_MAIN_OK",
        f"Agent 最终结果错误：{run_id}",
    )

    raw_records = _list(run.get("raw_records"), f"{run_id}.raw_records")
    expected_depths = list(range(depth + 1)) + list(range(depth - 1, -1, -1))
    _require(len(raw_records) == len(expected_depths), f"Agent HTTP 深度序列长度错误：{run_id}")
    actual_depths: list[int] = []
    for record_index, (record_value, expected_depth) in enumerate(zip(raw_records, expected_depths, strict=True)):
        _, headers = _headers(_mapping(record_value, f"{run_id}.raw[{record_index}]"), f"{run_id}.raw[{record_index}]")
        agent_id = headers.get("x-claude-code-agent-id")
        parent_agent_id = headers.get("x-claude-code-parent-agent-id")
        expected_agent_id = agent_ids[expected_depth - 1] if expected_depth else None
        expected_parent_id = agent_ids[expected_depth - 2] if expected_depth > 1 else None
        _require(agent_id == expected_agent_id and parent_agent_id == expected_parent_id, f"Agent HTTP 父子链错误：{run_id}.request{record_index}")
        actual_depths.append(expected_depth)
    return {
        "run_id": run_id,
        "depth": depth,
        "tool_use_count": depth,
        "tool_result_count": depth,
        "dynamic_agent_id_count": len(agent_ids),
        "dynamic_tool_id_count": len(tool_ids),
        "dynamic_agent_id_sha256": [hashlib.sha256(value.encode()).hexdigest() for value in agent_ids],
        "dynamic_tool_id_sha256": [hashlib.sha256(value.encode()).hexdigest() for value in tool_ids],
        "tool_input_sha256": tool_input_hashes,
        "parent_tool_use_id_chain_verified": True,
        "tool_result_agent_id_to_header_chain_verified": True,
        "request_depth_sequence": actual_depths,
        "final_result": final_result.get("result"),
        "_agent_ids": agent_ids,
        "_tool_ids": tool_ids,
    }


def _retry_delay_interval(attempt: int) -> tuple[int, int]:
    _require(attempt >= 1, "retry attempt 必须从 1 开始")
    base = min(500 * (2 ** (attempt - 1)), 32000)
    return base, int(base * 1.25)


def _analyze_retry_run(run: Mapping[str, Any], fault_count: int) -> dict[str, Any]:
    run_id = _string(run.get("run_id"), "run_id")
    raw_records = _list(run.get("raw_records"), f"{run_id}.raw_records")
    lifecycle = _list(run.get("lifecycle_records"), f"{run_id}.lifecycle_records")
    stdout = _list(run.get("stdout_records"), f"{run_id}.stdout_records")
    _require(len(raw_records) == fault_count + 1, f"retry 主请求数错误：{run_id}")
    statuses = [_integer(_mapping(record.get("response"), f"{run_id}.response").get("status"), f"{run_id}.status") for record in raw_records]
    _require(statuses == [500] * fault_count + [200], f"retry HTTP 状态序列错误：{run_id}")

    expected_events: list[str] = []
    for _ in range(fault_count):
        expected_events.extend(("request", "fault_status"))
    expected_events.append("request")
    _require([event.get("_event") for event in lifecycle] == expected_events, f"retry 生命周期事件序列错误：{run_id}")
    for index, event in enumerate(lifecycle):
        _require(
            event.get("_run_id") == run_id
            and event.get("_task") == "oauth"
            and event.get("_subject") == "claude-http"
            and event.get("_scenario") == "s1"
            and event.get("_category") == "claude"
            and event.get("_boundary") == "official_cli_to_official_platform",
            f"retry 生命周期范围元数据错误：{run_id}.event{index}",
        )
        _require(
            event.get("method") == "POST"
            and event.get("path") == "/v1/messages?beta=true"
            and event.get("http_version") == "HTTP/1.1",
            f"retry 生命周期端点元数据错误：{run_id}.event{index}",
        )
    request_events = [event for event in lifecycle if event.get("_event") == "request"]
    fault_events = [event for event in lifecycle if event.get("_event") == "fault_status"]
    _require([event.get("_flow_id") for event in request_events] == [record.get("_flow_id") for record in raw_records], f"retry flow 关联失败：{run_id}")
    _require(len(set(event.get("_flow_id") for event in request_events)) == fault_count + 1, f"retry flow ID 不唯一：{run_id}")
    for index, event in enumerate(fault_events):
        _require(
            event.get("_flow_id") == request_events[index].get("_flow_id")
            and event.get("injected_status") == 500
            and event.get("remaining_budget") == fault_count - index - 1,
            f"fault_status 记录或 flow 绑定错误：{run_id}.fault{index}",
        )
    _require(all(event.get("retry_count") == "0" for event in request_events), f"生命周期 retry_count 非 0：{run_id}")

    http_session_ids = {
        _headers(record, f"{run_id}.raw.session")[1].get("x-claude-code-session-id")
        for record in raw_records
    }
    _require(len(http_session_ids) == 1 and None not in http_session_ids, f"retry HTTP session-id 不稳定：{run_id}")
    http_session_id = next(iter(http_session_ids))

    retry_events = [event for event in stdout if event.get("type") == "system" and event.get("subtype") == "api_retry"]
    _require(len(retry_events) == fault_count, f"api_retry 数错误：{run_id}")
    delays: list[int] = []
    intervals: list[list[int]] = []
    retry_session_ids: set[str] = set()
    for attempt, event in enumerate(retry_events, 1):
        lower, upper = _retry_delay_interval(attempt)
        delay = _integer(event.get("retry_delay_ms"), f"{run_id}.api_retry[{attempt}].retry_delay_ms")
        _require(lower <= delay <= upper, f"retry_delay 超出理论区间：{run_id}.attempt{attempt}")
        _require(
            event.get("attempt") == attempt
            and event.get("max_retries") == 10
            and event.get("error_status") == 500
            and event.get("error") == "server_error",
            f"api_retry 字段错误：{run_id}.attempt{attempt}",
        )
        delays.append(delay)
        intervals.append([lower, upper])
        retry_session_ids.add(_string(event.get("session_id"), f"{run_id}.api_retry.session_id"))
    _require(len(retry_session_ids) == 1, f"api_retry session_id 不稳定：{run_id}")
    _require(retry_session_ids == {http_session_id}, f"api_retry 与 HTTP session-id 未绑定：{run_id}")
    init_events = [event for event in stdout if event.get("type") == "system" and event.get("subtype") == "init"]
    _require(len(init_events) == 1 and init_events[0].get("session_id") == http_session_id, f"stdout init 与 HTTP session-id 未绑定：{run_id}")

    monotonic_ns = [_integer(event.get("_captured_monotonic_ns"), f"{run_id}.request.monotonic_ns") for event in request_events]
    _require(all(after > before for before, after in zip(monotonic_ns, monotonic_ns[1:])), f"retry 请求时间不单调：{run_id}")
    fault_monotonic_ns = [
        _integer(event.get("_captured_monotonic_ns"), f"{run_id}.fault.monotonic_ns")
        for event in fault_events
    ]
    _require(
        all(
            monotonic_ns[index] <= fault_time < monotonic_ns[index + 1]
            for index, fault_time in enumerate(fault_monotonic_ns)
        ),
        f"retry fault_status 时间未夹在配对请求与下次请求之间：{run_id}",
    )
    gaps = [(after - before) / 1_000_000 for before, after in zip(monotonic_ns, monotonic_ns[1:])]
    overhead = [gap - delay for gap, delay in zip(gaps, delays, strict=True)]
    _require(all(0 <= value <= MAX_RETRY_OVERHEAD_MS for value in overhead), f"retry 实际 gap 与声明 delay 无法绑定：{run_id}")
    pre_cap_count = min(fault_count, 7)
    _require(all(after > before for before, after in zip(gaps[:pre_cap_count], gaps[1:pre_cap_count])), f"retry 封顶前实际 gap 未严格递增：{run_id}")

    final_results = [event for event in stdout if event.get("type") == "result"]
    _require(len(final_results) == 1, f"retry 最终 result 数不为 1：{run_id}")
    final_result = final_results[0]
    _require(
        final_result.get("subtype") == "success"
        and final_result.get("is_error") is False
        and final_result.get("api_error_status") is None
        and final_result.get("result") == "S1_OK",
        f"retry 最终结果错误：{run_id}",
    )
    _require(final_result.get("session_id") in retry_session_ids, f"api_retry 与最终结果 session 未关联：{run_id}")
    final_headers = _headers(raw_records[-1], f"{run_id}.final")[1]
    response_headers = {
        str(name).lower(): str(value)
        for name, value in _list(_mapping(raw_records[-1].get("response"), f"{run_id}.final.response").get("headers"), f"{run_id}.final.response.headers")
    }
    _require(response_headers.get("content-type") == "text/event-stream; charset=utf-8", f"retry 最终 200 不是 SSE：{run_id}")
    _require(final_headers.get("x-stainless-retry-count") == "0", f"retry 最终请求 SDK retry-count 非 0：{run_id}")
    return {
        "run_id": run_id,
        "fault_count": fault_count,
        "request_count": len(raw_records),
        "fault_status_count": len(fault_events),
        "api_retry_count": len(retry_events),
        "status_sequence": statuses,
        "attempt_sequence": list(range(1, fault_count + 1)),
        "max_retries": 10,
        "error_status": 500,
        "retry_delay_ms": delays,
        "theoretical_intervals_ms": intervals,
        "actual_request_gap_ms": [round(value, 3) for value in gaps],
        "capture_overhead_ms": [round(value, 3) for value in overhead],
        "request_timestamps_strictly_increasing": True,
        "pre_cap_gaps_strictly_increasing": True,
        "all_gaps_cover_declared_delay": True,
        "stainless_retry_count_values": ["0"] * len(raw_records),
        "final_status": 200,
        "final_result": "S1_OK",
        "success": True,
    }


def _public_report(value: Mapping[str, Any]) -> dict[str, Any]:
    """移除仅用于跨运行校验的动态原值，报告只留可复核摘要。"""
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _find_repository_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    raise EvidenceValidationError(f"无法定位归档所属 Git 仓库：{path}")


def _repository_relative(path: Path, repository_root: Path) -> str:
    resolved = path.resolve()
    _require(path.is_file() and not path.is_symlink(), f"证据文件缺失或为符号链接：{path}")
    try:
        return resolved.relative_to(repository_root).as_posix()
    except ValueError as error:
        raise EvidenceValidationError(f"证据文件不在归档所属仓库内：{path}") from error


def _catalog_entry(
    repository_root: Path,
    run: Mapping[str, Any],
    *,
    include_stdout: bool,
    include_lifecycle: bool,
) -> dict[str, Any]:
    paths = [
        _mapping(run, "run").get("run_dir") / "manifest.json",
        run.get("receipt_path"),
        run.get("post_secret_receipt_path"),
        run.get("invocation_path"),
        run.get("summary_path"),
        run.get("raw_path"),
    ]
    if include_stdout:
        paths.append(run.get("stdout_path"))
    if include_lifecycle:
        paths.append(run.get("lifecycle_path"))
    _require(all(isinstance(path, Path) for path in paths), f"catalog 路径类型错误：{run.get('run_id')}")
    relative_path_pairs = sorted(
        ((_repository_relative(path, repository_root), path) for path in paths),
        key=lambda pair: pair[0],
    )
    relative_paths = [relative for relative, _ in relative_path_pairs]
    _require(len(relative_paths) == len(set(relative_paths)), f"catalog 路径重复：{run.get('run_id')}")
    return {
        "run_id": run.get("run_id"),
        "paths": relative_paths,
        "sha256_by_path": {
            relative: _sha256_file(path)
            for relative, path in relative_path_pairs
        },
    }


def _build_evidence_catalog(
    repository_root: Path,
    categories: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    def entries(category: str, *, stdout: bool = False, lifecycle: bool = False) -> list[dict[str, Any]]:
        return [
            _catalog_entry(
                repository_root,
                run,
                include_stdout=stdout,
                include_lifecycle=lifecycle,
            )
            for run in sorted(categories[category], key=lambda item: item["run_id"])
        ]

    return {
        "HEADER-negative": entries("header-baseline"),
        "HEADER-client": entries("header-client-app"),
        "HEADER-container": entries("header-container"),
        "HEADER-remote": entries("header-remote-session"),
        "HEADER-combo": entries("header-combination", stdout=True),
        "AGENT-depths": {
            scenario: entries(f"agent-{scenario}", stdout=True)
            for scenario in ("a1", "a2", "a3")
        },
        "RETRY-count3": entries("retry-3", stdout=True, lifecycle=True),
        "RETRY-count5": entries("retry-5", stdout=True, lifecycle=True),
        "RETRY-count9": entries("retry-9", stdout=True, lifecycle=True),
    }


def _source_compatibility(categories: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    category_hashes = {
        category: sorted({run["receipt"]["source_bundle_sha256"] for run in runs})
        for category, runs in categories.items()
    }
    uniform_required = set(EXPECTED_CATEGORY_COUNTS) - {"agent-a3"}
    _require(
        all(len(category_hashes[category]) == 1 for category in uniform_required),
        "除 Agent a3 外，同类两轮的采集执行源必须一致",
    )
    header_single_categories = (
        "header-baseline",
        "header-client-app",
        "header-container",
        "header-remote-session",
    )
    header_single_hashes = sorted({
        source_hash
        for category in header_single_categories
        for source_hash in category_hashes[category]
    })
    _require(len(header_single_hashes) == 1, "Header 负例/三个单变量正例的采集执行源不一致")
    retry_hashes = sorted({
        source_hash
        for category in ("retry-3", "retry-5", "retry-9")
        for source_hash in category_hashes[category]
    })
    _require(len(retry_hashes) == 1, "retry 3/5/9 矩阵的采集执行源不一致")

    all_source_maps = [run["receipt"]["source_sha256_by_path"] for runs in categories.values() for run in runs]
    path_sets = {tuple(sorted(source_map)) for source_map in all_source_maps}
    _require(len(path_sets) == 1, "22 轮执行源文件路径集不一致")
    source_paths = list(next(iter(path_sets)))
    global_variant_paths = sorted({
        path for path in source_paths
        if len({source_map[path] for source_map in all_source_maps}) > 1
    })

    a3_maps = [run["receipt"]["source_sha256_by_path"] for run in categories["agent-a3"]]
    a3_variant_paths = sorted({
        path for path in source_paths
        if len({source_map[path] for source_map in a3_maps}) > 1
    })
    _require(a3_variant_paths == ["capturelib/scenarios.py"], "Agent a3 两轮执行源差异不只限于 scenario 驱动")
    a3_pair_uniform = len(category_hashes["agent-a3"]) == 1
    return {
        "category_source_bundle_sha256": category_hashes,
        "header_negative_and_single_positive_source_uniform": True,
        "retry_matrix_source_uniform": True,
        "all_non_a3_pairs_source_uniform": True,
        "agent_a3_pair_source_uniform": a3_pair_uniform,
        "agent_a3_variant_paths": a3_variant_paths,
        "global_variant_paths": global_variant_paths,
        "agent_a3_exact_invocation_and_wire_validated_despite_driver_variant": True,
    }


def analyze_campaign(capture_root: Path | str) -> dict[str, Any]:
    lexical_root = Path(capture_root).absolute()
    _require(
        lexical_root.is_dir() and not lexical_root.is_symlink(),
        f"采集根目录不存在或为符号链接：{lexical_root}",
    )
    root = lexical_root.resolve()
    runs_root = root / "runs"
    receipts_root = root / "runtime-receipts"
    post_secret_receipts_root = root / "post-run-secret-scan-receipts"
    _require(
        runs_root.is_dir()
        and not runs_root.is_symlink()
        and receipts_root.is_dir()
        and not receipts_root.is_symlink()
        and post_secret_receipts_root.is_dir()
        and not post_secret_receipts_root.is_symlink(),
        f"采集根目录结构不完整：{root}",
    )
    run_entries = sorted(runs_root.iterdir())
    _require(all(path.is_dir() and not path.is_symlink() for path in run_entries), "runs/ 只能含非符号链接运行目录")
    run_dirs = run_entries
    _require(len(run_dirs) == 22, f"有效运行数必须为 22，实际为 {len(run_dirs)}")

    categories: dict[str, list[dict[str, Any]]] = {key: [] for key in EXPECTED_CATEGORY_COUNTS}
    campaign_binding: list[dict[str, str]] = []
    receipt_hashes: set[str] = set()
    post_secret_receipt_hashes: set[str] = set()
    post_secret_tool_hashes: set[str] = set()
    nonce_hashes: set[str] = set()
    source_hash_counts: dict[str, int] = {}
    common_image_ids: set[str] = set()
    common_image_refs: set[str] = set()
    common_container_ids: set[str] = set()
    expected_receipt_filenames: set[str] = set()
    expected_post_secret_receipt_filenames: set[str] = set()

    for run_dir in run_dirs:
        run = _validate_manifest(root, run_dir)
        category = _classify_run(run)
        wire = _validate_http_records(run)
        run["wire"] = wire
        categories[category].append(run)
        receipt = _mapping(run.get("receipt"), f"{run_dir.name}.receipt_summary")
        receipt_hashes.add(_string(receipt.get("receipt_sha256"), "receipt_sha256"))
        nonce_hashes.add(_string(receipt.get("run_nonce_sha256"), "run_nonce_sha256"))
        source_hash = _string(receipt.get("source_bundle_sha256"), "source_bundle_sha256")
        source_hash_counts[source_hash] = source_hash_counts.get(source_hash, 0) + 1
        common_image_ids.add(_string(receipt.get("runtime_image_id"), "runtime_image_id"))
        common_image_refs.add(_string(receipt.get("runtime_image_reference"), "runtime_image_reference"))
        common_container_ids.add(_string(receipt.get("container_id"), "container_id"))
        expected_receipt_filenames.add(_string(receipt.get("receipt_filename"), "receipt_filename"))
        post_secret_receipt = _mapping(run.get("post_secret_receipt"), f"{run_dir.name}.post_secret_receipt")
        post_secret_receipt_hashes.add(_string(post_secret_receipt.get("receipt_sha256"), "post_secret_receipt_sha256"))
        post_secret_tool_hashes.add(_string(post_secret_receipt.get("tool_sha256"), "post_secret_tool_sha256"))
        expected_post_secret_receipt_filenames.add(f"{run['run_id']}.json")
        campaign_binding.append({
            "run_id": run["run_id"],
            "category": category,
            "manifest_sha256": run["manifest_sha256"],
            "receipt_sha256": receipt["receipt_sha256"],
            "post_run_secret_receipt_sha256": post_secret_receipt["receipt_sha256"],
            "source_bundle_sha256": source_hash,
        })

    for category, expected_count in EXPECTED_CATEGORY_COUNTS.items():
        _require(len(categories[category]) == expected_count, f"矩阵 {category} 必须恰有 {expected_count} 轮")
    _require(len(receipt_hashes) == len(run_dirs), "host receipt SHA 未逐运行唯一")
    _require(len(post_secret_receipt_hashes) == len(run_dirs), "post-run secret receipt SHA 未逐运行唯一")
    _require(len(post_secret_tool_hashes) == 1, "post-run secret receipt 生成器身份不一致")
    _require(len(nonce_hashes) == len(run_dirs), "host receipt nonce 未逐运行唯一")
    _require(len(common_image_ids) == len(common_image_refs) == len(common_container_ids) == 1, "运行镜像或容器身份未在 22 轮保持一致")
    receipt_entries = sorted(receipts_root.iterdir())
    _require(all(path.is_file() and not path.is_symlink() for path in receipt_entries), "runtime-receipts/ 只能含非符号链接普通文件")
    _require({path.name for path in receipt_entries} == expected_receipt_filenames, "runtime-receipts/ 与 22 个运行未一一对应")
    post_secret_receipt_entries = sorted(post_secret_receipts_root.iterdir())
    _require(
        all(path.is_file() and not path.is_symlink() for path in post_secret_receipt_entries),
        "post-run-secret-scan-receipts/ 只能含非符号链接普通文件",
    )
    _require(
        {path.name for path in post_secret_receipt_entries} == expected_post_secret_receipt_filenames,
        "post-run-secret-scan-receipts/ 与 22 个运行未一一对应",
    )
    all_client_request_ids = [
        request_id
        for runs in categories.values()
        for run in runs
        for request_id in run["wire"]["_client_request_ids"]
    ]
    _require(
        len(all_client_request_ids) == len(set(all_client_request_ids)),
        "x-client-request-id 在跨运行证据中重用",
    )
    source_compatibility = _source_compatibility(categories)

    header_report: dict[str, Any] = {}
    expected_header_projections = {
        "header-baseline": ["x-app", "x-client-request-id"],
        "header-client-app": ["x-app", "x-client-app", "x-client-request-id"],
        "header-container": ["x-app", "x-claude-remote-container-id", "x-client-request-id"],
        "header-remote-session": ["x-app", "x-claude-remote-session-id", "x-client-request-id"],
        "header-combination": list(CONDITIONAL_HEADER_SLOTS),
    }
    for category in (
        "header-baseline",
        "header-client-app",
        "header-container",
        "header-remote-session",
        "header-combination",
    ):
        _require(
            all(
                run["wire"]["observed_conditional_header_projection"]
                == expected_header_projections[category]
                for run in categories[category]
            ),
            f"{category} 未覆盖预期条件 Header 投影",
        )
        if category != "header-combination":
            _require(
                all(run["wire"]["request_count"] == 1 for run in categories[category]),
                f"{category} 每轮必须只有一个主请求",
            )
        header_report[category] = [
            {"run_id": run["run_id"], **_public_report(run["wire"])}
            for run in categories[category]
        ]

    agent_report_internal: dict[str, list[dict[str, Any]]] = {}
    for scenario in ("a1", "a2", "a3"):
        agent_report_internal[scenario] = [_analyze_agent_run(run) for run in categories[f"agent-{scenario}"]]
    combination_agent_internal = [_analyze_agent_run(run) for run in categories["header-combination"]]
    all_agent_analyses = [
        report
        for scenario in ("a1", "a2", "a3")
        for report in agent_report_internal[scenario]
    ] + combination_agent_internal
    all_agent_ids = [dynamic_id for report in all_agent_analyses for dynamic_id in report["_agent_ids"]]
    all_tool_ids = [dynamic_id for report in all_agent_analyses for dynamic_id in report["_tool_ids"]]
    dynamic_ids_globally_unique = (
        len(all_agent_ids) == len(set(all_agent_ids))
        and len(all_tool_ids) == len(set(all_tool_ids))
        and set(all_agent_ids).isdisjoint(all_tool_ids)
    )
    _require(dynamic_ids_globally_unique, "Agent/tool 动态 ID 在独立运行之间重用")
    two_independent_runs_per_depth = all(
        len(agent_report_internal[scenario]) == 2
        for scenario in ("a1", "a2", "a3")
    )
    _require(two_independent_runs_per_depth, "Agent a1/a2/a3 未各有两轮独立运行")
    pair_sessions_distinct = all(
        len({run["wire"]["_http_session_id"] for run in categories[f"agent-{scenario}"]}) == 2
        for scenario in ("a1", "a2", "a3")
    )
    _require(pair_sessions_distinct, "Agent 同深度两轮未体现独立 session")
    agent_report = {
        scenario: [_public_report(report) for report in agent_report_internal[scenario]]
        for scenario in ("a1", "a2", "a3")
    }
    combination_agent_report = [_public_report(report) for report in combination_agent_internal]

    retry_report: dict[str, Any] = {}
    all_overhead: list[float] = []
    for fault_count in (3, 5, 9):
        reports = [_analyze_retry_run(run, fault_count) for run in categories[f"retry-{fault_count}"]]
        retry_report[str(fault_count)] = reports
        for report in reports:
            all_overhead.extend(report["capture_overhead_ms"])

    campaign_binding.sort(key=lambda item: item["run_id"])
    repository_root = _find_repository_root(root)
    evidence_catalog = _build_evidence_catalog(repository_root, categories)
    analyzer_path = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "passed": True,
        "campaign_name": root.name,
        "analyzer_identity": {
            "path": _repository_relative(analyzer_path, repository_root),
            "sha256": _sha256_file(analyzer_path),
        },
        "campaign_binding_sha256": _canonical_sha256(campaign_binding),
        "run_bindings": campaign_binding,
        "evidence_catalog": evidence_catalog,
        "run_count": len(run_dirs),
        "matrix_counts": {key: len(categories[key]) for key in EXPECTED_CATEGORY_COUNTS},
        "integrity": {
            "complete_m_runs": len(run_dirs),
            "manifest_secret_scan_self_report_consistent_runs": len(run_dirs),
            "post_run_secret_scan_verified_runs": len(run_dirs),
            "exact_secret_scan_passed_runs": len(run_dirs),
            "host_receipt_sha_verified_runs": len(run_dirs),
            "artifact_inventory_verified_runs": len(run_dirs),
            "unique_receipts": len(receipt_hashes),
            "unique_post_run_secret_receipts": len(post_secret_receipt_hashes),
            "unique_run_nonces": len(nonce_hashes),
            "globally_unique_client_request_id_count": len(all_client_request_ids),
            "post_run_secret_scan_tool_sha256": next(iter(post_secret_tool_hashes)),
            "source_bundle_sha256_counts": dict(sorted(source_hash_counts.items())),
            "source_compatibility": source_compatibility,
            "common_runtime_image_id": next(iter(common_image_ids)),
            "common_runtime_image_reference": next(iter(common_image_refs)),
            "common_container_id": next(iter(common_container_ids)),
        },
        "headers": {
            "profiles": header_report,
            "negative_baseline_run_count": len(categories["header-baseline"]),
            "single_variable_positive_run_count_per_header": min(
                len(categories[category])
                for category in (
                    "header-client-app",
                    "header-container",
                    "header-remote-session",
                )
            ),
            "combination_run_count": len(categories["header-combination"]),
            "canonical_seven_field_projection": list(CONDITIONAL_HEADER_SLOTS),
            "ua_linkage_verified": True,
        },
        "agents": {
            "independent_runs": agent_report,
            "combination_a2_runs": combination_agent_report,
            "two_independent_runs_per_depth": two_independent_runs_per_depth,
            "independent_session_per_depth_pair": pair_sessions_distinct,
            "dynamic_ids_discovered_not_pinned": dynamic_ids_globally_unique,
        },
        "retries": {
            "theory": {
                "base_delay_ms": 500,
                "multiplier": 2,
                "base_cap_ms": 32000,
                "jitter_factor_interval": [1.0, 1.25],
                "formula": "base=min(500*2^(attempt-1),32000); delay in [base,1.25*base]",
                "max_retries_observed_in_events": 10,
                "capture_overhead_bound_ms": [0, MAX_RETRY_OVERHEAD_MS],
            },
            "fault_counts": retry_report,
            "overall_capture_overhead_ms": {
                "minimum": min(all_overhead),
                "maximum": max(all_overhead),
            },
            "two_runs_per_fault_count": all(
                len(retry_report[str(fault_count)]) == 2
                for fault_count in (3, 5, 9)
            ),
            "at_least_nine_retries_observed": max(
                report["api_retry_count"]
                for reports in retry_report.values()
                for report in reports
            ) >= 9,
            "final_200_and_success_in_all_runs": all(
                report["final_status"] == 200 and report["success"] is True
                for reports in retry_report.values()
                for report in reports
            ),
        },
    }


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="严格复核 Claude Code 2.1.220 待补证采集矩阵")
    parser.add_argument("capture_root", type=Path, help="含 runs/ 与 runtime-receipts/ 的归档根目录")
    parser.add_argument("--output", type=Path, help="JSON 报告输出路径；省略时写 stdout")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report = analyze_campaign(args.capture_root)
    except (ValueError, OSError, KeyError, TypeError) as error:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "passed": False,
            "error_type": type(error).__name__,
            "error": str(error),
        }
        _write_report(args.output, failure)
        return 1
    _write_report(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
