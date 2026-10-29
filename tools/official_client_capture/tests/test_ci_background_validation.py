"""修复轮后台验证（E4-01，``tools/ci/background_validation.py``）：结论按提交与部署收据绑定、批次边界与 VC-5 前核对、
同一提交不重复起、新一轮取代旧一轮、停下时整组终止、部署在途中换了判中止。

入口门禁用替身脚本（按环境变量决定结论、耗时与核对的部署收据），后台进程是真实的新会话子进程。

两客户端都纳管（B5a-10）：一次全量门禁、结论绑定两份部署收据（集合摘要进文件名），入口门禁带 ``--require-deployed-all`` 并须
逐一核对过其余客户端的收据；另一客户端的轮次（另一运行根）start 时搭上同一次验证、不再起；两客户端的批次边界与 VC-5 前
核对各按自己的收据取到这份结论；新部署起新验证时，登记里旧集合的在跑验证一并停下；搭上的轮次 stop 也停得下它、status 列得出
它。只有 Codex 纳管时不写集合、不登记。

跨提交、跨部署的后继（``BackgroundCrossCommitInheritTests``）：H7 一轮后台里一个测试故意失败，结论 failed、批次边界退出 3 拒派；
H8 修好并重新部署（提交与部署收据集合都变了）后，``start --unit-inherit-request … --unit-inherit-source-result <H7 结论>`` 起新
运行，只重跑失败与受影响的单元，其余承接 H7 那一轮实际执行成功的记录，并在新结论里登记每条承接项的来源运行 ID 与记录摘要、四个分开
的计数和每个重跑单元的原因与耗时；来源与批准失效、记录被篡改或过期、审计未通过、祖先关系不成立时拒绝或不承接并给出明确原因。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ci import background_validation as bv
from tools.official_client_capture.tests import background_cross_commit_fixture as cross
from tools.official_client_capture.tests import unit_inherit_fixture as inherit_fixture

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
    --parallel) echo "parallel:$2" >> "$FAKE_GATES_CALLS"; shift 2 ;;
    --resume-from) echo "resume:$2" >> "$FAKE_GATES_CALLS"; shift 2 ;;
    --resume-series) echo "series:$2" >> "$FAKE_GATES_CALLS"; shift 2 ;;
    --profile|--mode|--record-store|--work|--wait-lock) shift 2 ;;
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
        (self.root / "x.bundle").write_bytes(b"isolated-bundle")
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
        if argv[:1] == ("start",) and "--preflight" not in argv:
            # Python 命令行的 start 缺省打开起跑前置检查与沿用（验收修正建议 12）；这些用例测的是别的，显式关掉，缺省行为另有用例。
            argv = (*argv, "--preflight", "off", "--reuse", "off")
        return subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **self.environ, **(env or {})})

    def start(self, commit: str, *, parallel: int | None = None, **env: str) -> dict:
        completed = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                             "--branch", "codex/test", "--commit", commit, "--entry-gates", str(self.gates),
                             *(["--parallel", str(parallel)] if parallel is not None else []), env=env)
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
        self.assertNotIn("parallel", payload)
        self.assertNotIn("parallel", payload["input_contract"], "缺省保持旧合同字段形状")
        self.assertEqual((payload["commit"], payload["deployment"]["receipt"], payload["profile"]),
                         (COMMIT_A, self.receipt.name, "full-gates"))
        self.assertEqual(Path(started["result"]).name, f"{COMMIT_A[:12]}-{bv._sha256_file(self.receipt)[:12]}.json")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["require-deployed", COMMIT_A], "门禁前核对部署")
        self.assertEqual(self.boundary()[0], 0)
        self.assertEqual(self.boundary("require-passed")[0], 0)
        again = self.start(COMMIT_A)
        self.assertEqual((again["action"], again["status"]), ("exists", "passed"), "同一提交＋同一部署不重复起")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split().count(COMMIT_A), 1)

    def test_explicit_parallel_reaches_the_gate_and_binds_reuse(self) -> None:
        started = self.start(COMMIT_A, parallel=2)
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual((payload["parallel"], payload["input_contract"]["parallel"]), (2, 2))
        self.assertEqual(self.calls.read_text().split(), ["require-deployed", "parallel:2", COMMIT_A])
        self.assertEqual(self.start(COMMIT_A, parallel=2)["action"], "exists")
        for changed in ([], ["--parallel", "3"]):
            with self.subTest(changed=changed):
                result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                                  "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test", "--commit", COMMIT_A,
                                  "--entry-gates", str(self.gates), *changed)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("合同不同", result.stderr)
        self.assertEqual(self.calls.read_text().split().count(COMMIT_A), 1, "并发参数不一致不能复用或重复派发")

    def test_invalid_parallel_is_rejected_before_dispatch(self) -> None:
        for value in ("0", "-1", "bad", "", "1.5"):
            with self.subTest(value=value):
                result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                                  "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test", "--commit", COMMIT_A,
                                  "--entry-gates", str(self.gates), "--parallel", value)
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.calls.exists())

    def test_parallel_drift_in_saved_result_aborts_before_dispatch(self) -> None:
        started = self.start(COMMIT_A, parallel=2)
        original = self.wait(started["result"])
        for field, value in (("parallel", 3), ("parallel", 0), ("input_contract", {**original["input_contract"], "parallel": True})):
            with self.subTest(field=field, value=value):
                bv._write(Path(started["result"]), {**original, field: value})
                with patch.object(bv.subprocess, "Popen") as dispatch:
                    self.assertEqual(bv.run(Path(started["result"])), 3)
                    dispatch.assert_not_called()
                self.assertEqual(bv._read(Path(started["result"]))["status"], "aborted")

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

    def test_start_records_the_run_process_start_identity_so_stop_can_verify_the_pid(self) -> None:
        """R7b：结论里记下 run 进程的启动标识（``pid_start``）。pid 被无关进程复用（后台被 SIGKILL 或机器重启而结论仍是 running）时，
        stop 动手前据此核对，对不上就不动。"""

        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="60")
        payload = self.wait(started["result"], until=("running",))
        self.assertTrue(payload["pid_start"])
        self.assertEqual(payload["pid_start"], bv.process_identity(payload["pid"]))

    def test_stop_output_carries_the_notes_when_the_payload_has_no_start_identity(self) -> None:
        """旧结论没有 ``pid_start``：stop 只按进程组处理，并把这一点写进 stop 的输出（``notes``），不再静默。"""

        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="60")
        payload = self.wait(started["result"], until=("running",))
        legacy = {key: value for key, value in payload.items() if key != "pid_start"}
        bv._write(Path(started["result"]), legacy)
        completed = self.cli("stop", "--runroot", str(self.runroot), "--reason", "测试")
        output = json.loads(completed.stdout)
        self.assertEqual(output["stopped"], [Path(started["result"]).name])
        self.assertTrue(any("pid_start" in note for note in output["notes"]), output)
        self.assertFalse(bv._pid_alive(payload["pid"]), "旧结论仍按进程组终止")

    def test_stop_output_has_no_notes_key_when_nothing_needs_saying(self) -> None:
        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="60")
        self.wait(started["result"], until=("running",))
        output = json.loads(self.cli("stop", "--runroot", str(self.runroot)).stdout)
        self.assertEqual(list(output), ["stopped"])

    def test_stop_waits_for_the_whole_group_to_finish_cleaning_up(self) -> None:
        """组长收到 SIGTERM 立即退出（Linux 上随即被回收），组员还在清理：要等整组退出才补发 SIGKILL。原来只等组长，
        执行器终止单元会话做到一半就被杀，排在后面的单元留在后台继续跑（E4-02 验收实测）。这里直接造一个进程组：组长
        ``sleep``，组员是收到 SIGTERM 后花 2 秒「清理」再写标记的 bash；测试进程立即回收组长，模拟 Linux 上的 init。"""

        marker = self.root / "cleaned"
        member = f"trap 'sleep 2; echo cleaned > {marker}; exit 0' TERM; sleep 60 & wait"
        leader = subprocess.Popen(["bash", "-c", f"bash -c {shlex.quote(member)} & exec sleep 60"], start_new_session=True)
        reaper = threading.Thread(target=leader.wait, daemon=True)
        reaper.start()
        time.sleep(0.5)   # 等组员装好 trap
        began = time.monotonic()
        bv._terminate({"pid": leader.pid})
        self.assertTrue(marker.is_file(), "清理做完才补发 SIGKILL")
        self.assertGreaterEqual(time.monotonic() - began, 1.5)
        self.assertFalse(bv._group_alive(leader.pid), "整组都已退出")

    def test_deployment_changed_during_the_run_is_aborted(self) -> None:
        other = self.deploy("20261002t230000z", files="d")   # 门禁核对到的是另一份收据
        payload = self.wait(self.start(COMMIT_A, FAKE_GATES_RECEIPT=str(other))["result"])
        self.assertEqual(payload["status"], "aborted")
        self.assertIn("部署在验证途中换了", payload["reason"])
        self.assertEqual(self.boundary()[0], 3)

    def test_deployment_evidence_removed_during_run_records_aborted(self):
        started = self.start(COMMIT_A, FAKE_GATES_SLEEP="0.3")
        self.receipt.unlink()
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "aborted", payload)
        self.assertIn("部署在验证途中换了", payload["reason"])

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

    def _start_cli(self, commit: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                        "--branch", "codex/test", "--commit", commit, "--entry-gates", str(self.gates), *extra, env=env)

    def test_resume_auto_restarts_a_failed_attempt_and_hands_the_resume_flags_to_the_gate(self) -> None:
        """修好接着跑：同提交、同部署上一次尝试失败后，--resume auto 重新起跑（旧终态留在 history），入口门禁带 --resume-from auto --resume-series bgv；
        不带 --resume 时失败的结论照旧返回 exists（命令行与改造前相同）。"""

        failed = self.wait(self.start(COMMIT_A, FAKE_GATES_STATUS="failed")["result"])
        self.assertEqual(failed["status"], "failed")
        self.assertNotIn("resume", failed)
        plain = self._start_cli(COMMIT_A)
        self.assertEqual((plain.returncode, json.loads(plain.stdout)["action"]), (0, "exists"), "不带 --resume：失败的结论不自动重启")
        resumed = self._start_cli(COMMIT_A, "--resume", "auto")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        started = json.loads(resumed.stdout)
        self.assertEqual(started["action"], "started")
        second = self.wait(started["result"])
        self.assertEqual((second["status"], second["resume"]), ("passed", "auto"))
        self.assertEqual((self.boundary()[0], self.boundary("require-passed")[0]), (0, 0), "续跑得到的通过结论 VC-5 准入（require-passed）与批次边界照常接受")
        self.assertNotIn("resume", second["input_contract"], "续跑只改变哪些单元被执行，不进输入合同")
        calls = self.calls.read_text(encoding="utf-8").split()
        self.assertEqual(calls[-4:], ["require-deployed", "resume:auto", "series:bgv", COMMIT_A])
        self.assertEqual(sum(1 for item in calls if item.startswith("resume:")), 1, "第一次尝试没带续跑参数")
        history = [json.loads(path.read_text()) for path in (self.runroot / "background-validation" / "history").glob("*.json")]
        self.assertTrue(any(item["status"] == "failed" for item in history), "旧的失败终态保留")
        again = self._start_cli(COMMIT_A, "--resume", "auto")
        self.assertEqual(json.loads(again.stdout)["action"], "exists", "已经通过的结论不会因为 --resume auto 被重跑")

    def test_resume_auto_is_refused_with_the_inheritance_and_audit_paths(self) -> None:
        self.wait(self.start(COMMIT_A, FAKE_GATES_STATUS="failed")["result"])
        policy = self.root / "policy.json"
        policy.write_text("{}", encoding="utf-8")
        result = self._start_cli(COMMIT_A, "--resume", "auto", "--audit-policy", str(policy))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume auto 不能与", result.stderr)

    def test_explicit_full_rerun_preserves_old_passed_history(self) -> None:
        first = self.wait(self.start(COMMIT_A)["result"])
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                          "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test", "--commit", COMMIT_A,
                          "--entry-gates", str(self.gates), "--rerun-full-set")
        self.assertEqual(result.returncode, 0, result.stderr)
        started = json.loads(result.stdout)
        self.assertEqual(started["action"], "started")
        second = self.wait(started["result"])
        self.assertEqual(second["mode"], "re-execute")
        self.assertNotEqual(first["out"], second["out"])
        history = list((self.runroot / "background-validation" / "history").glob("*.json"))
        self.assertTrue(any(json.loads(p.read_text())["out"] == first["out"] for p in history))
        self.assertEqual(self.calls.read_text().split().count(COMMIT_A), 2)

    def test_expired_full_set_receipt_automatically_starts_new_full_execution(self) -> None:
        first = self.start(COMMIT_A)
        payload = self.wait(first["result"])
        expired = self.root / "expired.json"
        expired.write_text(json.dumps({"expires_at_utc": "2026-01-01T00:00:00Z", "run_id": "old-full-run"}))
        payload["full_set_receipt"] = {"status": "issued", "receipt": {"path": str(expired), "sha256": bv._sha256_file(expired)}}
        bv._write(Path(first["result"]), payload)
        rerun = self.start(COMMIT_A)
        self.assertEqual(rerun["action"], "started")
        current = self.wait(rerun["result"])
        self.assertEqual(current["full_set_predecessor"], str(expired))
        self.assertNotEqual(payload["out"], current["out"])
        self.assertEqual(json.loads(expired.read_text())["run_id"], "old-full-run")

    def test_background_uses_independent_go_caches_despite_inherited_environment(self):
        # 真实后台子进程写出实际环境；前台清理只删除继承的旧缓存，后台仍完整通过。
        self.gates.write_text(FAKE_ENTRY_GATES.replace('sleep "${FAKE_GATES_SLEEP:-0}"',
            'mkdir -p "$GOCACHE" "$GOMODCACHE" "$GOTMPDIR"\n'
            'printf "%s\\n" "$GOCACHE" "$GOMODCACHE" "$GOTMPDIR" > "$FAKE_CACHE_RECORD"\n'
            'sleep "${FAKE_GATES_SLEEP:-0}"'))
        shared = self.root / "foreground-cache"
        shared.mkdir()
        cache_record = self.root / "cache-paths.txt"
        started = self.start(COMMIT_A, GOCACHE=str(shared), GOMODCACHE=str(shared), GOTMPDIR=str(shared),
                             FAKE_CACHE_RECORD=str(cache_record), FAKE_GATES_SLEEP="0.3")
        shared.rmdir()
        result = self.wait(started["result"])
        self.assertEqual(result["status"], "passed")
        roots = cache_record.read_text().splitlines()
        self.assertEqual(roots, [result["cache_roots"][key] for key in ("GOCACHE", "GOMODCACHE", "GOTMPDIR")])
        self.assertTrue(all(Path(path).is_relative_to(bv.fixed_work_root(self.data)) for path in roots), "缺省工作区是固定测试树坐标（D1）")
        self.assertEqual(len(set(roots)), 3)

    def test_background_refuses_foreground_work_and_changed_bundle(self):
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
            "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A,
            "--work", str(self.root / "entry-gates-work"), "--entry-gates", str(self.gates))
        self.assertEqual(result.returncode, 2, result.stderr)
        first = self.start(COMMIT_A)
        self.wait(first["result"])
        (self.root / "x.bundle").write_bytes(b"changed-bundle")
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
            "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A, "--entry-gates", str(self.gates))
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_concurrent_start_only_launches_one_runner(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.start(COMMIT_A, FAKE_GATES_SLEEP="0.5"))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(result["action"] for result in results), ["exists", "started"])
        self.wait(results[0]["result"])
        self.assertEqual(self.calls.read_text().splitlines().count(COMMIT_A), 1)

    def test_explicit_work_must_bind_current_commit_and_deployment(self):
        sha = bv.latest_deployment(self.data)["sha256"]
        parent = self.runroot / "background-validation/work"
        source_parent = self.data.parent / "entry-gates-source-work"
        for work in (root / name for root in (parent, source_parent)
                     for name in ("shared", f"{COMMIT_B}-{sha}", f"{COMMIT_A}-{'0'*64}")):
            result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A,
                "--work", str(work), "--entry-gates", str(self.gates))
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.calls.exists())
        work = parent / f"{COMMIT_A}-{sha}" / "approved-isolation"
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
            "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A,
            "--work", str(work), "--entry-gates", str(self.gates))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.wait(json.loads(result.stdout)["result"])
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["work"], str(work))
        # 显式来源工作区与状态发布目录分开；先准备全部目录，真实后台进程的原子终态发布不改来源精确目录。
        from tools.ci import read_audit
        source = source_parent / f"{COMMIT_A}-{sha}"
        for leaf in ("go-build-cache", "go-mod-cache", "go-tmp"):
            (source / leaf).mkdir(mode=0o700, parents=True)
        paths = (source, source_parent, source_parent.parent)
        before = [read_audit.host_snapshot(str(path)) for path in paths]
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
            "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A,
            "--work", str(source), "--rerun-full-set", "--entry-gates", str(self.gates))
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(json.loads(result.stdout)["result"])
        current = self.wait(str(path))
        self.assertEqual(current["status"], "passed")
        self.assertEqual(set(current), set(payload), "新坐标不改变旧结果 JSON 形状")
        self.assertEqual([read_audit.host_snapshot(str(item)) for item in paths], before)
        # 合法旧坐标与新坐标都拒绝链接；运行期不能靠修改输入合同挪到其它提交的来源。
        linked = source / "linked"
        linked.symlink_to(source, target_is_directory=True)
        result = self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data),
            "--bundle", str(self.root / "x.bundle"), "--branch", "b", "--commit", COMMIT_A,
            "--work", str(linked), "--entry-gates", str(self.gates))
        self.assertEqual(result.returncode, 2, result.stderr)
        changed = source_parent / f"{COMMIT_B}-{sha}"
        current["work"] = str(changed)
        current["input_contract"]["work"] = str(changed)
        current["cache_roots"] = {key: str(changed / leaf) for key, leaf in
                                  (("GOCACHE", "go-build-cache"), ("GOMODCACHE", "go-mod-cache"), ("GOTMPDIR", "go-tmp"))}
        current["status"] = "running"
        bv._write(path, current)
        calls = self.calls.read_text()
        result = self.cli("run", "--result", str(path))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.calls.read_text(), calls, "篡改工作区不得派发门禁")
        self.assertEqual(bv._read(path)["status"], "aborted")


# 两客户端都纳管时的入口门禁替身：记下部署核对开关与提交；总摘要来源写本轮客户端核对的收据，带 --require-deployed-all 时另写
# FAKE_GATES_OTHERS 给出的「其余客户端=收据文件名」（没设这个变量时不写，模拟没有逐一核对）。
FAKE_TWO_CLIENT_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""; ARGS=(); FLAG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --parallel) echo "parallel:$2" >> "$FAKE_GATES_CALLS"; shift 2 ;;
    --profile|--mode|--record-store|--work|--wait-lock) shift 2 ;;
    --require-deployed|--require-deployed-all) FLAG="$1"; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
echo "$FLAG ${ARGS[2]}" >> "$FAKE_GATES_CALLS"
sleep "${FAKE_GATES_SLEEP:-0}"
mkdir -p "$OUT"
python3 - "$OUT" "${FAKE_GATES_STATUS:-passed}" "$FAKE_GATES_RECEIPT" "${FAKE_GATES_OTHERS-__unset__}" "$FLAG" <<'PY'
import json, sys
out, status, receipt, others, flag = sys.argv[1:6]
source = {"deploy_receipt": receipt}
if flag == "--require-deployed-all" and others != "__unset__":
    source["deploy_receipts_others"] = others
json.dump({"status": status, "gates": [{"gate_id": "test-capture-tools", "status": status}], "source": source},
          open(out + "/entry-gates.json", "w"))
PY
[ "${FAKE_GATES_STATUS:-passed}" = passed ]
"""


class TwoClientBackgroundValidationTests(unittest.TestCase):
    """B5a-10：Codex 与 Claude 都已纳管，两客户端的轮次各有自己的运行根。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        (self.data / "control").mkdir(parents=True)
        self.codex_run, self.claude_run = self.root / "codex-run", self.root / "claude-run"
        for runroot in (self.codex_run, self.claude_run):
            runroot.mkdir()
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text(FAKE_TWO_CLIENT_GATES, encoding="utf-8")
        (self.root / "x.bundle").write_bytes(b"two-client-bundle")
        self.calls = self.root / "calls.log"
        self.codex_receipt, self.claude_receipt = self.deploy("20261007t000000z")
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        for runroot in (self.codex_run, self.claude_run):
            bv.stop(runroot, "测试收尾")

    def deploy(self, stamp: str, *, files: str = "f") -> tuple[Path, Path]:
        """一次交换共享根的部署：两份收据同一个时间戳（B5a-01）。"""

        codex = self.data / "control" / f"codex-01600-supervisor-enable-{stamp}.json"
        codex.write_text(json.dumps({"status": "passed", "tool_files_sha256": files * 64, "policy_version": 7}), encoding="utf-8")
        claude = self.data / "control" / f"claude-managed-deploy-{stamp}.json"
        claude.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": files * 64,
                                                                                           "policy_version": 2}}), encoding="utf-8")
        return codex, claude

    def gates_env(self, client: str, codex: Path | None = None, claude: Path | None = None) -> dict[str, str]:
        codex, claude = codex or self.codex_receipt, claude or self.claude_receipt
        own, other = (codex, claude) if client == "codex" else (claude, codex)
        return {"FAKE_GATES_CALLS": str(self.calls), "FAKE_GATES_RECEIPT": str(own),
                "FAKE_GATES_OTHERS": f"{'claude' if client == 'codex' else 'codex'}={other.name}"}

    def cli(self, *argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if argv[:1] == ("start",) and "--preflight" not in argv:
            argv = (*argv, "--preflight", "off", "--reuse", "off")   # 同上：这些用例不测前置检查与沿用
        return subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})

    def start(self, commit: str, runroot: Path, client: str = "codex", *, parallel: int | None = None, **env: str) -> dict:
        argv = ["start", "--runroot", str(runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                "--branch", "claude/test", "--commit", commit, "--entry-gates", str(self.gates)]
        if client != "codex":
            argv += ["--client", client]
        if parallel is not None:
            argv += ["--parallel", str(parallel)]
        completed = self.cli(*argv, env={**self.gates_env(client), **env})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def boundary(self, runroot: Path, client: str, name: str = "check-boundary") -> tuple[int, str]:
        completed = self.cli(name, "--runroot", str(runroot), "--data-root", str(self.data), "--client", client)
        return completed.returncode, completed.stdout + completed.stderr

    def wait(self, result: str, *, until: tuple[str, ...] = ("passed", "failed", "aborted", "superseded")) -> dict:
        deadline = time.monotonic() + 60
        while True:
            payload = bv._read(Path(result)) or {}
            if payload.get("status") in until:
                return payload
            self.assertLess(time.monotonic(), deadline, f"后台验证没有在时限内到达 {until}：{payload}")
            time.sleep(0.05)

    def test_shared_run_requires_the_same_explicit_parallel(self) -> None:
        started = self.start(COMMIT_A, self.codex_run, parallel=2)
        self.assertEqual(self.wait(started["result"])["status"], "passed")
        attached = self.start(COMMIT_A, self.claude_run, "claude", parallel=2)
        self.assertEqual(attached["result"], started["result"])
        self.assertEqual(self.calls.read_text().split().count("parallel:2"), 1)
        changed = self.cli("start", "--runroot", str(self.claude_run), "--data-root", str(self.data),
                           "--bundle", str(self.root / "x.bundle"), "--branch", "claude/test", "--commit", COMMIT_A,
                           "--entry-gates", str(self.gates), "--client", "claude", "--parallel", "3",
                           env=self.gates_env("claude"))
        self.assertEqual(changed.returncode, 2, changed.stderr)
        self.assertIn("输入合同不同", changed.stderr)

    def test_one_run_serves_both_clients_from_their_own_runroots(self) -> None:
        started = self.start(COMMIT_A, self.codex_run)
        payload = self.wait(started["result"])
        self.assertEqual(payload["status"], "passed", payload)
        deployments = bv.deployment_set(self.data)
        self.assertEqual(sorted(payload["deployments"]), ["claude", "codex"])
        self.assertEqual(payload["deployments"], deployments)
        self.assertEqual(Path(started["result"]).name, f"{COMMIT_A[:12]}-{bv.set_sha256(deployments)[:12]}.json", "文件名用集合摘要")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split(), ["--require-deployed-all", COMMIT_A], "核对每个已纳管客户端")
        registration = bv.registry_path(self.data, COMMIT_A, deployments)
        self.assertEqual(json.loads(registration.read_text(encoding="utf-8"))["result"], started["result"])
        attached = self.start(COMMIT_A, self.claude_run, "claude")
        self.assertEqual((attached["action"], attached["attached"], attached["status"], attached["result"]),
                         ("exists", "claude", "passed", started["result"]), "另一客户端的轮次搭同一次验证，不再起")
        self.assertEqual(self.calls.read_text(encoding="utf-8").split().count(COMMIT_A), 1, "全量门禁两客户端只跑一次")
        for runroot, client in ((self.claude_run, "claude"), (self.codex_run, "claude"), (self.codex_run, "codex"), (self.claude_run, "codex")):
            with self.subTest(runroot=runroot.name, client=client):
                for name in ("check-boundary", "require-passed"):
                    code, message = self.boundary(runroot, client, name)
                    self.assertEqual(code, 0, message)
                    self.assertIn("后台验证通过", message)

    def test_failed_run_refuses_claude_batches_in_another_runroot(self) -> None:
        payload = self.wait(self.start(COMMIT_A, self.codex_run, FAKE_GATES_STATUS="failed")["result"])
        self.assertEqual(payload["status"], "failed")
        code, message = self.boundary(self.claude_run, "claude")
        self.assertEqual(code, 3, message)
        self.assertIn("拒绝派发下一批", message)
        self.assertEqual(self.boundary(self.claude_run, "claude", "require-passed")[0], 3)

    def test_gates_must_check_every_other_bound_receipt(self) -> None:
        for label, others in (("没有逐一核对", None), ("核对的不是绑定的那份", "claude=claude-managed-deploy-20261006t000000z.json")):
            with self.subTest(label):
                environment = self.gates_env("codex")
                if others is None:
                    environment.pop("FAKE_GATES_OTHERS")
                else:
                    environment["FAKE_GATES_OTHERS"] = others
                commit = COMMIT_A if others is None else COMMIT_B
                argv = ["start", "--runroot", str(self.codex_run), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                        "--branch", "claude/test", "--commit", commit, "--entry-gates", str(self.gates), "--preflight", "off", "--reuse", "off"]
                completed = subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120,
                                           env={**{k: v for k, v in os.environ.items() if k != "FAKE_GATES_OTHERS"},
                                                "PYTHONDONTWRITEBYTECODE": "1", **environment})
                self.assertEqual(completed.returncode, 0, completed.stderr)
                payload = self.wait(json.loads(completed.stdout.strip().splitlines()[-1])["result"])
                self.assertEqual(payload["status"], "aborted", payload)
                self.assertIn("没有逐一核对其余客户端绑定的部署收据", payload["reason"])
                self.assertEqual(self.boundary(self.claude_run, "claude")[0], 3)

    def test_new_deployment_starts_a_new_validation_and_stops_the_stale_one(self) -> None:
        old = self.start(COMMIT_A, self.codex_run, FAKE_GATES_SLEEP="60")
        pid = self.wait(old["result"], until=("running",))["pid"]
        codex, claude = self.deploy("20261007t010000z", files="e")
        environment = self.gates_env("claude", codex, claude)
        argv = ["start", "--runroot", str(self.claude_run), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                "--branch", "claude/test", "--commit", COMMIT_B, "--entry-gates", str(self.gates), "--client", "claude"]
        completed = self.cli(*argv, env=environment)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        new = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(new["action"], "started")
        self.assertIn(old["result"], new["superseded"], "旧部署集合的在跑验证（另一运行根）一并停下")
        self.assertEqual(bv._read(Path(old["result"]))["status"], "superseded")
        self.assertFalse(bv._pid_alive(pid))
        payload = self.wait(new["result"])
        self.assertEqual((payload["status"], payload["client"]), ("passed", "claude"))
        for runroot, client in ((self.claude_run, "claude"), (self.codex_run, "codex")):
            with self.subTest(client=client):
                code, message = self.boundary(runroot, client, "require-passed")
                self.assertEqual(code, 0, message)
                self.assertIn(Path(new["result"]).name, message)

    def test_attached_round_can_stop_the_shared_run_before_its_regression(self) -> None:
        # Codex 的轮次发起、Claude 的轮次搭上：Claude 的修复轮跑定向回归前 stop（只给自己的运行根）也要停得下这一次。
        started = self.start(COMMIT_A, self.codex_run, FAKE_GATES_SLEEP="60")
        pid = self.wait(started["result"], until=("running",))["pid"]
        attached = self.start(COMMIT_A, self.claude_run, "claude")
        self.assertEqual((attached["action"], attached["attached"], attached["result"]), ("exists", "claude", started["result"]))
        [listed] = json.loads(self.cli("status", "--runroot", str(self.claude_run)).stdout)
        self.assertEqual((listed["path"], listed["attached"], listed["effective_status"]), (started["result"], True, "running"),
                         "搭上的轮次的 status 列出它正在用的那一次")
        stopped = self.cli("stop", "--runroot", str(self.claude_run), "--reason", "Claude 修复轮跑定向回归前停下")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertIn(Path(started["result"]).name, json.loads(stopped.stdout)["stopped"])
        self.assertEqual(bv._read(Path(started["result"]))["status"], "superseded")
        self.assertFalse(bv._pid_alive(pid), "Claude 轮次的 stop 停下了 Codex 轮次发起的那一次")
        [own] = json.loads(self.cli("status", "--runroot", str(self.codex_run)).stdout)
        self.assertEqual((own["effective_status"], "attached" in own), ("superseded", False), "发起方的 status 照旧只列自己的结论")

    def test_claude_only_redeploy_needs_its_own_validation(self) -> None:
        self.wait(self.start(COMMIT_A, self.codex_run)["result"])
        (self.data / "control" / "claude-managed-deploy-20261007t020000z.json").write_text(
            json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "9" * 64}}), encoding="utf-8")
        code, message = self.boundary(self.claude_run, "claude", "require-passed")
        self.assertEqual(code, 3, message)
        self.assertIn("还没有后台验证", message)
        self.assertEqual(self.boundary(self.codex_run, "codex", "require-passed")[0], 0, "Codex 的收据没变，原结论照样成立")


class BackgroundUnitInheritTests(unittest.TestCase):
    """只验后台新增接线；用原承接夹具生产真实决策和消费证明，不重跑已通过的原生三单元实验。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.world = inherit_fixture.World(self.root / "world", scenarios=())
        self.data = self.world.root / "data"
        self.runroot = self.root / "run"
        self.claude_runroot = self.root / "claude-run"
        self.claude_deploy = self.world.control / "claude-managed-deploy-20261005t000000z.json"
        self.claude_deploy.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "a" * 64}}))
        self.bundle = self.root / "source.bundle"
        self.bundle.write_bytes("后台接线夹具".encode())
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text("# 后台测试替身入口；原决策与消费验证不替换\n")
        self.policy = self.root / "policy.json"
        self.policy.write_text('{"schema_version":"unit-audit-policy/v1","units":{}}\n')
        deployments = bv.deployment_set(self.data)
        self.work = bv._bound_work_roots(self.runroot, self.data, inherit_fixture.COMMIT,
                                        bv.latest_deployment(self.data), deployments)[0]
        self.work.mkdir(parents=True)
        self.marker = self.work / "preserve-source"
        self.marker.write_text("必须保留的来源\n")
        self.context = self.world.context(profile="full-gates", gates=sorted(("check-egress-spec", "test-capture-tools")))
        self.request = self.world.request(context=self.context)
        self.world.sample(inherit_fixture.T0 + 150)
        patches = (patch.object(bv, "_unit_inherit", return_value=inherit_fixture.ui),
                   patch.object(inherit_fixture.ui, "current_time", return_value=inherit_fixture.T0 + 150))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        old_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old_handler)
        self.process = type("NoProcess", (), {"pid": -1})()
        self.original = None

    def arguments(self, request: Path | None = None, *extra: str):
        return bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                          "--bundle", str(self.bundle), "--branch", "codex/claude-upgrade-finish", "--commit", inherit_fixture.COMMIT,
                          "--work", str(self.work), "--record-store", str(self.world.store.root), "--audit-policy", str(self.policy),
                          "--entry-gates", str(self.gates), "--parallel", "2", "--preflight", "off", "--reuse", "off",
                          *(["--unit-inherit-request", str(request)] if request else []), *extra])

    def failed_background(self) -> Path:
        # 后台生命周期单测不另派子进程；正常后台的真实会话与停止由同文件原测试覆盖。
        with patch.object(bv.subprocess, "Popen", return_value=self.process):
            started = bv.start(self.arguments())
        path = Path(started["result"])
        self.original = {**bv._read(path), "status": "failed", "returncode": 1, "failed_gates": ["test-capture-tools"]}
        bv._write(path, self.original)
        return path

    def resume(self) -> Path:
        with patch.object(bv.subprocess, "Popen", return_value=self.process):
            return Path(bv.start(self.arguments(self.request))["result"])

    def gate_result(self, path: Path, *, damage: str | None = None, fallback: str | None = None) -> None:
        """原 Session 逐条判定、封存三个复核点并发布；只改变 beta 输入，所以只有它生成后继正式记录。"""
        payload = bv._read(path)
        out = Path(payload["out"])
        (out / "executor").mkdir(parents=True)
        self.world.declared.clear()
        self.world.declared["test_beta"] = [inherit_fixture.ur.value_entry("tests", "file:test_beta", "修复后的输入")]
        original_builder = self.world.manifest_body
        currents = {unit: self.world.current(unit) for unit in self.world.units}

        def change_all(checkpoint, moment):
            if checkpoint == fallback:
                for unit in self.world.units:
                    self.world.declared[unit] = [inherit_fixture.ur.value_entry("tests", f"file:{unit}", "全部输入已变化")]
                    current = self.world.current(unit)
                    currents[unit].inputs = current.inputs
                    currents[unit].inputs_sha256 = current.inputs_sha256

        if fallback == "queue":
            change_all("queue", inherit_fixture.T0)

        def manifest_body(*args, **kwargs):
            body = original_builder(*args, **kwargs)
            return inherit_fixture.ur.seal({**{key: value for key, value in body.items() if key != "manifest_sha256"},
                                            "out_dir": str(out / "executor")}, "manifest_sha256")

        with patch.object(self.world, "manifest_body", side_effect=manifest_body):
            result = self.world.run(self.request, before=change_all, currents=currents,
                                    context=lambda checkpoint: dict(self.context, client="claude") if fallback == "unauthorized" else self.context)
        self.assertEqual(result.problems, [])
        self.assertEqual(set(result.executed_records), set(self.world.units) if fallback else {"test_beta"})
        self.assertEqual(result.manifest["counts"]["inherited"], 0 if fallback else len(self.world.units) - 1)
        self.result = result
        manifest = out / "executor/unit-manifest.json"
        manifest.write_bytes(result.manifest_path.read_bytes())
        (out / "executor/unit-inherit-decision.json").write_text(json.dumps(result.session.build_decision(result.decisions)))
        gates = [{"gate_id": gate, "status": "passed"} for gate in self.context["gates"]]
        summary = {"status": "passed", "profile": "full-gates", "mode": result.manifest["mode"], "gates": gates,
                   "source": {"commit": inherit_fixture.COMMIT, "deploy_receipt": self.world.deploy.name,
                              "deploy_receipts_others": "claude=" + self.claude_deploy.name},
                   "record_store": str(self.world.store.root), "test_tree": str(self.work / "test-tree"),
                   "unit_manifest": {"path": str(manifest), "manifest_sha256": result.manifest["manifest_sha256"],
                                     "self_check": "passed"}}
        (out / "entry-gates.json").write_text(json.dumps(summary))
        (out / "gates-manifest.json").write_text(json.dumps({"profile": "full-gates", "gates": gates}))
        if damage == "decision":
            self.world.store.decision_path(result.manifest["run_id"]).unlink()
        elif damage == "manifest":
            manifest.unlink()
        elif damage == "gates":
            (out / "gates-manifest.json").write_text(json.dumps({"profile": "full-gates", "gates": gates[:1]}))
        self.world.sample(inherit_fixture.T0 + 150)

    def finish(self, path: Path, *, damage: str | None = None, fallback: str | None = None) -> dict:
        def dispatch(argv, **kwargs):
            self.assertEqual(argv[argv.index("--mode") + 1], "unit-inherit")
            self.assertEqual(argv[argv.index("--work") + 1], str(self.work))
            self.assertEqual(argv[argv.index("--unit-inherit-request") + 1], str(self.request))
            self.assertIn("--require-deployed-all", argv)
            self.assertEqual(kwargs["env"]["GOCACHE"], str(self.work / "go-build-cache"))
            self.gate_result(path, damage=damage, fallback=fallback)
            return type("CompletedGate", (), {"wait": lambda self: 0})()

        with patch.object(bv.subprocess, "Popen", side_effect=dispatch):
            self.assertEqual(bv.run(path), 0)
        return bv._read(path)

    def test_failed_background_resumes_same_work_and_both_clients_consume_the_decision(self) -> None:
        path = self.failed_background()
        originals = {unit: record["record_sha256"] for unit, record in self.world.records.items()}
        self.assertEqual(self.resume(), path)
        payload = self.finish(path)
        self.assertEqual(payload["status"], "passed", payload)
        history = Path(payload["predecessor"]["path"])
        self.assertEqual(json.loads(history.read_text()), self.original)
        self.assertEqual(bv._sha256_file(history), payload["predecessor"]["sha256"])
        self.assertNotEqual(self.original["out"], payload["out"])
        self.assertEqual(self.marker.read_text(), "必须保留的来源\n")
        self.assertEqual(originals, {unit: record["record_sha256"] for unit, record in self.world.records.items()})
        self.assertEqual(payload["unit_inherit_consumption"]["proof"]["executed"], 1)
        for client, runroot in (("codex", self.runroot), ("claude", self.claude_runroot)):
            for require in (False, True):
                with self.subTest(client=client, require=require):
                    code, reason = bv.check(runroot, self.data, client=client, require_passed=require)
                    self.assertEqual(code, 0, reason)
        # 同一成功后继不会再派；没有显式请求的 B-09 行为由原后台测试逐项守住。
        with patch.object(bv.subprocess, "Popen") as process:
            self.assertEqual(bv.start(self.arguments(self.request))["action"], "exists")
            process.assert_not_called()
        missing = json.loads(json.dumps(payload))
        missing["unit_inherit_consumption"]["proof"]["consumption"] = None
        bv._write(path, missing)
        self.assertEqual(bv.check(self.claude_runroot, self.data, client="claude", require_passed=True)[0], 3)
        bv._write(path, payload)
        self.world.store.decision_path(self.result.manifest["run_id"]).unlink()
        for client, runroot in (("codex", self.runroot), ("claude", self.claude_runroot)):
            self.assertEqual(bv.check(runroot, self.data, client=client, require_passed=True)[0], 3)

    def test_all_executed_fallback_requires_authorized_checkpoint_proofs(self) -> None:
        path = self.failed_background()
        for checkpoint in ("queue", "lock", "unauthorized"):
            with self.subTest(checkpoint=checkpoint):
                bv._write(path, self.original)
                self.assertEqual(self.resume(), path)
                payload = self.finish(path, fallback=checkpoint)
                expected = "aborted" if checkpoint == "unauthorized" else "passed"
                self.assertEqual(payload["status"], expected, payload)
                if expected == "passed":
                    proof = payload["unit_inherit_consumption"]["proof"]
                    self.assertEqual((proof["inherited"], proof["executed"]), (0, len(self.world.units)))
                    self.assertIsNotNone(proof["consumption"])
                for client, runroot in (("codex", self.runroot), ("claude", self.claude_runroot)):
                    code, reason = bv.check(runroot, self.data, client=client, require_passed=True)
                    self.assertEqual(code, 3 if expected == "aborted" else 0, reason)

    def test_passed_label_without_complete_decision_cannot_publish_background_success(self) -> None:
        self.failed_background()
        for damage in ("decision", "manifest", "gates"):
            with self.subTest(damage=damage):
                path = self.resume()
                payload = self.finish(path, damage=damage)
                self.assertEqual(payload["status"], "aborted", payload)
                self.assertIn("后台逐单元消费不通过", payload["reason"])
                self.assertEqual(bv.check(self.claude_runroot, self.data, client="claude", require_passed=True)[0], 3)

    def test_missing_approval_wrong_scope_and_b09_mix_are_refused_before_start(self) -> None:
        path = self.failed_background()
        for context in (dict(self.context, client="claude"), dict(self.context, commit="d" * 40),
                        dict(self.context, profile="entry"), dict(self.context, record_store=str(self.root / "other"))):
            with self.subTest(context=context), patch.object(bv.subprocess, "Popen") as process:
                request = self.world.request(context=context)
                with self.assertRaises((bv.ValidationError, inherit_fixture.fs.ContractError)):
                    bv.start(self.arguments(request))
                process.assert_not_called()
        missing = self.world.request(context=self.context, approvals=[])
        with self.assertRaisesRegex(inherit_fixture.fs.ContractError, "批准"):
            bv.start(self.arguments(missing))
        with self.assertRaisesRegex(bv.ValidationError, "B-09"):
            bv.start(self.arguments(self.request, "--rerun-full-set"))
        self.assertEqual(bv._read(path), self.original)

    def test_work_drift_and_still_live_predecessor_are_refused(self) -> None:
        path = self.failed_background()
        with self.assertRaisesRegex(bv.ValidationError, "保留原"):
            bv.start(self.arguments(self.request, "--work", str(self.work / "changed")))
        with patch.object(bv, "_group_alive", return_value=True), self.assertRaisesRegex(bv.ValidationError, "尚未退出"):
            bv.start(self.arguments(self.request))
        self.assertEqual(bv._read(path), self.original)

    def test_request_change_after_start_is_refused_before_gate_dispatch(self) -> None:
        self.failed_background()
        original = self.request.read_bytes()
        for damage in ("bytes", "reference"):
            with self.subTest(damage=damage):
                self.request.write_bytes(original)
                path = self.resume()
                if damage == "bytes":
                    self.request.write_bytes(original + b"\n")
                else:
                    bv._write(path, {**bv._read(path), "unit_inherit_request": {"sha256": "0" * 64}})
                with patch.object(bv.subprocess, "Popen") as process:
                    self.assertEqual(bv.run(path), 3)
                    process.assert_not_called()
                self.assertEqual(bv._read(path)["status"], "aborted")


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """目录树的（相对路径 → 大小、修改时间纳秒）快照：验证祖先关系的读取没有改动测试树里的任何文件。"""

    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(Path(root).rglob("*")) if path.is_file()}


class CrossCommitCase(unittest.TestCase):
    """跨提交后继测试的共同基座：临时目录、SIGTERM 处理器还原、场景构造；``patch_unit_inherit`` 决定后台模块用哪个承接合同实例。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.patch_unit_inherit()
        old_handler = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, old_handler)

    def patch_unit_inherit(self) -> None:
        """缺省：后台模块与夹具共用同一个承接合同实例（时钟钉在替身时刻），与原承接测试同做法。"""

        for patcher in (patch.object(bv, "_unit_inherit", return_value=inherit_fixture.ui),
                        patch.object(inherit_fixture.ui, "current_time", return_value=inherit_fixture.T0 + 150)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scenario(self, *, real_git: bool = False, name: str = "scene", **options) -> cross.Scenario:
        git = cross.GitHistory(self.root / f"{name}-history") if real_git else None
        scene = cross.Scenario(self, self.root / name, git=git, **options)
        scene.start_h7()
        return scene

    def prepared(self, *, real_git: bool = False, **options) -> cross.Scenario:
        """H7 已失败；H8 已修复并重新部署，新提交就绪（真实 git 时另有增量 bundle）。"""

        scene = self.scenario(real_git=real_git, **options)
        scene.deploy_h8()
        scene.prepare_h8()
        if real_git:
            scene.advance_git()
        return scene


class FlipHistory(cross.GitHistory):
    """新提交让 ``input.txt`` 由文件变成同名目录（git 里是一条删除加一条新增，不是类型变化 T）。"""

    def advance(self) -> None:
        self.run("checkout", "-q", cross.BRANCH)
        (self.source / "input.txt").unlink()
        (self.source / "input.txt").mkdir()
        (self.source / "input.txt" / "inner.txt").write_text("目录里的文件\n", encoding="utf-8")
        self.run("add", "-A")
        self.run("commit", "-q", "-m", "文件变目录")
        self.new = self.rev()
        self.run("checkout", "-q", "unrelated")
        self.commit("other.txt", "无关历史线的后继\n", "无关历史线的新提交")
        self.unrelated_new = self.rev()
        self.run("checkout", "-q", cross.BRANCH)


class BackgroundCrossCommitProductionLoaderTests(CrossCommitCase):
    """后台模块不替换承接合同、走生产的按路径加载（与夹具不是同一个模块实例，只经文件交接），整条 H7→H8 路径照样成立。"""

    def patch_unit_inherit(self) -> None:
        sibling = bv._unit_inherit()
        self.assertIsNot(sibling, inherit_fixture.ui, "生产加载的是另一个模块实例")
        for target in (sibling, inherit_fixture.ui):
            patcher = patch.object(target, "current_time", return_value=inherit_fixture.T0 + 150)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_h7_to_h8_works_with_the_modules_loaded_the_production_way(self) -> None:
        scene = self.prepared(real_git=True)
        request = scene.request()
        path = scene.start_successor(request, real_ancestry=True)
        payload = scene.finish_successor(path, request)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 3, "consumer_accepted": 3, "inherited": 3})
        self.assertEqual({item["source_run_id"] for item in payload["inherited_units"]}, {cross.H7_RUN})
        for require in (False, True):
            self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=require)[0], 0)
        # 登记被改也照样被生产加载路径下的批次边界拦住。
        forged = json.loads(json.dumps(payload))
        forged["inherited_units"] = forged["inherited_units"][1:]
        bv._write(path, forged)
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 3)


class BackgroundCrossCommitInheritTests(CrossCommitCase):
    """跨提交、跨部署的后继：H7 一个测试故意失败 → H8 修好并重新部署后只重跑失败与受影响的单元，其余承接 H7 轮实际执行成功的记录。

    世界由 ``background_cross_commit_fixture`` 搭（原承接夹具上的两轮后台）；承接提案用原合同的 ``propose`` 现算，三个复核点、
    决策收据、清单自检与承接消费者都是原合同的真实实现，入口门禁与后台子进程是替身。"""

    def test_h7_failure_refuses_batches_then_h8_inherits_h7_records_in_a_new_run(self) -> None:
        scene = self.scenario(real_git=True)
        h7_before = scene.h7_path.read_bytes()
        # H7：形成 failed 结论，批次边界按各客户端自己的部署收据取到同一份结论，退出 3 拒派。
        for client, runroot in (("codex", scene.runroot), ("claude", scene.claude_runroot)):
            for require in (False, True):
                code, message = bv.check(runroot, scene.data, client=client, require_passed=require)
                self.assertEqual(code, 3, message)
            self.assertIn("拒绝派发下一批", message)
        # 修复并重新部署：当前部署还没有后台记录——仍只提示先起后台（语义不改），要求通过的核对退出 3。
        scene.deploy_h8()
        scene.prepare_h8()
        scene.advance_git()
        code, message = bv.check(scene.runroot, scene.data, require_passed=False)
        self.assertEqual(code, 0, message)
        self.assertIn("还没有后台验证", message)
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 3)
        # 提案用原合同现算：不受影响的单元取 H7 那一轮实际执行成功的新记录（比 SOURCE_RUN 的历史记录新），失败与受影响的、
        # 只能实时重跑的、没有任何来源记录的单元都不在其中。
        sources = scene.propose()
        self.assertEqual(sorted(sources["units"]), sorted(cross.STABLE_UNITS))
        self.assertEqual({item["run_id"] for item in sources["units"].values()}, {cross.H7_RUN})
        self.assertEqual({unit: item["record_sha256"] for unit, item in sources["units"].items()},
                         {unit: scene.h7_records[unit]["record_sha256"] for unit in cross.STABLE_UNITS})
        request = scene.request(sources)
        # 起 H8：新结论（新提交、新部署集合）、沿用 H7 的物理 WORK；旧结论原样不动。
        started = scene.start_h8(request, real_ancestry=True)
        self.assertNotEqual(started, scene.h7_path)
        self.assertEqual(scene.h7_path.read_bytes(), h7_before, "旧运行只作来源，不改它的结论")
        payload = bv._read(started)
        self.assertEqual((payload["status"], payload["mode"], payload["commit"]), ("running", "unit-inherit", scene.c8))
        self.assertNotEqual(payload["deployments"], scene.deployments7)
        self.assertNotEqual(payload["out"], bv._read(scene.h7_path)["out"])
        self.assertEqual(payload["work"], str(scene.work))
        source = payload["unit_inherit_source"]
        self.assertEqual((source["commit"], source["executor_run_id"], source["status"], source["ancestry"]["status"]),
                         (scene.c7, cross.H7_RUN, "failed", "verified"))
        self.assertIn("WORK 测试树对象库", source["ancestry"]["method"])
        self.assertEqual(source["request_units_from_source"], 3, "启动时就提示：请求里有 3 个单元指向来源后台那一轮的执行记录")
        self.assertEqual(scene.last_started["unit_inherit_source"]["request_units_from_source"], 3)
        self.assertNotIn("warning", scene.last_started["unit_inherit_source"])
        self.assertEqual(bv._sha256_file(Path(source["frozen"]["path"])), source["frozen"]["sha256"])
        self.assertEqual(json.loads(Path(payload["predecessor"]["path"]).read_text()), scene.h7_result)
        # 起了新验证、还没结论：批次边界放行（running），要求通过的核对仍拒绝。
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=False)[0], 0)
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 3)
        # H8 跑完：只重跑失败与受影响的单元，其余承接 H7 轮的记录。
        payload = scene.finish_h8(started, request)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(scene.marker.read_text(), "必须保留的来源\n")
        inherited = {item["unit_id"]: item for item in payload["inherited_units"]}
        self.assertEqual(sorted(inherited), sorted(cross.STABLE_UNITS))
        for unit, item in inherited.items():
            self.assertEqual((item["source_run_id"], item["source_record_sha256"], item["source_commit"], item["source_origin"]),
                             (cross.H7_RUN, scene.h7_records[unit]["record_sha256"], scene.c7, "predecessor_run"), unit)
            self.assertEqual(item["source_commit_basis"], "predecessor_result")
            # 是 H7 那一轮实际执行成功的新记录，不是记录库里更早的历史记录（SOURCE_RUN 里同一个单元另有一条更早的）。
            self.assertNotEqual(item["source_record_sha256"], scene.world.records[unit]["record_sha256"], unit)
        # 四个数分开统计（漏斗）：登记 6（实时单元不登记），其中只有 3 个在来源集合里有审计过的来源记录，消费者接受 3，实际承接 3。
        self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 3, "consumer_accepted": 3, "inherited": 3})
        self.assertEqual(payload["inherit_summary"]["request_source_units"], 3)
        reruns = {item["unit_id"]: item for item in payload["rerun_units"]}
        self.assertEqual(set(reruns), {cross.FAILED_UNIT, cross.AFFECTED_UNIT, cross.REALTIME_UNIT, cross.NEW_UNIT})
        self.assertEqual(set(scene.result.executed_records), set(reruns), "进入重跑队列的只有失败、受影响、实时与新增单元")
        self.assertEqual(reruns[cross.FAILED_UNIT]["reason_category"], "failed")
        self.assertIn("inputs_changed", reruns[cross.FAILED_UNIT]["reason_categories"])
        self.assertEqual(reruns[cross.FAILED_UNIT]["changed_inputs"], [f"file:{cross.FAILED_UNIT}"])
        self.assertEqual(reruns[cross.AFFECTED_UNIT]["reason_category"], "inputs_changed")
        self.assertEqual(reruns[cross.REALTIME_UNIT]["reason_category"], "unaudited_realtime")
        self.assertEqual(reruns[cross.NEW_UNIT]["reason_category"], "no_source")
        self.assertEqual(reruns[cross.NEW_UNIT]["predecessor_status"], "absent")
        for item in reruns.values():
            self.assertEqual(item["seconds"], 1.0, "每个重跑单元记录耗时")
            self.assertTrue(item["reason"] and item["reasons"])
            self.assertIs(item["audit_passed"], True, "重跑单元新执行记录的读集审计结论也登记")
        summary = payload["inherit_summary"]
        self.assertEqual((summary["outcome"], summary["planned"], summary["executed"], summary["inherited"],
                          summary["inherited_from_predecessor_run"], summary["inherited_from_earlier_runs"]),
                         ("inherited", 7, 4, 3, 3, 0))
        self.assertEqual((summary["executed_audit_passed"], summary["audit_ready_after_run"], summary["rerun_failed_units"],
                          summary["rerun_seconds"], summary["inherited_source_seconds"]), (4, 7, [], 4.0, 3.0))
        self.assertEqual(set(summary["definitions"]), {"registered", "audit_passed", "consumer_accepted", "inherited", "executed_audit_passed",
                                                       "audit_ready_after_run", "rerun_seconds"}, "结论文件自带四个计数与耗时的口径说明")
        self.assertEqual(summary["predecessor_ancestry"], "verified")
        self.assertEqual(payload["unit_inherit_consumption"]["proof"]["inherited"], 3)
        # 批次边界：两个客户端的通过核对（含承接消费证明与登记重放）都放行；H7 的失败结论不再是当前部署的结论。
        for client, runroot in (("codex", scene.runroot), ("claude", scene.claude_runroot)):
            for require in (False, True):
                code, message = bv.check(runroot, scene.data, client=client, require_passed=require)
                self.assertEqual(code, 0, message)
                self.assertIn("后台验证通过", message)
                self.assertIn("跨提交后继：承接 3 个、重跑 4 个", message)
        self.assertEqual(scene.h7_path.read_bytes(), h7_before)

    def test_the_claude_client_round_takes_the_same_path(self) -> None:
        """真实的修复轮是 Claude 客户端：结论带 client，部署收据取 Claude 的那份，两个客户端的批次边界各按自己的收据取到它。"""

        scene = self.scenario(client="claude")
        self.assertEqual(bv._read(scene.h7_path)["client"], "claude")
        for client, runroot in (("claude", scene.claude_runroot), ("codex", scene.runroot)):
            self.assertEqual(bv.check(runroot, scene.data, client=client, require_passed=False)[0], 3, "H7 失败：两个客户端都拒派")
        scene.deploy_h8()
        scene.prepare_h8()
        request = scene.request()
        path = scene.start_successor(request)
        started = bv._read(path)
        self.assertEqual((started["client"], started["unit_inherit_source"]["client"], started["deployment"]["receipt"]),
                         ("claude", "claude", scene.claude_d8.name))
        finished = scene.finish_successor(path, request)
        self.assertEqual(finished["status"], "passed", finished)
        self.assertEqual(finished["inherit_counts"], {"registered": 6, "audit_passed": 3, "consumer_accepted": 3, "inherited": 3})
        self.assertEqual({item["source_run_id"] for item in finished["inherited_units"]}, {cross.H7_RUN})
        for client, runroot in (("claude", scene.claude_runroot), ("codex", scene.runroot)):
            for require in (False, True):
                code, message = bv.check(runroot, scene.data, client=client, require_passed=require)
                self.assertEqual(code, 0, message)
        # 来源是 Claude 的轮次，换 Codex 起后继（客户端不同）被拒。
        scene.restore_h7()
        self.assert_refused(scene, scene.request(client="codex"), "客户端（来源 claude，本次 codex）", client="codex")

    # ------------------------------------------------------------------ 拒绝：选项、来源状态与身份

    def results(self, scene: cross.Scenario) -> list[str]:
        return sorted(path.name for path in (scene.runroot / "background-validation").glob("*.json"))

    def assert_refused(self, scene: cross.Scenario, request: Path | None, pattern: str, *extra: str, **kwargs) -> None:
        """起后继被拒：抛 ValidationError（中文原因含 ``pattern``），不留下新结论，来源结论原样不动。"""

        before, source_before = self.results(scene), scene.h7_path.read_bytes()
        with self.assertRaisesRegex(bv.ValidationError, pattern):
            scene.start_successor(request, *extra, **kwargs)
        self.assertEqual(self.results(scene), before, "拒绝时不得留下新结论")
        self.assertEqual(scene.h7_path.read_bytes(), source_before)

    def test_source_option_needs_the_request_and_the_ancestry_flag_needs_the_source(self) -> None:
        scene = self.prepared()
        request = scene.request()
        self.assert_refused(scene, None, "--unit-inherit-source-result 只与 --unit-inherit-request 同用")
        self.assert_refused(scene, request, "--allow-unverified-ancestry 只与 --unit-inherit-source-result 同用",
                            "--allow-unverified-ancestry", source=None)

    def test_running_vanished_aborted_or_stopped_sources_are_refused_and_passed_is_accepted(self) -> None:
        scene = self.prepared()
        request = scene.request()
        for label, changes, pattern in (
                ("仍在运行", {"status": "running", "pid": os.getpid(), "completed_at_utc": None}, "仍在运行，拒绝承接"),
                ("进程消失", {"status": "running", "pid": 2 ** 22 + 12345, "completed_at_utc": None}, "进程已不在而没有写结论"),
                ("中止", {"status": "aborted", "reason": "收到信号 15"}, "中止、没有门禁结论"),
                ("被停下", {"status": "superseded", "reason": "被更新的修复轮停下"}, "被停下的运行没有发布运行清单")):
            with self.subTest(label):
                scene.rewrite_h7(**changes)
                self.assert_refused(scene, request, pattern)
        scene.restore_h7()
        with patch.object(bv, "_group_alive", return_value=True):
            self.assert_refused(scene, request, "进程组尚未退出")
        # passed 的来源同样是合法来源（成果完整即可）。
        scene.rewrite_h7(status="passed", returncode=0, failed_gates=[])
        payload = bv._read(scene.start_successor(request))
        self.assertEqual(payload["unit_inherit_source"]["status"], "passed")

    def test_source_must_share_client_group_work_record_store_and_parallel(self) -> None:
        scene = self.prepared()
        request = scene.request()
        own_work = bv._bound_work_roots(scene.runroot, scene.data, scene.c8, bv.latest_deployment(scene.data),
                                        bv.deployment_set(scene.data))[0]
        other_store = str(scene.root / "other-store")
        cases = (
            ("客户端", {"client": "claude"}, {}, "客户端（来源 claude，本次 codex）"),
            ("组合", {"profile": "entry"}, {}, "组合（来源 entry"),
            ("记录库", {"record_store": other_store}, {}, "记录库（来源 "),
            ("来源并发", {"input_contract": {**scene.h7_result["input_contract"], "parallel": 3}}, {}, "调度并发参数（来源 3，本次 2）"),
            ("本次并发", {}, {"parallel": "3"}, "调度并发参数（来源 2，本次 3）"),
            ("WORK", {}, {"work": own_work}, "WORK（来源 "),
        )
        for label, changes, options, pattern in cases:
            with self.subTest(label):
                scene.rewrite_h7(**changes)
                self.assert_refused(scene, request, pattern, **options)
        scene.restore_h7()
        # 伪造来源的提交不能把别处的 WORK 说成来源的：WORK 须在「本轮坐标或来源自己的坐标」所绑定的根里。
        scene.rewrite_h7(commit="1" * 40)
        self.assert_refused(scene, request, "后台工作目录必须绑定")
        scene.rewrite_h7(commit="zz")
        self.assert_refused(scene, request, "缺少提交或部署绑定")
        scene.rewrite_h7(input_contract=["不是对象"])   # 内容残缺的结论不能让命令行抛栈，要明确拒绝
        self.assert_refused(scene, request, "来源后台结论的内容不合法")

    def test_source_file_must_be_a_regular_result_of_this_runroot(self) -> None:
        scene = self.prepared()
        request = scene.request()
        directory = scene.runroot / "background-validation"
        outside = scene.root / "elsewhere.json"
        outside.write_bytes(scene.h7_path.read_bytes())
        wrong_schema = directory / "wrong-schema.json"
        wrong_schema.write_text(json.dumps({"schema_version": "other/v1"}))
        garbage = directory / "garbage.json"
        garbage.write_text("不是 JSON")
        link = directory / "link.json"
        link.symlink_to(scene.h7_path)
        missing = directory / "missing.json"
        for label, source, pattern in (("别的目录", outside, "必须是本运行根的后台结论"), ("格式不对", wrong_schema, "不是 background-validation/v1"),
                                       ("不是 JSON", garbage, "不可读或不是合法 JSON"), ("符号链接", link, "无符号链接的规范绝对路径"),
                                       ("不存在", missing, "不可读或不是合法 JSON")):
            with self.subTest(label):
                self.assert_refused(scene, request, pattern, source=source)
        # 本提交、本部署的当前结论不能当来源（那是同提交同部署的原续作路径）。
        deployment, deployments = bv.latest_deployment(scene.data), bv.deployment_set(scene.data)
        current = bv.result_path(scene.runroot, scene.c8, deployment, deployments)
        bv._write(current, scene.h7_result)
        self.assert_refused(scene, request, "就是本提交与部署的当前结论", source=current)

    def test_source_must_leave_a_published_manifest_and_consistent_summary(self) -> None:
        scene = self.prepared()
        request = scene.request()
        summary_path, manifest_path = scene.h7_out / "entry-gates.json", scene.h7_out / "executor" / "unit-manifest.json"
        published = scene.world.store.manifest_path(cross.H7_RUN)

        def edited(path: Path, change) -> None:
            payload = json.loads(path.read_text())
            change(payload)
            path.write_text(json.dumps(payload))

        cases = (
            ("没有摘要", summary_path, lambda path: path.unlink(), "没有留下入口门禁摘要或逐单元清单"),
            ("没有清单", manifest_path, lambda path: path.unlink(), "没有留下入口门禁摘要或逐单元清单"),
            ("清单被改", manifest_path, lambda path: edited(path, lambda body: body.update(inheritance_max_age_hours=1)), "自摘要不符"),
            ("自检未通过", summary_path, lambda path: edited(path, lambda body: body["unit_manifest"].update(self_check="failed")), "自摘要不符"),
            ("提交不一致", summary_path, lambda path: edited(path, lambda body: body["source"].update(commit="2" * 40)), "入口门禁摘要里的提交"),
            ("没有发布进记录库", published, lambda path: path.unlink(), "没有发布进本记录库"),
        )
        for label, path, damage, pattern in cases:
            with self.subTest(label):
                original = path.read_bytes()
                try:
                    damage(path)
                    self.assert_refused(scene, request, pattern)
                finally:
                    path.write_bytes(original)
        self.assertEqual(bv._read(scene.start_successor(request))["status"], "running", "还原之后可以正常起")

    # ------------------------------------------------------------------ 拒绝：请求与批准

    def test_expired_misscoped_or_unbound_approvals_and_a_foreign_context_are_refused(self) -> None:
        scene = self.prepared()
        sources = scene.propose()
        fs = inherit_fixture.fs
        roles = sorted(fs.APPROVAL_ROLES)

        def approvals(**overrides):
            return lambda binding: [scene.world.approval(role, binding, **overrides) for role in roles]

        wrapped = "后台跨提交承接的请求或批准不成立：专项批准缺失、错绑、已撤回或过期"
        cases = (
            ("批准过期", {"approvals": approvals(expires_at_utc=fs.timestamp(inherit_fixture.T0 - 10))}, wrapped),
            ("批准范围不匹配", {"approvals": approvals(scope="unit-inherit/v0")}, wrapped),
            ("批准绑定的不是本上下文与来源集合", {"approvals": approvals(binding_sha256="0" * 64)}, wrapped),
            ("批准已撤回", {"approvals": approvals(status="revoked")}, wrapped),
            ("缺一类批准", {"approvals": lambda binding: [scene.world.approval(roles[0], binding)]},
             "后台跨提交承接的请求或批准不成立：缺少三方审核及变更批准"),
            ("请求的提交不是本次的", {"commit": scene.c7}, "请求未开启承接，或客户端、提交、部署集合、阶段、组合、记录库错绑"),
        )
        for label, options, pattern in cases:
            with self.subTest(label):
                self.assert_refused(scene, scene.request(sources, **options), pattern)

    # ------------------------------------------------------------------ 祖先关系（真实 git 历史）

    def test_ancestry_is_verified_with_the_work_history_and_a_foreign_history_line_is_refused(self) -> None:
        scene = self.prepared(real_git=True)
        git = scene.git
        before = tree_snapshot(scene.work / "test-tree")
        unrelated = git.bundle(scene.root / "unrelated.bundle", git.unrelated, "unrelated")
        created: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args, **kwargs) -> str:
            created.append(Path(real_mkdtemp(*args, **kwargs)))
            return str(created[-1])

        with patch.object(tempfile, "mkdtemp", side_effect=recording):
            # 新提交建在另一条无关历史线上：旧提交不是它的祖先——确定不是祖先，永远拒绝，显式退化开关也绕不过。
            request = scene.request(commit=git.unrelated_new)
            for extra in ((), ("--allow-unverified-ancestry",)):
                self.assert_refused(scene, request, "不是新提交 [0-9a-f]{12} 的祖先", *extra, real_ancestry=True,
                                    commit=git.unrelated_new, bundle=unrelated, branch="unrelated")
            # bundle 里没有这个分支指向这个提交：参数自相矛盾，同样绕不过。
            self.assert_refused(scene, scene.request(), "bundle 里没有分支 no-such-branch", "--allow-unverified-ancestry",
                                real_ancestry=True, branch="no-such-branch")
        # 验证用的临时对象库用完即删（成功、拒绝两种出口都删），测试树一个字节都不动。
        self.assertEqual(len(created), 3)
        self.assertFalse([path for path in created if path.exists()])
        self.assertEqual(tree_snapshot(scene.work / "test-tree"), before)
        # 旧提交的祖先关系成立：登记验证方式。
        payload = bv._read(scene.start_successor(scene.request(), real_ancestry=True))
        self.assertEqual(payload["unit_inherit_source"]["ancestry"],
                         {"old_commit": scene.c7, "new_commit": scene.c8, "status": "verified",
                          "method": "git merge-base --is-ancestor（临时对象库：WORK 测试树对象库作备用，加新 bundle）；来源提交到新提交之间没有类型变化"})
        self.assertEqual(tree_snapshot(scene.work / "test-tree"), before)

    def test_unverifiable_ancestry_is_refused_unless_explicitly_degraded_and_recorded(self) -> None:
        scene = self.prepared(real_git=True)
        shutil.rmtree(scene.work / "test-tree")   # 没有完整历史：增量 bundle 的前置提交无处可寻
        request = scene.request()
        self.assert_refused(scene, request, "无法验证旧提交 [0-9a-f]{12} 是新提交 [0-9a-f]{12} 的祖先.*--allow-unverified-ancestry",
                            real_ancestry=True)
        payload = bv._read(scene.start_successor(request, "--allow-unverified-ancestry", real_ancestry=True))
        ancestry = payload["unit_inherit_source"]["ancestry"]
        self.assertEqual((ancestry["status"], ancestry["method"]), ("unverified", "none"))
        self.assertIn("bundle 校验失败", ancestry["reason"])
        self.assertEqual(scene.last_started["unit_inherit_source"]["ancestry"], "unverified")
        # 没有测试树但 bundle 自带完整历史（不是增量）：只靠 bundle 就能验证，方式如实写明。
        full = scene.git.bundle(scene.root / "full.bundle", None, cross.BRANCH)
        verified = bv._verify_ancestry(scene.c7, scene.c8, bundle=full, branch=cross.BRANCH, work=scene.work)
        self.assertEqual((verified["status"], verified["method"]),
                         ("verified", "git merge-base --is-ancestor（临时对象库：仅新 bundle 自带历史）；来源提交到新提交之间没有类型变化"))

    def test_ancestry_decisions_for_equal_unknown_and_foreign_commits(self) -> None:
        scene = self.prepared(real_git=True)
        git, work = scene.git, scene.work
        common = {"bundle": scene.h8_bundle, "branch": cross.BRANCH, "work": work}
        self.assertEqual(bv._verify_ancestry(scene.c7, scene.c7, **common),
                         {"old_commit": scene.c7, "new_commit": scene.c7, "status": "verified", "method": "same-commit"})
        unknown = "1" * 40
        with self.assertRaisesRegex(bv.ValidationError, "旧提交 111111111111 不在可用历史里"):
            bv._verify_ancestry(unknown, scene.c8, **common)
        degraded = bv._verify_ancestry(unknown, scene.c8, allow_unverified=True, **common)
        self.assertEqual((degraded["status"], degraded["method"]), ("unverified", "none"))
        with self.assertRaisesRegex(bv.ValidationError, "不是新提交"):
            bv._verify_ancestry(git.unrelated, scene.c8, allow_unverified=True, **common)
        with patch.object(bv, "_git", side_effect=FileNotFoundError("git")):   # 机器上没有 git：无法验证，不是「不是祖先」
            with self.assertRaisesRegex(bv.ValidationError, "找不到可用的 git"):
                bv._verify_ancestry(scene.c7, scene.c8, **common)
            self.assertEqual(bv._verify_ancestry(scene.c7, scene.c8, allow_unverified=True, **common)["status"], "unverified")
        # 同一提交换了部署（重新部署同一提交）：不需要 git 就成立，端到端可用。
        request = scene.request(commit=scene.c7)
        payload = bv._read(scene.start_successor(request, commit=scene.c7, real_ancestry=True))
        self.assertEqual(payload["unit_inherit_source"]["ancestry"]["method"], "same-commit")

    def test_a_same_name_file_to_directory_flip_vetoes_the_successor_even_with_degraded_ancestry(self) -> None:
        """T10 在权威路径上的补丁（已知缺口 R2）：承接视图发现不了“同名文件变目录”，所以来源提交到新提交之间有这类翻转时拒绝起承接后继。
        这是确定的结论（不是“无法验证”），``--allow-unverified-ancestry`` 绕不过；去掉承接请求完整执行不受影响。"""
        git = FlipHistory(self.root / "flip-history")
        scene = cross.Scenario(self, self.root / "flip-scene", git=git)
        scene.start_h7()
        scene.deploy_h8()
        scene.prepare_h8()
        scene.advance_git()
        request = scene.request()
        before = tree_snapshot(scene.work / "test-tree")
        for extra in ((), ("--allow-unverified-ancestry",)):
            with self.subTest(extra=extra):
                self.assert_refused(scene, request, "input.txt：同名文件变成了目录.*完整执行", *extra, real_ancestry=True)
        self.assertEqual(tree_snapshot(scene.work / "test-tree"), before)
        with self.assertRaisesRegex(bv.ValidationError, "类型变化"):
            bv._verify_ancestry(git.old, git.new, bundle=scene.h8_bundle, branch=cross.BRANCH, work=scene.work, allow_unverified=True)

    # ------------------------------------------------------------------ 单元级的不承接：来源失效、被篡改、过期、审计未通过

    def run_successor(self, scene: cross.Scenario, sources: dict, *, damage=None, **request_options):
        request = scene.request(sources, **request_options)
        path = scene.start_successor(request)
        if damage is not None:
            damage()
        return scene.finish_successor(path, request), path

    def rerun_map(self, payload: dict) -> dict:
        return {item["unit_id"]: item for item in payload["rerun_units"]}

    def test_a_tampered_source_record_is_neither_audited_nor_inherited(self) -> None:
        scene = self.prepared()
        sources = scene.propose()
        victim = inherit_fixture.EGRESS[0]

        def tamper() -> None:
            record_path = scene.world.store.record_path(victim, sources["units"][victim]["record_sha256"])
            body = json.loads(record_path.read_text())
            body["seconds"] = 999.0   # 改一个字段而不重算自摘要
            record_path.write_text(json.dumps(body))

        payload, path = self.run_successor(scene, sources, damage=tamper)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(sorted(item["unit_id"] for item in payload["inherited_units"]), sorted(set(cross.STABLE_UNITS) - {victim}))
        # 登记 6、审计通过 2（被篡改的记录不算审计通过）、消费者接受 2、实际承接 2：四个数分开统计。
        self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 2, "consumer_accepted": 2, "inherited": 2})
        item = self.rerun_map(payload)[victim]
        self.assertEqual(item["reason_category"], "source_invalid")
        self.assertIn("记录自摘要不符（被改过）", "；".join(item["reasons"]))
        self.assertEqual(payload["inherit_summary"]["outcome"], "inherited")
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 0)

    def test_a_source_record_without_a_passed_read_audit_is_not_inherited(self) -> None:
        victim = inherit_fixture.EGRESS[1]
        scene = self.prepared(h7_no_audit=(victim,))
        sources = scene.explicit_sources({unit: scene.h7_records[unit] for unit in cross.STABLE_UNITS})
        payload, path = self.run_successor(scene, sources)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(sorted(item["unit_id"] for item in payload["inherited_units"]), sorted(set(cross.STABLE_UNITS) - {victim}))
        self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 2, "consumer_accepted": 2, "inherited": 2})
        item = self.rerun_map(payload)[victim]
        self.assertEqual(item["reason_category"], "audit_not_passed")
        self.assertIn("读集覆盖未闭合", "；".join(item["reasons"]))

    def test_expired_or_revoked_sources_degrade_to_an_explicit_full_rerun_not_a_fake_inheritance(self) -> None:
        for index, label in enumerate(("过期", "撤销")):
            with self.subTest(label):
                scene = self.prepared(name=f"scene-{index}")
                sources = scene.propose()
                damage, options = None, {}
                if label == "过期":
                    options["max_age_hours"] = 0.25   # H7 的记录完成在 30 分钟前
                    hint = "超过承接期限"
                else:
                    def damage() -> None:
                        inherit_fixture.ui.revoke(scene.world.store.root, cross.H7_RUN, reason="来源运行被撤销", actor="验收")
                    hint = "已撤销"
                payload, path = self.run_successor(scene, sources, damage=damage, **options)
                self.assertEqual(payload["status"], "passed", payload)
                self.assertEqual(payload["inherited_units"], [])
                self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 3, "consumer_accepted": 0, "inherited": 0})
                self.assertEqual(payload["inherit_summary"]["outcome"], "full_rerun")
                self.assertIn("整套重新执行", payload["inherit_summary"]["note"])
                self.assertEqual(len(payload["rerun_units"]), 7, "全部单元重新执行")
                reruns = self.rerun_map(payload)
                for unit in cross.STABLE_UNITS:
                    self.assertEqual(reruns[unit]["reason_category"], "source_invalid", unit)
                    self.assertIn(hint, "；".join(reruns[unit]["reasons"]))
                self.assertEqual(payload["unit_inherit_consumption"]["proof"]["inherited"], 0)
                code, message = bv.check(scene.runroot, scene.data, require_passed=True)
                self.assertEqual(code, 0, message)
                self.assertIn("没有任何单元被承接，实际是整套重新执行", message, "批次边界也不静默：整套重跑不冒充承接")

    def test_mixed_sources_register_where_each_inherited_record_came_from(self) -> None:
        scene = self.prepared()
        older, stable = inherit_fixture.EGRESS[1], cross.STABLE_UNITS
        records = {unit: scene.h7_records[unit] for unit in stable}
        records[older] = scene.world.records[older]   # 这一条来自更早的历史（SOURCE_RUN），不是 H7 那一轮
        payload, path = self.run_successor(scene, scene.explicit_sources(records))
        self.assertEqual(payload["status"], "passed", payload)
        inherited = {item["unit_id"]: item for item in payload["inherited_units"]}
        self.assertEqual(sorted(inherited), sorted(stable))
        for unit in set(stable) - {older}:
            self.assertEqual((inherited[unit]["source_run_id"], inherited[unit]["source_origin"], inherited[unit]["source_commit"]),
                             (cross.H7_RUN, "predecessor_run", scene.c7))
        self.assertEqual((inherited[older]["source_run_id"], inherited[older]["source_origin"]), (inherit_fixture.SOURCE_RUN, "earlier_run"))
        self.assertEqual(inherited[older]["source_record_sha256"], scene.world.records[older]["record_sha256"])
        self.assertEqual((inherited[older]["source_commit"], inherited[older]["source_commit_basis"]), (None, None),
                         "解析不出来源运行的提交就如实写空，不猜")
        self.assertEqual((payload["inherit_summary"]["inherited_from_predecessor_run"], payload["inherit_summary"]["inherited_from_earlier_runs"]),
                         (2, 1))

    def test_start_warns_when_the_request_takes_nothing_from_the_source_run(self) -> None:
        scene = self.prepared()
        older = inherit_fixture.EGRESS[1]
        # 请求里只有更早历史里的一条记录，没有任何单元指向来源后台那一轮：来源对这次承接没有贡献，启动时就提示（不拒绝）。
        scene.start_successor(scene.request(scene.explicit_sources({older: scene.world.records[older]})))
        info = scene.last_started["unit_inherit_source"]
        self.assertEqual(info["request_units_from_source"], 0)
        self.assertIn("没有贡献", info["warning"])

    def test_a_failed_successor_still_registers_what_it_inherited_without_a_consumer_count(self) -> None:
        scene = self.prepared()
        request = scene.request()
        path = scene.start_successor(request)
        payload = scene.finish_successor(path, request, returncode=1, failing="test-capture-tools")
        self.assertEqual((payload["status"], payload["failed_gates"]), ("failed", ["test-capture-tools"]))
        self.assertNotIn("inherit_report_error", payload)
        self.assertEqual(payload["inherit_counts"], {"registered": 6, "audit_passed": 3, "consumer_accepted": None, "inherited": 3})
        self.assertEqual(len(payload["inherited_units"]), 3)
        self.assertEqual(payload["inherit_summary"]["rerun_failed_units"], [], "单元都通过，是门禁项失败")
        code, message = bv.check(scene.runroot, scene.data, require_passed=False)
        self.assertEqual(code, 3, message)
        self.assertIn("拒绝派发下一批", message)

    def test_a_rerun_that_fails_again_is_registered_as_such(self) -> None:
        scene = self.prepared()
        scene.world.failing_in_run[cross.H8_RUN] = {cross.FAILED_UNIT}   # 修复没修好：重跑之后 test_alpha 仍然失败
        request = scene.request()
        path = scene.start_successor(request)
        payload = scene.finish_successor(path, request, returncode=1, failing="test-capture-tools")
        self.assertEqual(payload["status"], "failed", payload)
        item = {entry["unit_id"]: entry for entry in payload["rerun_units"]}[cross.FAILED_UNIT]
        self.assertEqual((item["passed"], item["predecessor_status"], item["reason_category"]), (False, "failed", "failed"))
        self.assertEqual(payload["inherit_summary"]["rerun_failed_units"], [cross.FAILED_UNIT])
        self.assertEqual(payload["inherit_summary"]["executed"], 4)

    def test_successor_chain_keeps_the_physical_work_and_resolves_older_source_commits(self) -> None:
        scene = self.prepared()
        request8 = scene.request()
        path8 = scene.start_successor(request8)
        failed8 = scene.finish_successor(path8, request8, returncode=1, failing="test-capture-tools")
        self.assertEqual(failed8["status"], "failed")
        # H8 也失败了；再修复、再部署（H9）：以 H8 的失败结论作来源，物理 WORK 仍是 H7 留下的那个。
        scene.deploy_h9()
        request9 = scene.request(commit=cross.C9)
        path9 = scene.start_successor(request9, commit=cross.C9, source=path8)
        payload = bv._read(path9)
        self.assertEqual((payload["work"], payload["unit_inherit_source"]["executor_run_id"], payload["unit_inherit_source"]["commit"]),
                         (str(scene.work), cross.H8_RUN, scene.c8))
        finished = scene.finish_successor(path9, request9, run_id=cross.H9_RUN, commit=cross.C9, receipts=(scene.codex_d9, scene.claude_d9))
        self.assertEqual(finished["status"], "passed", finished)
        inherited = {item["unit_id"]: item for item in finished["inherited_units"]}
        self.assertEqual(sorted(inherited), sorted([*cross.STABLE_UNITS, cross.FAILED_UNIT, cross.AFFECTED_UNIT, cross.NEW_UNIT]))
        for unit in cross.STABLE_UNITS:   # 更早（H7 轮）的记录：来源提交从那一轮的入口门禁摘要解析
            self.assertEqual((inherited[unit]["source_run_id"], inherited[unit]["source_origin"], inherited[unit]["source_commit"],
                              inherited[unit]["source_commit_basis"]), (cross.H7_RUN, "earlier_run", scene.c7, "entry_gates_summary"))
        for unit in (cross.FAILED_UNIT, cross.AFFECTED_UNIT, cross.NEW_UNIT):   # H8 那一轮实际执行成功的新记录
            self.assertEqual((inherited[unit]["source_run_id"], inherited[unit]["source_origin"], inherited[unit]["source_commit"]),
                             (cross.H8_RUN, "predecessor_run", scene.c8))
        self.assertEqual([item["unit_id"] for item in finished["rerun_units"]], [cross.REALTIME_UNIT])
        self.assertEqual(finished["inherit_counts"], {"registered": 6, "audit_passed": 6, "consumer_accepted": 6, "inherited": 6})
        for require in (False, True):
            self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=require)[0], 0)

    # ------------------------------------------------------------------ 运行期漂移、批次边界重放、重复与重试

    def test_run_refuses_a_drifted_source_registration_before_dispatching_the_gates(self) -> None:
        scene = self.prepared()
        request = scene.request()
        path = scene.start_successor(request)
        original = bv._read(path)
        source = original["unit_inherit_source"]

        def drifted(**changes):
            return {**original, "unit_inherit_source": {**source, **changes}}

        frozen = Path(source["frozen"]["path"])
        frozen_bytes = frozen.read_bytes()
        outside = scene.root / "outside-copy.json"   # 内容与摘要都对，但不在本运行根的 history 目录里
        outside.write_bytes(frozen_bytes)
        stripped = {key: value for key, value in original.items() if key != "unit_inherit_source"}
        cases = (
            ("冻结摘要被改", drifted(frozen={**source["frozen"], "sha256": "0" * 64}), "冻结副本缺失或被改动"),
            ("冻结副本换成别处的拷贝", drifted(frozen={"path": str(outside), "sha256": source["frozen"]["sha256"]}), "冻结副本缺失或被改动"),
            ("来源 WORK 被改", drifted(work=str(scene.root / "elsewhere")), "来源登记与输入合同不一致"),
            ("来源运行被改", drifted(executor_run_id="20261005t000000z-1-forged"), "来源登记与输入合同不一致"),
            ("登记被删而合同还带指纹", stripped, "结论里却没有来源登记"),
        )
        for label, payload, pattern in cases:
            with self.subTest(label):
                bv._write(path, payload)
                with patch.object(bv.subprocess, "Popen") as process:
                    self.assertEqual(bv.run(path), 3)
                    process.assert_not_called()
                aborted = bv._read(path)
                self.assertEqual(aborted["status"], "aborted")
                self.assertRegex(aborted["reason"], pattern)
        bv._write(path, original)
        frozen.write_bytes(frozen_bytes + b"\n")   # 冻结副本的内容被动过
        with patch.object(bv.subprocess, "Popen") as process:
            self.assertEqual(bv.run(path), 3)
            process.assert_not_called()
        self.assertIn("冻结副本缺失或被改动", bv._read(path)["reason"])

    def test_boundary_replays_the_registration_against_the_manifest_decision_and_request(self) -> None:
        scene = self.prepared()
        request = scene.request()
        path = scene.start_successor(request)
        passed = scene.finish_successor(path, request)
        self.assertEqual(passed["status"], "passed", passed)
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 0)
        for label, change in (
                ("少登一条承接项", lambda body: body.update(inherited_units=body["inherited_units"][1:])),
                ("来源记录摘要被改", lambda body: body["inherited_units"][0].update(source_record_sha256="0" * 64)),
                ("来源运行被改", lambda body: body["inherited_units"][0].update(source_run_id=inherit_fixture.SOURCE_RUN)),
                ("计数被改", lambda body: body["inherit_counts"].update(inherited=body["inherit_counts"]["inherited"] + 1)),
                ("重跑类别被改", lambda body: body["rerun_units"][0].update(reason_category="failed")),
                ("登记整个删掉", lambda body: body.pop("inherited_units")),
                ("来源登记被删而合同还带指纹", lambda body: body.pop("unit_inherit_source"))):
            with self.subTest(label):
                forged = json.loads(json.dumps(passed))
                change(forged)
                bv._write(path, forged)
                code, message = bv.check(scene.runroot, scene.data, require_passed=True)
                self.assertEqual(code, 3, message)
                self.assertIn("后台跨提交承接登记不能重放", message)
                self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=False)[0], 3)
        bv._write(path, passed)
        self.assertEqual(bv.check(scene.runroot, scene.data, require_passed=True)[0], 0)

    def test_repeating_the_start_is_idempotent_and_a_different_source_is_a_different_contract(self) -> None:
        scene = self.prepared()
        request = scene.request()
        path = scene.start_successor(request)
        scene.finish_successor(path, request)
        again = scene.start_successor(request)
        self.assertEqual((again, scene.last_started["action"], scene.last_started["status"]), (path, "exists", "passed"))
        frozen_copy = Path(bv._read(path)["unit_inherit_source"]["frozen"]["path"])   # 内容相同、路径不同的来源
        self.assert_refused(scene, request, "输入或缓存隔离合同不同", source=frozen_copy)

    def test_a_transiently_aborted_attempt_can_be_retried_against_the_same_source(self) -> None:
        scene = self.prepared()
        request = scene.request()
        path = scene.start_successor(request)
        attempt = bv._read(path)
        bv._write(path, {**attempt, "status": "aborted", "reason": "机器重启", "returncode": 3})
        # 重试漏带来源选项：认不出沿用来源的物理 WORK，明确提示，不留下新结论、不改上一次尝试。
        before = path.read_bytes()
        with self.assertRaisesRegex(bv.ValidationError, "上一次尝试是跨提交后继.*重试须带同一个 --unit-inherit-source-result"):
            scene.start_successor(request, source=None)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(scene.start_successor(request), path)
        retried = bv._read(path)
        self.assertEqual((retried["status"], retried["predecessor"]["status"]), ("running", "aborted"))
        self.assertEqual(retried["unit_inherit_source"]["executor_run_id"], cross.H7_RUN)
        final = scene.finish_successor(path, request)
        self.assertEqual(final["status"], "passed", final)
        self.assertEqual(len(final["inherited_units"]), 3)

    # ------------------------------------------------------------------ 兼容：原续作路径与命令行

    def test_same_commit_same_deployment_resume_keeps_its_shape_and_never_needs_the_source(self) -> None:
        scene = self.scenario()
        request = scene.request(commit=scene.c7)
        path = scene.start_successor(request, commit=scene.c7, source=None, bundle=scene.dummy_bundle)
        self.assertEqual(path, scene.h7_path, "原路径：同提交同部署续作写回同一个结论文件")
        payload = bv._read(path)
        self.assertEqual((payload["status"], payload["predecessor"]["status"]), ("running", "failed"))
        self.assertNotIn("unit_inherit_source", payload)
        self.assertNotIn("unit_inherit_source", payload["input_contract"])
        self.assertNotIn("unit_inherit_source", scene.last_started)
        self.assertEqual(payload["work"], str(scene.work))

    def test_command_line_reports_a_refused_source_with_exit_code_two(self) -> None:
        scene = self.prepared()
        request = scene.request()
        scene.rewrite_h7(status="aborted", reason="收到信号 15")
        stderr = io.StringIO()
        argv = scene.argv(commit=scene.c8, bundle=scene.h8_bundle, request=request, source=scene.h7_path)
        with contextlib.redirect_stderr(stderr), patch.object(bv, "_verify_ancestry", side_effect=scene.verified):
            self.assertEqual(bv.main(argv), 2)
        self.assertIn("后台验证：来源后台中止、没有门禁结论", stderr.getvalue())
        self.assertEqual(self.results(scene), [scene.h7_path.name])

    # ------------------------------------------------------------------ 重跑原因分类（纯函数）

    def test_rerun_reasons_are_classified_from_structured_evidence_before_text(self) -> None:
        def entry(reasons=(), **fields):
            return {"unit_id": "u", "disposition": "executed", "reasons": list(reasons), "inheritable": True, **fields}

        def record(*names, **fields):
            return {"inputs": [{"name": name, "sha256": digest} for name, digest in names], "spec_sha256": "s", "environment_sha256": "e",
                    "executor": {"sha256": "x"}, "policy_sha256": "p", **fields}

        failed = {"passed": False}
        passed = {"passed": True}
        same, other = record(("file:a", "1")), record(("file:a", "2"))
        host_noise = record(("file:a", "1"), ("host:/etc", "9"), ("runtime-contract", "8"), ("require-read-audit", "7"))
        cases = (
            ("前序失败", entry(["来源集合没有登记这个单元：执行"]), failed, same, same, True, "failed", ["failed"]),
            ("来源记录没通过", entry(["来源记录不能承接：该次执行没通过（有 1 个测试没通过）"]), passed, same, same, True, "failed", ["failed"]),
            ("清单项不可承接", entry(["来源集合没有登记这个单元：执行"], inheritable=False), passed, same, same, False, "unaudited_realtime",
             ["unaudited_realtime"]),
            ("输入合同未闭合文字", entry(["输入合同未闭合：只验证本次执行结果，禁止承接"]), None, None, None, False, "unaudited_realtime",
             ["unaudited_realtime"]),
            ("读集未闭合", entry(["来源记录不能承接：读集覆盖未闭合，不能承接"]), passed, same, same, True, "audit_not_passed",
             ["audit_not_passed"]),
            ("记录被改", entry(["来源记录不能承接：记录自摘要不符（被改过）"]), passed, same, same, True, "source_invalid", ["source_invalid"]),
            ("过期", entry(["来源记录不能承接：超过承接期限（2.0 小时前完成，期限 1.0 小时）"]), passed, same, same, True, "source_invalid",
             ["source_invalid"]),
            ("来源运行已撤销", entry(["来源运行 r1 已撤销：只重跑指向它的单元"]), passed, same, same, True, "source_invalid", ["source_invalid"]),
            ("输入变了（结构化）", entry(["来源集合没有登记这个单元：执行"]), passed, same, other, False, "inputs_changed", ["inputs_changed"]),
            ("输入变了（文字）", entry(["来源记录不能承接：输入变了：file:a"]), None, None, None, True, "inputs_changed", ["inputs_changed"]),
            ("宿主快照等易变项不算输入变化", entry(["来源集合没有登记这个单元：执行"]), passed, same, host_noise, False, "no_source", ["no_source"]),
            ("复核点拒绝", entry(["逐单元承接排队前复核拒绝：校时失败"]), passed, same, same, True, "session_refused", ["session_refused"]),
            ("没有前序也没登记", entry(["来源集合没有登记这个单元：执行"]), None, None, None, False, "no_source", ["no_source"]),
            ("登记了却说不清", entry([]), passed, same, same, True, "other", ["other"]),
            ("失败又改了输入", entry(["来源集合没有登记这个单元：执行"]), failed, same, other, False, "failed", ["failed", "inputs_changed"]),
        )
        for label, row, before, old, new, registered, primary, categories in cases:
            with self.subTest(label):
                found = bv._classify_rerun(row, before, old, new, registered=registered)
                self.assertEqual((found["reason_category"], found["reason_categories"]), (primary, categories), found)
                self.assertTrue(found["reason"])
        context = bv._classify_rerun(entry(), passed, record(("file:a", "1")), record(("file:a", "1"), executor={"sha256": "y"}),
                                     registered=False)
        self.assertEqual((context["reason_category"], context["changed_context"]), ("inputs_changed", ["执行器"]))


if __name__ == "__main__":
    unittest.main()
