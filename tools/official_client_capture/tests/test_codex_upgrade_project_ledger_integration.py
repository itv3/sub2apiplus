"""总账与主工具、监督器的集成：plan 注册、消费者门禁、无总账时的 0.154 formal 拒绝。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import runtime_egress_fixtures


def _future(hours: int = 48) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


def _chmod_tree(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)


def _data_root(directory: str, *, with_ledger: bool) -> Path:
    root = Path(directory).resolve() / "data"
    (root / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
    _chmod_tree(root)
    if with_ledger:
        ledger.create_project_ledger(
            root / ledger.LEDGER_DIR_NAME,
            project_id="p",
            absolute_deadline_utc=_future(),
            deadline_approved_by="老板",
            estimation_policy="none",
            estimation_policy_approved_by="老板",
            fixture_only=False,
        )
    return root


def _fake_create(campaign_dir: Path, *, mode: str = "formal", version: str = "0.154.0", deadline: str | None = None):
    def create(arguments: argparse.Namespace) -> dict:
        campaign_dir.mkdir(mode=0o700)
        manifest = {"campaign_id": arguments.campaign_id, "campaign_mode": mode, "target_version": version}
        _write_json(campaign_dir / "campaign.json", manifest)
        if deadline is not None:
            _write_json(campaign_dir / "control" / "vc" / "campaign-plan.json", {"original_deadline_at_utc": deadline})
            _chmod_tree(campaign_dir)
        return manifest

    return create


class ProjectLedgerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # 本组只验证总账消费者；出口数据来自零请求替身，R15 专项独立验证实际门禁。
        self.enterContext(runtime_egress_fixtures.stubbed_runtime_egress_admission())

    def test_plan_registers_campaign_with_frozen_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _data_root(directory, with_ledger=True)
            campaign_dir = root / "evidence" / "campaigns" / "c1"
            arguments = argparse.Namespace(campaign_dir=campaign_dir, campaign_id="c1", campaign_mode="formal", target_version="0.154.0")
            deadline = _future(10)
            with mock.patch.object(codex_upgrade, "_create_campaign_unadmitted", side_effect=_fake_create(campaign_dir, deadline=deadline)):
                manifest = codex_upgrade.create_campaign(arguments)
            self.assertEqual(manifest["campaign_id"], "c1")
            head = ledger.replay_head(root / ledger.LEDGER_DIR_NAME)
            self.assertEqual(head["registered_campaigns"]["c1"]["deadline_at_utc"], deadline)
            self.assertTrue((campaign_dir / "ledger" / "outbox" / "batch-000001" / "COMMIT").is_file())
            # 消费者门禁通过
            codex_upgrade._assert_project_ledger_consumer("resume", argparse.Namespace(campaign_dir=campaign_dir))
            codex_upgrade._assert_project_ledger_consumer("capture-official", argparse.Namespace(campaign_dir=campaign_dir, capture_action="seal"))
            codex_upgrade._assert_project_ledger_consumer("capture-candidate", argparse.Namespace(campaign_dir=campaign_dir, capture_action="seal"))
            for step in ("seal", "compare", "accept", "production-activation"):
                codex_upgrade._assert_project_ledger_consumer("canonical-advance", argparse.Namespace(campaign_dir=campaign_dir, canonical_step=step))
            supervisor._assert_campaign_run_admitted(campaign_dir)

    def test_formal_0154_plan_without_ledger_is_rejected_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _data_root(directory, with_ledger=False)
            campaign_dir = root / "evidence" / "campaigns" / "c1"
            arguments = argparse.Namespace(campaign_dir=campaign_dir, campaign_id="c1", campaign_mode="formal", target_version="0.154.0")
            with mock.patch.object(codex_upgrade, "_create_campaign_unadmitted", side_effect=_fake_create(campaign_dir)) as create:
                with self.assertRaisesRegex(ledger.ProjectLedgerError, "项目总账不存在"):
                    codex_upgrade.create_campaign(arguments)
                create.assert_not_called()
            self.assertFalse(campaign_dir.exists())
            # preflight 与历史版本在没有总账时照常创建
            preflight = argparse.Namespace(campaign_dir=root / "evidence" / "campaigns" / "p1", campaign_id="p1", campaign_mode="preflight_only", target_version="0.154.0")
            with mock.patch.object(codex_upgrade, "_create_campaign_unadmitted", side_effect=_fake_create(preflight.campaign_dir, mode="preflight_only")):
                codex_upgrade.create_campaign(preflight)
            legacy = argparse.Namespace(campaign_dir=root / "evidence" / "campaigns" / "l1", campaign_id="l1", campaign_mode="formal", target_version="0.151.0")
            with mock.patch.object(codex_upgrade, "_create_campaign_unadmitted", side_effect=_fake_create(legacy.campaign_dir, version="0.151.0")):
                codex_upgrade.create_campaign(legacy)

    def test_consumers_reject_unregistered_0154_formal_and_pass_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _data_root(directory, with_ledger=True)
            campaign_dir = root / "evidence" / "campaigns" / "c1"
            campaign_dir.mkdir(mode=0o700)
            _write_json(campaign_dir / "campaign.json", {"campaign_id": "c1", "campaign_mode": "formal", "target_version": "0.154.0"})
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "缺少账本 plan"):
                codex_upgrade._assert_project_ledger_consumer("resume", argparse.Namespace(campaign_dir=campaign_dir))
            with self.assertRaisesRegex(supervisor.SupervisorError, "项目总账拒绝派发"):
                supervisor._assert_campaign_run_admitted(campaign_dir)
            # 候选 seal 与 canonical-advance 的每个步骤同样是消费者，不能绕开门禁。
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "seal 拒绝"):
                codex_upgrade._assert_project_ledger_consumer("capture-candidate", argparse.Namespace(campaign_dir=campaign_dir, capture_action="seal"))
            for step, consumer in (("seal", "seal"), ("compare", "compare"), ("accept", "accept"), ("rollback-verification", "canonical-advance")):
                with self.assertRaisesRegex(ledger.ProjectLedgerError, f"{consumer} 拒绝"):
                    codex_upgrade._assert_project_ledger_consumer("canonical-advance", argparse.Namespace(campaign_dir=campaign_dir, canonical_step=step))
            # capture-official／capture-candidate run 不是消费者
            codex_upgrade._assert_project_ledger_consumer("capture-official", argparse.Namespace(campaign_dir=campaign_dir, capture_action="run"))
            codex_upgrade._assert_project_ledger_consumer("capture-candidate", argparse.Namespace(campaign_dir=campaign_dir, capture_action="run"))
        with tempfile.TemporaryDirectory() as directory:
            root = _data_root(directory, with_ledger=False)
            legacy = root / "evidence" / "campaigns" / "l1"
            legacy.mkdir(mode=0o700)
            _write_json(legacy / "campaign.json", {"campaign_id": "l1", "campaign_mode": "formal", "target_version": "0.151.0"})
            codex_upgrade._assert_project_ledger_consumer("resume", argparse.Namespace(campaign_dir=legacy))
            supervisor._assert_campaign_run_admitted(legacy)
            modern = root / "evidence" / "campaigns" / "m1"
            modern.mkdir(mode=0o700)
            _write_json(modern / "campaign.json", {"campaign_id": "m1", "campaign_mode": "formal", "target_version": "0.154.0"})
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "项目总账不存在"):
                codex_upgrade._assert_project_ledger_consumer("resume", argparse.Namespace(campaign_dir=modern))

    def test_successor_creation_registers_new_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = _data_root(directory, with_ledger=True)
            predecessor = root / "evidence" / "campaigns" / "fresh"
            predecessor.mkdir(mode=0o700)
            _write_json(predecessor / "campaign.json", {"campaign_id": "fresh", "campaign_mode": "formal", "target_version": "0.154.0"})
            successor_dir = root / "evidence" / "campaigns" / "reuse-1"
            arguments = argparse.Namespace(predecessor_campaign_dir=predecessor, campaign_dir=successor_dir, campaign_id="reuse-1")
            with mock.patch.object(codex_upgrade, "_create_successor_campaign_unadmitted", side_effect=lambda a: _fake_create(successor_dir)(a)):
                codex_upgrade.create_successor_campaign(arguments)
            head = ledger.replay_head(root / ledger.LEDGER_DIR_NAME)
            self.assertEqual(sorted(head["registered_campaigns"]), ["reuse-1"])


if __name__ == "__main__":
    unittest.main()
