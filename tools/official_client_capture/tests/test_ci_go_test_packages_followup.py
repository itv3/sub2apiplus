"""Go 按包补跑的验收修正（必修 5 与建议 9，``tools/ci/go_test_packages.py``）。

用一个脚本化的假 ``go``（Python 脚本放在临时目录并排在 PATH 最前）驱动真实的包装器进程与真实的执行器进程，不依赖本机装了 Go：

* 必修 5：缩到单个用例补跑通过后，对该包再整包补跑一次，整包通过才算这个包补跑通过；整包确认没过则补跑仍红；缩到用例没过就不必确认；
  补跑标注写明“缩到用例＋整包确认”；
* 建议 9：go 不在 PATH 里（127）、go list 失败（125）、go 子进程被信号杀（128＋信号，如 137、143）都按“环境失败”处理，执行器不补跑；测试失败仍退出 1。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.ci import retry_policy as rp
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "tools" / "ci" / "go_test_packages.py"

# 脚本化的假 go：读 FAKE_GO_SCRIPT（JSON），按顺序消费 ``runs``（每次 ``go test -json …`` 一项），``go list`` 与 ``go test -list`` 不消费。
# 每次调用的参数追加到 FAKE_GO_SCRIPT 同名 .calls 文件，供测试断言“跑了哪些命令”。
FAKE_GO = textwrap.dedent("""\
    #!%(python)s
    import json, os, sys
    path = os.environ["FAKE_GO_SCRIPT"]
    script = json.load(open(path))
    args = sys.argv[1:]
    with open(path + ".calls", "a") as handle:
        handle.write(json.dumps(args) + "\\n")
    if args[0] == "list":
        if script.get("list_fail"):
            sys.stderr.write("packages.Load: boom\\n")
            sys.exit(1)
        if "-json" in args:
            print(json.dumps({"Dir": script["dir"], "TestGoFiles": ["x_test.go"]}))
        else:
            print("\\n".join(script["packages"]))
        sys.exit(0)
    if args[0] == "test" and "-list" in args:
        print("\\n".join(script["tests"][args[-1]] + ["ok  \\t" + args[-1] + "\\t0.01s"]))
        sys.exit(0)
    index = len([1 for line in open(path + ".calls") if json.loads(line)[0] == "test" and "-list" not in json.loads(line)]) - 1
    run = script["runs"][index]
    for item in run.get("events", []):
        print(json.dumps(item))
    if run.get("signal"):
        os.kill(os.getpid(), run["signal"])
    sys.exit(run.get("exit", 0))
""")


def ev(action: str, package: str, test: str | None = None) -> dict:
    return {"Action": action, "Package": package, **({"Test": test} if test else {})}


def package_events(package: str, results: dict[str, str]) -> list[dict]:
    """一个包的事件流：每个顶层用例 run＋终态，最后是包级终态（任一用例 fail 则包 fail）。"""

    events: list[dict] = []
    for name, action in results.items():
        events += [ev("run", package, name), ev(action, package, name)]
    events.append(ev("fail" if "fail" in results.values() else "pass", package))
    return events


class FakeGoBase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "go").write_text(FAKE_GO % {"python": sys.executable}, encoding="utf-8")
        (self.bin / "go").chmod(0o755)
        self.package_dir = self.root / "pkg"
        self.package_dir.mkdir()
        (self.package_dir / "x_test.go").write_text("package x\n\nfunc TestFlaky() {}\n", encoding="utf-8")   # 没有 TestMain／init：允许缩到用例
        self.script = self.root / "fake-go.json"
        self.sidecar_dir = self.root / "sidecars"
        self.sidecar_dir.mkdir()

    def write_script(self, **fields: object) -> None:
        base = {"packages": ["p/a", "p/b"], "dir": str(self.package_dir), "tests": {"p/a": ["TestFlaky", "TestOther"], "p/b": ["TestB"]}, "runs": []}
        self.script.write_text(json.dumps({**base, **fields}), encoding="utf-8")
        calls = Path(str(self.script) + ".calls")
        if calls.exists():
            calls.unlink()

    def calls(self) -> list[list[str]]:
        path = Path(str(self.script) + ".calls")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def go_test_runs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] == "test" and "-list" not in call]

    def environment(self, **extra: str) -> dict[str, str]:
        return {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_GO_SCRIPT": str(self.script), "PYTHONDONTWRITEBYTECODE": "1", **extra}

    def wrapper(self, kind: str = "formal", **extra: str) -> subprocess.CompletedProcess:
        sidecar = {"UNIT_EXECUTOR_KIND": kind, "UNIT_EXECUTOR_SIDECAR": str(self.sidecar_dir / f"{kind}.json"),
                   "UNIT_EXECUTOR_FIRST_SIDECAR": str(self.sidecar_dir / "formal.json")}
        return subprocess.run([sys.executable, "-B", str(WRAPPER), "--tags", "unit", "--pattern", "./..."], capture_output=True, text=True, timeout=120,
                              env=self.environment(**sidecar, **extra))

    def sidecar(self, kind: str) -> dict:
        return json.loads((self.sidecar_dir / f"{kind}.json").read_text(encoding="utf-8"))


FIRST_PASS_FLAKY = [*package_events("p/a", {"TestFlaky": "fail", "TestOther": "pass"}), *package_events("p/b", {"TestB": "pass"})]


class NarrowedRetryNeedsWholePackageConfirmationTests(FakeGoBase):
    def first_pass(self) -> None:
        completed = self.wrapper("formal")
        self.assertEqual(completed.returncode, 1, completed.stdout[-1500:])

    def test_narrowed_pass_is_followed_by_one_whole_package_run_and_both_must_pass(self) -> None:
        self.write_script(runs=[
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "pass"}), "exit": 0},                       # 缩到用例
            {"events": package_events("p/a", {"TestFlaky": "pass", "TestOther": "pass"}), "exit": 0},  # 整包确认
        ])
        self.first_pass()
        retry = self.wrapper("retry")
        self.assertEqual(retry.returncode, 0, retry.stdout[-2500:])
        runs = self.go_test_runs()
        self.assertEqual(len(runs), 3, runs)
        self.assertIn("-run", runs[1])
        self.assertNotIn("-run", runs[2], "确认那一次是整包，不带 -run")
        self.assertEqual(runs[2][-1], "p/a", "确认的只是被缩过的那个包")
        retried = self.sidecar("retry")
        self.assertEqual([(item["package"], item["scope"], item["narrow_ok"], item["confirm_ok"], item["ok"]) for item in retried["retried"]],
                         [("p/a", "tests+package", True, True, True)])
        self.assertIn("缩到用例＋整包确认", retried["retry_note"])
        self.assertTrue(retried["ok"])

    def test_the_whole_package_confirmation_failing_keeps_the_retry_red(self) -> None:
        self.write_script(runs=[
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "pass"}), "exit": 0},
            {"events": package_events("p/a", {"TestFlaky": "pass", "TestOther": "fail"}), "exit": 1},   # 缩过的用例能过，同包别的用例红
        ])
        self.first_pass()
        retry = self.wrapper("retry")
        self.assertEqual(retry.returncode, 1, retry.stdout[-2500:])
        item = self.sidecar("retry")["retried"][0]
        self.assertEqual((item["narrow_ok"], item["confirm_ok"], item["ok"]), (True, False, False))
        self.assertFalse(self.sidecar("retry")["ok"])

    def test_a_failed_narrowed_run_needs_no_confirmation(self) -> None:
        self.write_script(runs=[
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "fail"}), "exit": 1},
        ])
        self.first_pass()
        retry = self.wrapper("retry")
        self.assertEqual(retry.returncode, 1)
        self.assertEqual(len(self.go_test_runs()), 2, "缩到用例就红了，不再多跑整包确认")
        self.assertIn("缩到用例未通过", self.sidecar("retry")["retry_note"])

    def test_a_whole_package_retry_is_not_confirmed_twice(self) -> None:
        # 不缩到用例（关闭缩到用例）的补跑本来就是整包，不需要再确认一次。
        self.write_script(runs=[
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "pass", "TestOther": "pass"}), "exit": 0},
        ])
        self.first_pass()
        retry = self.wrapper("retry", GO_TEST_PACKAGES_NARROW="off")
        self.assertEqual(retry.returncode, 0, retry.stdout[-1500:])
        self.assertEqual(len(self.go_test_runs()), 2)
        self.assertEqual(self.sidecar("retry")["retried"][0]["scope"], "package")


class EnvironmentFailureExitCodeTests(FakeGoBase):
    def test_go_missing_from_path_exits_127(self) -> None:
        self.write_script()
        completed = subprocess.run([sys.executable, "-B", str(WRAPPER), "--pattern", "./..."], capture_output=True, text=True, timeout=60,
                                   env={"PATH": str(self.root / "no-such-bin"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 127, completed.stdout)
        self.assertIn("找不到 go", completed.stdout)

    def test_go_list_failure_exits_125_not_a_test_failure(self) -> None:
        self.write_script(list_fail=True)
        completed = self.wrapper("formal")
        self.assertEqual(completed.returncode, 125, completed.stdout)

    def test_a_go_process_killed_by_a_signal_exits_128_plus_the_signal(self) -> None:
        for number, expected in ((signal.SIGTERM, 143), (signal.SIGKILL, 137)):
            with self.subTest(signal=number.name):
                self.write_script(runs=[{"events": [ev("run", "p/a", "TestFlaky")], "signal": int(number)}])
                completed = self.wrapper("formal")
                self.assertEqual(completed.returncode, expected, completed.stdout[-800:])
                self.assertIn("环境失败", completed.stdout)
                self.assertFalse(self.sidecar("formal")["expected_ok"], "旁路文件不留“首轮正常”的假象")

    def test_a_count_argument_that_contradicts_the_cache_contract_is_refused_before_running_go(self) -> None:
        """验收修正必修 4：附加参数排在包装器固定的 -count=1 之后，Go 以最后一次为准——矛盾的 -count 在跑 go 之前就拒绝（环境类退出码 125）。"""

        self.write_script(runs=[{"events": FIRST_PASS_FLAKY, "exit": 1}])
        for clash in (["-count=0"], ["--count=2"], ["-count", "3"]):
            with self.subTest(clash=clash):
                completed = subprocess.run([sys.executable, "-B", str(WRAPPER), "--pattern", "./...", "--", *clash], capture_output=True, text=True,
                                           timeout=60, env=self.environment())
                self.assertEqual(completed.returncode, 125, completed.stdout)
                self.assertIn("矛盾", completed.stdout)
                self.assertEqual(self.calls(), [], "一条 go 命令都没起")
        ok = subprocess.run([sys.executable, "-B", str(WRAPPER), "--pattern", "./...", "--", "-count=1"], capture_output=True, text=True,
                            timeout=60, env=self.environment())
        self.assertEqual(ok.returncode, 1, ok.stdout)   # -count=1 不矛盾：照常运行（假 go 里首轮有失败包）

    def test_a_real_test_failure_still_exits_1(self) -> None:
        self.write_script(runs=[{"events": FIRST_PASS_FLAKY, "exit": 1}])
        self.assertEqual(self.wrapper("formal").returncode, 1)

    def test_every_wrapper_environment_code_is_classified_as_an_environment_failure(self) -> None:
        for code in (124, 125, 127, 137, 143):
            with self.subTest(code=code):
                self.assertEqual(rp.classify_failure(is_command=True, exit_code=code, signal=None, timed_out=False, has_result=False,
                                                     result_successful=False, audit_failed=False), rp.CLASS_ENVIRONMENT)
        self.assertEqual(rp.classify_failure(is_command=True, exit_code=1, signal=None, timed_out=False, has_result=False,
                                             result_successful=False, audit_failed=False), rp.CLASS_EXECUTION)


class ExecutorEndToEndWithFakeGoTests(RetryRunBase):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "go").write_text(FAKE_GO % {"python": sys.executable}, encoding="utf-8")
        (self.bin / "go").chmod(0o755)
        self.package_dir = self.root / "pkg"
        self.package_dir.mkdir()
        (self.package_dir / "x_test.go").write_text("package x\n", encoding="utf-8")
        self.script = self.root / "fake-go.json"

    def go_unit(self) -> dict:
        return self.command_unit("backend:fake-go", [str(WRAPPER), "--tags", "unit", "--pattern", "./..."])

    def run_with_fake_go(self, runs: list[dict]) -> tuple[subprocess.CompletedProcess, dict]:
        self.script.write_text(json.dumps({"packages": ["p/a", "p/b"], "dir": str(self.package_dir),
                                           "tests": {"p/a": ["TestFlaky", "TestOther"], "p/b": ["TestB"]}, "runs": runs}), encoding="utf-8")
        completed, summary, _manifest = self.run_gates(
            [self.go_unit()], env_extra={"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "FAKE_GO_SCRIPT": str(self.script)})
        return completed, summary

    def test_a_signal_killed_go_is_an_environment_failure_and_is_not_retried(self) -> None:
        completed, summary = self.run_with_fake_go([{"events": [ev("run", "p/a", "TestFlaky")], "signal": int(signal.SIGTERM)}])
        self.assertEqual(completed.returncode, 1)
        marker = summary["units"][0]["retry"]
        self.assertEqual((marker["status"], marker["failure_class"]), ("not-retriable", "environment"))
        self.assertEqual(summary["units"][0]["exit_code"], 143)
        self.assertNotIn("retry", [event["kind"] for event in self.events() if event["event"] == "start"], "环境失败不补跑")

    def test_a_flaky_package_retry_is_marked_as_narrowed_plus_whole_package_confirmation(self) -> None:
        completed, summary = self.run_with_fake_go([
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "pass"}), "exit": 0},
            {"events": package_events("p/a", {"TestFlaky": "pass", "TestOther": "pass"}), "exit": 0},
        ])
        self.assertEqual(completed.returncode, 0, completed.stdout[-1500:] + completed.stderr[-1500:])
        marker = summary["units"][0]["retry"]
        self.assertEqual(marker["status"], "retry-passed")
        self.assertIn("缩到用例＋整包确认", marker["reason"])
        self.assertIn("缩到用例＋整包确认", marker["retry_attempt"]["note"])
        self.assertNotIn("note", marker["first_attempt"])
        self.assertEqual(rp.summary_problems(summary, require_files=True), [])

    def test_a_failing_whole_package_confirmation_makes_the_executor_retry_fail(self) -> None:
        completed, summary = self.run_with_fake_go([
            {"events": FIRST_PASS_FLAKY, "exit": 1},
            {"events": package_events("p/a", {"TestFlaky": "pass"}), "exit": 0},
            {"events": package_events("p/a", {"TestFlaky": "pass", "TestOther": "fail"}), "exit": 1},
        ])
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(summary["units"][0]["retry"]["status"], "retry-failed")
        self.assertEqual(summary["retry"]["retry_failed"], ["backend:fake-go"])


if __name__ == "__main__":
    unittest.main()
