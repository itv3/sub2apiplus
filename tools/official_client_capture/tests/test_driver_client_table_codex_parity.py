"""B5a-06 验收：驱动链与入口基础设施按客户端参数化之后，Codex 的派生、判定与命令行与改造前逐字相同。

改造前的源码取自 B5a-06 开工时本分支的 HEAD（``BASELINE_COMMIT``，即 B5a-04 的提交），从真实仓库的 git 历史读出，作为独立
模块对象加载，与改造后的模块在同一组输入下各算一次：

* 参数解析（``parse_env.py``）：同一份 Codex 参数文件（驱动测试夹具与改造前的参数模板）``parse`` 与 ``derive`` 的结果逐字
  相同（含键的先后），主程序输出等于改造前的输出再加上新增可选键（B5a-06 四个、B5a-09 的 ``UPSTREAM_IP``）的 ``unset``
  （清掉外层环境残留的另一客户端参数）；
  一组非法改写的报错逐字相同；用通用键（``CLIENT_BIN`` 等）写的 Codex 参数文件与用旧键写的解析结果相同；
* 入口步骤（``entry_steps.py``）：Codex 步骤表的输入声明与摘要、门禁项输入清单逐字相同；同一夹具上无记录、改造前写的
  记录、改造后写的记录、逐类改动输入、Formal 已建与强制重做各情形下 ``evaluate`` 的结果逐字相同；
* 入口编排（``entry_orchestrator.py``，即 ``entry.sh --plan``）：同一夹具上只判定的结果与打印的判定表逐字相同；
* 驱动脚本：把改动后脚本里的客户端表变量（``CT_``）代入 Codex 的取值、基线里 B5a-04 的客户端参数按 Codex 取空之后，
  与改造前的非注释行逐行相同，只剩登记在 ``SHELL_RESIDUALS`` 的几处（每处写明为什么等价）；
* 安装脚本（``install.py``）：只有 Codex 纳管时安装收据与复验结果和改造前相同；后台验证、入口门禁、入口空跑沿用的
  部署收据规则与 pre-A3 声明和改造前相同。

基线提交读不到（浅克隆）时跳过并写明原因；某个驱动脚本在 B5a-06 之后又改过（内容摘要不再是 ``SHELL_DIGESTS`` 登记的
那份）时，它的逐行对照跳过——以新的基线提交重新对照：B5a-09 改过的入口便宜检查以 B5a-09 开工时的提交
（``B5A09_BASELINE_COMMIT``）为新基线再对照一次（``B5A09_SHELL_RESIDUALS``），B5a-10 改过的批次派发、VC-5 验收与入口门禁
以 B5a-10 开工时的提交（``B5A10_BASELINE_COMMIT``）为新基线，B3-04a 改过的 VC-4 总控以 B3-04a 开工时的提交
（``B304A_BASELINE_COMMIT``）为新基线（``LATER_SHELL_BASELINES``）。本模块读真实仓库的 git 历史，按
``unit_records.GIT_READERS`` 登记。
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import importlib.util
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_orchestrator as eo
from tools.ci import entry_steps as es
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests.test_ci_entry_steps import Fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_COMMIT = "f28c0250a"
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
# 新增可选键的 unset：B5a-06 的四个通用键，B5a-09 的 UPSTREAM_IP（Claude 场景清单的必需 Campaign 变量，Codex 参数文件不写）。
NEW_OPTIONAL_UNSETS = "".join(f"unset {key}\n" for key in ("CLIENT", "CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID", "UPSTREAM_IP"))


def git_source(relative: str, commit: str = BASELINE_COMMIT) -> bytes | None:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{commit}:{relative}"],
                              capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def load_baseline(relative: str, name: str, *, siblings: dict[str, types.ModuleType] | None = None) -> types.ModuleType | None:
    """从 git 历史读出改造前的模块源码，作为独立模块对象加载：``__file__`` 指向仓库里的同一位置，加载期间临时登记在
    ``sys.modules``（dataclass 按模块名回查）并按需替换同目录的兄弟模块（``siblings``），结束后恢复。"""

    source = git_source(relative)
    if source is None:
        return None
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / relative)
    with mock.patch.dict(sys.modules, {name: module, **(siblings or {})}):
        exec(compile(source, f"{BASELINE_COMMIT}:{relative}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


def load_current(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


def _main_output(module: types.ModuleType, path: Path) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = module.main(["parse_env.py", str(path)])
    return rc, out.getvalue()


class ParseEnvCodexParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = load_baseline("tools/arm64_capture_driver/driver/parse_env.py", "parse_env_before_b5a06")
        if self.old is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的参数解析器（浅克隆或不在仓库内）")
        self.new = load_current(DRIVER / "parse_env.py", "parse_env_after_b5a06")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.fixture = driver_tests._DriverFixture(self.root)

    def _old_template(self) -> str:
        text = git_source("tools/arm64_capture_driver/driver/env.example.sh").decode("utf-8")
        for key in ("REPLACE_APPROVED_PROFILE_SHA256", "REPLACE_OFFICIAL_CODEX_SHA256", "REPLACE_OFFICIAL_PACKAGE_SHA256", "REPLACE_KILO_SHA256"):
            text = text.replace(key, "a" * 64)
        text = text.replace("REPLACE_CODEX_ACCOUNT_ID", "91").replace("REPLACE_API_KEY_ID", "92")
        (self.root / "docker" / "capture-cli" / "data").mkdir(parents=True, exist_ok=True)
        return text.replace("/root/", f"{self.root}/")

    def assert_same(self, text: str, label: str) -> None:
        old_values, new_values = self.old.parse(text), self.new.parse(text)
        self.assertEqual(list(new_values.items()), list(old_values.items()), f"{label}：parse 逐字相同（含键的先后）")
        old_derived, new_derived = self.old.derive(old_values), self.new.derive(new_values)
        self.assertEqual(list(new_derived.items()), list(old_derived.items()), f"{label}：derive 逐字相同（含键的先后）")
        path = self.root / f"{label}.sh"
        path.write_text(text, encoding="utf-8")
        old_rc, old_out = _main_output(self.old, path)
        new_rc, new_out = _main_output(self.new, path)
        self.assertEqual((new_rc, old_rc), (0, 0))
        self.assertEqual(new_out, old_out + NEW_OPTIONAL_UNSETS, f"{label}：主程序只在末尾多出新增可选键的 unset")

    def test_codex_parse_derive_and_main_output_match_pre_b5a06(self) -> None:
        self.assert_same(self.fixture.env_file.read_text(encoding="utf-8"), "fixture")
        self.assert_same(self._old_template(), "old-template")
        # 带上全部可选键（覆盖默认布局的写法）：可选键原样并进派生。
        extra = self.fixture.env_file.read_text(encoding="utf-8") + "".join(f'{key}="{value}"\n' for key, value in {
            "BASELINE_SOURCE": "/src/base", "TARGET_SOURCE": "/src/target", "TARGET_PACKAGE": "/pkg/x.tar.gz",
            "TARGET_CODE_MODE_HOST_SHA256": "e" * 64, "CAPTURE_RUNTIME_IMAGE": "ghcr.io/x@sha256:" + "f" * 64,
            "ENTRY_COMMIT": "1" * 40, "ENTRY_BRANCH": "codex/x", "P0_ROLLBACK_EVIDENCE": "/x/rollback.json",
            "EVIDENCE_DECISION": "reuse", "VC4_BUILD_NETWORK": "host"}.items())
        self.assert_same(extra, "optional")

    def test_codex_rejections_are_word_for_word(self) -> None:
        source = self.fixture.env_file.read_text(encoding="utf-8")
        cases = {
            "缺二进制": re.sub(r"^CODEX_BIN=.*\n", "", source, flags=re.M),
            "缺账号": re.sub(r"^CODEX_ACCOUNT_ID=.*\n", "", source, flags=re.M),
            "缺 Kilo": re.sub(r"^KILO_SHA256=.*\n", "", source, flags=re.M),
            "二进制摘要非法": re.sub(r"^CODEX_BIN_SHA256=.*$", "CODEX_BIN_SHA256=bad", source, flags=re.M),
            "Kilo 摘要非法": re.sub(r"^KILO_SHA256=.*$", "KILO_SHA256=bad", source, flags=re.M),
            "账号非法": re.sub(r"^CODEX_ACCOUNT_ID=.*$", "CODEX_ACCOUNT_ID=0", source, flags=re.M),
            "版本非法": re.sub(r"^TARGET_VERSION=.*$", "TARGET_VERSION=latest", source, flags=re.M),
            "画像不一致": re.sub(r"^TARGET_PROFILE_ID=.*$", "TARGET_PROFILE_ID=other", source, flags=re.M),
            "坐标越界": re.sub(r"^NEW=.*$", "NEW=../escape", source, flags=re.M),
            "未知键": source + "EXTRA_KEY=1\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(self.old.EnvFileError) as old_error:
                    self.old.parse(text)
                with self.assertRaises(self.new.EnvFileError) as new_error:
                    self.new.parse(text)
                self.assertEqual(str(new_error.exception), str(old_error.exception))

    def test_generic_keys_are_aliases_of_the_codex_keys(self) -> None:
        """「旧键作别名」：Codex 参数文件用通用键写目标客户端二进制、摘要与账号，解析结果与用旧键写的逐字相同（归一成旧键）。"""

        source = self.fixture.env_file.read_text(encoding="utf-8")
        generic = source
        for old, new in (("CODEX_BIN_SHA256", "CLIENT_BIN_SHA256"), ("CODEX_BIN", "CLIENT_BIN"), ("CODEX_ACCOUNT_ID", "CLIENT_ACCOUNT_ID")):
            generic = re.sub(rf"^{old}=", f"{new}=", generic, flags=re.M)
        self.assertNotIn("CODEX_BIN=", generic)
        self.assertEqual(list(self.new.parse(generic).items()), list(self.old.parse(source).items()))
        both = source + 'CLIENT_BIN="/opt/codex-0.156.1/bin/codex"\n'
        self.assertEqual(self.new.parse(both), self.old.parse(source), "通用键与旧键同值两处都写也可以")
        with self.assertRaisesRegex(self.new.EnvFileError, "别名"):
            self.new.parse(source + 'CLIENT_BIN="/opt/other/codex"\n')


def _without_clock(result: dict) -> dict:
    return {key: value for key, value in result.items() if key not in {"evaluated_at_utc", "started_at_utc", "completed_at_utc", "run_dir"}}


class EntryStepsCodexParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = load_baseline("tools/ci/entry_steps.py", "entry_steps_before_b5a06")
        if self.old is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的入口步骤（浅克隆或不在仓库内）")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = Fixture(Path(directory.name).resolve())

    def test_codex_step_table_and_gate_inputs_match_pre_b5a06(self) -> None:
        self.assertEqual([step.spec() for step in es.STEPS], [step.spec() for step in self.old.STEPS])
        self.assertEqual([step.spec_sha256() for step in es.STEPS], [step.spec_sha256() for step in self.old.STEPS],
                         "输入声明摘要不变：改造前写下的步骤记录照常沿用")
        self.assertEqual([step.spec() for step in es.STEPS_BY_CLIENT["codex"]], [step.spec() for step in self.old.STEPS])
        as_json = lambda table: {key: [item.as_json() for item in value] for key, value in table.items()}  # noqa: E731
        self.assertEqual(as_json(es.GATE_INPUTS), as_json(self.old.GATE_INPUTS))
        self.assertEqual(as_json(es.GATE_INPUTS_BY_CLIENT["codex"]), as_json(self.old.GATE_INPUTS))
        self.assertEqual([item.as_json() for item in es.pre_a3_data_root("codex")], [item.as_json() for item in self.old.PRE_A3_DATA_ROOT])
        self.assertEqual([item.as_json() for item in es.GUIDES], [item.as_json() for item in self.old.GUIDES])
        for name in ("PREFLIGHT_REQUIRED", "PLAN_PARAMS", "JOB_CONTAINERS", "DEPLOY_CONSISTENCY_PARTS", "DEPLOY_RECEIPT_GLOB",
                     "DEPLOY_RECEIPT_GLOBS", "DEPLOY_RECEIPT_STAMP_SEPARATORS", "DEPLOY_RECEIPT_IDENTITY_KEYS"):
            with self.subTest(name):
                self.assertEqual(getattr(es, name), getattr(self.old, name))

    def _ctx(self, module: types.ModuleType) -> object:
        return module.Context(params=dict(self.fx.params), driver_dir=self.fx.driver, steps_dir=self.fx.steps_dir, runner=self.fx.runner)

    def _record_all(self, module: types.ModuleType) -> None:
        ctx = self._ctx(module)
        for step in module.STEPS:
            if not step.live:
                module.begin(step, ctx)
                module.finish(step, ctx, status="passed", products=self.fx.products.get(step.step_id, {}))

    def assert_evaluations_match(self, label: str, **options: object) -> dict:
        old = _without_clock(self.old.evaluate(self._ctx(self.old), is_formal_built=False, **options))
        new = _without_clock(es.evaluate(self._ctx(es), is_formal_built=False, **options))
        self.assertEqual(new, old, label)
        return new

    def test_codex_evaluate_matches_pre_b5a06(self) -> None:
        self.assert_evaluations_match("没有步骤记录")
        self._record_all(self.old)
        reused = self.assert_evaluations_match("改造前写下的记录")
        self.assertEqual({step["decision"] for step in reused["steps"]}, {"reuse", "live"}, "改造前的记录在改造后照常沿用")
        shutil.rmtree(self.fx.steps_dir)
        self._record_all(es)
        self.assert_evaluations_match("改造后写下的记录")
        self.assert_evaluations_match("强制重做入口门禁", force={"entry-gates": "验收：强制重做"})
        self.assertEqual(_without_clock(self.old.evaluate(self._ctx(self.old), is_formal_built=True)),
                         _without_clock(es.evaluate(self._ctx(es), is_formal_built=True)), "Formal 已建")
        # 逐类改动输入：参数、指南第二部分、驱动文件、部署收据、受管五摘要；每次改动后两边判定仍逐字相同。
        mutations = (
            ("参数", lambda: self.fx.params.update(STAGE_BUDGETS="VC-0=180")),
            ("指南第二部分", lambda: self.fx.guide.write_text(self.fx.guide.read_text(encoding="utf-8").replace("第二部分正文", "第二部分改了"),
                                                              encoding="utf-8")),
            ("驱动文件", lambda: (self.fx.driver / "entry-gates.sh").write_text("# 驱动改了\n", encoding="utf-8")),
            ("部署收据", lambda: self.fx.deploy(stamp="20261003t000000z")),
            ("受管五摘要", lambda: self.fx.identity.update(control_sha256="9" * 64)),
        )
        for label, mutate in mutations:
            mutate()
            self.assert_evaluations_match(f"改动：{label}")

    def test_formal_built_probe_matches_pre_b5a06(self) -> None:
        new = self.fx.params["NEW"]
        (self.fx.data / "evidence" / "campaigns" / new).mkdir(parents=True)
        (self.fx.data / "evidence" / "campaigns" / new / "campaign.json").write_text("{}\n", encoding="utf-8")
        for site in ({"site": "formal-built"}, {"site": "inconsistent", "formal": {"replayable": False}}, {"site": "none"}):
            with self.subTest(site=site["site"]):
                self.fx.formal_site = site
                self.assertEqual(es.formal_built(self._ctx(es)), self.old.formal_built(self._ctx(self.old)))


class EntryPlanCodexParityTests(unittest.TestCase):
    """``entry.sh --plan`` 就是入口编排器的只判定：同一夹具上改造前后的结果与打印的判定表逐字相同。"""

    def setUp(self) -> None:
        old_steps = load_baseline("tools/ci/entry_steps.py", "entry_steps_before_b5a06_plan")
        self.old = load_baseline("tools/ci/entry_orchestrator.py", "entry_orchestrator_before_b5a06",
                                 siblings={"entry_steps_sibling": old_steps} if old_steps else None)
        if self.old is None or old_steps is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的入口编排器（浅克隆或不在仓库内）")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.fx = Fixture(self.root)
        self.fx.params.update({"RUNROOT": str(self.root / "run"), "ENTRY_BUNDLE": str(self.root / "x.bundle"),
                               "ENTRY_BRANCH": "codex/test"})
        self.runs = 0

    def _plan(self, module: types.ModuleType) -> tuple[dict, str]:
        self.runs += 1
        run_dir = self.root / "runs" / str(self.runs)

        def no_execution(argv, cwd, env, log):  # 只判定不执行：编排器的命令一条都不该跑
            raise AssertionError(f"--plan 执行了命令：{argv}")

        orchestrator = module.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=run_dir, steps_dir=self.fx.steps_dir,
                                           runner=no_execution, es_runner=self.fx.runner)
        result = orchestrator.orchestrate(plan_only=True)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            module.print_summary(result)
        return _without_clock(result), printed.getvalue().replace(str(run_dir), "<run>")

    def assert_plans_match(self, label: str) -> None:
        old_result, old_table = self._plan(self.old)
        new_result, new_table = self._plan(eo)
        self.assertEqual(new_result, old_result, label)
        self.assertEqual(new_table, old_table, f"{label}：打印的判定表逐字相同")

    def test_codex_plan_table_matches_pre_b5a06(self) -> None:
        self.assert_plans_match("没有步骤记录")
        self.fx.record_all()
        self.assert_plans_match("全部记录沿用")
        self.fx.params["STAGE_BUDGETS"] = "VC-0=180"
        self.assert_plans_match("账本输入变了（阻塞）")
        (self.fx.driver / "pre-a3.sh").write_text("# 改了\n", encoding="utf-8")
        self.assert_plans_match("pre-A3 驱动变了")
        formal = self.fx.data / "evidence" / "campaigns" / self.fx.params["NEW"]
        formal.mkdir(parents=True)
        (formal / "campaign.json").write_text("{}\n", encoding="utf-8")
        self.assert_plans_match("Formal 已建")


# ------------------------------------------------------------------------------------------------ 驱动脚本的逐行对照

def _codex_shell_values() -> dict[str, object]:
    values: dict[str, object] = {}
    for line in es.CT.shell_lines("codex"):
        name, _, raw = line.partition("=")
        values[name] = tuple(shlex.split(raw[1:-1])) if raw.startswith("(") else (shlex.split(raw) or [""])[0]
    return values


def _literal(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:@%+=,-]+", value) else shlex.quote(value)


def codex_effective(text: str, values: dict[str, object]) -> str:
    """改动后脚本代入 Codex 的客户端表取值：追加的数组参数为空时连同前面的空格一起去掉，其余按字面代入。"""

    def array(match: re.Match[str]) -> str:
        items = values[match.group(1)]
        return (" " + " ".join(_literal(item) for item in items)) if items else ""

    text = re.sub(r' \$\{(CT_[A-Z0-9_]+)\[@\]\+"\$\{\1\[@\]\}"\}', array, text)
    text = re.sub(r'"\$\{(CT_[A-Z0-9_]+)\[@\]\}"', lambda m: " ".join(_literal(item) for item in values[m.group(1)]), text)
    text = re.sub(r'"\$(CT_[A-Z0-9_]+)"', lambda m: _literal(values[m.group(1)]) if values[m.group(1)] else '""', text)
    return re.sub(r'\$\{?(CT_[A-Z0-9_]+)\}?', lambda m: str(values[m.group(1)]), text)


def baseline_codex_effective(text: str) -> str:
    """基线里 B5a-04 的 pre-A3 客户端参数：只在 CLIENT 不是 codex 时有值，Codex 下为空（两行设置与追加处一并去掉）。"""

    text = text.replace(' ${PA3_CLIENT_ARGS[@]+"${PA3_CLIENT_ARGS[@]}"}', "")
    return "".join(line for line in text.splitlines(keepends=True)
                   if not line.startswith(("PA3_CLIENT_ARGS=()", 'if [ "${CLIENT:-codex}" != codex ]')))


def residual(old: str, new: str) -> list[str]:
    code = lambda text: [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]  # noqa: E731
    return [line for line in difflib.unified_diff(code(old), code(new), lineterm="", n=0) if not line.startswith(("---", "+++", "@@"))]


# B5a-06 改动后各驱动脚本的内容摘要：脚本之后又改过就跳过逐行对照（以新的基线提交重新对照）。
SHELL_DIGESTS: dict[str, str] = {
    "entry-gates.sh": "63b2f897b5103d8aa23a276a4727355dabe6b9dd76238aee1ecd5802b468e832",
    "entry-preflight.sh": "b18f8106f495a6893ddccf94c453213a927f00981d7a681dbd22ccbbdcf0edda",
    "guard.sh": "23ef1f8cbd4785872793fcbb28a87ef70146596a5d2eb026866abd003cac6a4d",
    "vc-batch.sh": "24269505c7e8a783563a06734b728f653422c02990d7eb3a9373783c35124c3f",
    "vc5-accept.sh": "360d3a59c1040883426c662cc4f658fd84ab5236f1ec510a23b6d988716031c5",
    "vc5-precheck.sh": "226e9a93763b1d7c149f6cb1b0bf12efba24d51dc9c142c9e4b2d2b61d9ecb75",
    "lib.sh": "f085a20cead4a844b84148d56adb522d9a2a93687fb0dc8bb2ccffeba6ca5174",
    "fix-and-continue.sh": "5935d4c70f64979435bee6a626caad04ef94babcf2b604b06bd80a91a5de622c",
}
# 代入 Codex 取值之后仍与基线不同的非注释行（- 基线，+ 改动后），每处都与改造前等价：
# * entry-gates.sh 部署绑定：install.py 的 latest_deploy_receipt 多传客户端（Codex 即缺省值 codex）；current_identity 以
#   位置参数接客户端（Codex 不传参数，调用与改造前相同）；部署收据的身份对象按客户端取（Codex 是收据顶层，取空串即顶层）；
# * entry-preflight.sh：必填键改为遍历客户端表（Codex 的键与顺序相同）；部署收据与五摘要同上；目标客户端的二进制与摘要
#   经客户端表的参数键间接取（Codex 即 CODEX_BIN／CODEX_BIN_SHA256）；plan-audit 的待接入判断对 Codex 恒不成立；
# * guard.sh／vc5-precheck.sh：总账、编排器与计时账本模块经 importlib 按客户端表取（Codex 即原来导入的那几个模块）；
# * lib.sh：解析参数之后加载客户端表（只赋值不导出）；
# * fix-and-continue.sh：仓库文档与部署范围由判定 deploy-state 按纳管状态给出（只有 Codex 纳管时文档是同样两份、范围参数
#   为空，部署命令的参数与改造前相同）。
SHELL_RESIDUALS: dict[str, list[str]] = {
    "entry-gates.sh": [
        '-  DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" <<\'PYDEPLOY\'',
        '+  DEPLOY=$(python3 -B - "$DRV/../install.py" "$D" codex <<\'PYDEPLOY\'',
        "-print(module.latest_deploy_receipt(Path(sys.argv[2])/'control')[0])",
        "+print(module.latest_deploy_receipt(Path(sys.argv[2])/'control', sys.argv[3])[0])",
        '-    "from tools.official_client_capture import codex_upgrade_policy_certification as pc; print(pc.current_identity()[\'tool_files_sha256\'])")',
        '-  DEPLOY_FILES=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[\'tool_files_sha256\'])" "$DEPLOY")',
        '+    "import sys; from tools.official_client_capture import codex_upgrade_policy_certification as pc; print(pc.current_identity(*sys.argv[1:])[\'tool_files_sha256\'])" \\',
        '+   )',
        '+  DEPLOY_FILES=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print((d[sys.argv[2]] if sys.argv[2] else d)[\'tool_files_sha256\'])" \\',
        '+    "$DEPLOY" "")',
    ],
    "entry-preflight.sh": [
        '-  for key in TARGET_CODE_MODE_HOST_SHA256 CAPTURE_RUNTIME_IMAGE CODEX_BIN CODEX_BIN_SHA256 OFFICIAL_ASSET_SHA256 \\',
        '-             TARGET_PACKAGE TARGET_SOURCE BASELINE_SOURCE ACTIVE_PROFILE MAIN_MODEL LITE_MODEL CODEX_ACCOUNT_ID API_KEY_ID \\',
        '-             COMPOSE_DIR PROJECT_DEADLINE_UTC STAGE_BUDGETS POLICY_COMPAT_RECEIPT POLICY_ACTIVATION PRE_A3_CERTIFICATION \\',
        '-             P0_ROLLBACK_EVIDENCE; do',
        '+  for key in TARGET_CODE_MODE_HOST_SHA256 CAPTURE_RUNTIME_IMAGE CODEX_BIN CODEX_BIN_SHA256 OFFICIAL_ASSET_SHA256 TARGET_PACKAGE TARGET_SOURCE BASELINE_SOURCE ACTIVE_PROFILE MAIN_MODEL LITE_MODEL CODEX_ACCOUNT_ID API_KEY_ID COMPOSE_DIR PROJECT_DEADLINE_UTC STAGE_BUDGETS POLICY_COMPAT_RECEIPT POLICY_ACTIVATION PRE_A3_CERTIFICATION P0_ROLLBACK_EVIDENCE; do',
        '-  python3 -B - "$DRV/../install.py" "$D" <<\'PY\'',
        '+  python3 -B - "$DRV/../install.py" "$D" codex <<\'PY\'',
        '-print(module.latest_deploy_receipt(Path(sys.argv[2]) / "control")[0])',
        '+print(module.latest_deploy_receipt(Path(sys.argv[2]) / "control", sys.argv[3])[0])',
        '-identity = certification.current_identity()',
        '-receipt = certification.load_deployment_receipt(Path(sys.argv[1]), expected_identity=identity)',
        '+client = sys.argv[2:]',
        '+identity = certification.current_identity(*client)',
        '+receipt = certification.load_deployment_receipt(Path(sys.argv[1]), expected_identity=identity, **({"client": client[0]} if client else {}))',
        '-  local host container version',
        '-  host=$(sha256sum "$CODEX_BIN" | cut -c1-64) || return 1',
        '-  container=$(docker exec capture-cli sha256sum "$CODEX_BIN" | cut -c1-64) || return 1',
        '-  version=$("$CODEX_BIN" --version 2>&1 | head -n 1) || return 1',
        '-  echo "宿主机 ${host}；采集容器 ${container}；登记 ${CODEX_BIN_SHA256}；版本 ${version}"',
        '-  [ "$host" = "$CODEX_BIN_SHA256" ] && [ "$container" = "$CODEX_BIN_SHA256" ] && [[ $version == *"$TARGET_VERSION"* ]]',
        '+  local host container version binary=${!CT_BINARY_KEY} expected=${!CT_BINARY_SHA256_KEY}',
        '+  host=$(sha256sum "$binary" | cut -c1-64) || return 1',
        '+  container=$(docker exec capture-cli sha256sum "$binary" | cut -c1-64) || return 1',
        '+  version=$("$binary" --version 2>&1 | head -n 1) || return 1',
        '+  echo "宿主机 ${host}；采集容器 ${container}；登记 ${expected}；版本 ${version}"',
        '+  [ "$host" = "$expected" ] && [ "$container" = "$expected" ] && [[ $version == *"$TARGET_VERSION"* ]]',
        '+  if [ -n "" ]; then echo "本轮客户端（codex）的 plan-audit 尚未接入："; return 1; fi',
    ],
    "guard.sh": [
        '-import sys, json',
        '-from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger',
        '+import importlib, sys, json',
        '+project_ledger = importlib.import_module(sys.argv[2])',
        '-" "${TARGET_VERSION:-}"',
        '+" "${TARGET_VERSION:-}" tools.official_client_capture.codex_upgrade_project_ledger',
        '-  python3 - "$CAMPAIGN" "$MODE" <<\'PY\'',
        '-import sys',
        '+  python3 - "$CAMPAIGN" "$MODE" tools.official_client_capture.codex_upgrade tools.official_client_capture.codex_upgrade_timing_ledger <<\'PY\'',
        '+import importlib, sys',
        '-from tools.official_client_capture import codex_upgrade as cu',
        '-from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger',
        '+cu = importlib.import_module(sys.argv[3])',
        '+timing_ledger = importlib.import_module(sys.argv[4])',
    ],
    "vc5-precheck.sh": [
        '-python3 - "$NEWDIR" "$CAND" "$B/source" "$IMAGE_ID" "$BUILD_ID" "$PROFILE_ID" "$PROFILE_DIGEST" <<\'PY\'',
        '-import argparse, json, sys, os',
        '+python3 - "$NEWDIR" "$CAND" "$B/source" "$IMAGE_ID" "$BUILD_ID" "$PROFILE_ID" "$PROFILE_DIGEST" tools.official_client_capture.codex_upgrade <<\'PY\'',
        '+import argparse, importlib, json, sys, os',
        '-from tools.official_client_capture import codex_upgrade as cu',
        '-NEW, CAND, SRC, IMAGE_ID, BUILD_ID, PROFILE_ID, PROFILE_DIGEST = sys.argv[1:]',
        '+NEW, CAND, SRC, IMAGE_ID, BUILD_ID, PROFILE_ID, PROFILE_DIGEST, ORCHESTRATOR = sys.argv[1:]',
        '+cu = importlib.import_module(ORCHESTRATOR)',
    ],
    "lib.sh": [
        '+CLIENT_TABLE_ASSIGNMENTS=$(python3 -B "$DRV/client_table.py" shell) || { echo "客户端表拒绝加载：CLIENT=${CLIENT:-}"; exit 2; }',
        '+eval "$CLIENT_TABLE_ASSIGNMENTS"; unset CLIENT_TABLE_ASSIGNMENTS',
    ],
    "fix-and-continue.sh": [
        '+    local -a repository_docs scope_args',
        '+    read -r -a repository_docs <<< "$REPOSITORY_DOCS"',
        '+    read -r -a scope_args <<< "$DEPLOY_SCOPE"',
        '-      && cp -p "$ST/docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md" "$ST/docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md" "$ST/docs/repository-docs/" \\',
        '+      && for doc in ${repository_docs[@]+"${repository_docs[@]}"}; do cp -p "$ST/docs/$doc" "$ST/docs/repository-docs/" || exit 1; done \\',
        '-    setsid -f bash -c \'echo "$$" > "$2"; cd "$0" && python3 tools/arm64_supervised_deploy.py --staging-root "$0" --production-root "$3/tools/official_client_capture" --production-doc-root "$3/docs" --control-root "$3/control" > "$1" 2>&1; echo "exit=$?" >> "$1"\' \\',
        '-      "$ST" "$LOG" "$PIDF" "$D" < /dev/null > /dev/null 2>&1 9>&-',
        '+    setsid -f bash -c \'echo "$$" > "$2"; cd "$0" && python3 tools/arm64_supervised_deploy.py --staging-root "$0" --production-root "$3/tools/official_client_capture" --production-doc-root "$3/docs" --control-root "$3/control" "${@:4}" > "$1" 2>&1; echo "exit=$?" >> "$1"\' \\',
        '+      "$ST" "$LOG" "$PIDF" "$D" ${scope_args[@]+"${scope_args[@]}"} < /dev/null > /dev/null 2>&1 9>&-',
    ],
}


class DriverScriptCodexParityTests(unittest.TestCase):
    def test_scripts_with_codex_values_match_pre_b5a06(self) -> None:
        values = _codex_shell_values()
        names = ("entry-gates.sh", "entry-preflight.sh", "guard.sh", "vc-batch.sh", "vc5-accept.sh", "vc5-precheck.sh", "lib.sh",
                 "fix-and-continue.sh")
        self.assertEqual(sorted(SHELL_DIGESTS), sorted(names))
        for name in names:
            with self.subTest(name):
                text = (DRIVER / name).read_text(encoding="utf-8")
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != SHELL_DIGESTS[name]:
                    self.skipTest(f"{name} 在 B5a-06 之后又改过：以新的基线提交重新对照")
                source = git_source(f"tools/arm64_capture_driver/driver/{name}")
                if source is None:
                    self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的 {name}")
                self.assertEqual(residual(baseline_codex_effective(source.decode("utf-8")), codex_effective(text, values)),
                                 SHELL_RESIDUALS.get(name, []))


# ------------------------------------------------------------------------------------------------ B5a-09 新基线

# B5a-09 改了入口便宜检查（上面 B5a-06 的逐行对照因此跳过）：以 B5a-09 开工时的提交为新基线，代入 Codex 取值后再逐行对照一次。
# 只剩下面登记的几处，每处 Codex 下都走不到或与基线等价：
# * target-client：另核容器的循环——Codex 的另核容器表为空，循环体一次不执行，判定只多一个恒为真的条件；
# * official-package：冻结复核分支——Codex 没有官方包复核模块（空串），分支不进，源码树核对那一行逐字不变；
# * plan-audit：按参数模板展开的函数与分支——Codex 的模板为空，走原来的参数形状（那一段逐字不变）；删掉的是 B5a-06 留下的待接入
#   判断（Codex 恒不成立）；退出码变量改成先声明、两个分支各自赋值（取值不变）。
# 基线脚本引用的待接入变量 CT_PENDING_CHECK_PLAN_AUDIT 是 Claude 待接入条目的 shell 视图（Codex 为空串），B5a-09 落实后表里不再
# 输出它，对照时按 Codex 的空串代入。
B5A09_BASELINE_COMMIT = "5ad660473"
B5A09_SHELL_DIGESTS: dict[str, str] = {
    "entry-preflight.sh": "762a64e1ca33bf25e84e44711497717ce0a8a3b530730306946d506a225c8cc4",
}
B5A09_SHELL_RESIDUALS: dict[str, list[str]] = {
    "entry-preflight.sh": [
        '-  local host container version binary=${!CT_BINARY_KEY} expected=${!CT_BINARY_SHA256_KEY}',
        '+  local host container version binary=${!CT_BINARY_KEY} expected=${!CT_BINARY_SHA256_KEY} name extra mismatched=0',
        '-  [ "$host" = "$expected" ] && [ "$container" = "$expected" ] && [[ $version == *"$TARGET_VERSION"* ]]',
        '+  for name in; do',
        '+    extra=$(docker exec "$name" sha256sum "$binary" | cut -c1-64) || return 1',
        '+    echo "${name} ${extra}"',
        '+    [ "$extra" = "$expected" ] || mismatched=1',
        '+  done',
        '+  [ "$host" = "$expected" ] && [ "$container" = "$expected" ] && [[ $version == *"$TARGET_VERSION"* ]] && [ "$mismatched" = 0 ]',
        '+  if [ -n "" ]; then',
        '+    python3 -m "" verify --freeze "$TARGET_PACKAGE" --target-version "$TARGET_VERSION" \\',
        '+      --binary-sha256 "${!CT_BINARY_SHA256_KEY}" --freeze-sha256 "$OFFICIAL_ASSET_SHA256"',
        '+    return',
        '+  fi',
        '+plan_audit_from_table() {',
        '+  local item name argv=()',
        '+  for item in ; do',
        '+    if [[ $item == \\$* ]]; then name=${item#\\$}; argv+=("${!name:-}"); else argv+=("$item"); fi',
        '+  done',
        '+  python3 -m tools.official_client_capture.codex_upgrade plan --audit-only \\',
        '+    --campaign-dir "$D/staging/entry-preflight-$(date -u +%Y%m%dt%H%M%Sz)/campaign" --campaign-id "${CAMPAIGN_PREFIX}-plan-audit" "${argv[@]}"',
        '+}',
        '-  if [ -n "" ]; then echo "本轮客户端（codex）的 plan-audit 尚未接入："; return 1; fi',
        '+  local rc',
        '+  if [ "${#CT_PLAN_AUDIT_ARGV[@]}" != 0 ]; then',
        '+    plan_audit_from_table > "$OUT/plan-audit.json"; rc=$?',
        '+  else',
        '-  local rc=$?',
        '+  rc=$?',
        '+  fi',
    ],
}


# B5a-10 改了批次派发、VC-5 验收与入口门禁三个脚本（上面 B5a-06／B5a-08 的逐行对照因此跳过）：以 B5a-10 开工时的提交为新基线，
# 代入 Codex 取值后再逐行对照。vc-batch.sh 与 vc5-accept.sh 只给后台验证的批次边界与 VC-5 前核对追加客户端参数（Codex 为空数组，
# 代入后与基线逐字相同）；entry-gates.sh 只多下面登记的几处，每处 Codex 下都等价：
# * 用法与初始化多了 --require-deployed-all 与它的三个变量（不给这个开关时恒为假或空）；
# * 参数解析多一个分支（只认 --require-deployed-all）；
# * 新增 check_deployed_others 与调用它的分支（只在给了 --require-deployed-all 时执行；只有 Codex 纳管的数据根上它也不核对
#   任何别的客户端）；
# * 导出门禁记录的来源多一个数组（没给开关时为空，命令行与基线相同）。
B5A10_BASELINE_COMMIT = "3cf9c165e"
B5A10_SHELL_DIGESTS: dict[str, str] = {
    'vc-batch.sh': 'a94b32137e9efff2a24053392938afab73304ced10e3a238d948a0d1cb59e1e3',
    'vc5-accept.sh': 'bfe7d7c8bbbc3abc326edeb4e92d06ded6effb79000bf5be6c8b53319feecf00',
    'entry-gates.sh': 'bbc1cd02625ba8fba74efcda74af7c19847a58bfdfa878631d766b9a5d2fa11e',
}
B5A10_SHELL_RESIDUALS: dict[str, list[str]] = {
    'vc-batch.sh': [],
    'vc5-accept.sh': [],
    'entry-gates.sh': [
        '-usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--with-gates <门禁项,…>] [--require-deployed] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
        '+usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
        '-WITH_GATES=""; REQUIRE_DEPLOYED=false; WAIT_LOCK=0',
        '+WITH_GATES=""; REQUIRE_DEPLOYED=false; WAIT_LOCK=0; REQUIRE_DEPLOYED_ALL=false; DEPLOY_OTHERS=""; DEPLOY_OTHERS_SOURCE=()',
        '+    --require-deployed-all) REQUIRE_DEPLOYED=true; REQUIRE_DEPLOYED_ALL=true; shift ;;',
        '+check_deployed_others() {',
        '+  if ! DEPLOY_OTHERS=$(python3 -B "$DRV/entry_steps.py" deploy-check --tree "$TREE" --data-root "$D" --except codex --pycache "$PYC"); then',
        '+    echo "其余已纳管客户端的部署不是本提交：${DEPLOY_OTHERS}：先受监督部署本提交（交换共享根的部署带上全部已纳管客户端），再跑入口门禁"',
        '+    echo "ENTRY_GATES_ABORTED：部署与测试树不一致，没有门禁结论；主体目录 ${OUT}"',
        '+    exit 3',
        '+  fi',
        '+  DEPLOY_OTHERS_SOURCE=(--source "deploy_receipts_others=$DEPLOY_OTHERS")',
        '+}',
        '+  if [ "$REQUIRE_DEPLOYED_ALL" = true ]; then',
        '+    check_deployed_others',
        '+    echo "其余已纳管客户端的部署也是本提交：${DEPLOY_OTHERS:-（没有其余已纳管客户端）}"',
        '+  fi',
        '-  --source "deploy_receipt=$DEPLOY" | cut -c1-600',
        '+  --source "deploy_receipt=$DEPLOY" ${DEPLOY_OTHERS_SOURCE[@]+"${DEPLOY_OTHERS_SOURCE[@]}"} | cut -c1-600',
    ],
}
# B3-04a（Claude 版候选链驱动）改了 VC-4 总控：以 B3-04a 开工时的提交为基线，最后一步的派发脚本改取客户端表
# vc4_dispatch_script（Codex 即 vc4.sh），代入 Codex 取值后非注释行与基线逐行相同，没有残差。Claude 专用的 vc4-requirements.sh、
# vc4-claude.sh 与本机 local-claude-candidate-chain.sh 是新文件，Codex 流程不经过它们。
B304A_BASELINE_COMMIT = "8015c1a78"
B304A_SHELL_DIGESTS: dict[str, str] = {
    "vc4-all.sh": "bd8faf3250a7e093d83b1dcdf11165014fcc684cfe725adc30d25136c2a051de",
}
B304A_SHELL_RESIDUALS: dict[str, list[str]] = {"vc4-all.sh": []}
# 之后各批的新基线（批次、基线提交、改动后的脚本摘要、代入 Codex 取值后的残差）：某个脚本再改过时它那一项跳过，以更新的
# 一批重新对照。基线脚本引用的待接入变量 CT_PENDING_CHECK_PLAN_AUDIT 已不再由客户端表给出，按 Codex 的空串代入（见 B5a-09 一段）。
LATER_SHELL_BASELINES: tuple[tuple[str, str, dict[str, str], dict[str, list[str]]], ...] = (
    ("B5a-09", B5A09_BASELINE_COMMIT, B5A09_SHELL_DIGESTS, B5A09_SHELL_RESIDUALS),
    ("B5a-10", B5A10_BASELINE_COMMIT, B5A10_SHELL_DIGESTS, B5A10_SHELL_RESIDUALS),
    ("B3-04a", B304A_BASELINE_COMMIT, B304A_SHELL_DIGESTS, B304A_SHELL_RESIDUALS),
)


class DriverScriptLaterBatchCodexParityTests(unittest.TestCase):
    def test_scripts_with_codex_values_match_later_batch_starts(self) -> None:
        values = {**_codex_shell_values(), "CT_PENDING_CHECK_PLAN_AUDIT": ""}
        for batch, commit, digests, residuals in LATER_SHELL_BASELINES:
            self.assertEqual(sorted(digests), sorted(residuals), batch)
            for name, digest in digests.items():
                with self.subTest(batch=batch, script=name):
                    text = (DRIVER / name).read_text(encoding="utf-8")
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
                        self.skipTest(f"{name} 在 {batch} 之后又改过：以新的基线提交重新对照")
                    source = git_source(f"tools/arm64_capture_driver/driver/{name}", commit)
                    if source is None:
                        self.skipTest(f"读不到基线提交 {commit} 的 {name}")
                    self.assertEqual(residual(codex_effective(source.decode("utf-8"), values), codex_effective(text, values)),
                                     residuals[name])


# ------------------------------------------------------------------------------------------------ 安装脚本与其它模块

class InstallAndConstantsCodexParityTests(unittest.TestCase):
    def test_install_receipt_and_verify_match_pre_b5a06_when_only_codex_is_managed(self) -> None:
        old = load_baseline("tools/arm64_capture_driver/install.py", "install_before_b5a06")
        if old is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的安装脚本")
        new = driver_tests.driver
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            outcomes = []
            for label, module in (("old", old), ("new", new)):
                data = root / label / "data"
                control = data / "control"
                control.mkdir(parents=True)
                (control / "codex-01600-supervisor-enable-20261001t000000z.json").write_text(
                    json.dumps({"status": "passed", "tool_files_sha256": "a" * 64, "supervisor_sha256": "b" * 64}), encoding="utf-8")
                target = root / label / "arm64-capture-driver"
                with mock.patch.object(module, "_running_as_root", return_value=False), \
                        mock.patch.object(module, "_require_root", return_value=None), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(["install", "--source", str(REPO_ROOT / "tools/arm64_capture_driver"),
                                                  "--target", str(target), "--data-root", str(data)]), 0)
                    verified = module.verify_install(target, data)
                [receipt_path] = sorted(control.glob("arm64-capture-driver-install-*.json"))
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                outcomes.append(({key: value for key, value in receipt.items() if key not in {"installed_at_utc", "install_target", "receipt_sha256"}},
                                 list(receipt), {key: value for key, value in verified.items() if key not in {"install_target", "install_receipt"}}))
            self.assertEqual(outcomes[1], outcomes[0], "只有 Codex 纳管：安装收据（v1、字段与顺序）与复验结果和改造前相同")

    def test_receipt_rules_and_pre_a3_declarations_match_pre_b5a06(self) -> None:
        from tools.ci import background_validation as bv
        from tools.ci import entry_gates as eg
        old_bv = load_baseline("tools/ci/background_validation.py", "background_validation_before_b5a06")
        old_steps = load_baseline("tools/ci/entry_steps.py", "entry_steps_before_b5a06_gates")
        old_eg = load_baseline("tools/ci/entry_gates.py", "entry_gates_before_b5a06",
                               siblings={"entry_steps_sibling": old_steps} if old_steps else None)
        if old_bv is None or old_eg is None:
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT} 的后台验证或入口门禁")
        for name in ("DEPLOY_RECEIPT_GLOB", "DEPLOY_RECEIPT_GLOBS", "DEPLOY_RECEIPT_STAMP_SEPARATORS", "DEPLOY_RECEIPT_IDENTITY_KEYS",
                     "DEPLOY_CLIENTS"):
            with self.subTest(name):
                self.assertEqual(getattr(bv, name), getattr(old_bv, name))
        self.assertEqual(eg.PRE_A3_GATE_COMMANDS["codex"], old_eg.PRE_A3_GATE_COMMANDS["codex"])
        self.assertEqual(eg.PRE_A3_GATE_COMMANDS["claude"], old_eg.PRE_A3_GATE_COMMANDS["claude"])
        sample = "tools/official_client_capture/tests/test_codex_upgrade_pre_a3_certification.py"
        data_inputs = [{"category": "environment", "name": "tree:fixed", "sha256": "0" * 64, "detail": {}}]
        self.assertEqual(eg.pre_a3_inputs(sample, data_inputs), old_eg.pre_a3_inputs(sample, data_inputs))
        self.assertEqual(eg.pre_a3_inputs(sample, data_inputs, "codex"), old_eg.pre_a3_inputs(sample, data_inputs))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory).resolve()
            # 改造前的入口门禁按名字取同目录的入口步骤：对照时换成改造前那一份。
            with mock.patch.dict(sys.modules, {"entry_steps_sibling": old_steps}):
                before = [entry["name"] for entry in old_eg.pre_a3_data_root_inputs(data)]
            self.assertEqual([entry["name"] for entry in eg.pre_a3_data_root_inputs(data)], before)
            self.assertEqual([entry["name"] for entry in eg.pre_a3_data_root_inputs(data, "codex")], before)


if __name__ == "__main__":
    unittest.main()
