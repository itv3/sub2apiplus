"""Claude 升级计时账本（Claude 升级控制面设计 B1-05）：计划 v2 带 client、按客户端的默认预算与生成器承接表。

Codex 账本仍写 v1 计划且字段不变；Claude 账本写 ``upgrade-timing-ledger-plan/v2``，create／append／checkpoint／replay
与 Codex 账本同一套事件语义；v2 计划缺 client、client 写成 codex，或 v1 计划混入 client 都拒绝；未绑定项目总账时
阶段上限取该客户端的默认预算；Claude 账本只沿 Claude 承接表承接历史生成器摘要，不借用 Codex 的历史边；计时账本
只能绑定同一客户端的项目总账，数据根迁移后逐级向上按本客户端的总账目录名重新定位。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger

START = "2026-10-07T00:00:00+00:00"
CLAUDE = {
    "upgrade_id": "claude-2-1-226-to-2-1-245",
    "baseline_version": "2.1.226",
    "target_version": "2.1.245",
    "campaign_purpose": "validation_only",
    "evidence_decision": "recapture",
}
CODEX = {
    "upgrade_id": "codex-0157-to-0160",
    "baseline_version": "0.157.0",
    "target_version": "0.160.0",
    "campaign_purpose": "validation_only",
    "evidence_decision": "recapture",
}


def _at(minutes: int) -> str:
    return (datetime(2026, 10, 7, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()


class ClaudeTimingLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _create(self, name: str, fields: dict[str, str], **options: Any) -> Path:
        root = self.directory / name
        ledger.create_ledger(root, started_at_utc=START, **fields, **options)
        return root

    @staticmethod
    def _plan(root: Path) -> dict[str, Any]:
        return json.loads((root / "ledger.json").read_text(encoding="utf-8"))

    def test_claude_plan_is_v2_with_client_and_claude_budgets(self) -> None:
        root = self._create("claude", CLAUDE, client="claude")
        plan = self._plan(root)
        self.assertEqual((plan["schema_version"], plan["client"]), (ledger.CLIENT_PLAN_SCHEMA, "claude"))
        self.assertEqual(plan["stage_budgets_minutes"], dict(ledger.DEFAULT_STAGE_BUDGETS_BY_CLIENT["claude"]))
        self.assertEqual(list(plan["stage_budgets_minutes"]), list(ledger.PHASE_ORDER))
        self.assertEqual(ledger.plan_client(plan), "claude")
        self.assertEqual(ledger.inspect_ledger(root, now=_at(1))["status"], "active")

    def test_codex_plan_stays_v1_without_client(self) -> None:
        implicit = self._plan(self._create("implicit", CODEX))
        explicit = self._plan(self._create("explicit", CODEX, client="codex"))
        for plan in (implicit, explicit):
            self.assertEqual(plan["schema_version"], ledger.PLAN_SCHEMA)
            self.assertNotIn("client", plan)
            self.assertEqual(plan["stage_budgets_minutes"], ledger.DEFAULT_STAGE_BUDGETS)
            self.assertEqual(ledger.plan_client(plan), "codex")
        self.assertEqual(implicit, explicit)

    def test_claude_ledger_append_checkpoint_replay(self) -> None:
        root = self._create("claude", CLAUDE, client="claude")
        ledger.append_event(root, event_id="vc0-done", phase="VC-0", event_type="stage_completed",
                            next_action="启动 VC-1", recorded_at_utc=_at(10))
        ledger.append_event(root, event_id="vc1-started", phase="VC-1", event_type="stage_started",
                            next_action="官方取证", recorded_at_utc=_at(11))
        ledger.append_event(root, event_id="vc1-attempt", phase="VC-1", event_type="attempt_started",
                            attempt_id="vc1-a1", recorded_at_utc=_at(11))
        original = ledger.build_checkpoint(root, observed_at_utc=_at(12))
        ledger._write_once(root / "receipts" / "vc1.json", original)
        ledger.append_event(root, event_id="vc1-attempt-failed", phase="VC-1", event_type="attempt_failed",
                            attempt_id="vc1-a1", root_cause_id="claude.capture.relay-failed", recorded_at_utc=_at(13))
        replayed = ledger.replay(root, "receipts/vc1.json")
        self.assertEqual(replayed, original)
        self.assertEqual((replayed["summary"]["head_sequence"], replayed["summary"]["active_phase"]), (4, "VC-1"))
        self.assertEqual(ledger.inspect_ledger(root, now=_at(14))["head_sequence"], 5)

    def test_client_plan_shape_is_closed(self) -> None:
        claude = self._plan(self._create("claude", CLAUDE, client="claude"))
        codex = self._plan(self._create("codex", CODEX))
        missing = {key: value for key, value in claude.items() if key != "client"}
        cases = {
            "v2 缺 client": (missing, "字段不闭合"),
            "v2 client 写成 codex": (dict(claude, client="codex"), "client 非法"),
            "v2 client 未知": (dict(claude, client="gemini"), "client 非法"),
            "v1 混入 client": (dict(codex, client="claude"), "字段不闭合"),
            "未知 schema": (dict(codex, schema_version="upgrade-timing-ledger-plan/v3"), "schema_version 不匹配"),
        }
        for label, (plan, message) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ledger.TimingLedgerError, message):
                ledger._validate_plan(plan)
        with self.assertRaisesRegex(ledger.TimingLedgerError, "未知客户端"):
            self._create("unknown", CLAUDE, client="gemini")

    def test_unbound_stage_ceilings_follow_client_defaults(self) -> None:
        wide = dict(ledger.DEFAULT_STAGE_BUDGETS_BY_CLIENT["claude"])
        self._create("claude", CLAUDE, client="claude", stage_budgets_minutes=wide)
        with self.assertRaisesRegex(ledger.TimingLedgerError, "VC-0 预算必须为正数且不得宽于上限 45"):
            self._create("codex", CODEX, stage_budgets_minutes=wide)
        too_wide = dict(wide, **{"VC-2": 225})
        with self.assertRaisesRegex(ledger.TimingLedgerError, "VC-2 预算必须为正数且不得宽于上限 210"):
            self._create("claude-too-wide", CLAUDE, client="claude", stage_budgets_minutes=too_wide)

    def test_cli_create_with_client(self) -> None:
        root = self.directory / "cli"
        argv = [
            "create", "--ledger-dir", str(root), "--upgrade-id", CLAUDE["upgrade_id"],
            "--baseline-version", CLAUDE["baseline_version"], "--target-version", CLAUDE["target_version"],
            "--campaign-purpose", CLAUDE["campaign_purpose"], "--evidence-decision", CLAUDE["evidence_decision"],
            "--client", "claude", "--stage-budget-minutes", "VC-3=60",
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ledger.main(argv), 0)
        plan = self._plan(root)
        self.assertEqual(plan["client"], "claude")
        self.assertEqual(plan["stage_budgets_minutes"], dict(ledger.DEFAULT_STAGE_BUDGETS_BY_CLIENT["claude"], **{"VC-3": 60}))
        self.assertEqual(ledger._stage_budget_arguments(["VC-3=60"]), dict(ledger.DEFAULT_STAGE_BUDGETS, **{"VC-3": 60}))


class ClaudeProducerSuccessorTests(unittest.TestCase):
    """Claude 账本只沿 Claude 承接表前进；Codex 历史边对 Claude 账本不可见。"""

    @staticmethod
    def _codex_edge() -> tuple[str, str]:
        """取 Codex 承接表里最后一份收据登记的计时工具摘要边（前序 → 后继）。"""

        repository_root = Path(ledger.__file__).resolve().parents[2]
        descriptor = ledger.PRODUCER_FREEZE_SUCCESSORS[-1]
        return ledger._load_freeze_successor_edge(repository_root, descriptor)

    def test_claude_ignores_codex_history_edges(self) -> None:
        before, after = self._codex_edge()
        current = dict(ledger._producer(), tool_sha256=after)
        frozen = dict(current, tool_sha256=before)
        self.assertTrue(ledger._producer_identity_matches(frozen, current))
        self.assertTrue(ledger._producer_identity_matches(frozen, current, client="codex"))
        self.assertFalse(ledger._producer_identity_matches(frozen, current, client="claude"))
        self.assertTrue(ledger._producer_identity_matches(current, current, client="claude"))

    def test_claude_follows_claude_successor_table(self) -> None:
        before, after = self._codex_edge()
        current = dict(ledger._producer(), tool_sha256=after)
        frozen = dict(current, tool_sha256=before)
        registered = {"claude": (ledger.PRODUCER_FREEZE_SUCCESSORS[-1],)}
        with mock.patch.dict(ledger.PRODUCER_FREEZE_SUCCESSORS_BY_CLIENT, registered):
            self.assertTrue(ledger._producer_identity_matches(frozen, current, client="claude"))

    def test_codex_table_is_the_historical_registry(self) -> None:
        self.assertIs(ledger.PRODUCER_FREEZE_SUCCESSORS_BY_CLIENT["codex"], ledger.PRODUCER_FREEZE_SUCCESSORS)
        self.assertEqual(ledger.PRODUCER_FREEZE_SUCCESSORS_BY_CLIENT["claude"], ())
        self.assertEqual(set(ledger.PRODUCER_FREEZE_SUCCESSORS_BY_CLIENT), set(ledger.PLAN_CLIENTS))


class ProjectLedgerBindingTests(unittest.TestCase):
    """B1-04 之后两客户端共用数据根、各有一本总账：计时账本只绑定同客户端总账，定位时按本客户端的目录名找。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name).resolve()
        self.deadline = (datetime.now(timezone.utc) + timedelta(hours=48)).replace(microsecond=0).isoformat()

    def _project(self, client: str) -> Path:
        root = self.data / project.ledger_dir_name(client)
        project.create_project_ledger(
            root, project_id=f"b1-05-{client}", absolute_deadline_utc=self.deadline, deadline_approved_by="老板",
            estimation_policy="none", estimation_policy_approved_by="老板", fixture_only=False, client=client,
        )
        return root

    def _timing(self, name: str, fields: dict[str, str], **options: Any) -> Path:
        root = self.data / "campaigns" / name / "ledger"
        root.parent.mkdir(parents=True, mode=0o700)
        ledger.create_ledger(root, **fields, **options)
        return root

    def test_claude_ledger_binds_claude_project_ledger(self) -> None:
        claude_project = self._project("claude")
        root = self._timing("claude", CLAUDE, client="claude", project_ledger_dir=claude_project)
        plan = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(plan["project_ledger_binding"]["path"], str(claude_project))
        self.assertEqual(ledger.inspect_ledger(root)["status"], "active")

    def test_cross_client_binding_is_rejected(self) -> None:
        claude_project, codex_project = self._project("claude"), self._project("codex")
        with self.assertRaisesRegex(ledger.TimingLedgerError, "项目总账属于 codex，不能绑定 claude"):
            self._timing("claude", CLAUDE, client="claude", project_ledger_dir=codex_project)
        with self.assertRaisesRegex(ledger.TimingLedgerError, "项目总账属于 claude，不能绑定 codex"):
            self._timing("codex", CODEX, project_ledger_dir=claude_project)

    def test_relocation_searches_client_ledger_directory(self) -> None:
        claude_project = self._project("claude")
        root = self._timing("claude", CLAUDE, client="claude", project_ledger_dir=claude_project)
        plan_sha256 = ledger._sha256_file(claude_project / "plan.json")
        moved = str(self.data / "elsewhere" / "upgrade-project-ledger-claude")
        located = ledger._locate_project_root(root, recorded_path=moved, plan_sha256=plan_sha256, client="claude")
        self.assertEqual(located.resolve(), claude_project.resolve())
        with self.assertRaises(ledger._ProjectLedgerUnreachable):
            ledger._locate_project_root(root, recorded_path=moved, plan_sha256=plan_sha256, client="codex")


if __name__ == "__main__":
    unittest.main()
