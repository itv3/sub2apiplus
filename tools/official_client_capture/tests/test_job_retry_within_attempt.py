"""同一 attempt 内补跑失败任务，不等于跨 attempt 拼接证据。

上游波动（模型 at capacity、压缩原因未触发）会让个别任务落空。不在同一 attempt 内补跑，
就只能靠 resume 整轮重来 20 分钟，而重来同样要赌 17 项一次全过——实测单轮全绿概率约
三成，收敛极慢。

补跑必须守住：run_nonce／环境边界不变（本来就在同一 attempt 内）、失败证据先归档再重跑
（否则新结论会落在旧样本上）、次数有上限且写进收据（不能把稳定失败重试成功）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[2]))

from tools.official_client_capture import codex_upgrade as cu


class _Job:
    def __init__(self, required: bool = True) -> None:
        self.job_id = "official-core"
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

    def test_补跑前归档失败证据(self) -> None:
        """不归档就补跑，新结论会落在混有旧样本的目录上。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run-dir"
            root.mkdir()
            (root / "traffic.pcap").write_bytes(b"stale")
            result = {
                "evidence_roots": [str(root)],
                "scenario_receipts": [
                    {"path": str(root / "scenario-receipt.json")}
                ],
            }
            cu._archive_failed_job_evidence(result, attempt_index=1)
            self.assertFalse(root.exists())
            archived = root.with_name(f"{root.name}.failed-attempt1")
            self.assertTrue(archived.is_dir())
            self.assertEqual((archived / "traffic.pcap").read_bytes(), b"stale")
            self.assertEqual(result["evidence_roots"], [str(archived)])
            self.assertEqual(
                result["scenario_receipts"][0]["path"],
                str(archived / "scenario-receipt.json"),
            )

    def test_最终失败也归档证据供跨_attempt_重跑(self) -> None:
        """用尽内部重试后必须清出固定路径，显式 resume 才能重新创建它。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run-dir"
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
                    "evidence_roots": [str(root)],
                }

            with mock.patch.object(cu, "run_job", side_effect=always_fail), \
                 mock.patch.object(cu.time, "sleep"):
                result = cu._run_job_with_retry(_Job(), Path(temporary))

            final_archive = root.with_name(f"{root.name}.failed-attempt3")
            self.assertEqual(calls, [1, 2, 3])
            self.assertFalse(root.exists())
            self.assertTrue(final_archive.is_dir())
            self.assertEqual(result["evidence_roots"], [str(final_archive)])
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
