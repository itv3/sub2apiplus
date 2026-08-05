#!/usr/bin/env python3
"""独立复算并校验官方出站单文档与兼容层退休的上游冲突单元 transition。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_DIR = ROOT / "docs" / "egress" / "validation" / "post-conflict-inventory"
POST_DIR = ROOT / "docs" / "egress" / "maintenance" / "post-conflict-inventory"
TRANSITION_PATH = POST_DIR / "transition.json"
UPSTREAM_BASE = "26d894ef4f50645a4bf1030e378ac892f17d0223"
BASE_FULL_SHA256 = "40719b6a58208b368b2b0a99657f8ab5871cc6aed54168f4074152a03d4860da"
BASE_GOVERNABLE_SHA256 = "5f5a595c5334b856220e97861767841475606f6a75bcef6cb9f41427eb8c3caa"
BASE_RECEIPT_SHA256 = "e3b2836e5b9811f8ceaefa6ee868642ee4dc8b528189e18368fffe70f607802c"
ALLOWED_CHANGED_PATHS = {
    "backend/internal/service/account_test_service.go",
    "backend/internal/service/account_usage_service.go",
    "backend/internal/service/openai_alpha_search.go",
    "backend/internal/service/openai_quota_service.go",
    "backend/internal/service/openai_upstream_http.go",
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def inventory(path: pathlib.Path, kind: str) -> tuple[bytes, dict[str, Any], dict[str, dict[str, Any]]]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        value.get("schema_version") != "changeset5-conflict-unit-inventory/v1"
        or value.get("inventory_kind") != kind
        or value.get("classification_upstream_base") != UPSTREAM_BASE
    ):
        raise RuntimeError(f"冲突 inventory schema、kind 或 upstream base 非法：{path}")
    units = value.get("units")
    if not isinstance(units, list) or value.get("unit_count") != len(units):
        raise RuntimeError(f"冲突 inventory units 或计数非法：{path}")
    by_id: dict[str, dict[str, Any]] = {}
    for unit in units:
        if (
            not isinstance(unit, dict)
            or not isinstance(unit.get("id"), str)
            or not isinstance(unit.get("path"), str)
            or unit["id"] in by_id
        ):
            raise RuntimeError(f"冲突 inventory 单元 ID／路径为空或重复：{path}")
        by_id[unit["id"]] = unit
    if [unit["id"] for unit in units] != sorted(by_id):
        raise RuntimeError(f"冲突 inventory 单元未按 ID 严格排序：{path}")
    if value.get("conflict_file_count") != len({unit["path"] for unit in units}):
        raise RuntimeError(f"冲突 inventory 文件计数不是单元路径闭集：{path}")
    return raw, value, by_id


def validate_receipt(
    path: pathlib.Path,
    expected_directory: str,
    full_raw: bytes,
    governable_raw: bytes,
    full_count: int,
    governable_count: int,
) -> bytes:
    raw = path.read_bytes()
    value = json.loads(raw)
    expected = {
        "schema_version": "changeset5-conflict-unit-inventory-receipt/v1",
        "changeset": "5",
        "full_inventory_path": f"{expected_directory}/full.json",
        "full_inventory_sha256": sha256(full_raw),
        "full_unit_count": full_count,
        "governable_inventory_path": f"{expected_directory}/governable.json",
        "governable_inventory_sha256": sha256(governable_raw),
        "governable_unit_count": governable_count,
        "governable_is_strict_subset": True,
        "classification_upstream_base": UPSTREAM_BASE,
    }
    if value != expected:
        raise RuntimeError(f"冲突 inventory receipt 与文件确定性复算不一致：{path}")
    return raw


def compare_units(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, Any]:
    before_ids = set(before)
    after_ids = set(after)
    changed = sorted(
        unit_id for unit_id in before_ids & after_ids if before[unit_id] != after[unit_id]
    )
    return {
        "added_unit_ids": sorted(after_ids - before_ids),
        "removed_unit_ids": sorted(before_ids - after_ids),
        "changed_unit_ids": changed,
        "changed_paths": sorted(
            {before[unit_id]["path"] for unit_id in changed}
            | {after[unit_id]["path"] for unit_id in changed}
        ),
    }


def build_transition() -> dict[str, Any]:
    base_full_raw, base_full, base_full_units = inventory(
        BASE_DIR / "full.json", "full_upstream_overlap"
    )
    base_governable_raw, base_governable, base_governable_units = inventory(
        BASE_DIR / "governable.json", "official_egress_governable_subset"
    )
    post_full_raw, post_full, post_full_units = inventory(
        POST_DIR / "full.json", "full_upstream_overlap"
    )
    post_governable_raw, post_governable, post_governable_units = inventory(
        POST_DIR / "governable.json", "official_egress_governable_subset"
    )
    if sha256(base_full_raw) != BASE_FULL_SHA256 or sha256(base_governable_raw) != BASE_GOVERNABLE_SHA256:
        raise RuntimeError("变更集 6 post 冲突 inventory 摘要漂移")
    base_receipt_raw = validate_receipt(
        BASE_DIR / "receipt.json",
        "docs/changeset6/post-conflict-inventory",
        base_full_raw,
        base_governable_raw,
        len(base_full_units),
        len(base_governable_units),
    )
    if sha256(base_receipt_raw) != BASE_RECEIPT_SHA256:
        raise RuntimeError("变更集 6 post 冲突 inventory receipt 摘要漂移")
    post_receipt_raw = validate_receipt(
        POST_DIR / "receipt.json",
        "docs/maintenance/post-conflict-inventory",
        post_full_raw,
        post_governable_raw,
        len(post_full_units),
        len(post_governable_units),
    )
    if not set(post_governable_units) < set(post_full_units):
        raise RuntimeError("维护 governable inventory 不是 full 的严格子集")
    full_diff = compare_units(base_full_units, post_full_units)
    governable_diff = compare_units(base_governable_units, post_governable_units)
    changed_paths = sorted(set(full_diff["changed_paths"]) | set(governable_diff["changed_paths"]))
    return {
        "schema_version": "official-egress-maintenance-conflict-transition/v1",
        "base": {
            "directory": "docs/changeset6/post-conflict-inventory",
            "full_sha256": sha256(base_full_raw),
            "governable_sha256": sha256(base_governable_raw),
            "receipt_sha256": sha256(base_receipt_raw),
        },
        "post": {
            "directory": "docs/maintenance/post-conflict-inventory",
            "full_sha256": sha256(post_full_raw),
            "governable_sha256": sha256(post_governable_raw),
            "receipt_sha256": sha256(post_receipt_raw),
        },
        "comparison": {
            "conflict_file_count_before": base_full["conflict_file_count"],
            "conflict_file_count_after": post_full["conflict_file_count"],
            "full_unit_count_before": len(base_full_units),
            "full_unit_count_after": len(post_full_units),
            "governable_unit_count_before": len(base_governable_units),
            "governable_unit_count_after": len(post_governable_units),
            "full_added_unit_ids": full_diff["added_unit_ids"],
            "full_removed_unit_ids": full_diff["removed_unit_ids"],
            "governable_added_unit_ids": governable_diff["added_unit_ids"],
            "governable_removed_unit_ids": governable_diff["removed_unit_ids"],
            "changed_full_unit_ids": full_diff["changed_unit_ids"],
            "changed_governable_unit_ids": governable_diff["changed_unit_ids"],
            "changed_path_closure": changed_paths,
        },
        "reason": "只修改稳定策略来源和通用 HTTP 入口的 legacy fail-close；冲突文件与单元 ID 集合不扩大。",
        "result": "passed",
    }


def validate_payload(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual != expected:
        raise RuntimeError("维护冲突 transition 与 base/post 的独立复算不一致")


def validate() -> None:
    expected = build_transition()
    raw = TRANSITION_PATH.read_bytes()
    actual = json.loads(raw)
    validate_payload(actual, expected)
    comparison = actual["comparison"]
    if (
        comparison["full_added_unit_ids"]
        or comparison["full_removed_unit_ids"]
        or comparison["governable_added_unit_ids"]
        or comparison["governable_removed_unit_ids"]
        or not set(comparison["changed_path_closure"]) <= ALLOWED_CHANGED_PATHS
        or actual["result"] != "passed"
    ):
        raise RuntimeError("维护冲突单元 ID 集合扩大或变化路径越界")
    print(
        "官方出站维护冲突 transition 通过："
        f"full {comparison['full_unit_count_before']}→{comparison['full_unit_count_after']}，"
        f"governable {comparison['governable_unit_count_before']}→"
        f"{comparison['governable_unit_count_after']}，"
        f"变化路径={comparison['changed_path_closure']}，SHA-256={sha256(raw)}"
    )


def self_test() -> None:
    expected = build_transition()
    mutations: list[dict[str, Any]] = []
    wrong_base = copy.deepcopy(expected)
    wrong_base["base"]["full_sha256"] = "0" * 64
    mutations.append(wrong_base)
    fake_unit = copy.deepcopy(expected)
    fake_unit["comparison"]["full_added_unit_ids"] = ["fake"]
    mutations.append(fake_unit)
    expanded_path = copy.deepcopy(expected)
    expanded_path["comparison"]["changed_path_closure"].append("backend/other.go")
    mutations.append(expanded_path)
    for mutation in mutations:
        try:
            validate_payload(mutation, expected)
        except RuntimeError:
            continue
        raise RuntimeError("维护冲突 transition 非法 mutation 未被拒绝")
    print("官方出站维护冲突 transition 判据 mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成维护冲突 transition")
    parser.add_argument("--self-test", action="store_true", help="运行维护冲突 transition mutation 自测")
    args = parser.parse_args()
    if args.write_transition:
        TRANSITION_PATH.write_bytes(canonical_json(build_transition()))
    if args.self_test:
        self_test()
        return 0
    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
