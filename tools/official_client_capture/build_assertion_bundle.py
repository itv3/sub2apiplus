#!/usr/bin/env python3
"""ACC-02 断言证据包：把多 job 根的断言相关原始文件确定性收口到单一 bundle。

采集多根而断言单根会造成路径空间分裂。本工具不扩展断言器的多根协议，而是在
seal 前把断言相关原始文件以**只读字节复制**收口到本侧 attempt 下的独立
``assertion-bundle/``，并产出 provenance 收据：逐项绑定来源 inventory 逻辑
路径、来源摘要、目标相对路径与目标摘要，复制前后摘要必须一致。

边界：

- 禁止符号链接与硬链接（来源、沿途目录、bundle 内一律拒绝）；
- 相对路径禁止逃逸（绝对路径、``..``、反斜杠、空段全部拒绝）；
- bundle 目录必须全新创建，目标文件写后置为只读；
- ``--verify`` 重放 provenance：来源漂移、复制后漂移、bundle 内多余或缺失
  文件、收据摘要不符都失败关闭——该重放即 ACC-03 seal 门禁的输入。

本工具只复制，不派生、不判断证据语义；派生（ACC-02b）与 manifest 语义校验
（ACC-03）各有其权威。``verify_manifest_kind_coverage`` 供 seal 校验分侧
场景×kind 覆盖矩阵（矩阵由 acceptance_contract 从批准画像机器推导）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

BUNDLE_PROVENANCE_SCHEMA = "codex-assertion-bundle-provenance/v1"
PROVENANCE_FILENAME = "provenance.json"

_ROOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AssertionBundleError(RuntimeError):
    """证据包收口或重放失败，必须失败关闭。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssertionBundleError(f"{label} 必须是非空相对路径")
    if value.startswith("/") or "\\" in value:
        raise AssertionBundleError(f"{label} 禁止绝对路径或反斜杠：{value}")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise AssertionBundleError(f"{label} 存在路径逃逸或空段：{value}")
    return value


def _assert_regular_source(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise AssertionBundleError(f"{label} 不存在：{path}") from error
    if path.is_symlink():
        raise AssertionBundleError(f"{label} 禁止符号链接：{path}")
    if not path.is_file():
        raise AssertionBundleError(f"{label} 必须是普通文件：{path}")
    if metadata.st_nlink != 1:
        raise AssertionBundleError(f"{label} 禁止硬链接：{path}")


def make_private_parents(root: Path, relative: str) -> None:
    """逐层创建目标父目录并置 0700。

    `Path.mkdir(parents=True, mode=...)` 的 mode 只作用于最末一层，中间层沿用
    umask 默认值（常见为 0755）。bundle 位于 attempt 证据根内，任何一层对
    group/other 开放都会让 seal 的 `_evidence_permissions_private` 拒绝封存。
    """

    current = root
    for segment in relative.split("/")[:-1]:
        current = current / segment
        if current.is_symlink():
            raise AssertionBundleError(f"目标路径沿途存在符号链接：{current}")
        if not current.exists():
            current.mkdir(mode=0o700)
        else:
            current.chmod(0o700)


def _assert_no_symlink_segments(root: Path, relative: str, label: str) -> None:
    current = root
    for segment in relative.split("/"):
        current = current / segment
        if current.is_symlink():
            raise AssertionBundleError(
                f"{label} 沿途存在符号链接：{current}"
            )


def parse_source_roots(pairs: Iterable[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for pair in pairs:
        name, separator, raw_path = pair.partition("=")
        if not separator:
            raise AssertionBundleError(
                f"--source-root 必须是 name=path 形式：{pair}"
            )
        if not _ROOT_NAME_RE.match(name):
            raise AssertionBundleError(f"证据根名称非法：{name}")
        if name in roots:
            raise AssertionBundleError(f"证据根名称重复：{name}")
        path = Path(raw_path)
        if path.is_symlink() or not path.is_dir():
            raise AssertionBundleError(
                f"证据根必须是存在的普通目录：{raw_path}"
            )
        roots[name] = path
    if not roots:
        raise AssertionBundleError("至少需要一个 --source-root")
    return roots


def load_plan(path: Path) -> list[dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AssertionBundleError(f"无法读取收口清单：{error}") from error
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise AssertionBundleError("收口清单 entries 必须是非空数组")
    seen_targets: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    plan: list[dict[str, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"root", "path", "target"}:
            raise AssertionBundleError(f"收口清单第 {index} 项字段不闭合")
        root = entry["root"]
        if not isinstance(root, str) or not _ROOT_NAME_RE.match(root):
            raise AssertionBundleError(f"收口清单第 {index} 项 root 非法")
        source_path = validate_relative_path(entry["path"], f"清单第 {index} 项 path")
        target = validate_relative_path(entry["target"], f"清单第 {index} 项 target")
        if target == PROVENANCE_FILENAME:
            raise AssertionBundleError("target 不得占用 provenance 收据路径")
        source_key = (root, source_path)
        if source_key in seen_sources:
            raise AssertionBundleError(f"来源重复收口：{root}/{source_path}")
        if target in seen_targets:
            raise AssertionBundleError(f"目标路径重复：{target}")
        seen_sources.add(source_key)
        seen_targets.add(target)
        plan.append({"root": root, "path": source_path, "target": target})
    return plan


def build_bundle(
    source_roots: Mapping[str, Path],
    plan: list[dict[str, str]],
    bundle_dir: Path,
) -> dict[str, Any]:
    if bundle_dir.exists():
        raise AssertionBundleError(f"bundle 目录必须全新创建：{bundle_dir}")
    # bundle 位于 attempt 证据根内，必须满足原始证据权限门禁（目录 0700／
    # 文件 0600 以内），否则 seal 的 _evidence_permissions_private 会拒绝封存。
    bundle_dir.mkdir(parents=True, mode=0o700)
    records: list[dict[str, str]] = []
    for entry in plan:
        root_name = entry["root"]
        if root_name not in source_roots:
            raise AssertionBundleError(f"收口清单引用未声明的证据根：{root_name}")
        root = source_roots[root_name]
        _assert_no_symlink_segments(root, entry["path"], "来源文件")
        source = root / entry["path"]
        _assert_regular_source(source, "来源文件")
        source_sha256 = _file_sha256(source)
        target = bundle_dir / entry["target"]
        make_private_parents(bundle_dir, entry["target"])
        if target.exists() or target.is_symlink():
            raise AssertionBundleError(f"目标已存在：{entry['target']}")
        target.write_bytes(source.read_bytes())
        target.chmod(0o400)
        target_sha256 = _file_sha256(target)
        if target_sha256 != source_sha256:
            raise AssertionBundleError(
                f"复制前后摘要不一致：{root_name}/{entry['path']}"
            )
        if _file_sha256(source) != source_sha256:
            raise AssertionBundleError(
                f"复制期间来源发生漂移：{root_name}/{entry['path']}"
            )
        records.append(
            {
                "source_root": root_name,
                "source_path": entry["path"],
                "source_inventory_path": f"{root_name}/{entry['path']}",
                "source_sha256": source_sha256,
                "target_path": entry["target"],
                "target_sha256": target_sha256,
            }
        )
    records.sort(key=lambda record: record["target_path"])
    provenance = {
        "schema_version": BUNDLE_PROVENANCE_SCHEMA,
        "entry_count": len(records),
        "entries": records,
    }
    provenance["provenance_sha256"] = _canonical_sha256(
        {"entries": records, "schema_version": BUNDLE_PROVENANCE_SCHEMA}
    )
    provenance_path = bundle_dir / PROVENANCE_FILENAME
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    provenance_path.chmod(0o400)
    return provenance


def load_provenance(bundle_dir: Path) -> dict[str, Any]:
    path = bundle_dir / PROVENANCE_FILENAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AssertionBundleError(f"无法读取 provenance 收据：{error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != BUNDLE_PROVENANCE_SCHEMA
        or not isinstance(document.get("entries"), list)
        or not document["entries"]
        or document.get("entry_count") != len(document["entries"])
    ):
        raise AssertionBundleError("provenance 收据结构非法")
    expected = _canonical_sha256(
        {
            "entries": document["entries"],
            "schema_version": BUNDLE_PROVENANCE_SCHEMA,
        }
    )
    if document.get("provenance_sha256") != expected:
        raise AssertionBundleError("provenance 收据摘要不符")
    return document


def verify_bundle(
    source_roots: Mapping[str, Path],
    bundle_dir: Path,
    *,
    allowed_extra_prefixes: Iterable[str] = (),
) -> dict[str, Any]:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise AssertionBundleError(f"bundle 目录不存在或不可信：{bundle_dir}")
    provenance = load_provenance(bundle_dir)
    expected_targets: set[str] = set()
    for entry in provenance["entries"]:
        if not isinstance(entry, dict) or set(entry) != {
            "source_root",
            "source_path",
            "source_inventory_path",
            "source_sha256",
            "target_path",
            "target_sha256",
        }:
            raise AssertionBundleError("provenance 条目字段不闭合")
        root_name = entry["source_root"]
        source_path = validate_relative_path(entry["source_path"], "来源路径")
        target_path = validate_relative_path(entry["target_path"], "目标路径")
        if entry["source_inventory_path"] != f"{root_name}/{source_path}":
            raise AssertionBundleError(
                f"inventory 逻辑路径与来源不一致：{entry['source_inventory_path']}"
            )
        if entry["source_sha256"] != entry["target_sha256"]:
            raise AssertionBundleError(
                f"provenance 条目摘要不成对：{target_path}"
            )
        if root_name not in source_roots:
            raise AssertionBundleError(f"证据根未提供，无法重放：{root_name}")
        root = source_roots[root_name]
        _assert_no_symlink_segments(root, source_path, "来源文件")
        source = root / source_path
        _assert_regular_source(source, "来源文件")
        if _file_sha256(source) != entry["source_sha256"]:
            raise AssertionBundleError(
                f"来源相对收口时发生漂移：{root_name}/{source_path}"
            )
        _assert_no_symlink_segments(bundle_dir, target_path, "bundle 文件")
        target = bundle_dir / target_path
        _assert_regular_source(target, "bundle 文件")
        if _file_sha256(target) != entry["target_sha256"]:
            raise AssertionBundleError(
                f"bundle 内容相对收口时发生漂移：{target_path}"
            )
        expected_targets.add(target_path)
    allowed_prefixes = tuple(allowed_extra_prefixes)
    for path in sorted(bundle_dir.rglob("*")):
        if path.is_symlink():
            raise AssertionBundleError(f"bundle 内禁止符号链接：{path}")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle_dir).as_posix()
        if relative == PROVENANCE_FILENAME or relative in expected_targets:
            continue
        if any(relative.startswith(prefix) for prefix in allowed_prefixes):
            continue
        raise AssertionBundleError(f"bundle 内存在未登记文件：{relative}")
    return provenance


def verify_manifest_kind_coverage(
    manifest: Mapping[str, Any],
    required: Mapping[str, Iterable[str]],
) -> None:
    """按分侧覆盖矩阵校验 capture manifest 的场景×kind 覆盖，缺失即失败。"""

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise AssertionBundleError("capture manifest artifacts 缺失")
    kinds_by_scenario: dict[str, set[str]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise AssertionBundleError("capture manifest artifact 必须是对象")
        kind = artifact.get("kind")
        scenario_ids = artifact.get("scenario_ids")
        if not isinstance(kind, str) or not isinstance(scenario_ids, list):
            raise AssertionBundleError("capture manifest artifact 字段非法")
        for scenario_id in scenario_ids:
            kinds_by_scenario.setdefault(str(scenario_id), set()).add(kind)
    missing: list[str] = []
    for scenario_id in sorted(required):
        actual = kinds_by_scenario.get(scenario_id, set())
        for kind in sorted(set(required[scenario_id])):
            if kind not in actual:
                missing.append(f"{scenario_id}:{kind}")
    if missing:
        raise AssertionBundleError(
            "capture manifest 未覆盖场景要求的 artifact kind："
            + "、".join(missing)
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="收口或重放断言证据包（ACC-02）"
    )
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="证据根，NAME 必须等于封存 inventory 的逻辑前缀",
    )
    parser.add_argument("--plan", type=Path, help="收口清单 JSON")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="重放 provenance 而不是收口",
    )
    parser.add_argument(
        "--allow-extra",
        action="append",
        default=[],
        metavar="PREFIX",
        help="verify 时允许的额外文件前缀（例如派生器产物目录）",
    )
    arguments = parser.parse_args()
    try:
        roots = parse_source_roots(arguments.source_root)
        if arguments.verify:
            provenance = verify_bundle(
                roots,
                arguments.bundle_dir,
                allowed_extra_prefixes=arguments.allow_extra,
            )
            print(
                f"断言证据包重放通过：{provenance['entry_count']} 项，"
                f"摘要={provenance['provenance_sha256']}"
            )
        else:
            if arguments.plan is None:
                raise AssertionBundleError("收口模式必须提供 --plan")
            plan = load_plan(arguments.plan)
            provenance = build_bundle(roots, plan, arguments.bundle_dir)
            print(
                f"断言证据包收口完成：{provenance['entry_count']} 项，"
                f"摘要={provenance['provenance_sha256']}"
            )
    except AssertionBundleError as error:
        print(f"断言证据包失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
