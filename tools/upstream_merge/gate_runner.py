"""上游合并 U-4 全量门禁的编排：检查线遇错不停，采集工具走统一调度执行器。

旧的 full-regression 命令用 ``&&`` 串起 test-gate、前端、采集工具与出站规格检查：第一个
失败之后其余全部不执行，一轮只能暴露一处问题；采集工具 2879 条用例单进程串行，占去全程
七成时间。本模块把检查拆成互不依赖的“检查线”：每条线内部按原有顺序依次执行，但前一步
失败不影响后一步；任一步骤失败即整体失败。

检查线按预计耗时从短到长执行（UM-19），每条线结束即把该线完整日志和一行进度写到标准输出，全部
结束后再输出汇总表：出站规格最常报红（合并后的冻结摘要漂移），放最前约 1.5 分钟出结论；v0.2.13
合并时它排最后、只在全部结束后输出，两轮失败都到第 25 分钟才看到。中途被终止时，已结束检查线的
日志已经写出；开头一行进度给出各线日志目录，运行中可逐线查看。

检查线（按执行顺序）：

- ``egress-spec``：``make -k check-egress-spec``，已含 test-official-client-control 与
  test-upstream-merge-tools；``-k`` 让互不依赖的子目标在前一个失败后继续执行。约 1.5 分钟。
- ``lint``：golangci-lint 默认、unit、integration 三种标签依次执行，与 CI 的覆盖一致；设置了
  ``UPSTREAM_GATE_GOLANGCI_LINT``（绝对路径）就用该二进制，与开工检查核对的是同一份。
- ``frontend``：lint:check、typecheck、关键 vitest 依次执行。
- ``capture-tools``：先跑分片闭合自检（``make test-capture-tools-shard-check``，与 CI 的 4 片作业
  同一份权重表，陈旧条目在本机就暴露），再跑全量（``make test-capture-tools``）：统一调度执行器
  按单元并行，各单元上报的测试 ID 必须与 discover 全集逐个相等（全集核对）。执行器的记录目录指到
  本次门禁的日志目录下，步骤结束后把未通过单元与诊断重跑的日志附进本线日志（UM-20）。原来的静态
  4 片片间不均，v0.2.13 合并三轮最慢一片都是 887 秒；执行器同一机器上全量约 350 秒。
- ``go-tests``：默认、unit、integration 三组 ``go test -count=1 ./...`` 依次执行，最慢（默认与
  unit 两组约 8.5 分钟）。三组不能合并——unit 标签会把替代实现编译进生产代码，默认组是唯一按
  生产编译形态运行的一组。三组之间保持串行：每组内部已按 GOMAXPROCS 并行，同时跑三组只会互相
  争抢 CPU。

``backend`` 模式只跑 lint 与 go-tests 两条线，供 ``backend/Makefile`` 的 test-gate 使用；``full``
模式跑全部五条，供根 Makefile 的 upstream-gate-full 使用。

``fast`` 模式是试验 worktree 的快线（根 Makefile 的 upstream-gate-fast，实现见 ``fast_lane.py``）：检查线为
toolchain、secret-scan、egress-spec、lint、frontend 与按定向回归清单生成的 targeted-regression，跳过采集工具线和
整套 go test。它写独立的状态文件（``FAST_STATUS_SCHEMA``），``workflow._load_runner_status`` 只认 full 模式的
schema，快线记录永远不能当作 U-4 收据。本模块提供的通用机制——内联步骤、失败归因、日志区间记录——都不改变
full／backend 模式的行为与状态文件形状。

同时运行的检查线数由 ``--jobs`` 或环境变量 ``UPSTREAM_GATE_JOBS`` 控制，默认 1：检查线
依次执行，只有采集工具在线内按单元并行。实测五条线全部并行时（10 核、16 GiB）总耗时 1129
秒，但满载下计时敏感用例会误判——``TestServerTimingConnectorRecordsDriverCallsWithoutRowLifetime``
与采集工具第 2 片的 4 条用例失败，单独重跑全部通过。门禁结论不能依赖机器负载，所以默认不让
检查线之间并行；确认用例稳定后可以显式调大。

integration 组依赖本机 Docker（repository 包的 TestMain 在缺 Docker 时直接退出 0，看起来通过、
实际没跑）。``UPSTREAM_GATE_INTEGRATION`` 为 auto（默认）时按 ``docker info`` 决定：可用则带
``CI=true`` 执行，缺 Docker 即失败而不是静默跳过；不可用则记为 not_executed，退出码不受影响，
由 gates-run 把 U-4 收据标为 awaiting_ci，再用 ``gates-import-ci`` 绑定同一候选提交的 CI 证据补齐。
``UPSTREAM_GATE_STATUS_FILE`` 指定时另写一份机器可读结果（逐步状态、failed、not_executed、result）。

验收用：设置 ``UPSTREAM_GATE_GO_JSON_DIR`` 时，go test 以 ``-json`` 执行，事件流按标签写入
该目录，便于按“标签、包、测试名”比对两次运行的测试集合；失败用例的输出摘进本线日志。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .fast_attribution import classify_egress_spec_summary, classify_go_test_output
from .toolchain import LINT_BINARY_ENV

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CODEX_SOURCE_ROOT = REPOSITORY_ROOT / "local-analysis" / "sources" / "codex-cli-0.149.1"
# go test 与 golangci-lint 共用的三组构建标签；空字符串表示默认标签。
BUILD_TAGS = ("", "unit", "integration")
MODES = ("backend", "full", "fast")
GO_JSON_DIR_ENV = "UPSTREAM_GATE_GO_JSON_DIR"
# integration 组的执行方式：auto 按本机 Docker 是否可用决定，run 强制执行，skip 强制不执行。
INTEGRATION_ENV = "UPSTREAM_GATE_INTEGRATION"
INTEGRATION_MODES = ("auto", "run", "skip")
# gates-run 指定的机器可读结果文件；未设置时只输出人读日志。
STATUS_FILE_ENV = "UPSTREAM_GATE_STATUS_FILE"
STATUS_SCHEMA = "official-egress-upstream-gate-runner-status/v1"
# 统一调度执行器的记录目录（tools/ci/unit_executor.py 的 _out_dir 读取）。
UNIT_EXECUTOR_OUT_DIR_ENV = "UNIT_EXECUTOR_OUT_DIR"
# 试验区快线（fast 模式）：独立的状态文件 schema。workflow 的 U-4 读侧据此拒绝把快线记录当收据。
FAST_STATUS_SCHEMA = "official-egress-upstream-gate-fast-status/v1"
# 快线的输入（均可用命令行参数覆盖）：request 或标准模板（供候选密钥扫描）、定向回归清单、日志目录、
# 与原全量状态文件对比耗时。
REQUEST_ENV = "UPSTREAM_GATE_REQUEST"
TARGETED_ENV = "UPSTREAM_GATE_TARGETED"
LOG_DIR_ENV = "UPSTREAM_GATE_LOG_DIR"
COMPARE_WITH_ENV = "UPSTREAM_GATE_COMPARE_WITH"
# 步骤的失败归因种类：go test 输出（按日志区间）与出站规格线（按执行器汇总）。
ATTRIBUTION_GO_TEST = "go_test"
ATTRIBUTION_EGRESS_SPEC = "egress_spec"


@dataclass(frozen=True)
class Step:
    """检查线里的一个步骤：命令、相对仓库根的工作目录与额外环境变量。"""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    # go test 的 JSON 事件流输出文件名；只在设置 UPSTREAM_GATE_GO_JSON_DIR 时生效。
    go_json_name: str | None = None
    # 统一调度执行器步骤：执行器记录目录经 UNIT_EXECUTOR_OUT_DIR 指到本次门禁日志目录下，步骤结束后把未通过
    # 单元与诊断重跑的日志附进本线日志（全量约 200 个单元，通过的不附）。
    unit_executor: bool = False
    # 需要本机 Docker 的步骤；Docker 不可用时记为 not_executed，由同一提交的 CI 证据补齐。
    requires_docker: bool = False
    # 在本进程内执行的步骤（秒级的前置检查）：返回 (退出码, 写进日志的文本)，不起子进程。
    inline: Callable[[], tuple[int, str]] | None = None
    # 统一调度执行器汇总所在的子目录：make check-egress-spec 经 entry_gates 把汇总写在 <记录目录>/check-egress-spec 下。
    unit_summary_subdir: str = ""
    # 失败后做归因（快线用）：ATTRIBUTION_GO_TEST 读本步骤在线日志里的输出区间，
    # ATTRIBUTION_EGRESS_SPEC 读执行器汇总。None 表示不归因。
    attribution: str | None = None


@dataclass(frozen=True)
class Lane:
    """一条检查线：步骤依次执行、遇错不停；不同检查线之间可以并行。"""

    name: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class StepResult:
    lane: str
    step: str
    status: str
    exit_code: int | None
    duration_seconds: float
    reason: str | None = None
    # 以下只在快线用：失败归因、归因依据、本步骤输出所在的线日志与字节区间、归因细节。
    classification: str | None = None
    classification_reason: str | None = None
    log_path: str | None = None
    log_range: tuple[int, int] | None = None
    detail: dict | None = None

    @property
    def key(self) -> str:
        return f"{self.lane}/{self.step}"


def _with(result: StepResult, **changes) -> StepResult:
    """复制一份 StepResult 并替换指定字段（数据类是冻结的）。"""

    values = {name: getattr(result, name) for name in result.__dataclass_fields__}
    values.update(changes)
    return StepResult(**values)


def _tag_label(tag: str) -> str:
    return tag or "default"


def _go_test_step(tag: str) -> Step:
    argv = ["go", "test"]
    if tag:
        argv.append(f"-tags={tag}")
    argv += ["-count=1", "./..."]
    integration = tag == "integration"
    return Step(
        name=f"go-test-{_tag_label(tag)}",
        argv=tuple(argv),
        cwd="backend",
        # repository 包的 integration 测试依赖 Docker：本机没有 Docker 时 TestMain 直接退出 0，
        # 看起来通过、实际没跑。带 CI=true 让它在缺 Docker 时失败而不是静默跳过。
        env=(("CI", "true"),) if integration else (),
        go_json_name=f"go-test-{_tag_label(tag)}.jsonl",
        requires_docker=integration,
    )


def _lint_step(tag: str) -> Step:
    # 与开工检查（toolchain-check／gates-run 启动前核对）用同一个二进制：设置了 UPSTREAM_GATE_GOLANGCI_LINT
    # 就用它，否则取 PATH 里的 golangci-lint，避免“核对的是一份、实际跑的是另一份”。
    binary = os.environ.get(LINT_BINARY_ENV, "").strip() or "golangci-lint"
    argv = [binary, "run"]
    if tag:
        argv.append(f"--build-tags={tag}")
    argv.append("./...")
    return Step(name=f"golangci-lint-{_tag_label(tag)}", argv=tuple(argv), cwd="backend")


def build_lanes(mode: str, codex_source_root: Path) -> list[Lane]:
    """按模式给出检查线；顺序即执行顺序：按预计耗时从短到长（UM-19，见模块说明）。"""

    if mode not in MODES:
        raise ValueError(f"未知模式：{mode}")
    if mode == "fast":
        raise ValueError("fast 模式的检查线取决于定向回归清单，请用 fast_lane.build_fast_lanes")
    lint = Lane("lint", tuple(_lint_step(tag) for tag in BUILD_TAGS))
    go_tests = Lane("go-tests", tuple(_go_test_step(tag) for tag in BUILD_TAGS))
    if mode == "backend":
        return [lint, go_tests]
    return [
        Lane(
            "egress-spec",
            (
                Step(
                    "check-egress-spec",
                    ("make", "-k", f"CODEX_0_149_1_SOURCE_ROOT={codex_source_root}", "check-egress-spec"),
                ),
            ),
        ),
        lint,
        Lane(
            "frontend",
            (
                Step("frontend-lint", ("pnpm", "--dir", "frontend", "run", "lint:check")),
                Step("frontend-typecheck", ("pnpm", "--dir", "frontend", "run", "typecheck")),
                Step("frontend-critical-vitest", ("make", "test-frontend-critical")),
            ),
        ),
        Lane(
            "capture-tools",
            (
                Step("capture-tools-shard-check", ("make", "test-capture-tools-shard-check")),
                Step("capture-tools", ("make", "test-capture-tools"), unit_executor=True),
            ),
        ),
        go_tests,
    ]


def default_jobs() -> int:
    """同时运行的检查线数：默认 1，理由见模块说明。"""

    raw = os.environ.get("UPSTREAM_GATE_JOBS", "").strip()
    if raw:
        value = int(raw)
        if value < 1:
            raise ValueError("UPSTREAM_GATE_JOBS 必须是正整数")
        return value
    return 1


def docker_available() -> bool:
    """本机 Docker 守护进程可用才算可用；命令缺失、超时或报错都按不可用处理。"""

    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_integration(mode: str, *, detect=docker_available) -> tuple[bool, str | None]:
    """返回 (是否执行需要 Docker 的步骤, 不执行的原因)。"""

    if mode not in INTEGRATION_MODES:
        raise ValueError(f"{INTEGRATION_ENV} 非法：{mode}，允许 {INTEGRATION_MODES}")
    if mode == "run":
        return True, None
    if mode == "skip":
        return False, f"{INTEGRATION_ENV}=skip"
    if detect():
        return True, None
    return False, "本机 Docker 不可用"


class _Runner:
    """执行检查线并记录每个步骤的结果；中断时终止全部子进程组。"""

    def __init__(
        self,
        repository_root: Path,
        log_dir: Path,
        env: dict[str, str],
        *,
        run_docker_steps: bool = True,
        docker_skip_reason: str | None = None,
    ) -> None:
        self.repository_root = repository_root
        self.log_dir = log_dir
        self.env = env
        self.go_json_dir = env.get(GO_JSON_DIR_ENV) or None
        self.run_docker_steps = run_docker_steps
        self.docker_skip_reason = docker_skip_reason
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        self._stopping = False

    def log_path(self, lane: Lane) -> Path:
        return self.log_dir / f"{lane.name}.log"

    def run_lane(self, lane: Lane) -> list[StepResult]:
        results: list[StepResult] = []
        with self.log_path(lane).open("ab") as log:
            for step in lane.steps:
                results.append(self._run_step(lane, step, log))
        return results

    def _run_step(self, lane: Lane, step: Step, log) -> StepResult:
        header = f"--- [{lane.name}] {step.name}: {' '.join(step.argv)}（目录 {step.cwd}）\n"
        log.write(header.encode("utf-8"))
        if step.requires_docker and not self.run_docker_steps:
            # 不执行也不能记为通过：交给 gates-run 标记 awaiting_ci，由同一提交的 CI 证据补齐。
            reason = self.docker_skip_reason or "本机 Docker 不可用"
            log.write(f"--- [{lane.name}] {step.name}: 未执行（{reason}），须以同一提交的 CI 证据补齐\n".encode("utf-8"))
            log.flush()
            return StepResult(lane.name, step.name, "not_executed", None, 0.0, reason)
        log.flush()
        if step.inline is not None:
            return self._run_inline_step(lane, step, log)
        env = dict(self.env)
        env.update(dict(step.env))
        unit_out_dir = self.log_dir / f"{lane.name}-{step.name}-units" if step.unit_executor else None
        if unit_out_dir is not None:
            env[UNIT_EXECUTOR_OUT_DIR_ENV] = str(unit_out_dir)
        argv = list(step.argv)
        json_output = None
        if self.go_json_dir and step.go_json_name:
            # go test 的 -json 必须放在包参数之前。
            argv.insert(2, "-json")
            json_output = Path(self.go_json_dir) / step.go_json_name
            log.write(f"go test 事件流写入 {json_output}\n".encode("utf-8"))
            log.flush()
        # 本步骤输出在线日志里的起点：子进程与本进程共用同一个文件描述符，位置只能看文件大小。
        log_start = os.fstat(log.fileno()).st_size
        started = time.monotonic()
        stdout = json_output.open("wb") if json_output is not None else log
        try:
            with self._lock:
                if self._stopping:
                    log.write("已中止：未启动\n".encode("utf-8"))
                    log.flush()
                    return StepResult(lane.name, step.name, "failed", None, 0.0)
                try:
                    process = subprocess.Popen(
                        argv,
                        cwd=self.repository_root / step.cwd,
                        stdout=stdout,
                        stderr=log,
                        env=env,
                        start_new_session=True,
                    )
                except OSError as error:
                    log.write(f"无法启动：{error}\n".encode("utf-8"))
                    log.flush()
                    return StepResult(lane.name, step.name, "failed", None, 0.0)
                self._processes.add(process)
            try:
                exit_code = process.wait()
            finally:
                with self._lock:
                    self._processes.discard(process)
        finally:
            if json_output is not None:
                stdout.close()
        duration = time.monotonic() - started
        status = "passed" if exit_code == 0 else "failed"
        if unit_out_dir is not None:
            _attach_unit_executor_failures(unit_out_dir / step.unit_summary_subdir, log)
        if json_output is not None:
            _append_go_json_failures(json_output, log)
        log.flush()
        log_end = os.fstat(log.fileno()).st_size
        result = StepResult(
            lane.name,
            step.name,
            status,
            exit_code,
            round(duration, 1),
            log_path=str(self.log_path(lane)),
            log_range=(log_start, log_end),
        )
        if status == "failed" and step.attribution is not None:
            result = self._attribute(result, step, log, log_start, log_end, unit_out_dir)
        log.write(f"--- [{lane.name}] {step.name}: 退出码 {exit_code}，{duration:.1f} 秒\n".encode("utf-8"))
        if result.classification is not None:
            log.write(f"--- [{lane.name}] {step.name}: 失败归因 {result.classification}：{result.classification_reason}\n".encode("utf-8"))
        log.flush()
        return result

    def _run_inline_step(self, lane: Lane, step: Step, log) -> StepResult:
        """在本进程内执行的秒级检查：输出文本写进线日志，异常一律记为失败。"""

        assert step.inline is not None
        log_start = os.fstat(log.fileno()).st_size
        started = time.monotonic()
        try:
            exit_code, text = step.inline()
        except Exception as error:  # noqa: BLE001 - 前置检查自身出错也必须成为可见的失败，而不是中断整条线
            exit_code, text = 1, f"内联检查异常：{type(error).__name__}：{error}\n"
        duration = time.monotonic() - started
        log.write(text.encode("utf-8", errors="replace"))
        log.flush()
        log_end = os.fstat(log.fileno()).st_size
        log.write(f"--- [{lane.name}] {step.name}: 退出码 {exit_code}，{duration:.1f} 秒\n".encode("utf-8"))
        log.flush()
        return StepResult(
            lane.name,
            step.name,
            "passed" if exit_code == 0 else "failed",
            exit_code,
            round(duration, 1),
            log_path=str(self.log_path(lane)),
            log_range=(log_start, log_end),
        )

    def _attribute(self, result: StepResult, step: Step, log, log_start: int, log_end: int, unit_out_dir: Path | None) -> StepResult:
        """对失败步骤做归因（只记录，不改变通过／失败）。归因过程自身出错一律按功能失败处理。"""

        try:
            if step.attribution == ATTRIBUTION_EGRESS_SPEC:
                if unit_out_dir is None:
                    raise ValueError("出站规格归因需要统一调度执行器记录目录")
                attribution = classify_egress_spec_summary(
                    unit_out_dir / step.unit_summary_subdir / "summary.json", source_root=self.repository_root / "backend"
                )
            elif step.attribution == ATTRIBUTION_GO_TEST:
                with open(self.log_path_of(log), "rb") as handle:
                    handle.seek(log_start)
                    text = handle.read(log_end - log_start).decode("utf-8", errors="replace")
                attribution = classify_go_test_output(text, source_root=self.repository_root / "backend")
            else:
                raise ValueError(f"未知的归因种类：{step.attribution}")
        except Exception as error:  # noqa: BLE001 - 归因失败时保守处理为功能失败
            from .fast_attribution import CLASS_FUNCTIONAL

            return _with(
                result,
                classification=CLASS_FUNCTIONAL,
                classification_reason=f"归因过程出错，按功能失败处理：{type(error).__name__}：{error}",
                log_path=self.log_path_of(log),
                log_range=(log_start, log_end),
            )
        return _with(
            result,
            classification=attribution.classification,
            classification_reason=attribution.reason,
            log_path=self.log_path_of(log),
            log_range=(log_start, log_end),
            detail=attribution.as_detail(),
        )

    @staticmethod
    def log_path_of(log) -> str:
        return str(log.name)

    def stop_all(self) -> None:
        with self._lock:
            self._stopping = True
            processes = list(self._processes)
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue


def _attach_unit_executor_failures(directory: Path, log) -> None:
    """把统一调度执行器未通过单元与诊断重跑的日志附进检查线日志。

    执行器的标准错误（逐个未通过单元、全集核对结论与最后的 Ran／OK／FAILED）已在本线日志里，但其中的日志路径
    在门禁临时目录里，门禁结束即删除，所以失败单元的日志要在这里整份附上。汇总缺失或不可读（前置检查失败、
    执行器自身崩溃）时记一行说明。
    """

    summary_path = directory / "summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        rows = [row for row in summary["units"] if not row["passed"]] + list(summary["diagnostic"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        log.write(f"统一调度执行器汇总不可用：{summary_path}（{error}）\n".encode("utf-8"))
        log.flush()
        return
    for row in rows:
        label = "诊断重跑" if row.get("kind") == "diagnostic" else "未通过单元"
        log.write(f"--- 附：{label} {row.get('unit_id')} ---\n".encode("utf-8"))
        try:
            with open(row["log"], "rb") as handle:
                shutil.copyfileobj(handle, log)
        except (OSError, KeyError, TypeError) as error:
            log.write(f"单元日志不可读：{error}\n".encode("utf-8"))
    log.flush()


def _append_go_json_failures(path: Path, log) -> None:
    """-json 模式下 go test 的输出在事件流里；把失败用例与失败包的输出摘进检查线日志。"""

    outputs: dict[tuple[str, str], list[str]] = {}
    failed: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        log.write(f"无法读取 go test 事件流：{error}\n".encode("utf-8"))
        return
    for line in lines:
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (str(event.get("Package") or ""), str(event.get("Test") or ""))
        if event.get("Action") == "output":
            outputs.setdefault(key, []).append(str(event.get("Output") or ""))
        elif event.get("Action") == "fail":
            failed.append(key)
    for package, test in failed:
        label = f"{package} {test}".strip()
        log.write(f"--- go test 失败：{label} ---\n".encode("utf-8"))
        log.write("".join(outputs.get((package, test), [])[-200:]).encode("utf-8"))
    log.flush()


def run_lanes(
    lanes: Sequence[Lane],
    *,
    jobs: int,
    repository_root: Path = REPOSITORY_ROOT,
    env: dict[str, str] | None = None,
    log_dir: Path | None = None,
    stream=None,
    run_docker_steps: bool = True,
    docker_skip_reason: str | None = None,
    status_file: Path | None = None,
    mode: str = "custom",
    render: Callable[..., str] | None = None,
    progress: Callable[..., str] | None = None,
) -> tuple[list[StepResult], float]:
    """执行检查线，返回结果与总耗时。

    开头写一行进度（检查线顺序、日志目录）；每条线结束即把该线完整日志与一行进度写到 stream（UM-19），
    并发时按结束先后整段写出、互不交错；全部结束后写汇总表（按检查线顺序）。中途被终止时已结束检查线
    的日志已经写出。status_file 非空时另写一份机器可读结果，只在全部结束后写一次，供 gates-run 判断
    是否有未执行、须由 CI 补齐的步骤。

    ``render`` 与 ``progress`` 供快线替换汇总表与进度行（快线要区分功能失败和待台账闭合）；默认沿用 full 模式的写法。
    """

    if jobs < 1:
        raise ValueError("jobs 必须是正整数")
    stream = stream if stream is not None else sys.stdout.buffer
    owned_log_dir = log_dir is None
    log_dir = Path(tempfile.mkdtemp(prefix="upstream-gate-")) if log_dir is None else log_dir
    base_env = dict(os.environ if env is None else env)
    base_env["PYTHONDONTWRITEBYTECODE"] = "1"
    runner = _Runner(
        repository_root,
        log_dir,
        base_env,
        run_docker_steps=run_docker_steps,
        docker_skip_reason=docker_skip_reason,
    )
    started = time.monotonic()
    results: dict[str, list[StepResult]] = {}
    emit_lock = threading.Lock()
    finished: list[str] = []
    stream.write(_progress_header(lanes, jobs=jobs, log_dir=log_dir, owned=owned_log_dir).encode("utf-8"))
    stream.flush()

    def run_and_emit(lane: Lane) -> list[StepResult]:
        lane_results = runner.run_lane(lane)
        with emit_lock:
            finished.append(lane.name)
            stream.write(f"\n===== 检查线 {lane.name} =====\n".encode("utf-8"))
            path = runner.log_path(lane)
            if path.is_file():
                with path.open("rb") as handle:
                    shutil.copyfileobj(handle, stream)
            line = (progress or _progress_line)(
                lane, lane_results, done=len(finished), total=len(lanes), elapsed=time.monotonic() - started
            )
            stream.write(line.encode("utf-8"))
            stream.flush()
        return lane_results

    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        def _terminate(_signum, _frame):
            # gates-run 或操作员终止编排时，按 Ctrl-C 同样的路径收掉全部子进程组。
            raise KeyboardInterrupt

        previous_handler = signal.signal(signal.SIGTERM, _terminate)
    try:
        with ThreadPoolExecutor(max_workers=min(jobs, max(len(lanes), 1))) as pool:
            futures = {lane.name: pool.submit(run_and_emit, lane) for lane in lanes}
            try:
                for name, future in futures.items():
                    results[name] = future.result()
            except BaseException:
                runner.stop_all()
                raise
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
    elapsed = round(time.monotonic() - started, 1)
    ordered = [result for lane in lanes for result in results[lane.name]]
    stream.write((render or render_summary)(ordered, jobs=jobs, elapsed=elapsed).encode("utf-8"))
    stream.flush()
    if status_file is not None:
        write_status_file(status_file, ordered, mode=mode, jobs=jobs, elapsed=elapsed)
    if owned_log_dir:
        shutil.rmtree(log_dir, ignore_errors=True)
    return ordered, elapsed


def _progress_header(lanes: Sequence[Lane], *, jobs: int, log_dir: Path, owned: bool) -> str:
    """开头一行进度：检查线顺序与各线日志目录（运行中各线日志实时写在那里）。"""

    order = " → ".join(lane.name for lane in lanes)
    cleanup = "，结束后删除" if owned else ""
    return f"[进度] {len(lanes)} 条检查线按此顺序执行：{order}（并发上限 {jobs}）；各线日志实时写在 {log_dir}{cleanup}\n"


def _progress_line(lane: Lane, results: Sequence[StepResult], *, done: int, total: int, elapsed: float) -> str:
    """一条检查线结束时的进度行：本线结论、本线用时、已结束几条、累计用时。"""

    failed = [item.step for item in results if item.status == "failed"]
    skipped = [item.step for item in results if item.status == "not_executed"]
    if failed:
        verdict = f"失败 {len(failed)} 项（{'、'.join(failed)}）"
    elif skipped:
        verdict = f"已执行的通过，未执行 {len(skipped)} 项须 CI 补齐（{'、'.join(skipped)}）"
    else:
        verdict = "全部通过"
    duration = sum(item.duration_seconds for item in results)
    return f"[进度] 检查线 {lane.name} 结束：{verdict}，本线 {duration:.1f} 秒；已结束 {done}/{total} 条，累计 {elapsed:.0f} 秒\n"


def overall_result(results: Sequence[StepResult]) -> str:
    """failed 优先；没有失败但有未执行步骤时为 awaiting_ci；否则 passed。"""

    if any(item.status == "failed" for item in results):
        return "failed"
    if any(item.status == "not_executed" for item in results):
        return "awaiting_ci"
    return "passed"


def status_document(results: Sequence[StepResult], *, mode: str, jobs: int, elapsed: float) -> dict:
    return {
        "schema_version": STATUS_SCHEMA,
        "mode": mode,
        "jobs": jobs,
        "elapsed_seconds": elapsed,
        "steps": [
            {
                "lane": item.lane,
                "step": item.step,
                "status": item.status,
                "exit_code": item.exit_code,
                "duration_seconds": item.duration_seconds,
                "reason": item.reason,
            }
            for item in results
        ],
        "failed": [item.key for item in results if item.status == "failed"],
        "not_executed": [item.key for item in results if item.status == "not_executed"],
        "result": overall_result(results),
    }


def write_status_file(path: Path, results: Sequence[StepResult], *, mode: str, jobs: int, elapsed: float) -> None:
    """写入机器可读结果；已存在即拒绝，避免覆盖上一轮证据。"""

    document = status_document(results, mode=mode, jobs=jobs, elapsed=elapsed)
    raw = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with open(path, "xb") as handle:
        handle.write(raw)


def render_summary(results: Sequence[StepResult], *, jobs: int, elapsed: float) -> str:
    lines = [
        "",
        f"===== 上游合并门禁汇总（并发上限 {jobs}，总耗时 {elapsed} 秒）=====",
        f"{'检查线':<16}{'步骤':<30}{'结果':<10}{'耗时(秒)':>10}",
    ]
    for result in results:
        lines.append(f"{result.lane:<16}{result.step:<30}{result.status:<10}{result.duration_seconds:>10}")
    failed = [item.key for item in results if item.status == "failed"]
    skipped = [item for item in results if item.status == "not_executed"]
    if failed:
        lines.append(f"结论：失败 {len(failed)} 项：{', '.join(failed)}")
    elif skipped:
        detail = "；".join(f"{item.key}（{item.reason}）" for item in skipped)
        lines.append(f"结论：已执行的检查全部通过；未执行 {len(skipped)} 项，须以同一提交的 CI 证据补齐：{detail}")
    else:
        lines.append("结论：全部通过")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.upstream_merge.gate_runner",
        description="上游合并 U-4 门禁的编排：检查线遇错不停、采集工具走统一调度执行器、结束后统一汇总",
    )
    parser.add_argument(
        "mode",
        choices=MODES,
        help="backend 只跑 go test 与 lint；full 跑全部检查线；fast 是试验区快线（需要 --targeted 定向回归清单）",
    )
    parser.add_argument("--jobs", type=int, help="同时运行的检查线数；默认取 UPSTREAM_GATE_JOBS，未设置时为 1；fast 模式恒为 1")
    parser.add_argument(
        "--codex-source-root",
        type=Path,
        default=DEFAULT_CODEX_SOURCE_ROOT,
        help="check-egress-spec-local-source 使用的只读 Codex 源码根",
    )
    fast = parser.add_argument_group("fast 模式")
    fast.add_argument("--targeted", type=Path, help=f"定向回归清单（targeted-regression-plan 生成）；默认取 {TARGETED_ENV}")
    fast.add_argument(
        "--request",
        type=Path,
        help=f"request 或标准模板，候选密钥扫描取其中的 secret_scan 门禁；默认取 {REQUEST_ENV}，再默认仓库里的标准模板",
    )
    fast.add_argument("--status-file", type=Path, help=f"快线状态文件（写一次）；默认取 {STATUS_FILE_ENV}，再默认日志目录下的 fast-status.json")
    fast.add_argument("--log-dir", type=Path, help=f"快线日志目录（必须不存在，结束后保留）；默认取 {LOG_DIR_ENV}，再默认临时目录")
    fast.add_argument(
        "--compare-with",
        type=Path,
        help=f"同一候选、同一环境下 upstream-gate-full 的状态文件，用来记录耗时差；默认取 {COMPARE_WITH_ENV}",
    )
    arguments = parser.parse_args(argv)
    if arguments.mode == "fast":
        return _main_fast(arguments, parser)
    try:
        jobs = arguments.jobs if arguments.jobs is not None else default_jobs()
    except ValueError as error:
        parser.error(f"并发上限非法：{error}")
    if jobs < 1:
        parser.error("--jobs 必须是正整数")
    try:
        run_docker_steps, skip_reason = resolve_integration(os.environ.get(INTEGRATION_ENV, "auto").strip() or "auto")
    except ValueError as error:
        parser.error(str(error))
    status_raw = os.environ.get(STATUS_FILE_ENV, "").strip()
    lanes = build_lanes(arguments.mode, arguments.codex_source_root)
    results, _elapsed = run_lanes(
        lanes,
        jobs=jobs,
        run_docker_steps=run_docker_steps,
        docker_skip_reason=skip_reason,
        status_file=Path(status_raw) if status_raw else None,
        mode=arguments.mode,
    )
    # 未执行不算失败：退出码只反映已执行步骤；是否须由 CI 补齐看状态文件与汇总结论。
    return 1 if any(item.status == "failed" for item in results) else 0


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def _main_fast(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """fast 模式入口：启动前检查不过就拒绝（退出码 2，不执行任何检查线）；否则跑快线，failed 退出 1，其余 0。"""

    # 延迟导入：fast_lane 依赖 workflow／gate_guard，而 workflow 经 ci_jobs 反过来导入本模块。
    from .errors import UpstreamMergeError
    from .fast_lane import prepare_fast_run, run_fast

    try:
        context = prepare_fast_run(
            REPOSITORY_ROOT,
            arguments.codex_source_root,
            targeted=arguments.targeted or _env_path(TARGETED_ENV),
            request=arguments.request or _env_path(REQUEST_ENV),
            status_file=arguments.status_file or _env_path(STATUS_FILE_ENV),
            log_dir=arguments.log_dir or _env_path(LOG_DIR_ENV),
            compare_with=arguments.compare_with or _env_path(COMPARE_WITH_ENV),
        )
    except UpstreamMergeError as error:
        print(f"快线拒绝启动：{error}", file=sys.stderr)
        return 2
    exit_code, _document = run_fast(context)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
