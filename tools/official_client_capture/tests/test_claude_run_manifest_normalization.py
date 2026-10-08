"""清单纯规范化与真实运行门禁分离：内容仍共用唯一实现，生成入口不能提供派发准入。"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade_vc as claude_vc
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError


class CampaignRunNormalizationTests(unittest.TestCase):
    """验证同一份规范化内容、首批入口边界以及生成后真实派发仍然失败关闭。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def _arguments(self, phase: str = "VC-1", *, client: str | None = "claude") -> dict:
        return {
            "campaign_id": "normalize-campaign",
            "campaign_plan_sha256": "1" * 64,
            "batch_id": "batch-first",
            "batch_sequence": 1,
            "batch_sha256": "2" * 64,
            "phase": phase,
            "predecessor_checkpoint": {
                "path": "control/vc/vc-0-checkpoint.json",
                "sha256": "3" * 64,
                "phase": "VC-0",
                "checkpoint_sha256": "4" * 64,
            },
            "original_deadline_at_utc": "2026-10-09T00:00:00Z",
            "actions": [{
                "action_id": "first-item",
                "operation": f"{phase}:fixture",
                "timeout_seconds": 5,
                "command": ["/bin/true"],
                "item_ids": ["first-item"],
            }],
            "execute_items": ["first-item"],
            "reuse_items": ["inherited-item"],
            "client": client,
        }

    def _write(self, payload: dict, name: str = "manifest.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return path

    def _first_batch(self) -> dict:
        arguments = self._arguments()
        return {
            "schema_version": vc_artifacts.VC_BATCH_SCHEMA,
            "campaign_id": arguments["campaign_id"],
            "campaign_plan_sha256": arguments["campaign_plan_sha256"],
            "batch_id": arguments["batch_id"],
            "sequence": arguments["batch_sequence"],
            "batch_sha256": arguments["batch_sha256"],
            "phase": arguments["phase"],
            "predecessor_checkpoint": arguments["predecessor_checkpoint"],
            "original_deadline_at_utc": arguments["original_deadline_at_utc"],
            "actions": arguments["actions"],
            "execute_item_ids": arguments["execute_items"],
            "reuse_item_ids": arguments["reuse_items"],
            "candidate_revision": None,
            "candidate_id": None,
            "evaluation_baseline": None,
            "baseline_commit_sha256": None,
            "evaluator_digests": None,
        }

    def test_generation_and_runtime_normalize_identical_content(self) -> None:
        for client in (None, "claude"):
            with self.subTest(client=client):
                arguments = self._arguments(client=client)
                pure = supervisor.build_batched_campaign_run_manifest_payload(**arguments)
                strict = supervisor.build_batched_campaign_run_manifest(**arguments)
                self.assertEqual(pure, strict)
                self.assertEqual(pure, supervisor._campaign_run_manifest(self._write(pure)))

    def test_pure_generation_does_not_probe_files_or_run_stage_gates(self) -> None:
        with (
            mock.patch.object(Path, "is_symlink", side_effect=AssertionError("纯核心不应探测软链接")),
            mock.patch.object(Path, "is_file", side_effect=AssertionError("纯核心不应探测文件")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("纯核心不应读取文件")),
            mock.patch.object(supervisor, "_validate_vc1_assertion_seal_gate", side_effect=AssertionError("纯核心不应代执行 VC-1 门禁")),
            mock.patch.object(supervisor, "_validate_vc5_seal_rehearsal_gate", side_effect=AssertionError("纯核心不应代执行 VC-5 门禁")),
        ):
            payload = supervisor.build_batched_campaign_run_manifest_payload(**self._arguments())
            binding = supervisor._normalize_campaign_run_file_binding(
                {"path": str(self.root / "absent.json"), "sha256": "5" * 64}, label="fixture"
            )
        self.assertEqual(payload["client"], "claude")
        self.assertEqual(binding["sha256"], "5" * 64)

    def test_broken_content_is_rejected_by_both_inputs(self) -> None:
        good = supervisor.build_batched_campaign_run_manifest_payload(**self._arguments())
        mutations = {
            "字段不闭合": {**good, "unexpected": True},
            "缺少必备字段": {key: value for key, value in good.items() if key != "batch_sha256"},
            "单元集合不一致": {**good, "execute_items": ["another-item"]},
            "执行与承接交叉": {**good, "reuse_items": ["first-item"]},
        }
        for name, payload in mutations.items():
            with self.subTest(name=name):
                with self.assertRaises(supervisor.SupervisorError):
                    supervisor._normalize_campaign_run_manifest(payload)
                with self.assertRaises(supervisor.SupervisorError):
                    supervisor._campaign_run_manifest(self._write(payload))

    def test_first_compiler_uses_pure_core_and_preserves_content(self) -> None:
        batch = self._first_batch()
        expected = claude_vc.vc_run_manifest_from_batch(batch, batch_model="staging")
        with (
            mock.patch.object(supervisor, "build_batched_campaign_run_manifest", side_effect=AssertionError("首批内容对照不应进入运行校验器")),
            mock.patch.object(supervisor, "_validate_vc1_assertion_seal_gate", side_effect=AssertionError("首批内容对照不应代执行门禁")),
            mock.patch.object(supervisor, "_validate_vc5_seal_rehearsal_gate", side_effect=AssertionError("首批内容对照不应代执行门禁")),
        ):
            actual = claude_vc._first_vc1_run_manifest_from_batch(batch)
        self.assertEqual(actual, expected)

    def test_first_compiler_cannot_be_used_for_other_phase_or_sequence(self) -> None:
        good = self._first_batch()
        for payload in ({**good, "phase": "VC-5"}, {**good, "sequence": 2}, {**good, "sequence": True}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ConfigurationError, "只接受序号 1 的 VC-1"):
                    claude_vc._first_vc1_run_manifest_from_batch(payload)

    def test_strict_compiler_still_calls_each_stage_gate_with_real_bindings(self) -> None:
        for phase, gate in (("VC-1", "_validate_vc1_assertion_seal_gate"), ("VC-5", "_validate_vc5_seal_rehearsal_gate")):
            with self.subTest(phase=phase), mock.patch.object(
                supervisor, gate, side_effect=supervisor.SupervisorError("门禁拒绝")
            ) as called:
                with self.assertRaisesRegex(supervisor.SupervisorError, "门禁拒绝"):
                    supervisor.build_batched_campaign_run_manifest(**self._arguments(phase))
                self.assertTrue(called.call_args.kwargs["require_bound_files"])

    def test_pure_vc5_manifest_cannot_bypass_real_rehearsal_gate(self) -> None:
        arguments = self._arguments("VC-5", client=None)
        action = copy.deepcopy(arguments["actions"][0])
        action["command"] = [
            "python3", "/tmp/codex_upgrade.py", "capture-candidate", "seal",
            "--campaign-dir", str(self.root / "absent-campaign"),
            "--candidate-id", "candidate-first", "--attempt-id", "attempt-first",
            "--capture-manifest", str(self.root / "capture-manifest.json"),
        ]
        arguments["actions"] = [action]
        generated = supervisor.build_batched_campaign_run_manifest_payload(**arguments)
        with self.assertRaisesRegex(supervisor.SupervisorError, "VC-5 seal 门禁的 Campaign 不存在"):
            supervisor._campaign_run_manifest(self._write(generated))

    def test_dispatch_rechecks_gate_before_lock_or_action(self) -> None:
        payload = supervisor.build_batched_campaign_run_manifest_payload(**self._arguments())
        arguments = argparse.Namespace(manifest=str(self._write(payload)), state_dir=str(self.root / "state"))
        with (
            mock.patch.object(supervisor, "_validate_vc1_assertion_seal_gate", side_effect=supervisor.SupervisorError("派发门禁拒绝")) as gate,
            mock.patch.object(supervisor, "_campaign_run_lock") as lock,
            mock.patch.object(supervisor, "_campaign_run_locked") as execute,
        ):
            with self.assertRaisesRegex(supervisor.SupervisorError, "派发门禁拒绝"):
                supervisor._campaign_run_command(arguments)
            self.assertTrue(gate.call_args.kwargs["require_bound_files"])
            lock.assert_not_called()
            execute.assert_not_called()

    def test_real_claude_evaluator_dependencies_are_computable(self) -> None:
        """真实策略不需要屏障或忽略豁免，compare 与 accept 两条读侧都可算出绑定摘要。"""

        digests = claude_identity.evaluator_dependency_digests()
        self.assertEqual(set(digests), set(claude_identity.EVALUATOR_DIGEST_FIELDS))
        for digest in digests.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_recovery_binding_runtime_still_rejects_symlink_and_digest_drift(self) -> None:
        target = self.root / "contract.json"
        target.write_bytes(b"frozen")
        binding = {"path": str(target), "sha256": supervisor._sha256(target.read_bytes())}
        self.assertEqual(supervisor._campaign_run_file_binding(binding, label="fixture", require_bound_files=True), binding)
        target.write_bytes(b"changed")
        with self.assertRaisesRegex(supervisor.SupervisorError, "路径或摘要漂移"):
            supervisor._campaign_run_file_binding(binding, label="fixture", require_bound_files=True)
        link = self.root / "contract-link.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(supervisor.SupervisorError, "路径或摘要漂移"):
            supervisor._campaign_run_file_binding({**binding, "path": str(link)}, label="fixture", require_bound_files=False)


if __name__ == "__main__":
    unittest.main()
