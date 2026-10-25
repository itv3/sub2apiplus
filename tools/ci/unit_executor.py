#!/usr/bin/env python3
"""统一调度执行器（E2-01）：把采集工具测试拆成互相隔离的单元，在整机处理器与内存额度内并行执行，一次报全。

与 ``make test-capture-tools`` 原来的单进程 discover 同一加载语义：以 ``tools/official_client_capture/tests`` 为起点、
``test_*.py`` 为模式 discover，测试 ID 形如 ``test_x.Class.method``；子进程同样把起点目录放在 ``sys.path`` 首位，
按 ID 逐个加载。不同之处只在执行方式：

* **单元**：默认一个测试模块一个单元；调度配置里登记了拆块的重模块按测试方法拆成若干块（按逐测试耗时用最长
  优先法均衡）；登记为独占的测试（模块、类或单个方法）单独拆成「独占单元」。
* **隔离**：每个单元一个子进程，自成会话与进程组（``posix_spawn`` 的 ``setsid``），日志单独一份；一个单元被信号
  终止只影响它自己。单元结束后扫描它会话里的残留子孙进程，宽限期后终止并记录。
  **清理按后代子树而不只是按会话**（WP-R5）：单元里的测试自己另起会话的进程（嵌套的 ``run-unit`` 等）也算该单元的后代——
  按单元启动标记、在世期间记的后代名册和 ppid 子树找到，先冻结（SIGSTOP）再统一 SIGKILL，发信号前核对 pid＋启动时间防复用；
  中断（``abort``）、超时、预约中止、单元退出后的残留清理都走这一套，见下文「进程树快照与后代清理」。
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
  ``--shared-caches off`` 时两样都不准备，单元按原环境运行（诊断用）；``bytecode`` 只准备字节码，清除外部身份记忆目录。
* **执行记录与承接**（E3-01，记录格式、输入与判定见同目录 ``unit_records.py``）：每个单元每次执行（正式、诊断）回收
  时写一条不可变记录。``run-gates`` 给了记录库（``--record-store``）时记录同时入库；模式为全集通过
  （``--mode full-set-pass``）时，先在记录库里给每个单元找可承接的记录，只执行找不到的，承接的单元按原记录参加
  全集核对与门禁聚合。每次 ``run-gates`` 运行写一份清单（``unit-manifest.json``：逐单元本次执行还是承接、依据或
  不承接的原因）并自检，自检不通过结论判失败。``run``／``run-commands``（``make test-capture-tools``、
  ``make check-egress-spec``、pre-A3 单独签发）一律全部执行、不入库。
* **逐单元承接**（B5a-08，``--mode unit-inherit --unit-inherit-request <JSON>``，合同见同目录 ``unit_inherit.py``）：来源记录
  可来自多次运行、多个提交，逐单元判定，不能承接的单元只重跑它自己。排队前、取得调度锁后、发布清单前各复核一次（每次
  刷新校时与撤销查询快照、追加留证、重做授权与每条承接记录的核对）；排队前或锁后拒绝即整次重新执行，发布前拒绝即清单
  不发布。决策收据随清单发布进记录库。执行的单元可以带读集审计（``--audit-reads``，承接的单元以来源记录的读集为准）。
  ``--decide-only --unit-inherit-propose <目录>`` 写出当前上下文与来源集合提案，供两类批准签字。

* **失败单元补跑**（Codex 升级工具优化第 2、3 项，``run-gates`` 专用；调度配置的 ``retry.enabled`` 打开才生效，缺省关，规则与限制见同目录
  ``retry_policy.py``）：并行段与独占段都跑完、机器腾空之后，对“执行失败”类（命令正常退出但退出码非 0、测试报告了失败）的失败单元逐个单独补跑一次；
  通过则并入结论并标注“补跑通过”（汇总行 ``retry``、汇总 ``retry`` 段、运行清单 ``retries``，两次日志都留），同一次运行里补跑再红即判红。
  超时被杀、读集未闭合、环境失败（信号终止、命令找不到、没写结果文件）不补跑，保持原来的诊断重跑（不改结论）。累计规则：同一单元最近 5 轮只许补跑
  通过 1 次，第 2 次再红直接判红、不再补跑，按单元 ID 写进记录库的 ``retry-ledger/``。补跑尝试的记录记为“正式执行”（运行清单自检不用改）但永不作
  承接来源。后端三组 go test 由 ``go_test_packages.py`` 按包运行：补跑只补跑失败的包（失败用例能独立运行时才缩到 ``-run``），命令经环境变量
  ``UNIT_EXECUTOR_SIDECAR``／``UNIT_EXECUTOR_FIRST_SIDECAR`` 读写自己的结构化旁路结果（路径不进环境指纹与单元规格）。
* **运行绑定**（Codex 升级工具优化第 5 项）：``run-gates`` 每次运行结束（以及 ``--decide-only`` 判定时）在输出目录另写 ``binding.json``——调度策略摘要、
  平台环境摘要、执行器摘要、门禁集合摘要和输入清单摘要（逐单元的规格摘要、输入摘要与可否承接按单元 ID 排序后的摘要）。后台验证用它给通过的结论做“沿用绑定”，
  同树重部署后用 ``entry-gates.sh --bind-only`` 现算一份比对，不跑任何测试。它是独立文件，不进汇总、清单与判定文件。
* **核数偏差报告**（Codex 升级工具优化第 6 项）：``run-gates`` 每次运行结束在输出目录另写 ``core-deviation.json``——对本次真正执行的单元，比较声明核数（调度额度）与
  实测核数（单元 CPU 秒／墙钟秒），偏差超过 30% 的单元逐个列出（耗时不足 5 秒的不计，太短测不准），并给出整轮实测／声明的总比值。后台验证把它放进结论目录。
  它是独立文件，不进汇总、清单与判定文件，也不影响任何结论。
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
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextvars import ContextVar
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
# 单元级超时（配置 unit_timeouts）的上限：严格审计把重单元放大 4～17 倍（10-14 实测），统一 3600 秒会让它们必超时；
# 上限 4 小时是为了配置写错时挂死保护不至于形同虚设。
MAX_UNIT_TIMEOUT_SECONDS = 14400.0
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


_CONTROL_READ_AUDIT: ContextVar[Any] = ContextVar("unit_executor_control_read_audit", default=None)
# 声明级承接（declared-inputs/v1，步骤 5 工作包 WP3）的两个上下文变量，用法与 _CONTROL_READ_AUDIT 相同：只在一次 run-gates 调用内有值，
# 调用结束由 _run_gates 复位。
# * _DECLARED_PROBE：合同探测会话工厂（entry_gates.declared_probe）。_gate_currents 每次被调用都用它建会话、对含 declared_scope 的声明取
#   contract:* 条目——启动、锁后复核、发布前复核各探测一次，会话内同一事实只探一次。测试可以预先设成带假 runner 的工厂。
# * _DECLARED_RUN：本次运行的声明级上下文（_DeclaredRun），清单里有 declared 单元时才创建，否则一直是 None。其余所有新分支都先看它，或看「当前
#   输入里有没有 declared-inputs-scope 条目」——control 四链的声明与条目里两者都没有，永远走不进去。
_DECLARED_PROBE: ContextVar[Any] = ContextVar("unit_executor_declared_probe", default=None)
_DECLARED_RUN: ContextVar[Any] = ContextVar("unit_executor_declared_run", default=None)
# 失败单元补跑（优化第 2 项）：本次 ``run-gates`` 的调度配置里打开了补跑才为真。补跑策略 ``retry_policy.py`` 决定“什么算通过”，所以打开时
# 它的字节算进执行器版本（改了它，旧记录整体不可承接）；关闭时执行器版本与改造前逐字相同。
_RETRY_ACTIVE: ContextVar[bool] = ContextVar("unit_executor_retry_active", default=False)


def _executor_files(helper: Path) -> list[Path]:
    """本次执行与首尾核对共同冻结固定sidecar，不给普通默认执行追加合同。"""
    adapter = _CONTROL_READ_AUDIT.get()
    return [helper, *(adapter.sidecar_paths() if adapter is not None else []), *_declared_executor_files()]


def _retry_executor_files() -> list[Path]:
    """打开补跑的运行把补跑策略模块 retry_policy.py（与 go_test_packages.py）的字节也算进执行器版本；没打开（缺省）返回空，执行器版本与改造前逐字相同。"""

    here = Path(__file__).resolve().parent
    # 补跑策略与后端 go test 按包运行的包装器（它决定后端三组测试的“没跑到的包算失败”与按包补跑）：都是“什么算通过”的一部分。
    return [here / "retry_policy.py", here / "go_test_packages.py"] if _RETRY_ACTIVE.get() else []


# 补跑打开时执行器版本多算补跑策略与 Go 包装器两个文件。用“包一层”的写法而不是改上面那个函数的返回行：control 四链的回归对 _executor_files 的
# 既有行逐字比较（补跑关闭时返回值与改造前相同）。
_executor_files_without_retry = _executor_files


def _executor_files(helper: Path) -> list[Path]:   # noqa: F811 - 有意重绑定
    return [*_executor_files_without_retry(helper), *_retry_executor_files()]


def _declared_executor_files() -> list[Path]:
    """本次运行含声明级单元时，把采样器 declared_sampler.py 的字节也算进执行器版本（逐次采样与过滤器都靠它，它变了声明级记录整体重新产生）；
    没有声明级单元（普通运行、control 四链）返回空列表，执行器版本与改造前逐字相同。"""

    run = _DECLARED_RUN.get()
    return [Path(__file__).resolve().parent / "declared_sampler.py"] if run is not None and run.declared else []


def _records_module() -> Any:
    """同目录的 unit_records.py（仓库 tools/ci 与驱动副本里都和本文件同目录）：按路径加载，不受当前目录与 PYTHONPATH 影响。"""

    adapter = _CONTROL_READ_AUDIT.get()
    if adapter is not None:
        return adapter._records_module()
    name = "unit_records_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "unit_records.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 unit_records.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _unit_inherit_module() -> Any:
    """同目录的 unit_inherit.py（逐单元承接合同，B5a-08）：同样按路径加载，只在 ``--mode unit-inherit`` 时用到。"""

    name = "unit_inherit_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "unit_inherit.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 unit_inherit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _audit_module() -> Any:
    """同目录的 read_audit.py（读集审计，E3-04）：同样按路径加载。"""

    adapter = _CONTROL_READ_AUDIT.get()
    if adapter is not None:
        return adapter
    name = "read_audit_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "read_audit.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 read_audit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _retry_module() -> Any:
    """同目录的 retry_policy.py（失败单元补跑策略，优化第 2 项）：同样按路径加载，纯标准库。"""

    name = "retry_policy_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "retry_policy.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 retry_policy.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
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
    reservation_grace_seconds: float | None = 30.0
    reservation_stop_seconds: float = 5.0
    # 单元级超时覆盖：单元 ID（含 #N 分块、!exclusive）→ 秒。只放宽、不缩短，上限见 MAX_UNIT_TIMEOUT_SECONDS，
    # 登记必须在配置的 unit_timeouts_basis 里写明依据。超时随单元规格（spec）进入记录与输入比对，所以必须在生成
    # 正式来源之前定好，来源与后继之间不得再改，否则旧记录的规格对不上、无法承接。
    unit_timeouts: dict[str, float] = field(default_factory=dict)
    # 失败单元补跑（优化第 2 项）：配置里的 ``retry`` 段；没有这一段＝关闭，行为与改造前逐字相同。只对 ``run-gates`` 生效。
    retry: Any = None


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
    unit_timeouts: dict[str, float] = {}
    if "unit_timeouts" in payload and not isinstance(payload["unit_timeouts"], dict):
        # 键写了就必须是对象：[]、0、false、null 过去会被下面的 ``or {}`` 悄悄当成空表接受，登记过的超时表就这样无声失效；只有缺省（没有这个键）才是空表。
        raise ExecutorError("unit_timeouts 必须是对象（单元 ID → 秒）")
    raw_timeouts = payload.get("unit_timeouts") or {}
    if not isinstance(raw_timeouts, dict):
        raise ExecutorError("unit_timeouts 必须是对象（单元 ID → 秒）")
    basis = payload.get("unit_timeouts_basis")
    if raw_timeouts and not (isinstance(basis, str) and basis.strip()):
        raise ExecutorError("登记了 unit_timeouts 就必须在 unit_timeouts_basis 里写明依据（公式与数据来源）")
    for unit_id, value in raw_timeouts.items():
        if not isinstance(unit_id, str) or not unit_id.strip():
            raise ExecutorError(f"单元超时登记的单元 ID 非法：{unit_id!r}")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_UNIT_TIMEOUT_SECONDS:
            # 超大整数（如 10**400）转不成 float，下面的 math.isfinite 会抛 OverflowError 而不是 ExecutorError；它们本来就超过上限，在这里按范围拒绝
            # （合法的值必须在默认超时与 MAX_UNIT_TIMEOUT_SECONDS 之间，绝对值超过上限的一律不合法）。报错里不带默认超时的数值：默认超时本身若也是超大整数，
            # 格式化它又会抛 OverflowError。
            raise ExecutorError(f"单元超时必须大于默认超时且不超过 {MAX_UNIT_TIMEOUT_SECONDS:.0f} 秒（只放宽、不缩短）：{unit_id}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ExecutorError(f"单元超时必须是有限秒数：{unit_id}")
        if not float(timeout) < value <= MAX_UNIT_TIMEOUT_SECONDS:
            raise ExecutorError(f"单元超时必须大于默认 {float(timeout):.0f} 秒且不超过 {MAX_UNIT_TIMEOUT_SECONDS:.0f} 秒"
                                f"（只放宽、不缩短）：{unit_id}")
        unit_timeouts[unit_id] = float(value)
    reservation_grace = payload.get("reservation_grace_seconds", 30)
    reservation_stop = payload.get("reservation_stop_seconds", 5)
    for name, value in (("reservation_grace_seconds", reservation_grace), ("reservation_stop_seconds", reservation_stop)):
        if name == "reservation_grace_seconds" and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ExecutorError(name + " 必须是有限非负秒数；宽限期可用 null 关闭中止")
    try:
        retry = _retry_module().parse_config(payload.get("retry")) if "retry" in payload else None
    except _retry_module().RetryPolicyError as error:
        raise ExecutorError(f"调度配置的补跑段非法：{error}") from error
    return ExecutorConfig(
        default_parallelism=parallelism,
        default_quota=quota(payload.get("default_quota") or {}, "default_quota"),
        quotas={module: quota(value, module) for module, value in (payload.get("quotas") or {}).items()},
        splits=splits,
        exclusive=tuple(exclusive),
        unit_timeout_seconds=float(timeout),
        orphan_grace_seconds=float(grace),
        raw=payload,
        reservation_grace_seconds=float(reservation_grace) if reservation_grace is not None else None,
        reservation_stop_seconds=float(reservation_stop),
        unit_timeouts=unit_timeouts,
        retry=retry,
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
    # 命令默认排空等待；只有明确可重跑的隔离检查才允许预约中止。
    reservation_restartable: bool = False


COMMANDS_SCHEMA = "unit-executor-commands/v1"
COMMANDS_SUMMARY_SCHEMA = "unit-executor-commands-summary/v1"
# inputs／inheritable／not_inheritable_reason 是门禁清单给 E3-01 承接用的声明（输入范围、是否可承接与原因），不影响执行。
_COMMAND_FIELDS = {"unit_id", "argv", "cwd", "env", "cores", "memory_mb", "exclusive", "timeout_seconds", "weight",
                   "inputs", "inheritable", "not_inheritable_reason", "input_scope", "reservation_restartable"}


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
    if not isinstance(item.get("reservation_restartable", False), bool):
        raise ExecutorError(f"命令单元的预约重派标记非法：{unit_id}")
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
        reservation_restartable=item.get("reservation_restartable", False),
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
    if config.unit_timeouts:
        if full_set:
            # 与拆块、独占名单同一纪律：单元改名或改拆块数后，登记的超时不能静默落空。
            missing = sorted(set(config.unit_timeouts) - {unit.unit_id for unit in units})
            if missing:
                raise ExecutorError("单元超时登记了规划里不存在的单元（测试改名或改拆块数后要同步配置）：" + "、".join(missing))
        units = [replace(unit, timeout_seconds=config.unit_timeouts[unit.unit_id]) if unit.unit_id in config.unit_timeouts else unit
                 for unit in units]
    return units


# ---------------------------------------------------------------------------
# 单元子进程：在当前进程跑给定测试 ID
# ---------------------------------------------------------------------------


class _RecordingResult(unittest.TextTestResult):
    """在 unittest 原有输出之外，逐个记录测试 ID 的结论与耗时。"""

    def __init__(self, *args: Any, failure_path: Path | None = None, unit_id: str = "", result_writer: Any = None,
                 preserve_first_failure: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict[str, Any]] = {}
        self._started: dict[str, float] = {}
        self.failure_path = failure_path
        self.unit_id = unit_id
        self.result_writer = result_writer or _write_json
        self.preserve_first_failure = preserve_first_failure
        self.failure_persisted = False

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
        if (self.failure_path is not None and outcome in {"failed", "error", "unexpected_success"}
                and not (self.preserve_first_failure and self.failure_persisted)):
            # 在输出失败标志之前持久化。成功路径不增加逐测试写盘；中止不能抹去已观察的失败。
            self.result_writer(self.failure_path, {"schema_version": UNIT_RESULT_SCHEMA, "unit_id": self.unit_id,
                                                  "tests": self.records, "tests_run": self.testsRun,
                                                  "successful": False, "partial": True})
            self.failure_persisted = True

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self._record(test, "failed")
        super().addFailure(test, err)

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        self._record(test, "error")
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", str(reason))

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        self._record(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:  # noqa: N802
        if err is not None:
            self._record(test, "failed" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)


def run_unit(start: Path, tests_file: Path | None, result_path: Path, *, runtime_tests_json: str | None = None) -> int:
    # 导入路径与 discover 一致：起点目录在首位（discover 以它为顶层），其后是当前目录（仓库根）。
    _import_path_like_unittest_main()
    start_abs = str(Path(start).resolve())
    if start_abs not in sys.path:
        sys.path.insert(0, start_abs)
    writer = _write_json
    if runtime_tests_json is None:
        payload = json.loads(Path(tests_file).read_text(encoding="utf-8"))
    else:
        runtime = _audit_module()._runtime_module()
        try:
            payload = runtime.runtime_test_payload(json.loads(runtime_tests_json), start=start, result=result_path)
        except (RuntimeError, ValueError, TypeError, KeyError) as error:
            raise ExecutorError(str(error)) from error
        writer = runtime.write_test_output
    test_ids = list(payload["test_ids"])
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for test_id in test_ids:
        name = test_id[len(FAILED_IMPORT_PREFIX):] if test_id.startswith(FAILED_IMPORT_PREFIX) else test_id
        suite.addTests(loader.loadTestsFromName(name))
    runner = unittest.TextTestRunner(verbosity=1, resultclass=lambda *args, **kwargs: _RecordingResult(
        *args, failure_path=result_path.with_suffix('.observed-failure.json'), unit_id=payload['unit_id'], result_writer=writer,
        preserve_first_failure=runtime_tests_json is not None, **kwargs))
    result = runner.run(suite)
    assert isinstance(result, _RecordingResult)
    writer(
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
        if (request is None or grant is None or grant.get("owner") != request.get("owner")
                or grant.get("request_sha256") != _sha256(request)):
            return None
        return grant

    def grant(self, by: str, *, expected: dict[str, Any] | None = None) -> bool:
        request = self.current()
        if expected is not None and request != expected:
            return False
        if request is not None and self.granted() is None:
            _write_json(self.grant_path, {"schema_version": RESERVATION_SCHEMA, "owner": request["owner"],
                                         "request_sha256": _sha256(request), "granted_at_utc": _utc_now(), "granted_by": by})
        return request is not None


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
            json.dump({"schema_version": RESERVATION_SCHEMA, "owner": owner, "owner_pid": owner_pid,
                       "request_id": os.urandom(16).hex(), "requested_at_utc": _utc_now()}, handle)
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


# ---------------------------------------------------------------------------
# 进程树快照与后代清理（WP-R5）
# ---------------------------------------------------------------------------
#
# 为什么：单元是 ``posix_spawn(setsid=True)`` 起的，每个单元一个会话，原来的清理只按会话杀。单元里的测试若自己再起
# 嵌套的 ``unit_executor.py run-unit`` 并 ``start_new_session``（``test_claude_real_delivery_integration`` 就是这样），
# 嵌套进程的会话号是它自己，不在单元会话里：后台 stop 之后它成了 PPID=1 的孤儿，继续持锁运行（10-14、10-15 实测）。
#
# 怎么找：属于某个单元的进程 = 单元根进程 + 单元会话成员 + 环境里带本单元启动标记的进程（``UNIT_EXECUTOR_RUN_TAG``，
# 子进程默认继承环境）+ 调度器在单元在世期间记下的后代名册（pid＋启动时间，兜底「环境被清空又先成了孤儿」的后代），
# 再取这些进程的 ppid 子树。**必须在杀任何进程之前取快照**：父进程一死，孙辈就改挂到 init，ppid 链断了。
#
# 怎么杀：先 SIGSTOP 冻结、反复取快照直到不再出现新进程（防止它们在被杀的过程中继续 fork），再统一 SIGKILL。冻结顺带
# 钉住 pid：停住的进程不会自己退出，pid 在 SIGKILL 前不会被复用。每次发信号前都核对「pid＋启动时间」与快照一致，
# 不一致（pid 已被复用）就不动。没有 /proc 时（macOS 本机测试）进程表走 ``ps``。

RUN_TAG_ENV = "UNIT_EXECUTOR_RUN_TAG"
# 单元在世期间每隔多久刷新一次后代名册（秒）。
LEDGER_REFRESH_SECONDS = 2.0
# 单元超时后先发 SIGTERM，再过这么久仍在就整棵子树 SIGKILL。
TIMEOUT_KILL_GRACE_SECONDS = 30.0
# 冻结阶段最多取几轮快照（每轮冻结新出现的进程）；正常一两轮就收敛。
FREEZE_ROUNDS = 8
# 冻结之后、SIGKILL 之前要再核对一次「pid＋启动时间」。读进程信息偶发失败（fd 耗尽、内存紧张、读到乱码、ps 超时）不等于「进程没了」：
# 在这个次数内重试（轮与轮之间睡 KILL_VERIFY_RETRY_SECONDS 秒）；仍核对不了就对它发 SIGCONT 解冻并记告警，绝不把进程留在 STOPPED 里。
KILL_VERIFY_ATTEMPTS = 5
KILL_VERIFY_RETRY_SECONDS = 0.1
# abort 回收各单元根进程最多等多久（秒）；根进程已被 SIGKILL，正常毫秒级回收，这个上限只防不可中断睡眠一类的极端情形无限阻塞。
ABORT_REAP_SECONDS = 10.0
_TAG_PREFIX = (RUN_TAG_ENV + "=").encode()
# 进程文件系统根（测试用假的 /proc 树覆盖 Linux 分支；生产永远是 /proc）。
PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcInfo:
    """进程表的一行。``start`` 是启动时间的不透明标识（Linux：/proc stat 的 starttime 刻度；后备：ps 的 lstart 文本），
    只用来和快照比较是不是同一个进程（防 pid 复用），不做时间运算。"""

    pid: int
    ppid: int
    sid: int | None
    start: str
    state: str


def _has_proc() -> bool:
    return (PROC_ROOT / "self" / "stat").is_file()


def _process_table_proc() -> dict[int, ProcInfo]:
    table: dict[int, ProcInfo] = {}
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[-1].split()
            table[int(entry.name)] = ProcInfo(int(entry.name), int(fields[1]), int(fields[3]), fields[19], fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return table


def _process_table_ps_strict() -> dict[int, ProcInfo]:
    """没有 /proc 时（macOS）的后备：``ps`` 列出全部进程，会话号用 ``os.getsid`` 补。``ps`` 起不来、超时或没有输出就报错
    （OSError／SubprocessError），不把「读不到」伪装成「一个进程都没有」——冻结后的复核要靠它区分「进程没了」和「暂时核对不了」。"""

    completed = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,stat=,lstart="], capture_output=True, text=True,
                               env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}, stdin=subprocess.DEVNULL, timeout=30)
    if completed.returncode != 0 and not completed.stdout.strip():
        raise OSError(f"ps 失败（退出码 {completed.returncode}）：{completed.stderr.strip()[:200]}")
    table: dict[int, ProcInfo] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        try:
            sid: int | None = os.getsid(pid)
        except OSError:
            sid = None
        table[pid] = ProcInfo(pid, ppid, sid, " ".join(parts[3].split()), parts[2])
    return table


def _process_table_ps() -> dict[int, ProcInfo]:
    """同上，但 ``ps`` 读不到时返回空表（取快照用：空表＝没有可清理的，调用方走按会话和进程组的兜底）。"""

    try:
        return _process_table_ps_strict()
    except (OSError, subprocess.SubprocessError):
        return {}


def process_table() -> dict[int, ProcInfo]:
    """当前全部进程（pid → 一行）。读不到时返回空表：调用方按「没有可清理的」处理，并走原来的按会话清理兜底。"""

    return _process_table_proc() if _has_proc() else _process_table_ps()


def descendants_of(table: dict[int, ProcInfo], roots: Any) -> set[int]:
    """根进程（表里存在的）连同按 ppid 往下的全部后代，不论它们后来是否另起了会话或进程组。"""

    children: dict[int, list[int]] = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    seen: set[int] = set()
    stack = [pid for pid in roots if pid in table]
    while stack:
        pid = stack.pop()
        if pid not in seen:
            seen.add(pid)
            stack.extend(children.get(pid, ()))
    return seen


def protected_pids(table: dict[int, ProcInfo]) -> set[int]:
    """绝不能动的进程：调用方自己和它的全部祖先（沿 ppid 往上）。"""

    protected = {os.getpid()}
    pid = os.getpid()
    while pid in table and table[pid].ppid > 0 and table[pid].ppid not in protected:
        pid = table[pid].ppid
        protected.add(pid)
    return protected


def tag_index(table: dict[int, ProcInfo]) -> dict[str, set[int]]:
    """环境里带启动标记的进程，按标记链的每一段建索引：段 → 进程集合。标记链是「外层:内层:…」（单元里再起的调度器把自己的标记
    接在外层标记后面），所以外层按自己那一段也认得出内层单元的进程。Linux 读 /proc/<pid>/environ（同属主才读得到）；没有 /proc
    时用 ``ps -E`` 尽力而为。一次扫描服务全部单元，不要对每个单元各扫一遍。"""

    index: dict[str, set[int]] = {}

    def add(pid: int, value: str) -> None:
        for token in value.split(":"):
            if token:
                index.setdefault(token, set()).add(pid)

    if _has_proc():
        for pid, info in table.items():
            if info.state[:1] in ("Z", "X"):
                continue
            try:
                data = (PROC_ROOT / str(pid) / "environ").read_bytes()
            except OSError:
                continue
            for item in data.split(b"\0"):
                if item.startswith(_TAG_PREFIX):
                    add(pid, item[len(_TAG_PREFIX):].decode("ascii", "replace"))
                    break
        return index
    try:
        completed = subprocess.run(["ps", "-A", "-ww", "-E", "-o", "pid=,command="], capture_output=True, text=True,
                                   env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return index
    pattern = re.compile(r"(?:^|\s)" + re.escape(RUN_TAG_ENV) + r"=(\S+)")
    for line in completed.stdout.splitlines():
        head, _, rest = line.strip().partition(" ")
        match = pattern.search(rest)
        if head.isdigit() and int(head) in table and match:
            add(int(head), match.group(1))
    return index


def tagged_pids(token: str, table: dict[int, ProcInfo]) -> set[int]:
    """环境里带标记 ``token``（标记链中的任一段）的进程。"""

    return set(tag_index(table).get(token, ()))


def _read_start(pid: int) -> tuple[str, str] | None:
    """单个进程现在的 (启动时间标识, 状态)。进程已经不在返回 ``None``；读不了（fd 耗尽、内存紧张、读到乱码或截断的 stat、
    ``ps`` 失败／超时）抛 OSError／ValueError／SubprocessError——调用方据此区分「确实没了」和「暂时核对不了」，
    故障注入测试也从这里下手。"""

    if _has_proc():
        try:
            text = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError, NotADirectoryError):
            return None   # 进程已退出（ENOENT／ESRCH），不是读故障
        fields = text.rsplit(")", 1)[-1].split()
        if len(fields) <= 19:
            raise ValueError(f"/proc/{pid}/stat 字段不足：{text[:80]!r}")
        return fields[19], fields[0]
    current = _process_table_ps_strict().get(pid)
    return None if current is None else (current.start, current.state)


def _identity(pid: int, info: ProcInfo) -> str:
    """pid 现在和快照里那个进程的关系：``same``（启动时间一致、没成僵尸）、``gone``（已不在或已成僵尸）、``other``（pid 已被
    另一个进程复用）、``unknown``（读不了，暂时核对不了）。"""

    try:
        current = _read_start(pid)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"
    if current is None or current[1][:1] in ("Z", "X"):
        return "gone"
    return "same" if current[0] == info.start else "other"


def _same_process(pid: int, info: ProcInfo) -> bool:
    """pid 现在还是快照里的那个进程：启动时间一致、没有成僵尸。"""

    return _identity(pid, info) == "same"


def _signal_if_same(pid: int, info: ProcInfo, sig: int) -> bool:
    if not _same_process(pid, info):
        return False
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _abbreviate(text: str, limit: int = 300) -> str:
    """过长的命令行保留开头和结尾（``python -c <很长的代码> 参数…`` 的参数在最后，掐掉尾巴就认不出是谁了）。"""

    return text if len(text) <= limit else text[:200] + " … " + text[-(limit - 203):]


def command_lines(pids: list[int], limit: int = 20) -> dict[str, str]:
    """前 ``limit`` 个进程的命令行（过长的保留头尾，共约 300 字符），诊断留证用；读不到的略过。Linux 读 /proc/<pid>/cmdline，
    没有 /proc 时用 ps。"""

    found: dict[str, str] = {}
    chosen = pids[:limit]
    if _has_proc():
        for pid in chosen:
            try:
                raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
            except OSError:
                continue
            found[str(pid)] = _abbreviate(raw.replace(b"\0", b" ").decode("utf-8", "replace").strip())
        return found
    try:
        completed = subprocess.run(["ps", "-ww", "-o", "pid=,command=", "-p", ",".join(str(pid) for pid in chosen)], capture_output=True,
                                   text=True, env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"}, stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return found
    for line in completed.stdout.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head.isdigit():
            found[head] = _abbreviate(rest.strip())
    return found


def kill_frozen(frozen: dict[int, ProcInfo], warnings: list[str] | None = None) -> list[int]:
    """杀掉已经 SIGSTOP 冻结的进程，保证**不会有进程留在 STOPPED 里**。返回实际发过 SIGKILL 的 pid。

    杀之前再核对一次身份。读进程信息偶发失败（``unknown``）不能当成「进程没了」：在 ``KILL_VERIFY_ATTEMPTS`` 轮内重试；仍核对不了的
    对它发 SIGCONT 解冻并写进 ``warnings``——不在这里盲杀，因为核对不了身份时按「pid＋启动时间」才发信号的保护就失效了；
    解冻后它仍在 ``find`` 的结果里，调用方后面的扫荡轮次还会再核对、再杀。核对出 ``other``（冻结的瞬间 pid 已被复用，
    比如「核对通过」与「发 SIGSTOP」之间原进程退出、pid 被别的进程拿走）说明被冻住的是无辜进程，同样发 SIGCONT 撤销冻结。
    第二次 Ctrl-C 等中断落在这里：先把其余冻结进程处理完，最后再把中断抛出去，已发出的 SIGKILL 不丢、也不留停住的进程。"""

    warnings = [] if warnings is None else warnings
    killed: list[int] = []
    interrupted: BaseException | None = None
    pending = dict(frozen)
    for _ in range(KILL_VERIFY_ATTEMPTS):
        retry: dict[int, ProcInfo] = {}
        for pid, info in pending.items():
            try:
                state = _identity(pid, info)
                if state == "same":
                    try:
                        os.kill(pid, signal.SIGKILL)
                        killed.append(pid)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        warnings.append(f"进程 {pid} 已冻结但无权发 SIGKILL（权限不足），仍处于停住状态")
                elif state == "other":
                    _unfreeze(pid)
                    warnings.append(f"冻结的 pid {pid} 在核对时已是另一个进程（pid 被复用），已发 SIGCONT 撤销冻结")
                elif state == "unknown":
                    retry[pid] = info
            except BaseException as error:   # noqa: BLE001 - 见函数说明：中断先记下，处理完其余进程再抛
                interrupted = interrupted or error
                retry[pid] = info
        pending = retry
        if not pending:
            break
        try:
            time.sleep(KILL_VERIFY_RETRY_SECONDS)
        except BaseException as error:   # noqa: BLE001
            interrupted = interrupted or error
    for pid in pending:
        _unfreeze(pid)
        warnings.append(f"进程 {pid} 冻结后连续 {KILL_VERIFY_ATTEMPTS} 次核对不了身份，已发 SIGCONT 解冻（没有杀；请核对它是否是遗留进程）")
    if interrupted is not None:
        raise interrupted
    return killed


def _unfreeze(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGCONT)


def kill_processes(find: Any, warnings: list[str] | None = None) -> list[int]:
    """终止 ``find()`` 给出的进程（pid → ProcInfo）：先反复冻结直到不再出现新进程，再统一 SIGKILL（见 ``kill_frozen``，
    不会留下停住的进程），最后再扫荡几次。返回实际发过 SIGKILL 的 pid；核对不了身份而改发 SIGCONT 的写进 ``warnings``。
    ``find`` 每轮重新取快照，所以冻结期间新 fork 出来的后代下一轮就看得到。"""

    frozen: dict[int, ProcInfo] = {}
    try:
        for _ in range(FREEZE_ROUNDS):
            fresh = {pid: info for pid, info in find().items() if pid not in frozen}
            if not fresh:
                break
            for pid, info in fresh.items():
                if _signal_if_same(pid, info, signal.SIGSTOP):
                    frozen[pid] = info
    finally:
        # 无论冻结阶段出什么错，冻住的都要杀掉（或至少解冻），不能把停住的进程留在后台。
        killed = kill_frozen(frozen, warnings)
    sent = set(killed)
    for _ in range(3):
        stray = {pid: info for pid, info in find().items() if pid not in sent}
        if not stray:
            break
        for pid, info in stray.items():
            if _signal_if_same(pid, info, signal.SIGKILL):
                sent.add(pid)
                killed.append(pid)
        time.sleep(0.05)
    return sorted(killed)


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
    interruption: dict[str, Any] | None = None
    stop_started: float | None = None
    resumed_from: list[dict[str, Any]] = field(default_factory=list)
    # 本次启动独有的随机标记（经环境变量 UNIT_EXECUTOR_RUN_TAG 下发，子进程继承）；清理时靠它认出不在 ppid 链上的孤儿。
    tag: str = ""
    # 后代名册：单元在世期间见过的后代 pid → 启动时间标识（见 Scheduler._remember）。
    known: dict[int, str] = field(default_factory=dict)
    # 补跑尝试（kind == "retry"）才有：首次尝试的记录与日志摘要、失败类别，写进补跑记录（``retry_of``）。
    retry_of: dict[str, Any] | None = None


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
    def execution_passed(self) -> bool:
        if self.exit_code != 0 or self.signal is not None or self.timed_out:
            return False
        return bool(self.unit.command) or (self.result is not None and bool(self.result.get("successful")))

    @property
    def passed(self) -> bool:
        if not self.execution_passed:
            return False
        audit = self.extra.get("read_audit")
        if audit is not None and audit.get("status") == "reexecute_required" and self.extra.get("reexecute_only") is True:
            return True
        if (audit is not None and audit.get("status") == "incomplete" and audit.get("coverage_scope") == DECLARED_COVERAGE_SCOPE
                and audit.get("undeclared_count") == 0):
            # 声明级逐次采样只因证据不完整（偶发的乱码行、缺 strace 等）没能证明读集覆盖：该次记录不可承接（Recorder 写 inheritable=False），
            # 但命令本身的通过／失败结论不变，整次运行不因此失败。
            return True
        if audit is not None and (audit.get("undeclared_count") != 0 or audit.get("status") != "passed"):
            return False
        return True


def unit_spec(unit: Unit, *, start: Path | None, pattern: str | None, timeout_seconds: float) -> dict[str, Any]:
    """单元规格（E3-01 承接比对的一项）：决定「这个单元执行的是什么」的全部字段；预计秒数只影响派发顺序，不算。
    只决定缓存位置的环境变量（字节码前缀、身份记忆目录）也不算：它们不影响结果，环境指纹同样不计（E3-02，pre-A3 场景
    单元带着这两个变量，生产字节码共享层在与不在不能让场景承接不了）。"""

    cache_only = _records_module().ENV_CACHE_ONLY
    env = {key: value for key, value in dict(unit.env).items() if key not in cache_only}
    common = {"unit_id": unit.unit_id, "env": env, "cores": unit.quota.cores, "memory_mb": unit.quota.memory_mb,
              "exclusive": unit.exclusive, "timeout_seconds": unit.timeout_seconds or timeout_seconds,
              "reservation_restartable": not unit.command or unit.reservation_restartable}
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
        if "read_audit" in outcome.extra:
            audit = json.loads(json.dumps(outcome.extra["read_audit"]))
            if self.store is not None and audit.get("trace"):
                trace = audit["trace"]
                trace["stored"] = str(self.store.put_log(Path(trace["path"]), trace["sha256"]))
            body["read_audit"] = audit
        if outcome.extra.get("declared_contracts"):
            # 声明级承接的运行前核验凭证（进 record_sha256，不进 inputs_sha256）；只有 declared 记录才有这个键，普通记录与 control 记录的键集合不变。
            body["declared_contracts"] = outcome.extra["declared_contracts"]
        if outcome.extra.get("declared_not_inheritable"):
            # 前置核验未通过、逐次采样的证据不完整、缺 strace：这一次执行的结论照常，但记录永不成为承接来源。
            body.update(inheritable=False, not_inheritable_reason=outcome.extra["declared_not_inheritable"])
        if item.resumed_from:
            body["resumed_from"] = item.resumed_from
        sidecar = item.log_path.parent.parent / "units" / (item.log_path.name.removesuffix(".log") + ".sidecar.json")
        if sidecar.is_file():
            # 命令单元写的结构化旁路结果（go test 按包结果等）：路径与摘要进记录，补跑通过的证据可追到首次尝试的包清单。
            body["sidecar"] = {"path": str(sidecar), "sha256": self.records.file_sha256(sidecar), "bytes": sidecar.stat().st_size}
        if item.kind == "retry":
            # 补跑尝试（优化第 2 项）：记录仍记为“正式执行”（运行清单自检只认正式执行记录），但永不作承接来源，并写明它补的是哪次尝试。
            # 补跑通过是“抖动后的通过”，不是干净的一次通过，所以不让后续运行当承接依据。
            body.update(kind="formal", attempt="retry", retry_of=item.retry_of, inheritable=False,
                        not_inheritable_reason="补跑尝试的记录不作承接来源（首次尝试已失败）")
        if outcome.extra.get("observed_failure"):
            body.update(observed_failure=outcome.extra["observed_failure"], inheritable=False,
                        not_inheritable_reason="预约中止前已登记测试失败，正式失败结论保留")
        if outcome.extra.get("reservation_interruption"):
            # 中止证据保留原身份，但不进入正式通过／失败或诊断集合，永不可承接。
            body.update(kind="reservation-aborted", original_kind=item.kind, passed=False, inheritable=False,
                        not_inheritable_reason="整机预约中止，须完整重派", tests=None,
                        reservation_interruption=outcome.extra["reservation_interruption"])
        record = self.records.seal_record(body)
        _write_json(item.record_path, record)
        path = self.store.put_record(record) if self.store is not None else item.record_path
        outcome.extra.update(disposition="executed", record_sha256=record["record_sha256"], record_path=str(path))
        return record


# ---------------------------------------------------------------------------
# 声明级承接（declared-inputs/v1）：执行器一侧的接线（步骤 5 工作包 WP3）
# ---------------------------------------------------------------------------
# 背景：命令单元（backend 的 go test、frontend 的检查、egress-spec 的整条 make 目标……）原来声明「整个仓库四段＋HEAD」，换一次提交全部失效。
# 声明级承接把命令的输入换成「它真实读到的目录与文件＋外部输入合同」，再用一份采样证据（strace 读集）证明读集都落在声明范围里。范围校验、
# 覆盖检查 C1～C7 与消费者核验在 unit_records.py，登记表自检与合同探测在 entry_gates.py，采样器在 declared_sampler.py；本节是执行器要做的四件事：
# 1. 算当前事实时并入合同条目（_gate_currents → _declared_add_contracts）；
# 2. 单元启动前做前置核验，拍下运行前凭证（declared_contract_preflight）；
# 3. 单元回收时产出声明级读集结果（ReadAuditor 的 declared 分支）：α'（缺省）读登记表证据、零运行时开销；β（--declared-sample）每次执行都带
#    strace 采样、用本次采样证明读集仍在声明范围内；
# 4. 汇总（_declared_run_summary）：登记数、审计通过数、消费者接受数、实际承接数四个数分开统计，每个被重跑的单元写明原因与耗时。
#
# 触发纪律（control 四链的行为必须逐字不变）：所有新分支只由「当前输入里有 declared-inputs-scope 条目」或「声明里有 declared_scope 键」触发，
# control 的声明与条目里两者都没有；本节任何函数都不调用 _audit_module()、不导入 read_audit——覆盖检查、证据解析、凭证核验全部用 unit_records
# 里的纯函数，采样用 declared_sampler。
#
# 设计盲区（已知、有意披露，不是遗漏）：采样的网络检查只覆盖 connect。采样器给 strace 的跟踪集合（declared_sampler.STRACE_OPTIONS 的 trace=）里
# 有 connect，没有 sendto、sendmsg、sendmmsg，所以「UDP 未连接套接字」直接 sendto／sendmsg／sendmmsg 往外部地址发包是看不到的，C5 网络规则对这条
# 路径无效。对已知的用法这个盲区不成立：TCP 外连一定先经 connect；glibc 与 Go 的 DNS 解析（不管走 UDP 还是 TCP）都先对解析服务器 connect，
# 目的地址会被收集到。也就是说漏掉的只可能是「自己 socket() 之后从不 connect、直接 sendto／sendmsg 的程序」；登记的单元里有没有这类程序，只能靠
# 评审它们的实现来判断，采样本身证明不了（它看不到这类调用）。以后登记新的 declared 单元时要想到这一点；要补上这个盲区得改采样器的跟踪集合
# （它的字节进执行器版本，改了要重采），不在执行器一侧修。

DECLARED_SCOPE_ENTRY = "declared-inputs-scope"
DECLARED_COVERAGE_SCOPE = "declared-inputs/v1"
# 逐次采样（β）覆盖不通过时的两类处理（调度者 10-14 定案；ARM64 真实采样发现 strace 在冷缓存的 Go 编译密集运行里约每三百万行有一次单行乱码
# 系统调用，原因未明）：
# * 未覆盖明细里的规则**全部**属于本集合——采样输出流没有完整结束（C7-stream）、出现不可容忍的未解析调用（C7-unresolved，偶发的乱码行）、strace
#   一行都没跟踪到（C7-lines）——说明只是证据不完整、没能证明覆盖，并没有发现违规：该次记录 status=incomplete、inheritable=False，命令本身的
#   通过／失败结论不变，整次运行不因此失败；
# * 出现集合之外的任何规则就是 failed，记录判失败、整次运行判失败（fail-closed，不静默降级）：C0～C6 的真访问与违规；C7-command（证据不属于这条
#   命令）；C7-format（执行器自己组装的证据格式不对，是实现缺陷，要响亮失败）；C7-invalid（组装的证据 valid 恒为真，出现说明实现有误）；C7-exit
#   （命令本身退出码非零，单元本来就失败）。混合（既有集合内又有集合外）一律 failed。
# 新增的覆盖规则默认不在本集合里，必须先评审归类（test_ci_declared_inputs_executor 钉住这个集合与 declared_coverage 的规则词表）。
# 步骤 5 WP3b：集合里多了 C7-detached（采样器丢弃的 strace 标记行总数超过上限——个位数是 Go 运行时线程被杀或退出的正常现象，大面积出现说明流被截断或
# 大批线程消失，读集可能不完整）。它和 C7-stream 同类：说明证据不足以证明覆盖，并没有发现违规，所以也只让该次记录不可承接。规则本身由记录层的
# declared_coverage 产生（C7-detached，标记行超过 64 条触发，WP1d 已合入）；集合里的每个名字都必须是 declared_coverage 真有的规则，测试钉住这一点。
DECLARED_INCOMPLETE_RULES = frozenset({"C7-stream", "C7-unresolved", "C7-lines", "C7-detached"})
# 合同探测会话要「看见」的 go 环境变量（与 entry_gates.DECLARED_GO_ENV_KEYS 同值，测试钉住）：go env 的生效值受它们影响，而离线值是写在单元清单的
# env 里的（不在执行器进程环境里）——探测必须叠上单元自己的这几个值，条目里的 env 与 requirements 才是单元真正会看到的。
_DECLARED_PROBE_ENV_KEYS = ("GOFLAGS", "GOPROXY", "GOTOOLCHAIN", "GOEXPERIMENT", "CGO_ENABLED", "GOENV")
# 采样证据 declared-sample/v1 的 coverage 里验证侧（unit_records._declared_validate_sample）认识的键——字段集合是闭合的，多一个键整份证据就格式非法。
# 前六个必填，后两个可选（detached_markers：被丢弃的 strace 标记行计数；unfinished_samples：流尾悬着的调用的起始行样例，给评审看）。
_DECLARED_SAMPLE_COVERAGE_KEYS = ("stream_complete", "unresolved", "unresolved_reasons", "unfinished_calls", "unresolved_process_events", "lines",
                                  "detached_markers", "unfinished_samples")


def declared_sample_mode(text: str) -> str:
    """命令行 ``--declared-sample`` 的取值：``off``（缺省）、``always`` 或 ``every:N``（N 为正整数）；非法抛 argparse 的 ArgumentTypeError。

    口径与入口脚本 ``entry-gates.sh`` 的正则 ``^(off|always|every:[1-9][0-9]*)$`` 逐字相同，不做任何宽容：**不去首尾空白**、不认换行、
    不认前导零（``every:07``）、不认大小写变体（``ALWAYS``）、不是字符串一律拒绝。合法的取值本身就是规范写法，原样返回。
    入口编排器、入口空跑、后台验证各有一份同口径的函数，测试核对四层一致——外层放过一个内层才报错的值，或者反过来，都会让同一个
    取值在 shell 层和 Python 层得到不同结论。"""

    if isinstance(text, str):
        if text in ("off", "always") or re.fullmatch(r"every:[1-9][0-9]*", text):
            return text
    raise argparse.ArgumentTypeError(f"--declared-sample 只接受 off、always 或 every:N（N 为正整数）：{text!r}")


def _declared_scope_entry_of(current: Any) -> dict[str, Any] | None:
    """当前事实的输入明细里的 declared-inputs-scope 条目；没有返回 None。control 四链与普通单元永远没有它，所有 declared 分支都先问这一句。"""

    for entry in getattr(current, "inputs", None) or []:
        if isinstance(entry, dict) and entry.get("name") == DECLARED_SCOPE_ENTRY:
            return entry
    return None


def _declared_control_governed(unit_id: Any) -> bool:
    """这个单元是不是 control 适配器管辖的（pre-A3 场景单元，含 control 四链）：声明级登记永远不管它们。

    plan 阶段的登记表自检（entry_gates.apply_declared_inputs）本来就禁止登记任何 ``pre-a3:*`` 单元，正常流程里它们到不了这里；但执行器自己不能
    只靠上游挡——清单的顶层汇总 ``declared_inputs.rerun_only_reasons`` 是磁盘上的 JSON，若哪天被写进（或被改成带上）control 单元的 ID，
    ``ReadAuditor._declared_exempt`` 就会放过它的读集审计，control 四链的严格审计被静默跳过。所以执行器一侧再挡一次，两条判据取并：
    ① 当前运行里 control 适配器（``_CONTROL_READ_AUDIT``）自己声明的管辖集合 ``CONTROL_UNIT_IDS``（适配器不在位、或是测试桩没有这个属性时当空集合）；
    ② 单元 ID 以 ``pre-a3:`` 开头——适配器拿不到（计划没选中 control）时的兜底。取并而不是二选一：任何一条认出来就不是声明级单元。"""

    if not isinstance(unit_id, str):
        return False
    adapter = _CONTROL_READ_AUDIT.get()
    governed = getattr(adapter, "CONTROL_UNIT_IDS", None) if adapter is not None else None
    return unit_id.startswith("pre-a3:") or (isinstance(governed, (set, frozenset)) and unit_id in governed)


def _declared_unit_ids(manifest: dict[str, Any]) -> tuple[set[str], set[str]]:
    """清单里登记了声明级承接的命令单元：（declared 状态的, rerun-only 状态的）。declared＝单元输入声明带 declared_scope 键
    （apply_declared_inputs 的产物）；rerun-only＝清单顶层 declared_inputs 汇总里列出原因的。没有登记表的清单两个集合都空。

    control 适配器管辖的单元（见 ``_declared_control_governed``）两个集合里都丢掉：即使清单里有人把它们的 ID 写进汇总的 rerun_only_reasons，
    也不会让它们进入声明级分支，control 四链的行为与没有登记表时逐字相同。"""

    declared: set[str] = set()
    for item in manifest.get("units") or []:
        if isinstance(item, dict) and isinstance(item.get("inputs"), dict) and "declared_scope" in item["inputs"] and isinstance(item.get("unit_id"), str):
            declared.add(item["unit_id"])
    summary = manifest.get("declared_inputs")
    reasons = summary.get("rerun_only_reasons") if isinstance(summary, dict) else None
    rerun_only = {unit_id for unit_id in reasons if isinstance(unit_id, str)} if isinstance(reasons, dict) else set()
    return ({unit_id for unit_id in declared if not _declared_control_governed(unit_id)},
            {unit_id for unit_id in rerun_only if not _declared_control_governed(unit_id)})


def _declared_registered(manifest: dict[str, Any], registered: Any) -> Any:
    """``_gate_currents`` 的「已登记」集合并入登记表里的单元（declared 与 rerun-only 都并入）：有 audit_policy 时未登记单元会被追加
    reexecute-only-policy 并把不可承接原因写成『输入合同未闭合』；登记表里的单元走自己的声明（declared）或清单字段 inheritable:false＋登记原因
    （rerun-only），不应被它覆盖。没有登记表（含 control 四链的计划）时原样返回，同一个对象，一字不变。"""

    declared, rerun_only = _declared_unit_ids(manifest)
    if not declared and not rerun_only:
        return registered
    return {**registered, **{unit_id: None for unit_id in sorted(declared | rerun_only) if unit_id not in registered}}


def _declared_sampler_module() -> Any:
    """同目录的 declared_sampler.py（逐次采样的 strace 选项、管道过滤器、跟踪集合都用它）：按路径加载，与 _executor_files 里算进执行器版本的是同一个文件。"""

    name = "declared_sampler_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "declared_sampler.py")
        if spec is None or spec.loader is None:
            raise ExecutorError("找不到同目录的 declared_sampler.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def declared_audit_verdict(findings: list[dict[str, Any]]) -> str:
    """覆盖检查的未覆盖明细 → 这一次执行的读集审计结论：``passed``（没有未覆盖）、``incomplete``（规则全部属于 DECLARED_INCOMPLETE_RULES：
    只因证据不完整、没能证明覆盖，并没有发现违规）、``failed``（出现集合之外的任何规则）。判据是闭合的：集合之外一律 failed。

    ``declared_coverage`` 返回的明细按（规则, 路径）排序并截断到前 200 条。明细被截断（总数大于返回的条数）时，看不到的那部分里可能有集合之外的规则，
    所以 ``_declared_collect`` 在截断时一律判 failed（失败关闭）；本函数只判断「拿到的这批明细」。"""

    rules = {str(item.get("rule")) for item in findings}
    if not rules:
        return "passed"
    return "incomplete" if rules <= DECLARED_INCOMPLETE_RULES else "failed"


def _declared_aliases(env: dict[str, str], *, tree: str, fallback: str) -> dict[str, str]:
    """采样路径别名（TREE、WORK、HOME、TMP）：TREE＝测试树根；WORK＝GOCACHE 的父目录（三个 Go 缓存同在工作根内，缓存位置核验已强制）；
    HOME＝环境 HOME；TMP＝TMPDIR 或 /tmp。别名要求绝对路径且不能是根；取不到合法值时用采样目录下一个不会被访问的占位目录——这时真实的缓存读写
    会作为没有声明前缀的绝对路径被覆盖检查拒绝，而不是被悄悄放过。"""

    def usable(value: Any) -> bool:
        return isinstance(value, str) and os.path.isabs(value) and os.path.normpath(value) != "/"

    placeholder = os.path.join(fallback, "declared-alias-unavailable")
    cache = env.get("GOCACHE")
    work = os.path.dirname(os.path.normpath(cache)) if usable(cache) else None
    home = env.get("HOME") if usable(env.get("HOME")) else os.path.expanduser("~")
    tmp = env.get("TMPDIR") if usable(env.get("TMPDIR")) else "/tmp"
    return {"TREE": tree, "WORK": work if usable(work) else placeholder, "HOME": home if usable(home) else placeholder, "TMP": tmp}


def _declared_sample_from_raw(raw: dict[str, Any], *, unit_id: str, launcher: list[str], command: list[str], cwd_rel: str,
                              exit_code: int | None, seconds: float, tracked: tuple[set[str], set[str]], commit: str,
                              strace_version: str, sampler: Any, records: Any) -> dict[str, Any]:
    """把过滤器产出的 ``declared-sample-raw/v1`` 组装成 ``declared-sample/v1``（本次运行自己的证据）。

    **不调用采样器的 merge_raws 也不采信它的 valid**：merge 带「运行前后 git status 为空」这类判据，在执行器并行执行、同一测试树里多个单元同时
    跑时没有意义，还含只属于登记表起草阶段的豁免。这里只做格式组装——仓库内路径标 tracked（git 跟踪的文件，或含跟踪文件的目录；.git 内部一律
    否）；仓库外路径、git 命令、网络照搬 raw；覆盖计数只取证据认识的键（只给评审看的诊断样例放进 ``review``，见下面的说明）；命令取单元规格，
    退出码取单元实际退出码；采样器摘要是本次实际运行的 declared_sampler.py 的字节摘要。``valid`` 恒为真、``invalid_reasons`` 恒空：是否合格
    完全由 ``declared_coverage`` 判，不留两套标准。"""

    files, directories = tracked

    def tracked_flag(path: str) -> bool:
        relative = "" if path == "{TREE}" else path[len("{TREE}/"):]
        if relative == ".git" or relative.startswith(".git/"):
            return False
        return relative in files or relative in directories

    # 证据 declared-sample/v1 的 coverage 字段集合是闭合的（unit_records 只认下面这些）。过滤器产出的 raw 里还带着只给评审看的诊断样例——
    # coverage.unresolved_samples（按原因归类的原始行样例）与顶层 detached_samples（被当垃圾行丢弃的带名字的行）——整份照搬会被消费者判格式非法
    # （C7-format），把「偶发乱码行只让记录不可承接」错判成整次运行失败。做法同采样器自己的合并：coverage 只取证据认识的键，诊断样例放进 review
    # （验证侧不解释 review，但它随证据一起封存，事后能看到乱码行到底长什么样）。
    # raw 顶层的 detached_side_effects（被丢弃的垃圾行里名字属于副作用类的条数）同样是只给评审看的计数，有才写，也放进 review，不碰 coverage 的
    # 字段集合（那是闭合的）；它不影响覆盖检查的结论（declared_coverage 只看 coverage，不读 review），合法性已由上面的 validate_raw 核过。
    coverage = {key: raw["coverage"][key] for key in _DECLARED_SAMPLE_COVERAGE_KEYS if key in raw["coverage"]}
    review: dict[str, Any] = {}
    if raw["coverage"].get("unresolved_samples"):
        review["unresolved_samples"] = {reason: list(items) for reason, items in raw["coverage"]["unresolved_samples"].items()}
    if raw.get("detached_samples"):
        review["detached_samples"] = list(raw["detached_samples"])
    if raw.get("detached_side_effects"):
        review["detached_side_effects"] = raw["detached_side_effects"]

    return {
        "schema_version": records.DECLARED_SAMPLE_SCHEMA,
        "unit_id": unit_id,
        "command": {"launcher": list(launcher), "argv": list(command), "cwd_rel": cwd_rel,
                    "sha256": records.declared_command_sha256(command, cwd_rel)},
        "sampled": {"commit": commit, "utc": records.utc_now(), "kernel": os.uname().release, "strace": strace_version,
                    "sampler_sha256": sampler.file_sha256(sampler.HERE),
                    "runs": [{"cache": "formal", "exit_code": exit_code, "lines": raw["lines"], "seconds": seconds}]},
        "aliases": dict(raw["aliases"]),
        "coverage": coverage,
        "repo_accesses": [[path, list(kinds), tracked_flag(path)] for path, kinds in raw["repo_accesses"]],
        "outside_accesses": [list(row) for row in raw["outside_accesses"]],
        "git_commands": [list(argv) for argv in raw["git_commands"]],
        "network": [dict(item) for item in raw["network"]],
        "exit_code": exit_code,
        "valid": True,
        "invalid_reasons": [],
        **({"review": review} if review else {}),
    }


class _DeclaredRun:
    """一次 run-gates 调用的声明级承接上下文（放在 ContextVar ``_DECLARED_RUN`` 里；清单里没有 declared 单元时不创建）。

    * ``entry``：执行器为 control 适配器而加载的 entry_gates 模块（同一个实例）——合同探测工厂、``declared_contract_problems``、缓存位置核验都在那里；
    * ``sample_mode``：命令行 ``--declared-sample``（off／always／every:N），缺省 off；
    * ``store``：记录库（every:N 按库里该单元已有的正式记录数决定本次采不采样；消费者接受数自检也用它）；
    * ``preflights``：单元启动前拍下的前置核验结果，回收时取走；
    * ``verify_memo``：``go mod verify`` 的结果（ok／failed／timeout）按 GOMODCACHE 路径记忆——缓存在单元运行期间只读，不必每个单元核一次
      （超时也记忆：卡住的核验整次运行只等一次，见 DECLARED_GO_VERIFY_TIMEOUT_SECONDS）；
    * 其余是同一次调用内的小缓存（登记表、git 跟踪集合、HEAD 提交、strace 版本）。"""

    def __init__(self, *, entry: Any, manifest: dict[str, Any], tree: Path, sample_mode: str, declared: set[str], rerun_only: set[str]) -> None:
        self.entry = entry
        self.manifest = manifest
        self.tree = Path(tree).resolve()
        self.sample_mode = sample_mode
        self.declared = frozenset(declared)
        self.rerun_only = frozenset(rerun_only)
        self.store: Any = None
        self.preflights: dict[str, dict[str, Any]] = {}
        self.verify_memo: dict[tuple[str, str, str], str] = {}
        self._registry: dict[str, Any] | None = None
        self._tracked: tuple[set[str], set[str]] | None = None
        self._commit: str | None = None
        self._strace_versions: dict[str, str] = {}

    def sampler_module(self) -> Any:
        return _declared_sampler_module()

    def registry(self) -> dict[str, Any]:
        """测试树里的登记表（清单的 declared_inputs 汇总记着 plan 时用的位置）；读不到返回 {}——调用方按「找不到证据」失败关闭。"""

        if self._registry is None:
            self._registry = {}
            summary = self.manifest.get("declared_inputs")
            located = (summary.get("registry") or {}).get("path") if isinstance(summary, dict) and isinstance(summary.get("registry"), dict) else None
            try:
                if isinstance(located, str) and located and located != self.entry.DECLARED_REGISTRY_PATH:
                    path = Path(located)
                    loaded = self.entry.load_declared_registry(self.tree, path=path if path.is_absolute() else self.tree / path)
                else:
                    loaded = self.entry.load_declared_registry(self.tree)
                self._registry = dict((loaded or {}).get("units") or {})
            except (ValueError, OSError):
                self._registry = {}
        return self._registry

    def evidence_file(self, unit_id: str, evidence_sha256: str) -> Path | None:
        """登记表证据文件（α' 的读集证据）：先按登记表里写的位置找，摘要对不上再在证据目录里按摘要找；位置只许是证据目录下的 .json 普通文件。
        找不到返回 None。"""

        directory = (self.tree / self.entry.DECLARED_SAMPLES_DIR).resolve()
        candidates: list[Path] = []
        relative = ((self.registry().get(unit_id) or {}).get("sample") or {}).get("evidence")
        if isinstance(relative, str) and relative:
            candidates.append(self.tree / relative)
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.json")))
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file() or path.resolve().parent != directory:
                    continue
                if _records_module().file_sha256(path) == evidence_sha256:
                    return path
            except OSError:
                continue
        return None

    def tracked(self) -> tuple[set[str], set[str]]:
        if self._tracked is None:
            self._tracked = self.sampler_module().tracked_sets(self.tree)
        return self._tracked

    def commit(self) -> str:
        if self._commit is None:
            completed = subprocess.run(["git", "-C", str(self.tree), "rev-parse", "HEAD"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self._commit = completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else "unknown"
        return self._commit

    def strace_version(self, strace: str, env: dict[str, str]) -> str:
        if strace not in self._strace_versions:
            self._strace_versions[strace] = self.sampler_module().strace_version(strace, env)
        return self._strace_versions[strace]

    def should_sample(self, unit_id: str) -> bool:
        """本次执行要不要带 strace 采样：off 不采；always 每次采；every:N 按记录库里该单元已有的正式记录数（对 N 取余为 0 就采，所以第一次执行
        必采）。没有记录库时没有历史可数，按零条算。"""

        if self.sample_mode == "always":
            return True
        found = re.fullmatch(r"every:([1-9][0-9]*)", self.sample_mode)
        if found is None:
            return False
        seen = 0
        if self.store is not None:
            seen = sum(1 for _path, record in self.store.candidates(unit_id) if record and record.get("kind") == "formal")
        return seen % int(found.group(1)) == 0


def _declared_probe_overlay(unit_env: Any) -> dict[str, str]:
    """单元清单 env 里会影响 go env 生效值的变量（离线值就在这里）。"""

    env = dict(unit_env or {})
    return {key: str(env[key]) for key in _DECLARED_PROBE_ENV_KEYS if key in env}


def _declared_probe_session(sessions: dict[Any, Any], unit_env: Any) -> Any:
    """取这个单元的合同探测会话。同一次 ``_gate_currents`` 调用里，探测环境相同的单元共用一个会话（同一事实只探一次）；环境按单元 env 里的 go
    变量区分，所以所有 declared Go 单元共用一个会话，前端等不带 go 变量的单元共用另一个。回调工厂没设置是编程错误，响亮失败。"""

    factory = _DECLARED_PROBE.get()
    if factory is None:
        raise ExecutorError("声明级单元需要合同探测回调：_DECLARED_PROBE 没有设置")
    overlay = _declared_probe_overlay(unit_env)
    key = tuple(sorted(overlay.items()))
    if key not in sessions:
        sessions[key] = factory(environ={**os.environ, **overlay}) if overlay else factory()
    return sessions[key]


def _declared_add_contracts(records: Any, inputs: list[dict[str, Any]], item: dict[str, Any], sessions: dict[Any, Any]) -> list[dict[str, Any]]:
    """declared 单元的输入明细并入 ``contract:*`` 条目（每次算当前事实都重新探测；探测失败写 unavailable 值，不抛）。"""

    session = _declared_probe_session(sessions, item.get("env"))
    return records._unique([*inputs, *session(item["inputs"])])


def _declared_file_digest(path: Path) -> str:
    """文件内容摘要；不存在记 missing、读不了记 unreadable(原因)。这些值都不是 64 位十六进制，凭证核验会拒绝它们。"""

    try:
        if not path.is_file():
            return "missing"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        return f"unreadable({type(error).__name__})"


def _declared_tree_location_ok(tree: Path, detail: dict[str, Any]) -> tuple[bool, str | None]:
    """位置类别是 tree 的缓存根（vite 缓存、tsbuildinfo）：路径逐级不能是符号链接；还不存在没关系（命令会创建），已匹配的 glob 项也不能是符号链接。"""

    relative = detail.get("path")
    if not isinstance(relative, str) or not relative:
        return False, "缓存合同缺少 path"
    base = Path(tree)
    for part in Path(relative).parts:
        base = base / part
        if base.is_symlink():
            return False, f"{base} 是符号链接"
        if not base.exists():
            return True, None
    pattern = detail.get("glob")
    if isinstance(pattern, str) and pattern and base.is_dir():
        for match in base.glob(pattern):
            if match.is_symlink():
                return False, f"{match} 是符号链接"
    return True, None


# go mod verify（go.sum 初态核验）这一步的单独超时，秒。
# 理由：预热（go_compile_warmup）已经做过一遍 go mod download／verify（本机真实 backend 约 2 秒，ARM64 按 10 倍保守估计约 20 秒，预热里这一步的上限
# 是 120 秒），这里只是单元启动前冗余的二次核验。它在调度线程里同步执行（Scheduler.launch → _declared_preflight），卡住（模块缓存上的锁、
# 坏盘、网络文件系统）就会拖住调度线程：其它运行中单元的超时检查与回收全部停滞。合同探测会话的默认 runner（entry_steps.default_runner）超时
# 是 900 秒，对这一步太长，所以单独给 180 秒（约为预热里同一步上限的 1.5 倍）。
# 超时按「核验没通过」处理：该单元的记录 inheritable=False、命令照跑（通过／失败结论不受影响），凭证里写明 verify=timeout 与原因。结果同
# 正常结果一样按 GOMODCACHE 记忆（_DeclaredRun.verify_memo），三个 Go 单元共用同一份缓存时整次运行最多等这一次。测试把这个常量调小来造超时。
DECLARED_GO_VERIFY_TIMEOUT_SECONDS = 180


def _declared_go_mod_verify(run: _DeclaredRun, *, env: dict[str, str], cwd: Path) -> str:
    """跑 ``go mod verify``（环境＝单元实际环境，工作目录＝go.mod 所在目录），返回 ``ok``／``failed``／``timeout``。

    超时用 ``DECLARED_GO_VERIFY_TIMEOUT_SECONDS``（每次调用时现取，测试可以调小）：超过就杀掉这个子进程、不再等，返回 ``timeout``。
    其它出错（go 不在 PATH、工作目录不存在等）与过去 ``default_runner`` 把它们折成退出码 127 同一个结果：``failed``。
    通过的判据不变：退出码为 0 且输出里有整行 ``all modules verified``（go_compile_warmup.module_verified）。"""

    try:
        completed = subprocess.run(["go", "mod", "verify"], capture_output=True, text=True, errors="replace", env=dict(env), cwd=str(cwd),
                                   stdin=subprocess.DEVNULL, timeout=DECLARED_GO_VERIFY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, subprocess.SubprocessError):
        return "failed"
    verified = run.entry._go_compile_warmup_module().module_verified(completed.returncode, completed.stdout.encode("utf-8", "replace"))
    return "ok" if verified else "failed"


def _declared_cache_credential(run: _DeclaredRun, *, detail: dict[str, Any], env: dict[str, str], files: dict[str, str],
                               tree: Path, module_dir: Path, locations: Any) -> tuple[dict[str, Any], bool, str | None]:
    """一个 ``cache:*`` 合同的运行前核验：返回（observed, 是否通过, 不通过的原因）。observed 的字段取廉价事实（调研 4.2）：
    ``any-content-addressed``／``any-scratch``→``{root_ok}``；``lock-derived-pnpm``→``{installed_lock_sha256, lock_file_sha256}``；
    ``lock-derived-go-sum``→``{go_sum_sha256, verify, root_ok}``（verify 取 ok／failed／timeout／skipped）。运行后不再对缓存目录做任何整体摘要。"""

    rule = detail.get("rule")
    location = detail.get("location")

    def root_ok() -> tuple[bool, str | None]:
        if location == "work":
            error = locations()
            return error is None, error
        if location == "tmp":
            value = env.get("TMPDIR") or "/tmp"
            ok = os.path.isabs(value) and os.path.isdir(value)
            return ok, None if ok else f"TMPDIR 不是存在的绝对目录：{value!r}"
        if location == "tree":
            return _declared_tree_location_ok(tree, detail)
        return False, f"不认识的位置类别 {location!r}"

    if rule in ("any-content-addressed", "any-scratch"):
        ok, why = root_ok()
        observed: dict[str, Any] = {"root_ok": ok}
        if why:
            observed["problem"] = why
        return observed, ok, why
    if rule == "lock-derived-pnpm":
        lock_file = detail.get("lock_file") or "frontend/pnpm-lock.yaml"
        installed_lock = detail.get("installed_lock") or "frontend/node_modules/.pnpm/lock.yaml"
        locked, installed = _declared_file_digest(tree / lock_file), _declared_file_digest(tree / installed_lock)
        observed = {"installed_lock_sha256": installed, "lock_file_sha256": locked}
        if not re.fullmatch(r"[0-9a-f]{64}", locked) or not re.fullmatch(r"[0-9a-f]{64}", installed):
            why = f"锁文件或安装锁副本读不到（{lock_file}={locked}，{installed_lock}={installed}）"
        elif installed != locked:
            why = f"node_modules 的安装锁副本 {installed_lock} 与 {lock_file} 不是同一份（安装结果不是按当前锁文件装的）"
        elif files.get(f"file:{lock_file}") != locked:
            why = f"锁文件摘要不等于输入里 file:{lock_file} 条目的摘要"
        else:
            why = None
        if why:
            observed["problem"] = why
        return observed, why is None, why
    if rule == "lock-derived-go-sum":
        lock_file = detail.get("lock_file") or "backend/go.sum"
        go_sum = _declared_file_digest(tree / lock_file)
        located, why = root_ok()
        verify = "skipped"
        if located:
            # go mod verify：不联网，只核对「缓存里已有模块」的内容与 go.sum 一致，**不证明缓存完整**（模块只有 .mod、压缩包从未下载时它也输出
            # all modules verified）；缓存完整性由预热里 go mod download 成功保证，运行期离线、缺模块会让命令直接失败。
            key = (env.get("GOMODCACHE") or "", str(module_dir), go_sum)
            if key not in run.verify_memo:
                run.verify_memo[key] = _declared_go_mod_verify(run, env=env, cwd=module_dir)
            verify = run.verify_memo[key]
            if verify == "timeout":
                why = (f"go mod verify 超过 {DECLARED_GO_VERIFY_TIMEOUT_SECONDS:g} 秒还没结束，已放弃等待（预热已核验过一遍，这里是冗余的二次核验，"
                       f"卡住不能拖住调度线程）：按核验没通过处理")
            elif verify != "ok":
                why = "go mod verify 没有通过（退出码非零，或输出里没有 all modules verified）"
            elif files.get(f"file:{lock_file}") != go_sum:
                why = f"go.sum 摘要不等于输入里 file:{lock_file} 条目的摘要"
        observed = {"go_sum_sha256": go_sum, "verify": verify, "root_ok": located}
        if why:
            observed["problem"] = why
        return observed, why is None, why
    return {"root_ok": False, "problem": f"不认识的缓存规则 {rule!r}"}, False, f"不认识的缓存规则 {rule!r}"


def _declared_preflight_checks(run: _DeclaredRun, unit: "Unit", current: Any, verified_at: str, credentials: list[dict[str, Any]],
                               problems: list[str]) -> None:
    entry = _declared_scope_entry_of(current)
    scope = entry["detail"]["scope"]
    inputs = [item for item in current.inputs or [] if isinstance(item, dict)]
    contracts = {item["name"]: item for item in inputs if str(item.get("name", "")).startswith("contract:")}
    files = {item["name"]: item["sha256"] for item in inputs if str(item.get("name", "")).startswith("file:")}
    env = {**os.environ, **dict(unit.env)}
    tree = run.tree

    # 合同条目自带的「不能承接」情形：探测失败（unavailable）、可执行缺失、离线要求未满足、go test 缺 -count=1（防测试结果缓存成为隐藏输入）。
    problems.extend(run.entry.declared_contract_problems(list(contracts.values()), argv=list(unit.command)))
    # 合同要求的变量必须真的在单元环境里等于要求值：门禁白名单环境不会替我们设，离线值只能来自单元清单的 env。
    for name in sorted(contracts):
        requires = (contracts[name].get("detail") or {}).get("requires")
        for key, value in sorted((requires.get("env_equals") or {}).items() if isinstance(requires, dict) else []):
            if env.get(key) != value:
                problems.append(f"{name}：合同要求单元环境里 {key}={value}，实际是 {env.get(key)!r}")

    # 本次前置核验用自己的探测会话（环境＝单元实际环境）：重探服务镜像与 git 提交、跑 go mod verify。
    session = (_DECLARED_PROBE.get() or run.entry.declared_probe)(environ=env)
    memo: list[str | None] = []

    def locations() -> str | None:
        """三个 Go 缓存目录的位置核验，直接复用预热收据用的 go_compile_warmup.cache_paths：绝对路径、无符号链接、在工作根内、不在测试树内、
        互不包含。工作根＝GOCACHE 的父目录（与探测会话、预热同一约定）。结果在本次核验内只算一次。"""

        if not memo:
            try:
                cache = env.get("GOCACHE")
                if not cache:
                    raise ValueError("单元环境里没有 GOCACHE")
                run.entry.declared_cache_locations(tree, Path(cache).parent, env)
                memo.append(None)
            except ValueError as error:
                memo.append(str(error))
        return memo[0]

    repo = scope.get("repo") or {}
    module_dir = next((tree / Path(path).parent for path in repo.get("files") or [] if path == "go.mod" or path.endswith("/go.mod")), tree / "backend")
    images = [ident for ident in sorted(scope.get("contracts") or []) if ident.startswith("service-image:")]
    commits = list((repo.get("git_history") or {}).get("commits") or [])
    fresh: dict[str, dict[str, Any]] = {}
    if images or commits:
        # 单元启动前再探一次，与算 Current 时的值比较：防 plan 之后 docker pull 换了镜像、历史提交被浅克隆丢掉。只重探需要的合同，不重算工具链摘要。
        reduced = {"schema_version": scope.get("schema_version", "declared-scope/v1"), "contracts": images,
                   "repo": {"git_history": {"commits": commits, "reason": "启动前复核"}} if commits else {}}
        fresh = {item["name"]: item for item in session.entries(reduced)}

    def credential(kind: str, ident: str, ok: bool, observed: dict[str, Any], rule: Any = None) -> None:
        item: dict[str, Any] = {"id": ident, "kind": kind, "verified_before_run": bool(ok), "observed": observed, "verified_at_utc": verified_at}
        if rule is not None:
            item["rule"] = rule
        credentials.append(item)

    for ident in sorted(scope.get("contracts") or []):
        kind, _, name = ident.partition(":")
        detail = (contracts.get(f"contract:{ident}") or {}).get("detail") or {}
        if kind == "cache":
            observed, ok, why = _declared_cache_credential(run, detail=detail, env=env, files=files, tree=tree, module_dir=module_dir,
                                                           locations=locations)
            credential("cache", name, ok, observed, detail.get("rule"))
            if not ok:
                problems.append(f"contract:{ident}：{why or '核验未通过'}")
        elif kind == "service-image":
            new_id = ((fresh.get(f"contract:{ident}") or {}).get("detail") or {}).get("image_id")
            old_id = detail.get("image_id")
            ok = isinstance(new_id, str) and not new_id.startswith("unavailable") and new_id == old_id
            credential("service-image", name, ok, {"image_id": new_id})
            if not ok:
                problems.append(f"contract:{ident}：启动前复核的镜像 Id {new_id!r} 与算当前事实时的 {old_id!r} 不一致（镜像被换过，或探测失败）")
    for commit in commits:
        old_tree = ((contracts.get(f"contract:git-history:{commit}") or {}).get("detail") or {}).get("tree")
        new_tree = ((fresh.get(f"contract:git-history:{commit}") or {}).get("detail") or {}).get("tree")
        ok = isinstance(new_tree, str) and not new_tree.startswith("unavailable") and new_tree == old_tree
        credential("git-history", commit, ok, {"commit": commit, "tree": new_tree})
        if not ok:
            problems.append(f"contract:git-history:{commit}：启动前复核的树对象 {new_tree!r} 与算当前事实时的 {old_tree!r} 不一致（提交缺失或探测失败）")


def declared_contract_preflight(unit: "Unit", current: Any) -> dict[str, Any]:
    """声明级命令单元启动前的前置核验（只对**要执行**的单元做，``Scheduler.launch`` 之前；回收后由 ``ReadAuditor.declared_finish`` 取走凭证）。

    核验：缓存根的位置（复用预热的 ``cache_paths``：绝对、无符号链接、在工作根内且互不包含）；node_modules 安装锁副本与 pnpm-lock.yaml 字节相同
    （且等于输入里的锁文件条目）；go.sum 初态（``go mod verify``，按 GOMODCACHE 路径记忆；它只核对缓存里已有模块的内容，**不证明缓存完整**）；
    ``go test`` 带 ``-count=1``；服务镜像 Id 与算当前事实时相同；声明的 git 历史提交仍然存在；合同要求的离线环境变量真的在单元环境里。

    返回 ``{"credentials": [...], "problems": [...], "verified_at_utc": ...}``：凭证数组直接进记录的 ``declared_contracts``（每项
    ``{id, kind, verified_before_run, observed, verified_at_utc[, rule]}``，格式见 unit_records 的 ``_declared_credential_problems``）；problems 非空
    则该次记录 inheritable=False，永不成为承接来源。核验自己出错也不拖垮调度器：记成一条问题，该次记录不可承接。"""

    run = _DECLARED_RUN.get()
    verified_at = _utc_now()
    credentials: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        if run is None:
            raise ExecutorError("没有声明级运行上下文")
        _declared_preflight_checks(run, unit, current, verified_at, credentials, problems)
    except Exception as error:  # noqa: BLE001 - 前置核验自身出错不能让调度器中止整次运行：该次记录不可承接，原因写明
        problems.append(f"前置核验自身出错：{type(error).__name__}: {error}")
    return {"credentials": credentials, "problems": problems, "verified_at_utc": verified_at}


def _declared_run_summary(records: Any, *, run: _DeclaredRun, outcomes: list["Outcome"], decisions: dict[str, Any], currents: dict[str, Any],
                          store: Any, facts: Any) -> dict[str, Any]:
    """汇总里的 ``declared_inputs``（方案 6.4 第 5 条）：四个数分开，再加每个被重跑单元的原因与耗时。

    * ``registered``：本次计划里登记为 declared 的命令单元数（``rerun_only`` 另计：登记为只重跑的单元数）；
    * ``audit_passed``：其中本次**执行**了且声明级读集审计通过的单元数（``audit_failed``、``audit_incomplete`` 另计，承接的单元本次没有审计）；
    * ``consumer_accepted``：消费者核验接受的单元数——承接的单元（承接决策本身就是消费者核验的接受）加本次执行的单元里，刚写下的记录按承接用的
      同一个 ``check_record`` 核验也被接受的（记录库里没有这条记录或没有记录库时无从核验，只算承接的）；被拒的单元及前三条原因列在 ``consumer_rejected``；
    * ``inherited``：实际承接的单元数；
    * ``reruns``：每个被重跑的 declared／rerun-only 单元的 ``{kind, reasons, seconds[, not_inheritable_reason]}``。"""

    by_unit = {outcome.unit.unit_id: outcome for outcome in outcomes}
    executed = [unit_id for unit_id in sorted(run.declared) if unit_id in by_unit and by_unit[unit_id].extra.get("disposition", "executed") == "executed"]
    inherited = [unit_id for unit_id in sorted(run.declared) if unit_id in by_unit and by_unit[unit_id].extra.get("disposition") == "inherited"]
    audits = {unit_id: (by_unit[unit_id].extra.get("read_audit") or {}) for unit_id in executed}
    accepted = list(inherited)
    rejected: dict[str, list[str]] = {}
    checked = store is not None
    if checked:
        check_facts = replace(facts, now=time.time())
        for unit_id in executed:
            path = Path(str(by_unit[unit_id].extra.get("record_path") or ""))
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                problems = records.check_record(store, path, record, currents[unit_id], check_facts)
            except Exception as error:  # noqa: BLE001 - 汇总里的自检不能拖垮整次运行：核验出错记成「不接受」及原因
                problems = [f"消费者核验没能完成：{type(error).__name__}: {error}"]
            if problems:
                rejected[unit_id] = problems[:3]
            else:
                accepted.append(unit_id)
    reruns: dict[str, dict[str, Any]] = {}
    for kind, members in (("declared", run.declared), ("rerun-only", run.rerun_only)):
        for unit_id in sorted(members):
            outcome = by_unit.get(unit_id)
            if outcome is None or outcome.extra.get("disposition", "executed") != "executed":
                continue
            row: dict[str, Any] = {"kind": kind, "reasons": list(decisions[unit_id].reasons) if unit_id in decisions else [],
                                   "seconds": outcome.seconds}
            current = currents.get(unit_id)
            if current is not None and current.reason:
                row["not_inheritable_reason"] = current.reason
            if outcome.extra.get("declared_not_inheritable"):
                row["record_not_inheritable_reason"] = outcome.extra["declared_not_inheritable"]
            reruns[unit_id] = row
    registry_total = ((run.manifest.get("declared_inputs") or {}).get("registered")) if isinstance(run.manifest.get("declared_inputs"), dict) else None
    return {
        "registered": len(run.declared), "rerun_only": len(run.rerun_only), "registry_total": registry_total,
        "audit_passed": sum(1 for unit_id in executed if audits[unit_id].get("status") == "passed"),
        "audit_failed": sum(1 for unit_id in executed if audits[unit_id].get("status") == "failed"),
        "audit_incomplete": sum(1 for unit_id in executed if audits[unit_id].get("status") == "incomplete"),
        "consumer_accepted": len(accepted), "consumer_rejected": rejected,
        "consumer_check": "checked" if checked else "skipped-no-record-store",
        "inherited": len(inherited), "executed": len(executed), "sample_mode": run.sample_mode, "reruns": reruns,
    }


def _declared_sync_auditor(scheduler: Any, currents: dict[str, Any]) -> None:
    """锁后复核重算了当前事实：只为声明级单元建的审计器（挂在调度器上、不在 _run_gates_current 的 auditor 变量里）要跟着换新的事实，
    旧代码只更新那个变量。普通审计器（含 control 四链的）不动。"""

    auditor = getattr(scheduler, "auditor", None)
    if auditor is not None and getattr(auditor, "declared_only", False):
        auditor.currents = currents


class ReadAuditor:
    """正式单元执行后、记录入库前核对读集。严格模式覆盖全根且不跳过整仓声明；
    旧范围审计仅用于诊断覆盖，不能标记为宿主输入已闭合。"""

    def __init__(self, *, out_dir: Path, repo_root: Path, data_root: Path | None, currents: dict[str, Any], strict: bool = False) -> None:
        self.module = _audit_module()
        self.records = _records_module()
        self.dir = Path(out_dir) / "audit"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo_root = str(Path(repo_root).resolve())
        self.data_root = str(Path(data_root).resolve()) if data_root is not None else None
        self.currents = currents
        self.results: dict[str, dict[str, Any]] = {}
        self.strict = strict
        # B5a-08：逐单元承接时承接的单元（不执行、没有轨迹；它们的读集已由 check_record 按来源记录的轨迹重放核过）。
        self.inherited: frozenset[str] = frozenset()
        # 声明级承接（WP3）：逐单元的 wrap→collect 状态（采样路径、启动前缀、降级原因、单元实际结果）；declared_only＝只为声明级单元建的审计器
        # （没给 --audit-reads、清单里却有 declared 单元时），它对其它单元什么都不做——不包 strace、不产生 read_audit。
        self._declared_state: dict[str, dict[str, Any]] = {}
        self.declared_only = False

    def trace_path(self, unit: Unit) -> Path:
        # 可读前缀不能保证唯一；完整单元 ID 摘要防止标点替换和长名称截断造成轨迹串用。
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", unit.unit_id)[:80].strip(".") or "unit"
        identity = hashlib.sha256(unit.unit_id.encode("utf-8")).hexdigest()
        return self.dir / f"{safe}-{identity}.trace.json"

    def skipped(self, unit: Unit) -> bool:
        """声明了整个仓库的单元不审计（见 read_audit.declares_whole_repo）。"""

        current = self.currents.get(unit.unit_id)
        return not self.strict and current is not None and self.module.declares_whole_repo(current.inputs)

    def wrap(self, unit: Unit, argv: list[str]) -> list[str]:
        current = self.currents.get(unit.unit_id)
        if _declared_scope_entry_of(current) is not None:
            return self._declared_wrap(unit, argv, current)
        if self.declared_only or self._declared_exempt(unit):
            return argv
        if current and any(entry.get("name") == "reexecute-only-policy" for entry in current.inputs or []):
            return argv
        if self.skipped(unit):
            return argv
        roots = [self.repo_root] + ([self.data_root] if self.data_root else [])
        trace = self.trace_path(unit)
        with contextlib.suppress(FileNotFoundError):
            trace.unlink()
        current = self.currents.get(unit.unit_id)
        runtime_entries = [entry for entry in (current.inputs or []) if entry.get("name") == "runtime-contract"] if current else []
        if runtime_entries:
            if not self.strict or len(runtime_entries) != 1:
                raise ExecutorError("隔离输出合同必须具有唯一输入绑定并启用严格审计")
            runtime = self.module._runtime_module()
            contract = runtime_entries[0]["detail"]["contract"]
            if unit.command:
                command = list(unit.command)
            else:
                # 测试单元仍保留原执行器参数；启动器单独按固定合同替换。
                command = argv
            if command[:len(runtime.LAUNCHER)] == runtime.LAUNCHER:
                if contract["mask_roots"] != [runtime.MASK_ROOT]:
                    raise ExecutorError("原启动器的生产别名遮挡不能被取消")
                command = command[len(runtime.LAUNCHER):]
            elif contract["mask_roots"]:
                raise ExecutorError("未识别的启动器不能使用既有遮挡合同")
            return runtime.prepare(contract, output=trace, argv=command, cwd=unit.cwd or self.repo_root,
                                   inputs=current.inputs, test_spec=None if unit.command else current.spec)
        return self.module.strace_argv(argv, output=trace, roots=roots, strict=self.strict)

    def collect(self, unit: Unit) -> dict[str, Any]:
        current = self.currents.get(unit.unit_id)
        inputs = current.inputs if current is not None else None
        if _declared_scope_entry_of(current) is not None:
            return self._declared_collect(unit, current)
        if self._declared_exempt(unit):
            # 登记为 rerun-only 的命令单元：与旧的「声明了整个仓库的单元不审计」同一个结果形状，另带 declared_exempt 标记让 report 的承接边界
            # 判定认得它（它的记录本来就不可承接，见 _declared_exempt）。
            result = {"skipped": "登记为 rerun-only 的命令单元（永不成为承接来源），不审计", "undeclared_count": 0, "undeclared": [],
                      "status": "passed", "coverage_complete": False, "coverage_scope": "not-observed", "declared_exempt": "rerun-only"}
            self.results[unit.unit_id] = result
            return result
        policy = next((entry for entry in inputs or [] if entry.get("name") == "reexecute-only-policy"), None)
        if policy is not None:
            if current.inheritable:
                raise ExecutorError("强制重跑策略与可承接状态矛盾")
            result = {"status": "reexecute_required", "coverage_complete": False, "coverage_scope": "not-observed",
                      "reexecute_policy_sha256": policy["sha256"], "undeclared_count": 0, "undeclared": [],
                      "reason": current.reason, "inputs_sha256": current.inputs_sha256}
            self.results[unit.unit_id] = result
            return result
        if self.skipped(unit):
            result = {"skipped": "输入声明了整个仓库，不审计", "undeclared_count": 0, "undeclared": [],
                      "status": "passed", "coverage_complete": False, "coverage_scope": "not-observed"}
            self.results[unit.unit_id] = result
            return result
        try:
            document = self.module.load_trace(self.trace_path(unit))
        except self.module.AuditError as error:
            result = {"undeclared_count": 1, "undeclared": [{"root": "audit", "path": "", "kinds": [], "sample": "",
                                                              "suggestion": f"没有审计轨迹（strace 或过滤器没跑起来）：{error}"}]}
        else:
            if inputs is None:
                result = {"undeclared_count": 1, "undeclared": [{"root": "audit", "path": "", "kinds": [], "sample": "",
                                                                  "suggestion": f"单元没有输入明细（{getattr(current, 'reason', '') or '不可承接'}），审计无从核对"}]}
            else:
                cwd = str(Path(unit.cwd).resolve()) if unit.command and unit.cwd else self.repo_root
                in_data_root = bool(self.data_root) and (cwd == self.data_root or cwd.startswith(self.data_root + "/"))
                head_id_only = not unit.command and unit.module.split("+", 1)[0] in self.records.HEAD_ID_ONLY_READERS
                result = self.module.audit_reads(document, inputs, repo_root=self.repo_root, data_root=self.data_root,
                                                 in_data_root=in_data_root, head_id_only=head_id_only, strict=self.strict)
                result["trace_lines"] = document.get("lines")
                trace = self.trace_path(unit)
                result["trace"] = {"path": str(trace), "sha256": self.records.file_sha256(trace)}
                if result.get("coverage_complete") is True:
                    # 宿主快照 v3：把每个宿主路径在这份已验证的严格轨迹里的访问类型封存进记录（随记录自摘要），承接时与
                    # 轨迹重算的结果交叉核对；没有 v3 条目（v2／control）时为空，不加字段，记录与过去逐字相同。
                    try:
                        modes = self.module.host_path_modes(document, inputs)
                    except self.module.AuditError:
                        modes = {}  # 取不到就不封存；v3 记录缺该字段时承接失败关闭
                    if modes:
                        result["host_path_modes"] = modes
        result.setdefault("status", "failed" if result.get("undeclared_count") else "passed")
        result.setdefault("coverage_complete", False)
        result.update(inputs_sha256=current.inputs_sha256 if current else None, repo_root=self.repo_root, data_root=self.data_root)
        self.results[unit.unit_id] = result
        return result

    # -- 声明级承接（declared-inputs/v1，WP3）---------------------------------------------------------------------------
    # 下面的方法只在「当前输入里有 declared-inputs-scope 条目」的单元上被调用（_declared_exempt 例外：见它的说明）；control 四链与普通单元不会进来。

    def handles_declared(self, unit: Unit) -> bool:
        return _declared_scope_entry_of(self.currents.get(unit.unit_id)) is not None

    def _declared_exempt(self, unit: Unit) -> bool:
        """登记表里登记为 rerun-only 的命令单元：读集审计放过它们（永不成为承接来源，审计对它们没有意义）。

        过去它们靠两条路被放过，登记之后两条路都走不通了：非严格审计按「声明了整个仓库的单元不审计」跳过，登记后清单标了 inheritable:false、
        当前输入明细为空，审计会报「单元没有输入明细，无从核对」；有 audit_policy 的严格审计运行里，没登记的单元会被追加 reexecute-only-policy
        条目，而声明级承接把登记表里的单元并入「已登记」集合（不追加该条目，否则 rerun-only 的原因会被覆盖），它们就会被严格审计去审，必然失败。
        这里统一放行：不包 strace、结果形状同旧的「声明了整个仓库的单元不审计」，report 的承接边界判定另认 declared_exempt 标记。
        它们的记录本来就不可承接（清单 inheritable:false），所以放行不放宽任何承接。

        control 适配器管辖的单元（control 四链、其它 pre-A3 场景单元）永远不放行，不管声明级上下文里的 rerun_only 集合写了什么：放行它们等于静默跳过
        它们的严格审计。``_declared_unit_ids`` 已经把它们挡在集合之外；这里是第二道（上下文可以不经清单直接构造），判据见 ``_declared_control_governed``。"""

        run = _DECLARED_RUN.get()
        return (run is not None and unit.unit_id in run.rerun_only
                and not _declared_control_governed(unit.unit_id)
                and _declared_scope_entry_of(self.currents.get(unit.unit_id)) is None)

    def note_outcome(self, outcome: "Outcome") -> None:
        """回收时由调度器在 collect 之前调用：声明级单元的逐次采样要把单元实际的退出码、信号与耗时写进证据。其它单元什么都不做。"""

        if self.handles_declared(outcome.unit):
            self._declared_state.setdefault(outcome.unit.unit_id, {})["outcome"] = outcome

    def _declared_cwd_rel(self, unit: Unit) -> str | None:
        """单元工作目录相对测试树根的路径（树根记 "."）；不在测试树内返回 None。"""

        cwd, root = os.path.normpath(unit.cwd or os.getcwd()), os.path.normpath(self.repo_root)
        if cwd == root:
            return "."
        return cwd[len(root) + 1:] if cwd.startswith(root + os.sep) else None

    def _declared_wrap(self, unit: Unit, argv: list[str], current: Any) -> list[str]:
        """声明级单元的 wrap。

        α'（缺省）：零运行时开销，原样返回 argv；读集证据在 collect 时取登记表里的那份。
        β（--declared-sample always／every:N）：剥掉启动前缀，把命令包进采样器的 strace（选项沿用 ``declared_sampler.STRACE_OPTIONS``），
        strace 的输出经 ``-o '|…'`` 交给过滤器进程（采样器的 filter 子命令，不在被跟踪的进程树内）；**启动前缀放在 strace 外侧**——命令看到的
        挂载命名空间与正式执行完全相同，而 unshare、mount 这类未建模的调用不进轨迹（与采样器 run 的做法一致，调研 5.3）。
        启动前缀怎么切：命令身份（登记的 command_sha256）是去掉前缀的 argv 加相对树根的工作目录的摘要，按前缀长度逐个试，摘要对得上的切法就是；
        一种切法都对不上说明执行的命令不是登记的那条，collect 判失败（fail-closed）。
        取不到 strace、过滤器管道路径不安全、别名非法等采样基础设施问题不使整次运行失败：降级为不包 strace 照常执行，collect 记为证据不完整
        （status=incomplete），该次记录不可承接。"""

        run = _DECLARED_RUN.get()
        state: dict[str, Any] = {"sampled": False}
        self._declared_state[unit.unit_id] = state
        if run is None or not run.should_sample(unit.unit_id):
            return argv
        reference = _declared_scope_entry_of(current)["detail"]["sample"]
        cwd_rel = self._declared_cwd_rel(unit)
        split = None
        if cwd_rel is not None:
            split = next((size for size in range(len(unit.command))
                          if self.records.declared_command_sha256(unit.command[size:], cwd_rel) == reference["command_sha256"]), None)
        if split is None:
            state["identity"] = (f"单元的命令行与工作目录（相对树根 {cwd_rel!r}）在启动前缀的任何切法下，命令摘要都不等于范围条目登记的 "
                                 f"command_sha256：执行的不是登记的那条命令")
            return argv
        launcher, command = list(unit.command[:split]), list(unit.command[split:])
        sampler = run.sampler_module()
        env = {**os.environ, **dict(unit.env)}
        strace = shutil.which("strace", path=env.get("PATH"))
        if strace is None:
            state["degraded"] = "no-strace：PATH 里找不到 strace，逐次采样做不了"
            return argv
        trace = self.trace_path(unit)
        base = trace.name.removesuffix(".trace.json")
        raw, sample_path = trace.with_name(base + ".declared-raw.json"), trace.with_name(base + ".declared-sample.json")
        try:
            aliases = _declared_aliases(env, tree=self.repo_root, fallback=str(self.dir))
            sampler.AliasTable(aliases)
            pipe = sampler.filter_pipe(output=raw, unit_id=unit.unit_id, aliases=aliases, chunk_lines=sampler.DEFAULT_CHUNK_LINES)
        except sampler.SamplerError as error:
            state["degraded"] = f"filter-pipe：{error}"
            return argv
        for stale in (raw, sample_path):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        state.update(sampled=True, launcher=launcher, command=command, cwd_rel=cwd_rel, raw=raw, sample=sample_path, aliases=aliases,
                     strace=strace, strace_env=env)
        return sampler.build_strace_argv(launcher=launcher, strace=strace, pipe=pipe, cwd=unit.cwd or os.getcwd(), command=command)

    def _declared_collect(self, unit: Unit, current: Any) -> dict[str, Any]:
        """声明级单元的读集结果（写进记录的 ``read_audit``，格式见调研 3.3）。

        证据来源：α'（sample_origin=registry）是登记表里的证据文件，字节摘要必须等于范围条目登记的 evidence_sha256；β（sample_origin=run）是本次
        运行自己采样出的 ``declared-sample/v1``。两者都以 ``trace={path, sha256}`` 带出（Recorder 把它封存进记录库 logs/<摘要>.log），并由
        ``declared_coverage`` 重算覆盖——执行侧与消费者共用同一个纯函数。

        结论：覆盖通过→passed；α' 覆盖不通过→failed（登记表证据在 plan 时已验过，现在不过说明登记表与代码不一致，整次运行判失败）；β 覆盖不通过按
        ``DECLARED_INCOMPLETE_RULES`` 分两类——规则全在集合内→incomplete（只因证据不完整，记录不可承接、命令结论不变），出现集合之外的规则→failed
        （fail-closed，整次运行判失败）。命令身份不符、证据文件缺失或被改过、组装的证据格式不对一律 failed。取不到 strace 等降级情形记为 incomplete。"""

        records = self.records
        run = _DECLARED_RUN.get()
        state = self._declared_state.pop(unit.unit_id, None) or {}
        entry = _declared_scope_entry_of(current)
        scope, reference = entry["detail"]["scope"], entry["detail"]["sample"]
        sampled = bool(state.get("sampled"))
        result: dict[str, Any] = {
            "coverage_scope": DECLARED_COVERAGE_SCOPE, "inputs_sha256": current.inputs_sha256, "repo_root": self.repo_root,
            "data_root": self.data_root, "sample_origin": "run" if sampled else "registry",
            "sample_binding": {"unit_id": unit.unit_id, "command_sha256": reference["command_sha256"],
                               "scope_sha256": records.sha256_json(scope), "evidence_sha256": reference["evidence_sha256"]}}

        def finish(status: str, *, findings: list[dict[str, Any]] | None = None, count: int | None = None, trace: dict[str, str] | None = None,
                   counts: dict[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
            failed = status == "failed"
            result.update(status=status, coverage_complete=status == "passed", undeclared_count=(count if count is not None else len(findings or []))
                          if failed else 0, undeclared=list(findings or [])[:200] if failed else [])
            if trace is not None:
                result["trace"] = trace
            if counts is not None:
                result["coverage"] = counts
            if status == "incomplete":
                result["incomplete"] = {"reason": reason, "rules": sorted({item["rule"] for item in findings or []}),
                                        "count": count if count is not None else len(findings or [])}
            self.results[unit.unit_id] = result
            return result

        def finding(rule: str, text: str, path: str = "") -> dict[str, Any]:
            return {"rule": rule, "path": path, "kinds": [], "reason": text}

        def judge(sample: dict[str, Any], trace: dict[str, str]) -> dict[str, Any]:
            coverage = records.declared_coverage(sample, scope, command_sha256=reference["command_sha256"])
            findings = coverage["undeclared"]
            verdict = "passed" if coverage["ok"] else (declared_audit_verdict(findings) if sampled else "failed")
            if verdict == "incomplete" and coverage["counts"]["undeclared"] > len(findings):
                verdict = "failed"   # 明细被截断：看不到的那部分里可能有集合之外的规则，只凭拿到的这批判不了「只是证据不完整」，失败关闭
            reason = None
            if verdict == "incomplete":
                reason = "；".join(f"{item['rule']} {item['path'] or item['reason']}"[:160] for item in findings[:3])
            return finish(verdict, findings=findings, count=coverage["counts"]["undeclared"], trace=trace, counts=coverage["counts"], reason=reason)

        if state.get("identity"):
            return finish("failed", findings=[finding("C7-command", state["identity"])])
        if state.get("degraded"):
            return finish("incomplete", findings=[], reason=state["degraded"])
        if sampled:
            outcome = state.get("outcome")
            raw_path, sample_path = state["raw"], state["sample"]
            if not raw_path.is_file():
                return finish("incomplete", findings=[], reason="filter-output-missing：过滤器没有产出采样结果（被中途清理或崩溃），读集没能证明")
            sampler = run.sampler_module()
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                problems = sampler.validate_raw(raw)
                if problems:
                    raise ValueError("；".join(problems[:3]))
                sample = _declared_sample_from_raw(
                    raw, unit_id=unit.unit_id, launcher=state["launcher"], command=state["command"], cwd_rel=state["cwd_rel"],
                    exit_code=getattr(outcome, "exit_code", None), seconds=float(getattr(outcome, "seconds", 0.0) or 0.0), tracked=run.tracked(),
                    commit=run.commit(), strace_version=run.strace_version(state["strace"], state["strace_env"]), sampler=sampler, records=records)
                sampler.write_document(sample_path, sample)
                digest = records.file_sha256(sample_path)
            except Exception as error:  # noqa: BLE001 - 组装证据出错是实现缺陷，响亮失败（C7-format），不降级
                return finish("failed", findings=[finding("C7-format", f"本次采样证据没能组装：{type(error).__name__}: {error}")])
            return judge(sample, {"path": str(sample_path), "sha256": digest})
        # α'：登记表证据。
        evidence_path = run.evidence_file(unit.unit_id, reference["evidence_sha256"]) if run is not None else None
        if evidence_path is None:
            return finish("failed", findings=[finding("evidence-missing", "找不到字节摘要等于范围条目登记的 evidence_sha256 的登记表证据文件")])
        try:
            data = evidence_path.read_bytes()
            evidence = json.loads(data.decode("utf-8"))
            command = evidence["command"]
            if evidence["unit_id"] != unit.unit_id:
                raise ValueError(f"证据属于别的单元：{evidence['unit_id']!r}")
            cwd_rel = self._declared_cwd_rel(unit)
            if list(unit.command) != list(command["launcher"]) + list(command["argv"]) or cwd_rel is None or (command["cwd_rel"] or ".") != cwd_rel:
                return finish("failed", findings=[finding("C7-command", "单元的命令行或工作目录不是登记表证据描述的那条命令（argv 不等于证据的 launcher＋argv，或 cwd_rel 不符）")])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            return finish("failed", findings=[finding("evidence-format", f"登记表证据读不出来或形状不对：{type(error).__name__}: {error}")])
        return judge(evidence, {"path": str(evidence_path), "sha256": reference["evidence_sha256"]})

    def declared_finish(self, item: "Running", outcome: "Outcome", audit: dict[str, Any]) -> None:
        """声明级单元回收后的收尾（调度器在 collect 之后调用）：带出运行前核验凭证 ``declared_contracts``，并把「这一次记录不可承接」的原因交给
        Recorder——前置核验没通过（缓存位置、安装锁、go.sum 初态、-count=1、镜像 Id、git 提交、离线变量）、逐次采样的证据不完整（含缺 strace 降级）。
        这一次执行的通过／失败结论不受影响。其它单元什么都不做。"""

        unit_id = item.unit.unit_id
        if not self.handles_declared(item.unit):
            return
        run = _DECLARED_RUN.get()
        preflight = run.preflights.pop(unit_id, None) if run is not None else None
        reasons: list[str] = []
        if preflight is None:
            reasons.append("declared-contract-preflight:该单元启动前没有做前置核验")
        else:
            if preflight["credentials"]:
                outcome.extra["declared_contracts"] = preflight["credentials"]
            reasons.extend(f"declared-contract-preflight:{problem}" for problem in preflight["problems"][:5])
        if audit.get("status") == "incomplete":
            reasons.append(f"declared-sample-incomplete:{(audit.get('incomplete') or {}).get('reason') or '证据不完整'}")
        if reasons:
            outcome.extra["declared_not_inheritable"] = "；".join(reasons)

    def _declared_report(self) -> dict[str, Any] | None:
        """read-audit.json 里的 declared 段（只有存在声明级结果时才有）：各状态的单元、证据来源计数、审计策略下放行的 rerun-only 单元。"""

        rows = {unit_id: result for unit_id, result in self.results.items() if result.get("coverage_scope") == DECLARED_COVERAGE_SCOPE}
        exempt = sorted(unit_id for unit_id, result in self.results.items() if result.get("declared_exempt"))
        if not rows and not exempt:
            return None
        of = lambda status: sorted(unit_id for unit_id, result in rows.items() if result.get("status") == status)  # noqa: E731
        return {"units": sorted(rows), "passed": of("passed"), "failed": of("failed"), "incomplete": of("incomplete"),
                "sample_origin": {origin: sorted(unit_id for unit_id, result in rows.items() if result.get("sample_origin") == origin)
                                  for origin in ("registry", "run")},
                "rerun_only_exempt": exempt}

    def declared_extra(self) -> dict[str, Any]:
        """汇总里 ``read_audit`` 段要追加的键（``declared``）：没有声明级结果时是空字典，汇总与改造前逐字相同。"""

        declared = self._declared_report()
        return {} if declared is None else {"declared": declared}

    def _declared_enforced(self, expected: set[str], missing: list[str], nonempty: bool) -> bool:
        """严格审计且结果里有声明级特例时，重新判定承接边界是否闭合。原判据只认「覆盖闭合且通过」与「带 reexecute-only-policy 的强制重跑」；
        这里在它们之外多认两类声明级结果——逐次采样证据不完整的 declared 单元（记录不可承接）、审计策略下放行的 rerun-only 单元（本来就不可承接）。
        原判据一字不改，只在它判不过、且存在这两类结果时才多看一遍。"""

        if not (self.strict and nonempty and not missing and set(self.results) == expected):
            return False
        for unit_id, result in self.results.items():
            current = self.currents[unit_id]
            if result.get("coverage_complete") is True and result.get("status") == "passed":
                continue
            if (result.get("status") == "reexecute_required" and result.get("coverage_complete") is False and current.inheritable is False
                    and any(entry.get("name") == "reexecute-only-policy" and entry.get("sha256") == result.get("reexecute_policy_sha256")
                            for entry in current.inputs or [])):
                continue
            if result.get("declared_exempt") == "rerun-only" and result.get("coverage_complete") is False and current.inheritable is False:
                continue
            if (result.get("status") == "incomplete" and result.get("coverage_scope") == DECLARED_COVERAGE_SCOPE
                    and result.get("coverage_complete") is False and result.get("undeclared_count") == 0):
                continue
            return False
        return True

    def report(self) -> dict[str, Any]:
        """写 ``audit/read-audit.json`` 并返回汇总里的那一段。"""

        flagged = sorted(unit_id for unit_id, result in self.results.items() if result.get("undeclared_count"))
        total = sum(int(result.get("undeclared_count") or 0) for result in self.results.values())
        skipped = sorted(unit_id for unit_id, result in self.results.items() if result.get("skipped"))
        # 要有审计结果的单元：本次执行的全部单元（没有承接时就是全部单元，与改造前相同）。
        expected = set(self.currents) - set(self.inherited)
        nonempty = bool(expected) or bool(self.inherited)
        missing = sorted(expected - set(self.results))
        complete = (self.strict and nonempty and set(self.results) == expected
                    and all(result.get("coverage_complete") is True and result.get("status") == "passed"
                            for result in self.results.values()))
        reexecute = sorted(unit_id for unit_id, result in self.results.items() if result.get("status") == "reexecute_required")
        enforced = (self.strict and nonempty and not missing and set(self.results) == expected
                    and all((result.get("coverage_complete") is True and result.get("status") == "passed") or
                            (unit_id in reexecute and result.get("coverage_complete") is False
                             and self.currents[unit_id].inheritable is False
                             and any(entry.get("name") == "reexecute-only-policy"
                                     and entry.get("sha256") == result.get("reexecute_policy_sha256")
                                     for entry in self.currents[unit_id].inputs or []))
                            for unit_id, result in self.results.items()))
        if self.strict and not enforced and any(result.get("declared_exempt") or result.get("status") == "incomplete"
                                                 for result in self.results.values()):
            # 声明级特例（逐次采样证据不完整、审计策略下放行的 rerun-only）：原判据不认识，另判一遍；原判据本身不改。
            enforced = self._declared_enforced(expected, missing, nonempty)
        path = self.dir / "read-audit.json"
        report = {"schema_version": self.module.REPORT_SCHEMA, "repo_root": self.repo_root, "data_root": self.data_root,
                  "data_root_exempt": dict(self.module.DATA_ROOT_EXEMPT),
                  "data_root_exempt_patterns": dict(self.module.DATA_ROOT_EXEMPT_PATTERNS), "units": self.results,
                  "units_with_findings": flagged, "undeclared_total": total, "skipped_whole_repo": skipped,
                  "coverage_complete": complete, "strict": self.strict, "missing_units": missing,
                  "reexecute_required_units": reexecute, "reuse_boundary_enforced": enforced}
        if self.inherited:
            report["inherited_units"] = sorted(self.inherited)
        declared = self._declared_report()
        if declared is not None:
            report["declared"] = declared  # 仅在存在声明级结果时追加；汇总里的同名键由 declared_extra 在调用处追加
        _write_json(path, report)
        return {"enabled": True, "report": str(path), "units": len(self.results) - len(skipped), "skipped_whole_repo": len(skipped),
                "units_with_findings": flagged[:50], "undeclared_total": total,
                "status": "failed" if total or (self.strict and not (complete or enforced)) else "passed",
                "coverage_complete": complete, "missing_units": missing, "reexecute_required_units": reexecute,
                "reuse_boundary_enforced": enforced}


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
        auditor: ReadAuditor | None = None,
        resume_validator: Any = None,
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
        self.auditor = auditor
        self.events_path = self.out_dir / "events.jsonl"
        self.reservation = Reservation(self.state_dir)
        self.running: dict[int, Running] = {}
        self.max_cores_in_use = 0.0
        self.resume_validator = resume_validator
        self.deferred: list[tuple[Unit, str]] = []
        # 补跑阶段用：单元 ID → 首次尝试的引用（见 Running.retry_of）。补跑阶段开始前由 ``_run_retry_phase`` 填好。
        self.retry_of: dict[str, dict[str, Any]] = {}
        self.attempts: dict[tuple[str, str], int] = {}
        self.interruptions: list[dict[str, Any]] = []
        self._ledger_at = time.monotonic()

    # -- 事件与预约 -----------------------------------------------------------
    def event(self, name: str, **payload: Any) -> None:
        record = {"t": round(time.time(), 3), "event": name, "cores_in_use": self.cores_in_use(), "running": sorted(r.unit.unit_id for r in self.running.values()), **payload}
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def abort(self) -> None:
        """调度器自身出错或被中断：终止全部在跑单元（整个会话），不留后台进程。"""

        # WP-R5：先取全部在跑单元的整棵子树（含另起会话的后代）快照、冻结、再统一 SIGKILL（见模块里「进程树快照与后代清理」），
        # 之后回收各单元根进程。进程表读不到或出错时回落到原来的按进程组和会话 SIGKILL，根进程一定会被杀。
        items = list(self.running.values())
        try:
            self._kill_items(items)
        finally:
            self._reap_roots(items)

    def _reap_roots(self, items: list[Running]) -> None:
        """回收各单元的根进程，**有界**：第二次 Ctrl-C 可能落在 ``_kill_items`` 取快照的阶段，那时根进程还活着，原来无条件
        ``waitpid(pid, 0)`` 会永久阻塞（基线只是把 KeyboardInterrupt 抛出去，不卡住）。所以先对仍未回收的根补发 SIGKILL（未回收
        的僵尸占着 pid，不会被复用），再用 WNOHANG 限时轮询回收；再被打断就直接抛出去，已发出的 SIGKILL 不丢。"""

        for item in items:
            if item.pid in self.running:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(item.pid, signal.SIGKILL)
        deadline = time.monotonic() + ABORT_REAP_SECONDS
        for item in items:
            while True:
                try:
                    waited, _status = os.waitpid(item.pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if waited != 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.running.pop(item.pid, None)

    def cores_in_use(self) -> float:
        return sum(item.unit.quota.cores for item in self.running.values())

    def memory_in_use(self) -> int:
        return sum(item.unit.quota.memory_mb for item in self.running.values())

    def honor_reservation(self) -> list[Outcome]:
        """停派、限时排空可重跑单元；只在原会话清理完成后授予同一预约。"""

        request = self.reservation.current()
        if request is None:
            return []
        token = _sha256(request)
        started = time.monotonic()
        self.event("reservation-requested", reservation_sha256=token, owner=request["owner"])
        finished: list[Outcome] = []
        while self.running:
            if self.reservation.current() != request:
                self.event("reservation-withdrawn", reservation_sha256=token)
                return finished
            finished.extend(self.reap(block=False))
            grace = self.config.reservation_grace_seconds
            if grace is not None and time.monotonic() - started >= grace:
                for item in list(self.running.values()):
                    if item.interruption is not None or item.timed_out or (item.unit.command and not item.unit.reservation_restartable):
                        continue
                    item.interruption = {"reservation_sha256": token, "owner": request["owner"],
                                         "status": "interrupted_for_reservation", "counted_as_failure": False,
                                         "requeue_required": True,
                                         "requested_at_utc": request.get("requested_at_utc"), "stopped_at_utc": _utc_now(),
                                         "grace_seconds": grace, "stop_seconds": self.config.reservation_stop_seconds}
                    item.stop_started = time.monotonic()
                    # macOS 上刚退出、尚未 wait 的进程组可能返回 EPERM；下一轮先回收，
                    # 真正仍存活且无法终止的单元会在 KILL 宽限后明确拒绝让出整机。
                    with contextlib.suppress(PermissionError):
                        self._signal_session(item.pid, signal.SIGTERM)
                    self.event("reservation-stop", unit=item.unit.unit_id, pid=item.pid, reservation_sha256=token)
            if self.running:
                time.sleep(POLL_SECONDS)
        if self.reservation.current() != request:
            return finished
        if not self.reservation.grant(f"scheduler-{os.getpid()}", expected=request):
            return finished
        self.event("reservation-granted", reservation_sha256=token)
        while self.reservation.current() == request:
            time.sleep(POLL_SECONDS)
        self.event("reservation-released", reservation_sha256=token)
        return finished

    def _signal_session(self, pid: int, sig: int) -> None:
        """主进程组和会话内其它组一起停止，避免只杀包装器就提前让出整机。"""
        item = self.running.get(pid)
        if item is not None:
            # WP-R5：**先取快照再发信号**。SIGKILL 档原来先杀进程组和会话成员、之后才在 ``_kill_items`` 里取快照：根进程与会话成员
            # 一死，环境被清空、又没来得及进名册的另起会话后代就改挂 init，再也找不到了。这里先把单元现在的全部存活进程记进名册
            # （pid＋启动时间，含按标记、按 ppid 子树找到的），后面的 ``_kill_items`` 靠名册照样认得出它们。
            self._remember_quietly(item)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sig)
        for member in _session_members(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(member, sig)
        # WP-R5：再加上单元自己另起会话的后代。SIGKILL 档走「冻结再终止」（_kill_items），其它信号发给当前能找到的全部存活进程。
        if item is None:
            return
        if sig == signal.SIGKILL:
            self._kill_items([item])
            return
        try:
            survivors = self._survivors(item)
        except Exception as error:   # noqa: BLE001 - 进程表读不到不能让预约中止的 SIGTERM 档半途崩掉；原因留事件
            with contextlib.suppress(OSError):
                self.event("tree-kill-error", error=repr(error), stage="signal-session")
            return
        for member, info in survivors.items():
            _signal_if_same(member, info, sig)

    def _remember_quietly(self, item: Running) -> None:
        """发信号之前先记名册；读进程表出错时只留事件、不抛——名册是锦上添花的依据，不能因为它拦住后面的信号。"""

        try:
            self._remember(item, tags=True)
        except Exception as error:   # noqa: BLE001
            with contextlib.suppress(OSError):
                self.event("tree-kill-error", error=repr(error), stage="remember")

    # -- 后代清理（WP-R5）---------------------------------------------------------
    def _survivors(self, item: Running, table: dict[int, ProcInfo] | None = None, *, tags: bool = True,
                   index: dict[str, set[int]] | None = None) -> dict[int, ProcInfo]:
        """单元现在还活着的全部进程（pid → 进程表行，不含僵尸、调用方自己和它的祖先）：单元根进程、单元会话成员、
        名册里仍是同一个进程的、环境带本单元标记的（``tags=False`` 时跳过，省掉逐进程读环境；多个单元一起找时传入同一份
        ``index``，只扫一遍环境），再加上它们的 ppid 子树。"""

        table = process_table() if table is None else table
        if not table:
            return {}
        # 根进程本身只在它还没被回收时才按 pid 认：回收之后这个 pid 可能已被别的进程复用（会话号不会：会话还有成员时 pid 不会被复用）。
        tracked = item.pid in self.running
        roots = {pid for pid, info in table.items() if (tracked and pid == item.pid) or info.sid == item.pid}
        roots |= {pid for pid, start in item.known.items() if pid in table and table[pid].start == start}
        if tags and item.tag:
            roots |= (tag_index(table) if index is None else index).get(item.tag, set())
        protected = protected_pids(table)
        return {pid: table[pid] for pid in descendants_of(table, roots)
                if pid not in protected and table[pid].state[:1] not in ("Z", "X")}

    def _remember(self, item: Running, table: dict[int, ProcInfo] | None = None, *, tags: bool = False,
                  index: dict[str, set[int]] | None = None) -> None:
        """把单元现在的存活进程记进后代名册（同时丢掉已经不在、或 pid 已被复用的旧条目）。发信号之前、超时、预约中止
        都要先调用：根进程一死，孙辈的 ppid 链就断了，名册是那之后唯一能认出环境被清空的孤儿的依据。"""

        table = process_table() if table is None else table
        alive = {pid: start for pid, start in item.known.items() if pid in table and table[pid].start == start}
        alive.update({pid: info.start for pid, info in self._survivors(item, table, tags=tags, index=index).items()})
        item.known = alive

    def _refresh_ledger(self) -> None:
        """每隔 ``LEDGER_REFRESH_SECONDS`` 秒给全部在跑单元刷新一次名册（一次进程表，不读环境）。"""

        now = time.monotonic()
        if not self.running or now - self._ledger_at < LEDGER_REFRESH_SECONDS:
            return
        self._ledger_at = now
        table = process_table()
        for item in self.running.values():
            self._remember(item, table)

    def _kill_items(self, items: list[Running]) -> None:
        """冻结再 SIGKILL 这些单元的全部存活进程（含根进程与另起会话的后代）。取快照、冻结、终止的细节见 ``kill_processes``。
        进程表读不到或出错时回落到原来的按进程组和会话杀：仍在被跟踪（尚未回收）的根进程一定会被杀——这一段放在 ``finally`` 里，
        第二次 Ctrl-C 之类的中断也不会跳过它。清理中遇到的告警（冻结后核对不了身份而改发 SIGCONT 等）写成 ``tree-kill-warning`` 事件。"""

        warnings: list[str] = []
        try:
            try:
                table = process_table()
                index = tag_index(table)
                for item in items:
                    self._remember(item, table, tags=True, index=index)   # 先于任何信号：之后根进程一死，ppid 链就断了

                def find() -> dict[int, ProcInfo]:
                    table = process_table()
                    index = tag_index(table)
                    return {pid: info for item in items for pid, info in self._survivors(item, table, index=index).items()}

                killed = kill_processes(find, warnings)
                if killed:
                    self.event("tree-kill", units=sorted(item.unit.unit_id for item in items), killed=len(killed))
            except Exception as error:   # noqa: BLE001 - 清理路径不能因为进程表出错而放过根进程；原因留事件，不吞
                with contextlib.suppress(OSError):
                    self.event("tree-kill-error", error=repr(error))
            if warnings:
                with contextlib.suppress(OSError):
                    self.event("tree-kill-warning", units=sorted(item.unit.unit_id for item in items), warnings=warnings)
        finally:
            for item in items:
                if item.pid in self.running:   # 已被回收的根进程，其 pid 可能已被复用，不再按 pid 发信号
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(item.pid, signal.SIGKILL)
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(item.pid, signal.SIGKILL)
                with contextlib.suppress(OSError):
                    for member in _session_members(item.pid):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.kill(member, signal.SIGKILL)

    def _resume_pending(self, kind: str) -> list[Unit]:
        if not self.deferred or self.reservation.current() is not None:
            return []
        if any(previous_kind != kind for _, previous_kind in self.deferred):
            raise ExecutorError("中止单元跨越正式／诊断阶段，拒绝重派")
        units = [unit for unit, _ in self.deferred]
        if self.resume_validator is not None:
            self.resume_validator(units)
        self.deferred.clear()
        for unit in units:
            self.event("reservation-requeued", unit=unit.unit_id, kind=kind)
        return units

    # -- 启动与回收 -----------------------------------------------------------
    def _declared_preflight(self, unit: Unit, kind: str) -> None:
        """声明级命令单元启动前的前置核验（只对要执行的正式单元做，见 declared_contract_preflight）。结果放进声明级上下文，回收后由
        ReadAuditor.declared_finish 取走。其它单元（含 control 四链）什么都不做。"""

        run = _DECLARED_RUN.get()
        if run is None or kind != "formal" or not unit.command:
            return
        current = self.recorder.currents.get(unit.unit_id)
        if _declared_scope_entry_of(current) is not None:
            run.preflights[unit.unit_id] = declared_contract_preflight(unit, current)

    def launch(self, unit: Unit, kind: str) -> None:
        safe = unit.unit_id.replace("#", "-").replace("!", "-")
        suffix = "" if kind == "formal" else f".{kind}"
        key = (unit.unit_id, kind)
        attempt = self.attempts[key] = self.attempts.get(key, 0) + 1
        if attempt > 1:
            suffix += f".resume-{attempt}"
        log_path = self.out_dir / "logs" / f"{safe}{suffix}.log"
        result_path = self.out_dir / "units" / f"{safe}{suffix}.result.json"
        record_path = self.out_dir / "units" / f"{safe}{suffix}.record.json"
        tests_path = self.out_dir / "units" / f"{safe}{suffix}.tests.json"
        # 单元自己的结构化旁路结果（命令单元可选；后端 go test 的按包结果等）。补跑时同时给出首次尝试的那一份，让命令按首次结果
        # 只补跑必要的部分。路径经 ``UNIT_EXECUTOR_`` 前缀的环境变量下发：不进环境指纹，也不在单元规格里。
        sidecar_path = self.out_dir / "units" / f"{safe}{suffix}.sidecar.json"
        first_sidecar_path = self.out_dir / "units" / f"{safe}.sidecar.json"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        for stale in (result_path, record_path, result_path.with_suffix('.observed-failure.json'),
                      result_path.with_suffix('.observed-failure.json.tmp')):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        with contextlib.suppress(FileNotFoundError):
            sidecar_path.unlink()
        if unit.command:
            # 命令单元：经 /bin/sh 切到单元的工作目录再 exec 原命令（posix_spawn 不能指定工作目录；exec 按 PATH 找命令）。
            argv = ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd or os.getcwd(), *unit.command]
        else:
            _write_json(tests_path, {"unit_id": unit.unit_id, "test_ids": list(unit.test_ids)})
            argv = self.unit_argv or [sys.executable, str(Path(__file__).resolve()), "run-unit"]
            argv = [*unit.launcher, *argv, "--start", str(self.start), "--tests-file", str(tests_path), "--result", str(result_path)]
        self._declared_preflight(unit, kind)
        if self.auditor is not None and kind == "formal":
            # 读集审计（E3-04）：strace 在最外层（启动前缀、切目录的 shell 都在它下面），轨迹经管道交给过滤器。
            argv = self.auditor.wrap(unit, argv)
        # 本次启动独有的标记，随环境传给单元的全部子孙（清理时认孤儿用，WP-R5）。外层调度器的单元里再起调度器时，外层标记
        # 已在环境里：接成「外层:自己」的标记链，外层按任一段都认得出内层单元的进程。
        tag = os.urandom(8).hex()
        parent_tags = os.environ.get(RUN_TAG_ENV, "")
        env = {
            **os.environ,
            # 单元内部并行度：默认按额度向上取整（至少 1）。单元自己显式给出的（如后端 Go 测试按 0.7 核排程、内部仍要
            # 两路并行编译与跑测试包）优先——调度额度管整机分配，内部并行度管单元自己怎么跑，两者可以不同。
            "UNIT_EXECUTOR_CORES": str(max(1, math.ceil(unit.quota.cores))),
            "GOMAXPROCS": str(max(1, math.ceil(unit.quota.cores))),
            **dict(unit.env),
            "UNIT_EXECUTOR_UNIT": unit.unit_id,
            "UNIT_EXECUTOR_KIND": kind,
            "UNIT_EXECUTOR_SIDECAR": str(sidecar_path),
            "UNIT_EXECUTOR_FIRST_SIDECAR": str(first_sidecar_path),
            RUN_TAG_ENV: f"{parent_tags}:{tag}" if parent_tags else tag,
            # 同目录的 unit_records.py：按闭包复制受管树的测试辅助（managed_tree_copy）在数据根里没有 tools/ci，靠它找到与
            # 输入声明同一份的闭包算法（E3-04）。UNIT_EXECUTOR_ 开头的变量不进环境指纹，也不在单元规格里。
            "UNIT_EXECUTOR_RECORDS_MODULE": str(Path(__file__).resolve().parent / "unit_records.py"),
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
        self.running[pid].tag = tag
        if kind == "retry":
            self.running[pid].retry_of = self.retry_of.get(unit.unit_id)
        self.running[pid].resumed_from = [row for row in self.interruptions if row["unit_id"] == unit.unit_id and row["kind"] == kind]
        self.max_cores_in_use = max(self.max_cores_in_use, self.cores_in_use())
        self.event("start", unit=unit.unit_id, kind=kind, pid=pid, cores=unit.quota.cores, attempt=attempt)

    def _clean_orphans(self, item: Running, *, interrupted: bool = False) -> list[int]:
        """单元根进程退出（已回收）之后清理它留下的全部残留：会话成员，以及另起会话的后代（按启动标记、名册和 ppid 子树找）。
        先给宽限期等它们自己退出；仍在的先 SIGTERM，1 秒后还在就冻结再 SIGKILL。返回宽限期过后仍存活的进程号（记入单元记录）。"""

        deadline = time.monotonic() + (0 if interrupted else self.config.orphan_grace_seconds)
        members = self._survivors(item)
        while members and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            members = self._survivors(item)
        orphans = sorted(members)
        if members:
            # 留证：单元记录里 ``orphans`` 只有个数，而 ``capture_ci`` 对任何孤儿判分片失败；残留是哪些命令要在事件流里能查到
            # （WP-R5 起另起会话的残留也计入，第一次在 ARM64／CI 全量跑时靠它定位既有测试的泄漏）。
            with contextlib.suppress(Exception):
                self.event("orphans", unit=item.unit.unit_id, pids=orphans, commands=command_lines(orphans))
            self._remember_quietly(item)
            for pid, info in members.items():
                _signal_if_same(pid, info, signal.SIGTERM)
            if self._survivors(item):
                time.sleep(1.0)
            self._kill_items([item])
        return orphans

    def reap(self, *, block: bool) -> list[Outcome]:
        finished: list[Outcome] = []
        while True:
            self._refresh_ledger()
            for pid, item in list(self.running.items()):
                elapsed = time.monotonic() - item.started
                # 单元自带超时（命令单元）优先，否则用调度配置的统一超时；先 SIGTERM，30 秒后仍在就整棵子树 SIGKILL。
                limit = item.unit.timeout_seconds or self.config.unit_timeout_seconds
                if item.stop_started is None and elapsed > limit and not item.timed_out:
                    item.timed_out = True
                    self._remember_quietly(item)   # 先记名册再发信号：根进程一死，另起会话的后代就只能靠名册和标记认
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(pid, signal.SIGTERM)
                    self.event("timeout", unit=item.unit.unit_id, pid=pid)
                elif item.timed_out and elapsed > limit + TIMEOUT_KILL_GRACE_SECONDS:
                    self._kill_items([item])
                try:
                    waited, status, usage = os.wait4(pid, os.WNOHANG)
                except ChildProcessError:
                    waited, status, usage = pid, 0, None
                if waited == 0:
                    if item.stop_started is not None and time.monotonic() - item.stop_started >= self.config.reservation_stop_seconds:
                        try:
                            self._signal_session(pid, signal.SIGKILL)
                        except PermissionError:
                            if time.monotonic() - item.stop_started > self.config.reservation_stop_seconds + 2:
                                raise ExecutorError("预约中止无法终止仍存活单元，拒绝授予整机")
                    continue
                del self.running[pid]
                orphans = self._clean_orphans(item, interrupted=item.interruption is not None)
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
                interrupted = (item.interruption is not None and
                               (exit_code in (0, 128 + signal.SIGTERM, 128 + signal.SIGKILL, -signal.SIGTERM, -signal.SIGKILL)))
                if item.interruption is not None:
                    # 无论原单元是否已经失败，都须核对清理后（含另起会话的后代）不再有活跃进程才能授予整机；僵尸与已退出的不算。
                    active = sorted(self._survivors(item))
                    if active:
                        raise ExecutorError(f"预约中止后会话仍有活跃进程：{active}")
                if interrupted and not item.unit.command:
                    journal = item.result_path.with_suffix('.observed-failure.json')
                    incomplete = journal.with_name(journal.name + '.tmp')
                    if journal.exists() or incomplete.exists():
                        observed = journal if journal.exists() else incomplete
                        partial = _read_json_file(observed)
                        # 原子发布被打断也保留失败；半份失败日志不允许被当成无失败而重跑成通过。
                        if partial is not None and partial.get('unit_id') == item.unit.unit_id:
                            outcome.result = outcome.result or partial
                        outcome.extra['observed_failure'] = {'path': str(observed),
                            'sha256': self.recorder.records.file_sha256(observed),
                            'reservation': {**item.interruption, 'status': 'failed_before_interruption',
                                            'counted_as_failure': True, 'requeue_required': False}}
                        interrupted = False
                if interrupted:
                    # 没有已登记失败的预约中止单独留档，重派从头执行。
                    outcome.extra["reservation_interruption"] = {**item.interruption, "active_session_members": active}
                    if self.auditor is not None:
                        trace = self.auditor.trace_path(item.unit)
                        if trace.is_file():
                            saved = item.record_path.with_suffix('.interrupted-trace.json')
                            shutil.copyfile(trace, saved)
                            outcome.extra["reservation_interruption"]["partial_trace"] = {"path": str(saved), "sha256": self.recorder.records.file_sha256(saved)}
                elif self.auditor is not None and self.auditor.declared_only and not self.auditor.handles_declared(item.unit):
                    pass  # 只为声明级单元建的审计器：其它单元不审计，记录里没有 read_audit
                elif self.auditor is not None and item.kind == "formal":
                    self.auditor.note_outcome(outcome)
                    audit = self.auditor.collect(item.unit)
                    outcome.extra["read_audit"] = audit
                    outcome.extra["reexecute_only"] = audit.get("status") == "reexecute_required"
                    self.auditor.declared_finish(item, outcome, audit)
                self.recorder.write(item, outcome)
                self.event("exit", unit=item.unit.unit_id, kind=item.kind, pid=pid, exit_code=outcome.exit_code, signal=outcome.signal,
                           timed_out=outcome.timed_out, orphans=len(orphans), seconds=outcome.seconds)
                if interrupted:
                    self.deferred.append((item.unit, item.kind))
                    self.interruptions.append({"unit_id": item.unit.unit_id, "kind": item.kind,
                                               "record_path": outcome.extra["record_path"], "record_sha256": outcome.extra["record_sha256"],
                                               **outcome.extra["reservation_interruption"]})
                else:
                    finished.append(outcome)
            if finished or not block or not self.running or self.reservation.current() is not None:
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
        while pending or self.running or self.deferred:
            outcomes.extend(self.honor_reservation())
            pending.extend(self._resume_pending(kind))
            if self.reservation.current() is not None:
                continue
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
        pending = sorted(units, key=lambda u: u.unit_id)
        while pending or self.running or self.deferred:
            outcomes.extend(self.honor_reservation())
            pending = self._resume_pending(kind) + pending
            if self.reservation.current() is not None:
                continue
            if pending and not self.running:
                self.launch(pending.pop(0), kind)
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
# 失败单元补跑（优化第 2 项，规则与限制见 retry_policy.py）
# ---------------------------------------------------------------------------


def classify_outcome(outcome: Outcome) -> str | None:
    """一次正式执行未通过时的失败类别（执行失败／超时被杀／读集未闭合／环境失败），通过返回 None。"""

    audit = outcome.extra.get("read_audit")
    return _retry_module().classify_failure(
        is_command=bool(outcome.unit.command), exit_code=outcome.exit_code, signal=outcome.signal, timed_out=outcome.timed_out,
        has_result=outcome.result is not None, result_successful=bool((outcome.result or {}).get("successful")),
        audit_failed=outcome.execution_passed and not outcome.passed and audit is not None,
        observed_failure=bool(outcome.extra.get("observed_failure")),
    ) if not outcome.passed else None


def _sidecar_note(outcome: Outcome) -> str | None:
    """命令单元旁路结果里的 ``retry_note``（补跑范围的一句话说明，如 go 包装器的“缩到用例＋整包确认”）；没有或读不出来返回 None。"""

    sidecar = outcome.log_path.parent.parent / "units" / (outcome.log_path.name.removesuffix(".log") + ".sidecar.json")
    try:
        note = json.loads(sidecar.read_text(encoding="utf-8")).get("retry_note")
    except (OSError, ValueError, AttributeError):
        return None
    return note if isinstance(note, str) and note.strip() else None


def _attempt_ref(records: Any, outcome: Outcome) -> dict[str, Any]:
    digest = records.path_digest(outcome.log_path) if outcome.log_path.exists() else None
    reference = _retry_module().attempt_ref(
        kind=outcome.kind, passed=outcome.passed, exit_code=outcome.exit_code, signal=outcome.signal, timed_out=outcome.timed_out,
        seconds=outcome.seconds, record_sha256=outcome.extra.get("record_sha256"), record_path=outcome.extra.get("record_path"),
        log_path=str(outcome.log_path), log_sha256=digest)
    note = _sidecar_note(outcome)
    if note is not None:
        reference["note"] = note
    return reference


def _run_retry_phase(scheduler: "Scheduler", formal: list[Outcome], *, store: Any, run_id: str, config: Any,
                     audited: Any) -> tuple[list[Outcome], list[Outcome], list[Unit], dict[str, dict[str, Any]]]:
    """并行段与独占段都跑完、机器腾空之后，对失败的单元补跑一次。

    返回（最终的正式结论列表, 补跑尝试列表, 仍要做不改结论的诊断重跑的单元, 单元 ID → 补跑标注）。补跑通过的单元用补跑尝试
    顶替首次失败的那一次（行里带标注）；补跑仍红、被拒绝、失败类别不可补跑的保持首次失败，同样带标注。补跑尝试一个一个单独
    跑（``run_alone``），期间不派发别的单元，所以满载引起的抖动不会再重演。"""

    policy, records = _retry_module(), scheduler.recorder.records
    ledger = None
    if store is not None:
        ledger = policy.RetryLedger(policy.default_ledger_root(store.root))
    markers: dict[str, dict[str, Any]] = {}
    pending: dict[str, tuple[Outcome, str, dict[str, Any] | None]] = {}
    for outcome in formal:
        failure_class = classify_outcome(outcome)
        if failure_class is None:
            continue
        unit = outcome.unit
        first = _attempt_ref(records, outcome)
        if failure_class != policy.CLASS_EXECUTION:
            markers[unit.unit_id] = policy.build_marker(
                status=policy.STATUS_NOT_RETRIABLE, failure_class=failure_class,
                reason=f"{policy.CLASS_LABELS[failure_class]}不补跑，保持原来的行为（只做不改结论的诊断重跑）", first_attempt=first,
                retry_attempt=None, ledger=None)
            continue
        allowance: dict[str, Any] | None = None
        denied = None
        if ledger is None:
            denied = "没有记录库，无法累计补跑次数，不补跑"
        elif unit.command and not unit.reservation_restartable:
            denied = "命令单元不是可从头重跑的（pre-A3 认证场景等有副作用的单元不补跑）"
        elif audited(unit):
            denied = "本次运行对该单元做读集审计，补跑得不到完整读集，不补跑"
        else:
            try:
                allowance = ledger.allowance(unit.unit_id, window_rounds=config.window_rounds, pass_limit=config.pass_limit)
            except policy.LedgerError as error:
                denied = f"补跑账本不可用，按拒绝处理：{error}"
            else:
                if not allowance["allowed"]:
                    denied = (f"最近 {allowance['window_rounds']} 轮内已补跑通过 {allowance['prior_retry_passed']} 次"
                              f"（上限 {allowance['pass_limit']} 次），这次再红直接判红、不再补跑")
        if denied is not None:
            markers[unit.unit_id] = policy.build_marker(status=policy.STATUS_DENIED, failure_class=failure_class, reason=denied,
                                                        first_attempt=first, retry_attempt=None, ledger=allowance)
            continue
        pending[unit.unit_id] = (outcome, failure_class, allowance)
        scheduler.retry_of[unit.unit_id] = {"record_sha256": first["record_sha256"], "record_path": first["record_path"],
                                            "log_sha256": first["log_sha256"], "failure_class": failure_class}
    retried: list[Outcome] = []
    if pending:
        # 机器此刻是空的（上面两段都已跑完）：逐个单独补跑。
        retried = scheduler.run_alone([item[0].unit for item in pending.values()], "retry")
    retry_by_unit = {outcome.unit.unit_id: outcome for outcome in retried}
    final: list[Outcome] = []
    entries: list[dict[str, Any]] = []
    for outcome in formal:
        unit_id = outcome.unit.unit_id
        if unit_id in pending:
            first_outcome, failure_class, allowance = pending[unit_id]
            attempt = retry_by_unit.get(unit_id)
            if attempt is None:
                raise ExecutorError(f"补跑阶段没有拿到单元 {unit_id} 的补跑结果")
            passed = attempt.passed
            attempt.extra["retry_of"] = scheduler.retry_of[unit_id]
            retry_attempt = _attempt_ref(records, attempt)
            scope_note = f"（{retry_attempt['note']}）" if retry_attempt.get("note") else ""
            marker = policy.build_marker(
                status=policy.STATUS_PASSED if passed else policy.STATUS_FAILED, failure_class=failure_class,
                reason=(("首次执行失败，机器腾空后单独补跑一次通过：结论算通过，但单列为补跑通过（两次日志都留）" if passed
                         else "首次执行失败，补跑仍然失败：判红") + scope_note),
                first_attempt=_attempt_ref(records, first_outcome), retry_attempt=retry_attempt, ledger=allowance)
            markers[unit_id] = marker
            winner = attempt if passed else first_outcome
            winner.extra["retry"] = marker
            final.append(winner)
            entries.append({"unit_id": unit_id, "run_id": run_id, "completed_at_utc": _utc_now(),
                            "outcome": policy.STATUS_PASSED if passed else policy.STATUS_FAILED, "failure_class": failure_class})
            continue
        if unit_id in markers:
            outcome.extra["retry"] = markers[unit_id]
        final.append(outcome)
    if ledger is not None and entries:
        # 补跑的结局立即落账（不等整轮结束）：整轮中途崩溃也不会让“补跑通过过一次”的记忆丢失而被绕过累计规则。
        ledger.append(entries)
    diagnostic_units = [outcome.unit for outcome in formal if not outcome.execution_passed and outcome.unit.unit_id not in pending]
    return final, retried, diagnostic_units, markers


CORE_DEVIATION_SCHEMA = "unit-executor-core-deviation/v1"
CORE_DEVIATION_THRESHOLD = 0.30
CORE_DEVIATION_MIN_SECONDS = 5.0


def core_deviation_report(outcomes: list[Outcome], *, threshold: float = CORE_DEVIATION_THRESHOLD,
                          min_seconds: float = CORE_DEVIATION_MIN_SECONDS) -> dict[str, Any]:
    """核数偏差报告（优化第 6 项）。只看本次真正执行的单元（承接的没有用量）；耗时不足 ``min_seconds`` 的不计。偏差＝实测核数／声明核数－1，
    绝对值超过 ``threshold`` 的单元列进 ``flagged``（按偏差绝对值从大到小）。``overall_ratio`` 是整轮实测核·秒／声明核·秒（用量到位的单元合计）。"""

    flagged: list[dict[str, Any]] = []
    measured_units = skipped = 0
    declared_total = measured_total = 0.0
    for outcome in outcomes:
        if outcome.extra.get("disposition", "executed") != "executed":
            continue
        declared, seconds = float(outcome.unit.quota.cores), float(outcome.seconds)
        if seconds < min_seconds or declared <= 0:
            skipped += 1
            continue
        measured = float(outcome.cpu_seconds) / seconds
        measured_units += 1
        declared_total += declared * seconds
        measured_total += float(outcome.cpu_seconds)
        deviation = measured / declared - 1
        if abs(deviation) > threshold:
            flagged.append({"unit_id": outcome.unit.unit_id, "declared_cores": declared, "measured_cores": round(measured, 3),
                            "deviation": round(deviation, 3), "direction": "实测高于声明（额度偏小）" if deviation > 0 else "实测低于声明（额度偏大）",
                            "seconds": round(seconds, 3), "cpu_seconds": round(float(outcome.cpu_seconds), 3)})
    flagged.sort(key=lambda row: (-abs(row["deviation"]), row["unit_id"]))
    return {"schema_version": CORE_DEVIATION_SCHEMA, "threshold": threshold, "min_seconds": min_seconds, "units_measured": measured_units,
            "units_too_short": skipped, "units_flagged": len(flagged),
            "declared_core_seconds": round(declared_total, 3), "measured_core_seconds": round(measured_total, 3),
            "overall_ratio": round(measured_total / declared_total, 3) if declared_total else None, "flagged": flagged}


def _write_core_deviation(out_dir: Path, outcomes: list[Outcome]) -> None:
    report = core_deviation_report(outcomes)
    _write_json(out_dir / "core-deviation.json", report)
    ratio = "无" if report["overall_ratio"] is None else f"{report['overall_ratio']:.2f}"
    print(f"核数偏差：{report['units_flagged']}／{report['units_measured']} 个单元的实测核数偏离声明超过 {int(report['threshold'] * 100)}%"
          f"（整轮实测／声明 {ratio}）；报告 {out_dir / 'core-deviation.json'}", file=sys.stderr)


BINDING_SCHEMA = "unit-executor-binding/v1"


def binding_document(*, mode: str, policy: str, environment_sha256: str, executor: dict[str, Any], units: list[Unit], currents: dict[str, Any],
                     gates: list[Gate]) -> dict[str, Any]:
    """运行绑定（优化第 5 项）：这次运行“测什么、怎么测、在什么环境测”的摘要。门禁集合按门禁项（单元与测试组成员、不在本平台执行项）算；输入清单按单元
    逐个取规格摘要、输入摘要（不可承接的单元没有输入摘要，记 ``-``）与可否承接，按单元 ID 排序后算。同一棵测试树、同一份环境上两次算出来必须相同。"""

    rows = sorted([unit.unit_id, currents[unit.unit_id].spec_sha256, currents[unit.unit_id].inputs_sha256 or "-",
                   bool(currents[unit.unit_id].inheritable)] for unit in units)
    gate_rows = sorted(({"gate_id": gate.gate_id, "units": sorted(gate.units), "test_groups": sorted(gate.test_groups),
                         "not_executed": [dict(item) for item in gate.not_executed]} for gate in gates), key=lambda row: row["gate_id"])
    return {"schema_version": BINDING_SCHEMA, "mode": mode, "policy_sha256": policy, "environment_sha256": environment_sha256,
            "executor_sha256": executor["sha256"], "gate_set_sha256": _sha256(gate_rows), "inputs_manifest_sha256": _sha256(rows), "units": len(rows)}


def _write_binding(out_dir: Path, **fields: Any) -> None:
    _write_json(out_dir / "binding.json", binding_document(**fields))


def _attach_retry_summary(summary: dict[str, Any], outcomes: list[Outcome], gates: list[Gate], config: Any) -> None:
    """打开补跑的运行：门禁项行带 ``retry_passed_units``（门禁项通过，但其中这几个单元是补跑才通过的，单列不静默放行），汇总带 ``retry`` 段。
    作为汇总之后的后处理而不是改汇总函数：补跑关闭时汇总与改造前逐字相同。"""

    passed = {o.unit.unit_id for o in outcomes if (o.extra.get("retry") or {}).get("status") == "retry-passed"}
    for gate_row, gate in zip(summary["gates"], gates):
        members = list(gate.units) + [row["unit_id"] for row in summary["units"] if row.get("group_id") in gate.test_groups]
        gate_row["retry_passed_units"] = sorted(unit_id for unit_id in members if unit_id in passed)
    summary["retry"] = _retry_module().summarize_markers(summary["units"], config=config)


def _manifest_with_retries(records: Any, manifest: dict[str, Any], attempts: list[Outcome]) -> dict[str, Any]:
    """运行清单另列 ``retries``：逐个补跑过的单元写明补跑尝试与首次尝试各自的记录（补跑通过的单元在 ``units`` 里引用补跑尝试的记录并带标注）。
    重新封存清单自摘要；补跑关闭时不调用，清单与改造前逐字相同。"""

    retries = [{"unit_id": o.unit.unit_id, "record_sha256": o.extra.get("record_sha256"), "record_path": o.extra.get("record_path"),
                "passed": o.passed, "of": (o.extra.get("retry_of") or {}).get("record_sha256"),
                "of_record_path": (o.extra.get("retry_of") or {}).get("record_path")} for o in attempts]
    return records.seal({**{key: value for key, value in manifest.items() if key != "manifest_sha256"}, "retries": retries}, "manifest_sha256")


# ---------------------------------------------------------------------------
# 按客户端筛选测试单元（Codex 升级工具优化第 9 项）
# ---------------------------------------------------------------------------

CLIENT_SELECTION_SCHEMA = "unit-executor-gates-selection/v1"


def _client_selection_checked(manifest: dict[str, Any], args: argparse.Namespace, records: Any, *, request_path: Any,
                              unit_inherit_mode: bool) -> dict[str, Any] | None:
    """门禁清单带 ``selection`` 块（``tools/ci/unit_ownership.py select`` 算出，``entry_gates.py plan`` 写入）时先做自检并核对运行条件；没有则返回 None，
    执行器行为与改造前逐字相同。筛选后的运行只是"全集的一个子集"，所以只许重新执行全集模式：承接、B-09／B-10 全集请求、逐单元承接、读集合同、
    收窄提案都拒绝（子集不能冒充全集，也不能拿子集去承接别人的结论）。"""

    selection = manifest.get("selection")
    if selection is None:
        return None
    if not isinstance(selection, dict) or selection.get("schema_version") != CLIENT_SELECTION_SCHEMA:
        raise ExecutorError("门禁清单的 selection 格式非法")
    body = {key: value for key, value in selection.items() if key != "selection_sha256"}
    if selection.get("selection_sha256") != _sha256(body):
        raise ExecutorError("门禁清单的 selection 自摘要不符（被改过）")
    modules = selection.get("modules")
    if (not isinstance(modules, dict) or not modules or set(modules.values()) - {"run", "skip"}
            or selection.get("scope") not in ("client-plan", "full")):
        raise ExecutorError("门禁清单的 selection 模块表或范围非法")
    if args.mode != records.RE_EXECUTE or request_path or unit_inherit_mode or args.unit_inherit_propose is not None:
        raise ExecutorError("按客户端筛选的清单只许 --mode re-execute：子集不能承接，也不能用来承接别人的结论")
    if (manifest.get("audit_policy") is not None or manifest.get("test_input_scopes")
            or any("input_scope" in item for item in manifest.get("units", []))):
        raise ExecutorError("按客户端筛选的清单不能同时带读集合同或收窄提案")
    return selection


def _apply_client_selection(selection: dict[str, Any], grouped: dict[str, list[str]], planned: list[Unit]) -> tuple[dict[str, list[str]], list[Unit]]:
    """在 ``plan_units`` 之后过滤：保留 selection 标 run 的模块。必须放在规划之后（规划要校验拆块、独占名单、超时登记的模块都存在），
    且 selection 的模块集合必须恰好等于 discover 出的全集（去掉 ``+pkg`` 后缀）——新增测试模块没登记、测试被删而表陈旧，都当场拦下。
    单元 ID、规格、额度不变，所以记录与全集运行的记录逐字相通。"""

    def base(name: str) -> str:
        return name.removesuffix("+pkg")

    modules = selection["modules"]
    discovered = {base(name) for name in grouped}
    if set(modules) != discovered:
        raise ExecutorError("selection 的模块集合与测试树的全集身份不匹配：多 "
                            f"{sorted(set(modules) - discovered)[:5]}，少 {sorted(discovered - set(modules))[:5]}")
    keep = {name for name, state in modules.items() if state == "run"}
    if not keep:
        raise ExecutorError("selection 一个模块都不保留")
    return ({name: ids for name, ids in grouped.items() if base(name) in keep},
            [unit for unit in planned if base(unit.module) in keep])


def _client_selection_summary(selection: dict[str, Any]) -> dict[str, Any]:
    """汇总里回显的筛选摘要（完整的 selection 在运行清单里）。"""

    modules = selection["modules"]
    return {"client": selection.get("client"), "scope": selection.get("scope"), "base_commit": selection.get("base_commit"),
            "head_commit": selection.get("head_commit"), "selection_sha256": selection.get("selection_sha256"),
            "ownership_sha256": selection.get("ownership_sha256"), "include_claude": selection.get("include_claude"),
            "modules_total": len(modules), "modules_run": sum(1 for state in modules.values() if state == "run"),
            "modules_skipped": sum(1 for state in modules.values() if state == "skip"),
            "modules_pulled": len(selection.get("pulled") or []), "advisories": len(selection.get("advisories") or [])}


def _record_round_in_ledger(final: list[Outcome], *, store: Any, run_id: str, already: set[str]) -> None:
    """整轮结束：把其余真正执行过的单元的结局（通过／失败）也记进账本，窗口里的“轮”才数得准（承接的单元这一轮没有执行，不记）。"""

    if store is None:
        return
    policy = _retry_module()
    ledger = policy.RetryLedger(policy.default_ledger_root(store.root))
    ledger.append({"unit_id": outcome.unit.unit_id, "run_id": run_id, "completed_at_utc": outcome.completed_at_utc or _utc_now(),
                   "outcome": policy.OUTCOME_PASSED if outcome.passed else policy.OUTCOME_FAILED}
                  for outcome in final
                  if outcome.unit.unit_id not in already and outcome.extra.get("disposition", "executed") == "executed")


# ---------------------------------------------------------------------------
# 汇总与全集核对
# ---------------------------------------------------------------------------


def _disposition(outcome: Outcome) -> dict[str, Any]:
    """汇总行里的处置（E3-01）：本次执行还是承接，记录摘要；承接的另带原运行。"""

    row = {"disposition": outcome.extra.get("disposition", "executed"), "record_sha256": outcome.extra.get("record_sha256")}
    if outcome.extra.get("inherited_from"):
        row["inherited_from"] = outcome.extra["inherited_from"]
    if outcome.extra.get("retry"):
        # 补跑标注（优化第 2 项，只有打开补跑的运行才有）：补跑通过、补跑仍红、补跑被拒绝、失败类别不可补跑，逐单元写明。
        row["retry"] = outcome.extra["retry"]
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
            p.add_argument("--selection-file", type=Path, default=None,
                           help="CI 分片的测试 ID 选择件；先校验全量身份与调度配置，只允许选择完整执行单元")
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
            p.add_argument("--shared-caches", choices=("auto", "bytecode", "off"), default="auto",
                           help="共享缓存：auto 准备字节码和身份记忆化；bytecode 只准备字节码并清除外部身份记忆目录；off 都不准备（单元按原环境运行，诊断用）")
            p.add_argument("--bytecode-helper", type=Path, default=DEFAULT_BYTECODE_HELPER, help="字节码共享层的预编译工具（测试与诊断用）")
            p.add_argument("--identity-memo", choices=("auto", "off"), default=None,
                           help="独立控制身份记忆化；省略时沿用 shared-caches 模式，off 清除外部身份目录")
            p.add_argument("--bytecode-source", type=Path, action="append", default=None, help="预编译进共享层的源码目录，可重复；默认 tools")
        if name == "run-gates":
            # E3-01：记录库与两种模式（方案 D12）。缺省重新执行全集；B-09 入口只有完整承接请求通过后才承接。
            p.add_argument("--record-store", type=Path, default=None, help="单元执行记录库：记录与日志入库，全集通过模式从这里找可承接的记录")
            p.add_argument("--mode", choices=("re-execute", "full-set-pass", "unit-inherit"), default="re-execute",
                           help="re-execute：重新执行全集，不承接；full-set-pass：全集通过，承接有效记录、只执行其余单元（要给记录库）；"
                                "unit-inherit：逐单元承接（B5a-08，要给 --unit-inherit-request 与记录库，合同见 unit_inherit.py）")
            p.add_argument("--inheritance-max-age-hours", type=float, default=168.0, help="承接期限（小时），默认 168（7 天），只能调小")
            p.add_argument("--decide-only", action="store_true",
                           help="只判定每个单元承接还是执行（写 decisions.json 并打印），不执行、不写记录、不占调度锁")
            p.add_argument("--target-platform-request", type=Path, help="B-10 目标平台单元承接请求；不得与 B-09 请求混用")
            p.add_argument("--full-set-request", type=Path, help="B-09 全集承接请求；任一条件失效则全量重跑")
            p.add_argument("--full-set-deployment", type=Path, help="入口本次核对的实际部署收据")
            # B5a-08：逐单元承接。入口门禁 entry-gates.sh --mode unit-inherit 给出下面几项；没有请求时整次重新执行。
            p.add_argument("--unit-inherit-request", type=Path, help="B5a-08 逐单元承接请求（unit-inherit-request/v1），只与 --mode unit-inherit 同用")
            p.add_argument("--unit-inherit-client", default=None, help="本轮客户端（承接上下文的一项）")
            p.add_argument("--unit-inherit-stage", default="pre-ledger", help="入口阶段：pre-ledger、post-ledger 或 formal:<campaign_id>")
            p.add_argument("--unit-inherit-control", type=Path, default=None, help="数据根的 control 目录：现算当前部署收据集合")
            p.add_argument("--unit-inherit-propose", type=Path, default=None,
                           help="只判定时把承接提案（当前上下文、来源集合、批准要签的绑定）写到这个新目录")
            # E3-04：读集审计。真跑才有读集，只许与 --mode re-execute 同用；要求 PATH 里有 strace（Linux）。
            p.add_argument("--audit-reads", action="store_true",
                           help="读集审计：正式执行的单元包在 strace 下，核对实际读取都在声明的输入范围里，超出即判失败（只许 re-execute）")
            p.add_argument("--audit-data-root", type=Path, default=None,
                           help="数据根：测试树单元读到这里一律报出；工作目录在这里的单元（pre-A3 场景）按与仓库同布局核对")
            p.add_argument("--audit-strict", action="store_true", help="全路径严格审计；宿主输入未绑定或解析不完整时拒绝通过")
            p.add_argument("--audit-host-inputs", type=Path, help="逐单元精确宿主输入清单，运行前生成快照并要求完整读集重放")
            # 声明级承接（declared-inputs/v1，步骤 5）：读集证据的来源。
            p.add_argument("--declared-sample", type=declared_sample_mode, default="off", metavar="off|always|every:N",
                           help="声明级命令单元的读集证据来源：off（缺省）用登记表里的采样证据，零运行时开销；always 每次执行都带 strace 采样，用本次采样证明读集"
                                "仍在声明范围内（覆盖失败判失败；只因证据不完整则该次记录不可承接、命令结论不变）；every:N 按记录库里该单元已有的正式记录数，每 N 次采样一次")
    unit = sub.add_parser("run-unit")
    unit.add_argument("--start", type=Path, required=True)
    test_source = unit.add_mutually_exclusive_group(required=True)
    test_source.add_argument("--tests-file", type=Path)
    test_source.add_argument("--runtime-tests-json", help="受管隔离测试载荷；只写私有盘的固定结果与失败旁路")
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
    selection_path = getattr(args, "selection_file", None)
    args.selection_binding = None
    if selection_path is not None:
        if selection_path.is_symlink() or not selection_path.is_file():
            raise ExecutorError("分片选择件必须是普通文件")
        raw = selection_path.read_bytes()
        try:
            selection = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise ExecutorError("分片选择件不是合法 JSON") from error
        expected = sorted(test_id for ids in grouped.values() for test_id in ids)
        if (not isinstance(selection, dict)
                or set(selection) != {"schema_version", "full_test_ids_sha256", "test_ids"}
                or selection["schema_version"] != "unit-executor-selection/v1"
                or selection["full_test_ids_sha256"] != _sha256(expected)):
            raise ExecutorError("分片选择件的全量测试身份不匹配")
        selected = selection["test_ids"]
        if (not isinstance(selected, list) or not selected or not all(isinstance(item, str) for item in selected)
                or len(selected) != len(set(selected)) or not set(selected) <= set(expected)):
            raise ExecutorError("分片测试 ID 缺失、重复或超出全集")
        selected = set(selected)
        if any(selected.intersection(unit.test_ids) and not set(unit.test_ids) <= selected for unit in units):
            raise ExecutorError("分片选择件不能截断已规划的执行单元")
        units = [unit for unit in units if set(unit.test_ids) <= selected]
        grouped = {module: [test_id for test_id in ids if test_id in selected]
                   for module, ids in grouped.items() if selected.intersection(ids)}
        scope = "shard"
        args.selection_binding = {"path": str(selection_path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                                  "full_test_ids_sha256": selection["full_test_ids_sha256"],
                                  "selected_test_ids_sha256": _sha256(sorted(selected))}
    return config, weights, durations, parallelism, cores, grouped, units, policy_digest(config, weights, durations, parallelism), scope


def _out_dir(args: argparse.Namespace) -> Path:
    """执行记录目录：参数优先，其次环境变量 UNIT_EXECUTOR_OUT_DIR（门禁脚本把记录放到自己的日志旁边），最后临时目录。"""

    out_dir = args.out_dir or (Path(os.environ["UNIT_EXECUTOR_OUT_DIR"]) if os.environ.get("UNIT_EXECUTOR_OUT_DIR") else None) \
        or Path(tempfile.gettempdir()) / "unit-executor-runs" / time.strftime("%Y%m%dt%H%M%Sz", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _prepare_shared_caches(args: argparse.Namespace, out_dir: Path, scheduler: Scheduler) -> tuple[dict[str, Any], str | None]:
    """单元之前准备缓存并计入总时长；bytecode 模式独立关闭身份记忆化，off 保留原诊断行为。"""

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
    memo_mode = args.identity_memo or {"auto": "auto", "bytecode": "off", "off": "inherit"}[args.shared_caches]
    if memo_mode == "off":
        os.environ.pop(IDENTITY_MEMO_ENV, None)
    identity_memo = os.environ.get(IDENTITY_MEMO_ENV) or None
    if memo_mode == "auto" and not identity_memo:
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
                        executor=records.executor_version(_executor_files(args.bytecode_helper)), environment=None,
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
                        "bytecode_cache": bytecode, "identity_memo": identity_memo,
                        "reservation_interruptions": scheduler.interruptions})
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
    chosen = summary.get("client_selection")
    if chosen:
        print(f"按客户端筛选（{chosen['client']} 计划，{chosen['scope']}）：测试模块 {chosen['modules_total']} 个，运行 {chosen['modules_run']}、跳过 {chosen['modules_skipped']}"
              f"（其中改动并入 {chosen['modules_pulled']} 个）；枢纽提示 {chosen['advisories']} 条；筛选摘要 {str(chosen['selection_sha256'])[:12]}", file=sys.stderr)
    retry = summary.get("retry") or {}
    for unit_id in retry.get("retry_passed", []):
        marker = retry["units"][unit_id]
        print(f"补跑通过（算入结论，单列）：{unit_id}（首次失败日志 {marker['first_attempt']['log']}；补跑日志 {marker['retry_attempt']['log']}）", file=sys.stderr)
    for unit_id in retry.get("retry_failed", []):
        marker = retry["units"][unit_id]
        print(f"补跑仍失败（判红）：{unit_id}（补跑日志 {marker['retry_attempt']['log']}）", file=sys.stderr)
    for unit_id in retry.get("retry_denied", []):
        print(f"补跑被拒绝（判红）：{unit_id}：{retry['units'][unit_id]['reason']}", file=sys.stderr)
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
    verdict = summary.get("unit_inherit_decision") or {}
    if verdict:
        steps = "，".join(f"{item['checkpoint']}{'通过' if item['authorized'] else '拒绝'}" for item in verdict.get("checkpoints") or [])
        reasons = [reason for item in verdict.get("checkpoints") or [] for reason in item.get("reasons") or []] + list(verdict.get("reasons") or [])
        print(f"逐单元承接：{verdict.get('action')}（复核点：{steps or '无'}）" + (f"；原因：{'；'.join(reasons)[:600]}" if reasons else ""),
              file=sys.stderr)
    print("-" * 70, file=sys.stderr)
    for gate in summary["gates"]:
        verdict = "通过" if gate["status"] == "passed" else f"未通过（失败单元 {len(gate['failed_units'])} 个：{gate['failed_units'][:5]}）"
        skipped = f"；不在本平台执行 {len(gate['not_executed'])} 项" if gate["not_executed"] else ""
        print(f"门禁 {gate['gate_id']}：{verdict}{skipped}", file=sys.stderr)
    audit = summary.get("read_audit") or {}
    if audit:
        verdict = "全集覆盖通过" if audit.get("coverage_complete") else "承接边界通过，全集读集未覆盖" if audit["status"] == "passed" else "不通过"
        print(f"读集检查（{audit['units']} 个单元，强制重跑 {len(audit.get('reexecute_required_units', []))} 个，"
              f"未声明读取 {audit['undeclared_total']} 处）：{verdict}；明细 {audit['report']}",
              file=sys.stderr)
        for unit_id in audit["units_with_findings"][:10]:
            print(f"  未声明读取：{unit_id}", file=sys.stderr)
    declared = summary.get("declared_inputs") or {}
    if declared:
        print(f"声明级承接（采样 {declared['sample_mode']}）：登记 {declared['registered']} 个（只重跑 {declared['rerun_only']} 个），本次执行 {declared['executed']} 个，"
              f"审计通过 {declared['audit_passed']}、失败 {declared['audit_failed']}、证据不完整 {declared['audit_incomplete']}；消费者接受 {declared['consumer_accepted']}；"
              f"实际承接 {declared['inherited']}", file=sys.stderr)
        for unit_id, row in list(declared["reruns"].items())[:10]:
            print(f"  重跑 {unit_id}（{row['kind']}，{row['seconds']:.1f} 秒）：{'；'.join(row['reasons'])[:200]}", file=sys.stderr)
        for unit_id, problems in list(declared["consumer_rejected"].items())[:10]:
            print(f"  消费者不接受 {unit_id}：{'；'.join(problems)[:300]}", file=sys.stderr)
    passed = sum(1 for gate in summary["gates"] if gate["status"] == "passed")
    print(f"门禁 {len(summary['gates'])} 项，通过 {passed} 项，用时 {summary['elapsed_seconds']:.3f}s", file=sys.stderr)
    print("OK" if summary["status"] == "passed" else f"FAILED (gates={len(summary['gates']) - passed})", file=sys.stderr)


def _gate_currents(records: Any, *, groups: list[DiscoverGroup], group_units: dict[str, list[Unit]], command_units: list[Unit],
                   manifest: dict[str, Any], timeout: float) -> dict[str, Any]:
    """``run-gates`` 各单元的当前事实（E3-01）：单元规格、输入明细与摘要、能否承接（不能时写明原因）。

    采集工具测试单元的输入优先用完整读集登记或已核对规格的收窄提案，其余按静态依赖闭包算（同一模块的拆块与独占单元
    共用一份）。未登记单元保持不可承接；每次恢复与发布复核都重算同一声明。命令单元的输入由门禁清单
    声明（``inputs``，pre-A3 场景单元另声明场景测试模块，同样按静态依赖闭包展开），清单标了 ``inheritable: false``
    的照写记录、不承接。工作目录不是干净的 git 检出时输入不完整（未跟踪文件不在范围里），本次一律不承接。"""

    items = {item["unit_id"]: item for item in manifest.get("units") or [] if isinstance(item, dict)}
    audit_policy = manifest.get("audit_policy")
    registered = audit_policy["units"] if audit_policy is not None else {}
    registered = _declared_registered(manifest, registered)  # 声明级承接：登记表里的单元（declared 与 rerun-only）也算已登记；没有登记表时原样不变
    test_scopes = {scope["unit_id"]: scope for scope in manifest.get("test_input_scopes", [])}
    repo, problem = None, ""
    try:
        repo = records.RepoIndex.load(Path.cwd())
        if not repo.clean:
            problem = f"测试树不干净（{len(repo.dirty)} 项未提交或未跟踪的改动，例如 {repo.dirty[:3]}）：输入不完整，本次不承接"
    except records.RecordsError as error:
        problem = f"执行器的工作目录不是可用的 git 检出（{error}）：算不出输入，本次不承接"
    currents: dict[str, Any] = {}
    # 同一棵受管树的静态依赖分析只建一次：采集工具测试组与声明了测试模块的命令单元（pre-A3 场景，E3-02）共用。
    deps_cache: dict[Path, Any] = {}
    # 声明级承接：本次调用的合同探测会话（探测环境相同的单元共用一个，会话内同一事实只探一次；每次调用重新探测）。
    declared_sessions: dict[Any, Any] = {}

    def current(unit: Unit, spec: dict[str, Any], inputs: list[dict[str, Any]] | None, reason: str) -> Any:
        if inputs is not None and not unit.command and any(item.get("name") == "runtime-contract" for item in inputs):
            inputs = records._unique([*inputs, records._audit_module()._runtime_module().test_spec_entry(spec)])
        return records.Current(unit_id=unit.unit_id, unit_type="command" if unit.command else "test", spec=spec, spec_sha256=_sha256(spec),
                               inputs=inputs, inputs_sha256=records.entries_sha256(inputs) if inputs is not None else None,
                               inheritable=inputs is not None and not reason, reason=reason)

    for group in groups:
        deps = records.TestDependencies(group.start.resolve().parent) if not problem else None
        if deps is not None:
            deps_cache.setdefault(deps.occ, deps)
        cache: dict[str, Any] = {}
        for unit in group_units[group.group_id]:
            spec = unit_spec(unit, start=group.start, pattern=group.pattern, timeout_seconds=timeout)
            scope = test_scopes.get(unit.unit_id)
            if scope is not None and scope["spec"] != spec:
                raise ExecutorError("测试收窄提案的完整规格漂移，拒绝沿用精确输入")
            declaration = scope["inputs"] if scope is not None else registered.get(unit.unit_id)
            module = unit.module.split("+", 1)[0]
            inputs, reason = None, problem
            if not reason:
                module_file = (group.start / f"{module}.py").resolve()
                if not module_file.is_file():
                    reason = f"找不到测试模块文件：{module_file}"
                else:
                    try:
                        if declaration is not None:
                            inputs = records.declared_inputs(repo, declaration, deps_cache)
                        else:
                            if module not in cache:
                                cache[module] = records.test_unit_inputs(repo, deps, module_file)
                            inputs = cache[module]
                    except (records.RecordsError, OSError, ValueError) as error:
                        reason = f"输入算不出来：{error}"
            # H2 实际解释器与 SSL 读取尚未进入完整宿主读集；所有拆块和独占单元只认本次执行。
            if module in ("test_claude_mac_capture_h2", "test_claude_mac_capture_h2_service") and not reason:
                reason = "H2 完整宿主运行库读集尚未闭合：本次执行记录不得承接"
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
                inputs = records.declared_inputs(repo, item["inputs"], deps_cache)
                if isinstance(item["inputs"], dict) and "declared_scope" in item["inputs"]:
                    inputs = _declared_add_contracts(records, inputs, item, declared_sessions)  # 声明级单元并入合同条目（现探测）
            except (records.RecordsError, OSError, ValueError) as error:
                reason = f"输入算不出来：{error}"
        currents[unit.unit_id] = current(unit, spec, inputs, reason)
    if audit_policy is not None:
        entry = records.value_entry("policy", "reexecute-only-policy", _sha256(audit_policy))
        for unit_id, value in currents.items():
            if unit_id not in registered:
                value.inputs = records._unique([*(value.inputs or []), entry])
                value.inputs_sha256 = records.entries_sha256(value.inputs)
                value.inheritable = False
                value.reason = "输入合同未闭合：只验证本次执行结果，禁止承接"
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
        if outcome is not None and outcome.extra.get("retry"):
            entry["retry"] = outcome.extra["retry"]
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


def _check_unit_inherit_args(args: argparse.Namespace, records: Any, request_path: Path | None) -> None:
    """逐单元承接（B5a-08）的参数核对：不与 B-09／B-10 请求混用，请求与提案只配 ``--mode unit-inherit``，要给记录库；有请求或
    要写提案时另要客户端与 control 目录（现算上下文用）。没给请求的 ``unit-inherit`` 不要求这些——它整次重新执行。"""

    if request_path:
        raise ExecutorError("逐单元承接不能与 B-09／B-10 请求同时使用")
    if args.mode != records.UNIT_INHERIT:
        raise ExecutorError("--unit-inherit-request／--unit-inherit-propose 只与 --mode unit-inherit 同用")
    if args.record_store is None:
        raise ExecutorError("逐单元承接要给记录库：--record-store")
    if args.unit_inherit_propose is not None and not args.decide_only:
        raise ExecutorError("--unit-inherit-propose 只与 --decide-only 同用")
    if (args.unit_inherit_request is not None or args.unit_inherit_propose is not None) and (
            not args.unit_inherit_client or args.unit_inherit_control is None):
        raise ExecutorError("逐单元承接要给客户端与 control 目录：--unit-inherit-client、--unit-inherit-control")


def _run_gates(args: argparse.Namespace) -> int:
    """当前适配器仅存活于本次门禁调用；退出、异常或下一次普通调用都恢复原默认。"""
    token = _CONTROL_READ_AUDIT.set(None)
    # 声明级承接的两个上下文变量同样只活在本次调用里：探测工厂复位成调用前的值（测试可以预先设成带假 runner 的工厂），运行上下文清空。
    declared_tokens = (_DECLARED_PROBE.set(_DECLARED_PROBE.get()), _DECLARED_RUN.set(None))
    retry_token = _RETRY_ACTIVE.set(False)   # 补跑开关同样只活在本次调用里（调度配置打开补跑时由 _run_gates_current 置位）
    try:
        return _run_gates_current(args)
    finally:
        _RETRY_ACTIVE.reset(retry_token)
        _CONTROL_READ_AUDIT.reset(token)
        _DECLARED_RUN.reset(declared_tokens[1])
        _DECLARED_PROBE.reset(declared_tokens[0])


def _run_gates_current(args: argparse.Namespace) -> int:
    """``run-gates``（E2-04）：一个测试组与若干命令单元同一次运行、同一套调度（额度、独占、预约、诊断、会话清理），
    全部跑完再按门禁项汇总——一个门禁项失败不影响别的门禁项照跑。测试组在执行器的工作目录下 discover。

    E3-01：全集通过模式先在记录库里给每个单元找可承接的记录，只执行找不到的；重新执行全集模式全部执行。两种模式
    的记录都入库（给了记录库时），运行结束写清单并自检。"""

    records = _records_module()
    if args.full_set_request and args.target_platform_request:
        raise ExecutorError("B-09 与 B-10 请求不能同时使用")
    request_path = args.target_platform_request or args.full_set_request
    # B5a-08：逐单元承接（unit-inherit）只认自己的请求，与 B-09／B-10 不混用。
    unit_inherit_mode = args.mode == records.UNIT_INHERIT
    if unit_inherit_mode or args.unit_inherit_request is not None or args.unit_inherit_propose is not None:
        _check_unit_inherit_args(args, records, request_path)
    target_mode = args.target_platform_request is not None
    decision_key = "target_platform_decision" if target_mode else "full_set_decision"
    decision_file = "target-platform-decision.json" if target_mode else "full-set-decision.json"
    if args.mode == records.FULL_SET_PASS and args.record_store is None:
        raise ExecutorError("全集通过模式（承接）要给记录库：--record-store")
    max_age = float(args.inheritance_max_age_hours)
    if not 0 < max_age <= records.MAX_AGE_HOURS:
        raise ExecutorError(f"承接期限只能在 0～{records.MAX_AGE_HOURS:g} 小时之间（只能调小）")
    config = load_config(args.config)
    # 失败单元补跑（优化第 2 项）：调度配置里 retry.enabled 为真才打开；要在算执行器版本之前置位（打开时 retry_policy.py 算进执行器版本）。
    retry_config = config.retry if config.retry is not None and config.retry.enabled else None
    _RETRY_ACTIVE.set(retry_config is not None)
    weights = load_weights(args.weights)
    durations = load_durations(args.durations)
    cores = args.cores or os.cpu_count() or 1
    groups, command_units, gates, manifest = load_gates_manifest(args.manifest, machine_cores=cores)
    # 按客户端筛选（优化第 9 项）：清单带 selection 块才有值；缺省 None，下面所有筛选分支都不进，行为与改造前相同。
    client_selection = _client_selection_checked(manifest, args, records, request_path=request_path, unit_inherit_mode=unit_inherit_mode)
    # 只从当前entry的固定加载器取得四control适配器；证据不提供模块路径。
    spec = importlib.util.spec_from_file_location("_executor_control_entry_gates", Path(__file__).resolve().with_name("entry_gates.py"))
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    adapter = entry.control_read_audit_for_plan(manifest)
    _CONTROL_READ_AUDIT.set(adapter)
    records = _records_module()
    # 声明级承接（步骤 5 WP3）：合同探测工厂（调用前已经设了就沿用，测试可预设带假 runner 的）与本次运行的声明级上下文——清单里登记了声明级承接
    # （declared 或 rerun-only）才创建，之后所有声明级分支都以它或「输入里有 declared-inputs-scope 条目」为触发条件；要在算执行器版本之前设好。
    if _DECLARED_PROBE.get() is None:
        _DECLARED_PROBE.set(entry.declared_probe)
    declared_units, rerun_only_units = _declared_unit_ids(manifest)
    declared_run = None
    if declared_units or rerun_only_units:
        declared_run = _DeclaredRun(entry=entry, manifest=manifest, tree=Path.cwd(), sample_mode=getattr(args, "declared_sample", "off"),
                                    declared=declared_units, rerun_only=rerun_only_units)
        _DECLARED_RUN.set(declared_run)
    resume_manifest_digest = records.file_sha256(args.manifest)
    parallelism = args.parallel or config.default_parallelism
    # 环境指纹要在准备共享缓存之前算：准备缓存会往本进程环境里写缓存位置。
    try:
        environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
    except records.RecordsError as error:
        raise ExecutorError(f"门禁清单的环境事实非法：{error}") from error
    executor = records.executor_version(_executor_files(args.bytecode_helper))
    store = records.RecordStore(args.record_store) if args.record_store is not None else None
    if declared_run is not None:
        declared_run.store = store
    group_units: dict[str, list[Unit]] = {}
    expected: dict[str, set[str]] = {}
    for group in groups:
        full_set = group.start.resolve() == DEFAULT_START.resolve() and group.pattern == DEFAULT_PATTERN
        grouped = discover_test_ids(group.start, group.pattern)
        planned = plan_units(grouped, config, weights, durations, machine_cores=cores, full_set=full_set)
        if client_selection is not None:
            if not full_set:
                raise ExecutorError("按客户端筛选只支持默认起点与模式的测试组全集")
            grouped, planned = _apply_client_selection(client_selection, grouped, planned)
        group_units[group.group_id] = [replace(unit, env=group.env, launcher=group.launcher) for unit in planned]
        expected[group.group_id] = {test_id for ids in grouped.values() for test_id in ids}
    test_units = [unit for units in group_units.values() for unit in units]
    clash = sorted({unit.unit_id for unit in test_units} & {unit.unit_id for unit in command_units})
    if clash:
        raise ExecutorError(f"命令单元与测试单元重名：{clash}")
    units = test_units + command_units
    test_scopes = manifest.get("test_input_scopes", [])
    if (not isinstance(test_scopes, list) or not all(isinstance(scope, dict) and isinstance(scope.get("spec"), dict)
            and scope["spec"].get("type") == "test" for scope in test_scopes)):
        raise ExecutorError("测试收窄提案清单非法")
    scopes = [item["input_scope"] for item in manifest.get("units", []) if "input_scope" in item] + test_scopes
    if scopes:
        if args.mode != records.RE_EXECUTE or not args.audit_reads or not args.audit_strict:
            raise ExecutorError("收窄提案必须重新执行严格审计，不能直接承接来源记录")
        # 以本轮实际发现与拆分结果核对全部规格；来源旧规格不能决定本轮执行的测试集合。
        test_specs = {unit.unit_id: unit_spec(unit, start=group.start, pattern=group.pattern,
                                           timeout_seconds=config.unit_timeout_seconds)
                      for group in groups for unit in group_units[group.group_id]}
        scope_spec = importlib.util.spec_from_file_location("_unit_executor_entry_gates", Path(__file__).resolve().with_name("entry_gates.py"))
        scope_module = importlib.util.module_from_spec(scope_spec)
        scope_spec.loader.exec_module(scope_module)
        expected_inputs = {item["unit_id"]: item.get("inputs") for item in manifest["units"] if "input_scope" in item}
        scope_module.apply_input_scopes(manifest, scopes, test_specs=test_specs)
        for item in manifest["units"]:
            if "input_scope" in item and item["inputs"] != expected_inputs[item["unit_id"]]:
                raise ExecutorError("收窄提案的输入、平台环境或执行器与来源不同，须重新建立读集")
        for scope in scopes:
            if (scope["environment_sha256"] != records.entries_sha256(environment)
                    or scope["executor_sha256"] != executor["sha256"]):
                raise ExecutorError("收窄提案的输入、平台环境或执行器与来源不同，须重新建立读集")
    audit_policy = manifest.get("audit_policy")
    if audit_policy is not None:
        try:
            records.validate_audit_policy(audit_policy, {unit.unit_id for unit in units})
        except records.RecordsError as error:
            raise ExecutorError(str(error)) from error
        if (not args.decide_only and not request_path and not unit_inherit_mode
                and (args.mode != records.RE_EXECUTE or not args.audit_reads or not args.audit_strict)):
            raise ExecutorError("读集合同登记须重新执行严格审计；当前只能只读预览承接集合")
        for item in manifest["units"]:
            if item["unit_id"] in audit_policy["units"] and item.get("inputs") != audit_policy["units"][item["unit_id"]]:
                raise ExecutorError("单元输入与已登记读集合同不一致")
        for scope in test_scopes:
            if scope["unit_id"] in audit_policy["units"] and scope["inputs"] != audit_policy["units"][scope["unit_id"]]:
                raise ExecutorError("测试收窄输入与已登记读集合同不一致")
    policy = gates_policy_digest(config, weights, durations, parallelism, manifest.get("scheduling"))
    currents = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units, manifest=manifest,
                              timeout=config.unit_timeout_seconds)
    def resume_basis(values: dict[str, Any]) -> dict[str, Any]:
        return {key: (value.spec_sha256, value.inputs_sha256, value.inheritable, value.reason) for key, value in values.items()}
    original_resume_basis = resume_basis(currents)
    if args.audit_strict and not args.audit_reads:
        raise ExecutorError("严格读集审计必须同时启用 --audit-reads")
    if args.audit_host_inputs:
        if not args.audit_reads or not args.audit_strict or args.mode != records.RE_EXECUTE:
            raise ExecutorError("宿主输入登记必须重新执行严格读集审计")
        host_inputs = _read_json_file(args.audit_host_inputs)
        if (not isinstance(host_inputs, dict) or set(host_inputs) != {"schema_version", "units"}
                or host_inputs["schema_version"] != "unit-host-inputs/v1" or not isinstance(host_inputs["units"], dict)
                or set(host_inputs["units"]) - set(currents)):
            raise ExecutorError("宿主输入清单格式错误或含未执行单元")
        for unit_id, paths in host_inputs["units"].items():
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths):
                raise ExecutorError("每个单元的宿主输入须为无重复的精确路径数组")
            current = currents[unit_id]
            # 计划期快照的合同版本由 records.new_host_snapshot 统一选择（普通运行 v3，control 适配器在位的运行 v2），
            # 与策略 host_paths 走的 declared_inputs 是同一个选择，同名条目才会摘要一致。
            current.inputs = records._unique([*(current.inputs or []), *[records.new_host_snapshot(path) for path in paths],
                                              records.value_entry("policy", "require-read-audit", "all-file-paths/v1")])
            current.inputs_sha256 = records.entries_sha256(current.inputs)
    if args.audit_reads:
        # B5a-08：逐单元承接下执行的单元同样真跑，可以带读集审计（承接的单元以来源记录的读集为准）。
        if args.mode != records.RE_EXECUTE and not unit_inherit_mode:
            raise ExecutorError("读集审计要真跑才有读集：--audit-reads 只许与 --mode re-execute 同用")
        if shutil.which("strace") is None:
            raise ExecutorError("读集审计要用 strace：PATH 里找不到（只在 Linux 上可用）")
    run_id, decided_at = records.new_run_id(), records.utc_now()
    facts = records.RunFacts(policy_sha256=policy, environment=environment, environment_sha256=records.entries_sha256(environment),
                             executor=executor, max_age_hours=max_age, now=time.time())
    decisions = {unit.unit_id: records.evaluate(store, currents[unit.unit_id], facts) if args.mode == records.FULL_SET_PASS and not request_path
                 else records.Decision(reasons=["重新执行全集：不承接"]) for unit in units}
    full_set_verdict = None
    if request_path:
        spec = importlib.util.spec_from_file_location("_full_set_receipt", Path(__file__).with_name("full_set_receipt.py"))
        full_set = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(full_set)
        assessor = full_set.assess
        if target_mode:
            target_spec = importlib.util.spec_from_file_location("_target_platform_gate", Path(__file__).with_name("target_platform_gate.py"))
            target_module = importlib.util.module_from_spec(target_spec)
            target_spec.loader.exec_module(target_module)
            assessor = target_module.assess
        try:
            request = full_set.read(request_path)
            full_set.live_check(request, deployment=args.full_set_deployment,
                                store=store.root if store is not None else None, tree=Path.cwd())
            if args.mode != records.FULL_SET_PASS:
                request = {**request, "reuse_enabled": False}
            full_set_verdict, decisions = assessor(request, currents, facts,
                gate_ids=[gate.gate_id for gate in gates], now=time.time())
        except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
            full_set_verdict = {"schema_version": "full-set-decision/v1", "action": "reexecute-all", "eligible": False,
                                "reuse_enabled": False, "reasons": [str(error)], "request": str(request_path)}
            decisions = {unit.unit_id: records.Decision(reasons=["B-09 全集重跑：" + str(error)]) for unit in units}
        if full_set_verdict["action"] not in ("inherit-full-set", "inherit-matching-units"):
            args.mode = records.RE_EXECUTE
        max_age = min(max_age, 24)
    # B5a-08：逐单元承接的排队前复核（只判定时另可写承接提案）。运行目录提前确定：只判定时证据快照写在运行目录，不进记录库。
    inherit = inherit_verdict = inherit_context = None
    early_out_dir = None
    if unit_inherit_mode:
        early_out_dir = _out_dir(args)
        inherit_module = _unit_inherit_module()

        def inherit_context(current_facts: Any) -> dict[str, Any] | None:
            """复核点现算的当前上下文；算不出（提交、部署收据集合不可读）时返回 None，复核按拒绝处理。"""

            try:
                return inherit_module.current_context(
                    client=args.unit_inherit_client, stage=args.unit_inherit_stage, commit=inherit_module.tree_commit(Path.cwd()),
                    deployments=inherit_module.deployment_set(args.unit_inherit_control),
                    environment_sha256=current_facts.environment_sha256, policy_sha256=policy, executor_sha256=executor["sha256"],
                    gate_ids=[gate.gate_id for gate in gates], profile=manifest.get("profile"), record_store=store.root)
            except inherit_module.ERRORS as error:
                print(f"逐单元承接：算不出当前上下文：{error}", file=sys.stderr)
                return None

        if args.unit_inherit_propose is not None:
            context = inherit_context(facts)
            if context is None:
                raise ExecutorError("逐单元承接：算不出当前上下文，不能写承接提案")
            proposal = inherit_module.write_proposal(args.unit_inherit_propose, context=context,
                                                     sources=inherit_module.propose(store.root, currents, facts, max_age_hours=max_age,
                                                                                   client=context["client"]))
            print(f"逐单元承接提案：可承接 {proposal['units']} 个单元、来源运行 {proposal['runs']} 个；批准要签的 binding_sha256 "
                  f"{proposal['binding_sha256']}（{args.unit_inherit_propose / 'binding.json'}）", file=sys.stderr)
        if args.unit_inherit_request is None:
            inherit_verdict = {"schema_version": records.UNIT_INHERIT_DECISION_SCHEMA, "action": "reexecute-all", "run_id": run_id,
                               "reasons": ["没有给逐单元承接请求：整次重新执行"], "checkpoints": []}
            args.mode = records.RE_EXECUTE
        else:
            inherit = inherit_module.Session(args.unit_inherit_request, store_root=store.root, run_id=run_id,
                                             evidence_root=early_out_dir / "unit-inherit-evidence" if args.decide_only else None)
            decisions = inherit.checkpoint("queue", currents, facts, inherit_context(facts), now=time.time())
            if inherit.authorized and not any(decision.inherit for decision in decisions.values()):
                inherit.fallback("排队前复核通过，但没有可承接的单元：整次重新执行")
            if not inherit.authorized:
                args.mode = records.RE_EXECUTE
            inherit_verdict = inherit.verdict()
            max_age = min(max_age, inherit.max_age_hours)
    # B-11 登记策略下，B-09（以及 B5a-08 逐单元承接）拒绝后仍须按原严格审计合同重新执行。
    if (audit_policy is not None and (full_set_verdict is not None or inherit_verdict is not None)
            and args.mode == records.RE_EXECUTE and not args.decide_only):
        if shutil.which("strace") is None:
            raise ExecutorError("B-09 全集重跑仍要求原登记的严格审计，缺少 strace")
        args.audit_reads = args.audit_strict = True
    to_run = [unit for unit in units if not decisions[unit.unit_id].inherit]
    out_dir = early_out_dir or _out_dir(args)
    if full_set_verdict is not None:
        _write_json(out_dir / decision_file, full_set_verdict)
    if inherit_verdict is not None:
        _write_json(out_dir / "unit-inherit-decision.json", inherit_verdict)
    if args.decide_only:
        # 只判定：每个单元承接还是执行、依据或原因；不执行、不写记录、不占调度锁。
        payload = {"mode": args.mode, "decided_at_utc": decided_at, "policy_sha256": policy, "environment_sha256": facts.environment_sha256,
                   "executor_sha256": executor["sha256"], "record_store": str(store.root) if store is not None else None,
                   "inherit": len(units) - len(to_run), "execute": len(to_run),
                   "units": {unit.unit_id: {"inherit": decisions[unit.unit_id].inherit, "reasons": decisions[unit.unit_id].reasons,
                                            "record_sha256": (decisions[unit.unit_id].record or {}).get("record_sha256")} for unit in units}}
        if inherit_verdict is not None:
            payload["unit_inherit_decision"] = inherit_verdict
        _write_json(out_dir / "decisions.json", payload)
        _write_binding(out_dir, mode=args.mode, policy=policy, environment_sha256=facts.environment_sha256, executor=executor, units=units,
                       currents=currents, gates=gates)
        print(f"只判定（{records.MODE_LABELS[args.mode]}）：承接 {payload['inherit']} 个、执行 {payload['execute']} 个；明细 {out_dir / 'decisions.json'}",
              file=sys.stderr)
        return 0
    state_dir = args.state_dir or default_state_dir()
    recorder = Recorder(out_dir=out_dir, mode=args.mode, run_id=run_id, policy=policy, executor=executor, environment=environment,
                        currents=currents, store=store)
    auditor = ReadAuditor(out_dir=out_dir, repo_root=Path.cwd(), data_root=args.audit_data_root, currents=currents,
                          strict=args.audit_strict) if args.audit_reads else None
    def validate_resume(restarted: list[Unit]) -> None:
        """批次可能改变输入／部署；保留原规格重派前核对，不给新输入沿用旧摘要。"""
        fresh = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units,
                               manifest=manifest, timeout=config.unit_timeout_seconds)
        fresh_basis = resume_basis(fresh)
        if any(fresh_basis[unit.unit_id] != original_resume_basis[unit.unit_id] for unit in restarted):
            raise ExecutorError("整机预约后单元规格或输入漂移，停止本轮，须重新建计划")
        for unit in restarted:
            for entry in recorder.currents[unit.unit_id].inputs or []:
                name = entry.get("name", "")
                # v2 条目整份相等（与过去相同）；v3 条目此刻没有轨迹，只比任何访问方式下都必须一致的核心集与声明字段，
                # 按访问方式的比较留给运行结束与承接。
                if name.startswith("host:") and _audit_module().host_entry_drift(entry):
                    raise ExecutorError("整机预约后宿主输入漂移，拒绝重派")
        fresh_environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
        if (records.entries_sha256(fresh_environment) != recorder.environment_sha256
                or records.executor_version(_executor_files(args.bytecode_helper)) != recorder.executor
                or records.file_sha256(args.manifest) != resume_manifest_digest
                or gates_policy_digest(load_config(args.config), load_weights(args.weights), load_durations(args.durations),
                                       parallelism, manifest.get("scheduling")) != policy):
            raise ExecutorError("整机预约后环境、工具或调度计划漂移，拒绝重派")
    lock = hold_scheduler_lock(state_dir, wait_seconds=args.wait_seconds)
    scheduler = Scheduler(
        out_dir=out_dir, state_dir=state_dir, parallelism=parallelism, machine_cores=cores,
        machine_memory=machine_memory_mb(), config=config, policy=policy, start=groups[0].start if groups else Path("."), recorder=recorder,
        auditor=auditor,
        resume_validator=validate_resume,
    )
    if auditor is None and declared_units:
        # 没给 --audit-reads、清单里却有 declared 单元：建一个只管声明级单元的非严格审计器挂在调度器上（对其它单元什么都不做）。局部变量 auditor
        # 保持 None——汇总里不出现旧的 read_audit 段，声明级的审计结果走 summary["declared_inputs"]，审计失败的单元照常使门禁失败。
        scheduler.auditor = ReadAuditor(out_dir=out_dir, repo_root=Path.cwd(), data_root=args.audit_data_root, currents=currents, strict=False)
        scheduler.auditor.declared_only = True
    try:
        # 锁可能排队很久：重新读请求、现场身份、输入、环境和动态凭证。
        if full_set_verdict is not None and any(d.inherit for d in decisions.values()):
            try:
                request = full_set.read(request_path)
                full_set.live_check(request, deployment=args.full_set_deployment, store=store.root, tree=Path.cwd())
                current_now = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units,
                                            manifest=manifest, timeout=config.unit_timeout_seconds)
                original_resume_basis = resume_basis(current_now)
                currents = current_now
                recorder.currents = currents
                # 这里只需要同步「只为声明级单元建的审计器」，不需要同步 --audit-reads 建的常规审计器（独立验收 #3 一般 8 怀疑过这一点，实测证伪）：
                # 进入本分支的前提是 B-09／B-10 判定里有可承接的单元，而判定只有在 --mode full-set-pass 下才会放行（其它模式下 reuse_enabled 被压成
                # False，assess 一律判全部重跑，没有可承接单元，本分支不进）；--audit-reads 却只许与 --mode re-execute（或逐单元承接，它与 B-09 请求
                # 互斥）同用，否则前面就报错退出。所以进到这里时不会有常规审计器，该同步的只有 declared_only 的那个。锁后复核之后常规审计器拿着旧事实的
                # 情形，唯一可达的是下面的逐单元承接分支，那里已经同步了（auditor.currents = currents）。这几个前提和同步行为都有测试钉住
                # （test_ci_declared_inputs_executor 的 AuditorFactsSyncTests），以后谁放宽了前提，测试会先红。
                _declared_sync_auditor(scheduler, currents)
                facts.now = time.time()
                fresh_environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
                fresh_facts = replace(facts, environment=fresh_environment, environment_sha256=records.entries_sha256(fresh_environment))
                environment = fresh_environment
                facts = fresh_facts
                recorder.environment = environment
                recorder.environment_sha256 = facts.environment_sha256
                full_set_verdict, decisions = assessor(request, current_now, fresh_facts,
                    gate_ids=[gate.gate_id for gate in gates], now=facts.now)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
                full_set_verdict = {"schema_version": "full-set-decision/v1", "action": "reexecute-all", "eligible": False,
                                    "reuse_enabled": False, "reasons": [str(error)]}
                decisions = {unit.unit_id: records.Decision(reasons=["B-09 锁后复核拒绝：" + str(error)]) for unit in units}
            to_run = [unit for unit in units if not decisions[unit.unit_id].inherit]
            if full_set_verdict["action"] not in ("inherit-full-set", "inherit-matching-units"):
                args.mode = recorder.mode = records.RE_EXECUTE
                if audit_policy is not None:
                    if shutil.which("strace") is None:
                        raise ExecutorError("锁后拒绝承接，重新执行原严格审计所需的 strace 不可用")
                    auditor = ReadAuditor(out_dir=out_dir, repo_root=Path.cwd(), data_root=args.audit_data_root,
                                          currents=currents, strict=True)
                    scheduler.auditor = auditor
            _write_json(out_dir / decision_file, full_set_verdict)
        # B5a-08：取得调度锁后复核（锁可能排队很久）：重算输入、环境与上下文，刷新凭证，承接集合只能缩小。
        if inherit is not None and args.mode == records.UNIT_INHERIT:
            current_now = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units,
                                         manifest=manifest, timeout=config.unit_timeout_seconds)
            original_resume_basis = resume_basis(current_now)
            currents = current_now
            recorder.currents = currents
            _declared_sync_auditor(scheduler, currents)
            fresh_environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
            facts = replace(facts, environment=fresh_environment, environment_sha256=records.entries_sha256(fresh_environment),
                            now=time.time())
            environment = fresh_environment
            recorder.environment = environment
            recorder.environment_sha256 = facts.environment_sha256
            decisions = inherit.checkpoint("lock", current_now, facts, inherit_context(facts), now=facts.now)
            if inherit.authorized and not any(decision.inherit for decision in decisions.values()):
                inherit.fallback("锁后复核通过，但承接集合已缩为空：整次重新执行")
            to_run = [unit for unit in units if not decisions[unit.unit_id].inherit]
            if not inherit.authorized:
                args.mode = recorder.mode = records.RE_EXECUTE
                if audit_policy is not None:
                    if shutil.which("strace") is None:
                        raise ExecutorError("锁后拒绝承接，重新执行原严格审计所需的 strace 不可用")
                    auditor = ReadAuditor(out_dir=out_dir, repo_root=Path.cwd(), data_root=args.audit_data_root,
                                          currents=currents, strict=True)
                    scheduler.auditor = auditor
            if auditor is not None:
                auditor.currents = currents
            inherit_verdict = inherit.verdict()
            _write_json(out_dir / "unit-inherit-decision.json", inherit_verdict)
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
        if auditor is not None and args.mode == records.UNIT_INHERIT:
            auditor.inherited = frozenset(unit.unit_id for unit in units if decisions[unit.unit_id].inherit)
        formal = scheduler.run_parallel([u for u in to_run if not u.exclusive], "formal")
        formal += scheduler.run_alone([u for u in to_run if u.exclusive], "formal")
        retry_attempts: list[Outcome] | None = None
        if retry_config is not None:
            # 失败单元补跑（优化第 2 项）：只补跑“执行失败”类、账本放行的单元，机器腾空后逐个单独跑一次；通过的并入结论并标注，
            # 仍红的判红。读集未闭合、超时、环境失败的单元不补跑，保持原来的诊断重跑（不改结论）。
            def _audited(unit: Unit) -> bool:
                active = scheduler.auditor
                return active is not None and (not getattr(active, "declared_only", False) or active.handles_declared(unit))

            formal, retry_attempts, diagnostic_units, _markers = _run_retry_phase(
                scheduler, formal, store=store, run_id=run_id, config=retry_config, audited=_audited)
            diagnostic = scheduler.run_alone(diagnostic_units, "diagnostic")
            _record_round_in_ledger(formal, store=store, run_id=run_id, already={o.unit.unit_id for o in retry_attempts})
        else:
            # 读集未闭合不能靠一次无审计的重跑补齐；仅执行本身失败才诊断重跑。
            diagnostic = scheduler.run_alone([o.unit for o in formal if not o.execution_passed], "diagnostic")
        inherited = [_inherited_outcome(unit, decisions[unit.unit_id], store) for unit in units if decisions[unit.unit_id].inherit]
        outcomes = formal + inherited
        summary = summarize_gates(groups, group_units, expected, command_units, gates, outcomes, diagnostic, time.monotonic() - started, policy)
        if retry_config is not None:
            _attach_retry_summary(summary, outcomes, gates, retry_config)
        unit_manifest = _unit_manifest(records, run_id=run_id, mode=args.mode, max_age=max_age, store=store, out_dir=out_dir,
                                       decided_at=decided_at, policy=policy, environment=environment, executor=executor, units=units,
                                       gates=gates, group_units=group_units, expected=expected, currents=currents, decisions=decisions,
                                       outcomes=outcomes, diagnostic=diagnostic)
        if retry_attempts is not None:
            unit_manifest = _manifest_with_retries(records, unit_manifest, retry_attempts)
        if client_selection is not None:
            # 筛选的运行：完整的 selection 封进运行清单（收尾校验与差集核对据此现算比对），重新封存清单自摘要。
            unit_manifest = records.seal({**{key: value for key, value in unit_manifest.items() if key != "manifest_sha256"},
                                          "client_selection": client_selection}, "manifest_sha256")
        if full_set_verdict is not None:
            unit_manifest = records.seal({**{key: value for key, value in unit_manifest.items() if key != "manifest_sha256"},
                                          decision_key: full_set_verdict}, "manifest_sha256")
        # B5a-08：发布前复核（重算输入与环境、刷新凭证、每条承接记录重做核对），决策收据先发布进记录库，再封进清单。复核
        # 不通过时决策收据只留在运行目录，清单照样封装但自检不过、不发布；已执行单元的记录已经入库，保留。
        inherit_problems: list[str] = []
        if inherit is not None and args.mode == records.UNIT_INHERIT:
            end_currents = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units,
                                          manifest=manifest, timeout=config.unit_timeout_seconds)
            end_environment = records.merge_environment(records.executor_environment(os.environ), manifest.get("environment") or [])
            end_facts = replace(facts, environment=end_environment, environment_sha256=records.entries_sha256(end_environment),
                                now=time.time())
            published = inherit.checkpoint("publish", end_currents, end_facts, inherit_context(end_facts), now=end_facts.now)
            inherit_problems = inherit.publish_problems(published)
            decision = inherit.build_decision(decisions)
            _write_json(out_dir / "unit-inherit-decision.json", decision)
            reference = {"path": str(store.decision_path(run_id)), "decision_sha256": decision["decision_sha256"],
                         "context_sha256": decision["context_sha256"], "sources_sha256": decision["sources_sha256"]}
            if not inherit_problems:
                try:
                    reference = inherit.publish_decision(decision)
                except inherit_module.ERRORS as error:
                    inherit_problems.append(f"决策收据发布失败：{error}")
            unit_manifest = records.seal({**{key: value for key, value in unit_manifest.items() if key != "manifest_sha256"},
                                          "unit_inherit_decision": reference}, "manifest_sha256")
            inherit_verdict = inherit.verdict()
        elif inherit is not None:
            # 整次重新执行了：决策收据（拒绝原因、各复核点的快照）只留在运行目录。
            _write_json(out_dir / "unit-inherit-decision.json", inherit.build_decision(decisions))
        problems = records.verify_manifest(unit_manifest, store=store)
        problems.extend(inherit_problems)
        if target_mode and inherited:
            try:
                request = full_set.read(request_path)
                full_set.live_check(request, deployment=args.full_set_deployment, store=store.root, tree=Path.cwd())
                end_currents = _gate_currents(records, groups=groups, group_units=group_units, command_units=command_units,
                                             manifest=manifest, timeout=config.unit_timeout_seconds)
                end_verdict, end_decisions = assessor(request, end_currents, facts,
                    gate_ids=[gate.gate_id for gate in gates], now=time.time())
                if (end_verdict["action"] != "inherit-matching-units"
                        or end_verdict.get("source_receipt") != full_set_verdict.get("source_receipt") or any(
                            not end_decisions[outcome.unit.unit_id].inherit
                            or end_decisions[outcome.unit.unit_id].record["record_sha256"] != decisions[outcome.unit.unit_id].record["record_sha256"]
                            for outcome in inherited)):
                    raise ValueError("结束复核不通过：" + "；".join(end_verdict["reasons"]))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as error:
                problems.append("B-10 结束时来源已失效：" + str(error))
        manifest_path = out_dir / "unit-manifest.json"
        _write_json(manifest_path, unit_manifest)
        # 自检通过才把清单存进记录库：承接要求原运行的清单把记录列为正式执行，没正常结束或自检不过的运行，它的记录不可承接。
        if store is not None and not problems:
            store.put_manifest(unit_manifest)
        summary.update({
            "reservation_interruptions": scheduler.interruptions,
            "max_cores_in_use": scheduler.max_cores_in_use, "machine_cores": cores, "parallelism": parallelism,
            "bytecode_cache": bytecode, "identity_memo": identity_memo, "mode": args.mode, "run_id": run_id,
            "record_store": str(store.root) if store is not None else None,
            decision_key: full_set_verdict,
            "inheritance": {"mode_label": records.MODE_LABELS[args.mode], **{key: unit_manifest["counts"][key] for key in ("executed", "inherited", "inherited_tests")}},
            "unit_manifest": {"path": str(manifest_path), "manifest_sha256": unit_manifest["manifest_sha256"],
                              "self_check": "passed" if not problems else "failed", "problems": problems[:50]},
        })
        if inherit_verdict is not None:
            summary["unit_inherit_decision"] = inherit_verdict
        if client_selection is not None:
            summary["client_selection"] = _client_selection_summary(client_selection)
            for group_row in summary["test_groups"].values():
                group_row["subset"] = client_selection.get("scope") == "client-plan"   # 组行的通过只是计划子集的通过
        if declared_run is not None:
            # 声明级承接的四个数（登记、审计通过、消费者接受、实际承接）与每个被重跑单元的原因和耗时；清单已发布，所以刚写下的记录能按承接用的同一个核验检查。
            summary["declared_inputs"] = _declared_run_summary(records, run=declared_run, outcomes=outcomes, decisions=decisions,
                                                               currents=currents, store=store, facts=facts)
        if bytecode["status"] == "failed" or problems:
            summary["status"] = "failed"
        if auditor is not None:
            summary["read_audit"] = auditor.report()
            summary["read_audit"].update(auditor.declared_extra())  # 只有存在声明级结果时才多一个 declared 键
            if summary["read_audit"]["status"] != "passed":
                summary["status"] = "failed"
        _write_binding(out_dir, mode=args.mode, policy=policy, environment_sha256=recorder.environment_sha256, executor=executor, units=units,
                       currents=currents, gates=gates)
        _write_core_deviation(out_dir, outcomes)
        _write_json(out_dir / "summary.json", summary)
        _print_gates_summary(summary)
        return 0 if summary["status"] == "passed" else 1
    except BaseException:
        scheduler.abort()
        raise
    finally:
        os.close(lock)


def _terminate_like_interrupt() -> None:
    """SIGTERM 与 Ctrl-C 一样走中断清理：调度器 ``abort`` 终止全部在跑单元（整个会话）、不留后台进程，退出码 143。
    单元各自成会话，原来 SIGTERM 直接结束调度进程、单元会留在后台继续占核；修复轮停下上一轮的后台验证（E4-01）靠这一条。"""
    # WP-R5：abort 带走的不只是会话成员，还有单元里另起会话的后代（嵌套的 run-unit 等），见「进程树快照与后代清理」。

    def handler(signum: int, _frame: Any) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 清理期间不再被打断
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)


def main(argv: list[str] | None = None) -> int:
    # 调度进程 discover 时会导入全部测试模块，同样不能在树里写字节码（单元子进程经环境变量禁写）。
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command in ("run", "run-commands", "run-gates"):
        _terminate_like_interrupt()
    try:
        if args.command == "run-unit":
            return run_unit(args.start, args.tests_file, args.result, runtime_tests_json=args.runtime_tests_json)
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
                "selection": args.selection_binding,
                "modules": len(grouped), "tests": sum(len(v) for v in grouped.values()),
                "units": [{"unit_id": u.unit_id, "tests": len(u.test_ids), "cores": u.quota.cores, "exclusive": u.exclusive, "weight": round(u.weight, 1)} for u in sorted(units, key=lambda u: -u.weight)],
            }, ensure_ascii=False, indent=1))
            return 0
        out_dir = _out_dir(args)
        state_dir = args.state_dir or default_state_dir()
        records = _records_module()
        recorder = Recorder(out_dir=out_dir, mode=records.RE_EXECUTE, run_id=records.new_run_id(), policy=policy,
                            executor=records.executor_version(_executor_files(args.bytecode_helper)), environment=None,
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
                                                "selection": args.selection_binding,
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
            summary["reservation_interruptions"] = scheduler.interruptions
            summary["machine_cores"] = cores
            summary["parallelism"] = parallelism
            summary["scope"] = scope
            summary["selection"] = args.selection_binding
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
