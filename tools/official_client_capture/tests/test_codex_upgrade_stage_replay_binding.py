"""R4：已有 checkpoint 的阶段只凭阶段审核对账写入账本的幂等重派证明逐字重派；证明结构与 schema 一致。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

START = datetime(2026, 8, 30, tzinfo=timezone.utc)


def at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


class StageReplayCheckpointBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "UpgradeTimingLedger"
        ledger.create_ledger(self.root, upgrade_id="codex-r4-binding", baseline_version="0.154.0",
                             target_version="0.156.1", campaign_purpose="validation_only",
                             evidence_decision="recapture", started_at_utc=START.isoformat())
        for minute, (event_id, phase, kind) in enumerate((
            ("vc0-done", "VC-0", "stage_completed"), ("vc1-start", "VC-1", "stage_started"),
            ("vc1-done", "VC-1", "stage_completed"), ("vc2-start", "VC-2", "stage_started"),
        ), 1):
            ledger.append_event(self.root, event_id=event_id, phase=phase, event_type=kind,
                                next_action="推进阶段", recorded_at_utc=at(minute))
        self.checkpoint = self.base / "vc-2-checkpoint.json"
        self.checkpoint.write_text('{"phase": "VC-2"}\n', encoding="utf-8")

    def _bind(self, role: str, payload: dict) -> dict:
        path = self.root / "receipts" / f"{role}.json"
        ledger._write_once(path, payload)
        return {"role": role, "path": path.relative_to(self.root).as_posix(), "sha256": ledger._sha256_file(path)}

    def _proof(self, reconciliation: dict, *, checkpoint_sha256: str) -> dict:
        digest = hashlib.sha256((json.dumps(reconciliation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")).hexdigest()
        return reconciler.build_stage_replay_proof(
            campaign_id="codex-r4-binding", run_id="run-0005", review_event_id="vc2-review",
            review_root_cause_id="rc-vc2", reconciliation_receipt_sha256=digest, commit_sha256="c" * 64,
            next_action="redispatch-same-batch",
            replay={"phase": "VC-2", "allowed": True, "reasons": [], "actions": [{
                "action_id": "classify-approve", "inputs": {},
                "command": ["python3", "-m", "tools.official_client_capture.codex_upgrade", "classify"],
                "outputs": {"checkpoint": checkpoint_sha256},
            }]},
        )

    def _review_passed(self) -> dict:
        ledger.append_event(self.root, event_id="vc2-abandoned", phase="VC-2", event_type="stage_abandoned",
                            root_cause_id="rc-vc2", next_action="stage_review_required：先对账", recorded_at_utc=at(10))
        ledger.append_event(self.root, event_id="vc2-review", phase="VC-2", event_type="stage_review_required",
                            root_cause_id="rc-vc2", next_action="stage_review_required：先对账", recorded_at_utc=at(11))
        reconciliation = {"status": "recoverable", "reservation_exists": False}
        proof = self._proof(reconciliation, checkpoint_sha256=ledger._sha256_file(self.checkpoint))
        receipts = sorted([self._bind("provenance", {"role": "provenance"}),
                           self._bind("reconciliation", reconciliation),
                           self._bind("stage_replay", proof)], key=lambda item: item["role"])
        ledger.append_event(self.root, event_id="vc2-review-passed", phase="VC-2", event_type="receipt_passed",
                            receipts=receipts, next_action="redispatch-same-batch", recorded_at_utc=at(12))
        return proof

    def test_review_proof_bound_to_current_checkpoint_allows_redispatch(self) -> None:
        proof = self._review_passed()
        self.assertEqual(upgrade._require_stage_replay_proof_for_checkpoint(self.root, "VC-2", self.checkpoint), proof)
        ledger.append_event(self.root, event_id="vc2-paused", phase="VC-2", event_type="deadline_paused",
                            next_action="deadline-extend preview/apply", recorded_at_utc=at(13),
                            deadline_control={"scopes": ["stage"], "paused_since_utc": at(13)})
        # 预算控制事件不改变放行事件：仍按最后一条实质事件核对。
        self.assertEqual(upgrade._require_stage_replay_proof_for_checkpoint(self.root, "VC-2", self.checkpoint), proof)

    def test_changed_checkpoint_or_other_phase_is_rejected(self) -> None:
        self._review_passed()
        with self.assertRaisesRegex(upgrade.ConfigurationError, "只允许阶段审核对账证明后的逐字重派"):
            upgrade._require_stage_replay_proof_for_checkpoint(self.root, "VC-3", self.checkpoint)
        self.checkpoint.write_text('{"phase": "VC-2", "changed": true}\n', encoding="utf-8")
        with self.assertRaisesRegex(upgrade.ConfigurationError, "与当前 checkpoint 不一致"):
            upgrade._require_stage_replay_proof_for_checkpoint(self.root, "VC-2", self.checkpoint)

    def test_environment_recovery_without_stage_replay_is_rejected(self) -> None:
        """环境恢复同样把下一动作设成逐字重派，但没有阶段幂等重派证明，不能让已有 checkpoint 的阶段再编译。"""

        ledger.append_event(self.root, event_id="vc2-env-paused", phase="VC-2", event_type="recovery_required",
                            root_cause_id="environment-prerequisite", next_action="reconcile-supervisor-run",
                            recorded_at_utc=at(10))
        receipts = sorted([self._bind("provenance", {"role": "provenance"}),
                           self._bind("reconciliation", {"status": "recoverable"})], key=lambda item: item["role"])
        ledger.append_event(self.root, event_id="vc2-env-reconciled", phase="VC-2", event_type="receipt_passed",
                            receipts=receipts, next_action="redispatch-same-batch", recorded_at_utc=at(11))
        self.assertEqual(ledger.inspect_ledger(self.root, now=at(12))["next_action"], "redispatch-same-batch")
        with self.assertRaisesRegex(upgrade.ConfigurationError, "没有绑定阶段幂等重派证明"):
            upgrade._require_stage_replay_proof_for_checkpoint(self.root, "VC-2", self.checkpoint)

    def test_proof_builder_matches_schema_and_historical_field_order(self) -> None:
        proof = self._proof({"status": "recoverable", "reservation_exists": False}, checkpoint_sha256="d" * 64)
        schema = json.loads(Path(artifacts.__file__).with_name("codex_upgrade_stage_replay.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], artifacts.STAGE_REPLAY_SCHEMA)
        self.assertEqual(set(proof), set(schema["required"]))
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        for key, rule in schema["properties"].items():
            if "const" in rule:
                self.assertEqual(proof[key], rule["const"], key)
            if "enum" in rule:
                self.assertIn(proof[key], rule["enum"], key)
        for action in proof["actions"]:
            self.assertEqual(set(action), set(schema["properties"]["actions"]["items"]["required"]))
        # 历史证明按这一字段顺序写出，重复对账时逐字节核对；构造函数不得改变顺序。
        self.assertEqual(list(proof)[:9], ["schema_version", "decision", "campaign_id", "run_id", "review_event_id",
                                          "review_root_cause_id", "reconciliation_receipt_sha256", "commit_sha256",
                                          "next_action"])


if __name__ == "__main__":
    unittest.main()
