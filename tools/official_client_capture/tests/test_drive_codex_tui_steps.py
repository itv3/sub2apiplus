"""drive_codex_tui 的有序步骤（--step）：解析、按键编码、执行顺序与命令行校验。

不起真终端：执行顺序用替身 TUI 记录调用；命令行错误用子进程跑真实脚本，确认在启动 TUI 之前就退出。
"""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.official_client_capture import drive_codex_tui as drive

SCRIPT = Path(drive.__file__).resolve()


class FakeTui:
    """记录 send／send_keys／pump 调用的替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def send(self, text: str, enter: bool = True) -> None:
        self.calls.append(("send", text, enter))

    def send_keys(self, raw: bytes) -> None:
        self.calls.append(("keys", raw))

    def pump(self, seconds: float) -> str:
        self.calls.append(("pump", seconds))
        return ""


class ParseStepTests(unittest.TestCase):
    def test_prompt_keeps_text_and_optional_hold(self) -> None:
        self.assertEqual(drive.parse_step("prompt:请只回复 OK"), drive.Step("prompt", "请只回复 OK", None))
        self.assertEqual(drive.parse_step("prompt@60:a@b:c"), drive.Step("prompt", "a@b:c", 60.0))

    def test_effort_levels_are_validated(self) -> None:
        for level in ("low", "medium", "high", "xhigh", "max", "ultra"):
            self.assertEqual(drive.parse_step(f"effort:{level}").arg, level)
        for bad in ("effort:", "effort:HIGH", "effort:3", "effort:high --speed=fast"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drive.parse_step(bad)

    def test_model_step_with_and_without_level(self) -> None:
        self.assertEqual(drive.parse_step("model:gpt-6-astra").arg, "gpt-6-astra")
        self.assertEqual(drive.parse_step("model@12:gpt-6.1-sol,high"), drive.Step("model", "gpt-6.1-sol,high", 12.0))
        for bad in ("model:", "model:bad model", "model:gpt-6;rm", "model:gpt-6,fast", "model:-x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drive.parse_step(bad)

    def test_keys_and_compact_take_no_argument(self) -> None:
        for good in ("effort-up", "effort-down@3", "compact", "compact@90"):
            drive.parse_step(good)
        for bad in ("effort-up:1", "compact:now"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drive.parse_step(bad)

    def test_slash_must_start_with_slash(self) -> None:
        self.assertEqual(drive.parse_step("slash:/status").arg, "/status")
        with self.assertRaises(ValueError):
            drive.parse_step("slash:status")

    def test_wait_accepts_either_form_but_needs_seconds(self) -> None:
        self.assertEqual(drive.parse_step("wait@30"), drive.Step("wait", None, 30.0))
        self.assertEqual(drive.parse_step("wait:2.5"), drive.Step("wait", None, 2.5))
        for bad in ("wait", "wait:abc", "wait@3:4"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drive.parse_step(bad)

    def test_unknown_and_malformed_steps_rejected(self) -> None:
        for bad in ("", "reboot", "Prompt:x", "prompt@x:y", "prompt"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                drive.parse_step(bad)


class EncodingTests(unittest.TestCase):
    def test_typed_text_matches_tui_inline_syntax(self) -> None:
        cases = {
            "prompt:hello": "hello",
            "effort:high": "/model --reasoning=high",
            "model:gpt-6-astra": "/model gpt-6-astra",
            "model:gpt-6-astra,low": "/model gpt-6-astra --reasoning=low",
            "compact": "/compact",
            "slash:/status": "/status",
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(drive.step_typed_text(drive.parse_step(spec)), expected)
        for spec in ("effort-up", "effort-down", "wait@5"):
            self.assertIsNone(drive.step_typed_text(drive.parse_step(spec)))

    def test_alt_chords_are_esc_prefixed(self) -> None:
        self.assertEqual(drive.step_raw_keys(drive.parse_step("effort-up")), b"\x1b.")
        self.assertEqual(drive.step_raw_keys(drive.parse_step("effort-down")), b"\x1b,")
        self.assertIsNone(drive.step_raw_keys(drive.parse_step("effort:low")))

    def test_default_hold_by_category(self) -> None:
        hold = lambda spec: drive.step_hold_seconds(drive.parse_step(spec), 120, 8, 75)  # noqa: E731
        self.assertEqual(hold("prompt:x"), 120)
        self.assertEqual(hold("effort:low"), 8)
        self.assertEqual(hold("model:gpt-6-astra"), 8)
        self.assertEqual(hold("effort-up"), 8)
        self.assertEqual(hold("compact"), 75)
        self.assertEqual(hold("slash:/status"), 75)
        self.assertEqual(hold("wait@4"), 4)
        self.assertEqual(hold("prompt@15:x"), 15)


class RunStepsTests(unittest.TestCase):
    def test_runs_in_order_with_holds(self) -> None:
        tui = FakeTui()
        slept: list[float] = []
        steps = drive.parse_steps([
            "prompt:first", "effort:high", "effort-up", "model:gpt-6-astra,low", "prompt@30:second", "compact", "prompt:third",
            "wait@5",
        ])
        with redirect_stdout(io.StringIO()):
            drive.run_steps(tui, steps, prompt_hold=100, setting_hold=8, slash_hold=75, sleep=slept.append)
        self.assertEqual(tui.calls, [
            ("send", "first", True), ("pump", 100),
            ("send", "/model --reasoning=high", True), ("pump", 8),
            ("keys", b"\x1b."), ("pump", 8),
            ("send", "/model gpt-6-astra --reasoning=low", True), ("pump", 8),
            ("send", "second", True), ("pump", 30),
            ("send", "/compact", True), ("pump", 75),
            ("send", "third", True), ("pump", 100),
            ("pump", 5.0),
        ])
        self.assertEqual(len(slept), len(steps))


class CommandLineTests(unittest.TestCase):
    def run_script(self, *extra: str) -> subprocess.CompletedProcess:
        # -I 隔离模式 + 不写字节码；codex-bin 故意指向不存在的文件，校验必须发生在启动 TUI 之前。
        return subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "--codex-bin", "/nonexistent/codex", "--cwd", "/tmp", *extra],
            capture_output=True, text=True, timeout=30,
        )

    def test_bad_step_exits_2_before_launch(self) -> None:
        done = self.run_script("--step", "effort:warp")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("步骤参数错误", done.stdout)
        self.assertNotIn("启动 TUI", done.stdout)

    def test_step_and_prompt_are_exclusive(self) -> None:
        done = self.run_script("--step", "compact", "--prompt", "hi")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("不能同时使用", done.stdout)
        self.assertNotIn("启动 TUI", done.stdout)


if __name__ == "__main__":
    unittest.main()
