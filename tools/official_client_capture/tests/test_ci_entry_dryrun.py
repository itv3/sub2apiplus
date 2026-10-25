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
from unittest import mock

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
    opening = {name: os.environ[name] for name in ("MODEL_PROBE_URL", "MODEL_PROBE_KEY_FILE", "MODEL_PROBE_MINIMAL", "SECURITY_SCAN_REPO",
                                                   "OPENING_CHECKS_SKIP", "OPENING_CHECKS_SKIP_REASON") if name in os.environ}
    handle.write(json.dumps({"argv": argv, "environ": sorted(os.environ), "env_file": values, "pid": os.getpid(), "opening": opening}) + "\n")
if config.get("sleep"):
    import time
    time.sleep(config["sleep"])
if config.get("change_audit_policy"):
    Path(argv[argv.index("--audit-policy") + 1]).write_text(config["change_audit_policy"], encoding="utf-8")
if config.get("change_source_work"):
    work = Path(argv[argv.index("--full-set-work") + 1])
    (work / "test-tree" / "source.txt").write_text("执行期间来源漂移", encoding="utf-8")
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


def source_workspace(root: Path, *, branch: str = "claude/test") -> tuple[Path, Path, str]:
    """小型真实 Git 来源与 bundle；使用原工作区校验器验真，不模拟其返回值。"""
    work, bundle = root / "source-work", root / "source.bundle"
    tree = work / "test-tree"
    tree.mkdir(parents=True)
    def git(*args: str) -> str:
        completed = subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True)
        return completed.stdout.strip()
    git("init", "-b", branch)
    (tree / "source.txt").write_text("原来源\n", encoding="utf-8")
    git("add", "source.txt")
    git("-c", "user.name=短验夹具", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
        "commit", "-m", "真实来源短验")
    commit = git("rev-parse", "HEAD")
    git("bundle", "create", str(bundle), branch)
    for name in ("pycache", "go-build-cache", "go-mod-cache", "go-tmp"):
        (work / name).mkdir()
    return work, bundle, commit


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
        self.environ = {**{key: value for key, value in os.environ.items() if key not in dr.OPENING_CHECK_ENV},
                        "PYTHONDONTWRITEBYTECODE": "1", "UNIT_EXECUTOR_STATE_DIR": str(self.state), "ENTRY_DRYRUN_HOUSEKEEPING": "off"}

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
                dr._acquire_serial_lock(lock, lambda: True, timeout=0.2, poll=0.05)  # time-threshold: allow 被测对象就是排队超时语义，0.2 秒是输入；锁被占住时无论机器多慢都必然超时
        finally:
            os.close(holder)
        held = dr._acquire_serial_lock(lock, lambda: True, timeout=5, poll=0.05)
        self.assertIsNotNone(held, "前一份释放后拿得到")
        fd, waited = held
        # 锁已释放，第一次非阻塞尝试就拿到；上界取 5 秒（等于上面用的排队超时）即可区分“没有排队”和“排到超时”，不再贴着 1 秒。
        self.assertLess(waited, 5)
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
                                  "--vc-env", str(self.fixture.env_file), "--entry-gates", str(gates), "--then-dryrun",
                                  "--preflight", "off", "--reuse", "off"],
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

    # ---- 开工检查的六个调用环境变量（优化第 14 项集成缺口）：编排器在干净环境里启动，必须白名单透传 ----

    SECRET = "sk-secret-key-content-must-never-be-logged-0123456789"
    REASON = "中转维护中，已人工确认 \"模型在线\" $HOME `date` ; rm -rf / 'x'\t(多空格   保留)"

    def opening_values(self) -> dict[str, str]:
        key_file = self.root / "relay-key"
        key_file.write_text(self.SECRET + "\n", encoding="utf-8")
        key_file.chmod(0o600)
        return {"MODEL_PROBE_URL": "http://relay.probe.invalid/v1", "MODEL_PROBE_KEY_FILE": str(key_file), "MODEL_PROBE_MINIMAL": "yes-minimal",
                "SECURITY_SCAN_REPO": "probe-owner/probe-repo", "OPENING_CHECKS_SKIP": "model-availability,main-security-scan",
                "OPENING_CHECKS_SKIP_REASON": self.REASON}

    NOISE = {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING"}

    def environment_names(self, call: dict) -> list[str]:
        return sorted(set(call["environ"]) - self.NOISE)

    def everything_written(self, *extra: str) -> bytes:
        """演练根、运行根与数据根下所有文件的内容，加上调用者拿到的输出：取值不得出现在其中任何一处。"""

        chunks = [text.encode("utf-8") for text in extra]
        for root in (self.runroot, self.data):
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    chunks.append(path.read_bytes())
        return b"\n".join(chunks)

    def test_the_six_opening_check_variables_reach_the_orchestrator_and_nothing_else_does(self) -> None:
        values = self.opening_values()
        self.environ = {**self.environ, **values, "FOO": "must-not-pass", "AWS_SECRET_ACCESS_KEY": "must-not-pass-either"}
        started = self.start()
        self.assertEqual(started.returncode, 0, started.stderr)
        payload = self.wait(json.loads(started.stdout.strip().splitlines()[-1])["result"])
        self.assertEqual(payload["status"], "passed", payload)
        [call] = self.calls()
        self.assertEqual(call["opening"], values, "六个变量原样到达（含引号、$、反引号、分号、制表符、中文与连续空格的跳过理由）")
        self.assertEqual(self.environment_names(call), sorted(["ARM64_VC_ENV", "HOME", "LANG", "PATH", *values]), "除这六个以外仍是干净环境")
        self.assertNotIn("FOO", call["environ"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", call["environ"])
        self.assertEqual(call["argv"], ["--to", "atomic-double"], "取值不在编排器的命令行里")
        written = self.everything_written(started.stdout, started.stderr)
        for name, value in values.items():
            self.assertNotIn(value.encode("utf-8"), written, f"{name} 的取值不应出现在任何落盘文件或输出里")
        self.assertNotIn(self.SECRET.encode("utf-8"), written, "密钥文件内容不进日志，也不进任何落盘文件")

    def test_without_the_variables_nothing_is_added_and_empty_values_count_as_unset(self) -> None:
        self.environ = {**self.environ, "FOO": "bar", "MODEL_PROBE_MINIMAL": "", "OPENING_CHECKS_SKIP": ""}
        self.assertEqual(self.wait(json.loads(self.start().stdout.strip().splitlines()[-1])["result"])["status"], "passed")
        [call] = self.calls()
        self.assertEqual(call["opening"], {})
        self.assertEqual(self.environment_names(call), ["ARM64_VC_ENV", "HOME", "LANG", "PATH"])

    def test_a_skip_only_takes_effect_for_the_run_started_with_it(self) -> None:
        """跳过逐次显式：调用者当次环境里设了才生效，不写进任何持久配置——下一次不带就没有。"""

        values = self.opening_values()
        self.environ = {**self.environ, **values}
        first = json.loads(self.start().stdout.strip().splitlines()[-1])
        self.assertEqual(self.wait(first["result"])["status"], "passed")
        self.environ = {key: value for key, value in self.environ.items() if key not in values}
        second = json.loads(self.start("--to", "p0-receipt").stdout.strip().splitlines()[-1])
        self.assertEqual(second["action"], "started")
        self.assertEqual(self.wait(second["result"])["status"], "passed")
        first_call, second_call = self.calls()
        self.assertEqual(first_call["opening"], values)
        self.assertEqual(second_call["opening"], {}, "上一次的变量不会残留到下一次")
        for result in (first["result"], second["result"]):
            text = Path(result).read_text(encoding="utf-8")
            for name in values:
                self.assertNotIn(name, text, f"空跑结论里不记 {name}")

    def test_the_chained_dry_run_after_a_background_validation_gets_the_variables_too(self) -> None:
        """后台验证通过后自动接的入口空跑：``_chain_dryrun`` 起 ``entry_dryrun.py start``（继承调用者环境），再起 run、再起编排器（干净环境加白名单透传）。
        链路上除最后一层，没有任何一层清环境——这里把真实的下游全跑一遍（只给 start 补上 --entry 指向编排器替身）。"""

        values = self.opening_values()
        payload = {"vc_env": str(self.fixture.env_file), "data_root": str(self.data), "runroot": str(self.runroot), "bundle": str(self.root / "x.bundle"),
                   "branch": "codex/test", "commit": COMMIT}
        real_run = subprocess.run
        chained_argv: list[list[str]] = []

        def with_fake_entry(argv, **kwargs):
            chained_argv.append(list(argv))
            return real_run([*argv, "--entry", str(self.entry)], **kwargs)

        environment = {**values, "FOO": "must-not-pass", "ENTRY_DRYRUN_HOUSEKEEPING": "off", "UNIT_EXECUTOR_STATE_DIR": str(self.state)}
        with mock.patch.dict(os.environ, environment), mock.patch.object(bv.subprocess, "run", side_effect=with_fake_entry):
            outcome = bv._chain_dryrun(payload)
        self.assertEqual(outcome.get("action"), "started", outcome)
        self.assertEqual(self.wait(outcome["result"])["status"], "passed")
        [call] = self.calls()
        self.assertEqual(call["opening"], values)
        self.assertEqual(self.environment_names(call), sorted(["ARM64_VC_ENV", "HOME", "LANG", "PATH", *values]))
        self.assertNotIn("FOO", call["environ"])
        for argv in chained_argv:
            for value in values.values():
                self.assertNotIn(value, argv, "取值不在链路上任何一层的命令行里")
        self.assertNotIn(self.SECRET.encode("utf-8"), self.everything_written())

    def test_background_validation_start_hands_the_caller_environment_to_its_run_process(self) -> None:
        gates = self.root / "entry-gates.sh"
        gates.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        values = self.opening_values()
        args = bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"),
                          "--branch", "codex/test", "--commit", COMMIT, "--vc-env", str(self.fixture.env_file), "--entry-gates", str(gates),
                          "--then-dryrun", "--preflight", "off", "--reuse", "off"])
        with mock.patch.dict(os.environ, {**values, "FOO": "bar"}), mock.patch.object(bv.subprocess, "Popen", return_value=mock.Mock(pid=-1)) as popen:
            bv.start(args)
        [started_run] = [call for call in popen.call_args_list if "--result" in call.args[0]]   # 另有 ps（subprocess.run 底层也是 Popen），不看它
        environment = started_run.kwargs["env"]
        for name, value in values.items():
            self.assertEqual(environment.get(name), value, f"{name} 随调用者环境交给后台验证的 run 进程")
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

    def start_with_source_work(self, work: Path, bundle: Path, commit: str, *extra: str) -> subprocess.CompletedProcess[str]:
        """仅本批显式工作区路径用真实 Git 提交，其它既有夹具与缺省命令不变。"""
        return self.cli("start", "--runroot", str(self.claude_runroot), "--data-root", str(self.data), "--vc-env", str(self.claude_env),
                        "--bundle", str(bundle), "--branch", "claude/test", "--commit", commit, "--entry", str(self.entry),
                        "--full-set-work", str(work), *extra)

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

        # 原无策略 argv 与结论命名先逐字核对；显式策略沿用原合同、绑定实际字节并透传严格审计。
        policy = self.root / "unit-audit-policy.json"
        declaration = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
        policy.write_text(json.dumps(declaration), encoding="utf-8")
        policy_digest = bv._sha256_file(policy)
        audited = self.start_claude("--unit-inherit-request", str(request), "--audit-policy", str(policy))
        self.assertEqual(audited.returncode, 0, audited.stderr)
        audited_outcome = json.loads(audited.stdout.strip().splitlines()[-1])
        audited_payload = self.wait(audited_outcome["result"])
        self.assertEqual(audited_payload["status"], "passed", audited_payload)
        self.assertEqual(audited_payload["audit_policy"], {"path": str(policy), "sha256": policy_digest})
        self.assertTrue(Path(audited_outcome["result"]).name.endswith(f"-unit-inherit-{digest[:12]}-audit-policy-{policy_digest[:12]}.json"))
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", "--unit-inherit-request", str(request),
                                                   "--audit-reads", "--audit-strict", "--audit-policy", str(policy)])
        self.assertNotEqual(audited_outcome["result"], outcome["result"], "不能以无策略结论顶替登记策略下的空跑")

        # 相同 JSON 的另一批字节也须另绑结论，不能仅比较解析后的对象。
        policy.write_text(json.dumps(declaration, indent=2) + "\n", encoding="utf-8")
        changed = json.loads(self.start_claude("--unit-inherit-request", str(request), "--audit-policy", str(policy)).stdout.strip().splitlines()[-1])
        self.assertNotEqual(changed["result"], audited_outcome["result"])
        self.assertEqual(self.wait(changed["result"])["status"], "passed")
        opened = self.start_claude("--opening", "--audit-policy", str(policy))
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(self.wait(json.loads(opened.stdout.strip().splitlines()[-1])["result"])["status"], "passed")
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "p0-receipt", "--reexecute-gates", "--audit-reads",
                                                   "--audit-strict", "--audit-policy", str(policy)])
        for extra in ((), ("--reexecute",)):
            with self.subTest(mode=extra):
                refused = self.start_claude(*extra, "--audit-policy", str(policy))
                self.assertEqual(refused.returncode, 2)
                self.assertIn("只与 --unit-inherit-request 或显式 --opening", refused.stderr)

        link = self.root / "policy-link.json"
        link.symlink_to(policy)
        linked_parent = self.root / "policy-parent-link"
        linked_parent.symlink_to(self.root, target_is_directory=True)
        for path in ("relative.json", str(self.root / "missing-policy.json"), str(link), str(linked_parent / policy.name),
                     str(self.root / "child" / ".." / policy.name), str(self.root)):
            with self.subTest(policy_path=path):
                refused = self.start_claude("--unit-inherit-request", str(request), "--audit-policy", path)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("规范绝对路径", refused.stderr)
        invalid = self.root / "invalid-policy.json"
        for content in ("broken-json", "[]", json.dumps({**declaration, "default": "inherit"}),
                        json.dumps({**declaration, "units": {"test:unregistered": {"require_read_audit": False}}})):
            with self.subTest(policy_content=content):
                invalid.write_text(content, encoding="utf-8")
                refused = self.start_claude("--unit-inherit-request", str(request), "--audit-policy", str(invalid))
                self.assertEqual(refused.returncode, 2)
                self.assertIn("合同无效", refused.stderr)

        work, bundle, commit = source_workspace(self.root)
        selected = self.start_with_source_work(work, bundle, commit, "--unit-inherit-request", str(request), "--audit-policy", str(policy))
        self.assertEqual(selected.returncode, 0, selected.stderr)
        selected_outcome = json.loads(selected.stdout.strip().splitlines()[-1])
        selected_payload = self.wait(selected_outcome["result"])
        reference = dr._full_set_work_reference(work, commit=commit, bundle=bundle, branch="claude/test")
        self.assertEqual(selected_payload["status"], "passed", selected_payload)
        self.assertEqual(selected_payload["full_set_work"], reference)
        self.assertTrue(Path(selected_outcome["result"]).name.endswith(f"-source-work-{reference['sha256'][:12]}.json"))
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", "--unit-inherit-request", str(request),
                                                   "--audit-reads", "--audit-strict", "--audit-policy", str(policy), "--full-set-work", str(work)])
        self.assertTrue(all(key not in self.calls()[-1]["environ"] for key in ("GOCACHE", "GOMODCACHE", "GOTMPDIR")),
                        "空跑编排器仍是干净环境，来源缓存只由门禁子进程接收")
        original_bundle = bundle.read_bytes()
        bundle.write_bytes(original_bundle + b"\n")
        with self.assertRaises(dr.DryRunError):
            dr._verify_full_set_work(selected_payload)
        bundle.write_bytes(original_bundle)
        other_bundle = self.root / "same-bytes.bundle"
        other_bundle.write_bytes(original_bundle)
        with self.assertRaisesRegex(dr.DryRunError, "空跑启动后变化"):
            dr._verify_full_set_work({**selected_payload, "bundle": str(other_bundle)})
        other_work = self.root / "other-source-work"
        other_work.mkdir()
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(work / "test-tree"), str(other_work / "test-tree")],
                       capture_output=True, text=True, check=True)
        for name in ("pycache", "go-build-cache", "go-mod-cache", "go-tmp"):
            (other_work / name).mkdir()
        other = self.start_with_source_work(other_work, bundle, commit, "--unit-inherit-request", str(request), "--audit-policy", str(policy))
        self.assertEqual(other.returncode, 0, other.stderr)
        other_outcome = json.loads(other.stdout.strip().splitlines()[-1])
        self.assertNotEqual(other_outcome["result"], selected_outcome["result"], "同一提交不同来源工作区不能共用结论")
        self.assertEqual(self.wait(other_outcome["result"])["status"], "passed")
        orphan = self.start_with_source_work(work, bundle, commit)
        self.assertEqual(orphan.returncode, 2)
        self.assertIn("与 --unit-inherit-request 一起提供", orphan.stderr)
        linked = self.root / "source-link"
        linked.symlink_to(work, target_is_directory=True)
        parent_link = self.root / "source-parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        for candidate in (Path("relative"), linked, parent_link / work.name, work / ".." / work.name):
            with self.subTest(work=candidate):
                refused = self.start_with_source_work(candidate, bundle, commit, "--unit-inherit-request", str(request))
                self.assertEqual(refused.returncode, 2)
                self.assertIn("规范绝对路径", refused.stderr)
        cache = work / "go-build-cache"
        cache.rmdir()
        cache.symlink_to(work / "go-mod-cache", target_is_directory=True)
        refused = self.start_with_source_work(work, bundle, commit, "--unit-inherit-request", str(request))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("规范绝对路径", refused.stderr)
        cache.unlink()
        cache.mkdir()
        wrong_commit = self.start_with_source_work(work, bundle, "d" * 40, "--unit-inherit-request", str(request))
        self.assertEqual(wrong_commit.returncode, 2)
        self.assertIn("提交不符", wrong_commit.stderr)
        wrong_branch = self.cli("start", "--runroot", str(self.claude_runroot), "--data-root", str(self.data), "--vc-env", str(self.claude_env),
                                "--bundle", str(bundle), "--branch", "wrong/test", "--commit", commit,
                                "--full-set-work", str(work), "--unit-inherit-request", str(request))
        self.assertEqual(wrong_branch.returncode, 2)
        self.assertIn("分支", wrong_branch.stderr)

    def test_two_profile_requests_reach_the_orchestrator_and_bind_separate_conclusions(self) -> None:
        primary, companion = self.root / "entry-request.json", self.root / "full-gates-request.json"
        primary.write_text(json.dumps({"profile": "entry"}), encoding="utf-8")
        companion.write_text(json.dumps({"profile": "full-gates", "revision": 1}), encoding="utf-8")
        argv = ("--unit-inherit-request", str(primary), "--unit-inherit-full-gates-request", str(companion))
        started = self.start_claude(*argv)
        self.assertEqual(started.returncode, 0, started.stderr)
        outcome = json.loads(started.stdout.strip().splitlines()[-1])
        payload = self.wait(outcome["result"])
        digest = bv._sha256_file(companion)
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual(payload["unit_inherit_full_gates_request"], {"path": str(companion), "sha256": digest})
        self.assertTrue(Path(outcome["result"]).name.endswith(f"-full-gates-{digest[:12]}.json"))
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", *argv])
        companion.write_text(json.dumps({"profile": "full-gates", "revision": 2}), encoding="utf-8")
        changed = json.loads(self.start_claude(*argv).stdout.strip().splitlines()[-1])
        self.assertNotEqual(changed["result"], outcome["result"], "配套批准变化不能命中旧空跑结论")
        self.assertEqual(self.wait(changed["result"])["status"], "passed")
        policy = self.root / "unit-audit-policy.json"
        policy.write_text(json.dumps({"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}), encoding="utf-8")
        audited = self.start_claude(*argv, "--audit-policy", str(policy))
        self.assertEqual(audited.returncode, 0, audited.stderr)
        self.assertEqual(self.wait(json.loads(audited.stdout.strip().splitlines()[-1])["result"])["status"], "passed")
        self.assertEqual(self.calls()[-1]["argv"], ["--to", "atomic-double", *argv, "--audit-reads", "--audit-strict", "--audit-policy", str(policy)])

        # 执行期间策略变更，即使编排器替身返回成功也不能发布通过结论。
        self.configure(change_audit_policy=policy.read_text(encoding="utf-8") + "\n")
        mutated = self.start_claude(*argv, "--audit-policy", str(policy), "--to", "pre-a3")
        self.assertEqual(mutated.returncode, 0, mutated.stderr)
        rejected = self.wait(json.loads(mutated.stdout.strip().splitlines()[-1])["result"])
        self.assertEqual((rejected["status"], rejected["failed_steps"]), ("failed", ["entry-orchestrator"]))
        self.assertIn("策略在空跑启动后变化", rejected["reason"])

        # 真实原树在执行期间变脏，替身成功也必须被结束复核拒绝。
        work, bundle, commit = source_workspace(self.root)
        self.configure(change_source_work=True)
        changed_work = self.start_with_source_work(work, bundle, commit, *argv)
        self.assertEqual(changed_work.returncode, 0, changed_work.stderr)
        rejected_work = self.wait(json.loads(changed_work.stdout.strip().splitlines()[-1])["result"])
        self.assertEqual((rejected_work["status"], rejected_work["failed_steps"]), ("failed", ["entry-orchestrator"]))
        self.assertIn("来源工作区核验失败", rejected_work["reason"])
        self._assert_existing_pre_a3_consumption(argv)

    def _assert_existing_pre_a3_consumption(self, argv: tuple[str, ...]) -> None:
        """双请求的演练准备实际调用原认证验证器；合成记录只验消费合同，不冒充九条真实链通过。"""
        from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
        from tools.official_client_capture.tests import test_codex_upgrade_policy_certification as policy_tests
        from tools.official_client_capture.tests import test_codex_upgrade_pre_a3_certification as certification_tests
        from tools.official_client_capture.tests import test_pre_a3_client_grouping as grouping_tests

        memo = {"CODEX_UPGRADE_IDENTITY_MEMO": str(self.root / "identity-memo")}
        with mock.patch.dict(os.environ, memo), mock.patch.dict(self.environ, memo):
            source_root = self.root / "source-certification"
            source_root.mkdir()
            _identity, compatibility, deployment, activation = grouping_tests._IdentityMemo.claude_bindings(self, source_root)
            store = certification_tests._SyntheticStore(source_root)
            entries = []
            registration = {item["id"]: item["test"] for item in pre_a3.real_chain_registration("claude")}
            for name in pre_a3.scenario_names("claude"):
                result = store.result(name)
                result["scenario"]["test"] = registration[name]
                result = pre_a3._sealed({key: value for key, value in result.items() if key != "result_sha256"}, "result_sha256")
                record = store.record(name, "entry-source", unit_id=f"pre-a3:claude:{name}", log_text=store.log_text(result))
                entries.append(store.entry(record))
            manifest = store.manifest("entry-source", entries)
            receipt = pre_a3.issue_certification_from_records(
                self.data / "staging" / "source-certification", unit_manifest=manifest, record_store=store.store.root,
                deployment_receipt=deployment, policy_activation=activation, client="claude")
            self.assertEqual(receipt["status"], "passed", receipt["failed_scenarios"])
            source = pre_a3.write_certification(source_root / "pre-a3.json", receipt)
            source_bytes = source.read_bytes()
            self.claude_receipt.write_bytes(deployment.read_bytes())
            # 本分支的总账、pre-A3 及全部校验依赖从真实源码包加载，不替换验证结果。
            (self.data / "tools/official_client_capture/codex_upgrade_project_ledger.py").unlink()
            for package in ("tools", "tools/official_client_capture"):
                (self.data / package / "__init__.py").write_text(
                    f"__path__.append({str(REPO_ROOT / package)!r})\n", encoding="utf-8")
            spec = self.root / "full79-spec.json"
            spec.write_text("{}", encoding="utf-8")
            values = dr._parse_env().parse(self.claude_env.read_text(encoding="utf-8"))
            values.update({"PRE_A3_CERTIFICATION": str(source), "POLICY_ACTIVATION": str(activation)})
            self.claude_env.write_text(dr._env_text(values), encoding="utf-8")
            self.configure()
            source_listing = sorted(str(path.relative_to(source_root)) for path in source_root.rglob("*"))
            started = self.start_claude(*argv)
            self.assertEqual(started.returncode, 0, started.stderr)
            payload = self.wait(json.loads(started.stdout.strip().splitlines()[-1])["result"])
            self.assertEqual(payload["status"], "passed", payload)
            prepared = self.calls()[-1]["env_file"]
            copied = Path(prepared["PRE_A3_CERTIFICATION"])
            self.assertNotEqual(copied, source)
            self.assertEqual(copied.read_bytes(), source_bytes)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            # 原入口之后生成新激活，原 record-reuse 必须把消费收据写演练目录，不能回写来源目录。
            current_activation = policy_tests._write_json(Path(prepared["POLICY_ACTIVATION"]),
                policy_certification.build_activation_certification(
                    self.claude_receipt, compatibility, observed_at_utc="2026-10-03T00:01:00Z", client="claude"))
            reuse = pre_a3.record_reuse(copied, deployment_receipt=self.claude_receipt, policy_activation=current_activation,
                                       receipt_root=copied.parent, client="claude")
            self.assertEqual(reuse["status"], "recorded", reuse)
            self.assertEqual(Path(reuse["reuse_receipt"]).parent, copied.parent)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(sorted(str(path.relative_to(source_root)) for path in source_root.rglob("*")), source_listing)

            # 有认证但无效时走真实 run 的 failed 落盘；不会启动编排器，也不降级为重跑九场景。
            calls_before = len(self.calls())
            def run_refusal(label: str, expected: str) -> None:
                conclusion = self.root / f"refused-{label}.json"
                dr._write(conclusion, {**payload, "status": "running", "runroot": str(self.root / f"refused-run-{label}"),
                                       "reason": None, "completed_at_utc": None})
                with mock.patch.dict(os.environ, self.environ):
                    self.assertEqual(dr.run(conclusion), 0)
                refused = dr._read(conclusion)
                self.assertEqual(refused["status"], "failed", refused)
                self.assertIsNotNone(refused["completed_at_utc"])
                self.assertIn(expected, refused["reason"])
                self.assertEqual(len(self.calls()), calls_before)

            wrong_client = {**receipt, "client": "codex"}
            wrong_client["receipt_sha256"] = pre_a3._fingerprint(
                {key: value for key, value in wrong_client.items() if key != "receipt_sha256"}, "claude")
            replaced_activation = json.loads(activation.read_bytes())
            replaced_activation.update({"status": "superseded", "superseded_by": "later-release.json"})
            replaced_activation["receipt_sha256"] = policy_certification._fingerprint(
                {key: value for key, value in replaced_activation.items() if key != "receipt_sha256"}, "claude")
            log = store.store.log_path(record["log"]["sha256"])
            mutations = (("bad-json", source, b"broken-json"),
                         ("wrong-client", source, json.dumps(wrong_client).encode()),
                         ("bad-log", log, log.read_bytes() + b"drift"),
                         ("replaced-activation", activation, json.dumps(replaced_activation).encode()),
                         ("missing-deployment", self.claude_receipt, None))
            for label, path, changed in mutations:
                with self.subTest(source_refusal=label):
                    original = path.read_bytes()
                    try:
                        path.unlink() if changed is None else path.write_bytes(changed)
                        run_refusal(label, "当前部署不可用" if changed is None else "原验证器")
                    finally:
                        path.write_bytes(original)

            # 故障只注入在真实验证器返回之后；原验证器照常执行，复核必须捕获源／部署在窗口内变化。
            real_run = subprocess.run
            for label, changed_path, changed_bytes in (("source-drift", source, source_bytes + b"\n"),
                                                        ("deployment-vanished", self.claude_receipt, None)):
                with self.subTest(after_verification=label):
                    original = changed_path.read_bytes()
                    verified = []
                    def mutate_after_verifier(command, *args, **kwargs):
                        completed = real_run(command, *args, **kwargs)
                        if "find-reusable" in command:
                            self.assertEqual(completed.returncode, 0, completed.stderr)
                            verified.append(True)
                            changed_path.unlink() if changed_bytes is None else changed_path.write_bytes(changed_bytes)
                        return completed
                    try:
                        with mock.patch.object(dr.subprocess, "run", side_effect=mutate_after_verifier):
                            run_refusal(label, "当前部署不可用" if changed_bytes is None else "核验期间变化")
                        self.assertEqual(verified, [True])
                    finally:
                        changed_path.write_bytes(original)

            # 旧 bridge 与普通源均保留原字节复制；Codex、缺请求、opening、reexecute 不消费此来源。
            target = self.root / "unused-certification.json"
            rehearsal = {"PRE_A3_CERTIFICATION": str(target)}
            dr._copy_pre_a3_source(payload, {**values, "CLAUDE_ENTRY_FULL79_SPEC": str(spec),
                                           "CLAUDE_ENTRY_FULL79_SPEC_SHA256": bv._sha256_file(spec)}, rehearsal, client="claude")
            self.assertEqual(target.read_bytes(), source_bytes)
            target.unlink()
            dr._copy_pre_a3_source(payload, {**values, "PRE_A3_CERTIFICATION": str(self.root / "missing-source.json")},
                                   rehearsal, client="claude")
            source.write_bytes(b"invalid-unused-source")
            for client, flags in (("codex", {}), ("claude", {"unit_inherit_request": None}),
                                   ("claude", {"opening": True}), ("claude", {"reexecute": True})):
                with self.subTest(unchanged_branch=(client, flags)):
                    dr._copy_pre_a3_source({**payload, **flags}, values, rehearsal, client=client)
                    self.assertFalse(target.exists())
            source.write_bytes(source_bytes)

    def test_companion_request_requires_the_primary_and_a_regular_absolute_file(self) -> None:
        primary, companion = self.root / "entry-request.json", self.root / "full-gates-request.json"
        primary.write_text("{}", encoding="utf-8")
        companion.write_text("{}", encoding="utf-8")
        orphan = self.start_claude("--unit-inherit-full-gates-request", str(companion))
        self.assertEqual(orphan.returncode, 2)
        self.assertIn("与 --unit-inherit-request 一起提供", orphan.stderr)
        link = self.root / "request-link.json"
        link.symlink_to(companion)
        for path in ("relative.json", str(self.root / "missing.json"), str(link)):
            with self.subTest(path=path):
                refused = self.start_claude("--unit-inherit-request", str(primary), "--unit-inherit-full-gates-request", path)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("已有的普通文件（绝对路径）", refused.stderr)

    def test_queued_two_request_dry_run_refuses_changed_bound_bytes_before_execution(self) -> None:
        primary, companion = self.root / "entry-request.json", self.root / "full-gates-request.json"
        for path in (primary, companion):
            path.write_text("{}", encoding="utf-8")
        payload = {"to": "atomic-double", "entry": str(self.entry),
                   "unit_inherit_request": {"path": str(primary), "sha256": bv._sha256_file(primary)},
                   "unit_inherit_full_gates_request": {"path": str(companion), "sha256": bv._sha256_file(companion)}}
        for path in (primary, companion):
            with self.subTest(path=path.name), mock.patch.object(dr.subprocess, "run") as execute:
                path.write_text('{"changed":true}', encoding="utf-8")
                with self.assertRaisesRegex(dr.DryRunError, "空跑启动后变化"):
                    dr._run_orchestrator(payload, self.claude_runroot, self.claude_env)
                execute.assert_not_called()
                path.write_text("{}", encoding="utf-8")

        # 单主请求与双请求均须在排队后复核，不能只守有配套请求的路径。
        single = {key: value for key, value in payload.items() if key != "unit_inherit_full_gates_request"}
        with mock.patch.object(dr.subprocess, "run") as execute:
            primary.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(dr.DryRunError, "空跑启动后变化"):
                dr._run_orchestrator(single, self.claude_runroot, self.claude_env)
            execute.assert_not_called()
            primary.write_text("{}", encoding="utf-8")

        policy = self.root / "unit-audit-policy.json"
        original = json.dumps({"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}})
        policy.write_text(original, encoding="utf-8")
        payload["audit_policy"] = {"path": str(policy), "sha256": bv._sha256_file(policy)}
        other = self.root / "other-policy.json"
        other.write_text(original, encoding="utf-8")
        for mutation in ("修改", "删除", "替换链接"):
            with self.subTest(mutation=mutation), mock.patch.object(dr.subprocess, "run") as execute:
                if mutation == "修改":
                    policy.write_text(original + "\n", encoding="utf-8")
                else:
                    policy.unlink()
                    if mutation == "替换链接":
                        policy.symlink_to(other)
                with self.assertRaises(dr.DryRunError):
                    dr._run_orchestrator(payload, self.claude_runroot, self.claude_env)
                execute.assert_not_called()
                policy.unlink(missing_ok=True)
                policy.write_text(original, encoding="utf-8")

        # 持住真实串行锁，让后台空跑进入排队后改策略；释放锁时须失败并且一次都未调用编排器。
        lock = dr._serial_lock_path({"data_root": str(self.data)})
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            started = self.start_claude("--unit-inherit-request", str(primary), "--audit-policy", str(policy))
            self.assertEqual(started.returncode, 0, started.stderr)
            result = json.loads(started.stdout.strip().splitlines()[-1])["result"]
            deadline = time.monotonic() + 30
            while not (dr._read(Path(result)) or {}).get("rehearsal_root"):
                self.assertLess(time.monotonic(), deadline, dr._read(Path(result)))
                time.sleep(0.05)
            policy.write_text(original + "\n", encoding="utf-8")
        finally:
            os.close(holder)
        rejected = self.wait(result)
        self.assertEqual(rejected["status"], "failed", rejected)
        self.assertIn("策略在空跑启动后变化", rejected["reason"])
        self.assertEqual(self.calls(), [], "排队后策略变化不能派发编排器")

        # 同一把真实串行锁下，来源树在排队期间变化也须拒绝，不能重建原树掩盖漂移。
        work, bundle, commit = source_workspace(self.root)
        holder = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            started = self.start_with_source_work(work, bundle, commit, "--unit-inherit-request", str(primary))
            self.assertEqual(started.returncode, 0, started.stderr)
            result = json.loads(started.stdout.strip().splitlines()[-1])["result"]
            deadline = time.monotonic() + 30
            while not (dr._read(Path(result)) or {}).get("rehearsal_root"):
                self.assertLess(time.monotonic(), deadline, dr._read(Path(result)))
                time.sleep(0.05)
            (work / "test-tree" / "source.txt").write_text("排队期间来源漂移", encoding="utf-8")
        finally:
            os.close(holder)
        rejected = self.wait(result)
        self.assertEqual(rejected["status"], "failed", rejected)
        self.assertIn("来源工作区核验失败", rejected["reason"])
        self.assertEqual(self.calls(), [], "排队后来源变化不能派发编排器")

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
                                        "--vc-env", str(env_file), "--entry-gates", str(gates), "--then-dryrun",
                                        "--preflight", "off", "--reuse", "off", *client],
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
        # 沿同一个自动衔接入口检查：本次带策略不能补造当前批准，也不能丢掉策略自动执行全集。
        for client in ("codex", "claude"):
            with self.subTest(audited_client=client), mock.patch.object(bv.subprocess, "run") as execute:
                skipped = bv._chain_dryrun({**validation, "client": client, "audit_policy": str(self.root / "registered-policy.json")})
                self.assertEqual(skipped["action"], "skipped")
                self.assertIn("--unit-inherit-request", skipped["reason"])
                self.assertIn("--audit-policy", skipped["reason"])
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
