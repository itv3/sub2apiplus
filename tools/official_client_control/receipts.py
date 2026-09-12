"""构建、验证、交付、晋升与激活收据的确定性生成和独立重放。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    expect_exact_keys,
    expect_object,
    sha256_file,
)
from .contracts import (
    ACTIVATION_RECEIPT_SCHEMA,
    CANDIDATE_BUILD_RECEIPT_SCHEMA,
    CANDIDATE_DELIVERY_RECEIPT_SCHEMA,
    DEPLOYMENT_STAGES,
    PROMOTION_RECEIPT_SCHEMA,
    VALIDATION_COMPLETED_SCHEMA,
    VALIDATION_GATE_RECEIPT_SCHEMA,
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


def build_candidate_build_receipt(
    store: Any, campaign_id: str, build_input: dict[str, Any]
) -> dict[str, Any]:
    """由 VC-4 已封存构建输入生成不可覆盖 CandidateBuildReceipt。"""

    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    value = expect_object(build_input, "candidate_build_input")
    expected_keys = {
        "candidate_id",
        "candidate_purpose",
        "profile_approval_ref",
        "release_artifact_ref",
        "support_envelope_ref",
        "source_identity",
        "profile_identity",
        "validation_scope_sha256",
        "build_identity",
        "artifact_inventory",
        "image",
        "completed_at_utc",
    }
    expect_exact_keys(value, expected_keys, "candidate_build_input")
    approval = store.load_fact(value["profile_approval_ref"])
    if approval["fact_kind"] != "profile_approved":
        raise ControlError("CandidateBuildReceipt 必须绑定 ProfileApprovalFact")
    if approval["campaign_id"] != campaign_id:
        raise ControlError("CandidateBuildReceipt 不得跨 Campaign 引用批准事实")
    approval_payload = approval["payload"]
    if (
        value["candidate_purpose"] != approval_payload["approval_purpose"]
        or value["release_artifact_ref"] != approval_payload["release_artifact_ref"]
        or value["support_envelope_ref"] != approval_payload["support_envelope_ref"]
    ):
        raise ControlError("CandidateBuildReceipt 与 ProfileApprovalFact 身份不一致")
    profile_identity = value["profile_identity"]
    expected_profile_refs = {
        "profile_schema_ref": approval_payload["profile_schema_ref"],
        "snapshot_ref": approval_payload["snapshot_ref"],
        "release_artifact_ref": approval_payload["release_artifact_ref"],
        "support_envelope_ref": approval_payload["support_envelope_ref"],
    }
    for key, expected in expected_profile_refs.items():
        if profile_identity.get(key) != expected:
            raise ControlError(f"CandidateBuildReceipt profile_identity.{key} 与批准事实不一致")
    release = store.load_object(value["release_artifact_ref"])["payload"]
    if (
        release["version"] != campaign["target_version"]
        or profile_identity.get("profile_digest") != release["profile_digest"]
    ):
        raise ControlError("CandidateBuildReceipt 的版本或 Profile digest 与 Release 不一致")
    store.load_object(profile_identity["model_capability_catalog_ref"])
    if value["build_identity"].get("deployed_version") != campaign["target_version"]:
        raise ControlError("CandidateBuildReceipt deployed_version 与 Campaign 目标不一致")
    inputs = [
        value["profile_approval_ref"],
        value["release_artifact_ref"],
        value["support_envelope_ref"],
        profile_identity["model_capability_catalog_ref"],
    ]
    receipt = {
        "schema_version": CANDIDATE_BUILD_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": value["candidate_id"],
        "candidate_purpose": value["candidate_purpose"],
        "target_version": campaign["target_version"],
        "profile_approval_ref": value["profile_approval_ref"],
        "release_artifact_ref": value["release_artifact_ref"],
        "support_envelope_ref": value["support_envelope_ref"],
        "source_identity": value["source_identity"],
        "profile_identity": profile_identity,
        "validation_scope_sha256": value["validation_scope_sha256"],
        "build_identity": value["build_identity"],
        "artifact_inventory": value["artifact_inventory"],
        "image": value["image"],
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": value["completed_at_utc"],
        "producer_tool_sha256": tool_sha256,
        "identity_sha256": "",
    }
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "candidate_build")
    return receipt


def finalize_candidate_build(
    store: Any, campaign_id: str, build_input: dict[str, Any]
) -> dict[str, Any]:
    return store.write_receipt(
        "candidate_build",
        build_candidate_build_receipt(store, campaign_id, build_input),
    )


def _candidate_build_input(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in (
            "candidate_id",
            "candidate_purpose",
            "profile_approval_ref",
            "release_artifact_ref",
            "support_envelope_ref",
            "source_identity",
            "profile_identity",
            "validation_scope_sha256",
            "build_identity",
            "artifact_inventory",
            "image",
            "completed_at_utc",
        )
    }


def build_validation_gate_receipt(
    store: Any,
    campaign_id: str,
    validation_attempt_ref: dict[str, Any],
    result_input: dict[str, Any],
) -> dict[str, Any]:
    """把一次执行或复用的外部门禁结果绑定到冻结 attempt 与计划。"""

    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    value = expect_object(result_input, "validation_gate_result")
    expect_exact_keys(
        value,
        {
            "gate_id",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        },
        "validation_gate_result",
    )
    attempt = store.load_fact(validation_attempt_ref)
    if attempt["fact_kind"] != "validation_attempt_created":
        raise ControlError("ValidationGateReceipt 必须绑定 ValidationAttempt")
    if attempt["campaign_id"] != campaign_id:
        raise ControlError("ValidationGateReceipt 不得跨 Campaign 引用 attempt")
    attempt_payload = attempt["payload"]
    plan_ref = attempt_payload["validation_execution_plan_ref"]
    plan = store.load_object(plan_ref)["payload"]
    matches = [item for item in plan["external_gates"] if item["gate_id"] == value["gate_id"]]
    if len(matches) != 1:
        raise ControlError("外部门禁不属于冻结的 ValidationExecutionPlan")
    planned = matches[0]
    if planned["source"] == "reuse":
        reused = store.load_receipt(planned["reused_receipt_ref"])
        if reused["result"] != "pass":
            raise ControlError("不得复用失败的外部门禁收据")
        for key in (
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        ):
            if value[key] != reused[key]:
                raise ControlError(f"复用门禁不得改写原收据字段：{key}")
    inputs = [
        validation_attempt_ref,
        plan_ref,
        attempt_payload["candidate_build_receipt_ref"],
        planned["reused_receipt_ref"],
        planned["previous_failed_receipt_ref"],
    ]
    receipt = {
        "schema_version": VALIDATION_GATE_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": attempt_payload["candidate_id"],
        "attempt_id": attempt_payload["attempt_id"],
        "candidate_ref": attempt_payload["candidate_ref"],
        "candidate_build_receipt_ref": attempt_payload["candidate_build_receipt_ref"],
        "validation_attempt_ref": validation_attempt_ref,
        "validation_execution_plan_ref": plan_ref,
        "gate_id": planned["gate_id"],
        "requirement_sha256": planned["requirement_sha256"],
        "source": planned["source"],
        "command": planned["command"],
        "working_directory": planned["working_directory"],
        "host_id": planned["host_id"],
        "architecture": planned["architecture"],
        "started_at_utc": value["started_at_utc"],
        "completed_at_utc": value["completed_at_utc"],
        "exit_code": value["exit_code"],
        "output_sha256": value["output_sha256"],
        "result": value["result"],
        "reused_receipt_ref": planned["reused_receipt_ref"],
        "previous_failed_receipt_ref": planned[
            "previous_failed_receipt_ref"
        ],
        "input_facts_sha256": canonical_sha256(inputs),
        "producer_tool_sha256": tool_sha256,
        "identity_sha256": "",
    }
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "validation_gate")
    return receipt


def finalize_validation_gate(
    store: Any,
    campaign_id: str,
    validation_attempt_ref: dict[str, Any],
    result_input: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_validation_gate_receipt(
        store, campaign_id, validation_attempt_ref, result_input
    )
    prior_failures: list[tuple[dict[str, Any], dict[str, Any]]] = []
    prior_passes: list[dict[str, Any]] = []
    for reference in store.list_receipt_refs("validation_gate"):
        existing = store.load_receipt(reference)
        if (
            existing["validation_attempt_ref"] == validation_attempt_ref
            and existing["gate_id"] == receipt["gate_id"]
        ):
            raise ControlError(
                "同一 ValidationAttempt 的外部门禁槽位不得覆盖；失败后必须建立新 attempt"
            )
        if (
            existing["campaign_id"] == campaign_id
            and existing["candidate_ref"] == receipt["candidate_ref"]
            and existing["gate_id"] == receipt["gate_id"]
        ):
            if existing["result"] == "failed":
                prior_failures.append((reference, existing))
            else:
                prior_passes.append(existing)
    previous_ref = receipt["previous_failed_receipt_ref"]
    if receipt["source"] == "execute" and prior_passes:
        raise ControlError("已有通过门禁必须重放收据，不得在新 attempt 重复执行")
    if receipt["source"] == "execute" and prior_failures:
        prior_failures.sort(
            key=lambda item: datetime.fromisoformat(
                item[1]["completed_at_utc"].replace("Z", "+00:00")
            )
        )
        if previous_ref != prior_failures[-1][0]:
            raise ControlError("重试外部门禁必须绑定最近一份前序失败收据")
    elif receipt["source"] == "execute" and previous_ref is not None:
        raise ControlError("外部门禁声明了不存在的前序失败收据")
    return store.write_receipt("validation_gate", receipt)


def _validation_gate_input(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in (
            "gate_id",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
        )
    }


def finalize_validation(
    store: Any,
    campaign_id: str,
    acceptance_ref: dict[str, Any],
    production_selector_after_ref: dict[str, Any],
    issued_at_utc: str,
) -> dict[str, Any]:
    """从严格 Acceptance 派生 VC-5 完成事实；门禁在追加前统一执行。"""

    _require_current_tool(store.load_campaign(campaign_id))
    acceptance = store.load_fact(acceptance_ref)
    if acceptance["fact_kind"] != "acceptance_recorded":
        raise ControlError("VC-5 finalizer 必须绑定 AcceptanceFact")
    if acceptance["campaign_id"] != campaign_id:
        raise ControlError("VC-5 finalizer 不得跨 Campaign 引用 AcceptanceFact")
    value = acceptance["payload"]
    if value.get("schema_version") is None:
        raise ControlError("VC-5 finalizer 不接受历史 AcceptanceFact")
    attempt = store.load_fact(value["validation_attempt_ref"])["payload"]
    payload = {
        "schema_version": VALIDATION_COMPLETED_SCHEMA,
        "attempt_id": attempt["attempt_id"],
        "candidate_id": value["candidate_id"],
        "candidate_ref": value["candidate_ref"],
        "candidate_build_receipt_ref": value["candidate_build_receipt_ref"],
        "validation_attempt_ref": value["validation_attempt_ref"],
        "validation_execution_plan_ref": attempt["validation_execution_plan_ref"],
        "candidate_evidence_package_ref": value["candidate_evidence_package_ref"],
        "acceptance_ref": acceptance_ref,
        "pair_refs": value["pair_refs"],
        "external_gate_receipt_refs": value["external_gate_receipt_refs"],
        "production_selector_after_ref": production_selector_after_ref,
        "vircs_state": attempt["vircs_state"],
        "result": "passed",
        "identity_sha256": "",
    }
    payload["identity_sha256"] = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    return store.append_fact(
        campaign_id, "validation_completed", payload, issued_at_utc
    )


def build_promotion_receipt(
    store: Any, campaign_id: str, promotion_fact_ref: dict[str, Any]
) -> dict[str, Any]:
    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    promotion = store.load_fact(promotion_fact_ref)
    if promotion["fact_kind"] != "release_promoted":
        raise ControlError("PromotionReceipt 必须由 release_promoted 事实生成")
    if promotion["campaign_id"] != campaign_id:
        raise ControlError("PromotionReceipt 不得跨 Campaign 引用晋升事实")
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
    if any(fact["campaign_id"] != campaign_id for _reference, fact in chain):
        raise ControlError("ActivationReceipt 不得跨 Campaign 引用 DeploymentFact")
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
    if any(
        fact["campaign_id"] != campaign_id
        for fact in (selector_before, selector_after, inventory_current)
    ):
        raise ControlError("ActivationReceipt 的 selector／Inventory 不得跨 Campaign")
    if inventory_current["payload"]["deployment_ref"] != restored_active_ref:
        raise ControlError("ActivationReceipt 的最终 Inventory 未绑定 restored_active")
    if selector_after["payload"]["catalog_snapshot_ref"] != final_payload[
        "runtime_catalog_snapshot_ref"
    ]:
        raise ControlError("恢复后的 Runtime Selector 与最终 DeploymentFact 不一致")
    promotion_ref = first["promotion_receipt_ref"]
    promotion = store.load_receipt(promotion_ref)
    if promotion["campaign_id"] != campaign_id:
        raise ControlError("ActivationReceipt 的 PromotionReceipt 跨 Campaign")
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


def build_candidate_delivery_receipt(
    store: Any,
    campaign_id: str,
    candidate_delivery_package_ref: dict[str, Any],
) -> dict[str, Any]:
    """由完整 DMIT 四阶段事实和交付包生成候选交付终态收据。"""

    campaign = store.load_campaign(campaign_id)
    tool_sha256 = _require_current_tool(campaign)
    package_document = store.load_object(candidate_delivery_package_ref)
    if package_document["object_kind"] != "candidate_delivery_package":
        raise ControlError("CandidateDeliveryReceipt 必须绑定 CandidateDeliveryPackage")
    package = package_document["payload"]
    if package["campaign_id"] != campaign_id:
        raise ControlError("CandidateDeliveryPackage 与 Campaign 不一致")
    facts = [store.load_fact(reference) for reference in package["delivery_fact_refs"]]
    stages = [fact["payload"]["stage"] for fact in facts]
    if stages != [
        "candidate_active",
        "rollback_verified",
        "candidate_restored",
        "stable_observed",
    ]:
        raise ControlError("CandidateDeliveryReceipt 要求完整有序四阶段事实")
    if any(fact["payload"]["result"] != "pass" for fact in facts):
        raise ControlError("CandidateDeliveryReceipt 不得消费失败的候选交付阶段")
    stable = facts[-1]
    candidate = store.load_fact(package["candidate_ref"])
    acceptance = store.load_fact(package["acceptance_ref"])
    validation = store.load_fact(package["validation_completed_ref"])
    if (
        candidate["fact_kind"] != "candidate_frozen"
        or acceptance["fact_kind"] != "acceptance_recorded"
        or validation["fact_kind"] != "validation_completed"
    ):
        raise ControlError("CandidateDeliveryReceipt 的 Candidate／Acceptance／VC-5 引用错误")
    candidate_payload = candidate["payload"]
    acceptance_payload = acceptance["payload"]
    if validation["payload"]["acceptance_ref"] != package["acceptance_ref"]:
        raise ControlError("CandidateDeliveryReceipt 未绑定同一 VC-5 Acceptance 链")
    plan = store.load_object(package["delivery_plan_ref"])["payload"]
    archive_ref = package["private_archive_manifest_ref"]
    inputs = [
        package["candidate_ref"],
        package["candidate_build_receipt_ref"],
        package["validation_completed_ref"],
        package["acceptance_ref"],
        package["candidate_evidence_package_ref"],
        package["delivery_plan_ref"],
        *package["delivery_fact_refs"],
        candidate_delivery_package_ref,
        archive_ref,
    ]
    receipt = {
        "schema_version": CANDIDATE_DELIVERY_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_purpose": acceptance_payload["acceptance_purpose"],
        "target_version": campaign["target_version"],
        "profile_approval_ref": acceptance_payload["profile_approval_ref"],
        "candidate_id": candidate_payload["candidate_id"],
        "candidate_ref": package["candidate_ref"],
        "candidate_build_receipt_ref": package["candidate_build_receipt_ref"],
        "validation_completed_ref": package["validation_completed_ref"],
        "acceptance_ref": package["acceptance_ref"],
        "candidate_evidence_package_ref": package[
            "candidate_evidence_package_ref"
        ],
        "delivery_plan_ref": package["delivery_plan_ref"],
        "delivery_fact_refs": package["delivery_fact_refs"],
        "candidate_delivery_package_ref": candidate_delivery_package_ref,
        "private_archive_manifest_ref": archive_ref,
        "candidate_image_digest": plan["candidate_image_digest"],
        "host_id": plan["host_id"],
        "architecture": plan["architecture"],
        "release_state": "ready_for_operator_release",
        "vircs_state": plan["vircs_state"],
        "input_facts_sha256": canonical_sha256(inputs),
        "completed_at_utc": stable["issued_at_utc"],
        "producer_tool_sha256": tool_sha256,
        "identity_sha256": "",
    }
    receipt["identity_sha256"] = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    validate_receipt_document(receipt, "candidate_delivery")
    return receipt


def finalize_candidate_delivery(
    store: Any,
    campaign_id: str,
    candidate_delivery_package_ref: dict[str, Any],
) -> dict[str, Any]:
    receipt = build_candidate_delivery_receipt(
        store, campaign_id, candidate_delivery_package_ref
    )
    for reference in store.list_receipt_refs("candidate_delivery"):
        existing = store.load_receipt(reference)
        if (
            existing["candidate_ref"] == receipt["candidate_ref"]
            and existing["acceptance_ref"] == receipt["acceptance_ref"]
        ):
            raise ControlError("同一 Candidate／Acceptance 的候选交付收据不得覆盖")
    return store.write_receipt("candidate_delivery", receipt)


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
    elif kind == "candidate_build":
        rebuilt = build_candidate_build_receipt(
            store,
            existing["campaign_id"],
            _candidate_build_input(existing),
        )
    elif kind == "validation_gate":
        rebuilt = build_validation_gate_receipt(
            store,
            existing["campaign_id"],
            existing["validation_attempt_ref"],
            _validation_gate_input(existing),
        )
    elif kind == "candidate_delivery":
        rebuilt = build_candidate_delivery_receipt(
            store,
            existing["campaign_id"],
            existing["candidate_delivery_package_ref"],
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
