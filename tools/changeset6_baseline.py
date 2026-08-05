#!/usr/bin/env python3
"""生成或校验变更集 6 开发前的非 clean 工作区基线。"""

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
BASELINE_DIR = ROOT / "docs" / "egress" / "validation" / "baseline"
WORKSPACE_MANIFEST = BASELINE_DIR / "workspace-manifest.json"
RELEASE_CATALOG = (
    ROOT
    / "backend"
    / "internal"
    / "officialegress"
    / "catalogdata"
    / "release-catalog.json"
)
FINAL_WIRE_DIR = ROOT / "docs" / "egress" / "consolidation" / "post-refactor-final-wire"

# 这些文件属于变更集 6 自身，不能倒灌进开发前工作区闭集。
CHANGESET6_PREFIXES = ("docs/egress/validation/",)
CHANGESET6_EXACT = {"tools/changeset6_baseline.py"}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def is_changeset6_path(path: str) -> bool:
    return path in CHANGESET6_EXACT or any(path.startswith(prefix) for prefix in CHANGESET6_PREFIXES)


def workspace_status() -> dict[str, str]:
    """返回完整 porcelain 路径闭集；重命名的源、目标均单独冻结。"""

    raw = run_git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.split(b"\0")
    entries: dict[str, str] = {}
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
        if not is_changeset6_path(path):
            entries[path] = status_code
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            historical_path = fields[index].decode("utf-8", errors="strict")
            index += 1
            if not is_changeset6_path(historical_path):
                entries[historical_path] = status_code + ":historical"
    return entries


def file_entry(relative_path: str) -> dict[str, Any]:
    absolute = ROOT / pathlib.PurePosixPath(relative_path)
    entry: dict[str, Any] = {
        "path": relative_path,
        "existence": "absent",
        "file_type": "absent",
        "mode": "",
        "size": 0,
        "sha256": "",
    }
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return entry
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"基线路径禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"基线路径必须是普通文件或明确缺失：{relative_path}")
    raw = absolute.read_bytes()
    entry.update(
        existence="present",
        file_type="regular",
        mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
        size=len(raw),
        sha256=sha256(raw),
    )
    return entry


def release_data_entries() -> list[dict[str, Any]]:
    manifest = json.loads(RELEASE_CATALOG.read_text(encoding="utf-8"))
    graph_path = pathlib.PurePosixPath(
        "backend/internal/officialegress"
    ) / pathlib.PurePosixPath(manifest["release_graph"]["path"])
    snapshot_catalog_path = pathlib.PurePosixPath(
        "backend/internal/officialegress"
    ) / pathlib.PurePosixPath(manifest["snapshot_catalog"]["path"])
    snapshot_catalog = json.loads((ROOT / snapshot_catalog_path).read_text(encoding="utf-8"))
    paths = {
        RELEASE_CATALOG.relative_to(ROOT).as_posix(),
        graph_path.as_posix(),
        snapshot_catalog_path.as_posix(),
    }
    snapshot_root = snapshot_catalog_path.parent
    for snapshot in snapshot_catalog["snapshots"]:
        paths.add((snapshot_root / pathlib.PurePosixPath(snapshot["file"])).as_posix())
    return [file_entry(path) for path in sorted(paths)]


def final_wire_entries() -> list[dict[str, Any]]:
    names = ("manifest.json", "secret-scan.json", "receipt.json")
    return [file_entry((FINAL_WIRE_DIR / name).relative_to(ROOT).as_posix()) for name in names]


def build_workspace_manifest() -> dict[str, Any]:
    statuses = workspace_status()
    paths = sorted(statuses)
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    entries = []
    for path in paths:
        item = file_entry(path)
        item["git_status"] = statuses[path]
        entries.append(item)
    return {
        "schema_version": "changeset6-workspace-baseline/v1",
        "changeset": "6",
        "task_status": "方案有条件通过",
        "head": run_git("rev-parse", "HEAD").decode().strip(),
        "head_tree": run_git("rev-parse", "HEAD^{tree}").decode().strip(),
        "workspace_path_count": len(entries),
        "workspace_path_set_sha256": sha256(path_set_raw),
        "workspace_entries": entries,
        "changeset5_post_final_wire": final_wire_entries(),
        "runtime_release_data": release_data_entries(),
        "rules": [
            "完整冻结开发前 git status --porcelain=v1 -z --untracked-files=all 路径闭集",
            "基线记录工作区实际字节，不以 HEAD 内容冒充已验收非 clean 工作区",
            "每个路径冻结存在状态、普通文件类型、权限、大小和 SHA-256，符号链接禁止",
            "变更集 5 post final-wire 三件套只读引用，禁止重新生成或覆盖",
            "变更集 6 自身目录和基线生成器不倒灌进开发前路径闭集",
        ],
    }


def write_workspace_manifest() -> None:
    if WORKSPACE_MANIFEST.exists():
        raise RuntimeError("工作区基线已存在，禁止按当前状态原地重写")
    manifest = build_workspace_manifest()
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_MANIFEST.write_bytes(canonical_json(manifest))


def validate_workspace_manifest() -> None:
    raw = WORKSPACE_MANIFEST.read_bytes()
    manifest = json.loads(raw)
    if manifest["schema_version"] != "changeset6-workspace-baseline/v1":
        raise RuntimeError("工作区基线 schema 非法")
    entries = manifest["workspace_entries"]
    paths = [entry["path"] for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("工作区基线路径未严格排序或存在重复")
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    if len(paths) != manifest["workspace_path_count"] or sha256(path_set_raw) != manifest[
        "workspace_path_set_sha256"
    ]:
        raise RuntimeError("工作区基线路径闭集指纹不一致")
    if manifest["head"] != "38a9929eac35a39c86de2f27de8f7a805d7dae52":
        raise RuntimeError("工作区基线 HEAD 与审核值不一致")
    if manifest["head_tree"] != "a8c3dee18a01a6138bfcea60860bb5ad11548c3a":
        raise RuntimeError("工作区基线 HEAD tree 与审核值不一致")
    for group in ("workspace_entries", "changeset5_post_final_wire", "runtime_release_data"):
        for item in manifest[group]:
            if item["file_type"] not in {"regular", "absent"}:
                raise RuntimeError(f"基线路径类型非法：{item['path']}")
            if item["file_type"] == "regular" and (
                len(item["sha256"]) != 64 or item["existence"] != "present"
            ):
                raise RuntimeError(f"基线路径摘要非法：{item['path']}")
    print(f"变更集 6 工作区基线校验通过：{sha256(raw)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-workspace", action="store_true")
    args = parser.parse_args()
    if args.write_workspace:
        write_workspace_manifest()
    validate_workspace_manifest()


if __name__ == "__main__":
    main()
