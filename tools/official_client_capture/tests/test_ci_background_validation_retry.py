"""后台验证结论与批次边界对“补跑通过”标注的处理（优化第 2 项）：结论单列补跑通过的单元，标注被改写或日志缺失就不放行。

执行器的汇总取自真实的 ``run-gates`` 运行（偶发单元首次失败、补跑通过）；入口门禁用替身脚本，只负责把准备好的入口总摘要拷到
``--out``；``check`` 重放标注，准入（``vc5_admission.state`` → ``background_validation.check``）因此同步认这个标注。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools.ci import background_validation as bv
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase

COMMIT = "c" * 40

FAKE_ENTRY_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --profile|--mode|--record-store|--work|--wait-lock|--parallel) shift 2 ;;
    *) shift ;;
  esac
done
mkdir -p "$OUT"
cp -R "$FAKE_GATES_FIXTURE"/. "$OUT"/
"""


class BackgroundRetryTests(RetryRunBase):
    def setUp(self) -> None:
        super().setUp()
        self.data = self.root / "data"
        self.runroot = self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.receipt = self.data / "control" / "codex-01600-supervisor-enable-20261003t000000z.json"
        self.receipt.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64, "policy_version": 7}), encoding="utf-8")
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text(FAKE_ENTRY_GATES, encoding="utf-8")
        self.gates.chmod(self.gates.stat().st_mode | stat.S_IXUSR)
        # 一次真实的执行器运行：偶发单元首次失败、补跑通过。
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        completed, summary, _manifest = self.run_gates(units, out="exec")
        self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
        self.exec_out = self.root / "exec"
        self.fixture = self.root / "fixture"
        self.fixture.mkdir()
        self.write_entry(summary_path=self.exec_out / "summary.json")

    def write_entry(self, *, summary_path: Path | None, status: str = "passed") -> None:
        body = {"status": status, "gates": [{"gate_id": "gate-flaky:once", "status": status}],
                "source": {"commit": COMMIT, "deploy_receipt": self.receipt.name}}
        if summary_path is not None:
            body["executor_summary"] = str(summary_path)
        (self.fixture / "entry-gates.json").write_text(json.dumps(body), encoding="utf-8")

    def conclusion(self, **extra: object) -> Path:
        deployment = bv.latest_deployment(self.data)
        path = bv.result_path(self.runroot, COMMIT, deployment)
        payload = {
            "schema_version": bv.SCHEMA, "commit": COMMIT, "branch": "b", "bundle": str(self.root / "x.bundle"), "deployment": deployment,
            "profile": "full-gates", "mode": "re-execute", "status": "running", "pid": None, "started_at_utc": "2026-10-25T00:00:00Z",
            "completed_at_utc": None, "out": str(self.root / "bv-out"), "log": str(self.root / "bv.log"), "record_store": None,
            "work": str(self.root / "work"), "input_contract": {}, "cache_roots": {}, "vc_env": None, "entry_gates": str(self.gates),
            "data_root": str(self.data), "runroot": str(self.runroot), "then_dryrun": False, "failed_gates": [], "reason": None,
            "returncode": None, "dryrun": None, **extra}
        bv._write(path, payload)
        return path

    def run_background(self, **extra: object) -> dict:
        path = self.conclusion(**extra)
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        # run() 要求缓存目录合同；用与 _start_locked 相同的派生，工作目录放在临时目录里。
        work = Path(bv._read(path)["work"])
        payload = bv._read(path)
        payload["work"] = str(work)
        payload["cache_roots"] = {"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"), "GOTMPDIR": str(work / "go-tmp")}
        payload["input_contract"] = {"work": str(work)}
        payload["out"] = str(self.root / "bv-out")
        bv._write(path, payload)
        with unittest.mock.patch.object(bv, "_bound_work_roots", return_value=(work,)):
            os.environ["FAKE_GATES_FIXTURE"] = str(self.fixture)
            self.addCleanup(os.environ.pop, "FAKE_GATES_FIXTURE", None)
            bv.run(path)
        return bv._read(path)

    def test_retry_report_lists_the_retry_passed_unit_with_both_logs(self) -> None:
        report = bv.retry_report(self.fixture)
        self.assertEqual(report["problems"], [])
        self.assertEqual([item["unit_id"] for item in report["passed_units"]], ["flaky:once"])
        item = report["passed_units"][0]
        self.assertTrue(Path(item["first_attempt_log"]).is_file() and Path(item["retry_attempt_log"]).is_file())
        self.assertEqual((item["window_rounds"], item["pass_limit"]), (5, 1))

    def test_a_passed_conclusion_lists_the_retry_passed_units(self) -> None:
        payload = self.run_background()
        self.assertEqual(payload["status"], "passed", payload.get("reason"))
        self.assertEqual([item["unit_id"] for item in payload["retry_passed_units"]], ["flaky:once"])

    def test_check_replays_the_marker_and_announces_it(self) -> None:
        self.run_background()
        code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual(code, 0, message)
        self.assertIn("补跑通过的单元 1 个", message)
        self.assertIn("flaky:once", message)

    def test_tampered_log_or_summary_makes_the_conclusion_fail_instead_of_passing(self) -> None:
        summary_file = self.exec_out / "summary.json"
        original = summary_file.read_text(encoding="utf-8")
        first_log = Path(json.loads(original)["retry"]["units"]["flaky:once"]["first_attempt"]["log"])
        first_log.write_text("被改写的日志\n", encoding="utf-8")
        payload = self.run_background()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("补跑标注自检不通过", payload["reason"])
        self.assertIn("日志摘要", payload["reason"])

    def test_hidden_retry_block_is_not_passed(self) -> None:
        summary_file = self.exec_out / "summary.json"
        document = json.loads(summary_file.read_text(encoding="utf-8"))
        del document["retry"]
        summary_file.write_text(json.dumps(document), encoding="utf-8")
        payload = self.run_background()
        self.assertEqual(payload["status"], "failed")
        self.assertIn("藏起来", payload["reason"])

    def test_check_refuses_when_the_marker_no_longer_replays(self) -> None:
        self.run_background()
        document = json.loads((self.exec_out / "summary.json").read_text(encoding="utf-8"))
        Path(document["retry"]["units"]["flaky:once"]["retry_attempt"]["log"]).write_text("改过\n", encoding="utf-8")
        code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual(code, 3)
        self.assertIn("补跑标注不能重放", message)

    def test_conclusions_without_retry_are_unchanged(self) -> None:
        # 总摘要不指向执行器汇总（旧形态／替身）：没有补跑信息可核，结论不多出任何键。
        self.write_entry(summary_path=None)
        payload = self.run_background()
        self.assertEqual(payload["status"], "passed", payload.get("reason"))
        self.assertNotIn("retry_passed_units", payload)
        code, message = bv.check(self.runroot, self.data, require_passed=True)
        self.assertEqual((code, "补跑" in message), (0, False))

    def test_driver_carries_an_identical_copy(self) -> None:
        repo = Path(bv.__file__).resolve().parents[2]
        self.assertEqual((repo / "tools" / "arm64_capture_driver" / "driver" / "background_validation.py").read_bytes(),
                         (repo / "tools" / "ci" / "background_validation.py").read_bytes())


if __name__ == "__main__":
    unittest.main()
