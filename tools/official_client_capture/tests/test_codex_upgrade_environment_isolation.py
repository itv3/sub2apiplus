"""修好接着跑第 13 项：环境污染按 attempt 隔离——恢复段污染事实与 resume 闭集的隔离作废来源。

端到端流程（污染 → 暂停 → environment-isolate → 全部重跑续跑、官方已封存受污染终态、候选 seal 失败的
作废对账与恢复预览后继）在 test_codex_upgrade.py 的 B0 夹具里覆盖；这里只测不依赖整套夹具的规则。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import codex_upgrade


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


class RecoverySegmentContaminationFactTests(unittest.TestCase):
    """恢复段 run-summary 状态为 environment_contaminated 时是一条污染事实（此前只经一次写入的旁路标记间接体现）。"""

    def _segment(self, root: Path, *, status: str = "environment_contaminated", tamper: bool = False) -> tuple[Path, Path]:
        campaign_dir = root / "campaign"
        attempt_root = campaign_dir / "candidates" / "cand" / "attempts" / "a1"
        summary = {
            "schema_version": codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA,
            "attempt_id": "a1",
            "recovery_revision": "ar1",
            "candidate_id": "cand",
            "status": status,
            "completed_at_utc": "2026-09-27T01:02:03.000000Z",
        }
        summary["attempt_recovery_digest"] = codex_upgrade._fingerprint(summary)
        if tamper:
            # 摘要之后被改：身份与自摘要都对不上，必须失败关闭而不是静默当作无污染。
            summary["candidate_id"] = "other"
        _write_json(attempt_root / "recovery" / "ar1" / codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, summary)
        return campaign_dir, attempt_root

    def test_contaminated_segment_is_a_fact_bound_to_its_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, attempt_root = self._segment(Path(directory))
            facts = codex_upgrade._recovery_segment_contamination_facts(
                campaign_dir, attempt_root, phase="candidate", candidate_id="cand"
            )
            self.assertEqual([fact["record"] for fact in facts], ["cand:a1.ar1:recovery"])
            fact = facts[0]
            summary = attempt_root / "recovery" / "ar1" / codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
            self.assertEqual(
                (fact["kind"], fact["phase"], fact["candidate_id"], fact["attempt_id"], fact["recovery_revision"]),
                ("recovery", "candidate", "cand", "a1", "ar1"),
            )
            self.assertEqual(fact["at_utc"], "2026-09-27T01:02:03.000000Z")
            self.assertEqual(
                fact["source"],
                {"path": summary.relative_to(campaign_dir).as_posix(), "sha256": codex_upgrade.file_sha256(summary)},
            )
            # 官方侧没有恢复段：不适用。
            self.assertEqual(
                codex_upgrade._recovery_segment_contamination_facts(
                    campaign_dir, attempt_root, phase="official", candidate_id=None
                ),
                [],
            )

    def test_non_contaminated_segment_is_not_a_fact(self) -> None:
        for status in ("awaiting_receipts", "failed"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                campaign_dir, attempt_root = self._segment(Path(directory), status=status)
                self.assertEqual(
                    codex_upgrade._recovery_segment_contamination_facts(
                        campaign_dir, attempt_root, phase="candidate", candidate_id="cand"
                    ),
                    [],
                )

    def test_tampered_contaminated_segment_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir, attempt_root = self._segment(Path(directory), tamper=True)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "身份或自摘要不一致"):
                codex_upgrade._recovery_segment_contamination_facts(
                    campaign_dir, attempt_root, phase="candidate", candidate_id="cand"
                )


class IsolatedSourceRecoveryScopeTests(unittest.TestCase):
    """被环境隔离作废的 attempt 作为 resume 来源：已完成作业全部作废、全部计划作业重跑，环境边界绑定隔离收据。"""

    PLANNED = ("a", "b", "c")
    ISOLATION = {"index": 1, "isolation_sha256": "9" * 64}

    @staticmethod
    def _result(job_id: str, finished_at_epoch: float | None = None) -> dict:
        """已完成作业结果；给出完成时刻即附带出口时段绑定（第三批 B3-14 的窗口判定读它）。"""

        item = {"id": job_id, "execution_sha256": job_id * 64, "status": "complete", "required": True}
        if finished_at_epoch is not None:
            item["runtime_egress"] = {
                "schema_version": "codex-upgrade-job-egress/v1", "run_dir": "/run", "campaign_id": "c", "owner_nonce": "n" * 64,
                "started_at_epoch": finished_at_epoch - 10.0, "finished_at_epoch": finished_at_epoch,
            }
        return item

    def _scope(self, *, status: str, isolation: dict | None, results: list[dict] | None = None, egress_trusted: bool = True) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            (campaign_dir / "campaign.json").write_text("{}\n", encoding="utf-8")
            attempt_root = campaign_dir / "a1"
            attempt_root.mkdir()
            attempt = {
                "status": status,
                "phase": "candidate",
                "candidate_id": "cand",
                "campaign_id": "c",
                "campaign_manifest_sha256": codex_upgrade.file_sha256(campaign_dir / "campaign.json"),
                "attempt_id": "a1",
                "run_nonce": "1" * 64,
                "attempt_digest": "2" * 64,
                # a、b 已完成，c 未执行（pending）。
                "results": results if results is not None else [self._result(job_id) for job_id in ("a", "b")],
                "job_checkpoint": {"path": "checkpoints", "record_count": 2, "last_sequence": 2, "last_sha256": "3" * 64},
            }
            store = mock.Mock()
            store.records.return_value = [1, 2]
            with mock.patch.object(
                codex_upgrade.codex_upgrade_supervisor, "job_egress_trusted", return_value=egress_trusted
            ), mock.patch.multiple(
                codex_upgrade,
                _attempt_isolation_receipt=mock.Mock(return_value=isolation),
                _load_capture_reservation=mock.Mock(return_value={
                    "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]
                }),
                _attempt_evolution_impact=mock.Mock(
                    return_value={"index": 0, "affected_job_ids": [], "changed_paths": [], "evolution_indexes": []}
                ),
                _resolve_attempt_binding=mock.Mock(return_value=attempt_root / "checkpoints"),
                _validate_checkpoint_records=mock.Mock(),
                # 污染 attempt 没有可用的前后环境收据：隔离来源不得去读它。
                _phase_evaluation_environment_boundary=mock.Mock(side_effect=AssertionError("不应读取源 attempt 的环境收据")),
            ), mock.patch.object(codex_upgrade.incremental_recovery, "CheckpointStore", mock.Mock(return_value=store)):
                return codex_upgrade._phase_evaluation_recovery_scope(
                    campaign_dir, {"campaign_id": "c"}, phase="candidate", candidate_id="cand",
                    attempt_root=attempt_root, attempt=attempt,
                    allow_awaiting_failures=status == "awaiting_receipts", allow_evolution_invalidated=True,
                )

    def test_isolated_source_reruns_every_planned_job(self) -> None:
        # environment_contaminated：采集中污染；awaiting_receipts：Kilo 后环境恢复失败（seal-failure）。
        for status in ("environment_contaminated", "awaiting_receipts"):
            with self.subTest(status=status):
                scope = self._scope(status=status, isolation=dict(self.ISOLATION))
                self.assertEqual(scope["execute_job_ids"], ["a", "b", "c"])
                self.assertEqual(scope["completed_job_ids"], [])
                self.assertEqual(scope["pending_job_ids"], ["c"])
                self.assertEqual(
                    scope["environment_isolation"],
                    {
                        "isolation_index": 1, "isolation_sha256": "9" * 64, "invalidated_job_ids": ["a", "b"],
                        # 无窗口：没有污染前可复用的作业。
                        "contamination_started_at_utc": None, "reused_job_ids": [],
                    },
                )
                self.assertEqual(scope["environment_boundary_sha256"], "9" * 64)
                # 闭集不变式：隔离作废作业与失败／未执行作业一起恰好构成执行闭集。
                with mock.patch.object(codex_upgrade, "_load_capture_reservation", return_value={
                    "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]
                }), mock.patch.object(codex_upgrade, "_job_execution_sha256", side_effect=lambda job: job.job_id * 64):
                    completed, execute = codex_upgrade._validate_recovery_scope_plan(
                        Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                        scope=scope, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in self.PLANNED],
                    )
                self.assertEqual((completed, execute), (set(), {"a", "b", "c"}))

    def test_windowed_isolation_reuses_jobs_finished_before_contamination(self) -> None:
        """第三批 B3-14（R3 污染部分）：隔离收据带污染发生时刻——之前完成且出口绑定可核验的作业保留复用、之后的重跑；
        缺出口绑定或绑定不可核验一律重跑；闭集不变式要求 completed 恰好等于 reused，篡改即拒。"""

        window = "2026-09-27T10:00:00Z"
        window_epoch = codex_upgrade._rfc3339_datetime(window, "t").timestamp()
        isolation = dict(self.ISOLATION, contamination_started_at_utc=window)
        planned = {"planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]}
        scope = self._scope(
            status="environment_contaminated", isolation=isolation,
            results=[self._result("a", window_epoch - 60), self._result("b", window_epoch + 60)],
        )
        self.assertEqual((scope["completed_job_ids"], scope["execute_job_ids"], scope["pending_job_ids"]), (["a"], ["b", "c"], ["c"]))
        self.assertEqual(
            scope["environment_isolation"],
            {
                "isolation_index": 1, "isolation_sha256": "9" * 64, "invalidated_job_ids": ["b"],
                "contamination_started_at_utc": window, "reused_job_ids": ["a"],
            },
        )
        self.assertEqual(scope["environment_boundary_sha256"], "9" * 64)
        with mock.patch.object(codex_upgrade, "_load_capture_reservation", return_value=planned), mock.patch.object(
            codex_upgrade, "_job_execution_sha256", side_effect=lambda job: job.job_id * 64
        ):
            completed, execute = codex_upgrade._validate_recovery_scope_plan(
                Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                scope=scope, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in self.PLANNED],
            )
            self.assertEqual((completed, execute), ({"a"}, {"b", "c"}))
            for tampered in (
                dict(scope, environment_isolation=dict(scope["environment_isolation"], reused_job_ids=[])),
                dict(scope, environment_isolation=dict(scope["environment_isolation"], contamination_started_at_utc=None)),
                dict(scope, completed_job_ids=["a", "b"], execute_job_ids=["c"],
                     environment_isolation=dict(scope["environment_isolation"], invalidated_job_ids=[])),
            ):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "environment_isolation 作废作业非法"):
                    codex_upgrade._validate_recovery_scope_plan(
                        Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                        scope=tampered, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in self.PLANNED],
                    )
        # 缺出口绑定（a）或绑定不可核验（b，job_egress_trusted 为假）：全部重跑。
        scope = self._scope(
            status="environment_contaminated", isolation=isolation,
            results=[self._result("a"), self._result("b", window_epoch - 60)], egress_trusted=False,
        )
        self.assertEqual((scope["completed_job_ids"], scope["execute_job_ids"]), ([], ["a", "b", "c"]))
        self.assertEqual(scope["environment_isolation"]["reused_job_ids"], [])
        self.assertEqual(scope["environment_isolation"]["invalidated_job_ids"], ["a", "b"])
        # 判定函数本身：缺绑定、核验抛错、完成时刻不早于窗口都为假。
        self.assertFalse(codex_upgrade._job_finished_before(self._result("a"), window_epoch))
        with mock.patch.object(codex_upgrade.codex_upgrade_supervisor, "job_egress_trusted", return_value=True):
            self.assertTrue(codex_upgrade._job_finished_before(self._result("a", window_epoch - 1), window_epoch))
            self.assertFalse(codex_upgrade._job_finished_before(self._result("a", window_epoch), window_epoch))
        with mock.patch.object(
            codex_upgrade.codex_upgrade_supervisor, "job_egress_trusted",
            side_effect=codex_upgrade.codex_upgrade_supervisor.SupervisorError("父 run 记录缺失"),
        ):
            self.assertFalse(codex_upgrade._job_finished_before(self._result("a", window_epoch - 1), window_epoch))

    def test_unisolated_contaminated_source_is_refused(self) -> None:
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只有 failed attempt"):
            self._scope(status="environment_contaminated", isolation=None)

    def test_isolation_scope_tampering_breaks_the_invariant(self) -> None:
        scope = self._scope(status="environment_contaminated", isolation=dict(self.ISOLATION))
        tampered = dict(scope, environment_isolation=dict(scope["environment_isolation"], invalidated_job_ids=["a"]))
        with mock.patch.object(codex_upgrade, "_load_capture_reservation", return_value={
            "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in self.PLANNED]
        }), mock.patch.object(codex_upgrade, "_job_execution_sha256", side_effect=lambda job: job.job_id * 64):
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "Job 闭集不一致"):
                codex_upgrade._validate_recovery_scope_plan(
                    Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                    scope=tampered, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in self.PLANNED],
                )


class ContinuityDriftTests(unittest.TestCase):
    """修好接着跑第 22 项：承接前环境连续性漂移是可识别的失败类型，续跑闭集把它当作全部重跑源。"""

    def test_drift_raises_the_dedicated_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory)
            relative = Path("official")
            after = campaign_dir / relative / "attempts" / "a1" / "evidence" / "environment" / "after" / "probe-manifest.json"
            after.parent.mkdir(parents=True)
            after.write_text("{}\n", encoding="utf-8")
            digests = iter([{"service": "a"}, {"service": "b"}])
            with mock.patch.object(codex_upgrade, "_probe_snapshot_digests", side_effect=lambda _manifest: next(digests)):
                with self.assertRaises(codex_upgrade.EnvironmentContinuityDrift) as caught:
                    codex_upgrade._verify_environment_continuity(campaign_dir, relative, {"a1"}, {"phase": "before"})
            self.assertIsInstance(caught.exception, codex_upgrade.ConfigurationError)
            self.assertIn("恢复预览全部重跑", str(caught.exception))
            # 连续时照常返回连续性收据；没有承接时不比较。
            with mock.patch.object(codex_upgrade, "_probe_snapshot_digests", return_value={"service": "a"}):
                receipt = codex_upgrade._verify_environment_continuity(campaign_dir, relative, {"a1"}, {"phase": "before"})
            self.assertEqual(receipt["source_attempt_id"], "a1")
            self.assertIsNone(codex_upgrade._verify_environment_continuity(campaign_dir, relative, set(), {"phase": "before"}))

    def test_drift_detection_needs_failed_status_and_exact_type(self) -> None:
        error = {"type": codex_upgrade.CONTINUITY_DRIFT_ERROR_TYPE, "message": "漂移"}
        self.assertTrue(codex_upgrade._attempt_continuity_drifted({"status": "failed", "execution_error": error}))
        self.assertFalse(codex_upgrade._attempt_continuity_drifted({"status": "awaiting_receipts", "execution_error": error}))
        self.assertFalse(
            codex_upgrade._attempt_continuity_drifted({"status": "failed", "execution_error": {"type": "ConfigurationError"}})
        )
        self.assertFalse(codex_upgrade._attempt_continuity_drifted({"status": "failed", "execution_error": None}))

    def test_scope_invariant_rejects_mixed_or_tampered_drift(self) -> None:
        planned = ("a", "b", "c")
        base = {
            "planned_job_ids": ["a", "b", "c"],
            "completed_job_ids": [],
            "failed_job_ids": [],
            "pending_job_ids": ["c"],
            "execute_job_ids": ["a", "b", "c"],
            "continuity_drift": {"invalidated_job_ids": ["a", "b"]},
        }

        def check(scope: dict) -> tuple:
            with mock.patch.object(codex_upgrade, "_load_capture_reservation", return_value={
                "planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in planned]
            }), mock.patch.object(codex_upgrade, "_job_execution_sha256", side_effect=lambda job: job.job_id * 64):
                return codex_upgrade._validate_recovery_scope_plan(
                    Path("/nonexistent"), phase="candidate", candidate_id="cand", source_root=Path("/nonexistent"),
                    scope=scope, planned_jobs=[SimpleNamespace(job_id=job_id) for job_id in planned],
                )

        self.assertEqual(check(dict(base)), (set(), {"a", "b", "c"}))
        for label, scope in (
            ("与隔离作废同时出现", dict(base, environment_isolation={"invalidated_job_ids": []})),
            ("少报作废作业", dict(base, continuity_drift={"invalidated_job_ids": ["a"]})),
            ("仍有复用", dict(base, completed_job_ids=["a"], execute_job_ids=["b", "c"], continuity_drift={"invalidated_job_ids": ["b"]})),
        ):
            with self.subTest(label=label), self.assertRaises(codex_upgrade.ConfigurationError):
                check(scope)


if __name__ == "__main__":
    unittest.main()
