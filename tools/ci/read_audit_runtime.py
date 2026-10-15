#!/usr/bin/env python3
"""B-11 隔离运行合同：在私有挂载命名空间建立空临时盘，分别保存挂载和被测命令轨迹。

本模块属于执行器的可信启动部分，代码摘要纳入执行器身份。它不能把普通宿主目录认作本轮输出；
只有经实际挂载核对的私有 tmpfs 才能提供输出证明。未支持的内核输入和系统调用仍拒绝承接。

输出归属有两套合同并存：``OutputTracker``（v3，按路径记账，rename／link／失败探测一律拒绝）保持原样，
供历史凭证与 control 四链重放；``OutputTrackerV4``（v4，按对象身份记账）供普通受管单元使用。
普通单元由 ``prepare`` 在受管请求里写入 ``trace_contract``，``run_child`` 据此让过滤器产出 v4 轨迹；
control 四链有自己的 prepare／run_child，不经过这个启用点。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import importlib.util
import itertools
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

# 轨迹合同版本（显式开关，默认 v3）。v3 是历史合同：输出事件 6 键、旧 OutputTracker、旧重放逻辑，字节行为不变，
# control 四链的 sidecar 复用的就是它；v4 在此基础上支持 rename／link／失败探测并对重复事件按名字级版本去重（WP-R9）。
# 文档版本字符串的权威定义在同目录 read_audit.py（RUNTIME_TRACE_SCHEMA_V4）；这里再存一份，是为了让重放 v3 凭证的路径
# 不依赖同目录 read_audit.py 里新增的名字（新旧文件混用时 v3 验真也不会因此出错）。两处取值由测试比对，防止漂移。
TRACE_SCHEMA_V4 = "read-audit-trace/v4"
TRACE_CONTRACT_V3 = "v3"
TRACE_CONTRACT_V4 = "v4"
TRACE_CONTRACTS = (TRACE_CONTRACT_V3, TRACE_CONTRACT_V4)
# 输出事件条数上限（去重之后仍超限则判失败，不静默截断）。这是 v3 合同的上限，数值不变（历史凭证与 control 四链按它验真）。
MAX_OUTPUT_EVENTS = 200000
# v4 合同的上限，与 read_audit.py 里的同名常量必须相等（两个文件互不导入）：由 test_ci_audit_hardening_h1 的
# test_parser_and_replay_v4_limits_are_equal 守住（test_ci_read_audit_wp_r2 的 OutputEventLimitTests.test_constants 另有一份同样的断言）。版本语义：只对
# read-audit-trace/v4 生效；已被旧上限截断的轨迹带 output-event-limit 未解析原因，验真永远失败，与本数值无关。
MAX_OUTPUT_EVENTS_V4 = 1000000

# v4 事件里需要识别的系统调用名。两路径调用的两端各产生一条事件，由 role／peer 配对。
TWO_PATH_NAMES = frozenset({"rename", "renameat", "renameat2", "link", "linkat"})
LINK_NAMES = frozenset({"link", "linkat"})
SYMLINK_NAMES = frozenset({"symlink", "symlinkat"})
# WP-R3：openat2 是和 openat 同构的“按路径打开并返回描述符”的调用（标志在 struct open_how 里，解析器已解码），
# 原先不在这里，于是用 openat2 创建／写私有盘文件会被判“未建立合同的输出操作”，而且“返回描述符落在私有盘外”的检查被整个跳过。
OPEN_NAMES = frozenset({"open", "openat", "openat2", "creat"})
# WP-R3：对符号链接“名字本身”不跟随的只读调用。对一个符号链接对象，只有这些调用（连同下面 _symlink_name_verdict 里列的
# unlink、rename、创建被拒绝等）才不会解引用链接：lstat 系读的是链接自己的 inode 元数据，readlink 读的是链接文本。
# 这两者的内容都是被测代码自己写进去的（symlink 调用的参数），不含任何宿主信息。
LSTAT_NAMES = frozenset({"lstat", "lstat64"})
FSTATAT_NAMES = frozenset({"newfstatat", "fstatat64", "statx"})
READLINK_NAMES = frozenset({"readlink", "readlinkat"})
# 对已证明的对象只改元数据或内容、不增删名字的调用：不改变名字空间，也不需要额外归属证明。
METADATA_WRITE_NAMES = frozenset({"chmod", "fchmodat", "fchmodat2", "chown", "lchown", "fchownat",
                                  "utime", "utimes", "utimensat", "futimesat", "truncate", "truncate64"})
ROLES = ("source", "destination")
# WP-R9：成功时永远不参与去重的写类调用（``OutputTrackerV4.dedup_key`` 返回 None）：创建／删除目录项与符号链接几乎总是改名字空间，
# 被拒绝时的判定又依赖子项个数等没有版本号覆盖的状态，保留它们的代价（实测合计约 2.5 万条）可以忽略，换来键的正确性证明更简单。
NEVER_DEDUP_WRITES = frozenset({"mkdir", "mkdirat", "unlink", "unlinkat", "rmdir"}) | SYMLINK_NAMES
# 这两个 renameat2 标志会让两个名字互换或在原位留下占位对象，对象身份模型不支持，一律拒绝。
UNSUPPORTED_RENAME_FLAGS = frozenset({"RENAME_EXCHANGE", "RENAME_WHITEOUT"})


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
    """只在本轮私有盘写两种测试产物；一律以 O_CREAT|O_EXCL 直接写目标名，不走「写临时文件再 rename」。

    为什么不用 rename：v3 合同（历史凭证、control 四链的重放仍按它）的跟踪器对 rename 一律拒绝，写成「临时文件 + rename」
    在 v3 下得不到输出归属证明。v4 合同的跟踪器（OutputTrackerV4）已经能证明 rename，但这个函数要同时满足两种合同，
    所以保持直接写入——O_EXCL 创建本身就是最干净的「首次写入」创建证明，两个合同都认。"""
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


class OutputTrackerV4:
    """v4 轨迹合同的输出归属跟踪器：按「对象身份」而不是按路径证明私有临时盘里每个名字的来源。

    为什么换模型：采集工具的原子写几乎处处是「写临时文件 → rename／link 发布」，旧跟踪器（OutputTracker）对
    rename、link 一律不认，对失败的创建探测（makedirs(exist_ok=True) 之类的 EEXIST）也一律判问题，导致大多数
    单元的输出归属证明失败。这里改成：

    * 名字 → 对象：``names`` 把绝对路径映射到对象编号；对象有类型（普通文件／目录／符号链接）和当前名字个数。
      硬链接让两个名字指向同一对象，删除其中一个名字不影响另一个；
    * 一个名字只有来自「本轮的创建」或「对已证明名字的 rename／link」才算已证明，盘外的东西永远进不来；
    * 写类调用失败按错误码与已知状态判，不再一刀切；读取、探测、执行类调用沿用旧规则，只要求路径已证明。

    符号链接（WP-R3）：私有盘内可以创建符号链接，对象类型为 ``symlink``，链接文本（symlink 调用的第一个参数）原样记在
    ``link_text`` 里，**永远不解引用**。链接对象只能被“不跟随”的调用碰到——unlink／unlinkat、rename 的任一端、
    lstat 系（lstat、AT_SYMLINK_NOFOLLOW 的 fstatat／statx）、readlink、创建被拒绝（EEXIST）、O_NOFOLLOW 打开得到 ELOOP；
    其余一切对链接名字的访问（打开、stat、access、chdir、execve、chmod……）以及一切路径的**中间分量**是链接的访问，
    都按“跟随式访问”判问题——因为这些调用会解引用链接，而解引用后的路径是宿主输入，strace 对非描述符调用不会把它打印出来。
    为什么这样安全：不跟随的调用只会读到链接 inode 自己的元数据和链接文本，两者都是被测代码自己写入的；
    会解引用的调用一律失败关闭，所以链接指向盘外（``/etc/passwd``）本身不构成读取，只有真的去跟随时才报。

    安全边界（保守拒绝，记入 problems）：跟随式访问、RENAME_EXCHANGE／RENAME_WHITEOUT、另一端不在盘内的两路径调用、
    来源未证明的 rename／link、把目录或符号链接硬链接、目录覆盖非空目录、目录移进自己的子目录，以及写类调用除
    EEXIST／ENOENT／ENOTDIR 之外的任何失败。

    另有三道「内核不可能产生的成功」自检——硬链接成功却目的名字已知、``O_CREAT|O_EXCL`` 创建成功却落在已知名字上、
    ``RENAME_NOREPLACE`` 成功却目的名字已知：内核只会在目的不存在时让这三种调用成功，轨迹却声称成功，说明名字表与真实
    状态已经脱节，所以判矛盾（记入 problems，不改名字表）。这三道自检不会放行任何东西——这类声明本来也不会把未证明的名字
    变成已证明——但少一道自检，就少一次发现「归属状态已经和真实文件系统对不上」的机会。

    ``observe_event`` 返回「本事件是否改变了名字空间状态」：解析器据此推进变更纪元、并决定该事件是否一定保留（改了状态的事件永不去重）；
    重放端用同一个返回值重算纪元，所以任何会改名字空间的分支都必须返回 True，反之必须返回 False。

    WP-R8（乱序容忍：待补证／销账）：``strace -f`` 按「各被追踪进程的系统调用返回被 tracer 处理的先后」逐行输出，不是因果顺序——
    进程 A 的 ``openat(O_CREAT)`` 在内核里早已完成，但 A 停在退出点等 tracer 取走，进程 B 紧接着的 ``stat`` 却先被打印。
    于是私有盘里会出现「先成功读到、后看到创建事件」的名字（10-15 ARM64 实测：supervisor 心跳进程写 minute-ledger.ndjson、主进程 stat 它）。
    原规则把这样的成功读取立即判为「首次读取前未证明由本轮生成」，把正常行为判成失败。现在改成两步：

    * **待补证**：私有盘内一个**未证明名字**上的、**不改名字空间且要求对象存在**的访问——kind 不是 write 也不是 missing 的事件，
      不论成功还是失败（EACCES、EINVAL 一类的失败同样意味着对象存在）——不立即判问题，而是把这个名字记进 ``awaiting``。
      这类事件仍然返回 False（没有改变名字空间：对象早已存在，跟踪器只是记了一笔债）；
    * **销账**：之后只要出现**同一个名字**的创建——``O_CREAT`` 的 open／creat、mkdir／mkdirat、symlink／symlinkat、
      link／linkat 的目的名字、rename／renameat／renameat2 把别处对象（目录则连同子树）改名到该名字——就从 ``awaiting`` 里划掉；
    * **清算**：轨迹结束仍未销账的名字，在 ``report()`` 里以与原来相同的文字（「首次读取前未证明由本轮生成：」+ 路径）报为问题。
      ``report()`` 只读取不修改状态，多次调用结果相同，重放端据此与文档里的 ``outputs`` 逐字相等比对。

    为什么这样安全（无需给乱序容忍窗口设上限）：私有盘是单元启动时检查为空的 tmpfs（``empty_temporary_root``），挂载在新的
    挂载命名空间里，只有本单元被追踪的进程树能在里面创建对象；每一种创建方式要么被跟踪器建模，要么被解析器登记未解析
    （bind、mknodat 判问题、AT_EMPTY_PATH 的 linkat 另一端不在盘内……）而使整份轨迹失败。所以盘内任何存在的名字必有一次可见的建模
    创建，该创建事件一定出现在轨迹里（只是可能比读取晚），而「从未创建却存在」的名字永远销不了账、必在清算时被报出。
    窗口不设上限的理由：乱序量取决于 tracer 的唤醒调度（可达上千条事件），任何上限都是任意数字；而销账只认**同一个名字**，
    窗口多长都不会让一个从未被创建的名字通过。读取内容类事件（``read``）比 ``stat`` 多暴露的是文件内容，但盘内内容只可能来自
    本单元自己的进程（它读取的宿主文件各有自己的宿主路径事件，由宿主快照逐项核对），所以并不新增任何宿主信息；
    宿主路径（不在私有盘内）不适用本规则，仍由宿主快照与策略声明判定。

    WP-R9（名字级去重）：解析器要对重复的输出事件去重（10-15 实测 compare_accept 产生 128 万条事件，88% 是对私有盘内路径的
    重复成功 stat／读；全局变更纪元被约 5.5 万条写／rename／unlink 切得太碎，去重效果有限）。去重的前提是「两条事件的判定相同」，
    而一条事件的判定只取决于**它所涉及的几个名字**的状态，不取决于整个名字空间。所以这里给每个名字一个**版本号**
    （``versions``：名字 → 唯一且单调的整数，未出现过的名字为 0），任何改变该名字**存在性、对象身份或待补证状态**的操作——创建、
    解绑、rename 的来源与目的（目录则连同整棵子树的每个名字）、硬链接目的、待补证的记账与销账——都给被影响的名字换一个新版本号；
    ``dedup_key(event)`` 在事件**到达时**（先于 ``observe_event``）给出去重键：事件本身的全部字段，加上它判定所依赖的状态：

    * 路径自己的版本号（⇒ 名字是否已知、指向哪个对象、对象类型是否符号链接、是否待补证）；
    * 路径的某个祖先是否符号链接（``_through_symlink``，只在存在符号链接对象时才可能为真；目录被改名／删除再换成链接，
      祖先的绑定变了，这一位随之变化，不需要「子树失效」的额外记账）；
    * 写类事件还要加「父目录是否已证明为目录」（创建失败判定用）；
    * 两路径调用的失败事件，两端的版本号、两端「是否经符号链接」与目的端父目录是否为目录（来源端与目的端事件用同一份
      归一化的状态，保证一次调用的两条事件要么一起被去重、要么一起保留，不破坏重放端的配对检查）。

    键相同 ⇒ 判定相同：随机对拍（test_ci_read_audit_wp_r8）逐条核对「同一个键的所有事件，观察结果（是否改状态、新增问题）完全一致」。
    不参与去重（``dedup_key`` 返回 None，永远保留）的事件：成功的两路径调用、成功的 mkdir／unlink／rmdir／symlink（它们的
    判定还依赖子项个数等没有版本号覆盖的状态，且几乎总是改名字空间）。另有一类即使键重复也必须保留：紧跟在一次文件创建之后的
    那条事件（``consumes_hidden_state``，它会消耗 ``_created_by_previous`` 这个隐藏状态，重放端必须看到它，才能和解析端消耗得一样）。
    注意：键里**没有**全局变更纪元——对其它名字的写、rename、unlink 不会使这个名字上的重复读取失效；而对这个名字
    （或带着它的目录）的任何改变都会换新版本号，使之后的同样读取重新保留一条。
    """

    def __init__(self, contract: dict[str, Any]) -> None:
        self.root = validate_contract(contract)["temporary_root"]
        # 名字 → 对象编号；对象 → 类型／名字个数；目录路径 → 直接子项个数（rmdir 与「覆盖非空目录」判定用）。
        self.names: dict[str, int] = {self.root: 0}
        self.kinds: dict[int, str] = {0: "directory"}
        self.links: dict[int, int] = {0: 1}
        self.entries: dict[str, int] = {self.root: 0}
        self.next_object = 1
        # WP-R3：符号链接对象 → 链接文本（symlink 调用的第一个参数，原样保存，从不解引用）。键集合就是“当前存活的符号链接
        # 对象”：符号链接不能被硬链接（_link 拒绝），所以每个链接对象恰有一个名字；为空时所有符号链接检查直接跳过，
        # 没有符号链接的普通单元（绝大多数）不付任何额外开销。
        self.link_text: dict[int, str] = {}
        self.covered: set[str] = set()
        self.problems: set[str] = set()
        # WP-R8：待补证的名字（当时未证明、却已被成功读取／探测的名字），按首次出现顺序；值恒为 None，只用字典保持确定的顺序。
        # 创建该名字的事件到达时划掉；轨迹结束仍留在这里的，由 ``report()`` 报为问题。
        self.awaiting: dict[str, None] = {}
        # WP-R9：名字 → 版本号（见类说明）。版本号取自单调递增的 ``_clock``，所以同一个名字的两个版本号绝不相等，
        # 「名字被删除后又重新创建成同样的对象」也不会和旧版本混同。
        self.versions: dict[str, int] = {}
        self._clock = 0
        # 上一条事件若创建了某个文件名，这里记着它的路径；下一条事件一到就被取出并清除（只对紧邻的下一条事件有效）。只服务于
        # O_CREAT|O_EXCL 的矛盾自检：解析器对一次 openat／open 调用产生两条紧挨着的事件（请求路径一条、返回描述符的真实路径一条，
        # 路径相同），创建名字的是第一条，第二条落在刚创建的名字上，不是矛盾。不影响任何事件的返回值，也不影响名字表。
        self._created_by_previous: str | None = None

    # ------------------------------------------------------------------ 名字表的基本操作

    def _problem(self, text: str, path: str) -> None:
        self.problems.add(text + path)

    def _kind_of(self, path: str) -> str | None:
        """名字当前指向的对象类型；未知名字返回 None。"""
        obj = self.names.get(path)
        return None if obj is None else self.kinds[obj]

    def _touch(self, path: str) -> None:
        """WP-R9：名字的存在性、对象身份或待补证状态变了，给它换一个新版本号。"""
        self._clock += 1
        self.versions[path] = self._clock

    def _await(self, path: str) -> None:
        """WP-R8：记一笔待补证——未证明的名字被成功读取／探测，等同名字的创建事件来销账（见类说明）。"""
        if path not in self.awaiting:
            self.awaiting[path] = None
            self._touch(path)

    def _settle(self, path: str) -> None:
        """WP-R8：名字刚被绑定到一个对象（创建、硬链接目的、rename 带入），划掉它的待补证。只认同一个名字。
        调用点都紧跟在绑定名字（``_touch``）之后，版本号已经换过，这里不用再换。"""
        self.awaiting.pop(path, None)

    def _create(self, path: str, kind: str, text: str | None = None) -> None:
        """在已证明的父目录下新建对象并绑定名字（调用方已确认父目录已知、路径未知）。

        ``text`` 只给符号链接：链接文本原样记下，不解释、不解引用。
        """
        obj = self.next_object
        self.next_object += 1
        self.names[path] = obj
        self._touch(path)
        self.kinds[obj] = kind
        self.links[obj] = 1
        self.entries[os.path.dirname(path)] += 1
        if kind == "directory":
            self.entries[path] = 0
        if kind == "symlink":
            self.link_text[obj] = text if text is not None else ""
        self._settle(path)

    def _unbind(self, path: str) -> None:
        """解绑一个名字；对象失去最后一个名字后消亡。目录解绑前调用方须确认它没有子项。"""
        obj = self.names.pop(path)
        self._touch(path)
        self.entries[os.path.dirname(path)] -= 1
        self.entries.pop(path, None)
        self.links[obj] -= 1
        if self.links[obj] == 0:
            del self.links[obj]
            del self.kinds[obj]
            self.link_text.pop(obj, None)

    def _move(self, source: str, destination: str, kind: str) -> None:
        """把名字 source（目录则连同整棵子树）改成 destination；调用方已确认 destination 不存在且不在 source 之内。"""
        if kind == "directory":
            prefix = source + "/"
            moved = [item for item in self.names if item == source or item.startswith(prefix)]
        else:
            moved = [source]
        for item in moved:
            renamed = destination + item[len(source):]
            self.names[renamed] = self.names.pop(item)
            self._touch(item)            # 来源端的名字消失（目录则连同子树里的每个名字）
            self._touch(renamed)         # 目的端的名字出现
            if item in self.entries:
                self.entries[renamed] = self.entries.pop(item)
            self._settle(renamed)
        self.entries[os.path.dirname(source)] -= 1
        self.entries[os.path.dirname(destination)] += 1

    # ------------------------------------------------------------------ 符号链接（WP-R3）

    def has_symlinks(self) -> bool:
        """当前是否有存活的符号链接对象；解析器据此决定要不要做路径形态检查，本类据此跳过全部符号链接检查。"""
        return bool(self.link_text)

    def is_symlink(self, path: str) -> bool:
        """路径此刻是不是一个已证明的符号链接名字（只看名字表，从不解引用）。"""
        obj = self.names.get(path)
        return obj is not None and obj in self.link_text

    def _through_symlink(self, path: str) -> bool:
        """路径的某个**真祖先**（不含路径自身、不含根）是符号链接名字：内核解析这条路径时必然解引用该链接。

        哪怕链接指向盘内、哪怕调用本身失败（ENOENT 同样取决于链接目标那一端的宿主状态），都算跟随式访问。
        """
        parent = os.path.dirname(path)
        while len(parent) > len(self.root):
            if self.is_symlink(parent):
                return True
            parent = os.path.dirname(parent)
        return False

    def _symlink_name_verdict(self, event: dict[str, Any]) -> str:
        """事件的路径本身（最后一个分量）此刻是符号链接名字时，这个调用是否会解引用它。

        返回 ``"ok"``（不跟随，且不改名字表，直接放行）、``"generic"``（不跟随，交给下面的通用判定：unlink／rename／
        创建被拒绝，它们对名字表的影响由通用逻辑负责）或 ``"problem"``（会跟随、或没有合同，一律判问题）。
        白名单之外的调用名一律是 ``problem``——新增系统调用默认失败关闭。每一项能暴露什么宿主信息：

        * ``unlink``／``unlinkat``、``rename*`` 的任一端：作用在目录项上，Linux 不解引用最后一个分量；
        * ``lstat``／``lstat64``、带 ``AT_SYMLINK_NOFOLLOW`` 的 ``newfstatat``／``fstatat64``／``statx``：读链接自己的 inode 元数据；
        * ``readlink``／``readlinkat``：读链接文本，而它是被测代码自己写的；
        * 对已存在名字的创建被拒绝（``mkdir``／``mknod``／``symlink`` 的 EEXIST，带 ``O_CREAT|O_EXCL`` 的打开得 EEXIST）：
          O_EXCL 不跟随链接；其余创建调用不解引用最后一个分量；
        * 带 ``O_NOFOLLOW`` 的打开得 ``ELOOP``：内核在碰到链接时就拒绝，没有读取目标的任何信息。``O_PATH|O_NOFOLLOW`` 会
          成功地得到一个指向链接本身的描述符，之后 ``openat(fd, "")``、``/proc/self/fd`` 等都能把它转成跟随——
          这类描述符的后续使用不可见，保持失败关闭（解析器同样仍登记 symlink-metadata-not-bound）。
        失败的 lstat／readlink（EACCES 之类）在已证明的链接上不可能出现，按矛盾判问题。
        """
        name, ok, error, flags = event["name"], event["succeeded"], event["errno"], set(event["flags"])
        if name in TWO_PATH_NAMES or name in SYMLINK_NAMES or name in {"unlink", "unlinkat"}:
            return "generic"
        if name in LSTAT_NAMES or name in READLINK_NAMES or (name in FSTATAT_NAMES and "AT_SYMLINK_NOFOLLOW" in flags):
            return "ok" if ok and event["kind"] == "stat" else "problem"
        if not ok and error == "EEXIST":
            if name in {"mkdir", "mkdirat", "mknod", "mknodat"}:
                return "generic"
            if name in OPEN_NAMES and name != "creat" and {"O_CREAT", "O_EXCL"} <= flags:
                return "generic"
        if not ok and error == "ELOOP" and name in OPEN_NAMES and "O_NOFOLLOW" in flags and "O_PATH" not in flags:
            return "ok"
        return "problem"

    def _create_symlink(self, event: dict[str, Any]) -> bool:
        """symlink／symlinkat 成功：在已证明的父目录下新建符号链接对象，链接文本原样记下（事件 target 字段）。

        暴露的宿主信息：无——创建链接不读取链接文本指向的任何东西，目标是否存在、是否在盘内都不影响调用结果。
        """
        path, text = event["path"], event["target"]
        if not isinstance(text, str) or text == "":
            self._problem("符号链接的链接文本未记录或为空，与成功的创建矛盾：", path)
        elif path in self.names:
            self._problem("符号链接创建成功却落在已知名字上，与轨迹矛盾：", path)
        elif self._kind_of(os.path.dirname(path)) != "directory":
            self._problem("新建符号链接的父目录未证明：", path)
        else:
            self._create(path, "symlink", text)
            return True
        return False

    # ------------------------------------------------------------------ 去重键（WP-R9）

    def _through(self, path: str) -> bool:
        """路径的某个真祖先是否符号链接；没有符号链接对象时恒为 False（省掉逐级查找）。"""
        return self._through_symlink(path) if self.link_text else False

    def dedup_key(self, event: dict[str, Any]) -> tuple | None:
        """事件到达时的去重键：键相同 ⇒ 跟踪器对这两条事件的判定（是否改名字空间、新增哪些问题）完全相同。

        必须在 ``observe_event`` **之前**调用——键描述的是事件到达时相关名字的状态。返回 None 表示这条事件永远保留，原因见类说明。
        解析器只对「observe_event 返回 False」的事件按此键去重；返回 True 的事件（改了名字空间）本来就永不去重。
        两路径调用的两条事件共用同一份归一化状态（来源端、目的端各自的版本与是否经链接），所以一次失败调用的两条事件
        要么都被去重、要么都保留——重放端要求盘内两端成对出现。
        隐藏状态 ``_created_by_previous`` 只被「``O_CREAT|O_EXCL`` 成功落在已知名字上」的矛盾自检读取，所以只有这类事件的键里带
        「它是不是刚被上一条事件创建」这一位；另外，紧跟在创建之后的**任何**事件都会消耗该隐藏状态，必须保留
        （``consumes_hidden_state``），否则重放端少看一条事件，隐藏状态的消耗就和解析端不同。
        """
        name, kind, path, peer, role = event["name"], event["kind"], event["path"], event["peer"], event["role"]
        succeeded = event["succeeded"]
        own = (path, kind, name, succeeded, event["errno"], tuple(event["flags"]), event["target"], role, peer)
        versions = self.versions
        if name in TWO_PATH_NAMES:
            if succeeded or role not in ROLES or peer is None or not under(peer, self.root):
                return None        # 成功的两路径调用要么改状态、要么是「同一对象」的空操作，两端一律保留；畸形事件本身就是问题
            source, destination = (path, peer) if role == "source" else (peer, path)
            return own + (versions.get(source, 0), versions.get(destination, 0), self._through(source), self._through(destination),
                          self._kind_of(os.path.dirname(destination)) == "directory")
        if succeeded and kind == "write" and name in NEVER_DEDUP_WRITES:
            return None
        state = (versions.get(path, 0), self._through(path))
        if kind == "write":
            state += (self._kind_of(os.path.dirname(path)) == "directory",)
            if name in OPEN_NAMES and {"O_CREAT", "O_EXCL"} <= set(event["flags"]):
                state += (self._created_by_previous == path,)
        return own + state

    def consumes_hidden_state(self) -> bool:
        """下一条事件是否会消耗隐藏状态：上一条事件刚创建了某个文件名。这样的事件必须保留（见 ``dedup_key``）。"""
        return self._created_by_previous is not None

    # ------------------------------------------------------------------ 事件入口

    def observe_event(self, event: dict[str, Any]) -> bool:
        # 取出「上一条事件刚创建的路径」并清除：它只对紧邻的这一条事件有效（说明见 __init__）。
        created_by_previous, self._created_by_previous = self._created_by_previous, None
        path, kind, name, succeeded = event["path"], event["kind"], event["name"], event["succeeded"]
        if not under(path, self.root):
            return False
        self.covered.add(path)
        target = event["target"]
        if self.link_text:
            # WP-R3：存在符号链接对象时，先判是否会解引用它（没有链接时这一整段被跳过）。排在“返回描述符落点”检查之前：
            # 经链接打开得到盘外的描述符，两种问题都成立，这里的说明更具体；返回描述符那一检查仍是第二道防线，对非链接名字照旧生效。
            if self._through_symlink(path):
                self._problem("路径的中间分量是符号链接，属于跟随式访问：" + name + ":", path)
                return False
            if self.is_symlink(path):
                verdict = self._symlink_name_verdict(event)
                if verdict == "problem":
                    self._problem("跟随符号链接的访问或没有合同的调用（链接名字只允许 unlink、rename、lstat、readlink 等不跟随的调用）："
                                  + name + ":", path)
                    return False
                if verdict == "ok":
                    return False
        if name in OPEN_NAMES and succeeded and (target is None or not under(target, self.root)):
            self._problem("文件描述符未绑定到私有临时盘：", path)
            return False
        if name in TWO_PATH_NAMES:
            return self._two_path(event)
        if not succeeded:
            self._failed(event)
            return False
        if name in SYMLINK_NAMES:
            return self._create_symlink(event)
        known = path in self.names
        if kind != "write":
            # 读取、探测、执行、列目录：名字未证明时不立即判问题，记待补证，等同名字的创建事件来销账（WP-R8，见类说明）。
            if not known:
                self._await(path)
            return False
        parent_is_directory = self._kind_of(os.path.dirname(path)) == "directory"
        if name in {"mkdir", "mkdirat"}:
            if known:
                self._problem("创建目录时已存在：", path)
            elif not parent_is_directory:
                self._problem("新建目录的父目录未证明：", path)
            else:
                self._create(path, "directory")
                return True
        elif name in OPEN_NAMES:
            if not known:
                if (name != "creat" and "O_CREAT" not in event["flags"]) or not parent_is_directory:
                    self._problem("首次写入缺少创建证明：", path)
                else:
                    self._create(path, "file")
                    self._created_by_previous = path
                    return True
            elif "O_CREAT" in event["flags"] and "O_EXCL" in event["flags"] and created_by_previous != path:
                # O_CREAT|O_EXCL 只在路径原先不存在时才会成功（已存在的名字，不论文件、目录还是符号链接，内核一律返回
                # EEXIST）。成功却落在已知名字上，说明名字表与真实状态已经脱节，判矛盾；矛盾不改名字表，所以仍返回 False。
                # 紧邻豁免：上一条事件刚创建了这个名字——解析器对一次 openat／open 调用产出两条紧挨着的事件（请求路径＋返回
                # 描述符的真实路径），第二条落在第一条刚创建的名字上，那是同一次创建，不是矛盾。真实解析器给这两条事件
                # 相同的调用编号，本可以精确区分；这里不靠调用编号，是因为既有的随机对拍测试用两个调用编号不同的相邻事件表示
                # 一次调用，要让它们零改动通过。代价：紧挨着创建事件的第二次 O_EXCL 成功也被放过；隔了别的事件、或豁免用掉
                # 之后的又一次重复，仍判矛盾。
                # creat 系统调用没有 O_EXCL 标志（等价于 O_CREAT|O_WRONLY|O_TRUNC），重新打开已知文件是合法的，不在此列。
                self._problem("O_CREAT|O_EXCL 创建成功却落在已知名字上，与轨迹矛盾：", path)
        elif name in {"unlink", "unlinkat", "rmdir"}:
            return self._remove(path, name, event["flags"])
        elif name in METADATA_WRITE_NAMES and known:
            pass
        else:
            self._problem("未建立合同的输出操作：" + name + ":", path)
        return False

    def _failed(self, event: dict[str, Any]) -> None:
        """单路径调用失败。

        * 探测不存在的路径（kind 为 missing，即 ENOENT／ENOTDIR）：未知路径合法；已知名字却报不存在，轨迹与归属状态矛盾。
        * 读取、探测、执行、列目录类调用的其他失败：它们既不创建也不删除任何东西，沿用旧规则，只要求访问的是本轮已证明的名字
          （未证明的名字记待补证，等创建事件销账，见类说明 WP-R8；轨迹结束仍未销账才报问题）。
          这一条不能收紧：python3 启动脚本时 glibc 的 realpath 会对私有目录下每一级路径 readlinkat 并得到 EINVAL，
          执行没有执行权限的假程序会得到 EACCES，旧跟踪器对已证明路径上的这类失败一向放行。
        * 写类调用失败（创建、删除、改名、改元数据）：按错误码与已知状态判。EEXIST 落在已知名字上是合法的「创建被拒绝」，
          落在未知路径上说明有来源不明的对象；ENOENT／ENOTDIR 落在未知路径上是合法探测，落在已知名字上是矛盾；
          其余任何错误码（EACCES、EISDIR、ENOTEMPTY 等）一律保守判问题。
        """
        path, kind, name, error = event["path"], event["kind"], event["name"], event["errno"]
        known = path in self.names
        if kind == "missing":
            if known:
                self._problem("已生成对象的缺失探测与轨迹矛盾：", path)
        elif kind != "write":
            # 同上：失败的读取类调用（EACCES、EINVAL……）同样意味着对象存在，名字未证明时记待补证。
            if not known:
                self._await(path)
        elif error == "EEXIST":
            if not known:
                self._problem("EEXIST 落在未知路径，与轨迹矛盾：", path)
        elif error in {"ENOENT", "ENOTDIR"}:
            if known:
                self._problem("已生成对象的缺失探测与轨迹矛盾：", path)
        else:
            self._problem(f"未建立合同的失败调用（{error or '无错误码'}）：{name}:", path)

    def _remove(self, path: str, name: str, flags: list[str] | None = None) -> bool:
        obj = self.names.get(path)
        if obj is None or path == self.root:
            self._problem("删除未证明的输出：", path)
            return False
        kind = self.kinds[obj]
        # unlink 只能删非目录（普通文件或符号链接）、rmdir 只能删目录；unlinkat 不带 AT_REMOVEDIR 时删非目录。
        # WP-R3：事件现在带 AT_* 标志，所以成功的 unlinkat 带着 AT_REMOVEDIR 却落在非目录上能直接判矛盾（内核不会让它成功）；
        # 反过来“不带 AT_REMOVEDIR 的 unlinkat 删目录”这个方向保持旧宽容（既有测试与历史事件流不带标志），不收紧。
        if ((name == "unlink" and kind not in {"file", "symlink"}) or (name == "rmdir" and kind != "directory")
                or (name == "unlinkat" and "AT_REMOVEDIR" in (flags or []) and kind != "directory")):
            self._problem("删除方式与已生成对象类型不符：", path)
            return False
        if kind == "directory" and self.entries.get(path, 0):
            # 内核只会删除空目录；归属状态里还有子项说明有删除没被观察到，轨迹与状态矛盾。
            self._problem("删除了仍有已知子项的目录：", path)
            return False
        self._unbind(path)
        return True

    # ------------------------------------------------------------------ 两路径调用（rename／link）

    def _two_path(self, event: dict[str, Any]) -> bool:
        """rename／link 的所有判定都挂在来源端事件上；目的端事件只登记覆盖路径（上面已登记），不改状态。

        这样跟踪器在两条事件之间不留隐藏状态：任何一条事件被去重或单独重放，结果都只取决于事件本身与名字表。
        """
        path, role, peer = event["path"], event["role"], event["peer"]
        if event["kind"] != "write" or role not in ROLES:
            self._problem("两路径调用缺少角色标记：", path)
            return False
        if peer is None or not under(peer, self.root):
            # 另一端在盘外（或无法还原，例如 AT_EMPTY_PATH）：无法证明对象来源或去向，拒绝。
            self._problem("两路径调用的另一端不在私有临时盘内：", path)
            return False
        if role == "destination":
            return False
        if not event["succeeded"]:
            self._two_path_failed(event)
            return False
        if event["name"] in LINK_NAMES:
            return self._link(path, peer)
        if UNSUPPORTED_RENAME_FLAGS & set(event["flags"]):
            self._problem("不支持 RENAME_EXCHANGE／RENAME_WHITEOUT：", path)
            return False
        return self._rename(path, peer, noreplace="RENAME_NOREPLACE" in event["flags"])

    def _two_path_failed(self, event: dict[str, Any]) -> None:
        source, destination, error = event["path"], event["peer"], event["errno"]
        if error == "EEXIST":
            # link 的目标已存在，或 RENAME_NOREPLACE 的目标已存在：两端都必须是本轮已知的名字。
            if source not in self.names or destination not in self.names:
                self._problem("EEXIST 落在未知路径，与轨迹矛盾：", source)
        elif error in {"ENOENT", "ENOTDIR"}:
            # 来源不存在，或目标的父目录不存在（不是目录），都会得到这类错误；
            # 来源已知、目标父目录也是已知目录时，这个失败无从解释。
            if source in self.names and self._kind_of(os.path.dirname(destination)) == "directory":
                self._problem("已生成对象的缺失探测与轨迹矛盾：", source)
        else:
            self._problem(f"未建立合同的失败调用（{error or '无错误码'}）：{event['name']}:", source)

    def _link(self, source: str, destination: str) -> bool:
        obj = self.names.get(source)
        if obj is None:
            self._problem("硬链接的来源未证明由本轮生成：", source)
        elif self.kinds[obj] != "file":
            self._problem("硬链接的来源不是普通文件：", source)
        elif self._kind_of(os.path.dirname(destination)) != "directory":
            self._problem("硬链接目标的父目录未证明：", destination)
        elif destination in self.names:
            self._problem("硬链接目标已存在却创建成功，与轨迹矛盾：", destination)
        else:
            # 目标名绑定到同一个对象：两个名字、一个对象。
            self.names[destination] = obj
            self._touch(destination)
            self.links[obj] += 1
            self.entries[os.path.dirname(destination)] += 1
            self._settle(destination)
            return True
        return False

    def _rename(self, source: str, destination: str, *, noreplace: bool = False) -> bool:
        if self.root in (source, destination):
            self._problem("不能移动或覆盖私有临时盘的根：", source)
            return False
        obj = self.names.get(source)
        if obj is None:
            self._problem("重命名的来源未证明由本轮生成：", source)
            return False
        if self._kind_of(os.path.dirname(destination)) != "directory":
            self._problem("重命名目标的父目录未证明：", destination)
            return False
        kind = self.kinds[obj]
        victim = self.names.get(destination)
        if victim is not None:
            if noreplace:
                # RENAME_NOREPLACE：目的名字已存在时内核一律返回 EEXIST（同一对象的两个名字、源与目的同名也一样），所以这一
                # 条必须排在下面「同一对象：成功但什么都不改」之前。成功却目的名字已知，说明名字表与真实状态已经脱节，
                # 与上面「硬链接成功却目的已知」同口径判矛盾；矛盾不改名字表，返回 False。
                self._problem("RENAME_NOREPLACE 成功却目标名字已存在，与轨迹矛盾：", destination)
                return False
            if victim == obj:
                # 同一对象的两个名字之间（含源与目标同名）：POSIX 规定成功但什么都不改。
                return False
            if (self.kinds[victim] == "directory") != (kind == "directory"):
                # 目录只能覆盖空目录、非目录（普通文件或符号链接）只能覆盖非目录；POSIX 允许符号链接与普通文件互相覆盖。
                self._problem("重命名覆盖了不同类型的已生成对象：", destination)
                return False
            if kind == "directory" and self.entries.get(destination, 0):
                self._problem("目录重命名覆盖了非空目录：", destination)
                return False
        if kind == "directory" and destination.startswith(source + "/"):
            self._problem("目录不能重命名到自己的子目录之下：", destination)
            return False
        if victim is not None:
            # 被覆盖的名字解绑；它的对象若还有别的名字（硬链接）则继续存在。
            self._unbind(destination)
        self._move(source, destination, kind)
        return True

    def report(self) -> dict[str, Any]:
        """覆盖路径与问题清单。清算在这里完成：仍待补证的名字按原文字报为「首次读取前未证明由本轮生成」。
        只读取状态、不修改，所以可以任意次调用（解析器出文档时一次，重放端复核时一次），结果相同。"""
        problems = set(self.problems)
        problems.update("首次读取前未证明由本轮生成：" + path for path in self.awaiting)
        return {"root": self.root, "paths": sorted(self.covered), "problems": sorted(problems)}


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


_V4_EVENT_KEYS = frozenset({"path", "kind", "name", "succeeded", "flags", "target", "errno", "call", "role", "peer", "epoch"})
_V4_KINDS = frozenset({"read", "exec", "write", "missing", "stat", "dir", "list"})
# WP-R3：v4 事件 flags 里允许出现的 ``AT_*`` 标志（strace 以符号输出的 dirfd 类调用的标志参数）。此前 v4 事件丢掉了它们，
# 所以 lstat（AT_SYMLINK_NOFOLLOW）与 stat 无法区分、unlinkat 的 AT_REMOVEDIR 看不见。这是白名单：解析器遇到名单之外的
# AT_* 标志登记 at-flag-not-decoded 未解析（AT_FDCWD 是描述符取值不是标志，不算），重放端遇到名单之外的标志按格式错误拒绝。
# read_audit.py 里有一份同值常量（两个文件互不导入），两处必须相等，由 test_ci_read_audit_wp_r3 守住。
AT_FLAGS_V4 = frozenset({"AT_SYMLINK_NOFOLLOW", "AT_SYMLINK_FOLLOW", "AT_REMOVEDIR", "AT_EMPTY_PATH", "AT_NO_AUTOMOUNT",
                         "AT_EACCESS", "AT_STATX_SYNC_AS_STAT", "AT_STATX_FORCE_SYNC", "AT_STATX_DONT_SYNC"})
# v4 的 flags：原有的打开标志，加上 renameat2 的三个符号标志与上面的 AT_* 标志（strace 以符号输出）。v3 的校验正则保持原样不动。
_V4_FLAG = re.compile(r"O_[A-Z0-9_]+|RENAME_NOREPLACE|RENAME_EXCHANGE|RENAME_WHITEOUT|" + "|".join(sorted(AT_FLAGS_V4)))
_ERRNO_NAME = re.compile(r"E[A-Z0-9]+")


def _check_event_v4(event: Any, root: str) -> None:
    """v4 输出事件的键集合、字段类型与字段之间的一致性；任何一项不符都按格式错误拒绝，不做宽容解释。"""
    if not isinstance(event, dict) or set(event) != _V4_EVENT_KEYS:
        raise RuntimeContractError("输出归属事件格式错误或越界")
    path, kind, name = event["path"], event["kind"], event["name"]
    succeeded, flags, target = event["succeeded"], event["flags"], event["target"]
    error, call, role, peer, epoch = event["errno"], event["call"], event["role"], event["peer"], event["epoch"]
    valid = (
        all(isinstance(value, str) for value in (path, kind, name)) and bool(name) and kind in _V4_KINDS
        and os.path.normpath(path) == path and under(path, root)
        and isinstance(succeeded, bool)
        and isinstance(flags, list) and all(isinstance(flag, str) and _V4_FLAG.fullmatch(flag) for flag in flags)
        and flags == sorted(set(flags))
        and (target is None or isinstance(target, str))
        # 成功的调用没有错误码；失败的调用必须带形如 EEXIST 的错误码。
        and (error is None or (isinstance(error, str) and _ERRNO_NAME.fullmatch(error) is not None))
        and (error is None) == succeeded
        # 缺失探测只可能来自因 ENOENT／ENOTDIR 失败的调用；不允许把别的失败改标成 missing 去躲开写类失败的错误码判定。
        and (kind != "missing" or (not succeeded and error in {"ENOENT", "ENOTDIR"}))
        and type(call) is int and call >= 1
        and type(epoch) is int and epoch >= 0
        # role／peer 只属于两路径调用：两路径调用的每条事件必须带角色，其余调用两者都为空。
        and (role is not None) == (name in TWO_PATH_NAMES) and (role is None or role in ROLES)
        and (peer is None or (isinstance(peer, str) and peer.startswith("/") and os.path.normpath(peer) == peer))
        and (role is not None or peer is None))
    if not valid:
        raise RuntimeContractError("输出归属事件格式错误或越界")


def _check_call_groups_v4(events: list[dict[str, Any]], root: str) -> None:
    """同一个系统调用产生的事件必须连续、同名、结果一致；两路径调用的两端要能互相印证。

    一次调用最多产生两条事件：openat 一类按请求路径与返回描述符各记一条，rename／link 一类按两端各记一条。
    调用编号严格递增，所以把别的调用的事件拼进来、拆开配对或重排顺序都会被这里发现。
    """
    previous = 0
    for call, group in itertools.groupby(events, key=lambda event: event["call"]):
        items = list(group)
        if call <= previous or len(items) > 2:
            raise RuntimeContractError("输出事件的调用编号乱序、重复或同一调用的事件过多")
        previous = call
        first = items[0]
        if any(item["name"] != first["name"] for item in items):
            raise RuntimeContractError("同一调用的事件不是同名调用")
        if len(items) == 2:
            second = items[1]
            if any(first[key] != second[key] for key in ("kind", "succeeded", "errno", "flags", "target")):
                raise RuntimeContractError("同一调用的两条事件结果不一致")
        if first["name"] not in TWO_PATH_NAMES:
            continue
        if len(items) == 2:
            second = items[1]
            if ((first["role"], second["role"]) != ROLES or first["peer"] != second["path"]
                    or second["peer"] != first["path"]):
                raise RuntimeContractError("两路径调用的配对事件角色或对端不自洽")
        elif first["peer"] is not None and under(first["peer"], root):
            # 另一端在盘内就一定有它自己的事件；只剩一端说明配对被拆开或事件被删。
            raise RuntimeContractError("两路径调用缺少盘内另一端的事件")


def _replay_events_v4(runtime: dict[str, Any], contract: dict[str, Any]) -> tuple[OutputTrackerV4, dict[str, set[str]]]:
    """重放 v4 输出事件：先逐条与按调用分组做结构校验，再按顺序喂给跟踪器并重算变更纪元。

    变更纪元 = 此前使名字空间状态发生变化的事件个数。解析器按名字级版本去重（WP-R9，键里不含纪元），重放端用同一个返回值自己
    重算纪元，每条事件携带的 epoch 必须与重算值一致——任何改状态的事件都不会被去重，被删掉或伪造一条，之后所有事件的纪元都对不上。
    """
    tracker = OutputTrackerV4(contract)
    events = runtime.get("output_events")
    if not isinstance(events, list) or len(events) > MAX_OUTPUT_EVENTS_V4:
        raise RuntimeContractError("输出归属轨迹缺失或超过上限")
    for event in events:
        _check_event_v4(event, tracker.root)
    _check_call_groups_v4(events, tracker.root)
    seen: dict[str, set[str]] = {}
    epoch = 0
    for event in events:
        if event["epoch"] != epoch:
            raise RuntimeContractError("输出事件的变更纪元与重算值不符")
        if tracker.observe_event(event):
            epoch += 1
        seen.setdefault(event["path"], set()).add(event["kind"])
    # WP-R8：清算后的问题（含仍待补证的名字）才是完整结论，所以看 report() 而不是只看 tracker.problems 属性。
    report = tracker.report()
    if report != runtime.get("outputs") or report["problems"]:
        raise RuntimeContractError("生成输出存在先读后写、来源不明或链接越界")
    return tracker, seen


def replay_runtime(document: dict[str, Any], inputs: list[dict[str, Any]], *, historical: bool = False) -> set[str]:
    """重放输出生成顺序、隔离身份和结束凭证；只返回确已证明的路径，不能按目录整段放行。

    按文档的 schema_version 分派：v4 走对象身份跟踪器与纪元／调用分组校验；其余（v3）走下面原样保留的旧合同。
    """
    runtime = document.get("runtime") or {}
    contract = validate_context(runtime.get("context"), inputs, historical=historical)
    completion = runtime.get("completion") or {}
    if any(completion.get(key) is not True for key in
           ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged", "command_finished")):
        raise RuntimeContractError("隔离执行未结束或输入／环境在执行期间变化")
    audit = _read_audit()
    entries = _host_entries(audit, inputs)
    if completion.get("host_entries_sha256") != host_binding_digest(audit, entries):
        raise RuntimeContractError("实际命名空间中的输入快照未绑定执行记录")
    if any(audit.is_host_entry_v3(entry) for entry in entries):
        # v3 快照按访问方式比较：结束凭证里封存的访问类型必须与这份完整轨迹重算的结果一致，否则凭证声称的“未变”
        # 可能是按更宽松的访问方式得出的。纯 v2 记录没有这一项，行为与历史相同。
        try:
            expected_modes = audit.host_path_modes(document, entries)
        except audit.AuditError as error:
            raise RuntimeContractError("宿主访问类型无法由完整轨迹确定：" + str(error)) from error
        if completion.get("host_path_modes") != expected_modes:
            raise RuntimeContractError("宿主访问类型凭证缺失，或与完整轨迹不符")
    if document.get("schema_version") == TRACE_SCHEMA_V4:
        if runtime["context"].get("control_chain") is not None:
            # control 四链只产生并重放 v3 轨迹；v4 文档带 control 专用凭证，说明有人想绕开 control 的专用重放。
            raise RuntimeContractError("v4 轨迹不得携带 control 专用凭证")
        tracker, seen = _replay_events_v4(runtime, contract)
    else:
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


def _host_entries(audit, inputs):
    """输入明细里的宿主快照条目（v2 与 v3 两种合同），保持输入顺序。"""
    return [entry for entry in inputs if entry.get("detail", {}).get("schema_version") in audit.HOST_SCHEMAS]


def host_binding_digest(audit, entries):
    """结束凭证 ``host_entries_sha256`` 的摘要口径。条目全是 v2（含空列表）时与历史完全相同，即 ``digest(entries)``；
    v3 条目只取（名字、摘要、合同版本）视图：承接时新旧条目的内容、目录项、易变元数据可以不同，只要无条件比较集的摘要
    相同就是同一份登记，详情的差异由 ``read_audit.host_inputs_inherit_problems`` 按访问方式比较。"""
    return digest([{"name": entry["name"], "sha256": entry["sha256"], "schema_version": audit.HOST_SCHEMA_V3}
                   if audit.is_host_entry_v3(entry) else entry for entry in entries])


def _host_baseline(audit, request):
    """子命名空间启动前取宿主基线并与登记快照比较，返回基线（before）。

    纯 v2 请求与历史逐字相同：整份相等。含 v3 条目时此刻还没有轨迹，不知道单元会怎样访问，所以只比较“任何访问方式下
    都必须一致”的核心集（类型、符号链接链、祖先目录、权限属主与文件大小）和策略声明的易变字段，不比内容摘要、目录项和
    其它时间字段；取到的 before 作为本次运行的基准，结束时再按访问方式比较。"""
    entries = request["host_entries"]
    if not any(audit.is_host_entry_v3(entry) for entry in entries):
        before = [audit.host_snapshot(path) for path in request["host_paths"]]
        if before != entries:
            raise RuntimeContractError("实际命名空间中的输入与登记快照不同")
        return before
    if request["host_paths"] != [entry.get("detail", {}).get("path") for entry in entries]:
        raise RuntimeContractError("宿主路径与登记快照的路径不一致")
    before = []
    for entry in entries:
        try:
            live = audit.host_snapshot_like(entry)
            if audit.is_host_entry_v3(entry):
                ok, reason = audit.host_snapshot_matches(audit.check_host_entry_v3(entry), live["detail"]["snapshot"], None,
                                                         audit.entry_declared_fields(entry))
            else:
                ok, reason = live == entry, "整份快照变了"
        except (OSError, ValueError, TypeError, audit.AuditError) as error:
            raise RuntimeContractError(f"实际命名空间中的输入快照失败：{entry.get('name')}（{error}）") from error
        if not ok:
            raise RuntimeContractError(f"实际命名空间中的输入与登记快照不同：{entry.get('name')}（{reason}）")
        before.append(live)
    return before


def _host_final(audit, request):
    """命令结束后重取宿主快照。纯 v2 请求与历史相同（取快照出错照旧向上抛）；含 v3 条目时出错记成失败标记，让结束比较
    判“已变”，而不是丢掉已经写出的轨迹。"""
    entries = request["host_entries"]
    if not any(audit.is_host_entry_v3(entry) for entry in entries):
        return [audit.host_snapshot(path) for path in request["host_paths"]]
    after = []
    for entry in entries:
        try:
            after.append(audit.host_snapshot_like(entry))
        except (OSError, ValueError, TypeError, audit.AuditError) as error:
            after.append({"snapshot_error": str(error)})
    return after


def _host_completion(audit, request, document):
    """结束凭证里与宿主快照有关的字段。``host_entries_sha256`` 绑定的是登记的快照（纯 v2 时启动已核对 before 与它整份相同，
    摘要与历史的 ``digest(before)`` 逐字相同）；含 v3 条目时另带 ``host_path_modes``——本次结束比较所用的访问类型，消费
    重放时必须与完整轨迹重算的结果一致。轨迹不完整就不写访问类型，重放据此拒绝。"""
    fields = {"host_entries_sha256": host_binding_digest(audit, request["host_entries"])}
    if any(audit.is_host_entry_v3(entry) for entry in request["host_entries"]):
        try:
            fields["host_path_modes"] = audit.host_path_modes(document, request["host_entries"])
        except audit.AuditError:
            pass
    return fields


def _host_unchanged(audit, request, before, after, document):
    """运行结束时的宿主输入核对，返回（是否未变，原因）。纯 v2 请求：``before == after``，与历史相同。

    含 v3 条目时读本次的轨迹文档（访问方式只能来自通过完整性检查的轨迹）：

    * 轨迹完整：对每个 v3 路径按访问方式同时比较「登记↔基线」与「基线↔结束」两对，任一不一致即“已变”；
    * 拿不到完整轨迹（文档缺失或不完整）：只比较「基线↔结束」，口径是最严格的整份相等（连时间字段都算），**不再比较**
      「登记↔基线」。这比访问方式口径在「基线↔结束」一对上更严，但少了「登记↔基线」一对，仍然正确，原因有二：
      1. 「登记↔基线」里不依赖访问方式的部分（类型、符号链接链、祖先目录、权限属主、文件大小、策略声明的易变字段）在子命名
         空间启动前已由 ``_host_baseline`` 比较过，不一致当场拒绝启动被测命令；只有依赖访问方式的内容摘要／目录项补比才
         需要轨迹，没有完整轨迹本来就做不了；
      2. 轨迹不完整的单元本身就判失败（严格读集审计报“完整轨迹缺失、未结束或含未解析调用，不可承接”），结束凭证也不会写入宿主
         访问类型（``_host_completion`` 算不出来就不写），重放时因“宿主访问类型无法由完整轨迹确定”被拒绝——所以这条路径
         得出的“未变”结论永远不可能被拿去承接，它只是单元失败时的现场诊断。"""
    entries = request["host_entries"]
    if not any(audit.is_host_entry_v3(entry) for entry in entries):
        return before == after, ""
    modes = None
    if document is not None:
        try:
            modes = audit.trace_access_modes(document)
        except audit.AuditError:
            modes = None
    for entry, first, last in zip(entries, before, after):
        if "snapshot_error" in last:
            return False, f"{entry.get('name')}：结束时取快照失败（{last['snapshot_error']}）"
        if not audit.is_host_entry_v3(entry):
            if first != last:
                return False, f"{entry.get('name')}：整份快照变了"
            continue
        planned, start, end = (item["detail"]["snapshot"] for item in (entry, first, last))
        path = entry["detail"]["path"]
        fields = audit.entry_declared_fields(entry)
        if modes is None:
            checks = [audit.host_snapshot_identical(start, end)]
        else:
            kinds = modes.get(path, ())
            checks = [audit.host_snapshot_matches(planned, start, kinds, fields), audit.host_snapshot_matches(start, end, kinds, fields)]
        for ok, reason in checks:
            if not ok:
                return False, f"{entry.get('name')}：{reason}"
    return True, ""


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
    before = _host_baseline(audit, request)
    # 轨迹合同版本由受管请求决定（请求带摘要，被测命令改不了）：请求里没有该键就是历史 v3，调用方式与改动前完全一致；
    # 只有 prepare 明确写入 v4 时才把开关交给过滤器。
    trace_contract = request.get("trace_contract", TRACE_CONTRACT_V3)
    if trace_contract not in TRACE_CONTRACTS:
        raise RuntimeContractError("启动请求的轨迹合同版本未知")
    options = {} if trace_contract == TRACE_CONTRACT_V3 else {"trace_contract": trace_contract}
    argv = audit.strace_argv(request["argv"], output=trace_path, roots=["/"], strict=True, runtime_context=context_path, **options)
    result = subprocess.run(argv, cwd=request["cwd"], env={**os.environ, "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root)},
                            stdin=subprocess.DEVNULL, check=False)
    after = _host_final(audit, request)
    # 宿主快照 v3 按访问方式比较，访问方式只能来自刚写出的轨迹，所以先读轨迹再下“未变”的结论；纯 v2 请求不提前读，
    # 与历史顺序相同。
    host_v3 = any(audit.is_host_entry_v3(host_entry) for host_entry in request["host_entries"])
    written = json.loads(trace_path.read_text()) if host_v3 and trace_path.is_file() else None
    unchanged, drift = _host_unchanged(audit, request, before, after, written)
    receipt = {"context": context, "host_inputs_unchanged": unchanged, "environment_unchanged": entry == contract_entry(contract),
               "namespace_unchanged": namespace == os.readlink("/proc/self/ns/mnt"), "returncode": result.returncode,
               "request_sha256": expected_digest, "host_entries": before}
    if host_v3:
        receipt["host_drift"] = drift
    _write(trace_path.with_name(trace_path.name + ".runtime.json"), receipt)
    if trace_path.is_file():
        document = written if written is not None else json.loads(trace_path.read_text())
        document.setdefault("runtime", {})["completion"] = {
            key: receipt[key] for key in ("host_inputs_unchanged", "environment_unchanged", "namespace_unchanged")}
        document["runtime"]["completion"].update(command_finished=True, returncode=result.returncode, request_sha256=expected_digest,
                                                **_host_completion(audit, request, document))
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
            inputs: list[dict[str, Any]], test_spec: dict[str, Any] | None = None,
            trace_contract: str = TRACE_CONTRACT_V4) -> list[str]:
    """启动前建立专用空挂载点，写受管请求；实际文件快照在子命名空间再次核对。

    普通受管单元（执行器调用本函数）默认产出 v4 轨迹：请求里写入 ``trace_contract``，``run_child`` 据此让过滤器
    产出 v4 文档。显式传 ``v3`` 时请求与改动前逐字节相同（不带该键）。control 四链有自己的 prepare／run_child，
    不经过这里的 v4 启用点，仍然生产并重放 v3 轨迹。
    """
    if trace_contract not in TRACE_CONTRACTS:
        raise RuntimeContractError("未知的轨迹合同版本")
    if not sys.platform.startswith("linux") or shutil.which("unshare") is None:
        raise RuntimeContractError("隔离合同只在具备 unshare 的 Linux 上可用")
    contract = validate_contract(contract)
    root = Path(contract["temporary_root"])
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise RuntimeContractError("临时根不能含符号链接")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(root.iterdir()) or stat.S_IMODE(root.stat().st_mode) != 0o700 or root.stat().st_uid != os.geteuid():
        raise RuntimeContractError("宿主挂载点必须为空、归运行账号所有且权限为 0700")
    entries = _host_entries(_read_audit(), inputs)
    request = {"contract_entry": contract_entry(contract), "parent_namespace": os.readlink("/proc/self/ns/mnt"),
               "output": str(output), "argv": argv, "cwd": cwd, "host_entries": entries,
               "host_paths": [entry["detail"]["path"] for entry in entries]}
    if trace_contract != TRACE_CONTRACT_V3:
        request["trace_contract"] = trace_contract
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
