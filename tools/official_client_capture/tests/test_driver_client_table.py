"""B5a-06：驱动链与入口基础设施的客户端表，以及按 ``CLIENT=claude`` 展开的各绑定点。

覆盖：

* 客户端表本身：两行闭合、驱动与 ``tools/ci`` 两份副本逐字相同、shell 视图只赋值不导出且覆盖驱动脚本引用的全部 ``CT_``
  变量、不认识的客户端失败关闭；与受管适配器、受监督部署器、驱动安装脚本、pre-A3 认证模块里同名的取值一致；模块、
  文件与文档绑定都落在真实文件上（尚未落实的模块必须登记为待接入）；驱动里没有版本字面量；
* 参数解析按 ``CLIENT=claude`` 展开全部键（与 Codex 派生同一组键），Claude 只认通用键、拒绝 Codex 专属键，模板里的
  Claude 写法能解析；``lib.sh`` 给出 CT_ 变量且不导出；
* 入口步骤、入口编排与入口门禁的 Claude 绑定：步骤表结构相同、输入按 Claude 声明（第二受管根、Claude 指南与共享控制面
  文档、客户端表、Claude 策略与环境收据、宿主机 /opt/claude-<版本>/claude）；读 Claude 部署收据、按 Claude 算五摘要；
  待接入的步骤不执行并写明工作项；只改 ``official_client_control/store.py`` 或任一 schema 时 Claude 场景单元的输入摘要变化、
  Codex 场景单元不变；
* B5a-01／B5a-04 留下的三个尾巴：修复轮部署按纳管状态带 ``--client codex claude`` 与两个受管根并复制 Claude 文档；入口门禁
  的部署绑定、策略认证按客户端（脚本静态核对加 CT_ 取值）；驱动安装收据绑定全部已纳管客户端、复验按客户端判最新。

本模块按 pre-A3 场景单元的输入声明展开测试模块的静态依赖闭包（读测试目录里的模块源码），按
``unit_records.WHOLE_TESTS_READERS`` 登记。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.ci import entry_dryrun as dr
from tools.ci import entry_gates as eg
from tools.ci import entry_orchestrator as eo
from tools.ci import entry_steps as es
from tools.ci import unit_records as records
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture.claude_upgrade_client_adapter import CLAUDE_CAMPAIGN_ID_RE
from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests
from tools.official_client_capture.tests.test_ci_entry_orchestrator import Commands, _option
from tools.official_client_capture.tests.test_ci_entry_steps import Fixture

REPO_ROOT = Path(__file__).resolve().parents[3]
DRIVER = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
OCC = REPO_ROOT / "tools" / "official_client_capture"
CT = es.CT
BASELINE, TARGET = "2.1.226", "2.1.245"


def claude_env_text(fixture: driver_tests._DriverFixture) -> str:
    """把驱动测试夹具的 Codex 参数文件改成 Claude 的：去掉 Codex 专属键，换版本、坐标与通用键。"""

    lines = []
    for line in fixture.env_file.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0]
        if key in {"KILO_BIN", "KILO_VERSION", "KILO_SHA256", "CODEX_BIN", "CODEX_BIN_SHA256", "CODEX_ACCOUNT_ID"}:
            continue
        lines.append(line)
    text = "CLIENT=claude\n" + "\n".join(lines) + "\n"
    text = text.replace('BASELINE_VERSION="0.154.0"', f'BASELINE_VERSION="{BASELINE}"')
    text = text.replace('TARGET_VERSION="0.156.1"', f'TARGET_VERSION="{TARGET}"')
    text = text.replace("codex-0.156.1-official", f"claude-code-{TARGET}-official")
    return text + (f'CLIENT_BIN="/opt/claude-{TARGET}/claude"\nCLIENT_BIN_SHA256="{"c" * 64}"\nCLIENT_ACCOUNT_ID="17"\n')


def load_parse_env():
    return driver_tests.load_script("parse_env")


class ClientTableStructureTests(unittest.TestCase):
    def test_driver_carries_an_identical_copy(self) -> None:
        self.assertEqual((DRIVER / "client_table.py").read_bytes(), (REPO_ROOT / "tools/ci/client_table.py").read_bytes())

    def test_rows_close_and_codex_is_the_default(self) -> None:
        self.assertEqual(CT.CLIENTS, ("codex", "claude"))
        self.assertEqual(set(CT.TABLE["codex"]), set(CT.TABLE["claude"]))
        self.assertEqual(CT.resolve_client(None), "codex")
        self.assertEqual(CT.resolve_client(""), "codex")
        self.assertEqual(CT.client_of({}), "codex")
        with self.assertRaisesRegex(CT.ClientTableError, "CLIENT 不认识"):
            CT.resolve_client("kilo")
        self.assertEqual(CT.TABLE["codex"]["pending"], {}, "Codex 没有待接入的条目")
        for name, reason in CT.TABLE["claude"]["pending"].items():
            self.assertRegex(reason, r"^B\d[a-z]?-\d{2}", f"{name} 要写明落实它的工作项")

    def test_shell_view_assigns_without_exporting_and_covers_every_script_reference(self) -> None:
        emitted = {line.split("=", 1)[0] for line in CT.shell_lines("claude")}
        self.assertEqual(emitted, {line.split("=", 1)[0] for line in CT.shell_lines("codex")}, "两客户端输出同一组变量")
        referenced = set()
        for path in DRIVER.glob("*.sh"):
            referenced.update(re.findall(r"\bCT_[A-Z0-9_]+", path.read_text(encoding="utf-8")))
        self.assertTrue(referenced)
        self.assertLessEqual(referenced, emitted, "驱动脚本引用的 CT_ 变量都由客户端表给出（拼错即红）")
        script = ('set -Eeuo pipefail\neval "$(python3 -B "$1" shell)"\n'
                  'printf "%s|%s|%s|" "$CT_CLIENT" "$CT_ORCHESTRATOR_MODULE" "$CT_PENDING_CHECK_PLAN_AUDIT"\n'
                  'printf "[%s]" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}\necho\nenv | grep -c "^CT_" || true\n')
        environment = {key: value for key, value in os.environ.items() if key != "CLIENT"}
        for client, expected_argv in (("codex", "[]"), ("claude", "[--client][claude]")):
            completed = subprocess.run(["bash", "-c", script, "client-table", str(DRIVER / "client_table.py")],
                                       env={**environment, "CLIENT": client}, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            first, exported = completed.stdout.splitlines()
            module = CT.spec(client)["orchestrator_module"]
            pending = CT.pending(client, "check:plan-audit") or ""
            self.assertEqual(first, f"{client}|{module}|{pending}|{expected_argv}")
            self.assertEqual(exported, "0", "只赋值、不导出：子进程看不到 CT_ 变量")
        unknown = subprocess.run([sys.executable, "-B", str(DRIVER / "client_table.py"), "shell"],
                                 env={**environment, "CLIENT": "kilo"}, capture_output=True, text=True)
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("CLIENT 不认识", unknown.stderr)

    def test_table_agrees_with_adapters_deployer_installer_and_certification(self) -> None:
        install = driver_tests.driver
        for client in CT.CLIENTS:
            with self.subTest(client):
                row, adapter = CT.spec(client), uca.adapter_for(client)
                self.assertEqual(row["deploy_receipt_glob"], adapter.deploy_receipt_glob)
                self.assertEqual(set(row["orchestrator_basenames"]), set(adapter.orchestrator_basenames))
                self.assertEqual(row["project_ledger_dir_name"], adapter.project_ledger_dir_name)
                self.assertEqual(row["deploy_receipt_glob"], deploy.DEPLOY_RECEIPT_GLOBS[client])
                self.assertEqual(tuple(row["managed_roots"]), deploy.CLIENT_MANAGED_ROOTS[client])
                self.assertEqual(tuple(row["repository_documents"]), deploy.CLIENT_MANAGED_DOCUMENTS[client])
                self.assertEqual(row["deploy_receipt_glob"], install.DEPLOY_RECEIPT_GLOBS[client])
                self.assertEqual(row["deploy_receipt_stamp_separator"], install.DEPLOY_RECEIPT_STAMP_SEPARATORS[client])
                self.assertEqual(row["deploy_receipt_identity_key"], install.DEPLOY_RECEIPT_IDENTITY_KEYS[client])
                self.assertEqual(tuple(row["client_argv"]), pre_a3.CLIENT_ARGV[client])
                self.assertEqual(eg.PRE_A3_GATE_COMMANDS[client][0], [*eg.GATE_COMMANDS["pre-a3"][0], *row["client_argv"]])
        self.assertEqual(CT.spec("codex")["deploy_receipt_schema"], deploy.CODEX_DEPLOY_RECEIPT_SCHEMA)
        self.assertEqual(CT.spec("claude")["deploy_receipt_schema"], deploy.CLAUDE_DEPLOY_RECEIPT_SCHEMA)
        self.assertEqual(install.DEPLOY_CLIENTS, CT.CLIENTS)

    def test_bindings_point_at_real_files_or_pending_steps(self) -> None:
        # 只有这几项模块允许还没有文件：它的消费步骤必须登记为待接入。
        consumers = {"closeout_module": "step:vc0-closeout", "zero_request_smoke_module": "step:zero-request-smoke"}
        for client in CT.CLIENTS:
            row = CT.spec(client)
            for key, value in row.items():
                if not key.endswith("_module"):
                    continue
                with self.subTest(client=client, key=key):
                    path = REPO_ROOT / (value.replace(".", "/") + ".py")
                    if not path.is_file():
                        self.assertIn(key, consumers, f"{client}.{key} 指向不存在的模块 {value}")
                        self.assertIsNotNone(CT.pending(client, consumers[key]), f"{value} 还不存在，{consumers[key]} 必须登记为待接入")
            self.assertTrue((OCC / row["identity_policy_file"]).is_file())
            self.assertTrue((OCC / row["orchestrator_file"]).is_file())
            for name in row["environment_receipt_files"]:
                self.assertTrue((OCC / name).is_file(), name)
            source = (REPO_ROOT / (row["environment_receipt_module"].replace(".", "/") + ".py")).read_text(encoding="utf-8")
            self.assertIn(f'"{row["environment_probe_version_option"]}"', source, "探针版本参数是环境收据采集器的真实选项")
            guide = (REPO_ROOT / "docs" / row["guide_file"]).read_text(encoding="utf-8").splitlines()
            self.assertIn(row["guide_part2_heading"], guide, "第二部分标题是指南里真实的一级标题")
            for name in (*row["entry_guides"], *row["repository_documents"]):
                self.assertTrue((REPO_ROOT / "docs" / name).is_file(), name)
            for part in row["deploy_consistency_parts"]:
                self.assertTrue((REPO_ROOT / part).exists(), part)

    def test_driver_has_no_version_literals(self) -> None:
        """驱动（含两份客户端表）里不出现任何客户端的具体版本：版本只来自本轮参数文件。"""

        patterns = [r"(?<![\w.])0[._]1[5-9][0-9](?![\w])", r"\bc01[5-9][0-9]", r"codex-01[5-9]", r"(?<![\w.])2[._]1[._][0-9]{2,3}(?![\w])",
                    r"claude-code-[0-9]", r"/opt/(?:codex|claude)-[0-9]"]
        files = [path for path in (*DRIVER.rglob("*"), REPO_ROOT / "tools/ci/client_table.py") if path.is_file()]
        for path in files:
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                with self.subTest(path=path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text), f"{path.name} 含版本字面量")
        for sample, pattern in (("codex-0.160.0", patterns[0]), (f"claude-code-{TARGET}", patterns[4]), (f"/opt/claude-{TARGET}", patterns[5]),
                                (TARGET, patterns[3])):
            self.assertIsNotNone(re.search(pattern, sample), sample)


class ParseEnvClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.fixture = driver_tests._DriverFixture(self.root)
        self.parser = load_parse_env()
        self.text = claude_env_text(self.fixture)

    def test_claude_parameters_expand_every_derived_key(self) -> None:
        values = self.parser.parse(self.text)
        self.assertEqual((values["CLIENT"], values["CLIENT_BIN"]), ("claude", f"/opt/claude-{TARGET}/claude"))
        derived = self.parser.derive(values)
        codex_derived = self.parser.derive(self.parser.parse(self.fixture.env_file.read_text(encoding="utf-8")))
        self.assertEqual(set(derived) - {"CLIENT", "CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID"}, set(codex_derived),
                         "与 Codex 派生同一组键（另加本轮写的客户端键）")
        data, tools = str(self.fixture.data_root), f"{self.fixture.data_root}/tools/official_client_capture"
        expected = {
            "CAMPAIGN_PREFIX": "claude-code-2_1_245", "TARGET_TAG": "claude-21245",
            "RULES_JSON": f"{tools}/claude_upgrade_rules_2_1_245.json",
            "BASELINE_RULES_JSON": f"{tools}/claude_upgrade_rules_2_1_226.json",
            "SCENARIOS_JSON": f"{tools}/claude_upgrade_scenarios_2_1_245.json",
            "BASELINE_SCENARIOS_JSON": f"{tools}/claude_upgrade_scenarios_2_1_226.json",
            "EXPECTATIONS_JSON": f"{tools}/claude_rule_expectations_2_1_245.json",
            "PROFILE_PATCH_JSON": f"{tools}/claude_profile_rule_patches_2_1_245.json",
            "LIFECYCLE_DIR": "docs/egress/lifecycle/claude-21245-candidate",
            "CANDIDATE_IMAGE_REPOSITORY": "sub2apiplus-claude-code-2_1_245-candidate",
            "BASELINE_SOURCE": f"{data}/official/claude-code-{BASELINE}", "TARGET_SOURCE": f"{data}/official/claude-code-{TARGET}",
            "TARGET_PACKAGE": f"{data}/official/claude-code-{TARGET}/freeze.json",
            "PRE_A3_CERTIFICATION": f"{data}/control/policy-certification/claude-pre-a3-path-certification-{self.fixture.stamp}.json",
        }
        for key, value in expected.items():
            with self.subTest(key):
                self.assertEqual(derived[key], value)
        self.assertRegex(derived["CAMPAIGN_PREFIX"], CLAUDE_CAMPAIGN_ID_RE, "Claude 前缀落在适配器认作易变维度的 Campaign ID 形态里")
        path = self.root / "claude.sh"
        path.write_text(self.text, encoding="utf-8")
        completed = subprocess.run([sys.executable, "-B", str(DRIVER / "parse_env.py"), str(path)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("export CLIENT=claude", completed.stdout)
        for key in ("KILO_BIN", "KILO_VERSION", "KILO_SHA256", "CODEX_BIN", "CODEX_BIN_SHA256", "CODEX_ACCOUNT_ID"):
            self.assertIn(f"unset {key}\n", completed.stdout, "外层环境残留的 Codex 专属参数被清掉")
            self.assertNotIn(f"export {key}=", completed.stdout)

    def test_claude_rejects_codex_keys_and_requires_generic_keys(self) -> None:
        cases = {
            "Codex 二进制键": (self.text + 'CODEX_BIN="/opt/codex/bin/codex"\n', "专属键"),
            "Kilo": (self.text + 'KILO_SHA256="' + "d" * 64 + '"\n', "专属键"),
            "缺二进制": (re.sub(r"^CLIENT_BIN=.*\n", "", self.text, flags=re.M), "CLIENT_BIN"),
            "摘要非法": (re.sub(r"^CLIENT_BIN_SHA256=.*$", "CLIENT_BIN_SHA256=bad", self.text, flags=re.M), "CLIENT_BIN_SHA256"),
            "账号非法": (re.sub(r"^CLIENT_ACCOUNT_ID=.*$", "CLIENT_ACCOUNT_ID=0", self.text, flags=re.M), "CLIENT_ACCOUNT_ID"),
            "客户端不认识": (self.text.replace("CLIENT=claude", "CLIENT=kilo"), "CLIENT 不认识"),
        }
        for label, (text, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(self.parser.EnvFileError, message):
                self.parser.parse(text)
        self.assertNotIn("TARGET_CODE_MODE_HOST_SHA256", self.parser.required_keys("claude"))
        self.assertEqual(self.parser.required_keys("codex"), self.parser.REQUIRED_KEYS)

    def test_template_documents_a_parsable_claude_round(self) -> None:
        """参数模板末尾注释里的 Claude 写法：取消注释、替换占位与示例版本后能按 Claude 解析。"""

        template = (DRIVER / "env.example.sh").read_text(encoding="utf-8")
        overrides = dict(re.findall(r"^#   ([A-Z_][A-Z0-9_]*)=([^\s（]+)", template, flags=re.M))
        self.assertEqual({"CLIENT", "TARGET_PROFILE_ID", "NEW", "IN", "UP", "CAND", "BUNDLE_BRANCH", "CLIENT_BIN", "CLIENT_BIN_SHA256",
                          "CLIENT_ACCOUNT_ID", "BASELINE_SOURCE", "TARGET_SOURCE", "TARGET_PACKAGE", "ENTRY_BRANCH"}, set(overrides))
        lines = []
        for line in template.splitlines():
            key = line.split("=", 1)[0]
            if key in {"KILO_BIN", "KILO_VERSION", "KILO_SHA256"}:
                continue
            lines.append(f"{key}={overrides[key]}" if key in overrides and not line.startswith("#") else line)
        lines += [f"{key}={value}" for key, value in overrides.items() if key in {"BASELINE_SOURCE", "TARGET_SOURCE", "TARGET_PACKAGE"}]
        text = "\n".join(lines) + "\n"
        for placeholder in ("REPLACE_APPROVED_PROFILE_SHA256", "REPLACE_OFFICIAL_CLAUDE_SHA256", "REPLACE_OFFICIAL_PACKAGE_SHA256"):
            text = text.replace(placeholder, "a" * 64)
        text = text.replace("REPLACE_CLAUDE_ACCOUNT_ID", "17").replace("REPLACE_API_KEY_ID", "92").replace("/root/", f"{self.root}/")
        (self.root / "docker/capture-cli/data").mkdir(parents=True, exist_ok=True)
        values = self.parser.parse(text)
        derived = self.parser.derive(values)
        self.assertEqual(values["CLIENT"], "claude")
        self.assertTrue(values["NEW"].startswith("claude-code-9.1.0-formal-"))
        self.assertTrue(derived["TARGET_PACKAGE"].endswith("/official/claude-code-9.1.0/freeze.json"))

    def test_lib_sh_gives_unexported_client_table_variables(self) -> None:
        drv = self.root / "drv"
        drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "client_table.py"):
            shutil.copy2(DRIVER / name, drv / name)
        (drv / "probe.sh").write_text('#!/bin/bash\nset -Eeuo pipefail\nsource "$(dirname "$0")/lib.sh"\n'
                                      'printf "%s|%s|%s|%s\\n" "$CT_CLIENT" "$CT_BINARY_KEY" "$CLIENT" "${!CT_BINARY_KEY}"\n'
                                      'env | grep -c "^CT_" || true\nenv | grep -c "^CODEX_BIN=" || true\n', encoding="utf-8")
        path = self.root / "claude.sh"
        path.write_text(self.text, encoding="utf-8")
        completed = subprocess.run(["bash", str(drv / "probe.sh")], capture_output=True, text=True,
                                   env={**os.environ, "ARM64_VC_ENV": str(path), "CODEX_BIN": "/stale/codex", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        line, exported, stale = completed.stdout.splitlines()[-3:]
        self.assertEqual(line, f"claude|CLIENT_BIN|claude|/opt/claude-{TARGET}/claude")
        self.assertEqual((exported, stale), ("0", "0"), "CT_ 变量不导出；外层残留的 CODEX_BIN 被清掉")


class EntryStepsClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = Fixture(Path(directory.name).resolve())
        self.fx.params.update(CLIENT="claude", CLIENT_BIN=self.fx.params.pop("CODEX_BIN"),
                              CLIENT_BIN_SHA256=self.fx.params.pop("CODEX_BIN_SHA256"), CLIENT_ACCOUNT_ID=self.fx.params.pop("CODEX_ACCOUNT_ID"))
        self.receipt = self.fx.data / "control" / "claude-managed-deploy-20261005t000000z.json"
        self.receipt.write_text(json.dumps({"status": "passed", "client": "claude",
                                            "identity": {"tool_files_sha256": "e" * 64}}), encoding="utf-8")
        self.requests: list[dict] = []
        original = self.fx.runner

        def runner(argv, env=None, cwd=None):
            if list(argv)[:3] == [sys.executable, "-B", "-c"]:
                self.requests.append(json.loads(argv[4]))
            return original(argv, env, cwd)

        self.runner = runner

    def ctx(self) -> es.Context:
        return es.Context(params=dict(self.fx.params), driver_dir=self.fx.driver, steps_dir=self.fx.steps_dir, runner=self.runner)

    def test_claude_step_table_keeps_the_structure_and_declares_claude_inputs(self) -> None:
        codex, claude = es.STEP_BY_ID_BY_CLIENT["codex"], es.STEP_BY_ID_BY_CLIENT["claude"]
        self.assertEqual(list(claude), list(codex))
        names = {step_id: {item.name for item in step.inputs} for step_id, step in claude.items()}
        self.assertIn("tree:{D}/tools/official_client_control#no_tests", names["pre-a3"])
        self.assertIn("tree:{D}/tools/official_client_control/tests#pycache", names["pre-a3"])
        self.assertIn("file:{D}/docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", names["pre-a3"])
        self.assertIn("file:{D}/docs/OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md", names["vc0-closeout"])
        self.assertNotIn("file:{D}/docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md", names["pre-a3"] | names["vc0-closeout"])
        self.assertIn("file:{D}/tools/official_client_capture/claude_tool_identity_policy_v2.json", names["policy-compatibility"])
        self.assertIn("command:codex_upgrade_policy_certification compatibility --client claude", names["policy-compatibility"])
        self.assertIn("command:codex_upgrade_policy_certification activation --client claude", names["policy-activation"])
        self.assertIn("file:/opt/claude-{TARGET_VERSION}/claude", names["environment-p0"])
        self.assertIn("file:{D}/tools/official_client_capture/claude_upgrade_arm64_environment_receipt.py", names["environment-p0"])
        self.assertIn("file:{CLIENT_BIN}", names["entry-preflight"] & names["job-rehearsal"])
        self.assertTrue({"param:CLIENT_BIN", "param:CLIENT_BIN_SHA256", "param:CLIENT_ACCOUNT_ID"} <= names["preflight-plan"])
        self.assertFalse({name for name in set().union(*names.values()) if "CODEX_" in name or "TARGET_CODE_MODE_HOST" in name})
        self.assertTrue(all("driver:client_table.py" in names[step_id] for step_id, step in claude.items()
                            if any(item.name == "driver:lib.sh" for item in step.inputs)), "Claude 的步骤登记客户端表")
        self.assertIn("tree:{D}/upgrade-project-ledger-claude#pycache", names["pre-a3"])

    def test_claude_context_reads_claude_receipt_and_identity(self) -> None:
        ctx = self.ctx()
        self.assertIs(ctx.steps, es.STEPS_BY_CLIENT["claude"])
        self.assertEqual(ctx.latest_deploy_receipt(), self.receipt)
        field = es.resolve(es.Input("upstream", "deploy_field", "tool_files_sha256"), ctx)
        self.assertEqual(field["detail"]["value"], json.dumps("e" * 64))
        es.resolve(es.Input("managed", "identity", "control_sha256"), ctx)
        self.assertEqual(self.requests[-1], {"identity": True, "identity_args": ["claude"]}, "五摘要按 Claude 策略算")
        inputs = {entry["name"] for entry in es.gate_inputs("pre-a3", ctx)}
        self.assertIn("tree:{D}/tools/official_client_control#no_tests", inputs)
        result = es.evaluate(ctx, is_formal_built=False)
        self.assertEqual([step["step_id"] for step in result["steps"]], list(es.STEP_BY_ID_BY_CLIENT["claude"]))
        bad = es.Context(params={**self.fx.params, "CLIENT": "kilo"}, driver_dir=self.fx.driver, steps_dir=self.fx.steps_dir)
        with self.assertRaisesRegex(es.EntryStepsError, "CLIENT 不认识"):
            bad.steps

    def test_deploy_consistency_covers_the_control_root_and_claude_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for side in ("tree", "data"):
                for part in CT.spec("claude")["deploy_consistency_parts"]:
                    path = root / side / part
                    if part.endswith((".md", ".py")):
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("x\n", encoding="utf-8")
                    else:
                        (path / "a.py").parent.mkdir(parents=True, exist_ok=True)
                        (path / "a.py").write_text("x\n", encoding="utf-8")
            self.assertEqual(es.deploy_consistency(root / "tree", root / "data", "claude"), [])
            (root / "data" / "tools/official_client_control/a.py").write_text("y\n", encoding="utf-8")
            self.assertEqual(es.deploy_consistency(root / "tree", root / "data", "claude"), ["tools/official_client_control"])
            self.assertNotIn("tools/official_client_control", es.deploy_consistency(root / "tree", root / "data"),
                             "Codex 不比第二受管根")
            completed = subprocess.run([sys.executable, "-B", str(DRIVER / "entry_steps.py"), "deploy-consistency", "--tree", str(root / "tree"),
                                        "--data-root", str(root / "data"), "--client", "claude"], capture_output=True, text=True)
            self.assertEqual((completed.returncode, completed.stdout.strip()), (1, "tools/official_client_control"))


class EntryOrchestratorClaudeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.fx = Fixture(self.root)
        shutil.rmtree(self.fx.data / "control" / f"{self.fx.params['UP']}-timing-ledger")
        (self.fx.data / "audit" / f"zero-request-smoke-{self.fx.params['STAMP']}.json").unlink()
        self.fx.params.update({"RUNROOT": str(self.root / "run"), "ENTRY_BUNDLE": str(self.root / "x.bundle"), "ENTRY_BRANCH": "claude/test",
                               "BASELINE_RULES_JSON": str(self.root / "rules.json"), "BASELINE_SCENARIOS_JSON": str(self.root / "scenarios.json"),
                               "CLIENT": "claude"})
        (self.fx.data / "control" / "claude-managed-deploy-20261005t000000z.json").write_text(
            json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "f" * 64}}), encoding="utf-8")
        self.commands = Commands(self.fx)
        self.runs = 0

    def orchestrator(self, runner=None) -> eo.Orchestrator:
        self.runs += 1
        return eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.root / "runs" / str(self.runs),
                               steps_dir=self.fx.steps_dir, runner=runner or self.commands, es_runner=self.fx.runner)

    def test_pending_steps_are_listed_in_the_plan_and_never_executed(self) -> None:
        plan = self.orchestrator().orchestrate(plan_only=True)
        reasons = {step["step_id"]: "；".join(step["reasons"]) for step in plan["steps"]}
        for name, work_item in CT.TABLE["claude"]["pending"].items():
            kind, _, step_id = name.partition(":")
            if kind == "step":
                self.assertIn(f"尚未接入：{work_item}", reasons[step_id])
        result = self.orchestrator().orchestrate()
        actions = {step["step_id"]: (step["action"], step["status"], "；".join(step["reasons"])) for step in result["steps"]}
        for step_id in ("zero-request-smoke", "atomic-double"):
            self.assertEqual(actions[step_id][:2], ("执行", "failed"))
            self.assertIn("B5a-09", actions[step_id][2])
        joined = [" ".join(call) for call in self.commands.calls]
        self.assertFalse([call for call in joined if "zero_request_smoke" in call or call.startswith("docker exec")],
                         "待接入的步骤不执行，不拿 Codex 的实现顶替")
        policy = [call for call in self.commands.calls if "codex_upgrade_policy_certification" in " ".join(call)]
        self.assertEqual([call[-2:] for call in policy], [["--client", "claude"]] * 2, "策略兼容与激活认证带 --client claude")
        self.assertEqual(result["exit_code"], 1, "建账本之前有步骤没通过：不建账本")

    def test_claude_ledger_and_environment_commands(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, cwd, env, log):
            calls.append(list(argv))
            return 1

        orchestrator = self.orchestrator(runner)
        orchestrator.step_ledger()
        orchestrator.step_environment_p0()
        ledger, environment = calls
        self.assertEqual(ledger[3], CT.spec("claude")["timing_ledger_module"])
        self.assertEqual(ledger[-2:], ["--client", "claude"])
        self.assertTrue(_option(ledger, "--project-ledger-dir").endswith("/evidence/campaigns/upgrade-project-ledger-claude"))
        self.assertEqual(environment[3], "tools.official_client_capture.claude_upgrade_arm64_environment_receipt")
        self.assertEqual(_option(environment, "--claude-version"), self.fx.params["TARGET_VERSION"])
        self.assertNotIn("--rust-tls-codex-version", environment)


class EntryGatesClaudeTests(unittest.TestCase):
    SCENARIO_TEST = "tools/official_client_capture/tests/test_claude_root_cause_codes.py"

    def test_claude_scenario_units_declare_the_control_root(self) -> None:
        names = lambda declaration: [item["name"] for item in declaration["ranges"]]  # noqa: E731
        self.assertEqual(names(eg.pre_a3_inputs(self.SCENARIO_TEST, [])), ["repo:managed", "repo:tests-fixtures", "repo:docs"])
        self.assertEqual(names(eg.pre_a3_inputs(self.SCENARIO_TEST, [], "claude")),
                         ["repo:managed", "repo:tests-fixtures", "repo:docs", "repo:official_client_control",
                          "repo:official_client_control-tests"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            units = root / "units.json"
            units.write_text(json.dumps({"schema_version": eg.COMMANDS_SCHEMA, "units": [
                {"unit_id": "pre-a3:claude:fixture", "argv": ["python3", "-m", "x"], "cwd": ".", "cores": 1, "memory_mb": 256,
                 "timeout_seconds": 60, "weight": 1.0, "scenario_test_file": self.SCENARIO_TEST}]}), encoding="utf-8")
            self.assertEqual(eg.pre_a3_units_client(units), "claude")
            manifest = eg.plan_gates(Path("."), profile="pre-a3", launcher=["launcher"], pre_a3_units=units, pre_a3_data_inputs=[],
                                     platform="linux")
            self.assertIn("repo:official_client_control", names(manifest["units"][0]["inputs"]))
            data = root / "data"
            data.mkdir()
            claude = [entry["name"] for entry in eg.pre_a3_data_root_inputs(data, "claude")]
            codex = [entry["name"] for entry in eg.pre_a3_data_root_inputs(data)]
            self.assertEqual(claude, codex + ["tree:{D}/staging/upgrade-project-ledger-claude#pycache",
                                              "tree:{D}/upgrade-project-ledger-claude#pycache"])

    def _repo(self, overrides: dict[str, str]) -> records.RepoIndex:
        """受管两根、文档与部署脚本的跟踪文件（文件系统列举，不读 git）配合成摘要；``overrides`` 改其中几个文件的摘要。"""

        files: dict[str, str] = {}
        for base in ("tools/official_client_capture", "tools/official_client_control", "docs"):
            for path in (REPO_ROOT / base).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    files[path.relative_to(REPO_ROOT).as_posix()] = "0" * 64
        files["tools/arm64_supervised_deploy.py"] = "0" * 64
        files.update(overrides)
        return records.RepoIndex(REPO_ROOT, "0" * 40, files, [])

    def test_control_root_changes_only_invalidate_claude_scenario_units(self) -> None:
        data_inputs = [{"category": "environment", "name": "tree:fixed", "sha256": "0" * 64, "detail": {}}]
        declarations = {client: eg.pre_a3_inputs(self.SCENARIO_TEST, data_inputs, client) for client in ("codex", "claude")}
        cache: dict = {}
        digest = lambda repo, client: records.entries_sha256(records.declared_inputs(repo, declarations[client], cache))  # noqa: E731
        base = self._repo({})
        before = {client: digest(base, client) for client in declarations}
        changes = ("tools/official_client_control/store.py",
                   *sorted(path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / "tools/official_client_control/schemas").glob("*.json")))
        self.assertGreater(len(changes), 2)
        for changed in changes:
            with self.subTest(changed):
                repo = self._repo({changed: "1" * 64})
                self.assertEqual(digest(repo, "codex"), before["codex"], "Codex 场景单元不声明第二受管根，不受影响")
                self.assertNotEqual(digest(repo, "claude"), before["claude"], "Claude 场景单元的输入摘要变化，场景失效重跑")
        repo = self._repo({"tools/official_client_capture/codex_upgrade.py": "1" * 64})
        self.assertNotEqual(digest(repo, "codex"), before["codex"], "对照：共享受管根的变化两客户端都失效")
        self.assertNotEqual(digest(repo, "claude"), before["claude"])

    def test_tree_identity_is_computed_with_the_client_policy(self) -> None:
        identity = {name: "a" * 64 for name in eg.IDENTITY_FIELDS}
        argvs = []

        def run(argv, **options):
            argvs.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, json.dumps(identity) + "\n", "")

        with mock.patch.object(eg.subprocess, "run", side_effect=run):
            self.assertEqual(eg.tree_tool_identity("/tree"), identity)
            self.assertEqual(eg.tree_tool_identity("/tree", "claude"), identity)
        self.assertEqual([argv[4:] for argv in argvs], [[], ["claude"]], "Codex 不带参数（与改造前相同），Claude 带客户端名")


class EntryScriptClaudeTests(unittest.TestCase):
    """驱动脚本按客户端表取值的几处（静态核对加按 Claude 取值运行抽出的函数）。"""

    @staticmethod
    def function(script: str, name: str) -> str:
        text = (DRIVER / script).read_text(encoding="utf-8")
        match = re.search(rf"^{name}\(\) \{{.*?^\}}\n", text, re.S | re.M)
        assert match is not None, name
        return match.group(0)

    def shell(self, body: str, client: str, **env: str) -> subprocess.CompletedProcess[str]:
        script = 'set -Euo pipefail\neval "$(python3 -B "$1" shell)"\n' + body
        environment = {key: value for key, value in os.environ.items() if key != "CLIENT"}
        return subprocess.run(["bash", "-c", script, "entry-script", str(DRIVER / "client_table.py")],
                              env={**environment, "CLIENT": client, **env}, capture_output=True, text=True)

    def test_preflight_checks_follow_the_client_table(self) -> None:
        audit = self.shell(self.function("entry-preflight.sh", "plan_audit") + "plan_audit\n", "claude")
        self.assertEqual(audit.returncode, 1)
        self.assertIn("plan-audit 尚未接入：B5a-09", audit.stdout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            binary = root / "claude"
            binary.write_text(f"#!/bin/bash\necho '{TARGET} (Claude Code)'\n", encoding="utf-8")
            binary.chmod(0o700)
            (root / "docker").write_text('#!/bin/bash\nshift 2\nexec "$@"\n', encoding="utf-8")
            (root / "docker").chmod(0o700)
            digest = subprocess.run(["shasum", "-a", "256", str(binary)], capture_output=True, text=True).stdout.split()[0]
            (root / "sha256sum").write_text('#!/bin/bash\nexec shasum -a 256 "$@"\n', encoding="utf-8")
            (root / "sha256sum").chmod(0o700)
            env = {"PATH": f"{root}:{os.environ['PATH']}", "CLIENT_BIN": str(binary), "CLIENT_BIN_SHA256": digest, "TARGET_VERSION": TARGET}
            target = self.shell(self.function("entry-preflight.sh", "target_client") + "target_client\n", "claude", **env)
            self.assertEqual(target.returncode, 0, target.stdout + target.stderr)
            self.assertIn(f"登记 {digest}", target.stdout)
            wrong = self.shell(self.function("entry-preflight.sh", "target_client") + "target_client\n", "claude",
                               **{**env, "CLIENT_BIN_SHA256": "0" * 64})
            self.assertNotEqual(wrong.returncode, 0)
            required = {key: "x" for key in CT.spec("claude")["preflight_required"]}
            required["P0_ROLLBACK_EVIDENCE"] = str(binary)
            check = self.shell(self.function("entry-preflight.sh", "required_parameters") + "required_parameters\n", "claude", **required)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_entry_gates_and_guard_bind_the_client(self) -> None:
        gates = (DRIVER / "entry-gates.sh").read_text(encoding="utf-8")
        deployed = self.function("entry-gates.sh", "check_deployed")
        self.assertIn('"$DRV/../install.py" "$D" "$CT_CLIENT"', deployed)
        self.assertIn("latest_deploy_receipt(Path(sys.argv[2])/'control', sys.argv[3])", deployed)
        self.assertIn('${CT_IDENTITY_ARGS[@]+"${CT_IDENTITY_ARGS[@]}"}', deployed)
        self.assertIn('"$DEPLOY" "$CT_DEPLOY_RECEIPT_IDENTITY_KEY"', deployed)
        self.assertIn('deploy-consistency --tree "$TREE" --data-root "$D" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}', deployed)
        policy = [line for line in gates.splitlines() if '"$CT_POLICY_CERTIFICATION_MODULE"' in line]
        self.assertEqual(len(policy), 2, "补签兼容收据与激活认证")
        self.assertTrue(all('${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}' in line for line in policy))
        guard = (DRIVER / "guard.sh").read_text(encoding="utf-8")
        verify = next(line for line in guard.splitlines() if 'install.py" verify' in line)
        self.assertIn('${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}', verify)
        self.assertIn('--ledger-dir "$D/evidence/campaigns/$CT_PROJECT_LEDGER_DIR_NAME"', guard)
        # Claude 取值下，部署收据身份对象与总账目录：
        values = self.shell('printf "%s|%s|%s" "$CT_DEPLOY_RECEIPT_IDENTITY_KEY" "$CT_PROJECT_LEDGER_DIR_NAME" "${CT_IDENTITY_ARGS[*]}"\n',
                            "claude")
        self.assertEqual(values.stdout, "identity|upgrade-project-ledger-claude|claude")


class InstallMultiClientTests(unittest.TestCase):
    """尾巴 3：两客户端都已纳管时，一次安装绑定两份部署收据（v2）；复验按客户端取该客户端当前最新的那份。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.data = self.root / "data"
        self.control = self.data / "control"
        self.control.mkdir(parents=True, mode=0o700)
        self.target = self.root / "arm64-capture-driver"
        self.install = driver_tests.driver
        for patch in (mock.patch.object(self.install, "_running_as_root", return_value=False),
                      mock.patch.object(self.install, "_require_root", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def codex_receipt(self, stamp: str, digest: str) -> Path:
        path = self.control / f"codex-01600-supervisor-enable-{stamp}.json"
        path.write_text(json.dumps({"status": "passed", "tool_files_sha256": digest, "supervisor_sha256": "s" * 64}), encoding="utf-8")
        return path

    def claude_receipt(self, stamp: str, digest: str) -> Path:
        path = self.control / f"claude-managed-deploy-{stamp}.json"
        path.write_text(json.dumps({"status": "passed", "client": "claude", "supervisor_sha256": "s" * 64,
                                    "identity": {"tool_files_sha256": digest}}), encoding="utf-8")
        return path

    def run_install(self, *extra: str) -> dict:
        before = set(self.control.glob("arm64-capture-driver-install-*.json"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.install.main(["install", "--source", str(REPO_ROOT / "tools/arm64_capture_driver"), "--target", str(self.target),
                                                "--data-root", str(self.data), *extra]), 0)
        # 同一秒里的几次安装，收据名只差随机后缀：按「这次新写的那一份」取，不按名字排序。
        [written] = set(self.control.glob("arm64-capture-driver-install-*.json")) - before
        return self.install.validate_install_receipt(json.loads(written.read_text(encoding="utf-8")))

    def test_both_managed_clients_are_bound_and_verified_per_client(self) -> None:
        codex = self.codex_receipt("20261007t000000z", "a" * 64)
        claude = self.claude_receipt("20261007t000000z", "b" * 64)
        self.assertEqual(self.install.managed_clients(self.control), ["codex", "claude"])
        receipt = self.run_install()
        self.assertEqual(receipt["schema_version"], self.install.INSTALL_RECEIPT_SCHEMA_V2)
        self.assertEqual({client: item["path"] for client, item in receipt["deployment_receipts"].items()},
                         {"codex": codex.name, "claude": claude.name})
        self.assertEqual(receipt["deployment_receipts"]["claude"]["tool_files_sha256"], "b" * 64)
        self.assertEqual(self.install.verify_install(self.target, self.data)["deployment_receipt"], codex.name)
        self.assertEqual(self.install.verify_install(self.target, self.data, "claude")["deployment_receipt"], claude.name)
        newer = self.claude_receipt("20261008t000000z", "c" * 64)
        with self.assertRaisesRegex(self.install.DriverError, "不是当前最新"):
            self.install.verify_install(self.target, self.data, "claude")
        self.assertEqual(self.install.verify_install(self.target, self.data)["status"], "verified", "Codex 的最新收据没变，照常复验")
        receipt = self.run_install()
        self.assertEqual(receipt["deployment_receipts"]["claude"]["path"], newer.name)
        self.assertEqual(self.install.verify_install(self.target, self.data, "claude")["deployment_receipt"], newer.name)
        explicit = self.run_install("--client", "claude")
        self.assertEqual((explicit["schema_version"], explicit["deployment_receipt"]["path"]),
                         (self.install.INSTALL_RECEIPT_SCHEMA, newer.name), "显式 --client 只绑定该客户端（v1）")
        completed = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "tools/arm64_capture_driver/install.py"), "verify", "--target",
                                    str(self.target), "--data-root", str(self.data), "--client", "claude"], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["deployment_receipt"], newer.name, "命令行 verify --client claude 按 Claude 判最新")

    def test_v2_receipt_contract_is_closed(self) -> None:
        self.codex_receipt("20261007t000000z", "a" * 64)
        self.claude_receipt("20261007t000000z", "b" * 64)
        receipt = self.run_install()
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        for label, mutate in (("只有一份绑定", lambda payload: payload["deployment_receipts"].pop("claude")),
                              ("客户端不认识", lambda payload: payload["deployment_receipts"].update(kilo=payload["deployment_receipts"]["codex"])),
                              ("混入 v1 字段", lambda payload: payload.update(deployment_receipt={}))):
            with self.subTest(label):
                payload = json.loads(json.dumps(unsigned))
                mutate(payload)
                payload["receipt_sha256"] = self.install._sha256_bytes(self.install._canonical(payload))
                with self.assertRaises(self.install.DriverError):
                    self.install.validate_install_receipt(payload)


class FixAndContinueDeployScopeTests(unittest.TestCase):
    """尾巴 1：修复轮部署按纳管状态带事务范围参数、复制签收据各客户端的活动文档。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.control = self.root / "data" / "control"
        self.control.mkdir(parents=True)
        (self.control / "codex-01600-supervisor-enable-20261007t000000z.json").write_text("{}", encoding="utf-8")
        self.helper = driver_tests.load_script("fix_and_continue")
        self.params = {"D": str(self.root / "data")}

    def test_codex_only_keeps_the_original_command_and_documents(self) -> None:
        self.assertEqual(self.helper.deploy_scope(self.params),
                         ([], ["OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"]))

    def test_claude_managed_signs_both_and_swaps_both_roots(self) -> None:
        (self.control / "claude-managed-deploy-20261007t000000z.json").write_text("{}", encoding="utf-8")
        arguments, documents = self.helper.deploy_scope(self.params)
        self.assertEqual(arguments, ["--client", "codex", "claude", "--managed-roots", "tools/official_client_capture",
                                     "tools/official_client_control"])
        self.assertEqual(documents, ["OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
                                     "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md"])
        # 部署器接受这组范围：已纳管的受影响客户端都签收据；归档文档就是这两个客户端的活动文档。
        scope = deploy.transaction_scope(arguments[arguments.index("--managed-roots") + 1:], ["codex", "claude"], self.control)
        self.assertEqual((scope["roots"], scope["clients"]), (list(arguments[-2:]), ["codex", "claude"]))
        self.assertEqual(list(deploy.transaction_documents(["codex", "claude"])[0]), documents)
        with self.assertRaisesRegex(deploy.DeploymentError, "已纳管"):
            deploy.transaction_scope(None, None, self.control)

    def test_deploy_step_copies_the_listed_documents_and_passes_the_scope(self) -> None:
        script = (DRIVER / "fix-and-continue.sh").read_text(encoding="utf-8")
        self.assertIn('"$ST" "$LOG" "$PIDF" "$D" ${scope_args[@]+"${scope_args[@]}"}', script)
        self.assertIn('--control-root "$3/control" "${@:4}"', script)
        start = script.index("    local -a repository_docs scope_args\n")
        end = script.index('|| fail_step "复制仓库文档到 staging 失败"', start)
        snippet = script[start:end].replace("    local -a", "declare -a")
        staging = self.root / "staging"
        names = ["OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md", "CODEX_CLI_CLIENT_EMULATION_GUIDE.md", "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
                 "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md"]
        for name in names:
            (staging / "docs").mkdir(parents=True, exist_ok=True)
            (staging / "docs" / name).write_text(name, encoding="utf-8")
        completed = subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + snippet + "\necho copied"], capture_output=True, text=True,
                                   env={**os.environ, "ST": str(staging), "REPOSITORY_DOCS": " ".join(names), "DEPLOY_SCOPE": ""})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        archived = sorted(path.name for path in (staging / "docs" / "repository-docs").iterdir())
        self.assertEqual(archived, sorted(names))
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o644 for path in (staging / "docs" / "repository-docs").iterdir()))


class PendingToolsClaudeTests(unittest.TestCase):
    def test_entry_dryrun_and_vc5_admission_refuse_claude_until_wired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            env = root / "claude.sh"
            env.write_text(claude_env_text(fixture), encoding="utf-8")
            import argparse
            args = argparse.Namespace(runroot=fixture.runroot, data_root=fixture.data_root, vc_env=env, bundle=root / "x.bundle",
                                      branch="claude/x", commit="a" * 40, opening=False, reexecute=False, to=None, entry=root / "entry.sh")
            with self.assertRaisesRegex(dr.DryRunError, "入口空跑尚未接入：B5a-10"):
                dr.start(args)
            admission = driver_tests.load_script("vc5_admission")
            parser = load_parse_env()
            config = parser.parse(env.read_text(encoding="utf-8"))
            with mock.patch.object(sys, "path", [str(DRIVER), *sys.path]):
                with self.assertRaisesRegex(admission.AdmissionError, "client_admission_pending"):
                    admission._require_client_admission(config)
                admission._require_client_admission(parser.parse(fixture.env_file.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
