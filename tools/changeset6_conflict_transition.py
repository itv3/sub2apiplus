#!/usr/bin/env python3
"""独立复算并校验变更集 6 冲突单元 transition。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE_DIR = ROOT / "docs" / "changeset5" / "post-refactor-conflict-inventory"
POST_DIR = ROOT / "docs" / "changeset6" / "post-conflict-inventory"
TRANSITION_PATH = POST_DIR / "transition.json"

BASELINE_FULL_SHA256 = "6a3c0a45339da65a01ae2e5d591cb682046f33fe35464583c66155dfe318c816"
BASELINE_GOVERNABLE_SHA256 = "03c2d35555febf086caffdd7ab03625353be5f1146064c19aebfc50b44543118"
BASELINE_RECEIPT_SHA256 = "e263d3b6199f6c411c4c48c4a59c19be773c48e8a61df3d0301fe9b112bcc9b4"
UPSTREAM_BASE = "26d894ef4f50645a4bf1030e378ac892f17d0223"
TRANSITION_REASON = (
    "变更集 6 在既有 WS pool 单元内接入 attempt-owned Body 与可 unwrap RuntimeError；"
    "冲突文件、单元 ID 和可治理单元数量均未扩大"
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"冲突证据顶层必须是对象：{path}")
    return value


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
    expected_full_path: str,
    expected_governable_path: str,
    full_raw: bytes,
    governable_raw: bytes,
    full_count: int,
    governable_count: int,
) -> bytes:
    raw = path.read_bytes()
    receipt = json.loads(raw)
    expected = {
        "schema_version": "changeset5-conflict-unit-inventory-receipt/v1",
        "changeset": "5",
        "full_inventory_path": expected_full_path,
        "full_inventory_sha256": sha256(full_raw),
        "full_unit_count": full_count,
        "governable_inventory_path": expected_governable_path,
        "governable_inventory_sha256": sha256(governable_raw),
        "governable_unit_count": governable_count,
        "governable_is_strict_subset": True,
        "classification_upstream_base": UPSTREAM_BASE,
    }
    if receipt != expected:
        raise RuntimeError(f"冲突 inventory receipt 与文件确定性复算不一致：{path}")
    return raw


def compare_units(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    before_ids = set(before)
    after_ids = set(after)
    common = before_ids & after_ids
    changed = sorted(unit_id for unit_id in common if before[unit_id] != after[unit_id])
    changed_paths = sorted(
        {before[unit_id]["path"] for unit_id in changed}
        | {after[unit_id]["path"] for unit_id in changed}
    )
    return {
        "added_unit_ids": sorted(after_ids - before_ids),
        "removed_unit_ids": sorted(before_ids - after_ids),
        "changed_unit_ids": changed,
        "changed_paths": changed_paths,
    }


def build_transition() -> dict[str, Any]:
    baseline_full_raw, baseline_full, baseline_full_units = inventory(
        BASELINE_DIR / "full.json", "full_upstream_overlap"
    )
    baseline_governable_raw, baseline_governable, baseline_governable_units = inventory(
        BASELINE_DIR / "governable.json", "official_egress_governable_subset"
    )
    post_full_raw, post_full, post_full_units = inventory(
        POST_DIR / "full.json", "full_upstream_overlap"
    )
    post_governable_raw, post_governable, post_governable_units = inventory(
        POST_DIR / "governable.json", "official_egress_governable_subset"
    )

    if (
        sha256(baseline_full_raw) != BASELINE_FULL_SHA256
        or sha256(baseline_governable_raw) != BASELINE_GOVERNABLE_SHA256
    ):
        raise RuntimeError("变更集 5 冲突 inventory 基线摘要漂移")
    if not set(baseline_governable_units) < set(baseline_full_units):
        raise RuntimeError("变更集 5 governable inventory 不是 full 的严格子集")
    if not set(post_governable_units) < set(post_full_units):
        raise RuntimeError("变更集 6 governable inventory 不是 full 的严格子集")

    baseline_receipt_raw = validate_receipt(
        BASELINE_DIR / "receipt.json",
        "docs/changeset5/post-refactor-conflict-inventory/full.json",
        "docs/changeset5/post-refactor-conflict-inventory/governable.json",
        baseline_full_raw,
        baseline_governable_raw,
        len(baseline_full_units),
        len(baseline_governable_units),
    )
    if sha256(baseline_receipt_raw) != BASELINE_RECEIPT_SHA256:
        raise RuntimeError("变更集 5 冲突 inventory receipt 摘要漂移")
    post_receipt_raw = validate_receipt(
        POST_DIR / "receipt.json",
        "docs/changeset6/post-conflict-inventory/full.json",
        "docs/changeset6/post-conflict-inventory/governable.json",
        post_full_raw,
        post_governable_raw,
        len(post_full_units),
        len(post_governable_units),
    )

    full_diff = compare_units(baseline_full_units, post_full_units)
    governable_diff = compare_units(baseline_governable_units, post_governable_units)
    changed_path_closure = sorted(
        set(full_diff["changed_paths"]) | set(governable_diff["changed_paths"])
    )
    return {
        "schema_version": "changeset6-conflict-unit-transition/v2",
        "changeset": "6",
        "baseline": {
            "directory": "docs/changeset5/post-refactor-conflict-inventory",
            "full_sha256": sha256(baseline_full_raw),
            "governable_sha256": sha256(baseline_governable_raw),
            "receipt_sha256": sha256(baseline_receipt_raw),
        },
        "post": {
            "directory": "docs/changeset6/post-conflict-inventory",
            "full_sha256": sha256(post_full_raw),
            "governable_sha256": sha256(post_governable_raw),
            "receipt_sha256": sha256(post_receipt_raw),
        },
        "comparison": {
            "conflict_file_count_before": baseline_full["conflict_file_count"],
            "conflict_file_count_after": post_full["conflict_file_count"],
            "full_unit_count_before": len(baseline_full_units),
            "full_unit_count_after": len(post_full_units),
            "governable_unit_count_before": len(baseline_governable_units),
            "governable_unit_count_after": len(post_governable_units),
            "full_added_unit_ids": full_diff["added_unit_ids"],
            "full_removed_unit_ids": full_diff["removed_unit_ids"],
            "governable_added_unit_ids": governable_diff["added_unit_ids"],
            "governable_removed_unit_ids": governable_diff["removed_unit_ids"],
            "changed_full_unit_count": len(full_diff["changed_unit_ids"]),
            "changed_full_unit_ids": full_diff["changed_unit_ids"],
            "changed_governable_unit_count": len(governable_diff["changed_unit_ids"]),
            "changed_governable_unit_ids": governable_diff["changed_unit_ids"],
            "changed_path_closure": changed_path_closure,
        },
        "reason": TRANSITION_REASON,
        "result": "passed",
    }


def validate_payload(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual != expected:
        raise RuntimeError("冲突 transition 与 baseline/post 的独立确定性复算不一致")


def write_transition() -> None:
    TRANSITION_PATH.write_bytes(canonical_json(build_transition()))


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
        or actual["result"] != "passed"
    ):
        raise RuntimeError("变更集 6 冲突单元 ID 集合发生增删")
    print(
        "变更集 6 冲突 transition 独立复算通过："
        f"full {comparison['full_unit_count_before']}→{comparison['full_unit_count_after']}，"
        f"governable {comparison['governable_unit_count_before']}→"
        f"{comparison['governable_unit_count_after']}，"
        f"变化路径={comparison['changed_path_closure']}，SHA-256={sha256(raw)}"
    )


def self_test() -> None:
    expected = build_transition()
    mutations: list[dict[str, Any]] = []
    wrong_baseline = copy.deepcopy(expected)
    wrong_baseline["baseline"]["full_sha256"] = "0" * 64
    mutations.append(wrong_baseline)
    fake_count = copy.deepcopy(expected)
    fake_count["comparison"]["changed_full_unit_count"] += 1
    mutations.append(fake_count)
    expanded_path = copy.deepcopy(expected)
    expanded_path["comparison"]["changed_path_closure"].append("backend/other.go")
    mutations.append(expanded_path)
    wrong_receipt = copy.deepcopy(expected)
    wrong_receipt["post"]["receipt_sha256"] = "f" * 64
    mutations.append(wrong_receipt)
    for mutation in mutations:
        try:
            validate_payload(mutation, expected)
        except RuntimeError:
            continue
        raise RuntimeError("冲突 transition 非法 mutation 未被拒绝")

    equal_count_replacement = compare_units(
        {"unit-a": {"id": "unit-a", "path": "same.go", "value": 1}},
        {"unit-b": {"id": "unit-b", "path": "same.go", "value": 1}},
    )
    if equal_count_replacement["added_unit_ids"] != ["unit-b"] or equal_count_replacement[
        "removed_unit_ids"
    ] != ["unit-a"]:
        raise RuntimeError("等量替换单元 ID 未被识别为一增一删")
    print("变更集 6 冲突 transition baseline、ID、计数、路径和 receipt mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="从冻结 baseline 与 post 生成 transition")
    parser.add_argument("--self-test", action="store_true", help="运行 transition 判据 mutation")
    args = parser.parse_args()
    if args.write:
        write_transition()
    if args.self_test:
        self_test()
        return 0
    validate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
