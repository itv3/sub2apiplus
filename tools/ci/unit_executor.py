#!/usr/bin/env python3
"""统一调度执行器（E2-01）：把采集工具测试拆成互相隔离的单元，在整机处理器与内存额度内并行执行，一次报全。

与 ``make test-capture-tools`` 原来的单进程 discover 同一加载语义：以 ``tools/official_client_capture/tests`` 为起点、
``test_*.py`` 为模式 discover，测试 ID 形如 ``test_x.Class.method``；子进程同样把起点目录放在 ``sys.path`` 首位，
按 ID 逐个加载。不同之处只在执行方式：

* **单元**：默认一个测试模块一个单元；调度配置里登记了拆块的重模块按测试方法拆成若干块（按逐测试耗时用最长
  优先法均衡）；登记为独占的测试（模块、类或单个方法）单独拆成「独占单元」。
* **隔离**：每个单元一个子进程，自成会话与进程组（``posix_spawn`` 的 ``setsid``），日志单独一份；一个单元被信号
  终止只影响它自己。单元结束后扫描它会话里的残留子孙进程，宽限期后终止并记录。
* **额度**：每个单元声明核数与内存，调度器保证在跑单元的额度之和不超过整机（核数取 ``os.cpu_count()``，内存取
  可用内存的八成）；单元内部的并行度经环境变量 ``UNIT_EXECUTOR_CORES``、``GOMAXPROCS`` 下发。``--parallel``
  只限制同时在跑的单元个数，设成多大都不会突破整机额度。
* **顺序**：并行段（最长优先，放不下的跳过、先跑放得下的）→ 独占段（逐个单跑，期间不派发别的）→ 诊断重跑
  （并行段与独占段里失败或被信号终止的单元逐个单独重跑一次）。诊断结果单独标为诊断执行，**不改结论**。
* **全集核对**：各正式单元上报的测试 ID 集合，必须与 discover 出的全集逐个相等（不多、不少、不重复）；单元崩溃、
  超时、没写结果文件都算失败。
* **一次报全**：全部单元跑完再汇总；标准错误最后两段与 unittest 相同（``Ran N tests in …``、``OK``／``FAILED (…)``），
  P0 收据照原样解析。
* **调度策略版本**：调度配置（并行度、额度、拆块、独占名单）与权重、逐测试耗时表合起来算一个摘要，写进每条正式
  单元结果与汇总。
* **采集申请整机资源**：采集批次开始前执行 ``acquire``，调度器停止派发新单元，等在跑单元结束、残留进程清理完再
  批准；采集结束 ``release``，调度器接着派发。同一台机器同一时间只运行一个调度器（状态目录里的 flock）。
* **字节码共享层**（E2-02）：环境里已有可用的 ``PYTHONPYCACHEPREFIX``（ARM64 门禁已用驱动 ``bytecode_cache.py``
  预编译）就直接沿用；没有时执行器先把标准库与原树预编译到记录目录下的 ``pycache-shared``（标准库按时间戳、原树按
  内容摘要），独占整机、并发数取整机核数，耗时计入总时长，所有单元只读使用。预编译失败时其余单元照跑（回落为各自
  从源码编译），结论判失败。
* **身份记忆化**（E2-02）：环境里没有 ``CODEX_UPGRADE_IDENTITY_MEMO`` 时设为记录目录下的 ``identity-memo``，全部单元
  共用——同一棵受管树的身份五摘要与评估器四项只算一次，键是整树逐文件摘要，测试改了副本树自然重算。
  ``--shared-caches off`` 时两样都不准备，单元按原环境运行（诊断用）。
* **执行记录与承接**（E3-01，记录格式、输入与判定见同目录 ``unit_records.py``）：每个单元每次执行（正式、诊断）回收
  时写一条不可变记录。``run-gates`` 给了记录库（``--record-store``）时记录同时入库；模式为全集通过
  （``--mode full-set-pass``）时，先在记录库里给每个单元找可承接的记录，只执行找不到的，承接的单元按原记录参加
  全集核对与门禁聚合。每次 ``run-gates`` 运行写一份清单（``unit-manifest.json``：逐单元本次执行还是承接、依据或
  不承接的原因）并自检，自检不通过结论判失败。``run``／``run-commands``（``make test-capture-tools``、
  ``make check-egress-spec``、pre-A3 单独签发）一律全部执行、不入库。

子命令：``plan``（列出单元与策略摘要，不执行）、``run``（执行并汇总；退出码 0 全部通过、1 有失败、2 用法或配置错误）、
``run-commands``（E2-03：按命令单元清单执行，每个单元一条命令）、``run-gates``（E2-04：按门禁清单执行——一个测试组与
若干命令单元同一次运行、同一套调度，结论按门禁项聚合）、``run-unit``（内部：在当前进程跑给定测试 ID、写结果文件）、
``acquire``／``release``（采集批次申请与归还整机资源）。
**不嵌套**：调度单元里再用外层调度器的状态目录（环境变量 ``UNIT_EXECUTOR_PARENT_STATE_DIR``）启动调度器会卡在同一把
锁上，直接报错；另给状态目录的（如测试）照常运行。
运行时与原 make 目标一样设置 ``CLAUDE_AST_TYPESCRIPT_MODULE``，从仓库根目录执行；调度进程与单元子进程一律不写字节码。
换了起点或模式（只跑一部分模块）时，调度配置里本次集合之外的登记项不参与规划；全量运行时登记项必须全部命中。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

DEFAULT_START = Path("tools/official_client_capture/tests")
DEFAULT_PATTERN = "test_*.py"
# 调度配置随执行器放在同一目录（命令单元可能从数据根等任意目录启动执行器）。
DEFAULT_CONFIG = Path(__file__).resolve().parent / "unit_executor.json"
DEFAULT_WEIGHTS = Path("tools/ci/capture_test_weights.json")
DEFAULT_DURATIONS = Path("tools/ci/capture_test_durations.json")


def _default_bytecode_helper() -> Path:
    """字节码预编译工具：执行器随 ARM64 驱动安装时与它同在 ``driver/`` 下，在仓库里则在 ``tools/arm64_capture_driver/driver/``。"""

    here = Path(__file__).resolve().parent
    beside = here / "bytecode_cache.py"
    return beside if beside.is_file() else here.parent / "arm64_capture_driver" / "driver" / "bytecode_cache.py"


DEFAULT_BYTECODE_HELPER = _default_bytecode_helper()
DEFAULT_BYTECODE_SOURCES = (Path("tools"),)
# 受管工具身份的跨进程记忆化目录（与 codex_upgrade_tool_identity_policy.IDENTITY_MEMO_ENV 同名；执行器不导入受管模块）。
IDENTITY_MEMO_ENV = "CODEX_UPGRADE_IDENTITY_MEMO"
CONFIG_SCHEMA = "unit-executor-config/v1"
WEIGHTS_SCHEMA = "capture-test-shard-weights/v1"
DURATIONS_SCHEMA = "capture-test-durations/v1"
UNIT_RESULT_SCHEMA = "unit-executor-unit-result/v1"
SUMMARY_SCHEMA = "unit-executor-summary/v1"
RESERVATION_SCHEMA = "unit-executor-reservation/v1"
FAILED_IMPORT_PREFIX = "unittest.loader._FailedTest."
POLL_SECONDS = 0.2


class ExecutorError(RuntimeError):
    """配置、闭合或用法错误（退出码 2）。"""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _records_module() -> Any:
    """同目录的 unit_records.py（仓库 tools/ci 与驱动副本里都和本文件同目录）：按路径加载，不受当前目录与 PYTHONPATH 影响。"""

    name = "unit_records_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "unit_records.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 unit_records.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


# ---------------------------------------------------------------------------
# 调度配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quota:
    """单元额度：核数可以是小数（按实测 CPU／墙钟比，等待为主的单元申请半核），内存以 MB 计。"""

    cores: float
    memory_mb: int


@dataclass(frozen=True)
class ExecutorConfig:
    """调度配置：默认并行度、默认额度、按模块的额度、拆块与独占名单、单元超时、残留进程宽限期。"""

    default_parallelism: int
    default_quota: Quota
    quotas: dict[str, Quota]
    splits: dict[str, int]
    exclusive: tuple[tuple[str, str], ...]
    unit_timeout_seconds: float
    orphan_grace_seconds: float
    raw: dict[str, Any]


def load_config(path: Path) -> ExecutorConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != CONFIG_SCHEMA:
        raise ExecutorError(f"调度配置格式非法：{path}")

    def quota(value: Any, label: str) -> Quota:
        if not isinstance(value, dict) or set(value) - {"cores", "memory_mb"}:
            raise ExecutorError(f"额度非法：{label}")
        cores = value.get("cores", 1)
        memory = value.get("memory_mb", 768)
        if not isinstance(cores, (int, float)) or isinstance(cores, bool) or cores < 0.1:
            raise ExecutorError(f"额度核数非法：{label}")
        if not isinstance(memory, int) or isinstance(memory, bool) or memory < 64:
            raise ExecutorError(f"额度内存非法：{label}")
        return Quota(float(cores), memory)

    parallelism = payload.get("default_parallelism")
    if not isinstance(parallelism, int) or isinstance(parallelism, bool) or parallelism < 1:
        raise ExecutorError("default_parallelism 必须是正整数")
    splits: dict[str, int] = {}
    for module, value in (payload.get("splits") or {}).items():
        if not isinstance(value, dict) or set(value) != {"chunks", "reason"} or not isinstance(value["chunks"], int) or value["chunks"] < 2:
            raise ExecutorError(f"拆块配置非法：{module}")
        if not isinstance(value["reason"], str) or not value["reason"].strip():
            raise ExecutorError(f"拆块配置缺少原因：{module}")
        splits[module] = value["chunks"]
    exclusive: list[tuple[str, str]] = []
    for item in payload.get("exclusive") or []:
        if not isinstance(item, dict) or set(item) != {"tests", "reason"}:
            raise ExecutorError(f"独占名单条目非法：{item!r}")
        # tests 可以是一个前缀（模块、类或方法），也可以是同一根因下的一组前缀。
        prefixes = [item["tests"]] if isinstance(item["tests"], str) else item["tests"]
        if not isinstance(prefixes, list) or not prefixes or not all(isinstance(p, str) and p.startswith("test_") for p in prefixes):
            raise ExecutorError(f"独占名单的测试前缀非法：{item!r}")
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ExecutorError(f"独占名单条目必须写明根因：{item['tests']}")
        exclusive.extend((prefix, item["reason"]) for prefix in prefixes)
    timeout = payload.get("unit_timeout_seconds", 3600)
    grace = payload.get("orphan_grace_seconds", 10)
    if not isinstance(timeout, (int, float)) or timeout <= 0 or not isinstance(grace, (int, float)) or grace < 0:
        raise ExecutorError("单元超时或残留进程宽限期非法")
    return ExecutorConfig(
        default_parallelism=parallelism,
        default_quota=quota(payload.get("default_quota") or {}, "default_quota"),
        quotas={module: quota(value, module) for module, value in (payload.get("quotas") or {}).items()},
        splits=splits,
        exclusive=tuple(exclusive),
        unit_timeout_seconds=float(timeout),
        orphan_grace_seconds=float(grace),
        raw=payload,
    )


def load_weights(path: Path) -> dict[str, float]:
    if not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != WEIGHTS_SCHEMA or not isinstance(payload.get("weights"), dict):
        raise ExecutorError(f"权重表格式非法：{path}")
    return {str(module): float(value) for module, value in payload["weights"].items()}


def load_durations(path: Path) -> dict[str, float]:
    if not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != DURATIONS_SCHEMA or not isinstance(payload.get("durations"), dict):
        raise ExecutorError(f"逐测试耗时表格式非法：{path}")
    return {str(test_id): float(value) for test_id, value in payload["durations"].items()}


def policy_digest(config: ExecutorConfig, weights: dict[str, float], durations: dict[str, float], parallelism: int) -> str:
    """调度策略版本：配置、权重、逐测试耗时与本次并行度的摘要（E3-01 承接时与输入摘要一起比对）。"""

    return _sha256({"config": config.raw, "weights": weights, "durations": durations, "parallelism": parallelism})


def gates_policy_digest(config: ExecutorConfig, weights: dict[str, float], durations: dict[str, float], parallelism: int,
                        scheduling: Any) -> str:
    """门禁清单模式的调度策略版本（E3-01）：调度配置（并行度、额度、拆块、独占名单、超时）、权重、逐测试耗时、本次并行度，
    加门禁清单给出的命令单元额度表（``scheduling``，与组合无关）。每次运行新建的认证根路径、组合名、各单元的命令不进
    策略版本——命令随单元规格比对；原来把整份门禁清单算进来，带 pre-A3 时每次都不同，承接永远不会命中。"""

    return _sha256({"config": config.raw, "weights": weights, "durations": durations, "parallelism": parallelism,
                    "scheduling": scheduling if scheduling is not None else {}})


# ---------------------------------------------------------------------------
# 发现与单元规划
# ---------------------------------------------------------------------------


def _import_path_like_unittest_main() -> None:
    """与 ``python3 -m unittest`` 相同：当前目录（仓库根）在导入路径上。

    本脚本以文件方式运行时 ``sys.path[0]`` 是 ``tools/ci``；原单进程 discover 里，部分测试模块要靠别的模块先把仓库根
    加进导入路径才能 ``import tools.…``。拆成独立进程后每个单元都要自己补齐，否则单独运行时会导入失败。
    """

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def _iterate(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iterate(item)
        else:
            yield item


PACKAGE_PREFIX = "tools.official_client_capture.tests."


def module_of(test_id: str) -> str:
    """测试 ID 所属的模块分组。

    discover 以起点目录为顶层，测试 ID 形如 ``test_x.Class.method``。某个测试模块在模块级引用了另一个模块的测试类
    （如 ``_RECOVERY_TESTS = recovery_tests.EvaluationRecoveryIntegrationTests``）时，discover 会在引用它的模块里把
    这些测试按完整包名（``tools.official_client_capture.tests.test_y.Class.method``）再跑一遍——原单进程 discover
    也是如此。这类 ID 单独归到 ``test_y+pkg`` 分组，照原语义执行，不与 ``test_y`` 自己的测试混在一个单元里。
    """

    if test_id.startswith(FAILED_IMPORT_PREFIX):
        return test_id[len(FAILED_IMPORT_PREFIX):].split(".")[0]
    if test_id.startswith(PACKAGE_PREFIX):
        return test_id[len(PACKAGE_PREFIX):].split(".")[0] + "+pkg"
    return test_id.split(".")[0]


def discover_test_ids(start: Path, pattern: str) -> dict[str, list[str]]:
    """与 ``python3 -m unittest discover -s <start> -p <pattern>`` 同一语义，返回 模块 → 测试 ID 列表。"""

    if not Path(start).is_dir():
        raise ExecutorError(f"测试起点目录不存在：{start}")
    _import_path_like_unittest_main()
    suite = unittest.defaultTestLoader.discover(start_dir=str(start), pattern=pattern)
    grouped: dict[str, list[str]] = {}
    for case in _iterate(suite):
        test_id = case.id()
        grouped.setdefault(module_of(test_id), []).append(test_id)
    if not grouped:
        raise ExecutorError("discover 没有发现任何用例")
    return {module: sorted(ids) for module, ids in sorted(grouped.items())}


@dataclass(frozen=True)
class Unit:
    unit_id: str
    module: str
    test_ids: tuple[str, ...]
    quota: Quota
    exclusive: bool
    weight: float
    # 命令单元（E2-03 起：pre-A3 场景，之后的后端测试、lint 等）：直接运行这条命令、不经 unittest，工作目录、额外
    # 环境变量与超时随单元给出；结果文件由命令自己写、调用方核对，执行器只认退出码、信号与超时。测试单元这几项都空。
    command: tuple[str, ...] = ()
    cwd: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    timeout_seconds: float | None = None
    # 测试单元的启动前缀（E2-04 门禁清单的测试组）：例如在私有挂载命名空间里遮住生产别名再 exec 单元进程；命令单元
    # 需要时直接把前缀写进自己的 argv。
    launcher: tuple[str, ...] = ()


COMMANDS_SCHEMA = "unit-executor-commands/v1"
COMMANDS_SUMMARY_SCHEMA = "unit-executor-commands-summary/v1"
# inputs／inheritable／not_inheritable_reason 是门禁清单给 E3-01 承接用的声明（输入范围、是否可承接与原因），不影响执行。
_COMMAND_FIELDS = {"unit_id", "argv", "cwd", "env", "cores", "memory_mb", "exclusive", "timeout_seconds", "weight",
                   "inputs", "inheritable", "not_inheritable_reason"}


def _string_env(value: Any, label: str) -> dict[str, str]:
    env = value or {}
    if not isinstance(env, dict) or not all(isinstance(key, str) and key and isinstance(item, str) for key, item in env.items()):
        raise ExecutorError(f"{label}的环境变量非法")
    return env


def _command_unit(item: Any, *, machine_cores: int, seen: set[str]) -> Unit:
    """解析一个命令单元（命令单元清单与门禁清单共用）：单元 ID 唯一、不含 ``/``（用作记录文件名），工作目录必须是
    绝对路径；额度、独占、超时与预计秒数（并行段按它从长到短派发）都可选。"""

    if not isinstance(item, dict) or set(item) - _COMMAND_FIELDS or "unit_id" not in item or "argv" not in item:
        raise ExecutorError(f"命令单元字段非法：{item!r}")
    unit_id = item["unit_id"]
    if not isinstance(unit_id, str) or not unit_id or "/" in unit_id or unit_id in seen:
        raise ExecutorError(f"命令单元 ID 非法或重复：{unit_id!r}")
    seen.add(unit_id)
    argv = item["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        raise ExecutorError(f"命令单元的命令非法：{unit_id}")
    cwd = item.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd)):
        raise ExecutorError(f"命令单元的工作目录必须是绝对路径：{unit_id}")
    env = _string_env(item.get("env"), f"命令单元 {unit_id} ")
    cores = item.get("cores", 1)
    memory = item.get("memory_mb", 768)
    if not isinstance(cores, (int, float)) or isinstance(cores, bool) or cores < 0.1:
        raise ExecutorError(f"命令单元额度核数非法：{unit_id}")
    if not isinstance(memory, int) or isinstance(memory, bool) or memory < 64:
        raise ExecutorError(f"命令单元额度内存非法：{unit_id}")
    exclusive = item.get("exclusive", False)
    timeout = item.get("timeout_seconds")
    weight = item.get("weight", 0)
    if not isinstance(exclusive, bool):
        raise ExecutorError(f"命令单元的独占标记非法：{unit_id}")
    if not isinstance(item.get("inheritable", True), bool) or not isinstance(item.get("not_inheritable_reason", ""), str) \
            or not isinstance(item.get("inputs", {}), dict):
        raise ExecutorError(f"命令单元的承接声明非法：{unit_id}")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
        raise ExecutorError(f"命令单元的超时非法：{unit_id}")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight < 0:
        raise ExecutorError(f"命令单元的预计秒数非法：{unit_id}")
    return Unit(
        unit_id=unit_id, module=unit_id, test_ids=(), quota=Quota(min(float(cores), float(max(1, machine_cores))), memory),
        exclusive=exclusive, weight=float(weight), command=tuple(argv), cwd=cwd, env=tuple(sorted(env.items())),
        timeout_seconds=float(timeout) if timeout is not None else None,
    )


def load_command_manifest(path: Path, *, machine_cores: int) -> tuple[list[Unit], dict[str, Any]]:
    """读命令单元清单（``unit-executor-commands/v1``）：每个单元一条命令、工作目录、额外环境变量、额度、独占、超时与
    预计秒数。"""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != COMMANDS_SCHEMA:
        raise ExecutorError(f"命令单元清单格式非法：{path}")
    items = payload.get("units")
    if not isinstance(items, list) or not items:
        raise ExecutorError(f"命令单元清单没有单元：{path}")
    seen: set[str] = set()
    return [_command_unit(item, machine_cores=machine_cores, seen=seen) for item in items], payload


GATES_SCHEMA = "unit-executor-gates/v1"
GATES_SUMMARY_SCHEMA = "unit-executor-gates-summary/v1"
_TEST_GROUP_FIELDS = {"group_id", "start", "pattern", "env", "launcher"}
_GATE_FIELDS = {"gate_id", "units", "test_groups", "not_executed"}


@dataclass(frozen=True)
class DiscoverGroup:
    """门禁清单里的测试组：按起点与模式 discover，照 ``run`` 的规则拆单元（额度、拆块、独占名单都沿用调度配置）。"""

    group_id: str
    start: Path
    pattern: str
    env: tuple[tuple[str, str], ...]
    launcher: tuple[str, ...]


@dataclass(frozen=True)
class Gate:
    """门禁项：由若干命令单元与测试组组成，全部正式执行且通过（测试组还要全集核对通过）才算通过。``not_executed``
    是清单里写明不在本平台执行的项（如 macOS 专用的部署脚本测试），原样写进汇总，不执行、不算失败。"""

    gate_id: str
    units: tuple[str, ...]
    test_groups: tuple[str, ...]
    not_executed: tuple[dict[str, Any], ...]


def load_gates_manifest(path: Path, *, machine_cores: int) -> tuple[list[DiscoverGroup], list[Unit], list[Gate], dict[str, Any]]:
    """读门禁清单（``unit-executor-gates/v1``）：至多一个测试组、若干命令单元、若干门禁项。

    闭合要求：门禁项引用的单元与测试组都必须在清单里；清单里的每个单元、每个测试组至少属于一个门禁项（没有游离
    单元）；一个单元可以同时属于几个门禁项（如 ``test-official-client-control`` 既单列为门禁项，又是
    ``check-egress-spec`` 的子检查），只执行一次。测试组至多一个：测试单元 ID 沿用 ``run`` 的模块名，调度配置里的
    额度、拆块与独占名单按模块名登记。
    """

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != GATES_SCHEMA:
        raise ExecutorError(f"门禁清单格式非法：{path}")
    raw_groups = payload.get("test_groups") or []
    if not isinstance(raw_groups, list) or len(raw_groups) > 1:
        raise ExecutorError("门禁清单的测试组至多一个")
    groups: list[DiscoverGroup] = []
    for item in raw_groups:
        if not isinstance(item, dict) or set(item) - _TEST_GROUP_FIELDS or not {"group_id", "start", "pattern"} <= set(item):
            raise ExecutorError(f"测试组字段非法：{item!r}")
        group_id = item["group_id"]
        if not isinstance(group_id, str) or not group_id or "/" in group_id:
            raise ExecutorError(f"测试组 ID 非法：{group_id!r}")
        if not isinstance(item["start"], str) or not item["start"] or os.path.isabs(item["start"]):
            raise ExecutorError(f"测试组起点必须是相对执行器工作目录的路径：{group_id}")
        if not isinstance(item["pattern"], str) or not item["pattern"]:
            raise ExecutorError(f"测试组模式非法：{group_id}")
        launcher = item.get("launcher") or []
        if not isinstance(launcher, list) or not all(isinstance(part, str) and part for part in launcher):
            raise ExecutorError(f"测试组的启动前缀非法：{group_id}")
        groups.append(DiscoverGroup(group_id, Path(item["start"]), item["pattern"],
                                tuple(sorted(_string_env(item.get("env"), f"测试组 {group_id} ").items())), tuple(launcher)))
    items = payload.get("units") or []
    if not isinstance(items, list):
        raise ExecutorError("门禁清单的 units 必须是列表")
    seen: set[str] = set()
    units = [_command_unit(item, machine_cores=machine_cores, seen=seen) for item in items]
    raw_gates = payload.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise ExecutorError("门禁清单没有门禁项")
    gates: list[Gate] = []
    gate_ids: set[str] = set()
    group_ids = {group.group_id for group in groups}
    referenced_units: set[str] = set()
    referenced_groups: set[str] = set()
    for item in raw_gates:
        if not isinstance(item, dict) or set(item) - _GATE_FIELDS or "gate_id" not in item:
            raise ExecutorError(f"门禁项字段非法：{item!r}")
        gate_id = item["gate_id"]
        if not isinstance(gate_id, str) or not gate_id or "/" in gate_id or gate_id in gate_ids:
            raise ExecutorError(f"门禁项 ID 非法或重复：{gate_id!r}")
        gate_ids.add(gate_id)
        member_units = item.get("units") or []
        member_groups = item.get("test_groups") or []
        not_executed = item.get("not_executed") or []
        if not isinstance(member_units, list) or not all(isinstance(u, str) for u in member_units) or len(set(member_units)) != len(member_units):
            raise ExecutorError(f"门禁项 {gate_id} 的单元列表非法")
        if not isinstance(member_groups, list) or not all(isinstance(g, str) for g in member_groups):
            raise ExecutorError(f"门禁项 {gate_id} 的测试组列表非法")
        if not isinstance(not_executed, list) or not all(
                isinstance(entry, dict) and isinstance(entry.get("reason"), str) and entry["reason"].strip() for entry in not_executed):
            raise ExecutorError(f"门禁项 {gate_id} 的不执行项必须逐条写明原因")
        if not member_units and not member_groups:
            raise ExecutorError(f"门禁项 {gate_id} 没有任何单元")
        unknown = sorted(set(member_units) - seen) + sorted(set(member_groups) - group_ids)
        if unknown:
            raise ExecutorError(f"门禁项 {gate_id} 引用了清单里没有的单元或测试组：{unknown}")
        referenced_units.update(member_units)
        referenced_groups.update(member_groups)
        gates.append(Gate(gate_id, tuple(member_units), tuple(member_groups), tuple(dict(entry) for entry in not_executed)))
    orphans = sorted(seen - referenced_units) + sorted(group_ids - referenced_groups)
    if orphans:
        raise ExecutorError(f"清单里有不属于任何门禁项的单元或测试组：{orphans}")
    return groups, units, gates, payload


def _matches(test_id: str, prefix: str) -> bool:
    return test_id == prefix or test_id.startswith(prefix + ".")


def plan_units(
    grouped: dict[str, list[str]],
    config: ExecutorConfig,
    weights: dict[str, float],
    durations: dict[str, float],
    *,
    machine_cores: int,
    full_set: bool = True,
) -> list[Unit]:
    """把全集拆成单元：独占测试单独成单元；登记拆块的模块按最长优先法均衡拆块；其余一个模块一个单元。

    ``full_set`` 为真（默认起点与模式的全量运行）时同时校验调度配置：登记了拆块或独占的模块必须存在，独占名单的
    每个条目至少命中一个测试——测试改名或删除后名单不会静默失效。只跑一部分模块（换了起点或模式，如只重跑失败项）
    时，本次集合之外的登记项不参与规划，也不报错。
    """

    if full_set:
        stale = sorted({prefix.split(".")[0] for prefix, _reason in config.exclusive} - set(grouped))
        stale += sorted(set(config.splits) - set(grouped))
        if stale:
            raise ExecutorError("调度配置登记了不存在的模块：" + "、".join(sorted(set(stale))))
        every_id = [t for ids in grouped.values() for t in ids]
        unmatched = sorted({prefix for prefix, _reason in config.exclusive if not any(_matches(t, prefix) for t in every_id)})
        if unmatched:
            raise ExecutorError("独占名单里的条目没有命中任何测试（测试改名或删除后要同步名单）：" + "、".join(unmatched))
    units: list[Unit] = []
    for module, test_ids in grouped.items():
        quota = config.quotas.get(module, config.default_quota)
        quota = Quota(min(quota.cores, float(max(1, machine_cores))), quota.memory_mb)
        module_weight = weights.get(module, 1.0)
        per_test = module_weight / max(1, len(test_ids))

        def seconds(test_id: str) -> float:
            return durations.get(test_id, per_test)

        exclusive_ids = [t for t in test_ids if any(_matches(t, prefix) for prefix, _reason in config.exclusive)]
        normal_ids = [t for t in test_ids if t not in set(exclusive_ids)]
        if exclusive_ids:
            units.append(Unit(f"{module}!exclusive", module, tuple(exclusive_ids), quota, True, sum(seconds(t) for t in exclusive_ids)))
        chunks = min(config.splits.get(module, 1), len(normal_ids)) if normal_ids else 0
        if chunks <= 1:
            if normal_ids:
                units.append(Unit(module, module, tuple(normal_ids), quota, False, sum(seconds(t) for t in normal_ids)))
            continue
        bins: list[list[str]] = [[] for _ in range(chunks)]
        totals = [0.0] * chunks
        for test_id in sorted(normal_ids, key=lambda t: (-seconds(t), t)):
            index = min(range(chunks), key=lambda i: (totals[i], i))
            bins[index].append(test_id)
            totals[index] += seconds(test_id)
        for index, members in enumerate(bins, 1):
            units.append(Unit(f"{module}#{index}", module, tuple(sorted(members)), quota, False, totals[index - 1]))
    covered = [t for unit in units for t in unit.test_ids]
    expected = [t for ids in grouped.values() for t in ids]
    if sorted(covered) != sorted(expected) or len(covered) != len(set(covered)):
        raise ExecutorError("单元规划的测试 ID 并集不等于全集或存在重复")
    return units


# ---------------------------------------------------------------------------
# 单元子进程：在当前进程跑给定测试 ID
# ---------------------------------------------------------------------------


class _RecordingResult(unittest.TextTestResult):
    """在 unittest 原有输出之外，逐个记录测试 ID 的结论与耗时。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict[str, Any]] = {}
        self._started: dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802 - unittest 接口
        self._started[test.id()] = time.monotonic()
        super().startTest(test)

    def _record(self, test: Any, outcome: str, reason: str | None = None) -> None:
        test_id = test.id() if hasattr(test, "id") else str(test)
        started = self._started.get(test_id)
        previous = self.records.get(test_id)
        if previous is not None and previous["outcome"] in {"failed", "error"}:
            return  # 子测试或清理阶段的失败已经记下，不被后续的成功覆盖
        self.records[test_id] = {
            "outcome": outcome,
            "seconds": round(time.monotonic() - started, 3) if started is not None else None,
        }
        if reason is not None:
            self.records[test_id]["reason"] = reason  # 跳过原因（E2-04：P0 证据的跳过清单逐条带原因）

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self._record(test, "failed")

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", str(reason))

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:  # noqa: N802
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._record(test, "failed" if issubclass(err[0], test.failureException) else "error")


def run_unit(start: Path, tests_file: Path, result_path: Path) -> int:
    # 导入路径与 discover 一致：起点目录在首位（discover 以它为顶层），其后是当前目录（仓库根）。
    _import_path_like_unittest_main()
    start_abs = str(Path(start).resolve())
    if start_abs not in sys.path:
        sys.path.insert(0, start_abs)
    payload = json.loads(Path(tests_file).read_text(encoding="utf-8"))
    test_ids = list(payload["test_ids"])
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for test_id in test_ids:
        name = test_id[len(FAILED_IMPORT_PREFIX):] if test_id.startswith(FAILED_IMPORT_PREFIX) else test_id
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=1, resultclass=_RecordingResult)
    result = runner.run(suite)
    assert isinstance(result, _RecordingResult)
    _write_json(
        result_path,
        {
            "schema_version": UNIT_RESULT_SCHEMA,
            "unit_id": payload["unit_id"],
            "tests": result.records,
            "tests_run": result.testsRun,
            "successful": result.wasSuccessful(),
        },
    )
    return 0 if result.wasSuccessful() else 1


# ---------------------------------------------------------------------------
# 状态目录：单实例锁与采集预约
# ---------------------------------------------------------------------------


def default_state_dir() -> Path:
    configured = os.environ.get("UNIT_EXECUTOR_STATE_DIR")
    if configured:
        return Path(configured)
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        return Path("/run/unit-executor")
    return Path(tempfile.gettempdir()) / f"unit-executor-{os.getuid()}"


def _state_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class Reservation:
    """采集批次的整机预约：``reservation.json`` 由申请方创建，``granted.json`` 由调度器（或无调度器时由申请方）写入。"""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = _state_dir(state_dir)
        self.request_path = self.state_dir / "reservation.json"
        self.grant_path = self.state_dir / "granted.json"

    def current(self) -> dict[str, Any] | None:
        request = _read_json_file(self.request_path)
        if request is None:
            return None
        if not _pid_alive(request.get("owner_pid")):
            # 申请方已经不在（没有 release 就退出）：预约作废，避免整机永远停派。
            for path in (self.grant_path, self.request_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
            return None
        return request

    def granted(self) -> dict[str, Any] | None:
        request = self.current()
        grant = _read_json_file(self.grant_path)
        if request is None or grant is None or grant.get("owner") != request.get("owner"):
            return None
        return grant

    def grant(self, by: str) -> None:
        request = self.current()
        if request is not None and self.granted() is None:
            _write_json(self.grant_path, {"schema_version": RESERVATION_SCHEMA, "owner": request["owner"], "granted_at_utc": _utc_now(), "granted_by": by})


def acquire(state_dir: Path, owner: str, owner_pid: int, timeout_seconds: float) -> dict[str, Any]:
    """采集批次申请整机：创建预约，等调度器停派、在跑单元结束并清理后批准；没有调度器在跑时立即批准。"""

    if not owner or "/" in owner:
        raise ExecutorError("owner 非法")
    reservation = Reservation(state_dir)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            descriptor = os.open(reservation.request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = reservation.current()
            if existing is not None and existing.get("owner") == owner:
                break
            if time.monotonic() > deadline:
                raise ExecutorError(f"已有其它采集占用整机：{existing}")
            time.sleep(POLL_SECONDS)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": RESERVATION_SCHEMA, "owner": owner, "owner_pid": owner_pid, "requested_at_utc": _utc_now()}, handle)
        break
    lock_path = reservation.state_dir / "scheduler.lock"
    while True:
        grant = reservation.granted()
        if grant is not None:
            return grant
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass  # 有调度器在跑：等它停派并批准
        else:
            reservation.grant("no-scheduler")
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        if time.monotonic() > deadline:
            release(state_dir, owner)
            raise ExecutorError("等待调度器批准整机预约超时，已撤回预约")
        time.sleep(POLL_SECONDS)


def release(state_dir: Path, owner: str) -> bool:
    reservation = Reservation(state_dir)
    request = _read_json_file(reservation.request_path)
    if request is None:
        return False
    if request.get("owner") != owner:
        raise ExecutorError(f"预约属于 {request.get('owner')}，不能由 {owner} 归还")
    for path in (reservation.grant_path, reservation.request_path):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    return True


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


def machine_memory_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(int(line.split()[1]) / 1024 * 0.8)
    except OSError:
        pass
    return 64 * 1024


def _session_members(session_id: int) -> list[int]:
    """Linux：列出属于某会话的进程（单元自成会话，会话号即单元主进程号）。"""

    members: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return members
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fields = stat_text.rsplit(")", 1)[-1].split()
        if len(fields) > 3 and fields[3] == str(session_id):
            members.append(int(entry.name))
    return members


@dataclass
class Running:
    unit: Unit
    pid: int
    started: float
    log_path: Path
    result_path: Path
    record_path: Path
    kind: str
    timed_out: bool = False
    started_at_utc: str = ""


@dataclass
class Outcome:
    unit: Unit
    kind: str
    exit_code: int | None
    signal: int | None
    timed_out: bool
    seconds: float
    cpu_seconds: float
    max_rss_mb: float
    orphans: list[int]
    log_path: Path
    result: dict[str, Any] | None
    extra: dict[str, Any] = field(default_factory=dict)
    started_at_utc: str = ""
    completed_at_utc: str = ""

    @property
    def passed(self) -> bool:
        if self.exit_code != 0 or self.signal is not None or self.timed_out:
            return False
        # 命令单元只认退出码、信号与超时（结果文件由命令自己写、调用方核对）；测试单元还要有一份成功的结果文件。
        return bool(self.unit.command) or (self.result is not None and bool(self.result.get("successful")))


def unit_spec(unit: Unit, *, start: Path | None, pattern: str | None, timeout_seconds: float) -> dict[str, Any]:
    """单元规格（E3-01 承接比对的一项）：决定「这个单元执行的是什么」的全部字段；预计秒数只影响派发顺序，不算。"""

    common = {"unit_id": unit.unit_id, "env": dict(unit.env), "cores": unit.quota.cores, "memory_mb": unit.quota.memory_mb,
              "exclusive": unit.exclusive, "timeout_seconds": unit.timeout_seconds or timeout_seconds}
    if unit.command:
        return {"type": "command", "argv": list(unit.command), "cwd": unit.cwd, **common}
    return {"type": "test", "start": str(start) if start is not None else None, "pattern": pattern, "test_ids": list(unit.test_ids),
            "launcher": list(unit.launcher), **common}


class Recorder:
    """单元执行记录（E3-01）：每个单元每次执行（正式、诊断）回收时写一条，运行目录一份；给了记录库再入库一份、日志
    按内容摘要入库。记录格式与各字段的含义见 ``unit_records.py``。``currents`` 是本次运行里每个单元的当前事实（规格、
    输入、能否承接）；``environment`` 为 None 的模式（``run``／``run-commands``）不算环境与输入。"""

    def __init__(self, *, out_dir: Path, mode: str, run_id: str, policy: str, executor: dict[str, Any],
                 environment: list[dict[str, Any]] | None, currents: dict[str, Any], store: Any = None) -> None:
        self.records = _records_module()
        self.out_dir = Path(out_dir)
        self.mode = mode
        self.run_id = run_id
        self.policy = policy
        self.executor = executor
        self.environment = environment
        self.environment_sha256 = self.records.entries_sha256(environment) if environment is not None else None
        self.currents = currents
        self.store = store

    def write(self, item: "Running", outcome: Outcome) -> dict[str, Any]:
        current = self.currents[item.unit.unit_id]
        log_digest = self.records.path_digest(item.log_path) if item.log_path.exists() else None
        log = {"path": str(item.log_path), "sha256": log_digest, "bytes": item.log_path.stat().st_size if log_digest else None}
        if self.store is not None and log_digest:
            log["stored"] = str(self.store.put_log(item.log_path, log_digest))
        body = {
            "unit_id": item.unit.unit_id,
            "unit_type": "command" if item.unit.command else "test",
            "kind": item.kind,
            "run": {"run_id": self.run_id, "mode": self.mode, "out_dir": str(self.out_dir)},
            "executor": self.executor,
            "policy_sha256": self.policy,
            "environment": self.environment,
            "environment_sha256": self.environment_sha256,
            "spec": current.spec,
            "spec_sha256": current.spec_sha256,
            "inputs": current.inputs,
            "inputs_sha256": current.inputs_sha256,
            "inheritable": current.inheritable,
            "not_inheritable_reason": current.reason or None,
            "test_ids": list(item.unit.test_ids),
            "tests": (outcome.result or {}).get("tests") if not item.unit.command else None,
            "passed": outcome.passed,
            "exit_code": outcome.exit_code,
            "signal": outcome.signal,
            "timed_out": outcome.timed_out,
            "seconds": outcome.seconds,
            "cpu_seconds": outcome.cpu_seconds,
            "max_rss_mb": outcome.max_rss_mb,
            "cores": item.unit.quota.cores,
            "memory_mb": item.unit.quota.memory_mb,
            "orphans": outcome.orphans,
            "log": log,
            "started_at_utc": outcome.started_at_utc,
            "completed_at_utc": outcome.completed_at_utc,
        }
        record = self.records.seal_record(body)
        _write_json(item.record_path, record)
        path = self.store.put_record(record) if self.store is not None else item.record_path
        outcome.extra.update(disposition="executed", record_sha256=record["record_sha256"], record_path=str(path))
        return record


def _plain_currents(records: Any, units: list[Unit], *, start: Path | None, pattern: str | None, timeout: float, reason: str) -> dict[str, Any]:
    """不承接的模式（run／run-commands）下各单元的当前事实：只有规格，不算输入。"""

    currents = {}
    for unit in units:
        spec = unit_spec(unit, start=start, pattern=pattern, timeout_seconds=timeout)
        currents[unit.unit_id] = records.Current(unit_id=unit.unit_id, unit_type="command" if unit.command else "test", spec=spec,
                                                 spec_sha256=_sha256(spec), inputs=None, inputs_sha256=None, inheritable=False, reason=reason)
    return currents


class Scheduler:
    def __init__(
        self,
        *,
        out_dir: Path,
        state_dir: Path,
        parallelism: int,
        machine_cores: int,
        machine_memory: int,
        config: ExecutorConfig,
        policy: str,
        start: Path,
        recorder: Recorder,
        unit_argv: list[str] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.state_dir = _state_dir(state_dir)
        self.parallelism = max(1, parallelism)
        self.machine_cores = max(1, machine_cores)
        self.machine_memory = max(256, machine_memory)
        self.config = config
        self.policy = policy
        self.start = Path(start)
        self.recorder = recorder
        self.unit_argv = unit_argv
        self.events_path = self.out_dir / "events.jsonl"
        self.reservation = Reservation(self.state_dir)
        self.running: dict[int, Running] = {}
        self.max_cores_in_use = 0.0

    # -- 事件与预约 -----------------------------------------------------------
    def event(self, name: str, **payload: Any) -> None:
        record = {"t": round(time.time(), 3), "event": name, "cores_in_use": self.cores_in_use(), "running": sorted(r.unit.unit_id for r in self.running.values()), **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def abort(self) -> None:
        """调度器自身出错或被中断：终止全部在跑单元（整个会话），不留后台进程。"""

        for pid in list(self.running):
            for member in [pid, *_session_members(pid)]:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(member, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
            self.running.pop(pid, None)

    def cores_in_use(self) -> float:
        return sum(item.unit.quota.cores for item in self.running.values())

    def memory_in_use(self) -> int:
        return sum(item.unit.quota.memory_mb for item in self.running.values())

    def honor_reservation(self) -> list[Outcome]:
        """有采集预约时停止派发：等在跑单元结束（残留进程已清理）后批准，等 release 后再继续；返回期间结束的单元。"""

        if self.reservation.current() is None:
            return []
        self.event("reservation-requested")
        finished: list[Outcome] = []
        while self.running:
            finished.extend(self.reap(block=True))
        self.reservation.grant(f"scheduler-{os.getpid()}")
        self.event("reservation-granted")
        while self.reservation.current() is not None:
            time.sleep(POLL_SECONDS)
        self.event("reservation-released")
        return finished

    # -- 启动与回收 -----------------------------------------------------------
    def launch(self, unit: Unit, kind: str) -> None:
        safe = unit.unit_id.replace("#", "-").replace("!", "-")
        suffix = "" if kind == "formal" else f".{kind}"
        log_path = self.out_dir / "logs" / f"{safe}{suffix}.log"
        result_path = self.out_dir / "units" / f"{safe}{suffix}.result.json"
        record_path = self.out_dir / "units" / f"{safe}{suffix}.record.json"
        tests_path = self.out_dir / "units" / f"{safe}{suffix}.tests.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for stale in (result_path, record_path):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        if unit.command:
            # 命令单元：经 /bin/sh 切到单元的工作目录再 exec 原命令（posix_spawn 不能指定工作目录；exec 按 PATH 找命令）。
            argv = ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd or os.getcwd(), *unit.command]
        else:
            _write_json(tests_path, {"unit_id": unit.unit_id, "test_ids": list(unit.test_ids)})
            argv = self.unit_argv or [sys.executable, str(Path(__file__).resolve()), "run-unit"]
            argv = [*unit.launcher, *argv, "--start", str(self.start), "--tests-file", str(tests_path), "--result", str(result_path)]
        env = {
            **os.environ,
            # 单元内部并行度：默认按额度向上取整（至少 1）。单元自己显式给出的（如后端 Go 测试按 0.7 核排程、内部仍要
            # 两路并行编译与跑测试包）优先——调度额度管整机分配，内部并行度管单元自己怎么跑，两者可以不同。
            "UNIT_EXECUTOR_CORES": str(max(1, math.ceil(unit.quota.cores))),
            "GOMAXPROCS": str(max(1, math.ceil(unit.quota.cores))),
            **dict(unit.env),
            "UNIT_EXECUTOR_UNIT": unit.unit_id,
            "UNIT_EXECUTOR_KIND": kind,
            # 本调度器的状态目录：单元里再启动调度器时，用的若正是这个目录就是嵌套（会卡在同一把锁上），直接拒绝。
            "UNIT_EXECUTOR_PARENT_STATE_DIR": str(self.state_dir.resolve()),
            # 与 make 目标一致禁写字节码：绕过 make 直接运行（如只重跑一部分模块）时，测试也不会在树里留下 __pycache__
            # ——驱动清单等检查遇到它会报错。
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        actions = [
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            (os.POSIX_SPAWN_DUP2, 1, 2),
        ]
        # 启动前缀可能是 unshare 之类的裸命令名：按 PATH 解析（posix_spawn 不查 PATH）。
        executable = argv[0] if os.sep in argv[0] else (shutil.which(argv[0], path=env.get("PATH")) or argv[0])
        pid = os.posix_spawn(executable, argv, env, file_actions=actions, setsid=True)
        self.running[pid] = Running(unit, pid, time.monotonic(), log_path, result_path, record_path, kind, started_at_utc=_utc_now())
        self.max_cores_in_use = max(self.max_cores_in_use, self.cores_in_use())
        self.event("start", unit=unit.unit_id, kind=kind, pid=pid, cores=unit.quota.cores)

    def _clean_orphans(self, session_id: int) -> list[int]:
        deadline = time.monotonic() + self.config.orphan_grace_seconds
        members = _session_members(session_id)
        while members and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            members = _session_members(session_id)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in _session_members(session_id):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
            if sig == signal.SIGTERM and _session_members(session_id):
                time.sleep(1.0)
        return members

    def reap(self, *, block: bool) -> list[Outcome]:
        finished: list[Outcome] = []
        while True:
            for pid, item in list(self.running.items()):
                elapsed = time.monotonic() - item.started
                # 单元自带超时（命令单元）优先，否则用调度配置的统一超时；先 SIGTERM，30 秒后仍在就 SIGKILL。
                limit = item.unit.timeout_seconds or self.config.unit_timeout_seconds
                if elapsed > limit and not item.timed_out:
                    item.timed_out = True
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGTERM)
                    self.event("timeout", unit=item.unit.unit_id, pid=pid)
                elif item.timed_out and elapsed > limit + 30:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGKILL)
                try:
                    waited, status, usage = os.wait4(pid, os.WNOHANG)
                except ChildProcessError:
                    waited, status, usage = pid, 0, None
                if waited == 0:
                    continue
                del self.running[pid]
                orphans = self._clean_orphans(pid)
                exit_code = os.waitstatus_to_exitcode(status)
                outcome = Outcome(
                    unit=item.unit,
                    kind=item.kind,
                    exit_code=exit_code if exit_code >= 0 else None,
                    signal=-exit_code if exit_code < 0 else None,
                    timed_out=item.timed_out,
                    seconds=round(time.monotonic() - item.started, 3),
                    cpu_seconds=round((usage.ru_utime + usage.ru_stime) if usage else 0.0, 3),
                    max_rss_mb=round((usage.ru_maxrss / (1024 * 1024) if sys.platform == "darwin" else usage.ru_maxrss / 1024) if usage else 0.0, 1),
                    orphans=orphans,
                    log_path=item.log_path,
                    result=_read_json_file(item.result_path),
                    started_at_utc=item.started_at_utc,
                    completed_at_utc=_utc_now(),
                )
                # 单元执行记录（E3-01）：不可变、带自摘要；给了记录库同时入库（承接以库里的记录为准）。
                self.recorder.write(item, outcome)
                self.event("exit", unit=item.unit.unit_id, kind=item.kind, pid=pid, exit_code=outcome.exit_code, signal=outcome.signal,
                           timed_out=outcome.timed_out, orphans=len(orphans), seconds=outcome.seconds)
                finished.append(outcome)
            if finished or not block or not self.running:
                return finished
            time.sleep(POLL_SECONDS)

    # -- 三段执行 ---------------------------------------------------------------
    def fits(self, unit: Unit) -> bool:
        return (
            len(self.running) < self.parallelism
            and self.cores_in_use() + unit.quota.cores <= self.machine_cores
            and (not self.running or self.memory_in_use() + unit.quota.memory_mb <= self.machine_memory)
        )

    def run_parallel(self, units: list[Unit], kind: str) -> list[Outcome]:
        pending = sorted(units, key=lambda u: (-u.weight, u.unit_id))
        outcomes: list[Outcome] = []
        while pending or self.running:
            outcomes.extend(self.honor_reservation())
            launched = False
            for unit in list(pending):
                if self.fits(unit):
                    pending.remove(unit)
                    self.launch(unit, kind)
                    launched = True
                    break
            if launched:
                continue
            outcomes.extend(self.reap(block=True))
        return outcomes

    def run_alone(self, units: list[Unit], kind: str) -> list[Outcome]:
        outcomes: list[Outcome] = []
        for unit in sorted(units, key=lambda u: u.unit_id):
            outcomes.extend(self.honor_reservation())
            while self.running:
                outcomes.extend(self.reap(block=True))
            self.launch(unit, kind)
            while self.running:
                outcomes.extend(self.reap(block=True))
        return outcomes


def hold_scheduler_lock(state_dir: Path, *, wait_seconds: float) -> int:
    """同一台机器同一时间只运行一个调度器：取得状态目录里的 flock；已被占用时等待，超时报错。"""

    # 嵌套：调度单元里又用外层调度器的状态目录启动调度器，会一直等外层释放锁（外层又在等这个单元）。只比外层调度器
    # 下发的状态目录：单元里的测试另给状态目录（参数或 UNIT_EXECUTOR_STATE_DIR）照常可用。
    parent = os.environ.get("UNIT_EXECUTOR_PARENT_STATE_DIR")
    if parent and Path(parent).resolve() == _state_dir(state_dir).resolve():
        raise ExecutorError(
            f"在调度单元（{os.environ.get('UNIT_EXECUTOR_UNIT', '未知')}）里又用外层调度器的状态目录启动调度器：嵌套调度会卡在"
            "同一把锁上，改为在外层清单里展开这些单元（或另给状态目录）"
        )
    path = _state_dir(state_dir) / "scheduler.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if not announced:
                print(f"已有调度器在本机运行，等待它结束：{path}", file=sys.stderr, flush=True)
                announced = True
            if time.monotonic() > deadline:
                os.close(descriptor)
                raise ExecutorError("等待本机其它调度器结束超时")
            time.sleep(1.0)


def prepare_shared_bytecode(out_dir: Path, scheduler: Scheduler, *, helper: Path, sources: list[Path], timeout_seconds: float = 1800.0) -> dict[str, Any]:
    """字节码共享层（E2-02）：沿用环境里已有的前缀，或者预编译一份到记录目录，返回状态、前缀与耗时。

    预编译在任何测试单元之前、独占整机运行（并发数经 ``UNIT_EXECUTOR_CORES`` 取整机核数）；成功后把前缀写进本进程
    环境，之后派发的单元都继承它、只读使用（单元一律禁写字节码）。
    """

    inherited = os.environ.get("PYTHONPYCACHEPREFIX", "")
    if inherited and Path(inherited).is_dir():
        return {"status": "inherited", "prefix": inherited, "seconds": 0.0}
    prefix = (Path(out_dir) / "pycache-shared").resolve()
    command = [sys.executable, str(helper), str(prefix), *(str(Path(source).resolve()) for source in sources)]
    environment = {**os.environ, "UNIT_EXECUTOR_CORES": str(scheduler.machine_cores), "PYTHONDONTWRITEBYTECODE": "1"}
    environment.pop("PYTHONPYCACHEPREFIX", None)
    scheduler.event("bytecode-start", prefix=str(prefix), cores=scheduler.machine_cores)
    started = time.monotonic()
    detail: dict[str, Any] = {}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout_seconds, stdin=subprocess.DEVNULL)
        lines = completed.stdout.strip().splitlines()
        detail = json.loads(lines[-1]) if lines else {"stderr": completed.stderr[-2000:]}
        passed = completed.returncode == 0 and detail.get("status") == "ready"
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        passed, detail = False, {"error": str(error)}
    seconds = round(time.monotonic() - started, 3)
    scheduler.event("bytecode-exit", prefix=str(prefix), passed=passed, seconds=seconds)
    if passed:
        os.environ["PYTHONPYCACHEPREFIX"] = str(prefix)
        return {"status": "ready", "prefix": str(prefix), "seconds": seconds,
                "stdlib_pyc": detail.get("stdlib_pyc"), "sources_pyc": detail.get("sources_pyc")}
    return {"status": "failed", "prefix": None, "seconds": seconds, "detail": detail}


# ---------------------------------------------------------------------------
# 汇总与全集核对
# ---------------------------------------------------------------------------


def _disposition(outcome: Outcome) -> dict[str, Any]:
    """汇总行里的处置（E3-01）：本次执行还是承接，记录摘要；承接的另带原运行。"""

    row = {"disposition": outcome.extra.get("disposition", "executed"), "record_sha256": outcome.extra.get("record_sha256")}
    if outcome.extra.get("inherited_from"):
        row["inherited_from"] = outcome.extra["inherited_from"]
    return row


def _diagnostic_rows(diagnostic: list[Outcome]) -> list[dict[str, Any]]:
    return [{"unit_id": o.unit.unit_id, "kind": o.kind, "passed": o.passed, "exit_code": o.exit_code, "signal": o.signal,
             "timed_out": o.timed_out, "seconds": o.seconds, "log": str(o.log_path), "record_sha256": o.extra.get("record_sha256")}
            for o in diagnostic]


def summarize(units: list[Unit], formal: list[Outcome], diagnostic: list[Outcome], expected: set[str], elapsed: float, policy: str) -> dict[str, Any]:
    """全集核对与汇总：只看正式执行（含承接的正式执行记录，E3-01）；诊断执行另列，不改结论。

    * 全集：各正式单元上报的测试 ID 并集必须等于 discover 全集——缺报、重复上报、全集之外的 ID（如 setUpClass
      失败的占位记录）、规划了却没执行也没承接的单元，任何一种都判失败；
    * 单元：退出码非 0、被信号终止、超时、没写结果文件，都判单元失败（即便它上报的测试都通过）。
    """

    reported: dict[str, list[str]] = {}
    unexpected: list[str] = []
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0, "expected_failure": 0, "unexpected_success": 0}
    unit_rows: list[dict[str, Any]] = []
    for outcome in formal:
        tests = (outcome.result or {}).get("tests") or {}
        assigned = set(outcome.unit.test_ids)
        for test_id, record in tests.items():
            if test_id in expected:
                reported.setdefault(test_id, []).append(outcome.unit.unit_id)
                key = record.get("outcome", "error")
                counts[key] = counts.get(key, 0) + 1
            else:
                unexpected.append(f"{outcome.unit.unit_id}: {test_id}")
        unit_rows.append({
            "unit_id": outcome.unit.unit_id, "kind": outcome.kind, "policy_sha256": policy, "exclusive": outcome.unit.exclusive,
            "cores": outcome.unit.quota.cores, "passed": outcome.passed, "exit_code": outcome.exit_code, "signal": outcome.signal,
            "timed_out": outcome.timed_out, "seconds": outcome.seconds, "cpu_seconds": outcome.cpu_seconds,
            "max_rss_mb": outcome.max_rss_mb, "orphans": len(outcome.orphans), "log": str(outcome.log_path),
            "missing": sorted(assigned - set(tests)), "tests": len(tests), "has_result": outcome.result is not None,
            **_disposition(outcome),
        })
    missing = sorted(expected - set(reported))
    duplicated = sorted(t for t, owners in reported.items() if len(owners) > 1)
    failed_units = [row["unit_id"] for row in unit_rows if not row["passed"]]
    not_run = sorted({u.unit_id for u in units} - {o.unit.unit_id for o in formal})
    problems = failed_units or missing or duplicated or unexpected or not_run or counts["failed"] or counts["error"] or counts["unexpected_success"]
    return {
        "schema_version": SUMMARY_SCHEMA,
        "status": "failed" if problems else "passed",
        "policy_sha256": policy,
        "elapsed_seconds": round(elapsed, 3),
        "expected_tests": len(expected),
        "reported_tests": len(reported),
        "counts": counts,
        "full_set": {"missing": missing, "duplicated": duplicated, "unexpected": unexpected, "units_not_run": not_run},
        "failed_units": failed_units,
        "units": unit_rows,
        "diagnostic": _diagnostic_rows(diagnostic),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    """标准错误最后两段与 unittest 一致：P0 收据按 ``^Ran N tests in`` 与 ``^OK``／``^FAILED`` 解析。"""

    counts = summary["counts"]
    for row in summary["units"]:
        if not row["passed"]:
            reason = f"信号 {row['signal']}" if row["signal"] else "超时" if row["timed_out"] else f"退出码 {row['exit_code']}"
            print(f"单元未通过：{row['unit_id']}（{reason}，日志 {row['log']}）", file=sys.stderr)
    for item in summary["diagnostic"]:
        verdict = "单独重跑通过" if item["passed"] else "单独重跑仍失败"
        print(f"诊断重跑（不改结论）：{item['unit_id']} {verdict}（日志 {item['log']}）", file=sys.stderr)
    full_set = summary["full_set"]
    for key, label in (("missing", "缺报"), ("duplicated", "重复上报"), ("unexpected", "全集之外"), ("units_not_run", "未执行的单元")):
        if full_set[key]:
            print(f"全集核对失败：{label} {len(full_set[key])} 个，前几个：{full_set[key][:5]}", file=sys.stderr)
    bytecode_failed = (summary.get("bytecode_cache") or {}).get("status") == "failed"
    if bytecode_failed:
        print(f"字节码共享层预编译失败（其余单元已照跑、各自从源码编译）：{summary['bytecode_cache'].get('detail')}", file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    print(f"Ran {summary['reported_tests']} tests in {summary['elapsed_seconds']:.3f}s", file=sys.stderr)
    print("", file=sys.stderr)
    if summary["status"] == "passed":
        print(f"OK (skipped={counts['skipped']})" if counts["skipped"] else "OK", file=sys.stderr)
    else:
        # errors 另计执行层问题：没写结果、被信号终止、超时的单元，以及全集核对的每一类问题。
        infrastructure = sum(1 for row in summary["units"] if not row["has_result"] or row["signal"] or row["timed_out"])
        infrastructure += sum(1 for key in ("missing", "duplicated", "unexpected", "units_not_run") if full_set[key])
        infrastructure += 1 if bytecode_failed else 0
        details = [f"failures={counts['failed']}", f"errors={counts['error'] + infrastructure}"]
        if counts["skipped"]:
            details.append(f"skipped={counts['skipped']}")
        print(f"FAILED ({', '.join(details)})", file=sys.stderr)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "run-commands", "run-gates"):
        p = sub.add_parser(name)
        if name == "run-commands":
            p.add_argument("--manifest", type=Path, required=True, help="命令单元清单（unit-executor-commands/v1）")
        elif name == "run-gates":
            p.add_argument("--manifest", type=Path, required=True, help="门禁清单（unit-executor-gates/v1）")
        else:
            p.add_argument("--start", type=Path, default=DEFAULT_START)
            p.add_argument("--pattern", default=DEFAULT_PATTERN)
        if name != "run-commands":
            p.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
            p.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--parallel", type=int, default=0, help="同时在跑的单元个数上限；0 取调度配置的默认值，1 为逐个执行")
        p.add_argument("--cores", type=int, default=0, help="整机核数（默认 os.cpu_count()）")
        p.add_argument("--state-dir", type=Path, default=None)
        p.add_argument("--out-dir", type=Path, default=None)
        p.add_argument("--wait-seconds", type=float, default=7200.0, help="本机已有调度器在跑时最多等待多久")
        if name in ("run", "run-commands", "run-gates"):
            p.add_argument("--shared-caches", choices=("auto", "off"), default="auto",
                           help="字节码共享层与身份记忆化：auto 沿用环境里已有的、没有就在记录目录里新建；off 都不准备（单元按原环境运行，诊断用）")
            p.add_argument("--bytecode-helper", type=Path, default=DEFAULT_BYTECODE_HELPER, help="字节码共享层的预编译工具（测试与诊断用）")
            p.add_argument("--bytecode-source", type=Path, action="append", default=None, help="预编译进共享层的源码目录，可重复；默认 tools")
        if name == "run-gates":
            # E3-01：记录库与两种模式（方案 D12）。缺省重新执行全集；入口门禁（驱动 entry-gates.sh）缺省全集通过。
            p.add_argument("--record-store", type=Path, default=None, help="单元执行记录库：记录与日志入库，全集通过模式从这里找可承接的记录")
            p.add_argument("--mode", choices=("re-execute", "full-set-pass"), default="re-execute",
                           help="re-execute：重新执行全集，不承接；full-set-pass：全集通过，承接有效记录、只执行其余单元（要给记录库）")
            p.add_argument("--inheritance-max-age-hours", type=float, default=168.0, help="承接期限（小时），默认 168（7 天），只能调小")
            p.add_argument("--decide-only", action="store_true",
                           help="只判定每个单元承接还是执行（写 decisions.json 并打印），不执行、不写记录、不占调度锁")
    unit = sub.add_parser("run-unit")
    unit.add_argument("--start", type=Path, required=True)
    unit.add_argument("--tests-file", type=Path, required=True)
    unit.add_argument("--result", type=Path, required=True)
    for name in ("acquire", "release"):
        p = sub.add_parser(name)
        p.add_argument("--owner", required=True)
        p.add_argument("--state-dir", type=Path, default=None)
        if name == "acquire":
            p.add_argument("--owner-pid", type=int, required=True, help="采集批次脚本的进程号；它退出而未 release 时预约自动作废")
            p.add_argument("--timeout", type=float, default=7200.0)
    return parser.parse_args(argv)


def _prepare(args: argparse.Namespace) -> tuple[ExecutorConfig, dict[str, float], dict[str, float], int, int, dict[str, list[str]], list[Unit], str, str]:
    config = load_config(args.config)
    weights = load_weights(args.weights)
    durations = load_durations(args.durations)
    parallelism = args.parallel or config.default_parallelism
    cores = args.cores or os.cpu_count() or 1
    # 范围：默认起点与模式是全量（make test-capture-tools），此时校验调度配置；换了起点或模式只跑一部分模块。
    full_set = Path(args.start).resolve() == DEFAULT_START.resolve() and args.pattern == DEFAULT_PATTERN
    grouped = discover_test_ids(args.start, args.pattern)
    units = plan_units(grouped, config, weights, durations, machine_cores=cores, full_set=full_set)
    scope = "full" if full_set else "subset"
    return config, weights, durations, parallelism, cores, grouped, units, policy_digest(config, weights, durations, parallelism), scope


def _out_dir(args: argparse.Namespace) -> Path:
    """执行记录目录：参数优先，其次环境变量 UNIT_EXECUTOR_OUT_DIR（门禁脚本把记录放到自己的日志旁边），最后临时目录。"""

    out_dir = args.out_dir or (Path(os.environ["UNIT_EXECUTOR_OUT_DIR"]) if os.environ.get("UNIT_EXECUTOR_OUT_DIR") else None) \
        or Path(tempfile.gettempdir()) / "unit-executor-runs" / time.strftime("%Y%m%dt%H%M%Sz", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _prepare_shared_caches(args: argparse.Namespace, out_dir: Path, scheduler: Scheduler) -> tuple[dict[str, Any], str | None]:
    """字节码共享层与身份记忆化（E2-02）：在任何单元之前准备，耗时计入总时长；``--shared-caches off`` 时都不准备。"""

    if args.shared_caches == "off":
        bytecode = {"status": "off", "prefix": os.environ.get("PYTHONPYCACHEPREFIX") or None, "seconds": 0.0}
    else:
        bytecode = prepare_shared_bytecode(out_dir, scheduler, helper=args.bytecode_helper,
                                           sources=list(args.bytecode_source or DEFAULT_BYTECODE_SOURCES))
    note = {"inherited": "沿用环境里的前缀", "ready": f"预编译 {bytecode['seconds']:.1f} 秒", "failed": "预编译失败",
            "off": "未准备（--shared-caches off）"}[bytecode["status"]]
    print(f"字节码共享层：{note}（{bytecode['prefix'] or '不设前缀'}）", file=sys.stderr, flush=True)
    # 身份记忆化：本次运行的全部单元共用一个缓存目录，同一棵树的身份五摘要与评估器四项只算一次；键是整树逐文件摘要，
    # 测试改副本树后自然重算（见 codex_upgrade_tool_identity_policy）。
    identity_memo = os.environ.get(IDENTITY_MEMO_ENV) or None
    if args.shared_caches != "off" and not identity_memo:
        identity_memo = str((out_dir / "identity-memo").resolve())
        os.environ[IDENTITY_MEMO_ENV] = identity_memo
    return bytecode, identity_memo


def summarize_commands(units: list[Unit], formal: list[Outcome], diagnostic: list[Outcome], elapsed: float, policy: str) -> dict[str, Any]:
    """命令单元的汇总：清单里每个单元都要正式执行一次，退出码 0、没有被信号终止、没有超时才算通过；诊断执行另列、不改结论。"""

    rows = [{
        "unit_id": o.unit.unit_id, "kind": o.kind, "policy_sha256": policy, "exclusive": o.unit.exclusive, "cores": o.unit.quota.cores,
        "passed": o.passed, "exit_code": o.exit_code, "signal": o.signal, "timed_out": o.timed_out, "seconds": o.seconds,
        "cpu_seconds": o.cpu_seconds, "max_rss_mb": o.max_rss_mb, "orphans": len(o.orphans), "log": str(o.log_path),
        **_disposition(o),
    } for o in formal]
    failed = [row["unit_id"] for row in rows if not row["passed"]]
    not_run = sorted({u.unit_id for u in units} - {o.unit.unit_id for o in formal})
    return {
        "schema_version": COMMANDS_SUMMARY_SCHEMA,
        "status": "failed" if failed or not_run else "passed",
        "policy_sha256": policy,
        "elapsed_seconds": round(elapsed, 3),
        "unit_count": len(units),
        "failed_units": failed,
        "units_not_run": not_run,
        "units": rows,
        "diagnostic": _diagnostic_rows(diagnostic),
    }


def _print_commands_summary(summary: dict[str, Any]) -> None:
    for row in summary["units"]:
        if not row["passed"]:
            reason = f"信号 {row['signal']}" if row["signal"] else "超时" if row["timed_out"] else f"退出码 {row['exit_code']}"
            print(f"单元未通过：{row['unit_id']}（{reason}，日志 {row['log']}）", file=sys.stderr)
    for item in summary["diagnostic"]:
        verdict = "单独重跑通过" if item["passed"] else "单独重跑仍失败"
        print(f"诊断重跑（不改结论）：{item['unit_id']} {verdict}（日志 {item['log']}）", file=sys.stderr)
    if summary["units_not_run"]:
        print(f"未执行的单元 {len(summary['units_not_run'])} 个：{summary['units_not_run'][:5]}", file=sys.stderr)
    if (summary.get("bytecode_cache") or {}).get("status") == "failed":
        print(f"字节码共享层预编译失败（其余单元已照跑、各自从源码编译）：{summary['bytecode_cache'].get('detail')}", file=sys.stderr)
    passed = sum(1 for row in summary["units"] if row["passed"])
    print("-" * 70, file=sys.stderr)
    print(f"命令单元 {summary['unit_count']} 个，通过 {passed} 个，用时 {summary['elapsed_seconds']:.3f}s", file=sys.stderr)
    print("OK" if summary["status"] == "passed" else f"FAILED (units={summary['unit_count'] - passed})", file=sys.stderr)


def _run_commands(args: argparse.Namespace) -> int:
    """``run-commands``：按清单把每条命令当作一个单元，与测试单元同一套调度（额度、独占、预约、诊断、会话清理）。"""

    config = load_config(args.config)
    cores = args.cores or os.cpu_count() or 1
    units, manifest = load_command_manifest(args.manifest, machine_cores=cores)
    parallelism = args.parallel or config.default_parallelism
    policy = _sha256({"commands": manifest, "parallelism": parallelism, "config": config.raw})
    out_dir = _out_dir(args)
    state_dir = args.state_dir or default_state_dir()
    records = _records_module()
    recorder = Recorder(out_dir=out_dir, mode=records.RE_EXECUTE, run_id=records.new_run_id(), policy=policy,
                        executor=records.executor_version([args.bytecode_helper]), environment=None,
                        currents=_plain_currents(records, units, start=None, pattern=None, timeout=config.unit_timeout_seconds,
                                                 reason="命令单元清单模式（run-commands）不承接"))
    lock = hold_scheduler_lock(state_dir, wait_seconds=args.wait_seconds)
    scheduler = Scheduler(
        out_dir=out_dir, state_dir=state_dir, parallelism=parallelism, machine_cores=cores,
        machine_memory=machine_memory_mb(), config=config, policy=policy, start=Path("."), recorder=recorder,
    )
    try:
        started = time.monotonic()
        _write_json(out_dir / "plan.json", {"policy_sha256": policy, "scope": "commands", "parallelism": parallelism, "machine_cores": cores,
                                            "units": [{"unit_id": u.unit_id, "argv": list(u.command), "cwd": u.cwd, "cores": u.quota.cores,
                                                       "exclusive": u.exclusive, "timeout_seconds": u.timeout_seconds} for u in units]})
        print(f"调度：命令单元 {len(units)} 个（{sum(1 for u in units if u.exclusive)} 个独占），并行度 {parallelism}，整机 {cores} 核，"
              f"策略 {policy[:12]}，记录 {out_dir}", file=sys.stderr, flush=True)
        bytecode, identity_memo = _prepare_shared_caches(args, out_dir, scheduler)
        formal = scheduler.run_parallel([u for u in units if not u.exclusive], "formal")
        formal += scheduler.run_alone([u for u in units if u.exclusive], "formal")
        diagnostic = scheduler.run_alone([o.unit for o in formal if not o.passed], "diagnostic")
        summary = summarize_commands(units, formal, diagnostic, time.monotonic() - started, policy)
        summary.update({"max_cores_in_use": scheduler.max_cores_in_use, "machine_cores": cores, "parallelism": parallelism,
                        "bytecode_cache": bytecode, "identity_memo": identity_memo})
        if bytecode["status"] == "failed":
            summary["status"] = "failed"
        _write_json(out_dir / "summary.json", summary)
        _print_commands_summary(summary)
        return 0 if summary["status"] == "passed" else 1
    except BaseException:
        scheduler.abort()
        raise
    finally:
        os.close(lock)


def summarize_gates(
    groups: list[DiscoverGroup],
    group_units: dict[str, list[Unit]],
    expected: dict[str, set[str]],
    command_units: list[Unit],
    gates: list[Gate],
    formal: list[Outcome],
    diagnostic: list[Outcome],
    elapsed: float,
    policy: str,
) -> dict[str, Any]:
    """门禁清单的汇总（E2-04）：测试组照 ``run`` 做全集核对，命令单元只认退出码、信号与超时，结论按门禁项聚合。

    门禁项通过＝它的每个命令单元都正式执行（或承接了正式执行记录，E3-01）且通过、每个测试组的全集核对通过；起止
    时间取本次执行的成员单元的最早开始与最晚结束（承接的单元另列 ``inherited_units``，不拉长时间窗）。测试组另列
    逐条跳过清单（测试 ID 与原因），供 P0 证据登记。诊断执行另列，不改结论。
    """

    by_unit = {outcome.unit.unit_id: outcome for outcome in formal}
    rows: list[dict[str, Any]] = []
    group_rows: dict[str, dict[str, Any]] = {}
    for group in groups:
        members = group_units[group.group_id]
        ids = {unit.unit_id for unit in members}
        summary = summarize(members, [o for o in formal if o.unit.unit_id in ids], [o for o in diagnostic if o.unit.unit_id in ids],
                            expected[group.group_id], elapsed, policy)
        skipped = sorted(
            ({"test_id": test_id, "reason": str(record.get("reason", ""))}
             for outcome in formal if outcome.unit.unit_id in ids
             for test_id, record in ((outcome.result or {}).get("tests") or {}).items()
             if record.get("outcome") == "skipped" and test_id in expected[group.group_id]),
            key=lambda item: item["test_id"],
        )
        group_rows[group.group_id] = {
            "status": summary["status"], "start": str(group.start), "pattern": group.pattern,
            "expected_tests": summary["expected_tests"], "reported_tests": summary["reported_tests"], "counts": summary["counts"],
            "full_set": summary["full_set"], "failed_units": summary["failed_units"], "units": sorted(ids), "skipped": skipped,
        }
        for row in summary["units"]:
            outcome = by_unit[row["unit_id"]]
            rows.append({"type": "test", "group_id": group.group_id, **row,
                         "started_at_utc": outcome.started_at_utc, "completed_at_utc": outcome.completed_at_utc})
    for unit in command_units:
        outcome = by_unit.get(unit.unit_id)
        if outcome is None:
            continue
        rows.append({
            "type": "command", "unit_id": unit.unit_id, "kind": outcome.kind, "policy_sha256": policy, "exclusive": unit.exclusive,
            "cores": unit.quota.cores, "passed": outcome.passed, "exit_code": outcome.exit_code, "signal": outcome.signal,
            "timed_out": outcome.timed_out, "seconds": outcome.seconds, "cpu_seconds": outcome.cpu_seconds,
            "max_rss_mb": outcome.max_rss_mb, "orphans": len(outcome.orphans), "log": str(outcome.log_path),
            "argv": list(unit.command), "cwd": unit.cwd, "started_at_utc": outcome.started_at_utc,
            "completed_at_utc": outcome.completed_at_utc, **_disposition(outcome),
        })
    gate_rows: list[dict[str, Any]] = []
    for gate in gates:
        members = list(gate.units) + [unit_id for group_id in gate.test_groups for unit_id in group_rows[group_id]["units"]]
        failed = [unit_id for unit_id in members if unit_id not in by_unit or not by_unit[unit_id].passed]
        groups_passed = all(group_rows[group_id]["status"] == "passed" for group_id in gate.test_groups)
        present = [by_unit[unit_id] for unit_id in members if unit_id in by_unit]
        executed = [o for o in present if o.extra.get("disposition", "executed") == "executed"]
        gate_rows.append({
            "gate_id": gate.gate_id,
            "status": "passed" if not failed and groups_passed else "failed",
            "units": list(gate.units),
            "test_groups": list(gate.test_groups),
            "failed_units": failed,
            "not_executed": list(gate.not_executed),
            "inherited_units": sorted(o.unit.unit_id for o in present if o.extra.get("disposition") == "inherited"),
            "started_at_utc": min((o.started_at_utc for o in executed), default=None),
            "completed_at_utc": max((o.completed_at_utc for o in executed), default=None),
            "unit_seconds": round(sum(o.seconds for o in executed), 3),
        })
    command_not_run = sorted({unit.unit_id for unit in command_units} - set(by_unit))
    return {
        "schema_version": GATES_SUMMARY_SCHEMA,
        "status": "passed" if all(row["status"] == "passed" for row in gate_rows) and not command_not_run else "failed",
        "policy_sha256": policy,
        "elapsed_seconds": round(elapsed, 3),
        "gates": gate_rows,
        "test_groups": group_rows,
        "units": rows,
        "units_not_run": command_not_run,
        "failed_units": [row["unit_id"] for row in rows if not row["passed"]],
        "diagnostic": _diagnostic_rows(diagnostic),
    }


def _print_gates_summary(summary: dict[str, Any]) -> None:
    for row in summary["units"]:
        if not row["passed"]:
            reason = f"信号 {row['signal']}" if row["signal"] else "超时" if row["timed_out"] else f"退出码 {row['exit_code']}"
            print(f"单元未通过：{row['unit_id']}（{reason}，日志 {row['log']}）", file=sys.stderr)
    for item in summary["diagnostic"]:
        verdict = "单独重跑通过" if item["passed"] else "单独重跑仍失败"
        print(f"诊断重跑（不改结论）：{item['unit_id']} {verdict}（日志 {item['log']}）", file=sys.stderr)
    for group_id, group in summary["test_groups"].items():
        for key, label in (("missing", "缺报"), ("duplicated", "重复上报"), ("unexpected", "全集之外"), ("units_not_run", "未执行的单元")):
            if group["full_set"][key]:
                print(f"测试组 {group_id} 全集核对失败：{label} {len(group['full_set'][key])} 个，前几个：{group['full_set'][key][:5]}", file=sys.stderr)
        counts = group["counts"]
        print(f"测试组 {group_id}：{group['reported_tests']}／{group['expected_tests']} 个测试，通过 {counts['passed']}、"
              f"失败 {counts['failed']}、错误 {counts['error']}、跳过 {counts['skipped']}", file=sys.stderr)
    if summary["units_not_run"]:
        print(f"未执行的命令单元 {len(summary['units_not_run'])} 个：{summary['units_not_run'][:5]}", file=sys.stderr)
    if (summary.get("bytecode_cache") or {}).get("status") == "failed":
        print(f"字节码共享层预编译失败（其余单元已照跑、各自从源码编译）：{summary['bytecode_cache'].get('detail')}", file=sys.stderr)
    inheritance, manifest = summary.get("inheritance") or {}, summary.get("unit_manifest") or {}
    if inheritance:
        print(f"承接（{inheritance.get('mode_label')}）：本次执行 {inheritance['executed']} 个单元，承接 {inheritance['inherited']} 个"
              f"（{inheritance['inherited_tests']} 个测试）；清单 {manifest.get('path')}，自检{'通过' if manifest.get('self_check') == 'passed' else '不通过'}",
              file=sys.stderr)
        for problem in (manifest.get("problems") or [])[:10]:
            print(f"清单自检不通过：{problem}", file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for gate in summary["gates"]:
        verdict = "通过" if gate["status"] == "passed" else f"未通过（失败单元 {len(gate['failed_units'])} 个：{gate['failed_units'][:5]}）"
        skipped = f"；不在本平台执行 {len(gate['not_executed'])} 项" if gate["not_executed"] else ""
        print(f"门禁 {gate['gate_id']}：{verdict}{skipped}", file=sys.stderr)
    passed = sum(1 for gate in summary["gates"] if gate["status"] == "passed")
    print(f"门禁 {len(summary['gates'])} 项，通过 {passed} 项，用时 {summary['elapsed_seconds']:.3f}s", file=sys.stderr)
    print("OK" if summary["status"] == "passed" else f"FAILED (gates={len(summary['gates']) - passed})", file=sys.stderr)


def _gate_currents(records: Any, *, groups: list[DiscoverGroup], group_units: dict[str, list[Unit]], command_units: list[Unit],
                   manifest: dict[str, Any], timeout: float) -> dict[str, Any]:
    """``run-gates`` 各单元的当前事实（E3-01）：单元规格、输入明细与摘要、能否承接（不能时写明原因）。

    采集工具测试单元的输入由执行器按静态依赖闭包算（同一模块的拆块与独占单元共用一份）；命令单元的输入由门禁清单
    声明（``inputs``），清单标了 ``inheritable: false`` 的（如 pre-A3 场景）照写记录、不承接。工作目录不是干净的 git
    检出时输入不完整（未跟踪文件不在范围里），本次一律不承接。"""

    items = {item["unit_id"]: item for item in manifest.get("units") or [] if isinstance(item, dict)}
    repo, problem = None, ""
    try:
        repo = records.RepoIndex.load(Path.cwd())
        if not repo.clean:
            problem = f"测试树不干净（{len(repo.dirty)} 项未提交或未跟踪的改动，例如 {repo.dirty[:3]}）：输入不完整，本次不承接"
    except records.RecordsError as error:
        problem = f"执行器的工作目录不是可用的 git 检出（{error}）：算不出输入，本次不承接"
    currents: dict[str, Any] = {}

    def current(unit: Unit, spec: dict[str, Any], inputs: list[dict[str, Any]] | None, reason: str) -> Any:
        return records.Current(unit_id=unit.unit_id, unit_type="command" if unit.command else "test", spec=spec, spec_sha256=_sha256(spec),
                               inputs=inputs, inputs_sha256=records.entries_sha256(inputs) if inputs is not None else None,
                               inheritable=inputs is not None and not reason, reason=reason)

    for group in groups:
        deps = records.TestDependencies(group.start.resolve().parent) if not problem else None
        cache: dict[str, Any] = {}
        for unit in group_units[group.group_id]:
            spec = unit_spec(unit, start=group.start, pattern=group.pattern, timeout_seconds=timeout)
            module = unit.module.split("+", 1)[0]
            inputs, reason = None, problem
            if not reason:
                module_file = (group.start / f"{module}.py").resolve()
                if not module_file.is_file():
                    reason = f"找不到测试模块文件：{module_file}"
                else:
                    try:
                        if module not in cache:
                            cache[module] = records.test_unit_inputs(repo, deps, module_file)
                        inputs = cache[module]
                    except (records.RecordsError, OSError, ValueError) as error:
                        reason = f"输入算不出来：{error}"
            currents[unit.unit_id] = current(unit, spec, inputs, reason)
    for unit in command_units:
        spec = unit_spec(unit, start=None, pattern=None, timeout_seconds=timeout)
        item = items.get(unit.unit_id, {})
        inputs, reason = None, ""
        if not item.get("inheritable", True):
            reason = str(item.get("not_inheritable_reason") or "门禁清单标为不可承接")
        elif "inputs" not in item:
            reason = "门禁清单没有声明这个单元的输入"
        elif problem:
            reason = problem
        else:
            try:
                inputs = records.declared_inputs(repo, item["inputs"])
            except (records.RecordsError, OSError, ValueError) as error:
                reason = f"输入算不出来：{error}"
        currents[unit.unit_id] = current(unit, spec, inputs, reason)
    return currents


def _inherited_outcome(unit: Unit, decision: Any, store: Any) -> Outcome:
    """承接的单元按原记录参加全集核对与门禁聚合（结论、测试结果、用量与起止时间都是原记录的）。"""

    record = decision.record
    return Outcome(
        unit=unit, kind="formal", exit_code=record["exit_code"], signal=None, timed_out=False,
        seconds=float(record.get("seconds") or 0.0), cpu_seconds=float(record.get("cpu_seconds") or 0.0),
        max_rss_mb=float(record.get("max_rss_mb") or 0.0), orphans=[], log_path=store.log_path(str(record["log"]["sha256"])),
        result=None if unit.command else {"tests": record["tests"], "tests_run": len(record["tests"]), "successful": True},
        extra={"disposition": "inherited", "record_sha256": record["record_sha256"], "record_path": str(decision.record_path),
               "inherited_from": {"run_id": record["run"]["run_id"], "completed_at_utc": record["completed_at_utc"]}},
        started_at_utc=record["started_at_utc"], completed_at_utc=record["completed_at_utc"],
    )


def _unit_manifest(records: Any, *, run_id: str, mode: str, max_age: float, store: Any, out_dir: Path, decided_at: str, policy: str,
                   environment: list[dict[str, Any]], executor: dict[str, Any], units: list[Unit], gates: list[Gate],
                   group_units: dict[str, list[Unit]], expected: dict[str, set[str]], currents: dict[str, Any],
                   decisions: dict[str, Any], outcomes: list[Outcome], diagnostic: list[Outcome]) -> dict[str, Any]:
    """本次运行的清单（E3-01）：逐单元写明本次执行还是承接——承接写原运行与记录摘要，执行写不承接的原因。"""

    gate_of: dict[str, list[str]] = {}
    for gate in gates:
        for unit_id in gate.units:
            gate_of.setdefault(unit_id, []).append(gate.gate_id)
        for group_id in gate.test_groups:
            for unit in group_units[group_id]:
                gate_of.setdefault(unit.unit_id, []).append(gate.gate_id)
    group_of = {unit.unit_id: group_id for group_id, members in group_units.items() for unit in members}
    by_unit = {outcome.unit.unit_id: outcome for outcome in outcomes}
    entries = []
    for unit in units:
        outcome, now = by_unit.get(unit.unit_id), currents[unit.unit_id]
        entry = {
            "unit_id": unit.unit_id, "unit_type": now.unit_type, "gates": sorted(gate_of.get(unit.unit_id, [])),
            "test_group": group_of.get(unit.unit_id),
            "disposition": outcome.extra.get("disposition", "executed") if outcome else "not_run",
            "record_sha256": outcome.extra.get("record_sha256") if outcome else None,
            "record_path": outcome.extra.get("record_path") if outcome else None,
            "passed": outcome.passed if outcome else False,
            "spec_sha256": now.spec_sha256, "inputs_sha256": now.inputs_sha256, "inheritable": now.inheritable,
        }
        if outcome is not None and outcome.extra.get("disposition") == "inherited":
            entry["basis"] = outcome.extra["inherited_from"]
        else:
            entry["reasons"] = list(decisions[unit.unit_id].reasons)
        entries.append(entry)
    inherited = [outcome for outcome in outcomes if outcome.extra.get("disposition") == "inherited"]
    return records.build_manifest(
        run_id=run_id, mode=mode, inheritance_max_age_hours=max_age, record_store=str(store.root) if store is not None else None,
        out_dir=str(out_dir), decided_at_utc=decided_at, completed_at_utc=records.utc_now(), policy_sha256=policy,
        environment=environment, environment_sha256=records.entries_sha256(environment), executor=executor,
        planned_units=[unit.unit_id for unit in units], units=entries,
        diagnostic=[{"unit_id": o.unit.unit_id, "record_sha256": o.extra.get("record_sha256"), "record_path": o.extra.get("record_path"),
                     "passed": o.passed} for o in diagnostic],
        test_groups={group_id: sorted(ids) for group_id, ids in expected.items()},
        counts={"planned": len(units), "executed": sum(1 for entry in entries if entry["disposition"] == "executed"),
                "inherited": len(inherited), "inherited_tests": sum(len((o.result or {}).get("tests") or {}) for o in inherited)},
    )


def _run_gates(args: argparse.Namespace) -> int:
    """``run-gates``（E2-04）：一个测试组与若干命令单元同一次运行、同一套调度（额度、独占、预约、诊断、会话清理），
    全部跑完再按门禁项汇总——一个门禁项失败不影响别的门禁项照跑。测试组在执行器的工作目录下 discover。

    E3-01：全集通过模式先在记录库里给每个单元找可承接的记录，只执行找不到的；重新执行全集模式全部执行。两种模式
    的记录都入库（给了记录库时），运行结束写清单并自检。"""

    records = _records_module()
    if args.mode == records.FULL_SET_PASS and args.record_store is None:
        raise ExecutorError("全集通过模式（承接）要给记录库：--record-store")
    max_age = float(args.inheritance_max_age_hours)
    if not 0 < max_age <= records.MAX_AGE_HOURS:
        raise ExecutorError(f"承接期限只能在 0～{records.MAX_AGE_HOURS:g} 小时之间（只能调小）")
    config = load_config(args.config)
    weights = load_weights(args.weights)
    durations = load_durations(args.durations)
    cores = args.cores or os.cpu_count() or 1
    groups, command_units, gates, manifest = load_gates_manifest(args.manifest, machine_cores=cores)
    parallelism = args.parallel or config.default_parallelism
    # 环境指纹要在准备共享缓存之前算：准备缓存会往本进程环境里写缓存位置。
    try:
        environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
    except records.RecordsError as error:
        raise ExecutorError(f"门禁清单的环境事实非法：{error}") from error
    executor = records.executor_version([args.bytecode_helper])
    store = records.RecordStore(args.record_store) if args.record_store is not None else None
    group_units: dict[str, list[Unit]] = {}
    expected: dict[str, set[str]] = {}
    for group in groups:
        full_set = group.start.resolve() == DEFAULT_START.resolve() and group.pattern == DEFAULT_PATTERN
        grouped = discover_test_ids(group.start, group.pattern)
        planned = plan_units(grouped, config, weights, durations, machine_cores=cores, full_set=full_set)
        group_units[group.group_id] = [replace(unit, env=group.env, launcher=group.launcher) for unit in planned]
        expected[group.group_id] = {test_id for ids in grouped.values() for test_id in ids}
    test_units = [unit for units in group_units.values() for unit in units]
    clash = sorted({unit.unit_id for unit in test_units} & {unit.unit_id for unit in command_units})
    if clash:
        raise ExecutorError(f"命令单元与测试单元重名：{clash}")
    units = test_units + command_units
    policy = gates_policy_digest(config, weights, durations, parallelism, manifest.get("scheduling"))
    currents = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units, manifest=manifest,
                              timeout=config.unit_timeout_seconds)
    run_id, decided_at = records.new_run_id(), records.utc_now()
    facts = records.RunFacts(policy_sha256=policy, environment=environment, environment_sha256=records.entries_sha256(environment),
                             executor=executor, max_age_hours=max_age, now=time.time())
    decisions = {unit.unit_id: records.evaluate(store, currents[unit.unit_id], facts) if args.mode == records.FULL_SET_PASS
                 else records.Decision(reasons=["重新执行全集：不承接"]) for unit in units}
    to_run = [unit for unit in units if not decisions[unit.unit_id].inherit]
    out_dir = _out_dir(args)
    if args.decide_only:
        # 只判定：每个单元承接还是执行、依据或原因；不执行、不写记录、不占调度锁。
        payload = {"mode": args.mode, "decided_at_utc": decided_at, "policy_sha256": policy, "environment_sha256": facts.environment_sha256,
                   "executor_sha256": executor["sha256"], "record_store": str(store.root) if store is not None else None,
                   "inherit": len(units) - len(to_run), "execute": len(to_run),
                   "units": {unit.unit_id: {"inherit": decisions[unit.unit_id].inherit, "reasons": decisions[unit.unit_id].reasons,
                                            "record_sha256": (decisions[unit.unit_id].record or {}).get("record_sha256")} for unit in units}}
        _write_json(out_dir / "decisions.json", payload)
        print(f"只判定（{records.MODE_LABELS[args.mode]}）：承接 {payload['inherit']} 个、执行 {payload['execute']} 个；明细 {out_dir / 'decisions.json'}",
              file=sys.stderr)
        return 0
    state_dir = args.state_dir or default_state_dir()
    recorder = Recorder(out_dir=out_dir, mode=args.mode, run_id=run_id, policy=policy, executor=executor, environment=environment,
                        currents=currents, store=store)
    lock = hold_scheduler_lock(state_dir, wait_seconds=args.wait_seconds)
    scheduler = Scheduler(
        out_dir=out_dir, state_dir=state_dir, parallelism=parallelism, machine_cores=cores,
        machine_memory=machine_memory_mb(), config=config, policy=policy, start=groups[0].start if groups else Path("."), recorder=recorder,
    )
    try:
        started = time.monotonic()
        _write_json(out_dir / "plan.json", {
            "policy_sha256": policy, "scope": "gates", "parallelism": parallelism, "machine_cores": cores, "mode": args.mode, "run_id": run_id,
            "record_store": str(store.root) if store is not None else None,
            "gates": [{"gate_id": g.gate_id, "units": list(g.units), "test_groups": list(g.test_groups), "not_executed": list(g.not_executed)} for g in gates],
            "units": [{"unit_id": u.unit_id, "type": "command" if u.command else "test", "tests": list(u.test_ids), "argv": list(u.command),
                       "cwd": u.cwd, "cores": u.quota.cores, "memory_mb": u.quota.memory_mb, "exclusive": u.exclusive,
                       "timeout_seconds": u.timeout_seconds, "inherit": decisions[u.unit_id].inherit,
                       "reasons": decisions[u.unit_id].reasons} for u in units],
        })
        print(f"调度：门禁 {len(gates)} 项，单元 {len(units)} 个（测试 {len(test_units)}、命令 {len(command_units)}；"
              f"{sum(1 for u in units if u.exclusive)} 个独占），并行度 {parallelism}，整机 {cores} 核，策略 {policy[:12]}，记录 {out_dir}；"
              f"{records.MODE_LABELS[args.mode]}：承接 {len(units) - len(to_run)} 个、执行 {len(to_run)} 个", file=sys.stderr, flush=True)
        if to_run:
            bytecode, identity_memo = _prepare_shared_caches(args, out_dir, scheduler)
        else:
            bytecode, identity_memo = {"status": "off", "prefix": None, "seconds": 0.0, "note": "全部承接，没有要执行的单元"}, None
        formal = scheduler.run_parallel([u for u in to_run if not u.exclusive], "formal")
        formal += scheduler.run_alone([u for u in to_run if u.exclusive], "formal")
        diagnostic = scheduler.run_alone([o.unit for o in formal if not o.passed], "diagnostic")
        inherited = [_inherited_outcome(unit, decisions[unit.unit_id], store) for unit in units if decisions[unit.unit_id].inherit]
        outcomes = formal + inherited
        summary = summarize_gates(groups, group_units, expected, command_units, gates, outcomes, diagnostic, time.monotonic() - started, policy)
        unit_manifest = _unit_manifest(records, run_id=run_id, mode=args.mode, max_age=max_age, store=store, out_dir=out_dir,
                                       decided_at=decided_at, policy=policy, environment=environment, executor=executor, units=units,
                                       gates=gates, group_units=group_units, expected=expected, currents=currents, decisions=decisions,
                                       outcomes=outcomes, diagnostic=diagnostic)
        problems = records.verify_manifest(unit_manifest, store=store)
        manifest_path = out_dir / "unit-manifest.json"
        _write_json(manifest_path, unit_manifest)
        # 自检通过才把清单存进记录库：承接要求原运行的清单把记录列为正式执行，没正常结束或自检不过的运行，它的记录不可承接。
        if store is not None and not problems:
            store.put_manifest(unit_manifest)
        summary.update({
            "max_cores_in_use": scheduler.max_cores_in_use, "machine_cores": cores, "parallelism": parallelism,
            "bytecode_cache": bytecode, "identity_memo": identity_memo, "mode": args.mode, "run_id": run_id,
            "record_store": str(store.root) if store is not None else None,
            "inheritance": {"mode_label": records.MODE_LABELS[args.mode], **{key: unit_manifest["counts"][key] for key in ("executed", "inherited", "inherited_tests")}},
            "unit_manifest": {"path": str(manifest_path), "manifest_sha256": unit_manifest["manifest_sha256"],
                              "self_check": "passed" if not problems else "failed", "problems": problems[:50]},
        })
        if bytecode["status"] == "failed" or problems:
            summary["status"] = "failed"
        _write_json(out_dir / "summary.json", summary)
        _print_gates_summary(summary)
        return 0 if summary["status"] == "passed" else 1
    except BaseException:
        scheduler.abort()
        raise
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> int:
    # 调度进程 discover 时会导入全部测试模块，同样不能在树里写字节码（单元子进程经环境变量禁写）。
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "run-unit":
            return run_unit(args.start, args.tests_file, args.result)
        if args.command == "acquire":
            grant = acquire(args.state_dir or default_state_dir(), args.owner, args.owner_pid, args.timeout)
            print(json.dumps(grant, ensure_ascii=False))
            return 0
        if args.command == "release":
            print(json.dumps({"released": release(args.state_dir or default_state_dir(), args.owner)}, ensure_ascii=False))
            return 0
        if args.command == "run-commands":
            return _run_commands(args)
        if args.command == "run-gates":
            return _run_gates(args)
        config, weights, durations, parallelism, cores, grouped, units, policy, scope = _prepare(args)
        if args.command == "plan":
            print(json.dumps({
                "policy_sha256": policy, "scope": scope, "parallelism": parallelism, "machine_cores": cores,
                "modules": len(grouped), "tests": sum(len(v) for v in grouped.values()),
                "units": [{"unit_id": u.unit_id, "tests": len(u.test_ids), "cores": u.quota.cores, "exclusive": u.exclusive, "weight": round(u.weight, 1)} for u in sorted(units, key=lambda u: -u.weight)],
            }, ensure_ascii=False, indent=1))
            return 0
        out_dir = _out_dir(args)
        state_dir = args.state_dir or default_state_dir()
        records = _records_module()
        recorder = Recorder(out_dir=out_dir, mode=records.RE_EXECUTE, run_id=records.new_run_id(), policy=policy,
                            executor=records.executor_version([args.bytecode_helper]), environment=None,
                            currents=_plain_currents(records, units, start=args.start, pattern=args.pattern,
                                                     timeout=config.unit_timeout_seconds, reason="测试模式（run）不承接"))
        lock = hold_scheduler_lock(state_dir, wait_seconds=args.wait_seconds)
        scheduler = Scheduler(
            out_dir=out_dir, state_dir=state_dir, parallelism=parallelism, machine_cores=cores,
            machine_memory=machine_memory_mb(), config=config, policy=policy, start=args.start, recorder=recorder,
        )
        try:
            started = time.monotonic()
            _write_json(out_dir / "plan.json", {"policy_sha256": policy, "scope": scope, "parallelism": parallelism, "machine_cores": cores,
                                                "units": [{"unit_id": u.unit_id, "tests": list(u.test_ids), "cores": u.quota.cores, "exclusive": u.exclusive} for u in units]})
            print(f"调度：{'全量' if scope == 'full' else '部分模块'} {len(units)} 个单元（{sum(1 for u in units if u.exclusive)} 个独占），并行度 {parallelism}，"
                  f"整机 {cores} 核，策略 {policy[:12]}，记录 {out_dir}", file=sys.stderr, flush=True)
            bytecode, identity_memo = _prepare_shared_caches(args, out_dir, scheduler)
            formal = scheduler.run_parallel([u for u in units if not u.exclusive], "formal")
            formal += scheduler.run_alone([u for u in units if u.exclusive], "formal")
            diagnostic = scheduler.run_alone([o.unit for o in formal if not o.passed], "diagnostic")
            expected = {t for ids in grouped.values() for t in ids}
            summary = summarize(units, formal, diagnostic, expected, time.monotonic() - started, policy)
            summary["max_cores_in_use"] = scheduler.max_cores_in_use
            summary["machine_cores"] = cores
            summary["parallelism"] = parallelism
            summary["scope"] = scope
            summary["bytecode_cache"] = bytecode
            summary["identity_memo"] = identity_memo
            if bytecode["status"] == "failed":
                summary["status"] = "failed"
            _write_json(out_dir / "summary.json", summary)
            _print_summary(summary)
            return 0 if summary["status"] == "passed" else 1
        except BaseException:
            scheduler.abort()
            raise
        finally:
            os.close(lock)
    except ExecutorError as error:
        print(f"调度错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
