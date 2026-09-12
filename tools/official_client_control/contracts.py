"""FW-D 控制面文档合同与严格字段校验。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

from .canonical import (
    PAIR_ID_RE,
    SAFE_ID_RE,
    SPEC_ID_RE,
    canonical_sha256,
    expect_bool,
    expect_exact_keys,
    expect_image_digest,
    expect_non_negative_int,
    expect_object,
    expect_rfc3339,
    expect_safe_id,
    expect_sha256,
    expect_string,
    expect_string_list,
    validate_external_binding,
)
from .errors import ControlError


STORE_SCHEMA = "official-client-control-store/v1"
BOOTSTRAP_SCHEMA = "official-client-control-bootstrap/v1"
CAMPAIGN_SCHEMA = "official-client-control-campaign/v1"
OBJECT_SCHEMA = "official-client-control-object/v1"
FACT_SCHEMA = "official-client-control-fact/v1"
PROMOTION_RECEIPT_SCHEMA = "official-client-control-promotion-receipt/v1"
ACTIVATION_RECEIPT_SCHEMA = "official-client-control-activation-receipt/v1"
CANDIDATE_BUILD_RECEIPT_SCHEMA = "official-client-candidate-build-receipt/v1"
VALIDATION_GATE_RECEIPT_SCHEMA = "official-client-validation-gate-receipt/v1"
CANDIDATE_DELIVERY_RECEIPT_SCHEMA = "official-client-candidate-delivery-receipt/v1"
EVIDENCE_PACKAGE_V3_SCHEMA = "official-client-evidence-package/v3"
RULE_MIGRATION_LEDGER_SCHEMA = "official-client-rule-migration-ledger/v1"
ATOMIC_ASSERTION_LEDGER_SCHEMA = "official-client-atomic-assertion-ledger/v1"
VALIDATION_CANDIDATE_V2_SCHEMA = "official-client-validation-candidate/v2"
SCENARIO_STAGE_V2_SCHEMA = "official-client-scenario-stage/v2"
PAIR_V2_SCHEMA = "official-client-pair/v2"
ACCEPTANCE_V2_SCHEMA = "official-client-acceptance/v2"
VALIDATION_EXECUTION_PLAN_SCHEMA = "official-client-validation-execution-plan/v1"
CANDIDATE_EVIDENCE_PACKAGE_SCHEMA = "official-client-candidate-evidence-package/v1"
VALIDATION_ATTEMPT_SCHEMA = "official-client-validation-attempt/v1"
VALIDATION_COMPLETED_SCHEMA = "official-client-validation-completed/v1"
CANDIDATE_DELIVERY_PLAN_SCHEMA = "official-client-candidate-delivery-plan/v1"
CANDIDATE_DELIVERY_PACKAGE_SCHEMA = "official-client-candidate-delivery-package/v1"
PRIVATE_ARCHIVE_MANIFEST_SCHEMA = "official-client-private-archive-manifest/v1"
CANDIDATE_DELIVERY_FACT_SCHEMA = "official-client-candidate-delivery-fact/v1"

DIMENSIONS = (
    "discovery",
    "evidence",
    "approval",
    "validation",
    "runtime_selector",
    "deployment",
)

FACT_DIMENSION = {
    "discovery_recorded": "discovery",
    "evidence_recorded": "evidence",
    "rule_classification_recorded": "evidence",
    "evidence_approved": "approval",
    "profile_approved": "approval",
    "candidate_frozen": "validation",
    "scenario_prepared": "validation",
    "scenario_captured": "validation",
    "scenario_sealed": "validation",
    "scenario_approved": "validation",
    "pair_recorded": "validation",
    "acceptance_recorded": "validation",
    "validation_attempt_created": "validation",
    "validation_completed": "validation",
    "selector_observed": "runtime_selector",
    "selector_activated": "runtime_selector",
    "release_promoted": "deployment",
    "accepted_not_activated": "deployment",
    "canary_passed": "deployment",
    "active": "deployment",
    "rollback_verified": "deployment",
    "restored_active": "deployment",
    "inventory_current_appended": "deployment",
    "candidate_delivery_recorded": "deployment",
}

DEPLOYMENT_STAGES = (
    "accepted_not_activated",
    "canary_passed",
    "active",
    "rollback_verified",
    "restored_active",
)

CANDIDATE_DELIVERY_STAGES = (
    "candidate_active",
    "rollback_verified",
    "candidate_restored",
    "stable_observed",
)

SCENARIO_FACT_STAGE = {
    "scenario_prepared": "prepare",
    "scenario_captured": "capture",
    "scenario_sealed": "seal",
    "scenario_approved": "approve",
}

EVIDENCE_LEVELS = {
    "verified",
    "observed",
    "blocked",
    "regressed_evidence",
}
RULE_LIFECYCLES = {"candidate", "active", "superseded"}
COMPATIBILITY_CLASSES = {"request_egress", "response_compat", "not_applicable"}
MIGRATION_DECISIONS = {"inherit", "change", "add", "delete", "condition_change"}
APPROVAL_PURPOSES = {"validation_only", "production_replacement"}
INGRESS_DISPOSITIONS = {
    "migrated_strict",
    "retained_legacy",
    "explicitly_retired",
    "rerouted",
}
EGRESS_DISPOSITIONS = {"persona_strict", "non_persona_managed", "denied"}
EGRESS_GUARD_STATES = {
    "source_absent",
    "out_of_scope_passthrough",
    "legacy_observe",
    "canary_enforce",
    "enforced",
}

OBJECT_KINDS = {
    "bootstrap",
    "persona_descriptor",
    "evidence_package",
    "rule_migration_ledger",
    "atomic_assertion_ledger",
    "validation_execution_plan",
    "candidate_evidence_package",
    "candidate_delivery_plan",
    "candidate_delivery_package",
    "private_archive_manifest",
    "model_capability_catalog",
    "profile_schema",
    "snapshot",
    "release_bundle",
    "release_artifact",
    "ingress_observation",
    "egress_observation",
    "production_ingress_inventory",
    "egress_disposition_inventory",
    "support_envelope",
    "active_support_envelope",
    "rollback_operational_envelope",
    "deployment_traffic_envelope",
    "persona_derivation",
    "compatibility_boundary",
    "scenario_plan",
    "deployment_plan",
    "runtime_catalog_snapshot",
    "operational_evidence",
    "promotion_diff",
    "acceptance_package",
}

_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _expect_enum(value: Any, allowed: set[str], label: str) -> str:
    text = expect_string(value, label)
    if text not in allowed:
        raise ControlError(f"{label} 非法：{text}")
    return text


def validate_persona(value: Any, label: str = "persona") -> dict[str, Any]:
    persona = expect_object(value, label)
    expect_exact_keys(
        persona,
        {"provider", "official_product", "auth_family", "upstream_route_family"},
        label,
    )
    for key in persona:
        expect_safe_id(persona[key], f"{label}.{key}")
    if persona["auth_family"] != "oauth":
        raise ControlError(f"{label}.auth_family 必须是 oauth")
    return persona


def persona_key(value: Any) -> str:
    persona = validate_persona(value)
    return "/".join(
        persona[key]
        for key in (
            "provider",
            "official_product",
            "auth_family",
            "upstream_route_family",
        )
    )


def validate_object_ref(value: Any, label: str = "object_ref") -> dict[str, Any]:
    reference = expect_object(value, label)
    expect_exact_keys(reference, {"object_kind", "sha256"}, label)
    kind = expect_string(reference["object_kind"], f"{label}.object_kind")
    if kind not in OBJECT_KINDS:
        raise ControlError(f"{label}.object_kind 未登记：{kind}")
    expect_sha256(reference["sha256"], f"{label}.sha256")
    return reference


def validate_fact_ref(value: Any, label: str = "fact_ref") -> dict[str, Any]:
    reference = expect_object(value, label)
    expect_exact_keys(
        reference,
        {"campaign_id", "dimension", "sequence", "sha256"},
        label,
    )
    expect_safe_id(reference["campaign_id"], f"{label}.campaign_id")
    dimension = expect_string(reference["dimension"], f"{label}.dimension")
    if dimension not in DIMENSIONS:
        raise ControlError(f"{label}.dimension 非法")
    sequence = expect_non_negative_int(reference["sequence"], f"{label}.sequence")
    if sequence < 1:
        raise ControlError(f"{label}.sequence 必须从 1 开始")
    expect_sha256(reference["sha256"], f"{label}.sha256")
    return reference


def validate_receipt_ref(value: Any, label: str = "receipt_ref") -> dict[str, Any]:
    reference = expect_object(value, label)
    expect_exact_keys(reference, {"receipt_kind", "sha256"}, label)
    kind = expect_string(reference["receipt_kind"], f"{label}.receipt_kind")
    if kind not in {
        "promotion",
        "activation",
        "candidate_build",
        "validation_gate",
        "candidate_delivery",
    }:
        raise ControlError(f"{label}.receipt_kind 非法")
    expect_sha256(reference["sha256"], f"{label}.sha256")
    return reference


def iter_object_refs(value: Any) -> Iterator[dict[str, Any]]:
    """递归枚举文档中的对象引用。"""

    if isinstance(value, dict):
        if set(value) == {"object_kind", "sha256"}:
            yield validate_object_ref(value)
            return
        for item in value.values():
            yield from iter_object_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_object_refs(item)


def iter_fact_refs(value: Any) -> Iterator[dict[str, Any]]:
    """递归枚举文档中的事实引用。"""

    if isinstance(value, dict):
        if set(value) == {"campaign_id", "dimension", "sequence", "sha256"}:
            yield validate_fact_ref(value)
            return
        for item in value.values():
            yield from iter_fact_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_fact_refs(item)


def iter_receipt_refs(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        if set(value) == {"receipt_kind", "sha256"}:
            yield validate_receipt_ref(value)
            return
        for item in value.values():
            yield from iter_receipt_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_receipt_refs(item)


def _validate_ref_list(
    value: Any,
    label: str,
    validator: Callable[[Any, str], dict[str, Any]],
    *,
    non_empty: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or (non_empty and not value):
        raise ControlError(f"{label} 必须是{'非空' if non_empty else ''}引用数组")
    result = [validator(item, f"{label}[{index}]") for index, item in enumerate(value)]
    keys = [canonical_sha256(item) for item in result]
    if keys != sorted(set(keys)):
        raise ControlError(f"{label} 必须按内容严格排序且不得重复")
    return result


def validate_bootstrap_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "bootstrap")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "source_commit",
            "contract_sources",
            "contract_bundle_sha256",
            "fw_c_receipts",
            "runtime_catalog",
            "tool_bundle_sha256",
            "result",
        },
        "bootstrap",
    )
    if payload["schema_version"] != BOOTSTRAP_SCHEMA:
        raise ControlError("bootstrap.schema_version 不匹配")
    source_commit = expect_string(payload["source_commit"], "bootstrap.source_commit")
    if not _GIT_COMMIT_RE.fullmatch(source_commit):
        raise ControlError("bootstrap.source_commit 必须是完整 Git commit")
    for key in ("contract_sources", "fw_c_receipts", "runtime_catalog"):
        items = payload[key]
        if not isinstance(items, list) or not items:
            raise ControlError(f"bootstrap.{key} 必须是非空数组")
        normalized = [
            validate_external_binding(item, f"bootstrap.{key}[{index}]")
            for index, item in enumerate(items)
        ]
        paths = [item["path"] for item in normalized]
        if paths != sorted(set(paths)):
            raise ControlError(f"bootstrap.{key} 必须按路径排序且不得重复")
    expect_sha256(payload["contract_bundle_sha256"], "bootstrap.contract_bundle_sha256")
    if payload["contract_bundle_sha256"] != canonical_sha256(payload["contract_sources"]):
        raise ControlError("bootstrap.contract_bundle_sha256 与合同源码清单不一致")
    expect_sha256(payload["tool_bundle_sha256"], "bootstrap.tool_bundle_sha256")
    if payload["result"] != "stable":
        raise ControlError("bootstrap.result 必须是 stable")
    return payload


def validate_campaign(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "campaign")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "target_version",
            "official_artifacts",
            "platforms",
            "entrypoints",
            "default_conditions",
            "tool_bundle_sha256",
            "bootstrap_ref",
            "created_at_utc",
            "identity_sha256",
        },
        "campaign",
    )
    if payload["schema_version"] != CAMPAIGN_SCHEMA:
        raise ControlError("campaign.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "campaign.campaign_id")
    validate_persona(payload["persona"])
    expect_string(payload["target_version"], "campaign.target_version")
    artifacts = payload["official_artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ControlError("campaign.official_artifacts 必须是非空数组")
    bindings = [
        validate_external_binding(item, f"campaign.official_artifacts[{index}]")
        for index, item in enumerate(artifacts)
    ]
    if [item["path"] for item in bindings] != sorted(
        {item["path"] for item in bindings}
    ):
        raise ControlError("campaign.official_artifacts 必须按路径排序且不得重复")
    expect_string_list(payload["platforms"], "campaign.platforms")
    expect_string_list(payload["entrypoints"], "campaign.entrypoints")
    expect_string_list(payload["default_conditions"], "campaign.default_conditions")
    expect_sha256(payload["tool_bundle_sha256"], "campaign.tool_bundle_sha256")
    bootstrap = validate_object_ref(payload["bootstrap_ref"], "campaign.bootstrap_ref")
    if bootstrap["object_kind"] != "bootstrap":
        raise ControlError("campaign.bootstrap_ref 必须引用 bootstrap")
    expect_rfc3339(payload["created_at_utc"], "campaign.created_at_utc")
    expected_identity = campaign_identity_sha256(payload)
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("campaign.identity_sha256 与身份字段不一致")
    return payload


def campaign_identity_sha256(payload: dict[str, Any]) -> str:
    fields = {
        key: payload[key]
        for key in (
            "persona",
            "target_version",
            "official_artifacts",
            "platforms",
            "entrypoints",
            "default_conditions",
            "tool_bundle_sha256",
            "bootstrap_ref",
        )
    }
    return canonical_sha256(fields)


def _validate_legacy_evidence_rule(value: Any, label: str) -> dict[str, Any]:
    """校验 EvidencePackage v1/v2 中已经冻结的历史规则记录。"""

    rule = expect_object(value, label)
    expect_exact_keys(
        rule,
        {
            "spec_id",
            "evidence_level",
            "rule_lifecycle",
            "compatibility_class",
            "migration_decision",
            "evidence_refs",
            "applicability",
        },
        label,
    )
    spec_id = expect_string(rule["spec_id"], f"{label}.spec_id")
    if not SPEC_ID_RE.fullmatch(spec_id):
        raise ControlError(f"{label}.spec_id 非法")
    _expect_enum(rule["evidence_level"], EVIDENCE_LEVELS, f"{label}.evidence_level")
    _expect_enum(rule["rule_lifecycle"], RULE_LIFECYCLES, f"{label}.rule_lifecycle")
    _expect_enum(
        rule["compatibility_class"],
        COMPATIBILITY_CLASSES,
        f"{label}.compatibility_class",
    )
    _expect_enum(
        rule["migration_decision"],
        MIGRATION_DECISIONS,
        f"{label}.migration_decision",
    )
    refs = rule["evidence_refs"]
    if not isinstance(refs, list) or not refs:
        raise ControlError(f"{label}.evidence_refs 必须是非空外部证据数组")
    normalized = [
        validate_external_binding(item, f"{label}.evidence_refs[{index}]")
        for index, item in enumerate(refs)
    ]
    if [item["path"] for item in normalized] != sorted(
        {item["path"] for item in normalized}
    ):
        raise ControlError(f"{label}.evidence_refs 必须按路径排序且不得重复")
    expect_string_list(rule["applicability"], f"{label}.applicability")
    return rule


def _validate_evidence_item(value: Any, label: str) -> dict[str, Any]:
    """校验 VC-1 事实证据项；这里不得出现迁移决定或目标规则结论。"""

    item = expect_object(value, label)
    expect_exact_keys(
        item,
        {
            "evidence_id",
            "source_ids",
            "evidence_refs",
            "applicability",
        },
        label,
    )
    expect_safe_id(item["evidence_id"], f"{label}.evidence_id")
    source_ids = expect_string_list(item["source_ids"], f"{label}.source_ids")
    for index, source_id in enumerate(source_ids):
        expect_safe_id(source_id, f"{label}.source_ids[{index}]")
    refs = item["evidence_refs"]
    if not isinstance(refs, list) or not refs:
        raise ControlError(f"{label}.evidence_refs 必须是非空外部证据数组")
    normalized = [
        validate_external_binding(reference, f"{label}.evidence_refs[{index}]")
        for index, reference in enumerate(refs)
    ]
    if [reference["path"] for reference in normalized] != sorted(
        {reference["path"] for reference in normalized}
    ):
        raise ControlError(f"{label}.evidence_refs 必须按路径排序且不得重复")
    expect_string_list(item["applicability"], f"{label}.applicability")
    return item


def _validate_persona_descriptor(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "persona_descriptor")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "routes", "sinks", "explicit_exclusions"},
        "persona_descriptor",
    )
    if payload["schema_version"] != "official-client-persona-descriptor/v1":
        raise ControlError("persona_descriptor.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_string_list(payload["routes"], "persona_descriptor.routes")
    expect_string_list(payload["sinks"], "persona_descriptor.sinks")
    expect_string_list(
        payload["explicit_exclusions"],
        "persona_descriptor.explicit_exclusions",
        non_empty=False,
    )
    return payload


def _validate_evidence_package(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "evidence_package")
    schema_version = payload.get("schema_version")
    expected_keys = {
        "schema_version",
        "persona",
        "version",
        "official_artifacts",
        "platforms",
        "entrypoints",
        "default_conditions",
        "comparison_policy_ref",
        "producer_tool_sha256",
    }
    if schema_version in {
        "official-client-evidence-package/v2",
        EVIDENCE_PACKAGE_V3_SCHEMA,
    }:
        expected_keys.add("completeness_ref")
    if schema_version == EVIDENCE_PACKAGE_V3_SCHEMA:
        expected_keys.add("evidence_items")
    else:
        expected_keys.add("rules")
    expect_exact_keys(
        payload,
        expected_keys,
        "evidence_package",
    )
    if schema_version not in {
        "official-client-evidence-package/v1",
        "official-client-evidence-package/v2",
        EVIDENCE_PACKAGE_V3_SCHEMA,
    }:
        raise ControlError("evidence_package.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_string(payload["version"], "evidence_package.version")
    artifacts = payload["official_artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ControlError("evidence_package.official_artifacts 必须是非空数组")
    for index, item in enumerate(artifacts):
        validate_external_binding(item, f"evidence_package.official_artifacts[{index}]")
    expect_string_list(payload["platforms"], "evidence_package.platforms")
    expect_string_list(payload["entrypoints"], "evidence_package.entrypoints")
    expect_string_list(payload["default_conditions"], "evidence_package.default_conditions")
    comparison_policy_ref = validate_object_ref(
        payload["comparison_policy_ref"], "evidence_package.comparison_policy_ref"
    )
    if comparison_policy_ref["object_kind"] != "operational_evidence":
        raise ControlError("evidence_package.comparison_policy_ref 必须引用 operational_evidence")
    if schema_version in {
        "official-client-evidence-package/v2",
        EVIDENCE_PACKAGE_V3_SCHEMA,
    }:
        completeness_ref = validate_object_ref(
            payload["completeness_ref"], "evidence_package.completeness_ref"
        )
        if completeness_ref["object_kind"] != "operational_evidence":
            raise ControlError("evidence_package.completeness_ref 必须引用 operational_evidence")
    expect_sha256(payload["producer_tool_sha256"], "evidence_package.producer_tool_sha256")
    if schema_version == EVIDENCE_PACKAGE_V3_SCHEMA:
        items = payload["evidence_items"]
        if not isinstance(items, list) or not items:
            raise ControlError("evidence_package.evidence_items 必须是非空数组")
        normalized_items = [
            _validate_evidence_item(item, f"evidence_items[{index}]")
            for index, item in enumerate(items)
        ]
        evidence_ids = [item["evidence_id"] for item in normalized_items]
        if evidence_ids != sorted(set(evidence_ids)):
            raise ControlError(
                "evidence_package.evidence_items 必须按 evidence_id 排序且不得重复"
            )
    else:
        rules = payload["rules"]
        if not isinstance(rules, list) or not rules:
            raise ControlError("evidence_package.rules 必须是非空数组")
        normalized_rules = [
            _validate_legacy_evidence_rule(item, f"rules[{index}]")
            for index, item in enumerate(rules)
        ]
        spec_ids = [item["spec_id"] for item in normalized_rules]
        if spec_ids != sorted(set(spec_ids)):
            raise ControlError("evidence_package.rules 必须按 spec_id 排序且不得重复")
    return payload


def _validate_spec_ids(
    value: Any,
    label: str,
    *,
    non_empty: bool = True,
) -> list[str]:
    spec_ids = expect_string_list(value, label, non_empty=non_empty)
    for spec_id in spec_ids:
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise ControlError(f"{label} 包含非法 SPEC：{spec_id}")
    return spec_ids


def rule_migration_ledger_identity_sha256(payload: dict[str, Any]) -> str:
    """复算 RuleMigrationLedger 的不可变身份。"""

    return canonical_sha256(
        {key: value for key, value in payload.items() if key != "identity_sha256"}
    )


def _validate_rule_migration_ledger(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "rule_migration_ledger")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "baseline_version",
            "target_version",
            "evidence_package_ref",
            "baseline_spec_ids",
            "target_spec_ids",
            "rules",
            "affected_rules",
            "inherited_rules",
            "identity_sha256",
        },
        "rule_migration_ledger",
    )
    if payload["schema_version"] != RULE_MIGRATION_LEDGER_SCHEMA:
        raise ControlError("rule_migration_ledger.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "rule_migration_ledger.campaign_id")
    validate_persona(payload["persona"], "rule_migration_ledger.persona")
    expect_string(payload["baseline_version"], "rule_migration_ledger.baseline_version")
    expect_string(payload["target_version"], "rule_migration_ledger.target_version")
    evidence_ref = validate_object_ref(
        payload["evidence_package_ref"],
        "rule_migration_ledger.evidence_package_ref",
    )
    if evidence_ref["object_kind"] != "evidence_package":
        raise ControlError("rule_migration_ledger 必须引用 EvidencePackage")

    baseline_spec_ids = _validate_spec_ids(
        payload["baseline_spec_ids"],
        "rule_migration_ledger.baseline_spec_ids",
        non_empty=False,
    )
    target_spec_ids = _validate_spec_ids(
        payload["target_spec_ids"], "rule_migration_ledger.target_spec_ids"
    )
    rules = payload["rules"]
    if not isinstance(rules, list) or not rules:
        raise ControlError("rule_migration_ledger.rules 必须是非空数组")
    normalized_rules: list[dict[str, Any]] = []
    rule_ids: list[str] = []
    for index, raw in enumerate(rules):
        label = f"rule_migration_ledger.rules[{index}]"
        rule = expect_object(raw, label)
        expect_exact_keys(
            rule,
            {
                "spec_id",
                "migration_decision",
                "evidence_level",
                "rule_lifecycle",
                "compatibility_class",
                "evidence_item_ids",
                "applicability",
            },
            label,
        )
        spec_id = expect_string(rule["spec_id"], f"{label}.spec_id")
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise ControlError(f"{label}.spec_id 非法")
        rule_ids.append(spec_id)
        _expect_enum(
            rule["migration_decision"],
            MIGRATION_DECISIONS,
            f"{label}.migration_decision",
        )
        _expect_enum(rule["evidence_level"], EVIDENCE_LEVELS, f"{label}.evidence_level")
        _expect_enum(
            rule["rule_lifecycle"], RULE_LIFECYCLES, f"{label}.rule_lifecycle"
        )
        _expect_enum(
            rule["compatibility_class"],
            COMPATIBILITY_CLASSES,
            f"{label}.compatibility_class",
        )
        evidence_item_ids = expect_string_list(
            rule["evidence_item_ids"], f"{label}.evidence_item_ids"
        )
        for item_index, evidence_id in enumerate(evidence_item_ids):
            expect_safe_id(evidence_id, f"{label}.evidence_item_ids[{item_index}]")
        expect_string_list(rule["applicability"], f"{label}.applicability")
        normalized_rules.append(rule)
    if rule_ids != sorted(set(rule_ids)):
        raise ControlError("rule_migration_ledger.rules 必须按 spec_id 排序且不得重复")

    baseline_set = set(baseline_spec_ids)
    target_set = set(target_spec_ids)
    if set(rule_ids) != baseline_set | target_set:
        raise ControlError("RuleMigrationLedger 规则分母不等于 baseline／target 并集")
    for rule in normalized_rules:
        spec_id = rule["spec_id"]
        decision = rule["migration_decision"]
        in_baseline = spec_id in baseline_set
        in_target = spec_id in target_set
        if decision == "add" and (in_baseline or not in_target):
            raise ControlError(f"{spec_id} add 必须只存在于目标规则集")
        if decision == "delete" and (not in_baseline or in_target):
            raise ControlError(f"{spec_id} delete 必须只存在于基线规则集")
        if decision in {"inherit", "change", "condition_change"} and not (
            in_baseline and in_target
        ):
            raise ControlError(f"{spec_id} {decision} 必须同时存在于基线与目标规则集")
        if decision == "delete" and rule["rule_lifecycle"] != "superseded":
            raise ControlError(f"{spec_id} delete 必须使用 superseded 生命周期")

    affected_rules = _validate_spec_ids(
        payload["affected_rules"],
        "rule_migration_ledger.affected_rules",
        non_empty=False,
    )
    inherited_rules = _validate_spec_ids(
        payload["inherited_rules"],
        "rule_migration_ledger.inherited_rules",
        non_empty=False,
    )
    expected_affected = sorted(
        rule["spec_id"]
        for rule in normalized_rules
        if rule["migration_decision"] != "inherit"
    )
    expected_inherited = sorted(
        rule["spec_id"]
        for rule in normalized_rules
        if rule["migration_decision"] == "inherit"
    )
    if affected_rules != expected_affected or inherited_rules != expected_inherited:
        raise ControlError(
            "RuleMigrationLedger 的 affected_rules／inherited_rules 不能由迁移决定复算"
        )
    if set(affected_rules) & set(inherited_rules):
        raise ControlError("RuleMigrationLedger 的 affected_rules／inherited_rules 必须互斥")
    if payload["identity_sha256"] != rule_migration_ledger_identity_sha256(payload):
        raise ControlError("rule_migration_ledger.identity_sha256 与分类内容不一致")
    return payload


def atomic_assertion_ledger_identity_sha256(payload: dict[str, Any]) -> str:
    """复算 AtomicAssertionLedger 的不可变身份。"""

    return canonical_sha256(
        {key: value for key, value in payload.items() if key != "identity_sha256"}
    )


def _validate_atomic_assertion_ledger(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "atomic_assertion_ledger")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "target_version",
            "rule_migration_ledger_ref",
            "scenario_only_owner_ids",
            "assertions",
            "identity_sha256",
        },
        "atomic_assertion_ledger",
    )
    if payload["schema_version"] != ATOMIC_ASSERTION_LEDGER_SCHEMA:
        raise ControlError("atomic_assertion_ledger.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "atomic_assertion_ledger.campaign_id")
    validate_persona(payload["persona"], "atomic_assertion_ledger.persona")
    expect_string(payload["target_version"], "atomic_assertion_ledger.target_version")
    ledger_ref = validate_object_ref(
        payload["rule_migration_ledger_ref"],
        "atomic_assertion_ledger.rule_migration_ledger_ref",
    )
    if ledger_ref["object_kind"] != "rule_migration_ledger":
        raise ControlError("atomic_assertion_ledger 必须引用 RuleMigrationLedger")
    scenario_owner_ids = expect_string_list(
        payload["scenario_only_owner_ids"],
        "atomic_assertion_ledger.scenario_only_owner_ids",
        non_empty=False,
    )
    for index, owner_id in enumerate(scenario_owner_ids):
        expect_safe_id(
            owner_id, f"atomic_assertion_ledger.scenario_only_owner_ids[{index}]"
        )
    assertions = payload["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise ControlError("atomic_assertion_ledger.assertions 必须是非空数组")
    assertion_ids: list[str] = []
    scenario_owners: set[str] = set()
    channel_order = {channel: index for index, channel in enumerate(("P", "R", "J", "M"))}
    for index, raw in enumerate(assertions):
        label = f"atomic_assertion_ledger.assertions[{index}]"
        assertion = expect_object(raw, label)
        expect_exact_keys(
            assertion,
            {
                "assertion_id",
                "owner_kind",
                "owner_id",
                "evidence_item_ids",
                "conditions",
                "channels",
                "result",
            },
            label,
        )
        assertion_ids.append(
            expect_safe_id(assertion["assertion_id"], f"{label}.assertion_id")
        )
        owner_kind = _expect_enum(
            assertion["owner_kind"],
            {"required_rule", "scenario_only"},
            f"{label}.owner_kind",
        )
        owner_id = expect_safe_id(assertion["owner_id"], f"{label}.owner_id")
        if owner_kind == "required_rule":
            if not SPEC_ID_RE.fullmatch(owner_id):
                raise ControlError(f"{label}.owner_id 必须是目标 SPEC")
        else:
            scenario_owners.add(owner_id)
        evidence_item_ids = expect_string_list(
            assertion["evidence_item_ids"], f"{label}.evidence_item_ids"
        )
        for item_index, evidence_id in enumerate(evidence_item_ids):
            expect_safe_id(evidence_id, f"{label}.evidence_item_ids[{item_index}]")
        expect_string_list(assertion["conditions"], f"{label}.conditions")
        channels = assertion["channels"]
        if not isinstance(channels, list) or not channels:
            raise ControlError(f"{label}.channels 必须是非空数组")
        if any(channel not in channel_order for channel in channels):
            raise ControlError(f"{label}.channels 只能包含 P／R／J／M")
        if channels != sorted(set(channels), key=channel_order.__getitem__):
            raise ControlError(f"{label}.channels 必须按 P／R／J／M 排序且不得重复")
        _expect_enum(assertion["result"], {"pass", "observed"}, f"{label}.result")
    if assertion_ids != sorted(set(assertion_ids)):
        raise ControlError(
            "atomic_assertion_ledger.assertions 必须按 assertion_id 排序且不得重复"
        )
    if scenario_owners != set(scenario_owner_ids):
        raise ControlError("AtomicAssertionLedger 的 scenario-only owner 未闭合")
    if payload["identity_sha256"] != atomic_assertion_ledger_identity_sha256(payload):
        raise ControlError("atomic_assertion_ledger.identity_sha256 与断言内容不一致")
    return payload


def _validate_vircs_state(value: Any, label: str) -> dict[str, Any]:
    """校验 Claude 候选链对用户管理生产机的固定未触碰声明。"""

    state = expect_object(value, label)
    expect_exact_keys(state, {"ownership", "verification", "action"}, label)
    expected = {
        "ownership": "operator_managed",
        "verification": "unverified",
        "action": "not_touched",
    }
    if state != expected:
        raise ControlError(
            f"{label} 必须固定为 operator_managed／unverified／not_touched"
        )
    return state


def _validate_run_conditions(value: Any, label: str) -> dict[str, Any]:
    conditions = expect_object(value, label)
    expect_exact_keys(
        conditions,
        {
            "run_nonce",
            "environment_sha256",
            "tool_sha256",
            "configuration_sha256",
            "network_sha256",
            "account_model_sha256",
        },
        label,
    )
    expect_safe_id(conditions["run_nonce"], f"{label}.run_nonce")
    for key in (
        "environment_sha256",
        "tool_sha256",
        "configuration_sha256",
        "network_sha256",
        "account_model_sha256",
    ):
        expect_sha256(conditions[key], f"{label}.{key}")
    return conditions


def _validate_validation_execution_plan(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "validation_execution_plan")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "plan_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "profile_approval_ref",
            "scenario_plan_ref",
            "atomic_assertion_ledger_ref",
            "validation_scope_sha256",
            "execute_item_ids",
            "reuse_item_ids",
            "external_gates",
            "run_conditions",
            "production_selector_before_ref",
            "vircs_state",
            "identity_sha256",
        },
        "validation_execution_plan",
    )
    if payload["schema_version"] != VALIDATION_EXECUTION_PLAN_SCHEMA:
        raise ControlError("validation_execution_plan.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "validation_execution_plan.campaign_id")
    validate_persona(payload["persona"], "validation_execution_plan.persona")
    expect_safe_id(payload["plan_id"], "validation_execution_plan.plan_id")
    expect_safe_id(payload["candidate_id"], "validation_execution_plan.candidate_id")
    validate_fact_ref(payload["candidate_ref"], "validation_execution_plan.candidate_ref")
    build_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "validation_execution_plan.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("validation_execution_plan 必须引用 CandidateBuildReceipt")
    validate_fact_ref(
        payload["profile_approval_ref"],
        "validation_execution_plan.profile_approval_ref",
    )
    expected_objects = {
        "scenario_plan_ref": "scenario_plan",
        "atomic_assertion_ledger_ref": "atomic_assertion_ledger",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(payload[key], f"validation_execution_plan.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"validation_execution_plan.{key} 类型错误")
    expect_sha256(
        payload["validation_scope_sha256"],
        "validation_execution_plan.validation_scope_sha256",
    )
    execute_ids = expect_string_list(
        payload["execute_item_ids"], "validation_execution_plan.execute_item_ids"
    )
    reuse_ids = expect_string_list(
        payload["reuse_item_ids"],
        "validation_execution_plan.reuse_item_ids",
        non_empty=False,
    )
    if set(execute_ids) & set(reuse_ids):
        raise ControlError("validation_execution_plan execute／reuse 集合不得相交")
    gates = payload["external_gates"]
    if not isinstance(gates, list) or not gates:
        raise ControlError("validation_execution_plan.external_gates 必须是非空数组")
    gate_ids: list[str] = []
    for index, raw in enumerate(gates):
        label = f"validation_execution_plan.external_gates[{index}]"
        gate = expect_object(raw, label)
        expect_exact_keys(
            gate,
            {
                "gate_id",
                "requirement_sha256",
                "source",
                "command",
                "working_directory",
                "host_id",
                "architecture",
                "reused_receipt_ref",
                "previous_failed_receipt_ref",
            },
            label,
        )
        gate_id = expect_safe_id(gate["gate_id"], f"{label}.gate_id")
        gate_ids.append(gate_id)
        expect_sha256(gate["requirement_sha256"], f"{label}.requirement_sha256")
        source = _expect_enum(gate["source"], {"execute", "reuse"}, f"{label}.source")
        command = expect_string_list(
            gate["command"], f"{label}.command", sorted_unique=False
        )
        if not command:
            raise ControlError(f"{label}.command 不得为空")
        expect_string(gate["working_directory"], f"{label}.working_directory")
        expect_safe_id(gate["host_id"], f"{label}.host_id")
        expect_string(gate["architecture"], f"{label}.architecture")
        reused = gate["reused_receipt_ref"]
        previous_failed = gate["previous_failed_receipt_ref"]
        if source == "execute":
            if gate_id not in execute_ids or reused is not None:
                raise ControlError(f"{label} execute 身份与动作清单不一致")
            if previous_failed is not None:
                reference = validate_receipt_ref(
                    previous_failed, f"{label}.previous_failed_receipt_ref"
                )
                if reference["receipt_kind"] != "validation_gate":
                    raise ControlError(f"{label} 前序失败必须引用 validation_gate 收据")
        else:
            if gate_id not in reuse_ids or previous_failed is not None:
                raise ControlError(f"{label} reuse 身份与动作清单不一致")
            reference = validate_receipt_ref(reused, f"{label}.reused_receipt_ref")
            if reference["receipt_kind"] != "validation_gate":
                raise ControlError(f"{label} 必须复用 validation_gate 收据")
    if gate_ids != sorted(set(gate_ids)):
        raise ControlError("validation_execution_plan.external_gates 必须按 gate_id 排序且不得重复")
    _validate_run_conditions(payload["run_conditions"], "validation_execution_plan.run_conditions")
    validate_fact_ref(
        payload["production_selector_before_ref"],
        "validation_execution_plan.production_selector_before_ref",
    )
    _validate_vircs_state(payload["vircs_state"], "validation_execution_plan.vircs_state")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("validation_execution_plan.identity_sha256 与计划内容不一致")
    return payload


def _validate_candidate_evidence_package(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "candidate_evidence_package")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "target_version",
            "package_id",
            "candidate_id",
            "attempt_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_attempt_ref",
            "atomic_assertion_ledger_ref",
            "scenario_approval_refs",
            "scenario_assertion_results",
            "raw_evidence_inventory",
            "environment_before_ref",
            "environment_after_ref",
            "recovery_evidence_ref",
            "secret_scan_evidence_ref",
            "artifact_count",
            "inventory_sha256",
            "identity_sha256",
        },
        "candidate_evidence_package",
    )
    if payload["schema_version"] != CANDIDATE_EVIDENCE_PACKAGE_SCHEMA:
        raise ControlError("candidate_evidence_package.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "candidate_evidence_package.campaign_id")
    validate_persona(payload["persona"], "candidate_evidence_package.persona")
    expect_string(payload["target_version"], "candidate_evidence_package.target_version")
    for key in ("package_id", "candidate_id", "attempt_id"):
        expect_safe_id(payload[key], f"candidate_evidence_package.{key}")
    validate_fact_ref(payload["candidate_ref"], "candidate_evidence_package.candidate_ref")
    receipt_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "candidate_evidence_package.candidate_build_receipt_ref",
    )
    if receipt_ref["receipt_kind"] != "candidate_build":
        raise ControlError("candidate_evidence_package 必须引用 CandidateBuildReceipt")
    validate_fact_ref(
        payload["validation_attempt_ref"],
        "candidate_evidence_package.validation_attempt_ref",
    )
    ledger_ref = validate_object_ref(
        payload["atomic_assertion_ledger_ref"],
        "candidate_evidence_package.atomic_assertion_ledger_ref",
    )
    if ledger_ref["object_kind"] != "atomic_assertion_ledger":
        raise ControlError("candidate_evidence_package 必须引用 AtomicAssertionLedger")
    _validate_ref_list(
        payload["scenario_approval_refs"],
        "candidate_evidence_package.scenario_approval_refs",
        validate_fact_ref,
    )
    results = payload["scenario_assertion_results"]
    if not isinstance(results, list):
        raise ControlError("candidate_evidence_package.scenario_assertion_results 必须是数组")
    result_ids: list[str] = []
    for index, raw in enumerate(results):
        label = f"candidate_evidence_package.scenario_assertion_results[{index}]"
        result = expect_object(raw, label)
        expect_exact_keys(
            result,
            {"assertion_id", "owner_id", "result", "source", "scenario_approval_refs"},
            label,
        )
        result_ids.append(expect_safe_id(result["assertion_id"], f"{label}.assertion_id"))
        expect_safe_id(result["owner_id"], f"{label}.owner_id")
        if result["result"] != "pass":
            raise ControlError(f"{label}.result 必须是 pass")
        _expect_enum(result["source"], {"execute", "reuse"}, f"{label}.source")
        _validate_ref_list(
            result["scenario_approval_refs"],
            f"{label}.scenario_approval_refs",
            validate_fact_ref,
        )
    if result_ids != sorted(set(result_ids)):
        raise ControlError("candidate_evidence_package 场景断言必须按 assertion_id 排序且不得重复")
    inventory = payload["raw_evidence_inventory"]
    if not isinstance(inventory, list) or not inventory:
        raise ControlError("candidate_evidence_package.raw_evidence_inventory 必须是非空数组")
    bindings = [
        validate_external_binding(item, f"candidate_evidence_package.raw_evidence_inventory[{index}]")
        for index, item in enumerate(inventory)
    ]
    if [item["path"] for item in bindings] != sorted({item["path"] for item in bindings}):
        raise ControlError("candidate_evidence_package.raw_evidence_inventory 必须按路径排序且不得重复")
    expected_objects = {
        "environment_before_ref": "operational_evidence",
        "environment_after_ref": "operational_evidence",
        "recovery_evidence_ref": "operational_evidence",
        "secret_scan_evidence_ref": "operational_evidence",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(payload[key], f"candidate_evidence_package.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"candidate_evidence_package.{key} 类型错误")
    count = expect_non_negative_int(
        payload["artifact_count"], "candidate_evidence_package.artifact_count"
    )
    if count != len(bindings):
        raise ControlError("candidate_evidence_package.artifact_count 与 inventory 不一致")
    if payload["inventory_sha256"] != canonical_sha256(bindings):
        raise ControlError("candidate_evidence_package.inventory_sha256 与 inventory 不一致")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("candidate_evidence_package.identity_sha256 与封存内容不一致")
    return payload


def _validate_delivery_stage_checks(value: Any, label: str) -> dict[str, Any]:
    checks = expect_object(value, label)
    expect_exact_keys(checks, CANDIDATE_DELIVERY_STAGES, label)
    for stage in CANDIDATE_DELIVERY_STAGES:
        expect_string_list(checks[stage], f"{label}.{stage}")
    return checks


def _validate_candidate_delivery_plan(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "candidate_delivery_plan")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "plan_id",
            "delivery_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "candidate_evidence_package_ref",
            "host_id",
            "architecture",
            "compose_sha256",
            "environment_sha256",
            "configuration_sha256",
            "network_sha256",
            "runtime_profile_sha256",
            "rollback_runtime_profile_sha256",
            "candidate_image_digest",
            "rollback_image_digest",
            "observation_window_seconds",
            "stage_checks",
            "environment_freeze_ref",
            "rollback_material_refs",
            "vircs_state",
            "identity_sha256",
        },
        "candidate_delivery_plan",
    )
    if payload["schema_version"] != CANDIDATE_DELIVERY_PLAN_SCHEMA:
        raise ControlError("candidate_delivery_plan.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "candidate_delivery_plan.campaign_id")
    validate_persona(payload["persona"], "candidate_delivery_plan.persona")
    for key in ("plan_id", "delivery_id", "candidate_id", "host_id"):
        expect_safe_id(payload[key], f"candidate_delivery_plan.{key}")
    validate_fact_ref(payload["candidate_ref"], "candidate_delivery_plan.candidate_ref")
    build_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "candidate_delivery_plan.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("candidate_delivery_plan 必须引用 CandidateBuildReceipt")
    validate_fact_ref(
        payload["validation_completed_ref"],
        "candidate_delivery_plan.validation_completed_ref",
    )
    validate_fact_ref(payload["acceptance_ref"], "candidate_delivery_plan.acceptance_ref")
    evidence_ref = validate_object_ref(
        payload["candidate_evidence_package_ref"],
        "candidate_delivery_plan.candidate_evidence_package_ref",
    )
    if evidence_ref["object_kind"] != "candidate_evidence_package":
        raise ControlError("candidate_delivery_plan 必须引用 CandidateEvidencePackage")
    expect_string(payload["architecture"], "candidate_delivery_plan.architecture")
    for key in (
        "compose_sha256",
        "environment_sha256",
        "configuration_sha256",
        "network_sha256",
        "runtime_profile_sha256",
        "rollback_runtime_profile_sha256",
    ):
        expect_sha256(payload[key], f"candidate_delivery_plan.{key}")
    expect_image_digest(
        payload["candidate_image_digest"],
        "candidate_delivery_plan.candidate_image_digest",
    )
    expect_image_digest(
        payload["rollback_image_digest"],
        "candidate_delivery_plan.rollback_image_digest",
    )
    if payload["candidate_image_digest"] == payload["rollback_image_digest"]:
        raise ControlError("candidate_delivery_plan 的 Candidate 与真实回退镜像不得相同")
    window = expect_non_negative_int(
        payload["observation_window_seconds"],
        "candidate_delivery_plan.observation_window_seconds",
    )
    if window < 1:
        raise ControlError("candidate_delivery_plan.observation_window_seconds 必须大于 0")
    _validate_delivery_stage_checks(payload["stage_checks"], "candidate_delivery_plan.stage_checks")
    freeze_ref = validate_object_ref(
        payload["environment_freeze_ref"],
        "candidate_delivery_plan.environment_freeze_ref",
    )
    if freeze_ref["object_kind"] != "operational_evidence":
        raise ControlError("candidate_delivery_plan.environment_freeze_ref 类型错误")
    _validate_ref_list(
        payload["rollback_material_refs"],
        "candidate_delivery_plan.rollback_material_refs",
        validate_object_ref,
    )
    _validate_vircs_state(payload["vircs_state"], "candidate_delivery_plan.vircs_state")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("candidate_delivery_plan.identity_sha256 与计划内容不一致")
    return payload


def _validate_private_archive_manifest(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "private_archive_manifest")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "manifest_id",
            "redacted",
            "entries",
            "entry_count",
            "total_bytes",
            "manifest_sha256",
            "identity_sha256",
        },
        "private_archive_manifest",
    )
    if payload["schema_version"] != PRIVATE_ARCHIVE_MANIFEST_SCHEMA:
        raise ControlError("private_archive_manifest.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "private_archive_manifest.campaign_id")
    validate_persona(payload["persona"], "private_archive_manifest.persona")
    expect_safe_id(payload["manifest_id"], "private_archive_manifest.manifest_id")
    if expect_bool(payload["redacted"], "private_archive_manifest.redacted") is not True:
        raise ControlError("private_archive_manifest.redacted 必须是 true")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ControlError("private_archive_manifest.entries 必须是非空数组")
    bindings = [
        validate_external_binding(item, f"private_archive_manifest.entries[{index}]")
        for index, item in enumerate(entries)
    ]
    if [item["path"] for item in bindings] != sorted({item["path"] for item in bindings}):
        raise ControlError("private_archive_manifest.entries 必须按路径排序且不得重复")
    if payload["entry_count"] != len(bindings):
        raise ControlError("private_archive_manifest.entry_count 与 entries 不一致")
    total_bytes = expect_non_negative_int(
        payload["total_bytes"], "private_archive_manifest.total_bytes"
    )
    if total_bytes != sum(item["bytes"] for item in bindings):
        raise ControlError("private_archive_manifest.total_bytes 与 entries 不一致")
    if payload["manifest_sha256"] != canonical_sha256(bindings):
        raise ControlError("private_archive_manifest.manifest_sha256 与 entries 不一致")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("private_archive_manifest.identity_sha256 与清单内容不一致")
    return payload


def _validate_candidate_delivery_package(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "candidate_delivery_package")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "package_id",
            "delivery_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "candidate_evidence_package_ref",
            "delivery_plan_ref",
            "delivery_fact_refs",
            "deployment_configuration_ref",
            "public_evidence_index_refs",
            "rollback_material_refs",
            "private_archive_manifest_ref",
            "candidate_image_digest",
            "artifact_count",
            "identity_sha256",
        },
        "candidate_delivery_package",
    )
    if payload["schema_version"] != CANDIDATE_DELIVERY_PACKAGE_SCHEMA:
        raise ControlError("candidate_delivery_package.schema_version 不匹配")
    expect_safe_id(payload["campaign_id"], "candidate_delivery_package.campaign_id")
    validate_persona(payload["persona"], "candidate_delivery_package.persona")
    for key in ("package_id", "delivery_id", "candidate_id"):
        expect_safe_id(payload[key], f"candidate_delivery_package.{key}")
    for key in ("candidate_ref", "validation_completed_ref", "acceptance_ref"):
        validate_fact_ref(payload[key], f"candidate_delivery_package.{key}")
    build_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "candidate_delivery_package.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("candidate_delivery_package 必须引用 CandidateBuildReceipt")
    expected_objects = {
        "candidate_evidence_package_ref": "candidate_evidence_package",
        "delivery_plan_ref": "candidate_delivery_plan",
        "deployment_configuration_ref": "operational_evidence",
        "private_archive_manifest_ref": "private_archive_manifest",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(payload[key], f"candidate_delivery_package.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"candidate_delivery_package.{key} 类型错误")
    fact_refs = payload["delivery_fact_refs"]
    if not isinstance(fact_refs, list) or len(fact_refs) != len(CANDIDATE_DELIVERY_STAGES):
        raise ControlError("candidate_delivery_package 必须绑定四阶段候选交付事实")
    for index, reference in enumerate(fact_refs):
        validate_fact_ref(reference, f"candidate_delivery_package.delivery_fact_refs[{index}]")
    for key in ("public_evidence_index_refs", "rollback_material_refs"):
        _validate_ref_list(
            payload[key], f"candidate_delivery_package.{key}", validate_object_ref
        )
    expect_image_digest(
        payload["candidate_image_digest"],
        "candidate_delivery_package.candidate_image_digest",
    )
    count = expect_non_negative_int(
        payload["artifact_count"], "candidate_delivery_package.artifact_count"
    )
    if count < 1:
        raise ControlError("candidate_delivery_package.artifact_count 必须大于 0")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("candidate_delivery_package.identity_sha256 与交付内容不一致")
    return payload


def _validate_profile_schema(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "profile_schema")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "version", "schema_id", "document"},
        "profile_schema",
    )
    if payload["schema_version"] != "official-client-profile-schema/v1":
        raise ControlError("profile_schema.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_string(payload["version"], "profile_schema.version")
    expect_safe_id(payload["schema_id"], "profile_schema.schema_id")
    expect_object(payload["document"], "profile_schema.document")
    return payload


def _validate_snapshot(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "snapshot")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "version",
            "profile_digest",
            "profile_schema_ref",
            "compiler_attestation_sha256",
            "document",
        },
        "snapshot",
    )
    if payload["schema_version"] != "official-client-snapshot/v1":
        raise ControlError("snapshot.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_string(payload["version"], "snapshot.version")
    expect_sha256(payload["profile_digest"], "snapshot.profile_digest")
    reference = validate_object_ref(payload["profile_schema_ref"], "snapshot.profile_schema_ref")
    if reference["object_kind"] != "profile_schema":
        raise ControlError("snapshot.profile_schema_ref 类型错误")
    expect_sha256(
        payload["compiler_attestation_sha256"],
        "snapshot.compiler_attestation_sha256",
    )
    expect_object(payload["document"], "snapshot.document")
    return payload


def _validate_release_bundle(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "release_bundle")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "version",
            "profile_digest",
            "snapshot_ref",
            "endpoint_digest",
            "transport_digest",
            "state_digest",
            "policy_digest",
            "document",
        },
        "release_bundle",
    )
    if payload["schema_version"] != "official-client-release-bundle/v1":
        raise ControlError("release_bundle.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_string(payload["version"], "release_bundle.version")
    expect_sha256(payload["profile_digest"], "release_bundle.profile_digest")
    reference = validate_object_ref(payload["snapshot_ref"], "release_bundle.snapshot_ref")
    if reference["object_kind"] != "snapshot":
        raise ControlError("release_bundle.snapshot_ref 类型错误")
    for key in ("endpoint_digest", "transport_digest", "state_digest", "policy_digest"):
        expect_sha256(payload[key], f"release_bundle.{key}")
    expect_object(payload["document"], "release_bundle.document")
    return payload


def _assert_no_deployment_roles(value: Any, label: str = "release_artifact") -> None:
    forbidden = {
        "active",
        "rollback",
        "candidate",
        "production_active",
        "production_rollback",
        "selector",
        "runtime_selector",
        "deployment_role",
    }
    if isinstance(value, dict):
        overlap = sorted(forbidden & {str(key).lower() for key in value})
        if overlap:
            raise ControlError(f"{label} 不得保存部署角色字段：{overlap}")
        for item in value.values():
            _assert_no_deployment_roles(item, label)
    elif isinstance(value, list):
        for item in value:
            _assert_no_deployment_roles(item, label)


def _validate_release_artifact(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "release_artifact")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "version",
            "profile_digest",
            "snapshot_ref",
            "release_bundle_ref",
        },
        "release_artifact",
    )
    if payload["schema_version"] != "official-client-release-artifact/v1":
        raise ControlError("release_artifact.schema_version 不匹配")
    _assert_no_deployment_roles(payload)
    validate_persona(payload["persona"])
    expect_string(payload["version"], "release_artifact.version")
    expect_sha256(payload["profile_digest"], "release_artifact.profile_digest")
    snapshot = validate_object_ref(payload["snapshot_ref"], "release_artifact.snapshot_ref")
    bundle = validate_object_ref(
        payload["release_bundle_ref"], "release_artifact.release_bundle_ref"
    )
    if snapshot["object_kind"] != "snapshot" or bundle["object_kind"] != "release_bundle":
        raise ControlError("release_artifact 的 Snapshot／ReleaseBundle 引用类型错误")
    return payload


def _validate_ingress_observation(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "ingress_observation")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "observed_at_utc", "source_refs", "aliases"},
        "ingress_observation",
    )
    if payload["schema_version"] != "official-client-ingress-observation/v1":
        raise ControlError("ingress_observation.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_rfc3339(payload["observed_at_utc"], "ingress_observation.observed_at_utc")
    _validate_ref_list(payload["source_refs"], "ingress_observation.source_refs", validate_object_ref)
    aliases = payload["aliases"]
    if not isinstance(aliases, list) or not aliases:
        raise ControlError("ingress_observation.aliases 必须是非空数组")
    alias_ids: list[str] = []
    for index, raw in enumerate(aliases):
        label = f"ingress_observation.aliases[{index}]"
        item = expect_object(raw, label)
        expect_exact_keys(
            item,
            {"alias_id", "logical_ingress_id", "physical_route", "caller_ids"},
            label,
        )
        alias_ids.append(expect_safe_id(item["alias_id"], f"{label}.alias_id"))
        expect_safe_id(item["logical_ingress_id"], f"{label}.logical_ingress_id")
        expect_string(item["physical_route"], f"{label}.physical_route")
        expect_string_list(item["caller_ids"], f"{label}.caller_ids")
    if alias_ids != sorted(set(alias_ids)):
        raise ControlError("ingress_observation.aliases 必须按 alias_id 排序且不得重复")
    return payload


def _validate_egress_observation(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "egress_observation")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "observed_at_utc", "source_refs", "egresses"},
        "egress_observation",
    )
    if payload["schema_version"] != "official-client-egress-observation/v1":
        raise ControlError("egress_observation.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_rfc3339(payload["observed_at_utc"], "egress_observation.observed_at_utc")
    _validate_ref_list(payload["source_refs"], "egress_observation.source_refs", validate_object_ref)
    egresses = payload["egresses"]
    if not isinstance(egresses, list) or not egresses:
        raise ControlError("egress_observation.egresses 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(egresses):
        label = f"egress_observation.egresses[{index}]"
        item = expect_object(raw, label)
        expect_exact_keys(
            item,
            {"egress_id", "route_id", "sink_id", "oauth_related", "kind"},
            label,
        )
        identities.append(expect_safe_id(item["egress_id"], f"{label}.egress_id"))
        expect_safe_id(item["route_id"], f"{label}.route_id")
        expect_safe_id(item["sink_id"], f"{label}.sink_id")
        expect_bool(item["oauth_related"], f"{label}.oauth_related")
        _expect_enum(
            item["kind"],
            {"inference", "lifecycle", "auxiliary"},
            f"{label}.kind",
        )
    if identities != sorted(set(identities)):
        raise ControlError("egress_observation.egresses 必须按 egress_id 排序且不得重复")
    return payload


def _validate_production_ingress_inventory(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "production_ingress_inventory")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "observation_ref", "entries"},
        "production_ingress_inventory",
    )
    if payload["schema_version"] != "official-client-production-ingress-inventory/v1":
        raise ControlError("production_ingress_inventory.schema_version 不匹配")
    validate_persona(payload["persona"])
    observation = validate_object_ref(
        payload["observation_ref"], "production_ingress_inventory.observation_ref"
    )
    if observation["object_kind"] != "ingress_observation":
        raise ControlError("production_ingress_inventory.observation_ref 类型错误")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ControlError("production_ingress_inventory.entries 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(entries):
        label = f"production_ingress_inventory.entries[{index}]"
        item = expect_object(raw, label)
        expect_exact_keys(
            item,
            {
                "logical_ingress_id",
                "physical_alias_ids",
                "caller_ids",
                "adapter_id",
                "route_id",
                "ingress_kind",
                "protocol_class",
                "current_disposition",
                "evidence_refs",
            },
            label,
        )
        identities.append(
            expect_safe_id(item["logical_ingress_id"], f"{label}.logical_ingress_id")
        )
        expect_string_list(item["physical_alias_ids"], f"{label}.physical_alias_ids")
        expect_string_list(item["caller_ids"], f"{label}.caller_ids")
        expect_safe_id(item["adapter_id"], f"{label}.adapter_id")
        expect_safe_id(item["route_id"], f"{label}.route_id")
        _expect_enum(
            item["ingress_kind"],
            {"official", "third_party"},
            f"{label}.ingress_kind",
        )
        expect_safe_id(item["protocol_class"], f"{label}.protocol_class")
        _expect_enum(
            item["current_disposition"],
            INGRESS_DISPOSITIONS,
            f"{label}.current_disposition",
        )
        _validate_ref_list(item["evidence_refs"], f"{label}.evidence_refs", validate_object_ref)
    if identities != sorted(set(identities)):
        raise ControlError(
            "production_ingress_inventory.entries 必须按 logical_ingress_id 排序且不得重复"
        )
    return payload


def _validate_managed_policy(value: Any, label: str) -> dict[str, Any]:
    policy = expect_object(value, label)
    expect_exact_keys(
        policy,
        {
            "authentication",
            "endpoint",
            "client",
            "timeout_policy",
            "retry_policy",
            "secret_policy",
            "audit_policy",
        },
        label,
    )
    for key in policy:
        expect_string(policy[key], f"{label}.{key}")
    return policy


def _validate_egress_disposition_inventory(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "egress_disposition_inventory")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "observation_ref", "entries"},
        "egress_disposition_inventory",
    )
    if payload["schema_version"] != "official-client-egress-disposition-inventory/v1":
        raise ControlError("egress_disposition_inventory.schema_version 不匹配")
    validate_persona(payload["persona"])
    observation = validate_object_ref(
        payload["observation_ref"], "egress_disposition_inventory.observation_ref"
    )
    if observation["object_kind"] != "egress_observation":
        raise ControlError("egress_disposition_inventory.observation_ref 类型错误")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ControlError("egress_disposition_inventory.entries 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(entries):
        label = f"egress_disposition_inventory.entries[{index}]"
        item = expect_object(raw, label)
        expect_exact_keys(
            item,
            {
                "egress_id",
                "current_disposition",
                "current_guard_state",
                "spec_ids",
                "managed_policy",
                "runtime_assertion_refs",
            },
            label,
        )
        identities.append(expect_safe_id(item["egress_id"], f"{label}.egress_id"))
        disposition = _expect_enum(
            item["current_disposition"],
            EGRESS_DISPOSITIONS,
            f"{label}.current_disposition",
        )
        guard_state = _expect_enum(
            item["current_guard_state"],
            EGRESS_GUARD_STATES,
            f"{label}.current_guard_state",
        )
        spec_ids = expect_string_list(
            item["spec_ids"], f"{label}.spec_ids", non_empty=False
        )
        for spec_id in spec_ids:
            if not SPEC_ID_RE.fullmatch(spec_id):
                raise ControlError(f"{label}.spec_ids 包含非法 SPEC")
        if disposition == "persona_strict" and not spec_ids:
            raise ControlError(f"{label} persona_strict 必须绑定 SPEC")
        if disposition != "persona_strict" and spec_ids:
            raise ControlError(f"{label} 非 strict 出站不得伪装成 SPEC 责任")
        if guard_state == "source_absent" and disposition != "denied":
            raise ControlError(f"{label} source_absent 只能对应 denied")
        if guard_state in {"out_of_scope_passthrough", "legacy_observe"} and disposition == "persona_strict":
            raise ControlError(f"{label} 未 enforce 的路径不得冒充 persona_strict")
        policy = item["managed_policy"]
        if disposition == "non_persona_managed":
            _validate_managed_policy(policy, f"{label}.managed_policy")
        elif policy is not None:
            raise ControlError(f"{label}.managed_policy 只允许 non_persona_managed 使用")
        _validate_ref_list(
            item["runtime_assertion_refs"],
            f"{label}.runtime_assertion_refs",
            validate_object_ref,
        )
    if identities != sorted(set(identities)):
        raise ControlError("egress_disposition_inventory.entries 必须按 egress_id 排序且不得重复")
    return payload


CAPABILITY_KEYS = {
    "platform",
    "logical_ingress_id",
    "protocol_class",
    "privacy_mode",
    "model",
    "feature",
    "endpoint",
}


def validate_capability(value: Any, label: str) -> dict[str, Any]:
    capability = expect_object(value, label)
    expect_exact_keys(capability, CAPABILITY_KEYS, label)
    for key in sorted(CAPABILITY_KEYS):
        expect_string(capability[key], f"{label}.{key}")
    return capability


def capability_key(value: Any) -> str:
    capability = validate_capability(value, "capability")
    return "\x00".join(capability[key] for key in sorted(CAPABILITY_KEYS))


def _validate_capabilities(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError(f"{label} 必须是非空能力数组")
    result = [validate_capability(item, f"{label}[{index}]") for index, item in enumerate(value)]
    keys = [capability_key(item) for item in result]
    if keys != sorted(set(keys)):
        raise ControlError(f"{label} 必须严格排序且不得重复")
    return result


def _validate_support_envelope(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "support_envelope")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "capabilities",
            "unsupported_conditions",
            "target_spec_ids",
            "production_ingress_inventory_ref",
            "boundary_assertion_refs",
        },
        "support_envelope",
    )
    if payload["schema_version"] != "official-client-support-envelope/v1":
        raise ControlError("support_envelope.schema_version 不匹配")
    validate_persona(payload["persona"])
    _validate_capabilities(payload["capabilities"], "support_envelope.capabilities")
    expect_string_list(
        payload["unsupported_conditions"],
        "support_envelope.unsupported_conditions",
        non_empty=False,
    )
    spec_ids = expect_string_list(payload["target_spec_ids"], "support_envelope.target_spec_ids")
    for spec_id in spec_ids:
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise ControlError("support_envelope.target_spec_ids 包含非法 SPEC")
    inventory = validate_object_ref(
        payload["production_ingress_inventory_ref"],
        "support_envelope.production_ingress_inventory_ref",
    )
    if inventory["object_kind"] != "production_ingress_inventory":
        raise ControlError("support_envelope 必须绑定 ProductionIngressInventory")
    _validate_ref_list(
        payload["boundary_assertion_refs"],
        "support_envelope.boundary_assertion_refs",
        validate_object_ref,
    )
    return payload


def _validate_active_support_envelope(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "active_support_envelope")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "support_envelope_ref",
            "profile_approval_ref",
            "acceptance_ref",
            "release_artifact_ref",
            "capabilities",
        },
        "active_support_envelope",
    )
    if payload["schema_version"] != "official-client-active-support-envelope/v1":
        raise ControlError("active_support_envelope.schema_version 不匹配")
    validate_persona(payload["persona"])
    support = validate_object_ref(
        payload["support_envelope_ref"], "active_support_envelope.support_envelope_ref"
    )
    if support["object_kind"] != "support_envelope":
        raise ControlError("active_support_envelope.support_envelope_ref 类型错误")
    validate_fact_ref(payload["profile_approval_ref"], "active_support_envelope.profile_approval_ref")
    validate_fact_ref(payload["acceptance_ref"], "active_support_envelope.acceptance_ref")
    release = validate_object_ref(
        payload["release_artifact_ref"], "active_support_envelope.release_artifact_ref"
    )
    if release["object_kind"] != "release_artifact":
        raise ControlError("active_support_envelope.release_artifact_ref 类型错误")
    _validate_capabilities(payload["capabilities"], "active_support_envelope.capabilities")
    return payload


def _validate_operational_bindings(value: Any, label: str) -> dict[str, Any]:
    bindings = expect_object(value, label)
    expect_exact_keys(
        bindings,
        {
            "image_digest",
            "selector_snapshot_ref",
            "configuration_ref",
            "dependencies_ref",
            "routes_ref",
            "exercise_receipt_ref",
        },
        label,
    )
    expect_image_digest(bindings["image_digest"], f"{label}.image_digest")
    for key in (
        "selector_snapshot_ref",
        "configuration_ref",
        "dependencies_ref",
        "routes_ref",
        "exercise_receipt_ref",
    ):
        reference = validate_object_ref(bindings[key], f"{label}.{key}")
        if reference["object_kind"] not in {
            "runtime_catalog_snapshot",
            "operational_evidence",
        }:
            raise ControlError(f"{label}.{key} 必须引用运行或操作证据")
    return bindings


def _validate_rollback_operational_envelope(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "rollback_operational_envelope")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "rollback_kind",
            "rollback_release_ref",
            "operational_bindings",
            "capabilities",
            "wire_evidence_scope",
        },
        "rollback_operational_envelope",
    )
    if payload["schema_version"] != "official-client-rollback-operational-envelope/v1":
        raise ControlError("rollback_operational_envelope.schema_version 不匹配")
    validate_persona(payload["persona"])
    kind = _expect_enum(
        payload["rollback_kind"],
        {"approved_release", "legacy_deployment"},
        "rollback_operational_envelope.rollback_kind",
    )
    release = payload["rollback_release_ref"]
    if kind == "approved_release":
        reference = validate_object_ref(release, "rollback_operational_envelope.rollback_release_ref")
        if reference["object_kind"] != "release_artifact":
            raise ControlError("approved_release 回滚必须引用 ReleaseArtifact")
    elif release is not None:
        raise ControlError("legacy_deployment 不得伪造 rollback ReleaseArtifact")
    _validate_operational_bindings(
        payload["operational_bindings"], "rollback_operational_envelope.operational_bindings"
    )
    _validate_capabilities(
        payload["capabilities"], "rollback_operational_envelope.capabilities"
    )
    _expect_enum(
        payload["wire_evidence_scope"],
        {"strict_approved", "diagnostic_only"},
        "rollback_operational_envelope.wire_evidence_scope",
    )
    if kind == "legacy_deployment" and payload["wire_evidence_scope"] != "diagnostic_only":
        raise ControlError("legacy deployment 回滚只能是 diagnostic_only")
    return payload


def _validate_deployment_traffic_envelope(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "deployment_traffic_envelope")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "active_support_envelope_ref",
            "rollback_operational_envelope_ref",
            "production_ingress_inventory_ref",
            "capabilities",
        },
        "deployment_traffic_envelope",
    )
    if payload["schema_version"] != "official-client-deployment-traffic-envelope/v1":
        raise ControlError("deployment_traffic_envelope.schema_version 不匹配")
    validate_persona(payload["persona"])
    expected_kinds = {
        "active_support_envelope_ref": "active_support_envelope",
        "rollback_operational_envelope_ref": "rollback_operational_envelope",
        "production_ingress_inventory_ref": "production_ingress_inventory",
    }
    for key, expected_kind in expected_kinds.items():
        reference = validate_object_ref(payload[key], f"deployment_traffic_envelope.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"deployment_traffic_envelope.{key} 类型错误")
    _validate_capabilities(
        payload["capabilities"], "deployment_traffic_envelope.capabilities"
    )
    return payload


def _validate_generic_manifest(value: Any, kind: str) -> dict[str, Any]:
    payload = expect_object(value, kind)
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "manifest_id", "entries"},
        kind,
    )
    expected_schema = f"official-client-{kind.replace('_', '-')}/v1"
    if payload["schema_version"] != expected_schema:
        raise ControlError(f"{kind}.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_safe_id(payload["manifest_id"], f"{kind}.manifest_id")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries:
        raise ControlError(f"{kind}.entries 必须是非空数组")
    entry_ids: list[str] = []
    for index, raw in enumerate(entries):
        label = f"{kind}.entries[{index}]"
        entry = expect_object(raw, label)
        expect_exact_keys(entry, {"id", "facts"}, label)
        entry_ids.append(expect_safe_id(entry["id"], f"{label}.id"))
        expect_object(entry["facts"], f"{label}.facts")
    if entry_ids != sorted(set(entry_ids)):
        raise ControlError(f"{kind}.entries 必须按 id 排序且不得重复")
    return payload


def _validate_runtime_catalog_snapshot(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "runtime_catalog_snapshot")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "catalog_digest",
            "production_active_ref",
            "production_rollback_ref",
            "observed_at_utc",
            "source_ref",
        },
        "runtime_catalog_snapshot",
    )
    if payload["schema_version"] != "official-client-runtime-catalog-snapshot/v1":
        raise ControlError("runtime_catalog_snapshot.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_sha256(payload["catalog_digest"], "runtime_catalog_snapshot.catalog_digest")
    for key in ("production_active_ref", "production_rollback_ref"):
        reference = payload[key]
        if reference is not None:
            parsed = validate_object_ref(reference, f"runtime_catalog_snapshot.{key}")
            if parsed["object_kind"] != "release_artifact":
                raise ControlError(f"runtime_catalog_snapshot.{key} 必须引用 ReleaseArtifact")
    expect_rfc3339(payload["observed_at_utc"], "runtime_catalog_snapshot.observed_at_utc")
    validate_external_binding(payload["source_ref"], "runtime_catalog_snapshot.source_ref")
    return payload


def _validate_deployment_plan(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "deployment_plan")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "persona",
            "manifest_id",
            "active_support_capabilities",
            "rollback_operational_capabilities",
            "deployment_traffic_capabilities",
            "rollback_target_kind",
            "failure_policy",
        },
        "deployment_plan",
    )
    if payload["schema_version"] != "official-client-deployment-plan/v1":
        raise ControlError("deployment_plan.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_safe_id(payload["manifest_id"], "deployment_plan.manifest_id")
    active = _validate_capabilities(
        payload["active_support_capabilities"],
        "deployment_plan.active_support_capabilities",
    )
    rollback = _validate_capabilities(
        payload["rollback_operational_capabilities"],
        "deployment_plan.rollback_operational_capabilities",
    )
    traffic = _validate_capabilities(
        payload["deployment_traffic_capabilities"],
        "deployment_plan.deployment_traffic_capabilities",
    )
    active_keys = {capability_key(item) for item in active}
    rollback_keys = {capability_key(item) for item in rollback}
    traffic_keys = {capability_key(item) for item in traffic}
    if not traffic_keys.issubset(active_keys & rollback_keys):
        raise ControlError("DeploymentPlan 的流量范围不属于 Active 与 Rollback 计划交集")
    _expect_enum(
        payload["rollback_target_kind"],
        {"approved_release", "legacy_deployment"},
        "deployment_plan.rollback_target_kind",
    )
    _expect_enum(
        payload["failure_policy"],
        {"persona_fail_close", "process_stop"},
        "deployment_plan.failure_policy",
    )
    return payload


def _validate_scenario_plan(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "scenario_plan")
    expect_exact_keys(
        payload,
        {"schema_version", "persona", "manifest_id", "scenarios"},
        "scenario_plan",
    )
    if payload["schema_version"] != "official-client-scenario-plan/v1":
        raise ControlError("scenario_plan.schema_version 不匹配")
    validate_persona(payload["persona"])
    expect_safe_id(payload["manifest_id"], "scenario_plan.manifest_id")
    scenarios = payload["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        raise ControlError("scenario_plan.scenarios 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(scenarios):
        label = f"scenario_plan.scenarios[{index}]"
        scenario = expect_object(raw, label)
        expect_exact_keys(
            scenario,
            {"id", "spec_ids", "ingress_protocol_classes", "conditions", "assertion_ids"},
            label,
        )
        identities.append(expect_safe_id(scenario["id"], f"{label}.id"))
        spec_ids = expect_string_list(scenario["spec_ids"], f"{label}.spec_ids")
        for spec_id in spec_ids:
            if not SPEC_ID_RE.fullmatch(spec_id):
                raise ControlError(f"{label}.spec_ids 包含非法 SPEC")
        expect_string_list(
            scenario["ingress_protocol_classes"],
            f"{label}.ingress_protocol_classes",
        )
        expect_string_list(scenario["conditions"], f"{label}.conditions")
        expect_string_list(scenario["assertion_ids"], f"{label}.assertion_ids")
    if identities != sorted(set(identities)):
        raise ControlError("scenario_plan.scenarios 必须按 id 排序且不得重复")
    return payload


def validate_object_document(value: Any) -> dict[str, Any]:
    document = expect_object(value, "object")
    expect_exact_keys(document, {"schema_version", "object_kind", "payload"}, "object")
    if document["schema_version"] != OBJECT_SCHEMA:
        raise ControlError("object.schema_version 不匹配")
    kind = expect_string(document["object_kind"], "object.object_kind")
    if kind not in OBJECT_KINDS:
        raise ControlError(f"object.object_kind 未登记：{kind}")
    validators: dict[str, Callable[[Any], dict[str, Any]]] = {
        "bootstrap": validate_bootstrap_payload,
        "persona_descriptor": _validate_persona_descriptor,
        "evidence_package": _validate_evidence_package,
        "rule_migration_ledger": _validate_rule_migration_ledger,
        "atomic_assertion_ledger": _validate_atomic_assertion_ledger,
        "validation_execution_plan": _validate_validation_execution_plan,
        "candidate_evidence_package": _validate_candidate_evidence_package,
        "candidate_delivery_plan": _validate_candidate_delivery_plan,
        "candidate_delivery_package": _validate_candidate_delivery_package,
        "private_archive_manifest": _validate_private_archive_manifest,
        "profile_schema": _validate_profile_schema,
        "snapshot": _validate_snapshot,
        "release_bundle": _validate_release_bundle,
        "release_artifact": _validate_release_artifact,
        "ingress_observation": _validate_ingress_observation,
        "egress_observation": _validate_egress_observation,
        "production_ingress_inventory": _validate_production_ingress_inventory,
        "egress_disposition_inventory": _validate_egress_disposition_inventory,
        "support_envelope": _validate_support_envelope,
        "active_support_envelope": _validate_active_support_envelope,
        "rollback_operational_envelope": _validate_rollback_operational_envelope,
        "deployment_traffic_envelope": _validate_deployment_traffic_envelope,
        "runtime_catalog_snapshot": _validate_runtime_catalog_snapshot,
        "deployment_plan": _validate_deployment_plan,
        "scenario_plan": _validate_scenario_plan,
    }
    if kind in validators:
        validators[kind](document["payload"])
    else:
        _validate_generic_manifest(document["payload"], kind)
    return document


def _validate_target_dispositions(
    value: Any,
    label: str,
    *,
    allowed: set[str],
    identity_key: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError(f"{label} 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(value):
        item_label = f"{label}[{index}]"
        item = expect_object(raw, item_label)
        expect_exact_keys(item, {identity_key, "target_disposition", "evidence_refs"}, item_label)
        identities.append(expect_safe_id(item[identity_key], f"{item_label}.{identity_key}"))
        _expect_enum(item["target_disposition"], allowed, f"{item_label}.target_disposition")
        _validate_ref_list(item["evidence_refs"], f"{item_label}.evidence_refs", validate_object_ref)
    if identities != sorted(set(identities)):
        raise ControlError(f"{label} 必须按 {identity_key} 排序且不得重复")
    return value


def _validate_egress_target_dispositions(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError(f"{label} 必须是非空数组")
    identities: list[str] = []
    for index, raw in enumerate(value):
        item_label = f"{label}[{index}]"
        item = expect_object(raw, item_label)
        expect_exact_keys(
            item,
            {
                "egress_id",
                "target_disposition",
                "spec_ids",
                "managed_policy",
                "evidence_refs",
            },
            item_label,
        )
        identities.append(expect_safe_id(item["egress_id"], f"{item_label}.egress_id"))
        disposition = _expect_enum(
            item["target_disposition"],
            EGRESS_DISPOSITIONS,
            f"{item_label}.target_disposition",
        )
        spec_ids = expect_string_list(
            item["spec_ids"], f"{item_label}.spec_ids", non_empty=False
        )
        for spec_id in spec_ids:
            if not SPEC_ID_RE.fullmatch(spec_id):
                raise ControlError(f"{item_label}.spec_ids 包含非法 SPEC")
        if disposition == "persona_strict" and not spec_ids:
            raise ControlError(f"{item_label} persona_strict 目标必须绑定 SPEC")
        if disposition != "persona_strict" and spec_ids:
            raise ControlError(f"{item_label} 非 strict 目标不得绑定 SPEC")
        policy = item["managed_policy"]
        if disposition == "non_persona_managed":
            _validate_managed_policy(policy, f"{item_label}.managed_policy")
        elif policy is not None:
            raise ControlError(f"{item_label}.managed_policy 只允许 managed 目标使用")
        _validate_ref_list(
            item["evidence_refs"], f"{item_label}.evidence_refs", validate_object_ref
        )
    if identities != sorted(set(identities)):
        raise ControlError(f"{label} 必须按 egress_id 排序且不得重复")
    return value


def _validate_discovery_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "discovery_recorded")
    expect_exact_keys(
        payload,
        {"version", "source", "discovered_at_utc", "tool_sha256", "artifact_refs"},
        "discovery_recorded",
    )
    expect_string(payload["version"], "discovery_recorded.version")
    expect_string(payload["source"], "discovery_recorded.source")
    expect_rfc3339(payload["discovered_at_utc"], "discovery_recorded.discovered_at_utc")
    expect_sha256(payload["tool_sha256"], "discovery_recorded.tool_sha256")
    bindings = payload["artifact_refs"]
    if not isinstance(bindings, list) or not bindings:
        raise ControlError("discovery_recorded.artifact_refs 必须是非空数组")
    for index, item in enumerate(bindings):
        validate_external_binding(item, f"discovery_recorded.artifact_refs[{index}]")
    return payload


def _validate_evidence_recorded_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "evidence_recorded")
    expect_exact_keys(
        payload,
        {"discovery_fact_ref", "evidence_package_ref"},
        "evidence_recorded",
    )
    validate_fact_ref(payload["discovery_fact_ref"], "evidence_recorded.discovery_fact_ref")
    reference = validate_object_ref(payload["evidence_package_ref"], "evidence_recorded.evidence_package_ref")
    if reference["object_kind"] != "evidence_package":
        raise ControlError("evidence_recorded 必须引用 EvidencePackage")
    return payload


def _validate_rule_classification_recorded_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "rule_classification_recorded")
    expect_exact_keys(
        payload,
        {
            "evidence_fact_ref",
            "evidence_package_ref",
            "rule_migration_ledger_ref",
            "atomic_assertion_ledger_ref",
        },
        "rule_classification_recorded",
    )
    validate_fact_ref(
        payload["evidence_fact_ref"],
        "rule_classification_recorded.evidence_fact_ref",
    )
    expected_kinds = {
        "evidence_package_ref": "evidence_package",
        "rule_migration_ledger_ref": "rule_migration_ledger",
        "atomic_assertion_ledger_ref": "atomic_assertion_ledger",
    }
    for key, expected_kind in expected_kinds.items():
        reference = validate_object_ref(
            payload[key], f"rule_classification_recorded.{key}"
        )
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"rule_classification_recorded.{key} 类型错误")
    return payload


def _validate_evidence_approved_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "evidence_approved")
    expect_exact_keys(
        payload,
        {"evidence_fact_ref", "evidence_package_ref", "reviewer", "review_ref"},
        "evidence_approved",
        optional={"classification_fact_ref"},
    )
    validate_fact_ref(payload["evidence_fact_ref"], "evidence_approved.evidence_fact_ref")
    if "classification_fact_ref" in payload:
        validate_fact_ref(
            payload["classification_fact_ref"],
            "evidence_approved.classification_fact_ref",
        )
    package = validate_object_ref(
        payload["evidence_package_ref"], "evidence_approved.evidence_package_ref"
    )
    if package["object_kind"] != "evidence_package":
        raise ControlError("evidence_approved 必须引用 EvidencePackage")
    expect_string(payload["reviewer"], "evidence_approved.reviewer")
    expect_string(payload["review_ref"], "evidence_approved.review_ref")
    return payload


def _validate_profile_approved_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "profile_approved")
    expect_exact_keys(
        payload,
        {
            "evidence_approval_ref",
            "approval_purpose",
            "persona_descriptor_ref",
            "profile_schema_ref",
            "snapshot_ref",
            "release_artifact_ref",
            "support_envelope_ref",
            "production_ingress_inventory_ref",
            "egress_disposition_inventory_ref",
            "persona_derivation_ref",
            "compatibility_boundary_ref",
            "scenario_plan_ref",
            "deployment_plan_ref",
            "target_spec_ids",
            "ingress_target_dispositions",
            "egress_target_dispositions",
            "reviewer",
            "review_ref",
            "identity_sha256",
        },
        "profile_approved",
    )
    validate_fact_ref(payload["evidence_approval_ref"], "profile_approved.evidence_approval_ref")
    _expect_enum(
        payload["approval_purpose"],
        APPROVAL_PURPOSES,
        "profile_approved.approval_purpose",
    )
    expected_kinds = {
        "persona_descriptor_ref": "persona_descriptor",
        "profile_schema_ref": "profile_schema",
        "snapshot_ref": "snapshot",
        "release_artifact_ref": "release_artifact",
        "support_envelope_ref": "support_envelope",
        "production_ingress_inventory_ref": "production_ingress_inventory",
        "egress_disposition_inventory_ref": "egress_disposition_inventory",
        "persona_derivation_ref": "persona_derivation",
        "compatibility_boundary_ref": "compatibility_boundary",
        "scenario_plan_ref": "scenario_plan",
        "deployment_plan_ref": "deployment_plan",
    }
    for key, expected_kind in expected_kinds.items():
        reference = validate_object_ref(payload[key], f"profile_approved.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"profile_approved.{key} 类型错误")
    spec_ids = expect_string_list(payload["target_spec_ids"], "profile_approved.target_spec_ids")
    for spec_id in spec_ids:
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise ControlError("profile_approved.target_spec_ids 包含非法 SPEC")
    _validate_target_dispositions(
        payload["ingress_target_dispositions"],
        "profile_approved.ingress_target_dispositions",
        allowed=INGRESS_DISPOSITIONS,
        identity_key="logical_ingress_id",
    )
    _validate_egress_target_dispositions(
        payload["egress_target_dispositions"],
        "profile_approved.egress_target_dispositions",
    )
    expect_string(payload["reviewer"], "profile_approved.reviewer")
    expect_string(payload["review_ref"], "profile_approved.review_ref")
    expected_identity = profile_approval_identity_sha256(payload)
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("profile_approved.identity_sha256 与批准内容不一致")
    return payload


def profile_approval_identity_sha256(payload: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in payload.items() if key != "identity_sha256"})


def _assert_candidate_has_no_selector(value: Any) -> None:
    forbidden = {
        "selector",
        "runtime_selector",
        "production_active",
        "production_rollback",
        "previous",
        "active",
        "rollback",
    }
    if isinstance(value, dict):
        overlap = sorted(forbidden & {str(key).lower() for key in value})
        if overlap:
            raise ControlError(f"ValidationCandidate 不得借用 production selector：{overlap}")
        for item in value.values():
            _assert_candidate_has_no_selector(item)
    elif isinstance(value, list):
        for item in value:
            _assert_candidate_has_no_selector(item)


def _validate_candidate_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "candidate_frozen")
    schema_version = payload.get("schema_version")
    required = {
        "candidate_id",
        "profile_approval_ref",
        "release_artifact_ref",
        "support_envelope_ref",
        "source_tree_sha256",
        "test_tree_sha256",
        "dependency_lock_sha256",
        "target_architecture",
        "build_id",
        "image_digest",
        "candidate_purpose",
        "identity_sha256",
    }
    if schema_version is not None:
        if schema_version != VALIDATION_CANDIDATE_V2_SCHEMA:
            raise ControlError("candidate_frozen.schema_version 不匹配")
        required |= {"schema_version", "candidate_build_receipt_ref"}
    expect_exact_keys(
        payload,
        required,
        "candidate_frozen",
    )
    _assert_candidate_has_no_selector(payload)
    expect_safe_id(payload["candidate_id"], "candidate_frozen.candidate_id")
    validate_fact_ref(payload["profile_approval_ref"], "candidate_frozen.profile_approval_ref")
    release = validate_object_ref(
        payload["release_artifact_ref"], "candidate_frozen.release_artifact_ref"
    )
    support = validate_object_ref(
        payload["support_envelope_ref"], "candidate_frozen.support_envelope_ref"
    )
    if release["object_kind"] != "release_artifact" or support["object_kind"] != "support_envelope":
        raise ControlError("ValidationCandidate 的 Release／SupportEnvelope 引用类型错误")
    for key in ("source_tree_sha256", "test_tree_sha256", "dependency_lock_sha256"):
        expect_sha256(payload[key], f"candidate_frozen.{key}")
    expect_string(payload["target_architecture"], "candidate_frozen.target_architecture")
    expect_safe_id(payload["build_id"], "candidate_frozen.build_id")
    expect_image_digest(payload["image_digest"], "candidate_frozen.image_digest")
    _expect_enum(
        payload["candidate_purpose"], APPROVAL_PURPOSES, "candidate_frozen.candidate_purpose"
    )
    if schema_version is not None:
        build_ref = validate_receipt_ref(
            payload["candidate_build_receipt_ref"],
            "candidate_frozen.candidate_build_receipt_ref",
        )
        if build_ref["receipt_kind"] != "candidate_build":
            raise ControlError("candidate_frozen 必须引用 CandidateBuildReceipt")
    if payload["identity_sha256"] != candidate_identity_sha256(payload):
        raise ControlError("candidate_frozen.identity_sha256 与 candidate 身份不一致")
    return payload


def candidate_identity_sha256(payload: dict[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in payload.items() if key != "identity_sha256"})


def _validate_scenario_payload(value: Any, stage: str) -> dict[str, Any]:
    payload = expect_object(value, f"scenario_{stage}")
    schema_version = payload.get("schema_version")
    required = {
        "candidate_id",
        "scenario_id",
        "attempt_id",
        "stage",
        "previous_stage_ref",
        "artifact_refs",
        "result",
    }
    if schema_version is not None:
        if schema_version != SCENARIO_STAGE_V2_SCHEMA:
            raise ControlError(f"scenario_{stage}.schema_version 不匹配")
        required |= {"schema_version", "validation_attempt_ref"}
    if stage == "approve":
        required |= {"reviewer", "review_ref"}
    expect_exact_keys(payload, required, f"scenario_{stage}")
    expect_safe_id(payload["candidate_id"], f"scenario_{stage}.candidate_id")
    expect_safe_id(payload["scenario_id"], f"scenario_{stage}.scenario_id")
    expect_safe_id(payload["attempt_id"], f"scenario_{stage}.attempt_id")
    if schema_version is not None:
        validate_fact_ref(
            payload["validation_attempt_ref"],
            f"scenario_{stage}.validation_attempt_ref",
        )
    if payload["stage"] != stage:
        raise ControlError(f"scenario_{stage}.stage 不匹配")
    previous = payload["previous_stage_ref"]
    if stage == "prepare":
        if previous is not None:
            raise ControlError("scenario prepare 不得存在前序阶段")
    else:
        validate_fact_ref(previous, f"scenario_{stage}.previous_stage_ref")
    _validate_ref_list(
        payload["artifact_refs"],
        f"scenario_{stage}.artifact_refs",
        validate_object_ref,
        non_empty=stage != "prepare",
    )
    if stage == "prepare":
        allowed_results = {"prepared"}
    elif stage in {"capture", "seal"}:
        allowed_results = {"pass", "failed"}
    else:
        allowed_results = {"pass"}
    if payload["result"] not in allowed_results:
        raise ControlError(
            f"scenario_{stage}.result 必须属于 {sorted(allowed_results)}"
        )
    if stage == "approve":
        expect_string(payload["reviewer"], "scenario_approve.reviewer")
        expect_string(payload["review_ref"], "scenario_approve.review_ref")
    return payload


def _validate_pair_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "pair_recorded")
    schema_version = payload.get("schema_version")
    required = {
        "pair_id",
        "spec_id",
        "candidate_id",
        "release_artifact_ref",
        "condition_sha256",
        "scenario_approval_refs",
        "official_result",
        "third_party_results",
        "dynamic_field_checks",
    }
    if schema_version is not None:
        if schema_version != PAIR_V2_SCHEMA:
            raise ControlError("pair_recorded.schema_version 不匹配")
        required |= {
            "schema_version",
            "profile_approval_ref",
            "candidate_evidence_package_ref",
            "atomic_assertion_ledger_ref",
            "candidate_result",
            "assertion_results",
            "implementation_anchors",
            "test_anchors",
            "result_source",
            "comparison_mode",
        }
    expect_exact_keys(
        payload,
        required,
        "pair_recorded",
    )
    pair_id = expect_string(payload["pair_id"], "pair_recorded.pair_id")
    match = PAIR_ID_RE.fullmatch(pair_id)
    spec_id = expect_string(payload["spec_id"], "pair_recorded.spec_id")
    if match is None or match.group(1) != spec_id:
        raise ControlError("PAIR ID 必须精确等于 PAIR-<SPEC-ID>")
    expect_safe_id(payload["candidate_id"], "pair_recorded.candidate_id")
    release = validate_object_ref(
        payload["release_artifact_ref"], "pair_recorded.release_artifact_ref"
    )
    if release["object_kind"] != "release_artifact":
        raise ControlError("pair_recorded.release_artifact_ref 类型错误")
    expect_sha256(payload["condition_sha256"], "pair_recorded.condition_sha256")
    _validate_ref_list(
        payload["scenario_approval_refs"],
        "pair_recorded.scenario_approval_refs",
        validate_fact_ref,
    )
    official = expect_object(payload["official_result"], "pair_recorded.official_result")
    official_keys = {"ingress_id", "translation", "result", "final_wire_sha256"}
    if schema_version is not None:
        official_keys.add("evidence_refs")
    expect_exact_keys(
        official,
        official_keys,
        "pair_recorded.official_result",
    )
    expect_safe_id(official["ingress_id"], "pair_recorded.official_result.ingress_id")
    if official["translation"] != "lossless" or official["result"] != "pass":
        raise ControlError("官方入口必须 lossless 且断言通过")
    expect_sha256(
        official["final_wire_sha256"], "pair_recorded.official_result.final_wire_sha256"
    )
    if schema_version is not None:
        _validate_ref_list(
            official["evidence_refs"],
            "pair_recorded.official_result.evidence_refs",
            validate_object_ref,
        )
    third_party = payload["third_party_results"]
    if not isinstance(third_party, list):
        raise ControlError("pair_recorded.third_party_results 必须是数组")
    protocols: list[str] = []
    for index, raw in enumerate(third_party):
        label = f"pair_recorded.third_party_results[{index}]"
        item = expect_object(raw, label)
        item_keys = {
            "protocol_class",
            "ingress_id",
            "translation",
            "result",
            "final_wire_sha256",
        }
        if schema_version is not None:
            item_keys.add("evidence_refs")
        expect_exact_keys(
            item,
            item_keys,
            label,
        )
        protocols.append(expect_safe_id(item["protocol_class"], f"{label}.protocol_class"))
        expect_safe_id(item["ingress_id"], f"{label}.ingress_id")
        if item["translation"] != "lossless" or item["result"] != "pass":
            raise ControlError(f"{label} 不能进入 strict PAIR 分母")
        expect_sha256(item["final_wire_sha256"], f"{label}.final_wire_sha256")
        if schema_version is not None:
            _validate_ref_list(
                item["evidence_refs"], f"{label}.evidence_refs", validate_object_ref
            )
    if protocols != sorted(set(protocols)):
        raise ControlError("pair_recorded.third_party_results 必须按协议类别排序且不得重复")
    checks = payload["dynamic_field_checks"]
    if not isinstance(checks, list) or not checks:
        raise ControlError("pair_recorded.dynamic_field_checks 必须是非空数组")
    check_ids: list[str] = []
    for index, raw in enumerate(checks):
        label = f"pair_recorded.dynamic_field_checks[{index}]"
        item = expect_object(raw, label)
        expect_exact_keys(item, {"id", "dimensions", "result"}, label)
        check_ids.append(expect_safe_id(item["id"], f"{label}.id"))
        dimensions = expect_string_list(item["dimensions"], f"{label}.dimensions")
        if not {"source", "format", "relation", "lifecycle"}.issubset(dimensions):
            raise ControlError(f"{label} 未覆盖动态字段四个比较维度")
        if item["result"] != "pass":
            raise ControlError(f"{label} 未通过")
    if check_ids != sorted(set(check_ids)):
        raise ControlError("pair_recorded.dynamic_field_checks 必须按 id 排序且不得重复")
    if schema_version is not None:
        validate_fact_ref(
            payload["profile_approval_ref"], "pair_recorded.profile_approval_ref"
        )
        evidence_ref = validate_object_ref(
            payload["candidate_evidence_package_ref"],
            "pair_recorded.candidate_evidence_package_ref",
        )
        if evidence_ref["object_kind"] != "candidate_evidence_package":
            raise ControlError("pair_recorded 必须引用 CandidateEvidencePackage")
        ledger_ref = validate_object_ref(
            payload["atomic_assertion_ledger_ref"],
            "pair_recorded.atomic_assertion_ledger_ref",
        )
        if ledger_ref["object_kind"] != "atomic_assertion_ledger":
            raise ControlError("pair_recorded 必须引用 AtomicAssertionLedger")
        candidate_result = expect_object(
            payload["candidate_result"], "pair_recorded.candidate_result"
        )
        expect_exact_keys(
            candidate_result,
            {"result", "final_wire_sha256", "evidence_refs"},
            "pair_recorded.candidate_result",
        )
        if candidate_result["result"] != "pass":
            raise ControlError("pair_recorded.candidate_result 必须是 pass")
        expect_sha256(
            candidate_result["final_wire_sha256"],
            "pair_recorded.candidate_result.final_wire_sha256",
        )
        _validate_ref_list(
            candidate_result["evidence_refs"],
            "pair_recorded.candidate_result.evidence_refs",
            validate_object_ref,
        )
        assertion_results = payload["assertion_results"]
        if not isinstance(assertion_results, list) or not assertion_results:
            raise ControlError("pair_recorded.assertion_results 必须是非空数组")
        assertion_ids: list[str] = []
        for index, raw in enumerate(assertion_results):
            label = f"pair_recorded.assertion_results[{index}]"
            result = expect_object(raw, label)
            expect_exact_keys(
                result,
                {"assertion_id", "owner_kind", "owner_id", "result", "source"},
                label,
            )
            assertion_ids.append(
                expect_safe_id(result["assertion_id"], f"{label}.assertion_id")
            )
            if result["owner_kind"] != "required_rule":
                raise ControlError(f"{label}.owner_kind 必须是 required_rule")
            if result["owner_id"] != spec_id:
                raise ControlError(f"{label}.owner_id 必须等于本 PAIR 的 SPEC ID")
            if result["result"] != "pass":
                raise ControlError(f"{label}.result 必须是 pass")
            _expect_enum(result["source"], {"execute", "reuse"}, f"{label}.source")
        if assertion_ids != sorted(set(assertion_ids)):
            raise ControlError("pair_recorded.assertion_results 必须按 assertion_id 排序且不得重复")
        expect_string_list(
            payload["implementation_anchors"], "pair_recorded.implementation_anchors"
        )
        expect_string_list(payload["test_anchors"], "pair_recorded.test_anchors")
        _expect_enum(
            payload["result_source"], {"execute", "reuse"}, "pair_recorded.result_source"
        )
        _expect_enum(
            payload["comparison_mode"],
            {"dual_wire", "candidate_profile"},
            "pair_recorded.comparison_mode",
        )
    return payload


def _validate_acceptance_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "acceptance_recorded")
    schema_version = payload.get("schema_version")
    required = {
        "candidate_id",
        "profile_approval_ref",
        "candidate_ref",
        "pair_refs",
        "boundary_assertion_refs",
        "inventory_assertion_refs",
        "acceptance_purpose",
        "result",
    }
    if schema_version is not None:
        if schema_version != ACCEPTANCE_V2_SCHEMA:
            raise ControlError("acceptance_recorded.schema_version 不匹配")
        required |= {
            "schema_version",
            "candidate_build_receipt_ref",
            "validation_attempt_ref",
            "candidate_evidence_package_ref",
            "atomic_assertion_ledger_ref",
            "external_gate_receipt_refs",
            "identity_sha256",
        }
    expect_exact_keys(
        payload,
        required,
        "acceptance_recorded",
    )
    expect_safe_id(payload["candidate_id"], "acceptance_recorded.candidate_id")
    validate_fact_ref(payload["profile_approval_ref"], "acceptance_recorded.profile_approval_ref")
    validate_fact_ref(payload["candidate_ref"], "acceptance_recorded.candidate_ref")
    _validate_ref_list(payload["pair_refs"], "acceptance_recorded.pair_refs", validate_fact_ref)
    _validate_ref_list(
        payload["boundary_assertion_refs"],
        "acceptance_recorded.boundary_assertion_refs",
        validate_object_ref,
    )
    _validate_ref_list(
        payload["inventory_assertion_refs"],
        "acceptance_recorded.inventory_assertion_refs",
        validate_object_ref,
    )
    purpose = _expect_enum(
        payload["acceptance_purpose"],
        APPROVAL_PURPOSES,
        "acceptance_recorded.acceptance_purpose",
    )
    expected_result = "accepted" if purpose == "production_replacement" else "validation_only"
    if payload["result"] != expected_result:
        raise ControlError(f"acceptance_recorded.result 必须是 {expected_result}")
    if schema_version is not None:
        build_ref = validate_receipt_ref(
            payload["candidate_build_receipt_ref"],
            "acceptance_recorded.candidate_build_receipt_ref",
        )
        if build_ref["receipt_kind"] != "candidate_build":
            raise ControlError("acceptance_recorded 必须引用 CandidateBuildReceipt")
        validate_fact_ref(
            payload["validation_attempt_ref"],
            "acceptance_recorded.validation_attempt_ref",
        )
        expected_objects = {
            "candidate_evidence_package_ref": "candidate_evidence_package",
            "atomic_assertion_ledger_ref": "atomic_assertion_ledger",
        }
        for key, expected_kind in expected_objects.items():
            reference = validate_object_ref(payload[key], f"acceptance_recorded.{key}")
            if reference["object_kind"] != expected_kind:
                raise ControlError(f"acceptance_recorded.{key} 类型错误")
        gate_refs = _validate_ref_list(
            payload["external_gate_receipt_refs"],
            "acceptance_recorded.external_gate_receipt_refs",
            validate_receipt_ref,
        )
        if any(reference["receipt_kind"] != "validation_gate" for reference in gate_refs):
            raise ControlError("acceptance_recorded 外部门禁必须引用 validation_gate 收据")
        expected_identity = canonical_sha256(
            {key: item for key, item in payload.items() if key != "identity_sha256"}
        )
        if payload["identity_sha256"] != expected_identity:
            raise ControlError("acceptance_recorded.identity_sha256 与验收内容不一致")
    return payload


def _validate_validation_attempt_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "validation_attempt_created")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "attempt_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_execution_plan_ref",
            "run_conditions",
            "production_selector_before_ref",
            "vircs_state",
            "result",
            "identity_sha256",
        },
        "validation_attempt_created",
    )
    if payload["schema_version"] != VALIDATION_ATTEMPT_SCHEMA:
        raise ControlError("validation_attempt_created.schema_version 不匹配")
    for key in ("attempt_id", "candidate_id"):
        expect_safe_id(payload[key], f"validation_attempt_created.{key}")
    validate_fact_ref(payload["candidate_ref"], "validation_attempt_created.candidate_ref")
    receipt_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "validation_attempt_created.candidate_build_receipt_ref",
    )
    if receipt_ref["receipt_kind"] != "candidate_build":
        raise ControlError("validation_attempt_created 必须引用 CandidateBuildReceipt")
    plan_ref = validate_object_ref(
        payload["validation_execution_plan_ref"],
        "validation_attempt_created.validation_execution_plan_ref",
    )
    if plan_ref["object_kind"] != "validation_execution_plan":
        raise ControlError("validation_attempt_created 必须引用 ValidationExecutionPlan")
    _validate_run_conditions(payload["run_conditions"], "validation_attempt_created.run_conditions")
    validate_fact_ref(
        payload["production_selector_before_ref"],
        "validation_attempt_created.production_selector_before_ref",
    )
    _validate_vircs_state(payload["vircs_state"], "validation_attempt_created.vircs_state")
    if payload["result"] != "created":
        raise ControlError("validation_attempt_created.result 必须是 created")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("validation_attempt_created.identity_sha256 与 attempt 内容不一致")
    return payload


def _validate_validation_completed_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "validation_completed")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "attempt_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_attempt_ref",
            "validation_execution_plan_ref",
            "candidate_evidence_package_ref",
            "acceptance_ref",
            "pair_refs",
            "external_gate_receipt_refs",
            "production_selector_after_ref",
            "vircs_state",
            "result",
            "identity_sha256",
        },
        "validation_completed",
    )
    if payload["schema_version"] != VALIDATION_COMPLETED_SCHEMA:
        raise ControlError("validation_completed.schema_version 不匹配")
    for key in ("attempt_id", "candidate_id"):
        expect_safe_id(payload[key], f"validation_completed.{key}")
    for key in (
        "candidate_ref",
        "validation_attempt_ref",
        "acceptance_ref",
        "production_selector_after_ref",
    ):
        validate_fact_ref(payload[key], f"validation_completed.{key}")
    receipt_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "validation_completed.candidate_build_receipt_ref",
    )
    if receipt_ref["receipt_kind"] != "candidate_build":
        raise ControlError("validation_completed 必须引用 CandidateBuildReceipt")
    expected_objects = {
        "validation_execution_plan_ref": "validation_execution_plan",
        "candidate_evidence_package_ref": "candidate_evidence_package",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(payload[key], f"validation_completed.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"validation_completed.{key} 类型错误")
    _validate_ref_list(payload["pair_refs"], "validation_completed.pair_refs", validate_fact_ref)
    gate_refs = _validate_ref_list(
        payload["external_gate_receipt_refs"],
        "validation_completed.external_gate_receipt_refs",
        validate_receipt_ref,
    )
    if any(reference["receipt_kind"] != "validation_gate" for reference in gate_refs):
        raise ControlError("validation_completed 外部门禁必须引用 validation_gate 收据")
    _validate_vircs_state(payload["vircs_state"], "validation_completed.vircs_state")
    if payload["result"] != "passed":
        raise ControlError("validation_completed.result 必须是 passed")
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("validation_completed.identity_sha256 与完成内容不一致")
    return payload


def _validate_candidate_delivery_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "candidate_delivery_recorded")
    expect_exact_keys(
        payload,
        {
            "schema_version",
            "delivery_id",
            "stage",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "delivery_plan_ref",
            "previous_stage_ref",
            "host_id",
            "architecture",
            "compose_sha256",
            "environment_sha256",
            "configuration_sha256",
            "network_sha256",
            "runtime_profile_sha256",
            "image_digest",
            "check_ids",
            "evidence_refs",
            "observed_seconds",
            "result",
            "identity_sha256",
        },
        "candidate_delivery_recorded",
    )
    if payload["schema_version"] != CANDIDATE_DELIVERY_FACT_SCHEMA:
        raise ControlError("candidate_delivery_recorded.schema_version 不匹配")
    for key in ("delivery_id", "candidate_id", "host_id"):
        expect_safe_id(payload[key], f"candidate_delivery_recorded.{key}")
    stage = _expect_enum(
        payload["stage"],
        set(CANDIDATE_DELIVERY_STAGES),
        "candidate_delivery_recorded.stage",
    )
    for key in ("candidate_ref", "validation_completed_ref", "acceptance_ref"):
        validate_fact_ref(payload[key], f"candidate_delivery_recorded.{key}")
    build_ref = validate_receipt_ref(
        payload["candidate_build_receipt_ref"],
        "candidate_delivery_recorded.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("candidate_delivery_recorded 必须引用 CandidateBuildReceipt")
    plan_ref = validate_object_ref(
        payload["delivery_plan_ref"], "candidate_delivery_recorded.delivery_plan_ref"
    )
    if plan_ref["object_kind"] != "candidate_delivery_plan":
        raise ControlError("candidate_delivery_recorded 必须引用 CandidateDeliveryPlan")
    if stage == "candidate_active":
        if payload["previous_stage_ref"] is not None:
            raise ControlError("candidate_active 不得声明前序候选交付事实")
    else:
        validate_fact_ref(
            payload["previous_stage_ref"],
            "candidate_delivery_recorded.previous_stage_ref",
        )
    expect_string(payload["architecture"], "candidate_delivery_recorded.architecture")
    for key in (
        "compose_sha256",
        "environment_sha256",
        "configuration_sha256",
        "network_sha256",
        "runtime_profile_sha256",
    ):
        expect_sha256(payload[key], f"candidate_delivery_recorded.{key}")
    expect_image_digest(payload["image_digest"], "candidate_delivery_recorded.image_digest")
    expect_string_list(payload["check_ids"], "candidate_delivery_recorded.check_ids")
    _validate_ref_list(
        payload["evidence_refs"],
        "candidate_delivery_recorded.evidence_refs",
        validate_object_ref,
    )
    observed = expect_non_negative_int(
        payload["observed_seconds"], "candidate_delivery_recorded.observed_seconds"
    )
    if stage != "stable_observed" and observed != 0:
        raise ControlError("非 stable_observed 阶段的 observed_seconds 必须是 0")
    _expect_enum(
        payload["result"],
        {"pass", "failed"},
        "candidate_delivery_recorded.result",
    )
    expected_identity = canonical_sha256(
        {key: item for key, item in payload.items() if key != "identity_sha256"}
    )
    if payload["identity_sha256"] != expected_identity:
        raise ControlError("candidate_delivery_recorded.identity_sha256 与阶段内容不一致")
    return payload


def _validate_selector_payload(value: Any, *, activated: bool) -> dict[str, Any]:
    label = "selector_activated" if activated else "selector_observed"
    payload = expect_object(value, label)
    required = {"catalog_snapshot_ref", "observation_kind"}
    if activated:
        required |= {"deployment_ref"}
    expect_exact_keys(payload, required, label)
    catalog = validate_object_ref(payload["catalog_snapshot_ref"], f"{label}.catalog_snapshot_ref")
    if catalog["object_kind"] != "runtime_catalog_snapshot":
        raise ControlError(f"{label} 必须引用 RuntimeCatalogSnapshot")
    expected_kind = "activated" if activated else "read_only"
    if payload["observation_kind"] != expected_kind:
        raise ControlError(f"{label}.observation_kind 必须是 {expected_kind}")
    if activated:
        validate_fact_ref(payload["deployment_ref"], f"{label}.deployment_ref")
    return payload


def _validate_release_promoted_payload(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "release_promoted")
    expect_exact_keys(
        payload,
        {"candidate_ref", "acceptance_ref", "release_artifact_ref", "promotion_diff_ref"},
        "release_promoted",
    )
    validate_fact_ref(payload["candidate_ref"], "release_promoted.candidate_ref")
    validate_fact_ref(payload["acceptance_ref"], "release_promoted.acceptance_ref")
    release = validate_object_ref(
        payload["release_artifact_ref"], "release_promoted.release_artifact_ref"
    )
    diff = validate_object_ref(payload["promotion_diff_ref"], "release_promoted.promotion_diff_ref")
    if release["object_kind"] != "release_artifact" or diff["object_kind"] != "promotion_diff":
        raise ControlError("release_promoted 引用类型错误")
    return payload


def _validate_deployment_payload(value: Any, stage: str) -> dict[str, Any]:
    payload = expect_object(value, stage)
    expect_exact_keys(
        payload,
        {
            "deployment_id",
            "stage",
            "candidate_id",
            "previous_stage_ref",
            "acceptance_ref",
            "promotion_receipt_ref",
            "active_support_envelope_ref",
            "rollback_operational_envelope_ref",
            "deployment_traffic_envelope_ref",
            "runtime_catalog_snapshot_ref",
            "image_digest",
            "evidence_refs",
        },
        stage,
    )
    expect_safe_id(payload["deployment_id"], f"{stage}.deployment_id")
    if payload["stage"] != stage:
        raise ControlError(f"{stage}.stage 不匹配")
    expect_safe_id(payload["candidate_id"], f"{stage}.candidate_id")
    previous = payload["previous_stage_ref"]
    if stage == "accepted_not_activated":
        if previous is not None:
            raise ControlError("accepted_not_activated 不得声明部署前序")
    else:
        validate_fact_ref(previous, f"{stage}.previous_stage_ref")
    validate_fact_ref(payload["acceptance_ref"], f"{stage}.acceptance_ref")
    promotion = validate_receipt_ref(payload["promotion_receipt_ref"], f"{stage}.promotion_receipt_ref")
    if promotion["receipt_kind"] != "promotion":
        raise ControlError(f"{stage} 必须引用 promotion receipt")
    expected_objects = {
        "active_support_envelope_ref": "active_support_envelope",
        "rollback_operational_envelope_ref": "rollback_operational_envelope",
        "deployment_traffic_envelope_ref": "deployment_traffic_envelope",
        "runtime_catalog_snapshot_ref": "runtime_catalog_snapshot",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(payload[key], f"{stage}.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"{stage}.{key} 类型错误")
    expect_image_digest(payload["image_digest"], f"{stage}.image_digest")
    _validate_ref_list(payload["evidence_refs"], f"{stage}.evidence_refs", validate_object_ref)
    return payload


def _validate_inventory_current_appended(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "inventory_current_appended")
    expect_exact_keys(
        payload,
        {"deployment_ref", "production_ingress_inventory_ref", "egress_disposition_inventory_ref"},
        "inventory_current_appended",
    )
    validate_fact_ref(payload["deployment_ref"], "inventory_current_appended.deployment_ref")
    ingress = validate_object_ref(
        payload["production_ingress_inventory_ref"],
        "inventory_current_appended.production_ingress_inventory_ref",
    )
    egress = validate_object_ref(
        payload["egress_disposition_inventory_ref"],
        "inventory_current_appended.egress_disposition_inventory_ref",
    )
    if ingress["object_kind"] != "production_ingress_inventory" or egress["object_kind"] != "egress_disposition_inventory":
        raise ControlError("inventory_current_appended Inventory 引用类型错误")
    return payload


def validate_fact_payload(fact_kind: str, payload: Any) -> dict[str, Any]:
    validators: dict[str, Callable[[Any], dict[str, Any]]] = {
        "discovery_recorded": _validate_discovery_payload,
        "evidence_recorded": _validate_evidence_recorded_payload,
        "rule_classification_recorded": _validate_rule_classification_recorded_payload,
        "evidence_approved": _validate_evidence_approved_payload,
        "profile_approved": _validate_profile_approved_payload,
        "candidate_frozen": _validate_candidate_payload,
        "pair_recorded": _validate_pair_payload,
        "acceptance_recorded": _validate_acceptance_payload,
        "validation_attempt_created": _validate_validation_attempt_payload,
        "validation_completed": _validate_validation_completed_payload,
        "selector_observed": lambda value: _validate_selector_payload(value, activated=False),
        "selector_activated": lambda value: _validate_selector_payload(value, activated=True),
        "release_promoted": _validate_release_promoted_payload,
        "inventory_current_appended": _validate_inventory_current_appended,
        "candidate_delivery_recorded": _validate_candidate_delivery_payload,
    }
    if fact_kind in SCENARIO_FACT_STAGE:
        return _validate_scenario_payload(payload, SCENARIO_FACT_STAGE[fact_kind])
    if fact_kind in DEPLOYMENT_STAGES:
        return _validate_deployment_payload(payload, fact_kind)
    try:
        return validators[fact_kind](payload)
    except KeyError as error:
        raise ControlError(f"未登记的 fact_kind：{fact_kind}") from error


def validate_fact_document(value: Any) -> dict[str, Any]:
    document = expect_object(value, "fact")
    expect_exact_keys(
        document,
        {
            "schema_version",
            "campaign_id",
            "dimension",
            "fact_kind",
            "sequence",
            "previous_fact_sha256",
            "issued_at_utc",
            "payload",
        },
        "fact",
    )
    if document["schema_version"] != FACT_SCHEMA:
        raise ControlError("fact.schema_version 不匹配")
    expect_safe_id(document["campaign_id"], "fact.campaign_id")
    dimension = expect_string(document["dimension"], "fact.dimension")
    if dimension not in DIMENSIONS:
        raise ControlError("fact.dimension 非法")
    fact_kind = expect_string(document["fact_kind"], "fact.fact_kind")
    if FACT_DIMENSION.get(fact_kind) != dimension:
        raise ControlError("fact_kind 与正交 dimension 不匹配")
    sequence = expect_non_negative_int(document["sequence"], "fact.sequence")
    if sequence < 1:
        raise ControlError("fact.sequence 必须从 1 开始")
    previous = document["previous_fact_sha256"]
    if sequence == 1:
        if previous is not None:
            raise ControlError("首条事实不得声明 previous_fact_sha256")
    else:
        expect_sha256(previous, "fact.previous_fact_sha256")
    expect_rfc3339(document["issued_at_utc"], "fact.issued_at_utc")
    validate_fact_payload(fact_kind, document["payload"])
    return document


def validate_store_metadata(value: Any) -> dict[str, Any]:
    payload = expect_object(value, "store")
    expect_exact_keys(payload, {"schema_version", "created_at_utc"}, "store")
    if payload["schema_version"] != STORE_SCHEMA:
        raise ControlError("store.schema_version 不匹配")
    expect_rfc3339(payload["created_at_utc"], "store.created_at_utc")
    return payload


def _validate_candidate_build_receipt(value: Any) -> dict[str, Any]:
    receipt = expect_object(value, "candidate_build_receipt")
    expect_exact_keys(
        receipt,
        {
            "schema_version",
            "campaign_id",
            "candidate_id",
            "candidate_purpose",
            "target_version",
            "profile_approval_ref",
            "release_artifact_ref",
            "support_envelope_ref",
            "source_identity",
            "profile_identity",
            "validation_scope_sha256",
            "build_identity",
            "artifact_inventory",
            "image",
            "input_facts_sha256",
            "completed_at_utc",
            "producer_tool_sha256",
            "identity_sha256",
        },
        "candidate_build_receipt",
    )
    if receipt["schema_version"] != CANDIDATE_BUILD_RECEIPT_SCHEMA:
        raise ControlError("candidate_build_receipt.schema_version 不匹配")
    expect_safe_id(receipt["campaign_id"], "candidate_build_receipt.campaign_id")
    expect_safe_id(receipt["candidate_id"], "candidate_build_receipt.candidate_id")
    _expect_enum(
        receipt["candidate_purpose"],
        APPROVAL_PURPOSES,
        "candidate_build_receipt.candidate_purpose",
    )
    expect_string(receipt["target_version"], "candidate_build_receipt.target_version")
    validate_fact_ref(
        receipt["profile_approval_ref"], "candidate_build_receipt.profile_approval_ref"
    )
    for key, expected_kind in (
        ("release_artifact_ref", "release_artifact"),
        ("support_envelope_ref", "support_envelope"),
    ):
        reference = validate_object_ref(receipt[key], f"candidate_build_receipt.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"candidate_build_receipt.{key} 类型错误")
    source = expect_object(receipt["source_identity"], "candidate_build_receipt.source_identity")
    expect_exact_keys(
        source,
        {
            "git_commit",
            "source_tree_sha256",
            "test_tree_sha256",
            "dependency_lock_sha256",
            "clean",
        },
        "candidate_build_receipt.source_identity",
    )
    commit = expect_string(source["git_commit"], "candidate_build_receipt.source_identity.git_commit")
    if not _GIT_COMMIT_RE.fullmatch(commit):
        raise ControlError("candidate_build_receipt.source_identity.git_commit 必须是完整 Git commit")
    for key in ("source_tree_sha256", "test_tree_sha256", "dependency_lock_sha256"):
        expect_sha256(source[key], f"candidate_build_receipt.source_identity.{key}")
    if expect_bool(source["clean"], "candidate_build_receipt.source_identity.clean") is not True:
        raise ControlError("Candidate 构建源码树必须处于 clean 状态")
    profile = expect_object(
        receipt["profile_identity"], "candidate_build_receipt.profile_identity"
    )
    expect_exact_keys(
        profile,
        {
            "profile_schema_ref",
            "snapshot_ref",
            "release_artifact_ref",
            "model_capability_catalog_ref",
            "support_envelope_ref",
            "profile_digest",
        },
        "candidate_build_receipt.profile_identity",
    )
    profile_kinds = {
        "profile_schema_ref": "profile_schema",
        "snapshot_ref": "snapshot",
        "release_artifact_ref": "release_artifact",
        "model_capability_catalog_ref": "model_capability_catalog",
        "support_envelope_ref": "support_envelope",
    }
    for key, expected_kind in profile_kinds.items():
        reference = validate_object_ref(profile[key], f"candidate_build_receipt.profile_identity.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"candidate_build_receipt.profile_identity.{key} 类型错误")
    if (
        profile["release_artifact_ref"] != receipt["release_artifact_ref"]
        or profile["support_envelope_ref"] != receipt["support_envelope_ref"]
    ):
        raise ControlError("candidate_build_receipt 顶层与画像身份引用不一致")
    expect_sha256(
        profile["profile_digest"], "candidate_build_receipt.profile_identity.profile_digest"
    )
    expect_sha256(
        receipt["validation_scope_sha256"],
        "candidate_build_receipt.validation_scope_sha256",
    )
    build = expect_object(receipt["build_identity"], "candidate_build_receipt.build_identity")
    expect_exact_keys(
        build,
        {
            "build_id",
            "deployed_version",
            "target_architecture",
            "toolchain",
            "parameters",
            "parameters_sha256",
        },
        "candidate_build_receipt.build_identity",
    )
    expect_safe_id(build["build_id"], "candidate_build_receipt.build_identity.build_id")
    expect_string(
        build["deployed_version"], "candidate_build_receipt.build_identity.deployed_version"
    )
    if build["deployed_version"] != receipt["target_version"]:
        raise ControlError("candidate_build_receipt deployed_version 与目标版本不一致")
    expect_string(
        build["target_architecture"],
        "candidate_build_receipt.build_identity.target_architecture",
    )
    expect_string_list(build["toolchain"], "candidate_build_receipt.build_identity.toolchain")
    expect_object(build["parameters"], "candidate_build_receipt.build_identity.parameters")
    if build["parameters_sha256"] != canonical_sha256(build["parameters"]):
        raise ControlError("candidate_build_receipt.build_identity.parameters_sha256 不匹配")
    inventory = expect_object(
        receipt["artifact_inventory"], "candidate_build_receipt.artifact_inventory"
    )
    expect_exact_keys(
        inventory,
        {"entries", "file_count", "total_bytes", "inventory_sha256"},
        "candidate_build_receipt.artifact_inventory",
    )
    entries = inventory["entries"]
    if not isinstance(entries, list) or not entries:
        raise ControlError("candidate_build_receipt.artifact_inventory.entries 必须是非空数组")
    paths: list[str] = []
    for index, raw in enumerate(entries):
        label = f"candidate_build_receipt.artifact_inventory.entries[{index}]"
        entry = expect_object(raw, label)
        expect_exact_keys(entry, {"role", "binding", "source_sha256"}, label)
        expect_safe_id(entry["role"], f"{label}.role")
        binding = validate_external_binding(entry["binding"], f"{label}.binding")
        paths.append(binding["path"])
        expect_sha256(entry["source_sha256"], f"{label}.source_sha256")
        if entry["source_sha256"] != source["source_tree_sha256"]:
            raise ControlError(f"{label} 未绑定同一最终源码树")
    if paths != sorted(set(paths)):
        raise ControlError("candidate_build_receipt artifact inventory 必须按路径排序且不得重复")
    if inventory["file_count"] != len(entries):
        raise ControlError("candidate_build_receipt artifact file_count 不一致")
    if inventory["total_bytes"] != sum(entry["binding"]["bytes"] for entry in entries):
        raise ControlError("candidate_build_receipt artifact total_bytes 不一致")
    if inventory["inventory_sha256"] != canonical_sha256(entries):
        raise ControlError("candidate_build_receipt artifact inventory_sha256 不一致")
    image = expect_object(receipt["image"], "candidate_build_receipt.image")
    expect_exact_keys(
        image,
        {"reference", "image_digest", "image_id", "manifest_digest"},
        "candidate_build_receipt.image",
    )
    reference = expect_string(image["reference"], "candidate_build_receipt.image.reference")
    for key in ("image_digest", "image_id", "manifest_digest"):
        expect_image_digest(image[key], f"candidate_build_receipt.image.{key}")
    if image["image_digest"] != image["manifest_digest"]:
        raise ControlError("Candidate image_digest 必须等于 OCI manifest_digest")
    if not reference.endswith("@" + image["manifest_digest"]):
        raise ControlError("Candidate 镜像引用必须以冻结的 OCI manifest digest 结尾")
    expected_inputs = [
        receipt["profile_approval_ref"],
        receipt["release_artifact_ref"],
        receipt["support_envelope_ref"],
        profile["model_capability_catalog_ref"],
    ]
    if receipt["input_facts_sha256"] != canonical_sha256(expected_inputs):
        raise ControlError("candidate_build_receipt.input_facts_sha256 不匹配")
    for key in ("input_facts_sha256", "producer_tool_sha256"):
        expect_sha256(receipt[key], f"candidate_build_receipt.{key}")
    expect_rfc3339(receipt["completed_at_utc"], "candidate_build_receipt.completed_at_utc")
    expected_identity = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    if receipt["identity_sha256"] != expected_identity:
        raise ControlError("candidate_build_receipt.identity_sha256 与构建内容不一致")
    return receipt


def _validate_validation_gate_receipt(value: Any) -> dict[str, Any]:
    receipt = expect_object(value, "validation_gate_receipt")
    expect_exact_keys(
        receipt,
        {
            "schema_version",
            "campaign_id",
            "candidate_id",
            "attempt_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_attempt_ref",
            "validation_execution_plan_ref",
            "gate_id",
            "requirement_sha256",
            "source",
            "command",
            "working_directory",
            "host_id",
            "architecture",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "output_sha256",
            "result",
            "reused_receipt_ref",
            "previous_failed_receipt_ref",
            "input_facts_sha256",
            "producer_tool_sha256",
            "identity_sha256",
        },
        "validation_gate_receipt",
    )
    if receipt["schema_version"] != VALIDATION_GATE_RECEIPT_SCHEMA:
        raise ControlError("validation_gate_receipt.schema_version 不匹配")
    for key in ("campaign_id", "candidate_id", "attempt_id", "gate_id", "host_id"):
        expect_safe_id(receipt[key], f"validation_gate_receipt.{key}")
    for key in ("candidate_ref", "validation_attempt_ref"):
        validate_fact_ref(receipt[key], f"validation_gate_receipt.{key}")
    build_ref = validate_receipt_ref(
        receipt["candidate_build_receipt_ref"],
        "validation_gate_receipt.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("validation_gate_receipt 必须引用 CandidateBuildReceipt")
    plan_ref = validate_object_ref(
        receipt["validation_execution_plan_ref"],
        "validation_gate_receipt.validation_execution_plan_ref",
    )
    if plan_ref["object_kind"] != "validation_execution_plan":
        raise ControlError("validation_gate_receipt 必须引用 ValidationExecutionPlan")
    expect_sha256(receipt["requirement_sha256"], "validation_gate_receipt.requirement_sha256")
    source = _expect_enum(receipt["source"], {"execute", "reuse"}, "validation_gate_receipt.source")
    expect_string_list(
        receipt["command"], "validation_gate_receipt.command", sorted_unique=False
    )
    expect_string(receipt["working_directory"], "validation_gate_receipt.working_directory")
    expect_string(receipt["architecture"], "validation_gate_receipt.architecture")
    started = expect_rfc3339(receipt["started_at_utc"], "validation_gate_receipt.started_at_utc")
    completed = expect_rfc3339(
        receipt["completed_at_utc"], "validation_gate_receipt.completed_at_utc"
    )
    if datetime.fromisoformat(completed.replace("Z", "+00:00")) < datetime.fromisoformat(
        started.replace("Z", "+00:00")
    ):
        raise ControlError("validation_gate_receipt 完成时间早于开始时间")
    exit_code = expect_non_negative_int(receipt["exit_code"], "validation_gate_receipt.exit_code")
    expect_sha256(receipt["output_sha256"], "validation_gate_receipt.output_sha256")
    result = _expect_enum(receipt["result"], {"pass", "failed"}, "validation_gate_receipt.result")
    if (result == "pass") != (exit_code == 0):
        raise ControlError("validation_gate_receipt result 与 exit_code 不一致")
    reused = receipt["reused_receipt_ref"]
    previous_failed = receipt["previous_failed_receipt_ref"]
    if source == "execute":
        if reused is not None:
            raise ControlError("execute 门禁不得声明 reused_receipt_ref")
        if previous_failed is not None:
            reference = validate_receipt_ref(
                previous_failed,
                "validation_gate_receipt.previous_failed_receipt_ref",
            )
            if reference["receipt_kind"] != "validation_gate":
                raise ControlError("前序失败必须引用 validation_gate 收据")
    else:
        reference = validate_receipt_ref(reused, "validation_gate_receipt.reused_receipt_ref")
        if reference["receipt_kind"] != "validation_gate":
            raise ControlError("reuse 门禁必须引用 validation_gate 收据")
        if previous_failed is not None:
            raise ControlError("reuse 门禁不得同时声明前序失败收据")
    expected_inputs = [
        receipt["validation_attempt_ref"],
        receipt["validation_execution_plan_ref"],
        receipt["candidate_build_receipt_ref"],
        receipt["reused_receipt_ref"],
        receipt["previous_failed_receipt_ref"],
    ]
    if receipt["input_facts_sha256"] != canonical_sha256(expected_inputs):
        raise ControlError("validation_gate_receipt.input_facts_sha256 不匹配")
    for key in ("input_facts_sha256", "producer_tool_sha256"):
        expect_sha256(receipt[key], f"validation_gate_receipt.{key}")
    expected_identity = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    if receipt["identity_sha256"] != expected_identity:
        raise ControlError("validation_gate_receipt.identity_sha256 与门禁内容不一致")
    return receipt


def _validate_candidate_delivery_receipt(value: Any) -> dict[str, Any]:
    receipt = expect_object(value, "candidate_delivery_receipt")
    expect_exact_keys(
        receipt,
        {
            "schema_version",
            "campaign_id",
            "campaign_purpose",
            "target_version",
            "profile_approval_ref",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "candidate_evidence_package_ref",
            "delivery_plan_ref",
            "delivery_fact_refs",
            "candidate_delivery_package_ref",
            "private_archive_manifest_ref",
            "candidate_image_digest",
            "host_id",
            "architecture",
            "release_state",
            "vircs_state",
            "input_facts_sha256",
            "completed_at_utc",
            "producer_tool_sha256",
            "identity_sha256",
        },
        "candidate_delivery_receipt",
    )
    if receipt["schema_version"] != CANDIDATE_DELIVERY_RECEIPT_SCHEMA:
        raise ControlError("candidate_delivery_receipt.schema_version 不匹配")
    for key in ("campaign_id", "candidate_id", "host_id"):
        expect_safe_id(receipt[key], f"candidate_delivery_receipt.{key}")
    _expect_enum(
        receipt["campaign_purpose"],
        APPROVAL_PURPOSES,
        "candidate_delivery_receipt.campaign_purpose",
    )
    expect_string(receipt["target_version"], "candidate_delivery_receipt.target_version")
    for key in (
        "profile_approval_ref",
        "candidate_ref",
        "validation_completed_ref",
        "acceptance_ref",
    ):
        validate_fact_ref(receipt[key], f"candidate_delivery_receipt.{key}")
    build_ref = validate_receipt_ref(
        receipt["candidate_build_receipt_ref"],
        "candidate_delivery_receipt.candidate_build_receipt_ref",
    )
    if build_ref["receipt_kind"] != "candidate_build":
        raise ControlError("candidate_delivery_receipt 必须引用 CandidateBuildReceipt")
    expected_objects = {
        "candidate_evidence_package_ref": "candidate_evidence_package",
        "delivery_plan_ref": "candidate_delivery_plan",
        "candidate_delivery_package_ref": "candidate_delivery_package",
        "private_archive_manifest_ref": "private_archive_manifest",
    }
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(receipt[key], f"candidate_delivery_receipt.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"candidate_delivery_receipt.{key} 类型错误")
    fact_refs = receipt["delivery_fact_refs"]
    if not isinstance(fact_refs, list) or len(fact_refs) != len(CANDIDATE_DELIVERY_STAGES):
        raise ControlError("candidate_delivery_receipt 必须绑定完整四阶段事实")
    for index, reference in enumerate(fact_refs):
        validate_fact_ref(reference, f"candidate_delivery_receipt.delivery_fact_refs[{index}]")
    expect_image_digest(
        receipt["candidate_image_digest"],
        "candidate_delivery_receipt.candidate_image_digest",
    )
    expect_string(receipt["architecture"], "candidate_delivery_receipt.architecture")
    if receipt["release_state"] != "ready_for_operator_release":
        raise ControlError("candidate_delivery_receipt.release_state 必须是 ready_for_operator_release")
    _validate_vircs_state(receipt["vircs_state"], "candidate_delivery_receipt.vircs_state")
    expected_inputs = [
        receipt["candidate_ref"],
        receipt["candidate_build_receipt_ref"],
        receipt["validation_completed_ref"],
        receipt["acceptance_ref"],
        receipt["candidate_evidence_package_ref"],
        receipt["delivery_plan_ref"],
        *receipt["delivery_fact_refs"],
        receipt["candidate_delivery_package_ref"],
        receipt["private_archive_manifest_ref"],
    ]
    if receipt["input_facts_sha256"] != canonical_sha256(expected_inputs):
        raise ControlError("candidate_delivery_receipt.input_facts_sha256 不匹配")
    for key in ("input_facts_sha256", "producer_tool_sha256"):
        expect_sha256(receipt[key], f"candidate_delivery_receipt.{key}")
    expect_rfc3339(receipt["completed_at_utc"], "candidate_delivery_receipt.completed_at_utc")
    expected_identity = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "identity_sha256"}
    )
    if receipt["identity_sha256"] != expected_identity:
        raise ControlError("candidate_delivery_receipt.identity_sha256 与交付内容不一致")
    return receipt


def validate_receipt_document(value: Any, kind: str) -> dict[str, Any]:
    if kind == "candidate_build":
        return _validate_candidate_build_receipt(value)
    if kind == "validation_gate":
        return _validate_validation_gate_receipt(value)
    if kind == "candidate_delivery":
        return _validate_candidate_delivery_receipt(value)
    receipt = expect_object(value, f"{kind}_receipt")
    if kind == "promotion":
        required = {
            "schema_version",
            "campaign_id",
            "candidate_ref",
            "acceptance_ref",
            "promotion_fact_ref",
            "release_artifact_ref",
            "promotion_diff_ref",
            "profile_approval_ref",
            "input_facts_sha256",
            "completed_at_utc",
            "producer_tool_sha256",
        }
        expected_schema = PROMOTION_RECEIPT_SCHEMA
    elif kind == "activation":
        required = {
            "schema_version",
            "campaign_id",
            "candidate_ref",
            "acceptance_ref",
            "promotion_receipt_ref",
            "deployment_fact_refs",
            "selector_before_ref",
            "selector_after_ref",
            "active_support_envelope_ref",
            "rollback_operational_envelope_ref",
            "deployment_traffic_envelope_ref",
            "final_ingress_inventory_ref",
            "final_egress_inventory_ref",
            "formal_image_digest",
            "final_state",
            "input_facts_sha256",
            "completed_at_utc",
            "producer_tool_sha256",
        }
        expected_schema = ACTIVATION_RECEIPT_SCHEMA
    else:
        raise ControlError(f"未知 receipt kind：{kind}")
    expect_exact_keys(receipt, required, f"{kind}_receipt")
    if receipt["schema_version"] != expected_schema:
        raise ControlError(f"{kind}_receipt.schema_version 不匹配")
    expect_safe_id(receipt["campaign_id"], f"{kind}_receipt.campaign_id")
    validate_fact_ref(receipt["candidate_ref"], f"{kind}_receipt.candidate_ref")
    validate_fact_ref(receipt["acceptance_ref"], f"{kind}_receipt.acceptance_ref")
    if kind == "promotion":
        validate_fact_ref(receipt["promotion_fact_ref"], "promotion_receipt.promotion_fact_ref")
        validate_fact_ref(receipt["profile_approval_ref"], "promotion_receipt.profile_approval_ref")
        expected_objects = {
            "release_artifact_ref": "release_artifact",
            "promotion_diff_ref": "promotion_diff",
        }
    else:
        promotion = validate_receipt_ref(
            receipt["promotion_receipt_ref"], "activation_receipt.promotion_receipt_ref"
        )
        if promotion["receipt_kind"] != "promotion":
            raise ControlError("activation receipt 必须引用 promotion receipt")
        raw_refs = receipt["deployment_fact_refs"]
        if not isinstance(raw_refs, list):
            raise ControlError("activation_receipt.deployment_fact_refs 必须是数组")
        refs = [
            validate_fact_ref(item, f"activation_receipt.deployment_fact_refs[{index}]")
            for index, item in enumerate(raw_refs)
        ]
        if len(refs) != len(DEPLOYMENT_STAGES):
            raise ControlError("activation receipt 必须绑定完整五阶段 DeploymentFact")
        validate_fact_ref(receipt["selector_before_ref"], "activation_receipt.selector_before_ref")
        validate_fact_ref(receipt["selector_after_ref"], "activation_receipt.selector_after_ref")
        expected_objects = {
            "active_support_envelope_ref": "active_support_envelope",
            "rollback_operational_envelope_ref": "rollback_operational_envelope",
            "deployment_traffic_envelope_ref": "deployment_traffic_envelope",
            "final_ingress_inventory_ref": "production_ingress_inventory",
            "final_egress_inventory_ref": "egress_disposition_inventory",
        }
        expect_image_digest(receipt["formal_image_digest"], "activation_receipt.formal_image_digest")
        if receipt["final_state"] != "restored_active":
            raise ControlError("activation receipt final_state 必须是 restored_active")
    for key, expected_kind in expected_objects.items():
        reference = validate_object_ref(receipt[key], f"{kind}_receipt.{key}")
        if reference["object_kind"] != expected_kind:
            raise ControlError(f"{kind}_receipt.{key} 类型错误")
    expect_sha256(receipt["input_facts_sha256"], f"{kind}_receipt.input_facts_sha256")
    expect_rfc3339(receipt["completed_at_utc"], f"{kind}_receipt.completed_at_utc")
    expect_sha256(receipt["producer_tool_sha256"], f"{kind}_receipt.producer_tool_sha256")
    return receipt
