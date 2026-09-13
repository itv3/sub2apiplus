"""同一 attempt 内补跑失败任务，不等于跨 attempt 拼接证据。

上游波动（模型 at capacity、压缩原因未触发）会让个别任务落空。不在同一 attempt 内补跑，
就只能靠 resume 整轮重来 20 分钟，而重来同样要赌 17 项一次全过——实测单轮全绿概率约
三成，收敛极慢。

补跑必须守住：run_nonce／环境边界不变（本来就在同一 attempt 内）、失败证据先归档再重跑
（否则新结论会落在旧样本上）、次数有上限且写进收据（不能把稳定失败重试成功）。
"""

from __future__ import annotations

import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.official_client_capture import codex_upgrade as cu


class _Job:
    def __init__(self, required: bool = True, job_id: str = "official-core") -> None:
        self.job_id = job_id
        self.required = required
        self.phase = "official"
        self.steps = []
        self.evidence_roots = []
        self.covers = ()
        self.scenario_ids = ()
        self.suites = ()
        self.description = "测试任务"
        self.required_scenario_receipts = ()


class JobRetryWithinAttemptTest(unittest.TestCase):
    def test_失败任务在同一_attempt_内补跑(self) -> None:
        calls: list[int] = []

        def fake(job, log_root, attempt_index=1, scenario_context=None):
            calls.append(attempt_index)
            status = "complete" if attempt_index == 2 else "failed"
            return {"id": job.job_id, "status": status, "evidence_roots": []}

        with mock.patch.object(cu, "run_job", side_effect=fake), \
             mock.patch.object(cu.time, "sleep"):
            result = cu._run_job_with_retry(_Job(), Path("/tmp"))

        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls, [1, 2])

    def test_补跑次数有上限(self) -> None:
        """稳定失败不能被重试成通过；用尽次数后如实返回失败。"""

        calls: list[int] = []

        def always_fail(job, log_root, attempt_index=1, scenario_context=None):
            calls.append(attempt_index)
            return {"id": job.job_id, "status": "failed", "evidence_roots": []}

        with mock.patch.object(cu, "run_job", side_effect=always_fail), \
             mock.patch.object(cu.time, "sleep"):
            result = cu._run_job_with_retry(_Job(), Path("/tmp"))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(calls), cu.JOB_RETRY_LIMIT + 1)

    def test_非必需任务不补跑(self) -> None:
        calls: list[int] = []

        def fail_once(job, log_root, attempt_index=1, scenario_context=None):
            calls.append(attempt_index)
            return {"id": job.job_id, "status": "failed", "evidence_roots": []}

        with mock.patch.object(cu, "run_job", side_effect=fail_once), \
             mock.patch.object(cu.time, "sleep"):
            cu._run_job_with_retry(_Job(required=False), Path("/tmp"))

        self.assertEqual(calls, [1])

    def test_cloud_config启动失败立即升级为campaign前置条件(self) -> None:
        """0.154 的全局启动失败不得对当前或后续 Job 做机械重试。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            root = host_runs / "cloud-config-failure"
            logical_root = "/root/oauth-capture/runs/cloud-config-failure"
            calls: list[int] = []

            def fail_once(job, log_root, attempt_index=1, scenario_context=None):
                calls.append(attempt_index)
                stderr = (
                    root
                    / "results"
                    / "direct"
                    / "codex-http"
                    / "s1"
                    / "turn1-stderr.log"
                )
                stderr.parent.mkdir(parents=True)
                stderr.write_bytes(
                    b"Error: timed out waiting for cloud config bundle after 15s\n"
                )
                return {
                    "id": job.job_id,
                    "status": "failed",
                    "evidence_roots": [logical_root],
                }

            with (
                mock.patch.object(cu, "run_job", side_effect=fail_once),
                mock.patch.object(cu.time, "sleep") as sleeper,
                mock.patch.object(
                    cu,
                    "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                    host_runs,
                ),
            ):
                result = cu._run_job_with_retry(_Job(), Path(temporary))

            self.assertEqual(calls, [1])
            sleeper.assert_not_called()
            self.assertEqual(
                result["error"],
                cu.CAMPAIGN_GLOBAL_PRECONDITION_ERROR,
            )
            self.assertEqual(
                result["evidence_roots"],
                [f"{logical_root}.failed-attempt1"],
            )
            self.assertTrue(
                (host_runs / "cloud-config-failure.failed-attempt1").is_dir()
            )

    def test_campaign前置失败保留失败项与pending闭集(self) -> None:
        """停线后只把已执行项记为 failed，其余计划项必须保持 pending。"""

        prior = {
            "id": "official-reused",
            "status": "complete",
            "disposition": "reused",
        }
        failed = {
            "id": "official-core",
            "status": "failed",
            "disposition": "executed",
            "error": cu.CAMPAIGN_GLOBAL_PRECONDITION_ERROR,
        }
        plan = cu._capture_attempt_incremental_plan(
            planned_jobs=[
                _Job(job_id="official-reused"),
                _Job(job_id="official-core"),
                _Job(job_id="official-pending"),
            ],
            prior_results=[prior],
            results=[prior, failed],
            changed_components=[],
            affected_job_ids=[],
        )

        self.assertEqual(plan["reused_job_ids"], ["official-reused"])
        self.assertEqual(plan["executed_job_ids"], ["official-core"])
        self.assertEqual(plan["failed_job_ids"], ["official-core"])
        self.assertEqual(plan["pending_job_ids"], ["official-pending"])
        unsigned = dict(plan)
        digest = unsigned.pop("plan_sha256")
        self.assertEqual(digest, cu.incremental_recovery.digest(unsigned))

    def test_补跑前归档失败证据(self) -> None:
        """宿主归档后，两条容器别名都必须同步重定位。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "data" / "runs"
            host_runs.mkdir(parents=True)
            root = host_runs / "run-dir"
            root.mkdir()
            (root / "traffic.pcap").write_bytes(b"stale")
            result = {
                "evidence_roots": ["/root/oauth-capture/runs/run-dir"],
                "scenario_receipts": [
                    {
                        "path": (
                            "/capture/runs/run-dir/scenario-receipt.json"
                        )
                    }
                ],
            }
            with mock.patch.object(
                cu,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                cu._archive_failed_job_evidence(result, attempt_index=1)
            self.assertFalse(root.exists())
            archived = root.with_name(f"{root.name}.failed-attempt1")
            self.assertTrue(archived.is_dir())
            self.assertEqual((archived / "traffic.pcap").read_bytes(), b"stale")
            self.assertEqual(
                result["evidence_roots"],
                ["/root/oauth-capture/runs/run-dir.failed-attempt1"],
            )
            self.assertEqual(
                result["scenario_receipts"][0]["path"],
                "/capture/runs/run-dir.failed-attempt1/scenario-receipt.json",
            )

    def test_归档支持_capture_别名并保留_oauth_收据别名(self) -> None:
        """任一登记别名发起归档时，另一别名下的引用也必须可重放。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            source = host_runs / "capture-alias"
            source.mkdir()
            result = {
                "evidence_roots": ["/capture/runs/capture-alias"],
                "nested": {
                    "path": (
                        "/root/oauth-capture/runs/capture-alias/receipt.json"
                    )
                },
            }
            with mock.patch.object(
                cu,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                cu._archive_failed_job_evidence(result, attempt_index=2)

            self.assertTrue((host_runs / "capture-alias.failed-attempt2").is_dir())
            self.assertEqual(
                result["evidence_roots"],
                ["/capture/runs/capture-alias.failed-attempt2"],
            )
            self.assertEqual(
                result["nested"]["path"],
                (
                    "/root/oauth-capture/runs/"
                    "capture-alias.failed-attempt2/receipt.json"
                ),
            )

    def test_归档拒绝未登记根与父目录跳转(self) -> None:
        """缺失路径也不能借未登记别名或父目录跳转绕过路由门禁。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            with mock.patch.object(
                cu,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ):
                for root in (
                    "/tmp/unregistered",
                    "/capture/runs/../escaped",
                    "/root/oauth-capture/runs",
                ):
                    with self.subTest(root=root), self.assertRaises(
                        cu.ConfigurationError
                    ):
                        cu._archive_failed_job_evidence(
                            {"evidence_roots": [root]},
                            attempt_index=1,
                        )

    def test_归档拒绝宿主符号链接(self) -> None:
        """容器逻辑路径不得经宿主 runs 子树中的符号链接逃逸。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (host_runs / "linked").symlink_to(outside, target_is_directory=True)
            with mock.patch.object(
                cu,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ), self.assertRaisesRegex(cu.ConfigurationError, "符号链接"):
                cu._archive_failed_job_evidence(
                    {"evidence_roots": ["/capture/runs/linked"]},
                    attempt_index=1,
                )
            self.assertTrue(outside.is_dir())

    def test_归档失败诊断保留异常类型与_errno(self) -> None:
        """父监督器应能区分只读文件系统，而不是只看到泛化失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            (host_runs / "readonly").mkdir()
            failure = OSError(errno.EROFS, "read-only file system")
            with mock.patch.object(
                cu,
                "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                host_runs,
            ), mock.patch.object(Path, "rename", side_effect=failure):
                with self.assertRaises(cu.ConfigurationError) as caught:
                    cu._archive_failed_job_evidence(
                        {"evidence_roots": ["/capture/runs/readonly"]},
                        attempt_index=1,
                    )
            message = str(caught.exception)
            self.assertIn("error_type=OSError", message)
            self.assertIn(f"errno={errno.EROFS}(EROFS)", message)
            self.assertIn("rollback_complete=true", message)
            self.assertNotIn(str(host_runs), message)

    def test_最终失败也归档证据供跨_attempt_重跑(self) -> None:
        """用尽内部重试后必须清出固定路径，显式 resume 才能重新创建它。"""

        with tempfile.TemporaryDirectory() as temporary:
            host_runs = Path(temporary) / "runs"
            host_runs.mkdir()
            root = host_runs / "run-dir"
            logical_root = "/root/oauth-capture/runs/run-dir"
            calls: list[int] = []

            def always_fail(job, log_root, attempt_index=1, scenario_context=None):
                self.assertFalse(root.exists())
                root.mkdir()
                (root / "traffic.pcap").write_bytes(
                    f"attempt-{attempt_index}".encode()
                )
                calls.append(attempt_index)
                return {
                    "id": job.job_id,
                    "status": "failed",
                    "evidence_roots": [logical_root],
                }

            with mock.patch.object(cu, "run_job", side_effect=always_fail), \
                 mock.patch.object(cu.time, "sleep"), \
                 mock.patch.object(
                     cu,
                     "FAILED_JOB_EVIDENCE_HOST_RUN_ROOT",
                     host_runs,
                 ):
                result = cu._run_job_with_retry(_Job(), Path(temporary))

            final_archive = root.with_name(f"{root.name}.failed-attempt3")
            self.assertEqual(calls, [1, 2, 3])
            self.assertFalse(root.exists())
            self.assertTrue(final_archive.is_dir())
            self.assertEqual(
                result["evidence_roots"],
                [f"{logical_root}.failed-attempt3"],
            )
            self.assertEqual(
                (final_archive / "traffic.pcap").read_bytes(), b"attempt-3"
            )

    def test_收据记录补跑次数(self) -> None:
        """审计要能还原真实执行过程，而不是只看到最后一次。"""

        with tempfile.TemporaryDirectory() as temporary:
            log_root = Path(temporary)
            result = cu.run_job(_Job(), log_root, attempt_index=3)
            self.assertEqual(result["attempt_index"], 3)


if __name__ == "__main__":
    unittest.main()
