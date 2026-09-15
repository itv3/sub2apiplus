"""项目总账：CAS、operation 幂等、head 缓存、batch 提交与补齐、admission、blocked 白名单、修复收据。"""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_project_ledger as ledger
from tools.official_client_capture import codex_upgrade_root_cause as root_cause


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _future(hours: int = 48) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


def _campaign(root: Path, campaign_id: str, *, staging: bool = False) -> Path:
    base = root / "staging" / "fixtures" if staging else root / "evidence" / "campaigns"
    campaign_dir = base / campaign_id
    campaign_dir.mkdir(parents=True, mode=0o700)
    for parent in (root / "staging", root / "staging" / "fixtures", root / "evidence", root / "evidence" / "campaigns"):
        if parent.exists():
            parent.chmod(0o700)
    _write_json(campaign_dir / "campaign.json", {"campaign_id": campaign_id, "campaign_mode": "formal", "target_version": "0.154.0"})
    return campaign_dir


def _create(root: Path, **overrides: object) -> Path:
    options = {
        "project_id": "codex-0154-upgrade",
        "absolute_deadline_utc": _future(),
        "deadline_approved_by": "老板",
        "estimation_policy": "upper_bound_from_sibling_or_turn_ratio",
        "estimation_policy_approved_by": "老板",
        "fixture_only": False,
        "initial_identity_keys": ["k-initial-1", "k-initial-2"],
        "initial_estimated_count": 30,
    }
    options.update(overrides)
    ledger_root = root / ledger.LEDGER_DIR_NAME
    ledger.create_project_ledger(ledger_root, **options)
    return ledger_root


def _register(root: Path, campaign_dir: Path, campaign_id: str, *, mode: str = "formal", deadline: str | None = None, now: datetime | None = None) -> dict:
    with ledger.admission_scope(campaign_dir, campaign_id=campaign_id, campaign_mode=mode, target_version="0.154.0", require=True, now=now) as admission:
        assert admission is not None
        return admission.register(campaign_dir, campaign_id=campaign_id, campaign_mode=mode, target_version="0.154.0", deadline_at_utc=deadline)


def _reconciliation(campaign_dir: Path, *, operation_id: str, keys: list[str], status: str = "resolved", root_cause_id: str | None = None, estimated: int = 0) -> None:
    with ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
        payload = {"campaign_id": campaign_dir.name, "request": {"status": status, "identity_keys": keys, "estimated_delta": estimated, "provenance_receipt_sha256": "b" * 64}}
        if root_cause_id is not None:
            payload["root_cause"] = {"root_cause_id": root_cause_id}
        ledger.write_batch(ledger_dir, operation_id=operation_id, event_type="reconciliation_committed", payload=payload, source={"kind": "campaign_event", "sha256": "c" * 64}, fragment_size=2)


def _concurrent_register(args: tuple[str, str, str]) -> str:
    root_text, campaign_text, campaign_id = args
    try:
        with ledger.admission_scope(Path(campaign_text), campaign_id=campaign_id, campaign_mode="formal", target_version="0.154.0", require=True) as admission:
            assert admission is not None
            admission.register(Path(campaign_text), campaign_id=campaign_id, campaign_mode="formal", target_version="0.154.0", deadline_at_utc=None)
        return "registered"
    except ledger.ProjectLedgerError as error:
        return f"rejected:{error}"


class ProjectLedgerTests(unittest.TestCase):
    def test_plan_freezes_approvals_and_head_replays_from_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            plan = json.loads((ledger_root / "plan.json").read_text("utf-8"))
            self.assertEqual(plan["deadline_approved_by"], "老板")
            self.assertEqual(plan["initial_identity_key_count"], 2)
            self.assertEqual(plan["root_cause_codes_sha256"], root_cause.load_codes()["codes_sha256"])
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["sequence"], 0)
            self.assertEqual(head["head_sha256"], plan["plan_sha256"])
            self.assertEqual((head["precise_total"], head["estimated_total"]), (2, 30))
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已存在"):
                _create(root)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "批准人"):
                ledger.create_project_ledger(root / "other", project_id="p", absolute_deadline_utc=_future(), deadline_approved_by=" ", estimation_policy="none", estimation_policy_approved_by="老板", fixture_only=False)

    def test_registration_batch_and_event_then_consumers_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            result = _register(root, campaign_dir, "c1", deadline=_future(24))
            self.assertEqual(result["status"], "appended")
            self.assertTrue((campaign_dir / "ledger" / "outbox" / "batch-000001" / "COMMIT").is_file())
            head = ledger.replay_head(ledger_root)
            self.assertIn("c1", head["registered_campaigns"])
            self.assertEqual(head["registered_campaigns"]["c1"]["registration_batch_sha256"], result["batch_sha256"])
            for command in sorted(ledger.CONSUMER_COMMANDS):
                report = ledger.assert_campaign_admitted(campaign_dir, command=command, require=True)
                self.assertEqual(report["campaign_id"], "c1")
            # 注册重试幂等
            again = _register_after_registered(root, campaign_dir)
            self.assertEqual(again["status"], "duplicate")

    def test_cas_conflict_and_duplicate_operation_with_different_payload_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            head = ledger.replay_head(ledger_root)
            new_head, status = ledger.append_project_event(ledger_root, operation_id="op-1", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": ["k1"], "estimated_delta": 0}}, source_batch_sha256="d" * 64, expected_head_sha256=head["head_sha256"])
            self.assertEqual(status, "appended")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "CAS 失败"):
                ledger.append_project_event(ledger_root, operation_id="op-2", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0}}, source_batch_sha256="d" * 64, expected_head_sha256=head["head_sha256"])
            _head, status = ledger.append_project_event(ledger_root, operation_id="op-1", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": ["k1"], "estimated_delta": 0}}, source_batch_sha256="d" * 64)
            self.assertEqual(status, "duplicate")
            with self.assertRaisesExpected(ledger.ProjectLedgerError, "重复且类型或 payload 不同"):
                ledger.append_project_event(ledger_root, operation_id="op-1", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": ["k2"], "estimated_delta": 0}}, source_batch_sha256="d" * 64)

    def assertRaisesExpected(self, exception: type[BaseException], pattern: str):  # noqa: N802 - unittest 风格
        return self.assertRaisesRegex(exception, pattern)

    def test_head_cache_ahead_is_rejected_and_missing_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            ledger.append_project_event(ledger_root, operation_id="op-1", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": ["k1"], "estimated_delta": 0}}, source_batch_sha256="d" * 64)
            cache = ledger_root / "head.json"
            cache.unlink()
            head = ledger.replay_head(ledger_root)
            self.assertTrue(cache.is_file())
            self.assertEqual(head["sequence"], 1)
            payload = json.loads(cache.read_text("utf-8"))
            payload["sequence"] = 5
            _write_json(cache, payload)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "超前"):
                ledger.replay_head(ledger_root)
            payload["sequence"] = 1
            payload["head_sha256"] = "e" * 64
            _write_json(cache, payload)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "同序号但摘要不符"):
                ledger.replay_head(ledger_root)

    def test_uncommitted_batch_is_not_pushed_and_commit_binds_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            _register(root, campaign_dir, "c1")
            _reconciliation(campaign_dir, operation_id="rec-1", keys=["k1", "k2", "k3", "k4", "k5"])
            batch_dir = campaign_dir / "ledger" / "outbox" / "batch-000002"
            self.assertEqual(sorted(p.name for p in batch_dir.iterdir()), ["COMMIT", "entry-01.json", "entry-02.json", "entry-03.json"])
            # 手工造一个未 COMMIT 的 batch-000003：不推送，且其后的 batch 不再看
            (batch_dir.parent / "batch-000003").mkdir(mode=0o700)
            entry = {"schema_version": ledger.ENTRY_SCHEMA, "batch_sequence": 3, "entry_sequence": 1, "operation_id": "rec-2", "event_type": "reconciliation_committed", "payload_fragment": {"request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0}}, "source": {"kind": "x", "sha256": "c" * 64}, "receipt_bindings": [], "previous_entry_sha256": None}
            entry["entry_sha256"] = ledger._digest(entry)
            _write_json(batch_dir.parent / "batch-000003" / "entry-01.json", entry)
            report = ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            statuses = [(r["batch"].rsplit("/", 1)[1], r["status"]) for r in report["results"]]
            self.assertEqual(statuses, [("batch-000001", "duplicate"), ("batch-000002", "appended"), ("batch-000003", "uncommitted")])
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["precise_total"], 2 + 5)
            self.assertEqual(head["accounted_identity_index"], ["k1", "k2", "k3", "k4", "k5"])
            # COMMIT 后追加 entry：失败关闭
            last_sha = json.loads((batch_dir / "entry-03.json").read_text("utf-8"))["entry_sha256"]
            extra = dict(entry, batch_sequence=2, entry_sequence=4, previous_entry_sha256=last_sha)
            extra["entry_sha256"] = ledger._digest({k: v for k, v in extra.items() if k != "entry_sha256"})
            _write_json(batch_dir / "entry-04.json", extra)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "缺项或追加"):
                ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            (batch_dir / "entry-04.json").unlink()
            # 篡改 entry 内容：摘要不一致
            tampered_path = batch_dir / "entry-02.json"
            tampered = json.loads(tampered_path.read_text("utf-8"))
            tampered["payload_fragment"]["request"]["identity_keys"] = ["zz"]
            _write_json(tampered_path, tampered)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "摘要不一致"):
                ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)

    def test_duplicate_identity_keys_are_recorded_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            _register(root, campaign_dir, "c1")
            _reconciliation(campaign_dir, operation_id="rec-1", keys=["k-initial-1", "k1", "k1"], estimated=4)
            ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["precise_total"], 3)
            self.assertEqual(head["estimated_total"], 34)
            self.assertEqual([d["identity_key"] for d in head["duplicate_identity_keys"]], ["k-initial-1", "k1"])

    def test_unresolved_blocks_registration_and_consumers_until_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            _register(root, campaign_dir, "c1")
            _reconciliation(campaign_dir, operation_id="rec-1", keys=[], status="unresolved", root_cause_id="rc1-a")
            _reconciliation(campaign_dir, operation_id="rec-2", keys=[], status="unresolved")
            ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            head = ledger.replay_head(ledger_root)
            self.assertTrue(head["blocked"])
            self.assertEqual(head["unresolved_operation_ids"], ["rec-1", "rec-2"])
            for command in sorted(ledger.CONSUMER_COMMANDS):
                with self.assertRaisesRegex(ledger.ProjectLedgerError, "blocked"):
                    ledger.assert_campaign_admitted(campaign_dir, command=command, require=True)
            other = _campaign(root, "c2")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "blocked"):
                _register(root, other, "c2")
            # blocked 白名单：terminal、repair、reconciliation、accounting_resolved 仍可写
            ledger.append_project_event(ledger_root, operation_id="term-c1", event_type="campaign_terminal", payload={"campaign_id": "c1", "terminal_reason": "accounting_unresolved"}, source_batch_sha256=None)
            ledger.append_project_event(ledger_root, operation_id="res-1", event_type="accounting_resolved", payload={"resolved_operation_id": "rec-1", "request": {"status": "resolved", "identity_keys": ["k9"], "estimated_delta": 0, "provenance_receipt_sha256": "b" * 64}}, source_batch_sha256=None)
            self.assertTrue(ledger.replay_head(ledger_root)["blocked"])
            ledger.append_project_event(ledger_root, operation_id="res-2", event_type="accounting_resolved", payload={"resolved_operation_id": "rec-2", "request": {"status": "estimated", "identity_keys": [], "estimated_delta": 3, "provenance_receipt_sha256": "b" * 64}}, source_batch_sha256=None)
            head = ledger.replay_head(ledger_root)
            self.assertFalse(head["blocked"])
            self.assertEqual(head["unresolved_operation_ids"], [])
            self.assertEqual(head["root_cause_counts"], {"rc1-a": 1})
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已终态"):
                ledger.assert_campaign_admitted(campaign_dir, command="seal", require=True)

    def test_root_cause_limit_rejects_and_repair_resets_without_reviving_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            _register(root, campaign_dir, "c1")
            _reconciliation(campaign_dir, operation_id="rec-1", keys=["k1"], root_cause_id="rc1-a")
            _reconciliation(campaign_dir, operation_id="rec-2", keys=["k2"], root_cause_id="rc1-a")
            ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["root_causes_at_limit"], ["rc1-a"])
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "根因"):
                ledger.assert_campaign_admitted(campaign_dir, command="resume", require=True)
            ledger.append_project_event(ledger_root, operation_id="term-c1", event_type="campaign_terminal", payload={"campaign_id": "c1", "terminal_reason": "root_cause_limit"}, source_batch_sha256=None)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "绑定必须恰好是"):
                ledger.record_root_cause_repair(ledger_root, root_cause_id="rc1-a", kind="code", bindings={"fix_commit_sha": "1" * 40})
            repair = ledger.record_root_cause_repair(ledger_root, root_cause_id="rc1-a", kind="code", bindings={"fix_commit_sha": "1" * 40, "regression_receipt_sha256": "2" * 64, "deployment_receipt_sha256": "3" * 64})
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["root_cause_counts"]["rc1-a"], 0)
            self.assertEqual(head["root_causes_at_limit"], [])
            self.assertIn("c1", head["terminal_campaigns"])
            self.assertTrue(Path(repair["receipt_path"]).is_file())
            # 修复收据与 batch 已写但事件未写时的重放：删掉事件文件后补齐器重新推送
            events = sorted((ledger_root / "events").iterdir())
            events[-1].unlink()
            (ledger_root / "head.json").unlink()
            report = ledger.reconcile_project_ledger(ledger_root)
            self.assertEqual([r["status"] for r in report["results"] if r["kind"] == "repairs"], ["appended"])
            self.assertEqual(ledger.replay_head(ledger_root)["root_cause_counts"]["rc1-a"], 0)

    def test_registration_retry_after_head_moved_appends_rejection_without_touching_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root, formal_open_limit=1)
            first = _campaign(root, "c1")
            second = _campaign(root, "c2")
            # c2 先写好注册 batch（模拟推送前中断），随后 c1 注册占满名额
            with ledger.project_lock(ledger_root):
                plan, _raw = ledger._load_plan(ledger_root)
                head = ledger._replay(ledger_root, plan, ledger._load_events(ledger_root), rebuild_cache=False)
                with ledger.campaign_ledger_lock(second) as ledger_dir:
                    campaign_plan = {"schema_version": ledger.CAMPAIGN_PLAN_SCHEMA, "campaign_id": "c2", "campaign_mode": "formal", "target_version": "0.154.0", "registration_operation_id": "register:c2", "admission_head_sha256": head["head_sha256"], "project_ledger": str(ledger_root), "deadline_at_utc": None, "created_at_utc": _future(0)}
                    ledger._write_once(ledger_dir / "plan.json", campaign_plan)
                    ledger.write_batch(ledger_dir, operation_id="register:c2", event_type="campaign_registered", payload=ledger._registration_payload(campaign_plan, second), source={"kind": "campaign_plan", "sha256": ledger._digest(campaign_plan)})
            _register(root, first, "c1")
            entry_before = (second / "ledger" / "outbox" / "batch-000001" / "entry-01.json").read_bytes()
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "注册已被追加拒绝"):
                ledger.assert_campaign_admitted(second, command="campaign-run", require=True)
            head = ledger.replay_head(ledger_root)
            self.assertIn("c2", head["rejected_campaigns"])
            self.assertIn("已达上限", head["rejected_campaigns"]["c2"]["reason"])
            self.assertEqual((second / "ledger" / "outbox" / "batch-000001" / "entry-01.json").read_bytes(), entry_before)

    def test_campaign_with_plan_but_no_committed_batch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            with ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
                ledger._write_once(ledger_dir / "plan.json", {"schema_version": ledger.CAMPAIGN_PLAN_SCHEMA, "campaign_id": "c1", "campaign_mode": "formal", "target_version": "0.154.0", "registration_operation_id": "register:c1", "admission_head_sha256": "a" * 64, "project_ledger": str(ledger_root), "deadline_at_utc": None, "created_at_utc": _future(0)})
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "无注册事件"):
                ledger.assert_campaign_admitted(campaign_dir, command="resume", require=True)
            unregistered = _campaign(root, "c3")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "缺少账本 plan"):
                ledger.assert_campaign_admitted(unregistered, command="resume", require=True)

    def test_bootstrap_cutover_batches_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir = _campaign(root, "c1")
            with ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
                batch = ledger.write_batch(ledger_dir, operation_id="legacy-1", event_type="reconciliation_committed", payload={"request": {"status": "resolved", "identity_keys": ["k1"], "estimated_delta": 0}}, source={"kind": "x", "sha256": "c" * 64})
            ledger_root = _create(root, initial_identity_keys=["k1"], bootstrap_cutover={"campaign_ledgers": [], "batch_sha256s": [batch["batch_sha256"]], "operation_ids": ["legacy-1"], "identity_keys_sha256": ledger._digest(["k1"])})
            report = ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            self.assertEqual([r["status"] for r in report["results"] if r["kind"] == "campaign"], ["bootstrap_cutover"])
            self.assertEqual(ledger.replay_head(ledger_root)["sequence"], 0)

    def test_budget_deadline_and_fixture_only_gates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root, live_request_budget=3, initial_identity_keys=["k1", "k2"], initial_estimated_count=1)
            campaign_dir = _campaign(root, "c1")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "预算已耗尽"):
                _register(root, campaign_dir, "c1")
            past = datetime.now(timezone.utc) + timedelta(hours=72)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "截止时间已到"):
                _register(root, campaign_dir, "c1", now=past)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "必须位于 staging 目录树内"):
                _create(base, fixture_only=True)
            (base / "staging").mkdir(mode=0o700)
            root = base / "staging" / "data"
            root.mkdir(mode=0o700)
            ledger_root = _create(root, fixture_only=True)
            plan, _raw = ledger._load_plan(ledger_root)
            production = base / "prod" / "evidence" / "campaigns" / "c-prod"
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "只允许 staging 路径下的 Campaign"):
                ledger._check_fixture_only(ledger_root, plan, production)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "只能创建在 staging"):
                ledger.create_fixture_ledger(base / "prod")
            staged = _campaign(root, "c-stage", staging=True)
            self.assertEqual(_register(root, staged, "c-stage")["status"], "appended")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "超过项目绝对截止"):
                _register(root, _campaign(root, "c-late", staging=True), "c-late", deadline=_future(200))
            # staging 树内的 fixture 总账可重复获取，且已注册 Campaign 的重注册幂等
            self.assertEqual(ledger.create_fixture_ledger(root), ledger_root)
            self.assertEqual(ledger.register_existing_campaign(staged)["status"], "duplicate")

    def test_supersede_approval_releases_formal_slot_once(self) -> None:
        """B8：同版本无终态 formal 达上限后，只能凭一次性批准收据取代；head 变化或用过即失效。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root, formal_open_limit=1)
            first = _campaign(root, "c1")
            _register(root, first, "c1")
            second = _campaign(root, "c2")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已达上限"):
                _register(root, second, "c2")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "未在总账注册"):
                ledger.create_supersede_approval(ledger_root, superseded_campaign_id="c9", approved_by="老板")
            approval = ledger.create_supersede_approval(ledger_root, superseded_campaign_id="c1", approved_by="老板")
            payload = json.loads(Path(approval["approval_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], ledger.SUPERSEDE_APPROVAL_SCHEMA)
            self.assertEqual(payload["admission_head_sha256"], ledger.replay_head(ledger_root)["head_sha256"])
            applied = ledger.apply_supersede_approval(ledger_root, Path(approval["approval_path"]))
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["terminal_campaigns"]["c1"]["terminal_reason"], "superseded")
            self.assertEqual(head["operations"][applied["operation_id"]]["event_type"], "campaign_terminal")
            # 用过即失效；被取代 Campaign 也不能再签发。
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已使用"):
                ledger.apply_supersede_approval(ledger_root, Path(approval["approval_path"]))
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已终态"):
                ledger.create_supersede_approval(ledger_root, superseded_campaign_id="c1", approved_by="老板")
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "已终态"):
                ledger.assert_campaign_admitted(first, command="resume", require=True)
            # 名额释放后 c2 可注册；随后 head 变化让先签的收据失效。
            _register(root, second, "c2")
            stale = ledger.create_supersede_approval(ledger_root, superseded_campaign_id="c2", approved_by="老板")
            ledger.append_project_event(
                ledger_root,
                operation_id="noise-repair",
                event_type="root_cause_repaired",
                payload={"root_cause_id": "rc1-00000000000000000000"},
                source_batch_sha256=None,
            )
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "head 已变化"):
                ledger.apply_supersede_approval(ledger_root, Path(stale["approval_path"]))
            self.assertNotIn("c2", ledger.replay_head(ledger_root)["terminal_campaigns"])
            # CLI 往返：重新签发并消费。
            self.assertEqual(
                ledger.main(["supersede-approval-create", "--ledger-dir", str(ledger_root), "--campaign-id", "c2", "--approved-by", "老板"]),
                0,
            )
            approvals = sorted((ledger_root / ledger.SUPERSESSIONS_DIR_NAME / "approvals").glob("supersede-c2-*.json"))
            self.assertEqual(len(approvals), 2)
            fresh = [p for p in approvals if json.loads(p.read_text(encoding="utf-8"))["admission_head_sha256"] == ledger.replay_head(ledger_root)["head_sha256"]]
            self.assertEqual(len(fresh), 1)
            self.assertEqual(ledger.main(["supersede-approval-apply", "--ledger-dir", str(ledger_root), "--approval", str(fresh[0])]), 0)
            self.assertEqual(ledger.replay_head(ledger_root)["terminal_campaigns"]["c2"]["terminal_reason"], "superseded")

    def test_concurrent_plans_only_one_registers_under_formal_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            _create(root, formal_open_limit=1)
            first = _campaign(root, "c1")
            second = _campaign(root, "c2")
            context = multiprocessing.get_context("fork")
            with context.Pool(2) as pool:
                results = pool.map(_concurrent_register, [(str(root), str(first), "c1"), (str(root), str(second), "c2")])
            self.assertEqual(sorted(r.split(":")[0] for r in results), ["registered", "rejected"])

    def test_codes_change_without_migration_is_rejected_and_mapping_inherits_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = _create(root)
            campaign_dir = _campaign(root, "c1")
            _register(root, campaign_dir, "c1")
            _reconciliation(campaign_dir, operation_id="rec-1", keys=[], root_cause_id="rc1-old")
            ledger.reconcile_project_ledger(ledger_root, campaign_dir=campaign_dir)
            plan_path = ledger_root / "plan.json"
            plan = json.loads(plan_path.read_text("utf-8"))
            frozen_sha = plan["root_cause_codes_sha256"]
            plan["root_cause_codes_sha256"] = "9" * 64
            plan["plan_sha256"] = ledger._digest({k: v for k, v in plan.items() if k != "plan_sha256"})
            plan_path.chmod(0o600)
            plan_path.unlink()
            _write_json(plan_path, plan)
            with self.assertRaisesRegex(ledger.ProjectLedgerError, "没有衔接的旧新 ID 映射收据"):
                ledger.replay_head(ledger_root)
            _write_json(ledger_root / "migrations" / "000001.json", {"schema_version": ledger.MIGRATION_SCHEMA, "sequence": 1, "from_codes_sha256": "9" * 64, "to_codes_sha256": frozen_sha, "from_algorithm_version": plan["root_cause_algorithm_version"], "to_algorithm_version": plan["root_cause_algorithm_version"], "id_mapping": {"rc1-old": "rc1-new"}, "approved_by": "老板", "approved_at_utc": _future(0)})
            (ledger_root / "migrations").chmod(0o700)
            head = ledger.replay_head(ledger_root)
            self.assertEqual(head["root_cause_counts"], {"rc1-new": 1})

    def test_cli_create_status_and_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = root / ledger.LEDGER_DIR_NAME
            code = ledger.main(["create-project-ledger", "--ledger-dir", str(ledger_root), "--project-id", "p", "--absolute-deadline-utc", _future(), "--deadline-approved-by", "老板", "--estimation-policy", "none", "--estimation-policy-approved-by", "老板"])
            self.assertEqual(code, 0)
            self.assertEqual(ledger.main(["status", "--ledger-dir", str(ledger_root)]), 0)
            campaign_dir = _campaign(root, "c1")
            self.assertEqual(ledger.main(["assert-campaign-admitted", "--campaign-dir", str(campaign_dir), "--consumer", "resume"]), 2)
            _register(root, campaign_dir, "c1")
            self.assertEqual(ledger.main(["assert-campaign-admitted", "--campaign-dir", str(campaign_dir), "--consumer", "resume"]), 0)
            self.assertEqual(os.stat(ledger_root / "events" / "000001.json").st_mode & 0o777, 0o600)


def _register_after_registered(root: Path, campaign_dir: Path) -> dict:
    ledger_root = root / ledger.LEDGER_DIR_NAME
    with ledger.project_lock(ledger_root):
        plan, _raw = ledger._load_plan(ledger_root)
        head = ledger._replay(ledger_root, plan, ledger._load_events(ledger_root), rebuild_cache=False)
        return ledger.register_campaign(ledger_root, campaign_dir, campaign_id="c1", campaign_mode="formal", target_version="0.154.0", deadline_at_utc=None, admission_head_sha256=head["head_sha256"])


if __name__ == "__main__":
    unittest.main()
