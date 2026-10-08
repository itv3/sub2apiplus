"""本机预算消费专项：真实私有文件、账头、锁和 UTC；只使用隔离测试来源。

不启动产品、listener、daemon 或网络。不模拟时间或删去 deadline 门禁；仅将
外部 Store/Docker 的 Candidate 门替换为显式测试门。生产 registry 始终为空。
"""

from __future__ import annotations

import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate
from tools.official_client_capture import claude_upgrade_client_receipts as importer
from tools.official_client_capture import claude_upgrade_mac_budget as budget
from tools.official_client_capture import claude_upgrade_mac_candidate_services as services
from tools.official_client_capture import claude_upgrade_mac_client_capture as control
from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture.tests import claude_client_receipt_fixture as f
from tools.official_client_capture.tests.claude_mac_v2_fixture import FixtureSourceAdapter


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Handle:
    """仅此测试作用域的 metadata 消费句柄；绝不创建监听或产品事实。"""

    source_scope = "formal"

    def __init__(self, task, owner):
        self.owner = owner
        self.task_sha256 = task["task_sha256"]
        self.capture_id = task["execution"]["capture_id"]
        self.producer_run_id = task["execution"]["producer_run_id"]
        self.capture_root = Path(task["execution"]["capture_root"])
        self.deployment = task["execution"]["source_deployments"]["capture"]
        self.terminal = copy.deepcopy(owner.terminal)
        self.reservation = None
        self.ready = None

    def reserve(self, task, hold):
        self.owner.calls.append("capture_reserve")
        budget.verify_bound_receipt(Path(task["execution"]["project_ledger"]), hold,
                                    task_sha256=task["task_sha256"])
        self.owner.assert_local_hold(hold)
        if self.reservation is not None:
            return self.reservation
        f.write(self.capture_root / "budget-reservation.json", hold)
        value = {"schema_version": "claude-upgrade-client-reservation/v2", "task_sha256": task["task_sha256"],
                 "candidate": task["candidate"], **{name: task["execution"][name] for name in
                 ("capture_id", "producer_run_id", "endpoint", "request_budget")}, "reserved_at_utc": utc(),
                 "deadline_utc": task["execution"]["request_policy"]["deadline_utc"],
                 "budget_reservation": c.binding(self.capture_root / "budget-reservation.json", self.capture_root),
                 "source_deployments": task["execution"]["source_deployments"]}
        f.write(self.capture_root / "reservation.json", value)
        self.reservation = value
        return value

    def wait_ready(self, task):
        self.owner.calls.append("ready")
        if self.ready is not None:
            return self.ready
        core = {"schema_version": "claude-upgrade-client-capture-ready/v2", "task_sha256": task["task_sha256"],
                "capture_id": self.capture_id, "reservation": c.binding(self.capture_root / "reservation.json", self.capture_root),
                "deployment": self.deployment, "ready_at_utc": utc()}
        proof = f.write(self.capture_root / "observations/ready.json", core)
        value = {**core, "observation": c.binding(proof, self.capture_root)}
        f.write(self.capture_root / "ready.json", value)
        self.ready = value
        return value

    def close_and_sync(self, task):
        self.owner.calls.append("close")
        if self.reservation is None:
            raise RuntimeError("没有真实执行过捕获预约，不能补造零收口")
        for name in ("reservation", "ready"):
            self.terminal[name] = c.binding(self.capture_root / (name + ".json"), self.capture_root)
        f.write(self.capture_root / "capture-manifest.json", self.terminal)
        return self.terminal

    def recover(self, task):
        raise AssertionError("消费测试不能重启捕获或伪造恢复")


class _Adapter(FixtureSourceAdapter):
    """静态测试来源只允许自己的服务/句柄，沿用 fixture_only 原件验证器。"""

    def __init__(self, case):
        self.case = case
        self.calls = []
        self.terminal = c.read_json(case.capture / "capture-manifest.json")
        self.handle = None
        self.product = mock.Mock(fixture_only=True)

    def owns_services(self, value):
        return value is self.case.service

    def capture_factory(self, task, *, attempt_root, services):
        self.calls.append("factory")
        self.case.assertEqual(attempt_root, self.case.attempt)
        self.case.assertIs(services, self.case.service)
        before = ledger.replay_head(self.case.project)
        self.handle = _Handle(task, self)
        self.case.assertEqual(ledger.replay_head(self.case.project)["head_sha256"], before["head_sha256"])
        return self.handle

    def owns_capture(self, value):
        return value is self.handle

    def verify_capture_handle(self, task, handle, attempt_root):
        self.case.assertIs(handle, self.handle)
        self.case.assertEqual(attempt_root, self.case.attempt)
        self._roots(task, handle.capture_root)

    def recorder_factory(self, task, *, bundle_root, services):
        self.calls.append("recorder")
        return self.product

    def owns_recorder(self, value):
        return value is self.product

    def assert_local_hold(self, hold):
        self.case.assertEqual(c.read_json(self.case.run / "budget-reservation.json"), hold)


class MacBudgetConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "staging/data"
        self.data.mkdir(parents=True, mode=0o700)
        self.project = self.data / ledger.ledger_dir_name("claude")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        ledger.create_project_ledger(self.project, project_id="consumer-fixture", client="claude",
            absolute_deadline_utc=future, deadline_approved_by="fixture", estimation_policy="none",
            estimation_policy_approved_by="fixture", fixture_only=True, live_request_budget=10, formal_open_limit=64)
        self.campaign = self.data / "evidence/campaigns/campaign-1"
        self.attempt = self.campaign / "candidates/candidate-1/attempts/attempt-1"
        self.values = {"campaign_id": "campaign-1", "candidate_id": "candidate-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        manifest_path = f.write(self.campaign / "campaign.json", {
            "schema_version": claude_upgrade.CAMPAIGN_SCHEMA, "client": "claude",
            "campaign_id": "campaign-1", "campaign_mode": "formal", "target_version": "2.1.226", "jobs": []})
        digest_path = self.campaign / "campaign.sha256"
        digest_path.write_text(c.binding(manifest_path, self.campaign)["sha256"] + "\n", encoding="ascii")
        digest_path.chmod(0o600)
        f.write(self.attempt / "attempt.json", {**self.values, "status": "awaiting_receipts"})
        self.task_path, self.bundle, self.capture = f.material(self.attempt, "claude-desktop", self.values, issued_at_utc=utc())
        self.task = c.read_json(self.task_path)
        self.run = self.bundle.parent
        f.write(self.attempt / "reservation.json", {"fixture_only": True, "attempt_id": "attempt-1"})
        campaign_deadline = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        f.write(self.campaign / "control/vc/campaign-plan.json", {"campaign_id": "campaign-1", "original_deadline_at_utc": campaign_deadline})
        with ledger.admission_scope(self.campaign, campaign_id="campaign-1", campaign_mode="formal", target_version="2.1.226", require=True, client="claude") as context:
            context.register(self.campaign, campaign_id="campaign-1", campaign_mode="formal", target_version="2.1.226", deadline_at_utc=campaign_deadline)
        plan, _raw = ledger._load_plan(self.project)
        runtime = ledger.RuntimeAdmission(self.project, plan, ledger.replay_head(self.project), ledger._campaign_plan(self.campaign / "ledger"))
        self.service = services.CandidateMacClientServices(self.campaign, runtime)
        self.adapter = _Adapter(self)
        self.patch(c, "SOURCE_ADAPTERS", {f.RECORDER: self.adapter})
        self.patch(claude_upgrade, "_require_formal_campaign", mock.Mock(return_value=c.read_json(self.campaign / "campaign.json")))
        self.patch(claude_upgrade, "_reject_contaminated_campaign", mock.Mock())
        self.patch(claude_upgrade, "_tool_identity", mock.Mock(return_value={"client": "claude"}))
        self.patch(claude_upgrade, "_require_tool_evolution_registered", mock.Mock())
        # 这里只隔离正式 attempt 的全套外部采集 schema 装载；_context 的状态、
        # 冻结 task/原预约字节、实际锁和全部预算/时间门禁仍由生产代码核验。
        self.patch(claude_upgrade, "_load_capture_attempt", mock.Mock(return_value=(self.attempt, c.read_json(self.attempt / "attempt.json"))))
        self.gate = self.patch(candidate, "formal_admission", mock.Mock())
        lease = claude_upgrade.CampaignLease(self.campaign, phase="candidate", candidate_id="candidate-1", attempt_id="attempt-1",
            deadline=incremental_recovery.WallClockDeadline(3600, label="预算消费专项"), command="client-run", campaign_id="campaign-1")
        lease._attached_parent, lease._supervisor = True, mock.Mock()
        lease._payload = {"state": "active", "campaign_id": "campaign-1", "attempt_id": "attempt-1", "deadline_at_utc": future}
        self.patch(claude_upgrade, "_ACTIVE_CAMPAIGN_LEASE", lease)
        (self.capture / "capture-manifest.json").unlink()
        self.index = self.attempt / "client-evidence/reservations" / (self.task["execution"]["producer_run_id"] + ".json")
        self.index.unlink()

    def patch(self, target, name, value):
        patch = mock.patch.object(target, name, value)
        patch.start()
        self.addCleanup(patch.stop)
        return value

    def reserve_ready(self):
        with importer._client_control_lock(self.campaign):
            self.service.reserve(self.task, self.attempt)
            control.register_reservation(self.campaign, self.attempt, self.task_path,
                                         c.binding(self.capture / "reservation.json", self.capture))
            self.service.wait_capture_ready(self.task)
        return budget.read_task_bound_receipt(self.project, self.task)

    def fit_recorded_fixture_window(self, start, end):
        """只调整离线产品观测夹具，使其发生在真实 ready 之后和原 deadline 之前。

        冻结 task、真实预算、预约与 ready 一字不改；观测及其摘要一起重建，
        全部生产来源、时序、provenance 和账务校验仍实际执行。
        """
        issued = c.timestamp(self.task["issued_at_utc"])

        def transform(value):
            if isinstance(value, list):
                return [transform(item) for item in value]
            if isinstance(value, dict):
                return {key: (start + (c.timestamp(item) - issued) / 12 * ((end - start).total_seconds())).isoformat()
                        if key.endswith("at_utc") else transform(item) for key, item in value.items()}
            return value

        originals = []
        for root in (self.bundle, self.capture):
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
                    continue
                if root == self.capture and (path.name in {"reservation.json", "ready.json", "budget-reservation.json", "candidate-budget-reservation.json"}):
                    continue
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                originals.append((root, path, [transform(row) for row in rows]))
        for _root, path, rows in originals:
            path.write_bytes(b"".join(c.canonical(row) + b"\n" for row in rows))

        def rebind(root, value):
            if isinstance(value, list):
                return [rebind(root, item) for item in value]
            if isinstance(value, dict):
                if set(value) == {"path", "sha256", "bytes"}:
                    return c.binding(root / value["path"], root)
                return {key: rebind(root, item) for key, item in value.items()}
            return value

        # standalone observations 先写好，再绑定进程/连接、journal 和恢复收据；
        # 最后重建包清单，保持每一层的实际原件与摘要一致。
        for root, path, rows in sorted(originals, key=lambda item: (item[1].name != "process-session.json", item[1].name in {"recorder-execution.json", "state-restoration.json", "source-manifest.json"})):
            path.write_bytes(b"".join(c.canonical(rebind(root, row)) + b"\n" for row in rows))
        manifest = c.read_json(self.bundle / "source-manifest.json")
        manifest["files"] = [c.binding(path, self.bundle) for path in sorted(self.bundle.rglob("*")) if path.is_file() and path.name != "source-manifest.json"]
        f.write(self.bundle / "source-manifest.json", manifest)
        self.adapter.handle.terminal = c.read_json(self.capture / "capture-manifest.json") if (self.capture / "capture-manifest.json").exists() else {
            **self.adapter.handle.terminal,
            **{key: c.binding(self.capture / name, self.capture) for key, name in
               (("requests", "requests.jsonl"), ("connections", "connections.jsonl"), ("transport_map", "transport-map.jsonl"))}}

    def test_real_hold_precedes_capture_and_own_full_hold_rechecks(self):
        receipt = self.reserve_ready()
        head = ledger.replay_head(self.project)
        self.assertEqual((head["precise_total"], head["reserved_live_requests"], head["available_live_requests"]), (0, 10, 0))
        self.assertEqual(self.adapter.calls, ["factory", "capture_reserve", "ready"])
        self.service.recheck_dispatch(self.task)
        self.assertEqual(self.adapter.calls[-1], "ready")
        self.assertGreaterEqual(self.gate.call_count, 2)
        self.assertEqual(budget.verify_bound_receipt(self.project, receipt, task_sha256=self.task["task_sha256"])["status"], "reserved")

    def test_missing_registry_and_factory_leave_no_hold(self):
        before = ledger.replay_head(self.project)["head_sha256"]
        for registry in ({}, {f.RECORDER: FixtureSourceAdapter()}):
            with self.subTest(registry=bool(registry)), mock.patch.dict(c.SOURCE_ADAPTERS, registry, clear=True):
                with importer._client_control_lock(self.campaign), self.assertRaises(ValueError):
                    self.service.reserve(self.task, self.attempt)
        self.assertEqual(ledger.replay_head(self.project)["head_sha256"], before)
        self.assertFalse((self.run / "budget-reservation.json").exists())
        self.assertFalse(self.adapter.calls)

    def test_foreign_handle_and_current_native_capture_cannot_be_relabelled(self):
        from tools.official_client_capture.claude_upgrade_mac_capture_client import CaptureClient
        for raw in (object(), object.__new__(CaptureClient)):
            with self.subTest(kind=type(raw).__name__), mock.patch.object(self.adapter, "capture_factory", return_value=raw):
                with importer._client_control_lock(self.campaign), self.assertRaises(ValueError):
                    self.service.reserve(self.task, self.attempt)
        self.assertNotIn("live_request_bounds", ledger.replay_head(self.project))

    def test_same_task_reserve_reuses_event_and_deadline(self):
        self.reserve_ready()
        before = ledger.replay_head(self.project)
        with importer._client_control_lock(self.campaign):
            self.service.reserve(self.task, self.attempt)
        self.assertEqual(ledger.replay_head(self.project)["sequence"], before["sequence"])
        self.assertEqual(self.task["execution"]["request_policy"]["deadline_utc"],
                         c.read_json(self.run / "budget-reservation.json")["bound"]["deadline_utc"])

    def test_read_helper_never_creates_lock_or_cache_and_frozen_drift_refuses(self):
        self.reserve_ready()
        (self.project / "head.json").unlink()
        with mock.patch.object(ledger, "project_lock", side_effect=AssertionError("只读不能取锁")):
            budget.read_task_bound_receipt(self.project, self.task)
        self.assertFalse((self.project / "head.json").exists())
        with (self.attempt / "reservation.json").open("ab") as handle:
            handle.write(b" ")
        with self.assertRaisesRegex(ValueError, "漂移"):
            budget.read_task_bound_receipt(self.project, self.task)

    def test_missing_event_cannot_downgrade_local_original(self):
        f.write(self.run / "budget-reservation.json", {"not": "a real event"})
        with self.assertRaisesRegex(ValueError, "禁止降级"):
            budget.read_task_bound_receipt(self.project, self.task)

    def test_hold_event_before_local_publication_is_reconstructed_without_dispatch(self):
        original = importer._write
        def interrupt(path, value):
            if Path(path).name == "budget-reservation.json":
                raise RuntimeError("预算原件发布前断点")
            return original(path, value)
        with mock.patch.object(importer, "_write", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "发布前断点"):
                control.run_client(self.task_path, self.attempt, services=self.service)
        receipt = budget.read_task_bound_receipt(self.project, self.task)
        self.assertIsNotNone(receipt)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)
        self.assertFalse((self.run / "budget-reservation.json").exists())
        self.assertNotIn("capture_reserve", self.adapter.calls)
        self.assertNotIn("recorder", self.adapter.calls)

    def test_intent_publication_failure_keeps_index_and_closes_once(self):
        original = importer._write
        def interrupt(path, value):
            if Path(path).name == "dispatch-intent.json":
                raise RuntimeError("派发意图发布断点")
            return original(path, value)
        with mock.patch.object(importer, "_write", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "意图发布"):
                control.run_client(self.task_path, self.attempt, services=self.service)
        self.assertTrue(self.index.exists())
        self.assertEqual(self.adapter.calls.count("close"), 1)
        self.assertNotIn("recorder", self.adapter.calls)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)

    def test_other_task_cannot_borrow_producer_hold(self):
        self.reserve_ready()
        other = copy.deepcopy(self.task)
        other["task_nonce"] = "different-task"
        other["task_sha256"] = c.digest({k: v for k, v in other.items() if k != "task_sha256"})
        with self.assertRaisesRegex(ValueError, "不能借用"):
            budget.read_task_bound_receipt(self.project, other)

    def test_ready_boolean_cannot_replace_live_reply_or_release_hold(self):
        self.reserve_ready()
        with mock.patch.object(self.adapter.handle, "wait_ready", return_value=True):
            with importer._client_control_lock(self.campaign), self.assertRaisesRegex(ValueError, "实时"):
                self.service.wait_capture_ready(self.task)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)

    def test_old_intent_and_terminal_reentry_never_open_factory(self):
        self.reserve_ready()
        self.adapter.calls.clear()
        f.write(self.run / "dispatch-intent.json", {"task_sha256": self.task["task_sha256"]})
        result = control.run_client(self.task_path, self.attempt, services=self.service)
        self.assertEqual(result["status"], "accounting_unresolved")
        self.assertFalse(self.adapter.calls)
        (self.run / "dispatch-intent.json").unlink()
        self.service.close_capture(self.task)
        self.adapter.calls.clear()
        self.assertEqual(control.run_client(self.task_path, self.attempt, services=self.service)["status"], "accounting_unresolved")
        self.assertFalse(self.adapter.calls)

    def test_terminal_without_new_hold_never_opens_factory(self):
        for name in ("capture-manifest.json", "undispatched.json", "native-capture-manifest.json"):
            with self.subTest(terminal=name):
                path = f.write(self.capture / name, {"unknown_terminal": True})
                self.assertEqual(control.run_client(self.task_path, self.attempt, services=self.service)["status"], "accounting_unresolved")
                self.assertFalse(self.adapter.calls)
                self.assertNotIn("live_request_bounds", ledger.replay_head(self.project))
                path.unlink()

    def test_reserve_failure_closes_once_after_unlock_and_preserves_hold(self):
        original = _Handle.reserve
        def failed(handle, task, hold):
            original(handle, task, hold)
            raise RuntimeError("原预约通道故障")
        with mock.patch.object(_Handle, "reserve", failed), mock.patch.object(self.service, "close_capture", side_effect=OSError("收口通道故障")) as closing:
            with self.assertRaisesRegex(RuntimeError, "原预约") as caught:
                control.run_client(self.task_path, self.attempt, services=self.service)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        closing.assert_called_once_with(self.task)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)
        self.assertFalse((self.run / "dispatch-intent.json").exists())

    def test_ready_failure_real_close_has_no_nested_campaign_lock(self):
        self.reserve_ready()
        with mock.patch.object(self.service, "wait_capture_ready", side_effect=RuntimeError("实时 ready 失败")):
            with self.assertRaisesRegex(RuntimeError, "实时 ready"):
                control.run_client(self.task_path, self.attempt, services=self.service)
        self.assertEqual(self.adapter.calls.count("close"), 1)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)

    def test_close_authenticates_terminal_but_does_not_settle(self):
        self.reserve_ready()
        self.service.close_capture(self.task)
        before = ledger.replay_head(self.project)
        self.assertEqual(before["reserved_live_requests"], 10)
        self.service.close_capture(self.task)
        self.assertEqual(self.adapter.calls.count("close"), 1)
        self.assertEqual(ledger.replay_head(self.project)["head_sha256"], before["head_sha256"])

    def test_actual_expiry_denies_dispatch_but_allows_close_and_exact_settlement(self):
        # 初始未预约夹具只为公共 setUp 构造；本例只保留唯一过期任务，
        # 避免另一个未取证任务使真实 Campaign provenance 变成未决。
        self.task_path.unlink()
        issued = (datetime.now(timezone.utc) - timedelta(seconds=297)).isoformat()
        self.task_path, self.bundle, self.capture = f.material(self.attempt, "claude-desktop", self.values,
                                                              nonce="expiring-consumer", issued_at_utc=issued)
        self.task = c.read_json(self.task_path)
        self.run = self.bundle.parent
        self.adapter.terminal = c.read_json(self.capture / "capture-manifest.json")
        (self.capture / "capture-manifest.json").unlink()
        self.index = self.attempt / "client-evidence/reservations" / (self.task["execution"]["producer_run_id"] + ".json")
        self.index.unlink()
        self.reserve_ready()
        deadline = c.timestamp(self.task["execution"]["request_policy"]["deadline_utc"])
        start = datetime.now(timezone.utc) + timedelta(milliseconds=10)
        self.assertLess(start + timedelta(seconds=1), deadline)
        self.fit_recorded_fixture_window(start, start + timedelta(seconds=1))
        while datetime.now(timezone.utc) <= deadline:
            time.sleep(min(0.05, max((deadline - datetime.now(timezone.utc)).total_seconds(), 0.001)))
        with self.assertRaisesRegex(ValueError, "deadline 已到"):
            self.service.recheck_dispatch(self.task)
        self.service.close_capture(self.task)
        with importer._client_control_lock(self.campaign):
            context = budget.accounting_runtime(self.campaign, self.task)
        self.assertEqual(context.root, self.project)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)
        self.assertNotIn("recorder", self.adapter.calls)
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            self.assertEqual(importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)["status"], "imported")
        head = ledger.replay_head(self.project)
        self.assertEqual((head["reserved_live_requests"], head["precise_total"]), (0, 1))
        self.assertNotIn("recorder", self.adapter.calls)

    def test_exact_import_commit_settles_before_success_and_replay_is_duplicate(self):
        self.reserve_ready()
        self.service.close_capture(self.task)
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            result = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)
            second = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)
        self.assertEqual(result, second)
        self.assertEqual(result["status"], "imported")
        head = ledger.replay_head(self.project)
        self.assertEqual((head["reserved_live_requests"], head["precise_total"]), (0, 1))
        events = ledger._load_events(self.project)
        types = [e["event_type"] for e in events]
        self.assertLess(types.index("reconciliation_committed"), types.index(budget.SETTLED_EVENT))
        self.assertEqual(types.count(budget.SETTLED_EVENT), 1)
        self.adapter.calls.clear()
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            self.assertEqual(control.run_client(self.task_path, self.attempt, services=self.service)["status"], "imported")
        self.assertFalse(self.adapter.calls)
        (self.run / "run.json").unlink()
        self.assertEqual(control.run_client(self.task_path, self.attempt, services=self.service)["status"], "accounting_unresolved")
        self.assertFalse(self.adapter.calls)

    def test_committed_accounting_survives_settlement_interruption_and_recovers(self):
        self.reserve_ready()
        self.service.close_capture(self.task)
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            with mock.patch.object(budget, "settle_task_bound", side_effect=RuntimeError("结清前断点")):
                with self.assertRaisesRegex(RuntimeError, "结清前"):
                    importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)
            before = (self.run / "accounting.json").read_bytes()
            self.assertFalse((self.attempt / "client-evidence/claude-desktop.json").exists())
            self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)
            self.assertEqual(importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)["status"], "imported")
        self.assertEqual((self.run / "accounting.json").read_bytes(), before)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 0)

    def test_failed_product_can_settle_exact_requests_without_success_receipt(self):
        self.reserve_ready()
        self.service.close_capture(self.task)
        f.write(self.run / "client-run-failure.json", {"schema_version": "claude-upgrade-client-run-failure/v2",
                "task_sha256": self.task["task_sha256"], "producer_run_id": self.task["execution"]["producer_run_id"], "error_type": "FixtureFailure"})
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            result = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)
        self.assertEqual(result["status"], "client_run_failed")
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 0)
        self.assertFalse((self.attempt / "client-evidence/claude-desktop.json").exists())

    def test_truncated_capture_cannot_release_after_unresolved_commit(self):
        self.reserve_ready()
        self.service.close_capture(self.task)
        terminal = c.read_json(self.capture / "capture-manifest.json")
        terminal["status"] = "truncated"
        f.write(self.capture / "capture-manifest.json", terminal)
        with mock.patch.object(claude_upgrade, "verify_mac_source_v2", wraps=c.verify_client_bundle):
            result = importer.import_bundle(self.campaign, self.attempt, "claude-desktop", self.task_path, self.bundle)
        self.assertEqual(result["status"], "accounting_unresolved")
        self.assertEqual(result["accounting"]["status"], "accounting_unresolved")
        types = [e["event_type"] for e in ledger._load_events(self.project)]
        self.assertIn("reconciliation_committed", types)
        self.assertNotIn(budget.SETTLED_EVENT, types)
        with self.assertRaisesRegex(ValueError, "截断"):
            budget._terminal_inputs(self.task, undispatched=False)
        self.assertEqual(ledger.replay_head(self.project)["reserved_live_requests"], 10)
        self.assertFalse((self.attempt / "client-evidence/claude-desktop.json").exists())


if __name__ == "__main__":
    unittest.main()
