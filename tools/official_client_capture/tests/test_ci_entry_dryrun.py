"""入口空跑（E4-02，``tools/ci/entry_dryrun.py``）：演练根与演练总账、演练参数只改产物坐标、干净环境里跑编排器、
结论按提交与部署绑定、开工空跑重新执行全集带审计、有采集在跑就让路、最远到 P0 收据；后台验证通过后接着空跑。

编排器用替身脚本（读演练参数文件里的 RUNROOT，按替身配置写运行汇总并退出），演练总账用数据根里的替身模块；后台进程是
真实的新会话子进程。
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
from tools.ci import entry_dryrun as dr
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE = REPO_ROOT / "tools" / "ci" / "entry_dryrun.py"
BV_MODULE = REPO_ROOT / "tools" / "ci" / "background_validation.py"
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
COMMIT = "e" * 40

# 编排器替身：记下参数与环境，按同目录 fake-entry.json 写运行汇总（env -i 之下看不到测试的环境变量，只能读文件）。
FAKE_ENTRY = r"""#!/bin/bash
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
python3 - "$HERE" "$@" <<'PY'
import json, os, sys
from pathlib import Path
here, argv = Path(sys.argv[1]), sys.argv[2:]
values = {}
for line in open(os.environ["ARM64_VC_ENV"], encoding="utf-8"):
    key, _, value = line.strip().partition("=")
    values[key] = value.strip('"')
config = json.loads((here / "fake-entry.json").read_text(encoding="utf-8"))
with open(here / "calls.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "environ": sorted(os.environ), "env_file": values, "pid": os.getpid()}) + "\n")
if config.get("sleep"):
    import time
    time.sleep(config["sleep"])
run = Path(values["RUNROOT"]) / "entry-runs" / "20261003t000000z"
run.mkdir(parents=True, exist_ok=True)
steps = [{"step_id": step, "action": "执行", "status": "passed", "reasons": []} for step in config.get("passed", [])]
steps += [{"step_id": step, "action": "执行", "status": "failed", "reasons": [f"{step} 没通过"]} for step in config.get("failed", [])]
steps += [{"step_id": step, "action": "被阻塞", "status": "", "reasons": ["依赖没过"]} for step in config.get("blocked", [])]
summary = {"steps": steps, "exit_code": config.get("rc", 0)}
if config.get("failed"):
    summary["stopped_because"] = "建账本之前有步骤没通过：" + ", ".join(config["failed"])
(run / "run.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
sys.exit(config.get("rc", 0))
PY
"""

# 项目总账模块替身：create-project-ledger 建目录、记下参数（核对 --fixture-only）。
STUB_LEDGER = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
ledger = Path(args[args.index("--ledger-dir") + 1])
ledger.mkdir(parents=True)
(ledger / "plan.json").write_text(json.dumps({"argv": args}), encoding="utf-8")
print(json.dumps({"status": "created"}))
"""


class EntryDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.fixture = driver_tests._DriverFixture(self.root)
        self.data = self.fixture.data_root
        self.runroot = self.fixture.runroot
        for relative, text in {"tools/__init__.py": "", "tools/official_client_capture/__init__.py": "",
                               "tools/official_client_capture/codex_upgrade_project_ledger.py": STUB_LEDGER}.items():
            path = self.data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.data / "control" / "codex-01600-supervisor-enable-20261003t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.entry_dir = self.root / "fake-driver"
        self.entry_dir.mkdir()
        self.entry = self.entry_dir / "entry.sh"
        self.entry.write_text(FAKE_ENTRY, encoding="utf-8")
        self.configure()
        self.state = self.root / "executor-state"
        self.environ = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_STATE_DIR": str(self.state)}

    def configure(self, **config: object) -> None:
        (self.entry_dir / "fake-entry.json").write_text(json.dumps({"passed": ["entry-preflight", "entry-gates", "pre-a3"], **config}),
                                                      encoding="utf-8")

    def cli(self, *argv: str, module: Path = MODULE) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(module), *argv], capture_output=True, text=True, timeout=120, env=self.environ)

    def start(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli("start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--vc-env", str(self.fixture.env_file),
                        "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test", "--commit", COMMIT,
                        "--entry", str(self.entry), *extra)

    def wait(self, result: str) -> dict:
        deadline = time.monotonic() + 60
        while True:
            payload = dr._read(Path(result)) or {}
            if payload.get("status") not in (None, "running"):
                return payload
            self.assertLess(time.monotonic(), deadline, f"空跑没有在时限内结束：{payload}")
            time.sleep(0.05)

    def calls(self) -> list[dict]:
        path = self.entry_dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def test_dry_run_in_rehearsal_root_with_only_artifact_coordinates_rewritten(self) -> None:
        started = self.start()
        self.assertEqual(started.returncode, 0, started.stderr)
        outcome = json.loads(started.stdout.strip().splitlines()[-1])
        self.assertEqual((outcome["action"], outcome["to"]), ("started", "atomic-double"))
        payload = self.wait(outcome["result"])
        self.assertEqual(payload["status"], "passed", payload)
        root = Path(payload["rehearsal_root"])
        self.assertEqual(root.parent, self.data / "staging", "演练根在数据根 staging 下")
        plan = json.loads((root / "evidence" / "campaigns" / "upgrade-project-ledger" / "plan.json").read_text(encoding="utf-8"))
        self.assertIn("--fixture-only", plan["argv"], "演练总账是 fixture_only")
        [call] = self.calls()
        self.assertEqual(call["argv"], ["--to", "atomic-double"])
        self.assertEqual(sorted(set(call["environ"]) - {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING"}), ["ARM64_VC_ENV", "HOME", "LANG", "PATH"],
                         "编排器在干净环境里启动")
        env, original = call["env_file"], self.fixture.env_file.read_text(encoding="utf-8")
        self.assertEqual((env["ENTRY_ROOT"], env["ENTRY_COMMIT"], env["ENTRY_BRANCH"]), (str(root), COMMIT, "codex/test"))
        self.assertTrue(env["RUNROOT"].startswith(str(self.runroot / "entry-dryrun" / "run-")))
        for key in ("POLICY_COMPAT_RECEIPT", "POLICY_ACTIVATION", "RELEASE_CERTIFICATION", "PRE_A3_CERTIFICATION"):
            self.assertTrue(env[key].startswith(str(root / "control" / "policy-certification")), key)
        for key in ("NEW", "IN", "UP", "CAND", "BASELINE_VERSION", "TARGET_VERSION", "D"):
            self.assertIn(f'{key}="{env[key]}"', original, f"{key} 原样沿用本轮参数")
        self.assertEqual(Path(outcome["result"]).name, f"{COMMIT[:12]}-{bv._sha256_file(next((self.data / 'control').glob('codex-*.json')))[:12]}.json")
        again = json.loads(self.start().stdout.strip().splitlines()[-1])
        self.assertEqual((again["action"], again["status"]), ("exists", "passed"), "同一提交＋同一部署不重复跑")

    def test_opening_dry_run_reexecutes_with_audit_up_to_p0_receipt(self) -> None:
        outcome = json.loads(self.start("--opening").stdout.strip().splitlines()[-1])
        self.assertTrue(outcome["result"].endswith("-opening.json"))
        self.assertEqual(self.wait(outcome["result"])["status"], "passed")
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "p0-receipt", "--reexecute-gates", "--audit-reads"])

    def test_reexecute_without_audit_for_cold_timing(self) -> None:
        outcome = json.loads(self.start("--reexecute").stdout.strip().splitlines()[-1])
        self.assertTrue(outcome["result"].endswith("-reexecute.json"))
        self.assertEqual(self.wait(outcome["result"])["status"], "passed")
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", "--reexecute-gates"])
        both = self.start("--opening", "--reexecute")
        self.assertEqual(both.returncode, 2)

    def test_failures_are_reported_all_at_once(self) -> None:
        self.configure(passed=["policy-compatibility"], failed=["entry-preflight", "entry-gates", "pre-a3"], blocked=["zero-request-smoke"],
                       rc=1)
        payload = self.wait(json.loads(self.start().stdout.strip().splitlines()[-1])["result"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failed_steps"], ["entry-preflight", "entry-gates", "pre-a3", "zero-request-smoke"])
        self.assertIn("建账本之前有步骤没通过", payload["reason"])

    def test_yields_while_a_capture_holds_the_machine(self) -> None:
        executor = dr._sibling("unit_executor")
        reservation = executor.Reservation(self.state)
        reservation.request_path.write_text(json.dumps({"schema_version": executor.RESERVATION_SCHEMA, "owner": "vc-batch-x-1",
                                                        "owner_pid": os.getpid(), "requested_at_utc": "2026-10-03T00:00:00Z"}),
                                            encoding="utf-8")
        yielded = self.start()
        self.assertEqual(yielded.returncode, 4, yielded.stdout + yielded.stderr)
        outcome = json.loads(yielded.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["action"], "yielded")
        self.assertIn("vc-batch-x-1", outcome["reason"])
        self.assertEqual(self.calls(), [], "让路时不跑编排器")
        reservation.request_path.unlink()
        self.assertEqual(self.wait(json.loads(self.start().stdout.strip().splitlines()[-1])["result"])["status"], "passed",
                         "让路过的可以再起")

    def test_stop_supersedes_a_running_dry_run_and_background_validation_stop_includes_it(self) -> None:
        """空跑与前台入口门禁共用调度器和入口门禁工作目录：修复轮跑定向回归之前要停下。后台验证的 stop 连空跑一起停；
        先标 superseded 再整组终止，编排器替身进程不留。"""

        self.configure(sleep=60)
        result = json.loads(self.start().stdout.strip().splitlines()[-1])["result"]
        deadline = time.monotonic() + 30
        while not self.calls():
            self.assertLess(time.monotonic(), deadline, "编排器替身没有启动")
            time.sleep(0.05)
        entry_pid = self.calls()[-1]["pid"]
        stopped = bv.stop(self.runroot, "修复轮跑定向回归前停下")
        self.assertEqual(stopped, [f"entry-dryrun/{Path(result).name}"])
        payload = dr._read(Path(result))
        self.assertEqual((payload["status"], payload["reason"]), ("superseded", "修复轮跑定向回归前停下"))
        deadline = time.monotonic() + 10
        while bv._pid_alive(entry_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(bv._pid_alive(entry_pid), "编排器整组终止")
        time.sleep(1)
        self.assertEqual(dr._read(Path(result))["status"], "superseded", "后台 run 收尾时不改写")

    def test_never_reaches_the_vc0_closeout(self) -> None:
        refused = self.start("--to", "vc0-closeout")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("VC-0 收口会发正式请求", refused.stderr)

    def test_background_validation_chains_a_dry_run_up_to_pre_a3(self) -> None:
        gates = self.root / "entry-gates.sh"
        gates.write_text("#!/bin/bash\nOUT=''; ARGS=()\nwhile [ $# -gt 0 ]; do case $1 in --out) OUT=$2; shift 2;; --profile|--mode|--record-store|--work) "
                         "shift 2;; --require-deployed) shift;; *) ARGS+=(\"$1\"); shift;; esac; done\nmkdir -p \"$OUT\"\n"
                         "R=$(ls \"$DATA_ROOT\"/control/codex-*.json | tail -1)\n"
                         "printf '{\"status\":\"passed\",\"gates\":[],\"source\":{\"deploy_receipt\":\"%s\"}}' \"$R\" > \"$OUT/entry-gates.json\"\n",
                         encoding="utf-8")
        environ = {**self.environ, "DATA_ROOT": str(self.data)}
        started = subprocess.run([sys.executable, "-B", str(BV_MODULE), "start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                                  "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test", "--commit", COMMIT,
                                  "--vc-env", str(self.fixture.env_file), "--entry-gates", str(gates), "--then-dryrun"],
                                 capture_output=True, text=True, timeout=120, env=environ)
        self.assertEqual(started.returncode, 0, started.stderr)
        result = Path(json.loads(started.stdout.strip().splitlines()[-1])["result"])
        deadline = time.monotonic() + 60
        while not ((bv._read(result) or {}).get("dryrun")):
            self.assertLess(time.monotonic(), deadline, bv._read(result))
            time.sleep(0.05)
        validation = bv._read(result)
        self.assertEqual((validation["status"], validation["dryrun"]["action"], validation["dryrun"]["to"]), ("passed", "started", "pre-a3"))
        self.assertTrue(Path(validation["dryrun"]["result"]).is_file())
        bv.stop(self.runroot, "测试收尾")

    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual((DRIVER / "entry_dryrun.py").read_bytes(), MODULE.read_bytes())


if __name__ == "__main__":
    unittest.main()
