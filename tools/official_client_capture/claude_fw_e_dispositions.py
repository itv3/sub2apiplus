#!/usr/bin/env python3
"""依据冻结的人工策略，生成 Claude FW-E 四方矩阵逐项 disposition。

本工具只把已经有证据的判断签发为闭合处置；其余项会逐项展开为 ``unclassified``，
并同时生成精确补证清单。禁止用“其余全部范围外”一类兜底规则清零闭集。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_sha256,
    load_json_file,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[2]
POLICY_SCHEMA = "claude-code-fw-e-disposition-review-policy/v1"
DISPOSITIONS_SCHEMA = "claude-code-fw-e-cross-source-dispositions/v1"
REVIEW_SCHEMA = "claude-code-fw-e-explicit-disposition-review/v1"
BLOCKERS_SCHEMA = "claude-code-fw-e-disposition-blockers/v1"
TARGET_INVENTORY_SCHEMA = "claude-code-target-sink-inventory/v1"
CONTAINMENT_SCHEMA = "claude-code-fw-e-sink-containment-evidence/v1"
CAPTURE_SCHEMA = "claude-code-fw-e-capture-index/v1"
NON_TRAFFIC_FINDINGS = {
    "capability_check",
    "declaration_name_or_body",
    "non_call_property_reference",
    "non_executable_literal",
}
INPUT_KEYS = {
    "source_2_1_88",
    "hitcc_2_1_197",
    "historical_rules",
    "historical_sink_inventory",
    "target_sink_inventory",
    "sink_containment",
    "runtime_capture_index",
}
GUIDE_PATH = ROOT / "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md"


class DispositionError(RuntimeError):
    """表示 disposition 策略、证据或闭集分母不一致。"""


def pretty_json_bytes(value: Any) -> bytes:
    """生成可读、确定性的 JSON 字节。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def write_once(path: Path, content: bytes) -> None:
    """以 O_EXCL 写入私有证据，禁止覆盖已有结果。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def evidence_path(path: Path) -> str:
    """优先返回仓库内相对路径，便于后继收据复算。"""

    resolved = path.resolve()
    if resolved.is_relative_to(ROOT):
        return resolved.relative_to(ROOT).as_posix()
    return resolved.as_posix()


def resolve_binding_path(raw: str) -> Path:
    """解析证据内声明的仓库相对路径。"""

    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def require_object(value: Any, label: str) -> dict[str, Any]:
    """要求值为 JSON 对象。"""

    if not isinstance(value, dict):
        raise DispositionError(f"{label} 必须是对象")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    """要求值为 JSON 数组。"""

    if not isinstance(value, list):
        raise DispositionError(f"{label} 必须是数组")
    return value


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    """拒绝策略字段缺失或静默扩展。"""

    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise DispositionError(f"{label} 字段不闭合：missing={missing} extra={extra}")


def require_sorted_unique(values: Any, label: str) -> list[str]:
    """读取严格排序且不重复的非空字符串数组。"""

    rows = require_list(values, label)
    result = [str(item) for item in rows]
    if any(not isinstance(item, str) or not item for item in rows):
        raise DispositionError(f"{label} 必须是非空字符串数组")
    if result != sorted(set(result)):
        raise DispositionError(f"{label} 必须严格排序且不得重复")
    return result


def flatten_rules(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取历史规则台账的三个稳定分区。"""

    rows: list[dict[str, Any]] = []
    for key in ("rules", "replacement_rules", "additional_rules"):
        rows.extend(require_list(ledger.get(key), f"historical_rules.{key}"))
    identities = [str(row.get("id")) for row in rows if isinstance(row, dict)]
    if len(identities) != len(rows) or len(set(identities)) != len(identities):
        raise DispositionError("历史规则 ID 缺失或重复")
    return rows


def input_binding(path: Path) -> dict[str, Any]:
    """生成普通输入文件的不可变绑定。"""

    if path.is_symlink() or not path.is_file():
        raise DispositionError(f"输入不是可信普通文件：{path}")
    return {
        "path": evidence_path(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def verify_policy_bindings(
    policy: dict[str, Any], paths: dict[str, Path]
) -> dict[str, dict[str, Any]]:
    """要求人工策略精确绑定本轮七类输入。"""

    expected = require_object(policy.get("input_sha256"), "policy.input_sha256")
    if set(expected) != INPUT_KEYS or set(paths) != INPUT_KEYS:
        raise DispositionError("策略或命令行输入集合不闭合")
    bindings = {key: input_binding(path) for key, path in paths.items()}
    for key in sorted(INPUT_KEYS):
        if expected.get(key) != bindings[key]["sha256"]:
            raise DispositionError(f"策略绑定的 {key} 摘要与实际输入不一致")
    return bindings


def evidence_rows(
    target: dict[str, Any],
    containment: dict[str, Any],
    target_inventory_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """校验目标 inventory 与 AST 包含证据的一一对应关系。"""

    if target.get("schema_version") != TARGET_INVENTORY_SCHEMA:
        raise DispositionError("目标 sink inventory schema 不匹配")
    completeness = target.get("completeness")
    sinks = require_list(target.get("sinks"), "target.sinks")
    if (
        not isinstance(completeness, dict)
        or completeness.get("truncated") is not False
        or target.get("sink_total") != len(sinks)
    ):
        raise DispositionError("目标 sink inventory 不完整")
    if containment.get("schema_version") != CONTAINMENT_SCHEMA:
        raise DispositionError("sink containment schema 不匹配")
    containment_status = containment.get("completeness")
    rows = require_list(containment.get("evidence"), "containment.evidence")
    if (
        not isinstance(containment_status, dict)
        or containment_status.get("result") != "passed"
        or containment_status.get("target_sink_count") != len(sinks)
        or containment_status.get("evidence_row_count") != len(rows)
    ):
        raise DispositionError("sink containment 没有完整覆盖目标分母")
    target_by_id = {str(row.get("sink_id")): row for row in sinks}
    evidence_by_id = {str(row.get("sink_id")): row for row in rows}
    if (
        len(target_by_id) != len(sinks)
        or len(evidence_by_id) != len(rows)
        or set(target_by_id) != set(evidence_by_id)
    ):
        raise DispositionError("目标 sink 与 containment 身份集合不一致")
    for sink_id, sink in target_by_id.items():
        evidence = evidence_by_id[sink_id]
        if evidence.get("semantic_sha256") != sink.get("semantic_sha256"):
            raise DispositionError(f"目标 sink 语义摘要漂移：{sink_id}")
    inventory_binding = require_object(containment.get("inventory"), "containment.inventory")
    if inventory_binding.get("sha256") != target_inventory_sha256:
        raise DispositionError("containment 绑定的目标 inventory 摘要不一致")
    return target_by_id, evidence_by_id


def add_unique_policy_row(
    output: dict[str, dict[str, Any]],
    identity: str,
    row: dict[str, Any],
    label: str,
) -> None:
    """把人工策略项加入分组，并拒绝组内重复身份。"""

    if not identity or identity == "None":
        raise DispositionError(f"{label} 身份缺失")
    if identity in output:
        raise DispositionError(f"{label} 重复：{identity}")
    output[identity] = row


def verify_containment_bundle(containment: dict[str, Any]) -> str:
    """复核 containment 绑定的目标 bundle，并返回其源码文本。"""

    bundle = require_object(containment.get("bundle"), "containment.bundle")
    raw_path = bundle.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise DispositionError("containment.bundle.path 非法")
    path = resolve_binding_path(raw_path)
    if sha256_file(path) != bundle.get("sha256"):
        raise DispositionError("containment 绑定的目标 bundle 摘要漂移")
    content = path.read_bytes()
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DispositionError("目标 bundle 不是完整 UTF-8") from error
    if len(content) != bundle.get("byte_count"):
        raise DispositionError("目标 bundle 字节数漂移")
    return source


def policy_target_maps(
    policy: dict[str, Any],
    known_specs: set[str],
    target_ids: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
    bundle_source: str,
    available_scenarios: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """读取 strict、managed、record-only 三类人工闭合判断。"""

    spec_sets_raw = require_object(policy.get("spec_sets"), "policy.spec_sets")
    spec_sets: dict[str, list[str]] = {}
    for name, raw in spec_sets_raw.items():
        specs = require_sorted_unique(raw, f"policy.spec_sets.{name}")
        unknown = sorted(set(specs) - known_specs)
        if unknown:
            raise DispositionError(f"策略 spec_set 引用未知规则：{name}={unknown}")
        spec_sets[name] = specs

    target_policy = require_object(policy.get("target_sinks"), "policy.target_sinks")
    require_exact_keys(
        target_policy,
        {"mapped_strict", "mapped_managed", "record_only_disabled"},
        "policy.target_sinks",
    )
    strict: dict[str, dict[str, Any]] = {}
    for raw in require_list(target_policy["mapped_strict"], "mapped_strict"):
        row = require_object(raw, "mapped_strict row")
        require_exact_keys(
            row,
            {"sink_id", "spec_set", "scenario_ids", "migration_decision", "rationale"},
            "mapped_strict row",
        )
        sink_id = str(row["sink_id"])
        spec_set = str(row["spec_set"])
        scenarios = require_sorted_unique(row["scenario_ids"], f"{sink_id}.scenario_ids")
        if spec_set not in spec_sets:
            raise DispositionError(f"strict sink 引用未知 spec_set：{sink_id}")
        unknown_scenarios = sorted(set(scenarios) - available_scenarios)
        if unknown_scenarios:
            raise DispositionError(
                f"strict sink 引用 Campaign 外场景：{sink_id}={unknown_scenarios}"
            )
        add_unique_policy_row(
            strict,
            sink_id,
            {**row, "spec_ids": spec_sets[spec_set]},
            "mapped_strict sink_id",
        )

    managed: dict[str, dict[str, Any]] = {}
    for raw in require_list(target_policy["mapped_managed"], "mapped_managed"):
        row = require_object(raw, "mapped_managed row")
        require_exact_keys(
            row,
            {"sink_id", "migration_decision", "rationale"},
            "mapped_managed row",
        )
        add_unique_policy_row(
            managed,
            str(row["sink_id"]),
            row,
            "mapped_managed sink_id",
        )

    record_only: dict[str, dict[str, Any]] = {}
    for raw in require_list(
        target_policy["record_only_disabled"], "record_only_disabled"
    ):
        row = require_object(raw, "record_only_disabled row")
        require_exact_keys(
            row,
            {
                "sink_id",
                "traffic_class",
                "required_gate_symbols",
                "migration_decision",
                "rationale",
            },
            "record_only_disabled row",
        )
        sink_id = str(row["sink_id"])
        gates = require_sorted_unique(
            row["required_gate_symbols"], f"{sink_id}.required_gate_symbols"
        )
        if row["traffic_class"] not in {"nonessential", "telemetry"}:
            raise DispositionError(f"record_only 流量类别非法：{sink_id}")
        evidence = evidence_by_id.get(sink_id)
        function = evidence.get("nearest_function") if isinstance(evidence, dict) else None
        if not isinstance(function, dict):
            raise DispositionError(f"record_only sink 缺少函数边界：{sink_id}")
        start = function.get("start")
        end = function.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise DispositionError(f"record_only sink 函数坐标非法：{sink_id}")
        function_source = bundle_source[start:end]
        missing_gates = [gate for gate in gates if gate not in function_source]
        if missing_gates:
            raise DispositionError(f"record_only sink 缺少直接 gate：{sink_id}={missing_gates}")
        add_unique_policy_row(
            record_only,
            sink_id,
            row,
            "record_only_disabled sink_id",
        )

    groups = {"mapped_strict": strict, "mapped_managed": managed, "record_only": record_only}
    seen: dict[str, str] = {}
    for group_name, rows in groups.items():
        for sink_id in rows:
            if sink_id in seen:
                raise DispositionError(
                    f"目标 sink 被多个策略组重复处置：{sink_id}={seen[sink_id]},{group_name}"
                )
            seen[sink_id] = group_name
            if sink_id not in target_ids:
                raise DispositionError(f"策略引用未知目标 sink：{sink_id}")
    return strict, managed, record_only


def build_target_dispositions(
    target_by_id: dict[str, dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
    strict: dict[str, dict[str, Any]],
    managed: dict[str, dict[str, Any]],
    record_only: dict[str, dict[str, Any]],
    target_path: str,
    containment_path: str,
    capture_path: str,
    runtime_sink_ids: set[str],
) -> list[dict[str, Any]]:
    """对目标分母的每个 sink 恰好签发一个 disposition。"""

    output: list[dict[str, Any]] = []
    for sink_id in sorted(target_by_id):
        sink = target_by_id[sink_id]
        evidence = evidence_by_id[sink_id]
        common_evidence = [containment_path, target_path]
        if sink_id in strict:
            policy = strict[sink_id]
            row = {
                "sink_id": sink_id,
                "traffic_class": "essential",
                "disposition": "mapped_strict",
                "rationale": policy["rationale"],
                "spec_ids": policy["spec_ids"],
                "scenario_ids": policy["scenario_ids"],
                "evidence_paths": sorted(common_evidence + [capture_path]),
                "migration_decision": policy["migration_decision"],
            }
        elif sink_id in managed:
            policy = managed[sink_id]
            evidence_paths = common_evidence + [evidence_path(GUIDE_PATH)]
            if sink_id in runtime_sink_ids:
                evidence_paths.append(capture_path)
            row = {
                "sink_id": sink_id,
                "traffic_class": "essential",
                "disposition": "mapped_managed",
                "rationale": policy["rationale"],
                "spec_ids": [],
                "scenario_ids": [],
                "evidence_paths": sorted(set(evidence_paths)),
                "migration_decision": policy["migration_decision"],
            }
        elif sink_id in record_only:
            policy = record_only[sink_id]
            row = {
                "sink_id": sink_id,
                "traffic_class": policy["traffic_class"],
                "disposition": "record_only_disabled",
                "rationale": policy["rationale"],
                "spec_ids": [],
                "scenario_ids": [],
                "evidence_paths": sorted(common_evidence + [capture_path]),
                "migration_decision": policy["migration_decision"],
            }
        elif sink.get("source_kind") == "lexical_only":
            finding = evidence.get("structural_finding")
            if finding not in NON_TRAFFIC_FINDINGS:
                raise DispositionError(
                    f"词法候选没有可排除的结构证据：{sink_id}={finding}"
                )
            row = {
                "sink_id": sink_id,
                "traffic_class": "not_traffic",
                "disposition": "out_of_scope_proven",
                "rationale": (
                    f"AST containment 将该词法命中唯一判为 {finding}；它不是独立发送调用点，"
                    "实际调用仍由 AST sink 分母单独覆盖。"
                ),
                "spec_ids": [],
                "scenario_ids": [],
                "evidence_paths": sorted(common_evidence),
                "migration_decision": "change",
            }
        else:
            row = {
                "sink_id": sink_id,
                "traffic_class": "unknown",
                "disposition": "unclassified",
                "rationale": (
                    f"已逐项确认这是 {sink.get('category')} 精确 AST 调用，但当前证据尚不能唯一证明"
                    "其 strict／managed／record-only／范围外归属。"
                ),
                "spec_ids": [],
                "scenario_ids": [],
                "evidence_paths": sorted(common_evidence),
                "migration_decision": "change",
            }
        output.append(row)
    return output


def historical_policy_ids(policy: dict[str, Any], group: str) -> set[str]:
    """读取历史来源中经人工定性的 managed ID。"""

    section = require_object(policy.get(group), f"policy.{group}")
    require_exact_keys(section, {"mapped_managed"}, f"policy.{group}")
    return set(
        require_sorted_unique(
            section["mapped_managed"], f"policy.{group}.mapped_managed"
        )
    )


def build_historical_dispositions(
    source: dict[str, Any],
    known_specs: set[str],
    managed_ids: set[str],
    source_path: str,
    ledger_path: str,
) -> list[dict[str, Any]]:
    """把 2.1.88 每个候选显式展开，混有未知 CAND 的行保持阻塞。"""

    rules = require_list(source.get("rules"), "source_2_1_88.rules")
    identities = {str(row.get("source_rule_id")) for row in rules}
    unknown_managed = sorted(managed_ids - identities)
    if unknown_managed:
        raise DispositionError(f"策略引用未知 2.1.88 候选：{unknown_managed}")
    output: list[dict[str, Any]] = []
    for raw in sorted(rules, key=lambda row: str(row.get("source_rule_id"))):
        source_id = str(raw.get("source_rule_id"))
        refs = sorted(set(str(item) for item in raw.get("spec_rule_ids", [])))
        unknown_refs = sorted(set(refs) - known_specs)
        if source_id in managed_ids:
            disposition = "mapped_managed"
            spec_ids: list[str] = []
            rationale = "该历史命题属于已定性的 count_tokens／OAuth 非 Persona 受管辅助路径。"
            evidence = [evidence_path(GUIDE_PATH), source_path]
        elif raw.get("target_static_status") == "out_of_scope":
            disposition = "out_of_scope_proven"
            spec_ids = []
            rationale = "2.1.88 台账已给出明确范围外处置，本轮逐项复核后保留该结论。"
            evidence = [source_path]
        elif refs and not unknown_refs:
            disposition = "mapped_historical"
            spec_ids = refs
            rationale = "该原子命题的全部引用均能唯一映射到冻结的历史 SPEC。"
            evidence = [ledger_path, source_path]
        else:
            disposition = "unclassified"
            spec_ids = []
            if unknown_refs:
                rationale = f"仍引用未编号候选 {', '.join(unknown_refs)}，不能伪装成已闭合历史规则。"
            else:
                rationale = "尚无目标版本 SPEC／场景能够唯一承担该历史命题。"
            evidence = [source_path]
        output.append(
            {
                "source_rule_id": source_id,
                "disposition": disposition,
                "spec_ids": spec_ids,
                "rationale": rationale,
                "evidence_paths": sorted(set(evidence)),
            }
        )
    return output


def build_hitcc_dispositions(
    hitcc: dict[str, Any],
    known_specs: set[str],
    managed_ids: set[str],
    hitcc_path: str,
    ledger_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """逐项处置 HitCC 原子线索及每篇 clue_source 文档。"""

    clues = require_list(hitcc.get("clues"), "hitcc.clues")
    identities = {str(row.get("clue_id")) for row in clues}
    unknown_managed = sorted(managed_ids - identities)
    if unknown_managed:
        raise DispositionError(f"策略引用未知 HitCC 线索：{unknown_managed}")
    clue_output: list[dict[str, Any]] = []
    for raw in sorted(clues, key=lambda row: str(row.get("clue_id"))):
        clue_id = str(raw.get("clue_id"))
        refs = sorted(set(str(item) for item in raw.get("spec_rule_ids", [])))
        unknown_refs = sorted(set(refs) - known_specs)
        if clue_id in managed_ids:
            disposition = "mapped_managed"
            spec_ids: list[str] = []
            rationale = "该线索是已定性的 count_tokens 非 Persona 受管辅助出站。"
            evidence = [evidence_path(GUIDE_PATH), hitcc_path]
        elif raw.get("coverage") == "out_of_scope":
            disposition = "out_of_scope_proven"
            spec_ids = []
            rationale = "HitCC 台账已明确将该原子线索判为范围外。"
            evidence = [hitcc_path]
        elif raw.get("coverage") == "covered" and refs and not unknown_refs:
            disposition = "mapped_historical"
            spec_ids = refs
            rationale = "该 covered 线索的全部引用均能唯一映射到冻结的历史 SPEC。"
            evidence = [hitcc_path, ledger_path]
        else:
            disposition = "unclassified"
            spec_ids = []
            if unknown_refs:
                rationale = f"仍引用未编号候选 {', '.join(unknown_refs)}，需要目标版本补证或新 SPEC。"
            else:
                rationale = "线索仍为 partial／missing，或没有可用的目标 SPEC 映射。"
            evidence = [hitcc_path]
        clue_output.append(
            {
                "clue_id": clue_id,
                "disposition": disposition,
                "spec_ids": spec_ids,
                "rationale": rationale,
                "evidence_paths": sorted(set(evidence)),
            }
        )

    clue_by_id = {row["clue_id"]: row for row in clue_output}
    documents = require_list(hitcc.get("document_inventory"), "hitcc.document_inventory")
    document_output: list[dict[str, Any]] = []
    for raw in sorted(documents, key=lambda row: str(row.get("path"))):
        if raw.get("disposition") != "clue_source":
            continue
        path = str(raw.get("path"))
        clue_ids = sorted(set(str(item) for item in raw.get("clue_ids", [])))
        mapped = [clue_by_id[item] for item in clue_ids if item in clue_by_id]
        unknown_clues = sorted(set(clue_ids) - set(clue_by_id))
        dispositions = {row["disposition"] for row in mapped}
        refs = sorted({spec for row in mapped for spec in row["spec_ids"]})
        if (
            raw.get("mapping_status") == "mapped"
            and mapped
            and not unknown_clues
            and dispositions == {"mapped_historical"}
            and refs
        ):
            disposition = "mapped_historical"
            spec_ids = refs
            rationale = "文档全部 clue_id 均已闭合到冻结的历史 SPEC。"
        elif (
            raw.get("mapping_status") == "mapped"
            and mapped
            and not unknown_clues
            and dispositions == {"mapped_managed"}
        ):
            disposition = "mapped_managed"
            spec_ids = []
            rationale = "文档全部 clue_id 均属于已定性的非 Persona 受管辅助路径。"
        elif mapped and not unknown_clues and dispositions == {"out_of_scope_proven"}:
            disposition = "out_of_scope_proven"
            spec_ids = []
            rationale = "文档全部原子线索均已有可复算的范围外处置。"
        else:
            disposition = "unclassified"
            spec_ids = []
            rationale = "文档仍含未原子化、未映射或未闭合线索，不能按文件整体推断范围外。"
        evidence = [hitcc_path]
        document_path = resolve_binding_path(path)
        if document_path.is_file() and not document_path.is_symlink():
            evidence.append(evidence_path(document_path))
        document_output.append(
            {
                "path": path,
                "disposition": disposition,
                "spec_ids": spec_ids,
                "rationale": rationale,
                "evidence_paths": sorted(set(evidence)),
            }
        )
    return clue_output, document_output


def build_runtime_dispositions(
    policy: dict[str, Any],
    capture: dict[str, Any],
    target_ids: set[str],
    capture_path: str,
    containment_path: str,
) -> list[dict[str, Any]]:
    """要求每个全 host/path 运行坐标都在人工策略中恰好出现一次。"""

    rows = require_list(policy.get("runtime_observations"), "policy.runtime_observations")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = require_object(raw, "runtime observation policy")
        require_exact_keys(
            row,
            {"observation_id", "sink_ids", "rationale"},
            "runtime observation policy",
        )
        observation_id = str(row["observation_id"])
        if observation_id in by_id:
            raise DispositionError(f"运行观测策略重复：{observation_id}")
        sink_ids = require_sorted_unique(row["sink_ids"], f"{observation_id}.sink_ids")
        unknown_sinks = sorted(set(sink_ids) - target_ids)
        if unknown_sinks:
            raise DispositionError(f"运行观测引用未知 sink：{observation_id}={unknown_sinks}")
        by_id[observation_id] = {**row, "sink_ids": sink_ids}
    target = require_object(capture.get("target"), "capture.target")
    observations = require_list(target.get("network_observations"), "capture observations")
    expected_ids = {str(row.get("observation_id")) for row in observations}
    if set(by_id) != expected_ids:
        raise DispositionError(
            "运行观测策略未闭合集合："
            f"missing={sorted(expected_ids - set(by_id))} extra={sorted(set(by_id) - expected_ids)}"
        )
    return [
        {
            "observation_id": observation_id,
            "disposition": "mapped_sink",
            "sink_ids": by_id[observation_id]["sink_ids"],
            "rationale": by_id[observation_id]["rationale"],
            "evidence_paths": sorted({capture_path, containment_path}),
        }
        for observation_id in sorted(by_id)
    ]


def require_capture(capture: dict[str, Any], target_version: str) -> set[str]:
    """复核 R 已补齐且隐私模式与目标身份冻结。"""

    if capture.get("schema_version") != CAPTURE_SCHEMA or capture.get("result") != "passed":
        raise DispositionError("runtime capture index 未通过")
    if capture.get("target_version") != target_version:
        raise DispositionError("runtime capture index 目标版本不一致")
    channels = capture.get("channels")
    if not isinstance(channels, list) or not {"P", "R", "J", "M"}.issubset(set(channels)):
        raise DispositionError("runtime capture index 缺少 P/R/J/M 通道")
    target = require_object(capture.get("target"), "capture.target")
    relay = require_object(capture.get("relay"), "capture.relay")
    relay_target = require_object(relay.get("target"), "capture.relay.target")
    scenarios = {str(item) for item in target.get("scenarios", [])}
    probe_ids = {str(item) for item in relay_target.get("probe_ids", [])}
    if (
        target.get("version") != target_version
        or target.get("capture_host_scopes") != ["all"]
        or relay.get("result") != "passed"
        or relay_target.get("version") != target_version
        or probe_ids != {f"r-{item}" for item in scenarios}
    ):
        raise DispositionError("目标全 host/path 或 relay R 场景没有闭合")
    for group_name in ("control", "target"):
        group = require_object(capture.get(group_name), f"capture.{group_name}")
        privacy = require_object(group.get("privacy_controls"), f"{group_name}.privacy")
        if privacy.get("result") != "passed" or privacy.get("required_values") != {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        }:
            raise DispositionError(f"{group_name} 没有证明隐私开关实际生效")
    return scenarios | probe_ids


def membership(value: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """把完整 disposition 展开成可审阅的精确 ID 集合。"""

    definitions = {
        "target_sinks": ("sink_id", value["target_sinks"]),
        "historical_source_candidates": (
            "source_rule_id",
            value["historical_source_candidates"],
        ),
        "hitcc_clues": ("clue_id", value["hitcc_clues"]),
        "hitcc_documents": ("path", value["hitcc_documents"]),
        "runtime_observations": ("observation_id", value["runtime_observations"]),
    }
    output: dict[str, dict[str, list[str]]] = {}
    for group, (identity_key, rows) in definitions.items():
        buckets: dict[str, list[str]] = {}
        for row in rows:
            buckets.setdefault(str(row["disposition"]), []).append(str(row[identity_key]))
        output[group] = {
            disposition: sorted(identities)
            for disposition, identities in sorted(buckets.items())
        }
    return output


def blocker_rows(dispositions: dict[str, Any]) -> list[dict[str, Any]]:
    """从显式处置中生成没有默认推断的补证队列。"""

    definitions = {
        "target_sinks": ("sink_id", dispositions["target_sinks"]),
        "historical_source_candidates": (
            "source_rule_id",
            dispositions["historical_source_candidates"],
        ),
        "hitcc_clues": ("clue_id", dispositions["hitcc_clues"]),
        "hitcc_documents": ("path", dispositions["hitcc_documents"]),
        "runtime_observations": ("observation_id", dispositions["runtime_observations"]),
    }
    rows = [
        {
            "group": group,
            "identity": str(row[identity_key]),
            "rationale": row["rationale"],
            "current_evidence_paths": row["evidence_paths"],
            "required_action": (
                "补目标版本 source-to-sink、适用条件、SPEC 与对应运行场景；"
                "在证据闭环前保持 unclassified。"
            ),
        }
        for group, (identity_key, group_rows) in definitions.items()
        for row in group_rows
        if row["disposition"] == "unclassified"
    ]
    return sorted(rows, key=lambda row: (row["group"], row["identity"]))


def build_dispositions(
    *,
    policy_path: Path,
    source_path: Path,
    hitcc_path: Path,
    ledger_path: Path,
    baseline_inventory_path: Path,
    target_inventory_path: Path,
    containment_path: Path,
    capture_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """构建完整 dispositions、显式覆盖报告和补证清单。"""

    policy = require_object(load_json_file(policy_path, "disposition policy"), "policy")
    require_exact_keys(
        policy,
        {
            "schema_version",
            "policy_id",
            "target_version",
            "input_sha256",
            "spec_sets",
            "target_sinks",
            "historical_source_candidates",
            "hitcc_clues",
            "runtime_observations",
        },
        "policy",
    )
    if policy.get("schema_version") != POLICY_SCHEMA:
        raise DispositionError("disposition policy schema 不匹配")
    target_version = str(policy.get("target_version"))
    paths = {
        "source_2_1_88": source_path,
        "hitcc_2_1_197": hitcc_path,
        "historical_rules": ledger_path,
        "historical_sink_inventory": baseline_inventory_path,
        "target_sink_inventory": target_inventory_path,
        "sink_containment": containment_path,
        "runtime_capture_index": capture_path,
    }
    bindings = verify_policy_bindings(policy, paths)

    source = require_object(load_json_file(source_path, "2.1.88 coverage"), "source")
    hitcc = require_object(load_json_file(hitcc_path, "HitCC coverage"), "hitcc")
    ledger = require_object(load_json_file(ledger_path, "historical rules"), "ledger")
    baseline_inventory = require_object(
        load_json_file(baseline_inventory_path, "historical sink inventory"),
        "baseline inventory",
    )
    target = require_object(
        load_json_file(target_inventory_path, "target sink inventory"), "target"
    )
    containment = require_object(
        load_json_file(containment_path, "sink containment"), "containment"
    )
    capture = require_object(load_json_file(capture_path, "capture index"), "capture")
    if (
        target.get("target_version") != target_version
        or containment.get("target_version") != target_version
        or capture.get("target_version") != target_version
        or baseline_inventory.get("target_version") != ledger.get("target_version")
    ):
        raise DispositionError("策略、目标证据或历史基线版本不一致")
    available_scenarios = require_capture(capture, target_version)
    target_by_id, evidence_by_id = evidence_rows(
        target,
        containment,
        bindings["target_sink_inventory"]["sha256"],
    )
    bundle_source = verify_containment_bundle(containment)
    historical_rules = flatten_rules(ledger)
    known_specs = {str(row["id"]) for row in historical_rules}
    strict, managed, record_only = policy_target_maps(
        policy,
        known_specs,
        set(target_by_id),
        evidence_by_id,
        bundle_source,
        available_scenarios,
    )
    runtime = build_runtime_dispositions(
        policy,
        capture,
        set(target_by_id),
        bindings["runtime_capture_index"]["path"],
        bindings["sink_containment"]["path"],
    )
    runtime_sink_ids = {sink_id for row in runtime for sink_id in row["sink_ids"]}
    target_rows = build_target_dispositions(
        target_by_id,
        evidence_by_id,
        strict,
        managed,
        record_only,
        bindings["target_sink_inventory"]["path"],
        bindings["sink_containment"]["path"],
        bindings["runtime_capture_index"]["path"],
        runtime_sink_ids,
    )
    historical_rows = build_historical_dispositions(
        source,
        known_specs,
        historical_policy_ids(policy, "historical_source_candidates"),
        bindings["source_2_1_88"]["path"],
        bindings["historical_rules"]["path"],
    )
    clue_rows, document_rows = build_hitcc_dispositions(
        hitcc,
        known_specs,
        historical_policy_ids(policy, "hitcc_clues"),
        bindings["hitcc_2_1_197"]["path"],
        bindings["historical_rules"]["path"],
    )
    dispositions = {
        "schema_version": DISPOSITIONS_SCHEMA,
        "target_version": target_version,
        "target_sinks": target_rows,
        "historical_source_candidates": historical_rows,
        "hitcc_clues": clue_rows,
        "hitcc_documents": document_rows,
        "runtime_observations": runtime,
    }
    groups = membership(dispositions)
    denominator_counts = {
        group: sum(len(identities) for identities in buckets.values())
        for group, buckets in groups.items()
    }
    expected_counts = {
        "target_sinks": len(target_by_id),
        "historical_source_candidates": len(source.get("rules", [])),
        "hitcc_clues": len(hitcc.get("clues", [])),
        "hitcc_documents": sum(
            1
            for row in hitcc.get("document_inventory", [])
            if row.get("disposition") == "clue_source"
        ),
        "runtime_observations": len(
            capture.get("target", {}).get("network_observations", [])
        ),
    }
    if denominator_counts != expected_counts:
        raise DispositionError(
            f"显式 disposition 没有闭合分母：actual={denominator_counts} expected={expected_counts}"
        )
    total = sum(denominator_counts.values())
    unclassified_counts = {
        group: len(buckets.get("unclassified", [])) for group, buckets in groups.items()
    }
    review = {
        "schema_version": REVIEW_SCHEMA,
        "policy_id": policy["policy_id"],
        "policy_binding": input_binding(policy_path),
        "target_version": target_version,
        "input_bindings": bindings,
        "denominator_counts": denominator_counts,
        "denominator_total": total,
        "explicit_disposition_counts": denominator_counts,
        "explicit_disposition_total": total,
        "unclassified_counts": unclassified_counts,
        "unclassified_total": sum(unclassified_counts.values()),
        "membership": groups,
        "membership_sha256": canonical_sha256(groups),
        "result": "passed",
    }
    blockers = blocker_rows(dispositions)
    blocker_report = {
        "schema_version": BLOCKERS_SCHEMA,
        "policy_id": policy["policy_id"],
        "target_version": target_version,
        "blocker_counts": dict(sorted(Counter(row["group"] for row in blockers).items())),
        "blocker_total": len(blockers),
        "items": blockers,
        "result": "blocked" if blockers else "passed",
    }
    return dispositions, review, blocker_report


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--source-2188", required=True, type=Path)
    parser.add_argument("--hitcc", required=True, type=Path)
    parser.add_argument("--baseline-ledger", required=True, type=Path)
    parser.add_argument("--baseline-inventory", required=True, type=Path)
    parser.add_argument("--target-inventory", required=True, type=Path)
    parser.add_argument("--containment", required=True, type=Path)
    parser.add_argument("--capture-index", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    return parser


def main() -> int:
    """生成不可覆盖的逐项 disposition 证据。"""

    arguments = build_parser().parse_args()
    if arguments.output_root.exists():
        print("失败：output-root 必须不存在，禁止覆盖", file=sys.stderr)
        return 1
    try:
        dispositions, review, blockers = build_dispositions(
            policy_path=arguments.policy,
            source_path=arguments.source_2188,
            hitcc_path=arguments.hitcc,
            ledger_path=arguments.baseline_ledger,
            baseline_inventory_path=arguments.baseline_inventory,
            target_inventory_path=arguments.target_inventory,
            containment_path=arguments.containment,
            capture_path=arguments.capture_index,
        )
        disposition_bytes = pretty_json_bytes(dispositions)
        disposition_sha256 = hashlib.sha256(disposition_bytes).hexdigest()
        review["dispositions_sha256"] = disposition_sha256
        blockers["dispositions_sha256"] = disposition_sha256
        write_once(arguments.output_root / "dispositions.json", disposition_bytes)
        write_once(arguments.output_root / "explicit-review.json", pretty_json_bytes(review))
        write_once(arguments.output_root / "blockers.json", pretty_json_bytes(blockers))
    except (DispositionError, OSError, ValueError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "完成："
        f"显式处置={review['explicit_disposition_total']}/{review['denominator_total']}，"
        f"未闭项={blockers['blocker_total']}，"
        f"显式覆盖={review['result']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
