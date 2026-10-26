"""后端 go test 按包运行与按包补跑（优化第 3 项，``tools/ci/go_test_packages.py``）。

三层：

* 纯逻辑：``go test -json`` 事件归纳、期望包集合核对（没跑到的包按失败处理）、缩到用例的判据（TestMain／init／被崩溃截断／失败太多一律整包）；
* 真实 go：在临时模块里造偶发用例，直接跑包装器——首轮红、补跑按包（偶发用例缩到 ``-run``，带 TestMain 的包整包）通过；缩到用例与整包补跑的结论一致；
* 执行器端到端：包装器作为命令单元交给 ``run-gates``，偶发包只补跑它自己，补跑通过并单列。

真实 go 的用例在没有 go 的机器上跳过（采集工具测试的 CI 作业不保证装了 Go）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.ci import entry_gates as eg
from tools.ci import go_test_packages as gtp
from tools.official_client_capture.tests.test_ci_unit_executor_retry import RetryRunBase

REPO_ROOT = Path(__file__).resolve().parents[3]
WRAPPER = REPO_ROOT / "tools" / "ci" / "go_test_packages.py"
HAVE_GO = shutil.which("go") is not None


def event(action: str, package: str, test: str | None = None, **extra: object) -> str:
    return json.dumps({"Action": action, "Package": package, **({"Test": test} if test else {}), **extra})


class ParseEventsTests(unittest.TestCase):
    def test_package_results_and_failed_top_level_tests(self) -> None:
        lines = [event("run", "p/a", "TestOne"), event("pass", "p/a", "TestOne"), event("pass", "p/a"),
                 event("run", "p/b", "TestBad"), event("run", "p/b", "TestBad/sub"), event("fail", "p/b", "TestBad/sub"),
                 event("fail", "p/b", "TestBad"), event("fail", "p/b"), event("skip", "p/c"), "非 JSON 的编译器输出\n"]
        parsed = gtp.parse_events(lines)
        self.assertEqual({name: item["action"] for name, item in parsed["packages"].items()}, {"p/a": "pass", "p/b": "fail", "p/c": "skip"})
        self.assertEqual(parsed["packages"]["p/b"]["failed_tests"], ["TestBad"])
        self.assertEqual(parsed["packages"]["p/b"]["tests"], {"TestBad": "fail"}, "子测试不单独算顶层用例")
        self.assertEqual(parsed["other_lines"], 1)

    def test_a_package_without_terminal_event_is_missing_and_never_counts_as_passed(self) -> None:
        parsed = gtp.parse_events([event("run", "p/a", "TestOne"), event("pass", "p/a", "TestOne"), event("pass", "p/a"),
                                   event("run", "p/crash", "TestX")])   # p/crash 崩溃：没有终态；p/never 根本没出现
        verdict = gtp.judge(["p/a", "p/crash", "p/never"], parsed, go_exit_code=0)
        self.assertEqual((verdict["passed_packages"], verdict["missing_packages"], verdict["ok"]), (["p/a"], ["p/crash", "p/never"], False))

    def test_go_exit_code_must_agree_with_the_event_stream(self) -> None:
        parsed = gtp.parse_events([event("pass", "p/a")])
        self.assertTrue(gtp.judge(["p/a"], parsed, go_exit_code=0)["ok"])
        verdict = gtp.judge(["p/a"], parsed, go_exit_code=1)
        self.assertEqual((verdict["inconsistent"], verdict["ok"]), (True, False), "退出码非 0 却没发现失败：拿不准，不判通过")

    def test_build_failures_are_package_failures(self) -> None:
        parsed = gtp.parse_events([json.dumps({"ImportPath": "p/a [p/a.test]", "Action": "build-fail"}), event("fail", "p/a")])
        self.assertEqual(parsed["build_failed"], ["p/a"])
        self.assertEqual(gtp.judge(["p/a"], parsed, go_exit_code=1)["failed_packages"], ["p/a"])

    def test_render_line_echoes_go_test_text(self) -> None:
        self.assertEqual(gtp.render_line(event("output", "p/a", Output="ok  \tp/a\t0.1s\n")), "ok  \tp/a\t0.1s\n")
        self.assertEqual(gtp.render_line(event("pass", "p/a")), "")
        self.assertEqual(gtp.render_line("# p/a\n./a.go:1:1: 编译错误\n"), "# p/a\n./a.go:1:1: 编译错误\n")


class NarrowingDecisionTests(unittest.TestCase):
    def record(self, **overrides: object) -> dict:
        return {"action": "fail", "tests": {"TestA": "pass", "TestBad": "fail"}, "started": ["TestA", "TestBad"], "failed_tests": ["TestBad"],
                "unsafe_output": False, **overrides}

    def inspector(self, **overrides: object):
        files = overrides.pop("files", [])
        return lambda _package: {"files": files, "tests": ["TestA", "TestBad"], **overrides}

    def test_independent_failures_narrow_to_the_failed_tests(self) -> None:
        decision = gtp.decide_scope("p/a", self.record(), narrow_enabled=True, inspector=self.inspector())
        self.assertEqual((decision["scope"], decision["tests"]), ("tests", ["TestBad"]))

    def test_everything_uncertain_falls_back_to_the_whole_package(self) -> None:
        cases = {
            "关闭缩到用例": dict(narrow_enabled=False),
            "首轮没有包级失败终态": dict(record=self.record(action=None)),
            "没识别到失败用例": dict(record=self.record(failed_tests=[])),
            "失败用例太多": dict(record=self.record(failed_tests=[f"Test{n}" for n in range(gtp.MAX_NARROW_TESTS + 1)])),
            "失败的不是顶层用例": dict(record=self.record(failed_tests=["Example_x"])),
            "首轮输出有 panic": dict(record=self.record(unsafe_output=True)),
            "有顶层用例没终态": dict(record=self.record(tests={"TestBad": "fail"})),
            "取不到测试清单": dict(inspector=lambda _p: (_ for _ in ()).throw(gtp.GoRunError("go 不在"))),
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                arguments = {"record": self.record(), "narrow_enabled": True, "inspector": self.inspector(), **overrides}
                decision = gtp.decide_scope("p/a", arguments["record"], narrow_enabled=arguments["narrow_enabled"], inspector=arguments["inspector"])
                self.assertEqual(decision["scope"], "package", decision)

    def test_test_main_or_init_in_the_package_blocks_narrowing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main_file, init_file, plain = (Path(directory) / name for name in ("m_test.go", "i_test.go", "p_test.go"))
            main_file.write_text("package x\nfunc TestMain(m *testing.M) {}\n", encoding="utf-8")
            init_file.write_text("package x\nfunc init() {}\n", encoding="utf-8")
            plain.write_text("package x\nfunc TestA(t *testing.T) {}\n", encoding="utf-8")
            self.assertIn("TestMain", gtp.source_blocks_narrowing([main_file]))
            self.assertIn("init", gtp.source_blocks_narrowing([init_file]))
            self.assertIsNone(gtp.source_blocks_narrowing([plain]))
            self.assertIn("读不出", gtp.source_blocks_narrowing([Path(directory) / "不存在_test.go"]))

    def test_run_pattern_is_anchored_and_escaped(self) -> None:
        self.assertEqual(gtp.run_pattern(["TestA", "TestB_x"]), "^(TestA|TestB_x)$")


class FirstSidecarTests(unittest.TestCase):
    def valid(self) -> dict:
        return {"schema_version": gtp.SCHEMA, "mode": "first", "tags": "unit", "pattern": "./...", "expected_ok": True,
                "expected_packages": ["p/a"], "packages": {"p/a": {}}, "failed_packages": ["p/a"], "missing_packages": []}

    def test_only_a_matching_first_sidecar_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "first.json"
            path.write_text(json.dumps(self.valid()), encoding="utf-8")
            self.assertIsNotNone(gtp.load_first_sidecar(str(path), tags="unit", pattern="./..."))
            self.assertIsNone(gtp.load_first_sidecar(str(path), tags="integration", pattern="./..."), "标签不同不是同一条命令")
            self.assertIsNone(gtp.load_first_sidecar(str(Path(directory) / "没有.json"), tags="unit", pattern="./..."))
            self.assertIsNone(gtp.load_first_sidecar(None, tags="unit", pattern="./..."))
            for label, change in {"模式": {"mode": "retry"}, "期望包为空": {"expected_packages": []}, "期望清单不可信": {"expected_ok": False}}.items():
                path.write_text(json.dumps({**self.valid(), **change}), encoding="utf-8")
                self.assertIsNone(gtp.load_first_sidecar(str(path), tags="unit", pattern="./..."), label)
            path.write_text("不是 JSON", encoding="utf-8")
            self.assertIsNone(gtp.load_first_sidecar(str(path), tags="unit", pattern="./..."))


class WrapperCommandTests(unittest.TestCase):
    def test_gate_commands_map_to_the_wrapper_and_keep_the_cache_contract(self) -> None:
        for gate_id, tags in (("backend-go-test", ""), ("backend-unit", "unit"), ("backend-integration", "integration")):
            argv, _cwd = eg.GATE_COMMANDS[gate_id]
            wrapped = eg.go_package_argv(argv)
            self.assertEqual(wrapped[:3], ["python3", "-B", str(eg.HERE / eg.GO_PACKAGE_WRAPPER)])
            self.assertEqual(wrapped[3:], (["--tags", tags] if tags else []) + ["--pattern", "./..."])
        with self.assertRaises(ValueError):
            eg.go_package_argv(["go", "test", "./..."])   # 缺 -count=1：入口门禁缓存合同不许

    def test_literal_gate_commands_are_unchanged_for_the_closeout_check(self) -> None:
        self.assertEqual(eg.GATE_COMMANDS["backend-unit"][0], ["go", "test", "-tags=unit", "./...", "-count=1"])

    def test_driver_carries_an_identical_copy(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "go_test_packages.py"
        self.assertEqual(driver.read_bytes(), WRAPPER.read_bytes())


GO_MOD = "module example.com/demo\n\ngo 1.18\n"
# 偶发用例：环境变量 FLAKY_DIR 下的标记文件不存在时失败并留下标记，之后都通过。
FLAKY_TEST = textwrap.dedent("""\
    package {name}

    import (
    \t"os"
    \t"path/filepath"
    \t"testing"
    )

    func TestStable(t *testing.T) {{}}

    func TestFlaky(t *testing.T) {{
    \tmarker := filepath.Join(os.Getenv("FLAKY_DIR"), "{name}")
    \tif _, err := os.Stat(marker); err != nil {{
    \t\t_ = os.WriteFile(marker, []byte("x"), 0o600)
    \t\tt.Fatal("首次偶发失败")
    \t}}
    }}
""")
MAIN_TEST = FLAKY_TEST + textwrap.dedent("""\

    func TestMain(m *testing.M) {{ os.Exit(m.Run()) }}
""")
PANIC_TEST = textwrap.dedent("""\
    package panicky

    import (
    \t"os"
    \t"path/filepath"
    \t"testing"
    )

    func TestFirst(t *testing.T) {}

    func TestPanicOnce(t *testing.T) {
    \tmarker := filepath.Join(os.Getenv("FLAKY_DIR"), "panicky")
    \tif _, err := os.Stat(marker); err != nil {
    \t\t_ = os.WriteFile(marker, []byte("x"), 0o600)
    \t\tpanic("首次崩溃")
    \t}
    }

    func TestAfterPanic(t *testing.T) {}
""")
ALWAYS_FAIL_TEST = textwrap.dedent("""\
    package broken

    import "testing"

    func TestBroken(t *testing.T) { t.Fatal("一直失败") }
""")


@unittest.skipUnless(HAVE_GO, "本机没有 go")
class DefaultTimeoutTests(unittest.TestCase):
    """go test 缺省 10 分钟会把 ARM64 满载下的 internal/service 包杀掉，且超时被杀不补跑；包装器默认补 30 分钟。"""

    def test_default_is_thirty_minutes_when_not_given(self) -> None:
        self.assertEqual(gtp.with_default_timeout(["-race"], None), ["-race", "-timeout=30m"])
        self.assertEqual(gtp.with_default_timeout([], ""), ["-timeout=30m"])

    def test_an_explicit_timeout_in_any_spelling_wins(self) -> None:
        for words in (["-timeout=5m"], ["-timeout", "5m"], ["--timeout=5m"], ["-test.timeout=5m"]):
            self.assertEqual(gtp.with_default_timeout(words, None), words)

    def test_environment_value_overrides_the_default_and_off_disables_it(self) -> None:
        self.assertEqual(gtp.with_default_timeout([], "45m"), ["-timeout=45m"])
        self.assertEqual(gtp.with_default_timeout(["-race"], "off"), ["-race"])


class RealGoTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.module = self.root / "mod"
        self.flags = self.root / "flags"
        self.flags.mkdir()
        self.write_module({"flaky": FLAKY_TEST.format(name="flaky"), "mainpkg": MAIN_TEST.format(name="mainpkg"), "panicky": PANIC_TEST,
                           "steady": "package steady\n\nimport \"testing\"\n\nfunc TestSteady(t *testing.T) {}\n"})
        (self.module / "notest").mkdir()
        (self.module / "notest" / "n.go").write_text("package notest\n", encoding="utf-8")

    def write_module(self, tests: dict[str, str]) -> None:
        self.module.mkdir(exist_ok=True)
        (self.module / "go.mod").write_text(GO_MOD, encoding="utf-8")
        for name, text in tests.items():
            (self.module / name).mkdir(exist_ok=True)
            (self.module / name / f"{name}.go").write_text(f"package {name}\n", encoding="utf-8")
            (self.module / name / f"{name}_test.go").write_text(text, encoding="utf-8")

    def reset_flags(self) -> None:
        for item in self.flags.iterdir():
            item.unlink()

    def wrapper(self, kind: str, *, tags: str = "", sidecar: str, first: str, narrow: bool = True) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": "",
                       "FLAKY_DIR": str(self.flags), "UNIT_EXECUTOR_KIND": kind, "UNIT_EXECUTOR_SIDECAR": sidecar,
                       "UNIT_EXECUTOR_FIRST_SIDECAR": first, "GO_TEST_PACKAGES_NARROW": "on" if narrow else "off"}
        return subprocess.run([sys.executable, "-B", str(WRAPPER), *(["--tags", tags] if tags else []), "--pattern", "./..."], cwd=self.module,
                              capture_output=True, text=True, timeout=300, env=environment)

    def first_and_retry(self, *, narrow: bool = True) -> tuple[subprocess.CompletedProcess, subprocess.CompletedProcess, dict, dict]:
        self.reset_flags()
        first_path, retry_path = str(self.root / "first.json"), str(self.root / "retry.json")
        first = self.wrapper("formal", sidecar=first_path, first=first_path)
        first_doc = json.loads(Path(first_path).read_text(encoding="utf-8"))
        retry = self.wrapper("retry", sidecar=retry_path, first=first_path, narrow=narrow)
        return first, retry, first_doc, json.loads(Path(retry_path).read_text(encoding="utf-8"))

    def test_first_run_fails_with_a_structured_package_result(self) -> None:
        first, _retry, doc, _ = self.first_and_retry()
        self.assertEqual(first.returncode, 1, first.stdout[-1500:])
        self.assertEqual(doc["failed_packages"], ["example.com/demo/flaky", "example.com/demo/mainpkg", "example.com/demo/panicky"])
        self.assertEqual(doc["missing_packages"], [])
        self.assertEqual(sorted(doc["expected_packages"]), sorted(
            f"example.com/demo/{name}" for name in ("flaky", "mainpkg", "notest", "panicky", "steady")), "含没有测试文件的包")
        self.assertEqual(doc["packages"]["example.com/demo/notest"]["action"], "skip")
        self.assertIn("首轮结论", first.stdout)
        self.assertIn("首次偶发失败", first.stdout, "日志是可读的 go test 文本输出")

    def test_retry_reruns_only_the_failed_packages_and_passes(self) -> None:
        _first, retry, doc, retried = self.first_and_retry()
        self.assertEqual(retry.returncode, 0, retry.stdout[-2500:])
        scopes = {item["package"].rsplit("/", 1)[1]: item["scope"] for item in retried["retried"]}
        self.assertEqual(scopes, {"flaky": "tests+package", "mainpkg": "package", "panicky": "package"},
                         "偶发用例缩到 -run 并做整包确认；带 TestMain 的包、被崩溃截断的包整包重跑")
        flaky = next(item for item in retried["retried"] if item["package"].endswith("/flaky"))
        self.assertEqual(flaky["tests"], ["TestFlaky"])
        self.assertTrue(flaky["narrow_ok"] and flaky["confirm_ok"] and flaky["ok"], "缩到用例通过后整包确认也通过，才算这个包补跑通过")
        self.assertIn("整包确认", retry.stdout)
        self.assertIn("缩到用例＋整包确认", retried["retry_note"])
        self.assertTrue(retried["ok"])
        self.assertNotIn("example.com/demo/steady", retry.stdout, "首轮通过的包不重跑")
        self.assertNotIn("example.com/demo/notest", retry.stdout)

    def test_narrowed_retry_agrees_with_a_whole_package_retry(self) -> None:
        _first, narrow, _doc, narrow_doc = self.first_and_retry(narrow=True)
        _first, whole, _doc, whole_doc = self.first_and_retry(narrow=False)
        self.assertEqual((narrow.returncode, whole.returncode), (0, 0))
        self.assertEqual({item["scope"] for item in whole_doc["retried"]}, {"package"})
        self.assertTrue(narrow_doc["ok"] and whole_doc["ok"], "缩到用例与整包补跑结论一致")

    def test_a_package_that_keeps_failing_keeps_the_retry_red(self) -> None:
        self.write_module({"broken": ALWAYS_FAIL_TEST})
        _first, retry, doc, retried = self.first_and_retry()
        self.assertIn("example.com/demo/broken", doc["failed_packages"])
        self.assertEqual(retry.returncode, 1)
        self.assertFalse(retried["ok"])
        broken = next(item for item in retried["retried"] if item["package"].endswith("/broken"))
        self.assertFalse(broken["ok"])

    def test_missing_first_sidecar_falls_back_to_a_full_rerun(self) -> None:
        self.reset_flags()
        retry_path = str(self.root / "retry.json")
        # 先让标记齐全（第一次跑把偶发消耗掉），再在没有首轮旁路文件的情况下补跑：整套重跑并通过。
        self.wrapper("formal", sidecar=str(self.root / "ignored.json"), first="")
        retry = self.wrapper("retry", sidecar=retry_path, first=str(self.root / "没有首轮.json"))
        self.assertEqual(retry.returncode, 0, retry.stdout[-1500:])
        self.assertIn("退回整套重跑", retry.stdout)
        self.assertEqual(json.loads(Path(retry_path).read_text(encoding="utf-8"))["scope"], "full")

    def test_narrowing_a_name_that_matches_nothing_does_not_pass_silently(self) -> None:
        # 首轮旁路文件声称 TestGone 失败，但包里没有这个用例：-run 不匹配任何用例时 go test 悄悄通过，这里必须判失败。
        self.reset_flags()
        first_path = str(self.root / "first.json")
        self.wrapper("formal", sidecar=first_path, first=first_path)
        doc = json.loads(Path(first_path).read_text(encoding="utf-8"))
        flaky = "example.com/demo/flaky"
        doc["packages"][flaky]["failed_tests"] = ["TestGone"]
        doc["packages"][flaky]["tests"]["TestGone"] = "fail"
        Path(first_path).write_text(json.dumps(doc), encoding="utf-8")
        retry_path = str(self.root / "retry.json")
        retry = self.wrapper("retry", sidecar=retry_path, first=first_path)
        retried = json.loads(Path(retry_path).read_text(encoding="utf-8"))
        item = next(entry for entry in retried["retried"] if entry["package"] == flaky)
        self.assertEqual((item["scope"], item["ok"], item["named_tests_not_passed"]), ("tests", False, ["TestGone"]))
        self.assertEqual(retry.returncode, 1)


@unittest.skipUnless(HAVE_GO, "本机没有 go")
class ExecutorEndToEndTests(RetryRunBase):
    def test_a_flaky_package_is_retried_by_package_inside_the_executor(self) -> None:
        module = self.root / "mod"
        flags = self.root / "flags"
        flags.mkdir()
        (module / "flaky").mkdir(parents=True)
        (module / "go.mod").write_text(GO_MOD, encoding="utf-8")
        (module / "flaky" / "flaky.go").write_text("package flaky\n", encoding="utf-8")
        (module / "flaky" / "flaky_test.go").write_text(FLAKY_TEST.format(name="flaky"), encoding="utf-8")
        (module / "steady").mkdir()
        (module / "steady" / "steady.go").write_text("package steady\n", encoding="utf-8")
        (module / "steady" / "steady_test.go").write_text("package steady\n\nimport \"testing\"\n\nfunc TestSteady(t *testing.T) {}\n", encoding="utf-8")
        unit = {"unit_id": "backend:unit", "argv": [sys.executable, "-B", str(WRAPPER), "--pattern", "./..."], "cwd": str(module),
                "reservation_restartable": True,
                "env": {"FLAKY_DIR": str(flags), "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOFLAGS": ""}}
        completed, summary, manifest = self.run_gates([unit])
        self.assertEqual(completed.returncode, 0, completed.stderr[-2500:])
        row = summary["units"][0]
        self.assertEqual((row["passed"], row["retry"]["status"]), (True, "retry-passed"))
        first_log = Path(row["retry"]["first_attempt"]["log"]).read_text(encoding="utf-8")
        retry_log = Path(row["retry"]["retry_attempt"]["log"]).read_text(encoding="utf-8")
        self.assertIn("首轮结论", first_log)
        self.assertIn("补跑结论：通过", retry_log)
        self.assertIn("缩到用例补跑", retry_log)
        self.assertIn("整包确认", retry_log)
        self.assertIn("缩到用例＋整包确认", row["retry"]["retry_attempt"]["note"], "标注里写清是缩到用例＋整包确认")
        self.assertNotIn("example.com/demo/steady", retry_log, "首轮通过的包不重跑")
        record_path = manifest["units"][0]["retry"]["retry_attempt"]["record_path"]
        record = json.loads(Path(record_path).read_text(encoding="utf-8"))
        self.assertIn("sidecar", record, "记录带出旁路结果的路径与摘要")
        self.assertEqual(summary["unit_manifest"]["self_check"], "passed", summary["unit_manifest"]["problems"])


if __name__ == "__main__":
    unittest.main()
