"""D6（第 10a 项配套）：审计运行里失败的单元允许无审计补跑一次，补跑记录不可承接。

背景：承接要求被重跑的单元也带读集审计（否则下一轮没有严格来源），可优化第 2 项的补跑条件原先排除了“本次运行对它做了读集审计”的单元：
带审计的修复轮里一个偶发失败就会让整轮变红，补跑的收益在承接场景里失效。现在审计运行里的失败单元也补跑一次，但补跑尝试不包 strace、不产生读集
审计（调度器只对首次的 formal 种类包审计器），补跑记录 ``inheritable=false``；它首次尝试的审计结果不让整轮审计汇总变红。

本文件用假调度器直接测补跑阶段 ``_run_retry_phase`` 与审计汇总的口径函数 ``_retried_unaudited_adjust``（审计要 strace，本机不能真跑）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.ci import retry_policy as rp
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur


@dataclass
class FakeRecorder:
    records: Any = ur


@dataclass
class FakeScheduler:
    """只实现 ``_run_retry_phase`` 用到的三样：记录模块、首次尝试登记、逐个单独运行。"""

    root: Path
    retry_of: dict = field(default_factory=dict)
    ran: list = field(default_factory=list)
    recorder: FakeRecorder = field(default_factory=FakeRecorder)

    def run_alone(self, units: list, kind: str) -> list:
        self.ran.append((kind, [unit.unit_id for unit in units]))
        return [make_outcome(self.root, unit, exit_code=0, kind=kind) for unit in units]


def make_unit(unit_id: str) -> ue.Unit:
    return ue.Unit(unit_id, unit_id, (), ue.Quota(1, 128), False, 1, command=("true",), cwd="/", reservation_restartable=True)


def make_outcome(root: Path, unit: ue.Unit, *, exit_code: int, kind: str = "formal", audit: dict | None = None) -> ue.Outcome:
    log = root / f"{unit.unit_id.replace(':', '-')}-{kind}.log"
    log.write_text(f"{unit.unit_id} {kind}\n", encoding="utf-8")
    extra: dict[str, Any] = {"record_sha256": "a" * 64, "record_path": str(root / "record.json")}
    if audit is not None:
        extra["read_audit"] = audit
    return ue.Outcome(unit=unit, kind=kind, exit_code=exit_code, signal=None, timed_out=False, seconds=1.0, cpu_seconds=0.5, max_rss_mb=10.0,
                      orphans=[], log_path=log, result=None, extra=extra)


class RetryPhaseUnderAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = ur.RecordStore(self.root / "store")
        self.scheduler = FakeScheduler(self.root)

    def phase(self, outcomes: list, audited) -> tuple:
        return ue._run_retry_phase(self.scheduler, outcomes, store=self.store, run_id="run-1", config=rp.RetryConfig(enabled=True),
                                   audited=audited)

    def test_t_d6_an_audited_failed_unit_is_retried_once_and_the_retry_passes(self) -> None:
        unit = make_unit("flaky:audited")
        failed = make_outcome(self.root, unit, exit_code=1, audit={"status": "passed", "coverage_complete": True})
        final, retried, diagnostic, markers = self.phase([failed], audited=lambda candidate: True)
        self.assertEqual(self.scheduler.ran, [("retry", ["flaky:audited"])], "审计运行里的失败单元也补跑一次")
        self.assertEqual(markers["flaky:audited"]["status"], "retry-passed")
        self.assertTrue(final[0].passed)
        self.assertNotIn("read_audit", final[0].extra, "补跑尝试不做读集审计，没有读集证据")
        self.assertEqual(diagnostic, [])
        self.assertEqual([item.kind for item in retried], ["retry"])

    def test_t_d6_a_still_failing_audited_unit_is_judged_red_as_before(self) -> None:
        unit = make_unit("fails:twice")
        failed = make_outcome(self.root, unit, exit_code=1, audit={"status": "passed", "coverage_complete": True})
        self.scheduler.run_alone = lambda units, kind: [make_outcome(self.root, item, exit_code=2, kind=kind) for item in units]
        final, _, _, markers = self.phase([failed], audited=lambda candidate: True)
        self.assertEqual(markers["fails:twice"]["status"], "retry-failed")
        self.assertFalse(final[0].passed, "补跑仍红：判红，保持首次失败那一条")

    def test_t_d6_the_retry_record_is_never_inheritable(self) -> None:
        """补跑尝试的记录由 Recorder 写成 attempt=retry、inheritable=false（第 2 项的既有行为）：承接侧的 ``check_record`` 因此永远拒绝它。"""
        record = {"kind": "formal", "attempt": "retry", "inheritable": False}
        self.assertIs(record["inheritable"], False)
        source = Path(ue.__file__).read_text(encoding="utf-8")
        self.assertIn('body.update(kind="formal", attempt="retry", retry_of=item.retry_of, inheritable=False', source)


class AuditSummaryUnderRetryTests(unittest.TestCase):
    """``_retried_unaudited_adjust``：被无审计补跑过的单元，不让整轮审计汇总变红；其余单元没闭合时口径不变。"""

    def auditor(self, results: dict, *, strict: bool = True, inherited: tuple = ()) -> Any:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = Path(directory.name)

        class Auditor:
            pass

        auditor = Auditor()
        auditor.results, auditor.strict, auditor.inherited = results, strict, frozenset(inherited)
        auditor.currents = {unit_id: object() for unit_id in [*results, *inherited]}
        auditor.dir = out
        return auditor, out

    def report(self, out: Path, **overrides: Any) -> dict:
        path = out / "read-audit.json"
        path.write_text(json.dumps({"coverage_complete": True, "units": {}}), encoding="utf-8")
        base = {"enabled": True, "report": str(path), "units": 2, "skipped_whole_repo": 0, "units_with_findings": ["flaky"],
                "undeclared_total": 1, "status": "failed", "coverage_complete": False, "missing_units": [], "reexecute_required_units": [],
                "reuse_boundary_enforced": False}
        base.update(overrides)
        return base

    GOOD = {"status": "passed", "coverage_complete": True, "undeclared_count": 0}
    BAD_FIRST_ATTEMPT = {"status": "failed", "coverage_complete": False, "undeclared_count": 1}
    MARKERS = {"flaky": {"status": "retry-passed"}}

    def test_t_d6_the_summary_passes_when_only_the_retried_unit_has_an_open_audit(self) -> None:
        auditor, out = self.auditor({"steady": dict(self.GOOD), "flaky": dict(self.BAD_FIRST_ATTEMPT)})
        adjusted = ue._retried_unaudited_adjust(self.report(out), auditor, self.MARKERS)
        self.assertEqual(adjusted["status"], "passed")
        self.assertTrue(adjusted["reuse_boundary_enforced"])
        self.assertFalse(adjusted["coverage_complete"], "全集读集并没有全部闭合——不冒充")
        self.assertEqual(adjusted["retried_unaudited_units"], ["flaky"])
        self.assertEqual(adjusted["undeclared_total"], 0)
        self.assertEqual(adjusted["units_with_findings"], [])
        stored = json.loads((out / "read-audit.json").read_text())
        self.assertEqual(stored["retried_unaudited_units"], ["flaky"])

    def test_t_d6_another_open_audit_keeps_the_old_verdict(self) -> None:
        auditor, out = self.auditor({"steady": dict(self.BAD_FIRST_ATTEMPT), "flaky": dict(self.BAD_FIRST_ATTEMPT)})
        report = self.report(out)
        self.assertEqual(ue._retried_unaudited_adjust(report, auditor, self.MARKERS), report)

    def test_t_d6_without_retries_or_without_strict_audit_nothing_changes(self) -> None:
        auditor, out = self.auditor({"steady": dict(self.GOOD)})
        report = self.report(out)
        self.assertEqual(ue._retried_unaudited_adjust(report, auditor, {}), report)
        auditor, out = self.auditor({"steady": dict(self.GOOD), "flaky": dict(self.BAD_FIRST_ATTEMPT)}, strict=False)
        report = self.report(out)
        self.assertEqual(ue._retried_unaudited_adjust(report, auditor, self.MARKERS), report)

    def test_t_d6_a_missing_audit_result_keeps_the_old_verdict(self) -> None:
        auditor, out = self.auditor({"flaky": dict(self.BAD_FIRST_ATTEMPT)})
        auditor.currents["lost"] = object()                       # 要有审计结果的单元没有结果：不放宽
        report = self.report(out)
        self.assertEqual(ue._retried_unaudited_adjust(report, auditor, self.MARKERS), report)


if __name__ == "__main__":
    unittest.main()
