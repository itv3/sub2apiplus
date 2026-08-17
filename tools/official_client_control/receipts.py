"""晋升与激活收据的确定性生成、只写一次封存和独立重放。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, canonical_sha256, sha256_file
from .contracts import (
    ACTIVATION_RECEIPT_SCHEMA,
    DEPLOYMENT_STAGES,
    PROMOTION_RECEIPT_SCHEMA,
    validate_receipt_document,
)
from .errors import ControlError


def control_tool_bundle_sha256() -> str:
    """绑定实际参与合同、Store、门禁、CLI 和 Schema 的执行源。"""

    root = Path(__file__).resolve().parent
    paths = sorted(
        [
            path
            for path in root.glob("*.py")
            if path.name != "__init__.py" and not path.is_symlink()
        ]
        + [
            path
            for path in (root / "schemas").glob("*.json")
            if not path.is_symlink()
        ]
    )
    if not paths:
        raise ControlError("FW-D 工具源码清单为空")
    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in paths
    ]
    return canonical_sha256(entries)


def _require_current_tool(campaign: dict[str, Any]) -> str:
    current = control_tool_bundle_sha256()
    if campaign["tool_bundle_sha256"] != current:
        raise ControlError("控制面工具身份已变化，必须建立新的 Campaign")
    return current


def build_promotion_receipt(
    store: Any, campaign_id: str, promotion_fact_ref: dict[str, Any]
) -> dict[str, Any]:
    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    promotion = store.load_fact(promotion_fact_ref)
    if promotion["fact_kind"] != "release_promoted":
        raise ControlError("PromotionReceipt 必须由 release_promoted 事实生成")
    payload = promotion["payload"]
    candidate = store.load_fact(payload["candidate_ref"])
    acceptance = store.load_fact(payload["acceptance_ref"])
    if candidate["fact_kind"] != "candidate_frozen" or acceptance["fact_kind"] != "acceptance_recorded":
        raise ControlError("晋升事实的 Candidate／Acceptance 引用类型错误")
    approval_ref = candidate["payload"]["profile_approval_ref"]
    inputs = [
        payload["candidate_ref"],
        payload["acceptance_ref"],
        promotion_fact_ref,
        approval_ref,
    ]
    receipt = {
        "schema_version": PROMOTION_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_ref": payload["candidate_ref"],
        "acceptance_ref": payload["acceptance_ref"],
        "promotion_fact_ref": promotion_fact_ref,
        "release_artifact_ref": payload["release_artifact_ref"],
        "promotion_diff_ref": payload["promotion_diff_ref"],
        "profile_approval_ref": approval_ref,
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": promotion["issued_at_utc"],
        "producer_tool_sha256": tool_sha256,
    }
    validate_receipt_document(receipt, "promotion")
    return receipt


def finalize_promotion(
    store: Any, campaign_id: str, promotion_fact_ref: dict[str, Any]
) -> dict[str, Any]:
    return store.write_receipt(
        "promotion", build_promotion_receipt(store, campaign_id, promotion_fact_ref)
    )


def _deployment_chain(
    store: Any, restored_active_ref: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    restored = store.load_fact(restored_active_ref)
    if restored["fact_kind"] != "restored_active":
        raise ControlError("激活收据必须从 restored_active DeploymentFact 生成")
    reversed_chain: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (restored_active_ref, restored)
    ]
    current = restored
    while current["fact_kind"] != "accepted_not_activated":
        previous_ref = current["payload"]["previous_stage_ref"]
        previous = store.load_fact(previous_ref)
        reversed_chain.append((previous_ref, previous))
        current = previous
    chain = list(reversed(reversed_chain))
    if [fact["fact_kind"] for _reference, fact in chain] != list(DEPLOYMENT_STAGES):
        raise ControlError("DeploymentFact 没有形成完整五阶段链")
    return chain


def build_activation_receipt(
    store: Any,
    campaign_id: str,
    restored_active_ref: dict[str, Any],
    selector_before_ref: dict[str, Any],
    selector_after_ref: dict[str, Any],
    inventory_current_ref: dict[str, Any],
) -> dict[str, Any]:
    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    chain = _deployment_chain(store, restored_active_ref)
    first = chain[0][1]["payload"]
    final = chain[-1][1]
    final_payload = final["payload"]
    selector_before = store.load_fact(selector_before_ref)
    selector_after = store.load_fact(selector_after_ref)
    inventory_current = store.load_fact(inventory_current_ref)
    if selector_before["fact_kind"] != "selector_observed":
        raise ControlError("ActivationReceipt 的 selector_before 必须是只读观察事实")
    if selector_after["fact_kind"] != "selector_activated":
        raise ControlError("ActivationReceipt 的 selector_after 必须是激活事实")
    if inventory_current["fact_kind"] != "inventory_current_appended":
        raise ControlError("ActivationReceipt 缺少 Deployment 后追加的当前 Inventory")
    if inventory_current["payload"]["deployment_ref"] != restored_active_ref:
        raise ControlError("ActivationReceipt 的最终 Inventory 未绑定 restored_active")
    if selector_after["payload"]["catalog_snapshot_ref"] != final_payload[
        "runtime_catalog_snapshot_ref"
    ]:
        raise ControlError("恢复后的 Runtime Selector 与最终 DeploymentFact 不一致")
    promotion_ref = first["promotion_receipt_ref"]
    promotion = store.load_receipt(promotion_ref)
    acceptance = store.load_fact(first["acceptance_ref"])
    candidate_ref = acceptance["payload"]["candidate_ref"]
    if promotion["candidate_ref"] != candidate_ref:
        raise ControlError("ActivationReceipt 的 Promotion 与 Candidate 不一致")
    deployment_refs = [reference for reference, _fact in chain]
    inputs = [
        candidate_ref,
        first["acceptance_ref"],
        selector_before_ref,
        selector_after_ref,
        inventory_current_ref,
        *deployment_refs,
    ]
    receipt = {
        "schema_version": ACTIVATION_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_ref": candidate_ref,
        "acceptance_ref": first["acceptance_ref"],
        "promotion_receipt_ref": promotion_ref,
        "deployment_fact_refs": deployment_refs,
        "selector_before_ref": selector_before_ref,
        "selector_after_ref": selector_after_ref,
        "active_support_envelope_ref": final_payload["active_support_envelope_ref"],
        "rollback_operational_envelope_ref": final_payload[
            "rollback_operational_envelope_ref"
        ],
        "deployment_traffic_envelope_ref": final_payload[
            "deployment_traffic_envelope_ref"
        ],
        "final_ingress_inventory_ref": inventory_current["payload"][
            "production_ingress_inventory_ref"
        ],
        "final_egress_inventory_ref": inventory_current["payload"][
            "egress_disposition_inventory_ref"
        ],
        "formal_image_digest": final_payload["image_digest"],
        "final_state": "restored_active",
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": inventory_current["issued_at_utc"],
        "producer_tool_sha256": tool_sha256,
    }
    validate_receipt_document(receipt, "activation")
    return receipt


def finalize_activation(
    store: Any,
    campaign_id: str,
    restored_active_ref: dict[str, Any],
    selector_before_ref: dict[str, Any],
    selector_after_ref: dict[str, Any],
    inventory_current_ref: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_activation_receipt(
        store,
        campaign_id,
        restored_active_ref,
        selector_before_ref,
        selector_after_ref,
        inventory_current_ref,
    )
    return store.write_receipt("activation", receipt)


def replay_receipt(store: Any, reference: dict[str, Any]) -> dict[str, Any]:
    """从所引用事实重建收据，拒绝任一输入或收据字段不匹配。"""

    existing = store.load_receipt(reference)
    kind = reference["receipt_kind"]
    if kind == "promotion":
        rebuilt = build_promotion_receipt(
            store, existing["campaign_id"], existing["promotion_fact_ref"]
        )
    elif kind == "activation":
        rebuilt = build_activation_receipt(
            store,
            existing["campaign_id"],
            existing["deployment_fact_refs"][-1],
            existing["selector_before_ref"],
            existing["selector_after_ref"],
            _find_inventory_current_ref(store, existing),
        )
    else:
        raise ControlError(f"未知收据类型：{kind}")
    if canonical_json_bytes(rebuilt) != canonical_json_bytes(existing):
        raise ControlError(f"{kind} 收据与事实独立复算结果不匹配")
    return existing


def _find_inventory_current_ref(store: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    campaign_id = receipt["campaign_id"]
    restored_ref = receipt["deployment_fact_refs"][-1]
    matches: list[dict[str, Any]] = []
    for fact in store.list_facts(campaign_id, "deployment"):
        if (
            fact["fact_kind"] == "inventory_current_appended"
            and fact["payload"]["deployment_ref"] == restored_ref
            and fact["payload"]["production_ingress_inventory_ref"]
            == receipt["final_ingress_inventory_ref"]
            and fact["payload"]["egress_disposition_inventory_ref"]
            == receipt["final_egress_inventory_ref"]
        ):
            matches.append(store.fact_ref(fact))
    if len(matches) != 1:
        raise ControlError("ActivationReceipt 无法唯一关联最终 Inventory 事实")
    return matches[0]
