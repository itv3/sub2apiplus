"""VC 后继批次原子编译派发入口回归测试。"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor


class AtomicDispatchTests(unittest.TestCase):
    def _arguments(self, root: Path) -> argparse.Namespace:
        campaign_dir = root / "campaign"
        state_dir = root / "state"
        campaign_dir.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            state_dir=state_dir,
            phase="VC-1",
            sequence=2,
            predecessor_checkpoint=root / "checkpoint.json",
            action_plan=root / "action-plan.json",
            heartbeat_seconds=1,
            watchdog_timeout_seconds=2.0,
            ledger_interval_seconds=1.0,
        )

    @staticmethod
    def _batch() -> dict[str, object]:
        now = datetime.now(timezone.utc)
        return {
            "must_start_by_utc": (now + timedelta(minutes=1)).isoformat(),
            "original_deadline_at_utc": (now + timedelta(minutes=5)).isoformat(),
        }

    @staticmethod
    def _compiled(arguments: argparse.Namespace) -> dict[str, object]:
        return {
            "status": "complete",
            "campaign_id": "atomic-fixture",
            "phase": arguments.phase,
            "batch_sequence": arguments.sequence,
        }

    @staticmethod
    def _artifact_paths(arguments: argparse.Namespace) -> tuple[Path, Path]:
        name = f"{arguments.sequence:04d}-{arguments.phase.lower()}.json"
        batch = arguments.campaign_dir / "control/vc/batches" / name
        manifest = arguments.campaign_dir / "control/vc/run-manifests" / name
        for path in (batch, manifest):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            path.write_text("{}\n", encoding="utf-8")
            path.chmod(0o600)
        return batch, manifest

    def test_success_keeps_single_state_lock_through_compile_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._arguments(Path(directory))
            observed = {"locked": False}

            def run_locked(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object]]:
                with self.assertRaises(supervisor.SupervisorError):
                    supervisor._campaign_run_lock(arguments.state_dir)
                observed["locked"] = True
                return 0, {
                    "status": "stopped",
                    "run_dir": str(arguments.state_dir / "run-fixture"),
                }

            with (
                mock.patch.object(
                    codex_upgrade,
                    "compile_vc_batch",
                    return_value=self._compiled(arguments),
                ),
                mock.patch.object(codex_upgrade, "_read_json", return_value=self._batch()),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_vc_artifacts,
                    "validate_vc_batch",
                    side_effect=lambda value: value,
                ),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_manifest",
                    return_value={"schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA},
                ),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_locked",
                    side_effect=run_locked,
                ),
            ):
                result, returncode = codex_upgrade.compile_and_run_vc_batch(arguments)

            self.assertEqual(returncode, 0)
            self.assertTrue(observed["locked"])
            self.assertEqual(result["status"], "stopped")
            self.assertIsNone(result["predispatch_stop"])

    def test_failure_before_parent_run_writes_generic_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._arguments(Path(directory))
            self._artifact_paths(arguments)
            receipt_path = arguments.campaign_dir / "control/vc/predispatch-stops/0002-vc-1.json"
            with (
                mock.patch.object(
                    codex_upgrade,
                    "compile_vc_batch",
                    return_value=self._compiled(arguments),
                ),
                mock.patch.object(codex_upgrade, "_read_json", return_value=self._batch()),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_vc_artifacts,
                    "validate_vc_batch",
                    side_effect=lambda value: value,
                ),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_manifest",
                    side_effect=supervisor.SupervisorError("fixture failure"),
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_predispatch_stop,
                    "_record_locked",
                    return_value=(receipt_path, {"status": "stopped"}),
                ) as recorder,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "已封存通用停线收据",
                ):
                    codex_upgrade.compile_and_run_vc_batch(arguments)

            recorder.assert_called_once()
            self.assertEqual(
                recorder.call_args.kwargs["failure_kind"],
                "dispatch-before-parent-run",
            )

    def test_partial_compile_rebuilds_manifest_before_generic_stop(self) -> None:
        """batch 单独落盘后必须补齐确定性 manifest，再封存通用停线。"""

        with tempfile.TemporaryDirectory() as directory:
            arguments = self._arguments(Path(directory))
            batch_path, manifest_path = self._artifact_paths(arguments)
            manifest_path.unlink()
            batch = {
                **self._batch(),
                "campaign_id": "atomic-fixture",
                "campaign_plan_sha256": "1" * 64,
                "batch_id": "vc-1-0002",
                "sequence": 2,
                "batch_sha256": "2" * 64,
                "phase": "VC-1",
                "predecessor_checkpoint": {
                    "path": "control/vc/vc-0-checkpoint.json",
                    "sha256": "3" * 64,
                    "phase": "VC-0",
                    "checkpoint_sha256": "4" * 64,
                },
                "actions": [],
                "execute_item_ids": [],
                "reuse_item_ids": [],
            }
            receipt_path = (
                arguments.campaign_dir
                / "control/vc/predispatch-stops/0002-vc-1.json"
            )

            def write_manifest(path: Path, _payload: object) -> None:
                self.assertEqual(path, manifest_path)
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.parent.chmod(0o700)
                path.write_text("{}\n", encoding="utf-8")
                path.chmod(0o600)

            with (
                mock.patch.object(
                    codex_upgrade,
                    "compile_vc_batch",
                    side_effect=codex_upgrade.ConfigurationError("partial compile"),
                ),
                mock.patch.object(codex_upgrade, "_read_json", return_value=batch),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_vc_artifacts,
                    "validate_vc_batch",
                    side_effect=lambda value: value,
                ),
                mock.patch.object(
                    codex_upgrade,
                    "_secure_write_json_once",
                    side_effect=write_manifest,
                ) as writer,
                mock.patch.object(
                    codex_upgrade.codex_upgrade_predispatch_stop,
                    "_record_locked",
                    return_value=(receipt_path, {"status": "stopped"}),
                ) as recorder,
            ):
                with self.assertRaisesRegex(
                    codex_upgrade.ConfigurationError,
                    "已封存通用停线收据",
                ):
                    codex_upgrade.compile_and_run_vc_batch(arguments)

            writer.assert_called_once()
            self.assertTrue(manifest_path.is_file())
            self.assertEqual(
                recorder.call_args.kwargs["failure_kind"],
                "compile-failed-after-artifact-write",
            )

    def test_failure_after_parent_run_does_not_claim_predispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arguments = self._arguments(Path(directory))

            def run_locked(*_args: object, **_kwargs: object) -> tuple[int, dict[str, object]]:
                (arguments.state_dir / "run-created").mkdir(mode=0o700)
                raise supervisor.SupervisorError("parent failure")

            with (
                mock.patch.object(
                    codex_upgrade,
                    "compile_vc_batch",
                    return_value=self._compiled(arguments),
                ),
                mock.patch.object(codex_upgrade, "_read_json", return_value=self._batch()),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_vc_artifacts,
                    "validate_vc_batch",
                    side_effect=lambda value: value,
                ),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_manifest",
                    return_value={"schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA},
                ),
                mock.patch.object(
                    supervisor,
                    "_campaign_run_locked",
                    side_effect=run_locked,
                ),
                mock.patch.object(
                    codex_upgrade.codex_upgrade_predispatch_stop,
                    "_record_locked",
                ) as recorder,
            ):
                with self.assertRaisesRegex(supervisor.SupervisorError, "parent failure"):
                    codex_upgrade.compile_and_run_vc_batch(arguments)
            recorder.assert_not_called()

    def test_expired_windows_are_classified_before_parent_run(self) -> None:
        """启动窗口和总 deadline 必须生成不同的通用停线原因。"""

        now = datetime.now(timezone.utc)
        cases = (
            (
                "start-window-expired",
                {
                    "must_start_by_utc": (now - timedelta(minutes=1)).isoformat(),
                    "original_deadline_at_utc": (now + timedelta(minutes=1)).isoformat(),
                },
            ),
            (
                "deadline-expired",
                {
                    "must_start_by_utc": (now - timedelta(minutes=2)).isoformat(),
                    "original_deadline_at_utc": (now - timedelta(minutes=1)).isoformat(),
                },
            ),
        )
        for expected_kind, batch in cases:
            with self.subTest(expected_kind=expected_kind):
                with tempfile.TemporaryDirectory() as directory:
                    arguments = self._arguments(Path(directory))
                    self._artifact_paths(arguments)
                    receipt_path = (
                        arguments.campaign_dir
                        / "control/vc/predispatch-stops/0002-vc-1.json"
                    )
                    with (
                        mock.patch.object(
                            codex_upgrade,
                            "compile_vc_batch",
                            return_value=self._compiled(arguments),
                        ),
                        mock.patch.object(codex_upgrade, "_read_json", return_value=batch),
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_vc_artifacts,
                            "validate_vc_batch",
                            side_effect=lambda value: value,
                        ),
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_predispatch_stop,
                            "_record_locked",
                            return_value=(receipt_path, {"status": "stopped"}),
                        ) as recorder,
                    ):
                        with self.assertRaisesRegex(
                            codex_upgrade.ConfigurationError,
                            "已封存通用停线收据",
                        ):
                            codex_upgrade.compile_and_run_vc_batch(arguments)

                    self.assertEqual(
                        recorder.call_args.kwargs["failure_kind"],
                        expected_kind,
                    )

    def test_interrupts_keep_original_exit_semantics_after_stop_receipt(self) -> None:
        """写入停线收据后不得把人工中断改写成普通配置错误。"""

        cases = (
            (KeyboardInterrupt(), KeyboardInterrupt, None),
            (SystemExit(23), SystemExit, 23),
        )
        for interruption, expected_type, expected_code in cases:
            with self.subTest(expected_type=expected_type.__name__):
                with tempfile.TemporaryDirectory() as directory:
                    arguments = self._arguments(Path(directory))
                    self._artifact_paths(arguments)
                    receipt_path = (
                        arguments.campaign_dir
                        / "control/vc/predispatch-stops/0002-vc-1.json"
                    )
                    with (
                        mock.patch.object(
                            codex_upgrade,
                            "compile_vc_batch",
                            return_value=self._compiled(arguments),
                        ),
                        mock.patch.object(
                            codex_upgrade,
                            "_read_json",
                            return_value=self._batch(),
                        ),
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_vc_artifacts,
                            "validate_vc_batch",
                            side_effect=lambda value: value,
                        ),
                        mock.patch.object(
                            supervisor,
                            "_campaign_run_manifest",
                            side_effect=interruption,
                        ),
                        mock.patch.object(
                            codex_upgrade.codex_upgrade_predispatch_stop,
                            "_record_locked",
                            return_value=(receipt_path, {"status": "stopped"}),
                        ) as recorder,
                    ):
                        with self.assertRaises(expected_type) as raised:
                            codex_upgrade.compile_and_run_vc_batch(arguments)

                    self.assertEqual(
                        recorder.call_args.kwargs["failure_kind"],
                        "interrupted-before-parent-run",
                    )
                    if expected_code is not None:
                        self.assertEqual(raised.exception.code, expected_code)

    def test_formal_cli_rejects_compile_only_and_allows_atomic_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            campaign_dir = root / "campaign"
            campaign_dir.mkdir(mode=0o700)
            campaign_path = campaign_dir / "campaign.json"
            campaign_path.write_text(
                json.dumps(
                    {
                        "campaign_mode": "formal",
                        "target_version": "0.154.0",
                    }
                ),
                encoding="utf-8",
            )
            campaign_path.chmod(0o600)
            arguments = argparse.Namespace(campaign_dir=campaign_dir)
            with self.assertRaisesRegex(
                codex_upgrade.ConfigurationError,
                "compile-and-run-vc-batch",
            ):
                codex_upgrade._reject_unparented_formal_write(
                    arguments,
                    "compile-vc-batch",
                )
            codex_upgrade._reject_unparented_formal_write(
                arguments,
                "compile-and-run-vc-batch",
            )

    def test_atomic_cli_uses_campaign_run_as_its_only_supervisor(self) -> None:
        """原子入口不能再被外层 CampaignLease 和相对墙钟预算包裹。"""

        arguments = codex_upgrade._build_parser().parse_args(
            [
                "compile-and-run-vc-batch",
                "--campaign-dir",
                "/tmp/campaign",
                "--state-dir",
                "/tmp/state",
                "--phase",
                "VC-1",
                "--sequence",
                "2",
                "--predecessor-checkpoint",
                "/tmp/checkpoint.json",
                "--action-plan",
                "/tmp/action-plan.json",
            ]
        )

        self.assertIsNone(codex_upgrade._mutable_command_coordinates(arguments))
        self.assertFalse(hasattr(arguments, "max_wall_seconds"))
        self.assertIsNone(arguments.heartbeat_seconds)


if __name__ == "__main__":
    unittest.main()
