#!/usr/bin/env python3
"""合并 Claude Code 目标 bundle 的 AST sink 与保守词法候选。

AST 能排除字符串、注释和普通同名方法造成的大量误命中，但动态别名与宿主调用可能
无法由 AST 分类器识别；词法扫描覆盖更保守，却不能证明命中就是网络发送。FW-E 因此
必须保存两者并集：落在 AST 调用节点内的词法命中归并到该节点，其余命中独立保留为
``lexical_only`` 候选，等待跨来源矩阵给出唯一处置。
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
    sinks = value.get("sinks")
    if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
        raise TargetInventoryError("AST sink_total 与数组不一致")
    identities = [row.get("sink_id") for row in sinks if isinstance(row, dict)]
    if len(identities) != len(sinks) or len(set(identities)) != len(identities):
        raise TargetInventoryError("AST sink_id 缺失或重复")
    return sorted(sinks, key=lambda row: int(row["source_start"]))


def _validate_lexical(value: dict[str, Any]) -> list[dict[str, Any]]:
    if value.get("sink_inventory_truncated") is not False:
        raise TargetInventoryError("词法 sink inventory 被截断或缺少完整性声明")
    sinks = value.get("sinks")
    if not isinstance(sinks, list) or value.get("sink_total") != len(sinks):
        raise TargetInventoryError("词法 sink_total 与数组不一致")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, row in enumerate(sinks):
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("offset"), int)
            or row["offset"] < 0
        ):
            raise TargetInventoryError(f"词法 sink[{index}] 非法")
        identity = (row["kind"], row["offset"])
        if identity in seen:
            raise TargetInventoryError(f"词法 sink 重复：{identity}")
        seen.add(identity)
        normalized.append(row)
    return sorted(normalized, key=lambda row: (int(row["offset"]), str(row["kind"])))


def _lexical_id(bundle_sha256: str, row: dict[str, Any]) -> str:
    digest = canonical_sha256(
        {
            "bundle_sha256": bundle_sha256,
            "kind": row["kind"],
            "offset": row["offset"],
        }
    )
    return f"TN-LEX-{digest[:20]}"


def build_target_inventory(
    ast: dict[str, Any],
    lexical: dict[str, Any],
    *,
    target_version: str,
    platform: str,
    ast_binding: dict[str, Any] | None = None,
    lexical_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建无截断、无重复身份的目标 sink 并集。"""

    ast_sinks = _validate_ast(ast)
    lexical_sinks = _validate_lexical(lexical)
    ast_bundle = ast.get("bundle")
    ast_bundle_sha = ast_bundle.get("sha256") if isinstance(ast_bundle, dict) else None
    lexical_bundle_sha = lexical.get("bundle_sha256")
    if not isinstance(ast_bundle_sha, str) or ast_bundle_sha != lexical_bundle_sha:
        raise TargetInventoryError("AST 与词法索引没有绑定同一 bundle")

    lexical_matches: dict[str, list[dict[str, Any]]] = {
        str(row["sink_id"]): [] for row in ast_sinks
    }
    lexical_only: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    for lexical_row in lexical_sinks:
        candidates = [
            row
            for row in ast_sinks
            if int(row["source_start"])
            <= int(lexical_row["offset"])
            < int(row["source_end"])
        ]
        lexical_id = _lexical_id(ast_bundle_sha, lexical_row)
        normalized_lexical = {
            "candidate_id": lexical_id,
            "kind": lexical_row["kind"],
            "offset": lexical_row["offset"],
            "nearest_symbol": lexical_row.get("nearest_symbol"),
        }
        if len(candidates) == 1:
            lexical_matches[str(candidates[0]["sink_id"])].append(normalized_lexical)
        elif not candidates:
            lexical_only.append(normalized_lexical)
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
        sinks.append(
            {
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
        )
    for row in lexical_only:
        semantic = canonical_sha256(
            {
                "candidate_id": row["candidate_id"],
                "kind": row["kind"],
                "source_kind": "lexical_only",
            }
        )
        sinks.append(
            {
                "sink_id": row["candidate_id"],
                "source_kind": "lexical_only",
                "category": f"lexical_candidate_{row['kind']}",
                "semantic_sha256": semantic,
                "source_start": row["offset"],
                "source_end": row["offset"],
                "owner_symbol": row.get("nearest_symbol"),
                "reachability": "unknown",
                "privacy_keys": [],
                "relevant_literals": [],
                "discovery_sources": ["lexical"],
                "lexical_candidates": [row],
            }
        )
    sinks.sort(key=lambda row: str(row["sink_id"]))
    identities = [str(row["sink_id"]) for row in sinks]
    if len(identities) != len(set(identities)):
        raise TargetInventoryError("合并后 sink_id 冲突")

    counts = Counter(str(row["source_kind"]) for row in sinks)
    return {
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
