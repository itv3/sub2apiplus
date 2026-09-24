"""Codex 0.154.0 完整 VC 制品链的入口与失败关闭测试。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade


class CodexUpgrade0154VCContractTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    @staticmethod
    def _manifest(version: str = "0.154.0") -> dict[str, object]:
        return {
            "campaign_id": "codex-0_154_0-campaign",
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "target_version": version,
        }

    @classmethod
    def _mark_implicit_r1(cls, campaign_dir: Path) -> None:
        """改造 2 之前的历史 Campaign 形态：已有原路径 VC-4 checkpoint 且无 revisions 目录 → 隐含 r1。"""

        cls._write(campaign_dir / "control" / "vc" / "vc-4-checkpoint.json", {"fixture": "vc-4"})

    @staticmethod
    def _active_ledger_patch() -> mock._patch:
        """合成夹具没有账本绑定；候选级写入门要求账本 active，这里显式给出该状态。"""

        return mock.patch.object(codex_upgrade, "_candidate_write_ledger_status", return_value="active")

    @staticmethod
    def _candidate_arguments(
        campaign_dir: Path,
        *,
        build_receipt: Path | None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            candidate_id="candidate-a",
            build_receipt=build_receipt,
            attempt_id=None,
            capture_manifest=None,
            assertion_evidence_root=None,
            restoration_report=None,
            evidence_root=[],
            approve_seal_sha256=None,
            observed_profile_receipt=None,
            client_evidence=[],
            rerun_failed=False,
        )

    def test_0154_classification_requires_derivation_inputs_before_evidence_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = [root / f"input-{index}.json" for index in range(5)]
            with (
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_require_formal_campaign",
                    return_value=self._manifest(),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_verify_plan_identity",
                    side_effect=AssertionError("缺输入时不得开始证据工作"),
                ),
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "--active-profile.*--profile-patch-manifest",
                ):
                    codex_upgrade.classify_campaign(
                        root / "campaign",
                        target_rule_manifest=inputs[0],
                        migration_manifest=inputs[1],
                        scenario_manifest=inputs[2],
                        profile_manifest=inputs[3],
                        assertion_profile_manifest=inputs[4],
                        active_profile=None,
                        profile_patch_manifest=None,
                    )

    def test_candidate_run_missing_build_receipt_stops_before_identity_and_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            self._mark_implicit_r1(campaign_dir)
            arguments = self._candidate_arguments(
                campaign_dir,
                build_receipt=None,
            )
            with (
                self._active_ledger_patch(),
                mock.patch.object(
                    codex_upgrade,
                    "_apply_candidate_runtime_override",
                    side_effect=lambda _root, manifest, _candidate: manifest,
                ),
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={"status": "complete"},
                ),
                mock.patch.object(codex_upgrade, "_candidate_identity_for_run") as identity,
                mock.patch.object(codex_upgrade, "_reserve_capture_attempt") as reserve,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "必须提供 --build-receipt",
                ):
                    codex_upgrade._run_capture_attempt(
                        arguments,
                        "candidate",
                        _lease=object(),
                        _manifest=self._manifest(),
                        _deadline=mock.MagicMock(),
                    )
            identity.assert_not_called()
            reserve.assert_not_called()

    def test_candidate_run_tampered_build_receipt_stops_before_identity_and_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            receipt_path = (
                campaign_dir
                / "candidates"
                / "candidate-a"
                / "build-receipt.json"
            )
            receipt_path.parent.mkdir(parents=True, mode=0o700)
            receipt_path.write_text(json.dumps({"tampered": True}) + "\n", encoding="utf-8")
            receipt_path.chmod(0o600)
            self._mark_implicit_r1(campaign_dir)
            arguments = self._candidate_arguments(
                campaign_dir,
                build_receipt=receipt_path,
            )
            with (
                self._active_ledger_patch(),
                mock.patch.object(
                    codex_upgrade,
                    "_apply_candidate_runtime_override",
                    side_effect=lambda _root, manifest, _candidate: manifest,
                ),
                mock.patch.object(codex_upgrade, "_reject_contaminated_campaign"),
                mock.patch.object(
                    codex_upgrade,
                    "_load_stage_result",
                    return_value={"status": "complete"},
                ),
                mock.patch.object(codex_upgrade, "_candidate_identity_for_run") as identity,
                mock.patch.object(codex_upgrade, "_reserve_capture_attempt") as reserve,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "构建收据字段不闭合",
                ):
                    codex_upgrade._run_capture_attempt(
                        arguments,
                        "candidate",
                        _lease=object(),
                        _manifest=self._manifest(),
                        _deadline=mock.MagicMock(),
                    )
            identity.assert_not_called()
            reserve.assert_not_called()

    def test_resume_and_all_require_new_candidate_identity_fields(self) -> None:
        arguments = argparse.Namespace(
            candidate_id="candidate-a",
            runtime_image=f"registry/sub2api@sha256:{'1' * 64}",
            build_id="build-a",
            profile_id="codex-0.154.0",
            profile_digest="2" * 64,
            deployed_version="0.154.0",
            candidate_purpose="validation_only",
            candidate_image_id=None,
            candidate_source=None,
            build_receipt=None,
        )
        for label in ("resume 候选抓包", "all 候选抓包"):
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "build_receipt.*candidate_image_id.*candidate_source",
                ):
                    codex_upgrade._require_candidate_launch_arguments(
                        arguments,
                        self._manifest(),
                        label=label,
                    )

    def test_0151_launch_compatibility_does_not_require_0154_receipts(self) -> None:
        arguments = argparse.Namespace(
            candidate_id="candidate-a",
            runtime_image=f"registry/sub2api@sha256:{'1' * 64}",
            build_id="build-a",
            profile_id="codex-0.151.0",
            profile_digest="2" * 64,
            deployed_version="0.151.0",
            candidate_purpose="validation_only",
            candidate_image_id=None,
            candidate_source=None,
            build_receipt=None,
        )
        codex_upgrade._require_candidate_launch_arguments(
            arguments,
            self._manifest("0.151.0"),
            label="历史候选抓包",
        )

    def test_native_canonical_compare_does_not_invent_missing_equal_fact(self) -> None:
        checkpoint = {
            "campaign": {
                "target_version": "0.154.0",
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
            },
            "migration": {
                "affected_rule_ids": ["SPEC-HDR-005"],
                "inherited_rule_ids": [],
            },
            "items": [
                {
                    "item_id": "candidate-seal",
                    "details": {"kind": "candidate-seal"},
                }
            ],
            "checkpoint_sha256": "1" * 64,
        }
        comparison = {"status": "complete", "offline_only": True}
        with (
            mock.patch.object(codex_upgrade, "_require_formal_campaign", return_value=self._manifest()),
            mock.patch.object(codex_upgrade, "_load_stage_result", return_value=comparison),
        ):
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "comparison"):
                codex_upgrade._canonical_compare(Path("/unused"), checkpoint)

    def test_native_canonical_accept_does_not_invent_missing_accepted_fact(self) -> None:
        checkpoint = {
            "campaign": {
                "target_version": "0.154.0",
                "candidate_id": "candidate-a",
                "attempt_id": "attempt-a",
            },
            "migration": {
                "affected_rule_ids": ["SPEC-HDR-005"],
                "inherited_rule_ids": [],
            },
            "items": [
                {"item_id": "candidate-seal", "details": {"kind": "candidate-seal"}},
                {"item_id": "compare", "details": {"kind": "compare"}},
                {
                    "item_id": "assert-SPEC-HDR-005",
                    "details": {"kind": "affected-rule-assertion"},
                },
            ],
        }
        acceptance = {
            "status": "complete",
            "production_state": "accepted_not_activated",
        }
        with (
            mock.patch.object(codex_upgrade, "_require_formal_campaign", return_value=self._manifest()),
            mock.patch.object(codex_upgrade, "_load_stage_result", return_value=acceptance),
        ):
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "acceptance"):
                codex_upgrade._canonical_accept(Path("/unused"), checkpoint)

    def test_intermediate_status_is_success_only_inside_campaign_run(self) -> None:
        """合法停靠点只对父批次成功，直接 CLI 仍以退出码 2 提醒。

        classify 草案的 ``draft`` 是 VC-2 首批的预期终点：0.154 首次真实派发时它曾以
        退出码 2 被父监督器判成动作失败并把账本停线，这里把它固定为合法停靠点；
        候选 seal 第一步的 ``client_checkpoint_created`` 同理（VC-5 前盘点补上）；
        真正的失败状态在父批次内仍是非零。
        """

        with mock.patch.dict(
            os.environ,
            {codex_upgrade.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"},
            clear=True,
        ):
            self.assertEqual(codex_upgrade._campaign_run_aware_exit_code({"status": "failed"}, 2), 2)
            self.assertEqual(codex_upgrade._campaign_run_aware_exit_code({"status": "blocked"}, 2), 2)
        for status in (
            "awaiting_receipts",
            "approval_required",
            "draft",
            "client_checkpoint_created",
        ):
            with self.subTest(status=status):
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        codex_upgrade._campaign_run_aware_exit_code(
                            {"status": status},
                            2,
                        ),
                        2,
                    )
                with mock.patch.dict(
                    os.environ,
                    {codex_upgrade.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"},
                    clear=True,
                ):
                    self.assertEqual(
                        codex_upgrade._campaign_run_aware_exit_code(
                            {"status": status},
                            2,
                        ),
                        0,
                    )

    def test_campaign_bootstrap_and_batch_controls_are_direct_only(self) -> None:
        """预检与批次控制直接运行，Formal plan 只能走原子收口。"""

        arguments = argparse.Namespace(
            campaign_mode="preflight_only",
            target_version="0.154.0",
        )
        for command in (
            "plan",
            "reuse-official-evidence",
            "compile-vc-batch",
            "compile-vc-interrupted-recovery-batch",
        ):
            with self.subTest(command=command), mock.patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                codex_upgrade._reject_unparented_formal_write(
                    arguments,
                    command,
                )
            with self.subTest(command=f"campaign-run:{command}"), mock.patch.dict(
                os.environ,
                {
                    codex_upgrade.codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "引导／批次控制面命令",
                ):
                    codex_upgrade._reject_unparented_formal_write(
                        arguments,
                        command,
                    )

        formal_arguments = argparse.Namespace(
            campaign_mode="formal",
            target_version="0.154.0",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "只能由 codex_upgrade_vc0_closeout.py 原子创建",
            ):
                codex_upgrade._reject_unparented_formal_write(
                    formal_arguments,
                    "plan",
                )

    def test_interrupted_recovery_rejects_owner_nonce_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory).resolve()
            binding = {
                "owner_nonce": "1" * 64,
            }
            with (
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor,
                    "_read_state",
                    return_value={"campaign_id": "campaign-a"},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_read_json",
                    return_value={"manifest": {}},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor,
                    "_recovery_predecessor_from_run",
                    return_value=binding,
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_supervisor,
                    "_audit_command",
                    return_value={"audit_incomplete": False},
                ),
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "owner 不一致",
                ):
                    codex_upgrade._interrupted_recovery_failed_supervisor(
                        run_dir,
                        campaign_id="campaign-a",
                        owner_nonce="2" * 64,
                    )

    def test_interrupted_attempt_close_is_idempotent_without_new_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            attempt_root = (
                campaign_dir / "official" / "attempts" / "attempt-a"
            )
            attempt_root.mkdir(parents=True)
            contract_path = (
                campaign_dir
                / "control"
                / "vc"
                / "recovery-contracts"
                / "0002-vc-1.json"
            )
            self._write(contract_path, {"fixture": True})
            marker = {
                "contract": {
                    "path": contract_path.relative_to(campaign_dir).as_posix(),
                    "sha256": codex_upgrade.file_sha256(contract_path),
                },
                "request_boundary": {
                    "reservation_exists": False,
                    "live_request_count": 0,
                    "scanned_bytes": 0,
                },
            }
            existing = {"interrupted_recovery": marker}
            (attempt_root / "attempt.json").write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_load_capture_attempt",
                    return_value=(attempt_root, existing),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_interrupted_recovery_source_snapshot",
                    side_effect=AssertionError("幂等重放不得重新读取孤儿路径"),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                    side_effect=AssertionError("幂等重放不得重新探测环境"),
                ),
            ):
                self.assertEqual(
                    codex_upgrade._close_interrupted_recovery_attempt(
                        campaign_dir,
                        self._manifest(),
                        contract_path,
                        {"source_attempt": {"attempt_id": "attempt-a"}},
                    ),
                    (attempt_root, existing),
                )

    def test_interrupted_attempt_close_rejects_partial_after_before_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            attempt_root = (
                campaign_dir / "official" / "attempts" / "attempt-a"
            )
            environment_root = attempt_root / "evidence" / "environment"
            self._write(
                environment_root / "before" / "probe-manifest.json",
                {"phase": "before"},
            )
            self._write(
                environment_root / "arm64-before" / "receipt.json",
                {"fixture": True},
            )
            self._write(
                environment_root / "after" / "probe-manifest.json",
                {"partial": True},
            )
            source = {"attempt_id": "attempt-a"}
            active = mock.MagicMock()
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_interrupted_recovery_source_snapshot",
                    return_value=(source, [], {"campaign_lease": {}}),
                ),
                mock.patch.object(codex_upgrade, "_bind_active_lease_attempt"),
                mock.patch.object(codex_upgrade, "_ACTIVE_CAMPAIGN_LEASE", active),
                mock.patch.object(
                    codex_upgrade,
                    "_bind_attempt_deadline_metadata",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "replay",
                    return_value={"continuity_identity_sha256": "1" * 64},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "receipt_equivalence_sha256",
                    # 合成环境收据只有连续性摘要；真实 v8 等价投影由环境收据专项测试覆盖。
                    side_effect=lambda _root, receipt: receipt["continuity_identity_sha256"],
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_probe_capture_environment",
                ) as probe,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "已有不完整 after",
                ):
                    codex_upgrade._close_interrupted_recovery_attempt(
                        campaign_dir,
                        self._manifest(),
                        campaign_dir / "contract.json",
                        {"source_attempt": source},
                    )
            probe.assert_not_called()

    def test_interrupted_attempt_close_rejects_arm64_continuity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            attempt_root = (
                campaign_dir / "official" / "attempts" / "attempt-a"
            )
            evidence_root = attempt_root / "evidence"
            environment_root = evidence_root / "environment"
            self._write(
                environment_root / "before" / "probe-manifest.json",
                {"phase": "before"},
            )
            self._write(
                environment_root / "arm64-before" / "receipt.json",
                {"fixture": True},
            )
            source = {"attempt_id": "attempt-a"}
            active = mock.MagicMock()
            restoration = evidence_root / "receipts" / "restoration-report.json"
            arm64_after = environment_root / "arm64-after" / "receipt.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "_interrupted_recovery_source_snapshot",
                    return_value=(source, [], {"campaign_lease": {}}),
                ),
                mock.patch.object(codex_upgrade, "_bind_active_lease_attempt"),
                mock.patch.object(codex_upgrade, "_ACTIVE_CAMPAIGN_LEASE", active),
                mock.patch.object(
                    codex_upgrade,
                    "_bind_attempt_deadline_metadata",
                    return_value=mock.MagicMock(),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "replay",
                    return_value={"continuity_identity_sha256": "1" * 64},
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_arm64_environment_receipt,
                    "receipt_equivalence_sha256",
                    # 合成环境收据只有连续性摘要；真实 v8 等价投影由环境收据专项测试覆盖。
                    side_effect=lambda _root, receipt: receipt["continuity_identity_sha256"],
                ),
                mock.patch.object(codex_upgrade, "_probe_capture_environment"),
                mock.patch.object(
                    codex_upgrade,
                    "_finalize_attempt_restoration",
                    return_value=(restoration, {}),
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_capture_arm64_environment_receipt",
                    return_value=(
                        arm64_after,
                        {"continuity_identity_sha256": "2" * 64},
                    ),
                ),
                mock.patch.object(codex_upgrade, "_write_capture_attempt") as write,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "ARM64 网络或运行身份漂移",
                ):
                    codex_upgrade._close_interrupted_recovery_attempt(
                        campaign_dir,
                        self._manifest(),
                        campaign_dir / "contract.json",
                        {"source_attempt": source},
                    )
            write.assert_not_called()

    def test_legacy_interrupted_watchdog_binding_replays_real_attempt(self) -> None:
        """真实 write→load 只兼容已封存 v3 的唯一 attempt-relative 路径。"""

        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            campaign_dir.mkdir(mode=0o700)
            manifest = {
                **self._manifest(),
                "baseline_version": "0.151.0",
            }
            self._write(campaign_dir / "campaign.json", manifest)
            attempt_root = campaign_dir / "official" / "attempts" / "attempt-a"
            attempt_root.mkdir(parents=True, mode=0o700)
            identity = {"version": "0.154.0", "binary": "fixture"}
            run_nonce = "1" * 64
            started = (
                datetime.now(timezone.utc) - timedelta(seconds=2)
            ).isoformat().replace("+00:00", "Z")
            execution_sha256 = "2" * 64
            reservation = {
                "schema_version": codex_upgrade.LEGACY_CAPTURE_RESERVATION_SCHEMA,
                "campaign_id": manifest["campaign_id"],
                "campaign_mode": "formal",
                "campaign_purpose": "validation_only",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(
                    campaign_dir / "campaign.json"
                ),
                "phase": "official",
                "candidate_id": None,
                "candidate_purpose": None,
                "attempt_id": "attempt-a",
                "run_nonce": run_nonce,
                "started_at_utc": started,
                "identity_sha256": codex_upgrade._fingerprint(identity),
                "planned_jobs": [
                    {
                        "id": "job-a",
                        "required": True,
                        "execution_sha256": execution_sha256,
                    }
                ],
            }
            reservation["reservation_digest"] = codex_upgrade._fingerprint(
                reservation
            )
            self._write(attempt_root / "reservation.json", reservation)
            result = {
                "id": "job-a",
                "status": "failed",
                "execution_sha256": execution_sha256,
            }
            store = codex_upgrade.incremental_recovery.CheckpointStore(
                attempt_root / "checkpoints"
            )
            checkpoint = store.append(
                {
                    "checkpoint_schema_version": codex_upgrade.JOB_CHECKPOINT_SCHEMA,
                    "campaign_id": manifest["campaign_id"],
                    "phase": "official",
                    "attempt_id": "attempt-a",
                    "run_nonce": run_nonce,
                    "item_id": "job-a",
                    "status": "failed",
                    "result_sha256": codex_upgrade.incremental_recovery.digest(result),
                    "result": result,
                    "previous_checkpoint_sha256": None,
                }
            )
            deadline = codex_upgrade.incremental_recovery.WallClockDeadline(120)
            deadline.phase = "official"
            heartbeat_path = attempt_root / "watchdog-heartbeat.json"
            codex_upgrade._write_attempt_heartbeat(
                heartbeat_path,
                deadline,
                operation="attempt:failed",
                force=True,
                attempt_root=attempt_root,
            )
            contract_path = (
                campaign_dir
                / "control"
                / "vc"
                / "recovery-contracts"
                / "0002-vc-1.json"
            )
            self._write(contract_path, {"fixture": True})
            source = {
                "attempt_id": "attempt-a",
                "run_nonce": run_nonce,
                "identity_sha256": reservation["identity_sha256"],
                "reservation": {
                    "path": "official/attempts/attempt-a/reservation.json",
                    "sha256": codex_upgrade.file_sha256(
                        attempt_root / "reservation.json"
                    ),
                    "reservation_digest": reservation["reservation_digest"],
                },
                "planned_job_ids": ["job-a"],
                "completed_job_ids": [],
                "failed_job_ids": ["job-a"],
                "pending_job_ids": [],
            }
            marker = {
                "contract": {
                    "path": contract_path.relative_to(campaign_dir).as_posix(),
                    "sha256": codex_upgrade.file_sha256(contract_path),
                },
                "request_boundary": {
                    "reservation_exists": False,
                    "live_request_count": 0,
                    "scanned_bytes": 0,
                },
            }
            plan_core = {
                "schema_version": codex_upgrade.incremental_recovery.SCHEMA_VERSION,
                "planned_job_ids": ["job-a"],
                "changed_components": [],
                "affected_job_ids": [],
                "reused_job_ids": [],
                "executed_job_ids": ["job-a"],
                "failed_job_ids": ["job-a"],
                "pending_job_ids": [],
            }
            payload = {
                "campaign_id": manifest["campaign_id"],
                "phase": "official",
                "candidate_id": None,
                "status": "failed",
                "identity": identity,
                "results": [result],
                "interrupted_recovery": marker,
                "incremental_plan": {
                    **plan_core,
                    "plan_sha256": codex_upgrade.incremental_recovery.digest(
                        plan_core
                    ),
                },
                "watchdog": {
                    "schema_version": codex_upgrade.WATCHDOG_HEARTBEAT_SCHEMA,
                    "budget_seconds": 120,
                    "heartbeat_seconds": 5,
                    "elapsed_seconds": deadline.elapsed_seconds,
                    "remaining_seconds": deadline.remaining_seconds,
                    "heartbeat": {
                        # 精确复现 v3 已落盘的错误绑定，不能改写 attempt。
                        "path": "watchdog-heartbeat.json",
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
                    "attempt_id": "attempt-a",
                    "run_nonce": run_nonce,
                    "path": "official/attempts/attempt-a/checkpoints",
                    "record_count": 1,
                    "last_sequence": 1,
                    "last_sha256": checkpoint["checkpoint_sha256"],
                },
                "execution_error": {
                    "type": "KeyboardInterrupt",
                    "message": "父 campaign-run 已中断。",
                },
                "restoration_error": None,
            }
            with (
                mock.patch.object(
                    codex_upgrade,
                    "load_campaign_manifest",
                    return_value=manifest,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_interrupted_recovery_campaign_plan",
                    return_value=(campaign_dir / "plan.json", {}),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_vc_artifacts,
                    "validate_interrupted_recovery_contract",
                    return_value={"source_attempt": source},
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_replay_attempt_evidence_permissions",
                    return_value={},
                ),
            ):
                written = codex_upgrade._write_capture_attempt(
                    campaign_dir,
                    attempt_root,
                    payload,
                )
                loaded_root, loaded = codex_upgrade._load_capture_attempt(
                    campaign_dir,
                    "official",
                    None,
                    "attempt-a",
                    _verified_campaign_manifest=manifest,
                )
            self.assertEqual(loaded_root, attempt_root)
            self.assertEqual(loaded["attempt_digest"], written["attempt_digest"])
            self.assertEqual(
                loaded["watchdog"]["heartbeat"]["path"],
                "watchdog-heartbeat.json",
            )

    def _delivery_fixture(
        self,
        root: Path,
        *,
        purpose: str,
    ) -> tuple[argparse.Namespace, dict[str, object], dict[str, object], dict[str, object], Path]:
        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        self._write(campaign_dir / "campaign.json", {"fixture": True})
        self._mark_implicit_r1(campaign_dir)
        candidate_id = "candidate-a"
        attempt_id = "attempt-a"
        build_path = campaign_dir / "candidates" / candidate_id / "build-receipt.json"
        self._write(build_path, {"fixture": "build"})
        build_binding = {
            "path": build_path.relative_to(campaign_dir).as_posix(),
            "sha256": codex_upgrade.file_sha256(build_path),
            "bytes": build_path.stat().st_size,
        }
        acceptance_path = campaign_dir / "acceptance" / candidate_id / "result.json"
        self._write(acceptance_path, {"fixture": "acceptance"})
        manifest = {
            "campaign_id": "codex-0_154_0-campaign",
            "campaign_mode": "formal",
            "campaign_purpose": purpose,
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
        }
        candidate = {
            "identity": {
                "build_receipt": build_binding,
                "source_tree_sha256": "1" * 64,
            }
        }
        acceptance = {
            "status": "complete",
            "accepted": True,
            "production_state": "accepted_not_activated",
            "candidate_build_receipt": build_binding,
        }
        arguments = argparse.Namespace(
            campaign_dir=campaign_dir,
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            build_receipt=build_path,
            private_archive_receipt=None,
            cleanup_decision_receipt=None,
        )
        return arguments, manifest, candidate, acceptance, acceptance_path

    def _delivery_common_patches(
        self,
        *,
        arguments: argparse.Namespace,
        manifest: dict[str, object],
        candidate: dict[str, object],
        acceptance: dict[str, object],
        acceptance_path: Path,
    ) -> tuple[mock._patch, ...]:
        def load_stage(_campaign: Path, stage: str, *_args: object, **_kwargs: object) -> dict[str, object]:
            return candidate if stage == "capture-candidate" else acceptance

        build_binding = candidate["identity"]["build_receipt"]
        return (
            mock.patch.object(codex_upgrade, "_require_formal_campaign", return_value=manifest),
            mock.patch.object(codex_upgrade, "_load_stage_result", side_effect=load_stage),
            self._active_ledger_patch(),
            mock.patch.object(
                codex_upgrade,
                "_capture_stage_attempt_context",
                return_value=(acceptance_path.parent, {"attempt_id": arguments.attempt_id}),
            ),
            mock.patch.object(
                codex_upgrade,
                "_replay_candidate_build_receipt",
                return_value=({}, build_binding),
            ),
            mock.patch.object(
                codex_upgrade,
                "_stage_path",
                return_value=(acceptance_path.parent, acceptance_path),
            ),
            mock.patch.object(
                codex_upgrade,
                "_replay_vc_completion",
                return_value={"receipt": {}, "checkpoint": {}},
            ),
        )

    def test_validation_only_delivery_closes_vc6_without_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, manifest, candidate, acceptance, acceptance_path = (
                self._delivery_fixture(Path(directory), purpose="validation_only")
            )
            completion = {
                "receipt": {"receipt_digest": "a" * 64},
                "checkpoint": {"checkpoint_sha256": "b" * 64},
            }
            patches = self._delivery_common_patches(
                arguments=arguments,
                manifest=manifest,
                candidate=candidate,
                acceptance=acceptance,
                acceptance_path=acceptance_path,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch.object(
                codex_upgrade,
                "_complete_vc_with_receipt",
                return_value=completion,
            ) as complete:
                result = codex_upgrade.deliver_candidate(arguments)
            self.assertEqual(result["release_state"], "ready_for_operator_release")
            self.assertEqual(result["vc6_status"], "complete")
            self.assertFalse((arguments.campaign_dir / "canonical").exists())
            complete.assert_called_once()

    def _production_checkpoint(
        self,
        arguments: argparse.Namespace,
    ) -> tuple[dict[str, object], Path]:
        checkpoint: dict[str, object] = {
            "checkpoint_sequence": 1,
            "checkpoint_sha256": "c" * 64,
            "phase": "VC-6",
            "campaign": {
                "candidate_id": arguments.candidate_id,
                "attempt_id": arguments.attempt_id,
            },
            "plan": {"execute_item_ids": []},
            "items": [
                {"item_id": "acceptance"},
                {"item_id": "production-activation"},
                {"item_id": "rollback-verification"},
                {"item_id": "retire-0.149.1"},
            ],
        }
        path = (
            arguments.campaign_dir
            / codex_upgrade.CANONICAL_DIRECTORY
            / codex_upgrade.CANONICAL_CHECKPOINT_DIRECTORY
            / "00000001.json"
        )
        self._write(path, checkpoint)
        return checkpoint, path

    def test_production_delivery_requires_archive_second_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, manifest, candidate, acceptance, acceptance_path = (
                self._delivery_fixture(Path(directory), purpose="production_replacement")
            )
            checkpoint, _ = self._production_checkpoint(arguments)
            patches = self._delivery_common_patches(
                arguments=arguments,
                manifest=manifest,
                candidate=candidate,
                acceptance=acceptance,
                acceptance_path=acceptance_path,
            )
            completion = {
                "receipt": {"receipt_digest": "d" * 64},
                "checkpoint": {"checkpoint_sha256": "e" * 64},
            }
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch.object(
                codex_upgrade,
                "_canonical_latest_checkpoint",
                return_value=checkpoint,
            ), mock.patch.object(
                codex_upgrade,
                "_complete_vc_with_receipt",
                return_value=completion,
            ) as complete:
                first = codex_upgrade.deliver_candidate(arguments)
                self.assertEqual(first["vc6_status"], "production_archive_pending")
                complete.assert_not_called()

                archive = arguments.campaign_dir / "control" / "vc" / "archive.json"
                cleanup = arguments.campaign_dir / "control" / "vc" / "cleanup.json"
                self._write(archive, {"fixture": "archive"})
                self._write(cleanup, {"fixture": "cleanup"})
                arguments.private_archive_receipt = archive
                arguments.cleanup_decision_receipt = cleanup
                with mock.patch.object(
                    codex_upgrade,
                    "_replay_campaign_vc_receipt",
                    side_effect=[({}, archive), ({}, cleanup)],
                ):
                    second = codex_upgrade.deliver_candidate(arguments)
            self.assertEqual(second["vc6_status"], "complete")
            complete.assert_called_once()

    def test_partial_archive_arguments_fail_before_delivery_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments, manifest, candidate, acceptance, acceptance_path = (
                self._delivery_fixture(Path(directory), purpose="production_replacement")
            )
            checkpoint, _ = self._production_checkpoint(arguments)
            arguments.private_archive_receipt = (
                arguments.campaign_dir / "control" / "vc" / "archive.json"
            )
            patches = self._delivery_common_patches(
                arguments=arguments,
                manifest=manifest,
                candidate=candidate,
                acceptance=acceptance,
                acceptance_path=acceptance_path,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], mock.patch.object(
                codex_upgrade,
                "_canonical_latest_checkpoint",
                return_value=checkpoint,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "必须同时提供",
                ):
                    codex_upgrade.deliver_candidate(arguments)
            self.assertFalse(
                codex_upgrade._candidate_delivery_receipt_path(
                    arguments.campaign_dir,
                    arguments.candidate_id,
                ).exists()
            )

    # ------------------------------------------------------------------
    # 只读导入 Campaign 的分类差异：VC-1 发现清单与动态差异回到原始目录解析
    # ------------------------------------------------------------------

    def _imported_classification_fixture(self, root: Path) -> tuple[Path, dict[str, object], dict[str, object]]:
        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

        predecessor = root / "predecessor"
        successor = root / "successor"
        for directory in (predecessor / "analysis", predecessor / "official" / "attempts" / "a1" / "finalized", successor / "analysis", successor / "official"):
            directory.mkdir(parents=True, mode=0o700)
        source_diff = {"added": [{"fingerprint": "1" * 64, "kind": "endpoint_literal", "file": "x.rs", "value": "/v"}], "removed": [], "added_count": 1, "removed_count": 0}
        official_diff = {"added": [{"fingerprint": "2" * 64, "kind": "http_request"}], "removed": [], "added_count": 1, "removed_count": 0}
        for directory in (predecessor, successor):
            self._write(directory / "analysis" / "source-diff.json", source_diff)
        source_sha = codex_upgrade.file_sha256(predecessor / "analysis" / "source-diff.json")
        finalized = predecessor / "official" / "attempts" / "a1" / "finalized"
        self._write(finalized / "baseline-to-target-official.json", official_diff)
        official_binding = {"path": "official/attempts/a1/finalized/baseline-to-target-official.json", "sha256": codex_upgrade.file_sha256(finalized / "baseline-to-target-official.json")}
        inventory = artifacts.build_discovery_inventory(
            campaign_id="origin-campaign",
            target_version="0.154.0",
            source_diff=source_diff,
            official_diff=official_diff,
            source_diff_binding={"path": "analysis/source-diff.json", "sha256": source_sha},
            official_diff_binding=official_binding,
            evidence_manifest_binding={"path": "official/attempts/a1/evidence-manifest.json", "sha256": "3" * 64},
        )
        self._write(finalized / "discovery-inventory.json", inventory)
        discovery_binding = {"path": "official/attempts/a1/finalized/discovery-inventory.json", "sha256": codex_upgrade.file_sha256(finalized / "discovery-inventory.json")}
        predecessor_manifest = {
            **self._manifest(),
            "campaign_id": "origin-campaign",
            "analysis": {"source-diff": {"path": "analysis/source-diff.json", "sha256": source_sha}},
        }
        self._write(predecessor / "campaign.json", predecessor_manifest)
        self._write(predecessor / "official" / "result.json", {"status": "complete", "discovery_inventory": discovery_binding, "official_diff": official_binding})
        successor_manifest = {
            **self._manifest(),
            "campaign_id": "successor-campaign",
            "analysis": {"source-diff": {"path": "analysis/source-diff.json", "sha256": source_sha}},
            "predecessor": {
                "campaign_dir": str(predecessor),
                "campaign_id": "origin-campaign",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(predecessor / "campaign.json"),
                "reason": codex_upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
            },
        }
        self._write(successor / "campaign.json", successor_manifest)
        projected_official = {
            "status": "complete",
            "predecessor_import": {"path": "predecessor-import.json", "sha256": "4" * 64},
            "discovery_inventory": discovery_binding,
            "official_diff": official_binding,
        }
        return successor, successor_manifest, projected_official

    def test_imported_campaign_resolves_discovery_inventory_at_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            successor, manifest, official = self._imported_classification_fixture(root)
            source_diff, official_diff = codex_upgrade._classification_differences(successor, manifest, official)
            self.assertEqual(source_diff["added_count"], 1)
            self.assertEqual(official_diff["added"][0]["fingerprint"], "2" * 64)
            # 后继目录本身没有 attempt 文件：解析必须发生在原始目录。
            self.assertFalse((successor / "official" / "attempts").exists())

    def test_imported_campaign_rejects_predecessor_manifest_drift_and_copied_diff_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            successor, manifest, official = self._imported_classification_fixture(root)
            drifted = json.loads(json.dumps(manifest))
            drifted["predecessor"]["campaign_manifest_sha256"] = "5" * 64
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "前序 Campaign 清单摘要漂移"):
                codex_upgrade._classification_differences(successor, drifted, official)
            # 导入复制的 source-diff 与原始不一致（此处伪造清单摘要以模拟复制件漂移）
            copied = json.loads(json.dumps(manifest))
            copied["analysis"]["source-diff"]["sha256"] = "6" * 64
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "计划期分析摘要漂移"):
                codex_upgrade._classification_differences(successor, copied, official)


if __name__ == "__main__":
    unittest.main()
