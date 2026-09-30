"""上游合并门禁并行编排的单元测试：只用合成步骤，不执行真实门禁。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.upstream_merge import gate_runner
from tools.upstream_merge.gate_runner import Lane, Step, build_lanes, run_lanes


def python_step(name: str, code: str) -> Step:
    return Step(name=name, argv=(sys.executable, "-c", code))


class BuildLanesTest(unittest.TestCase):
    """检查线必须覆盖旧 full-regression 命令的全部检查，且不丢任何一组标签。"""

    def test_full_mode_covers_old_full_regression(self) -> None:
        lanes = build_lanes("full", Path("/src/codex"))
        self.assertEqual([lane.name for lane in lanes], ["go-tests", "lint", "frontend", "capture-tools", "egress-spec"])
        by_name = {lane.name: lane for lane in lanes}
        go_steps = by_name["go-tests"].steps
        self.assertEqual(
            [step.argv for step in go_steps],
            [
                ("go", "test", "-count=1", "./..."),
                ("go", "test", "-tags=unit", "-count=1", "./..."),
                ("go", "test", "-tags=integration", "-count=1", "./..."),
            ],
        )
        self.assertTrue(all(step.cwd == "backend" for step in go_steps))
        self.assertEqual(
            [step.argv for step in by_name["lint"].steps],
            [
                ("golangci-lint", "run", "./..."),
                ("golangci-lint", "run", "--build-tags=unit", "./..."),
                ("golangci-lint", "run", "--build-tags=integration", "./..."),
            ],
        )
        self.assertEqual(
            [step.argv for step in by_name["frontend"].steps],
            [
                ("pnpm", "--dir", "frontend", "run", "lint:check"),
                ("pnpm", "--dir", "frontend", "run", "typecheck"),
                ("make", "test-frontend-critical"),
            ],
        )
        self.assertEqual(by_name["capture-tools"].steps[0].argv, ("make", "test-capture-tools-parallel"))
        self.assertEqual(
            by_name["egress-spec"].steps[0].argv,
            ("make", "-k", "CODEX_0_149_1_SOURCE_ROOT=/src/codex", "check-egress-spec"),
        )

    def test_backend_mode_runs_only_go_tests_and_lint(self) -> None:
        self.assertEqual([lane.name for lane in build_lanes("backend", Path("/src"))], ["go-tests", "lint"])

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_lanes("partial", Path("/src"))


class RunLanesTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stream = io.BytesIO()

    def run_lanes(self, lanes, *, jobs: int, env: dict[str, str] | None = None):
        return run_lanes(lanes, jobs=jobs, repository_root=self.root, env=env or dict(os.environ), stream=self.stream)

    def test_failed_step_does_not_stop_following_steps(self) -> None:
        marker = self.root / "second-step-ran"
        lane = Lane(
            "a",
            (
                python_step("fails", "import sys; sys.exit(3)"),
                python_step("writes-marker", f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ok')"),
            ),
        )
        results, _elapsed = self.run_lanes([lane], jobs=1)
        self.assertEqual([(item.step, item.status, item.exit_code) for item in results], [("fails", "failed", 3), ("writes-marker", "passed", 0)])
        self.assertTrue(marker.is_file())
        self.assertIn("结论：失败 1 项：a/fails", self.stream.getvalue().decode("utf-8"))

    def test_lanes_run_in_parallel_within_jobs_limit(self) -> None:
        lanes = [Lane(name, (python_step("sleep", "import time; time.sleep(0.6)"),)) for name in ("a", "b")]
        started = time.monotonic()
        self.run_lanes(lanes, jobs=2)
        parallel = time.monotonic() - started
        self.stream = io.BytesIO()
        started = time.monotonic()
        self.run_lanes(lanes, jobs=1)
        serial = time.monotonic() - started
        self.assertLess(parallel, 1.1)
        self.assertGreaterEqual(serial, 1.2)

    def test_output_is_grouped_by_lane_in_declared_order(self) -> None:
        lanes = [
            Lane("first", (python_step("slow", "import time; time.sleep(0.3); print('from-first')"),)),
            Lane("second", (python_step("fast", "print('from-second')"),)),
        ]
        results, _elapsed = self.run_lanes(lanes, jobs=2)
        output = self.stream.getvalue().decode("utf-8")
        # 第二条线先结束，但输出仍按声明顺序整段排列，不会交错。
        self.assertLess(output.index("===== 检查线 first ====="), output.index("from-first"))
        self.assertLess(output.index("from-first"), output.index("===== 检查线 second ====="))
        self.assertLess(output.index("===== 检查线 second ====="), output.index("from-second"))
        self.assertIn("结论：全部通过", output)
        self.assertTrue(all(item.status == "passed" for item in results))

    def test_missing_executable_fails_step_and_continues(self) -> None:
        lane = Lane(
            "a",
            (
                Step("missing", ("definitely-not-an-installed-command-xyz",)),
                python_step("after", "print('after-missing')"),
            ),
        )
        results, _elapsed = self.run_lanes([lane], jobs=1)
        self.assertEqual([item.status for item in results], ["failed", "passed"])
        self.assertIsNone(results[0].exit_code)
        output = self.stream.getvalue().decode("utf-8")
        self.assertIn("无法启动", output)
        self.assertIn("after-missing", output)

    def test_children_get_bytecode_guard_and_private_shard_log_dir(self) -> None:
        probe = self.root / "env.txt"
        code = (
            "import os, pathlib; "
            f"pathlib.Path({str(probe)!r}).write_text("
            "os.environ.get('PYTHONDONTWRITEBYTECODE', '') + '|' + os.environ.get('CAPTURE_TEST_SHARD_LOG_DIR', ''))"
        )
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONDONTWRITEBYTECODE", "CAPTURE_TEST_SHARD_LOG_DIR"}}
        self.run_lanes([Lane("a", (python_step("probe", code),))], jobs=1, env=env)
        guard, shard_dir = probe.read_text().split("|")
        self.assertEqual(guard, "1")
        self.assertTrue(shard_dir.endswith("capture-test-shards"))

    def test_go_json_dir_redirects_go_test_stdout(self) -> None:
        # 用一个假的 go 命令记录收到的参数，确认 -json 插在包参数之前、stdout 写进事件流文件。
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_go = fake_bin / "go"
        fake_go.write_text('#!/bin/sh\necho "$@"\n', encoding="utf-8")
        fake_go.chmod(0o755)
        json_dir = self.root / "json"
        json_dir.mkdir()
        (self.root / "backend").mkdir()
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env[gate_runner.GO_JSON_DIR_ENV] = str(json_dir)
        lane = Lane("go-tests", (gate_runner._go_test_step("unit"),))
        results, _elapsed = self.run_lanes([lane], jobs=1, env=env)
        self.assertEqual(results[0].status, "passed")
        recorded = (json_dir / "go-test-unit.jsonl").read_text(encoding="utf-8").strip()
        self.assertEqual(recorded, "test -json -tags=unit -count=1 ./...")

    def test_attached_log_dir_is_copied_into_lane_log(self) -> None:
        shard_dir = self.root / "shards"
        shard_dir.mkdir()
        (shard_dir / "shard-1.log").write_text("ERROR: test_something\n", encoding="utf-8")
        (shard_dir / "shard-2.log").write_text("OK\n", encoding="utf-8")
        env = dict(os.environ)
        env["PROBE_LOG_DIR"] = str(shard_dir)
        step = Step("shards", (sys.executable, "-c", "pass"), attach_log_dir_env="PROBE_LOG_DIR")
        self.run_lanes([Lane("capture-tools", (step,))], jobs=1, env=env)
        output = self.stream.getvalue().decode("utf-8")
        self.assertLess(output.index("--- 附：shard-1.log ---"), output.index("ERROR: test_something"))
        self.assertLess(output.index("ERROR: test_something"), output.index("--- 附：shard-2.log ---"))

    def test_go_json_failures_are_extracted_into_lane_log(self) -> None:
        # 假 go 命令输出 test2json 事件：一个失败用例及其输出、一个通过用例。
        events = [
            {"Action": "output", "Package": "example/pkg", "Test": "TestBroken", "Output": "broken detail\n"},
            {"Action": "fail", "Package": "example/pkg", "Test": "TestBroken"},
            {"Action": "output", "Package": "example/pkg", "Test": "TestFine", "Output": "fine detail\n"},
            {"Action": "pass", "Package": "example/pkg", "Test": "TestFine"},
            {"Action": "fail", "Package": "example/pkg"},
        ]
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        payload = "\n".join(json.dumps(item) for item in events)
        fake_go = fake_bin / "go"
        fake_go.write_text("#!/bin/sh\ncat <<'EVENTS'\n" + payload + "\nEVENTS\nexit 1\n", encoding="utf-8")
        fake_go.chmod(0o755)
        json_dir = self.root / "json"
        json_dir.mkdir()
        (self.root / "backend").mkdir()
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env[gate_runner.GO_JSON_DIR_ENV] = str(json_dir)
        results, _elapsed = self.run_lanes([Lane("go-tests", (gate_runner._go_test_step(""),))], jobs=1, env=env)
        self.assertEqual(results[0].status, "failed")
        output = self.stream.getvalue().decode("utf-8")
        self.assertIn("--- go test 失败：example/pkg TestBroken ---", output)
        self.assertIn("broken detail", output)
        self.assertNotIn("fine detail", output)


class DefaultJobsTest(unittest.TestCase):
    def test_env_overrides_default_and_rejects_non_positive(self) -> None:
        original = os.environ.get("UPSTREAM_GATE_JOBS")
        self.addCleanup(lambda: os.environ.__setitem__("UPSTREAM_GATE_JOBS", original) if original is not None else os.environ.pop("UPSTREAM_GATE_JOBS", None))
        os.environ["UPSTREAM_GATE_JOBS"] = "3"
        self.assertEqual(gate_runner.default_jobs(), 3)
        os.environ["UPSTREAM_GATE_JOBS"] = "0"
        with self.assertRaises(ValueError):
            gate_runner.default_jobs()
        os.environ.pop("UPSTREAM_GATE_JOBS")
        # 未设置时检查线依次执行：满载并行会让计时敏感用例误判。
        self.assertEqual(gate_runner.default_jobs(), 1)


if __name__ == "__main__":
    unittest.main()
