"""VC-1 权限别名预派发失败封口工具回归测试。"""

from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade_vc1_permission_alias_predispatch_closeout as closeout,
)


class PermissionAliasPredispatchCloseoutTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _fixture(self, root: Path) -> dict[str, object]:
        """建立能真实重放旧 helper 误拒绝的最小现场。"""

        data_root = root / "data"
        campaign_dir = data_root / "evidence/campaigns" / closeout.CAMPAIGN_ID
        attempt_path = (
            campaign_dir
            / "official/attempts"
            / closeout.ATTEMPT_ID
            / "attempt.json"
        )
        self._write_json(attempt_path, {"fixture": True})
        state_dir = data_root / "control" / f"{closeout.CAMPAIGN_ID}-supervisor"
        state_dir.mkdir(parents=True, mode=0o700)
        state_dir.chmod(0o700)
        lock_path = state_dir / ".campaign-run.lock"
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)

        run_names = tuple(f"run-{value * 64}" for value in ("1", "2", "3"))
        for sequence, run_name in enumerate(run_names, 1):
            run_dir = state_dir / run_name
            run_dir.mkdir(mode=0o700)
            run_dir.chmod(0o700)
            self._write_json(
                run_dir / "campaign-run-manifest.json",
                {
                    "manifest": {
                        "campaign_id": closeout.CAMPAIGN_ID,
                        "batch_sequence": sequence,
                    }
                },
            )

        action_input_dir = (
            data_root / "control" / f"{closeout.CAMPAIGN_ID}-action-inputs"
        )
        action_input_dir.mkdir(parents=True, mode=0o700)
        action_input_dir.chmod(0o700)
        sequence_four_batch_path = (
            campaign_dir / "control/vc/batches/0004-vc-1.json"
        )
        sequence_four_manifest_path = (
            campaign_dir / "control/vc/run-manifests/0004-vc-1.json"
        )
        sequence_four_action_plan_path = (
            action_input_dir / "vc1-sequence4-action-plan.json"
        )
        sequence_four_batch_sha256 = "b" * 64
        compiled_at = "2000-01-01T00:00:00+00:00"
        must_start_by = "2000-01-01T00:01:00+00:00"
        self._write_json(
            sequence_four_batch_path,
            {
                "sequence": 4,
                "batch_id": "vc-1-0004",
                "batch_sha256": sequence_four_batch_sha256,
                "compiled_at_utc": compiled_at,
                "must_start_by_utc": must_start_by,
            },
        )
        self._write_json(
            sequence_four_manifest_path,
            {
                "schema_version": "codex-upgrade-campaign-run/v2",
                "campaign_id": closeout.CAMPAIGN_ID,
                "batch_sequence": 4,
                "batch_sha256": sequence_four_batch_sha256,
                "actions": [
                    {"action_id": "harden-official-evidence-permissions-via-alias"},
                    {"action_id": "seal-official-preview"},
                ],
            },
        )
        self._write_json(sequence_four_action_plan_path, {"fixture": True})

        failed_deployment_path = (
            data_root / "control/codex-0154-supervisor-enable-failed.json"
        )
        failed_tool_files_sha256 = "c" * 64
        failed_supervisor_sha256 = "d" * 64
        self._write_json(
            failed_deployment_path,
            {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "tool_files_sha256": failed_tool_files_sha256,
                "supervisor_sha256": failed_supervisor_sha256,
            },
        )

        tool_root = data_root / "tools/official_client_capture"
        tool_root.mkdir(parents=True, mode=0o700)
        tool_root.chmod(0o700)
        supervisor_path = tool_root / "codex_upgrade_supervisor.py"
        supervisor_path.write_text("# 监督器夹具\n", encoding="utf-8")
        supervisor_path.chmod(0o600)
        tool_path = (
            tool_root
            / "codex_upgrade_vc1_permission_alias_predispatch_closeout.py"
        )
        tool_path.write_text("# 预派发封口夹具\n", encoding="utf-8")
        tool_path.chmod(0o600)
        helper_path = tool_root / "codex_upgrade_vc1_permission_alias_closeout.py"
        helper_path.write_text("# 当前 helper 夹具\n", encoding="utf-8")
        helper_path.chmod(0o600)

        rollback = data_root / "control/managed-tools-backup-before-fixture"
        rollback.mkdir(mode=0o700)
        rollback.chmod(0o700)
        historical_helper_path = (
            rollback / "codex_upgrade_vc1_permission_alias_closeout.py"
        )
        historical_helper_path.write_text(
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class Snapshot:\n"
            "    value: int = 1\n"
            "class PermissionAliasCloseoutError(RuntimeError):\n"
            "    pass\n"
            "def inspect_permission_boundary(**kwargs):\n"
            f"    raise PermissionAliasCloseoutError({closeout.EXPECTED_OLD_ERROR!r})\n",
            encoding="utf-8",
        )
        historical_helper_path.chmod(0o600)

        current_deployment_path = (
            data_root / "control/codex-0154-supervisor-enable-current.json"
        )
        current_tool_files_sha256 = "e" * 64
        self._write_json(
            current_deployment_path,
            {
                "schema_version": "codex-arm64-supervisor-enable/v1",
                "status": "passed",
                "architecture": "aarch64",
                "production_tool_root": str(tool_root),
                "tool_files_sha256": current_tool_files_sha256,
                "supervisor_sha256": self._sha256(supervisor_path),
                "rollback_backup": str(rollback),
            },
        )
        receipt_path = action_input_dir / "sequence4-predispatch-closeout-receipt.json"
        boundary = mock.Mock(
            entries=(object(), object()),
            gaps=(object(),),
            gap_sha256="f" * 64,
            boundary_sha256="a" * 64,
        )
        bindings = {
            "HOST_DATA_ROOT": data_root,
            "CAMPAIGN_DIR": campaign_dir,
            "ATTEMPT_PATH": attempt_path,
            "STATE_DIR": state_dir,
            "ACTION_INPUT_DIR": action_input_dir,
            "RECEIPT_PATH": receipt_path,
            "TOOL_PATH": tool_path,
            "SUPERVISOR_PATH": supervisor_path,
            "CURRENT_HELPER_PATH": helper_path,
            "SEQUENCE4_BATCH_PATH": sequence_four_batch_path,
            "SEQUENCE4_MANIFEST_PATH": sequence_four_manifest_path,
            "SEQUENCE4_ACTION_PLAN_PATH": sequence_four_action_plan_path,
            "FAILED_DEPLOYMENT_PATH": failed_deployment_path,
            "SEQUENCE4_BATCH_FILE_SHA256": self._sha256(sequence_four_batch_path),
            "SEQUENCE4_BATCH_SHA256": sequence_four_batch_sha256,
            "SEQUENCE4_MANIFEST_FILE_SHA256": self._sha256(
                sequence_four_manifest_path
            ),
            "SEQUENCE4_ACTION_PLAN_SHA256": self._sha256(
                sequence_four_action_plan_path
            ),
            "FAILED_DEPLOYMENT_SHA256": self._sha256(failed_deployment_path),
            "FAILED_TOOL_FILES_SHA256": failed_tool_files_sha256,
            "FAILED_SUPERVISOR_SHA256": failed_supervisor_sha256,
            "FAILED_HELPER_SHA256": self._sha256(historical_helper_path),
            "HISTORICAL_HELPER_DEPLOYMENT_PATH": current_deployment_path,
            "HISTORICAL_HELPER_DEPLOYMENT_SHA256": self._sha256(
                current_deployment_path
            ),
            "HISTORICAL_HELPER_ROLLBACK_PATH": rollback,
            "SEQUENCE4_COMPILED_AT_UTC": compiled_at,
            "SEQUENCE4_MUST_START_BY_UTC": must_start_by,
            "EXPECTED_RUN_NAMES": run_names,
        }
        return {
            "bindings": bindings,
            "boundary": boundary,
            "state_dir": state_dir,
            "receipt_path": receipt_path,
            "current_deployment_path": current_deployment_path,
            "current_deployment_sha256": self._sha256(current_deployment_path),
            "current_tool_files_sha256": current_tool_files_sha256,
            "tool_sha256": self._sha256(tool_path),
        }

    def test_closeout_replays_old_error_and_writes_zero_request_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory).resolve())
            with (
                mock.patch.multiple(closeout, **fixture["bindings"]),
                mock.patch.object(
                    closeout.permission_closeout,
                    "inspect_permission_boundary",
                    return_value=fixture["boundary"],
                ),
            ):
                receipt = closeout.close_predispatch(
                    current_deployment_receipt=fixture["current_deployment_path"],
                    current_deployment_receipt_sha256=fixture[
                        "current_deployment_sha256"
                    ],
                    tool_files_sha256=fixture["current_tool_files_sha256"],
                    self_sha256=fixture["tool_sha256"],
                    receipt_path=fixture["receipt_path"],
                )
                original = Path(fixture["receipt_path"]).read_bytes()
                with self.assertRaises(FileExistsError):
                    closeout.close_predispatch(
                        current_deployment_receipt=fixture[
                            "current_deployment_path"
                        ],
                        current_deployment_receipt_sha256=fixture[
                            "current_deployment_sha256"
                        ],
                        tool_files_sha256=fixture["current_tool_files_sha256"],
                        self_sha256=fixture["tool_sha256"],
                        receipt_path=fixture["receipt_path"],
                    )
            self.assertEqual(receipt["deterministic_error"], closeout.EXPECTED_OLD_ERROR)
            self.assertFalse(receipt["source_run_created"])
            self.assertEqual(receipt["scanned_bytes"], 0)
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertEqual(
                stat.S_IMODE(Path(fixture["receipt_path"]).stat().st_mode),
                0o600,
            )
            self.assertEqual(Path(fixture["receipt_path"]).read_bytes(), original)

    def test_closeout_rejects_any_sequence_four_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = self._fixture(Path(directory).resolve())
            unexpected = Path(fixture["state_dir"]) / f"run-{'4' * 64}"
            unexpected.mkdir(mode=0o700)
            unexpected.chmod(0o700)
            with (
                mock.patch.multiple(closeout, **fixture["bindings"]),
                mock.patch.object(
                    closeout.permission_closeout,
                    "inspect_permission_boundary",
                    return_value=fixture["boundary"],
                ),
            ):
                with self.assertRaisesRegex(
                    closeout.PredispatchCloseoutError,
                    "历史不再精确为 sequence 1～3",
                ):
                    closeout.close_predispatch(
                        current_deployment_receipt=fixture[
                            "current_deployment_path"
                        ],
                        current_deployment_receipt_sha256=fixture[
                            "current_deployment_sha256"
                        ],
                        tool_files_sha256=fixture["current_tool_files_sha256"],
                        self_sha256=fixture["tool_sha256"],
                        receipt_path=fixture["receipt_path"],
                    )


if __name__ == "__main__":
    unittest.main()
