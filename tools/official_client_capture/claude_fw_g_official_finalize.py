#!/usr/bin/env python3
"""复算 Claude Code 2.1.226 FW-G 独立官方 Campaign 并生成脱敏派生证据。

本工具只读取 Vircs 隔离 Campaign。它复用 FW-F 的逐原子断言实现重新解析全部
P/R/M，随后把 110 条断言唯一映射回 40 条 RequiredRules 和 4 条
scenario-only 断言。输出只保存结构、计数、路径和摘要，不携带请求正文、终端
转录、账号标识或 OAuth 凭据。

这里不会签发 production-replacement ApprovalFact，也不会把 observed 改成
verified。实现后对拍、负例和 DMIT 验收尚未闭合时，官方复测只能作为晋升输入。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.official_client_capture import claude_fw_f_measured_rules  # noqa: E402
from tools.official_client_capture import claude_fw_f_v21_finalize  # noqa: E402
from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


CAMPAIGN_ID = "fw-g-official-rerun-v9-19a2e8ba7"
CAMPAIGN_SHA256 = "708e0de2b9cc6b1987b603c87e5ed13491695f39dba3c51dc27d12b613c6c881"
SOURCE_BUNDLE_SHA256 = "b7c5e8baf7a911d55bb84623f8c98e445c350574d5ded774d2eb2bc536a92af1"
TARGET_BINARY_SHA256 = "4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555"
IMPLEMENTATION_BASE_COMMIT = "19a2e8ba719b35803992eb7b01bd73bee6bc1a24"
EXPECTED_REQUIRED_RULES = 40
EXPECTED_PROFILE_ASSERTIONS = 106
EXPECTED_SCENARIO_ASSERTIONS = 4
EXPECTED_ATOMIC_ASSERTIONS = 110
EXPECTED_PROBES = 77
EXPECTED_CANDIDATES = 593
EXPECTED_DIMENSIONS = 49
EXPECTED_REQUEST_OCCURRENCES = 394
EXPECTED_ENDPOINT_COUNTS = {
    "egress-claude-count-tokens": 36,
    "egress-claude-lifecycle-hello": 81,
    "egress-claude-mcp-servers": 4,
    "egress-claude-messages-inference": 123,
    "egress-claude-oauth-profile": 7,
    "egress-claude-oauth-token-refresh": 1,
    "egress-claude-policy-limits": 71,
    "egress-claude-settings": 71,
}

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!<)[A-Za-z0-9._~+/-]{20,}")
CLAUDE_TOKEN_RE = re.compile(r"\bsk-ant-[A-Za-z0-9_-]{12,}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
CALLBACK_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}\b")
JSON_SECRET_RE = re.compile(
    r'(?i)"(?:access_token|refresh_token|authorization_code|oauth_token)"\s*:\s*"(?!<)[^"]+"'
)


class OfficialFinalizeError(RuntimeError):
    """表示独立官方复测身份、映射或脱敏门禁失败。"""


def require(condition: bool, message: str) -> None:
    """失败即停止，禁止生成部分成立的晋升输入。"""

    if not condition:
        raise OfficialFinalizeError(message)


def load_json(path: Path) -> dict[str, Any]:
    """读取顶层为对象的 JSON。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfficialFinalizeError(f"无法读取 JSON：{path}: {exc}") from exc
    require(isinstance(value, dict), f"JSON 顶层不是对象：{path}")
    return value


def portable_binding(path: Path, evidence_root: Path) -> dict[str, Any]:
    """生成不包含文件内容的可携带证据绑定。"""

    resolved = path.resolve()
    require(resolved.is_relative_to(evidence_root), f"证据越出根目录：{path}")
    require(resolved.is_file() and not resolved.is_symlink(), f"证据不是可信普通文件：{path}")
    return {
        "path": resolved.relative_to(evidence_root).as_posix(),
        "sha256": sha256_file(resolved),
        "bytes": resolved.stat().st_size,
    }


def stripped_evidence_refs(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留定位和复算所需字段，剔除动态请求内容及账号上下文。"""

    refs: dict[tuple[str, str], dict[str, Any]] = {}
    for value in values:
        path = value.get("path")
        channel = value.get("channel")
        digest = value.get("sha256")
        size = value.get("bytes")
        require(
            isinstance(path, str)
            and isinstance(channel, str)
            and isinstance(digest, str)
            and isinstance(size, int),
            "原子断言证据引用不完整",
        )
        refs[(path, channel)] = {
            "path": path,
            "channel": channel,
            "sha256": digest,
            "bytes": size,
        }
    return [refs[key] for key in sorted(refs)]


def validate_required_rules(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """验证 40/106/4 分母，并构造每条原子断言的唯一归属。"""

    require(manifest.get("schema_version") == "claude-code-required-rule-manifest/v1", "RequiredRules schema 不匹配")
    require(manifest.get("target_version") == "2.1.226", "RequiredRules 目标版本不匹配")
    require(manifest.get("required_rule_count") == EXPECTED_REQUIRED_RULES, "RequiredRules 不是 40 条")
    require(manifest.get("profile_atomic_assertion_count") == EXPECTED_PROFILE_ASSERTIONS, "画像原子断言不是 106 条")
    require(manifest.get("scenario_only_assertion_count") == EXPECTED_SCENARIO_ASSERTIONS, "scenario-only 断言不是 4 条")
    require(manifest.get("atomic_assertion_count") == EXPECTED_ATOMIC_ASSERTIONS, "原子断言总数不是 110 条")

    required_rules = manifest.get("required_rules")
    scenario_groups = manifest.get("scenario_only_groups")
    require(isinstance(required_rules, list) and len(required_rules) == EXPECTED_REQUIRED_RULES, "RequiredRules 数组不闭合")
    require(isinstance(scenario_groups, list) and scenario_groups, "scenario-only 分组缺失")

    profile_owner: dict[str, str] = {}
    for rule in required_rules:
        spec_id = rule.get("spec_id")
        atomic_ids = rule.get("atomic_assertion_ids")
        require(isinstance(spec_id, str) and isinstance(atomic_ids, list) and atomic_ids, "RequiredRule 身份或断言为空")
        for atomic_id in atomic_ids:
            require(isinstance(atomic_id, str) and atomic_id not in profile_owner, f"画像断言重复归属：{atomic_id}")
            profile_owner[atomic_id] = spec_id

    scenario_owner: dict[str, str] = {}
    for group in scenario_groups:
        group_id = group.get("group_id")
        atomic_ids = group.get("atomic_assertion_ids")
        require(isinstance(group_id, str) and isinstance(atomic_ids, list) and atomic_ids, "scenario-only 分组非法")
        for atomic_id in atomic_ids:
            require(
                isinstance(atomic_id, str)
                and atomic_id not in scenario_owner
                and atomic_id not in profile_owner,
                f"scenario-only 断言重复归属：{atomic_id}",
            )
            scenario_owner[atomic_id] = group_id

    require(len(profile_owner) == EXPECTED_PROFILE_ASSERTIONS, "画像断言唯一归属不是 106 条")
    require(len(scenario_owner) == EXPECTED_SCENARIO_ASSERTIONS, "scenario-only 唯一归属不是 4 条")
    return required_rules, profile_owner, scenario_owner


def compact_result(value: dict[str, Any], label: str) -> dict[str, Any]:
    """保留官方正负例的条件、分母和结果，不携带原始请求。"""

    require(value.get("result") == "passed", f"{label} 未通过")
    scenarios = value.get("scenarios")
    sample_count = value.get("sample_count")
    require(isinstance(scenarios, list) and isinstance(sample_count, int), f"{label} 缺少场景或分母")
    result = {
        "assertion_id": value.get("assertion_id"),
        "kind": value.get("kind"),
        "scenarios": scenarios,
        "sample_count": sample_count,
        "result": "passed",
    }
    if "violation_count" in value:
        require(value.get("violation_count") == 0, f"{label} 存在违规样本")
        result["violation_count"] = 0
    return result


def build_atomic_verification(
    measured: dict[str, Any],
    profile_owner: dict[str, str],
    scenario_owner: dict[str, str],
) -> dict[str, Any]:
    """把 v5 的 110 条实测断言转换为无正文的可携带证明。"""

    entries = measured.get("entries")
    require(isinstance(entries, list) and len(entries) == EXPECTED_ATOMIC_ASSERTIONS, "v5 实测断言不是 110 条")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        spec_id = entry.get("spec_id")
        require(isinstance(spec_id, str) and spec_id not in seen, f"v5 原子断言重复：{spec_id}")
        seen.add(spec_id)
        require(spec_id in profile_owner or spec_id in scenario_owner, f"v5 原子断言无归属：{spec_id}")
        require(entry.get("assertion_id") == f"PAIR-{spec_id}", f"v5 PAIR 身份不一致：{spec_id}")
        require(entry.get("assertion_result") == "passed", f"v5 原子断言未通过：{spec_id}")
        require(entry.get("evidence_level") == "observed", f"官方复测前置等级漂移：{spec_id}")
        channels = entry.get("evidence_channels")
        expected_channels = ["M", "P"] if entry.get("domain") == "tls" else ["M", "R"]
        require(channels == expected_channels, f"v5 原子断言证据通道不闭合：{spec_id}")
        refs = stripped_evidence_refs(entry.get("evidence_refs", []))
        require(refs, f"v5 原子断言缺少证据引用：{spec_id}")
        result.append(
            {
                "spec_id": spec_id,
                "pair_id": entry["assertion_id"],
                "domain": entry.get("domain"),
                "egress_ids": entry.get("egress_ids"),
                "compatibility_class": entry.get("compatibility_class"),
                "profile_required_rule_id": profile_owner.get(spec_id),
                "scenario_only_group_id": scenario_owner.get(spec_id),
                "evidence_channels": channels,
                "evidence_refs": refs,
                "evidence_refs_sha256": canonical_sha256(refs),
                "official_positive": compact_result(entry.get("official_positive", {}), f"{spec_id} 官方正例"),
                "official_negative": compact_result(entry.get("official_negative", {}), f"{spec_id} 官方负例"),
                "official_retest_result": "passed",
                "promotion_state": "official_retest_only_not_verified",
            }
        )
    require(seen == set(profile_owner) | set(scenario_owner), "v5 与 40/110 映射集合不一致")
    return {
        "schema_version": "claude-code-fw-g-official-atomic-verification/v1",
        "campaign_id": CAMPAIGN_ID,
        "target_version": "2.1.226",
        "target_binary_sha256": TARGET_BINARY_SHA256,
        "atomic_assertion_count": len(result),
        "profile_atomic_assertion_count": len(profile_owner),
        "scenario_only_assertion_count": len(scenario_owner),
        "result": "passed",
        "entries": sorted(result, key=lambda value: value["spec_id"]),
    }


def build_required_rule_verification(
    required_rules: list[dict[str, Any]],
    atomic: dict[str, Any],
) -> dict[str, Any]:
    """证明 40 条 RequiredRules 均由 v5 画像断言唯一全覆盖。"""

    atomic_by_id = {entry["spec_id"]: entry for entry in atomic["entries"]}
    entries: list[dict[str, Any]] = []
    for rule in required_rules:
        spec_id = rule["spec_id"]
        atomic_ids = rule["atomic_assertion_ids"]
        require(all(value in atomic_by_id for value in atomic_ids), f"RequiredRule 缺少 v5 原子断言：{spec_id}")
        require(
            all(atomic_by_id[value]["profile_required_rule_id"] == spec_id for value in atomic_ids),
            f"RequiredRule 原子断言归属漂移：{spec_id}",
        )
        entries.append(
            {
                "spec_id": spec_id,
                "domain": rule.get("domain"),
                "atomic_assertion_ids": atomic_ids,
                "atomic_assertion_count": len(atomic_ids),
                "atomic_verification_sha256": canonical_sha256(
                    [atomic_by_id[value] for value in atomic_ids]
                ),
                "official_retest_result": "passed",
                "evidence_level_before_implementation_pair": "observed",
            }
        )
    require(len(entries) == EXPECTED_REQUIRED_RULES, "RequiredRules 复测映射不是 40 条")
    return {
        "schema_version": "claude-code-fw-g-required-rule-official-verification/v1",
        "campaign_id": CAMPAIGN_ID,
        "target_version": "2.1.226",
        "required_rule_count": len(entries),
        "profile_atomic_assertion_count": EXPECTED_PROFILE_ASSERTIONS,
        "result": "passed",
        "promotion_eligibility": "blocked_until_implementation_pair_and_negative_gates",
        "entries": sorted(entries, key=lambda value: value["spec_id"]),
    }


def build_orthogonal_closure(
    dimensions: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """保留 49 维和 593 候选的唯一终态，不复制历史线索正文。"""

    dimension_entries = dimensions.get("entries")
    candidate_entries = candidates.get("entries")
    require(
        dimensions.get("result") == "passed"
        and dimensions.get("resolved_count") == EXPECTED_DIMENSIONS
        and dimensions.get("unresolved_count") == 0
        and isinstance(dimension_entries, list),
        "v5 49 维没有闭合",
    )
    require(
        candidates.get("result") == "passed"
        and candidates.get("resolved_count") == EXPECTED_CANDIDATES
        and candidates.get("unresolved_count") == 0
        and isinstance(candidate_entries, list),
        "v5 593 候选没有闭合",
    )
    compact_candidates = [
        {
            "candidate_id": value.get("candidate_id"),
            "candidate_group": value.get("candidate_group"),
            "status": value.get("status"),
            "disposition": value.get("disposition"),
            "binding_ids": value.get("binding_ids"),
        }
        for value in candidate_entries
    ]
    candidate_ids = [value["candidate_id"] for value in compact_candidates]
    require(len(candidate_ids) == len(set(candidate_ids)) == EXPECTED_CANDIDATES, "v5 候选终态重复或缺失")
    return {
        "schema_version": "claude-code-fw-g-orthogonal-closure/v1",
        "campaign_id": CAMPAIGN_ID,
        "dimension_count": EXPECTED_DIMENSIONS,
        "dimension_disposition_counts": dimensions.get("disposition_counts"),
        "dimensions": dimension_entries,
        "candidate_count": EXPECTED_CANDIDATES,
        "candidate_group_counts": candidates.get("group_counts"),
        "candidate_disposition_counts": candidates.get("disposition_counts"),
        "candidates": compact_candidates,
        "unresolved_count": 0,
        "result": "passed",
    }


def build_campaign_verification(
    campaign_root: Path,
    evidence_root: Path,
    inputs: dict[str, Any],
    wire: dict[str, Any],
) -> dict[str, Any]:
    """冻结 v5 Campaign、生产零差异和完整请求分母。"""

    campaign = inputs["campaign"]
    summary = inputs["summary"]
    production = inputs["production_diff"]
    require(campaign.get("campaign_id") == CAMPAIGN_ID, "v5 Campaign ID 不一致")
    require(sha256_file(campaign_root / "campaign.json") == CAMPAIGN_SHA256, "v5 Campaign 摘要不一致")
    require(summary.get("capture_source_bundle_sha256") == SOURCE_BUNDLE_SHA256, "v5 Source Bundle 摘要不一致")
    require(summary.get("target_binary_sha256") == TARGET_BINARY_SHA256, "v5 官方二进制摘要不一致")
    require(summary.get("probe_count") == summary.get("passed_count") == EXPECTED_PROBES, "v5 不是 77/77")
    require(summary.get("failed_count") == 0 and summary.get("result") == "passed", "v5 存在失败事件")
    require(production.get("result") == "passed" and production.get("differences") == [], "Vircs 生产前后存在差异")
    require(wire.get("result") == "passed" and wire.get("scenario_count") == EXPECTED_PROBES, "v9 WireInventory 未闭合")
    require(
        wire.get("request_occurrence_count") == EXPECTED_REQUEST_OCCURRENCES
        and wire.get("endpoint_counts") == EXPECTED_ENDPOINT_COUNTS,
        "v9 实际请求分母漂移",
    )
    behavior_condition_path = campaign_root / "environment/profile-cache-condition.json"
    behavior_condition = load_json(behavior_condition_path)
    require(
        behavior_condition
        == {
            "schema_version": "claude-code-fw-g-behavior-coverage-condition/v1",
            "campaign_id": CAMPAIGN_ID,
            "condition": "profile_cache_timestamp_forced_stale_on_tmpfs_copy",
            "purpose": "obtain_positive_wire_occurrence_without_defining_traffic_presence_rule",
            "source_global_state_modified": False,
            "temporary_secret_copy_removed": True,
            "raw_state_included": False,
            "secret_or_identity_included": False,
            "campaign_execution_result": "passed",
            "result": "passed",
        },
        "v9 profile cache 条件收据不一致",
    )
    return {
        "schema_version": "claude-code-fw-g-official-campaign-verification/v1",
        "campaign_id": CAMPAIGN_ID,
        "campaign_sha256": CAMPAIGN_SHA256,
        "capture_source_bundle_sha256": SOURCE_BUNDLE_SHA256,
        "implementation_base_commit": IMPLEMENTATION_BASE_COMMIT,
        "target_version": "2.1.226",
        "target_binary_sha256": TARGET_BINARY_SHA256,
        "probe_count": EXPECTED_PROBES,
        "passed_count": EXPECTED_PROBES,
        "failed_count": 0,
        "request_occurrence_count": wire.get("request_occurrence_count"),
        "unique_wire_content_count": wire.get("unique_wire_content_count"),
        "endpoint_counts": wire.get("endpoint_counts"),
        "traffic_presence_policy": {
            "comparison_dimension": False,
            "occurrence_counts_are_campaign_integrity_only": True,
            "profile_positive_condition": "profile_cache_timestamp_forced_stale_on_tmpfs_copy",
        },
        "candidate_count": EXPECTED_CANDIDATES,
        "matrix_dimension_count": EXPECTED_DIMENSIONS,
        "privacy_configuration": inputs["catalog"].get("privacy_configuration"),
        "production_comparison": {
            "result": "passed",
            "differences": [],
            "before_sha256": production.get("before_sha256"),
            "after_sha256": production.get("after_sha256"),
        },
        "source_bindings": {
            "campaign": portable_binding(campaign_root / "campaign.json", evidence_root),
            "scenario_catalog": portable_binding(campaign_root / "scenario-catalog.json", evidence_root),
            "candidate_denominator": portable_binding(campaign_root / "candidate-denominator.json", evidence_root),
            "execution_summary": portable_binding(campaign_root / "execution-summary.json", evidence_root),
            "production_comparison": portable_binding(
                campaign_root / "environment" / "production-compare.json", evidence_root
            ),
            "behavior_coverage_condition": portable_binding(
                behavior_condition_path, evidence_root
            ),
        },
        "result": "passed",
    }


def scan_documents(documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """阻断 OAuth secret、回调码、邮箱和未脱敏 Bearer 进入可携带制品。"""

    patterns = {
        "email": EMAIL_RE,
        "bearer": BEARER_RE,
        "claude_token": CLAUDE_TOKEN_RE,
        "jwt": JWT_RE,
        "oauth_callback": CALLBACK_RE,
        "json_secret": JSON_SECRET_RE,
    }
    findings: list[dict[str, str]] = []
    scanned: list[dict[str, Any]] = []
    for name, document in sorted(documents.items()):
        raw = canonical_json_bytes(document)
        text = raw.decode("utf-8")
        scanned.append({"path": name, "sha256": canonical_sha256(document), "bytes": len(raw)})
        for pattern_name, pattern in patterns.items():
            if pattern.search(text):
                findings.append({"path": name, "pattern": pattern_name})
    require(not findings, f"可携带制品命中敏感模式：{findings}")
    return {
        "schema_version": "claude-code-fw-g-portable-secret-scan/v1",
        "campaign_id": CAMPAIGN_ID,
        "scanned_files": scanned,
        "finding_count": 0,
        "result": "passed",
    }


def write_once(output_dir: Path, documents: dict[str, dict[str, Any]]) -> None:
    """以追加式全新目录写入脱敏派生证据。"""

    require(not output_dir.exists(), f"输出目录已存在，禁止覆盖：{output_dir}")
    output_dir.mkdir(parents=True, mode=0o700)
    for name, document in sorted(documents.items()):
        path = output_dir / name
        path.write_bytes(canonical_json_bytes(document))
        path.chmod(0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析只读证据根和输出位置。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--campaign-root", required=True, type=Path)
    parser.add_argument("--prior-measured-rules", required=True, type=Path)
    parser.add_argument("--prior-candidate-resolutions", required=True, type=Path)
    parser.add_argument("--required-rules", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行 v5 全量复算并输出可携带派生证据。"""

    args = parse_args(argv)
    try:
        evidence_root = args.evidence_root.resolve()
        campaign_root = args.campaign_root.resolve()
        output_dir = args.output_dir.resolve()
        require(evidence_root.is_dir() and not evidence_root.is_symlink(), "证据根不可信")
        require(campaign_root.is_relative_to(evidence_root), "Campaign 不在证据根内")
        require(output_dir.is_relative_to(evidence_root), "输出目录不在证据根内")

        # 两个历史复算模块都把 ROOT 作为证据路径信任根；FW-G 必须显式切换到
        # 本次隔离工作树，不能借用开发机路径或 complete-v21 目录。
        claude_fw_f_v21_finalize.ROOT = evidence_root
        claude_fw_f_measured_rules.ROOT = evidence_root

        inputs, runs = claude_fw_f_v21_finalize.load_campaign(
            campaign_root,
            expected_source_sha256=SOURCE_BUNDLE_SHA256,
            production_comparison_relative="environment/production-compare.json",
        )
        inputs["denominator_path"] = campaign_root / "candidate-denominator.json"
        wire = claude_fw_f_v21_finalize.build_wire_inventory(
            campaign_root,
            inputs,
            runs,
            expected_request_occurrence_count=EXPECTED_REQUEST_OCCURRENCES,
            expected_endpoint_counts=EXPECTED_ENDPOINT_COUNTS,
        )
        revalidated = claude_fw_f_v21_finalize.revalidate_prior_atomic_rules(
            campaign_root, runs
        )
        measured = claude_fw_f_v21_finalize.build_measured_rules(
            campaign_root,
            inputs,
            runs,
            args.prior_measured_rules.resolve(),
            revalidated,
        )
        dimensions = claude_fw_f_v21_finalize.build_dimension_ledger(
            inputs, runs, measured
        )
        candidates = claude_fw_f_v21_finalize.build_candidate_ledger(
            inputs,
            measured,
            args.prior_candidate_resolutions.resolve(),
        )

        manifest = load_json(args.required_rules.resolve())
        required_rules, profile_owner, scenario_owner = validate_required_rules(manifest)
        atomic = build_atomic_verification(measured, profile_owner, scenario_owner)
        required = build_required_rule_verification(required_rules, atomic)
        orthogonal = build_orthogonal_closure(dimensions, candidates)
        campaign = build_campaign_verification(
            campaign_root, evidence_root, inputs, wire
        )
        producer = portable_binding(Path(__file__), evidence_root)
        documents = {
            "campaign-verification.json": campaign,
            "official-atomic-verification.json": atomic,
            "required-rule-official-verification.json": required,
            "orthogonal-closure.json": orthogonal,
            "wire-inventory.json": wire,
        }
        manifest_document = {
            "schema_version": "claude-code-fw-g-official-portable-manifest/v1",
            "campaign_id": CAMPAIGN_ID,
            "producer": producer,
            "artifacts": [
                {
                    "path": name,
                    "sha256": canonical_sha256(document),
                    "bytes": len(canonical_json_bytes(document)),
                }
                for name, document in sorted(documents.items())
            ],
            "raw_transcript_included": False,
            "raw_request_body_included": False,
            "account_identity_included": False,
            "oauth_secret_included": False,
            "approval_issued": False,
            "result": "passed",
        }
        documents["portable-manifest.json"] = manifest_document
        documents["portable-secret-scan.json"] = scan_documents(documents)
        write_once(output_dir, documents)
    except (OfficialFinalizeError, claude_fw_f_v21_finalize.FinalizeError) as exc:
        print(f"Claude FW-G 官方复测派生失败：{exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "result": "passed",
                "campaign_id": CAMPAIGN_ID,
                "probe_count": EXPECTED_PROBES,
                "required_rule_count": EXPECTED_REQUIRED_RULES,
                "profile_atomic_assertion_count": EXPECTED_PROFILE_ASSERTIONS,
                "scenario_only_assertion_count": EXPECTED_SCENARIO_ASSERTIONS,
                "candidate_count": EXPECTED_CANDIDATES,
                "approval_issued": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
