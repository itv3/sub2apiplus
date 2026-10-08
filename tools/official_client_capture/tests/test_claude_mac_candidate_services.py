"""候选服务的真实总账／Candidate 接口与缺能力零启动回归；不运行产品或联网。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_mac_candidate_services as services
from tools.official_client_capture import claude_upgrade_mac_client_capture as control
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.tests import claude_client_receipt_fixture as f


class _TestSource:
    """只在本模块测试作用域存在，不能签正式来源或提供真实后端。"""

    def owns_services(self, service: object) -> bool:
        return isinstance(service, services.CandidateMacClientServices)


class MacCandidateServicesTests(unittest.TestCase):
    def setUp(self) -> None:
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        self.root = Path(outer.name).resolve()
        self.campaign = self.root / "staging/data/evidence/campaigns/campaign-1"
        self.attempt = self.campaign / "candidates/candidate-1/attempts/attempt-1"
        self.values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        f.write(self.campaign / "campaign.json", {"campaign_id": "campaign-1", "client": "claude"})
        f.write(self.attempt / "attempt.json", {**self.values, "status": "awaiting_receipts"})
        self.task_path, self.bundle, self.capture_root = f.material(self.attempt, "claude-desktop", self.values)
        self.task = c.read_json(self.task_path)
        now = datetime.now(timezone.utc)
        self.task["issued_at_utc"] = now.isoformat()
        self.task["execution"]["source_scope"] = "formal"
        self.task["execution"]["request_policy"]["deadline_utc"] = (now + timedelta(minutes=30)).isoformat()
        self.task["task_sha256"] = c.digest({k: v for k, v in self.task.items() if k != "task_sha256"})
        f.write(self.task_path, self.task)
        self.project = Path(self.task["execution"]["project_ledger"])
        with ledger.admission_scope(self.campaign, campaign_id="campaign-1", campaign_mode="formal",
                                    target_version="2.1.226", require=True, client="claude") as admission:
            ledger.register_campaign(self.project, self.campaign, campaign_id="campaign-1", campaign_mode="formal",
                                     target_version="2.1.226", deadline_at_utc=None,
                                     admission_head_sha256=admission.admission_head_sha256)
        plan, _raw = ledger._load_plan(self.project)
        head = ledger.replay_head(self.project)
        campaign_plan = ledger._campaign_plan(self.campaign / ledger.CAMPAIGN_LEDGER_DIR_NAME)
        runtime = ledger.RuntimeAdmission(self.project, plan, head, campaign_plan)
        self.service = services.CandidateMacClientServices(self.campaign, runtime)
        self.lease = claude_upgrade.CampaignLease(
            self.campaign, phase="candidate", candidate_id="candidate-1", attempt_id="attempt-1",
            deadline=incremental_recovery.WallClockDeadline(3600, label="Mac 服务测试"), command="client-run",
            campaign_id="campaign-1")
        self.lease._attached_parent = True
        self.lease._supervisor = mock.Mock()
        self.lease._payload = {"state": "active", "campaign_id": "campaign-1", "attempt_id": "attempt-1",
                               "deadline_at_utc": (now + timedelta(hours=1)).isoformat()}
        self.registry = mock.patch.dict(c.SOURCE_ADAPTERS, {f.RECORDER: _TestSource()}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)
        self.patch(claude_upgrade, "_ACTIVE_CAMPAIGN_LEASE", self.lease)
        self.patch(claude_upgrade, "_require_formal_campaign", mock.Mock(return_value={"campaign_id": "campaign-1", "client": "claude"}))
        self.patch(claude_upgrade, "_reject_contaminated_campaign", mock.Mock())
        self.patch(claude_upgrade, "_tool_identity", mock.Mock(return_value={"client": "claude"}))
        self.evolution = self.patch(claude_upgrade, "_require_tool_evolution_registered", mock.Mock())
        self.candidate_admission = self.patch(candidate, "formal_admission", mock.Mock())
        self.time_budget = self.patch(capture, "_require_capture_budget", mock.Mock())
        # 本套只验接线，完整 Campaign 三层计时与 Store/Docker 门禁由原合同套件验收；总账和 head CAS 使用真实现。
        self.patch(ledger, "_campaign_deadline_problems", mock.Mock(return_value=[]))

    def patch(self, target: object, name: str, value: object) -> object:
        patch = mock.patch.object(target, name, value)
        patch.start()
        self.addCleanup(patch.stop)
        return value

    def admit(self) -> None:
        with ledger.project_lock(self.project):
            self.service.admit(self.task, self.attempt)

    def test_unregistered_source_stops_before_candidate_or_product(self) -> None:
        with mock.patch.dict(c.SOURCE_ADAPTERS, {}, clear=True), self.assertRaisesRegex(ValueError, "来源未登记"):
            self.admit()
        self.candidate_admission.assert_not_called()
        self.assertFalse((self.bundle.parent / "dispatch-intent.json").exists())

    def test_foreign_service_is_rejected(self) -> None:
        with mock.patch.object(_TestSource, "owns_services", return_value=False), self.assertRaisesRegex(ValueError, "不属于"):
            self.admit()
        self.candidate_admission.assert_not_called()

    def test_actual_candidate_identity_time_and_lease_interfaces_are_used(self) -> None:
        self.admit()
        self.evolution.assert_called_once()
        self.candidate_admission.assert_called_once()
        args, kwargs = self.candidate_admission.call_args
        self.assertEqual((args[0], args[2]), (self.campaign, "candidate-1"))
        self.assertIs(kwargs["deadline"], self.lease.deadline)
        self.time_budget.assert_called_once_with(self.lease.deadline, operation="mac-client:reservation-admission", reservation=True)

    def test_missing_or_foreign_candidate_lease_is_rejected(self) -> None:
        for value in (None, self.lease):
            with self.subTest(lease=value), mock.patch.object(claude_upgrade, "_ACTIVE_CAMPAIGN_LEASE", value):
                if value is not None:
                    value.candidate_id = "candidate-other"
                with self.assertRaisesRegex(ValueError, "lease"):
                    self.admit()

    def test_lease_must_bind_same_attempt_and_parent_supervisor(self) -> None:
        self.lease._payload["attempt_id"] = "other-attempt"
        with self.assertRaisesRegex(ValueError, "原候选 attempt"):
            self.admit()
        self.lease._payload["attempt_id"] = "attempt-1"
        self.lease._attached_parent = False
        self.lease._descriptor = 1
        with self.assertRaisesRegex(ValueError, "父监督器"):
            self.admit()

    def test_task_deadline_cannot_exceed_lease_or_be_restarted(self) -> None:
        now = datetime.now(timezone.utc)
        for issued, deadline, message in (
                (now, now + timedelta(hours=2), "超过原监督 lease"),
                (now - timedelta(hours=1), now - timedelta(minutes=1), "冻结 deadline 已到")):
            with self.subTest(deadline=deadline):
                self.task["issued_at_utc"] = issued.isoformat()
                self.task["execution"]["request_policy"]["deadline_utc"] = deadline.isoformat()
                self.task["task_sha256"] = c.digest({k: v for k, v in self.task.items() if k != "task_sha256"})
                with self.assertRaisesRegex(ValueError, message):
                    self.admit()

    def test_current_attempt_build_originals_are_verified(self) -> None:
        receipt = self.campaign / "candidates/candidate-1/build-receipt.json"
        raw = c.read_json(receipt)
        raw["image"]["manifest_digest"] = "sha256:" + "b" * 64
        f.write(receipt, raw)
        with self.assertRaisesRegex(ValueError, "构建收据"):
            self.admit()
        self.candidate_admission.assert_not_called()

    def test_no_project_lock_or_forged_runtime_plan_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "项目锁"):
            self.service.admit(self.task, self.attempt)
        self.service.runtime_admission.plan["fixture_only"] = False
        with self.assertRaisesRegex(ValueError, "受管原件"):
            self.admit()

    def test_original_candidate_gate_failure_propagates(self) -> None:
        self.candidate_admission.side_effect = ValueError("原 Candidate gate 拒绝")
        with self.assertRaisesRegex(ValueError, "原 Candidate gate"):
            self.admit()
        self.time_budget.assert_not_called()

    def test_time_budget_failure_cannot_be_bypassed(self) -> None:
        self.time_budget.side_effect = ValueError("原清理时间预算不足")
        with self.assertRaisesRegex(ValueError, "原清理時間|原清理时间"):
            self.admit()

    def test_missing_factory_is_not_claimed_as_budget_reservation(self) -> None:
        before = ledger.replay_head(self.project)
        with ledger.project_lock(self.project), self.assertRaisesRegex(services.CandidateServicesUnavailable, "工厂"):
            self.service.reserve(self.task, self.attempt)
        self.assertEqual(ledger.replay_head(self.project)["head_sha256"], before["head_sha256"])
        self.assertFalse((self.bundle.parent / "dispatch-intent.json").exists())

    def test_real_head_change_between_admission_and_cas_is_rejected(self) -> None:
        runtime = self.service.runtime_admission
        original_cas = runtime.reservation_cas

        def register_before_cas(**kwargs: object) -> dict:
            campaign = self.campaign.parent / "campaign-2"
            f.write(campaign / "campaign.json", {"campaign_id": "campaign-2", "client": "claude"})
            ledger.register_campaign(self.project, campaign, campaign_id="campaign-2", campaign_mode="formal",
                                     target_version="2.1.226", deadline_at_utc=None,
                                     admission_head_sha256=runtime.head_sha256)
            return original_cas(**kwargs)

        with mock.patch.object(runtime, "reservation_cas", side_effect=register_before_cas):
            f.write(self.attempt / "reservation.json", {"fixture_only": True, "original_attempt": "attempt-1"})
            (self.capture_root / "capture-manifest.json").unlink()
            # 这个窄用例只隔离工厂构造，真实预算原语与 CAS 仍运行；完整静态工厂
            # 的来源／所有权由预算消费专项核验，不能据此登记正式来源。
            with mock.patch.object(self.service, "_capture", return_value=mock.Mock()), ledger.project_lock(self.project), self.assertRaisesRegex(ValueError, "head 已并发前进"):
                self.service.reserve(self.task, self.attempt)
        self.assertEqual(len(ledger.replay_head(self.project)["registered_campaigns"]), 2)
        self.assertFalse((self.bundle.parent / "dispatch-intent.json").exists())

    def test_all_missing_live_methods_fail_closed(self) -> None:
        for call in (lambda: self.service.wait_capture_ready(self.task), lambda: self.service.recorder(self.task, self.bundle),
                     lambda: self.service.close_capture(self.task)):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_run_client_stops_before_intent_recorder_or_import(self) -> None:
        (self.capture_root / "capture-manifest.json").unlink()
        with mock.patch.object(self.service, "recorder") as recorder, mock.patch.object(self.service, "close_capture") as close:
            with self.assertRaises(services.CandidateServicesUnavailable):
                control.run_client(self.task_path, self.attempt, services=self.service)
            recorder.assert_not_called()
            close.assert_called_once_with(self.task)
        self.assertFalse((self.bundle.parent / "dispatch-intent.json").exists())

    def test_dispatch_recheck_reuses_original_gate_failure(self) -> None:
        self.candidate_admission.side_effect = ValueError("准备期间 Candidate 身份漂移")
        with self.assertRaisesRegex(ValueError, "身份漂移"):
            self.service.recheck_dispatch(self.task)


if __name__ == "__main__":
    unittest.main()
