"""R17：reconcile-attempt 的恢复预览按 resume 同一复用判定只读复算。

覆盖：
* 结构钉住：resume（``_run_capture_attempt``）失败重跑代码块的摘要与内部函数调用顺序；复算函数按同一顺序
  调用同一组函数，``_prior_complete_results`` 的关键字参数集合一致。resume 判定逻辑一变，测试即红，
  必须先同步 ``official_recovery_reuse_check`` 再更新摘要。
* 复算行为（替身驱动各内部步骤）：resume 会拒绝的情形（承接不完整、闭集不符、源 attempt 不符、已封存、
  产出变化缺映射）一律失败；执行集为空不做路径授权；有签名交接时用交接结果；awaiting_receipts 源放宽状态。
* 对账接线：段模式、候选阶段、孤儿 attempt 不复算；official 复算失败转为 ReconcilerError；
  reconcile_attempt 在生成预览之后、批准之前复算。
"""

from __future__ import annotations

import hashlib
import inspect
import re
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler

# resume 失败重跑代码块（从 rerun_failed 分支到承接集合比对之前）的摘要。
RERUN_BLOCK_SHA256 = "95223d61cb7c77a77f9157708d11e8a61c57195fa5ade2fdc813213e5a5d195b"
# resume 与复算共同按序调用的内部函数（候选专用的 runtime successor 只在 resume 里）。
SHARED_SEQUENCE = (
    "_latest_failed_attempt_for_identity(",
    "_phase_evaluation_recovery_scope(",
    "_validate_recovery_scope_plan(",
    "_authorize_phase_recovery_production_paths(",
    "_validate_interrupted_recovery_transition(",
    "_phase_recovery_exact_affected_job_ids(",
    "_load_recovery_execution_handoff(",
    "_prior_complete_results(",
)


def _resume_rerun_block() -> str:
    source = inspect.getsource(codex_upgrade._run_capture_attempt)
    start = source.index('        getattr(arguments, "rerun_failed", False)\n        and classification_candidate_reuse_context is None')
    end = source.index('        completed_ids = {item["id"] for item in prior_results}', start)
    return source[start:end]


def _positions(text: str, names: tuple[str, ...]) -> list[int]:
    return [text.find(name) for name in names]


def _call_keywords(text: str, name: str) -> set[str]:
    """取某个调用（最后一次出现）的关键字参数名集合。"""

    start = text.rindex(name) + len(name)
    depth, index = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        index += 1
    return set(re.findall(r"^\s*([a-z_]+)=", text[start:index], flags=re.M))


class ResumeMirrorPinTests(unittest.TestCase):
    def test_resume_rerun_block_is_pinned(self) -> None:
        digest = hashlib.sha256(_resume_rerun_block().encode("utf-8")).hexdigest()
        self.assertEqual(
            digest, RERUN_BLOCK_SHA256,
            "resume 失败重跑判定已变化：先同步 codex_upgrade.official_recovery_reuse_check，再更新本摘要",
        )

    def test_check_calls_same_helpers_in_same_order(self) -> None:
        block = _resume_rerun_block()
        check = inspect.getsource(codex_upgrade.official_recovery_reuse_check)
        for label, text in (("resume", block), ("复算", check)):
            with self.subTest(side=label):
                positions = _positions(text, SHARED_SEQUENCE)
                self.assertNotIn(-1, positions, f"{label} 缺少调用：{dict(zip(SHARED_SEQUENCE, positions))}")
                self.assertEqual(positions, sorted(positions), f"{label} 调用顺序变化")

    def test_prior_results_keyword_sets_match(self) -> None:
        block = _resume_rerun_block()
        check = inspect.getsource(codex_upgrade.official_recovery_reuse_check)
        resume_keywords = _call_keywords(block, "_prior_complete_results(")
        check_keywords = _call_keywords(check, "_prior_complete_results(")
        # 跨 Campaign 来源与候选 runtime successor 专用参数只在 resume 里（official 默认路径取默认值）。
        resume_only = {"source_campaign_dir", "source_candidate_id", "source_receipt_binding", "validated_current_production_sha256"}
        self.assertEqual(resume_keywords - resume_only, check_keywords)
        for name in ("_phase_evaluation_recovery_scope(", "_validate_recovery_scope_plan(", "_authorize_phase_recovery_production_paths(", "_load_recovery_execution_handoff("):
            with self.subTest(call=name):
                self.assertEqual(_call_keywords(block, name), _call_keywords(check, name))


def _job(job_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(job_id=job_id)


class OfficialRecoveryReuseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.campaign_dir = Path(self._directory.name).resolve()
        self.source_root = self.campaign_dir / "official" / "attempts" / "20260926T000000Z-aaaaaaaaaaaaaaaa"
        self.source_root.mkdir(parents=True)
        self.patches = {
            "_require_formal_campaign": mock.Mock(return_value={"official_identity": {"binary": "x"}, "campaign_id": "c"}),
            "_reject_contaminated_campaign": mock.Mock(return_value=None),
            "_load_stage_result": mock.Mock(side_effect=codex_upgrade.ConfigurationError("capture-official 尚未封存")),
            "_active_unsealed_attempts": mock.Mock(return_value=[]),
            "_campaign_jobs": mock.Mock(return_value=[_job("a"), _job("b"), _job("c")]),
            "_tool_identity": mock.Mock(return_value={"files_sha256": "t"}),
            "_cheap_capture_tool_impact": mock.Mock(return_value={"kind": "none"}),
            "_latest_failed_attempt_for_identity": mock.Mock(return_value=(self.source_root, {"status": "failed"})),
            "_phase_evaluation_recovery_scope": mock.Mock(return_value={"completed_job_ids": ["a", "b"]}),
            "_validate_recovery_scope_plan": mock.Mock(return_value=({"a", "b"}, {"c"})),
            "_authorize_phase_recovery_production_paths": mock.Mock(return_value={"tools/p.py"}),
            "_phase_recovery_exact_affected_job_ids": mock.Mock(return_value={"c"}),
            "_load_recovery_execution_handoff": mock.Mock(return_value=None),
            "_prior_complete_results": mock.Mock(return_value=[{"id": "a"}, {"id": "b"}]),
        }
        self._stack = [mock.patch.object(codex_upgrade, name, value) for name, value in self.patches.items()]
        for patcher in self._stack:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self._stack):
            patcher.stop()
        self._directory.cleanup()

    def _check(self, **overrides):
        arguments = {"source_attempt_id": self.source_root.name, "reuse_job_ids": ["b", "a"], "execute_job_ids": ["c"]}
        arguments.update(overrides)
        return codex_upgrade.official_recovery_reuse_check(self.campaign_dir, **arguments)

    def test_consistent_preview_passes_with_resume_arguments(self) -> None:
        result = self._check()
        self.assertEqual(result["status"], "consistent")
        self.assertEqual((result["reuse_job_ids"], result["execute_job_ids"]), (["a", "b"], ["c"]))
        kwargs = self.patches["_prior_complete_results"].call_args.kwargs
        self.assertEqual(kwargs["expected_reuse_job_ids"], {"a", "b"})
        self.assertEqual(kwargs["allowed_high_risk_path_changes"], {"tools/p.py"})
        self.assertEqual(kwargs["allowed_source_statuses"], ("failed",))
        self.assertEqual(kwargs["source_attempt_id"], self.source_root.name)
        self.assertEqual(tuple(kwargs["affected_job_ids"]), ())
        self.assertEqual(self.patches["_prior_complete_results"].call_args.args[1], codex_upgrade._capture_attempt_relative("official", None))

    def test_rejections_resume_would_raise(self) -> None:
        cases = {
            "承接不完整": ("_prior_complete_results", mock.Mock(side_effect=codex_upgrade.ConfigurationError("恢复 transition 的已完成 Job 无法安全复用"))),
            "源 attempt 缺失": ("_latest_failed_attempt_for_identity", mock.Mock(return_value=None)),
            "已封存": ("_load_stage_result", mock.Mock(return_value={"status": "complete"})),
            "待封存 attempt": ("_active_unsealed_attempts", mock.Mock(return_value=["x"])),
            "产出变化缺映射": ("_cheap_capture_tool_impact", mock.Mock(return_value={"kind": "unmapped_production_paths", "unmapped_production_paths": ["x.py"]})),
            "污染": ("_reject_contaminated_campaign", mock.Mock(side_effect=codex_upgrade.ConfigurationError("污染"))),
        }
        for label, (name, replacement) in cases.items():
            with self.subTest(label=label), mock.patch.object(codex_upgrade, name, replacement), self.assertRaises(codex_upgrade.ConfigurationError):
                self._check()

    def test_preview_sets_must_equal_frozen_scope(self) -> None:
        for overrides in ({"reuse_job_ids": ["a"]}, {"execute_job_ids": ["c", "b"]}, {"source_attempt_id": "20260926T000001Z-bbbbbbbbbbbbbbbb"}):
            with self.subTest(overrides=overrides), self.assertRaises(codex_upgrade.ConfigurationError):
                self._check(**overrides)

    def test_empty_execute_skips_path_authorization(self) -> None:
        with mock.patch.object(codex_upgrade, "_validate_recovery_scope_plan", mock.Mock(return_value=({"a", "b", "c"}, set()))), \
                mock.patch.object(codex_upgrade, "_prior_complete_results", mock.Mock(return_value=[{"id": "a"}, {"id": "b"}, {"id": "c"}])) as prior:
            result = self._check(reuse_job_ids=["a", "b", "c"], execute_job_ids=[])
            self.assertEqual(result["allowed_production_paths"], [])
            self.patches["_authorize_phase_recovery_production_paths"].assert_not_called()
            self.patches["_phase_recovery_exact_affected_job_ids"].assert_not_called()
            self.assertEqual(prior.call_args.kwargs["allowed_high_risk_path_changes"], set())

    def test_handoff_results_replace_recomputation(self) -> None:
        with mock.patch.object(codex_upgrade, "_load_recovery_execution_handoff", mock.Mock(return_value={"reused_results": [{"id": "a"}, {"id": "b"}]})):
            self.assertTrue(self._check()["handoff_used"])
            self.patches["_prior_complete_results"].assert_not_called()
        with mock.patch.object(codex_upgrade, "_load_recovery_execution_handoff", mock.Mock(return_value={"reused_results": [{"id": "a"}]})), \
                self.assertRaises(codex_upgrade.ConfigurationError):
            self._check()

    def test_awaiting_receipts_source_widens_statuses(self) -> None:
        with mock.patch.object(codex_upgrade, "_latest_failed_attempt_for_identity", mock.Mock(return_value=(self.source_root, {"status": "awaiting_receipts"}))):
            self._check()
        self.assertEqual(self.patches["_prior_complete_results"].call_args.kwargs["allowed_source_statuses"], ("failed", "awaiting_receipts"))
        self.assertTrue(self.patches["_phase_evaluation_recovery_scope"].call_args.kwargs["allow_awaiting_failures"])

    def test_interrupted_transition_is_validated_like_resume(self) -> None:
        marker = self.source_root / "interrupted.json"
        marker.write_text("{}", encoding="utf-8")
        with mock.patch.object(codex_upgrade, "_interrupted_recovery_transition_path", mock.Mock(return_value=marker)), \
                mock.patch.object(codex_upgrade, "_validate_interrupted_recovery_transition", mock.Mock(return_value={"affected_job_ids": ["c"]})) as validate:
            self._check()
            validate.assert_called_once()
        self.assertEqual(self.patches["_phase_recovery_exact_affected_job_ids"].call_args.kwargs["explicit_affected_job_ids"], ["c"])


class ReconcilerWiringTests(unittest.TestCase):
    PREVIEW = {"source_attempt_receipt_exists": True, "source_attempt_id": "att", "reuse_job_ids": ["a"], "execute_job_ids": ["b"]}

    def test_scope_of_recheck(self) -> None:
        with mock.patch.object(codex_upgrade, "official_recovery_reuse_check") as check:
            self.assertEqual(reconciler._resume_reuse_check(Path("/c"), phase="candidate", recovery_revision="ar1", preview=self.PREVIEW)["status"], "not_applicable")
            segment = reconciler._resume_reuse_check(Path("/c"), phase="official", recovery_revision="ar1", preview=self.PREVIEW)
            self.assertEqual(segment["status"], "not_applicable")
            self.assertIn("恢复段", segment["reason"])
            self.assertEqual(reconciler._resume_reuse_check(Path("/c"), phase="candidate", recovery_revision=None, preview=self.PREVIEW)["status"], "not_applicable")
            orphan = {**self.PREVIEW, "source_attempt_receipt_exists": False}
            self.assertEqual(reconciler._resume_reuse_check(Path("/c"), phase="official", recovery_revision=None, preview=orphan)["status"], "not_applicable")
            check.assert_not_called()
            check.return_value = {"status": "consistent"}
            self.assertEqual(reconciler._resume_reuse_check(Path("/c"), phase="official", recovery_revision=None, preview=self.PREVIEW), {"status": "consistent"})
            check.assert_called_once_with(Path("/c"), source_attempt_id="att", reuse_job_ids=["a"], execute_job_ids=["b"])

    def test_inconsistency_is_reported_not_raised(self) -> None:
        with mock.patch.object(codex_upgrade, "official_recovery_reuse_check", side_effect=codex_upgrade.ConfigurationError("无法安全复用")):
            result = reconciler._resume_reuse_check(Path("/c"), phase="official", recovery_revision=None, preview=self.PREVIEW)
        self.assertEqual(result, {"status": "inconsistent", "reason": "无法安全复用"})
        with mock.patch.object(codex_upgrade, "official_recovery_reuse_check", side_effect=KeyError("official_identity")), \
                self.assertRaises(KeyError):
            reconciler._resume_reuse_check(Path("/c"), phase="official", recovery_revision=None, preview=self.PREVIEW)

    def test_reconcile_attempt_rechecks_before_approval(self) -> None:
        source = inspect.getsource(reconciler.reconcile_attempt)
        preview_at = source.index("preview = _recovery_preview(")
        check_at = source.index("_resume_reuse_check(")
        refuse_at = source.index("拒绝批准：")
        approve_at = source.index("approve_recovery_preview(")
        self.assertLess(preview_at, check_at)
        self.assertLess(check_at, refuse_at)
        self.assertLess(refuse_at, approve_at)


if __name__ == "__main__":
    unittest.main()
