#!/usr/bin/env python3
"""生成或校验官方出站单文档与兼容层退休变更相对基准提交的完整工作区 transition。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import stat
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RETIREMENT_RECEIPT_PATH = (
    ROOT / "docs" / "egress" / "maintenance" / "official-egress-consolidation-retirement.json"
)
TRANSITION_DIR = ROOT / "docs" / "egress" / "maintenance" / "workspace-transition"
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
FROZEN_MANIFEST_SHA256 = "24aef4dfd748c49f831c38c3910064902517bfeca9e8c8e1b862b22a571449c4"
FROZEN_RECEIPT_SHA256 = "718325f294187b1d6d156ca095c5922493c5847e1d51ded154ed86cfb5a472bb"
FROZEN_RETIREMENT_SHA256 = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"
# 发版流水线 release.yml 的 sync-version-file 作业在每次发 tag 后自动改写该文件并推回
# 默认分支，其内容不由人工提交产生；若纳入登记，冻结的哈希会在下一次发版立即过期并使
# 门禁失败。该路径 scope 为 repository_support，与官方出站画像和规格封闭无关，故与自
# 引用的 manifest／receipt 一并排除。
VERSION_PATH = ROOT / "backend" / "cmd" / "server" / "VERSION"
EXCLUDED_PATHS = {
    MANIFEST_PATH.relative_to(ROOT).as_posix(),
    RECEIPT_PATH.relative_to(ROOT).as_posix(),
    VERSION_PATH.relative_to(ROOT).as_posix(),
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


def current_state(relative_path: str) -> dict[str, Any]:
    absolute = ROOT / pathlib.PurePosixPath(relative_path)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return empty_state()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"维护 transition 禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"维护 transition 路径必须是普通文件或明确缺失：{relative_path}")
    raw = absolute.read_bytes()
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": len(raw),
        "sha256": sha256(raw),
    }


def commit_state(commit: str, relative_path: str) -> dict[str, Any]:
    raw = run_git("ls-tree", "-z", commit, "--", relative_path)
    if not raw:
        return empty_state()
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError(f"基准提交路径解析结果不唯一：{relative_path}")
    metadata, actual_path = records[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="strict") != relative_path:
        raise RuntimeError(f"基准提交路径解析漂移：{relative_path}")
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"基准提交路径不是受支持的普通文件：{relative_path}")
    content = run_git("cat-file", "blob", object_id)
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": "0755" if mode == "100755" else "0644",
        "size": len(content),
        "sha256": sha256(content),
    }


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
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def committed_paths(base_commit: str) -> set[str]:
    raw = run_git("diff", "--name-only", "-z", f"{base_commit}..HEAD")
    return {
        value.decode("utf-8", errors="strict")
        for value in raw.split(b"\0")
        if value
    }


def validate_state(value: Any, path: str) -> None:
    if value == empty_state():
        return
    if (
        not isinstance(value, dict)
        or value.get("existence") != "present"
        or value.get("file_type") != "regular"
        or value.get("mode") not in {"0644", "0755"}
        or not isinstance(value.get("size"), int)
        or value["size"] < 0
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
    ):
        raise RuntimeError(f"维护 transition 状态非法：{path}")


def scope_of(path: str) -> str:
    if path.startswith("docs/egress/maintenance/") or path in {
        "docs/CODEX_CLI_0145_EGRESS_SPEC.md",
        "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    }:
        return "documentation_and_receipts"
    if path.startswith("backend/internal/officialegress/"):
        return "officialegress_core"
    if path.startswith("backend/internal/service/"):
        return "service_boundary"
    if path.startswith("backend/cmd/egressscan/") or path.startswith("tools/") or path == "Makefile":
        return "source_gate"
    return "repository_support"


def load_retirement_receipt() -> tuple[bytes, dict[str, Any]]:
    raw = RETIREMENT_RECEIPT_PATH.read_bytes()
    receipt = json.loads(raw)
    if (
        receipt.get("schema_version") != "official-egress-consolidation-retirement/v1"
        or not receipt.get("base_commit")
    ):
        raise RuntimeError("官方出站维护退休收据非法")
    return raw, receipt


def build_transition() -> tuple[dict[str, Any], dict[str, Any]]:
    retirement_raw, retirement = load_retirement_receipt()
    base_commit = retirement["base_commit"]
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("维护 transition 基准提交不是当前 HEAD 的祖先")
    candidates = (committed_paths(base_commit) | status_paths()) - EXCLUDED_PATHS
    entries: list[dict[str, Any]] = []
    for path in sorted(candidates):
        before = commit_state(base_commit, path)
        after = current_state(path)
        if before == after:
            continue
        entries.append(
            {
                "path": path,
                "scope": scope_of(path),
                "before": before,
                "after": after,
                "deletion_allowed": before["file_type"] == "regular"
                and after["file_type"] == "absent",
                "reason": "官方出站单文档合并与已失效执行兼容层退休",
                "machine_proofs": [
                    "docs/egress/maintenance/official-egress-consolidation-retirement.json",
                    "make check-egress-spec",
                ],
            }
        )
    paths = [entry["path"] for entry in entries]
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "official-egress-maintenance-workspace-transition/v1",
        "base_commit": base_commit,
        "retirement_receipt": RETIREMENT_RECEIPT_PATH.relative_to(ROOT).as_posix(),
        "retirement_receipt_sha256": sha256(retirement_raw),
        "candidate_path_count": len(candidates),
        "transition_entry_count": len(entries),
        "transition_path_set_sha256": sha256(path_set_raw),
        "entries": entries,
        "rules": [
            "基准提交后的已提交路径与当前完整 git status 路径取并集",
            "before 固定来自基准提交，after 来自当前普通文件或明确缺失",
            "存在状态、类型、权限、大小和 SHA-256 全部纳入比较",
            "manifest 与 receipt 因自引用循环排除，VERSION 因发版流水线自动回写排除，"
            "其余变化必须唯一登记",
        ],
    }
    manifest_raw = canonical_json(manifest)
    scope_counts: dict[str, int] = {}
    for entry in entries:
        scope_counts[entry["scope"]] = scope_counts.get(entry["scope"], 0) + 1
    receipt = {
        "schema_version": "official-egress-maintenance-workspace-transition-receipt/v1",
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(manifest_raw),
        "base_commit": base_commit,
        "retirement_receipt_sha256": sha256(retirement_raw),
        "transition_entry_count": len(entries),
        "added_entry_count": sum(entry["before"]["file_type"] == "absent" for entry in entries),
        "deleted_entry_count": sum(entry["after"]["file_type"] == "absent" for entry in entries),
        "scope_counts": dict(sorted(scope_counts.items())),
        "result": "passed",
    }
    return manifest, receipt


def write_transition() -> None:
    manifest, receipt = build_transition()
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(canonical_json(manifest))
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_transition() -> None:
    expected_manifest, expected_receipt = build_transition()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_raw)
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("维护 transition entries 非法")
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("维护 transition 路径未严格排序或存在重复")
    for entry in entries:
        path = entry["path"]
        validate_state(entry.get("before"), path)
        validate_state(entry.get("after"), path)
        deleted = entry["before"]["file_type"] == "regular" and entry["after"]["file_type"] == "absent"
        if entry.get("deletion_allowed") is not deleted:
            raise RuntimeError(f"维护 transition 删除授权非法：{path}")
        if not entry.get("reason") or not entry.get("machine_proofs"):
            raise RuntimeError(f"维护 transition 缺少原因或机器证明：{path}")
    if manifest != expected_manifest or receipt != expected_receipt:
        raise RuntimeError("维护 transition 与基准提交及当前状态的确定性复算结果不一致")
    print(
        "官方出站维护工作区 transition 有效："
        f"{len(entries)} 项，manifest SHA-256={sha256(manifest_raw)}"
    )


def validate_frozen_transition() -> None:
    """只复核已接受历史原文与摘要链，不把后继变更吸收到旧收据。"""

    retirement_raw = RETIREMENT_RECEIPT_PATH.read_bytes()
    manifest_raw = MANIFEST_PATH.read_bytes()
    receipt_raw = RECEIPT_PATH.read_bytes()
    if sha256(retirement_raw) != FROZEN_RETIREMENT_SHA256:
        raise RuntimeError("官方出站维护退休收据历史原文漂移")
    if sha256(manifest_raw) != FROZEN_MANIFEST_SHA256:
        raise RuntimeError("官方出站维护工作区 transition 历史原文漂移")
    if sha256(receipt_raw) != FROZEN_RECEIPT_SHA256:
        raise RuntimeError("官方出站维护工作区 transition receipt 历史原文漂移")
    manifest = json.loads(manifest_raw)
    receipt = json.loads(receipt_raw)
    if (
        manifest.get("schema_version")
        != "official-egress-maintenance-workspace-transition/v1"
        or receipt.get("schema_version")
        != "official-egress-maintenance-workspace-transition-receipt/v1"
        or receipt.get("manifest_sha256") != FROZEN_MANIFEST_SHA256
        or receipt.get("retirement_receipt_sha256") != FROZEN_RETIREMENT_SHA256
        or manifest.get("retirement_receipt_sha256") != FROZEN_RETIREMENT_SHA256
        or receipt.get("transition_entry_count") != len(manifest.get("entries", []))
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("官方出站维护工作区 transition 历史摘要链非法")
    print("官方出站维护工作区 transition 历史原文与摘要链有效")


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
    for mutation in (
        {**present, "file_type": "symlink"},
        {**present, "mode": "0777"},
        {**present, "sha256": "a" * 63},
        {**empty_state(), "existence": "present"},
    ):
        try:
            validate_state(mutation, "mutation.go")
        except RuntimeError:
            continue
        raise RuntimeError(f"维护 transition mutation 未被拒绝：{mutation}")
    print("官方出站维护工作区 transition 判据 mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成维护 transition")
    parser.add_argument("--self-test", action="store_true", help="运行 transition 判据 mutation 自测")
    parser.add_argument(
        "--frozen-only",
        action="store_true",
        help="只用当前 HEAD 复核既有历史 transition，不吸收后继工作区变更",
    )
    args = parser.parse_args()
    if args.write_transition:
        if args.frozen_only:
            raise RuntimeError("--write-transition 不能与 --frozen-only 同时使用")
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
