"""U-5 处置所绑定的三类外部收据：原业务回归收据、共享合同后继收据与 Campaign（UM-9 第 4 项）。

v0.2.4 与 v0.2.10 合并时这两份收据由脚本按先例生成，``disposition-seal`` 只算文件摘要、不看内容；
Campaign 路径也只做文件绑定。这里集中定义三者的结构：``disposition-draft`` 用它生成，
``disposition-seal`` 用它校验内容与本 Plan、验收收据、ChangeDecision 一致，Campaign 必须是对应
Persona 已登记格式的生产批准／激活收据。本模块不依赖 workflow，避免循环导入。
"""

from __future__ import annotations

from typing import Any, Callable

from .canonical import expect_object, expect_string, validate_identity
from .errors import UpstreamMergeError

ORIGINAL_BUSINESS_RECEIPT_SCHEMA = "official-egress-upstream-original-business-receipt/v1"
SHARED_CONTRACT_SUCCESSOR_SCHEMA = "official-egress-upstream-shared-contract-successor/v1"
ORIGINAL_BUSINESS_CATEGORY = "original_business"
SHARED_CONTRACT_FAIL_CLOSE = (
    "任一路径若实际改变了共享合同要素，U-4 的官方出站 wire 与 Persona 矩阵门禁会失败并阻断；本次对应门禁均通过。"
)
ORIGINAL_BUSINESS_FIELDS = {
    "schema_version",
    "plan_id",
    "plan_identity_sha256",
    "attempt_id",
    "gate",
    "source_candidate",
    "verification_receipt_identity_sha256",
    "result",
    "identity_sha256",
}
SHARED_CONTRACT_FIELDS = {
    "schema_version",
    "plan_id",
    "plan_identity_sha256",
    "source_tree",
    "source_commit",
    "scope",
    "purpose",
    "affected_paths",
    "fail_close_behavior",
    "verification",
    "persona_baseline_unchanged",
    "result",
    "identity_sha256",
}


def build_original_business_receipt(
    *,
    plan_id: str,
    plan_identity: str,
    verification: dict[str, Any],
    source_binding: dict[str, Any],
) -> dict[str, Any]:
    """由 VerificationReceipt 的原业务回归门禁派生（未签名）。"""

    gates = [gate for gate in verification.get("gates", []) if gate.get("category") == ORIGINAL_BUSINESS_CATEGORY]
    if len(gates) != 1 or gates[0].get("status") != "passed":
        raise UpstreamMergeError(f"验收收据缺少唯一且通过的 {ORIGINAL_BUSINESS_CATEGORY} 门禁")
    return {
        "schema_version": ORIGINAL_BUSINESS_RECEIPT_SCHEMA,
        "plan_id": plan_id,
        "plan_identity_sha256": plan_identity,
        "attempt_id": verification["attempt_id"],
        "gate": gates[0],
        "source_candidate": source_binding,
        "verification_receipt_identity_sha256": verification["identity_sha256"],
        "result": "passed",
    }


def validate_original_business_receipt(
    document: dict[str, Any],
    *,
    plan_id: str,
    plan_identity: str,
    verification: dict[str, Any],
    source_binding: dict[str, Any],
) -> None:
    """内容必须与本次绑定的验收收据、最新 SourceCandidate 逐字段一致。"""

    label = "原业务回归收据"
    if set(document) != ORIGINAL_BUSINESS_FIELDS:
        raise UpstreamMergeError(f"{label} 字段不闭合：{sorted(set(document) ^ ORIGINAL_BUSINESS_FIELDS)}")
    validate_identity(document, label)
    expected = build_original_business_receipt(
        plan_id=plan_id,
        plan_identity=plan_identity,
        verification=verification,
        source_binding=source_binding,
    )
    for field, value in expected.items():
        if document.get(field) != value:
            raise UpstreamMergeError(f"{label}.{field} 与本 Plan 验收收据或最新 SourceCandidate 不一致")


def shared_contract_verification(verification: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_id": verification["attempt_id"],
        "verification_receipt_identity_sha256": verification["identity_sha256"],
        "result": verification.get("result"),
        "executed_gate_count": verification.get("executed_gate_count"),
        "skipped_gate_count": verification.get("skipped_gate_count"),
    }


def persona_baseline(official_clients: dict[str, Any], identity_change_count: int) -> dict[str, Any]:
    return {
        "claude_target_version": official_clients["claude"]["target_version"],
        "codex_target_version": official_clients["codex"]["target_version"],
        "protected_objects_unchanged": True,
        "official_client_identity_change_count": identity_change_count,
    }


def validate_shared_contract_receipt(
    document: dict[str, Any],
    *,
    plan_id: str,
    plan_identity: str,
    source: dict[str, Any],
    shared_paths: dict[str, tuple[str, int]],
    verification: dict[str, Any],
    official_clients: dict[str, Any],
    identity_change_count: int,
) -> None:
    """共享合同后继收据须逐个登记 ChangeDecision 的共享控制面路径，并绑定本次验收。

    ``shared_paths`` 是路径到 (sha256, bytes) 的映射，取自最新源码候选提交。每条的 change_kind 与
    assessment 由人工填写，这里只要求非空、足够说明性质。
    """

    label = "共享合同后继收据"
    if set(document) != SHARED_CONTRACT_FIELDS:
        raise UpstreamMergeError(f"{label} 字段不闭合：{sorted(set(document) ^ SHARED_CONTRACT_FIELDS)}")
    validate_identity(document, label)
    fixed = {
        "schema_version": SHARED_CONTRACT_SUCCESSOR_SCHEMA,
        "plan_id": plan_id,
        "plan_identity_sha256": plan_identity,
        "source_tree": source["source_tree"],
        "source_commit": source["source_commit"],
        "verification": shared_contract_verification(verification),
        "persona_baseline_unchanged": persona_baseline(official_clients, identity_change_count),
        "result": "closed",
    }
    for field, value in fixed.items():
        if document.get(field) != value:
            raise UpstreamMergeError(f"{label}.{field} 与本 Plan 最新源码候选或验收收据不一致")
    for field in ("scope", "purpose", "fail_close_behavior"):
        if len(expect_string(document.get(field), f"{label}.{field}").strip()) < 8:
            raise UpstreamMergeError(f"{label}.{field} 必须写明")
    affected = document.get("affected_paths")
    if not isinstance(affected, list):
        raise UpstreamMergeError(f"{label}.affected_paths 必须是数组")
    seen: dict[str, tuple[str, int]] = {}
    for index, raw in enumerate(affected):
        item = expect_object(raw, f"{label}.affected_paths[{index}]")
        if set(item) != {"path", "sha256", "bytes", "change_kind", "assessment"}:
            raise UpstreamMergeError(f"{label}.affected_paths[{index}] 字段不闭合")
        path = expect_string(item.get("path"), f"{label}.affected_paths[{index}].path")
        if path in seen:
            raise UpstreamMergeError(f"{label} 重复登记 {path}")
        seen[path] = (item.get("sha256"), item.get("bytes"))
        if not expect_string(item.get("change_kind"), f"{label}.affected_paths[{index}].change_kind").strip():
            raise UpstreamMergeError(f"{label} {path} 未填写 change_kind")
        if len(expect_string(item.get("assessment"), f"{label}.affected_paths[{index}].assessment").strip()) < 16:
            raise UpstreamMergeError(f"{label} {path} 的 assessment 必须逐条说明实际变化性质")
    if set(seen) != set(shared_paths):
        raise UpstreamMergeError(
            f"{label} 未闭合 ChangeDecision 的共享控制面路径："
            f"缺少={sorted(set(shared_paths) - set(seen))} 多余={sorted(set(seen) - set(shared_paths))}"
        )
    drifted = sorted(path for path, facts in seen.items() if tuple(facts) != tuple(shared_paths[path]))
    if drifted:
        raise UpstreamMergeError(f"{label} 登记的文件摘要与最新源码候选不一致：{drifted}")


def _claude_fw_h(document: dict[str, Any]) -> tuple[str, str]:
    if document.get("status") != "approved":
        raise UpstreamMergeError("Claude 生产批准收据 status 不是 approved")
    target = expect_object(document.get("target"), "Claude 生产批准收据.target")
    return str(target.get("product")), str(target.get("version"))


def _claude_fw_g(document: dict[str, Any]) -> tuple[str, str]:
    if document.get("result") != "accepted":
        raise UpstreamMergeError("Claude FW-G 验收收据 result 不是 accepted")
    target = expect_object(document.get("target"), "Claude FW-G 验收收据.target")
    return str(target.get("product")), str(target.get("version"))


def _codex_activation_v2(document: dict[str, Any]) -> tuple[str, str]:
    final = expect_object(document.get("final_state"), "Codex 生产激活收据.final_state")
    target = expect_object(document.get("target"), "Codex 生产激活收据.target")
    if final.get("health") != "pass" or final.get("active_version") != target.get("version"):
        raise UpstreamMergeError("Codex 生产激活收据未处于健康的已激活状态")
    return "codex-cli", str(final.get("active_version"))


def _codex_activation_legacy(document: dict[str, Any]) -> tuple[str, str]:
    final = expect_object(document.get("final_state"), "Codex 生产激活收据.final_state")
    if final.get("status") != "active" or final.get("health") != "pass":
        raise UpstreamMergeError("Codex 生产激活收据未处于健康的已激活状态")
    return "codex-cli", str(final.get("codex_version"))


# 各 Persona 已登记的生产批准／激活收据格式；新格式出现时在此登记，未登记一律拒绝。
CAMPAIGN_EXTRACTORS: dict[str, dict[str, Callable[[dict[str, Any]], tuple[str, str]]]] = {
    "claude": {
        "claude-fw-h-production-approval/v2": _claude_fw_h,
        "claude-code-fw-g-public-acceptance/v1": _claude_fw_g,
    },
    "codex": {
        "codex-production-activation-receipt/v2": _codex_activation_v2,
        "codex-production-activation-receipt/v1": _codex_activation_v2,
        "official-egress-production-activation/v1": _codex_activation_legacy,
    },
}


def validate_campaign(
    document: dict[str, Any],
    *,
    client: str,
    mode: str,
    persona: dict[str, Any],
    target_version: str,
) -> dict[str, str]:
    """Campaign 必须是该 Persona 已登记格式的生产收据；new_candidate 还须正是计划冻结的 Active 版本。"""

    schema = document.get("schema_version")
    extractors = CAMPAIGN_EXTRACTORS.get(client, {})
    if schema not in extractors:
        raise UpstreamMergeError(
            f"{client} campaign 不是已登记的生产批准／激活收据格式：{schema}；可用 {sorted(extractors)}"
        )
    product, version = extractors[str(schema)](document)
    if product != persona.get("official_product"):
        raise UpstreamMergeError(f"{client} campaign 的产品 {product} 与计划 Persona 不一致")
    if mode == "new_candidate" and version != target_version:
        raise UpstreamMergeError(
            f"{client} new_candidate 的 campaign 必须是计划冻结 Active {target_version} 的生产收据，实际为 {version}"
        )
    return {"schema_version": str(schema), "product": product, "version": version}
