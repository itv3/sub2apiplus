"""Codex 官方客户端升级 UpgradeTimingLedger 测试。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_timing_ledger as ledger


class TimingLedgerTests(unittest.TestCase):
    START = "2026-08-30T00:00:00+00:00"

    def _create(self, root: Path) -> None:
        ledger.create_ledger(
            root,
            upgrade_id="codex-0151",
            baseline_version="0.149.1",
            target_version="0.151.0",
            campaign_purpose="production_replacement",
            evidence_decision="recapture",
            started_at_utc=self.START,
        )

    @staticmethod
    def _at(minutes: int, seconds: int = 0) -> str:
        started = datetime(2026, 8, 30, tzinfo=timezone.utc)
        return (started + timedelta(minutes=minutes, seconds=seconds)).isoformat()

    def test_checkpoint_replays_after_later_events_are_appended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            original = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            ledger._write_once(root / "receipts" / "p0.json", original)
            ledger.append_event(
                root,
                event_id="p0-receipt-passed",
                phase="VC-0",
                event_type="receipt_passed",
                recorded_at_utc=self._at(2),
            )
            replayed = ledger.replay(root, "receipts/p0.json")
            self.assertEqual(replayed, original)
            self.assertEqual(replayed["summary"]["head_sequence"], 1)

    def test_legacy_checkpoint_without_null_recovery_fields_replays(self) -> None:
        """旧版摘要未写入新增的空恢复字段时仍可按原始字节重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            receipt["summary"].pop("recovery_phase")
            receipt["summary"].pop("recovery_root_cause_id")
            ledger._write_once(root / "receipts" / "legacy-v1.json", receipt)

            replayed = ledger.replay(root, "receipts/legacy-v1.json")

            self.assertEqual(replayed, receipt)
            self.assertNotIn("recovery_phase", replayed["summary"])
            self.assertNotIn("recovery_root_cause_id", replayed["summary"])

    def test_legacy_checkpoint_cannot_omit_non_null_recovery_fields(self) -> None:
        """当前重算存在恢复状态时，旧格式缺字段也必须拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            ledger.append_event(
                root,
                event_id="vc0-recovery-required",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="environment-prerequisite",
                next_action="reconcile-supervisor-run",
                recorded_at_utc=self._at(1),
            )
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(2))
            receipt["summary"].pop("recovery_phase")
            receipt["summary"].pop("recovery_root_cause_id")
            ledger._write_once(root / "receipts" / "invalid-legacy-v1.json", receipt)

            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "checkpoint 重放结果不一致",
            ):
                ledger.replay(root, "receipts/invalid-legacy-v1.json")

    def test_current_checkpoint_recovery_fields_remain_strict(self) -> None:
        """新格式已写入恢复字段后，字段内容仍参与严格字节校验。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            receipt["summary"]["recovery_phase"] = "VC-0"
            ledger._write_once(root / "receipts" / "tampered-current.json", receipt)

            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "checkpoint 重放结果不一致",
            ):
                ledger.replay(root, "receipts/tampered-current.json")

            partial = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            partial["summary"].pop("recovery_phase")
            ledger._write_once(root / "receipts" / "partial-current.json", partial)
            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "checkpoint 重放结果不一致",
            ):
                ledger.replay(root, "receipts/partial-current.json")

    def test_registered_producer_successor_preserves_frozen_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            plan_path = root / "ledger.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["producer"]["tool_sha256"] = (
                "f28f2527e6496a20af0377f00febf7c40f1b1d673a3d149f9797c899c257b8ee"
            )
            plan_path.write_bytes(ledger._canonical(plan))
            plan_path.chmod(0o600)
            status = ledger.inspect_ledger(root, now=self._at(1))
            self.assertEqual(status["status"], "active")
            self.assertEqual(status["total_deadline_at_utc"], "2026-08-30T06:00:00+00:00")

    def test_producer_path_relocation_does_not_invalidate_historical_ledger(self) -> None:
        """工作树根变化不应迫使历史收据重新执行。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            plan_path = root / "ledger.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["producer"]["tool"] = (
                "/srv/retired-codex-worktree/tools/official_client_capture/"
                "codex_upgrade_timing_ledger.py"
            )
            plan["producer"]["tool_sha256"] = ledger._producer()["tool_sha256"]
            plan_path.write_bytes(ledger._canonical(plan))
            plan_path.chmod(0o600)
            status = ledger.inspect_ledger(root, now=self._at(1))
            self.assertEqual(status["status"], "active")

    def test_relocated_legacy_producer_uses_registered_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            plan_path = root / "ledger.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["producer"]["tool"] = (
                "/srv/retired-codex-worktree/tools/official_client_capture/"
                "codex_upgrade_timing_ledger.py"
            )
            plan["producer"]["tool_sha256"] = (
                "828a8ff86a4b021037d7e9068d80b1d422ff22d39fba89cb1c9ac04e451157e0"
            )
            plan_path.write_bytes(ledger._canonical(plan))
            plan_path.chmod(0o600)
            status = ledger.inspect_ledger(root, now=self._at(1))
            self.assertEqual(status["status"], "active")

    def test_checkpoint_replays_across_registered_producer_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            frozen_sha256 = (
                "f28f2527e6496a20af0377f00febf7c40f1b1d673a3d149f9797c899c257b8ee"
            )
            plan_path = root / "ledger.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["producer"]["tool_sha256"] = frozen_sha256
            plan_path.write_bytes(ledger._canonical(plan))
            plan_path.chmod(0o600)
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            receipt["producer"]["tool_sha256"] = frozen_sha256
            ledger._write_once(root / "receipts" / "frozen-producer.json", receipt)
            replayed = ledger.replay(root, "receipts/frozen-producer.json")
            self.assertEqual(replayed, receipt)

    def test_freeze_successor_with_proven_deletion_replays_and_unproven_fails(self) -> None:
        """B1：带证明删除冻结路径的 freeze successor 才能参与计时工具摘要链。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reader = root / "tools" / "official_client_capture" / "historical_reader.py"
            reader.parent.mkdir(parents=True)
            reader.write_text("def read():\n    return None\n", encoding="utf-8")
            reader_sha256 = hashlib.sha256(reader.read_bytes()).hexdigest()
            relative = "docs/egress/maintenance/upstream-b1-freeze-successor.json"
            receipt_path = root / Path(*relative.split("/"))
            receipt_path.parent.mkdir(parents=True)
            descriptor = {
                "path": relative,
                "base_commit": "a" * 40,
                "scope": "upstream-b1-freeze-successor",
                "result": "passed_with_deletions",
            }
            proof = {
                "algorithm": "deletion-proof/v1",
                "reason": "执行分支退役",
                "deleted_paths": [
                    {
                        "path": "tools/official_client_capture/retired.py",
                        "frozen": True,
                        "last_sha256": "3" * 64,
                        "reference_scan": {
                            "algorithm": "reference-scan/v1",
                            "patterns": ["retired", "retired.py"],
                            "scopes": ["python:import-and-attribute", "shell:invocation", "json:command-fields"],
                            "references": [],
                        },
                    }
                ],
                "historical_readers": [
                    {"path": "tools/official_client_capture/historical_reader.py", "sha256": reader_sha256}
                ],
            }

            def write(**overrides: object) -> None:
                document: dict[str, object] = {
                    "schema_version": "official-egress-upstream-freeze-successor/v1",
                    "issued_at_utc": "2026-09-16T00:00:00Z",
                    "base_commit": "a" * 40,
                    "current_commit": "b" * 40,
                    "scope": "upstream-b1-freeze-successor",
                    "mode": "commit",
                    "extra_worktree_paths": [],
                    "frozen_path_count": 2,
                    "frozen_edge_count": 2,
                    "changed_path_count": 2,
                    "transitions": [
                        {
                            "path": ledger.PRODUCER_TOOL_RELATIVE,
                            "old_path": "",
                            "status": "M",
                            "predecessor_sha256s": ["1" * 64],
                            "to_sha256": "2" * 64,
                            "source_receipts": ["docs/egress/maintenance/base.json"],
                            "reason": "登记计时工具后继",
                        }
                    ],
                    "unregistered_path_count": 0,
                    "unregistered_paths": [],
                    "deleted_frozen_paths": ["tools/official_client_capture/retired.py"],
                    "required_manual_actions": [],
                    "verification": ["make check-egress-spec-ci"],
                    "safety": {
                        "deployment_performed": False,
                        "live_account_used": False,
                        "official_egress_profile_changed": False,
                        "production_config_changed": False,
                        "wire_or_persona_selection_changed": False,
                    },
                    "result": "passed_with_deletions",
                    "deletion_proof": json.loads(json.dumps(proof)),
                }
                document.update(overrides)
                for key in [k for k, v in document.items() if v is None]:
                    document.pop(key)
                compact = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                document["identity_sha256"] = hashlib.sha256(compact.encode("utf-8")).hexdigest()
                receipt_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            write()
            self.assertEqual(ledger._load_freeze_successor_edge(root, descriptor), ("1" * 64, "2" * 64))
            write(deletion_proof=None)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "deletion_proof"):
                ledger._load_freeze_successor_edge(root, descriptor)
            referenced = json.loads(json.dumps(proof))
            referenced["deleted_paths"][0]["reference_scan"]["references"] = [
                {"kind": "shell", "path": "run.sh", "line": 3, "content": "python3 retired.py"}
            ]
            write(deletion_proof=referenced)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "仍有引用"):
                ledger._load_freeze_successor_edge(root, descriptor)
            missing = json.loads(json.dumps(proof))
            missing["historical_readers"][0]["path"] = "tools/official_client_capture/missing_reader.py"
            write(deletion_proof=missing)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "历史读取器"):
                ledger._load_freeze_successor_edge(root, descriptor)
            # 未登记路径的删除可以携带证明；证明里若声称冻结删除但 deleted_frozen_paths 为空则拒绝。
            write(deleted_frozen_paths=[], result="passed_local_evidence_successor")
            plain = dict(descriptor, result="passed_local_evidence_successor")
            with self.assertRaisesRegex(ledger.TimingLedgerError, "与 deleted_frozen_paths 不一致"):
                ledger._load_freeze_successor_edge(root, plain)
            unregistered = json.loads(json.dumps(proof))
            unregistered["deleted_paths"][0]["frozen"] = False
            write(deleted_frozen_paths=[], result="passed_local_evidence_successor", deletion_proof=unregistered)
            self.assertEqual(ledger._load_freeze_successor_edge(root, plain), ("1" * 64, "2" * 64))

    def test_unregistered_producer_digest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            plan_path = root / "ledger.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["producer"]["tool_sha256"] = "0" * 64
            plan_path.write_bytes(ledger._canonical(plan))
            plan_path.chmod(0o600)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "生成器身份漂移"):
                ledger.inspect_ledger(root, now=self._at(1))

    def test_stage_deadline_requires_stop_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            status = ledger.inspect_ledger(root, now=self._at(45))
            self.assertEqual(status["status"], "stop_required")
            with self.assertRaisesRegex(ledger.TimingLedgerError, "要求停线"):
                ledger.append_event(
                    root,
                    event_id="illegal-work",
                    phase="VC-0",
                    event_type="receipt_passed",
                    recorded_at_utc=self._at(46),
                )
            stopped = ledger.append_event(
                root,
                event_id="vc0-timeout-stop",
                phase="VC-0",
                event_type="stop_the_line",
                next_action="拆分工具修复并重新执行干净 P0",
                recorded_at_utc=self._at(46),
            )
            self.assertEqual(stopped["status"], "stopped")

    def test_stage_abandoned_returns_to_vc0_without_resetting_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            ledger.append_event(
                root,
                event_id="vc0-complete",
                phase="VC-0",
                event_type="stage_completed",
                recorded_at_utc=self._at(1),
            )
            ledger.append_event(
                root,
                event_id="vc1-start",
                phase="VC-1",
                event_type="stage_started",
                recorded_at_utc=self._at(2),
            )
            ledger.append_event(
                root,
                event_id="capture-start",
                phase="VC-1",
                event_type="attempt_started",
                attempt_id="capture-1",
                recorded_at_utc=self._at(3),
            )
            ledger.append_event(
                root,
                event_id="capture-failed",
                phase="VC-1",
                event_type="attempt_failed",
                attempt_id="capture-1",
                root_cause_id="producer-tool-path-drift",
                recorded_at_utc=self._at(4),
            )
            abandoned = ledger.append_event(
                root,
                event_id="vc1-abandoned",
                phase="VC-1",
                event_type="stage_abandoned",
                root_cause_id="producer-tool-path-drift",
                next_action="独立修复产出工具并从干净 VC-0 重来",
                recorded_at_utc=self._at(5),
            )
            self.assertIsNone(abandoned["active_phase"])
            self.assertEqual(
                abandoned["total_deadline_at_utc"],
                "2026-08-30T06:00:00+00:00",
            )
            restarted = ledger.append_event(
                root,
                event_id="vc0-restarted",
                phase="VC-0",
                event_type="stage_started",
                recorded_at_utc=self._at(6),
            )
            self.assertEqual(restarted["active_phase"], "VC-0")
            self.assertEqual(
                restarted["stage_deadline_at_utc"],
                "2026-08-30T00:51:00+00:00",
            )
            self.assertEqual(
                restarted["total_deadline_at_utc"],
                "2026-08-30T06:00:00+00:00",
            )

    def test_stage_abandoned_requires_root_cause_and_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "stage_abandoned",
            ):
                ledger.append_event(
                    root,
                    event_id="vc0-abandoned-without-cause",
                    phase="VC-0",
                    event_type="stage_abandoned",
                    recorded_at_utc=self._at(1),
                )

    def test_recovery_required_before_reservation_only_reconcile_can_resume(self) -> None:
        """reservation 前失败保持当前阶段暂停，普通推进被拒，对账收据恢复 active。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            paused = ledger.append_event(
                root,
                event_id="vc0-prerequisite-paused",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="environment-prerequisite",
                next_action="reconcile-supervisor-run",
                recorded_at_utc=self._at(1),
            )
            self.assertEqual(paused["status"], "recovery_required")
            self.assertEqual(paused["active_phase"], "VC-0")
            self.assertEqual(paused["recovery_phase"], "VC-0")
            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "只允许对账或已批准的恢复动作",
            ):
                ledger.append_event(
                    root,
                    event_id="illegal-next-stage",
                    phase="VC-0",
                    event_type="stage_completed",
                    recorded_at_utc=self._at(2),
                )

            bindings = []
            for role in ("provenance", "reconciliation"):
                path = root / "receipts" / f"{role}.json"
                ledger._write_once(path, {"role": role, "status": "passed"})
                bindings.append(
                    {
                        "role": role,
                        "path": path.relative_to(root).as_posix(),
                        "sha256": ledger._sha256_file(path),
                    }
                )
            resumed = ledger.append_event(
                root,
                event_id="vc0-prerequisite-reconciled",
                phase="VC-0",
                event_type="receipt_passed",
                receipts=bindings,
                next_action="redispatch-same-batch",
                recorded_at_utc=self._at(3),
            )
            self.assertEqual(resumed["status"], "active")
            self.assertEqual(resumed["active_phase"], "VC-0")
            self.assertIsNone(resumed["recovery_root_cause_id"])

    def test_recovery_required_after_reservation_needs_bound_approval(self) -> None:
        """reservation 后只允许先对账 attempt，再由绑定预览的批准恢复 active。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            ledger.append_event(
                root,
                event_id="vc0-attempt-paused",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="environment-prerequisite",
                next_action="reconcile-attempt",
                recorded_at_utc=self._at(1),
            )
            ledger.append_event(
                root,
                event_id="attempt-started-by-reconcile",
                phase="VC-0",
                event_type="attempt_started",
                attempt_id="attempt-1",
                recorded_at_utc=self._at(2),
            )
            reconciled = ledger.append_event(
                root,
                event_id="attempt-failed-by-reconcile",
                phase="VC-0",
                event_type="attempt_failed",
                attempt_id="attempt-1",
                root_cause_id="environment-prerequisite",
                recorded_at_utc=self._at(3),
            )
            self.assertEqual(reconciled["status"], "recovery_required")
            with self.assertRaisesRegex(
                ledger.TimingLedgerError,
                "必须绑定恢复预览与恢复批准",
            ):
                ledger.append_event(
                    root,
                    event_id="unbound-recovery",
                    phase="VC-0",
                    event_type="recovery_authorized",
                    root_cause_id="environment-prerequisite",
                    next_action="resume-rerun-failed",
                    recorded_at_utc=self._at(4),
                )

            bindings = []
            for role in ("recovery_approval", "recovery_preview"):
                path = root / "receipts" / f"{role}.json"
                ledger._write_once(path, {"role": role, "status": "passed"})
                bindings.append(
                    {
                        "role": role,
                        "path": path.relative_to(root).as_posix(),
                        "sha256": ledger._sha256_file(path),
                    }
                )
            resumed = ledger.append_event(
                root,
                event_id="approved-recovery",
                phase="VC-0",
                event_type="recovery_authorized",
                root_cause_id="environment-prerequisite",
                receipts=bindings,
                next_action="resume-rerun-failed",
                recorded_at_utc=self._at(5),
            )
            self.assertEqual(resumed["status"], "active")
            self.assertEqual(resumed["same_root_cause_failures"], {"environment-prerequisite": 1})

    def test_third_same_root_cause_attempt_is_forbidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            ledger.append_event(
                root,
                event_id="attempt-1-start",
                phase="VC-0",
                event_type="attempt_started",
                attempt_id="attempt-1",
                recorded_at_utc=self._at(1),
            )
            ledger.append_event(
                root,
                event_id="attempt-1-fail",
                phase="VC-0",
                event_type="attempt_failed",
                attempt_id="attempt-1",
                root_cause_id="same-cause",
                recorded_at_utc=self._at(2),
            )
            ledger.append_event(
                root,
                event_id="attempt-2-start",
                phase="VC-0",
                event_type="attempt_started",
                attempt_id="attempt-2",
                root_cause_id="same-cause",
                recorded_at_utc=self._at(3),
            )
            second = ledger.append_event(
                root,
                event_id="attempt-2-fail",
                phase="VC-0",
                event_type="attempt_failed",
                attempt_id="attempt-2",
                root_cause_id="same-cause",
                recorded_at_utc=self._at(4),
            )
            self.assertEqual(second["status"], "stop_required")
            with self.assertRaisesRegex(ledger.TimingLedgerError, "停线|第三次"):
                ledger.append_event(
                    root,
                    event_id="attempt-3-start",
                    phase="VC-0",
                    event_type="attempt_started",
                    attempt_id="attempt-3",
                    root_cause_id="same-cause",
                    recorded_at_utc=self._at(5),
                )

    def test_checkpoint_replay_rejects_event_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            receipt = ledger.build_checkpoint(root, observed_at_utc=self._at(1))
            ledger._write_once(root / "receipts" / "p0.json", receipt)
            event_path = root / "events" / "000001.json"
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["next_action"] = "被篡改"
            event_path.write_text(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            event_path.chmod(0o600)
            with self.assertRaises(ledger.TimingLedgerError):
                ledger.replay(root, "receipts/p0.json")

    def test_schema_matches_runtime_version(self) -> None:
        schema = json.loads(
            Path(ledger.__file__)
            .with_name("codex_upgrade_timing_ledger.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            ledger.RECEIPT_SCHEMA,
        )

    def test_project_ledger_binding_lets_campaign_plan_set_budgets(self) -> None:
        """绑定项目总账后，总预算与阶段预算由总账绝对截止裁剪，不再按 360／75 硬切。"""

        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            project_root = root / project_ledger.LEDGER_DIR_NAME
            started = datetime(2026, 9, 16, tzinfo=timezone.utc)
            deadline = started + timedelta(days=3)
            project_ledger.create_project_ledger(
                project_root,
                project_id="binding-project",
                absolute_deadline_utc=deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                deadline_approved_by="test",
                estimation_policy="upper_bound_from_sibling_or_turn_ratio",
                estimation_policy_approved_by="test",
                fixture_only=False,
            )
            unbound = root / "unbound"
            with self.assertRaisesRegex(ledger.TimingLedgerError, "1～360"):
                ledger.create_ledger(
                    unbound,
                    upgrade_id="unbound",
                    baseline_version="0.151.0",
                    target_version="0.154.0",
                    campaign_purpose="validation_only",
                    evidence_decision="reuse",
                    started_at_utc=started.isoformat(),
                    total_budget_minutes=24 * 60,
                )
            bound = root / "bound"
            summary = ledger.create_ledger(
                bound,
                upgrade_id="bound",
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose="validation_only",
                evidence_decision="reuse",
                started_at_utc=started.isoformat(),
                total_budget_minutes=48 * 60,
                stage_budgets_minutes={**ledger.DEFAULT_STAGE_BUDGETS, "VC-2": 24 * 60, "VC-5": 6 * 60},
                project_ledger_dir=project_root,
            )
            self.assertEqual(summary["status"], "active")
            plan = json.loads((bound / "ledger.json").read_text(encoding="utf-8"))
            binding = plan[ledger.PROJECT_LEDGER_BINDING_FIELD]
            self.assertEqual(binding["path"], str(project_root.resolve()))
            self.assertEqual(binding["absolute_deadline_utc"], plan["stage_budgets_minutes"] and binding["absolute_deadline_utc"])
            self.assertEqual(plan["stage_budgets_minutes"]["VC-2"], 24 * 60)
            # 总预算不得超过总账剩余；阶段预算不得超过总预算。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "总墙钟预算"):
                ledger.create_ledger(
                    root / "over",
                    upgrade_id="over",
                    baseline_version="0.151.0",
                    target_version="0.154.0",
                    campaign_purpose="validation_only",
                    evidence_decision="reuse",
                    started_at_utc=started.isoformat(),
                    total_budget_minutes=4 * 24 * 60,
                    project_ledger_dir=project_root,
                )
            with self.assertRaisesRegex(ledger.TimingLedgerError, "VC-2 预算"):
                ledger.create_ledger(
                    root / "stage-over",
                    upgrade_id="stage-over",
                    baseline_version="0.151.0",
                    target_version="0.154.0",
                    campaign_purpose="validation_only",
                    evidence_decision="reuse",
                    started_at_utc=started.isoformat(),
                    total_budget_minutes=120,
                    stage_budgets_minutes={**ledger.DEFAULT_STAGE_BUDGETS, "VC-2": 121},
                    project_ledger_dir=project_root,
                )
            # 历史（未绑定）账本的 plan 不含绑定字段，仍按旧规则读取。
            unbound_plan = json.loads((root / "bound" / "ledger.json").read_text(encoding="utf-8"))
            self.assertIn(ledger.PROJECT_LEDGER_BINDING_FIELD, unbound_plan)
            self.assertEqual(ledger._stage_budget_arguments(["VC-2=1440", "VC-6=90"])["VC-2"], 1440)
            with self.assertRaisesRegex(ledger.TimingLedgerError, "阶段预算参数非法"):
                ledger._stage_budget_arguments(["VC-9=10"])

    def test_phase_ledger_state_lists_completed_phases_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            self._create(root)
            ledger.append_event(root, event_id="c0", phase="VC-0", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(1))
            ledger.append_event(root, event_id="s1", phase="VC-1", event_type="stage_started", next_action="x", recorded_at_utc=self._at(2))
            ledger.append_event(root, event_id="c1", phase="VC-1", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(3))
            state = ledger.phase_ledger_state(root, now=self._at(4))
            self.assertEqual(state["completed_phases"], ["VC-0", "VC-1"])
            self.assertIsNone(state["active_phase"])
            self.assertEqual(state["head_sequence"], 4)


if __name__ == "__main__":
    unittest.main()
