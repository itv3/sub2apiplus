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

子命令：``plan``（列出单元与策略摘要，不执行）、``run``（执行并汇总；退出码 0 全部通过、1 有失败、2 用法或配置错误）、
``run-unit``（内部：在当前进程跑给定测试 ID、写结果文件）、``acquire``／``release``（采集批次申请与归还整机资源）。
运行时与原 make 目标一样设置 ``CLAUDE_AST_TYPESCRIPT_MODULE``，从仓库根目录执行；调度进程与单元子进程一律不写字节码。
换了起点或模式（只跑一部分模块）时，调度配置里本次集合之外的登记项不参与规划；全量运行时登记项必须全部命中。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

DEFAULT_START = Path("tools/official_client_capture/tests")
DEFAULT_PATTERN = "test_*.py"
DEFAULT_CONFIG = Path("tools/ci/unit_executor.json")
DEFAULT_WEIGHTS = Path("tools/ci/capture_test_weights.json")
DEFAULT_DURATIONS = Path("tools/ci/capture_test_durations.json")
DEFAULT_BYTECODE_HELPER = Path("tools/arm64_capture_driver/driver/bytecode_cache.py")
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

    def _record(self, test: Any, outcome: str) -> None:
        test_id = test.id() if hasattr(test, "id") else str(test)
        started = self._started.get(test_id)
        previous = self.records.get(test_id)
        if previous is not None and previous["outcome"] in {"failed", "error"}:
            return  # 子测试或清理阶段的失败已经记下，不被后续的成功覆盖
        self.records[test_id] = {
            "outcome": outcome,
            "seconds": round(time.monotonic() - started, 3) if started is not None else None,
        }

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
        self._record(test, "skipped")

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

    @property
    def passed(self) -> bool:
        return (
            self.exit_code == 0
            and self.signal is None
            and not self.timed_out
            and self.result is not None
            and bool(self.result.get("successful"))
        )


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
        _write_json(tests_path, {"unit_id": unit.unit_id, "test_ids": list(unit.test_ids)})
        for stale in (result_path, record_path):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        argv = self.unit_argv or [sys.executable, str(Path(__file__).resolve()), "run-unit"]
        argv = [*argv, "--start", str(self.start), "--tests-file", str(tests_path), "--result", str(result_path)]
        env = {
            **os.environ,
            "UNIT_EXECUTOR_UNIT": unit.unit_id,
            "UNIT_EXECUTOR_KIND": kind,
            # 与 make 目标一致禁写字节码：绕过 make 直接运行（如只重跑一部分模块）时，测试也不会在树里留下 __pycache__
            # ——驱动清单等检查遇到它会报错。
            "PYTHONDONTWRITEBYTECODE": "1",
            # 单元内部并行度：按额度向上取整（至少 1）。
            "UNIT_EXECUTOR_CORES": str(max(1, math.ceil(unit.quota.cores))),
            "GOMAXPROCS": str(max(1, math.ceil(unit.quota.cores))),
        }
        actions = [
            (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            (os.POSIX_SPAWN_DUP2, 1, 2),
        ]
        pid = os.posix_spawn(argv[0], argv, env, file_actions=actions, setsid=True)
        self.running[pid] = Running(unit, pid, time.monotonic(), log_path, result_path, record_path, kind)
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
                if elapsed > self.config.unit_timeout_seconds and not item.timed_out:
                    item.timed_out = True
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGTERM)
                    self.event("timeout", unit=item.unit.unit_id, pid=pid)
                elif item.timed_out and elapsed > self.config.unit_timeout_seconds + 30:
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
                )
                # 单元执行记录：正式／诊断、调度策略版本、测试 ID 与逐个结论、退出原因与资源用量（E3-01 的承接以此为准）。
                _write_json(item.record_path, {
                    "schema_version": UNIT_RESULT_SCHEMA,
                    "unit_id": item.unit.unit_id,
                    "kind": item.kind,
                    "policy_sha256": self.policy,
                    "test_ids": list(item.unit.test_ids),
                    "tests": (outcome.result or {}).get("tests"),
                    "passed": outcome.passed,
                    "exit_code": outcome.exit_code,
                    "signal": outcome.signal,
                    "timed_out": outcome.timed_out,
                    "seconds": outcome.seconds,
                    "cpu_seconds": outcome.cpu_seconds,
                    "max_rss_mb": outcome.max_rss_mb,
                    "cores": item.unit.quota.cores,
                    "orphans": orphans,
                    "log": str(item.log_path),
                })
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


def summarize(units: list[Unit], formal: list[Outcome], diagnostic: list[Outcome], expected: set[str], elapsed: float, policy: str) -> dict[str, Any]:
    """全集核对与汇总：只看正式执行；诊断执行另列，不改结论。

    * 全集：各正式单元上报的测试 ID 并集必须等于 discover 全集——缺报、重复上报、全集之外的 ID（如 setUpClass
      失败的占位记录）、规划了却没执行的单元，任何一种都判失败；
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
        "diagnostic": [
            {"unit_id": o.unit.unit_id, "kind": o.kind, "passed": o.passed, "exit_code": o.exit_code, "signal": o.signal,
             "timed_out": o.timed_out, "seconds": o.seconds, "log": str(o.log_path)}
            for o in diagnostic
        ],
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
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--start", type=Path, default=DEFAULT_START)
        p.add_argument("--pattern", default=DEFAULT_PATTERN)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        p.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
        p.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
        p.add_argument("--parallel", type=int, default=0, help="同时在跑的单元个数上限；0 取调度配置的默认值，1 为逐个执行")
        p.add_argument("--cores", type=int, default=0, help="整机核数（默认 os.cpu_count()）")
        p.add_argument("--state-dir", type=Path, default=None)
        p.add_argument("--out-dir", type=Path, default=None)
        p.add_argument("--wait-seconds", type=float, default=7200.0, help="本机已有调度器在跑时最多等待多久")
        if name == "run":
            p.add_argument("--shared-caches", choices=("auto", "off"), default="auto",
                           help="字节码共享层与身份记忆化：auto 沿用环境里已有的、没有就在记录目录里新建；off 都不准备（单元按原环境运行，诊断用）")
            p.add_argument("--bytecode-helper", type=Path, default=DEFAULT_BYTECODE_HELPER, help="字节码共享层的预编译工具（测试与诊断用）")
            p.add_argument("--bytecode-source", type=Path, action="append", default=None, help="预编译进共享层的源码目录，可重复；默认 tools")
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
        config, weights, durations, parallelism, cores, grouped, units, policy, scope = _prepare(args)
        if args.command == "plan":
            print(json.dumps({
                "policy_sha256": policy, "scope": scope, "parallelism": parallelism, "machine_cores": cores,
                "modules": len(grouped), "tests": sum(len(v) for v in grouped.values()),
                "units": [{"unit_id": u.unit_id, "tests": len(u.test_ids), "cores": u.quota.cores, "exclusive": u.exclusive, "weight": round(u.weight, 1)} for u in sorted(units, key=lambda u: -u.weight)],
            }, ensure_ascii=False, indent=1))
            return 0
        # 执行记录目录：参数优先，其次环境变量 UNIT_EXECUTOR_OUT_DIR（门禁脚本把记录放到自己的日志旁边），最后临时目录。
        out_dir = args.out_dir or (Path(os.environ["UNIT_EXECUTOR_OUT_DIR"]) if os.environ.get("UNIT_EXECUTOR_OUT_DIR") else None) \
            or Path(tempfile.gettempdir()) / "unit-executor-runs" / time.strftime("%Y%m%dt%H%M%Sz", time.gmtime())
        out_dir.mkdir(parents=True, exist_ok=True)
        state_dir = args.state_dir or default_state_dir()
        lock = hold_scheduler_lock(state_dir, wait_seconds=args.wait_seconds)
        scheduler = Scheduler(
            out_dir=out_dir, state_dir=state_dir, parallelism=parallelism, machine_cores=cores,
            machine_memory=machine_memory_mb(), config=config, policy=policy, start=args.start,
        )
        try:
            started = time.monotonic()
            _write_json(out_dir / "plan.json", {"policy_sha256": policy, "scope": scope, "parallelism": parallelism, "machine_cores": cores,
                                                "units": [{"unit_id": u.unit_id, "tests": list(u.test_ids), "cores": u.quota.cores, "exclusive": u.exclusive} for u in units]})
            print(f"调度：{'全量' if scope == 'full' else '部分模块'} {len(units)} 个单元（{sum(1 for u in units if u.exclusive)} 个独占），并行度 {parallelism}，"
                  f"整机 {cores} 核，策略 {policy[:12]}，记录 {out_dir}", file=sys.stderr, flush=True)
            # 字节码共享层在任何测试单元之前准备，耗时计入总时长。
            if args.shared_caches == "off":
                bytecode = {"status": "off", "prefix": os.environ.get("PYTHONPYCACHEPREFIX") or None, "seconds": 0.0}
            else:
                bytecode = prepare_shared_bytecode(out_dir, scheduler, helper=args.bytecode_helper,
                                                   sources=list(args.bytecode_source or DEFAULT_BYTECODE_SOURCES))
            bytecode_note = {"inherited": "沿用环境里的前缀", "ready": f"预编译 {bytecode['seconds']:.1f} 秒", "failed": "预编译失败",
                             "off": "未准备（--shared-caches off）"}[bytecode["status"]]
            print(f"字节码共享层：{bytecode_note}（{bytecode['prefix'] or '不设前缀'}）", file=sys.stderr, flush=True)
            # 身份记忆化（E2-02）：本次运行的全部单元共用一个缓存目录，同一棵树的身份五摘要与评估器四项只算一次；
            # 键是整树逐文件摘要，测试改副本树后自然重算（见 codex_upgrade_tool_identity_policy）。
            identity_memo = os.environ.get(IDENTITY_MEMO_ENV) or None
            if args.shared_caches != "off" and not identity_memo:
                identity_memo = str((out_dir / "identity-memo").resolve())
                os.environ[IDENTITY_MEMO_ENV] = identity_memo
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
