"""编排器服务面合同测试（Claude 升级控制面设计 B1-09、B1-15）：两个适配器跑同一组合同用例。

* 服务面每个成员在 Claude 适配器上都能取到，种类一致；函数成员与 Codex 编排器签名逐项相等。
* 迁入薄壳在 Claude 适配器作用域里转调共享实现；待实现成员报 ``ClaudeOrchestratorPending`` 并写明工作项。
* 照搬的通用函数与 Codex 源码逐字一致，同一组行为用例对两个编排器各跑一遍结果相同。
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.tests import orchestrator_surface_inventory as inventory

COPIED = (
    "_fingerprint", "_normalized_json_sha256", "_read_json", "_utc_now", "_rfc3339_datetime", "_secure_write_json_once",
    "_secure_copy_file_once", "_write_or_verify_json_ignoring", "_require_file_binding", "_directory_tree_digest",
    "_campaign_lease_pid_alive", "_campaign_lease_staleness", "_campaign_file", "_capture_attempt_relative",
    "_candidate_build_receipt_path", "_candidate_revisions_root", "_candidate_revision_dir", "_assertions_root_for_baseline",
    "_attempt_recovery_segment_root", "_recovery_continuity_receipt_path", "_evaluation_baseline_dir", "_failed_job_ids",
    "_read_campaign_lease", "CampaignLease", "_campaign_lock", "_campaign_operation_lease", "_write_campaign_lease_stop_once",
    "_validate_campaign_lease_stop_receipt", "_lease_identity_matches", "_campaign_coordinates", "_require_formal_campaign",
    "_git_commit",
    # B2-07：证据根取代归档与冲突隔离的读取（服务面四个成员与它们的辅助函数）
    "_attempt_reference_matches", "_validate_attempt_reference", "_evidence_root_supersessions", "_supersession_row_state",
    "_evidence_root_relocations", "_evidence_conflict_quarantines", "_conflict_quarantined_attempts",
    "_attempt_conflict_quarantine_receipt",
    # B2-08：环境污染与隔离、作废集合、工具演进影响、连续性漂移收据与阶段路径（Campaign 级阶段）
    "_attempt_continuity_drift_receipt", "_attempt_evolution_impact", "_attempt_evolution_invalidated_job_ids",
    "_require_recovery_preview_scope_equals_frozen_jobs", "_rerun_all_invalidated_attempts", "_isolation_invalidated_attempts",
    "_environment_isolations", "_contamination_fact", "_isolated_contamination_keys", "_campaign_contamination_records",
    "_campaign_environment_decision", "_legacy_stage_path", "_stage_path",
)
# 登记的分叉（照搬后改为 Claude 自己的实现）：函数 → 理由。
FORKED = {
    "_campaign_tool_evolutions": "B2-08：演进链读写改由 claude_upgrade_evolution 校验（共享链校验在 Codex 评估读侧闭包内，策略文件钉死为 Codex 的）",
    "_campaign_effective_tool_identity": "B2-08：同上，有效工具身份取 Claude 演进链",
}


class SurfaceContractTests(unittest.TestCase):
    def test_every_surface_member_resolves_on_claude_adapter(self) -> None:
        orchestrator = uca.adapter_for("claude").orchestrator
        for name, signature in uca.ORCHESTRATOR_SURFACE.items():
            with self.subTest(name=name):
                member = getattr(orchestrator, name)
                if signature is None:
                    self.assertFalse(inspect.isfunction(member) and name not in {"file_sha256"})
                elif signature == "class":
                    self.assertTrue(inspect.isclass(member))
                else:
                    self.assertTrue(callable(member))

    def test_function_signatures_match_codex(self) -> None:
        for name, signature in uca.ORCHESTRATOR_SURFACE.items():
            if signature is None or signature == "class":
                continue
            with self.subTest(name=name):
                self.assertEqual(
                    str(inspect.signature(getattr(claude_upgrade, name))),
                    str(inspect.signature(getattr(codex_upgrade, name))),
                )

    def test_copied_functions_are_verbatim(self) -> None:
        """照搬的通用函数与 Codex 逐字一致：Codex 改动时这里提醒同步或登记分叉（附录 E 第 6 条以后合并）。"""

        for name in COPIED:
            with self.subTest(name=name):
                self.assertEqual(inspect.getsource(getattr(claude_upgrade, name)), inspect.getsource(getattr(codex_upgrade, name)))
        self.assertEqual(set(COPIED) & set(FORKED), set())
        for name in FORKED:
            with self.subTest(forked=name):
                self.assertNotEqual(inspect.getsource(getattr(claude_upgrade, name)), inspect.getsource(getattr(codex_upgrade, name)))

    def test_shells_run_shared_flow_under_claude_scope(self) -> None:
        observed: list[str] = []

        def fake(*arguments, **options):
            del arguments, options
            observed.append(upgrade_control_plane._adapter().client)
            return "shared"

        shells = sorted(name for name in uca.ORCHESTRATOR_SURFACE if name in set(inventory.migrated_function_names()))
        self.assertGreater(len(shells), 50)
        for name in shells:
            with self.subTest(name=name), mock.patch.object(upgrade_control_plane, name, side_effect=fake) as patched:
                function = getattr(claude_upgrade, name)
                parameters = inspect.signature(function).parameters.values()
                positional = [object() for item in parameters if item.default is inspect.Parameter.empty and item.kind in {item.POSITIONAL_ONLY, item.POSITIONAL_OR_KEYWORD}]
                keywords = {item.name: object() for item in parameters if item.default is inspect.Parameter.empty and item.kind is item.KEYWORD_ONLY}
                result = function(*positional, **keywords)
                if inspect.isgenerator(result) or hasattr(result, "__enter__"):
                    with result:
                        pass
                self.assertEqual(patched.call_count, 1)
        self.assertEqual(set(observed), {"claude"})

    def test_pending_members_name_their_work_item(self) -> None:
        # B2-08 落实官方阶段、B3-01 补 Campaign 级阶段结果与 b0 评估路径：候选状态、候选级阶段结果、b≥1 评估路径与候选恢复段
        # 仍各自报待实现。
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"campaign_status（候选阶段）：由 B2-11"):
            claude_upgrade.campaign_status(Path("/nonexistent"), "c1")
        for stage, work_item in (("capture-candidate", "B4-02"), ("compare", "B4-03"), ("accept", "B4-06")):
            with self.subTest(stage=stage), self.assertRaisesRegex(
                claude_upgrade.ClaudeOrchestratorPending, rf"_load_stage_result（{stage}）：由 {work_item}"
            ):
                claude_upgrade._load_stage_result(Path("/nonexistent"), stage, "c1")
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"_stage_read_source（评估基线 b1）：由 B4-04"):
            claude_upgrade._stage_read_source(Path("/nonexistent"), "c1", 1, "compare")
        with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"_load_attempt_recovery_segment：由 B4-05"):
            claude_upgrade._load_attempt_recovery_segment(Path("/nonexistent"), candidate_id="c1", attempt_id="a1", recovery_revision="ar1")
        for member in ("recovery_segment_summary", "segment_reuse_proofs"):
            with self.subTest(member=member), self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, r"B4-05"):
                getattr(uca.adapter_for("claude"), member)(Path("/nonexistent"), "c1", "a1", "ar1")
        self.assertTrue(issubclass(claude_upgrade.ClaudeOrchestratorPending, codex_upgrade.ConfigurationError))


class CopiedBehaviourContractTests(unittest.TestCase):
    """同一组行为用例对两个编排器各跑一遍，结果必须相同。"""

    def _both(self):
        return (("codex", codex_upgrade), ("claude", claude_upgrade))

    def test_digests_paths_and_timestamps(self) -> None:
        payload = {"b": [1, 2], "a": "值"}
        campaign = Path("/data/evidence/campaigns/c1")
        results = {}
        for label, module in self._both():
            results[label] = (
                module._fingerprint(payload),
                module._normalized_json_sha256(payload),
                module._capture_attempt_relative("candidate", "cand-1"),
                module._candidate_build_receipt_path(campaign, "cand-1"),
                module._candidate_revision_dir(campaign, 3),
                module._assertions_root_for_baseline(campaign, "cand-1", 2),
                module._attempt_recovery_segment_root(campaign / "a1", "ar2"),
                module._recovery_continuity_receipt_path(campaign, "a1"),
                module._evaluation_baseline_dir(campaign, "cand-1", 1),
                module._rfc3339_datetime("2026-10-07T00:00:00Z", "t"),
                module._failed_job_ids([{"id": "j1", "status": "failed"}, {"id": "j2", "status": "passed"}]),
            )
        self.assertEqual(results["codex"], results["claude"])
        for label, module in self._both():
            with self.subTest(label=label):
                stamp = module._utc_now()
                self.assertEqual(datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None, True)
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._capture_attempt_relative("candidate", "../escape")

    def test_write_once_and_verify(self) -> None:
        for label, module in self._both():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "out"
                root.mkdir(mode=0o700)
                path = root / "receipt.json"
                module._secure_write_json_once(path, {"value": 1})
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._secure_write_json_once(path, {"value": 2})
                self.assertEqual(module._read_json(path, "收据"), {"value": 1})
                module._write_or_verify_json_ignoring(path, {"value": 1}, volatile=())
                with self.assertRaises(codex_upgrade.ConfigurationError):
                    module._write_or_verify_json_ignoring(path, {"value": 3}, volatile=())

    def test_lease_staleness(self) -> None:
        now = datetime(2026, 10, 7, tzinfo=timezone.utc)
        payload = {"owner_pid": 2 ** 30, "heartbeat_at_utc": "2026-10-06T00:00:00+00:00", "heartbeat_seconds": 30,
                   "deadline_at_utc": "2026-10-07T01:00:00+00:00"}
        results = {label: module._campaign_lease_staleness(payload, now=now) for label, module in self._both()}
        self.assertEqual(results["codex"], results["claude"])


if __name__ == "__main__":
    unittest.main()
