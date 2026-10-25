"""``claude_stage_result_read_once`` 的专项回归（只测测试夹具本身，不动受管工具）。

复用的前提是“作用域内读到的是同一份字节”，所以重点核：第一次真实、之后复用；不同 Campaign／候选各算各的；
带 manifest 且 campaign_id 不一致时走真实调用；异常与 None 不缓存；返回值是深拷贝；出口核对两份落盘文件未变，
变了就失败；退出作用域即恢复原函数。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture.tests import claude_stage_result_read_once as once

CANDIDATE = "claude-cand-r1"


class StageResultReadOnceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="stage-result-once-")
        self.addCleanup(directory.cleanup)
        self.campaign = Path(directory.name).resolve() / "campaign"
        self.stage_path = delivery.stage_result_path(self.campaign, CANDIDATE)
        self.checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(self.campaign, delivery.DELIVERY_PHASE, revision=1)
        for path in (self.stage_path, self.checkpoint_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        self.calls: list[tuple[str, str, object]] = []
        self.payload: dict | None = {"campaign_id": "c1", "vc6_status": "complete", "receipt_ref": {"sha256": "x"}}

        def fake(campaign_dir, candidate_id, *, manifest=None):
            self.calls.append((str(campaign_dir), str(candidate_id), manifest))
            if isinstance(self.payload, Exception):
                raise self.payload
            return None if self.payload is None else dict(self.payload, receipt_ref=dict(self.payload["receipt_ref"]))

        patcher = mock.patch.object(delivery, "load_stage_result", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_call_is_real_and_following_calls_are_reused(self) -> None:
        with once.stage_result_read_once(self.campaign, CANDIDATE, 1) as reads:
            first = delivery.load_stage_result(self.campaign, CANDIDATE)
            second = delivery.load_stage_result(self.campaign, CANDIDATE, manifest={"campaign_id": "c1"})
            third = delivery.load_stage_result(str(self.campaign), CANDIDATE)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((reads.real_calls, reads.reused_calls), (1, 2))
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_results_are_deep_copies(self) -> None:
        with once.stage_result_read_once(self.campaign, CANDIDATE, 1):
            first = delivery.load_stage_result(self.campaign, CANDIDATE)
            first["receipt_ref"]["sha256"] = "tampered"
            first["vc6_status"] = "tampered"
            second = delivery.load_stage_result(self.campaign, CANDIDATE)
        self.assertEqual(second["receipt_ref"], {"sha256": "x"})
        self.assertEqual(second["vc6_status"], "complete")

    def test_other_candidate_or_mismatched_manifest_goes_real(self) -> None:
        with once.stage_result_read_once(self.campaign, CANDIDATE, 1) as reads:
            delivery.load_stage_result(self.campaign, CANDIDATE)
            delivery.load_stage_result(self.campaign, "other-candidate")
            delivery.load_stage_result(self.campaign, CANDIDATE, manifest={"campaign_id": "different"})
        self.assertEqual(reads.real_calls, 3)
        self.assertEqual(len(self.calls), 3)

    def test_none_and_exceptions_are_not_cached(self) -> None:
        with once.stage_result_read_once(self.campaign, CANDIDATE, 1) as reads:
            self.payload = None
            self.assertIsNone(delivery.load_stage_result(self.campaign, CANDIDATE))
            self.payload = RuntimeError("读取失败")
            with self.assertRaises(RuntimeError):
                delivery.load_stage_result(self.campaign, CANDIDATE)
            with self.assertRaises(RuntimeError):
                delivery.load_stage_result(self.campaign, CANDIDATE)
        self.assertEqual((reads.real_calls, reads.reused_calls), (3, 0))

    def test_exit_fails_when_watched_file_changes(self) -> None:
        with self.assertRaises(AssertionError):
            with once.stage_result_read_once(self.campaign, CANDIDATE, 1):
                delivery.load_stage_result(self.campaign, CANDIDATE)
                self.stage_path.write_text('{"changed": true}', encoding="utf-8")
        with self.assertRaises(AssertionError):
            with once.stage_result_read_once(self.campaign, CANDIDATE, 1):
                self.checkpoint_path.unlink()

    def test_exit_fails_when_watched_file_is_missing_at_entry(self) -> None:
        self.stage_path.unlink()
        with self.assertRaises(AssertionError):
            with once.stage_result_read_once(self.campaign, CANDIDATE, 1):
                pass

    def test_original_function_is_restored(self) -> None:
        before = delivery.load_stage_result
        with once.stage_result_read_once(self.campaign, CANDIDATE, 1):
            self.assertIsNot(delivery.load_stage_result, before)
        self.assertIs(delivery.load_stage_result, before)


if __name__ == "__main__":
    unittest.main()
