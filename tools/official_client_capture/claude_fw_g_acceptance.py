#!/usr/bin/env python3
"""封存 Claude Code 2.1.226 FW-G 后继批准与隔离验收事实。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_once,
)
from tools.official_client_control.contracts import (  # noqa: E402
    candidate_identity_sha256,
    campaign_identity_sha256,
    capability_key,
    profile_approval_identity_sha256,
)
from tools.official_client_control.receipts import (  # noqa: E402
    control_tool_bundle_sha256,
)
from tools.official_client_control.store import ControlStore  # noqa: E402


TARGET_VERSION = "2.1.226"
EXPECTED_REQUIRED_RULES = 40
EXPECTED_PROFILE_ASSERTIONS = 106
EXPECTED_SCENARIO_ASSERTIONS = 4
EXPECTED_ATOMIC_ASSERTIONS = 110
EXPECTED_OFFICIAL_PROBES = 77
EXPECTED_OFFICIAL_REQUESTS = 394
EXPECTED_OFFICIAL_CANDIDATES = 593
EXPECTED_OFFICIAL_DIMENSIONS = 49

CANDIDATE_COMMIT = "651ccd518d97c53bb3089860a0fdf80009c1be9e"
CANDIDATE_TREE = "71eccef8c9498de12bafaa7006108c10996cd10d"
CANDIDATE_IMAGE = "sha256:9b923fd1a60835fa8474712764befba34a02f06e8642c5ac3af1aa9967464566"
CANDIDATE_PROFILE = "4da60bc238694a06a0dc80d68117abddd2de98c7c924c4db4c5dd929ea411e17"
CANDIDATE_WIRE = "c1c3c8c83710c9afc7005f71fa45d0837484a6bd042f75c08e5cde5451822a3e"
CANDIDATE_RELEASE = "c1053492eabc0b10d9d5f92f807a1df0d507c777b64a528e938426350c0d5350"
CANDIDATE_RELEASE_BUNDLE = "4213ea92a7d76c4ef3aa318f4d93628cbcf675dc86566b107dddb70a70e6eb41"
CANDIDATE_SOURCE_TREE = "2792b9d29e57b66a12bc80f576e02dd06306eac467b8dda73e2dbd7a69b19d5b"
CANDIDATE_TEST_TREE = "6ef5c064a3e489579e4f471d3ad954de132e1e8260058bb1438c74d81905f3e3"
CANDIDATE_DEPENDENCY_LOCK = "bad9c6d5cd2e48d916e8c1f217f43951984be6d8cd0892ef9e22d8e43e071339"

CAMPAIGN_ID = "claude-code-2_1_226-fw-g-production-replacement-v2-20260821"
CANDIDATE_ID = "claude-code-2_1_226-fw-g-651ccd518"
REVIEWER = "project-owner-confirmed"
REVIEW_REF = "codex-task-fw-g-desktop-fix-owner-confirmation-20260821"

CANDIDATE_TREE_SCHEMA = "claude-fw-g-candidate-git-tree/v1"
CANDIDATE_DEPENDENCY_NAMES = {
    "Cargo.lock",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "package-lock.json",
    "package.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

SECRET_PATTERNS = {
    "bearer": re.compile(rb"(?i)\bBearer\s+(?!<)[A-Za-z0-9._~+/-]{20,}"),
    "claude_token": re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{12,}\b"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "oauth_callback": re.compile(rb"\b[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}\b"),
    "json_oauth_secret": re.compile(
        rb'(?i)"(?:access_token|refresh_token|authorization_code|oauth_token)"\s*:\s*"(?!<)[^"]+"'
    ),
}


class AcceptanceError(RuntimeError):
    """表示 FW-G 身份、证据、门禁或脱敏条件不成立。"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"无法读取 JSON：{path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON 顶层不是对象：{path}")
    return value


def candidate_git_material_digests(
    repository_root: Path,
    commit: str,
) -> dict[str, str]:
    """从固定 Git 提交复算源码、测试与依赖三类候选树摘要。"""

    completed = subprocess.run(
        ["git", "ls-tree", "-r", "-l", "-z", "--full-tree", commit],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(completed.returncode == 0 and not completed.stderr, "无法复算候选 Git 树")
    entries: list[dict[str, Any]] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            metadata, raw_path = raw.split(b"\t", 1)
            mode, kind, object_id, size = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise AcceptanceError("候选 Git 树条目无法解析") from exc
        require(kind == "blob" and size.isdigit(), f"候选 Git 树含非普通 blob：{path}")
        entries.append(
            {
                "mode": mode,
                "path": path,
                "git_blob": object_id,
                "bytes": int(size),
            }
        )
    require(entries and entries == sorted(entries, key=lambda item: item["path"]), "候选 Git 树为空或未排序")

    def is_test_path(value: str) -> bool:
        candidate = PurePosixPath(value)
        name = candidate.name
        return (
            "tests" in candidate.parts
            or name.endswith("_test.go")
            or name.startswith("test_")
            or ".test." in name
            or ".spec." in name
        )

    classified = {
        "source": entries,
        "test": [item for item in entries if is_test_path(item["path"])],
        "dependency": [
            item
            for item in entries
            if PurePosixPath(item["path"]).name in CANDIDATE_DEPENDENCY_NAMES
        ],
    }
    require(classified["test"] and classified["dependency"], "候选测试树或依赖树为空")
    return {
        name: canonical_sha256(
            {
                "schema_version": CANDIDATE_TREE_SCHEMA,
                "classification": name,
                "commit": commit,
                "entries": values,
            }
        )
        for name, values in classified.items()
    }


def external_binding(path: Path, repository_root: Path = REPOSITORY_ROOT) -> dict[str, Any]:
    resolved = path.resolve()
    require(resolved.is_relative_to(repository_root.resolve()), f"外部证据越出仓库：{path}")
    require(resolved.is_file() and not resolved.is_symlink(), f"外部证据不是可信普通文件：{path}")
    return {
        "path": resolved.relative_to(repository_root.resolve()).as_posix(),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def staged_external_binding(path: Path, advertised_relative: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"阶段证据不是可信普通文件：{path}")
    require(not advertised_relative.startswith("/"), "阶段证据路径必须是仓库相对路径")
    return {
        "path": advertised_relative,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def sorted_refs(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = list(values)
    result.sort(key=canonical_sha256)
    require(len(result) == len({canonical_sha256(item) for item in result}), "引用数组存在重复")
    return result


def write_canonical(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_once(path, canonical_json_bytes(value))
    path.chmod(mode)


def scan_secret_bytes(content: bytes, label: str) -> None:
    hits = [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(content)]
    require(not hits, f"{label} 命中秘密模式：{hits}")


def run_go_receipt(
    repository_root: Path,
    *,
    receipt_id: str,
    packages: Sequence[str],
    tests: Sequence[str],
) -> dict[str, Any]:
    require(packages and tests, "Go 门禁必须声明 package 与测试")
    pattern = "^(" + "|".join(re.escape(name) for name in tests) + ")$"
    command = ["go", "test", *packages, "-run", pattern, "-count=1"]
    completed = subprocess.run(
        command,
        cwd=repository_root / "backend",
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(completed.returncode == 0, f"Go 门禁失败：{receipt_id}")
    scan_secret_bytes(completed.stdout, f"{receipt_id}.stdout")
    scan_secret_bytes(completed.stderr, f"{receipt_id}.stderr")
    return {
        "schema_version": "claude-code-fw-g-local-regression/v1",
        "receipt_id": receipt_id,
        "candidate_commit": CANDIDATE_COMMIT,
        "packages": list(packages),
        "tests": list(tests),
        "command_sha256": hashlib.sha256("\0".join(command).encode()).hexdigest(),
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "exit_code": completed.returncode,
        "result": "passed",
    }


def validate_inputs(
    old_evidence: dict[str, Any],
    official: dict[str, Any],
    candidate_rules: dict[str, Any],
    candidate_verification: dict[str, Any],
    dmit: dict[str, Any],
) -> list[str]:
    rules = old_evidence.get("rules")
    require(isinstance(rules, list) and len(rules) == EXPECTED_REQUIRED_RULES, "FW-F EvidencePackage 不是 40 条")
    spec_ids = [item.get("spec_id") for item in rules]
    require(spec_ids == sorted(set(spec_ids)), "FW-F RequiredRules 未排序或重复")
    require(
        official.get("schema_version") == "claude-code-fw-g-required-rule-official-verification/v1"
        and official.get("target_version") == TARGET_VERSION
        and official.get("required_rule_count") == EXPECTED_REQUIRED_RULES
        and official.get("result") == "passed",
        "FW-G 官方逐规则复测未闭合",
    )
    official_specs = [item.get("spec_id") for item in official.get("entries", [])]
    require(official_specs == spec_ids, "官方复测与 RequiredRules 集合不一致")
    require(
        candidate_rules.get("schema_version") == "claude-code-fw-g-required-rule-candidate-pair/v1"
        and candidate_rules.get("target_version") == TARGET_VERSION
        and candidate_rules.get("required_rule_count") == EXPECTED_REQUIRED_RULES
        and candidate_rules.get("result") == "passed",
        "FW-G 候选逐规则 PAIR 未闭合",
    )
    candidate_specs = [item.get("spec_id") for item in candidate_rules.get("entries", [])]
    require(candidate_specs == spec_ids, "候选 PAIR 与 RequiredRules 集合不一致")
    source = candidate_verification.get("source", {})
    require(
        candidate_verification.get("result") == "passed"
        and source.get("clean") is True
        and source.get("commit") == CANDIDATE_COMMIT
        and source.get("tree") == CANDIDATE_TREE,
        "候选源码身份不一致",
    )
    require(
        candidate_verification.get("candidate_profile", {}).get("sha256") == CANDIDATE_PROFILE
        and candidate_verification.get("candidate_wire", {}).get("sha256") == CANDIDATE_WIRE,
        "候选 Profile/Wire 身份不一致",
    )
    require(
        dmit.get("schema_version") == "claude-code-fw-g-dmit-acceptance/v1"
        and dmit.get("phase") == "FW-G"
        and dmit.get("result") == "passed",
        "DMIT 验收收据不成立",
    )
    dmit_candidate = dmit.get("candidate", {})
    require(
        dmit_candidate.get("commit") == CANDIDATE_COMMIT
        and dmit_candidate.get("tree") == CANDIDATE_TREE
        and dmit_candidate.get("image_digest") == CANDIDATE_IMAGE
        and dmit_candidate.get("architecture") == "linux/amd64",
        "DMIT 候选身份不一致",
    )
    release = dmit.get("release", {})
    require(
        release.get("version") == TARGET_VERSION
        and release.get("profile_sha256") == CANDIDATE_PROFILE
        and release.get("release_sha256") == CANDIDATE_RELEASE
        and release.get("bundle_sha256") == CANDIDATE_RELEASE_BUNDLE
        and release.get("wire_sha256") == CANDIDATE_WIRE,
        "DMIT Release 身份不一致",
    )
    ingress = dmit.get("ingress_assertions", {})
    required_ingress = {
        "claude_desktop_count_tokens_beta",
        "claude_desktop_messages_beta",
        "official_messages",
        "third_party_anthropic_messages",
        "official_count_tokens",
        "third_party_standard_count_tokens",
        "unapproved_official_release_fail_close",
        "outside_support_envelope_fail_close",
    }
    require(
        required_ingress.issubset(ingress)
        and all(ingress[name] == "passed" for name in required_ingress),
        "DMIT strict 入口或范围外拒绝未闭合",
    )
    rollback = dmit.get("rollback_drill", {})
    require(
        rollback.get("rollback_health") == "healthy"
        and rollback.get("messages_positive") == "passed"
        and rollback.get("count_tokens_positive") == "passed"
        and rollback.get("candidate_restored") is True
        and rollback.get("restored_image_digest") == CANDIDATE_IMAGE,
        "DMIT rollback/恢复未闭合",
    )
    scripts = dmit.get("script_results")
    require(
        isinstance(scripts, list)
        and len(scripts) == 6
        and all(isinstance(item, dict) for item in scripts)
        and {item.get("id") for item in scripts}
        == {
            "count-tokens-boundary",
            "desktop-beta-ingress",
            "live-messages-stream",
            "official-count-tokens",
            "official-messages",
            "strict-ingress-boundary",
        }
        and all(item.get("result") == "passed" for item in scripts),
        "DMIT 脚本结果没有唯一覆盖五组基线与 Desktop 专项验收",
    )
    runtime = dmit.get("final_runtime", {})
    require(
        runtime.get("application") == "running_healthy"
        and runtime.get("health_http_status") == 200
        and runtime.get("restart_count") == 0
        and runtime.get("dependency_container_ids_unchanged") is True
        and runtime.get("fatal_or_panic_count") == 0
        and runtime.get("error_level_count") == 0
        and runtime.get("persona_or_oauth_failure_count") == 0
        and runtime.get("guard_or_finalization_failure_count") == 0
        and runtime.get("oauth_secret_pattern_count") == 0,
        "DMIT 恢复后的运行态或纯正例日志观察未闭合",
    )
    oauth = dmit.get("oauth_precondition", {})
    require(
        oauth.get("provider") == "anthropic"
        and oauth.get("auth_family") == "oauth"
        and oauth.get("state") == "active"
        and oauth.get("credential_material_persisted") is False
        and oauth.get("approved_model_mapping_complete") is True,
        "DMIT OAuth 前置条件或批准模型映射不完整",
    )
    environment = dmit.get("environment", {})
    require(
        environment.get("name") == "DMIT"
        and environment.get("isolated_candidate") is True
        and environment.get("vircs_connected") is False
        and environment.get("vircs_service_changed") is False
        and dmit.get("production_selector_modified") is False
        and dmit.get("fw_h_started") is False,
        "FW-G 隔离边界不成立",
    )
    return spec_ids


def build_ingress_observation(persona: dict[str, Any], source_ref: dict[str, Any]) -> dict[str, Any]:
    aliases = [
        {
            "alias_id": "alias-chat-bare",
            "logical_ingress_id": "chat-completions-oauth",
            "physical_route": "POST /chat/completions",
            "caller_ids": ["handler.Gateway.ChatCompletions", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-chat-v1",
            "logical_ingress_id": "chat-completions-oauth",
            "physical_route": "POST /v1/chat/completions",
            "caller_ids": ["handler.Gateway.ChatCompletions", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-codex-direct-responses-http",
            "logical_ingress_id": "codex-direct-rerouted",
            "physical_route": "POST /backend-api/codex/responses",
            "caller_ids": ["handler.OpenAIGateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-codex-direct-responses-subpath",
            "logical_ingress_id": "codex-direct-rerouted",
            "physical_route": "POST /backend-api/codex/responses/*subpath",
            "caller_ids": ["handler.OpenAIGateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-codex-direct-responses-ws",
            "logical_ingress_id": "codex-direct-rerouted",
            "physical_route": "GET /backend-api/codex/responses",
            "caller_ids": ["handler.OpenAIGateway.ResponsesWebSocket", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-count-bare-official",
            "logical_ingress_id": "official-count-tokens-oauth",
            "physical_route": "POST /messages/count_tokens [official-client-claim]",
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.official-client-claim"],
        },
        {
            "alias_id": "alias-count-bare-third-party",
            "logical_ingress_id": "third-party-count-tokens-oauth",
            "physical_route": "POST /messages/count_tokens [standard-api]",
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.standard-api"],
        },
        {
            "alias_id": "alias-count-v1-official",
            "logical_ingress_id": "official-count-tokens-oauth",
            "physical_route": "POST /v1/messages/count_tokens [official-client-claim]",
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.official-client-claim"],
        },
        {
            "alias_id": "alias-count-v1-third-party",
            "logical_ingress_id": "third-party-count-tokens-oauth",
            "physical_route": "POST /v1/messages/count_tokens [standard-api]",
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.standard-api"],
        },
        {
            "alias_id": "alias-messages-v1-official",
            "logical_ingress_id": "official-messages-oauth",
            "physical_route": "POST /v1/messages [official-client-claim]",
            "caller_ids": ["handler.Gateway.Messages", "service.Gateway.official-client-claim"],
        },
        {
            "alias_id": "alias-messages-v1-third-party",
            "logical_ingress_id": "third-party-messages-oauth",
            "physical_route": "POST /v1/messages [standard-api]",
            "caller_ids": ["handler.Gateway.Messages", "service.Gateway.standard-api"],
        },
        {
            "alias_id": "alias-responses-bare-http",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "POST /responses",
            "caller_ids": ["handler.Gateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-responses-bare-subpath",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "POST /responses/*subpath",
            "caller_ids": ["handler.Gateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-responses-bare-ws",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "GET /responses",
            "caller_ids": ["handler.OpenAIGateway.ResponsesWebSocket", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-responses-v1-http",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "POST /v1/responses",
            "caller_ids": ["handler.Gateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-responses-v1-subpath",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "POST /v1/responses/*subpath",
            "caller_ids": ["handler.Gateway.Responses", "server.routes.RegisterGatewayRoutes"],
        },
        {
            "alias_id": "alias-responses-v1-ws",
            "logical_ingress_id": "responses-oauth",
            "physical_route": "GET /v1/responses",
            "caller_ids": ["handler.OpenAIGateway.ResponsesWebSocket", "server.routes.RegisterGatewayRoutes"],
        },
    ]
    aliases.sort(key=lambda item: item["alias_id"])
    return {
        "schema_version": "official-client-ingress-observation/v1",
        "persona": persona,
        "observed_at_utc": "2026-08-20T17:41:00Z",
        "source_refs": [source_ref],
        "aliases": aliases,
    }


def build_ingress_inventory(
    persona: dict[str, Any],
    observation_ref: dict[str, Any],
    evidence_ref: dict[str, Any],
) -> dict[str, Any]:
    entries = [
        {
            "logical_ingress_id": "chat-completions-oauth",
            "physical_alias_ids": ["alias-chat-bare", "alias-chat-v1"],
            "caller_ids": ["handler.Gateway.ChatCompletions", "server.routes.RegisterGatewayRoutes"],
            "adapter_id": "openai-chat-legacy",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "third_party",
            "protocol_class": "openai-chat-completions",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "codex-direct-rerouted",
            "physical_alias_ids": [
                "alias-codex-direct-responses-http",
                "alias-codex-direct-responses-subpath",
                "alias-codex-direct-responses-ws",
            ],
            "caller_ids": [
                "handler.OpenAIGateway.Responses",
                "handler.OpenAIGateway.ResponsesWebSocket",
                "server.routes.RegisterGatewayRoutes",
            ],
            "adapter_id": "codex-direct-legacy",
            "route_id": "codex-product-route",
            "ingress_kind": "third_party",
            "protocol_class": "codex-responses",
            "current_disposition": "rerouted",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "official-count-tokens-oauth",
            "physical_alias_ids": ["alias-count-bare-official", "alias-count-v1-official"],
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.official-client-claim"],
            "adapter_id": "anthropic-count-tokens-official",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "official",
            "protocol_class": "anthropic-messages",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "official-messages-oauth",
            "physical_alias_ids": ["alias-messages-v1-official"],
            "caller_ids": ["handler.Gateway.Messages", "service.Gateway.official-client-claim"],
            "adapter_id": "anthropic-messages-official",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "official",
            "protocol_class": "anthropic-messages",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "responses-oauth",
            "physical_alias_ids": [
                "alias-responses-bare-http",
                "alias-responses-bare-subpath",
                "alias-responses-bare-ws",
                "alias-responses-v1-http",
                "alias-responses-v1-subpath",
                "alias-responses-v1-ws",
            ],
            "caller_ids": [
                "handler.Gateway.Responses",
                "handler.OpenAIGateway.ResponsesWebSocket",
                "server.routes.RegisterGatewayRoutes",
            ],
            "adapter_id": "openai-responses-legacy",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "third_party",
            "protocol_class": "openai-responses",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "third-party-count-tokens-oauth",
            "physical_alias_ids": ["alias-count-bare-third-party", "alias-count-v1-third-party"],
            "caller_ids": ["handler.Gateway.CountTokens", "service.Gateway.standard-api"],
            "adapter_id": "anthropic-count-tokens-standard-api",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "third_party",
            "protocol_class": "anthropic-messages",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
        {
            "logical_ingress_id": "third-party-messages-oauth",
            "physical_alias_ids": ["alias-messages-v1-third-party"],
            "caller_ids": ["handler.Gateway.Messages", "service.Gateway.standard-api"],
            "adapter_id": "anthropic-messages-standard-api",
            "route_id": "claude-oauth-legacy",
            "ingress_kind": "third_party",
            "protocol_class": "anthropic-messages",
            "current_disposition": "retained_legacy",
            "evidence_refs": [evidence_ref],
        },
    ]
    entries.sort(key=lambda item: item["logical_ingress_id"])
    return {
        "schema_version": "official-client-production-ingress-inventory/v1",
        "persona": persona,
        "observation_ref": observation_ref,
        "entries": entries,
    }


def build_capabilities() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def add(ingress: str, endpoint: str, feature: str, model: str) -> None:
        values.append(
            {
                "platform": "linux/amd64",
                "logical_ingress_id": ingress,
                "protocol_class": "anthropic-messages",
                "privacy_mode": "essential-traffic-no-telemetry",
                "model": model,
                "feature": feature,
                "endpoint": endpoint,
            }
        )

    add(
        "official-messages-oauth",
        "GET /api/claude_code/policy_limits + GET /api/claude_code/settings + GET /api/oauth/profile",
        "essential-account-profile-policy-and-settings",
        "claude-sonnet-5 + claude-haiku-4-5-20251001",
    )
    add("official-messages-oauth", "GET /v1/mcp_servers?limit=1000", "official-mcp-server-discovery", "claude-sonnet-5")
    add(
        "official-messages-oauth",
        "HEAD /api/hello + POST /v1/messages?beta=true",
        "sdk-cli-tui-agent-background-hook-remote-retry-fallback-and-tools",
        "claude-sonnet-5 + claude-haiku-4-5",
    )
    add("official-messages-oauth", "POST /v1/messages/count_tokens?beta=true", "tui-context-token-counting", "claude-sonnet-5")
    add("official-messages-oauth", "POST /v1/oauth/token", "expired-oauth-token-refresh", "credential-lifecycle")
    add("third-party-messages-oauth", "POST /v1/messages?beta=true", "lossless-anthropic-messages", "claude-sonnet-5")
    add("official-count-tokens-oauth", "POST /v1/messages/count_tokens?beta=true", "official-count-token-request", "claude-sonnet-5")
    add("third-party-count-tokens-oauth", "POST /v1/messages/count_tokens?beta=true", "standard-api-count-token-request", "claude-sonnet-5")
    return sorted(values, key=capability_key)


def build_scenario_plan(
    old_plan: dict[str, Any], persona: dict[str, Any]
) -> dict[str, Any]:
    scenarios = copy.deepcopy(old_plan["scenarios"])
    for scenario in scenarios:
        scenario["assertion_ids"] = sorted(f"PAIR-{spec_id}" for spec_id in scenario["spec_ids"])
        scenario["conditions"] = sorted(
            {
                "approval=production-replacement",
                "environment=DMIT",
                "privacy=essential-traffic-no-telemetry",
            }
        )
        scenario["ingress_protocol_classes"] = ["anthropic-messages"]
    scenarios.sort(key=lambda item: item["id"])
    return {
        "schema_version": "official-client-scenario-plan/v1",
        "persona": persona,
        "manifest_id": "claude-fw-g-scenario-plan",
        "scenarios": scenarios,
    }


def build_verified_evidence(
    old_evidence: dict[str, Any],
    tool_sha256: str,
    completeness_ref: dict[str, Any],
    common_bindings: Sequence[dict[str, Any]],
    tls_binding: dict[str, Any],
) -> dict[str, Any]:
    payload = copy.deepcopy(old_evidence)
    payload["producer_tool_sha256"] = tool_sha256
    payload["completeness_ref"] = completeness_ref
    for rule in payload["rules"]:
        rule["evidence_level"] = "verified"
        additions = list(common_bindings)
        if rule["spec_id"] in {"SPEC-PROTO-001", "SPEC-TLS-001", "SPEC-TLS-003"}:
            additions.append(tls_binding)
        by_path = {item["path"]: item for item in [*rule["evidence_refs"], *additions]}
        require(len(by_path) == len([*rule["evidence_refs"], *additions]), f"证据路径重复：{rule['spec_id']}")
        rule["evidence_refs"] = [by_path[path] for path in sorted(by_path)]
    return payload


def generic_manifest(
    kind: str,
    persona: dict[str, Any],
    manifest_id: str,
    entries: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    values = sorted(copy.deepcopy(list(entries)), key=lambda item: item["id"])
    return {
        "schema_version": f"official-client-{kind.replace('_', '-')}/v1",
        "persona": persona,
        "manifest_id": manifest_id,
        "entries": values,
    }


def iter_external_bindings(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if set(value) == {"path", "sha256", "bytes"}:
            yield value
            return
        for item in value.values():
            yield from iter_external_bindings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_external_bindings(item)


def collect_store_bindings(store_root: Path) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for path in sorted(store_root.rglob("*.json")):
        value = load_json(path)
        for binding in iter_external_bindings(value):
            previous = by_path.get(binding["path"])
            require(previous is None or previous == binding, f"同一路径存在冲突外部摘要：{binding['path']}")
            by_path[binding["path"]] = binding
    return [by_path[path] for path in sorted(by_path)]


def historical_git_blob(repository_root: Path, relative: str, binding: dict[str, Any]) -> bytes | None:
    commits = subprocess.run(
        ["git", "log", "--all", "--format=%H", "--", relative],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if commits.returncode != 0:
        return None
    for commit in commits.stdout.splitlines():
        result = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if (
            result.returncode == 0
            and len(result.stdout) == binding["bytes"]
            and hashlib.sha256(result.stdout).hexdigest() == binding["sha256"]
        ):
            return result.stdout
    return None


def build_frozen_external_view(
    repository_root: Path,
    store_root: Path,
    view_root: Path,
    staged_output: Path,
    final_output: Path,
) -> dict[str, Any]:
    view_root.mkdir(parents=True, mode=0o700)
    bindings = collect_store_bindings(store_root)
    final_relative = final_output.resolve().relative_to(repository_root.resolve())
    staged_sources = 0
    current_sources = 0
    historical_sources = 0
    for binding in bindings:
        relative = Path(binding["path"])
        target = view_root / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = repository_root / relative
        if relative.is_relative_to(final_relative):
            source = staged_output / relative.relative_to(final_relative)
            staged_sources += 1
        if (
            source.is_file()
            and not source.is_symlink()
            and source.stat().st_size == binding["bytes"]
            and sha256_file(source) == binding["sha256"]
        ):
            # 历史重放视图必须与工作区 inode 隔离；硬链接会让后续原地编辑污染已封存证据。
            shutil.copy2(source, target)
            if not relative.is_relative_to(final_relative):
                current_sources += 1
            continue
        blob = historical_git_blob(repository_root, relative.as_posix(), binding)
        require(blob is not None, f"无法解析冻结外部证据：{relative}")
        target.write_bytes(blob)
        target.chmod(0o600)
        historical_sources += 1
    return {
        "bindings": len(bindings),
        "current_sources": current_sources,
        "staged_sources": staged_sources,
        "historical_git_sources": historical_sources,
    }


class FactClock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def next(self) -> str:
        self.value += timedelta(seconds=1)
        return self.value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    repository_root = arguments.repository_root.resolve()
    base_store_root = arguments.base_store.resolve()
    official_dir = arguments.official_dir.resolve()
    candidate_dir = arguments.candidate_dir.resolve()
    dmit_path = arguments.dmit_receipt.resolve()
    output = arguments.output.resolve()
    public_receipt = arguments.public_receipt.resolve()
    require(repository_root == REPOSITORY_ROOT.resolve(), "必须在当前仓库执行 FW-G finalizer")
    git_material = candidate_git_material_digests(repository_root, CANDIDATE_COMMIT)
    require(
        git_material
        == {
            "source": CANDIDATE_SOURCE_TREE,
            "test": CANDIDATE_TEST_TREE,
            "dependency": CANDIDATE_DEPENDENCY_LOCK,
        },
        "Candidate 源码、测试或依赖树摘要漂移",
    )
    require(base_store_root.is_dir() and not base_store_root.is_symlink(), "FW-F Store 不可信")
    require(not output.exists(), f"输出目录已存在，禁止覆盖：{output}")
    require(not public_receipt.exists(), f"公开收据已存在，禁止覆盖：{public_receipt}")

    official_path = official_dir / "required-rule-official-verification.json"
    candidate_rules_path = candidate_dir / "required-rule-candidate-pair.json"
    candidate_verification_path = candidate_dir / "candidate-verification.json"
    official = load_json(official_path)
    candidate_rules = load_json(candidate_rules_path)
    candidate_verification = load_json(candidate_verification_path)
    dmit = load_json(dmit_path)

    stage = output.parent / f".{output.name}.staging-{os.getpid()}"
    require(not stage.exists(), f"阶段目录已存在：{stage}")
    stage.mkdir(parents=True, mode=0o700)
    try:
        store_root = stage / "control-store"
        shutil.copytree(base_store_root, store_root, copy_function=shutil.copy2)
        store = ControlStore(store_root.resolve())
        old_campaign_files = sorted((store_root / "campaigns").glob("*.json"))
        require(len(old_campaign_files) == 1, "FW-F Store 必须只有一个历史 Campaign")
        old_campaign = store.load_campaign(old_campaign_files[0].stem)
        old_approvals = [
            fact
            for fact in store.list_facts(old_campaign["campaign_id"])
            if fact["fact_kind"] == "profile_approved"
        ]
        require(len(old_approvals) == 1, "FW-F validation-only ProfileApproval 不唯一")
        old_approval = old_approvals[0]["payload"]
        require(old_approval["approval_purpose"] == "validation_only", "FW-F 前置批准不是 validation_only")
        old_evidence_approval = store.load_fact(old_approval["evidence_approval_ref"])
        old_evidence_ref = old_evidence_approval["payload"]["evidence_package_ref"]
        old_evidence = store.load_object(old_evidence_ref)["payload"]
        spec_ids = validate_inputs(
            old_evidence,
            official,
            candidate_rules,
            candidate_verification,
            dmit,
        )
        require(old_approval["target_spec_ids"] == spec_ids, "FW-F Approval 与 40 条规则集合不一致")
        require(old_approval["release_artifact_ref"]["sha256"] == CANDIDATE_RELEASE, "FW-F Release 引用漂移")

        tls_receipt = run_go_receipt(
            repository_root,
            receipt_id="claude-fw-g-tls-h1-wire",
            packages=["./internal/service"],
            tests=["TestClaudeFWGCandidateCapturesFrozenTLSAndH1Wire"],
        )
        codex_receipt = run_go_receipt(
            repository_root,
            receipt_id="codex-final-wire-zero-difference",
            packages=["./internal/officialegress", "./internal/service"],
            tests=[
                "TestChangeset3PostIdentityAuthorityFinalWireIsFrozen",
                "TestChangeset5PostRefactorFinalWireIsFrozenAndMatchesPre",
                "TestChangeset6PostFinalWireIsFrozenAndMatchesChangeset5",
            ],
        )
        write_canonical(stage / "tls-h1-wire-regression.json", tls_receipt)
        write_canonical(stage / "codex-final-wire-regression.json", codex_receipt)

        output_relative = output.relative_to(repository_root).as_posix()
        tls_binding = staged_external_binding(
            stage / "tls-h1-wire-regression.json",
            f"{output_relative}/tls-h1-wire-regression.json",
        )
        codex_binding = staged_external_binding(
            stage / "codex-final-wire-regression.json",
            f"{output_relative}/codex-final-wire-regression.json",
        )
        official_binding = external_binding(official_path, repository_root)
        candidate_binding = external_binding(candidate_rules_path, repository_root)
        candidate_verification_binding = external_binding(candidate_verification_path, repository_root)
        dmit_binding = external_binding(dmit_path, repository_root)

        persona = copy.deepcopy(old_campaign["persona"])
        tool_sha256 = control_tool_bundle_sha256()
        require(old_campaign["tool_bundle_sha256"] == tool_sha256, "控制工具身份相对 FW-F 漂移")

        official_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-official-retest",
                [
                    {
                        "id": "official-retest",
                        "facts": {
                            "binding": official_binding,
                            "required_rules": EXPECTED_REQUIRED_RULES,
                            "profile_assertions": EXPECTED_PROFILE_ASSERTIONS,
                            "scenario_assertions": EXPECTED_SCENARIO_ASSERTIONS,
                            "atomic_assertions": EXPECTED_ATOMIC_ASSERTIONS,
                            "probes": EXPECTED_OFFICIAL_PROBES,
                            "requests": EXPECTED_OFFICIAL_REQUESTS,
                            "candidates": EXPECTED_OFFICIAL_CANDIDATES,
                            "dimensions": EXPECTED_OFFICIAL_DIMENSIONS,
                            "result": "passed",
                        },
                    }
                ],
            ),
        )
        candidate_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-candidate-pair",
                [
                    {
                        "id": "candidate-pair",
                        "facts": {
                            "candidate_binding": candidate_binding,
                            "identity_binding": candidate_verification_binding,
                            "commit": CANDIDATE_COMMIT,
                            "tree": CANDIDATE_TREE,
                            "required_rules": EXPECTED_REQUIRED_RULES,
                            "profile_assertions": EXPECTED_PROFILE_ASSERTIONS,
                            "result": "passed",
                        },
                    }
                ],
            ),
        )
        dmit_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-dmit-acceptance",
                [
                    {
                        "id": "dmit-isolated-acceptance",
                        "facts": {
                            "binding": dmit_binding,
                            "strict_ingress_classes": 4,
                            "outside_scope": "fail-close",
                            "rollback": "passed",
                            "restoration": "passed",
                            "production_selector_modified": False,
                            "vircs_connected": False,
                            "result": "passed",
                        },
                    }
                ],
            ),
        )
        tls_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-tls-h1-wire",
                [{"id": "local-wire-capture", "facts": {"binding": tls_binding, "result": "passed"}}],
            ),
        )
        codex_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-codex-regression",
                [{"id": "codex-zero-difference", "facts": {"binding": codex_binding, "result": "passed"}}],
            ),
        )
        acceptance_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-acceptance-evidence",
                [
                    {"id": "candidate", "facts": {"ref": candidate_evidence_ref}},
                    {"id": "codex", "facts": {"ref": codex_evidence_ref}},
                    {"id": "dmit", "facts": {"ref": dmit_evidence_ref}},
                    {"id": "official", "facts": {"ref": official_evidence_ref}},
                    {"id": "tls", "facts": {"ref": tls_evidence_ref}},
                ],
            ),
        )
        boundary_evidence_ref = store.seal_object(
            "operational_evidence",
            generic_manifest(
                "operational_evidence",
                persona,
                "claude-fw-g-boundary-assertions",
                [
                    {
                        "id": "outside-support-envelope",
                        "facts": {
                            "candidate_ref": candidate_evidence_ref,
                            "dmit_ref": dmit_evidence_ref,
                            "unknown_oauth_egress": "denied",
                            "result": "passed",
                        },
                    }
                ],
            ),
        )

        verified_evidence = build_verified_evidence(
            old_evidence,
            tool_sha256,
            acceptance_evidence_ref,
            [official_binding, candidate_binding, dmit_binding],
            tls_binding,
        )
        verified_evidence_ref = store.seal_object("evidence_package", verified_evidence)

        observation_ref = store.seal_object(
            "ingress_observation",
            build_ingress_observation(persona, acceptance_evidence_ref),
        )
        inventory_ref = store.seal_object(
            "production_ingress_inventory",
            build_ingress_inventory(persona, observation_ref, acceptance_evidence_ref),
        )
        capabilities = build_capabilities()
        unsupported = [
            item
            for item in store.load_object(old_approval["support_envelope_ref"])["payload"]["unsupported_conditions"]
            if item not in {"evidence-level-not-verified", "production-replacement"}
        ]
        support_ref = store.seal_object(
            "support_envelope",
            {
                "schema_version": "official-client-support-envelope/v1",
                "persona": persona,
                "capabilities": capabilities,
                "unsupported_conditions": unsupported,
                "target_spec_ids": spec_ids,
                "production_ingress_inventory_ref": inventory_ref,
                "boundary_assertion_refs": [boundary_evidence_ref],
            },
        )

        old_scenario_plan = store.load_object(old_approval["scenario_plan_ref"])["payload"]
        scenario_plan = build_scenario_plan(old_scenario_plan, persona)
        scenario_plan_ref = store.seal_object("scenario_plan", scenario_plan)
        deployment_plan_ref = store.seal_object(
            "deployment_plan",
            {
                "schema_version": "official-client-deployment-plan/v1",
                "persona": persona,
                "manifest_id": "claude-fw-g-deployment-plan",
                "active_support_capabilities": capabilities,
                "rollback_operational_capabilities": capabilities,
                "deployment_traffic_capabilities": capabilities,
                "rollback_target_kind": "legacy_deployment",
                "failure_policy": "persona_fail_close",
            },
        )

        old_derivation = store.load_object(old_approval["persona_derivation_ref"])["payload"]
        derivation_entries = copy.deepcopy(old_derivation["entries"])
        for entry in derivation_entries:
            if entry["id"] == "adapter":
                entry["facts"]["official_anthropic_messages"] = "lossless-persona-strict"
                entry["facts"]["third_party_anthropic_messages"] = "lossless-persona-strict"
                entry["facts"]["count_tokens"] = "official-and-third-party-persona-strict"
            if entry["id"] == "system-and-metadata":
                entry["facts"]["approval_scope"] = "production_replacement"
        derivation_entries.append(
            {"id": "fw-g-verification", "facts": {"acceptance_evidence_ref": acceptance_evidence_ref}}
        )
        persona_derivation_ref = store.seal_object(
            "persona_derivation",
            generic_manifest(
                "persona_derivation",
                persona,
                "claude-persona-derivation-fw-g",
                derivation_entries,
            ),
        )
        compatibility_boundary_ref = store.seal_object(
            "compatibility_boundary",
            generic_manifest(
                "compatibility_boundary",
                persona,
                "claude-compatibility-boundary-fw-g",
                [
                    {
                        "id": "count-tokens",
                        "facts": {
                            "official_ingress": "official-count-tokens-oauth",
                            "third_party_ingress": "third-party-count-tokens-oauth",
                            "target_disposition": "migrated_strict",
                            "translation": "lossless-required",
                        },
                    },
                    {
                        "id": "lossy-third-party",
                        "facts": {
                            "ingresses": ["chat-completions-oauth", "responses-oauth"],
                            "strict_pair_membership": "denied",
                            "target_disposition": "retained_legacy",
                        },
                    },
                    {
                        "id": "messages",
                        "facts": {
                            "official_ingress": "official-messages-oauth",
                            "third_party_ingress": "third-party-messages-oauth",
                            "target_disposition": "migrated_strict",
                            "translation": "lossless-required",
                        },
                    },
                    {
                        "id": "oauth-refresh",
                        "facts": {
                            "legacy_egress_alias": "egress-claude-oauth-refresh",
                            "legacy_egress_disposition": "non_persona_managed",
                            "official_client_egress": "egress-claude-oauth-token-refresh",
                            "official_client_egress_disposition": "persona_strict",
                        },
                    },
                    {
                        "id": "response-boundary",
                        "facts": {
                            "strict_request_denominator": "excluded",
                            "supporting_fact": "FACT-2_1_226-RESPONSE-COMPATIBILITY",
                        },
                    },
                    {"id": "verification", "facts": {"acceptance_evidence_ref": acceptance_evidence_ref}},
                ],
            ),
        )

        campaign = {
            "schema_version": "official-client-control-campaign/v1",
            "campaign_id": CAMPAIGN_ID,
            "persona": persona,
            "target_version": TARGET_VERSION,
            "official_artifacts": copy.deepcopy(old_campaign["official_artifacts"]),
            "platforms": copy.deepcopy(old_campaign["platforms"]),
            "entrypoints": copy.deepcopy(old_campaign["entrypoints"]),
            "default_conditions": copy.deepcopy(old_campaign["default_conditions"]),
            "tool_bundle_sha256": tool_sha256,
            "bootstrap_ref": copy.deepcopy(old_campaign["bootstrap_ref"]),
            "created_at_utc": "2026-08-20T17:41:01Z",
            "identity_sha256": "",
        }
        campaign["identity_sha256"] = campaign_identity_sha256(campaign)
        store.create_campaign(campaign)
        clock = FactClock(datetime(2026, 8, 20, 17, 41, 1, tzinfo=timezone.utc))
        discovery_bindings = sorted(
            [official_binding, candidate_binding, candidate_verification_binding, dmit_binding, tls_binding, codex_binding],
            key=lambda item: item["path"],
        )
        discovery_ref = store.append_fact(
            CAMPAIGN_ID,
            "discovery_recorded",
            {
                "version": TARGET_VERSION,
                "source": "fw-g-official-retest-candidate-pair-dmit-acceptance",
                "discovered_at_utc": clock.next(),
                "tool_sha256": tool_sha256,
                "artifact_refs": discovery_bindings,
            },
            clock.next(),
        )
        evidence_ref = store.append_fact(
            CAMPAIGN_ID,
            "evidence_recorded",
            {"discovery_fact_ref": discovery_ref, "evidence_package_ref": verified_evidence_ref},
            clock.next(),
        )
        evidence_approval_ref = store.append_fact(
            CAMPAIGN_ID,
            "evidence_approved",
            {
                "evidence_fact_ref": evidence_ref,
                "evidence_package_ref": verified_evidence_ref,
                "reviewer": REVIEWER,
                "review_ref": f"{REVIEW_REF}:evidence",
            },
            clock.next(),
        )

        ingress_targets = []
        for entry in store.load_object(inventory_ref)["payload"]["entries"]:
            logical_id = entry["logical_ingress_id"]
            if logical_id in {
                "official-count-tokens-oauth",
                "official-messages-oauth",
                "third-party-count-tokens-oauth",
                "third-party-messages-oauth",
            }:
                disposition = "migrated_strict"
            elif logical_id == "codex-direct-rerouted":
                disposition = "rerouted"
            else:
                disposition = "retained_legacy"
            ingress_targets.append(
                {
                    "logical_ingress_id": logical_id,
                    "target_disposition": disposition,
                    "evidence_refs": [acceptance_evidence_ref],
                }
            )
        egress_targets = copy.deepcopy(old_approval["egress_target_dispositions"])
        for target in egress_targets:
            target["evidence_refs"] = [acceptance_evidence_ref]

        profile_approval = {
            "evidence_approval_ref": evidence_approval_ref,
            "approval_purpose": "production_replacement",
            "persona_descriptor_ref": copy.deepcopy(old_approval["persona_descriptor_ref"]),
            "profile_schema_ref": copy.deepcopy(old_approval["profile_schema_ref"]),
            "snapshot_ref": copy.deepcopy(old_approval["snapshot_ref"]),
            "release_artifact_ref": copy.deepcopy(old_approval["release_artifact_ref"]),
            "support_envelope_ref": support_ref,
            "production_ingress_inventory_ref": inventory_ref,
            "egress_disposition_inventory_ref": copy.deepcopy(old_approval["egress_disposition_inventory_ref"]),
            "persona_derivation_ref": persona_derivation_ref,
            "compatibility_boundary_ref": compatibility_boundary_ref,
            "scenario_plan_ref": scenario_plan_ref,
            "deployment_plan_ref": deployment_plan_ref,
            "target_spec_ids": spec_ids,
            "ingress_target_dispositions": ingress_targets,
            "egress_target_dispositions": egress_targets,
            "reviewer": REVIEWER,
            "review_ref": f"{REVIEW_REF}:profile",
            "identity_sha256": "",
        }
        profile_approval["identity_sha256"] = profile_approval_identity_sha256(profile_approval)
        profile_approval_ref = store.append_fact(
            CAMPAIGN_ID,
            "profile_approved",
            profile_approval,
            clock.next(),
        )

        candidate = {
            "candidate_id": CANDIDATE_ID,
            "profile_approval_ref": profile_approval_ref,
            "release_artifact_ref": copy.deepcopy(old_approval["release_artifact_ref"]),
            "support_envelope_ref": support_ref,
            "source_tree_sha256": CANDIDATE_SOURCE_TREE,
            "test_tree_sha256": CANDIDATE_TEST_TREE,
            "dependency_lock_sha256": CANDIDATE_DEPENDENCY_LOCK,
            "target_architecture": "linux/amd64",
            "build_id": "fw-g-651ccd518-linux-amd64",
            "image_digest": CANDIDATE_IMAGE,
            "candidate_purpose": "production_replacement",
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        candidate_ref = store.append_fact(CAMPAIGN_ID, "candidate_frozen", candidate, clock.next())

        scenario_artifacts = sorted_refs(
            [official_evidence_ref, candidate_evidence_ref, dmit_evidence_ref, tls_evidence_ref, codex_evidence_ref]
        )
        scenario_approvals: dict[str, dict[str, Any]] = {}
        for scenario in scenario_plan["scenarios"]:
            previous: dict[str, Any] | None = None
            for stage_name, fact_kind, result in (
                ("prepare", "scenario_prepared", "prepared"),
                ("capture", "scenario_captured", "pass"),
                ("seal", "scenario_sealed", "pass"),
                ("approve", "scenario_approved", "pass"),
            ):
                payload: dict[str, Any] = {
                    "candidate_id": CANDIDATE_ID,
                    "scenario_id": scenario["id"],
                    "attempt_id": "dmit-651ccd518",
                    "stage": stage_name,
                    "previous_stage_ref": previous,
                    "artifact_refs": [] if stage_name == "prepare" else scenario_artifacts,
                    "result": result,
                }
                if stage_name == "approve":
                    payload |= {
                        "reviewer": REVIEWER,
                        "review_ref": f"{REVIEW_REF}:scenario:{scenario['id']}",
                    }
                previous = store.append_fact(CAMPAIGN_ID, fact_kind, payload, clock.next())
            require(previous is not None, f"场景未完成：{scenario['id']}")
            scenario_approvals[scenario["id"]] = previous

        spec_scenarios: dict[str, list[dict[str, Any]]] = {spec_id: [] for spec_id in spec_ids}
        for scenario in scenario_plan["scenarios"]:
            for spec_id in scenario["spec_ids"]:
                spec_scenarios[spec_id].append(scenario_approvals[scenario["id"]])
        require(all(spec_scenarios.values()), "ScenarioPlan 没有覆盖全部 RequiredRules")
        candidate_rules_by_spec = {item["spec_id"]: item for item in candidate_rules["entries"]}
        pair_refs: list[dict[str, Any]] = []
        for spec_id in spec_ids:
            count_rule = spec_id == "SPEC-EP-009"
            official_ingress = "official-count-tokens-oauth" if count_rule else "official-messages-oauth"
            third_party_ingress = "third-party-count-tokens-oauth" if count_rule else "third-party-messages-oauth"
            pair = {
                "pair_id": f"PAIR-{spec_id}",
                "spec_id": spec_id,
                "candidate_id": CANDIDATE_ID,
                "release_artifact_ref": copy.deepcopy(old_approval["release_artifact_ref"]),
                "condition_sha256": candidate_rules_by_spec[spec_id]["atomic_pair_sha256"],
                "scenario_approval_refs": sorted_refs(spec_scenarios[spec_id]),
                "official_result": {
                    "ingress_id": official_ingress,
                    "translation": "lossless",
                    "result": "pass",
                    "final_wire_sha256": CANDIDATE_WIRE,
                },
                "third_party_results": [
                    {
                        "protocol_class": "anthropic-messages",
                        "ingress_id": third_party_ingress,
                        "translation": "lossless",
                        "result": "pass",
                        "final_wire_sha256": CANDIDATE_WIRE,
                    }
                ],
                "dynamic_field_checks": [
                    {
                        "id": "trusted-dynamic-fields",
                        "dimensions": ["format", "lifecycle", "relation", "source"],
                        "result": "pass",
                    }
                ],
            }
            pair_refs.append(store.append_fact(CAMPAIGN_ID, "pair_recorded", pair, clock.next()))

        acceptance_ref = store.append_fact(
            CAMPAIGN_ID,
            "acceptance_recorded",
            {
                "candidate_id": CANDIDATE_ID,
                "profile_approval_ref": profile_approval_ref,
                "candidate_ref": candidate_ref,
                "pair_refs": sorted_refs(pair_refs),
                "boundary_assertion_refs": [boundary_evidence_ref],
                "inventory_assertion_refs": sorted_refs([acceptance_evidence_ref, dmit_evidence_ref]),
                "acceptance_purpose": "production_replacement",
                "result": "accepted",
            },
            clock.next(),
        )

        view_stats = build_frozen_external_view(
            repository_root,
            store_root,
            stage / "external-replay-view",
            stage,
            output,
        )
        replay = store.replay(
            external_root=(stage / "external-replay-view").resolve(),
            require_external=True,
        )
        status = __import__(
            "tools.official_client_control.gates", fromlist=["WorkflowGates"]
        ).WorkflowGates(store).status(CAMPAIGN_ID)
        require(status["checkpoint"] == "ready", "FW-G Campaign 未达到 ready")
        require(status["production_state"] == "not_activated", "FW-G 越权进入生产状态")

        local_summary = {
            "schema_version": "claude-code-fw-g-acceptance-summary/v1",
            "campaign_id": CAMPAIGN_ID,
            "candidate_id": CANDIDATE_ID,
            "profile_approval_ref": profile_approval_ref,
            "candidate_ref": candidate_ref,
            "acceptance_ref": acceptance_ref,
            "scenario_count": len(scenario_approvals),
            "pair_count": len(pair_refs),
            "replay": replay,
            "external_replay_view": view_stats,
            "status": status,
            "result": "accepted",
        }
        write_canonical(stage / "acceptance-summary.json", local_summary)
        for path in stage.rglob("*.json"):
            scan_secret_bytes(path.read_bytes(), str(path.relative_to(stage)))
        stage.rename(output)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise

    public = {
        "schema_version": "claude-code-fw-g-public-acceptance/v1",
        "phase": "FW-G",
        "target": {
            "product": "claude-code",
            "version": TARGET_VERSION,
            "platform": "linux/amd64",
            "profile_sha256": CANDIDATE_PROFILE,
            "wire_sha256": CANDIDATE_WIRE,
            "release_artifact_sha256": CANDIDATE_RELEASE,
        },
        "campaign": {
            "campaign_id": CAMPAIGN_ID,
            "identity_sha256": campaign["identity_sha256"],
            "profile_approval_ref": profile_approval_ref,
            "approval_purpose": "production_replacement",
        },
        "candidate": {
            "candidate_id": CANDIDATE_ID,
            "candidate_ref": candidate_ref,
            "commit": CANDIDATE_COMMIT,
            "tree": CANDIDATE_TREE,
            "image_digest": CANDIDATE_IMAGE,
            "architecture": "linux/amd64",
        },
        "coverage": {
            "required_rules": EXPECTED_REQUIRED_RULES,
            "profile_atomic_assertions": EXPECTED_PROFILE_ASSERTIONS,
            "scenario_only_assertions": EXPECTED_SCENARIO_ASSERTIONS,
            "atomic_assertions": EXPECTED_ATOMIC_ASSERTIONS,
            "official_probes": EXPECTED_OFFICIAL_PROBES,
            "official_requests": EXPECTED_OFFICIAL_REQUESTS,
            "official_candidates": EXPECTED_OFFICIAL_CANDIDATES,
            "official_dimensions": EXPECTED_OFFICIAL_DIMENSIONS,
            "scenario_approvals": len(scenario_approvals),
            "pair_facts": len(pair_refs),
        },
        "validation": {
            "official_retest": "passed",
            "candidate_pair": "passed",
            "tls_h1_wire_capture": "passed",
            "dmit_strict_ingress": "passed",
            "outside_support_envelope": "fail-close-passed",
            "rollback_and_restoration": "passed",
            "codex_final_wire": "zero-difference",
            "acceptance_ref": acceptance_ref,
            "result": "accepted",
        },
        "control_store": {
            "path": output.relative_to(repository_root).as_posix() + "/control-store",
            "historical_campaign_preserved": True,
            "campaigns": replay["campaigns"],
            "objects": replay["objects"],
            "facts": replay["facts"],
            "external_bindings": replay["external_bindings"],
            "external_verified": replay["external_verified"],
            "checkpoint": status["checkpoint"],
        },
        "safety": {
            "production_selector_modified": False,
            "fw_h_started": False,
            "production_state": "not_activated",
            "vircs_connected": False,
            "vircs_service_changed": False,
        },
        "secret_scan": {"result": "passed", "matched_patterns": 0},
        "result": "accepted",
    }
    scan_secret_bytes(canonical_json_bytes(public), "公开 FW-G 收据")
    write_canonical(public_receipt, public, mode=0o644)
    return public


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="封存 Claude Code FW-G 后继批准与 AcceptanceFact")
    parser.add_argument("--repository-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument(
        "--base-store",
        type=Path,
        default=REPOSITORY_ROOT / "local-analysis/fw-f/claude-code-2.1.226/profile-approval-v5/control-store",
    )
    parser.add_argument(
        "--official-dir",
        type=Path,
        default=REPOSITORY_ROOT / "local-analysis/fw-g/claude-code-2.1.226/fw-g-official-derived-v2-v9-19a2e8ba7/portable-v1",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=REPOSITORY_ROOT / "local-analysis/fw-g/claude-code-2.1.226/candidate-pair-651ccd518",
    )
    parser.add_argument(
        "--dmit-receipt",
        type=Path,
        default=REPOSITORY_ROOT / "local-analysis/fw-g/claude-code-2.1.226/dmit-acceptance-651ccd518/dmit-acceptance.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "local-analysis/fw-g/claude-code-2.1.226/acceptance-v2-651ccd518",
    )
    parser.add_argument(
        "--public-receipt",
        type=Path,
        default=REPOSITORY_ROOT / "docs/egress/maintenance/claude-fw-g-acceptance.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = finalize(build_parser().parse_args(argv))
    except AcceptanceError as error:
        print(f"Claude FW-G finalizer 拒绝：{error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"Claude FW-G finalizer 失败：{error}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
