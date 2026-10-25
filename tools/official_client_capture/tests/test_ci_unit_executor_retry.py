"""失败单元补跑（Codex 升级工具优化第 2 项，``tools/ci/retry_policy.py`` 与执行器 ``run-gates`` 的补跑阶段）。

全部用临时目录里生成的小命令与小测试模块驱动真实的执行器进程；状态目录、记录库与输出目录都在临时目录里。覆盖：

* 失败分类四类（执行失败／超时被杀／读集未闭合／环境失败），只有执行失败可补跑；
* 偶发（只红一次）的单元：机器腾空后只补跑它，通过则并入结论、标注“补跑通过”，两次日志都留，运行清单自检通过；
* 连红两次的单元：同一次运行里补跑再红即判红；
* 累计规则：最近 5 轮同一单元只许补跑通过 1 次，第 2 次再红直接判红、不再补跑；账本按单元 ID 写进记录库；
* 补跑关闭（缺省）时汇总、清单与改造前逐字相同；没有记录库、不可从头重跑的单元一律不补跑；
* 结论（``retry_policy.summary_problems``）对标注的自检：被藏起来的补跑、被改写的标注都能报出来。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.ci import retry_policy as rp
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"

# 第一次运行（标记文件不存在）失败并留下标记，之后都成功：模拟“只红一次”的偶发。
FLAKY_ONCE = textwrap.dedent("""\
    import os, sys
    flag = sys.argv[1]
    if not os.path.exists(flag):
        open(flag, "w").write("x")
        print("首次执行：模拟偶发失败")
        sys.exit(1)
    print("补跑：成功")
""")
# 永远失败（用来验证补跑再红即判红）。
ALWAYS_FAIL = "import sys\nprint('一直失败')\nsys.exit(3)\n"
# 每次运行追加一行计数，再按参数决定退出码。
COUNTING = textwrap.dedent("""\
    import sys
    open(sys.argv[1], "a").write("run\\n")
    sys.exit(int(sys.argv[2]))
""")


def _config(root: Path, **overrides: object) -> Path:
    payload = {
        "schema_version": ue.CONFIG_SCHEMA, "default_parallelism": 2, "default_quota": {"cores": 1, "memory_mb": 128},
        "quotas": {}, "splits": {}, "exclusive": [], "unit_timeout_seconds": 120, "orphan_grace_seconds": 1, **overrides,
    }
    path = root / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class RetryRunBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.store = self.root / "store"
        (self.root / "flaky.py").write_text(FLAKY_ONCE, encoding="utf-8")
        (self.root / "fail.py").write_text(ALWAYS_FAIL, encoding="utf-8")
        (self.root / "count.py").write_text(COUNTING, encoding="utf-8")

    def command_unit(self, unit_id: str, argv: list[str], *, restartable: bool = True, **extra: object) -> dict:
        unit = {"unit_id": unit_id, "argv": [sys.executable, "-B", *argv], "cwd": str(self.root), **extra}
        if restartable:
            unit["reservation_restartable"] = True
        return unit

    def run_gates(self, units: list[dict], *, retry: object = {"enabled": True}, out: str = "out", store: bool = True,
                  test_group: dict | None = None, config_extra: dict | None = None) -> tuple[subprocess.CompletedProcess, dict, dict]:
        gates = [{"gate_id": f"gate-{unit['unit_id']}", "units": [unit["unit_id"]]} for unit in units]
        payload: dict = {"schema_version": ue.GATES_SCHEMA, "units": units, "gates": gates}
        if test_group is not None:
            payload["test_groups"] = [test_group]
            payload["gates"].append({"gate_id": "tests", "test_groups": [test_group["group_id"]]})
        manifest = self.root / "gates.json"
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        overrides = dict(config_extra or {})
        if retry is not None:
            overrides["retry"] = retry
        command = [sys.executable, "-B", str(EXECUTOR), "run-gates", "--manifest", str(manifest), "--config", str(_config(self.root, **overrides)),
                   "--weights", str(self.root / "no-weights.json"), "--durations", str(self.root / "no-durations.json"), "--cores", "4",
                   "--state-dir", str(self.root / "state"), "--out-dir", str(self.root / out), "--shared-caches", "off"]
        if store:
            command += ["--record-store", str(self.store)]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=300,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT)})
        summary = json.loads((self.root / out / "summary.json").read_text(encoding="utf-8"))
        manifest_path = self.root / out / "unit-manifest.json"
        unit_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        return completed, summary, unit_manifest

    def events(self, out: str = "out") -> list[dict]:
        return [json.loads(line) for line in (self.root / out / "events.jsonl").read_text(encoding="utf-8").splitlines()]


class FlakyUnitRetryTests(RetryRunBase):
    def test_a_unit_that_fails_once_is_retried_alone_and_recorded_as_retry_passed(self) -> None:
        flag = self.root / "flag"
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(flag)]),
                 self.command_unit("steady:ok", ["-c", "pass"])]
        completed, summary, manifest = self.run_gates(units)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertEqual(summary["status"], "passed")
        rows = {row["unit_id"]: row for row in summary["units"]}
        self.assertTrue(rows["flaky:once"]["passed"])
        self.assertEqual(rows["flaky:once"]["kind"], "retry", "通过的是补跑尝试")
        self.assertNotIn("retry", rows["steady:ok"], "没失败过的单元没有补跑标注")
        marker = rows["flaky:once"]["retry"]
        self.assertEqual((marker["status"], marker["failure_class"]), ("retry-passed", "execution"))
        self.assertFalse(marker["first_attempt"]["passed"])
        self.assertTrue(marker["retry_attempt"]["passed"])
        self.assertEqual(marker["ledger"]["prior_retry_passed"], 0)
        # 汇总单列补跑通过；门禁项带 retry_passed_units；两次日志都在盘上且摘要与标注一致。
        self.assertEqual(summary["retry"]["retry_passed"], ["flaky:once"])
        gate = {item["gate_id"]: item for item in summary["gates"]}["gate-flaky:once"]
        self.assertEqual((gate["status"], gate["retry_passed_units"]), ("passed", ["flaky:once"]))
        self.assertEqual(rp.marker_file_problems(marker), [])
        self.assertNotEqual(marker["first_attempt"]["log"], marker["retry_attempt"]["log"])
        self.assertIn("首次执行", Path(marker["first_attempt"]["log"]).read_text(encoding="utf-8"))
        self.assertIn("补跑：成功", Path(marker["retry_attempt"]["log"]).read_text(encoding="utf-8"))
        self.assertEqual(rp.summary_problems(summary, require_files=True), [])
        # 只补跑了失败的那个单元，且是单独跑的（补跑启动时没有别的单元在跑）。
        starts = [event for event in self.events() if event["event"] == "start"]
        retries = [event for event in starts if event["kind"] == "retry"]
        self.assertEqual([event["unit"] for event in retries], ["flaky:once"])
        self.assertEqual(retries[0]["running"], ["flaky:once"])
        self.assertEqual([event["unit"] for event in starts if event["kind"] == "diagnostic"], [], "补跑取代了这个单元的诊断重跑")
        self.assertIn("补跑通过（算入结论，单列）：flaky:once", completed.stderr)

    def test_manifest_lists_the_retry_and_passes_the_unchanged_self_check(self) -> None:
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        _completed, summary, manifest = self.run_gates(units)
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])
        self.assertEqual(ur.verify_manifest(manifest, store=ur.RecordStore(self.store)), [], "记录模块的清单自检不用改")
        entry = manifest["units"][0]
        self.assertEqual(entry["retry"]["status"], "retry-passed")
        self.assertEqual(entry["record_sha256"], entry["retry"]["retry_attempt"]["record_sha256"], "清单引用补跑尝试的记录")
        self.assertEqual([item["unit_id"] for item in manifest["retries"]], ["flaky:once"])
        self.assertEqual(manifest["retries"][0]["of"], entry["retry"]["first_attempt"]["record_sha256"])
        store = ur.RecordStore(self.store)
        first = store.record_path("flaky:once", entry["retry"]["first_attempt"]["record_sha256"])
        retried = store.record_path("flaky:once", entry["retry"]["retry_attempt"]["record_sha256"])
        self.assertTrue(first.is_file() and retried.is_file(), "两条记录都入库")
        retry_record = json.loads(retried.read_text(encoding="utf-8"))
        self.assertEqual((retry_record["attempt"], retry_record["inheritable"], retry_record["kind"]), ("retry", False, "formal"))
        self.assertEqual(retry_record["retry_of"]["record_sha256"], entry["retry"]["first_attempt"]["record_sha256"])

    def test_retry_passed_record_is_never_an_inheritance_source(self) -> None:
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        _completed, _summary, manifest = self.run_gates(units)
        store = ur.RecordStore(self.store)
        record = json.loads(store.record_path("flaky:once", manifest["units"][0]["record_sha256"]).read_text(encoding="utf-8"))
        current = ur.Current(unit_id="flaky:once", unit_type="command", spec={}, spec_sha256=record["spec_sha256"], inputs=record["inputs"],
                             inputs_sha256=record["inputs_sha256"], inheritable=True)
        facts = ur.RunFacts(policy_sha256=record["policy_sha256"], environment=record["environment"], environment_sha256=record["environment_sha256"],
                            executor=record["executor"], max_age_hours=24.0, now=ur.parse_utc(record["completed_at_utc"]) or 0.0)
        problems = ur.check_record(store, store.record_path("flaky:once", record["record_sha256"]), record, current, facts)
        self.assertTrue(any("明确不可承接" in problem for problem in problems), problems)

    def test_a_flaky_unittest_module_is_retried_as_a_whole_unit(self) -> None:
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_flaky.py").write_text(textwrap.dedent(f"""\
            import os, unittest
            FLAG = {str(self.root / "unit-flag")!r}
            class FlakyTests(unittest.TestCase):
                def test_once(self):
                    if not os.path.exists(FLAG):
                        open(FLAG, "w").write("x")
                        self.fail("首次失败")
        """), encoding="utf-8")
        group = {"group_id": "tests", "start": "tests", "pattern": "test_*.py"}
        completed, summary, manifest = self.run_gates([], test_group=group)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        group_summary = summary["test_groups"]["tests"]
        self.assertEqual((group_summary["status"], group_summary["expected_tests"], group_summary["reported_tests"]), ("passed", 1, 1))
        self.assertEqual(summary["retry"]["retry_passed"], ["test_flaky"])
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])


class RetryFailureTests(RetryRunBase):
    def test_a_unit_that_fails_twice_in_the_same_run_is_red(self) -> None:
        completed, summary, manifest = self.run_gates([self.command_unit("always:fails", [str(self.root / "fail.py")])])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(summary["status"], "failed")
        marker = summary["units"][0]["retry"]
        self.assertEqual((marker["status"], summary["units"][0]["passed"]), ("retry-failed", False))
        self.assertEqual(summary["retry"]["retry_failed"], ["always:fails"])
        self.assertEqual(summary["failed_units"], ["always:fails"])
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])
        self.assertEqual(rp.summary_problems(summary), [])
        self.assertIn("补跑仍失败（判红）：always:fails", completed.stderr)
        # 补跑仍红的单元：只补跑了一次（首次＋一次补跑），不是无限重试。
        runs = [event for event in self.events() if event["event"] == "start" and event["unit"] == "always:fails"]
        self.assertEqual([event["kind"] for event in runs], ["formal", "retry"])

    def test_non_execution_failures_are_not_retried(self) -> None:
        units = [
            self.command_unit("timeout:slow", ["-c", "import time; time.sleep(30)"], timeout_seconds=1),
            self.command_unit("signal:killed", ["-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]),
            self.command_unit("missing:command", ["-c", "import sys; sys.exit(127)"]),
        ]
        completed, summary, _manifest = self.run_gates(units)
        self.assertEqual(completed.returncode, 1)
        markers = {row["unit_id"]: row["retry"] for row in summary["units"]}
        self.assertEqual({unit_id: (marker["status"], marker["failure_class"]) for unit_id, marker in markers.items()},
                         {"timeout:slow": ("not-retriable", "timeout"), "signal:killed": ("not-retriable", "environment"),
                          "missing:command": ("not-retriable", "environment")})
        self.assertEqual(sorted(summary["retry"]["not_retriable"]), sorted(markers))
        kinds = [(event["unit"], event["kind"]) for event in self.events() if event["event"] == "start"]
        self.assertEqual(sorted(kind for _unit, kind in kinds), ["diagnostic"] * 3 + ["formal"] * 3, "不补跑，只做不改结论的诊断重跑")
        self.assertEqual(sorted(item["unit_id"] for item in summary["diagnostic"]), sorted(markers))

    def test_no_record_store_or_not_restartable_unit_is_never_retried(self) -> None:
        flaky = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag-a")])]
        completed, summary, _ = self.run_gates(flaky, store=False)
        self.assertEqual(completed.returncode, 1)
        marker = summary["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-denied")
        self.assertIn("没有记录库", marker["reason"])
        not_restartable = [self.command_unit("scenario:once", [str(self.root / "flaky.py"), str(self.root / "flag-b")], restartable=False)]
        completed, summary, _ = self.run_gates(not_restartable, out="out2")
        self.assertEqual(completed.returncode, 1)
        marker = summary["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-denied")
        self.assertIn("不是可从头重跑的", marker["reason"])
        starts = [event for event in self.events("out2") if event["event"] == "start" and event["unit"] == "scenario:once"]
        self.assertEqual([event["kind"] for event in starts], ["formal", "diagnostic"])


class RetryAccumulationTests(RetryRunBase):
    def flaky_unit(self, name: str) -> dict:
        return self.command_unit("flaky:acc", [str(self.root / "flaky.py"), str(self.root / name)])

    def test_second_failure_within_five_rounds_is_red_without_retry(self) -> None:
        first_completed, first, _ = self.run_gates([self.flaky_unit("flag-1")], out="r1")
        self.assertEqual(first_completed.returncode, 0)
        self.assertEqual(first["retry"]["retry_passed"], ["flaky:acc"])
        # 第二轮：又偶发失败（换一个新的标记文件）——窗口内已补跑通过 1 次，直接判红，不再补跑。
        completed, second, _ = self.run_gates([self.flaky_unit("flag-2")], out="r2")
        self.assertEqual(completed.returncode, 1)
        marker = second["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-denied")
        self.assertEqual(marker["ledger"]["prior_retry_passed"], 1)
        self.assertIn("直接判红", marker["reason"])
        kinds = [event["kind"] for event in self.events("r2") if event["event"] == "start"]
        self.assertNotIn("retry", kinds)
        self.assertEqual(second["retry"]["retry_denied"], ["flaky:acc"])

    def test_ledger_is_written_per_unit_id_in_the_record_store(self) -> None:
        self.run_gates([self.flaky_unit("flag-1"), self.command_unit("steady:ok", ["-c", "pass"])], out="r1")
        ledger = rp.RetryLedger(self.store / rp.LEDGER_DIRECTORY)
        entries = ledger.entries("flaky:acc")
        self.assertEqual([entry["outcome"] for entry in entries], ["retry-passed"])
        self.assertEqual([entry["outcome"] for entry in ledger.entries("steady:ok")], ["passed"], "干净通过的单元也记一轮，窗口才数得准")
        self.assertTrue(all(entry["run_id"] for entry in entries))

    def test_retry_is_allowed_again_after_the_window_has_moved_on(self) -> None:
        self.run_gates([self.flaky_unit("flag-1")], out="r1")
        ledger = rp.RetryLedger(self.store / rp.LEDGER_DIRECTORY)
        # 之后连续 5 轮这个单元都干净通过：补跑通过那一轮滑出窗口。
        ledger.append({"unit_id": "flaky:acc", "run_id": f"later-{number}", "outcome": "passed"} for number in range(5))
        completed, summary, _ = self.run_gates([self.flaky_unit("flag-2")], out="r2")
        self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
        self.assertEqual(summary["retry"]["retry_passed"], ["flaky:acc"])
        self.assertEqual(summary["units"][0]["retry"]["ledger"]["prior_retry_passed"], 0)

    def test_window_of_four_rounds_still_blocks(self) -> None:
        self.run_gates([self.flaky_unit("flag-1")], out="r1")
        ledger = rp.RetryLedger(self.store / rp.LEDGER_DIRECTORY)
        ledger.append({"unit_id": "flaky:acc", "run_id": f"later-{number}", "outcome": "passed"} for number in range(4))
        completed, summary, _ = self.run_gates([self.flaky_unit("flag-2")], out="r2")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(summary["units"][0]["retry"]["status"], "retry-denied")

    def test_a_damaged_ledger_denies_retry_instead_of_relaxing_it(self) -> None:
        self.run_gates([self.command_unit("steady:ok", ["-c", "pass"])], out="r0")
        ledger = rp.RetryLedger(self.store / rp.LEDGER_DIRECTORY)
        ledger.path_for("flaky:acc").parent.mkdir(parents=True, exist_ok=True)
        ledger.path_for("flaky:acc").write_text("不是 JSON\n", encoding="utf-8")
        completed, summary, _ = self.run_gates([self.flaky_unit("flag-3")], out="r1")
        self.assertEqual(completed.returncode, 1)
        marker = summary["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-denied")
        self.assertIn("账本不可用", marker["reason"])


class RetryDisabledByDefaultTests(RetryRunBase):
    def test_without_retry_config_nothing_changes(self) -> None:
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        completed, summary, manifest = self.run_gates(units, retry=None)
        self.assertEqual(completed.returncode, 1, "不补跑：一红即判红")
        self.assertNotIn("retry", summary)
        self.assertNotIn("retries", manifest)
        self.assertNotIn("retry", summary["units"][0])
        self.assertNotIn("retry_passed_units", summary["gates"][0])
        self.assertEqual([item["unit_id"] for item in summary["diagnostic"]], ["flaky:once"], "原来的诊断重跑照旧")
        self.assertFalse((self.store / rp.LEDGER_DIRECTORY).exists(), "关闭时不写账本")

    def test_explicitly_disabled_retry_behaves_like_absent(self) -> None:
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        completed, summary, _ = self.run_gates(units, retry={"enabled": False})
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("retry", summary)

    def test_invalid_retry_config_is_a_configuration_error(self) -> None:
        for bad in ({"enabled": "yes"}, {"window_rounds": 5}, {"enabled": True, "pass_limit": 6}, {"enabled": True, "extra": 1},
                    {"enabled": True, "window_rounds": 0}):
            with self.subTest(bad=bad), self.assertRaises(rp.RetryPolicyError):
                rp.parse_config(bad)
        path = _config(self.root, retry={"enabled": "yes"})
        with self.assertRaisesRegex(ue.ExecutorError, "补跑段非法"):
            ue.load_config(path)

    def test_the_repository_config_turns_retry_on_with_five_rounds_and_one_pass(self) -> None:
        config = ue.load_config(REPO_ROOT / "tools" / "ci" / "unit_executor.json")
        self.assertEqual((config.retry.enabled, config.retry.window_rounds, config.retry.pass_limit), (True, 5, 1))


class FailureClassificationTests(unittest.TestCase):
    base = dict(is_command=True, exit_code=1, signal=None, timed_out=False, has_result=False, result_successful=False, audit_failed=False)

    def classify(self, **overrides: object) -> str | None:
        return rp.classify_failure(**{**self.base, **overrides})

    def test_four_classes(self) -> None:
        self.assertIsNone(self.classify(exit_code=0))
        self.assertEqual(self.classify(), rp.CLASS_EXECUTION)
        self.assertEqual(self.classify(timed_out=True, exit_code=None, signal=15), rp.CLASS_TIMEOUT)
        self.assertEqual(self.classify(exit_code=0, audit_failed=True), rp.CLASS_READ_SET)
        self.assertEqual(self.classify(exit_code=None, signal=9), rp.CLASS_ENVIRONMENT)
        self.assertEqual(self.classify(exit_code=127), rp.CLASS_ENVIRONMENT)
        self.assertEqual(self.classify(exit_code=126), rp.CLASS_ENVIRONMENT)
        self.assertEqual(self.classify(observed_failure=True), rp.CLASS_ENVIRONMENT)

    def test_test_units_need_a_result_file_to_count_as_execution_failure(self) -> None:
        test_unit = {**self.base, "is_command": False}
        self.assertEqual(rp.classify_failure(**{**test_unit, "has_result": True, "result_successful": False}), rp.CLASS_EXECUTION)
        self.assertEqual(rp.classify_failure(**{**test_unit, "has_result": False}), rp.CLASS_ENVIRONMENT, "没写出结果文件归环境，不补跑")
        self.assertIsNone(rp.classify_failure(**{**test_unit, "exit_code": 0, "has_result": True, "result_successful": True}))
        self.assertEqual(rp.classify_failure(**{**test_unit, "exit_code": 0, "has_result": True, "result_successful": False}), rp.CLASS_EXECUTION,
                         "进程退出 0 但报告失败：仍是测试失败")


class MarkerSelfCheckTests(RetryRunBase):
    def passed_summary(self) -> dict:
        units = [self.command_unit("flaky:once", [str(self.root / "flaky.py"), str(self.root / "flag")])]
        _completed, summary, _manifest = self.run_gates(units)
        return summary

    def test_a_hidden_retry_is_reported(self) -> None:
        summary = self.passed_summary()
        hidden = {key: value for key, value in summary.items() if key != "retry"}
        self.assertTrue(any("藏起来" in problem for problem in rp.summary_problems(hidden)))

    def test_a_forged_marker_is_reported(self) -> None:
        summary = self.passed_summary()
        for label, mutate in {
            "非执行失败却标补跑通过": lambda m: m.update(failure_class="timeout"),
            "没有补跑尝试": lambda m: m.update(retry_attempt=None),
            "账本窗口被改": lambda m: m["ledger"].update(window_rounds=99),
            "窗口内此前已补跑通过": lambda m: m["ledger"].update(prior_retry_passed=1),
            "两次尝试同一条记录": lambda m: m["retry_attempt"].update(record_sha256=m["first_attempt"]["record_sha256"]),
            "日志摘要被改": lambda m: m["first_attempt"].update(log_sha256="0" * 64),
        }.items():
            with self.subTest(label):
                forged = json.loads(json.dumps(summary))
                mutate(forged["units"][0]["retry"])
                forged["retry"]["units"] = {forged["units"][0]["unit_id"]: forged["units"][0]["retry"]}
                self.assertTrue(rp.summary_problems(forged, require_files=True), label)

    def test_passed_flag_must_match_the_retry_status(self) -> None:
        summary = self.passed_summary()
        forged = json.loads(json.dumps(summary))
        forged["units"][0]["retry"]["status"] = "retry-failed"
        forged["retry"]["retry_failed"], forged["retry"]["retry_passed"] = forged["retry"]["retry_passed"], []
        self.assertTrue(any("通过标志" in problem for problem in rp.summary_problems(forged)))


class RetryDriverCopyTests(unittest.TestCase):
    def test_driver_carries_identical_copies_of_every_file_the_executor_loads(self) -> None:
        """执行器按“同目录”加载 retry_policy.py：驱动目录必须带一份与 tools/ci 逐字节相同的，否则 ARM64 上打开补跑就找不到模块。"""

        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("retry_policy.py", "unit_executor.py", "unit_executor.json", "entry_gates.py", "entry_housekeeping.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes(), f"驱动里的 {name} 与 tools/ci 原件不一致")


if __name__ == "__main__":
    unittest.main()
