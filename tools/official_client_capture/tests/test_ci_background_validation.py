"""修复轮后台验证（E4-01，``tools/ci/background_validation.py``）：结论按提交与部署收据绑定、批次边界与 VC-5 前核对、
同一提交不重复起、新一轮取代旧一轮、停下时整组终止、部署在途中换了判中止。

入口门禁用替身脚本（按环境变量决定结论、耗时与核对的部署收据），后台进程是真实的新会话子进程。

两客户端都纳管（B5a-10）：一次全量门禁、结论绑定两份部署收据（集合摘要进文件名），入口门禁带 ``--require-deployed-all`` 并须
逐一核对过其余客户端的收据；另一客户端的轮次（另一运行根）start 时搭上同一次验证、不再起；两客户端的批次边界与 VC-5 前
核对各按自己的收据取到这份结论；新部署起新验证时，登记里旧集合的在跑验证一并停下；搭上的轮次 stop 也停得下它、status 列得出
它。只有 Codex 纳管时不写集合、不登记。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
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
        self.assertTrue(all(Path(path).is_relative_to(self.runroot / "background-validation/work") for path in roots))
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
        for work in (parent / "shared", parent / f"{COMMIT_B}-{sha}", parent / f"{COMMIT_A}-{'0'*64}"):
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


# 两客户端都纳管时的入口门禁替身：记下部署核对开关与提交；总摘要来源写本轮客户端核对的收据，带 --require-deployed-all 时另写
# FAKE_GATES_OTHERS 给出的「其余客户端=收据文件名」（没设这个变量时不写，模拟没有逐一核对）。
FAKE_TWO_CLIENT_GATES = r"""#!/bin/bash
set -euo pipefail
OUT=""; ARGS=(); FLAG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
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
        return subprocess.run([sys.executable, "-B", str(MODULE), *argv], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})

    def start(self, commit: str, runroot: Path, client: str = "codex", **env: str) -> dict:
        argv = ["start", "--runroot", str(runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                "--branch", "claude/test", "--commit", commit, "--entry-gates", str(self.gates)]
        if client != "codex":
            argv += ["--client", client]
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
                        "--branch", "claude/test", "--commit", commit, "--entry-gates", str(self.gates)]
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


if __name__ == "__main__":
    unittest.main()
