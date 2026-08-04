#!/usr/bin/env python3
"""生成或校验变更集 6 相对开发前非 clean 工作区基线的完整 transition。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import stat
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "docs" / "changeset6" / "baseline" / "workspace-manifest.json"
TRANSITION_DIR = ROOT / "docs" / "changeset6" / "workspace-transition"
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
FROZEN_BASELINE_SHA256 = "c8c6e6452390abd076ae771b17b11bf079a2a3c45359707cf6ac8fb11ed5c760"
FROZEN_MANIFEST_SHA256 = "4835f90e4ee0d45f92de6cf2bf6866310960a48e67127bfc94f1ad6d2076c7a0"
FROZEN_RECEIPT_SHA256 = "f0494d0c016aaaa2a0b49ef7067e47e3a5ed03ae241c9bee174bd266c6592046"
EXCLUDED_PATHS = {
    MANIFEST_PATH.relative_to(ROOT).as_posix(),
    RECEIPT_PATH.relative_to(ROOT).as_posix(),
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def empty_state() -> dict[str, Any]:
    return {
        "existence": "absent",
        "file_type": "absent",
        "mode": "",
        "size": 0,
        "sha256": "",
    }


def state_fields(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "existence": item["existence"],
        "file_type": item["file_type"],
        "mode": item["mode"],
        "size": item["size"],
        "sha256": item["sha256"],
    }


def current_state(relative_path: str) -> dict[str, Any]:
    absolute = ROOT / pathlib.PurePosixPath(relative_path)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return empty_state()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"变更集 6 路径禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"变更集 6 路径必须是普通文件或明确缺失：{relative_path}")
    raw = absolute.read_bytes()
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": len(raw),
        "sha256": sha256(raw),
    }


def head_state(relative_path: str) -> dict[str, Any]:
    raw = run_git("ls-tree", "-z", "HEAD", "--", relative_path)
    if not raw:
        return empty_state()
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError(f"HEAD 路径解析结果不唯一：{relative_path}")
    metadata, actual_path = records[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="strict") != relative_path:
        raise RuntimeError(f"HEAD 路径解析漂移：{relative_path}")
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"HEAD 路径不是受支持的普通文件：{relative_path} ({mode} {object_type})")
    content = run_git("cat-file", "blob", object_id)
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": "0755" if mode == "100755" else "0644",
        "size": len(content),
        "sha256": sha256(content),
    }


def status_paths() -> set[str]:
    """返回当前完整 porcelain 路径闭集；重命名的源、目标均纳入。"""

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
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def changeset6_evidence_paths() -> set[str]:
    """显式枚举被仓库 docs/* 规则忽略的变更集 6 证据，避免 git status 漏记。"""

    evidence_root = ROOT / "docs" / "changeset6"
    paths: set[str] = set()
    if not evidence_root.exists():
        return paths
    for path in evidence_root.rglob("*"):
        if path.is_file() or path.is_symlink():
            relative_path = path.relative_to(ROOT).as_posix()
            if relative_path not in EXCLUDED_PATHS:
                paths.add(relative_path)
    return paths


def load_baseline() -> tuple[bytes, dict[str, Any], dict[str, dict[str, Any]]]:
    raw = BASELINE_PATH.read_bytes()
    baseline = json.loads(raw)
    if baseline.get("schema_version") != "changeset6-workspace-baseline/v1":
        raise RuntimeError("变更集 6 工作区基线 schema 非法")
    entries = baseline.get("workspace_entries")
    if not isinstance(entries, list) or baseline.get("workspace_path_count") != len(entries):
        raise RuntimeError("变更集 6 工作区基线条目或计数非法")
    paths = [item.get("path") for item in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("变更集 6 工作区基线路径未严格排序或存在重复")
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    if sha256(path_set_raw) != baseline.get("workspace_path_set_sha256"):
        raise RuntimeError("变更集 6 工作区基线路径闭集摘要非法")
    return raw, baseline, {item["path"]: state_fields(item) for item in entries}


def scope_of(path: str) -> str:
    if path.startswith("docs/changeset6/") or path.startswith("tools/changeset6_"):
        return "changeset6_evidence"
    if path.startswith("backend/internal/officialegress/catalogdata/runtime/") or path.endswith(
        "runtime_catalog_files.go"
    ):
        return "runtime_catalog"
    if path.startswith("backend/internal/officialegress/"):
        return "officialegress_core"
    if path.startswith("backend/internal/service/"):
        return "service_attempt_boundary"
    if path in {"Makefile", "docs/1.md"}:
        return "spec_and_gate"
    return "changeset6_support"


def build_transition() -> tuple[dict[str, Any], dict[str, Any]]:
    baseline_raw, baseline, frozen = load_baseline()
    candidates = (set(frozen) | status_paths() | changeset6_evidence_paths()) - EXCLUDED_PATHS
    entries: list[dict[str, Any]] = []
    unchanged_baseline_count = 0
    for path in sorted(candidates):
        before = frozen.get(path)
        if before is None:
            before = head_state(path)
        after = current_state(path)
        if before == after:
            if path in frozen:
                unchanged_baseline_count += 1
            continue
        entries.append(
            {
                "path": path,
                "scope": scope_of(path),
                "before": before,
                "after": after,
                "deletion_allowed": before["file_type"] == "regular"
                and after["file_type"] == "absent",
                "reason": "变更集 6 已确认方案、实现或验收证据的确定性工作区迁移",
                "machine_proofs": [
                    "make check-egress-spec",
                    "docs/changeset6/post/acceptance-report.md",
                ],
            }
        )
    paths = [item["path"] for item in entries]
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "changeset6-workspace-transition/v1",
        "changeset": "6",
        "baseline_manifest_path": "docs/changeset6/baseline/workspace-manifest.json",
        "baseline_manifest_sha256": sha256(baseline_raw),
        "baseline_head": baseline["head"],
        "baseline_head_tree": baseline["head_tree"],
        "baseline_workspace_path_count": len(frozen),
        "candidate_path_count": len(candidates),
        "transition_entry_count": len(entries),
        "transition_path_set_sha256": sha256(path_set_raw),
        "entries": entries,
        "rules": [
            "开发前 498 个非 clean 路径、当前完整 git status 路径及被 docs/* 忽略的变更集 6 证据取并集后逐项复算",
            "基线内路径以前置冻结字节为 before，基线外路径以冻结 HEAD tree 为 before",
            "当前状态必须等于 before，或被唯一 transition 精确覆盖；禁止未登记漂移",
            "文件存在状态、类型、权限、大小和 SHA-256 均纳入比较；符号链接禁止",
            "transition manifest 与 receipt 因自引用循环从候选闭集中排除，其余变更集 6 证据全部冻结",
        ],
    }
    manifest_raw = canonical_json(manifest)
    scope_counts: dict[str, int] = {}
    for item in entries:
        scope_counts[item["scope"]] = scope_counts.get(item["scope"], 0) + 1
    receipt = {
        "schema_version": "changeset6-workspace-transition-receipt/v1",
        "changeset": "6",
        "manifest_path": "docs/changeset6/workspace-transition/manifest.json",
        "manifest_sha256": sha256(manifest_raw),
        "baseline_manifest_sha256": sha256(baseline_raw),
        "baseline_workspace_path_count": len(frozen),
        "unchanged_baseline_path_count": unchanged_baseline_count,
        "transition_entry_count": len(entries),
        "added_entry_count": sum(item["before"]["file_type"] == "absent" for item in entries),
        "deleted_entry_count": sum(item["after"]["file_type"] == "absent" for item in entries),
        "scope_counts": dict(sorted(scope_counts.items())),
        "result": "passed",
    }
    return manifest, receipt


def write_transition() -> None:
    manifest, receipt = build_transition()
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(canonical_json(manifest))
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_state(state_value: Any, path: str) -> None:
    if not isinstance(state_value, dict):
        raise RuntimeError(f"工作区 transition 状态不是对象：{path}")
    if state_value == empty_state():
        return
    if (
        state_value.get("existence") != "present"
        or state_value.get("file_type") != "regular"
        or state_value.get("mode") not in {"0644", "0755"}
        or not isinstance(state_value.get("size"), int)
        or state_value["size"] < 0
        or not isinstance(state_value.get("sha256"), str)
        or len(state_value["sha256"]) != 64
    ):
        raise RuntimeError(f"工作区 transition 状态非法：{path}")


def validate_transition() -> None:
    expected_manifest, expected_receipt = build_transition()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_raw)
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    if manifest.get("schema_version") != "changeset6-workspace-transition/v1":
        raise RuntimeError("工作区 transition schema 非法")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("工作区 transition entries 非法")
    paths = [item.get("path") for item in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("工作区 transition 路径未严格排序或存在重复")
    for item in entries:
        path = item["path"]
        validate_state(item.get("before"), path)
        validate_state(item.get("after"), path)
        deleted = item["before"]["file_type"] == "regular" and item["after"]["file_type"] == "absent"
        if item.get("deletion_allowed") is not deleted:
            raise RuntimeError(f"工作区 transition 删除授权非法：{path}")
        if not item.get("reason") or not item.get("machine_proofs"):
            raise RuntimeError(f"工作区 transition 缺少原因或机器证明：{path}")
    if manifest != expected_manifest:
        raise RuntimeError("工作区 transition 与基线及当前状态的确定性复算结果不一致")
    if receipt != expected_receipt:
        raise RuntimeError("工作区 transition receipt 与确定性复算结果不一致")
    print(
        "变更集 6 工作区 transition 当前状态闭集有效："
        f"{len(entries)} 项，manifest SHA-256={sha256(manifest_raw)}"
    )


def validate_frozen_transition() -> None:
    """只验证已接受变更集 6 的历史证据原文，不拿它约束后续工作区。"""

    baseline_raw = BASELINE_PATH.read_bytes()
    manifest_raw = MANIFEST_PATH.read_bytes()
    receipt_raw = RECEIPT_PATH.read_bytes()
    if sha256(baseline_raw) != FROZEN_BASELINE_SHA256:
        raise RuntimeError("变更集 6 工作区基线历史原文漂移")
    if sha256(manifest_raw) != FROZEN_MANIFEST_SHA256:
        raise RuntimeError("变更集 6 工作区 transition 历史原文漂移")
    if sha256(receipt_raw) != FROZEN_RECEIPT_SHA256:
        raise RuntimeError("变更集 6 工作区 transition receipt 历史原文漂移")
    manifest = json.loads(manifest_raw)
    receipt = json.loads(receipt_raw)
    if (
        manifest.get("schema_version") != "changeset6-workspace-transition/v1"
        or receipt.get("schema_version") != "changeset6-workspace-transition-receipt/v1"
        or receipt.get("manifest_sha256") != FROZEN_MANIFEST_SHA256
        or receipt.get("baseline_manifest_sha256") != FROZEN_BASELINE_SHA256
    ):
        raise RuntimeError("变更集 6 工作区 transition 历史元数据非法")
    print("变更集 6 工作区 transition 历史原文与摘要链有效")


def self_test() -> None:
    present = {
        "existence": "present",
        "file_type": "regular",
        "mode": "0644",
        "size": 3,
        "sha256": "a" * 64,
    }
    validate_state(present, "sample.go")
    validate_state(empty_state(), "sample.go")
    mutations = [
        {**present, "file_type": "symlink"},
        {**present, "mode": "0777"},
        {**present, "sha256": "a" * 63},
        {**empty_state(), "existence": "present"},
    ]
    for mutation in mutations:
        try:
            validate_state(mutation, "mutation.go")
        except RuntimeError:
            continue
        raise RuntimeError(f"工作区 transition 状态 mutation 未被拒绝：{mutation}")
    print("变更集 6 工作区 transition 判据 mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成最终 transition")
    parser.add_argument("--self-test", action="store_true", help="运行 transition 判据 mutation 自测")
    parser.add_argument("--frozen-only", action="store_true", help="只验证已接受历史 transition 的原文摘要")
    args = parser.parse_args()
    if args.write_transition:
        write_transition()
    if args.self_test:
        self_test()
        return 0
    if args.frozen_only:
        validate_frozen_transition()
        return 0
    validate_transition()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
