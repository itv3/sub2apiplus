"""入口空跑启动入口编排器时对开工检查六个调用环境变量的白名单透传（优化第 14 项的集成缺口，``tools/ci/entry_dryrun.py``）。

空跑在干净环境里起编排器（原先是 ``env -i HOME=… LANG=… PATH=… ARM64_VC_ENV=…``），六个变量（``MODEL_PROBE_URL``、``MODEL_PROBE_KEY_FILE``、``MODEL_PROBE_MINIMAL``、
``SECURITY_SCAN_REPO``、``OPENING_CHECKS_SKIP``、``OPENING_CHECKS_SKIP_REASON``）进不去，第一步 entry-preflight 的 model-availability 与 main-security-scan 必然报“未配置”。
修法：只在调用者当次环境里有非空值时才透传这六个，别的变量一律不带；值经子进程的 env 参数交给编排器，不进命令行。

这里覆盖纯函数与 ``_run_orchestrator`` 交给子进程的参数和环境（真实子进程端到端的用例在 ``test_ci_entry_dryrun``，链路上各层的环境继承也在那里）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_dryrun as dr

NAMES = ("MODEL_PROBE_URL", "MODEL_PROBE_KEY_FILE", "MODEL_PROBE_MINIMAL", "SECURITY_SCAN_REPO", "OPENING_CHECKS_SKIP", "OPENING_CHECKS_SKIP_REASON")
REASON = "中转维护中，已人工确认 \"模型在线\" $HOME `date` ; rm -rf / 'x'\t(多空格   保留)\n第二行"
VALUES = {"MODEL_PROBE_URL": "http://relay.probe.invalid/v1", "MODEL_PROBE_KEY_FILE": "/secure/relay-key", "MODEL_PROBE_MINIMAL": "yes-minimal",
          "SECURITY_SCAN_REPO": "probe-owner/probe-repo", "OPENING_CHECKS_SKIP": "model-availability", "OPENING_CHECKS_SKIP_REASON": REASON}
BASE = {"HOME": "/root", "LANG": "C.UTF-8", "PATH": dr.CLEAN_PATH, "ARM64_VC_ENV": "/run/env.sh"}


class EnvironmentTests(unittest.TestCase):
    def test_the_whitelist_is_exactly_the_six_documented_variables(self) -> None:
        self.assertEqual(dr.OPENING_CHECK_ENV, NAMES)

    def test_without_the_variables_the_environment_is_the_old_clean_one(self) -> None:
        self.assertEqual(dr._orchestrator_environment(Path("/run/env.sh"), {}), BASE)
        self.assertEqual(dr._orchestrator_environment(Path("/run/env.sh"), {"PATH": "/x", "HOME": "/home/x", "FOO": "bar"}), BASE,
                         "调用者的 PATH、HOME 等也不带进来：干净环境用固定值")

    def test_the_six_variables_are_passed_when_the_caller_has_them(self) -> None:
        environment = dr._orchestrator_environment(Path("/run/env.sh"), {**VALUES, "FOO": "bar", "AWS_SECRET_ACCESS_KEY": "s", "CI": "true"})
        self.assertEqual(environment, {**BASE, **VALUES})

    def test_each_variable_is_independent_and_only_non_empty_values_count(self) -> None:
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(dr._orchestrator_environment(Path("/e"), {name: "v"}), {**BASE, "ARM64_VC_ENV": "/e", name: "v"})
                self.assertNotIn(name, dr._orchestrator_environment(Path("/e"), {name: ""}), "空串等于没设")
                self.assertNotIn(name, dr._orchestrator_environment(Path("/e"), {}))

    def test_a_reason_with_spaces_quotes_shell_metacharacters_and_newlines_arrives_byte_for_byte(self) -> None:
        environment = dr._orchestrator_environment(Path("/e"), {"OPENING_CHECKS_SKIP_REASON": REASON})
        self.assertEqual(environment["OPENING_CHECKS_SKIP_REASON"], REASON)

    def test_the_default_source_is_the_current_process_environment(self) -> None:
        with mock.patch.dict(os.environ, {**VALUES, "FOO": "bar"}):
            environment = dr._orchestrator_environment(Path("/e"))
        self.assertEqual({name: environment[name] for name in NAMES}, VALUES)
        self.assertNotIn("FOO", environment)


class OrchestratorLaunchTests(unittest.TestCase):
    """``_run_orchestrator`` 交给 ``subprocess.run`` 的参数与环境（子进程替身）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runroot = Path(directory.name).resolve()
        self.payload = {"to": "atomic-double", "entry": "/drv/entry.sh"}

    def launch(self, caller: dict[str, str]):
        with mock.patch.dict(os.environ, caller), mock.patch.object(dr.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            for name in (*NAMES, "FOO"):
                if name not in caller:
                    os.environ.pop(name, None)
            dr._run_orchestrator(self.payload, self.runroot, Path("/run/env.sh"))
        return run.call_args.args[0], run.call_args.kwargs

    def test_the_command_line_has_no_values_and_no_env_wrapper_any_more(self) -> None:
        argv, kwargs = self.launch({**VALUES, "FOO": "bar"})
        self.assertEqual(argv, ["bash", "/drv/entry.sh", "--to", "atomic-double"], "命令行就是编排器本身，ps 里看不到任何取值")
        for value in VALUES.values():
            self.assertFalse(any(value in part for part in argv))
        self.assertEqual(kwargs["env"], {**BASE, **VALUES})
        self.assertNotIn("FOO", kwargs["env"])

    def test_without_the_variables_the_environment_is_unchanged_from_before(self) -> None:
        _argv, kwargs = self.launch({"FOO": "bar"})
        self.assertEqual(kwargs["env"], BASE)

    def test_the_mode_options_still_follow_the_entry_exactly_as_before(self) -> None:
        self.payload["opening"] = True
        argv, _kwargs = self.launch({})
        self.assertEqual(argv, ["bash", "/drv/entry.sh", "--to", "atomic-double", "--reexecute-gates", "--audit-reads"])

    def test_nothing_about_the_variables_is_written_to_the_orchestrator_log(self) -> None:
        self.launch(dict(VALUES))
        log = (self.runroot / "entry.out").read_bytes() if (self.runroot / "entry.out").exists() else b""
        for value in VALUES.values():
            self.assertNotIn(value.encode("utf-8"), log)

    def test_the_driver_carries_an_identical_copy(self) -> None:
        root = Path(dr.__file__).resolve().parents[2]
        self.assertEqual((root / "tools" / "arm64_capture_driver" / "driver" / "entry_dryrun.py").read_bytes(), Path(dr.__file__).read_bytes())


if __name__ == "__main__":
    unittest.main()
