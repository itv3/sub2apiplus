"""B5a-06 验收：驱动链与入口基础设施按客户端参数化之后，Codex 的派生、判定与命令行与改造前逐字相同。

改造前的源码取自 B5a-06 开工时本分支的 HEAD（``BASELINE_COMMIT``，即 B5a-04 的提交），从真实仓库的 git 历史读出，作为独立
模块对象加载，与改造后的模块在同一组输入下各算一次：

* 参数解析（``parse_env.py``）：同一份 Codex 参数文件（驱动测试夹具与改造前的参数模板）``parse`` 与 ``derive`` 的结果逐字
  相同（含键的先后），主程序输出等于改造前的输出再加上新增可选键（B5a-06 四个、B5a-09 的 ``UPSTREAM_IP``、B2-11 第二部分
  Claude 预检 plan 的十一个）的 ``unset``（清掉外层环境残留的另一客户端参数）；
  一组非法改写的报错逐字相同；用通用键（``CLIENT_BIN`` 等）写的 Codex 参数文件与用旧键写的解析结果相同；
* 入口步骤（``entry_steps.py``）：Codex 步骤表的输入声明与摘要、门禁项输入清单逐字相同；同一夹具上无记录、改造前写的
  记录、改造后写的记录、逐类改动输入、Formal 已建与强制重做各情形下 ``evaluate`` 的结果逐字相同；
* 入口编排（``entry_orchestrator.py``，即 ``entry.sh --plan``）：同一夹具上只判定的结果与打印的判定表逐字相同；
* 驱动脚本：把改动后脚本里的客户端表变量（``CT_``）代入 Codex 的取值、基线里 B5a-04 的客户端参数按 Codex 取空之后，
  与改造前的非注释行逐行相同，只剩登记在 ``SHELL_RESIDUALS`` 的几处（每处写明为什么等价）；
* 安装脚本（``install.py``）：只有 Codex 纳管时安装收据与复验结果和改造前相同；后台验证、入口门禁、入口空跑沿用的
  部署收据规则与 pre-A3 声明和改造前相同。

基线提交读不到（浅克隆）时整条用例跳过（父级 ``skipTest``）并写明原因。驱动脚本的逐行对照按批登记：B5a-06 以改造前的
``BASELINE_COMMIT`` 为基线（``SHELL_DIGESTS``／``SHELL_RESIDUALS``），之后改过驱动脚本的各批以该批开工时的提交为新基线
（``LATER_SHELL_BASELINES``：B5a-09 的入口便宜检查、B5a-10 的批次派发／VC-5 验收／入口门禁、B3-04a 的 VC-4 总控）。5a 收口
（B5a-11）把对照基线统一换成收口时的主线头（``B5A11_BASELINE_COMMIT``）：九个驱动脚本都登记在这一批，基线就是当前内容，
残差为空；之后改动驱动脚本的批次照样在 ``LATER_SHELL_BASELINES`` 末尾追加一项。

某个脚本在某一批之后又改过（内容摘要不再是该批登记的那份）时，那一批对它的逐行对照不再执行，改为核对接管：它当前的内容
必须由另一批登记过（登记了当前摘要的那一批会逐行对照它），没有任何一批登记就失败——改了驱动脚本就要追加一批。子测试里
一律不跳过：子测试跳过时 unittest 既不报父用例、又多报子测试编号，入口门禁的全集核对因此失败（防回归见
``test_unit_inherit_codex_parity.ParitySkipDisciplineTests``）。改造前基线到收口基线的累计变化在收口时一次性核对过：每一次
改动都落在某一批登记的对照区间里。本模块读真实仓库的 git 历史，按 ``unit_records.GIT_READERS`` 登记。
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
# 新增可选键的 unset：B5a-06 的四个通用键，B5a-09 的 UPSTREAM_IP（Claude 场景清单的必需 Campaign 变量，Codex 参数文件不写），
# B2-11 第二部分 Claude 预检 plan 的十一个键（Codex 参数文件同样不写）。
NEW_OPTIONAL_UNSETS = "".join(f"unset {key}\n" for key in (
    "CLIENT", "CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID", "UPSTREAM_IP",
    "ACTIVE_RELEASE_CATALOG", "STATIC_BASELINE_ANCHORS", "STATIC_BASELINE_REACHABILITY", "PRODUCTION_INVENTORY_STORE",
    "PRODUCTION_INVENTORY_CAMPAIGN", "PREDECESSOR_STORE", "PREDECESSOR_STORE_CAMPAIGN", "ENTRYPOINT", "PRIVACY_MODE",
    "OAUTH_ORG_ID", "MODEL_CATALOG_SHA256",
    # B5b-01a：Claude 的 Campaign 用途（Codex 参数文件不得写）。
    "CAMPAIGN_PURPOSE",
    # B5b-01b 续（边界核对接入）：Claude 专属可选键——VC-5 边界核对 codex-final-wire-isolation 的四个轮次参数（Codex 参数文件不得写）。
    "BOUNDARY_CODEX_ACCOUNT_ID", "BOUNDARY_CODEX_API_KEY_ID", "BOUNDARY_CODEX_MODEL", "PRODUCTION_COMMIT",
    # B5a-09 来源接线：只清除外层残留的 Claude 工程来源，不向 Codex 单元传递任何新环境。
    "CLAUDE_2_1_226_RELAY_FIXTURE", "CLAUDE_2_1_226_FREEZE_FIXTURE", "CLAUDE_2_1_226_STATIC_FIXTURE",
    # 工程磁盘上限未在本轮明确请求时清除外层残留；正式 Codex 仍按原 69% 判据。
    "ENGINEERING_ROOT_MAX_USED_PERCENT",
    # B5a-09／B2-11：唯一正常 Codex 输出差异是清理外层残留的 Claude 目标清单键，不导出或使用它。
    "CLAUDE_TARGET_SCENARIOS_JSON",
    # B5a-09固定79接线：Codex只清除外层Claude专属参数，不消费或导出。
    "CLAUDE_ENTRY_FULL79_SPEC", "CLAUDE_ENTRY_FULL79_SPEC_SHA256",
))


def git_source(relative: str, commit: str = BASELINE_COMMIT) -> bytes | None:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{commit}:{relative}"],
                              capture_output=True, check=True, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def commit_readable(commit: str) -> bool:
    """基线提交在本仓库里读得到（浅克隆或不在仓库内时读不到；对照用例据此在父级跳过，不在子测试里跳过）。"""

    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{commit}^{{commit}}"],
                       capture_output=True, check=True, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def assert_handed_over(case: unittest.TestCase, label: str, batch: str, owners: list[str]) -> None:
    """接管核对：某一批登记的改后内容已不是当前内容时，当前内容必须由另一批登记（那一批逐行对照它）；没有即失败。"""

    case.assertTrue(owners, f"{label} 在 {batch} 之后又改过，但没有一批登记它当前的内容：追加一批（基线提交、改后摘要与残差）")


# 对照基线时登记的有意偏差（相对路径 → (基线原文, 现行写法, 理由)）。
# 入口编排器不再把出口策略文件当 environment-p0 的输入：出口由老板指定、想换就换，换策略不得让入口步骤失效；
# 保护不变弱（entry-preflight 的 guard.sh egress-check 与环境收据采集时的实时准入每次都核对）。除这一行，其余仍逐字对照。
BASELINE_DELIBERATE_DEVIATIONS: dict[str, tuple[tuple[bytes, bytes, str], ...]] = {
    "tools/ci/entry_steps.py": ((
        b'Input("environment", "file", "/etc/sub2api-egress/policy.json"),\n', b"",
        "environment-p0 不再以出口策略整文件为输入"),),
}


def load_baseline(relative: str, name: str, *, siblings: dict[str, types.ModuleType] | None = None) -> types.ModuleType | None:
    """从 git 历史读出改造前的模块源码，作为独立模块对象加载：``__file__`` 指向仓库里的同一位置，加载期间临时登记在
    ``sys.modules``（dataclass 按模块名回查）并按需替换同目录的兄弟模块（``siblings``），结束后恢复。"""

    source = git_source(relative)
    if source is None:
        return None
    for before, after, reason in BASELINE_DELIBERATE_DEVIATIONS.get(relative, ()):
        # 登记过的有意偏差：把基线里的这一行按现行决定改掉再对照，其余仍逐字对照。行在基线里找不到就失败，防止登记陈旧。
        assert before in source, f"基线 {relative} 里找不到登记的偏差行（{reason}）"
        source = source.replace(before, after)
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

    def test_codex_refuses_claude_boundary_keys(self) -> None:
        """B5b-01b 续：边界核对的四个轮次参数是 Claude 专属可选键。Codex 参数文件写了照旧被拒（改造前是「键不在允许集合内」，
        现在是「不得含其它客户端的专属键」——只有报错措辞变化，接受的参数文件集合不变）。"""

        source = self.fixture.env_file.read_text(encoding="utf-8")
        for key, value in (("BOUNDARY_CODEX_ACCOUNT_ID", "42"), ("BOUNDARY_CODEX_API_KEY_ID", "7"),
                           ("BOUNDARY_CODEX_MODEL", "gpt-5.6-codex"), ("PRODUCTION_COMMIT", "a" * 40)):
            with self.subTest(key=key):
                text = source + f"{key}={value}\n"
                with self.assertRaisesRegex(self.old.EnvFileError, "键不在允许集合内"):
                    self.old.parse(text)
                with self.assertRaisesRegex(self.new.EnvFileError, f"不得含其它客户端的专属键：\\['{key}'\\]"):
                    self.new.parse(text)

    def test_codex_refuses_claude_target_scenarios_before_reading_the_path(self) -> None:
        """接受的 Codex 参数集合不扩大；新增键只把旧未知键拒绝细化成客户端专属键拒绝。"""

        source = self.fixture.env_file.read_text(encoding="utf-8")
        text = source + "CLAUDE_TARGET_SCENARIOS_JSON=/missing/claude-scenarios.json\n"
        with self.assertRaisesRegex(self.old.EnvFileError, "键不在允许集合内"):
            self.old.parse(text)
        with self.assertRaisesRegex(self.new.EnvFileError, "不得含其它客户端的专属键"):
            self.new.parse(text)

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

    @staticmethod
    def _legacy_evaluate(orchestrator, is_formal: bool, in_range: set[str]) -> dict:
        """改造前的判定：Formal 建成之前入口门禁无条件强制重做，不看步骤记录。B-09 强制重做改为仅请求模式之后，新编排器的缺省判定
        不再是这样；对照时把新编排器的判定函数换回它，其余（打印、各步骤的判定与原因）仍逐字对照。新规则（缺省按记录、24 小时
        时效、atomic-double 连带）由 test_ci_entry_orchestrator.EntryGatesReuseTests 单独核对。"""

        force = None if is_formal else {"entry-gates": eo.GATES_FORCE_REASON}
        return eo.ES.evaluate(orchestrator.ctx, is_formal_built=is_formal, force=force)

    def assert_plans_match(self, label: str) -> None:
        old_result, old_table = self._plan(self.old)
        with mock.patch.object(eo.Orchestrator, "_evaluate", self._legacy_evaluate):
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


# B5a-06 改动后各驱动脚本的内容摘要：脚本之后又改过就不再逐行对照，改为核对接管（当前内容由之后的某一批登记）。
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


def shell_batches_registering(name: str, digest: str) -> list[str]:
    """登记了驱动脚本 ``name`` 这份内容（摘要）的批次：B5a-06 与之后各批（含 5a 收口的统一基线）。"""

    owners = ["B5a-06"] if SHELL_DIGESTS.get(name) == digest else []
    return owners + [batch for batch, _commit, digests, _residuals in LATER_SHELL_BASELINES if digests.get(name) == digest]


class DriverScriptCodexParityTests(unittest.TestCase):
    def test_scripts_with_codex_values_match_pre_b5a06(self) -> None:
        if not commit_readable(BASELINE_COMMIT):
            self.skipTest(f"读不到基线提交 {BASELINE_COMMIT}（浅克隆或不在仓库内）")
        values = _codex_shell_values()
        names = ("entry-gates.sh", "entry-preflight.sh", "guard.sh", "vc-batch.sh", "vc5-accept.sh", "vc5-precheck.sh", "lib.sh",
                 "fix-and-continue.sh")
        self.assertEqual(sorted(SHELL_DIGESTS), sorted(names))
        for name in names:
            with self.subTest(name):
                text = (DRIVER / name).read_text(encoding="utf-8")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if digest != SHELL_DIGESTS[name]:
                    # B5a-06 之后又改过：这一段的累计变化已在收口时一次性核对，这里只核对当前内容由之后的某一批登记。
                    assert_handed_over(self, name, "B5a-06", shell_batches_registering(name, digest))
                    continue
                source = git_source(f"tools/arm64_capture_driver/driver/{name}")
                self.assertIsNotNone(source, f"基线提交 {BASELINE_COMMIT} 里没有 {name}")
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
# 5a 收口（B5a-11）的统一对照基线：B5a-06／08／09／10 每批都以「上一提交为新基线」补偿对照，旧基线的逐行对照随之失效。收口时把
# 基线统一换成收口时的主线头，上面各批登记过的九个驱动脚本都登记在这一批：基线就是当前内容，残差为空。改造前基线到这里的累计
# 变化在收口时一次性核对过（每次改动都落在上面某一批登记的对照区间里）。
B5A11_BASELINE_COMMIT = "375c240390321677035e47a3fb5fe8091c637b88"
B5A11_SHELL_DIGESTS: dict[str, str] = {
    "entry-gates.sh": "bbc1cd02625ba8fba74efcda74af7c19847a58bfdfa878631d766b9a5d2fa11e",
    "entry-preflight.sh": "762a64e1ca33bf25e84e44711497717ce0a8a3b530730306946d506a225c8cc4",
    "fix-and-continue.sh": "5935d4c70f64979435bee6a626caad04ef94babcf2b604b06bd80a91a5de622c",
    "guard.sh": "23ef1f8cbd4785872793fcbb28a87ef70146596a5d2eb026866abd003cac6a4d",
    "lib.sh": "f085a20cead4a844b84148d56adb522d9a2a93687fb0dc8bb2ccffeba6ca5174",
    "vc-batch.sh": "a94b32137e9efff2a24053392938afab73304ced10e3a238d948a0d1cb59e1e3",
    "vc4-all.sh": "bd8faf3250a7e093d83b1dcdf11165014fcc684cfe725adc30d25136c2a051de",
    "vc5-accept.sh": "bfe7d7c8bbbc3abc326edeb4e92d06ded6effb79000bf5be6c8b53319feecf00",
    "vc5-precheck.sh": "226e9a93763b1d7c149f6cb1b0bf12efba24d51dc9c142c9e4b2d2b61d9ecb75",
}
B5A11_SHELL_RESIDUALS: dict[str, list[str]] = {name: [] for name in B5A11_SHELL_DIGESTS}
# B5b-01a（驱动链 VC-5 准入、修复轮、入口用途与同目标复用旧链按客户端）改了下面这些脚本：以 B5b-01a 开工时的提交为基线，代入
# Codex 取值后只剩登记的几处，每处 Codex 下都等价或走不到：
# * fix-and-continue.sh：受管命令的编排器与项目总账模块改取 client-modules 给出的短名（Codex 即 codex_upgrade 与
#   codex_upgrade_project_ledger，受管命令逐字不变；修复轮的逐字对照见下面 FixAndContinueCodexParityTests）；
# * lib.sh：待接入工具核对（Codex 没有待接入条目，循环里的条件恒不成立）；各 VC-5 脚本在 source lib.sh 之前登记要用的
#   待接入条目（CLIENT_TOOLS_REQUIRED，对 Codex 是空操作）；vc5-switch.sh 只在切候选时登记，恢复生产两客户端通用；
# * pre-a3.sh：取部署收据多传客户端（Codex 即缺省值 codex，与 entry-gates.sh 同一写法）；
# * stage1.sh／stage2.sh／vc23.sh：给出了预检 plan 模板、没有 reuse-official-evidence、VC-2／VC-3 派发脚本不是 vc23.sh 的客户端
#   （Claude）在副作用之前拒绝，Codex 的模板为空、子命令与派发脚本即本脚本，判断恒不触发；
# * vc0-gate-target.sh、vc5-gate-target.sh 与 env.example.sh：代入后逐行相同（环境收据模块与版本参数按客户端表；模板只加了注释）。
B5B01A_BASELINE_COMMIT = "b6f9d3d2f"
B5B01A_SHELL_DIGESTS: dict[str, str] = {
    "env.example.sh": "a6559b2e2c7658c2866dcb9f03e72ad8a569890ef8d03ecca69fb16840fc0042",
    "fix-and-continue.sh": "b510a649430f1287ba88051029e58cb6133fdc40a09a1cfe19af5741046c0256",
    "lib.sh": "e10f364e823194d5deb596d1cadd944b253d5cfb5c241446697fbb756d080431",
    "pre-a3.sh": "1f31682216574e38383af72c88649ce6415bc75632790ff008e73c3c09e1d438",
    "stage1.sh": "34b0ee1f365acb0632212556c880dc380a01485f42457d9cf1e4bc2850cdaf63",
    "stage2.sh": "7c2911e6e28857d021034db47756be12fea1934dbf898d190f81dc13517db856",
    "vc0-gate-target.sh": "15b772d2f55fb6a45ce73f428e36ce5cf241581b6d86a224b42128e8f525071d",
    "vc23.sh": "524633590dea68471ec75d1d49f56cf6784a07b8efa47d9a5676c2a057635ca5",
    "vc5-accept.sh": "3478f336fb261f53825aa300406077a720e6c46e2275f0bb22913d988ba5ceb8",
    "vc5-all.sh": "b78d00a9763452debf7e5a4f8f0fdfe2146469cfdd7b7182383e9a2cbec0c6fa",
    "vc5-canonical2.sh": "0753b7e0d05ff5d8a5d80c4d354c97ade30a32f89ad0ff15b19230267c5c1cd1",
    "vc5-gate-target.sh": "9b991b8397ea1be59d44b63175d8cb8149d005b540691c5357bcb9e75b8992b3",
    "vc5-kilo.sh": "df082d2f204ce6d61eb49ed29368800297f85a73c90d7b9544d61528d6134694",
    "vc5-precheck.sh": "54b4c14e9a7824ee6f34a3717d9b160eb8180594055424b85a5dba28b7c12710",
    "vc5-recover.sh": "861d23b205cc18cf445945ee8a1002c829ad0b88c6154f529b81f235bafdb91e",
    "vc5-seal-receipts.sh": "169b32b4274a6a947856c83d545776b5d8cab74c571bb4cd7be04068dadd0e13",
    "vc5-seal.sh": "54fb4b9f26513ebe55b389e70ec5fd7e1a80abd959603ecf4d6250818ccfe7fb",
    "vc5-start.sh": "56f79dddd134d8ea2a79bf56baf21ddc622d5e9f8f90b41b321fc79fdbed7682",
    "vc5-switch.sh": "8a785fe25972a1ff8b8f666b3096f5485f79ecd933d759547860ce67de97cf4e",
}
B5B01A_SHELL_RESIDUALS: dict[str, list[str]] = {
    "env.example.sh": [],
    "fix-and-continue.sh": [
        '+CLIENT_MODULES=$(python3 "$FCPY" client-modules --params "$PARAMS_PATH" < /dev/null) || exit 2',
        '+eval "$CLIENT_MODULES"',
        "+unset CLIENT_MODULES",
        '-  managed codex_upgrade evolution-status tool-evolution-status --campaign-dir "$C"',
        '+  managed "$ORCHESTRATOR" evolution-status tool-evolution-status --campaign-dir "$C"',
        '-  managed codex_upgrade evolution-preview tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON"',
        '+  managed "$ORCHESTRATOR" evolution-preview tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON"',
        '-  managed codex_upgrade evolution-apply tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON" \\',
        '+  managed "$ORCHESTRATOR" evolution-apply tool-evolution --campaign-dir "$C" --fix-commit "$FIX_COMMIT" --reason "$EVOLUTION_REASON" \\',
        '-  managed codex_upgrade evolution-status-after tool-evolution-status --campaign-dir "$C"',
        '+  managed "$ORCHESTRATOR" evolution-status-after tool-evolution-status --campaign-dir "$C"',
        '-  managed codex_upgrade extend-preview deadline-extend preview --campaign-dir "$C" --scope stage --phase "$EXTEND_PHASE" \\',
        '+  managed "$ORCHESTRATOR" extend-preview deadline-extend preview --campaign-dir "$C" --scope stage --phase "$EXTEND_PHASE" \\',
        '-  managed codex_upgrade extend-apply deadline-extend apply --campaign-dir "$C" --preview "$EXT_PREVIEW" \\',
        '+  managed "$ORCHESTRATOR" extend-apply deadline-extend apply --campaign-dir "$C" --preview "$EXT_PREVIEW" \\',
        '-  managed codex_upgrade_project_ledger "$label" "${args[@]}"',
        '+  managed "$PROJECT_LEDGER_MODULE" "$label" "${args[@]}"',
        '-  managed codex_upgrade "$label" "$@"',
        '+  managed "$ORCHESTRATOR" "$label" "$@"',
        '-  managed codex_upgrade "$label-after-repair" "$@"',
        '+  managed "$ORCHESTRATOR" "$label-after-repair" "$@"',
        '-  managed codex_upgrade reconcile reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"',
        '+  managed "$ORCHESTRATOR" reconcile reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"',
        '-  managed codex_upgrade approve-preview reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"',
        '+  managed "$ORCHESTRATOR" approve-preview reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT"',
        '-  managed codex_upgrade approve reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --approve-recovery-sha256 "$REVIEW_SHA256"',
        '+  managed "$ORCHESTRATOR" approve reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --approve-recovery-sha256 "$REVIEW_SHA256"',
        '-  managed codex_upgrade authorize reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --authorize-recovery-preview "$PREVIEW"',
        '+  managed "$ORCHESTRATOR" authorize reconcile-attempt --campaign-dir "$C" --attempt-id "$ATTEMPT" --authorize-recovery-preview "$PREVIEW"',
    ],
    "lib.sh": [
        "+if declare -p CLIENT_TOOLS_REQUIRED >/dev/null 2>&1; then",
        '+  CLIENT_TOOLS_WAITING=""',
        '+  for CLIENT_TOOL in "${CLIENT_TOOLS_REQUIRED[@]}"; do',
        '+    if [ -n "${!CLIENT_TOOL:-}" ]; then CLIENT_TOOLS_WAITING="${CLIENT_TOOLS_WAITING}；${!CLIENT_TOOL}"; fi',
        "+  done",
        '+  if [ -n "$CLIENT_TOOLS_WAITING" ]; then echo "本轮客户端（codex）的这一步尚未接入：${CLIENT_TOOLS_WAITING#；}"; exit 3; fi',
        "+  unset CLIENT_TOOL CLIENT_TOOLS_WAITING",
        "+fi",
    ],
    "pre-a3.sh": [
        "-DEPLOY=$(python3 -B - \"$DRV/../install.py\" \"$D\" <<'PYDEPLOY'",
        "+DEPLOY=$(python3 -B - \"$DRV/../install.py\" \"$D\" codex <<'PYDEPLOY'",
        "-print(module.latest_deploy_receipt(Path(sys.argv[2])/'control')[0])",
        "+print(module.latest_deploy_receipt(Path(sys.argv[2])/'control', sys.argv[3])[0])",
    ],
    "stage1.sh": [
        '+if [ "${#CT_PREFLIGHT_PLAN_ARGV[@]}" != 0 ]; then',
        '+  echo "本轮客户端（codex）的预检 Campaign 由入口编排器按客户端表模板建立：改用 entry.sh（同一 ARM64_VC_ENV）"; exit 3',
        "+fi",
    ],
    "stage2.sh": [
        '+[ -n reuse-official-evidence ] || { echo "本轮客户端（codex）没有 reuse-official-evidence：同目标复用改用 entry.sh（EVIDENCE_DECISION=reuse）"; exit 3; }',
    ],
    "vc0-gate-target.sh": [],
    "vc23.sh": [
        '+[ vc23.sh = vc23.sh ] || { echo "本轮客户端（codex）的 VC-2／VC-3 派发脚本是 vc23.sh，拒绝"; exit 3; }',
    ],
    "vc5-accept.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_ACCEPT)",
    ],
    "vc5-all.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN CT_PENDING_TOOL_VC5_SEAL CT_PENDING_TOOL_VC5_ACCEPT)",
    ],
    "vc5-canonical2.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_ACCEPT)",
    ],
    "vc5-gate-target.sh": [],
    "vc5-kilo.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
    ],
    "vc5-precheck.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)",
    ],
    "vc5-recover.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RECOVER CT_PENDING_TOOL_VC5_RUN)",
    ],
    "vc5-seal-receipts.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
    ],
    "vc5-seal.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
    ],
    "vc5-start.sh": [
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)",
    ],
    "vc5-switch.sh": [
        '+[ "${1:-}" = restore ] || CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)',
    ],
}
# B5b-01b（驱动链 VC-5 的 run／seal／accept 按客户端分派脚本）改了下面这些脚本：以 B5b-01b 开工时的提交为基线，代入 Codex
# 取值后只剩登记的几处，每处 Codex 下都等价或走不到：
# * lib.sh：按派发脚本核对（CLIENT_STEP_REQUIRED）——Codex 各脚本登记的就是自己，条件恒不成立；
# * vc5-start／seal／kilo／seal-receipts／accept／canonical2／precheck／switch／recover：待接入登记（CLIENT_TOOLS_REQUIRED，对 Codex
#   是空操作）换成按派发脚本核对（对 Codex 同样是空操作；vc5-recover 仍登记候选恢复段的待接入）；
# * vc5-all.sh：各步派发脚本按客户端表调用（代入后与原来逐字相同，不进残差）；暂停文件 vc5-pause.json 只由 Claude 的 seal 链写，
#   Codex 下删除不存在的文件、判断恒不成立；canonical 交接按表项判断，Codex 的表项非空，恒走原分支。
B5B01B_BASELINE_COMMIT = "322d3ea62"
B5B01B_SHELL_DIGESTS: dict[str, str] = {
    "lib.sh": "2b735161039e81facf72c3e5810aaf84ebe9ff26bd180ce5edbc2a49c21cb7c9",
    "vc5-accept.sh": "bdbe2da526e55cb824e18dcd8b554e98a15c3530f8397df21f1f84fa03869cd1",
    "vc5-all.sh": "2b0254512cdb81d608c62bf531fef15dcace0532c809e15427cfe71e6bbeea28",
    "vc5-canonical2.sh": "5e62e8d2df9f41e6ed46acdb8cd3fbee594cc469b30b68f6f1929a188d5dd680",
    "vc5-kilo.sh": "a0207243d2704fee1976619e7145a0a43ea63e767d27d161404ae21392c005cd",
    "vc5-precheck.sh": "5d45a95529b20428d98d496cc36c5d13846e180aaf452ff5fe917a22bba8937d",
    "vc5-recover.sh": "03e948c1fc251dfe6c131c37c98d434daace79f0a0c6f8b755f1eedc4dcfaf7b",
    "vc5-seal-receipts.sh": "40d98a1e51208d95bdd5e4602ae5d2435ea6bb45434ad3fcddd2a8b8e10e3fc5",
    "vc5-seal.sh": "ad7b57d99c6e67b1998c9a7214f4c3c62f99162f7a309934f48dccab2b1516d2",
    "vc5-start.sh": "02606c4d381251c474fc468291cc3ad2e3dd88c9474ec0e3e03072492ea60779",
    "vc5-switch.sh": "70b8e6e3f547d78b4987ee2ef7166dc6521883e6428d90186e42d24ea5885b20",
}
B5B01B_SHELL_RESIDUALS: dict[str, list[str]] = {
    "lib.sh": [
        "+export PYTHONDONTWRITEBYTECODE=1",
        "-ARM64_VC_ENV_EXPORTS=$(python3 \"$DRV/parse_env.py\" \"$ARM64_VC_ENV\") || { echo \"参数文件拒绝加载：$ARM64_VC_ENV\"; exit 2; }",
        "+ARM64_VC_ENV_EXPORTS=$(python3 -B \"$DRV/parse_env.py\" \"$ARM64_VC_ENV\") || { echo \"参数文件拒绝加载：$ARM64_VC_ENV\"; exit 2; }",
        "+fi",
        "+if declare -p CLIENT_STEP_REQUIRED >/dev/null 2>&1; then",
        "+  CLIENT_STEP_DISPATCH=\"${!CLIENT_STEP_REQUIRED[0]:-}\"",
        "+  if [ \"$CLIENT_STEP_DISPATCH\" != \"${CLIENT_STEP_REQUIRED[1]}\" ]; then",
        "+    echo \"本轮客户端（codex）的这一步由 ${CLIENT_STEP_DISPATCH:-（无，本客户端没有这一步）} 负责，不是 ${CLIENT_STEP_REQUIRED[1]}，拒绝\"; exit 3",
        "+  fi",
        "+  unset CLIENT_STEP_DISPATCH",
    ],
    "vc5-accept.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_ACCEPT)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_ACCEPT_SCRIPT vc5-accept.sh)",
    ],
    "vc5-all.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN CT_PENDING_TOOL_VC5_SEAL CT_PENDING_TOOL_VC5_ACCEPT)",
        "+  rm -f \"$RUNROOT/vc5-pause.json\"",
        "+  if [ -f \"$RUNROOT/vc5-pause.json\" ]; then",
        "+    python3 -c \"import json,sys; d=json.load(open(sys.argv[1])); print('VC5_ALL_PAUSED reason=' + d['reason'] + ' next=' + d['next'])\" \"$RUNROOT/vc5-pause.json\"; exit 0",
        "+  fi",
        "-echo \"=== canonical 交接 $(utc_now)\"; bash \"$DRV/vc5-canonical2.sh\" \"$ATT\" 2>&1 | { grep -E \"review_sha256|CLI preview|一致|plan ->|===|rc=|actions:|checkpoint|import-receipt|canonical latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败\" || true; } | cut -c1-300",
        "+if [ -n vc5-canonical2.sh ]; then",
        "+  echo \"=== canonical 交接 $(utc_now)\"; bash \"$DRV/vc5-canonical2.sh\" \"$ATT\" 2>&1 | { grep -E \"review_sha256|CLI preview|一致|plan ->|===|rc=|actions:|checkpoint|import-receipt|canonical latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败\" || true; } | cut -c1-300",
        "+else",
        "+  echo \"=== 本轮客户端（codex）没有 canonical 交接：VC-5 已在 accept 时完成 $(utc_now)\"",
        "+fi",
    ],
    "vc5-canonical2.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_ACCEPT)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_CANONICAL_SCRIPT vc5-canonical2.sh)",
    ],
    "vc5-kilo.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_SEAL_SCRIPT vc5-seal.sh)",
    ],
    "vc5-precheck.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)",
    ],
    "vc5-recover.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RECOVER CT_PENDING_TOOL_VC5_RUN)",
        "+CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RECOVER)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)",
    ],
    "vc5-seal-receipts.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_SEAL_SCRIPT vc5-seal.sh)",
    ],
    "vc5-seal.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_SEAL)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_SEAL_SCRIPT vc5-seal.sh)",
    ],
    "vc5-start.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)",
    ],
    "vc5-switch.sh": [
        "-[ \"${1:-}\" = restore ] || CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RUN)",
        "+[ \"${1:-}\" = restore ] || CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)",
    ],
}
# 之后各批的新基线（批次、基线提交、改动后的脚本摘要、代入 Codex 取值后的残差）：某个脚本再改过时它那一项改为核对接管（当前
# 内容由更新的一批登记），改了驱动脚本的批次在末尾追加一项。基线脚本引用的待接入变量 CT_PENDING_CHECK_PLAN_AUDIT 已不再由客户端
# 表给出，按 Codex 的空串代入（见 B5a-09 一段）。
# m9 修复（入口空跑 guard-pre-plan 报「驱动目录不得含 __pycache__」）：lib.sh 在任何 python 调用之前导出
# PYTHONDONTWRITEBYTECODE=1，参数解析器调用加 -B。两处只决定字节码写不写，命令与输出不变（原先到导出 D 时也会导出同一个变量）。
M9_BYTECODE_BASELINE_COMMIT = "6097da0d16ccecd552f657052179a9906e12d0e8"
M9_BYTECODE_SHELL_DIGESTS: dict[str, str] = {
    "lib.sh": "b2543fed5635b04ec5396fc1c7edf76b1f0ef3b3e9e1d3d05016510b5cfb05df",
}
M9_BYTECODE_SHELL_RESIDUALS: dict[str, list[str]] = {
    "lib.sh": [
        "+export PYTHONDONTWRITEBYTECODE=1",
        '-ARM64_VC_ENV_EXPORTS=$(python3 "$DRV/parse_env.py" "$ARM64_VC_ENV") || { echo "参数文件拒绝加载：$ARM64_VC_ENV"; exit 2; }',
        '+ARM64_VC_ENV_EXPORTS=$(python3 -B "$DRV/parse_env.py" "$ARM64_VC_ENV") || { echo "参数文件拒绝加载：$ARM64_VC_ENV"; exit 2; }',
    ],
}
# B4-05b-1（候选采集续跑按客户端分派脚本，含 B5b-01b 续的边界核对接入）改了下面三个脚本：以 rebase 后父提交为基线，代入 Codex
# 取值后只剩登记的几处：
# * env.example.sh：只追加 Claude 边界核对参数的中文注释，Codex 参数与代码不变；
# * lib.sh：只改注释里的待接入变量示例（待接入条目全部落实，表里不再有 CT_PENDING_TOOL_VC5_RECOVER），代码不变；
# * vc5-recover.sh：候选恢复段的待接入登记删去（对 Codex 本来就是空操作），按派发脚本核对改认续跑派发脚本表项
#   CT_VC5_RECOVER_SCRIPT（Codex 取值就是 vc5-recover.sh 自己，条件恒不成立）。
B405B1_BASELINE_COMMIT = "3d114ee8973f2d07090c72f2c3ef3773d166dd9b"
B405B1_SHELL_DIGESTS: dict[str, str] = {
    "env.example.sh": "b8f7cecb3eda9a6807d1d0239f48ccc31d52b9f310cfff62b393c41c656ac651",
    "lib.sh": "41c966559b1fd8cb99cfd23b8033f062e0621dcdb5391591d3f1666697460233",
    "vc5-recover.sh": "5f590092be6304e8bde89f4a4e7a8fc37e8f8e3c7b26b75dab5f3a3fbebb5307",
}
B405B1_SHELL_RESIDUALS: dict[str, list[str]] = {
    "env.example.sh": [],
    "lib.sh": [],
    "vc5-recover.sh": [
        "-CLIENT_TOOLS_REQUIRED=(CT_PENDING_TOOL_VC5_RECOVER)",
        "-CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-start.sh)",
        "+CLIENT_STEP_REQUIRED=(CT_VC5_RECOVER_SCRIPT vc5-recover.sh)",
    ],
}
# B5a-09 工程来源接线：Codex 表项为空，新增循环不进入循环体，PRE_A3_ARGS 逐字相同。
# test_claude_pre_a3_source_wiring 另以真实 bash 执行这个循环，核对空表与三个固定来源的实际参数。
B5A09_SOURCE_BASELINE_COMMIT = "4cc10d5857177f60e5ad48f85bc1e8f572ee9f68"
B5A09_SOURCE_SHELL_DIGESTS = {"entry-gates.sh": "3c88989210c6770b56f3dad9fa4705f53d0f784dc37a3e9dc0f30c9562bbff37"}
B5A09_SOURCE_SHELL_RESIDUALS = {"entry-gates.sh": [
    "+    for name in; do",
    '+      if [ -z "${!name:-}" ]; then echo "ENTRY_GATES_ABORTED：pre-A3 缺少本轮显式来源 ${name}" >&2; exit 3; fi',
    '+      PRE_A3_ARGS+=(--pre-a3-env "$name=${!name}")',
    "+    done",
]}
# 工程磁盘策略：基线为本次开工提交，逐行登记真实替换；缺省仍走 69%／30 GiB 和 40 GiB floor，
# 唯一提高路径由真实私有 staging fixture_only 总账复验限定，拒绝场景另由专项测试覆盖。
# env.example.sh 的工程参数中文说明也是该批次既有产物，非注释残差为空；补全此前遗漏的接管登记。
ENGINEERING_DISK_BASELINE_COMMIT = 'f769c32c5831fccb76d1f66cf093e8d3eb919090'
ENGINEERING_DISK_SHELL_DIGESTS = {"guard.sh": '166e25b995eec31604b536f7a08c6253e4194c52701ecbaecdd95d2133b48071',
                                  "env.example.sh": "a0504a53f47488df9d35c5ca373b5ce973d76623fe8a3cf9b7d36332d8ccd9a2"}
ENGINEERING_DISK_SHELL_RESIDUALS = {"guard.sh": [
    '-python3 - "$MIN_FREE_GIB" <<\'PY\'',
    '-import shutil, sys',
    '-t, u, f = shutil.disk_usage("/"); used = u * 100 / t; free_gib = f / 2**30; need = float(sys.argv[1])',
    '-print(f"guard/disk: used={used:.1f}% free={free_gib:.1f}GiB (policy ≤69% & ≥30GiB; operator ≥{need:g}GiB, floor 40)")',
    '-if used > 69 or free_gib < 30 or free_gib < need:',
    '+python3 -B - "$MIN_FREE_GIB" "$DRV" <<\'PY\'',
    '+import os, sys',
    '+sys.path.insert(0, sys.argv[2])',
    '+import entry_housekeeping as housekeeping',
    '+try:',
    '+    disk = housekeeping.disk_status(min_free_gib=float(sys.argv[1]), environment=os.environ)',
    '+except housekeeping.HousekeepingError as error:',
    '+    sys.exit(f"guard/disk: FAIL — {error}")',
    '+print(f"guard/disk: used={disk[\'used_percent\']:.1f}% free={disk[\'free_gib\']:.1f}GiB "',
    '+      f"(policy ≤{disk[\'max_used_percent\']:g}% & ≥30GiB; operator ≥{disk[\'min_free_gib\']:g}GiB, floor 40)")',
    '+if disk.get("engineering_policy"):',
    '+    print("guard/disk-engineering:", disk["engineering_policy"])',
    '+if not disk["ok"]:',
], "env.example.sh": []}
# 入口可选并发透传以实际父提交为基线；缺省数组为空，执行器 argv 保持原样。
# 新参数的正整数检查仅在显式提供 --parallel 时执行；不改客户端表、模式或承接批准规则。
ENTRY_PARALLEL_BASELINE_COMMIT = "5b4663bc3ac79d76ddc99b9570d696d76cfb0714"
ENTRY_PARALLEL_SHELL_DIGESTS = {'entry-gates.sh': 'a8c8083f82cd430b46b4f2f8ca59faa060da0089c6c2d484f4be9dc95bf726c1'}
ENTRY_PARALLEL_SHELL_RESIDUALS = {'entry-gates.sh': ['-usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--with-gates '
                    '<门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] '
                    '[--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] '
                    '[--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] '
                    '[--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" '
                    '>&2; }',
                    '+usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--parallel '
                    '<正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] '
                    '[--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] '
                    '[--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store '
                    '<目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 '
                    '位提交> [<前端依赖目录绝对路径>]" >&2; }',
                    '+PARALLEL_ARGS=()',
                    '+    --parallel)',
                    '+      if ! [[ "${2:-}" =~ ^[1-9][0-9]*$ ]]; then echo "--parallel 必须是正整数" >&2; exit 2; fi',
                    '+      PARALLEL_ARGS=(--parallel "$2"); shift 2 ;;',
                    '-    --mode "$MODE" --record-store "$STORE" ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} ${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} '
                    '${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?',
                    '+    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} '
                    '${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || '
                    'EXEC_RC=$?']}
# B5a-09 私有 fixture 总账接点：默认保留原 D 路径与 status 命令；只有显式工程策略复验通过才进入新分支。
# 锁内重新核客户端、私有目录与 plan 摘要，原 replay_head 与业务停线不变；专项测试验证合法路径、漂移和拒绝场景。
FIXTURE_PROJECT_GUARD_BASELINE_COMMIT = 'f1ce9dcd3ae0e46d105b9ac0c3811a646f192a49'
FIXTURE_PROJECT_GUARD_SHELL_DIGESTS = {'guard.sh': 'f7c56c3891d46b6b95cb51424501c1ced922a8f8ce3c796542bb3377d52c5a67'}
FIXTURE_PROJECT_GUARD_SHELL_RESIDUALS = {'guard.sh': ['-python3 -m tools.official_client_capture.codex_upgrade_project_ledger status --ledger-dir '
              '"$D/evidence/campaigns/upgrade-project-ledger" | python3 -c "',
              '+guard_project_status() {',
              '+  if [ "${ENGINEERING_ROOT_MAX_USED_PERCENT+x}" != x ]; then',
              '+    python3 -m tools.official_client_capture.codex_upgrade_project_ledger status --ledger-dir '
              '"$D/evidence/campaigns/upgrade-project-ledger"',
              '+    return',
              '+  fi',
              '+  python3 -B - "$DRV" tools.official_client_capture.codex_upgrade_project_ledger <<\'PY\'',
              '+import importlib, json, os, sys',
              '+from pathlib import Path',
              '+sys.path.insert(0, sys.argv[1])',
              '+import entry_housekeeping as housekeeping',
              '+project_ledger = importlib.import_module(sys.argv[2])',
              '+os.umask(0o077)',
              '+try:',
              '+    policy = housekeeping.disk_policy(os.environ)',
              '+    if policy is None:',
              '+        raise housekeeping.HousekeepingError("显式工程策略未通过复验")',
              '+    root = Path(policy["ledger_dir"])',
              '+    with project_ledger.project_lock(root):',
              '+        locked_policy = housekeeping.disk_policy(os.environ)',
              '+        if locked_policy != policy:',
              '+            raise housekeeping.HousekeepingError("工程总账路径、客户端或 plan 摘要在取锁后漂移")',
              '+        head = project_ledger.replay_head(root)',
              '+except (housekeeping.HousekeepingError, project_ledger.ProjectLedgerError,',
              '+        project_ledger.root_cause.RootCauseError, OSError) as error:',
              '+    print(f"guard/project: FAIL — {error}", file=sys.stderr)',
              '+    sys.exit(2)',
              '+result = {key: value for key, value in head.items() if key not in {"accounted_identity_index", '
              '"operations"}}',
              '+print(json.dumps(result, ensure_ascii=False, sort_keys=True))',
              '+PY',
              '+}',
              '+guard_project_status | python3 -c "']}
# B5a-09固定79接线只由Claude显式参数触发；Codex分支与原消费命令保持不变。
# 新增条件与函数保留实际逐行残差，合法／拒绝分支及未配置行为由接线短验另核。
RECORDED_BRIDGE_BASELINE_COMMIT = 'c12674dcc0c10e0b26a3d3ed1ae4e6784fd0a986'
RECORDED_BRIDGE_SHELL_DIGESTS = {'entry-gates.sh': 'c2a1b66703be613d700afcfa5eb2ba163a665b68775c0d5304f36aa26f2af7e4'}
RECORDED_BRIDGE_SHELL_RESIDUALS = {'entry-gates.sh': ['+RECORDED_BRIDGE=false',
                    '+if [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
                    '+  if [ codex != claude ] || [ -z "${CLAUDE_ENTRY_FULL79_SPEC:-}" ] || [ -z '
                    '"${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
                    '+    echo "ENTRY_GATES_ABORTED：完整79接线只允许Claude，文件与外持SHA必须成对提供" >&2; exit 3',
                    '+  fi',
                    '+  RECORDED_BRIDGE=true',
                    '+fi',
                    '+verify_recorded_full79() {',
                    '+  local args=(--spec "$CLAUDE_ENTRY_FULL79_SPEC" --sha256 "$CLAUDE_ENTRY_FULL79_SPEC_SHA256") '
                    'name',
                    '+  for name in; do',
                    '+    args+=(--pre-a3-env "$name=${!name:-}")',
                    '+  done',
                    '+  if [ "$#" -gt 0 ]; then args+=(--certification "$1" --tree "$TREE" --data-root "$D"); fi',
                    '+  ( cd "$D" && PYTHONPATH="$D" python3 -B "$DRV/entry_gates.py" recorded-full79-inputs '
                    '"${args[@]}" ) > "$OUT/recorded-full79-inputs.json"',
                    '+}',
                    '+    if [ "$RECORDED_BRIDGE" = true ]; then',
                    '+      if [ "${#AUDIT_POLICY_ARGS[@]}" -eq 0 ]; then',
                    '+        echo "ENTRY_GATES_ABORTED：固定79运行必须显式提供冻结audit-policy，不能改写其它单元承接策略" >&2; exit 3',
                    '+      fi',
                    '+      verify_recorded_full79',
                    '+      PRE_A3_ARGS+=(--recorded-full79-spec "$CLAUDE_ENTRY_FULL79_SPEC" '
                    '--recorded-full79-spec-sha256 "$CLAUDE_ENTRY_FULL79_SPEC_SHA256")',
                    '+      AUDIT_READS=true; AUDIT_STRICT=true',
                    '+    fi',
                    '+    if [ "$RECORDED_BRIDGE" = true ]; then verify_recorded_full79 "$PRE_A3_CERTIFICATION"; fi',
                    '+    if [ "$RECORDED_BRIDGE" = true ]; then verify_recorded_full79 "$REUSE"; fi',
                    '-  python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification record-reuse '
                    '--certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation '
                    '"$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" | cut -c1-300',
                    '+  if [ "$RECORDED_BRIDGE" = true ]; then',
                    '+    verify_recorded_full79 "$PRE_A3_CERTIFICATION"',
                    '+    python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification record-reuse '
                    '--certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation '
                    '"$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" | tee '
                    '"$OUT/pre-a3-reuse.json" | cut -c1-300',
                    '+  else',
                    '+    python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification record-reuse '
                    '--certification "$PRE_A3_CERTIFICATION" --deployment-receipt "$DEPLOY" --policy-activation '
                    '"$POLICY_ACTIVATION" --receipt-root "$(dirname "$PRE_A3_CERTIFICATION")" | cut -c1-300',
                    '+  fi']}
# 来源目录稳定性修复按真实父提交登记；权限、锁与可变输出均按实差演进，不追认旧来源。
ENTRY_SOURCE_STABILITY_BASELINE = 'cf691738ba83d7fe6474ffdf134cd680372b21b8'
ENTRY_SOURCE_STABILITY_DIGESTS = {'entry-gates.sh': '30180dfe1877ebaece0eb89a5034658ee4028876d11f2b19fe75cabb81310d19'}
ENTRY_SOURCE_STABILITY_RESIDUALS = {'entry-gates.sh': ['-mkdir -p "$WORK"; chmod 700 "$WORK"',
                    '-LOCK="$WORK/.entry-gates.lock"',
                    '+private_directory() {',
                    '+  python3 -B - "$1" <<\'PY\'',
                    '+import os, stat, sys',
                    '+path = sys.argv[1]',
                    '+os.makedirs(path, mode=0o700, exist_ok=True)',
                    '+if stat.S_IMODE(os.stat(path).st_mode) != 0o700:',
                    '+    os.chmod(path, 0o700)',
                    '+PY',
                    '+}',
                    '+private_directory "$WORK"',
                    '+LOCK_BASE=$(python3 -B - "$WORK" <<\'PY\'',
                    '+import hashlib, os, pathlib, sys',
                    '+work = pathlib.Path(os.path.realpath(sys.argv[1]))',
                    '+root = work.parent / ".entry-gates-locks" / hashlib.sha256(str(work).encode()).hexdigest()',
                    '+if any(item.is_symlink() for item in (root, *root.parents)):',
                    '+    raise ValueError("入口锁目录不得含符号链接")',
                    '+print(root)',
                    '+PY',
                    '+)',
                    '+private_directory "$(dirname "$LOCK_BASE")"',
                    '+private_directory "$LOCK_BASE"',
                    '+LOCK="$LOCK_BASE/held"',
                    '+LEGACY_LOCK="$WORK/.entry-gates.lock"',
                    '+LEGACY_OWNED=false',
                    '-trap \'rm -rf "$LOCK"\' EXIT',
                    '+cleanup_entry_lock() {',
                    '+  if [ "$LEGACY_OWNED" = true ] && [ "$(cat "$LEGACY_LOCK/pid" 2>/dev/null || true)" = "$$" ]; '
                    'then',
                    '+    : > "$LEGACY_LOCK/pid"',
                    '+  fi',
                    '+  rm -rf "$LOCK"',
                    '+}',
                    '+trap cleanup_entry_lock EXIT',
                    '+while [ -e "$LEGACY_LOCK" ]; do',
                    '+  if [ -L "$LEGACY_LOCK" ] || [ ! -d "$LEGACY_LOCK" ] || [ -L "$LEGACY_LOCK/pid" ]; then',
                    '+    echo "ENTRY_GATES_ABORTED：旧入口锁必须是普通目录与 PID 文件，拒绝迁移"; exit 3',
                    '+  fi',
                    '+  HOLDER=$(cat "$LEGACY_LOCK/pid" 2>/dev/null || true)',
                    '+  if [ -n "$HOLDER" ] && kill -0 "$HOLDER" 2>/dev/null; then',
                    '+    if [ "$LOCK_WAITED" -lt "$WAIT_LOCK" ]; then sleep 10; LOCK_WAITED=$((LOCK_WAITED + 10)); '
                    'continue; fi',
                    '+    echo "ENTRY_GATES_ABORTED：已有旧版入口门禁在运行（PID ${HOLDER}），拒绝迁移"; exit 3',
                    '+  fi',
                    '+  break',
                    '+done',
                    '+private_directory "$LEGACY_LOCK"',
                    '+printf \'%s\\n\' "$$" > "$LEGACY_LOCK/pid"',
                    '+LEGACY_OWNED=true',
                    '-mkdir -p "$(dirname "$OUT")"; mkdir -p -m 0700 "$OUT"; chmod 700 "$OUT"',
                    '+mkdir -p "$(dirname "$OUT")"; private_directory "$OUT"',
                    '-mkdir -p "$STORE"; chmod 700 "$STORE"',
                    '+private_directory "$STORE"']}




# >>> T2 预检登记常量（脚本生成，可重跑）
T2_PRECHECK_BASELINE = 'b6c3cd0f049e4ab4a271bae2f976e726d07c5998'
T2_PRECHECK_DIGESTS = {'entry-gates.sh': 'f80292303f9970da87a735b1d63f247e74b81dc7d544393054832a0b2db57186',
 'entry-preflight.sh': '9f4417a012a9bb5e4d572f7018e6f8cbca88d640dba2dd130fbba87a647f22b3',
 'env.example.sh': '9b14a35fe251cd604dcafd4ba5fa75e19bf141b65512a5f782a261b9efad61f3',
 'lib.sh': 'da83d97f499b8e01d230465641ad4393dbf842684b9f9f4804847aa5b2767ce5',
 'vc5-all.sh': '83b6d035858d5d279fef4a23d218c62da65a4b974cb4e76f0527ce04a339f999',
 'vc5-gate-target.sh': '98ce09d9da772e8f32372f74bc892c5820b35d80fa64a97538a66a6d2c6b52a8',
 'vc5-precheck.sh': '7f50098622a6652f1366ebfc009ad2e80cf5ca846ecee010340c4ab275e19b79'}
T2_PRECHECK_RESIDUALS = {'entry-gates.sh': ['-usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] '
                    '[--parallel <正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache '
                    '<目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--pre-a3-staging-parent '
                    '<绝对目录，默认 $D/staging/pre-a3-scenarios>] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] '
                    '[--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] '
                    '[--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] [--declared-sample '
                    'off|always|every:N] [--bind-only] [--gate-scope full|client-plan|auto [--select-base <40 位提交>] [--include-claude]] <bundle '
                    '绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
                    '+usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|vc4|pre-a3|regression] [--go-warmup auto|off] '
                    '[--parallel <正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache '
                    '<目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--pre-a3-staging-parent '
                    '<绝对目录，默认 $D/staging/pre-a3-scenarios>] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] '
                    '[--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] '
                    '[--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] [--declared-sample '
                    'off|always|every:N] [--bind-only] [--gate-scope full|client-plan|auto [--select-base <40 位提交>] [--include-claude]] '
                    '[--resume-from auto|<运行ID> [--resume-series <系列>] [--resume-binding-extra <JSON>] [--resume-max-age-hours <小时>] '
                    '[--resume-waive-env <理由> --resume-waive-env-names <条目,…>]] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
                    '+RESUME_FROM=""; RESUME_SERIES=""; RESUME_BINDING_EXTRA="{}"; RESUME_MAX_AGE=""; RESUME_WAIVE=""; RESUME_WAIVE_NAMES=""; '
                    'RESUME_ARGS=(); FORCE_ARGS=()',
                    '+    --resume-from) RESUME_FROM="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '+    --resume-series) RESUME_SERIES="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '+    --resume-binding-extra) RESUME_BINDING_EXTRA="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '+    --resume-max-age-hours) RESUME_MAX_AGE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '+    --resume-waive-env) RESUME_WAIVE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '+    --resume-waive-env-names) RESUME_WAIVE_NAMES="${2:-}"; shift 2 || { usage; exit 2; } ;;',
                    '-case "$PROFILE" in entry|full-gates|preflight|pre-a3|regression) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac',
                    '+case "$PROFILE" in entry|full-gates|preflight|vc4|pre-a3|regression) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac',
                    '+if [ -n "$RESUME_FROM" ] && [ -z "$RESUME_WAIVE$RESUME_WAIVE_NAMES" ]; then',
                    '+  RESUME_WAIVE="${ENTRY_RESUME_WAIVE_ENV:-}"; RESUME_WAIVE_NAMES="${ENTRY_RESUME_WAIVE_ENV_NAMES:-}"',
                    '+fi',
                    '+if [ -n "$RESUME_FROM" ]; then',
                    '+  if ! [[ "$RESUME_FROM" =~ ^(auto|[0-9A-Za-z._-]{1,128})$ ]]; then echo "--resume-from 只接受 auto 或来源运行 ID：$RESUME_FROM" >&2; '
                    'exit 2; fi',
                    '+  if [ "$MODE" != re-execute ] || [ -n "$FULL_SET_REQUEST" ] || [ -n "$UNIT_INHERIT_REQUEST" ] || [ -n "$UNIT_INHERIT_PROPOSE" '
                    '] || [ "$AUDIT_READS" = true ] \\',
                    '+      || [ "$AUDIT_STRICT" = true ] || [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ '
                    '"${#AUDIT_POLICY_ARGS[@]}" -gt 0 ] \\',
                    '+      || [ "$BIND_ONLY" = true ] || [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
                    '+    echo "续跑沿用只与重新执行全集同用，不能与读集审计、读集合同、收窄提案、B-09／B-10、逐单元承接、--bind-only 同用" >&2; exit 2',
                    '+  fi',
                    '+  RESUME_SERIES="${RESUME_SERIES:-$PROFILE}"',
                    '+  if ! [[ "$RESUME_SERIES" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then echo "--resume-series 只允许字母、数字与 ._-：$RESUME_SERIES" '
                    '>&2; exit 2; fi',
                    '+  if [ -n "$RESUME_MAX_AGE" ] && ! [[ "$RESUME_MAX_AGE" =~ ^[0-9]+(\\.[0-9]+)?$ ]]; then echo "--resume-max-age-hours '
                    '是小时数：$RESUME_MAX_AGE" >&2; exit 2; fi',
                    '+  if { [ -n "$RESUME_WAIVE" ] && [ -z "$RESUME_WAIVE_NAMES" ]; } || { [ -z "$RESUME_WAIVE" ] && [ -n "$RESUME_WAIVE_NAMES" ]; '
                    '}; then',
                    '+    echo "环境豁免要同时给理由（--resume-waive-env）和点名的指纹条目（--resume-waive-env-names）" >&2; exit 2',
                    '+  fi',
                    "+  python3 -B -c 'import json, sys; value = json.loads(sys.argv[1]); sys.exit(0 if isinstance(value, dict) else 1)' "
                    '"$RESUME_BINDING_EXTRA" \\',
                    '+    || { echo "--resume-binding-extra 必须是 JSON 对象：$RESUME_BINDING_EXTRA" >&2; exit 2; }',
                    '+elif [ -n "$RESUME_SERIES$RESUME_MAX_AGE$RESUME_WAIVE$RESUME_WAIVE_NAMES" ]; then',
                    '+  echo "--resume-series／--resume-max-age-hours／--resume-waive-env* 只与 --resume-from 同用" >&2; exit 2',
                    '+fi',
                    '-  case "$PROFILE" in entry|full-gates|preflight) ;; *) AUTO_SKIP="组合 ${PROFILE} 没有采集工具测试组" ;; esac',
                    '+  case "$PROFILE" in entry|full-gates|preflight|vc4) ;; *) AUTO_SKIP="组合 ${PROFILE} 没有采集工具测试组" ;; esac',
                    '-  case "$PROFILE" in entry|full-gates|preflight) ;; *) echo "按客户端筛选只用于含采集工具测试组的全量组合（entry、full-gates、preflight）：$PROFILE" >&2; '
                    'exit 2 ;; esac',
                    '+  case "$PROFILE" in entry|full-gates|preflight|vc4) ;; *) echo '
                    '"按客户端筛选只用于含采集工具测试组的全量组合（entry、full-gates、preflight、vc4）：$PROFILE" >&2; exit 2 ;; esac',
                    '+if [ -n "$RESUME_FROM" ]; then',
                    '+  python3 -B - "$OUT/resume-binding.json" codex "$PLAN_PROFILE" "$PRE_A3_MODE" "${DEPLOY:-}" "${TREE_FILES:-}" '
                    '"${DEPLOY_OTHERS:-}" "$RESUME_BINDING_EXTRA" <<\'PYBIND\'',
                    '+import hashlib, json, os, sys',
                    '+out, client, plan_profile, pre_a3, deploy, tool_files, others, extra = sys.argv[1:]',
                    '+binding = {"client": client, "plan_profile": plan_profile, "pre_a3": pre_a3,',
                    '+           "deploy_receipt_sha256": hashlib.sha256(open(deploy, "rb").read()).hexdigest() if deploy else None,',
                    '+           "tool_files_sha256": tool_files or None, "deploy_others": others or None, "extra": json.loads(extra)}',
                    '+fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)',
                    '+with os.fdopen(fd, "w", encoding="utf-8") as handle:',
                    '+    json.dump(binding, handle, ensure_ascii=False, sort_keys=True, indent=1)',
                    '+PYBIND',
                    '+  RESUME_ARGS=(--resume-from "$RESUME_FROM" --resume-series "$RESUME_SERIES" --resume-binding "$OUT/resume-binding.json")',
                    '+  if [ -n "$RESUME_MAX_AGE" ]; then RESUME_ARGS+=(--resume-max-age-hours "$RESUME_MAX_AGE"); fi',
                    '+  if [ -n "$RESUME_WAIVE" ]; then RESUME_ARGS+=(--resume-waive-env "$RESUME_WAIVE" --resume-waive-env-names '
                    '"$RESUME_WAIVE_NAMES"); fi',
                    '+  if python3 -B "$DRV/entry_gates.py" resume-plan --record-store "$STORE" --series "$RESUME_SERIES" --tree "$TREE" > '
                    '"$OUT/resume-plan.json" 2> "$OUT/resume-plan.err"; then',
                    '+    FORCE_ARGS=(--resume-plan "$OUT/resume-plan.json")',
                    '+    RESUME_LAYER2=$(python3 -B -c \'import json, sys; d = json.load(open(sys.argv[1])); print(d["layer"], d.get("base_commit") '
                    'or "-", ",".join(d.get("force_modules") or []) or "-")\' "$OUT/resume-plan.json")',
                    '+    read -r RESUME_LAYER RESUME_BASE RESUME_FORCE <<< "$RESUME_LAYER2"',
                    '+    echo "续跑沿用：系列 ${RESUME_SERIES}，$(python3 -B -c \'import json, sys; '
                    'print("；".join(json.load(open(sys.argv[1]))["reasons"]))\' "$OUT/resume-plan.json")"',
                    '+    if [ "$RESUME_LAYER" = 2 ] && [ "${#SCOPE_ARGS[@]}" -gt 0 ]; then',
                    '+      if [ -z "$SELECT_BASE" ] && [ "$RESUME_BASE" != "-" ]; then SCOPE_ARGS+=(--select-base "$RESUME_BASE"); fi',
                    '+      if [ "$RESUME_FORCE" != "-" ]; then FORCE_ARGS+=(--force-run-modules "$RESUME_FORCE"); fi',
                    '+    fi',
                    '+  else',
                    '+    echo "续跑规划没做出来（忽略，按原样规划；原因 $(tail -n 1 "$OUT/resume-plan.err" | cut -c1-200)）"',
                    '+  fi',
                    '+fi',
                    '-    ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} \\',
                    '+    ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} ${FORCE_ARGS[@]+"${FORCE_ARGS[@]}"} \\',
                    '+if [ -n "$RESUME_FROM" ] && [ "$GO_WARMUP_ACTIVE" = true ]; then',
                    '+  ( cd "$TREE" && "${EXEC_ENV[@]}" python3 -B "$DRV/unit_executor.py" run-gates --out-dir "$OUT/resume-decide" --manifest '
                    '"$OUT/gates-manifest.json" \\',
                    '+      --mode re-execute --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} "${RESUME_ARGS[@]}" --decide-only ) '
                    '> "$OUT/resume-decide.log" 2>&1 < /dev/null || true',
                    '+  if python3 -B - "$OUT/resume-decide/decisions.json" <<\'PYDECIDE\'',
                    '+import json, sys',
                    '+try:',
                    '+    units = json.load(open(sys.argv[1], encoding="utf-8"))["units"]',
                    '+except (OSError, ValueError, KeyError):',
                    '+    sys.exit(1)   # 判定没做出来：保守起见照常预热',
                    '+go = [name for name in ("backend:go-test", "backend:unit", "backend:integration") if name in units]',
                    '+sys.exit(0 if go and all(units[name]["inherit"] for name in go) else 1)',
                    '+PYDECIDE',
                    '+  then GO_WARMUP_ACTIVE=false; echo "续跑：后端 go test 各组都沿用上一轮的通过记录，跳过 Go 预编译预热"; fi',
                    '+fi',
                    '-    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} '
                    '${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ${BIND_ARGS[@]+"${BIND_ARGS[@]}"} ) '
                    '> "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?',
                    '+    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} '
                    '${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} '
                    '${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"} ${BIND_ARGS[@]+"${BIND_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?'],
 'entry-preflight.sh': ['+receipt_not_applicable() {',
                        '+  if [ codex = codex ]; then return 1; fi',
                        '+  printf \'%s\\n\' "本轮客户端 codex 没有这条流程，未做检查" > "$OUT/$1.skipped"',
                        '+  echo "跳过 $1：本轮客户端 codex 不适用（未做检查，不是通过）"',
                        '+  return 0',
                        '+}',
                        '+local_capture_receipt() {',
                        '+  local rc=0 skip=0',
                        '+  opening_skipped local-capture-receipt || skip=$?',
                        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
                        '+  if receipt_not_applicable local-capture-receipt; then return 0; fi',
                        '+  if [ -z "${ENTRY_COMMIT:-}" ]; then echo "参数文件没有 ENTRY_COMMIT（要进入入口门禁的提交），无法核对本地收据"; return 1; fi',
                        '+  python3 -B "$DRV/opening_checks.py" capture-receipt-check --dir "$RUNROOT/local-receipts" --commit "$ENTRY_COMMIT" || '
                        'rc=$?',
                        '+  return "$rc"',
                        '+}',
                        '+tui_prelaunch_receipt() {',
                        '+  local rc=0 skip=0',
                        '+  opening_skipped tui-prelaunch-receipt || skip=$?',
                        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
                        '+  if receipt_not_applicable tui-prelaunch-receipt; then return 0; fi',
                        '+  python3 -B "$DRV/opening_checks.py" tui-receipt-check --dir "$RUNROOT/local-receipts" --version "$TARGET_VERSION" '
                        '--binary-sha256 "${!CT_BINARY_SHA256_KEY}" || rc=$?',
                        '+  return "$rc"',
                        '+}',
                        '+check local-capture-receipt local_capture_receipt',
                        '+check tui-prelaunch-receipt tui_prelaunch_receipt'],
 'env.example.sh': [],
 'lib.sh': ['-  local parameters',
            '-  parameters=$(bash "$DRV/vc5-precheck.sh" --consume --export-parameters) || return 3',
            '+  local parameters rc=0',
            '+  parameters=$(bash "$DRV/vc5-precheck.sh" --consume --export-parameters) || rc=$?',
            '+  if [ "$rc" -ne 0 ]; then',
            '+    echo "VC5_ADMISSION_REQUIRED_FAILED rc=${rc}：准入消费没有通过（原因 JSON 见上一行标准错误）。排查：bash vc5-precheck.sh --dry-run 看 ttl（管理令牌剩余有效期）与 '
            'actions；" \\',
            '+         "令牌快过期用批准件 --apply，或 vc5-precheck.sh --renew --approver \'批准人及依据\'（vc5-all.sh 启动前设 VC5_RENEW_APPROVER 可在 canonical 前自动续签）" '
            '>&2',
            '+    return 3',
            '+  fi',
            '+}',
            '+vc5_admission_guard() {',
            '+  local stage=$1 rc=0 detail',
            '+  detail=$(bash "$DRV/vc5-precheck.sh" --consume 2>&1 >/dev/null) || rc=$?',
            '+  if [ "$rc" -eq 0 ]; then echo "VC5_ADMISSION_GUARD stage=${stage} 准入消费通过"; return 0; fi',
            '+  echo "VC5_ADMISSION_GUARD stage=${stage} 准入消费失败 rc=${rc}：$(printf \'%s\' "$detail" | tail -n 1 | cut -c1-300)"',
            '+  if [ -z "${VC5_RENEW_APPROVER:-}" ]; then',
            '+    echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：没有设置 VC5_RENEW_APPROVER，不自动续签。排查：bash vc5-precheck.sh --dry-run（看 ttl 与 actions）；" '
            '\\',
            '+         "令牌快过期时按批准件 --apply，或 bash vc5-precheck.sh --renew --approver \'批准人及依据\'，然后重起 vc5-all.sh（已完成的阶段会跳过）"',
            '+    return 3',
            '+  fi',
            '+  echo "VC5_ADMISSION_GUARD stage=${stage} 自动续签管理令牌（批准人：${VC5_RENEW_APPROVER}）"',
            '+  rc=0; detail=$(bash "$DRV/vc5-precheck.sh" --renew --approver "$VC5_RENEW_APPROVER" 2>&1 | tail -n 1 | cut -c1-300) || rc=$?',
            '+  if [ "$rc" -ne 0 ] || ! printf \'%s\' "$detail" | grep -q \'"renewed": true\'; then',
            '+    echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：自动续签没有成功：${detail}"',
            '+    return 3',
            '+  fi',
            '+  rc=0; bash "$DRV/vc5-precheck.sh" --consume >/dev/null 2>&1 || rc=$?',
            '+  if [ "$rc" -ne 0 ]; then echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：续签后准入消费仍不通过 rc=${rc}"; return 3; fi',
            '+  echo "VC5_ADMISSION_GUARD stage=${stage} 续签成功，准入消费通过"',
            '-  ( cd "$tree/$workdir" && unset CODEX_UPGRADE_IDENTITY_MEMO && export PYTHONPYCACHEPREFIX="$pyc" '
            'CODEX_SPEC_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \\',
            '+  ( cd "$tree/$workdir" && unset CODEX_UPGRADE_IDENTITY_MEMO ENTRY_COMMIT ENTRY_BUNDLE ENTRY_BRANCH && export '
            'PYTHONPYCACHEPREFIX="$pyc" CODEX_SPEC_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \\'],
 'vc5-all.sh': ['+vc5_stage() {',
                '+  local stage=$1 pattern=$2 rc=0 raw',
                '+  shift 2',
                '+  raw="$RUNROOT/vc5-all-${stage}.raw"',
                '+  : > "$raw"; chmod 600 "$raw"',
                '+  set +e',
                '+  "$@" 2>&1 | tee "$raw" | { grep -E "${pattern}|VC5_ADMISSION|VC5_ALL_" || true; } | cut -c1-300',
                '+  rc=${PIPESTATUS[0]}',
                '+  set -e',
                '+  if [ "$rc" -ne 0 ]; then',
                '+    echo "VC5_ALL_STAGE_FAILED stage=${stage} rc=${rc} raw=${raw}（未过滤的完整输出）；末 8 行："',
                '+    tail -n 8 "$raw" | cut -c1-300',
                '+    return "$rc"',
                '+  fi',
                '+}',
                '-  python3 -c "import json; d=json.load(open(\'$G/local/full-regression.gate.json\')); assert d[\'exit_code\']==0 and '
                "d['tree_head']=='$DC', d; print('full-regression(本机):', d['exit_code'], d['host'], d['architecture'], d['started_at_utc'], "
                'd[\'completed_at_utc\'])"',
                '+  python3 -c "import json; d=json.load(open(\'$G/local/full-regression.gate.json\')); assert d[\'exit_code\']==0 and '
                "d['tree_head']=='$DC', d; assert d.get('client_selection') is None or '${VC5_ALLOW_SUBSET_LOCAL_GATES:-}'=='1', '本地 full-regression "
                "是按客户端筛选的子集（client_selection），不是 make test 全集；确认接受请设 VC5_ALLOW_SUBSET_LOCAL_GATES=1'; print('full-regression(本机):', d['exit_code'], "
                'd[\'host\'], d[\'architecture\'], d[\'started_at_utc\'], d[\'completed_at_utc\'])"',
                '-  echo "=== seal 链 $(utc_now)"; bash "$DRV/vc5-seal.sh" "$ATT" 2>&1 | { grep -E "===|ATT=|batch ok|BATCH '
                'FAILED|REHEARSAL|预演:|CKPT=|SEAL_SHA=|candidate '
                'result|compare:|账本|SEAL_DONE|SEAL_ABORT|PERMISSION_CLOSEOUT|Error|Traceback|拒绝|失败|assert" || true; } | cut -c1-300',
                '+  echo "=== seal 链 $(utc_now)"; vc5_stage seal "===|ATT=|batch ok|BATCH FAILED|REHEARSAL|预演:|CKPT=|SEAL_SHA=|candidate '
                'result|compare:|账本|SEAL_DONE|SEAL_ABORT|PERMISSION_CLOSEOUT|Error|Traceback|拒绝|失败|assert" bash "$DRV/vc5-seal.sh" "$ATT"',
                '-  echo "=== accept $(utc_now)"; bash "$DRV/vc5-accept.sh" "$ATT" 2>&1 | { grep -E '
                '"===|assertions:|evaluation-run:|target-platform|归档|replay:|batch ok|BATCH '
                'FAILED|acceptance:|账本|ACCEPT_DONE|Error|Traceback|拒绝|失败|缺少" || true; } | cut -c1-300',
                '+  echo "=== accept $(utc_now)"; vc5_stage accept "===|assertions:|evaluation-run:|target-platform|归档|replay:|batch ok|BATCH '
                'FAILED|acceptance:|账本|ACCEPT_DONE|Error|Traceback|拒绝|失败|缺少" bash "$DRV/vc5-accept.sh" "$ATT"',
                '-  echo "=== canonical 交接 $(utc_now)"; bash "$DRV/vc5-canonical2.sh" "$ATT" 2>&1 | { grep -E "review_sha256|CLI preview|一致|plan '
                '->|===|rc=|actions:|checkpoint|import-receipt|canonical '
                'latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败" || true; } | cut -c1-300',
                '+  vc5_admission_guard canonical',
                '+  echo "=== canonical 交接 $(utc_now)"; vc5_stage canonical "review_sha256|CLI preview|一致|plan '
                '->|===|rc=|actions:|checkpoint|import-receipt|canonical '
                'latest|vc5-completion|账本|CANONICAL2_DONE|CANONICAL2_SKIP|Traceback|Error|assert|拒绝|失败" bash "$DRV/vc5-canonical2.sh" "$ATT"'],
 'vc5-gate-target.sh': ["-set +e; env -u CODEX_UPGRADE_IDENTITY_MEMO unshare -m --propagation private bash -c 'mount -t tmpfs -o "
                        'ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test\' > "$GATE/logs/target-platform.stdout.log" 2> '
                        '"$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)',
                        '+set +e; env -u CODEX_UPGRADE_IDENTITY_MEMO -u ENTRY_COMMIT -u ENTRY_BUNDLE -u ENTRY_BRANCH unshare -m --propagation '
                        "private bash -c 'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test' > "
                        '"$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)'],
 'vc5-precheck.sh': ['-  --dry-run|--apply|--consume) exec python3 -B "$(dirname "${BASH_SOURCE[0]}")/vc5_admission.py" "$@" ;;',
                     '+  --dry-run|--apply|--consume|--renew) exec python3 -B "$(dirname "${BASH_SOURCE[0]}")/vc5_admission.py" "$@" ;;']}
# <<< T2 预检登记常量

# >>> 收尾登记常量（自动生成，勿手改）
CLOSEOUT_SH_1_BASELINE = '5fcdf0b6b6ba847e86f42452c975cd841832e606'
CLOSEOUT_SH_1_DIGESTS = {'entry-gates.sh': 'f80292303f9970da87a735b1d63f247e74b81dc7d544393054832a0b2db57186'}
CLOSEOUT_SH_1_RESIDUALS = {'entry-gates.sh': ['-usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|pre-a3|regression] [--go-warmup auto|off] [--parallel <正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
        '-PROFILE=entry; OUT=""; WORK="$(dirname "$D")/entry-gates-work"; PYC=""; PRE_A3_SOURCE=auto; MODE=re-execute; STORE="$(dirname "$D")/unit-records"; AUDIT_READS=false',
        '+usage() { echo "用法：bash entry-gates.sh [--profile entry|full-gates|preflight|vc4|pre-a3|regression] [--go-warmup auto|off] [--parallel <正整数>] [--with-gates <门禁项,…>] [--require-deployed|--require-deployed-all] [--out <目录>] [--work <目录>] [--pycache <目录>] [--policy-activation <文件>] [--pre-a3-certification <文件>] [--pre-a3-mode auto|present|run] [--pre-a3-staging-parent <绝对目录，默认 $D/staging/pre-a3-scenarios>] [--mode full-set-pass|re-execute|unit-inherit] [--full-set-request <JSON>] [--unit-inherit-request <JSON>|--unit-inherit-propose <新目录>] [--unit-inherit-stage <阶段>] [--record-store <目录>] [--audit-reads] [--audit-strict] [--audit-host-inputs <JSON>] [--audit-policy <JSON>] [--input-scope <提案>] [--declared-sample off|always|every:N] [--bind-only] [--gate-scope full|client-plan|auto [--select-base <40 位提交>] [--include-claude]] [--resume-from auto|<运行ID> [--resume-series <系列>] [--resume-binding-extra <JSON>] [--resume-max-age-hours <小时>] [--resume-waive-env <理由> --resume-waive-env-names <条目,…>]] <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录绝对路径>]" >&2; }',
        '+PROFILE=entry; OUT=""; WORK="$(dirname "$D")/entry-gates-work"; PYC=""; PRE_A3_SOURCE=auto; PRE_A3_STAGING_PARENT=""; MODE=re-execute; STORE="$(dirname "$D")/unit-records"; AUDIT_READS=false',
        '+BIND_ONLY=false; BIND_ARGS=()',
        '+DECLARED_SAMPLE=off',
        '+GATE_SCOPE=full; SELECT_BASE=""; INCLUDE_CLAUDE=false; SCOPE_ARGS=()',
        '+RESUME_FROM=""; RESUME_SERIES=""; RESUME_BINDING_EXTRA="{}"; RESUME_MAX_AGE=""; RESUME_WAIVE=""; RESUME_WAIVE_NAMES=""; RESUME_ARGS=(); FORCE_ARGS=()',
        '+    --bind-only) BIND_ONLY=true; BIND_ARGS=(--decide-only); shift ;;',
        '+    --declared-sample) DECLARED_SAMPLE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --gate-scope) GATE_SCOPE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --select-base) SELECT_BASE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --include-claude) INCLUDE_CLAUDE=true; shift ;;',
        '+    --resume-from) RESUME_FROM="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --resume-series) RESUME_SERIES="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --resume-binding-extra) RESUME_BINDING_EXTRA="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --resume-max-age-hours) RESUME_MAX_AGE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --resume-waive-env) RESUME_WAIVE="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --resume-waive-env-names) RESUME_WAIVE_NAMES="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '+    --pre-a3-staging-parent) PRE_A3_STAGING_PARENT="${2:-}"; shift 2 || { usage; exit 2; } ;;',
        '-case "$PROFILE" in entry|full-gates|preflight|pre-a3|regression) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac',
        '+case "$PROFILE" in entry|full-gates|preflight|vc4|pre-a3|regression) ;; *) echo "未知的门禁组合：$PROFILE" >&2; exit 2 ;; esac',
        '+if ! [[ "$DECLARED_SAMPLE" =~ ^(off|always|every:[1-9][0-9]*)$ ]]; then echo "--declared-sample 只接受 off、always 或 every:N（N 为正整数）：$DECLARED_SAMPLE" >&2; exit 2; fi',
        '+fi',
        '+if [ -n "$RESUME_FROM" ] && [ -z "$RESUME_WAIVE$RESUME_WAIVE_NAMES" ]; then',
        '+  RESUME_WAIVE="${ENTRY_RESUME_WAIVE_ENV:-}"; RESUME_WAIVE_NAMES="${ENTRY_RESUME_WAIVE_ENV_NAMES:-}"',
        '+fi',
        '+if [ -n "$RESUME_FROM" ]; then',
        '+  if ! [[ "$RESUME_FROM" =~ ^(auto|[0-9A-Za-z._-]{1,128})$ ]]; then echo "--resume-from 只接受 auto 或来源运行 ID：$RESUME_FROM" >&2; exit 2; fi',
        '+  if [ "$MODE" != re-execute ] || [ -n "$FULL_SET_REQUEST" ] || [ -n "$UNIT_INHERIT_REQUEST" ] || [ -n "$UNIT_INHERIT_PROPOSE" ] || [ "$AUDIT_READS" = true ] \\',
        '+      || [ "$AUDIT_STRICT" = true ] || [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ "${#AUDIT_POLICY_ARGS[@]}" -gt 0 ] \\',
        '+      || [ "$BIND_ONLY" = true ] || [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
        '+    echo "续跑沿用只与重新执行全集同用，不能与读集审计、读集合同、收窄提案、B-09／B-10、逐单元承接、--bind-only 同用" >&2; exit 2',
        '+  fi',
        '+  RESUME_SERIES="${RESUME_SERIES:-$PROFILE}"',
        '+  if ! [[ "$RESUME_SERIES" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then echo "--resume-series 只允许字母、数字与 ._-：$RESUME_SERIES" >&2; exit 2; fi',
        '+  if [ -n "$RESUME_MAX_AGE" ] && ! [[ "$RESUME_MAX_AGE" =~ ^[0-9]+(\\.[0-9]+)?$ ]]; then echo "--resume-max-age-hours 是小时数：$RESUME_MAX_AGE" >&2; exit 2; fi',
        '+  if { [ -n "$RESUME_WAIVE" ] && [ -z "$RESUME_WAIVE_NAMES" ]; } || { [ -z "$RESUME_WAIVE" ] && [ -n "$RESUME_WAIVE_NAMES" ]; }; then',
        '+    echo "环境豁免要同时给理由（--resume-waive-env）和点名的指纹条目（--resume-waive-env-names）" >&2; exit 2',
        '+  fi',
        '+  python3 -B -c \'import json, sys; value = json.loads(sys.argv[1]); sys.exit(0 if isinstance(value, dict) else 1)\' "$RESUME_BINDING_EXTRA" \\',
        '+    || { echo "--resume-binding-extra 必须是 JSON 对象：$RESUME_BINDING_EXTRA" >&2; exit 2; }',
        '+elif [ -n "$RESUME_SERIES$RESUME_MAX_AGE$RESUME_WAIVE$RESUME_WAIVE_NAMES" ]; then',
        '+  echo "--resume-series／--resume-max-age-hours／--resume-waive-env* 只与 --resume-from 同用" >&2; exit 2',
        '+fi',
        '+case "$GATE_SCOPE" in full|client-plan|auto) ;; *) echo "未知的门禁范围：${GATE_SCOPE}（只有 full、client-plan、auto）" >&2; exit 2 ;; esac',
        '+if [ "$GATE_SCOPE" = full ]; then',
        '+  if [ -n "$SELECT_BASE" ] || [ "$INCLUDE_CLAUDE" = true ]; then echo "--select-base、--include-claude 只在 --gate-scope client-plan／auto 下有意义" >&2; exit 2; fi',
        '+elif [ "$GATE_SCOPE" = auto ]; then',
        '+  if [ -n "$SELECT_BASE" ] && ! [[ "$SELECT_BASE" =~ ^[0-9a-f]{40}$ ]]; then echo "--select-base 必须是 40 位小写提交：$SELECT_BASE" >&2; exit 2; fi',
        '+  AUTO_SKIP=""',
        '+  case "$PROFILE" in entry|full-gates|preflight|vc4) ;; *) AUTO_SKIP="组合 ${PROFILE} 没有采集工具测试组" ;; esac',
        '+  if [ codex != codex ]; then AUTO_SKIP="本版只启用 codex 计划"; fi',
        '+  if [ "$MODE" != re-execute ] || [ -n "$FULL_SET_REQUEST" ] || [ -n "$UNIT_INHERIT_REQUEST" ] || [ -n "$UNIT_INHERIT_PROPOSE" ]; then',
        '+    AUTO_SKIP="模式或请求要求全集（全集通过、逐单元承接）"',
        '+  fi',
        '+  if [ "$AUDIT_READS" = true ] || [ "$AUDIT_STRICT" = true ] || [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ "${#AUDIT_POLICY_ARGS[@]}" -gt 0 ] \\',
        '+      || [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
        '+    AUTO_SKIP="读集审计、读集合同、收窄提案或固定79接线在用（这些走全集）"',
        '+  fi',
        '+  if [ -n "$AUTO_SKIP" ]; then',
        '+    echo "按客户端筛选（auto）未启用，保持全集：${AUTO_SKIP}"',
        '+  else',
        '+    SCOPE_ARGS=(--gate-scope auto --client codex)',
        '+    if [ -n "$SELECT_BASE" ]; then SCOPE_ARGS+=(--select-base "$SELECT_BASE"); fi',
        '+    if [ "$INCLUDE_CLAUDE" = true ]; then SCOPE_ARGS+=(--include-claude); fi',
        '+  fi',
        '+else',
        '+  if ! [[ "$SELECT_BASE" =~ ^[0-9a-f]{40}$ ]]; then echo "--gate-scope client-plan 必须同时给 --select-base（40 位小写提交）：$SELECT_BASE" >&2; exit 2; fi',
        '+  case "$PROFILE" in entry|full-gates|preflight|vc4) ;; *) echo "按客户端筛选只用于含采集工具测试组的全量组合（entry、full-gates、preflight、vc4）：$PROFILE" >&2; exit 2 ;; esac',
        '+  if [ "$MODE" != re-execute ] || [ -n "$FULL_SET_REQUEST" ] || [ -n "$UNIT_INHERIT_REQUEST" ] || [ -n "$UNIT_INHERIT_PROPOSE" ]; then',
        '+    echo "按客户端筛选只许 --mode re-execute，不能与全集请求、逐单元承接同用（子集不能承接，也不能冒充全集）" >&2; exit 2',
        '+  fi',
        '+  if [ "$AUDIT_READS" = true ] || [ "$AUDIT_STRICT" = true ] || [ -n "$AUDIT_HOST_INPUTS" ] || [ "${#INPUT_SCOPE_ARGS[@]}" -gt 0 ] || [ "${#AUDIT_POLICY_ARGS[@]}" -gt 0 ] \\',
        '+      || [ -n "${CLAUDE_ENTRY_FULL79_SPEC:-}${CLAUDE_ENTRY_FULL79_SPEC_SHA256:-}" ]; then',
        '+    echo "按客户端筛选不能与读集审计、读集合同、收窄提案、固定79接线同用（这些走全集）" >&2; exit 2',
        '+  fi',
        '+  SCOPE_ARGS=(--gate-scope client-plan --select-base "$SELECT_BASE" --client codex)',
        '+  if [ "$INCLUDE_CLAUDE" = true ]; then SCOPE_ARGS+=(--include-claude); fi',
        '+  elif [ "$MODE" = unit-inherit ] && [ codex = claude ]; then',
        '+    verify_history_tree "$TREE" || { echo "ENTRY_GATES_ABORTED：原测试树历史不合合同"; exit 3; }',
        '+    if python3 -B "$DRV/entry_gates.py" prepare-scope-successor --tree "$TREE" --bytecode-cache "$PYC" \\',
        '+        --commit "$PCOMMIT" --bundle "$PBUNDLE" --branch "$PBRANCH" --lock-directory "$LOCK" --lock-owner "$$" \\',
        '+        > "$OUT/workspace-successor.json"; then',
        '+      REUSE_WORKSPACE=true',
        '+    else',
        '+      echo "ENTRY_GATES_ABORTED：后继不满足原位内容更新条件；保留原树和缓存，不派发单元"; exit 3',
        '+    fi',
        '+    PRE_A3_PARENT="${PRE_A3_STAGING_PARENT:-$D/staging/pre-a3-scenarios}"',
        '+    if [[ "$PRE_A3_PARENT" != /* ]]; then echo "ENTRY_GATES_ABORTED：--pre-a3-staging-parent 必须是绝对路径：${PRE_A3_PARENT}" >&2; exit 3; fi',
        '-    PA3_PARENT=$(python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification plan --staging-parent "$D/staging/pre-a3-scenarios" --output "$UNITS" \\',
        '+    PA3_PARENT=$(python3 -m tools.official_client_capture.codex_upgrade_pre_a3_certification plan --staging-parent "$PRE_A3_PARENT" --output "$UNITS" \\',
        '-EXEC_ENV+=(PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "PYTHONPYCACHEPREFIX=$PYC" "CODEX_0_149_1_SOURCE_ROOT=$HISTORICAL_SOURCE_ROOT" "CAPTURE_TYPESCRIPT_MODULE=$TS")',
        '+if [ -n "${UNIT_EXECUTOR_RETRY_LEDGER:-}" ]; then EXEC_ENV+=("UNIT_EXECUTOR_RETRY_LEDGER=$UNIT_EXECUTOR_RETRY_LEDGER"); fi',
        '+EXEC_ENV+=(PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 "PYTHONPYCACHEPREFIX=$PYC" "CODEX_SPEC_SOURCE_ROOT=$HISTORICAL_SOURCE_ROOT" "CAPTURE_TYPESCRIPT_MODULE=$TS")',
        '+if [ -n "$RESUME_FROM" ]; then',
        '+  python3 -B - "$OUT/resume-binding.json" codex "$PLAN_PROFILE" "$PRE_A3_MODE" "${DEPLOY:-}" "${TREE_FILES:-}" "${DEPLOY_OTHERS:-}" "$RESUME_BINDING_EXTRA" <<\'PYBIND\'',
        '+import hashlib, json, os, sys',
        '+out, client, plan_profile, pre_a3, deploy, tool_files, others, extra = sys.argv[1:]',
        '+binding = {"client": client, "plan_profile": plan_profile, "pre_a3": pre_a3,',
        '+           "deploy_receipt_sha256": hashlib.sha256(open(deploy, "rb").read()).hexdigest() if deploy else None,',
        '+           "tool_files_sha256": tool_files or None, "deploy_others": others or None, "extra": json.loads(extra)}',
        '+fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)',
        '+with os.fdopen(fd, "w", encoding="utf-8") as handle:',
        '+    json.dump(binding, handle, ensure_ascii=False, sort_keys=True, indent=1)',
        '+PYBIND',
        '+  RESUME_ARGS=(--resume-from "$RESUME_FROM" --resume-series "$RESUME_SERIES" --resume-binding "$OUT/resume-binding.json")',
        '+  if [ -n "$RESUME_MAX_AGE" ]; then RESUME_ARGS+=(--resume-max-age-hours "$RESUME_MAX_AGE"); fi',
        '+  if [ -n "$RESUME_WAIVE" ]; then RESUME_ARGS+=(--resume-waive-env "$RESUME_WAIVE" --resume-waive-env-names "$RESUME_WAIVE_NAMES"); fi',
        '+  if python3 -B "$DRV/entry_gates.py" resume-plan --record-store "$STORE" --series "$RESUME_SERIES" --tree "$TREE" > "$OUT/resume-plan.json" 2> "$OUT/resume-plan.err"; then',
        '+    FORCE_ARGS=(--resume-plan "$OUT/resume-plan.json")',
        '+    RESUME_LAYER2=$(python3 -B -c \'import json, sys; d = json.load(open(sys.argv[1])); print(d["layer"], d.get("base_commit") or "-", ",".join(d.get("force_modules") or []) or "-")\' "$OUT/resume-plan.json")',
        '+    read -r RESUME_LAYER RESUME_BASE RESUME_FORCE <<< "$RESUME_LAYER2"',
        '+    echo "续跑沿用：系列 ${RESUME_SERIES}，$(python3 -B -c \'import json, sys; print("；".join(json.load(open(sys.argv[1]))["reasons"]))\' "$OUT/resume-plan.json")"',
        '+    if [ "$RESUME_LAYER" = 2 ] && [ "${#SCOPE_ARGS[@]}" -gt 0 ]; then',
        '+      if [ -z "$SELECT_BASE" ] && [ "$RESUME_BASE" != "-" ]; then SCOPE_ARGS+=(--select-base "$RESUME_BASE"); fi',
        '+      if [ "$RESUME_FORCE" != "-" ]; then FORCE_ARGS+=(--force-run-modules "$RESUME_FORCE"); fi',
        '+    fi',
        '+  else',
        '+    echo "续跑规划没做出来（忽略，按原样规划；原因 $(tail -n 1 "$OUT/resume-plan.err" | cut -c1-200)）"',
        '+  fi',
        '+fi',
        '+    ${SCOPE_ARGS[@]+"${SCOPE_ARGS[@]}"} ${FORCE_ARGS[@]+"${FORCE_ARGS[@]}"} \\',
        '+if [ -n "$RESUME_FROM" ] && [ "$GO_WARMUP_ACTIVE" = true ]; then',
        '+  ( cd "$TREE" && "${EXEC_ENV[@]}" python3 -B "$DRV/unit_executor.py" run-gates --out-dir "$OUT/resume-decide" --manifest "$OUT/gates-manifest.json" \\',
        '+      --mode re-execute --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} "${RESUME_ARGS[@]}" --decide-only ) > "$OUT/resume-decide.log" 2>&1 < /dev/null || true',
        '+  if python3 -B - "$OUT/resume-decide/decisions.json" <<\'PYDECIDE\'',
        '+import json, sys',
        '+try:',
        '+    units = json.load(open(sys.argv[1], encoding="utf-8"))["units"]',
        '+except (OSError, ValueError, KeyError):',
        '+    sys.exit(1)   # 判定没做出来：保守起见照常预热',
        '+go = [name for name in ("backend:go-test", "backend:unit", "backend:integration") if name in units]',
        '+sys.exit(0 if go and all(units[name]["inherit"] for name in go) else 1)',
        '+PYDECIDE',
        '+  then GO_WARMUP_ACTIVE=false; echo "续跑：后端 go test 各组都沿用上一轮的通过记录，跳过 Go 预编译预热"; fi',
        '+fi',
        '+if [ "$AUDIT_READS" = true ]; then AUDIT_ARGS+=(--identity-memo off); fi',
        '+if [ "$DECLARED_SAMPLE" != off ]; then AUDIT_ARGS+=(--declared-sample "$DECLARED_SAMPLE"); echo "声明级采样：--declared-sample ${DECLARED_SAMPLE}（声明级命令单元按此取读集证据）"; fi',
        '-    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} ${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?',
        '+    --mode "$MODE" --record-store "$STORE" ${PARALLEL_ARGS[@]+"${PARALLEL_ARGS[@]}"} ${AUDIT_ARGS[@]+"${AUDIT_ARGS[@]}"} ${FULL_SET_ARGS[@]+"${FULL_SET_ARGS[@]}"} ${UNIT_INHERIT_ARGS[@]+"${UNIT_INHERIT_ARGS[@]}"} ${RESUME_ARGS[@]+"${RESUME_ARGS[@]}"} ${BIND_ARGS[@]+"${BIND_ARGS[@]}"} ) > "$OUT/executor.log" 2>&1 < /dev/null || EXEC_RC=$?',
        '+if [ "$BIND_ONLY" = true ]; then',
        '+  if [ "$EXEC_RC" -ne 0 ] || [ ! -f "$OUT/executor/binding.json" ]; then',
        '+    echo "ENTRY_GATES_ABORTED：运行绑定没算出来（rc=${EXEC_RC}）；日志 ${OUT}/executor.log"; exit 3',
        '+  fi',
        '+  rm -rf "$TREE" "$PYC"',
        '+  echo "ENTRY_GATES_BIND_ONLY ${OUT}/executor/binding.json"',
        '+  exit 0',
        '+fi']}
CLOSEOUT_SH_2_BASELINE = '6d8e73b95d06bc66cd7fcf74e9d03297bb25b5df'
CLOSEOUT_SH_2_DIGESTS = {'entry-preflight.sh': '9f4417a012a9bb5e4d572f7018e6f8cbca88d640dba2dd130fbba87a647f22b3'}
CLOSEOUT_SH_2_RESIDUALS = {'entry-preflight.sh': ['+  rm -f "$OUT/$name.skipped"',
        '-  printf \'%s %s\\n\' "$name" "$([ "$rc" = 0 ] && echo passed || echo failed)" >> "$OUT/results.txt"',
        '-  if [ "$rc" = 0 ]; then',
        '+  local status=failed',
        '+  if [ "$rc" = 0 ]; then if [ -f "$OUT/$name.skipped" ]; then status=skipped; else status=passed; fi; fi',
        '+  printf \'%s %s\\n\' "$name" "$status" >> "$OUT/results.txt"',
        '+  if [ "$status" = skipped ]; then',
        '+    echo "已跳过 ${name}（未做检查，不是通过；理由见 $OUT/$name.skipped）"',
        '+  elif [ "$rc" = 0 ]; then',
        '+opening_skipped() {',
        '+  case ",${OPENING_CHECKS_SKIP:-}," in *",$1,"*) ;; *) return 1 ;; esac',
        '+  local reason="${OPENING_CHECKS_SKIP_REASON:-}"',
        '+  if [ -z "${reason//[[:space:]]/}" ]; then',
        '+    echo "拒绝跳过 $1：OPENING_CHECKS_SKIP 里列了它，却没写 OPENING_CHECKS_SKIP_REASON（理由必填）；这一项按未通过处理"',
        '+    return 2',
        '+  fi',
        '+  printf \'%s\\n\' "$reason" > "$OUT/$1.skipped"',
        '+  echo "已按 OPENING_CHECKS_SKIP 显式跳过 $1（未做检查，不是通过）；理由：$reason"',
        '+  return 0',
        '+}',
        '+model_availability() {',
        '+  local rc=0 skip=0',
        '+  opening_skipped model-availability || skip=$?',
        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
        '+  python3 -B "$DRV/opening_checks.py" models --models "${MAIN_MODEL},${LITE_MODEL}" ${MODEL_PROBE_MINIMAL:+--minimal} || rc=$?',
        '+  if [ "$rc" = 3 ]; then echo "模型可用性探测未配置：设 MODEL_PROBE_URL 与 MODEL_PROBE_KEY_FILE 后重跑；确需跳过请显式 OPENING_CHECKS_SKIP=model-availability 并同时写 OPENING_CHECKS_SKIP_REASON"; fi',
        '+  return "$rc"',
        '+}',
        '+main_security_scan() {',
        '+  local rc=0 skip=0',
        '+  opening_skipped main-security-scan || skip=$?',
        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
        '+  python3 -B "$DRV/opening_checks.py" security-scan ${SECURITY_SCAN_REPO:+--repo "$SECURITY_SCAN_REPO"} || rc=$?',
        '+  if [ "$rc" = 3 ]; then echo "Security Scan 核对未配置：设 SECURITY_SCAN_REPO=owner/name（并保证 gh 已登录）后重跑；确需跳过请显式 OPENING_CHECKS_SKIP=main-security-scan 并同时写 OPENING_CHECKS_SKIP_REASON"; fi',
        '+  return "$rc"',
        '+}',
        '+receipt_not_applicable() {',
        '+  if [ codex = codex ]; then return 1; fi',
        '+  printf \'%s\\n\' "本轮客户端 codex 没有这条流程，未做检查" > "$OUT/$1.skipped"',
        '+  echo "跳过 $1：本轮客户端 codex 不适用（未做检查，不是通过）"',
        '+  return 0',
        '+}',
        '+local_capture_receipt() {',
        '+  local rc=0 skip=0',
        '+  opening_skipped local-capture-receipt || skip=$?',
        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
        '+  if receipt_not_applicable local-capture-receipt; then return 0; fi',
        '+  if [ -z "${ENTRY_COMMIT:-}" ]; then echo "参数文件没有 ENTRY_COMMIT（要进入入口门禁的提交），无法核对本地收据"; return 1; fi',
        '+  python3 -B "$DRV/opening_checks.py" capture-receipt-check --dir "$RUNROOT/local-receipts" --commit "$ENTRY_COMMIT" || rc=$?',
        '+  return "$rc"',
        '+}',
        '+tui_prelaunch_receipt() {',
        '+  local rc=0 skip=0',
        '+  opening_skipped tui-prelaunch-receipt || skip=$?',
        '+  if [ "$skip" = 0 ]; then return 0; elif [ "$skip" = 2 ]; then return 1; fi',
        '+  if receipt_not_applicable tui-prelaunch-receipt; then return 0; fi',
        '+  python3 -B "$DRV/opening_checks.py" tui-receipt-check --dir "$RUNROOT/local-receipts" --version "$TARGET_VERSION" --binary-sha256 "${!CT_BINARY_SHA256_KEY}" || rc=$?',
        '+  return "$rc"',
        '+}',
        '-python3 -B - "$OUT/results.txt" "$OUT/summary.json" <<\'PY\'',
        '-import json, sys',
        '+check model-availability model_availability',
        '+check main-security-scan main_security_scan',
        '+check local-capture-receipt local_capture_receipt',
        '+check tui-prelaunch-receipt tui_prelaunch_receipt',
        '+python3 -B - "$OUT/results.txt" "$OUT/summary.json" "$OUT" <<\'PY\'',
        '+import json, os, sys',
        '-failed = [name for name, status in checks.items() if status != "passed"]',
        '+failed = [name for name, status in checks.items() if status not in ("passed", "skipped")]',
        '+skipped = {}',
        '+for name, status in checks.items():',
        '+    if status == "skipped":',
        '+        with open(os.path.join(sys.argv[3], name + ".skipped"), encoding="utf-8") as handle:',
        '+            skipped[name] = handle.read().strip()',
        '-           "checks": checks, "failed": failed}',
        '+           "checks": checks, "failed": failed, "skipped": skipped}',
        '+  if grep -q \' skipped$\' "$OUT/results.txt"; then echo "注意：有被显式跳过的检查项（未做检查，不是通过）：$(grep \' skipped$\' "$OUT/results.txt" | cut -d\' \' -f1 | tr \'\\n\' \' \')"; fi']}
CLOSEOUT_SH_3_BASELINE = 'c14cba3cd62d7ea6bcd02b4abf43e0b19f460ad7'
CLOSEOUT_SH_3_DIGESTS = {'env.example.sh': '9b14a35fe251cd604dcafd4ba5fa75e19bf141b65512a5f782a261b9efad61f3'}
CLOSEOUT_SH_3_RESIDUALS = {'env.example.sh': []}
CLOSEOUT_SH_4_BASELINE = '3ab5afea9da94d68efdeebafbd7a6542e21ea3e4'
CLOSEOUT_SH_4_DIGESTS = {'vc0-gate-target.sh': 'b1d173a477b03009cfa6abde600bc81953817ebf1d72a7038b2fe1675a2fb94d'}
CLOSEOUT_SH_4_RESIDUALS = {'vc0-gate-target.sh': ['-bash "$DRV/entry-gates.sh" --profile preflight --out "$OUT" --work "$PRE" --pycache "$PYC" "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$NM_DIR" || RC=$?',
        '+GATE_SCOPE_ARGS=(); if [ codex = codex ]; then GATE_SCOPE_ARGS=(--gate-scope auto); fi',
        '+bash "$DRV/entry-gates.sh" --profile preflight --out "$OUT" --work "$PRE" --pycache "$PYC" ${GATE_SCOPE_ARGS[@]+"${GATE_SCOPE_ARGS[@]}"} "$PBUNDLE" "$PBRANCH" "$PCOMMIT" "$NM_DIR" || RC=$?']}
CLOSEOUT_SH_5_BASELINE = 'f9fd48826c9ddc47376a57beb65328cc35cca180'
CLOSEOUT_SH_5_DIGESTS = {'lib.sh': 'da83d97f499b8e01d230465641ad4393dbf842684b9f9f4804847aa5b2767ce5'}
CLOSEOUT_SH_5_RESIDUALS = {'lib.sh': ['-  local parameters',
        '-  parameters=$(bash "$DRV/vc5-precheck.sh" --consume --export-parameters) || return 3',
        '+  local parameters rc=0',
        '+  parameters=$(bash "$DRV/vc5-precheck.sh" --consume --export-parameters) || rc=$?',
        '+  if [ "$rc" -ne 0 ]; then',
        '+    echo "VC5_ADMISSION_REQUIRED_FAILED rc=${rc}：准入消费没有通过（原因 JSON 见上一行标准错误）。排查：bash vc5-precheck.sh --dry-run 看 ttl（管理令牌剩余有效期）与 actions；" \\',
        '+         "令牌快过期用批准件 --apply，或 vc5-precheck.sh --renew --approver \'批准人及依据\'（vc5-all.sh 启动前设 VC5_RENEW_APPROVER 可在 canonical 前自动续签）" >&2',
        '+    return 3',
        '+  fi',
        '+}',
        '+vc5_admission_guard() {',
        '+  local stage=$1 rc=0 detail',
        '+  detail=$(bash "$DRV/vc5-precheck.sh" --consume 2>&1 >/dev/null) || rc=$?',
        '+  if [ "$rc" -eq 0 ]; then echo "VC5_ADMISSION_GUARD stage=${stage} 准入消费通过"; return 0; fi',
        '+  echo "VC5_ADMISSION_GUARD stage=${stage} 准入消费失败 rc=${rc}：$(printf \'%s\' "$detail" | tail -n 1 | cut -c1-300)"',
        '+  if [ -z "${VC5_RENEW_APPROVER:-}" ]; then',
        '+    echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：没有设置 VC5_RENEW_APPROVER，不自动续签。排查：bash vc5-precheck.sh --dry-run（看 ttl 与 actions）；" \\',
        '+         "令牌快过期时按批准件 --apply，或 bash vc5-precheck.sh --renew --approver \'批准人及依据\'，然后重起 vc5-all.sh（已完成的阶段会跳过）"',
        '+    return 3',
        '+  fi',
        '+  echo "VC5_ADMISSION_GUARD stage=${stage} 自动续签管理令牌（批准人：${VC5_RENEW_APPROVER}）"',
        '+  rc=0; detail=$(bash "$DRV/vc5-precheck.sh" --renew --approver "$VC5_RENEW_APPROVER" 2>&1 | tail -n 1 | cut -c1-300) || rc=$?',
        '+  if [ "$rc" -ne 0 ] || ! printf \'%s\' "$detail" | grep -q \'"renewed": true\'; then',
        '+    echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：自动续签没有成功：${detail}"',
        '+    return 3',
        '+  fi',
        '+  rc=0; bash "$DRV/vc5-precheck.sh" --consume >/dev/null 2>&1 || rc=$?',
        '+  if [ "$rc" -ne 0 ]; then echo "VC5_ADMISSION_GUARD_FAILED stage=${stage}：续签后准入消费仍不通过 rc=${rc}"; return 3; fi',
        '+  echo "VC5_ADMISSION_GUARD stage=${stage} 续签成功，准入消费通过"',
        '-  ( cd "$tree/$workdir" && unset CODEX_UPGRADE_IDENTITY_MEMO && export PYTHONPYCACHEPREFIX="$pyc" CODEX_0_149_1_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \\',
        '+  ( cd "$tree/$workdir" && unset CODEX_UPGRADE_IDENTITY_MEMO ENTRY_COMMIT ENTRY_BUNDLE ENTRY_BRANCH && export PYTHONPYCACHEPREFIX="$pyc" CODEX_SPEC_SOURCE_ROOT="$HISTORICAL_SOURCE_ROOT" \\']}
CLOSEOUT_SH_6_BASELINE = 'f9fd48826c9ddc47376a57beb65328cc35cca180'
CLOSEOUT_SH_6_DIGESTS = {'vc5-gate-target.sh': '98ce09d9da772e8f32372f74bc892c5820b35d80fa64a97538a66a6d2c6b52a8'}
CLOSEOUT_SH_6_RESIDUALS = {'vc5-gate-target.sh': ['-ATT="$1"; GATE="$2"; T="$3"; SRC1491=$HISTORICAL_SOURCE_ROOT',
        '+ATT="$1"; GATE="$2"; T="$3"; SRC_SPEC=$HISTORICAL_SOURCE_ROOT',
        '-export CODEX_0_149_1_SOURCE_ROOT="$SRC1491"',
        '+export CODEX_SPEC_SOURCE_ROOT="$SRC_SPEC"',
        '-    --pycache "$PYC" --historical-source "$SRC1491" > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"',
        '+    --pycache "$PYC" --historical-source "$SRC_SPEC" > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"',
        '-set +e; env -u CODEX_UPGRADE_IDENTITY_MEMO unshare -m --propagation private bash -c \'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test\' > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)',
        '+set +e; env -u CODEX_UPGRADE_IDENTITY_MEMO -u ENTRY_COMMIT -u ENTRY_BUNDLE -u ENTRY_BRANCH unshare -m --propagation private bash -c \'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec make test\' > "$GATE/logs/target-platform.stdout.log" 2> "$GATE/logs/target-platform.stderr.log"; RC=$?; set -e; END=$(utc_now)']}
# <<< 收尾登记常量
LATER_SHELL_BASELINES: tuple[tuple[str, str, dict[str, str], dict[str, list[str]]], ...] = (
    ("入口来源目录与持久协调锁", ENTRY_SOURCE_STABILITY_BASELINE, ENTRY_SOURCE_STABILITY_DIGESTS, ENTRY_SOURCE_STABILITY_RESIDUALS),
    ("B5a-09", B5A09_BASELINE_COMMIT, B5A09_SHELL_DIGESTS, B5A09_SHELL_RESIDUALS),
    ("B5a-10", B5A10_BASELINE_COMMIT, B5A10_SHELL_DIGESTS, B5A10_SHELL_RESIDUALS),
    ("B3-04a", B304A_BASELINE_COMMIT, B304A_SHELL_DIGESTS, B304A_SHELL_RESIDUALS),
    ("B5a-11", B5A11_BASELINE_COMMIT, B5A11_SHELL_DIGESTS, B5A11_SHELL_RESIDUALS),
    ("B5b-01a", B5B01A_BASELINE_COMMIT, B5B01A_SHELL_DIGESTS, B5B01A_SHELL_RESIDUALS),
    ("m9-bytecode", M9_BYTECODE_BASELINE_COMMIT, M9_BYTECODE_SHELL_DIGESTS, M9_BYTECODE_SHELL_RESIDUALS),
    ("B5b-01b", B5B01B_BASELINE_COMMIT, B5B01B_SHELL_DIGESTS, B5B01B_SHELL_RESIDUALS),
    ("B4-05b-1", B405B1_BASELINE_COMMIT, B405B1_SHELL_DIGESTS, B405B1_SHELL_RESIDUALS),
    ("B5a-09-source", B5A09_SOURCE_BASELINE_COMMIT, B5A09_SOURCE_SHELL_DIGESTS, B5A09_SOURCE_SHELL_RESIDUALS),
    ("升级入口可选并发透传", ENTRY_PARALLEL_BASELINE_COMMIT, ENTRY_PARALLEL_SHELL_DIGESTS, ENTRY_PARALLEL_SHELL_RESIDUALS),
    ("engineering-disk", ENGINEERING_DISK_BASELINE_COMMIT, ENGINEERING_DISK_SHELL_DIGESTS, ENGINEERING_DISK_SHELL_RESIDUALS),
    ("B5a-09-fixture-project-guard", FIXTURE_PROJECT_GUARD_BASELINE_COMMIT, FIXTURE_PROJECT_GUARD_SHELL_DIGESTS, FIXTURE_PROJECT_GUARD_SHELL_RESIDUALS),
    ("B5a-09固定79接线", RECORDED_BRIDGE_BASELINE_COMMIT, RECORDED_BRIDGE_SHELL_DIGESTS, RECORDED_BRIDGE_SHELL_RESIDUALS),
    # >>> T2 预检登记行
    ("T2-预检收据与准入续签", T2_PRECHECK_BASELINE, T2_PRECHECK_DIGESTS, T2_PRECHECK_RESIDUALS),
    # <<< T2 预检登记行
    # >>> 收尾登记行（自动生成，勿手改）
    ('收尾登记 entry-gates.sh', CLOSEOUT_SH_1_BASELINE, CLOSEOUT_SH_1_DIGESTS, CLOSEOUT_SH_1_RESIDUALS),
    ('收尾登记 entry-preflight.sh', CLOSEOUT_SH_2_BASELINE, CLOSEOUT_SH_2_DIGESTS, CLOSEOUT_SH_2_RESIDUALS),
    ('收尾登记 env.example.sh', CLOSEOUT_SH_3_BASELINE, CLOSEOUT_SH_3_DIGESTS, CLOSEOUT_SH_3_RESIDUALS),
    ('收尾登记 vc0-gate-target.sh', CLOSEOUT_SH_4_BASELINE, CLOSEOUT_SH_4_DIGESTS, CLOSEOUT_SH_4_RESIDUALS),
    ('收尾登记 lib.sh', CLOSEOUT_SH_5_BASELINE, CLOSEOUT_SH_5_DIGESTS, CLOSEOUT_SH_5_RESIDUALS),
    ('收尾登记 vc5-gate-target.sh', CLOSEOUT_SH_6_BASELINE, CLOSEOUT_SH_6_DIGESTS, CLOSEOUT_SH_6_RESIDUALS),
    # <<< 收尾登记行
)


class DriverScriptLaterBatchCodexParityTests(unittest.TestCase):
    def test_scripts_with_codex_values_match_later_batch_starts(self) -> None:
        unreadable = sorted({commit for _batch, commit, _digests, _residuals in LATER_SHELL_BASELINES if not commit_readable(commit)})
        if unreadable:
            self.skipTest(f"读不到基线提交 {'、'.join(unreadable)}（浅克隆或不在仓库内）")
        # 统一基线覆盖收口前登记过的全部驱动脚本。
        before = {name for batch, _commit, digests, _residuals in LATER_SHELL_BASELINES
                  if batch in {"B5a-09", "B5a-10", "B3-04a"} for name in digests}
        self.assertEqual(sorted(B5A11_SHELL_DIGESTS), sorted(set(SHELL_DIGESTS) | before))
        values = {**_codex_shell_values(), "CT_PENDING_CHECK_PLAN_AUDIT": ""}
        for batch, commit, digests, residuals in LATER_SHELL_BASELINES:
            self.assertEqual(sorted(digests), sorted(residuals), batch)
            for name, digest in digests.items():
                with self.subTest(batch=batch, script=name):
                    text = (DRIVER / name).read_text(encoding="utf-8")
                    current = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    if current != digest:
                        assert_handed_over(self, name, batch, shell_batches_registering(name, current))
                        continue
                    source = git_source(f"tools/arm64_capture_driver/driver/{name}", commit)
                    self.assertIsNotNone(source, f"基线提交 {commit} 里没有 {name}")
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


# ------------------------------------------------------------------------------------------------ B5b-01a 的 Python 对照

# 修复轮、VC-5 准入、VC-5 计划生成器与入口编排器的 Codex 行为以 B5b-01a 开工时的提交（B5B01A_BASELINE_COMMIT，见上面驱动脚本
# 一段）为基线逐字对照（同一夹具上改造前后各跑一次）。基线提交读不到（浅克隆）时整条用例在父级跳过，子测试里一律不跳过。


def skip_unless_b5b01a_readable(case: unittest.TestCase) -> None:
    if not commit_readable(B5B01A_BASELINE_COMMIT):
        case.skipTest(f"读不到基线提交 {B5B01A_BASELINE_COMMIT}（浅克隆或不在仓库内）")


def _b5b01a_module(relative: str, name: str, *, siblings: dict[str, types.ModuleType] | None = None) -> types.ModuleType:
    source = git_source(relative, B5B01A_BASELINE_COMMIT)
    if source is None:
        raise AssertionError(f"基线提交 {B5B01A_BASELINE_COMMIT} 里没有 {relative}")
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / relative)
    with mock.patch.dict(sys.modules, {name: module, **(siblings or {})}), mock.patch.object(sys, "path", [str(DRIVER), *sys.path]):
        exec(compile(source, f"{B5B01A_BASELINE_COMMIT}:{relative}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


def _b5b01a_driver(root: Path) -> Path:
    """基线提交的整个驱动目录（git archive 解到 ``root``），修复轮对照用它的 fix-and-continue.sh 与同目录模块。"""

    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", B5B01A_BASELINE_COMMIT, "tools/arm64_capture_driver"],
                             capture_output=True, stdin=subprocess.DEVNULL)
    if archive.returncode:
        raise AssertionError(f"读不到基线提交 {B5B01A_BASELINE_COMMIT} 的驱动目录：{archive.stderr.decode(errors='replace')[:200]}")
    root.mkdir(mode=0o700)
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    return root / "tools" / "arm64_capture_driver" / "driver"


class FixAndContinueCodexParityTests(unittest.TestCase):
    """修复轮（B5b-01a 改为按客户端表取编排器与共享模块、客户端取驱动参数文件）：Codex 一轮完整的 15 步，改造前后的受管调用
    （模块与参数）、各步骤记录与终端输出逐字相同（夹具根、提交号、摘要与时刻归一化后比较）。"""

    def run_round(self, root: Path, script: Path | None) -> dict[str, object]:
        from tools.official_client_capture.tests import test_arm64_driver_fix_and_continue as fc

        root.mkdir(mode=0o700)
        fixture = fc._Round(root)
        completed = fixture.run(script=script)
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-3000:])
        drivers = [str(fc.SCRIPTS)] + ([str(script.parent)] if script is not None else [])

        def normal(text: str) -> str:
            for path in drivers:
                text = text.replace(path, "<DRV>")
            text = text.replace(str(root), "<ROOT>").replace(fixture.head, "<HEAD>")
            text = re.sub(r"\b[0-9a-f]{64}\b", "<SHA256>", text)
            text = re.sub(r"\d{8}t\d{6}(?:\d+)?z(?:-\d+)?", "<STAMP>", text)
            text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", "<UTC>", text)
            return re.sub(r"\[\d{2}:\d{2}:\d{2}Z\]", "[<CLOCK>]", text)

        records = {path.name: normal(path.read_text(encoding="utf-8")) for path in sorted(fixture.out.glob("*.json"))}
        calls = [normal(json.dumps(call, ensure_ascii=False, sort_keys=True)) for call in fixture.calls()]
        return {"stdout": normal(completed.stdout), "records": records, "calls": calls}

    def test_codex_round_matches_b5b01a_baseline(self) -> None:
        skip_unless_b5b01a_readable(self)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            baseline = _b5b01a_driver(root / "baseline-tree")
            before = self.run_round(root / "before", baseline / "fix-and-continue.sh")
            after = self.run_round(root / "after", None)
        self.assertEqual(after["calls"], before["calls"], "受管调用（模块与参数）逐字相同")
        self.assertEqual(sorted(after["records"]), sorted(before["records"]))
        for name in before["records"]:
            with self.subTest(record=name):
                self.assertEqual(after["records"][name], before["records"][name])
        self.assertEqual(after["stdout"], before["stdout"], "终端输出逐字相同")


class Vc5AdmissionCodexParityTests(unittest.TestCase):
    """VC-5 准入（B5b-01a 把画像与收据重放、reuse 分支搬进 Codex 适配器）：同一组编排器替身上，改造前后的目标画像结果、各处失败
    的错误类型与静态错误码、准入报告逐字相同。"""

    def setUp(self) -> None:
        from tools.arm64_capture_driver.driver import vc5_admission as admission

        skip_unless_b5b01a_readable(self)
        self.new = admission
        self.old = _b5b01a_module("tools/arm64_capture_driver/driver/vc5_admission.py", "vc5_admission_before_b5b01a")
        # 改造前的模块按扁平名导入同目录的参数解析与轮次根解析（驱动以脚本方式运行时它们就在 sys.path 最前）。
        path = mock.patch.object(sys, "path", [str(DRIVER), *sys.path])
        path.start()
        self.addCleanup(path.stop)

    def catalog_world(self, root: Path) -> tuple[dict[str, str], mock.Mock, dict[str, object]]:
        from tools.official_client_capture import codex_upgrade as upgrade

        admission = self.new
        catalog = root / "catalog"
        profile_digest = "d" * 64
        relative = f"catalogdata/runtime/profiles/9.1.0/{profile_digest}.json"
        raw = admission.canonical({"Digest": profile_digest})
        admission.atomic_write(catalog / relative, raw)
        stage = {"campaign_id": "fixture", "target_version": "9.1.0", "profile_id": "fixture-profile",
                 "target_profile_digest": profile_digest, "classification_sha256": "c" * 64,
                 "inventory": [{"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}]}
        stage["inventory_sha256"] = upgrade._fingerprint(stage["inventory"])
        receipt = catalog / "catalog-stage-receipt.json"
        admission.write_json(receipt, stage)
        config = {"D": str(root), "B": str(root / "candidates/candidate"), "NEW": "fixture", "CAND": "candidate",
                  "RUNROOT": str(root / "round"), "IN": "inputs", "UP": "upgrade", "BASELINE_VERSION": "9.0.0",
                  "TARGET_VERSION": "9.1.0", "PROFILE_ID": "fixture-profile", "LIFECYCLE_DIR": "docs/lifecycle/fixture", "C": "a" * 40}
        admission.atomic_write(root / "candidates/candidate/source/docs/lifecycle/fixture/catalog-stage/catalog-stage-receipt.json",
                               receipt.read_bytes())
        campaign = root / "evidence/campaigns/fixture"
        admission.write_json(campaign / "candidates/candidate/build-receipt.json", {})
        manifest = {"campaign_id": "fixture", "baseline_version": "9.0.0", "target_version": "9.1.0",
                    "vc_control": {"campaign_plan": {"path": "control/vc/campaign-plan.json"}}}
        admission.write_json(campaign / "campaign.json", manifest)
        admission.atomic_write(campaign / "campaign.sha256", upgrade.file_sha256(campaign / "campaign.json").encode())
        admission.write_json(campaign / "control/vc/campaign-plan.json", {})
        admission.write_json(root / "control/upgrade-timing-ledger/ledger.json", {})
        revision = {"campaign_id": "fixture", "candidate_id": "candidate"}
        admission.write_json(campaign / "control/vc/revisions/r1/revision.json", revision)
        admission.write_json(campaign / "control/vc/revisions/r1/COMMIT", {})
        fake = mock.Mock()
        fake._require_formal_campaign.return_value = manifest
        fake._campaign_timing_ledger_dir.return_value = root / "control/upgrade-timing-ledger"
        fake._candidate_revision_dir.return_value = campaign / "control/vc/revisions/r1"
        fake._load_stage_result.return_value = {"joint_manifest_sha256": "c" * 64}
        fake._replay_vc_checkpoint.return_value = (None, {"stage_receipt": {"path": str(receipt), "sha256": upgrade.file_sha256(receipt)}})
        fake.file_sha256 = upgrade.file_sha256
        fake._verify_catalog_stage_output = upgrade._verify_catalog_stage_output
        fake._profile_binding_from_manifest.return_value = ("fixture-profile", profile_digest)
        fake._replay_candidate_build_receipt.return_value = ({"image": {"reference": "fixture@sha256:" + "b" * 64,
                                                                       "image_id": "sha256:" + "b" * 64},
                                                             "build": {"build_id": "fixture-build"}}, {})
        fake._current_candidate_revision_record.return_value = (1, revision)
        fake._bind_candidate_identity_to_build_receipt.return_value = {"git_commit": "a" * 40}
        return config, fake, {"catalog": catalog, "relative": relative, "receipt": receipt}

    def outcome(self, module: types.ModuleType, config: dict[str, str], fake: mock.Mock) -> tuple[str, object]:
        from tools.official_client_capture import codex_upgrade as upgrade

        subject = module.ManagedFacts.__new__(module.ManagedFacts)
        subject.config, subject.upgrade = config, fake
        names = ("_require_formal_campaign", "_load_stage_result", "_vc_campaign_plan", "_replay_vc_checkpoint",
                 "_verify_catalog_stage_output", "_profile_binding_from_manifest", "_replay_candidate_build_receipt",
                 "_current_candidate_revision_record", "_candidate_identity_for_run", "_bind_candidate_identity_to_build_receipt",
                 "file_sha256")
        # 改造后经适配器直接取 codex_upgrade 的成员：装上与 subject.upgrade 同一组替身（改造前只用 subject.upgrade）。
        with mock.patch.multiple(upgrade, **{name: getattr(fake, name) for name in names}):
            try:
                return "result", subject.catalog()
            except Exception as error:  # 对照的是错误的类型名与消息
                return type(error).__name__, str(error)

    def test_catalog_results_and_refusals_match_b5b01a_baseline(self) -> None:
        cases = {
            "通过": lambda config, fake, world: None,
            "VC-3 收据摘要漂移": lambda config, fake, world: fake._replay_vc_checkpoint.return_value[1]["stage_receipt"].update(sha256="0" * 64),
            "画像绑定不符": lambda config, fake, world: setattr(fake._profile_binding_from_manifest, "return_value", ("other-profile", "d" * 64)),
            "候选 revision 不符": lambda config, fake, world: setattr(fake._current_candidate_revision_record, "return_value",
                                                                 (1, {"candidate_id": "other"})),
            "候选提交不符": lambda config, fake, world: setattr(fake._bind_candidate_identity_to_build_receipt, "return_value",
                                                           {"git_commit": "b" * 40}),
            "树内收据漂移": lambda config, fake, world: (Path(config["B"]) / "source/docs/lifecycle/fixture/catalog-stage/catalog-stage-receipt.json")
            .write_bytes(b"{}"),
            "画像内容漂移": lambda config, fake, world: (world["catalog"] / world["relative"]).write_bytes(b"drift"),
        }
        for label, mutate in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                outcomes = []
                for module in (self.old, self.new):
                    root = Path(directory).resolve() / ("old" if module is self.old else "new")
                    config, fake, world = self.catalog_world(root)
                    mutate(config, fake, world)
                    outcomes.append(self.outcome(module, config, fake))
                self.assertEqual(outcomes[1], outcomes[0])

    def test_inspection_reports_match_b5b01a_baseline(self) -> None:
        from tools.official_client_capture import codex_upgrade as upgrade
        from tools.official_client_capture.tests import test_vc5_admission as admission_tests

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            compose = root / "compose"
            compose.mkdir()
            self.new.atomic_write(compose / ".env", ('JWT_SECRET="隔离验收签名密钥"\nADMIN_EMAIL=admin@fixture.invalid\n'
                                                     "POSTGRES_USER=fixture\nPOSTGRES_PASSWORD=fixture\nPOSTGRES_DB=fixture\n").encode())
            self.new.atomic_write(root / "signer", b"#!/bin/sh\nexit 1\n", 0o700)
            official = root / "data/evidence/campaigns/predecessor"
            config = {"D": str(root / "data"), "RUNROOT": str(root / "round"), "NEW": "fixture-campaign", "CAND": "fixture-candidate",
                      "UP": "fixture", "TARGET_VERSION": "9.1.0", "PROFILE_ID": "fixture-profile", "PROFILE_DIGEST": "b" * 64,
                      "EVIDENCE_DECISION": "recapture", "OFFICIAL_CAMPAIGN": "old-campaign", "PREDECESSOR_CAMPAIGN": str(official),
                      "B": str(root / "candidate"), "COMPOSE_DIR": str(compose), "JWTGEN_BIN": str(root / "signer")}
            source = {"campaign_id": "predecessor", "target_version": "9.1.0"}
            current = {"predecessor": {"campaign_dir": str(official), "campaign_id": "predecessor", "campaign_manifest_sha256": "e" * 64}}

            def reports() -> list[dict]:
                found = []
                for module in (self.old, self.new):
                    facts = admission_tests.Facts(root / "data/runtime/codex-profile-9.1.0.json", config)
                    facts.upgrade = upgrade  # 改造前的 reuse 分支经 facts.upgrade 取编排器
                    report = module.Admission(dict(config), facts).inspect()
                    report.pop("checked_at_utc")
                    found.append(report)
                return found

            with mock.patch.object(upgrade, "_require_formal_campaign", side_effect=lambda path: source if path == official else current), \
                    mock.patch.object(upgrade, "file_sha256", return_value="e" * 64):
                for label, change in (("recapture", {}), ("reuse 通过", {"EVIDENCE_DECISION": "reuse"}),
                                      ("reuse 目标版本不同", {"source": "9.0.0"}), ("reuse 前序摘要不符", {"current": "wrong"})):
                    with self.subTest(label):
                        if "source" in change:
                            source["target_version"] = change["source"]
                        elif "current" in change:
                            source["target_version"] = "9.1.0"
                            current["predecessor"]["campaign_manifest_sha256"] = change["current"]
                        else:
                            config.update(change)
                        before, after = reports()
                        self.assertEqual(after, before)


class Vc5PlanGeneratorCodexParityTests(unittest.TestCase):
    """VC-5 计划生成器（B5b-01a 只加了待接入核对，对 Codex 恒不触发）：同一组准入收据与阶段坐标，改造前后写出的每份计划逐字相同。"""

    def generate(self, script: Path, output: Path, args: list[str], modules: dict, environment: dict) -> dict[str, bytes]:
        import os
        import runpy

        argv = ["gen_vc5_plans.py", str(output), *args]
        with mock.patch.dict(sys.modules, modules), mock.patch.dict(sys.modules), mock.patch.dict(os.environ, environment), \
                mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "path", list(sys.path)), \
                contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(str(script), run_name="__main__")
        return {path.name: path.read_bytes() for path in sorted(output.iterdir()) if path.name.startswith("action-plan-vc5")}

    def test_generated_plans_match_b5b01a_baseline(self) -> None:
        from tools.arm64_capture_driver.driver import parse_env

        skip_unless_b5b01a_readable(self)
        source = git_source("tools/arm64_capture_driver/driver/gen_vc5_plans.py", B5B01A_BASELINE_COMMIT)
        self.assertIsNotNone(source, f"基线提交 {B5B01A_BASELINE_COMMIT} 里没有 VC-5 计划生成器")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = root / "baseline" / "gen_vc5_plans.py"
            baseline.parent.mkdir()
            baseline.write_bytes(source)
            fixture = driver_tests._DriverFixture(root / "fixture")
            config = parse_env.parse(fixture.env_file.read_text())
            config.update(parse_env.derive(config))
            image = "sha256:" + "a" * 64
            receipt = {"effective_parameters": {"PROFILE_ID": config["PROFILE_ID"], "PROFILE_DIGEST": "d" * 64,
                                                "OFFICIAL_CAMPAIGN": str(fixture.newdir)},
                       "bindings": {"campaign": fixture.new, "candidate": fixture.cand, "image_id": image, "build_id": "build"}}
            fake_admission = mock.Mock()
            fake_admission.Admission.return_value.consume.return_value = receipt
            attempt_root = fixture.newdir / "candidates" / fixture.cand / "attempts" / "attempt-1"
            parameters = {
                "IMAGE_ID": image, "BUILD_ID": "build", "BUILD_RECEIPT": str(fixture.newdir / "candidates" / fixture.cand / "build-receipt.json"),
                "PROFILE_ID": config["PROFILE_ID"], "PROFILE_DIGEST": "d" * 64, "CAPTURE_WRITE": "current-result",
                "COMPARE_WRITE": "current-compare", "ASSERTIONS_WRITE": str(root / "assertions-write"), "ACCEPT_WRITE": "current-accept",
                "EV": str(attempt_root / "evidence"), "ASSERTION_CONFIG_DIR": str(root / "assertion-config"), "EVALUATION_BASELINE": "0",
                "ASSERTIONS_RESULT": str(root / "assertions-write" / "results.json"), "GATE_ROOT": str(root / "gates"),
            }
            fake_phase = mock.Mock()
            fake_phase.resolve.return_value = {"parameters": parameters}
            fake_config = mock.Mock(load_config=lambda: dict(config), candidate_job_ids=lambda campaign, candidate: ["job-a", "job-b"])
            modules = {"vc5_admission": fake_admission, "driver_config": fake_config, "phase_context": fake_phase}
            environment = {**config, "CANDIDATE_DIR": str(fixture.candidate_dir)}
            for label, extra in (("run", []), ("attempt", ["attempt-1"]), ("seal", ["attempt-1", "e" * 64])):
                with self.subTest(label):
                    outputs = []
                    for name, script in (("before", baseline), ("after", DRIVER / "gen_vc5_plans.py")):
                        output = Path(config["W"])
                        if output.exists():
                            shutil.rmtree(output)
                        output.mkdir(parents=True)
                        outputs.append(self.generate(script, output, [fixture.new, fixture.cand, image, "build", *extra], modules, environment))
                    self.assertTrue(outputs[0])
                    self.assertEqual(outputs[1], outputs[0])


def skip_unless_b5b01b_readable(case: unittest.TestCase) -> None:
    if not commit_readable(B5B01B_BASELINE_COMMIT):
        case.skipTest(f"读不到基线提交 {B5B01B_BASELINE_COMMIT}（浅克隆或不在仓库内）")


class Vc5GateFactsCodexParityTests(unittest.TestCase):
    """门禁 facts（B5b-01b 只加了可选 --subject，Codex 不传）：同一组 Codex 输入，改造前后写出的 facts 逐字相同；--subject 只认闭合字段。"""

    def write(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, encoding="utf-8")
        return path

    def inputs(self, root: Path) -> list[str]:
        gate_root, local, target = root / "gates", root / "local", root / "target"
        for gate_id, directory in (("check-egress-spec", local), ("full-regression", local), ("target-platform", target)):
            self.write(directory / f"{gate_id}.gate.json", {
                "command": ["make", gate_id], "working_directory": ".", "host": "host-a", "architecture": "linux/aarch64",
                "started_at_utc": "2026-10-08T00:00:00Z", "completed_at_utc": "2026-10-08T00:01:00Z", "exit_code": 0,
            })
            self.write(directory / f"{gate_id}.stdout.log", f"{gate_id} ok\n")
            self.write(directory / f"{gate_id}.stderr.log", "")
        for role in ("before", "after"):
            self.write(gate_root / "environment" / f"attempt-1-{role}.json", {"role": role})
        campaign = self.write(root / "campaign.json", {"campaign_id": "c1", "campaign_mode": "formal", "campaign_purpose": "production_replacement", "target_version": "0.160.0"})
        seal = self.write(root / "result.json", {"candidate_id": "cand", "candidate_purpose": "production_replacement", "package_digest": "p" * 64,
                                                "identity": {"profile_id": "codex-x", "profile_digest": "d" * 64, "source_tree_sha256": "t" * 64,
                                                             "image_id": "sha256:" + "i" * 64, "image_reference": "r@sha256:" + "i" * 64}})
        build = self.write(root / "build-receipt.json", {"target_architecture": "linux/arm64"})
        return [str(gate_root), "attempt-1", str(campaign), str(seal), str(build), str(local), str(target)]

    def run_script(self, script: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(script), *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_codex_facts_match_b5b01b_baseline_and_subject_must_close(self) -> None:
        skip_unless_b5b01b_readable(self)
        source = git_source("tools/arm64_capture_driver/driver/build_gate_facts.py", B5B01B_BASELINE_COMMIT)
        self.assertIsNotNone(source)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            baseline = root / "baseline.py"
            baseline.write_bytes(source)
            outputs = []
            for label, script in (("before", baseline), ("after", DRIVER / "build_gate_facts.py")):
                argv = self.inputs(root / label)
                completed = self.run_script(script, argv)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                outputs.append((root / label / "gates" / "candidate-gates.facts.json").read_text(encoding="utf-8").replace(str(root / label), "<root>"))
            self.assertEqual(outputs[1], outputs[0])
            # --subject：字段闭合且 Campaign／候选与本轮一致才用；Claude 的 subject 原样进 facts。
            argv = self.inputs(root / "subject")
            facts = json.loads(outputs[0])
            subject = self.write(root / "subject.json", {**facts["subject"], "profile_id": "claude-code-2.1.285-official"})
            (root / "subject" / "gates" / "candidate-gates.facts.json").unlink(missing_ok=True)
            completed = self.run_script(DRIVER / "build_gate_facts.py", [*argv, "--subject", str(subject)])
            self.assertEqual(completed.returncode, 0, completed.stderr)
            written = json.loads((root / "subject" / "gates" / "candidate-gates.facts.json").read_text(encoding="utf-8"))
            self.assertEqual(written["subject"]["profile_id"], "claude-code-2.1.285-official")
            for bad in ({**facts["subject"], "extra": 1}, {**facts["subject"], "candidate_id": "other"}):
                self.write(subject, bad)
                completed = self.run_script(DRIVER / "build_gate_facts.py", [*argv, "--subject", str(subject)])
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("--subject", completed.stderr)


class PhaseContextCodexParityTests(unittest.TestCase):
    """阶段解析（B5b-01b 只新增 Claude 分支）：B5b-01b 开工时的每个顶层定义原样保留，resolve 只改了按客户端选上下文与 VC-5 计划
    核对的两处；Codex 参数选的仍是原来的 Context。行为面的 Codex 覆盖在 test_arm64_phase_context。"""

    RESOLVE_RESIDUALS = [
        "-        context = Context(config, candidate=candidate, upgrade=upgrade)",
        '+        context_class = ClaudeContext if client_spec(config)["client"] == "claude" else Context',
        "+        context = context_class(config, candidate=candidate, upgrade=upgrade)",
        "-            _vc5_plan(context, plan)",
        "+            (_claude_vc5_plan if isinstance(context, ClaudeContext) else _vc5_plan)(context, plan)",
    ]
    # 10-23 Codex 修正：不含 canonical 项的 VC-6 计划（交付登记）改由 _vc6_delivery_plan 核对绑定，canonical 批次的校验不变。
    VC6_RECEIPTS_RESIDUALS = [
        '-    if group is None or (group["campaign_dir"], group["candidate_id"], group["attempt_id"]) != (',
        "+    if group is None:",
        "+        _vc6_delivery_plan(context, plan)",
        "+        return",
        '+    if (group["campaign_dir"], group["candidate_id"], group["attempt_id"]) != (',
    ]

    def segments(self, text: str) -> dict[str, str]:
        import ast

        tree = ast.parse(text)
        return {node.name: ast.get_source_segment(text, node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}

    def test_codex_definitions_are_untouched(self) -> None:
        skip_unless_b5b01b_readable(self)
        source = git_source("tools/arm64_capture_driver/driver/phase_context.py", B5B01B_BASELINE_COMMIT)
        self.assertIsNotNone(source)
        before = self.segments(source.decode("utf-8"))
        after = self.segments((DRIVER / "phase_context.py").read_text(encoding="utf-8"))
        # _vc6_delivery_plan 与 _vc6_receipts 的改动不属于 B5b-01b：10-23 修正 VC-6 交付登记批次被误拒（Codex 缺陷修复），
        # 新增定义单列放行，_vc6_receipts 的改动钉成 VC6_RECEIPTS_RESIDUALS，其余 Codex 定义仍须逐字未改。
        self.assertEqual(sorted(set(after) - set(before)), ["ClaudeContext", "_claude_vc5_plan", "_vc6_delivery_plan"])
        for name, text in before.items():
            if name in {"resolve", "_vc6_receipts"}:
                continue
            with self.subTest(definition=name):
                self.assertEqual(after[name], text)
        changed = [line for line in difflib.unified_diff(before["resolve"].splitlines(), after["resolve"].splitlines(), lineterm="", n=0)
                   if line[:1] in "+-" and not line.startswith(("+++", "---")) and not line[1:].strip().startswith("#")]
        self.assertEqual(changed, self.RESOLVE_RESIDUALS)
        receipts = [line for line in difflib.unified_diff(before["_vc6_receipts"].splitlines(), after["_vc6_receipts"].splitlines(), lineterm="", n=0)
                    if line[:1] in "+-" and not line.startswith(("+++", "---")) and not line[1:].strip().startswith("#")]
        self.assertEqual(receipts, self.VC6_RECEIPTS_RESIDUALS)


class EntryLedgerAndPreflightCodexParityTests(unittest.TestCase):
    """入口编排器（B5b-01a：Claude 按参数 CAMPAIGN_PURPOSE 建账本）：Codex 的建账本与预检 plan 命令行与 B5b-01a 开工时逐字相同。"""

    def test_codex_ledger_and_preflight_commands_match_b5b01a_baseline(self) -> None:
        skip_unless_b5b01a_readable(self)
        old_steps = _b5b01a_module("tools/ci/entry_steps.py", "entry_steps_before_b5b01a")
        old = _b5b01a_module("tools/ci/entry_orchestrator.py", "entry_orchestrator_before_b5b01a",
                             siblings={"entry_steps_sibling": old_steps})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            commands = []
            for label, module in (("before", old), ("after", eo)):
                fx = Fixture(root / label)
                shutil.rmtree(fx.data / "control" / f"{fx.params['UP']}-timing-ledger")
                fx.params.update({"RUNROOT": str(root / label / "run")})
                calls: list[list[str]] = []

                def runner(argv, cwd, env, log, calls=calls, label=label):
                    # 总预算按当前时刻折算到项目截止（分钟），两次运行可能跨分钟：只比较它之外的逐字参数。
                    text = [str(item).replace(str(root / label), "<ROOT>") for item in argv]
                    if "--total-budget-minutes" in text:
                        text[text.index("--total-budget-minutes") + 1] = "<MINUTES>"
                    calls.append(text)
                    return 1

                orchestrator = module.Orchestrator(fx.params, driver_dir=fx.driver, run_dir=root / label / "runs", steps_dir=fx.steps_dir,
                                                   runner=runner, es_runner=fx.runner)
                orchestrator.step_ledger()
                commands.append(calls)
        self.assertEqual(commands[1], commands[0], "Codex 建账本命令（用途固定 production_replacement）逐字相同")



# ------------------------------------------------------------------------------------------------ B4-05b-1 的监督器与续跑计划对照

def skip_unless_b405b1_readable(case: unittest.TestCase) -> None:
    if not commit_readable(B405B1_BASELINE_COMMIT):
        case.skipTest(f"读不到基线提交 {B405B1_BASELINE_COMMIT}（浅克隆或不在仓库内）")


CODEX_RECOVERY_PREFIX = ["/usr/bin/python3", "/data/tools/official_client_capture/codex_upgrade.py"]
CODEX_RECOVERY_IDENTITY = {
    "--candidate-id": "cand-1",
    "--build-receipt": "/data/campaign/candidates/cand-1/build-receipt.json",
    "--runtime-image": "repo@sha256:" + "1" * 64,
    "--candidate-image-id": "sha256:" + "1" * 64,
    "--candidate-source": "/root/candidate/source",
    "--build-id": "build-1",
    "--deployed-version": "0.160.0",
    "--profile-id": "profile-1",
    "--profile-digest": "2" * 64,
    "--candidate-purpose": "production_replacement",
}


class SupervisorCandidateRecoveryCodexParityTests(unittest.TestCase):
    """监督器候选采集续跑（B4-05b-1：续跑命令的身份参数改经适配器按清单归属客户端取）：同一组 Codex 输入（Codex 批次清单、
    未给适配器的直接调用），B4-05b-1 开工时的监督器与现在的逐字相同——解析、命令构造、续跑动作种类与路由、父批次身份与三个
    续跑后继协议（父 run 的失败终态与对账核验在两边换成同一个记录替身）。gen_vc5_recovery_plans.py 只换了客户端守卫一段。"""

    def setUp(self) -> None:
        skip_unless_b405b1_readable(self)
        relative = "tools/official_client_capture/codex_upgrade_supervisor.py"
        source = git_source(relative, B405B1_BASELINE_COMMIT)
        self.assertIsNotNone(source)
        old = types.ModuleType("codex_upgrade_supervisor_before_b405b1")
        old.__file__ = str(REPO_ROOT / relative)
        # 历史源码按包方式导入，使用与正式监督器相同的相对导入上下文。
        old.__package__ = "tools.official_client_capture"
        with mock.patch.dict(sys.modules, {old.__name__: old}):
            exec(compile(source, f"{B405B1_BASELINE_COMMIT}:{relative}", "exec"), old.__dict__)  # noqa: S102 - 受控的历史源码
        from tools.official_client_capture import codex_upgrade_supervisor as new

        self.old, self.new = old, new

    def tokens(self) -> list[str]:
        return [item for flag in self.new.CANDIDATE_RECOVERY_IDENTITY_FLAGS for item in (flag, CODEX_RECOVERY_IDENTITY[flag])]

    def same(self, call) -> object:
        """同一调用在改造前后各跑一次：返回值逐字相同，或抛同名异常且消息相同。"""

        outcomes = []
        for module in (self.old, self.new):
            try:
                outcomes.append(("ok", call(module)))
            except Exception as error:  # noqa: BLE001 - 两边的异常类是各自模块的同名类，按类名与消息比
                outcomes.append((type(error).__name__, str(error)))
        self.assertEqual(outcomes[1], outcomes[0])
        return outcomes[1][1]

    def manifest(self, action: dict, sequence: int = 1) -> dict:
        return {
            "schema_version": self.new.CAMPAIGN_RUN_BATCHED_SCHEMA, "phase": "VC-5", "batch_sequence": sequence, "no_op": False,
            "campaign_id": "c1", "campaign_plan_sha256": "p" * 64, "original_deadline_at_utc": "2026-10-10T15:59:00Z",
            "predecessor_checkpoint": {"phase": "VC-4"}, "candidate_id": "cand-1", "candidate_revision": 1,
            "evaluation_baseline": None, "baseline_commit_sha256": None, "execute_items": ["candidate-run"], "reuse_items": [],
            "actions": [action],
        }

    def test_codex_candidate_recovery_matches_b405b1_baseline(self) -> None:
        campaign = "/data/campaign"
        run = [*CODEX_RECOVERY_PREFIX, "capture-candidate", "run", "--campaign-dir", campaign, *self.tokens(),
               "--acknowledge-live-requests", "--max-wall-seconds", "21600"]
        self.assertEqual(self.new.candidate_recovery_identity_flags(), self.new.CANDIDATE_RECOVERY_IDENTITY_FLAGS)
        claude_run = ["/usr/bin/python3", "/data/tools/official_client_capture/claude_upgrade.py", "capture-candidate", "run",
                      "--campaign-dir", campaign, "--candidate-id", "cand-1", "--acknowledge-live-requests"]
        for label, command in (("run", run), ("缺身份参数", run[:-6] + run[-4:]), ("多一个参数", [*run, "--extra", "x"]),
                               ("Claude 形状", claude_run), ("非列表", "run")):
            with self.subTest(parse=label):
                self.same(lambda module, command=command: module.candidate_capture_run_identity(command))
        preview = self.same(lambda module: module.candidate_recovery_preview_command(CODEX_RECOVERY_PREFIX, campaign, CODEX_RECOVERY_IDENTITY))
        rerun = self.same(lambda module: module.candidate_recovery_run_command(
            CODEX_RECOVERY_PREFIX, campaign, CODEX_RECOVERY_IDENTITY, "/data/campaign/control/reconciliation/attempt-1/recovery-preview-01.json"))
        claude_preview = [*claude_run[:2], "resume", "--campaign-dir", campaign, "--rerun-failed", "--preview-recovery", "--candidate-id", "cand-1"]
        for label, command, is_preview in (("预览", preview, True), ("补跑", rerun, False), ("预览当补跑", preview, False),
                                           ("Claude 预览", claude_preview, True)):
            with self.subTest(identity=label):
                self.same(lambda module, command=command, is_preview=is_preview:
                          module._candidate_recovery_command_identity(command, preview=is_preview))
        actions = {
            "capture": {"action_id": "candidate-run", "operation": "VC-5:capture-candidate-run", "timeout_seconds": 21600,
                        "command": run, "item_ids": ["candidate-run"]},
            "preview": {"action_id": self.new.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID, "operation": self.new.CANDIDATE_RECOVERY_OPERATION,
                        "timeout_seconds": 1800, "command": preview, "item_ids": ["candidate-run"]},
            "run": {"action_id": self.new.CANDIDATE_RECOVERY_RUN_ACTION_ID, "operation": self.new.CANDIDATE_RECOVERY_OPERATION,
                    "timeout_seconds": 21600, "command": rerun, "item_ids": ["candidate-run"]},
            "seal": {"action_id": "candidate-seal", "operation": "VC-5:seal", "timeout_seconds": 900,
                     "command": [*CODEX_RECOVERY_PREFIX, "capture-candidate", "seal", "--campaign-dir", campaign], "item_ids": ["candidate-seal"]},
        }
        for kind, action in actions.items():
            manifest = self.manifest(action)
            with self.subTest(kind=kind):
                self.same(lambda module, manifest=manifest: module.candidate_recovery_parent_identity(manifest))
                self.same(lambda module, manifest=manifest, action=action: module.candidate_capture_recovery_action(manifest, action["action_id"]))
                for failure_class in ("execution-failure", "deadline-expired"):
                    self.same(lambda module, manifest=manifest, action=action, failure_class=failure_class:
                              module.candidate_capture_recovery_route(manifest, action["action_id"], failure_class))

    def test_codex_recovery_successor_protocols_match_b405b1_baseline(self) -> None:
        campaign = "/data/campaign"
        run = [*CODEX_RECOVERY_PREFIX, "capture-candidate", "run", "--campaign-dir", campaign, *self.tokens(), "--acknowledge-live-requests"]
        preview = self.new.candidate_recovery_preview_command(CODEX_RECOVERY_PREFIX, campaign, CODEX_RECOVERY_IDENTITY)
        rerun = self.new.candidate_recovery_run_command(CODEX_RECOVERY_PREFIX, campaign, CODEX_RECOVERY_IDENTITY, "/data/p.json")
        operation, preview_id, run_id = (self.new.CANDIDATE_RECOVERY_OPERATION, self.new.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID,
                                         self.new.CANDIDATE_RECOVERY_RUN_ACTION_ID)
        capture_action = {"action_id": "candidate-run", "operation": "VC-5:capture-candidate-run", "timeout_seconds": 21600,
                          "command": run, "item_ids": ["candidate-run"]}
        preview_action = {"action_id": preview_id, "operation": operation, "timeout_seconds": 1800, "command": preview, "item_ids": ["candidate-run"]}
        run_action = {"action_id": run_id, "operation": operation, "timeout_seconds": 21600, "command": rerun, "item_ids": ["candidate-run"]}
        drifted = dict(preview_action, command=[*preview[:-2], "--candidate-purpose", "validation_only"])
        cases = (
            ("采集失败后预览", "_validate_batched_candidate_recovery_preview_successor", capture_action, preview_action),
            ("采集失败后预览·身份漂移", "_validate_batched_candidate_recovery_preview_successor", capture_action, drifted),
            ("预览失败后逐字重派", "_validate_batched_candidate_recovery_preview_retry_successor", preview_action, preview_action),
            ("补跑失败后新预览", "_validate_batched_candidate_recovery_run_retry_successor", run_action, preview_action),
            ("补跑失败后新预览·身份漂移", "_validate_batched_candidate_recovery_run_retry_successor", run_action, drifted),
            ("前序不是续跑", "_validate_batched_candidate_recovery_run_retry_successor", capture_action, preview_action),
        )
        with tempfile.TemporaryDirectory() as directory:
            prior_dir = Path(directory)
            for label, protocol, prior_action, successor_action in cases:
                prior, successor = self.manifest(prior_action, 1), self.manifest(successor_action, 2)
                calls: dict[str, list] = {"old": [], "new": []}

                def check(module, protocol=protocol, prior=prior, successor=successor):
                    side = "old" if module is self.old else "new"
                    with mock.patch.object(module, "_verify_failed_official_recovery_parent",
                                           lambda *args, **kwargs: calls[side].append(kwargs.get("action_id"))):
                        return getattr(module, protocol)({}, prior, prior_dir, successor, campaign_dir=Path(campaign))

                with self.subTest(case=label):
                    self.same(check)
                    self.assertEqual(calls["new"], calls["old"])

    def test_codex_recovery_plan_generator_only_swaps_the_client_guard(self) -> None:
        source = git_source("tools/arm64_capture_driver/driver/gen_vc5_recovery_plans.py", B405B1_BASELINE_COMMIT)
        self.assertIsNotNone(source)
        self.assertEqual(
            residual(source.decode("utf-8"), (DRIVER / "gen_vc5_recovery_plans.py").read_text(encoding="utf-8")),
            [
                '-WAITING = [item for item in (client_table().pending(client_spec(CONFIG)["client"], name)',
                '-                             for name in ("tool:vc5-recover", "tool:vc5-run")) if item]',
                "-if WAITING:",
                '-    raise SystemExit("本轮客户端的 VC-5 续跑计划尚未接入：" + "；".join(WAITING))',
                '+DISPATCH = client_table().spec(client_spec(CONFIG)["client"])["vc5_recover_script"]',
                '+if DISPATCH != "vc5-recover.sh":',
                '+    raise SystemExit(f"本轮客户端的 VC-5 续跑由 {DISPATCH} 派发（计划由 claude_vc5.py recovery-plans 生成），本生成器只给 Codex 的续跑驱动用")',
            ],
        )
        # Codex 的续跑派发脚本表项就是 vc5-recover.sh：守卫对 Codex 恒不触发。
        self.assertEqual(es.CT.spec("codex")["vc5_recover_script"], "vc5-recover.sh")


if __name__ == "__main__":
    unittest.main()
