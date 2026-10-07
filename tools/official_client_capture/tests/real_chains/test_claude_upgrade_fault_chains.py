"""Claude 升级 B5b-03：故障恢复真实链（pre-A3 场景，零请求、隔离副本树）。

本模块登记两条链，外层入口与 pre-A3 登记（``codex_upgrade_pre_a3_certification.CLAUDE_SCENARIOS``）一一对应：

* ``vc-chain.claude-recovery-chain``（本批）：
  - 父进程中断（批次父 run 已 prepared、owner 在 commit 之前被 SIGKILL）；
  - 预约后中断（官方 attempt 跑到一半被 SIGKILL，留下孤儿 attempt）；
  - seal 失败（VC-1 封存批次失败后对账、N+1 逐字重派）；
  - 提交中崩溃两层各一（批次派发事务的 COMMIT 发布步失败；动作结果 WAL 的 Store 事实已写、批准目录未发布时硬杀）；
  - 账务未决（被打断的作业证据缺权威来源 → 两本账暂停 → accounting-resolve 批准上界补账 → 同一对象重新对账续跑）；
  - 预算到期（批次派发前 Campaign 预算真实到期 → 暂停期间派发零写入拒绝 → 延期 → 原 Campaign 同序号派发）；
  - 同根因暂停（批次派发事务同一根因失败两次 → 达上限停线 → campaign-resume 凭修复证据放行 → 续派）；
  - 工具演进后旧链重放（演进登记后旧收据按形成期重放仍通过、作废作业只重跑它们）；
  - 候选真实对账到续跑（未批准拒绝、重复对账／批准不重复入账，只重跑失败候选 Job）；
  - 有候选 attempt 的完整工具演进（真实身份变化 → 整体预览 → 两侧登记 → 候选作废闭集 → 对账批准续跑）；
  - 多根部署各故障点（受监督部署在交换、备份、新增根、回滚、收据各点被杀 → 下次启动按日志恢复 → 再部署成功）；
  - 跨客户端误用（Codex 收据喂给 Claude 入口、缺 client 的合同、x86_64 证据喂给 arm64 目标、夹具收据冒充正式引用）。
* ``vc-chain.claude-late-stage-faults``：VC-2～VC-6 一个 Campaign 的连续后段故障链，等 B4-06b（compare／accept）与 B4-07
  收尾合入后补，补时再登记（只登记能通过的链）。

每个场景都核对同样六项（``ScenarioAudit``），逐项写进链指标：

1. 可信结果保留：故障之前已经可信的产物（checkpoint、COMMIT、完成作业的证据、生产树与收据）逐字节不变（取代归档的证据
   按归档位置核对）；
2. 只执行合法闭集：恢复执行的动作／作业恰好等于对账或日志推出的闭集，其余只读复用；
3. 请求不重复入账：重复派发、重复对账、重复补账的入账增量为零，根因只计一次；
4. 累计时间不重置：计时账本的累计时长单调不减，原始截止不变，已开始的阶段不重新开始；
5. 历史事实不覆盖：Store、两本账的事件、对账收据与已提交批次只追加不改写；
6. 输出续跑入口：每个失败或暂停的输出都给出下一步命令。

两层结构与 B5a-05（``test_claude_upgrade_control_chain``）相同：外层（``test_*``，pre-A3 的场景入口）把两个受管根复制成
隔离副本树、Linux root 上进 ``unshare -m -n`` 命名空间，再起子进程跑链体；链体先做隔离自检，再按场景组在副本树里起
子进程运行本模块的 ``_scenario_*`` 方法（各自的类夹具照常建、拆），收齐每个场景写出的六项记录。场景方法只在副本树
子进程里运行；本机直接 ``python -m unittest <模块>.<类>._scenario_<名>`` 运行单个场景供调试（不写记录文件）。

零请求：命名空间模式由构造保证；所有取证都走替身中继与替身 docker，批次动作都是零请求的控制面命令。本文件位于
tests/，不进任何受管身份。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from unittest import mock

from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import claude_chain_driver as chain_driver
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_predecessor as predecessor_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_staging_recovery as staging_tests
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_control_chain as shell

REPO_ROOT = Path(__file__).resolve().parents[4]
# 链体在子进程里按点分名运行；不用 __name__，直接以脚本运行本文件时它是 __main__。
CHAIN_MODULE = "tools.official_client_capture.tests.real_chains." + Path(__file__).stem

# 场景 ID → 链体方法名（与 pre-A3 登记、外层 test_* 方法一一对应）。
CHAINS: Mapping[str, Mapping[str, str]] = {
    "recovery": {"scenario": "vc-chain.claude-recovery-chain", "exercise": "_exercise_recovery_chain"},
}

# 设计 B5b-03 的验收：ARM64 上每条不超过 15 分钟（含复制副本树）；子进程超时比它略短，留出外层收尾。
CHAIN_BUDGET_SECONDS = 900
CHAIN_TIMEOUT_SECONDS = 870
# 每组场景子进程的超时取链体剩余预算再减去这段余量。
GROUP_MARGIN_SECONDS = 30.0

# 场景组写六项记录的文件（链体设置；本机单跑场景时不设置，只做断言）。
RECORDS_ENV = "CLAUDE_FAULT_CHAIN_RECORDS"
METRICS_PREFIX = "CLAUDE_FAULT_CHAIN_METRICS "

# 六项检查：键 → 说明（记录与报错都用说明）。
SIX_CHECKS: Mapping[str, str] = {
    "trusted": "可信结果保留",
    "closure": "只执行合法闭集",
    "accounting": "请求不重复入账",
    "elapsed": "累计时间不重置",
    "history": "历史事实不覆盖",
    "entry": "输出续跑入口",
}

# 历史与可信快照里不比较的文件：锁、只是缓存的 head、点开头的临时文件（只看有没有改写已有事实）。
VOLATILE_NAMES = frozenset({"head.json"})


# ---------------------------------------------------------------------------
# 六项检查
# ---------------------------------------------------------------------------


def _volatile(path: Path) -> bool:
    return path.name in VOLATILE_NAMES or path.name.startswith(".") or path.suffix == ".lock" or "locks" in path.parts


def file_digests(*paths: Path) -> dict[str, str]:
    """路径（文件或目录，递归）下全部普通文件 → 内容摘要；锁、缓存与点文件不计，不存在的路径跳过。"""

    digests: dict[str, str] = {}
    for root in paths:
        root = Path(root)
        if root.is_file() and not root.is_symlink():
            digests[str(root)] = hashlib.sha256(root.read_bytes()).hexdigest()
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and not _volatile(path.relative_to(root)):
                digests[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def ledger_totals(*, timing_dir: Path | None = None, project_root: Path | None = None) -> dict[str, Any]:
    """两本账的入账口径：项目总账的精确／估计请求数、根因计数与操作数，计时账本的请求数与累计时长。"""

    from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing

    totals: dict[str, Any] = {}
    if project_root is not None:
        head = project_ledger.replay_head(project_root)
        totals["project"] = {
            "precise_total": head["precise_total"],
            "estimated_total": head["estimated_total"],
            "root_cause_counts": dict(head["root_cause_counts"]),
            "operations": len(head["operations"]),
            "sequence": head["sequence"],
        }
    if timing_dir is not None:
        summary = timing.inspect_ledger(timing_dir, project_ledger_optional=True)
        events = [event for event, _raw in timing._load_events(timing_dir)]
        totals["timing"] = {
            "total_live_request_count": summary["total_live_request_count"],
            "total_elapsed_seconds": summary["total_elapsed_seconds"],
            "original_total_deadline_at_utc": summary["original_total_deadline_at_utc"],
            "head_sequence": summary["head_sequence"],
            "started_phases": sorted({event["phase"] for event in events if event["event_type"] == "stage_started"}),
            "stage_started_counts": {
                phase: sum(1 for event in events if event["event_type"] == "stage_started" and event["phase"] == phase)
                for phase in sorted({event["phase"] for event in events})
            },
            "event_ids": [event["event_id"] for event in events],
        }
    return totals


class ScenarioAudit:
    """一个场景的六项检查：故障前取快照（可信产物、历史事实、两本账），恢复后逐项核对并记录证据。

    每一项都必须恰好核对一次；``report`` 在六项齐全时把记录写进链体的记录文件（本机单跑时只断言）。"""

    def __init__(self, case: unittest.TestCase, scenario: str) -> None:
        self.case = case
        self.scenario = scenario
        self.checks: dict[str, dict[str, Any]] = {}
        self.trusted_paths: tuple[Path, ...] = ()
        self.trusted_before: dict[str, str] = {}
        self.history_paths: tuple[Path, ...] = ()
        self.history_before: dict[str, str] = {}
        self.timing_dir: Path | None = None
        self.project_root: Path | None = None
        self.totals_before: dict[str, Any] = {}
        self.started = time.monotonic()

    # ---- 故障前快照 ----

    def snapshot(
        self,
        *,
        trusted: Iterable[Path],
        history: Iterable[Path],
        timing_dir: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.trusted_paths = tuple(Path(path) for path in trusted)
        self.trusted_before = file_digests(*self.trusted_paths)
        self.case.assertTrue(self.trusted_before, f"{self.scenario}：故障前没有可信产物可核对")
        self.history_paths = tuple(Path(path) for path in history)
        self.history_before = file_digests(*self.history_paths)
        self.timing_dir, self.project_root = timing_dir, project_root
        self.totals_before = ledger_totals(timing_dir=timing_dir, project_root=project_root)

    def totals(self) -> dict[str, Any]:
        return ledger_totals(timing_dir=self.timing_dir, project_root=self.project_root)

    def accounting_state(self) -> dict[str, Any]:
        """比较「重复动作有没有改变入账」用的口径：总账全部计数与序号、计时账本请求数与事件序列（不含随时间变的累计时长）。"""

        totals = self.totals()
        state: dict[str, Any] = {}
        if "project" in totals:
            state["project"] = totals["project"]
        if "timing" in totals:
            state["timing"] = {key: totals["timing"][key] for key in ("total_live_request_count", "head_sequence", "event_ids")}
        return state

    def _record(self, key: str, **evidence: Any) -> None:
        self.case.assertNotIn(key, self.checks, f"{self.scenario}：{SIX_CHECKS[key]} 重复核对")
        self.checks[key] = {"check": SIX_CHECKS[key], "passed": True, **evidence}

    # ---- 1 可信结果保留 ----

    def check_trusted(self, *, relocated: Mapping[str, str] | None = None) -> None:
        """故障前的可信文件逐字节不变；``relocated`` 给出被取代归档的目录（旧前缀 → 新前缀），按归档位置核对。"""

        moved = 0
        for path, digest in self.trusted_before.items():
            current = path
            for old, new in (relocated or {}).items():
                if path.startswith(old.rstrip("/") + "/"):
                    current = new.rstrip("/") + path[len(old.rstrip("/")):]
                    moved += 1
                    break
            target = Path(current)
            self.case.assertTrue(target.is_file(), f"{self.scenario}：可信产物丢失 {current}")
            self.case.assertEqual(
                hashlib.sha256(target.read_bytes()).hexdigest(), digest, f"{self.scenario}：可信产物被改写 {current}"
            )
        self._record("trusted", files=len(self.trusted_before), relocated_files=moved)

    # ---- 2 只执行合法闭集 ----

    def check_closure(self, *, executed: Iterable[str], expected: Iterable[str], reused: Iterable[str] = (),
                      expected_reused: Iterable[str] = (), what: str) -> None:
        executed, expected = sorted(executed), sorted(expected)
        reused, expected_reused = sorted(reused), sorted(expected_reused)
        self.case.assertEqual(executed, expected, f"{self.scenario}：{what}执行项与合法闭集不同")
        self.case.assertEqual(reused, expected_reused, f"{self.scenario}：{what}复用项与合法闭集不同")
        self._record("closure", what=what, executed=executed, reused=reused)

    # ---- 3 请求不重复入账 ----

    def check_accounting(
        self,
        *,
        precise_delta: int = 0,
        estimated_delta: int = 0,
        root_causes: int | None = None,
        root_cause_state: Mapping[str, Any] | None = None,
        repeats: Iterable[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = (),
    ) -> None:
        """入账增量等于预期（零请求场景都是 0）；``repeats`` 是 (重复动作, 动作前口径, 动作后口径)，前后必须相等；
        ``root_causes`` 给出时，新增根因的计数和必须等于它（每次失败只计一次）。根因修复会把准入计数清零，这时用
        ``root_cause_state``（修复之前取的 ``accounting_state``）核对计数。"""

        after = self.totals()
        evidence: dict[str, Any] = {"repeats": []}
        if "project" in after:
            before = self.totals_before["project"]
            now = after["project"]
            self.case.assertEqual(now["precise_total"] - before["precise_total"], precise_delta, f"{self.scenario}：精确请求入账增量不对")
            self.case.assertEqual(
                now["estimated_total"] - before["estimated_total"], estimated_delta, f"{self.scenario}：估计请求入账增量不对"
            )
            if root_causes is not None:
                counted = root_cause_state["project"] if root_cause_state is not None else now
                added = {cause: count for cause, count in counted["root_cause_counts"].items()
                         if count != before["root_cause_counts"].get(cause, 0)}
                increments = sum(count - before["root_cause_counts"].get(cause, 0) for cause, count in added.items())
                self.case.assertEqual(increments, root_causes, f"{self.scenario}：根因入账次数不对：{added}")
                evidence["root_cause_increments"] = increments
            evidence.update(precise_delta=precise_delta, estimated_delta=estimated_delta)
        for label, first, second in repeats:
            self.case.assertEqual(dict(first), dict(second), f"{self.scenario}：重复「{label}」改变了入账")
            evidence["repeats"].append(label)
        self._record("accounting", **evidence)

    # ---- 4 累计时间不重置 ----

    def check_elapsed(self, *, reopened_phases: Iterable[str] = ()) -> None:
        """累计时长单调不减、原始总截止不变、已开始的阶段没有再写 stage_started（``reopened_phases`` 是经批准重开的阶段）。"""

        if self.timing_dir is None:
            # 不涉及计时账本的场景（部署、误用拒绝）：核对它没有被写（零写入由历史检查兜底）。
            self.case.fail(f"{self.scenario}：累计时间检查需要计时账本；不涉及账本的场景用 check_elapsed_untouched")
        before, after = self.totals_before["timing"], self.totals()["timing"]
        self.case.assertGreaterEqual(after["total_elapsed_seconds"], before["total_elapsed_seconds"], f"{self.scenario}：累计时长回退")
        self.case.assertEqual(
            after["original_total_deadline_at_utc"], before["original_total_deadline_at_utc"], f"{self.scenario}：原始总截止被改"
        )
        self.case.assertEqual(after["event_ids"][: len(before["event_ids"])], before["event_ids"], f"{self.scenario}：计时事件被改写")
        reopened = set(reopened_phases)
        for phase in before["started_phases"]:
            if phase in reopened:
                continue
            self.case.assertEqual(
                after["stage_started_counts"].get(phase), before["stage_started_counts"].get(phase),
                f"{self.scenario}：已开始的 {phase} 又写了 stage_started（阶段计时被重置）",
            )
        self._record(
            "elapsed", total_elapsed_before=before["total_elapsed_seconds"], total_elapsed_after=after["total_elapsed_seconds"],
            original_total_deadline_at_utc=after["original_total_deadline_at_utc"], new_events=len(after["event_ids"]) - len(before["event_ids"]),
        )

    def check_elapsed_untouched(self, *ledger_paths: Path) -> None:
        """不经计时账本的场景（部署、误用拒绝）：给定的计时与项目账本逐字节不变，即没有入账、也没有重置。"""

        self.case.assertTrue(ledger_paths, f"{self.scenario}：要给出核对的账本")
        before = {path: digest for path, digest in self.history_before.items() if any(path.startswith(str(root)) for root in ledger_paths)}
        self.case.assertTrue(before, f"{self.scenario}：历史快照里没有这些账本")
        after = file_digests(*ledger_paths)
        self.case.assertEqual(after, before, f"{self.scenario}：账本被写")
        self._record("elapsed", ledgers=len(ledger_paths), ledger_files=len(before), untouched=True)

    # ---- 5 历史事实不覆盖 ----

    def check_history(self, *, archived: Mapping[str, str] | None = None) -> None:
        """故障前的历史事实文件全部仍在且逐字节不变；``archived`` 给出按设计被归档移走的目录（旧前缀 → 新前缀）。"""

        moved = 0
        for path, digest in self.history_before.items():
            current = path
            for old, new in (archived or {}).items():
                if path.startswith(old.rstrip("/") + "/"):
                    current = new.rstrip("/") + path[len(old.rstrip("/")):]
                    moved += 1
                    break
            target = Path(current)
            self.case.assertTrue(target.is_file(), f"{self.scenario}：历史事实丢失 {current}")
            self.case.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), digest, f"{self.scenario}：历史事实被改写 {current}")
        after = file_digests(*self.history_paths)
        self._record("history", files_before=len(self.history_before), files_after=len(after), archived_files=moved)

    # ---- 6 输出续跑入口 ----

    def check_entry(self, text: Any, *fragments: str) -> None:
        rendered = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        self.case.assertTrue(fragments, "续跑入口要给出期待的命令片段")
        for fragment in fragments:
            self.case.assertIn(fragment, rendered, f"{self.scenario}：输出没有给出续跑入口「{fragment}」")
        self._record("entry", fragments=list(fragments), excerpt=rendered[:300])

    # ---- 汇总 ----

    def report(self, **facts: Any) -> dict[str, Any]:
        missing = [label for key, label in SIX_CHECKS.items() if key not in self.checks]
        self.case.assertEqual(missing, [], f"{self.scenario}：六项检查没有做全")
        record = {
            "scenario": self.scenario,
            "checks": self.checks,
            "seconds": round(time.monotonic() - self.started, 3),
            **facts,
        }
        target = os.environ.get(RECORDS_ENV)
        if target:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return record


# ---------------------------------------------------------------------------
# 外层：隔离副本树、命名空间与子进程（隔离规则与 B5a-05 相同，见模块说明）
# ---------------------------------------------------------------------------


def _copy_tree(destination: Path) -> Path:
    """两个受管根的隔离副本：``official_client_capture`` 按本模块的静态依赖闭包复制，``official_client_control`` 整根复制；
    另带受监督部署脚本（多根部署场景）、Claude 两份文档与 Claude Release 目录（批次夹具冻结的 Active 输入）。"""

    tree = trees.copy_managed_tree(destination, include_tests="closure", closure_of=Path(__file__))
    shutil.copytree(
        REPO_ROOT / "tools" / "official_client_control",
        tree / "tools" / "official_client_control",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(REPO_ROOT / "tools" / "arm64_supervised_deploy.py", tree / "tools" / "arm64_supervised_deploy.py")
    # 批次夹具建 Campaign 时冻结真实的 2.1.226 Active 输入（Claude Release 目录与画像），按仓库相对路径读取。
    catalog = Path("backend") / "internal" / "officialegress" / "catalogdata" / "claude"
    shutil.copytree(REPO_ROOT / catalog, tree / catalog)
    for name in shell.CLAUDE_DOCUMENTS:
        source = REPO_ROOT / "docs" / name
        if source.is_file():
            shutil.copy2(source, tree / "docs" / name)
    return tree


def _parse_metrics(stdout: str) -> dict[str, Any]:
    lines = [line[len(METRICS_PREFIX):] for line in stdout.splitlines() if line.startswith(METRICS_PREFIX)]
    if not lines:
        return {}
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class _ChainRecorder(shell._ChainRecorder):
    """链体记录：步骤、事实与逐场景的六项记录；指标行用本模块的前缀。"""

    def __init__(self, chain: str) -> None:
        super().__init__(chain)
        self.facts["scenarios"] = []

    def emit(self) -> None:
        payload = {"chain": self.chain, "steps": self.steps, "exercise_seconds": round(time.monotonic() - self.started, 3), **self.facts}
        print(METRICS_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _run_group(case: unittest.TestCase, recorder: _ChainRecorder, scenarios: Mapping[str, str], step: str) -> None:
    """在副本树里起一个子进程按顺序运行一组场景方法（``类._scenario_名`` → 场景名），收齐每个场景写出的六项记录。

    子进程环境与链体相同（同一命名空间、``PYTHONPATH`` 只含副本根、同一 ``TMPDIR`` 与仅夹具总账开关），单独成一个进程组：
    超时整组终止。全部通过（正好运行这几项、没有跳过）且每个场景都写出六项齐全的记录才记 ``step``。"""

    remaining = CHAIN_TIMEOUT_SECONDS - GROUP_MARGIN_SECONDS - (time.monotonic() - recorder.started)
    records_file = Path(tempfile.mkstemp(prefix="fault-chain-records-", suffix=".jsonl")[1])
    environment = {**os.environ, RECORDS_ENV: str(records_file)}
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "unittest", "-v", *(f"{CHAIN_MODULE}.{test}" for test in scenarios)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(shell._tree_root()), env=environment,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=max(remaining, 1.0))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    seconds = round(time.monotonic() - started, 3)
    records = [json.loads(line) for line in records_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    records_file.unlink()
    recorder.facts["scenarios"].extend(records)
    recorder.facts.setdefault("groups", []).append(
        {"step": step, "tests": list(scenarios), "seconds": seconds, "returncode": None if timed_out else process.returncode}
    )
    lines = stderr.rstrip().splitlines()
    ran = f"Ran {len(scenarios)} test{'s' if len(scenarios) > 1 else ''} in "
    passed = (not timed_out and process.returncode == 0 and bool(lines) and lines[-1] == "OK"
              and any(line.startswith(ran) for line in lines))
    reported = [record["scenario"] for record in records]
    complete = reported == list(scenarios.values()) and all(set(record["checks"]) == set(SIX_CHECKS) for record in records)
    if not passed or not complete:
        recorder.emit()
        reason = (f"超时（{seconds} 秒，链体剩余预算 {round(remaining, 1)} 秒）" if timed_out
                  else f"退出码 {process.returncode}" if not passed else f"六项记录不全：报了 {reported}")
        case.fail(f"场景组「{step}」没有通过（{reason}）：{', '.join(scenarios)}\n{stdout[-2000:]}\n{stderr[-6000:]}")
    recorder.step(step)


# ---------------------------------------------------------------------------
# 场景组一：真实副本树批次（父监督器子进程真实执行）
# ---------------------------------------------------------------------------


class _BatchFaultScenarios(staging_tests._StagingCase):
    """B3-01 的真实工作树批次夹具（``_RealTreeBatchCase``）加 B3-06 的崩溃驱动：父进程中断、提交中崩溃两层、预算到期、
    同根因暂停。"""

    def _audit(self, scenario: str, campaign: Path) -> ScenarioAudit:
        audit = ScenarioAudit(self, scenario)
        vc = campaign / "control" / "vc"
        audit.snapshot(
            trusted=[campaign / "campaign.json", vc / "campaign-plan.json", vc / "commits", vc / "batches",
                     *sorted(vc.glob("vc-*-checkpoint.json"))],
            history=[self.store_root, self.ledger_dir / "events", self.ledger_dir / "ledger.json", self.project_root / "events",
                     self.project_root / "plan.json", campaign / "control" / "reconciliation", vc / "commits", vc / "batches",
                     vc / "run-manifests"],
            timing_dir=self.ledger_dir,
            project_root=self.project_root,
        )
        return audit

    def _committed(self, campaign: Path, sequence: int, phase: str = "VC-2") -> dict[str, Any]:
        return json.loads((campaign / "control" / "vc" / "batches" / f"{sequence:04d}-{phase.lower()}.json").read_text(encoding="utf-8"))

    def _scenario_parent_interrupted(self) -> None:
        """父进程中断：VC-2 首批父 run 已 prepared、owner 在 commit 之前被 SIGKILL → monitor 封存 aborted_prepared →
        状态给出同序号重派 → 下次入口对账、ABORT 入账一次 → 同序号 attempt-2 提交，只执行原动作。"""

        campaign = self.campaign = self.build_batch_campaign()
        audit = self._audit("父进程中断", campaign)
        actions = self.draft_actions(campaign)
        self.run_driver("VC-2", 2, actions, [{"module": "supervisor", "name": "_commit_prepared_run", "mode": "exit_before"}])
        (run_dir,) = self.staging_runs()
        self.wait_terminal(run_dir, {"aborted_prepared"})
        self.assertEqual(staging_tests._read(run_dir / "stop-receipt.json")["reason"], "prepared-abandoned")
        # 对账父 run：入账一次根因，给出续跑入口（序号未占，同序号重新 prepare）；再对账一次幂等、入账不变。
        code, reconciled, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", run_dir])
        self.assertIsNotNone(reconciled, stderr)
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        first_totals = audit.accounting_state()
        code, _again, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", run_dir])
        self.assertEqual(code, 0, stderr)
        second_totals = audit.accounting_state()
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, actions), 2, "VC-2")
        self.assertEqual(result["staging_attempt"], 2)
        (handled,) = result["orphans"]
        self.assertEqual((handled["kind"], handled["already_reconciled"]), ("parent-run", True))
        totals_after = audit.accounting_state()
        # 重复派发同序号：已提交即拒绝，零写入、入账不变。
        code, _again, stderr = self.dispatch(campaign, "VC-2", 2, actions)
        self.assertEqual(code, 1, stderr)
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch["execute_item_ids"], expected=["vc2-classify-draft"],
                            reused=batch["reuse_item_ids"], expected_reused=[], what="同序号重派批次")
        audit.check_accounting(root_causes=1, repeats=[("重复对账父 run", first_totals, second_totals),
                                                       ("重复派发已提交序号", totals_after, audit.accounting_state())])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(reconciled["next_command"], "compile-and-run-vc-batch", "同序号")
        audit.report(parent_run=run_dir.name, staging_attempt=result["staging_attempt"])

    def _scenario_commit_publish_crash(self) -> None:
        """提交中崩溃（批次派发事务层）：COMMIT 发布步失败（计时事件已写、COMMIT 未写）→ 入口当场对账、ABORT、归档已发布的
        半产物 → 同序号 attempt-2 提交；阶段已 active，不再补登记事件。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture.capturelib.model import ConfigurationError

        campaign = self.campaign = self.build_batch_campaign()
        audit = self._audit("提交中崩溃（批次派发事务层）", campaign)
        actions = self.draft_actions(campaign)
        original = claude_upgrade._publish_staging_file
        calls = {"count": 0}

        def failing(source: Path, target: Path, campaign_dir: Path) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise ConfigurationError("故障恢复链注入：COMMIT 发布步失败")
            original(source, target, campaign_dir)

        with mock.patch.object(claude_upgrade, "_publish_staging_file", side_effect=failing):
            code, result, stderr = self.dispatch(campaign, "VC-2", 2, actions)
        self.assertEqual((code, result), (1, None))
        (run_dir,) = self.staging_runs()
        receipt = staging_tests._read(campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json")
        self.assertEqual((receipt["failure_class"], receipt["live_request_count"]), ("parent-prepare-abandoned", 0))
        self.assertFalse(self.commit_path(2).exists())
        archive = campaign / "control" / "vc" / "staging-aborts" / "0002-vc-2" / "attempt-1"
        self.assertTrue((archive / "batch.json").is_file())
        after_failure = audit.totals()
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, actions), 2, "VC-2")
        self.assertEqual((result["staging_attempt"], result["timing_ledger"]["events"], result["orphans"]), (2, [], []))
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch["execute_item_ids"], expected=["vc2-classify-draft"],
                            reused=batch["reuse_item_ids"], expected_reused=[], what="同序号重派批次")
        # 失败当场已对账入账一次；重派不再入账（同一根因计数不变）。
        audit.check_accounting(root_causes=1, repeats=[("同序号重派", {"root_causes": after_failure["project"]["root_cause_counts"]},
                                                         {"root_causes": audit.totals()["project"]["root_cause_counts"]})])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(stderr, "commit-publish 步失败", "同序号重新派发")
        audit.report(parent_run=run_dir.name, archived=str(archive.relative_to(campaign)))

    def _scenario_action_wal_crash(self) -> None:
        """提交中崩溃（动作结果 WAL 层）：VC-2 批准动作在 Store 分类事实写入之后、批准目录发布之前被硬杀 → 账本进阶段审核、
        对账前 N+1 零写入拒绝 → reconcile-supervisor-run 写阶段幂等重派证明（重复对账逐字相同）→ N+1 逐字重派复用原事实。"""

        import collections

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_classify as classify
        from tools.official_client_capture import claude_upgrade_vc
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        if not site.ENABLE_USER_SITE:
            self.fail("动作子进程的崩溃注入经 usercustomize 进入，要求启用用户 site（不跳过：跳过即未认证）")
        campaign = self.campaign = self.build_batch_campaign()

        def action(action_id: str, argv: list[Any]) -> dict[str, Any]:
            from tools.official_client_capture import claude_upgrade_batch as batch

            return batch.managed_action(action_id, f"VC-2:{action_id}", argv, item_ids=[action_id])

        self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, [action("vc2-classify-draft", ["classify", "--campaign-dir", campaign])]), 2, "VC-2")
        directory = self.write_documents(self.resolved_documents(campaign / classify.DRAFT_ROOT / "rev-0001"), self.root / "vc2-resolved")
        revise = ["classify", "--campaign-dir", campaign, "--from-revision", "rev-0001", "--revision-input", directory]
        self.assert_batch_passed(self.dispatch(campaign, "VC-2", 3, [action("vc2-classify-revise", revise)]), 3, "VC-2")
        code, revised, stderr = self.revise(campaign, "rev-0001", directory)
        self.assertEqual((code, revised["revision"]), (2, "rev-0002"), stderr)
        audit = self._audit("提交中崩溃（动作结果 WAL 层）", campaign)
        # 可信产物另加两份分类修订（草案与修订都已提交，崩溃与重派都不得改写）。
        audit.trusted_before.update(file_digests(campaign / classify.DRAFT_ROOT))
        approve = [action("vc2-classify-approve", self.approve_argv(campaign, "rev-0002", revised["review_sha256"]))]
        inject = self.root / "inject"
        inject.mkdir(mode=0o700)
        (inject / "usercustomize.py").write_text(batch_tests.USERCUSTOMIZE, encoding="utf-8")
        marker = self.root / "crashed.marker"
        crash = {"PYTHONPATH": os.pathsep.join([str(inject), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
                 batch_tests.CRASH_ACTION_ENV: "vc2-classify-approve", batch_tests.CRASH_MARKER_ENV: str(marker),
                 batch_tests.CRASH_SUFFIX_ENV: "/classification/approved"}
        with mock.patch.dict(os.environ, crash):
            code, failed, stderr = self.dispatch(campaign, "VC-2", 4, approve)
        self.assertTrue(marker.is_file(), "崩溃注入没有生效（usercustomize 未被动作子进程加载）")
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:vc2-classify-approve"), stderr)
        failed_run = Path(failed["campaign_run"]["run_dir"])
        facts = [fact for fact in self.store_facts() if fact["fact_kind"] == "rule_classification_recorded"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "stage_review_required")
        # 对账前 N+1 零写入拒绝。
        code, _result, stderr = self.dispatch(campaign, "VC-2", 5, approve)
        self.assertEqual(code, 1)
        self.assertIn("stage_review_required", stderr)
        self.assertFalse(self.staging_dir(5).exists())
        code, reconciled, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        first_totals = audit.accounting_state()
        code, again, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertEqual((code, again["stage_replay"]), (0, reconciled["stage_replay"]), stderr)
        second_totals = audit.accounting_state()
        proof = reconciled["stage_replay"]
        self.assertEqual((proof["allowed"], proof["next_action"]), (True, "redispatch-same-batch"))
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 5, approve), 5, "VC-2")
        facts = [fact for fact in self.store_facts() if fact["fact_kind"] == "rule_classification_recorded"]
        self.assertEqual([fact["producer_run"]["run_dir"] for fact in facts], [str(failed_run)], "重派必须复用崩溃前写下的唯一事实")
        self.assertEqual(collections.Counter(fact["fact_kind"] for fact in self.store_facts())["rule_classification_recorded"], 1)
        self.assertEqual(claude_upgrade._load_stage_result(campaign, "classify")["revision"], "rev-0002")
        self.assertTrue(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-2").is_file())
        audit.check_trusted()
        audit.check_closure(executed=[item["action_id"] for item in proof["actions"]], expected=["vc2-classify-approve"],
                            reused=self._committed(campaign, 5)["reuse_item_ids"], expected_reused=[], what="阶段幂等重派证明")
        audit.check_accounting(root_causes=1, repeats=[("重复对账父 run", first_totals, second_totals)])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(reconciled, "redispatch-same-batch")
        audit.report(failed_run=failed_run.name, redispatched_sequence=5, completion=result["timing_ledger"]["completion"] is not None)

    def _scenario_same_root_cause_pause(self) -> None:
        """同根因暂停（批次派发层）：prepare 之后同一根因连续失败两次 → 第二次对账判定暂停、给出修复登记入口 → 达上限后
        派发零写入拒绝 → 凭修复提交、离线回归与部署收据登记根因修复（只清零这个根因）→ 同序号重派提交。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture

        campaign = self.campaign = self.build_batch_campaign()
        audit = self._audit("同根因暂停", campaign)
        actions = self.draft_actions(campaign)
        original = claude_upgrade.compile_vc_batch

        def crashing(arguments: Any, **options: Any) -> Any:
            original(arguments, **options)
            raise RuntimeError("故障恢复链注入：prepare 之后失败")

        messages = []
        for _ in range(2):
            with mock.patch.object(claude_upgrade, "compile_vc_batch", side_effect=crashing):
                code, result, stderr = self.dispatch(campaign, "VC-2", 2, actions)
            self.assertEqual((code, result), (1, None), stderr)
            messages.append(stderr)
        head = self.head()
        (cause, count), = head["root_cause_counts"].items()
        self.assertEqual(count, 2)
        counted = audit.accounting_state()
        # 达上限：干净派发也被项目总账拒绝，零写入（不新开 staging attempt、入账不变）。
        refused_state = audit.accounting_state()
        attempts = sorted(path.name for path in self.staging_dir(2).iterdir())
        code, _result, stderr = self.dispatch(campaign, "VC-2", 2, actions)
        self.assertEqual(code, 1)
        self.assertIn("根因达上限", stderr)
        self.assertEqual(sorted(path.name for path in self.staging_dir(2).iterdir()), attempts)
        after_refusal = audit.accounting_state()
        # 修复登记：修复提交、离线回归收据与晚于停线的受管部署收据（code 类修复，只清零这个根因）。
        regression = self.root / "regression-summary.txt"
        regression.write_text("test_claude_upgrade_staging_recovery exit=0\n", encoding="utf-8")
        deploy = ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261008t000000z")
        sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()  # noqa: E731
        repair = subprocess.run(
            [sys.executable, "-m", "tools.official_client_capture.codex_upgrade_project_ledger", "record-root-cause-repair",
             "--ledger-dir", str(self.project_root), "--root-cause-id", cause, "--kind", "code",
             "--binding", f"fix_commit_sha={'b' * 40}", "--binding", f"regression_receipt_sha256={sha(regression)}",
             "--binding", f"deployment_receipt_sha256={sha(deploy)}", "--note", "故障恢复链：修复导致同根因连续失败的缺陷"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(repair.returncode, 0, repair.stdout + repair.stderr)
        result = self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, actions), 2, "VC-2")
        self.assertEqual(result["commit"]["staging_attempt"], len(attempts) + 1)
        # 修复只追加 root_cause_repaired 事件把准入计数清零；两次失败事件原样留在总账里（历史检查兜底）。
        self.assertEqual(self.head()["root_cause_counts"].get(cause), 0)
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch["execute_item_ids"], expected=["vc2-classify-draft"],
                            reused=batch["reuse_item_ids"], expected_reused=[], what="修复后同序号重派批次")
        audit.check_accounting(root_causes=2, root_cause_state=counted, repeats=[("达上限时派发", refused_state, after_refusal)])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(messages[1], "record-root-cause-repair", "同序号重新派发")
        audit.report(root_cause_id=cause, staging_attempts=len(attempts) + 1)

    def _scenario_budget_expired(self) -> None:
        """预算到期（批次派发层）：Campaign 预算在 VC-2 首批派发之前真实到期（开始时刻前推，不改时钟）→ 派发零写入拒绝并
        给出延期入口 → deadline-extend 预览／批准写两本账（原始截止不变、只追加延期）→ 原 Campaign 同序号派发，批次带原始
        截止与延期绑定。"""

        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

        create, build_plan = timing.create_ledger, vc_artifacts.build_campaign_plan
        # 总预算 30 分钟、开始时刻前推到只剩约 8 秒：阶段预算（不得宽于总预算）还要容纳正式的清理窗口与终态排空（与 Codex R8
        # 同一约束），所以不缩预算本身；延期总截止之后 VC-2 阶段照常有完整预算。
        started = datetime.now(timezone.utc) - timedelta(minutes=30) + timedelta(seconds=8)
        deadline = started + timedelta(minutes=30)

        def aged(*arguments: Any, **options: Any) -> Any:
            options.update(started_at_utc=started.isoformat(), total_budget_minutes=30,
                           stage_budgets_minutes={phase: 30 for phase in timing.PHASE_ORDER})
            return create(*arguments, **options)

        def plan_with_ledger_deadline(*arguments: Any, **options: Any) -> Any:
            # VC-0 收口按计时账本的总截止写 VC 总计划的原始截止（claude_upgrade_campaign），夹具里同样对齐，总账注册才承接得上。
            options["original_deadline_at_utc"] = deadline.isoformat()
            return build_plan(*arguments, **options)

        with mock.patch.object(timing, "create_ledger", side_effect=aged), \
                mock.patch.object(vc_artifacts, "build_campaign_plan", side_effect=plan_with_ledger_deadline):
            campaign = self.campaign = self.build_batch_campaign()
        self.assertEqual(datetime.fromisoformat(timing.inspect_ledger(self.ledger_dir)["total_deadline_at_utc"]), deadline)
        audit = self._audit("预算到期", campaign)
        originals = {path: path.read_bytes() for path in (campaign / "campaign.json", campaign / "control" / "vc" / "campaign-plan.json",
                                                          self.ledger_dir / "ledger.json", self.project_root / "plan.json")}
        while datetime.now(timezone.utc) <= deadline:
            time.sleep(0.2)
        actions = self.draft_actions(campaign)
        before_refusal = audit.accounting_state()
        code, _result, refused = self.dispatch(campaign, "VC-2", 2, actions)
        self.assertEqual(code, 1, refused)
        self.assertFalse(self.staging_dir(2).exists(), "到期后派发必须零写入")
        after_refusal = audit.accounting_state()
        new_deadline = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        code, preview, stderr = batch_tests.run_main(["deadline-extend", "preview", "--campaign-dir", campaign, "--scope", "campaign",
                                                      "--new-deadline-at-utc", new_deadline, "--reason", "故障恢复链：预算真实到期后延期续跑"])
        self.assertIsNotNone(preview, stderr)
        self.assertEqual(code, 0, stderr)
        code, applied, stderr = batch_tests.run_main(["deadline-extend", "apply", "--campaign-dir", campaign, "--preview", preview["preview_path"],
                                                      "--approve-sha256", preview["review_sha256"], "--approved-by", "故障恢复链"])
        self.assertIsNotNone(applied, stderr)
        self.assertEqual(code, 0, stderr)
        self.assert_batch_passed(self.dispatch(campaign, "VC-2", 2, actions), 2, "VC-2")
        batch = self._committed(campaign, 2)
        plan = json.loads(originals[campaign / "control" / "vc" / "campaign-plan.json"])
        self.assertEqual(batch["original_deadline_at_utc"], plan["original_deadline_at_utc"])
        self.assertIn("deadline_extension", batch)
        self.assertEqual(originals, {path: path.read_bytes() for path in originals}, "原始坐标文件逐字节不变（延期只追加事件）")
        audit.check_trusted()
        audit.check_closure(executed=batch["execute_item_ids"], expected=["vc2-classify-draft"],
                            reused=batch["reuse_item_ids"], expected_reused=[], what="延期后同序号派发批次")
        audit.check_accounting(repeats=[("到期时派发", before_refusal, after_refusal)])
        audit.check_elapsed()
        audit.check_history()
        # 派发入口的拒绝来自共享的项目总账消费者门禁（Codex 同一文字），只写到「需要批准延期」，没有写命令名（已报协调人）。
        audit.check_entry(refused, "预算已暂停", "批准延期")
        audit.report(extended_to=new_deadline)


    def _scenario_seal_batch_failure(self) -> None:
        """seal 失败：VC-1 封存批次（静态分析在 seal 预览之前）真实失败 → 账本进阶段审核、对账前 N+1 零写入拒绝 →
        reconcile-supervisor-run 按 VC-1 封存批次的阶段幂等重派合同写证明（重复对账逐字相同）→ N+1 逐字重派（动作替身成功）。"""

        from tools.official_client_capture import claude_upgrade_static
        from tools.official_client_capture import claude_upgrade_vc
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        if not site.ENABLE_USER_SITE:
            self.fail("动作替身经 usercustomize 进入动作子进程，要求启用用户 site（不跳过：跳过即未认证）")
        predecessor = dict(batch_tests.Vc1SealBatchReplayTests.PREDECESSOR)
        campaign = self.campaign = self.build_batch_campaign(vc1_sealed=False, predecessor=predecessor)
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-1").exists())
        audit = self._audit("seal 失败", campaign)
        actions = claude_upgrade_static.vc1_seal_actions(campaign)
        code, failed, stderr = self.dispatch(campaign, "VC-1", 2, actions)
        self.assertIsNotNone(failed, stderr)
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, f"action-failed:{claude_upgrade_static.ACTION_ID}"), stderr)
        failed_run = Path(failed["campaign_run"]["run_dir"])
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "stage_review_required")
        before_refusal = audit.accounting_state()
        code, _result, stderr = self.dispatch(campaign, "VC-1", 3, actions)
        self.assertEqual(code, 1)
        self.assertIn("stage_review_required", stderr)
        self.assertFalse(self.staging_dir(3, "VC-1").exists())
        after_refusal = audit.accounting_state()
        code, reconciled, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        first = audit.accounting_state()
        code, again, stderr = batch_tests.run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", failed_run])
        self.assertEqual((code, again["stage_replay"]), (0, reconciled["stage_replay"]), stderr)
        second = audit.accounting_state()
        proof = reconciled["stage_replay"]
        self.assertEqual((proof["allowed"], proof["phase"], proof["next_action"]), (True, "VC-1", "redispatch-same-batch"))
        inject = self.root / "inject"
        inject.mkdir(mode=0o700)
        (inject / "usercustomize.py").write_text(batch_tests.VC1_STUB_USERCUSTOMIZE, encoding="utf-8")
        stub = {"PYTHONPATH": os.pathsep.join([str(inject), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
                batch_tests.VC1_STUB_ACTIONS_ENV: ",".join(item["action_id"] for item in actions)}
        with mock.patch.dict(os.environ, stub):
            self.assert_batch_passed(self.dispatch(campaign, "VC-1", 3, actions), 3, "VC-1")
        batch = self._committed(campaign, 3, "VC-1")
        audit.check_trusted()
        audit.check_closure(executed=batch["execute_item_ids"], expected=[item["action_id"] for item in actions],
                            reused=batch["reuse_item_ids"], expected_reused=[], what="封存批次逐字重派")
        audit.check_accounting(repeats=[("对账前派发 N+1", before_refusal, after_refusal), ("重复对账父 run", first, second)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(reconciled, "redispatch-same-batch")
        audit.report(failed_run=failed_run.name, redispatched_sequence=3)


# ---------------------------------------------------------------------------
# 场景组二：官方取证（B2-06 取证夹具＋B2-07 seal 输入＋B2-08 对账所需的两本账、部署收据与 Store Campaign）
# ---------------------------------------------------------------------------


class _OfficialFaultScenarios(recovery_tests.RecoveryChainCase):
    """官方取证世界：预约后中断、账务未决、工具演进后旧链重放。替身中继记录每次被调用的 probe（执行闭集按它核对）。"""

    _kill_during_second_job = recovery_tests.OrphanAttemptTests._kill_during_second_job

    def _audit(self, scenario: str, campaign: Path, *, trusted: Iterable[Path]) -> ScenarioAudit:
        audit = ScenarioAudit(self, scenario)
        audit.snapshot(
            trusted=[campaign / "campaign.json", *trusted],
            history=[self.store_root(campaign), self.ledger_dir / "events", self.ledger_dir / "ledger.json", self.project_root / "events",
                     self.project_root / "plan.json", campaign / "control" / "reconciliation", campaign / "control" / "accounting",
                     campaign / "control" / "tool-evolution"],
            timing_dir=self.ledger_dir,
            project_root=self.project_root,
        )
        return audit

    @staticmethod
    def store_root(campaign: Path) -> Path:
        from tools.official_client_capture import claude_upgrade_store

        return claude_upgrade_store.campaign_store_root(campaign)

    def _attempt_root(self, campaign: Path, attempt_id: str) -> Path:
        return campaign / "official" / "attempts" / attempt_id

    def _job_roots(self, campaign: Path) -> dict[str, Path]:
        """总计划冻结的每个官方 Job 的宿主证据根。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_capture as capture

        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.chain_patched():
            layout = capture._capture_layout(campaign, manifest)
            return {job.job_id: capture._host_path(job.evidence_roots[0], layout)
                    for job in claude_upgrade._campaign_jobs(campaign, manifest, "official")}

    def _orphan(self, campaign: Path) -> tuple[Path, str, str]:
        """首批跑到第二个 Job 收尾时整个进程被 SIGKILL：返回 (孤儿 attempt 根, 已完成 Job, 被打断 Job)。"""

        with self.chain_patched():
            status = self._kill_during_second_job(campaign)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        (orphan,) = sorted((campaign / "official" / "attempts").iterdir())
        self.assertFalse((orphan / "attempt.json").exists())
        (receipt,) = sorted(orphan.glob("job-*.json"))
        completed = receipt.name[len("job-"):-len(".json")]
        roots = self._job_roots(campaign)
        started = [job for job, root in roots.items() if job != completed and root.is_dir() and any(root.iterdir())]
        self.assertEqual(len(started), 1, f"被打断的 Job 应恰好一个：{started}")
        return orphan, completed, started[0]

    def _scenario_reserved_attempt_interrupted(self) -> None:
        """预约后中断：首批取证跑到第二个 Job 时进程被 SIGKILL，留下没有 attempt.json 的孤儿 attempt → 状态要求先对账 →
        reconcile-attempt 零请求先入账（写到一半的 Job 证据照样计量）后判定、预览全部重跑（来源没有收据，不能复用）→ 再对账幂等
        → 批准 → resume 取代归档被占用的固定证据根后全部重跑。"""

        from tools.official_client_capture import claude_upgrade

        campaign = self.build_recovery_campaign()
        orphan, completed, interrupted = self._orphan(campaign)
        roots = self._job_roots(campaign)
        audit = self._audit("预约后中断", campaign, trusted=[orphan, roots[completed], roots[interrupted]])
        with self.chain_patched():
            status = claude_upgrade.campaign_status(campaign)
        self.assertEqual(status["status"], "official_capture_interrupted")
        reconciled = self.reconcile(campaign, orphan.name)
        preview = reconciled["recovery_preview"]
        first = audit.accounting_state()
        self.reconcile(campaign, orphan.name)
        second = audit.accounting_state()
        provenance = recovery_tests._read(campaign / reconciled["provenance_receipt"]["path"])
        counted = sum(job["precise_count"] for job in provenance["jobs"])
        self.assertGreater(sum(job["precise_count"] for job in provenance["jobs"] if job["job_id"] == interrupted), 0,
                           "写到一半的 Job 证据必须计量，不因缺收据漏计")
        self.approve(campaign, orphan.name, preview)
        calls_before = self.relay_probe_calls()
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        supersession = recovery_tests._read(campaign / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{resumed['attempt_id']}.json")
        archived = {row["host_root"]: row["archived_host_root"] for row in supersession["roots"]}
        self.assertEqual(sorted(archived), sorted(str(roots[job]) for job in (completed, interrupted)))
        executed_probes = self.relay_probe_calls()[len(calls_before):]
        audit.check_trusted(relocated=archived)
        audit.check_closure(executed=preview["execute_job_ids"], expected=sorted(self._job_roots(campaign)),
                            reused=preview["reuse_job_ids"], expected_reused=[], what="孤儿 attempt 的恢复预览")
        self.assertEqual(sorted(executed_probes), sorted(probe for job in sorted(roots) for probe in self.job_probes(job)))
        audit.check_accounting(precise_delta=counted, root_causes=1, repeats=[("重复对账孤儿 attempt", first, second)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(status["next_command"], "reconcile-attempt")
        audit.report(orphan_attempt=orphan.name, resumed_attempt=resumed["attempt_id"], counted_requests=counted)

    def _scenario_accounting_unresolved(self) -> None:
        """账务未决：被打断 Job 的中继登记了有效连接却没有客户端原始字节（中继在写字节时被杀）→ reconcile-attempt 只暂停
        （accounting），总账 blocked、给出补账入口 → accounting-resolve 预览、按批准上界补账（以同一批准重跑幂等）→ 同一 attempt
        重新对账可恢复 → 批准预览 → resume。"""

        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        campaign = self.build_recovery_campaign()
        orphan, completed, interrupted = self._orphan(campaign)
        roots = self._job_roots(campaign)
        connections = sorted(roots[interrupted].rglob("*.client_to_upstream.bin"))
        self.assertTrue(connections, "被打断的 Job 没有中继连接字节")
        connections[0].unlink()
        audit = self._audit("账务未决", campaign, trusted=[orphan / f"job-{completed}.json", roots[completed]])
        with self.chain_patched():
            code, paused, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", orphan.name])
        self.assertIsNotNone(paused, stderr)
        self.assertEqual((code, paused["status"], paused["decision"]["pause_kinds"]), (3, "paused", ["accounting"]), stderr)
        self.assertIsNone(paused["decision"]["terminal_reason"])
        operation = f"reconcile-attempt:{orphan.name}"
        head = project_ledger.replay_head(self.project_root)
        self.assertEqual((head["blocked"], head["unresolved_operation_ids"]), (True, [operation]))
        evidence = self.root / "provenance-audit.txt"
        evidence.write_text(f"{interrupted} 的中继在写客户端字节时被杀，按同类作业上界估计 3 次请求。\n", encoding="utf-8")
        argv = ["accounting-resolve", "--campaign-dir", str(campaign), "--operation-id", operation, "--estimated-count", "3",
                "--evidence", str(evidence), "--reason", "被打断作业缺客户端字节，按上界估计"]
        with self.chain_patched():
            code, preview, stderr = self.run_main(argv)
            self.assertIsNotNone(preview, stderr)
            self.assertEqual((code, preview["status"], preview["resolution_mode"]), (0, "approval_required", "estimated_upper_bound"), stderr)
            approve = [*argv, "--approve-sha256", preview["review_sha256"], "--approved-by", "故障恢复链"]
            code, resolved, stderr = self.run_main(approve)
            self.assertEqual((code, resolved["status"], resolved["blocked"]), (0, "resolved", False), stderr)
            first = audit.accounting_state()
            code, rerun, stderr = self.run_main(approve)
            self.assertEqual((code, rerun["status"], rerun["batch"]["reused"]), (0, "resolved", True), stderr)
            second = audit.accounting_state()
        reconciled = self.reconcile(campaign, orphan.name)
        self.approve(campaign, orphan.name, reconciled["recovery_preview"])
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        head = project_ledger.replay_head(self.project_root)
        self.assertEqual((head["blocked"], head["unresolved_operation_ids"], len(head["accounting_resolutions"][paused["campaign_id"]])),
                         (False, [], 1))
        from tools.official_client_capture import claude_upgrade

        supersession = recovery_tests._read(campaign / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{resumed['attempt_id']}.json")
        archived = {row["host_root"]: row["archived_host_root"] for row in supersession["roots"]}
        precise = sum(job["precise_count"] for job in recovery_tests._read(campaign / paused["provenance_receipt"]["path"])["jobs"])
        audit.check_trusted(relocated=archived)
        audit.check_closure(executed=reconciled["recovery_preview"]["execute_job_ids"], expected=sorted(roots),
                            reused=reconciled["recovery_preview"]["reuse_job_ids"], expected_reused=[], what="补账后的恢复预览")
        audit.check_accounting(precise_delta=precise, estimated_delta=3, root_causes=1, repeats=[("以同一批准重跑补账", first, second)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(paused["next_command"], "accounting-resolve")
        audit.report(operation=operation, estimated=3, precise=precise)

    build_evolution_campaign = recovery_tests.ToolEvolutionChainTests.build_evolution_campaign
    evolved_identity = recovery_tests.ToolEvolutionChainTests.evolved_identity

    def _scenario_tool_evolution_replay(self) -> None:
        """工具演进后旧链重放：首批完成并有 seal 预览 → 修好一个 wire 依赖并部署（编排器侧身份与 Store 侧受管树一起变）→
        未登记演进时 seal 与 run 都被拒 → 登记演进（两侧，以同一批准重跑只核对、不重算）→ 旧 attempt 被演进作废的作业只有
        依赖它的那一个，seal 拒绝作废结果 → 对账预览只执行它 → resume 只重跑它的 probe → 新 attempt 在演进后身份下进 seal 预览；
        首批 attempt 按形成期身份照旧完整重放（旧收据不因演进失效）。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_store
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
        from tools.official_client_control import receipts as store_receipts

        campaign = self.build_evolution_campaign()
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "awaiting_receipts")
        with self.chain_patched():
            code, preview, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        roots = self._job_roots(campaign)
        first_root = self._attempt_root(campaign, first["attempt_id"])
        others = sorted(job for job in roots if job != recovery_tests.EVOLVED_JOB)
        audit = self._audit("工具演进后旧链重放", campaign, trusted=[first_root, *(roots[job] for job in others)])

        evolved = self.evolved_identity()
        key = f"official_client_capture/{recovery_tests.EVOLVED_DEPENDENCY}"
        self.tree.write(key, self.tree.path(key).read_bytes() + b"\n# B5b-03 evolution fixture\n")
        self.addCleanup(self.tree.restore)
        ledger_fixture.write_claude_deploy_receipt(self.data, evolved, stamp="20261008t010000z")
        evolution_argv = ["tool-evolution", "--campaign-dir", str(campaign), "--fix-commit", "a" * 40, "--reason", "修复 FW-F v3 判定"]
        with self.chain_patched(), mock.patch.object(claude_upgrade, "_tool_identity", side_effect=lambda **_: json.loads(json.dumps(evolved))):
            code, _result, refused_seal = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("尚未登记工具演进", refused_seal)
            code, proposed, stderr = self.run_main(evolution_argv)
            self.assertEqual((code, proposed["status"]), (0, "approval_required"), stderr)
            self.assertEqual(proposed["impact"]["official"]["affected_job_ids"], [recovery_tests.EVOLVED_JOB])
            approve = [*evolution_argv, "--approve-sha256", proposed["review_sha256"], "--approved-by", "故障恢复链"]
            code, applied, stderr = self.run_main(approve)
            self.assertEqual((code, applied["status"], applied["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)
            first_state = audit.accounting_state()
            code, again, stderr = self.run_main(approve)
            self.assertEqual((code, again["reused"], again["store_registration"]["status"]), (0, True, "current"), stderr)
            second_state = audit.accounting_state()
            status = claude_upgrade.campaign_status(campaign)
            self.assertEqual(status["official_attempt"]["evolution_invalidated_job_ids"], [recovery_tests.EVOLVED_JOB])
            code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("被工具演进作废", stderr)
            code, reconciled, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            recovery_preview = reconciled["recovery_preview"]
            code, _approved, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"],
                                                     "--approve-recovery-sha256", recovery_preview["review_sha256"]])
            self.assertEqual(code, 0, stderr)
            calls_before = self.relay_probe_calls()
            code, resumed, stderr = self.run_main(["resume", "--campaign-dir", str(campaign), "--rerun-failed", "--recovery-preview",
                                                   reconciled["recovery_preview_path"], "--acknowledge-live-requests"])
            self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
            self.assertEqual(self.relay_probe_calls()[len(calls_before):], self.job_probes(recovery_tests.EVOLVED_JOB))
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, resumed["attempt_id"]))
            self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)
            # 旧链重放：首批 attempt 按形成期身份完整重放（run 末收口重放、逐 Job 收据），不因演进失效。
            _root, replayed = claude_upgrade._load_capture_attempt(campaign, "official", None, first["attempt_id"], _replay_evidence_permissions=True)
        self.assertEqual(replayed["attempt_id"], first["attempt_id"])
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign))
        self.assertEqual(len(store_receipts.tool_evolutions(store, dict(store_campaign))), 1)
        _root, attempt = self.load_attempt(campaign, resumed["attempt_id"])
        reused = sorted(item["id"] for item in attempt["results"] if item["disposition"] == "reused")
        audit.check_trusted()
        audit.check_closure(executed=recovery_preview["execute_job_ids"], expected=[recovery_tests.EVOLVED_JOB],
                            reused=reused, expected_reused=others, what="演进作废后的恢复")
        # 首批是直接 CLI 取证（没有父监督器收账），它的请求在对账时按来源核算精确入账一次；演进登记本身不入账。
        counted = sum(job["precise_count"] for job in recovery_tests._read(campaign / reconciled["provenance_receipt"]["path"])["jobs"])
        audit.check_accounting(precise_delta=counted, repeats=[("以同一批准重跑演进登记", first_state, second_state)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry({"seal": refused_seal, "status": status["next_command"]}, "tool-evolution", "reconcile-attempt")
        audit.report(invalidated=[recovery_tests.EVOLVED_JOB], resumed_attempt=resumed["attempt_id"])


    # ---- 跨客户端误用（官方取证世界：入口按 Claude 合同读部署收据，误用一律零写入拒绝）----

    def _misuse_setup(self, scenario: str) -> tuple[Path, dict[str, Any], ScenarioAudit, Path, dict[str, Any]]:
        """首批有一个 Job 失败的官方 attempt（对账入口要读当前工具的部署收据）；返回 (Campaign, attempt, 审计, 收据, 收据正文)。

        项目总账按生产形态建（不是仅夹具总账）：对账器只在生产总账下要求当前工具的通过部署收据（仅夹具总账不要求），
        误用核对的就是这道门。链体整体设置了仅夹具开关，这里建总账时临时去掉它。"""

        environment = {key: value for key, value in os.environ.items() if key != project_ledger_fixture.FIXTURE_ONLY_ENV}
        with mock.patch.dict(os.environ, environment, clear=True):
            campaign = self.build_recovery_campaign()
        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        self.assertFalse(project_ledger._load_plan(self.project_root)[0].get("fixture_only"), "误用场景要用生产形态的项目总账")
        self.fail_probe(self.job_probes(recovery_tests.FAILING_JOB)[0], 3)
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "failed")
        (receipt,) = sorted((self.data / "control").glob("claude-managed-deploy-*.json"))
        audit = self._audit(scenario, campaign, trusted=[self._attempt_root(campaign, first["attempt_id"]), receipt])
        return campaign, first, audit, receipt, json.loads(receipt.read_text(encoding="utf-8"))

    def _refused_reconcile(self, campaign: Path, attempt_id: str) -> str:
        with self.chain_patched():
            code, result, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", attempt_id])
        self.assertEqual((code, result), (1, None), stderr)
        self.assertFalse((campaign / "control" / "reconciliation" / f"attempt-{attempt_id}").exists(), "误用必须零写入")
        return stderr

    @staticmethod
    def _counted(campaign: Path, reconciled: Mapping[str, Any]) -> int:
        """对账按来源核算精确入账的请求数（直接 CLI 取证没有父监督器收账，首批请求在纠正后的这次对账里入账一次）。"""

        return sum(job["precise_count"] for job in recovery_tests._read(campaign / reconciled["provenance_receipt"]["path"])["jobs"])

    def _corrected_reconcile(self, campaign: Path, attempt_id: str) -> dict[str, Any]:
        """误用纠正（放回合法的 Claude 部署收据）之后，同一入口照常对账：恢复预览只执行失败 Job。"""

        reconciled = self.reconcile(campaign, attempt_id)
        self.assertEqual(reconciled["recovery_preview"]["execute_job_ids"], [recovery_tests.FAILING_JOB])
        return reconciled

    def _scenario_misuse_codex_receipt(self) -> None:
        """跨客户端误用：Codex 收据喂给 Claude 入口——控制目录里只有 Codex 部署收据（codex-arm64-supervisor-enable/v1，身份在顶层），
        或把它改名成 Claude 收据的文件名，Claude 对账入口都按「没有当前工具的通过部署收据」零写入拒绝；Codex 的 pre-A3 认证拿到
        Claude 的链覆盖核对同样拒绝。放回 Claude 收据后同一入口照常对账。"""

        from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3

        campaign, first, audit, receipt, original = self._misuse_setup("跨客户端误用：Codex 收据喂给 Claude 入口")
        codex = {"schema_version": "codex-arm64-supervisor-enable/v1", "status": "passed", "created_at_utc": original["created_at_utc"],
                 **{key: value for key, value in original["identity"].items() if key != "tool_files_sha256"},
                 "tool_files_sha256": original["identity"]["tool_files_sha256"]}
        saved = receipt.read_bytes()
        receipt.unlink()
        codex_path = self.data / "control" / "codex-20261008t000000z-supervisor-enable-20261008t000000z.json"
        codex_path.write_text(json.dumps(codex), encoding="utf-8")
        state = audit.accounting_state()
        refusals = [self._refused_reconcile(campaign, first["attempt_id"])]
        disguised = self.data / "control" / "claude-managed-deploy-20261008t000001z.json"
        disguised.write_text(json.dumps(codex), encoding="utf-8")
        refusals.append(self._refused_reconcile(campaign, first["attempt_id"]))
        after = audit.accounting_state()
        with self.assertRaisesRegex(pre_a3.CertificationError, "不属于客户端 claude"):
            pre_a3.real_chain_coverage({"schema_version": pre_a3.SCHEMA_VERSION_V2, "scenarios": [],
                                        "real_chain_registration": pre_a3.real_chain_registration("codex")}, client="claude")
        disguised.unlink()
        codex_path.unlink()
        receipt.write_bytes(saved)
        audit.check_trusted()
        reconciled = self._corrected_reconcile(campaign, first["attempt_id"])
        audit.check_closure(executed=reconciled["recovery_preview"]["execute_job_ids"], expected=[recovery_tests.FAILING_JOB],
                            reused=reconciled["recovery_preview"]["reuse_job_ids"],
                            expected_reused=sorted(set(self._job_roots(campaign)) - {recovery_tests.FAILING_JOB}),
                            what="纠正后的恢复预览（误用期间零执行）")
        audit.check_accounting(precise_delta=self._counted(campaign, reconciled), root_causes=1, repeats=[("两次误用对账", state, after)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(refusals[0], "部署收据", "已受管部署")
        audit.report(refusals=len(refusals))

    def _scenario_misuse_missing_client(self) -> None:
        """跨客户端误用：缺 client 的合同——Claude 部署收据（official-client-managed-deploy/v1）去掉 client 字段，对账入口零写入拒绝；
        Claude 的 pre-A3 认证合同（v3）缺 client 同样拒绝。补回 client 后同一入口照常对账。"""

        from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3

        campaign, first, audit, receipt, original = self._misuse_setup("跨客户端误用：缺 client 的合同")
        saved = receipt.read_bytes()
        stripped = {key: value for key, value in original.items() if key != "client"}
        receipt.write_text(json.dumps(stripped, ensure_ascii=False, indent=2), encoding="utf-8")
        state = audit.accounting_state()
        refused = self._refused_reconcile(campaign, first["attempt_id"])
        after = audit.accounting_state()
        with self.assertRaisesRegex(pre_a3.CertificationError, "不认识的客户端"):
            pre_a3.certification_client({"schema_version": pre_a3.SCHEMA_VERSION_V3, "scenarios": []})
        receipt.write_bytes(saved)
        audit.check_trusted()
        reconciled = self._corrected_reconcile(campaign, first["attempt_id"])
        audit.check_closure(executed=reconciled["recovery_preview"]["execute_job_ids"], expected=[recovery_tests.FAILING_JOB],
                            reused=reconciled["recovery_preview"]["reuse_job_ids"],
                            expected_reused=sorted(set(self._job_roots(campaign)) - {recovery_tests.FAILING_JOB}),
                            what="纠正后的恢复预览（误用期间零执行）")
        audit.check_accounting(precise_delta=self._counted(campaign, reconciled), root_causes=1, repeats=[("缺 client 时对账", state, after)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(refused, "部署收据", "已受管部署")
        audit.report()


# ---------------------------------------------------------------------------
# 场景组三：多根受监督部署（B5a-01 的写前意图日志与按内容恢复；部署替身环境见 arm64_deploy_fixture）
# ---------------------------------------------------------------------------


class _CandidateFaultScenarios(chain_driver.ClaudeCandidateChainDriver):
    """B 组：只保留集中登记的输入来源替身，真实对账／批准消费／续跑与演进判定共同推进一个候选。"""

    def _audit(self, scenario: str, campaign: Path, trusted: Iterable[Path]) -> ScenarioAudit:
        from tools.official_client_capture import claude_upgrade_store

        audit = ScenarioAudit(self, scenario)
        audit.snapshot(
            trusted=[campaign / "campaign.json", campaign / "official" / "result.json", *trusted],
            history=[claude_upgrade_store.campaign_store_root(campaign), self.ledger_dir / "events", self.ledger_dir / "ledger.json",
                     self.project_root / "events", self.project_root / "plan.json", campaign / "control" / "reconciliation",
                     campaign / "control" / "tool-evolution"],
            timing_dir=self.ledger_dir, project_root=self.project_root,
        )
        return audit

    def _scenario_candidate_reconcile_resume(self) -> None:
        """候选中继写完字节后失败 → 真实对账 → 未批准拒绝 → 批准与重复批准 → 只重跑失败作业。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_recovery as recovery

        campaign = self.candidate_world()
        first = self.candidate_run(campaign, fail_refresh=True)
        first_root, _first_attempt = self.candidate_attempt(campaign, first["attempt_id"])
        roots = self.candidate_job_roots(campaign)
        audit = self._audit("候选真实对账链端到端", campaign, [first_root, roots[chain_driver.LIVE_JOB]])
        calls = self.relay_probe_calls()
        reconciled = self.candidate_reconcile(campaign, first["attempt_id"])
        preview = reconciled["recovery_preview"]
        self.assertEqual((preview["phase"], preview["candidate_id"]), ("candidate", chain_driver.CANDIDATE))
        self.assertEqual((preview["execute_job_ids"], preview["reuse_job_ids"]),
                         ([chain_driver.REFRESH_JOB], [chain_driver.LIVE_JOB]))
        self.assertEqual(reconciled["resume_reuse_check"]["status"], "consistent")
        first_state = audit.accounting_state()
        receipt_bytes = file_digests(campaign / "control" / "reconciliation")
        repeated = self.candidate_reconcile(campaign, first["attempt_id"])
        second_state = audit.accounting_state()
        self.assertEqual(repeated["recovery_preview"]["review_sha256"], preview["review_sha256"])
        for path, digest in receipt_bytes.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        attempts_root = first_root.parent
        before = sorted(path.name for path in attempts_root.iterdir())
        with self.source_context():
            code, _result, refusal = self.run_main([
                "resume", "--campaign-dir", str(campaign), "--candidate-id", chain_driver.CANDIDATE, "--rerun-failed",
                "--recovery-preview", reconciled["recovery_preview_path"], "--acknowledge-live-requests",
            ])
        self.assertEqual(code, 1)
        self.assertIn("尚未批准", refusal)
        self.assertEqual(sorted(path.name for path in attempts_root.iterdir()), before)
        self.assertEqual(self.relay_probe_calls(), calls)
        approved = self.candidate_approve(campaign, first["attempt_id"], preview)
        approved_state = audit.accounting_state()
        again = self.candidate_approve(campaign, first["attempt_id"], preview)
        repeated_approval_state = audit.accounting_state()
        self.assertEqual(again["recovery_approval"], approved["recovery_approval"])
        resumed = self.candidate_resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual(self.relay_probe_calls()[len(calls):], [chain_driver.REFRESH_PROBE])
        attempt_root, attempt = self.candidate_attempt(campaign, resumed["attempt_id"])
        with self.source_context():
            source = recovery.load_recovery_source(campaign, attempt_root)
            status = claude_upgrade.campaign_status(campaign, chain_driver.CANDIDATE)
        self.assertEqual((source["source_attempt_id"], source["execute_job_ids"]),
                         (first["attempt_id"], [chain_driver.REFRESH_JOB]))
        self.assertEqual(source["recovery_approval"], {
            "index": approved["recovery_approval"]["index"],
            "receipt_sha256": approved["recovery_approval"]["receipt_sha256"],
        })
        self.assertIsNotNone(source["timing_recovery_event"], "恢复批准必须真实推进计时账本并绑定消费事件")
        self.assertEqual(status["status"], "candidate_awaiting_client_checkpoint")
        executed = [row["id"] for row in attempt["results"] if row["disposition"] == "executed"]
        reused = [row["id"] for row in attempt["results"] if row["disposition"] == "reused"]
        self.assertTrue(all(row["status"] == "complete" for row in attempt["results"]))
        counted = recovery_tests._read(campaign / reconciled["provenance_receipt"]["path"])["precise_total"]
        self.assertGreater(counted, 0, "替身中继原始字节必须真正进入请求核算，不能用零请求占位绕过")
        audit.check_trusted()
        audit.check_closure(executed=executed, expected=[chain_driver.REFRESH_JOB], reused=reused,
                            expected_reused=[chain_driver.LIVE_JOB], what="真实批准消费后的候选执行")
        audit.check_accounting(precise_delta=counted, root_causes=1, repeats=[
            ("重复候选对账", first_state, second_state), ("重复候选恢复批准", approved_state, repeated_approval_state),
        ])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry({"reconcile": reconciled["next_command"], "status": status["next_command"]}, "resume", "client-evidence")
        audit.report(candidate_id=chain_driver.CANDIDATE, source_attempt=first["attempt_id"], resumed_attempt=resumed["attempt_id"],
                     counted_requests=counted, input_source="claude_chain_driver 已登记的离线来源，B5b-02 待替换")

    def _scenario_candidate_tool_evolution(self) -> None:
        """隔离副本里修候选脚本：整体预览判定官方不受影响、已有候选两项失效，登记后真实对账续跑。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_store
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_control import receipts as store_receipts

        campaign = self.candidate_world()
        first = self.candidate_run(campaign)
        first_root, _attempt = self.candidate_attempt(campaign, first["attempt_id"])
        audit = self._audit("有候选 attempt 的整条工具演进预览", campaign, [first_root])
        calls = self.relay_probe_calls()
        # 必须在外层建立的隔离副本内变异；手工直接调用本场景时宁可拒绝，也不改用户源码。
        self.assertEqual(Path(os.environ.get(shell.TREE_ENV, "/missing")).resolve(), REPO_ROOT.resolve())
        shell._assert_isolation(self, shell._ChainRecorder("candidate-evolution-input"))
        path = Path(claude_upgrade.__file__).resolve().parent / chain_driver.EVOLVED_DEPENDENCY
        original = path.read_bytes()
        self.addCleanup(path.write_bytes, original)
        path.write_bytes(original + "\n# B5b-03：候选采集脚本修复后的身份来源。\n".encode("utf-8"))
        key = f"official_client_capture/{chain_driver.EVOLVED_DEPENDENCY}"
        self.tree.write(key, path.read_bytes())
        self.addCleanup(self.tree.restore)
        evolved = claude_upgrade._tool_identity(include_git=False)
        self.assertNotEqual(evolved["wire_producer_sha256"], self.tool_identity["wire_producer_sha256"])
        ledger_fixture.write_claude_deploy_receipt(self.data, evolved, stamp="20261008t020000z")
        argv = ["tool-evolution", "--campaign-dir", str(campaign), "--fix-commit", "b" * 40, "--reason", "修复候选采集脚本"]
        before = file_digests(campaign, self.ledger_dir, self.project_root)
        with self.source_context():
            code, preview, stderr = self.run_main(argv)
        self.assertEqual((code, preview["status"]), (0, "approval_required"), stderr)
        self.assertEqual(file_digests(campaign, self.ledger_dir, self.project_root), before, "完整演进预览必须只读")
        self.assertEqual(preview["changes"]["impact_paths"], [key])
        self.assertEqual(preview["changes"]["unmapped_paths"], [])
        self.assertFalse(preview["changes"]["wire_closure_changed"])
        self.assertEqual((preview["impact"]["official"]["sealed"], preview["impact"]["official"]["affected_job_ids"]), (True, []))
        self.assertEqual(set(preview["impact"]["candidates"]), {chain_driver.CANDIDATE})
        impact = preview["impact"]["candidates"][chain_driver.CANDIDATE]
        expected = sorted([chain_driver.LIVE_JOB, chain_driver.REFRESH_JOB])
        self.assertEqual((impact["planned_job_ids"], impact["affected_job_ids"], impact["sealed"]), (expected, expected, False))
        self.assertEqual(preview["evaluator"]["candidates"][chain_driver.CANDIDATE]["current_evaluation_baseline"], 0)
        approved_argv = [*argv, "--approve-sha256", preview["review_sha256"], "--approved-by", "故障恢复链"]
        with self.source_context():
            code, applied, stderr = self.run_main(approved_argv)
            self.assertEqual((code, applied["status"], applied["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)
            first_state = audit.accounting_state()
            code, again, stderr = self.run_main(approved_argv)
            self.assertEqual((code, again["reused"], again["store_registration"]["status"]), (0, True, "current"), stderr)
            second_state = audit.accounting_state()
            status = claude_upgrade.campaign_status(campaign, chain_driver.CANDIDATE)
        self.assertEqual(status["candidate_attempt"]["evolution_invalidated_job_ids"], expected)
        self.assertEqual(self.relay_probe_calls(), calls, "演进预览与登记不执行任何 probe")
        reconciled = self.candidate_reconcile(campaign, first["attempt_id"])
        recovery_preview = reconciled["recovery_preview"]
        self.assertEqual((recovery_preview["execute_job_ids"], recovery_preview["reuse_job_ids"]), (expected, []))
        self.candidate_approve(campaign, first["attempt_id"], recovery_preview)
        resumed = self.candidate_resume(campaign, reconciled["recovery_preview_path"])
        _root, replayed = self.candidate_attempt(campaign, resumed["attempt_id"])
        self.assertEqual(self.relay_probe_calls()[len(calls):], calls, "共同依赖变化后应恰好重跑候选全部 probe 一次")
        reservation = recovery_tests._read(_root / "reservation.json")
        self.assertEqual(reservation["tool_evolution"]["index"], 1)
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign))
        self.assertEqual(len(store_receipts.tool_evolutions(store, dict(store_campaign))), 1)
        counted = recovery_tests._read(campaign / reconciled["provenance_receipt"]["path"])["precise_total"]
        self.assertGreater(counted, 0)
        audit.check_trusted()
        audit.check_closure(executed=[row["id"] for row in replayed["results"] if row["disposition"] == "executed"], expected=expected,
                            reused=[row["id"] for row in replayed["results"] if row["disposition"] == "reused"], what="完整演进判定后的候选闭集")
        audit.check_accounting(precise_delta=counted, root_causes=0, repeats=[("重复候选工具演进批准", first_state, second_state)])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(status["next_command"], "reconcile-attempt")
        audit.report(candidate_id=chain_driver.CANDIDATE, invalidated=expected, resumed_attempt=resumed["attempt_id"],
                     counted_requests=counted, input_source="claude_chain_driver 已登记的离线来源，B5b-02 待替换")


class _DeployFaultScenarios(unittest.TestCase):
    """受监督部署在各故障点被杀（``fork`` 出的子进程在注入点 ``os._exit``，不执行任何 except／finally，现场就是进程被杀的
    样子）→ 下次启动的部署先按日志恢复：收据正文未入日志的残留按内容回滚到部署前、拒绝本次部署并提示重新执行；收据正文已入
    日志的残留补齐收据后照常部署 → 再部署成功。部署不碰两本账：数据根旁另建 Claude 项目总账与计时账本，核对它们逐字节不变。"""


    def setUp(self) -> None:
        from tools import arm64_supervised_deploy as deploy
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_capture.tests.arm64_deploy_fixture import DeployHarness

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.deploy = deploy
        self.capture_root, self.control_root = deploy.CAPTURE_ROOT, deploy.CONTROL_ROOT
        self.BOTH = ("--managed-roots", deploy.CAPTURE_ROOT, deploy.CONTROL_ROOT, "--client", "codex", "claude")
        self.harness = DeployHarness(self.root / "site")
        ledgers = self.root / "ledgers"
        ledgers.mkdir(mode=0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(ledgers)
        self.timing_dir = ledgers / "timing"
        timing.create_ledger(self.timing_dir, upgrade_id="claude-deploy-fault-ledger", baseline_version="2.1.226",
                             target_version="2.1.285", campaign_purpose="validation_only", evidence_decision="recapture", client="claude")

    # ---- 共用

    def _kill_at(self, install: Callable[[Any], contextlib.AbstractContextManager[Any]]) -> None:
        """子进程里装好注入点再跑部署，注入点处 ``os._exit(9)``；父进程确认子进程确实在注入点退出。"""

        failure = self.root / f"kill-failure-{time.monotonic_ns()}.txt"
        pid = os.fork()
        if pid == 0:  # pragma: no cover - 子进程
            code = 0
            try:
                with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    with install(self.harness):
                        self.harness.run(*self.BOTH)
                code = 98
            except BaseException as error:  # noqa: BLE001 —— 注入点之前就失败也算没有在注入点被杀，原因写给父进程
                import traceback

                failure.write_text("".join(traceback.format_exception(error)), encoding="utf-8")
                code = 97
            finally:
                os._exit(code)
        _pid, status = os.waitpid(pid, 0)
        detail = failure.read_text(encoding="utf-8")[-2000:] if failure.exists() else ""
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 9, f"部署子进程没有在注入点被杀：{status}\n{detail}")

    def _hook(self, attribute: str, target: Callable[[Any], Path]) -> Callable[[Any], contextlib.AbstractContextManager[Any]]:
        @contextlib.contextmanager
        def install(harness: Any):
            def die(first: Path, second: Path) -> None:
                if Path(second) == target(harness):
                    os._exit(9)

            setattr(harness, attribute, die)
            yield

        return install

    def _patch_kill(self, owner: Any, name: str, *, when: Callable[..., bool]) -> Callable[[Any], contextlib.AbstractContextManager[Any]]:
        @contextlib.contextmanager
        def install(_harness: Any):
            original = getattr(owner, name)

            def wrapper(*arguments: Any, **options: Any) -> Any:
                if when(*arguments, **options):
                    os._exit(9)
                return original(*arguments, **options)

            with mock.patch.object(owner, name, wrapper):
                yield

        return install

    def _audit(self, scenario: str, trusted: Iterable[Path]) -> ScenarioAudit:
        # 历史事实：已写的部署收据、改名留证的日志与两本账（活动日志按设计在事务收尾时删除或改名，不在其列）。
        control = self.harness.control
        history = [*sorted(control.glob("*-deploy-*.json")), *sorted(control.glob("*supervisor-enable*.json")),
                   *self.harness.retired_journals(), self.project_root, self.timing_dir]
        audit = ScenarioAudit(self, scenario)
        audit.snapshot(trusted=trusted, history=history, timing_dir=self.timing_dir, project_root=self.project_root)
        return audit

    def _production_paths(self) -> list[Path]:
        harness = self.harness
        return [harness.production(root) for root in self.deploy.MANAGED_ROOT_CLIENTS] + [
            harness.data / "tools" / self.deploy.MANAGED_ASSERTION_PREPARER, harness.docs]

    def _rolled_back(self, scenario: str, kill: Callable[[Any], contextlib.AbstractContextManager[Any]], *, control_root_old: bool | None,
                     expected_restored: list[str], recovery_kill: Callable[[Any], contextlib.AbstractContextManager[Any]] | None = None) -> None:
        """收据正文未入日志的残留：恢复按内容回滚到部署前（只动被切换过的对象）、拒绝本次部署并给出重新执行入口 → 再部署成功。"""

        harness = self.harness
        harness.build(control_root_old=control_root_old)
        before = harness.snapshot()
        audit = self._audit(scenario, self._production_paths())
        self._kill_at(kill)
        self.assertEqual(len(harness.journals()), 1, "被杀的部署应留下一份活动日志")
        if recovery_kill is not None:
            # 恢复本身在回滚交换完成、rollback_done 写入之前又被杀：再次启动按内容判已回滚，不重复交换。
            self._kill_at(recovery_kill)
        with self.assertRaises(self.deploy.DeploymentError) as refused:
            harness.run(*self.BOTH)
        (retired,) = harness.retired_journals()
        records = [json.loads(line) for line in retired.read_text(encoding="utf-8").splitlines() if line.strip()]
        done = [record for record in records if record.get("marker") == "rollback_done"]
        self.assertTrue(done, "回滚完成必须在日志里留下 rollback_done")
        restored_roots = sorted(item["object"] for item in done[-1]["restored"])
        self.assertEqual(harness.snapshot(), before, "恢复后全部受管对象回到部署前")
        audit.check_trusted()
        repeat_before = audit.accounting_state()
        harness.stamp = "20261008T010000Z"
        self.assertEqual(harness.run(*self.BOTH), 0, "恢复之后重新执行部署应成功")
        self.assertEqual((harness.journals(), len(harness.retired_journals())), ([], 1))
        self.assertEqual((len(harness.receipts("codex")), len(harness.receipts("claude"))), (1, 1))
        self.assertEqual(harness.tree(self.control_root), harness.staging_tree(self.control_root))
        audit.check_closure(executed=restored_roots, expected=expected_restored, what="启动恢复按内容换回的受管根")
        audit.check_accounting(repeats=[("再部署", repeat_before, audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(str(refused.exception), "已按内容恢复到部署前", "重新执行")
        audit.report(retired_journal=harness.retired_journals()[0].name)

    def _receipts_residue(self, scenario: str, kill: Callable[[Any], contextlib.AbstractContextManager[Any]], *,
                          expected_written: int) -> None:
        """收据正文已入日志的残留：再次部署先核对新版本、按日志正文补齐缺失的收据（已写的只核对不改），删日志后照常部署。"""

        harness = self.harness
        harness.build(control_root_old=True)
        self._kill_at(kill)
        (journal,) = harness.journals()
        prepared = {item["client"]: item for item in self.deploy.SwitchJournal.load(journal).receipts or []}
        written_before = sorted(path.name for path in [*harness.receipts("codex"), *harness.receipts("claude")])
        audit = self._audit(scenario, [*self._production_paths(), *harness.receipts("codex"), *harness.receipts("claude")])
        harness.stamp = "20261008T020000Z"
        self.assertEqual(harness.run(*self.BOTH), 0, "收据残留补齐后本次部署照常进行")
        recovered = json.loads(harness.last_output)["recovered_switch_journal"]
        self.assertEqual(recovered["outcome"], "receipts-completed")
        missing = sorted(Path(item["path"]).name for item in prepared.values() if Path(item["path"]).name not in written_before)
        self.assertEqual(len(missing), expected_written)
        repeat_before = audit.accounting_state()
        self.assertEqual(self.deploy.recover_switch_journal(harness.control)["outcome"], "none", "日志已删：再次启动无事可做")
        audit.check_trusted()
        audit.check_closure(executed=recovered["written_receipts"], expected=missing,
                            reused=recovered["verified_receipts"], expected_reused=written_before, what="按日志正文补齐的收据")
        audit.check_accounting(repeats=[("再次启动恢复", repeat_before, audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(recovered, "receipts-completed")
        audit.report(written=recovered["written_receipts"], verified=recovered["verified_receipts"])

    # ---- 各故障点（设计 B5a-01 ②～⑦）

    def _scenario_deploy_after_exchange(self) -> None:
        self._rolled_back("多根部署故障点：首根交换完成后被杀", self._hook("after_exchange", lambda h: h.production(self.capture_root)),
                          control_root_old=True, expected_restored=[self.capture_root])

    def _scenario_deploy_after_backup(self) -> None:
        self._rolled_back("多根部署故障点：旧树已入备份、第二根交换前被杀",
                          self._hook("before_exchange", lambda h: h.production(self.control_root)),
                          control_root_old=True, expected_restored=[self.capture_root])

    def _scenario_deploy_after_new_root(self) -> None:
        self._rolled_back("多根部署故障点：首次新增的根切入后被杀",
                          self._hook("after_rename_noreplace", lambda h: h.production(self.control_root)),
                          control_root_old=None, expected_restored=[self.capture_root, self.control_root])

    def _scenario_deploy_interrupted_rollback(self) -> None:
        deploy = self.deploy
        self._rolled_back(
            "多根部署故障点：恢复回滚中再被杀",
            self._hook("before_exchange", lambda h: h.production(self.control_root)),
            control_root_old=True, expected_restored=[],
            recovery_kill=self._patch_kill(deploy.SwitchJournal, "append", when=lambda _journal, marker, **_fields: marker == "rollback_done"),
        )

    def _scenario_deploy_one_receipt_written(self) -> None:
        deploy = self.deploy
        written: list[Any] = []

        def second_write(path: Path, _payload: Any) -> bool:
            written.append(path)
            return len(written) == 2

        self._receipts_residue("多根部署故障点：只写出一份收据时被杀", self._patch_kill(deploy, "write_json_once", when=second_write),
                               expected_written=1)

    def _scenario_deploy_receipts_written(self) -> None:
        deploy = self.deploy
        self._receipts_residue("多根部署故障点：收据全部写出、日志未删时被杀",
                               self._patch_kill(deploy.SwitchJournal, "remove", when=lambda _journal: True), expected_written=0)


# ---------------------------------------------------------------------------
# 场景组四：前序证据导入的跨客户端／跨环境误用（B2-09 的前序 Store 与带受管 Store 的 Claude 目标 Campaign）
# ---------------------------------------------------------------------------


class _PredecessorMisuseScenarios(predecessor_tests._PredecessorCase):
    """x86_64 证据喂给 arm64 目标、夹具收据冒充正式引用：Store 侧零写入拒绝。导入不碰两本账：工作区另建 Claude 项目总账与
    计时账本，核对它们逐字节不变。"""

    def setUp(self) -> None:
        super().setUp()
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture

        ledgers = self.workspace / "ledgers"
        ledgers.mkdir(mode=0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(ledgers)
        self.timing_dir = ledgers / "timing"
        timing.create_ledger(self.timing_dir, upgrade_id="claude-predecessor-misuse-ledger", baseline_version="2.1.226",
                             target_version="2.1.285", campaign_purpose="validation_only", evidence_decision="recapture", client="claude")

    def _audit(self, scenario: str, *, trusted: Iterable[Path], history: Iterable[Path]) -> ScenarioAudit:
        audit = ScenarioAudit(self, scenario)
        audit.snapshot(trusted=trusted, history=[*history, self.project_root, self.timing_dir],
                       timing_dir=self.timing_dir, project_root=self.project_root)
        return audit

    def _scenario_misuse_x86_evidence(self) -> None:
        """跨客户端误用：x86_64 证据喂给 arm64 目标——前序是 linux/amd64 平台的 Store Campaign，arm64 目标的总计划冻结了它，
        read_only_reuse 导入仍按七项身份逐项相等零写入拒绝（拒绝原因写明 platform）；只能以 regression_fixture 登记（见下一场景）。"""

        from tools.official_client_control.store import ControlStore

        other = self.make_predecessor("amd64-predecessor", schema="v2", **{**predecessor_tests.SYNTHETIC_TARGET, "platforms": ["linux/amd64"]})
        campaign_dir, _fixture = self.make_target(
            "amd64", predecessor=self.binding(ControlStore.open_read_only(other.store_root), predecessor_tests.PREDECESSOR_ID))
        target_store = self.target_store(campaign_dir).root
        audit = self._audit("跨客户端误用：x86_64 证据喂给 arm64 目标", trusted=[other.store_root, target_store, campaign_dir / "campaign.json"],
                            history=[target_store])
        state = audit.accounting_state()
        code, _stdout, stderr = predecessor_tests._run_main(predecessor_tests._import_argv(
            campaign_dir, other.store_root, predecessor_tests.PREDECESSOR_ID, "read_only_reuse"))
        self.assertEqual(code, 1, stderr)
        self.assertRegex(stderr, "read_only_reuse 要求七项全部可证且逐项相等：platform")
        imported = self.target_store(campaign_dir).list_object_refs("predecessor_import")
        audit.check_trusted()
        audit.check_closure(executed=[str(item) for item in imported], expected=[], what="被拒绝的前序导入（登记对象）")
        audit.check_accounting(repeats=[("被拒绝的导入", state, audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(stderr, "platform", "七项全部可证且逐项相等")
        audit.report(predecessor_platform="linux/amd64")

    def _scenario_misuse_fixture_as_formal(self) -> None:
        """跨客户端误用：夹具收据冒充正式引用——regression_fixture 导入（x86_64 前序，三项身份差异如实登记）成功但逐个对象都不
        授予正式引用；拿导入的 EvidencePackage 写本 Campaign 的 EvidenceFact 被 Store 拒绝（零写入），以同一导入重跑复用原登记。"""

        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.store import ControlStore

        predecessor = self.make_predecessor()
        campaign_dir, fixture = self.make_target(
            predecessor=self.binding(ControlStore.open_read_only(predecessor.store_root), predecessor_tests.PREDECESSOR_ID))
        self.lock(predecessor.store_root)
        target_store = self.target_store(campaign_dir).root
        audit = self._audit("跨客户端误用：夹具收据冒充正式引用", trusted=[predecessor.store_root, campaign_dir / "campaign.json"],
                            history=[target_store])
        summary = self.run_import(campaign_dir, predecessor.store_root, predecessor_tests.PREDECESSOR_ID, "regression_fixture")
        self.assertEqual([item["item"] for item in summary["identity_differences"]], ["platform", "oauth_org_id", "model_catalog_sha256"])
        self.assertTrue(summary["formal_references"])
        self.assertTrue(all(not item["authorized"] for item in summary["formal_references"]))
        facts_before = self.target_store(campaign_dir).list_facts(predecessor_tests.TARGET_ID)
        state = audit.accounting_state()
        with self.assertRaisesRegex(ControlError, "未获授权引用：③") as refused:
            self.write_target_evidence(campaign_dir, fixture, summary["evidence_package_ref"])
        # EvidenceFact 被拒（Discovery 是本 Campaign 自己的，先于引用核验写入，属合法事实）。
        facts_after = self.target_store(campaign_dir).list_facts(predecessor_tests.TARGET_ID)
        self.assertEqual([fact["fact_kind"] for fact in facts_after if fact["fact_kind"] == "evidence_recorded"],
                         [fact["fact_kind"] for fact in facts_before if fact["fact_kind"] == "evidence_recorded"])
        again = self.run_import(campaign_dir, predecessor.store_root, predecessor_tests.PREDECESSOR_ID, "regression_fixture")
        self.assertEqual((again["import_ref"], again["receipt_reused"]), (summary["import_ref"], True))
        audit.check_trusted()
        audit.check_closure(executed=[item["fact_kind"] for item in summary["imported_facts"]],
                            expected=["discovery_recorded", "evidence_recorded"], what="夹具导入登记的事实（不授予正式引用）")
        audit.check_accounting(repeats=[("拒绝的正式引用与重跑导入", state, audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(str(refused.exception), "未获授权引用", "③")
        audit.report(identity_differences=[item["item"] for item in summary["identity_differences"]], live_request_count=summary["live_request_count"])


# ---------------------------------------------------------------------------
# 链：recovery-chain 的场景组（步骤名 → {场景方法: 场景名}；场景名与各场景 ScenarioAudit 的名字逐字相同）
# ---------------------------------------------------------------------------

RECOVERY_CHAIN_GROUPS: tuple[tuple[str, Mapping[str, str]], ...] = (
    ("真实副本树批次：父进程中断、提交中崩溃两层、seal 失败、同根因暂停、预算到期", {
        "_BatchFaultScenarios._scenario_parent_interrupted": "父进程中断",
        "_BatchFaultScenarios._scenario_commit_publish_crash": "提交中崩溃（批次派发事务层）",
        "_BatchFaultScenarios._scenario_action_wal_crash": "提交中崩溃（动作结果 WAL 层）",
        "_BatchFaultScenarios._scenario_seal_batch_failure": "seal 失败",
        "_BatchFaultScenarios._scenario_same_root_cause_pause": "同根因暂停",
        "_BatchFaultScenarios._scenario_budget_expired": "预算到期",
    }),
    ("官方取证：预约后中断、账务未决、工具演进后旧链重放、Codex 收据与缺 client 的合同", {
        "_OfficialFaultScenarios._scenario_reserved_attempt_interrupted": "预约后中断",
        "_OfficialFaultScenarios._scenario_accounting_unresolved": "账务未决",
        "_OfficialFaultScenarios._scenario_tool_evolution_replay": "工具演进后旧链重放",
        "_OfficialFaultScenarios._scenario_misuse_codex_receipt": "跨客户端误用：Codex 收据喂给 Claude 入口",
        "_OfficialFaultScenarios._scenario_misuse_missing_client": "跨客户端误用：缺 client 的合同",
    }),
    ("候选取证：真实对账批准续跑、有候选 attempt 的完整工具演进预览", {
        "_CandidateFaultScenarios._scenario_candidate_reconcile_resume": "候选真实对账链端到端",
        "_CandidateFaultScenarios._scenario_candidate_tool_evolution": "有候选 attempt 的整条工具演进预览",
    }),
    ("多根受监督部署各故障点", {
        "_DeployFaultScenarios._scenario_deploy_after_exchange": "多根部署故障点：首根交换完成后被杀",
        "_DeployFaultScenarios._scenario_deploy_after_backup": "多根部署故障点：旧树已入备份、第二根交换前被杀",
        "_DeployFaultScenarios._scenario_deploy_after_new_root": "多根部署故障点：首次新增的根切入后被杀",
        "_DeployFaultScenarios._scenario_deploy_interrupted_rollback": "多根部署故障点：恢复回滚中再被杀",
        "_DeployFaultScenarios._scenario_deploy_one_receipt_written": "多根部署故障点：只写出一份收据时被杀",
        "_DeployFaultScenarios._scenario_deploy_receipts_written": "多根部署故障点：收据全部写出、日志未删时被杀",
    }),
    ("前序证据导入：x86_64 证据与夹具收据冒充正式引用", {
        "_PredecessorMisuseScenarios._scenario_misuse_x86_evidence": "跨客户端误用：x86_64 证据喂给 arm64 目标",
        "_PredecessorMisuseScenarios._scenario_misuse_fixture_as_formal": "跨客户端误用：夹具收据冒充正式引用",
    }),
)


class ClaudeFaultChainTests(unittest.TestCase):
    """外层场景入口（方法直接写在类体里，部署脚本按 AST 核对）与链体（只在副本树子进程里运行）。"""

    real_chain_metrics: dict[str, Any]

    # ---- 外层：pre-A3 登记的场景入口 ----

    def test_recovery_chain_keeps_trusted_results_and_resumes_every_fault(self) -> None:
        self._run_chain("recovery")

    def _isolation_mode(self) -> str:
        """Linux 上以 root 运行必须进命名空间（缺 ``unshare`` 即失败，不降级）；其余环境降级为副本树加替身。"""

        if sys.platform.startswith("linux") and os.geteuid() == 0:
            if shutil.which("unshare") is None:
                self.fail("Linux root 上必须进 unshare -m -n 命名空间运行，但 PATH 里没有 unshare")
            return shell.ISOLATION_NAMESPACE
        return shell.ISOLATION_DEGRADED

    def _run_chain(self, chain: str) -> None:
        spec = CHAINS[chain]
        started = time.monotonic()
        mode = self._isolation_mode()
        metrics: dict[str, Any] = {}
        completed: subprocess.CompletedProcess | None = None
        copy_seconds: float | None = None
        timed_out = False
        try:
            with tempfile.TemporaryDirectory(prefix=f"claude-fault-chain-{chain}-", dir=shell._scratch_root()) as directory:
                staging = Path(directory).resolve() / "staging"
                staging.mkdir(mode=0o700)
                # 与 B5a-05 相同：staging 的属组改成本进程的有效组（macOS 新建目录取父目录属组）。
                os.chown(staging, -1, os.getegid())
                tree = _copy_tree(staging / "tree")
                copy_seconds = round(time.monotonic() - started, 3)
                environment = trees.subprocess_env(tree, {
                    project_ledger_fixture.FIXTURE_ONLY_ENV: "1",
                    "TMPDIR": str(staging),
                    shell.ISOLATION_ENV: mode,
                    shell.TREE_ENV: str(tree),
                    shell.SOURCE_REPO_ENV: str(REPO_ROOT),
                    shell.IDENTITY_MEMO_ENV: str(staging / "identity-memo"),
                })
                argv = [sys.executable, "-m", "unittest", "-v", f"{CHAIN_MODULE}.{type(self).__name__}.{spec['exercise']}"]
                command = shell._namespace_argv(argv) if mode == shell.ISOLATION_NAMESPACE else argv
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, cwd=str(tree), env=environment,
                                               timeout=CHAIN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    timed_out = True
                if completed is not None:
                    metrics = _parse_metrics(completed.stdout)
        finally:
            elapsed = round(time.monotonic() - started, 3)
            self.real_chain_metrics = {
                **{key: value for key, value in metrics.items() if key != "chain"},
                "scenario": spec["scenario"],
                "seconds": elapsed,
                "copy_seconds": copy_seconds,
                "isolation": mode,
                "network_isolated": mode == shell.ISOLATION_NAMESPACE,
                "returncode": None if completed is None else completed.returncode,
            }
        if timed_out or elapsed >= CHAIN_BUDGET_SECONDS:
            self.fail(f"{spec['scenario']} 超过 {CHAIN_BUDGET_SECONDS} 秒（实测 {elapsed} 秒）：每条链的验收上限是 15 分钟")
        assert completed is not None
        if completed.returncode != 0:
            self.fail(f"{spec['scenario']} 子进程失败（退出码 {completed.returncode}，隔离={mode}，耗时 {elapsed} 秒；已通过的步骤："
                      f"{'、'.join(metrics.get('steps') or []) or '没有'}）：\n{completed.stdout[-3000:]}\n{completed.stderr[-6000:]}")
        self.assertNotIn("skipped=", completed.stderr, "链体不得跳过")
        self.assertTrue(metrics.get("steps"), "通过的链必须写出指标与已通过的步骤")
        self.assertEqual(metrics.get("upstream_requests"), 0, "链体必须零请求")
        expected = [name for _step, group in RECOVERY_CHAIN_GROUPS for name in group.values()]
        self.assertEqual([record["scenario"] for record in metrics.get("scenarios") or []], expected, "每个场景都要写出六项记录")

    # ---- 链体：recovery-chain ----

    def _exercise_recovery_chain(self) -> None:
        recorder = _ChainRecorder("recovery")
        shell._assert_isolation(self, recorder)
        for step, group in RECOVERY_CHAIN_GROUPS:
            _run_group(self, recorder, group, step)
        recorder.facts["six_checks"] = {record["scenario"]: sorted(record["checks"]) for record in recorder.facts["scenarios"]}
        recorder.emit()


if __name__ == "__main__":
    unittest.main()
