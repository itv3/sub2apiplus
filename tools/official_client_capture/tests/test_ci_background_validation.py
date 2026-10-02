"""修复轮后台验证（E4-01，``tools/ci/background_validation.py``）：结论按提交与部署收据绑定、批次边界与 VC-5 前核对、
同一提交不重复起、新一轮取代旧一轮、停下时整组终止、部署在途中换了判中止。

入口门禁用替身脚本（按环境变量决定结论、耗时与核对的部署收据），后台进程是真实的新会话子进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.ci import background_validation as bv

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools" / "ci" / "background_validation.py"
DRIVER_COPY = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background_validation.py"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40

# 入口门禁替身：解析 --out 与最后三个位置参数，按 FAKE_GATES_* 环境变量睡一会儿、写总摘要并退出。
FAKE_ENTRY_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""; ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --profile|--mode|--record-store|--work) shift 2 ;;
    --require-deployed) echo "require-deployed" >> "$FAKE_GATES_CALLS"; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
echo "${ARGS[2]}" >> "$FAKE_GATES_CALLS"
sleep "${FAKE_GATES_SLEEP:-0}"
mkdir -p "$OUT"
STATUS="${FAKE_GATES_STATUS:-passed}"
python3 - "$OUT" "$STATUS" "${FAKE_GATES_RECEIPT:-}" "${ARGS[2]}" <<'PY'
import json, sys
out, status, receipt, commit = sys.argv[1:5]
gates = [{"gate_id": "backend-go-test", "status": "passed"}, {"gate_id": "test-capture-tools", "status": status}]
json.dump({"status": status, "gates": gates, "source": {"commit": commit, "deploy_receipt": receipt}}, open(out + "/entry-gates.json", "w"))
PY
[ "$STATUS" = passed ]
"""


class BackgroundValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        self.runroot = self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text(FAKE_ENTRY_GATES, encoding="utf-8")
        self.calls = self.root / "calls.log"
        self.receipt = self.deploy("20261003t000000z")
        self.environ = {"FAKE_GATES_CALLS": str(self.calls), "FAKE_GATES_RECEIPT": str(self.receipt)}
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        if self.runroot.is_dir():
            bv.stop(self.runroot, "测试收尾")

    def deploy(self, stamp: str, *, files: str = "f") -> Path:
        path = self.data / "control" / f"codex-01600-supervisor-enable-{stamp}.json"
        path.write_text(json.dumps({"status": "passed", "tool_files_sha256": files * 64, "policy_version": 7}), encoding="utf-8")
        return path

    def cli(self, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **self.environ, **(env or {})})

    def start(self, commit: str, **env: str) -> dict:
        completed = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                             "--branch", "codex/test", "--commit", commit, "--entry-gates", str(self.gates), env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def wait(self, result: str, *, until: tuple[str, ...] = ("passed", "failed", "aborted", "superseded")) -> dict:
        deadline = time.monotonic() + 60
        while True:
            payload = bv._read(Path(result)) or {}
            if payload.get("status") in until:
                return payload
            self.assertLess(time.monotonic(), deadline, f"后台验证没有在时限内到达 {until}：{payload}")
            time.sleep(0.05)

    def boundary(self, name: str = "check-boundary") -> tuple[int, str]:
        completed = self.cli(name, "--runroot", str(self.runroot), "--data-root", str(self.data))
        return completed.returncode, completed.stdout + completed.stderr

    def test_passed_result_is_bound_to_commit_and_deployment(self) -> None:
        started = self.start(COMMIT_A)
        self.assertEqual(started["action"], "started")
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual((payload["commit"], payload["deployment"]["receipt"], payload["profile"]),
                         (COMMIT_A, self.receipt.name, "full-gates"))
        self.assertEqual(Path(started["result"]).name, f"{COMMIT_A[:12]}-{bv._sha256_file(self.receipt)[:12]}.json")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["require-deployed", COMMIT_A], "门禁前核对部署")
        self.assertEqual(self.boundary()[0], 0)
        self.assertEqual(self.boundary("require-passed")[0], 0)
        again = self.start(COMMIT_A)
        self.assertEqual((again["action"], again["status"]), ("exists", "passed"), "同一提交＋同一部署不重复起")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split().count(COMMIT_A), 1)

    def test_failed_result_refuses_the_next_batch_and_vc5_acceptance(self) -> None:
        payload = self.wait(self.start(COMMIT_A, FAKE_GATES_STATUS="failed")["result"])
        self.assertEqual((payload["status"], payload["failed_gates"]), ("failed", ["test-capture-tools"]))
        code, message = self.boundary()
        self.assertEqual(code, 3, message)
        self.assertIn("拒绝派发下一批", message)
        self.assertIn("test-capture-tools", message)
        self.assertEqual(self.boundary("require-passed")[0], 3)

    def test_running_allows_batches_but_not_vc5_acceptance(self) -> None:
        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="30")
        self.wait(started["result"], until=("running",))
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("还在跑", message)
        code, message = self.boundary("require-passed")
        self.assertEqual(code, 3, message)

    def test_missing_result_only_hints_at_batches_and_blocks_vc5_acceptance(self) -> None:
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("还没有后台验证", message)
        self.assertEqual(self.boundary("require-passed")[0], 3)
        self.wait(self.start(COMMIT_A)["result"])
        self.deploy("20261003t010000z", files="e")   # 新部署：旧结论不绑定它
        code, message = self.boundary("require-passed")
        self.assertEqual(code, 3, message)
        self.assertIn("还没有后台验证", message)

    def test_new_round_supersedes_the_running_one_and_kills_its_group(self) -> None:
        old = self.start(COMMIT_A, FAKE_GATES_SLEEP="60")
        running = self.wait(old["result"], until=("running",))
        old_pid = running["pid"]
        new = self.start(COMMIT_B)
        self.assertEqual(new["superseded"], [Path(old["result"]).name])
        superseded = bv._read(Path(old["result"]))
        self.assertEqual(superseded["status"], "superseded", "先标 superseded 再终止：后台 run 收尾时不得改写成 aborted")
        self.assertFalse(bv._pid_alive(old_pid), "被取代的后台验证整组终止")
        self.assertEqual(self.wait(new["result"])["status"], "passed")

    def test_stop_marks_superseded_and_boundary_only_hints(self) -> None:
        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="60")
        pid = self.wait(started["result"], until=("running",))["pid"]
        completed = self.cli("stop", "--runroot", str(self.runroot), "--reason", "修复轮跑定向回归前停下")
        self.assertEqual(json.loads(completed.stdout)["stopped"], [Path(started["result"]).name])
        payload = bv._read(Path(started["result"]))
        self.assertEqual((payload["status"], payload["reason"]), ("superseded", "修复轮跑定向回归前停下"))
        self.assertFalse(bv._pid_alive(pid))
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("被停下", message)
        restarted = self.start(COMMIT_A)
        self.assertEqual(restarted["action"], "started", "被停下的可以重新起")

    def test_deployment_changed_during_the_run_is_aborted(self) -> None:
        other = self.deploy("20261002t230000z", files="d")   # 门禁核对到的是另一份收据
        payload = self.wait(self.start(COMMIT_A, FAKE_GATES_RECEIPT=str(other))["result"])
        self.assertEqual(payload["status"], "aborted")
        self.assertIn("部署在验证途中换了", payload["reason"])
        self.assertEqual(self.boundary()[0], 3)

    def test_vanished_runner_counts_as_no_result_and_can_be_restarted(self) -> None:
        deployment = bv.latest_deployment(self.data)
        path = bv.result_path(self.runroot, COMMIT_A, deployment)
        bv._write(path, {"schema_version": bv.SCHEMA, "commit": COMMIT_A, "deployment": deployment, "status": "running",
                         "pid": 2 ** 22 + 12345, "started_at_utc": "2026-10-03T00:00:00Z"})
        code, message = self.boundary()
        self.assertEqual(code, 0, message)
        self.assertIn("进程已经不在", message)
        self.assertEqual(self.start(COMMIT_A)["action"], "started")

    def test_start_rejects_short_commit_and_relative_paths(self) -> None:
        completed = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", "x.bundle",
                             "--branch", "b", "--commit", COMMIT_A)
        self.assertEqual(completed.returncode, 2)
        completed = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x"),
                             "--branch", "b", "--commit", "abc")
        self.assertEqual(completed.returncode, 2)

    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual(DRIVER_COPY.read_bytes(), MODULE.read_bytes())


if __name__ == "__main__":
    unittest.main()
