"""统一调度执行器（E2-01，``tools/ci/unit_executor.py``）：额度、独占、隔离、诊断、全集核对、采集预约。

全部用临时目录里生成的小测试模块驱动真实的执行器进程（每个单元一个子进程），不触碰真实测试集与本机调度状态；
状态目录与输出目录都在临时目录里，和本机正在运行的调度器互不干扰。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tools.ci import unit_executor as ue

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"


def _write_modules(root: Path, modules: dict[str, str]) -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    for name, body in modules.items():
        (tests / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return tests


def _config(root: Path, **overrides: object) -> Path:
    payload = {
        "schema_version": ue.CONFIG_SCHEMA,
        "default_parallelism": 2,
        "default_quota": {"cores": 1, "memory_mb": 128},
        "quotas": {},
        "splits": {},
        "exclusive": [],
        "unit_timeout_seconds": 120,
        "orphan_grace_seconds": 1,
        **overrides,
    }
    path = root / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _weights(root: Path, weights: dict[str, float]) -> Path:
    path = root / "weights.json"
    path.write_text(json.dumps({"schema_version": ue.WEIGHTS_SCHEMA, "weights": weights}), encoding="utf-8")
    return path


def _run(root: Path, tests: Path, *, parallel: int, cores: int, config: Path, weights: Path | None = None,
         out: str = "out") -> tuple[subprocess.CompletedProcess[str], dict, list[dict]]:
    # 调度类用例不准备字节码共享层（单元按原环境运行）；共享层由 UnitExecutorBytecodeTests 专门验证。
    command = [
        sys.executable, str(EXECUTOR), "run", "--start", str(tests), "--config", str(config),
        "--weights", str(weights or root / "no-weights.json"), "--durations", str(root / "no-durations.json"),
        "--parallel", str(parallel), "--cores", str(cores), "--state-dir", str(root / "state"), "--out-dir", str(root / out),
        "--shared-caches", "off",
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    summary = json.loads((root / out / "summary.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (root / out / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return completed, summary, events


SLEEPER = """
import time, unittest
class SleepTests(unittest.TestCase):
    def test_sleep(self):
        time.sleep({seconds})
"""

# 字节码共享层用例的替身预编译工具：成功的只建空前缀，失败的直接退出 1（真实工具由 UnitExecutorBytecodeTests 单独跑一次）。
OK_HELPER = (
    "import json, sys\nfrom pathlib import Path\n"
    "Path(sys.argv[1]).mkdir(parents=True, exist_ok=True)\n"
    "print(json.dumps({'status': 'ready', 'stdlib_pyc': 0, 'sources_pyc': {}}))\n"
)
FAIL_HELPER = "import json, sys\nprint(json.dumps({'status': 'failed', 'error': '示例：预编译失败'}))\nsys.exit(1)\n"

# 单元里的探针：记录本单元进程看到的字节码前缀、禁写开关，以及本模块的 .pyc 是否命中前缀里的预编译产物。
PREFIX_PROBE = """
import json, os, sys, unittest
class PrefixTests(unittest.TestCase):
    def test_prefix(self):
        module = sys.modules[__name__]
        with open(os.environ["E202_PROBE_OUT"], "w", encoding="utf-8") as handle:
            json.dump({"prefix": sys.pycache_prefix, "env": os.environ.get("PYTHONPYCACHEPREFIX"), "dont_write": sys.dont_write_bytecode,
                       "memo": os.environ.get("CODEX_UPGRADE_IDENTITY_MEMO"),
                       "cached": module.__cached__, "cached_exists": os.path.isfile(module.__cached__)}, handle)
"""


class UnitExecutorPlanTests(unittest.TestCase):
    def test_plan_splits_heavy_module_isolates_exclusive_tests_and_covers_full_set(self) -> None:
        grouped = {
            "test_heavy": [f"test_heavy.HeavyTests.test_{i}" for i in range(6)],
            "test_timing": ["test_timing.TimingTests.test_fast", "test_timing.TimingTests.test_watchdog"],
            "test_light": ["test_light.LightTests.test_a"],
        }
        config = ue.ExecutorConfig(
            default_parallelism=3, default_quota=ue.Quota(1, 128), quotas={"test_heavy": ue.Quota(8, 512)},
            splits={"test_heavy": 3}, exclusive=(("test_timing.TimingTests.test_watchdog", "亚秒级看门狗"),),
            unit_timeout_seconds=60, orphan_grace_seconds=1, raw={},
        )
        durations = {f"test_heavy.HeavyTests.test_{i}": float(10 - i) for i in range(6)}
        units = ue.plan_units(grouped, config, {}, durations, machine_cores=4)
        by_id = {unit.unit_id: unit for unit in units}
        self.assertEqual(sorted(by_id), ["test_heavy#1", "test_heavy#2", "test_heavy#3", "test_light", "test_timing", "test_timing!exclusive"])
        self.assertEqual(by_id["test_timing!exclusive"].test_ids, ("test_timing.TimingTests.test_watchdog",))
        self.assertTrue(by_id["test_timing!exclusive"].exclusive)
        self.assertEqual(by_id["test_heavy#1"].quota.cores, 4, "额度不超过整机核数")
        self.assertEqual(sorted(t for unit in units for t in unit.test_ids), sorted(t for ids in grouped.values() for t in ids))
        weights = sorted(by_id[f"test_heavy#{i}"].weight for i in (1, 2, 3))
        self.assertLessEqual(weights[-1] - weights[0], 3.0, "最长优先法拆块应大致均衡")

    def test_config_rejects_unknown_module_and_unexplained_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _config(root, exclusive=[{"tests": "test_x.A.test_b", "reason": " "}])
            with self.assertRaisesRegex(ue.ExecutorError, "根因"):
                ue.load_config(path)
            path = _config(root, splits={"test_missing": {"chunks": 2, "reason": "重模块"}})
            config = ue.load_config(path)
            with self.assertRaisesRegex(ue.ExecutorError, "不存在的模块"):
                ue.plan_units({"test_a": ["test_a.A.test_x"]}, config, {}, {}, machine_cores=2)

    def test_full_run_rejects_stale_entries_while_subset_run_ignores_entries_outside_it(self) -> None:
        config = ue.ExecutorConfig(
            default_parallelism=2, default_quota=ue.Quota(1, 128), quotas={}, splits={"test_heavy": 2},
            exclusive=(("test_timing.TimingTests.test_watchdog", "亚秒级看门狗"),), unit_timeout_seconds=60, orphan_grace_seconds=1, raw={},
        )
        subset = {"test_light": ["test_light.LightTests.test_a"]}
        self.assertEqual([u.unit_id for u in ue.plan_units(subset, config, {}, {}, machine_cores=2, full_set=False)], ["test_light"])
        with self.assertRaisesRegex(ue.ExecutorError, "不存在的模块"):
            ue.plan_units(subset, config, {}, {}, machine_cores=2)
        # 独占条目对应的测试改了名：全量运行报错（名单不能静默失效），只跑部分模块时照常规划。
        renamed = {**subset, "test_heavy": ["test_heavy.H.test_1", "test_heavy.H.test_2"],
                   "test_timing": ["test_timing.TimingTests.test_watchdog_renamed"]}
        with self.assertRaisesRegex(ue.ExecutorError, "没有命中任何测试"):
            ue.plan_units(renamed, config, {}, {}, machine_cores=2)
        units = ue.plan_units(renamed, config, {}, {}, machine_cores=2, full_set=False)
        self.assertEqual(sorted(u.unit_id for u in units), ["test_heavy#1", "test_heavy#2", "test_light", "test_timing"])

    def test_full_set_check_rejects_missing_duplicated_and_unexpected_results(self) -> None:
        quota = ue.Quota(1, 128)
        unit_a = ue.Unit("test_a", "test_a", ("test_a.A.test_1", "test_a.A.test_2"), quota, False, 1.0)
        unit_b = ue.Unit("test_b", "test_b", ("test_b.B.test_1",), quota, False, 1.0)
        expected = {"test_a.A.test_1", "test_a.A.test_2", "test_b.B.test_1"}

        def outcome(unit: ue.Unit, tests: dict[str, str]) -> ue.Outcome:
            return ue.Outcome(unit=unit, kind="formal", exit_code=0, signal=None, timed_out=False, seconds=1.0, cpu_seconds=1.0,
                              max_rss_mb=1.0, orphans=[], log_path=Path("x.log"),
                              result={"successful": True, "tests": {t: {"outcome": o} for t, o in tests.items()}})

        ok = ue.summarize([unit_a, unit_b], [outcome(unit_a, {"test_a.A.test_1": "passed", "test_a.A.test_2": "passed"}),
                                             outcome(unit_b, {"test_b.B.test_1": "passed"})], [], expected, 1.0, "p")
        self.assertEqual(ok["status"], "passed")
        missing = ue.summarize([unit_a, unit_b], [outcome(unit_a, {"test_a.A.test_1": "passed"}),
                                                  outcome(unit_b, {"test_b.B.test_1": "passed"})], [], expected, 1.0, "p")
        self.assertEqual((missing["status"], missing["full_set"]["missing"]), ("failed", ["test_a.A.test_2"]))
        duplicated = ue.summarize([unit_a, unit_b], [outcome(unit_a, {"test_a.A.test_1": "passed", "test_a.A.test_2": "passed"}),
                                                     outcome(unit_b, {"test_b.B.test_1": "passed", "test_a.A.test_1": "passed"})],
                                  [], expected, 1.0, "p")
        self.assertEqual((duplicated["status"], duplicated["full_set"]["duplicated"]), ("failed", ["test_a.A.test_1"]))
        unexpected = ue.summarize([unit_a, unit_b], [outcome(unit_a, {"test_a.A.test_1": "passed", "test_a.A.test_2": "passed",
                                                                      "setUpClass (test_a.A)": "error"}),
                                                     outcome(unit_b, {"test_b.B.test_1": "passed"})], [], expected, 1.0, "p")
        self.assertEqual(unexpected["status"], "failed")
        self.assertTrue(unexpected["full_set"]["unexpected"])
        not_run = ue.summarize([unit_a, unit_b], [outcome(unit_a, {"test_a.A.test_1": "passed", "test_a.A.test_2": "passed"})],
                               [], expected, 1.0, "p")
        self.assertEqual(not_run["full_set"]["units_not_run"], ["test_b"])


class UnitExecutorRunTests(unittest.TestCase):
    def test_quota_never_exceeds_machine_cores_and_exclusive_unit_runs_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = {f"test_m{i}": SLEEPER.format(seconds=0.4) for i in range(6)}
            modules["test_timing"] = SLEEPER.format(seconds=0.4)
            tests = _write_modules(root, modules)
            config = _config(root, quotas={"test_m0": {"cores": 2, "memory_mb": 128}},
                             exclusive=[{"tests": "test_timing", "reason": "示例：亚秒级计时断言"}])
            completed, summary, events = _run(root, tests, parallel=8, cores=2, config=config)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertLessEqual(max(event["cores_in_use"] for event in events), 2)
            self.assertLessEqual(summary["max_cores_in_use"], 2)
            starts = [i for i, event in enumerate(events) if event["event"] == "start" and event["unit"] == "test_timing!exclusive"]
            exits = [i for i, event in enumerate(events) if event["event"] == "exit" and event["unit"] == "test_timing!exclusive"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(events[starts[0]]["running"], ["test_timing!exclusive"], "独占单元启动时不得有别的单元在跑")
            between = [event for event in events[starts[0] + 1:exits[0]] if event["event"] == "start"]
            self.assertEqual(between, [], "独占单元运行期间不得派发别的单元")

    def test_failures_and_signals_stay_isolated_and_diagnostics_do_not_change_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = _write_modules(root, {
                "test_ok": "import unittest\nclass OkTests(unittest.TestCase):\n    def test_ok(self): pass\n",
                "test_fail": "import unittest\nclass FailTests(unittest.TestCase):\n    def test_fail(self): self.assertEqual(1, 2)\n",
                "test_killed": "import os, signal, unittest\nclass KilledTests(unittest.TestCase):\n"
                               "    def test_killed(self): os.kill(os.getpid(), signal.SIGKILL)\n",
            })
            completed, summary, _events = _run(root, tests, parallel=3, cores=3, config=_config(root))
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(sorted(summary["failed_units"]), ["test_fail", "test_killed"])
            rows = {row["unit_id"]: row for row in summary["units"]}
            self.assertTrue(rows["test_ok"]["passed"])
            self.assertEqual(rows["test_killed"]["signal"], 9)
            self.assertEqual(summary["full_set"]["missing"], ["test_killed.KilledTests.test_killed"])
            self.assertEqual(sorted(item["unit_id"] for item in summary["diagnostic"]), ["test_fail", "test_killed"])
            records = sorted((root / "out" / "units").glob("*.record.json"))
            kinds = {json.loads(path.read_text())["unit_id"] + "/" + json.loads(path.read_text())["kind"] for path in records}
            self.assertIn("test_fail/diagnostic", kinds)
            self.assertIn("test_fail/formal", kinds)
            self.assertIn("单元未通过：test_killed（信号 9", completed.stderr)

    def test_serial_and_parallel_runs_give_identical_outcomes_and_p0_parsable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = (
                "import unittest\nclass Tests(unittest.TestCase):\n"
                "    def test_a(self): pass\n    def test_b(self): pass\n"
                "    @unittest.skip('示例跳过')\n    def test_skipped(self): pass\n"
            )
            tests = _write_modules(root, {f"test_m{i}": body for i in range(4)})
            config = _config(root, splits={"test_m0": {"chunks": 2, "reason": "示例拆块"}})
            serial, serial_summary, _ = _run(root, tests, parallel=1, cores=4, config=config, out="serial")
            parallel, parallel_summary, _ = _run(root, tests, parallel=4, cores=4, config=config, out="parallel")
            self.assertEqual((serial.returncode, parallel.returncode), (0, 0))

            def outcomes(out: str) -> dict[str, str]:
                merged: dict[str, str] = {}
                for path in (root / out / "units").glob("*.record.json"):
                    for test_id, record in (json.loads(path.read_text())["tests"] or {}).items():
                        merged[test_id] = record["outcome"]
                return merged

            self.assertEqual(outcomes("serial"), outcomes("parallel"))
            self.assertEqual(len(outcomes("serial")), 12)
            for completed in (serial, parallel):
                ran = re.findall(r"^Ran (\d+) tests? in", completed.stderr, re.M)
                ok = re.findall(r"^OK(?: \((?:skipped=(\d+))?\))?$", completed.stderr, re.M)
                self.assertEqual(ran, ["12"])
                self.assertEqual(ok, ["4"])
            self.assertEqual(serial_summary["policy_sha256"] == parallel_summary["policy_sha256"], False, "并行度属于调度策略版本")

    def test_capture_reservation_waits_for_running_units_and_blocks_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules = {"test_slow": SLEEPER.format(seconds=2.5)}
            modules.update({f"test_q{i}": SLEEPER.format(seconds=0.1) for i in range(3)})
            tests = _write_modules(root, modules)
            weights = _weights(root, {"test_slow": 100.0})
            command = [
                sys.executable, str(EXECUTOR), "run", "--start", str(tests), "--config", str(_config(root)),
                "--weights", str(weights), "--durations", str(root / "none.json"), "--parallel", "1", "--cores", "1",
                "--state-dir", str(root / "state"), "--out-dir", str(root / "out"), "--shared-caches", "off",
            ]
            process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            events_path = root / "out" / "events.jsonl"
            deadline = time.monotonic() + 30
            while not (events_path.exists() and '"unit": "test_slow"' in events_path.read_text()):
                self.assertLess(time.monotonic(), deadline, "慢单元没有启动")
                time.sleep(0.05)
            grant = ue.acquire(root / "state", "capture-test", os.getpid(), 30)
            self.assertTrue(grant["granted_by"].startswith("scheduler-"))
            events = [json.loads(line) for line in events_path.read_text().splitlines()]
            names = [event["event"] for event in events]
            granted_at = names.index("reservation-granted")
            self.assertIn("exit", names[:granted_at], "批准前在跑的慢单元必须已经结束")
            self.assertEqual(events[granted_at]["running"], [])
            time.sleep(0.6)
            after = [json.loads(line) for line in events_path.read_text().splitlines()][granted_at + 1:]
            self.assertEqual([event for event in after if event["event"] == "start"], [], "预约期间不得派发新单元")
            self.assertTrue(ue.release(root / "state", "capture-test"))
            _stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            summary = json.loads((root / "out" / "summary.json").read_text())
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(len(summary["units"]), 4)

    def test_run_started_outside_make_never_writes_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = _write_modules(root, {"test_flag": (
                "import os, sys, unittest\nclass FlagTests(unittest.TestCase):\n"
                "    def test_flag(self):\n"
                "        self.assertTrue(sys.dont_write_bytecode)\n"
                "        self.assertEqual(os.environ.get('PYTHONDONTWRITEBYTECODE'), '1')\n"
            )})
            (root / "helper-ok.py").write_text(OK_HELPER, encoding="utf-8")
            command = [
                sys.executable, str(EXECUTOR), "run", "--start", str(tests), "--config", str(_config(root)),
                "--weights", str(root / "none.json"), "--durations", str(root / "none.json"), "--parallel", "1", "--cores", "1",
                "--state-dir", str(root / "state"), "--out-dir", str(root / "out"), "--bytecode-helper", str(root / "helper-ok.py"),
            ]
            environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
            completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, env=environment)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(sorted(str(path) for path in tests.rglob("__pycache__")), [], "调度进程与单元子进程都不得写字节码")
            self.assertEqual(sorted(str(path) for path in (root / "out" / "pycache-shared").rglob("*.pyc")), [], "单元只读使用共享层")

    def test_subset_run_with_repository_config_is_planned_without_stale_entry_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = _write_modules(root, {"test_m0": SLEEPER.format(seconds=0)})
            completed, summary, _events = _run(root, tests, parallel=2, cores=2, config=REPO_ROOT / ue.DEFAULT_CONFIG)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((summary["status"], summary["scope"]), ("passed", "subset"))


class UnitExecutorBytecodeTests(unittest.TestCase):
    """E2-02 共享缓存：字节码共享层没有前缀时先预编译、单元只读使用，已有前缀就沿用，预编译失败不拖住其余单元但结论
    判失败；身份记忆化目录没有就在记录目录里新建、有就沿用，全部单元共用；``--shared-caches off`` 两样都不准备。"""

    def _run_probe(self, root: Path, *extra: str, inherited: Path | None = None,
                   memo: Path | None = None) -> tuple[subprocess.CompletedProcess[str], dict, dict, list[str]]:
        tests = _write_modules(root, {"test_probe": PREFIX_PROBE})
        command = [
            sys.executable, str(EXECUTOR), "run", "--start", str(tests), "--config", str(_config(root)),
            "--weights", str(root / "none.json"), "--durations", str(root / "none.json"), "--parallel", "2", "--cores", "2",
            "--state-dir", str(root / "state"), "--out-dir", str(root / "out"), *extra,
        ]
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPYCACHEPREFIX", ue.IDENTITY_MEMO_ENV}}
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", "E202_PROBE_OUT": str(root / "probe.json")})
        if inherited is not None:
            environment["PYTHONPYCACHEPREFIX"] = str(inherited)
        if memo is not None:
            environment[ue.IDENTITY_MEMO_ENV] = str(memo)
        completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600, env=environment)
        summary = json.loads((root / "out" / "summary.json").read_text(encoding="utf-8"))
        probe = json.loads((root / "probe.json").read_text(encoding="utf-8"))
        names = [json.loads(line)["event"] for line in (root / "out" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(str(path) for path in tests.rglob("__pycache__")), [])
        return completed, summary, probe, names

    def test_shared_layer_is_precompiled_before_any_unit_and_units_hit_it_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tests = root / "tests"
            # 真实预编译工具（驱动 bytecode_cache.py）：标准库按时间戳、源码目录按内容摘要。
            completed, summary, probe, names = self._run_probe(root, "--bytecode-source", str(tests))
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            prefix = str((root / "out" / "pycache-shared").resolve())
            self.assertEqual((summary["bytecode_cache"]["status"], summary["bytecode_cache"]["prefix"]), ("ready", prefix))
            self.assertEqual((probe["prefix"], probe["env"], probe["dont_write"]), (prefix, prefix, True))
            self.assertTrue(probe["cached"].startswith(prefix) and probe["cached_exists"], probe)
            self.assertLess(names.index("bytecode-exit"), names.index("start"), "预编译在任何单元之前完成")
            self.assertIn("字节码共享层：预编译", completed.stderr)
            memo = str((root / "out" / "identity-memo").resolve())
            self.assertEqual((summary["identity_memo"], probe["memo"]), (memo, memo), "身份记忆化目录建在记录目录里、交给单元")

    def test_inherited_prefix_is_used_without_precompiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inherited = root / "pycache-inherited"
            inherited.mkdir()
            (root / "helper-fail.py").write_text(FAIL_HELPER, encoding="utf-8")
            memo = root / "memo-inherited"
            completed, summary, probe, names = self._run_probe(root, "--bytecode-helper", str(root / "helper-fail.py"), inherited=inherited, memo=memo)
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            self.assertEqual((summary["bytecode_cache"]["status"], summary["bytecode_cache"]["prefix"]), ("inherited", str(inherited)))
            self.assertNotIn("bytecode-start", names, "已有前缀时不得再预编译")
            self.assertEqual(probe["prefix"], str(inherited))
            self.assertEqual((summary["identity_memo"], probe["memo"]), (str(memo), str(memo)), "环境里已有的记忆化目录照样沿用")

    def test_shared_caches_off_prepares_neither(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "helper-fail.py").write_text(FAIL_HELPER, encoding="utf-8")
            completed, summary, probe, names = self._run_probe(root, "--shared-caches", "off", "--bytecode-helper", str(root / "helper-fail.py"))
            self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
            self.assertEqual((summary["bytecode_cache"]["status"], summary["identity_memo"]), ("off", None))
            self.assertNotIn("bytecode-start", names)
            self.assertEqual((probe["prefix"], probe["memo"]), (None, None))

    def test_identity_memo_variable_matches_the_managed_module(self) -> None:
        from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

        self.assertEqual(ue.IDENTITY_MEMO_ENV, tip.IDENTITY_MEMO_ENV)

    def test_precompile_failure_fails_the_run_while_units_still_run_without_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "helper-fail.py").write_text(FAIL_HELPER, encoding="utf-8")
            completed, summary, probe, _names = self._run_probe(root, "--bytecode-helper", str(root / "helper-fail.py"))
            self.assertEqual(completed.returncode, 1)
            self.assertEqual((summary["status"], summary["bytecode_cache"]["status"]), ("failed", "failed"))
            self.assertEqual(summary["failed_units"], [], "单元照跑且通过，失败只来自共享层")
            self.assertIsNone(probe["prefix"], "预编译失败时不设前缀")
            self.assertIn("字节码共享层预编译失败", completed.stderr)
            self.assertTrue(completed.stderr.strip().splitlines()[-1].startswith("FAILED (failures=0, errors=1"), completed.stderr[-500:])


class UnitExecutorRepositoryConfigTests(unittest.TestCase):
    def test_repository_config_plans_the_real_full_set(self) -> None:
        """仓库里的调度配置对真实全集规划：登记的拆块与独占都命中现有测试、规划闭合；测试改名或删除后这里先变红。"""

        completed = subprocess.run([sys.executable, str(EXECUTOR), "plan"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=600,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])
        plan = json.loads(completed.stdout)
        self.assertEqual(plan["scope"], "full")
        config = ue.load_config(REPO_ROOT / ue.DEFAULT_CONFIG)
        unit_ids = {unit["unit_id"] for unit in plan["units"]}
        for module, chunks in config.splits.items():
            self.assertEqual({f"{module}#{i}" for i in range(1, chunks + 1)} & unit_ids, {f"{module}#{i}" for i in range(1, chunks + 1)})
        exclusive_modules = {prefix.split(".")[0] for prefix, _reason in config.exclusive}
        self.assertEqual({unit_id for unit_id in unit_ids if unit_id.endswith("!exclusive")}, {f"{m}!exclusive" for m in exclusive_modules})
        self.assertEqual(sum(unit["tests"] for unit in plan["units"]), plan["tests"])


if __name__ == "__main__":
    unittest.main()
