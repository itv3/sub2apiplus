#!/usr/bin/env python3
"""B-11 隔离运行合同：在私有挂载命名空间建立空临时盘，分别保存挂载和被测命令轨迹。

本模块属于执行器的可信启动部分，代码摘要纳入执行器身份。它不能把普通宿主目录认作本轮输出；
只有经实际挂载核对的私有 tmpfs 才能提供输出证明。未支持的内核输入和系统调用仍拒绝承接。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import json
import math
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
TEST_SPEC_SCHEMA = "unit-runtime-test-spec/v1"
TEST_TRANSPORT_SCHEMA = "unit-runtime-test-transport/v1"
TEST_EXPORT_SCHEMA = "unit-runtime-test-export/v1"
TEST_RESULT_SCHEMA = "unit-executor-unit-result/v1"
TEST_RESULT_NAME = "unit-result.json"
TEST_FAILURE_NAME = "unit-result.observed-failure.json"
MAX_TEST_RESULT_BYTES = 16 * 1024 * 1024
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


def test_spec_entry(spec: Any) -> dict[str, Any]:
    """只绑定本次已规划测试的完整规格，不能用宿主临时 tests.json 代替稳定输入。"""
    fields = {"type", "unit_id", "env", "cores", "memory_mb", "exclusive", "timeout_seconds",
              "reservation_restartable", "start", "pattern", "test_ids", "launcher"}
    if (not isinstance(spec, dict) or set(spec) != fields or spec.get("type") != "test"
            or not isinstance(spec["unit_id"], str) or not spec["unit_id"].startswith("test_")
            or re.fullmatch(r"[A-Za-z0-9_.#!+-]+", spec["unit_id"]) is None
            or not isinstance(spec["start"], str) or not spec["start"] or ".." in Path(spec["start"]).parts
            or not isinstance(spec["pattern"], str) or not spec["pattern"]
            or not isinstance(spec["test_ids"], list) or not spec["test_ids"]
            or not all(isinstance(item, str) and item and "\x00" not in item for item in spec["test_ids"])
            or len(spec["test_ids"]) != len(set(spec["test_ids"]))
            or not isinstance(spec["env"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in spec["env"].items())
            or not isinstance(spec["launcher"], list)
            or not all(isinstance(part, str) and part for part in spec["launcher"])
            or not isinstance(spec["exclusive"], bool) or not isinstance(spec["reservation_restartable"], bool)
            or not isinstance(spec["memory_mb"], int) or isinstance(spec["memory_mb"], bool) or spec["memory_mb"] < 64):
        raise RuntimeContractError("专用测试运输的完整规格非法")
    for name in ("cores", "timeout_seconds"):
        value = spec[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise RuntimeContractError("专用测试运输的额度或超时非法")
    return {"category": "policy", "name": "runtime-test-spec", "sha256": digest(spec),
            "detail": {"schema_version": TEST_SPEC_SCHEMA, "spec": spec}}


def _test_transport_check(value: Any, contract: dict[str, Any], inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fields = {"schema_version", "spec", "spec_sha256", "payload_sha256", "temporary_root", "result_path"}
    if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != TEST_TRANSPORT_SCHEMA:
        raise RuntimeContractError("专用测试运输格式非法")
    entry = test_spec_entry(value["spec"])
    spec = value["spec"]
    payload = {"unit_id": spec["unit_id"], "test_ids": spec["test_ids"]}
    if (value["spec_sha256"] != entry["sha256"] or value["payload_sha256"] != digest(payload)
            or value["temporary_root"] != contract["temporary_root"]):
        raise RuntimeContractError("专用测试运输的规格、载荷或临时根错绑")
    path = value["result_path"]
    safe = spec["unit_id"].replace("#", "-").replace("!", "-")
    if (not isinstance(path, str) or not path.startswith("/") or str(Path(path)) != path or ".." in Path(path).parts
            or Path(path).parent.name != "units"
            or re.fullmatch(re.escape(safe) + r"(?:\.resume-[1-9][0-9]*)?\.result\.json", Path(path).name) is None
            or under(path, contract["temporary_root"]) or under(contract["temporary_root"], str(Path(path).parent))
            or any(under(path, root) for root in ("/proc", "/sys", "/dev"))):
        raise RuntimeContractError("只允许原调度器的规范测试结果坐标")
    if inputs is not None and [item for item in inputs if item.get("name") == "runtime-test-spec"] != [entry]:
        raise RuntimeContractError("专用测试运输未与当前完整规格输入绑定")
    return value


def test_transport_argv(value: dict[str, Any], contract: dict[str, Any], argv: list[str], *, output: Path) -> list[str]:
    """仅替换既有 run-unit 的临时载荷与两种固定输出，不提供通用命令或导出能力。"""
    _test_transport_check(value, contract)
    result = Path(value["result_path"])
    tests = str(result)[:-len(".result.json")] + ".tests.json"
    expected = [sys.executable, str(HERE.with_name("unit_executor.py")), "run-unit", "--start", value["spec"]["start"],
                "--tests-file", tests, "--result", str(result)]
    if argv != expected or result.parent != output.parent.parent / "units":
        raise RuntimeContractError("专用测试运输只允许本次原 run-unit 命令与结果目录")
    for path in (result, *result.parents):
        if path.is_symlink():
            raise RuntimeContractError("测试结果坐标不能含符号链接")
    return _runtime_test_command(value)


def _runtime_test_command(value: dict[str, Any]) -> list[str]:
    return [sys.executable, str(HERE.with_name("unit_executor.py")), "run-unit", "--start", value["spec"]["start"],
            "--runtime-tests-json", json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "--result", str(Path(value["temporary_root"]) / TEST_RESULT_NAME)]


def runtime_test_payload(value: Any, *, start: Path, result: Path) -> dict[str, Any]:
    """既有 run-unit 的专用入口；完整规格和固定私有结果坐标均须吻合。"""
    root = value.get("temporary_root") if isinstance(value, dict) else None
    contract = validate_contract({"schema_version": SCHEMA, "temporary_root": root, "mask_roots": [], "environment_paths": []})
    _test_transport_check(value, contract)
    if str(start) != value["spec"]["start"] or str(result) != str(Path(root) / TEST_RESULT_NAME):
        raise RuntimeContractError("专用测试载荷的起点或私有结果坐标错绑")
    return {"unit_id": value["spec"]["unit_id"], "test_ids": value["spec"]["test_ids"]}


def test_result_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False) + "\n").encode("utf-8")


def write_test_output(path: Path, value: dict[str, Any]) -> None:
    """只在本轮私有盘写两种测试产物；直接写入，保留通用 rename 的拒绝规则。"""
    if path.name not in {TEST_RESULT_NAME, TEST_FAILURE_NAME}:
        raise RuntimeContractError("专用测试输出类型非法")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(test_result_bytes(value))


def _check_test_result(value: Any, spec: dict[str, Any], *, partial: bool, returncode: int) -> None:
    fields = {"schema_version", "unit_id", "tests", "tests_run", "successful"} | ({"partial"} if partial else set())
    if (not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != TEST_RESULT_SCHEMA
            or value["unit_id"] != spec["unit_id"] or not isinstance(value["tests"], dict)
            or not isinstance(value["successful"], bool) or not isinstance(value["tests_run"], int)
            or isinstance(value["tests_run"], bool) or not 0 < value["tests_run"] <= len(spec["test_ids"])
            or not set(value["tests"]) <= set(spec["test_ids"])):
        raise RuntimeContractError("测试结果缺件、损坏或与本次测试集合错绑")
    failures = {"failed", "error", "unexpected_success"}
    for item in value["tests"].values():
        if (not isinstance(item, dict) or not {"outcome", "seconds"} <= set(item) or set(item) - {"outcome", "seconds", "reason"}
                or item["outcome"] not in failures | {"passed", "skipped", "expected_failure"}
                or (item["seconds"] is not None and (isinstance(item["seconds"], bool)
                    or not isinstance(item["seconds"], (int, float)) or not math.isfinite(item["seconds"]) or item["seconds"] < 0))
                or ("reason" in item and not isinstance(item["reason"], str))):
            raise RuntimeContractError("测试逐项结果格式非法")
    failed = any(item["outcome"] in failures for item in value["tests"].values())
    if partial:
        if value.get("partial") is not True or value["successful"] is not False or not failed:
            raise RuntimeContractError("失败旁路不能冒充成功或空证据")
    elif (set(value["tests"]) != set(spec["test_ids"]) or value["tests_run"] != len(spec["test_ids"])
          or value["successful"] == failed or value["successful"] != (returncode == 0)):
        raise RuntimeContractError("测试结果全集、成功状态或退出码不一致")


def _check_observed_failure(result: dict[str, Any], failure: dict[str, Any] | None) -> None:
    """首次已完整持久化的失败不能被最终结果改成成功；后续失败只追加在完整结果里。"""
    if result["successful"] != (failure is None):
        raise RuntimeContractError("最终测试结果与已观察失败凭证矛盾")
    if failure is not None:
        for test_id, item in failure["tests"].items():
            if item["outcome"] in {"failed", "error", "unexpected_success"} and result["tests"].get(test_id) != item:
                raise RuntimeContractError("最终测试结果覆盖了已观察的完整失败")


def _read_test_artifact(path: Path, spec: dict[str, Any], *, partial: bool, returncode: int) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                or before.st_size > MAX_TEST_RESULT_BYTES):
            raise RuntimeContractError("测试产物必须是本轮普通文件且不得超过上限")
        raw = stream.read(MAX_TEST_RESULT_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > MAX_TEST_RESULT_BYTES or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise RuntimeContractError("测试产物在读取期间变化")
    value = json.loads(raw)
    _check_test_result(value, spec, partial=partial, returncode=returncode)
    if raw != test_result_bytes(value):
        raise RuntimeContractError("测试产物不符合专用编码合同")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "payload": value}


def _export_test_artifact(artifact: dict[str, Any], destination: Path) -> dict[str, Any]:
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise RuntimeContractError("导出测试结果不能经过符号链接")
    if not destination.parent.is_dir():
        raise RuntimeContractError("原调度器的结果目录缺失")
    temporary = destination.with_name(destination.name + ".runtime-test-export.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(test_result_bytes(artifact["payload"]))
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    if hashlib.sha256(destination.read_bytes()).hexdigest() != artifact["sha256"]:
        raise RuntimeContractError("测试结果导出摘要不符")
    return {**artifact, "destination": str(destination)}


def export_test_results(value: dict[str, Any], contract: dict[str, Any], document: dict[str, Any],
                        inputs: list[dict[str, Any]], *, returncode: int) -> dict[str, Any]:
    """审计流结束后只导出固定测试结果与失败旁路；未知产物、错绑和缺件均拒绝。"""
    _test_transport_check(value, contract, inputs)
    # 导出前核本轮生成证明；载荷绑定与最终导出凭证随后在完整消费重放里复核。
    covered = replay_runtime(document, [item for item in inputs if item.get("name") != "runtime-test-spec"])
    root, destination = Path(contract["temporary_root"]), Path(value["result_path"])
    failure = None
    failure_path = root / TEST_FAILURE_NAME
    if failure_path.exists():
        if str(failure_path) not in covered:
            raise RuntimeContractError("失败旁路没有本轮生成证明")
        failure = _read_test_artifact(failure_path, value["spec"], partial=True, returncode=returncode)
        # 即使进程在最终结果形成前中止，已观察失败仍保留在原宿主旁路。
        failure = _export_test_artifact(failure, destination.with_suffix(".observed-failure.json"))
    result_path = root / TEST_RESULT_NAME
    if str(result_path) not in covered:
        raise RuntimeContractError("最终测试结果缺件或没有本轮生成证明")
    result = _read_test_artifact(result_path, value["spec"], partial=False, returncode=returncode)
    _check_observed_failure(result["payload"], failure["payload"] if failure is not None else None)
    result = _export_test_artifact(result, destination)
    return {"schema_version": TEST_EXPORT_SCHEMA, "status": "exported", "transport": value,
            "returncode": returncode, "result": result, "observed_failure": failure}


def _replay_test_export(document: dict[str, Any], inputs: list[dict[str, Any]], contract: dict[str, Any], covered: set[str]) -> None:
    completion = document["runtime"]["completion"]
    bindings = [item for item in inputs if item.get("name") == "runtime-test-spec"]
    export = completion.get("test_export")
    if not bindings and export is None:
        return
    if (len(bindings) != 1 or not isinstance(export, dict)
            or set(export) != {"schema_version", "status", "transport", "returncode", "result", "observed_failure"}
            or export.get("schema_version") != TEST_EXPORT_SCHEMA or export.get("status") != "exported"
            or not isinstance(export.get("returncode"), int) or isinstance(export["returncode"], bool)
            or export["returncode"] != completion.get("returncode")):
        raise RuntimeContractError("专用测试导出凭证缺失、失败或与结束凭证错绑")
    transport = _test_transport_check(export["transport"], contract, inputs)
    for name, filename, partial in (("result", TEST_RESULT_NAME, False), ("observed_failure", TEST_FAILURE_NAME, True)):
        item = export[name]
        if item is None and partial:
            continue
        source = str(Path(contract["temporary_root"]) / filename)
        destination = Path(transport["result_path"])
        if partial:
            destination = destination.with_suffix(".observed-failure.json")
        if (not isinstance(item, dict) or set(item) != {"path", "sha256", "payload", "destination"}
                or item.get("path") != source or source not in covered or item.get("destination") != str(destination)
                or "write" not in dict(document.get("accesses") or []).get(source, [])):
            raise RuntimeContractError("测试导出产物的类型、坐标或本轮生成证明不符")
        _check_test_result(item["payload"], transport["spec"], partial=partial, returncode=export["returncode"])
        if item["sha256"] != hashlib.sha256(test_result_bytes(item["payload"])).hexdigest():
            raise RuntimeContractError("测试导出产物摘要漂移")
    _check_observed_failure(export["result"]["payload"],
                            export["observed_failure"]["payload"] if export["observed_failure"] is not None else None)


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
    device = os.lstat("/dev/null")
    if not stat.S_ISCHR(device.st_mode):
        raise RuntimeContractError("/dev/null 不是标准字符设备")
    if sys.platform.startswith("linux") and (os.major(device.st_rdev), os.minor(device.st_rdev)) != (1, 3):
        raise RuntimeContractError("/dev/null 设备身份不符")
    terminal = os.lstat("/dev/tty")
    if not stat.S_ISCHR(terminal.st_mode) or (sys.platform.startswith("linux")
            and (os.major(terminal.st_rdev), os.minor(terminal.st_rdev)) != (5, 0)):
        raise RuntimeContractError("/dev/tty 设备身份不符")
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as error:
        if error.errno != errno.ENXIO:
            raise RuntimeContractError("控制终端探测不符合无终端合同") from error
        terminal_state = "absent-ENXIO"
    else:
        os.close(descriptor)
        terminal_state = "present"
    payload = {"contract": contract, "platform": {"system": platform.system(), "arch": platform.machine(),
               "kernel": platform.release()}, "environment": [environment_snapshot(path) for path in contract["environment_paths"]],
               "null_device": {"mode": device.st_mode, "uid": device.st_uid, "gid": device.st_gid, "rdev": device.st_rdev},
               "terminal": {"state": terminal_state, "mode": terminal.st_mode, "uid": terminal.st_uid,
                            "gid": terminal.st_gid, "rdev": terminal.st_rdev}}
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


def validate_context(context: Any, inputs: list[dict[str, Any]], *, historical: bool = False) -> dict[str, Any]:
    if not isinstance(context, dict) or context.get("schema_version") != CONTEXT_SCHEMA:
        raise RuntimeContractError("隔离环境凭证缺失")
    entries = [entry for entry in inputs if entry.get("name") == "runtime-contract"]
    if len(entries) != 1:
        raise RuntimeContractError("隔离合同未与输入摘要绑定")
    entry = entries[0]
    contract = validate_contract(entry.get("detail", {}).get("contract"))
    if historical:
        detail = entry.get("detail") or {}
        expected = {"category": "environment", "name": "runtime-contract",
                    "sha256": digest({key: value for key, value in detail.items() if key != "schema_version"}), "detail": detail}
    else:
        expected = contract_entry(contract)
    if entry != expected or context.get("contract_entry") != entry:
        raise RuntimeContractError("隔离合同、平台、设备或环境输入漂移")
    if entry["detail"]["terminal"]["state"] != "absent-ENXIO":
        raise RuntimeContractError("隔离执行必须无控制终端")
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
        if (not isinstance(row.get("mount_id"), int) or row["mount_id"] <= 0
                or row.get("filesystem") != "tmpfs" or expected[row["target"]] not in row.get("options", [])):
            raise RuntimeContractError("实际挂载类型或只读属性不符")
    if context.get("empty_masks") != {path: True for path in contract["mask_roots"]}:
        raise RuntimeContractError("生产别名没有被实际空盘遮挡")
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


def replay_runtime(document: dict[str, Any], inputs: list[dict[str, Any]], *, historical: bool = False) -> set[str]:
    """重放输出生成顺序、隔离身份和结束凭证；只返回确已证明的路径，不能按目录整段放行。"""
    runtime = document.get("runtime") or {}
    contract = validate_context(runtime.get("context"), inputs, historical=historical)
    completion = runtime.get("completion") or {}
    if any(completion.get(key) is not True for key in
           ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged", "command_finished")):
        raise RuntimeContractError("隔离执行未结束或输入／环境在执行期间变化")
    host_schema = _read_audit().HOST_SCHEMA
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == host_schema]
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
    terminal_events = runtime.get("terminal_events", [])
    terminal_kinds = set()
    for event in terminal_events:
        if not isinstance(event, dict) or set(event) != {"name", "kind", "errno", "succeeded"}:
            raise RuntimeContractError("控制终端观察格式错误")
        terminal_kinds.add(event["kind"])
        if event["name"] in {"open", "openat"} and event["errno"] == "ENXIO" and event["succeeded"] is False:
            continue
        if event["name"] in {"stat", "lstat", "newfstatat", "statx", "access", "faccessat", "faccessat2"} and event["kind"] == "stat" and event["succeeded"] is True:
            continue
        raise RuntimeContractError("实际控制终端访问超出无终端探测合同")
    if terminal_kinds != set(paths.get("/dev/tty", [])):
        raise RuntimeContractError("控制终端轨迹与访问集合不一致")
    if terminal_kinds:
        covered.add("/dev/tty")
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
    _replay_test_export(document, inputs, contract, covered)
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
        rows.append({"mount_id": int(parts[0]), "target": target, "filesystem": fs[0], "options": parts[5].split(","),
                     "propagation": parts[6:]})
    return rows


def _visible_mounts(rows: list[dict[str, Any]], targets: set[str]) -> list[dict[str, Any]]:
    """旧 bind mount 仍列在 mountinfo 中；用实际目录描述符的挂载 ID 选择当前可见层，不能按行序猜。"""
    result = []
    for target in sorted(targets):
        descriptor = os.open(target, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = Path(f"/proc/self/fdinfo/{descriptor}").read_text()
            match = re.search(r"^mnt_id:\s*(\d+)\s*$", info, re.M)
            if match is None:
                raise RuntimeContractError("实际目录描述符没有挂载身份")
            candidates = [row for row in rows if row["mount_id"] == int(match[1]) and row["target"] == target]
            if len(candidates) != 1:
                raise RuntimeContractError("实际可见挂载不能与 mountinfo 唯一对应")
            result.append(candidates[0])
        finally:
            os.close(descriptor)
    return result


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
    test_transport = request.get("test_transport")
    bound_inputs = [entry, *request["host_entries"]]
    if test_transport is not None:
        if not isinstance(request.get("test_spec"), dict):
            raise RuntimeContractError("受管请求缺少完整测试规格输入")
        bound_inputs.append(request.get("test_spec"))
        _test_transport_check(test_transport, contract, bound_inputs)
        if (request["argv"] != _runtime_test_command(test_transport)
                or Path(test_transport["result_path"]).parent != trace_path.parent.parent / "units"):
            raise RuntimeContractError("受管请求的测试命令或导出坐标错绑")
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
    mounts = _visible_mounts(rows, wanted)
    context = {"schema_version": CONTEXT_SCHEMA, "contract_entry": entry, "parent_namespace": request["parent_namespace"],
               "namespace": namespace, "empty_temporary_root": not any(root.iterdir()),
               "empty_masks": {mask: not any(Path(mask).iterdir()) for mask in contract["mask_roots"]},
               "private_mounts": not any(any(value.startswith(("shared:", "master:")) for value in row["propagation"]) for row in rows),
               "mounts": mounts, "setup_commands": setup_commands, "setup_success": True,
               "setup_trace": "".join(setup_parts), "setup_trace_sha256": hashlib.sha256("".join(setup_parts).encode()).hexdigest()}
    context_path = trace_path.with_name(trace_path.name + ".context.json")
    _write(context_path, context)
    validate_context(context, [entry])
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
        if test_transport is not None:
            try:
                exported = export_test_results(test_transport, contract, document, bound_inputs, returncode=result.returncode)
            except (RuntimeContractError, OSError, ValueError, TypeError, KeyError) as error:
                document["runtime"]["completion"]["test_export"] = {
                    "schema_version": TEST_EXPORT_SCHEMA, "status": "failed", "transport": test_transport, "error": str(error)}
                _write(trace_path, document)
                print(f"专用测试结果运输失败：{error}", file=sys.stderr)
                return 2
            document["runtime"]["completion"]["test_export"] = exported
        _write(trace_path, document)
    elif test_transport is not None:
        raise RuntimeContractError("完整审计轨迹缺失，不能导出测试结果")
    return result.returncode


def prepare(contract: dict[str, Any], *, output: Path, argv: list[str], cwd: str,
            inputs: list[dict[str, Any]], test_spec: dict[str, Any] | None = None) -> list[str]:
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
    host_schema = _read_audit().HOST_SCHEMA
    entries = [entry for entry in inputs if entry.get("detail", {}).get("schema_version") == host_schema]
    request = {"contract_entry": contract_entry(contract), "parent_namespace": os.readlink("/proc/self/ns/mnt"),
               "output": str(output), "argv": argv, "cwd": cwd, "host_entries": entries,
               "host_paths": [entry["detail"]["path"] for entry in entries]}
    if test_spec is not None:
        payload = {"unit_id": test_spec["unit_id"], "test_ids": test_spec["test_ids"]}
        transport = {"schema_version": TEST_TRANSPORT_SCHEMA, "spec": test_spec, "spec_sha256": digest(test_spec),
                     "payload_sha256": digest(payload), "temporary_root": contract["temporary_root"], "result_path": argv[-1]}
        _test_transport_check(transport, contract, inputs)
        request.update(test_transport=transport, test_spec=test_spec_entry(test_spec),
                       argv=test_transport_argv(transport, contract, argv, output=output))
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
