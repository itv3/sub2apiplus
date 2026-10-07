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
import hashlib
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
# B2-11 第二部分：Claude 预检 plan 的参数键（参数模板里有写法；入口便宜检查与预检 plan 一步按客户端表报缺）。
PLAN_KEYS = ("ACTIVE_RELEASE_CATALOG", "STATIC_BASELINE_ANCHORS", "STATIC_BASELINE_REACHABILITY", "PRODUCTION_INVENTORY_STORE",
             "PRODUCTION_INVENTORY_CAMPAIGN", "PREDECESSOR_STORE", "PREDECESSOR_STORE_CAMPAIGN", "ENTRYPOINT", "PRIVACY_MODE",
             "OAUTH_ORG_ID", "MODEL_CATALOG_SHA256")


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


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


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
        # 待接入变量取表里第一个条目作样本（条目随工作项落实删去，样本跟着表走）：本客户端已接入时为空串。
        sample = CT.pending_names()[0]
        variable = CT._shell_name("pending_" + sample)
        script = ('set -Eeuo pipefail\neval "$(python3 -B "$1" shell)"\n'
                  f'printf "%s|%s|%s|" "$CT_CLIENT" "$CT_ORCHESTRATOR_MODULE" "${variable}"\n'
                  'printf "[%s]" ${CT_CLIENT_ARGV[@]+"${CT_CLIENT_ARGV[@]}"}\necho\nenv | grep -c "^CT_" || true\n')
        environment = {key: value for key, value in os.environ.items() if key != "CLIENT"}
        for client, expected_argv in (("codex", "[]"), ("claude", "[--client][claude]")):
            completed = subprocess.run(["bash", "-c", script, "client-table", str(DRIVER / "client_table.py")],
                                       env={**environment, "CLIENT": client}, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            first, exported = completed.stdout.splitlines()
            module = CT.spec(client)["orchestrator_module"]
            pending = CT.pending(client, sample) or ""
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


def codex_names_all(steps: dict) -> set[str]:
    """Codex 全部步骤的输入名（核对 Codex 的声明里没有混进 Claude 专属的参数）。"""

    return {item.name for step in steps.values() for item in step.inputs}


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
        self.assertEqual(set(derived) - {"CLIENT", "CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID", "CAMPAIGN_PURPOSE"},
                         set(codex_derived), "与 Codex 派生同一组键（另加本轮写的客户端键与 Claude 的 Campaign 用途）")
        self.assertEqual(derived["CAMPAIGN_PURPOSE"], "production_replacement", "Claude 缺省用途（B5b-01）")
        self.assertNotIn("CAMPAIGN_PURPOSE", codex_derived, "Codex 不派生用途，与改造前相同")
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

    def test_campaign_purpose_is_claude_only_and_closed(self) -> None:
        """B5b-01：Campaign 用途只对登记了用途参数键的客户端（Claude）生效，可选值闭合；Codex 参数文件写了即拒绝。"""

        values = self.parser.parse(self.text + "CAMPAIGN_PURPOSE=validation_only\n")
        self.assertEqual(self.parser.derive(values)["CAMPAIGN_PURPOSE"], "validation_only")
        with self.assertRaisesRegex(self.parser.EnvFileError, "CAMPAIGN_PURPOSE 只能是"):
            self.parser.parse(self.text + "CAMPAIGN_PURPOSE=fixture_only\n")
        codex = self.fixture.env_file.read_text(encoding="utf-8")
        with self.assertRaisesRegex(self.parser.EnvFileError, "不得写 CAMPAIGN_PURPOSE"):
            self.parser.parse(codex + "CAMPAIGN_PURPOSE=production_replacement\n")

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
                          "CLIENT_ACCOUNT_ID", "BASELINE_SOURCE", "TARGET_SOURCE", "TARGET_PACKAGE", "ENTRY_BRANCH", "UPSTREAM_IP",
                          "CAMPAIGN_PURPOSE", *PLAN_KEYS}, set(overrides))
        lines = []
        for line in template.splitlines():
            key = line.split("=", 1)[0]
            if key in {"KILO_BIN", "KILO_VERSION", "KILO_SHA256"}:
                continue
            lines.append(f"{key}={overrides[key]}" if key in overrides and not line.startswith("#") else line)
        lines += [f"{key}={value}" for key, value in overrides.items()
                  if key in {"BASELINE_SOURCE", "TARGET_SOURCE", "TARGET_PACKAGE", "UPSTREAM_IP", *PLAN_KEYS}]
        text = "\n".join(lines) + "\n"
        text = text.replace("REPLACE_API_ANTHROPIC_COM_IP", "160.79.104.10")
        for placeholder in ("REPLACE_APPROVED_PROFILE_SHA256", "REPLACE_OFFICIAL_CLAUDE_SHA256", "REPLACE_OFFICIAL_PACKAGE_SHA256",
                            "REPLACE_MODEL_CATALOG_SHA256"):
            text = text.replace(placeholder, "a" * 64)
        text = text.replace("REPLACE_CLAUDE_ACCOUNT_ID", "17").replace("REPLACE_API_KEY_ID", "92").replace("/root/", f"{self.root}/")
        (self.root / "docker/capture-cli/data").mkdir(parents=True, exist_ok=True)
        values = self.parser.parse(text)
        derived = self.parser.derive(values)
        self.assertEqual(values["CLIENT"], "claude")
        self.assertTrue(values["NEW"].startswith("claude-code-9.1.0-formal-"))
        self.assertTrue(derived["TARGET_PACKAGE"].endswith("/official/claude-code-9.1.0/freeze.json"))
        self.assertEqual(values["UPSTREAM_IP"], "160.79.104.10")
        with self.assertRaisesRegex(self.parser.EnvFileError, "UPSTREAM_IP 必须是 IP 地址"):
            self.parser.parse(text.replace("UPSTREAM_IP=160.79.104.10", "UPSTREAM_IP=api.anthropic.com"))
        # B2-11 第二部分的预检 plan 键：模板里的写法能解析，格式与成对规则拒绝写错的。
        self.assertEqual(values["MODEL_CATALOG_SHA256"], "a" * 64)
        self.assertTrue(values["PRODUCTION_INVENTORY_STORE"].endswith("/control-store"))
        self.assertEqual(values["PREDECESSOR_STORE_CAMPAIGN"], "REPLACE_PREDECESSOR_CAMPAIGN")
        # 只改真正的赋值行（行首），模板注释里的同名写法不动。
        for label, (before, after, message) in {
            "相对路径": ("\nACTIVE_RELEASE_CATALOG=", "\nACTIVE_RELEASE_CATALOG=relative/", "绝对路径"),
            "非法标识": ("\nENTRYPOINT=REPLACE_STORE_ENTRYPOINT", "\nENTRYPOINT=-bad", "安全标识"),
            "摘要": ("\nMODEL_CATALOG_SHA256=" + "a" * 64, "\nMODEL_CATALOG_SHA256=xyz", "SHA-256"),
            "缺成对": ("\nPREDECESSOR_STORE_CAMPAIGN=REPLACE_PREDECESSOR_CAMPAIGN\n", "\n", "必须同时给出"),
        }.items():
            with self.subTest(label):
                self.assertEqual(text.count(before), 1, before)
                with self.assertRaisesRegex(self.parser.EnvFileError, message):
                    self.parser.parse(text.replace(before, after))

    def test_plan_audit_template_uses_exported_parameters_and_real_plan_flags(self) -> None:
        """B5a-09：Claude 的 plan-audit 参数模板只引用参数文件导出的键（UPSTREAM_IP 可缺省、由必填项报缺），展开后是 Claude
        plan --audit-only 认得的参数。"""

        from tools.official_client_capture import claude_upgrade

        values = self.parser.parse(self.text)
        exported = {**values, **self.parser.derive(values)}
        template = CT.spec("claude")["plan_audit_argv"]
        names = [item[1:] for item in template if item.startswith("$")]
        self.assertTrue(names)
        # 夹具参数文件没写的键只能是参数文件可缺省、由便宜检查的必填项报缺的那几个（UPSTREAM_IP、采集镜像）。
        absent = set(names) - set(exported)
        self.assertIn("UPSTREAM_IP", absent)
        self.assertLessEqual(absent, set(self.parser.OPTIONAL_KEYS) & set(CT.spec("claude")["preflight_required"]))
        self.assertNotIn("UPSTREAM_IP", CT.spec("codex")["preflight_required"])
        filled = {"UPSTREAM_IP": "160.79.104.10", "CAPTURE_RUNTIME_IMAGE": "oauth-egress-capture-capture-cli@sha256:" + "b" * 64}
        argv = [exported.get(item[1:], filled.get(item[1:])) if item.startswith("$") else item for item in template]
        arguments = claude_upgrade._build_parser().parse_args(
            ["plan", "--audit-only", "--campaign-dir", "/x/campaign", "--campaign-id", "c-plan-audit", *argv])
        self.assertTrue(arguments.audit_only)
        self.assertEqual((str(arguments.target_package), arguments.target_package_sha256),
                         (exported["TARGET_PACKAGE"], exported["OFFICIAL_ASSET_SHA256"]))
        self.assertEqual((arguments.target_sha256, arguments.claude_bin, arguments.upstream_ip),
                         (exported["CLIENT_BIN_SHA256"], exported["CLIENT_BIN"], "160.79.104.10"))
        self.assertEqual(CT.spec("codex")["plan_audit_argv"], (), "Codex 沿用脚本里的原参数形状")

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
        self.assertTrue({"param:CLIENT_BIN", "param:CLIENT_BIN_SHA256", "param:UPSTREAM_IP", "param:ENTRY_COMMIT"} <= names["preflight-plan"])
        # B2-11 第二部分：预检 plan 登记 Claude plan 实际读的参数与另读的文件、Store 目录；Claude plan 不读的 Codex 那几项不登记。
        self.assertTrue({"param:ACTIVE_RELEASE_CATALOG", "param:PRODUCTION_INVENTORY_STORE", "param:PREDECESSOR_STORE",
                         "file:{ACTIVE_RELEASE_CATALOG}", "file:{STATIC_BASELINE_ANCHORS}", "file:{STATIC_BASELINE_REACHABILITY}",
                         "tree:{PRODUCTION_INVENTORY_STORE}#pycache", "tree:{PREDECESSOR_STORE}#pycache"} <= names["preflight-plan"])
        self.assertFalse({"param:LITE_MODEL", "param:COMPOSE_DIR"} & names["preflight-plan"])
        # B5b-01：预检 plan 另带 Campaign 用途与候选阶段的账号、ingress API Key（原 B4-01 d 段），计时账本一步登记用途；
        # Codex 的两步声明不变（没有用途参数，账号键是 CODEX_ACCOUNT_ID 且 Codex 预检本来就登记）。
        self.assertTrue({"param:CAMPAIGN_PURPOSE", "param:CLIENT_ACCOUNT_ID", "param:API_KEY_ID"} <= names["preflight-plan"])
        self.assertIn("param:CAMPAIGN_PURPOSE", names["ledger"])
        self.assertNotIn("param:CAMPAIGN_PURPOSE", {item.name for item in codex["ledger"].inputs} | codex_names_all(codex))
        codex_names = {item.name for item in codex["preflight-plan"].inputs}
        self.assertFalse({name for name in codex_names if "ACTIVE_RELEASE_CATALOG" in name or "PRODUCTION_INVENTORY" in name},
                         "Codex 的预检 plan 输入不变")
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
                               "CLIENT": "claude", "CAMPAIGN_PURPOSE": "production_replacement"})
        (self.fx.data / "control" / "claude-managed-deploy-20261005t000000z.json").write_text(
            json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "f" * 64}}), encoding="utf-8")
        self.commands = Commands(self.fx)
        self.runs = 0

    def orchestrator(self, runner=None) -> eo.Orchestrator:
        self.runs += 1
        return eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.root / "runs" / str(self.runs),
                               steps_dir=self.fx.steps_dir, runner=runner or self.commands, es_runner=self.fx.runner)

    def test_pre_ledger_steps_run_the_claude_tools(self) -> None:
        """建账本之前的各步对 Claude 全部接入（B5a-09 续落实 atomic-double 之后，Claude 已没有待接入的入口步骤）：零请求 smoke
        执行 Claude 的模块，atomic-double 在 capture-cli 里调共享收据模块并追加 --client claude，证据根取 Claude 的前缀。"""

        self.assertFalse([name for name in CT.TABLE["claude"]["pending"] if name.startswith("step:")], "Claude 没有待接入的入口步骤")
        plan = self.orchestrator().orchestrate(plan_only=True)
        self.assertFalse([step["step_id"] for step in plan["steps"] if any("尚未接入" in reason for reason in step["reasons"])])
        smoke_module, smoke_calls = CT.spec("claude")["zero_request_smoke_module"], []

        def runner(argv, cwd, env, log):
            # B5a-09 接入的零请求 smoke：执行客户端表登记的 Claude 模块（替身照收据合同写一份通过的收据）。
            if smoke_module in argv:
                smoke_calls.append(list(argv))
                output = Path(_option(argv, "--output"))
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"schema_version": "zero-request-smoke/v1", "client": "claude", "status": "passed"}),
                                  encoding="utf-8")
                return 0
            return self.commands(argv, cwd, env, log)

        result = self.orchestrator(runner).orchestrate(to_step="atomic-double")
        actions = {step["step_id"]: (step["action"], step["status"], "；".join(step["reasons"])) for step in result["steps"]}
        self.assertEqual(actions["zero-request-smoke"][:2], ("执行", "passed"))
        [smoke] = smoke_calls
        self.assertEqual(smoke[smoke.index("-m") + 1:smoke.index("-m") + 2], [smoke_module])
        self.assertTrue(_option(smoke, "--output").endswith(f"/audit/zero-request-smoke-{self.fx.params['STAMP']}.json"))
        self.assertIn(f"/staging/zero-request-smoke-{self.fx.params['STAMP']}", _option(smoke, "--staging-root"))
        self.assertEqual(actions["atomic-double"][:2], ("执行", "passed"), actions["atomic-double"])
        [atomic] = [call for call in self.commands.calls if call[:2] == ["docker", "exec"]]
        self.assertEqual(atomic[atomic.index("-m") + 1:atomic.index("-m") + 3],
                         [CT.spec("claude")["atomic_rehearsal_module"], "atomic-double-collect"])
        self.assertEqual(atomic[-2:], ["--client", "claude"], "共享收据模块按 --client claude 签 Claude 合同")
        self.assertTrue(_option(atomic, "--evidence-root").startswith(f"/capture/staging/{CT.spec('claude')['atomic_root_prefix']}"))
        joined = [" ".join(call) for call in self.commands.calls]
        self.assertFalse([call for call in joined if "codex_upgrade_zero_request_smoke" in call], "不拿 Codex 的实现顶替")
        policy = [call for call in self.commands.calls if "codex_upgrade_policy_certification" in " ".join(call)]
        self.assertEqual([call[-2:] for call in policy], [["--client", "claude"]] * 2, "策略兼容与激活认证带 --client claude")
        self.assertEqual(result["exit_code"], 0, result.get("stopped_because"))

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
        self.assertEqual(_option(ledger, "--campaign-purpose"), "production_replacement")
        # B5b-01：Claude 的计时账本按参数 CAMPAIGN_PURPOSE 建（文档路发现的 validation_only 走不了 entry.sh）。
        self.fx.params["CAMPAIGN_PURPOSE"] = "validation_only"
        self.orchestrator(runner).step_ledger()
        self.assertEqual(_option(calls[-1], "--campaign-purpose"), "validation_only")
        calls[:] = [ledger, environment]
        self.assertTrue(_option(ledger, "--project-ledger-dir").endswith("/evidence/campaigns/upgrade-project-ledger-claude"))
        self.assertEqual(environment[3], "tools.official_client_capture.claude_upgrade_arm64_environment_receipt")
        self.assertEqual(_option(environment, "--claude-version"), self.fx.params["TARGET_VERSION"])
        self.assertNotIn("--rust-tls-codex-version", environment)


class EntryOrchestratorClaudeBackHalfTests(unittest.TestCase):
    """B2-11 第二部分：入口后半段四步对 Claude 接入——预检 plan 按客户端表模板展开（真实 Claude 解析器解析）、发布认证与 P0
    收据按客户端表执行、VC-0 收口跑 Claude 收口模块并按三态（通过、需要处理即退出 3、失败）收尾。前序步骤的记录按 Claude 的
    步骤表写（各步骤方法只从记录取产物）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.fx = Fixture(self.root)
        shutil.rmtree(self.fx.data / "control" / f"{self.fx.params['UP']}-timing-ledger")
        self.fx.images.update({"capture-cli-synthetic": "sha256:" + "9" * 64})
        inputs = self.root / "claude-inputs"
        store = inputs / "active-store"
        store.mkdir(parents=True)
        self.claude = {
            "CLIENT": "claude", "CLIENT_BIN": "/opt/claude-x/claude", "CLIENT_BIN_SHA256": "c" * 64, "UPSTREAM_IP": "160.79.104.10",
            "ACTIVE_RELEASE_CATALOG": str(_write_text(inputs / "active-release-catalog.json", "{}")),
            "STATIC_BASELINE_ANCHORS": str(_write_text(inputs / "anchors.json", "{}")),
            "STATIC_BASELINE_REACHABILITY": str(_write_text(inputs / "reachability.json", "{}")),
            "PRODUCTION_INVENTORY_STORE": str(store), "PRODUCTION_INVENTORY_CAMPAIGN": "claude-code-active",
            "ENTRYPOINT": "sdk-cli", "PRIVACY_MODE": "essential-traffic", "OAUTH_ORG_ID": "claude-org", "MODEL_CATALOG_SHA256": "e" * 64,
            "RUNROOT": str(self.root / "run"), "CAMPAIGN_PREFIX": "claude-code-2_1_245",
            # B5b-01：parse_env 给 Claude 派生的 Campaign 用途；候选阶段的验收账号（Claude 用通用键，API_KEY_ID 两客户端同名）。
            "CAMPAIGN_PURPOSE": "production_replacement", "CLIENT_ACCOUNT_ID": "7",
        }
        self.fx.params.update(self.claude)
        (self.fx.data / "control" / "claude-managed-deploy-20261005t000000z.json").write_text(
            json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "f" * 64}}), encoding="utf-8")
        self.ledger_dir = self.fx.data / "control" / f"{self.fx.params['UP']}-timing-ledger"
        self.commands = Commands(self.fx)
        self.calls: list[list[str]] = []
        self.closeout_results: list[tuple[int, dict]] = []

    def runner(self, argv, cwd, env, log):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:4] == [sys.executable, "-B", "-m", CT.spec("claude")["orchestrator_module"]] and argv[4] == "plan":
            campaign = Path(_option(argv, "--campaign-dir"))
            campaign.mkdir()
            (campaign / "campaign.json").write_text(json.dumps({"campaign_id": _option(argv, "--campaign-id")}), encoding="utf-8")
            return 0
        if argv[:4] == [sys.executable, "-B", "-m", CT.spec("claude")["closeout_module"]]:
            rc, payload = self.closeout_results.pop(0) if self.closeout_results else (0, {"status": "passed"})
            if rc == 0:
                _write_text(Path(_option(argv, "--audit-dir")) / "receipt.json", json.dumps(payload))
            with log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return rc
        return self.commands(argv, cwd, env, log)

    def orchestrator(self) -> eo.Orchestrator:
        return eo.Orchestrator(self.fx.params, driver_dir=self.fx.driver, run_dir=self.root / "runs", steps_dir=self.fx.steps_dir,
                               runner=self.runner, es_runner=self.fx.runner)

    def record(self, step_id: str, products: dict[str, str]) -> None:
        ctx = es.Context(params=dict(self.fx.params), driver_dir=self.fx.driver, steps_dir=self.fx.steps_dir, runner=self.fx.runner)
        step = ctx.step(step_id)
        es.begin(step, ctx)
        es.finish(step, ctx, status="passed", products=products)

    def ledger_records(self) -> None:
        ledger = _write_text(self.ledger_dir / "ledger.json", json.dumps({
            "schema_version": "upgrade-timing-ledger-plan/v2", "client": "claude", "upgrade_id": self.fx.params["UP"],
            "campaign_purpose": "production_replacement", "baseline_version": BASELINE, "target_version": TARGET}))
        checkpoint = _write_text(self.ledger_dir / "receipts" / "vc0-input-r1-preflight.json", "{}")
        environment = _write_text(self.root / "environment" / "p0" / "receipt.json", "{}")
        self.record("ledger", {"ledger": str(ledger)})
        self.record("ledger-checkpoint", {"checkpoint": str(checkpoint)})
        self.record("environment-p0", {"receipt": str(environment)})

    def plan_call(self) -> list[str]:
        [call] = [call for call in self.calls if call[3:5] == [CT.spec("claude")["orchestrator_module"], "plan"]]
        return call

    def test_preflight_plan_follows_the_claude_template_and_real_parser(self) -> None:
        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture import claude_upgrade_campaign

        self.assertIsNone(CT.pending("claude", "step:preflight-plan"))
        self.assertIsNone(CT.pending("claude", "step:vc0-closeout"))
        self.ledger_records()
        passed, products = self.orchestrator().step_preflight_plan()
        self.assertTrue(passed)
        argv = self.plan_call()
        self.assertFalse({"--capture-codex-bin", "--codex-account-id", "--baseline-source"} & set(argv), "不拿 Codex 的参数形状顶替")
        parsed = claude_upgrade._build_parser().parse_args(argv[4:])
        campaign = Path(products["campaign"]).parent
        self.assertEqual((parsed.campaign_dir, parsed.campaign_id, parsed.campaign_mode), (campaign, campaign.name, "preflight_only"))
        self.assertTrue(campaign.name.startswith("claude-code-2_1_245-preflight-vc5-r1-fffffffff-"))
        self.assertTrue(claude_upgrade_campaign.creating_campaign(parsed), "预检的四个控制收据参数齐全")
        self.assertEqual((parsed.timing_ledger_dir, str(parsed.timing_receipt)), (self.ledger_dir, "receipts/vc0-input-r1-preflight.json"))
        self.assertEqual((parsed.arm64_environment_root, str(parsed.arm64_environment_receipt)),
                         (self.root / "environment" / "p0", "receipt.json"))
        expected = {
            "target_sha256": self.claude["CLIENT_BIN_SHA256"], "claude_bin": self.claude["CLIENT_BIN"], "upstream_ip": "160.79.104.10",
            "active_release_catalog": Path(self.claude["ACTIVE_RELEASE_CATALOG"]), "active_profile": Path(self.fx.params["ACTIVE_PROFILE"]),
            "static_baseline_anchors": Path(self.claude["STATIC_BASELINE_ANCHORS"]),
            "static_baseline_reachability": Path(self.claude["STATIC_BASELINE_REACHABILITY"]),
            "production_inventory_store": Path(self.claude["PRODUCTION_INVENTORY_STORE"]),
            "production_inventory_campaign": "claude-code-active", "entrypoint": "sdk-cli", "privacy_mode": "essential-traffic",
            "oauth_org_id": "claude-org", "model_catalog_sha256": "e" * 64, "source_commit": self.fx.params["ENTRY_COMMIT"],
            "predecessor_store": None, "predecessor_campaign": None,
            # B5b-01（原 B4-01 d 段）：Campaign 用途与候选阶段的账号、ingress API Key 进预检，VC-0 收口按它恢复 Formal plan。
            "campaign_purpose": "production_replacement", "claude_account_id": "7", "api_key_id": self.fx.params["API_KEY_ID"],
        }
        self.assertEqual({key: getattr(parsed, key) for key in expected}, expected)
        claude_upgrade_campaign._require_create_arguments(parsed)
        self.fx.params["CAMPAIGN_PURPOSE"] = "validation_only"
        self.calls.clear()
        self.assertTrue(self.orchestrator().step_preflight_plan()[0])
        self.assertEqual(claude_upgrade._build_parser().parse_args(self.plan_call()[4:]).campaign_purpose, "validation_only")

    def test_preflight_plan_reuse_and_missing_parameters(self) -> None:
        from tools.official_client_capture import claude_upgrade

        self.ledger_records()
        self.fx.params["EVIDENCE_DECISION"] = "reuse"
        with self.assertRaisesRegex(eo.OrchestratorError, "参数里没有 PREDECESSOR_STORE"):
            self.orchestrator().step_preflight_plan()
        predecessor = self.root / "predecessor-store"
        predecessor.mkdir()
        self.fx.params.update({"PREDECESSOR_STORE": str(predecessor), "PREDECESSOR_STORE_CAMPAIGN": "claude-code-2_1_245-old"})
        self.assertTrue(self.orchestrator().step_preflight_plan()[0])
        parsed = claude_upgrade._build_parser().parse_args(self.plan_call()[4:])
        self.assertEqual((parsed.predecessor_store, parsed.predecessor_campaign), (predecessor, "claude-code-2_1_245-old"))
        del self.fx.params["ENTRYPOINT"]
        with self.assertRaisesRegex(eo.OrchestratorError, "参数里没有 ENTRYPOINT"):
            self.orchestrator().step_preflight_plan()

    def test_release_certification_and_p0_receipt_follow_the_table(self) -> None:
        products = self.fx.products
        self.ledger_records()
        self.record("policy-activation", {"activation": self.fx.params["POLICY_ACTIVATION"]})
        self.record("pre-a3", {"certification": self.fx.params["PRE_A3_CERTIFICATION"]})
        self.record("job-rehearsal", products["job-rehearsal"])
        self.record("atomic-double", products["atomic-double"])
        orchestrator = self.orchestrator()
        passed, release = orchestrator.step_release_certification()
        self.assertTrue(passed)
        certify = [call for call in self.calls if CT.spec("claude")["release_certification_module"] in call]
        self.assertEqual([call[4] for call in certify], ["issue", "verify"])
        self.assertEqual([call[-2:] for call in certify], [["--client", "claude"]] * 2)
        reuse = [call for call in self.calls if "record-reuse" in call]
        self.assertEqual(reuse[0][-2:], ["--client", "claude"])
        self.assertTrue(_option(certify[0], "--deployment-receipt").endswith("claude-managed-deploy-20261005t000000z.json"))
        # P0 收据：两客户端同一合同，subject 取 Claude 的计时账本计划。
        for gate_id in ("test-capture-tools", "check-egress-spec"):
            _write_text(Path(products["entry-gates"][f"p0-{gate_id}"]), json.dumps({
                "gate_id": gate_id, "status": "passed", "exit_code": 0, "passed": 3, "failed": 0, "approved_skip": 0,
                "unexpected_skip": 0}))
        self.record("entry-gates", products["entry-gates"])
        self.record("release-certification", release)
        passed, receipt = orchestrator.step_p0_receipt()
        self.assertTrue(passed, orchestrator.outcomes)
        facts = self.commands.p0_facts
        self.assertEqual((facts["schema_version"], facts["subject"]["upgrade_id"], facts["subject"]["target_version"]),
                         (CT.spec("claude")["p0_facts_schema"], self.fx.params["UP"], TARGET))
        self.assertTrue(receipt["receipt"].endswith("/p0-receipt.json"))

    def closeout_ready(self) -> eo.Orchestrator:
        products = self.fx.products
        campaign = self.root / "preflight" / "campaign.json"
        _write_text(campaign, "{}")
        report = _write_text(self.root / "probe" / "report.json", json.dumps({"status": "passed"}))
        self.record("preflight-plan", {"campaign": str(campaign)})
        self.record("client-launch-probe", {"report": str(report)})
        self.record("p0-receipt", products["p0-receipt"])
        self.record("release-certification", {"certification": self.fx.params["RELEASE_CERTIFICATION"]})
        self.record("entry-gates", products["entry-gates"])
        orchestrator = self.orchestrator()
        # 只执行收口一步：其余步骤的结果按「沿用」占位，汇总（_after_closeout）照常列出全部步骤。
        orchestrator.outcomes = {step_id: eo.Outcome(step_id, "reuse", action="沿用") for step_id in eo.ORDER}
        orchestrator.outcomes["vc0-closeout"] = eo.Outcome("vc0-closeout", "run")
        return orchestrator

    def test_vc0_closeout_runs_the_claude_module_in_three_states(self) -> None:
        orchestrator = self.closeout_ready()
        orchestrator.execute("vc0-closeout")
        outcome = orchestrator.outcomes["vc0-closeout"]
        self.assertEqual((outcome.action, outcome.status), ("执行", "passed"), outcome.reasons)
        [closeout] = [call for call in self.calls if CT.spec("claude")["closeout_module"] in call]
        self.assertEqual(_option(closeout, "--formal-campaign-id"), self.fx.params["NEW"])
        self.assertEqual(_option(closeout, "--preflight-campaign-dir"), str(self.root / "preflight"))
        probe = [call for call in self.calls if "client_launch_probe.py" in " ".join(call)]
        self.assertEqual(probe[0][-2:], ["--client", "claude"], "收口前复核启动探测报告按 Claude")
        self.assertEqual(orchestrator._after_closeout({})["exit_code"], 0)
        # 需要批准：退出 3，标「阻塞」并给出下一条命令（不是失败）。
        self.closeout_results = [(3, {"status": "blocked", "kind": "approval", "message": "首批恢复预览等待批准",
                                      "next_command": "entry.sh --approve-sha256 <摘要> --approved-by <批准人>"})]
        orchestrator = self.closeout_ready()
        orchestrator.execute("vc0-closeout")
        result = orchestrator._after_closeout({})
        self.assertEqual(result["exit_code"], 3)
        outcome = orchestrator.outcomes["vc0-closeout"]
        self.assertEqual(outcome.action, "阻塞")
        self.assertIn("首批恢复预览等待批准", outcome.reasons)
        self.assertTrue(any(reason.startswith("下一步：entry.sh --approve-sha256") for reason in outcome.reasons))
        # 收口失败：退出 1。
        self.closeout_results = [(1, {"status": "failed"})]
        orchestrator = self.closeout_ready()
        orchestrator.execute("vc0-closeout")
        self.assertEqual(orchestrator._after_closeout({})["exit_code"], 1)
        self.assertTrue(any("收口失败" in reason for reason in orchestrator.outcomes["vc0-closeout"].reasons))


class ClaudeVc1DriverTests(unittest.TestCase):
    """B2-11 第二部分：VC-1 首批之后的封存批次由 vc1-claude.sh／claude_vc1.py 派发（客户端表 vc1_dispatch_script，Codex 没有）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.campaign = self.root / "campaign"
        self.inputs = self.root / "inputs"
        for path in (self.campaign, self.inputs):
            path.mkdir()
        self.module = driver_tests.load_script("claude_vc1")

    def environment(self, client: str):
        return mock.patch.dict(os.environ, {"CLIENT": client, "NEWDIR": str(self.campaign), "W": str(self.inputs)})

    def test_table_and_script_bind_the_claude_dispatch(self) -> None:
        self.assertEqual((CT.spec("codex")["vc1_dispatch_script"], CT.spec("claude")["vc1_dispatch_script"]), (None, "vc1-claude.sh"))
        script = (DRIVER / "vc1-claude.sh").read_text(encoding="utf-8")
        self.assertIn('"$CT_VC1_DISPATCH_SCRIPT" != vc1-claude.sh', script)
        self.assertIn('bash "$DRV/vc-batch.sh" "$NEW" "$IN" VC-1 "$SEQ" VC-0 "$PLAN"', script, "阶段 VC-1、前序 VC-0")
        self.assertTrue(os.access(DRIVER / "vc1-claude.sh", os.X_OK))

    def test_plan_writes_the_checked_batch_plan_and_rejects_other_clients(self) -> None:
        from tools.official_client_capture import claude_upgrade_batch
        from tools.official_client_capture import claude_upgrade_static

        actions = [
            claude_upgrade_batch.managed_action("vc1-02-seal-preview", "VC-1:vc1-02-seal-preview",
                                                ["capture-official", "seal", "--campaign-dir", str(self.campaign), "--attempt-id", "a1"],
                                                item_ids=["vc1-02-seal-preview"]),
            claude_upgrade_static.static_action(self.campaign),
        ]
        output = self.inputs / "action-plan-vc1-seal-preview.json"
        with self.environment("claude"), mock.patch.object(claude_upgrade_static, "vc1_seal_actions", return_value=actions) as seal, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.main(["plan", "--output", str(output)]), 0)
        self.assertEqual(seal.call_args.kwargs, {"approve_seal_sha256": None, "discovery_dispositions": None})
        plan = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([item["action_id"] for item in plan["actions"]], ["vc1-01-analyze-target", "vc1-02-seal-preview"],
                         "按 action_id 排序：静态分析先于 seal 预览")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.environment("claude"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.module.main(["plan", "--output", str(self.root / "elsewhere.json")]), 3)
        self.assertIn("派发拒绝", stderr.getvalue())
        with self.environment("codex"), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(self.module.main(["plan", "--output", str(output)]), 3)
        self.assertIn("不是 vc1-claude.sh", stderr.getvalue())


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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            binary = root / "claude"
            binary.write_text(f"#!/bin/bash\necho '{TARGET} (Claude Code)'\n", encoding="utf-8")
            binary.chmod(0o700)
            # docker 替身：容器内命令在本机执行；SYNTHETIC_SHA 给出时断网合成容器报这个摘要（模拟两个容器装的不是同一份）。
            (root / "docker").write_text('#!/bin/bash\nif [ "$2" = capture-cli-synthetic ] && [ -n "${SYNTHETIC_SHA:-}" ]; then\n'
                                         '  echo "$SYNTHETIC_SHA  $4"; exit 0\nfi\nshift 2\nexec "$@"\n', encoding="utf-8")
            (root / "docker").chmod(0o700)
            digest = subprocess.run(["shasum", "-a", "256", str(binary)], capture_output=True, text=True).stdout.split()[0]
            (root / "sha256sum").write_text('#!/bin/bash\nexec shasum -a 256 "$@"\n', encoding="utf-8")
            (root / "sha256sum").chmod(0o700)
            env = {"PATH": f"{root}:{os.environ['PATH']}", "CLIENT_BIN": str(binary), "CLIENT_BIN_SHA256": digest, "TARGET_VERSION": TARGET}
            target = self.shell(self.function("entry-preflight.sh", "target_client") + "target_client\n", "claude", **env)
            self.assertEqual(target.returncode, 0, target.stdout + target.stderr)
            self.assertIn(f"登记 {digest}", target.stdout)
            self.assertIn(f"capture-cli-synthetic {digest}", target.stdout, "Claude 另核断网合成容器（B5a-09）")
            for label, overrides in (("登记摘要不符", {"CLIENT_BIN_SHA256": "0" * 64}), ("断网容器不同", {"SYNTHETIC_SHA": "1" * 64})):
                with self.subTest(label):
                    wrong = self.shell(self.function("entry-preflight.sh", "target_client") + "target_client\n", "claude",
                                       **{**env, **overrides})
                    self.assertNotEqual(wrong.returncode, 0)
            codex = self.shell(self.function("entry-preflight.sh", "target_client") + "target_client\n", "codex",
                               **{**env, "CODEX_BIN": str(binary), "CODEX_BIN_SHA256": digest, "SYNTHETIC_SHA": "1" * 64})
            self.assertEqual(codex.returncode, 0, codex.stdout + codex.stderr)
            self.assertNotIn("capture-cli-synthetic", codex.stdout, "Codex 只核宿主机与 capture-cli")
            required = {key: "x" for key in CT.spec("claude")["preflight_required"]}
            required["P0_ROLLBACK_EVIDENCE"] = str(binary)
            check = self.shell(self.function("entry-preflight.sh", "required_parameters") + "required_parameters\n", "claude", **required)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_plan_audit_and_official_package_run_the_claude_tools(self) -> None:
        """B5a-09：抽出的 plan-audit 与 official-package 两项按 Claude 取值调用真实的 plan --audit-only 与冻结复核（合成冻结
        目录、仓库里的 2.1.285 清单），通过时零写入；缺 UPSTREAM_IP、摘要不符时不通过并如实报出。Codex 的 official-package
        仍只核源码树，不调用冻结复核。"""

        from tools.official_client_capture.tests import test_claude_upgrade_freeze as freeze_tests

        version = "2.1.285"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "keys").mkdir()
            binary = freeze_tests._elf(freeze_tests.GLIBC, payload=b"claude-entry-preflight")
            freeze = freeze_tests.freeze_fixture(root / "data" / "official" / f"claude-code-{version}", signer=freeze_tests._Signer(root / "keys"),
                                                 binary=binary, version=version)
            (root / "bin").mkdir()
            (root / "bin" / "sha256sum").write_text('#!/bin/bash\nexec shasum -a 256 "$@"\n', encoding="utf-8")
            (root / "bin" / "sha256sum").chmod(0o700)
            (root / "out").mkdir()
            digest = lambda path: subprocess.run(["shasum", "-a", "256", str(path)], capture_output=True, text=True).stdout.split()[0]  # noqa: E731
            env = {
                "PATH": f"{root / 'bin'}:{os.environ['PATH']}", "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                "D": str(root / "data"), "OUT": str(root / "out"), "CAMPAIGN_PREFIX": f"claude-code-{version.replace('.', '_')}",
                "BASELINE_VERSION": BASELINE, "TARGET_VERSION": version, "CLIENT_BIN": f"/opt/claude-{version}/claude",
                "CLIENT_BIN_SHA256": hashlib.sha256(binary).hexdigest(), "TARGET_PACKAGE": str(freeze),
                "OFFICIAL_ASSET_SHA256": digest(freeze), "CAPTURE_RUNTIME_IMAGE": "oauth-egress-capture-capture-cli@sha256:" + "b" * 64,
                "SCENARIOS_JSON": str(OCC / f"claude_upgrade_scenarios_{version.replace('.', '_')}.json"),
                "MAIN_MODEL": "claude-sonnet-5", "UPSTREAM_IP": "160.79.104.10",
                # B5b-01：模板另带 Campaign 用途（parse_env 给 Claude 派生缺省值）与候选阶段的账号、ingress API Key。
                "CAMPAIGN_PURPOSE": "production_replacement", "CLIENT_ACCOUNT_ID": "7", "API_KEY_ID": "8",
            }
            audit_body = (self.function("entry-preflight.sh", "plan_audit_from_table") + self.function("entry-preflight.sh", "plan_audit")
                          + "plan_audit\n")
            audit = self.shell(audit_body, "claude", **env)
            self.assertEqual(audit.returncode, 0, audit.stdout + audit.stderr)
            result = json.loads((root / "out" / "plan-audit.json").read_text(encoding="utf-8"))
            self.assertEqual((result["schema_version"], result["status"]), ("claude-upgrade-plan-audit/v1", "passed"))
            self.assertEqual([item["name"] for item in result["checks"]], ["arguments", "official-package", "manifests", "jobs", "tool-identity"])
            self.assertIn("passed   official-package", audit.stdout)
            self.assertIn("未校验（建账本之后由 plan 校验）", audit.stdout)
            self.assertFalse((root / "data" / "staging").exists(), "审计零写入：拟定的 Campaign 目录不会被创建")
            missing = self.shell(audit_body, "claude", **{key: value for key, value in env.items() if key != "UPSTREAM_IP"})
            self.assertEqual(missing.returncode, 1)
            self.assertIn("failed   arguments", missing.stdout)
            self.assertIn("blocked  jobs", missing.stdout)
            package_body = self.function("entry-preflight.sh", "official_package") + "official_package\n"
            package = self.shell(package_body, "claude", **env)
            self.assertEqual(package.returncode, 0, package.stdout + package.stderr)
            self.assertIn('"status": "passed"', package.stdout)
            for label, overrides, message in (
                ("目标二进制摘要不符", {"CLIENT_BIN_SHA256": "0" * 64}, "冻结目录复核未通过"),
                ("冻结收据摘要不符", {"OFFICIAL_ASSET_SHA256": "0" * 64}, f"官方包 {digest(freeze)}；登记 {'0' * 64}"),
            ):
                with self.subTest(label):
                    wrong = self.shell(package_body, "claude", **{**env, **overrides})
                    self.assertEqual(wrong.returncode, 1)
                    self.assertIn(message, wrong.stdout + wrong.stderr)
            sources = {"BASELINE_SOURCE": str(root / "base-src"), "TARGET_SOURCE": str(root / "target-src")}
            for value in sources.values():
                Path(value).mkdir()
            codex = self.shell(package_body, "codex", **{**env, **sources})
            self.assertEqual(codex.returncode, 0, codex.stdout + codex.stderr)
            self.assertIn("基线与目标源码树就位", codex.stdout)
            self.assertNotIn('"status"', codex.stdout, "Codex 不调用冻结复核")

    def test_require_deployed_all_checks_every_other_managed_client(self) -> None:
        """B5a-10：入口门禁 --require-deployed-all 在本轮客户端之外逐一核对已纳管客户端的部署（entry_steps.py deploy-check），
        核对过的收据进总摘要来源 deploy_receipts_others；任一不符没有门禁结论（退出 3）。"""

        gates = (DRIVER / "entry-gates.sh").read_text(encoding="utf-8")
        self.assertIn("    --require-deployed-all) REQUIRE_DEPLOYED=true; REQUIRE_DEPLOYED_ALL=true; shift ;;", gates)
        self.assertIn('--source "deploy_receipt=$DEPLOY" ${DEPLOY_OTHERS_SOURCE[@]+"${DEPLOY_OTHERS_SOURCE[@]}"}', gates)
        stub = ("from pathlib import Path\ndef current_identity(client='codex'):\n"
                "    return {'tool_files_sha256': (Path(__file__).resolve().parents[2] / f'identity-{client}.txt').read_text().strip()}\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree, data = root / "tree", root / "data"
            parts = {part for client in CT.CLIENTS for part in CT.spec(client)["deploy_consistency_parts"]}
            for side in (tree, data):
                for part in parts:
                    target = side / part if part.endswith((".md", ".py")) else side / part / "module.py"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f"# {part}\n", encoding="utf-8")
                for relative, content in {"tools/__init__.py": "", "tools/official_client_capture/__init__.py": "", "identity-claude.txt": "c" * 64,
                                          "tools/official_client_capture/codex_upgrade_policy_certification.py": stub}.items():
                    (side / relative).write_text(content, encoding="utf-8")
            (data / "control").mkdir()
            receipt = data / "control" / "claude-managed-deploy-20261007t000000z.json"
            receipt.write_text(json.dumps({"status": "passed", "client": "claude", "identity": {"tool_files_sha256": "c" * 64}}), encoding="utf-8")
            (data / "control" / "codex-01600-supervisor-enable-20261007t000000z.json").write_text(
                json.dumps({"status": "passed", "tool_files_sha256": "a" * 64}), encoding="utf-8")
            body = (self.function("entry-gates.sh", "check_deployed_others") + 'DEPLOY_OTHERS_SOURCE=()\ncheck_deployed_others\n'
                    'printf "%s\\n" "${DEPLOY_OTHERS_SOURCE[@]}"\n')
            env = {"DRV": str(DRIVER), "TREE": str(tree), "D": str(data), "PYC": str(root / "pyc"), "OUT": str(root / "out"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            checked = self.shell(body, "codex", **env)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertEqual(checked.stdout.splitlines(), ["--source", f"deploy_receipts_others=claude={receipt.name}"])
            (data / "docs" / "CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md").write_text("# 旧指南\n", encoding="utf-8")
            refused = self.shell(body, "codex", **env)
            self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
            self.assertIn("其余已纳管客户端的部署不是本提交", refused.stdout)
            self.assertIn("docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", refused.stdout)

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
    def test_entry_dryrun_and_vc5_admission_are_wired_for_claude(self) -> None:
        """B5a-10 落实了入口空跑（客户端表不再登记 tool:entry-dryrun）：Claude 的空跑按 Claude 的部署收据起，数据根没有它时报的是
        缺收据，不是「尚未接入」。B5b-01 落实了 VC-5 准入（不再登记 tool:vc5-admission）：两客户端都放行；Claude 的 VC-5 派发
        （run／seal／accept／recover）在 B5b-01b 接入前仍登记为待接入。"""

        self.assertIsNone(CT.pending("claude", "tool:entry-dryrun"))
        self.assertIsNone(CT.pending("claude", "tool:vc5-admission"))
        self.assertEqual(sorted(CT.TABLE["claude"]["pending"]), ["tool:vc5-accept", "tool:vc5-recover", "tool:vc5-run", "tool:vc5-seal"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = driver_tests._DriverFixture(root)
            env = root / "claude.sh"
            env.write_text(claude_env_text(fixture), encoding="utf-8")
            import argparse
            args = argparse.Namespace(runroot=fixture.runroot, data_root=fixture.data_root, vc_env=env, bundle=root / "x.bundle",
                                      branch="claude/x", commit="a" * 40, opening=False, reexecute=False, to=None, entry=root / "entry.sh",
                                      unit_inherit_request=None)
            with self.assertRaisesRegex(Exception, "数据根没有受管工具部署收据") as caught:
                dr.start(args)
            self.assertEqual(type(caught.exception).__name__, "ValidationError")
            admission = driver_tests.load_script("vc5_admission")
            parser = load_parse_env()
            config = parser.parse(env.read_text(encoding="utf-8"))
            with mock.patch.object(sys, "path", [str(DRIVER), *sys.path]):
                admission._require_client_admission(config)
                admission._require_client_admission(parser.parse(fixture.env_file.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
