#!/usr/bin/env python3
"""B-11 隔离运行合同：在私有挂载命名空间建立空临时盘，分别保存挂载和被测命令轨迹。

本模块属于执行器的可信启动部分，代码摘要纳入执行器身份。它不能把普通宿主目录认作本轮输出；
只有经实际挂载核对的私有 tmpfs 才能提供输出证明。未支持的内核输入和系统调用仍拒绝承接。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "unit-runtime-contract/v1"
CONTEXT_SCHEMA = "read-audit-runtime-context/v1"
HERE = Path(__file__).resolve()
MASK_ROOT = "/root/oauth-capture"
ENVIRONMENT_PATHS = {"/proc/filesystems", "/proc/sys/crypto/fips_enabled"}
LAUNCHER = ["unshare", "-m", "--propagation", "private", "bash", "-c",
            'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec "$@"', "entry-gates"]


class RuntimeContractError(RuntimeError):
    """隔离或取证条件不满足，禁止承接。"""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def validate_contract(value: Any) -> dict[str, Any]:
    """只接受当前已实现的空 tmpfs 和生产别名遮挡，禁止任意挂载命令或路径前缀豁免。"""
    fields = {"schema_version", "temporary_root", "mask_roots", "environment_paths"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != SCHEMA:
        raise RuntimeContractError("运行合同格式错误")
    root = value.get("temporary_root")
    if (not isinstance(root, str) or not root.startswith("/") or str(Path(root)) != root
            or ".." in Path(root).parts or len(Path(root).parts) < 5
            or any(under(root, item) for item in ("/proc", "/sys", "/dev"))):
        raise RuntimeContractError("临时根必须是专用的规范绝对路径，不能声明整个临时目录")
    if value["mask_roots"] not in ([], [MASK_ROOT]):
        raise RuntimeContractError("只支持已登记的生产别名遮挡")
    paths = value["environment_paths"]
    if (not isinstance(paths, list) or not all(isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths) or set(paths) - ENVIRONMENT_PATHS):
        raise RuntimeContractError("动态环境输入尚无可重放合同")
    if any(under(root, mask) or under(mask, root) for mask in value["mask_roots"]):
        raise RuntimeContractError("临时盘不能覆盖或位于遮挡根中")
    return value


def environment_snapshot(path: str) -> dict[str, Any]:
    if path not in ENVIRONMENT_PATHS:
        raise RuntimeContractError("未登记的动态环境路径")
    target = Path(path)
    if target.is_symlink():
        raise RuntimeContractError("动态环境输入不能通过链接替换")
    try:
        with target.open("rb") as stream:
            content = stream.read(1024 * 1024 + 1)
    except FileNotFoundError:
        return {"path": path, "kind": "missing"}
    if len(content) > 1024 * 1024:
        raise RuntimeContractError("动态环境输入超过合同上限")
    return {"path": path, "kind": "file", "content_sha256": hashlib.sha256(content).hexdigest()}


def contract_entry(value: Any) -> dict[str, Any]:
    contract = validate_contract(value)
    device = os.stat("/dev/null")
    if not stat.S_ISCHR(device.st_mode):
        raise RuntimeContractError("/dev/null 不是标准字符设备")
    if sys.platform.startswith("linux") and (os.major(device.st_rdev), os.minor(device.st_rdev)) != (1, 3):
        raise RuntimeContractError("/dev/null 设备身份不符")
    payload = {"contract": contract, "platform": {"system": platform.system(), "arch": platform.machine(),
               "kernel": platform.release()}, "environment": [environment_snapshot(path) for path in contract["environment_paths"]],
               "null_device": {"mode": device.st_mode, "uid": device.st_uid, "gid": device.st_gid, "rdev": device.st_rdev}}
    return {"category": "environment", "name": "runtime-contract", "sha256": digest(payload),
            "detail": {"schema_version": SCHEMA, **payload}}


class OutputTracker:
    """按调用顺序证明生成文件归属；未知写法、先读后写、链接及来源不明均拒绝。"""

    def __init__(self, contract: dict[str, Any]) -> None:
        self.root = validate_contract(contract)["temporary_root"]
        self.created = {self.root: "directory"}
        self.covered: set[str] = set()
        self.problems: set[str] = set()

    def observe(self, path: str, kind: str, name: str, body: str, succeeded: bool, target: str | None = None) -> None:
        if not under(path, self.root):
            return
        self.covered.add(path)
        if name in {"open", "openat", "creat"} and succeeded and (target is None or not under(target, self.root)):
            self.problems.add("文件描述符未绑定到私有临时盘：" + path)
            return
        if name in {"link", "linkat", "symlink", "symlinkat", "rename", "renameat", "renameat2"}:
            self.problems.add("链接或移动操作尚无输出归属证明：" + path)
            return
        known = self.created.get(path)
        if kind == "missing":
            if known:
                self.problems.add("已生成对象的缺失探测与轨迹矛盾：" + path)
            return
        if kind != "write":
            if not known:
                self.problems.add("首次读取前未证明由本轮生成：" + path)
            return
        if not succeeded:
            self.problems.add("未建立合同的失败写操作：" + path)
            return
        if name in {"mkdir", "mkdirat"}:
            if known:
                self.problems.add("创建目录时已存在：" + path)
            elif self.created.get(str(Path(path).parent)) != "directory":
                self.problems.add("新建目录的父目录未证明：" + path)
            else:
                self.created[path] = "directory"
        elif name in {"open", "openat", "creat"}:
            if not known:
                if (name != "creat" and "O_CREAT" not in body) or self.created.get(str(Path(path).parent)) != "directory":
                    self.problems.add("首次写入缺少创建证明：" + path)
                else:
                    self.created[path] = "file"
        elif name in {"unlink", "unlinkat", "rmdir"}:
            if not known or path == self.root:
                self.problems.add("删除未证明的输出：" + path)
            else:
                self.created.pop(path, None)
        elif name in {"chmod", "fchmodat", "utime", "utimes", "utimensat", "truncate"} and known:
            pass
        else:
            self.problems.add("未建立合同的输出操作：" + name + ":" + path)

    def report(self) -> dict[str, Any]:
        return {"root": self.root, "paths": sorted(self.covered), "problems": sorted(self.problems)}


def validate_context(context: Any, inputs: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(context, dict) or context.get("schema_version") != CONTEXT_SCHEMA:
        raise RuntimeContractError("隔离环境凭证缺失")
    entries = [entry for entry in inputs if entry.get("name") == "runtime-contract"]
    if len(entries) != 1:
        raise RuntimeContractError("隔离合同未与输入摘要绑定")
    entry = entries[0]
    contract = validate_contract(entry.get("detail", {}).get("contract"))
    if entry != contract_entry(contract) or context.get("contract_entry") != entry:
        raise RuntimeContractError("隔离合同、平台、设备或环境输入漂移")
    parent, child = context.get("parent_namespace"), context.get("namespace")
    if not all(isinstance(value, str) and re.fullmatch(r"mnt:\[\d+\]", value) for value in (parent, child)) or parent == child:
        raise RuntimeContractError("未证明新的挂载命名空间")
    if context.get("empty_temporary_root") is not True or context.get("private_mounts") is not True:
        raise RuntimeContractError("临时盘非本轮空盘或挂载传播未隔离")
    expected = {contract["temporary_root"]: "rw", **{path: "ro" for path in contract["mask_roots"]}}
    mounts = context.get("mounts")
    if not isinstance(mounts, list) or len(mounts) != len(expected) or {row.get("target") for row in mounts} != set(expected):
        raise RuntimeContractError("实际挂载集合与合同不同")
    for row in mounts:
        if row.get("filesystem") != "tmpfs" or expected[row["target"]] not in row.get("options", []):
            raise RuntimeContractError("实际挂载类型或只读属性不符")
    trace = context.get("setup_trace")
    if not isinstance(trace, str) or hashlib.sha256(trace.encode()).hexdigest() != context.get("setup_trace_sha256"):
        raise RuntimeContractError("启动挂载轨迹缺失或摘要漂移")
    if context.get("setup_commands") != ["private", *contract["mask_roots"], contract["temporary_root"]]:
        raise RuntimeContractError("启动挂载步骤不完整")
    if not trace.strip() or context.get("setup_success") is not True:
        raise RuntimeContractError("启动挂载未完成")
    calls = []
    for line in trace.splitlines():
        match = re.fullmatch(r'\d+\s+mount\("([^"\\]*)", "([^"\\]*)", (NULL|"[^"\\]*"), ([A-Z0-9_|]+), (NULL|"[^"\\]*")\)\s*= 0', line)
        if match is None:
            raise RuntimeContractError("启动轨迹含失败、未解析或额外挂载调用")
        calls.append(match.groups())
    if len(calls) != len(expected) + 1 or calls[0] != ("none", "/", "NULL", "MS_REC|MS_PRIVATE", "NULL"):
        raise RuntimeContractError("私有传播设置没有实际调用证明")
    for call, target in zip(calls[1:], [*contract["mask_roots"], contract["temporary_root"]]):
        source, observed, filesystem, flags, options = call
        required_flags = {"MS_RDONLY"} if target in contract["mask_roots"] else {"MS_NOSUID", "MS_NODEV"}
        required_options = {"size=64k", "mode=0755"} if target in contract["mask_roots"] else {"size=512m", "mode=0700"}
        if (source != "tmpfs" or observed != target or filesystem != '"tmpfs"'
                or set(flags.split("|")) != required_flags or set(options.strip('"').split(",")) != required_options):
            raise RuntimeContractError("实际挂载参数与批准合同不同")
    return contract


def replay_runtime(document: dict[str, Any], inputs: list[dict[str, Any]]) -> set[str]:
    """重放输出生成顺序、隔离身份和结束凭证；只返回确已证明的路径，不能按目录整段放行。"""
    runtime = document.get("runtime") or {}
    contract = validate_context(runtime.get("context"), inputs)
    completion = runtime.get("completion") or {}
    if any(completion.get(key) is not True for key in
           ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged", "command_finished")):
        raise RuntimeContractError("隔离执行未结束或输入／环境在执行期间变化")
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == "read-audit-host-snapshot/v1"]
    if completion.get("host_entries_sha256") != digest(entries):
        raise RuntimeContractError("实际命名空间中的输入快照未绑定执行记录")
    tracker = OutputTracker(contract)
    events = runtime.get("output_events")
    if not isinstance(events, list) or len(events) > 200000:
        raise RuntimeContractError("输出归属轨迹缺失或超过上限")
    seen: dict[str, set[str]] = {}
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"path", "kind", "name", "succeeded", "flags", "target"}
                or not all(isinstance(event[key], str) for key in ("path", "kind", "name"))
                or not isinstance(event["succeeded"], bool) or not isinstance(event["flags"], list)
                or not all(isinstance(flag, str) and re.fullmatch(r"O_[A-Z0-9_]+", flag) for flag in event["flags"])
                or (event["target"] is not None and not isinstance(event["target"], str))
                or not under(event["path"], tracker.root)):
            raise RuntimeContractError("输出归属事件格式错误或越界")
        tracker.observe(event["path"], event["kind"], event["name"], "|".join(event["flags"]), event["succeeded"], event["target"])
        seen.setdefault(event["path"], set()).add(event["kind"])
    if tracker.report() != runtime.get("outputs") or tracker.problems:
        raise RuntimeContractError("生成输出存在先读后写、来源不明或链接越界")
    paths = dict(document.get("accesses") or [])
    expected = {path: set(kinds) for path, kinds in paths.items() if under(path, tracker.root)}
    if seen != expected:
        raise RuntimeContractError("输出归属与完整路径集合不闭合")
    covered = set(seen)
    environment = {entry["path"]: entry for entry in runtime["context"]["contract_entry"]["detail"]["environment"]}
    for path, kinds in paths.items():
        if path == "/dev/null" and set(kinds) <= {"read", "write", "stat"}:
            covered.add(path)
        elif path in environment:
            allowed = {"missing"} if environment[path]["kind"] == "missing" else {"read", "stat"}
            if set(kinds) <= allowed:
                covered.add(path)
        else:
            for mask in contract["mask_roots"]:
                if path == mask and set(kinds) <= {"dir", "stat", "list"}:
                    covered.add(path)
                elif under(path, mask) and set(kinds) <= {"missing"}:
                    covered.add(path)
    return covered


def _read_audit():
    spec = importlib.util.spec_from_file_location("runtime_read_audit", HERE.with_name("read_audit.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _mount_rows() -> list[dict[str, Any]]:
    rows = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        parts, fs = left.split(), right.split()
        target = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), parts[4])
        rows.append({"target": target, "filesystem": fs[0], "options": parts[5].split(","),
                     "propagation": parts[6:]})
    return rows


def run_child(request_path: Path, expected_digest: str) -> int:
    request = json.loads(request_path.read_text())
    if digest(request) != expected_digest:
        raise RuntimeContractError("启动请求摘要不符")
    entry = request["contract_entry"]
    contract = validate_contract(entry["detail"]["contract"])
    if entry != contract_entry(contract):
        raise RuntimeContractError("启动环境与登记时不一致")
    namespace = os.readlink("/proc/self/ns/mnt")
    if namespace == request["parent_namespace"]:
        raise RuntimeContractError("未进入独立挂载命名空间")
    root = Path(contract["temporary_root"])
    if any(path.is_symlink() for path in (root, *root.parents)) or any(root.iterdir()):
        raise RuntimeContractError("宿主临时挂载点非空或含链接")
    trace_path = Path(request["output"])
    setup_parts, setup_commands = [], []
    operations = [("private", ["mount", "--make-rprivate", "/"])]
    operations += [(mask, ["mount", "-t", "tmpfs", "-o", "ro,size=64k,mode=0755", "tmpfs", mask]) for mask in contract["mask_roots"]]
    operations.append((str(root), ["mount", "-t", "tmpfs", "-o", "size=512m,mode=0700,nosuid,nodev", "tmpfs", str(root)]))
    for index, (label, command) in enumerate(operations):
        path = trace_path.with_name(trace_path.name + f".mount-{index}")
        result = subprocess.run(["strace", "-f", "-qq", "-s", "4096", "-e", "trace=mount,umount2,unshare,setns",
                                 "-o", str(path), "--", *command], stdin=subprocess.DEVNULL, check=False)
        setup_parts.append(path.read_text()); setup_commands.append(label)
        if result.returncode:
            raise RuntimeContractError("隔离挂载失败，停止被测命令")
    rows = _mount_rows()
    wanted = {str(root), *contract["mask_roots"]}
    mounts = [row for row in rows if row["target"] in wanted]
    context = {"schema_version": CONTEXT_SCHEMA, "contract_entry": entry, "parent_namespace": request["parent_namespace"],
               "namespace": namespace, "empty_temporary_root": not any(root.iterdir()),
               "private_mounts": not any(any(value.startswith(("shared:", "master:")) for value in row["propagation"]) for row in rows),
               "mounts": mounts, "setup_commands": setup_commands, "setup_success": True,
               "setup_trace": "".join(setup_parts), "setup_trace_sha256": hashlib.sha256("".join(setup_parts).encode()).hexdigest()}
    validate_context(context, [entry])
    context_path = trace_path.with_name(trace_path.name + ".context.json")
    _write(context_path, context)
    audit = _read_audit()
    before = [audit.host_snapshot(path) for path in request["host_paths"]]
    if before != request["host_entries"]:
        raise RuntimeContractError("实际命名空间中的输入与登记快照不同")
    argv = audit.strace_argv(request["argv"], output=trace_path, roots=["/"], strict=True, runtime_context=context_path)
    result = subprocess.run(argv, cwd=request["cwd"], env={**os.environ, "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root)},
                            stdin=subprocess.DEVNULL, check=False)
    after = [audit.host_snapshot(path) for path in request["host_paths"]]
    receipt = {"context": context, "host_inputs_unchanged": before == after, "environment_unchanged": entry == contract_entry(contract),
               "namespace_unchanged": namespace == os.readlink("/proc/self/ns/mnt"), "returncode": result.returncode,
               "request_sha256": expected_digest, "host_entries": before}
    _write(trace_path.with_name(trace_path.name + ".runtime.json"), receipt)
    if trace_path.is_file():
        document = json.loads(trace_path.read_text())
        document.setdefault("runtime", {})["completion"] = {
            key: receipt[key] for key in ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged")}
        document["runtime"]["completion"].update(command_finished=True, host_entries_sha256=digest(before),
                                                returncode=result.returncode, request_sha256=expected_digest)
        _write(trace_path, document)
    return result.returncode


def prepare(contract: dict[str, Any], *, output: Path, argv: list[str], cwd: str,
            inputs: list[dict[str, Any]]) -> list[str]:
    """启动前建立专用空挂载点，写受管请求；实际文件快照在子命名空间再次核对。"""
    if not sys.platform.startswith("linux") or shutil.which("unshare") is None:
        raise RuntimeContractError("隔离合同只在具备 unshare 的 Linux 上可用")
    contract = validate_contract(contract)
    root = Path(contract["temporary_root"])
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise RuntimeContractError("临时根不能含符号链接")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(root.iterdir()) or stat.S_IMODE(root.stat().st_mode) != 0o700 or root.stat().st_uid != os.geteuid():
        raise RuntimeContractError("宿主挂载点必须为空、归运行账号所有且权限为 0700")
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == "read-audit-host-snapshot/v1"]
    request = {"contract_entry": contract_entry(contract), "parent_namespace": os.readlink("/proc/self/ns/mnt"),
               "output": str(output), "argv": argv, "cwd": cwd, "host_entries": entries,
               "host_paths": [entry["detail"]["path"] for entry in entries]}
    path = output.with_name(output.name + ".request.json")
    _write(path, request)
    return ["unshare", "-m", "--propagation", "unchanged", sys.executable, "-B", str(HERE),
            "--request", str(path), "--request-sha256", digest(request)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        return run_child(args.request, args.request_sha256)
    except (RuntimeContractError, OSError, ValueError, KeyError) as error:
        print(f"隔离读集取证失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
