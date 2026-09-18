"""Candidate seal 隔离预演：动作归一化、收据生成、门禁校验与 driver 执行。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade_seal_rehearsal as rehearsal
from tools.official_client_capture import codex_upgrade_supervisor as supervisor


TOOL_IDENTITY = {
    "files_sha256": "1" * 64,
    "policy_version": 6,
    "policy_sha256": "2" * 64,
    "wire_producer_sha256": "3" * 64,
    "evidence_semantics_sha256": "4" * 64,
    "control_sha256": "5" * 64,
}


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _seal_actions(campaign_dir: Path, attempt_id: str, *, approve_sha: str = "f" * 64) -> list[dict[str, object]]:
    """与正式 seal 批次同构的四个动作：checkpoint（live）、assertion、preview、approve。"""

    base = [
        sys.executable,
        "/tmp/codex_upgrade.py",
        "capture-candidate",
        "seal",
        "--campaign-dir",
        str(campaign_dir),
        "--candidate-id",
        "cand-1",
        "--attempt-id",
        attempt_id,
    ]
    return [
        {
            "action_id": "candidate-seal-checkpoint",
            "operation": "VC-5:candidate-seal-checkpoint",
            "timeout_seconds": 300,
            "command": list(base),
            "item_ids": ["candidate-seal"],
        },
        {
            "action_id": "prepare-candidate-assertion-bundle",
            "operation": "VC-5:prepare-candidate-assertion-bundle",
            "timeout_seconds": 600,
            "command": [
                "/usr/bin/env",
                f"CAMPAIGN_DIR={campaign_dir}",
                f"ATTEMPT_ID={attempt_id}",
                "SIDE=candidate",
                "CANDIDATE_ID=cand-1",
                "CANDIDATE_SOURCE_ROOT=/tmp/candidate-source",
                "/usr/bin/bash",
                "/tmp/prepare_assertion_bundle.sh",
            ],
            "item_ids": ["candidate-seal"],
        },
        {
            "action_id": "seal-candidate-preview",
            "operation": "VC-5:seal-candidate-preview",
            "timeout_seconds": 1500,
            "command": [*base, "--capture-manifest", "/tmp/capture-manifest.json"],
            "item_ids": ["candidate-seal"],
        },
        {
            "action_id": "seal-candidate-approve",
            "operation": "VC-5:seal-candidate-approve",
            "timeout_seconds": 600,
            "command": [*base, "--approve-seal-sha256", approve_sha],
            "item_ids": ["candidate-seal"],
        },
    ]


class SealRehearsalTests(unittest.TestCase):
    def _campaign(self, root: Path) -> tuple[Path, str]:
        campaign_dir = root / "data" / "evidence" / "campaigns" / "campaign-seal"
        attempt_id = "20260917T190726Z-96ecf4e9a4848948"
        _write_json(campaign_dir / "campaign.json", {"campaign_id": "campaign-seal"})
        _write_json(
            campaign_dir / "candidates" / "cand-1" / "attempts" / attempt_id / "attempt.json",
            {"attempt_id": attempt_id, "candidate_id": "cand-1", "status": "awaiting_receipts"},
        )
        (campaign_dir / "ledger").mkdir(mode=0o700)
        _write_json(campaign_dir / "ledger" / "plan.json", {"campaign_id": "campaign-seal"})
        return campaign_dir, attempt_id

    def test_normalize_skips_live_checkpoint_and_masks_review_sha(self) -> None:
        actions = _seal_actions(Path("/campaign"), "attempt-1", approve_sha="a" * 64)
        normalized = rehearsal.normalize_rehearsal_actions(actions)
        self.assertEqual(
            [item["action_id"] for item in normalized],
            ["prepare-candidate-assertion-bundle", "seal-candidate-preview", "seal-candidate-approve"],
        )
        approve = normalized[-1]
        self.assertTrue(approve["approve"])
        self.assertIn(rehearsal.REVIEW_PLACEHOLDER, approve["normalized_command"])
        self.assertNotIn("a" * 64, approve["normalized_command"])
        # 不同的 review 摘要得到同一序列摘要；动作内容漂移则不同。
        other = rehearsal.normalize_rehearsal_actions(
            _seal_actions(Path("/campaign"), "attempt-1", approve_sha="b" * 64)
        )
        self.assertEqual(rehearsal.actions_sha256(normalized), rehearsal.actions_sha256(other))
        drifted = _seal_actions(Path("/campaign"), "attempt-1")
        drifted[1]["command"][-1] = "/tmp/other.sh"
        self.assertNotEqual(
            rehearsal.actions_sha256(normalized),
            rehearsal.actions_sha256(rehearsal.normalize_rehearsal_actions(drifted)),
        )
        # Candidate Job 本身不属于零请求后处理闭集。
        illegal = _seal_actions(Path("/campaign"), "attempt-1")
        illegal[1]["item_ids"] = ["candidate-frozen-core"]
        with self.assertRaisesRegex(rehearsal.SealRehearsalError, "零请求后处理阶段项闭集"):
            rehearsal.normalize_rehearsal_actions(illegal)

    def test_rehearse_writes_passed_receipt_and_gate_accepts_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, attempt_id = self._campaign(root)
            actions = _seal_actions(campaign_dir, attempt_id)

            def fake_driver(arguments: dict[str, object]) -> dict[str, object]:
                self.assertEqual(arguments["overlay_roots"], [str(root / "data"), str(root / "alias")])
                self.assertEqual([item["action_id"] for item in arguments["actions"]][0], "prepare-candidate-assertion-bundle")
                return {
                    "schema_version": rehearsal.DRIVER_SCHEMA_VERSION,
                    "status": "passed",
                    "results": [
                        {"action_id": item["action_id"], "status": "passed", "returncode": 0, "duration_seconds": 1.0, "stdout_tail": "", "stderr_tail": "", "upper_inventory": [{"path": "x", "size": 1, "mode": 0o600, "kind": "file", "sha256": "0" * 64}]}
                        for item in arguments["actions"]
                    ],
                    "final_status": {"returncode": 0, "status": "candidate_sealed", "next_command": "compare", "stderr_tail": ""},
                    "upper_inventory": [],
                }

            result = rehearsal.rehearse(
                campaign_dir=campaign_dir,
                candidate_id="cand-1",
                attempt_id=attempt_id,
                actions=actions,
                data_root=root / "data",
                alias_roots=[root / "alias"],
                upper_root=root / "upper",
                status_command=["status"],
                tool_identity=TOOL_IDENTITY,
                driver_runner=fake_driver,
            )
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["lower_unchanged"])
            self.assertEqual(result["final_status"]["status"], "candidate_sealed")
            self.assertEqual(result["skipped_action_ids"], ["candidate-seal-checkpoint"])
            receipt_path = Path(result["receipt_path"])
            self.assertTrue(receipt_path.is_file())
            self.assertEqual(receipt_path.parent, campaign_dir / "control" / "seal-rehearsal" / attempt_id)
            stored = rehearsal.load_receipt(receipt_path)
            self.assertEqual(stored["actions_sha256"], result["actions_sha256"])
            self.assertEqual(stored["results"][0]["upper_inventory_count"], 1)

            # 门禁：同一批次（review 摘要不同也算同一批次）通过。
            verified = rehearsal.verify_rehearsal_for_batch(
                campaign_dir,
                campaign_id="campaign-seal",
                candidate_id="cand-1",
                attempt_id=attempt_id,
                actions=_seal_actions(campaign_dir, attempt_id, approve_sha="e" * 64),
                tool_identity=TOOL_IDENTITY,
            )
            self.assertEqual(verified["path"], str(receipt_path))
            # 工具身份变化、动作漂移、attempt 变化、过期各自拒绝。
            with self.assertRaisesRegex(rehearsal.SealRehearsalError, "control_sha256"):
                rehearsal.verify_rehearsal_for_batch(
                    campaign_dir,
                    campaign_id="campaign-seal",
                    candidate_id="cand-1",
                    attempt_id=attempt_id,
                    actions=actions,
                    tool_identity={**TOOL_IDENTITY, "control_sha256": "9" * 64},
                )
            drifted = _seal_actions(campaign_dir, attempt_id)
            drifted[2]["command"].append("--extra")
            with self.assertRaisesRegex(rehearsal.SealRehearsalError, "动作序列与预演不同"):
                rehearsal.verify_rehearsal_for_batch(
                    campaign_dir,
                    campaign_id="campaign-seal",
                    candidate_id="cand-1",
                    attempt_id=attempt_id,
                    actions=drifted,
                    tool_identity=TOOL_IDENTITY,
                )
            expired = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            with self.assertRaisesRegex(rehearsal.SealRehearsalError, "有效期"):
                rehearsal.verify_rehearsal_for_batch(
                    campaign_dir,
                    campaign_id="campaign-seal",
                    candidate_id="cand-1",
                    attempt_id=attempt_id,
                    actions=actions,
                    tool_identity=TOOL_IDENTITY,
                    now=expired,
                )
            attempt_path = campaign_dir / "candidates" / "cand-1" / "attempts" / attempt_id / "attempt.json"
            _write_json(attempt_path, {"attempt_id": attempt_id, "candidate_id": "cand-1", "status": "awaiting_receipts", "drift": 1})
            with self.assertRaisesRegex(rehearsal.SealRehearsalError, "attempt.json 摘要已变化"):
                rehearsal.verify_rehearsal_for_batch(
                    campaign_dir,
                    campaign_id="campaign-seal",
                    candidate_id="cand-1",
                    attempt_id=attempt_id,
                    actions=actions,
                    tool_identity=TOOL_IDENTITY,
                )

    def test_rehearse_marks_failed_when_driver_fails_or_lower_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, attempt_id = self._campaign(root)
            actions = _seal_actions(campaign_dir, attempt_id)

            def failing_driver(arguments: dict[str, object]) -> dict[str, object]:
                return {
                    "schema_version": rehearsal.DRIVER_SCHEMA_VERSION,
                    "status": "failed",
                    "results": [{"action_id": "prepare-candidate-assertion-bundle", "status": "failed", "returncode": 1, "duration_seconds": 0.3, "stdout_tail": "", "stderr_tail": "候选证据根缺少 candidate-go-test.jsonl", "upper_inventory": []}],
                    "final_status": None,
                    "upper_inventory": [],
                }

            failed = rehearsal.rehearse(
                campaign_dir=campaign_dir, candidate_id="cand-1", attempt_id=attempt_id, actions=actions,
                data_root=root / "data", alias_roots=[], upper_root=root / "upper", status_command=["status"],
                tool_identity=TOOL_IDENTITY, driver_runner=failing_driver,
            )
            self.assertEqual(failed["status"], "failed")
            with self.assertRaisesRegex(rehearsal.SealRehearsalError, "status 不是 passed"):
                rehearsal.verify_rehearsal_for_batch(
                    campaign_dir, campaign_id="campaign-seal", candidate_id="cand-1", attempt_id=attempt_id,
                    actions=actions, tool_identity=TOOL_IDENTITY,
                )

            def leaking_driver(arguments: dict[str, object]) -> dict[str, object]:
                # 隔离失效：driver 直接写进了正式 attempt 目录。
                leak = campaign_dir / "candidates" / "cand-1" / "attempts" / attempt_id / "seal-draft.json"
                _write_json(leak, {"leak": True})
                return {
                    "schema_version": rehearsal.DRIVER_SCHEMA_VERSION,
                    "status": "passed",
                    "results": [],
                    "final_status": {"returncode": 0, "status": "candidate_sealed", "next_command": "compare", "stderr_tail": ""},
                    "upper_inventory": [],
                }

            leaked = rehearsal.rehearse(
                campaign_dir=campaign_dir, candidate_id="cand-1", attempt_id=attempt_id, actions=actions,
                data_root=root / "data", alias_roots=[], upper_root=root / "upper", status_command=["status"],
                tool_identity=TOOL_IDENTITY, driver_runner=leaking_driver,
            )
            self.assertEqual(leaked["status"], "failed")
            self.assertFalse(leaked["lower_unchanged"])

    def test_driver_executes_actions_and_substitutes_review_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            attempt_root = root / "attempt"
            attempt_root.mkdir()
            marker = root / "ran.txt"
            preview = attempt_root / "seal-preview.json"
            write_preview = (
                "import json,sys; from pathlib import Path; "
                f"Path({str(preview)!r}).write_text(json.dumps({{'review_sha256': 'c' * 64}}))"
            )
            record_argv = (
                "import sys; from pathlib import Path; "
                f"Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]))"
            )
            actions = rehearsal.normalize_rehearsal_actions(
                [
                    {"action_id": "candidate-seal-checkpoint", "operation": "x", "timeout_seconds": 5, "command": ["true"], "item_ids": ["candidate-seal"]},
                    {"action_id": "preview", "operation": "x", "timeout_seconds": 5, "command": [sys.executable, "-c", write_preview], "item_ids": ["candidate-seal"]},
                    {"action_id": "approve", "operation": "x", "timeout_seconds": 5, "command": [sys.executable, "-c", record_argv, "--approve-seal-sha256", "0" * 64], "item_ids": ["candidate-seal"]},
                ]
            )
            status_script = "import json; print(json.dumps({'status': 'candidate_sealed', 'next_command': 'compare'}))"
            with mock.patch.object(rehearsal, "_mount_overlay") as mounted, mock.patch.object(rehearsal, "_bind_alias") as bound:
                result = rehearsal.run_driver(
                    {
                        "overlay_roots": [str(root / "data"), str(root / "alias")],
                        "upper_root": str(root / "upper"),
                        "attempt_root": str(attempt_root),
                        "actions": actions,
                        "status_command": [sys.executable, "-c", status_script],
                        "environment": {},
                    }
                )
            self.assertEqual(mounted.call_count, 1)
            self.assertEqual(bound.call_count, 1)
            self.assertEqual(result["status"], "passed")
            self.assertEqual([item["status"] for item in result["results"]], ["passed", "passed"])
            self.assertEqual(marker.read_text(encoding="utf-8"), f"--approve-seal-sha256 {'c' * 64}")
            self.assertEqual(result["final_status"]["status"], "candidate_sealed")

    def test_driver_treats_preview_approval_required_stop_as_passed_and_injects_context(self) -> None:
        """seal 预览以退出码 2 + status=approval_required 停靠属于通过；driver 注入预演上下文标记并清除 campaign-run 标记。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            attempt_root = root / "attempt"
            attempt_root.mkdir()
            preview = attempt_root / "seal-preview.json"
            env_marker = root / "env.txt"
            write_preview_and_stop = (
                "import json,os,sys; from pathlib import Path; "
                f"Path({str(preview)!r}).write_text(json.dumps({{'review_sha256': 'd' * 64}})); "
                f"Path({str(env_marker)!r}).write_text(os.environ.get('CODEX_UPGRADE_SEAL_REHEARSAL_ACTIVE','') + '|' + os.environ.get('CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE','')); "
                "print(json.dumps({'status': 'approval_required', 'review_sha256': 'd' * 64})); sys.exit(2)"
            )
            stop_without_preview = "import json,sys; print(json.dumps({'status': 'approval_required'})); sys.exit(2)"
            plain_failure = "import sys; sys.exit(2)"
            def run(command):
                actions = rehearsal.normalize_rehearsal_actions(
                    [{"action_id": "preview", "operation": "x", "timeout_seconds": 5, "command": [sys.executable, "-c", command], "item_ids": ["candidate-seal"]}]
                )
                with mock.patch.object(rehearsal, "_mount_overlay"), mock.patch.object(rehearsal, "_bind_alias"), mock.patch.dict(
                    os.environ, {"CODEX_UPGRADE_CAMPAIGN_RUN_ACTIVE": "1"}
                ):
                    return rehearsal.run_driver(
                        {"overlay_roots": [str(root / "data")], "upper_root": str(root / "upper"), "attempt_root": str(attempt_root), "actions": actions, "status_command": [], "environment": {}}
                    )
            result = run(write_preview_and_stop)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["results"][0]["legal_stop"], "approval_required")
            self.assertEqual(env_marker.read_text(encoding="utf-8"), "1|")
            preview.unlink()
            self.assertEqual(run(stop_without_preview)["status"], "failed")
            self.assertEqual(run(plain_failure)["status"], "failed")

    def test_supervisor_gate_requires_receipt_and_rejects_bash_c(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir, attempt_id = self._campaign(root)
            actions = _seal_actions(campaign_dir, attempt_id)
            # 无收据：解析正式清单时失败关闭。
            with mock.patch.object(rehearsal, "_tool_identity", return_value=TOOL_IDENTITY):
                with self.assertRaisesRegex(supervisor.SupervisorError, "没有任何预演收据"):
                    supervisor._validate_vc5_seal_rehearsal_gate(
                        schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                        campaign_id="campaign-seal",
                        phase="VC-5",
                        actions=actions,
                        require_bound_files=True,
                    )
                # 只含 Kilo 检查点的批次不受门禁。
                supervisor._validate_vc5_seal_rehearsal_gate(
                    schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    campaign_id="campaign-seal",
                    phase="VC-5",
                    actions=actions[:1],
                    require_bound_files=True,
                )
                # bash -c 拼接坐标：失败关闭。
                bash_c = [
                    {
                        "action_id": "prepare-candidate-assertion-bundle",
                        "operation": "x",
                        "timeout_seconds": 5,
                        "command": ["/usr/bin/bash", "-c", f"export CAMPAIGN_DIR={campaign_dir} ATTEMPT_ID={attempt_id} SIDE=candidate; exec /usr/bin/bash /tmp/prepare_assertion_bundle.sh"],
                        "item_ids": ["candidate-seal"],
                    }
                ]
                with self.assertRaisesRegex(supervisor.SupervisorError, "bash -c"):
                    supervisor._validate_vc5_seal_rehearsal_gate(
                        schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                        campaign_id="campaign-seal",
                        phase="VC-5",
                        actions=bash_c,
                        require_bound_files=False,
                    )
                # 生成通过收据后同一批次放行。
                rehearsal.rehearse(
                    campaign_dir=campaign_dir, candidate_id="cand-1", attempt_id=attempt_id, actions=actions,
                    data_root=root / "data", alias_roots=[], upper_root=root / "upper", status_command=["status"],
                    tool_identity=TOOL_IDENTITY,
                    driver_runner=lambda arguments: {
                        "schema_version": rehearsal.DRIVER_SCHEMA_VERSION,
                        "status": "passed",
                        "results": [],
                        "final_status": {"returncode": 0, "status": "candidate_sealed", "next_command": "compare", "stderr_tail": ""},
                        "upper_inventory": [],
                    },
                )
                supervisor._validate_vc5_seal_rehearsal_gate(
                    schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                    campaign_id="campaign-seal",
                    phase="VC-5",
                    actions=actions,
                    require_bound_files=True,
                )
                # 不同 attempt 的动作与 assertion 混在一批：坐标不一致。
                mixed = list(actions)
                mixed_command = list(actions[2]["command"])
                mixed_command[mixed_command.index("--attempt-id") + 1] = "other-attempt"
                mixed[2] = dict(actions[2], command=mixed_command)
                with self.assertRaisesRegex(supervisor.SupervisorError, "同一 Campaign"):
                    supervisor._validate_vc5_seal_rehearsal_gate(
                        schema_version=supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                        campaign_id="campaign-seal",
                        phase="VC-5",
                        actions=mixed,
                        require_bound_files=False,
                    )


if __name__ == "__main__":
    unittest.main()
