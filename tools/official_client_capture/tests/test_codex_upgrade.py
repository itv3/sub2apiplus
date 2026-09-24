"""Codex CLI 升级编排器的离线门禁。"""

from __future__ import annotations

import argparse
import copy
import contextlib
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from unittest import mock

from tools.official_client_capture import candidate_evidence_guard
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.tests import project_ledger_fixture
from tools.official_client_capture.tests import runtime_egress_fixtures
from tools.official_client_capture import codex_upgrade_evidence_manifest
from tools.official_client_capture import codex_upgrade_gate_receipt
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt
from tools.official_client_capture import codex_upgrade_receipt_finalizer
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_wire_transition as wire_transition
from tools.official_client_capture import (
    codex_upgrade_harden_evidence_permissions as harden,
)
from tools.official_client_capture import (
    codex_upgrade_official_attempt_audit as attempt_audit,
)
from tools.official_client_capture.codex_upgrade import (
    build_coverage,
    compare_inventory,
    compare_surfaces,
    load_rule_manifest,
    scan_evidence,
    scan_source_tree,
)
from tools.official_client_capture.codex_upgrade import Job
from tools.official_client_capture.tests.control_receipt_fixtures import (
    create_arm_receipt,
    create_historical_p0_gate_receipt,
    create_job_rehearsal_receipt,
    create_p0_gate_receipt,
    create_release_certification,
    create_timing_checkpoint,
)


class CodexUpgradeTest(unittest.TestCase):
    # 合成场景的两个 Job id。默认值与历史用例一致；真实评估链夹具（改造 5 M1 审核修正）把它们改为
    # 正式 0.154 声明覆盖的 Job id，使受管子进程（CLI）的证据标签声明校验无需 mock 即可通过。
    synthetic_job_ids: dict[str, str] = {"official": "official-test", "candidate": "candidate-test"}

    def setUp(self) -> None:
        super().setUp()
        # 公共 Campaign 夹具使用 0.147 离线合成数据；0.151 与其他版本
        # 继续调用正式声明校验，fail-close 行为另由 rehearsal 专项测试覆盖。
        original = getattr(
            codex_upgrade_job_rehearsal_receipt,
            "_target_evidence_label_declaration_sha256",
        )

        def target_evidence_label_declaration_sha256(
            target_version: str,
            target_scenario: object,
            **kwargs: object,
        ) -> str:
            if target_version in {"0.147.0", "0.154.0"}:
                # 合成 Job id 不在正式声明内时回退固定摘要（历史行为）；真实评估链夹具用正式
                # Job id，真实声明可算出即用真实值（子进程 CLI 无 patch 也能一致）。
                try:
                    return original(target_version, target_scenario, **kwargs)
                except codex_upgrade_job_rehearsal_receipt.JobRehearsalReceiptError:
                    return "d" * 64
            return original(target_version, target_scenario, **kwargs)

        patcher = mock.patch.object(
            codex_upgrade_job_rehearsal_receipt,
            "_target_evidence_label_declaration_sha256",
            side_effect=target_evidence_label_declaration_sha256,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bound_evidence_path_accepts_legacy_attempt_relative_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attempt_root = (
                Path(directory)
                / "campaign"
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            receipt = attempt_root / "evidence" / "client" / "receipts" / "observed.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}", encoding="utf-8")
            stage = {
                "attempt": {
                    "path": "candidates/candidate-a/attempts/attempt-a/attempt.json",
                },
                "evidence_roots": [str(attempt_root / "evidence")],
            }
            binding = {
                "path": "evidence/client/receipts/observed.json",
                "sha256": codex_upgrade.file_sha256(receipt),
            }
            self.assertEqual(
                codex_upgrade._bound_evidence_path(
                    stage,
                    binding,
                    label="运行画像观测收据",
                ),
                receipt.resolve(),
            )

            first_root = (
                Path(directory) / "a" / "attempt-old" / "evidence"
            )
            second_root = (
                Path(directory) / "b" / "attempt-a" / "evidence"
            )
            second_receipt = second_root / "client" / "receipts" / "observed.json"
            second_receipt.parent.mkdir(parents=True)
            second_receipt.write_text("legacy", encoding="utf-8")
            legacy_stage = {
                "attempt": stage["attempt"],
                "evidence_roots": [str(first_root), str(second_root)],
                "evidence_inventory": {
                    "entries": [
                        {
                            "path": "002-evidence/client/receipts/observed.json",
                            "sha256": codex_upgrade.file_sha256(second_receipt),
                        }
                    ]
                },
            }
            self.assertTrue(
                codex_upgrade._stage_inventory_binding_matches(
                    legacy_stage,
                    {
                        "path": "evidence/client/receipts/observed.json",
                        "sha256": codex_upgrade.file_sha256(second_receipt),
                    },
                    label="运行画像观测",
                )
            )

    def test_third_party_client_model_uses_lite_track_and_preserves_history(self) -> None:
        self.assertEqual(
            codex_upgrade._third_party_client_model(
                {"model": "gpt-5.5", "lite_model": "gpt-5.6-luna"}
            ),
            "gpt-5.6-luna",
        )
        self.assertEqual(
            codex_upgrade._third_party_client_model({"model": "gpt-5.5"}),
            "gpt-5.5",
        )
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "第三方客户端冻结模型",
        ):
            codex_upgrade._third_party_client_model({"model": ""})

    def test_plan_requires_all_versioned_policy_inputs(self) -> None:
        parser = codex_upgrade._build_parser()
        plan_parser = next(
            action.choices["plan"]
            for action in parser._actions
            if getattr(action, "choices", None) and "plan" in action.choices
        )
        actions = {action.dest: action for action in plan_parser._actions}
        self.assertTrue(actions["rule_manifest"].required)
        self.assertTrue(actions["scenario_manifest"].required)
        self.assertTrue(actions["target_scenario_manifest"].required)
        self.assertTrue(actions["model"].required)
        self.assertTrue(actions["lite_model"].required)
        self.assertTrue(actions["campaign_mode"].required)
        self.assertTrue(actions["campaign_purpose"].required)
        self.assertTrue(actions["timing_ledger_dir"].required)
        self.assertTrue(actions["timing_receipt"].required)
        self.assertTrue(actions["arm64_environment_root"].required)
        self.assertTrue(actions["arm64_environment_receipt"].required)
        self.assertFalse(actions["job_rehearsal_root"].required)
        self.assertFalse(actions["job_rehearsal_receipt"].required)
        self.assertIsNone(actions["model"].default)
        self.assertIsNone(actions["lite_model"].default)

    def test_plan_defaults_to_opt_runtime_and_rejects_root_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(root / "defaults")
            for field in (
                "capture_codex_bin",
                "relay_codex_bin",
                "capture_code_mode_host_bin",
                "relay_code_mode_host_bin",
            ):
                setattr(arguments, field, "")
            manifest = codex_upgrade.create_campaign(arguments)
            runtime_bin = "/opt/codex-0.147.0/bin"
            self.assertEqual(
                manifest["configuration"]["capture_codex_bin"],
                f"{runtime_bin}/codex",
            )
            self.assertEqual(
                manifest["configuration"]["relay_code_mode_host_bin"],
                f"{runtime_bin}/codex-code-mode-host",
            )

            for field in (
                "capture_codex_bin",
                "relay_codex_bin",
                "capture_code_mode_host_bin",
                "relay_code_mode_host_bin",
            ):
                with self.subTest(field=field):
                    rejected = self._campaign_arguments(root / f"rejected-{field}")
                    setattr(rejected, field, f"/root/runtime/{field}")
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "不得位于 /root",
                    ):
                        codex_upgrade.create_campaign(rejected)

    def test_official_runtime_requires_world_traversal_and_execution(self) -> None:
        # ARM64 受管仓库位于 /root；若把正例夹具建在测试目录下，/root=0700
        # 会让父目录遍历检查天然失败。这里固定使用 /tmp 模拟真实 /opt。
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory).resolve()
            runtime = root / "opt" / "codex-0.151.0" / "bin"
            runtime.mkdir(parents=True)
            for path in (root, root / "opt", runtime.parent, runtime):
                path.chmod(0o755)
            binary = runtime / "codex"
            binary.write_bytes(b"codex")
            binary.chmod(0o755)

            self.assertTrue(
                codex_upgrade._is_world_traversable_executable(binary)
            )

            runtime.parent.chmod(0o700)
            self.assertFalse(
                codex_upgrade._is_world_traversable_executable(binary)
            )
            runtime.parent.chmod(0o755)
            binary.chmod(0o750)
            self.assertFalse(
                codex_upgrade._is_world_traversable_executable(binary)
            )

    def test_plan_rejects_missing_or_invalid_mode_and_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for field, value, message in (
                ("campaign_mode", None, "campaign-mode"),
                ("campaign_mode", "dry_run", "campaign-mode"),
                ("campaign_purpose", None, "campaign-purpose"),
                ("campaign_purpose", "diagnostic", "campaign-purpose"),
            ):
                arguments = self._campaign_arguments(root / field / str(value))
                setattr(arguments, field, value)
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    message,
                ):
                    codex_upgrade.create_campaign(arguments)

    def test_formal_requires_matching_full_job_rehearsal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for field in ("job_rehearsal_root", "job_rehearsal_receipt"):
                with self.subTest(field=field):
                    arguments = self._campaign_arguments(root / field)
                    setattr(arguments, field, None)
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "job-rehearsal",
                    ):
                        codex_upgrade.create_campaign(arguments)

            drifted = self._campaign_arguments(root / "drifted")
            drifted.capture_container = "other-capture"
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "完整 Job 离线演练收据未通过",
            ):
                codex_upgrade.create_campaign(drifted)

    def test_preflight_rejects_job_rehearsal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(
                root / "preflight",
                campaign_mode="preflight_only",
            )
            arguments.job_rehearsal_root = arguments.arm64_environment_root
            arguments.job_rehearsal_receipt = arguments.arm64_environment_receipt
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "preflight_only 不得消费",
            ):
                codex_upgrade.create_campaign(arguments)

    def test_recovery_preflight_accepts_current_active_phase(self) -> None:
        """恢复态离线 P0 可在 VC-4 重跑，但不会推进正式 Campaign。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(
                root / "recovery-preflight",
                campaign_mode="preflight_only",
            )
            ledger_root = arguments.timing_ledger_dir
            for index, phase in enumerate(("VC-0", "VC-1", "VC-2", "VC-3")):
                codex_upgrade_timing_ledger.append_event(
                    ledger_root,
                    event_id=f"complete-{index}-{phase.lower()}",
                    phase=phase,
                    event_type="stage_completed",
                )
                next_phase = f"VC-{index + 1}"
                if next_phase == "VC-4":
                    # 改造 2：候选级阶段开工前账本必须先激活 r1。
                    codex_upgrade_timing_ledger.append_event(
                        ledger_root,
                        event_id="stage-revision-r1",
                        phase="VC-4",
                        event_type="stage_revision",
                        revision=1,
                        candidate_id="cand-1",
                        revision_commit_sha256="6" * 64,
                    )
                codex_upgrade_timing_ledger.append_event(
                    ledger_root,
                    event_id=f"start-{index + 1}-{next_phase.lower()}",
                    phase=next_phase,
                    event_type="stage_started",
                )
            checkpoint = "receipts/recovery-vc4.json"
            codex_upgrade_timing_ledger.checkpoint(ledger_root, checkpoint)
            arguments.timing_receipt = ledger_root / checkpoint

            manifest = codex_upgrade.create_campaign(arguments)
            self.assertEqual(manifest["campaign_mode"], "preflight_only")
            self.assertEqual(
                codex_upgrade.campaign_status(arguments.campaign_dir)["status"],
                "preflight_complete",
            )

    def test_evaluation_recovery_controls_bind_stopped_original_and_new_p0(
        self,
    ) -> None:
        """原地恢复保留 91 个旧请求，并强制绑定新的完整控制闭环。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root / "original")
            original_ledger = Path(
                manifest["control_receipts"]["upgrade_timing"]["ledger_dir"]
            )
            codex_upgrade_timing_ledger.append_event(
                original_ledger,
                event_id="complete-vc0",
                phase="VC-0",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                original_ledger,
                event_id="start-vc1",
                phase="VC-1",
                event_type="stage_started",
            )
            codex_upgrade_timing_ledger.append_event(
                original_ledger,
                event_id="attempt-r26-started",
                phase="VC-1",
                event_type="attempt_started",
                attempt_id="r26",
            )
            codex_upgrade_timing_ledger.append_event(
                original_ledger,
                event_id="attempt-r26-completed",
                phase="VC-1",
                event_type="attempt_completed",
                attempt_id="r26",
                live_request_count=91,
            )
            codex_upgrade_timing_ledger.append_event(
                original_ledger,
                event_id="stop-tool-defect",
                phase="VC-1",
                event_type="stop_the_line",
                root_cause_id="evaluation-tool-defect",
                next_action="完成恢复 P0 后原地封存 r26",
            )
            codex_upgrade_timing_ledger.checkpoint(
                original_ledger,
                "receipts/stop.json",
            )
            stop_receipt = original_ledger / "receipts" / "stop.json"

            recovery_id = "upgrade-0146-test-recovery"
            recovery_ledger = root / "recovery-control" / "UpgradeTimingLedger"
            recovery_timing_receipt = create_timing_checkpoint(
                recovery_ledger,
                upgrade_id=recovery_id,
                baseline_version="0.145.0",
                target_version="0.147.0",
                campaign_purpose="validation_only",
            )
            recovery_arm_root = root / "recovery-control" / "arm64-p0"
            recovery_arm_receipt = create_arm_receipt(
                recovery_arm_root,
                phase="p0",
                subject_id=recovery_id,
                prefix="p0",
                rust_tls_codex_version="0.147.0",
            )

            preflight_arguments = self._campaign_arguments(
                root / "recovery-preflight",
                campaign_id="recovery-preflight",
                campaign_mode="preflight_only",
            )
            preflight_arguments.timing_ledger_dir = recovery_ledger
            preflight_arguments.timing_receipt = recovery_timing_receipt
            preflight_arguments.arm64_environment_root = recovery_arm_root
            preflight_arguments.arm64_environment_receipt = recovery_arm_receipt
            preflight_manifest = codex_upgrade.create_campaign(preflight_arguments)
            rehearsal_contract = codex_upgrade._job_rehearsal_contract_from_arguments(
                preflight_arguments
            )
            rehearsal_root = root / "recovery-control" / "job-rehearsal"
            rehearsal_receipt = create_job_rehearsal_receipt(
                rehearsal_root,
                contract=rehearsal_contract,
                preflight_campaign_id=preflight_manifest["campaign_id"],
                preflight_campaign_dir=preflight_arguments.campaign_dir,
                preflight_manifest_sha256=codex_upgrade.file_sha256(
                    preflight_arguments.campaign_dir / "campaign.json"
                ),
            )

            transition_arguments = argparse.Namespace(
                predecessor_stop_ledger_dir=original_ledger,
                predecessor_stop_receipt=stop_receipt,
                recovery_timing_ledger_dir=recovery_ledger,
                recovery_timing_receipt=recovery_timing_receipt,
                recovery_arm64_environment_root=recovery_arm_root,
                recovery_arm64_environment_receipt=recovery_arm_receipt,
                job_rehearsal_root=rehearsal_root,
                job_rehearsal_receipt=rehearsal_receipt,
            )
            current_tool = codex_upgrade._tool_identity(include_git=False)
            with mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=None,
            ):
                controls = codex_upgrade._phase_recovery_controls_from_arguments(
                    transition_arguments,
                    campaign_dir,
                    manifest,
                    current_tool,
                )
            self.assertEqual(
                controls["stop_checkpoint"]["total_live_request_count"],
                91,
            )
            self.assertEqual(
                controls["recovery"]["upgrade_timing"]["upgrade_id"],
                recovery_id,
            )
            with mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=None,
            ):
                self.assertEqual(
                    codex_upgrade._validate_phase_recovery_controls(
                        campaign_dir,
                        manifest,
                        controls,
                        current_tool,
                    ),
                    controls,
                )

    def test_recovery_rehearsal_only_accepts_managed_scenario_source_digest_drift(
        self,
    ) -> None:
        """恢复场景必须来自受管原文件，Formal 只承接历史章节摘要。"""

        managed_path = (
            Path(codex_upgrade.__file__).resolve().parent
            / "codex_upgrade_scenarios_0_151_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_dir = root / "formal"
            preflight_dir = root / "preflight"
            formal_path = formal_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(formal_path, historical)
            self._write_json(preflight_path, managed)
            formal_manifest = {
                "target_version": "0.151.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        formal_path,
                        "inputs/target.json",
                    )
                },
            }
            preflight_manifest = {
                "target_version": "0.151.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        preflight_path,
                        "inputs/target.json",
                    )
                },
            }

            override = codex_upgrade._recovery_rehearsal_target_scenario_override(
                formal_dir,
                formal_manifest,
                preflight_dir,
                preflight_manifest,
            )
            self.assertEqual(override, managed)

            # 2026-09-18：preflight 与 Formal 冻结的是同一份历史场景时，只读加载
            # 按历史场景复算合同（返回 None），不再要求等于当前受管原文件；
            # 受管场景在同版本内新增 Job 后，旧恢复后继 Campaign 仍能对账。
            self._write_json(preflight_path, historical)
            preflight_manifest["inputs"]["target_discovery_scenarios"] = (
                self._binding(preflight_path, "inputs/target.json")
            )
            self.assertIsNone(
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                )
            )
            # preflight 是与 Formal 冻结场景有实质差异的历史快照：按快照承接时
            # 同样只允许 source_spec.sha256 级漂移，实质变化仍拒绝。
            stray = json.loads(json.dumps(managed, ensure_ascii=False))
            stray["profile_id"] = "stray-preflight-profile"
            self._write_json(preflight_path, stray)
            preflight_manifest["inputs"]["target_discovery_scenarios"] = (
                self._binding(preflight_path, "inputs/target.json")
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "除 source_spec.sha256 外发生变化",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                )

            self._write_json(preflight_path, managed)
            preflight_manifest["inputs"]["target_discovery_scenarios"] = (
                self._binding(preflight_path, "inputs/target.json")
            )
            changed = json.loads(json.dumps(historical, ensure_ascii=False))
            changed["profile_id"] = "tampered-profile"
            self._write_json(formal_path, changed)
            formal_manifest["inputs"]["target_discovery_scenarios"] = self._binding(
                formal_path,
                "inputs/target.json",
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "除 source_spec.sha256 外发生变化",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                )

    def test_official_only_reuse_accepts_candidate_job_additions(self) -> None:
        """官方证据只读复用只要求 official Job 执行合同一致；候选侧新增 Job 由新 Campaign 承担。"""

        managed_path = (
            Path(codex_upgrade.__file__).resolve().parent
            / "codex_upgrade_scenarios_0_154_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        # 历史 Formal（VC-1 官方证据 Campaign）冻结的是没有 candidate-trace-test 的场景。
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64
        historical["capture_jobs"] = [
            job for job in historical["capture_jobs"] if job["id"] != "candidate-trace-test"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_dir = root / "formal"
            preflight_dir = root / "preflight"
            formal_path = formal_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(formal_path, historical)
            self._write_json(preflight_path, managed)
            formal_manifest = {
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(formal_path, "inputs/target.json")
                },
            }
            preflight_manifest = {
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(preflight_path, "inputs/target.json")
                },
            }
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "除 source_spec.sha256 外发生变化",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir, formal_manifest, preflight_dir, preflight_manifest
                )
            override = codex_upgrade._recovery_rehearsal_target_scenario_override(
                formal_dir,
                formal_manifest,
                preflight_dir,
                preflight_manifest,
                official_only_reuse=True,
            )
            self.assertEqual(override, managed)
            # manifest 自带 official-only predecessor 原因时同样放行（新 Campaign 的只读加载）。
            formal_manifest["predecessor"] = {"reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON}
            self.assertEqual(
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir, formal_manifest, preflight_dir, preflight_manifest
                ),
                managed,
            )
            # 官方 Job 执行字段变化仍拒绝。
            drifted = json.loads(json.dumps(historical, ensure_ascii=False))
            official = next(job for job in drifted["capture_jobs"] if job["phase"] == "official")
            official["steps"][0]["timeout_seconds"] = 7
            self._write_json(formal_path, drifted)
            formal_manifest["inputs"]["target_discovery_scenarios"] = self._binding(
                formal_path, "inputs/target.json"
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "除 source_spec.sha256 外发生变化",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    official_only_reuse=True,
                )

    def test_evaluation_epoch_command_targets_candidate_attempt_when_candidate_id_given(self) -> None:
        """--candidate-id 让 evaluation-epoch 定位 Candidate attempt；缺省仍是 official attempt。"""

        calls: list[tuple[str, str | None, str]] = []

        def fake_load(campaign_dir, phase, candidate_id, attempt_id, **_kwargs):
            calls.append((phase, candidate_id, attempt_id))
            return Path("/attempt-root"), {"status": "awaiting_receipts"}

        with mock.patch.object(codex_upgrade, "_require_formal_campaign", return_value={"campaign_id": "c1"}), mock.patch.object(
            codex_upgrade, "_load_capture_attempt", side_effect=fake_load
        ), mock.patch.object(codex_upgrade, "_tool_identity", return_value={"evidence_semantics_sha256": "e" * 64}), mock.patch.object(
            codex_upgrade.codex_upgrade_wire_transition, "append_epoch", return_value=Path("/attempt-root/evaluation-epoch-01.json")
        ), mock.patch.object(
            codex_upgrade.codex_upgrade_wire_transition, "load_epochs", return_value=[{"index": 1, "to_evidence_semantics_sha256": "e" * 64}]
        ):
            result = codex_upgrade._evaluation_epoch_command(
                argparse.Namespace(campaign_dir=Path("/c1"), attempt_id="att-1", reason="r", candidate_id="cand-1")
            )
            self.assertEqual(result["status"], "epoch_appended")
            codex_upgrade._evaluation_epoch_command(
                argparse.Namespace(campaign_dir=Path("/c1"), attempt_id="att-2", reason="r", candidate_id=None)
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "candidate-id 格式非法"):
                codex_upgrade._evaluation_epoch_command(
                    argparse.Namespace(campaign_dir=Path("/c1"), attempt_id="att-3", reason="r", candidate_id="bad/id")
                )
        self.assertEqual(calls, [("candidate", "cand-1", "att-1"), ("official", None, "att-2")])
        parser = codex_upgrade._build_parser()
        parsed = parser.parse_args(["evaluation-epoch", "--campaign-dir", "/c1", "--attempt-id", "a", "--reason", "r", "--candidate-id", "cand-1"])
        self.assertEqual(parsed.candidate_id, "cand-1")

    def test_isolated_seal_rehearsal_context_only_on_overlay_for_post_run_commands(self) -> None:
        """预演标记只在 overlay 副本上、且只对零请求 post-run 命令等同 campaign-run 派发。"""

        campaign_dir = Path("/data/evidence/campaigns/c1")
        seal = argparse.Namespace(campaign_dir=campaign_dir, capture_action="seal")
        run = argparse.Namespace(campaign_dir=campaign_dir, capture_action="run")
        compare = argparse.Namespace(campaign_dir=campaign_dir)
        with mock.patch.dict(os.environ, {codex_upgrade.SEAL_REHEARSAL_CONTEXT_ENV: "1"}):
            with mock.patch.object(codex_upgrade, "_mount_source_of", return_value=("overlay", "/data")):
                self.assertTrue(codex_upgrade._in_isolated_seal_rehearsal(seal, "capture-candidate"))
                self.assertTrue(codex_upgrade._in_isolated_seal_rehearsal(compare, "compare"))
                self.assertTrue(codex_upgrade._in_isolated_seal_rehearsal(compare, "accept"))
                # live 采集与非 post-run 命令不放行
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(run, "capture-candidate"))
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(compare, "resume"))
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(compare, "plan"))
            # 正式目录（非 overlay）上带标记仍失败关闭
            with mock.patch.object(codex_upgrade, "_mount_source_of", return_value=("ext4", "/")):
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(seal, "capture-candidate"))
            with mock.patch.object(codex_upgrade, "_mount_source_of", return_value=None):
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(seal, "capture-candidate"))
        # 无标记时一律不放行
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(codex_upgrade.SEAL_REHEARSAL_CONTEXT_ENV, None)
            with mock.patch.object(codex_upgrade, "_mount_source_of", return_value=("overlay", "/data")):
                self.assertFalse(codex_upgrade._in_isolated_seal_rehearsal(seal, "capture-candidate"))

    def test_official_reuse_target_scenario_transition_follows_candidate_job_update(
        self,
    ) -> None:
        """官方证据复用后继的 target 场景跟随受管场景的候选侧演进，并可从 predecessor-import 复算。"""

        managed_path = (
            Path(codex_upgrade.__file__).resolve().parent
            / "codex_upgrade_scenarios_0_154_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64
        historical["capture_jobs"] = [
            job for job in historical["capture_jobs"] if job["id"] != "candidate-trace-test"
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir = root / "predecessor"
            staging_dir = root / "staging"
            preflight_dir = root / "preflight"
            for campaign_root in (predecessor_dir, staging_dir, preflight_dir):
                campaign_root.mkdir(mode=0o700)
            predecessor_path = predecessor_dir / "inputs" / "target.json"
            staging_path = staging_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(predecessor_path, historical)
            self._write_json(staging_path, historical)
            self._write_json(preflight_path, managed)
            predecessor_manifest = {
                "campaign_id": "formal-predecessor",
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        predecessor_path, "inputs/target.json"
                    )
                },
            }
            successor_manifest = json.loads(json.dumps(predecessor_manifest))
            successor_manifest["campaign_id"] = "formal-successor"
            preflight_manifest = {
                "campaign_id": "preflight-current",
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        preflight_path, "inputs/target.json"
                    )
                },
            }
            self._write_json(preflight_dir / "campaign.json", preflight_manifest)
            arguments = argparse.Namespace(
                reason=codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
                job_rehearsal_root=root / "rehearsal",
                job_rehearsal_receipt=Path("receipt.json"),
            )
            copied_files: dict[str, dict[str, object]] = {}
            with mock.patch.object(
                codex_upgrade, "_control_receipt_relative", return_value="receipt.json"
            ), mock.patch.object(
                codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                "replay",
                return_value={"preflight_campaign": {"path": str(preflight_dir)}},
            ), mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_preflight_from_receipt",
                return_value=(preflight_dir, preflight_manifest),
            ):
                # 非官方证据复用原因：不触发。
                self.assertIsNone(
                    codex_upgrade._official_reuse_target_scenario_transition(
                        argparse.Namespace(reason="classification_fact_correction"),
                        staging_dir,
                        successor_manifest,
                        copied_files,
                    )
                )
                transition = codex_upgrade._official_reuse_target_scenario_transition(
                    arguments,
                    staging_dir,
                    successor_manifest,
                    copied_files,
                )
            self.assertIsNotNone(transition)
            assert transition is not None
            self.assertEqual(
                transition["reason"],
                codex_upgrade.OFFICIAL_REUSE_TARGET_SCENARIO_TRANSITION_REASON,
            )
            self.assertEqual(transition["added_job_ids"], ["candidate-trace-test"])
            self.assertEqual(transition["removed_job_ids"], [])
            self.assertEqual(transition["changed_job_ids"], [])
            self.assertEqual(
                transition["predecessor"],
                predecessor_manifest["inputs"]["target_discovery_scenarios"],
            )
            self.assertEqual(
                transition["successor"],
                {"path": "inputs/target.json", "sha256": codex_upgrade.file_sha256(preflight_path)},
            )
            self.assertEqual(
                transition["preflight_campaign"],
                {
                    "campaign_id": "preflight-current",
                    "manifest_sha256": codex_upgrade.file_sha256(preflight_dir / "campaign.json"),
                },
            )
            # staging 场景已被受管快照逐字替换，manifest 与 copied_files 同步。
            self.assertEqual(
                codex_upgrade.file_sha256(staging_path),
                codex_upgrade.file_sha256(preflight_path),
            )
            self.assertEqual(
                successor_manifest["inputs"]["target_discovery_scenarios"],
                transition["successor"],
            )
            self.assertEqual(
                copied_files["inputs/target.json"]["kind"],
                "official_reuse_target_scenario",
            )
            schema = json.loads(
                Path(codex_upgrade.__file__)
                .with_name("codex_upgrade_predecessor_import.schema.json")
                .read_text(encoding="utf-8")
            )
            self.assertIn(
                "official_reuse_target_scenario",
                schema["$defs"]["copiedFile"]["properties"]["kind"]["enum"],
            )
            self.assertEqual(
                set(transition),
                set(schema["$defs"]["officialReuseTargetScenarioTransition"]["required"]),
            )
            # predecessor-import 读取侧按 Git 两端文件复算过渡。
            receipt = {
                "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
                "official_reuse_target_scenario_transition": transition,
            }
            self.assertTrue(
                codex_upgrade._validate_official_reuse_target_scenario_import(
                    receipt,
                    campaign_dir=staging_dir,
                    manifest=successor_manifest,
                    predecessor_dir=predecessor_dir,
                    predecessor_manifest=predecessor_manifest,
                )
            )
            self.assertFalse(
                codex_upgrade._validate_official_reuse_target_scenario_import(
                    {"reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON},
                    campaign_dir=staging_dir,
                    manifest=successor_manifest,
                    predecessor_dir=predecessor_dir,
                    predecessor_manifest=predecessor_manifest,
                )
            )
            tampered = json.loads(json.dumps(transition))
            tampered["added_job_ids"] = []
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "Job 集漂移"):
                codex_upgrade._validate_official_reuse_target_scenario_import(
                    {
                        "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
                        "official_reuse_target_scenario_transition": tampered,
                    },
                    campaign_dir=staging_dir,
                    manifest=successor_manifest,
                    predecessor_dir=predecessor_dir,
                    predecessor_manifest=predecessor_manifest,
                )
            # 场景相同（无候选侧演进）时不产生过渡。
            self._write_json(staging_dir / "inputs" / "same.json", managed)
            same_manifest = {
                "campaign_id": "formal-same",
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        staging_dir / "inputs" / "same.json", "inputs/same.json"
                    )
                },
            }
            with mock.patch.object(
                codex_upgrade, "_control_receipt_relative", return_value="receipt.json"
            ), mock.patch.object(
                codex_upgrade.codex_upgrade_job_rehearsal_receipt, "replay", return_value={}
            ), mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_preflight_from_receipt",
                return_value=(preflight_dir, preflight_manifest),
            ):
                self.assertIsNone(
                    codex_upgrade._official_reuse_target_scenario_transition(
                        arguments, staging_dir, same_manifest, {}
                    )
                )

    def test_v7_failed_job_recovery_allows_only_non_execution_scenario_drift(
        self,
    ) -> None:
        """v7 定向恢复只可承接执行合同完全相同的场景元数据变化。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_154_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64
        historical["capture_jobs"][-1]["description"] = "v7 历史说明"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_dir = root / "formal"
            preflight_dir = root / "preflight"
            formal_path = formal_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(formal_path, historical)
            self._write_json(preflight_path, managed)
            formal_manifest = {
                "target_version": "0.154.0",
                "predecessor": {
                    "reason": "candidate_failed_job_tool_recovery",
                    "campaign_id": codex_upgrade.C0154_V7_RECOVERY_SOURCE[
                        "campaign_id"
                    ],
                },
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        formal_path,
                        "inputs/target.json",
                    )
                },
            }
            preflight_manifest = {
                "target_version": "0.154.0",
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        preflight_path,
                        "inputs/target.json",
                    )
                },
            }

            source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
            with mock.patch.object(
                codex_upgrade,
                "_require_c0154_v7_recovery_source_binding",
                return_value={"campaign_id": source["campaign_id"]},
            ) as source_guard:
                self.assertEqual(
                    codex_upgrade._recovery_rehearsal_target_scenario_override(
                        formal_dir,
                        formal_manifest,
                        preflight_dir,
                        preflight_manifest,
                        recovery_candidate_id=source["candidate_id"],
                        recovery_attempt_id=source["attempt_id"],
                    ),
                    managed,
                )
                source_guard.assert_called_once_with(
                    formal_dir,
                    formal_manifest,
                    candidate_id=source["candidate_id"],
                    attempt_id=source["attempt_id"],
                )

            formal_manifest["predecessor"]["campaign_id"] = "another-campaign"
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "v7 失败 Job 恢复",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    recovery_candidate_id=source["candidate_id"],
                    recovery_attempt_id=source["attempt_id"],
                )

            formal_manifest["predecessor"]["campaign_id"] = (
                codex_upgrade.C0154_V7_RECOVERY_SOURCE["campaign_id"]
            )

            changed_execution = json.loads(
                json.dumps(historical, ensure_ascii=False)
            )
            changed_execution["capture_jobs"][-1]["required"] = not bool(
                changed_execution["capture_jobs"][-1]["required"]
            )
            self._write_json(formal_path, changed_execution)
            formal_manifest["inputs"]["target_discovery_scenarios"] = self._binding(
                formal_path,
                "inputs/target.json",
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_c0154_v7_recovery_source_binding",
                    return_value={"campaign_id": source["campaign_id"]},
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "除 source_spec.sha256 外发生变化",
                ),
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    recovery_candidate_id=source["candidate_id"],
                    recovery_attempt_id=source["attempt_id"],
                )

    def test_a15_post_run_seal_allows_only_non_execution_scenario_drift(
        self,
    ) -> None:
        """A15 seal 后继只可承接执行合同完全相同的场景元数据变化。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_154_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64
        historical["capture_jobs"][-1]["description"] = "A15 历史说明"
        source = codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_dir = root / "formal"
            preflight_dir = root / "preflight"
            formal_path = formal_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(formal_path, historical)
            self._write_json(preflight_path, managed)
            formal_manifest = {
                "target_version": source["target_version"],
                "predecessor": {
                    "campaign_dir": source["campaign_dir"],
                    "campaign_id": source["campaign_id"],
                    "campaign_manifest_sha256": source[
                        "campaign_manifest_sha256"
                    ],
                    "reason": codex_upgrade.POST_RUN_SEAL_RECOVERY_REASON,
                },
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        formal_path,
                        "inputs/target.json",
                    )
                },
            }
            preflight_manifest = {
                "target_version": source["target_version"],
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        preflight_path,
                        "inputs/target.json",
                    )
                },
            }

            with mock.patch.object(
                codex_upgrade,
                "_require_c0154_a15_post_run_seal_source_binding",
                return_value={"source_attempt": {}},
            ) as source_guard:
                self.assertEqual(
                    codex_upgrade._recovery_rehearsal_target_scenario_override(
                        formal_dir,
                        formal_manifest,
                        preflight_dir,
                        preflight_manifest,
                        recovery_candidate_id=source["candidate_id"],
                        recovery_attempt_id=source["attempt_id"],
                    ),
                    managed,
                )
                source_guard.assert_called_once_with(
                    formal_dir,
                    formal_manifest,
                    candidate_id=source["candidate_id"],
                    attempt_id=source["attempt_id"],
                )

            changed_execution = json.loads(
                json.dumps(historical, ensure_ascii=False)
            )
            changed_execution["capture_jobs"][-1]["required"] = not bool(
                changed_execution["capture_jobs"][-1]["required"]
            )
            self._write_json(formal_path, changed_execution)
            formal_manifest["inputs"]["target_discovery_scenarios"] = self._binding(
                formal_path,
                "inputs/target.json",
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_c0154_a15_post_run_seal_source_binding",
                    return_value={"source_attempt": {}},
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "除 source_spec.sha256 外发生变化",
                ),
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    recovery_candidate_id=source["candidate_id"],
                    recovery_attempt_id=source["attempt_id"],
                )

    def test_v7_failed_job_recovery_allows_registered_timing_schema_rebase(
        self,
    ) -> None:
        """登记为零执行控制前提的 timing schema 可重基，普通控制工具仍拒绝。"""

        registered_paths = {
            "run_candidate_core_capture.sh",
            "codex_upgrade_candidate_readiness.py",
            "codex_upgrade_scenarios_0_154_0.json",
            "codex_upgrade_timing_ledger.schema.json",
        }
        components = {
            codex_upgrade._tool_component_for_path(path)
            for path in registered_paths
        }
        self.assertEqual(
            components,
            {"relay", "shared", "scenario", "evaluator"},
        )
        self.assertTrue(
            components.issubset(
                codex_upgrade._RUNTIME_SUCCESSOR_ALLOWED_REBASE_COMPONENTS
            )
        )
        self.assertNotIn(
            codex_upgrade._tool_component_for_path(
                "codex_upgrade_supervisor.py"
            ),
            codex_upgrade._RUNTIME_SUCCESSOR_ALLOWED_REBASE_COMPONENTS,
        )

    def test_v7_failed_job_recovery_requires_exactly_one_source_mode(
        self,
    ) -> None:
        """场景无漂移时也必须在创建来源与发布收据之间严格二选一。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_154_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal_dir = root / "formal"
            preflight_dir = root / "preflight"
            formal_path = formal_dir / "inputs" / "target.json"
            preflight_path = preflight_dir / "inputs" / "target.json"
            self._write_json(formal_path, managed)
            self._write_json(preflight_path, managed)
            formal_manifest = {
                "campaign_id": "successor-a",
                "target_version": source["target_version"],
                "predecessor": {
                    "campaign_dir": source["campaign_dir"],
                    "campaign_id": source["campaign_id"],
                    "campaign_manifest_sha256": source[
                        "campaign_manifest_sha256"
                    ],
                    "reason": "candidate_failed_job_tool_recovery",
                },
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        formal_path,
                        "inputs/target.json",
                    )
                },
            }
            preflight_manifest = {
                "target_version": source["target_version"],
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        preflight_path,
                        "inputs/target.json",
                    )
                },
            }

            with mock.patch.object(
                codex_upgrade,
                "_require_c0154_v7_recovery_source_binding",
                return_value={"abandoned_candidate_attempt": {}},
            ) as creation_source:
                self.assertIsNone(
                    codex_upgrade._recovery_rehearsal_target_scenario_override(
                        formal_dir,
                        formal_manifest,
                        preflight_dir,
                        preflight_manifest,
                        recovery_candidate_id=source["candidate_id"],
                        recovery_attempt_id=source["attempt_id"],
                    )
                )
                creation_source.assert_called_once()

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "必须同时提供 Candidate 与 attempt",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    recovery_candidate_id=source["candidate_id"],
                )

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "必须且只能选择",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                )

            self._write_json(formal_dir / "predecessor-import.json", {})
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "必须且只能选择",
            ):
                codex_upgrade._recovery_rehearsal_target_scenario_override(
                    formal_dir,
                    formal_manifest,
                    preflight_dir,
                    preflight_manifest,
                    recovery_candidate_id=source["candidate_id"],
                    recovery_attempt_id=source["attempt_id"],
                )

            with mock.patch.object(
                codex_upgrade,
                "_published_c0154_v7_recovery_coordinates",
                return_value=(source["candidate_id"], source["attempt_id"]),
            ) as runtime_source:
                self.assertIsNone(
                    codex_upgrade._recovery_rehearsal_target_scenario_override(
                        formal_dir,
                        formal_manifest,
                        preflight_dir,
                        preflight_manifest,
                    )
                )
                runtime_source.assert_called_once_with(formal_dir, formal_manifest)

    def test_reclassification_explicit_noop_uses_preflight_scenario_without_recovery_transition(
        self,
    ) -> None:
        """分类纠正显式 no-op 即使没有控制 transition 也必须读取当前场景。"""

        arguments = argparse.Namespace(
            reason="classification_fact_correction",
            job_rehearsal_root=Path("/control/job-rehearsal"),
            job_rehearsal_receipt=Path("receipt.json"),
        )
        preflight_dir = Path("/control/preflight")
        preflight_manifest = {"campaign_mode": "preflight_only"}
        current_scenario = {"codex_version": "0.151.0"}
        with (
            mock.patch.object(
                codex_upgrade,
                "_successor_incremental_noop_preflight_from_coordinates",
                return_value=(preflight_dir, preflight_manifest),
            ) as noop_preflight,
            mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=current_scenario,
            ) as scenario,
        ):
            actual = codex_upgrade._successor_rehearsal_target_scenario_override(
                arguments,
                Path("/campaign/.successor-staging"),
                {"target_version": "0.151.0"},
                reclassification_successor=True,
                recovery_control_transition=None,
        )

        self.assertEqual(actual, current_scenario)
        noop_preflight.assert_called_once_with(
            Path("/control/job-rehearsal"),
            Path("receipt.json"),
            {"target_version": "0.151.0"},
            label="分类纠正后继显式 Job 演练",
        )
        scenario.assert_called_once_with(
            Path("/campaign/.successor-staging"),
            {"target_version": "0.151.0"},
            preflight_dir,
            preflight_manifest,
        )

    def test_reclassification_inherited_noop_uses_its_preflight_scenario(
        self,
    ) -> None:
        """分类纠正直接继承 no-op 时也不得退回 Formal 历史场景。"""

        arguments = argparse.Namespace(
            reason="classification_fact_correction",
            job_rehearsal_root=None,
            job_rehearsal_receipt=None,
        )
        preflight_dir = Path("/control/preflight")
        preflight_manifest = {"campaign_mode": "preflight_only"}
        current_scenario = {"codex_version": "0.151.0"}
        manifest = {"target_version": "0.151.0"}
        with (
            mock.patch.object(
                codex_upgrade,
                "_successor_incremental_noop_preflight",
                return_value=(preflight_dir, preflight_manifest),
            ) as inherited,
            mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=current_scenario,
            ) as scenario,
        ):
            actual = codex_upgrade._successor_rehearsal_target_scenario_override(
                arguments,
                Path("/campaign/.successor-staging"),
                manifest,
                reclassification_successor=True,
                recovery_control_transition=None,
            )

        self.assertEqual(actual, current_scenario)
        inherited.assert_called_once_with(manifest)
        scenario.assert_called_once_with(
            Path("/campaign/.successor-staging"),
            manifest,
            preflight_dir,
            preflight_manifest,
        )

    def test_failed_job_recovery_uses_current_preflight_scenario(self) -> None:
        """失败 Job 正式恢复必须用当前 preflight 场景复算演练合同。"""

        arguments = argparse.Namespace(
            reason="candidate_failed_job_tool_recovery",
            job_rehearsal_root=Path("/control/job-rehearsal"),
            job_rehearsal_receipt=Path("receipt.json"),
            predecessor_candidate_id=codex_upgrade.C0154_V7_RECOVERY_SOURCE[
                "candidate_id"
            ],
            predecessor_attempt_id=codex_upgrade.C0154_V7_RECOVERY_SOURCE[
                "attempt_id"
            ],
        )
        preflight_dir = Path("/control/preflight")
        preflight_manifest = {"campaign_mode": "preflight_only"}
        current_scenario = {"codex_version": "0.154.0"}
        manifest = {"target_version": "0.154.0"}
        transition = {"reason": "stopped_to_active"}
        with (
            mock.patch.object(
                codex_upgrade,
                "_assert_recovery_rehearsal_uses_successor_controls",
                return_value=(preflight_dir, preflight_manifest),
            ) as controls,
            mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=current_scenario,
            ) as scenario,
        ):
            actual = codex_upgrade._successor_rehearsal_target_scenario_override(
                arguments,
                Path("/campaign/.successor-staging"),
                manifest,
                reclassification_successor=False,
                recovery_control_transition=transition,
            )

        self.assertEqual(actual, current_scenario)
        controls.assert_called_once_with(arguments, manifest)
        scenario.assert_called_once_with(
            Path("/campaign/.successor-staging"),
            manifest,
            preflight_dir,
            preflight_manifest,
            recovery_candidate_id=codex_upgrade.C0154_V7_RECOVERY_SOURCE[
                "candidate_id"
            ],
            recovery_attempt_id=codex_upgrade.C0154_V7_RECOVERY_SOURCE[
                "attempt_id"
            ],
        )

    def test_post_run_seal_recovery_uses_current_preflight_scenario(self) -> None:
        """A15 seal 正式恢复必须把冻结来源坐标传给场景兼容门禁。"""

        source = codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE
        arguments = argparse.Namespace(
            reason=codex_upgrade.POST_RUN_SEAL_RECOVERY_REASON,
            job_rehearsal_root=Path("/control/job-rehearsal"),
            job_rehearsal_receipt=Path("receipt.json"),
            predecessor_candidate_id=source["candidate_id"],
            predecessor_attempt_id=source["attempt_id"],
        )
        preflight_dir = Path("/control/preflight")
        preflight_manifest = {"campaign_mode": "preflight_only"}
        current_scenario = {"codex_version": "0.154.0"}
        manifest = {"target_version": "0.154.0"}
        transition = {"reason": "stopped_to_active"}
        with (
            mock.patch.object(
                codex_upgrade,
                "_assert_recovery_rehearsal_uses_successor_controls",
                return_value=(preflight_dir, preflight_manifest),
            ),
            mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_target_scenario_override",
                return_value=current_scenario,
            ) as scenario,
        ):
            actual = codex_upgrade._successor_rehearsal_target_scenario_override(
                arguments,
                Path("/campaign/.successor-staging"),
                manifest,
                reclassification_successor=False,
                recovery_control_transition=transition,
            )

        self.assertEqual(actual, current_scenario)
        scenario.assert_called_once_with(
            Path("/campaign/.successor-staging"),
            manifest,
            preflight_dir,
            preflight_manifest,
            recovery_candidate_id=source["candidate_id"],
            recovery_attempt_id=source["attempt_id"],
        )

    def test_classification_noop_preflight_does_not_replace_successor_controls(
        self,
    ) -> None:
        """no-op 来源控制可不同；其 preflight 身份和当前控制仍必须各自有效。"""

        with tempfile.TemporaryDirectory() as directory:
            preflight_dir = Path(directory) / "preflight"
            preflight_dir.mkdir()
            manifest_path = preflight_dir / "campaign.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            preflight_manifest = {
                "campaign_mode": "preflight_only",
                "campaign_id": "current-noop-preflight",
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
                "campaign_purpose": "production_replacement",
                "tool_identity": {"files_sha256": "a" * 64},
                "control_receipts": {
                    "upgrade_timing": {"upgrade_id": "noop-ledger"},
                    "arm64_environment": {"subject_id": "noop-ledger"},
                },
            }
            successor_manifest = {
                "baseline_version": "0.149.1",
                "target_version": "0.151.0",
                "campaign_purpose": "production_replacement",
                "tool_identity": {"files_sha256": "a" * 64},
                "control_receipts": {
                    "upgrade_timing": {"upgrade_id": "formal-ledger"},
                    "arm64_environment": {"subject_id": "formal-ledger"},
                },
            }
            rehearsal = {
                "preflight_campaign": {
                    "path": str(preflight_dir),
                    "campaign_id": "current-noop-preflight",
                    "manifest_sha256": codex_upgrade.file_sha256(manifest_path),
                }
            }
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=preflight_manifest,
            ):
                actual = codex_upgrade._recovery_rehearsal_preflight_from_receipt(
                    rehearsal,
                    successor_manifest,
                    require_successor_controls=False,
                )
                self.assertEqual(actual, (preflight_dir, preflight_manifest))
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "控制合同不一致",
                ):
                    codex_upgrade._recovery_rehearsal_preflight_from_receipt(
                        rehearsal,
                        successor_manifest,
                    )

    def test_plan_identity_uses_transition_when_original_ledger_is_stopped(
        self,
    ) -> None:
        """阶段限定漂移先读取 transition，不再要求旧 Ledger 继续 active。"""

        def identity(sha256: str) -> dict[str, object]:
            entries = [{"path": "codex_upgrade.py", "sha256": sha256}]
            return {
                "git_commit": None,
                "entry_count": 1,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                "entries": entries,
                **codex_upgrade._tool_identity_sides(entries),
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Cargo.lock").write_text("lock", encoding="utf-8")
            package = root / "package.tar.gz"
            package.write_bytes(b"package")
            before = identity("a" * 64)
            current = identity("b" * 64)
            package_identity = {"asset_sha256": "c" * 64}
            manifest = {
                "configuration": {
                    "target_source": str(source),
                    "target_package": str(package),
                },
                "official_identity": {
                    "source_tree_sha256": "d" * 64,
                    "cargo_lock_sha256": "e" * 64,
                    "package": package_identity,
                },
                "target_version": "0.151.0",
                "target_sha256": "f" * 64,
                "tool_identity": before,
            }
            calls: list[bool] = []

            def verify_controls(*args: object, require_active: bool) -> None:
                calls.append(require_active)
                if require_active:
                    raise AssertionError("旧停线 Ledger 不应再被要求 active")

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_verify_control_receipts",
                    side_effect=verify_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_directory_tree_digest",
                    return_value="d" * 64,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "file_sha256",
                    return_value="e" * 64,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_codex_package",
                    return_value=package_identity,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=current,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_phase_evaluation_transition",
                    return_value={"path": "transition.json", "sha256": "1" * 64},
                ),
                mock.patch.object(codex_upgrade, "_record_evaluation_side_drift"),
            ):
                binding = codex_upgrade._verify_plan_identity(
                    root,
                    manifest,
                    operation="capture-candidate-seal",
                    attempt_root=root / "attempt",
                    attempt={"attempt_id": "r26"},
                )
            self.assertEqual(calls, [False])
            self.assertEqual(binding["path"], "transition.json")

    def test_metadata_only_seal_accepts_only_zero_live_permanent_stop_boundary(
        self,
    ) -> None:
        manifest = {
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
        }
        frozen = {
            "status": "active",
            "active_phase": "VC-0",
            "total_live_request_count": 0,
            "head_sequence": 1,
        }
        current = {
            **frozen,
            "status": "stopped",
            "head_sequence": 2,
            "next_action": "permanent-stop-control-replacement-expired-before-reservation",
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
        }
        self.assertTrue(
            codex_upgrade._metadata_only_stopped_timing_allowed(
                frozen,
                current,
                manifest,
            )
        )
        for mutation in (
            {"total_live_request_count": 1},
            {"head_sequence": 3},
            {"next_action": "manual-stop"},
            {"campaign_purpose": "validation_only"},
        ):
            with self.subTest(mutation=mutation):
                invalid = {**current, **mutation}
                self.assertFalse(
                    codex_upgrade._metadata_only_stopped_timing_allowed(
                        frozen,
                        invalid,
                        manifest,
                    )
                )

    def test_metadata_only_historical_epoch_fallback_requires_budget_only_zero_boundary(
        self,
    ) -> None:
        """历史 epoch 仅预算到期且全零时才可回退冻结控制。"""

        manifest = {
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
            "control_receipts": {
                "upgrade_timing": {"ledger_dir": "/frozen"},
            },
        }
        epoch = {
            "boundary": codex_upgrade._control_epoch_zero_boundary(),
            "invariants": {"required_active_phase": "VC-2"},
            "successor_controls": {
                "upgrade_timing": {"ledger_dir": "/latest"},
            },
        }
        frozen_summary = {
            "status": "active",
            "active_phase": "VC-0",
            "total_live_request_count": 0,
            "head_sequence": 1,
        }
        frozen_current = {
            **frozen_summary,
            "status": "stopped",
            "head_sequence": 2,
            "next_action": "permanent-stop-control-replacement-expired-before-reservation",
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "campaign_purpose": "production_replacement",
        }
        latest_current = {
            "status": "stop_required",
            "active_phase": "VC-2",
            "total_live_request_count": 0,
            "same_root_cause_failures": {},
        }

        def checkpoint(
            timing: Mapping[str, Any],
            *,
            label: str,
        ) -> tuple[Path, Mapping[str, Any]]:
            root = Path(str(timing["ledger_dir"]))
            summary = frozen_summary if root.name == "frozen" else {"status": "active"}
            return root, {"summary": summary}

        with (
            mock.patch.object(
                codex_upgrade,
                "_sealed_stage_timing_checkpoint",
                side_effect=checkpoint,
            ),
            mock.patch.object(
                codex_upgrade.codex_upgrade_timing_ledger,
                "inspect_ledger",
                side_effect=lambda root: (
                    frozen_current if root.name == "frozen" else latest_current
                ),
            ),
        ):
            self.assertTrue(
                codex_upgrade._metadata_only_historical_epoch_fallback_allowed(
                    Path("/campaign"), manifest, epoch
                )
            )

        for mutation in (
            {"boundary": {**epoch["boundary"], "attempt_count": 1}},
            {"latest_status": "stopped"},
            {"live": 1},
            {"failures": {"root": 1}},
        ):
            with self.subTest(mutation=mutation):
                mutated_epoch = dict(epoch)
                mutated_latest = dict(latest_current)
                if "boundary" in mutation:
                    mutated_epoch["boundary"] = mutation["boundary"]
                if "latest_status" in mutation:
                    mutated_latest["status"] = mutation["latest_status"]
                if "live" in mutation:
                    mutated_latest["total_live_request_count"] = mutation["live"]
                if "failures" in mutation:
                    mutated_latest["same_root_cause_failures"] = mutation["failures"]
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_sealed_stage_timing_checkpoint",
                        side_effect=checkpoint,
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_timing_ledger,
                        "inspect_ledger",
                        side_effect=lambda root, current=mutated_latest: (
                            frozen_current if root.name == "frozen" else current
                        ),
                    ),
                ):
                    self.assertFalse(
                        codex_upgrade._metadata_only_historical_epoch_fallback_allowed(
                            Path("/campaign"), manifest, mutated_epoch
                        )
                    )

    def test_metadata_only_restoration_reuses_source_after_without_probe(self) -> None:
        """metadata-only seal 只复制来源 after，不重新执行环境探针。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source-evidence"
            target = base / "target-evidence"
            source_after = source / "environment" / "after"
            target_client_after = target / "environment" / "client-after"
            source_after.mkdir(parents=True, mode=0o700)
            target_client_after.mkdir(parents=True, mode=0o700)

            def write(path: Path, payload: object) -> None:
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                path.chmod(0o600)

            observed_at = "2026-09-06T08:50:00Z"
            snapshot_rows: list[dict[str, object]] = []
            for kind, name in codex_upgrade.ENVIRONMENT_STATE_FILES.items():
                payload = {"kind": kind, "stable": True}
                path = source_after / name
                write(path, payload)
                snapshot_rows.append(
                    {
                        "bytes": path.stat().st_size,
                        "comparison": {"mode": "equal"},
                        "kind": kind,
                        "path": name,
                        "sha256": codex_upgrade.file_sha256(path),
                    }
                )
            probe = {
                "schema_version": "codex-upgrade-environment-probe/v1",
                "phase": "after",
                "observed_at_utc": observed_at,
                "selected_account_id": 1,
                "selected_key_id": "key",
                "snapshots": snapshot_rows,
                "targets": {},
            }
            source_probe = source_after / "probe-manifest.json"
            write(source_probe, probe)
            client_probe = target_client_after / "probe-manifest.json"
            write(
                client_probe,
                {
                    "schema_version": "codex-upgrade-environment-probe/v1",
                    "phase": "after",
                    "observed_at_utc": observed_at,
                },
            )
            source_environment = {
                "after_probe": {
                    "path": "environment/after/probe-manifest.json",
                    "sha256": codex_upgrade.file_sha256(source_probe),
                    "bytes": source_probe.stat().st_size,
                }
            }
            receipt_path = target / "receipts" / "client-restoration-report.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                ) as probe_capture,
                mock.patch.object(
                    codex_upgrade,
                    "_finalize_attempt_restoration",
                    return_value=(receipt_path, {"status": "complete"}),
                ) as finalize,
            ):
                result = codex_upgrade._candidate_post_client_restoration(
                    {},
                    target,
                    "candidate-a",
                    source_evidence_root=source,
                    source_environment=source_environment,
                )
            self.assertTrue(result[3])
            probe_capture.assert_not_called()
            finalize.assert_called_once()
            self.assertEqual(
                finalize.call_args.kwargs["before_directory"], "after"
            )
            self.assertEqual(
                finalize.call_args.kwargs["after_directory"], "client-after"
            )
            for name in sorted(
                {"probe-manifest.json", *codex_upgrade.ENVIRONMENT_STATE_FILES.values()}
            ):
                self.assertEqual(
                    (target / "environment" / "after" / name).read_bytes(),
                    (source_after / name).read_bytes(),
                )

    def test_metadata_only_restoration_rejects_source_snapshot_drift(self) -> None:
        """来源 after 任一状态快照摘要漂移时必须 fail-close。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source-evidence"
            target = base / "target-evidence"
            source_after = source / "environment" / "after"
            source_after.mkdir(parents=True, mode=0o700)
            snapshots: list[dict[str, object]] = []
            for kind, name in codex_upgrade.ENVIRONMENT_STATE_FILES.items():
                path = source_after / name
                path.write_text(json.dumps({"kind": kind}) + "\n", encoding="utf-8")
                path.chmod(0o600)
                snapshots.append(
                    {
                        "bytes": path.stat().st_size,
                        "comparison": {"mode": "equal"},
                        "kind": kind,
                        "path": name,
                        "sha256": "0" * 64,
                    }
                )
            probe_path = source_after / "probe-manifest.json"
            probe_path.write_text(
                json.dumps(
                    {
                        "phase": "after",
                        "observed_at_utc": "2026-09-06T08:50:00Z",
                        "snapshots": snapshots,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            probe_path.chmod(0o600)
            source_environment = {
                "after_probe": {
                    "path": "environment/after/probe-manifest.json",
                    "sha256": codex_upgrade.file_sha256(probe_path),
                    "bytes": probe_path.stat().st_size,
                }
            }
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "快照摘要漂移",
            ):
                codex_upgrade._candidate_post_client_restoration(
                    {},
                    target,
                    "candidate-a",
                    source_evidence_root=source,
                    source_environment=source_environment,
                )

    def test_metadata_only_environment_projection_copies_complete_bound_state(
        self,
    ) -> None:
        """新 attempt 投影完整环境，并在新证据根重建恢复收据。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "source-evidence"
            target = base / "target-evidence"
            source.mkdir(mode=0o700)
            target.mkdir(mode=0o700)
            source_environment: dict[str, object] = {
                "evidence_root": str(source),
            }

            for snapshot_name in ("before", "after"):
                snapshot_root = source / "environment" / snapshot_name
                snapshot_root.mkdir(parents=True, mode=0o700)
                snapshots: list[dict[str, object]] = []
                for kind, name in codex_upgrade.ENVIRONMENT_STATE_FILES.items():
                    state_path = snapshot_root / name
                    self._write_json(
                        state_path,
                        {"kind": kind, "snapshot": snapshot_name, "stable": True},
                    )
                    snapshots.append(
                        {
                            "bytes": state_path.stat().st_size,
                            "comparison": {"mode": "equal"},
                            "kind": kind,
                            "path": name,
                            "sha256": codex_upgrade.file_sha256(state_path),
                        }
                    )
                probe_path = snapshot_root / "probe-manifest.json"
                self._write_json(
                    probe_path,
                    {
                        "phase": snapshot_name,
                        "observed_at_utc": "2026-09-17T00:00:00Z",
                        "snapshots": snapshots,
                    },
                )
                source_environment[f"{snapshot_name}_probe"] = {
                    "path": f"environment/{snapshot_name}/probe-manifest.json",
                    "sha256": codex_upgrade.file_sha256(probe_path),
                    "bytes": probe_path.stat().st_size,
                }

                arm64_root = source / "environment" / f"arm64-{snapshot_name}"
                arm64_root.mkdir(parents=True, mode=0o700)
                facts_path = arm64_root / "facts.json"
                receipt_path = arm64_root / "receipt.json"
                self._write_json(
                    facts_path,
                    {"phase": f"attempt_{snapshot_name}", "source": True},
                )
                self._write_json(
                    receipt_path,
                    {"facts": {"path": "facts.json"}, "source": True},
                )
                source_environment[f"arm64_{snapshot_name}_receipt"] = {
                    "path": f"environment/arm64-{snapshot_name}/receipt.json",
                    "sha256": codex_upgrade.file_sha256(receipt_path),
                    "bytes": receipt_path.stat().st_size,
                }

            def replay_arm64(root: Path, name: str) -> dict[str, object]:
                self.assertEqual(name, "receipt.json")
                snapshot_name = root.name.removeprefix("arm64-")
                return {
                    "status": "passed",
                    "phase": f"attempt_{snapshot_name}",
                    "subject_id": "source-attempt-a",
                }

            def finalize_restoration(
                evidence_root: Path,
                *,
                phase: str,
                candidate_id: str | None,
                **_kwargs: object,
            ) -> tuple[Path, dict[str, object]]:
                self.assertEqual(evidence_root, target)
                self.assertEqual(phase, "candidate")
                self.assertEqual(candidate_id, "candidate-a")
                report_path = evidence_root / "receipts" / "restoration-report.json"
                self._write_json(
                    report_path,
                    {"status": "restored", "evidence_root": str(evidence_root)},
                )
                return report_path, {"status": "restored"}

            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "replay",
                    side_effect=replay_arm64,
                ) as replay,
                mock.patch.object(
                    codex_upgrade,
                    "_finalize_attempt_restoration",
                    side_effect=finalize_restoration,
                ) as finalize,
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                ) as probe,
                mock.patch.object(
                    codex_upgrade,
                    "_capture_arm64_environment_receipt",
                ) as arm64_probe,
                mock.patch.object(
                    codex_upgrade,
                    "_run_job_with_retry",
                ) as run_job,
            ):
                projected = (
                    codex_upgrade._materialize_metadata_only_environment_projection(
                        source,
                        target,
                        source_environment,
                        source_attempt_id="source-attempt-a",
                        candidate_id="candidate-a",
                    )
                )

            self.assertEqual(projected["evidence_root"], str(target))
            self.assertEqual(replay.call_count, 2)
            finalize.assert_called_once()
            probe.assert_not_called()
            arm64_probe.assert_not_called()
            run_job.assert_not_called()
            for snapshot_name in ("before", "after"):
                for name in sorted(
                    {
                        "probe-manifest.json",
                        *codex_upgrade.ENVIRONMENT_STATE_FILES.values(),
                    }
                ):
                    self.assertEqual(
                        (
                            target
                            / "environment"
                            / snapshot_name
                            / name
                        ).read_bytes(),
                        (
                            source
                            / "environment"
                            / snapshot_name
                            / name
                        ).read_bytes(),
                    )
                for name in ("facts.json", "receipt.json"):
                    self.assertEqual(
                        (
                            target
                            / "environment"
                            / f"arm64-{snapshot_name}"
                            / name
                        ).read_bytes(),
                        (
                            source
                            / "environment"
                            / f"arm64-{snapshot_name}"
                            / name
                        ).read_bytes(),
                    )
            self.assertTrue(
                (target / projected["restoration_report"]["path"]).is_file()
            )
            for role in (
                "before_probe",
                "after_probe",
                "restoration_report",
                "arm64_before_receipt",
                "arm64_after_receipt",
            ):
                self.assertGreater(projected[role]["bytes"], 0)
                self.assertRegex(projected[role]["sha256"], r"^[0-9a-f]{64}$")

    def test_first_candidate_seal_creates_fresh_client_after_checkpoint(self) -> None:
        """新 metadata-only attempt 首次 seal 必须真实采集 client-after。"""

        with tempfile.TemporaryDirectory() as directory:
            evidence_root = Path(directory).resolve() / "evidence"
            evidence_root.mkdir(mode=0o700)
            observed_at = "2026-09-17T00:10:00Z"

            def probe_environment(
                _manifest: dict[str, object],
                output_dir: Path,
                phase: str,
            ) -> dict[str, object]:
                self.assertEqual(output_dir, evidence_root / "environment" / "client-after")
                self.assertEqual(phase, "after")
                self._write_json(
                    output_dir / "probe-manifest.json",
                    {"phase": "after", "observed_at_utc": observed_at},
                )
                return {"phase": phase}

            def finalize_restoration(
                root: Path,
                *,
                phase: str,
                candidate_id: str | None,
                before_directory: str,
                after_directory: str,
                output_name: str,
            ) -> tuple[Path, dict[str, object]]:
                self.assertEqual(root, evidence_root)
                self.assertEqual(phase, "candidate")
                self.assertEqual(candidate_id, "candidate-a")
                self.assertEqual(before_directory, "after")
                self.assertEqual(after_directory, "client-after")
                path = root / "receipts" / output_name
                self._write_json(path, {"passed": True})
                return path, {"passed": True}

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                    side_effect=probe_environment,
                ) as probe,
                mock.patch.object(
                    codex_upgrade,
                    "_finalize_attempt_restoration",
                    side_effect=finalize_restoration,
                ) as finalize,
            ):
                path, receipt, checkpoint_at, created = (
                    codex_upgrade._candidate_post_client_restoration(
                        {},
                        evidence_root,
                        "candidate-a",
                    )
                )

            self.assertTrue(created)
            self.assertEqual(receipt, {"passed": True})
            self.assertEqual(checkpoint_at, observed_at)
            self.assertEqual(
                path,
                evidence_root / "receipts" / "client-restoration-report.json",
            )
            probe.assert_called_once()
            finalize.assert_called_once()

    def test_evaluation_transition_is_limited_to_one_attempt_and_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "r26"
            attempt_root.mkdir(parents=True)
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
            raw_root = Path(directory) / "raw"
            raw_root.mkdir()

            def identity(sha256: str) -> dict[str, object]:
                entries = [{"path": "codex_upgrade.py", "sha256": sha256}]
                return {
                    "git_commit": None,
                    "entry_count": 1,
                    "files_sha256": codex_upgrade._fingerprint(
                        {"entries": entries}
                    ),
                    "entries": entries,
                    **codex_upgrade._tool_identity_sides(entries),
                }

            expected = identity("a" * 64)
            current = identity("b" * 64)
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "tool_identity": expected,
            }
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "c1",
                "candidate_purpose": "validation_only",
                "attempt_id": "r26",
                "attempt_digest": "c" * 64,
                "status": "awaiting_receipts",
                "evidence_roots": [str(raw_root)],
            }
            recovery_controls = {
                "schema_version": codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA,
                "marker": "recovery-a",
            }
            arguments = argparse.Namespace(
                campaign_dir=campaign,
                phase="candidate",
                candidate_id="c1",
                attempt_id="r26",
                approve_transition_sha256=None,
            )

            common_patches = (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=current,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    return_value=recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4]:
                preview = codex_upgrade.create_phase_evaluation_transition(arguments)
            self.assertEqual(preview["raw_evidence_scanned_bytes"], 0)

            arguments.approve_transition_sha256 = preview["review_sha256"]
            approval_patches = (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=current,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    return_value=recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            )
            with approval_patches[0], approval_patches[1], approval_patches[2], approval_patches[3], approval_patches[4]:
                approved = codex_upgrade.create_phase_evaluation_transition(arguments)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(approved["raw_evidence_scanned_bytes"], 0)
            self.assertEqual(approved["transition_index"], 1)

            wrong_attempt = {**attempt, "candidate_id": "c2"}
            with mock.patch.object(
                codex_upgrade,
                "_validate_phase_recovery_controls",
                return_value=recovery_controls,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "身份、范围或工具摘要漂移",
                ):
                    codex_upgrade._validate_phase_evaluation_transition(
                        campaign,
                        manifest,
                        attempt_root=attempt_root,
                        attempt=wrong_attempt,
                        current_tool=current,
                    )

            original_receipt = (
                attempt_root / "evaluation-transition.json"
            ).read_bytes()
            replacement = identity("d" * 64)
            replacement_controls = {
                "schema_version": codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA,
                "marker": "recovery-b",
            }
            arguments.approve_transition_sha256 = None
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=replacement,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    side_effect=codex_upgrade.ConfigurationError(
                        "UpgradeTimingLedger 当前状态为 stopped，必须停线"
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=replacement_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            ):
                replacement_preview = (
                    codex_upgrade.create_phase_evaluation_transition(arguments)
                )

            self.assertEqual(replacement_preview["transition_index"], 2)
            self.assertTrue(
                replacement_preview["preview"].endswith(
                    "evaluation-transition-02-preview.json"
                )
            )
            self.assertEqual(
                json.loads(
                    Path(replacement_preview["preview"]).read_text(encoding="utf-8")
                )["recovery_controls"],
                recovery_controls,
            )

            arguments.approve_transition_sha256 = replacement_preview[
                "review_sha256"
            ]
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=replacement,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    side_effect=codex_upgrade.ConfigurationError(
                        "UpgradeTimingLedger 当前状态为 stopped，必须停线"
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=replacement_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            ):
                replacement_approved = (
                    codex_upgrade.create_phase_evaluation_transition(arguments)
                )
            self.assertEqual(replacement_approved["transition_index"], 2)
            self.assertEqual(
                (attempt_root / "evaluation-transition.json").read_bytes(),
                original_receipt,
            )
            self.assertTrue(
                (attempt_root / "evaluation-transition-02.json").is_file()
            )

            arguments.approve_transition_sha256 = None
            third = identity("e" * 64)
            third_controls = {
                "schema_version": codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA,
                "marker": "recovery-c",
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=third,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    side_effect=codex_upgrade.ConfigurationError(
                        "UpgradeTimingLedger 当前状态为 stopped，必须停线"
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=third_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            ):
                third_preview = codex_upgrade.create_phase_evaluation_transition(
                    arguments
                )
            self.assertEqual(third_preview["transition_index"], 3)
            self.assertTrue(
                third_preview["preview"].endswith(
                    "evaluation-transition-03-preview.json"
                )
            )

            arguments.approve_transition_sha256 = third_preview["review_sha256"]
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=third,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    side_effect=codex_upgrade.ConfigurationError(
                        "UpgradeTimingLedger 当前状态为 stopped，必须停线"
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=third_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
            ):
                third_approved = codex_upgrade.create_phase_evaluation_transition(
                    arguments
                )
            self.assertEqual(third_approved["transition_index"], 3)
            self.assertTrue(
                (attempt_root / "evaluation-transition-03.json").is_file()
            )

            arguments.approve_transition_sha256 = None
            fourth = identity("f" * 64)
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=fourth,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    return_value=third_controls,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "缺少隔离全链预检收据",
                ),
            ):
                codex_upgrade.create_phase_evaluation_transition(arguments)

            terminal_binding = {
                "path": str(Path(directory) / "terminal-preflight.json"),
                "sha256": "0" * 64,
            }
            arguments.terminal_transition_preflight_receipt = Path(
                terminal_binding["path"]
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=fourth,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_terminal_transition_preflight_receipt",
                    return_value=terminal_binding,
                ),
            ):
                terminal_preview = codex_upgrade.create_phase_evaluation_transition(
                    arguments
                )
            self.assertEqual(terminal_preview["transition_index"], 4)
            self.assertTrue(
                terminal_preview["preview"].endswith(
                    "evaluation-transition-04-preview.json"
                )
            )
            terminal_preview_payload = json.loads(
                Path(terminal_preview["preview"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                terminal_preview_payload["terminal_preflight"],
                terminal_binding,
            )

            arguments.approve_transition_sha256 = terminal_preview["review_sha256"]
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=fourth,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_terminal_transition_preflight_receipt",
                    return_value=terminal_binding,
                ),
            ):
                terminal_approved = codex_upgrade.create_phase_evaluation_transition(
                    arguments
                )
            self.assertEqual(terminal_approved["transition_index"], 4)
            self.assertTrue(
                (attempt_root / "evaluation-transition-04.json").is_file()
            )

            arguments.approve_transition_sha256 = None
            arguments.terminal_transition_preflight_receipt = None
            fifth = identity("1" * 64)
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=fifth,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "已使用终端第 4 槽",
                ),
            ):
                codex_upgrade.create_phase_evaluation_transition(arguments)

    def test_historical_replacement_transition_rejects_rehashed_control_tamper(
        self,
    ) -> None:
        """历史槽位只读重放；即使重算摘要也不能改变冻结控制关系。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "failed-a"
            attempt_root.mkdir(parents=True)
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")

            def identity(value: str) -> dict[str, object]:
                entries = [{"path": "codex_upgrade.py", "sha256": value}]
                return {
                    "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                    "entries": entries,
                    **codex_upgrade._tool_identity_sides(entries),
                }

            expected = identity("a" * 64)
            current = identity("b" * 64)
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "tool_identity": expected,
            }
            checkpoint = {
                "path": "checkpoints",
                "record_count": 1,
                "last_sequence": 1,
                "last_sha256": "c" * 64,
            }
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "c1",
                "attempt_id": "failed-a",
                "attempt_digest": "d" * 64,
                "run_nonce": "run-a",
                "status": "failed",
                "evidence_roots": [],
                "job_checkpoint": checkpoint,
                "results": [{"id": "job-a", "status": "failed"}],
            }
            scope = {
                "schema_version": "codex-upgrade-failed-attempt-scope/v1",
                "source_attempt_id": "failed-a",
                "source_attempt_digest": "d" * 64,
                "run_nonce": "run-a",
                "planned_job_ids": ["job-a"],
                "completed_job_ids": [],
                "failed_job_ids": ["job-a"],
                "pending_job_ids": [],
                "execute_job_ids": ["job-a"],
                "checkpoint": checkpoint,
                "environment_boundary_sha256": "e" * 64,
            }

            def binding(root_field: str, root: str) -> dict[str, object]:
                return {
                    root_field: root,
                    "receipt": {
                        "path": "receipt.json",
                        "sha256": "f" * 64,
                        "bytes": 1,
                    },
                }

            predecessor_timing = {
                **binding("ledger_dir", "/ledger-old"),
                "upgrade_id": "upgrade-old",
                "evidence_decision": "reuse",
            }
            recovery_timing = {
                **binding("ledger_dir", "/ledger-new"),
                "upgrade_id": "upgrade-new",
                "evidence_decision": "reuse",
            }
            controls = {
                "schema_version": (
                    codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA
                ),
                "predecessor": {
                    "upgrade_timing": predecessor_timing,
                    "arm64_environment": binding("evidence_root", "/p0-old"),
                },
                "stop_checkpoint": {
                    **binding("ledger_dir", "/ledger-old"),
                    "upgrade_id": "upgrade-old",
                    "evidence_decision": "reuse",
                    "active_phase": "VC-4",
                    "head_sequence": 1,
                    "head_sha256": "1" * 64,
                    "total_elapsed_seconds": 1,
                    "total_live_request_count": 0,
                },
                "recovery": {
                    "upgrade_timing": recovery_timing,
                    "arm64_environment": binding("evidence_root", "/p0-new"),
                    "job_rehearsal": binding("evidence_root", "/rehearsal"),
                },
                "current_tool_files_sha256": str(current["files_sha256"]),
            }
            preview_path = codex_upgrade._evaluation_transition_preview_path(
                attempt_root,
                2,
            )
            receipt_path = codex_upgrade._evaluation_transition_path(attempt_root, 2)

            def write_pair(preview: dict[str, object]) -> None:
                preview_path.write_text(
                    json.dumps(preview, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                projection = {
                    key: value
                    for key, value in preview.items()
                    if key
                    not in {
                        "schema_version",
                        "campaign_mode",
                        "campaign_purpose",
                        "status",
                    }
                }
                core = {
                    "schema_version": codex_upgrade.TOOL_EVALUATION_TRANSITION_SCHEMA,
                    "approved_at_utc": "2026-09-04T00:00:00Z",
                    **projection,
                    "preview": {
                        "path": preview_path.relative_to(campaign).as_posix(),
                        "sha256": codex_upgrade.file_sha256(preview_path),
                    },
                    "status": "approved",
                }
                receipt = {**core, "transition_digest": codex_upgrade._fingerprint(core)}
                receipt_path.write_text(
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            preview = codex_upgrade._build_phase_evaluation_transition_preview(
                campaign,
                manifest,
                phase="candidate",
                candidate_id="c1",
                attempt_root=attempt_root,
                attempt=attempt,
                current_tool=current,
                recovery_controls=controls,
                transition_index=2,
                frozen_recovery_scope=scope,
                reuse_frozen_recovery_scope=True,
            )
            write_pair(preview)
            replayed_controls, replayed_scope = (
                codex_upgrade._historical_phase_evaluation_transition_frozen_state(
                    campaign,
                    manifest,
                    attempt_root=attempt_root,
                    attempt=attempt,
                    transition_index=2,
                )
            )
            self.assertEqual(replayed_controls, controls)
            self.assertEqual(replayed_scope, scope)

            tampered = json.loads(json.dumps(preview))
            tampered["recovery_controls"]["stop_checkpoint"]["ledger_dir"] = (
                "/ledger-tampered"
            )
            tampered_core = {
                key: value
                for key, value in tampered.items()
                if key not in {"status", "review_sha256"}
            }
            tampered["review_sha256"] = codex_upgrade._fingerprint(tampered_core)
            write_pair(tampered)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "冻结 Ledger 关系非法",
            ):
                codex_upgrade._historical_phase_evaluation_transition_frozen_state(
                    campaign,
                    manifest,
                    attempt_root=attempt_root,
                    attempt=attempt,
                    transition_index=2,
                )

    def test_stage_evaluation_transition_extracts_file_binding_from_impact(self) -> None:
        binding = {
            "path": "official/attempts/r1/evaluation-transition.json",
            "sha256": "1" * 64,
        }
        impact = {
            "kind": "phase_evaluation_transition",
            "changed_components": ["orchestrator"],
            "evaluation_transition": binding,
        }

        self.assertEqual(
            codex_upgrade._stage_evaluation_transition_binding(impact),
            binding,
        )
        self.assertEqual(
            codex_upgrade._stage_evaluation_transition_binding(binding),
            binding,
        )
        self.assertIsNone(
            codex_upgrade._stage_evaluation_transition_binding(
                {"kind": "component_drift", "changed_components": ["evaluator"]}
            )
        )

    def test_failed_candidate_transition_only_allows_registered_production_closure(
        self,
    ) -> None:
        """产出侧变化必须逐文件完全落入两个失败 MITM Job。"""

        attempt = {"status": "failed"}
        scope = {
            "execute_job_ids": ["candidate-compact-mitm", "candidate-core-mitm"],
            "completed_job_ids": ["candidate-frozen-aux"],
        }
        allowed = codex_upgrade._phase_evaluation_failed_job_production_changes(
            {
                "production": [
                    "codex_upgrade.py",
                    "mitm_scenario_checkpoint.py",
                    "run_sub2api_openai_mitm_matrix.sh",
                ],
                "evaluation": [],
            },
            phase="candidate",
            attempt=attempt,
            recovery_scope=scope,
        )
        self.assertEqual(
            allowed,
            {
                "mitm_scenario_checkpoint.py",
                "run_sub2api_openai_mitm_matrix.sh",
            },
        )

        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "未登记的产出侧变化",
        ):
            codex_upgrade._phase_evaluation_failed_job_production_changes(
                {"production": ["unknown-producer.py"], "evaluation": []},
                phase="candidate",
                attempt=attempt,
                recovery_scope=scope,
            )

        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "超出冻结失败",
        ):
            codex_upgrade._phase_evaluation_failed_job_production_changes(
                {
                    "production": ["run_sub2api_direct_matrix.sh"],
                    "evaluation": [],
                },
                phase="candidate",
                attempt=attempt,
                recovery_scope=scope,
            )

    def test_failed_candidate_transition_preview_and_load_share_production_scope(
        self,
    ) -> None:
        """transition 创建与消费使用同一份失败闭集规则。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "r1"
            raw_root = Path(directory) / "raw"
            attempt_root.mkdir(parents=True)
            raw_root.mkdir()
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")

            def identity(digest: str) -> dict[str, object]:
                entries = [
                    {"path": "codex_upgrade.py", "sha256": digest},
                    {"path": "mitm_scenario_checkpoint.py", "sha256": digest},
                    {
                        "path": "run_sub2api_openai_mitm_matrix.sh",
                        "sha256": digest,
                    },
                ]
                return {
                    "git_commit": None,
                    "entry_count": len(entries),
                    "files_sha256": codex_upgrade._fingerprint(
                        {"entries": entries}
                    ),
                    "entries": entries,
                    **codex_upgrade._tool_identity_sides(entries),
                }

            expected = identity("1" * 64)
            current = identity("2" * 64)
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "tool_identity": expected,
            }
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "c1",
                "attempt_id": "r1",
                "attempt_digest": "3" * 64,
                "status": "failed",
                "evidence_roots": [str(raw_root)],
            }
            scope = {
                "source_attempt_id": "r1",
                "source_attempt_digest": "3" * 64,
                "execute_job_ids": [
                    "candidate-compact-mitm",
                    "candidate-core-mitm",
                ],
                "completed_job_ids": ["candidate-frozen-aux"],
            }
            with mock.patch.object(
                codex_upgrade,
                "_phase_evaluation_recovery_scope",
                return_value=scope,
            ):
                preview = codex_upgrade._build_phase_evaluation_transition_preview(
                    campaign,
                    manifest,
                    phase="candidate",
                    candidate_id="c1",
                    attempt_root=attempt_root,
                    attempt=attempt,
                    current_tool=current,
                    recovery_controls={"marker": "controls"},
                )
            classifications = {
                item["path"]: item["classification"]
                for item in preview["changed_files"]
            }
            self.assertEqual(
                classifications["run_sub2api_openai_mitm_matrix.sh"],
                "failed_job_production",
            )
            self.assertEqual(preview["recovery_scope"], scope)

            transition_path = attempt_root / "evaluation-transition.json"
            transition_path.write_text("{}\n", encoding="utf-8")
            receipt = {
                "allowed_operations": ["capture-run"],
                "recovery_scope": scope,
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_source",
                    return_value=(attempt_root, attempt, None, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_index",
                    return_value=1,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_evaluation_transition",
                    return_value=receipt,
                ),
            ):
                binding = codex_upgrade._load_phase_evaluation_transition(
                    campaign,
                    manifest,
                    attempt_root=attempt_root,
                    attempt=attempt,
                    operation="capture-run",
                    current_tool=current,
                    drift=codex_upgrade._tool_identity_drift(current, expected),
                )
            self.assertEqual(binding["sha256"], codex_upgrade.file_sha256(transition_path))

    def test_authorized_production_paths_validate_transition_before_reuse(
        self,
    ) -> None:
        """历史结果排除高风险文件前必须先重放已批准 transition。"""

        expected_entries = [
            {"path": "run_sub2api_openai_mitm_matrix.sh", "sha256": "1" * 64}
        ]
        current_entries = [
            {"path": "run_sub2api_openai_mitm_matrix.sh", "sha256": "2" * 64}
        ]

        def identity(entries: list[dict[str, str]]) -> dict[str, object]:
            return {
                "entries": entries,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                **codex_upgrade._tool_identity_sides(entries),
            }

        expected = identity(expected_entries)
        current = identity(current_entries)
        scope = {
            "execute_job_ids": ["candidate-compact-mitm", "candidate-core-mitm"],
            "completed_job_ids": ["candidate-frozen-aux"],
        }
        with mock.patch.object(
            codex_upgrade,
            "_load_phase_evaluation_transition",
            return_value={"path": "transition.json", "sha256": "3" * 64},
        ) as transition:
            paths = codex_upgrade._authorize_phase_recovery_production_paths(
                Path("/campaign"),
                {"tool_identity": expected},
                phase="candidate",
                candidate_id="c1",
                attempt_root=Path("/campaign/attempt"),
                attempt={"status": "failed", "phase": "candidate"},
                current_tool=current,
                recovery_scope=scope,
            )
        self.assertEqual(paths, {"run_sub2api_openai_mitm_matrix.sh"})
        transition.assert_called_once()
        self.assertEqual(transition.call_args.kwargs["operation"], "capture-run")

    def test_phase_recovery_replaces_coarse_relay_impact_with_exact_jobs(
        self,
    ) -> None:
        """获批 MITM 文件只能失效两个 MITM Job，不能传播 relay 粗闭集。"""

        planned = {
            "candidate-compact-direct",
            "candidate-compact-mitm",
            "candidate-core-direct",
            "candidate-core-mitm",
            "candidate-frozen-aux",
        }
        scope = {
            "execute_job_ids": [
                "candidate-compact-mitm",
                "candidate-core-mitm",
            ],
            "completed_job_ids": [
                "candidate-compact-direct",
                "candidate-core-direct",
                "candidate-frozen-aux",
            ],
        }

        affected = codex_upgrade._phase_recovery_exact_affected_job_ids(
            {
                "mitm_scenario_checkpoint.py",
                "run_sub2api_openai_mitm_matrix.sh",
            },
            planned_job_ids=planned,
            recovery_scope=scope,
        )

        self.assertEqual(
            affected,
            {"candidate-compact-mitm", "candidate-core-mitm"},
        )
        self.assertNotIn("candidate-compact-direct", affected)
        self.assertNotIn("candidate-core-direct", affected)

    def test_phase_recovery_exact_mapping_rejects_unknown_or_reused_job(
        self,
    ) -> None:
        """未知产出文件或触及 reused Job 时仍必须失败关闭。"""

        scope = {
            "execute_job_ids": ["candidate-core-mitm"],
            "completed_job_ids": ["candidate-compact-mitm"],
        }
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "没有 Job 映射",
        ):
            codex_upgrade._phase_recovery_exact_affected_job_ids(
                {"unknown-producer.py"},
                planned_job_ids={
                    "candidate-compact-mitm",
                    "candidate-core-mitm",
                },
                recovery_scope=scope,
            )
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "超出冻结 recovery_scope",
        ):
            codex_upgrade._phase_recovery_exact_affected_job_ids(
                {"run_sub2api_openai_mitm_matrix.sh"},
                planned_job_ids={
                    "candidate-compact-mitm",
                    "candidate-core-mitm",
                },
                recovery_scope=scope,
            )

    def test_legacy_vc0_recovery_requires_unique_unsealed_failed_candidate(
        self,
    ) -> None:
        """VC-0 例外只承接唯一且未 seal 的失败 Candidate attempt。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "r1"
            attempt_root.mkdir(parents=True)
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")
            manifest = {"campaign_id": "campaign-a"}
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "c1",
                "attempt_id": "r1",
                "attempt_digest": "4" * 64,
                "status": "failed",
            }
            scope = {
                "source_attempt_id": "r1",
                "source_attempt_digest": "4" * 64,
                "execute_job_ids": ["candidate-core-mitm"],
            }
            with mock.patch.object(
                codex_upgrade,
                "_phase_evaluation_recovery_scope",
                return_value=scope,
            ):
                self.assertEqual(
                    codex_upgrade._legacy_vc0_phase_recovery_scope(
                        campaign,
                        manifest,
                        phase="candidate",
                        candidate_id="c1",
                        attempt_root=attempt_root,
                        attempt=attempt,
                    ),
                    scope,
                )

                sealed_path = campaign / "candidates" / "c1" / "result.json"
                sealed_path.write_text("{}\n", encoding="utf-8")
                self.assertIsNone(
                    codex_upgrade._legacy_vc0_phase_recovery_scope(
                        campaign,
                        manifest,
                        phase="candidate",
                        candidate_id="c1",
                        attempt_root=attempt_root,
                        attempt=attempt,
                    )
                )

    def test_final_execution_epoch_recovery_uses_epoch2_controls(self) -> None:
        """最终 epoch 后只允许固定两项失败闭集使用 epoch2 控制。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "r1"
            attempt_root.mkdir(parents=True)
            epoch_path = campaign / "control-epochs" / "control-epoch-02.json"
            epoch_path.parent.mkdir()
            self._write_json(epoch_path, {"epoch": 2})
            self._write_json(campaign / "campaign.json", {})
            planned = [
                "candidate-compact-direct",
                "candidate-compact-mitm",
                "candidate-core-direct",
                "candidate-core-mitm",
                "candidate-frozen-aux",
                "candidate-frozen-core",
                "candidate-h1-wire",
                "candidate-images-wire",
                "candidate-ws-handshake-repeat",
            ]
            execute = ["candidate-compact-mitm", "candidate-core-mitm"]
            reused = sorted(set(planned) - set(execute))
            manifest = {"campaign_id": "campaign-a"}
            attempt = {
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "phase": "candidate",
                "candidate_id": "c1",
                "attempt_id": "r1",
                "attempt_digest": "4" * 64,
                "status": "failed",
                "started_at_utc": "2026-09-04T01:00:01Z",
                "incremental_plan": {
                    "planned_job_ids": planned,
                    "executed_job_ids": execute,
                    "failed_job_ids": execute,
                    "pending_job_ids": [],
                    "reused_job_ids": reused,
                },
            }
            epoch = {
                "epoch_index": 2,
                "status": "active",
                "created_at_utc": "2026-09-04T01:00:00Z",
                "execution_authorization": {
                    "schema_version": (
                        codex_upgrade.CONTROL_EPOCH_FINAL_EXECUTION_SCHEMA
                    ),
                    "mode": codex_upgrade.CONTROL_EPOCH_FINAL_EXECUTION_MODE,
                    "scope": {
                        "planned_job_ids": planned,
                        "execute_job_ids": execute,
                        "reused_job_ids": reused,
                    },
                },
                "successor_controls": {
                    "upgrade_timing": {"ledger_dir": "/control/epoch2"},
                    "arm64_environment": {"evidence_root": "/control/p0"},
                },
            }
            with mock.patch.object(
                codex_upgrade,
                "_load_control_epoch_receipt",
                return_value=epoch,
            ):
                context = (
                    codex_upgrade._final_execution_epoch_phase_recovery_context(
                        campaign,
                        manifest,
                        phase="candidate",
                        candidate_id="c1",
                        attempt_root=attempt_root,
                        attempt=attempt,
                    )
                )

            self.assertEqual(
                context["predecessor_timing"]["ledger_dir"],
                "/control/epoch2",
            )
            self.assertEqual(context["marker"]["execute_job_ids"], execute)
            self.assertEqual(len(context["marker"]["reused_job_ids"]), 7)

            attempt["incremental_plan"]["failed_job_ids"] = [
                "candidate-core-mitm"
            ]
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_receipt",
                    return_value=epoch,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "固定 2 执行／7 复用闭集",
                ),
            ):
                codex_upgrade._final_execution_epoch_phase_recovery_context(
                    campaign,
                    manifest,
                    phase="candidate",
                    candidate_id="c1",
                    attempt_root=attempt_root,
                    attempt=attempt,
                )

    def test_pre_job_failure_transition_executes_all_frozen_jobs(self) -> None:
        """首个 Job 前失败只在严格空边界下恢复全部冻结 Job。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            attempt_root = campaign / "official" / "attempts" / "attempt-a"
            checkpoint_root = attempt_root / "checkpoints"
            logs_root = attempt_root / "logs"
            evidence_root = attempt_root / "evidence"
            environment_root = evidence_root / "environment"
            after_root = environment_root / "after"
            for path in (
                campaign,
                attempt_root,
                checkpoint_root,
                logs_root,
                after_root,
                environment_root / "arm64-before",
                environment_root / "arm64-after",
                evidence_root / "receipts",
            ):
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o700)
            (campaign / "campaign.json").write_text("{}\n", encoding="utf-8")

            configuration = {
                "service_container": "sub2apiplus",
                "keeper_container": "sub2apiplus-keeper",
                "postgres_container": "sub2apiplus-postgres",
                "redis_container": "sub2apiplus-redis",
                "capture_container": "capture-cli",
                "codex_account_id": 22,
                "api_key_id": 4,
            }
            manifest = {
                "campaign_id": "campaign-a",
                "configuration": configuration,
            }
            planned = {"job-a": "a" * 64, "job-b": "b" * 64}

            snapshots: list[dict[str, object]] = []
            for kind, name in codex_upgrade.ENVIRONMENT_STATE_FILES.items():
                path = after_root / name
                payload = (json.dumps({"kind": kind}, sort_keys=True) + "\n").encode()
                path.write_bytes(payload)
                snapshots.append(
                    {
                        "bytes": len(payload),
                        "comparison": {"mode": "byte_equal"},
                        "kind": kind,
                        "path": name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            probe = {
                "observed_at_utc": "2026-09-02T09:23:28Z",
                "phase": "after",
                "schema_version": (
                    codex_upgrade.codex_upgrade_environment_probe.PROBE_MANIFEST_SCHEMA
                ),
                "selected_account_id": configuration["codex_account_id"],
                "selected_key_id": configuration["api_key_id"],
                "snapshots": snapshots,
                "targets": {
                    "service": configuration["service_container"],
                    "keeper": configuration["keeper_container"],
                    "postgres": configuration["postgres_container"],
                    "redis": configuration["redis_container"],
                    "capture": configuration["capture_container"],
                },
            }
            probe_path = after_root / "probe-manifest.json"
            probe_path.write_text(
                json.dumps(probe, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            heartbeat_path = attempt_root / "watchdog-heartbeat.json"
            heartbeat = {
                "schema_version": codex_upgrade.WATCHDOG_HEARTBEAT_SCHEMA,
                "phase": "official",
                "operation": "attempt:reserved",
                "elapsed_seconds": 6.0,
                "remaining_seconds": 3594.0,
                "last_completed_job_id": None,
                "updated_at_utc": "2026-09-02T09:23:21Z",
            }
            heartbeat_path.write_text(
                json.dumps(heartbeat, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            relative_attempt = attempt_root.relative_to(campaign)
            unsigned_plan = {
                "schema_version": codex_upgrade.incremental_recovery.SCHEMA_VERSION,
                "planned_job_ids": sorted(planned),
                "changed_components": [],
                "affected_job_ids": [],
                "reused_job_ids": [],
                "executed_job_ids": [],
                "failed_job_ids": [],
                "pending_job_ids": sorted(planned),
            }
            incremental_plan = {
                **unsigned_plan,
                "plan_sha256": codex_upgrade.incremental_recovery.digest(
                    unsigned_plan
                ),
            }
            attempt = {
                "status": "failed",
                "phase": "official",
                "candidate_id": None,
                "campaign_id": manifest["campaign_id"],
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign / "campaign.json"
                ),
                "attempt_id": attempt_root.name,
                "run_nonce": "c" * 64,
                "attempt_digest": "d" * 64,
                "started_at_utc": "2026-09-02T09:23:20Z",
                "completed_at_utc": "2026-09-02T09:23:40Z",
                "results": [],
                "continuity": None,
                "incremental_plan": incremental_plan,
                "execution_error": {
                    "type": "ConfigurationError",
                    "message": "heartbeat 标签非法",
                },
                "restoration_error": {
                    "type": "ReceiptFinalizerError",
                    "message": "before 探针不存在",
                },
                "evidence_roots": [
                    str(evidence_root.resolve()),
                    str(logs_root.resolve()),
                ],
                "environment": {
                    "evidence_root": str(evidence_root.resolve()),
                    "before_probe": None,
                    "after_probe": {
                        "path": "environment/after/probe-manifest.json",
                        "sha256": codex_upgrade.file_sha256(probe_path),
                        "bytes": probe_path.stat().st_size,
                    },
                    "restoration_report": None,
                    "arm64_before_receipt": None,
                    "arm64_after_receipt": None,
                },
                "watchdog": {
                    "schema_version": codex_upgrade.WATCHDOG_HEARTBEAT_SCHEMA,
                    "budget_seconds": 3600.0,
                    "heartbeat_seconds": 5,
                    "elapsed_seconds": 20.0,
                    "remaining_seconds": 3580.0,
                    "heartbeat": {
                        "path": str(
                            (relative_attempt / heartbeat_path.name).as_posix()
                        ),
                        "sha256": codex_upgrade.file_sha256(heartbeat_path),
                        "bytes": heartbeat_path.stat().st_size,
                    },
                    "timeout_checkpoint": None,
                    "last_completed_job_id": None,
                },
                "job_checkpoint": {
                    "schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                    "campaign_id": manifest["campaign_id"],
                    "phase": "official",
                    "attempt_id": attempt_root.name,
                    "run_nonce": "c" * 64,
                    "path": str((relative_attempt / "checkpoints").as_posix()),
                    "record_count": 0,
                    "last_sequence": None,
                    "last_sha256": None,
                },
            }
            reservation = {
                "planned_jobs": [
                    {"id": job_id, "execution_sha256": digest}
                    for job_id, digest in planned.items()
                ]
            }
            with mock.patch.object(
                codex_upgrade,
                "_load_capture_reservation",
                return_value=reservation,
            ) as load_reservation:
                scope = codex_upgrade._phase_evaluation_recovery_scope(
                    campaign,
                    manifest,
                    phase="official",
                    candidate_id=None,
                    attempt_root=attempt_root,
                    attempt=attempt,
                )
            load_reservation.assert_called_once_with(
                campaign,
                attempt_root,
                phase="official",
                candidate_id=None,
                _manifest=manifest,
            )
            self.assertEqual(scope["source_mode"], "pre_job_failure")
            self.assertEqual(scope["completed_job_ids"], [])
            self.assertEqual(scope["pending_job_ids"], sorted(planned))
            self.assertEqual(scope["execute_job_ids"], sorted(planned))
            self.assertEqual(scope["checkpoint"]["record_count"], 0)

            heartbeat["operation"] = "job:job-a:start"
            heartbeat_path.write_text(
                json.dumps(heartbeat, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            attempt["watchdog"]["heartbeat"] = {
                "path": str((relative_attempt / heartbeat_path.name).as_posix()),
                "sha256": codex_upgrade.file_sha256(heartbeat_path),
                "bytes": heartbeat_path.stat().st_size,
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=reservation,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "heartbeat 已进入 Job",
                ),
            ):
                codex_upgrade._phase_evaluation_recovery_scope(
                    campaign,
                    manifest,
                    phase="official",
                    candidate_id=None,
                    attempt_root=attempt_root,
                    attempt=attempt,
                )

            ordinary_failure = {
                **attempt,
                "results": [
                    {
                        "id": "job-a",
                        "execution_sha256": planned["job-a"],
                        "status": "failed",
                    }
                ],
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=reservation,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "没有已完成 Job",
                ),
            ):
                codex_upgrade._phase_evaluation_recovery_scope(
                    campaign,
                    manifest,
                    phase="official",
                    candidate_id=None,
                    attempt_root=attempt_root,
                    attempt=ordinary_failure,
                )

    def test_failed_transition_keeps_source_capture_only_and_unlocks_closed_successor(self) -> None:
        """失败源只可补跑；后继闭集完成后才可进入离线阶段。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "campaign"
            source_root = campaign / "official" / "attempts" / "source"
            successor_root = campaign / "official" / "attempts" / "successor"
            for path in (campaign, source_root, successor_root):
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(0o700)
            source_attempt_path = source_root / "attempt.json"
            successor_attempt_path = successor_root / "attempt.json"
            source_attempt_path.write_text("source\n", encoding="utf-8")
            successor_attempt_path.write_text("successor\n", encoding="utf-8")
            campaign_id = "transition-recovery"
            identity = {"identity": "same"}
            planned = {
                "job-a": "a" * 64,
                "job-b": "b" * 64,
                "job-c": "c" * 64,
            }

            def result(job_id: str, status: str, disposition: str = "executed") -> dict[str, object]:
                return {
                    "id": job_id,
                    "phase": "official",
                    "required": True,
                    "execution_sha256": planned[job_id],
                    "status": status,
                    "disposition": disposition,
                    "incremental_result_key": "d" * 64,
                    "evidence_roots": [],
                }

            source_results = [result("job-a", "complete"), result("job-b", "failed")]
            scope = {
                "planned_job_ids": ["job-a", "job-b", "job-c"],
                "completed_job_ids": ["job-a"],
                "failed_job_ids": ["job-b"],
                "pending_job_ids": ["job-c"],
                "execute_job_ids": ["job-b", "job-c"],
            }
            expected_source_receipt = {
                "path": str(source_attempt_path.relative_to(campaign)),
                "sha256": codex_upgrade.file_sha256(source_attempt_path),
                "bytes": source_attempt_path.stat().st_size,
            }
            successor_results = [
                {
                    **source_results[0],
                    "disposition": "reused",
                    "carried_from_attempt": source_root.name,
                    "source_receipt": expected_source_receipt,
                },
                result("job-b", "complete"),
                result("job-c", "complete"),
            ]
            checkpoint_root = successor_root / "checkpoints"
            checkpoint_root.mkdir(mode=0o700)
            checkpoint_store = codex_upgrade.incremental_recovery.CheckpointStore(
                checkpoint_root
            )
            for item in successor_results:
                previous = checkpoint_store.records()
                checkpoint_store.append(
                    {
                        "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                        "campaign_id": campaign_id,
                        "phase": "official",
                        "attempt_id": successor_root.name,
                        "run_nonce": "e" * 64,
                        "item_id": item["id"],
                        "status": "complete",
                        "disposition": item["disposition"],
                        "result_sha256": codex_upgrade.incremental_recovery.digest(item),
                        "result_key": item["incremental_result_key"],
                        "result": item,
                        "previous_checkpoint_sha256": (
                            previous[-1]["checkpoint_sha256"] if previous else None
                        ),
                    }
                )
            checkpoint = {
                "path": "checkpoints",
                "record_count": len(checkpoint_store.records()),
            }
            source_attempt = {
                "campaign_id": campaign_id,
                "phase": "official",
                "candidate_id": None,
                "status": "failed",
                "attempt_id": source_root.name,
                "run_nonce": "f" * 64,
                "identity": identity,
                "results": source_results,
            }
            successor_attempt = {
                "campaign_id": campaign_id,
                "phase": "official",
                "candidate_id": None,
                "status": "awaiting_receipts",
                "attempt_id": successor_root.name,
                "run_nonce": "e" * 64,
                "identity": identity,
                "results": successor_results,
                "incremental_plan": {
                    "planned_job_ids": scope["planned_job_ids"],
                    "reused_job_ids": scope["completed_job_ids"],
                    "executed_job_ids": scope["execute_job_ids"],
                    "failed_job_ids": [],
                    "pending_job_ids": [],
                },
                "job_checkpoint": checkpoint,
            }
            reservations = {
                source_root: {"planned_jobs": [
                    {"id": job_id, "execution_sha256": digest}
                    for job_id, digest in planned.items()
                ]},
                successor_root: {"planned_jobs": [
                    {"id": job_id, "execution_sha256": digest}
                    for job_id, digest in planned.items()
                ]},
            }
            transition = {"recovery_scope": scope}
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    side_effect=lambda _campaign, attempt_root, **_kwargs: reservations[attempt_root],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_resolve_attempt_binding",
                    return_value=checkpoint_root,
                ),
            ):
                allowed = codex_upgrade._phase_evaluation_recovery_successor_operations(
                    campaign,
                    {"campaign_id": campaign_id},
                    phase="official",
                    candidate_id=None,
                    source_root=source_root,
                    source_attempt=source_attempt,
                    successor_root=successor_root,
                    successor_attempt=successor_attempt,
                    transition=transition,
                )
            self.assertEqual(
                allowed,
                ("capture-official-seal", "deep-verify"),
            )

            # resume 可以在新 attempt 内把复用结果的增量元数据重绑到当前
            # 有效工具身份；seal 必须接受该确定性重算，但不能放行证据字段变化。
            rebased_source = {
                **source_results[0],
                "incremental_result_key": "e" * 64,
            }
            successor_results[0]["incremental_result_key"] = "e" * 64
            rebase_mocks = (
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_jobs",
                    return_value=[mock.Mock(job_id="job-a")],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_rebase_reused_result",
                    return_value=rebased_source,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_checkpoint_records",
                    return_value=None,
                ),
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    side_effect=lambda _campaign, attempt_root, **_kwargs: reservations[attempt_root],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_resolve_attempt_binding",
                    return_value=checkpoint_root,
                ),
                rebase_mocks[0],
                rebase_mocks[1],
                rebase_mocks[2],
            ):
                allowed = codex_upgrade._phase_evaluation_recovery_successor_operations(
                    campaign,
                    {"campaign_id": campaign_id},
                    phase="official",
                    candidate_id=None,
                    source_root=source_root,
                    source_attempt=source_attempt,
                    successor_root=successor_root,
                    successor_attempt=successor_attempt,
                    transition=transition,
                    current_tool={"files_sha256": "f" * 64},
                )
            self.assertEqual(allowed, ("capture-official-seal", "deep-verify"))

            successor_results[0]["evidence_roots"] = ["mutated"]
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "未只读承接",
            ):
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_load_capture_reservation",
                        side_effect=lambda _campaign, attempt_root, **_kwargs: reservations[attempt_root],
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_resolve_attempt_binding",
                        return_value=checkpoint_root,
                    ),
                    rebase_mocks[0],
                    rebase_mocks[1],
                ):
                    codex_upgrade._phase_evaluation_recovery_successor_operations(
                        campaign,
                        {"campaign_id": campaign_id},
                        phase="official",
                        candidate_id=None,
                        source_root=source_root,
                        source_attempt=source_attempt,
                        successor_root=successor_root,
                        successor_attempt=successor_attempt,
                        transition=transition,
                        current_tool={"files_sha256": "f" * 64},
                    )
            successor_results[0]["evidence_roots"] = []
            successor_results[0]["incremental_result_key"] = "d" * 64

            source_attempt["status"] = "awaiting_receipts"
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "只有 failed source attempt",
            ):
                with mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    side_effect=lambda _campaign, attempt_root, **_kwargs: reservations[attempt_root],
                ):
                    codex_upgrade._phase_evaluation_recovery_successor_operations(
                        campaign,
                        {"campaign_id": campaign_id},
                        phase="official",
                        candidate_id=None,
                        source_root=source_root,
                        source_attempt=source_attempt,
                        successor_root=successor_root,
                        successor_attempt=successor_attempt,
                        transition=transition,
                    )

            source_attempt["status"] = "failed"
            successor_results[0]["disposition"] = "executed"
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "未只读承接",
            ):
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_load_capture_reservation",
                        side_effect=lambda _campaign, attempt_root, **_kwargs: reservations[attempt_root],
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_resolve_attempt_binding",
                        return_value=checkpoint_root,
                    ),
                ):
                    codex_upgrade._phase_evaluation_recovery_successor_operations(
                        campaign,
                        {"campaign_id": campaign_id},
                        phase="official",
                        candidate_id=None,
                        source_root=source_root,
                        source_attempt=source_attempt,
                        successor_root=successor_root,
                        successor_attempt=successor_attempt,
                        transition=transition,
                    )

    def test_failed_transition_authorization_does_not_widen_source_attempt(self) -> None:
        """同一 transition 在源 attempt 上仍严格限制为 capture-run。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            source_root = campaign / "official" / "attempts" / "source"
            successor_root = campaign / "official" / "attempts" / "successor"
            source_root.mkdir(parents=True)
            successor_root.mkdir(parents=True)
            transition_path = source_root / "evaluation-transition.json"
            transition_path.write_text("{}\n", encoding="utf-8")
            source_attempt = {
                "phase": "official",
                "candidate_id": None,
                "status": "failed",
            }
            current_tool = {"files_sha256": "b" * 64}
            drift = {"production": [], "evaluation": ["codex_upgrade.py"]}
            receipt = {"allowed_operations": ["capture-run"]}

            common = (
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_index",
                    return_value=1,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_evaluation_transition",
                    return_value=receipt,
                ),
            )
            with common[0], common[1], mock.patch.object(
                codex_upgrade,
                "_phase_evaluation_transition_source",
                return_value=(source_root, source_attempt, None, None),
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "未授权当前操作",
                ):
                    codex_upgrade._load_phase_evaluation_transition(
                        campaign,
                        {"campaign_id": "campaign"},
                        attempt_root=source_root,
                        attempt=source_attempt,
                        operation="capture-official-seal",
                        current_tool=current_tool,
                        drift=drift,
                    )

            successor_attempt = {
                "phase": "official",
                "candidate_id": None,
                "status": "awaiting_receipts",
                "evaluation_transition": {
                    "path": str(transition_path.relative_to(campaign)),
                    "sha256": codex_upgrade.file_sha256(transition_path),
                },
            }
            with (
                common[0],
                common[1],
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_source",
                    return_value=(
                        source_root,
                        source_attempt,
                        transition_path,
                        1,
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_successor_operations",
                    return_value=("capture-official-seal", "deep-verify"),
                ),
            ):
                binding = codex_upgrade._load_phase_evaluation_transition(
                    campaign,
                    {"campaign_id": "campaign"},
                    attempt_root=successor_root,
                    attempt=successor_attempt,
                    operation="capture-official-seal",
                    current_tool=current_tool,
                    drift=drift,
                )
            self.assertEqual(
                binding,
                {
                    "path": "official/attempts/source/evaluation-transition.json",
                    "sha256": codex_upgrade.file_sha256(transition_path),
                },
            )

            replacement_path = source_root / "evaluation-transition-02.json"
            replacement_path.write_text("replacement\n", encoding="utf-8")
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_index",
                    return_value=2,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_evaluation_transition",
                    return_value=receipt,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_transition_source",
                    return_value=(
                        source_root,
                        source_attempt,
                        transition_path,
                        1,
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_successor_operations",
                    return_value=("capture-official-seal", "deep-verify"),
                ),
            ):
                replacement_binding = codex_upgrade._load_phase_evaluation_transition(
                    campaign,
                    {"campaign_id": "campaign"},
                    attempt_root=successor_root,
                    attempt=successor_attempt,
                    operation="capture-official-seal",
                    current_tool=current_tool,
                    drift=drift,
                )
            self.assertEqual(
                replacement_binding,
                {
                    "path": "official/attempts/source/evaluation-transition-02.json",
                    "sha256": codex_upgrade.file_sha256(replacement_path),
                },
            )

    def test_campaign_loader_rejects_missing_invalid_and_tampered_mode(self) -> None:
        for mutation, update_digest, message in (
            ("missing", True, "campaign_mode"),
            ("invalid", True, "campaign_mode"),
            ("tampered", False, "摘要不一致"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                campaign_dir, _ = self._create_campaign(root)
                path = campaign_dir / "campaign.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                if mutation == "missing":
                    payload.pop("campaign_mode")
                elif mutation == "invalid":
                    payload["campaign_mode"] = "dry_run"
                else:
                    payload["campaign_mode"] = "preflight_only"
                self._write_json(path, payload)
                if update_digest:
                    (campaign_dir / "campaign.sha256").write_text(
                        codex_upgrade.file_sha256(path) + "\n",
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    message,
                ):
                    codex_upgrade.load_campaign_manifest(campaign_dir)

    def test_preflight_only_is_terminal_and_rejects_live_continuations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(root, campaign_mode="preflight_only")
            manifest = codex_upgrade.create_campaign(arguments)
            self.assertEqual(manifest["campaign_mode"], "preflight_only")
            status = codex_upgrade.campaign_status(arguments.campaign_dir)
            self.assertEqual(status["status"], "preflight_complete")
            self.assertNotEqual(status["next_command"], "capture-official")

            arguments.acknowledge_live_requests = True
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_verify_execution_tree",
                    side_effect=AssertionError("preflight 不得触碰执行环境"),
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "preflight_only",
                ),
            ):
                codex_upgrade._run_capture_attempt(arguments, "official")
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "preflight_only",
            ):
                codex_upgrade.save_stage_result(
                    arguments.campaign_dir,
                    "capture-official",
                    {"status": "failed"},
                )
            return_code, _, stderr = self._run_main(
                ["resume", "--campaign-dir", str(arguments.campaign_dir)]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("preflight_only", stderr)

    def test_candidate_purpose_must_match_campaign_before_runtime_checks(self) -> None:
        arguments = argparse.Namespace(
            runtime_image=f"candidate@sha256:{'1' * 64}",
            build_id="build-a",
            deployed_version="version-a",
            profile_id="profile-a",
            profile_digest="2" * 64,
            candidate_purpose="production_replacement",
        )
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "Campaign 冻结用途",
        ):
            codex_upgrade._candidate_identity_for_run(
                arguments,
                {"campaign_purpose": "validation_only"},
                {},
            )

    def test_incremental_execution_rebinds_verified_identity_to_build_receipt(
        self,
    ) -> None:
        """Docker 重验后的基础身份必须补回 VC-4 构建字段再参与等值校验。"""

        base_identity = {
            "git_commit": "a" * 40,
            "source_root": "/candidate/source",
            "source_tree_sha256": "b" * 64,
            "image_reference": f"candidate@sha256:{'c' * 64}",
            "image_digest": f"sha256:{'c' * 64}",
            "image_id": f"sha256:{'d' * 64}",
            "build_id": "build-a",
            "deployed_version": "0.154.0",
            "profile_id": "profile-a",
            "profile_digest": "e" * 64,
            "candidate_purpose": "production_replacement",
        }
        bound_identity = {
            **base_identity,
            "target_architecture": "linux/arm64",
            "binary": {"sha256": "f" * 64},
            "build_receipt": {
                "path": "candidates/candidate-a/build-receipt.json",
                "sha256": "1" * 64,
                "bytes": 1,
            },
        }
        receipt = {"schema_version": "candidate-build-receipt/v2"}
        binding = {
            "path": "candidates/candidate-a/build-receipt.json",
            "sha256": "1" * 64,
            "bytes": 1,
        }
        with (
            mock.patch.object(
                codex_upgrade,
                "_candidate_identity_for_run",
                return_value=base_identity,
            ) as verify,
            mock.patch.object(
                codex_upgrade,
                "_bind_candidate_identity_to_build_receipt",
                return_value=bound_identity,
            ) as bind,
        ):
            actual = codex_upgrade._candidate_identity_for_incremental_execution(
                argparse.Namespace(),
                {},
                {},
                candidate_build_receipt=receipt,
                candidate_build_binding=binding,
                deadline=None,
            )
        self.assertEqual(actual, bound_identity)
        self.assertTrue(verify.call_args.kwargs["verify_image"])
        bind.assert_called_once_with(
            mock.ANY,
            base_identity,
            receipt,
            binding,
        )

    def test_incremental_admin_credential_checks_only_executed_jobs(self) -> None:
        """复用 aux、只执行 core 时不得误要求管理凭据。"""

        class ReservationReached(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            campaign_dir.mkdir()
            identity = {
                "git_commit": "a" * 40,
                "source_root": "/candidate/source",
                "source_tree_sha256": "b" * 64,
                "image_reference": f"candidate@sha256:{'c' * 64}",
                "image_digest": f"sha256:{'c' * 64}",
                "image_id": f"sha256:{'d' * 64}",
                "build_id": "build-a",
                "deployed_version": "0.154.0",
                "profile_id": "profile-a",
                "profile_digest": "e" * 64,
                "candidate_purpose": "production_replacement",
            }
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "tool_identity": {},
            }
            planned_jobs = [
                Job(
                    job_id=job_id,
                    phase="candidate",
                    suites=("full",),
                    description=job_id,
                    steps=(),
                    evidence_roots=(f"/tmp/evidence/{job_id}",),
                    covers=(),
                )
                for job_id in ("candidate-frozen-aux", "candidate-frozen-core")
            ]
            arguments = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id="candidate-a",
                candidate_image_id=identity["image_id"],
                rerun_failed=True,
                recovery_preview_payload={
                    "source_attempt_receipt_exists": False,
                    "execute_job_ids": ["candidate-frozen-core"],
                    "reuse_job_ids": [],
                },
                preview_recovery=False,
                acknowledge_live_requests=True,
                capture_root=Path("/root/oauth-capture"),
            )
            deadline = codex_upgrade.incremental_recovery.WallClockDeadline(120)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {}, clear=True))
                for target, options in (
                    ("_apply_candidate_runtime_override", {"return_value": manifest}),
                    ("_reject_contaminated_campaign", {}),
                    ("_load_stage_result", {"return_value": {"status": "complete"}}),
                    ("_requires_complete_vc_artifacts", {"return_value": False}),
                    ("_active_unsealed_attempts", {"return_value": []}),
                    ("_classification_candidate_reuse_source", {"return_value": None}),
                    ("_candidate_identity_for_run", {"return_value": identity}),
                    ("_campaign_jobs", {"return_value": planned_jobs}),
                    ("_tool_identity", {"return_value": {"files_sha256": "f" * 64}}),
                    (
                        "_cheap_capture_tool_impact",
                        {
                            "return_value": {
                                "kind": "unchanged",
                                "affected_job_ids": [],
                                "changed_components": [],
                            }
                        },
                    ),
                    ("_latest_failed_attempt_for_identity", {"return_value": None}),
                    ("_runtime_successor_recovery_source", {"return_value": None}),
                    ("_verify_plan_identity", {"return_value": {"kind": "unchanged"}}),
                    (
                        "_candidate_identity_for_incremental_execution",
                        {"return_value": identity},
                    ),
                    ("_verify_execution_tree", {}),
                    ("_require_capture_budget_before_data_action", {}),
                    ("_reserve_capture_attempt", {"side_effect": ReservationReached}),
                ):
                    stack.enter_context(mock.patch.object(codex_upgrade, target, **options))
                with self.assertRaises(ReservationReached):
                    codex_upgrade._run_capture_attempt(
                        arguments,
                        "candidate",
                        _lease=mock.Mock(),
                        _manifest=manifest,
                        _deadline=deadline,
                    )

    def test_incremental_admin_credential_still_required_when_aux_executes(
        self,
    ) -> None:
        """本轮执行集合包含 aux 时仍必须严格要求管理凭据。"""

        aux = Job(
            job_id="candidate-frozen-aux",
            phase="candidate",
            suites=("full",),
            description="candidate-frozen-aux",
            steps=(),
            evidence_roots=("/tmp/evidence/candidate-frozen-aux",),
            covers=(),
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "candidate-frozen-aux",
            ),
        ):
            codex_upgrade._validate_candidate_admin_credential([aux])

    def test_checked_in_baseline_scenario_bindings_match_sources(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        repo_root = tool_root.parents[1]
        scenario_path = tool_root / "codex_upgrade_scenarios_0_145_0.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))

        self.assertEqual(scenario["codex_version"], "0.145.0")

        source_spec = scenario["source_spec"]
        frozen_profile = json.loads(
            (
                tool_root / "candidate_rule_expectations_0_145_0.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            source_spec["sha256"],
            frozen_profile["source_spec_sha256"],
        )

        rule_binding = scenario["rule_manifest"]
        rule_path = repo_root / rule_binding["path"]
        self.assertEqual(
            rule_binding["sha256"],
            codex_upgrade.file_sha256(rule_path),
        )
        rule_manifest = json.loads(rule_path.read_text(encoding="utf-8"))
        self.assertEqual(
            rule_binding["rule_count"],
            len(rule_manifest["required_rules"]),
        )

        candidate_jobs = [
            job for job in scenario["capture_jobs"] if job["phase"] == "candidate"
        ]
        self.assertTrue(candidate_jobs)
        for job in candidate_jobs:
            for step in job["steps"]:
                self.assertEqual(
                    step["environment"].get("CODEX_VERSION"),
                    "{target_version}",
                )

        evidence_owners: dict[tuple[str, str], str] = {}
        for job in scenario["capture_jobs"]:
            for root in job["evidence_roots"]:
                owner_key = (job["phase"], root)
                self.assertNotIn(owner_key, evidence_owners)
                evidence_owners[owner_key] = job["id"]

        serialized_scenario = json.dumps(scenario, ensure_ascii=False)
        self.assertNotIn(
            "/capture/tools/official_client_capture",
            serialized_scenario,
        )
        wham_job = next(
            job for job in scenario["capture_jobs"] if job["id"] == "official-wham-safe"
        )
        wham_command = wham_job["steps"][1]["argv"][2]
        self.assertIn("basicConstraints=critical,CA:TRUE", wham_command)
        self.assertIn("basicConstraints=critical,CA:FALSE", wham_command)
        self.assertIn(
            "SSL_CERT_FILE=/capture/runtime/{campaign_id}-official-wham-safe/ca.crt",
            wham_command,
        )
        self.assertNotIn(
            "SSL_CERT_FILE=/capture/runtime/{campaign_id}-official-wham-safe/server.crt",
            wham_command,
        )

        mutated = json.loads(json.dumps(scenario))
        candidate = next(
            job for job in mutated["capture_jobs"] if job["phase"] == "candidate"
        )
        candidate["steps"][0]["environment"]["CODEX_VERSION"] = "0.147.0"
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "Campaign target_version",
        ):
            codex_upgrade._validate_scenario_manifest_shape(mutated)

    def test_current_scenario_manifests_are_additive_and_model_parameterized(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        repo_root = tool_root.parents[1]
        for version in ("0.147.0", "0.149.1", "0.151.0", "0.154.0"):
            suffix = version.replace(".", "_")
            scenario_path = tool_root / f"codex_upgrade_scenarios_{suffix}.json"
            scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
            self.assertEqual(scenario["codex_version"], version)
            codex_upgrade._validate_scenario_manifest_shape(scenario)

            source = scenario["source_spec"]
            if version == "0.147.0":
                frozen_binding = (
                    codex_upgrade._load_frozen_assertion_source_spec_binding(
                        tool_root
                        / "candidate_rule_expectations_0_147_0.json",
                        version,
                    )
                )
                self.assertEqual(
                    codex_upgrade._scenario_source_spec_binding(
                        scenario,
                        label="0.147.0 场景清单",
                    ),
                    frozen_binding,
                )
            else:
                self.assertEqual(
                    source["sha256"],
                    codex_upgrade.source_spec_section_sha256(
                        repo_root / source["path"], source["fragment"]
                    ),
                )
            rules = scenario["rule_manifest"]
            self.assertEqual(
                rules["sha256"], codex_upgrade.file_sha256(repo_root / rules["path"])
            )

            serialized = json.dumps(scenario, ensure_ascii=False)
            self.assertNotIn("gpt-5.4\"", serialized)
            self.assertNotIn("gpt-5.6-luna\"", serialized)
            core = next(
                job for job in scenario["capture_jobs"]
                if job["id"] == "candidate-frozen-core"
            )
            self.assertEqual(
                core["steps"][0]["environment"]["MAIN_MODEL"], "{model}"
            )
            self.assertEqual(
                core["steps"][0]["environment"]["LITE_MODEL"], "{lite_model}"
            )
            if version in {"0.149.1", "0.151.0", "0.154.0"}:
                auxiliary = next(
                    job
                    for job in scenario["capture_jobs"]
                    if job["id"] == "candidate-frozen-aux"
                )
                self.assertEqual(auxiliary["track"], "lite")
                self.assertEqual(auxiliary["model_id"], "{lite_model}")
                self.assertTrue(auxiliary["expected_use_responses_lite"])
                self.assertFalse(auxiliary["required_model_receipt"])
                wham_job = next(
                    job
                    for job in scenario["capture_jobs"]
                    if job["id"] == "official-wham-safe"
                )
                wham_command = wham_job["steps"][1]["argv"][2]
                self.assertIn("--entrypoint python3", wham_command)
                self.assertNotIn("{runtime_image} python3 ", wham_command)
                if version == "0.154.0":
                    self.assertIn(
                        "run_root={repo_root}/runs/{campaign_id}-official-wham-safe",
                        wham_command,
                    )
                    self.assertIn(
                        "runtime_root={repo_root}/runtime/{campaign_id}-official-wham-safe",
                        wham_command,
                    )
                    self.assertIn("-v {repo_root}:/capture", wham_command)
                    self.assertNotIn("-v {capture_root}:/capture", wham_command)
                realtime_job = next(
                    job
                    for job in scenario["capture_jobs"]
                    if job["id"] == "official-relay-realtime-webrtc"
                )
                self.assertEqual(
                    realtime_job["steps"][0]["environment"][
                        "RELAY_SYNTHESIZE_REALTIME_CALL_AFTER"
                    ],
                    "1",
                )

            if version == "0.151.0":
                jobs = {job["id"]: job for job in scenario["capture_jobs"]}
                negative = jobs["official-relay-file-upload-c2pa-negative"]
                positive = jobs["official-relay-file-upload-c2pa-positive"]
                auxiliary = jobs["candidate-frozen-aux"]
                self.assertNotEqual(
                    negative["evidence_roots"], positive["evidence_roots"]
                )
                self.assertNotEqual(
                    negative["steps"][0]["environment"]["RUN_ID"],
                    positive["steps"][0]["environment"]["RUN_ID"],
                )
                self.assertEqual(
                    negative["steps"][0]["environment"][
                        "A14_C2PA_EXPECTATION"
                    ],
                    "negative",
                )
                self.assertEqual(
                    positive["steps"][0]["environment"][
                        "A14_C2PA_EXPECTATION"
                    ],
                    "positive",
                )
                self.assertEqual(
                    negative["steps"][0]["environment"]["SCENARIO_JOB_ID"],
                    negative["id"],
                )
                self.assertEqual(
                    positive["steps"][0]["environment"]["SCENARIO_JOB_ID"],
                    positive["id"],
                )
                self.assertEqual(
                    auxiliary["steps"][0]["environment"][
                        "CANDIDATE_A14_C2PA_SEQUENCE"
                    ],
                    "negative,positive",
                )
                self.assertEqual(
                    negative["required_scenario_receipts"], ["A14"]
                )
                self.assertEqual(
                    positive["required_scenario_receipts"], ["A14"]
                )

                evidence_owners: dict[tuple[str, str], str] = {}
                for job in scenario["capture_jobs"]:
                    for evidence_root in job["evidence_roots"]:
                        owner = (job["phase"], evidence_root)
                        self.assertNotIn(owner, evidence_owners)
                        evidence_owners[owner] = job["id"]

    def test_01491_plan_jobs_execute_target_scenario_instead_of_baseline(self) -> None:
        """目标 CLI 的 official jobs 必须来自 0.149.1 清单。"""

        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            tool_root = Path(__file__).resolve().parents[1]
            arguments.baseline_version = "0.147.0"
            arguments.target_version = "0.149.1"
            arguments.rule_manifest = tool_root / "codex_upgrade_rules_0_147_0.json"
            arguments.scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_147_0.json"
            )
            arguments.target_scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_149_1.json"
            )
            arguments.output = arguments.campaign_dir
            arguments.model = "gpt-5.5"
            arguments.lite_model = "gpt-5.6-terra"
            rules = load_rule_manifest(arguments.rule_manifest, "0.147.0")

            jobs, baseline_path, target_path = codex_upgrade._load_plan_jobs(
                arguments, rules
            )

            self.assertEqual(baseline_path, arguments.scenario_manifest)
            self.assertEqual(target_path, arguments.target_scenario_manifest)
            wham_job = next(
                job for job in jobs if job.job_id == "official-wham-safe"
            )
            self.assertIn("--entrypoint python3", wham_job.steps[1]["argv"][2])
            realtime_job = next(
                job
                for job in jobs
                if job.job_id == "official-relay-realtime-webrtc"
            )
            self.assertEqual(
                realtime_job.steps[0]["environment"][
                    "RELAY_SYNTHESIZE_REALTIME_CALL_AFTER"
                ],
                "1",
            )
            auxiliary_job = next(
                job for job in jobs if job.job_id == "candidate-frozen-aux"
            )
            self.assertEqual(auxiliary_job.track, "lite")
            self.assertEqual(auxiliary_job.model_id, "gpt-5.6-terra")
            self.assertTrue(auxiliary_job.expected_use_responses_lite)
            self.assertFalse(auxiliary_job.required_model_receipt)

    def test_0151_plan_jobs_bind_both_c2pa_branches(self) -> None:
        """0.151 目标清单必须独立执行 A14 正负官方分支。"""

        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            tool_root = Path(__file__).resolve().parents[1]
            arguments.baseline_version = "0.149.1"
            arguments.target_version = "0.151.0"
            arguments.rule_manifest = (
                tool_root / "codex_upgrade_rules_0_149_1.json"
            )
            arguments.scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_149_1.json"
            )
            arguments.target_scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_151_0.json"
            )
            arguments.output = arguments.campaign_dir
            rules = load_rule_manifest(arguments.rule_manifest, "0.149.1")

            jobs, baseline_path, target_path = codex_upgrade._load_plan_jobs(
                arguments, rules
            )

            self.assertEqual(baseline_path, arguments.scenario_manifest)
            self.assertEqual(target_path, arguments.target_scenario_manifest)
            by_id = {job.job_id: job for job in jobs}
            negative = by_id["official-relay-file-upload-c2pa-negative"]
            positive = by_id["official-relay-file-upload-c2pa-positive"]
            auxiliary = by_id["candidate-frozen-aux"]
            self.assertNotEqual(negative.evidence_roots, positive.evidence_roots)
            self.assertNotEqual(
                negative.steps[0]["environment"]["RUN_ID"],
                positive.steps[0]["environment"]["RUN_ID"],
            )
            self.assertEqual(
                negative.steps[0]["environment"]["A14_C2PA_EXPECTATION"],
                "negative",
            )
            self.assertEqual(
                positive.steps[0]["environment"]["A14_C2PA_EXPECTATION"],
                "positive",
            )
            self.assertEqual(
                negative.steps[0]["environment"]["SCENARIO_JOB_ID"],
                negative.job_id,
            )
            self.assertEqual(
                positive.steps[0]["environment"]["SCENARIO_JOB_ID"],
                positive.job_id,
            )
            self.assertEqual(
                auxiliary.steps[0]["environment"][
                    "CANDIDATE_A14_C2PA_SEQUENCE"
                ],
                "negative,positive",
            )

    def test_safe_plan_preserves_job_step_environment(self) -> None:
        """冻结 Campaign 时不得丢失步骤环境变量。"""

        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            tool_root = Path(__file__).resolve().parents[1]
            arguments.baseline_version = "0.149.1"
            arguments.target_version = "0.151.0"
            arguments.rule_manifest = tool_root / "codex_upgrade_rules_0_149_1.json"
            arguments.scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_149_1.json"
            )
            arguments.target_scenario_manifest = (
                tool_root / "codex_upgrade_scenarios_0_151_0.json"
            )
            arguments.model = "gpt-5.5"
            arguments.lite_model = "gpt-5.6-terra"
            rules = load_rule_manifest(arguments.rule_manifest, "0.149.1")
            jobs, _, _ = codex_upgrade._load_plan_jobs(arguments, rules)

            plan = codex_upgrade._safe_plan(arguments, jobs, rules)
            auxiliary = next(
                job for job in plan["jobs"] if job["id"] == "candidate-frozen-aux"
            )
            self.assertEqual(
                auxiliary["steps"][0]["environment"]["CANDIDATE_A14_C2PA_SEQUENCE"],
                "negative,positive",
            )
            self.assertEqual(
                auxiliary["steps"][0]["environment"]["CODEX_VERSION"],
                "0.151.0",
            )

    def test_historical_baseline_uses_frozen_profile_and_target_uses_current_spec(
        self,
    ) -> None:
        """0.147 只认同版本冻结画像，0.149.1 仍严格绑定当前候选规格。"""

        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            arguments.baseline_version = "0.147.0"
            arguments.target_version = "0.149.1"
            arguments.campaign_dir = Path(directory).resolve() / "campaign"
            arguments.output = arguments.campaign_dir
            context = codex_upgrade._job_context(arguments)
            tool_root = Path(__file__).resolve().parents[1]
            baseline_scenario = (
                tool_root / "codex_upgrade_scenarios_0_147_0.json"
            )
            frozen_profile = (
                tool_root / "candidate_rule_expectations_0_147_0.json"
            )
            frozen_binding = (
                codex_upgrade._load_frozen_assertion_source_spec_binding(
                    frozen_profile,
                    "0.147.0",
                )
            )

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "规格第二章摘要不一致",
            ):
                codex_upgrade.load_scenario_jobs(
                    baseline_scenario,
                    context,
                    expected_version="0.147.0",
                    require_bindings=True,
                )
            baseline_jobs = codex_upgrade.load_scenario_jobs(
                baseline_scenario,
                context,
                expected_version="0.147.0",
                require_bindings=True,
                historical_source_spec_binding=frozen_binding,
            )
            self.assertTrue(baseline_jobs)

            target_jobs = codex_upgrade.load_scenario_jobs(
                tool_root / "codex_upgrade_scenarios_0_149_1.json",
                context,
                expected_version="0.149.1",
                require_bindings=True,
            )
            self.assertTrue(target_jobs)

            tampered_profile = Path(directory) / "tampered-profile.json"
            tampered = json.loads(frozen_profile.read_text(encoding="utf-8"))
            tampered["source_spec_sha256"] = "0" * 64
            self._write_json(tampered_profile, tampered)
            tampered_binding = (
                codex_upgrade._load_frozen_assertion_source_spec_binding(
                    tampered_profile,
                    "0.147.0",
                )
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "受控冻结画像不一致",
            ):
                codex_upgrade.load_scenario_jobs(
                    baseline_scenario,
                    context,
                    expected_version="0.147.0",
                    require_bindings=True,
                    historical_source_spec_binding=tampered_binding,
                )

    def test_runtime_successor_only_bridges_reclassified_historical_plan(self) -> None:
        """运行时后继只能用当前批准场景承接历史 Formal 摘要。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(root / "inputs")
            current = json.loads(
                arguments.target_scenario_manifest.read_text(encoding="utf-8")
            )
            frozen = json.loads(json.dumps(current, ensure_ascii=False))
            frozen["source_spec"]["sha256"] = "0" * 64
            frozen["profile_id"] = "codex-0.147.0-historical"
            approved = json.loads(json.dumps(current, ensure_ascii=False))
            approved["profile_id"] = "codex-0.147.0-approved"

            staging = root / "staging"
            frozen_path = staging / "inputs/target-discovery-scenarios.json"
            approved_path = staging / "classification/approved/scenarios.json"
            self._write_json(frozen_path, frozen)
            self._write_json(approved_path, approved)
            manifest = {
                "target_version": "0.147.0",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": frozen_path.relative_to(staging).as_posix(),
                        "sha256": codex_upgrade.file_sha256(frozen_path),
                    }
                },
            }
            classification_bindings = {
                "scenario_manifest": {
                    "path": approved_path.relative_to(staging).as_posix(),
                    "sha256": codex_upgrade.file_sha256(approved_path),
                }
            }
            self.assertEqual(
                codex_upgrade._successor_uses_reclassified_historical_plan_binding(
                    staging, manifest, classification_bindings
                ),
                codex_upgrade._scenario_source_spec_binding(
                    frozen,
                    label="测试历史 Formal 场景",
                ),
            )

            stale_approval = json.loads(json.dumps(approved, ensure_ascii=False))
            stale_approval["source_spec"]["sha256"] = "1" * 64
            self._write_json(approved_path, stale_approval)
            classification_bindings["scenario_manifest"]["sha256"] = (
                codex_upgrade.file_sha256(approved_path)
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "批准场景未绑定当前规格摘要",
            ):
                codex_upgrade._successor_uses_reclassified_historical_plan_binding(
                    staging,
                    manifest,
                    classification_bindings,
                )

            self.assertEqual(
                codex_upgrade._successor_uses_reclassified_historical_plan_binding(
                    staging,
                    manifest,
                    classification_bindings,
                    allow_historical_approved_source_spec=True,
                ),
                codex_upgrade._scenario_source_spec_binding(
                    frozen,
                    label="测试递归历史控制场景",
                ),
            )

            manifest["predecessor"] = {
                "reason": "candidate_recovery_control_refresh"
            }
            self.assertEqual(
                codex_upgrade._successor_uses_reclassified_historical_plan_binding(
                    staging,
                    manifest,
                    classification_bindings,
                ),
                codex_upgrade._scenario_source_spec_binding(
                    frozen,
                    label="测试控制刷新历史场景",
                ),
            )

            changed_execution = json.loads(json.dumps(approved, ensure_ascii=False))
            changed_execution["capture_jobs"][0]["steps"][0]["argv"] = ["false"]
            self._write_json(approved_path, changed_execution)
            classification_bindings["scenario_manifest"]["sha256"] = (
                codex_upgrade.file_sha256(approved_path)
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "官方执行合同不一致",
            ):
                codex_upgrade._successor_uses_reclassified_historical_plan_binding(
                    staging,
                    manifest,
                    classification_bindings,
                )

    def test_control_refresh_replays_contract_from_bound_preflight(self) -> None:
        """控制刷新发布后必须从绑定 preflight 复算合同，禁止退回历史场景。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            current_contract = {"scenario": "current"}
            rehearsal = {"preflight_campaign": {"campaign_id": "preflight"}}
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
                "predecessor": {
                    "reason": "candidate_recovery_control_refresh",
                },
            }
            rehearsal = {"preflight_campaign": {"campaign_id": "preflight"}}

            def build_contract(**values):
                return {"scenario": values["target_scenario"]["marker"]}

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=build_contract,
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: (
                        "e" * 64
                        if contract == current_contract
                        else "f" * 64
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_preflight_from_receipt",
                    return_value=(campaign_dir / "preflight", {}),
                ) as preflight,
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_target_scenario_override",
                    return_value={"marker": "current"},
                ) as scenario_override,
            ):
                contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                    recovery_rehearsal_receipt=rehearsal,
                )
                self.assertEqual(contract, current_contract)
                preflight.assert_called_once_with(rehearsal, manifest)
                scenario_override.assert_called_once()
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "绑定的 preflight/no-op 收据",
                ):
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign_dir,
                        manifest,
                    )

    def test_published_reclassification_successor_replays_bound_noop_scenario(
        self,
    ) -> None:
        """原子发布后的状态重放必须继续使用 no-op preflight 当前场景。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            current_contract = {"scenario": "current"}
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
                "predecessor": {
                    "reason": "classification_fact_correction",
                },
            }

            def build_contract(**values):
                return {"scenario": values["target_scenario"]["marker"]}

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=build_contract,
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: (
                        "e" * 64 if contract == current_contract else "f" * 64
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_successor_incremental_noop_preflight",
                    return_value=(campaign_dir / "preflight", {}),
                ) as noop_preflight,
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_target_scenario_override",
                    return_value={"marker": "current"},
                ) as scenario_override,
            ):
                contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                )

            self.assertEqual(contract, current_contract)
            noop_preflight.assert_called_once_with(
                manifest,
                require_active=True,
            )
            scenario_override.assert_called_once()

    def test_published_failed_job_recovery_replays_bound_noop_scenario(
        self,
    ) -> None:
        """失败 Job 后继运行时必须继续使用 no-op preflight 当前场景。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            current_contract = {"scenario": "current"}
            rehearsal = {"preflight_campaign": {"campaign_id": "preflight"}}
            manifest = {
                "target_version": "0.154.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
                "predecessor": {
                    "reason": "candidate_failed_job_tool_recovery",
                },
            }

            def build_contract(**values):
                return {"scenario": values["target_scenario"]["marker"]}

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=build_contract,
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: (
                        "e" * 64 if contract == current_contract else "f" * 64
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_preflight_from_receipt",
                    return_value=(campaign_dir / "preflight", {}),
                ) as bound_preflight,
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_target_scenario_override",
                    return_value={"marker": "current"},
                ) as scenario_override,
            ):
                contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                    recovery_rehearsal_receipt=rehearsal,
                )

            self.assertEqual(contract, current_contract)
            bound_preflight.assert_called_once_with(
                rehearsal,
                manifest,
            )
            scenario_override.assert_called_once()
            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    return_value={"scenario": "historical"},
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "恢复后继必须从绑定的 preflight/no-op 收据",
                ),
            ):
                codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                )

    def test_read_only_reclassification_contract_allows_stopped_noop_preflight(
        self,
    ) -> None:
        """只读加载旧后继时不得要求其 no-op preflight 仍 active。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
                "predecessor": {
                    "reason": "classification_fact_correction",
                },
            }

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    return_value={"scenario": "historical"},
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    return_value="e" * 64,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_successor_incremental_noop_preflight",
                    return_value=None,
                ) as noop_preflight,
            ):
                codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                    _require_incremental_noop_preflight_active=False,
                )

            noop_preflight.assert_called_once_with(
                manifest,
                require_active=False,
            )

    def test_metadata_only_flag_reaches_noop_preflight_chain(self) -> None:
        """外层确认 metadata-only fallback 后必须把许可传到 no-op 链。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            scenario_path.parent.mkdir(parents=True)
            self._write_json(scenario_path, {"marker": "historical"})
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {"path": "inputs/target.json"},
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {"execution_contract_sha256": "e" * 64}
                },
                "predecessor": {"reason": "classification_fact_correction"},
            }
            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    return_value={"scenario": "historical"},
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    return_value="e" * 64,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_successor_incremental_noop_preflight",
                    return_value=None,
                ) as noop_preflight,
            ):
                codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                    _require_incremental_noop_preflight_active=False,
                    _allow_stopped_metadata_only=True,
                )

            noop_preflight.assert_called_once_with(
                manifest,
                require_active=False,
                allow_stopped_metadata_only=True,
            )

    def test_metadata_only_flag_reaches_coordinate_control_replay(self) -> None:
        """no-op 坐标重放必须把 metadata-only 许可传给内层控制校验。"""

        manifest = {
            "control_receipts": {
                "job_rehearsal": {
                    "evidence_root": "/evidence",
                    "receipt": {"path": "receipt.json"},
                }
            }
        }
        with mock.patch.object(
            codex_upgrade,
            "_successor_incremental_noop_preflight_from_coordinates",
            return_value=None,
        ) as coordinates:
            codex_upgrade._successor_incremental_noop_preflight(
                manifest,
                require_active=True,
                allow_stopped_metadata_only=True,
            )
        coordinates.assert_called_once_with(
            Path("/evidence"),
            Path("receipt.json"),
            manifest,
            label="分类纠正后继继承的 Job 演练",
            require_active=True,
            allow_stopped_metadata_only=True,
        )

        with (
            mock.patch.object(
                codex_upgrade,
                "_control_receipt_relative",
                return_value="receipt.json",
            ),
            mock.patch.object(
                codex_upgrade.codex_upgrade_job_rehearsal_receipt,
                "replay",
                return_value={"status": "incremental-noop"},
            ),
            mock.patch.object(
                codex_upgrade,
                "_recovery_rehearsal_preflight_from_receipt",
                return_value=(Path("/preflight"), {}),
            ),
            mock.patch.object(
                codex_upgrade,
                "_verify_control_receipts",
            ) as verify_controls,
        ):
            codex_upgrade._successor_incremental_noop_preflight_from_coordinates(
                Path("/evidence"),
                Path("receipt.json"),
                manifest,
                label="测试 no-op",
                require_active=True,
                allow_stopped_metadata_only=True,
            )
        verify_controls.assert_called_once_with(
            Path("/preflight"),
            {},
            require_active=True,
            allow_stopped_metadata_only=True,
        )

    def test_active_runtime_override_replays_ancestor_noop_historically(
        self,
    ) -> None:
        """当前修复控制 active 时，祖先 no-op preflight 仍按历史状态重放。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            current_contract = {"scenario": "current"}
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
                "predecessor": {"reason": "classification_fact_correction"},
            }
            override = {
                "job_rehearsal": {"execution_contract_sha256": "e" * 64}
            }

            def build_contract(**values):
                return {"scenario": values["target_scenario"]["marker"]}

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    side_effect=build_contract,
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    side_effect=lambda contract: (
                        "e" * 64 if contract == current_contract else "f" * 64
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_successor_incremental_noop_preflight",
                    return_value=(campaign_dir / "preflight", {}),
                ) as noop_preflight,
                mock.patch.object(
                    codex_upgrade,
                    "_recovery_rehearsal_target_scenario_override",
                    return_value={"marker": "current"},
                ),
            ):
                contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                    campaign_dir,
                    manifest,
                    control_receipts_override=override,
                )

            self.assertEqual(contract, current_contract)
            noop_preflight.assert_called_once_with(
                manifest,
                require_active=False,
            )

    def test_bound_rehearsal_replays_only_evidence_label_digest(self) -> None:
        """已绑定重放的演练只承接冻结的证据标签摘要。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            scenario_path = campaign_dir / "inputs/target.json"
            self._write_json(scenario_path, {"marker": "historical"})
            current_contract = {
                "scenario": "historical",
                "evidence_label_declaration_sha256": "a" * 64,
            }
            historical_contract = {
                **current_contract,
                "evidence_label_declaration_sha256": "b" * 64,
            }
            manifest = {
                "target_version": "0.151.0",
                "target_sha256": "a" * 64,
                "suite": "full",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target.json",
                    },
                    "extra_jobs": None,
                },
                "official_identity": {
                    "package": {
                        "asset_sha256": "b" * 64,
                        "code_mode_host_sha256": "c" * 64,
                    }
                },
                "configuration": {},
                "tool_identity": {"files_sha256": "d" * 64},
                "control_receipts": {
                    "job_rehearsal": {
                        "execution_contract_sha256": "e" * 64,
                    }
                },
            }
            rehearsal = {"execution_contract": historical_contract}

            with (
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "build_execution_contract",
                    return_value=current_contract,
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "validate_execution_contract",
                ),
                mock.patch.object(
                    codex_upgrade_job_rehearsal_receipt,
                    "execution_contract_sha256",
                    return_value="e" * 64,
                ),
            ):
                self.assertEqual(
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign_dir,
                        manifest,
                        recovery_rehearsal_receipt=rehearsal,
                        _allow_bound_evidence_label_digest=True,
                    ),
                    historical_contract,
                )
                self.assertEqual(
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign_dir,
                        manifest,
                        recovery_rehearsal_receipt=rehearsal,
                    ),
                    current_contract,
                )
                # 工具历史兼容不能隐式放宽 evaluator 标签摘要；必须由已经
                # 按 control 文件绑定重放收据的调用点显式授权。
                self.assertEqual(
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign_dir,
                        manifest,
                        recovery_rehearsal_receipt=rehearsal,
                        _allow_historical_tool_identity=True,
                    ),
                    current_contract,
                )

                drifted = {
                    **historical_contract,
                    "scenario": "changed",
                }
                self.assertEqual(
                    codex_upgrade._job_rehearsal_contract_from_manifest(
                        campaign_dir,
                        manifest,
                        recovery_rehearsal_receipt={
                            "execution_contract": drifted
                        },
                        _allow_bound_evidence_label_digest=True,
                    ),
                    current_contract,
                )

    def test_plan_freezes_target_scenario_and_official_reloads_same_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(
                root, campaign_mode="preflight_only"
            )
            target_payload = json.loads(
                arguments.target_scenario_manifest.read_text(encoding="utf-8")
            )
            official_job = next(
                job
                for job in target_payload["capture_jobs"]
                if job["id"] == "official-test"
            )
            official_job["steps"][0]["argv"] = ["printf", "target-scenario"]
            self._write_json(arguments.target_scenario_manifest, target_payload)

            manifest = codex_upgrade.create_campaign(arguments)
            target_reference = manifest["inputs"]["target_discovery_scenarios"]
            frozen_target = root / "campaign" / target_reference["path"]
            self.assertEqual(
                json.loads(frozen_target.read_text(encoding="utf-8"))[
                    "codex_version"
                ],
                "0.147.0",
            )
            jobs = codex_upgrade._campaign_jobs(
                root / "campaign", manifest, "official"
            )
            reloaded = next(job for job in jobs if job.job_id == "official-test")
            self.assertEqual(reloaded.steps[0]["argv"], ["printf", "target-scenario"])

    def test_plan_preserves_exact_bytes_for_bound_json_inputs(self) -> None:
        """非规范 JSON 的冻结字节和 SHA 不得因重新格式化而漂移。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(
                root, campaign_mode="preflight_only"
            )

            baseline_rules = root / "baseline-rules-noncanonical.json"
            baseline_payload = json.loads(
                arguments.rule_manifest.read_text(encoding="utf-8")
            )
            baseline_rules.write_bytes(
                (
                    " \r\n"
                    + json.dumps(
                        baseline_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\r\n"
                ).encode("utf-8")
            )
            arguments.rule_manifest = baseline_rules

            baseline_scenarios = json.loads(
                arguments.scenario_manifest.read_text(encoding="utf-8")
            )
            baseline_scenarios["rule_manifest"]["path"] = baseline_rules.name
            baseline_scenarios["rule_manifest"]["sha256"] = (
                codex_upgrade.file_sha256(baseline_rules)
            )
            arguments.scenario_manifest.write_bytes(
                (
                    json.dumps(
                        baseline_scenarios,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + " \r\n"
                ).encode("utf-8")
            )

            target_scenarios = json.loads(
                arguments.target_scenario_manifest.read_text(encoding="utf-8")
            )
            arguments.target_scenario_manifest.write_bytes(
                (
                    "\n\t"
                    + json.dumps(
                        target_scenarios,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ).encode("utf-8")
            )

            manifest = codex_upgrade.create_campaign(arguments)
            for input_name, source in (
                ("baseline_rules", arguments.rule_manifest),
                ("discovery_scenarios", arguments.scenario_manifest),
                (
                    "target_discovery_scenarios",
                    arguments.target_scenario_manifest,
                ),
            ):
                with self.subTest(input_name=input_name):
                    reference = manifest["inputs"][input_name]
                    frozen = arguments.campaign_dir / reference["path"]
                    self.assertEqual(frozen.read_bytes(), source.read_bytes())
                    self.assertEqual(
                        reference["sha256"],
                        codex_upgrade.file_sha256(source),
                    )

    def test_plan_rejects_baseline_manifest_as_target_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            arguments.target_scenario_manifest = arguments.scenario_manifest
            arguments.output = arguments.campaign_dir
            rules = load_rule_manifest(
                arguments.rule_manifest, arguments.baseline_version
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "codex_version 与当前阶段不一致",
            ):
                codex_upgrade._load_plan_jobs(arguments, rules)

    def test_campaign_capture_scripts_bind_frozen_tool_root(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        scripts = (
            "run_official_codex_compact_capture.sh",
            "run_official_http_fallback_baseline.sh",
            "run_official_relay_scenario.sh",
            "run_sub2api_direct_matrix.sh",
            "run_sub2api_openai_mitm_matrix.sh",
            "run_h1_wire_probe.sh",
            "run_images_wire_probe.sh",
        )
        for name in scripts:
            content = (tool_root / name).read_text(encoding="utf-8")
            self.assertIn("capture_tool_root=", content, name)
            self.assertNotIn(
                "/capture/tools/official_client_capture",
                content,
                name,
            )

    def test_job_validation_rejects_shared_phase_evidence_root(self) -> None:
        jobs = [
            Job(
                job_id=job_id,
                phase="official",
                suites=("full",),
                description=job_id,
                steps=({"argv": ["true"], "environment": {}, "timeout": 60},),
                evidence_roots=("/tmp/shared-evidence",),
                covers=(),
            )
            for job_id in ("official-one", "official-two")
        ]
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "证据根必须由单一任务独占",
        ):
            codex_upgrade._validate_jobs(jobs, ())

    def test_resume_parser_exposes_classification_candidate_reuse_approval(
        self,
    ) -> None:
        parser = codex_upgrade._build_parser()
        resume_parser = next(
            action.choices["resume"]
            for action in parser._actions
            if getattr(action, "choices", None) and "resume" in action.choices
        )
        actions = {action.dest: action for action in resume_parser._actions}
        self.assertIn("candidate_reuse_source_campaign_dir", actions)
        self.assertIn("candidate_reuse_source_candidate_id", actions)
        self.assertIn("candidate_reuse_source_attempt_id", actions)
        self.assertIn("approve_candidate_reuse_sha256", actions)

    def test_post_run_seal_recovery_parser_freezes_a15_source(self) -> None:
        """专用入口默认绑定 A15 唯一 Candidate／attempt，不能退化为通用 successor。"""

        parser = codex_upgrade._build_parser()
        arguments = parser.parse_args(
            [
                codex_upgrade.FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND,
                "--predecessor-campaign-dir",
                str(codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE["campaign_dir"]),
                "--campaign-dir",
                "/tmp/successor",
                "--campaign-id",
                "successor-a",
                "--codex-account-id",
                "90",
                "--job-rehearsal-root",
                "/tmp/rehearsal",
                "--job-rehearsal-receipt",
                "/tmp/rehearsal/receipt.json",
                "--recovery-timing-ledger-dir",
                "/tmp/timing",
                "--recovery-timing-receipt",
                "/tmp/timing/receipt.json",
                "--recovery-arm64-environment-root",
                "/tmp/arm64",
                "--recovery-arm64-environment-receipt",
                "/tmp/arm64/receipt.json",
                "--predecessor-stop-ledger-dir",
                "/tmp/stop",
                "--predecessor-stop-receipt",
                "/tmp/stop/receipt.json",
            ]
        )
        self.assertEqual(
            arguments.reason,
            codex_upgrade.POST_RUN_SEAL_RECOVERY_REASON,
        )
        self.assertEqual(
            arguments.predecessor_candidate_id,
            codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE["candidate_id"],
        )
        self.assertEqual(
            arguments.predecessor_attempt_id,
            codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE["attempt_id"],
        )

    def test_post_run_seal_recovery_rejects_canonicalized_source(self) -> None:
        """来源切换到 canonical checkpoint 后，专用恢复入口也必须只读。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor = root / "predecessor"
            marker = (
                predecessor
                / codex_upgrade.CANONICAL_DIRECTORY
                / codex_upgrade.CANONICAL_IMPORT_RECEIPT_FILENAME
            )
            self._write_json(marker, {"status": "complete"})
            arguments = argparse.Namespace(
                campaign_dir=root / "successor",
                predecessor_campaign_dir=predecessor,
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "canonical checkpoint",
            ):
                codex_upgrade._reject_canonical_legacy_write(
                    arguments,
                    codex_upgrade.FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND,
                )

    def test_metadata_only_job_roots_exclude_attempt_evidence_and_logs(self) -> None:
        """复用边界只能来自九项结果，不得夹带来源 attempt 自身根。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            job_root = base / "job-root"
            attempt_evidence = base / "attempt" / "evidence"
            attempt_logs = base / "attempt" / "logs"
            for path in (job_root, attempt_evidence, attempt_logs):
                path.mkdir(parents=True, mode=0o700)
            attempt = {
                "results": [
                    {
                        "id": job_id,
                        "status": "complete",
                        "evidence_roots": [str(job_root)],
                    }
                    for job_id in sorted(
                        codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
                    )
                ],
                "evidence_roots": [
                    str(job_root),
                    str(attempt_evidence),
                    str(attempt_logs),
                ],
            }
            roots = codex_upgrade._classification_candidate_job_evidence_roots(
                attempt,
                require_existing=True,
            )
            self.assertEqual(roots, [job_root.resolve()])

    def test_post_run_seal_build_projection_accepts_only_frozen_v7_origin(
        self,
    ) -> None:
        """A15 的 VC-4 投影保留原始 v7 绑定，但不得接受其他历史身份。"""

        a15 = codex_upgrade.C0154_A15_POST_RUN_SEAL_SOURCE
        v7 = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        manifest = {
            "campaign_id": a15["campaign_id"],
            "target_version": a15["target_version"],
            "campaign_purpose": "production_replacement",
            "tool_identity": {"files_sha256": a15["tool_files_sha256"]},
        }
        receipt = {
            "campaign_id": v7["campaign_id"],
            "campaign_manifest_sha256": v7["campaign_manifest_sha256"],
        }
        matches = codex_upgrade._candidate_build_projection_campaign_binding_matches
        self.assertTrue(
            matches(
                manifest,
                predecessor_manifest_sha256=a15["campaign_manifest_sha256"],
                receipt=receipt,
                candidate_id=a15["candidate_id"],
                attempt_id=a15["attempt_id"],
            )
        )
        drifted = dict(receipt)
        drifted["campaign_manifest_sha256"] = "0" * 64
        self.assertFalse(
            matches(
                manifest,
                predecessor_manifest_sha256=a15["campaign_manifest_sha256"],
                receipt=drifted,
                candidate_id=a15["candidate_id"],
                attempt_id=a15["attempt_id"],
            )
        )
        unrelated = copy.deepcopy(manifest)
        unrelated["campaign_id"] = "unrelated-campaign"
        self.assertFalse(
            matches(
                unrelated,
                predecessor_manifest_sha256="1" * 64,
                receipt=receipt,
                candidate_id=a15["candidate_id"],
                attempt_id=a15["attempt_id"],
            )
        )

    def test_classification_candidate_reuse_preview_stops_before_all_writes(
        self,
    ) -> None:
        """零请求预览不得预约、探针、验镜像或读取管理凭据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir()
            arguments, manifest, identity, jobs, context = (
                self._classification_candidate_reuse_run_fixture(
                    campaign_dir,
                    preview=True,
                )
            )
            preview = {
                "status": "approval_required",
                "execute_job_ids": [],
                "reused_job_ids": sorted(
                    codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
                ),
                "reservation_exists": False,
                "live_request_count": 0,
                "scanned_bytes": 0,
            }
            with (
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={"status": "complete"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_active_unsealed_attempts",
                    return_value=[],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_classification_candidate_reuse_source",
                    return_value=context,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_candidate_identity_for_run",
                    return_value=identity,
                ) as identity_builder,
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_jobs",
                    return_value=jobs,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={"files_sha256": "1" * 64},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_cheap_capture_tool_impact",
                    return_value={"affected_job_ids": [], "changed_components": []},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_build_classification_candidate_reuse_preview",
                    return_value=(preview, []),
                ),
                mock.patch.object(codex_upgrade, "_verify_plan_identity") as verify,
                mock.patch.object(
                    codex_upgrade,
                    "_reserve_capture_attempt",
                ) as reserve,
                mock.patch.object(
                    codex_upgrade,
                    "_validate_candidate_admin_credential",
                ) as credential,
            ):
                result = codex_upgrade._run_capture_attempt(
                    arguments,
                    "candidate",
                    _lease=mock.Mock(),
                    _manifest=manifest,
                    _deadline=mock.Mock(),
                )
            self.assertEqual(result, preview)
            self.assertEqual(identity_builder.call_count, 1)
            verify.assert_not_called()
            reserve.assert_not_called()
            credential.assert_not_called()
            self.assertEqual(list(campaign_dir.iterdir()), [])

    def test_classification_candidate_reuse_approval_has_zero_job_execution(
        self,
    ) -> None:
        """批准后建立全 reused attempt，不要求 live ack 或管理 token。"""

        class ReservationReached(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir()
            arguments, manifest, identity, jobs, context = (
                self._classification_candidate_reuse_run_fixture(
                    campaign_dir,
                    preview=False,
                )
            )
            reused = [
                {"id": job.job_id, "status": "complete", "disposition": "reused"}
                for job in jobs
            ]
            transition_binding = {
                "path": codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_TRANSITION_FILENAME,
                "sha256": "2" * 64,
            }
            source_binding = {**transition_binding, "bytes": 1}
            with (
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={"status": "complete"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_active_unsealed_attempts",
                    return_value=[],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_classification_candidate_reuse_source",
                    return_value=context,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_candidate_identity_for_run",
                    return_value=identity,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_jobs",
                    return_value=jobs,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value={"files_sha256": "1" * 64},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_cheap_capture_tool_impact",
                    return_value={"affected_job_ids": [], "changed_components": []},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_build_classification_candidate_reuse_preview",
                    return_value=({"preview_sha256": "3" * 64}, reused),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_approve_classification_candidate_reuse_transition",
                    return_value=({}, transition_binding, source_binding),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_prior_complete_results",
                    return_value=reused,
                ) as prior,
                mock.patch.object(
                    codex_upgrade,
                    "_verify_plan_identity",
                    return_value=None,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_execution_tree",
                ) as execution_tree,
                mock.patch.object(
                    codex_upgrade,
                    "_validate_candidate_admin_credential",
                ) as credential,
                mock.patch.object(
                    codex_upgrade,
                    "_reserve_capture_attempt",
                    side_effect=ReservationReached,
                ) as reserve,
            ):
                with self.assertRaises(ReservationReached):
                    codex_upgrade._run_capture_attempt(
                        arguments,
                        "candidate",
                        _lease=mock.Mock(),
                        _manifest=manifest,
                        _deadline=mock.Mock(),
                    )
            self.assertFalse(arguments.acknowledge_live_requests)
            credential.assert_not_called()
            execution_tree.assert_not_called()
            self.assertEqual(
                {job.job_id for job in reserve.call_args.kwargs["jobs"]},
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS,
            )
            source_tool = prior.call_args.kwargs["source_tool_identity"]
            self.assertEqual(
                source_tool["files_sha256"],
                codex_upgrade._fingerprint(
                    {"entries": [{"path": "capture.py", "sha256": "d" * 64}]}
                ),
            )

    def test_classification_candidate_reuse_writes_metadata_only_attempt(
        self,
    ) -> None:
        """批准后只写预约、复用 checkpoint 和来源绑定，不运行环境或 Job。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            source_evidence = root / "source-evidence"
            source_evidence.mkdir(mode=0o700)
            arguments, manifest, identity, jobs, context = (
                self._classification_candidate_reuse_run_fixture(
                    campaign_dir,
                    preview=False,
                )
            )
            context["source_attempt"] = {
                "attempt_id": "source-attempt-a",
                "status": "awaiting_receipts",
                "evidence_roots": [str(source_evidence.resolve())],
                "results": [
                    {
                        "id": job.job_id,
                        "status": "complete",
                        "evidence_roots": [str(source_evidence.resolve())],
                    }
                    for job in jobs
                ],
                "environment": {
                    "evidence_root": str(source_evidence.resolve()),
                },
            }
            reused = [
                {
                    "id": job.job_id,
                    "status": "complete",
                    "disposition": "reused",
                    "source_receipt": {
                        "path": "classification-candidate-reuse-transition.json",
                        "sha256": "2" * 64,
                        "bytes": 1,
                    },
                }
                for job in jobs
            ]
            attempt_root = campaign_dir / "candidates" / "candidate-a" / "attempts" / "attempt-a"
            attempt_root.mkdir(parents=True, mode=0o700)
            reservation = {
                "run_nonce": "4" * 64,
                "started_at_utc": "2026-09-05T00:00:00Z",
            }
            transition_binding = {
                "path": codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_TRANSITION_FILENAME,
                "sha256": "2" * 64,
            }
            source_binding = {**transition_binding, "bytes": 1}
            projected_environment = {
                "evidence_root": str((attempt_root / "evidence").resolve()),
                "before_probe": {"path": "environment/before/probe-manifest.json", "sha256": "7" * 64, "bytes": 1},
                "after_probe": {"path": "environment/after/probe-manifest.json", "sha256": "8" * 64, "bytes": 1},
                "restoration_report": {"path": "receipts/restoration-report.json", "sha256": "9" * 64, "bytes": 1},
                "arm64_before_receipt": {"path": "environment/arm64-before/receipt.json", "sha256": "a" * 64, "bytes": 1},
                "arm64_after_receipt": {"path": "environment/arm64-after/receipt.json", "sha256": "b" * 64, "bytes": 1},
            }
            deadline = codex_upgrade._attempt_deadline(
                argparse.Namespace(max_wall_seconds=60, heartbeat_seconds=5),
                "candidate",
            )

            def write_attempt(
                _campaign_dir: Path,
                _attempt_root: Path,
                payload: dict[str, object],
            ) -> dict[str, object]:
                codex_upgrade._validate_attempt_incremental_fields(
                    payload,
                    {job.job_id for job in jobs},
                )
                return {**payload, "attempt_digest": "5" * 64}

            # 20 个替身用 ExitStack 逐个进入：Python 3.12 的静态嵌套块上限是 20，
            # 括号式 with 每个上下文各算一个块，受管部署机（3.12）编译不过。
            with contextlib.ExitStack() as stack:
                for target, options in (
                    ("_reject_contaminated_campaign", {}),
                    ("_load_stage_result", {"return_value": {"status": "complete"}}),
                    ("_active_unsealed_attempts", {"return_value": []}),
                    ("_classification_candidate_reuse_source", {"return_value": context}),
                    ("_candidate_identity_for_run", {"return_value": identity}),
                    ("_campaign_jobs", {"return_value": jobs}),
                    ("_tool_identity", {"return_value": {"files_sha256": "1" * 64}}),
                    ("_cheap_capture_tool_impact", {"return_value": {"affected_job_ids": [], "changed_components": []}}),
                    ("_build_classification_candidate_reuse_preview", {"return_value": ({"preview_sha256": "3" * 64}, reused)}),
                    ("_approve_classification_candidate_reuse_transition", {"return_value": ({}, transition_binding, source_binding)}),
                    ("_prior_complete_results", {"return_value": reused}),
                    ("_reserve_capture_attempt", {"return_value": (attempt_root, reservation)}),
                    (
                        "_close_attempt_evidence_permissions",
                        {
                            "return_value": {
                                "path": "evidence-permission-closeout.json",
                                "sha256": "6" * 64,
                                "bytes": 1,
                            }
                        },
                    ),
                    (
                        "_materialize_metadata_only_environment_projection",
                        {"return_value": projected_environment},
                    ),
                ):
                    stack.enter_context(mock.patch.object(codex_upgrade, target, **options))
                writer = stack.enter_context(
                    mock.patch.object(codex_upgrade, "_write_capture_attempt", side_effect=write_attempt)
                )
                probe = stack.enter_context(mock.patch.object(codex_upgrade, "_probe_capture_environment"))
                arm64_probe = stack.enter_context(mock.patch.object(codex_upgrade, "_capture_arm64_environment_receipt"))
                run_job = stack.enter_context(mock.patch.object(codex_upgrade, "_run_job_with_retry"))
                verify = stack.enter_context(mock.patch.object(codex_upgrade, "_verify_plan_identity"))
                execution_tree = stack.enter_context(mock.patch.object(codex_upgrade, "_verify_execution_tree"))
                credential = stack.enter_context(mock.patch.object(codex_upgrade, "_validate_candidate_admin_credential"))
                result = codex_upgrade._run_capture_attempt(
                    arguments,
                    "candidate",
                    _lease=mock.Mock(),
                    _manifest=manifest,
                    _deadline=deadline,
                )
            self.assertEqual(result["status"], "awaiting_receipts")
            self.assertEqual(result["live_request_count"], 0)
            self.assertEqual(result["scanned_bytes"], 0)
            payload = writer.call_args.args[2]
            self.assertEqual(payload["incremental_plan"]["executed_job_ids"], [])
            self.assertEqual(
                set(payload["incremental_plan"]["reused_job_ids"]),
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS,
            )
            self.assertEqual(payload["environment"], projected_environment)
            invalid_payload = copy.deepcopy(payload)
            invalid_payload["environment"]["restoration_report"] = None
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "metadata-only attempt 的零执行边界不闭合",
            ):
                codex_upgrade._validate_attempt_incremental_fields(
                    invalid_payload,
                    {job.job_id for job in jobs},
                )
            probe.assert_not_called()
            arm64_probe.assert_not_called()
            run_job.assert_not_called()
            verify.assert_not_called()
            execution_tree.assert_not_called()
            credential.assert_not_called()

    def test_metadata_only_attempt_loader_forwards_frozen_identity(self) -> None:
        """status 重放 metadata-only attempt 时必须把冻结身份交给来源 transition。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            attempt_root = (
                campaign_dir
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            attempt_root.mkdir(parents=True)
            campaign_path = campaign_dir / "campaign.json"
            reservation_path = attempt_root / "reservation.json"
            transition_path = (
                campaign_dir
                / codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_TRANSITION_FILENAME
            )
            self._write_json(campaign_path, {"campaign_id": "campaign-a"})
            self._write_json(reservation_path, {"reservation": "frozen"})
            self._write_json(transition_path, {"transition": "frozen"})
            identity = {"profile_id": "codex-0.151.0"}
            manifest = {
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
            }
            reservation = {
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "candidate_purpose": "validation_only",
                "run_nonce": "1" * 64,
                "started_at_utc": "2026-09-05T00:00:00Z",
                "identity_sha256": codex_upgrade._fingerprint(identity),
                "planned_jobs": [],
            }
            attempt = {
                "schema_version": codex_upgrade.LEGACY_CAPTURE_ATTEMPT_SCHEMA,
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "candidate_purpose": "validation_only",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign_path
                ),
                "attempt_id": "attempt-a",
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "run_nonce": "1" * 64,
                "started_at_utc": "2026-09-05T00:00:00Z",
                "completed_at_utc": "2026-09-05T00:00:01Z",
                "identity": identity,
                "reservation": {
                    "path": str(reservation_path.relative_to(campaign_dir)),
                    "sha256": codex_upgrade.file_sha256(reservation_path),
                },
                "status": "awaiting_receipts",
                "classification_candidate_reuse_transition": {
                    "path": codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_TRANSITION_FILENAME,
                    "sha256": codex_upgrade.file_sha256(transition_path),
                },
            }
            attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
            self._write_json(attempt_root / "attempt.json", attempt)

            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                mock.patch.object(codex_upgrade, "_require_formal_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=reservation,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_attempt_incremental_fields",
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_attempt_watchdog_bindings",
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_classification_candidate_reuse_transition",
                    return_value={},
                ) as transition_loader,
            ):
                _, loaded = codex_upgrade._load_capture_attempt(
                    campaign_dir,
                    "candidate",
                    "candidate-a",
                    "attempt-a",
                )

            self.assertEqual(loaded["identity"], identity)
            self.assertEqual(
                transition_loader.call_args.kwargs["identity"],
                identity,
            )

    def test_cross_campaign_reuse_requires_transition_outside_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            source = root / "source"
            current.mkdir()
            (source / "candidates" / "candidate-a" / "attempts").mkdir(
                parents=True
            )
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                side_effect=[{"campaign_id": "source"}, {"campaign_id": "current"}],
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "缺少当前 Campaign 来源 transition",
                ):
                    codex_upgrade._prior_complete_results(
                        current,
                        Path("candidates/candidate-b"),
                        [],
                        phase="candidate",
                        candidate_id="candidate-b",
                        identity={},
                        source_attempt_id="attempt-a",
                        source_campaign_dir=source,
                        source_candidate_id="candidate-a",
                        expected_reuse_job_ids=(),
                        allowed_source_statuses=("awaiting_receipts",),
                    )

    def test_candidate_reuse_wrong_approval_digest_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            preview = self._valid_classification_candidate_reuse_preview()
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "批准摘要",
            ):
                codex_upgrade._approve_classification_candidate_reuse_transition(
                    campaign_dir,
                    preview,
                    "f" * 64,
                )
            self.assertFalse(
                (
                    campaign_dir
                    / codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_TRANSITION_FILENAME
                ).exists()
            )

    def test_classification_candidate_reuse_source_accepts_unique_nine_jobs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_source_fixture(
                Path(directory)
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=fixture["source_manifest"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(fixture["source_attempt_root"], fixture["attempt"]),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=fixture["reservation"],
                ),
            ):
                context = codex_upgrade._classification_candidate_reuse_source(
                    fixture["arguments"],
                    fixture["campaign_dir"],
                    fixture["manifest"],
                    {"status": "complete", "predecessor_import": None},
                    candidate_id="candidate-target",
                )
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context["source_attempt_id"], "attempt-source")
            self.assertEqual(context["identity"], fixture["attempt"]["identity"])

    def test_classification_candidate_reuse_allows_only_vc4_build_projection(
        self,
    ) -> None:
        """当前 Candidate 可已有 VC-4 构建收据，但不得已有 attempt 或其他条目。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_source_fixture(
                Path(directory)
            )
            candidate_root = (
                fixture["campaign_dir"] / "candidates" / "candidate-target"
            )
            candidate_root.mkdir(parents=True)
            (candidate_root / "build-receipt.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            patches = (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=fixture["source_manifest"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(fixture["source_attempt_root"], fixture["attempt"]),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=fixture["reservation"],
                ),
            )
            with patches[0], patches[1], patches[2]:
                context = codex_upgrade._classification_candidate_reuse_source(
                    fixture["arguments"],
                    fixture["campaign_dir"],
                    fixture["manifest"],
                    {"status": "complete", "predecessor_import": None},
                    candidate_id="candidate-target",
                )
            self.assertIsNotNone(context)

            (candidate_root / "attempts").mkdir()
            with patches[0], patches[1], patches[2]:
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "已有 Candidate／attempt",
                ):
                    codex_upgrade._classification_candidate_reuse_source(
                        fixture["arguments"],
                        fixture["campaign_dir"],
                        fixture["manifest"],
                        {"status": "complete", "predecessor_import": None},
                        candidate_id="candidate-target",
                    )

    def test_classification_candidate_reuse_source_rejects_incomplete_job(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_source_fixture(
                Path(directory)
            )
            fixture["attempt"]["results"][0]["status"] = "failed"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=fixture["source_manifest"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(fixture["source_attempt_root"], fixture["attempt"]),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_reservation",
                    return_value=fixture["reservation"],
                ),
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "九项全部 complete",
                ):
                    codex_upgrade._classification_candidate_reuse_source(
                        fixture["arguments"],
                        fixture["campaign_dir"],
                        fixture["manifest"],
                        {"status": "complete", "predecessor_import": None},
                        candidate_id="candidate-target",
                    )

    def test_classification_candidate_reuse_source_rejects_non_direct_predecessor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_source_fixture(
                Path(directory)
            )
            fixture["manifest"]["predecessor"]["campaign_dir"] = str(
                (Path(directory) / "different-source").resolve()
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "不是当前 Campaign 的直接前序",
            ):
                codex_upgrade._classification_candidate_reuse_source(
                    fixture["arguments"],
                    fixture["campaign_dir"],
                    fixture["manifest"],
                    {"status": "complete", "predecessor_import": None},
                    candidate_id="candidate-target",
                )

    def test_classification_candidate_reuse_uses_attempt_tool_snapshot(
        self,
    ) -> None:
        """Campaign 旧快照漂移时，以实际产出 attempt 身份复用九项。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_preview_fixture(
                Path(directory),
                source_producer_sha="a" * 64,
            )
            with mock.patch.object(
                codex_upgrade,
                "_prior_complete_results",
                return_value=fixture["results"],
            ) as prior:
                preview, reused = (
                    codex_upgrade._build_classification_candidate_reuse_preview(
                        fixture["campaign_dir"],
                        fixture["manifest"],
                        candidate_id="candidate-target",
                        identity=fixture["identity"],
                        planned_jobs=fixture["jobs"],
                        current_tool=fixture["current_tool"],
                        context=fixture["context"],
                    )
                )
            self.assertEqual(preview["execute_job_ids"], [])
            self.assertEqual(preview["reused_job_ids"], sorted(
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
            ))
            self.assertEqual(preview["scanned_bytes"], 0)
            self.assertEqual(reused, fixture["results"])
            frozen = prior.call_args.kwargs["source_tool_identity"]
            self.assertEqual(
                frozen["files_sha256"],
                fixture["current_tool"]["files_sha256"],
            )

    def test_reused_result_rebase_uses_precomputed_incremental_metadata(
        self,
    ) -> None:
        """复用结果重绑不得重复解析同一个 Job 的工具依赖闭包。"""

        job = Job(
            job_id="candidate-core-direct",
            phase="candidate",
            suites=("full",),
            description="candidate-core-direct",
            steps=({"argv": ["true"], "environment": {}},),
            evidence_roots=("/tmp/candidate-core-direct",),
            covers=(),
        )
        incremental = {
            "components": ["producer"],
            "component_digests": {"producer": "1" * 64},
            "tool_dependency_files": {"runner.sh": "2" * 64},
            "dependency_sha256": "3" * 64,
            "input_sha256": "4" * 64,
            "environment_sha256": "5" * 64,
            "result_key": "6" * 64,
        }
        with mock.patch.object(
            codex_upgrade,
            "_job_incremental_metadata",
            side_effect=AssertionError("不得重复解析工具依赖"),
        ) as metadata_builder:
            rebased = codex_upgrade._rebase_reused_result(
                {"id": job.job_id, "status": "complete"},
                job,
                identity={"candidate_id": "candidate-a"},
                tool_identity={"files_sha256": "7" * 64},
                incremental_metadata=incremental,
            )
        metadata_builder.assert_not_called()
        self.assertEqual(rebased["incremental_result_key"], "6" * 64)
        self.assertEqual(rebased["tool_dependency_files"], {"runner.sh": "2" * 64})

    def test_classification_candidate_reuse_rejects_attempt_producer_drift(
        self,
    ) -> None:
        """attempt 中真实 producer 摘要变化时仍在预览阶段停线。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_preview_fixture(
                Path(directory),
                source_producer_sha="c" * 64,
            )
            with (
                mock.patch.object(codex_upgrade, "_prior_complete_results") as prior,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "产出工具漂移",
                ),
            ):
                codex_upgrade._build_classification_candidate_reuse_preview(
                    fixture["campaign_dir"],
                    fixture["manifest"],
                    candidate_id="candidate-target",
                    identity=fixture["identity"],
                    planned_jobs=fixture["jobs"],
                    current_tool=fixture["current_tool"],
                    context=fixture["context"],
                )
            prior.assert_not_called()

    def test_classification_candidate_reuse_reads_source_controls_historically(
        self,
    ) -> None:
        """显式跨 Campaign 预览不得要求历史 Ledger 仍是当前 head。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = self._classification_candidate_reuse_preview_fixture(
                Path(directory),
                source_producer_sha="a" * 64,
            )
            context = fixture["context"]
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    side_effect=[context["source_manifest"], fixture["manifest"]],
                ) as load_manifest,
                mock.patch.object(
                    codex_upgrade,
                    "_ordered_capture_attempts",
                    return_value=[],
                ) as ordered_attempts,
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "找不到同身份失败 attempt",
                ),
            ):
                codex_upgrade._prior_complete_results(
                    fixture["campaign_dir"],
                    Path("candidates/candidate-target"),
                    fixture["jobs"],
                    phase="candidate",
                    candidate_id="candidate-target",
                    identity=fixture["identity"],
                    tool_identity=fixture["current_tool"],
                    expected_reuse_job_ids=(
                        codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
                    ),
                    source_attempt_id="attempt-source",
                    source_campaign_dir=context["source_campaign_dir"],
                    source_candidate_id="candidate-source",
                    allowed_source_statuses=("awaiting_receipts",),
                    allow_unbound_cross_campaign_preview=True,
                    source_tool_identity=fixture["current_tool"],
                )
            source_call = load_manifest.call_args_list[0]
            self.assertEqual(
                Path(source_call.args[0]).resolve(),
                Path(context["source_campaign_dir"]).resolve(),
            )
            self.assertIs(source_call.kwargs["_control_epoch_bootstrap"], True)
            self.assertEqual(
                load_manifest.call_args_list[1],
                mock.call(fixture["campaign_dir"]),
            )
            self.assertIs(
                ordered_attempts.call_args.kwargs["_historical_manifest_controls"],
                True,
            )

    @staticmethod
    def _valid_classification_candidate_reuse_preview() -> dict[str, object]:
        job_ids = sorted(codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS)
        result_bindings = [
            {
                "id": job_id,
                "result_sha256": hashlib.sha256(job_id.encode()).hexdigest(),
                "evidence_roots": [f"/tmp/evidence/{job_id}"],
            }
            for job_id in job_ids
        ]
        evidence_roots = sorted(
            root
            for item in result_bindings
            for root in item["evidence_roots"]
        )
        preview: dict[str, object] = {
            "schema_version": (
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_PREVIEW_SCHEMA
            ),
            "status": "approval_required",
            "campaign_id": "campaign-current",
            "campaign_manifest_sha256": "1" * 64,
            "candidate_id": "candidate-a",
            "source_campaign": {
                "campaign_dir": "/tmp/source-campaign",
                "campaign_id": "campaign-source",
                "campaign_manifest_sha256": "2" * 64,
            },
            "source_candidate_id": "candidate-a",
            "source_attempt": {
                "path": "candidates/candidate-a/attempts/attempt-a/attempt.json",
                "sha256": "3" * 64,
                "bytes": 1,
                "attempt_id": "attempt-a",
                "attempt_digest": "4" * 64,
                "status": "awaiting_receipts",
            },
            "identity_sha256": "5" * 64,
            "current_tool_files_sha256": "6" * 64,
            "current_tool_production_sha256": "7" * 64,
            "planned_job_ids": job_ids,
            "execute_job_ids": [],
            "reused_job_ids": job_ids,
            "job_contract_sha256": "8" * 64,
            "source_results_sha256": "9" * 64,
            "result_bindings": result_bindings,
            "evidence_roots": evidence_roots,
            "evidence_roots_sha256": codex_upgrade._fingerprint(
                {"evidence_roots": evidence_roots}
            ),
            "reservation_exists": False,
            "live_request_count": 0,
            "scanned_bytes": 0,
        }
        preview["preview_sha256"] = codex_upgrade._fingerprint(preview)
        return preview

    @classmethod
    def _classification_candidate_reuse_source_fixture(
        cls,
        root: Path,
    ) -> dict[str, object]:
        campaign_dir = root / "campaign"
        source_dir = root / "source"
        campaign_dir.mkdir()
        source_attempt_root = (
            source_dir
            / "candidates"
            / "candidate-source"
            / "attempts"
            / "attempt-source"
        )
        source_attempt_root.mkdir(parents=True)
        source_attempt_path = source_attempt_root / "attempt.json"
        source_attempt_path.write_text("{}\n", encoding="utf-8")
        source_manifest_path = source_dir / "campaign.json"
        source_manifest_path.write_text("{}\n", encoding="utf-8")
        source_manifest = {"campaign_id": "campaign-source"}
        identity = {"runtime": "same"}
        job_ids = sorted(codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS)
        results = [
            {
                "id": job_id,
                "status": "complete",
                "evidence_roots": [f"/tmp/evidence/{job_id}"],
            }
            for job_id in job_ids
        ]
        attempt = {
            "status": "awaiting_receipts",
            "attempt_id": "attempt-source",
            "attempt_digest": "a" * 64,
            "identity": identity,
            "results": results,
        }
        reservation = {"planned_jobs": [{"id": job_id} for job_id in job_ids]}
        manifest = {
            "campaign_id": "campaign-current",
            "campaign_mode": "formal",
            "predecessor": {
                "campaign_dir": str(source_dir.resolve()),
                "campaign_id": "campaign-source",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    source_manifest_path
                ),
                "reason": "classification_fact_correction",
            },
        }
        arguments = argparse.Namespace(
            candidate_reuse_source_campaign_dir=source_dir.resolve(),
            candidate_reuse_source_candidate_id="candidate-source",
            candidate_reuse_source_attempt_id="attempt-source",
            approve_candidate_reuse_sha256=None,
            rerun_failed=True,
            preview_recovery=True,
        )
        return {
            "campaign_dir": campaign_dir,
            "source_dir": source_dir,
            "source_attempt_root": source_attempt_root,
            "source_manifest": source_manifest,
            "attempt": attempt,
            "reservation": reservation,
            "manifest": manifest,
            "arguments": arguments,
        }

    @staticmethod
    def _classification_candidate_reuse_preview_fixture(
        root: Path,
        *,
        source_producer_sha: str,
    ) -> dict[str, object]:
        """构造 Campaign 旧快照与 attempt 实际快照分离的零执行预览。"""

        def tool_identity(entries: list[dict[str, str]]) -> dict[str, object]:
            components = codex_upgrade._tool_component_identities(entries)
            return {
                "entry_count": len(entries),
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                "entries": entries,
                "components": components["components"],
                **codex_upgrade._tool_identity_sides(entries),
            }

        campaign_dir = root / "campaign"
        source_dir = root / "source-campaign"
        source_root = (
            source_dir
            / "candidates"
            / "candidate-source"
            / "attempts"
            / "attempt-source"
        )
        campaign_dir.mkdir()
        source_root.mkdir(parents=True)
        (campaign_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
        (source_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
        (source_root / "attempt.json").write_text("{}\n", encoding="utf-8")
        current_entries = [
            {"path": "capture.py", "sha256": "a" * 64},
            {"path": "codex_upgrade.py", "sha256": "b" * 64},
        ]
        current_tool = tool_identity(current_entries)
        source_entries = [
            {"path": "capture.py", "sha256": source_producer_sha},
            {"path": "codex_upgrade.py", "sha256": "b" * 64},
        ]
        source_components = codex_upgrade._tool_component_identities(
            source_entries
        )["components"]
        old_campaign_tool = tool_identity(
            [
                {"path": "capture.py", "sha256": "f" * 64},
                {"path": "codex_upgrade.py", "sha256": "b" * 64},
            ]
        )
        identity = {"runtime": "same"}
        jobs = [
            Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=job_id,
                steps=(),
                evidence_roots=(f"/tmp/evidence/{job_id}",),
                covers=(),
            )
            for job_id in sorted(
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
            )
        ]
        results = [
            {
                "id": job.job_id,
                "status": "complete",
                "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "evidence_roots": list(job.evidence_roots),
            }
            for job in jobs
        ]
        source_attempt = {
            "attempt_id": "attempt-source",
            "attempt_digest": "d" * 64,
            "status": "awaiting_receipts",
            "identity": identity,
            "tool_components": source_components,
            "results": results,
        }
        source_manifest = {
            "campaign_id": "campaign-source",
            "tool_identity": old_campaign_tool,
        }
        return {
            "campaign_dir": campaign_dir,
            "manifest": {
                "campaign_id": "campaign-current",
                "tool_identity": current_tool,
            },
            "identity": identity,
            "jobs": jobs,
            "results": results,
            "current_tool": current_tool,
            "context": {
                "source_campaign_dir": source_dir,
                "source_manifest": source_manifest,
                "source_candidate_id": "candidate-source",
                "source_attempt_id": "attempt-source",
                "source_root": source_root,
                "source_attempt": source_attempt,
                "identity": identity,
            },
        }

    @staticmethod
    def _classification_candidate_reuse_run_fixture(
        campaign_dir: Path,
        *,
        preview: bool,
    ) -> tuple[
        argparse.Namespace,
        dict[str, object],
        dict[str, object],
        list[Job],
        dict[str, object],
    ]:
        identity = {
            "git_commit": "f" * 40,
            "source_root": "/tmp/source",
            "source_tree_sha256": "a" * 64,
            "image_reference": f"candidate@sha256:{'b' * 64}",
            "image_digest": f"sha256:{'b' * 64}",
            "image_id": f"sha256:{'b' * 64}",
            "build_id": "build-a",
            "deployed_version": "0.1.1",
            "profile_id": "profile-a",
            "profile_digest": "c" * 64,
            "candidate_purpose": "production_replacement",
        }
        manifest: dict[str, object] = {
            "campaign_id": "campaign-current",
            "campaign_mode": "formal",
            "campaign_purpose": "production_replacement",
            "predecessor": {"reason": "classification_fact_correction"},
        }
        jobs = [
            Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=job_id,
                steps=(),
                evidence_roots=(f"/tmp/evidence/{job_id}",),
                covers=(),
            )
            for job_id in sorted(
                codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
            )
        ]
        source_root = campaign_dir.parent / "source-attempt"
        context: dict[str, object] = {
            "source_campaign_dir": campaign_dir.parent / "source-campaign",
            "source_manifest": {
                "campaign_id": "campaign-source",
                "tool_identity": {
                    "files_sha256": "e" * 64,
                    "entries": [
                        {"path": "capture.py", "sha256": "e" * 64}
                    ],
                },
            },
            "source_candidate_id": "candidate-a",
            "source_attempt_id": "attempt-a",
            "source_root": source_root,
            "source_attempt": {
                "status": "awaiting_receipts",
                "tool_components": codex_upgrade._tool_component_identities(
                    [{"path": "capture.py", "sha256": "d" * 64}]
                )["components"],
            },
            "identity": identity,
        }
        arguments = argparse.Namespace(
            campaign_dir=campaign_dir,
            candidate_id="candidate-a",
            runtime_image=identity["image_reference"],
            candidate_image_id=identity["image_id"],
            candidate_source=Path(identity["source_root"]),
            build_id=identity["build_id"],
            deployed_version=identity["deployed_version"],
            profile_id=identity["profile_id"],
            profile_digest=identity["profile_digest"],
            candidate_purpose=identity["candidate_purpose"],
            rerun_failed=True,
            preview_recovery=preview,
            approve_candidate_reuse_sha256=(None if preview else "3" * 64),
            acknowledge_live_requests=False,
            capture_root=Path("/root/oauth-capture"),
        )
        return arguments, manifest, identity, jobs, context

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_c0154_v7_source_attempt(
        self,
        predecessor_dir: Path,
        source: Mapping[str, object],
        campaign_manifest_sha256: str,
    ) -> tuple[dict[str, str], dict[str, object]]:
        """生成固定来源校验的最小自摘要 attempt 夹具。"""

        identity = {"candidate": source["candidate_id"]}
        attempt: dict[str, object] = {
            "schema_version": codex_upgrade.CAPTURE_ATTEMPT_SCHEMA,
            "campaign_id": source["campaign_id"],
            "campaign_manifest_sha256": campaign_manifest_sha256,
            "phase": "candidate",
            "candidate_id": source["candidate_id"],
            "attempt_id": source["attempt_id"],
            "status": "failed",
            "identity": identity,
        }
        attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
        attempt_path = (
            predecessor_dir
            / "candidates"
            / str(source["candidate_id"])
            / "attempts"
            / str(source["attempt_id"])
            / "attempt.json"
        )
        self._write_json(attempt_path, attempt)
        attempt_sha256 = codex_upgrade.file_sha256(attempt_path)
        identity_sha256 = codex_upgrade._fingerprint(identity)
        overrides = {
            "attempt_sha256": attempt_sha256,
            "attempt_digest": str(attempt["attempt_digest"]),
            "identity_sha256": identity_sha256,
        }
        abandoned: dict[str, object] = {
            "candidate_id": source["candidate_id"],
            "attempt_id": source["attempt_id"],
            "path": attempt_path.relative_to(predecessor_dir).as_posix(),
            "sha256": attempt_sha256,
            "attempt_digest": attempt["attempt_digest"],
            "identity_sha256": identity_sha256,
            "status": "failed",
        }
        return overrides, abandoned

    @staticmethod
    def _write_state_snapshot(path: Path, state: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(candidate_evidence_guard.normalize_state(state))
        path.chmod(0o600)

    @staticmethod
    def _binding(path: Path, logical_path: str) -> dict[str, str]:
        return {
            "path": logical_path,
            "sha256": codex_upgrade.file_sha256(path),
        }

    @staticmethod
    def _environment_binding(evidence_root: Path, path: Path) -> dict[str, object]:
        """attempt.environment 用的 evidence_root 相对绑定（path/sha256/bytes）。"""

        return {
            "path": path.relative_to(evidence_root).as_posix(),
            "sha256": codex_upgrade.file_sha256(path),
            "bytes": path.stat().st_size,
        }

    @staticmethod
    def _make_private_tree(root: Path) -> None:
        for path in sorted(root.rglob("*")):
            path.chmod(0o700 if path.is_dir() else 0o600)
        root.chmod(0o700)

    @staticmethod
    def _database_state(*, after: bool) -> dict[str, object]:
        protected_tables = []
        for name, columns in sorted(
            codex_upgrade_receipt_finalizer.DATABASE_PROTECTED_TABLES.items()
        ):
            primary_key = hashlib.sha256(f"{name}:1".encode()).hexdigest()
            protected_tables.append(
                {
                    "exists": True,
                    "name": name,
                    "primary_key_columns": list(columns),
                    "primary_key_fingerprints": [primary_key],
                    "row_count": 1,
                }
            )
        append_only_watermarks = [
            {
                "exists": True,
                "max_id": 2 if after else 1,
                "name": name,
                "row_count": 2 if after else 1,
            }
            for name in sorted(
                codex_upgrade_receipt_finalizer.DATABASE_WATERMARK_TABLES
            )
        ]
        return {
            "append_only_watermarks": append_only_watermarks,
            "comparison_policy": (
                codex_upgrade_receipt_finalizer.DATABASE_COMPARISON_POLICY
            ),
            "probe_kind": "database",
            "protected_tables": protected_tables,
        }

    @staticmethod
    def _attached_campaign_lease(
        campaign_dir: Path,
        *,
        campaign_id: str,
        deadline_at_epoch: float,
    ) -> tuple[mock.Mock, codex_upgrade.CampaignLease]:
        """构造只附加父监督器、不创建嵌套 monitor 的租约夹具。"""

        started_monotonic_ns = codex_upgrade.time.monotonic_ns()
        remaining_seconds = max(1.0, deadline_at_epoch - codex_upgrade.time.time())
        attached = mock.Mock()
        attached.campaign_id = campaign_id
        attached.phase = "official"
        attached.owner_pid = 4321
        attached.owner_nonce = "a" * 64
        attached.deadline_at_epoch = deadline_at_epoch
        attached.heartbeat_seconds = 0.05
        attached.run_dir = campaign_dir / ".supervisor" / "run-parent"
        attached._started_monotonic_ns = started_monotonic_ns
        attached._deadline_monotonic_ns = started_monotonic_ns + int(
            remaining_seconds * 1_000_000_000
        )
        lease = codex_upgrade.CampaignLease(
            campaign_dir,
            phase="official",
            candidate_id=None,
            deadline=codex_upgrade.incremental_recovery.WallClockDeadline(120),
            command="capture-official",
            campaign_id=campaign_id,
        )
        return attached, lease

    def test_parent_supervisor_deadline_is_bound_into_attempt_reservation(self) -> None:
        """父监督器绝对 deadline 必须穿过附加租约进入预约收据。"""

        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_id = "reservation-parent"
            manifest = {
                "campaign_id": campaign_id,
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
            }
            self._write_json(campaign_dir / "campaign.json", manifest)
            deadline_at_epoch = codex_upgrade.time.time() + 60
            attached, lease = self._attached_campaign_lease(
                campaign_dir,
                campaign_id=campaign_id,
                deadline_at_epoch=deadline_at_epoch,
            )
            job = Job(
                job_id="official-parent-deadline",
                phase="official",
                suites=("full",),
                description="父监督器 deadline 绑定测试",
                steps=(),
                evidence_roots=(str(Path(temporary) / "evidence"),),
                covers=(),
            )

            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor.SupervisorClient,
                    "attach_from_environment",
                    return_value=attached,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                lease,
            ):
                expected_deadline = (
                    codex_upgrade.codex_upgrade_supervisor._epoch_to_utc(
                        deadline_at_epoch
                    )
                )
                self.assertEqual(
                    lease.payload["deadline_at_utc"],
                    expected_deadline,
                )
                _attempt_dir, reservation = codex_upgrade._reserve_capture_attempt(
                    campaign_dir,
                    phase="official",
                    candidate_id=None,
                    identity={"profile_id": "official-parent"},
                    jobs=[job],
                    lease=lease,
                )

            self.assertEqual(
                reservation["campaign_lease"]["deadline_at_utc"],
                expected_deadline,
            )
            self.assertEqual(
                reservation["campaign_lease"]["owner_nonce"],
                attached.owner_nonce,
            )

    def test_attempt_reservation_rejects_missing_lease_deadline_before_mutation(self) -> None:
        """deadline 缺失时安全失败，且不得创建 attempts 目录。"""

        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_id = "reservation-missing-deadline"
            manifest = {
                "campaign_id": campaign_id,
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
            }
            self._write_json(campaign_dir / "campaign.json", manifest)
            attached, lease = self._attached_campaign_lease(
                campaign_dir,
                campaign_id=campaign_id,
                deadline_at_epoch=codex_upgrade.time.time() + 60,
            )
            job = Job(
                job_id="official-missing-deadline",
                phase="official",
                suites=("full",),
                description="缺失 deadline 安全失败测试",
                steps=(),
                evidence_roots=(str(Path(temporary) / "evidence"),),
                covers=(),
            )

            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor.SupervisorClient,
                    "attach_from_environment",
                    return_value=attached,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                lease,
            ):
                self.assertIsNotNone(lease._payload)
                lease._payload.pop("deadline_at_utc")  # type: ignore[union-attr]
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "deadline_at_utc",
                ):
                    codex_upgrade._reserve_capture_attempt(
                        campaign_dir,
                        phase="official",
                        candidate_id=None,
                        identity={"profile_id": "official-parent"},
                        jobs=[job],
                        lease=lease,
                    )

            self.assertFalse((campaign_dir / "official" / "attempts").exists())

    def test_attempt_reservation_is_created_below_phase_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign_dir = Path(temporary) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            self._write_json(campaign_dir / "campaign.json", {})
            manifest = {
                "campaign_id": "reservation-test",
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
            }
            job = Job(
                job_id="candidate-test",
                phase="candidate",
                suites=("full",),
                description="原子预约测试",
                steps=(),
                evidence_roots=(str(Path(temporary) / "evidence"),),
                covers=(),
            )

            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value=manifest,
            ):
                attempt_dir, reservation = codex_upgrade._reserve_capture_attempt(
                    campaign_dir,
                    phase="candidate",
                    candidate_id="candidate-a",
                    identity={
                        "profile_id": "profile-a",
                        "candidate_purpose": "validation_only",
                    },
                    jobs=[job],
                )

            self.assertTrue(attempt_dir.is_dir())
            self.assertTrue(
                attempt_dir.is_relative_to(
                    campaign_dir / "candidates" / "candidate-a" / "attempts"
                )
            )
            self.assertEqual(attempt_dir.stat().st_mode & 0o777, 0o700)
            self.assertTrue((attempt_dir / "reservation.json").is_file())
            self.assertRegex(reservation["run_nonce"], r"^[0-9a-f]{64}$")

    def _write_scenario_manifest(
        self,
        root: Path,
        rule_manifest: Path,
        rules: tuple[str, ...],
        *,
        version: str,
        name: str,
        historical_source_binding: bool = False,
    ) -> Path:
        spec_path = Path(__file__).resolve().parents[3] / "docs" / (
            "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
        )
        source_spec_sha256 = codex_upgrade.source_spec_section_sha256(
            spec_path, "第二章"
        )
        if historical_source_binding:
            frozen_profile = Path(__file__).resolve().parents[1] / (
                "candidate_rule_expectations_"
                f"{version.replace('.', '_')}.json"
            )
            source_spec_sha256 = json.loads(
                frozen_profile.read_text(encoding="utf-8")
            )["source_spec_sha256"]
        scenario_manifest = root / name
        self._write_json(
            scenario_manifest,
            {
                "schema_version": codex_upgrade.SCENARIO_SCHEMA,
                "codex_version": version,
                "profile_id": (
                    "codex-0.147.0-test-v1"
                    if version == "0.147.0"
                    else (
                        "codex-0.145.0-upgrade-v1"
                        if version == "0.145.0"
                        else f"codex-{version}-test-v1"
                    )
                ),
                "source_spec": {
                    "path": "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
                    "fragment": "第二章",
                    "sha256": source_spec_sha256,
                },
                "rule_manifest": {
                    "path": str(
                        rule_manifest.resolve().relative_to(
                            Path(__file__).resolve().parents[3]
                        )
                    )
                    if rule_manifest.resolve().is_relative_to(
                        Path(__file__).resolve().parents[3]
                    )
                    else rule_manifest.name,
                    "sha256": codex_upgrade.file_sha256(rule_manifest),
                    "rule_count": len(rules),
                },
                "required_client_bindings": [
                    "kilo-compatible",
                    "kilo-responses",
                ],
                "variable_contract": [
                    {
                        "name": "campaign_dir",
                        "type": "absolute_path",
                        "required": True,
                        "sensitive": False,
                        "description": "测试 Campaign 根目录。",
                    },
                    {
                        "name": "target_version",
                        "type": "string",
                        "required": True,
                        "sensitive": False,
                        "description": "测试 Campaign 目标版本。",
                    }
                ],
                "evidence_scenarios": [
                    {
                        "scenario_id": "A01",
                        "description": "测试规则全集的双侧证据场景",
                        "trigger": "对官方 CLI 与候选服务执行同一组离线夹具",
                        "preconditions": ["测试证据根已创建"],
                        "required_artifact_kinds": ["process_trace"],
                        "covers": list(rules),
                    }
                ],
                "capture_jobs": [
                    {
                        "id": self.synthetic_job_ids["official"],
                        "phase": "official",
                        "suites": ["full"],
                        "description": "测试官方抓包阶段",
                        "required": True,
                        "steps": [
                            {
                                "argv": ["true"],
                                "environment": {},
                                "timeout_seconds": 60,
                            }
                        ],
                        "evidence_roots": [
                            "{campaign_dir}/official-evidence"
                        ],
                        "covers": list(rules),
                        "scenario_ids": ["A01"],
                        "required_scenario_receipts": [],
                    },
                    {
                        "id": self.synthetic_job_ids["candidate"],
                        "phase": "candidate",
                        "suites": ["full"],
                        "description": "测试候选抓包阶段",
                        "required": True,
                        "steps": [
                            {
                                "argv": ["true"],
                                "environment": {
                                    "CODEX_VERSION": "{target_version}",
                                },
                                "timeout_seconds": 60,
                            }
                        ],
                        "evidence_roots": [
                            "{campaign_dir}/candidate-evidence"
                        ],
                        "covers": list(rules),
                        "scenario_ids": ["A01"],
                        "required_scenario_receipts": [],
                    },
                ],
            },
        )
        return scenario_manifest

    def _add_runtime_codex_jobs(
        self,
        payload: dict[str, object],
        *,
        bind_codex_bin: bool,
    ) -> dict[str, object]:
        """为后继场景测试补入固定五个 Candidate 客户端 Job。"""

        updated = json.loads(json.dumps(payload))
        if not any(
            item["name"] == "capture_codex_bin"
            for item in updated["variable_contract"]
        ):
            updated["variable_contract"].append(
                {
                    "name": "capture_codex_bin",
                    "type": "absolute_path",
                    "required": True,
                    "sensitive": False,
                    "description": "测试 Candidate 固定 Codex 二进制。",
                }
            )
        template = next(
            job
            for job in updated["capture_jobs"]
            if job["id"] == "candidate-test"
        )
        for job_id in sorted(codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS):
            job = json.loads(json.dumps(template))
            job["id"] = job_id
            job["evidence_roots"] = [
                "{campaign_dir}/candidate-evidence/" + job_id
            ]
            if bind_codex_bin:
                job["steps"][0]["environment"]["CODEX_BIN"] = (
                    "{capture_codex_bin}"
                )
            updated["capture_jobs"].append(job)
        return updated

    def test_new_campaign_requires_current_p0_producer_and_target_probe_version(self) -> None:
        """R15：新建或承接 Campaign 的 P0 必须由当前 producer 采集，Rust TLS 探针使用本轮目标版本。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "p0-receipt.json").write_text("{}\n", encoding="utf-8")
            arguments = argparse.Namespace(arm64_environment_root=root, arm64_environment_receipt=root / "p0-receipt.json",
                                           target_version="0.156.1")
            current = {"producer": {"version": codex_upgrade.codex_upgrade_arm64_environment_receipt.PRODUCER_VERSION},
                       "rust_tls_probe": {"binary": "/opt/codex-0.156.1/bin/codex", "codex_version": "0.156.1"}}
            cases = (
                (current, None),
                ({**current, "producer": {"version": "7"}}, "当前环境 producer"),
                ({**current, "rust_tls_probe": {"binary": "/opt/codex-0.154.0/bin/codex", "codex_version": "0.154.0"}},
                 "Rust TLS 探针版本"),
                ({"producer": current["producer"]}, "Rust TLS 探针版本"),
            )
            for receipt, message in cases:
                with self.subTest(message=message), mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt, "replay", return_value=receipt,
                ):
                    if message is None:
                        codex_upgrade._require_current_p0_environment(arguments)
                    else:
                        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, message):
                            codex_upgrade._require_current_p0_environment(arguments)

    def test_plan_rejects_p0_probe_for_other_client_version(self) -> None:
        """P0 探针用了非本轮目标版本的客户端时，建 Campaign 在写入任何产物前拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = self._campaign_arguments(root, campaign_mode="preflight_only")
            mismatched = create_arm_receipt(root / "control" / "arm64-p0-other", phase="p0",
                                            subject_id=arguments.campaign_id, prefix="p0",
                                            rust_tls_codex_version="0.145.0")
            arguments.arm64_environment_root = mismatched.parent
            arguments.arm64_environment_receipt = mismatched
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "Rust TLS 探针版本"):
                codex_upgrade.create_campaign(arguments)
            self.assertFalse((arguments.campaign_dir / "campaign.json").exists())

    def _campaign_arguments(
        self,
        root: Path,
        *,
        campaign_id: str = "upgrade-0146-test",
        campaign_mode: str = "formal",
        campaign_purpose: str = "validation_only",
        baseline_version: str = "0.145.0",
        target_version: str = "0.147.0",
        model: str = "gpt-5.4",
        lite_model: str = "gpt-5.6-luna",
    ) -> argparse.Namespace:
        baseline_source = root / "baseline-source"
        target_source = root / "target-source"
        baseline_evidence = root / "baseline-evidence"
        for directory in (
            baseline_source,
            target_source,
            baseline_evidence,
        ):
            directory.mkdir(parents=True)
        for source in (baseline_source, target_source):
            (source / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.12.28"\n',
                encoding="utf-8",
            )
        self._write_json(
            baseline_evidence / "surface.json",
            {
                "records": [
                    {
                        "request": {
                            "method": "POST",
                            "path": "/backend-api/codex/responses",
                            "http_version": "HTTP/1.1",
                            "headers": [
                                ["version", baseline_version],
                                ["host", "chatgpt.com"],
                            ],
                            "json_shape": {"model": "<string>", "input": []},
                        }
                    }
                ]
            },
        )
        baseline_rule_manifest = (
            Path(__file__).resolve().parents[1]
            / f"codex_upgrade_rules_{baseline_version.replace('.', '_')}.json"
        )
        required_rules = list(
            load_rule_manifest(baseline_rule_manifest, baseline_version)
        )
        scenario_manifest = self._write_scenario_manifest(
            root,
            baseline_rule_manifest,
            tuple(required_rules),
            version=baseline_version,
            name="scenarios.json",
            historical_source_binding=True,
        )
        target_rule_manifest = root / "target-rules.json"
        self._write_json(
            target_rule_manifest,
            {
                "schema_version": codex_upgrade.RULE_SCHEMA,
                "codex_version": target_version,
                "required_rules": required_rules,
            },
        )
        target_scenario_manifest = self._write_scenario_manifest(
            root,
            target_rule_manifest,
            tuple(required_rules),
            version=target_version,
            name="target-scenarios.json",
        )
        package_path = root / "codex-package-x86_64-unknown-linux-musl.tar.gz"
        binary_bytes = b"codex-cli-test-binary"
        code_mode_host_bytes = b"codex-code-mode-host-test-binary"
        package_metadata = json.dumps(
            {
                "layoutVersion": 1,
                "version": target_version,
                "target": "x86_64-unknown-linux-musl",
                "variant": "codex",
                "entrypoint": "bin/codex",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        # 恢复 preflight 必须与前序 Campaign 得到相同的合成包摘要；
        # 固定 gzip 时间，避免慢速 ARM64 跨秒执行时产生不同字节。
        with package_path.open("wb") as package_file:
            with gzip.GzipFile(
                fileobj=package_file,
                mode="wb",
                filename="",
                mtime=0,
            ) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for name, content, mode in (
                        ("codex-package.json", package_metadata, 0o644),
                        ("bin/codex", binary_bytes, 0o755),
                        (
                            "bin/codex-code-mode-host",
                            code_mode_host_bytes,
                            0o755,
                        ),
                    ):
                        member = tarfile.TarInfo(name)
                        member.size = len(content)
                        member.mode = mode
                        member.mtime = 0
                        archive.addfile(member, io.BytesIO(content))
        timing_root = root / "control" / "UpgradeTimingLedger"
        timing_receipt = create_timing_checkpoint(
            timing_root,
            upgrade_id=campaign_id,
            baseline_version=baseline_version,
            target_version=target_version,
            campaign_purpose=campaign_purpose,
        )
        arm_root = root / "control" / "arm64-p0"
        arm_receipt = create_arm_receipt(
            arm_root,
            phase="p0",
            subject_id=campaign_id,
            prefix="p0",
            # P0 由当前 producer 采集，Rust TLS 探针使用本轮目标版本。
            rust_tls_codex_version=target_version,
        )
        runtime_image = f"capture-runtime@sha256:{'b' * 64}"
        target_sha256 = hashlib.sha256(binary_bytes).hexdigest()
        target_package_sha256 = codex_upgrade.file_sha256(package_path)
        target_code_mode_host_sha256 = hashlib.sha256(
            code_mode_host_bytes
        ).hexdigest()
        live_compose_dir = ""
        live_compose_files = ""
        if campaign_purpose == "production_replacement":
            compose_dir = root / "compose"
            compose_dir.mkdir(mode=0o700)
            compose_file = compose_dir / "docker-compose.yml"
            compose_file.write_text("services: {}\n", encoding="utf-8")
            compose_file.chmod(0o600)
            live_compose_dir = str(compose_dir.resolve())
            live_compose_files = str(compose_file.resolve())
        rehearsal_root: Path | None = None
        rehearsal_receipt: Path | None = None
        p0_gate_root = None
        release_certification = None
        p0_gate_receipt: Path | None = None
        if campaign_mode == "formal":
            # 旧版本只用于离线合成 Campaign；目标标签声明门禁由 0.151
            # 专项测试覆盖，不能为即将退休的 0.147 新增生产声明。
            original_declaration = codex_upgrade_job_rehearsal_receipt._target_evidence_label_declaration_sha256

            def declaration_or_fixed(target_version_arg: str, target_scenario_arg: object, **kwargs: object) -> str:
                try:
                    return original_declaration(target_version_arg, target_scenario_arg, **kwargs)
                except codex_upgrade_job_rehearsal_receipt.JobRehearsalReceiptError:
                    return "d" * 64

            with mock.patch.object(
                codex_upgrade_job_rehearsal_receipt,
                "_target_evidence_label_declaration_sha256",
                side_effect=declaration_or_fixed,
            ):
                contract = (
                    codex_upgrade_job_rehearsal_receipt.build_execution_contract(
                        target_version=target_version,
                        target_sha256=target_sha256,
                        target_package_sha256=target_package_sha256,
                        target_code_mode_host_sha256=target_code_mode_host_sha256,
                        suite="full",
                        tool_files_sha256=codex_upgrade._tool_identity()[
                            "files_sha256"
                        ],
                        wire_producer_sha256=codex_upgrade._tool_identity().get(
                            "wire_producer_sha256"
                        ),
                        policy_sha256=codex_upgrade._tool_identity().get(
                            "policy_sha256"
                        ),
                        configuration={
                            "runtime_image": runtime_image,
                            "model": model,
                            "lite_model": lite_model,
                            "capture_root": "/root/oauth-capture",
                            "capture_container": "capture-cli",
                            "service_container": "sub2apiplus",
                            "keeper_container": "sub2apiplus-keeper",
                            "postgres_container": "sub2apiplus-postgres",
                            "redis_container": "sub2apiplus-redis",
                            "capture_codex_bin": f"/opt/codex-{target_version}/bin/codex",
                            "relay_codex_bin": f"/opt/codex-{target_version}/bin/codex",
                            "capture_code_mode_host_bin": (
                                f"/opt/codex-{target_version}/bin/codex-code-mode-host"
                            ),
                            "relay_code_mode_host_bin": (
                                f"/opt/codex-{target_version}/bin/codex-code-mode-host"
                            ),
                            "codex_account_id": 90,
                            "api_key_id": 1,
                            "live_attestation_compose_dir": live_compose_dir,
                            "live_attestation_compose_files": live_compose_files,
                        },
                        target_scenario=json.loads(
                            target_scenario_manifest.read_text(encoding="utf-8")
                        ),
                        extra_jobs=None,
                    )
                )
            rehearsal_root = root / "control" / "job-rehearsal"
            rehearsal_receipt = create_job_rehearsal_receipt(
                rehearsal_root,
                contract=contract,
                preflight_campaign_id="preflight-fixture",
            )
            if codex_upgrade._requires_complete_vc_artifacts(target_version):
                release_certification = create_release_certification(
                    root / "control" / "release-certification",
                    job_rehearsal_root=rehearsal_root,
                    job_rehearsal_receipt=rehearsal_receipt,
                )
                p0_gate_root = root / "control" / "p0-gate"
                p0_gate_receipt = create_p0_gate_receipt(
                    p0_gate_root,
                    upgrade_id=campaign_id,
                    baseline_version=baseline_version,
                    target_version=target_version,
                    campaign_purpose=campaign_purpose,
                    release_certification=release_certification,
                )
        return argparse.Namespace(
            command="plan",
            campaign_dir=root / "campaign",
            output=None,
            dry_run=False,
            execute=False,
            acknowledge_live_requests=False,
            baseline_version=baseline_version,
            target_version=target_version,
            campaign_mode=campaign_mode,
            campaign_purpose=campaign_purpose,
            timing_ledger_dir=timing_root,
            timing_receipt=timing_receipt,
            arm64_environment_root=arm_root,
            arm64_environment_receipt=arm_receipt,
            job_rehearsal_root=rehearsal_root,
            job_rehearsal_receipt=rehearsal_receipt,
            p0_gate_root=p0_gate_root,
            p0_gate_receipt=p0_gate_receipt,
            release_certification=release_certification,
            baseline_source=baseline_source,
            target_source=target_source,
            baseline_evidence=baseline_evidence,
            target_sha256=target_sha256,
            target_package=package_path,
            target_package_sha256=target_package_sha256,
            target_code_mode_host_sha256=target_code_mode_host_sha256,
            runtime_image=runtime_image,
            rule_manifest=baseline_rule_manifest,
            scenario_manifest=scenario_manifest,
            target_scenario_manifest=target_scenario_manifest,
            extra_jobs=None,
            suite="full",
            campaign_id=campaign_id,
            model=model,
            lite_model=lite_model,
            capture_root=Path("/root/oauth-capture"),
            capture_container="capture-cli",
            service_container="sub2apiplus",
            keeper_container="sub2apiplus-keeper",
            postgres_container="sub2apiplus-postgres",
            redis_container="sub2apiplus-redis",
            capture_codex_bin=f"/opt/codex-{target_version}/bin/codex",
            relay_codex_bin=f"/opt/codex-{target_version}/bin/codex",
            capture_code_mode_host_bin=(
                f"/opt/codex-{target_version}/bin/codex-code-mode-host"
            ),
            relay_code_mode_host_bin=(
                f"/opt/codex-{target_version}/bin/codex-code-mode-host"
            ),
            codex_account_id=90,
            api_key_id=1,
            live_attestation_compose_dir=live_compose_dir,
            live_attestation_compose_files=live_compose_files,
            candidate_id=None,
            candidate_purpose=None,
            profile_id=None,
            profile_digest=None,
            target_rule_manifest=None,
            migration_manifest=None,
            assertion_profile_manifest=None,
            approve_manifest_sha256=None,
            assertions=None,
        )

    def _create_campaign(
        self,
        root: Path,
        *,
        campaign_mode: str = "formal",
        campaign_purpose: str = "validation_only",
    ) -> tuple[Path, dict[str, object]]:
        arguments = self._campaign_arguments(
            root,
            campaign_mode=campaign_mode,
            campaign_purpose=campaign_purpose,
        )
        codex_upgrade.create_campaign(arguments)
        campaign_dir = arguments.campaign_dir
        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        return campaign_dir, manifest

    def _seal_official_stage(
        self,
        root: Path,
        campaign_dir: Path,
        manifest: dict[str, object],
        *,
        include_new_surface: bool = False,
        evaluation_transition_identity: dict[str, object] | None = None,
        evaluation_recovery_controls: dict[str, object] | None = None,
        seal: bool = True,
        bind_environment: bool = False,
    ) -> Path:
        evidence_root = root / "official-evidence"
        self._write_capture_stage(
            campaign_dir,
            evidence_root,
            phase="official",
            identity=manifest["official_identity"],
            include_new_surface=include_new_surface,
            evaluation_transition_identity=evaluation_transition_identity,
            evaluation_recovery_controls=evaluation_recovery_controls,
            seal=seal,
            bind_environment=bind_environment,
        )
        return evidence_root

    def _synthesize_candidate_receipts(
        self,
        evidence_root: Path,
        *,
        campaign_manifest: dict[str, object],
        attempt_id: str,
        run_nonce: str,
        attempt_started_at_utc: str,
        client_checkpoint_at_utc: str,
        identity: dict[str, object],
        candidate_id: str,
        target_version: str,
        third_party_model: str,
        timestamp: Callable[[int], str],
        receipts_subdir: str = "",
    ) -> tuple[Path, list[str], Path]:
        """合成候选侧机器收据（运行画像审计→两份 Kilo 五件套→observed-profile／Kilo finalizer→Kilo 后恢复报告→client-after 探针），
        全部落在 ``evidence_root``（``receipts_subdir`` 非空时落在其子目录，finalizer 的 producer.evidence_root 仍是
        ``evidence_root``）；返回 (observed_profile_path, client_receipts, post_client_restoration_report)。
        M2 的恢复段增量封存用例复用本方法把收据合成到恢复段证据根的 client/ 子树（v3 权限收口只允许该派生路径新增）。"""

        self.assertIsNotNone(candidate_id)
        receipts_root = evidence_root / receipts_subdir if receipts_subdir else evidence_root
        receipts_root.mkdir(parents=True, exist_ok=True)

        def relative(path: Path) -> Path:
            return path.relative_to(evidence_root)

        observed_runtime_path = (
            receipts_root / "observed-profile-runtime-audit.json"
        )
        self._write_json(
            observed_runtime_path,
            {
                "schema_version": (
                    codex_upgrade_receipt_finalizer.RUNTIME_AUDIT_SCHEMA
                ),
                "source": "sub2api-runtime",
                "event_type": "profile_activated",
                "event_id": "profile-event-1",
                "campaign_id": campaign_manifest["campaign_id"],
                "attempt_id": attempt_id,
                "run_nonce": run_nonce,
                "candidate_id": candidate_id,
                "target_version": target_version,
                "profile_id": identity["profile_id"],
                "profile_digest": identity["profile_digest"],
                "image_id": identity["image_id"],
                "image_reference": identity["image_reference"],
                "source_tree_sha256": identity["source_tree_sha256"],
                "build_id": identity["build_id"],
                "deployed_version": identity["deployed_version"],
                "observed_at_utc": timestamp(10),
            },
        )
        observed_profile_path = receipts_root / "observed-profile.json"
        client_receipts: list[str] = []
        kilo_arguments: list[argparse.Namespace] = []
        clients = (
            (
                "kilo-compatible",
                "openai-compatible",
                "/v1/chat/completions",
            ),
            (
                "kilo-responses",
                "openai-responses",
                "/v1/responses",
            ),
        )
        for client, protocol, entrypoint in clients:
            installation_id = f"installation-{client}"
            request_id = f"request-{client}"
            response_id = f"response-{client}"
            ingress_witness_id = f"ingress-{client}"
            transport = (
                "http" if client == "kilo-compatible" else "websocket"
            )
            installation_path = (
                receipts_root / f"{client}-installation.json"
            )
            ingress_path = receipts_root / f"{client}-ingress.json"
            runtime_path = receipts_root / f"{client}-runtime-audit.json"
            response_path = (
                receipts_root / f"{client}-response-witness.json"
            )
            usage_path = receipts_root / f"{client}-usage-audit.json"
            self._write_json(
                installation_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.KILO_INSTALLATION_SCHEMA
                    ),
                    "source": "kilo-installation",
                    "installation_id": installation_id,
                    "product_id": "kilo",
                    "display_name": "Kilo Code",
                    "client_version": "kilo-test-1",
                    "executable_path": (
                        "/Applications/Kilo Code.app/Contents/MacOS/Kilo"
                    ),
                    "executable_sha256": hashlib.sha256(
                        client.encode()
                    ).hexdigest(),
                    "observed_at_utc": timestamp(-3600),
                },
            )
            self._write_json(
                ingress_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.KILO_INGRESS_SCHEMA
                    ),
                    "source": "kilo-ingress",
                    "witness_id": ingress_witness_id,
                    "request_id": request_id,
                    "campaign_id": campaign_manifest["campaign_id"],
                    "attempt_id": attempt_id,
                    "run_nonce": run_nonce,
                    "installation_id": installation_id,
                    "client_id": client,
                    "client_version": "kilo-test-1",
                    "protocol": protocol,
                    "entrypoint": entrypoint,
                    "model": third_party_model,
                    "candidate_id": candidate_id,
                    "target_version": target_version,
                    "received_at_utc": timestamp(20),
                },
            )
            self._write_json(
                runtime_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.RUNTIME_AUDIT_SCHEMA
                    ),
                    "source": "sub2api-runtime",
                    "event_type": "oauth_request_forwarded",
                    "event_id": f"runtime-{client}",
                    "request_id": request_id,
                    "campaign_id": campaign_manifest["campaign_id"],
                    "attempt_id": attempt_id,
                    "run_nonce": run_nonce,
                    "ingress_witness_id": ingress_witness_id,
                    "installation_id": installation_id,
                    "client_id": client,
                    "protocol": protocol,
                    "entrypoint": entrypoint,
                    "model": third_party_model,
                    "candidate_id": candidate_id,
                    "target_version": target_version,
                    "profile_id": identity["profile_id"],
                    "profile_digest": identity["profile_digest"],
                    "image_id": identity["image_id"],
                    "source_tree_sha256": identity["source_tree_sha256"],
                    "build_id": identity["build_id"],
                    "deployed_version": identity["deployed_version"],
                    "auth_mode": "oauth",
                    "oauth_account_id": 90,
                    "upstream_endpoint": "/backend-api/codex/responses",
                    "transport": transport,
                    "affected_branches": [transport],
                    "observed_at_utc": timestamp(30),
                },
            )
            self._write_json(
                response_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.KILO_RESPONSE_SCHEMA
                    ),
                    "source": "kilo-response",
                    "witness_id": f"response-witness-{client}",
                    "request_id": request_id,
                    "campaign_id": campaign_manifest["campaign_id"],
                    "attempt_id": attempt_id,
                    "run_nonce": run_nonce,
                    "installation_id": installation_id,
                    "client_id": client,
                    "candidate_id": candidate_id,
                    "http_status": 200,
                    "response_id": response_id,
                    "completed_at_utc": timestamp(40),
                },
            )
            self._write_json(
                usage_path,
                {
                    "schema_version": (
                        codex_upgrade_receipt_finalizer.USAGE_AUDIT_SCHEMA
                    ),
                    "source": "sub2api-usage",
                    "event_id": f"usage-event-{client}",
                    "request_id": request_id,
                    "campaign_id": campaign_manifest["campaign_id"],
                    "attempt_id": attempt_id,
                    "run_nonce": run_nonce,
                    "response_id": response_id,
                    "candidate_id": candidate_id,
                    "usage_id": f"usage-{client}",
                    "oauth_account_id": 90,
                    "recorded_at_utc": timestamp(50),
                },
            )
            receipt_path = receipts_root / f"{client}.json"
            kilo_arguments.append(
                argparse.Namespace(
                    evidence_root=evidence_root,
                    output=relative(receipt_path),
                    campaign_id=campaign_manifest["campaign_id"],
                    attempt_id=attempt_id,
                    run_nonce=run_nonce,
                    attempt_started_at_utc=attempt_started_at_utc,
                    client_checkpoint_at_utc=client_checkpoint_at_utc,
                    client_id=client,
                    candidate_id=candidate_id,
                    target_version=target_version,
                    profile_id=identity["profile_id"],
                    profile_digest=identity["profile_digest"],
                    candidate_image_id=identity["image_id"],
                    source_tree_sha256=identity["source_tree_sha256"],
                    build_id=identity["build_id"],
                    deployed_version=identity["deployed_version"],
                    model=third_party_model,
                    installation=relative(installation_path),
                    ingress=relative(ingress_path),
                    runtime_audit=relative(runtime_path),
                    response_witness=relative(response_path),
                    usage_audit=relative(usage_path),
                )
            )
            client_receipts.append(f"{client}={receipt_path}")

        self._make_private_tree(receipts_root)
        codex_upgrade_receipt_finalizer.finalize_observed_profile(
            argparse.Namespace(
                evidence_root=evidence_root,
                output=relative(observed_profile_path),
                campaign_id=campaign_manifest["campaign_id"],
                attempt_id=attempt_id,
                run_nonce=run_nonce,
                attempt_started_at_utc=attempt_started_at_utc,
                client_checkpoint_at_utc=client_checkpoint_at_utc,
                candidate_id=candidate_id,
                target_version=target_version,
                profile_id=identity["profile_id"],
                profile_digest=identity["profile_digest"],
                image_id=identity["image_id"],
                image_reference=identity["image_reference"],
                source_tree_sha256=identity["source_tree_sha256"],
                build_id=identity["build_id"],
                deployed_version=identity["deployed_version"],
                runtime_audit=relative(observed_runtime_path),
            )
        )
        for arguments in kilo_arguments:
            codex_upgrade_receipt_finalizer.finalize_kilo_binding(arguments)
        # v3 权限收口只放行 receipts/client-restoration-report.json 与 environment/client-after/ 这两个固定
        # 派生路径：子目录模式（M2 恢复段）按生产落点写，默认模式保持既有 evidence 根顶层写法。
        post_client_restoration_report = (
            evidence_root / "receipts" / "client-restoration-report.json"
            if receipts_subdir
            else receipts_root / "client-restoration-report.json"
        )
        post_client_restoration_report.parent.mkdir(parents=True, exist_ok=True)
        post_client_arguments: dict[str, object] = {
            "evidence_root": evidence_root,
            "output": relative(post_client_restoration_report),
            "phase": "candidate",
            "candidate_id": candidate_id,
        }
        for check_id, before_name, after_name, comparator in (
            codex_upgrade_receipt_finalizer.RESTORATION_INPUTS
        ):
            before_path = receipts_root / f"client-{before_name}.json"
            after_path = receipts_root / f"client-{after_name}.json"
            if comparator == "before_subset":
                before_state = self._database_state(after=True)
                after_state = self._database_state(after=True)
            else:
                before_state = {
                    "probe_kind": f"post_client_{check_id}",
                    "stable_value": "restored",
                }
                after_state = dict(before_state)
            self._write_state_snapshot(before_path, before_state)
            self._write_state_snapshot(after_path, after_state)
            post_client_arguments[before_name] = relative(before_path)
            post_client_arguments[after_name] = relative(after_path)
        codex_upgrade_receipt_finalizer.finalize_restoration(
            argparse.Namespace(**post_client_arguments)
        )
        self._write_json(
            evidence_root
            / "environment"
            / "client-after"
            / "probe-manifest.json",
            {
                "schema_version": "codex-upgrade-environment-probe/v1",
                "phase": "after",
                "observed_at_utc": client_checkpoint_at_utc,
            },
        )
        return observed_profile_path, client_receipts, post_client_restoration_report

    def _write_capture_stage(
        self,
        campaign_dir: Path,
        evidence_root: Path,
        *,
        phase: str,
        identity: dict[str, object],
        candidate_id: str | None = None,
        include_new_surface: bool = False,
        restoration_passed: bool = True,
        evaluation_transition_identity: dict[str, object] | None = None,
        evaluation_recovery_controls: dict[str, object] | None = None,
        seal: bool = True,
        bind_environment: bool = False,
        prepare_evidence: Callable[[Path], None] | None = None,
        extra_artifacts: list[dict[str, object]] | Callable[[Path], list[dict[str, object]]] | None = None,
    ) -> None:
        # 改造 5 M1 审核修正（真实评估链）：``prepare_evidence`` 在证据根建立后、扫描前放入真实
        # 断言证据（e2e H1 流 bundle）；``extra_artifacts`` 追加进 capture manifest，供真实 checker
        # 按场景交集投影与评估。默认不传时行为与既有用例完全一致。
        # ``prepare_evidence`` 先于目录创建调用：真实断言 bundle 要求证据根由它全新建立
        # （bundle 目录＝证据根，manifest 内 artifact 路径相对证据根）。
        if prepare_evidence is not None:
            prepare_evidence(evidence_root)
        evidence_root.mkdir(parents=True, exist_ok=True)
        if callable(extra_artifacts):
            extra_artifacts = extra_artifacts(evidence_root)
        campaign_manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        target_version = str(campaign_manifest["target_version"])
        campaign_configuration = campaign_manifest["configuration"]
        # 第三方入口（Kilo）收据的 model 必须等于 Campaign 的 Lite 轨（历史 Campaign 回退主轨）。
        third_party_model = codex_upgrade._third_party_client_model(campaign_configuration)
        attempt_id = (
            "20260731T000000Z-1111111111111111"
            if phase == "official"
            else "20260731T000000Z-2222222222222222"
        )
        run_nonce = ("1" if phase == "official" else "2") * 64
        window_base = datetime.now(timezone.utc) - timedelta(minutes=1)

        def timestamp(offset_seconds: int) -> str:
            return (
                (window_base + timedelta(seconds=offset_seconds))
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )

        attempt_started_at_utc = timestamp(0)
        client_checkpoint_at_utc = timestamp(360)
        prefix = evidence_root.name
        records = [
            {
                "request": {
                    "method": "POST",
                    "path": "/backend-api/codex/responses",
                    "http_version": "HTTP/1.1",
                    "headers": [
                        ["version", target_version],
                        ["host", "chatgpt.com"],
                    ],
                    "json_shape": {"model": "<string>", "input": []},
                }
            }
        ]
        if include_new_surface:
            records.append(
                {
                    "request": {
                        "method": "GET",
                        "path": "/backend-api/codex/new-egress?token=redacted",
                        "http_version": "HTTP/1.1",
                        "headers": [["host", "chatgpt.com"]],
                    }
                }
            )
        self._write_json(
            evidence_root / "surface.json",
            {"records": records},
        )

        restoration_report = evidence_root / "restoration-report.json"
        restoration_arguments: dict[str, object] = {
            "evidence_root": evidence_root,
            "output": Path(restoration_report.name),
            "phase": phase,
            "candidate_id": candidate_id,
        }
        for check_id, before_name, after_name, comparator in (
            codex_upgrade_receipt_finalizer.RESTORATION_INPUTS
        ):
            before_path = evidence_root / f"{before_name}.json"
            after_path = evidence_root / f"{after_name}.json"
            if comparator == "before_subset":
                before_state = self._database_state(after=False)
                after_state = self._database_state(after=True)
            else:
                before_state = {
                    "probe_kind": check_id,
                    "stable_value": "restored",
                }
                after_state = dict(before_state)
            self._write_state_snapshot(before_path, before_state)
            self._write_state_snapshot(after_path, after_state)
            restoration_arguments[before_name] = Path(before_path.name)
            restoration_arguments[after_name] = Path(after_path.name)
        self._make_private_tree(evidence_root)
        codex_upgrade_receipt_finalizer.finalize_restoration(
            argparse.Namespace(**restoration_arguments)
        )
        if bind_environment:
            # 历史 attempt 的环境绑定：恢复报告与 ARM64 前后收据都在 evidence_root 内，
            # ARM64 收据内容由测试用 replay 替身解释。
            for phase_name in ("arm64-before", "arm64-after"):
                self._write_json(
                    evidence_root / "environment" / phase_name / "receipt.json",
                    {
                        "schema_version": "codex-upgrade-arm64-environment-receipt/v1",
                        "status": "passed",
                        "phase": phase_name.replace("-", "_").replace("arm64", "attempt"),
                        "subject_id": attempt_id,
                    },
                )

        capture_manifest = evidence_root / "capture-manifest.json"
        self._write_json(
            capture_manifest,
            {
                "schema_version": (
                    "codex-candidate-capture-manifest/v1"
                ),
                "codex_version": target_version,
                "capture_id": f"{phase}-{candidate_id or 'official'}",
                "status": "complete",
                "artifacts": [
                    {
                        "path": "surface.json",
                        "sha256": codex_upgrade.file_sha256(
                            evidence_root / "surface.json"
                        ),
                        "kind": "process_trace",
                        "parser": "observation_json",
                        "scenario_ids": ["A01"],
                        "labels": {"side": phase},
                    },
                    *[dict(item) for item in (extra_artifacts or [])],
                ],
            },
        )

        client_bindings: list[dict[str, object]] = []
        observed_profile: dict[str, str] | None = None
        client_receipts: list[str] = []
        post_client_restoration_report: Path | None = None
        if phase == "candidate":
            observed_profile_path, client_receipts, post_client_restoration_report = self._synthesize_candidate_receipts(
                evidence_root,
                campaign_manifest=campaign_manifest,
                attempt_id=attempt_id,
                run_nonce=run_nonce,
                attempt_started_at_utc=attempt_started_at_utc,
                client_checkpoint_at_utc=client_checkpoint_at_utc,
                identity=identity,
                candidate_id=str(candidate_id),
                target_version=target_version,
                third_party_model=third_party_model,
                timestamp=timestamp,
            )


        self._make_private_tree(evidence_root)
        restoration = codex_upgrade._validate_restoration_report(
            restoration_report,
            [evidence_root],
            phase=phase,
            candidate_id=candidate_id,
        )
        if phase == "candidate":
            self.assertIsNotNone(post_client_restoration_report)
            restoration["post_client"] = (
                codex_upgrade._validate_restoration_report(
                    post_client_restoration_report,
                    [evidence_root],
                    phase="candidate",
                    candidate_id=str(candidate_id),
                )
            )
            observed_profile, _ = (
                codex_upgrade._validate_observed_profile_receipt(
                    observed_profile_path,
                    [evidence_root],
                    campaign_id=str(campaign_manifest["campaign_id"]),
                    attempt_id=attempt_id,
                    run_nonce=run_nonce,
                    attempt_started_at_utc=attempt_started_at_utc,
                    client_checkpoint_at_utc=client_checkpoint_at_utc,
                    candidate_id=str(candidate_id),
                    target_version=target_version,
                    expected_profile_id=str(identity["profile_id"]),
                    expected_profile_digest=str(identity["profile_digest"]),
                    image_id=str(identity["image_id"]),
                    image_reference=str(identity["image_reference"]),
                    source_tree_sha256=str(identity["source_tree_sha256"]),
                    build_id=str(identity["build_id"]),
                    deployed_version=str(identity["deployed_version"]),
                )
            )
            client_bindings = codex_upgrade._parse_client_evidence(
                client_receipts,
                [evidence_root],
                campaign_id=str(campaign_manifest["campaign_id"]),
                attempt_id=attempt_id,
                run_nonce=run_nonce,
                attempt_started_at_utc=attempt_started_at_utc,
                client_checkpoint_at_utc=client_checkpoint_at_utc,
                candidate_id=str(candidate_id),
                target_version=target_version,
                model=third_party_model,
                identity=identity,
            )
        normalized_surface = codex_upgrade.scan_evidence(
            [evidence_root], f"target-{phase}"
        )
        stage_relative = (
            Path("official")
            if phase == "official"
            else Path("candidates") / str(candidate_id)
        )
        surface_path = campaign_dir / stage_relative / "surface.json"
        self._write_json(surface_path, normalized_surface)
        capture_binding = self._binding(
            capture_manifest, f"{prefix}/{capture_manifest.name}"
        )
        try:
            planned_job = next(
                job
                for job in codex_upgrade._campaign_jobs(
                    campaign_dir,
                    campaign_manifest,
                    phase,
                    candidate_id=candidate_id,
                    runtime_image=str(identity.get("image_reference", "")),
                    profile_id=str(identity.get("profile_id", "")),
                    profile_digest=str(identity.get("profile_digest", "")),
                )
                if job.job_id == self.synthetic_job_ids[phase]
            )
            execution_sha256 = codex_upgrade._job_execution_sha256(planned_job)
        except codex_upgrade.ConfigurationError:
            execution_sha256 = "0" * 64
        result_item = {
            "id": self.synthetic_job_ids[phase],
            "phase": phase,
            "required": True,
            "execution_sha256": execution_sha256,
            "status": "complete" if restoration_passed else "failed",
            "description": "合成抓包阶段",
            "duration_seconds": 0.0,
            "steps": [],
            "evidence_roots": [str(evidence_root)],
            "missing_evidence_patterns": [],
            "empty_evidence_patterns": [],
            "covers": [],
            "scenario_ids": ["A01"],
            "scenario_receipts": [],
            "scenario_receipt_failures": [],
            "track": "main",
            "model_id": campaign_configuration["model"],
            "expected_use_responses_lite": False,
            "required_model_receipt": False,
            "model_condition_receipt": None,
            "model_condition_receipt_failure": None,
        }
        binary_verification: dict[str, object] | None = None
        if phase == "official":
            package_identity = campaign_manifest["official_identity"]["package"]
            binary_verification = {
                "passed": True,
                "expected_version": target_version,
                "expected_sha256": campaign_manifest["target_sha256"],
                "runtime_image_reference": f"capture-runtime@sha256:{'b' * 64}",
                "runtime_image_id": f"sha256:{'c' * 64}",
                "identities": [
                    {
                        "label": label,
                        "path": path,
                        "version": target_version,
                        "version_output": f"codex-cli {target_version}",
                        "sha256": campaign_manifest["target_sha256"],
                    }
                    for label, path in (
                        (
                            "container:capture_codex_bin",
                            campaign_configuration["capture_codex_bin"],
                        ),
                        (
                            "container:relay_codex_bin",
                            campaign_configuration["relay_codex_bin"],
                        ),
                        (
                            "host:relay_codex_bin",
                            campaign_configuration["relay_codex_bin"],
                        ),
                    )
                ],
                "package": package_identity,
                "helpers": [
                    {
                        "label": label,
                        "path": path,
                        "sha256": package_identity["code_mode_host_sha256"],
                    }
                    for label, path in (
                        (
                            "container:capture_code_mode_host_bin",
                            campaign_configuration["capture_code_mode_host_bin"],
                        ),
                        (
                            "container:relay_code_mode_host_bin",
                            campaign_configuration["relay_code_mode_host_bin"],
                        ),
                        (
                            "host:relay_code_mode_host_bin",
                            campaign_configuration["relay_code_mode_host_bin"],
                        ),
                    )
                ],
            }
        attempt_root = (
            campaign_dir / stage_relative / "attempts" / attempt_id
        )
        attempt_root.mkdir(parents=True, mode=0o700)
        reservation: dict[str, object] = {
            "schema_version": codex_upgrade.LEGACY_CAPTURE_RESERVATION_SCHEMA,
            "campaign_id": campaign_manifest["campaign_id"],
            "campaign_mode": campaign_manifest["campaign_mode"],
            "campaign_purpose": campaign_manifest["campaign_purpose"],
            "campaign_manifest_sha256": codex_upgrade.file_sha256(
                campaign_dir / "campaign.json"
            ),
            "phase": phase,
            "candidate_id": candidate_id,
            "candidate_purpose": (
                campaign_manifest["campaign_purpose"]
                if phase == "candidate"
                else None
            ),
            "attempt_id": attempt_id,
            "run_nonce": run_nonce,
            "started_at_utc": attempt_started_at_utc,
            "identity_sha256": codex_upgrade._fingerprint(identity),
            "planned_jobs": [
                {
                    "id": result_item["id"],
                    "required": True,
                    "execution_sha256": execution_sha256,
                }
            ],
        }
        reservation["reservation_digest"] = codex_upgrade._fingerprint(reservation)
        codex_upgrade._secure_write_json_once(
            attempt_root / "reservation.json", reservation
        )
        with mock.patch.object(
            codex_upgrade,
            "_replay_attempt_evidence_permissions",
            return_value={},
        ):
            attempt = codex_upgrade._write_capture_attempt(
                campaign_dir,
                attempt_root,
                {
                "campaign_id": campaign_manifest["campaign_id"],
                "phase": phase,
                "candidate_id": candidate_id,
                "status": "awaiting_receipts",
                "identity": identity,
                "results": [result_item],
                "evidence_roots": [str(evidence_root)],
                "environment": (
                    {
                        "evidence_root": str(evidence_root),
                        "before_probe": None,
                        "after_probe": None,
                        "restoration_report": self._environment_binding(
                            evidence_root, restoration_report
                        ),
                        "arm64_before_receipt": self._environment_binding(
                            evidence_root, evidence_root / "environment" / "arm64-before" / "receipt.json"
                        ),
                        "arm64_after_receipt": self._environment_binding(
                            evidence_root, evidence_root / "environment" / "arm64-after" / "receipt.json"
                        ),
                    }
                    if bind_environment
                    else {
                        "evidence_root": str(evidence_root),
                        "before_probe": None,
                        "after_probe": None,
                        "restoration_report": None,
                    }
                ),
                "binary_verification": binary_verification,
                "execution_error": None,
                "restoration_error": None,
                "next_gate": "生成机器收据后 seal",
                },
            )
        # 这一大组通用阶段夹具模拟的是历史 Attempt；显式冻结为 v2，后续
        # 测试才能验证新运行时保留的只读兼容，而不会伪造 v3 权限收据。
        attempt["schema_version"] = codex_upgrade.LEGACY_CAPTURE_ATTEMPT_SCHEMA
        attempt.pop("attempt_digest", None)
        attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
        (attempt_root / "attempt.json").write_text(
            json.dumps(attempt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (attempt_root / "attempt.json").chmod(0o600)
        if not seal:
            # 只留下 awaiting_receipts 的历史 attempt，供官方证据复用导入测试使用。
            return
        evaluation_transition: dict[str, str] | None = None
        if evaluation_transition_identity is not None:
            self.assertIsNotNone(evaluation_recovery_controls)
            # 模拟同一 attempt 已经使用两个评估过渡槽位；第三个槽位仍由正式
            # 两步审批入口生成和重放，避免测试绕过 transition-03 的真实合同。
            for index, target_digest in ((1, "a" * 64), (2, "b" * 64)):
                self._write_json(
                    codex_upgrade._evaluation_transition_preview_path(
                        attempt_root,
                        index,
                    ),
                    {"to_tool_files_sha256": target_digest},
                )
            transition_arguments = argparse.Namespace(
                campaign_dir=campaign_dir,
                phase=phase,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                approve_transition_sha256=None,
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=evaluation_transition_identity,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_recovery_controls_from_arguments",
                    return_value=evaluation_recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_phase_recovery_controls",
                    return_value=evaluation_recovery_controls,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_historical_phase_evaluation_transition_frozen_state",
                    return_value=(evaluation_recovery_controls, None),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_validate_frozen_phase_recovery_controls",
                    return_value=evaluation_recovery_controls,
                ),
            ):
                transition_preview = (
                    codex_upgrade.create_phase_evaluation_transition(
                        transition_arguments
                    )
                )
                self.assertEqual(transition_preview["transition_index"], 3)
                transition_arguments.approve_transition_sha256 = (
                    transition_preview["review_sha256"]
                )
                transition_result = (
                    codex_upgrade.create_phase_evaluation_transition(
                        transition_arguments
                    )
                )
            transition_path = Path(str(transition_result["transition"]))
            evaluation_transition = self._binding(
                transition_path,
                transition_path.relative_to(campaign_dir).as_posix(),
            )
        attempt_path = attempt_root / "attempt.json"
        payload: dict[str, object] = {
            "status": "complete" if restoration_passed else "failed",
            "campaign_mode": campaign_manifest["campaign_mode"],
            "campaign_purpose": campaign_manifest["campaign_purpose"],
            "candidate_purpose": (
                campaign_manifest["campaign_purpose"]
                if phase == "candidate"
                else None
            ),
            "attempt": self._binding(
                attempt_path, attempt_path.relative_to(campaign_dir).as_posix()
            ),
            "evidence_roots": [str(evidence_root)],
            "identity": identity,
            "results": [result_item],
            "surface": self._binding(
                surface_path, surface_path.relative_to(campaign_dir).as_posix()
            ),
            "client_bindings": client_bindings,
            "assertion_context": {
                "capture_manifest": capture_binding,
                "capture_manifest_path": str(capture_manifest.resolve()),
                "evidence_root": str(evidence_root.resolve()),
                "evidence_prefix": prefix,
            },
            "assertion_gate": {
                "side": "official" if phase == "official" else "candidate",
                "bundle_dir_name": "assertion-bundle",
                "bundle_provenance_sha256": "1" * 64,
                "bundle_entry_count": 1,
                "derived_provenance_sha256": None,
                "candidate_trace_receipt_sha256": None,
                "capture_manifest": {
                    "path": "capture-manifest.json",
                    "sha256": capture_binding["sha256"],
                },
                "acceptance_contract_sha256": "2" * 64,
                "artifact_count": 1,
                "observation_count": 1,
                "checked_rule_count": 1,
                "checked_check_count": 1,
            },
            "restoration": restoration,
            "security": {
                "raw_evidence_private": True,
                **codex_upgrade._evidence_security([evidence_root]),
            },
        }
        if evaluation_transition is not None:
            payload["evaluation_transition"] = evaluation_transition
        if observed_profile is not None:
            payload["observed_profile"] = observed_profile
        if phase == "official":
            payload["binary_verification"] = binary_verification
        payload["evidence_inventory"] = codex_upgrade._evidence_inventory(
            [evidence_root]
        )
        if evaluation_transition is not None or (
            phase == "official"
            and codex_upgrade._requires_complete_vc_artifacts(campaign_manifest)
        ):
            evidence_manifest = (
                codex_upgrade_evidence_manifest.build_evidence_manifest(
                    [evidence_root],
                    checkpoint_path=(
                        attempt_root / "evidence-manifest-checkpoint.json"
                    ),
                )
            )
            evidence_manifest_path = attempt_root / "evidence-manifest.json"
            self._write_json(evidence_manifest_path, evidence_manifest)
            payload["evidence_manifest"] = self._binding(
                evidence_manifest_path,
                evidence_manifest_path.relative_to(campaign_dir).as_posix(),
            )
            payload["evidence_inventory"] = evidence_manifest["inventory"]
            payload["scan_summary"] = evidence_manifest["scan"]
            payload["security"] = {
                "raw_evidence_private": True,
                **evidence_manifest["security"],
            }
        if (
            phase == "official"
            and codex_upgrade._requires_complete_vc_artifacts(campaign_manifest)
        ):
            source_diff = codex_upgrade._analysis_payload(
                campaign_dir,
                campaign_manifest,
                "source-diff",
            )
            baseline_surface = codex_upgrade._analysis_payload(
                campaign_dir,
                campaign_manifest,
                "baseline-surface",
            )
            official_diff = codex_upgrade.compare_surfaces(
                baseline_surface,
                normalized_surface,
            )
            finalized_root = attempt_root / "finalized"
            finalized_root.mkdir(mode=0o700)
            official_diff_path = finalized_root / "baseline-to-target-official.json"
            self._write_json(official_diff_path, official_diff)
            official_diff_binding = self._binding(
                official_diff_path,
                official_diff_path.relative_to(campaign_dir).as_posix(),
            )
            source_diff_path = campaign_dir / campaign_manifest["analysis"][
                "source-diff"
            ]["path"]
            discovery = codex_upgrade.codex_upgrade_vc_artifacts.build_discovery_inventory(
                campaign_id=str(campaign_manifest["campaign_id"]),
                target_version=target_version,
                source_diff=source_diff,
                official_diff=official_diff,
                source_diff_binding=self._binding(
                    source_diff_path,
                    source_diff_path.relative_to(campaign_dir).as_posix(),
                ),
                official_diff_binding=official_diff_binding,
                evidence_manifest_binding=payload["evidence_manifest"],
            )
            discovery_path = finalized_root / "discovery-inventory.json"
            self._write_json(discovery_path, discovery)
            payload["official_diff"] = official_diff_binding
            payload["discovery_inventory"] = self._binding(
                discovery_path,
                discovery_path.relative_to(campaign_dir).as_posix(),
            )
        codex_upgrade._seal_preview(
            campaign_dir,
            attempt_root,
            phase=phase,
            candidate_id=candidate_id,
            attempt=attempt,
            stage_payload=payload,
            approve_sha256=None,
        )
        preview_path = codex_upgrade._seal_preview_path(
            attempt_root,
            codex_upgrade._seal_transition_index(evaluation_transition),
        )
        payload["seal_preview"] = self._binding(
            preview_path,
            preview_path.relative_to(campaign_dir).as_posix(),
        )
        stage_path = codex_upgrade.save_stage_result(
            campaign_dir,
            "capture-official" if phase == "official" else "capture-candidate",
            payload,
            candidate_id=candidate_id,
        )
        if (
            phase == "official"
            and restoration_passed
            and codex_upgrade._requires_complete_vc_artifacts(campaign_manifest)
        ):
            codex_upgrade._complete_vc_phase(
                campaign_dir,
                campaign_manifest,
                phase="VC-1",
                stage_receipt_path=stage_path.resolve(strict=True),
            )

    def _write_classification_manifests(
        self,
        root: Path,
        *,
        omit_last: bool = False,
        blocked_rule: str | None = None,
        rules: tuple[str, ...] | None = None,
        assertion_profile_payload: dict[str, object] | None = None,
        version: str = "0.147.0",
        profile_payload: dict[str, object] | None = None,
    ) -> tuple[Path, Path, Path, Path, Path, tuple[str, ...]]:
        # 改造 5 M1 审核修正（真实评估链）：``rules`` 覆盖目标规则集合（默认 0.145.0 全集），
        # ``assertion_profile_payload`` 直接给出断言画像（默认由冻结画像裁剪）；默认行为不变。
        baseline_manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        if rules is None:
            rules = load_rule_manifest(baseline_manifest, "0.145.0")
        target_manifest = root / "target-rules.json"
        migration_manifest = root / "rule-migration.json"
        scenario_manifest = root / "target-scenarios.json"
        profile_manifest = root / "profile.json"
        assertion_profile_manifest = root / "assertion-profile.json"
        self._write_json(
            target_manifest,
            {
                "schema_version": codex_upgrade.RULE_SCHEMA,
                "codex_version": version,
                "required_rules": list(rules),
            },
        )
        entries = []
        for rule in rules[:-1] if omit_last else rules:
            classification = "blocked" if rule == blocked_rule else "inherit"
            entries.append(
                {
                    "baseline_rule": rule,
                    "target_rule": rule,
                    "classification": classification,
                    "rationale": "测试迁移闭环",
                    "evidence_refs": (
                        [] if classification == "blocked" else ["official-diff.json"]
                    ),
                }
            )
        self._write_json(
            migration_manifest,
            {
                "schema_version": codex_upgrade.MIGRATION_SCHEMA,
                "baseline_version": "0.145.0",
                "target_version": version,
                "status": "approved",
                "entries": entries,
                "discovery_classifications": [],
            },
        )
        self._write_scenario_manifest(
            root,
            target_manifest,
            rules,
            version=version,
            name=scenario_manifest.name,
        )
        if profile_payload is None:
            profile_payload = {
                "transport": "codex-official-egress",
                "rule_count": len(rules),
            }
        self._write_json(
            profile_manifest,
            {
                "schema_version": codex_upgrade.PROFILE_SCHEMA,
                "codex_version": version,
                "profile_id": f"codex-{version}-test-v1",
                "profile_digest": "c" * 64,
                "profile_payload": profile_payload,
                "profile_payload_sha256": codex_upgrade._fingerprint(
                    profile_payload
                ),
                "status": "approved",
            },
        )
        if assertion_profile_payload is not None:
            self._write_json(assertion_profile_manifest, assertion_profile_payload)
        else:
            self._write_assertion_profile(
                assertion_profile_manifest,
                rules,
                version=version,
            )
        return (
            target_manifest,
            migration_manifest,
            scenario_manifest,
            profile_manifest,
            assertion_profile_manifest,
            rules,
        )

    def _write_assertion_profile(
        self,
        path: Path,
        rules: tuple[str, ...],
        *,
        version: str,
    ) -> None:
        frozen_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        payload = json.loads(frozen_path.read_text(encoding="utf-8"))
        payload = json.loads(
            json.dumps(payload, ensure_ascii=False).replace("0.145.0", version)
        )
        spec_path = Path(__file__).resolve().parents[3] / (
            "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
        )
        payload["source_spec_sha256"] = (
            codex_upgrade.source_spec_section_sha256(spec_path, "第二章")
        )
        rows = {
            row["rule_id"]: row
            for row in payload["rules"]
        }
        template = json.loads(json.dumps(payload["rules"][0]))
        selected: list[dict[str, object]] = []
        for rule in rules:
            row = rows.get(rule)
            if row is None:
                row = json.loads(json.dumps(template))
                row["rule_id"] = rule
                row["description"] = "测试新增规则断言"
            selected.append(row)
        payload["rules"] = selected
        self._write_json(path, payload)

    def _classification_arguments(
        self,
        campaign_dir: Path,
        manifests: tuple[Path, Path, Path, Path, Path],
    ) -> list[str]:
        target, migration, scenario, profile, assertion_profile = manifests
        return [
            "classify",
            "--campaign-dir",
            str(campaign_dir),
            "--target-rule-manifest",
            str(target),
            "--migration-manifest",
            str(migration),
            "--scenario-manifest",
            str(scenario),
            "--profile-manifest",
            str(profile),
            "--assertion-profile-manifest",
            str(assertion_profile),
        ]

    def _approve_classification(
        self,
        campaign_dir: Path,
        manifests: tuple[Path, Path, Path, Path, Path],
    ) -> tuple[int, dict[str, object], str]:
        arguments = self._classification_arguments(campaign_dir, manifests)
        request_code, request_stdout, request_stderr = self._run_main(arguments)
        self.assertEqual(request_code, 2, request_stderr)
        request = json.loads(request_stdout)
        self.assertEqual(request["status"], "approval_required")
        return_code, stdout, stderr = self._run_main(
            [
                *arguments,
                "--approve-manifest-sha256",
                request["joint_manifest_sha256"],
            ]
        )
        return return_code, json.loads(stdout) if stdout else {}, stderr

    def _run_main(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = codex_upgrade.main(arguments)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def _create_classified_campaign(
        self,
        root: Path,
        *,
        campaign_purpose: str = "validation_only",
    ) -> tuple[Path, dict[str, object], tuple[str, ...]]:
        campaign_dir, manifest = self._create_campaign(
            root,
            campaign_purpose=campaign_purpose,
        )
        self._seal_official_stage(root, campaign_dir, manifest)
        target, migration, scenario, profile, assertion_profile, rules = (
            self._write_classification_manifests(root)
        )
        return_code, _, stderr = self._approve_classification(
            campaign_dir,
            (target, migration, scenario, profile, assertion_profile),
        )
        self.assertEqual(return_code, 0, stderr)
        return campaign_dir, manifest, rules

    def _seal_candidate_stage(
        self,
        root: Path,
        campaign_dir: Path,
        *,
        candidate_id: str = "candidate-a",
        profile_digest: str | None = None,
        git_commit: str | None = "f" * 40,
        include_new_surface: bool = False,
        restoration_passed: bool = True,
    ) -> tuple[Path, dict[str, object]]:
        profile_digest = profile_digest or "c" * 64
        evidence_root = root / f"{candidate_id}-evidence"
        identity = {
            "git_commit": git_commit,
            "source_tree_sha256": "d" * 64,
            "image_reference": f"sub2apiplus@sha256:{'9' * 64}",
            "image_digest": f"sha256:{'9' * 64}",
            "image_id": f"sha256:{'e' * 64}",
            "build_id": "build-0146-test",
            "deployed_version": "0.1.999-test",
            "profile_id": "codex-0.147.0-test-v1",
            "profile_digest": profile_digest,
            "candidate_purpose": codex_upgrade.load_campaign_manifest(
                campaign_dir
            )["campaign_purpose"],
        }
        self._write_capture_stage(
            campaign_dir,
            evidence_root,
            phase="candidate",
            identity=identity,
            candidate_id=candidate_id,
            include_new_surface=include_new_surface,
            restoration_passed=restoration_passed,
        )
        return evidence_root, identity

    def _candidate_gate_receipt(
        self,
        root: Path,
        campaign_dir: Path,
        candidate_id: str = "candidate-a",
    ) -> tuple[Path, Path]:
        """生成与当前封存 candidate 精确绑定的合成外部门禁收据。"""

        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        candidate = codex_upgrade._load_stage_result(
            campaign_dir,
            "capture-candidate",
            candidate_id,
        )
        identity = candidate["identity"]
        gate_root = root / f"{candidate_id}-external-gate"
        gate_root.mkdir(mode=0o700)
        gates = []
        for index, gate_id in enumerate(
            sorted(codex_upgrade_gate_receipt.CANDIDATE_COMMANDS)
        ):
            cwd, command = codex_upgrade_gate_receipt.CANDIDATE_COMMANDS[gate_id]
            evidence = gate_root / "evidence" / f"{gate_id}.json"
            self._write_json(evidence, {"gate_id": gate_id, "passed": True})
            evidence.chmod(0o600)
            gates.append(
                {
                    "gate_id": gate_id,
                    "command": list(command),
                    "working_directory": cwd,
                    "host": "runner-1",
                    "architecture": "linux/amd64",
                    "started_at_utc": f"2026-08-23T00:{index:02d}:00Z",
                    "completed_at_utc": f"2026-08-23T00:{index:02d}:30Z",
                    "exit_code": 0,
                    "status": "passed",
                    "passed_count": 1,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "stdout_sha256": "1" * 64,
                    "stderr_sha256": "2" * 64,
                    "evidence": [
                        {
                            "path": evidence.relative_to(gate_root).as_posix(),
                            "sha256": codex_upgrade.file_sha256(evidence),
                        }
                    ],
                }
            )
        facts = {
            "schema_version": codex_upgrade_gate_receipt.FACTS_SCHEMA,
            "phase": codex_upgrade_gate_receipt.CANDIDATE_PHASE,
            "attempt": {
                "attempt_id": "candidate-gate-attempt",
                "root_cause_id": None,
                "previous_receipt": None,
            },
            "subject": {
                "campaign_id": manifest["campaign_id"],
                "campaign_mode": manifest["campaign_mode"],
                "campaign_purpose": manifest["campaign_purpose"],
                "candidate_id": candidate_id,
                "candidate_purpose": identity["candidate_purpose"],
                "target_version": manifest["target_version"],
                "target_architecture": "linux/amd64",
                "profile_id": identity["profile_id"],
                "profile_digest": identity["profile_digest"],
                "candidate_package_digest": candidate["package_digest"],
                "candidate_source_tree_sha256": identity["source_tree_sha256"],
                "candidate_image_id": identity["image_id"],
                "candidate_image_reference": identity["image_reference"],
                "production_tree_sha256": None,
                "acceptance_sha256": None,
                "promotion_receipt_sha256": None,
            },
            "inputs": [],
            "gate_plan": None,
            "environment": {},
            "gates": gates,
        }
        for role, environment_phase in (
            ("before", "gate_before"),
            ("after", "gate_after"),
        ):
            environment_receipt = create_arm_receipt(
                gate_root,
                phase=environment_phase,
                subject_id="candidate-gate-attempt",
                prefix=f"gate-{role}",
            )
            facts["environment"][role] = {
                "path": environment_receipt.relative_to(gate_root).as_posix(),
                "sha256": codex_upgrade.file_sha256(environment_receipt),
            }
        facts_path = gate_root / "facts.json"
        self._write_json(facts_path, facts)
        facts_path.chmod(0o600)
        codex_upgrade_gate_receipt.finalize(
            gate_root,
            "facts.json",
            "receipt.json",
        )
        return gate_root, gate_root / "receipt.json"

    def _accept_campaign(
        self,
        root: Path,
        campaign_dir: Path,
        candidate_id: str,
        assertions: Path,
    ) -> dict[str, object]:
        gate_root, gate_receipt = self._candidate_gate_receipt(
            root,
            campaign_dir,
            candidate_id,
        )
        return codex_upgrade.accept_campaign(
            campaign_dir,
            candidate_id,
            assertions,
            gate_root,
            gate_receipt,
        )

    def _write_assertions(
        self,
        root: Path,
        rules: tuple[str, ...],
        identity: dict[str, str],
        *,
        candidate_id: str = "candidate-a",
        omit_last: bool = False,
        profile_digest: str | None = None,
        first_rule_status: str = "pass",
        first_rule_evidence_level: str = "full",
        campaign_dir: Path | None = None,
        official_evidence: Path | None = None,
    ) -> Path:
        assertions_path = root / f"{candidate_id}-assertions.json"
        selected_rules = rules[:-1] if omit_last else rules
        campaign_dir = campaign_dir or root / "campaign"
        manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
        official = codex_upgrade._load_stage_result(
            campaign_dir, "capture-official"
        )
        classification = codex_upgrade._load_stage_result(
            campaign_dir, "classify"
        )
        candidate = codex_upgrade._load_stage_result(
            campaign_dir, "capture-candidate", candidate_id
        )
        comparison = codex_upgrade._load_stage_result(
            campaign_dir, "compare", candidate_id
        )
        official_evidence = (
            official_evidence or root / "official-evidence" / "surface.json"
        )
        candidate_evidence = root / f"{candidate_id}-evidence" / "surface.json"
        official_relative = "official-evidence/surface.json"
        candidate_relative = f"{candidate_id}-evidence/surface.json"
        machine_root = campaign_dir / "assertions" / candidate_id / "machine"
        rows = []
        checker_sha256 = next(
            item["sha256"]
            for item in manifest["tool_identity"]["entries"]
            if item["path"] == "candidate_rule_assertion.py"
        )
        profile_reference = classification["assertion_profile_manifest"]
        rule_reference = classification["target_rule_manifest"]
        approved_profile = (
            campaign_dir / profile_reference["path"]
        ).resolve(strict=True)
        approved_rules = (
            campaign_dir / rule_reference["path"]
        ).resolve(strict=True)
        validation_modes = codex_upgrade._acceptance_validation_modes(
            campaign_dir, classification, tuple(rules)
        )
        official_authority = codex_upgrade._classification_official_authority(
            classification
        )
        for index, rule in enumerate(selected_rules):
            machine_bindings: dict[str, dict[str, str]] = {}
            commands: dict[str, list[str]] = {}
            sides = (
                (("official", official), ("candidate", candidate))
                if validation_modes[rule] == "dual_wire"
                else (("candidate", candidate),)
            )
            for side, stage in sides:
                expected_check_ids = codex_upgrade._acceptance_expected_check_ids(
                    campaign_dir, classification, rule, side
                )
                machine_path = machine_root / side / f"{rule}.json"
                context = stage["assertion_context"]
                command = codex_upgrade.build_machine_assertion_command(
                    rule_id=rule,
                    capture_manifest=context["capture_manifest_path"],
                    evidence_root=context["evidence_root"],
                    profile=str(approved_profile),
                    rule_manifest=str(approved_rules),
                    expected_codex_version="0.147.0",
                    expected_profile_sha256=profile_reference["sha256"],
                    side=side,
                    output=str(machine_path.resolve()),
                )
                machine_result = {
                    "schema_version": codex_upgrade.MACHINE_ASSERTION_SCHEMA,
                    "rule_id": rule,
                    "status": "pass",
                    "started_at": "2026-07-31T00:00:00Z",
                    "finished_at": "2026-07-31T00:00:01Z",
                    "exit_code": 0,
                    "checker_sha256": checker_sha256,
                    "command_sha256": (
                        codex_upgrade.machine_command_sha256(command)
                    ),
                    "checks": [
                        {
                            "id": check_id,
                            "description": f"{check_id} 判据",
                            "passed": True,
                            "expected": {"present": True},
                            "actual": {"present": True},
                            "evidence_paths": ["surface.json"],
                        }
                        for check_id in expected_check_ids
                    ],
                }
                self._write_json(machine_path, machine_result)
                machine_bindings[side] = self._binding(
                    machine_path,
                    machine_path.relative_to(campaign_dir).as_posix(),
                )
                commands[side] = command
            row: dict[str, object] = {
                "rule": rule,
                "validation_mode": validation_modes[rule],
                "status": first_rule_status if index == 0 else "pass",
                "candidate_evidence_refs": [
                    {
                        "path": candidate_relative,
                        "sha256": hashlib.sha256(
                            candidate_evidence.read_bytes()
                        ).hexdigest(),
                    }
                ],
                "candidate_machine_result": machine_bindings["candidate"],
                "candidate_command": commands["candidate"],
                "evidence_level": (
                    first_rule_evidence_level if index == 0 else "full"
                ),
                "rationale": "离线机器断言逐规则通过",
            }
            if validation_modes[rule] == "dual_wire":
                row["official_evidence_refs"] = [
                    {
                        "path": official_relative,
                        "sha256": hashlib.sha256(
                            official_evidence.read_bytes()
                        ).hexdigest(),
                    }
                ]
                row["official_machine_result"] = machine_bindings["official"]
                row["official_command"] = commands["official"]
            else:
                row["official_authority"] = dict(official_authority)
            rows.append(row)
        self._write_json(
            assertions_path,
            {
                "schema_version": codex_upgrade.RESULTS_SCHEMA_V2,
                "document_kind": "results",
                "candidate_id": candidate_id,
                "target_version": "0.147.0",
                "profile_id": identity["profile_id"],
                "profile_digest": profile_digest or identity["profile_digest"],
                "official_package_digest": official["package_digest"],
                "candidate_package_digest": candidate["package_digest"],
                "comparison_package_digest": comparison["package_digest"],
                "acceptance_contract_sha256": (
                    codex_upgrade._acceptance_contract_sha256(
                        campaign_dir, classification
                    )
                ),
                "rules": rows,
            },
        )
        return assertions_path

    def test_campaign_cli_exposes_all_staged_commands(self) -> None:
        parser = codex_upgrade._build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        self.assertEqual(
            set(subparsers[0].choices),
            {
                "plan",
                "canonical-import",
                "canonical-advance",
                "compile-vc-batch",
                "compile-and-run-vc-batch",
                "compile-vc-interrupted-recovery-batch",
                "recover-vc1-interruption",
                "successor",
                "recover-candidate-failed-jobs",
                "recover-candidate-post-run-seal",
                "rehearse-candidate-seal",
                "capture-official",
                "classify",
                "prepare-profile",
                "stage-profile",
                "plan-candidate-gates",
                "record-candidate-build",
                "capture-candidate",
                "candidate-runtime-override",
                "reuse-official-evidence",
                "compare",
                "accept",
                "deliver-candidate",
                "all",
                "evaluation-transition",
                "terminal-transition-preflight",
                "control-epoch",
                "deep-verify",
                "wire-transition-intent",
                "wire-transition-final",
                "evaluation-epoch",
                "verdict-official-attempt-identity",
                "harden-evidence-permissions",
                "reconcile-supervisor-run",
                "reconcile-attempt",
                "account-sealed-official",
                "account-sealed-candidate",
                # 改造 2：候选级 revision 的两个零请求控制命令。
                "revision-open",
                "invalidate-candidate",
                "evaluation-recover",
                # R8：预算延期与显式放弃是批次之间的控制面命令。
                "deadline-extend",
                "campaign-abandon",
                "status",
                "resume",
            },
        )

    def test_formal_failed_job_recovery_cli_fixes_reason_and_candidate(self) -> None:
        """正式恢复入口不暴露 reason，且默认保持 v7 Candidate 身份。"""

        parser = codex_upgrade._build_parser()
        arguments = parser.parse_args(
            [
                "recover-candidate-failed-jobs",
                "--predecessor-campaign-dir",
                "/campaign/source",
                "--campaign-dir",
                "/campaign/successor",
                "--campaign-id",
                "successor-a",
                "--codex-account-id",
                "90",
                "--predecessor-attempt-id",
                "attempt-a",
                "--job-rehearsal-root",
                "/control/rehearsal",
                "--job-rehearsal-receipt",
                "/control/rehearsal/receipt.json",
                "--recovery-timing-ledger-dir",
                "/control/timing",
                "--recovery-timing-receipt",
                "/control/timing/receipt.json",
                "--recovery-arm64-environment-root",
                "/control/arm64",
                "--recovery-arm64-environment-receipt",
                "/control/arm64/receipt.json",
                "--predecessor-stop-ledger-dir",
                "/control/stopped",
                "--predecessor-stop-receipt",
                "/control/stopped/receipt.json",
            ]
        )
        self.assertEqual(
            arguments.reason,
            "candidate_failed_job_tool_recovery",
        )
        self.assertEqual(arguments.predecessor_candidate_id, "c0154-candidate-v7")
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "recover-candidate-failed-jobs",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )

    def test_formal_failed_job_recovery_rejects_non_v7_source(self) -> None:
        """专用入口必须锁定固定路径、清单、Candidate、attempt 与工具摘要。"""

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            predecessor_dir = root / str(source["campaign_id"])
            predecessor_dir.mkdir()
            predecessor_path = predecessor_dir / "campaign.json"
            manifest = {
                "campaign_id": source["campaign_id"],
                "target_version": source["target_version"],
                "tool_identity": {"files_sha256": source["tool_files_sha256"]},
            }
            self._write_json(predecessor_path, manifest)
            arguments = argparse.Namespace(
                predecessor_campaign_dir=predecessor_dir,
                predecessor_candidate_id=source["candidate_id"],
                predecessor_attempt_id=source["attempt_id"],
            )
            attempt_overrides, abandoned = self._write_c0154_v7_source_attempt(
                predecessor_dir,
                source,
                codex_upgrade.file_sha256(predecessor_path),
            )
            fixed = {
                "campaign_dir": str(predecessor_dir),
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    predecessor_path
                ),
                **attempt_overrides,
            }
            with mock.patch.dict(source, fixed):
                actual = codex_upgrade._require_c0154_v7_formal_recovery_source(
                    arguments,
                    manifest,
                )
                self.assertEqual(actual["abandoned_candidate_attempt"], abandoned)

                for field, value in (
                    ("campaign_id", "another-campaign"),
                    ("target_version", "0.155.0"),
                    ("tool_identity", {"files_sha256": "0" * 64}),
                ):
                    changed = copy.deepcopy(manifest)
                    changed[field] = value
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "只允许已审计",
                    ):
                        codex_upgrade._require_c0154_v7_formal_recovery_source(
                            arguments,
                            changed,
                        )
                for field, value in (
                    ("predecessor_candidate_id", "another-candidate"),
                    ("predecessor_attempt_id", "another-attempt"),
                ):
                    changed = copy.copy(arguments)
                    setattr(changed, field, value)
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "只允许已审计",
                    ):
                        codex_upgrade._require_c0154_v7_formal_recovery_source(
                            changed,
                            manifest,
                        )

                wrong_path = copy.copy(arguments)
                wrong_path.predecessor_campaign_dir = root / "same-id-copy" / str(
                    source["campaign_id"]
                )
                wrong_path.predecessor_campaign_dir.mkdir(parents=True)
                self._write_json(
                    wrong_path.predecessor_campaign_dir / "campaign.json",
                    manifest,
                )
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "直接前序路径不可信",
                ):
                    codex_upgrade._require_c0154_v7_formal_recovery_source(
                        wrong_path,
                        manifest,
                    )

                with mock.patch.dict(
                    source,
                    {"campaign_manifest_sha256": "0" * 64},
                ):
                    with self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "前序清单摘要漂移",
                    ):
                        codex_upgrade._require_c0154_v7_formal_recovery_source(
                            arguments,
                            manifest,
                        )

    def test_v7_recovery_source_binding_rejects_version_hash_candidate_and_tool_drift(
        self,
    ) -> None:
        """场景元数据例外必须锁死完整 v7 来源坐标。"""

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            predecessor_dir = root / str(source["campaign_id"])
            predecessor_dir.mkdir()
            predecessor_path = predecessor_dir / "campaign.json"
            predecessor_manifest = {
                "campaign_id": source["campaign_id"],
                "target_version": source["target_version"],
                "tool_identity": {"files_sha256": source["tool_files_sha256"]},
            }
            self._write_json(predecessor_path, predecessor_manifest)
            source_manifest_sha256 = codex_upgrade.file_sha256(predecessor_path)
            manifest = {
                "target_version": source["target_version"],
                "predecessor": {
                    "campaign_dir": str(predecessor_dir),
                    "campaign_id": source["campaign_id"],
                    "campaign_manifest_sha256": source_manifest_sha256,
                    "reason": "candidate_failed_job_tool_recovery",
                },
            }
            attempt_overrides, abandoned = self._write_c0154_v7_source_attempt(
                predecessor_dir,
                source,
                source_manifest_sha256,
            )

            with mock.patch.dict(
                source,
                {
                    "campaign_dir": str(predecessor_dir),
                    "campaign_manifest_sha256": source_manifest_sha256,
                    **attempt_overrides,
                },
            ):
                actual = codex_upgrade._require_c0154_v7_recovery_source_binding(
                    root / ".successor-staging",
                    manifest,
                    candidate_id=source["candidate_id"],
                    attempt_id=source["attempt_id"],
                )
                self.assertEqual(actual["predecessor_manifest"], predecessor_manifest)
                self.assertEqual(actual["abandoned_candidate_attempt"], abandoned)

                changed = copy.deepcopy(manifest)
                changed["target_version"] = "0.151.0"
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "直接前序绑定不匹配",
                ):
                    codex_upgrade._require_c0154_v7_recovery_source_binding(
                        root / ".successor-staging",
                        changed,
                        candidate_id=source["candidate_id"],
                        attempt_id=source["attempt_id"],
                    )

                changed = copy.deepcopy(manifest)
                changed["predecessor"]["campaign_manifest_sha256"] = "0" * 64
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "直接前序绑定不匹配",
                ):
                    codex_upgrade._require_c0154_v7_recovery_source_binding(
                        root / ".successor-staging",
                        changed,
                        candidate_id=source["candidate_id"],
                        attempt_id=source["attempt_id"],
                    )

                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "Candidate 或 attempt 身份漂移",
                ):
                    codex_upgrade._require_c0154_v7_recovery_source_binding(
                        root / ".successor-staging",
                        manifest,
                        candidate_id="another-candidate",
                        attempt_id=source["attempt_id"],
                    )

                changed = copy.deepcopy(manifest)
                changed["predecessor"]["campaign_dir"] = str(root / "copy")
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "直接前序绑定不匹配",
                ):
                    codex_upgrade._require_c0154_v7_recovery_source_binding(
                        root / ".successor-staging",
                        changed,
                        candidate_id=source["candidate_id"],
                        attempt_id=source["attempt_id"],
                    )

                predecessor_manifest["tool_identity"]["files_sha256"] = "0" * 64
                self._write_json(predecessor_path, predecessor_manifest)
                changed_manifest_sha256 = codex_upgrade.file_sha256(
                    predecessor_path
                )
                tool_drift_manifest = copy.deepcopy(manifest)
                tool_drift_manifest["predecessor"][
                    "campaign_manifest_sha256"
                ] = changed_manifest_sha256
                with (
                    mock.patch.dict(
                        source,
                        {
                            "campaign_manifest_sha256": changed_manifest_sha256
                        },
                    ),
                    self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "工具.*身份漂移",
                    ),
                ):
                    codex_upgrade._require_c0154_v7_recovery_source_binding(
                        root / ".successor-staging",
                        tool_drift_manifest,
                        candidate_id=source["candidate_id"],
                        attempt_id=source["attempt_id"],
                    )

    def test_v7_legacy_build_receipt_requires_explicit_source_authority(
        self,
    ) -> None:
        """未进入固定 v7 来源重放时，v1 构建收据必须默认拒绝。"""

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory).resolve()
            receipt_path = (
                campaign_dir
                / "candidates"
                / str(source["candidate_id"])
                / "build-receipt.json"
            )
            self._write_json(
                receipt_path,
                {
                    "schema_version": (
                        codex_upgrade_vc_artifacts.LEGACY_CANDIDATE_BUILD_SCHEMA
                    )
                },
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "受管历史投影",
            ):
                codex_upgrade._replay_candidate_build_receipt(
                    campaign_dir,
                    {},
                    str(source["candidate_id"]),
                    receipt_path,
                )

    def test_v7_frozen_legacy_build_receipt_rejects_each_fixed_field_drift(
        self,
    ) -> None:
        """v1 兼容必须同时锁死来源、文件字节及两层自摘要。"""

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory).resolve()
            receipt_path = (
                campaign_dir
                / "candidates"
                / str(source["candidate_id"])
                / "build-receipt.json"
            )
            receipt = {
                "schema_version": (
                    codex_upgrade_vc_artifacts.LEGACY_CANDIDATE_BUILD_SCHEMA
                ),
                "campaign_id": source["campaign_id"],
                "campaign_manifest_sha256": "1" * 64,
                "candidate_id": source["candidate_id"],
                "target_version": source["target_version"],
                "receipt_digest": "2" * 64,
                "build": {"parameters_sha256": "3" * 64},
            }
            self._write_json(receipt_path, receipt)
            fixed = {
                "campaign_dir": str(campaign_dir),
                "campaign_manifest_sha256": receipt[
                    "campaign_manifest_sha256"
                ],
                "build_receipt_sha256": codex_upgrade.file_sha256(receipt_path),
                "build_receipt_bytes": receipt_path.stat().st_size,
                "build_receipt_digest": receipt["receipt_digest"],
                "build_parameters_sha256": receipt["build"][
                    "parameters_sha256"
                ],
            }
            manifest = {
                "campaign_id": source["campaign_id"],
                "target_version": source["target_version"],
            }

            with (
                mock.patch.dict(source, fixed),
                mock.patch.object(
                    codex_upgrade,
                    "_validated_c0154_v7_recovery_source_scope",
                    return_value={},
                ) as source_guard,
            ):
                binding = (
                    codex_upgrade._require_c0154_v7_frozen_legacy_build_receipt(
                        campaign_dir,
                        manifest,
                        str(source["candidate_id"]),
                        receipt_path,
                        receipt,
                    )
                )
                self.assertEqual(
                    binding,
                    {
                        "path": receipt_path.relative_to(campaign_dir).as_posix(),
                        "sha256": source["build_receipt_sha256"],
                        "bytes": source["build_receipt_bytes"],
                    },
                )
                source_guard.assert_called_once_with(
                    campaign_dir,
                    manifest,
                    candidate_id=source["candidate_id"],
                    attempt_id=source["attempt_id"],
                )

                receipt_drifts = {
                    "schema_version": {
                        "schema_version": codex_upgrade_vc_artifacts.CANDIDATE_BUILD_SCHEMA
                    },
                    "campaign_id": {"campaign_id": "another-campaign"},
                    "campaign_manifest_sha256": {
                        "campaign_manifest_sha256": "4" * 64
                    },
                    "candidate_id": {"candidate_id": "another-candidate"},
                    "target_version": {"target_version": "0.155.0"},
                    "receipt_digest": {"receipt_digest": "5" * 64},
                    "parameters_sha256": {
                        "build": {"parameters_sha256": "6" * 64}
                    },
                }
                for field, changes in receipt_drifts.items():
                    with self.subTest(field=field):
                        changed = copy.deepcopy(receipt)
                        changed.update(changes)
                        with self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError,
                            "历史构建收据.*漂移",
                        ):
                            codex_upgrade._require_c0154_v7_frozen_legacy_build_receipt(
                                campaign_dir,
                                manifest,
                                str(source["candidate_id"]),
                                receipt_path,
                                changed,
                            )

                for field, value in (
                    ("build_receipt_sha256", "7" * 64),
                    ("build_receipt_bytes", receipt_path.stat().st_size + 1),
                ):
                    with (
                        self.subTest(field=field),
                        mock.patch.dict(source, {field: value}),
                        self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError,
                            "历史构建收据.*漂移",
                        ),
                    ):
                        codex_upgrade._require_c0154_v7_frozen_legacy_build_receipt(
                            campaign_dir,
                            manifest,
                            str(source["candidate_id"]),
                            receipt_path,
                            receipt,
                        )

                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "历史构建收据.*漂移",
                ):
                    codex_upgrade._require_c0154_v7_frozen_legacy_build_receipt(
                        campaign_dir,
                        manifest,
                        "another-candidate",
                        receipt_path,
                        receipt,
                    )

            with (
                mock.patch.dict(source, fixed),
                mock.patch.object(
                    codex_upgrade,
                    "_validated_c0154_v7_recovery_source_scope",
                    side_effect=codex_upgrade.ConfigurationError(
                        "v7 失败 Job 恢复的直接前序路径不可信。"
                    ),
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "直接前序路径不可信",
                ),
            ):
                codex_upgrade._require_c0154_v7_frozen_legacy_build_receipt(
                    campaign_dir,
                    manifest,
                    str(source["candidate_id"]),
                    receipt_path,
                    receipt,
                )

    def test_published_v7_recovery_coordinates_replay_abandoned_attempt(self) -> None:
        """已发布后继必须从严格 v6 收据完整重放唯一源 attempt。"""

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            predecessor_dir = root / str(source["campaign_id"])
            campaign_dir = root / "successor"
            predecessor_dir.mkdir()
            campaign_dir.mkdir()
            predecessor_manifest = {
                "campaign_id": source["campaign_id"],
                "target_version": source["target_version"],
                "tool_identity": {"files_sha256": source["tool_files_sha256"]},
            }
            predecessor_path = predecessor_dir / "campaign.json"
            self._write_json(predecessor_path, predecessor_manifest)
            predecessor_sha256 = codex_upgrade.file_sha256(predecessor_path)
            manifest = {
                "campaign_id": "successor-a",
                "target_version": source["target_version"],
                "predecessor": {
                    "campaign_dir": str(predecessor_dir),
                    "campaign_id": source["campaign_id"],
                    "campaign_manifest_sha256": predecessor_sha256,
                    "reason": "candidate_failed_job_tool_recovery",
                },
            }
            successor_path = campaign_dir / "campaign.json"
            self._write_json(successor_path, manifest)
            attempt_overrides, abandoned = self._write_c0154_v7_source_attempt(
                predecessor_dir,
                source,
                predecessor_sha256,
            )
            predecessor_binding = {
                "campaign_dir": str(predecessor_dir),
                "campaign_id": source["campaign_id"],
                "campaign_manifest_sha256": predecessor_sha256,
            }
            imported = {
                "schema_version": codex_upgrade.PREDECESSOR_RECOVERY_IMPORT_SCHEMA,
                "created_at_utc": "2026-09-17T05:28:07Z",
                "reason": "candidate_failed_job_tool_recovery",
                "successor_campaign_id": manifest["campaign_id"],
                "successor_campaign_manifest_sha256": codex_upgrade.file_sha256(
                    successor_path
                ),
                "predecessor_campaign": predecessor_binding,
                "stages": {},
                "copied_files": [],
                "configuration_transition": {},
                "abandoned_candidate_attempt": abandoned,
                "job_rehearsal_transition": {},
                "recovery_control_transition": {},
            }
            imported["receipt_digest"] = codex_upgrade._fingerprint(imported)
            import_path = campaign_dir / "predecessor-import.json"

            def write_import(value: dict[str, object]) -> None:
                unsigned = {
                    key: item
                    for key, item in value.items()
                    if key != "receipt_digest"
                }
                value["receipt_digest"] = codex_upgrade._fingerprint(unsigned)
                self._write_json(import_path, value)

            with mock.patch.dict(
                source,
                {
                    "campaign_dir": str(predecessor_dir),
                    "campaign_manifest_sha256": predecessor_sha256,
                    **attempt_overrides,
                },
            ):
                write_import(imported)
                self.assertEqual(
                    codex_upgrade._published_c0154_v7_recovery_coordinates(
                        campaign_dir,
                        manifest,
                    ),
                    (source["candidate_id"], source["attempt_id"]),
                )

                extra_field = copy.deepcopy(imported)
                extra_field["unexpected"] = True
                write_import(extra_field)
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "导入身份",
                ):
                    codex_upgrade._published_c0154_v7_recovery_coordinates(
                        campaign_dir,
                        manifest,
                    )

                missing_field = copy.deepcopy(imported)
                missing_field.pop("job_rehearsal_transition")
                write_import(missing_field)
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "导入身份",
                ):
                    codex_upgrade._published_c0154_v7_recovery_coordinates(
                        campaign_dir,
                        manifest,
                    )

                write_import(imported)
                attempt_path = predecessor_dir / str(abandoned["path"])
                drifted_attempt = json.loads(
                    attempt_path.read_text(encoding="utf-8")
                )
                drifted_attempt["unexpected"] = True
                self._write_json(attempt_path, drifted_attempt)
                with (
                    mock.patch.dict(
                        source,
                        {"attempt_sha256": codex_upgrade.file_sha256(attempt_path)},
                    ),
                    self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "源 attempt 完整身份漂移",
                    ),
                ):
                    codex_upgrade._published_c0154_v7_recovery_coordinates(
                        campaign_dir,
                        manifest,
                    )

    def test_runtime_target_scenario_allows_only_five_codex_bin_bindings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = self._campaign_arguments(root)
            original = json.loads(
                arguments.target_scenario_manifest.read_text(encoding="utf-8")
            )
            predecessor = self._add_runtime_codex_jobs(
                original,
                bind_codex_bin=False,
            )
            successor = self._add_runtime_codex_jobs(
                original,
                bind_codex_bin=True,
            )

            self.assertEqual(
                codex_upgrade._validate_runtime_target_scenario_change(
                    predecessor,
                    successor,
                ),
                tuple(sorted(codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS)),
            )

            official_drift = json.loads(json.dumps(successor))
            next(
                job
                for job in official_drift["capture_jobs"]
                if job["id"] == "official-test"
            )["steps"][0]["argv"] = ["false"]
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "官方 Job 执行合同",
            ):
                codex_upgrade._validate_runtime_target_scenario_change(
                    predecessor,
                    official_drift,
                )

            candidate_drift = json.loads(json.dumps(successor))
            next(
                job
                for job in candidate_drift["capture_jobs"]
                if job["id"] == "candidate-core-direct"
            )["description"] = "未授权变化"
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "除固定 CODEX_BIN 外",
            ):
                codex_upgrade._validate_runtime_target_scenario_change(
                    predecessor,
                    candidate_drift,
                )

            partial = json.loads(json.dumps(successor))
            next(
                job
                for job in partial["capture_jobs"]
                if job["id"] == "candidate-core-direct"
            )["steps"][0]["environment"].pop("CODEX_BIN")
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "全部固定客户端 Job",
            ):
                codex_upgrade._validate_runtime_target_scenario_change(
                    predecessor,
                    partial,
                )

    def test_runtime_successor_recovery_uses_failed_changed_union(self) -> None:
        """v9 首次恢复只执行失败项与五个场景变化项的并集。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "successor"
            predecessor = root / "predecessor"
            source_root = (
                predecessor
                / "candidates"
                / "candidate-old"
                / "attempts"
                / "attempt-old"
            )
            source_root.mkdir(parents=True)
            campaign.mkdir()
            source_attempt_path = source_root / "attempt.json"
            source_attempt_path.write_text("source\n", encoding="utf-8")
            identity = {"runtime": "same"}
            source_attempt = {
                "candidate_id": "candidate-old",
                "attempt_id": "attempt-old",
                "attempt_digest": "a" * 64,
                "identity": identity,
                "status": "failed",
            }
            changed = sorted(codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS)
            reused = [
                "candidate-frozen-core",
                "candidate-h1-wire",
                "candidate-images-wire",
            ]
            failed = [
                "candidate-compact-mitm",
                "candidate-core-mitm",
                "candidate-frozen-aux",
            ]
            planned = sorted(set(changed) | set(reused) | set(failed))
            source_completed = sorted(set(planned) - set(failed))
            abandoned = {
                "candidate_id": "candidate-old",
                "attempt_id": "attempt-old",
                "path": str(source_attempt_path.relative_to(predecessor)),
                "sha256": codex_upgrade.file_sha256(source_attempt_path),
                "attempt_digest": source_attempt["attempt_digest"],
                "identity_sha256": codex_upgrade._fingerprint(identity),
                "status": "failed",
            }
            import_path = campaign / "predecessor-import.json"
            self._write_json(
                import_path,
                {
                    "schema_version": (
                        codex_upgrade.PREDECESSOR_RUNTIME_SCENARIO_IMPORT_SCHEMA
                    ),
                    "reason": "candidate_runtime_identity_correction",
                    "abandoned_candidate_attempt": abandoned,
                    "target_scenario_transition": {
                        "changed_job_ids": changed,
                    },
                },
            )
            classification = {
                "predecessor_import": {
                    "path": "predecessor-import.json",
                    "sha256": codex_upgrade.file_sha256(import_path),
                }
            }
            jobs = [
                Job(
                    job_id=job_id,
                    phase="candidate",
                    suites=("full",),
                    description=job_id,
                    steps=(),
                    evidence_roots=(str(root / job_id),),
                    covers=(),
                )
                for job_id in planned
            ]
            source_scope = {
                "planned_job_ids": planned,
                "completed_job_ids": source_completed,
                "failed_job_ids": failed,
                "pending_job_ids": [],
                "execute_job_ids": failed,
                "environment_boundary_sha256": "b" * 64,
            }
            def tool_identity(values: dict[str, str]) -> dict[str, object]:
                entries = [
                    {"path": path, "sha256": sha256}
                    for path, sha256 in sorted(values.items())
                ]
                components = codex_upgrade._tool_component_identities(entries)
                return {
                    "git_commit": None,
                    "entry_count": len(entries),
                    "files_sha256": codex_upgrade._fingerprint(
                        {"entries": entries}
                    ),
                    "entries": entries,
                    "components": components["components"],
                    **codex_upgrade._tool_identity_sides(entries),
                }

            source_files = {
                "build_fingerprint_proxy.sh": "9" * 64,
                "codex_upgrade.py": "1" * 64,
                "codex_upgrade_predecessor_import.schema.json": "2" * 64,
                "codex_upgrade_scenarios_0_151_0.json": "3" * 64,
                "codex_upgrade_scenarios_0_154_0.json": "d" * 64,
                "mitm_scenario_checkpoint.py": "8" * 64,
                "prewarm_codex_home.py": "a" * 64,
                "run_candidate_aux_capture.sh": "4" * 64,
                "run_codex_scenario_target.py": "5" * 64,
                "run_sub2api_direct_matrix.sh": "6" * 64,
                "run_sub2api_openai_mitm_matrix.sh": "7" * 64,
                "runtime_scripts/run_fingerprint_mitm_pair.sh": "b" * 64,
                "runtime_scripts/start_mitm.sh": "c" * 64,
            }
            successor_files = dict(source_files)
            for path in source_files:
                successor_files[path] = codex_upgrade._fingerprint(
                    {"changed_path": path}
                )
            predecessor_manifest = {
                "campaign_id": "campaign-old",
                "tool_identity": tool_identity(source_files),
            }
            manifest = {
                "predecessor": {
                    "campaign_dir": str(predecessor),
                    "reason": "candidate_runtime_identity_correction",
                },
                "tool_identity": tool_identity(successor_files),
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=predecessor_manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=source_scope,
                ),
            ):
                recovery = codex_upgrade._runtime_successor_recovery_source(
                    campaign,
                    manifest,
                    classification,
                    candidate_id="candidate-old",
                    identity=identity,
                    planned_jobs=jobs,
                )

            self.assertIsNotNone(recovery)
            assert recovery is not None
            scope = recovery["scope"]
            self.assertEqual(scope["completed_job_ids"], sorted(reused))
            self.assertEqual(
                scope["execute_job_ids"], sorted(set(changed) | set(failed))
            )
            self.assertEqual(len(scope["execute_job_ids"]), 6)
            self.assertEqual(
                recovery["source_receipt"]["path"], "predecessor-import.json"
            )
            self.assertEqual(
                recovery["allowed_high_risk_path_changes"],
                sorted(
                    set(codex_upgrade.RUNTIME_SUCCESSOR_CHANGED_TOOL_PATH_JOB_IDS)
                    & set(source_files)
                ),
            )
            self.assertEqual(
                recovery["source_tool_affected_job_ids"],
                sorted(set(changed) | {"candidate-frozen-aux"}),
            )

    def test_control_replacement_uses_epoch_frozen_failed_scope(self) -> None:
        """replacement 恢复不得用粗粒度工具影响集合扩大 epoch 闭集。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "replacement"
            predecessor = root / "failed-source"
            source_root = (
                predecessor
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            source_root.mkdir(parents=True)
            campaign.mkdir()
            source_attempt_path = source_root / "attempt.json"
            source_attempt_path.write_text("source\n", encoding="utf-8")
            identity = {"runtime": "same"}
            failed = ["candidate-compact-mitm", "candidate-core-mitm"]
            reused = [
                "candidate-compact-direct",
                "candidate-core-direct",
                "candidate-frozen-aux",
                "candidate-frozen-core",
                "candidate-h1-wire",
                "candidate-images-wire",
                "candidate-ws-handshake-repeat",
            ]
            planned = sorted(failed + reused)
            source_attempt = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "a" * 64,
                "identity": identity,
                "status": "failed",
            }
            abandoned = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "path": str(source_attempt_path.relative_to(predecessor)),
                "sha256": codex_upgrade.file_sha256(source_attempt_path),
                "attempt_digest": source_attempt["attempt_digest"],
                "identity_sha256": codex_upgrade._fingerprint(identity),
                "status": "failed",
            }

            def tool_identity(values: dict[str, str]) -> dict[str, object]:
                entries = [
                    {"path": path, "sha256": sha256}
                    for path, sha256 in sorted(values.items())
                ]
                components = codex_upgrade._tool_component_identities(entries)
                return {
                    "entries": entries,
                    "files_sha256": codex_upgrade._fingerprint(
                        {"entries": entries}
                    ),
                    "components": components["components"],
                    **codex_upgrade._tool_identity_sides(entries),
                }

            source_files = {
                "codex_upgrade.py": "1" * 64,
                "mitm_scenario_checkpoint.py": "2" * 64,
                "run_sub2api_openai_mitm_matrix.sh": "3" * 64,
                "run_sub2api_direct_matrix.sh": "4" * 64,
            }
            successor_files = {
                path: f"{index:x}" * 64
                for index, path in enumerate(source_files, 5)
            }
            predecessor_manifest = {
                "campaign_id": "failed-source",
                "tool_identity": tool_identity(source_files),
            }
            predecessor_manifest_path = predecessor / "campaign.json"
            self._write_json(predecessor_manifest_path, predecessor_manifest)
            source_campaign = {
                "campaign_dir": str(predecessor.resolve()),
                "campaign_id": "failed-source",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    predecessor_manifest_path
                ),
            }
            import_path = campaign / "predecessor-import.json"
            self._write_json(
                import_path,
                {
                    "schema_version": (
                        codex_upgrade.PREDECESSOR_CONTROL_REPLACEMENT_IMPORT_SCHEMA
                    ),
                    "reason": "candidate_recovery_control_replacement",
                    "control_replacement": {
                        "source_campaign": source_campaign,
                        "source_attempt": abandoned,
                    },
                },
            )
            classification = {
                "predecessor_import": {
                    "path": "predecessor-import.json",
                    "sha256": codex_upgrade.file_sha256(import_path),
                }
            }
            manifest = {
                "predecessor": {
                    "campaign_dir": str((root / "control-refresh").resolve()),
                    "reason": "candidate_recovery_control_replacement",
                },
                "tool_identity": tool_identity(successor_files),
            }
            jobs = [
                Job(
                    job_id=job_id,
                    phase="candidate",
                    suites=("full",),
                    description=job_id,
                    steps=(),
                    evidence_roots=(str(root / job_id),),
                    covers=(),
                )
                for job_id in planned
            ]
            source_scope = {
                "planned_job_ids": planned,
                "completed_job_ids": reused,
                "failed_job_ids": failed,
                "pending_job_ids": [],
                "execute_job_ids": failed,
                "environment_boundary_sha256": "b" * 64,
            }
            epoch_source = {
                **abandoned,
                "candidate_identity_sha256": abandoned["identity_sha256"],
                "planned_job_ids": planned,
                "execute_job_ids": failed,
                "reused_job_ids": reused,
                "failed_job_ids": failed,
                "pending_job_ids": [],
                "production_paths": [
                    "mitm_scenario_checkpoint.py",
                    "run_sub2api_openai_mitm_matrix.sh",
                ],
            }
            epoch_tool = tool_identity(successor_files)
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=predecessor_manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=source_scope,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_control_epoch_receipt",
                    return_value={
                        "source": epoch_source,
                        "invariants": {
                            "tool_production_sha256": (
                                codex_upgrade._tool_identity_side_digest_excluding(
                                    epoch_tool,
                                    "production",
                                    codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
                                )
                            )
                        },
                    },
                ),
            ):
                recovery = codex_upgrade._runtime_successor_recovery_source(
                    campaign,
                    manifest,
                    classification,
                    candidate_id="candidate-a",
                    identity=identity,
                    planned_jobs=jobs,
                )

            self.assertIsNotNone(recovery)
            assert recovery is not None
            self.assertEqual(recovery["scope"]["execute_job_ids"], failed)
            self.assertEqual(recovery["scope"]["completed_job_ids"], reused)
            self.assertEqual(
                recovery["allowed_high_risk_path_changes"],
                [
                    "mitm_scenario_checkpoint.py",
                    "run_sub2api_openai_mitm_matrix.sh",
                ],
            )
            self.assertEqual(
                recovery["validated_current_production_sha256"],
                codex_upgrade._tool_identity_side_digest_excluding(
                    epoch_tool,
                    "production",
                    codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
                ),
            )

    def test_producer_successor_recovers_optional_failures_from_awaiting_attempt(
        self,
    ) -> None:
        """产出工具后继可承接尚未进入 Kilo／seal 的可选失败项。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign = root / "successor"
            predecessor = root / "predecessor"
            source_root = (
                predecessor
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            source_root.mkdir(parents=True)
            campaign.mkdir()
            source_attempt_path = source_root / "attempt.json"
            source_attempt_path.write_text("source\n", encoding="utf-8")
            identity = {"runtime": "same"}
            failed = ["candidate-compact-mitm", "candidate-core-mitm"]
            reused = [
                "candidate-compact-direct",
                "candidate-core-direct",
                "candidate-frozen-aux",
                "candidate-frozen-core",
                "candidate-h1-wire",
                "candidate-images-wire",
                "candidate-ws-handshake-repeat",
            ]
            planned = sorted(failed + reused)
            source_attempt = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "a" * 64,
                "identity": identity,
                "status": "awaiting_receipts",
            }
            abandoned = {
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "path": str(source_attempt_path.relative_to(predecessor)),
                "sha256": codex_upgrade.file_sha256(source_attempt_path),
                "attempt_digest": source_attempt["attempt_digest"],
                "identity_sha256": codex_upgrade._fingerprint(identity),
                "status": "awaiting_receipts",
            }
            import_path = campaign / "predecessor-import.json"
            self._write_json(
                import_path,
                {
                    "schema_version": codex_upgrade.PREDECESSOR_RECOVERY_IMPORT_SCHEMA,
                    "reason": "candidate_failed_job_tool_recovery",
                    "abandoned_candidate_attempt": abandoned,
                },
            )
            classification = {
                "predecessor_import": {
                    "path": "predecessor-import.json",
                    "sha256": codex_upgrade.file_sha256(import_path),
                }
            }
            jobs = [
                Job(
                    job_id=job_id,
                    phase="candidate",
                    suites=("full",),
                    description=job_id,
                    steps=(),
                    evidence_roots=(str(root / job_id),),
                    covers=(),
                )
                for job_id in planned
            ]

            def tool_identity(mitm_sha256: str, orchestrator_sha256: str) -> dict[str, object]:
                entries = [
                    {
                        "path": "codex_upgrade.py",
                        "sha256": orchestrator_sha256,
                    },
                    {
                        "path": "run_sub2api_openai_mitm_matrix.sh",
                        "sha256": mitm_sha256,
                    },
                ]
                components = codex_upgrade._tool_component_identities(entries)
                return {
                    "entries": entries,
                    "files_sha256": codex_upgrade._fingerprint(
                        {"entries": entries}
                    ),
                    "components": components["components"],
                    **codex_upgrade._tool_identity_sides(entries),
                }

            predecessor_manifest = {
                "campaign_id": "campaign-old",
                "tool_identity": tool_identity("1" * 64, "2" * 64),
            }
            manifest = {
                "predecessor": {
                    "campaign_dir": str(predecessor),
                    "reason": "candidate_failed_job_tool_recovery",
                },
                "tool_identity": tool_identity("3" * 64, "4" * 64),
            }
            source_scope = {
                "planned_job_ids": planned,
                "completed_job_ids": reused,
                "failed_job_ids": failed,
                "pending_job_ids": [],
                "execute_job_ids": failed,
                "environment_boundary_sha256": "b" * 64,
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=predecessor_manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(source_root, source_attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_phase_evaluation_recovery_scope",
                    return_value=source_scope,
                ) as scope_builder,
            ):
                recovery = codex_upgrade._runtime_successor_recovery_source(
                    campaign,
                    manifest,
                    classification,
                    candidate_id="candidate-a",
                    identity=identity,
                    planned_jobs=jobs,
                )

            self.assertIsNotNone(recovery)
            assert recovery is not None
            self.assertEqual(recovery["scope"]["execute_job_ids"], failed)
            self.assertEqual(recovery["scope"]["completed_job_ids"], reused)
            self.assertEqual(
                recovery["allowed_high_risk_path_changes"],
                ["run_sub2api_openai_mitm_matrix.sh"],
            )
            self.assertEqual(
                recovery["allowed_source_statuses"],
                ["awaiting_receipts", "failed"],
            )
            self.assertTrue(
                scope_builder.call_args.kwargs["allow_awaiting_failures"]
            )

    def test_legacy_awaiting_optional_failure_is_a_recovery_source(self) -> None:
        """旧工具误写的可选失败必须进入定向恢复，不能继续要求 seal。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt_root = root / "attempt-a"
            attempt_root.mkdir()
            (attempt_root / "attempt.json").write_text("{}\n", encoding="utf-8")
            identity = {"runtime": "same"}
            attempt = {
                "status": "awaiting_receipts",
                "identity": identity,
                "results": [
                    {
                        "id": "candidate-core-mitm",
                        "required": False,
                        "status": "failed",
                    }
                ],
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_ordered_capture_attempts",
                    return_value=[(attempt_root, {})],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
            ):
                source = codex_upgrade._latest_failed_attempt_for_identity(
                    root,
                    phase="candidate",
                    candidate_id="candidate-a",
                    identity=identity,
                )
            self.assertEqual(source, (attempt_root, attempt))

    def test_seal_rejects_any_failed_job_before_scanning(self) -> None:
        """可选 Job 失败也必须在证据扫描和 seal 之前失败关闭。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt_root = root / "attempt-a"
            attempt = {
                "results": [
                    {
                        "id": "candidate-compact-mitm",
                        "required": False,
                        "status": "failed",
                    }
                ]
            }
            arguments = argparse.Namespace(
                campaign_dir=root,
                candidate_id="candidate-a",
                attempt_id="attempt-a",
            )
            with (
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value={"campaign_id": "campaign-a"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_plan_identity",
                ) as plan_identity,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "禁止 seal",
                ):
                    codex_upgrade._seal_capture_attempt(arguments, "candidate")
            plan_identity.assert_not_called()

    def test_historical_result_allows_only_closed_high_risk_paths(self) -> None:
        """粗组件变化只能排除已映射到执行闭集的精确文件。"""

        job = Job(
            job_id="candidate-h1-wire",
            phase="candidate",
            suites=("full",),
            description="h1",
            steps=(
                {
                    "argv": [
                        "bash",
                        "/repo/tools/official_client_capture/run_h1_wire_probe.sh",
                    ],
                    "environment": {},
                    "timeout": 60,
                },
            ),
            evidence_roots=("/capture/h1",),
            covers=(),
        )

        def identity(
            direct_sha256: str,
            orchestrator_sha256: str,
            scenario_sha256: str = "6" * 64,
        ) -> dict[str, object]:
            entries = [
                {"path": "codex_upgrade.py", "sha256": orchestrator_sha256},
                {
                    "path": "codex_upgrade_scenarios_0_151_0.json",
                    "sha256": scenario_sha256,
                },
                {"path": "run_h1_wire_probe.sh", "sha256": "1" * 64},
                {
                    "path": "run_sub2api_direct_matrix.sh",
                    "sha256": direct_sha256,
                },
            ]
            components = codex_upgrade._tool_component_identities(entries)
            return {
                "entries": entries,
                "components": components["components"],
                **codex_upgrade._tool_identity_sides(entries),
            }

        frozen = identity("2" * 64, "3" * 64)
        current = identity("4" * 64, "5" * 64)
        runtime_identity = {"runtime": "same"}
        metadata = codex_upgrade._job_incremental_metadata(
            job,
            identity=runtime_identity,
            tool_identity=frozen,
        )
        result = {
            "execution_sha256": metadata["input_sha256"],
            "tool_components": metadata["components"],
            "tool_component_digests": metadata["component_digests"],
            "tool_dependency_files": metadata["tool_dependency_files"],
            "input_sha256": metadata["input_sha256"],
            "environment_sha256": metadata["environment_sha256"],
            "dependency_sha256": metadata["dependency_sha256"],
            "incremental_result_key": metadata["result_key"],
        }
        self.assertFalse(
            codex_upgrade._historical_result_metadata_matches(
                result,
                job,
                runtime_identity,
                frozen,
                metadata["input_sha256"],
                current_tool=current,
            )
        )
        self.assertTrue(
            codex_upgrade._historical_result_metadata_matches(
                result,
                job,
                runtime_identity,
                frozen,
                metadata["input_sha256"],
                current_tool=current,
                allowed_high_risk_path_changes={
                    "run_sub2api_direct_matrix.sh"
                },
            )
        )
        epoch_current = identity("4" * 64, "5" * 64, "7" * 64)
        epoch_production_sha256 = (
            codex_upgrade._tool_identity_side_digest_excluding(
                epoch_current,
                "production",
                codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
            )
        )
        self.assertTrue(
            codex_upgrade._historical_result_metadata_matches(
                result,
                job,
                runtime_identity,
                frozen,
                metadata["input_sha256"],
                current_tool=epoch_current,
                allowed_high_risk_path_changes={
                    "run_sub2api_direct_matrix.sh"
                },
                validated_current_production_sha256=(
                    epoch_production_sha256
                ),
            )
        )

    def test_v7_preview_replays_hybrid_and_evaluator_drift(self) -> None:
        """v7 严格预览应承接旧结果，不得把混合文件或 timing schema 判成重跑。"""

        def tool_identity(
            values: dict[str, str],
            *,
            wire_closure: str,
            files_sha256: str | None = None,
        ) -> dict[str, object]:
            entries = [
                {"path": path, "sha256": digest}
                for path, digest in sorted(values.items())
            ]
            components = codex_upgrade._tool_component_identities(entries)
            return {
                "entry_count": len(entries),
                "files_sha256": files_sha256
                or codex_upgrade._fingerprint({"entries": entries}),
                "entries": entries,
                "components": components["components"],
                **codex_upgrade._tool_identity_sides(entries),
                "orchestrator_closures": {
                    "wire_producer": {"closure_sha256": wire_closure}
                },
            }

        source = codex_upgrade.C0154_V7_RECOVERY_SOURCE
        frozen_files = {
            "candidate_rule_expectations_0_154_0.json": "1" * 64,
            "codex_upgrade.py": "2" * 64,
            "codex_upgrade_timing_ledger.schema.json": "3" * 64,
            "run_candidate_core_capture.sh": "4" * 64,
            "run_h1_wire_probe.sh": "5" * 64,
        }
        current_files = {
            path: (digest if path == "run_h1_wire_probe.sh" else "6" * 64)
            for path, digest in frozen_files.items()
        }
        frozen_tool = tool_identity(
            frozen_files,
            wire_closure="7" * 64,
        )
        current_tool = tool_identity(current_files, wire_closure="8" * 64)
        job = Job(
            job_id="candidate-h1-wire",
            phase="candidate",
            suites=("full",),
            description="h1",
            steps=(
                {
                    "argv": [
                        "bash",
                        "/capture/tools/official_client_capture/run_h1_wire_probe.sh",
                    ],
                    "environment": {},
                },
            ),
            evidence_roots=("/capture/h1",),
            covers=(),
        )
        identity = {"runtime": "same"}
        metadata = codex_upgrade._job_incremental_metadata(
            job,
            identity=identity,
            tool_identity=frozen_tool,
        )
        result = {
            "id": job.job_id,
            "status": "complete",
            "execution_sha256": metadata["input_sha256"],
            "tool_components": metadata["components"],
            "tool_component_digests": metadata["component_digests"],
            "tool_dependency_files": metadata["tool_dependency_files"],
            "input_sha256": metadata["input_sha256"],
            "environment_sha256": metadata["environment_sha256"],
            "dependency_sha256": metadata["dependency_sha256"],
            "incremental_result_key": metadata["result_key"],
        }
        attempt = {
            "status": "failed",
            "identity": identity,
            "results": [result],
        }
        frozen_manifest = {
            "campaign_id": source["campaign_id"],
            "target_version": source["target_version"],
            "tool_identity": frozen_tool,
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            current_dir = root / "current"
            attempt_root = (
                source_dir
                / "candidates"
                / str(source["candidate_id"])
                / "attempts"
                / str(source["attempt_id"])
            )
            attempt_root.mkdir(parents=True)
            (attempt_root / "attempt.json").write_text("{}\n", encoding="utf-8")
            current_dir.mkdir()
            with (
                mock.patch.dict(
                    source,
                    {"tool_files_sha256": frozen_tool["files_sha256"]},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    side_effect=[
                        frozen_manifest,
                        {"campaign_id": "current-campaign"},
                    ],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_ordered_capture_attempts",
                    return_value=[(attempt_root, {})],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
            ):
                reused = codex_upgrade._prior_complete_results(
                    current_dir,
                    Path("candidates/current-candidate"),
                    [job],
                    phase="candidate",
                    candidate_id="current-candidate",
                    identity=identity,
                    tool_identity=current_tool,
                    expected_reuse_job_ids=[job.job_id],
                    source_attempt_id=str(source["attempt_id"]),
                    source_campaign_dir=source_dir,
                    source_candidate_id=str(source["candidate_id"]),
                    allowed_high_risk_path_changes={
                        "codex_upgrade_timing_ledger.schema.json",
                        "run_candidate_core_capture.sh",
                    },
                    validated_current_production_sha256=(
                        codex_upgrade._tool_identity_side_digest_excluding(
                            current_tool,
                            "production",
                            codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
                        )
                    ),
                    allow_unbound_cross_campaign_preview=True,
                )

        self.assertEqual([item["id"] for item in reused], [job.job_id])
        self.assertEqual(
            reused[0]["incremental_result_key"],
            codex_upgrade._job_incremental_metadata(
                job,
                identity=identity,
                tool_identity=current_tool,
            )["result_key"],
        )

    def test_plan_identity_does_not_load_transition_from_cross_campaign_failure(
        self,
    ) -> None:
        """v9 跨 Campaign 失败源走低风险 capture-run，不借用前序 transition。"""

        def identity(sha256: str) -> dict[str, object]:
            entries = [{"path": "codex_upgrade.py", "sha256": sha256}]
            components = codex_upgrade._tool_component_identities(entries)
            return {
                "git_commit": None,
                "entry_count": 1,
                "files_sha256": codex_upgrade._fingerprint(
                    {"entries": entries}
                ),
                "entries": entries,
                "components": components["components"],
                **codex_upgrade._tool_identity_sides(entries),
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Cargo.lock").write_text("lock", encoding="utf-8")
            package = root / "package.tar.gz"
            package.write_bytes(b"package")
            before = identity("a" * 64)
            current = identity("b" * 64)
            package_identity = {"asset_sha256": "c" * 64}
            manifest = {
                "campaign_id": "campaign-new",
                "configuration": {
                    "target_source": str(source),
                    "target_package": str(package),
                },
                "official_identity": {
                    "source_tree_sha256": "d" * 64,
                    "cargo_lock_sha256": "e" * 64,
                    "package": package_identity,
                },
                "target_version": "0.151.0",
                "target_sha256": "f" * 64,
                "tool_identity": before,
            }
            controls: list[bool] = []
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_verify_control_receipts",
                    side_effect=lambda *args, require_active: controls.append(
                        require_active
                    ),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_directory_tree_digest",
                    return_value="d" * 64,
                ),
                mock.patch.object(
                    codex_upgrade, "file_sha256", return_value="e" * 64
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_codex_package",
                    return_value=package_identity,
                ),
                mock.patch.object(
                    codex_upgrade, "_tool_identity", return_value=current
                ),
                mock.patch.object(
                    codex_upgrade, "_load_phase_evaluation_transition"
                ) as load_transition,
                mock.patch.object(
                    codex_upgrade, "_record_evaluation_side_drift"
                ),
            ):
                impact = codex_upgrade._verify_plan_identity(
                    root,
                    manifest,
                    operation="capture-run",
                    attempt_root=root / "predecessor-attempt",
                    attempt={
                        "campaign_id": "campaign-old",
                        "status": "failed",
                    },
                )
            self.assertEqual(controls, [False, True])
            load_transition.assert_not_called()
            self.assertEqual(impact["kind"], "component_drift")
            self.assertEqual(impact["changed_components"], ["orchestrator"])

    def test_successor_job_coordinate_relocation_is_closed(self) -> None:
        current_campaign = "campaign-new"
        predecessor_campaign = "campaign-old"
        job = Job(
            job_id="candidate-h1-wire",
            phase="candidate",
            suites=("full",),
            description="坐标迁移测试",
            steps=(
                {
                    "argv": [
                        "true",
                        f"/capture/{current_campaign}/result.json",
                    ],
                    "environment": {"RUN_ID": current_campaign},
                    "timeout": 60,
                },
            ),
            evidence_roots=(f"/capture/{current_campaign}/evidence",),
            covers=(),
        )
        previous = codex_upgrade._job_execution_payload(job)
        previous["evidence_roots"] = [
            value.replace(current_campaign, predecessor_campaign)
            for value in previous["evidence_roots"]
        ]
        previous["steps"] = [
            {
                **step,
                "argv": [
                    value.replace(current_campaign, predecessor_campaign)
                    for value in step["argv"]
                ],
                "environment": {
                    key: value.replace(current_campaign, predecessor_campaign)
                    for key, value in step["environment"].items()
                },
            }
            for step in previous["steps"]
        ]
        recorded = codex_upgrade._fingerprint(previous)
        frozen = {"id": job.job_id, "phase": job.phase}
        self.assertTrue(
            codex_upgrade._successor_job_execution_matches(
                job,
                recorded,
                frozen,
                current_campaign_id=current_campaign,
                predecessor_campaign_id=predecessor_campaign,
                current_candidate_id="candidate-a",
                predecessor_candidate_id="candidate-a",
            )
        )
        changed_job = Job(
            **{
                **job.__dict__,
                "steps": (
                    {
                        "argv": ["false"],
                        "environment": {},
                        "timeout": 60,
                    },
                ),
            }
        )
        self.assertFalse(
            codex_upgrade._successor_job_execution_matches(
                changed_job,
                recorded,
                frozen,
                current_campaign_id=current_campaign,
                predecessor_campaign_id=predecessor_campaign,
                current_candidate_id="candidate-a",
                predecessor_candidate_id="candidate-a",
            )
        )

    @staticmethod
    def _legacy_default_codex_bin_fixture() -> tuple[
        Job,
        str,
        dict[str, object],
        dict[str, object],
    ]:
        """构造仅缺少历史缺省 CODEX_BIN 的后继复用夹具。"""

        current_campaign = "campaign-new"
        predecessor_campaign = "campaign-old"
        current_candidate = "candidate-new"
        predecessor_candidate = "candidate-old"
        script_sha256 = (
            "6cd0a9f9cff4d600ddfa39a572274638b86ecb9a9443519a5b114be08242219f"
        )
        job = Job(
            job_id="candidate-core-direct",
            phase="candidate",
            suites=("core", "full"),
            description="历史缺省 CODEX_BIN 兼容测试",
            steps=(
                {
                    "argv": [
                        "bash",
                        "/workspace/tools/official_client_capture/"
                        "run_sub2api_direct_matrix.sh",
                    ],
                    "environment": {
                        "CODEX_VERSION": "0.151.0",
                        "CODEX_BIN": "/opt/codex-0.151.0/bin/codex",
                        "RUN_ID": f"{current_campaign}-{current_candidate}",
                    },
                    "timeout_seconds": 3600,
                },
            ),
            evidence_roots=(
                f"/capture/{current_campaign}/{current_candidate}",
            ),
            covers=(),
        )
        legacy_step = dict(job.steps[0])
        legacy_environment = dict(legacy_step["environment"])
        legacy_environment.pop("CODEX_BIN")
        legacy_step["environment"] = legacy_environment
        legacy_job = Job(**{**job.__dict__, "steps": (legacy_step,)})
        frozen_job = codex_upgrade._job_execution_payload(legacy_job)
        encoded = json.dumps(frozen_job, ensure_ascii=False)
        encoded = encoded.replace(current_campaign, predecessor_campaign)
        encoded = encoded.replace(current_candidate, predecessor_candidate)
        frozen_job = json.loads(encoded)
        recorded_sha256 = codex_upgrade._fingerprint(frozen_job)
        relay_sha256 = "a" * 64
        frozen_manifest: dict[str, object] = {
            "target_version": "0.151.0",
            "jobs": [frozen_job],
            "tool_identity": {
                "components": {
                    "relay": {
                        "sha256": relay_sha256,
                        "entries": [
                            {
                                "path": "run_sub2api_direct_matrix.sh",
                                "sha256": script_sha256,
                            }
                        ],
                    }
                }
            },
        }
        result: dict[str, object] = {
            "tool_components": ["relay"],
            "tool_component_digests": {"relay": relay_sha256},
        }
        return job, recorded_sha256, frozen_manifest, result

    def test_legacy_default_codex_bin_execution_matches_exact_history(
        self,
    ) -> None:
        """逐字脚本把历史缺省路径展开为同一路径时允许只读复用。"""

        job, recorded, manifest, result = (
            self._legacy_default_codex_bin_fixture()
        )
        self.assertTrue(
            codex_upgrade._legacy_default_codex_bin_execution_matches(
                job,
                recorded,
                manifest,
                result,
                authorized_job_ids=codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS,
                current_campaign_id="campaign-new",
                predecessor_campaign_id="campaign-old",
                current_candidate_id="candidate-new",
                predecessor_candidate_id="candidate-old",
            )
        )

    def test_legacy_default_codex_bin_execution_fails_closed_on_drift(
        self,
    ) -> None:
        """脚本、路径、额外环境或 transition 范围变化均不得兼容。"""

        base_job, recorded, base_manifest, base_result = (
            self._legacy_default_codex_bin_fixture()
        )

        def matches(
            job: Job,
            manifest: dict[str, object],
            *,
            authorized: frozenset[str] = (
                codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS
            ),
        ) -> bool:
            return codex_upgrade._legacy_default_codex_bin_execution_matches(
                job,
                recorded,
                manifest,
                base_result,
                authorized_job_ids=authorized,
                current_campaign_id="campaign-new",
                predecessor_campaign_id="campaign-old",
                current_candidate_id="candidate-new",
                predecessor_candidate_id="candidate-old",
            )

        with self.subTest(change="script-sha256"):
            manifest = json.loads(json.dumps(base_manifest))
            manifest["tool_identity"]["components"]["relay"]["entries"][0][
                "sha256"
            ] = "b" * 64
            self.assertFalse(matches(base_job, manifest))

        with self.subTest(change="codex-bin-path"):
            step = dict(base_job.steps[0])
            environment = dict(step["environment"])
            environment["CODEX_BIN"] = "/opt/codex-0.149.1/bin/codex"
            step["environment"] = environment
            changed = Job(**{**base_job.__dict__, "steps": (step,)})
            self.assertFalse(matches(changed, base_manifest))

        with self.subTest(change="extra-environment"):
            step = dict(base_job.steps[0])
            environment = dict(step["environment"])
            environment["UNAPPROVED"] = "1"
            step["environment"] = environment
            changed = Job(**{**base_job.__dict__, "steps": (step,)})
            self.assertFalse(matches(changed, base_manifest))

        with self.subTest(change="transition-job-set"):
            self.assertFalse(
                matches(
                    base_job,
                    base_manifest,
                    authorized=frozenset({"candidate-core-direct"}),
                )
            )

    def test_successor_rebinds_runtime_target_scenario_and_replays_v9(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            predecessor_root = root / "predecessor"
            arguments = self._campaign_arguments(predecessor_root)
            original = json.loads(
                arguments.target_scenario_manifest.read_text(encoding="utf-8")
            )
            predecessor_scenario = self._add_runtime_codex_jobs(
                original,
                bind_codex_bin=False,
            )
            self._write_json(
                arguments.target_scenario_manifest,
                predecessor_scenario,
            )
            predecessor_rehearsal_root = (
                predecessor_root / "control" / "runtime-job-rehearsal"
            )
            predecessor_contract = (
                codex_upgrade._job_rehearsal_contract_from_arguments(arguments)
            )
            predecessor_rehearsal = create_job_rehearsal_receipt(
                predecessor_rehearsal_root,
                contract=predecessor_contract,
                preflight_campaign_id="runtime-predecessor-preflight",
            )
            arguments.job_rehearsal_root = predecessor_rehearsal_root
            arguments.job_rehearsal_receipt = predecessor_rehearsal
            codex_upgrade.create_campaign(arguments)
            predecessor_dir = arguments.campaign_dir
            predecessor_manifest = codex_upgrade.load_campaign_manifest(
                predecessor_dir
            )
            self._seal_official_stage(
                predecessor_root,
                predecessor_dir,
                predecessor_manifest,
            )
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(predecessor_root)
            )
            return_code, _, stderr = self._approve_classification(
                predecessor_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)

            successor_scenario_path = root / "target-scenarios-current.json"
            successor_scenario = self._add_runtime_codex_jobs(
                original,
                bind_codex_bin=True,
            )
            self._write_json(successor_scenario_path, successor_scenario)
            arguments.target_scenario_manifest = successor_scenario_path
            arguments.codex_account_id = 91
            successor_contract = (
                codex_upgrade._job_rehearsal_contract_from_arguments(arguments)
            )
            successor_rehearsal_root = root / "successor-job-rehearsal"
            successor_rehearsal = create_job_rehearsal_receipt(
                successor_rehearsal_root,
                contract=successor_contract,
                preflight_campaign_id="runtime-successor-preflight",
            )
            successor_dir = root / "successor"
            return_code, stdout, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-runtime-scenario-successor",
                    "--codex-account-id",
                    "91",
                    "--reason",
                    "candidate_runtime_identity_correction",
                    "--target-scenario-manifest",
                    str(successor_scenario_path),
                    "--job-rehearsal-root",
                    str(successor_rehearsal_root),
                    "--job-rehearsal-receipt",
                    str(successor_rehearsal),
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertTrue(result["target_scenario_rebound"])
            self.assertTrue(result["job_rehearsal_rebound"])
            self.assertEqual(
                codex_upgrade.campaign_status(successor_dir)["status"],
                "profile_approved",
            )
            successor_manifest = codex_upgrade.load_campaign_manifest(
                successor_dir
            )
            receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                receipt["schema_version"],
                codex_upgrade.PREDECESSOR_RUNTIME_SCENARIO_IMPORT_SCHEMA,
            )
            self.assertEqual(
                receipt["target_scenario_transition"]["changed_job_ids"],
                sorted(codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS),
            )
            self.assertIn(
                "runtime_target_scenario",
                {item["kind"] for item in receipt["copied_files"]},
            )
            schema = json.loads(
                Path(codex_upgrade.__file__)
                .with_name("codex_upgrade_predecessor_import.schema.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(set(receipt), set(schema["required"]))
            self.assertEqual(
                schema["properties"]["schema_version"]["const"],
                codex_upgrade.PREDECESSOR_RUNTIME_SCENARIO_IMPORT_SCHEMA,
            )
            frozen_target = successor_dir / successor_manifest["inputs"][
                "target_discovery_scenarios"
            ]["path"]
            self.assertEqual(
                codex_upgrade.file_sha256(frozen_target),
                successor_manifest["inputs"]["target_discovery_scenarios"][
                    "sha256"
                ],
            )

    def test_successor_carries_forward_official_and_classification_read_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            predecessor_official = codex_upgrade._load_stage_result(
                predecessor_dir, "capture-official"
            )
            predecessor_classification = codex_upgrade._load_stage_result(
                predecessor_dir, "classify"
            )
            successor_dir = root / "successor"
            return_code, stdout, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-successor",
                    "--codex-account-id",
                    "91",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "profile_approved")
            self.assertFalse(result["official_recapture_required"])
            self.assertEqual(result["codex_account_id"], 91)

            successor_manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            self.assertEqual(
                successor_manifest["predecessor"]["campaign_id"],
                predecessor_manifest["campaign_id"],
            )
            self.assertEqual(
                predecessor_manifest["configuration"]["codex_account_id"], 90
            )
            self.assertEqual(
                successor_manifest["configuration"]["codex_account_id"], 91
            )
            import_receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                import_receipt["schema_version"],
                codex_upgrade.PREDECESSOR_IMPORT_SCHEMA,
            )
            self.assertEqual(
                import_receipt["configuration_transition"],
                {
                    "codex_account_id": {
                        "predecessor": 90,
                        "successor": 91,
                        "reason": "operator_selected_active_account",
                    }
                },
            )
            self.assertEqual(
                successor_manifest["official_identity"],
                predecessor_manifest["official_identity"],
            )
            stored_official = json.loads(
                (successor_dir / "official" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("predecessor_import", stored_official)
            self.assertNotIn("attempt", stored_official)
            self.assertEqual(
                stored_official["surface"]["path"],
                "imports/official/surface.json",
            )
            self.assertFalse((successor_dir / "official" / "attempts").exists())

            official = codex_upgrade._load_stage_result(
                successor_dir, "capture-official"
            )
            classification = codex_upgrade._load_stage_result(
                successor_dir, "classify"
            )
            self.assertEqual(official["identity"], predecessor_official["identity"])
            self.assertEqual(
                official["evidence_inventory"],
                predecessor_official["evidence_inventory"],
            )
            self.assertNotEqual(
                official["package_digest"],
                predecessor_official["package_digest"],
            )
            self.assertEqual(
                classification["joint_manifest_sha256"],
                predecessor_classification["joint_manifest_sha256"],
            )
            self.assertNotEqual(
                classification["package_digest"],
                predecessor_classification["package_digest"],
            )
            self.assertFalse((successor_dir / "official" / "attempts").exists())

    def test_successor_rebinds_current_job_rehearsal_after_tool_change(
        self,
    ) -> None:
        """产出侧工具变化后必须用当前合同的新演练收据建立后继。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            current_identity = codex_upgrade._tool_identity()
            entries = [dict(item) for item in current_identity["entries"]]
            entries.append(
                {
                    "path": "synthetic-output-tool.py",
                    "sha256": "f" * 64,
                }
            )
            entries.sort(key=lambda item: item["path"])
            successor_identity = {
                **current_identity,
                "entry_count": len(entries),
                "files_sha256": codex_upgrade._fingerprint(
                    {"entries": entries}
                ),
                "entries": entries,
                **codex_upgrade._tool_identity_sides(entries),
            }
            successor_contract_manifest = json.loads(
                json.dumps(predecessor_manifest)
            )
            successor_contract_manifest["tool_identity"] = successor_identity
            successor_contract_manifest["configuration"]["codex_account_id"] = 91
            contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                predecessor_dir,
                successor_contract_manifest,
            )
            rehearsal_root = root / "control" / "successor-rehearsal"

            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                rehearsal_receipt = create_job_rehearsal_receipt(
                    rehearsal_root,
                    contract=contract,
                    preflight_campaign_id="recovery-preflight-fixture",
                )
                rejected_dir = root / "successor-without-rehearsal"
                return_code, _, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(rejected_dir),
                        "--campaign-id",
                        "upgrade-0146-successor-rejected",
                        "--codex-account-id",
                        "91",
                        "--reason",
                        "candidate_runtime_identity_correction",
                    ]
                )
                self.assertEqual(return_code, 1)
                self.assertIn("当前执行合同", stderr)
                self.assertFalse(rejected_dir.exists())

                successor_dir = root / "successor-with-rehearsal"
                return_code, stdout, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        "upgrade-0146-successor-rebound",
                        "--codex-account-id",
                        "91",
                        "--reason",
                        "candidate_runtime_identity_correction",
                        "--job-rehearsal-root",
                        str(rehearsal_root),
                        "--job-rehearsal-receipt",
                        str(rehearsal_receipt),
                    ]
                )
                self.assertEqual(return_code, 0, stderr)
                self.assertTrue(json.loads(stdout)["job_rehearsal_rebound"])
                successor_manifest = codex_upgrade.load_campaign_manifest(
                    successor_dir
                )

            predecessor_control = predecessor_manifest["control_receipts"][
                "job_rehearsal"
            ]
            successor_control = successor_manifest["control_receipts"][
                "job_rehearsal"
            ]
            self.assertNotEqual(successor_control, predecessor_control)
            self.assertEqual(
                successor_control["execution_contract_sha256"],
                codex_upgrade_job_rehearsal_receipt.execution_contract_sha256(
                    contract
                ),
            )
            import_receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                import_receipt["schema_version"],
                codex_upgrade.PREDECESSOR_REHEARSAL_IMPORT_SCHEMA,
            )
            self.assertEqual(
                import_receipt["job_rehearsal_transition"],
                {
                    "reason": "current_execution_contract_rehearsal",
                    "predecessor": predecessor_control,
                    "successor": successor_control,
                },
            )

    def test_successor_rebinds_controls_after_predecessor_ledger_stops(
        self,
    ) -> None:
        """旧 Ledger 停线后，新演练、计时和 ARM64 控制必须来自同一 preflight。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_root = root / "predecessor"
            # 模拟 Formal 创建后规格章节发生合法维护、批准场景已按当前章节重做；
            # successor 必须在导入阶段闭环形成前仍能验证当前 rehearsal。
            with mock.patch.object(
                codex_upgrade,
                "source_spec_section_sha256",
                return_value="0" * 64,
            ):
                predecessor_dir, predecessor_manifest = self._create_campaign(
                    predecessor_root
                )
                self._seal_official_stage(
                    predecessor_root,
                    predecessor_dir,
                    predecessor_manifest,
                )
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(predecessor_root)
            )
            return_code, _, stderr = self._approve_classification(
                predecessor_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            frozen_scenario = json.loads(
                (
                    predecessor_dir
                    / predecessor_manifest["inputs"][
                        "target_discovery_scenarios"
                    ]["path"]
                ).read_text(encoding="utf-8")
            )
            approved_scenario = json.loads(
                scenario.read_text(encoding="utf-8")
            )
            self.assertNotEqual(
                frozen_scenario["source_spec"]["sha256"],
                approved_scenario["source_spec"]["sha256"],
            )
            predecessor_timing = predecessor_manifest["control_receipts"][
                "upgrade_timing"
            ]
            predecessor_ledger = Path(predecessor_timing["ledger_dir"])
            for index, phase in enumerate(("VC-0", "VC-1", "VC-2", "VC-3")):
                codex_upgrade_timing_ledger.append_event(
                    predecessor_ledger,
                    event_id=f"complete-{phase.lower()}",
                    phase=phase,
                    event_type="stage_completed",
                )
                if index == 3:
                    # 改造 2：候选级阶段开工前账本必须先激活 r1。
                    codex_upgrade_timing_ledger.append_event(
                        predecessor_ledger,
                        event_id="stage-revision-r1",
                        phase="VC-4",
                        event_type="stage_revision",
                        revision=1,
                        candidate_id="cand-1",
                        revision_commit_sha256="6" * 64,
                    )
                codex_upgrade_timing_ledger.append_event(
                    predecessor_ledger,
                    event_id=f"start-vc{index + 1}",
                    phase=f"VC-{index + 1}",
                    event_type="stage_started",
                )
            codex_upgrade_timing_ledger.append_event(
                predecessor_ledger,
                event_id="vc4-stop-fixture",
                phase="VC-4",
                event_type="stop_the_line",
                root_cause_id="wall-clock-fixture",
                next_action="建立恢复 preflight 和 successor",
            )
            stop_relative = "receipts/stop.json"
            codex_upgrade_timing_ledger.checkpoint(
                predecessor_ledger,
                stop_relative,
            )
            stop_receipt = predecessor_ledger / stop_relative

            preflight_arguments = self._campaign_arguments(
                root / "recovery-preflight",
                campaign_mode="preflight_only",
            )
            preflight_arguments.campaign_id = "recovery-preflight-fixture"
            preflight_arguments.campaign_dir = root / "recovery-preflight-campaign"
            recovery_timing_root = root / "recovery-control" / "timing"
            preflight_arguments.timing_ledger_dir = recovery_timing_root
            preflight_arguments.timing_receipt = create_timing_checkpoint(
                recovery_timing_root,
                upgrade_id=preflight_arguments.campaign_id,
                baseline_version=preflight_arguments.baseline_version,
                target_version=preflight_arguments.target_version,
                campaign_purpose=preflight_arguments.campaign_purpose,
            )
            recovery_arm_root = root / "recovery-control" / "arm64"
            preflight_arguments.arm64_environment_root = recovery_arm_root
            preflight_arguments.arm64_environment_receipt = create_arm_receipt(
                recovery_arm_root,
                phase="p0",
                subject_id=preflight_arguments.campaign_id,
                prefix="recovery-p0",
                rust_tls_codex_version=preflight_arguments.target_version,
            )
            preflight_manifest = codex_upgrade.create_campaign(
                preflight_arguments
            )
            contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                preflight_arguments.campaign_dir,
                preflight_manifest,
            )
            rehearsal_root = root / "recovery-rehearsal"
            rehearsal_receipt = create_job_rehearsal_receipt(
                rehearsal_root,
                contract=contract,
                preflight_campaign_id=preflight_manifest["campaign_id"],
                preflight_campaign_dir=preflight_arguments.campaign_dir,
                preflight_manifest_sha256=codex_upgrade.file_sha256(
                    preflight_arguments.campaign_dir / "campaign.json"
                ),
            )

            successor_dir = root / "successor"
            return_code, stdout, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-recovery-successor",
                    "--codex-account-id",
                    "90",
                    "--reason",
                    "candidate_runtime_identity_correction",
                    "--job-rehearsal-root",
                    str(rehearsal_root),
                    "--job-rehearsal-receipt",
                    str(rehearsal_receipt),
                    "--recovery-timing-ledger-dir",
                    str(preflight_arguments.timing_ledger_dir),
                    "--recovery-timing-receipt",
                    str(preflight_arguments.timing_receipt),
                    "--recovery-arm64-environment-root",
                    str(preflight_arguments.arm64_environment_root),
                    "--recovery-arm64-environment-receipt",
                    str(preflight_arguments.arm64_environment_receipt),
                    "--predecessor-stop-ledger-dir",
                    str(predecessor_ledger),
                    "--predecessor-stop-receipt",
                    str(stop_receipt),
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertTrue(
                json.loads(stdout)["recovery_controls_rebound"]
            )
            successor_manifest = codex_upgrade.load_campaign_manifest(
                successor_dir
            )
            self.assertEqual(
                successor_manifest["control_receipts"]["upgrade_timing"],
                preflight_manifest["control_receipts"]["upgrade_timing"],
            )
            import_receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                import_receipt["schema_version"],
                codex_upgrade.PREDECESSOR_RECOVERY_IMPORT_SCHEMA,
            )
            # 8 条阶段事件 + 1 条 stage_revision（改造 2）+ 1 条 stop_the_line = 11。
            self.assertEqual(
                import_receipt["recovery_control_transition"]["stop_checkpoint"][
                    "head_sequence"
                ],
                11,
            )

    def test_campaign_fixture_package_is_independent_of_wall_clock(self) -> None:
        """合成包不得因前序和恢复 preflight 跨秒而改变摘要。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(gzip.time, "time", return_value=1_000):
                first = self._campaign_arguments(
                    root / "first",
                    campaign_mode="preflight_only",
                )
            with mock.patch.object(gzip.time, "time", return_value=2_000):
                second = self._campaign_arguments(
                    root / "second",
                    campaign_mode="preflight_only",
                )
            self.assertEqual(
                first.target_package_sha256,
                second.target_package_sha256,
            )

    def test_successor_rebinds_live_attestation_compose_coordinates_immutably(
        self,
    ) -> None:
        """运行时纠正后继必须冻结新 compose 路径，并保留前序配置。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            compose_dir = root / "compose"
            compose_dir.mkdir(mode=0o700)
            compose_dir = compose_dir.resolve()
            base_compose = compose_dir / "docker-compose.yml"
            candidate_override = compose_dir / "candidate-r23.override.yml"
            base_compose.write_text("services: {}\n", encoding="utf-8")
            candidate_override.write_text("services: {}\n", encoding="utf-8")
            base_compose.chmod(0o600)
            candidate_override.chmod(0o600)
            compose_files = f"{base_compose} -f {candidate_override}"
            successor_dir = root / "successor"

            return_code, stdout, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-runtime-rebound",
                    "--codex-account-id",
                    "91",
                    "--reason",
                    "candidate_runtime_identity_correction",
                    "--live-attestation-compose-dir",
                    str(compose_dir),
                    "--live-attestation-compose-files",
                    compose_files,
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertTrue(json.loads(stdout)["runtime_configuration_rebound"])

            successor_manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            self.assertEqual(
                predecessor_manifest["configuration"].get(
                    "live_attestation_compose_files", ""
                ),
                "",
            )
            self.assertEqual(
                successor_manifest["configuration"][
                    "live_attestation_compose_dir"
                ],
                str(compose_dir),
            )
            self.assertEqual(
                successor_manifest["configuration"][
                    "live_attestation_compose_files"
                ],
                compose_files,
            )
            import_receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                import_receipt["schema_version"],
                codex_upgrade.PREDECESSOR_RUNTIME_IMPORT_SCHEMA,
            )
            self.assertEqual(
                import_receipt["configuration_transition"],
                {
                    "codex_account_id": {
                        "predecessor": 90,
                        "successor": 91,
                        "reason": "operator_selected_active_account",
                    },
                    "live_attestation_compose_dir": {
                        "predecessor": "",
                        "successor": str(compose_dir),
                        "reason": "candidate_runtime_identity_correction",
                    },
                    "live_attestation_compose_files": {
                        "predecessor": "",
                        "successor": compose_files,
                        "reason": "candidate_runtime_identity_correction",
                    },
                },
            )

    def test_successor_rejects_partial_or_reclassification_compose_rebinding(
        self,
    ) -> None:
        """不完整坐标及分类纠正后继均不得改变 Candidate 部署路径。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, _, _ = self._create_classified_campaign(
                root / "predecessor"
            )
            compose_dir = root / "compose"
            compose_dir.mkdir(mode=0o700)
            compose_dir = compose_dir.resolve()
            compose_file = compose_dir / "docker-compose.yml"
            compose_file.write_text("services: {}\n", encoding="utf-8")
            compose_file.chmod(0o600)
            common = [
                "successor",
                "--predecessor-campaign-dir",
                str(predecessor_dir),
                "--codex-account-id",
                "91",
            ]

            partial_dir = root / "partial"
            return_code, _, stderr = self._run_main(
                [
                    *common,
                    "--campaign-dir",
                    str(partial_dir),
                    "--campaign-id",
                    "upgrade-0146-partial-runtime",
                    "--reason",
                    "candidate_runtime_identity_correction",
                    "--live-attestation-compose-dir",
                    str(compose_dir),
                ]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("必须同时提供", stderr)
            self.assertFalse(partial_dir.exists())

            reclassification_dir = root / "reclassification"
            return_code, _, stderr = self._run_main(
                [
                    *common,
                    "--campaign-dir",
                    str(reclassification_dir),
                    "--campaign-id",
                    "upgrade-0146-reclassification-runtime",
                    "--reason",
                    "classification_fact_correction",
                    "--live-attestation-compose-dir",
                    str(compose_dir),
                    "--live-attestation-compose-files",
                    str(compose_file),
                ]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("只有 candidate_runtime_identity_correction", stderr)
            self.assertFalse(reclassification_dir.exists())

    def test_reclassification_successor_imports_only_official_stage(self) -> None:
        """批准事实纠正必须复用官方证据，但不得复制旧批准五件套。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            predecessor_official = codex_upgrade._load_stage_result(
                predecessor_dir, "capture-official"
            )
            successor_dir = root / "successor"
            with mock.patch.object(
                codex_upgrade,
                "load_scenario_jobs",
                wraps=codex_upgrade.load_scenario_jobs,
            ) as load_scenario_jobs:
                return_code, stdout, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        "upgrade-0146-reclassification-successor",
                        "--codex-account-id",
                        "92",
                        "--reason",
                        "classification_fact_correction",
                    ]
                )
            self.assertEqual(return_code, 0, stderr)
            self.assertTrue(
                any(
                    isinstance(
                        call.kwargs.get("historical_source_spec_binding"),
                        codex_upgrade.HistoricalSourceSpecBinding,
                    )
                    for call in load_scenario_jobs.call_args_list
                )
            )
            result = json.loads(stdout)
            self.assertEqual(result["status"], "official_sealed")
            self.assertFalse(result["official_recapture_required"])
            self.assertFalse(result["classification_imported"])
            self.assertTrue(result["classification_reapproval_required"])
            self.assertEqual(result["codex_account_id"], 92)

            successor_manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            self.assertEqual(
                successor_manifest["predecessor"]["campaign_id"],
                predecessor_manifest["campaign_id"],
            )
            self.assertEqual(
                successor_manifest["configuration"]["codex_account_id"], 92
            )
            self.assertFalse((successor_dir / "classification" / "result.json").exists())
            self.assertFalse((successor_dir / "classification" / "approved").exists())

            import_receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                import_receipt["schema_version"],
                codex_upgrade.PREDECESSOR_RECLASSIFICATION_IMPORT_SCHEMA,
            )
            self.assertEqual(
                import_receipt["import_mode"],
                "official_only_reclassification",
            )
            self.assertFalse(
                any(
                    item["kind"] == "approved_classification"
                    for item in import_receipt["copied_files"]
                )
            )

            replayed = codex_upgrade._load_stage_result(
                successor_dir, "capture-official"
            )
            self.assertEqual(
                replayed["evidence_inventory"],
                predecessor_official["evidence_inventory"],
            )
            self.assertEqual(
                replayed["security"], predecessor_official["security"]
            )
            approval_request = codex_upgrade.classify_campaign(
                successor_dir,
                target_rule_manifest=predecessor_dir.parent / "target-rules.json",
                migration_manifest=predecessor_dir.parent / "rule-migration.json",
                scenario_manifest=predecessor_dir.parent / "target-scenarios.json",
                profile_manifest=predecessor_dir.parent / "profile.json",
                assertion_profile_manifest=(
                    predecessor_dir.parent / "assertion-profile.json"
                ),
            )
            self.assertEqual(approval_request["status"], "approval_required")
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._load_stage_result(successor_dir, "classify")

    def test_sealed_stage_control_recovery_imports_only_official_stage(
        self,
    ) -> None:
        """多级导入的 transition-03 只能零执行承接，并完整重放最新收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest = self._create_campaign(
                root / "predecessor"
            )
            current_identity = codex_upgrade._tool_identity()
            entries = [
                {
                    **item,
                    "sha256": (
                        "f" * 64
                        if item["path"] == "codex_upgrade.py"
                        else item["sha256"]
                    ),
                }
                for item in current_identity["entries"]
            ]
            entries.sort(key=lambda item: item["path"])
            component_identity = codex_upgrade._tool_component_identities(
                entries
            )
            successor_identity = {
                **current_identity,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                "entries": entries,
                "components": component_identity["components"],
                "component_identity_sha256": codex_upgrade._fingerprint(
                    component_identity
                ),
                **codex_upgrade._tool_identity_sides(entries),
            }
            predecessor_controls = predecessor_manifest["control_receipts"]
            ledger_root = Path(
                predecessor_controls["upgrade_timing"]["ledger_dir"]
            )
            arm_root = Path(
                predecessor_controls["arm64_environment"]["evidence_root"]
            )
            arm_receipt = arm_root / predecessor_controls[
                "arm64_environment"
            ]["receipt"]["path"]
            codex_upgrade_timing_ledger.append_event(
                ledger_root,
                event_id="sealed-stage-complete-vc0",
                phase="VC-0",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_root,
                event_id="sealed-stage-start-vc1",
                phase="VC-1",
                event_type="stage_started",
            )
            vc1_relative = "receipts/sealed-stage-vc1.json"
            codex_upgrade_timing_ledger.checkpoint(
                ledger_root,
                vc1_relative,
            )
            vc1_receipt = ledger_root / vc1_relative
            vc1_control_arguments = argparse.Namespace(
                campaign_mode="preflight_only",
                campaign_purpose=predecessor_manifest["campaign_purpose"],
                baseline_version=predecessor_manifest["baseline_version"],
                target_version=predecessor_manifest["target_version"],
                timing_ledger_dir=ledger_root,
                timing_receipt=vc1_receipt,
                arm64_environment_root=arm_root,
                arm64_environment_receipt=arm_receipt,
            )
            vc1_controls = codex_upgrade._plan_control_receipts(
                vc1_control_arguments
            )
            vc1_controls["job_rehearsal"] = predecessor_controls[
                "job_rehearsal"
            ]
            recovery_controls = {
                "schema_version": (
                    codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA
                ),
                "predecessor": predecessor_manifest["control_receipts"],
                "stop_checkpoint": {
                    "reason": "测试只读承接已批准的恢复控制",
                },
                "recovery": vc1_controls,
            }
            self._seal_official_stage(
                root / "predecessor",
                predecessor_dir,
                predecessor_manifest,
                evaluation_transition_identity=successor_identity,
                evaluation_recovery_controls=recovery_controls,
            )
            (
                target,
                migration,
                scenario,
                profile,
                assertion_profile,
                _,
            ) = self._write_classification_manifests(root / "predecessor")
            return_code, _, stderr = self._approve_classification(
                predecessor_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            imported_predecessor_dir = root / "classification-successor"
            imported_arguments = codex_upgrade._build_parser().parse_args(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(imported_predecessor_dir),
                    "--campaign-id",
                    "upgrade-0146-classification-correction",
                    "--codex-account-id",
                    "92",
                    "--reason",
                    "classification_fact_correction",
                ]
            )
            imported_result = codex_upgrade.create_successor_campaign(
                imported_arguments
            )
            self.assertEqual(imported_result["status"], "official_sealed")
            predecessor_dir = imported_predecessor_dir
            predecessor_manifest = codex_upgrade.load_campaign_manifest(
                predecessor_dir
            )
            self.assertFalse(
                (
                    predecessor_dir
                    / "official"
                    / "attempts"
                    / "20260731T000000Z-1111111111111111"
                    / "evaluation-transition-03.json"
                ).exists()
            )
            predecessor_official = codex_upgrade._load_stage_result(
                predecessor_dir,
                "capture-official",
            )
            self.assertTrue(
                predecessor_official["evaluation_transition"]["path"].endswith(
                    "evaluation-transition-03.json"
                )
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_root,
                event_id="sealed-stage-complete-vc1",
                phase="VC-1",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_root,
                event_id="sealed-stage-start-vc2",
                phase="VC-2",
                event_type="stage_started",
            )
            vc2_relative = "receipts/sealed-stage-vc2.json"
            codex_upgrade_timing_ledger.checkpoint(
                ledger_root,
                vc2_relative,
            )
            vc2_receipt = ledger_root / vc2_relative
            preflight_arguments = self._campaign_arguments(
                root / "sealed-stage-preflight",
                campaign_mode="preflight_only",
            )
            preflight_arguments.timing_ledger_dir = ledger_root
            preflight_arguments.timing_receipt = vc2_receipt
            preflight_arguments.arm64_environment_root = arm_root
            preflight_arguments.arm64_environment_receipt = arm_receipt
            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                preflight_manifest = codex_upgrade.create_campaign(
                    preflight_arguments
                )
            successor_contract_manifest = json.loads(
                json.dumps(predecessor_manifest)
            )
            successor_contract_manifest["tool_identity"] = successor_identity
            successor_contract_manifest["control_receipts"] = json.loads(
                json.dumps(vc1_controls)
            )
            successor_contract_manifest["configuration"]["codex_account_id"] = 93
            contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                predecessor_dir,
                successor_contract_manifest,
            )
            rehearsal_root = root / "control" / "sealed-stage-rehearsal"
            successor_dir = root / "successor"
            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                rehearsal_receipt = create_job_rehearsal_receipt(
                    rehearsal_root,
                    contract=contract,
                    preflight_campaign_id=preflight_manifest["campaign_id"],
                    preflight_campaign_dir=preflight_arguments.campaign_dir,
                    preflight_manifest_sha256=codex_upgrade.file_sha256(
                        preflight_arguments.campaign_dir / "campaign.json"
                    ),
                )
                arguments = codex_upgrade._build_parser().parse_args(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        "upgrade-0146-sealed-stage-recovery",
                        "--codex-account-id",
                        "93",
                        "--reason",
                        "sealed_stage_control_recovery",
                        "--active-timing-ledger-dir",
                        str(ledger_root),
                        "--active-timing-receipt",
                        str(vc2_receipt),
                        "--active-arm64-environment-root",
                        str(arm_root),
                        "--active-arm64-environment-receipt",
                        str(arm_receipt),
                        "--job-rehearsal-root",
                        str(rehearsal_root),
                        "--job-rehearsal-receipt",
                        str(rehearsal_receipt),
                    ]
                )
                result = codex_upgrade.create_successor_campaign(arguments)
            self.assertEqual(result["status"], "official_sealed")
            self.assertTrue(result["sealed_stage_control_recovered"])
            self.assertFalse(result["classification_imported"])
            self.assertEqual(result["executed_job_count"], 0)
            self.assertEqual(result["scanned_bytes"], 0)
            self.assertEqual(result["live_request_count"], 0)
            successor_manifest = codex_upgrade.load_campaign_manifest(
                successor_dir
            )
            self.assertEqual(
                successor_manifest["control_receipts"]["upgrade_timing"][
                    "receipt"
                ]["path"],
                vc2_relative,
            )
            self.assertEqual(
                successor_manifest["control_receipts"]["arm64_environment"],
                predecessor_controls["arm64_environment"],
            )
            self.assertFalse(
                (successor_dir / "classification" / "result.json").exists()
            )
            receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                receipt["schema_version"],
                codex_upgrade.PREDECESSOR_SEALED_STAGE_RECOVERY_IMPORT_SCHEMA,
            )
            self.assertEqual(
                receipt["import_mode"],
                "official_only_sealed_stage_control_recovery",
            )
            self.assertEqual(
                receipt["execution_summary"],
                {
                    "executed_job_ids": [],
                    "scanned_bytes": 0,
                    "live_request_count": 0,
                },
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_verify_stage_evidence",
                    side_effect=AssertionError(
                        "导入 official 不得重新扫描原始证据"
                    ),
                ) as evidence_scan,
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=successor_identity,
                ),
            ):
                replayed = codex_upgrade._load_stage_result(
                    successor_dir,
                    "capture-official",
                    _ignore_checkpoint=True,
                    _skip_evidence_scan=True,
                )
                classification_draft = codex_upgrade.classify_campaign(
                    successor_dir
                )
            evidence_scan.assert_not_called()
            self.assertEqual(classification_draft["status"], "draft")
            self.assertEqual(
                replayed["evidence_inventory"],
                predecessor_official["evidence_inventory"],
            )
            self.assertEqual(
                replayed["security"],
                predecessor_official["security"],
            )

    def test_sealed_stage_control_mode_rejects_partial_or_mixed_coordinates(
        self,
    ) -> None:
        names = (
            "active_timing_ledger_dir",
            "active_timing_receipt",
            "active_arm64_environment_root",
            "active_arm64_environment_receipt",
            "recovery_timing_ledger_dir",
            "recovery_timing_receipt",
            "recovery_arm64_environment_root",
            "recovery_arm64_environment_receipt",
            "predecessor_stop_ledger_dir",
            "predecessor_stop_receipt",
        )
        arguments = argparse.Namespace(**{name: None for name in names})
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须选择"):
            codex_upgrade._sealed_stage_control_mode(arguments)

        arguments.active_timing_ledger_dir = Path("/tmp/active")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "完整提供"):
            codex_upgrade._sealed_stage_control_mode(arguments)

        arguments.recovery_timing_ledger_dir = Path("/tmp/recovery")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不得混用"):
            codex_upgrade._sealed_stage_control_mode(arguments)

    def test_sealed_stage_control_recovery_rebinds_stopped_ledger(
        self,
    ) -> None:
        """旧恢复 Ledger 停线后，只能绑定新 VC-2 控制并零执行导入。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest = self._create_campaign(
                root / "predecessor"
            )
            current_identity = codex_upgrade._tool_identity()
            entries = [
                {
                    **item,
                    "sha256": (
                        "f" * 64
                        if item["path"] == "codex_upgrade.py"
                        else item["sha256"]
                    ),
                }
                for item in current_identity["entries"]
            ]
            entries.sort(key=lambda item: item["path"])
            component_identity = codex_upgrade._tool_component_identities(entries)
            successor_identity = {
                **current_identity,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                "entries": entries,
                "components": component_identity["components"],
                "component_identity_sha256": codex_upgrade._fingerprint(
                    component_identity
                ),
                **codex_upgrade._tool_identity_sides(entries),
            }
            predecessor_controls = predecessor_manifest["control_receipts"]
            stopped_ledger = Path(
                predecessor_controls["upgrade_timing"]["ledger_dir"]
            )
            predecessor_arm_root = Path(
                predecessor_controls["arm64_environment"]["evidence_root"]
            )
            predecessor_arm_receipt = predecessor_arm_root / predecessor_controls[
                "arm64_environment"
            ]["receipt"]["path"]
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-complete-vc0",
                phase="VC-0",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-start-vc1",
                phase="VC-1",
                event_type="stage_started",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-attempt-started",
                phase="VC-1",
                event_type="attempt_started",
                attempt_id="official-r1",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-attempt-completed",
                phase="VC-1",
                event_type="attempt_completed",
                attempt_id="official-r1",
                live_request_count=29,
            )
            vc1_relative = "receipts/stopped-stage-vc1.json"
            codex_upgrade_timing_ledger.checkpoint(stopped_ledger, vc1_relative)
            vc1_receipt = stopped_ledger / vc1_relative
            vc1_controls = codex_upgrade._plan_control_receipts(
                argparse.Namespace(
                    campaign_mode="preflight_only",
                    campaign_purpose=predecessor_manifest["campaign_purpose"],
                    baseline_version=predecessor_manifest["baseline_version"],
                    target_version=predecessor_manifest["target_version"],
                    timing_ledger_dir=stopped_ledger,
                    timing_receipt=vc1_receipt,
                    arm64_environment_root=predecessor_arm_root,
                    arm64_environment_receipt=predecessor_arm_receipt,
                )
            )
            vc1_controls["job_rehearsal"] = predecessor_controls["job_rehearsal"]
            recovery_controls = {
                "schema_version": (
                    codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA
                ),
                "predecessor": predecessor_controls,
                "stop_checkpoint": {"reason": "测试封存后的停线恢复"},
                "recovery": vc1_controls,
            }
            self._seal_official_stage(
                root / "predecessor",
                predecessor_dir,
                predecessor_manifest,
                evaluation_transition_identity=successor_identity,
                evaluation_recovery_controls=recovery_controls,
            )
            predecessor_official = codex_upgrade._load_stage_result(
                predecessor_dir,
                "capture-official",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-complete-vc1",
                phase="VC-1",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-start-vc2",
                phase="VC-2",
                event_type="stage_started",
            )
            codex_upgrade_timing_ledger.append_event(
                stopped_ledger,
                event_id="stopped-stage-stop-vc2",
                phase="VC-2",
                event_type="stop_the_line",
                root_cause_id="sealed-stage-ledger-expired",
                next_action="绑定新 VC-2 Ledger/P0 后创建受管 successor",
            )
            stop_relative = "receipts/stopped-stage-stop.json"
            codex_upgrade_timing_ledger.checkpoint(stopped_ledger, stop_relative)
            stop_receipt = stopped_ledger / stop_relative

            recovery_id = "upgrade-0146-sealed-stopped-recovery"
            recovery_ledger = root / "recovery-control" / "timing"
            create_timing_checkpoint(
                recovery_ledger,
                upgrade_id=recovery_id,
                baseline_version=predecessor_manifest["baseline_version"],
                target_version=predecessor_manifest["target_version"],
                campaign_purpose=predecessor_manifest["campaign_purpose"],
            )
            codex_upgrade_timing_ledger.append_event(
                recovery_ledger,
                event_id="recovery-complete-vc0",
                phase="VC-0",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                recovery_ledger,
                event_id="recovery-start-vc1",
                phase="VC-1",
                event_type="stage_started",
            )
            codex_upgrade_timing_ledger.append_event(
                recovery_ledger,
                event_id="recovery-complete-vc1",
                phase="VC-1",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                recovery_ledger,
                event_id="recovery-start-vc2",
                phase="VC-2",
                event_type="stage_started",
            )
            recovery_vc2_relative = "receipts/recovery-vc2.json"
            codex_upgrade_timing_ledger.checkpoint(
                recovery_ledger,
                recovery_vc2_relative,
            )
            recovery_vc2_receipt = recovery_ledger / recovery_vc2_relative
            recovery_arm_root = root / "recovery-control" / "arm64"
            recovery_arm_receipt = create_arm_receipt(
                recovery_arm_root,
                phase="p0",
                subject_id=recovery_id,
                prefix="recovery-p0",
                rust_tls_codex_version=predecessor_manifest["target_version"],
            )
            preflight_arguments = self._campaign_arguments(
                root / "sealed-stopped-preflight",
                campaign_id=recovery_id,
                campaign_mode="preflight_only",
            )
            preflight_arguments.timing_ledger_dir = recovery_ledger
            preflight_arguments.timing_receipt = recovery_vc2_receipt
            preflight_arguments.arm64_environment_root = recovery_arm_root
            preflight_arguments.arm64_environment_receipt = recovery_arm_receipt
            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                preflight_manifest = codex_upgrade.create_campaign(
                    preflight_arguments
                )
            successor_contract_manifest = json.loads(
                json.dumps(predecessor_manifest)
            )
            successor_contract_manifest["tool_identity"] = successor_identity
            successor_contract_manifest["control_receipts"] = (
                codex_upgrade._plan_control_receipts(preflight_arguments)
            )
            successor_contract_manifest["control_receipts"]["job_rehearsal"] = (
                predecessor_controls["job_rehearsal"]
            )
            successor_contract_manifest["configuration"]["codex_account_id"] = 94
            contract = codex_upgrade._job_rehearsal_contract_from_manifest(
                predecessor_dir,
                successor_contract_manifest,
            )
            rehearsal_root = root / "recovery-control" / "job-rehearsal"
            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                rehearsal_receipt = create_job_rehearsal_receipt(
                    rehearsal_root,
                    contract=contract,
                    preflight_campaign_id=preflight_manifest["campaign_id"],
                    preflight_campaign_dir=preflight_arguments.campaign_dir,
                    preflight_manifest_sha256=codex_upgrade.file_sha256(
                        preflight_arguments.campaign_dir / "campaign.json"
                    ),
                )
            successor_dir = root / "successor"
            arguments = codex_upgrade._build_parser().parse_args(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-sealed-stopped-successor",
                    "--codex-account-id",
                    "94",
                    "--reason",
                    "sealed_stage_control_recovery",
                    "--predecessor-stop-ledger-dir",
                    str(stopped_ledger),
                    "--predecessor-stop-receipt",
                    str(stop_receipt),
                    "--recovery-timing-ledger-dir",
                    str(recovery_ledger),
                    "--recovery-timing-receipt",
                    str(recovery_vc2_receipt),
                    "--recovery-arm64-environment-root",
                    str(recovery_arm_root),
                    "--recovery-arm64-environment-receipt",
                    str(recovery_arm_receipt),
                    "--job-rehearsal-root",
                    str(rehearsal_root),
                    "--job-rehearsal-receipt",
                    str(rehearsal_receipt),
                ]
            )
            with mock.patch.object(
                codex_upgrade,
                "_tool_identity",
                return_value=successor_identity,
            ):
                result = codex_upgrade.create_successor_campaign(arguments)
            self.assertEqual(result["status"], "official_sealed")
            self.assertTrue(result["sealed_stage_control_recovered"])
            self.assertTrue(result["recovery_controls_rebound"])
            self.assertEqual(result["executed_job_count"], 0)
            self.assertEqual(result["scanned_bytes"], 0)
            self.assertEqual(result["live_request_count"], 0)
            successor_manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            self.assertEqual(
                successor_manifest["control_receipts"]["upgrade_timing"][
                    "receipt"
                ]["path"],
                recovery_vc2_relative,
            )
            self.assertEqual(
                successor_manifest["control_receipts"]["arm64_environment"][
                    "subject_id"
                ],
                recovery_id,
            )
            receipt = json.loads(
                (successor_dir / "predecessor-import.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                receipt["schema_version"],
                codex_upgrade.PREDECESSOR_SEALED_STAGE_RECOVERY_IMPORT_SCHEMA,
            )
            self.assertEqual(
                receipt["recovery_control_transition"]["stop_checkpoint"][
                    "total_live_request_count"
                ],
                29,
            )
            self.assertEqual(
                receipt["stage_control_transition"]["predecessor"],
                vc1_controls,
            )
            self.assertEqual(
                receipt["stage_control_transition"]["successor"],
                successor_manifest["control_receipts"],
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_verify_stage_evidence",
                    side_effect=AssertionError(
                        "stopped 恢复不得重新扫描原始证据"
                    ),
                ) as evidence_scan,
                mock.patch.object(
                    codex_upgrade,
                    "_tool_identity",
                    return_value=successor_identity,
                ),
            ):
                replayed = codex_upgrade._load_stage_result(
                    successor_dir,
                    "capture-official",
                    _ignore_checkpoint=True,
                    _skip_evidence_scan=True,
                )
                classification_draft = codex_upgrade.classify_campaign(
                    successor_dir
                )
            evidence_scan.assert_not_called()
            self.assertEqual(classification_draft["status"], "draft")
            self.assertEqual(
                replayed["evidence_inventory"],
                predecessor_official["evidence_inventory"],
            )

    def test_successor_replays_predecessor_through_compare_and_accept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_root = root / "predecessor"
            predecessor_dir, _, rules = self._create_classified_campaign(
                predecessor_root
            )
            successor_dir = root / "campaign"
            return_code, _, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-successor-accept",
                    "--codex-account-id",
                    "90",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            _, identity = self._seal_candidate_stage(root, successor_dir)
            comparison = codex_upgrade.compare_campaign(
                successor_dir, "candidate-a"
            )
            self.assertTrue(comparison["equal"])
            assertions = self._write_assertions(
                root,
                rules,
                identity,
                campaign_dir=successor_dir,
                official_evidence=(
                    predecessor_root / "official-evidence" / "surface.json"
                ),
            )
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = self._accept_campaign(
                    root,
                    successor_dir,
                    "candidate-a",
                    assertions,
                )
            self.assertTrue(acceptance["accepted"])
            self.assertEqual(
                codex_upgrade.campaign_status(
                    successor_dir, "candidate-a"
                )["status"],
                "ready",
            )

    def test_successor_replays_historical_v1_account_invariant_receipt(self) -> None:
        """历史 v1 收据没有账号过渡字段，只允许原账号逐字承接。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, _, _ = self._create_classified_campaign(
                root / "predecessor"
            )
            successor_dir = root / "successor"
            return_code, _, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0146-successor-v1-replay",
                    "--codex-account-id",
                    "90",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )
            self.assertEqual(return_code, 0, stderr)

            import_path = successor_dir / "predecessor-import.json"
            receipt = json.loads(import_path.read_text(encoding="utf-8"))
            receipt["schema_version"] = codex_upgrade.PREDECESSOR_IMPORT_SCHEMA_V1
            receipt.pop("configuration_transition")
            receipt.pop("receipt_digest")
            receipt["receipt_digest"] = codex_upgrade._fingerprint(receipt)
            self._write_json(import_path, receipt)

            manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            stage_payload = json.loads(
                (successor_dir / "official" / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            stage_payload["predecessor_import"]["sha256"] = (
                codex_upgrade.file_sha256(import_path)
            )
            original_loader = codex_upgrade.load_campaign_manifest
            predecessor_loads: list[bool] = []

            def historical_loader(path: Path, **kwargs: object):
                resolved = (
                    path.parent if path.name == "campaign.json" else path
                ).resolve()
                if resolved == predecessor_dir.resolve():
                    predecessor_loads.append(
                        kwargs.get("_control_epoch_bootstrap") is True
                    )
                return original_loader(path, **kwargs)

            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                side_effect=historical_loader,
            ):
                replayed = codex_upgrade._validate_predecessor_import_receipt(
                    successor_dir,
                    manifest,
                    stage_payload,
                    "capture-official",
                    frozenset(),
                )
            self.assertEqual(replayed["status"], "complete")
            self.assertTrue(predecessor_loads)
            self.assertTrue(predecessor_loads[0])

    def test_successor_replays_historical_stage_without_rebinding_finalizer(
        self,
    ) -> None:
        """历史机器收据保留原 finalizer 身份，后继只重放阶段封印。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, _, _ = self._create_classified_campaign(
                root / "predecessor"
            )
            successor_dir = root / "successor"
            with mock.patch.object(
                codex_upgrade,
                "_replay_capture_stage_receipts",
                side_effect=codex_upgrade.ConfigurationError(
                    "历史 finalizer 不得被当前路径重新绑定"
                ),
            ) as replay:
                return_code, stdout, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        "upgrade-0146-successor-historical-finalizer",
                        "--codex-account-id",
                        "90",
                        "--reason",
                        "candidate_runtime_identity_correction",
                    ]
                )
                self.assertEqual(return_code, 0, stderr)
                self.assertEqual(json.loads(stdout)["status"], "profile_approved")
                self.assertEqual(
                    codex_upgrade.campaign_status(successor_dir)["status"],
                    "profile_approved",
                )
            replay.assert_not_called()

    def test_successor_chain_rejects_second_successor_with_same_reason(
        self,
    ) -> None:
        """同一根因只允许一次后继，第二层必须停线以阻断 rN 循环。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, _, _ = self._create_classified_campaign(
                root / "predecessor"
            )
            first_successor = root / "successor-one"
            second_successor = root / "successor-two"
            return_code, _, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(first_successor),
                    "--campaign-id",
                    "upgrade-0146-successor-level-one",
                    "--codex-account-id",
                    "90",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            first_manifest = codex_upgrade.load_campaign_manifest(first_successor)
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                side_effect=AssertionError(
                    "successor 根因查重不得重放历史 Campaign 控制链"
                ),
            ):
                codex_upgrade._reject_repeated_successor_reason(
                    first_successor,
                    first_manifest,
                    "candidate_failed_job_tool_recovery",
                )
            campaign_schema = json.loads(
                Path(codex_upgrade.__file__)
                .with_name("codex_upgrade_campaign.schema.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(
                    campaign_schema["$defs"]["predecessor"]["properties"][
                        "reason"
                    ]["enum"]
                ),
                set(codex_upgrade.SUCCESSOR_REASONS),
            )

            return_code, _, stderr = self._run_main(
                [
                    "successor",
                    "--predecessor-campaign-dir",
                    str(first_successor),
                    "--campaign-dir",
                    str(second_successor),
                    "--campaign-id",
                    "upgrade-0146-successor-level-two",
                    "--codex-account-id",
                    "91",
                    "--reason",
                    "candidate_runtime_identity_correction",
                ]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("第二层必须停线", stderr)
            self.assertFalse(second_successor.exists())

    def test_successor_replays_inherited_incremental_noop_without_job_rerun(
        self,
    ) -> None:
        """分类纠正后继必须用 no-op 追溯 passed 事实，不得要求重跑 Job。"""

        predecessor_control = {
            "evidence_root": "/control/noop",
            "receipt": {"path": "receipt.json"},
        }
        successor_manifest = {
            "campaign_mode": "formal",
            "predecessor": {"reason": "classification_fact_correction"},
            "control_receipts": {"job_rehearsal": predecessor_control},
        }
        arguments = argparse.Namespace(
            job_rehearsal_root=None,
            job_rehearsal_receipt=None,
            recovery_timing_ledger_dir=None,
        )
        with (
            mock.patch.object(
                codex_upgrade,
                "_job_rehearsal_contract_from_manifest",
                return_value={"job_count": 1},
            ),
            mock.patch.object(
                codex_upgrade,
                "_job_rehearsal_control_from_receipt",
                return_value=predecessor_control,
            ) as replay,
        ):
            transition = codex_upgrade._successor_job_rehearsal_transition(
                arguments,
                Path("/campaign/.successor-staging"),
                successor_manifest,
            )
        self.assertIsNone(transition)
        self.assertTrue(replay.call_args.kwargs["allow_incremental_noop"])

    def test_failed_job_tool_recovery_requires_complete_control_bindings(
        self,
    ) -> None:
        """失败 Job 工具恢复不能借新原因绕过 attempt 与恢复控制门禁。"""

        for reason in (
            "candidate_failed_job_tool_recovery",
            "candidate_recovery_control_refresh",
        ):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                predecessor_dir, _, _ = self._create_classified_campaign(
                    root / "predecessor"
                )
                successor_dir = root / "successor"
                return_code, _, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        "upgrade-0146-failed-job-tool-recovery",
                        "--codex-account-id",
                        "90",
                        "--reason",
                        reason,
                    ]
                )
                self.assertEqual(return_code, 1)
                self.assertIn("必须同时绑定前序失败 attempt", stderr)
                self.assertFalse(successor_dir.exists())

    def test_control_refresh_ignores_only_historical_source_spec_metadata(self) -> None:
        """历史基线章节摘要变化不得扩大失败 Job 闭集，其他未知文件仍停线。"""

        def identity(files: dict[str, str]) -> dict[str, object]:
            entries = [
                {"path": path, "sha256": digest}
                for path, digest in sorted(files.items())
            ]
            components = codex_upgrade._tool_component_identities(entries)
            return {
                "entries": entries,
                "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
                "components": components["components"],
                **codex_upgrade._tool_identity_sides(entries),
            }

        source_files = {
            "candidate_rule_expectations_0_149_1.json": "1" * 64,
            "codex_upgrade_scenarios_0_149_1.json": "2" * 64,
            "run_sub2api_openai_mitm_matrix.sh": "3" * 64,
            "codex_upgrade_scenarios_0_151_0.json": "9" * 64,
        }
        current_files = {
            "candidate_rule_expectations_0_149_1.json": "4" * 64,
            "codex_upgrade_scenarios_0_149_1.json": "5" * 64,
            "run_sub2api_openai_mitm_matrix.sh": "6" * 64,
            "codex_upgrade_scenarios_0_151_0.json": "a" * 64,
        }
        predecessor = {
            "target_version": "0.151.0",
            "tool_identity": identity(source_files),
        }
        successor = {
            "target_version": "0.151.0",
            "tool_identity": identity(current_files),
        }
        attempt = {"status": "failed"}
        scope = {
            "failed_job_ids": ["candidate-compact-mitm", "candidate-core-mitm"],
            "execute_job_ids": ["candidate-compact-mitm", "candidate-core-mitm"],
        }
        with (
            mock.patch.object(
                codex_upgrade,
                "_load_capture_attempt",
                return_value=(Path("/campaign/attempt"), attempt),
            ),
            mock.patch.object(
                codex_upgrade,
                "_phase_evaluation_recovery_scope",
                return_value=scope,
            ),
            mock.patch.object(
                codex_upgrade,
                "_target_scenario_source_spec_only_drift",
                return_value=True,
            ),
        ):
            codex_upgrade._validate_failed_job_tool_recovery_source(
                Path("/campaign"),
                predecessor,
                successor,
                candidate_id="candidate-a",
                attempt_id="attempt-a",
                reason="candidate_recovery_control_refresh",
            )

            unsafe_source = dict(source_files)
            unsafe_current = dict(current_files)
            unsafe_source["unknown_producer.py"] = "7" * 64
            unsafe_current["unknown_producer.py"] = "8" * 64
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "未登记的产出侧工具变化",
            ):
                codex_upgrade._validate_failed_job_tool_recovery_source(
                    Path("/campaign"),
                    {"tool_identity": identity(unsafe_source)},
                    {"tool_identity": identity(unsafe_current)},
                    candidate_id="candidate-a",
                    attempt_id="attempt-a",
                    reason="candidate_recovery_control_refresh",
                )

    def test_target_scenario_metadata_exception_rejects_job_change(self) -> None:
        """目标场景只允许 source_spec 摘要变化，执行字段变化仍必须停线。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_path = root / "inputs" / "target-discovery-scenarios.json"
            frozen_path.parent.mkdir(parents=True)
            current_path = Path(codex_upgrade.__file__).with_name(
                "codex_upgrade_scenarios_0_151_0.json"
            )
            current = json.loads(current_path.read_text(encoding="utf-8"))
            frozen = json.loads(json.dumps(current, ensure_ascii=False))
            frozen["source_spec"]["sha256"] = "0" * 64
            self._write_json(frozen_path, frozen)
            manifest = {
                "target_version": "0.151.0",
                "inputs": {
                    "target_discovery_scenarios": {
                        "path": "inputs/target-discovery-scenarios.json",
                        "sha256": codex_upgrade.file_sha256(frozen_path),
                    }
                },
            }
            self.assertTrue(
                codex_upgrade._target_scenario_source_spec_only_drift(root, manifest)
            )

            for field in ("argv", "environment", "timeout_seconds"):
                with self.subTest(field=field):
                    changed = json.loads(json.dumps(frozen, ensure_ascii=False))
                    step = changed["capture_jobs"][0]["steps"][0]
                    if field == "argv":
                        step[field] = ["false"]
                    elif field == "environment":
                        step[field] = {"UNAUTHORIZED": "1"}
                    else:
                        step[field] = int(step[field]) + 1
                    self._write_json(frozen_path, changed)
                    manifest["inputs"]["target_discovery_scenarios"]["sha256"] = (
                        codex_upgrade.file_sha256(frozen_path)
                    )
                    self.assertFalse(
                        codex_upgrade._target_scenario_source_spec_only_drift(
                            root,
                            manifest,
                        )
                    )

    def test_campaign_jobs_replays_only_historical_source_digest(self) -> None:
        """恢复预览可读历史摘要，但不得放宽批准场景的其他字节。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        historical = json.loads(json.dumps(managed, ensure_ascii=False))
        historical["source_spec"]["sha256"] = "0" * 64

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            frozen_path = campaign_dir / "inputs" / "target.json"
            approved_path = campaign_dir / "classification" / "approved.json"
            self._write_json(frozen_path, historical)
            self._write_json(approved_path, historical)
            scenario_reference = self._binding(
                approved_path,
                "classification/approved.json",
            )
            manifest = {
                "target_version": "0.151.0",
                "baseline_version": "0.149.1",
                "suite": "full",
                "predecessor": {
                    "reason": "candidate_recovery_control_refresh",
                },
                "inputs": {
                    "target_discovery_scenarios": self._binding(
                        frozen_path,
                        "inputs/target.json",
                    ),
                },
            }
            arguments = argparse.Namespace(
                scenario_manifest=frozen_path,
                extra_jobs=None,
            )
            job = Job(
                job_id="candidate-test",
                phase="candidate",
                suites=("full",),
                description="测试历史摘要读取",
                steps=(),
                evidence_roots=(),
                covers=(),
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_campaign_arguments",
                    return_value=arguments,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={
                        "status": "complete",
                        "scenario_manifest": scenario_reference,
                    },
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_job_context",
                    return_value={},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "load_scenario_jobs",
                    return_value=[job],
                ) as load_jobs,
                mock.patch.object(
                    codex_upgrade,
                    "_approved_rules",
                    return_value=(),
                ),
                mock.patch.object(codex_upgrade, "_validate_jobs"),
            ):
                jobs = codex_upgrade._campaign_jobs(
                    campaign_dir,
                    manifest,
                    "candidate",
                )

            self.assertEqual(jobs, [job])
            self.assertEqual(
                load_jobs.call_args.kwargs["historical_source_spec_binding"],
                codex_upgrade._scenario_source_spec_binding(
                    historical,
                    label="测试历史场景",
                ),
            )

    def test_runtime_transition_combines_approved_semantics_and_formal_execution(
        self,
    ) -> None:
        """批准 coverage 可保留，但执行字段必须采用已授权的 Formal 合同。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        formal = json.loads(managed_path.read_text(encoding="utf-8"))
        approved, bindings = codex_upgrade._runtime_codex_binary_bindings(formal)
        self.assertEqual(bindings, codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS)
        approved["capture_jobs"][0]["description"] = "批准后的说明"

        with mock.patch.object(
            codex_upgrade,
            "_bound_runtime_scenario_transition_job_ids",
            return_value=codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS,
        ):
            effective = (
                codex_upgrade._approved_scenario_with_formal_execution_contract(
                    Path("/campaign"),
                    {},
                    approved=approved,
                    formal=formal,
                )
            )

        self.assertEqual(
            codex_upgrade._scenario_non_execution_contract(effective),
            codex_upgrade._scenario_non_execution_contract(approved),
        )
        self.assertEqual(
            codex_upgrade._scenario_job_execution_contract(effective),
            codex_upgrade._scenario_job_execution_contract(formal),
        )

    def test_runtime_transition_loads_every_ancestor_as_historical_context(
        self,
    ) -> None:
        """多级只读前序链不得在更早祖先退回当前源码校验。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            middle = root / "middle"
            oldest = root / "oldest"
            for path in (current, middle, oldest):
                path.mkdir()
                (path / "campaign.json").write_text("{}\n", encoding="utf-8")
            scenario = {"path": "inputs/target.json", "sha256": "a" * 64}
            oldest_manifest = {
                "campaign_id": "oldest",
                "inputs": {"target_discovery_scenarios": scenario},
            }
            middle_manifest = {
                "campaign_id": "middle",
                "inputs": {"target_discovery_scenarios": scenario},
                "predecessor": {
                    "campaign_dir": str(oldest),
                    "campaign_id": "oldest",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        oldest / "campaign.json"
                    ),
                    "reason": "candidate_runtime_identity_correction",
                },
            }
            current_manifest = {
                "campaign_id": "current",
                "inputs": {"target_discovery_scenarios": scenario},
                "predecessor": {
                    "campaign_dir": str(middle),
                    "campaign_id": "middle",
                    "campaign_manifest_sha256": codex_upgrade.file_sha256(
                        middle / "campaign.json"
                    ),
                    "reason": "candidate_recovery_control_replacement",
                },
            }
            manifests = {
                middle.resolve(): middle_manifest,
                oldest.resolve(): oldest_manifest,
            }

            def load_manifest(path: Path, **kwargs: object) -> dict[str, object]:
                self.assertIs(kwargs.get("_control_epoch_bootstrap"), True)
                return manifests[path.resolve()]

            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                side_effect=load_manifest,
            ) as loader:
                changed = codex_upgrade._bound_runtime_scenario_transition_job_ids(
                    current,
                    current_manifest,
                )
            self.assertEqual(changed, frozenset())
            self.assertEqual(loader.call_count, 2)

            tampered = json.loads(json.dumps(current_manifest))
            tampered["predecessor"]["campaign_manifest_sha256"] = "b" * 64
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    side_effect=load_manifest,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "前序绑定漂移",
                ),
            ):
                codex_upgrade._bound_runtime_scenario_transition_job_ids(
                    current,
                    tampered,
                )

    def test_runtime_transition_rejects_unapproved_execution_change(self) -> None:
        """CODEX_BIN 之外的命令、环境或超时变化必须在预约前失败。"""

        managed_path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_scenarios_0_151_0.json"
        )
        managed = json.loads(managed_path.read_text(encoding="utf-8"))
        approved, _ = codex_upgrade._runtime_codex_binary_bindings(managed)
        for field in ("argv", "environment", "timeout_seconds"):
            with self.subTest(field=field):
                formal = json.loads(json.dumps(managed, ensure_ascii=False))
                job = next(
                    item
                    for item in formal["capture_jobs"]
                    if item["id"] == "candidate-core-mitm"
                )
                if field == "argv":
                    job["steps"][0][field] = ["false"]
                elif field == "environment":
                    job["steps"][0][field]["UNAPPROVED"] = "1"
                else:
                    job["steps"][0][field] += 1
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_bound_runtime_scenario_transition_job_ids",
                        return_value=codex_upgrade.RUNTIME_CODEX_BINARY_JOB_IDS,
                    ),
                    self.assertRaisesRegex(
                        codex_upgrade.ConfigurationError,
                        "未获 transition 批准",
                    ),
                ):
                    codex_upgrade._approved_scenario_with_formal_execution_contract(
                        Path("/campaign"),
                        {},
                        approved=approved,
                        formal=formal,
                    )

    def test_control_refresh_only_allows_bound_legacy_vc0_stop(self) -> None:
        """VC-0 控制刷新必须绑定失败 Candidate 和 attempt，其他原因仍拒绝。"""

        arguments = argparse.Namespace(
            reason="candidate_recovery_control_refresh",
            predecessor_candidate_id="candidate-a",
            predecessor_attempt_id="attempt-a",
        )
        self.assertTrue(
            codex_upgrade._successor_stop_phase_allowed(arguments, "VC-0")
        )
        arguments.predecessor_attempt_id = None
        self.assertFalse(
            codex_upgrade._successor_stop_phase_allowed(arguments, "VC-0")
        )
        arguments.predecessor_attempt_id = "attempt-a"
        arguments.reason = "candidate_failed_job_tool_recovery"
        self.assertFalse(
            codex_upgrade._successor_stop_phase_allowed(arguments, "VC-0")
        )
        self.assertTrue(
            codex_upgrade._successor_stop_phase_allowed(arguments, "VC-4")
        )

    def test_control_refresh_replays_approved_transition_recovery_controls(
        self,
    ) -> None:
        """控制刷新必须从已批准 transition 取得过期恢复控制链。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            attempt_root = (
                campaign_dir
                / "candidates"
                / "candidate-a"
                / "attempts"
                / "attempt-a"
            )
            attempt_root.mkdir(parents=True)
            campaign_path = campaign_dir / "campaign.json"
            self._write_json(campaign_path, {})
            checkpoint = {
                "path": "candidates/candidate-a/attempts/attempt-a/checkpoints",
                "record_count": 2,
                "last_sequence": 2,
                "last_sha256": "9" * 64,
            }
            attempt = {
                "attempt_id": "attempt-a",
                "attempt_digest": "a" * 64,
                "run_nonce": "8" * 64,
                "status": "failed",
                "job_checkpoint": checkpoint,
                "results": [
                    {"id": "candidate-core-direct", "status": "complete"},
                    {"id": "candidate-core-mitm", "status": "failed"},
                ],
            }
            scope = {
                "schema_version": "codex-upgrade-failed-attempt-scope/v1",
                "source_attempt_id": "attempt-a",
                "source_attempt_digest": "a" * 64,
                "run_nonce": "8" * 64,
                "planned_job_ids": [
                    "candidate-core-direct",
                    "candidate-core-mitm",
                ],
                "completed_job_ids": ["candidate-core-direct"],
                "failed_job_ids": ["candidate-core-mitm"],
                "pending_job_ids": [],
                "execute_job_ids": ["candidate-core-mitm"],
                "checkpoint": checkpoint,
                "environment_boundary_sha256": "7" * 64,
            }
            frozen_pair = {
                "upgrade_timing": {"ledger_dir": "/control/formal"},
                "arm64_environment": {"evidence_root": "/control/formal-p0"},
            }
            source_controls = {
                "upgrade_timing": {"ledger_dir": "/control/expired"},
                "arm64_environment": {"evidence_root": "/control/expired-p0"},
                "job_rehearsal": {"evidence_root": "/control/expired-rehearsal"},
            }
            recovery_controls = {
                "schema_version": (
                    codex_upgrade.TOOL_EVALUATION_RECOVERY_CONTROLS_SCHEMA
                ),
                "predecessor": frozen_pair,
                "stop_checkpoint": {"ledger_dir": "/control/formal"},
                "recovery": source_controls,
                "current_tool_files_sha256": "b" * 64,
                "legacy_vc0_recovery": {
                    "mode": "formal_candidate_attempt",
                    "source_attempt_id": "attempt-a",
                    "source_attempt_digest": "a" * 64,
                    "execute_job_ids": ["candidate-core-mitm"],
                },
            }
            manifest = {
                "campaign_id": "campaign-a",
                "control_receipts": frozen_pair,
            }
            preview_core = {
                "schema_version": (
                    codex_upgrade.TOOL_EVALUATION_TRANSITION_PREVIEW_SCHEMA
                ),
                "campaign_id": "campaign-a",
                "campaign_mode": "formal",
                "campaign_purpose": "production_replacement",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign_path
                ),
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "a" * 64,
                "evidence_boundary_sha256": "c" * 64,
                "from_tool_files_sha256": "d" * 64,
                "to_tool_files_sha256": "b" * 64,
                "from_production_sha256": "e" * 64,
                "to_production_sha256": "f" * 64,
                "from_evaluation_sha256": "1" * 64,
                "to_evaluation_sha256": "2" * 64,
                "changed_files": [],
                "allowed_operations": ["capture-run"],
                "recovery_controls": recovery_controls,
                "recovery_scope": scope,
                "raw_evidence_scanned_bytes": 0,
            }
            preview = {
                **preview_core,
                "status": "approval_required",
                "review_sha256": codex_upgrade._fingerprint(preview_core),
            }
            preview_path = attempt_root / "evaluation-transition-02-preview.json"
            self._write_json(preview_path, preview)
            preview_projection = {
                key: value
                for key, value in preview.items()
                if key
                not in {
                    "schema_version",
                    "campaign_mode",
                    "campaign_purpose",
                    "status",
                }
            }
            transition_core = {
                "schema_version": codex_upgrade.TOOL_EVALUATION_TRANSITION_SCHEMA,
                "approved_at_utc": "2026-09-03T08:44:32Z",
                "campaign_id": "campaign-a",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign_path
                ),
                "phase": "candidate",
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
                "attempt_digest": "a" * 64,
                "evidence_boundary_sha256": preview["evidence_boundary_sha256"],
                "from_tool_files_sha256": preview["from_tool_files_sha256"],
                "to_tool_files_sha256": preview["to_tool_files_sha256"],
                "from_production_sha256": preview["from_production_sha256"],
                "to_production_sha256": preview["to_production_sha256"],
                "from_evaluation_sha256": preview["from_evaluation_sha256"],
                "to_evaluation_sha256": preview["to_evaluation_sha256"],
                "changed_files": [],
                "allowed_operations": ["capture-run"],
                "recovery_controls": recovery_controls,
                "recovery_scope": scope,
                "preview": {
                    "path": preview_path.relative_to(campaign_dir).as_posix(),
                    "sha256": codex_upgrade.file_sha256(preview_path),
                },
                "review_sha256": preview_projection["review_sha256"],
                "raw_evidence_scanned_bytes": 0,
                "status": "approved",
            }
            transition = {
                **transition_core,
                "transition_digest": codex_upgrade._fingerprint(transition_core),
            }
            transition_path = attempt_root / "evaluation-transition-02.json"
            self._write_json(transition_path, transition)

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ) as load_attempt,
                mock.patch.object(
                    codex_upgrade,
                    "_legacy_vc0_phase_recovery_scope",
                    return_value=None,
                ) as current_scope,
            ):
                replayed, binding, controls = (
                    codex_upgrade._candidate_control_refresh_source_transition(
                        campaign_dir,
                        manifest,
                        candidate_id="candidate-a",
                        attempt_id="attempt-a",
                        source=transition_path,
                        _historical_manifest_controls=True,
                    )
                )
            current_scope.assert_not_called()
            load_attempt.assert_called_once_with(
                campaign_dir,
                "candidate",
                "candidate-a",
                "attempt-a",
                _historical_manifest_controls=True,
            )
            self.assertEqual(replayed, transition)
            self.assertEqual(controls, source_controls)
            self.assertEqual(
                binding,
                {
                    "path": transition_path.relative_to(campaign_dir).as_posix(),
                    "sha256": codex_upgrade.file_sha256(transition_path),
                },
            )

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_legacy_vc0_phase_recovery_scope",
                    return_value=None,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "失败闭集已经漂移",
                ),
            ):
                codex_upgrade._candidate_control_refresh_source_transition(
                    campaign_dir,
                    manifest,
                    candidate_id="candidate-a",
                    attempt_id="attempt-a",
                    source=transition_path,
                )

            broken_scope = {**scope, "pending_job_ids": ["candidate-core-mitm"]}
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "冻结 Job 集合关系非法",
            ):
                codex_upgrade._validate_historical_recovery_scope(
                    broken_scope,
                    attempt,
                )

    def test_successor_replay_fails_closed_on_local_or_predecessor_drift(
        self,
    ) -> None:
        for drift_side in ("successor", "predecessor"):
            with self.subTest(drift_side=drift_side), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                predecessor_dir, _, _ = self._create_classified_campaign(
                    root / "predecessor"
                )
                successor_dir = root / "successor"
                return_code, _, stderr = self._run_main(
                    [
                        "successor",
                        "--predecessor-campaign-dir",
                        str(predecessor_dir),
                        "--campaign-dir",
                        str(successor_dir),
                        "--campaign-id",
                        f"upgrade-0146-successor-{drift_side}",
                        "--codex-account-id",
                        "90",
                        "--reason",
                        "candidate_runtime_identity_correction",
                    ]
                )
                self.assertEqual(return_code, 0, stderr)
                target = (
                    successor_dir / "classification" / "approved" / "profile.json"
                    if drift_side == "successor"
                    else predecessor_dir / "official" / "surface.json"
                )
                target.write_bytes(target.read_bytes() + b"\n")
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    codex_upgrade.campaign_status(successor_dir)

    def test_successor_requires_new_id_and_paired_abandoned_attempt_coordinates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            base = [
                "successor",
                "--predecessor-campaign-dir",
                str(predecessor_dir),
                "--campaign-dir",
                str(root / "successor"),
                "--campaign-id",
                predecessor_manifest["campaign_id"],
                "--codex-account-id",
                "90",
                "--reason",
                "candidate_runtime_identity_correction",
            ]
            return_code, _, stderr = self._run_main(base)
            self.assertEqual(return_code, 1)
            self.assertIn("新的 campaign-id", stderr)

            paired = list(base)
            paired[paired.index(predecessor_manifest["campaign_id"])] = (
                "upgrade-0146-successor-paired"
            )
            paired.extend(["--predecessor-candidate-id", "candidate-a"])
            return_code, _, stderr = self._run_main(paired)
            self.assertEqual(return_code, 1)
            self.assertIn("必须同时提供", stderr)

    def test_campaign_manifest_is_immutable_and_stage_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            manifest_path = campaign_dir / "campaign.json"
            before = manifest_path.read_bytes()
            self._seal_official_stage(root, campaign_dir, manifest)
            self.assertEqual(manifest_path.read_bytes(), before)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.save_stage_result(
                    campaign_dir,
                    "capture-official",
                    {"status": "complete"},
                )

    def test_stage_envelope_preserves_result_schema_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, _ = self._create_campaign(Path(directory))
            path = codex_upgrade.save_stage_result(
                campaign_dir,
                "capture-official",
                {
                    "schema_version": "codex-test-official-result/v1",
                    "status": "failed",
                },
            )
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], codex_upgrade.STAGE_SCHEMA)
            self.assertEqual(
                receipt["result_schema_version"],
                "codex-test-official-result/v1",
            )

    def test_capture_stage_cannot_drift_from_approved_seal_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            stage_path = campaign_dir / "official" / "result.json"
            stage = json.loads(stage_path.read_text(encoding="utf-8"))
            stage["identity"]["version"] = "0.146.1"
            stage.pop("package_digest")
            stage["package_digest"] = codex_upgrade._fingerprint(stage)
            self._write_json(stage_path, stage)

            with self.assertRaises(codex_upgrade.ConfigurationError) as caught:
                codex_upgrade._load_stage_result(
                    campaign_dir,
                    "capture-official",
                )
            self.assertIn("seal 预览不一致", str(caught.exception))

    def test_plan_and_status_cli_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            plan_code, plan_stdout, plan_stderr = self._run_main(
                [
                    "plan",
                    "--campaign-dir",
                    str(arguments.campaign_dir),
                    "--baseline-version",
                    arguments.baseline_version,
                    "--target-version",
                    arguments.target_version,
                    "--campaign-mode",
                    arguments.campaign_mode,
                    "--campaign-purpose",
                    arguments.campaign_purpose,
                    "--timing-ledger-dir",
                    str(arguments.timing_ledger_dir),
                    "--timing-receipt",
                    str(arguments.timing_receipt),
                    "--arm64-environment-root",
                    str(arguments.arm64_environment_root),
                    "--arm64-environment-receipt",
                    str(arguments.arm64_environment_receipt),
                    "--job-rehearsal-root",
                    str(arguments.job_rehearsal_root),
                    "--job-rehearsal-receipt",
                    str(arguments.job_rehearsal_receipt),
                    "--baseline-source",
                    str(arguments.baseline_source),
                    "--target-source",
                    str(arguments.target_source),
                    "--baseline-evidence",
                    str(arguments.baseline_evidence),
                    "--target-sha256",
                    arguments.target_sha256,
                    "--target-package",
                    str(arguments.target_package),
                    "--target-package-sha256",
                    arguments.target_package_sha256,
                    "--target-code-mode-host-sha256",
                    arguments.target_code_mode_host_sha256,
                    "--runtime-image",
                    arguments.runtime_image,
                    "--rule-manifest",
                    str(arguments.rule_manifest),
                    "--scenario-manifest",
                    str(arguments.scenario_manifest),
                    "--target-scenario-manifest",
                    str(arguments.target_scenario_manifest),
                    "--campaign-id",
                    arguments.campaign_id,
                    "--model",
                    "gpt-5.4",
                    "--lite-model",
                    "gpt-5.6-luna",
                ]
            )
            self.assertEqual(plan_code, 0, plan_stderr)
            self.assertEqual(json.loads(plan_stdout)["status"], "planned")
            status_code, status_stdout, status_stderr = self._run_main(
                ["status", "--campaign-dir", str(arguments.campaign_dir)]
            )
            self.assertEqual(status_code, 0, status_stderr)
            self.assertEqual(json.loads(status_stdout)["status"], "planned")

    def test_0151_upgrade_pair_model_policy_mutations_fail_closed(self) -> None:
        codex_upgrade._validate_upgrade_pair_models(
            baseline_version="0.149.1",
            target_version="0.151.0",
            model="gpt-5.5",
            lite_model="gpt-5.6-terra",
        )

        mutations = (
            ({"baseline_version": ""}, "不支持的 Codex 升级对"),
            ({"target_version": ""}, "不支持的 Codex 升级对"),
            ({"target_version": "0.152.0"}, "不支持的 Codex 升级对"),
            ({"model": ""}, "主升级线只能使用 gpt-5.5"),
            ({"model": "gpt-5.6-terra"}, "主升级线只能使用 gpt-5.5"),
            ({"lite_model": ""}, "Lite 专项只能使用 gpt-5.6-terra"),
            ({"lite_model": "gpt-5.5"}, "Lite 专项只能使用 gpt-5.6-terra"),
        )
        baseline = {
            "baseline_version": "0.149.1",
            "target_version": "0.151.0",
            "model": "gpt-5.5",
            "lite_model": "gpt-5.6-terra",
        }
        for mutation, message in mutations:
            with self.subTest(mutation=mutation):
                values = {**baseline, **mutation}
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    message,
                ):
                    codex_upgrade._validate_upgrade_pair_models(**values)

    def test_manifest_and_plan_contracts_carry_v2_identity(self) -> None:
        """A2-1：清单推导与 plan 推导的 Job 演练合同都必须带冻结的 wire／policy 摘要，与 collect 一致。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "preflight",
                campaign_id="upgrade-0154-v2-contract",
                campaign_mode="preflight_only",
                campaign_purpose="production_replacement",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            codex_upgrade.create_campaign(arguments)
            manifest = codex_upgrade.load_campaign_manifest(arguments.campaign_dir)
            identity = manifest["tool_identity"]
            self.assertTrue(identity.get("wire_producer_sha256") and identity.get("policy_sha256"))
            derived = codex_upgrade._job_rehearsal_contract_from_manifest(arguments.campaign_dir, manifest)
            self.assertEqual(derived["wire_producer_sha256"], identity["wire_producer_sha256"])
            self.assertEqual(derived["policy_sha256"], identity["policy_sha256"])
            with mock.patch.object(
                codex_upgrade_job_rehearsal_receipt,
                "_target_evidence_label_declaration_sha256",
                return_value=derived["evidence_label_declaration_sha256"],
            ):
                from_arguments = codex_upgrade._job_rehearsal_contract_from_arguments(arguments)
            self.assertEqual(from_arguments, derived)
            # 历史清单没有 v2 字段时合同保持 v1 形状。
            legacy = copy.deepcopy(dict(manifest))
            legacy["tool_identity"] = {
                key: value
                for key, value in identity.items()
                if key not in ("wire_producer_sha256", "policy_sha256")
            }
            legacy_contract = codex_upgrade._job_rehearsal_contract_from_manifest(arguments.campaign_dir, legacy)
            self.assertNotIn("wire_producer_sha256", legacy_contract)
            self.assertNotIn("policy_sha256", legacy_contract)

    def test_account_sealed_official_writes_project_ledger_batch(self) -> None:
        """已封存 official 阶段的请求按 provenance 入总账：精确键去重、估计按来源去重、幂等。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "sealed",
                campaign_id="upgrade-0154-sealed-accounting",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            manifest = codex_upgrade.create_campaign(arguments)
            campaign_dir = arguments.campaign_dir
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未封存"):
                codex_upgrade._account_sealed_official_command(argparse.Namespace(campaign_dir=campaign_dir))
            self._seal_official_stage(root / "sealed", campaign_dir, manifest)
            before = codex_upgrade_project_ledger.replay_head(ledger_root)
            synthetic = {
                "schema_version": "live-request-provenance/v2",
                "campaign_id": manifest["campaign_id"],
                "counting_rule": "codex_model_requests/v2",
                "estimation_policy": "upper_bound_from_sibling_or_turn_ratio",
                "requests": [
                    {"identity_key": f"k{i}", "job_id": "official-core"}
                    for i in range(3)
                ],
                "jobs": [
                    {
                        "job_id": "official-core",
                        "phase": "official",
                        "status": "estimated",
                        "roots": [
                            {
                                "first_owner_job_id": "official-core",
                                "producer_run_id": "run-core",
                                "branches": [{"status": "estimated", "estimated_count": 4}],
                            }
                        ],
                    }
                ],
                "unresolved_job_ids": [],
                "precise_total": 3,
                "estimated_total": 4,
            }
            with mock.patch.object(reconciler.provenance, "collect_campaign_provenance", return_value=dict(synthetic)):
                result = codex_upgrade._account_sealed_official_command(argparse.Namespace(campaign_dir=campaign_dir))
                again = codex_upgrade._account_sealed_official_command(argparse.Namespace(campaign_dir=campaign_dir))
            self.assertEqual(result["status"], "accounted")
            self.assertEqual(result["request"]["new_identity_keys"], 3)
            self.assertEqual(result["request"]["estimated_delta"], 4)
            self.assertTrue(again["batch"].get("reused"), again)
            after = codex_upgrade_project_ledger.replay_head(ledger_root)
            self.assertEqual(after["precise_total"], before["precise_total"] + 3)
            self.assertEqual(after["estimated_total"], before["estimated_total"] + 4)
            self.assertEqual(after["root_cause_counts"], before["root_cause_counts"])
            receipt_dir = campaign_dir / "control" / reconciler.RECONCILIATION_DIR
            self.assertTrue(any(p.name.startswith("sealed-official-") for p in receipt_dir.iterdir()))

    def test_account_sealed_candidate_writes_project_ledger_once(self) -> None:
        """成功 Candidate 封存后有独立的幂等入账入口。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "sealed-candidate",
                campaign_id="upgrade-0154-sealed-candidate-accounting",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            codex_upgrade.create_campaign(arguments)
            campaign_dir = arguments.campaign_dir
            command = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id="candidate-a",
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未封存"):
                codex_upgrade._account_sealed_candidate_command(command)
            self._seal_candidate_stage(root / "sealed-candidate", campaign_dir)
            before = codex_upgrade_project_ledger.replay_head(ledger_root)
            synthetic = {
                "schema_version": "live-request-provenance/v2",
                "campaign_id": "upgrade-0154-sealed-candidate-accounting",
                "counting_rule": "codex_model_requests/v2",
                "estimation_policy": "upper_bound_from_sibling_or_turn_ratio",
                "requests": [
                    {"identity_key": "candidate-k1", "job_id": "candidate-core"},
                    {"identity_key": "candidate-k2", "job_id": "candidate-core"},
                ],
                "jobs": [
                    {
                        "job_id": "candidate-core",
                        "phase": "candidate",
                        "status": "resolved",
                        "roots": [],
                        "estimated_count": 0,
                    }
                ],
                "unresolved_job_ids": [],
                "pending_job_ids": [],
                "pre_request_zero_job_ids": [],
                "precise_total": 2,
                "estimated_total": 0,
            }
            with mock.patch.object(
                reconciler.provenance,
                "collect_campaign_provenance",
                return_value=dict(synthetic),
            ):
                result = codex_upgrade._account_sealed_candidate_command(command)
                again = codex_upgrade._account_sealed_candidate_command(command)
            self.assertEqual(result["phase"], "candidate")
            self.assertEqual(result["candidate_id"], "candidate-a")
            self.assertEqual(result["request"]["new_identity_keys"], 2)
            self.assertTrue(again["batch"].get("reused"), again)
            after = codex_upgrade_project_ledger.replay_head(ledger_root)
            self.assertEqual(after["precise_total"], before["precise_total"] + 2)
            receipt_dir = campaign_dir / "control" / reconciler.RECONCILIATION_DIR
            self.assertTrue(
                any(
                    path.name.startswith("sealed-candidate-candidate-a-")
                    for path in receipt_dir.iterdir()
                )
            )

    def test_release_certification_requirement_follows_policy_version(self) -> None:
        """发布认证绑定只对策略 v5 起创建的完整 VC 链 Campaign 必需，历史清单按 Job 演练承接。"""

        required = codex_upgrade._release_certification_required
        self.assertFalse(required({"target_version": "0.154.0", "tool_identity": {"files_sha256": "x"}}))
        self.assertFalse(required({"target_version": "0.154.0", "tool_identity": {"policy_version": 4}}))
        self.assertFalse(required({"target_version": "0.154.0", "tool_identity": {"policy_version": True}}))
        self.assertTrue(required({"target_version": "0.154.0", "tool_identity": {"policy_version": 5}}))
        self.assertTrue(required({"target_version": "0.154.0", "tool_identity": {"policy_version": "6"}}))
        self.assertFalse(required({"target_version": "0.151.0", "tool_identity": {"policy_version": 5}}))
        self.assertTrue(
            required(
                {"target_version": "0.154.0", "tool_identity": {}},
                {"release_certification": {"path": "/x"}},
            )
        )

    def test_historical_0154_controls_without_release_certification_replay(self) -> None:
        """C3 之前创建的 0.154 formal Campaign 没有发布认证绑定，只读加载必须按历史 P0 形状重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "campaign-root",
                campaign_id="upgrade-0154-historical-controls",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            codex_upgrade.create_campaign(arguments)
            campaign_dir = arguments.campaign_dir
            manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
            self.assertGreaterEqual(
                codex_upgrade._manifest_policy_version(manifest),
                codex_upgrade.RELEASE_CERTIFICATION_POLICY_VERSION,
            )
            controls = copy.deepcopy(dict(manifest["control_receipts"]))
            self.assertIn("release_certification", controls)
            rehearsal_binding = controls["job_rehearsal"]
            rehearsal_receipt = (
                Path(rehearsal_binding["evidence_root"]) / rehearsal_binding["receipt"]["path"]
            )
            historical_root = (root / "control" / "p0-gate-historical").resolve()
            historical_receipt = create_historical_p0_gate_receipt(
                historical_root,
                upgrade_id=str(controls["p0_gate"]["upgrade_id"]),
                baseline_version="0.151.0",
                target_version="0.154.0",
                campaign_purpose=str(manifest["campaign_purpose"]),
                job_rehearsal_receipt=rehearsal_receipt,
            )
            payload = json.loads(historical_receipt.read_text(encoding="utf-8"))
            controls["p0_gate"] = {
                "evidence_root": str(historical_root),
                "receipt": {
                    "path": historical_receipt.name,
                    "sha256": hashlib.sha256(historical_receipt.read_bytes()).hexdigest(),
                    "bytes": historical_receipt.stat().st_size,
                },
                "receipt_digest": payload["receipt_digest"],
                "upgrade_id": controls["p0_gate"]["upgrade_id"],
            }
            del controls["release_certification"]
            historical_manifest = copy.deepcopy(dict(manifest))
            historical_manifest["tool_identity"] = {
                key: value
                for key, value in dict(manifest["tool_identity"]).items()
                if key != "policy_version"
            }
            # 历史清单：没有策略版本、没有发布认证绑定、P0 绑定 Job 演练摘要 → 只读重放通过。
            codex_upgrade._verify_control_receipts(
                campaign_dir,
                historical_manifest,
                require_active=False,
                _control_override=controls,
            )
            # 同一套历史控制换成策略 v5 的清单：发布认证成为必需，缺失即拒绝。
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "缺少完整控制收据绑定"
            ):
                codex_upgrade._verify_control_receipts(
                    campaign_dir,
                    dict(manifest),
                    require_active=False,
                    _control_override=controls,
                )
            # 历史 P0 若绑定了别的 Job 演练摘要，同样拒绝。
            drifted = copy.deepcopy(controls)
            drifted["job_rehearsal"] = {
                **drifted["job_rehearsal"],
                "receipt": {**drifted["job_rehearsal"]["receipt"], "sha256": "0" * 64},
            }
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._verify_control_receipts(
                    campaign_dir,
                    historical_manifest,
                    require_active=False,
                    _control_override=drifted,
                )

    def test_frozen_rehearsal_replays_after_evaluator_label_digest_rotation(
        self,
    ) -> None:
        """冻结演练只因 evaluator 标签文件换版时仍可按原合同只读重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "campaign-root",
                campaign_id="upgrade-0154-frozen-label-digest",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            codex_upgrade.create_campaign(arguments)
            campaign_dir = arguments.campaign_dir
            manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
            rehearsal = manifest["control_receipts"]["job_rehearsal"]
            receipt = json.loads(
                (
                    Path(rehearsal["evidence_root"])
                    / rehearsal["receipt"]["path"]
                ).read_text(encoding="utf-8")
            )
            frozen_digest = receipt["execution_contract"][
                "evidence_label_declaration_sha256"
            ]
            rotated_digest = "0" * 64 if frozen_digest != "0" * 64 else "1" * 64

            with mock.patch.object(
                codex_upgrade_job_rehearsal_receipt,
                "_target_evidence_label_declaration_sha256",
                return_value=rotated_digest,
            ):
                codex_upgrade._verify_control_receipts(
                    campaign_dir,
                    manifest,
                    require_active=False,
                )
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "Formal 执行合同",
                ):
                    codex_upgrade._verify_control_receipts(
                        campaign_dir,
                        manifest,
                        require_active=True,
                    )

    def test_0154_upgrade_pair_model_policy_mutations_fail_closed(self) -> None:
        """0.154 必须用非 Lite 主线和 Astra Lite 轨，错配时立即拒绝。"""

        codex_upgrade._validate_upgrade_pair_models(
            baseline_version="0.151.0",
            target_version="0.154.0",
            model="gpt-5.5",
            lite_model="gpt-6-astra",
        )

        mutations = (
            ({"baseline_version": "0.149.1"}, "不支持的 Codex 升级对"),
            ({"target_version": "0.155.0"}, "不支持的 Codex 升级对"),
            ({"model": "gpt-6-astra"}, "主升级线只能使用 gpt-5.5"),
            ({"lite_model": "gpt-5.6-terra"}, "Lite 专项只能使用 gpt-6-astra"),
        )
        baseline = {
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "model": "gpt-5.5",
            "lite_model": "gpt-6-astra",
        }
        for mutation, message in mutations:
            with self.subTest(mutation=mutation):
                values = {**baseline, **mutation}
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    message,
                ):
                    codex_upgrade._validate_upgrade_pair_models(**values)

    def test_plan_rejects_package_helper_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(Path(directory))
            arguments.target_code_mode_host_sha256 = "f" * 64
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "codex-code-mode-host 摘要不一致",
            ):
                codex_upgrade.create_campaign(arguments)

    def test_candidate_stage_never_overwrites_an_existing_candidate_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _ = self._create_campaign(root)
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-a"
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.save_stage_result(
                    campaign_dir,
                    "capture-candidate",
                    {"status": "complete"},
                    candidate_id="candidate-a",
                )
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-b"
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(
                set(status["candidates"]), {"candidate-a", "candidate-b"}
            )

    def test_campaign_manifest_digest_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, _ = self._create_campaign(Path(directory))
            manifest_path = campaign_dir / "campaign.json"
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["target_sha256"] = "f" * 64
            self._write_json(manifest_path, payload)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.load_campaign_manifest(campaign_dir)

    def test_classify_requires_baseline_and_target_rule_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root, omit_last=True)
            )
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("闭环", stderr)

    def test_classify_seals_complete_migration_without_rewriting_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            campaign_bytes = (campaign_dir / "campaign.json").read_bytes()
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            return_code, _, stderr = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertEqual(
                (campaign_dir / "campaign.json").read_bytes(), campaign_bytes
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["stages"]["classify"], "complete")
            frozen = {path: path.read_bytes() for path in (campaign_dir / "classification").rglob("*") if path.is_file()}
            return_code, _, stderr = self._approve_classification(
                campaign_dir, (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertEqual(frozen, {path: path.read_bytes() for path in frozen})

    def test_classify_rejects_target_scenario_execution_contract_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            scenario_payload = json.loads(scenario.read_text(encoding="utf-8"))
            official_job = next(
                job
                for job in scenario_payload["capture_jobs"]
                if job["id"] == "official-test"
            )
            official_job["steps"][0]["argv"] = ["false"]
            self._write_json(scenario, scenario_payload)

            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )

            self.assertEqual(return_code, 1)
            self.assertIn("official 执行契约", stderr)

    def test_classify_requires_exact_dynamic_discovery_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(
                root,
                campaign_dir,
                manifest,
                include_new_surface=True,
            )
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("未唯一分类", stderr)

    def test_classify_rejects_old_0145_assertion_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            (
                target,
                migration,
                scenario,
                profile,
                assertion_profile,
                _,
            ) = self._write_classification_manifests(root)
            payload = json.loads(
                assertion_profile.read_text(encoding="utf-8")
            )
            payload["codex_version"] = "0.145.0"
            self._write_json(assertion_profile, payload)
            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (
                        target,
                        migration,
                        scenario,
                        profile,
                        assertion_profile,
                    ),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("断言画像 codex_version 不一致", stderr)

    def test_classify_draft_rewrites_every_nested_version_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)

            receipt = codex_upgrade.classify_campaign(campaign_dir)
            self.assertEqual(receipt["status"], "draft")
            replacement = receipt["assertion_version_replacements"]
            self.assertEqual(replacement["baseline_version"], "0.145.0")
            self.assertEqual(replacement["target_version"], "0.147.0")
            self.assertEqual(replacement["count"], 11)
            self.assertEqual(len(replacement["paths"]), 11)

            assertion_profile = json.loads(
                (Path(receipt["path"]) / "assertion-profile.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn(
                "0.145.0",
                json.dumps(assertion_profile, ensure_ascii=False),
            )
            coordinates = codex_upgrade._assertion_profile_version_coordinates(
                assertion_profile
            )
            self.assertTrue(coordinates)
            self.assertEqual({version for _, version in coordinates}, {"0.147.0"})

    def test_0147_没有期望覆盖时画像逐字不变(self) -> None:
        """R9 复核后撤销了唯一一条 override，0.147 现在不应有任何期望变更。

        原 override 把 `wham-get-paths` 的 `wham/usage` 改成 `wham/settings/user`，
        依据是「0.147 用后者替代了前者」。这条判定已被双重证伪：0.147 的
        `backend-client/src/client/rate_limit_resets.rs:83` 仍然构造 `{}/wham/usage`，
        而 `settings/user` 在 `client.rs:640` 是另一个独立调用点；relay 面实测
        A12（配额查询）发的正是 usage ＋ rate-limit-reset-credits，与 0.145 一致。

        原判定来自 mitm 面证据，那个采集面恰好只捕到 `settings/user`——是证据面
        选错，不是版本行为变化。
        """

        base_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        profile = json.loads(base_path.read_text(encoding="utf-8"))
        updated, count = codex_upgrade._apply_assertion_profile_overrides(
            profile,
            target_version="0.147.0",
            base_profile_path=base_path,
        )
        self.assertEqual(count, 0)
        self.assertEqual(updated, profile)

    def test_01491_期望覆盖从_0147_精确追加_routing_hint(self) -> None:
        tool_root = Path(__file__).resolve().parents[1]
        base_path = tool_root / "candidate_rule_expectations_0_147_0.json"
        profile = json.loads(base_path.read_text(encoding="utf-8"))
        updated, count = codex_upgrade._apply_assertion_profile_overrides(
            profile,
            target_version="0.149.1",
            base_profile_path=base_path,
        )
        self.assertEqual(count, 3)
        for rule_id, check_id in (
            ("SPEC-H1-004", "responses-order"),
            ("SPEC-WS-002", "default-swap-remove-order"),
            ("SPEC-EP-014", "legacy-default-headers"),
        ):
            check = next(
                check
                for rule in updated["rules"]
                if rule["rule_id"] == rule_id
                for check in rule["checks"]
                if check["id"] == check_id
            )
            if (rule_id, check_id) == ("SPEC-H1-004", "responses-order"):
                header_order = check["assertion"]["value"]
                self.assertIn("x-codex-routing-hint", header_order)
                self.assertLess(
                    header_order.index("x-openai-internal-codex-responses-lite"),
                    header_order.index("x-codex-routing-hint"),
                )
                self.assertNotIn("cookie", header_order)
            elif (rule_id, check_id) == (
                "SPEC-EP-014",
                "legacy-default-headers",
            ):
                assertion = check["assertion"]
                self.assertEqual(
                    assertion["operator"], "all_ordered_subset_of"
                )
                self.assertIn("x-codex-routing-hint", assertion["required"])
                self.assertIn(
                    "x-openai-internal-codex-responses-lite",
                    assertion["required"],
                )
                self.assertIn("cookie", assertion["allowed"])
                self.assertNotIn("cookie", assertion["required"])
            else:
                self.assertIn(
                    "x-codex-routing-hint", check["assertion"]["value"]
                )

    def test_0151_responses_固定线序允许_cookie_条件槽(self) -> None:
        """0.151 的 cookie 可选语义必须由有序允许全集明确表达。"""

        profile_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_151_0.json"
        )
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        check = next(
            check
            for rule in profile["rules"]
            if rule["rule_id"] == "SPEC-H1-004"
            for check in rule["checks"]
            if check["id"] == "responses-order"
        )
        assertion = check["assertion"]
        self.assertEqual(assertion["operator"], "all_ordered_subset_of")
        self.assertIn("cookie", assertion["allowed"])
        self.assertNotIn("cookie", assertion["required"])
        self.assertEqual(
            assertion["allowed"].index("cookie"),
            assertion["allowed"].index("user-agent") + 1,
        )
        self.assertEqual(
            assertion["allowed"].index("host"),
            assertion["allowed"].index("cookie") + 1,
        )

    def test_wham_get_paths_保持_0145_原期望(self) -> None:
        """防回归：不得再把 usage 换成 settings/user。"""

        base_path = (
            Path(__file__).resolve().parents[1]
            / "candidate_rule_expectations_0_145_0.json"
        )
        profile = json.loads(base_path.read_text(encoding="utf-8"))
        check = next(
            check
            for rule in profile["rules"]
            if rule["rule_id"] == "SPEC-EP-019"
            for check in rule["checks"]
            if check["id"] == "wham-get-paths"
        )
        self.assertEqual(
            check["assertion"]["value"],
            [
                "/backend-api/wham/usage",
                "/backend-api/wham/rate-limit-reset-credits",
            ],
        )
        # settings/user 属另一个调用点，不进 A12 的路径集合；画像别处已按
        # not_equal 显式排除它，这里一并锁住，避免两端再次各说各话。
        excluded = [
            condition
            for rule in profile["rules"]
            for check_item in rule["checks"]
            for condition in check_item["select"].get("where", [])
            if condition.get("value") == "/backend-api/wham/settings/user"
        ]
        self.assertTrue(excluded)
        for condition in excluded:
            self.assertEqual(condition["operator"], "not_equal")

    def test_classify_rejects_nested_baseline_version_after_top_level_update(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            payload = json.loads(assertion_profile.read_text(encoding="utf-8"))
            identity = next(
                check["assertion"]["value"]
                for rule in payload["rules"]
                for check in rule["checks"]
                if isinstance(check["assertion"].get("value"), dict)
                and "user_agent_prefix" in check["assertion"]["value"]
            )
            identity["user_agent_prefix"] = "codex_exec/0.145.0"
            self._write_json(assertion_profile, payload)

            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("仍残留 baseline 版本坐标", stderr)

    def test_classify_rejects_non_target_behavior_version_coordinate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(root)
            )
            payload = json.loads(assertion_profile.read_text(encoding="utf-8"))
            query_pair = next(
                pair
                for rule in payload["rules"]
                for check in rule["checks"]
                for pair in (
                    check["assertion"].get("value", {}).get("query_pairs", [])
                    if isinstance(check["assertion"].get("value"), dict)
                    else []
                )
                if pair[0] == "client_version"
            )
            query_pair[1] = "0.144.0"
            self._write_json(assertion_profile, payload)

            return_code, _, stderr = self._run_main(
                self._classification_arguments(
                    campaign_dir,
                    (target, migration, scenario, profile, assertion_profile),
                )
            )
            self.assertEqual(return_code, 1)
            self.assertIn("行为版本坐标与 target_version 不一致", stderr)

    def test_stage_profile_requires_profile_approved_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _ = self._create_campaign(root)
            return_code, _, stderr = self._run_main(
                [
                    "stage-profile",
                    "--campaign-dir",
                    str(campaign_dir),
                    "--output",
                    str(root / "catalog-stage"),
                ]
            )
            self.assertEqual(return_code, 1)
            self.assertIn("只允许从 profile_approved 状态执行", stderr)

    def test_prepare_profile_binds_official_sealed_target_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}\n", encoding="utf-8")
            output = root / "profile-draft.json"
            profile = {
                "schema_version": codex_upgrade.PROFILE_SCHEMA,
                "codex_version": manifest["target_version"],
                "profile_id": "codex-0.147.0-prepared",
                "profile_digest": "d" * 64,
                "profile_payload": {"prepared": True},
                "profile_payload_sha256": codex_upgrade._fingerprint(
                    {"prepared": True}
                ),
                "status": "draft",
            }

            def prepare(*_args: object, **_kwargs: object) -> argparse.Namespace:
                output.write_text(json.dumps(profile), encoding="utf-8")
                return argparse.Namespace(
                    returncode=0,
                    stdout=json.dumps(profile),
                    stderr="",
                )

            with mock.patch.object(
                codex_upgrade,
                "_run_external_command",
                side_effect=prepare,
            ) as run:
                return_code, stdout, stderr = self._run_main(
                    [
                        "prepare-profile",
                        "--campaign-dir",
                        str(campaign_dir),
                        "--snapshot",
                        str(snapshot),
                        "--profile-id",
                        profile["profile_id"],
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 0, stderr)
            self.assertIn('"status": "draft"', stdout)
            self.assertIn("-prepare-snapshot", run.call_args.args[0])

    def test_stage_profile_binds_approved_identity_without_changing_active(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest, _ = self._create_classified_campaign(root)
            classification = codex_upgrade._load_stage_result(
                campaign_dir,
                "classify",
            )
            receipt = {
                "schema_version": "official-egress-catalog-stage/v1",
                "campaign_id": manifest["campaign_id"],
                "classification_sha256": classification[
                    "joint_manifest_sha256"
                ],
                "target_version": manifest["target_version"],
                "target_profile_digest": "c" * 64,
                "candidate_release_mode": "previous",
                "active_unchanged": True,
                "production_selector_changed": False,
            }
            output = root / "catalog-stage"
            asset = b"{}\n"
            inventory = [
                {
                    "path": "catalogdata/runtime/release-catalog.json",
                    "sha256": hashlib.sha256(asset).hexdigest(),
                    "size": len(asset),
                }
            ]
            receipt["inventory"] = inventory
            receipt["inventory_sha256"] = codex_upgrade._fingerprint(inventory)

            def run_stage(*_args: object, **_kwargs: object) -> argparse.Namespace:
                target = output / "catalogdata/runtime/release-catalog.json"
                target.parent.mkdir(parents=True)
                target.write_bytes(asset)
                (output / "catalog-stage-receipt.json").write_text(
                    json.dumps(receipt),
                    encoding="utf-8",
                )
                return argparse.Namespace(
                    returncode=0,
                    stdout=json.dumps(receipt),
                    stderr="",
                )

            with mock.patch.object(
                codex_upgrade,
                "_run_external_command",
                side_effect=run_stage,
            ) as run:
                return_code, stdout, stderr = self._run_main(
                    [
                        "stage-profile",
                        "--campaign-dir",
                        str(campaign_dir),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(return_code, 0, stderr)
            self.assertIn('"active_unchanged": true', stdout)
            command = run.call_args.args[0]
            self.assertIn("./cmd/egresscatalogstage", command)
            self.assertIn(str(output), command)

            frozen = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
            with mock.patch.object(codex_upgrade, "_run_external_command") as repeat:
                result = codex_upgrade.stage_profile_catalog(campaign_dir, output.resolve())
            repeat.assert_not_called()
            self.assertTrue(result["active_unchanged"])
            self.assertEqual(frozen, {path: path.read_bytes() for path in frozen})

    def test_classify_supports_explicit_rule_add_delete_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            baseline_manifest = (
                Path(__file__).resolve().parents[1]
                / "codex_upgrade_rules_0_145_0.json"
            )
            baseline_rules = list(
                load_rule_manifest(baseline_manifest, "0.145.0")
            )
            added_rule = "SPEC-NEW-001"
            target_rules = [
                rule for rule in baseline_rules if rule != baseline_rules[2]
            ] + [added_rule]
            target = root / "target-rules-mixed.json"
            migration = root / "rule-migration-mixed.json"
            profile = root / "profile-mixed.json"
            assertion_profile = root / "assertion-profile-mixed.json"
            self._write_json(
                target,
                {
                    "schema_version": codex_upgrade.RULE_SCHEMA,
                    "codex_version": "0.147.0",
                    "required_rules": target_rules,
                },
            )
            entries = []
            for index, rule in enumerate(baseline_rules):
                classification = {
                    0: "change",
                    1: "condition_change",
                    2: "delete",
                }.get(index, "inherit")
                entries.append(
                    {
                        "baseline_rule": rule,
                        "target_rule": (
                            None if classification == "delete" else rule
                        ),
                        "classification": classification,
                        "rationale": "显式迁移分类",
                        "evidence_refs": ["official-diff.json"],
                    }
                )
            entries.append(
                {
                    "baseline_rule": None,
                    "target_rule": added_rule,
                    "classification": "add",
                    "rationale": "目标版本新增规则",
                    "evidence_refs": ["official-diff.json"],
                }
            )
            self._write_json(
                migration,
                {
                    "schema_version": codex_upgrade.MIGRATION_SCHEMA,
                    "baseline_version": "0.145.0",
                    "target_version": "0.147.0",
                    "status": "approved",
                    "entries": entries,
                    "discovery_classifications": [],
                },
            )
            profile_payload = {
                "transport": "codex-official-egress",
                "rule_count": len(target_rules),
            }
            self._write_json(
                profile,
                {
                    "schema_version": codex_upgrade.PROFILE_SCHEMA,
                    "codex_version": "0.147.0",
                    "profile_id": "codex-0.147.0-test-v1",
                    "profile_digest": "c" * 64,
                    "profile_payload": profile_payload,
                    "profile_payload_sha256": codex_upgrade._fingerprint(
                        profile_payload
                    ),
                    "status": "approved",
                },
            )
            scenario = self._write_scenario_manifest(
                root,
                target,
                tuple(target_rules),
                version="0.147.0",
                name="scenarios-mixed.json",
            )
            self._write_assertion_profile(
                assertion_profile,
                tuple(target_rules),
                version="0.147.0",
            )
            return_code, _, stderr = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 0, stderr)
            self._seal_candidate_stage(root, campaign_dir)
            comparison = codex_upgrade.compare_campaign(
                campaign_dir, "candidate-a"
            )
            self.assertTrue(comparison["coverage"]["complete"])
            self.assertEqual(
                comparison["coverage"]["required_rule_count"],
                len(target_rules),
            )

    def test_blocked_migration_keeps_campaign_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            self._seal_official_stage(root, campaign_dir, manifest)
            baseline_manifest = (
                Path(__file__).resolve().parents[1]
                / "codex_upgrade_rules_0_145_0.json"
            )
            rules = load_rule_manifest(baseline_manifest, "0.145.0")
            target, migration, scenario, profile, assertion_profile, _ = (
                self._write_classification_manifests(
                    root, blocked_rule=rules[0]
                )
            )
            return_code, _, _ = self._approve_classification(
                campaign_dir,
                (target, migration, scenario, profile, assertion_profile),
            )
            self.assertEqual(return_code, 2)
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["status"], "blocked")
            self.assertEqual(status["stages"]["classify"], "blocked")

    def test_compare_campaign_is_offline_and_keeps_candidate_evidence_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            evidence_root, _ = self._seal_candidate_stage(root, campaign_dir)
            evidence_sha = hashlib.sha256(
                (evidence_root / "surface.json").read_bytes()
            ).hexdigest()
            with (
                mock.patch.object(
                    codex_upgrade,
                    "run_job",
                    side_effect=AssertionError("compare 不得运行抓包任务"),
                ),
                mock.patch.object(
                    codex_upgrade.subprocess,
                    "run",
                    side_effect=AssertionError("compare 不得启动外部进程"),
                ),
            ):
                result = codex_upgrade.compare_campaign(
                    campaign_dir, "candidate-a"
                )
            self.assertTrue(result["equal"])
            self.assertEqual(
                hashlib.sha256(
                    (evidence_root / "surface.json").read_bytes()
                ).hexdigest(),
                evidence_sha,
            )

    def test_compare_exit_code_allows_complete_offline_surface_difference(self) -> None:
        result = {
            "status": "complete",
            "offline_only": True,
            "equal": False,
            "coverage": {"complete": True},
            "profile_binding_matches": True,
        }
        self.assertEqual(codex_upgrade._compare_result_exit_code(result), 0)

        for invalid in (
            {**result, "status": "failed"},
            {**result, "offline_only": False},
            {**result, "coverage": {"complete": False}},
            {**result, "profile_binding_matches": False},
        ):
            self.assertEqual(codex_upgrade._compare_result_exit_code(invalid), 2)

    def test_recovery_failure_blocks_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            self._seal_candidate_stage(
                root, campaign_dir, restoration_passed=False
            )
            failed_stage = codex_upgrade._load_stage_result(
                campaign_dir,
                "capture-candidate",
                "candidate-a",
            )
            self.assertEqual(failed_stage["status"], "failed")
            self.assertTrue(failed_stage["restoration"]["passed"])
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade.compare_campaign(campaign_dir, "candidate-a")

    def test_accept_requires_one_passing_assertion_for_every_target_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root, rules, identity, omit_last=True
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_profile_identity_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity,
                profile_digest="f" * 64,
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_not_applicable_without_full_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity,
                first_rule_status="not_applicable",
                first_rule_evidence_level="partial",
            )
            with self.assertRaises(codex_upgrade.ConfigurationError):
                self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )

    def test_accept_succeeds_only_after_compare_and_all_rule_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            comparison = codex_upgrade.compare_campaign(
                campaign_dir, "candidate-a"
            )
            self.assertTrue(comparison["equal"])
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(acceptance["accepted"])
            self.assertEqual(acceptance["campaign_mode"], "formal")
            self.assertEqual(acceptance["campaign_purpose"], "validation_only")
            self.assertEqual(acceptance["candidate_purpose"], "validation_only")
            self.assertEqual(
                acceptance["production_state"], "accepted_not_activated"
            )
            self.assertTrue(
                acceptance["gates"]["candidate_external_gate_complete"]
            )
            self.assertEqual(
                acceptance["candidate_identity"]["source_tree_sha256"],
                identity["source_tree_sha256"],
            )
            candidate = codex_upgrade._load_stage_result(
                campaign_dir, "capture-candidate", "candidate-a"
            )
            self.assertEqual(
                {
                    (item["client_id"], item["protocol"], item["entrypoint"])
                    for item in candidate["client_bindings"]
                },
                {
                    (
                        "kilo-compatible",
                        "openai-compatible",
                        "/v1/chat/completions",
                    ),
                    (
                        "kilo-responses",
                        "openai-responses",
                        "/v1/responses",
                    ),
                },
            )
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["status"], "ready")

    def test_candidate_purpose_is_frozen_through_attempt_and_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            self._seal_candidate_stage(root, campaign_dir)
            candidate = codex_upgrade._load_stage_result(
                campaign_dir, "capture-candidate", "candidate-a"
            )
            self.assertEqual(candidate["candidate_purpose"], "validation_only")
            self.assertEqual(
                candidate["identity"]["candidate_purpose"], "validation_only"
            )
            attempt_path = campaign_dir / candidate["attempt"]["path"]
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            self.assertEqual(attempt["candidate_purpose"], "validation_only")
            reservation_path = campaign_dir / attempt["reservation"]["path"]
            reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
            self.assertEqual(reservation["candidate_purpose"], "validation_only")
            preview_path = campaign_dir / candidate["seal_preview"]["path"]
            preview = json.loads(preview_path.read_text(encoding="utf-8"))
            self.assertEqual(preview["candidate_purpose"], "validation_only")

            comparison = codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            self.assertEqual(comparison["candidate_purpose"], "validation_only")
            sealed = codex_upgrade._load_stage_result(
                campaign_dir, "compare", "candidate-a"
            )
            self.assertEqual(sealed["candidate_purpose"], "validation_only")

    def test_attempt_and_comparison_purpose_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            self._seal_candidate_stage(root, campaign_dir)
            candidate = codex_upgrade._load_stage_result(
                campaign_dir, "capture-candidate", "candidate-a"
            )
            attempt_path = campaign_dir / candidate["attempt"]["path"]
            attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
            attempt_id = attempt["attempt_id"]
            attempt["candidate_purpose"] = "production_replacement"
            attempt.pop("attempt_digest")
            attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
            self._write_json(attempt_path, attempt)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "身份或摘要不一致",
            ):
                codex_upgrade._load_capture_attempt(
                    campaign_dir,
                    "candidate",
                    "candidate-a",
                    attempt_id,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, _ = self._create_classified_campaign(root)
            self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            path = campaign_dir / "comparisons/candidate-a/result.json"
            comparison = json.loads(path.read_text(encoding="utf-8"))
            comparison["candidate_purpose"] = "production_replacement"
            comparison.pop("package_digest")
            comparison["package_digest"] = codex_upgrade._fingerprint(comparison)
            self._write_json(path, comparison)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "candidate purpose",
            ):
                codex_upgrade._load_stage_result(
                    campaign_dir, "compare", "candidate-a"
                )

    def test_production_replacement_plan_rejects_missing_live_compose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._campaign_arguments(
                Path(directory),
                campaign_purpose="production_replacement",
            )
            arguments.live_attestation_compose_dir = ""
            arguments.live_attestation_compose_files = ""
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "Live attestation compose",
            ):
                codex_upgrade.create_campaign(arguments)

    def test_production_replacement_ready_requires_explicit_activation_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(
                root,
                campaign_purpose="production_replacement",
            )
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertEqual(
                acceptance["candidate_purpose"], "production_replacement"
            )
            status = codex_upgrade.campaign_status(campaign_dir, "candidate-a")
            self.assertEqual(status["status"], "ready")
            self.assertEqual(
                status["production_status"], "accepted_not_activated"
            )
            self.assertIn("§4.6", status["next_command"])
            self.assertIn("不得宣称生产升级完成", status["next_command"])

    def test_accept_rejects_missing_candidate_external_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "外部门禁收据无法重放",
            ):
                codex_upgrade.accept_campaign(
                    campaign_dir,
                    "candidate-a",
                    assertions,
                    root / "missing-gate-root",
                    root / "missing-gate-root/receipt.json",
                )

    def test_ready_status_replays_candidate_external_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                accepted = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(accepted["accepted"])
            evidence = (
                root
                / "candidate-a-external-gate/evidence/check-egress-spec.json"
            )
            self._write_json(evidence, {"tampered": True})
            evidence.chmod(0o600)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "外部门禁收据无法重放",
            ):
                codex_upgrade.campaign_status(campaign_dir, "candidate-a")

    def test_accept_uses_rule_contract_instead_of_raw_surface_set_equality(self) -> None:
        """28／7 任务计划的完整指纹集合可不同，验收结论必须来自批准规则重放。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(
                root,
                campaign_dir,
                include_new_surface=True,
            )
            comparison = codex_upgrade.compare_campaign(
                campaign_dir, "candidate-a"
            )
            self.assertFalse(comparison["equal"])
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(acceptance["accepted"])
            self.assertFalse(acceptance["equal"])
            self.assertTrue(acceptance["gates"]["comparison_complete"])

    def test_blocked_accept_is_written_to_unique_attempt(self) -> None:
        """门禁不通过时必须保留结果，不能因缺失目录 helper 在收尾阶段崩溃。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(
                root,
                campaign_dir,
                git_commit=None,
            )
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                acceptance = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertFalse(acceptance["accepted"])
            self.assertEqual(
                acceptance["failed_gates"], ["candidate_identity_complete"]
            )
            attempts = sorted(
                (campaign_dir / "acceptance" / "candidate-a" / "attempts").glob(
                    "*/result.json"
                )
            )
            self.assertEqual(len(attempts), 1)
            blocked = json.loads(attempts[0].read_text(encoding="utf-8"))
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(
                blocked["failed_gates"], ["candidate_identity_complete"]
            )

    def test_accept_rejects_handwritten_machine_pass_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity = self._seal_candidate_stage(root, campaign_dir)
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "离线重放",
            ):
                self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )

    def test_accept_rejects_evidence_tampering_after_compare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            evidence_root, identity = self._seal_candidate_stage(
                root, campaign_dir
            )
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(root, rules, identity)
            (evidence_root / "surface.json").write_text(
                '{"records":[]}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "原始证据摘要",
            ):
                self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )

    def _accept_with_mutated_results(
        self,
        root: Path,
        mutate,
    ):
        """构造合法 v2 results 后按需变异，返回 accept 调用结果或异常。"""

        campaign_dir, _, rules = self._create_classified_campaign(root)
        _, identity = self._seal_candidate_stage(root, campaign_dir)
        codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
        assertions = self._write_assertions(root, rules, identity)
        document = json.loads(assertions.read_text(encoding="utf-8"))
        mutate(document)
        self._write_json(assertions, document)
        with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
            return self._accept_campaign(
                root, campaign_dir, "candidate-a", assertions
            )

    def test_accept_rejects_legacy_v1_results_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["schema_version"] = "codex-egress-rule-assertions/v1"

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "旧 schema 已废除"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_wrong_validation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "dual_wire":
                        row["validation_mode"] = "candidate_profile"
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "validation_mode 与验收契约不一致"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_candidate_profile_row_carrying_official_side(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "candidate_profile":
                        row["official_command"] = ["python3", "fake.py"]
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "字段不闭合"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_forged_official_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                for row in document["rules"]:
                    if row["validation_mode"] == "candidate_profile":
                        row["official_authority"]["review_sha256"] = "f" * 64
                        break

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "官方权威"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_contract_digest_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["acceptance_contract_sha256"] = "0" * 64

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "冻结验收契约摘要"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_accept_rejects_missing_positive_negative_leftovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            def mutate(document: dict) -> None:
                document["rules"][0]["positive_assertions"] = ["x"]

            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError, "字段不闭合"
            ):
                self._accept_with_mutated_results(Path(directory), mutate)

    def test_candidate_specific_status_does_not_leak_between_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _, rules = self._create_classified_campaign(root)
            _, identity_a = self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-a"
            )
            codex_upgrade.compare_campaign(campaign_dir, "candidate-a")
            assertions = self._write_assertions(
                root,
                rules,
                identity_a,
                candidate_id="candidate-a",
            )
            with mock.patch.object(codex_upgrade, "_rerun_machine_assertion"):
                accepted = self._accept_campaign(
                    root, campaign_dir, "candidate-a", assertions
                )
            self.assertTrue(accepted["accepted"])
            self._seal_candidate_stage(
                root, campaign_dir, candidate_id="candidate-b"
            )

            status_a = codex_upgrade.campaign_status(
                campaign_dir, "candidate-a"
            )
            status_b = codex_upgrade.campaign_status(
                campaign_dir, "candidate-b"
            )
            self.assertEqual(status_a["status"], "ready")
            self.assertEqual(status_b["status"], "candidate_sealed")
            self.assertEqual(
                status_b["candidate_states"]["candidate-a"], "ready"
            )
            self.assertEqual(
                status_b["candidate_states"]["candidate-b"],
                "candidate_sealed",
            )

    def test_0145_rule_manifest_contains_exact_required_scope(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        rules = load_rule_manifest(manifest, "0.145.0")
        self.assertEqual(len(rules), 42)
        self.assertIn("SPEC-EP-023", rules)
        self.assertNotIn("SPEC-H2-001", rules)
        self.assertNotIn("SPEC-WS-003", rules)
        self.assertNotIn("SPEC-BODY-007", rules)

    def test_source_inventory_detects_endpoint_and_dependency_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            for source in (baseline, target):
                (source / "src").mkdir(parents=True)
            (baseline / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.12.28"\n',
                encoding="utf-8",
            )
            (target / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.13.0"\n',
                encoding="utf-8",
            )
            (baseline / "src/client.rs").write_text(
                'client.post("/backend-api/codex/responses").send();\n',
                encoding="utf-8",
            )
            (target / "src/client.rs").write_text(
                (
                    'client.post("/backend-api/codex/responses").send();\n'
                    'client.post("/backend-api/codex/new-egress").send();\n'
                ),
                encoding="utf-8",
            )
            baseline_inventory = scan_source_tree(baseline, "0.145.0")
            target_inventory = scan_source_tree(target, "0.147.0")
            difference = compare_inventory(
                baseline_inventory, target_inventory
            )
            added_values = {
                item["value"] for item in difference["added"]
            }
            self.assertIn(
                "/backend-api/codex/new-egress", added_values
            )
            self.assertTrue(
                any("reqwest|0.13.0" in value for value in added_values)
            )

    def test_dynamic_surface_detects_new_route_and_body_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            baseline.mkdir()
            target.mkdir()
            common = {
                "method": "POST",
                "path": "/backend-api/codex/responses",
                "http_version": "HTTP/1.1",
                "headers": [["version", "0.145.0"], ["host", "chatgpt.com"]],
                "json_shape": {"model": "<string>", "input": []},
            }
            (baseline / "surface.json").write_text(
                json.dumps({"records": [{"request": common}]}),
                encoding="utf-8",
            )
            new_request = {
                **common,
                "path": "/backend-api/codex/new-egress?token=secret",
                "json_shape": {"model": "<string>", "new_field": True},
            }
            (target / "surface.json").write_text(
                json.dumps(
                    {"records": [{"request": common}, {"request": new_request}]}
                ),
                encoding="utf-8",
            )
            baseline_surface = scan_evidence([baseline], "baseline")
            target_surface = scan_evidence([target], "target")
            difference = compare_surfaces(baseline_surface, target_surface)
            self.assertEqual(difference["added_count"], 1)
            self.assertEqual(
                difference["added"][0]["path"],
                "/backend-api/codex/new-egress?token",
            )
            serialized = json.dumps(difference, ensure_ascii=False)
            self.assertNotIn("secret", serialized)

    def test_raw_mitm_body_is_reduced_to_shape_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "request": {
                    "method": "POST",
                    "path": "/backend-api/codex/responses",
                    "http_version": "HTTP/1.1",
                    "headers": [["authorization", "SECRET-TOKEN"]],
                    "body": {
                        "text": json.dumps(
                            {
                                "model": "gpt-test",
                                "input": [{"role": "user", "content": "SECRET-TEXT"}],
                            }
                        )
                    },
                }
            }
            (root / "capture.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            result = scan_evidence([root], "candidate")
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SECRET-TOKEN", serialized)
            self.assertNotIn("SECRET-TEXT", serialized)
            self.assertEqual(result["surface_count"], 1)
            self.assertIsNotNone(
                result["surfaces"][0]["body_shape_sha256"]
            )

    def test_h2_request_signature_includes_host_and_query_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = {
                "connections": [
                    {
                        "protocol": "h2",
                        "frames": [
                            {
                                "type": "HEADERS",
                                "header_names_in_order": [
                                    ":method",
                                    ":scheme",
                                    ":authority",
                                    ":path",
                                    "content-type",
                                ],
                                "headers": [
                                    {"name": ":method", "value": "POST"},
                                    {"name": ":scheme", "value": "https"},
                                    {
                                        "name": ":authority",
                                        "value": "api.openai.com",
                                    },
                                    {
                                        "name": ":path",
                                        "value": "/v1/new?token=<redacted>",
                                    },
                                    {
                                        "name": "content-type",
                                        "value": "application/json",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
            (root / "relay.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            result = scan_evidence([root], "h2")
            self.assertEqual(result["surface_count"], 1)
            surface = result["surfaces"][0]
            self.assertEqual(surface["host"], "api.openai.com")
            self.assertEqual(surface["path"], "/v1/new?token")
            self.assertEqual(surface["protocol"], "h2")
            self.assertEqual(
                surface["header_names"], summary["connections"][0]["frames"][0]["header_names_in_order"]
            )

    def test_rule_coverage_requires_official_and_candidate_evidence(self) -> None:
        official = Job(
            job_id="official",
            phase="official",
            suites=("full",),
            description="official",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        candidate = Job(
            job_id="candidate",
            phase="candidate",
            suites=("full",),
            description="candidate",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        incomplete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "failed"},
            ],
        )
        self.assertFalse(incomplete["complete"])
        complete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "complete"},
            ],
        )
        self.assertTrue(complete["complete"])

    def test_run_job_fails_when_any_evidence_root_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            present_root = root / "present"
            present_root.mkdir()
            (present_root / "evidence.json").write_text(
                '{"status":"complete"}\n',
                encoding="utf-8",
            )
            self._make_private_tree(present_root)
            missing_root = root / "missing"
            log_root = root / "logs"
            log_root.mkdir()
            job = Job(
                job_id="multi-root",
                phase="official",
                suites=("full",),
                description="任一证据根缺失都必须失败",
                steps=({"argv": ["true"], "timeout": 30},),
                evidence_roots=(str(present_root), str(missing_root)),
                covers=("SPEC-H1-001",),
                scenario_ids=("A01",),
            )
            result = codex_upgrade.run_job(job, log_root)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                result["missing_evidence_patterns"], [str(missing_root)]
            )

    def test_candidate_runtime_override_changes_candidate_arguments_without_new_campaign(
        self,
    ) -> None:
        """候选层运行坐标覆盖：run／seal 读取生效值，磁盘清单与 Campaign 身份不变。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            frozen_digest = (campaign_dir / "campaign.sha256").read_text(
                encoding="utf-8"
            ).strip()
            result = codex_upgrade.create_candidate_runtime_override(
                argparse.Namespace(
                    campaign_dir=campaign_dir,
                    candidate_id="cand-1",
                    reason="切换到当前可用服务容器",
                    set=["service_container=sub2apiplus-b"],
                )
            )
            self.assertEqual(result["status"], "recorded")
            receipt_path = campaign_dir / "candidates" / "cand-1" / "runtime-override.json"
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(oct(receipt_path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(
                result["overrides"],
                {
                    "service_container": {
                        "predecessor": "sub2apiplus",
                        "successor": "sub2apiplus-b",
                    },
                },
            )
            # 磁盘清单与摘要不变，Campaign 身份不受影响。
            self.assertEqual(
                (campaign_dir / "campaign.sha256").read_text(encoding="utf-8").strip(),
                frozen_digest,
            )
            self.assertEqual(
                codex_upgrade.load_campaign_manifest(campaign_dir)["configuration"][
                    "codex_account_id"
                ],
                90,
            )
            effective = codex_upgrade._apply_candidate_runtime_override(
                campaign_dir, manifest, "cand-1"
            )
            self.assertEqual(effective["configuration"]["codex_account_id"], 90)
            self.assertEqual(effective["configuration"]["service_container"], "sub2apiplus-b")
            self.assertEqual(manifest["configuration"]["codex_account_id"], 90)
            again = codex_upgrade._apply_candidate_runtime_override(
                campaign_dir, effective, "cand-1"
            )
            self.assertEqual(again["configuration"], effective["configuration"])
            arguments = codex_upgrade._campaign_arguments(
                campaign_dir, effective, candidate_id="cand-1"
            )
            self.assertEqual(arguments.codex_account_id, 90)
            self.assertEqual(arguments.service_container, "sub2apiplus-b")
            probe = codex_upgrade._environment_probe_arguments(
                effective, root / "probe", "before"
            )
            self.assertEqual(probe.account_id, 90)
            self.assertEqual(probe.service_container, "sub2apiplus-b")
            # 其他候选不受影响；未封存 attempt 扫描不会把覆盖收据当成 attempt。
            untouched = codex_upgrade._apply_candidate_runtime_override(
                campaign_dir, manifest, "cand-2"
            )
            self.assertEqual(untouched["configuration"]["codex_account_id"], 90)
            self.assertEqual(
                codex_upgrade._active_unsealed_attempts(campaign_dir, "candidate"), []
            )

    def test_candidate_runtime_override_rejects_semantic_keys_duplicates_and_noops(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, _ = self._create_campaign(root)

            def attempt(*items: str) -> None:
                codex_upgrade.create_candidate_runtime_override(
                    argparse.Namespace(
                        campaign_dir=campaign_dir,
                        candidate_id="cand-x",
                        reason="测试",
                        set=list(items),
                    )
                )

            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不允许在候选层覆盖"):
                attempt("target_source=/tmp/other")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不允许在候选层覆盖"):
                attempt("runtime_image=capture-runtime@sha256:" + "c" * 64)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不允许在候选层覆盖"):
                attempt("codex_account_id=90")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不允许在候选层覆盖"):
                attempt("codex_account_id=abc")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "重复指定"):
                attempt("service_container=sub2apiplus-b", "service_container=sub2apiplus-c")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须同时覆盖"):
                attempt("live_attestation_compose_dir=/tmp/compose")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是合法容器名"):
                attempt("service_container=bad name")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "规范绝对路径"):
                attempt("relay_codex_bin=relative/codex")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "至少提供一个"):
                attempt()
            self.assertFalse(
                (campaign_dir / "candidates" / "cand-x" / "runtime-override.json").exists()
            )

    def test_candidate_runtime_override_is_write_once_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir, manifest = self._create_campaign(root)
            arguments = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id="cand-1",
                reason="换服务容器",
                set=["service_container=sub2apiplus-b"],
            )
            codex_upgrade.create_candidate_runtime_override(arguments)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已存在"):
                codex_upgrade.create_candidate_runtime_override(arguments)
            path = campaign_dir / "candidates" / "cand-1" / "runtime-override.json"
            receipt = json.loads(path.read_text(encoding="utf-8"))
            receipt["overrides"]["service_container"]["successor"] = "sub2apiplus-c"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "摘要或 schema 非法"):
                codex_upgrade._apply_candidate_runtime_override(campaign_dir, manifest, "cand-1")
            # 重新签名但 predecessor 与冻结值不衔接同样拒绝。
            receipt["overrides"]["service_container"] = {
                "predecessor": "wrong-predecessor",
                "successor": "sub2apiplus-c",
            }
            receipt.pop("receipt_digest")
            receipt["receipt_digest"] = codex_upgrade._fingerprint(receipt)
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不衔接"):
                codex_upgrade._apply_candidate_runtime_override(campaign_dir, manifest, "cand-1")
            # 已有 attempt 或已封存的候选不能再登记覆盖。
            (campaign_dir / "candidates" / "cand-2" / "attempts").mkdir(parents=True)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "首个 attempt 前"):
                codex_upgrade.create_candidate_runtime_override(
                    argparse.Namespace(
                        campaign_dir=campaign_dir,
                        candidate_id="cand-2",
                        reason="x",
                        set=["service_container=sub2apiplus-b"],
                    )
                )
            (campaign_dir / "candidates" / "cand-3").mkdir(parents=True)
            (campaign_dir / "candidates" / "cand-3" / "result.json").write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已封存"):
                codex_upgrade.create_candidate_runtime_override(
                    argparse.Namespace(
                        campaign_dir=campaign_dir,
                        candidate_id="cand-3",
                        reason="x",
                        set=["service_container=sub2apiplus-b"],
                    )
                )

    def test_reuse_official_evidence_imports_official_stage_without_legacy_entry(
        self,
    ) -> None:
        """已封存官方证据经正式命令只读导入新 Campaign，可连续复用且不重发取证。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predecessor_dir, predecessor_manifest, _ = (
                self._create_classified_campaign(root / "predecessor")
            )
            predecessor_official = codex_upgrade._load_stage_result(
                predecessor_dir, "capture-official"
            )
            first_dir = root / "reuse-1"
            return_code, stdout, stderr = self._run_main(
                [
                    "reuse-official-evidence",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(first_dir),
                    "--campaign-id",
                    "upgrade-0146-official-reuse-1",
                    "--codex-account-id",
                    "93",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "official_sealed")
            self.assertFalse(result["official_recapture_required"])
            self.assertFalse(result["classification_imported"])
            self.assertTrue(result["classification_reapproval_required"])
            first_manifest = codex_upgrade.load_campaign_manifest(first_dir)
            self.assertEqual(
                first_manifest["predecessor"]["reason"], "official_evidence_reuse"
            )
            self.assertEqual(
                first_manifest["predecessor"]["campaign_id"],
                predecessor_manifest["campaign_id"],
            )
            self.assertEqual(first_manifest["configuration"]["codex_account_id"], 93)
            import_receipt = json.loads(
                (first_dir / "predecessor-import.json").read_text(encoding="utf-8")
            )
            self.assertEqual(import_receipt["reason"], "official_evidence_reuse")
            self.assertEqual(import_receipt["import_mode"], "official_only_reclassification")
            self.assertFalse(
                any(
                    item["kind"] == "approved_classification"
                    for item in import_receipt["copied_files"]
                )
            )
            replayed = codex_upgrade._load_stage_result(first_dir, "capture-official")
            self.assertEqual(
                replayed["evidence_inventory"], predecessor_official["evidence_inventory"]
            )
            self.assertFalse((first_dir / "classification" / "result.json").exists())
            # 同一份官方证据可以再次被只读导入（例如又一次工具修复），不算同根因第二层。
            second_dir = root / "reuse-2"
            return_code, stdout, stderr = self._run_main(
                [
                    "reuse-official-evidence",
                    "--predecessor-campaign-dir",
                    str(first_dir),
                    "--campaign-dir",
                    str(second_dir),
                    "--campaign-id",
                    "upgrade-0146-official-reuse-2",
                    "--codex-account-id",
                    "94",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "official_sealed")
            second_manifest = codex_upgrade.load_campaign_manifest(second_dir)
            self.assertEqual(
                second_manifest["predecessor"]["campaign_id"],
                first_manifest["campaign_id"],
            )
            # 正式目标版本下该命令不属于旧写入入口，不被 campaign-run 旧入口拒绝。
            formal_dir = root / "formal-0151"
            formal_dir.mkdir()
            (formal_dir / "campaign.json").write_text(
                json.dumps({"campaign_mode": "formal", "target_version": "0.151.0"}),
                encoding="utf-8",
            )
            codex_upgrade._reject_campaign_run_legacy_write(
                argparse.Namespace(
                    campaign_dir=root / "new-formal",
                    predecessor_campaign_dir=formal_dir,
                ),
                "reuse-official-evidence",
            )
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "旧写入入口"):
                codex_upgrade._reject_campaign_run_legacy_write(
                    argparse.Namespace(
                        campaign_dir=root / "new-formal",
                        predecessor_campaign_dir=formal_dir,
                    ),
                    "successor",
                )

    def test_0154_reuse_official_evidence_rebuilds_vc0_and_seals_vc1(
        self,
    ) -> None:
        """0.154 官方证据复用必须形成新 Campaign 的零请求 VC 控制链。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "predecessor",
                campaign_id="upgrade-0154-predecessor",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            predecessor_manifest = codex_upgrade.create_campaign(arguments)
            predecessor_dir = arguments.campaign_dir
            self._seal_official_stage(
                root / "predecessor",
                predecessor_dir,
                predecessor_manifest,
            )

            successor_dir = root / "successor"
            return_code, stdout, stderr = self._run_main(
                [
                    "reuse-official-evidence",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0154-successor",
                    "--codex-account-id",
                    "93",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "official_sealed")
            self.assertEqual(result["executed_job_count"], 0)
            self.assertEqual(result["scanned_bytes"], 0)
            self.assertEqual(result["live_request_count"], 0)

            manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            predecessor_plan = codex_upgrade._vc_campaign_plan(
                predecessor_dir,
                predecessor_manifest,
            )
            plan = codex_upgrade._vc_campaign_plan(successor_dir, manifest)
            self.assertEqual(plan["campaign_id"], "upgrade-0154-successor")
            self.assertNotEqual(plan["plan_sha256"], predecessor_plan["plan_sha256"])

            control = manifest["vc_control"]
            batch = json.loads(
                (successor_dir / control["first_formal_batch"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(batch["phase"], "VC-1")
            self.assertEqual(batch["execute_item_ids"], [])
            self.assertEqual(batch["reuse_item_ids"], ["official-test"])
            self.assertEqual(batch["actions"], [])

            run_manifest = json.loads(
                (
                    successor_dir
                    / control["first_campaign_run_manifest"]["path"]
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(run_manifest["no_op"])
            self.assertEqual(run_manifest["execute_items"], [])
            self.assertEqual(run_manifest["reuse_items"], ["official-test"])

            _, checkpoint = codex_upgrade._replay_vc_checkpoint(
                successor_dir,
                plan,
                "VC-1",
            )
            self.assertEqual(checkpoint["execute_item_ids"], [])
            self.assertEqual(checkpoint["reuse_item_ids"], ["official-test"])
            self.assertEqual(
                checkpoint["metrics"],
                {"live_request_count": 0, "scanned_bytes": 0},
            )
            self.assertEqual(
                checkpoint["stage_receipt"]["path"],
                "official/result.json",
            )
            self.assertFalse((successor_dir / "official" / "attempts").exists())

    def test_0154_classification_successor_rebuilds_official_reuse_vc_chain(
        self,
    ) -> None:
        """0.154 分类纠正后继不得继承前序 VC 坐标或 VC-2+ 制品。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "predecessor",
                campaign_id="upgrade-0154-classification-predecessor",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            predecessor_manifest = codex_upgrade.create_campaign(arguments)
            predecessor_dir = arguments.campaign_dir
            timing_ledger = Path(
                predecessor_manifest["control_receipts"]["upgrade_timing"][
                    "ledger_dir"
                ]
            )
            codex_upgrade_timing_ledger.append_event(
                timing_ledger,
                event_id="classification-successor-complete-vc0",
                phase="VC-0",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                timing_ledger,
                event_id="classification-successor-start-vc1",
                phase="VC-1",
                event_type="stage_started",
            )
            self._seal_official_stage(
                root / "predecessor",
                predecessor_dir,
                predecessor_manifest,
            )
            codex_upgrade_timing_ledger.append_event(
                timing_ledger,
                event_id="classification-successor-complete-vc1",
                phase="VC-1",
                event_type="stage_completed",
            )
            codex_upgrade_timing_ledger.append_event(
                timing_ledger,
                event_id="classification-successor-start-vc2",
                phase="VC-2",
                event_type="stage_started",
            )

            rules = tuple(predecessor_manifest["required_rules"])
            patch_manifest_path = (
                Path(__file__).resolve().parents[1]
                / "profile_rule_patches_0_154_0.json"
            )
            patch_manifest = json.loads(
                patch_manifest_path.read_text(encoding="utf-8")
            )
            patched_rules = []
            for patch in patch_manifest["rule_patches"]:
                if patch["rule_id"] not in patched_rules:
                    patched_rules.append(patch["rule_id"])
            migration_path = root / "rule-migration-0154.json"
            self._write_json(
                migration_path,
                {
                    "schema_version": codex_upgrade.MIGRATION_SCHEMA,
                    "baseline_version": "0.151.0",
                    "target_version": "0.154.0",
                    "status": "approved",
                    "entries": [
                        {
                            "baseline_rule": rule,
                            "target_rule": rule,
                            "classification": (
                                "change" if rule in patched_rules else "inherit"
                            ),
                            "rationale": "测试分类事实纠正后继",
                            "evidence_refs": ["official-diff.json"],
                        }
                        for rule in rules
                    ],
                    "discovery_classifications": [],
                },
            )
            catalog_root = (
                Path(__file__).resolve().parents[3]
                / "docs/egress/lifecycle/codex-0154-candidate/catalog-stage/"
                "catalogdata/runtime/profiles"
            )
            active_profile = (
                catalog_root
                / "0.151.0/dbc65378c80a2ad843ce1ba6253a2e47f0dd5d8bc812bb536a2d24ddb7a59e39.json"
            )
            # 目标画像必须是 active 副本加受管补丁清单：与仓库补丁内容解耦，补丁变化时测试
            # 仍按同一派生规则成立。
            target_profile_payload, _ = codex_upgrade._replace_json_string_literal(
                json.loads(active_profile.read_text(encoding="utf-8")),
                "0.151.0",
                "0.154.0",
            )
            for patch in patch_manifest["rule_patches"]:
                codex_upgrade._profile_pointer_replace(
                    target_profile_payload, patch["path"], patch["after"]
                )
            target_profile_payload["Digest"] = codex_upgrade._fingerprint(
                {"test": "classification-successor", "payload": target_profile_payload}
            )
            profile_path = root / "profile-0154.json"
            self._write_json(
                profile_path,
                {
                    "schema_version": codex_upgrade.PROFILE_SCHEMA,
                    "codex_version": "0.154.0",
                    "profile_id": "codex-0.154.0-test-v1",
                    "profile_digest": target_profile_payload["Digest"],
                    "profile_payload": target_profile_payload,
                    "profile_payload_sha256": codex_upgrade._fingerprint(
                        target_profile_payload
                    ),
                    "status": "approved",
                },
            )
            assertion_profile = root / "assertion-profile-0154.json"
            self._write_assertion_profile(
                assertion_profile,
                rules,
                version="0.154.0",
            )
            classification_arguments = {
                "target_rule_manifest": root / "predecessor/target-rules.json",
                "migration_manifest": migration_path,
                "scenario_manifest": root / "predecessor/target-scenarios.json",
                "profile_manifest": profile_path,
                "assertion_profile_manifest": assertion_profile,
                "active_profile": active_profile,
                "profile_patch_manifest": (
                    Path(__file__).resolve().parents[1]
                    / "profile_rule_patches_0_154_0.json"
                ),
            }
            preview = codex_upgrade.classify_campaign(
                predecessor_dir,
                **classification_arguments,
            )
            approved = codex_upgrade.classify_campaign(
                predecessor_dir,
                **classification_arguments,
                approve_manifest_sha256=preview["joint_manifest_sha256"],
            )
            self.assertEqual(approved["status"], "complete")
            self.assertTrue(
                (predecessor_dir / "control/vc/vc-2-checkpoint.json").is_file()
            )

            successor_dir = root / "successor"
            return_code, stdout, stderr = self._run_main(
                [
                    "reuse-official-evidence",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0154-classification-successor",
                    "--codex-account-id",
                    "93",
                    "--classification-fact-correction",
                ]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "official_sealed")
            self.assertEqual(result["executed_job_count"], 0)
            self.assertEqual(result["live_request_count"], 0)

            manifest = codex_upgrade.load_campaign_manifest(successor_dir)
            predecessor_plan = codex_upgrade._vc_campaign_plan(
                predecessor_dir,
                predecessor_manifest,
            )
            plan = codex_upgrade._vc_campaign_plan(successor_dir, manifest)
            self.assertEqual(
                plan["campaign_id"],
                "upgrade-0154-classification-successor",
            )
            self.assertNotEqual(plan["plan_sha256"], predecessor_plan["plan_sha256"])
            control = manifest["vc_control"]
            batch = json.loads(
                (successor_dir / control["first_formal_batch"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(batch["execute_item_ids"], [])
            self.assertEqual(batch["reuse_item_ids"], ["official-test"])
            self.assertEqual(batch["actions"], [])
            _, checkpoint = codex_upgrade._replay_vc_checkpoint(
                successor_dir,
                plan,
                "VC-1",
            )
            self.assertEqual(checkpoint["execute_item_ids"], [])
            self.assertEqual(checkpoint["reuse_item_ids"], ["official-test"])
            self.assertEqual(
                checkpoint["metrics"],
                {"live_request_count": 0, "scanned_bytes": 0},
            )
            self.assertEqual(
                sorted(
                    path.name
                    for path in (successor_dir / "control/vc").glob(
                        "vc-*-checkpoint.json"
                    )
                ),
                ["vc-0-checkpoint.json", "vc-1-checkpoint.json"],
            )

    # ------------------------------------------------------------------
    # A3a：官方证据从 awaiting_receipts 前序 attempt 只读导入
    # ------------------------------------------------------------------

    @staticmethod
    def _tree_digests(root: Path) -> dict[str, tuple[str, str | None]]:
        """逐文件记录 mode 与内容摘要，用于证明前序目录在导入前后逐字节不变。"""

        digests: dict[str, tuple[str, str | None]] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                continue
            mode = format(path.stat().st_mode & 0o777, "04o")
            digest = codex_upgrade.file_sha256(path) if path.is_file() else None
            digests[path.relative_to(root).as_posix()] = (mode, digest)
        return digests

    def _write_import_receipt(
        self,
        path: Path,
        payload: dict[str, object],
        *,
        self_digest: bool = False,
    ) -> Path:
        document = dict(payload)
        if self_digest:
            document["receipt_sha256"] = codex_upgrade._fingerprint(document)
        self._write_json(path, document)
        path.chmod(0o600)
        return path

    def _official_attempt_import_fixture(self, root: Path) -> dict[str, object]:
        """构造 A3a 导入所需的前序 awaiting_receipts attempt 与七份绑定收据。

        前序 Campaign 放在规范的 ``<data>/evidence/campaigns/<id>`` 布局下，
        权限收口走真实两步式命令；审计、裁定、部署、路径认证与策略激活收据
        按各自 schema 手写，五摘要取当前工具身份。
        """

        data = root / "data"
        campaigns = data / "evidence" / "campaigns"
        campaigns.mkdir(parents=True)
        for path in (data, data / "evidence", campaigns):
            path.chmod(0o700)
        ledger_root = project_ledger_fixture.install_fixture_ledger(data)
        arguments = self._campaign_arguments(
            campaigns,
            campaign_id="upgrade-0154-fresh",
            baseline_version="0.151.0",
            target_version="0.154.0",
            model="gpt-5.5",
            lite_model="gpt-6-astra",
        )
        # 生产布局下目录名等于 campaign_id；后继坐标迁移只替换 campaign_id，
        # 夹具必须保持同样的对应关系。
        arguments.campaign_dir = campaigns / "upgrade-0154-fresh"
        predecessor_manifest = codex_upgrade.create_campaign(arguments)
        predecessor_dir = arguments.campaign_dir
        evidence_root = self._seal_official_stage(
            campaigns,
            predecessor_dir,
            predecessor_manifest,
            seal=False,
            bind_environment=True,
        )
        attempt_id = "20260731T000000Z-1111111111111111"
        attempt_root = predecessor_dir / "official" / "attempts" / attempt_id
        attempt_path = attempt_root / "attempt.json"
        attempt_sha256 = codex_upgrade.file_sha256(attempt_path)
        source_attempt = json.loads(attempt_path.read_text(encoding="utf-8"))

        # A3b-1：经正式 CLI 走真实两步式权限收口（preview → apply → replay）。
        return_code, stdout, stderr = self._run_main(
            [
                "harden-evidence-permissions",
                "preview",
                "--campaign-dir",
                str(predecessor_dir),
                "--attempt-id",
                attempt_id,
            ]
        )
        self.assertEqual(return_code, 0, stderr)
        preview = json.loads(stdout)
        self.assertEqual(preview["status"], "approval_required")
        self.assertNotIn("entries", preview)
        return_code, stdout, stderr = self._run_main(
            [
                "harden-evidence-permissions",
                "apply",
                "--campaign-dir",
                str(predecessor_dir),
                "--attempt-id",
                attempt_id,
                "--approve-sha256",
                preview["review_sha256"],
            ]
        )
        self.assertEqual(return_code, 0, stderr)
        applied = json.loads(stdout)
        self.assertEqual(applied["status"], "applied")
        return_code, stdout, stderr = self._run_main(
            [
                "harden-evidence-permissions",
                "replay",
                "--campaign-dir",
                str(predecessor_dir),
                "--attempt-id",
                attempt_id,
            ]
        )
        self.assertEqual(return_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "passed")
        permission_path = (
            predecessor_dir
            / "control"
            / "evidence-permissions"
            / attempt_id
            / f"apply-{int(applied['index']):02d}.json"
        )
        _root, roots, _attempt = harden._evidence_roots(predecessor_dir, attempt_id)
        entries = harden._snapshot(roots, with_content=True)
        inventory = sorted(
            (
                {
                    "path": str(Path(entry["path"]).resolve()),
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                }
                for entry in entries
                if entry["kind"] == "file"
            ),
            key=lambda item: item["path"],
        )
        tool = codex_upgrade._tool_identity(include_git=False)
        identity_five = {
            "policy_sha256": tool["policy_sha256"],
            "wire_producer_sha256": tool["wire_producer_sha256"],
            "evidence_semantics_sha256": tool["evidence_semantics_sha256"],
            "control_sha256": tool["control_sha256"],
            "tool_files_sha256": tool["files_sha256"],
        }
        now = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        receipts = root / "receipts"
        receipts.mkdir(mode=0o700)
        audit_path = self._write_import_receipt(
            receipts / "audit.json",
            {
                "schema_version": attempt_audit.SCHEMA_VERSION,
                "campaign_id": predecessor_manifest["campaign_id"],
                "attempt_id": attempt_id,
                "attempt_status": "awaiting_receipts",
                "attempt_sha256": attempt_sha256,
                "observed_at_utc": now,
                "status": "passed",
                "failed_sections": [],
                "identity": {"passed": True, "mismatched_fields": []},
                "account": {"passed": True},
                "models": {"passed": True, "problems": []},
                "environment": {"passed": True, "problems": []},
                "integrity": {
                    "passed": True,
                    "problems": [],
                    "checkpoint_count": 1,
                    "checkpoint_statuses": {"complete": 1},
                    "evidence_root_count": len(roots),
                    "missing_evidence_roots": [],
                    "inventory_file_count": len(inventory),
                    "inventory_bytes": sum(int(item["bytes"]) for item in inventory),
                    "inventory_sha256": codex_upgrade._fingerprint(inventory),
                    "inventory": inventory,
                    "nonconforming_permission_entries": 0,
                },
                "requests": {
                    "status": "complete",
                    "counting_rule": "turn_ratio",
                    "estimation_policy": "none",
                    "precise_total": 0,
                    "estimated_total": 0,
                    "unresolved_job_ids": [],
                    "identity_keys_sha256": "0" * 64,
                },
                "permissions": {
                    "nonconforming_entries": 0,
                    "action": "只报告；收口由 harden-evidence-permissions 两步式命令执行",
                },
            },
        )
        verdict_path = self._write_import_receipt(
            receipts / "verdict.json",
            {
                "schema_version": wire_transition.VERDICT_SCHEMA,
                "campaign_id": predecessor_manifest["campaign_id"],
                "attempt_id": attempt_id,
                "observed_at_utc": now,
                "policy_sha256": tool["policy_sha256"],
                "frozen_files_sha256": predecessor_manifest["tool_identity"]["files_sha256"],
                "frozen_v2_identity": None,
                "active_deployment_receipt": None,
                "historical_copy": None,
                "historical_copy_source_receipt": None,
                "historical_wire_producer_sha256": tool["wire_producer_sha256"],
                "current_wire_producer_sha256": tool["wire_producer_sha256"],
                "current_files_sha256": tool["files_sha256"],
                "problems": [],
                "basis": "historical_copy_policy_v2",
                "verdict": "equal",
            },
        )
        deployment_path = self._write_import_receipt(
            receipts / "deployment.json",
            {
                "schema_version": codex_upgrade.ARM64_SUPERVISED_DEPLOY_RECEIPT_SCHEMA,
                "status": "passed",
                "campaign_id": "codex-0154-a3a-deploy",
                "created_at_utc": now,
                "architecture": "aarch64",
                "production_tool_root": "/root/docker/capture-cli/data/tools/official_client_capture",
                "production_doc_root": "/root/docker/capture-cli/data/docs",
                "policy_version": tool["policy_version"],
                **identity_five,
                "supervisor_sha256": "1" * 64,
                "assertion_preparer_sha256": "2" * 64,
                "rollback_backup": None,
                "assertion_preparer_rollback_backup": None,
                "document_rollback_backup": None,
                "switched_archived_documents": [],
                "installed_runtime_documents": [],
                "supervisor_run_dir": "/root/docker/capture-cli/data/control/deploy-run",
            },
        )
        deployment_binding = {
            "path": str(deployment_path),
            "sha256": codex_upgrade.file_sha256(deployment_path),
        }
        certification_path = self._write_import_receipt(
            receipts / "path-certification.json",
            {
                "schema_version": codex_upgrade.PRE_A3_PATH_CERTIFICATION_SCHEMA,
                "status": "passed",
                "certified_at_utc": now,
                "identity": identity_five,
                "deployment_receipt": deployment_binding,
                "scenarios": ["reuse-official-evidence", "seal"],
            },
            self_digest=True,
        )
        activation_path = self._write_import_receipt(
            receipts / "policy-activation.json",
            {
                "schema_version": codex_upgrade.POLICY_ACTIVATION_CERTIFICATION_SCHEMA,
                "status": "active",
                "activated_at_utc": now,
                "policy_version": tool["policy_version"],
                "policy_sha256": tool["policy_sha256"],
                "identity": identity_five,
                "deployment_receipt": deployment_binding,
                "authorized_scopes": ["A2.5", "A3b"],
                "superseded_by": None,
            },
            self_digest=True,
        )
        return {
            "data": data,
            "campaigns": campaigns,
            "ledger": ledger_root,
            "predecessor_dir": predecessor_dir,
            "predecessor_manifest": predecessor_manifest,
            "attempt_id": attempt_id,
            "attempt_root": attempt_root,
            "attempt_sha256": attempt_sha256,
            "source_attempt": source_attempt,
            "evidence_root": evidence_root,
            "audit": audit_path,
            "verdict": verdict_path,
            "permission": permission_path,
            "certification": certification_path,
            "activation": activation_path,
            "deployment": deployment_path,
            "tool": tool,
            "identity_five": identity_five,
        }

    @staticmethod
    def _official_attempt_import_argv(
        fixture: dict[str, object],
        successor_dir: Path,
        *,
        omit: tuple[str, ...] = (),
    ) -> list[str]:
        options = [
            ("--predecessor-campaign-dir", fixture["predecessor_dir"]),
            ("--campaign-dir", successor_dir),
            ("--campaign-id", "upgrade-0154-official-reuse"),
            ("--codex-account-id", "93"),
            ("--predecessor-official-attempt-id", fixture["attempt_id"]),
            ("--audit-receipt", fixture["audit"]),
            ("--identity-verdict", fixture["verdict"]),
            ("--permission-receipt", fixture["permission"]),
            ("--path-certification", fixture["certification"]),
            ("--policy-activation", fixture["activation"]),
            ("--deployment-receipt", fixture["deployment"]),
            ("--project-ledger", fixture["ledger"]),
        ]
        argv = ["reuse-official-evidence"]
        for flag, value in options:
            if flag in omit:
                continue
            argv.extend([flag, str(value)])
        return argv

    @staticmethod
    def _fake_permission_closeout(attempt_root: Path, evidence_roots: object) -> dict[str, object]:
        """离线夹具没有受管 runs 别名，权限收口收据用可重放的替身文件代替。"""

        receipt = attempt_root / "evidence-permission-closeout.json"
        receipt.write_text(
            json.dumps(
                {
                    "schema_version": "codex-upgrade-evidence-permission-closeout/v1",
                    "status": "passed",
                    "evidence_roots": [str(root) for root in evidence_roots],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        receipt.chmod(0o600)
        return {
            "path": receipt.name,
            "sha256": codex_upgrade.file_sha256(receipt),
            "bytes": receipt.stat().st_size,
        }

    def test_0154_reuse_official_evidence_imports_awaiting_receipts_attempt_and_seals(
        self,
    ) -> None:
        """A3a：前序停在 awaiting_receipts 时，七份收据绑定后零执行导入并由新 Campaign seal。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._official_attempt_import_fixture(root)
            predecessor_dir = fixture["predecessor_dir"]
            evidence_root = fixture["evidence_root"]
            source_attempt = fixture["source_attempt"]
            before = self._tree_digests(predecessor_dir)
            evidence_before = self._tree_digests(evidence_root)
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            successor_dir = fixture["campaigns"] / "upgrade-0154-official-reuse"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_close_official_reuse_evidence_permissions",
                    side_effect=self._fake_permission_closeout,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_replay_attempt_evidence_permissions",
                    return_value={},
                ),
            ):
                return_code, stdout, stderr = self._run_main(
                    self._official_attempt_import_argv(fixture, successor_dir)
                )
                self.assertEqual(return_code, 0, stderr)
                result = json.loads(stdout)
                self.assertEqual(result["status"], "official_awaiting_receipts")
                self.assertFalse(result["official_sealed"])
                self.assertTrue(result["official_imported"])
                self.assertEqual(result["import_mode"], "official_attempt_reuse")
                self.assertEqual(result["executed_job_count"], 0)
                self.assertEqual(result["live_request_count"], 0)
                self.assertEqual(result["scanned_bytes"], 0)
                self.assertIn("零请求", result["next_command"])
                attempt_id = result["official_attempt_id"]
                self.assertTrue(attempt_id)
                # 前序目录与前序证据逐字节、逐 mode 不变。
                self.assertEqual(self._tree_digests(predecessor_dir), before)
                self.assertEqual(self._tree_digests(evidence_root), evidence_before)

                import_receipt = json.loads(
                    (successor_dir / "predecessor-import.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    import_receipt["schema_version"],
                    codex_upgrade.PREDECESSOR_OFFICIAL_ATTEMPT_IMPORT_SCHEMA,
                )
                self.assertEqual(import_receipt["import_mode"], "official_attempt_reuse")
                self.assertEqual(import_receipt["stages"], {})
                imported = import_receipt["official_attempt_import"]
                self.assertEqual(
                    imported["source_attempt"]["attempt_digest"],
                    source_attempt["attempt_digest"],
                )
                self.assertEqual(imported["source_attempt"]["sha256"], fixture["attempt_sha256"])
                self.assertEqual(imported["source_attempt"]["job_ids"], ["official-test"])
                self.assertEqual(imported["identity_verdict"]["verdict"], "equal")
                self.assertTrue(imported["content_proof"]["matches_audit_inventory"])
                self.assertTrue(imported["content_proof"]["matches_permission_receipt"])
                self.assertEqual(
                    imported["content_proof"]["content_sha256"],
                    json.loads(fixture["permission"].read_text(encoding="utf-8"))[
                        "content_sha256_after"
                    ],
                )
                self.assertEqual(imported["deployment_receipt"]["tool_files_sha256"], fixture["tool"]["files_sha256"])
                self.assertEqual(imported["tool_identity"]["wire_producer_sha256"], fixture["tool"]["wire_producer_sha256"])
                self.assertEqual(imported["project_ledger"]["head_sha256"], head_before["head_sha256"])

                attempt_root = successor_dir / "official" / "attempts" / attempt_id
                attempt = json.loads((attempt_root / "attempt.json").read_text(encoding="utf-8"))
                self.assertEqual(attempt["schema_version"], codex_upgrade.CAPTURE_ATTEMPT_SCHEMA)
                self.assertEqual(attempt["status"], "awaiting_receipts")
                self.assertEqual(
                    attempt["official_evidence_reuse_transition"]["path"],
                    "predecessor-import.json",
                )
                self.assertEqual(attempt["evidence_roots"], source_attempt["evidence_roots"])
                self.assertEqual(attempt["environment"]["evidence_root"], str(evidence_root))
                self.assertIsNone(attempt["environment"]["arm64_before_receipt"])
                self.assertEqual(attempt["incremental_plan"]["reused_job_ids"], ["official-test"])
                self.assertEqual(attempt["incremental_plan"]["executed_job_ids"], [])
                self.assertEqual(len(attempt["results"]), 1)
                reused = attempt["results"][0]
                self.assertEqual(reused["disposition"], "reused")
                self.assertEqual(reused["carried_from_attempt"], fixture["attempt_id"])
                self.assertEqual(reused["source_receipt"]["path"], "predecessor-import.json")
                # 执行摘要按新 Campaign 坐标重绑，前序证据根原样保留。
                self.assertNotEqual(
                    reused["execution_sha256"],
                    source_attempt["results"][0]["execution_sha256"],
                )
                self.assertEqual(reused["evidence_roots"], source_attempt["results"][0]["evidence_roots"])
                self.assertFalse((successor_dir / "official" / "result.json").exists())
                self.assertEqual(
                    codex_upgrade.campaign_status(successor_dir)["status"],
                    "official_awaiting_receipts",
                )
                # 总账：已注册且消费者门禁放行；导入本身不消耗任何请求预算。
                admitted = codex_upgrade_project_ledger.assert_campaign_admitted(
                    successor_dir, command="seal", require=True
                )
                self.assertEqual(admitted["campaign_id"], "upgrade-0154-official-reuse")
                head_after = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
                self.assertEqual(
                    head_after["remaining_live_requests"],
                    head_before["remaining_live_requests"],
                )

                # seal：零请求，环境与恢复收据来自前序 attempt，证据清单按前序证据根构建。
                capture_manifest = evidence_root / "capture-manifest.json"
                capture_binding = self._binding(
                    capture_manifest, f"{evidence_root.name}/{capture_manifest.name}"
                )
                assertion_context = {
                    "capture_manifest": capture_binding,
                    "capture_manifest_path": str(capture_manifest.resolve()),
                    "evidence_root": str(evidence_root.resolve()),
                    "evidence_prefix": evidence_root.name,
                }
                assertion_gate = {
                    "side": "official",
                    "bundle_dir_name": "assertion-bundle",
                    "bundle_provenance_sha256": "1" * 64,
                    "bundle_entry_count": 1,
                    "derived_provenance_sha256": None,
                    "candidate_trace_receipt_sha256": None,
                    "capture_manifest": {
                        "path": "capture-manifest.json",
                        "sha256": capture_binding["sha256"],
                    },
                    "acceptance_contract_sha256": "2" * 64,
                    "artifact_count": 1,
                    "observation_count": 1,
                    "checked_rule_count": 1,
                    "checked_check_count": 1,
                }

                original_replay = codex_upgrade.codex_upgrade_arm64_environment_receipt.replay

                def replay_arm64(directory: Path, name: str) -> dict[str, object]:
                    # 只替换前序 attempt 的环境收据；Campaign 控制收据仍走真实重放。
                    directory = Path(directory)
                    if directory.parent != evidence_root / "environment":
                        return original_replay(directory, name)
                    return {
                        "status": "passed",
                        "phase": (
                            "attempt_before"
                            if "before" in directory.name
                            else "attempt_after"
                        ),
                        "subject_id": fixture["attempt_id"],
                        "continuity_identity_sha256": "c" * 64,
                    }

                seal_arguments = argparse.Namespace(
                    campaign_dir=successor_dir,
                    attempt_id=attempt_id,
                    approve_seal_sha256=None,
                    evidence_root=[],
                    capture_manifest=None,
                    assertion_evidence_root=None,
                    restoration_report=None,
                )
                with (
                    mock.patch.object(
                        codex_upgrade,
                        "_verify_official_binaries",
                        return_value=source_attempt["binary_verification"],
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_capture_assertion_context",
                        return_value=assertion_context,
                    ),
                    mock.patch.object(
                        codex_upgrade,
                        "_run_seal_assertion_gate",
                        return_value=assertion_gate,
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_arm64_environment_receipt,
                        "replay",
                        side_effect=replay_arm64,
                    ),
                    mock.patch.object(
                        codex_upgrade.codex_upgrade_arm64_environment_receipt,
                        "receipts_equivalent",
                        side_effect=lambda left_root, left, right_root, right:
                        left["continuity_identity_sha256"] == right["continuity_identity_sha256"],
                    ),
                ):
                    preview = codex_upgrade._seal_capture_attempt(seal_arguments, "official")
                    self.assertEqual(preview["status"], "approval_required")
                    self.assertEqual(
                        codex_upgrade.campaign_status(successor_dir)["status"],
                        "official_awaiting_seal_approval",
                    )
                    seal_arguments.approve_seal_sha256 = preview["review_sha256"]
                    sealed = codex_upgrade._seal_capture_attempt(seal_arguments, "official")
                    self.assertEqual(sealed["status"], "complete")
                self.assertEqual(
                    codex_upgrade.campaign_status(successor_dir)["status"],
                    "official_sealed",
                )
                stage = codex_upgrade._load_stage_result(
                    successor_dir,
                    "capture-official",
                    _replay_machine_receipts=False,
                )
                self.assertEqual(stage["status"], "complete")
                self.assertEqual([item["id"] for item in stage["results"]], ["official-test"])
                self.assertEqual(stage["results"][0]["disposition"], "reused")
                manifest = codex_upgrade.load_campaign_manifest(successor_dir)
                plan = codex_upgrade._vc_campaign_plan(successor_dir, manifest)
                _, checkpoint = codex_upgrade._replay_vc_checkpoint(successor_dir, plan, "VC-1")
                self.assertEqual(checkpoint["execute_item_ids"], [])
                self.assertEqual(checkpoint["reuse_item_ids"], ["official-test"])
                self.assertEqual(checkpoint["metrics"]["live_request_count"], 0)
                self.assertEqual(checkpoint["stage_receipt"]["path"], "official/result.json")
                # R6：seal 产生派生清单和 preview 后仍可重入原导入命令，自动结束 VC-0／VC-1。
                sealed_before = self._tree_digests(successor_dir)
                code, stdout, stderr = self._run_main(
                    self._official_attempt_import_argv(fixture, successor_dir)
                )
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["official_attempt_id"], attempt_id)
                self.assertEqual(self._tree_digests(successor_dir), sealed_before)
                timing_dir = codex_upgrade._campaign_timing_ledger_dir(successor_dir, manifest)
                timing_state = codex_upgrade_timing_ledger.phase_ledger_state(timing_dir)
                self.assertIsNone(timing_state["active_phase"])
                self.assertEqual(timing_state["completed_phases"], ["VC-0", "VC-1"])
                # seal 之后前序仍然逐字节不变，总账请求计数增量为 0。
                self.assertEqual(self._tree_digests(predecessor_dir), before)
                head_sealed = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
                self.assertEqual(
                    head_sealed["remaining_live_requests"],
                    head_before["remaining_live_requests"],
                )

    def test_0154_official_attempt_import_rejects_broken_bindings(self) -> None:
        """A3a：非法输入发布前拒绝；R6 物化失败保留已发布事务，原命令可继续。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._official_attempt_import_fixture(root)
            predecessor_dir = fixture["predecessor_dir"]
            before = self._tree_digests(predecessor_dir)
            successor_dir = fixture["campaigns"] / "upgrade-0154-official-reuse"

            def expect_rejection(argv: list[str], fragment: str) -> None:
                return_code, _stdout, stderr = self._run_main(argv)
                self.assertNotEqual(return_code, 0)
                self.assertIn(fragment, stderr)
                self.assertFalse(successor_dir.exists(), stderr)
                self.assertEqual(self._tree_digests(predecessor_dir), before)

            def rewrite(path: Path, mutate) -> None:
                payload = json.loads(path.read_text(encoding="utf-8"))
                mutate(payload)
                self._write_json(path, payload)
                path.chmod(0o600)

            with (
                mock.patch.object(
                    codex_upgrade,
                    "_close_official_reuse_evidence_permissions",
                    side_effect=self._fake_permission_closeout,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_replay_attempt_evidence_permissions",
                    return_value={},
                ),
            ):
                # 缺少任一导入参数。
                expect_rejection(
                    self._official_attempt_import_argv(
                        fixture, successor_dir, omit=("--audit-receipt",)
                    ),
                    "必须同时提供：--audit-receipt",
                )
                # 审计未通过。
                audit_backup = fixture["audit"].read_text(encoding="utf-8")
                rewrite(fixture["audit"], lambda payload: payload.update(status="failed", failed_sections=["models"]))
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "A1a 审计收据未通过",
                )
                fixture["audit"].write_text(audit_backup, encoding="utf-8")
                # 审计 inventory 与当前证据不一致。
                rewrite(fixture["audit"], lambda payload: payload["integrity"]["inventory"].pop())
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "A1a 审计 inventory 不一致",
                )
                fixture["audit"].write_text(audit_backup, encoding="utf-8")
                # 裁定不是「相等」。
                verdict_backup = fixture["verdict"].read_text(encoding="utf-8")
                rewrite(fixture["verdict"], lambda payload: payload.update(verdict="different"))
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "A1b 身份裁定",
                )
                fixture["verdict"].write_text(verdict_backup, encoding="utf-8")
                # 部署收据五摘要不是当前工具。
                deployment_backup = fixture["deployment"].read_text(encoding="utf-8")
                rewrite(fixture["deployment"], lambda payload: payload.update(wire_producer_sha256="0" * 64))
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "五摘要与当前工具身份不一致",
                )
                fixture["deployment"].write_text(deployment_backup, encoding="utf-8")
                # 路径认证绑定的不是这份部署收据（部署收据字节变化即失效）。
                rewrite(fixture["deployment"], lambda payload: payload.update(supervisor_run_dir="/elsewhere"))
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "pre-A3 路径认证收据",
                )
                fixture["deployment"].write_text(deployment_backup, encoding="utf-8")
                # 策略激活已被替换。
                activation_backup = fixture["activation"].read_text(encoding="utf-8")
                rewrite(
                    fixture["activation"],
                    lambda payload: (
                        payload.update(superseded_by="tool-release-certification/v1"),
                        payload.pop("receipt_sha256"),
                        payload.update(receipt_sha256=codex_upgrade._fingerprint(payload)),
                    ),
                )
                expect_rejection(
                    self._official_attempt_import_argv(fixture, successor_dir),
                    "已被后续认证替换",
                )
                fixture["activation"].write_text(activation_backup, encoding="utf-8")
                # 前序证据内容在权限收口后被改动（mode 不变）。
                surface_path = fixture["evidence_root"] / "surface.json"
                surface_backup = surface_path.read_bytes()
                surface_path.write_bytes(surface_backup + b"\n")
                surface_path.chmod(0o600)
                return_code, _stdout, stderr = self._run_main(
                    self._official_attempt_import_argv(fixture, successor_dir)
                )
                self.assertNotEqual(return_code, 0)
                self.assertIn("内容在权限收口后发生变化", stderr)
                self.assertFalse(successor_dir.exists())
                surface_path.write_bytes(surface_backup)
                surface_path.chmod(0o600)
                self.assertEqual(self._tree_digests(predecessor_dir), before)
                # 前序未封存却不提供导入参数。
                expect_rejection(
                    self._official_attempt_import_argv(
                        fixture,
                        successor_dir,
                        omit=(
                            "--predecessor-official-attempt-id",
                            "--audit-receipt",
                            "--identity-verdict",
                            "--permission-receipt",
                            "--path-certification",
                            "--policy-activation",
                            "--deployment-receipt",
                            "--project-ledger",
                        ),
                    ),
                    "前序官方阶段尚未封存",
                )
                # R6：物化失败不删除已发布事务；总账尚未注册，原预约保留供原命令续作。
                with mock.patch.object(
                    codex_upgrade,
                    "_close_official_reuse_evidence_permissions",
                    side_effect=codex_upgrade.ConfigurationError("前序证据仍有 1 个条目未达到 0700/0600"),
                ):
                    code, _, stderr = self._run_main(
                        self._official_attempt_import_argv(fixture, successor_dir)
                    )
                    self.assertNotEqual(code, 0)
                    self.assertIn("未达到 0700/0600", stderr)
                self.assertTrue((successor_dir / "control/official-reuse-resume.json").is_file())
                published = self._tree_digests(successor_dir)
                head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
                self.assertNotIn("upgrade-0154-official-reuse", head["registered_campaigns"])
                # 修好全部绑定后仍能成功导入，证明上面的拒绝没有污染前序。
                return_code, stdout, stderr = self._run_main(
                    self._official_attempt_import_argv(fixture, successor_dir)
                )
                self.assertEqual(return_code, 0, stderr)
                self.assertEqual(json.loads(stdout)["status"], "official_awaiting_receipts")
                resumed = self._tree_digests(successor_dir)
                self.assertEqual({key: resumed[key] for key in published}, published)
                self.assertEqual(self._tree_digests(predecessor_dir), before)

    def test_0154_official_attempt_import_arguments_rejected_for_sealed_predecessor(
        self,
    ) -> None:
        """前序已封存时走既有只读导入路径，八项 attempt 导入参数一律拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_ledger_fixture.install_fixture_ledger(root)
            arguments = self._campaign_arguments(
                root / "predecessor",
                campaign_id="upgrade-0154-sealed",
                baseline_version="0.151.0",
                target_version="0.154.0",
                model="gpt-5.5",
                lite_model="gpt-6-astra",
            )
            predecessor_manifest = codex_upgrade.create_campaign(arguments)
            predecessor_dir = arguments.campaign_dir
            self._seal_official_stage(root / "predecessor", predecessor_dir, predecessor_manifest)
            successor_dir = root / "successor"
            return_code, _stdout, stderr = self._run_main(
                [
                    "reuse-official-evidence",
                    "--predecessor-campaign-dir",
                    str(predecessor_dir),
                    "--campaign-dir",
                    str(successor_dir),
                    "--campaign-id",
                    "upgrade-0154-sealed-successor",
                    "--codex-account-id",
                    "93",
                    "--predecessor-official-attempt-id",
                    "20260731T000000Z-1111111111111111",
                ]
            )
            self.assertNotEqual(return_code, 0)
            self.assertIn("只允许在前序官方阶段尚未封存时使用", stderr)
            self.assertFalse(successor_dir.exists())
            # A2 裁定命令经正式 CLI 可达：没有部署收据时如实裁定为「不同」并返回 3。
            control_root = root / "control"
            control_root.mkdir(mode=0o700)
            verdict_output = root / "verdict.json"
            return_code, stdout, stderr = self._run_main(
                [
                    "verdict-official-attempt-identity",
                    "--campaign-dir",
                    str(predecessor_dir),
                    "--attempt-id",
                    "20260731T000000Z-1111111111111111",
                    "--control-root",
                    str(control_root),
                    "--output",
                    str(verdict_output),
                ]
            )
            self.assertEqual(return_code, 3, stderr)
            self.assertEqual(json.loads(stdout)["verdict"], "different")
            self.assertTrue(verdict_output.is_file())

    # ------------------------------------------------------------------
    # B0：两个 reconciler、先入账后判定、恢复预览与批准、resume 衔接
    # ------------------------------------------------------------------

    def _b0_fixture(self, root: Path, *, campaign_id: str = "upgrade-0154-b0") -> dict[str, object]:
        """规范宿主布局下的 0.154 Formal Campaign：总账、部署收据、Campaign 账本齐全。"""

        data = root / "data"
        campaigns = data / "evidence" / "campaigns"
        control = data / "control"
        campaigns.mkdir(parents=True)
        control.mkdir()
        for path in (data, data / "evidence", campaigns, control):
            path.chmod(0o700)
        ledger_root = project_ledger_fixture.install_fixture_ledger(data)
        arguments = self._campaign_arguments(
            campaigns,
            campaign_id=campaign_id,
            baseline_version="0.151.0",
            target_version="0.154.0",
            model="gpt-5.5",
            lite_model="gpt-6-astra",
        )
        arguments.campaign_dir = campaigns / campaign_id
        # 夹具 Job 的证据根是宿主路径（<campaign_dir>/official-evidence），provenance 与审计
        # 按“已在宿主数据根内”的规则直接使用，不经 CAPTURE_ROOT 映射。
        manifest = codex_upgrade.create_campaign(arguments)
        campaign_dir = arguments.campaign_dir
        tool = codex_upgrade._tool_identity(include_git=False)
        now = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        deployment = self._write_import_receipt(
            control / "codex-0154-supervisor-enable-20260915t170000z.json",
            {
                "schema_version": codex_upgrade.ARM64_SUPERVISED_DEPLOY_RECEIPT_SCHEMA,
                "status": "passed",
                "campaign_id": "codex-0154-b0-deploy",
                "created_at_utc": now,
                "architecture": "aarch64",
                "production_tool_root": "/root/docker/capture-cli/data/tools/official_client_capture",
                "production_doc_root": "/root/docker/capture-cli/data/docs",
                "tool_files_sha256": tool["files_sha256"],
                "policy_version": tool["policy_version"],
                "policy_sha256": tool["policy_sha256"],
                "wire_producer_sha256": tool["wire_producer_sha256"],
                "evidence_semantics_sha256": tool["evidence_semantics_sha256"],
                "control_sha256": tool["control_sha256"],
                "supervisor_sha256": "1" * 64,
                "assertion_preparer_sha256": "2" * 64,
                "rollback_backup": None,
            },
        )
        jobs = codex_upgrade._campaign_jobs(campaign_dir, manifest, "official")
        return {
            "data": data,
            "control": control,
            "ledger": ledger_root,
            "campaign_dir": campaign_dir,
            "manifest": manifest,
            "jobs": jobs,
            "tool": tool,
            "deployment": deployment,
            "timing_ledger": Path(str(manifest["control_receipts"]["upgrade_timing"]["ledger_dir"])),
        }

    def _b0_orphan_attempt(
        self,
        fixture: dict[str, object],
        *,
        complete_job: bool = False,
        evidence_roots: list[Path] | None = None,
    ) -> str:
        """发布孤儿 attempt；可选写 Job 收据，并仅在完整完成时写 checkpoint。"""

        campaign_dir = fixture["campaign_dir"]
        manifest = fixture["manifest"]
        jobs = fixture["jobs"]
        attempt_root, reservation = codex_upgrade._reserve_capture_attempt(
            campaign_dir,
            phase="official",
            candidate_id=None,
            identity=dict(manifest["official_identity"]),
            jobs=jobs,
            allow_failed_rerun=True,
        )
        if complete_job or evidence_roots is not None:
            job = jobs[0]
            result = {
                "id": job.job_id,
                "phase": "official",
                "required": True,
                "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "complete",
                "description": "合成 Job",
                "duration_seconds": 0.0,
                "steps": [],
                "evidence_roots": [
                    str(path.resolve(strict=True)) for path in (evidence_roots or [])
                ],
                "missing_evidence_patterns": [],
                "empty_evidence_patterns": [],
                "covers": [],
                "scenario_ids": [],
                "scenario_receipts": [],
                "scenario_receipt_failures": [],
                "track": "main",
                "model_id": "gpt-5.5",
                "expected_use_responses_lite": False,
                "required_model_receipt": False,
                "model_condition_receipt": None,
                "model_condition_receipt_failure": None,
                "disposition": "executed",
            }
            codex_upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
            if complete_job:
                store = codex_upgrade.incremental_recovery.CheckpointStore(
                    attempt_root / "checkpoints"
                )
                store.append(
                    {
                        "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                        "campaign_id": manifest["campaign_id"],
                        "phase": "official",
                        "attempt_id": attempt_root.name,
                        "run_nonce": reservation["run_nonce"],
                        "item_id": job.job_id,
                        "status": "complete",
                        "disposition": "executed",
                        "result_sha256": codex_upgrade.incremental_recovery.digest(result),
                        "result_key": None,
                        "result": result,
                        "source_receipt": None,
                        "previous_checkpoint_sha256": None,
                    }
                )
        return attempt_root.name

    def _historical_v7_run_summary(self) -> dict[str, object]:
        """返回 ARM64 v7 第三次归档的逐字节等价结构化摘要。"""

        scenarios = {
            "A03": (
                {"models_manifest": 1, "responses_http_success": 4},
                46425,
                "cc09c6f4db410b3efc3b72696eb9803a59c6f48f6219ce7dff8dbded208063d9",
            ),
            "A04": (
                {"models_manifest": 3, "responses_http_success": 4},
                65323,
                "0f400552e0aebbb51cd52f3922fee7604f48fe9dbedb24cc1071642922d21bdb",
            ),
            "A05": (
                {
                    "models_manifest": 1,
                    "responses_ws_handshake_success": 2,
                    "responses_ws_response_create": 2,
                },
                49394,
                "17ed44b8a02aec93799e15fcd19f27317ff0c0de365ffefc333b029cbb7c98dc",
            ),
            "A06": (
                {
                    "responses_ws_handshake_success": 1,
                    "responses_ws_response_create": 3,
                },
                11737,
                "f9dbe8eb39deebc4c1afd8a5ea8a441819a100211290bf189f16a6e8b7abef3a",
            ),
            "A07": (
                {
                    "responses_http_fallback_success": 1,
                    "responses_ws_retryable_failure": 6,
                },
                61147,
                "f8a8f59ac37201fee52e0d5cc385f1eaf66b87e084c01ae51e82bd175dd89aac",
            ),
            "A08": (
                {"models_manifest": 1, "responses_http_success": 3},
                38270,
                "2375f4a2c19a12feaf70b7393c20e0619a770bfb791ef08cf34eaf758ee43c17",
            ),
            "A10": (
                {"responses_http_success": 4},
                42180,
                "716fdeda87a975a144ac7ffeb947834702b5fbfe8c0c6010aa503dbfeb82b3ce",
            ),
            "A15": (
                {"models_manifest": 1},
                8151,
                "d84230edcbace5f9e476d6cb937120d28a44da6a2e5e7abcfa4b2a70fb537998",
            ),
        }
        return {
            "schema_version": "candidate-core-capture/v1",
            "codex_version": "0.154.0",
            "run_id": (
                "c0154-formal-vc5-recovery-20260916t122646z-"
                "c0154-candidate-v7-candidate-frozen-core"
            ),
            "status": "failed",
            "exit_code": 1,
            "synthetic_profile": "candidate-core-v1",
            "explicit_gate": True,
            "production_forwarding_enabled": False,
            "scenarios": [
                {
                    "scenario_id": scenario_id,
                    "actions": scenario_data[0],
                    "production_forwarded": False,
                    "pcap_bytes": scenario_data[1],
                    "pcap_sha256": scenario_data[2],
                }
                for scenario_id, scenario_data in scenarios.items()
            ],
            "limitations": {
                "A08": (
                    "relay 只声明真实跨调用连接；keepalive/断连重试关系由受源码哈希"
                    "约束的结构化测试补证"
                ),
                "A10_token_budget": (
                    "TokenBudget 零出站只由结构化测试证明，本脚本不伪造不存在的网络请求"
                ),
                "A15_surface": (
                    "relay 证明身份 header 的真实出站；exec/TUI 进程来源由结构化测试证明"
                ),
            },
            "restoration": {
                "account_proxy_original": "NULL|NULL",
                "account_proxy_equal": True,
                "account_extra_equal": True,
                "hosts_sha256_equal": True,
                "ca_bundle_sha256_equal": True,
            },
        }

    def _b0_historical_v7_attempt(
        self,
        fixture: dict[str, object],
        *,
        host_runs: Path,
    ) -> tuple[str, dict[str, object]]:
        """发布绑定真实 v7 producer 与完整 run-summary 的历史 Candidate attempt。"""

        campaign_dir = fixture["campaign_dir"]
        manifest = fixture["manifest"]
        candidate_id = "c0154-candidate-v7"
        identity = {"candidate_purpose": manifest["campaign_purpose"]}
        summary = self._historical_v7_run_summary()
        evidence_name = f"{summary['run_id']}.failed-attempt3"
        logical_evidence_root = f"/root/oauth-capture/runs/{evidence_name}"
        job = Job(
            job_id="candidate-frozen-core",
            phase="candidate",
            suites=("full",),
            description="历史 v7 Candidate frozen core",
            steps=({"argv": ["bash", "run_candidate_core_capture.sh"]},),
            evidence_roots=(logical_evidence_root,),
            covers=(),
            scenario_ids=("A03", "A04", "A05", "A06", "A07", "A08", "A10", "A15"),
        )
        evidence_root = host_runs / evidence_name
        evidence_root.mkdir(parents=True)
        summary_path = evidence_root / "run-summary.json"
        self._write_json(summary_path, summary)
        summary_path.chmod(0o600)
        attempt_root, _reservation = codex_upgrade._reserve_capture_attempt(
            campaign_dir,
            phase="candidate",
            candidate_id=candidate_id,
            identity=identity,
            jobs=[job],
            allow_failed_rerun=True,
        )
        result = {
            "id": job.job_id,
            "phase": "candidate",
            "required": True,
            "execution_sha256": codex_upgrade._job_execution_sha256(job),
            "status": "failed",
            "description": "历史 v7 A15 失败",
            "duration_seconds": 0.0,
            "steps": [{"step": 1, "return_code": 1}],
            "evidence_roots": list(job.evidence_roots),
            "missing_evidence_patterns": [],
            "empty_evidence_patterns": [],
            "covers": [],
            "scenario_ids": list(job.scenario_ids),
            "scenario_receipts": [],
            "scenario_receipt_failures": [],
            "track": "main",
            "model_id": "gpt-5.5",
            "expected_use_responses_lite": False,
            "required_model_receipt": False,
            "model_condition_receipt": None,
            "model_condition_receipt_failure": None,
            "tool_dependency_files": {
                "run_candidate_core_capture.sh": (
                    "b313b3fd313cd689e56d95f15e54f856"
                    "78d1b367f6e8d8bd4d8b3d4e53b1d54c"
                )
            },
            "disposition": "executed",
        }
        attempt = codex_upgrade._write_capture_attempt(
            campaign_dir,
            attempt_root,
            {
                "campaign_id": manifest["campaign_id"],
                "phase": "candidate",
                "candidate_id": candidate_id,
                "status": "failed",
                "identity": identity,
                "results": [result],
                "failure_observations": [],
                "evidence_roots": [],
                "evidence_permission_closeout": None,
                "evidence_permission_error": {
                    "type": "SyntheticFailure",
                    "message": "合成历史 attempt 不封存证据权限收据。",
                },
                "environment": {
                    "evidence_root": str(attempt_root / "evidence"),
                    "before_probe": None,
                    "after_probe": {"status": "passed"},
                    "restoration_report": {"status": "passed"},
                    "arm64_before_receipt": None,
                    "arm64_after_receipt": None,
                },
                "binary_verification": None,
                "execution_error": None,
                "restoration_error": None,
                "next_gate": "对账历史 v7 attempt。",
            },
        )
        attempt = dict(attempt)
        attempt["schema_version"] = codex_upgrade.LEGACY_CAPTURE_ATTEMPT_SCHEMA
        attempt.pop("failure_observations", None)
        attempt.pop("root_causes", None)
        attempt.pop("attempt_digest", None)
        attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
        attempt_path = attempt_root / "attempt.json"
        attempt_path.write_text(
            json.dumps(attempt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        attempt_path.chmod(0o600)
        return attempt_root.name, attempt

    def _b0_failed_attempt(
        self,
        fixture: dict[str, object],
        *,
        scenario_ids: tuple[str, ...] = ("A15",),
        failure_observations: list[dict[str, str]] | None = None,
        legacy_without_failure_arrays: bool = False,
    ) -> tuple[str, dict[str, object]]:
        """发布已恢复环境下的失败 attempt，可选降级为历史无数组形态。"""

        campaign_dir = fixture["campaign_dir"]
        manifest = fixture["manifest"]
        jobs = fixture["jobs"]
        job = jobs[0]
        attempt_root, _reservation = codex_upgrade._reserve_capture_attempt(
            campaign_dir,
            phase="official",
            candidate_id=None,
            identity=dict(manifest["official_identity"]),
            jobs=jobs,
            allow_failed_rerun=True,
        )
        result = {
            "id": job.job_id,
            "phase": "official",
            "required": True,
            "execution_sha256": codex_upgrade._job_execution_sha256(job),
            "status": "failed",
            "description": "合成失败 Job",
            "duration_seconds": 0.0,
            "steps": [],
            "evidence_roots": [],
            "missing_evidence_patterns": [],
            "empty_evidence_patterns": [],
            "covers": [],
            "scenario_ids": list(scenario_ids),
            "scenario_receipts": [],
            "scenario_receipt_failures": [
                {"scenario_id": scenario_id, "reason": "合成枚举失败"}
                for scenario_id in scenario_ids
            ],
            "track": "main",
            "model_id": "gpt-5.5",
            "expected_use_responses_lite": False,
            "required_model_receipt": False,
            "model_condition_receipt": None,
            "model_condition_receipt_failure": None,
            "disposition": "executed",
        }
        attempt = codex_upgrade._write_capture_attempt(
            campaign_dir,
            attempt_root,
            {
                "campaign_id": manifest["campaign_id"],
                "phase": "official",
                "candidate_id": None,
                "status": "failed",
                "identity": dict(manifest["official_identity"]),
                "results": [result],
                "failure_observations": list(failure_observations or []),
                "evidence_roots": [],
                "evidence_permission_closeout": None,
                "evidence_permission_error": {
                    "type": "SyntheticFailure",
                    "message": "合成失败 attempt 没有证据目录。",
                },
                "environment": {
                    "evidence_root": str(attempt_root / "evidence"),
                    "before_probe": None,
                    "after_probe": {"status": "passed"},
                    "restoration_report": {"status": "passed"},
                    "arm64_before_receipt": None,
                    "arm64_after_receipt": None,
                },
                "binary_verification": None,
                "execution_error": None,
                "restoration_error": None,
                "next_gate": "对账失败 attempt。",
            },
        )
        if legacy_without_failure_arrays:
            attempt = dict(attempt)
            attempt["schema_version"] = codex_upgrade.LEGACY_CAPTURE_ATTEMPT_SCHEMA
            attempt.pop("failure_observations", None)
            attempt.pop("root_causes", None)
            attempt.pop("attempt_digest", None)
            attempt["attempt_digest"] = codex_upgrade._fingerprint(attempt)
            (attempt_root / "attempt.json").write_text(
                json.dumps(attempt, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (attempt_root / "attempt.json").chmod(0o600)
        return attempt_root.name, attempt

    def _b0_run_dir(
        self,
        fixture: dict[str, object],
        name: str,
        *,
        state: str = "failed",
        owner_pid: int | None = None,
        phase: str = "VC-0",
        failure_class: str | None = None,
        failure_observations: list[dict[str, str]] | None = None,
        batched_manifest: dict[str, object] | None = None,
        action_id: str = "dispatch",
        failure_kind: str = "handled-error",
        error_type: str = "ConfigurationError",
        post_run_tooling: bool = False,
        started_offset_seconds: float = -30.0,
    ) -> Path:
        """一个已终止（或仍在运行）的父监督器 run 目录：state、events、minute ledger、run 清单。

        ``batched_manifest`` 给出时以它替代默认的 v2 内层清单（seal 段批次等场景）；
        ``post_run_tooling`` 为真时按父监督器的判据函数写 post-run-tooling 收据。
        """

        supervisor = codex_upgrade.codex_upgrade_supervisor
        run_dir = fixture["control"] / f"run-{name}"
        run_dir.mkdir(mode=0o700)
        if owner_pid is None:
            # 已退出并被回收的进程号：_owner_alive 对它返回 False。
            finished = subprocess.Popen(["true"])
            finished.wait()
            owner_pid = finished.pid
        started = time.time() + started_offset_seconds
        terminal = started + 1.0
        state_payload: dict[str, object] = {
            "schema_version": supervisor.STATE_SCHEMA,
            "supervisor_schema_version": supervisor.SCHEMA_VERSION,
            "campaign_id": fixture["manifest"]["campaign_id"],
            "phase": phase,
            "owner_pid": owner_pid,
            "owner_nonce": "7" * 64,
            "started_at_utc": supervisor._epoch_to_utc(started),
            "started_at_epoch": started,
            "started_monotonic_ns": 1_000_000_000,
            "deadline_at_epoch": started + 3600.0,
            "deadline_monotonic_ns": 3_601_000_000_000,
            "heartbeat_seconds": 5.0,
            "watchdog_timeout_seconds": 30.0,
            "ledger_interval_seconds": 60.0,
            "state": state,
            "terminate_owner": False,
        }
        if state != "running":
            state_payload["terminal_at_utc"] = supervisor._epoch_to_utc(terminal)
            state_payload["terminal_at_epoch"] = terminal
        self._write_json(run_dir / "state.json", state_payload)
        (run_dir / "state.json").chmod(0o600)
        supervisor._append_event(
            run_dir,
            event_type="dispatch",
            operation="dispatch",
            owner_pid=owner_pid,
            owner_nonce="7" * 64,
            campaign_id=str(fixture["manifest"]["campaign_id"]),
            phase=phase,
            status=state,
            reason="SIGKILL" if state != "running" else None,
        )
        supervisor._write_minute_record(
            run_dir,
            bucket_start=started,
            bucket_end=terminal,
            heartbeat=None,
            owner_alive=state == "running",
            heartbeat_age=None,
            classification="failed" if state != "running" else "active",
        )
        inner = batched_manifest or {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": fixture["manifest"]["campaign_id"],
            "phase": phase,
            "deadline_seconds": 3600,
            "no_op": False,
            "execute_items": ["official-test"],
            "reuse_items": [],
            "actions": [],
        }
        self._write_json(
            run_dir / "campaign-run-manifest.json",
            {
                "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                "manifest": inner,
                "manifest_sha256": supervisor._sha256(supervisor._canonical(inner)),
            },
        )
        if failure_class is not None:
            if batched_manifest is not None and state == "failed":
                # 真实父监督器在动作失败后会写 stop receipt；重派门禁靠它定位
                # 失败动作，夹具按同一函数写入。
                supervisor._stop_receipt(
                    run_dir,
                    event_type="failed",
                    reason=f"action-failed:{action_id}",
                    detected_at_epoch=terminal,
                    owner_pid=owner_pid,
                    owner_nonce="7" * 64,
                    campaign_id=str(fixture["manifest"]["campaign_id"]),
                    phase=phase,
                )
            diagnostic_path = supervisor._action_diagnostic_path(
                run_dir,
                action_id,
                create_directory=True,
            )
            diagnostic = supervisor._write_action_diagnostic(
                diagnostic_path,
                campaign_id=str(fixture["manifest"]["campaign_id"]),
                phase=phase,
                action_id=action_id,
                owner_pid=owner_pid,
                owner_nonce="7" * 64,
                failure_kind=failure_kind,
                failure_class=failure_class,
                failure_observations=failure_observations,
                error_type=error_type,
                message="机器分类测试失败。",
            )
            if post_run_tooling:
                classification = supervisor.post_run_tooling_facts(
                    fixture["campaign_dir"],
                    inner,
                    run_started_at_utc=str(state_payload["started_at_utc"]),
                )
                self.assertTrue(classification["qualifies"], classification["reasons"])
                supervisor._write_post_run_tooling_receipt(
                    run_dir,
                    campaign_id=str(fixture["manifest"]["campaign_id"]),
                    phase=phase,
                    action_id=action_id,
                    owner_pid=owner_pid,
                    owner_nonce="7" * 64,
                    diagnostic_sha256=str(diagnostic["diagnostic_sha256"]),
                    facts=classification["facts"],
                )
        for path in run_dir.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        return run_dir

    @staticmethod
    def _b0_candidate_job_ids(fixture: dict[str, object]) -> list[str]:
        """B0 夹具 Campaign 的候选 Job 闭集（合成场景只有 candidate-test）。"""

        return sorted(
            str(item["id"])
            for item in fixture["manifest"]["jobs"]
            if item.get("phase") == "candidate"
        )

    def _b0_completed_candidate_attempt(self, fixture: dict[str, object]) -> Path:
        """按正式预约与封存合同发布一个九项 Job 全部 complete、等待 seal 收据的候选 attempt。"""

        campaign_dir = fixture["campaign_dir"]
        manifest = fixture["manifest"]
        candidate_id = "cand-1"
        identity = {"candidate_purpose": manifest["campaign_purpose"]}
        # 分类阶段未封存时 _campaign_jobs 不可用；按历史 v7 夹具的方式自造 Job，
        # ID 与 0.154 场景文件的九个候选 Job 一致。
        jobs = [
            Job(
                job_id=job_id,
                phase="candidate",
                suites=("full",),
                description=f"合成候选 Job {job_id}",
                steps=({"argv": ["bash", f"{job_id}.sh"]},),
                evidence_roots=(f"/root/oauth-capture/runs/{job_id}",),
                covers=(),
                scenario_ids=("A03",),
            )
            for job_id in self._b0_candidate_job_ids(fixture)
        ]
        self.assertTrue(jobs)
        attempt_root, reservation = codex_upgrade._reserve_capture_attempt(
            campaign_dir,
            phase="candidate",
            candidate_id=candidate_id,
            identity=identity,
            jobs=jobs,
            allow_failed_rerun=True,
        )
        results: list[dict[str, object]] = []
        store = codex_upgrade.incremental_recovery.CheckpointStore(
            attempt_root / "checkpoints"
        )
        previous_checkpoint_sha256: str | None = None
        for job in jobs:
            result = {
                "id": job.job_id,
                "phase": "candidate",
                "required": True,
                "execution_sha256": codex_upgrade._job_execution_sha256(job),
                "status": "complete",
                "description": "只读承接的候选 Job",
                "duration_seconds": 0.0,
                "steps": [],
                "evidence_roots": [],
                "missing_evidence_patterns": [],
                "empty_evidence_patterns": [],
                "covers": [],
                "scenario_ids": list(job.scenario_ids),
                "scenario_receipts": [],
                "scenario_receipt_failures": [],
                "track": "main",
                "model_id": "gpt-5.5",
                "expected_use_responses_lite": False,
                "required_model_receipt": False,
                "model_condition_receipt": None,
                "model_condition_receipt_failure": None,
                "disposition": "executed",
            }
            codex_upgrade._secure_write_json_once(
                attempt_root / f"job-{job.job_id}.json", result
            )
            appended = store.append(
                {
                    "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                    "campaign_id": manifest["campaign_id"],
                    "phase": "candidate",
                    "attempt_id": attempt_root.name,
                    "run_nonce": reservation["run_nonce"],
                    "item_id": job.job_id,
                    "status": "complete",
                    "disposition": "executed",
                    "result_sha256": codex_upgrade.incremental_recovery.digest(result),
                    "result_key": None,
                    "result": result,
                    "source_receipt": None,
                    "previous_checkpoint_sha256": previous_checkpoint_sha256,
                }
            )
            previous_checkpoint_sha256 = str(appended["checkpoint_sha256"])
            results.append(result)
        # awaiting_receipts 的 v3 attempt 必须带通过的证据权限收口收据：对空的
        # evidence／logs 目录真实收口一次，绑定与正式流程完全相同。
        evidence_root = attempt_root / "evidence"
        logs_root = attempt_root / "logs"
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        logs_root.mkdir(mode=0o700, exist_ok=True)
        closeout_binding = codex_upgrade._close_attempt_evidence_permissions(
            attempt_root, [evidence_root, logs_root]
        )
        codex_upgrade._write_capture_attempt(
            campaign_dir,
            attempt_root,
            {
                "campaign_id": manifest["campaign_id"],
                "phase": "candidate",
                "candidate_id": candidate_id,
                "status": "awaiting_receipts",
                "identity": identity,
                "results": results,
                "failure_observations": [],
                "evidence_roots": [str(evidence_root), str(logs_root)],
                "evidence_permission_closeout": closeout_binding,
                "evidence_permission_error": None,
                "environment": {
                    "evidence_root": str(attempt_root / "evidence"),
                    "before_probe": None,
                    "after_probe": {"status": "passed"},
                    "restoration_report": {"status": "passed"},
                    "arm64_before_receipt": None,
                    "arm64_after_receipt": None,
                },
                "binary_verification": None,
                "execution_error": None,
                "restoration_error": None,
                "next_gate": "运行 capture manifest finalizer。",
            },
        )
        return attempt_root

    def _b0_seal_batch_manifest(
        self,
        fixture: dict[str, object],
        *,
        batch_sequence: int = 1,
        actions: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """VC-5 seal 段的 v2 内层清单：execute 只有 candidate-seal，九项 Job 全部复用。"""

        supervisor = codex_upgrade.codex_upgrade_supervisor
        plan = codex_upgrade._vc_campaign_plan(fixture["campaign_dir"], fixture["manifest"])
        campaign_dir = fixture["campaign_dir"]
        attempt_id = next(
            path.name
            for path in (campaign_dir / "candidates" / "cand-1" / "attempts").iterdir()
            if path.is_dir()
        )
        # 动作按正式 /usr/bin/env 坐标形式声明；VC-5 seal 预演门禁由
        # test_codex_upgrade_seal_rehearsal 单独覆盖，这里只测 post-run-tooling
        # 分类与对账，构造清单时旁路该门禁。
        with mock.patch.object(supervisor, "_validate_vc5_seal_rehearsal_gate"):
            return supervisor.build_batched_campaign_run_manifest(
                campaign_id=str(fixture["manifest"]["campaign_id"]),
                campaign_plan_sha256=str(plan["plan_sha256"]),
                batch_id=f"vc-5-{batch_sequence:04d}",
                batch_sequence=batch_sequence,
                batch_sha256=str(batch_sequence) * 64,
                phase="VC-5",
                predecessor_checkpoint={
                    "path": "control/vc/vc-4-checkpoint.json",
                    "sha256": "3" * 64,
                    "phase": "VC-4",
                    "checkpoint_sha256": "4" * 64,
                },
                original_deadline_at_utc="2099-09-15T08:12:43Z",
                actions=actions
                if actions is not None
                else [
                    {
                        "action_id": "prepare-candidate-assertion-bundle",
                        "operation": "VC-5:prepare-candidate-assertion-bundle",
                        "timeout_seconds": 5.0,
                        "command": [
                            "/usr/bin/env",
                            f"CAMPAIGN_DIR={campaign_dir}",
                            f"ATTEMPT_ID={attempt_id}",
                            "SIDE=candidate",
                            "CANDIDATE_ID=cand-1",
                            "/usr/bin/bash",
                            "/tmp/prepare_assertion_bundle.sh",
                        ],
                        "item_ids": ["candidate-seal"],
                    }
                ],
                execute_items=["candidate-seal"],
                reuse_items=self._b0_candidate_job_ids(fixture),
            )

    def _b0_advance_ledger_to_vc5(self, ledger_dir: Path) -> None:
        for completed, started in (
            ("VC-0", "VC-1"),
            ("VC-1", "VC-2"),
            ("VC-2", "VC-3"),
            ("VC-3", "VC-4"),
            ("VC-4", "VC-5"),
        ):
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"fixture-{completed.lower()}-completed",
                phase=completed,
                event_type="stage_completed",
                next_action=f"启动 {started}",
            )
            if started == "VC-4":
                # 改造 2：候选级阶段开工前账本必须先激活 r1。
                codex_upgrade_timing_ledger.append_event(
                    ledger_dir,
                    event_id="fixture-stage-revision-r1",
                    phase="VC-4",
                    event_type="stage_revision",
                    revision=1,
                    candidate_id="cand-1",
                    revision_commit_sha256="6" * 64,
                    next_action="派发 VC-4 首批",
                )
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"fixture-{started.lower()}-started",
                phase=started,
                event_type="stage_started",
                next_action="运行父批次",
            )

    @staticmethod
    def _b0_resume(campaign_dir: Path, preview: Path | None = None) -> dict[str, object]:
        """0.154 的 resume 由 campaign-run 派发，测试直接调用入口后的实现验证 B0 门禁。"""

        result, _return_code = codex_upgrade._resume_campaign(
            argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id=None,
                rerun_failed=True,
                recovery_preview=preview,
                preview_recovery=False,
                assertions=None,
                external_gate_root=None,
                external_gate_receipt=None,
            )
        )
        return result

    @staticmethod
    def _b0_ledger_events(ledger_dir: Path) -> list[tuple[str, str]]:
        return [
            (str(event["event_type"]), str(event["event_id"]))
            for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir)
        ]

    def test_post_run_tooling_failure_reconciles_then_allows_verbatim_redispatch(self) -> None:
        """seal 段零请求失败：父账本暂停而非停线，对账后恢复 VC-5 并只允许逐字重派。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        supervisor = codex_upgrade.codex_upgrade_supervisor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            self._b0_advance_ledger_to_vc5(ledger_dir)
            attempt_root = self._b0_completed_candidate_attempt(fixture)
            inner = self._b0_seal_batch_manifest(fixture)
            run_dir = self._b0_run_dir(
                fixture,
                "f" * 64,
                phase="VC-5",
                failure_class="execution-failure",
                batched_manifest=inner,
                action_id="prepare-candidate-assertion-bundle",
                failure_kind="child-returncode",
                error_type="ChildProcessError",
                post_run_tooling=True,
                started_offset_seconds=5.0,
            )
            # 父监督器关账：post-run-tooling 只暂停当前阶段，不写 stage_abandoned。
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                inner,
                failed_action_id="prepare-candidate-assertion-bundle",
                failure_class="post-run-tooling",
            )
            self.assertEqual(closeout["ledger_status"], "recovery_required")
            self.assertEqual(
                codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)["status"],
                "recovery_required",
            )
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])

            result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(result["live_request_count"], 0)
            self.assertIn("逐字重派同一 seal 批次", result["next_command"])
            receipt = json.loads(
                (campaign_dir / result["reconciliation_receipt"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["failure_class"], "post-run-tooling")
            diagnostic = receipt["run"]["action_diagnostic"]
            self.assertEqual(diagnostic["declared_failure_class"], "execution-failure")
            self.assertEqual(diagnostic["failure_class"], "post-run-tooling")
            self.assertTrue(diagnostic["post_run_tooling"]["recomputed"])
            self.assertEqual(diagnostic["post_run_tooling"]["attempt_id"], attempt_root.name)
            head_after = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head_after["sequence"], head_before["sequence"] + 1)
            timing_after = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(timing_after["status"], "active")
            self.assertEqual(timing_after["active_phase"], "VC-5")
            self.assertEqual(timing_after["next_action"], "redispatch-same-batch")
            events = [item[0] for item in self._b0_ledger_events(ledger_dir)]
            self.assertNotIn("stage_abandoned", events)
            self.assertNotIn("stop_the_line", events)

            # 幂等：重复对账不推进总账与账本。
            replay = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(replay["status"], "recoverable")
            self.assertTrue(replay["batch"]["reused"])
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])["sequence"],
                head_after["sequence"],
            )

            # 重派门禁：后继批次逐字相同才放行；actions 漂移失败关闭。
            prior_state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
            successor = self._b0_seal_batch_manifest(fixture, batch_sequence=2)
            self.assertTrue(
                supervisor._validate_batched_environment_redispatch_successor(
                    prior_state,
                    inner,
                    run_dir,
                    successor,
                    campaign_dir=campaign_dir,
                )
            )
            drifted_actions = json.loads(json.dumps(inner["actions"]))
            drifted_actions[0]["command"][-1] = "/tmp/other_assertion_bundle.sh"
            drifted = self._b0_seal_batch_manifest(
                fixture, batch_sequence=2, actions=drifted_actions
            )
            with self.assertRaisesRegex(supervisor.SupervisorError, "只允许原批次内容重派"):
                supervisor._validate_batched_environment_redispatch_successor(
                    prior_state,
                    inner,
                    run_dir,
                    drifted,
                    campaign_dir=campaign_dir,
                )

    def test_post_run_tooling_receipt_cannot_revive_stopped_ledger(self) -> None:
        """账本已 stop_the_line 的旧 run 即便带 post-run-tooling 收据也只能永久停线。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        supervisor = codex_upgrade.codex_upgrade_supervisor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            self._b0_advance_ledger_to_vc5(ledger_dir)
            self._b0_completed_candidate_attempt(fixture)
            inner = self._b0_seal_batch_manifest(fixture)
            run_dir = self._b0_run_dir(
                fixture,
                "e" * 64,
                phase="VC-5",
                failure_class="execution-failure",
                batched_manifest=inner,
                action_id="prepare-candidate-assertion-bundle",
                failure_kind="child-returncode",
                error_type="ChildProcessError",
                post_run_tooling=True,
                started_offset_seconds=5.0,
            )
            # 父监督器按默认分类关账（例如收据写入后父进程自身异常）：账本停线。
            closeout = supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                inner,
                failed_action_id="prepare-candidate-assertion-bundle",
                failure_class="execution-failure",
            )
            self.assertEqual(closeout["ledger_status"], "stopped")
            result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(result["status"], "permanent_stop")
            self.assertEqual(result["decision"]["terminal_reason"], "prior_stop_the_line")

    def test_recovery_required_before_reservation_reconciles_then_redispatches(self) -> None:
        """reservation 前环境门禁失败：先暂停，对账只推进一次，再恢复同阶段。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id="pre-reservation-recovery-required",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="environment-prerequisite",
                next_action="reconcile-supervisor-run",
            )
            run_dir = self._b0_run_dir(
                fixture,
                "e" * 64,
                failure_class="environment-prerequisite",
            )
            head_before = codex_upgrade_project_ledger.replay_head(
                fixture["ledger"]
            )
            self.assertEqual(
                codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)["status"],
                "recovery_required",
            )
            with self.assertRaisesRegex(
                codex_upgrade.TimingLedgerGateError,
                "recovery_required",
            ):
                codex_upgrade._timing_ledger_batch_events(
                    campaign_dir,
                    fixture["manifest"],
                    codex_upgrade._vc_campaign_plan(
                        campaign_dir,
                        fixture["manifest"],
                    ),
                    phase="VC-0",
                    ledger_dir=ledger_dir,
                )
            result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(result["live_request_count"], 0)
            self.assertEqual(
                result["root_cause"]["stable_error_code"],
                "supervisor-run.interrupted",
            )
            head_after = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head_after["sequence"], head_before["sequence"] + 1)
            timing_after = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(timing_after["status"], "active")
            self.assertEqual(timing_after["active_phase"], "VC-0")
            self.assertEqual(timing_after["next_action"], "redispatch-same-batch")
            self.assertFalse(list((campaign_dir / "official" / "attempts").glob("*")))

            replay = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(replay["status"], "recoverable")
            self.assertTrue(replay["batch"]["reused"])
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])[
                    "sequence"
                ],
                head_after["sequence"],
            )
            self.assertEqual(
                codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)[
                    "head_sha256"
                ],
                timing_after["head_sha256"],
            )

    def test_reconcile_supervisor_run_preserves_multiple_readiness_causes(self) -> None:
        """每个 check_id + failure_code 独立生成稳定根因，并在重放时只计一次。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id="multi-readiness-recovery-required",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="campaign-run.action-failed",
                next_action="reconcile-supervisor-run",
            )
            observations = [
                {
                    "check_id": "candidate-readiness.image",
                    "failure_code": "runtime-image-or-capability-mismatch",
                },
                {
                    "check_id": "candidate-readiness.storage",
                    "failure_code": "host-or-container-path-not-writable",
                },
                {
                    "check_id": "candidate-readiness.image",
                    "failure_code": "runtime-image-or-capability-mismatch",
                },
            ]
            run_dir = self._b0_run_dir(
                fixture,
                "f" * 64,
                failure_class="environment-prerequisite",
                failure_observations=observations,
            )
            result = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(len(result["failure_observations"]), 2)
            self.assertEqual(len(result["root_causes"]), 2)
            self.assertEqual(
                {item["stable_error_code"] for item in result["root_causes"]},
                {"campaign-run.action-failed"},
            )
            cause_ids = [
                item["root_cause_id"] for item in result["root_causes"]
            ]
            self.assertEqual(len(set(cause_ids)), 2)
            self.assertEqual(
                {
                    item["root_cause_id"]
                    for item in result["failure_observations"]
                },
                set(cause_ids),
            )
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(
                {cause_id: head["root_cause_counts"][cause_id] for cause_id in cause_ids},
                {cause_id: 1 for cause_id in cause_ids},
            )

            replay = reconciler.reconcile_supervisor_run(run_dir, campaign_dir)
            replay_head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertTrue(replay["batch"]["reused"])
            self.assertEqual(
                {cause_id: replay_head["root_cause_counts"][cause_id] for cause_id in cause_ids},
                {cause_id: 1 for cause_id in cause_ids},
            )
            regenerated_observations, regenerated_causes = (
                reconciler._supervisor_run_failures(
                    {
                        "phase": "VC-0",
                        "last_operation": "different-volatile-operation",
                        "failure_observations": [
                            {
                                "check_id": item["check_id"],
                                "failure_code": item["failure_code"],
                            }
                            for item in result["failure_observations"]
                        ],
                    }
                )
            )
            self.assertEqual(
                [item["root_cause_id"] for item in regenerated_observations],
                [item["root_cause_id"] for item in result["failure_observations"]],
            )
            self.assertEqual(
                [item["root_cause_id"] for item in regenerated_causes],
                cause_ids,
            )

    def test_attempt_freezes_deduplicated_failures_with_cross_campaign_ids(self) -> None:
        """attempt 按 check_id + failure_code 去重，根因身份不含 Campaign 坐标。"""

        observations = [
            {
                "check_id": "candidate-readiness.storage",
                "failure_code": "host-or-container-path-not-writable",
            },
            {
                "check_id": "candidate-readiness.storage",
                "failure_code": "host-or-container-path-not-writable",
            },
            {"check_id": "A15", "failure_code": "scenario-receipt-failed"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first_fixture = self._b0_fixture(
                root / "first", campaign_id="upgrade-0154-attempt-causes-first"
            )
            _first_id, first = self._b0_failed_attempt(
                first_fixture,
                failure_observations=observations,
            )
            second_fixture = self._b0_fixture(
                root / "second", campaign_id="upgrade-0154-attempt-causes-second"
            )
            _second_id, second = self._b0_failed_attempt(
                second_fixture,
                failure_observations=observations,
            )
            self.assertEqual(len(first["failure_observations"]), 2)
            self.assertEqual(len(first["root_causes"]), 2)
            self.assertEqual(
                [
                    (item["check_id"], item["failure_code"])
                    for item in first["failure_observations"]
                ],
                [
                    ("A15", "scenario-receipt-failed"),
                    (
                        "candidate-readiness.storage",
                        "host-or-container-path-not-writable",
                    ),
                ],
            )
            self.assertEqual(
                [item["root_cause_id"] for item in first["root_causes"]],
                [item["root_cause_id"] for item in second["root_causes"]],
            )

    def test_historical_candidate_core_summary_recovers_a15_root_cause(self) -> None:
        """v7 的完整摘要只在旧 producer 与闭合动作矩阵下还原 A15。"""

        with tempfile.TemporaryDirectory() as directory:
            host_runs = Path(directory).resolve() / "runs"
            summary = self._historical_v7_run_summary()
            evidence_name = f"{summary['run_id']}.failed-attempt3"
            evidence_root = host_runs / evidence_name
            evidence_root.mkdir(parents=True)
            summary_path = evidence_root / "run-summary.json"
            self._write_json(
                summary_path,
                summary,
            )
            summary_path.chmod(0o600)
            result = {
                "id": "candidate-frozen-core",
                "phase": "candidate",
                "status": "failed",
                "tool_dependency_files": {
                    "run_candidate_core_capture.sh": (
                        "b313b3fd313cd689e56d95f15e54f856"
                        "78d1b367f6e8d8bd4d8b3d4e53b1d54c"
                    )
                },
                "evidence_roots": [
                    f"/root/oauth-capture/runs/{evidence_name}"
                ],
                "scenario_receipt_failures": [],
                "model_condition_receipt_failure": None,
                "missing_evidence_patterns": [],
                "empty_evidence_patterns": [],
                "steps": [{"step": 1, "return_code": 1}],
            }
            with mock.patch.object(
                codex_upgrade,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                observations = codex_upgrade._job_failure_observations(result)
            self.assertEqual(
                observations,
                [
                    {
                        "check_id": "A15.models_manifest",
                        "failure_code": "action-count-below-contract",
                    }
                ],
            )

            # 相同摘要若没有登记 producer 身份，只能退回通用 step 根因。
            unknown = json.loads(json.dumps(result))
            unknown["tool_dependency_files"][
                "run_candidate_core_capture.sh"
            ] = "0" * 64
            with mock.patch.object(
                codex_upgrade,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                self.assertEqual(
                    codex_upgrade._job_failure_observations(unknown),
                    [
                        {
                            "check_id": "job.candidate-frozen-core.step-1",
                            "failure_code": "nonzero-exit",
                        }
                    ],
                )

            # 已登记 producer 还必须逐字节命中 v7 最终摘要；局部矩阵即使结构
            # 合法也不能冒充这次事故的精确诊断。
            partial = json.loads(json.dumps(summary))
            partial["scenarios"] = partial["scenarios"][:-1]
            self._write_json(summary_path, partial)
            summary_path.chmod(0o600)
            with (
                mock.patch.object(
                    codex_upgrade,
                    "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                    host_runs,
                ),
                self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "摘要与冻结事故不一致",
                ),
            ):
                codex_upgrade._job_failure_observations(result)

    def test_reconcile_stopped_attempt_preserves_a15_root_cause_once(self) -> None:
        """真实 v7 在 stopped 与后续身份漂移下仍保留 A15，且只计数一次。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            host_runs = fixture["data"] / "runs"
            host_runs.mkdir(mode=0o700)
            with mock.patch.object(
                codex_upgrade,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                attempt_id, attempt = self._b0_historical_v7_attempt(
                    fixture,
                    host_runs=host_runs,
                )
            attempt_path = (
                campaign_dir
                / "candidates"
                / "c0154-candidate-v7"
                / "attempts"
                / attempt_id
                / "attempt.json"
            )
            attempt_bytes = attempt_path.read_bytes()
            with mock.patch.object(
                codex_upgrade,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                derived_observations, derived_causes = (
                    codex_upgrade._attempt_failure_facts(attempt)
                )
            self.assertEqual(len(derived_observations), 1)
            self.assertEqual(
                derived_observations[0]["check_id"],
                "A15.models_manifest",
            )
            self.assertEqual(
                derived_observations[0]["failure_code"],
                "action-count-below-contract",
            )
            a15_cause_id = derived_causes[0]["root_cause_id"]
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"v7-attempt-started-{attempt_id}",
                phase="VC-0",
                event_type="attempt_started",
                attempt_id=attempt_id,
                next_action="capture-official",
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"v7-attempt-failed-{attempt_id}",
                phase="VC-0",
                event_type="attempt_failed",
                attempt_id=attempt_id,
                root_cause_id=a15_cause_id,
                next_action="stop-the-line",
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id="v7-stage-abandoned",
                phase="VC-0",
                event_type="stage_abandoned",
                root_cause_id=a15_cause_id,
                next_action="stop-the-line",
            )
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id="v7-stop-the-line",
                phase="VC-0",
                event_type="stop_the_line",
                root_cause_id=a15_cause_id,
                next_action="reconcile-attempt",
            )

            with (
                mock.patch.object(
                    codex_upgrade,
                    "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                    host_runs,
                ),
                mock.patch.object(
                    reconciler,
                    "_identity_facts",
                    return_value={"unchanged": False},
                ),
            ):
                result = reconciler.reconcile_attempt(campaign_dir, attempt_id)
            self.assertEqual(result["status"], "permanent_stop")
            self.assertFalse(result["identity_unchanged"])
            self.assertEqual(
                result["decision"]["terminal_reason"],
                "prior_stop_the_line",
            )
            self.assertIn(
                "当前有效 wire 身份或策略摘要已变化",
                result["decision"]["reasons"],
            )
            self.assertEqual(result["root_cause"]["root_cause_id"], a15_cause_id)
            self.assertEqual(
                result["root_cause"]["stable_error_code"],
                "campaign-run.action-failed",
            )
            self.assertEqual(
                result["failure_observations"][0]["check_id"],
                "A15.models_manifest",
            )
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])[
                    "root_cause_counts"
                ][a15_cause_id],
                1,
            )
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])[
                    "terminal_campaigns"
                ][str(fixture["manifest"]["campaign_id"])]["terminal_reason"],
                "prior_stop_the_line",
            )
            with (
                mock.patch.object(
                    codex_upgrade,
                    "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                    host_runs,
                ),
                mock.patch.object(
                    reconciler,
                    "_identity_facts",
                    return_value={"unchanged": False},
                ),
            ):
                replay = reconciler.reconcile_attempt(campaign_dir, attempt_id)
            self.assertTrue(replay["batch"]["reused"])
            self.assertEqual(attempt_path.read_bytes(), attempt_bytes)
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])[
                    "root_cause_counts"
                ][a15_cause_id],
                1,
            )

    def test_stopped_terminal_reason_precedes_later_identity_drift(self) -> None:
        """旧 Campaign 的既成停线终态不能被部署后的身份漂移覆盖。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        decision = reconciler._decide(
            head={
                "blocked": False,
                "remaining_live_requests": 100,
                "root_cause_counts": {},
                "root_causes_at_limit": [],
            },
            plan={"absolute_deadline_utc": "2026-09-18T00:00:00Z"},
            ledger={"status": "stopped"},
            identity={"unchanged": False},
            environment_status="restored",
            campaign_deadline_at_utc="2026-09-18T00:00:00Z",
            root_cause_id="rc1-a15",
            request_status="complete",
            now="2026-09-17T00:00:00Z",
        )

        self.assertEqual(decision["decision"], "permanent_stop")
        self.assertEqual(decision["terminal_reason"], "prior_stop_the_line")
        self.assertIn(
            "当前有效 wire 身份或策略摘要已变化",
            decision["reasons"],
        )

    def test_malformed_historical_v7_writes_no_reconciliation_artifact(self) -> None:
        """历史摘要校验必须先于 provenance 与账本写入，失败时保持零副作用。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            host_runs = fixture["data"] / "runs"
            host_runs.mkdir(mode=0o700)
            with mock.patch.object(
                codex_upgrade,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                attempt_id, _attempt = self._b0_historical_v7_attempt(
                    fixture,
                    host_runs=host_runs,
                )
            summary = self._historical_v7_run_summary()
            evidence_root = host_runs / f"{summary['run_id']}.failed-attempt3"
            summary["scenarios"] = summary["scenarios"][:-1]
            self._write_json(evidence_root / "run-summary.json", summary)
            (evidence_root / "run-summary.json").chmod(0o600)
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            events_before = self._b0_ledger_events(fixture["timing_ledger"])
            receipt_dir = (
                fixture["campaign_dir"]
                / "control"
                / "reconciliation"
                / f"attempt-{attempt_id}"
            )

            with (
                mock.patch.object(
                    codex_upgrade,
                    "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                    host_runs,
                ),
                self.assertRaisesRegex(
                    reconciler.ReconcilerError,
                    "历史 attempt 失败观测无法重放.*摘要与冻结事故不一致",
                ),
            ):
                reconciler.reconcile_attempt(fixture["campaign_dir"], attempt_id)

            self.assertFalse(receipt_dir.exists())
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])[
                    "head_sha256"
                ],
                head_before["head_sha256"],
            )
            self.assertEqual(
                self._b0_ledger_events(fixture["timing_ledger"]),
                events_before,
            )

    def test_reconcile_attempt_counts_each_observation_once(self) -> None:
        """同一 attempt 的多个观测分别计数，重复观测与整次对账重放都不增计数。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            observations = [
                {
                    "check_id": "candidate-readiness.storage",
                    "failure_code": "host-or-container-path-not-writable",
                },
                {
                    "check_id": "candidate-readiness.storage",
                    "failure_code": "host-or-container-path-not-writable",
                },
                {"check_id": "A15", "failure_code": "scenario-receipt-failed"},
            ]
            attempt_id, _attempt = self._b0_failed_attempt(
                fixture,
                failure_observations=observations,
            )
            result = reconciler.reconcile_attempt(
                fixture["campaign_dir"], attempt_id
            )
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(len(result["failure_observations"]), 2)
            self.assertEqual(len(result["root_causes"]), 2)
            cause_ids = [item["root_cause_id"] for item in result["root_causes"]]
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(
                {cause_id: head["root_cause_counts"][cause_id] for cause_id in cause_ids},
                {cause_id: 1 for cause_id in cause_ids},
            )
            self.assertEqual(
                {
                    (item["check_id"], item["failure_code"])
                    for item in head["failure_observations"]
                    if item["operation_id"] == f"reconcile-attempt:{attempt_id}"
                },
                {
                    ("A15", "scenario-receipt-failed"),
                    (
                        "candidate-readiness.storage",
                        "host-or-container-path-not-writable",
                    ),
                },
            )
            replay = reconciler.reconcile_attempt(
                fixture["campaign_dir"], attempt_id
            )
            self.assertTrue(replay["batch"]["reused"])
            replay_head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(
                {cause_id: replay_head["root_cause_counts"][cause_id] for cause_id in cause_ids},
                {cause_id: 1 for cause_id in cause_ids},
            )

    def test_reconcile_historical_attempt_without_failure_arrays(self) -> None:
        """历史 attempt 不回写，按既有 Job 枚举字段只读派生 A15 根因。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            attempt_id, _attempt = self._b0_failed_attempt(
                fixture,
                legacy_without_failure_arrays=True,
            )
            attempt_path = (
                fixture["campaign_dir"]
                / "official"
                / "attempts"
                / attempt_id
                / "attempt.json"
            )
            before = attempt_path.read_bytes()
            result = reconciler.reconcile_attempt(fixture["campaign_dir"], attempt_id)
            self.assertEqual(
                result["root_cause"]["stable_error_code"],
                "campaign-run.action-failed",
            )
            self.assertEqual(result["failure_observations"][0]["check_id"], "A15")
            self.assertEqual(len(result["root_causes"]), 1)
            self.assertEqual(attempt_path.read_bytes(), before)

    def test_late_reconcile_pauses_but_does_not_relabel_a15_as_deadline(self) -> None:
        """逾期对账只暂停，attempt 根因仍固定在其完成时的 A15。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            attempt_id, attempt = self._b0_failed_attempt(fixture)
            a15_cause_id = attempt["root_causes"][0]["root_cause_id"]
            deadline = codex_upgrade_timing_ledger.inspect_ledger(
                fixture["timing_ledger"]
            )["total_deadline_at_utc"]
            later = (
                datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                + timedelta(hours=1)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            result = reconciler.reconcile_attempt(
                fixture["campaign_dir"], attempt_id, now=later
            )
            self.assertEqual(result["status"], "paused")
            self.assertEqual(
                result["root_cause"]["stable_error_code"],
                "campaign-run.action-failed",
            )
            self.assertEqual(result["root_cause"]["root_cause_id"], a15_cause_id)
            self.assertIsNone(result["decision"]["terminal_reason"])
            cause_ids = [item["root_cause_id"] for item in result["root_causes"]]
            self.assertEqual(cause_ids, [a15_cause_id])
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(
                {cause_id: head["root_cause_counts"][cause_id] for cause_id in cause_ids},
                {cause_id: 1 for cause_id in cause_ids},
            )

    def test_recovery_required_after_reservation_needs_approved_resume(self) -> None:
        """reservation 后环境前提失败：对账保留暂停，批准在 resume 边界恢复 active。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id="post-reservation-recovery-required",
                phase="VC-0",
                event_type="recovery_required",
                root_cause_id="environment-prerequisite",
                next_action="reconcile-attempt",
            )
            attempt_id = self._b0_orphan_attempt(fixture)
            result = reconciler.reconcile_attempt(campaign_dir, attempt_id)
            self.assertEqual(result["status"], "recoverable")
            preview = result["recovery_preview"]
            preview_path = Path(result["recovery_preview_path"])
            self.assertEqual(
                preview["campaign_ledger_head"]["status"],
                "recovery_required",
            )
            self.assertEqual(
                preview["project_ledger_head"]["sha256"],
                result["project_head"]["head_sha256"],
            )
            self.assertEqual(
                codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)["status"],
                "recovery_required",
            )
            with self.assertRaisesRegex(reconciler.ReconcilerError, "尚未批准"):
                reconciler.load_approved_recovery_preview(
                    campaign_dir,
                    preview_path,
                    phase="official",
                    candidate_id=None,
                )
            reconciler.approve_recovery_preview(
                campaign_dir,
                attempt_id,
                approve_sha256=preview["review_sha256"],
            )
            loaded = reconciler.load_approved_recovery_preview(
                campaign_dir,
                preview_path,
                phase="official",
                candidate_id=None,
            )
            self.assertTrue(loaded["timing_recovery_event"]["appended"])
            timing_after = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(timing_after["status"], "active")
            replay = reconciler.load_approved_recovery_preview(
                campaign_dir,
                preview_path,
                phase="official",
                candidate_id=None,
            )
            self.assertFalse(replay["timing_recovery_event"]["appended"])
            self.assertEqual(
                codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)[
                    "head_sha256"
                ],
                timing_after["head_sha256"],
            )
            with codex_upgrade_project_ledger.campaign_ledger_lock(
                campaign_dir
            ) as campaign_ledger_dir:
                codex_upgrade_project_ledger.write_batch(
                    campaign_ledger_dir,
                    operation_id="concurrent-head-advance",
                    event_type="reconciliation_committed",
                    payload={
                        "campaign_id": fixture["manifest"]["campaign_id"],
                        "request": {
                            "status": "resolved",
                            "identity_keys": [],
                            "estimated_delta": 0,
                            "estimated_sources": [],
                        },
                    },
                    source={"kind": "test", "sha256": "0" * 64},
                )
            codex_upgrade_project_ledger.reconcile_project_ledger(
                fixture["ledger"],
                campaign_dir=campaign_dir,
            )
            with self.assertRaisesRegex(
                reconciler.ReconcilerError,
                "项目总账 head 已推进",
            ):
                reconciler.load_approved_recovery_preview(
                    campaign_dir,
                    preview_path,
                    phase="official",
                    candidate_id=None,
                )

    def test_b0_reconcile_attempt_orphan_is_recoverable_and_gates_resume(self) -> None:
        """孤儿 attempt：先入账后判定为可恢复，生成零请求预览；resume 只认已批准预览。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            attempt_id = self._b0_orphan_attempt(fixture)
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(codex_upgrade.campaign_status(campaign_dir)["status"], "official_capture_interrupted")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "先执行 reconcile-attempt"):
                self._b0_resume(campaign_dir)

            return_code, stdout, stderr = self._run_main(
                ["reconcile-attempt", "--campaign-dir", str(campaign_dir), "--attempt-id", attempt_id]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(result["root_cause"]["stable_error_code"], "attempt.interrupted")
            self.assertEqual(result["root_cause"]["failed_step"], "reservation")
            self.assertEqual(result["jobs"], {"complete": [], "failed": [], "indeterminate": [], "pending": ["official-test"]})
            self.assertEqual(result["live_request_count"], 0)
            self.assertEqual(result["ledger_attempt_events"], "recorded")
            receipt_path = campaign_dir / result["reconciliation_receipt"]["path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], reconciler.ATTEMPT_SCHEMA)
            self.assertFalse(receipt["attempt_receipt_exists"])
            self.assertEqual(receipt["deployment_receipt"]["sha256"], codex_upgrade.file_sha256(fixture["deployment"]))
            events = self._b0_ledger_events(fixture["timing_ledger"])
            self.assertIn(("attempt_started", f"reconcile-attempt-started-{attempt_id}"), events)
            self.assertIn(("attempt_failed", f"reconcile-attempt-failed-{attempt_id}"), events)
            # 一个 batch 一个项目事件，且已推入总账。
            self.assertFalse(result["batch"]["reused"])
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head["sequence"], head_before["sequence"] + 1)
            self.assertEqual(head["root_cause_counts"][result["root_cause"]["root_cause_id"]], 1)
            self.assertFalse(head["blocked"])
            self.assertEqual(result["project_head"]["root_cause_count"], 1)
            preview = result["recovery_preview"]
            self.assertEqual(preview["schema_version"], reconciler.RECOVERY_PREVIEW_SCHEMA)
            self.assertEqual(preview["execute_job_ids"], ["official-test"])
            self.assertEqual(preview["reuse_job_ids"], [])
            self.assertFalse(preview["source_attempt_receipt_exists"])
            self.assertEqual(preview["expected_new_requests"]["unknown_job_ids"], ["official-test"])
            self.assertEqual(preview["reservation_exists"], False)
            preview_path = Path(result["recovery_preview_path"])
            self.assertTrue(preview_path.is_file())

            # 幂等重放：不重复写收据、账本事件与 batch，总账 head 不变。
            replay = reconciler.reconcile_attempt(campaign_dir, attempt_id)
            self.assertEqual(replay["status"], "recoverable")
            self.assertTrue(replay["batch"]["reused"])
            self.assertEqual(replay["reconciliation_receipt"], result["reconciliation_receipt"])
            self.assertEqual(self._b0_ledger_events(fixture["timing_ledger"]), events)
            self.assertEqual(codex_upgrade_project_ledger.replay_head(fixture["ledger"])["sequence"], head["sequence"])
            self.assertEqual(replay["recovery_preview"]["index"], preview["index"])

            # 已对账的孤儿不再是 active 预约，而是待补跑的失败 attempt。
            self.assertEqual(codex_upgrade._active_unsealed_attempts(campaign_dir, "official"), [])
            self.assertEqual(codex_upgrade._failed_capture_attempts(campaign_dir, "official"), [f"official:{attempt_id}"])
            status = codex_upgrade.campaign_status(campaign_dir)
            self.assertEqual(status["status"], "official_capture_failed")
            self.assertIn("--recovery-preview", status["next_command"])

            # 未批准预览：resume 拒绝。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "必须提供 --recovery-preview"):
                self._b0_resume(campaign_dir)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未批准"):
                self._b0_resume(campaign_dir, preview_path)
            # 错误摘要不能批准。
            return_code, _stdout, stderr = self._run_main(
                ["reconcile-attempt", "--campaign-dir", str(campaign_dir), "--attempt-id", attempt_id, "--approve-recovery-sha256", "0" * 64]
            )
            self.assertNotEqual(return_code, 0)
            self.assertIn("批准摘要与任何恢复预览都不一致", stderr)
            return_code, stdout, stderr = self._run_main(
                ["reconcile-attempt", "--campaign-dir", str(campaign_dir), "--attempt-id", attempt_id, "--approve-recovery-sha256", preview["review_sha256"]]
            )
            self.assertEqual(return_code, 0, stderr)
            approval = json.loads(stdout)["recovery_approval"]
            self.assertEqual(approval["schema_version"], reconciler.RECOVERY_APPROVAL_SCHEMA)
            self.assertEqual(approval["execute_job_ids"], ["official-test"])
            # 批准后 resume 通过门禁，并把冻结闭集交给 run（此处用替身截住真实派发）。
            captured: dict[str, object] = {}

            def fake_run(arguments: argparse.Namespace, phase: str) -> dict[str, object]:
                captured["phase"] = phase
                captured["preview"] = getattr(arguments, "recovery_preview_payload", None)
                return {"status": "awaiting_receipts"}

            with mock.patch.object(codex_upgrade, "_run_capture_attempt", side_effect=fake_run):
                resumed = self._b0_resume(campaign_dir, preview_path)
            self.assertEqual(resumed["status"], "awaiting_receipts")
            self.assertEqual(captured["phase"], "official")
            self.assertEqual(captured["preview"]["execute_job_ids"], ["official-test"])
            self.assertEqual(captured["preview"]["approval"]["approved_sha256"], preview["review_sha256"])
            # 工具身份漂移后预览失效。
            drifted = dict(fixture["tool"], wire_producer_sha256="0" * 64)
            with mock.patch.object(codex_upgrade, "_tool_identity", return_value=drifted):
                with self.assertRaisesRegex(reconciler.ReconcilerError, "工具身份已变化"):
                    reconciler.load_approved_recovery_preview(campaign_dir, preview_path, phase="official", candidate_id=None)

    def test_b0_reconcile_attempt_same_root_cause_limit_stops_the_line(self) -> None:
        """同根因第二次失败使总账计数到达上限：stage_abandoned、stop_the_line 与 campaign_terminal。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            first = self._b0_orphan_attempt(fixture)
            first_result = reconciler.reconcile_attempt(campaign_dir, first)
            self.assertEqual(first_result["status"], "recoverable")
            root_cause_id = first_result["root_cause"]["root_cause_id"]
            second = self._b0_orphan_attempt(fixture)
            second_result = reconciler.reconcile_attempt(campaign_dir, second)
            self.assertEqual(second_result["status"], "permanent_stop")
            self.assertEqual(second_result["root_cause"]["root_cause_id"], root_cause_id)
            self.assertEqual(second_result["decision"]["terminal_reason"], "root_cause_limit")
            self.assertEqual(second_result["project_head"]["root_cause_count"], 2)
            events = self._b0_ledger_events(fixture["timing_ledger"])
            types = [item[0] for item in events]
            self.assertEqual(types[-2:], ["stage_abandoned", "stop_the_line"])
            self.assertLess(types.index("attempt_failed"), types.index("stage_abandoned"))
            self.assertEqual(codex_upgrade_timing_ledger.inspect_ledger(fixture["timing_ledger"])["status"], "stopped")
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head["terminal_campaigns"][str(fixture["manifest"]["campaign_id"])]["terminal_reason"], "root_cause_limit")
            self.assertIn(root_cause_id, head["root_causes_at_limit"])
            with self.assertRaisesRegex(codex_upgrade_project_ledger.ProjectLedgerError, "已终态"):
                codex_upgrade_project_ledger.assert_campaign_admitted(campaign_dir, command="resume", require=True)
            # 停线后再对账同一 attempt：幂等，不再追加事件。
            replay = reconciler.reconcile_attempt(campaign_dir, second)
            self.assertEqual(replay["status"], "permanent_stop")
            self.assertEqual(self._b0_ledger_events(fixture["timing_ledger"]), events)
            self.assertEqual(codex_upgrade_project_ledger.replay_head(fixture["ledger"])["sequence"], head["sequence"])
            with self.assertRaisesRegex(reconciler.ReconcilerError, "不接受恢复批准"):
                reconciler.reconcile_attempt(campaign_dir, second, approve_recovery_sha256="0" * 64)

    def test_b0_reconcile_attempt_unresolved_accounting_blocks_and_terminates(self) -> None:
        """请求数无法确定：请求部分 unresolved、总账 blocked，但 campaign_terminal 仍可写。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            # Job 证据根存在但没有任何权威来源（无 manifest／result／relay），也没有证明请求前失败的日志。
            evidence_root = campaign_dir / "official-evidence"
            evidence_root.mkdir(mode=0o700)
            self._write_json(evidence_root / "surface.json", {"records": []})
            (evidence_root / "surface.json").chmod(0o600)
            attempt_id = self._b0_orphan_attempt(
                fixture,
                evidence_roots=[evidence_root],
            )
            result = reconciler.reconcile_attempt(campaign_dir, attempt_id)
            self.assertEqual(result["status"], "permanent_stop")
            self.assertEqual(result["decision"]["terminal_reason"], "accounting_unresolved")
            self.assertEqual(result["root_cause"]["stable_error_code"], "attempt.accounting-unresolved")
            self.assertEqual(result["jobs"]["indeterminate"], ["official-test"])
            self.assertTrue(result["project_head"]["blocked"])
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertTrue(head["blocked"])
            self.assertEqual(head["unresolved_operation_ids"], [f"reconcile-attempt:{attempt_id}"])
            self.assertIn(str(fixture["manifest"]["campaign_id"]), head["terminal_campaigns"])
            with self.assertRaisesRegex(codex_upgrade_project_ledger.ProjectLedgerError, "blocked|已终态"):
                codex_upgrade_project_ledger.assert_campaign_admitted(campaign_dir, command="seal", require=True)

    def test_b0_reconcile_attempt_deadline_expired_records_failure_and_pauses(self) -> None:
        """deadline 到期仍先 metadata-only 入账，再暂停；不废弃阶段、不自动终态。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            attempt_id = self._b0_orphan_attempt(fixture)
            codex_upgrade_timing_ledger.append_event(
                ledger_dir,
                event_id=f"run-attempt-started-{attempt_id}",
                phase="VC-0",
                event_type="attempt_started",
                attempt_id=attempt_id,
                next_action="capture-official",
            )
            deadline = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)["total_deadline_at_utc"]
            later = (
                datetime.fromisoformat(deadline.replace("Z", "+00:00")) + timedelta(hours=1)
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            result = reconciler.reconcile_attempt(campaign_dir, attempt_id, now=later)
            self.assertEqual(result["status"], "paused")
            self.assertIsNone(result["decision"]["terminal_reason"])
            self.assertEqual(result["root_cause"]["stable_error_code"], "attempt.deadline-expired")
            types = [item[0] for item in self._b0_ledger_events(ledger_dir)]
            self.assertEqual(types[-2:], ["attempt_failed", "deadline_paused"])
            self.assertNotIn("stage_abandoned", types)
            self.assertNotIn("stop_the_line", types)
            self.assertFalse(codex_upgrade_project_ledger.replay_head(fixture["ledger"])["terminal_campaigns"])
            failed_event = next(
                event for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_dir)
                if event["event_id"] == f"reconcile-attempt-failed-{attempt_id}"
            )
            self.assertEqual(failed_event["receipts"], [])
            self.assertEqual(failed_event["live_request_count"], 0)
            self.assertEqual(codex_upgrade_timing_ledger.inspect_ledger(ledger_dir, now=later)["status"], "deadline_paused")

    def test_b0_reconcile_attempt_precise_requests_enter_ledger_once(self) -> None:
        """有权威来源的证据按身份键精确入账；同一证据两次对账只计一次。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler
        from tools.official_client_capture.tests.test_codex_upgrade_live_request_provenance import (
            RESPONSES,
            _mitm_http_row,
            _turn_events,
            _write_jsonl,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            capture = campaign_dir / "official-evidence"
            self._write_json(
                capture / "manifest.json",
                {
                    "schema_version": "official-client-capture/v1",
                    "case_results": [
                        {"evidence": "mitm", "subject": "codex-http", "scenario": "s4", "scenario_result": {"turn_count": 1}}
                    ],
                },
            )
            _turn_events(capture / "results" / "mitm" / "codex-http" / "s4" / "turn1-events.jsonl", 1)
            _write_jsonl(
                capture / "mitm" / "codex-http" / "s4" / "codex-http.jsonl",
                [
                    _mitm_http_row("b0-run", "codex-http", "s4", "GET", "/backend-api/models"),
                    _mitm_http_row("b0-run", "codex-http", "s4", "POST", RESPONSES, "gpt-5.5"),
                    _mitm_http_row("b0-run", "codex-http", "s4", "POST", RESPONSES + "?x=1", "gpt-5.5"),
                ],
            )
            self._make_private_tree(capture)
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            first = self._b0_orphan_attempt(
                fixture,
                complete_job=True,
                evidence_roots=[capture],
            )
            result = reconciler.reconcile_attempt(campaign_dir, first)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(result["jobs"]["complete"], ["official-test"])
            batch_dir = Path(result["batch"]["batch_dir"])
            entry = json.loads((batch_dir / "entry-01.json").read_text(encoding="utf-8"))
            request = entry["payload_fragment"]["request"]
            self.assertEqual(request["status"], "resolved")
            self.assertEqual(len(request["identity_keys"]), 2)
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head["precise_total"], head_before["precise_total"] + 2)
            # 没有 after 探针：complete Job 不复用，预览按 provenance 给出预计请求数。
            preview = result["recovery_preview"]
            self.assertEqual(preview["reuse_job_ids"], [])
            self.assertEqual(preview["expected_new_requests"]["known_by_job"], {"official-test": 2})
            second = self._b0_orphan_attempt(fixture)
            second_result = reconciler.reconcile_attempt(campaign_dir, second)
            second_entry = json.loads((Path(second_result["batch"]["batch_dir"]) / "entry-01.json").read_text(encoding="utf-8"))
            self.assertEqual(second_entry["payload_fragment"]["request"]["identity_keys"], [])
            self.assertEqual(
                codex_upgrade_project_ledger.replay_head(fixture["ledger"])["precise_total"],
                head_before["precise_total"] + 2,
            )

    def test_b0_estimated_sources_are_counted_once_in_project_ledger(self) -> None:
        """估计部分按来源去重：同一 direct 分支多次进入 batch 只累加一次上界。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            for operation in ("op-a", "op-b"):
                with codex_upgrade_project_ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
                    codex_upgrade_project_ledger.write_batch(
                        ledger_dir,
                        operation_id=operation,
                        event_type="reconciliation_committed",
                        payload={
                            "campaign_id": fixture["manifest"]["campaign_id"],
                            "request": {
                                "status": "estimated",
                                "identity_keys": [],
                                "estimated_delta": 7,
                                "estimated_sources": [{"source_id": "c:run-direct", "job_id": "official-test", "estimated_count": 7}],
                            },
                            "root_cause": {"root_cause_id": "rc1-" + "a" * 20},
                        },
                        source={"kind": "test", "sha256": "0" * 64},
                    )
                codex_upgrade_project_ledger.reconcile_project_ledger(fixture["ledger"], campaign_dir=campaign_dir)
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head["estimated_total"], head_before["estimated_total"] + 7)
            self.assertEqual(head["accounted_estimated_sources"], ["c:run-direct"])
            self.assertEqual(head["root_cause_counts"]["rc1-" + "a" * 20], 2)

    def test_b0_uncommitted_batch_is_not_pushed_and_blocks_new_batches(self) -> None:
        """batch 写了 entry 未 COMMIT：补齐器不推送，对账也不得在其后再写新 batch。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            attempt_id = self._b0_orphan_attempt(fixture)
            outbox = campaign_dir / "ledger" / "outbox"
            existing = sorted(outbox.iterdir())
            dangling = outbox / f"batch-{len(existing) + 1:06d}"
            dangling.mkdir(mode=0o700)
            entry = {
                "schema_version": codex_upgrade_project_ledger.ENTRY_SCHEMA,
                "batch_sequence": len(existing) + 1,
                "entry_sequence": 1,
                "operation_id": "dangling-op",
                "event_type": "reconciliation_committed",
                "payload_fragment": {"campaign_id": fixture["manifest"]["campaign_id"]},
                "source": {"kind": "test", "sha256": "0" * 64},
                "receipt_bindings": [],
                "previous_entry_sha256": None,
            }
            entry["entry_sha256"] = codex_upgrade_project_ledger._digest(entry)
            self._write_json(dangling / "entry-01.json", entry)
            (dangling / "entry-01.json").chmod(0o600)
            report = codex_upgrade_project_ledger.reconcile_project_ledger(fixture["ledger"], campaign_dir=campaign_dir)
            self.assertTrue(any(item.get("status") == "uncommitted" for item in report["results"]))
            with self.assertRaisesRegex(reconciler.ReconcilerError, "未 COMMIT"):
                reconciler.reconcile_attempt(campaign_dir, attempt_id)

    def test_b0_reconcile_supervisor_run_recoverable_then_limit_stops(self) -> None:
        """父监督器 run 对账：可恢复时账本 receipt_passed 且 phase 保持 active；同根因第二次停线。"""

        from tools.official_client_capture import codex_upgrade_reconciler as reconciler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._b0_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            running = self._b0_run_dir(fixture, "a" * 64, state="running", owner_pid=os.getpid())
            with self.assertRaisesRegex(reconciler.ReconcilerError, "仍在运行"):
                reconciler.reconcile_supervisor_run(running, campaign_dir)
            first = self._b0_run_dir(fixture, "b" * 64)
            return_code, stdout, stderr = self._run_main(
                ["reconcile-supervisor-run", "--run-dir", str(first), "--campaign-dir", str(campaign_dir)]
            )
            self.assertEqual(return_code, 0, stderr)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "recoverable")
            self.assertEqual(result["root_cause"]["stable_error_code"], "supervisor-run.interrupted")
            self.assertEqual(result["root_cause"]["failed_step"], "dispatch")
            self.assertEqual(result["live_request_count"], 0)
            receipt = json.loads((campaign_dir / result["reconciliation_receipt"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema_version"], reconciler.SUPERVISOR_RUN_SCHEMA)
            self.assertFalse(receipt["attempt_events_fabricated"])
            self.assertEqual(receipt["run"]["state"], "failed")
            self.assertFalse(receipt["run"]["owner_alive"])
            summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
            self.assertEqual(summary["status"], "active")
            self.assertEqual(summary["active_phase"], "VC-0")
            events = self._b0_ledger_events(ledger_dir)
            self.assertIn(("receipt_passed", f"reconcile-run-passed-{first.name}"), events)
            self.assertNotIn("attempt_failed", [item[0] for item in events])
            head = codex_upgrade_project_ledger.replay_head(fixture["ledger"])
            self.assertEqual(head["root_cause_counts"][result["root_cause"]["root_cause_id"]], 1)
            # run 期间产生过 reservation → 必须改用 reconcile-attempt。
            orphan = self._b0_orphan_attempt(fixture)
            later = self._b0_run_dir(fixture, "c" * 64)
            with self.assertRaisesRegex(reconciler.ReconcilerError, "改用 reconcile-attempt"):
                reconciler.reconcile_supervisor_run(later, campaign_dir)
            self.assertTrue(orphan)
            # 同根因第二个 run（reservation 早于该 run 启动）：计数到 2，永久停线。
            reconciler.reconcile_attempt(campaign_dir, orphan)
            time.sleep(0.05)
            second = self._b0_run_dir(fixture, "d" * 64)
            state = json.loads((second / "state.json").read_text(encoding="utf-8"))
            state["started_at_epoch"] = time.time() + 5.0
            state["started_at_utc"] = codex_upgrade.codex_upgrade_supervisor._epoch_to_utc(state["started_at_epoch"])
            state["terminal_at_epoch"] = state["started_at_epoch"] + 1.0
            state["terminal_at_utc"] = codex_upgrade.codex_upgrade_supervisor._epoch_to_utc(state["terminal_at_epoch"])
            (second / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (second / "minute-ledger.ndjson").unlink()
            codex_upgrade.codex_upgrade_supervisor._write_minute_record(
                second,
                bucket_start=state["started_at_epoch"],
                bucket_end=state["terminal_at_epoch"],
                heartbeat=None,
                owner_alive=False,
                heartbeat_age=None,
                classification="failed",
            )
            second_result = reconciler.reconcile_supervisor_run(second, campaign_dir)
            self.assertEqual(second_result["status"], "permanent_stop")
            self.assertEqual(second_result["decision"]["terminal_reason"], "root_cause_limit")
            self.assertEqual(codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)["status"], "stopped")
            terminal = codex_upgrade_project_ledger.replay_head(fixture["ledger"])["terminal_campaigns"]
            self.assertEqual(terminal[str(fixture["manifest"]["campaign_id"])]["terminal_reason"], "root_cause_limit")

    def test_formal_campaign_run_enforcement_covers_future_target_versions(self) -> None:
        """campaign-run 强制派发与旧写入拒绝按历史豁免集合判定，不再逐版本硬编码。"""

        context_env = codex_upgrade.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = iter(range(1, 100))

            def campaign(target_version: str) -> argparse.Namespace:
                campaign_dir = root / f"campaign-{next(counter)}"
                campaign_dir.mkdir()
                (campaign_dir / "campaign.json").write_text(
                    json.dumps(
                        {"campaign_mode": "formal", "target_version": target_version}
                    ),
                    encoding="utf-8",
                )
                return argparse.Namespace(
                    campaign_dir=campaign_dir,
                    command="capture-candidate",
                    candidate_id="c1",
                )

            environ = dict(codex_upgrade.os.environ)
            environ.pop(context_env, None)
            with mock.patch.dict(codex_upgrade.os.environ, environ, clear=True):
                for version in ("0.151.0", "0.153.0", "1.2.3"):
                    with self.subTest(version=version):
                        with self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError, "必须由 campaign-run 派发"
                        ):
                            codex_upgrade._reject_unparented_formal_write(
                                campaign(version), "capture-candidate"
                            )
                        with self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError, "旧写入入口"
                        ):
                            codex_upgrade._reject_campaign_run_legacy_write(
                                campaign(version), "successor"
                            )
                for version in ("0.147.0", "0.149.1"):
                    codex_upgrade._reject_unparented_formal_write(
                        campaign(version), "capture-candidate"
                    )
                codex_upgrade._reject_campaign_run_legacy_write(
                    campaign("0.147.0"), "successor"
                )
            with mock.patch.dict(codex_upgrade.os.environ, {context_env: "1"}):
                codex_upgrade._reject_unparented_formal_write(
                    campaign("0.155.0"), "capture-candidate"
                )

    # ------------------------------------------------------------------
    # VC-2～VC-6 派发链：零请求合成动作走真实 compile-and-run-vc-batch，
    # 覆盖项目总账 admission、时间账本阶段推进与 checkpoint 链。
    # ------------------------------------------------------------------

    def _vc_chain_fixture(self, root: Path) -> dict[str, object]:
        """只读导入形态的 0.154 Formal Campaign：no-op 首批、VC-1 已封存、账本停在 active VC-0。

        这正是 reuse-official-evidence 建出的恢复 Campaign 在 VC-2 开工前的真实状态。
        """

        # 本 helper 只用于本文件的合成子命令；独立 real_chains 使用受限 staging 夹具总账。
        self.enterContext(runtime_egress_fixtures.offline_campaign_egress())
        original = codex_upgrade._create_initial_vc_control_artifacts

        def as_reuse(*args: object, **kwargs: object) -> dict[str, object]:
            kwargs["reuse_official_jobs"] = True
            return original(*args, **kwargs)

        with mock.patch.object(codex_upgrade, "_create_initial_vc_control_artifacts", side_effect=as_reuse):
            fixture = self._b0_fixture(root, campaign_id="upgrade-0154-vc-chain")
        campaign_dir = fixture["campaign_dir"]
        # 只读导入 Campaign 以 predecessor.reason 标识，加载器据此重算 no-op 首批。
        manifest_path = campaign_dir / "campaign.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["predecessor"] = {
            "campaign_dir": str(root / "predecessor-fixture"),
            "campaign_id": "upgrade-0154-vc-chain-predecessor",
            "campaign_manifest_sha256": "0" * 64,
            "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (campaign_dir / "campaign.sha256").write_text(codex_upgrade.file_sha256(manifest_path) + "\n", encoding="utf-8")
        fixture["manifest"] = manifest
        stage_receipt = campaign_dir / "control" / "vc-chain" / "vc-1-stage-result.json"
        stage_receipt.parent.mkdir(mode=0o700)
        self._write_json(stage_receipt, {"phase": "VC-1", "status": "complete"})
        stage_receipt.chmod(0o600)
        codex_upgrade._complete_vc_phase(
            campaign_dir,
            manifest,
            phase="VC-1",
            stage_receipt_path=stage_receipt.resolve(strict=True),
        )
        state_dir = root / "supervisor"
        state_dir.mkdir(mode=0o700)
        return {**fixture, "state_dir": state_dir}

    @staticmethod
    def _vc_chain_action_plan(root: Path, campaign_dir: Path, phase: str, *, fail: bool = False, stage_receipt_source: Path | None = None) -> Path:
        """合成动作：子进程用当前工具封存本阶段 checkpoint；``fail`` 时以非零退出。

        ``stage_receipt_source`` 给出时，本阶段收据取该文件的逐字节副本（真实评估链让 VC-3 阶段收据
        等于候选树内 Catalog stage 收据，供 record-candidate-build 的 revision-seal 字节比对）。
        """

        repo_root = Path(codex_upgrade.__file__).resolve().parents[2]
        if fail:
            script = "import sys; sys.exit(3)"
        else:
            script = (
                "import json, sys, traceback\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[3])\n"
                "campaign_dir = Path(sys.argv[1]); phase = sys.argv[2]\n"
                "source = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None\n"
                "try:\n"
                "    from unittest import mock\n"
                "    from tools.official_client_capture import codex_upgrade\n"
                "    from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal\n"
                "    receipt = campaign_dir / 'control' / 'vc-chain' / f'{phase.lower()}-stage-result.json'\n"
                "    if source is not None:\n"
                "        receipt.write_bytes(source.read_bytes())\n"
                "    else:\n"
                "        receipt.write_text(json.dumps({'phase': phase, 'status': 'complete'}) + '\\n', encoding='utf-8')\n"
                "    receipt.chmod(0o600)\n"
                "    # 与父测试 setUp 一致：0.154 合成 Campaign 不走正式证据标签声明。\n"
                "    with mock.patch.object(rehearsal, '_target_evidence_label_declaration_sha256', return_value='d' * 64):\n"
                "        manifest = codex_upgrade._require_formal_campaign(campaign_dir)\n"
                "        codex_upgrade._complete_vc_phase(campaign_dir, manifest, phase=phase, stage_receipt_path=receipt.resolve())\n"
                "except BaseException:\n"
                "    (campaign_dir / 'control' / 'vc-chain' / f'{phase.lower()}-error.txt').write_text(traceback.format_exc(), encoding='utf-8')\n"
                "    raise\n"
            )
        item_id = f"{phase.lower()}-synthetic"
        plan = {
            "schema_version": codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": [item_id],
            "reuse_item_ids": [],
            "actions": [
                {
                    "action_id": item_id,
                    "operation": f"{phase}:synthetic-checkpoint",
                    "timeout_seconds": 120,
                    "command": [
                        sys.executable, "-c", script, str(campaign_dir), phase, str(repo_root),
                        *([str(stage_receipt_source)] if stage_receipt_source is not None else []),
                    ],
                    "item_ids": [item_id],
                }
            ],
        }
        path = root / "action-plans" / f"{phase.lower()}{'-fail' if fail else ''}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path.resolve(strict=True)

    @staticmethod
    def _vc_chain_arguments(fixture: dict[str, object], phase: str, sequence: int, action_plan: Path) -> argparse.Namespace:
        order = codex_upgrade_vc_artifacts.VC_PHASES
        predecessor = order[order.index(phase) - 1]
        campaign_dir = fixture["campaign_dir"]
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            state_dir=fixture["state_dir"],
            phase=phase,
            sequence=sequence,
            predecessor_checkpoint=campaign_dir / "control" / "vc" / f"{predecessor.lower()}-checkpoint.json",
            action_plan=action_plan,
            heartbeat_seconds=0.2,
            watchdog_timeout_seconds=5.0,
            ledger_interval_seconds=0.2,
        )

    def test_vc_chain_batches_advance_ledger_and_checkpoints_through_vc6(self) -> None:
        """VC-2～VC-6 每批经原子入口派发：admission、账本阶段事件与 checkpoint 链全部闭合。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._vc_chain_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            head_before = codex_upgrade_project_ledger.replay_head(fixture["ledger"])["sequence"]
            order = codex_upgrade_vc_artifacts.VC_PHASES
            for sequence, phase in enumerate(order[2:], start=2):
                if phase == "VC-4":
                    # 改造 2：候选级首批派发前必须先激活 r1（零请求、幂等）。
                    opened = codex_upgrade.open_candidate_revision(
                        argparse.Namespace(campaign_dir=campaign_dir, candidate_id="candidate-r1", initial=True, supersedes=None)
                    )
                    self.assertEqual((opened["revision"], opened["idempotent"]), (1, False))
                    self.assertTrue((campaign_dir / "control" / "vc" / "revisions" / "r1" / "COMMIT").is_file())
                action_plan = self._vc_chain_action_plan(root, campaign_dir, phase)
                result, returncode = codex_upgrade.compile_and_run_vc_batch(
                    self._vc_chain_arguments(fixture, phase, sequence, action_plan)
                )
                error_note = campaign_dir / "control" / "vc-chain" / f"{phase.lower()}-error.txt"
                self.assertEqual(
                    returncode, 0, error_note.read_text(encoding="utf-8") if error_note.exists() else result
                )
                self.assertEqual(result["status"], "stopped")
                self.assertEqual(result["campaign_run"]["reason"], "queue-complete")
                self.assertEqual(result["project_ledger"]["campaign_id"], fixture["manifest"]["campaign_id"])
                if phase == "VC-2":
                    # 只读导入 Campaign 的 no-op 首批由原子入口在同一锁内代跑成父 run 历史。
                    self.assertEqual(result["bootstrap_noop_run"]["reason"], "incremental-noop")
                    self.assertEqual(result["bootstrap_noop_run"]["batch_sequence"], 1)
                else:
                    self.assertIsNone(result["bootstrap_noop_run"])
                checkpoint = campaign_dir / "control" / "vc" / f"{phase.lower()}-checkpoint.json"
                self.assertTrue(checkpoint.is_file(), phase)
                completion = result["timing_ledger"]["completion"]
                self.assertFalse(completion["idempotent"], phase)
                state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
                self.assertEqual(state["status"], "active")
                self.assertIsNone(state["active_phase"])
                self.assertEqual(state["completed_phases"], list(order[: order.index(phase) + 1]))
            events = self._b0_ledger_events(ledger_dir)
            # 恢复账本停在 active VC-0：VC-2 首批一次补齐 VC-0／VC-1 完成再开 VC-2；
            # 之后每批各写一次 started／completed。
            self.assertEqual(
                events[1:10],
                [
                    ("stage_completed", "vc-batch-0002-vc-2-vc-0-completed"),
                    ("stage_started", "vc-batch-0002-vc-2-vc-1-started"),
                    ("stage_completed", "vc-batch-0002-vc-2-vc-1-completed"),
                    ("stage_started", "vc-batch-0002-vc-2-vc-2-started"),
                    ("stage_completed", "vc-batch-0002-vc-2-completed"),
                    ("stage_started", "vc-batch-0003-vc-3-vc-3-started"),
                    ("stage_completed", "vc-batch-0003-vc-3-completed"),
                    ("stage_revision", "stage-revision-r1"),
                    ("stage_started", "vc-batch-0004-vc-4-vc-4-started"),
                ],
            )
            self.assertEqual(events[-1], ("stage_completed", "vc-batch-0006-vc-6-completed"))
            # 派发本身不写项目总账事件；admission 只读重放 head。
            self.assertEqual(codex_upgrade_project_ledger.replay_head(fixture["ledger"])["sequence"], head_before)
            # 已封存阶段不得重编：改造 2 起账本门在编译前就拒绝重开当前 revision 已完成的阶段
            # （比原先 compile 期的"已有 checkpoint"更早，不写任何制品）。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已登记 VC-6 在当前 revision 完成，禁止重开"):
                codex_upgrade.compile_and_run_vc_batch(
                    self._vc_chain_arguments(fixture, "VC-6", 7, self._vc_chain_action_plan(root / "again", campaign_dir, "VC-6"))
                )
            # 5 个阶段批次 + 1 个 no-op 引导首批
            self.assertEqual(len(list(fixture["state_dir"].glob("run-*/state.json"))), 6)

    def test_vc_chain_rejects_stopped_ledger_before_any_artifact_is_written(self) -> None:
        """账本已停线时原子入口在编译前拒绝：不写 batch、manifest、run 或停线收据。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._vc_chain_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            codex_upgrade_timing_ledger.append_event(
                fixture["timing_ledger"], event_id="manual-stop", phase="VC-0", event_type="stop_the_line", next_action="停线"
            )
            action_plan = self._vc_chain_action_plan(root, campaign_dir, "VC-2")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "当前状态为 stopped"):
                codex_upgrade.compile_and_run_vc_batch(self._vc_chain_arguments(fixture, "VC-2", 2, action_plan))
            self.assertEqual(sorted(path.name for path in (campaign_dir / "control" / "vc" / "batches").iterdir()), ["0001-vc-1.json"])
            self.assertFalse((campaign_dir / "control" / "vc" / "predispatch-stops").exists())
            self.assertEqual(list(fixture["state_dir"].glob("run-*")), [])

    def test_vc_chain_failed_batch_abandons_stage_and_blocks_next_batch(self) -> None:
        """动作失败：父 run 把账本推成 stage_abandoned＋stage_review_required，后续批次被拒。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._vc_chain_fixture(root)
            campaign_dir = fixture["campaign_dir"]
            ledger_dir = fixture["timing_ledger"]
            result, returncode = codex_upgrade.compile_and_run_vc_batch(
                self._vc_chain_arguments(fixture, "VC-2", 2, self._vc_chain_action_plan(root, campaign_dir, "VC-2", fail=True))
            )
            self.assertEqual(returncode, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["campaign_run"]["timing_closeout"]["status"], "passed", result["campaign_run"]["timing_closeout"])
            self.assertIsNone(result["timing_ledger"]["completion"])
            state = codex_upgrade_timing_ledger.phase_ledger_state(ledger_dir)
            self.assertEqual(state["status"], "stage_review_required")
            self.assertIsNone(state["active_phase"])
            self.assertFalse((campaign_dir / "control" / "vc" / "vc-2-checkpoint.json").exists())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "当前状态为 stage_review_required"):
                codex_upgrade.compile_and_run_vc_batch(
                    self._vc_chain_arguments(fixture, "VC-2", 3, self._vc_chain_action_plan(root / "retry", campaign_dir, "VC-2"))
                )

class EvidenceManifestTest(unittest.TestCase):
    """单次内容扫描、断点续作和零扫描复核必须可机器证明。"""

    @staticmethod
    def _private_file(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_bytes(payload)
        path.chmod(0o600)

    def test_preflight_failure_reads_zero_content_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            root.mkdir(mode=0o700)
            evidence = root / "open.json"
            evidence.write_text("{}", encoding="utf-8")
            evidence.chmod(0o644)
            with mock.patch.object(
                codex_upgrade_evidence_manifest,
                "_hash_and_scan",
            ) as scanner:
                with self.assertRaisesRegex(
                    codex_upgrade_evidence_manifest.EvidenceManifestError,
                    "group/other",
                ):
                    codex_upgrade_evidence_manifest.build_evidence_manifest(
                        [root],
                        checkpoint_path=Path(directory) / "checkpoint.json",
                    )
            scanner.assert_not_called()

    def test_merge_reuses_source_bytes_and_scans_only_delta(self) -> None:
        """metadata-only 合并不得重新读取来源证据正文。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source" / "evidence"
            delta = base / "current" / "evidence"
            self._private_file(source / "source.json", b'{"source":true}\n')
            self._private_file(delta / "kilo.json", b'{"kilo":true}\n')
            source_manifest = (
                codex_upgrade_evidence_manifest.build_evidence_manifest(
                    [source],
                    checkpoint_path=base / "source-checkpoint.json",
                )
            )
            delta_manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                [delta],
                checkpoint_path=base / "delta-checkpoint.json",
            )
            with mock.patch.object(
                codex_upgrade_evidence_manifest,
                "_hash_and_scan",
            ) as scanner:
                merged = codex_upgrade_evidence_manifest.merge_evidence_manifests(
                    source_manifest,
                    delta_manifest,
                )
                boundary = codex_upgrade_evidence_manifest.verify_manifest_boundary(
                    merged,
                    [source, delta],
                )
            scanner.assert_not_called()
            self.assertEqual(boundary["scanned_bytes"], 0)
            self.assertEqual(
                merged["scan"]["scanned_bytes"],
                delta_manifest["total_bytes"],
            )
            self.assertEqual(
                merged["scan"]["reused_bytes"],
                source_manifest["total_bytes"],
            )
            self.assertEqual(merged["entry_count"], 2)

    def _projection_fixture(self, base: Path) -> tuple[Path, Path, Path, dict, dict]:
        """两根来源清单（job-a／job-b）与一根增量清单（recovery 段）。"""

        job_a = base / "attempt" / "job-a"
        job_b = base / "attempt" / "job-b"
        delta_root = base / "attempt" / "recovery-ar1" / "job-b"
        self._private_file(job_a / "a.json", b'{"job":"a"}\n')
        self._private_file(job_a / "nested" / "a2.json", b'{"job":"a2"}\n')
        self._private_file(job_b / "b.json", b'{"job":"b","old":true}\n')
        self._private_file(delta_root / "b.json", b'{"job":"b","recovered":true}\n')
        source = codex_upgrade_evidence_manifest.build_evidence_manifest(
            [job_a, job_b], checkpoint_path=base / "source-checkpoint.json"
        )
        delta = codex_upgrade_evidence_manifest.build_evidence_manifest(
            [delta_root], checkpoint_path=base / "delta-checkpoint.json"
        )
        return job_a, job_b, delta_root, source, delta

    def test_projection_keeps_whole_roots_only_and_records_dropped_roots(self) -> None:
        """改造 5 M2：投影只按整根保留，前缀与条目逐字沿用，零扫描；收据精确记录丢弃根。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            job_a, job_b, _delta_root, source, _delta = self._projection_fixture(base)
            with mock.patch.object(codex_upgrade_evidence_manifest, "_hash_and_scan") as scanner:
                projected, receipt = codex_upgrade_evidence_manifest.project_evidence_manifest(
                    source, keep_roots=[job_a]
                )
            scanner.assert_not_called()
            source_row = next(row for row in source["roots"] if row["path"] == str(job_a))
            self.assertEqual(projected["roots"], [source_row])  # 保留根行（含 stat 边界与 prefix）逐字沿用
            self.assertEqual(
                [entry["path"] for entry in projected["entries"]],
                [entry["path"] for entry in source["entries"] if entry["path"].startswith(f"{source_row['prefix']}/")],
            )
            self.assertEqual(projected["entry_count"], 2)
            self.assertEqual(projected["scan"], {"full_scan_count": 1, "scanned_bytes": 0, "reused_bytes": projected["total_bytes"], "total_bytes": projected["total_bytes"], "elapsed_seconds": 0.0})
            self.assertEqual(projected["security"]["file_count"], 2)
            self.assertEqual(receipt["schema_version"], codex_upgrade_evidence_manifest.PROJECTION_SCHEMA)
            self.assertEqual((receipt["kept_roots"], receipt["dropped_roots"]), ([str(job_a)], [str(job_b)]))
            self.assertEqual((receipt["kept_entry_count"], receipt["dropped_entry_count"]), (2, 1))
            self.assertEqual((receipt["source_manifest_digest"], receipt["projected_manifest_digest"]), (source["manifest_digest"], projected["manifest_digest"]))
            # 收据重放：丢弃根必须与恢复基线冻结的集合精确相等。
            codex_upgrade_evidence_manifest.validate_projection_receipt(
                receipt, source_manifest=source, projected_manifest=projected, expected_dropped_roots=[job_b]
            )
            with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "不精确相等"):
                codex_upgrade_evidence_manifest.validate_projection_receipt(
                    receipt, source_manifest=source, projected_manifest=projected, expected_dropped_roots=[job_b, job_a]
                )
            # 条目级裁剪（保留根给成子目录）与不存在的根都拒绝；空保留集合合法（全部 Job 重采）。
            with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "不在来源"):
                codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[job_a / "nested"])
            with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "不在来源"):
                codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[base / "missing"])
            empty, empty_receipt = codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[])
            self.assertEqual((empty["roots"], empty["entry_count"], empty["total_bytes"]), ([], 0, 0))
            self.assertEqual(empty_receipt["dropped_roots"], sorted([str(job_a), str(job_b)]))

    def test_merge_preserve_prefixes_keeps_projected_prefixes_and_rejects_conflicts(self) -> None:
        """改造 5 M2：preserve_prefixes 合并沿用投影前缀、只扫描增量、根集合恰为并集；前缀冲突拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            job_a, job_b, delta_root, source, delta = self._projection_fixture(base)
            projected, _receipt = codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[job_a])
            with mock.patch.object(codex_upgrade_evidence_manifest, "_hash_and_scan") as scanner:
                merged = codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, delta, preserve_prefixes=True)
                boundary = codex_upgrade_evidence_manifest.verify_manifest_boundary(merged, [job_a, delta_root])
            scanner.assert_not_called()
            self.assertEqual(boundary["scanned_bytes"], 0)
            self.assertEqual({row["path"]: row["prefix"] for row in merged["roots"]}, {str(job_a): "job-a", str(delta_root): "job-b"})
            self.assertEqual(sorted(entry["path"] for entry in merged["entries"]), sorted([*(e["path"] for e in projected["entries"]), *(e["path"] for e in delta["entries"])]))
            self.assertEqual((merged["scan"]["scanned_bytes"], merged["scan"]["reused_bytes"]), (delta["total_bytes"], projected["total_bytes"]))
            self.assertEqual(merged["total_bytes"], projected["total_bytes"] + delta["total_bytes"])
            # 增量根与保留根同名（前缀冲突）：preserve 模式拒绝；默认模式仍按既有规则重算为 001-／002-。
            conflict_root = base / "other" / "job-a"
            self._private_file(conflict_root / "c.json", b'{"c":true}\n')
            conflict = codex_upgrade_evidence_manifest.build_evidence_manifest([conflict_root], checkpoint_path=base / "conflict-checkpoint.json")
            with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "前缀冲突"):
                codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, conflict, preserve_prefixes=True)
            renumbered = codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, conflict)
            self.assertEqual(sorted(row["prefix"] for row in renumbered["roots"]), ["001-job-a", "002-job-a"])

    def test_segment_seal_s1_delta_scan_interruption_resumes_without_rereading_and_reprojects_identically(self) -> None:
        """崩溃矩阵 S1（段 seal 第 3 阶段）：投影已写、delta 扫描中途中断 → 续作时已完成条目不重读（逐文件
        checkpoint），投影纯函数重算逐字相同（投影收据 write-or-verify 通过），合并结果与一次成功扫描相同。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            job_a, job_b, delta_root, source, _delta = self._projection_fixture(base)
            self._private_file(delta_root / "nested" / "b2.json", b'{"job":"b2","recovered":true}\n')
            self._private_file(delta_root / "nested" / "b3.json", b'{"job":"b3","recovered":true}\n')
            projected, receipt = codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[job_a])
            checkpoint = base / "segment-delta-checkpoint.json"
            original_scan = codex_upgrade_evidence_manifest._hash_and_scan
            scanned: list[str] = []

            def interrupted_scan(path, logical_path, known_secrets):
                if len(scanned) == 2:
                    raise OSError("simulated delta scan interruption")
                scanned.append(logical_path)
                return original_scan(path, logical_path, known_secrets)

            with mock.patch.object(codex_upgrade_evidence_manifest, "_hash_and_scan", side_effect=interrupted_scan):
                with self.assertRaisesRegex(OSError, "simulated delta scan interruption"):
                    codex_upgrade_evidence_manifest.build_evidence_manifest([delta_root], checkpoint_path=checkpoint)
            self.assertEqual(len(scanned), 2)
            self.assertTrue(checkpoint.is_file())
            completed_before = sorted(item["path"] for item in json.loads(checkpoint.read_text(encoding="utf-8"))["completed"])
            self.assertEqual(completed_before, sorted(scanned))
            # 续作：只扫描剩余条目，已完成条目不重读。
            resumed: list[str] = []

            def resumed_scan(path, logical_path, known_secrets):
                resumed.append(logical_path)
                return original_scan(path, logical_path, known_secrets)

            with mock.patch.object(codex_upgrade_evidence_manifest, "_hash_and_scan", side_effect=resumed_scan):
                delta = codex_upgrade_evidence_manifest.build_evidence_manifest([delta_root], checkpoint_path=checkpoint)
            self.assertEqual(set(resumed) & set(completed_before), set())
            self.assertEqual(sorted(resumed + completed_before), sorted(entry["path"] for entry in delta["entries"]))
            self.assertEqual(delta["entry_count"], 3)
            # 投影纯函数：中断后重算逐字相同 → 投影收据 write-or-verify 不会冲突。
            reprojected, receipt_again = codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[job_a])
            self.assertEqual((reprojected, receipt_again), (projected, receipt))
            # 合并结果与一次成功扫描相同（不含 elapsed／完成时间等易变字段的条目与摘要）。
            clean = codex_upgrade_evidence_manifest.build_evidence_manifest([delta_root], checkpoint_path=base / "clean-checkpoint.json")
            merged = codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, delta, preserve_prefixes=True)
            merged_clean = codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, clean, preserve_prefixes=True)
            for field in ("roots", "entries", "inventory", "security", "total_bytes", "entry_count"):
                self.assertEqual(merged[field], merged_clean[field], field)
            # 续作只重新扫描剩余条目的字节；已完成条目与投影根都记为复用。
            resumed_bytes = sum(entry["size"] for entry in delta["entries"] if entry["path"] in resumed)
            self.assertEqual((delta["scan"]["scanned_bytes"], delta["scan"]["reused_bytes"]), (resumed_bytes, delta["total_bytes"] - resumed_bytes))
            self.assertEqual((merged["scan"]["scanned_bytes"], merged["scan"]["reused_bytes"]), (resumed_bytes, projected["total_bytes"] + delta["total_bytes"] - resumed_bytes))
            self.assertEqual(merged["scan"]["total_bytes"], projected["total_bytes"] + delta["total_bytes"])

    def test_segment_seal_s2_written_merged_manifest_is_reused_without_rescan(self) -> None:
        """崩溃矩阵 S2（段 seal 第 3 阶段）：合并清单与投影收据已写、result 未写 → 第二次 seal 只读回清单并复核
        stat 边界（零扫描、不再投影／合并），清单字节不变即可直接进第 4 阶段。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            job_a, _job_b, delta_root, source, delta = self._projection_fixture(base)
            baseline_dir = base / "b1"
            baseline_dir.mkdir(mode=0o700)
            projected, receipt = codex_upgrade_evidence_manifest.project_evidence_manifest(source, keep_roots=[job_a])
            merged = codex_upgrade_evidence_manifest.merge_evidence_manifests(projected, delta, preserve_prefixes=True)
            manifest_path = codex_upgrade._evidence_manifest_path(baseline_dir)
            projection_path = baseline_dir / codex_upgrade.MANIFEST_PROJECTION_FILENAME
            codex_upgrade._write_or_verify_json(projection_path, receipt)
            codex_upgrade._write_or_verify_json(manifest_path, merged)
            manifest_bytes = manifest_path.read_bytes()
            # 第二次进入第 3 阶段：与 _seal_attempt_recovery_segment 的"清单已存在"分支同口径。
            with mock.patch.object(codex_upgrade_evidence_manifest, "_hash_and_scan") as scanner, \
                    mock.patch.object(codex_upgrade_evidence_manifest, "project_evidence_manifest") as project, \
                    mock.patch.object(codex_upgrade_evidence_manifest, "merge_evidence_manifests") as merge:
                loaded = codex_upgrade._load_evidence_manifest(manifest_path)
                loaded_receipt = codex_upgrade._read_json(projection_path, "投影收据")
                boundary = codex_upgrade_evidence_manifest.verify_manifest_boundary(loaded, [job_a, delta_root])
            scanner.assert_not_called()
            project.assert_not_called()
            merge.assert_not_called()
            self.assertEqual(boundary["scanned_bytes"], 0)
            self.assertEqual(loaded["manifest_digest"], merged["manifest_digest"])
            self.assertEqual(loaded_receipt, receipt)
            self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
            # 同一内容 write-or-verify 幂等；漂移内容拒绝覆盖。
            codex_upgrade._write_or_verify_json(manifest_path, merged)
            self.assertEqual(manifest_path.read_bytes(), manifest_bytes)
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._write_or_verify_json(manifest_path, {**merged, "entry_count": merged["entry_count"] + 1})

    def test_evidence_manifest_boundary_ignores_device_only_in_isolated_rehearsal(self) -> None:
        """隔离预演（overlay 副本）上 st_dev 必然不同：只在带标记且根在 overlay 上时忽略 device，其余 stat 仍逐项比较。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            base.chmod(0o700)
            root = base / "evidence"
            self._private_file(root / "a.json", b'{"a":true}\n')
            manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                [root], checkpoint_path=base / "checkpoint.json"
            )
            drifted = json.loads(json.dumps(manifest))
            for entry in drifted["entries"]:
                entry["device"] = entry["device"] + 1
            drifted["metadata_sha256"] = "e" * 64
            drifted["manifest_digest"] = codex_upgrade_evidence_manifest.canonical_json_sha256(
                {k: v for k, v in drifted.items() if k != "manifest_digest"}
            )
            with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "stat 边界发生漂移"):
                codex_upgrade_evidence_manifest.verify_manifest_boundary(drifted, [root])
            env = {codex_upgrade_evidence_manifest.REHEARSAL_CONTEXT_ENV: "1"}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                codex_upgrade_evidence_manifest, "_mount_fstype_of", return_value="ext4"
            ):
                with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "stat 边界发生漂移"):
                    codex_upgrade_evidence_manifest.verify_manifest_boundary(drifted, [root])
            with mock.patch.dict(os.environ, env), mock.patch.object(
                codex_upgrade_evidence_manifest, "_mount_fstype_of", return_value="overlay"
            ):
                self.assertEqual(
                    codex_upgrade_evidence_manifest.verify_manifest_boundary(drifted, [root])["status"],
                    "passed",
                )
                # 隔离预演下其它 stat 字段漂移仍失败关闭
                (root / "a.json").write_bytes(b'{"a":true,"b":1}\n')
                with self.assertRaisesRegex(codex_upgrade_evidence_manifest.EvidenceManifestError, "stat 边界发生漂移"):
                    codex_upgrade_evidence_manifest.verify_manifest_boundary(drifted, [root])

    def test_metadata_only_deep_verify_reuses_existing_source_manifest(self) -> None:
        """已有来源 manifest 时，deep-verify 只核对边界，不重扫正文。"""

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            campaign = base / "campaign"
            campaign.mkdir(mode=0o700)
            attempt_root = campaign / "candidates" / "candidate-a" / "attempts" / "attempt-a"
            attempt_root.mkdir(parents=True, mode=0o700)
            source_dir = base / "source-campaign"
            source_root = source_dir / "candidates" / "source" / "attempts" / "source-a"
            source_root.mkdir(parents=True, mode=0o700)
            source_evidence = base / "source-evidence"
            self._private_file(source_evidence / "source.json", b'{"source":true}\n')
            source_manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                [source_evidence],
                checkpoint_path=base / "source-checkpoint.json",
            )
            manifest_path = codex_upgrade._evidence_manifest_path(source_root)
            self._private_file(
                manifest_path,
                (json.dumps(source_manifest, ensure_ascii=False) + "\n").encode(),
            )
            attempt = {
                "attempt_id": "attempt-a",
                "candidate_id": "candidate-a",
                "identity": {},
                "classification_candidate_reuse_transition": {},
            }
            source_attempt = {
                "attempt_id": "source-a",
                "evidence_roots": [str(source_evidence)],
                "results": [
                    {
                        "id": job_id,
                        "status": "complete",
                        "evidence_roots": [str(source_evidence)],
                    }
                    for job_id in sorted(
                        codex_upgrade.CLASSIFICATION_CANDIDATE_REUSE_JOB_IDS
                    )
                ],
            }
            with (
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value={"campaign_id": "campaign-a"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, attempt),
                ),
                mock.patch.object(codex_upgrade, "_bind_active_lease_attempt"),
                mock.patch.object(codex_upgrade, "_verify_plan_identity"),
                mock.patch.object(
                    codex_upgrade,
                    "_classification_candidate_reuse_attempt_source",
                    return_value=(source_dir, source_root, source_attempt, {}),
                ),
                mock.patch.object(
                    codex_upgrade_evidence_manifest,
                    "_hash_and_scan",
                ) as scanner,
            ):
                result = codex_upgrade.deep_verify_campaign(
                    campaign,
                    candidate_id="candidate-a",
                    attempt_id="attempt-a",
                )
            scanner.assert_not_called()
            self.assertEqual(result["full_scan_count"], 0)
            self.assertEqual(result["scanned_bytes"], 0)
            self.assertEqual(result["reused_bytes"], source_manifest["total_bytes"])

    def test_historical_inventory_only_allows_order_difference(self) -> None:
        first = {"path": "evidence/a.json", "size": 1, "sha256": "a" * 64}
        second = {"path": "run/b.json", "size": 2, "sha256": "b" * 64}
        historical = {
            "entry_count": 2,
            "entries": [first, second],
            "digest": "c" * 64,
        }
        manifest = {
            "entry_count": 2,
            "entries": [second, first],
            "digest": "d" * 64,
        }
        self.assertTrue(
            codex_upgrade._inventory_contents_equal(historical, manifest)
        )
        changed = json.loads(json.dumps(manifest))
        changed["entries"][0]["size"] = 3
        self.assertFalse(
            codex_upgrade._inventory_contents_equal(historical, changed)
        )
        duplicate = {
            "entry_count": 2,
            "entries": [first, first],
            "digest": "e" * 64,
        }
        self.assertFalse(
            codex_upgrade._inventory_contents_equal(historical, duplicate)
        )

    def test_imported_stage_replay_accepts_only_inventory_order_difference(
        self,
    ) -> None:
        def write_json(path: Path, payload: object) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        first = {"path": "evidence/a.json", "size": 1, "sha256": "a" * 64}
        second = {"path": "run/b.json", "size": 2, "sha256": "b" * 64}
        historical = {
            "entry_count": 2,
            "entries": [first, second],
            "digest": "c" * 64,
        }
        manifest_inventory = {
            "entry_count": 2,
            "entries": [second, first],
            "digest": "d" * 64,
        }
        security = {
            "file_count": 2,
            "findings": [],
            "known_secret_env_names": [],
            "known_secret_scan_passed": True,
            "limitation": None,
            "scanned_bytes": 3,
        }
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            manifest_path = campaign / "evidence-manifest.json"
            manifest_path.write_text("{}\n", encoding="utf-8")
            stage = {
                "predecessor_import": {
                    "path": "predecessor-import.json",
                    "sha256": "e" * 64,
                },
                "evidence_manifest": {
                    "path": "evidence-manifest.json",
                    "sha256": codex_upgrade.file_sha256(manifest_path),
                },
                "evidence_roots": [str(campaign / "evidence")],
                "evidence_inventory": historical,
                "security": {"raw_evidence_private": True, **security},
            }
            loaded_manifest = {
                "inventory": manifest_inventory,
                "security": security,
            }
            with mock.patch.object(
                codex_upgrade,
                "_load_evidence_manifest",
                return_value=loaded_manifest,
            ):
                self.assertIs(
                    codex_upgrade._stage_evidence_manifest(
                        campaign,
                        stage,
                        verify_boundary=False,
                    ),
                    loaded_manifest,
                )

                local_stage = dict(stage)
                local_stage.pop("predecessor_import")
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "inventory 不一致",
                ):
                    codex_upgrade._stage_evidence_manifest(
                        campaign,
                        local_stage,
                        verify_boundary=False,
                    )

                changed = json.loads(json.dumps(stage))
                changed["evidence_inventory"]["entries"][0]["size"] = 9
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "inventory 不一致",
                ):
                    codex_upgrade._stage_evidence_manifest(
                        campaign,
                        changed,
                        verify_boundary=False,
                    )

                duplicate = json.loads(json.dumps(stage))
                duplicate["evidence_inventory"]["entries"] = [first, first]
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "inventory 不一致",
                ):
                    codex_upgrade._stage_evidence_manifest(
                        campaign,
                        duplicate,
                        verify_boundary=False,
                    )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            predecessor = base / "predecessor"
            successor = base / "successor"
            predecessor_manifest = {"campaign_id": "predecessor"}
            write_json(predecessor / "campaign.json", predecessor_manifest)
            predecessor_binding = {
                "campaign_dir": str(predecessor),
                "campaign_id": "predecessor",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    predecessor / "campaign.json"
                ),
            }
            successor_manifest = {
                "campaign_id": "successor",
                "predecessor": {
                    **predecessor_binding,
                    "reason": "candidate_recovery_control_replacement",
                },
            }
            write_json(successor / "campaign.json", successor_manifest)
            manifest_relative = "official/attempts/original/evidence-manifest.json"
            original_manifest = predecessor / manifest_relative
            write_json(original_manifest, {})
            inherited_binding = {
                "path": manifest_relative,
                "sha256": codex_upgrade.file_sha256(original_manifest),
            }
            predecessor_stage_core = {
                "stage": "capture-official",
                "campaign_id": "predecessor",
                "campaign_manifest_sha256": predecessor_binding[
                    "campaign_manifest_sha256"
                ],
                "evidence_manifest": inherited_binding,
            }
            write_json(
                predecessor / "official/result.json",
                {
                    **predecessor_stage_core,
                    "package_digest": codex_upgrade._fingerprint(
                        predecessor_stage_core
                    ),
                },
            )
            receipt_core = {
                "reason": "candidate_recovery_control_replacement",
                "predecessor_campaign": predecessor_binding,
                "successor_campaign_id": "successor",
                "successor_campaign_manifest_sha256": codex_upgrade.file_sha256(
                    successor / "campaign.json"
                ),
            }
            receipt = {
                **receipt_core,
                "receipt_digest": codex_upgrade._fingerprint(receipt_core),
            }
            receipt_path = successor / "predecessor-import.json"
            write_json(receipt_path, receipt)
            import_binding = {
                "path": "predecessor-import.json",
                "sha256": codex_upgrade.file_sha256(receipt_path),
            }
            successor_stage_core = {
                "stage": "capture-official",
                "campaign_id": "successor",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    successor / "campaign.json"
                ),
                "predecessor_import": import_binding,
            }
            write_json(
                successor / "official/result.json",
                {
                    **successor_stage_core,
                    "package_digest": codex_upgrade._fingerprint(
                        successor_stage_core
                    ),
                },
            )
            imported_stage = {
                "stage": "capture-official",
                "predecessor_import": import_binding,
                "evidence_manifest": inherited_binding,
                "evidence_roots": [str(successor / "evidence")],
                "evidence_inventory": historical,
                "security": {"raw_evidence_private": True, **security},
            }
            loaded_manifest = {
                "inventory": manifest_inventory,
                "security": security,
            }
            with mock.patch.object(
                codex_upgrade,
                "_load_evidence_manifest",
                return_value=loaded_manifest,
            ):
                self.assertIs(
                    codex_upgrade._stage_evidence_manifest(
                        successor,
                        imported_stage,
                        verify_boundary=False,
                    ),
                    loaded_manifest,
                )
                original_manifest.write_text("{\"tampered\":true}\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "原文件漂移",
                ):
                    codex_upgrade._stage_evidence_manifest(
                        successor,
                        imported_stage,
                        verify_boundary=False,
                    )

    def test_interrupted_scan_resumes_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "evidence"
            root.mkdir(mode=0o700)
            first = root / "a.json"
            second = root / "b.json"
            self._private_file(first, b'{"a":1}')
            self._private_file(second, b'{"b":2}')
            checkpoint = base / "checkpoint.json"
            original = codex_upgrade_evidence_manifest._hash_and_scan
            calls: list[str] = []

            def interrupted(*args: object, **kwargs: object):
                result = original(*args, **kwargs)
                calls.append(str(args[1]))
                if len(calls) == 2:
                    raise RuntimeError("模拟中断")
                return result

            with mock.patch.object(
                codex_upgrade_evidence_manifest,
                "_hash_and_scan",
                side_effect=interrupted,
            ):
                with self.assertRaisesRegex(RuntimeError, "模拟中断"):
                    codex_upgrade_evidence_manifest.build_evidence_manifest(
                        [root],
                        checkpoint_path=checkpoint,
                    )
            resumed: list[str] = []

            def recording(*args: object, **kwargs: object):
                resumed.append(str(args[1]))
                return original(*args, **kwargs)

            with mock.patch.object(
                codex_upgrade_evidence_manifest,
                "_hash_and_scan",
                side_effect=recording,
            ):
                manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                    [root],
                    checkpoint_path=checkpoint,
                )
            self.assertEqual(len(resumed), 1)
            self.assertTrue(resumed[0].endswith("/b.json"))
            self.assertEqual(manifest["scan"]["reused_bytes"], first.stat().st_size)
            self.assertEqual(
                manifest["scan"]["scanned_bytes"],
                second.stat().st_size,
            )

    def test_scan_rejects_stat_boundary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "evidence"
            root.mkdir(mode=0o700)
            first = root / "a.json"
            second = root / "b.json"
            self._private_file(first, b'{"a":1}')
            self._private_file(second, b'{"b":2}')
            original = codex_upgrade_evidence_manifest._hash_and_scan

            def drifting(*args: object, **kwargs: object):
                result = original(*args, **kwargs)
                if str(args[1]).endswith("/a.json"):
                    second.write_bytes(b'{"b":3}')
                    second.chmod(0o600)
                return result

            with mock.patch.object(
                codex_upgrade_evidence_manifest,
                "_hash_and_scan",
                side_effect=drifting,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade_evidence_manifest.EvidenceManifestError,
                    "扫描期间发生漂移",
                ):
                    codex_upgrade_evidence_manifest.build_evidence_manifest(
                        [root],
                        checkpoint_path=base / "checkpoint.json",
                    )

    def test_existing_surface_is_reused_without_second_parser_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            campaign = base / "campaign"
            attempt_root = campaign / "candidates" / "c1" / "attempts" / "r26"
            attempt_root.mkdir(parents=True, mode=0o700)
            for path in (campaign, *attempt_root.parents):
                if path.is_dir() and path.is_relative_to(campaign):
                    path.chmod(0o700)
            evidence = base / "evidence"
            evidence.mkdir(mode=0o700)
            capture = evidence / "capture.json"
            self._private_file(
                capture,
                b'{"request":{"method":"POST","path":"/responses"}}',
            )
            manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                [evidence],
                checkpoint_path=base / "checkpoint.json",
            )
            finalized = attempt_root / "finalized"
            finalized.mkdir(mode=0o700)
            surface = codex_upgrade._scan_evidence_files(
                [capture],
                "target-sub2api",
                input_paths=[evidence],
            )
            self._private_file(
                finalized / "surface.json",
                (json.dumps(surface, ensure_ascii=False) + "\n").encode(),
            )
            attempt = {"evidence_roots": [str(evidence)]}
            with mock.patch.object(
                codex_upgrade,
                "_scan_evidence_files",
            ) as scanner:
                loaded, binding = codex_upgrade._load_or_build_attempt_surface(
                    campaign,
                    attempt_root,
                    attempt,
                    manifest,
                    label="target-sub2api",
                )
            scanner.assert_not_called()
            self.assertEqual(loaded, surface)
            self.assertTrue(binding["path"].endswith("finalized/surface.json"))

    def test_new_surface_uses_manifest_file_list_not_recursive_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            campaign = base / "campaign"
            attempt_root = campaign / "official" / "attempts" / "r1"
            attempt_root.mkdir(parents=True, mode=0o700)
            for path in (campaign, *attempt_root.parents):
                if path.is_dir() and path.is_relative_to(campaign):
                    path.chmod(0o700)
            evidence = base / "evidence"
            evidence.mkdir(mode=0o700)
            capture = evidence / "capture.json"
            self._private_file(
                capture,
                b'{"request":{"method":"POST","path":"/responses"}}',
            )
            manifest = codex_upgrade_evidence_manifest.build_evidence_manifest(
                [evidence],
                checkpoint_path=base / "checkpoint.json",
            )
            attempt = {"evidence_roots": [str(evidence)]}
            with (
                mock.patch.object(codex_upgrade, "scan_evidence") as recursive,
                mock.patch.object(
                    codex_upgrade,
                    "_scan_evidence_files",
                    wraps=codex_upgrade._scan_evidence_files,
                ) as bounded,
            ):
                surface, _ = codex_upgrade._load_or_build_attempt_surface(
                    campaign,
                    attempt_root,
                    attempt,
                    manifest,
                    label="target-official",
                )
            recursive.assert_not_called()
            bounded.assert_called_once()
            self.assertEqual(surface["file_count"], 1)

    def test_existing_deep_verify_receipt_must_match_current_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory) / "campaign"
            checkpoints = campaign / "verification-checkpoints"
            checkpoints.mkdir(parents=True, mode=0o700)
            for path in (campaign, checkpoints):
                path.chmod(0o700)
            self._private_file(campaign / "campaign.json", b"{}\n")
            stage = campaign / "official" / "result.json"
            self._private_file(stage, b'{"status":"complete"}\n')
            scan = {
                "status": "passed",
                "manifest_digest": "a" * 64,
                "full_scan_count": 1,
                "scanned_bytes": 1,
                "reused_bytes": 0,
                "total_bytes": 1,
                "elapsed_seconds": 0.1,
            }
            with mock.patch.object(
                codex_upgrade,
                "load_campaign_manifest",
                return_value={"campaign_id": "campaign-a"},
            ):
                codex_upgrade._write_deep_verify_receipt(
                    campaign,
                    "capture-official",
                    None,
                    stage_path=stage,
                    evidence_manifest_binding={
                        "path": "manifest.json",
                        "sha256": "b" * 64,
                    },
                    imported_checkpoint=None,
                    scan=scan,
                    evaluation_transition=None,
                )
                self._private_file(stage, b'{"status":"changed"}\n')
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "当前预期事实不一致",
                ):
                    codex_upgrade._write_deep_verify_receipt(
                        campaign,
                        "capture-official",
                        None,
                        stage_path=stage,
                        evidence_manifest_binding={
                            "path": "manifest.json",
                            "sha256": "b" * 64,
                        },
                        imported_checkpoint=None,
                        scan=scan,
                        evaluation_transition=None,
                    )

    def test_v3_seal_schema_preserves_v2_and_stage_manifest_fields(self) -> None:
        tool_root = Path(codex_upgrade.__file__).resolve().parent
        seal_schema = json.loads(
            (tool_root / "codex_upgrade_seal_preview.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            seal_schema["properties"]["schema_version"]["enum"],
            [
                codex_upgrade.LEGACY_SEAL_PREVIEW_SCHEMA,
                codex_upgrade.SEAL_PREVIEW_SCHEMA,
            ],
        )
        stage_schema = json.loads(
            (tool_root / "codex_upgrade_stage_result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            {"evidence_manifest", "scan_summary", "evaluation_transition"}
            .issubset(stage_schema["properties"])
        )


class ToolIdentitySideSplitTest(unittest.TestCase):
    """工具身份按证据影响面分级：产出侧严格、评估侧放行且留痕。

    这套判定替代了原先「任何工具漂移一律拒绝」的一刀切。分级的前提是评估侧文件
    只读既有证据，改动后已封存字节逐字不变；因此本测试必须同时锁死两件事——
    产出侧不得被放行，未登记的新文件不得被当成评估侧。
    """

    def _identity(self, entries):
        payload = {
            "entry_count": len(entries),
            "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
            "entries": entries,
            **codex_upgrade._tool_identity_sides(entries),
        }
        return payload

    def _final_execution_source(self):
        execute = list(codex_upgrade.CONTROL_EPOCH_FINAL_EXECUTION_JOB_IDS)
        reused = [
            "candidate-compact-direct",
            "candidate-core-direct",
            "candidate-frozen-aux",
            "candidate-frozen-core",
            "candidate-h1-wire",
            "candidate-images-wire",
            "candidate-ws-handshake-repeat",
        ]
        return {
            "planned_job_ids": sorted(execute + reused),
            "execute_job_ids": execute,
            "reused_job_ids": reused,
        }

    def test_final_epoch_allows_only_mapped_failed_scope_producer_drift(self):
        paths = [
            "build_fingerprint_proxy.sh",
            "prewarm_codex_home.py",
            "run_sub2api_openai_mitm_matrix.sh",
            "runtime_scripts/run_fingerprint_mitm_pair.sh",
            "runtime_scripts/start_mitm.sh",
        ]
        expected = self._identity(
            [{"path": path, "sha256": "a" * 64} for path in paths]
        )
        current = self._identity(
            [{"path": path, "sha256": "b" * 64} for path in paths]
        )
        manifest = {"tool_identity": expected, "configuration": {}}
        allowed = codex_upgrade._control_epoch_failed_scope_production_paths(
            manifest,
            current,
            self._final_execution_source(),
        )
        self.assertEqual(allowed, set(paths))
        invariants = codex_upgrade._control_epoch_invariants(
            manifest,
            current,
            allowed_production_paths=allowed,
        )
        self.assertEqual(
            invariants["tool_production_sha256"],
            codex_upgrade._tool_identity_side_digest_excluding(
                expected,
                "production",
                codex_upgrade._PHASE_EVALUATION_HYBRID_FILES,
            ),
        )

    def test_final_epoch_rejects_producer_drift_touching_reused_job(self):
        path = "run_sub2api_direct_matrix.sh"
        expected = self._identity([{"path": path, "sha256": "a" * 64}])
        current = self._identity([{"path": path, "sha256": "b" * 64}])
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "失败闭集之外",
        ):
            codex_upgrade._control_epoch_failed_scope_production_paths(
                {"tool_identity": expected},
                current,
                self._final_execution_source(),
            )

    def test_final_epoch_excludes_0151_assertion_profile_from_production_drift(self):
        """分类事实纠正不得被误判为 Candidate 产出侧变化。"""

        path = "candidate_rule_expectations_0_151_0.json"
        expected = self._identity([{"path": path, "sha256": "a" * 64}])
        current = self._identity([{"path": path, "sha256": "b" * 64}])
        allowed = codex_upgrade._control_epoch_failed_scope_production_paths(
            {"tool_identity": expected},
            current,
            self._final_execution_source(),
        )
        self.assertEqual(allowed, set())
        self.assertIn(path, codex_upgrade._PHASE_EVALUATION_HYBRID_FILES)

    def test_real_tree_splits_into_both_sides(self):
        identity = codex_upgrade._tool_identity(include_git=False)
        self.assertEqual(
            identity["entry_count"],
            identity["production_count"] + identity["evaluation_count"],
        )
        self.assertGreater(identity["evaluation_count"], 0)
        # 白名单里的每一项都必须真实存在，否则是登记了不存在的豁免。
        paths = {entry["path"] for entry in identity["entries"]}
        self.assertEqual(codex_upgrade._EVALUATION_SIDE_FILES - paths, set())

    def test_unlisted_file_falls_back_to_production(self):
        """fail-close：未登记文件必须落到产出侧，不能因为忘记登记被静默放行。"""
        entries = [{"path": "brand_new_tool.py", "sha256": "a" * 64}]
        sides = codex_upgrade._tool_identity_sides(entries)
        self.assertEqual(sides["production_count"], 1)
        self.assertEqual(sides["evaluation_count"], 0)

    def test_single_runner_change_invalidates_only_its_job(self):
        """逐文件反向依赖不得把一个 runner 变化扩大到同组件全部 Job。"""

        expected = self._identity(
            [
                {"path": "runner-a.sh", "sha256": "a" * 64},
                {"path": "runner-b.sh", "sha256": "b" * 64},
            ]
        )
        current = self._identity(
            [
                {"path": "runner-a.sh", "sha256": "c" * 64},
                {"path": "runner-b.sh", "sha256": "b" * 64},
            ]
        )
        jobs = [
            Job(
                job_id=f"job-{suffix}",
                phase="candidate",
                suites=("full",),
                description=f"job-{suffix}",
                steps=({"argv": [f"/capture/runner-{suffix}.sh"], "environment": {}},),
                evidence_roots=(f"/tmp/job-{suffix}",),
                covers=(),
            )
            for suffix in ("a", "b")
        ]
        affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            jobs,
            expected,
            current,
        )
        self.assertEqual(affected, ["job-a"])
        self.assertEqual(changed, ["runner-a.sh"])
        self.assertEqual(unmapped, [])

    def test_registered_runner_for_other_jobs_has_zero_local_impact(self):
        """已登记但不属于当前计划的 producer 变化不得使当前 Job 失效。"""

        changed_path = "run_sub2api_direct_matrix.sh"
        stable_path = "run_sub2api_openai_mitm_matrix.sh"
        expected = self._identity(
            [
                {"path": changed_path, "sha256": "a" * 64},
                {"path": stable_path, "sha256": "b" * 64},
            ]
        )
        current = self._identity(
            [
                {"path": changed_path, "sha256": "c" * 64},
                {"path": stable_path, "sha256": "b" * 64},
            ]
        )
        job = Job(
            job_id="candidate-core-mitm",
            phase="candidate",
            suites=("full",),
            description="candidate-core-mitm",
            steps=({"argv": [f"/capture/{stable_path}"], "environment": {}},),
            evidence_roots=("/tmp/candidate-core-mitm",),
            covers=(),
        )
        affected, _changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [job],
            expected,
            current,
        )
        self.assertEqual(affected, [])
        self.assertEqual(unmapped, [])

    def test_unregistered_producer_change_stops_instead_of_full_rerun(self):
        """未知产出文件必须返回未映射集合，调用方据此在 reservation 前停线。"""

        expected = self._identity(
            [{"path": "unknown-producer.sh", "sha256": "a" * 64}]
        )
        current = self._identity(
            [{"path": "unknown-producer.sh", "sha256": "b" * 64}]
        )
        affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [],
            expected,
            current,
        )
        self.assertEqual(affected, [])
        self.assertEqual(changed, ["unknown-producer.sh"])
        self.assertEqual(unmapped, ["unknown-producer.sh"])

    def test_evaluation_file_change_has_zero_candidate_job_impact(self):
        """控制／评估文件变化只更新控制事实，不得使 Candidate Job 失效。"""

        path = "codex_upgrade_supervisor.py"
        expected = self._identity([{"path": path, "sha256": "a" * 64}])
        current = self._identity([{"path": path, "sha256": "b" * 64}])
        affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [],
            expected,
            current,
        )
        self.assertEqual(affected, [])
        self.assertEqual(changed, [])
        self.assertEqual(unmapped, [])

    def test_zero_production_drift_skips_job_dependency_graph(self):
        """产出路径未变化时不得构建昂贵的 Job 依赖反向图。"""

        path = "codex_upgrade_supervisor.py"
        expected = self._identity([{"path": path, "sha256": "a" * 64}])
        current = self._identity([{"path": path, "sha256": "b" * 64}])
        with mock.patch.object(
            codex_upgrade,
            "_tool_path_job_map",
            side_effect=AssertionError("零变化不应解析 Job 依赖"),
        ) as dependency_graph:
            affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
                [],
                expected,
                current,
            )
        self.assertEqual(affected, [])
        self.assertEqual(changed, [])
        self.assertEqual(unmapped, [])
        dependency_graph.assert_not_called()

    def test_hybrid_drift_requires_explicit_historical_replay_authorization(self):
        """v2 wire 闭包变化默认停线，仅严格历史恢复可排除阶段混合文件。"""

        paths = {
            "candidate_rule_expectations_0_154_0.json",
            "codex_upgrade.py",
        }
        expected = self._identity(
            [{"path": path, "sha256": "a" * 64} for path in sorted(paths)]
        )
        current = self._identity(
            [{"path": path, "sha256": "b" * 64} for path in sorted(paths)]
        )
        expected["orchestrator_closures"] = {
            "wire_producer": {"closure_sha256": "c" * 64}
        }
        current["orchestrator_closures"] = {
            "wire_producer": {"closure_sha256": "d" * 64}
        }

        _affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [],
            expected,
            current,
        )
        self.assertEqual(set(changed), paths)
        self.assertEqual(set(unmapped), paths)

        affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [],
            expected,
            current,
            allow_phase_evaluation_hybrid_drift=True,
        )
        self.assertEqual(affected, [])
        self.assertEqual(changed, [])
        self.assertEqual(unmapped, [])

    def test_control_and_environment_schemas_are_evaluation_side(self):
        """控制／环境收据 Schema 变化不得使已封存官方证据失效。"""
        paths = {
            "codex_upgrade_arm64_environment_receipt.schema.json",
            "codex_upgrade_campaign_lease.schema.json",
            "codex_upgrade_campaign_lease_stop.schema.json",
            "codex_upgrade_classification_candidate_reuse_transition.schema.json",
            "codex_upgrade_capture_reservation.schema.json",
        }
        entries = [
            {"path": path, "sha256": "a" * 64}
            for path in sorted(paths)
        ]
        sides = codex_upgrade._tool_identity_sides(entries)
        self.assertEqual(sides["production_count"], 0)
        self.assertEqual(sides["evaluation_count"], len(paths))
        drift = codex_upgrade._tool_identity_drift(
            self._identity(entries), self._identity([])
        )
        self.assertEqual(drift["production"], [])
        self.assertEqual(drift["evaluation"], sorted(paths))

    def test_evidence_permission_closeout_is_control_evaluation_only(self):
        """权限闭合只改变元数据与控制收据，不得使请求 Job 失效。"""

        path = "codex_upgrade_evidence_permissions.py"
        entries = [{"path": path, "sha256": "a" * 64}]
        sides = codex_upgrade._tool_identity_sides(entries)
        self.assertEqual(sides["production_count"], 0)
        self.assertEqual(sides["evaluation_count"], 1)
        components = codex_upgrade._tool_component_identities(entries)["components"]
        self.assertEqual(components["control"]["entry_count"], 1)
        affected, changed, unmapped = codex_upgrade._exact_tool_path_impact(
            [],
            self._identity(entries),
            self._identity(
                [{"path": path, "sha256": "b" * 64}]
            ),
        )
        self.assertEqual(affected, [])
        self.assertEqual(changed, [])
        self.assertEqual(unmapped, [])

    def test_canonical_control_and_activation_files_are_evaluation_side(self):
        """canonical 调度、门禁和画像补丁不得触发候选请求重跑。"""
        paths = set(codex_upgrade._CANONICAL_EVALUATION_ONLY_FILES)
        entries = [
            {"path": path, "sha256": "a" * 64}
            for path in sorted(paths)
        ]
        sides = codex_upgrade._tool_identity_sides(entries)
        self.assertEqual(sides["production_count"], 0)
        self.assertEqual(sides["evaluation_count"], len(paths))
        components = codex_upgrade._tool_component_identities(entries)["components"]
        self.assertEqual(components["shared"]["entry_count"], 0)
        expected_control = len(
            paths.intersection(codex_upgrade._CONTROL_PLANE_TOOL_FILES)
        )
        self.assertEqual(
            components["control"]["entry_count"],
            expected_control,
        )
        self.assertEqual(
            components["evaluator"]["entry_count"],
            len(paths) - expected_control,
        )

    def test_candidate_trace_transformers_are_evaluation_side(self):
        """候选 trace 转换器和映射只处理既有证据，不得使请求 Job 失效。"""

        paths = {
            "candidate_test_trace.py",
            "candidate_test_fact_map_0_151_0.json",
        }
        entries = [
            {"path": path, "sha256": "a" * 64}
            for path in sorted(paths)
        ]
        sides = codex_upgrade._tool_identity_sides(entries)
        self.assertEqual(sides["production_count"], 0)
        self.assertEqual(sides["evaluation_count"], len(paths))

    def test_control_environment_and_data_components_are_independent(self):
        """控制、环境和数据文件必须落入不同的失效身份。"""

        entries = [
            {
                "path": "codex_upgrade_supervisor.py",
                "sha256": "a" * 64,
            },
            {
                "path": "codex_upgrade_arm64_environment_receipt.py",
                "sha256": "b" * 64,
            },
            {
                "path": "run_sub2api_openai_mitm_matrix.sh",
                "sha256": "c" * 64,
            },
        ]
        identity = codex_upgrade._tool_component_identities(entries)
        components = identity["components"]
        self.assertEqual(components["control"]["entry_count"], 1)
        self.assertEqual(components["environment"]["entry_count"], 1)
        self.assertEqual(components["relay"]["entry_count"], 1)

    def test_all_versioned_evidence_labels_are_evaluator_only(self):
        """版本化证据标签补漏不得污染 shared 或使抓包 Job 失效。"""

        identity = codex_upgrade._tool_identity(include_git=False)
        label_paths = {
            str(entry["path"])
            for entry in identity["entries"]
            if str(entry["path"]).startswith("codex_upgrade_evidence_labels_")
            and str(entry["path"]).endswith(".json")
        }
        self.assertGreater(len(label_paths), 0)
        self.assertTrue(label_paths.issubset(codex_upgrade._EVALUATION_SIDE_FILES))
        components = codex_upgrade._tool_component_identities(
            [
                {"path": path, "sha256": "a" * 64}
                for path in sorted(label_paths)
            ]
        )["components"]
        self.assertEqual(components["evaluator"]["entry_count"], len(label_paths))
        self.assertEqual(components["shared"]["entry_count"], 0)

    def test_0154_evidence_labels_use_current_model_tracks(self):
        """0.154 专用标签不得残留旧主轨或旧 Lite 轨模型名称。"""

        path = Path(codex_upgrade.__file__).with_name(
            "codex_upgrade_evidence_labels_0_154_0.json"
        )
        serialized = json.dumps(
            json.loads(path.read_text(encoding="utf-8")),
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn("gpt-5.5", serialized)
        self.assertIn("gpt-6-astra", serialized)
        self.assertNotIn("gpt-5.4", serialized)
        self.assertNotIn("gpt-5.6-luna", serialized)

    def test_invalidation_summary_never_overlaps_reuse(self):
        """失效 Job 与复用收据不能同时包含同一项。"""

        summary = codex_upgrade._capture_invalidation_summary(
            invalidated_job_ids=["candidate-core-mitm"],
            reused_receipt_ids=["candidate-core-direct"],
            changed_control_paths=["codex_upgrade.py"],
            changed_environment_paths=[
                "codex_upgrade_arm64_environment_receipt.py"
            ],
        )
        self.assertEqual(
            summary,
            {
                "invalidated_control_ids": ["codex_upgrade.py"],
                "invalidated_p0_ids": ["arm64-environment"],
                "invalidated_job_ids": ["candidate-core-mitm"],
                "reused_receipt_ids": ["candidate-core-direct"],
            },
        )
        with self.assertRaisesRegex(
            codex_upgrade.ConfigurationError,
            "集合重叠",
        ):
            codex_upgrade._capture_invalidation_summary(
                invalidated_job_ids=["candidate-core-mitm"],
                reused_receipt_ids=["candidate-core-mitm"],
            )

    def test_preflight_plan_does_not_hardcode_bound_p0_as_invalidated(self):
        """新 preflight 只重放绑定 P0，不得把环境 producer 伪报为变化。"""

        summary = codex_upgrade._preflight_plan_invalidation_summary(
            {"jobs": [{"id": "official-core"}, {"id": "candidate-core-mitm"}]}
        )
        self.assertEqual(summary["invalidated_p0_ids"], [])
        self.assertEqual(summary["reused_receipt_ids"], ["arm64-environment"])
        self.assertEqual(
            summary["invalidated_job_ids"],
            ["candidate-core-mitm", "official-core"],
        )

    def test_sealed_stage_recovery_uses_complete_evaluation_closure(self):
        """sealed-stage 白名单必须覆盖同一份评估组件闭集。"""
        self.assertTrue(
            codex_upgrade._EVALUATION_COMPONENT_FILES.issubset(
                codex_upgrade._SEALED_STAGE_RECOVERY_ALLOWED_FILES
            )
        )
        self.assertIn(
            "codex_upgrade_arm64_environment_receipt.schema.json",
            codex_upgrade._SEALED_STAGE_RECOVERY_ALLOWED_FILES,
        )
        self.assertIn(
            "codex_upgrade_classification_candidate_reuse_transition.schema.json",
            codex_upgrade._SEALED_STAGE_RECOVERY_ALLOWED_FILES,
        )
        self.assertEqual(
            codex_upgrade._tool_component_for_path(
                "codex_upgrade_classification_candidate_reuse_transition.schema.json"
            ),
            "control",
        )

    def test_drift_classifies_changed_paths(self):
        before = [
            {"path": "assertion_gate.py", "sha256": "a" * 64},
            {"path": "run_candidate_core_capture.sh", "sha256": "b" * 64},
        ]
        after = [
            {"path": "assertion_gate.py", "sha256": "c" * 64},
            {"path": "run_candidate_core_capture.sh", "sha256": "b" * 64},
        ]
        drift = codex_upgrade._tool_identity_drift(
            self._identity(after), self._identity(before)
        )
        self.assertEqual(drift["evaluation"], ["assertion_gate.py"])
        self.assertEqual(drift["production"], [])

    def test_drift_detects_added_and_removed_files(self):
        before = [{"path": "assertion_gate.py", "sha256": "a" * 64}]
        after = [
            {"path": "assertion_gate.py", "sha256": "a" * 64},
            {"path": "run_new_capture.sh", "sha256": "d" * 64},
        ]
        drift = codex_upgrade._tool_identity_drift(
            self._identity(after), self._identity(before)
        )
        self.assertEqual(drift["production"], ["run_new_capture.sh"])
        drift_removed = codex_upgrade._tool_identity_drift(
            self._identity(before), self._identity(after)
        )
        self.assertEqual(drift_removed["production"], ["run_new_capture.sh"])

    def test_evaluation_drift_ledger_appends_and_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            before = [{"path": "assertion_gate.py", "sha256": "a" * 64}]
            after = [{"path": "assertion_gate.py", "sha256": "c" * 64}]
            expected, current = self._identity(before), self._identity(after)
            drift = codex_upgrade._tool_identity_drift(current, expected)
            codex_upgrade._record_evaluation_side_drift(
                campaign, current, expected, drift
            )
            ledger_path = campaign / "tool-evaluation-drift.json"
            self.assertTrue(ledger_path.is_file())
            self.assertEqual(ledger_path.stat().st_mode & 0o777, 0o600)
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(
                ledger["schema_version"],
                codex_upgrade.TOOL_EVALUATION_DRIFT_SCHEMA,
            )
            self.assertEqual(len(ledger["records"]), 1)
            self.assertEqual(
                ledger["records"][0]["changed_files"], ["assertion_gate.py"]
            )
            # 同一评估侧状态重复放行不再追加。
            codex_upgrade._record_evaluation_side_drift(
                campaign, current, expected, drift
            )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["records"]), 1)
            # 再次变化则必须留下第二条。
            third = self._identity([{"path": "assertion_gate.py", "sha256": "e" * 64}])
            codex_upgrade._record_evaluation_side_drift(
                campaign,
                third,
                expected,
                codex_upgrade._tool_identity_drift(third, expected),
            )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(len(ledger["records"]), 2)

    def test_production_only_drift_writes_no_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign = Path(directory)
            before = [{"path": "run_candidate_core_capture.sh", "sha256": "a" * 64}]
            after = [{"path": "run_candidate_core_capture.sh", "sha256": "b" * 64}]
            expected, current = self._identity(before), self._identity(after)
            drift = codex_upgrade._tool_identity_drift(current, expected)
            self.assertEqual(drift["production"], ["run_candidate_core_capture.sh"])
            codex_upgrade._record_evaluation_side_drift(
                campaign, current, expected, drift
            )
            self.assertFalse((campaign / "tool-evaluation-drift.json").exists())


if __name__ == "__main__":
    unittest.main()


class ExecutionTreeVerificationTest(unittest.TestCase):
    """采集执行副本必须与受管树逐字一致。

    k71 的根因：`_tool_identity` 只扫描本文件所在的受管树，而采集脚本与 relay 由
    `$CAPTURE_MOUNT/tools/official_client_capture/` 执行，是另一份副本。两者漂移时
    工具身份校验照样通过，跑的却是旧代码——受管树里 Cookie 与 Lite 两组修复都已就位，
    执行副本停在更早版本，四条判据必败且无任何报警。
    """

    def _mirror(self, root: Path) -> Path:
        """按受管口径把当前工具树复制一份到 root/tools/official_client_capture。"""

        managed = Path(codex_upgrade.__file__).resolve().parent
        target = root / "tools" / "official_client_capture"
        for entry in codex_upgrade._tool_tree_entries(managed):
            dst = target / entry["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((managed / entry["path"]).read_bytes())
        return target

    def test_identical_copy_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._mirror(root)
            codex_upgrade._verify_execution_tree(root)

    def test_drifted_copy_is_rejected_and_names_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._mirror(root)
            drifted = target / "upstream_byte_relay.py"
            drifted.write_bytes(drifted.read_bytes() + "\n# 旧副本\n".encode("utf-8"))
            with self.assertRaises(codex_upgrade.ConfigurationError) as raised:
                codex_upgrade._verify_execution_tree(root)
        self.assertIn("upstream_byte_relay.py", str(raised.exception))

    def test_missing_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._mirror(root)
            (target / "upstream_byte_relay.py").unlink()
            with self.assertRaises(codex_upgrade.ConfigurationError):
                codex_upgrade._verify_execution_tree(root)

    def test_absent_execution_root_is_skipped(self) -> None:
        """执行位置不存在不是漂移，是路径配置问题，由采集脚本自身的解析负责。

        本校验若把「必须存在」也管上，所有用假 capture_root 的单元测试都跑不了，
        而真实采集机上该目录必然存在——收紧这一条只有代价没有收益。
        """

        with tempfile.TemporaryDirectory() as tmp:
            codex_upgrade._verify_execution_tree(Path(tmp))

    def test_inaccessible_execution_root_is_skipped(self) -> None:
        """普通用户无法遍历父目录时，不应让离线计划和 CI 单元测试崩溃。"""

        with mock.patch.object(
            Path,
            "is_dir",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            codex_upgrade._verify_execution_tree(Path("/root/oauth-capture"))

    def test_execution_root_equal_to_managed_tree_is_allowed(self) -> None:
        """本地直接在仓库内跑时两者同一目录，不应自我否决。"""

        repo_root = Path(codex_upgrade.__file__).resolve().parents[2]
        codex_upgrade._verify_execution_tree(repo_root)
