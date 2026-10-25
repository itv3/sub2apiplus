#!/usr/bin/env python3
"""修复轮的“影响集预览”（Codex 升级工具优化第 11 项：修复轮后台验证只跑受影响的单元）。

只读、纯函数、不碰宿主：由上一轮来源记录封存的承接视图材料（``read_audit.host_path_modes`` 与输入明细里的宿主快照）和两次提交之间的
git 变更集，列出哪些单元**可能**受影响。**预览不是权威**：权威仍是 ``unit_records.check_record`` 的窄化分支（经 ``unit_inherit.assess``），
它用同一个承接视图（``read_audit.inherit_view``）在当前输入上现场比对；本模块只回答“改了这些文件，大概要重跑谁”，用来展示、排期和对拍。
预览必须是权威判定的保守超集——任何被权威判拒绝的单元都必须出现在预览里（用测试与 10b 影子执行对拍），多报可以（stat 级的内容改动权威会放过，
预览按受影响报），漏报不行。

单元 u 的来源记录给出：访问集合 ``A_u``（``host_path_modes`` 里有访问类型的路径）和每个路径的访问方式。变更集（仓库相对路径，来自
``git diff --raw -z --no-renames <旧提交> <新提交>``）里的路径 p 让 u 受影响，当且仅当满足下列任一条：

1. p 属于 ``A_u``，且 p 被修改、删除或改了权限（内容或存在性变化；stat 级只改内容不改大小的权威会放过，预览仍报）。例外：整树遍历签名下
   （``read_audit.walk_stat_directories``，B2）只被 stat 的测试文件，只改内容不改权限、不删除时不报——权威本来就忽略它们的大小；
2. p 被新增或删除，且 u 枚举过它的某个祖先目录 d（``list``）：直接父目录一定算；更高层的祖先只有“p 在 d 下的第一层名字原本不在 d 的目录项里”
   （新增）或“原本在”（删除，保守）时才算——目录项只记名字；
3. p 被新增，且 p 或 p 的某个祖先是 u 探测过却不存在的路径（``missing``）；
4. p 的类型变了（文件↔目录↔符号链接），且 p 或它的父目录属于 ``A_u``——目录项只有名字没有类型，权威发现不了这种变化（已知缺口 R2），
   所以变更集里出现类型变化、符号链接（120000）、子模块（160000）或 ``.gitmodules`` 变更时**一票否决**：本轮不承接任何单元；
5. u 读过仓库 ``.git`` 下的东西（``HEAD``、对象库…），而提交变了——提交一变就该重跑，这是真依赖；
6. u 的非宿主视图条目（``runtime-test-spec`` 等整份比较的）在 ``changed_entries`` 里。

另外祖先目录被整个删除（它的全部目录项都在被删除的路径里）时，访问过这个目录本身的单元也算受影响。没有可窄化的来源记录（旧口径、读集未闭合、
没有来源）的单元一律报“必重跑”——自然成立：无来源即执行。

``changeset_from_git`` 取变更集；``predict_affected`` 出预览；命令行 ``affected --store <记录库> --run-id <来源运行> --repo <测试树> --old <提交> --new <提交>``
把两者接起来，打印每个受影响单元的原因。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
VETO_MODES = frozenset({"120000", "160000"})


def _sibling(filename: str, key: str) -> Any:
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return sys.modules[key]


def _audit():
    return _sibling("read_audit.py", "inherit_predict_read_audit")


def _records():
    return _sibling("unit_records.py", "inherit_predict_unit_records")


@dataclass
class ChangeSet:
    """两次提交之间的变更集（仓库相对路径，不含重命名：重命名按删除加新增）。"""

    added: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)
    type_changed: set[str] = field(default_factory=set)
    mode_changed: set[str] = field(default_factory=set)       # 权限位变了（修改的一种；预览里不享受整树遍历的大小豁免）
    head_changed: bool = False
    veto: list[str] = field(default_factory=list)             # 一票否决的原因；非空＝本轮不承接任何单元


_RAW = re.compile(rb":(\d{6}) (\d{6}) [0-9a-f]+ [0-9a-f]+ ([A-Z])\d*")


def parse_raw_diff(output: bytes, *, head_changed: bool = False) -> ChangeSet:
    """解析 ``git diff --raw -z --no-renames`` 的输出：``:<旧模式> <新模式> <旧对象> <新对象> <状态>\\0<路径>\\0`` 成对出现。"""

    changes = ChangeSet(head_changed=head_changed)
    parts = output.split(b"\0")
    index = 0
    while index + 1 < len(parts):
        header, raw_path = parts[index], parts[index + 1]
        index += 2
        if not header:
            continue
        match = _RAW.fullmatch(header)
        if match is None:
            raise ValueError(f"无法解析 git diff --raw 的一行：{header!r}")
        old_mode, new_mode, status = match.group(1).decode(), match.group(2).decode(), match.group(3).decode()
        path = raw_path.decode("utf-8", "surrogateescape")
        if status == "A":
            changes.added.add(path)
        elif status == "D":
            changes.deleted.add(path)
        elif status == "M":
            changes.modified.add(path)
            if old_mode != new_mode:
                changes.mode_changed.add(path)
        elif status == "T":
            changes.type_changed.add(path)
        else:
            raise ValueError(f"变更集里有无法归类的状态 {status}：{path}")
        if (VETO_MODES & {old_mode, new_mode}) or status == "T":
            changes.veto.append(f"{path}：类型变化、符号链接或子模块（模式 {old_mode}→{new_mode}）")
        if path == ".gitmodules":
            changes.veto.append(".gitmodules 变更")
    return changes


def changeset_from_git(repo: Path | str, old: str, new: str) -> ChangeSet:
    """旧提交到新提交的变更集（只读）。``head_changed`` 由两个提交解析出的对象号是否相同决定。"""

    def run(*args: str) -> bytes:
        completed = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
        if completed.returncode != 0:
            raise ValueError(f"git {' '.join(args)} 失败：{completed.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        return completed.stdout

    resolved_old = run("rev-parse", "--verify", old + "^{commit}").decode().strip()
    resolved_new = run("rev-parse", "--verify", new + "^{commit}").decode().strip()
    diff = run("diff", "--raw", "-z", "--no-renames", "--no-abbrev", resolved_old, resolved_new)
    return parse_raw_diff(diff, head_changed=resolved_old != resolved_new)


def _relative(path: str, repo_root: str) -> str | None:
    """绝对路径换成仓库相对路径；仓库根自己是空串，仓库外返回 None。"""

    root = repo_root.rstrip("/")
    if path == root:
        return ""
    return path[len(root) + 1:] if path.startswith(root + "/") else None


def _parent(relative: str) -> str:
    return relative.rsplit("/", 1)[0] if "/" in relative else ""


def _ancestors(relative: str) -> list[str]:
    """仓库相对路径的祖先目录（由近到远，最后是仓库根，用空串表示）。"""

    parts = relative.split("/")
    return ["/".join(parts[:count]) for count in range(len(parts) - 1, 0, -1)] + [""]


def _listing(record: Mapping[str, Any], repo_root: str) -> dict[str, list[str]]:
    """来源记录里仓库内目录的目录项（相对路径 → 名字列表）。"""

    found: dict[str, list[str]] = {}
    audit = _audit()
    for entry in record.get("inputs") or []:
        if not audit.is_host_entry_v3(entry):
            continue
        snapshot = entry["detail"]["snapshot"]
        relative = _relative(entry["detail"]["path"], repo_root)
        if relative is not None and snapshot.get("kind") == "directory":
            found[relative] = list(snapshot.get("entries") or [])
    return found


def _unit_reasons(record: Mapping[str, Any], changes: ChangeSet, changed_entries: frozenset[str]) -> list[str]:
    audit_module = _audit()
    block = record["read_audit"]
    modes: Mapping[str, Sequence[str]] = block["host_path_modes"]
    repo_root = str(block.get("repo_root") or "")
    if not repo_root:
        return ["来源记录没有测试树坐标：必重跑"]
    walk = audit_module.walk_stat_directories(record["inputs"], modes, (repo_root, block.get("data_root")))
    accessed: dict[str, frozenset[str]] = {}
    for path, kinds in modes.items():
        relative = _relative(path, repo_root)
        if kinds and relative is not None:
            accessed[relative] = frozenset(kinds)
    listings = _listing(record, repo_root)
    reasons: list[str] = []

    def add(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    for path in sorted(changes.modified | changes.deleted | changes.mode_changed):
        kinds = accessed.get(path)
        if kinds is None:
            continue
        exempt = (path in changes.modified and path not in changes.mode_changed and kinds == {"stat"}
                  and (repo_root.rstrip("/") + "/" + _parent(path)).rstrip("/") in walk)
        if not exempt:
            add(f"被访问的 {path} 被{'删除' if path in changes.deleted else '修改'}（访问方式 {'／'.join(sorted(kinds))}）")
    missing = [path for path, kinds in accessed.items() if "missing" in kinds]
    for path in sorted(changes.added | changes.deleted | changes.type_changed):
        for directory in _ancestors(path):
            if "list" not in accessed.get(directory, ()):
                continue
            below = path.split("/")[len(directory.split("/")) if directory else 0]
            names = listings.get(directory, [])
            if directory == _parent(path) or (below not in names if path in changes.added else below in names):
                add(f"被枚举的目录 {directory} 的目录项会变（{'新增' if path in changes.added else '删除'} {path}）")
        if path in changes.added:
            for probe in missing:
                if path == probe or path.startswith(probe + "/"):
                    add(f"来源判为不存在的 {probe} 现在有了东西（新增 {path}）")
        if path in changes.type_changed and (path in accessed or _parent(path) in accessed):
            add(f"{path} 的类型变了，而它或它的父目录被访问过")
    for path in sorted(changes.deleted):
        for directory in _ancestors(path):
            names = listings.get(directory)
            if directory in accessed and names is not None and names and all(
                    (directory + "/" + name) in changes.deleted or any(item.startswith(directory + "/" + name + "/") for item in changes.deleted)
                    for name in names):
                add(f"被访问的目录 {directory} 整个被删除（删除 {path}）")
    if changes.head_changed:
        for relative, kinds in accessed.items():
            if (relative == ".git" or relative.startswith(".git/")) and kinds & {"read", "exec", "list"}:
                add(f"读了 {relative}，而提交变了（读 git 的单元提交一变就重跑）")
    for name in sorted(changed_entries):
        if any(entry.get("name") == name for entry in record.get("inputs") or []):
            add(f"整份比较的非宿主输入 {name} 变了")
    return reasons


def predict_affected(source_records: Mapping[str, Mapping[str, Any] | None], changes: ChangeSet,
                     *, changed_entries: Iterable[str] = ()) -> dict[str, list[str]]:
    """影响集预览：``{单元 ID: 原因列表}``，只含可能受影响的单元（保守超集，非权威）；没有出现的单元预期可承接，由权威判定最终确认。

    ``source_records``：单元 ID → 来源记录（没有来源给 None）。没有可窄化来源记录的单元一律受影响（必重跑）。变更集有一票否决项时所有单元都受影响。"""

    changed = frozenset(changed_entries)
    narrowed = _records()._narrowed_source
    result: dict[str, list[str]] = {}
    for unit_id, record in source_records.items():
        if changes.veto:
            result[unit_id] = ["本轮不承接任何单元（一票否决）：" + "；".join(changes.veto[:3])]
        elif record is None or not narrowed(record):
            result[unit_id] = ["没有可窄化的来源记录（无来源、旧口径或读集未闭合）：必重跑"]
        else:
            reasons = _unit_reasons(record, changes, changed)
            if reasons:
                result[unit_id] = reasons
    return result


def source_records_from_run(store_root: Path | str, run_id: str) -> dict[str, dict[str, Any] | None]:
    """来源运行清单里每个单元对应的记录（读不出来给 None）。"""

    records = _records()
    store = records.RecordStore(Path(store_root))
    manifest = store.manifest(run_id)
    if manifest is None:
        raise ValueError(f"记录库里没有运行 {run_id} 的清单")
    found: dict[str, dict[str, Any] | None] = {}
    for entry in manifest.get("units") or []:
        path = entry.get("record_path")
        found[str(entry["unit_id"])] = records._read_json(Path(path)) if path else None
    return found


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    affected = sub.add_parser("affected", help="打印旧提交到新提交之间可能受影响的单元（预览，非权威）")
    affected.add_argument("--store", type=Path, required=True)
    affected.add_argument("--run-id", required=True, help="来源运行（通常是上一轮后台验证的执行器运行 ID）")
    affected.add_argument("--repo", type=Path, required=True)
    affected.add_argument("--old", required=True)
    affected.add_argument("--new", required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        sources = source_records_from_run(args.store, args.run_id)
        changes = changeset_from_git(args.repo, args.old, args.new)
        affected = predict_affected(sources, changes)
    except (OSError, ValueError, KeyError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 3
    print(json.dumps({"status": "ok", "units": len(sources), "affected": len(affected), "veto": changes.veto,
                      "changes": {key: len(getattr(changes, key)) for key in ("added", "deleted", "modified", "type_changed", "mode_changed")},
                      "result": affected}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
