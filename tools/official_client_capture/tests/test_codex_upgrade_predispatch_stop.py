"""通用 VC batch 预派发停线工具回归测试。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade_predispatch_stop as stop
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts


class PredispatchStopTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _fixture(
        self,
        root: Path,
        *,
        expired: bool = False,
    ) -> tuple[Path, Path, Path, Path]:
        campaign_dir = root / "campaign"
        state_dir = root / "state"
        campaign_dir.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        now = datetime.now(timezone.utc)
        if expired:
            created = now - timedelta(minutes=10)
            compiled = now - timedelta(minutes=9)
            must_start = now - timedelta(minutes=8)
            deadline = now - timedelta(minutes=7)
        else:
            created = now - timedelta(minutes=1)
            compiled = now
            must_start = now + timedelta(minutes=1)
            deadline = now + timedelta(minutes=10)
        plan = artifacts.build_campaign_plan(
            campaign_id="atomic-fixture",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc=created.isoformat(),
            original_deadline_at_utc=deadline.isoformat(),
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )
        plan_path = campaign_dir / "control/vc/campaign-plan.json"
        self._write_json(plan_path, plan)
        campaign = {
            "campaign_id": "atomic-fixture",
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "vc_control": {
                "campaign_plan": {
                    "path": "control/vc/campaign-plan.json",
                    "sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                }
            },
        }
        self._write_json(campaign_dir / "campaign.json", campaign)
        predecessor = {
            "path": "control/vc/vc-0-checkpoint.json",
            "sha256": "5" * 64,
            "phase": "VC-0",
            "checkpoint_sha256": "6" * 64,
        }
        batch = artifacts.build_vc_batch(
            campaign_plan=plan,
            phase="VC-1",
            sequence=1,
            predecessor_checkpoint=predecessor,
            execute_item_ids=[],
            reuse_item_ids=["fixture-reuse"],
            actions=[],
            compiled_at_utc=compiled.isoformat(),
            must_start_by_utc=must_start.isoformat(),
        )
        batch_path = campaign_dir / "control/vc/batches/0001-vc-1.json"
        self._write_json(batch_path, batch)
        manifest = supervisor.build_batched_campaign_run_manifest(
            campaign_id="atomic-fixture",
            campaign_plan_sha256=plan["plan_sha256"],
            batch_id=batch["batch_id"],
            batch_sequence=batch["sequence"],
            batch_sha256=batch["batch_sha256"],
            phase=batch["phase"],
            predecessor_checkpoint=batch["predecessor_checkpoint"],
            original_deadline_at_utc=batch["original_deadline_at_utc"],
            actions=batch["actions"],
            execute_items=batch["execute_item_ids"],
            reuse_items=batch["reuse_item_ids"],
        )
        manifest_path = campaign_dir / "control/vc/run-manifests/0001-vc-1.json"
        self._write_json(manifest_path, manifest)
        return campaign_dir, state_dir, batch_path, manifest_path

    def test_record_and_replay_freezes_zero_request_terminal_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            receipt_path, receipt = stop.record(
                campaign_dir=fixture[0],
                state_dir=fixture[1],
                batch_path=fixture[2],
                manifest_path=fixture[3],
                failure_kind="dispatch-before-parent-run",
                error_type="SupervisorError",
            )

            self.assertEqual(receipt["status"], "stopped")
            self.assertFalse(receipt["source_run_created"])
            self.assertFalse(receipt["successor_eligible"])
            self.assertEqual(
                receipt["metrics"],
                {"live_request_count": 0, "scanned_bytes": 0},
            )
            self.assertEqual(receipt["next_action"], stop.NEXT_ACTION)
            self.assertEqual(
                stop.replay(
                    campaign_dir=fixture[0],
                    state_dir=fixture[1],
                    receipt_path=receipt_path,
                ),
                receipt,
            )

    def test_record_accepts_expired_deadline_without_reopening_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory), expired=True)
            _path, receipt = stop.record(
                campaign_dir=fixture[0],
                state_dir=fixture[1],
                batch_path=fixture[2],
                manifest_path=fixture[3],
                failure_kind="operator-recovery",
                error_type="ProcessExit",
            )
            self.assertEqual(receipt["timing_state"], "deadline-expired")

    def test_tampered_batch_is_rejected_before_receipt_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            batch = json.loads(fixture[2].read_text(encoding="utf-8"))
            batch["reuse_item_ids"] = ["tampered"]
            self._write_json(fixture[2], batch)
            with self.assertRaises(stop.PredispatchStopError):
                stop.record(
                    campaign_dir=fixture[0],
                    state_dir=fixture[1],
                    batch_path=fixture[2],
                    manifest_path=fixture[3],
                    failure_kind="dispatch-before-parent-run",
                    error_type="SupervisorError",
                )
            self.assertFalse(
                (fixture[0] / "control/vc/predispatch-stops/0001-vc-1.json").exists()
            )

    def test_existing_parent_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            arguments = type(
                "Arguments",
                (),
                {
                    "manifest": fixture[3],
                    "state_dir": fixture[1],
                    "heartbeat_seconds": 0.05,
                    "watchdog_timeout_seconds": 1.0,
                    "ledger_interval_seconds": 0.05,
                },
            )()
            returncode, _result = supervisor._campaign_run_command(arguments)
            self.assertEqual(returncode, 0)
            with self.assertRaisesRegex(stop.PredispatchStopError, "已有父 run"):
                stop.record(
                    campaign_dir=fixture[0],
                    state_dir=fixture[1],
                    batch_path=fixture[2],
                    manifest_path=fixture[3],
                    failure_kind="operator-recovery",
                    error_type="ProcessExit",
                )

    def test_concurrent_lock_and_duplicate_receipt_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            descriptor, _state_dir = supervisor._campaign_run_lock(fixture[1])
            try:
                with self.assertRaisesRegex(
                    stop.PredispatchStopError,
                    "已有动作队列运行",
                ):
                    stop.record(
                        campaign_dir=fixture[0],
                        state_dir=fixture[1],
                        batch_path=fixture[2],
                        manifest_path=fixture[3],
                        failure_kind="operator-recovery",
                        error_type="ProcessExit",
                    )
            finally:
                os.close(descriptor)

            stop.record(
                campaign_dir=fixture[0],
                state_dir=fixture[1],
                batch_path=fixture[2],
                manifest_path=fixture[3],
                failure_kind="operator-recovery",
                error_type="ProcessExit",
            )
            with self.assertRaisesRegex(stop.PredispatchStopError, "已经存在"):
                stop.record(
                    campaign_dir=fixture[0],
                    state_dir=fixture[1],
                    batch_path=fixture[2],
                    manifest_path=fixture[3],
                    failure_kind="operator-recovery",
                    error_type="ProcessExit",
                )

    def test_replay_rejects_rehashed_semantic_and_path_tampering(self) -> None:
        """重算自摘要也不能伪造失败原因、时间状态或收据位置。"""

        cases = (
            (
                "failure-kind",
                lambda receipt: receipt["source_failure"].update(
                    {"kind": "invented-failure"}
                ),
            ),
            (
                "timing-state",
                lambda receipt: receipt.update({"timing_state": "deadline-expired"}),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    fixture = self._fixture(Path(directory))
                    receipt_path, _receipt = stop.record(
                        campaign_dir=fixture[0],
                        state_dir=fixture[1],
                        batch_path=fixture[2],
                        manifest_path=fixture[3],
                        failure_kind="dispatch-before-parent-run",
                        error_type="SupervisorError",
                    )
                    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
                    mutate(tampered)
                    tampered.pop("receipt_sha256")
                    tampered["receipt_sha256"] = artifacts.digest(tampered)
                    self._write_json(receipt_path, tampered)
                    with self.assertRaises(stop.PredispatchStopError):
                        stop.replay(
                            campaign_dir=fixture[0],
                            state_dir=fixture[1],
                            receipt_path=receipt_path,
                        )

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory))
            receipt_path, receipt = stop.record(
                campaign_dir=fixture[0],
                state_dir=fixture[1],
                batch_path=fixture[2],
                manifest_path=fixture[3],
                failure_kind="dispatch-before-parent-run",
                error_type="SupervisorError",
            )
            moved = fixture[0] / "control/vc/moved-stop.json"
            self._write_json(moved, receipt)
            with self.assertRaises(stop.PredispatchStopError):
                stop.replay(
                    campaign_dir=fixture[0],
                    state_dir=fixture[1],
                    receipt_path=moved,
                )


if __name__ == "__main__":
    unittest.main()
