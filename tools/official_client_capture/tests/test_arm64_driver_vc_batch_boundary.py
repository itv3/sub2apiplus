"""批次驱动 vc-batch.sh 的修复轮规则（E4-01）：派发前读当前部署的后台验证结论（失败或中止即拒绝派发、退出 3），
再向统一调度执行器申请整机，批次期间持有预约、结束（含失败）归还。

真实的 vc-batch.sh、lib.sh、background_validation.py 与 unit_executor.py；数据根里的编排器换成替身模块（记下被调用时
整机预约的状态），执行器状态目录在临时目录里，不碰本机调度状态。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ci import background_validation as bv
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
COMMIT = "c" * 40

# 编排器替身：compile-and-run-vc-batch 时记下整机预约的状态（批准了谁），按 VC_BATCH_STUB_RC 退出。
STUB_ORCHESTRATOR = """
import json, os, sys
state = os.environ["UNIT_EXECUTOR_STATE_DIR"]
def read(name):
    try:
        return json.load(open(os.path.join(state, name)))
    except OSError:
        return None
granted = read("granted.json")
with open(os.environ["VC_BATCH_STUB_LOG"], "a") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "granted_owner": (granted or {}).get("owner")}) + "\\n")
print(json.dumps({"status": "stopped", "phase": "VC-5", "batch_sequence": 7, "campaign_run": {"status": "stopped", "actions": []}}))
sys.exit(int(os.environ.get("VC_BATCH_STUB_RC", "0")))
"""


class VcBatchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.fixture = driver_tests._DriverFixture(self.root)
        self.data = self.fixture.data_root
        self.drv = self.root / "drv"
        self.drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "vc-batch.sh", "background_validation.py", "unit_executor.py", "unit_records.py",
                     "read_audit.py", "unit_executor.json"):
            (self.drv / name).write_bytes((SCRIPTS / name).read_bytes())
        for relative, text in {"tools/__init__.py": "", "tools/official_client_capture/__init__.py": "",
                               "tools/official_client_capture/codex_upgrade.py": STUB_ORCHESTRATOR}.items():
            path = self.data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.data / "control" / self.fixture.inputs).mkdir(parents=True, exist_ok=True)
        self.receipt = self.data / "control" / "codex-01600-supervisor-enable-20261003t000000z.json"
        self.receipt.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.state = self.root / "executor-state"
        self.log = self.root / "orchestrator.log"
        self.env = {**self.fixture.env, "UNIT_EXECUTOR_STATE_DIR": str(self.state), "VC_BATCH_STUB_LOG": str(self.log),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def validation(self, status: str) -> None:
        deployment = bv.latest_deployment(self.data)
        bv._write(bv.result_path(self.fixture.runroot, COMMIT, deployment), {
            "schema_version": bv.SCHEMA, "commit": COMMIT, "deployment": deployment, "status": status, "pid": None,
            "started_at_utc": "2026-10-03T00:00:00Z", "failed_gates": ["test-capture-tools"] if status == "failed" else [],
            "reason": "测试" if status == "aborted" else None, "log": "/x/log"})

    def batch(self, phase: str = "VC-2", predecessor: str = "VC-1", **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.drv / "vc-batch.sh"), self.fixture.new, self.fixture.inputs, phase, "7", predecessor, "plan.json"],
                              capture_output=True, text=True, errors="replace", env={**os.environ, **self.env, **env}, cwd=str(self.root),
                              timeout=120)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.is_file() else []

    def test_failed_or_aborted_background_validation_refuses_dispatch(self) -> None:
        for status in ("failed", "aborted"):
            with self.subTest(status):
                self.validation(status)
                result = self.batch()
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("拒绝派发下一批", result.stdout)
                self.assertEqual(self.calls(), [], "不放行时不派发批次")
                self.assertFalse((self.state / "reservation.json").exists(), "不放行时也不申请整机")

    def test_passed_batch_holds_the_machine_and_releases_it(self) -> None:
        self.validation("passed")
        result = self.batch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("整机已批准", result.stdout)
        [call] = self.calls()
        self.assertEqual(call["granted_owner"], f"vc-batch-{self.fixture.new}-7", "批次运行期间持有整机预约")
        self.assertIn("compile-and-run-vc-batch", call["argv"])
        self.assertFalse((self.state / "reservation.json").exists(), "批次结束归还整机")

    def test_failed_batch_still_releases_and_running_or_missing_validation_is_allowed(self) -> None:
        result = self.batch(VC_BATCH_STUB_RC="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("还没有后台验证", result.stdout, "没有结论只提示、照常派发")
        self.assertFalse((self.state / "reservation.json").exists(), "批次失败也归还整机")
        self.validation("running")   # 后台进程号为空：读出来是 vanished，同样只提示
        result = self.batch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_candidate_stage_without_revisions_falls_back_to_campaign_checkpoint(self) -> None:
        """VC-4～VC-6 的前序 checkpoint：没有任何 revision 目录时回落 Campaign 级路径（原来 ls 无匹配在 pipefail 下让整个
        脚本中止，回落分支走不到）。"""

        self.validation("passed")
        result = self.batch("VC-5", "VC-4")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"predecessor checkpoint: {self.fixture.newdir}/control/vc/vc-4-checkpoint.json", result.stdout)
        [call] = self.calls()
        self.assertEqual(call["argv"][call["argv"].index("--predecessor-checkpoint") + 1],
                         str(self.fixture.newdir / "control" / "vc" / "vc-4-checkpoint.json"))


if __name__ == "__main__":
    unittest.main()
