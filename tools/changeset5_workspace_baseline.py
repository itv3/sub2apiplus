#!/usr/bin/env python3
"""生成或校验变更集 5 开发前的双分类工作区基线。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import subprocess
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "egress" / "consolidation" / "workspace-baseline"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"
RECEIPT_PATH = OUTPUT_DIR / "receipt.json"
TRANSITION_DIR = ROOT / "docs" / "egress" / "consolidation" / "workspace-transition"
TRANSITION_MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
TRANSITION_RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
CHANGESET4_BASE = ROOT / "docs" / "egress" / "source-freeze" / "base-files.sha256"

INCIDENTAL_PATHS = {
    ".vite/vitest/results.json",
    "backend/-h",
}

# 这些路径属于变更集 5 的前置证据或门禁实现，不冒充变更集 4 已验收成果。
PREREQUISITE_PREFIXES = (
    "docs/egress/consolidation/",
    "tools/changeset5_",
    "backend/cmd/egressconflictinventory/",
    "backend/internal/officialegress/receipt_transport_transitions.go",
    "backend/internal/officialegress/catalogdata/changeset4-transport-receipt-transitions.json",
    "backend/internal/service/official_egress_changeset5_",
)
PREREQUISITE_EXACT = {
    ".gitignore",
    "Makefile",
    "backend/internal/officialegress/migration_receipts.go",
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def changeset4_paths() -> tuple[set[str], dict[str, str]]:
    paths: set[str] = set()
    digests: dict[str, str] = {}
    for line in CHANGESET4_BASE.read_text(encoding="utf-8").splitlines():
        digest, path = line.split("  ", 1)
        paths.add(path)
        digests[path] = digest
    return paths, digests


def status_paths() -> set[str]:
    raw = run_git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        text = field.decode("utf-8", errors="strict")
        if len(text) < 4:
            raise RuntimeError(f"无法解析 git status 记录：{text!r}")
        status_code, path = text[:2], text[3:]
        paths.add(path)
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError(f"重命名记录缺少旧路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def reviewed_docs_paths() -> set[str]:
    paths: set[str] = set()
    for name in ("changeset0", "changeset1a", "changeset1b", "changeset1c", "changeset2", "changeset3", "changeset4"):
        root = ROOT / "docs" / name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() or path.is_symlink():
                paths.add(path.relative_to(ROOT).as_posix())
    return paths


def classify(path: str) -> str:
    if path in INCIDENTAL_PATHS:
        return "incidental_non_authoritative_paths"
    if path in PREREQUISITE_EXACT or any(path.startswith(prefix) for prefix in PREREQUISITE_PREFIXES):
        return "changeset5_prerequisite_artifacts"
    return "protected_prior_artifacts"


def entry(path: str, prior_digests: dict[str, str]) -> dict[str, Any]:
    absolute = ROOT / pathlib.PurePosixPath(path)
    result: dict[str, Any] = {
        "path": path,
        "file_type": "absent",
        "mode": "",
        "size": 0,
        "sha256": "",
    }
    if path in prior_digests:
        result["changeset4_base_sha256"] = prior_digests[path]
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return result
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"基线路径禁止符号链接：{path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"基线路径必须是普通文件或明确缺失：{path}")
    raw = absolute.read_bytes()
    result.update(
        file_type="regular",
        mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
        size=len(raw),
        sha256=sha256(raw),
    )
    return result


def build_manifest() -> dict[str, Any]:
    base_paths, prior_digests = changeset4_paths()
    paths = base_paths | status_paths() | reviewed_docs_paths()
    paths.discard(MANIFEST_PATH.relative_to(ROOT).as_posix())
    paths.discard(RECEIPT_PATH.relative_to(ROOT).as_posix())
    groups: dict[str, list[dict[str, Any]]] = {
        "protected_prior_artifacts": [],
        "incidental_non_authoritative_paths": [],
        "changeset5_prerequisite_artifacts": [],
    }
    for path in sorted(paths):
        groups[classify(path)].append(entry(path, prior_digests))
    return {
        "schema_version": "changeset5-workspace-baseline/v1",
        "changeset": "5",
        "classification_upstream_base": "26d894ef4f50645a4bf1030e378ac892f17d0223",
        "observed_remote_head": "825ca7b1fc9335f904bc077f051de815fb61e47f",
        "changeset4_base_manifest": "docs/egress/source-freeze/base-files.sha256",
        "changeset4_base_manifest_sha256": sha256(CHANGESET4_BASE.read_bytes()),
        **groups,
        "rules": [
            "远端观察值不参与本变更集 diff、分类或验收",
            "incidental_non_authoritative_paths 记录摘要但不属于已验收权威成果",
            "changeset5_prerequisite_artifacts 是前置证据或门禁实现，不冒充前序成果",
            "所有已记录路径只允许普通文件或明确缺失，禁止符号链接和文件类型静默变化",
        ],
    }


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")


def state_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_type": item["file_type"],
        "mode": item["mode"],
        "size": item["size"],
        "sha256": item["sha256"],
    }


def transition_metadata(path: str) -> tuple[str, list[str], list[str]]:
    if path in {
        "backend/internal/service/official_egress_changeset3_pairing_audit_test.go",
        "backend/internal/service/official_egress_finalizer_pairing_test.go",
    }:
        return (
            "替代 Executor、旧符号绝迹与 final-wire 门禁生效后删除旧 pairing 门禁",
            ["changeset5:legacy_pairing_gate_retirement"],
            ["docs/egress/consolidation/pairing-gate-retirement.json", "TestChangeset5LegacySymbolsDefinitionsAndCallsAreZero"],
        )
    if path.startswith("backend/cmd/egressscan/"):
        return (
            "登记 scanner 生命周期迁移、移除收据及当前源码锁",
            ["changeset5:scanner_source_lifecycle_transition"],
            ["docs/egress/consolidation/bootstrap-inventory-lock.json", "docs/egress/consolidation/removal-receipts.json"],
        )
    if path in {
        "backend/internal/repository/official_egress_guard.go",
        "backend/internal/repository/openai_oauth_service.go",
        "backend/internal/repository/req_client_pool.go",
        "backend/internal/service/openai_ws_client.go",
        "backend/internal/service/official_egress_transport_adapters.go",
    }:
        return (
            "按冲突单元清单机械迁移官方出站专属 transport/guard declaration",
            ["changeset5:declaration_migration"],
            ["docs/egress/consolidation/conflict-migration-receipt.json", "TestChangeset5ConflictUnitsOnlyShrinkAndPreserveUnrelatedFork"],
        )
    if path == "backend/internal/service/official_egress_changeset5_final_wire_test.go":
        return (
            "建立 original pre、normalized pre 与 post 三段 final-wire 时间链及 mutation 门禁",
            ["changeset5:final_wire_normalization_transition"],
            ["docs/egress/consolidation/final-wire-normalization-transition.json", "TestChangeset5NormalizedPreAppliesOnlyExactOAuthNoiseTransition"],
        )
    if path == "backend/internal/service/official_egress_changeset3_production_final_wire_test.go":
        return (
            "仅在测试采集边界固定 OAuth InvocationID，消除连接生命周期随机噪声",
            ["changeset5:oauth_capture_determinism"],
            ["docs/egress/consolidation/changeset3-source-transition.json", "TestChangeset5NormalizationTransitionRejectsWrongOrExpandedApproval"],
        )
    if path == "backend/internal/officialegress/changeset3_post_identity_authority_final_wire_frozen_test.go":
        return (
            "串联变更集 5 源码迁移 receipt，同时保持变更集 3 历史 manifest 不变",
            ["changeset5:changeset3_source_transition"],
            ["docs/egress/consolidation/changeset3-source-transition.json"],
        )
    if path in {"Makefile", "tools/changeset5_workspace_baseline.py"}:
        return (
            "接入并强化变更集 5 证据闭集与当前状态复算门禁",
            ["changeset5:evidence_gate_integration"],
            ["make check-egress-spec", "TestChangeset5WorkspaceBaselineIsIndependentlyFrozen"],
        )
    if path in {
        "docs/1.md",
        "docs/CODEX_CLI_0145_EGRESS_SPEC.md",
        "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    }:
        return (
            "同步变更集 5 已实施架构、证据口径与复审边界",
            ["changeset5:specification_update"],
            [
                "python3 tools/check_spec_refs.py",
                "docs/egress/consolidation/egress-surface-inventory.json",
            ],
        )
    if path == "tools/check_ledger_completeness.py":
        return (
            "使用严格完整路径结构化比对 52 个出站面并识别迁移后的 WS declaration",
            ["changeset5:egress_surface_gate"],
            ["docs/egress/consolidation/egress-surface-inventory.json", "TestChangeset5SurfaceInventoryIsIndependentlyLocked"],
        )
    if path == "tools/version_leak_baseline.json" or "0145" in path or path.endswith("official_egress_version_leak_ast.json"):
        return (
            "清理版本无关生产符号中的 0145，并收紧到精确证据闭集",
            ["changeset5:version_neutral_symbol_cleanup"],
            ["docs/egress/consolidation/0145-symbol-allowlist.json", "TestChangeset50145AllowlistIsIndependentlyLocked"],
        )
    if path == "backend/internal/service/openai_images_responses.go":
        return (
            "清理 Codex Images Body 组装中的版本化内部标识符，并通过分类 overlay 纳入有效 governable 集合",
            ["changeset5:version_neutral_symbol_cleanup", "changeset5:conflict_classification_overlay"],
            ["docs/egress/consolidation/conflict-classification-amendments.json", "TestChangeset5ConflictOverlayDoesNotHideOtherNonOfficialDrift"],
        )
    if path.endswith("_test.go"):
        return (
            "将旧链行为断言迁入 Executor、WS session、AST 闭集或版本中立测试",
            ["changeset5:replacement_test_migration"],
            ["make check-egress-spec", "go test ./... -count=1"],
        )
    return (
        "迁移版本中立官方出站逻辑并薄化共享接入点，保持非官方 fork 片段不变",
        ["changeset5:official_egress_refactor"],
        ["docs/egress/consolidation/conflict-migration-receipt.json", "make check-egress-spec"],
    )


def build_transition() -> tuple[dict[str, Any], dict[str, Any]]:
    baseline_raw = MANIFEST_PATH.read_bytes()
    baseline = json.loads(baseline_raw)
    baseline_receipt_raw = RECEIPT_PATH.read_bytes()
    entries: list[dict[str, Any]] = []
    counts = {
        "protected_prior_artifacts": 0,
        "incidental_non_authoritative_paths": 0,
        "changeset5_prerequisite_artifacts": 0,
    }
    unchanged = dict.fromkeys(counts, 0)
    for classification in counts:
        for frozen in baseline[classification]:
            current = entry(frozen["path"], {})
            before = state_fields(frozen)
            after = state_fields(current)
            if before == after:
                unchanged[classification] += 1
                continue
            reason, migration_units, machine_proofs = transition_metadata(frozen["path"])
            entries.append(
                {
                    "path": frozen["path"],
                    "classification": classification,
                    "before": before,
                    "after": after,
                    "reason": reason,
                    "migration_units": migration_units,
                    "deletion_allowed": after["file_type"] == "absent",
                    "machine_proofs": machine_proofs,
                }
            )
            counts[classification] += 1
    entries.sort(key=lambda item: item["path"])
    manifest = {
        "schema_version": "changeset5-workspace-transition/v1",
        "changeset": "5",
        "baseline_manifest_path": "docs/egress/consolidation/workspace-baseline/manifest.json",
        "baseline_manifest_sha256": sha256(baseline_raw),
        "baseline_receipt_path": "docs/egress/consolidation/workspace-baseline/receipt.json",
        "baseline_receipt_sha256": sha256(baseline_receipt_raw),
        "frozen_path_count": sum(len(baseline[name]) for name in counts),
        "entry_count": len(entries),
        "entries": entries,
        "rules": [
            "558 个冻结路径逐项复算；当前值必须等于基线值或被唯一 transition 精确覆盖",
            "未变化路径禁止登记 transition；transition 外路径必须与冻结值逐字节一致",
            "删除必须同时满足 before=regular、after=absent、deletion_allowed=true",
            "文件类型、权限、内容、存在状态分别验证；符号链接一律拒绝",
        ],
    }
    manifest_raw = canonical_json(manifest)
    receipt = {
        "schema_version": "changeset5-workspace-transition-receipt/v1",
        "changeset": "5",
        "manifest_path": "docs/egress/consolidation/workspace-transition/manifest.json",
        "manifest_sha256": sha256(manifest_raw),
        "frozen_path_count": manifest["frozen_path_count"],
        "transition_entry_count": len(entries),
        "transition_counts": counts,
        "unchanged_counts": unchanged,
        "deleted_entry_count": sum(item["after"]["file_type"] == "absent" for item in entries),
        "result": "passed",
    }
    return manifest, receipt


def write_transition() -> None:
    manifest, receipt = build_transition()
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    TRANSITION_MANIFEST_PATH.write_bytes(canonical_json(manifest))
    TRANSITION_RECEIPT_PATH.write_bytes(canonical_json(receipt))


def write() -> None:
    manifest = build_manifest()
    manifest_raw = canonical_json(manifest)
    receipt = {
        "schema_version": "changeset5-workspace-baseline-receipt/v1",
        "changeset": "5",
        "manifest_path": "docs/egress/consolidation/workspace-baseline/manifest.json",
        "manifest_sha256": sha256(manifest_raw),
        "protected_prior_count": len(manifest["protected_prior_artifacts"]),
        "incidental_non_authoritative_count": len(manifest["incidental_non_authoritative_paths"]),
        "changeset5_prerequisite_count": len(manifest["changeset5_prerequisite_artifacts"]),
        "file_type_policy": "regular_or_explicit_absent; symlink_forbidden",
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(manifest_raw)
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_structure() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    receipt = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "changeset5-workspace-baseline/v1":
        raise RuntimeError("工作区基线 schema 非法")
    manifest_raw = MANIFEST_PATH.read_bytes()
    if receipt.get("manifest_sha256") != sha256(manifest_raw):
        raise RuntimeError("工作区基线 receipt 与 manifest 摘要不一致")
    expected_counts = {
        "protected_prior_count": len(manifest["protected_prior_artifacts"]),
        "incidental_non_authoritative_count": len(manifest["incidental_non_authoritative_paths"]),
        "changeset5_prerequisite_count": len(manifest["changeset5_prerequisite_artifacts"]),
    }
    for name, expected in expected_counts.items():
        if receipt.get(name) != expected:
            raise RuntimeError(f"工作区基线计数漂移：{name}")
    all_paths: set[str] = set()
    for group in (
        "protected_prior_artifacts",
        "incidental_non_authoritative_paths",
        "changeset5_prerequisite_artifacts",
    ):
        for item in manifest[group]:
            path = item["path"]
            if path in all_paths:
                raise RuntimeError(f"工作区基线路径跨分类重复：{path}")
            all_paths.add(path)
            if item["file_type"] not in {"regular", "absent"}:
                raise RuntimeError(f"工作区基线文件类型非法：{path}")
            if item["file_type"] == "regular" and len(item["sha256"]) != 64:
                raise RuntimeError(f"工作区基线摘要非法：{path}")
    incidental = {item["path"] for item in manifest["incidental_non_authoritative_paths"]}
    if incidental != INCIDENTAL_PATHS:
        raise RuntimeError(f"临时产物分类漂移：{sorted(incidental)}")


def validate_transition() -> None:
    baseline_raw = MANIFEST_PATH.read_bytes()
    baseline = json.loads(baseline_raw)
    transition_raw = TRANSITION_MANIFEST_PATH.read_bytes()
    transition = json.loads(transition_raw)
    receipt = json.loads(TRANSITION_RECEIPT_PATH.read_text(encoding="utf-8"))
    if transition.get("schema_version") != "changeset5-workspace-transition/v1" or transition.get("changeset") != "5":
        raise RuntimeError("工作区 transition schema 或 changeset 非法")
    if transition.get("baseline_manifest_sha256") != sha256(baseline_raw):
        raise RuntimeError("工作区 transition 未绑定冻结 baseline manifest")
    if transition.get("baseline_receipt_sha256") != sha256(RECEIPT_PATH.read_bytes()):
        raise RuntimeError("工作区 transition 未绑定冻结 baseline receipt")
    groups = (
        "protected_prior_artifacts",
        "incidental_non_authoritative_paths",
        "changeset5_prerequisite_artifacts",
    )
    frozen: dict[str, tuple[str, dict[str, Any]]] = {}
    for classification in groups:
        for item in baseline[classification]:
            if item["path"] in frozen:
                raise RuntimeError(f"冻结工作区路径重复：{item['path']}")
            frozen[item["path"]] = (classification, item)
    if len(frozen) != 558 or transition.get("frozen_path_count") != len(frozen):
        raise RuntimeError(f"工作区冻结路径闭集必须严格为 558 项：actual={len(frozen)}")
    transition_entries = transition.get("entries")
    if not isinstance(transition_entries, list) or transition.get("entry_count") != len(transition_entries):
        raise RuntimeError("工作区 transition entries 或计数非法")
    by_path: dict[str, dict[str, Any]] = {}
    for item in transition_entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or item["path"] in by_path:
            raise RuntimeError(f"工作区 transition 路径为空或重复：{item!r}")
        path = item["path"]
        if path not in frozen:
            raise RuntimeError(f"工作区 transition 登记了冻结闭集外路径：{path}")
        if not item.get("reason") or not item.get("migration_units") or not item.get("machine_proofs"):
            raise RuntimeError(f"工作区 transition 缺少原因、迁移单元或机器证明：{path}")
        by_path[path] = item
    actual_counts = dict.fromkeys(groups, 0)
    unchanged_counts = dict.fromkeys(groups, 0)
    deleted_count = 0
    for path, (classification, frozen_item) in frozen.items():
        current = state_fields(entry(path, {}))
        before = state_fields(frozen_item)
        transition_item = by_path.get(path)
        if current == before:
            unchanged_counts[classification] += 1
            if transition_item is not None:
                raise RuntimeError(f"工作区 transition 虚假登记未变化路径：{path}")
            continue
        if transition_item is None:
            raise RuntimeError(f"冻结工作区路径发生未登记变化：{path}")
        if transition_item.get("classification") != classification:
            raise RuntimeError(f"工作区 transition 分类错误：{path}")
        if transition_item.get("before") != before or transition_item.get("after") != current:
            raise RuntimeError(f"工作区 transition 前后状态与实际不一致：{path}")
        deletion_allowed = transition_item.get("deletion_allowed")
        deleted = before["file_type"] == "regular" and current["file_type"] == "absent"
        if deleted:
            deleted_count += 1
            if deletion_allowed is not True:
                raise RuntimeError(f"删除项未显式允许：{path}")
        elif deletion_allowed is not False:
            raise RuntimeError(f"非删除项错误声明 deletion_allowed：{path}")
        actual_counts[classification] += 1
    if len(by_path) != sum(actual_counts.values()):
        raise RuntimeError("工作区 transition 存在未消费条目")
    expected_receipt = {
        "schema_version": "changeset5-workspace-transition-receipt/v1",
        "changeset": "5",
        "manifest_path": "docs/egress/consolidation/workspace-transition/manifest.json",
        "manifest_sha256": sha256(transition_raw),
        "frozen_path_count": len(frozen),
        "transition_entry_count": len(by_path),
        "transition_counts": actual_counts,
        "unchanged_counts": unchanged_counts,
        "deleted_entry_count": deleted_count,
        "result": "passed",
    }
    if receipt != expected_receipt:
        raise RuntimeError("工作区 transition receipt 与当前确定性复算结果不一致")


def self_test() -> None:
    before = {"file_type": "regular", "mode": "0644", "size": 3, "sha256": "a" * 64}
    after = {"file_type": "absent", "mode": "", "size": 0, "sha256": ""}
    valid = {
        "path": "sample.go",
        "classification": "protected_prior_artifacts",
        "before": before,
        "after": after,
        "reason": "测试删除",
        "migration_units": ["changeset5:test"],
        "deletion_allowed": True,
        "machine_proofs": ["mutation"],
    }
    invalid = [
        {**valid, "deletion_allowed": False},
        {**valid, "before": {**before, "file_type": "absent"}},
        {**valid, "after": {**after, "file_type": "regular"}},
    ]
    for item in invalid:
        deleted = item["before"]["file_type"] == "regular" and item["after"]["file_type"] == "absent"
        accepted = (deleted and item["deletion_allowed"] is True) or (
            not deleted and item["deletion_allowed"] is False
        )
        if accepted:
            raise RuntimeError(f"工作区 transition 删除 mutation 未被拒绝：{item}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="生成冻结基线")
    parser.add_argument("--write-transition", action="store_true", help="从最终当前状态确定性生成 transition")
    parser.add_argument("--self-test", action="store_true", help="运行 transition 判据 mutation 自测")
    args = parser.parse_args()
    if args.write:
        write()
    if args.write_transition:
        write_transition()
    if args.self_test:
        self_test()
        print("✅ 变更集 5 工作区 transition 判据 mutation 自测通过")
        return 0
    validate_structure()
    validate_transition()
    print("✅ 变更集 5 工作区 558 项基线与 transition 当前状态闭集有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
