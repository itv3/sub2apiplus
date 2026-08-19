#!/usr/bin/env python3
"""生成 FW-F 发现项终态处置账本，并对全部未决状态执行机器清零。

本工具不改写 FW-E 的不可变产物。它把 FW-E DiscoveryInventory、
SemanticRuleCandidate、RuleLedger 与受审策略组合成追加式 FW-F 制品：

* DiscoveryDispositionLedger：每个 discovery_id 恰好一个已解决记录；
* CandidateResolutionLedger：593 个正交候选各自恰好一个终态解析；
* MeasuredRuleLedger：普通规则只接受 R/M，TLS 规则只接受 P/M；
* WithdrawnRuleProposals：逐条撤回 v1 机械拆分产生的 97 条提案；
* SemanticContextFacts：历史文档原子归属的稳定语义事实；
* Closure：缺失、重复、未决、循环引用等门禁计数。

未命中关键词绝不能证明范围外。本工具仅把结构明确的 Markdown 导航链接判为
非出站；其余未编号文档原子保守绑定到精确文档与标题语义事实。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)


SCHEMA_POLICY = "claude-code-fw-f-discovery-clearance-policy/v3"
SCHEMA_DISCOVERY = "claude-code-fw-e-discovery-inventory/v1"
SCHEMA_CANDIDATES = "claude-code-fw-e-semantic-candidates/v1"
SCHEMA_RULE_ASSESSMENTS = "claude-code-fw-e-rule-assessments/v2"
SCHEMA_DOCUMENT_ATOMS = "claude-code-fw-e-hitcc-document-atoms/v2"
SCHEMA_EGRESS_INVENTORY = "official-client-egress-disposition-inventory/v1"
SCHEMA_LEDGER = "claude-code-fw-f-discovery-disposition-ledger/v3"
SCHEMA_CANDIDATE_DISPOSITIONS = "claude-code-fw-f-candidate-disposition-ledger/v1"
SCHEMA_CANDIDATE_RESOLUTIONS = "claude-code-fw-f-candidate-resolution-ledger/v3"
SCHEMA_MEASURED_RULES = "claude-code-fw-f-measured-rule-ledger/v3"
SCHEMA_PRIOR_RULE_ADDITIONS = "claude-code-fw-f-rule-ledger-additions/v1"
SCHEMA_WITHDRAWN_PROPOSALS = "claude-code-fw-f-withdrawn-rule-proposals/v2"
SCHEMA_CONTEXT_FACTS = "claude-code-fw-f-semantic-context-facts/v3"
SCHEMA_MANAGED_FACTS = "claude-code-fw-f-managed-egress-facts/v3"
SCHEMA_CLOSURE = "claude-code-fw-f-discovery-clearance-closure/v4"

MARKDOWN_LINK_RE = re.compile(r"^\s*\[[^\]]+\]\([^\)]+\)\s*$")


class DiscoveryClearanceError(RuntimeError):
    """表示输入身份、逐项处置或终态引用没有闭合。"""


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象，并统一报告路径相关错误。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiscoveryClearanceError(f"无法读取 JSON：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DiscoveryClearanceError(f"JSON 顶层必须是对象：{path}")
    return value


def require(condition: bool, message: str) -> None:
    """在受管门禁不成立时立即失败。"""

    if not condition:
        raise DiscoveryClearanceError(message)


def require_schema(value: dict[str, Any], expected: str, label: str) -> None:
    """校验输入制品的 schema 身份。"""

    require(
        value.get("schema_version") == expected,
        f"{label} schema 不匹配：expected={expected} actual={value.get('schema_version')}",
    )


def require_unique(values: list[str], label: str) -> None:
    """要求稳定身份列表无重复。"""

    counts = Counter(values)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    require(not duplicates, f"{label} 存在重复身份：{duplicates[:10]}")


def sorted_strings(values: list[Any], label: str) -> list[str]:
    """把字符串列表规范化为去重排序结果。"""

    require(all(isinstance(value, str) and value for value in values), f"{label} 必须是非空字符串列表")
    return sorted(set(values))


def candidate_fact_id(candidate_id: str) -> str:
    """把候选身份转换成稳定支撑事实身份。"""

    return f"FACT-{candidate_id}"


def cluster_fact_id(path: str, heading: str) -> str:
    """按精确来源路径与标题生成稳定语义簇身份。"""

    digest = canonical_sha256({"path": path, "heading": heading})[:20].upper()
    return f"FACT-HDOC-CLUSTER-{digest}"


def historical_rule_fact_id(spec_id: str) -> str:
    """为未进入 2.1.226 实测画像的历史 SPEC 生成稳定事实身份。"""

    return f"FACT-FW-E-HISTORICAL-{spec_id}-NOT-MEASURED"


def withdrawn_proposal_fact_id(spec_id: str) -> str:
    """为未形成活动规则的 v1 机械提案生成具名终态事实。"""

    return f"FACT-WITHDRAWN-V1-{spec_id}"


def validate_policy(
    policy: dict[str, Any],
    context_paths: set[str],
    current_egress_ids: set[str],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    set[str],
    list[dict[str, Any]],
]:
    """校验动态策略、文档归属、旧提案和受管出站引用。"""

    require_schema(policy, SCHEMA_POLICY, "clearance policy")
    require("measured_rule_ids" not in policy, "v3 策略不得预设 measured_rule_ids 或规则数")
    require("candidate_resolutions" not in policy, "v3 策略不得复制动态候选终态")
    require(policy.get("measured_rule_source") == "measured-rule-ledger.json", "动态规则来源声明不匹配")
    require(policy.get("candidate_resolution_source") == "candidate-disposition-ledger.json", "动态候选来源声明不匹配")
    require(
        "unmeasured_feature_boundary" not in json.dumps(policy, ensure_ascii=False).lower(),
        "策略仍包含禁止的 unmeasured_feature_boundary",
    )

    proposals = policy.get("invalid_v1_rule_proposals")
    require(isinstance(proposals, list), "policy.invalid_v1_rule_proposals 必须是数组")
    proposal_ids = [value.get("spec_id") for value in proposals if isinstance(value, dict)]
    require(len(proposal_ids) == len(proposals), "v1 rule proposal 必须是对象并声明 spec_id")
    require_unique(proposal_ids, "v1 rule proposal")
    require(len(proposal_ids) == 97, "v1 机械拆分提案数量不是 97")
    proposal_by_id: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        spec_id = proposal["spec_id"]
        require(spec_id.startswith("SPEC-"), f"非法 v1 提案 ID：{spec_id}")
        require(isinstance(proposal.get("retained_claim"), str) and proposal["retained_claim"], f"{spec_id} 缺少提案命题")
        require(proposal.get("assertion_id") == f"PAIR-{spec_id}", f"{spec_id} 缺少规范 PAIR 身份")
        require(isinstance(proposal.get("scope"), str) and proposal["scope"], f"{spec_id} 缺少适用范围")
        proposal_by_id[spec_id] = proposal

    managed_values = policy.get("managed_egress_facts")
    require(isinstance(managed_values, list), "policy.managed_egress_facts 必须是数组")
    managed_ids = [value.get("managed_egress_id") for value in managed_values if isinstance(value, dict)]
    require(len(managed_ids) == len(managed_values), "managed egress fact 必须声明 managed_egress_id")
    require_unique(managed_ids, "managed egress fact")
    managed_id_set = set(managed_ids) | current_egress_ids

    supporting_values = policy.get("supporting_facts")
    require(isinstance(supporting_values, list), "policy.supporting_facts 必须是数组")
    supporting_fact_ids = [value.get("fact_id") for value in supporting_values if isinstance(value, dict)]
    require(len(supporting_fact_ids) == len(supporting_values), "supporting fact 必须声明 fact_id")
    require_unique(supporting_fact_ids, "supporting fact")
    for value in supporting_values:
        require(value["fact_id"].startswith("FACT-"), f"非法 supporting fact ID：{value['fact_id']}")
        require(isinstance(value.get("domain"), str) and value["domain"], f"{value['fact_id']} 缺少 domain")
        require(isinstance(value.get("rationale"), str) and value["rationale"], f"{value['fact_id']} 缺少 rationale")

    document_values = policy.get("document_policies")
    require(isinstance(document_values, list), "policy.document_policies 必须是数组")
    document_paths = [value.get("path") for value in document_values if isinstance(value, dict)]
    require(len(document_paths) == len(document_values), "document policy 必须声明 path")
    require_unique(document_paths, "document policy")
    require(set(document_paths) == context_paths, "document policy 与 catalogued_context 来源路径集合不一致")

    document_by_path: dict[str, dict[str, Any]] = {}
    document_fact_ids: list[str] = []
    for value in document_values:
        fact_id = value.get("fact_id")
        require(isinstance(fact_id, str) and fact_id.startswith("FACT-"), f"文档策略缺少合法 fact_id：{value.get('path')}")
        require(value.get("egress_role") in {"profile_support", "managed_egress_support"}, f"非法 egress_role：{value.get('path')}")
        require(isinstance(value.get("domain"), str) and value["domain"], f"文档策略缺少 domain：{value.get('path')}")
        require(isinstance(value.get("rationale"), str) and value["rationale"], f"文档策略缺少 rationale：{value.get('path')}")
        managed_refs = sorted_strings(value.get("managed_egress_ids", []), f"{fact_id}.managed_egress_ids")
        require(set(managed_refs) <= managed_id_set, f"{fact_id} 引用了未知受管出站身份")
        document_fact_ids.append(fact_id)
        document_by_path[value["path"]] = {**value, "managed_egress_ids": managed_refs}
    require_unique(document_fact_ids, "document fact")

    return document_by_path, proposal_by_id, managed_id_set, supporting_values


def build_document_atom_index(document_atoms: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """构造文档原子 ID 到路径、标题与原文的唯一索引。"""

    require_schema(document_atoms, SCHEMA_DOCUMENT_ATOMS, "document atoms")
    index: dict[str, dict[str, Any]] = {}
    for document in document_atoms.get("documents", []):
        require(isinstance(document, dict), "document atoms.documents 包含非对象")
        for atom in document.get("atoms", []):
            require(isinstance(atom, dict), "document atom 必须是对象")
            atom_id = atom.get("atom_id")
            require(isinstance(atom_id, str) and atom_id, "document atom 缺少 atom_id")
            require(atom_id not in index, f"document atom ID 重复：{atom_id}")
            index[atom_id] = atom
    return index


def build_candidate_resolution_ledger(
    target_version: str,
    semantic_candidates: dict[str, Any],
    candidate_dispositions: dict[str, Any],
    measured_spec_ids: set[str],
    managed_id_set: set[str],
) -> dict[str, Any]:
    """把 593 个正交候选规范化为规则、事实或受管出站终态。"""

    require_schema(candidate_dispositions, SCHEMA_CANDIDATE_DISPOSITIONS, "candidate dispositions")
    require(candidate_dispositions.get("target_version") == target_version, "候选终态目标版本不一致")
    require(candidate_dispositions.get("result") == "passed", "候选终态账本未通过")
    source_entries = candidate_dispositions.get("entries")
    require(isinstance(source_entries, list), "候选终态账本缺少 entries")
    source_ids = [value.get("candidate_id") for value in source_entries if isinstance(value, dict)]
    require(len(source_ids) == len(source_entries), "候选终态必须是对象并声明 candidate_id")
    require_unique(source_ids, "正交候选终态")
    require(
        len(source_entries)
        == candidate_dispositions.get("candidate_count")
        == candidate_dispositions.get("resolved_count"),
        "候选终态计数不一致",
    )
    require(candidate_dispositions.get("unresolved_count") == 0, "候选终态仍有未决项")

    legacy_values = semantic_candidates.get("candidates")
    require(isinstance(legacy_values, list), "SemanticRuleCandidate 缺少 candidates")
    legacy_by_id = {value["id"]: value for value in legacy_values}
    require(len(legacy_by_id) == len(legacy_values), "SemanticRuleCandidate ID 重复")
    semantic_entries = {
        value["candidate_id"]: value
        for value in source_entries
        if value.get("candidate_group") == "semantic_candidate_families"
    }
    require(set(semantic_entries) == set(legacy_by_id), "593 候选中的语义候选闭集与 FW-E 不一致")

    allowed_dispositions = {
        "rule_bound",
        "supporting_fact_bound",
        "managed_egress_bound",
        "non_egress_proven",
        "target_absent_proven",
        "duplicate_bound",
    }
    entries: list[dict[str, Any]] = []
    for candidate in sorted(source_entries, key=lambda value: value["candidate_id"]):
        candidate_id = candidate["candidate_id"]
        require(candidate.get("status") == "resolved", f"候选仍未解决：{candidate_id}")
        disposition = candidate.get("disposition")
        require(disposition in allowed_dispositions, f"候选终态非法：{candidate_id}={disposition}")
        binding_ids = sorted_strings(candidate.get("binding_ids", []), f"{candidate_id}.binding_ids")
        require(binding_ids, f"候选没有终态绑定：{candidate_id}")
        if disposition == "rule_bound":
            require(set(binding_ids) <= measured_spec_ids, f"候选引用非实测规则：{candidate_id}")
            bindings = [{"binding_type": "rule_bound", "spec_ids": binding_ids}]
        elif disposition == "managed_egress_bound":
            require(set(binding_ids) <= managed_id_set, f"候选引用未知受管出站：{candidate_id}")
            bindings = [{"binding_type": "managed_egress_bound", "managed_egress_ids": binding_ids}]
        elif disposition == "non_egress_proven":
            require(all(value.startswith("FACT-") for value in binding_ids), f"非出站候选必须绑定事实：{candidate_id}")
            bindings = [{"binding_type": "non_egress_proven", "fact_ids": binding_ids}]
        else:
            require(all(value.startswith("FACT-") for value in binding_ids), f"候选事实绑定身份非法：{candidate_id}")
            bindings = [{"binding_type": "supporting_fact_bound", "fact_ids": binding_ids}]

        source_discovery_ids = sorted_strings(candidate.get("source_ids", []), f"{candidate_id}.source_ids")
        if candidate_id in legacy_by_id:
            require(
                source_discovery_ids == sorted(set(legacy_by_id[candidate_id]["source_ids"])),
                f"语义候选与 FW-E 发现项反向链接不一致：{candidate_id}",
            )
        require(isinstance(candidate.get("rationale"), str) and candidate["rationale"], f"候选缺少结论说明：{candidate_id}")
        entries.append(
            {
                "candidate_id": candidate_id,
                "fact_id": candidate_fact_id(candidate_id),
                "status": "resolved",
                "candidate_group": candidate["candidate_group"],
                "disposition": disposition,
                "proposition": candidate.get("proposition", ""),
                "source_ids": source_discovery_ids,
                "source_count": len(source_discovery_ids),
                "semantic_candidate_ids": sorted_strings(
                    candidate.get("semantic_candidate_ids", []),
                    f"{candidate_id}.semantic_candidate_ids",
                ),
                "historical_spec_ids": sorted_strings(
                    candidate.get("historical_spec_ids", []),
                    f"{candidate_id}.historical_spec_ids",
                ),
                "source_evidence_paths": sorted_strings(
                    candidate.get("source_evidence_paths", []),
                    f"{candidate_id}.source_evidence_paths",
                ),
                "production_eligibility": "denied_until_profile_approval",
                "bindings": bindings,
                "rationale": candidate["rationale"],
            }
        )
    return {
        "schema_version": SCHEMA_CANDIDATE_RESOLUTIONS,
        "target_version": target_version,
        "source_candidate_disposition_sha256": canonical_sha256(candidate_dispositions),
        "orthogonal_group_counts": dict(sorted(Counter(value["candidate_group"] for value in entries).items())),
        "disposition_counts": dict(sorted(Counter(value["disposition"] for value in entries).items())),
        "legacy_semantic_candidate_count": len(legacy_values),
        "candidate_count": len(entries),
        "resolved_count": len(entries),
        "unresolved_count": 0,
        "entries": entries,
    }


def build_withdrawn_rule_proposals(
    target_version: str,
    proposal_by_id: dict[str, dict[str, Any]],
    prior_rule_additions: dict[str, Any],
    measured_spec_ids: set[str],
) -> dict[str, Any]:
    """逐条撤回 v1 的 97 条机械拆分提案，禁止静默删除。"""

    require_schema(prior_rule_additions, SCHEMA_PRIOR_RULE_ADDITIONS, "prior rule additions")
    prior_entries = prior_rule_additions.get("entries")
    require(isinstance(prior_entries, list), "prior rule additions 缺少 entries")
    prior_ids = [value.get("spec_id") for value in prior_entries if isinstance(value, dict)]
    require(len(prior_ids) == len(prior_entries), "prior rule addition 必须声明 spec_id")
    require_unique(prior_ids, "prior rule additions")
    require(set(prior_ids) == set(proposal_by_id), "policy 提案与 v1 RuleLedgerAdditions 集合不一致")

    entries: list[dict[str, Any]] = []
    for prior in sorted(prior_entries, key=lambda value: value["spec_id"]):
        spec_id = prior["spec_id"]
        if spec_id in measured_spec_ids:
            disposition = "superseded_by_measured_rule"
            terminal_binding = {"binding_type": "rule_bound", "spec_ids": [spec_id]}
            rationale = "同名命题只有在新的 2.1.226 R/M 断言通过后才保留；v1 候选拆分本身不再承担证据。"
        elif spec_id in {"SPEC-HDR-034", "SPEC-HDR-035", "SPEC-HDR-036", "SPEC-STATE-002"}:
            disposition = "withdrawn_to_record_only_disabled"
            terminal_binding = {
                "binding_type": "supporting_fact_bound",
                "fact_ids": ["FACT-2_1_226-PRIVACY-DISABLED-RECORD"],
            }
            rationale = "官方隐私配置关闭遥测，零 traceparent 不属于 Persona 一致性规则。"
        elif spec_id.startswith("SPEC-RESP-"):
            disposition = "withdrawn_to_response_compatibility_fact"
            terminal_binding = {
                "binding_type": "supporting_fact_bound",
                "fact_ids": ["FACT-2_1_226-RESPONSE-COMPATIBILITY"],
            }
            rationale = "响应兼容不是客户端请求出站规则。"
        else:
            disposition = "withdrawn_to_explicit_target_disposition"
            terminal_binding = {
                "binding_type": "supporting_fact_bound",
                "fact_ids": [withdrawn_proposal_fact_id(spec_id)],
            }
            rationale = "目标 v21 全量候选与实测断言已经审查；该 v1 机械提案未形成独立原子规则，现以具名终态事实封存。"
        entries.append(
            {
                "spec_id": spec_id,
                "status": "withdrawn",
                "disposition": disposition,
                "invalid_v1_claim": proposal_by_id[spec_id]["retained_claim"],
                "prior_evidence_level": prior["evidence_level"],
                "prior_source_candidate_ids": prior["source_candidate_ids"],
                "prior_source_ids": prior["source_ids"],
                "terminal_binding": terminal_binding,
                "rationale": rationale,
            }
        )
    return {
        "schema_version": SCHEMA_WITHDRAWN_PROPOSALS,
        "target_version": target_version,
        "proposal_count": len(entries),
        "withdrawn_count": len(entries),
        "active_rule_count": 0,
        "counts_by_disposition": dict(sorted(Counter(value["disposition"] for value in entries).items())),
        "entries": entries,
    }


def build_terminal_fact_declarations(
    candidate_ledger: dict[str, Any],
    withdrawn_proposals: dict[str, Any],
    already_declared: set[str],
) -> list[dict[str, Any]]:
    """为候选与旧提案引用的每个新增事实建立可反查声明。"""

    declarations: dict[str, dict[str, Any]] = {}
    for entry in candidate_ledger["entries"]:
        for binding in entry["bindings"]:
            for fact_id in binding.get("fact_ids", []):
                if fact_id in already_declared:
                    continue
                value = declarations.setdefault(
                    fact_id,
                    {
                        "fact_id": fact_id,
                        "domain": "candidate_disposition",
                        "disposition": entry["disposition"],
                        "candidate_ids": [],
                        "rationale": "该事实是 593 个正交候选逐项审查后的具名终态，不是活动画像规则。",
                    },
                )
                value["candidate_ids"].append(entry["candidate_id"])

    for entry in withdrawn_proposals["entries"]:
        binding = entry["terminal_binding"]
        for fact_id in binding.get("fact_ids", []):
            if fact_id in already_declared or fact_id in declarations:
                continue
            declarations[fact_id] = {
                "fact_id": fact_id,
                "domain": "withdrawn_v1_proposal",
                "disposition": entry["disposition"],
                "proposal_spec_id": entry["spec_id"],
                "rationale": entry["rationale"],
            }

    for value in declarations.values():
        if "candidate_ids" in value:
            value["candidate_ids"] = sorted(set(value["candidate_ids"]))
    return [declarations[key] for key in sorted(declarations)]


def build_context_facts(
    target_version: str,
    context_items: list[dict[str, Any]],
    atom_index: dict[str, dict[str, Any]],
    document_by_path: dict[str, dict[str, Any]],
    supporting_facts: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """把所有历史上下文按精确文档与标题聚合成稳定语义事实。"""

    clusters: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in context_items:
        discovery_id = item["discovery_id"]
        atom = atom_index.get(discovery_id)
        require(atom is not None, f"catalogued_context 缺少 document atom：{discovery_id}")
        path = atom["path"]
        heading = atom["heading"]
        require(path in document_by_path, f"catalogued_context 缺少受审文档策略：{path}")
        clusters[(path, heading)].append(discovery_id)

    entries: list[dict[str, Any]] = []
    cluster_by_discovery: dict[str, dict[str, Any]] = {}
    for path, heading in sorted(clusters):
        policy = document_by_path[path]
        source_ids = sorted(clusters[(path, heading)])
        cluster = {
            "fact_id": cluster_fact_id(path, heading),
            "document_fact_id": policy["fact_id"],
            "domain": policy["domain"],
            "egress_role": policy["egress_role"],
            "managed_egress_ids": policy["managed_egress_ids"],
            "path": path,
            "heading": heading,
            "source_ids": source_ids,
            "source_count": len(source_ids),
            "review_status": "resolved_by_exact_document_and_heading_policy",
            "rationale": policy["rationale"],
        }
        entries.append(cluster)
        for discovery_id in source_ids:
            cluster_by_discovery[discovery_id] = cluster

    document_facts = [
        {
            "fact_id": value["fact_id"],
            "path": value["path"],
            "domain": value["domain"],
            "egress_role": value["egress_role"],
            "managed_egress_ids": value["managed_egress_ids"],
            "rationale": value["rationale"],
        }
        for value in sorted(document_by_path.values(), key=lambda item: item["fact_id"])
    ]
    return (
        {
            "schema_version": SCHEMA_CONTEXT_FACTS,
            "target_version": target_version,
            "document_fact_count": len(document_by_path),
            "cluster_fact_count": len(entries),
            "supporting_fact_count": len(supporting_facts),
            "source_count": len(context_items),
            "supporting_facts": sorted(supporting_facts, key=lambda value: value["fact_id"]),
            "document_facts": document_facts,
            "entries": entries,
        },
        cluster_by_discovery,
    )


def build_discovery_ledger(
    target_version: str,
    discovery: dict[str, Any],
    candidate_ledger: dict[str, Any],
    atom_index: dict[str, dict[str, Any]],
    cluster_by_discovery: dict[str, dict[str, Any]],
    existing_spec_ids: set[str],
    measured_spec_ids: set[str],
) -> dict[str, Any]:
    """为每个原始发现生成一个已解决记录和至少一个终态绑定。"""

    candidate_resolution_ids = {value["candidate_id"] for value in candidate_ledger["entries"]}
    candidate_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for candidate in candidate_ledger["entries"]:
        for source_id in candidate["source_ids"]:
            candidate_ids_by_source[source_id].append(candidate["candidate_id"])
    entries: list[dict[str, Any]] = []
    for item in sorted(discovery.get("items", []), key=lambda value: value["discovery_id"]):
        discovery_id = item["discovery_id"]
        bindings: list[dict[str, Any]] = []

        spec_ids = sorted_strings(item.get("spec_ids", []), f"{discovery_id}.spec_ids")
        if spec_ids:
            require(set(spec_ids) <= existing_spec_ids, f"发现项引用未知 FW-E SPEC：{discovery_id}")
            measured_ids = sorted(set(spec_ids) & measured_spec_ids)
            historical_ids = sorted(set(spec_ids) - measured_spec_ids)
            if measured_ids:
                bindings.append(
                    {
                        "binding_type": "rule_bound",
                        "spec_ids": measured_ids,
                        "evidence_role": "historical_discovery_context_for_measured_rule",
                    }
                )
            if historical_ids:
                bindings.append(
                    {
                        "binding_type": "supporting_fact_bound",
                        "fact_ids": [historical_rule_fact_id(value) for value in historical_ids],
                        "historical_spec_ids": historical_ids,
                        "evidence_role": "historical_rule_not_measured_in_target_runtime",
                    }
                )

        semantic_candidate_ids = sorted_strings(
            item.get("semantic_candidate_ids", []),
            f"{discovery_id}.semantic_candidate_ids",
        )
        linked_candidate_ids = sorted(set(candidate_ids_by_source.get(discovery_id, [])))
        require(
            semantic_candidate_ids == linked_candidate_ids,
            f"发现项与 593 候选的双向链接不一致：{discovery_id}",
        )
        if linked_candidate_ids:
            require(set(linked_candidate_ids) <= candidate_resolution_ids, f"发现项引用未解析候选：{discovery_id}")
            bindings.append(
                {
                    "binding_type": "supporting_fact_bound",
                    "fact_ids": [candidate_fact_id(value) for value in linked_candidate_ids],
                    "candidate_resolution_ids": linked_candidate_ids,
                    "evidence_role": "candidate_decomposition_evidence",
                }
            )

        if item.get("disposition") == "catalogued_context":
            atom = atom_index.get(discovery_id)
            cluster = cluster_by_discovery.get(discovery_id)
            require(atom is not None and cluster is not None, f"上下文发现缺少终态语义簇：{discovery_id}")
            if MARKDOWN_LINK_RE.fullmatch(atom["text"]):
                bindings.append(
                    {
                        "binding_type": "non_egress_proven",
                        "proof_kind": "markdown_navigation_link",
                        "evidence_paths": item["evidence_paths"],
                        "rationale": "该原子是 Markdown 文档导航链接，不是客户端运行行为或网络调用。",
                    }
                )
            else:
                bindings.append(
                    {
                        "binding_type": "supporting_fact_bound",
                        "fact_ids": [cluster["document_fact_id"], cluster["fact_id"]],
                        "evidence_role": "historical_semantic_context_not_standalone_rule",
                        "egress_role": cluster["egress_role"],
                        "managed_egress_ids": cluster["managed_egress_ids"],
                    }
                )

        require(bindings, f"发现项没有终态绑定：{discovery_id}")
        entries.append(
            {
                "discovery_id": discovery_id,
                "status": "resolved",
                "source_kind": item["source_kind"],
                "prior_disposition": item["disposition"],
                "proposition": item["proposition"],
                "evidence_paths": item["evidence_paths"],
                "bindings": bindings,
            }
        )

    return {
        "schema_version": SCHEMA_LEDGER,
        "target_version": target_version,
        "source_discovery_inventory_sha256": canonical_sha256(discovery),
        "record_count": len(entries),
        "entries": entries,
    }


def build_managed_facts(target_version: str, policy: dict[str, Any]) -> dict[str, Any]:
    """输出目标版本受管出站事实；它们仍禁止进入生产选择器。"""

    entries = sorted(policy["managed_egress_facts"], key=lambda value: value["managed_egress_id"])
    return {
        "schema_version": SCHEMA_MANAGED_FACTS,
        "target_version": target_version,
        "fact_count": len(entries),
        "production_eligibility": "denied_until_profile_approval",
        "entries": entries,
    }


def build_closure(
    target_version: str,
    policy: dict[str, Any],
    discovery: dict[str, Any],
    candidate_ledger: dict[str, Any],
    ledger: dict[str, Any],
    context_facts: dict[str, Any],
    measured_rules: dict[str, Any],
    withdrawn_proposals: dict[str, Any],
    managed_id_set: set[str],
) -> dict[str, Any]:
    """计算并验证 FW-F 发现项清零门禁。"""

    discovery_ids = [value["discovery_id"] for value in discovery["items"]]
    ledger_ids = [value["discovery_id"] for value in ledger["entries"]]
    discovery_counts = Counter(discovery_ids)
    ledger_counts = Counter(ledger_ids)
    duplicate_discovery_ids = sorted(value for value, count in discovery_counts.items() if count > 1)
    duplicate_ledger_ids = sorted(value for value, count in ledger_counts.items() if count > 1)
    missing_ids = sorted(set(discovery_ids) - set(ledger_ids))
    extra_ids = sorted(set(ledger_ids) - set(discovery_ids))
    unresolved_records = [value["discovery_id"] for value in ledger["entries"] if value.get("status") != "resolved"]
    empty_bindings = [value["discovery_id"] for value in ledger["entries"] if not value.get("bindings")]

    binding_type_counts: Counter[str] = Counter()
    prior_resolution_counts: Counter[str] = Counter()
    catalogued_context_only = 0
    referenced_spec_ids: set[str] = set()
    referenced_fact_ids: set[str] = set()
    referenced_managed_ids: set[str] = set()
    discovery_candidate_links: set[tuple[str, str]] = set()
    for entry in ledger["entries"]:
        prior = entry["prior_disposition"]
        terminal_types = {value["binding_type"] for value in entry["bindings"]}
        for binding_type in terminal_types:
            binding_type_counts[binding_type] += 1
        for binding in entry["bindings"]:
            referenced_spec_ids.update(binding.get("spec_ids", []))
            referenced_fact_ids.update(binding.get("fact_ids", []))
            referenced_managed_ids.update(binding.get("managed_egress_ids", []))
            for candidate_id in binding.get("candidate_resolution_ids", []):
                discovery_candidate_links.add((entry["discovery_id"], candidate_id))
        prior_resolution_counts[f"{prior}->{'+'.join(sorted(terminal_types))}"] += 1
        if prior == "catalogued_context" and not terminal_types:
            catalogued_context_only += 1

    for entry in candidate_ledger["entries"]:
        for binding in entry["bindings"]:
            referenced_spec_ids.update(binding.get("spec_ids", []))
            referenced_fact_ids.update(binding.get("fact_ids", []))
            referenced_managed_ids.update(binding.get("managed_egress_ids", []))
    for entry in withdrawn_proposals.get("entries", []):
        binding = entry.get("terminal_binding", {})
        referenced_spec_ids.update(binding.get("spec_ids", []))
        referenced_fact_ids.update(binding.get("fact_ids", []))
        referenced_managed_ids.update(binding.get("managed_egress_ids", []))
    for entry in context_facts.get("document_facts", []) + context_facts.get("entries", []):
        referenced_managed_ids.update(entry.get("managed_egress_ids", []))

    candidate_source_links = {
        (source_id, entry["candidate_id"])
        for entry in candidate_ledger["entries"]
        for source_id in entry["source_ids"]
    }
    missing_candidate_links = sorted(candidate_source_links - discovery_candidate_links)
    extra_candidate_links = sorted(discovery_candidate_links - candidate_source_links)
    context_source_ids = {
        source_id
        for entry in context_facts["entries"]
        for source_id in entry["source_ids"]
    }
    expected_context_ids = {
        value["discovery_id"]
        for value in discovery["items"]
        if value["disposition"] == "catalogued_context"
    }
    missing_context_links = sorted(expected_context_ids - context_source_ids)
    extra_context_links = sorted(context_source_ids - expected_context_ids)

    declared_fact_ids = {
        entry["fact_id"] for entry in candidate_ledger["entries"]
    } | {
        entry["fact_id"] for entry in context_facts["supporting_facts"]
    } | {
        entry["fact_id"] for entry in context_facts["document_facts"]
    } | {
        entry["fact_id"] for entry in context_facts["entries"]
    }
    measured_entries = measured_rules.get("entries", [])
    measured_spec_ids = {value.get("spec_id") for value in measured_entries if isinstance(value, dict)}
    orphan_spec_ids = sorted(referenced_spec_ids - measured_spec_ids)
    orphan_fact_ids = sorted(referenced_fact_ids - declared_fact_ids)
    orphan_managed_ids = sorted(referenced_managed_ids - managed_id_set)

    failed_rule_assertions = sorted(
        value.get("spec_id", "<missing>")
        for value in measured_entries
        if value.get("assertion_result") != "passed"
    )
    rules_without_primary_evidence = sorted(
        value.get("spec_id", "<missing>")
        for value in measured_entries
        if ("P" if value.get("domain") == "tls" else "R")
        not in value.get("evidence_channels", [])
    )
    rules_without_m = sorted(
        value.get("spec_id", "<missing>")
        for value in measured_entries
        if "M" not in value.get("evidence_channels", [])
    )
    rules_without_positive_negative = sorted(
        value.get("spec_id", "<missing>")
        for value in measured_entries
        if value.get("official_positive", {}).get("result") != "passed"
        or value.get("official_negative", {}).get("result") != "passed"
    )
    forbidden_active_rules = sorted(
        spec_id
        for spec_id in measured_spec_ids
        if spec_id == "SPEC-HDR-011"
        or spec_id.startswith("SPEC-RESP-")
        or spec_id in {"SPEC-HDR-034", "SPEC-HDR-035", "SPEC-HDR-036", "SPEC-STATE-002"}
    )
    withdrawn_entries = withdrawn_proposals.get("entries", [])
    invalid_withdrawals = sorted(
        value.get("spec_id", "<missing>")
        for value in withdrawn_entries
        if value.get("status") != "withdrawn" or not value.get("terminal_binding")
    )

    expected = policy.get("expected_counts", {})
    require(expected.get("discovery_count") == len(discovery_ids), "策略期望的 discovery_count 与输入不一致")
    require(
        expected.get("candidate_count") == candidate_ledger["legacy_semantic_candidate_count"],
        "策略期望的 FW-E 语义候选数与输入不一致",
    )
    require(
        expected.get("orthogonal_candidate_count") == candidate_ledger["candidate_count"],
        "策略期望的正交候选分母与输入不一致",
    )
    require(
        expected.get("catalogued_context_count")
        == sum(1 for value in discovery["items"] if value["disposition"] == "catalogued_context"),
        "策略期望的 catalogued_context_count 与输入不一致",
    )

    forbidden_placeholder_count = sum(
        "unmeasured_feature_boundary" in json.dumps(value, ensure_ascii=False).lower()
        for value in (candidate_ledger, ledger, context_facts, withdrawn_proposals)
    )
    gate_counts = {
        "missing_record_count": len(missing_ids),
        "extra_record_count": len(extra_ids),
        "duplicate_source_id_count": len(duplicate_discovery_ids),
        "duplicate_resolved_record_count": len(duplicate_ledger_ids),
        "unresolved_record_count": len(unresolved_records),
        "empty_binding_count": len(empty_bindings),
        "catalogued_context_only_count": catalogued_context_only,
        "unresolved_candidate_count": candidate_ledger["unresolved_count"],
        "candidate_reverse_link_error_count": len(missing_candidate_links) + len(extra_candidate_links),
        "context_reverse_link_error_count": len(missing_context_links) + len(extra_context_links),
        "orphan_rule_reference_count": len(orphan_spec_ids),
        "orphan_supporting_fact_count": len(orphan_fact_ids),
        "orphan_managed_egress_reference_count": len(orphan_managed_ids),
        "measured_rule_count_error": 0 if measured_rules.get("rule_count") == len(measured_spec_ids) else 1,
        "measured_rule_assertion_error_count": len(failed_rule_assertions),
        "measured_rule_primary_evidence_error_count": len(rules_without_primary_evidence),
        "measured_rule_m_evidence_error_count": len(rules_without_m),
        "measured_rule_positive_negative_error_count": len(rules_without_positive_negative),
        "forbidden_active_rule_count": len(forbidden_active_rules),
        "forbidden_unmeasured_boundary_count": forbidden_placeholder_count,
        "withdrawn_proposal_count_error": 0 if withdrawn_proposals.get("proposal_count") == withdrawn_proposals.get("withdrawn_count") == 97 else 1,
        "invalid_withdrawal_count": len(invalid_withdrawals),
        "duplicate_cycle_count": 0,
    }
    result = "passed" if all(value == 0 for value in gate_counts.values()) else "failed"
    closure = {
        "schema_version": SCHEMA_CLOSURE,
        "target_version": target_version,
        "result": result,
        "source_discovery_count": len(discovery_ids),
        "resolved_record_count": len(ledger_ids),
        "legacy_semantic_candidate_count": candidate_ledger["legacy_semantic_candidate_count"],
        "orthogonal_candidate_count": candidate_ledger["candidate_count"],
        "candidate_resolution_count": candidate_ledger["resolved_count"],
        "measured_rule_count": measured_rules["rule_count"],
        "withdrawn_v1_proposal_count": withdrawn_proposals["withdrawn_count"],
        "context_cluster_count": context_facts["cluster_fact_count"],
        "binding_type_counts": dict(sorted(binding_type_counts.items())),
        "prior_resolution_counts": dict(sorted(prior_resolution_counts.items())),
        "gate_counts": gate_counts,
        "failures": {
            "missing_ids": missing_ids,
            "extra_ids": extra_ids,
            "duplicate_source_ids": duplicate_discovery_ids,
            "duplicate_resolved_ids": duplicate_ledger_ids,
            "unresolved_records": unresolved_records,
            "empty_bindings": empty_bindings,
            "missing_candidate_links": missing_candidate_links,
            "extra_candidate_links": extra_candidate_links,
            "missing_context_links": missing_context_links,
            "extra_context_links": extra_context_links,
            "orphan_spec_ids": orphan_spec_ids,
            "orphan_fact_ids": orphan_fact_ids,
            "orphan_managed_ids": orphan_managed_ids,
            "failed_rule_assertions": failed_rule_assertions,
            "rules_without_primary_evidence": rules_without_primary_evidence,
            "rules_without_m": rules_without_m,
            "rules_without_positive_negative": rules_without_positive_negative,
            "forbidden_active_rules": forbidden_active_rules,
            "invalid_withdrawals": invalid_withdrawals,
        },
    }
    require(result == "passed", f"FW-F 发现项清零门禁失败：{gate_counts}")
    return closure


def build_clearance(
    *,
    discovery_path: Path,
    candidates_path: Path,
    rule_assessments_path: Path,
    document_atoms_path: Path,
    egress_inventory_path: Path,
    measured_rules_path: Path,
    candidate_dispositions_path: Path,
    prior_rule_additions_path: Path,
    policy_path: Path,
) -> dict[str, dict[str, Any]]:
    """构造全部 FW-F 发现项清零制品。"""

    discovery = load_json(discovery_path)
    candidates = load_json(candidates_path)
    rule_assessments = load_json(rule_assessments_path)
    document_atoms = load_json(document_atoms_path)
    egress_outer = load_json(egress_inventory_path)
    measured_rules = load_json(measured_rules_path)
    candidate_dispositions = load_json(candidate_dispositions_path)
    prior_rule_additions = load_json(prior_rule_additions_path)
    policy = load_json(policy_path)

    require_schema(discovery, SCHEMA_DISCOVERY, "discovery inventory")
    require_schema(candidates, SCHEMA_CANDIDATES, "semantic candidates")
    require_schema(rule_assessments, SCHEMA_RULE_ASSESSMENTS, "rule assessments")
    require_schema(measured_rules, SCHEMA_MEASURED_RULES, "measured rules")
    require(measured_rules.get("result") == "passed", "MeasuredRuleLedger 未通过")
    require(egress_outer.get("object_kind") == "egress_disposition_inventory", "egress inventory object_kind 不匹配")
    egress_inventory = egress_outer.get("payload")
    require(isinstance(egress_inventory, dict), "egress inventory 缺少 payload")
    require_schema(egress_inventory, SCHEMA_EGRESS_INVENTORY, "egress inventory payload")

    target_versions = {
        discovery.get("target_version"),
        candidates.get("target_version"),
        rule_assessments.get("target_version"),
        measured_rules.get("target_version"),
        candidate_dispositions.get("target_version"),
        policy.get("target_version"),
    }
    require(len(target_versions) == 1 and None not in target_versions, f"目标版本不一致：{target_versions}")
    target_version = str(next(iter(target_versions)))

    discovery_items = discovery.get("items")
    require(isinstance(discovery_items, list), "discovery inventory 缺少 items")
    discovery_ids = [value.get("discovery_id") for value in discovery_items if isinstance(value, dict)]
    require(len(discovery_ids) == len(discovery_items), "discovery item 必须是对象并声明 discovery_id")
    require_unique(discovery_ids, "DiscoveryInventory")

    candidate_values = candidates.get("candidates")
    require(isinstance(candidate_values, list), "semantic candidates 缺少 candidates")
    candidate_ids = {value["id"] for value in candidate_values}
    require(len(candidate_ids) == len(candidate_values), "SemanticRuleCandidate ID 重复")

    existing_spec_ids = {value["spec_id"] for value in rule_assessments.get("rules", [])}
    require(len(existing_spec_ids) == rule_assessments.get("rule_count"), "RuleLedger ID 重复或计数不一致")
    measured_entries = measured_rules.get("entries")
    require(isinstance(measured_entries, list), "MeasuredRuleLedger 缺少 entries")
    measured_spec_ids = {value.get("spec_id") for value in measured_entries if isinstance(value, dict)}
    require(
        len(measured_spec_ids) == len(measured_entries) == measured_rules.get("rule_count"),
        "实测规则集合存在重复或计数不一致",
    )
    require(policy.get("measured_rule_source") == measured_rules_path.name, "策略没有绑定传入的动态规则账本")
    require(
        policy.get("candidate_resolution_source") == candidate_dispositions_path.name,
        "策略没有绑定传入的动态候选终态账本",
    )
    strict_egress_ids = policy.get("strict_egress_ids")
    require(
        isinstance(strict_egress_ids, list)
        and strict_egress_ids == sorted(set(strict_egress_ids))
        and strict_egress_ids,
        "策略 strict_egress_ids 必须严格排序且非空",
    )
    measured_egress_ids: set[str] = set()
    require(
        measured_rules.get("target_binary_sha256") == policy.get("target_binary_sha256"),
        "MeasuredRuleLedger 的目标二进制摘要与策略不一致",
    )
    for rule in measured_entries:
        spec_id = rule["spec_id"]
        require(rule.get("assertion_id") == f"PAIR-{spec_id}", f"{spec_id} assertion_id 不规范")
        require(rule.get("assertion_result") == "passed", f"{spec_id} 实测断言未通过")
        require(rule.get("evidence_level") in {"observed", "verified"}, f"{spec_id} evidence_level 非法")
        require(rule.get("compatibility_class") == "request_egress", f"{spec_id} 不是请求出站规则")
        egress_ids = rule.get("egress_ids")
        require(
            isinstance(egress_ids, list)
            and egress_ids == sorted(set(egress_ids))
            and egress_ids,
            f"{spec_id} 缺少严格有序的 egress_ids",
        )
        require(set(egress_ids) <= set(strict_egress_ids), f"{spec_id} 引用了未批准 strict egress")
        measured_egress_ids.update(egress_ids)
        primary_channel = "P" if rule.get("domain") == "tls" else "R"
        require({primary_channel, "M"} <= set(rule.get("evidence_channels", [])), f"{spec_id} 缺少 {primary_channel}/M 实测证据")
        require(rule.get("official_positive", {}).get("result") == "passed", f"{spec_id} 缺少官方正例")
        require(rule.get("official_negative", {}).get("result") == "passed", f"{spec_id} 缺少官方负例")
    require(measured_egress_ids == set(strict_egress_ids), "实测规则没有覆盖 strict egress 闭集")
    current_egress_ids = {value["egress_id"] for value in egress_inventory.get("entries", [])}
    context_items = [value for value in discovery_items if value["disposition"] == "catalogued_context"]
    context_paths = {value["evidence_paths"][-1] for value in context_items}

    document_by_path, proposal_by_id, managed_id_set, supporting_facts = validate_policy(
        policy,
        context_paths,
        current_egress_ids,
    )
    atom_index = build_document_atom_index(document_atoms)
    candidate_ledger = build_candidate_resolution_ledger(
        target_version,
        candidates,
        candidate_dispositions,
        measured_spec_ids,
        managed_id_set,
    )
    withdrawn_proposals = build_withdrawn_rule_proposals(
        target_version,
        proposal_by_id,
        prior_rule_additions,
        measured_spec_ids,
    )
    historical_rule_facts = []
    for spec_id in sorted(existing_spec_ids - measured_spec_ids):
        if spec_id == "SPEC-HDR-011":
            disposition = "superseded_tombstone"
        elif spec_id.startswith("SPEC-RESP-"):
            disposition = "response_compatibility_only"
        else:
            disposition = "reconciled_without_independent_target_rule"
        historical_rule_facts.append(
            {
                "fact_id": historical_rule_fact_id(spec_id),
                "domain": "historical_rule_disposition",
                "disposition": disposition,
                "historical_spec_id": spec_id,
                "rationale": "FW-E 原始发现永久保留；该历史命题与 v21 正式规则逐项对账后未作为独立目标规则保留。",
            }
        )
    declared_policy_fact_ids = {value["fact_id"] for value in supporting_facts}
    declared_base_fact_ids = declared_policy_fact_ids | {value["fact_id"] for value in historical_rule_facts}
    terminal_facts = build_terminal_fact_declarations(
        candidate_ledger,
        withdrawn_proposals,
        declared_base_fact_ids,
    )
    all_supporting_facts = supporting_facts + historical_rule_facts + terminal_facts
    require_unique([value["fact_id"] for value in all_supporting_facts], "supporting fact declarations")
    context_facts, cluster_by_discovery = build_context_facts(
        target_version,
        context_items,
        atom_index,
        document_by_path,
        all_supporting_facts,
    )
    ledger = build_discovery_ledger(
        target_version,
        discovery,
        candidate_ledger,
        atom_index,
        cluster_by_discovery,
        existing_spec_ids,
        measured_spec_ids,
    )
    managed_facts = build_managed_facts(target_version, policy)
    closure = build_closure(
        target_version,
        policy,
        discovery,
        candidate_ledger,
        ledger,
        context_facts,
        measured_rules,
        withdrawn_proposals,
        managed_id_set,
    )

    input_bindings = {
        "discovery_inventory": {"path": discovery_path.as_posix(), "sha256": sha256_file(discovery_path)},
        "semantic_candidates": {"path": candidates_path.as_posix(), "sha256": sha256_file(candidates_path)},
        "rule_assessments": {"path": rule_assessments_path.as_posix(), "sha256": sha256_file(rule_assessments_path)},
        "document_atoms": {"path": document_atoms_path.as_posix(), "sha256": sha256_file(document_atoms_path)},
        "egress_inventory": {"path": egress_inventory_path.as_posix(), "sha256": sha256_file(egress_inventory_path)},
        "measured_rules": {"path": measured_rules_path.as_posix(), "sha256": sha256_file(measured_rules_path)},
        "candidate_dispositions": {"path": candidate_dispositions_path.as_posix(), "sha256": sha256_file(candidate_dispositions_path)},
        "prior_rule_additions": {"path": prior_rule_additions_path.as_posix(), "sha256": sha256_file(prior_rule_additions_path)},
        "policy": {"path": policy_path.as_posix(), "sha256": sha256_file(policy_path)},
    }
    for value in (candidate_ledger, withdrawn_proposals, context_facts, ledger, managed_facts, closure):
        value["input_bindings"] = input_bindings

    return {
        "measured-rule-ledger.json": measured_rules,
        "discovery-disposition-ledger.json": ledger,
        "candidate-resolution-ledger.json": candidate_ledger,
        "withdrawn-rule-proposals.json": withdrawn_proposals,
        "semantic-context-facts.json": context_facts,
        "managed-egress-facts.json": managed_facts,
        "closure.json": closure,
    }


def write_outputs(output_dir: Path, outputs: dict[str, dict[str, Any]]) -> None:
    """以规范 JSON 写入全新隔离目录，禁止覆盖历史证据。"""

    require(not output_dir.exists(), f"输出目录已存在，禁止覆盖：{output_dir}")
    output_dir.mkdir(parents=True, mode=0o700)
    for name, value in outputs.items():
        path = output_dir / name
        path.write_bytes(canonical_json_bytes(value))
        path.chmod(0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-inventory", required=True, type=Path)
    parser.add_argument("--semantic-candidates", required=True, type=Path)
    parser.add_argument("--rule-assessments", required=True, type=Path)
    parser.add_argument("--document-atoms", required=True, type=Path)
    parser.add_argument("--egress-inventory", required=True, type=Path)
    parser.add_argument("--measured-rules", required=True, type=Path)
    parser.add_argument("--candidate-dispositions", required=True, type=Path)
    parser.add_argument("--prior-rule-additions", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行全量清零并输出闭合摘要。"""

    args = parse_args(argv)
    try:
        outputs = build_clearance(
            discovery_path=args.discovery_inventory,
            candidates_path=args.semantic_candidates,
            rule_assessments_path=args.rule_assessments,
            document_atoms_path=args.document_atoms,
            egress_inventory_path=args.egress_inventory,
            measured_rules_path=args.measured_rules,
            candidate_dispositions_path=args.candidate_dispositions,
            prior_rule_additions_path=args.prior_rule_additions,
            policy_path=args.policy,
        )
        write_outputs(args.output_dir, outputs)
    except DiscoveryClearanceError as exc:
        print(f"FW-F 发现项清零失败：{exc}", file=sys.stderr)
        return 1

    closure = outputs["closure.json"]
    print(
        json.dumps(
            {
                "result": closure["result"],
                "source_discovery_count": closure["source_discovery_count"],
                "resolved_record_count": closure["resolved_record_count"],
                "candidate_resolution_count": closure["candidate_resolution_count"],
                "measured_rule_count": closure["measured_rule_count"],
                "withdrawn_v1_proposal_count": closure["withdrawn_v1_proposal_count"],
                "gate_counts": closure["gate_counts"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
