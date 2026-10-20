"""恢复预览按环境隔离的污染窗口给出可复用作业（与 resume 同一判定）的单元测试。

背景（0.163.0 VC-1 首批实采）：监督器在作业失败后停了采集容器，attempt 判 environment_contaminated；操作员做了带污染发生时刻
的环境隔离。resume 对这类被隔离的 attempt 保留窗口起点之前完成且出口时段可核验的作业复用，但恢复预览原先把被污染 attempt 的
结果一律不复用（复用 0、执行全部），批准前的 resume 复用核对（R17）因此判“预览与 resume 不一致”而拒绝批准。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler

WINDOW_START = "2026-10-20T21:41:52.700Z"
WINDOW_EPOCH = datetime(2026, 10, 20, 21, 41, 52, 700000, tzinfo=timezone.utc).timestamp()
ATTEMPT_ID = "20261020T212831Z-3939d80823307ee4"


class IsolationWindowReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.campaign = Path(self._tmp.name)
        self.attempt_root = self.campaign / "official" / "attempts" / ATTEMPT_ID
        self.attempt_root.mkdir(parents=True)

    def write_attempt(self, results: list[dict]) -> None:
        (self.attempt_root / "attempt.json").write_text(json.dumps({"results": results}), encoding="utf-8")

    @staticmethod
    def job(job_id: str, finished_at: float | None) -> dict:
        result: dict = {"id": job_id, "status": "complete"}
        if finished_at is not None:
            result["runtime_egress"] = {"finished_at_epoch": finished_at}
        return result

    def reusable(self, complete: list[str], *, isolation: dict | None, trusted: bool = True) -> list[str]:
        with mock.patch.object(codex_upgrade, "_attempt_isolation_receipt", return_value=isolation), \
                mock.patch.object(codex_upgrade.codex_upgrade_supervisor, "job_egress_trusted", return_value=trusted):
            return reconciler._isolation_window_reusable(self.campaign, "official", None, ATTEMPT_ID, complete)

    def test_jobs_finished_before_the_window_are_reused_and_later_ones_are_not(self) -> None:
        self.write_attempt([
            self.job("job-early", WINDOW_EPOCH - 600),
            self.job("job-just-before", WINDOW_EPOCH - 0.001),
            self.job("job-at-window", WINDOW_EPOCH),
            self.job("job-after", WINDOW_EPOCH + 5),
        ])
        got = self.reusable(
            ["job-early", "job-just-before", "job-at-window", "job-after"],
            isolation={"contamination_started_at_utc": WINDOW_START},
        )
        self.assertEqual(got, ["job-early", "job-just-before"])

    def test_no_isolation_receipt_means_no_reuse(self) -> None:
        self.write_attempt([self.job("job-early", WINDOW_EPOCH - 600)])
        self.assertEqual(self.reusable(["job-early"], isolation=None), [])

    def test_isolation_without_a_window_means_no_reuse(self) -> None:
        self.write_attempt([self.job("job-early", WINDOW_EPOCH - 600)])
        self.assertEqual(self.reusable(["job-early"], isolation={"index": 1}), [])

    def test_unverifiable_egress_binding_is_treated_as_contaminated(self) -> None:
        self.write_attempt([self.job("job-early", WINDOW_EPOCH - 600)])
        self.assertEqual(
            self.reusable(["job-early"], isolation={"contamination_started_at_utc": WINDOW_START}, trusted=False), [],
        )

    def test_job_without_a_runtime_egress_binding_is_not_reused(self) -> None:
        self.write_attempt([self.job("job-unbound", None)])
        self.assertEqual(self.reusable(["job-unbound"], isolation={"contamination_started_at_utc": WINDOW_START}), [])

    def test_only_complete_jobs_are_considered_and_unknown_ids_are_ignored(self) -> None:
        self.write_attempt([self.job("job-early", WINDOW_EPOCH - 600), self.job("job-failed", WINDOW_EPOCH - 600)])
        got = self.reusable(["job-early", "job-missing"], isolation={"contamination_started_at_utc": WINDOW_START})
        self.assertEqual(got, ["job-early"])

    def test_missing_attempt_file_means_no_reuse(self) -> None:
        self.assertEqual(self.reusable(["job-early"], isolation={"contamination_started_at_utc": WINDOW_START}), [])

    def test_no_complete_jobs_means_no_reuse_without_reading_anything(self) -> None:
        self.assertEqual(self.reusable([], isolation={"contamination_started_at_utc": WINDOW_START}), [])


if __name__ == "__main__":
    unittest.main()


class IsolatedAttemptContinuityTests(unittest.TestCase):
    """被隔离且带窗口的 attempt 没有 after 探针时，连续性核对改用隔离收据里的干净环境作边界；其余情形照旧拒绝。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.campaign = Path(self._tmp.name)
        (self.campaign / "official" / "attempts" / ATTEMPT_ID).mkdir(parents=True)
        self.isolation = {
            "index": 1,
            "isolation_sha256": "a" * 64,
            "clean_environment": {"sha256": "b" * 64},
            "contamination_started_at_utc": WINDOW_START,
        }

    def verify(self, *, isolation: dict | None, **kwargs):
        with mock.patch.object(codex_upgrade, "_attempt_isolation_receipt", return_value=isolation):
            return codex_upgrade._verify_environment_continuity(
                self.campaign, Path("official"), {ATTEMPT_ID}, {"kind": "before"}, **kwargs
            )

    def test_isolation_with_window_replaces_the_missing_after_probe(self) -> None:
        record = self.verify(isolation=self.isolation)
        self.assertEqual(record["environment_boundary"], "environment_isolation")
        self.assertEqual(record["source_attempt_id"], ATTEMPT_ID)
        self.assertEqual(record["isolation_sha256"], "a" * 64)
        self.assertEqual(record["clean_environment_sha256"], "b" * 64)
        self.assertEqual(record["compared_kinds"], [])

    def test_missing_after_probe_without_isolation_is_still_rejected(self) -> None:
        with self.assertRaises(codex_upgrade.ConfigurationError):
            self.verify(isolation=None)

    def test_isolation_without_a_window_is_still_rejected(self) -> None:
        without_window = {key: value for key, value in self.isolation.items() if key != "contamination_started_at_utc"}
        with self.assertRaises(codex_upgrade.ConfigurationError):
            self.verify(isolation=without_window)

    def test_cross_campaign_source_never_uses_the_isolation_shortcut(self) -> None:
        with self.assertRaises(codex_upgrade.ConfigurationError):
            self.verify(isolation=self.isolation, source_campaign_dir=self.campaign, source_relative=Path("official"))

    def test_no_carried_attempt_means_no_continuity_record(self) -> None:
        with mock.patch.object(codex_upgrade, "_attempt_isolation_receipt", return_value=self.isolation):
            self.assertIsNone(
                codex_upgrade._verify_environment_continuity(self.campaign, Path("official"), set(), {"kind": "before"})
            )
