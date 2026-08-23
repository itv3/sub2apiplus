"""只通过参数数组调用 Git，并生成可复算的源码与发送面快照。"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .canonical import (
    expect_git_object,
    expect_object,
    expect_sha256,
    expect_string,
    load_json,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
    write_json_once,
)
from .errors import UpstreamMergeError


ROUTE_RE = re.compile(
    r"(?P<receiver>[A-Za-z_][A-Za-z0-9_.]*)\."
    r"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|Any)"
    r"\(\s*\"(?P<path>[^\"\\]*(?:\\.[^\"\\]*)*)\"",
    re.MULTILINE,
)

GO_FUNCTION_RE = re.compile(
    r"^func\s+(?:(?P<receiver>\([^\n)]+\))\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\n\]]+\]\s*)?\(",
    re.MULTILINE,
)

EGRESS_MIGRATION_RECEIPT_PATHS = (
    "docs/egress/lifecycle/migration-receipts.json",
    "docs/egress/migration/migration-receipts.json",
)


def _route_function_identity(source: str, offset: int) -> str:
    """返回路由调用所属的稳定 Go 函数身份，区分不同作用域中的同名局部变量。"""

    enclosing: re.Match[str] | None = None
    for match in GO_FUNCTION_RE.finditer(source, 0, offset):
        enclosing = match
    if enclosing is None:
        return "<package>"
    receiver = enclosing.group("receiver")
    name = enclosing.group("name")
    if receiver is None:
        return name
    normalized_receiver = " ".join(receiver.split())
    return f"{normalized_receiver}.{name}"


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """禁用 shell 执行命令，并保留完整 stdout/stderr。"""

    if not argv or not all(isinstance(item, str) and item for item in argv):
        raise UpstreamMergeError("命令 argv 必须是非空字符串数组")
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=env,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise UpstreamMergeError(
            f"命令失败（exit={completed.returncode}）：{list(argv)!r}：{detail}"
        )
    return completed


def run_git(
    repository_root: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run_process(("git", *args), cwd=repository_root, check=check)


def git_output(repository_root: Path, *args: str) -> str:
    return run_git(repository_root, *args).stdout.strip()


def assert_git_repository(repository_root: Path) -> Path:
    if repository_root.is_symlink() or not repository_root.is_dir():
        raise UpstreamMergeError(f"仓库根不可信：{repository_root}")
    resolved = repository_root.resolve(strict=True)
    top = Path(git_output(resolved, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != resolved:
        raise UpstreamMergeError(f"必须从 Git 顶层执行：expected={resolved} actual={top}")
    return resolved


def assert_clean(repository_root: Path, label: str) -> None:
    status = run_git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ).stdout
    if status:
        raise UpstreamMergeError(f"{label} 必须是干净工作树")


def rev_parse(repository_root: Path, expression: str) -> str:
    value = git_output(repository_root, "rev-parse", "--verify", expression)
    return expect_git_object(value, f"Git 表达式 {expression}")


def commit_tree(repository_root: Path, commit: str) -> str:
    expect_git_object(commit, "commit")
    return rev_parse(repository_root, f"{commit}^{{tree}}")


def merge_base(repository_root: Path, left: str, right: str) -> str:
    value = git_output(repository_root, "merge-base", left, right)
    return expect_git_object(value, "merge base")


def tag_commit(repository_root: Path, tag: str) -> str:
    return rev_parse(repository_root, f"refs/tags/{tag}^{{commit}}")


def remote_url(repository_root: Path, remote: str) -> str:
    return git_output(repository_root, "remote", "get-url", remote)


def current_branch_ref(repository_root: Path) -> str:
    value = git_output(repository_root, "symbolic-ref", "-q", "HEAD")
    if not value.startswith("refs/heads/"):
        raise UpstreamMergeError("当前 HEAD 不是受维护本地分支")
    return value


def object_at(repository_root: Path, treeish: str, relative: str) -> tuple[str, str]:
    safe_relative_path(relative, "protected path")
    object_id = rev_parse(repository_root, f"{treeish}:{relative}")
    object_type = git_output(repository_root, "cat-file", "-t", object_id)
    if object_type not in {"blob", "tree"}:
        raise UpstreamMergeError(f"受保护路径对象类型非法：{relative}={object_type}")
    return object_id, object_type


def protected_objects(
    repository_root: Path,
    treeish: str,
    paths: list[str],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for relative in sorted(set(paths)):
        object_id, object_type = object_at(repository_root, treeish, relative)
        entries.append(
            {
                "path": relative,
                "object_id": object_id,
                "object_type": object_type,
            }
        )
    return entries


def validate_protected_objects(
    repository_root: Path,
    treeish: str,
    entries: Any,
) -> None:
    if not isinstance(entries, list) or not entries:
        raise UpstreamMergeError("repository.protected_objects 必须是非空数组")
    paths: list[str] = []
    for index, value in enumerate(entries):
        label = f"repository.protected_objects[{index}]"
        entry = expect_object(value, label)
        if set(entry) != {"path", "object_id", "object_type"}:
            raise UpstreamMergeError(f"{label} 字段不闭合")
        relative = safe_relative_path(entry.get("path"), f"{label}.path")
        expected_id = expect_git_object(entry.get("object_id"), f"{label}.object_id")
        object_type = expect_string(entry.get("object_type"), f"{label}.object_type")
        if object_type not in {"blob", "tree"}:
            raise UpstreamMergeError(f"{label}.object_type 非法")
        actual_id, actual_type = object_at(repository_root, treeish, relative)
        if actual_id != expected_id or actual_type != object_type:
            raise UpstreamMergeError(f"受保护路径在合并中发生变化：{relative}")
        paths.append(relative)
    if paths != sorted(set(paths)):
        raise UpstreamMergeError("repository.protected_objects 必须按路径排序且不得重复")


def _tool_source_paths(repository_root: Path) -> list[str]:
    """返回会改变上游合并事实含义的受管工具闭集。"""

    candidates = {
        "Makefile",
        "tools/check_ledger_completeness.py",
        "tools/upstream_merge_plan.schema.json",
        "tools/upstream_merge_request.schema.json",
        "tools/upstream_merge_artifacts.schema.json",
    }
    package_root = repository_root / "tools" / "upstream_merge"
    if not package_root.is_dir() or package_root.is_symlink():
        raise UpstreamMergeError("缺少受管上游合并工具目录")
    for path in package_root.rglob("*.py"):
        if path.is_symlink() or not path.is_file():
            raise UpstreamMergeError(f"工具源不是可信普通文件：{path}")
        candidates.add(path.relative_to(repository_root).as_posix())
    scanner_root = repository_root / "backend" / "cmd" / "egressscan"
    for path in scanner_root.rglob("*.go"):
        if path.is_symlink() or not path.is_file():
            raise UpstreamMergeError(f"发送面扫描器源不可信：{path}")
        candidates.add(path.relative_to(repository_root).as_posix())
    missing = sorted(path for path in candidates if not (repository_root / path).is_file())
    if missing:
        raise UpstreamMergeError(f"受管工具闭集缺少文件：{missing}")
    return sorted(candidates)


def tool_bundle(repository_root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for relative in _tool_source_paths(repository_root):
        path = repository_root / PurePosixPath(relative)
        entries.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    digest_payload = "".join(
        f"{entry['path']}\0{entry['sha256']}\0{entry['bytes']}\n" for entry in entries
    ).encode("utf-8")
    return {
        "files": entries,
        "bundle_sha256": sha256_bytes(digest_payload),
    }


def validate_tool_bundle(repository_root: Path, value: Any) -> dict[str, Any]:
    bundle = expect_object(value, "tool_bundle")
    if set(bundle) != {"files", "bundle_sha256"}:
        raise UpstreamMergeError("tool_bundle 字段不闭合")
    expected_digest = expect_sha256(bundle.get("bundle_sha256"), "tool_bundle.bundle_sha256")
    files = bundle.get("files")
    if not isinstance(files, list) or not files:
        raise UpstreamMergeError("tool_bundle.files 必须是非空数组")
    paths: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, value_item in enumerate(files):
        label = f"tool_bundle.files[{index}]"
        entry = expect_object(value_item, label)
        if set(entry) != {"path", "sha256", "bytes"}:
            raise UpstreamMergeError(f"{label} 字段不闭合")
        relative = safe_relative_path(entry.get("path"), f"{label}.path")
        digest = expect_sha256(entry.get("sha256"), f"{label}.sha256")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UpstreamMergeError(f"{label}.bytes 非法")
        path = repository_root / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise UpstreamMergeError(f"受管工具文件不存在：{relative}")
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise UpstreamMergeError(f"受管工具文件漂移：{relative}")
        paths.append(relative)
        normalized.append({"path": relative, "sha256": digest, "bytes": size})
    if paths != sorted(set(paths)):
        raise UpstreamMergeError("tool_bundle.files 必须按路径排序且不得重复")
    current_paths = _tool_source_paths(repository_root)
    if paths != current_paths:
        raise UpstreamMergeError("受管工具闭集发生新增、遗漏或重命名")
    digest_payload = "".join(
        f"{entry['path']}\0{entry['sha256']}\0{entry['bytes']}\n"
        for entry in normalized
    ).encode("utf-8")
    if sha256_bytes(digest_payload) != expected_digest:
        raise UpstreamMergeError("tool_bundle.bundle_sha256 漂移")
    return bundle


def route_snapshot(repository_root: Path, commit: str, tree: str) -> dict[str, Any]:
    """扫描生产路由注册调用；任何新增别名都会进入 U-2 差异分母。"""

    expect_git_object(commit, "route snapshot commit")
    expect_git_object(tree, "route snapshot tree")
    roots = [
        repository_root / "backend" / "internal" / "server" / "routes",
        repository_root / "backend" / "cmd" / "server",
    ]
    entries: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            raise UpstreamMergeError(f"生产路由扫描根不存在或不可信：{root}")
        for path in sorted(root.rglob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            if path.is_symlink() or not path.is_file():
                raise UpstreamMergeError(f"生产路由源码不可信：{path}")
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(repository_root).as_posix()
            for match in ROUTE_RE.finditer(text):
                raw_path = bytes(match.group("path"), "utf-8").decode("unicode_escape")
                line = text.count("\n", 0, match.start()) + 1
                function = _route_function_identity(text, match.start())
                fingerprint_raw = (
                    f"{relative}\0{function}\0{match.group('receiver')}\0"
                    f"{match.group('method')}\0{raw_path}"
                ).encode("utf-8")
                entries.append(
                    {
                        "route_fingerprint": sha256_bytes(fingerprint_raw),
                        "file": relative,
                        "function": function,
                        "receiver": match.group("receiver"),
                        "method": match.group("method").upper(),
                        "path": raw_path,
                        "line_hint": line,
                    }
                )
    entries.sort(key=lambda item: item["route_fingerprint"])
    identities = [item["route_fingerprint"] for item in entries]
    if identities != sorted(set(identities)):
        raise UpstreamMergeError("生产路由快照出现重复身份")
    return {
        "schema_version": "official-egress-upstream-route-snapshot/v1",
        "source_commit": commit,
        "source_tree": tree,
        "scan_roots": [
            "backend/cmd/server",
            "backend/internal/server/routes",
        ],
        "entry_count": len(entries),
        "entries": entries,
    }


def run_egress_snapshot(repository_root: Path, output: Path) -> None:
    """复用受审扫描算法，另行封装当前 commit/tree 的 U-2 快照。"""

    if output.exists() or output.is_symlink():
        raise UpstreamMergeError(f"发送面快照输出已存在：{output}")
    assert_clean(repository_root, "source-to-sink snapshot")
    source_commit = rev_parse(repository_root, "HEAD^{commit}")
    source_tree = commit_tree(repository_root, source_commit)
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise UpstreamMergeError(f"发送面快照父目录不可信：{output.parent}")
    output.parent.chmod(0o700)
    with tempfile.TemporaryDirectory(
        prefix="egressscan-upstream-",
        dir=output.parent,
    ) as temporary:
        raw_output = Path(temporary) / "legacy-scan.json"
        completed = run_process(
            (
                "go",
                "run",
                "./cmd/egressscan",
                "-mode",
                "snapshot",
                "-migration-receipts",
                ",".join(
                    str(repository_root / relative)
                    for relative in EGRESS_MIGRATION_RECEIPT_PATHS
                ),
                "-out",
                str(raw_output),
            ),
            cwd=repository_root / "backend",
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise UpstreamMergeError(f"发送面快照失败：{detail}")
        baseline = expect_object(
            load_json(raw_output, "egressscan snapshot output"),
            "egressscan snapshot output",
        )
    required = {
        "bootstrap_commit",
        "scan_pattern",
        "build_contexts",
        "packages_loaded",
        "sinks",
    }
    actual_fields = set(baseline)
    if actual_fields != required and actual_fields != required | {"syntax_fallback_files"}:
        raise UpstreamMergeError("egressscan snapshot 输出字段不闭合")
    sinks = baseline.get("sinks")
    contexts = baseline.get("build_contexts")
    package_count = baseline.get("packages_loaded")
    if not isinstance(sinks, list) or not isinstance(contexts, list):
        raise UpstreamMergeError("egressscan snapshot 输出缺少 sinks 或 build_contexts")
    if isinstance(package_count, bool) or not isinstance(package_count, int) or package_count < 0:
        raise UpstreamMergeError("egressscan snapshot packages_loaded 非法")
    snapshot: dict[str, Any] = {
        "schema_version": "official-egress-upstream-source-to-sink-snapshot/v1",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "scan_pattern": expect_string(baseline.get("scan_pattern"), "scan_pattern"),
        "build_contexts": contexts,
        "packages_loaded": package_count,
        "sink_count": len(sinks),
        "sinks": sinks,
    }
    if "syntax_fallback_files" in baseline:
        fallback = baseline["syntax_fallback_files"]
        if not isinstance(fallback, list):
            raise UpstreamMergeError("egressscan syntax_fallback_files 非法")
        snapshot["syntax_fallback_files"] = fallback
    write_json_once(output, snapshot)
    assert_clean(repository_root, "source-to-sink snapshot 执行后")


def status_paths(repository_root: Path) -> list[str]:
    """解析包含重命名历史路径的完整工作树变化闭集。"""

    raw = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repository_root,
    )
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
            raise UpstreamMergeError(f"无法解析 git status：{text!r}")
        status_code, relative = text[:2], text[3:]
        safe_relative_path(relative, "git status path")
        paths.add(relative)
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise UpstreamMergeError(f"重命名状态缺少历史路径：{text!r}")
            old = fields[index].decode("utf-8", errors="strict")
            index += 1
            safe_relative_path(old, "git status old path")
            paths.add(old)
    return sorted(paths)


def unmerged_entries(repository_root: Path) -> list[dict[str, Any]]:
    raw = subprocess.check_output(["git", "ls-files", "-u", "-z"], cwd=repository_root)
    entries: list[dict[str, Any]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, object_id, stage = metadata.decode("ascii").split(" ")
        relative = raw_path.decode("utf-8", errors="strict")
        safe_relative_path(relative, "conflict path")
        entries.append(
            {
                "path": relative,
                "stage": int(stage),
                "mode": mode,
                "object_id": expect_git_object(object_id, "conflict object"),
            }
        )
    entries.sort(key=lambda item: (item["path"], item["stage"]))
    return entries


def changed_paths(
    repository_root: Path,
    before: str,
    after: str,
) -> list[dict[str, str]]:
    """返回含重命名前后路径的提交差异。"""

    raw = subprocess.check_output(
        ["git", "diff", "--name-status", "--find-renames", "-z", before, after],
        cwd=repository_root,
    )
    fields = [field.decode("utf-8", errors="strict") for field in raw.split(b"\0") if field]
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status_code = fields[index]
        index += 1
        kind = status_code[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise UpstreamMergeError("Git rename/copy 差异记录不完整")
            old_path, path = fields[index], fields[index + 1]
            index += 2
            safe_relative_path(old_path, "changed old path")
            safe_relative_path(path, "changed path")
            entries.append(
                {"status": kind, "path": path, "old_path": old_path}
            )
        else:
            if index >= len(fields):
                raise UpstreamMergeError("Git 差异记录缺少路径")
            path = fields[index]
            index += 1
            safe_relative_path(path, "changed path")
            entries.append({"status": kind, "path": path, "old_path": ""})
    entries.sort(key=lambda item: (item["path"], item["old_path"], item["status"]))
    return entries


def executable_identity(command: str, cwd: Path) -> dict[str, Any]:
    """记录门禁实际执行文件；系统脚本无法摘要时仍保留解析路径。"""

    if "/" in command:
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
    else:
        found = shutil.which(command)
        if found is None:
            raise UpstreamMergeError(f"找不到门禁可执行文件：{command}")
        resolved = Path(found).resolve()
    result: dict[str, Any] = {"command": command, "resolved_path": str(resolved)}
    if resolved.is_file() and not resolved.is_symlink():
        result.update({"sha256": sha256_file(resolved), "bytes": resolved.stat().st_size})
    else:
        result.update({"sha256": None, "bytes": None})
    return result


def command_environment() -> dict[str, Any]:
    """冻结计划生成端的基本工具版本，便于重放识别环境漂移。"""

    probes = {
        "git": ("git", "--version"),
        "go": ("go", "version"),
        "make": ("make", "--version"),
        "python": (sys.executable, "--version"),
    }
    result: dict[str, Any] = {}
    for name, argv in probes.items():
        completed = run_process(argv, cwd=Path.cwd(), check=False)
        if completed.returncode != 0:
            raise UpstreamMergeError(f"无法冻结工具版本：{name}")
        version = (completed.stdout or completed.stderr).splitlines()[0].strip()
        result[name] = {
            "argv": list(argv),
            "version": version,
            "executable": executable_identity(argv[0], Path.cwd()),
        }
    return result
