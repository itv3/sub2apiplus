#!/usr/bin/env python3
"""生成 Claude FW-E 四方候选矩阵和目标 sink 闭集报告。

输入包括 2.1.88 真源码候选、HitCC 线索、历史官方规则、历史／目标 bundle 的 target-native
inventory，以及可选的人工 disposition。工具不会猜测未知语义：没有明确处置的目标 sink、历史候选
或 HitCC 线索会保持 ``unclassified`` 并阻止 ``--require-closed``。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_control.canonical import (  # noqa: E402
    canonical_sha256,
    load_json_file,
    sha256_file,
)


SCHEMA_INVENTORY = "claude-code-target-sink-inventory/v1"
SCHEMA_DISPOSITIONS = "claude-code-fw-e-cross-source-dispositions/v1"
SCHEMA_MATRIX = "claude-code-fw-e-cross-source-matrix/v1"
SCHEMA_CLOSURE = "claude-code-fw-e-completeness/v1"
ALLOWED_SINK_DISPOSITIONS = {
    "mapped_strict",
    "mapped_managed",
    "record_only_disabled",
    "out_of_scope_proven",
    "unclassified",
}
ALLOWED_TRAFFIC_CLASSES = {
    "essential",
    "nonessential",
    "telemetry",
    "not_traffic",
    "unknown",
}
ALLOWED_HISTORICAL_DISPOSITIONS = {
    "mapped_historical",
    "mapped_managed",
    "record_only_disabled",
    "out_of_scope_proven",
    "unclassified",
}
SHA256_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
REQUIRED_PRIVACY_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
}


class CrosswalkError(RuntimeError):
    """表示四方矩阵输入、处置或闭集不满足受管约束。"""


def utc_now() -> str:
    """返回秒精度 UTC 时间。"""

    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def write_private_json(path: Path, value: Any) -> None:
    """以禁止覆盖的方式写入私有 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def require_schema(value: dict[str, Any], expected: str, label: str) -> None:
    """校验输入 Schema。"""

    if value.get("schema_version") != expected:
        raise CrosswalkError(f"{label} schema 不匹配：{value.get('schema_version')}")


def flatten_baseline(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    """读取历史台账全部规则，不把数量固定成目标版本规则上限。"""

    rows: list[dict[str, Any]] = []
    for key in ("rules", "replacement_rules", "additional_rules"):
        value = ledger.get(key)
        if not isinstance(value, list):
            raise CrosswalkError(f"历史规则台账缺少 {key}")
        rows.extend(value)
    identities = [row.get("id") for row in rows if isinstance(row, dict)]
    if len(rows) != len(identities) or len(set(identities)) != len(identities):
        raise CrosswalkError("历史规则 ID 不唯一")
    return sorted(rows, key=lambda row: str(row["id"]))


def inventory_sinks(value: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """校验 AST 与词法并集 inventory 的完整 sink 数组。"""

    require_schema(value, SCHEMA_INVENTORY, label)
    completeness = value.get("completeness")
    if (
        not isinstance(completeness, dict)
        or completeness.get("truncated") is not False
        or completeness.get("ast_parse_diagnostic_count") != 0
        or completeness.get("ambiguous_lexical_match_count") != 0
        or completeness.get("duplicate_sink_id_count") != 0
    ):
        raise CrosswalkError(f"{label} sink inventory 被截断或缺少完整性声明")
    sinks = value.get("sinks")
    if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
        raise CrosswalkError(f"{label} sink_total 与实际数组不一致")
    identities = [row.get("sink_id") for row in sinks if isinstance(row, dict)]
    if len(identities) != len(sinks) or len(set(identities)) != len(identities):
        raise CrosswalkError(f"{label} sink_id 缺失或重复")
    for row in sinks:
        if not SHA256_RE.fullmatch(str(row.get("semantic_sha256", ""))):
            raise CrosswalkError(f"{label} sink 缺少语义摘要：{row.get('sink_id')}")
        if row.get("source_kind") not in {"ast_call", "lexical_only"}:
            raise CrosswalkError(f"{label} sink source_kind 非法：{row.get('sink_id')}")
    return sorted(sinks, key=lambda row: str(row["sink_id"]))


def load_dispositions(
    path: Path | None,
    target_version: str,
    target_ids: set[str],
) -> dict[str, dict[str, dict[str, Any]]]:
    """读取目标 sink 与两份历史资料的逐项人工处置。"""

    if path is None:
        return {
            "target_sinks": {},
            "historical_source_candidates": {},
            "hitcc_clues": {},
            "hitcc_documents": {},
            "runtime_observations": {},
        }
    value = load_json_file(path, "cross-source dispositions")
    require_schema(value, SCHEMA_DISPOSITIONS, "cross-source dispositions")
    if value.get("target_version") != target_version:
        raise CrosswalkError("cross-source dispositions 目标版本不匹配")
    expected_keys = {
        "schema_version",
        "target_version",
        "target_sinks",
        "historical_source_candidates",
        "hitcc_clues",
        "hitcc_documents",
        "runtime_observations",
    }
    if set(value) != expected_keys:
        raise CrosswalkError("cross-source dispositions 顶层字段不闭合")
    entries = value.get("target_sinks")
    if not isinstance(entries, list):
        raise CrosswalkError("cross-source dispositions.target_sinks 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("sink_id"), str):
            raise CrosswalkError("target-native disposition 条目非法")
        sink_id = entry["sink_id"]
        if sink_id in result:
            raise CrosswalkError(f"target-native disposition 重复：{sink_id}")
        if sink_id not in target_ids:
            raise CrosswalkError(f"target-native disposition 引用未知 sink：{sink_id}")
        disposition = entry.get("disposition")
        if disposition not in ALLOWED_SINK_DISPOSITIONS:
            raise CrosswalkError(f"target-native disposition 非法：{sink_id}={disposition}")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise CrosswalkError(f"target-native disposition 缺少理由：{sink_id}")
        spec_ids = entry.get("spec_ids", [])
        scenario_ids = entry.get("scenario_ids", [])
        evidence_paths = entry.get("evidence_paths", [])
        if not isinstance(spec_ids, list) or not all(
            isinstance(item, str) and item for item in spec_ids
        ):
            raise CrosswalkError(f"target-native disposition spec_ids 非法：{sink_id}")
        if not isinstance(scenario_ids, list) or not all(
            isinstance(item, str) and item for item in scenario_ids
        ):
            raise CrosswalkError(f"target-native disposition scenario_ids 非法：{sink_id}")
        if not isinstance(evidence_paths, list) or not all(
            isinstance(item, str) and item for item in evidence_paths
        ):
            raise CrosswalkError(f"target-native disposition evidence_paths 非法：{sink_id}")
        traffic_class = entry.get("traffic_class")
        if traffic_class not in ALLOWED_TRAFFIC_CLASSES:
            raise CrosswalkError(f"target-native traffic_class 非法：{sink_id}")
        if traffic_class == "unknown" and disposition != "unclassified":
            raise CrosswalkError(f"未知流量类别只能保持 unclassified：{sink_id}")
        if traffic_class == "not_traffic" and disposition != "out_of_scope_proven":
            raise CrosswalkError(f"not_traffic 只能用于已证明的非发送候选：{sink_id}")
        if traffic_class in {"nonessential", "telemetry"} and disposition not in {
            "record_only_disabled",
            "unclassified",
        }:
            raise CrosswalkError(f"关闭的非必要／遥测 sink 只能 record_only：{sink_id}")
        if disposition == "record_only_disabled" and traffic_class not in {
            "nonessential",
            "telemetry",
        }:
            raise CrosswalkError(f"record_only sink 的流量类别非法：{sink_id}")
        if disposition != "unclassified" and not evidence_paths:
            raise CrosswalkError(f"已处置 sink 必须绑定证据：{sink_id}")
        if disposition == "mapped_strict" and not spec_ids:
            raise CrosswalkError(f"strict sink 必须绑定规则：{sink_id}")
        if disposition == "mapped_strict" and not scenario_ids:
            raise CrosswalkError(f"strict sink 必须绑定运行场景：{sink_id}")
        if disposition in {"out_of_scope_proven", "unclassified"} and (
            spec_ids or scenario_ids
        ):
            raise CrosswalkError(f"范围外／未分类 sink 不得伪造规则或场景绑定：{sink_id}")
        migration_decision = entry.get("migration_decision", "change")
        if migration_decision not in {
            "inherit",
            "change",
            "add",
            "delete",
            "condition_change",
        }:
            raise CrosswalkError(f"sink migration_decision 非法：{sink_id}")
        if migration_decision == "delete":
            raise CrosswalkError(f"目标仍存在的 sink 不得声明 delete：{sink_id}")
        if migration_decision == "add":
            expected_entry_keys = {
                "sink_id",
                "traffic_class",
                "disposition",
                "rationale",
                "spec_ids",
                "scenario_ids",
                "evidence_paths",
                "migration_decision",
                "new_rule",
            }
            new_rule = entry.get("new_rule")
            if not isinstance(new_rule, dict):
                raise CrosswalkError(f"add sink 缺少 new_rule：{sink_id}")
            required = {"id", "domain", "retained_claim", "scope", "required_channels"}
            if set(new_rule) != required:
                raise CrosswalkError(f"add sink new_rule 字段不闭合：{sink_id}")
            if new_rule["id"] not in spec_ids:
                raise CrosswalkError(f"add sink new_rule.id 未进入 spec_ids：{sink_id}")
        else:
            expected_entry_keys = {
                "sink_id",
                "traffic_class",
                "disposition",
                "rationale",
                "spec_ids",
                "scenario_ids",
                "evidence_paths",
                "migration_decision",
            }
            if "new_rule" in entry:
                raise CrosswalkError(f"非 add sink 禁止携带 new_rule：{sink_id}")
        if set(entry) != expected_entry_keys:
            raise CrosswalkError(f"target-native disposition 字段不闭合：{sink_id}")
        result[sink_id] = entry

    def historical_entries(key: str, identity_key: str) -> dict[str, dict[str, Any]]:
        raw_entries = value.get(key)
        if not isinstance(raw_entries, list):
            raise CrosswalkError(f"cross-source dispositions.{key} 必须是数组")
        output: dict[str, dict[str, Any]] = {}
        required = {identity_key, "disposition", "spec_ids", "rationale", "evidence_paths"}
        for raw in raw_entries:
            if not isinstance(raw, dict) or set(raw) != required:
                raise CrosswalkError(f"{key} disposition 字段不闭合")
            identity = raw.get(identity_key)
            if not isinstance(identity, str) or not identity or identity in output:
                raise CrosswalkError(f"{key} disposition 身份缺失或重复：{identity}")
            disposition = raw.get("disposition")
            if disposition not in ALLOWED_HISTORICAL_DISPOSITIONS:
                raise CrosswalkError(f"{key} disposition 非法：{identity}")
            refs = raw.get("spec_ids")
            evidence = raw.get("evidence_paths")
            rationale = raw.get("rationale")
            if not isinstance(refs, list) or not all(isinstance(item, str) and item for item in refs):
                raise CrosswalkError(f"{key} spec_ids 非法：{identity}")
            if not isinstance(evidence, list) or not all(
                isinstance(item, str) and item for item in evidence
            ):
                raise CrosswalkError(f"{key} evidence_paths 非法：{identity}")
            if not isinstance(rationale, str) or not rationale.strip():
                raise CrosswalkError(f"{key} rationale 缺失：{identity}")
            if disposition == "mapped_historical" and not refs:
                raise CrosswalkError(f"映射历史项必须绑定规则：{identity}")
            if disposition in {
                "mapped_managed",
                "record_only_disabled",
                "out_of_scope_proven",
                "unclassified",
            } and refs:
                raise CrosswalkError(f"非 strict 历史项不得绑定目标规则：{identity}")
            if disposition != "unclassified" and not evidence:
                raise CrosswalkError(f"已处置历史项必须绑定证据：{identity}")
            output[identity] = raw
        return output

    runtime_entries = value.get("runtime_observations")
    if not isinstance(runtime_entries, list):
        raise CrosswalkError("cross-source dispositions.runtime_observations 必须是数组")
    runtime: dict[str, dict[str, Any]] = {}
    runtime_keys = {
        "observation_id",
        "disposition",
        "sink_ids",
        "rationale",
        "evidence_paths",
    }
    for raw in runtime_entries:
        if not isinstance(raw, dict) or set(raw) != runtime_keys:
            raise CrosswalkError("runtime observation disposition 字段不闭合")
        observation_id = raw.get("observation_id")
        if (
            not isinstance(observation_id, str)
            or not observation_id
            or observation_id in runtime
        ):
            raise CrosswalkError(f"runtime observation 身份缺失或重复：{observation_id}")
        if raw.get("disposition") not in {
            "mapped_sink",
            "out_of_scope_proven",
            "unclassified",
        }:
            raise CrosswalkError(f"runtime observation disposition 非法：{observation_id}")
        sink_ids = raw.get("sink_ids")
        evidence_paths = raw.get("evidence_paths")
        if not isinstance(sink_ids, list) or not all(
            isinstance(item, str) and item for item in sink_ids
        ):
            raise CrosswalkError(f"runtime observation sink_ids 非法：{observation_id}")
        if not isinstance(evidence_paths, list) or not all(
            isinstance(item, str) and item for item in evidence_paths
        ):
            raise CrosswalkError(f"runtime observation evidence_paths 非法：{observation_id}")
        if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
            raise CrosswalkError(f"runtime observation rationale 缺失：{observation_id}")
        if raw["disposition"] == "mapped_sink" and not sink_ids:
            raise CrosswalkError(f"mapped runtime observation 必须绑定 sink：{observation_id}")
        if raw["disposition"] != "unclassified" and not evidence_paths:
            raise CrosswalkError(f"已处置 runtime observation 必须绑定证据：{observation_id}")
        runtime[observation_id] = raw

    return {
        "target_sinks": result,
        "historical_source_candidates": historical_entries(
            "historical_source_candidates", "source_rule_id"
        ),
        "hitcc_clues": historical_entries("hitcc_clues", "clue_id"),
        "hitcc_documents": historical_entries("hitcc_documents", "path"),
        "runtime_observations": runtime,
    }


def historical_source_rows(
    source: dict[str, Any],
    known_specs: set[str],
    overrides: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 2.1.88 候选转换为唯一处置行。"""

    rules = source.get("rules")
    if not isinstance(rules, list):
        raise CrosswalkError("2.1.88 台账缺少 rules")
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for raw in rules:
        source_id = raw.get("source_rule_id")
        refs = raw.get("spec_rule_ids", [])
        if not isinstance(source_id, str) or not isinstance(refs, list):
            raise CrosswalkError("2.1.88 候选结构非法")
        seen.add(source_id)
        matched = sorted({str(item) for item in refs if str(item) in known_specs})
        status = raw.get("target_static_status")
        override = overrides.get(source_id)
        if override is not None:
            disposition = str(override["disposition"])
            matched = sorted(set(override["spec_ids"]))
            rationale = str(override["rationale"])
            evidence_paths = sorted(set(override["evidence_paths"]))
        elif status == "out_of_scope":
            disposition = "out_of_scope_proven"
            rationale = "历史台账已将该原子命题明确判为范围外。"
            evidence_paths = []
        elif matched:
            disposition = "mapped_historical"
            rationale = "沿用历史台账中已存在的 SPEC 映射；仍由目标证据决定证据等级。"
            evidence_paths = []
        else:
            disposition = "unclassified"
            rationale = "历史台账没有可用的目标规则映射。"
            evidence_paths = []
        if any(spec_id not in known_specs for spec_id in matched):
            raise CrosswalkError(f"2.1.88 disposition 引用未知规则：{source_id}")
        if disposition == "unclassified":
            unresolved.append(source_id)
        rows.append(
            {
                "source_rule_id": source_id,
                "proposition": raw.get("proposition"),
                "source_paths": raw.get("source_paths", []),
                "historical_status": status,
                "spec_ids": matched,
                "unresolved_refs": sorted(
                    {str(item) for item in refs if str(item) not in known_specs}
                ),
                "disposition": disposition,
                "rationale": rationale,
                "evidence_paths": evidence_paths,
            }
        )
    unknown = sorted(set(overrides) - seen)
    if unknown:
        raise CrosswalkError(f"2.1.88 disposition 引用未知候选：{unknown}")
    return rows, sorted(unresolved)


def hitcc_rows(
    hitcc: dict[str, Any],
    known_specs: set[str],
    clue_overrides: dict[str, dict[str, Any]],
    document_overrides: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """把 HitCC 原子线索和直接线索文档转换为闭集行。"""

    clues = hitcc.get("clues")
    documents = hitcc.get("document_inventory")
    if not isinstance(clues, list) or not isinstance(documents, list):
        raise CrosswalkError("HitCC 台账缺少 clues 或 document_inventory")
    rows: list[dict[str, Any]] = []
    unresolved_clues: list[str] = []
    seen_clues: set[str] = set()
    for raw in clues:
        clue_id = raw.get("clue_id")
        refs = raw.get("spec_rule_ids", [])
        if not isinstance(clue_id, str) or not isinstance(refs, list):
            raise CrosswalkError("HitCC 线索结构非法")
        seen_clues.add(clue_id)
        matched = sorted({str(item) for item in refs if str(item) in known_specs})
        coverage = raw.get("coverage")
        override = clue_overrides.get(clue_id)
        if override is not None:
            disposition = str(override["disposition"])
            matched = sorted(set(override["spec_ids"]))
            rationale = str(override["rationale"])
            evidence_paths = sorted(set(override["evidence_paths"]))
        elif coverage == "out_of_scope":
            disposition = "out_of_scope_proven"
            rationale = "HitCC 台账已将该原子线索明确判为范围外。"
            evidence_paths = []
        elif coverage == "covered" and matched:
            disposition = "mapped_historical"
            rationale = "沿用 HitCC 台账中已闭合的 SPEC 映射。"
            evidence_paths = []
        else:
            disposition = "unclassified"
            rationale = "HitCC 线索仍为 partial／missing 或没有有效 SPEC 映射。"
            evidence_paths = []
        if any(spec_id not in known_specs for spec_id in matched):
            raise CrosswalkError(f"HitCC disposition 引用未知规则：{clue_id}")
        if disposition == "unclassified":
            unresolved_clues.append(clue_id)
        rows.append(
            {
                "clue_id": clue_id,
                "proposition": raw.get("proposition"),
                "source_path": raw.get("source_path"),
                "source_lines": raw.get("source_lines"),
                "coverage": coverage,
                "spec_ids": matched,
                "unresolved_refs": sorted(
                    {str(item) for item in refs if str(item) not in known_specs}
                ),
                "disposition": disposition,
                "rationale": rationale,
                "evidence_paths": evidence_paths,
            }
        )
    unknown_clues = sorted(set(clue_overrides) - seen_clues)
    if unknown_clues:
        raise CrosswalkError(f"HitCC disposition 引用未知线索：{unknown_clues}")

    document_paths = {str(row.get("path")) for row in documents}
    unknown_documents = sorted(set(document_overrides) - document_paths)
    if unknown_documents:
        raise CrosswalkError(f"HitCC disposition 引用未知文档：{unknown_documents}")
    clue_rows_by_id = {str(row["clue_id"]): row for row in rows}
    document_rows: list[dict[str, Any]] = []
    unresolved_documents: list[str] = []
    for row in documents:
        path = str(row.get("path"))
        if row.get("disposition") != "clue_source":
            continue
        override = document_overrides.get(path)
        if override is not None:
            disposition = str(override["disposition"])
            refs = sorted(set(override["spec_ids"]))
            rationale = str(override["rationale"])
            evidence_paths = sorted(set(override["evidence_paths"]))
        else:
            clue_ids = [str(item) for item in row.get("clue_ids", [])]
            refs = sorted(
                {
                    spec_id
                    for clue_id in clue_ids
                    for spec_id in clue_rows_by_id.get(clue_id, {}).get("spec_ids", [])
                }
            )
            if row.get("mapping_status") == "mapped" and refs:
                disposition = "mapped_historical"
                rationale = "文档已通过 clue_id 反向映射到现有原子规则。"
            else:
                disposition = "unclassified"
                rationale = "直接线索文档尚未获得唯一原子命题处置。"
            evidence_paths = []
        if disposition == "unclassified":
            unresolved_documents.append(path)
        document_rows.append(
            {
                "path": path,
                "mapping_status": row.get("mapping_status"),
                "clue_ids": sorted(str(item) for item in row.get("clue_ids", [])),
                "disposition": disposition,
                "spec_ids": refs,
                "rationale": rationale,
                "evidence_paths": evidence_paths,
            }
        )
    return rows, document_rows, sorted(unresolved_clues), sorted(unresolved_documents)


def require_explicit_dispositions(
    source: dict[str, Any],
    hitcc: dict[str, Any],
    target_sinks: list[dict[str, Any]],
    capture_index: dict[str, Any] | None,
    dispositions: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """在闭集模式下要求每个分母项都由本次人工处置显式签发。"""

    expected = {
        "target_sinks": {str(row.get("sink_id")) for row in target_sinks},
        "historical_source_candidates": {
            str(row.get("source_rule_id")) for row in source.get("rules", [])
        },
        "hitcc_clues": {
            str(row.get("clue_id")) for row in hitcc.get("clues", [])
        },
        "hitcc_documents": {
            str(row.get("path"))
            for row in hitcc.get("document_inventory", [])
            if row.get("disposition") == "clue_source"
        },
        "runtime_observations": {
            str(row.get("observation_id"))
            for row in (
                capture_index.get("target", {}).get("network_observations", [])
                if isinstance(capture_index, dict)
                else []
            )
        },
    }
    for key, expected_ids in expected.items():
        actual_ids = set(dispositions[key])
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        if missing or extra:
            raise CrosswalkError(
                f"{key} 未逐项显式处置：missing={missing} extra={extra}"
            )


def explicit_disposition_counts(
    source: dict[str, Any],
    hitcc: dict[str, Any],
    target_sinks: list[dict[str, Any]],
    capture_index: dict[str, Any] | None,
    dispositions: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, int]]:
    """分别统计每个闭集分母的预期项和显式签发项。"""

    expected = {
        "target_sinks": {str(row.get("sink_id")) for row in target_sinks},
        "historical_source_candidates": {
            str(row.get("source_rule_id")) for row in source.get("rules", [])
        },
        "hitcc_clues": {
            str(row.get("clue_id")) for row in hitcc.get("clues", [])
        },
        "hitcc_documents": {
            str(row.get("path"))
            for row in hitcc.get("document_inventory", [])
            if row.get("disposition") == "clue_source"
        },
        "runtime_observations": {
            str(row.get("observation_id"))
            for row in (
                capture_index.get("target", {}).get("network_observations", [])
                if isinstance(capture_index, dict)
                else []
            )
        },
    }
    return {
        key: {
            "expected": len(expected_ids),
            "explicit": len(set(dispositions[key]) & expected_ids),
        }
        for key, expected_ids in expected.items()
    }


def validate_strict_scenarios(
    capture_index: dict[str, Any] | None,
    dispositions: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """确保 strict sink 只引用本次目标 Campaign 实际存在的场景。"""

    strict_entries = [
        row
        for row in dispositions["target_sinks"].values()
        if row.get("disposition") == "mapped_strict"
    ]
    if not strict_entries:
        return
    if not isinstance(capture_index, dict):
        raise CrosswalkError("strict sink 缺少目标 Campaign capture index")
    target = capture_index.get("target")
    relay = capture_index.get("relay")
    available = {
        str(item)
        for item in (
            target.get("scenarios", []) if isinstance(target, dict) else []
        )
    }
    relay_target = relay.get("target") if isinstance(relay, dict) else None
    if isinstance(relay_target, dict):
        available.update(str(item) for item in relay_target.get("probe_ids", []))
    for row in strict_entries:
        unknown = sorted(set(row.get("scenario_ids", [])) - available)
        if unknown:
            raise CrosswalkError(
                f"strict sink 引用本次 Campaign 外场景：{row['sink_id']}={unknown}"
            )


def target_sink_rows(
    baseline_sinks: list[dict[str, Any]],
    target_sinks: list[dict[str, Any]],
    dispositions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """为每个目标 sink 绑定历史语义命中和人工处置。"""

    baseline_by_semantic: dict[str, list[str]] = defaultdict(list)
    for sink in baseline_sinks:
        baseline_by_semantic[str(sink["semantic_sha256"])].append(str(sink["sink_id"]))
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    additions: list[dict[str, Any]] = []
    for sink in target_sinks:
        sink_id = str(sink["sink_id"])
        disposition = dispositions.get(sink_id)
        if disposition is None:
            disposition_value = "unclassified"
            traffic_class = "unknown"
            rationale = "尚未签发目标 sink disposition。"
            spec_ids: list[str] = []
            scenario_ids: list[str] = []
            evidence_paths: list[str] = []
            migration_decision = "change"
        else:
            disposition_value = str(disposition["disposition"])
            traffic_class = str(disposition["traffic_class"])
            rationale = str(disposition["rationale"])
            spec_ids = sorted(set(disposition.get("spec_ids", [])))
            scenario_ids = sorted(set(disposition.get("scenario_ids", [])))
            evidence_paths = sorted(set(disposition.get("evidence_paths", [])))
            migration_decision = str(disposition.get("migration_decision", "change"))
            if migration_decision == "add":
                additions.append(dict(disposition["new_rule"]))
        if disposition_value == "unclassified":
            unresolved.append(sink_id)
        rows.append(
            {
                "sink_id": sink_id,
                "source_kind": sink.get("source_kind"),
                "discovery_sources": sink.get("discovery_sources", []),
                "category": sink.get("category"),
                "semantic_sha256": sink.get("semantic_sha256"),
                "reachability": sink.get("reachability"),
                "privacy_keys": sink.get("privacy_keys", []),
                "baseline_sink_ids": sorted(
                    baseline_by_semantic.get(str(sink["semantic_sha256"]), [])
                ),
                "traffic_class": traffic_class,
                "disposition": disposition_value,
                "migration_decision": migration_decision,
                "spec_ids": spec_ids,
                "scenario_ids": scenario_ids,
                "evidence_paths": evidence_paths,
                "rationale": rationale,
            }
        )
    return rows, sorted(unresolved), additions


def runtime_observation_rows(
    capture_index: dict[str, Any] | None,
    dispositions: dict[str, dict[str, Any]],
    target_sink_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """闭合全 host／path 运行发现，拒绝待证端点预筛样本。"""

    if capture_index is None:
        if dispositions:
            raise CrosswalkError("缺少 capture index 时禁止提供 runtime dispositions")
        return [], [], ["capture_index_missing"]
    if (
        capture_index.get("schema_version") != "claude-code-fw-e-capture-index/v1"
        or capture_index.get("result") != "passed"
    ):
        raise CrosswalkError("capture index 未通过")
    target = capture_index.get("target")
    if not isinstance(target, dict):
        raise CrosswalkError("capture index 缺少 target")
    for group_name in ("control", "target"):
        group = capture_index.get(group_name)
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
            raise CrosswalkError(
                f"capture index 的 {group_name} 未证明两项隐私开关实际生效"
            )
    scopes = target.get("capture_host_scopes")
    scope_failures: list[str] = []
    if scopes != ["all"]:
        scope_failures.append("target_capture_not_all_hosts")
    channels = capture_index.get("channels")
    required_channels = {"P", "R", "J", "M"}
    if not isinstance(channels, list) or not required_channels.issubset(
        {str(item) for item in channels}
    ):
        scope_failures.append("target_required_channels_missing")
    relay = capture_index.get("relay")
    relay_target = relay.get("target") if isinstance(relay, dict) else None
    expected_probe_ids = {
        f"r-{scenario}" for scenario in target.get("scenarios", [])
    }
    if (
        not isinstance(relay, dict)
        or relay.get("result") != "passed"
        or not isinstance(relay_target, dict)
        or relay_target.get("version") != target.get("version")
        or set(relay_target.get("probe_ids", [])) != expected_probe_ids
    ):
        scope_failures.append("target_relay_r_incomplete")
    observations = target.get("network_observations")
    if not isinstance(observations, list):
        scope_failures.append("target_network_inventory_missing")
        observations = []
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for raw in observations:
        observation_id = raw.get("observation_id") if isinstance(raw, dict) else None
        if not isinstance(observation_id, str) or observation_id in seen:
            raise CrosswalkError(f"runtime observation 身份缺失或重复：{observation_id}")
        seen.add(observation_id)
        disposition = dispositions.get(observation_id)
        if disposition is None:
            disposition_value = "unclassified"
            sink_ids: list[str] = []
            rationale = "全 host／path 观测尚未绑定目标 sink。"
            evidence_paths: list[str] = []
        else:
            disposition_value = str(disposition["disposition"])
            sink_ids = sorted(set(disposition["sink_ids"]))
            rationale = str(disposition["rationale"])
            evidence_paths = sorted(set(disposition["evidence_paths"]))
            unknown_sinks = sorted(set(sink_ids) - target_sink_ids)
            if unknown_sinks:
                raise CrosswalkError(
                    f"runtime observation 引用未知目标 sink：{observation_id}={unknown_sinks}"
                )
        if disposition_value == "unclassified":
            unresolved.append(observation_id)
        rows.append(
            {
                "observation_id": observation_id,
                "transport": raw.get("transport"),
                "method": raw.get("method"),
                "scheme": raw.get("scheme"),
                "host": raw.get("host"),
                "port": raw.get("port"),
                "path": raw.get("path"),
                "disposition": disposition_value,
                "sink_ids": sink_ids,
                "rationale": rationale,
                "evidence_paths": evidence_paths,
            }
        )
    unknown = sorted(set(dispositions) - seen)
    if unknown:
        raise CrosswalkError(f"runtime disposition 引用未知观测：{unknown}")
    return sorted(rows, key=lambda row: row["observation_id"]), sorted(unresolved), scope_failures


def build_matrix(
    source_path: Path,
    hitcc_path: Path,
    baseline_ledger_path: Path,
    baseline_native_path: Path,
    target_native_path: Path,
    target_version: str,
    dispositions_path: Path | None,
    capture_index_path: Path | None,
    require_explicit: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构建完整矩阵和闭集报告。"""

    source = load_json_file(source_path, "2.1.88 coverage")
    hitcc = load_json_file(hitcc_path, "HitCC coverage")
    baseline_ledger = load_json_file(baseline_ledger_path, "historical rule ledger")
    baseline_native = load_json_file(baseline_native_path, "baseline native inventory")
    target_native = load_json_file(target_native_path, "target native inventory")
    capture_index = (
        load_json_file(capture_index_path, "FW-E capture index")
        if capture_index_path is not None
        else None
    )
    baseline_rules = flatten_baseline(baseline_ledger)
    baseline_sinks = inventory_sinks(baseline_native, "baseline sink inventory")
    target_sinks = inventory_sinks(target_native, "target sink inventory")
    if baseline_native.get("target_version") != baseline_ledger.get("target_version"):
        raise CrosswalkError("baseline sink inventory 与历史规则版本不一致")
    if target_native.get("target_version") != target_version:
        raise CrosswalkError("target sink inventory 与目标版本不一致")
    baseline_spec_ids = {str(row["id"]) for row in baseline_rules}
    dispositions = load_dispositions(
        dispositions_path, target_version, {str(row["sink_id"]) for row in target_sinks}
    )
    validate_strict_scenarios(capture_index, dispositions)
    if require_explicit:
        require_explicit_dispositions(
            source, hitcc, target_sinks, capture_index, dispositions
        )
    explicit_counts = explicit_disposition_counts(
        source, hitcc, target_sinks, capture_index, dispositions
    )
    explicit_expected_total = sum(row["expected"] for row in explicit_counts.values())
    explicit_actual_total = sum(row["explicit"] for row in explicit_counts.values())
    sink_rows, unresolved_sinks, additions = target_sink_rows(
        baseline_sinks, target_sinks, dispositions["target_sinks"]
    )
    additions_by_id: dict[str, dict[str, Any]] = {}
    for row in additions:
        addition_id = str(row.get("id"))
        previous = additions_by_id.get(addition_id)
        if previous is not None and previous != row:
            raise CrosswalkError(f"目标新增规则定义冲突：{addition_id}")
        additions_by_id[addition_id] = row
    additions = [additions_by_id[key] for key in sorted(additions_by_id)]
    addition_ids = [str(row["id"]) for row in additions]
    if set(addition_ids) & baseline_spec_ids:
        raise CrosswalkError("目标新增规则 ID 与历史规则冲突")
    known_specs = baseline_spec_ids | set(addition_ids)
    for row in sink_rows:
        unknown_specs = sorted(set(row["spec_ids"]) - known_specs)
        if unknown_specs:
            raise CrosswalkError(
                f"目标 sink 引用未知规则：{row['sink_id']}={unknown_specs}"
            )
    source_rows, unresolved_source = historical_source_rows(
        source, known_specs, dispositions["historical_source_candidates"]
    )
    clue_rows, document_rows, unresolved_clues, unresolved_documents = hitcc_rows(
        hitcc,
        known_specs,
        dispositions["hitcc_clues"],
        dispositions["hitcc_documents"],
    )
    runtime_rows, unresolved_runtime, runtime_scope_failures = runtime_observation_rows(
        capture_index,
        dispositions["runtime_observations"],
        {str(row["sink_id"]) for row in target_sinks},
    )
    target_rules = [
        {
            "id": str(row["id"]),
            "domain": row.get("domain"),
            "retained_claim": row.get("retained_claim"),
            "scope": row.get("scope"),
            "required_channels": row.get("required_channels", []),
            "origin": "historical_rule",
            "baseline_disposition": (
                row.get("status", {}).get("disposition")
                if isinstance(row.get("status"), dict)
                else None
            ),
        }
        for row in baseline_rules
    ] + [
        dict(row, origin="target_native_add", baseline_disposition=None)
        for row in additions
    ]
    bindings = {
        "source_2_1_88": {
            "path": str(source_path),
            "sha256": sha256_file(source_path),
        },
        "hitcc_2_1_197": {
            "path": str(hitcc_path),
            "sha256": sha256_file(hitcc_path),
        },
        "historical_rules": {
            "path": str(baseline_ledger_path),
            "sha256": sha256_file(baseline_ledger_path),
        },
        "historical_sink_inventory": {
            "path": str(baseline_native_path),
            "sha256": sha256_file(baseline_native_path),
        },
        "target_sink_inventory": {
            "path": str(target_native_path),
            "sha256": sha256_file(target_native_path),
        },
    }
    if dispositions_path is not None:
        bindings["target_dispositions"] = {
            "path": str(dispositions_path),
            "sha256": sha256_file(dispositions_path),
        }
    if capture_index_path is not None:
        bindings["runtime_capture_index"] = {
            "path": str(capture_index_path),
            "sha256": sha256_file(capture_index_path),
        }
    unresolved = {
        "source_candidate_ids": unresolved_source,
        "hitcc_clue_ids": unresolved_clues,
        "hitcc_document_paths": unresolved_documents,
        "target_sink_ids": unresolved_sinks,
        "runtime_observation_ids": unresolved_runtime,
        "runtime_capture_scope": runtime_scope_failures,
    }
    unresolved_total = sum(len(value) for value in unresolved.values())
    matrix = {
        "schema_version": SCHEMA_MATRIX,
        "target_version": target_version,
        "target_bundle_sha256": target_native.get("bundle_sha256"),
        "bindings": bindings,
        "counts": {
            "historical_rule_count": len(baseline_rules),
            "source_candidate_count": len(source_rows),
            "hitcc_clue_count": len(clue_rows),
            "hitcc_document_count": len(document_rows),
            "historical_sink_count": len(baseline_sinks),
            "target_sink_count": len(sink_rows),
            "target_add_rule_count": len(additions),
            "target_rule_count": len(target_rules),
            "runtime_observation_count": len(runtime_rows),
            "explicit_disposition_expected_count": explicit_expected_total,
            "explicit_disposition_count": explicit_actual_total,
            "unresolved_total": unresolved_total,
        },
        "historical_source_candidates": source_rows,
        "hitcc_clues": clue_rows,
        "hitcc_documents": document_rows,
        "target_sinks": sink_rows,
        "runtime_observations": runtime_rows,
        "target_rules": target_rules,
        "unresolved": unresolved,
        "generated_at_utc": utc_now(),
    }
    closure = {
        "schema_version": SCHEMA_CLOSURE,
        "target_version": target_version,
        "matrix_sha256": canonical_sha256(matrix),
        "target_inventory_sha256": sha256_file(target_native_path),
        "target_sink_total": len(sink_rows),
        "target_sink_disposition_counts": dict(
            sorted(Counter(row["disposition"] for row in sink_rows).items())
        ),
        "historical_source_disposition_counts": dict(
            sorted(Counter(row["disposition"] for row in source_rows).items())
        ),
        "hitcc_clue_disposition_counts": dict(
            sorted(Counter(row["disposition"] for row in clue_rows).items())
        ),
        "hitcc_document_disposition_counts": dict(
            sorted(Counter(row["disposition"] for row in document_rows).items())
        ),
        "target_add_rule_count": len(additions),
        "runtime_observation_total": len(runtime_rows),
        "runtime_observation_disposition_counts": dict(
            sorted(Counter(row["disposition"] for row in runtime_rows).items())
        ),
        "explicit_dispositions": {
            "groups": explicit_counts,
            "expected_total": explicit_expected_total,
            "explicit_total": explicit_actual_total,
            "result": (
                "passed"
                if explicit_actual_total == explicit_expected_total
                else "blocked"
            ),
        },
        "target_sink_traffic_class_counts": dict(
            sorted(Counter(row["traffic_class"] for row in sink_rows).items())
        ),
        "target_sink_source_kind_counts": dict(
            sorted(Counter(row["source_kind"] for row in sink_rows).items())
        ),
        "unresolved": unresolved,
        "unresolved_total": unresolved_total,
        "result": "passed" if unresolved_total == 0 else "blocked",
    }
    return matrix, closure


def build_parser() -> argparse.ArgumentParser:
    """创建命令行解析器。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-2188", required=True, type=Path)
    parser.add_argument("--hitcc", required=True, type=Path)
    parser.add_argument("--baseline-ledger", required=True, type=Path)
    parser.add_argument(
        "--baseline-inventory",
        "--baseline-native",
        dest="baseline_native",
        required=True,
        type=Path,
    )
    parser.add_argument(
        "--target-inventory",
        "--target-native",
        dest="target_native",
        required=True,
        type=Path,
    )
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--dispositions", type=Path)
    parser.add_argument("--capture-index", type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--require-explicit", action="store_true")
    parser.add_argument("--require-closed", action="store_true")
    return parser


def main() -> int:
    """运行四方矩阵生成与闭集判定。"""

    arguments = build_parser().parse_args()
    if arguments.output_root.exists():
        print("失败：output-root 必须不存在，禁止覆盖", file=sys.stderr)
        return 1
    try:
        matrix, closure = build_matrix(
            arguments.source_2188,
            arguments.hitcc,
            arguments.baseline_ledger,
            arguments.baseline_native,
            arguments.target_native,
            arguments.target_version,
            arguments.dispositions,
            arguments.capture_index,
            arguments.require_explicit or arguments.require_closed,
        )
        arguments.output_root.mkdir(parents=True, mode=0o700)
        write_private_json(arguments.output_root / "matrix.json", matrix)
        write_private_json(arguments.output_root / "closure.json", closure)
    except (CrosswalkError, OSError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(
        "完成："
        f"目标 sink={closure['target_sink_total']}，"
        f"新增规则={closure['target_add_rule_count']}，"
        f"未闭项={closure['unresolved_total']}，"
        f"结果={closure['result']}"
    )
    if arguments.require_closed and closure["result"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
