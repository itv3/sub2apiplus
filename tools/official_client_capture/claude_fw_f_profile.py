#!/usr/bin/env python3
"""完成 Claude Code FW-F 的 target-first 画像、双版本样例和受管批准。

本工具只在新的本地 FW-F Store 中追加对象和事实，不修改 FW-E Store、Runtime
Selector、DMIT 或生产环境。目标版本必须先生成；2.1.220 只在同一 Schema
文档和同一 Compiler attestation 下生成 baseline fixture。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    ensure_directory,
    expect_rfc3339,
    sha256_file,
    write_once,
)
from tools.official_client_control.contracts import (  # noqa: E402
    campaign_identity_sha256,
    capability_key,
    profile_approval_identity_sha256,
)
from tools.official_client_control.errors import ControlError  # noqa: E402
from tools.official_client_control.gates import WorkflowGates  # noqa: E402
from tools.official_client_control.receipts import (  # noqa: E402
    control_tool_bundle_sha256,
)
from tools.official_client_control.store import ControlStore  # noqa: E402


POLICY_SCHEMA = "claude-code-fw-f-profile-policy/v5"
CLEARANCE_CLOSURE_SCHEMA = "claude-code-fw-f-discovery-clearance-closure/v4"
MEASURED_RULES_SCHEMA = "claude-code-fw-f-measured-rule-ledger/v3"
WITHDRAWN_PROPOSALS_SCHEMA = "claude-code-fw-f-withdrawn-rule-proposals/v2"
REQUIRED_RULE_MANIFEST_SCHEMA = "claude-code-required-rule-manifest/v1"
TARGET_PROFILE_SCHEMA = "claude-code-fw-f-target-profile/v5"
SUMMARY_SCHEMA = "claude-code-fw-f-profile-summary/v5"
CLOSURE_SCHEMA = "claude-code-fw-f-profile-closure/v5"
HTTP_SPLIT = b"\r\n\r\n"
REQUEST_LINE_RE = re.compile(r"^([A-Z]+) ([^ ]+) (HTTP/\d\.\d)$")
GUIDE_RULES_BEGIN = "<!-- FW-F-ACTIVE-RULES-BEGIN -->"
GUIDE_RULES_END = "<!-- FW-F-ACTIVE-RULES-END -->"
GUIDE_SPEC_ID_RE = re.compile(r"^### (SPEC-[A-Z0-9_-]+) .+$", re.MULTILINE)
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

# 共享 Executor 只允许消费这些厂商无关控制事实。字段值会绑定具体 Persona，
# 但字段集合不得出现 Claude Header、Body、版本策略或 fallback 语义。
COMPILED_ENVELOPE_FIELDS = {
    "attempt_ordinal",
    "attempt_reason",
    "binding_id",
    "body_replayability",
    "bundle_digest",
    "dialect_attestation_sha256",
    "endpoint",
    "endpoint_id",
    "identity_attestation_sha256",
    "invocation_id",
    "method",
    "persona_sha256",
    "prepared_request_capability_sha256",
    "profile_digest",
    "protocol",
    "release_artifact_ref",
    "route_id",
    "schema_version",
    "sink_id",
    "transport_capability_sha256",
}


class ProfileBuildError(RuntimeError):
    """表示 FW-F 输入、画像或批准门禁没有闭合。"""


def require(condition: bool, message: str) -> None:
    """在受管前置条件不成立时立即停止。"""

    if not condition:
        raise ProfileBuildError(message)


def load_json(path: Path, label: str) -> dict[str, Any]:
    """严格读取顶层 JSON 对象。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileBuildError(f"无法读取 {label}：{path}: {exc}") from exc
    require(isinstance(value, dict), f"{label} 顶层必须是对象：{path}")
    return value


def repository_path(value: str, label: str) -> Path:
    """把策略中的仓库相对路径解析为可信普通路径。"""

    require(isinstance(value, str) and value, f"{label} 必须是非空路径")
    path = (ROOT / value).resolve()
    require(path.is_relative_to(ROOT), f"{label} 越过仓库根：{value}")
    require(path.is_file() and not path.is_symlink(), f"{label} 不是可信普通文件：{value}")
    return path


def external_binding(path: Path) -> dict[str, Any]:
    """为仓库内普通文件建立可由 Store 重放的外部绑定。"""

    resolved = path.resolve()
    require(resolved.is_relative_to(ROOT), f"外部绑定不在仓库内：{resolved}")
    require(resolved.is_file() and not resolved.is_symlink(), f"外部绑定不可信：{resolved}")
    return {
        "path": resolved.relative_to(ROOT).as_posix(),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def guide_rule_ids(path: Path) -> list[str]:
    """提取指南活动规则区，作为五方规则闭集中的文档侧权威集合。"""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileBuildError(f"无法读取 Claude 指南：{path}: {exc}") from exc
    require(text.count(GUIDE_RULES_BEGIN) == 1, "Claude 指南缺少唯一活动规则起始标记")
    require(text.count(GUIDE_RULES_END) == 1, "Claude 指南缺少唯一活动规则结束标记")
    section = text.split(GUIDE_RULES_BEGIN, 1)[1].split(GUIDE_RULES_END, 1)[0]
    spec_ids = GUIDE_SPEC_ID_RE.findall(section)
    require(spec_ids and len(spec_ids) == len(set(spec_ids)), "Claude 指南活动规则 ID 为空或重复")
    return sorted(spec_ids)


def external_bindings(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """生成按路径严格排序且去重的外部绑定。"""

    values = [external_binding(path) for path in paths]
    by_path = {value["path"]: value for value in values}
    require(len(by_path) == len(values), "外部绑定路径重复")
    return [by_path[path] for path in sorted(by_path)]


def parse_time(value: str) -> datetime:
    """解析并规范化带时区的 RFC3339 时间。"""

    expect_rfc3339(value, "issued_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class FactClock:
    """为追加事实提供确定且单调的签发时间。"""

    def __init__(self, value: str) -> None:
        self.current = parse_time(value) - timedelta(seconds=1)

    def next(self) -> str:
        self.current += timedelta(seconds=1)
        return self.current.isoformat().replace("+00:00", "Z")


def validate_policy(policy: dict[str, Any]) -> None:
    """校验项目特定策略的闭集身份和安全边界。"""

    require(policy.get("schema_version") == POLICY_SCHEMA, "FW-F profile policy schema 不匹配")
    require(policy.get("approval_purpose") == "validation_only", "当前证据只能签发 validation_only 批准")
    require(
        "unmeasured_feature_boundary" not in json.dumps(policy, ensure_ascii=False).lower(),
        "画像策略仍包含禁止的 unmeasured_feature_boundary",
    )
    require(policy.get("target_version") != policy.get("baseline_version"), "目标与基线版本不得相同")
    require(isinstance(policy.get("persona"), dict), "policy.persona 缺失")
    require(isinstance(policy.get("profile_schema_document"), dict), "policy.profile_schema_document 缺失")
    compiler_contract = policy.get("compiler_contract")
    require(isinstance(compiler_contract, dict), "policy.compiler_contract 缺失")
    require(
        set(compiler_contract.get("compiled_envelope_fields", [])) == COMPILED_ENVELOPE_FIELDS,
        "compiler_contract 没有精确声明最小 CompiledEnvelope 字段闭集",
    )
    require(isinstance(policy.get("persona_contracts"), dict), "policy.persona_contracts 缺失")
    require(
        set(policy["persona_contracts"]) == {
            "executor_instance",
            "identity_facts",
            "ingress_adapter",
            "planner",
            "transport_capability",
            "typed_egress_plan",
        },
        "persona_contracts 必须精确覆盖 FW-F 扩展点",
    )
    require(policy.get("sample_runs", {}).keys() == {"target", "baseline"}, "sample_runs 必须含 target 和 baseline")
    for role in ("target", "baseline"):
        runs = policy["sample_runs"][role]
        require(isinstance(runs, dict) and set(runs) == {"a1", "s1", "s2", "s4"}, f"{role} 样例必须覆盖 a1/s1/s2/s4")
    for key in ("baseline_artifacts", "contract_sources", "fw_c_receipts", "runtime_catalog"):
        values = policy.get(key)
        require(
            isinstance(values, list) and values == sorted(set(values)) and values,
            f"policy.{key} 必须严格排序且无重复",
        )
    capabilities = policy.get("support_capabilities")
    require(isinstance(capabilities, list) and capabilities, "SupportEnvelope 能力不得为空")
    keys = [capability_key(value) for value in capabilities]
    require(keys == sorted(set(keys)), "support_capabilities 必须严格排序且无重复")
    require(
        policy.get("ingress_target_dispositions", {}).get("messages-oauth") == "migrated_strict",
        "官方 messages 入口必须规划为 migrated_strict",
    )
    managed = policy.get("managed_egress_ids")
    denied = policy.get("denied_egress_ids")
    require(isinstance(managed, list) and managed == sorted(set(managed)), "managed_egress_ids 必须严格排序")
    require(isinstance(denied, list) and denied == sorted(set(denied)), "denied_egress_ids 必须严格排序")
    require(not (set(managed) & set(denied)), "managed 与 denied 出站集合重叠")
    strict_ids = policy.get("persona_strict_egress_ids")
    require(
        isinstance(strict_ids, list)
        and strict_ids == sorted(set(strict_ids))
        and strict_ids,
        "persona_strict_egress_ids 必须严格排序且非空",
    )
    require(not (set(strict_ids) & (set(managed) | set(denied))), "strict、managed 与 denied 出站集合重叠")
    require(len(strict_ids) == 8, "目标画像必须覆盖 v21 实测的 8 类 strict egress")
    v21_evidence = policy.get("v21_evidence")
    require(isinstance(v21_evidence, dict), "policy.v21_evidence 缺失")
    require(
        v21_evidence.get("scenario_count") == 77
        and v21_evidence.get("matrix_dimension_count") == 49
        and v21_evidence.get("candidate_count") == 593
        and v21_evidence.get("request_occurrence_count") == 395,
        "v21 证据分母不完整",
    )
    require(v21_evidence.get("entrypoints") == ["cli", "sdk-cli"], "v21 entrypoint 闭集不一致")
    contracts = policy.get("strict_egress_contracts")
    require(isinstance(contracts, list) and contracts, "strict_egress_contracts 不得为空")
    contract_ids = [value.get("egress_id") for value in contracts if isinstance(value, dict)]
    require(contract_ids == strict_ids, "strict_egress_contracts 与 strict egress 闭集不一致或无序")
    endpoint_kinds: list[str] = []
    endpoint_ids: list[str] = []
    for contract in contracts:
        endpoint_kind = contract.get("endpoint_kind")
        endpoint_id = contract.get("endpoint_id")
        require(isinstance(endpoint_kind, str) and endpoint_kind, "strict egress 缺少 endpoint_kind")
        require(isinstance(endpoint_id, str) and endpoint_id, f"{endpoint_kind} 缺少 endpoint_id")
        endpoint_kinds.append(endpoint_kind)
        endpoint_ids.append(endpoint_id)
        require(contract.get("route_id") in policy["routes"], f"{endpoint_kind} route 未登记")
        require(contract.get("sink_id") in policy["sinks"], f"{endpoint_kind} Sink 未登记")
        endpoint = contract.get("endpoint")
        require(isinstance(endpoint, dict), f"{endpoint_kind} endpoint 缺失")
        require(
            endpoint.get("scheme") == "https"
            and endpoint.get("host") == contract["sink_id"]
            and endpoint.get("port") == 443
            and endpoint.get("http_version") == "HTTP/1.1",
            f"{endpoint_kind} endpoint 坐标非法",
        )
    require(len(endpoint_kinds) == len(set(endpoint_kinds)), "strict endpoint_kind 重复")
    require(len(endpoint_ids) == len(set(endpoint_ids)), "strict endpoint_id 重复")


def validate_clearance_inputs(
    clearance: dict[str, Any],
    measured_rules: dict[str, Any],
    withdrawn_proposals: dict[str, Any],
) -> None:
    """拒绝把未清零发现项、未通过原子断言或未撤回提案带入画像批准。"""

    require(clearance.get("schema_version") == CLEARANCE_CLOSURE_SCHEMA, "clearance closure schema 不匹配")
    require(clearance.get("result") == "passed", "发现项清零未通过")
    require(
        clearance.get("resolved_record_count")
        == clearance.get("source_discovery_count")
        == 7368,
        "发现项没有 7,368/7,368 清零",
    )
    gate_counts = clearance.get("gate_counts")
    require(isinstance(gate_counts, dict) and gate_counts, "发现项清零门禁缺失")
    require(all(value == 0 for value in gate_counts.values()), "发现项清零仍有非零门禁")
    require(clearance.get("legacy_semantic_candidate_count") == 32, "FW-E 语义候选没有 32/32 收敛")
    require(
        clearance.get("candidate_resolution_count")
        == clearance.get("orthogonal_candidate_count")
        == 593,
        "正交候选没有 593/593 收敛",
    )
    require(measured_rules.get("schema_version") == MEASURED_RULES_SCHEMA, "measured rules schema 不匹配")
    require(measured_rules.get("result") == "passed", "实测规则台账未通过")
    entries = measured_rules.get("entries")
    require(
        isinstance(entries, list)
        and measured_rules.get("rule_count") == len(entries)
        and len(entries) > 0,
        "实测规则数量与台账不一致或为空",
    )
    spec_ids = [value.get("spec_id") for value in entries if isinstance(value, dict)]
    require(len(spec_ids) == len(entries) and spec_ids == sorted(set(spec_ids)), "实测规则 ID 不唯一或无序")
    forbidden = {
        "SPEC-HDR-011",
        "SPEC-HDR-034",
        "SPEC-HDR-035",
        "SPEC-HDR-036",
        "SPEC-STATE-002",
    }
    for rule in entries:
        spec_id = rule["spec_id"]
        require(spec_id not in forbidden and not spec_id.startswith("SPEC-RESP-"), f"非活动规则进入画像：{spec_id}")
        require(rule.get("assertion_id") == f"PAIR-{spec_id}", f"{spec_id} 缺少独立 PAIR 断言")
        require(rule.get("assertion_result") == "passed", f"{spec_id} 实测断言未通过")
        require(rule.get("compatibility_class") == "request_egress", f"{spec_id} 不是 request-egress")
        egress_ids = rule.get("egress_ids")
        require(
            isinstance(egress_ids, list)
            and egress_ids == sorted(set(egress_ids))
            and egress_ids,
            f"{spec_id} 缺少严格有序的 egress_ids",
        )
        primary_channel = "P" if rule.get("domain") == "tls" else "R"
        require({primary_channel, "M"} <= set(rule.get("evidence_channels", [])), f"{spec_id} 缺少 {primary_channel}/M")
        positive = rule.get("official_positive", {})
        negative = rule.get("official_negative", {})
        require(
            positive.get("assertion_id") == f"PAIR-{spec_id}-POSITIVE"
            and positive.get("result") == "passed"
            and positive.get("sample_count", 0) > 0,
            f"{spec_id} 缺少独立官方正例",
        )
        require(
            negative.get("assertion_id") == f"PAIR-{spec_id}-NEGATIVE"
            and negative.get("result") == "passed"
            and negative.get("sample_count", 0) > 0,
            f"{spec_id} 缺少官方条件对照或零违规分母断言",
        )
        require(
            negative.get("kind")
            in {
                "official_condition_absent_or_zero_violation",
                "official_negative_or_zero_violation_denominator",
            }
            and negative.get("violation_count") == 0,
            f"{spec_id} 的反向断言类型或零违规结果非法",
        )
        require(
            isinstance(rule.get("applicability"), list)
            and rule["applicability"]
            and isinstance(rule.get("applicability_scope"), str)
            and rule["applicability_scope"],
            f"{spec_id} 缺少条件或适用范围",
        )
        sample_scope = rule.get("sample_scope")
        require(
            isinstance(sample_scope, dict)
            and sample_scope.get("eligible_count", 0) > 0
            and sample_scope.get("matched_count", 0) > 0
            and isinstance(sample_scope.get("unit"), str)
            and sample_scope["unit"],
            f"{spec_id} 缺少完整实测分母",
        )
        refs = rule.get("evidence_refs")
        require(isinstance(refs, list) and refs, f"{spec_id} 缺少 evidence_refs")
        require(
            any(
                value.get("channel") == primary_channel
                and (primary_channel == "P" or value.get("path", "").endswith(".bin"))
                for value in refs
            ),
            f"{spec_id} 没有绑定原始 {primary_channel} 证据",
        )
    require(withdrawn_proposals.get("schema_version") == WITHDRAWN_PROPOSALS_SCHEMA, "withdrawn proposals schema 不匹配")
    require(
        withdrawn_proposals.get("proposal_count")
        == withdrawn_proposals.get("withdrawn_count")
        == 97
        and withdrawn_proposals.get("active_rule_count") == 0,
        "v1 的 97 条提案没有全部撤回",
    )


def validate_required_rule_manifest(
    manifest: dict[str, Any],
    measured_rules: dict[str, Any],
) -> None:
    """校验 110 条原子断言到 40 条画像规则的无损、唯一映射。"""

    require(
        manifest.get("schema_version") == REQUIRED_RULE_MANIFEST_SCHEMA,
        "RequiredRules manifest schema 不匹配",
    )
    required_rules = manifest.get("required_rules")
    scenario_groups = manifest.get("scenario_only_groups")
    require(isinstance(required_rules, list) and required_rules, "required_rules 为空")
    require(isinstance(scenario_groups, list) and scenario_groups, "scenario_only_groups 为空")

    required_ids = [
        value.get("spec_id") for value in required_rules if isinstance(value, dict)
    ]
    require(
        len(required_ids) == len(required_rules)
        and required_ids == sorted(set(required_ids))
        and len(required_ids) == manifest.get("required_rule_count") == 40,
        "RequiredRules 必须是严格有序的 40 条唯一规则",
    )
    group_ids = [
        value.get("group_id") for value in scenario_groups if isinstance(value, dict)
    ]
    require(
        len(group_ids) == len(scenario_groups)
        and group_ids == sorted(set(group_ids)),
        "scenario-only 分组 ID 重复或无序",
    )

    atomic_entries = measured_rules.get("entries")
    require(isinstance(atomic_entries, list), "MeasuredRuleLedger entries 缺失")
    atomic_by_id = {value["spec_id"]: value for value in atomic_entries}
    require(len(atomic_by_id) == len(atomic_entries), "MeasuredRuleLedger 原子 ID 重复")
    mapped_ids: list[str] = []
    profile_atomic_ids: list[str] = []
    required_fields = {
        "domain",
        "implementation",
        "measured",
        "rule",
        "scope",
        "source",
        "status",
        "title",
    }
    for rule in required_rules:
        spec_id = rule["spec_id"]
        require(
            rule.get("responsibility") == "persona_strict",
            f"{spec_id} 不是 persona_strict RequiredRule",
        )
        for field in sorted(required_fields):
            require(
                isinstance(rule.get(field), str) and rule[field],
                f"{spec_id} 缺少 Codex 六字段所需的 {field}",
            )
        atomic_ids = rule.get("atomic_assertion_ids")
        require(
            isinstance(atomic_ids, list)
            and atomic_ids == sorted(set(atomic_ids))
            and atomic_ids
            and spec_id in atomic_ids,
            f"{spec_id} 原子断言映射为空、重复、无序或不含主 ID",
        )
        require(
            all(value in atomic_by_id for value in atomic_ids),
            f"{spec_id} 映射了未知原子断言",
        )
        require(
            all(
                atomic_by_id[value].get("compatibility_class") == "request_egress"
                for value in atomic_ids
            ),
            f"{spec_id} 映射了非 request-egress 原子断言",
        )
        mapped_ids.extend(atomic_ids)
        profile_atomic_ids.extend(atomic_ids)

    scenario_ids: list[str] = []
    for group in scenario_groups:
        group_id = group["group_id"]
        require(
            group.get("responsibility") == "client_local_scenario",
            f"{group_id} 不是客户端本地场景",
        )
        require(
            isinstance(group.get("title"), str)
            and group["title"]
            and isinstance(group.get("disposition"), str)
            and group["disposition"],
            f"{group_id} 缺少标题或处置",
        )
        atomic_ids = group.get("atomic_assertion_ids")
        require(
            isinstance(atomic_ids, list)
            and atomic_ids == sorted(set(atomic_ids))
            and atomic_ids,
            f"{group_id} 原子断言映射为空、重复或无序",
        )
        require(
            all(value in atomic_by_id for value in atomic_ids),
            f"{group_id} 映射了未知原子断言",
        )
        mapped_ids.extend(atomic_ids)
        scenario_ids.extend(atomic_ids)

    require(
        len(mapped_ids) == len(set(mapped_ids)),
        "原子断言被多个 RequiredRule／scenario-only 分组重复消费",
    )
    require(
        sorted(mapped_ids) == sorted(atomic_by_id),
        "110 条原子断言没有被 RequiredRules 与 scenario-only 完整且唯一覆盖",
    )
    require(
        len(profile_atomic_ids)
        == manifest.get("profile_atomic_assertion_count")
        == 106,
        "画像原子断言必须是 106 条",
    )
    require(
        len(scenario_ids)
        == manifest.get("scenario_only_assertion_count")
        == 4,
        "客户端本地场景断言必须是 4 条",
    )
    require(
        len(mapped_ids) == manifest.get("atomic_assertion_count") == 110,
        "原子断言总数必须是 110 条",
    )
    require(
        sorted(scenario_ids)
        == [
            "SPEC-BODY-053",
            "SPEC-BODY-054",
            "SPEC-STATE-005",
            "SPEC-STATE-010",
        ],
        "scenario-only 边界与已批准的客户端本地行为不一致",
    )


def find_single_object(store: ControlStore, kind: str) -> tuple[dict[str, Any], Path]:
    """从 FW-E Store 中读取指定类型的唯一对象。"""

    directory = store.root / "objects" / kind
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    require(len(paths) == 1, f"FW-E Store 中 {kind} 对象数量不是 1：{len(paths)}")
    path = paths[0]
    reference = {"object_kind": kind, "sha256": path.stem}
    return store.load_object(reference), path


def find_campaign_evidence(store: ControlStore, campaign_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 FW-E Campaign 的唯一 EvidenceFact 和 EvidencePackage。"""

    facts = [
        fact for fact in store.list_facts(campaign_id, "evidence")
        if fact["fact_kind"] == "evidence_recorded"
    ]
    require(len(facts) == 1, f"FW-E Campaign 的 EvidenceFact 数量不是 1：{len(facts)}")
    reference = facts[0]["payload"]["evidence_package_ref"]
    return facts[0], store.load_object(reference)


def parse_http_request(content: bytes, label: str) -> dict[str, Any]:
    """从已脱敏的原始 R 字节解析一条 HTTP/1.1 请求。"""

    require(HTTP_SPLIT in content, f"{label} 缺少 HTTP Header/Body 分隔")
    raw_head, raw_body = content.split(HTTP_SPLIT, 1)
    try:
        lines = raw_head.decode("latin-1").split("\r\n")
    except UnicodeDecodeError as exc:
        raise ProfileBuildError(f"{label} Header 无法解析") from exc
    match = REQUEST_LINE_RE.fullmatch(lines[0])
    require(match is not None, f"{label} 请求行不合法：{lines[0]}")
    headers: list[dict[str, str]] = []
    for index, line in enumerate(lines[1:]):
        require(":" in line, f"{label} Header[{index}] 缺少冒号")
        name, value = line.split(":", 1)
        headers.append({"name": name, "value": value.lstrip(" ")})
    lowered = {item["name"].lower(): item["value"] for item in headers}
    content_encoding = lowered.get("content-encoding")
    decoded_body = raw_body
    if content_encoding is not None:
        require(content_encoding == "gzip", f"{label} 使用未支持的 Content-Encoding：{content_encoding}")
        try:
            decoded_body = gzip.decompress(raw_body)
        except (OSError, EOFError) as exc:
            raise ProfileBuildError(f"{label} gzip Body 无法解压：{exc}") from exc
    try:
        body = json.loads(decoded_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProfileBuildError(f"{label} Body 不是 JSON：{exc}") from exc
    require(isinstance(body, dict), f"{label} Body 顶层必须是对象")
    require("<secret>" in lowered.get("authorization", ""), f"{label} Authorization 未脱敏")
    return {
        "method": match.group(1),
        "request_target": match.group(2),
        "http_version": match.group(3),
        "headers": headers,
        "body": body,
        "content_encoding": content_encoding,
        "wire_body_sha256": __import__("hashlib").sha256(raw_body).hexdigest(),
        "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
        "body_sha256": canonical_sha256(body),
    }


def parse_http_stream(content: bytes, label: str) -> list[dict[str, Any]]:
    """按 Content-Length 拆分连接复用产生的连续 HTTP/1.1 请求。"""

    requests: list[dict[str, Any]] = []
    offset = 0
    while offset < len(content):
        head_end = content.find(HTTP_SPLIT, offset)
        require(head_end >= 0, f"{label} offset={offset} 缺少 Header/Body 分隔")
        raw_head = content[offset:head_end]
        try:
            lines = raw_head.decode("latin-1").split("\r\n")
        except UnicodeDecodeError as exc:
            raise ProfileBuildError(f"{label} offset={offset} Header 无法解析") from exc
        match = REQUEST_LINE_RE.fullmatch(lines[0])
        require(match is not None, f"{label} offset={offset} 请求行不合法：{lines[0]}")
        content_length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                try:
                    content_length = int(line.split(":", 1)[1].strip())
                except ValueError as exc:
                    raise ProfileBuildError(f"{label} Content-Length 非整数") from exc
        body_start = head_end + len(HTTP_SPLIT)
        end = body_start + content_length
        require(end <= len(content), f"{label} Content-Length 越过流结尾")
        chunk = content[offset:end]
        if content_length == 0:
            request = {
                "method": match.group(1),
                "request_target": match.group(2),
                "http_version": match.group(3),
                "headers": [
                    {"name": line.split(":", 1)[0], "value": line.split(":", 1)[1].lstrip(" ")}
                    for line in lines[1:]
                ],
                "body": {},
                "raw_sha256": __import__("hashlib").sha256(chunk).hexdigest(),
                "body_sha256": canonical_sha256({}),
            }
        else:
            request = parse_http_request(chunk, f"{label}@{offset}")
        request["stream_offset"] = offset
        request["stream_length"] = len(chunk)
        requests.append(request)
        offset = end
    return requests


def select_run_requests(
    run_dir: Path,
    scenario: str,
    request_kind: str,
    method: str,
    request_target: str,
) -> list[dict[str, Any]]:
    """从单个 relay run 中选择指定闭集请求，并绑定其原始证据。"""

    require(run_dir.is_dir() and not run_dir.is_symlink(), f"样例目录不可信：{run_dir}")
    relay_path = run_dir / "relay" / "relay.json"
    manifest_path = run_dir / "relay-manifest.json"
    relay = load_json(relay_path, "relay")
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for connection in relay.get("connections", []):
        if not isinstance(connection, dict) or not connection.get("valid"):
            continue
        connection_id = connection.get("connection_id")
        if not isinstance(connection_id, int):
            continue
        raw_path = run_dir / "relay" / f"conn{connection_id:03d}.client_to_upstream.bin"
        if not raw_path.is_file():
            continue
        try:
            parsed_requests = parse_http_stream(
                raw_path.read_bytes(), f"{scenario}:{raw_path.name}"
            )
        except ProfileBuildError:
            continue
        for parsed in parsed_requests:
            if parsed["method"] == method and parsed["request_target"] == request_target:
                candidates.append((raw_path, parsed))
    require(candidates, f"{scenario} 没有 {request_kind} R 请求")
    result: list[dict[str, Any]] = []
    for index, (raw_path, parsed) in enumerate(candidates, start=1):
        parsed["scenario"] = scenario if len(candidates) == 1 else f"{scenario}-{index:02d}"
        parsed["evidence"] = external_bindings([raw_path, relay_path, manifest_path])
        result.append(parsed)
    return result


def select_inference_requests(run_dir: Path, scenario: str) -> list[dict[str, Any]]:
    """选择全部 `POST /v1/messages?beta=true` 推理请求。"""

    return select_run_requests(
        run_dir,
        scenario,
        "messages inference",
        "POST",
        "/v1/messages?beta=true",
    )


def select_lifecycle_requests(run_dir: Path, scenario: str) -> list[dict[str, Any]]:
    """选择官方客户端在推理前发送的 `HEAD /api/hello` 生命周期探测。"""

    return select_run_requests(
        run_dir,
        scenario,
        "lifecycle hello",
        "HEAD",
        "/api/hello",
    )


def value_type(value: Any) -> str:
    """返回不混淆 bool 与 int 的 JSON 类型名。"""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) or isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def body_shape(value: Any, depth: int = 0) -> Any:
    """只保留结构和类型，避免把用户文本或 Persona 提示词写入画像。"""

    if depth >= 3:
        return value_type(value)
    if isinstance(value, dict):
        return {key: body_shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        unique = {canonical_sha256(body_shape(item, depth + 1)): body_shape(item, depth + 1) for item in value[:16]}
        return {"type": "array", "item_shapes": [unique[key] for key in sorted(unique)]}
    return value_type(value)


def classify_header(name: str, values: list[str], present_count: int, sample_count: int) -> dict[str, Any]:
    """把 Header 区分为静态、派生、动态、凭据或条件字段。"""

    lowered = name.lower()
    if lowered == "authorization":
        return {"classification": "credential", "source": "Persona OAuth access token"}
    if lowered == "content-length":
        return {"classification": "derived", "source": "serialized body byte length"}
    if present_count != sample_count:
        return {"classification": "conditional", "source": "scenario and invocation state"}
    unique = sorted(set(values))
    if len(unique) == 1:
        value = unique[0]
        if UUID_RE.fullmatch(value) or lowered in {
            "x-client-request-id",
            "x-claude-code-session-id",
            "traceparent",
        }:
            return {"classification": "dynamic", "source": "invocation or session state"}
        return {"classification": "static", "value": value}
    if all(UUID_RE.fullmatch(value) for value in unique):
        return {"classification": "dynamic", "source": "invocation or session state"}
    return {"classification": "dynamic", "source": "scenario, runtime or invocation state"}


def aggregate_profile(
    role: str,
    version: str,
    samples: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """把四个官方 R 样例收敛成不含原始提示词的版本画像。"""

    require(samples, f"{role} 没有样例")
    methods = sorted({sample["method"] for sample in samples})
    targets = sorted({sample["request_target"] for sample in samples})
    versions = sorted({sample["http_version"] for sample in samples})
    require(len(methods) == len(targets) == len(versions) == 1, f"{role} endpoint 坐标不唯一")

    header_order_by_scenario: dict[str, list[str]] = {}
    header_values: dict[str, list[str]] = defaultdict(list)
    header_presence: dict[str, list[str]] = defaultdict(list)
    original_names: dict[str, str] = {}
    for sample in samples:
        names: list[str] = []
        for header in sample["headers"]:
            lowered = header["name"].lower()
            names.append(header["name"])
            original_names.setdefault(lowered, header["name"])
            header_values[lowered].append(header["value"])
            header_presence[lowered].append(sample["scenario"])
        header_order_by_scenario[sample["scenario"]] = names

    headers: list[dict[str, Any]] = []
    for lowered in sorted(original_names):
        fact = classify_header(
            original_names[lowered],
            header_values[lowered],
            len(header_presence[lowered]),
            len(samples),
        )
        headers.append({
            "name": original_names[lowered],
            "present_in": sorted(header_presence[lowered]),
            **fact,
        })

    body_keys = {sample["scenario"]: list(sample["body"].keys()) for sample in samples}
    body_key_sets = [set(value) for value in body_keys.values()]
    required_keys = sorted(set.intersection(*body_key_sets))
    optional_keys = sorted(set.union(*body_key_sets) - set(required_keys))
    field_types: dict[str, list[str]] = {}
    for key in sorted(set.union(*body_key_sets)):
        field_types[key] = sorted({value_type(sample["body"][key]) for sample in samples if key in sample["body"]})
    safe_static: dict[str, Any] = {}
    for key in ("model", "stream"):
        values = [sample["body"].get(key) for sample in samples]
        if all(value == values[0] for value in values) and isinstance(values[0], (str, bool, int, float)):
            safe_static[key] = values[0]

    rule_documents = sorted(rules, key=lambda item: item["spec_id"])
    return {
        "schema_version": TARGET_PROFILE_SCHEMA,
        "generation_role": role,
        "identity": {
            "version": version,
            "entrypoint": "sdk-cli",
            "authentication": "claude.ai-oauth",
            "provider": "firstParty",
            "platform": "linux/amd64",
        },
        "endpoint": {
            "scheme": "https",
            "host": "api.anthropic.com",
            "port": 443,
            "method": methods[0],
            "request_target": targets[0],
            "http_version": versions[0],
        },
        "headers": {
            "order_by_scenario": header_order_by_scenario,
            "facts": headers,
        },
        "body": {
            "top_level_order_by_scenario": body_keys,
            "required_fields": required_keys,
            "optional_fields": optional_keys,
            "field_types": field_types,
            "safe_static_fields": safe_static,
            "shape_by_scenario": {
                sample["scenario"]: body_shape(sample["body"])
                for sample in samples
            },
            "content_policy": "不保存用户文本、Persona system 原文或动态凭据，只保存结构、摘要和派生规则",
        },
        "transport": {
            "tls_sni": "api.anthropic.com",
            "application_protocol": "HTTP/1.1",
            "client_alpn_offer_evidence": "unobserved",
            "clienthello_fingerprint_evidence": "unobserved",
            "connection_reuse": "measured_rule_only",
            "compression": "runtime_header_policy",
        },
        "state": {
            "request_id": "per_invocation",
            "session_id": "per_session",
            "agent_lineage": "conditional",
            "retry": "unsupported_in_current_support_envelope",
        },
        "privacy": {
            "DISABLE_TELEMETRY": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "traffic_presence_comparison": "disabled",
        },
        "rules": rule_documents,
        "sample_facts": [
            {
                "scenario": sample["scenario"],
                "raw_sha256": sample["raw_sha256"],
                "body_sha256": sample["body_sha256"],
                "evidence": sample["evidence"],
            }
            for sample in sorted(samples, key=lambda item: item["scenario"])
        ],
    }


def attach_lifecycle_profile(
    inference_profile: dict[str, Any],
    lifecycle_profile: dict[str, Any],
) -> dict[str, Any]:
    """把生命周期探测作为同一 Release 的第二个 strict 端点写入画像。"""

    require(
        inference_profile["identity"] == lifecycle_profile["identity"],
        "推理与生命周期画像身份不一致",
    )
    require(
        lifecycle_profile["endpoint"]["method"] == "HEAD"
        and lifecycle_profile["endpoint"]["request_target"] == "/api/hello",
        "生命周期端点不是 HEAD /api/hello",
    )
    result = dict(inference_profile)
    result["lifecycle"] = {
        "endpoint_id": "claude-lifecycle-hello-v1",
        "purpose": "official-client-lifecycle-probe",
        "endpoint": lifecycle_profile["endpoint"],
        "headers": lifecycle_profile["headers"],
        "body": lifecycle_profile["body"],
        "transport": lifecycle_profile["transport"],
        "state": lifecycle_profile["state"],
        "sample_facts": lifecycle_profile["sample_facts"],
    }
    return result


def attach_strict_feature_matrix(
    profile: dict[str, Any],
    contracts: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把完整 strict 端点和实测条件矩阵写入同一个不可变画像。"""

    require(contracts, "strict endpoint contract 为空")
    rule_ids_by_egress: dict[str, list[str]] = defaultdict(list)
    rule_ids_by_domain: dict[str, list[str]] = defaultdict(list)
    for rule in rules:
        spec_id = rule["spec_id"]
        rule_ids_by_domain[rule["domain"]].append(spec_id)
        egress_ids = rule.get("egress_ids")
        require(
            isinstance(egress_ids, list)
            and egress_ids == sorted(set(egress_ids))
            and egress_ids,
            f"{spec_id} 缺少 strict egress 映射",
        )
        for egress_id in egress_ids:
            rule_ids_by_egress[egress_id].append(spec_id)

    contract_ids = [value["egress_id"] for value in contracts]
    require(contract_ids == sorted(set(contract_ids)), "strict endpoint contract 未按 egress_id 严格排序")
    require(set(contract_ids) == set(rule_ids_by_egress), "strict endpoint contract 与规则 egress 闭集不一致")

    result = dict(profile)
    result["identity"] = dict(profile["identity"])
    result["identity"]["entrypoints"] = (
        evidence["entrypoints"] if evidence is not None else [profile["identity"]["entrypoint"]]
    )
    result["state"] = dict(profile["state"])
    result["state"].update({
        "retry": "measured-condition-matrix" if evidence is not None else result["state"]["retry"],
        "resume_and_fork": "measured" if evidence is not None else "not-declared-by-baseline-fixture",
        "fallback": "measured" if evidence is not None else "not-declared-by-baseline-fixture",
    })
    auxiliary: dict[str, dict[str, Any]] = {}
    endpoint_entries: list[dict[str, Any]] = []
    for contract in contracts:
        endpoint_kind = contract["endpoint_kind"]
        spec_ids = sorted(rule_ids_by_egress[contract["egress_id"]])
        tls_spec_ids = sorted(
            rule["spec_id"]
            for rule in rules
            if "P" in rule.get("evidence_channels", [])
            and contract["egress_id"] in rule["egress_ids"]
        )
        endpoint_entries.append({
            "egress_id": contract["egress_id"],
            "endpoint_kind": endpoint_kind,
            "route_id": contract["route_id"],
            "sink_id": contract["sink_id"],
            "binding_id": contract["binding_id"],
            "endpoint_id": contract["endpoint_id"],
            "endpoint": contract["endpoint"],
            "body_policy": contract["body_policy"],
            "spec_ids": spec_ids,
            "source": "target-primary-measurement-rule-ledger" if evidence is not None else "baseline-fixture",
        })
        if endpoint_kind in {"messages-inference", "lifecycle-hello"}:
            view = profile if endpoint_kind == "messages-inference" else profile.get("lifecycle")
            require(isinstance(view, dict), f"{endpoint_kind} 画像视图缺失")
            require(view.get("endpoint") == contract["endpoint"], f"{endpoint_kind} 画像与合同端点不一致")
            transport = dict(view["transport"])
            transport.update({
                "client_alpn_offer_evidence": "measured-p-channel" if evidence is not None and tls_spec_ids else "not-measured-for-this-egress",
                "clienthello_fingerprint_evidence": "measured-p-channel" if evidence is not None and tls_spec_ids else "not-measured-for-this-egress",
                "tls_spec_ids": tls_spec_ids,
            })
            if endpoint_kind == "messages-inference":
                result["transport"] = transport
            else:
                result["lifecycle"] = dict(result["lifecycle"])
                result["lifecycle"]["transport"] = transport
            continue
        headers = contract.get("headers")
        require(isinstance(headers, list) and headers, f"{endpoint_kind} 缺少 evidence-derived Header")
        auxiliary[endpoint_kind] = {
            "endpoint_id": contract["endpoint_id"],
            "purpose": "official-client-essential-auxiliary",
            "endpoint": contract["endpoint"],
            "headers": {
                "order_by_scenario": {
                    "target-rm": [value["name"] for value in headers],
                },
                "facts": headers,
            },
            "body": {
                "body_policy": contract["body_policy"],
                "top_level_order": contract.get("top_level_order", []),
                "required_fields": contract.get("required_fields", []),
                "optional_fields": contract.get("optional_fields", []),
                "secret_fields": contract.get("secret_fields", []),
                "content_policy": contract.get(
                    "content_policy",
                    "不保存 OAuth 凭据；Authorization 只保存动态来源策略",
                ),
            },
            "transport": {
                "tls_sni": contract["sink_id"],
                "application_protocol": contract["endpoint"]["http_version"],
                "client_alpn_offer_evidence": "measured-p-channel" if evidence is not None and tls_spec_ids else "not-measured-for-this-egress",
                "clienthello_fingerprint_evidence": "measured-p-channel" if evidence is not None and tls_spec_ids else "not-measured-for-this-egress",
                "tls_spec_ids": tls_spec_ids,
                "connection_reuse": "measured-rule-only",
                "compression": "response-accept-encoding-only",
            },
            "state": {
                "sequence_rule": "SPEC-EP-008",
                "retry": "not-inferred-from-messages",
            },
            "sample_facts": [{"spec_ids": spec_ids}],
        }

    result["auxiliary"] = auxiliary
    result["strict_endpoints"] = endpoint_entries
    result["feature_matrix"] = {
        "rule_count": len(rules),
        "rule_ids_by_domain": {
            key: sorted(values) for key, values in sorted(rule_ids_by_domain.items())
        },
        "rule_ids_by_egress": {
            key: sorted(values) for key, values in sorted(rule_ids_by_egress.items())
        },
        "campaign_id": None if evidence is None else evidence["campaign_id"],
        "scenario_count": None if evidence is None else evidence["scenario_count"],
        "matrix_dimension_count": None if evidence is None else evidence["matrix_dimension_count"],
        "candidate_count": None if evidence is None else evidence["candidate_count"],
        "request_occurrence_count": None if evidence is None else evidence["request_occurrence_count"],
        "capture_source_bundle_sha256": (
            None if evidence is None else evidence["capture_source_bundle_sha256"]
        ),
        "target_binary_sha256": None if evidence is None else evidence["target_binary_sha256"],
        "traffic_presence_comparison": "excluded-by-official-privacy-configuration",
    }
    return result


def endpoint_kinds(profile: dict[str, Any]) -> list[str]:
    """返回画像中已登记的 strict endpoint_kind 闭集。"""

    entries = profile.get("strict_endpoints")
    if not isinstance(entries, list) or not entries:
        return ["messages-inference", "lifecycle-hello"]
    values = [value.get("endpoint_kind") for value in entries if isinstance(value, dict)]
    require(len(values) == len(entries) and len(values) == len(set(values)), "strict endpoint_kind 非唯一")
    return values


def endpoint_profile(profile: dict[str, Any], endpoint_kind: str) -> dict[str, Any]:
    """从完整 Persona 画像中选择一个已登记 strict 端点视图。"""

    if endpoint_kind == "messages-inference":
        return profile
    if endpoint_kind == "lifecycle-hello":
        lifecycle = profile.get("lifecycle")
        require(isinstance(lifecycle, dict), "画像缺少 lifecycle 端点")
        return lifecycle
    auxiliary = profile.get("auxiliary")
    if isinstance(auxiliary, dict) and endpoint_kind in auxiliary:
        view = auxiliary[endpoint_kind]
        require(isinstance(view, dict), f"{endpoint_kind} 辅助画像不是对象")
        return view
    raise ProfileBuildError(f"未知画像端点：{endpoint_kind}")


def endpoint_control(profile: dict[str, Any], endpoint_kind: str) -> dict[str, str]:
    """返回共享 Envelope 需要的端点闭集控制坐标。"""

    entries = profile.get("strict_endpoints")
    if isinstance(entries, list) and entries:
        matches = [value for value in entries if value.get("endpoint_kind") == endpoint_kind]
        require(len(matches) == 1, f"{endpoint_kind} strict endpoint 控制坐标数量不是 1")
        value = matches[0]
        return {
            "route_id": value["route_id"],
            "sink_id": value["sink_id"],
            "binding_id": value["binding_id"],
            "endpoint_id": value["endpoint_id"],
            "body_replayability": (
                "replayable_bytes"
                if value["body_policy"] in {"json", "json-or-gzip-json", "json-secret-fields"}
                else "empty"
            ),
        }
    controls = {
        "messages-inference": {
            "route_id": "claude-messages-inference",
            "sink_id": "api.anthropic.com",
            "binding_id": "validation-only:messages-oauth",
            "endpoint_id": "claude-messages-inference-v1",
            "body_replayability": "replayable_bytes",
        },
        "lifecycle-hello": {
            "route_id": "claude-lifecycle-hello",
            "sink_id": "api.anthropic.com",
            "binding_id": "validation-only:messages-oauth:lifecycle",
            "endpoint_id": "claude-lifecycle-hello-v1",
            "body_replayability": "empty",
        },
    }
    try:
        return controls[endpoint_kind]
    except KeyError as exc:
        raise ProfileBuildError(f"未知端点控制坐标：{endpoint_kind}") from exc


def compare_profiles(target: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """生成 target-first 的可复算双版本差分摘要。"""

    def static_headers(profile: dict[str, Any]) -> dict[str, str]:
        values: dict[str, str] = {}
        for endpoint_kind in endpoint_kinds(profile):
            view = endpoint_profile(profile, endpoint_kind)
            for item in view["headers"]["facts"]:
                if item["classification"] == "static":
                    values[f"{endpoint_kind}/{item['name'].lower()}"] = item["value"]
        return values

    def endpoints(profile: dict[str, Any]) -> dict[str, Any]:
        return {
            endpoint_kind: endpoint_profile(profile, endpoint_kind)["endpoint"]
            for endpoint_kind in endpoint_kinds(profile)
        }

    target_headers = static_headers(target)
    baseline_headers = static_headers(baseline)
    changed_headers = {
        key: {"baseline": baseline_headers.get(key), "target": target_headers.get(key)}
        for key in sorted(set(target_headers) | set(baseline_headers))
        if target_headers.get(key) != baseline_headers.get(key)
    }
    target_endpoints = endpoints(target)
    baseline_endpoints = endpoints(baseline)
    endpoint_changes: dict[str, dict[str, Any]] = {}
    for endpoint_kind in sorted(set(target_endpoints) | set(baseline_endpoints)):
        target_endpoint = target_endpoints.get(endpoint_kind, {})
        baseline_endpoint = baseline_endpoints.get(endpoint_kind, {})
        for key in sorted(set(target_endpoint) | set(baseline_endpoint)):
            if target_endpoint.get(key) != baseline_endpoint.get(key):
                endpoint_changes[f"{endpoint_kind}/{key}"] = {
                    "baseline": baseline_endpoint.get(key),
                    "target": target_endpoint.get(key),
                }
    require(changed_headers or endpoint_changes, "目标与 2.1.220 没有任何版本差异，疑似错误复用旧画像")
    return {
        "generation_order": ["target", "baseline"],
        "target_version": target["identity"]["version"],
        "baseline_version": baseline["identity"]["version"],
        "changed_static_headers": changed_headers,
        "endpoint_changes": endpoint_changes,
        "target_rule_count": len(target["rules"]),
        "baseline_rule_count": len(baseline["rules"]),
        "result": "passed",
    }


def expected_compiled_envelope(
    profile: dict[str, Any],
    persona: dict[str, Any],
    release: dict[str, dict[str, Any]],
    compiler_attestation: str,
    persona_contracts: dict[str, Any],
    endpoint_kind: str = "messages-inference",
) -> dict[str, Any]:
    """把 Persona 自有事实折叠为共享层可消费的最小控制投影。"""

    profile_digest = canonical_sha256(profile)
    view = endpoint_profile(profile, endpoint_kind)
    control = endpoint_control(profile, endpoint_kind)
    endpoint = {
        "scheme": view["endpoint"]["scheme"],
        "host": view["endpoint"]["host"],
        "port": view["endpoint"]["port"],
        "request_target": view["endpoint"]["request_target"],
    }
    identity_attestation = canonical_sha256({
        "contract": persona_contracts["identity_facts"],
        "facts": profile["identity"],
        "persona": persona,
    })
    dialect_attestation = canonical_sha256({
        "compiler_attestation_sha256": compiler_attestation,
        "endpoint_kind": endpoint_kind,
        "endpoint": view["endpoint"],
        "headers": view["headers"],
        "body": view["body"],
        "state": view["state"],
    })
    transport_capability = canonical_sha256({
        "contract": persona_contracts["transport_capability"],
        "profile": view["transport"],
    })
    prepared_request_capability = canonical_sha256({
        "method": view["endpoint"]["method"],
        "endpoint": endpoint,
        "protocol": "http",
        "header_contract_sha256": canonical_sha256(view["headers"]),
        "body_contract_sha256": canonical_sha256(view["body"]),
        "body_replayability": control["body_replayability"],
        "identity_attestation_sha256": identity_attestation,
        "dialect_attestation_sha256": dialect_attestation,
        "transport_capability_sha256": transport_capability,
    })
    return {
        "schema_version": "official-client-compiled-envelope-sample/v1",
        "persona_sha256": canonical_sha256(persona),
        "release_artifact_ref": release["release_artifact"],
        "profile_digest": profile_digest,
        "bundle_digest": release["release_bundle"]["sha256"],
        "route_id": control["route_id"],
        "sink_id": control["sink_id"],
        "binding_id": control["binding_id"],
        "endpoint_id": control["endpoint_id"],
        "endpoint": endpoint,
        "method": view["endpoint"]["method"],
        "protocol": "http",
        "invocation_id": f"fw-f-sample:{profile['generation_role']}:{endpoint_kind}",
        "attempt_ordinal": 1,
        "attempt_reason": "initial",
        "body_replayability": control["body_replayability"],
        "transport_capability_sha256": transport_capability,
        "identity_attestation_sha256": identity_attestation,
        "dialect_attestation_sha256": dialect_attestation,
        "prepared_request_capability_sha256": prepared_request_capability,
    }


def validate_compiled_envelope(
    envelope: dict[str, Any],
    profile: dict[str, Any],
    persona: dict[str, Any],
    release: dict[str, dict[str, Any]],
    compiler_attestation: str,
    persona_contracts: dict[str, Any],
    endpoint_kind: str = "messages-inference",
) -> None:
    """精确拒绝字段扩张、跨 Persona 或跨 Release 的纵向样例。"""

    require(set(envelope) == COMPILED_ENVELOPE_FIELDS, "CompiledEnvelope 字段闭集不一致")
    expected = expected_compiled_envelope(
        profile,
        persona,
        release,
        compiler_attestation,
        persona_contracts,
        endpoint_kind,
    )
    require(envelope == expected, "CompiledEnvelope 与 Persona／Release／Profile 合同不一致")


def compile_vertical_sample(
    profile: dict[str, Any],
    persona: dict[str, Any],
    descriptor_ref: dict[str, Any],
    release: dict[str, dict[str, Any]],
    compiler_attestation: str,
    target_spec_ids: list[str],
    persona_contracts: dict[str, Any],
    endpoint_kind: str = "messages-inference",
) -> dict[str, Any]:
    """用暂定合同生成不可部署的纵向 CompiledEnvelope 样例。"""

    profile_digest = canonical_sha256(profile)
    view = endpoint_profile(profile, endpoint_kind)
    control = endpoint_control(profile, endpoint_kind)
    identity_facts_sha256 = canonical_sha256({
        "contract": persona_contracts["identity_facts"],
        "facts": profile["identity"],
    })
    plan = {
        "planner_contract": persona_contracts["planner"],
        "typed_egress_plan_contract": persona_contracts["typed_egress_plan"],
        "persona_descriptor_ref": descriptor_ref,
        "release_artifact_ref": release["release_artifact"],
        "route_id": control["route_id"],
        "sink_id": control["sink_id"],
        "binding_id": control["binding_id"],
        "endpoint_id": control["endpoint_id"],
        "profile_digest": profile_digest,
        "identity_facts_sha256": identity_facts_sha256,
        "target_spec_ids": target_spec_ids,
    }
    compiled_envelope = expected_compiled_envelope(
        profile,
        persona,
        release,
        compiler_attestation,
        persona_contracts,
        endpoint_kind,
    )
    validate_compiled_envelope(
        compiled_envelope,
        profile,
        persona,
        release,
        compiler_attestation,
        persona_contracts,
        endpoint_kind,
    )
    envelope_sha256 = canonical_sha256(compiled_envelope)
    return {
        "schema_version": "claude-code-fw-f-vertical-sample/v1",
        "generation_role": profile["generation_role"],
        "stages": [
            "PersonaDescriptor",
            "IngressProtocolAdapter",
            "CanonicalRequest+TranslationReport",
            "PersonaPlanner+IdentityAuthority",
            "ClaudeEgressPlan",
            "DialectCompiler",
            "CompiledEnvelope",
            "ExecutorAuthority",
            "FinalizationToken",
            "Guard",
        ],
        "plan_sha256": canonical_sha256(plan),
        "identity_facts_sha256": identity_facts_sha256,
        "compiled_envelope": compiled_envelope,
        "executor": {
            "authority_id": persona_contracts["executor_instance"]["authority_id"],
            "token_issuer_id": persona_contracts["executor_instance"]["token_issuer_id"],
            "compiled_envelope_sha256": envelope_sha256,
            "authority_scope": "per_persona",
            "token_capability": "single_use",
        },
        "expected_wire": {
            "method": view["endpoint"]["method"],
            "request_target": view["endpoint"]["request_target"],
            "http_version": view["endpoint"]["http_version"],
            "header_contract_sha256": canonical_sha256(view["headers"]),
            "body_contract_sha256": canonical_sha256(view["body"]),
        },
        "guard": {
            "mode": "validation_only",
            "compiled_envelope_sha256": envelope_sha256,
            "runtime_catalog_registration": False,
            "production_selector_change": False,
            "unknown_scope": "deny",
        },
        "result": "passed",
    }


def seal_manifest(
    store: ControlStore,
    kind: str,
    persona: dict[str, Any],
    manifest_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """封存通用、按 ID 排序的 Persona manifest。"""

    normalized = sorted(entries, key=lambda item: item["id"])
    require([item["id"] for item in normalized] == sorted({item["id"] for item in normalized}), f"{manifest_id} entry ID 重复")
    return store.seal_object(kind, {
        "schema_version": f"official-client-{kind.replace('_', '-')}/v1",
        "persona": persona,
        "manifest_id": manifest_id,
        "entries": normalized,
    })


def measured_profile_rules(
    measured_rules: dict[str, Any],
    rule_manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 110 条原子证据无损归并为 40 条 Codex 粒度 RequiredRules。"""

    atomic_by_id = {
        value["spec_id"]: value for value in measured_rules["entries"]
    }
    evidence_rules: list[dict[str, Any]] = []
    profile_rules: list[dict[str, Any]] = []
    for definition in rule_manifest["required_rules"]:
        atomic_rules = [
            atomic_by_id[value] for value in definition["atomic_assertion_ids"]
        ]
        refs_by_path: dict[str, dict[str, Any]] = {}
        for source in atomic_rules:
            for value in source["evidence_refs"]:
                projected = {
                    "path": value["path"],
                    "sha256": value["sha256"],
                    "bytes": value["bytes"],
                }
                previous = refs_by_path.setdefault(value["path"], projected)
                require(
                    previous == projected,
                    f"{definition['spec_id']} 的原子证据路径绑定不一致：{value['path']}",
                )

        applicability_sets = [
            set(value["applicability"]) for value in atomic_rules
        ]
        common_applicability = sorted(set.intersection(*applicability_sets))
        evidence_levels = {value["evidence_level"] for value in atomic_rules}
        production_eligibility = {
            value["production_eligibility"] for value in atomic_rules
        }
        rule_lifecycles = {value["rule_lifecycle"] for value in atomic_rules}
        require(
            evidence_levels <= {"observed", "verified"} and evidence_levels,
            f"{definition['spec_id']} 原子证据等级非法",
        )
        require(
            len(production_eligibility) == 1 and len(rule_lifecycles) == 1,
            f"{definition['spec_id']} 原子生命周期或批准用途不一致",
        )
        atomic_assertion_ids = [
            value["assertion_id"] for value in atomic_rules
        ]
        contrast_ids = [
            value["official_negative"]["assertion_id"]
            for value in atomic_rules
            if value["official_negative"]["kind"]
            == "official_condition_absent_or_zero_violation"
        ]
        denominator_ids = [
            value["official_negative"]["assertion_id"]
            for value in atomic_rules
            if value["official_negative"]["kind"]
            == "official_negative_or_zero_violation_denominator"
        ]
        evidence_rule = {
            "spec_id": definition["spec_id"],
            "evidence_level": (
                "verified" if evidence_levels == {"verified"} else "observed"
            ),
            "rule_lifecycle": next(iter(rule_lifecycles)),
            "compatibility_class": "request_egress",
            "migration_decision": (
                "add"
                if all(value["migration_decision"] == "add" for value in atomic_rules)
                else "change"
            ),
            "evidence_refs": [
                refs_by_path[path] for path in sorted(refs_by_path)
            ],
            "applicability": common_applicability,
        }
        evidence_rules.append(evidence_rule)
        profile_rules.append({
            **evidence_rule,
            "title": definition["title"],
            "atomic_assertion_ids": definition["atomic_assertion_ids"],
            "atomic_pair_assertion_ids": atomic_assertion_ids,
            "evidence_assurance": {
                "official_positive_assertion_ids": [
                    value["official_positive"]["assertion_id"]
                    for value in atomic_rules
                ],
                "official_condition_contrast_assertion_ids": contrast_ids,
                "zero_violation_denominator_assertion_ids": denominator_ids,
                "all_atomic_assertions_passed": all(
                    value["assertion_result"] == "passed"
                    for value in atomic_rules
                ),
            },
            "egress_ids": sorted({
                egress_id
                for value in atomic_rules
                for egress_id in value["egress_ids"]
            }),
            "domain": definition["domain"],
            "atomic_domains": sorted({
                value["domain"] for value in atomic_rules
            }),
            "retained_claim": definition["rule"],
            "scope": definition["scope"],
            "source": definition["source"],
            "measured": definition["measured"],
            "implementation": definition["implementation"],
            "status": definition["status"],
            "evidence_channels": sorted({
                channel
                for value in atomic_rules
                for channel in value["evidence_channels"]
            }),
            "assertion_id": f"COMPOSITE-{definition['spec_id']}",
            "assertion_result": (
                "passed"
                if all(
                    value["assertion_result"] == "passed"
                    for value in atomic_rules
                )
                else "failed"
            ),
            "sample_scope": {
                "atomic_assertion_count": len(atomic_rules),
                "atomic_scopes": {
                    value["spec_id"]: value["sample_scope"]
                    for value in atomic_rules
                },
            },
            "applicability_scope": definition["scope"],
            "production_eligibility": next(iter(production_eligibility)),
        })
    ids = [value["spec_id"] for value in evidence_rules]
    require(
        ids == sorted(set(ids))
        and len(ids) == rule_manifest["required_rule_count"],
        "画像 RequiredRules 不是严格有序的 40 条闭集",
    )
    return evidence_rules, profile_rules


def baseline_profile_rules(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """把 2.1.220 历史台账投影为同一 ProfileSchema 可表达的 fixture 规则。"""

    result: list[dict[str, Any]] = []
    for rule in ledger.get("rules", []):
        status = rule.get("status", {})
        if rule["id"] == "SPEC-EP-002":
            egress_ids = ["egress-claude-lifecycle-hello"]
        elif rule["id"] == "SPEC-PROTO-001":
            egress_ids = [
                "egress-claude-lifecycle-hello",
                "egress-claude-messages-inference",
            ]
        else:
            egress_ids = ["egress-claude-messages-inference"]
        result.append({
            "spec_id": rule["id"],
            "domain": rule.get("domain", rule["id"].split("-")[1].lower()),
            "retained_claim": rule["retained_claim"],
            "evidence_level": status.get("disposition", "blocked"),
            "rule_lifecycle": "candidate",
            "compatibility_class": (
                "response_compat"
                if rule["id"].startswith("SPEC-RESP-")
                else "tombstone"
                if rule["id"] == "SPEC-HDR-011"
                else "request_egress"
            ),
            "migration_decision": "inherit",
            "egress_ids": egress_ids,
            "production_eligibility": "baseline_fixture_only",
        })
    result.sort(key=lambda item: item["spec_id"])
    require(result, "2.1.220 baseline 规则为空")
    return result


def rebuild_inventories(
    store: ControlStore,
    persona: dict[str, Any],
    old_store: ControlStore,
    ingress_document: dict[str, Any],
    ingress_path: Path,
    egress_document: dict[str, Any],
    egress_path: Path,
    strict_contracts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """保留 FW-E 当前事实，并追加目标 strict 端点的 source-absent 当前态。"""

    ingress_old = ingress_document["payload"]
    egress_old = egress_document["payload"]
    ingress_observation = old_store.load_object(ingress_old["observation_ref"])["payload"]
    egress_observation = old_store.load_object(egress_old["observation_ref"])["payload"]
    source = seal_manifest(store, "operational_evidence", persona, "fw-f-current-inventory-source", [
        {"id": "egress-inventory", "facts": {"binding": external_binding(egress_path), "preservation": "current_fact_unchanged"}},
        {"id": "ingress-inventory", "facts": {"binding": external_binding(ingress_path), "preservation": "current_fact_unchanged"}},
        {
            "id": "planned-strict-egress-identities",
            "facts": {
                "current_source_state": "absent",
                "egress_ids": sorted(value["egress_id"] for value in strict_contracts),
                "implementation_stage": "FW-G",
            },
        },
    ])
    ingress_observation_ref = store.seal_object("ingress_observation", {
        **ingress_observation,
        "source_refs": [source],
    })
    ingress_entries = []
    for item in ingress_old["entries"]:
        ingress_entries.append({**item, "evidence_refs": [source]})
    ingress_ref = store.seal_object("production_ingress_inventory", {
        **ingress_old,
        "observation_ref": ingress_observation_ref,
        "entries": ingress_entries,
    })
    observed_egresses = {
        item["egress_id"]: item for item in egress_observation["egresses"]
    }
    endpoint_kind_to_observation_kind = {
        "count-tokens": "auxiliary",
        "messages-inference": "inference",
        "lifecycle-hello": "lifecycle",
        "mcp-servers": "auxiliary",
        "oauth-profile": "auxiliary",
        "oauth-token-refresh": "auxiliary",
        "policy-limits": "auxiliary",
        "remote-settings": "auxiliary",
    }
    for contract in strict_contracts:
        egress_id = contract["egress_id"]
        if egress_id in observed_egresses:
            continue
        endpoint_kind = contract["endpoint_kind"]
        require(
            endpoint_kind in endpoint_kind_to_observation_kind,
            f"strict egress 缺少 observation kind：{endpoint_kind}",
        )
        observed_egresses[egress_id] = {
            "egress_id": egress_id,
            "route_id": f"route-{egress_id}",
            "sink_id": f"sink-{egress_id}",
            "oauth_related": True,
            "kind": endpoint_kind_to_observation_kind[endpoint_kind],
        }
    egress_observation_ref = store.seal_object("egress_observation", {
        **egress_observation,
        "source_refs": [source],
        "egresses": [observed_egresses[key] for key in sorted(observed_egresses)],
    })
    egress_entries = []
    for item in egress_old["entries"]:
        egress_entries.append({
            **item,
            "runtime_assertion_refs": [source],
        })
    existing_ids = {value["egress_id"] for value in egress_entries}
    for contract in strict_contracts:
        if contract["egress_id"] in existing_ids:
            continue
        egress_entries.append({
            "egress_id": contract["egress_id"],
            "current_disposition": "denied",
            "current_guard_state": "source_absent",
            "managed_policy": None,
            "spec_ids": [],
            "runtime_assertion_refs": [source],
        })
    egress_entries.sort(key=lambda value: value["egress_id"])
    egress_ref = store.seal_object("egress_disposition_inventory", {
        **egress_old,
        "observation_ref": egress_observation_ref,
        "entries": egress_entries,
    })
    return source, ingress_ref, egress_ref


def release_objects(
    store: ControlStore,
    persona: dict[str, Any],
    version: str,
    schema_id: str,
    schema_document: dict[str, Any],
    compiler_attestation: str,
    profile_document: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """为一个版本生成 ProfileSchema、Snapshot、Bundle 和 ReleaseArtifact。"""

    schema_ref = store.seal_object("profile_schema", {
        "schema_version": "official-client-profile-schema/v1",
        "persona": persona,
        "version": version,
        "schema_id": schema_id,
        "document": schema_document,
    })
    profile_digest = canonical_sha256(profile_document)
    snapshot_ref = store.seal_object("snapshot", {
        "schema_version": "official-client-snapshot/v1",
        "persona": persona,
        "version": version,
        "profile_digest": profile_digest,
        "profile_schema_ref": schema_ref,
        "compiler_attestation_sha256": compiler_attestation,
        "document": profile_document,
    })
    endpoint_views = {
        endpoint_kind: endpoint_profile(profile_document, endpoint_kind)
        for endpoint_kind in endpoint_kinds(profile_document)
    }
    bundle_ref = store.seal_object("release_bundle", {
        "schema_version": "official-client-release-bundle/v1",
        "persona": persona,
        "version": version,
        "profile_digest": profile_digest,
        "snapshot_ref": snapshot_ref,
        "endpoint_digest": canonical_sha256({
            key: value["endpoint"] for key, value in endpoint_views.items()
        }),
        "transport_digest": canonical_sha256({
            key: value["transport"] for key, value in endpoint_views.items()
        }),
        "state_digest": canonical_sha256({
            key: value["state"] for key, value in endpoint_views.items()
        }),
        "policy_digest": canonical_sha256({
            "headers": {
                key: value["headers"] for key, value in endpoint_views.items()
            },
            "body": {
                key: value["body"] for key, value in endpoint_views.items()
            },
            "privacy": profile_document["privacy"],
            "strict_endpoints": profile_document.get("strict_endpoints", []),
            "feature_matrix": profile_document.get("feature_matrix", {}),
            "rules": profile_document["rules"],
        }),
        "document": {
            "generation_role": profile_document["generation_role"],
            "profile_document_sha256": profile_digest,
            "rule_count": len(profile_document["rules"]),
        },
    })
    release_ref = store.seal_object("release_artifact", {
        "schema_version": "official-client-release-artifact/v1",
        "persona": persona,
        "version": version,
        "profile_digest": profile_digest,
        "snapshot_ref": snapshot_ref,
        "release_bundle_ref": bundle_ref,
    })
    return {
        "profile_schema": schema_ref,
        "snapshot": snapshot_ref,
        "release_bundle": bundle_ref,
        "release_artifact": release_ref,
    }


def scenario_plan(
    store: ControlStore,
    persona: dict[str, Any],
    request_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """按规则域生成精确覆盖 SupportEnvelope 分母的场景计划。"""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rule in request_rules:
        groups[rule["domain"]].append(rule)
    scenarios = []
    for domain in sorted(groups):
        rules = sorted(groups[domain], key=lambda value: value["spec_id"])
        spec_ids = [value["spec_id"] for value in rules]
        scenarios.append({
            "id": f"domain-{domain}",
            "spec_ids": spec_ids,
            "ingress_protocol_classes": ["anthropic-messages"],
            "conditions": [
                "approval=validation-only",
                "entrypoint=cli|sdk-cli",
                "privacy=essential-traffic-no-telemetry",
            ],
            "assertion_ids": sorted({
                assertion_id
                for rule in rules
                for assertion_id in rule["atomic_pair_assertion_ids"]
            }),
        })
    return store.seal_object("scenario_plan", {
        "schema_version": "official-client-scenario-plan/v1",
        "persona": persona,
        "manifest_id": "claude-fw-f-scenario-plan",
        "scenarios": scenarios,
    })


def rule_spec_ids_by_egress(
    request_rules: list[dict[str, Any]],
    expected_egress_ids: list[str],
) -> dict[str, list[str]]:
    """把 request-egress 规则精确归属到已声明的物理出站闭集。"""

    require(
        expected_egress_ids == sorted(set(expected_egress_ids)) and expected_egress_ids,
        "strict egress ID 闭集必须严格排序且非空",
    )
    rule_ids = [rule["spec_id"] for rule in request_rules]
    require(rule_ids == sorted(set(rule_ids)) and rule_ids, "request-egress 规则必须严格排序且非空")
    result: dict[str, list[str]] = {egress_id: [] for egress_id in expected_egress_ids}
    for rule in request_rules:
        egress_ids = rule.get("egress_ids")
        require(
            isinstance(egress_ids, list)
            and egress_ids == sorted(set(egress_ids))
            and egress_ids,
            f"{rule['spec_id']} 缺少严格有序的 egress_ids",
        )
        unknown = sorted(set(egress_ids) - set(result))
        require(not unknown, f"{rule['spec_id']} 绑定未声明 strict egress：{unknown}")
        for egress_id in egress_ids:
            result[egress_id].append(rule["spec_id"])
    for egress_id, spec_ids in result.items():
        require(spec_ids, f"strict egress 没有实测规则：{egress_id}")
        require(spec_ids == sorted(set(spec_ids)), f"strict egress 规则映射非法：{egress_id}")
    require(
        set(rule_ids) == {spec_id for spec_ids in result.values() for spec_id in spec_ids},
        "逐 egress 规则映射没有覆盖 request-egress 规则全集",
    )
    return result


def build(args: argparse.Namespace) -> dict[str, Any]:
    """执行完整 FW-F target-first 构造并返回闭合摘要。"""

    policy = load_json(args.policy, "FW-F profile policy")
    validate_policy(policy)
    clearance_dir = args.clearance_dir.resolve()
    clearance_paths = {
        name: clearance_dir / filename
        for name, filename in {
            "ledger": "discovery-disposition-ledger.json",
            "candidate_resolutions": "candidate-resolution-ledger.json",
            "measured_rules": "measured-rule-ledger.json",
            "withdrawn_proposals": "withdrawn-rule-proposals.json",
            "context_facts": "semantic-context-facts.json",
            "managed_facts": "managed-egress-facts.json",
            "closure": "closure.json",
        }.items()
    }
    for label, path in clearance_paths.items():
        require(path.is_file() and not path.is_symlink(), f"清零制品缺失：{label}={path}")
    clearance = load_json(clearance_paths["closure"], "clearance closure")
    measured_rules = load_json(clearance_paths["measured_rules"], "measured rules")
    withdrawn_proposals = load_json(clearance_paths["withdrawn_proposals"], "withdrawn proposals")
    validate_clearance_inputs(clearance, measured_rules, withdrawn_proposals)
    rule_manifest = load_json(args.rule_manifest, "RequiredRules manifest")
    validate_required_rule_manifest(rule_manifest, measured_rules)
    require(
        rule_manifest.get("target_version") == policy["target_version"],
        "RequiredRules manifest 目标版本与策略不一致",
    )

    old_store = ControlStore(args.fw_e_store.resolve())
    old_campaign = old_store.load_campaign(args.fw_e_campaign)
    require(old_campaign["target_version"] == policy["target_version"], "FW-E Campaign 目标版本与策略不一致")
    require(old_campaign["persona"] == policy["persona"], "FW-E Campaign Persona 与策略不一致")
    old_status = WorkflowGates(old_store).status(args.fw_e_campaign)
    require(old_status["checkpoint"] == "evidence_recorded", "FW-E Store 不在 evidence_recorded")
    find_campaign_evidence(old_store, args.fw_e_campaign)
    ingress_document, ingress_path = find_single_object(old_store, "production_ingress_inventory")
    egress_document, egress_path = find_single_object(old_store, "egress_disposition_inventory")
    old_bootstrap = old_store.load_object(old_campaign["bootstrap_ref"])["payload"]

    baseline_ledger = load_json(args.baseline_rules, "2.1.220 rules")
    evidence_rules, target_rules = measured_profile_rules(
        measured_rules,
        rule_manifest,
    )
    baseline_rules = baseline_profile_rules(baseline_ledger)

    output = args.output_dir.resolve()
    require(output.is_relative_to(ROOT), "FW-F output 必须位于仓库内")
    require(not output.exists(), f"FW-F output 已存在，禁止覆盖：{output}")
    ensure_directory(output)
    store = ControlStore.initialize(output / "control-store", args.issued_at)
    clock = FactClock(args.issued_at)
    tool_digest = control_tool_bundle_sha256()

    contract_paths = [repository_path(value, "contract_sources") for value in policy["contract_sources"]]
    guide_paths = [path for path in contract_paths if path.name == "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md"]
    require(len(guide_paths) == 1, "contract_sources 必须且只能包含一份 Claude 指南")
    require(
        args.rule_manifest.resolve() in contract_paths,
        "RequiredRules manifest 必须进入 contract_sources 内容绑定",
    )
    guide_spec_ids = guide_rule_ids(guide_paths[0])
    contract_sources = external_bindings(contract_paths)
    old_contract_by_path = {
        value["path"]: value
        for value in old_bootstrap["contract_sources"]
        if value["path"].startswith("backend/internal/officialegress/")
    }
    for value in contract_sources:
        if value["path"].startswith("backend/internal/officialegress/"):
            require(old_contract_by_path.get(value["path"]) == value, f"Codex 共享运行时发生变化：{value['path']}")
    fw_c_receipts = external_bindings([repository_path(value, "fw_c_receipts") for value in policy["fw_c_receipts"]])
    runtime_catalog = external_bindings([repository_path(value, "runtime_catalog") for value in policy["runtime_catalog"]])
    bootstrap_ref = store.seal_object("bootstrap", {
        "schema_version": "official-client-control-bootstrap/v1",
        "source_commit": args.source_commit,
        "contract_sources": contract_sources,
        "contract_bundle_sha256": canonical_sha256(contract_sources),
        "fw_c_receipts": fw_c_receipts,
        "runtime_catalog": runtime_catalog,
        "tool_bundle_sha256": tool_digest,
        "result": "stable",
    })
    campaign = {
        "schema_version": "official-client-control-campaign/v1",
        "campaign_id": policy["campaign_id"],
        "persona": policy["persona"],
        "target_version": policy["target_version"],
        "official_artifacts": old_campaign["official_artifacts"],
        "platforms": old_campaign["platforms"],
        "entrypoints": old_campaign["entrypoints"],
        "default_conditions": old_campaign["default_conditions"],
        "tool_bundle_sha256": tool_digest,
        "bootstrap_ref": bootstrap_ref,
        "created_at_utc": clock.next(),
        "identity_sha256": "",
    }
    campaign["identity_sha256"] = campaign_identity_sha256(campaign)
    store.create_campaign(campaign)

    clearance_bindings = external_bindings([*clearance_paths.values(), args.clearance_receipt])
    discovery_ref = store.append_fact(policy["campaign_id"], "discovery_recorded", {
        "version": policy["target_version"],
        "source": "fw-e-evidence-plus-fw-f-semantic-clearance",
        "discovered_at_utc": clock.next(),
        "tool_sha256": tool_digest,
        "artifact_refs": sorted(old_campaign["official_artifacts"] + clearance_bindings, key=lambda item: item["path"]),
    }, clock.next())

    discovery_binding_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-discovery-clearance-binding", [
        {"id": "candidate-resolution", "facts": {"binding": external_binding(clearance_paths["candidate_resolutions"]), "resolved": clearance["candidate_resolution_count"], "unresolved": 0}},
        {"id": "clearance-closure", "facts": {"binding": external_binding(clearance_paths["closure"]), "result": "passed"}},
        {"id": "discovery-ledger", "facts": {"binding": external_binding(clearance_paths["ledger"]), "resolved": 7368, "source": 7368}},
        {"id": "measured-atomic-assertions", "facts": {"binding": external_binding(clearance_paths["measured_rules"]), "atomic_assertions": len(measured_rules["entries"]), "all_assertions_passed": True}},
        {"id": "required-rule-manifest", "facts": {"binding": external_binding(args.rule_manifest), "required_rules": len(target_rules), "profile_atomic_assertions": rule_manifest["profile_atomic_assertion_count"], "scenario_only_assertions": rule_manifest["scenario_only_assertion_count"]}},
        {"id": "withdrawn-v1-proposals", "facts": {"binding": external_binding(clearance_paths["withdrawn_proposals"]), "withdrawn": 97, "active": 0}},
    ])
    comparison_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-comparison-policy", [
        {"id": "behavior-boundary", "facts": {"traffic_presence_comparison": "disabled", "strict_wire_when_invoked": "required"}},
        {"id": "target-first", "facts": {"first": policy["target_version"], "second": policy["baseline_version"], "old_version_design_authority": "denied"}},
    ])
    evidence_package_ref = store.seal_object("evidence_package", {
        "schema_version": "official-client-evidence-package/v2",
        "persona": policy["persona"],
        "version": policy["target_version"],
        "official_artifacts": old_campaign["official_artifacts"],
        "platforms": old_campaign["platforms"],
        "entrypoints": old_campaign["entrypoints"],
        "default_conditions": old_campaign["default_conditions"],
        "comparison_policy_ref": comparison_ref,
        "completeness_ref": discovery_binding_ref,
        "producer_tool_sha256": tool_digest,
        "rules": evidence_rules,
    })
    evidence_fact_ref = store.append_fact(policy["campaign_id"], "evidence_recorded", {
        "discovery_fact_ref": discovery_ref,
        "evidence_package_ref": evidence_package_ref,
    }, clock.next())
    evidence_approval_ref = store.append_fact(policy["campaign_id"], "evidence_approved", {
        "evidence_fact_ref": evidence_fact_ref,
        "evidence_package_ref": evidence_package_ref,
        "reviewer": policy["reviewer"],
        "review_ref": policy["review_ref"] + ":evidence",
    }, clock.next())

    target_samples = [
        sample
        for scenario, path in sorted(policy["sample_runs"]["target"].items())
        for sample in select_inference_requests((ROOT / path).resolve(), scenario)
    ]
    target_lifecycle_samples = [
        sample
        for scenario, path in sorted(policy["sample_runs"]["target"].items())
        for sample in select_lifecycle_requests((ROOT / path).resolve(), scenario)
    ]
    target_profile = attach_strict_feature_matrix(
        attach_lifecycle_profile(
            aggregate_profile("target_first", policy["target_version"], target_samples, target_rules),
            aggregate_profile(
                "target_first_lifecycle",
                policy["target_version"],
                target_lifecycle_samples,
                [],
            ),
        ),
        policy["strict_egress_contracts"],
        target_rules,
        policy["v21_evidence"],
    )
    schema_document = policy["profile_schema_document"]
    schema_document_sha256 = canonical_sha256(schema_document)
    compiler_attestation = canonical_sha256({
        "schema_document_sha256": schema_document_sha256,
        "compiler_contract": policy["compiler_contract"],
    })
    descriptor_ref = store.seal_object("persona_descriptor", {
        "schema_version": "official-client-persona-descriptor/v1",
        "persona": policy["persona"],
        "routes": policy["routes"],
        "sinks": policy["sinks"],
        "explicit_exclusions": policy["explicit_exclusions"],
    })
    target_release = release_objects(
        store,
        policy["persona"],
        policy["target_version"],
        policy["profile_schema_id"],
        schema_document,
        compiler_attestation,
        target_profile,
    )

    baseline_samples = [
        sample
        for scenario, path in sorted(policy["sample_runs"]["baseline"].items())
        for sample in select_inference_requests((ROOT / path).resolve(), scenario)
    ]
    baseline_lifecycle_samples = [
        sample
        for scenario, path in sorted(policy["sample_runs"]["baseline"].items())
        for sample in select_lifecycle_requests((ROOT / path).resolve(), scenario)
    ]
    baseline_artifact_bindings = external_bindings([
        *[repository_path(value, "baseline_artifacts") for value in policy["baseline_artifacts"]],
        args.baseline_rules,
    ])
    baseline_request_rules = [
        rule for rule in baseline_rules if rule["compatibility_class"] == "request_egress"
    ]
    baseline_egress_ids = {
        egress_id for rule in baseline_request_rules for egress_id in rule["egress_ids"]
    }
    baseline_contracts = [
        value
        for value in policy["strict_egress_contracts"]
        if value["egress_id"] in baseline_egress_ids
    ]
    baseline_profile = attach_strict_feature_matrix(
        attach_lifecycle_profile(
            aggregate_profile("baseline_fixture", policy["baseline_version"], baseline_samples, baseline_rules),
            aggregate_profile(
                "baseline_fixture_lifecycle",
                policy["baseline_version"],
                baseline_lifecycle_samples,
                [],
            ),
        ),
        baseline_contracts,
        baseline_request_rules,
    )
    baseline_release = release_objects(
        store,
        policy["persona"],
        policy["baseline_version"],
        policy["profile_schema_id"],
        schema_document,
        compiler_attestation,
        baseline_profile,
    )
    profile_diff = compare_profiles(target_profile, baseline_profile)
    target_request_rules = [
        rule for rule in target_rules if rule["compatibility_class"] == "request_egress"
    ]
    target_request_spec_ids = sorted(rule["spec_id"] for rule in target_request_rules)
    target_spec_ids_by_egress = rule_spec_ids_by_egress(
        target_request_rules,
        policy["persona_strict_egress_ids"],
    )
    target_vertical_by_endpoint: dict[str, dict[str, Any]] = {}
    for contract in policy["strict_egress_contracts"]:
        target_vertical_by_endpoint[contract["endpoint_kind"]] = compile_vertical_sample(
            target_profile,
            policy["persona"],
            descriptor_ref,
            target_release,
            compiler_attestation,
            target_spec_ids_by_egress[contract["egress_id"]],
            policy["persona_contracts"],
            contract["endpoint_kind"],
        )

    baseline_spec_ids_by_egress = rule_spec_ids_by_egress(
        baseline_request_rules,
        sorted(baseline_egress_ids),
    )
    baseline_vertical_by_endpoint: dict[str, dict[str, Any]] = {}
    for contract in baseline_contracts:
        baseline_vertical_by_endpoint[contract["endpoint_kind"]] = compile_vertical_sample(
            baseline_profile,
            policy["persona"],
            descriptor_ref,
            baseline_release,
            compiler_attestation,
            baseline_spec_ids_by_egress[contract["egress_id"]],
            policy["persona_contracts"],
            contract["endpoint_kind"],
        )
    require(
        set(baseline_vertical_by_endpoint) == {"lifecycle-hello", "messages-inference"},
        "2.1.220 fixture 必须且只能生成 messages 与 lifecycle 两个纵向样例",
    )

    target_sample_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-target-first-sample", [
        *[
            {"id": f"compiled-envelope-{endpoint_kind}", "facts": vertical}
            for endpoint_kind, vertical in sorted(target_vertical_by_endpoint.items())
        ],
        {"id": "source-evidence", "facts": {"evidence_package_ref": evidence_package_ref, "target_version": policy["target_version"]}},
        *[
            {"id": f"sample-inference-{sample['scenario']}", "facts": {"raw_sha256": sample["raw_sha256"], "body_sha256": sample["body_sha256"], "evidence": sample["evidence"]}}
            for sample in target_samples
        ],
        *[
            {"id": f"sample-lifecycle-{sample['scenario']}", "facts": {"raw_sha256": sample["raw_sha256"], "body_sha256": sample["body_sha256"], "evidence": sample["evidence"]}}
            for sample in target_lifecycle_samples
        ],
    ])
    baseline_sample_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-21220-baseline-fixture", [
        *[
            {"id": f"compiled-envelope-{endpoint_kind}", "facts": vertical}
            for endpoint_kind, vertical in sorted(baseline_vertical_by_endpoint.items())
        ],
        {"id": "cross-release-diff", "facts": profile_diff},
        {"id": "official-artifacts", "facts": {"bindings": baseline_artifact_bindings, "fixture_only": True}},
        *[
            {"id": f"sample-inference-{sample['scenario']}", "facts": {"raw_sha256": sample["raw_sha256"], "body_sha256": sample["body_sha256"], "evidence": sample["evidence"]}}
            for sample in baseline_samples
        ],
        *[
            {"id": f"sample-lifecycle-{sample['scenario']}", "facts": {"raw_sha256": sample["raw_sha256"], "body_sha256": sample["body_sha256"], "evidence": sample["evidence"]}}
            for sample in baseline_lifecycle_samples
        ],
    ])
    production_compare = load_json(repository_path(policy["production_compare"], "production_compare"), "production compare")
    require(production_compare.get("result") == "passed", "生产只读对比没有通过")
    contract_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-final-shared-contract", [
        {"id": "codex-fw-c", "facts": {"receipts": fw_c_receipts, "final_wire": "frozen"}},
        {"id": "compiled-envelope", "facts": {"allowed_fields": sorted(COMPILED_ENVELOPE_FIELDS), "persona_policy_field_count": 0}},
        {"id": "production-selector", "facts": {"binding": external_binding(repository_path(policy["production_compare"], "production_compare")), "changed": False}},
        {"id": "shared-runtime", "facts": {"contract_sources": contract_sources, "codex_runtime_change_count": 0}},
        {"id": "vertical-samples", "facts": {"target_ref": target_sample_ref, "baseline_ref": baseline_sample_ref, "same_schema_document": True, "same_compiler_attestation": True}},
    ])
    boundary_ref = seal_manifest(store, "operational_evidence", policy["persona"], "fw-f-boundary-plan", [
        {"id": "outside-support", "facts": {"target_behavior": "persona_fail_close", "implementation_stage": "FW-G"}},
        {"id": "production-registration", "facts": {"allowed": False, "selector_change": False}},
    ])

    inventory_source_ref, ingress_ref, egress_ref = rebuild_inventories(
        store,
        policy["persona"],
        old_store,
        ingress_document,
        ingress_path,
        egress_document,
        egress_path,
        policy["strict_egress_contracts"],
    )
    request_rules = target_request_rules
    request_spec_ids = sorted(rule["spec_id"] for rule in request_rules)
    manifest_spec_ids = sorted(
        rule["spec_id"] for rule in rule_manifest["required_rules"]
    )
    atomic_spec_ids = sorted(
        rule["spec_id"] for rule in measured_rules["entries"]
    )
    profile_atomic_ids = sorted(
        spec_id
        for rule in rule_manifest["required_rules"]
        for spec_id in rule["atomic_assertion_ids"]
    )
    scenario_only_atomic_ids = sorted(
        spec_id
        for group in rule_manifest["scenario_only_groups"]
        for spec_id in group["atomic_assertion_ids"]
    )
    evidence_spec_ids = sorted(rule["spec_id"] for rule in evidence_rules)
    snapshot_spec_ids = sorted(rule["spec_id"] for rule in target_profile["rules"])
    support_spec_ids = list(request_spec_ids)
    tls_rules = [rule for rule in measured_rules["entries"] if rule["domain"] == "tls"]
    non_tls_rules = [rule for rule in measured_rules["entries"] if rule["domain"] != "tls"]
    required_rule_id_sets_equal = (
        guide_spec_ids
        == manifest_spec_ids
        == snapshot_spec_ids
        == evidence_spec_ids
        == support_spec_ids
    )
    atomic_assertion_mapping_complete = (
        sorted(profile_atomic_ids + scenario_only_atomic_ids)
        == atomic_spec_ids
        and len(profile_atomic_ids + scenario_only_atomic_ids)
        == len(set(profile_atomic_ids + scenario_only_atomic_ids))
    )
    require(
        required_rule_id_sets_equal
        and atomic_assertion_mapping_complete
        and request_spec_ids == target_request_spec_ids
        and len(request_spec_ids) == len(target_rules) == 40
        and len(profile_atomic_ids) == 106
        and len(scenario_only_atomic_ids) == 4
        and len(atomic_spec_ids) == measured_rules["rule_count"] == 110,
        "指南、RequiredRules、110 条原子断言、Snapshot、EvidencePackage 与 SupportEnvelope 对账失败",
    )
    all_tls_rules_bind_p_and_m = bool(tls_rules) and all(
        set(rule["evidence_channels"]) == {"P", "M"} for rule in tls_rules
    )
    all_non_tls_rules_bind_r_and_m = bool(non_tls_rules) and all(
        set(rule["evidence_channels"]) == {"R", "M"} for rule in non_tls_rules
    )
    all_atomic_assertions_have_positive_and_countercheck = all(
        rule["official_positive"]["result"] == "passed"
        and rule["official_negative"]["result"] == "passed"
        for rule in measured_rules["entries"]
    )
    require(all_tls_rules_bind_p_and_m, "TLS 规则没有全部严格绑定 P/M")
    require(all_non_tls_rules_bind_r_and_m, "普通规则没有全部严格绑定 R/M")
    require(
        all_atomic_assertions_have_positive_and_countercheck,
        "存在缺少官方正例或条件对照／零违规分母的原子断言",
    )
    official_condition_contrast_count = sum(
        rule["official_negative"]["kind"]
        == "official_condition_absent_or_zero_violation"
        for rule in measured_rules["entries"]
    )
    zero_violation_denominator_count = sum(
        rule["official_negative"]["kind"]
        == "official_negative_or_zero_violation_denominator"
        for rule in measured_rules["entries"]
    )
    rule_reconciliation = {
        "guide_required_rule_count": len(guide_spec_ids),
        "manifest_required_rule_count": len(manifest_spec_ids),
        "snapshot_required_rule_count": len(snapshot_spec_ids),
        "evidence_package_required_rule_count": len(evidence_spec_ids),
        "support_envelope_required_rule_count": len(support_spec_ids),
        "required_rule_id_sets_equal": required_rule_id_sets_equal,
        "atomic_assertion_ledger_count": len(atomic_spec_ids),
        "profile_atomic_assertion_count": len(profile_atomic_ids),
        "scenario_only_assertion_count": len(scenario_only_atomic_ids),
        "atomic_assertion_mapping_complete": atomic_assertion_mapping_complete,
        "atomic_assertion_mapping_unique": (
            len(profile_atomic_ids + scenario_only_atomic_ids)
            == len(set(profile_atomic_ids + scenario_only_atomic_ids))
        ),
        "tls_atomic_assertion_count": len(tls_rules),
        "all_tls_atomic_assertions_bind_p_and_m": all_tls_rules_bind_p_and_m,
        "non_tls_atomic_assertion_count": len(non_tls_rules),
        "all_non_tls_atomic_assertions_bind_r_and_m": all_non_tls_rules_bind_r_and_m,
        "all_atomic_assertions_have_official_positive_and_countercheck": (
            all_atomic_assertions_have_positive_and_countercheck
        ),
        "official_condition_contrast_assertion_count": official_condition_contrast_count,
        "zero_violation_denominator_assertion_count": zero_violation_denominator_count,
        "independent_official_negative_required_for_unconditional_rule": False,
    }
    support_ref = store.seal_object("support_envelope", {
        "schema_version": "official-client-support-envelope/v1",
        "persona": policy["persona"],
        "capabilities": policy["support_capabilities"],
        "unsupported_conditions": sorted(policy["explicit_exclusions"] + [
            "evidence-level-not-verified",
            "production-replacement",
        ]),
        "target_spec_ids": support_spec_ids,
        "production_ingress_inventory_ref": ingress_ref,
        "boundary_assertion_refs": sorted([
            baseline_sample_ref,
            boundary_ref,
            contract_ref,
            discovery_binding_ref,
            target_sample_ref,
        ], key=canonical_sha256),
    })
    derivation_ref = seal_manifest(store, "persona_derivation", policy["persona"], "claude-persona-derivation", [
        {"id": "adapter", "facts": policy["persona_contracts"]["ingress_adapter"]},
        {"id": "compiler", "facts": {"attestation_sha256": compiler_attestation, "contract": policy["compiler_contract"], "same_for_both_versions": True}},
        {"id": "executor", "facts": policy["persona_contracts"]["executor_instance"]},
        {"id": "identity", "facts": policy["persona_contracts"]["identity_facts"]},
        {"id": "planner", "facts": policy["persona_contracts"]["planner"]},
        {"id": "typed-egress-plan", "facts": policy["persona_contracts"]["typed_egress_plan"]},
        {"id": "releases", "facts": {"target_release_ref": target_release["release_artifact"], "baseline_fixture_ref": baseline_release["release_artifact"], "generation_order": ["target", "baseline"]}},
        {"id": "semantic-clearance", "facts": {"clearance_ref": discovery_binding_ref, "resolved": 7368, "unresolved": 0}},
        {"id": "system-and-metadata", "facts": {"ownership": "persona", "silent_client_state_fabrication": "denied", "derivation": "evidence-or-profile-derived-only", "approval_scope": "validation_only"}},
        {"id": "transport", "facts": {"contract": policy["persona_contracts"]["transport_capability"], "profile": target_profile["transport"]}},
    ])
    compatibility_ref = seal_manifest(store, "compatibility_boundary", policy["persona"], "claude-compatibility-boundary", [
        {"id": "count-tokens", "facts": {"ingress": "count-tokens-oauth", "ingress_disposition": "retained_legacy", "legacy_egress_alias": "egress-claude-token-count", "legacy_egress_disposition": "non_persona_managed", "official_client_egress": "egress-claude-count-tokens", "official_client_egress_disposition": "strict_validation_only"}},
        {"id": "oauth-refresh", "facts": {"legacy_egress_alias": "egress-claude-oauth-refresh", "legacy_egress_disposition": "non_persona_managed", "official_client_egress": "egress-claude-oauth-token-refresh", "official_client_egress_disposition": "strict_validation_only"}},
        {"id": "lossy-third-party", "facts": {"ingresses": ["chat-completions-oauth", "responses-oauth"], "strict_pair_membership": "denied", "target_disposition": "retained_legacy"}},
        {"id": "official-messages", "facts": {"ingress": "messages-oauth", "translation": "lossless-required", "target_disposition": "migrated_strict"}},
        {"id": "response-boundary", "facts": {"supporting_fact": "FACT-2_1_226-RESPONSE-COMPATIBILITY", "strict_request_denominator": "excluded"}},
    ])
    scenarios_ref = scenario_plan(store, policy["persona"], request_rules)
    deployment_ref = store.seal_object("deployment_plan", {
        "schema_version": "official-client-deployment-plan/v1",
        "persona": policy["persona"],
        "manifest_id": "claude-fw-f-deployment-plan",
        "active_support_capabilities": policy["support_capabilities"],
        "rollback_operational_capabilities": policy["support_capabilities"],
        "deployment_traffic_capabilities": policy["support_capabilities"],
        "rollback_target_kind": "legacy_deployment",
        "failure_policy": "persona_fail_close",
    })

    ingress_inventory = store.load_object(ingress_ref)["payload"]
    ingress_targets = []
    known_ingress_ids = {item["logical_ingress_id"] for item in ingress_inventory["entries"]}
    require(set(policy["ingress_target_dispositions"]) == known_ingress_ids, "入口 target disposition 未覆盖 Inventory")
    for ingress_id, disposition in sorted(policy["ingress_target_dispositions"].items()):
        ingress_targets.append({
            "logical_ingress_id": ingress_id,
            "target_disposition": disposition,
            "evidence_refs": [inventory_source_ref],
        })

    egress_inventory = store.load_object(egress_ref)["payload"]
    egress_by_id = {item["egress_id"]: item for item in egress_inventory["entries"]}
    strict_ids = set(policy["persona_strict_egress_ids"])
    expected_egress_ids = strict_ids | set(policy["managed_egress_ids"]) | set(policy["denied_egress_ids"])
    require(expected_egress_ids == set(egress_by_id), "出站 target disposition 未覆盖 Inventory")
    egress_targets = []
    for egress_id in sorted(egress_by_id):
        current = egress_by_id[egress_id]
        if egress_id in strict_ids:
            disposition = "persona_strict"
            spec_ids = target_spec_ids_by_egress[egress_id]
            managed_policy = None
        elif egress_id in policy["managed_egress_ids"]:
            disposition = "non_persona_managed"
            spec_ids = []
            managed_policy = current["managed_policy"]
            require(managed_policy is not None, f"{egress_id} 缺少受管策略")
        else:
            disposition = "denied"
            spec_ids = []
            managed_policy = None
        egress_targets.append({
            "egress_id": egress_id,
            "target_disposition": disposition,
            "spec_ids": spec_ids,
            "managed_policy": managed_policy,
            "evidence_refs": [inventory_source_ref],
        })

    approval_payload = {
        "evidence_approval_ref": evidence_approval_ref,
        "approval_purpose": policy["approval_purpose"],
        "persona_descriptor_ref": descriptor_ref,
        "profile_schema_ref": target_release["profile_schema"],
        "snapshot_ref": target_release["snapshot"],
        "release_artifact_ref": target_release["release_artifact"],
        "support_envelope_ref": support_ref,
        "production_ingress_inventory_ref": ingress_ref,
        "egress_disposition_inventory_ref": egress_ref,
        "persona_derivation_ref": derivation_ref,
        "compatibility_boundary_ref": compatibility_ref,
        "scenario_plan_ref": scenarios_ref,
        "deployment_plan_ref": deployment_ref,
        "target_spec_ids": request_spec_ids,
        "ingress_target_dispositions": ingress_targets,
        "egress_target_dispositions": egress_targets,
        "reviewer": policy["reviewer"],
        "review_ref": policy["review_ref"] + ":profile",
        "identity_sha256": "",
    }
    approval_payload["identity_sha256"] = profile_approval_identity_sha256(approval_payload)
    approval_ref = store.append_fact(policy["campaign_id"], "profile_approved", approval_payload, clock.next())

    cross_release_envelope_negative = False
    target_messages_vertical = target_vertical_by_endpoint["messages-inference"]
    mixed_release_envelope = dict(target_messages_vertical["compiled_envelope"])
    mixed_release_envelope["release_artifact_ref"] = baseline_release["release_artifact"]
    try:
        validate_compiled_envelope(
            mixed_release_envelope,
            target_profile,
            policy["persona"],
            target_release,
            compiler_attestation,
            policy["persona_contracts"],
        )
    except ProfileBuildError:
        cross_release_envelope_negative = True
    require(cross_release_envelope_negative, "跨 Release CompiledEnvelope 未被拒绝")

    codex_persona = {
        "provider": "openai",
        "official_product": "codex-cli",
        "auth_family": "oauth",
        "upstream_route_family": "responses",
    }
    cross_persona_envelope_negative = False
    mixed_persona_envelope = dict(target_messages_vertical["compiled_envelope"])
    mixed_persona_envelope["persona_sha256"] = canonical_sha256(codex_persona)
    try:
        validate_compiled_envelope(
            mixed_persona_envelope,
            target_profile,
            policy["persona"],
            target_release,
            compiler_attestation,
            policy["persona_contracts"],
        )
    except ProfileBuildError:
        cross_persona_envelope_negative = True
    require(cross_persona_envelope_negative, "跨 Persona CompiledEnvelope 未被拒绝")

    cross_release_graph_negative = False
    try:
        WorkflowGates(store).validate_new_object({
            "schema_version": "official-client-control-object/v1",
            "object_kind": "release_bundle",
            "payload": {
                "schema_version": "official-client-release-bundle/v1",
                "persona": policy["persona"],
                "version": policy["target_version"],
                "profile_digest": store.load_object(target_release["snapshot"])["payload"]["profile_digest"],
                "snapshot_ref": baseline_release["snapshot"],
                "endpoint_digest": "1" * 64,
                "transport_digest": "2" * 64,
                "state_digest": "3" * 64,
                "policy_digest": "4" * 64,
                "document": {"negative": True},
            },
        })
    except ControlError:
        cross_release_graph_negative = True
    require(cross_release_graph_negative, "跨 Release 对象图混用未被控制面拒绝")
    cross_persona_graph_negative = False
    try:
        WorkflowGates._same_persona(policy["persona"], codex_persona, "FW-F cross persona negative")
    except ControlError:
        cross_persona_graph_negative = True
    require(cross_persona_graph_negative, "跨 Persona 对象图混用未被控制面拒绝")

    status = WorkflowGates(store).status(policy["campaign_id"])
    require(status["checkpoint"] == "profile_approved", f"FW-F Store 未到 profile_approved：{status}")
    require(status["fact_counts"]["validation"] == 0, "FW-F 不得建立 candidate")
    replay = store.replay(external_root=ROOT, require_external=True)
    require(replay["result"] == "passed" and replay["external_verified"], "FW-F Store 完整重放失败")

    references = {
        "campaign_id": policy["campaign_id"],
        "bootstrap": bootstrap_ref,
        "evidence_package": evidence_package_ref,
        "evidence_approval": evidence_approval_ref,
        "profile_approval": approval_ref,
        "target": target_release,
        "baseline_fixture": baseline_release,
        "support_envelope": support_ref,
        "production_ingress_inventory": ingress_ref,
        "egress_disposition_inventory": egress_ref,
        "persona_derivation": derivation_ref,
        "compatibility_boundary": compatibility_ref,
        "scenario_plan": scenarios_ref,
        "deployment_plan": deployment_ref,
        "final_shared_contract": contract_ref,
        "discovery_clearance": discovery_binding_ref,
        "target_vertical_sample": target_sample_ref,
        "baseline_vertical_fixture": baseline_sample_ref,
        "boundary_plan": boundary_ref,
    }
    closure = {
        "schema_version": CLOSURE_SCHEMA,
        "target_version": policy["target_version"],
        "baseline_version": policy["baseline_version"],
        "approval_purpose": policy["approval_purpose"],
        "semantic_clearance": {
            "source_discoveries": clearance["source_discovery_count"],
            "resolved_discoveries": clearance["resolved_record_count"],
            "unresolved_discoveries": clearance["gate_counts"]["unresolved_record_count"],
            "resolved_candidates": clearance["candidate_resolution_count"],
            "measured_atomic_assertions": clearance["measured_rule_count"],
            "withdrawn_v1_proposals": clearance["withdrawn_v1_proposal_count"],
        },
        "rule_counts": {
            "required_rules": len(target_rules),
            "request_egress_required_rules": len(request_rules),
            "atomic_assertions": len(atomic_spec_ids),
            "profile_atomic_assertions": len(profile_atomic_ids),
            "scenario_only_assertions": len(scenario_only_atomic_ids),
            "response_compat": 0,
            "verified": sum(rule["evidence_level"] == "verified" for rule in target_rules),
            "not_verified": sum(rule["evidence_level"] != "verified" for rule in target_rules),
        },
        "target_first": {
            "generation_order": ["target", "baseline"],
            "same_schema_document": True,
            "schema_document_sha256": schema_document_sha256,
            "same_compiler_attestation": True,
            "compiler_attestation_sha256": compiler_attestation,
            "compiled_envelope_fields": sorted(COMPILED_ENVELOPE_FIELDS),
            "compiled_envelope_persona_policy_field_count": 0,
            "profile_diff": profile_diff,
            "target_spec_ids_by_egress": target_spec_ids_by_egress,
            "target_vertical_samples": target_vertical_by_endpoint,
            "baseline_vertical_samples": baseline_vertical_by_endpoint,
        },
        "negative_gates": {
            "cross_persona_envelope_rejected": cross_persona_envelope_negative,
            "cross_persona_object_graph_rejected": cross_persona_graph_negative,
            "cross_release_envelope_rejected": cross_release_envelope_negative,
            "cross_release_object_graph_rejected": cross_release_graph_negative,
        },
        "codex_zero_difference": {
            "shared_runtime_change_count": 0,
            "fw_c_receipt_count": len(fw_c_receipts),
            "result": "passed",
        },
        "rule_reconciliation": rule_reconciliation,
        "store": {
            "status": status,
            "replay": replay,
        },
        "phase_boundary": {
            "profile_schema_generated": True,
            "target_snapshot_generated": True,
            "baseline_fixture_generated": True,
            "evidence_approval_issued": True,
            "profile_approval_issued": True,
            "validation_candidate_generated": False,
            "runtime_selector_changed": False,
            "dmit_deployment_performed": False,
            "vircs_deployment_performed": False,
            "production_changed": False,
        },
        "fw_f_status": "complete_validation_only",
        "result": "passed",
    }
    summary = {
        "schema_version": SUMMARY_SCHEMA,
        "campaign_id": policy["campaign_id"],
        "target_version": policy["target_version"],
        "baseline_version": policy["baseline_version"],
        "approval_purpose": policy["approval_purpose"],
        "target_profile_digest": store.load_object(target_release["snapshot"])["payload"]["profile_digest"],
        "baseline_profile_digest": store.load_object(baseline_release["snapshot"])["payload"]["profile_digest"],
        "target_rule_count": len(target_rules),
        "support_rule_count": len(request_rules),
        "atomic_assertion_count": len(atomic_spec_ids),
        "profile_atomic_assertion_count": len(profile_atomic_ids),
        "scenario_only_assertion_count": len(scenario_only_atomic_ids),
        "discovery_count": clearance["source_discovery_count"],
        "unresolved_discovery_count": 0,
        "rule_reconciliation": rule_reconciliation,
        "references": references,
        "result": "passed",
    }
    write_once(output / "references.json", canonical_json_bytes(references))
    write_once(output / "closure.json", canonical_json_bytes(closure))
    write_once(output / "summary.json", canonical_json_bytes(summary))
    return summary


def build_parser() -> argparse.ArgumentParser:
    """创建只接受显式绝对输入的命令行。"""

    parser = argparse.ArgumentParser(description="完成 Claude Code FW-F target-first 画像与批准")
    parser.add_argument("--fw-e-store", required=True, type=Path)
    parser.add_argument("--fw-e-campaign", required=True)
    parser.add_argument("--clearance-dir", required=True, type=Path)
    parser.add_argument("--clearance-receipt", required=True, type=Path)
    parser.add_argument("--baseline-rules", required=True, type=Path)
    parser.add_argument("--rule-manifest", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--issued-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行 FW-F，并把规范摘要写到标准输出。"""

    try:
        args = build_parser().parse_args(argv)
        require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit) is not None, "source_commit 必须是 40 位小写 Git SHA")
        for label in (
            "fw_e_store",
            "clearance_dir",
            "clearance_receipt",
            "baseline_rules",
            "rule_manifest",
            "policy",
        ):
            value = getattr(args, label)
            require(value.is_absolute(), f"--{label.replace('_', '-')} 必须是绝对路径")
        result = build(args)
    except (ProfileBuildError, ControlError) as exc:
        print(f"FW-F profile 拒绝：{exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
