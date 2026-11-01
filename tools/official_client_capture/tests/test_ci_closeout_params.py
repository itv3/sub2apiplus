"""收尾工具包（``tools/ci/closeout/``）的参数解析与“字面量不残留”单元测试。

覆盖：
* 版本标记与 Campaign 标记的派生（0.166.0 -> 0166 / c01660，补丁非 0 时保留补丁位），以及用 0.166 的实际参数得到的全部派生值与上一轮 cenv.sh 里
  现取的值逐项相同（前缀、轮次目录、候选 id、VC-2 输入目录、分支名、收尾 tag、激活事实文件名、ARM64 路径）；
* 参数来源优先级：命令行 > 环境变量 CLOSEOUT_<大写名> > 总账字段 ``closeout.<键> = 值`` > 派生默认值；总账解析的各种写法；
* 缺少某一步的必需参数时报错，写明“缺哪个参数、怎么补”，退出码 2；版本格式不对、Campaign 名与目标版本对不上都报错；
* 命令行拆分：全局选项在步骤名之前，步骤参数（含空串、与全局选项同名的记号）原样保留；
* 步骤表与 ``closeout.sh`` 里的 case 分支一一对应；shell 导出行能被 bash 安全 eval（含空格与引号）；
* 入库源码里不残留 0.166 轮的专有字面量（版本号、标记、Campaign 名、提交号、日期、本机绝对路径）；
* 四处各自实现的 ``tag_of`` 彼此一致。

全部用合成参数，不依赖 ARM64、网络与 ``local-analysis``；临时目录建在系统临时目录。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
KIT = REPO_ROOT / "tools" / "ci" / "closeout"


def load(name: str):
    """按路径加载 tools/ci/closeout 下的模块（不写字节码）。"""

    spec = importlib.util.spec_from_file_location(f"{name}_closeout_test", KIT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cp = load("closeout_params")

# 用上一轮（0.166）的实际参数：这里的值写在测试里是对账用的“样例”，工具源码里不许出现它们。
ACTUAL = {
    "target_version": "0.166.0", "baseline_version": "0.165.0", "retire_version": "0.164.0",
    "campaign": "c01660-formal-vc1-r1-20261031t000000z", "tag_date": "20261101", "main": "/work/sub2apiplus",
}


def resolve(cli=None, env=None, step="b9-render-guide", ledger_text=None, today="20990101"):
    values = dict.fromkeys(cp.PARAM_SPECS)
    values.update(cli or {})
    values["with_part2"] = (cli or {}).get("with_part2", False)
    return cp.resolve(values, env or {}, cwd=Path("/nonexistent"), step=step, today=today, ledger_text=ledger_text)


class TagDerivation(unittest.TestCase):
    def test_tag_of(self) -> None:
        self.assertEqual(cp.tag_of("0.166.0"), "0166")
        self.assertEqual(cp.tag_of("0.149.1"), "01491")
        self.assertEqual(cp.tag_of("0.9.0"), "0009")

    def test_campaign_tag_keeps_patch_digit(self) -> None:
        self.assertEqual(cp.campaign_tag_of("0.166.0"), "c01660")
        self.assertEqual(cp.campaign_tag_of("0.149.1"), "c01491")

    def test_bad_version_rejected(self) -> None:
        for bad in ("0.166", "v0.166.0", "0.166.0-rc1", ""):
            with self.assertRaises(cp.ParamError):
                cp.check_version(bad, "版本")

    def test_tag_of_agrees_across_scripts(self) -> None:
        """四处各自带一份 tag_of（脚本要单独 scp 到 ARM64，不能互相 import），必须一致。"""

        others = [load("gen_terminal_facts"), load("make_closeout_config"), load("part2_minimal")]
        for version in ("0.166.0", "0.149.1", "0.200.0", "1.2.3"):
            for module in others:
                self.assertEqual(module.tag_of(version), cp.tag_of(version), (module.__name__, version))


class DerivedValuesMatchLastRound(unittest.TestCase):
    """用 0.166 的实际参数，派生值与上一轮 cenv.sh 里的取值逐项相同。"""

    def test_all_names(self) -> None:
        values, sources = resolve({**ACTUAL})
        expect = {
            "prefix": "CODEX_CLI_0165_TO_0166", "ctag": "c01660", "round": "r1", "stamp": "20261031t000000z",
            "candidate": "c01660-candidate-r1", "inputs": "c01660-r1-inputs-20261031t000000z",
            "rr": "/root/vc-rounds/c01660-r1", "close": "/root/vc-rounds/c01660-r1/closeout", "prep": "/root/codex-0166-prep",
            "data": "/root/docker/capture-cli/data", "fact_name": "c01660-production-activation-fact.json",
            "closeout_tag": "codex-01660-closeout-20261101",
            "vc6_receipt_tag": "codex-01660-vc6-catalog-promotion-retire-20261101",
            "cbranch": "reg0166/closeout", "int_branch": "reg0166/integration", "cand_branch": "codex/codex-0166-candidate-r1",
            "wt": "/work/sub2apiplus-wt/closeout166", "target_tag": "0166", "baseline_tag": "0165", "retire_tag": "0164",
            "out_dir": "/work/sub2apiplus/local-analysis/closeout-0.166.0", "facts": "/work/sub2apiplus/local-analysis/closeout-0.166.0/facts",
            "logs": "/work/sub2apiplus/local-analysis/closeout-0.166.0/logs",
            "spec_source_root": "/work/sub2apiplus/local-analysis/sources/codex-cli-0.166.0",
            "typescript_module": "/work/sub2apiplus/frontend/node_modules/typescript/lib/typescript.js",
            "ssh_host": "ARM64", "gate_driver": "/root/arm64-capture-driver/driver/entry-gates.sh",
        }
        for key, value in expect.items():
            self.assertEqual(values[key], value, key)
        self.assertEqual(sources["prefix"], "derived")
        self.assertEqual(sources["target_version"], "cli")

    def test_next_round_example_has_no_last_round_literals(self) -> None:
        values, _ = resolve({"target_version": "0.167.0", "baseline_version": "0.166.0", "retire_version": "0.165.0",
                             "campaign": "c01670-formal-vc1-r2-20261105t010203z", "main": "/work/sub2apiplus"})
        self.assertEqual(values["prefix"], "CODEX_CLI_0166_TO_0167")
        self.assertEqual(values["candidate"], "c01670-candidate-r2")
        self.assertEqual(values["inputs"], "c01670-r2-inputs-20261105t010203z")
        self.assertEqual(values["rr"], "/root/vc-rounds/c01670-r2")
        self.assertEqual(values["cand_branch"], "codex/codex-0167-candidate-r2")
        self.assertEqual(values["wt"], "/work/sub2apiplus-wt/closeout167")
        for text in values.values():
            self.assertNotRegex(str(text), r"0166.*0165|c01660")

    def test_campaign_version_mismatch(self) -> None:
        with self.assertRaises(cp.ParamError) as raised:
            resolve({**ACTUAL, "target_version": "0.167.0"})
        self.assertIn("版本标记", str(raised.exception))

    def test_campaign_shape(self) -> None:
        with self.assertRaises(cp.ParamError):
            resolve({**ACTUAL, "campaign": "c01660-formal-r1"})

    def test_campaign_optional_for_discovery(self) -> None:
        values, _ = resolve({"target_version": "0.166.0", "main": "/m"}, step="b0-check")
        self.assertEqual(values["candidate"], "c01660-candidate-r1")
        self.assertNotIn("campaign", values)
        self.assertNotIn("inputs", values)


class Sources(unittest.TestCase):
    def test_priority_cli_env_ledger_derived(self) -> None:
        ledger = "- `closeout.prefix` = FROM_LEDGER\nclseout.nothing = x\n"
        values, sources = resolve({**ACTUAL}, ledger_text=ledger)
        self.assertEqual((values["prefix"], sources["prefix"]), ("FROM_LEDGER", "ledger"))
        values, sources = resolve({**ACTUAL}, env={"CLOSEOUT_PREFIX": "FROM_ENV"}, ledger_text=ledger)
        self.assertEqual((values["prefix"], sources["prefix"]), ("FROM_ENV", "env"))
        values, sources = resolve({**ACTUAL, "prefix": "FROM_CLI"}, env={"CLOSEOUT_PREFIX": "FROM_ENV"}, ledger_text=ledger)
        self.assertEqual((values["prefix"], sources["prefix"]), ("FROM_CLI", "cli"))
        values, sources = resolve({**ACTUAL})
        self.assertEqual(sources["prefix"], "derived")

    def test_ledger_values_replace_arguments(self) -> None:
        ledger = "\n".join(["# 总账", "closeout.target_version = 0.166.0", "* closeout.baseline_version: 0.165.0", "> `closeout.retire_version`：`0.164.0`",
                            "closeout.campaign = c01660-formal-vc1-r1-20261031t000000z", "closeout.main = /work/sub2apiplus"])
        values, sources = resolve({}, ledger_text=ledger, step="b0-check")
        self.assertEqual(values["retire_version"], "0.164.0")
        self.assertEqual(sources["campaign"], "ledger")
        self.assertEqual(values["prefix"], "CODEX_CLI_0165_TO_0166")

    def test_parse_ledger_forms(self) -> None:
        text = "\n".join(["closeout.campaign = old", "正文里提到 closeout.campaign = 不是行首也不算", "- closeout.campaign = new  ", "closeout.empty =", "  closeout.round: r2"])
        found = cp.parse_ledger(text)
        self.assertEqual(found["campaign"], "new")  # 追加式总账：同键取最后一次
        self.assertEqual(found["round"], "r2")
        self.assertNotIn("empty", found)

    def test_ledger_file_unreadable(self) -> None:
        with self.assertRaises(cp.ParamError):
            cp.resolve({"ledger": "/nonexistent/ledger.md", "with_part2": False}, {}, cwd=Path("/"), step="b0-check")


class MissingParameters(unittest.TestCase):
    def test_message_names_missing_parameter_and_how_to_fix(self) -> None:
        with self.assertRaises(cp.ParamError) as raised:
            resolve({"target_version": "0.166.0", "main": "/m"}, step="b5-audit-index")
        text = str(raised.exception)
        for needle in ("baseline_version", "--baseline-version", "CLOSEOUT_BASELINE_VERSION", "closeout.baseline_version", "retire_version", "campaign", "--campaign"):
            self.assertIn(needle, text)

    def test_target_version_always_needed(self) -> None:
        with self.assertRaises(cp.ParamError) as raised:
            resolve({}, step="b1-worktree")
        self.assertIn("target_version", str(raised.exception))

    def test_ledger_hint_uses_given_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "STATUS.md"
            ledger.write_text("closeout.target_version = 0.166.0\n", encoding="utf-8")
            with self.assertRaises(cp.ParamError) as raised:
                cp.resolve({"ledger": str(ledger), "main": "/m", "with_part2": False}, {}, cwd=Path("/"), step="b6-commit-stage")
        self.assertIn(str(ledger), str(raised.exception))
        self.assertIn("baseline_version", str(raised.exception))

    def test_part2_steps_not_required_unless_enabled(self) -> None:
        resolve({"target_version": "0.166.0", "main": "/m"}, step="b3-regen")  # 没开 --with-part2：不检查 i1_dir，不报错
        with self.assertRaises(cp.ParamError) as raised:
            resolve({"target_version": "0.166.0", "main": "/m", "with_part2": True}, step="b3-regen")
        self.assertIn("i1_dir", str(raised.exception))

    def test_unknown_step(self) -> None:
        with self.assertRaises(cp.ParamError):
            resolve({**ACTUAL}, step="b99-nothing")

    def test_main_returns_2_and_prints_reason(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cp.main(["emit", "--version", "0.166.0", "--main", "/m", "b7-terminal-facts"], env={}, cwd=Path("/"))
        self.assertEqual(code, 2)
        self.assertIn("缺少参数 baseline_version", err.getvalue())


class CommandLine(unittest.TestCase):
    def test_split_keeps_step_arguments_verbatim(self) -> None:
        head, step, rest = cp.split_command_line(["emit", "--version", "0.166.0", "--with-part2", "--campaign=c1", "b16-prepush", "", "--fast", "--version", "x"])
        self.assertEqual(head, ["emit", "--version", "0.166.0", "--with-part2", "--campaign=c1"])
        self.assertEqual((step, rest), ("b16-prepush", ["", "--fast", "--version", "x"]))

    def test_option_after_step_is_not_global(self) -> None:
        _head, step, rest = cp.split_command_line(["show", "b9-render-guide", "--behavior-summary", "x"])
        self.assertEqual((step, rest), ("b9-render-guide", ["--behavior-summary", "x"]))

    def test_unknown_option_before_step_is_refused(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cp.main(["emit", "--no-such-option", "b0-check"], env={}, cwd=Path("/"))
        self.assertEqual(code, 2)
        self.assertIn("未知选项", err.getvalue())

    def test_missing_step(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cp.main(["emit", "--version", "0.166.0"], env={}, cwd=Path("/")), 2)

    def test_emit_is_evaluable_by_bash(self) -> None:
        values, _ = resolve({**ACTUAL, "co_author": "O'Neil <a b@x>"}, step="b16-prepush")
        text = cp.shell_exports(values, "b16-prepush", ["", "--fast", "含 空格", "it's"])
        script = text + '\nprintf "%s|%s|%s|%s|%s\\n" "${CO_AUTHOR}" "${CL_STEP}" "${#CL_STEP_ARGS[@]}" "${CL_STEP_ARGS[2]}" "${CL_STEP_ARGS[3]}"\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "O'Neil <a b@x>|b16-prepush|4|含 空格|it's")

    def test_steps_subcommand_lists_every_step(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cp.main(["steps"]), 0)
        for step in cp.STEPS:
            self.assertIn(step.name, out.getvalue())


class StepTable(unittest.TestCase):
    def test_every_required_key_is_a_known_parameter(self) -> None:
        names = [step.name for step in cp.STEPS]
        self.assertEqual(len(names), len(set(names)))
        for step in cp.STEPS:
            for key in step.requires:
                self.assertIn(key, cp.PARAM_SPECS, (step.name, key))

    def test_steps_match_closeout_sh_case_labels(self) -> None:
        source = (KIT / "closeout.sh").read_text(encoding="utf-8")
        labels = set(re.findall(r"^(b\d+[ab]?-[a-z0-9-]+)\)", source, re.M))
        self.assertEqual(labels, {step.name for step in cp.STEPS})

    def test_arm64_write_steps_are_guarded_in_closeout_sh(self) -> None:
        source = (KIT / "closeout.sh").read_text(encoding="utf-8")
        for step in cp.STEPS:
            if not step.arm64_write:
                continue
            body = source.split(f"\n{step.name})", 1)[1].split("\n;;", 1)[0]
            self.assertIn("arm64_write", body, step.name)

    def test_part2_steps_are_guarded_in_closeout_sh(self) -> None:
        source = (KIT / "closeout.sh").read_text(encoding="utf-8")
        for step in cp.STEPS:
            if step.part2:
                body = source.split(f"\n{step.name})", 1)[1].split("\n;;", 1)[0]
                self.assertIn("part2_enabled", body, step.name)


class NoLastRoundLiterals(unittest.TestCase):
    """入库源码里不残留上一轮（0.166）的专有字面量；测试文件里的样例不在扫描范围。"""

    # 0.150～0.166 的版本号（含补丁）、对应的 4 位／5 位标记、Campaign 与分支前缀、上一轮的提交号与日期戳。
    BANNED = re.compile(
        r"0\.1(?:5\d|6[0-6])(?:\.\d+)?\b"           # 0.150.x ~ 0.166.x
        r"|(?<![0-9])c?01(?:5\d|6[0-6])\d?(?![0-9])"  # 0160 / 01660 / c01660
        r"|reg01(?:5\d|6[0-6])"                       # reg0166/…
        r"|closeout1(?:5\d|6[0-6])\b"                 # closeout166
        r"|ed65367dce|9238676e4d|cd028ceb78|4b70aa891d|e1f5b3e898|2d6b7893c3|75dc751505|07eb6c6242"
        r"|2026103\d|2026110\d|2026-1[01]-\d\d"       # 上一轮 Campaign 时间戳与日期
        r"|/Users/czs|/private/tmp|scratch/[A-Z0-9]"  # 本机绝对路径
    )

    def test_sources_have_no_literals(self) -> None:
        offenders = []
        for path in sorted(KIT.iterdir()):
            if not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # 仓库里一直存在的历史测试模块名（晋升后形态回归清单要引用），不是本轮字面量
                match = self.BANNED.search(line.replace("test_codex_0151_worktree_successor", ""))
                if match:
                    offenders.append(f"{path.name}:{number}: {match.group(0)}")
        self.assertEqual(offenders, [], "入库源码里有上一轮专有字面量：\n" + "\n".join(offenders))

    def test_banned_pattern_catches_samples(self) -> None:
        for sample in ("CODEX_CLI_0165_TO_0166", "c01660-formal", "0.166.0", "reg0166/closeout", "20261031t000000z", "ed65367dce", "/Users/czs/x"):
            self.assertTrue(self.BANNED.search(sample), sample)
        for sample in ("0.167.0", "c01670", "0.149.1", "x.y.z", "0.2.18-5"):
            self.assertFalse(self.BANNED.search(sample), sample)

    def test_scripts_compile_without_writing_bytecode(self) -> None:
        for path in sorted(KIT.glob("*.py")):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        for path in sorted(KIT.glob("*.sh")):
            subprocess.run(["bash", "-n", str(path)], check=True)

    def test_no_bytecode_left_in_kit(self) -> None:
        self.assertEqual(list(KIT.rglob("__pycache__")), [])


if __name__ == "__main__":
    unittest.main()
