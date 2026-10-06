#!/usr/bin/env python3
"""合并 Claude Code 目标 bundle 的 AST sink 与保守词法候选。

AST 能排除字符串、注释和普通同名方法造成的大量误命中，但动态别名与宿主调用可能
无法由 AST 分类器识别；词法扫描覆盖更保守，却不能证明命中就是网络发送。FW-E 因此
必须保存两者并集：落在 AST 调用节点内的词法命中归并到该节点，其余命中独立保留为
``lexical_only`` 候选，等待跨来源矩阵给出唯一处置。

多模块输入（Claude 升级控制面设计 B2-04）：AST 与词法索引都由同一份模块清单产生时，
每条 sink 带 ``module``，只在同一模块内归并；词法偏移按码点、AST 按 UTF-16 码元，归并一律用
词法行的 ``offset_utf16``。``counts.by_module`` 逐模块计数且各项之和等于总数；入口关系未决的
模块、动态加载边与 ``reachability == "unresolved_entry"`` 的 sink 汇成 ``unresolved_entries``，
由 ``unresolved_discovery_items`` 转成 DiscoveryInventory 的「未决」发现项。单文件输入的输出与旧实现一致。
"""

from __future__ import annotations

import argparse
import datetime as dt
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


AST_SCHEMA = "claude-code-target-native-inventory/v1"
INVENTORY_SCHEMA = "claude-code-target-sink-inventory/v1"
OFFSET_UNIT_UTF16 = "utf16"
MANIFEST_BUNDLE_KIND = "bun_module_manifest"
REACHABILITY_UNRESOLVED = "unresolved_entry"
# DiscoveryInventory 中「未决」发现项的来源类别与处置：沿用闭集里表示「尚未处置」的 unclassified，
# 使闭合门禁在这些项被显式处置之前保持 blocked。
UNRESOLVED_SOURCE_KIND = "target_unresolved_entry"
UNRESOLVED_DISPOSITION = "unclassified"


class TargetInventoryError(RuntimeError):
    """表示 AST／词法输入不完整或无法无歧义归并。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _write_private_json(path: Path, value: Any) -> None:
    """只创建一次私有 JSON，禁止覆盖已有取证产物。"""

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


def _ast_modules(value: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """清单模式的 AST 返回 模块名 → 模块统计（按清单序号有序）；单文件模式返回 None。"""

    bundle = value.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("kind") != MANIFEST_BUNDLE_KIND:
        return None
    modules = value.get("modules")
    if not isinstance(modules, list) or not modules:
        raise TargetInventoryError("多模块 AST 缺少 modules")
    result: dict[str, dict[str, Any]] = {}
    for row in sorted(
        (item for item in modules if isinstance(item, dict)),
        key=lambda item: int(item.get("index", -1)),
    ):
        name = row.get("name")
        if not isinstance(name, str) or name in result:
            raise TargetInventoryError("多模块 AST 的模块名缺失或重复")
        if not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64:
            raise TargetInventoryError(f"多模块 AST 的模块缺少内容摘要：{name}")
        result[name] = row
    if len(result) != len(modules):
        raise TargetInventoryError("多模块 AST 的 modules 含非法行")
    return result


def _validate_ast(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema_version") != AST_SCHEMA:
        raise TargetInventoryError("AST inventory schema 不匹配")
    completeness = value.get("completeness")
    if not isinstance(completeness, dict) or completeness.get("truncated") is not False:
        raise TargetInventoryError("AST sink inventory 被截断或缺少完整性声明")
    parser = value.get("parser")
    diagnostics = parser.get("parse_diagnostics") if isinstance(parser, dict) else None
    if not isinstance(diagnostics, list) or diagnostics:
        raise TargetInventoryError("AST 解析存在诊断或未保存诊断数组")
    if value.get("offset_unit") not in {None, OFFSET_UNIT_UTF16}:
        raise TargetInventoryError(f"AST 坐标单位不受支持：{value.get('offset_unit')}")
    sinks = value.get("sinks")
    if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
        raise TargetInventoryError("AST sink_total 与数组不一致")
    identities = [row.get("sink_id") for row in sinks if isinstance(row, dict)]
    if len(identities) != len(sinks) or len(set(identities)) != len(identities):
        raise TargetInventoryError("AST sink_id 缺失或重复")
    modules = _ast_modules(value)
    if modules is None:
        return sorted(sinks, key=lambda row: int(row["source_start"]))
    order = {name: position for position, name in enumerate(modules)}
    per_module: Counter[str] = Counter()
    for row in sinks:
        if row.get("module") not in order:
            raise TargetInventoryError(f"AST sink 没有绑定已分析模块：{row.get('sink_id')}")
        per_module[str(row["module"])] += 1
    for name, stats in modules.items():
        if stats.get("sink_count") != per_module[name]:
            raise TargetInventoryError(f"AST 模块 sink 计数与 sinks 不一致：{name}")
    return sorted(
        sinks, key=lambda row: (order[str(row["module"])], int(row["source_start"]))
    )


def _validate_lexical(value: dict[str, Any], *, multi_module: bool) -> list[dict[str, Any]]:
    if value.get("sink_inventory_truncated") is not False:
        raise TargetInventoryError("词法 sink inventory 被截断或缺少完整性声明")
    sinks = value.get("sinks")
    if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
        raise TargetInventoryError("词法 sink_total 与数组不一致")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, row in enumerate(sinks):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("offset"), int)
            or row["offset"] < 0
        ):
            raise TargetInventoryError(f"词法 sink[{index}] 非法")
        if "offset_utf16" in row and (
            not isinstance(row["offset_utf16"], int) or row["offset_utf16"] < row["offset"]
        ):
            raise TargetInventoryError(f"词法 sink[{index}] 的 offset_utf16 非法")
        if multi_module:
            if not isinstance(row.get("module"), str) or "offset_utf16" not in row:
                raise TargetInventoryError(f"多模块词法 sink[{index}] 缺少 module 或 offset_utf16")
            identity: tuple[Any, ...] = (row["module"], row["kind"], row["offset"])
        else:
            identity = (row["kind"], row["offset"])
        if identity in seen:
            raise TargetInventoryError(f"词法 sink 重复：{identity}")
        seen.add(identity)
        normalized.append(row)
    return sorted(
        normalized,
        key=lambda row: (str(row.get("module", "")), int(row["offset"]), str(row["kind"])),
    )


def _lexical_id(bundle_sha256: str, row: dict[str, Any]) -> str:
    """词法候选身份：按所在文本内容寻址。

    单文件时 ``bundle_sha256`` 是 bundle 摘要（与旧实现相同）；多模块时传入所属模块的内容摘要，
    内容不变的模块跨版本得到同一身份，旧版主模块的身份与单文件口径一致。两个模块内容完全相同
    会撞号，由合并后的 sink_id 冲突检查拒绝，不静默去重。
    """

    digest = canonical_sha256(
        {
            "bundle_sha256": bundle_sha256,
            "kind": row["kind"],
            "offset": row["offset"],
        }
    )
    return f"TN-LEX-{digest[:20]}"


def _lexical_position(
    row: dict[str, Any],
    *,
    utf16_ast: bool,
    astral_free: bool,
) -> int:
    """词法命中在 AST 坐标系中的位置。

    AST 按 UTF-16 码元计偏移、Python 词法按码点计偏移；二者只在增补平面字符处分叉。词法行带
    ``offset_utf16`` 时直接使用；旧格式词法索引只有码点偏移，仅当 AST 声明该文本没有增补平面
    字符（两种偏移逐一相等）时才可使用，否则拒绝合并，不按错位坐标静默归并。
    """

    if not utf16_ast:
        return int(row["offset"])
    if "offset_utf16" in row:
        return int(row["offset_utf16"])
    if astral_free:
        return int(row["offset"])
    raise TargetInventoryError(
        "词法索引缺少 offset_utf16，且 AST 未证明文本没有增补平面字符，码点偏移不能与 UTF-16 坐标合并"
    )


def build_target_inventory(
    ast: dict[str, Any],
    lexical: dict[str, Any],
    *,
    target_version: str,
    platform: str,
    ast_binding: dict[str, Any] | None = None,
    lexical_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建无截断、无重复身份的目标 sink 并集（单文件或多模块）。"""

    ast_sinks = _validate_ast(ast)
    modules = _ast_modules(ast)
    multi_module = modules is not None
    lexical_sinks = _validate_lexical(lexical, multi_module=multi_module)
    ast_bundle = ast.get("bundle")
    ast_bundle_sha = ast_bundle.get("sha256") if isinstance(ast_bundle, dict) else None
    lexical_bundle_sha = lexical.get("bundle_sha256")
    if not isinstance(ast_bundle_sha, str) or ast_bundle_sha != lexical_bundle_sha:
        raise TargetInventoryError("AST 与词法索引没有绑定同一 bundle")
    if multi_module and (
        not isinstance(ast_bundle.get("manifest_sha256"), str)
        or ast_bundle.get("manifest_sha256") != lexical.get("bundle_manifest_sha256")
    ):
        raise TargetInventoryError("AST 与词法索引没有绑定同一模块清单")
    utf16_ast = ast.get("offset_unit") == OFFSET_UNIT_UTF16
    if multi_module:
        unknown = sorted({str(row["module"]) for row in lexical_sinks} - set(modules))
        if unknown:
            raise TargetInventoryError(f"词法 sink 引用了 AST 未分析的模块：{unknown}")
        unresolved_modules = {
            name for name, stats in modules.items() if stats.get("status") == "unresolved"
        }
    else:
        unresolved_modules = set()

    def astral_free(row: dict[str, Any]) -> bool:
        if multi_module:
            return modules[str(row["module"])].get("astral_code_point_count") == 0
        return isinstance(ast_bundle, dict) and ast_bundle.get("astral_code_point_count") == 0

    ast_by_module: dict[str | None, list[dict[str, Any]]] = {}
    for row in ast_sinks:
        ast_by_module.setdefault(row.get("module") if multi_module else None, []).append(row)
    lexical_matches: dict[str, list[dict[str, Any]]] = {
        str(row["sink_id"]): [] for row in ast_sinks
    }
    lexical_only: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for lexical_row in lexical_sinks:
        position = _lexical_position(
            lexical_row, utf16_ast=utf16_ast, astral_free=astral_free(lexical_row)
        )
        module_key = lexical_row["module"] if multi_module else None
        candidates = [
            row
            for row in ast_by_module.get(module_key, [])
            if int(row["source_start"]) <= position < int(row["source_end"])
        ]
        lexical_id = _lexical_id(
            str(modules[module_key]["sha256"]) if multi_module else ast_bundle_sha, lexical_row
        )
        normalized_lexical = {
            "candidate_id": lexical_id,
            "kind": lexical_row["kind"],
            "offset": lexical_row["offset"],
            "nearest_symbol": lexical_row.get("nearest_symbol"),
        }
        if multi_module:
            normalized_lexical["module"] = lexical_row["module"]
            normalized_lexical["offset_utf16"] = position
        elif utf16_ast and position != lexical_row["offset"]:
            normalized_lexical["offset_utf16"] = position
        if len(candidates) == 1:
            lexical_matches[str(candidates[0]["sink_id"])].append(normalized_lexical)
        elif not candidates:
            lexical_only.append({**normalized_lexical, "_position": position})
        else:
            ambiguous.append(
                {
                    **normalized_lexical,
                    "ast_sink_ids": sorted(str(row["sink_id"]) for row in candidates),
                }
            )
    if ambiguous:
        raise TargetInventoryError(
            f"词法候选同时落入多个 AST 节点：{[row['candidate_id'] for row in ambiguous]}"
        )

    sinks: list[dict[str, Any]] = []
    for row in ast_sinks:
        sink_id = str(row["sink_id"])
        matches = sorted(
            lexical_matches[sink_id], key=lambda item: str(item["candidate_id"])
        )
        sink = {
            "sink_id": sink_id,
            "source_kind": "ast_call",
            "category": row.get("category"),
            "semantic_sha256": row.get("semantic_sha256"),
            "source_start": row.get("source_start"),
            "source_end": row.get("source_end"),
            "owner_symbol": row.get("owner_symbol"),
            "reachability": row.get("reachability", "unknown"),
            "privacy_keys": row.get("privacy_keys", []),
            "relevant_literals": row.get("relevant_literals", []),
            "discovery_sources": (
                ["ast", "lexical"] if matches else ["ast"]
            ),
            "lexical_candidates": matches,
        }
        if multi_module:
            sink["module"] = row["module"]
        sinks.append(sink)
    for row in lexical_only:
        position = row.pop("_position")
        semantic = canonical_sha256(
            {
                "candidate_id": row["candidate_id"],
                "kind": row["kind"],
                "source_kind": "lexical_only",
            }
        )
        sink = {
            "sink_id": row["candidate_id"],
            "source_kind": "lexical_only",
            "category": f"lexical_candidate_{row['kind']}",
            "semantic_sha256": semantic,
            "source_start": position,
            "source_end": position,
            "owner_symbol": row.get("nearest_symbol"),
            # 词法候选不做调用图判定；只有所属模块的入口关系未决时如实标 unresolved_entry。
            "reachability": (
                REACHABILITY_UNRESOLVED
                if multi_module and row["module"] in unresolved_modules
                else "unknown"
            ),
            "privacy_keys": [],
            "relevant_literals": [],
            "discovery_sources": ["lexical"],
            "lexical_candidates": [row],
        }
        if multi_module:
            sink["module"] = row["module"]
        sinks.append(sink)
    sinks.sort(key=lambda row: str(row["sink_id"]))
    identities = [str(row["sink_id"]) for row in sinks]
    if len(identities) != len(set(identities)):
        raise TargetInventoryError("合并后 sink_id 冲突")

    counts = Counter(str(row["source_kind"]) for row in sinks)
    result: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA,
        "target_version": target_version,
        "platform": platform,
        "bundle_sha256": ast_bundle_sha,
        "source_bindings": {
            "ast": ast_binding,
            "lexical": lexical_binding,
        },
        "completeness": {
            "truncated": False,
            "ast_parse_diagnostic_count": 0,
            "ambiguous_lexical_match_count": 0,
            "duplicate_sink_id_count": 0,
            "disposition_state": "unclassified_until_crosswalk",
        },
        "counts": {
            "ast_sink_count": len(ast_sinks),
            "lexical_candidate_count": len(lexical_sinks),
            "lexical_matched_ast_count": len(lexical_sinks) - len(lexical_only),
            "lexical_only_count": len(lexical_only),
            "inventory_sink_count": len(sinks),
            "source_kind_counts": dict(sorted(counts.items())),
        },
        "sink_total": len(sinks),
        "sinks": sinks,
        "limitations": [
            "lexical_only 只是保守候选，必须由语法上下文或运行证据显式处置，不能自动视为网络发送。",
            "AST 调用分类无法独立穷尽动态别名、反射、原生模块和宿主 transport。",
            "reachability=unknown 不表示不可达，也不能支持 delete 或范围外结论。",
        ],
        "generated_at_utc": _utc_now(),
    }
    if multi_module:
        _attach_module_view(result, ast, modules, ast_sinks, lexical_sinks, lexical_only, sinks)
    return result


def _attach_module_view(
    result: dict[str, Any],
    ast: dict[str, Any],
    modules: dict[str, dict[str, Any]],
    ast_sinks: list[dict[str, Any]],
    lexical_sinks: list[dict[str, Any]],
    lexical_only: list[dict[str, Any]],
    sinks: list[dict[str, Any]],
) -> None:
    """多模块输出：逐模块计数（各项之和等于总数）、模块图摘要与「未决」清单。"""

    by_module: dict[str, dict[str, int]] = {
        name: {
            "ast_sink_count": 0,
            "lexical_candidate_count": 0,
            "lexical_matched_ast_count": 0,
            "lexical_only_count": 0,
            "inventory_sink_count": 0,
        }
        for name in modules
    }
    for row in ast_sinks:
        by_module[str(row["module"])]["ast_sink_count"] += 1
    for row in lexical_sinks:
        by_module[str(row["module"])]["lexical_candidate_count"] += 1
    for row in lexical_only:
        by_module[str(row["module"])]["lexical_only_count"] += 1
    for counts in by_module.values():
        counts["lexical_matched_ast_count"] = (
            counts["lexical_candidate_count"] - counts["lexical_only_count"]
        )
        counts["inventory_sink_count"] = counts["ast_sink_count"] + counts["lexical_only_count"]
    totals = result["counts"]
    for key in (
        "ast_sink_count",
        "lexical_candidate_count",
        "lexical_matched_ast_count",
        "lexical_only_count",
        "inventory_sink_count",
    ):
        if sum(counts[key] for counts in by_module.values()) != totals[key]:
            raise TargetInventoryError(f"逐模块计数之和与总数不一致：{key}")
    totals["by_module"] = by_module
    totals["reachability_counts"] = dict(
        sorted(Counter(str(row["reachability"]) for row in sinks).items())
    )

    graph = ast.get("module_graph") if isinstance(ast.get("module_graph"), dict) else {}
    unresolved = unresolved_entries(ast, sinks)
    result["bundle_manifest_sha256"] = ast["bundle"]["manifest_sha256"]
    result["module_graph"] = graph
    result["unresolved_entries"] = unresolved
    completeness = result["completeness"]
    completeness["module_count"] = len(modules)
    completeness["unresolved_module_count"] = sum(
        1 for row in unresolved if row["kind"] == "module"
    )
    completeness["unresolved_dynamic_edge_count"] = sum(
        1 for row in unresolved if row["kind"] == "dynamic_edge"
    )
    completeness["unresolved_entry_sink_count"] = sum(
        1 for row in unresolved if row["kind"] == "sink"
    )
    # 旧字段 limitations 保持原样，多模块说明另起字段追加。
    result["multi_module_limitations"] = [
        "reachability=unresolved_entry 表示所属模块与已确认入口的关系未决；"
        "这些 sink、未决模块与动态加载边一并进入 unresolved_entries，必须显式处置。",
        "bundle_sha256 沿用 AST 的 bundle.sha256：规则 ①（旧版单主 bundle）是旧主模块内容摘要，"
        "分块 bundle 是模块清单摘要；模块清单另以 bundle_manifest_sha256 绑定。",
        "counts.by_module 逐模块计数，各项之和等于对应总数；词法候选身份按所属模块内容寻址。",
    ]


def unresolved_entries(ast: dict[str, Any], sinks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """汇总「未决」：入口关系未决的模块、非字面量动态加载边、reachability 为 unresolved_entry 的 sink。

    每项带稳定身份 ``unresolved_id``（按内容摘要，不依赖输出顺序），按身份排序。
    """

    rows: list[dict[str, Any]] = []
    graph = ast.get("module_graph") if isinstance(ast.get("module_graph"), dict) else {}
    for item in graph.get("unresolved_modules") or []:
        identity = {"kind": "module", "module": item.get("name")}
        rows.append(
            {
                "unresolved_id": f"TN-UNRESOLVED-MOD-{canonical_sha256(identity)[:20]}",
                "kind": "module",
                "module": item.get("name"),
                "reason": item.get("reason"),
            }
        )
    for item in ast.get("unresolved_dynamic_edges") or []:
        identity = {
            "kind": "dynamic_edge",
            "module": item.get("from"),
            "edge_kind": item.get("kind"),
            "source_start": item.get("source_start"),
            "source_end": item.get("source_end"),
        }
        rows.append(
            {
                "unresolved_id": f"TN-UNRESOLVED-DYN-{canonical_sha256(identity)[:20]}",
                "kind": "dynamic_edge",
                "module": item.get("from"),
                "edge_kind": item.get("kind"),
                "via_alias": item.get("via_alias"),
                "owner_symbol": item.get("owner_symbol"),
                "source_start": item.get("source_start"),
                "source_end": item.get("source_end"),
                "argument_shape": item.get("argument_shape"),
                "reason": "dynamic_load_target_not_literal",
            }
        )
    for sink in sinks:
        if sink.get("reachability") != REACHABILITY_UNRESOLVED:
            continue
        rows.append(
            {
                "unresolved_id": f"TN-UNRESOLVED-SINK-{sink['sink_id']}",
                "kind": "sink",
                "module": sink.get("module"),
                "sink_id": sink["sink_id"],
                "source_kind": sink.get("source_kind"),
                "category": sink.get("category"),
                "reason": "module_entry_unresolved",
            }
        )
    rows.sort(key=lambda row: str(row["unresolved_id"]))
    identities = [row["unresolved_id"] for row in rows]
    if len(identities) != len(set(identities)):
        raise TargetInventoryError("未决项身份重复")
    return rows


def unresolved_discovery_items(
    inventory: dict[str, Any], evidence_paths: list[str]
) -> list[dict[str, Any]]:
    """把 inventory 的 ``unresolved_entries`` 转成 DiscoveryInventory 发现项（「未决」）。

    字段与 ``claude_fw_e_validation_closure`` 的发现项一致；处置一律是 unclassified，
    使证据闭集在逐项显式处置之前保持 blocked。供 VC-1 封存阶段的 DiscoveryInventory 生成器调用。
    """

    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise TargetInventoryError("目标 inventory schema 不匹配")
    entries = inventory.get("unresolved_entries")
    if not isinstance(entries, list):
        raise TargetInventoryError("目标 inventory 缺少 unresolved_entries（不是多模块 inventory）")
    version = inventory.get("target_version")
    items: list[dict[str, Any]] = []
    for row in entries:
        if row["kind"] == "module":
            proposition = (
                f"目标 {version} 模块 {row['module']} 与已确认入口的关系未决（{row['reason']}）。"
            )
        elif row["kind"] == "dynamic_edge":
            proposition = (
                f"目标 {version} 模块 {row['module']} 在偏移 {row['source_start']} 处有加载目标"
                f"不是字面量的 {row['edge_kind']}，可能加载任意模块。"
            )
        else:
            proposition = (
                f"目标 {version} sink {row['sink_id']}（{row['category']}）所属模块"
                f" {row['module']} 的入口关系未决。"
            )
        items.append(
            {
                "discovery_id": row["unresolved_id"],
                "source_kind": UNRESOLVED_SOURCE_KIND,
                "proposition": proposition,
                "disposition": UNRESOLVED_DISPOSITION,
                "semantic_candidate_ids": [],
                "spec_ids": [],
                "evidence_paths": sorted(set(evidence_paths)),
                "rationale": (
                    "入口关系未决：既不能证明可达，也不能作为不可达或范围外结论，"
                    "必须由人工或运行证据显式处置后才能闭合。"
                ),
            }
        )
    return items


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ast-inventory", required=True, type=Path)
    parser.add_argument("--lexical-index", required=True, type=Path)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    arguments = _build_parser().parse_args()
    try:
        ast = load_json_file(arguments.ast_inventory, "AST inventory")
        lexical = load_json_file(arguments.lexical_index, "lexical index")
        result = build_target_inventory(
            ast,
            lexical,
            target_version=arguments.target_version,
            platform=arguments.platform,
            ast_binding={
                "path": str(arguments.ast_inventory),
                "sha256": sha256_file(arguments.ast_inventory),
            },
            lexical_binding={
                "path": str(arguments.lexical_index),
                "sha256": sha256_file(arguments.lexical_index),
            },
        )
        _write_private_json(arguments.output, result)
    except (OSError, ValueError, TargetInventoryError) as error:
        print(f"失败：{error}", file=sys.stderr)
        return 1
    print(
        "完成："
        f"AST={result['counts']['ast_sink_count']}，"
        f"词法={result['counts']['lexical_candidate_count']}，"
        f"词法独有={result['counts']['lexical_only_count']}，"
        f"并集={result['sink_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
