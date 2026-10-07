"""入口空跑（E4-02，``tools/ci/entry_dryrun.py``）：演练根与演练总账、演练参数只改产物坐标、干净环境里跑编排器、
结论按提交与部署绑定、开工空跑重新执行全集带审计、有采集在跑就让路、最远到 P0 收据；后台验证通过后接着空跑。

编排器用替身脚本（读演练参数文件里的 RUNROOT，按替身配置写运行汇总并退出），演练总账用数据根里的替身模块；后台进程是
真实的新会话子进程。

按客户端（B5a-10）：Claude 的空跑绑定 Claude 的部署收据、结论文件名带 ``claude-`` 前缀，与 Codex 的结论各一份；没通过的步骤
分类——客户端表登记的待接入记 registered_pending，pre-A3 按旁路认证登记的记录库逐场景读场景结果，链体停在登记占位上的记
scenario_pending，隔离降级、有请求、错误信息不是占位、认证读不到的记 real；逐单元承接请求原样交给编排器；有采集在跑时
同样让路（yielded）；两客户端都纳管时后台验证通过后两客户端的入口空跑都接上（另一客户端的轮次在验证进行中搭上）。
空跑串行（10-07 B5a-10 验收发现）：两份空跑在入口门禁工作目录上排队，后到的等前一份跑完；排队期间被 stop 标为 superseded
就放弃，超过上限结论 failed；只有一份空跑时结论里不出现排队字段（与改造前同形）。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.ci import background_validation as bv
from tools.ci import entry_dryrun as dr
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
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
if config.get("pre_a3_failed"):
    target = Path(values["PRE_A3_CERTIFICATION"])
    side = target.with_name(f"{target.stem}.failed-20261003t000000z{target.suffix}")
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps(config["pre_a3_failed"], ensure_ascii=False), encoding="utf-8")
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
        # 门禁由替身执行，但后台验证仍须绑定真实普通文件的字节摘要，不能传不存在的 bundle。
        (self.root / "x.bundle").write_bytes(b"isolated-entry-dryrun-bundle")
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
        # 开跑前的日常维护会清 Go 编译缓存、看根盘：替身环境里关掉（不碰开发机的缓存与磁盘），专门的用例另开。
        self.environ = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_STATE_DIR": str(self.state),
                        "ENTRY_DRYRUN_HOUSEKEEPING": "off"}

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
        self.assertNotIn("serial_lock_waited_seconds", payload, "只有一份空跑时不排队，结论与改造前同形")
        again = json.loads(self.start().stdout.strip().splitlines()[-1])
        self.assertEqual((again["action"], again["status"]), ("exists", "passed"), "同一提交＋同一部署不重复跑")

    def test_serial_lock_gives_up_when_superseded_and_fails_past_the_limit(self) -> None:
        lock = self.root / "entry-gates-work" / dr.SERIAL_LOCK_NAME
        self.assertEqual(dr._serial_lock_path({"data_root": str(self.data)}), self.data.parent / "entry-gates-work" / dr.SERIAL_LOCK_NAME,
                         "锁与 entry-gates.sh 缺省的入口门禁工作目录同一处")
        lock.parent.mkdir()
        holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            self.assertIsNone(dr._acquire_serial_lock(lock, lambda: False, timeout=5, poll=0.05),
                              "排队期间结论被 stop 标为 superseded 就放弃")
            with self.assertRaisesRegex(dr.DryRunError, "超过"):
                dr._acquire_serial_lock(lock, lambda: True, timeout=0.2, poll=0.05)
        finally:
            os.close(holder)
        held = dr._acquire_serial_lock(lock, lambda: True, timeout=5, poll=0.05)
        self.assertIsNotNone(held, "前一份释放后拿得到")
        fd, waited = held
        self.assertLess(waited, 1)
        os.close(fd)

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

    def test_housekeeping_runs_first_and_a_disk_over_the_line_stops_the_dry_run(self) -> None:
        """开跑前先做日常维护（Go 编译缓存旧条目、记录库、空跑留存），根盘越过停线就不跑：结论 failed、没通过的一步记 disk，
        不建演练根、编排器不启动。「越过停线」用参数文件的 MIN_FREE_GIB 抬高门槛造出来；Go 缓存指向临时目录。"""

        gocache = self.root / "gocache"
        (gocache / "ab").mkdir(parents=True)
        old, fresh = gocache / "ab" / "old-a", gocache / "ab" / "fresh-d"
        for path in (old, fresh):
            path.write_bytes(b"x")
        stale = time.time() - 7 * 3600
        os.utime(old, (stale, stale))
        for day in range(1, 7):
            (self.data / "staging" / f"entry-dryrun-202610{day:02d}t000000z").mkdir(parents=True)
        env_file = self.root / "env-high-floor.sh"
        env_file.write_text(re.sub(r"^MIN_FREE_GIB=.*$", 'MIN_FREE_GIB="99999999"', self.fixture.env_file.read_text(encoding="utf-8"),
                                   flags=re.M), encoding="utf-8")
        environ = {key: value for key, value in self.environ.items() if key != "ENTRY_DRYRUN_HOUSEKEEPING"}
        started = subprocess.run([sys.executable, "-B", str(MODULE), "start", "--runroot", str(self.runroot), "--data-root", str(self.data),
                                  "--vc-env", str(env_file), "--bundle", str(self.root / "x.bundle"), "--branch", "codex/test",
                                  "--commit", COMMIT, "--entry", str(self.entry)],
                                 capture_output=True, text=True, timeout=120, env={**environ, "GOCACHE": str(gocache)})
        self.assertEqual(started.returncode, 0, started.stderr)
        result = Path(json.loads(started.stdout.strip().splitlines()[-1])["result"])
        payload = self.wait(str(result))
        self.assertEqual((payload["status"], payload["failed_steps"]), ("failed", ["disk"]), payload)
        self.assertIn("根盘越过停线", payload["reason"])
        self.assertIsNone(payload["rehearsal_root"])
        self.assertEqual(self.calls(), [], "越过停线不启动编排器")
        self.assertFalse(old.exists(), "6 小时以上没用过的 Go 缓存条目删掉")
        self.assertTrue(fresh.exists())
        summary = payload["housekeeping"]
        self.assertEqual((summary["go_cache"]["removed_files"], summary["record_store"]["status"], summary["dryruns"]["removed"]),
                         (1, "absent", 1), summary)
        self.assertFalse((self.data / "staging" / "entry-dryrun-20261001t000000z").exists(), "空跑只留最近 5 次")
        report = json.loads(result.with_name(f"{result.stem}.housekeeping.json").read_text(encoding="utf-8"))
        self.assertFalse(report["disk"]["ok"])
        self.assertEqual(report["disk"]["min_free_gib"], 99999999)

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
        self.assertEqual(validation["input_contract"]["bundle_sha256"], bv._sha256_file(self.root / "x.bundle"))
        self.assertEqual((validation["status"], validation["dryrun"]["action"], validation["dryrun"]["to"]), ("passed", "started", "pre-a3"))
        chained = Path(validation["dryrun"]["result"])
        self.assertTrue(chained.is_file())
        self.assertTrue(chained.name.endswith("-to-pre-a3.json"), chained.name)
        self.wait(str(chained))
        explicit = json.loads(self.start("--to", "p0-receipt").stdout.strip().splitlines()[-1])
        self.assertEqual(explicit["action"], "started", "后台验证接着的空跑（到 pre-A3）不顶替明确执行的空跑")
        self.assertTrue(explicit["result"].endswith("-to-p0-receipt.json"))
        self.assertEqual(self.wait(explicit["result"])["status"], "passed")
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "p0-receipt"])
        bv.stop(self.runroot, "测试收尾")

    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual((DRIVER / "entry_dryrun.py").read_bytes(), MODULE.read_bytes())


CLAUDE_CHAINS = ("vc-chain.claude-deadline", "vc-chain.claude-tool-evolution", "vc-chain.claude-reconcile", "vc-chain.claude-root-cause")


def claude_env(fixture: driver_tests._DriverFixture, runroot: Path) -> Path:
    """把驱动夹具的 Codex 参数文件改成 Claude 轮次的：去掉 Codex 专属键，换版本、通用键与这一轮自己的运行根。"""

    lines = []
    for line in fixture.env_file.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        if key in {"KILO_BIN", "KILO_VERSION", "KILO_SHA256", "CODEX_BIN", "CODEX_BIN_SHA256", "CODEX_ACCOUNT_ID"}:
            continue
        lines.append(f'RUNROOT="{runroot}"' if key == "RUNROOT" else line)
    text = "CLIENT=claude\n" + "\n".join(lines) + "\n"
    text = text.replace('BASELINE_VERSION="0.154.0"', 'BASELINE_VERSION="2.1.226"').replace('TARGET_VERSION="0.156.1"', 'TARGET_VERSION="2.1.285"')
    text += f'CLIENT_BIN="/opt/claude-2.1.285/claude"\nCLIENT_BIN_SHA256="{"c" * 64}"\nCLIENT_ACCOUNT_ID="17"\n'
    runroot.mkdir(mode=0o700, exist_ok=True)
    path = runroot / "env.sh"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def pre_a3_store(root: Path, scenarios: dict[str, dict]) -> dict:
    """一份带 pre-A3 场景执行记录的记录库与运行清单，返回指向它的旁路认证内容（没通过的 v3 认证只剩占位与问题列表）。"""

    records = dr._sibling("unit_records")
    store = records.RecordStore(root / "unit-records")
    units = []
    for name, scenario in scenarios.items():
        unit_id = f"pre-a3:claude:{name}"
        line = pre_a3.SCENARIO_RESULT_PREFIX + json.dumps({"name": name, "kind": "formal", "network_attempts": scenario.pop("attempts", 0),
                                                           "scenario": {"name": name, **scenario}}, ensure_ascii=False)
        log = ("单元输出……\n" + line + "\n").encode("utf-8")
        log_sha = records.sha256_bytes(log)
        store.log_path(log_sha).parent.mkdir(parents=True, exist_ok=True)
        store.log_path(log_sha).write_bytes(log)
        record_sha = records.sha256_bytes(unit_id.encode("utf-8"))
        record = store.record_path(unit_id, record_sha)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"unit_id": unit_id, "record_sha256": record_sha, "log": {"sha256": log_sha}}), encoding="utf-8")
        units.append({"unit_id": unit_id, "disposition": "executed", "record_sha256": record_sha})
    manifest = store.manifest_path("run-1")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"run_id": "run-1", "units": units}), encoding="utf-8")
    return {"schema_version": "tool-pre-a3-path-certification/v3", "client": "claude", "status": "failed",
            "record_store": str(store.root), "unit_manifest": {"path": str(manifest), "run_id": "run-1"},
            "failed_scenarios": list(scenarios)}


def waiting_scenario(name: str, **metrics: object) -> dict:
    pending = f"等 B2-08：{name} 的恢复步骤"
    return {"status": "failed", "error": f"{name}：{pending}（占位之前的步骤已通过：……）",
            "metrics": {"pending": pending, "isolation": "namespace", "network_isolated": True, "upstream_requests": 0,
                        "returncode": 1, "steps": ["隔离自检"], **metrics}}


class ClaudeEntryDryRunTests(unittest.TestCase):
    """Claude 的入口空跑（B5a-10）。借用上面的替身、夹具与辅助方法（不继承，免得把 Codex 的用例在两客户端布局下再跑一遍），
    数据根另有一份 Claude 部署收据，Claude 轮次有自己的运行根。"""

    configure = EntryDryRunTests.configure
    cli = EntryDryRunTests.cli
    start = EntryDryRunTests.start
    wait = EntryDryRunTests.wait
    calls = EntryDryRunTests.calls

    def setUp(self) -> None:
        EntryDryRunTests.setUp(self)
        self.claude_receipt = self.data / "control" / "claude-managed-deploy-20261003t000000z.json"
        self.claude_receipt.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "f" * 64}}),
                                       encoding="utf-8")
        self.claude_runroot = self.root / "claude-runroot"
        self.claude_env = claude_env(self.fixture, self.claude_runroot)

    def start_claude(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.cli("start", "--runroot", str(self.claude_runroot), "--data-root", str(self.data), "--vc-env", str(self.claude_env),
                        "--bundle", str(self.root / "x.bundle"), "--branch", "claude/test", "--commit", COMMIT, "--entry", str(self.entry), *extra)

    def test_claude_dry_run_is_prefixed_bound_to_claude_and_classifies_pending_steps(self) -> None:
        store = pre_a3_store(self.root, {name: waiting_scenario(name) for name in CLAUDE_CHAINS})
        # B5a-09 续落实 atomic-double 之后 Claude 没有登记待接入的入口步骤：空跑里它照常执行（这里通过），只剩 pre-A3 占位。
        self.configure(passed=["entry-preflight", "policy-compatibility", "policy-activation", "entry-gates", "zero-request-smoke",
                               "atomic-double"],
                       failed=["pre-a3"], rc=1, pre_a3_failed=store)
        started = self.start_claude()
        self.assertEqual(started.returncode, 0, started.stderr)
        outcome = json.loads(started.stdout.strip().splitlines()[-1])
        result = Path(outcome["result"])
        self.assertEqual(result, self.claude_runroot / "entry-dryrun" / f"claude-{COMMIT[:12]}-{bv._sha256_file(self.claude_receipt)[:12]}.json")
        payload = self.wait(str(result))
        self.assertEqual((payload["status"], payload["client"], payload["deployment"]["receipt"]),
                         ("failed", "claude", self.claude_receipt.name))
        classes = payload["failure_classes"]
        self.assertEqual(set(classes), {"pre-a3"})
        # 没有登记为待接入的步骤失败一律算真实失败（atomic-double 不再是 registered_pending）。
        self.assertEqual(dr.classify_failures("claude", [{"step_id": "atomic-double", "action": "执行", "reasons": ["收据没写出"]}],
                                              self.root)["atomic-double"]["class"], "real")
        self.assertEqual(classes["pre-a3"]["class"], "scenario_pending", classes["pre-a3"])
        self.assertEqual(sorted(classes["pre-a3"]["detail"]["pending"]), sorted(f"pre-a3:claude:{name}" for name in CLAUDE_CHAINS))
        self.assertEqual(payload["real_failed_steps"], [], "只差登记过的待接入与占位，没有真实失败")
        root = Path(payload["rehearsal_root"])
        self.assertTrue((root / "evidence" / "campaigns" / "upgrade-project-ledger-claude" / "plan.json").is_file(), "演练总账按 Claude 建")
        self.assertEqual(self.calls()[-1]["env_file"]["CLIENT"], "claude")
        # 同一提交＋同一部署，Codex 的空跑另一份（文件名不带前缀、结论与改造前同形）。
        self.configure()
        codex = json.loads(self.start().stdout.strip().splitlines()[-1])
        codex_payload = self.wait(codex["result"])
        self.assertEqual(Path(codex["result"]).name, f"{COMMIT[:12]}-{bv._sha256_file(next((self.data / 'control').glob('codex-*.json')))[:12]}.json")
        self.assertEqual(codex_payload["status"], "passed")
        self.assertFalse({"client", "failure_classes", "real_failed_steps"} & set(codex_payload))
        again = json.loads(self.start_claude().stdout.strip().splitlines()[-1])
        self.assertEqual((again["action"], again["status"]), ("exists", "failed"), "同一提交＋同一部署不重复跑")

    def test_real_pre_a3_failures_are_not_hidden(self) -> None:
        cases = {
            "隔离降级": waiting_scenario("vc-chain.claude-deadline", isolation="degraded", network_isolated=False),
            "有上游请求": waiting_scenario("vc-chain.claude-reconcile", upstream_requests=2),
            "超时": {**waiting_scenario("vc-chain.claude-root-cause"), "error": "vc-chain.claude-root-cause 超过 900 秒（实测 901 秒）"},
            "没有占位": {"status": "failed", "error": "子进程失败", "metrics": {"isolation": "namespace", "network_isolated": True}},
        }
        for label, scenario in cases.items():
            with self.subTest(label):
                verdict, why = dr._scenario_verdict({"network_attempts": 0, "scenario": scenario})
                self.assertEqual(verdict, "real", why)
        self.assertEqual(dr._scenario_verdict({"network_attempts": 0, "scenario": {"status": "passed"}})[0], "passed")
        self.assertEqual(dr._scenario_verdict({"network_attempts": 1, "scenario": waiting_scenario("x")})[0], "real")
        mixed = pre_a3_store(self.root, {"vc-chain.claude-deadline": waiting_scenario("vc-chain.claude-deadline"),
                                         "vc-chain.claude-reconcile": cases["隔离降级"]})
        certification = self.root / "cert" / "pre-a3-path-certification.json"
        certification.parent.mkdir()
        (certification.parent / "pre-a3-path-certification.failed-20261003t000000z.json").write_text(json.dumps(mixed), encoding="utf-8")
        detail = dr.classify_pre_a3(certification)
        self.assertEqual((detail["pending"], detail["real"]), (["pre-a3:claude:vc-chain.claude-deadline"], ["pre-a3:claude:vc-chain.claude-reconcile"]))
        self.assertEqual(dr.classify_pre_a3(self.root / "nowhere" / "pre-a3-path-certification.json")["status"], "unavailable")
        classes = dr.classify_failures("claude", [{"step_id": "pre-a3", "action": "执行", "status": "failed"},
                                                  {"step_id": "entry-gates", "action": "执行", "status": "failed"},
                                                  {"step_id": "zero-request-smoke", "action": "被阻塞", "status": ""}], self.root / "nowhere")
        self.assertEqual({step: item["class"] for step, item in classes.items()},
                         {"pre-a3": "real", "entry-gates": "real", "zero-request-smoke": "blocked"})
        self.assertEqual(dr.PRE_A3_RESULT_PREFIX, pre_a3.SCENARIO_RESULT_PREFIX, "场景结果行的前缀与受管 pre-A3 认证模块一致")

    def test_claude_dry_run_yields_while_a_capture_holds_the_machine(self) -> None:
        # 让路与客户端无关：有采集持有整机预约时 Claude 的空跑同样不起，结论（带 claude- 前缀）记 yielded、退出 4。
        executor = dr._sibling("unit_executor")
        reservation = executor.Reservation(self.state)
        reservation.request_path.write_text(json.dumps({"schema_version": executor.RESERVATION_SCHEMA, "owner": "vc-batch-claude-1",
                                                        "owner_pid": os.getpid(), "requested_at_utc": "2026-10-03T00:00:00Z"}),
                                            encoding="utf-8")
        yielded = self.start_claude()
        self.assertEqual(yielded.returncode, 4, yielded.stdout + yielded.stderr)
        outcome = json.loads(yielded.stdout.strip().splitlines()[-1])
        self.assertEqual(outcome["action"], "yielded")
        payload = dr._read(Path(outcome["result"]))
        self.assertTrue(Path(outcome["result"]).name.startswith(f"claude-{COMMIT[:12]}-"), outcome["result"])
        self.assertEqual((payload["status"], payload["client"]), ("yielded", "claude"))
        self.assertIn("vc-batch-claude-1", payload["reason"])
        self.assertEqual(self.calls(), [], "让路时不跑编排器")
        reservation.request_path.unlink()
        again = json.loads(self.start_claude().stdout.strip().splitlines()[-1])
        self.assertEqual(again["result"], outcome["result"])
        self.assertEqual(self.wait(again["result"])["status"], "passed", "让路过的可以再起")

    def test_orchestrator_crash_counts_as_a_real_failure(self) -> None:
        missing_entry = self.root / "no-such-entry.sh"
        started = self.cli("start", "--runroot", str(self.claude_runroot), "--data-root", str(self.data), "--vc-env", str(self.claude_env),
                           "--bundle", str(self.root / "x.bundle"), "--branch", "claude/test", "--commit", COMMIT, "--entry", str(missing_entry))
        payload = self.wait(json.loads(started.stdout.strip().splitlines()[-1])["result"])
        self.assertEqual((payload["status"], payload["real_failed_steps"]), ("failed", ["entry-orchestrator"]))

    def test_unit_inherit_request_reaches_the_orchestrator(self) -> None:
        request = self.root / "unit-inherit-request.json"
        request.write_text(json.dumps({"schema_version": "unit-inherit-request/v1"}), encoding="utf-8")
        outcome = json.loads(self.start_claude("--unit-inherit-request", str(request)).stdout.strip().splitlines()[-1])
        digest = bv._sha256_file(request)
        self.assertTrue(Path(outcome["result"]).name.endswith(f"-unit-inherit-{digest[:12]}.json"), outcome["result"])
        payload = self.wait(outcome["result"])
        self.assertEqual(payload["unit_inherit_request"], {"path": str(request), "sha256": digest})
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", "--unit-inherit-request", str(request)])
        for extra in (("--opening",), ("--reexecute",)):
            with self.subTest(extra):
                refused = self.start_claude("--unit-inherit-request", str(request), *extra)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("不能与 --opening／--reexecute", refused.stderr)
        missing = self.start_claude("--unit-inherit-request", str(self.root / "missing.json"))
        self.assertEqual(missing.returncode, 2)

    def test_two_clients_dry_runs_queue_on_the_gate_work_directory(self) -> None:
        # 两客户端都纳管时，后台验证通过后两份空跑几乎同时接上；编排器各跑 3 秒，后到的那份排队、两份都通过。
        self.configure(sleep=3)
        codex = json.loads(self.start().stdout.strip().splitlines()[-1])
        claude = json.loads(self.start_claude().stdout.strip().splitlines()[-1])
        self.assertEqual((codex["action"], claude["action"]), ("started", "started"))
        results = [self.wait(codex["result"]), self.wait(claude["result"])]
        self.assertEqual([item["status"] for item in results], ["passed", "passed"], results)
        queued = [item for item in results if "serial_lock_waited_seconds" in item]
        self.assertEqual(len(queued), 1, "恰有一份排过队")
        self.assertGreaterEqual(queued[0]["serial_lock_waited_seconds"], 1)
        self.assertEqual(len(self.calls()), 2, "两份都跑了编排器")

    def test_background_validation_chains_both_clients_dry_runs(self) -> None:
        codex_receipt = next((self.data / "control").glob("codex-*.json"))
        gates = self.root / "entry-gates-two.sh"
        gates.write_text("#!/bin/bash\nOUT=''; ARGS=()\nwhile [ $# -gt 0 ]; do case $1 in --out) OUT=$2; shift 2;; --profile|--mode|--record-store|--work|--wait-lock) "
                         "shift 2;; --require-deployed|--require-deployed-all) shift;; *) ARGS+=(\"$1\"); shift;; esac; done\nsleep \"${FAKE_GATES_SLEEP:-0}\"\nmkdir -p \"$OUT\"\n"
                         "printf '{\"status\":\"passed\",\"gates\":[],\"source\":{\"deploy_receipt\":\"%s\",\"deploy_receipts_others\":\"%s\"}}' "
                         "\"$FAKE_GATES_RECEIPT\" \"$FAKE_GATES_OTHERS\" > \"$OUT/entry-gates.json\"\n", encoding="utf-8")
        environ = {**self.environ, "FAKE_GATES_RECEIPT": str(codex_receipt), "FAKE_GATES_OTHERS": f"claude={self.claude_receipt.name}",
                   "FAKE_GATES_SLEEP": "2"}

        def start_validation(runroot: Path, env_file: Path, *client: str) -> dict:
            completed = subprocess.run([sys.executable, "-B", str(BV_MODULE), "start", "--runroot", str(runroot), "--data-root", str(self.data),
                                        "--bundle", str(self.root / "x.bundle"), "--branch", "claude/test", "--commit", COMMIT,
                                        "--vc-env", str(env_file), "--entry-gates", str(gates), "--then-dryrun", *client],
                                       capture_output=True, text=True, timeout=120, env=environ)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout.strip().splitlines()[-1])

        started = start_validation(self.runroot, self.fixture.env_file)
        self.assertEqual(started["action"], "started")
        attached = start_validation(self.claude_runroot, self.claude_env, "--client", "claude")
        self.assertEqual((attached["action"], attached["attached"], attached["result"]), ("exists", "claude", started["result"]),
                         "Claude 的轮次在验证进行中搭上，不再起")
        result = Path(started["result"])
        deadline = time.monotonic() + 60
        while not ((bv._read(result) or {}).get("peer_dryruns")):
            self.assertLess(time.monotonic(), deadline, bv._read(result))
            time.sleep(0.05)
        validation = bv._read(result)
        self.assertEqual(validation["status"], "passed")
        codex_dryrun, claude_dryrun = Path(validation["dryrun"]["result"]), Path(validation["peer_dryruns"]["claude"]["result"])
        self.assertEqual(codex_dryrun.parent, self.runroot / "entry-dryrun")
        self.assertEqual(claude_dryrun.parent, self.claude_runroot / "entry-dryrun")
        self.assertEqual(codex_dryrun.name, f"{COMMIT[:12]}-{bv._sha256_file(codex_receipt)[:12]}-to-pre-a3.json")
        self.assertEqual(claude_dryrun.name, f"claude-{COMMIT[:12]}-{bv._sha256_file(self.claude_receipt)[:12]}-to-pre-a3.json",
                         "同一提交＋同一部署两客户端的空跑结论各一份")
        for chained in (codex_dryrun, claude_dryrun):
            self.wait(str(chained))
        late = start_validation(self.claude_runroot, self.claude_env, "--client", "claude")
        self.assertEqual((late["action"], late["status"], late["dryrun"]["action"]), ("exists", "passed", "exists"),
                         "验证已通过时搭上的当场接空跑，同一份空跑已有就不重复起")
        bv.stop(self.runroot, "测试收尾")
        bv.stop(self.claude_runroot, "测试收尾")


if __name__ == "__main__":
    unittest.main()
