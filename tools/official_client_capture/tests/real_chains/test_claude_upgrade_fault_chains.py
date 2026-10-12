"""Claude 升级 B5b-03：故障恢复真实链（pre-A3 场景，零请求、隔离副本树）。

本模块登记三条链，外层入口与 pre-A3 登记（``codex_upgrade_pre_a3_certification.CLAUDE_SCENARIOS``）一一对应：

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
* ``vc-chain.claude-late-stage-faults``：VC-2～VC-6 一个 Campaign 的七场景连续后段故障链；当前只登记既有可执行
  入口，不预填通过。
* ``vc-chain.claude-revision-evaluator-recovery``：同一 Campaign 的 r2／评估器八场景链，公共入口仅在 Linux ARM64 root
  下执行原隔离外壳；本机私有入口与轻量结果不能充当正式链认证。

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
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import stat
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

from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import claude_chain_diagnostic_budget as diagnostic_budget
from tools.official_client_capture.tests import claude_chain_driver as chain_driver
from tools.official_client_capture.tests import claude_late_stage_chain_driver as late_driver
from tools.official_client_capture.tests import claude_late_control_chain_driver as late_control_driver
from tools.official_client_capture.tests import claude_transient_chain_driver as transient_driver
from tools.official_client_capture.tests import claude_fault_chain_world as stops
from tools.official_client_capture.tests.real_chains import claude_revision_evaluator_scenarios as revision_scenarios
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests
from tools.official_client_capture.tests import test_claude_upgrade_predecessor as predecessor_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_staging_recovery as staging_tests
from tools.official_client_capture.tests.real_chains import test_claude_upgrade_control_chain as shell

REPO_ROOT = Path(__file__).resolve().parents[4]
# 链体在子进程里按点分名运行；不用 __name__，直接以脚本运行本文件时它是 __main__。
CHAIN_MODULE = "tools.official_client_capture.tests.real_chains." + Path(__file__).stem

# late 的七场景与 revision 的八场景是不同闭集；登记入口不合并两者，也不证明完整认证。
LATE_CONTINUOUS_SCENARIOS = (
    '候选 transient 裁定与恢复段续跑', '后段同根因暂停并续派 compare', '后段预算到期并延期续派 builder',
    '后段批次 COMMIT 发布中断', '后段交付修复后动作中断', '后段交付阶段批准失效', '后段交付收据后中断与重复收尾',
)
REVISION_CONTINUOUS_SCENARIOS = (
    '候选源码失败与 r2 修订接续', '评估器修复拒绝错误部署收据：codex', '评估器修复拒绝错误部署收据：missing-client',
    '评估器真实修复与 b1 零采集重评', '后段批次 COMMIT 发布中断', '后段交付修复后动作中断',
    '后段交付阶段批准失效', '后段交付收据后中断与重复收尾',
)
# 场景 ID → 链体方法名（与 pre-A3 登记、外层 test_* 方法一一对应）；通过状态只来自实际运行。
CHAINS: Mapping[str, Mapping[str, Any]] = {
    "recovery": {"scenario": "vc-chain.claude-recovery-chain", "exercise": "_exercise_recovery_chain"},
    'late-continuous': {'scenario': 'vc-chain.claude-late-stage-faults', 'exercise': '_exercise_late_continuous',
                        'expected': LATE_CONTINUOUS_SCENARIOS},
    'revision-continuous': {'scenario': 'vc-chain.claude-revision-evaluator-recovery', 'exercise': '_exercise_revision_continuous',
                            'expected': REVISION_CONTINUOUS_SCENARIOS},
}

# 保留待验表接口供显式本机入口读取；公共入口登记不表示任何链已实际通过。
PENDING_CONTINUOUS_CHAINS: Mapping[str, Mapping[str, Any]] = {}

# 单连续方法链的兼容导出；recovery 按登记的 21 个独立方法计算外层上限。
# 时间合同只由 pre-A3 定义，避免内部已扩时却仍被外层执行器截断。
CHAIN_BUDGET_SECONDS = certification.claude_fault_time_budget("vc-chain.claude-late-stage-faults")["chain_budget_seconds"]
CHAIN_TIMEOUT_SECONDS = certification.claude_fault_time_budget("vc-chain.claude-late-stage-faults")["chain_timeout_seconds"]

# 场景组写六项记录的文件（链体设置；本机单跑场景时不设置，只做断言）。
RECORDS_ENV = "CLAUDE_FAULT_CHAIN_RECORDS"
METRICS_PREFIX = "CLAUDE_FAULT_CHAIN_METRICS "
# 原公共入口生成的内部坐标，不从 CI 环境白名单新增旁路入口。
METHODS_ROOT_ENV = "CLAUDE_FAULT_CHAIN_METHODS"
METHOD_VERIFY_ENV = "CLAUDE_FAULT_METHOD_VERIFY"

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


def ledger_totals(*, timing_dir: Path | None = None, project_root: Path | None = None,
                  project_limit: int | None = None, timing_limit: int | None = None,
                  project_bindings: list[dict[str, Any]] | None = None,
                  now: str | None = None) -> dict[str, Any]:
    """两本账的入账口径：项目总账的精确／估计请求数、根因计数与操作数，计时账本的请求数与累计时长。"""

    from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
    from tools.official_client_capture import codex_upgrade_timing_ledger as timing

    totals: dict[str, Any] = {}
    if project_root is not None:
        if project_limit is None:
            plan, _raw = project_ledger._load_plan(project_root)
            history = project_ledger.read_project_history_snapshot(project_root)
            head = project_ledger._replay(project_root, plan, history['events'], rebuild_cache=False)
        else:
            # 先核完整原链与原head，再只读重算绑定过的精确历史前缀。
            # 私有切片不带head，也不进入执行世界、产品引用或原缓存写入。
            history = project_ledger.read_project_history_snapshot(project_root)
            if not project_bindings or project_limit < 0 or project_limit > history['head_sequence']:
                raise ValueError('历史项目账切点缺原字节绑定或序号越界')
            originals = {Path(binding['path']).relative_to(project_root).as_posix(): stops.read_cut(binding)
                         for binding in project_bindings}
            expected = {'plan.json', 'initial-identity-keys.json',
                *(f'events/{index:06d}.json' for index in range(1, project_limit + 1))}
            extras = set(originals) - expected
            if any(not re.fullmatch(r'migrations/[0-9]{6}\.json', name) for name in extras):
                raise ValueError('历史项目账只读切片有未声明文件')
            if not expected <= set(originals) or len(originals) != len(project_bindings):
                raise ValueError('历史项目账缺plan、初始身份键或完整事件前缀')
            with tempfile.TemporaryDirectory(prefix='cf-audit-ledger-') as directory:
                audit_root = Path(directory)
                (audit_root / 'events').mkdir(mode=0o700)
                if extras:
                    (audit_root / 'migrations').mkdir(mode=0o700)
                for relative, raw in originals.items():
                    path = audit_root / relative
                    path.write_bytes(raw)
                    path.chmod(0o400)
                plan, _raw = project_ledger._load_plan(audit_root)
                events = project_ledger._load_events(audit_root)
                if events != history['events'][:project_limit]:
                    raise ValueError('绑定历史前缀不是当前原账本事件链祖先')
                head = project_ledger._replay(audit_root, plan, events, rebuild_cache=False)
        totals["project"] = {
            "precise_total": head["precise_total"],
            "estimated_total": head["estimated_total"],
            "root_cause_counts": dict(head["root_cause_counts"]),
            "operations": len(head["operations"]),
            "sequence": head["sequence"],
        }
    if timing_dir is not None:
        summary = timing.inspect_ledger(timing_dir, project_ledger_optional=True, limit=timing_limit, now=now)
        events = [event for event, _raw in timing._load_events(timing_dir, limit=timing_limit)]
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
        self.inputs: dict[str, dict[str, Any]] = {}
        self.replay_cut: dict[str, Any] = {}
        self.ledger_observations: list[dict[str, Any]] = []
        self.before_ledger: dict[str, Any] | None = None
        self.revalidating = False
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
        saved = stops.load_state('audit-before:' + self.scenario)
        if saved is not None and os.environ.get(stops.METHOD_RESUME_ENV):
            for key, value in saved.items():
                setattr(self, key, value)
            self.ledger_observations = (stops.load_state('audit-ledger:' + self.scenario) or {}).get('observations', [])
            # 重用的是原 before；不是把停点后的当前态当成故障前状态。
            return
        cursor_name = getattr(self.case, '_fault_cursor_name', None)
        cursor = stops.load_state('cursor:' + cursor_name) if cursor_name else None
        if os.environ.get(stops.METHOD_RESUME_ENV) and saved is None:
            if cursor is None or cursor.get('audit_snapshots', {}).get(self.scenario) != 'uninitialized':
                raise ValueError(f'{self.scenario} 缺少原故障前快照，禁止从当前态补造 before')
        self.trusted_paths = tuple(Path(path) for path in trusted)
        self.trusted_before = file_digests(*self.trusted_paths)
        self.case.assertTrue(self.trusted_before, f"{self.scenario}：故障前没有可信产物可核对")
        self.history_paths = tuple(Path(path) for path in history)
        self.history_before = file_digests(*self.history_paths)
        self.timing_dir, self.project_root = timing_dir, project_root
        self.totals_before = self.totals()
        self.before_ledger = self.ledger_observations[-1] if self.ledger_observations else None
        stops.save_state('audit-before:' + self.scenario, self._before())
        if cursor is not None:
            cursor.setdefault('audit_snapshots', {})[self.scenario] = 'recorded'
            stops.save_state('cursor:' + cursor_name, cursor)

    def _before(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in ('trusted_paths', 'trusted_before', 'history_paths',
            'history_before', 'timing_dir', 'project_root', 'totals_before', 'before_ledger')}

    def object_source(self, value: Any) -> dict[str, Any] | None:
        """定位原命令完整输出中的同一个对象；只给审计绑定原件，不授予动作跳过权。"""

        if stops.record_root() is None:
            return None

        def locate(current: Any, trail: list[Any]) -> list[Any] | None:
            if type(current) is type(value) and current == value:
                return trail
            if isinstance(current, dict):
                for key, item in current.items():
                    found = locate(item, [*trail, key])
                    if found is not None:
                        return found
            if isinstance(current, list):
                for index, item in enumerate(current):
                    found = locate(item, [*trail, index])
                    if found is not None:
                        return found
            return None

        for row in reversed((stops.load_state('command-observations') or {}).get('commands', [])):
            for label in ('stdout', 'stderr'):
                if row[label] is None:
                    continue
                raw = stops.read_cut(row[label]).decode('utf-8')
                if label == 'stderr' and isinstance(value, str) and raw == value:
                    return {'binding': row[label], 'format': 'text', 'trail': [], 'command': row['argv'],
                            'returncode': row['returncode'], 'command_binding': row.get('command_binding')}
                if not raw.strip():
                    continue
                try:
                    document = json.loads(raw)
                except ValueError:
                    continue
                trail = locate(document, [])
                if trail is not None:
                    return {'binding': row[label], 'format': 'json', 'trail': trail,
                        'command': row['argv'], 'returncode': row['returncode'],
                        'command_binding': row.get('command_binding')}
        raise ValueError(f'{self.scenario} 缺原 stdout/JSON 对象绑定，不能仅保存审计输入摘要')

    @staticmethod
    def read_source(source: dict[str, Any]) -> Any:
        if source.get('command_binding') is not None:
            command = stops._decoded(json.loads(stops.read_cut(source['command_binding'])))
            if (command['argv'] != source['command'] or command['returncode'] != source['returncode']
                    or source['binding'] not in (command.get('stdout'), command.get('stderr'))):
                raise ValueError('原命令、退出码与输出原件不一致')
            invocation = command.get('invocation_binding')
            if invocation is None or json.loads(stops.read_cut(invocation)) != {'argv': command['argv']}:
                raise ValueError('原输出缺副作用前的完整调用绑定')
            for label in ('stdout', 'stderr'):
                if command.get(label) is not None:
                    stops.read_cut(command[label])
        elif source.get('command') is not None:
            if source['command'] != ['原 JSON 读侧', str(source['binding']['path'])]:
                raise ValueError('命令输出缺原描述文件，不能当作文件读侧来源')
        text = stops.read_cut(source['binding']).decode('utf-8')
        if source['format'] == 'text':
            return text
        value = json.loads(text)
        for key in source['trail']:
            value = value[key]
        return value

    def closure_source(self, value: Any, executed: tuple[str, ...], reused: tuple[str, ...] = (),
                       *, row_key: str | None = None, disposition: str | None = None) -> dict[str, Any] | None:
        """调用方明示原结果字段；恢复从该原件重新投影实际执行／复用闭集。"""

        source = self.object_source(value)
        return None if source is None else {'source': source, 'executed': executed, 'reused': reused,
            'row_key': row_key, 'disposition': disposition}

    def delivery_closure_source(self, path: Path, begin: int = 0) -> dict[str, Any] | None:
        if stops.record_root() is None:
            return None
        return {'calls': stops.file_cut(path), 'begin': begin}

    def file_closure_source(self, path: Path, executed: tuple[str, ...], reused: tuple[str, ...] = (),
                            *, row_key: str | None = None, disposition: str | None = None,
                            reused_path: Path | None = None) -> dict[str, Any] | None:
        if stops.record_root() is None:
            return None
        source = {'source': {'binding': stops.file_cut(path), 'format': 'json', 'trail': []},
            'executed': executed, 'reused': reused, 'row_key': row_key, 'disposition': disposition}
        if reused_path is not None:
            source['reused_source'] = {'binding': stops.file_cut(reused_path), 'format': 'json', 'trail': []}
        return source

    def refusal_closure_source(self, stderr: str) -> dict[str, Any] | None:
        """拒绝命令的空闭集必须绑定真实退出和完整事实快照，不能用空列表追认。"""

        if stops.record_root() is None:
            return None
        source = self.object_source(stderr)
        if source is None or source.get('returncode') in (None, 0) or not source.get('command_binding'):
            raise ValueError('空执行闭集缺真实非零退出的命令原件')
        paths = tuple(dict.fromkeys((*self.trusted_paths, *self.history_paths)))
        before = {**self.trusted_before, **self.history_before}
        after = file_digests(*paths)
        self.case.assertTrue(paths and before, '拒绝闭集必须声明实际受管事实根')
        self.case.assertEqual(after, before, '拒绝命令不能创建、删除或改写声明事实根中的文件')
        return {'refusal': source, 'facts': after,
                'bindings': [stops.file_cut(Path(path)) for path in sorted(after)]}

    def journal_closure_source(self, path: Path, marker: str, field: str) -> dict[str, Any] | None:
        if stops.record_root() is None:
            return None
        return {'journal': stops.file_cut(path), 'marker': marker, 'field': field}

    def _closure_original(self, source: dict[str, Any]) -> tuple[list[str], list[str]]:
        if 'journal' in source:
            rows = [json.loads(line) for line in stops.read_cut(source['journal']).decode('utf-8').splitlines() if line]
            matches = [row for row in rows if row.get('marker') == source['marker']]
            if not matches:
                raise ValueError('原部署日志缺所声明的恢复marker')
            return sorted(row['object'] for row in matches[-1][source['field']]), []
        if 'refusal' in source:
            stderr = self.read_source(source['refusal'])
            if not stderr or source['refusal'].get('returncode') in (None, 0):
                raise ValueError('拒绝闭集缺完整原 stderr 或真实非零退出')
            original = {str(binding['path']): hashlib.sha256(stops.read_cut(binding)).hexdigest()
                        for binding in source['bindings']}
            if original != source['facts'] or original != {**self.trusted_before, **self.history_before}:
                raise ValueError('拒绝闭集的原事实字节与故障前快照不一致')
            return [], []
        if 'calls' in source:
            rows = [json.loads(line) for line in stops.read_cut(source['calls']).decode('utf-8').splitlines() if line]
            return [f'{segment}:{stage}' for segment, stage in rows[source['begin']:]], []
        value = self.read_source(source['source'])
        def items(keys: Iterable[str], disposition: str) -> list[str]:
            result = (self.read_source(source['reused_source'])
                      if disposition == 'reused' and 'reused_source' in source else value)
            for key in keys:
                result = result[key]
            if not keys:
                return []
            if source['disposition'] is not None and result and isinstance(result[0], dict):
                result = [row for row in result if row[source['disposition']] == disposition]
            if source['row_key'] is not None and result and isinstance(result[0], dict):
                result = [row[source['row_key']] for row in result]
            return sorted(result)
        return items(source['executed'], 'executed'), items(source['reused'], 'reused')

    def totals(self) -> dict[str, Any]:
        if self.revalidating or stops.record_root() is None:
            return ledger_totals(timing_dir=self.timing_dir, project_root=self.project_root, **self.replay_cut)
        observed = datetime.now(timezone.utc).isoformat()
        totals = ledger_totals(timing_dir=self.timing_dir, project_root=self.project_root, now=observed)
        cut, bindings = self._ledger_cut(totals, observed)
        row = {'cut': cut, 'bindings': bindings, 'totals': totals}
        self.ledger_observations.append(row)
        stops.save_state('audit-ledger:' + self.scenario, {'observations': self.ledger_observations})
        return totals

    def _ledger_cut(self, totals: dict[str, Any], observed: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """两本账用原事件序号与原字节绑定；后续追加不影响已经消费的切点。"""

        cut: dict[str, Any] = {'now': observed}
        bindings = []
        for label, root, plan_name, field, limit_name in (
                ('project', self.project_root, 'plan.json', 'sequence', 'project_limit'),
                ('timing', self.timing_dir, 'ledger.json', 'head_sequence', 'timing_limit')):
            if root is None:
                continue
            limit = int(totals[label][field])
            cut[limit_name] = limit
            files = [root / plan_name]
            if label == 'project':
                files.append(root / 'initial-identity-keys.json')
                if (root / 'migrations').is_dir():
                    files.extend(sorted((root / 'migrations').iterdir()))
            files.extend(root / 'events' / f'{index:06d}.json' for index in range(1, limit + 1))
            observed_bindings = [stops.file_cut(path) for path in files]
            bindings.extend(observed_bindings)
            if label == 'project':
                cut['project_bindings'] = observed_bindings
        return cut, bindings

    @staticmethod
    def _accounting_projection(totals: dict[str, Any]) -> dict[str, Any]:
        state = {}
        if 'project' in totals:
            state['project'] = totals['project']
        if 'timing' in totals:
            state['timing'] = {key: totals['timing'][key]
                for key in ('total_live_request_count', 'head_sequence', 'event_ids')}
        return state

    def _repeat_source(self, value: Mapping[str, Any]) -> dict[str, Any]:
        for row in reversed(self.ledger_observations):
            totals = row['totals']
            if dict(value) == self._accounting_projection(totals):
                return {'observation': row, 'projection': 'accounting'}
            if dict(value) == {'root_causes': totals.get('project', {}).get('root_cause_counts')}:
                return {'observation': row, 'projection': 'root_causes'}
        raise ValueError(f'{self.scenario} 重复入账核对缺少原账本切点，禁止保存摘要承接')

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

        self.inputs['trusted'] = {'relocated': dict(relocated or {})}
        moved = 0
        for path, digest in self.trusted_before.items():
            current = path
            for old, new in (relocated or {}).items():
                if path == old or path.startswith(old.rstrip("/") + "/"):
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
                      expected_reused: Iterable[str] = (), what: str, source: dict[str, Any] | None = None) -> None:
        executed, expected = sorted(executed), sorted(expected)
        reused, expected_reused = sorted(reused), sorted(expected_reused)
        self.inputs['closure'] = dict(executed=executed, expected=expected, reused=reused,
                                     expected_reused=expected_reused, what=what)
        if source is not None:
            self.inputs['closure_source'] = source
            actual, reused_actual = self._closure_original(source)
            self.case.assertEqual(executed, sorted(actual), '执行闭集与原动作／调用日志不一致')
            self.case.assertEqual(reused, sorted(reused_actual), '复用闭集与原动作／调用日志不一致')
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

        repeats = list(repeats)
        self.inputs['accounting'] = dict(precise_delta=precise_delta, estimated_delta=estimated_delta,
            root_causes=root_causes, root_cause_state=root_cause_state, repeats=repeats)
        if stops.record_root() is not None and not self.revalidating:
            self.inputs['accounting_sources'] = {
                'repeats': [(self._repeat_source(first), self._repeat_source(second)) for _label, first, second in repeats],
                'root_cause_state': None if root_cause_state is None else self._repeat_source(root_cause_state)}
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

        reopened_phases = list(reopened_phases)
        self.inputs['elapsed'] = {'reopened_phases': reopened_phases}
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

    def check_elapsed_untouched(self, *ledger_paths: Path,
                                original_after: list[dict[str, Any]] | None = None) -> None:
        """不经计时账本的场景（部署、误用拒绝）：给定的计时与项目账本逐字节不变，即没有入账、也没有重置。"""

        self.case.assertTrue(ledger_paths, f"{self.scenario}：要给出核对的账本")
        before = {path: digest for path, digest in self.history_before.items()
                  if any(Path(path) == root or Path(path).is_relative_to(root) for root in ledger_paths)}
        self.case.assertTrue(before, f"{self.scenario}：历史快照里没有这些账本")
        if self.revalidating:
            if original_after is None or len(original_after) != len(before):
                raise ValueError('账本零写入重验缺原 after 完整文件清单')
            after = {str(binding['path']): hashlib.sha256(stops.read_cut(binding)).hexdigest()
                     for binding in original_after}
        else:
            after = file_digests(*ledger_paths)
            if stops.record_root() is not None:
                original_after = [stops.file_cut(Path(path)) for path in sorted(after)]
        self.inputs['elapsed'] = {'ledger_paths': ledger_paths, 'original_after': original_after}
        self.case.assertEqual(after, before, f"{self.scenario}：账本被写")
        self._record("elapsed", ledgers=len(ledger_paths), ledger_files=len(before), untouched=True)

    # ---- 5 历史事实不覆盖 ----

    def check_history(self, *, archived: Mapping[str, str] | None = None) -> None:
        """故障前的历史事实文件全部仍在且逐字节不变；``archived`` 给出按设计被归档移走的目录（旧前缀 → 新前缀）。"""

        self.inputs['history'] = {'archived': dict(archived or {})}
        moved = 0
        for path, digest in self.history_before.items():
            current = path
            for old, new in (archived or {}).items():
                if path == old or path.startswith(old.rstrip("/") + "/"):
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
        self.inputs['entry'] = {'text': text, 'fragments': fragments}
        if stops.record_root() is not None and not self.revalidating:
            try:
                self.inputs['entry_source'] = {'whole': self.object_source(text)}
            except ValueError:
                if not isinstance(text, dict):
                    raise
                self.inputs['entry_source'] = {'parts': {key: self.object_source(value) for key, value in text.items()}}
        rendered = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        self.case.assertTrue(fragments, "续跑入口要给出期待的命令片段")
        for fragment in fragments:
            self.case.assertIn(fragment, rendered, f"{self.scenario}：输出没有给出续跑入口「{fragment}」")
        self._record("entry", fragments=list(fragments), excerpt=rendered[:300])

    # ---- 汇总 ----

    def report(self, **facts: Any) -> dict[str, Any]:
        if stops.load_state('audit-complete:' + self.scenario) is not None:
            if not os.environ.get(stops.METHOD_RESUME_ENV):
                raise ValueError('已有完整审计只能由原方法恢复入口只读消费')
            record = type(self).revalidate(self.case, self.scenario)
            target = os.environ.get(RECORDS_ENV)
            if target:
                with open(target, 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
            return record
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
        observed = datetime.now(timezone.utc).isoformat()
        after = ledger_totals(timing_dir=self.timing_dir, project_root=self.project_root, now=observed)
        cut, bindings = self._ledger_cut(after, observed)
        stops.save_state('audit-complete:' + self.scenario, {'before': self._before(), 'inputs': self.inputs,
            'record': record, 'cut': cut, 'bindings': bindings, 'after': after})
        stops.save_case(self.case)
        return record

    @classmethod
    def revalidate(cls, case: unittest.TestCase, scenario: str) -> dict[str, Any]:
        """以原账本历史切点和原六个检查重验前缀，不能仅核 passed 摘要。"""

        saved = stops.load_state('audit-complete:' + scenario)
        if saved is None:
            raise ValueError(f'{scenario} 缺少完整审计停点，禁止追认旧摘要')
        audit = cls(case, scenario)
        for key, value in saved['before'].items():
            setattr(audit, key, value)
        audit.replay_cut = saved['cut']
        audit.revalidating = True
        for binding in saved['bindings']:
            stops.read_cut(binding)
        case.assertEqual(audit.totals(), saved['after'], '原账本历史切点已改变')
        inputs = saved['inputs']
        case.assertEqual(set(inputs) - {'accounting_sources', 'closure_source', 'entry_source'}, set(SIX_CHECKS))
        sources = inputs.get('accounting_sources')
        if sources is None:
            raise ValueError(f'{scenario} 缺少原账本重复动作切点，不能追认摘要')

        def original_accounting(source: dict[str, Any]) -> dict[str, Any]:
            row = source['observation']
            for binding in row['bindings']:
                stops.read_cut(binding)
            totals = ledger_totals(timing_dir=audit.timing_dir, project_root=audit.project_root, **row['cut'])
            case.assertEqual(totals, row['totals'], '重复动作的原账本切点已改变')
            if source['projection'] == 'accounting':
                return audit._accounting_projection(totals)
            case.assertEqual(source['projection'], 'root_causes')
            return {'root_causes': totals['project']['root_cause_counts']}

        repeats = inputs['accounting']['repeats']
        case.assertEqual(len(repeats), len(sources['repeats']))
        for (_label, first, second), (first_source, second_source) in zip(repeats, sources['repeats']):
            case.assertEqual(dict(first), original_accounting(first_source))
            case.assertEqual(dict(second), original_accounting(second_source))
        if sources['root_cause_state'] is not None:
            case.assertEqual(inputs['accounting']['root_cause_state'], original_accounting(sources['root_cause_state']))
        if audit.before_ledger is None:
            raise ValueError(f'{scenario} 缺少原 before 的账本字节，禁止追认当前态')
        for binding in audit.before_ledger['bindings']:
            stops.read_cut(binding)
        case.assertEqual(audit.totals_before, ledger_totals(timing_dir=audit.timing_dir,
            project_root=audit.project_root, **audit.before_ledger['cut']), '原 before 账本切点已改变')
        if not inputs.get('closure_source') or not inputs.get('entry_source'):
            raise ValueError(f'{scenario} 缺原调用日志或 stdout/JSON 绑定，禁止承接摘要')
        actual, reused = audit._closure_original(inputs['closure_source'])
        case.assertEqual(sorted(actual), inputs['closure']['executed'])
        case.assertEqual(sorted(reused), inputs['closure']['reused'])
        entry_source = inputs['entry_source']
        entry = (audit.read_source(entry_source['whole']) if 'whole' in entry_source else
                 {key: audit.read_source(value) for key, value in entry_source['parts'].items()})
        case.assertEqual(entry, inputs['entry']['text'])
        audit.check_trusted(**inputs['trusted'])
        audit.check_closure(**inputs['closure'])
        audit.check_accounting(**inputs['accounting'])
        elapsed = inputs['elapsed']
        if 'ledger_paths' in elapsed:
            audit.check_elapsed_untouched(*elapsed['ledger_paths'], original_after=elapsed.get('original_after'))
        else:
            audit.check_elapsed(**elapsed)
        audit.check_history(**inputs['history'])
        audit.check_entry(inputs['entry']['text'], *inputs['entry']['fragments'])
        case.assertEqual(set(audit.checks), set(SIX_CHECKS))
        return saved['record']


# ---------------------------------------------------------------------------
# 外层：隔离副本树、命名空间与子进程（隔离规则与 B5a-05 相同，见模块说明）
# ---------------------------------------------------------------------------


def _copy_tree(destination: Path) -> Path:
    """两个受管根的隔离副本：``official_client_capture`` 按本模块的静态依赖闭包复制，``official_client_control`` 整根复制；
    另带钉住的门禁读取器及其同目录依赖、受监督部署脚本、Claude 文档与 Release 目录。"""

    from tools.official_client_capture import codex_upgrade_gate_receipt as gate

    tree = trees.copy_managed_tree(destination, include_tests="closure", closure_of=Path(__file__))
    shutil.copytree(
        REPO_ROOT / "tools" / "official_client_control",
        tree / "tools" / "official_client_control",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(REPO_ROOT / "tools" / "arm64_supervised_deploy.py", tree / "tools" / "arm64_supervised_deploy.py")
    # 门禁 reader 在隔离解释器中只认固定目录。先由原消费者核验钉住摘要，
    # 再把该源码或已安装驱动的同目录依赖带入副本，不能依赖宿主回退。
    verifier_root = Path(gate._target_gate_verifier().__file__).resolve().parent
    shutil.copytree(verifier_root, tree / "tools" / "ci",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
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


def _expected_scenarios(scenarios: Mapping[str, str | tuple[str, ...]]) -> list[str]:
    """普通组每方法一条，连续组一方法多条；展开后必须保持声明顺序且场景名唯一。"""

    expected = [name for value in scenarios.values() for name in ((value,) if isinstance(value, str) else value)]
    if not expected or len(expected) != len(set(expected)) or any(not isinstance(name, str) or not name for name in expected):
        raise ValueError('场景组的六项记录声明为空、重复或非法')
    return expected


def _records_complete(records: list[dict[str, Any]], expected: list[str]) -> bool:
    """每次实际故障的六项都必须明确通过；缺项、重复、错序或伪造总体 passed 均拒绝。"""

    return (all(isinstance(row, dict) for row in records)
        and [row.get('scenario') for row in records] == expected and all(
        isinstance(row.get('checks'), dict) and set(row['checks']) == set(SIX_CHECKS)
        and all(isinstance(check, dict) and check.get('passed') is True for check in row['checks'].values()) for row in records))


@contextlib.contextmanager
def _record_scope(root: Path, *, resume: bool = False, verify: bool = False):
    """只在原执行者的作用域切换记录坐标，退出后恢复原环境。"""

    keys = (stops.METHOD_RECORD_ENV, stops.METHOD_RESUME_ENV, METHOD_VERIFY_ENV)
    previous = {key: os.environ.get(key) for key in keys}
    os.environ[stops.METHOD_RECORD_ENV] = str(root)
    for key, enabled in ((stops.METHOD_RESUME_ENV, resume), (METHOD_VERIFY_ENV, verify)):
        if enabled:
            os.environ[key] = '1'
        else:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _private_record_root(root: Path) -> Path:
    """记录根只能是当前执行用户的私有目录；失效停点不得换根掩盖。"""

    if root.is_symlink():
        raise ValueError('故障链记录根不能是符号链接')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError('故障链记录根必须是当前用户独占的目录')
    return root.resolve(strict=True)


@contextlib.contextmanager
def _record_lock(root: Path):
    """同一原链只有一个续作者；锁冲突具体拒绝，不再复制世界。"""

    path = root / '.resume.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError('故障链续作锁不是当前用户的普通文件')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('该原链仍有执行者，不能同时续作') from error
        yield
    finally:
        os.close(descriptor)


def _source_inputs() -> dict[str, Any]:
    """冻结实际源码与来源坐标；修复有身份变化时先走原受控演进，不能暗用新源码承接旧结果。"""

    roots = (REPO_ROOT / 'tools' / 'official_client_capture',
             REPO_ROOT / 'tools' / 'official_client_control', REPO_ROOT / 'tools' / 'ci',
             REPO_ROOT / 'backend' / 'internal' / 'officialegress' / 'catalogdata' / 'claude')
    digests: dict[str, str] = {}
    for root in roots:
        for directory, folders, files in os.walk(root, followlinks=False):
            folders[:] = sorted(name for name in folders if name not in {'__pycache__', 'versions', '.git'}
                                 and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                if path.suffix != '.pyc' and path.is_file() and not path.is_symlink():
                    digests[path.relative_to(REPO_ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in (REPO_ROOT / 'tools' / 'arm64_supervised_deploy.py',
                 *(REPO_ROOT / 'docs' / name for name in shell.CLAUDE_DOCUMENTS)):
        if path.is_file() and not path.is_symlink():
            digests[path.relative_to(REPO_ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'repository': str(REPO_ROOT), 'files': digests,
            'fixtures': {key: os.environ.get(key) for key in
                         ('CLAUDE_2_1_226_RELAY_FIXTURE', 'CLAUDE_2_1_226_FREEZE_FIXTURE')}}


def _continuation_methods(chain: str) -> dict[str, list[str]]:
    """承接的全集来自原链登记，调用方不能少填一个未完成方法后冒充整链只读。"""

    if chain == 'late-continuous':
        groups = [{'_TransientSegmentScenarios._scenario_full_control_delivery': LATE_CONTINUOUS_SCENARIOS}]
    elif chain == 'revision-continuous':
        groups = [{'_RevisionEvaluatorScenarios._scenario_revision_evaluator_delivery': REVISION_CONTINUOUS_SCENARIOS}]
    elif chain == 'recovery':
        groups = [group for _step, group in RECOVERY_CHAIN_GROUPS]
    else:
        raise ValueError('本次产品承接未授权其它原链')
    return {method: _expected_scenarios({method: expected}) for group in groups for method, expected in group.items()}


def _verify_continuation_method(document: dict[str, Any], method: str, expected: list[str]) -> None:
    row = document['methods'].get(method)
    if (row is None or row['root'] != str(stops.record_root()) or row['expected'] != expected
            or set(document['methods']) != set(_continuation_methods(document['chain']))):
        raise ValueError('本次承接不是原链完整方法集合或方法坐标变化')
    context = stops.load_state('method-context')
    if context is None or context.get('method') != method or context.get('tree') != document['tree']:
        raise ValueError('原方法上下文缺失或不属于同一世界')
    if document['mode'] in stops.FORMED_READ_ONLY_MODES:
        result = stops.load_state('method-result')
        if result is None or result.get('complete') is not True or result.get('executor') != row['executor_origin']:
            raise ValueError('只读承接的形成期执行器不是原成功方法绑定的原件')
        for binding in row['originals']:
            stops._read_exact_cut(binding)


def prepare_product_continuation(chain: str, mode: str, before_files: dict[str, Any], *,
                                  campaign: Path | None = None, deployment: Path | None = None,
                                  output_path: Path | None = None) -> dict[str, Any]:
    """从真实原件生成本次测试侧凭证；不部署、不登记工具演进、不签认证。

    before_files 是已归档的真实旧控制文件切点；revision 必须先完成原受监督部署及两层登记。
    原链记录根仍由既有 CLAUDE_RECORDED_CHAIN_RECORD_DIR 指定，不增加公共放行环境变量。
    """

    root = Path(os.environ['CLAUDE_RECORDED_CHAIN_RECORD_DIR']).resolve(strict=True)
    if root.is_symlink() or mode not in {*stops.FORMED_READ_ONLY_MODES, 'registered-control-evolution'}:
        raise ValueError('原链记录根或承接模式非法')
    with _record_lock(root), _record_scope(root):
        saved = stops.load_state('chain:' + chain)
        if saved is None or saved.get('diagnostic_seconds') is not None:
            raise ValueError('承接必须绑定原正式链停点')
        source = _source_inputs()
        path = (stops.current_product_continuation_path(root, source) if mode == 'formed-evidence-read-only'
                else root / stops.PRODUCT_CONTINUATION_FILE) if output_path is None else Path(output_path)
        if output_path is not None and path != stops.current_product_continuation_path(root, source):
            raise ValueError('新凭证须使用本次非测试来源绑定的准确新路径，禁止覆盖旧凭证')
        if path.exists():
            binding = stops.file_cut(path)
            document = stops.verify_product_continuation(binding, tree=Path(saved['tree']),
                                                         original_source=saved['source'], current_source=source)
            if (document['chain'] != chain or document['mode'] != mode or document['before_files'] != before_files
                    or document['chain_coordinates'] != {key: saved[key] for key in document['chain_coordinates']}):
                raise ValueError('已发布承接凭证的原始意图或链坐标不同，禁止替换')
            if mode == 'registered-control-evolution' and (
                    campaign is None or deployment is None
                    or document['registration']['campaign'] != str(campaign.resolve())
                    or document['registration']['deployment']['path'] != str(deployment.resolve())):
                raise ValueError('已发布承接凭证的 Campaign 或部署坐标不同')
            for method, expected in _continuation_methods(chain).items():
                with _record_scope(Path(document['methods'][method]['root'])):
                    _verify_continuation_method(document, method, expected)
                    if mode in stops.FORMED_READ_ONLY_MODES:
                        _complete_method_original(expected)
            return binding
        proof_function = (stops.formed_source_continuation_proof if mode == 'formed-evidence-read-only' else stops.control_continuation_proof)
        proof = proof_function(saved['source'], source, before_files)
        tree = Path(saved['tree'])
        methods = {}
        for method, expected in _continuation_methods(chain).items():
            method_root = Path(saved['recorded_worlds']) / 'method-records' / hashlib.sha256(method.encode()).hexdigest()[:24]
            if method_root.is_symlink() or not method_root.is_dir():
                raise ValueError('原方法记录根缺失；不补建成功世界：' + method)
            with _record_lock(method_root), _record_scope(method_root):
                context = stops.load_state('method-context')
                if (context is None or context.get('method') != method or context.get('expected') != expected
                        or context.get('tree') != str(tree)):
                    raise ValueError('原方法身份、场景全集或tree不匹配')
                if mode in stops.FORMED_READ_ONLY_MODES:
                    result = _complete_method_original(expected)
                    origin = result.get('executor')
                    if origin is None:
                        raise ValueError('原成功方法缺形成期执行器绑定：' + method)
                    # 新 invocation 及验证输出可写；原业务停点、命令前缀和成功结果必须原字节保留。
                    excluded = {hashlib.sha256(name.encode()).hexdigest() + '.json'
                                for name in ('method-context', 'method-verification')}
                    originals = [stops.file_cut(path) for path in sorted(method_root.glob('*.json'))
                                 if path.name not in excluded]
                else:
                    origin = None
                    result = stops.load_state('method-result')
                    cursor = stops.load_state('cursor:revision-chain')
                    if chain != 'revision-continuous' or (result and result.get('complete')) or cursor is None or cursor.get('phase') != 9:
                        raise ValueError('写续作只接受本次 revision 第9阶段原未完成停点')
                    if campaign is None or Path(cursor['campaign']).resolve() != campaign.resolve():
                        raise ValueError('写续作必须使用第9阶段原停点的 Campaign')
                    originals = []
                methods[method] = {'root': str(method_root), 'expected': expected, 'originals': originals,
                                   'executor_origin': origin}
        registration = None
        if mode == 'registered-control-evolution':
            if campaign is None or deployment is None:
                raise ValueError('revision 承接缺真实部署与原 Campaign')
            registration = stops.registered_control_facts(tree, campaign, deployment)
        coordinates = {key: saved[key] for key in ('chain', 'source', 'scratch_root', 'tree', 'recorded_worlds',
                                                   'isolation', 'diagnostic_seconds', 'first_started_at_utc')}
        document = {'schema': 'claude-fault-product-continuation/v1', 'mode': mode, 'chain': chain,
                    'tree': str(tree), 'chain_coordinates': coordinates, 'original_source': saved['source'],
                    'current_source': source, 'before_files': before_files, 'control_proof': proof,
                    'product_files': stops._executor_product_files(tree), 'methods': methods,
                    'registration': registration}
        if mode == 'formed-evidence-read-only':
            document['schema'] = 'claude-fault-product-continuation/v2'
            document['source_proof'] = document.pop('control_proof')
        if mode in stops.FORMED_READ_ONLY_MODES and chain == 'revision-continuous':
            document['registration'] = stops._revision_formed_registration(document)
        raw = (json.dumps(stops._encoded(document), sort_keys=True, ensure_ascii=False) + '\n').encode()
        if path.exists():
            if path.is_symlink() or path.read_bytes() != raw:
                raise ValueError('原承接凭证已存在且意图不同；不得覆盖原件')
        else:
            descriptor, temporary = tempfile.mkstemp(prefix='.product-continuation-', dir=root)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                candidate = stops.file_cut(Path(temporary))
                stops.verify_product_continuation(candidate, tree=tree, original_source=saved['source'],
                                                   current_source=source)
                os.link(temporary, path)
                directory = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(temporary).unlink(missing_ok=True)
        binding = stops.file_cut(path)
        stops.verify_product_continuation(binding, tree=tree, original_source=saved['source'], current_source=source)
        return binding


def _read_records(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError('原方法六项记录缺失或不是普通文件')
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _publish_resumed_method_records(case: unittest.TestCase, records: list[dict[str, Any]],
                                   expected: list[str]) -> None:
    """原方法收口时补发已重验审计；只发布当前尚未封存的 invocation，不追改旧结果。"""

    if not _records_complete(records, expected):
        raise ValueError('原方法终态缺完整且有序的六项审计')
    target = os.environ.get(RECORDS_ENV)
    if not target:
        return
    path = Path(target)
    published = _read_records(path)
    names = [row.get('scenario') for row in published if isinstance(row, dict)]
    by_name = {row['scenario']: row for row in records}
    if (not _records_complete(published, names)
            or names != [name for name in expected if name in names]
            or published != [by_name[name] for name in names]):
        raise ValueError('本轮已发审计不是原终态的有序子集，禁止覆盖冲突或重复记录')
    if names == expected:
        return
    if not os.environ.get(stops.METHOD_RESUME_ENV) or os.environ.get(METHOD_VERIFY_ENV):
        raise ValueError('缺项审计只能由原未完成方法的续作入口补发')
    root, context = stops.record_root(), stops.load_state('method-context')
    if (root is None or context is None or not context.get('invocations')
            or context.get('method') != type(case).__name__ + '.' + case._testMethodName
            or context.get('expected') != expected or context.get('tree') != str(REPO_ROOT)):
        raise ValueError('补发审计缺当前原方法与执行树的上下文绑定')
    invocation = context['invocations'][-1]
    output = Path(invocation['output'])
    if (invocation.get('state') != 'started' or invocation.get('verify') is not False
            or output.is_symlink() or output.parent.resolve() != root
            or not output.name.startswith('invocation-')
            or path != output / 'records.jsonl'
            or any(Path(row['output']) == output for row in context['invocations'][:-1])):
        raise ValueError('只能补发当前新 invocation 的未封存审计输出')

    def require_unsealed() -> None:
        if any((output / name).exists() or (output / name).is_symlink()
               for name in ('stdout.log', 'stderr.log', 'exit.json')):
            raise ValueError('当前 invocation 已封存，禁止补改审计输出')

    require_unsealed()
    original = path.read_bytes()
    missing = [name for name in expected if name not in names]
    # 此回调按原 phase=11／done 和原件验真，不以 method-result.complete 授权。
    # 已在本轮 report 的记录不重复重验；全部完成游标零输出时才重验全部八项。
    recovered = case.revalidate_completed_method(missing)
    if not _records_complete(recovered, missing) or recovered != [by_name[name] for name in missing]:
        raise ValueError('承接审计原件与终态记录不一致，禁止补发')
    if stops.load_state('method-context') != context or path.is_symlink() or path.read_bytes() != original:
        raise ValueError('审计重验期间当前 invocation 或输出发生变化，禁止发布')
    require_unsealed()
    descriptor, temporary = tempfile.mkstemp(prefix='.records-', dir=output)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(output, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _method_result_passed(code: int | None, stderr: str, count: int = 1) -> bool:
    lines = stderr.rstrip().splitlines()
    ran = f"Ran {count} test{'s' if count > 1 else ''} in "
    return code == 0 and bool(lines) and lines[-1] == 'OK' and any(line.startswith(ran) for line in lines)


def _complete_method_original(expected: list[str]) -> dict[str, Any]:
    """派发前先核实际成功、原命令和六项原件；缺一项都不能转入动作体。"""

    saved = stops.load_state('method-result')
    if saved is None or saved.get('complete') is not True or saved.get('expected') != expected:
        raise ValueError('原方法没有完整成功停点，不能承接摘要')
    if bool(saved.get('diagnostic_only')) != (diagnostic_budget.diagnostic_seconds() is not None):
        raise ValueError('诊断方法结果不能承接为正式认证')
    for binding in saved['bindings'].values():
        stops._read_exact_cut(binding)
    command_prefix = saved.get('command_prefix')
    if command_prefix is None:
        raise ValueError('原成功方法缺完整命令观察前缀，不能只承接 unittest 输出')
    observations = (stops.load_state('command-observations') or {}).get('commands', [])
    if observations != command_prefix:
        raise ValueError('原成功方法之后的命令观察列表已改变')
    stops.verify_commands(command_prefix)
    original_exit = json.loads(stops.read_cut(saved['bindings']['exit']))
    context = stops.load_state('method-context')
    if (context is None or original_exit.get('timed_out') is not False
            or original_exit.get('returncode') != saved['returncode']
            or original_exit.get('argv') != [sys.executable, '-m', 'unittest', '-v',
                                           CHAIN_MODULE + '.' + context['method']]):
        raise ValueError('原方法的 argv、退出码或超时事实与停点不一致')
    stderr = stops.read_cut(saved['bindings']['stderr']).decode('utf-8')
    if not _method_result_passed(saved['returncode'], stderr):
        raise ValueError('原方法没有真实成功退出事实')
    if not _records_complete(_read_records(saved['bindings']['records']['path']), expected):
        raise ValueError('原方法成功输出缺少逐场景六项记录')
    return saved


def _resume_complete_method(case: unittest.TestCase, expected: list[str]) -> list[dict[str, Any]]:
    """重验真实成功退出及六项原件；连续链由原夹具重绑其活守卫作用域。"""

    saved = _complete_method_original(expected)
    callback = getattr(case, 'revalidate_completed_method', None)
    if callback is not None:
        records = callback(expected)
    else:
        if not stops.restore_case(case):
            raise ValueError('原成功方法缺少真实夹具坐标')
        campaign = getattr(case, 'campaign', None)
        if campaign is not None:
            stops.verify_case(case, Path(campaign))
        records = [ScenarioAudit.revalidate(case, name) for name in expected]
    original = _read_records(saved['bindings']['records']['path'])
    if records != original or not _records_complete(records, expected):
        raise ValueError('原方法六项原件、顺序或成功结果不一致')
    return records


def _revalidate_formed_method_current(binding: dict[str, Any], method: str) -> dict[str, Any]:
    """当前实际源码进程的纯验入口；只恢复原值并调用既有成功回调。

    调用方仍在原隔离 namespace。此函数不运输或改写产品，不执行 unittest
    方法体、setUp 或初始化器，不调用部署／恢复事务；缺只读所需原值即拒绝。
    """
    from tools.official_client_control.qualification_material_import import historical_material_reader_scope
    from tools.official_client_capture.claude_gate_material_worker import gate_material_reader_scope
    pending = stops._decoded(json.loads(stops._read_exact_cut(binding)))
    method_root = Path(pending['methods'][method]['root'])
    current_source = _source_inputs()
    # 绝不启动 setUp/Class 的受管树或世界构造器。原 saved case 恢复完整值，
    # 缺失时不能靠当前产品初始化补齐；专用回调继续核各自原世界/终态游标。
    # 父 _run_group 已持原方法锁；子进程只继承同一记录 scope，不能再次 flock。
    with _record_scope(method_root, resume=True, verify=True), stops.formed_method_read_only(), \
            stops.formed_source_proof_scope(binding, current_source=current_source, current_source_reader=_source_inputs) as document, \
            gate_material_reader_scope(), historical_material_reader_scope(REPO_ROOT) as materials:
        if (Path(document['current_source']['repository']).resolve(strict=True) != REPO_ROOT
                or method not in _continuation_methods(document['chain'])):
            raise ValueError('当前纯验必须从固定 current_source.repository 加载该原方法')
        expected = _continuation_methods(document['chain'])[method]
        class_name, method_name = method.split('.', 1)
        allowed_classes = {'_BatchFaultScenarios', '_OfficialFaultScenarios', '_CandidateFaultScenarios',
            '_DeployFaultScenarios', '_PredecessorMisuseScenarios', '_TransientSegmentScenarios', '_RevisionEvaluatorScenarios'}
        if class_name not in allowed_classes:
            raise ValueError('当前纯验方法不属于既有明确类集合')
        case = globals()[class_name](methodName=method_name)
        _verify_continuation_method(document, method, expected)
        if not stops.restore_case(case):
            raise ValueError('当前纯验缺原完整 case 值，不允许初始化替代')
        if class_name in {'_BatchFaultScenarios', '_OfficialFaultScenarios'} and not getattr(case, 'campaign', None):
            raise ValueError('当前批次/官方纯验缺原Campaign主语，不能只返回六项audit')
        case._read_only_revalidation = True
        records = _resume_complete_method(case, expected)
        _verify_continuation_method(document, method, expected)
        after = _source_inputs()
        if after != current_source or after != document['current_source']:
            raise ValueError('当前纯验期间固定源码或来源坐标漂移')
        validators = {}
        for name, module in tuple(sys.modules.items()):
            if (not (name in {'tools.official_client_capture', 'tools.official_client_control', 'tools.ci',
                             'tools.arm64_supervised_deploy'}
                     or name.startswith(('tools.official_client_capture.', 'tools.official_client_control.', 'tools.ci.')))
                    or '.tests.' in name or name.endswith('.tests') or not getattr(module, '__file__', None)):
                continue
            path = Path(module.__file__).resolve(strict=True)
            if not path.is_relative_to(REPO_ROOT):
                raise ValueError('当前纯验产品模块实际从另一源码根加载：' + name)
            relative = path.relative_to(REPO_ROOT).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if after['files'].get(relative) != digest:
                raise ValueError('当前纯验产品模块不是准确冻结源码：' + name)
            validators[relative] = digest
        return {'schema': 'claude-fault-current-formed-verification/v1', 'method': method,
            'purpose': 'current-formed-read-only', 'current_source': after,
            'validator_modules': validators, 'records': records, 'material_consumers': materials}


def _resumable_method(method: Callable[..., Any]) -> Callable[..., Any]:
    """保留原 unittest 方法和 ID；成功停点只走原件重验，不再运行故障注入。"""

    @functools.wraps(method)
    def execute(case: unittest.TestCase, *args: Any, **options: Any) -> Any:
        # 以当前模块实际 __file__ 推出的根核验，不能只核 env 中的 tree 却从宿主导入执行器。
        stops.executor_invocation_binding(REPO_ROOT)
        continuation = stops.invocation_product_continuation(REPO_ROOT)
        formed = continuation is not None and continuation[1]['mode'] in stops.FORMED_READ_ONLY_MODES
        if formed and not os.environ.get(METHOD_VERIFY_ENV):
            raise ValueError('已形成结果承接必须走只读方法入口，禁止退回动作体')
        if not os.environ.get(METHOD_VERIFY_ENV):
            return method(case, *args, **options)
        context = stops.load_state('method-context')
        if context is None or context.get('method') != type(case).__name__ + '.' + method.__name__:
            raise ValueError('方法重验入口与原方法身份不一致')
        if formed:
            method_name = type(case).__name__ + '.' + method.__name__
            _verify_continuation_method(continuation[1], method_name, context['expected'])
        with stops.formed_method_read_only() if formed else contextlib.nullcontext():
            records = _resume_complete_method(case, context['expected'])
        if formed:
            stops.verify_product_continuation(continuation[0], tree=REPO_ROOT)
        target = os.environ.get(RECORDS_ENV)
        if not target:
            raise ValueError('原方法重验缺六项输出坐标')
        with open(target, 'a', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        return None
    return execute


def _run_group(case: unittest.TestCase, recorder: _ChainRecorder, scenarios: Mapping[str, str | tuple[str, ...]], step: str) -> None:
    """在副本树里起一个子进程按顺序运行一组场景方法（``类._scenario_名`` → 场景名），收齐每个场景写出的六项记录。

    子进程环境与链体相同（同一命名空间、``PYTHONPATH`` 只含副本根、同一 ``TMPDIR`` 与仅夹具总账开关），单独成一个进程组：
    超时整组终止。全部通过（正好运行这几项、没有跳过）且每个场景都写出六项齐全的记录才记 ``step``。"""

    diagnostic_seconds = diagnostic_budget.diagnostic_seconds()
    if len(scenarios) > 1:
        errors = []
        for method, names in scenarios.items():
            try:
                _run_group(case, recorder, {method: names}, step + " / " + method)
            except (AssertionError, ValueError) as error:
                errors.append(str(error))
        if errors:
            case.fail("独立场景已收齐失败：\n" + "\n".join(errors))
        return
    method_root = None
    continuation = stops.invocation_product_continuation(REPO_ROOT)
    formed = continuation is not None and continuation[1]['mode'] in stops.FORMED_READ_ONLY_MODES
    if os.environ.get(METHODS_ROOT_ENV):
        method = next(iter(scenarios))
        method_root = Path(os.environ[METHODS_ROOT_ENV]) / hashlib.sha256(method.encode()).hexdigest()[:24]
        if formed and (method_root.is_symlink() or not method_root.is_dir()):
            raise ValueError('已成功方法原记录根缺失，禁止补建后重跑')
        method_root = _private_record_root(method_root)
    if formed and method_root is None:
        raise ValueError('只读承接缺少原方法根，禁止普通动作入口')
    if method_root is not None:
        with _record_lock(method_root), _record_scope(method_root):
            context = stops.load_state('method-context')
            expected = _expected_scenarios(scenarios)
            coordinates = {'method': next(iter(scenarios)), 'expected': expected, 'tree': str(shell._tree_root())}
            if formed:
                _verify_continuation_method(continuation[1], next(iter(scenarios)), expected)
                _complete_method_original(expected)
            if context is None:
                context = {**coordinates, 'seconds': 0.0, 'invocations': []}
                stops.save_state('method-context', context)
            elif any(context.get(key) != value for key, value in coordinates.items()):
                raise ValueError('原方法、场景顺序或隔离源码根已改变，不能承接停点')
            previous = stops.load_state('method-result')
            verify = bool(previous and previous.get('complete'))
            if formed and not verify:
                raise ValueError('只读承接的原方法成功记录缺失，禁止执行动作体')
            resume = bool(context['invocations'])
            with _record_scope(method_root, resume=resume, verify=verify):
                _run_group_body(case, recorder, scenarios, step, method_root=method_root, context=context,
                                verify=verify)
        return
    _run_group_body(case, recorder, scenarios, step)


def _run_group_body(case: unittest.TestCase, recorder: _ChainRecorder,
                    scenarios: Mapping[str, str | tuple[str, ...]], step: str,
                    *, method_root: Path | None = None, context: dict[str, Any] | None = None,
                    verify: bool = False) -> None:
    """原方法实际 invocation；完整原件逐次保存，承接也实际运行原方法的重验分支。"""

    diagnostic_seconds = diagnostic_budget.diagnostic_seconds()
    expected = _expected_scenarios(scenarios)
    method_timeout = (diagnostic_seconds if diagnostic_seconds is not None else
                      certification.claude_fault_time_budget(CHAINS[recorder.chain]["scenario"])["method_timeout_seconds"])
    if method_root is None:
        descriptor, filename = tempfile.mkstemp(prefix="fault-chain-records-", suffix=".jsonl")
        os.close(descriptor)
        records_file = Path(filename)
        output = None
    else:
        output = Path(tempfile.mkdtemp(prefix='invocation-', dir=method_root))
        records_file = output / 'records.jsonl'
        records_file.touch(mode=0o600)
    environment = {**os.environ, RECORDS_ENV: str(records_file)}
    # 这是当前真实重验的执行器绑定，旧 method-result 仍按形成期原件读取，不能改签原输出。
    executor_binding = stops.executor_invocation_binding(shell._tree_root())
    if executor_binding is not None:
        # 普通运行生成的时间戳字节码可能误中同秒同长度修复；不同执行器只能用各自的树外缓存。
        environment['PYTHONPYCACHEPREFIX'] = str(trees.copy_pycache_prefix(shell._tree_root()) / ('executor-' + executor_binding['sha256']))
    started = time.monotonic()
    argv = [sys.executable, "-m", "unittest", "-v", *(f"{CHAIN_MODULE}.{test}" for test in scenarios)]
    if context is not None:
        context['invocations'].append({'argv': argv, 'output': output, 'state': 'started', 'verify': verify,
                                       'executor': executor_binding})
        stops.save_state('method-context', context)
    pair = stops.invocation_product_continuation(shell._tree_root())
    if pair is not None and pair[1]['mode'] == 'formed-evidence-read-only':
        if not verify or method_root is None or output is None or len(scenarios) != 1:
            raise ValueError('当前历史纯验缺原完整方法、只读派发或旁侧输出坐标')
        current_root = Path(pair[1]['current_source']['repository']).resolve(strict=True)
        current_environment = dict(environment)
        current_environment.pop(stops.EXECUTOR_SOURCE_ENV, None)
        current_environment.pop('PYTHONPATH', None)
        # -B 仍可能读取当前根中同秒同长度的旧 pyc；每次 invocation 使用从未存在的树外前缀。
        current_pycache = output / 'current-pycache'
        if os.path.lexists(current_pycache):
            raise ValueError('当前纯验字节码前缀已经存在；禁止消费旧 pyc')
        # 产品导入之前设置前缀并禁止写入；父子进程首尾都核此目录没有生成。
        current_code = ('import json,os,sys\n'
            'if os.path.lexists(sys.argv[4]): raise ValueError("当前纯验字节码前缀已经存在")\n'
            'sys.pycache_prefix=sys.argv[4]\n'
            'sys.dont_write_bytecode=True\n'
            'sys.path.insert(0,sys.argv[1])\n'
            'from tools.official_client_capture.tests.real_chains.test_claude_upgrade_fault_chains '
            'import _revalidate_formed_method_current,stops\n'
            'current_report=_revalidate_formed_method_current('
            'stops._decoded(json.loads(sys.argv[2])),sys.argv[3])\n'
            'if os.path.lexists(sys.argv[4]): raise ValueError("当前纯验生成了字节码前缀")\n'
            'print(json.dumps(stops._encoded(current_report),ensure_ascii=False,sort_keys=True))')
        current_argv = [sys.executable, '-I', '-B', '-c', current_code, str(current_root),
            json.dumps(stops._encoded(pair[0]), ensure_ascii=False), next(iter(scenarios)), str(current_pycache)]
        context['invocations'][-1].update(argv=current_argv, purpose='current-formed-read-only',
                                        pycache_prefix=str(current_pycache))
        stops.save_state('method-context', context)
        current_process = subprocess.Popen(current_argv, cwd=current_root, env=current_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        current_timed_out = False
        try:
            current_stdout, current_stderr = current_process.communicate(timeout=method_timeout)
        except subprocess.TimeoutExpired:
            current_timed_out = True
            try:
                os.killpg(current_process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            current_stdout, current_stderr = current_process.communicate()
        (output / 'current-stdout.log').write_text(current_stdout, encoding='utf-8')
        (output / 'current-stderr.log').write_text(current_stderr, encoding='utf-8')
        (output / 'current-exit.json').write_text(json.dumps({'argv': current_argv,
            'returncode': current_process.returncode, 'timed_out': current_timed_out,
            'purpose': 'current-formed-read-only', 'pycache_prefix': str(current_pycache),
            'pycache_generated': os.path.lexists(current_pycache),
            'seconds': round(time.monotonic() - started, 3)},
            ensure_ascii=False), encoding='utf-8')
        try:
            if os.path.lexists(current_pycache):
                raise ValueError('当前纯验字节码前缀生成；拒绝本次结果')
            if current_timed_out or current_process.returncode != 0:
                raise ValueError('当前历史成功纯验超时或拒绝；不继续原方法：' + current_stderr[-6000:])
            stops.verify_executor_source(executor_binding, shell._tree_root(), verify_product=False)
            current_verification = stops._decoded(json.loads(current_stdout))
            original = _read_records(stops.load_state('method-result')['bindings']['records']['path'])
            if (not isinstance(current_verification, dict)
                    or current_verification.get('schema') != 'claude-fault-current-formed-verification/v1'
                    or current_verification.get('purpose') != 'current-formed-read-only'
                    or current_verification.get('method') != next(iter(scenarios))
                    or current_verification.get('current_source') != pair[1]['current_source']
                    or current_verification.get('records') != original
                    or not _records_complete(current_verification['records'], expected)):
                raise ValueError('当前真实消费者结果不是原方法、准确当前来源和原六项结果')
            if time.monotonic() - started > method_timeout:
                raise ValueError('当前纯验已耗尽原方法预算，不扩预算接纳结果')
        except (ValueError, OSError, KeyError, TypeError) as error:
            # 新失败只封存旁侧 invocation；原成功 method-result 的字节与绑定不动。
            seconds = round(time.monotonic() - started, 3)
            failure_bindings = {name: stops.file_cut(output / filename) for name, filename in
                (('current_stdout', 'current-stdout.log'), ('current_stderr', 'current-stderr.log'),
                 ('current_exit', 'current-exit.json'), ('records', 'records.jsonl'))}
            context['seconds'] += seconds
            context['invocations'][-1].update(state='finished', seconds=seconds,
                current_argv=current_argv, bindings=failure_bindings, current_error=str(error))
            stops.save_state('method-context', context)
            stops.save_state('method-verification', {'complete': False, 'expected': expected,
                'returncode': current_process.returncode, 'bindings': failure_bindings,
                'diagnostic_only': diagnostic_seconds is not None, 'current_error': str(error)})
            recorder.emit()
            case.fail(str(error))
        # v2 本次唯一消费者就是上述真实当前进程。原成功 stdout/exit/command_prefix
        # 已由 _complete_method_original 实读保留；不再起旧 unittest，不伪造 Ran/OK。
        records = current_verification['records']
        with records_file.open('w', encoding='utf-8') as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
        seconds = round(time.monotonic() - started, 3)
        bindings = {name: stops.file_cut(output / filename) for name, filename in
            (('current_stdout', 'current-stdout.log'), ('current_stderr', 'current-stderr.log'),
             ('current_exit', 'current-exit.json'), ('records', 'records.jsonl'))}
        context['seconds'] += seconds
        context['invocations'][-1].update(state='finished', seconds=seconds, bindings=bindings)
        stops.save_state('method-context', context)
        stops.save_state('method-verification', {'complete': True, 'expected': expected,
            'returncode': current_process.returncode, 'bindings': bindings,
            'diagnostic_only': diagnostic_seconds is not None, 'executor': executor_binding,
            'purpose': 'current-formed-read-only', 'current_source': current_verification['current_source'],
            'command_prefix': (stops.load_state('command-observations') or {}).get('commands', [])})
        recorder.facts['scenarios'].extend(records)
        recorder.facts.setdefault('groups', []).append({'step': step, 'tests': list(scenarios),
            'seconds': seconds, 'returncode': current_process.returncode, 'timeout_seconds': method_timeout,
            'inherited': True, 'cumulative_seconds': context['seconds'],
            'current_executor_verified': executor_binding is not None,
            'purpose': 'current-formed-read-only', 'diagnostic_only': diagnostic_seconds is not None})
        recorder.step(step)
        return
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(shell._tree_root()), env=environment,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=method_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    seconds = round(time.monotonic() - started, 3)
    executor_error = None
    if executor_binding is not None:
        try:
            stops.verify_executor_source(executor_binding, shell._tree_root(), verify_product=False)
        except (ValueError, OSError) as error:
            # 真实子进程已经退出，执行器核验失败也先保留 stdout/stderr 与实际退出原件。
            executor_error = str(error)
    if diagnostic_seconds is not None:
        log_root = Path(os.environ["CLAUDE_RECORDED_CHAIN_RECORD_DIR"]) / "diagnostic-groups"
        log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_id = hashlib.sha256(step.encode()).hexdigest()[:16]
        invocation_logs = Path(tempfile.mkdtemp(prefix=log_id + '-', dir=log_root))
        for suffix, value in (("stdout.log", stdout), ("stderr.log", stderr), ("records.jsonl", records_file.read_text())):
            with (invocation_logs / suffix).open("x", encoding="utf-8") as stream:
                stream.write(value)
    bindings = None
    if output is not None:
        for name, text in (('stdout', stdout), ('stderr', stderr)):
            (output / (name + '.log')).write_text(text, encoding='utf-8')
        (output / 'exit.json').write_text(json.dumps({'argv': argv, 'returncode': process.returncode,
            'timed_out': timed_out, 'seconds': seconds, 'verify': verify,
            'executor': stops._encoded(executor_binding), 'executor_error': executor_error}, ensure_ascii=False), encoding='utf-8')
        bindings = {name: stops.file_cut(output / filename) for name, filename in
                    (('stdout', 'stdout.log'), ('stderr', 'stderr.log'), ('records', 'records.jsonl'), ('exit', 'exit.json'))}
        context['seconds'] += seconds
        context['invocations'][-1].update(state='finished', seconds=seconds, bindings=bindings)
        stops.save_state('method-context', context)
    records = _read_records(records_file)
    if output is None:
        records_file.unlink()
    recorder.facts["scenarios"].extend(records)
    recorder.facts.setdefault("groups", []).append(
        {"step": step, "tests": list(scenarios), "seconds": seconds, "returncode": None if timed_out else process.returncode, "timeout_seconds": method_timeout,
         "inherited": verify, "cumulative_seconds": context['seconds'] if context is not None else seconds,
         "current_executor_verified": executor_binding is not None,
         "diagnostic_only": diagnostic_seconds is not None}
    )
    passed = not timed_out and executor_error is None and _method_result_passed(process.returncode, stderr, len(scenarios))
    reported = [record["scenario"] for record in records]
    complete = _records_complete(records, expected)
    if bindings is not None:
        result = {'complete': passed and complete, 'expected': expected, 'returncode': process.returncode,
                  'bindings': bindings, 'diagnostic_only': diagnostic_seconds is not None,
                  'executor': executor_binding,
                  'command_prefix': (stops.load_state('command-observations') or {}).get('commands', [])}
        # 后续重验不能覆盖最初成功的真实退出与输出原件。
        stops.save_state('method-verification' if verify else 'method-result', result)
    if not passed or not complete:
        recorder.emit()
        reason = (f"超时（{seconds} 秒，本方法独立预算 {method_timeout} 秒）" if timed_out
                  else f"执行器字节核验失败：{executor_error}" if executor_error is not None
                  else f"退出码 {process.returncode}" if not passed else f"六项记录不全：报了 {reported}")
        case.fail(f"场景组「{step}」没有通过（{reason}）：{', '.join(scenarios)}\n{stdout[-2000:]}\n{stderr[-6000:]}")
    recorder.step(step)


# ---------------------------------------------------------------------------
# 场景组一：真实副本树批次（父监督器子进程真实执行）
# ---------------------------------------------------------------------------


class _BatchFaultScenarios(staging_tests._StagingCase):
    """原六个批次故障场景的明确停点；原序号、故障注入、真实动作和六项检查保持。"""

    COMMAND_KEYS = {
        'batch-parent': ('driver-interrupted', 'reconcile-first', 'reconcile-repeat', 'redispatch', 'committed-refusal'),
        'batch-publish': ('publish-failure', 'redispatch'),
        'batch-wal': ('draft', 'revise', 'read-revise', 'approve-failure', 'review-refusal', 'reconcile-first', 'reconcile-repeat', 'redispatch'),
        'batch-root-cause': ('failure-1', 'failure-2', 'limit-refusal', 'record-repair', 'redispatch'),
        'batch-budget': ('deadline-refusal', 'deadline-preview', 'deadline-apply', 'dispatch'),
        'batch-seal': ('seal-failure', 'review-refusal', 'reconcile-first', 'reconcile-repeat', 'redispatch'),
    }

    def _batch_start(self, name: str, scenario: str, builder: Callable[[], Path]) -> dict[str, Any]:
        """原建场只运行一次；旧阶段必须逐项重读原命令输出及产品读侧，摘要不能跳动作。"""
        state = stops.resume_checkpoint(self, name)
        if state is not None:
            if state.get('phase') == 'initializing':
                raise ValueError('原批次首次建场未闭合；保留原坐标，禁止重新构造 Campaign')
            rows = state['completed_commands']
            if tuple(row['key'] for row in rows) != self.COMMAND_KEYS[name][:len(rows)]:
                raise ValueError('原批次逻辑命令清单不是冻结顺序的前缀')
            for row in rows:
                original = stops.command_step(self, name, row['key'], row['argv'],
                    lambda: self.fail('已完成原批次命令不得再次派发'), campaign=state['campaign'])
                self.assertEqual(original, row['outcome'], '原停点局部值与完整命令原件不一致')
            return state
        if os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('原批次方法缺原游标，禁止重新初始化')
        stops.checkpoint(self, name, phase='initializing', audit_snapshots={scenario: 'uninitialized'})
        self.campaign = builder()
        state = {'phase': 0, 'campaign': self.campaign, 'completed_commands': [],
                 'audit_snapshots': {scenario: 'uninitialized'}}
        stops.checkpoint(self, name, **state)
        return state

    def _batch_save(self, name: str, state: dict[str, Any], phase: int, **values: Any) -> None:
        state.update(values, phase=phase)
        stops.checkpoint(self, name, **state)

    def _batch_command(self, name: str, state: dict[str, Any], key: str, argv: list[Any],
                       invoke: Callable[[], tuple[int, Any, str]] | None = None) -> tuple[int, Any, str]:
        if self.COMMAND_KEYS[name][len(state['completed_commands'])] != key:
            raise ValueError('原批次命令不在下一逻辑停点')
        result = stops.command_step(self, name, key, argv, invoke or (lambda: batch_tests.run_main(argv)),
                                    campaign=state['campaign'])
        state['completed_commands'].append({'key': key, 'argv': argv, 'outcome': result})
        return result

    def _batch_dispatch(self, name: str, state: dict[str, Any], key: str, phase: str, sequence: int,
                        actions: list[dict[str, Any]]) -> tuple[int, Any, str]:
        campaign = state['campaign']
        plan_path = self.root / 'action-plans' / f"{phase.lower()}-{actions[0]['action_id']}.json"
        argv = self.cli_argv(phase, sequence, plan_path)
        return self._batch_command(name, state, key, argv, lambda: self.dispatch(campaign, phase, sequence, actions))

    def _batch_driver(self, name: str, state: dict[str, Any], actions: list[dict[str, Any]]) -> None:
        driver_tmp = self.root / 'driver-tmp'
        config = {'tmpdir': str(driver_tmp), 'argv': self.cli_argv('VC-2', 2,
            self.root / 'action-plans' / f"vc-2-{actions[0]['action_id']}.json"),
            'patches': [{'module': 'supervisor', 'name': '_commit_prepared_run', 'mode': 'exit_before'}]}
        argv = [sys.executable, '-c', staging_tests.DRIVER, str(staging_tests.REPOSITORY_ROOT), json.dumps(config)]
        def invoke() -> tuple[int, Any, str]:
            driver_tmp.mkdir(mode=0o700, exist_ok=True)
            self.plan_file('VC-2', actions)
            pending = stops.begin_command(argv)
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
            stops.record_command(argv, completed.returncode, completed.stdout, completed.stderr, pending=pending)
            try:
                output = json.loads(completed.stdout) if completed.stdout.strip() else None
            except ValueError:
                output = None
            return completed.returncode, output, completed.stderr
        code, _result, stderr = self._batch_command(name, state, 'driver-interrupted', argv, invoke)
        self.assertEqual(code, 9, stderr[-2000:])

    def _audit(self, scenario: str, campaign: Path, *, trusted_extra: Iterable[Path] = ()) -> ScenarioAudit:
        audit = ScenarioAudit(self, scenario)
        vc = campaign / 'control/vc'
        audit.snapshot(trusted=[campaign / 'campaign.json', vc / 'campaign-plan.json', vc / 'commits', vc / 'batches',
            *sorted(vc.glob('vc-*-checkpoint.json')), *trusted_extra], history=[self.store_root, self.ledger_dir / 'events',
            self.ledger_dir / 'ledger.json', self.project_root / 'events', self.project_root / 'plan.json',
            campaign / 'control/reconciliation', vc / 'commits', vc / 'batches', vc / 'run-manifests'],
            timing_dir=self.ledger_dir, project_root=self.project_root)
        return audit

    def _committed(self, campaign: Path, sequence: int, phase: str = 'VC-2') -> dict[str, Any]:
        path = campaign / 'control/vc/batches' / f'{sequence:04d}-{phase.lower()}.json'
        result = json.loads(path.read_bytes())
        stops.record_json_read(path)
        return result

    def _batch_closure(self, audit: ScenarioAudit, campaign: Path, sequence: int, phase: str = 'VC-2') -> dict[str, Any] | None:
        return audit.file_closure_source(campaign / 'control/vc/batches' / f'{sequence:04d}-{phase.lower()}.json',
                                         ('execute_item_ids',), ('reuse_item_ids',))

    @_resumable_method
    def _scenario_parent_interrupted(self) -> None:
        """原父 run 中断、对账与同序号 attempt-2；停点后不重新注入原故障。"""
        name, scenario = 'batch-parent', '父进程中断'
        state = self._batch_start(name, scenario, self.build_batch_campaign)
        campaign = state['campaign']
        audit = self._audit(scenario, campaign)
        actions = self.draft_actions(campaign)
        if state['phase'] == 0:
            self._batch_driver(name, state, actions)
            self._batch_save(name, state, 1)
        if state['phase'] == 1:
            (run_dir,) = self.staging_runs()
            self.wait_terminal(run_dir, {'aborted_prepared'})
            self.assertEqual(staging_tests._read(run_dir / 'stop-receipt.json')['reason'], 'prepared-abandoned')
            self._batch_save(name, state, 2, run_dir=run_dir, stop_binding=stops.file_cut(run_dir / 'stop-receipt.json'))
        run_dir = state['run_dir']
        stops.read_cut(state['stop_binding'])
        argv = ['reconcile-supervisor-run', '--campaign-dir', campaign, '--run-dir', run_dir]
        if state['phase'] == 2:
            code, reconciled, stderr = self._batch_command(name, state, 'reconcile-first', argv)
            self.assertIsNotNone(reconciled, stderr)
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._batch_save(name, state, 3, reconciled=reconciled, first=audit.accounting_state())
        if state['phase'] == 3:
            code, _again, stderr = self._batch_command(name, state, 'reconcile-repeat', argv)
            self.assertEqual(code, 0, stderr)
            self._batch_save(name, state, 4, second=audit.accounting_state())
        if state['phase'] == 4:
            result = self.assert_batch_passed(self._batch_dispatch(name, state, 'redispatch', 'VC-2', 2, actions), 2, 'VC-2')
            self.assertEqual(result['staging_attempt'], 2)
            (handled,) = result['orphans']
            self.assertEqual((handled['kind'], handled['already_reconciled']), ('parent-run', True))
            self._batch_save(name, state, 5, result=result, totals_after=audit.accounting_state())
        if state['phase'] == 5:
            code, _again, stderr = self._batch_dispatch(name, state, 'committed-refusal', 'VC-2', 2, actions)
            self.assertEqual(code, 1, stderr)
            self._batch_save(name, state, 6, after_refusal=audit.accounting_state())
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch['execute_item_ids'], expected=['vc2-classify-draft'], reused=batch['reuse_item_ids'],
            expected_reused=[], what='同序号重派批次', source=self._batch_closure(audit, campaign, 2))
        audit.check_accounting(root_causes=1, repeats=[('重复对账父 run', state['first'], state['second']),
            ('重复派发已提交序号', state['totals_after'], state['after_refusal'])])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['reconciled']['next_command'], 'compile-and-run-vc-batch', '同序号')
        audit.report(parent_run=run_dir.name, staging_attempt=state['result']['staging_attempt'])

    @_resumable_method
    def _scenario_commit_publish_crash(self) -> None:
        """原 COMMIT 发布步故障与同序号恢复，原失败 stderr 和归档均保留。"""
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture.capturelib.model import ConfigurationError
        name, scenario = 'batch-publish', '提交中崩溃（批次派发事务层）'
        state = self._batch_start(name, scenario, self.build_batch_campaign)
        campaign = state['campaign']
        audit = self._audit(scenario, campaign)
        actions = self.draft_actions(campaign)
        if state['phase'] == 0:
            original, calls = claude_upgrade._publish_staging_file, {'count': 0}
            def failing(source: Path, target: Path, campaign_dir: Path) -> None:
                calls['count'] += 1
                if calls['count'] == 2:
                    raise ConfigurationError('故障恢复链注入：COMMIT 发布步失败')
                original(source, target, campaign_dir)
            with mock.patch.object(claude_upgrade, '_publish_staging_file', side_effect=failing):
                code, result, stderr = self._batch_dispatch(name, state, 'publish-failure', 'VC-2', 2, actions)
            self.assertEqual((code, result), (1, None))
            (run_dir,) = self.staging_runs()
            receipt_path = campaign / 'control/reconciliation' / f'run-{run_dir.name}' / 'supervisor-run-reconciliation.json'
            receipt = staging_tests._read(receipt_path)
            self.assertEqual((receipt['failure_class'], receipt['live_request_count']), ('parent-prepare-abandoned', 0))
            self.assertFalse(self.commit_path(2).exists())
            archive = campaign / 'control/vc/staging-aborts/0002-vc-2/attempt-1'
            self.assertTrue((archive / 'batch.json').is_file())
            self._batch_save(name, state, 1, run_dir=run_dir, archive=archive, failure_stderr=stderr,
                reconciliation_binding=stops.file_cut(receipt_path), after_failure=audit.totals())
        stops.read_cut(state['reconciliation_binding'])
        if state['phase'] == 1:
            result = self.assert_batch_passed(self._batch_dispatch(name, state, 'redispatch', 'VC-2', 2, actions), 2, 'VC-2')
            self.assertEqual((result['staging_attempt'], result['timing_ledger']['events'], result['orphans']), (2, [], []))
            self._batch_save(name, state, 2, result=result)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch['execute_item_ids'], expected=['vc2-classify-draft'], reused=batch['reuse_item_ids'],
            expected_reused=[], what='同序号重派批次', source=self._batch_closure(audit, campaign, 2))
        audit.check_accounting(root_causes=1, repeats=[('同序号重派',
            {'root_causes': state['after_failure']['project']['root_cause_counts']},
            {'root_causes': audit.totals()['project']['root_cause_counts']})])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['failure_stderr'], 'commit-publish 步失败', '同序号重新派发')
        audit.report(parent_run=state['run_dir'].name, archived=str(state['archive'].relative_to(campaign)))

    @_resumable_method
    def _scenario_action_wal_crash(self) -> None:
        """原分类 Store/WAL 写入后硬杀、阶段审核与 N+1 逐字重派。"""
        import collections
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_classify as classify
        from tools.official_client_capture import claude_upgrade_vc
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        if not site.ENABLE_USER_SITE:
            self.fail('动作子进程的崩溃注入经 usercustomize 进入，要求启用用户 site（不跳过：跳过即未认证）')
        name, scenario = 'batch-wal', '提交中崩溃（动作结果 WAL 层）'
        state = self._batch_start(name, scenario, self.build_batch_campaign)
        campaign = state['campaign']
        def action(action_id: str, argv: list[Any]) -> dict[str, Any]:
            return batch_tests.batch.managed_action(action_id, f'VC-2:{action_id}', argv, item_ids=[action_id])
        if state['phase'] == 0:
            self.assert_batch_passed(self._batch_dispatch(name, state, 'draft', 'VC-2', 2,
                [action('vc2-classify-draft', ['classify', '--campaign-dir', campaign])]), 2, 'VC-2')
            self._batch_save(name, state, 1)
        if state['phase'] == 1:
            self._batch_save(name, state, 2)
            directory = self.write_documents(self.resolved_documents(campaign / classify.DRAFT_ROOT / 'rev-0001'), self.root / 'vc2-resolved')
            self._batch_save(name, state, 3, directory=directory,
                revision_bindings=[stops.file_cut(path) for path in sorted(directory.rglob('*')) if path.is_file()])
        if state['phase'] == 2:
            raise ValueError('原修订输入首次物化未闭合；禁止覆盖原输入')
        for binding in state['revision_bindings']:
            stops.read_cut(binding)
        directory = state['directory']
        revise = ['classify', '--campaign-dir', campaign, '--from-revision', 'rev-0001', '--revision-input', directory]
        if state['phase'] == 3:
            self.assert_batch_passed(self._batch_dispatch(name, state, 'revise', 'VC-2', 3,
                [action('vc2-classify-revise', revise)]), 3, 'VC-2')
            self._batch_save(name, state, 4)
        if state['phase'] == 4:
            code, revised, stderr = self._batch_command(name, state, 'read-revise', revise)
            self.assertEqual((code, revised['revision']), (2, 'rev-0002'), stderr)
            self._batch_save(name, state, 5, revised=revised)
        audit = self._audit(scenario, campaign, trusted_extra=[campaign / classify.DRAFT_ROOT])
        approve = [action('vc2-classify-approve', self.approve_argv(campaign, 'rev-0002', state['revised']['review_sha256']))]
        if state['phase'] == 5:
            self._batch_save(name, state, 6)
            inject = self.root / 'inject'
            inject.mkdir(mode=0o700)
            injection = inject / 'usercustomize.py'
            injection.write_text(batch_tests.USERCUSTOMIZE, encoding='utf-8')
            marker = self.root / 'crashed.marker'
            crash = {'PYTHONPATH': os.pathsep.join([str(inject), os.environ.get('PYTHONPATH', '')]).rstrip(os.pathsep),
                batch_tests.CRASH_ACTION_ENV: 'vc2-classify-approve', batch_tests.CRASH_MARKER_ENV: str(marker),
                batch_tests.CRASH_SUFFIX_ENV: '/classification/approved'}
            self._batch_save(name, state, 7, injection_binding=stops.file_cut(injection), crash=crash, marker=marker)
        if state['phase'] == 6:
            raise ValueError('原 WAL 崩溃注入输入未闭合；禁止覆盖或重新生成')
        stops.read_cut(state['injection_binding'])
        if state['phase'] == 7:
            with mock.patch.dict(os.environ, state['crash']):
                code, failed, stderr = self._batch_dispatch(name, state, 'approve-failure', 'VC-2', 4, approve)
            self.assertTrue(state['marker'].is_file(), '崩溃注入没有生效')
            self.assertEqual((code, failed['campaign_run']['reason']), (1, 'action-failed:vc2-classify-approve'), stderr)
            failed_run = Path(failed['campaign_run']['run_dir'])
            facts = [fact for fact in self.store_facts() if fact['fact_kind'] == 'rule_classification_recorded']
            self.assertEqual(len(facts), 1)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)['status'], 'stage_review_required')
            self._batch_save(name, state, 8, failed_run=failed_run, marker_binding=stops.file_cut(state['marker']))
        stops.read_cut(state['marker_binding'])
        if state['phase'] == 8:
            code, _result, stderr = self._batch_dispatch(name, state, 'review-refusal', 'VC-2', 5, approve)
            self.assertEqual(code, 1)
            self.assertIn('stage_review_required', stderr)
            self.assertFalse(self.staging_dir(5).exists())
            self._batch_save(name, state, 9)
        argv = ['reconcile-supervisor-run', '--campaign-dir', campaign, '--run-dir', state['failed_run']]
        if state['phase'] == 9:
            code, reconciled, stderr = self._batch_command(name, state, 'reconcile-first', argv)
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._batch_save(name, state, 10, reconciled=reconciled, first=audit.accounting_state())
        if state['phase'] == 10:
            code, again, stderr = self._batch_command(name, state, 'reconcile-repeat', argv)
            self.assertEqual((code, again['stage_replay']), (0, state['reconciled']['stage_replay']), stderr)
            self._batch_save(name, state, 11, second=audit.accounting_state())
        proof = state['reconciled']['stage_replay']
        self.assertEqual((proof['allowed'], proof['next_action']), (True, 'redispatch-same-batch'))
        if state['phase'] == 11:
            result = self.assert_batch_passed(self._batch_dispatch(name, state, 'redispatch', 'VC-2', 5, approve), 5, 'VC-2')
            self._batch_save(name, state, 12, result=result)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        facts = [fact for fact in self.store_facts() if fact['fact_kind'] == 'rule_classification_recorded']
        self.assertEqual([fact['producer_run']['run_dir'] for fact in facts], [str(state['failed_run'])], '复用崩溃前唯一事实')
        self.assertEqual(collections.Counter(fact['fact_kind'] for fact in self.store_facts())['rule_classification_recorded'], 1)
        self.assertEqual(claude_upgrade._load_stage_result(campaign, 'classify')['revision'], 'rev-0002')
        self.assertTrue(claude_upgrade_vc.vc_checkpoint_path(campaign, 'VC-2').is_file())
        audit.check_trusted()
        audit.check_closure(executed=[item['action_id'] for item in proof['actions']], expected=['vc2-classify-approve'],
            reused=self._committed(campaign, 5)['reuse_item_ids'], expected_reused=[], what='阶段幂等重派证明',
            source=audit.closure_source(proof, ('actions',), row_key='action_id'))
        audit.check_accounting(root_causes=1, repeats=[('重复对账父 run', state['first'], state['second'])])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['reconciled'], 'redispatch-same-batch')
        audit.report(failed_run=state['failed_run'].name, redispatched_sequence=5,
                     completion=state['result']['timing_ledger']['completion'] is not None)

    @_resumable_method
    def _scenario_same_root_cause_pause(self) -> None:
        """原两次同根因故障、上限拒绝、受控修复登记与同序号恢复。"""
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        name, scenario = 'batch-root-cause', '同根因暂停'
        state = self._batch_start(name, scenario, self.build_batch_campaign)
        campaign = state['campaign']
        audit = self._audit(scenario, campaign)
        actions = self.draft_actions(campaign)
        original = claude_upgrade.compile_vc_batch
        def crashing(arguments: Any, **options: Any) -> Any:
            original(arguments, **options)
            raise RuntimeError('故障恢复链注入：prepare 之后失败')
        for index in range(2):
            if state['phase'] == index:
                with mock.patch.object(claude_upgrade, 'compile_vc_batch', side_effect=crashing):
                    code, result, stderr = self._batch_dispatch(name, state, f'failure-{index + 1}', 'VC-2', 2, actions)
                self.assertEqual((code, result), (1, None), stderr)
                self._batch_save(name, state, index + 1, messages=[*state.get('messages', []), stderr])
        if state['phase'] == 2:
            (cause, count), = self.head()['root_cause_counts'].items()
            self.assertEqual(count, 2)
            self._batch_save(name, state, 3, cause=cause, counted=audit.accounting_state(),
                refused_state=audit.accounting_state(), attempts=sorted(path.name for path in self.staging_dir(2).iterdir()))
        if state['phase'] == 3:
            code, _result, stderr = self._batch_dispatch(name, state, 'limit-refusal', 'VC-2', 2, actions)
            self.assertEqual(code, 1)
            self.assertIn('根因达上限', stderr)
            self.assertEqual(sorted(path.name for path in self.staging_dir(2).iterdir()), state['attempts'])
            self._batch_save(name, state, 4, after_refusal=audit.accounting_state())
        if state['phase'] == 4:
            self._batch_save(name, state, 5)
            regression = self.root / 'regression-summary.txt'
            regression.write_text('test_claude_upgrade_staging_recovery exit=0\n', encoding='utf-8')
            deploy = ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp='20261008t000000z')
            sha = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            argv = [sys.executable, '-m', 'tools.official_client_capture.codex_upgrade_project_ledger', 'record-root-cause-repair',
                '--ledger-dir', str(self.project_root), '--root-cause-id', state['cause'], '--kind', 'code',
                '--binding', f"fix_commit_sha={'b' * 40}", '--binding', f'regression_receipt_sha256={sha(regression)}',
                '--binding', f'deployment_receipt_sha256={sha(deploy)}', '--note', '故障恢复链：修复导致同根因连续失败的缺陷']
            self._batch_save(name, state, 6, repair_argv=argv, repair_bindings=[stops.file_cut(regression), stops.file_cut(deploy)])
        if state['phase'] == 5:
            raise ValueError('原修复输入物化未闭合；禁止覆盖或重新形成部署收据')
        for binding in state['repair_bindings']:
            stops.read_cut(binding)
        if state['phase'] == 6:
            argv = state['repair_argv']
            def repair() -> tuple[int, Any, str]:
                pending = stops.begin_command(argv)
                done = subprocess.run(argv, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                stops.record_command(argv, done.returncode, done.stdout, done.stderr, pending=pending)
                try:
                    result = json.loads(done.stdout) if done.stdout.strip() else None
                except ValueError:
                    result = None
                return done.returncode, result, done.stderr
            code, _result, stderr = self._batch_command(name, state, 'record-repair', argv, repair)
            self.assertEqual(code, 0, stderr)
            self._batch_save(name, state, 7)
        if state['phase'] == 7:
            result = self.assert_batch_passed(self._batch_dispatch(name, state, 'redispatch', 'VC-2', 2, actions), 2, 'VC-2')
            self.assertEqual(result['commit']['staging_attempt'], len(state['attempts']) + 1)
            self._batch_save(name, state, 8)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        self.assertEqual(self.head()['root_cause_counts'].get(state['cause']), 0)
        audit.check_trusted()
        batch = self._committed(campaign, 2)
        audit.check_closure(executed=batch['execute_item_ids'], expected=['vc2-classify-draft'], reused=batch['reuse_item_ids'],
            expected_reused=[], what='修复后同序号重派批次', source=self._batch_closure(audit, campaign, 2))
        audit.check_accounting(root_causes=2, root_cause_state=state['counted'],
            repeats=[('达上限时派发', state['refused_state'], state['after_refusal'])])
        self.assert_unique_accounting()
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['messages'][1], 'record-root-cause-repair', '同序号重新派发')
        audit.report(root_cause_id=state['cause'], staging_attempts=len(state['attempts']) + 1)

    @_resumable_method
    def _scenario_budget_expired(self) -> None:
        """原截止真实到期再批准延期；恢复不重置开始、原截止或已冻结的新截止。"""
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
        name, scenario = 'batch-budget', '预算到期'
        create, build_plan = timing.create_ledger, vc_artifacts.build_campaign_plan
        started = datetime.now(timezone.utc) - timedelta(minutes=30) + timedelta(seconds=8)
        deadline = started + timedelta(minutes=30)
        def aged(*arguments: Any, **options: Any) -> Any:
            options.update(started_at_utc=started.isoformat(), total_budget_minutes=30,
                stage_budgets_minutes={phase: 30 for phase in timing.PHASE_ORDER})
            return create(*arguments, **options)
        def plan_with_ledger_deadline(*arguments: Any, **options: Any) -> Any:
            options['original_deadline_at_utc'] = deadline.isoformat()
            return build_plan(*arguments, **options)
        def builder() -> Path:
            with mock.patch.object(timing, 'create_ledger', side_effect=aged), \
                    mock.patch.object(vc_artifacts, 'build_campaign_plan', side_effect=plan_with_ledger_deadline):
                return self.build_batch_campaign()
        state = self._batch_start(name, scenario, builder)
        campaign = state['campaign']
        audit = self._audit(scenario, campaign)
        if state['phase'] == 0:
            deadline_text = timing.inspect_ledger(self.ledger_dir)['total_deadline_at_utc']
            self.assertEqual(deadline_text, staging_tests._read(campaign / 'control/vc/campaign-plan.json')['original_deadline_at_utc'])
            paths = (campaign / 'campaign.json', campaign / 'control/vc/campaign-plan.json',
                self.ledger_dir / 'ledger.json', self.project_root / 'plan.json')
            self._batch_save(name, state, 1, deadline=deadline_text, originals={path: path.read_bytes() for path in paths})
        deadline = datetime.fromisoformat(state['deadline'])
        actions = self.draft_actions(campaign)
        if state['phase'] == 1:
            while datetime.now(timezone.utc) <= deadline:
                time.sleep(0.2)
            self._batch_save(name, state, 2, before_refusal=audit.accounting_state())
        if state['phase'] == 2:
            code, _result, refused = self._batch_dispatch(name, state, 'deadline-refusal', 'VC-2', 2, actions)
            self.assertEqual(code, 1, refused)
            self.assertFalse(self.staging_dir(2).exists(), '到期后派发必须零写入')
            self._batch_save(name, state, 3, refused=refused, after_refusal=audit.accounting_state(),
                new_deadline=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat())
        if state['phase'] == 3:
            argv = ['deadline-extend', 'preview', '--campaign-dir', campaign, '--scope', 'campaign',
                '--new-deadline-at-utc', state['new_deadline'], '--reason', '故障恢复链：预算真实到期后延期续跑']
            code, preview, stderr = self._batch_command(name, state, 'deadline-preview', argv)
            self.assertIsNotNone(preview, stderr)
            self.assertEqual(code, 0, stderr)
            self._batch_save(name, state, 4, preview=preview)
        if state['phase'] == 4:
            preview = state['preview']
            argv = ['deadline-extend', 'apply', '--campaign-dir', campaign, '--preview', preview['preview_path'],
                '--approve-sha256', preview['review_sha256'], '--approved-by', '故障恢复链']
            code, applied, stderr = self._batch_command(name, state, 'deadline-apply', argv)
            self.assertIsNotNone(applied, stderr)
            self.assertEqual(code, 0, stderr)
            self._batch_save(name, state, 5)
        if state['phase'] == 5:
            self.assert_batch_passed(self._batch_dispatch(name, state, 'dispatch', 'VC-2', 2, actions), 2, 'VC-2')
            self._batch_save(name, state, 6)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        batch = self._committed(campaign, 2)
        plan = json.loads(state['originals'][campaign / 'control/vc/campaign-plan.json'])
        self.assertEqual(batch['original_deadline_at_utc'], plan['original_deadline_at_utc'])
        self.assertIn('deadline_extension', batch)
        self.assertEqual(state['originals'], {path: path.read_bytes() for path in state['originals']}, '原始坐标不变，延期只追加')
        audit.check_trusted()
        audit.check_closure(executed=batch['execute_item_ids'], expected=['vc2-classify-draft'], reused=batch['reuse_item_ids'],
            expected_reused=[], what='延期后同序号派发批次', source=self._batch_closure(audit, campaign, 2))
        audit.check_accounting(repeats=[('到期时派发', state['before_refusal'], state['after_refusal'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['refused'], '预算已暂停', '批准延期')
        audit.report(extended_to=state['new_deadline'])

    @_resumable_method
    def _scenario_seal_batch_failure(self) -> None:
        """原 seal 失败、阶段审核与 N+1 原动作替身重派；不因恢复再次生成 stub。"""
        from tools.official_client_capture import claude_upgrade_static
        from tools.official_client_capture import claude_upgrade_vc
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        if not site.ENABLE_USER_SITE:
            self.fail('动作替身要求启用用户 site（不跳过：跳过即未认证）')
        name, scenario = 'batch-seal', 'seal 失败'
        builder = lambda: self.build_batch_campaign(vc1_sealed=False, predecessor=dict(batch_tests.Vc1SealBatchReplayTests.PREDECESSOR))
        state = self._batch_start(name, scenario, builder)
        campaign = state['campaign']
        audit = self._audit(scenario, campaign)
        actions = claude_upgrade_static.vc1_seal_actions(campaign)
        if state['phase'] == 0:
            self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, 'VC-1').exists())
            code, failed, stderr = self._batch_dispatch(name, state, 'seal-failure', 'VC-1', 2, actions)
            self.assertIsNotNone(failed, stderr)
            self.assertEqual((code, failed['campaign_run']['reason']), (1, f'action-failed:{claude_upgrade_static.ACTION_ID}'), stderr)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)['status'], 'stage_review_required')
            self._batch_save(name, state, 1, failed_run=Path(failed['campaign_run']['run_dir']), before_refusal=audit.accounting_state())
        if state['phase'] == 1:
            code, _result, stderr = self._batch_dispatch(name, state, 'review-refusal', 'VC-1', 3, actions)
            self.assertEqual(code, 1)
            self.assertIn('stage_review_required', stderr)
            self.assertFalse(self.staging_dir(3, 'VC-1').exists())
            self._batch_save(name, state, 2, after_refusal=audit.accounting_state())
        argv = ['reconcile-supervisor-run', '--campaign-dir', campaign, '--run-dir', state['failed_run']]
        if state['phase'] == 2:
            code, reconciled, stderr = self._batch_command(name, state, 'reconcile-first', argv)
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._batch_save(name, state, 3, reconciled=reconciled, first=audit.accounting_state())
        if state['phase'] == 3:
            code, again, stderr = self._batch_command(name, state, 'reconcile-repeat', argv)
            self.assertEqual((code, again['stage_replay']), (0, state['reconciled']['stage_replay']), stderr)
            self._batch_save(name, state, 4, second=audit.accounting_state())
        proof = state['reconciled']['stage_replay']
        self.assertEqual((proof['allowed'], proof['phase'], proof['next_action']), (True, 'VC-1', 'redispatch-same-batch'))
        if state['phase'] == 4:
            self._batch_save(name, state, 5)
            inject = self.root / 'inject'
            inject.mkdir(mode=0o700)
            injection = inject / 'usercustomize.py'
            injection.write_text(batch_tests.VC1_STUB_USERCUSTOMIZE, encoding='utf-8')
            stub = {'PYTHONPATH': os.pathsep.join([str(inject), os.environ.get('PYTHONPATH', '')]).rstrip(os.pathsep),
                    batch_tests.VC1_STUB_ACTIONS_ENV: ','.join(item['action_id'] for item in actions)}
            self._batch_save(name, state, 6, stub=stub, injection_binding=stops.file_cut(injection))
        if state['phase'] == 5:
            raise ValueError('原 seal 动作替身输入未闭合；禁止覆盖原文件')
        stops.read_cut(state['injection_binding'])
        if state['phase'] == 6:
            with mock.patch.dict(os.environ, state['stub']):
                self.assert_batch_passed(self._batch_dispatch(name, state, 'redispatch', 'VC-1', 3, actions), 3, 'VC-1')
            self._batch_save(name, state, 7)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        batch = self._committed(campaign, 3, 'VC-1')
        audit.check_trusted()
        audit.check_closure(executed=batch['execute_item_ids'], expected=[item['action_id'] for item in actions],
            reused=batch['reuse_item_ids'], expected_reused=[], what='封存批次逐字重派', source=self._batch_closure(audit, campaign, 3, 'VC-1'))
        audit.check_accounting(repeats=[('对账前派发 N+1', state['before_refusal'], state['after_refusal']),
                                      ('重复对账父 run', state['first'], state['second'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['reconciled'], 'redispatch-same-batch')
        audit.report(failed_run=state['failed_run'].name, redispatched_sequence=3)


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

    def _official_start(self, name: str, scenario: str, builder: Callable[[], Path]) -> dict[str, Any]:
        """只首次创建 Campaign；初始化没有闭合时明确拒绝，不能重新建账。"""

        state = stops.resume_checkpoint(self, name)
        if state is not None:
            if state.get('phase') == 'initializing':
                raise ValueError('官方故障链的首次建场尚未闭合；保留原坐标，禁止重新构造 Campaign')
            return state
        if os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('官方故障方法缺原游标，禁止重新初始化')
        stops.checkpoint(self, name, phase='initializing', audit_snapshots={scenario: 'uninitialized'})
        self.campaign = builder()
        state = {'phase': 0, 'campaign': self.campaign, 'audit_snapshots': {scenario: 'uninitialized'}}
        stops.checkpoint(self, name, **state)
        return state

    def _official_save(self, name: str, state: dict[str, Any], phase: int, **values: Any) -> None:
        state.update(values, phase=phase)
        stops.checkpoint(self, name, **state)

    def _official_cli(self, name: str, key: str, campaign: Path, argv: list[str]) -> tuple[int, Any, str]:
        with self.chain_patched():
            return stops.command_step(self, name, key, argv, lambda: self.run_main(argv), campaign=campaign)

    @_resumable_method
    def _scenario_reserved_attempt_interrupted(self) -> None:
        """原孤儿 attempt 先对账入账，独立重复对账后批准，仅从未完成的动作续作。"""

        from tools.official_client_capture import claude_upgrade

        name, scenario = 'official-reserved', '预约后中断'
        state = self._official_start(name, scenario, self.build_recovery_campaign)
        campaign = state['campaign']
        if state['phase'] == 0:
            self._official_save(name, state, 1)
            orphan, completed, interrupted = self._orphan(campaign)
            self._official_save(name, state, 2, orphan=orphan, completed=completed, interrupted=interrupted)
        if state['phase'] == 1:
            raise ValueError('孤儿故障注入未形成完整原停点；须检查原 attempt，禁止再次注入或取证')
        orphan, completed, interrupted = (state[key] for key in ('orphan', 'completed', 'interrupted'))
        roots = self._job_roots(campaign)
        audit = self._audit(scenario, campaign, trusted=[orphan, roots[completed], roots[interrupted]])
        if state['phase'] == 2:
            argv = ['status', '--campaign-dir', str(campaign)]
            code, status, stderr = self._official_cli(name, 'status', campaign, argv)
            self.assertEqual(status['status'], 'official_capture_interrupted', stderr)
            code, reconciled, stderr = self._official_cli(name, 'reconcile-first', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name])
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._official_save(name, state, 3, status=status, reconciled=reconciled, first=audit.accounting_state())
        reconciled = state['reconciled']
        preview = reconciled['recovery_preview']
        if state['phase'] == 3:
            code, _repeat, stderr = self._official_cli(name, 'reconcile-repeat', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name])
            self.assertEqual(code, 0, stderr)
            self._official_save(name, state, 4, second=audit.accounting_state())
        provenance = recovery_tests._read(campaign / reconciled['provenance_receipt']['path'])
        counted = sum(job['precise_count'] for job in provenance['jobs'])
        self.assertGreater(sum(job['precise_count'] for job in provenance['jobs'] if job['job_id'] == interrupted), 0)
        if state['phase'] == 4:
            code, approved, stderr = self._official_cli(name, 'approve', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name,
                 '--approve-recovery-sha256', preview['review_sha256']])
            self.assertEqual((code, approved['recovery_approval']['approved_sha256']), (0, preview['review_sha256']), stderr)
            self._official_save(name, state, 5, calls_before=self.relay_probe_calls())
        if state['phase'] == 5:
            code, resumed, stderr = self._official_cli(name, 'resume', campaign,
                ['resume', '--campaign-dir', str(campaign), '--rerun-failed', '--recovery-preview',
                 reconciled['recovery_preview_path'], '--acknowledge-live-requests'])
            self.assertEqual((code, resumed['status']), (2, 'awaiting_receipts'), stderr)
            self._official_save(name, state, 6, resumed=resumed, calls_after=self.relay_probe_calls())
        resumed = state['resumed']
        supersession = recovery_tests._read(campaign / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{resumed['attempt_id']}.json")
        archived = {row['host_root']: row['archived_host_root'] for row in supersession['roots']}
        self.assertEqual(sorted(archived), sorted(str(roots[job]) for job in (completed, interrupted)))
        executed_probes = state['calls_after'][len(state['calls_before']):]
        audit.check_trusted(relocated=archived)
        audit.check_closure(executed=preview['execute_job_ids'], expected=sorted(roots),
            reused=preview['reuse_job_ids'], expected_reused=[], what='孤儿 attempt 的恢复预览',
            source=audit.closure_source(preview, ('execute_job_ids',), ('reuse_job_ids',)))
        self.assertEqual(sorted(executed_probes), sorted(probe for job in sorted(roots) for probe in self.job_probes(job)))
        audit.check_accounting(precise_delta=counted, root_causes=1,
            repeats=[('重复对账孤儿 attempt', state['first'], state['second'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['status']['next_command'], 'reconcile-attempt')
        audit.report(orphan_attempt=orphan.name, resumed_attempt=resumed['attempt_id'], counted_requests=counted)
        self._official_save(name, state, 7)

    @_resumable_method
    def _scenario_accounting_unresolved(self) -> None:
        """原账务暂停、补账批准、重复补账和恢复分别保存，原请求与原 before 不重建。"""

        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
        from tools.official_client_capture import claude_upgrade

        name, scenario = 'official-accounting', '账务未决'
        state = self._official_start(name, scenario, self.build_recovery_campaign)
        campaign = state['campaign']
        if state['phase'] == 0:
            self._official_save(name, state, 1)
            orphan, completed, interrupted = self._orphan(campaign)
            self._official_save(name, state, 2, orphan=orphan, completed=completed, interrupted=interrupted)
        if state['phase'] == 1:
            raise ValueError('账务故障注入缺完整原停点，禁止再次取证')
        orphan, completed, interrupted = (state[key] for key in ('orphan', 'completed', 'interrupted'))
        roots = self._job_roots(campaign)
        if state['phase'] == 2:
            connections = sorted(roots[interrupted].rglob('*.client_to_upstream.bin'))
            self.assertTrue(connections, '被打断的 Job 没有中继连接字节')
            self._official_save(name, state, 3, removed_connection=stops.file_cut(connections[0]))
            connections[0].unlink()
            self._official_save(name, state, 4)
        if state['phase'] == 3:
            if Path(state['removed_connection']['path']).exists():
                stops.read_cut(state['removed_connection'])
                Path(state['removed_connection']['path']).unlink()
            self._official_save(name, state, 4)
        audit = self._audit(scenario, campaign, trusted=[orphan / f'job-{completed}.json', roots[completed]])
        if state['phase'] == 4:
            code, paused, stderr = self._official_cli(name, 'pause', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name])
            self.assertEqual((code, paused['status'], paused['decision']['pause_kinds']), (3, 'paused', ['accounting']), stderr)
            self.assertIsNone(paused['decision']['terminal_reason'])
            operation = f'reconcile-attempt:{orphan.name}'
            head = project_ledger.replay_head(self.project_root)
            self.assertEqual((head['blocked'], head['unresolved_operation_ids']), (True, [operation]))
            evidence = self.root / 'provenance-audit.txt'
            if evidence.exists():
                raise ValueError('补账说明已存在但无原停点，禁止覆盖')
            evidence.write_text(f'{interrupted} 的中继在写客户端字节时被杀，按同类作业上界估计 3 次请求。\n', encoding='utf-8')
            self._official_save(name, state, 5, paused=paused, operation=operation, evidence=stops.file_cut(evidence))
        stops.read_cut(state['evidence'])
        argv = ['accounting-resolve', '--campaign-dir', str(campaign), '--operation-id', state['operation'],
                '--estimated-count', '3', '--evidence', str(state['evidence']['path']), '--reason', '被打断作业缺客户端字节，按上界估计']
        if state['phase'] == 5:
            code, preview, stderr = self._official_cli(name, 'resolution-preview', campaign, argv)
            self.assertEqual((code, preview['status'], preview['resolution_mode']), (0, 'approval_required', 'estimated_upper_bound'), stderr)
            self._official_save(name, state, 6, preview=preview)
        approve = [*argv, '--approve-sha256', state['preview']['review_sha256'], '--approved-by', '故障恢复链']
        if state['phase'] == 6:
            code, resolved, stderr = self._official_cli(name, 'resolution-apply', campaign, approve)
            self.assertEqual((code, resolved['status'], resolved['blocked']), (0, 'resolved', False), stderr)
            self._official_save(name, state, 7, first=audit.accounting_state())
        if state['phase'] == 7:
            code, rerun, stderr = self._official_cli(name, 'resolution-repeat', campaign, approve)
            self.assertEqual((code, rerun['status'], rerun['batch']['reused']), (0, 'resolved', True), stderr)
            self._official_save(name, state, 8, second=audit.accounting_state())
        if state['phase'] == 8:
            code, reconciled, stderr = self._official_cli(name, 'reconcile', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name])
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._official_save(name, state, 9, reconciled=reconciled)
        reconciled = state['reconciled']
        if state['phase'] == 9:
            code, _approved, stderr = self._official_cli(name, 'approve', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', orphan.name,
                 '--approve-recovery-sha256', reconciled['recovery_preview']['review_sha256']])
            self.assertEqual(code, 0, stderr)
            self._official_save(name, state, 10)
        if state['phase'] == 10:
            code, resumed, stderr = self._official_cli(name, 'resume', campaign,
                ['resume', '--campaign-dir', str(campaign), '--rerun-failed', '--recovery-preview',
                 reconciled['recovery_preview_path'], '--acknowledge-live-requests'])
            self.assertEqual((code, resumed['status']), (2, 'awaiting_receipts'), stderr)
            self._official_save(name, state, 11, resumed=resumed)
        resumed, paused = state['resumed'], state['paused']
        head = project_ledger.replay_head(self.project_root)
        self.assertEqual((head['blocked'], head['unresolved_operation_ids'], len(head['accounting_resolutions'][paused['campaign_id']])), (False, [], 1))
        supersession = recovery_tests._read(campaign / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{resumed['attempt_id']}.json")
        archived = {row['host_root']: row['archived_host_root'] for row in supersession['roots']}
        precise = self._counted(campaign, paused)
        audit.check_trusted(relocated=archived)
        audit.check_closure(executed=reconciled['recovery_preview']['execute_job_ids'], expected=sorted(roots),
            reused=reconciled['recovery_preview']['reuse_job_ids'], expected_reused=[], what='补账后的恢复预览',
            source=audit.closure_source(reconciled['recovery_preview'], ('execute_job_ids',), ('reuse_job_ids',)))
        audit.check_accounting(precise_delta=precise, estimated_delta=3, root_causes=1,
            repeats=[('以同一批准重跑补账', state['first'], state['second'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(paused['next_command'], 'accounting-resolve')
        audit.report(operation=state['operation'], estimated=3, precise=precise)
        self._official_save(name, state, 12)

    build_evolution_campaign = recovery_tests.ToolEvolutionChainTests.build_evolution_campaign
    evolved_identity = recovery_tests.ToolEvolutionChainTests.evolved_identity

    @_resumable_method
    def _scenario_tool_evolution_replay(self) -> None:
        """保留原首批与形成期身份，原演进批准后只续作失效 Job 和尚未完成的封存。"""

        from tools.official_client_capture import claude_upgrade, claude_upgrade_store
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
        from tools.official_client_control import receipts as store_receipts

        name, scenario = 'official-evolution', '工具演进后旧链重放'
        state = self._official_start(name, scenario, self.build_evolution_campaign)
        campaign = state['campaign']
        if state['phase'] == 0:
            code, first, stderr = self._official_cli(name, 'capture-first', campaign,
                ['capture-official', 'run', '--campaign-dir', str(campaign), '--acknowledge-live-requests'])
            self.assertEqual((code, first['status']), (2, 'awaiting_receipts'), stderr)
            self._official_save(name, state, 1, first=first)
        first = state['first']
        if state['phase'] == 1:
            argv = self.seal_argv(campaign, first['attempt_id'])
            with self.chain_patched():
                code, preview, stderr = stops.command_step(self, name, 'seal-first', argv,
                    lambda: seal_tests.run_main(argv), campaign=campaign)
            self.assertEqual((code, preview['status']), (2, 'awaiting_seal_approval'), stderr)
            self._official_save(name, state, 2)
        roots = self._job_roots(campaign)
        others = sorted(job for job in roots if job != recovery_tests.EVOLVED_JOB)
        audit = self._audit(scenario, campaign,
            trusted=[self._attempt_root(campaign, first['attempt_id']), *(roots[job] for job in others)])
        key = f'official_client_capture/{recovery_tests.EVOLVED_DEPENDENCY}'
        if state['phase'] == 2:
            original = self.tree.path(key).read_bytes()
            evolved = self.evolved_identity()
            created_at = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            # 先冻结原 helper 生成的完整预期字节，再允许向世界写出收据。
            # 私有准备目录不进入产品来源；恢复只消费同一次写入意图。
            with tempfile.TemporaryDirectory(prefix='cf-deploy-intent-') as directory:
                planned = ledger_fixture.write_claude_deploy_receipt(Path(directory), evolved,
                    stamp='20261008t010000z', created_at_utc=created_at)
                deploy_raw = planned.read_bytes()
            self._official_save(name, state, 3, evolved=evolved,
                original_tool=original, revised_tool=original + '\n# B5b-03 工具演进夹具\n'.encode('utf-8'),
                deploy_created_at=created_at, deploy_raw=deploy_raw)
        if state['phase'] == 3:
            current = self.tree.path(key).read_bytes()
            if current not in (state['original_tool'], state['revised_tool']):
                raise ValueError('原演进夹具工具字节不匹配，禁止再次注入修复')
            if current == state['original_tool']:
                self.tree.write(key, state['revised_tool'])
            deployed = self.data / 'control/claude-managed-deploy-20261008t010000z.json'
            if deployed.exists():
                if deployed.is_symlink() or not deployed.is_file() or deployed.read_bytes() != state['deploy_raw']:
                    raise ValueError('原演进部署收据与冻结的同次写入意图不一致，禁止覆盖')
            else:
                deployed = ledger_fixture.write_claude_deploy_receipt(self.data, state['evolved'],
                    stamp='20261008t010000z', created_at_utc=state['deploy_created_at'])
                self.assertEqual(deployed.read_bytes(), state['deploy_raw'], '原部署 helper 输出与冻结意图不同')
            self._official_save(name, state, 4, deployed=stops.file_cut(deployed))
        stops.read_cut(state['deployed'])
        if stops.record_root() is None:
            self.addCleanup(self.tree.restore)
        evolved = state['evolved']
        evolution_argv = ['tool-evolution', '--campaign-dir', str(campaign), '--fix-commit', 'a' * 40, '--reason', '修复 FW-F v3 判定']

        def command(command_key: str, argv: list[str], *, seal: bool = False) -> tuple[int, Any, str]:
            with self.chain_patched(), mock.patch.object(claude_upgrade, '_tool_identity', side_effect=lambda **_: json.loads(json.dumps(evolved))):
                return stops.command_step(self, name, command_key, argv,
                    lambda: seal_tests.run_main(argv) if seal else self.run_main(argv), campaign=campaign)

        if state['phase'] == 4:
            code, _result, refused_seal = command('seal-unregistered', self.seal_argv(campaign, first['attempt_id']), seal=True)
            self.assertEqual(code, 1)
            self.assertIn('尚未登记工具演进', refused_seal)
            self._official_save(name, state, 5, refused_seal=refused_seal)
        if state['phase'] == 5:
            code, proposed, stderr = command('evolution-preview', evolution_argv)
            self.assertEqual((code, proposed['status']), (0, 'approval_required'), stderr)
            self.assertEqual(proposed['impact']['official']['affected_job_ids'], [recovery_tests.EVOLVED_JOB])
            self._official_save(name, state, 6, proposed=proposed)
        approve = [*evolution_argv, '--approve-sha256', state['proposed']['review_sha256'], '--approved-by', '故障恢复链']
        if state['phase'] == 6:
            code, applied, stderr = command('evolution-apply', approve)
            self.assertEqual((code, applied['status'], applied['store_registration']['status']), (0, 'evolution_applied', 'registered'), stderr)
            self._official_save(name, state, 7, first_state=audit.accounting_state())
        if state['phase'] == 7:
            code, again, stderr = command('evolution-repeat', approve)
            self.assertEqual((code, again['reused'], again['store_registration']['status']), (0, True, 'current'), stderr)
            self._official_save(name, state, 8, second_state=audit.accounting_state())
        if state['phase'] == 8:
            code, status, stderr = command('status', ['status', '--campaign-dir', str(campaign)])
            self.assertEqual(status['official_attempt']['evolution_invalidated_job_ids'], [recovery_tests.EVOLVED_JOB], stderr)
            self._official_save(name, state, 9, status=status)
        if state['phase'] == 9:
            code, _result, stderr = command('seal-invalidated', self.seal_argv(campaign, first['attempt_id']), seal=True)
            self.assertEqual(code, 1)
            self.assertIn('被工具演进作废', stderr)
            self._official_save(name, state, 10)
        if state['phase'] == 10:
            code, reconciled, stderr = command('reconcile', ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', first['attempt_id']])
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._official_save(name, state, 11, reconciled=reconciled)
        reconciled = state['reconciled']
        recovery_preview = reconciled['recovery_preview']
        if state['phase'] == 11:
            code, _approved, stderr = command('recovery-approve', ['reconcile-attempt', '--campaign-dir', str(campaign),
                '--attempt-id', first['attempt_id'], '--approve-recovery-sha256', recovery_preview['review_sha256']])
            self.assertEqual(code, 0, stderr)
            self._official_save(name, state, 12, calls_before=self.relay_probe_calls())
        if state['phase'] == 12:
            code, resumed, stderr = command('resume', ['resume', '--campaign-dir', str(campaign), '--rerun-failed',
                '--recovery-preview', reconciled['recovery_preview_path'], '--acknowledge-live-requests'])
            self.assertEqual((code, resumed['status']), (2, 'awaiting_receipts'), stderr)
            self._official_save(name, state, 13, resumed=resumed, calls_after=self.relay_probe_calls())
        resumed = state['resumed']
        self.assertEqual(state['calls_after'][len(state['calls_before']):], self.job_probes(recovery_tests.EVOLVED_JOB))
        if state['phase'] == 13:
            code, sealed_preview, stderr = command('seal-resumed', self.seal_argv(campaign, resumed['attempt_id']), seal=True)
            self.assertEqual((code, sealed_preview['status']), (2, 'awaiting_seal_approval'), stderr)
            self._official_save(name, state, 14)
        with self.chain_patched(), mock.patch.object(claude_upgrade, '_tool_identity', side_effect=lambda **_: json.loads(json.dumps(evolved))):
            _root, replayed = claude_upgrade._load_capture_attempt(campaign, 'official', None, first['attempt_id'], _replay_evidence_permissions=True)
            self.assertEqual(replayed['attempt_id'], first['attempt_id'])
            store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign))
            self.assertEqual(len(store_receipts.tool_evolutions(store, dict(store_campaign))), 1)
            attempt_root, attempt = self.load_attempt(campaign, resumed['attempt_id'])
        reused = sorted(item['id'] for item in attempt['results'] if item['disposition'] == 'reused')
        audit.check_trusted()
        audit.check_closure(executed=recovery_preview['execute_job_ids'], expected=[recovery_tests.EVOLVED_JOB],
            reused=reused, expected_reused=others, what='演进作废后的恢复',
            source=audit.file_closure_source(campaign / reconciled['recovery_preview_path'], ('execute_job_ids',),
                ('results',), reused_path=attempt_root / 'attempt.json', row_key='id', disposition='disposition'))
        audit.check_accounting(precise_delta=self._counted(campaign, reconciled),
            repeats=[('以同一批准重跑演进登记', state['first_state'], state['second_state'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry({'seal': state['refused_seal'], 'status': state['status']['next_command']}, 'tool-evolution', 'reconcile-attempt')
        audit.report(invalidated=[recovery_tests.EVOLVED_JOB], resumed_attempt=resumed['attempt_id'])
        self._official_save(name, state, 15)

    # ---- 跨客户端误用（官方取证世界：入口按 Claude 合同读部署收据，误用一律零写入拒绝）----

    def _misuse_setup(self, scenario: str) -> tuple[str, dict[str, Any], ScenarioAudit]:
        """生产形态的原首批只建一次；坏收据恢复期间仍沿用原审计 before。"""

        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
        name = 'official-misuse:' + scenario

        def builder() -> Path:
            environment = {key: value for key, value in os.environ.items() if key != project_ledger_fixture.FIXTURE_ONLY_ENV}
            with mock.patch.dict(os.environ, environment, clear=True):
                return self.build_recovery_campaign()

        state = self._official_start(name, scenario, builder)
        campaign = state['campaign']
        self.assertFalse(project_ledger._load_plan(self.project_root)[0].get('fixture_only'))
        if state['phase'] == 0:
            argv = ['capture-official', 'run', '--campaign-dir', str(campaign), '--acknowledge-live-requests']
            def invoke() -> tuple[int, Any, str]:
                self.fail_probe(self.job_probes(recovery_tests.FAILING_JOB)[0], 3)
                return self.run_main(argv)
            with self.chain_patched():
                code, first, stderr = stops.command_step(self, name, 'capture-first', argv, invoke,
                                                       campaign=campaign)
            self.assertEqual(first['status'], 'failed', stderr)
            (receipt,) = sorted((self.data / 'control').glob('claude-managed-deploy-*.json'))
            self._official_save(name, state, 1, first=first, receipt=receipt, receipt_raw=receipt.read_bytes())
        audit = self._audit(scenario, campaign,
            trusted=[self._attempt_root(campaign, state['first']['attempt_id']), state['receipt']])
        return name, state, audit

    def _refused_reconcile(self, name: str, key: str, campaign: Path, attempt_id: str) -> str:
        code, result, stderr = self._official_cli(name, key, campaign,
            ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', attempt_id])
        self.assertEqual((code, result), (1, None), stderr)
        self.assertFalse((campaign / 'control' / 'reconciliation' / f'attempt-{attempt_id}').exists(), '误用必须零写入')
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

    @_resumable_method
    def _scenario_misuse_codex_receipt(self) -> None:
        """Codex 收据与伪装文件分别零写入拒绝，恢复只放回原 Claude 收据再对账。"""

        from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
        name, state, audit = self._misuse_setup('跨客户端误用：Codex 收据喂给 Claude 入口')
        campaign, first, receipt = (state[key] for key in ('campaign', 'first', 'receipt'))
        original = json.loads(state['receipt_raw'])
        codex = {'schema_version': 'codex-arm64-supervisor-enable/v1', 'status': 'passed',
            'created_at_utc': original['created_at_utc'], **original['identity']}
        codex_path = self.data / 'control' / 'codex-20261008t000000z-supervisor-enable-20261008t000000z.json'
        disguised = self.data / 'control' / 'claude-managed-deploy-20261008t000001z.json'
        raw = json.dumps(codex).encode('utf-8')
        if state['phase'] == 1:
            self._official_save(name, state, 2, state_before=audit.accounting_state())
        if state['phase'] == 2:
            if receipt.exists() and receipt.read_bytes() != state['receipt_raw']:
                raise ValueError('原 Claude 部署收据字节已改变')
            receipt.unlink(missing_ok=True)
            if codex_path.exists() and codex_path.read_bytes() != raw:
                raise ValueError('误用注入文件不是原 Codex 收据字节')
            if not codex_path.exists():
                codex_path.write_bytes(raw)
            refusal = self._refused_reconcile(name, 'refuse-codex', campaign, first['attempt_id'])
            self._official_save(name, state, 3, first_refusal=refusal)
        if state['phase'] == 3:
            if disguised.exists() and disguised.read_bytes() != raw:
                raise ValueError('原伪装部署收据字节已改变')
            if not disguised.exists():
                disguised.write_bytes(raw)
            second = self._refused_reconcile(name, 'refuse-disguised', campaign, first['attempt_id'])
            self._official_save(name, state, 4, second_refusal=second, state_after=audit.accounting_state())
        with self.assertRaisesRegex(pre_a3.CertificationError, '不属于客户端 claude'):
            pre_a3.real_chain_coverage({'schema_version': pre_a3.SCHEMA_VERSION_V2, 'scenarios': [],
                'real_chain_registration': pre_a3.real_chain_registration('codex')}, client='claude')
        if state['phase'] == 4:
            for path in (disguised, codex_path):
                if path.exists() and path.read_bytes() != raw:
                    raise ValueError('修复前误用注入原件已改变')
                path.unlink(missing_ok=True)
            if receipt.exists() and receipt.read_bytes() != state['receipt_raw']:
                raise ValueError('修复前已有其他 Claude 收据，禁止覆盖')
            if not receipt.exists():
                receipt.write_bytes(state['receipt_raw'])
            self._official_save(name, state, 5)
        audit.check_trusted()
        if state['phase'] == 5:
            code, reconciled, stderr = self._official_cli(name, 'reconcile-corrected', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', first['attempt_id']])
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._official_save(name, state, 6, reconciled=reconciled)
        reconciled = state['reconciled']
        preview = reconciled['recovery_preview']
        audit.check_closure(executed=preview['execute_job_ids'], expected=[recovery_tests.FAILING_JOB],
            reused=preview['reuse_job_ids'], expected_reused=sorted(set(self._job_roots(campaign)) - {recovery_tests.FAILING_JOB}),
            what='纠正后的恢复预览（误用期间零执行）',
            source=audit.closure_source(preview, ('execute_job_ids',), ('reuse_job_ids',)))
        audit.check_accounting(precise_delta=self._counted(campaign, reconciled), root_causes=1,
            repeats=[('两次误用对账', state['state_before'], state['state_after'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['first_refusal'], '部署收据', '已受管部署')
        audit.report(refusals=2)
        self._official_save(name, state, 7)

    @_resumable_method
    def _scenario_misuse_missing_client(self) -> None:
        """缺 client 的收据先拒绝，再按原字节修复；不得重新取证或换一份成功记录。"""

        from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
        name, state, audit = self._misuse_setup('跨客户端误用：缺 client 的合同')
        campaign, first, receipt = (state[key] for key in ('campaign', 'first', 'receipt'))
        stripped = {key: value for key, value in json.loads(state['receipt_raw']).items() if key != 'client'}
        raw = json.dumps(stripped, ensure_ascii=False, indent=2).encode('utf-8')
        if state['phase'] == 1:
            self._official_save(name, state, 2, state_before=audit.accounting_state())
        if state['phase'] == 2:
            if receipt.read_bytes() not in (raw, state['receipt_raw']):
                raise ValueError('缺 client 注入前的原收据字节已改变')
            if receipt.read_bytes() != raw:
                receipt.write_bytes(raw)
            refused = self._refused_reconcile(name, 'refuse-missing-client', campaign, first['attempt_id'])
            self._official_save(name, state, 3, refused=refused, state_after=audit.accounting_state())
        with self.assertRaisesRegex(pre_a3.CertificationError, '不认识的客户端'):
            pre_a3.certification_client({'schema_version': pre_a3.SCHEMA_VERSION_V3, 'scenarios': []})
        if state['phase'] == 3:
            if receipt.read_bytes() not in (raw, state['receipt_raw']):
                raise ValueError('缺 client 的原注入收据已改变，禁止覆盖')
            if receipt.read_bytes() != state['receipt_raw']:
                receipt.write_bytes(state['receipt_raw'])
            self._official_save(name, state, 4)
        audit.check_trusted()
        if state['phase'] == 4:
            code, reconciled, stderr = self._official_cli(name, 'reconcile-corrected', campaign,
                ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', first['attempt_id']])
            self.assertEqual((code, reconciled['status']), (0, 'recoverable'), stderr)
            self._official_save(name, state, 5, reconciled=reconciled)
        reconciled = state['reconciled']
        preview = reconciled['recovery_preview']
        audit.check_closure(executed=preview['execute_job_ids'], expected=[recovery_tests.FAILING_JOB],
            reused=preview['reuse_job_ids'], expected_reused=sorted(set(self._job_roots(campaign)) - {recovery_tests.FAILING_JOB}),
            what='纠正后的恢复预览（误用期间零执行）',
            source=audit.closure_source(preview, ('execute_job_ids',), ('reuse_job_ids',)))
        audit.check_accounting(precise_delta=self._counted(campaign, reconciled), root_causes=1,
            repeats=[('缺 client 时对账', state['state_before'], state['state_after'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['refused'], '部署收据', '已受管部署')
        audit.report()
        self._official_save(name, state, 6)


# ---------------------------------------------------------------------------
# 场景组三：多根受监督部署（B5a-01 的写前意图日志与按内容恢复；部署替身环境见 arm64_deploy_fixture）
# ---------------------------------------------------------------------------


class _CandidateFaultScenarios(chain_driver.ClaudeCandidateChainDriver):
    """原两个候选场景的同世界停点；来源继续由原真实namespace与VC形成链提供。"""

    COMMAND_KEYS = {
        'candidate-reconcile': ('candidate-run', 'reconcile-first', 'reconcile-repeat', 'unapproved-refusal', 'approve-first', 'approve-repeat', 'resume', 'status'),
        # 原八个键保持原顺序：未授权 resume 的真实拒绝永久保留，授权及后继调用只追加新键。
        'candidate-evolution': ('candidate-run', 'evolution-preview', 'evolution-apply', 'evolution-repeat', 'status', 'reconcile', 'approve', 'resume',
                                'authorize', 'authorize-repeat', 'resume-authorized'),
    }

    def _candidate_start(self, name: str, scenario: str) -> dict[str, Any]:
        state = stops.resume_checkpoint(self, name)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV):
                entry = stops.load_state('candidate-method-entry')
                if (entry != {'method': self._testMethodName, 'status': 'uninitialized'}
                        or stops.load_state('candidate-vc5-start') is not None):
                    raise ValueError('候选原方法缺明确的首次读侧交接许可，禁止重建')
            stops.checkpoint(self, name, phase='initializing', audit_snapshots={scenario: 'uninitialized'})
            campaign = self.candidate_world()
            state = {'phase': 0, 'campaign': campaign, 'completed_commands': [],
                'audit_snapshots': {scenario: 'uninitialized'}}
            stops.checkpoint(self, name, **state)
            stops.save_state('candidate-method-entry', {'method': self._testMethodName, 'status': 'started'})
        elif state['phase'] == 'initializing':
            original = stops.load_state('candidate-vc5-start')
            if original is None or original.get('status') != 'complete':
                raise ValueError('候选原阶段首次绑定未闭合；保留原件，禁止重建')
            campaign = self.candidate_world()
            state = {'phase': 0, 'campaign': campaign, 'completed_commands': [],
                'audit_snapshots': {scenario: 'uninitialized'}}
            stops.checkpoint(self, name, **state)
        else:
            self.assertEqual(self.candidate_world(), state['campaign'])
            rows = state['completed_commands']
            if tuple(row['key'] for row in rows) != self.COMMAND_KEYS[name][:len(rows)]:
                raise ValueError('候选原命令清单不是冻结逻辑顺序的前缀')
            with self.source_context(), stops.verified_read_only_world(self, state['campaign']):
                for row in rows:
                    outcome = stops.command_step(self, name, row['key'], row['argv'],
                        lambda: self.fail('已完成候选命令不得重发'), campaign=state['campaign'],
                        enclosing_commands=chain_driver.original_enclosing_commands(self),
                        preflight_retry_verifier=self.candidate_token_preflight_retry if row['key'] == 'candidate-run' else None)
                    self.assertEqual(outcome, row['outcome'], '候选原停点与完整stdout/stderr不一致')
        return state

    def _candidate_save(self, name: str, state: dict[str, Any], phase: int, **values: Any) -> None:
        state.update(values, phase=phase)
        stops.checkpoint(self, name, **state)

    def _candidate_cli(self, name: str, state: dict[str, Any], key: str, argv: list[Any]) -> tuple[int, Any, str]:
        if self.COMMAND_KEYS[name][len(state['completed_commands'])] != key:
            raise ValueError('候选命令不在原下一逻辑停点')
        with self.source_context():
            outcome = stops.command_step(self, name, key, argv, lambda: self.run_main(argv), campaign=state['campaign'],
                enclosing_commands=chain_driver.original_enclosing_commands(self),
                preflight_retry_verifier=self.candidate_token_preflight_retry if key == 'candidate-run' else None)
        state['completed_commands'].append({'key': key, 'argv': argv, 'outcome': outcome})
        return outcome

    def _candidate_run_step(self, name: str, state: dict[str, Any], *, fail_refresh: bool) -> dict[str, Any]:
        """原故障输入只设置一次；命令pending或complete时只重读原输出，不再注入。"""
        from tools.official_client_capture import claude_upgrade
        command_state = stops.load_state('command-step:' + name + ':candidate-run')
        planned = {chain_driver.REFRESH_PROBE: 1 if fail_refresh else 0}
        if command_state is None:
            if state.get('capture_input_started'):
                if json.loads(self.failures_file.read_bytes()) != planned:
                    raise ValueError('候选原采集故障输入未闭合，禁止再次设置或重新取证')
            else:
                state['capture_input_started'] = True
                stops.checkpoint(self, name, **state)
                self.fail_probe(chain_driver.REFRESH_PROBE, planned[chain_driver.REFRESH_PROBE])
        argv = ['capture-candidate', 'run', '--campaign-dir', str(state['campaign']),
            '--candidate-id', chain_driver.CANDIDATE, '--acknowledge-live-requests']
        with mock.patch.object(claude_upgrade, 'JOB_RETRY_LIMIT', 0):
            code, result, stderr = self._candidate_cli(name, state, 'candidate-run', argv)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result['status']), (2, 'failed' if fail_refresh else 'awaiting_receipts'), stderr)
        if json.loads(self.failures_file.read_bytes()) != {chain_driver.REFRESH_PROBE: 0}:
            self.fail_probe(chain_driver.REFRESH_PROBE, 0)
        return result

    def _candidate_reconcile_step(self, name: str, state: dict[str, Any], key: str,
                                  attempt: str, preview: dict[str, Any] | None = None) -> dict[str, Any]:
        argv = ['reconcile-attempt', '--campaign-dir', str(state['campaign']), '--attempt-id', attempt]
        if preview is not None:
            argv.extend(['--approve-recovery-sha256', preview['review_sha256']])
        code, result, stderr = self._candidate_cli(name, state, key, argv)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result['status']), (0, 'recoverable'), stderr)
        if preview is not None:
            self.assertEqual(result['recovery_approval']['approved_sha256'], preview['review_sha256'])
        return result

    def _candidate_resume_step(self, name: str, state: dict[str, Any], preview_path: str, *, key: str = 'resume') -> dict[str, Any]:
        argv = ['resume', '--campaign-dir', str(state['campaign']), '--candidate-id', chain_driver.CANDIDATE,
            '--rerun-failed', '--recovery-preview', preview_path, '--acknowledge-live-requests']
        code, result, stderr = self._candidate_cli(name, state, key, argv)
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result['status']), (2, 'awaiting_receipts'), stderr)
        return result

    def _candidate_timing_recovery_binding(self, preview: dict[str, Any], source: dict[str, Any]) -> None:
        """按原预览冻结状态核计时消费：active 不需解锁；审核／恢复暂停必须绑定实际授权事件。"""

        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        frozen_status = preview['campaign_ledger_head']['status']
        binding = source['timing_recovery_event']
        if frozen_status == 'active':
            self.assertIsNone(binding, '原 active 预览不能补造计时恢复事件')
            return
        self.assertIn(frozen_status, {'recovery_required', 'stage_review_required', 'candidate_review_required'})
        self.assertIsInstance(binding, dict, '原暂停预览的恢复来源必须绑定实际计时授权事件')
        expected_id = f"recovery-authorized-{preview['source_attempt_id']}-{int(preview['index']):02d}"
        self.assertEqual(binding['event_id'], expected_id)
        rows = [(event, raw) for event, raw in timing._load_events(self.ledger_dir) if event['event_id'] == expected_id]
        self.assertEqual(len(rows), 1, '恢复来源必须引用唯一原计时授权事件')
        event, raw = rows[0]
        self.assertEqual((event['event_type'], event['phase']), ('recovery_authorized', 'VC-5'))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), binding['event_sha256'])

    def _candidate_evolution_resume_steps(self, name: str, state: dict[str, Any], first_root: Path,
                                         reconciled: dict[str, Any], audit: ScenarioAudit) -> None:
        """保留原未授权失败，只追加授权、授权重验与后继续跑；各动作仍经原唯一命令停点。"""

        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        campaign = state['campaign']
        first = state['first']
        if state['phase'] == 11:
            # 工具演进确实暂停计时账；原失败 resume 先只读承接，不改原 key、argv 或完整输出。
            # 新世界同样实测这一零副作用拒绝，然后沿已有显式授权入口继续。
            calls_before = self.relay_probe_calls()
            attempts_before = sorted(path.name for path in first_root.parent.iterdir())
            argv = ['resume', '--campaign-dir', str(campaign), '--candidate-id', chain_driver.CANDIDATE,
                '--rerun-failed', '--recovery-preview', reconciled['recovery_preview_path'], '--acknowledge-live-requests']
            code, result, refusal = self._candidate_cli(name, state, 'resume', argv)
            self.assertEqual((code, result), (1, None))
            self.assertIn('UpgradeTimingLedger 为 active（当前 recovery_required）', refusal)
            self.assertEqual(self.relay_probe_calls(), calls_before)
            self.assertEqual(sorted(path.name for path in first_root.parent.iterdir()), attempts_before)
            self._candidate_save(name, state, 12, refused_resume=refusal)
        if state['phase'] == 12:
            argv = ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', first['attempt_id'],
                '--authorize-recovery-preview', reconciled['recovery_preview_path']]
            code, authorized, stderr = self._candidate_cli(name, state, 'authorize', argv)
            self.assertIsNotNone(authorized, stderr)
            self.assertEqual((code, authorized['status']), (0, 'authorized'), stderr)
            self.assertIsNotNone(authorized['timing_recovery_event'])
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)['status'], 'active')
            self._candidate_save(name, state, 13, authorized=authorized, authorized_state=audit.accounting_state())
        if state['phase'] == 13:
            argv = ['reconcile-attempt', '--campaign-dir', str(campaign), '--attempt-id', first['attempt_id'],
                '--authorize-recovery-preview', reconciled['recovery_preview_path']]
            code, repeated, stderr = self._candidate_cli(name, state, 'authorize-repeat', argv)
            self.assertIsNotNone(repeated, stderr)
            self.assertEqual((code, repeated['status']), (0, 'authorized'), stderr)
            self.assertEqual({key: repeated['timing_recovery_event'][key] for key in ('event_id', 'event_sha256')},
                {key: state['authorized']['timing_recovery_event'][key] for key in ('event_id', 'event_sha256')})
            self._candidate_save(name, state, 14, repeated_authorization_state=audit.accounting_state())
        if state['phase'] == 14:
            resumed = self._candidate_resume_step(name, state, reconciled['recovery_preview_path'], key='resume-authorized')
            self._candidate_save(name, state, 15, resumed=resumed)

    def _audit(self, scenario: str, campaign: Path, trusted: Iterable[Path]) -> ScenarioAudit:
        from tools.official_client_capture import claude_upgrade_store
        audit = ScenarioAudit(self, scenario)
        audit.snapshot(trusted=[campaign / 'campaign.json', campaign / 'official/result.json', *trusted],
            history=[claude_upgrade_store.campaign_store_root(campaign), self.ledger_dir / 'events', self.ledger_dir / 'ledger.json',
                self.project_root / 'events', self.project_root / 'plan.json', campaign / 'control/reconciliation',
                campaign / 'control/tool-evolution'], timing_dir=self.ledger_dir, project_root=self.project_root)
        return audit

    @_resumable_method
    def _scenario_candidate_reconcile_resume(self) -> None:
        """原失败候选attempt→两次对账→未批准拒绝→两次批准→只重跑失败作业。"""
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_recovery as recovery
        if self.recorded_entry('reconcile-resume'):
            return
        name, scenario = 'candidate-reconcile', '候选真实对账链端到端'
        state = self._candidate_start(name, scenario)
        campaign = state['campaign']
        if state['phase'] == 0:
            first = self._candidate_run_step(name, state, fail_refresh=True)
            self._candidate_save(name, state, 1, first=first)
        first = state['first']
        first_root, _attempt = self.candidate_attempt(campaign, first['attempt_id'])
        roots = self.candidate_job_roots(campaign)
        audit = self._audit(scenario, campaign, [first_root, roots[chain_driver.LIVE_JOB]])
        if state['phase'] == 1:
            self._candidate_save(name, state, 2, calls=self.relay_probe_calls())
        if state['phase'] == 2:
            reconciled = self._candidate_reconcile_step(name, state, 'reconcile-first', first['attempt_id'])
            preview = reconciled['recovery_preview']
            self.assertEqual((preview['phase'], preview['candidate_id']), ('candidate', chain_driver.CANDIDATE))
            self.assertEqual((preview['execute_job_ids'], preview['reuse_job_ids']), ([chain_driver.REFRESH_JOB], [chain_driver.LIVE_JOB]))
            self.assertEqual(reconciled['resume_reuse_check']['status'], 'consistent')
            self._candidate_save(name, state, 3, reconciled=reconciled, first_state=audit.accounting_state(),
                receipt_bytes=file_digests(campaign / 'control/reconciliation'))
        reconciled = state['reconciled']
        preview = reconciled['recovery_preview']
        if state['phase'] == 3:
            repeated = self._candidate_reconcile_step(name, state, 'reconcile-repeat', first['attempt_id'])
            self.assertEqual(repeated['recovery_preview']['review_sha256'], preview['review_sha256'])
            for path, digest in state['receipt_bytes'].items():
                self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
            self._candidate_save(name, state, 4, second_state=audit.accounting_state(),
                attempts_before=sorted(path.name for path in first_root.parent.iterdir()))
        if state['phase'] == 4:
            argv = ['resume', '--campaign-dir', str(campaign), '--candidate-id', chain_driver.CANDIDATE,
                '--rerun-failed', '--recovery-preview', reconciled['recovery_preview_path'], '--acknowledge-live-requests']
            code, _result, refusal = self._candidate_cli(name, state, 'unapproved-refusal', argv)
            self.assertEqual(code, 1)
            self.assertIn('尚未批准', refusal)
            self.assertEqual(sorted(path.name for path in first_root.parent.iterdir()), state['attempts_before'])
            self.assertEqual(self.relay_probe_calls(), state['calls'])
            self._candidate_save(name, state, 5)
        if state['phase'] == 5:
            approved = self._candidate_reconcile_step(name, state, 'approve-first', first['attempt_id'], preview)
            self._candidate_save(name, state, 6, approved=approved, approved_state=audit.accounting_state())
        if state['phase'] == 6:
            again = self._candidate_reconcile_step(name, state, 'approve-repeat', first['attempt_id'], preview)
            self.assertEqual(again['recovery_approval'], state['approved']['recovery_approval'])
            self._candidate_save(name, state, 7, repeated_approval_state=audit.accounting_state())
        if state['phase'] == 7:
            resumed = self._candidate_resume_step(name, state, reconciled['recovery_preview_path'])
            self._candidate_save(name, state, 8, resumed=resumed)
        if state['phase'] == 8:
            code, status, stderr = self._candidate_cli(name, state, 'status',
                ['status', '--campaign-dir', str(campaign), '--candidate-id', chain_driver.CANDIDATE])
            self.assertEqual(code, 0, stderr)
            self._candidate_save(name, state, 9, status=status)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        if stops.load_state('audit-complete:' + scenario) is not None:
            self.recorded_audit_report(ScenarioAudit.revalidate(self, scenario))
            return
        resumed, approved, calls = state['resumed'], state['approved'], state['calls']
        self.assertEqual(self.relay_probe_calls()[len(calls):], [chain_driver.REFRESH_PROBE])
        attempt_root, attempt = self.candidate_attempt(campaign, resumed['attempt_id'])
        with self.source_context():
            source = recovery.load_recovery_source(campaign, attempt_root)
            status = claude_upgrade.campaign_status(campaign, chain_driver.CANDIDATE)
        self.assertEqual((status['status'], status['next_command']),
            (state['status']['status'], state['status']['next_command']), '状态原stdout与当前只读操作入口不一致')
        self.assertEqual((source['source_attempt_id'], source['execute_job_ids']), (first['attempt_id'], [chain_driver.REFRESH_JOB]))
        self.assertEqual(source['recovery_approval'], {'index': approved['recovery_approval']['index'],
            'receipt_sha256': approved['recovery_approval']['receipt_sha256']})
        self._candidate_timing_recovery_binding(preview, source)
        self.assertEqual(status['status'], 'candidate_awaiting_client_checkpoint')
        self.assertTrue(all(row['status'] == 'complete' for row in attempt['results']))
        counted = recovery_tests._read(campaign / reconciled['provenance_receipt']['path'])['precise_total']
        self.assertGreater(counted, 0, '原录制字节必须真实进入核算')
        audit.check_trusted()
        audit.check_closure(executed=[row['id'] for row in attempt['results'] if row['disposition'] == 'executed'],
            expected=[chain_driver.REFRESH_JOB], reused=[row['id'] for row in attempt['results'] if row['disposition'] == 'reused'],
            expected_reused=[chain_driver.LIVE_JOB], what='真实批准消费后的候选执行',
            source=audit.file_closure_source(attempt_root / 'attempt.json', ('results',), ('results',), row_key='id', disposition='disposition'))
        audit.check_accounting(precise_delta=counted, root_causes=1, repeats=[('重复候选对账', state['first_state'], state['second_state']),
            ('重复候选恢复批准', state['approved_state'], state['repeated_approval_state'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry({'reconcile': reconciled['next_command'], 'status': state['status']['next_command']}, 'resume', 'client-evidence')
        record = audit.report(candidate_id=chain_driver.CANDIDATE, source_attempt=first['attempt_id'], resumed_attempt=resumed['attempt_id'],
            counted_requests=counted, input_source=self.assert_recorded_sources_untouched())
        self.recorded_audit_report(record)

    @_resumable_method
    def _scenario_candidate_tool_evolution(self) -> None:
        """原隔离脚本修复、只读演进预览、两次登记、真实对账批准与候选闭集续跑。"""
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_recovery as recovery
        from tools.official_client_capture import claude_upgrade_store
        from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_control import receipts as store_receipts
        if self.recorded_entry('tool-evolution'):
            return
        name, scenario = 'candidate-evolution', '有候选 attempt 的整条工具演进预览'
        state = self._candidate_start(name, scenario)
        campaign = state['campaign']
        if state['phase'] == 0:
            first = self._candidate_run_step(name, state, fail_refresh=False)
            self._candidate_save(name, state, 1, first=first)
        first = state['first']
        first_root, _attempt = self.candidate_attempt(campaign, first['attempt_id'])
        audit = self._audit(scenario, campaign, [first_root])
        path = Path(claude_upgrade.__file__).resolve().parent / chain_driver.EVOLVED_DEPENDENCY
        key = f'official_client_capture/{chain_driver.EVOLVED_DEPENDENCY}'
        if state['phase'] == 1:
            self.assertEqual(Path(os.environ.get(shell.TREE_ENV, '/missing')).resolve(), REPO_ROOT.resolve())
            shell._assert_isolation(self, shell._ChainRecorder('candidate-evolution-input'))
            original = path.read_bytes()
            changed = original + '\n# B5b-03：候选采集脚本修复后的身份来源。\n'.encode('utf-8')
            self._candidate_save(name, state, 2, calls=self.relay_probe_calls(), source_path=path,
                source_before=original, source_after=changed)
        self.assertEqual(path, state['source_path'])
        if stops.record_root() is None:
            self.addCleanup(path.write_bytes, state['source_before'])
            self.addCleanup(self.tree.restore)
        if state['phase'] == 2:
            if path.read_bytes() == state['source_before']:
                path.write_bytes(state['source_after'])
            elif path.read_bytes() != state['source_after']:
                raise ValueError('候选隔离源码不是原字节或冻结的本次修复，禁止覆盖')
            self.tree.write(key, state['source_after'])
            evolved = claude_upgrade._tool_identity(include_git=False)
            self.assertNotEqual(evolved['wire_producer_sha256'], self.tool_identity['wire_producer_sha256'])
            self._candidate_save(name, state, 3, evolved=evolved, source_binding=stops.file_cut(path))
        stops.read_cut(state['source_binding'])
        if state['phase'] == 3:
            deploy = self.data / 'control/claude-managed-deploy-20261008t020000z.json'
            if deploy.exists() or deploy.is_symlink():
                raise ValueError('候选演进部署的原目标已经存在，禁止建立新的写入意图')
            created_at = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            with tempfile.TemporaryDirectory(prefix='cf-candidate-deploy-intent-') as directory:
                planned = ledger_fixture.write_claude_deploy_receipt(Path(directory), state['evolved'],
                    stamp='20261008t020000z', created_at_utc=created_at)
                deploy_raw = planned.read_bytes()
            self._candidate_save(name, state, 4, deploy_created_at=created_at, deploy_raw=deploy_raw,
                deploy_path=deploy, deploy_absent_before=True)
        if state['phase'] == 4:
            if ('deploy_raw' not in state or 'deploy_created_at' not in state
                    or state.get('deploy_absent_before') is not True):
                raise ValueError('候选演进部署缺同次写入意图，禁止重签部署或重建现场')
            deploy = self.data / 'control/claude-managed-deploy-20261008t020000z.json'
            if deploy != state.get('deploy_path'):
                raise ValueError('候选演进部署原目标发生变化，禁止覆盖')
            if deploy.exists() or deploy.is_symlink():
                if deploy.is_symlink() or not deploy.is_file() or deploy.read_bytes() != state['deploy_raw']:
                    raise ValueError('候选原部署收据与冻结的同次意图不一致，禁止覆盖')
            else:
                deploy = ledger_fixture.write_claude_deploy_receipt(self.data, state['evolved'],
                    stamp='20261008t020000z', created_at_utc=state['deploy_created_at'])
                self.assertEqual(deploy.read_bytes(), state['deploy_raw'], '原部署 helper 输出与冻结意图不同')
            policy_certification.load_deployment_receipt(deploy, client='claude', expected_identity={
                **{field: state['evolved'][field] for field in ('policy_version', 'policy_sha256',
                    'wire_producer_sha256', 'evidence_semantics_sha256', 'control_sha256')},
                'tool_files_sha256': state['evolved']['files_sha256']})
            self._candidate_save(name, state, 5, deployment_binding=stops.file_cut(deploy))
        stops.read_cut(state['deployment_binding'])
        argv = ['tool-evolution', '--campaign-dir', str(campaign), '--fix-commit', 'b' * 40, '--reason', '修复候选采集脚本']
        if state['phase'] == 5:
            before = file_digests(campaign, self.ledger_dir, self.project_root)
            code, preview, stderr = self._candidate_cli(name, state, 'evolution-preview', argv)
            self.assertEqual((code, preview['status']), (0, 'approval_required'), stderr)
            self.assertEqual(file_digests(campaign, self.ledger_dir, self.project_root), before, '演进预览必须只读')
            self.assertEqual(preview['changes']['impact_paths'], [key])
            self.assertEqual(preview['changes']['unmapped_paths'], [])
            self.assertFalse(preview['changes']['wire_closure_changed'])
            self.assertEqual((preview['impact']['official']['sealed'], preview['impact']['official']['affected_job_ids']), (True, []))
            self.assertEqual(set(preview['impact']['candidates']), {chain_driver.CANDIDATE})
            impact = preview['impact']['candidates'][chain_driver.CANDIDATE]
            expected = sorted([chain_driver.LIVE_JOB, chain_driver.REFRESH_JOB])
            self.assertEqual((impact['planned_job_ids'], impact['affected_job_ids'], impact['sealed']), (expected, expected, False))
            self.assertEqual(preview['evaluator']['candidates'][chain_driver.CANDIDATE]['current_evaluation_baseline'], 0)
            self._candidate_save(name, state, 6, preview=preview, expected=expected)
        preview, expected = state['preview'], state['expected']
        approved_argv = [*argv, '--approve-sha256', preview['review_sha256'], '--approved-by', '故障恢复链']
        if state['phase'] == 6:
            code, applied, stderr = self._candidate_cli(name, state, 'evolution-apply', approved_argv)
            self.assertEqual((code, applied['status'], applied['store_registration']['status']), (0, 'evolution_applied', 'registered'), stderr)
            self._candidate_save(name, state, 7, first_state=audit.accounting_state())
        if state['phase'] == 7:
            code, again, stderr = self._candidate_cli(name, state, 'evolution-repeat', approved_argv)
            self.assertEqual((code, again['reused'], again['store_registration']['status']), (0, True, 'current'), stderr)
            self._candidate_save(name, state, 8, second_state=audit.accounting_state())
        if state['phase'] == 8:
            code, status, stderr = self._candidate_cli(name, state, 'status',
                ['status', '--campaign-dir', str(campaign), '--candidate-id', chain_driver.CANDIDATE])
            self.assertEqual(code, 0, stderr)
            self.assertEqual(status['candidate_attempt']['evolution_invalidated_job_ids'], expected)
            self.assertEqual(self.relay_probe_calls(), state['calls'], '演进预览与登记不执行probe')
            self._candidate_save(name, state, 9, status=status)
        if state['phase'] == 9:
            reconciled = self._candidate_reconcile_step(name, state, 'reconcile', first['attempt_id'])
            self.assertEqual((reconciled['recovery_preview']['execute_job_ids'], reconciled['recovery_preview']['reuse_job_ids']), (expected, []))
            self._candidate_save(name, state, 10, reconciled=reconciled)
        reconciled = state['reconciled']
        if state['phase'] == 10:
            self._candidate_reconcile_step(name, state, 'approve', first['attempt_id'], reconciled['recovery_preview'])
            self._candidate_save(name, state, 11)
        self._candidate_evolution_resume_steps(name, state, first_root, reconciled, audit)
        self.assertEqual(tuple(row['key'] for row in state['completed_commands']), self.COMMAND_KEYS[name])
        if stops.load_state('audit-complete:' + scenario) is not None:
            self.recorded_audit_report(ScenarioAudit.revalidate(self, scenario))
            return
        resumed = state['resumed']
        attempt_root, replayed = self.candidate_attempt(campaign, resumed['attempt_id'])
        with self.source_context():
            source = recovery.load_recovery_source(campaign, attempt_root)
        self._candidate_timing_recovery_binding(reconciled['recovery_preview'], source)
        self.assertEqual(self.relay_probe_calls()[len(state['calls']):], state['calls'], '共同依赖变化后恰好重跑全部probe一次')
        self.assertEqual(recovery_tests._read(attempt_root / 'reservation.json')['tool_evolution']['index'], 1)
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign))
        self.assertEqual(len(store_receipts.tool_evolutions(store, dict(store_campaign))), 1)
        counted = recovery_tests._read(campaign / reconciled['provenance_receipt']['path'])['precise_total']
        self.assertGreater(counted, 0)
        audit.check_trusted()
        audit.check_closure(executed=[row['id'] for row in replayed['results'] if row['disposition'] == 'executed'], expected=expected,
            reused=[row['id'] for row in replayed['results'] if row['disposition'] == 'reused'], what='完整演进判定后的候选闭集',
            source=audit.file_closure_source(attempt_root / 'attempt.json', ('results',), ('results',), row_key='id', disposition='disposition'))
        audit.check_accounting(precise_delta=counted, root_causes=0, repeats=[('重复候选工具演进批准', state['first_state'], state['second_state']),
            ('重复候选计时恢复授权', state['authorized_state'], state['repeated_authorization_state'])])
        audit.check_elapsed()
        audit.check_history()
        audit.check_entry(state['status']['next_command'], 'reconcile-attempt')
        record = audit.report(candidate_id=chain_driver.CANDIDATE, invalidated=expected, resumed_attempt=resumed['attempt_id'],
            counted_requests=counted, input_source=self.assert_recorded_sources_untouched())
        self.recorded_audit_report(record)


class _DeployFaultScenarios(unittest.TestCase):
    """受监督部署在各故障点被杀（``fork`` 出的子进程在注入点 ``os._exit``，不执行任何 except／finally，现场就是进程被杀的
    样子）→ 下次启动的部署先按日志恢复：收据正文未入日志的残留按内容回滚到部署前、拒绝本次部署并提示重新执行；收据正文已入
    日志的残留补齐收据后照常部署 → 再部署成功。部署不碰两本账：数据根旁另建 Claude 项目总账与计时账本，核对它们逐字节不变。"""


    def setUp(self) -> None:
        from tools import arm64_supervised_deploy as deploy
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
        from tools.official_client_capture.tests.arm64_deploy_fixture import DeployHarness

        self.root, restoring = stops.fixture_directory(self, 'deploy')
        self.deploy = deploy
        self.capture_root, self.control_root = deploy.CAPTURE_ROOT, deploy.CONTROL_ROOT
        self.BOTH = ('--managed-roots', deploy.CAPTURE_ROOT, deploy.CONTROL_ROOT, '--client', 'codex', 'claude')
        self.harness = DeployHarness(self.root / 'site')
        if restoring:
            values = stops.load_state('deploy-fixture')
            if values is None or not stops.restore_case(self):
                raise ValueError('原部署世界缺夹具与账本坐标，禁止重建')
            self.timing_dir = values['timing_dir']
            return
        ledgers = self.root / 'ledgers'
        ledgers.mkdir(mode=0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(ledgers)
        self.timing_dir = ledgers / 'timing'
        timing.create_ledger(self.timing_dir, upgrade_id='claude-deploy-fault-ledger', baseline_version='2.1.226',
            target_version='2.1.285', campaign_purpose='validation_only', evidence_decision='recapture', client='claude')
        stops.save_case(self)
        stops.save_state('deploy-fixture', {'timing_dir': self.timing_dir})

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """原六部署方法纯验当前读者：实读原日志／意图／收据与生产字节。"""
        from tools import arm64_supervised_deploy as deploy
        from tools.official_client_capture.tests.arm64_deploy_fixture import DeployHarness
        allowed = {name: scenario for _step, group in RECOVERY_CHAIN_GROUPS
            for name, scenario in group.items() if name.startswith('_DeployFaultScenarios.')}
        method = type(self).__name__ + '.' + self._testMethodName
        if method not in allowed or expected != [allowed[method]] or not stops.restore_case(self):
            raise ValueError('部署纯验仅接受六个原成功方法及准确单场景')
        values = stops.load_state('deploy-fixture')
        if values is None:
            raise ValueError('部署纯验缺原账本定位，不能新建')
        self.deploy, self.timing_dir = deploy, values['timing_dir']
        self.harness = DeployHarness(self.root / 'site', module=deploy)
        scenario = expected[0]
        receipts_residue = self._testMethodName in {'_scenario_deploy_one_receipt_written', '_scenario_deploy_receipts_written'}
        name = ('deploy-receipts:' if receipts_residue else 'deploy-rollback:') + scenario
        state = stops.load_state('cursor:' + name)
        if state is None or state.get('phase') != (7 if receipts_residue else 9):
            raise ValueError('部署纯验缺原完成游标，禁止运行恢复事务')
        if self.harness.journals():
            raise ValueError('部署纯验仍有活动交换日志，不允许补恢复')
        for path in self.harness.retired_journals():
            deploy.SwitchJournal.load(path)
        key = 'complete-receipts' if receipts_residue else 'redeploy'
        saved = stops.load_state('deploy-intent:' + name + ':' + key)
        if saved is None:
            raise ValueError('部署纯验缺同次部署完整意图原件')
        journal = deploy.SwitchJournal.load(Path(saved['binding']['path']))
        stops._read_exact_cut(saved['binding'])
        deploy.verify_switched_entries(journal)
        for client, relative in journal.intent['receipt_paths'].items():
            path = Path(relative)
            if path.is_symlink() or not path.is_file() or path not in self.harness.receipts(client):
                raise ValueError('部署纯验缺同次客户端准确收据')
            receipt = json.loads(path.read_bytes())
            if receipt.get('status') != 'passed':
                raise ValueError('部署纯验实际收据不是原成功结果')
            if client == 'claude' and (receipt.get('client') != client
                    or receipt.get('switch_journal_sha256') != journal.intent_sha256):
                raise ValueError('Claude部署收据不是该原交换意图')
        return [ScenarioAudit.revalidate(self, scenario)]

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

    def _deploy_cursor(self, name: str, scenario: str) -> dict[str, Any]:
        state = stops.resume_checkpoint(self, name)
        if state is None:
            if os.environ.get(stops.METHOD_RESUME_ENV):
                raise ValueError('原部署方法缺停点，禁止重建或再注入故障')
            state = {'phase': 0, 'audit_snapshots': {scenario: 'uninitialized'}}
            stops.checkpoint(self, name, **state)
        for key, value in state.get('harness', {}).items():
            setattr(self.harness, key, value)
        return state

    def _deploy_save(self, name: str, state: dict[str, Any], phase: int, **values: Any) -> None:
        state.update(values, phase=phase, harness={key: getattr(self.harness, key)
            for key in ('stamp', 'events', 'exchange_calls', 'last_output')})
        stops.checkpoint(self, name, **state)

    def _deploy_run(self, name: str, key: str) -> tuple[int, Any, str]:
        """保存原 main 完整输出及同次部署意图原字节；续作不再次跑部署事务。"""

        argv = self.harness.argv(*self.BOTH)
        def invoke() -> tuple[int, Any, str]:
            pending = stops.begin_command(argv)
            create = self.deploy.SwitchJournal.create
            def capture_intent(*args: Any, **options: Any) -> Any:
                journal = create(*args, **options)
                if stops.record_root() is not None:
                    directory = Path(tempfile.mkdtemp(prefix='deploy-intent-', dir=stops.record_root()))
                    copy = directory / 'original.journal'
                    copy.write_bytes(journal.path.read_bytes())
                    copy.chmod(0o400)
                    stops.save_state('deploy-intent:' + name + ':' + key, {'binding': stops.file_cut(copy)})
                return journal
            code, result, stderr = 0, None, ''
            with mock.patch.object(self.deploy.SwitchJournal, 'create', side_effect=capture_intent):
                try:
                    code = self.harness.run(*self.BOTH)
                except self.deploy.DeploymentError as error:
                    code, stderr = 1, str(error)
            stdout = self.harness.last_output
            if stdout.strip():
                result = json.loads(stdout)
            stops.record_command(argv, code, stdout, stderr, pending=pending)
            return code, result, stderr
        return stops.command_step(self, name, key, argv, invoke)

    def _deploy_relocations(self, name: str, key: str) -> dict[str, str]:
        saved = stops.load_state('deploy-intent:' + name + ':' + key)
        if saved is None:
            if stops.record_root() is not None:
                raise ValueError('原成功部署缺同次意图原件，不能猜备份坐标')
            return {}
        lines = stops.read_cut(saved['binding']).decode('utf-8').splitlines()
        intent = json.loads(lines[0])['intent']
        return {entry['production_path']: entry['backup_path']
                for group in ('roots', 'preparers', 'documents') for entry in intent[group] if entry['old_exists']}

    def _rolled_back(self, scenario: str, kill: Callable[[Any], contextlib.AbstractContextManager[Any]], *, control_root_old: bool | None,
                     expected_restored: list[str], recovery_kill: Callable[[Any], contextlib.AbstractContextManager[Any]] | None = None) -> None:
        """沿原日志回滚后只续作部署；原故障注入与重复恢复各有独立停点。"""

        name = 'deploy-rollback:' + scenario
        state = self._deploy_cursor(name, scenario)
        harness = self.harness
        if state['phase'] == 0:
            self._deploy_save(name, state, 1)
            harness.build(control_root_old=control_root_old)
            self._deploy_save(name, state, 2, before=harness.snapshot())
        if state['phase'] == 1:
            raise ValueError('原部署夹具首次建场未闭合，禁止清空原世界再建')
        audit = self._audit(scenario, self._production_paths())
        if state['phase'] == 2:
            self._deploy_save(name, state, 3)
            self._kill_at(kill)
            self.assertEqual(len(harness.journals()), 1)
            self._deploy_save(name, state, 4)
        if state['phase'] == 3:
            raise ValueError('原部署故障注入缺实际退出停点，检查原日志；禁止再次注入')
        if state['phase'] == 4:
            if recovery_kill is not None:
                self._deploy_save(name, state, 5)
                self._kill_at(recovery_kill)
            self._deploy_save(name, state, 6)
        if state['phase'] == 5:
            raise ValueError('原回滚故障注入缺实际退出停点，禁止再次交换')
        if state['phase'] == 6:
            code, _result, refused = self._deploy_run(name, 'recover-refused')
            self.assertEqual(code, 1)
            (retired,) = harness.retired_journals()
            records = [json.loads(line) for line in retired.read_text(encoding='utf-8').splitlines() if line.strip()]
            done = [record for record in records if record.get('marker') == 'rollback_done']
            self.assertTrue(done)
            restored = sorted(item['object'] for item in done[-1]['restored'])
            self.assertEqual(harness.snapshot(), state['before'], '恢复后全部受管对象回到部署前')
            if stops.record_root() is None:
                audit.check_trusted()
            self._deploy_save(name, state, 7, refused=refused, retired=retired, restored=restored,
                repeat_before=audit.accounting_state())
        if state['phase'] == 7:
            harness.stamp = '20261008T010000Z'
            code, result, stderr = self._deploy_run(name, 'redeploy')
            self.assertEqual(code, 0, stderr)
            self._deploy_save(name, state, 8, result=result, repeat_after=audit.accounting_state())
        self.assertEqual((harness.journals(), len(harness.retired_journals())), ([], 1))
        self.assertEqual((len(harness.receipts('codex')), len(harness.receipts('claude'))), (1, 1))
        self.assertEqual(harness.tree(self.control_root), harness.staging_tree(self.control_root))
        relocated = self._deploy_relocations(name, 'redeploy')
        # 记录模式重验同次意图中旧副本；普通测试保留回滚完成边界的原断言。
        if stops.record_root() is not None:
            audit.check_trusted(relocated=relocated)
        audit.check_closure(executed=state['restored'], expected=expected_restored, what='启动恢复按内容换回的受管根',
            source=audit.journal_closure_source(state['retired'], 'rollback_done', 'restored'))
        audit.check_accounting(repeats=[('再部署', state['repeat_before'], state['repeat_after'])])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(state['refused'], '已按内容恢复到部署前', '重新执行')
        audit.report(retired_journal=state['retired'].name)
        self._deploy_save(name, state, 9)

    def _receipts_residue(self, scenario: str, kill: Callable[[Any], contextlib.AbstractContextManager[Any]], *, expected_written: int) -> None:
        """原收据残留只补齐一次；每次幂等恢复仍保留独立实际调用与原账本切点。"""

        name = 'deploy-receipts:' + scenario
        state = self._deploy_cursor(name, scenario)
        harness = self.harness
        if state['phase'] == 0:
            self._deploy_save(name, state, 1)
            harness.build(control_root_old=True)
            self._deploy_save(name, state, 2)
        if state['phase'] == 1:
            raise ValueError('原收据残留夹具建场未闭合，禁止重建')
        if state['phase'] == 2:
            self._deploy_save(name, state, 3)
            self._kill_at(kill)
            (journal,) = harness.journals()
            prepared = {item['client']: item for item in self.deploy.SwitchJournal.load(journal).receipts or []}
            written = sorted(path.name for path in [*harness.receipts('codex'), *harness.receipts('claude')])
            self._deploy_save(name, state, 4, prepared=prepared, written=written)
        if state['phase'] == 3:
            raise ValueError('原收据故障注入缺实际退出停点，禁止重新部署')
        audit = self._audit(scenario, [*self._production_paths(), *harness.receipts('codex'), *harness.receipts('claude')])
        if state['phase'] == 4:
            harness.stamp = '20261008T020000Z'
            code, result, stderr = self._deploy_run(name, 'complete-receipts')
            self.assertEqual(code, 0, stderr)
            recovered = result['recovered_switch_journal']
            self.assertEqual(recovered['outcome'], 'receipts-completed')
            missing = sorted(Path(item['path']).name for item in state['prepared'].values() if Path(item['path']).name not in state['written'])
            self.assertEqual(len(missing), expected_written)
            self._deploy_save(name, state, 5, recovered=recovered, missing=missing, repeat_before=audit.accounting_state())
        if state['phase'] == 5:
            argv = ['recover_switch_journal', str(harness.control)]
            def invoke() -> tuple[int, Any, str]:
                pending = stops.begin_command(argv)
                result = self.deploy.recover_switch_journal(harness.control)
                stops.record_command(argv, 0, json.dumps(result), '', pending=pending)
                return 0, result, ''
            _code, result, _stderr = stops.command_step(self, name, 'recovery-repeat', argv, invoke)
            self.assertEqual(result['outcome'], 'none')
            self._deploy_save(name, state, 6, repeat_after=audit.accounting_state())
        audit.check_trusted()
        recovered = state['recovered']
        audit.check_closure(executed=recovered['written_receipts'], expected=state['missing'],
            reused=recovered['verified_receipts'], expected_reused=state['written'], what='按日志正文补齐的收据',
            source=audit.closure_source(recovered, ('written_receipts',), ('verified_receipts',)))
        audit.check_accounting(repeats=[('再次启动恢复', state['repeat_before'], state['repeat_after'])])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(recovered, 'receipts-completed')
        audit.report(written=recovered['written_receipts'], verified=recovered['verified_receipts'])
        self._deploy_save(name, state, 7)

    # ---- 各故障点（设计 B5a-01 ②～⑦）

    @_resumable_method
    def _scenario_deploy_after_exchange(self) -> None:
        self._rolled_back("多根部署故障点：首根交换完成后被杀", self._hook("after_exchange", lambda h: h.production(self.capture_root)),
                          control_root_old=True, expected_restored=[self.capture_root])

    @_resumable_method
    def _scenario_deploy_after_backup(self) -> None:
        self._rolled_back("多根部署故障点：旧树已入备份、第二根交换前被杀",
                          self._hook("before_exchange", lambda h: h.production(self.control_root)),
                          control_root_old=True, expected_restored=[self.capture_root])

    @_resumable_method
    def _scenario_deploy_after_new_root(self) -> None:
        self._rolled_back("多根部署故障点：首次新增的根切入后被杀",
                          self._hook("after_rename_noreplace", lambda h: h.production(self.control_root)),
                          control_root_old=None, expected_restored=[self.capture_root, self.control_root])

    @_resumable_method
    def _scenario_deploy_interrupted_rollback(self) -> None:
        deploy = self.deploy
        self._rolled_back(
            "多根部署故障点：恢复回滚中再被杀",
            self._hook("before_exchange", lambda h: h.production(self.control_root)),
            control_root_old=True, expected_restored=[],
            recovery_kill=self._patch_kill(deploy.SwitchJournal, "append", when=lambda _journal, marker, **_fields: marker == "rollback_done"),
        )

    @_resumable_method
    def _scenario_deploy_one_receipt_written(self) -> None:
        deploy = self.deploy
        written: list[Any] = []

        def second_write(path: Path, _payload: Any) -> bool:
            written.append(path)
            return len(written) == 2

        self._receipts_residue("多根部署故障点：只写出一份收据时被杀", self._patch_kill(deploy, "write_json_once", when=second_write),
                               expected_written=1)

    @_resumable_method
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

    @classmethod
    def setUpClass(cls) -> None:
        """记录模式重绑原受管树；原树未建完或修改前字节缺失时拒绝重新复制。"""

        cls._recorded_tree = stops.record_root() is not None
        if not cls._recorded_tree:
            super().setUpClass()
            return
        root, restoring = stops.fixture_directory(cls, 'predecessor-tree')
        saved = stops.load_state('predecessor-tree-fixture')
        if restoring:
            if saved is None or saved.get('status') != 'complete' or saved.get('root') != root:
                raise ValueError('原前序受管树初始化未闭合，禁止复制新树')
            cls.tree = predecessor_tests.ManagedToolTree(root / 'repo', existing=True)
            current = sorted(path for path in cls.tree.root.rglob('*') if path.is_file())
            if current != [binding['path'] for binding in saved['bindings']]:
                raise ValueError('原前序受管树文件集合已改变')
            for binding in saved['bindings']:
                stops.read_cut(binding)
            return
        stops.save_state('predecessor-tree-fixture', {'status': 'pending', 'root': root})
        cls.tree = predecessor_tests.ManagedToolTree(root / 'repo')
        bindings = [stops.file_cut(path) for path in sorted(cls.tree.root.rglob('*')) if path.is_file()]
        stops.save_state('predecessor-tree-fixture', {'status': 'complete', 'root': root, 'bindings': bindings})

    @classmethod
    def tearDownClass(cls) -> None:
        if not cls._recorded_tree:
            super().tearDownClass()

    def setUp(self) -> None:
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture

        if stops.record_root() is None:
            super().setUp()
            restoring = False
        else:
            self.workspace, restoring = stops.fixture_directory(self, 'predecessor-workspace')
            active = self.tree.active()
            active.__enter__()
            self.addCleanup(active.__exit__, None, None, None)
            if restoring:
                saved = stops.load_state('predecessor-fixture')
                workspace = self.workspace
                if saved is None or saved.get('status') != 'complete' or not stops.restore_case(self):
                    raise ValueError('原前序工作区或两账未闭合，禁止重新建账')
                if self.workspace != workspace or saved['workspace'] != workspace:
                    raise ValueError('原前序工作区坐标已改变')
                self.timing_dir = saved['timing_dir']
                self.project_root = saved['project_root']
                from tools.official_client_capture import codex_upgrade_project_ledger as project
                project.read_project_history_snapshot(self.project_root)
                timing.inspect_ledger(self.timing_dir, project_ledger_optional=True)
                return
            self._locked = []
            stops.save_state('predecessor-fixture', {'status': 'pending', 'workspace': self.workspace})
        ledgers = self.workspace / "ledgers"
        ledgers.mkdir(mode=0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(ledgers)
        self.timing_dir = ledgers / "timing"
        timing.create_ledger(self.timing_dir, upgrade_id="claude-predecessor-misuse-ledger", baseline_version="2.1.226",
                             target_version="2.1.285", campaign_purpose="validation_only", evidence_decision="recapture", client="claude")
        stops.save_case(self)
        stops.save_state('predecessor-fixture', {'status': 'complete', 'workspace': self.workspace,
            'timing_dir': self.timing_dir, 'project_root': self.project_root})

    @staticmethod
    def _fixture_values(fixture: predecessor_tests.SyntheticCampaign) -> dict[str, Any]:
        """只保存原证据写入所用值；Store、时钟对象与 SyntheticCampaign 实例不序列化。"""

        return {**{key: getattr(fixture, key) for key in ('root', 'external_root', 'store_root', 'schema',
            'campaign_id', 'tool_sha256', 'official_binding')}, 'clock': fixture._clock.isoformat()}

    @staticmethod
    def _restore_fixture(values: dict[str, Any]) -> predecessor_tests.SyntheticCampaign:
        """跳过会初始化 Store 和覆盖外部原件的构造器，重绑原 _time 所需时钟值。"""

        required = {'root', 'external_root', 'store_root', 'schema', 'campaign_id', 'tool_sha256',
                    'official_binding', 'clock'}
        if set(values) != required:
            raise ValueError('原 SyntheticCampaign 明示值字段不完整')
        fixture = object.__new__(predecessor_tests.SyntheticCampaign)
        for key, value in values.items():
            if key != 'clock':
                setattr(fixture, key, value)
        fixture._clock = datetime.fromisoformat(values['clock'])
        return fixture

    def _verify_predecessor(self, state: dict[str, Any]) -> None:
        """最小合成总计划没有正式 VC 控制；用其原 Store 与外部原件读侧核验，不冒充正式 Campaign。"""

        from tools.official_client_capture import claude_upgrade_store as store_module
        from tools.official_client_control.store import ControlStore

        for binding in state['campaign_bindings']:
            stops.read_cut(binding)
        source = ControlStore.open_read_only(state['predecessor_root'])
        source.replay(external_root=state['predecessor_external'], require_external=True)
        target = ControlStore.open_read_only(store_module.campaign_store_root(state['campaign_dir']))
        fixture = self._restore_fixture(state['target_fixture'])
        target.replay(external_root=fixture.external_root, require_external=True)
        campaign = target.load_campaign(predecessor_tests.TARGET_ID)
        self.assertEqual(campaign['tool_identity']['tool_files_sha256'], fixture.tool_sha256)
        self.assertEqual(campaign['official_artifacts'], [fixture.official_binding])

    def _predecessor_start(self, name: str, scenario: str, *, amd64: bool = False) -> dict[str, Any]:
        state = stops.resume_checkpoint(self, name)
        if state is not None:
            if state.get('phase') == 'initializing':
                raise ValueError('原前序 Campaign 首次构造未闭合，保留现场并禁止重建')
            self._verify_predecessor(state)
            return state
        if os.environ.get(stops.METHOD_RESUME_ENV):
            raise ValueError('原前序场景缺游标，禁止重新初始化')
        stops.checkpoint(self, name, phase='initializing', audit_snapshots={scenario: 'uninitialized'})
        from tools.official_client_control.store import ControlStore

        predecessor = (self.make_predecessor('amd64-predecessor', schema='v2',
            **{**predecessor_tests.SYNTHETIC_TARGET, 'platforms': ['linux/amd64']}) if amd64
            else self.make_predecessor())
        campaign_dir, fixture = self.make_target('amd64' if amd64 else 'target',
            predecessor=self.binding(ControlStore.open_read_only(predecessor.store_root), predecessor_tests.PREDECESSOR_ID))
        if not amd64:
            self.lock(predecessor.store_root)
        state = {'phase': 0, 'campaign_dir': campaign_dir, 'predecessor_root': predecessor.store_root,
            'predecessor_external': predecessor.external_root, 'target_fixture': self._fixture_values(fixture),
            'campaign_bindings': [stops.file_cut(campaign_dir / filename)
                                  for filename in ('campaign.json', 'campaign.sha256')],
            'audit_snapshots': {scenario: 'uninitialized'}}
        self._predecessor_save(name, state, 0)
        return state

    def _predecessor_save(self, name: str, state: dict[str, Any], phase: int, **values: Any) -> None:
        state.update(values, phase=max(int(state['phase']), phase))
        stops.checkpoint(self, name, **state)

    def _predecessor_cli(self, name: str, key: str, state: dict[str, Any], mode: str) -> tuple[int, Any, str]:
        argv = predecessor_tests._import_argv(state['campaign_dir'], state['predecessor_root'],
                                              predecessor_tests.PREDECESSOR_ID, mode)

        def invoke() -> tuple[int, Any, str]:
            pending = stops.begin_command(argv)
            code, stdout, stderr = predecessor_tests._run_main(argv)
            stops.record_command(argv, code, stdout, stderr, pending=pending)
            return code, json.loads(stdout) if stdout.strip() else None, stderr

        result = stops.command_step(self, name, key, argv, invoke)
        self._verify_predecessor(state)
        if result[0] == 0:
            store = self.target_store(state['campaign_dir'])
            store.load_object(result[1]['import_ref'])
            store.load_receipt(result[1]['receipt_ref'])
        return result

    def _predecessor_fact(self, name: str, key: str, state: dict[str, Any], kind: str,
                          payload: dict[str, Any], issued: str) -> tuple[int, Any, str]:
        """记录原 Store API 的真实引用或 ControlError；pending 缺完整输出时只交原事实裁定。"""

        from tools.official_client_control.errors import ControlError

        store = self.target_store(state['campaign_dir'])
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        argv = ['原 Store.append_fact', str(store.root), predecessor_tests.TARGET_ID, kind, digest, issued]

        def invoke() -> tuple[int, Any, str]:
            pending = stops.begin_command(argv)
            try:
                result = store.append_fact(predecessor_tests.TARGET_ID, kind, payload, issued)
            except ControlError as error:
                stderr = str(error)
                stops.record_command(argv, 1, '', stderr, pending=pending)
                return 1, None, stderr
            stdout = json.dumps(result, ensure_ascii=False, sort_keys=True)
            stops.record_command(argv, 0, stdout, '', pending=pending)
            return 0, result, ''

        result = stops.command_step(self, name, key, argv, invoke)
        self._verify_predecessor(state)
        return result

    def write_target_evidence(self, campaign_dir: Path, fixture: predecessor_tests.SyntheticCampaign,
                              package_ref: dict[str, Any]) -> dict[str, Any]:
        """原两次事实写入分别停点；Discovery 成功后恢复只核原引用，不再追加第二条。"""

        if stops.record_root() is None:
            return super().write_target_evidence(campaign_dir, fixture, package_ref)
        from tools.official_client_control.errors import ControlError

        name, state = self._predecessor_cursor, self._predecessor_state
        self.assertEqual(campaign_dir, state['campaign_dir'])
        if 'discovery_payload' not in state:
            store = self.target_store(campaign_dir)
            payload = {'version': store.load_campaign(predecessor_tests.TARGET_ID)['target_version'],
                'source': 'synthetic-fixture', 'discovered_at_utc': fixture._time(),
                'tool_sha256': fixture.tool_sha256, 'artifact_refs': [fixture.official_binding]}
            discovery_issued, evidence_issued = fixture._time(), fixture._time()
            self._predecessor_save(name, state, 2, discovery_payload=payload, discovery_issued=discovery_issued,
                evidence_issued=evidence_issued, target_fixture=self._fixture_values(fixture))
        code, discovery, stderr = self._predecessor_fact(name, 'discovery-write', state,
            'discovery_recorded', state['discovery_payload'], state['discovery_issued'])
        self.assertEqual(code, 0, stderr)
        original = self.target_store(campaign_dir).load_fact(discovery)
        self.assertEqual((original['fact_kind'], original['payload'], original['issued_at_utc']),
            ('discovery_recorded', state['discovery_payload'], state['discovery_issued']))
        if 'discovery_ref' in state:
            self.assertEqual(discovery, state['discovery_ref'])
        self._predecessor_save(name, state, 3, discovery_ref=discovery)
        if 'evidence_before' not in state:
            before = file_digests(self.target_store(campaign_dir).root)
            self._predecessor_save(name, state, 3, evidence_before=before,
                evidence_bindings=[stops.file_cut(Path(path)) for path in sorted(before)])
        code, result, stderr = self._predecessor_fact(name, 'evidence-refusal', state, 'evidence_recorded',
            {'discovery_fact_ref': discovery, 'evidence_package_ref': package_ref}, state['evidence_issued'])
        for binding in state['evidence_bindings']:
            stops.read_cut(binding)
        self.assertEqual(file_digests(self.target_store(campaign_dir).root), state['evidence_before'],
                         '原 Evidence 拒绝不得新增、删除或改写 Store 原件')
        self._predecessor_save(name, state, 4)
        if code != 0:
            raise ControlError(stderr)
        return result

    def revalidate_completed_method(self, expected: list[str]) -> list[dict[str, Any]]:
        """只返回原六项记录；唯一外层 runner 负责输出，不重复写记录文件。"""

        contexts = {
            '跨客户端误用：x86_64 证据喂给 arm64 目标': 'predecessor-x86',
            '跨客户端误用：夹具收据冒充正式引用': 'predecessor-fixture-formal',
        }
        if len(expected) != 1 or expected[0] not in contexts:
            raise ValueError('前序原方法的场景集合不一致')
        state = stops.resume_checkpoint(self, contexts[expected[0]])
        minimum = 2 if expected[0] == '跨客户端误用：x86_64 证据喂给 arm64 目标' else 6
        if state is None or state.get('phase') == 'initializing' or state.get('phase', -1) < minimum:
            raise ValueError('原前序方法缺完整世界游标')
        self._verify_predecessor(state)
        return [ScenarioAudit.revalidate(self, expected[0])]

    def _audit(self, scenario: str, *, trusted: Iterable[Path], history: Iterable[Path]) -> ScenarioAudit:
        audit = ScenarioAudit(self, scenario)
        audit.snapshot(trusted=trusted, history=[*history, self.project_root, self.timing_dir],
                       timing_dir=self.timing_dir, project_root=self.project_root)
        return audit

    @_resumable_method
    def _scenario_misuse_x86_evidence(self) -> None:
        """跨客户端误用：x86_64 证据喂给 arm64 目标——前序是 linux/amd64 平台的 Store Campaign，arm64 目标的总计划冻结了它，
        read_only_reuse 导入仍按七项身份逐项相等零写入拒绝（拒绝原因写明 platform）；只能以 regression_fixture 登记（见下一场景）。"""

        name, scenario = 'predecessor-x86', '跨客户端误用：x86_64 证据喂给 arm64 目标'
        world = self._predecessor_start(name, scenario, amd64=True)
        campaign_dir = world['campaign_dir']
        target_store = self.target_store(campaign_dir).root
        audit = self._audit(scenario, trusted=[world['predecessor_root'], target_store, campaign_dir / "campaign.json"],
                            history=[target_store])
        if 'accounting_before' not in world:
            self._predecessor_save(name, world, 0, accounting_before=audit.accounting_state())
        code, _stdout, stderr = self._predecessor_cli(name, 'read-only-reuse-refusal', world, 'read_only_reuse')
        self.assertEqual(code, 1, stderr)
        self.assertRegex(stderr, "read_only_reuse 要求七项全部可证且逐项相等：platform")
        self._predecessor_save(name, world, 1)
        imported = self.target_store(campaign_dir).list_object_refs("predecessor_import")
        audit.check_trusted()
        audit.check_closure(executed=[str(item) for item in imported], expected=[], what="被拒绝的前序导入（登记对象）",
                            source=audit.refusal_closure_source(stderr))
        audit.check_accounting(repeats=[("被拒绝的导入", world['accounting_before'], audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(stderr, "platform", "七项全部可证且逐项相等")
        audit.report(predecessor_platform="linux/amd64")
        self._predecessor_save(name, world, 2)

    @_resumable_method
    def _scenario_misuse_fixture_as_formal(self) -> None:
        """跨客户端误用：夹具收据冒充正式引用——regression_fixture 导入（x86_64 前序，三项身份差异如实登记）成功但逐个对象都不
        授予正式引用；拿导入的 EvidencePackage 写本 Campaign 的 EvidenceFact 被 Store 拒绝（零写入），以同一导入重跑复用原登记。"""

        from tools.official_client_control.errors import ControlError
        name, scenario = 'predecessor-fixture-formal', '跨客户端误用：夹具收据冒充正式引用'
        world = self._predecessor_start(name, scenario)
        self._predecessor_cursor, self._predecessor_state = name, world
        campaign_dir, fixture = world['campaign_dir'], self._restore_fixture(world['target_fixture'])
        target_store = self.target_store(campaign_dir).root
        audit = self._audit(scenario, trusted=[world['predecessor_root'], campaign_dir / "campaign.json"],
                            history=[target_store])
        code, summary, stderr = self._predecessor_cli(name, 'fixture-import', world, 'regression_fixture')
        self.assertEqual(code, 0, stderr)
        self._predecessor_save(name, world, 1)
        self.assertEqual([item["item"] for item in summary["identity_differences"]], ["platform", "oauth_org_id", "model_catalog_sha256"])
        self.assertTrue(summary["formal_references"])
        self.assertTrue(all(not item["authorized"] for item in summary["formal_references"]))
        if 'facts_before' not in world:
            self._predecessor_save(name, world, 2,
                facts_before=self.target_store(campaign_dir).list_facts(predecessor_tests.TARGET_ID),
                accounting_before=audit.accounting_state())
        facts_before = world['facts_before']
        with self.assertRaisesRegex(ControlError, "未获授权引用：③") as refused:
            self.write_target_evidence(campaign_dir, fixture, summary["evidence_package_ref"])
        # EvidenceFact 被拒（Discovery 是本 Campaign 自己的，先于引用核验写入，属合法事实）。
        facts_after = self.target_store(campaign_dir).list_facts(predecessor_tests.TARGET_ID)
        self.assertEqual([fact["fact_kind"] for fact in facts_after if fact["fact_kind"] == "evidence_recorded"],
                         [fact["fact_kind"] for fact in facts_before if fact["fact_kind"] == "evidence_recorded"])
        code, again, stderr = self._predecessor_cli(name, 'fixture-import-repeat', world, 'regression_fixture')
        self.assertEqual(code, 0, stderr)
        self.assertEqual((again["import_ref"], again["receipt_reused"]), (summary["import_ref"], True))
        self._predecessor_save(name, world, 5)
        audit.check_trusted()
        audit.check_closure(executed=[item["fact_kind"] for item in summary["imported_facts"]],
                            expected=["discovery_recorded", "evidence_recorded"], what="夹具导入登记的事实（不授予正式引用）",
                            source=audit.closure_source(summary, ('imported_facts',), row_key='fact_kind'))
        audit.check_accounting(repeats=[("拒绝的正式引用与重跑导入", world['accounting_before'], audit.accounting_state())])
        audit.check_elapsed_untouched(self.project_root, self.timing_dir)
        audit.check_history()
        audit.check_entry(str(refused.exception), "未获授权引用", "③")
        audit.report(identity_differences=[item["item"] for item in summary["identity_differences"]], live_request_count=summary["live_request_count"])
        self._predecessor_save(name, world, 6)


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


class _LateStageFaultScenarios(late_driver.ClaudeLateStageChainDriver):
    """A 组未认证场景；单独定向验收后才能加入外层完整链登记。"""

    @_resumable_method
    def _scenario_delivery_faults_in_one_campaign(self) -> None:
        records = self.exercise_delivery_faults()
        self.assertEqual(len(records), 4)


class _LateControlFaultScenarios(late_control_driver.ClaudeLateControlChainDriver):
    """同一世界的暂停、延期和交付恢复；完整 A 组剩余场景未验收前不注册。"""

    @_resumable_method
    def _scenario_control_and_delivery_in_one_campaign(self) -> None:
        records = self.exercise_control_and_delivery_faults()
        self.assertEqual(len(records), 6)


class _TransientSegmentScenarios(transient_driver.ClaudeTransientChainDriver):
    """恢复段未认证场景；定向串行验收完成前不加入外层完整链登记。"""

    @_resumable_method
    def _scenario_apply_run_seal_builder_in_one_campaign(self) -> None:
        self.run_transient_chain()

    @_resumable_method
    def _scenario_full_control_delivery(self) -> None:
        world = self.run_transient_control_delivery_chain()
        self.assertEqual(tuple(row['scenario'] for row in world['records']), LATE_CONTINUOUS_SCENARIOS)


class _RevisionEvaluatorScenarios(revision_scenarios._RevisionEvaluatorScenarios):
    """私有完整第三链；普通发现和当前认证都不自动运行。"""

    @_resumable_method
    def _scenario_revision_evaluator_delivery(self) -> None:
        world = self.run_revision_evaluator_delivery_chain()
        self.assertEqual(tuple(row['scenario'] for row in world['records']), REVISION_CONTINUOUS_SCENARIOS)
        _publish_resumed_method_records(self, world['records'], list(REVISION_CONTINUOUS_SCENARIOS))


class ClaudeFaultChainTests(unittest.TestCase):
    """外层场景入口（方法直接写在类体里，部署脚本按 AST 核对）与链体（只在副本树子进程里运行）。"""

    real_chain_metrics: dict[str, Any]

    # ---- 外层：pre-A3 登记的场景入口 ----

    def test_recovery_chain_keeps_trusted_results_and_resumes_every_fault(self) -> None:
        """完整恢复链含原生候选录制世界；非原生宿主未验证，不启动降级整链。"""

        if not sys.platform.startswith("linux") or os.geteuid() != 0 or os.uname().machine not in {"aarch64", "arm64"}:
            self.skipTest("未验证：正式恢复链要求 Linux ARM64 root 与 mount/net namespace")
        self._run_chain("recovery")

    def test_late_stage_faults_resume_seven_scenarios(self) -> None:
        """正式入口要求 Linux ARM64 root；本机降级回归只能走原私有入口，不能成为认证结果。"""

        if not sys.platform.startswith("linux") or os.geteuid() != 0 or os.uname().machine not in {"aarch64", "arm64"}:
            self.skipTest("未验证：正式后段链要求 Linux ARM64 root 与 mount/net namespace")
        self._run_chain('late-continuous')

    def _run_pending_late_continuous(self) -> None:
        """保留原显式定向入口；允许本机降级回归，不能拿其结果替代正式场景认证。"""

        self._run_chain('late-continuous')

    def test_revision_evaluator_faults_resume_eight_scenarios(self) -> None:
        """公共入口仅在 Linux ARM64 root 中委托原八场景外壳；跳过、失败均不能签认证。"""

        if not sys.platform.startswith("linux") or os.geteuid() != 0 or os.uname().machine not in {"aarch64", "arm64"}:
            self.skipTest("未验证：正式 r2／评估器链要求 Linux ARM64 root 与 mount/net namespace")
        self._run_pending_revision_continuous()

    def _run_pending_revision_continuous(self) -> None:
        """保留显式本机定向入口；公共入口也委托本实现，维持唯一八场景外壳。

        claude_revision_evaluator_scenarios 的 --execute 委托本入口保存
        真实指标与首败；本机成功仍是夹具结果，不签正式认证。
        """

        self._run_chain('revision-continuous')

    def _isolation_mode(self) -> str:
        """Linux 上以 root 运行必须进命名空间（缺 ``unshare`` 即失败，不降级）；其余环境降级为副本树加替身。"""

        if sys.platform.startswith("linux") and os.geteuid() == 0:
            if shutil.which("unshare") is None:
                self.fail("Linux root 上必须进 unshare -m -n 命名空间运行，但 PATH 里没有 unshare")
            return shell.ISOLATION_NAMESPACE
        return shell.ISOLATION_DEGRADED

    def _run_chain(self, chain: str, *, diagnostic_seconds: int | None = None) -> None:
        # 正式入口只有一条命令。原稳定记录目录有唯一停点即重验续作，
        # 没有记录目录时保持原临时世界行为；不为失败自动申请新世界。
        self.assertIsNone(diagnostic_budget.diagnostic_seconds(), '公共链不能继承诊断预算；须显式调用私有诊断入口')
        record_directory = os.environ.get('CLAUDE_RECORDED_CHAIN_RECORD_DIR')
        if not record_directory:
            if os.environ.get(stops.CHAIN_RESUME_ENV):
                raise ValueError('显式恢复必须指定原稳定记录目录，不能重建缺失停点')
            self._run_chain_body(chain, diagnostic_seconds=diagnostic_seconds)
            return
        root = _private_record_root(Path(record_directory))
        started = time.monotonic()
        with _record_lock(root), _record_scope(root):
            stop_name = 'chain:' + chain
            saved = stops.load_state(stop_name)
            source = _source_inputs()
            if saved is None and os.environ.get(stops.CHAIN_RESUME_ENV):
                raise ValueError('原链缺完整停点，不能从历史 scratch.json 追认 before 或初始化新世界')
            if saved is not None:
                if saved.get('chain') != chain or saved.get('diagnostic_seconds') != diagnostic_seconds:
                    raise ValueError('原链身份或预算模式变化，禁止消费另一条链或诊断结果')
                continuation_path = stops.selected_product_continuation_path(root, source)
                continuation = stops.file_cut(continuation_path) if continuation_path.exists() else None
                stops.validate_executor_inputs(saved['source'], source, continuation)
                if continuation is not None:
                    document = stops.verify_product_continuation(continuation, tree=Path(saved['tree']),
                                                                 original_source=saved['source'], current_source=source)
                    coordinates = {key: saved[key] for key in document['chain_coordinates']}
                    if coordinates != document['chain_coordinates'] or document['chain'] != chain:
                        raise ValueError('承接凭证不属于原链停点，禁止换世界')
                    for method, expected in _continuation_methods(chain).items():
                        with _record_scope(Path(document['methods'][method]['root'])):
                            _verify_continuation_method(document, method, expected)
                            if document['mode'] in stops.FORMED_READ_ONLY_MODES:
                                _complete_method_original(expected)
                if saved.get('isolation') != self._isolation_mode():
                    raise ValueError('原链隔离方式已改变，不能承接另一执行环境')
            else:
                continuation = None
            self._run_chain_body(chain, diagnostic_seconds=diagnostic_seconds, stop=saved,
                                 source=source, setup_seconds=time.monotonic() - started, continuation=continuation)

    def _run_chain_body(self, chain: str, *, diagnostic_seconds: int | None = None,
                        stop: dict[str, Any] | None = None, source: dict[str, Any] | None = None,
                        setup_seconds: float = 0.0, continuation: dict[str, Any] | None = None) -> None:
        # 诊断只允许私有调用显式给预算；公共 test 入口不能靠继承环境放宽认证。
        self.assertIsNone(diagnostic_budget.diagnostic_seconds(), "公共链不能继承诊断预算；须显式调用私有诊断入口")
        if diagnostic_seconds is not None:
            diagnostic_budget.validate_seconds(diagnostic_seconds)
            if not os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR"):
                raise ValueError("诊断必须指定原件记录目录")
        spec = CHAINS[chain] if chain in CHAINS else PENDING_CONTINUOUS_CHAINS[chain]
        method_count = sum(len(group) for _step, group in RECOVERY_CHAIN_GROUPS) if chain == "recovery" else 1
        time_budget = certification.claude_fault_time_budget(spec["scenario"])
        self.assertEqual(method_count, time_budget["method_count"], "场景数量变化必须同步正式时间合同")
        timeout_seconds = (method_count * diagnostic_seconds + 60 if diagnostic_seconds is not None else
                           time_budget["chain_timeout_seconds"])
        started = time.monotonic()
        mode = self._isolation_mode()
        metrics: dict[str, Any] = {}
        completed: subprocess.CompletedProcess | None = None
        copy_seconds: float | None = None
        timed_out = False
        record_directory = os.environ.get("CLAUDE_RECORDED_CHAIN_RECORD_DIR")
        stdout_bytes, stderr_bytes = b"", b""
        stdout, stderr = "", ""
        executor_binding = None
        try:
            # 连续链内还有两层来源夹具，守卫的 Unix socket 路径必须保持足够短。
            # macOS 的系统临时根本身很长；短根仍位于宿主数据根和执行副本之外。
            scratch = Path("/private/tmp") if sys.platform == "darwin" else shell._scratch_root()
            # 已配置原件目录时保留原 scratch 坐标：来源和收据绑定绝对路径，不能搬走后改 JSON 冒充可重放。
            if stop is not None:
                previous_root = Path(stop['scratch_root'])
                if (previous_root.is_symlink() or not previous_root.is_dir()
                        or previous_root.parent != scratch.resolve()):
                    raise ValueError('原 scratch 坐标缺失或不属于原隔离根，禁止另建世界')
                temporary = contextlib.nullcontext(str(previous_root))
            else:
                temporary = (contextlib.nullcontext(tempfile.mkdtemp(prefix="cfc-", dir=scratch)) if record_directory else
                         tempfile.TemporaryDirectory(prefix="cfc-", dir=scratch))
            with temporary as directory:
                staging = Path(directory).resolve() / "staging"
                recorded_worlds = staging / "recorded-worlds"
                try:
                    if stop is None:
                        staging.mkdir(mode=0o700)
                        # 与 B5a-05 相同：新 staging 取本进程有效组。
                        os.chown(staging, -1, os.getegid())
                        recorded_worlds.mkdir(mode=0o700)
                        tree = _copy_tree(staging / "tree")
                        copy_seconds = round(time.monotonic() - started, 3)
                        if record_directory:
                            stop = {'chain': chain, 'source': source, 'scratch_root': Path(directory).resolve(),
                                    'tree': tree, 'recorded_worlds': recorded_worlds, 'isolation': mode,
                                    'diagnostic_seconds': diagnostic_seconds, 'seconds': 0.0,
                                    'first_started_at_utc': datetime.now(timezone.utc).isoformat(), 'invocations': []}
                            stops.save_state('chain:' + chain, stop)
                    else:
                        tree = Path(stop['tree'])
                        if (tree != staging / 'tree' or recorded_worlds != stop['recorded_worlds']
                                or any(path.is_symlink() or not path.is_dir() for path in (staging, tree, recorded_worlds))):
                            raise ValueError('原源码树或记录世界缺失，禁止重新复制补造')
                        copy_seconds = 0.0
                    if record_directory:
                        # 与原 copy_managed_tree 同一闭包算法；只更新其中的普通测试源码。
                        # 产品和夹具不运输，原 source 原件不改；世界仍由原停点绑定。
                        executor_paths = sorted(path.relative_to(REPO_ROOT).as_posix()
                            for path in trees._closure_files(Path(__file__))
                            if stops._executor_path(path.relative_to(REPO_ROOT).as_posix()))
                        executor_binding = stops.prepare_executor_source(tree, stop['source'], source, executor_paths,
                                                                          continuation=continuation)
                    environment = trees.subprocess_env(tree, {
                        project_ledger_fixture.FIXTURE_ONLY_ENV: "1",
                        "TMPDIR": str(staging),
                        shell.ISOLATION_ENV: mode,
                        shell.TREE_ENV: str(tree),
                        shell.SOURCE_REPO_ENV: str(REPO_ROOT),
                        shell.IDENTITY_MEMO_ENV: str(staging / "identity-memo"),
                        # 宿主数据根在 namespace 中保持只读，链体原件只写本次私有可写根。
                        "CLAUDE_RECORDED_CHAIN_RECORD_DIR": str(recorded_worlds),
                    })
                    environment.pop(stops.EXECUTOR_SOURCE_ENV, None)
                    if diagnostic_seconds is not None:
                        environment[diagnostic_budget.DIAGNOSTIC_TIMEOUT_ENV] = str(diagnostic_seconds)
                    if record_directory:
                        methods = recorded_worlds / 'method-records'
                        _private_record_root(methods)
                        environment[METHODS_ROOT_ENV] = str(methods)
                        environment[stops.EXECUTOR_SOURCE_ENV] = json.dumps(stops._encoded(executor_binding))
                        environment['PYTHONPYCACHEPREFIX'] = str(trees.copy_pycache_prefix(tree) / ('executor-' + executor_binding['sha256']))
                    # 链体自身不能把公共目录当成方法目录；每个原方法由 _run_group 唯一绑定。
                    for key in (stops.METHOD_RECORD_ENV, stops.METHOD_RESUME_ENV, METHOD_VERIFY_ENV):
                        environment.pop(key, None)
                    argv = [sys.executable, "-m", "unittest", "-v", f"{CHAIN_MODULE}.{type(self).__name__}.{spec['exercise']}"]
                    command = shell._namespace_argv(argv) if mode == shell.ISOLATION_NAMESPACE else argv
                    try:
                        # 先捕获完整原始字节；解码与指标解析失败也必须保留子进程原件。
                        completed = subprocess.run(command, capture_output=True, cwd=str(tree), env=environment,
                                                   timeout=timeout_seconds, stdin=subprocess.DEVNULL)
                        stdout_bytes, stderr_bytes = completed.stdout or b"", completed.stderr or b""
                        if executor_binding is not None:
                            stops.verify_executor_source(executor_binding, tree, verify_product=False)
                        if continuation is not None:
                            stops.verify_product_continuation(continuation, tree=tree)
                    except subprocess.TimeoutExpired as error:
                        timed_out = True
                        stdout_bytes, stderr_bytes = error.stdout or b"", error.stderr or b""
                    if completed is not None:
                        stdout, stderr = stdout_bytes.decode("utf-8"), stderr_bytes.decode("utf-8")
                        metrics = _parse_metrics(stdout)
                finally:
                    if record_directory:
                        # 只增旁侧原件，不移动 scratch、不改绑定；失败和超时与成功走同一保存出口。
                        error = sys.exc_info()[1]
                        parent = Path(record_directory)
                        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        output = Path(tempfile.mkdtemp(prefix=f"fault-chain-{chain}-{Path(directory).name}-", dir=parent))
                        for name, raw in (("stdout.log", stdout_bytes), ("stderr.log", stderr_bytes)):
                            path = output / name
                            with path.open("xb") as handle:
                                handle.write(raw)
                            path.chmod(0o600)
                        state = output / "scratch.json"
                        with state.open("x", encoding="utf-8") as handle:
                            json.dump({"scenario": spec["scenario"], "scratch_root": str(Path(directory).resolve()),
                                       "recorded_worlds": str(recorded_worlds), "isolation": mode,
                                       "returncode": None if completed is None else completed.returncode,
                                       "timed_out": timed_out, "timeout_seconds": timeout_seconds,
                                       "executor": stops._encoded(executor_binding),
                                       "error_type": None if error is None else type(error).__name__},
                                      handle, ensure_ascii=False, sort_keys=True, indent=2)
                            handle.write("\n")
                        state.chmod(0o600)
                        if stop is not None:
                            stop['invocations'].append({'stdout': stops.file_cut(output / 'stdout.log'),
                                'stderr': stops.file_cut(output / 'stderr.log'), 'exit': stops.file_cut(state),
                                'executor': executor_binding})
        finally:
            invocation_elapsed = round(time.monotonic() - started + setup_seconds, 3)
            elapsed = round(invocation_elapsed + (float(stop['seconds']) if stop is not None else 0.0), 3)
            if stop is not None:
                stop['seconds'] = elapsed
                stops.save_state('chain:' + chain, stop)
            self.real_chain_metrics = {
                **{key: value for key, value in metrics.items() if key != "chain"},
                "scenario": spec["scenario"],
                "diagnostic_only": diagnostic_seconds is not None,
                "time_budget": time_budget,
                # 三层安全网只约束本次调用；历史成本继续累计，不抵扣本次续作或重验的完整时限。
                "formal_time_budget_met": invocation_elapsed < time_budget["chain_budget_seconds"],
                "seconds": elapsed,
                "invocation_seconds": invocation_elapsed,
                "copy_seconds": copy_seconds,
                "isolation": mode,
                "network_isolated": mode == shell.ISOLATION_NAMESPACE,
                "returncode": None if completed is None else completed.returncode,
            }
        if timed_out:
            self.fail(f"{spec['scenario']} 执行器超过清理时限 {timeout_seconds} 秒；诊断={diagnostic_seconds is not None}")
        if diagnostic_seconds is None and invocation_elapsed >= time_budget["chain_budget_seconds"]:
            self.fail(f"{spec['scenario']} 本次调用超过安全网 {time_budget['chain_budget_seconds']} 秒"
                      f"（本次 {invocation_elapsed} 秒，累计 {elapsed} 秒）")
        assert completed is not None
        if completed.returncode != 0:
            self.fail(f"{spec['scenario']} 子进程失败（退出码 {completed.returncode}，隔离={mode}，耗时 {elapsed} 秒；已通过的步骤："
                      f"{'、'.join(metrics.get('steps') or []) or '没有'}）：\n{stdout[-3000:]}\n{stderr[-6000:]}")
        self.assertNotIn("skipped=", stderr, "链体不得跳过")
        self.assertTrue(metrics.get("steps"), "通过的链必须写出指标与已通过的步骤")
        self.assertEqual(metrics.get("upstream_requests"), 0, "链体必须零请求")
        expected = list(spec.get('expected') or [name for _step, group in RECOVERY_CHAIN_GROUPS for name in group.values()])
        records = metrics.get('scenarios') or []
        self.assertTrue(_records_complete(records, expected), '每个场景必须按顺序写出六项真实通过记录')
        if spec.get('expected'):
            self.assertEqual(len({row.get('campaign_id') for row in records}), 1, '连续链不得拼多个 Campaign 的场景')
            self.assertTrue(all(row.get('campaign_id') for row in records), '连续记录缺少 Campaign 坐标')

    def _exercise_late_continuous(self) -> None:
        recorder = _ChainRecorder('late-continuous')
        shell._assert_isolation(self, recorder)
        _run_group(self, recorder, {'_TransientSegmentScenarios._scenario_full_control_delivery': LATE_CONTINUOUS_SCENARIOS},
                   '同 Campaign 的 transient、暂停、延期、严格验收与交付恢复')
        recorder.facts['six_checks'] = {row['scenario']: sorted(row['checks']) for row in recorder.facts['scenarios']}
        recorder.emit()

    def _exercise_revision_continuous(self) -> None:
        recorder = _ChainRecorder('revision-continuous')
        shell._assert_isolation(self, recorder)
        _run_group(self, recorder, {'_RevisionEvaluatorScenarios._scenario_revision_evaluator_delivery': REVISION_CONTINUOUS_SCENARIOS},
                   '同 Campaign 的 r2、评估器修复、严格验收与交付恢复')
        recorder.facts['six_checks'] = {row['scenario']: sorted(row['checks']) for row in recorder.facts['scenarios']}
        recorder.emit()

    # ---- 链体：recovery-chain ----

    def _exercise_recovery_chain(self) -> None:
        recorder = _ChainRecorder("recovery")
        shell._assert_isolation(self, recorder)
        failures = []
        for step, group in RECOVERY_CHAIN_GROUPS:
            try:
                _run_group(self, recorder, group, step)
            except AssertionError as error:
                failures.append(str(error))
        if failures:
            recorder.emit()
            self.fail("已执行全部独立场景组：\n" + "\n".join(failures))
        recorder.facts["six_checks"] = {record["scenario"]: sorted(record["checks"]) for record in recorder.facts["scenarios"]}
        recorder.emit()


if __name__ == "__main__":
    unittest.main()
