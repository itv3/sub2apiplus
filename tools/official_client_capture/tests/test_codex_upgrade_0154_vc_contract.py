"""Codex 0.154.0 完整 VC 制品链的入口与失败关闭测试。"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
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
            arguments = self._candidate_arguments(
                campaign_dir,
                build_receipt=None,
            )
            with (
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
            arguments = self._candidate_arguments(
                campaign_dir,
                build_receipt=receipt_path,
            )
            with (
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
        """合法停靠点只对父批次成功，直接 CLI 仍以退出码 2 提醒。"""

        for status in ("awaiting_receipts", "approval_required"):
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
        """Campaign 引导与批次编译必须直接运行，不能成为队列动作。"""

        arguments = argparse.Namespace()
        for command in (
            "plan",
            "reuse-official-evidence",
            "compile-vc-batch",
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

    def _delivery_fixture(
        self,
        root: Path,
        *,
        purpose: str,
    ) -> tuple[argparse.Namespace, dict[str, object], dict[str, object], dict[str, object], Path]:
        campaign_dir = root / "campaign"
        campaign_dir.mkdir(mode=0o700)
        self._write(campaign_dir / "campaign.json", {"fixture": True})
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
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], mock.patch.object(
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
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], mock.patch.object(
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
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], mock.patch.object(
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


if __name__ == "__main__":
    unittest.main()
