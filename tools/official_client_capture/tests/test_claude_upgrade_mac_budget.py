"""持久请求上界的竞争准入、真实 COMMIT 结清与历史形态回归。

全部总账位于私有 staging，且明确 fixture_only；只执行账本合同，不启动产品、
捕获服务或正式来源登记。任务级来源失败测试保留空登记表的真实拒绝行为。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import claude_upgrade_mac_budget as budget
from tools.official_client_capture import codex_upgrade_project_ledger as ledger


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", "utf-8")
    path.chmod(0o600)


class MacBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve() / "staging"
        self.data.mkdir(mode=0o700)
        self.root = self.data / ledger.ledger_dir_name("claude")
        ledger.create_project_ledger(self.root, project_id="mac-budget-fixture", client="claude",
            absolute_deadline_utc=_future(48), deadline_approved_by="fixture",
            estimation_policy="none", estimation_policy_approved_by="fixture",
            fixture_only=True, live_request_budget=6, formal_open_limit=64)
        self.campaigns = {name: self._campaign(name) for name in ("campaign-a", "campaign-b")}

    def _campaign(self, name: str) -> Path:
        directory = self.data / name
        directory.mkdir(mode=0o700)
        _write(directory / "campaign.json", {"client": "claude", "campaign_id": name, "campaign_mode": "formal", "target_version": "2.1.226"})
        with ledger.admission_scope(directory, campaign_id=name, campaign_mode="formal", target_version="2.1.226", require=True, client="claude") as admission:
            self.assertIsNotNone(admission)
            admission.register(directory, campaign_id=name, campaign_mode="formal", target_version="2.1.226", deadline_at_utc=_future(24))
        return directory

    def _bound(self, marker: str = "a", amount: int = 4, campaign: str = "campaign-a") -> dict:
        return {"schema_version": budget.BOUND_SCHEMA, "client": "claude", "campaign_id": campaign,
            "candidate_id": "candidate-a", "attempt_id": "attempt-a", "run_nonce": "original-run",
            "candidate_revision": 1, "task_sha256": marker * 64, "producer_run_id": "producer-" + marker,
            "capture_id": "capture-" + marker, "request_budget": amount, "deadline_utc": _future(12),
            "task_binding": {"path": "candidates/candidate-a/attempts/attempt-a/client-evidence/desktop/tasks/task-a/task.json", "sha256": "1" * 64, "bytes": 100},
            "attempt_reservation_binding": {"path": "candidates/candidate-a/attempts/attempt-a/reservation.json", "sha256": "2" * 64, "bytes": 100}}

    def _admission(self, campaign: str = "campaign-a") -> ledger.RuntimeAdmission:
        plan, _raw = ledger._load_plan(self.root)
        return ledger.RuntimeAdmission(self.root, plan, ledger.replay_head(self.root), ledger._campaign_plan(self.campaigns[campaign] / "ledger"))

    def _reserve(self, bound: dict) -> dict:
        with ledger.project_lock(self.root):
            admission = self._admission(bound["campaign_id"])
            return budget.reserve_bound(admission, bound, expected_sequence=admission.head_sequence, expected_head_sha256=admission.head_sha256)

    def _account(self, bound: dict, keys: list[str], *, status: str = "resolved") -> dict:
        operation = "client-import:" + bound["producer_run_id"]
        payload = {"campaign_id": bound["campaign_id"], "subject_kind": "client_capture", "subject_id": bound["producer_run_id"],
            "phase": "candidate", "candidate_id": bound["candidate_id"], "task_sha256": bound["task_sha256"],
            "request": {"status": status, "identity_keys": keys, "estimated_delta": 0}}
        campaign = self.campaigns[bound["campaign_id"]]
        with ledger.campaign_ledger_lock(campaign) as directory:
            batch = ledger.write_batch(directory, operation_id=operation, event_type="reconciliation_committed", payload=payload, source={"kind": "fixture-only", "sha256": "3" * 64})
        ledger.reconcile_project_ledger(self.root, campaign_dir=campaign)
        return {"schema_version": "claude-upgrade-client-accounting/v1", "status": "accounted" if status == "resolved" else "accounting_unresolved",
            "task_sha256": bound["task_sha256"], "identity_keys_sha256": ledger._digest(keys), "operation_id": operation,
            "batch_sha256": batch["batch_sha256"], "project_ledger": str(self.root)}

    def _settle(self, receipt: dict, accounting: dict, keys: list[str], *, campaign: str = "campaign-a") -> dict:
        with ledger.project_lock(self.root):
            return budget.settle_bound(self._admission(campaign), receipt, accounting, identity_keys=keys,
                capture_terminal_binding={"path": "capture-manifest.json", "sha256": "4" * 64, "bytes": 100})

    def test_active_hold_is_durable_and_does_not_count_as_requests(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        (self.root / "head.json").unlink()
        head = ledger.replay_head(self.root)
        self.assertEqual((head["precise_total"], head["estimated_total"], head["remaining_live_requests"]), (0, 0, 6))
        self.assertEqual((head["reserved_live_requests"], head["available_live_requests"]), (4, 2))
        self.assertEqual(budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"])["status"], "reserved")
        self.assertFalse(list(self.data.glob("*/candidates/*/attempts/*")))

    def test_competing_hold_requires_entire_frozen_upper_bound(self) -> None:
        self._reserve(self._bound())
        rejected = self._bound("b", amount=3, campaign="campaign-b")
        before = ledger.replay_head(self.root)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "可用额度不足"):
            self._reserve(rejected)
        self.assertEqual(ledger.replay_head(self.root)["head_sha256"], before["head_sha256"])
        rejected["request_budget"] = 2
        self._reserve(rejected)
        self.assertEqual(ledger.replay_head(self.root)["available_live_requests"], 0)

    def test_same_task_reuses_event_without_refreshing_bound_or_deadline(self) -> None:
        bound = self._bound()
        first = self._reserve(bound)
        before = ledger.replay_head(self.root)
        self.assertEqual(self._reserve(bound), first)
        self.assertEqual(ledger.replay_head(self.root)["sequence"], before["sequence"])
        changed = {**bound, "request_budget": 1}
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "发生冲突"):
            self._reserve(changed)

    def test_different_task_cannot_reuse_producer_or_capture(self) -> None:
        first = self._bound()
        self._reserve(first)
        for field in ("producer_run_id", "capture_id"):
            with self.subTest(field=field):
                second = self._bound("b", 1)
                second[field] = first[field]
                with self.assertRaisesRegex(ledger.ProjectLedgerError, "重复占用"):
                    self._reserve(second)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 4)

    def test_head_cas_advance_rejects_new_reservation(self) -> None:
        with ledger.project_lock(self.root):
            admission = self._admission()
            old_sequence, old_sha = admission.head_sequence, admission.head_sha256
            self._reserve(self._bound("b", 1, "campaign-b"))
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "CAS|head 已并发前进"):
                budget.reserve_bound(admission, self._bound(), expected_sequence=old_sequence, expected_head_sha256=old_sha)

    def test_full_hold_blocks_competing_live_entries_but_not_offline_consumers(self) -> None:
        bound = self._bound(amount=6)
        receipt = self._reserve(bound)
        for command in ledger.LIVE_CONSUMER_COMMANDS:
            with self.subTest(command=command):
                with self.assertRaisesRegex(ledger.ProjectLedgerError, "请求预算"):
                    ledger.assert_campaign_admitted(self.campaigns["campaign-b"], command=command, require=True)
                ledger.assert_campaign_admitted(self.campaigns["campaign-a"], command=command, require=True,
                    reservation_operation_id=budget.reservation_operation_id(bound["task_sha256"]))
        for command in ("seal", "compare", "accept"):
            ledger.assert_campaign_admitted(self.campaigns["campaign-a"], command=command, require=True)
        with ledger.runtime_admission_scope(self.campaigns["campaign-a"], require=True,
                reservation_operation_id=budget.reservation_operation_id(bound["task_sha256"])) as admission:
            admission.reservation_cas(expected_sequence=admission.head_sequence, expected_head_sha256=admission.head_sha256,
                reservation_operation_id=budget.reservation_operation_id(bound["task_sha256"]))
            budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"], dispatch=True)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "原项目锁"):
            budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"], dispatch=True)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "项目请求预算已耗尽"):
            self._campaign("campaign-c")
        with ledger.project_lock(self.root):
            admission = self._admission()
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "项目请求预算"):
                admission.account_candidate_probe(campaign_id=bound["campaign_id"], candidate_id=bound["candidate_id"],
                    dispatch_id="probe-after-full-hold", identity_key="8" * 64, receipt_sha256="9" * 64,
                    response_status=200, expected_head_sha256=admission.head_sha256)

    def test_own_hold_cannot_be_borrowed_by_another_campaign(self) -> None:
        bound = self._bound()
        self._reserve(bound)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "原任务 Campaign"):
            ledger.assert_campaign_admitted(self.campaigns["campaign-b"], command="campaign-run", require=True,
                reservation_operation_id=budget.reservation_operation_id(bound["task_sha256"]))

    def test_expiration_denies_dispatch_and_never_releases_hold(self) -> None:
        bound = self._bound()
        self._reserve(bound)
        after_deadline = ledger._timestamp(bound["deadline_utc"], "fixture") + timedelta(seconds=1)
        head = ledger.replay_head(self.root)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "deadline 已到"):
            budget.available_for_dispatch(head, bound["campaign_id"], budget.reservation_operation_id(bound["task_sha256"]), now=after_deadline)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 4)

    def test_read_only_receipt_verification_does_not_create_lock_or_cache(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        cache = self.root / "head.json"
        cache.unlink()
        with mock.patch.object(ledger, "project_lock", side_effect=AssertionError("reader 不应取项目锁")):
            budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"])
        self.assertFalse(cache.exists())
        corrupted = {**receipt, "reserved_at_utc": _future(1)}
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "真实项目|原项目链"):
            budget.verify_bound_receipt(self.root, corrupted, task_sha256=bound["task_sha256"])

    def test_unresolved_capture_keeps_hold_until_real_resolution_commit(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        keys = ["5" * 64]
        accounting = self._account(bound, keys, status="unresolved")
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "尚未全部精确入账"):
            self._settle(receipt, accounting, keys)
        before = ledger.replay_head(self.root)
        self.assertEqual(before["reserved_live_requests"], 4)
        ledger.append_project_event(self.root, operation_id="resolve-fixture", event_type="accounting_resolved",
            payload={"resolved_operation_id": accounting["operation_id"], "request": {"status": "resolved", "identity_keys": keys, "estimated_delta": 0}}, source_batch_sha256=None)
        self._settle(receipt, accounting, keys)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 0)

    def test_commit_then_crash_reentry_settles_once_and_cannot_redispatch(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        keys = ["5" * 64, "6" * 64]
        accounting = self._account(bound, keys)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 4)
        self.assertEqual(self._settle(receipt, accounting, keys)["status"], "appended")
        head = ledger.replay_head(self.root)
        self.assertEqual((head["precise_total"], head["reserved_live_requests"], head["available_live_requests"]), (2, 0, 4))
        self.assertEqual(self._settle(receipt, accounting, keys)["status"], "duplicate")
        self.assertEqual(ledger.replay_head(self.root)["sequence"], head["sequence"])
        self.assertEqual(self._reserve(bound), receipt)
        self.assertEqual(budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"])["status"], "settled")
        with ledger.project_lock(self.root), self.assertRaisesRegex(ledger.ProjectLedgerError, "活动预算占用"):
            budget.verify_bound_receipt(self.root, receipt, task_sha256=bound["task_sha256"], dispatch=True)

    def test_excess_and_duplicate_native_requests_keep_original_accounting(self) -> None:
        bound = self._bound(amount=2)
        receipt = self._reserve(bound)
        keys = [format(index, "064x") for index in range(1, 8)]
        accounting = self._account(bound, keys)
        self._settle(receipt, accounting, keys)
        self._account(bound, keys)
        head = ledger.replay_head(self.root)
        self.assertEqual((head["precise_total"], head["estimated_total"], head["remaining_live_requests"], head["reserved_live_requests"]), (7, 0, 0, 0))

    def test_project_event_without_real_campaign_commit_cannot_settle(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        keys = ["5" * 64]
        operation = "client-import:" + bound["producer_run_id"]
        ledger.append_project_event(self.root, operation_id=operation, event_type="reconciliation_committed",
            payload={"campaign_id": bound["campaign_id"], "subject_kind": "client_capture", "subject_id": bound["producer_run_id"], "phase": "candidate", "candidate_id": bound["candidate_id"], "task_sha256": bound["task_sha256"], "request": {"status": "resolved", "identity_keys": keys}}, source_batch_sha256="7" * 64)
        accounting = {"operation_id": operation, "task_sha256": bound["task_sha256"], "batch_sha256": "7" * 64, "project_ledger": str(self.root), "identity_keys_sha256": ledger._digest(keys)}
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "真实且内容相同的 Campaign COMMIT"):
            self._settle(receipt, accounting, keys)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 4)

    def test_other_campaign_admission_cannot_release_original_hold(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        accounting = self._account(bound, [])
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "原 Claude RuntimeAdmission"):
            self._settle(receipt, accounting, [], campaign="campaign-b")

    def test_no_registered_source_cannot_authorize_formal_zero_release(self) -> None:
        bound = self._bound()
        receipt = self._reserve(bound)
        task = {"task_sha256": bound["task_sha256"], "execution": {"source_scope": "formal", "capture_root": str(self.data / "absent"), "recorder_id": "not-registered"}}
        with mock.patch.object(contract, "validate_task", return_value=task), mock.patch.dict(contract.SOURCE_ADAPTERS, {}, clear=True):
            with self.assertRaisesRegex(contract.ConfigurationError, "来源|登记"):
                with ledger.project_lock(self.root):
                    budget.settle_task_bound(self._admission(), task, receipt, {}, undispatched=True)
        self.assertEqual(ledger.replay_head(self.root)["reserved_live_requests"], 4)

    def test_production_primitive_requires_original_frozen_task_before_reservation(self) -> None:
        self.data = self.data / "formal-boundary"
        self.data.mkdir(mode=0o700)
        self.root = self.data / ledger.ledger_dir_name("claude")
        ledger.create_project_ledger(self.root, project_id="closed-formal-test", client="claude",
            absolute_deadline_utc=_future(48), deadline_approved_by="fixture",
            estimation_policy="none", estimation_policy_approved_by="fixture",
            fixture_only=False, live_request_budget=6, formal_open_limit=64)
        self.campaigns = {"campaign-a": self._campaign("campaign-a")}
        before = ledger.replay_head(self.root)
        with self.assertRaises(contract.ConfigurationError):
            self._reserve(self._bound())
        self.assertEqual(ledger.replay_head(self.root)["head_sha256"], before["head_sha256"])

    def test_malformed_event_is_rejected_without_uncaught_key_error_or_write(self) -> None:
        before = ledger.replay_head(self.root)
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "字段集合不闭合"):
            ledger.append_project_event(self.root, operation_id="mac-budget:" + "a" * 64,
                event_type=budget.RESERVED_EVENT, payload={}, source_batch_sha256=None)
        self.assertEqual(ledger.replay_head(self.root)["head_sha256"], before["head_sha256"])

    def test_future_accounting_correction_does_not_rewrite_formation_balance(self) -> None:
        earlier = self._bound("b", amount=1, campaign="campaign-b")
        accounting = self._account(earlier, ["5" * 64])
        receipt = self._reserve(self._bound(amount=4))
        event = next(row for row in ledger._load_events(self.root) if row["operation_id"] == accounting["operation_id"])
        corrected = {**event["payload"], "request": {"status": "resolved", "identity_keys": ["5" * 64, "6" * 64, "7" * 64], "estimated_delta": 0}}
        ledger._record_historical_correction(self.root, correction_event_type="reconciliation_corrected", original_operation_id=event["operation_id"], corrected_payload=corrected, reason="夹具验证形成期余额不回溯")
        head = ledger.replay_head(self.root)
        self.assertEqual((head["precise_total"], head["reserved_live_requests"], head["available_live_requests"]), (3, 4, 0))
        with ledger.project_lock(self.root), self.assertRaisesRegex(ledger.ProjectLedgerError, "实际余额不能覆盖"):
            budget.verify_bound_receipt(self.root, receipt, task_sha256=receipt["bound"]["task_sha256"], dispatch=True)

    def test_no_hold_head_keeps_original_fields_and_codex_rejects_claude_event(self) -> None:
        head = ledger.replay_head(self.root)
        self.assertFalse({"live_request_bounds", "reserved_live_requests", "available_live_requests"} & set(head))
        codex_root = self.data / ledger.ledger_dir_name("codex")
        ledger.create_project_ledger(codex_root, project_id="codex-fixture", absolute_deadline_utc=_future(48), deadline_approved_by="fixture", estimation_policy="none", estimation_policy_approved_by="fixture", fixture_only=True, live_request_budget=6)
        before = (codex_root / "head.json").read_bytes()
        bound = self._bound()
        with self.assertRaisesRegex(ledger.ProjectLedgerError, "Codex 或历史总账"):
            ledger.append_project_event(codex_root, operation_id=budget.reservation_operation_id(bound["task_sha256"]), event_type=budget.RESERVED_EVENT, payload={"bound": bound}, source_batch_sha256=None)
        self.assertEqual((codex_root / "head.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
