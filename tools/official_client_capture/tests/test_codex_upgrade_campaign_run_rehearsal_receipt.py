"""Codex campaign-run 分批演练收据生成与重放测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade_campaign_run_rehearsal_receipt as rehearsal,
)
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts


class CampaignRunRehearsalReceiptTests(unittest.TestCase):
    def _write_json(self, path: Path, value: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _inputs(self, root: Path) -> rehearsal.PreflightInputs:
        campaign_dir = (root / "preflight").resolve()
        campaign_dir.mkdir(mode=0o700)
        control = campaign_dir / "control" / "vc"
        control.mkdir(parents=True, mode=0o700)
        (campaign_dir / "control").chmod(0o700)
        now = datetime.now(timezone.utc)
        plan = codex_upgrade_vc_artifacts.build_campaign_plan(
            campaign_id="p0-rehearsal-test",
            campaign_mode="preflight_only",
            campaign_purpose="production_replacement",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc=now.isoformat(),
            original_deadline_at_utc=(now + timedelta(minutes=10)).isoformat(),
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256=None,
            p0_gate_sha256=None,
        )
        plan_path = self._write_json(control / "campaign-plan.json", plan)
        checkpoint = codex_upgrade_vc_artifacts.build_vc_checkpoint(
            campaign_plan=plan,
            phase="VC-0",
            status="complete",
            predecessor_checkpoint=None,
            stage_receipt={
                "path": "control/vc/campaign-plan.json",
                "sha256": rehearsal._sha256_file(plan_path),
            },
            completed_at_utc=now.isoformat(),
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        checkpoint_path = self._write_json(
            control / "vc-0-checkpoint.json",
            checkpoint,
        )
        supervisor_sha256 = rehearsal._sha256_file(
            Path(codex_upgrade_supervisor.__file__).resolve()
        )
        producer_sha256 = rehearsal._sha256_file(Path(rehearsal.__file__).resolve())
        manifest = {
            "campaign_id": "p0-rehearsal-test",
            "campaign_mode": "preflight_only",
            "vc_control": {
                "campaign_plan": {
                    "path": "control/vc/campaign-plan.json",
                    "sha256": rehearsal._sha256_file(plan_path),
                },
                "vc0_checkpoint": {
                    "path": "control/vc/vc-0-checkpoint.json",
                    "sha256": rehearsal._sha256_file(checkpoint_path),
                },
            },
            "tool_identity": {
                "entries": [
                    {
                        "path": "codex_upgrade_supervisor.py",
                        "sha256": supervisor_sha256,
                    },
                    {
                        "path": Path(rehearsal.__file__).name,
                        "sha256": producer_sha256,
                    },
                ]
            },
        }
        return rehearsal.PreflightInputs(
            campaign_dir=campaign_dir,
            manifest=manifest,
            campaign_plan=plan,
            campaign_plan_path=plan_path,
            vc0_checkpoint=checkpoint,
            vc0_checkpoint_path=checkpoint_path,
            supervisor_sha256=supervisor_sha256,
            producer_sha256=producer_sha256,
        )

    def _collect(
        self,
        root: Path,
    ) -> tuple[rehearsal.PreflightInputs, Path, dict[str, object]]:
        inputs = self._inputs(root)
        evidence_root = (root / "rehearsal").resolve()
        evidence_root.mkdir(mode=0o700)
        with mock.patch.object(
            rehearsal,
            "_load_preflight_inputs",
            return_value=inputs,
        ):
            receipt = rehearsal.collect(
                evidence_root,
                "receipt.json",
                campaign_dir=inputs.campaign_dir,
            )
        return inputs, evidence_root, receipt

    def test_collect_executes_two_batches_and_rejects_deadline_drift(self) -> None:
        """两个真实父 run 必须闭合，deadline 负例不得创建第三个 run。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs, evidence_root, receipt = self._collect(root)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertEqual(
                [item["batch_sequence"] for item in receipt["runs"]],
                [1, 2],
            )
            self.assertEqual(
                len(list(evidence_root.glob("run-*/state.json"))),
                2,
            )
            self.assertFalse((evidence_root / "must-not-run.json").exists())
            for run in receipt["runs"]:
                audit = codex_upgrade_supervisor._audit_command(
                    Path(str(run["run_dir"]))
                )
                self.assertEqual(audit["state"], "stopped")
                self.assertFalse(audit["audit_incomplete"])
            with mock.patch.object(
                rehearsal,
                "_load_preflight_inputs",
                return_value=inputs,
            ):
                replayed = rehearsal.replay(
                    evidence_root,
                    "receipt.json",
                    campaign_dir=inputs.campaign_dir,
                )
            self.assertEqual(replayed, receipt)

    def test_replay_rejects_tampered_action_marker_and_extra_asset(self) -> None:
        """动作事实或 inventory 任一漂移都必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs, evidence_root, _receipt = self._collect(root)
            marker_path = evidence_root / "action-1.json"
            original = marker_path.read_bytes()
            marker = json.loads(original)
            marker["live_request_count"] = 1
            self._write_json(marker_path, marker)
            with (
                mock.patch.object(
                    rehearsal,
                    "_load_preflight_inputs",
                    return_value=inputs,
                ),
                self.assertRaisesRegex(
                    rehearsal.CampaignRunRehearsalError,
                    "marker",
                ),
            ):
                rehearsal.replay(
                    evidence_root,
                    "receipt.json",
                    campaign_dir=inputs.campaign_dir,
                )
            marker_path.write_bytes(original)
            marker_path.chmod(0o600)
            self._write_json(evidence_root / "unregistered.json", {"extra": True})
            with (
                mock.patch.object(
                    rehearsal,
                    "_load_preflight_inputs",
                    return_value=inputs,
                ),
                self.assertRaisesRegex(
                    rehearsal.CampaignRunRehearsalError,
                    "inventory",
                ),
            ):
                rehearsal.replay(
                    evidence_root,
                    "receipt.json",
                    campaign_dir=inputs.campaign_dir,
                )

    def test_collect_rejects_nonempty_root_without_overwrite(self) -> None:
        """已有输出或其他资产存在时不得覆盖或续写。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs = self._inputs(root)
            evidence_root = (root / "rehearsal").resolve()
            evidence_root.mkdir(mode=0o700)
            existing = self._write_json(
                evidence_root / "receipt.json",
                {"existing": True},
            )
            original = existing.read_bytes()
            with (
                mock.patch.object(
                    rehearsal,
                    "_load_preflight_inputs",
                    return_value=inputs,
                ),
                self.assertRaisesRegex(
                    rehearsal.CampaignRunRehearsalError,
                    "必须为空",
                ),
            ):
                rehearsal.collect(
                    evidence_root,
                    "receipt.json",
                    campaign_dir=inputs.campaign_dir,
                )
            self.assertEqual(existing.read_bytes(), original)

    def test_atomic_double_run_uses_clean_independent_vc0_vc1_roots(self) -> None:
        """真实原子入口必须在两个新根连续形成父 run 与零请求 VC-1。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            evidence_root = root / "atomic-double"
            evidence_root.mkdir(mode=0o700)
            receipt = rehearsal.collect_atomic_double(
                evidence_root,
                "receipt.json",
                require_arm64=False,
            )

            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertEqual(receipt["scanned_bytes"], 0)
            self.assertFalse(receipt["network_used"])
            self.assertTrue(
                all(
                    not Path(item["path"]).is_absolute()
                    for item in receipt["tool_identity"]["files"]
                )
            )
            self.assertEqual(
                [item["campaign_id"] for item in receipt["instances"]],
                ["atomic-vc0-vc1-1", "atomic-vc0-vc1-2"],
            )
            for item in receipt["instances"]:
                self.assertEqual(item["parent_run"]["state"], "stopped")
                self.assertEqual(
                    item["negative_fixtures"],
                    {
                        "canonical_tamper_rejected": True,
                        "existing_parent_run_rejected": True,
                        "deadline_drift_rejected": True,
                        "extra_file_detected_by_inventory": True,
                    },
                )
            self.assertEqual(
                rehearsal.replay_atomic_double(
                    evidence_root,
                    "receipt.json",
                    require_arm64=False,
                ),
                receipt,
            )

    def test_atomic_environment_matches_capture_cli_mount_contract(self) -> None:
        """容器双 runs 别名必须同 inode 且均可写，宽泛父根保持只读。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            capture = root / "capture"
            staging = root / "staging"
            runs = root / "runs"
            for path in (capture, staging, runs):
                path.mkdir(mode=0o700)
            paths = {
                Path("/capture"): capture,
                Path("/capture/staging"): staging,
                Path("/root/oauth-capture/runs"): runs,
                Path("/capture/runs"): runs,
            }
            readonly_flag = getattr(rehearsal.os, "ST_RDONLY", 1)

            def statvfs(path: Path) -> mock.Mock:
                return mock.Mock(f_flag=readonly_flag if path == capture else 0)

            with (
                mock.patch.object(
                    rehearsal.os,
                    "uname",
                    return_value=mock.Mock(machine="aarch64"),
                ),
                mock.patch.object(
                    rehearsal,
                    "_mount_directory",
                    side_effect=lambda path, _label: paths[path],
                ),
                mock.patch.object(rehearsal.os, "statvfs", side_effect=statvfs),
            ):
                environment = rehearsal._atomic_environment(require_arm64=True)

            self.assertTrue(environment["capture_root_readonly"])
            self.assertFalse(environment["staging_root_readonly"])
            self.assertFalse(environment["logical_runs_readonly"])
            self.assertFalse(environment["writable_runs_readonly"])
            self.assertTrue(environment["runs_same_inode"])

    def test_atomic_double_replay_rejects_extra_file(self) -> None:
        """任一实例出现未登记文件时，冻结 inventory 必须失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            evidence_root = root / "atomic-double"
            evidence_root.mkdir(mode=0o700)
            rehearsal.collect_atomic_double(
                evidence_root,
                "receipt.json",
                require_arm64=False,
            )
            self._write_json(
                evidence_root / "instance-2" / "unregistered.json",
                {"extra": True},
            )
            with self.assertRaisesRegex(
                rehearsal.CampaignRunRehearsalError,
                "inventory",
            ):
                rehearsal.replay_atomic_double(
                    evidence_root,
                    "receipt.json",
                    require_arm64=False,
                )

    def test_atomic_replay_rejects_rehashed_instance_and_inventory_tampering(self) -> None:
        """重算收据摘要也不能夹带实例字段或把额外文件登记成合法资产。"""

        cases = ("extra-instance-field", "changed-campaign-id", "registered-extra-file")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    root.chmod(0o700)
                    evidence_root = root / "atomic-double"
                    evidence_root.mkdir(mode=0o700)
                    rehearsal.collect_atomic_double(
                        evidence_root,
                        "receipt.json",
                        require_arm64=False,
                    )
                    receipt_path = evidence_root / "receipt.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if case == "extra-instance-field":
                        receipt["instances"][0]["unexpected"] = True
                    elif case == "changed-campaign-id":
                        receipt["instances"][0]["campaign_id"] = "forged-campaign"
                    else:
                        self._write_json(
                            evidence_root / "instance-1" / "unregistered.json",
                            {"extra": True},
                        )
                        receipt["instances"][0]["inventory"] = rehearsal._atomic_inventory(
                            evidence_root / "instance-1"
                        )
                    receipt.pop("receipt_sha256")
                    receipt["receipt_sha256"] = codex_upgrade_vc_artifacts.digest(
                        receipt
                    )
                    self._write_json(receipt_path, receipt)
                    with self.assertRaises(rehearsal.CampaignRunRehearsalError):
                        rehearsal.replay_atomic_double(
                            evidence_root,
                            "receipt.json",
                            require_arm64=False,
                        )


if __name__ == "__main__":
    unittest.main()
