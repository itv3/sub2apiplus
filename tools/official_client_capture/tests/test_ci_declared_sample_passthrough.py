"""``--declared-sample off|always|every:N`` 从公共后台与入口一路传到执行器（步骤 5 WP3b 第 1 项）。

正式验证轮（H7／H8）要显式 ``--declared-sample always``，所以选项要照 ``--audit-reads``／``--audit-strict`` 现有的透传方式，沿着两条链一路送到执行器的
``run-gates``：

* 公共后台：``background-validate.sh start``（原样交给）→ ``background_validation.py start``（解析、校验、进输入合同与结论）→ ``run`` 起的
  ``entry-gates.sh`` → 执行器；
* 入口：``entry-dryrun.sh start``（原样交给）→ ``entry_dryrun.py start``（解析、校验、进结论与结论文件名）→ ``entry.sh`` →
  ``entry_orchestrator.py``（解析、校验、进两步的步骤输入）→ ``entry-gates.sh``（entry 与 pre-A3 两次调用）→ 执行器。

约定：缺省是 off，不传时一切现有行为与输出逐字不变（不多一个参数、不多一个结论键、不改文件名、不改步骤输入）；非法值（不是 off、always、
every:N）在最外层就报错，不会跑到执行器才失败。

这里逐层核对：各层校验口径一致、最外层拒绝非法值、每一层确实转交、缺省时什么都不多、取值进了该进的绑定（步骤输入、输入合同、结论文件名）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from tools.ci import background_validation as bv
from tools.ci import entry_dryrun as dr
from tools.ci import entry_orchestrator as eo
from tools.ci import unit_executor as ue
from tools.official_client_capture.tests import test_ci_background_validation as bvt
from tools.official_client_capture.tests import test_ci_entry_orchestrator as eot

REPO_ROOT = Path(__file__).resolve().parents[3]
CI = REPO_ROOT / "tools" / "ci"
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
COMMIT = "a" * 40

# 取值口径与入口脚本 entry-gates.sh 的正则 ^(off|always|every:[1-9][0-9]*)$ 逐字相同：合法的取值本身就是规范写法，原样返回；
# 首尾空白、换行、前导零、大小写变体一律拒绝（过去 Python 四层先 strip() 再判，比 shell 宽，独立验收 #3 一般 11）。
VALID = {"off": "off", "always": "always", "every:1": "every:1", "every:3": "every:3", "every:12": "every:12", "every:100": "every:100"}
WHITESPACE = [" off", "off ", " always", "always ", "always\n", "\nalways", "\talways\n", " every:12 ", " every:3", "every:3 ", "every:3\n", "every:3\r",
              "every:3\r\n", "\u00a0always", "always\u00a0", "every:3\u3000", "alwa\nys"]
INVALID = ["", " ", "on", "every", "every:", "every:0", "every:-1", "every:x", "every:1.5", "every:07", "every:00", "ALWAYS", "Always", "Off", "OFF", "EVERY:3",
           "Every:3", "every:3 4", "every: 3", "every :3", "every:\u0663", "every:\uff13", "always;touch x", "off always", "--declared-sample", *WHITESPACE]
# \u5176\u5b83\u6587\u5b57\u7684\u6570\u5b57\uff08\u963f\u62c9\u4f2f-\u5370\u5ea6\u6570\u5b57\u4e09\u3001\u5168\u89d2\u6570\u5b57\u4e09\uff09\uff1aPython \u7684 [0-9] \u53ea\u8ba4 ASCII \u6570\u5b57\uff0c\u6c38\u8fdc\u62d2\u7edd\uff1bshell \u6b63\u5219\u7684 [0-9] \u4f1a\u4e0d\u4f1a\u5339\u914d\u5b83\u4eec\u53d6\u51b3\u4e8e locale \u7684
# \u6392\u5e8f\u89c4\u5219\uff0c\u6240\u4ee5\u53ea\u5728 Python \u56db\u5c42\u7684\u62d2\u7edd\u7528\u4f8b\u91cc\u51fa\u73b0\uff0c\u4e0d\u8fdb\u300c\u9010\u503c\u5bf9\u7167 shell\u300d\u3002
SHELL_LOCALE_DEPENDENT = {"every:\u0663", "every:\uff13"}


class ModeValidatorTests(unittest.TestCase):
    """四个入口各有一份校验函数（执行器、编排器、空跑、后台验证），口径必须一致：免得外层放过一个内层才报错的值，或者反过来。"""

    VALIDATORS = {"unit_executor": ue.declared_sample_mode, "entry_orchestrator": eo._declared_sample_mode,
                  "entry_dryrun": dr._declared_sample_mode, "background_validation": bv._declared_sample_mode}

    def test_every_layer_accepts_the_same_values_and_returns_the_same_canonical_form(self) -> None:
        for raw, canonical in VALID.items():
            for layer, validator in self.VALIDATORS.items():
                with self.subTest(layer=layer, raw=raw):
                    self.assertEqual(validator(raw), canonical)
                    self.assertEqual(canonical, raw, "合法的取值就是规范写法：不做任何规范化")

    def test_no_layer_trims_whitespace_or_newlines(self) -> None:
        """独立验收 #3 一般 11：shell 层的正则拒绝首尾空白与换行，Python 四层过去先 strip() 再判，同一个取值在两层得到不同结论。现在四层一样严格。"""

        for raw in WHITESPACE:
            for layer, validator in self.VALIDATORS.items():
                with self.subTest(layer=layer, raw=raw), self.assertRaises(argparse.ArgumentTypeError):
                    validator(raw)

    def test_the_python_layers_agree_with_the_shell_regex_value_by_value(self) -> None:
        """逐值对照：把 entry-gates.sh 里的取值校验原样截出来在 bash 里跑，同一批取值的结论（接受／拒绝）和四个 Python 校验函数必须一致。"""

        shell = EntryGatesScriptTests()
        for raw in [*VALID, *INVALID]:
            if raw in SHELL_LOCALE_DEPENDENT:
                continue   # shell 的 [0-9] 会不会匹配其它文字的数字取决于 locale 的排序规则，不拿来对照；Python 一侧的拒绝由别的用例钉住
            code, line, _stderr = shell.parse("--declared-sample", raw)
            accepted_by_shell = code == 0 and line == f"SAMPLE={raw}"
            if code == 0:
                self.assertEqual(line, f"SAMPLE={raw}", "shell 接受时不改写取值")
            for layer, validator in self.VALIDATORS.items():
                try:
                    validator(raw)
                    accepted = True
                except argparse.ArgumentTypeError:
                    accepted = False
                self.assertEqual(accepted, accepted_by_shell, f"{layer} 与 shell 对 {raw!r} 的结论不同（shell 退出码 {code}）")

    def test_every_layer_refuses_the_same_values_with_the_same_kind_of_error(self) -> None:
        for raw in INVALID:
            for layer, validator in self.VALIDATORS.items():
                with self.subTest(layer=layer, raw=raw):
                    with self.assertRaises(argparse.ArgumentTypeError) as caught:
                        validator(raw)
                    self.assertIn("--declared-sample", str(caught.exception))
                    self.assertIn("off、always 或 every:N", str(caught.exception))

    def test_a_non_string_value_is_refused_too(self) -> None:
        for layer, validator in self.VALIDATORS.items():
            for raw in (None, 3, ["always"], {"always": 1}):
                with self.subTest(layer=layer, raw=raw), self.assertRaises(argparse.ArgumentTypeError):
                    validator(raw)

    def test_the_parsers_default_to_off_and_accept_every_valid_form(self) -> None:
        start = ["start", "--runroot", "/r", "--data-root", "/d", "--bundle", "/b", "--branch", "x", "--commit", COMMIT]
        dryrun = ["start", "--runroot", "/r", "--data-root", "/d", "--vc-env", "/e", "--bundle", "/b", "--branch", "x", "--commit", COMMIT]
        self.assertEqual(bv._parse(start).declared_sample, "off")
        self.assertEqual(dr._parse(dryrun).declared_sample, "off")
        self.assertEqual(eo._parse([]).declared_sample, "off")
        self.assertEqual(ue._parse(["run-gates", "--manifest", "m.json"]).declared_sample, "off")
        for raw, canonical in VALID.items():
            with self.subTest(raw=raw):
                self.assertEqual(bv._parse([*start, "--declared-sample", raw]).declared_sample, canonical)
                self.assertEqual(dr._parse([*dryrun, "--declared-sample", raw]).declared_sample, canonical)
                self.assertEqual(eo._parse(["--declared-sample", raw]).declared_sample, canonical)
                self.assertEqual(ue._parse(["run-gates", "--manifest", "m.json", "--declared-sample", raw]).declared_sample, canonical)


class OutermostRejectionTests(unittest.TestCase):
    """非法值在最外层就报错：命令行退出码 2，没有任何副作用，不会等到入口门禁或执行器才失败。"""

    def run_cli(self, script: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-B", str(script), *argv], capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})

    def test_background_validation_start_refuses_before_touching_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for raw in ("sometimes", "every:0", "", " always", "always\n", " every:3 "):
                with self.subTest(raw=raw):
                    completed = self.run_cli(CI / "background_validation.py", "start", "--runroot", str(root / "run"), "--data-root", str(root / "data"),
                                             "--bundle", str(root / "x.bundle"), "--branch", "x", "--commit", COMMIT, "--declared-sample", raw)
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn("--declared-sample", completed.stderr)
            self.assertEqual(list(root.iterdir()), [], "最外层拒绝：没有建任何目录或结论")

    def test_entry_dryrun_start_refuses_before_touching_anything(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for raw in ("sometimes", "every:0", "", " always", "always\n", " every:3 "):
                with self.subTest(raw=raw):
                    completed = self.run_cli(CI / "entry_dryrun.py", "start", "--runroot", str(root / "run"), "--data-root", str(root / "data"),
                                             "--vc-env", str(root / "env.sh"), "--bundle", str(root / "x.bundle"), "--branch", "x", "--commit", COMMIT,
                                             "--declared-sample", raw)
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn("--declared-sample", completed.stderr)
            self.assertEqual(list(root.iterdir()), [])

    def test_entry_orchestrator_refuses_before_looking_for_any_parameter(self) -> None:
        for raw in ("sometimes", "every:0", "", " always", "always\n", " every:3 "):
            with self.subTest(raw=raw):
                completed = self.run_cli(CI / "entry_orchestrator.py", "--declared-sample", raw)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("--declared-sample", completed.stderr)
                self.assertNotIn("参数里没有 D 或 RUNROOT", completed.stderr, "在检查环境参数之前就拒绝了")

    def test_the_orchestrator_constructor_refuses_too(self) -> None:
        for raw in ("sometimes", "", " always", "always\n", "every:03"):
            with self.subTest(raw=raw), self.assertRaisesRegex(eo.OrchestratorError, "--declared-sample"):
                eo.Orchestrator({}, driver_dir=DRIVER, run_dir=DRIVER, steps_dir=DRIVER, declared_sample=raw)


class EntryGatesScriptTests(unittest.TestCase):
    """``entry-gates.sh`` 的参数解析与转交：把脚本里的两段原样截出来在 bash 里跑（不跑门禁本身），与 test_ci_unit_inherit_executor 同一做法。"""

    SCRIPT = DRIVER / "entry-gates.sh"

    def segment(self, start: str, end: str) -> str:
        text = self.SCRIPT.read_text(encoding="utf-8")
        begin = text.index(start)
        return text[begin:text.index(end, begin)]

    def parse(self, *args: str) -> tuple[int, str, str]:
        segment = self.segment('PROFILE=entry; OUT="";', 'for file in "$POLICY_ACTIVATION" "$PRE_A3_CERTIFICATION"; do')
        snippet = ('set -Eeuo pipefail\nusage() { echo "用法" >&2; }\nD=/data; HISTORY_TEST_TREE=/hist\n' + segment + 'printf "SAMPLE=%s\\n" "$DECLARED_SAMPLE"\n')
        completed = subprocess.run(["bash", "-c", snippet, "entry-gates", *args, "/bundle", "codex/x", "c" * 40], capture_output=True, text=True, timeout=30)
        return completed.returncode, completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "", completed.stderr

    def test_the_default_is_off_and_valid_values_are_taken_as_given(self) -> None:
        self.assertEqual(self.parse()[:2], (0, "SAMPLE=off"))
        for raw in ("off", "always", "every:1", "every:12"):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse("--declared-sample", raw)[:2], (0, f"SAMPLE={raw}"))

    def test_invalid_values_exit_2_at_the_script_boundary(self) -> None:
        for raw in ("sometimes", "every:0", "every:", "every:x", "ALWAYS", "", "every:07", "always extra", *WHITESPACE):
            with self.subTest(raw=raw):
                code, _line, stderr = self.parse("--declared-sample", raw)
                self.assertEqual(code, 2, stderr)
                self.assertIn("--declared-sample 只接受 off、always 或 every:N", stderr)
        # 少了取值：后面的位置参数被当成取值，剩下的位置参数不够，走用法错误，同样以 2 退出而不会往下跑。
        code, line, stderr = self.parse("--declared-sample")
        self.assertEqual((code, line), (2, ""), stderr)
        self.assertIn("用法", stderr)

    def forwarded(self, **values: str) -> tuple[list[str], str]:
        segment = self.segment('AUDIT_ARGS=()\nif [ "$AUDIT_READS" = true ]', "# B5a-08：承接上下文要本轮客户端、入口阶段与 control 目录")
        defaults = {"AUDIT_READS": "false", "AUDIT_STRICT": "false", "AUDIT_HOST_INPUTS": "", "DECLARED_SAMPLE": "off", "FULL_SET_REQUEST": "", "D": "/data"}
        assignments = "".join(f"{key}={json.dumps(value)}\n" for key, value in {**defaults, **values}.items())
        snippet = ("set -Eeuo pipefail\nutc_now() { echo T; }\nFULL_SET_ARGS=()\n" + assignments + segment +
                   'for item in ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"}; do printf "[%s]" "$item"; done; printf "\\n"\n')
        completed = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        return re.findall(r"\[([^]]*)\]", lines[-1]), "\n".join(lines[:-1])

    def test_the_mode_reaches_the_executor_arguments_only_when_it_is_not_off(self) -> None:
        self.assertEqual(self.forwarded(), ([], ""), "缺省：执行器参数与输出同改造前，一个字都不多")
        self.assertEqual(self.forwarded(DECLARED_SAMPLE="off"), ([], ""))
        items, output = self.forwarded(DECLARED_SAMPLE="always")
        self.assertEqual(items, ["--declared-sample", "always"])
        self.assertIn("声明级采样：--declared-sample always", output)
        self.assertEqual(self.forwarded(DECLARED_SAMPLE="every:3")[0], ["--declared-sample", "every:3"])

    def test_it_travels_with_the_audit_arguments_in_a_fixed_order(self) -> None:
        items, _output = self.forwarded(AUDIT_READS="true", AUDIT_STRICT="true", AUDIT_HOST_INPUTS="/h.json", DECLARED_SAMPLE="always")
        self.assertEqual(items, ["--audit-reads", "--audit-data-root", "/data", "--audit-strict", "--audit-host-inputs", "/h.json", "--declared-sample", "always"])
        self.assertEqual(self.forwarded(AUDIT_READS="true")[0], ["--audit-reads", "--audit-data-root", "/data"], "不带采样方式时审计参数与改造前相同")

    def test_the_single_executor_invocation_expands_the_audit_arguments(self) -> None:
        """脚本里只有一处起 run-gates，且它展开 AUDIT_ARGS——采样方式经 AUDIT_ARGS 转交，所以不会有哪一次调用漏掉它。"""

        text = self.SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r"unit_executor\.py\" run-gates[^\n]*\n[^\n]*", text)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn('${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"}', calls[0])

    def test_the_script_still_parses(self) -> None:
        completed = subprocess.run(["bash", "-n", str(self.SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class OrchestratorPassthroughTests(eot.OrchestratorTestCase):
    """入口编排器：选项进两步的步骤输入、原样交给入口门禁与 pre-A3 两次调用；缺省时一切不变。"""

    def orchestrate(self, **kwargs):
        """基类的助手不认识 ``declared_sample``：把它改成构造参数后照旧交给基类（基类负责其余全部选项）。"""

        sample = kwargs.pop("declared_sample", None)
        if sample is None:
            return super().orchestrate(**kwargs)
        original = eo.Orchestrator

        def with_sample(*args, **inner):
            return original(*args, declared_sample=sample, **inner)

        with mock.patch.object(eo, "Orchestrator", with_sample):
            return super().orchestrate(**kwargs)

    def build(self, **kwargs) -> eo.Orchestrator:
        return eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.fx.root / "build", steps_dir=self.fx.steps_dir,
                               runner=self.commands, es_runner=self.fx.runner, **kwargs)

    def gate_calls(self, since: int = 0) -> list[list[str]]:
        return [call for call in self.commands.calls[since:] if call[0] == "bash" and call[1].endswith("entry-gates.sh")]

    def run_gates(self, *, fresh: bool = True, **options) -> list[list[str]]:
        """起一个编排器：入口门禁随编排跑一次，再单独做一次 pre-A3，返回这两次入口门禁调用的命令行（临时根换成占位符）。

        缺省每次都重建夹具：正式坐标的序号后缀、pre-A3 沿用还是重做都取决于此前跑过什么，要逐字对比命令行就必须从同样的起点开始。"""

        if fresh:
            self.setUp()
        root = self.fx.root
        orchestrator = eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=root / "gates", steps_dir=self.fx.steps_dir,
                                       runner=self.commands, es_runner=self.fx.runner, reexecute_gates=True, **options)
        result = orchestrator.orchestrate(to_step="atomic-double")
        self.assertEqual(result["exit_code"], 0, self.actions(result))
        orchestrator.outcomes["pre-a3"] = eo.Outcome("pre-a3", "run")
        orchestrator.entry_gates_and_pre_a3(False, True)
        calls = self.gate_calls()
        self.assertEqual([eot._option(call, "--profile") for call in calls], ["entry", "pre-a3"])
        return [[part.replace(str(root), "<根>") for part in call] for call in calls]

    def test_the_mode_reaches_both_gate_invocations_only_when_given(self) -> None:
        plain = self.run_gates()
        self.assertTrue(all("--declared-sample" not in call for call in plain), "缺省：入口门禁与 pre-A3 的命令行里没有这个选项")
        sampled = self.run_gates(declared_sample="always")
        self.assertEqual([eot._option(call, "--declared-sample") for call in sampled], ["always", "always"])
        self.assertTrue(all(call.count("--declared-sample") == 1 for call in sampled))
        every = self.run_gates(declared_sample="every:3")
        self.assertEqual([eot._option(call, "--declared-sample") for call in every], ["every:3", "every:3"])

    def test_the_commands_are_identical_with_and_without_an_explicit_off(self) -> None:
        plain = self.run_gates()
        self.assertEqual(self.run_gates(declared_sample="off"), plain, "显式 off 与不传：两次门禁调用的命令行逐字相同")

    def test_the_mode_is_the_only_difference_in_the_gate_command_lines(self) -> None:
        plain = self.run_gates()
        sampled = self.run_gates(declared_sample="always")

        def without_mode(call: list[str]) -> list[str]:
            at = call.index("--declared-sample")
            return call[:at] + call[at + 2:]

        self.assertEqual([without_mode(call) for call in sampled], plain, "去掉这一个选项后与缺省调用完全相同：没有顺手改别的")

    def test_the_mode_travels_with_the_audit_options(self) -> None:
        policy = self.fx.root / "audit-policy.json"
        eot._write_json(policy, {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}})
        calls = self.run_gates(fresh=False, audit_reads=True, audit_strict=True, audit_policy=policy, declared_sample="always")
        for call in calls:
            self.assertIn("--audit-reads", call)
            self.assertIn("--audit-strict", call)
            self.assertEqual(eot._option(call, "--audit-policy"), "<根>/audit-policy.json")   # run_gates 已把临时根换成占位符
            self.assertEqual(eot._option(call, "--declared-sample"), "always")

    def test_step_inputs_carry_the_mode_only_in_the_two_gate_steps(self) -> None:
        def inputs(**kwargs) -> dict[str, tuple]:
            return {step.step_id: tuple(step.inputs) for step in self.build(**kwargs).ctx.steps}

        # 编排器是用 importlib 单独加载 entry_steps 的，Input 类要取它自己的那份（与 tools.ci.entry_steps 不是同一个模块对象）。
        param = eo.ES.Input("param", "param", eo.ES.DECLARED_SAMPLE_PARAM)
        default = inputs()
        self.assertEqual(default, inputs(declared_sample="off"), "缺省与显式 off：步骤输入逐字相同")
        for sample in ("always", "every:2"):
            with self.subTest(sample=sample):
                changed = inputs(declared_sample=sample)
                self.assertEqual(set(changed), set(default))
                for step_id, step_inputs in changed.items():
                    if step_id in ("entry-gates", "pre-a3"):
                        self.assertEqual(step_inputs, default[step_id] + (param,), step_id)
                    else:
                        self.assertEqual(step_inputs, default[step_id], f"{step_id}：其它步骤不受影响")
        self.assertEqual(self.build(declared_sample="every:2").params[eo.ES.DECLARED_SAMPLE_PARAM], "every:2")
        self.assertNotIn(eo.ES.DECLARED_SAMPLE_PARAM, self.build().params)

    def test_a_same_named_key_in_the_environment_cannot_switch_it_on(self) -> None:
        self.fx.params[eo.ES.DECLARED_SAMPLE_PARAM] = "always"
        orchestrator = self.build()
        self.assertNotIn(eo.ES.DECLARED_SAMPLE_PARAM, orchestrator.params, "只从本次显式选项生成步骤输入")
        self.assertEqual(orchestrator.declared_sample, "off")
        explicit_off = self.build(declared_sample="off")
        self.assertNotIn(eo.ES.DECLARED_SAMPLE_PARAM, explicit_off.params)

    def test_changing_the_mode_redoes_the_gate_steps_but_keeps_the_ledger(self) -> None:
        first = self.orchestrate(declared_sample="always")
        self.assertEqual(first["exit_code"], 0, self.actions(first))
        unchanged = self.orchestrate(declared_sample="always")
        self.assertEqual(self.actions(unchanged)["pre-a3"], "沿用")
        changed = self.orchestrate(declared_sample="every:2")
        actions = self.actions(changed)
        self.assertEqual(changed["exit_code"], 0, actions)
        self.assertEqual((actions["entry-gates"], actions["pre-a3"]), ("执行／passed", "执行／passed"), "换了采样方式不能沿用上一种方式的步骤记录")
        self.assertEqual(actions["ledger"], "沿用")
        self.assertEqual(self.commands.count("create"), 1)
        back = self.actions(self.orchestrate())
        self.assertEqual((back["entry-gates"], back["pre-a3"]), ("执行／passed", "执行／passed"), "改回缺省 off 同样重做")

    def test_main_passes_the_parsed_mode_to_the_constructor(self) -> None:
        seen: list[str] = []

        class Probe:
            def __init__(self, *args, **kwargs) -> None:
                seen.append(kwargs["declared_sample"])

            def orchestrate(self, **kwargs):
                return {"exit_code": 0, "steps": []}

        environ = {"D": str(self.fx.root), "RUNROOT": str(self.fx.root / "run")}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)   # main 里打开的锁文件靠进程退出关闭，这里不是被测对象
            with mock.patch.object(eo, "Orchestrator", Probe), mock.patch.object(eo, "print_summary"), mock.patch.dict("os.environ", environ, clear=True):
                self.assertEqual(eo.main(["--declared-sample", "every:4"]), 0)
                self.assertEqual(eo.main([]), 0)
        self.assertEqual(seen, ["every:4", "off"])


class DryRunPassthroughTests(unittest.TestCase):
    """入口空跑：选项进结论（载荷与文件名）、派发时原样交给入口编排器；缺省时结论、文件名与命令行逐字不变。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data, self.runroot = self.root / "data", self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        (self.data / "control" / "codex-01600-supervisor-enable-20261003t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.bundle = self.root / "x.bundle"
        self.bundle.write_bytes(b"dryrun-bundle")
        self.vc_env = self.root / "env.sh"
        self.vc_env.write_text("", encoding="utf-8")

    def namespace(self, **changes) -> argparse.Namespace:
        values = dict(runroot=self.runroot, data_root=self.data, vc_env=self.vc_env, bundle=self.bundle, branch="codex/test", commit=COMMIT, opening=False,
                      reexecute=False, unit_inherit_request=None, unit_inherit_full_gates_request=None, full_set_work=None, audit_policy=None, to=None,
                      entry=self.root / "entry.sh")
        values.update(changes)
        return argparse.Namespace(**values)

    def start(self, **changes) -> tuple[int, dict, dict]:
        # 后台进程用本测试进程的号顶替：空跑结论里记的 pid 还活着，才会被判成“已在跑”（exists）。
        with mock.patch.object(dr, "capture_busy", return_value=None), mock.patch.object(dr.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())):
            code, outcome = dr.start(self.namespace(**changes))
        return code, outcome, dr._read(Path(outcome["result"]))

    def test_the_default_conclusion_is_exactly_what_it_was_before(self) -> None:
        code, outcome, payload = self.start()
        self.assertEqual((code, outcome["action"]), (0, "started"))
        self.assertNotIn("declared_sample", payload)
        deployment = payload["deployment"]
        self.assertEqual(Path(outcome["result"]), dr.result_path(self.runroot, COMMIT, deployment, opening=False, to=dr.DEFAULT_TO), "文件名不变")
        # 参数对象里没有这个属性（改造前的调用方式）与显式 off：同一份结论。
        again = self.start(declared_sample="off")
        self.assertEqual((again[1]["action"], again[1]["result"]), ("exists", outcome["result"]), "显式 off 与不传是同一种空跑")
        self.assertNotIn("declared_sample", again[2])

    def test_a_non_default_mode_is_recorded_and_names_a_different_conclusion(self) -> None:
        _code, base, _payload = self.start()
        results = {}
        for sample in ("always", "every:3"):
            code, outcome, payload = self.start(declared_sample=sample)
            self.assertEqual((code, outcome["action"]), (0, "started"), "采样方式不同的空跑是不同的空跑，不被缺省空跑顶替")
            self.assertEqual(payload["declared_sample"], sample)
            self.assertNotEqual(outcome["result"], base["result"])
            results[sample] = outcome["result"]
        self.assertTrue(results["always"].endswith("-declared-sample-always.json"))
        self.assertTrue(results["every:3"].endswith("-declared-sample-every-3.json"))
        self.assertEqual(Path(results["always"]).name.removesuffix("-declared-sample-always.json") + ".json", Path(base["result"]).name,
                         "文件名只在末尾多一段，前缀（提交、部署）不变")
        again = self.start(declared_sample="always")
        self.assertEqual((again[1]["action"], again[1]["result"]), ("exists", results["always"]), "同一种采样方式不重复起")

    def test_the_mode_combines_with_the_other_kinds_of_dry_runs(self) -> None:
        for changes in ({"opening": True}, {"reexecute": True}):
            with self.subTest(changes=changes):
                _code, outcome, payload = self.start(declared_sample="always", **changes)
                self.assertEqual(payload["declared_sample"], "always")
                self.assertTrue(Path(outcome["result"]).name.endswith("-declared-sample-always.json"))

    def dispatched(self, payload: dict) -> list[str]:
        captured: list[list[str]] = []
        with mock.patch.object(dr.subprocess, "run", side_effect=lambda argv, **kwargs: captured.append(list(argv)) or mock.Mock(returncode=0)):
            dr._run_orchestrator(payload, self.runroot, self.vc_env)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_the_orchestrator_command_line_carries_the_mode_only_when_it_is_recorded(self) -> None:
        for changes in ({}, {"opening": True}, {"reexecute": True}):
            with self.subTest(changes=changes):
                _code, _outcome, plain = self.start(**changes)
                default = self.dispatched(plain)
                self.assertNotIn("--declared-sample", default, "缺省：命令行里没有这个选项")
                _code, _outcome, payload = self.start(declared_sample="every:2", **changes)
                argv = self.dispatched(payload)
                self.assertEqual(argv.count("--declared-sample"), 1)
                self.assertEqual(argv[-2:], ["--declared-sample", "every:2"], "选项放在命令行末尾，不挪动原有选项")
                self.assertEqual(argv[:-2], default, "去掉采样方式后与缺省空跑的命令行逐项相同")

    def test_a_tampered_conclusion_cannot_smuggle_anything_onto_the_command_line(self) -> None:
        _code, _outcome, payload = self.start(declared_sample="always")
        for bad in ("always; rm -rf /", "sometimes", ["always"], 3):
            with self.subTest(bad=bad):
                with mock.patch.object(dr.subprocess, "run") as run, self.assertRaisesRegex(dr.DryRunError, "采样方式非法"):
                    dr._run_orchestrator({**payload, "declared_sample": bad}, self.runroot, self.vc_env)
                run.assert_not_called()

    def test_start_refuses_an_invalid_mode_programmatically_too(self) -> None:
        for raw in ("sometimes", "", " always", "always\n", "every:03"):
            with self.subTest(raw=raw), mock.patch.object(dr, "capture_busy", return_value=None), self.assertRaisesRegex(dr.DryRunError, "--declared-sample"):
                dr.start(self.namespace(declared_sample=raw))

    def test_only_a_missing_mode_means_off_an_empty_string_is_an_invalid_value(self) -> None:
        """过去 ``getattr(args, "declared_sample", None) or "off"`` 把空字符串也当成缺省 off；入口脚本的正则拒绝空串，这里同口径：只有没给（属性不存在或 None）才是 off。"""

        self.assertEqual(self.start()[1]["action"], "started")
        namespace = self.namespace()
        self.assertFalse(hasattr(namespace, "declared_sample"))
        with mock.patch.object(dr, "capture_busy", return_value=None), mock.patch.object(dr.subprocess, "Popen", return_value=mock.Mock(pid=os.getpid())):
            code, outcome = dr.start(self.namespace(declared_sample=None))
        self.assertEqual((code, outcome["action"]), (0, "exists"), "None 与不传是同一种空跑")


class BackgroundPassthroughTests(unittest.TestCase):
    """后台验证：选项进输入合同与结论、``run`` 起入口门禁时原样交给它；缺省时输入合同、结论与命令行逐字不变。"""

    FAKE_ENTRY_GATES = r"""#!/bin/bash
set -euo pipefail
python3 - "$@" <<'PY'
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_GATES_ARGV_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\n")
out = argv[argv.index("--out") + 1]
os.makedirs(out, exist_ok=True)
json.dump({"status": "passed", "gates": [{"gate_id": "g", "status": "passed"}],
           "source": {"commit": argv[-1], "deploy_receipt": os.environ.get("FAKE_GATES_RECEIPT", "")}}, open(out + "/entry-gates.json", "w"))
PY
"""

    def setUp(self) -> None:
        self.helper = bvt.BackgroundValidationTests("cli")   # 复用后台验证测试的夹具（数据根、部署收据、命令行助手），不跑它的用例
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.argv_log = self.helper.root / "argv.jsonl"
        self.helper.gates.write_text(self.FAKE_ENTRY_GATES, encoding="utf-8")
        self.helper.environ["FAKE_GATES_ARGV_LOG"] = str(self.argv_log)

    def start(self, commit: str, *extra: str) -> subprocess.CompletedProcess[str]:
        h = self.helper
        return h.cli("start", "--runroot", str(h.runroot), "--data-root", str(h.data), "--bundle", str(h.root / "x.bundle"), "--branch", "codex/test",
                     "--commit", commit, "--entry-gates", str(h.gates), *extra)

    def finished(self, completed: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        started = json.loads(completed.stdout.strip().splitlines()[-1])
        return self.helper.wait(started["result"])

    def gate_argv(self) -> list[list[str]]:
        return [json.loads(line) for line in self.argv_log.read_text(encoding="utf-8").splitlines()]

    def test_the_default_start_leaves_the_contract_the_conclusion_and_the_command_line_as_they_were(self) -> None:
        payload = self.finished(self.start(COMMIT_A))
        self.assertEqual(payload["status"], "passed", payload)
        self.assertNotIn("declared_sample", payload)
        self.assertNotIn("declared_sample", payload["input_contract"], "缺省保持旧的输入合同字段形状")
        [argv] = self.gate_argv()
        self.assertNotIn("--declared-sample", argv)
        explicit = self.start(COMMIT_A, "--declared-sample", "off")
        self.assertEqual((explicit.returncode, json.loads(explicit.stdout.strip().splitlines()[-1])["action"]), (0, "exists"),
                         "显式 off 与不传是同一份输入合同")

    def test_an_explicit_mode_reaches_the_entry_gates_and_binds_the_contract(self) -> None:
        payload = self.finished(self.start(COMMIT_A, "--declared-sample", "always"))
        self.assertEqual(payload["status"], "passed", payload)
        self.assertEqual((payload["declared_sample"], payload["input_contract"]["declared_sample"]), ("always", "always"))
        [argv] = self.gate_argv()
        self.assertEqual(argv.count("--declared-sample"), 1)
        self.assertEqual(argv[argv.index("--declared-sample") + 1], "always")
        again = self.start(COMMIT_A, "--declared-sample", "always")
        self.assertEqual(json.loads(again.stdout.strip().splitlines()[-1])["action"], "exists", "同一取值不重复起")
        every = self.finished(self.start(COMMIT_B, "--declared-sample", "every:2"))
        self.assertEqual(every["declared_sample"], "every:2")

    def test_a_different_mode_for_the_same_commit_and_deployment_needs_an_explicit_stop(self) -> None:
        self.finished(self.start(COMMIT_A, "--declared-sample", "always"))
        for extra in ((), ("--declared-sample", "every:2"), ("--declared-sample", "off")):
            with self.subTest(extra=extra):
                refused = self.start(COMMIT_A, *extra)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn("输入或缓存隔离合同不同", refused.stderr)

    def test_invalid_values_are_refused_before_any_background_process_starts(self) -> None:
        for raw in ("sometimes", "every:0", "", " always", "always\n", "every:3 "):
            with self.subTest(raw=raw):
                refused = self.start(COMMIT_A, "--declared-sample", raw)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("--declared-sample", refused.stderr)
        self.assertFalse(self.argv_log.exists(), "没有起过入口门禁")

    def test_start_refuses_an_empty_or_padded_mode_programmatically_and_only_a_missing_one_means_off(self) -> None:
        """与入口脚本的正则同口径（空串、首尾空白、换行都拒绝）：过去 ``getattr(…) or "off"`` 把空串悄悄当成缺省 off。只有没给（None）才是 off。"""

        h = self.helper
        arguments = ["start", "--runroot", str(h.runroot), "--data-root", str(h.data), "--bundle", str(h.root / "x.bundle"),
                     "--branch", "codex/test", "--commit", COMMIT_A, "--entry-gates", str(h.gates)]
        for raw in ("", " always", "always\n", "every:03"):
            args = bv._parse(arguments)
            args.declared_sample = raw
            # 万一哪天又放过了这些取值，也不能真起后台进程（那会让回归变成一个挂住的测试）：起进程直接当失败。
            with (self.subTest(raw=raw), mock.patch.object(bv.subprocess, "Popen", side_effect=AssertionError("非法取值不得起后台进程")),
                  self.assertRaisesRegex(bv.ValidationError, "--declared-sample")):
                bv.start(args)
        args = bv._parse(arguments)
        args.declared_sample = None
        with mock.patch.object(bv.subprocess, "Popen", return_value=mock.Mock(pid=-1)):
            started = bv.start(args)
        self.assertNotIn("declared_sample", bv._read(Path(started["result"])), "None 与不传一样是缺省 off：结论里没有这个键")

    def test_run_refuses_a_conclusion_whose_mode_drifted_from_the_input_contract(self) -> None:
        h = self.helper
        with mock.patch.object(bv.subprocess, "Popen", return_value=mock.Mock(pid=-1)):
            started = bv.start(bv._parse(["start", "--runroot", str(h.runroot), "--data-root", str(h.data), "--bundle", str(h.root / "x.bundle"),
                                          "--branch", "codex/test", "--commit", COMMIT_A, "--entry-gates", str(h.gates), "--declared-sample", "always"]))
        path = Path(started["result"])
        original = bv._read(path)
        self.assertEqual(original["declared_sample"], "always")
        contract = original["input_contract"]
        damaged = {
            "改成别的取值": {**original, "declared_sample": "every:2"},
            "取值被删掉": {key: value for key, value in original.items() if key != "declared_sample"},
            "取值改成 off": {**original, "declared_sample": "off"},
            "不是规范写法": {**original, "declared_sample": " always "},
            "不是字符串": {**original, "declared_sample": ["always"]},
            "合同里的被删掉": {**original, "input_contract": {key: value for key, value in contract.items() if key != "declared_sample"}},
        }
        for label, body in damaged.items():
            with self.subTest(label):
                bv._write(path, body)
                with mock.patch.object(bv.subprocess, "Popen", side_effect=AssertionError("漂移时不得起入口门禁")):
                    self.assertEqual(bv.run(path), 3)
                result = bv._read(path)
                self.assertEqual(result["status"], "aborted")
                self.assertIn("采样方式", result["reason"])

    def test_a_same_commit_continuation_must_keep_the_original_mode(self) -> None:
        """逐单元续作保持原采样方式：悄悄换成缺省的 off（或反过来）会让读集证据的来源变掉。"""

        helper = bvt.BackgroundUnitInheritTests("arguments")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        with mock.patch.object(bv.subprocess, "Popen", return_value=helper.process):
            started = bv.start(helper.arguments(None, "--declared-sample", "always"))
        path = Path(started["result"])
        bv._write(path, {**bv._read(path), "status": "failed", "returncode": 1, "failed_gates": ["test-capture-tools"]})
        with mock.patch.object(bv.subprocess, "Popen", return_value=helper.process):
            for extra in ((), ("--declared-sample", "every:2")):
                with self.subTest(extra=extra), self.assertRaisesRegex(bv.ValidationError, "必须保留原采样方式"):
                    bv.start(helper.arguments(helper.request, *extra))
            resumed = bv.start(helper.arguments(helper.request, "--declared-sample", "always"))
        self.assertEqual(Path(resumed["result"]), path)
        self.assertEqual(bv._read(path)["declared_sample"], "always")


COMMIT_A, COMMIT_B = bvt.COMMIT_A, bvt.COMMIT_B


if __name__ == "__main__":
    unittest.main()
