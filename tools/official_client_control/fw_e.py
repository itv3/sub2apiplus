"""FW-E 证据、发送面和未批准提案的通用封存器。

本模块只负责把已经采集并复核的外部证据写入 FW-D 的追加式 Store。它不会
生成 ProfileSchema、Snapshot、ReleaseArtifact、Persona 实现或任何生产绑定，
也不会签发 ``evidence_approved``。客户端专用的下载、抓包和规则判断必须先在
各自工具中完成，再通过本模块的闭合计划进入 Store。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import (
    canonical_sha256,
    expect_exact_keys,
    expect_rfc3339,
    expect_safe_id,
    expect_sha256,
    expect_string,
    expect_string_list,
    load_json_file,
    resolve_relative,
    sha256_file,
    validate_external_binding,
    validate_relative_path,
)
from .contracts import (
    COMPATIBILITY_CLASSES,
    EGRESS_DISPOSITIONS,
    EGRESS_GUARD_STATES,
    EVIDENCE_LEVELS,
    INGRESS_DISPOSITIONS,
    MIGRATION_DECISIONS,
    RULE_LIFECYCLES,
    SPEC_ID_RE,
    campaign_identity_sha256,
    validate_persona,
)
from .errors import ControlError
from .receipts import control_tool_bundle_sha256
from .store import ControlStore


PLAN_SCHEMA = "official-client-fw-e-seal-plan/v2"
TARGET_INVENTORY_SCHEMA = "claude-code-target-sink-inventory/v1"
CROSS_SOURCE_MATRIX_SCHEMA = "claude-code-fw-e-cross-source-matrix/v1"
COMPLETENESS_SCHEMA = "claude-code-fw-e-completeness/v1"
CAPTURE_INDEX_SCHEMA = "claude-code-fw-e-capture-index/v1"
REQUIRED_PRIVACY_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}


def external_binding(external_root: Path, relative_path: str) -> dict[str, Any]:
    """为外部证据生成可复算绑定，并拒绝符号链接和越界路径。"""

    validate_relative_path(relative_path, "外部证据路径")
    path = resolve_relative(external_root, relative_path)
    if path.is_symlink() or not path.is_file():
        raise ControlError(f"外部证据不是可信普通文件：{relative_path}")
    binding = {
        "path": relative_path,
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
    validate_external_binding(binding, "外部证据绑定")
    return binding


def external_bindings(
    external_root: Path, relative_paths: list[str], label: str
) -> list[dict[str, Any]]:
    """生成按路径严格排序且无重复的外部证据绑定。"""

    paths = expect_string_list(relative_paths, label)
    return [external_binding(external_root, path) for path in paths]


def _load_external_json(
    external_root: Path, relative_path: Any, label: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取一个已完成路径与摘要约束的外部 JSON。"""

    relative = expect_string(relative_path, label)
    binding = external_binding(external_root, relative)
    value = load_json_file(resolve_relative(external_root, relative), label)
    return value, binding


def _validate_completeness_artifacts(
    external_root: Path,
    *,
    target_version: str,
    inventory_paths: Any,
    matrix_path: Any,
    closure_path: Any,
    capture_index_path: Any,
) -> dict[str, Any]:
    """绑定目标 sink、四方矩阵、闭集结论与全量运行发现。"""

    inventory_relatives = expect_string_list(
        inventory_paths, "target_sink_inventory_paths"
    )
    inventories: list[dict[str, Any]] = []
    inventory_bindings: list[dict[str, Any]] = []
    inventory_sha256s: set[str] = set()
    platforms: set[str] = set()
    for relative in inventory_relatives:
        value, binding = _load_external_json(
            external_root, relative, "target sink inventory"
        )
        if (
            value.get("schema_version") != TARGET_INVENTORY_SCHEMA
            or value.get("target_version") != target_version
        ):
            raise ControlError("target sink inventory Schema 或目标版本不匹配")
        completeness = value.get("completeness")
        if (
            not isinstance(completeness, dict)
            or completeness.get("truncated") is not False
            or completeness.get("ast_parse_diagnostic_count") != 0
            or completeness.get("ambiguous_lexical_match_count") != 0
            or completeness.get("duplicate_sink_id_count") != 0
        ):
            raise ControlError("target sink inventory 不完整")
        sinks = value.get("sinks")
        if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
            raise ControlError("target sink inventory 数量不一致")
        sink_ids = [row.get("sink_id") for row in sinks if isinstance(row, dict)]
        if len(sink_ids) != len(sinks) or len(set(sink_ids)) != len(sink_ids):
            raise ControlError("target sink inventory 身份缺失或重复")
        platform = expect_string(value.get("platform"), "target inventory platform")
        if platform in platforms:
            raise ControlError(f"target sink inventory 平台重复：{platform}")
        platforms.add(platform)
        inventories.append(value)
        inventory_bindings.append(binding)
        inventory_sha256s.add(binding["sha256"])

    matrix, matrix_binding = _load_external_json(
        external_root, matrix_path, "cross_source_matrix_path"
    )
    closure, closure_binding = _load_external_json(
        external_root, closure_path, "completeness_closure_path"
    )
    capture, capture_binding = _load_external_json(
        external_root, capture_index_path, "capture_index_path"
    )
    if (
        matrix.get("schema_version") != CROSS_SOURCE_MATRIX_SCHEMA
        or matrix.get("target_version") != target_version
    ):
        raise ControlError("cross-source matrix Schema 或目标版本不匹配")
    if (
        closure.get("schema_version") != COMPLETENESS_SCHEMA
        or closure.get("target_version") != target_version
        or closure.get("result") != "passed"
        or closure.get("unresolved_total") != 0
        or closure.get("matrix_sha256") != canonical_sha256(matrix)
    ):
        raise ControlError("FW-E completeness closure 未通过或没有绑定当前矩阵")
    unresolved = closure.get("unresolved")
    if not isinstance(unresolved, dict) or any(unresolved.values()):
        raise ControlError("FW-E completeness closure 仍含未处置项")
    if closure.get("target_inventory_sha256") not in inventory_sha256s:
        raise ControlError("completeness closure 未绑定计划内 target inventory")
    if closure.get("target_sink_disposition_counts", {}).get("unclassified", 0):
        raise ControlError("completeness closure 仍含 unclassified target sink")
    if closure.get("runtime_observation_disposition_counts", {}).get(
        "unclassified", 0
    ):
        raise ControlError("completeness closure 仍含 inventory 外运行观测")

    target_sinks = matrix.get("target_sinks")
    if not isinstance(target_sinks, list):
        raise ControlError("cross-source matrix 缺少 target_sinks")
    matrix_sink_ids = [
        row.get("sink_id") for row in target_sinks if isinstance(row, dict)
    ]
    if (
        len(matrix_sink_ids) != len(target_sinks)
        or len(set(matrix_sink_ids)) != len(matrix_sink_ids)
        or closure.get("target_sink_total") != len(matrix_sink_ids)
    ):
        raise ControlError("cross-source matrix target sink 闭集不一致")
    matching_inventories = [
        value
        for value, binding in zip(inventories, inventory_bindings, strict=True)
        if binding["sha256"] == closure["target_inventory_sha256"]
    ]
    if len(matching_inventories) != 1:
        raise ControlError("completeness closure 必须唯一绑定一个 target inventory")
    bound_sink_ids = sorted(
        str(row["sink_id"]) for row in matching_inventories[0]["sinks"]
    )
    if sorted(str(item) for item in matrix_sink_ids) != bound_sink_ids:
        raise ControlError("cross-source matrix 与 target inventory sink 分母不一致")

    if (
        capture.get("schema_version") != CAPTURE_INDEX_SCHEMA
        or capture.get("target_version") != target_version
        or capture.get("result") != "passed"
        or capture.get("network_inventory", {}).get("result") != "passed"
        or capture.get("network_inventory", {}).get("host_prefilter_disabled") is not True
    ):
        raise ControlError("FW-E capture index 没有通过全 host/path inventory 门禁")
    target_capture = capture.get("target")
    if (
        not isinstance(target_capture, dict)
        or target_capture.get("capture_host_scopes") != ["all"]
        or not isinstance(target_capture.get("network_observations"), list)
    ):
        raise ControlError("FW-E target capture 仍使用 host 预筛或缺少网络 inventory")
    for group_name in ("control", "target"):
        group = capture.get(group_name)
        privacy = group.get("privacy_controls") if isinstance(group, dict) else None
        if (
            not isinstance(privacy, dict)
            or privacy.get("result") != "passed"
            or privacy.get("required_values") != REQUIRED_PRIVACY_ENV
            or not isinstance(privacy.get("case_count"), int)
            or privacy["case_count"] <= 0
            or not isinstance(privacy.get("environment_manifest_sha256s"), list)
            or not privacy["environment_manifest_sha256s"]
        ):
            raise ControlError(
                f"FW-E {group_name} capture 未证明两项隐私开关的实际值"
            )
    capture_observation_ids = sorted(
        str(row.get("observation_id"))
        for row in target_capture["network_observations"]
        if isinstance(row, dict)
    )
    matrix_runtime = matrix.get("runtime_observations")
    if not isinstance(matrix_runtime, list):
        raise ControlError("cross-source matrix 缺少 runtime observations")
    matrix_observation_ids = sorted(
        str(row.get("observation_id"))
        for row in matrix_runtime
        if isinstance(row, dict)
    )
    if (
        len(capture_observation_ids) != len(target_capture["network_observations"])
        or len(set(capture_observation_ids)) != len(capture_observation_ids)
        or capture_observation_ids != matrix_observation_ids
    ):
        raise ControlError("运行 host/path inventory 与四方矩阵处置不一致")

    target_rules = matrix.get("target_rules")
    if not isinstance(target_rules, list) or not target_rules:
        raise ControlError("cross-source matrix 缺少 target rules")
    expected_rule_ids = sorted(str(row.get("id")) for row in target_rules)
    if len(set(expected_rule_ids)) != len(expected_rule_ids):
        raise ControlError("cross-source matrix target rule ID 重复")
    return {
        "inventory_bindings": inventory_bindings,
        "matrix_binding": matrix_binding,
        "closure_binding": closure_binding,
        "capture_binding": capture_binding,
        "expected_rule_ids": expected_rule_ids,
        "target_sink_count": len(matrix_sink_ids),
        "runtime_observation_count": len(matrix_observation_ids),
    }


def _expect_enum(value: Any, allowed: set[str], label: str) -> str:
    text = expect_string(value, label)
    if text not in allowed:
        raise ControlError(f"{label} 非法：{text}")
    return text


def _validate_rules(
    rules: Any,
    expected_rule_ids: list[str],
    external_root: Path,
) -> list[dict[str, Any]]:
    if not isinstance(rules, list) or not rules:
        raise ControlError("FW-E rules 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    identities: list[str] = []
    for index, raw in enumerate(rules):
        label = f"FW-E rules[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(
            raw,
            {
                "spec_id",
                "evidence_level",
                "rule_lifecycle",
                "compatibility_class",
                "migration_decision",
                "decision_basis",
                "semantic_equivalence_proven",
                "evidence_paths",
                "applicability",
            },
            label,
        )
        spec_id = expect_string(raw["spec_id"], f"{label}.spec_id")
        if not SPEC_ID_RE.fullmatch(spec_id):
            raise ControlError(f"{label}.spec_id 非法")
        identities.append(spec_id)
        evidence_level = _expect_enum(
            raw["evidence_level"], EVIDENCE_LEVELS, f"{label}.evidence_level"
        )
        lifecycle = _expect_enum(
            raw["rule_lifecycle"], RULE_LIFECYCLES, f"{label}.rule_lifecycle"
        )
        compatibility = _expect_enum(
            raw["compatibility_class"],
            COMPATIBILITY_CLASSES,
            f"{label}.compatibility_class",
        )
        decision = _expect_enum(
            raw["migration_decision"],
            MIGRATION_DECISIONS,
            f"{label}.migration_decision",
        )
        basis = _expect_enum(
            raw["decision_basis"],
            {
                "semantic_equivalence_proven",
                "observed_difference",
                "inheritance_not_proven",
                "new_target_rule",
                "removed_target_rule",
                "condition_difference",
            },
            f"{label}.decision_basis",
        )
        equivalence = raw["semantic_equivalence_proven"]
        if not isinstance(equivalence, bool):
            raise ControlError(f"{label}.semantic_equivalence_proven 必须是布尔值")
        if decision == "inherit" and (
            not equivalence or basis != "semantic_equivalence_proven"
        ):
            raise ControlError(f"{spec_id} 未证明语义等价，禁止使用 inherit")
        if decision != "inherit" and equivalence:
            raise ControlError(f"{spec_id} 非 inherit 决策不得声称语义等价已证明")
        if decision == "delete" and lifecycle != "superseded":
            raise ControlError(f"{spec_id} delete 必须使用 superseded 生命周期")
        if evidence_level == "verified" and basis == "inheritance_not_proven":
            raise ControlError(f"{spec_id} 继承未证明时不能标为 verified")
        evidence_paths = expect_string_list(
            raw["evidence_paths"], f"{label}.evidence_paths"
        )
        applicability = expect_string_list(
            raw["applicability"], f"{label}.applicability"
        )
        if evidence_level == "blocked":
            required_validation_markers = {
                "approval_scope=validation_only",
                "production_eligibility=denied",
            }
            if (
                lifecycle != "candidate"
                or decision != "add"
                or not required_validation_markers.issubset(set(applicability))
                or not any(
                    item.startswith("validation_scope=") for item in applicability
                )
            ):
                raise ControlError(
                    f"{spec_id} blocked 规则只有在 validation-only、禁止生产且边界明确时才能封存 FW-E"
                )
        normalized.append(
            {
                "spec_id": spec_id,
                "evidence_level": evidence_level,
                "rule_lifecycle": lifecycle,
                "compatibility_class": compatibility,
                "migration_decision": decision,
                "evidence_refs": [
                    external_binding(external_root, path) for path in evidence_paths
                ],
                "applicability": applicability,
            }
        )
    expected = expect_string_list(expected_rule_ids, "expected_rule_ids")
    if identities != sorted(set(identities)):
        raise ControlError("FW-E rules 必须按 spec_id 排序且不得重复")
    if identities != expected:
        missing = sorted(set(expected) - set(identities))
        extra = sorted(set(identities) - set(expected))
        raise ControlError(f"FW-E 规则台账未闭合：missing={missing}, extra={extra}")
    return normalized


def _validate_aliases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("ingress.aliases 必须是非空数组")
    identities: list[str] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        label = f"ingress.aliases[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(
            raw,
            {"alias_id", "logical_ingress_id", "physical_route", "caller_ids"},
            label,
        )
        alias_id = expect_safe_id(raw["alias_id"], f"{label}.alias_id")
        identities.append(alias_id)
        result.append(
            {
                "alias_id": alias_id,
                "logical_ingress_id": expect_safe_id(
                    raw["logical_ingress_id"], f"{label}.logical_ingress_id"
                ),
                "physical_route": expect_string(
                    raw["physical_route"], f"{label}.physical_route"
                ),
                "caller_ids": expect_string_list(
                    raw["caller_ids"], f"{label}.caller_ids"
                ),
            }
        )
    if identities != sorted(set(identities)):
        raise ControlError("ingress.aliases 必须按 alias_id 排序且不得重复")
    return result


def _validate_ingress_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("ingress.entries 必须是非空数组")
    identities: list[str] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        label = f"ingress.entries[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(
            raw,
            {
                "logical_ingress_id",
                "physical_alias_ids",
                "caller_ids",
                "adapter_id",
                "route_id",
                "ingress_kind",
                "protocol_class",
                "current_disposition",
            },
            label,
        )
        ingress_id = expect_safe_id(
            raw["logical_ingress_id"], f"{label}.logical_ingress_id"
        )
        identities.append(ingress_id)
        result.append(
            {
                "logical_ingress_id": ingress_id,
                "physical_alias_ids": expect_string_list(
                    raw["physical_alias_ids"], f"{label}.physical_alias_ids"
                ),
                "caller_ids": expect_string_list(
                    raw["caller_ids"], f"{label}.caller_ids"
                ),
                "adapter_id": expect_safe_id(raw["adapter_id"], f"{label}.adapter_id"),
                "route_id": expect_safe_id(raw["route_id"], f"{label}.route_id"),
                "ingress_kind": _expect_enum(
                    raw["ingress_kind"],
                    {"official", "third_party"},
                    f"{label}.ingress_kind",
                ),
                "protocol_class": expect_safe_id(
                    raw["protocol_class"], f"{label}.protocol_class"
                ),
                "current_disposition": _expect_enum(
                    raw["current_disposition"],
                    INGRESS_DISPOSITIONS,
                    f"{label}.current_disposition",
                ),
            }
        )
    if identities != sorted(set(identities)):
        raise ControlError("ingress.entries 必须按 logical_ingress_id 排序且不得重复")
    return result


def _validate_observed_egresses(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("egress.observed 必须是非空数组")
    identities: list[str] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        label = f"egress.observed[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(
            raw, {"egress_id", "route_id", "sink_id", "oauth_related", "kind"}, label
        )
        egress_id = expect_safe_id(raw["egress_id"], f"{label}.egress_id")
        identities.append(egress_id)
        oauth_related = raw["oauth_related"]
        if not isinstance(oauth_related, bool):
            raise ControlError(f"{label}.oauth_related 必须是布尔值")
        result.append(
            {
                "egress_id": egress_id,
                "route_id": expect_safe_id(raw["route_id"], f"{label}.route_id"),
                "sink_id": expect_safe_id(raw["sink_id"], f"{label}.sink_id"),
                "oauth_related": oauth_related,
                "kind": _expect_enum(
                    raw["kind"],
                    {"inference", "lifecycle", "auxiliary"},
                    f"{label}.kind",
                ),
            }
        )
    if identities != sorted(set(identities)):
        raise ControlError("egress.observed 必须按 egress_id 排序且不得重复")
    return result


def _validate_managed_policy(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ControlError(f"{label} 必须是对象")
    keys = {
        "authentication",
        "endpoint",
        "client",
        "timeout_policy",
        "retry_policy",
        "secret_policy",
        "audit_policy",
    }
    expect_exact_keys(value, keys, label)
    return {key: expect_string(value[key], f"{label}.{key}") for key in sorted(keys)}


def _validate_egress_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("egress.entries 必须是非空数组")
    identities: list[str] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        label = f"egress.entries[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(
            raw,
            {
                "egress_id",
                "current_disposition",
                "current_guard_state",
                "spec_ids",
                "managed_policy",
            },
            label,
        )
        egress_id = expect_safe_id(raw["egress_id"], f"{label}.egress_id")
        identities.append(egress_id)
        disposition = _expect_enum(
            raw["current_disposition"],
            EGRESS_DISPOSITIONS,
            f"{label}.current_disposition",
        )
        state = _expect_enum(
            raw["current_guard_state"],
            EGRESS_GUARD_STATES,
            f"{label}.current_guard_state",
        )
        spec_ids = expect_string_list(
            raw["spec_ids"], f"{label}.spec_ids", non_empty=False
        )
        for spec_id in spec_ids:
            if not SPEC_ID_RE.fullmatch(spec_id):
                raise ControlError(f"{label}.spec_ids 包含非法 SPEC")
        policy = raw["managed_policy"]
        if disposition == "non_persona_managed":
            policy = _validate_managed_policy(policy, f"{label}.managed_policy")
        elif policy is not None:
            raise ControlError(f"{label}.managed_policy 只允许 managed 使用")
        if state == "legacy_observe" and disposition == "persona_strict":
            raise ControlError(f"{egress_id} 遗留观察路径不得冒充 persona_strict")
        if state == "source_absent" and disposition != "denied":
            raise ControlError(f"{egress_id} source_absent 只能对应 denied")
        if state == "out_of_scope_passthrough":
            raise ControlError(f"{egress_id} 已知 Claude OAuth 出站不得停留在 out_of_scope_passthrough")
        if state not in {"source_absent", "legacy_observe"}:
            raise ControlError(f"{egress_id} FW-E 当前事实只能是 source_absent 或 legacy_observe")
        result.append(
            {
                "egress_id": egress_id,
                "current_disposition": disposition,
                "current_guard_state": state,
                "spec_ids": spec_ids,
                "managed_policy": policy,
            }
        )
    if identities != sorted(set(identities)):
        raise ControlError("egress.entries 必须按 egress_id 排序且不得重复")
    return result


def _seal_evidence_manifest(
    store: ControlStore,
    persona: dict[str, Any],
    manifest_id: str,
    bindings: list[dict[str, Any]],
    purpose: str,
) -> dict[str, Any]:
    entries = [
        {
            "id": f"binding-{index:04d}",
            "facts": {"binding": binding, "purpose": purpose},
        }
        for index, binding in enumerate(bindings, start=1)
    ]
    return store.seal_object(
        "operational_evidence",
        {
            "schema_version": "official-client-operational-evidence/v1",
            "persona": persona,
            "manifest_id": manifest_id,
            "entries": entries,
        },
    )


def _validate_target_proposals(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ControlError("target_proposals 必须是非空数组")
    identities: list[str] = []
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        label = f"target_proposals[{index}]"
        if not isinstance(raw, dict):
            raise ControlError(f"{label} 必须是对象")
        expect_exact_keys(raw, {"id", "kind", "target_disposition", "rationale"}, label)
        proposal_id = expect_safe_id(raw["id"], f"{label}.id")
        identities.append(proposal_id)
        kind = _expect_enum(raw["kind"], {"ingress", "egress"}, f"{label}.kind")
        allowed = INGRESS_DISPOSITIONS if kind == "ingress" else EGRESS_DISPOSITIONS
        result.append(
            {
                "id": proposal_id,
                "kind": kind,
                "target_disposition": _expect_enum(
                    raw["target_disposition"], allowed, f"{label}.target_disposition"
                ),
                "rationale": expect_string(raw["rationale"], f"{label}.rationale"),
            }
        )
    if identities != sorted(set(identities)):
        raise ControlError("target_proposals 必须按 id 排序且不得重复")
    return result


def _validate_traffic_observation_policy(value: Any) -> dict[str, Any]:
    """冻结 FW-E 流量观测边界，禁止把流量是否出现当作一致性维度。"""

    if not isinstance(value, dict):
        raise ControlError("traffic_observation_policy 必须是对象")
    expect_exact_keys(
        value,
        {
            "traffic_presence_comparison",
            "strict_wire_traffic_classes",
            "record_only_traffic_classes",
            "absence_of_record_only_traffic",
        },
        "traffic_observation_policy",
    )
    presence_comparison = expect_string(
        value["traffic_presence_comparison"],
        "traffic_observation_policy.traffic_presence_comparison",
    )
    strict_wire = expect_string_list(
        value["strict_wire_traffic_classes"],
        "traffic_observation_policy.strict_wire_traffic_classes",
    )
    record_only = expect_string_list(
        value["record_only_traffic_classes"],
        "traffic_observation_policy.record_only_traffic_classes",
    )
    absence = expect_string(
        value["absence_of_record_only_traffic"],
        "traffic_observation_policy.absence_of_record_only_traffic",
    )
    if presence_comparison != "disabled":
        raise ControlError("FW-E 禁止把流量类别是否出现作为一致性对比维度")
    if strict_wire != ["essential"]:
        raise ControlError("FW-E strict wire／PAIR 范围只能使用 essential 流量")
    if record_only != ["nonessential", "telemetry"]:
        raise ControlError("FW-E 必须把 nonessential 与 telemetry 固定为 record-only")
    if absence != "conformant_not_a_difference":
        raise ControlError("FW-E 零遥测／零非必要流量必须判为允许且不构成差异")
    return {
        "traffic_presence_comparison": presence_comparison,
        "strict_wire_traffic_classes": strict_wire,
        "record_only_traffic_classes": record_only,
        "absence_of_record_only_traffic": absence,
    }


def seal_fw_e_plan(
    store: ControlStore,
    external_root: Path,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """封存 FW-E 当前事实并停在 ``evidence_recorded``，等待独立批准。"""

    if not external_root.is_absolute() or external_root.is_symlink() or not external_root.is_dir():
        raise ControlError("external_root 必须是现有的非符号链接绝对目录")
    expect_exact_keys(
        plan,
        {
            "schema_version",
            "campaign_id",
            "persona",
            "target_version",
            "platforms",
            "entrypoints",
            "default_conditions",
            "traffic_observation_policy",
            "created_at_utc",
            "discovered_at_utc",
            "discovery_source",
            "source_commit",
            "contract_source_paths",
            "fw_c_receipt_paths",
            "runtime_catalog_paths",
            "official_artifact_paths",
            "target_sink_inventory_paths",
            "cross_source_matrix_path",
            "completeness_closure_path",
            "capture_index_path",
            "rules",
            "inventory_observed_at_utc",
            "inventory_evidence_paths",
            "ingress_aliases",
            "ingress_entries",
            "egress_observed",
            "egress_entries",
            "target_proposals",
        },
        "FW-E seal plan",
    )
    if plan["schema_version"] != PLAN_SCHEMA:
        raise ControlError("FW-E seal plan schema_version 不匹配")
    persona = validate_persona(plan["persona"])
    campaign_id = expect_safe_id(plan["campaign_id"], "campaign_id")
    target_version = expect_string(plan["target_version"], "target_version")
    platforms = expect_string_list(plan["platforms"], "platforms")
    entrypoints = expect_string_list(plan["entrypoints"], "entrypoints")
    conditions = expect_string_list(plan["default_conditions"], "default_conditions")
    traffic_policy = _validate_traffic_observation_policy(
        plan["traffic_observation_policy"]
    )
    if "privacy=essential-traffic" not in conditions:
        raise ControlError("FW-E Claude 证据必须冻结 privacy=essential-traffic")
    created_at = expect_rfc3339(plan["created_at_utc"], "created_at_utc")
    discovered_at = expect_rfc3339(plan["discovered_at_utc"], "discovered_at_utc")
    inventory_at = expect_rfc3339(
        plan["inventory_observed_at_utc"], "inventory_observed_at_utc"
    )
    source_commit = expect_string(plan["source_commit"], "source_commit")
    if len(source_commit) != 40 or any(ch not in "0123456789abcdef" for ch in source_commit):
        raise ControlError("source_commit 必须是完整小写 Git commit")

    contract_sources = external_bindings(
        external_root, plan["contract_source_paths"], "contract_source_paths"
    )
    fw_c_receipts = external_bindings(
        external_root, plan["fw_c_receipt_paths"], "fw_c_receipt_paths"
    )
    runtime_catalog = external_bindings(
        external_root, plan["runtime_catalog_paths"], "runtime_catalog_paths"
    )
    official_artifacts = external_bindings(
        external_root, plan["official_artifact_paths"], "official_artifact_paths"
    )
    completeness = _validate_completeness_artifacts(
        external_root,
        target_version=target_version,
        inventory_paths=plan["target_sink_inventory_paths"],
        matrix_path=plan["cross_source_matrix_path"],
        closure_path=plan["completeness_closure_path"],
        capture_index_path=plan["capture_index_path"],
    )
    inventory_bindings = external_bindings(
        external_root, plan["inventory_evidence_paths"], "inventory_evidence_paths"
    )
    rules = _validate_rules(
        plan["rules"], completeness["expected_rule_ids"], external_root
    )
    aliases = _validate_aliases(plan["ingress_aliases"])
    ingress_entries = _validate_ingress_entries(plan["ingress_entries"])
    observed_egresses = _validate_observed_egresses(plan["egress_observed"])
    egress_entries = _validate_egress_entries(plan["egress_entries"])
    proposals = _validate_target_proposals(plan["target_proposals"])

    tool_sha256 = control_tool_bundle_sha256()
    bootstrap_ref = store.seal_object(
        "bootstrap",
        {
            "schema_version": "official-client-control-bootstrap/v1",
            "source_commit": source_commit,
            "contract_sources": contract_sources,
            "contract_bundle_sha256": canonical_sha256(contract_sources),
            "fw_c_receipts": fw_c_receipts,
            "runtime_catalog": runtime_catalog,
            "tool_bundle_sha256": tool_sha256,
            "result": "stable",
        },
    )
    campaign = {
        "schema_version": "official-client-control-campaign/v1",
        "campaign_id": campaign_id,
        "persona": persona,
        "target_version": target_version,
        "official_artifacts": official_artifacts,
        "platforms": platforms,
        "entrypoints": entrypoints,
        "default_conditions": conditions,
        "tool_bundle_sha256": tool_sha256,
        "bootstrap_ref": bootstrap_ref,
        "created_at_utc": created_at,
        "identity_sha256": "",
    }
    campaign["identity_sha256"] = campaign_identity_sha256(campaign)
    store.create_campaign(campaign)
    discovery_ref = store.append_fact(
        campaign_id,
        "discovery_recorded",
        {
            "version": target_version,
            "source": expect_string(plan["discovery_source"], "discovery_source"),
            "discovered_at_utc": discovered_at,
            "tool_sha256": tool_sha256,
            "artifact_refs": official_artifacts,
        },
        discovered_at,
    )

    inventory_evidence_ref = _seal_evidence_manifest(
        store,
        persona,
        "fw-e-inventory-source-to-sink",
        inventory_bindings,
        "FW-E 当前生产入口与 OAuth 出站只读盘点",
    )
    traffic_policy_ref = store.seal_object(
        "operational_evidence",
        {
            "schema_version": "official-client-operational-evidence/v1",
            "persona": persona,
            "manifest_id": "fw-e-traffic-observation-policy",
            "entries": [
                {
                    "id": "essential",
                    "facts": {
                        "disposition": "strict_wire_pair_scope",
                        "traffic_presence_comparison": traffic_policy[
                            "traffic_presence_comparison"
                        ],
                        "pair_requirement": "required_for_invoked_strict_scenario",
                    },
                },
                {
                    "id": "nonessential",
                    "facts": {
                        "disposition": "record_only",
                        "official_control": "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                        "implementation_gate": "isEssentialTrafficOnly()",
                        "traffic_presence_comparison": traffic_policy[
                            "traffic_presence_comparison"
                        ],
                        "absence_policy": traffic_policy[
                            "absence_of_record_only_traffic"
                        ],
                    },
                },
                {
                    "id": "telemetry",
                    "facts": {
                        "disposition": "record_only",
                        "official_control": "DISABLE_TELEMETRY",
                        "implementation_gate": "isAnalyticsDisabled()",
                        "traffic_presence_comparison": traffic_policy[
                            "traffic_presence_comparison"
                        ],
                        "absence_policy": traffic_policy[
                            "absence_of_record_only_traffic"
                        ],
                    },
                },
            ],
        },
    )
    completeness_ref = _seal_evidence_manifest(
        store,
        persona,
        "fw-e-target-sink-cross-source-closure",
        [
            *completeness["inventory_bindings"],
            completeness["matrix_binding"],
            completeness["closure_binding"],
            completeness["capture_binding"],
        ],
        "FW-E 目标 sink、历史资料、全 host/path 运行观测和规则闭集",
    )
    proposal_ref = store.seal_object(
        "operational_evidence",
        {
            "schema_version": "official-client-operational-evidence/v1",
            "persona": persona,
            "manifest_id": "fw-e-unapproved-target-dispositions",
            "entries": [
                {
                    "id": proposal["id"],
                    "facts": {
                        "approval_state": "unapproved_proposal",
                        "kind": proposal["kind"],
                        "target_disposition": proposal["target_disposition"],
                        "rationale": proposal["rationale"],
                    },
                }
                for proposal in proposals
            ],
        },
    )
    ingress_observation_ref = store.seal_object(
        "ingress_observation",
        {
            "schema_version": "official-client-ingress-observation/v1",
            "persona": persona,
            "observed_at_utc": inventory_at,
            "source_refs": [inventory_evidence_ref],
            "aliases": aliases,
        },
    )
    ingress_inventory_ref = store.seal_object(
        "production_ingress_inventory",
        {
            "schema_version": "official-client-production-ingress-inventory/v1",
            "persona": persona,
            "observation_ref": ingress_observation_ref,
            "entries": [
                {**entry, "evidence_refs": [inventory_evidence_ref]}
                for entry in ingress_entries
            ],
        },
    )
    egress_observation_ref = store.seal_object(
        "egress_observation",
        {
            "schema_version": "official-client-egress-observation/v1",
            "persona": persona,
            "observed_at_utc": inventory_at,
            "source_refs": [inventory_evidence_ref],
            "egresses": observed_egresses,
        },
    )
    egress_inventory_ref = store.seal_object(
        "egress_disposition_inventory",
        {
            "schema_version": "official-client-egress-disposition-inventory/v1",
            "persona": persona,
            "observation_ref": egress_observation_ref,
            "entries": [
                {**entry, "runtime_assertion_refs": [inventory_evidence_ref]}
                for entry in egress_entries
            ],
        },
    )
    evidence_package_ref = store.seal_object(
        "evidence_package",
        {
            "schema_version": "official-client-evidence-package/v2",
            "persona": persona,
            "version": target_version,
            "official_artifacts": official_artifacts,
            "platforms": platforms,
            "entrypoints": entrypoints,
            "default_conditions": conditions,
            "comparison_policy_ref": traffic_policy_ref,
            "completeness_ref": completeness_ref,
            "producer_tool_sha256": tool_sha256,
            "rules": rules,
        },
    )
    evidence_fact_ref = store.append_fact(
        campaign_id,
        "evidence_recorded",
        {
            "discovery_fact_ref": discovery_ref,
            "evidence_package_ref": evidence_package_ref,
        },
        inventory_at,
    )
    status = __import__(
        "tools.official_client_control.gates", fromlist=["WorkflowGates"]
    ).WorkflowGates(store).status(campaign_id)
    if status["checkpoint"] != "evidence_recorded":
        raise ControlError("FW-E 封存后没有停在 evidence_recorded")
    if any(
        fact["fact_kind"] in {"evidence_approved", "profile_approved"}
        for fact in store.list_facts(campaign_id)
    ):
        raise ControlError("FW-E 不得自行签发 Evidence 或 Profile 批准")
    return {
        "schema_version": "official-client-fw-e-seal-result/v1",
        "campaign_id": campaign_id,
        "campaign_identity_sha256": campaign["identity_sha256"],
        "bootstrap_ref": bootstrap_ref,
        "discovery_fact_ref": discovery_ref,
        "evidence_package_ref": evidence_package_ref,
        "traffic_observation_policy_ref": traffic_policy_ref,
        "completeness_ref": completeness_ref,
        "evidence_fact_ref": evidence_fact_ref,
        "production_ingress_inventory_ref": ingress_inventory_ref,
        "egress_disposition_inventory_ref": egress_inventory_ref,
        "target_disposition_proposal_ref": proposal_ref,
        "tool_bundle_sha256": tool_sha256,
        "rule_count": len(rules),
        "target_sink_count": completeness["target_sink_count"],
        "runtime_observation_count": completeness["runtime_observation_count"],
        "checkpoint": status["checkpoint"],
        "approval_state": "awaiting_explicit_evidence_approval",
    }
