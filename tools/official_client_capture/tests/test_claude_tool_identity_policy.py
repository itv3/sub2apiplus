"""Claude 身份引擎 v3（Claude 升级控制面设计 B1-07）：多根清单、跨模块按符号闭包、经适配器的间接调用可见。

在合成的双根受管树上验证：清单覆盖两个根与 ``.mjs``、排除 tests；经 ``adapter.orchestrator.<名字>``、适配器方法成员与
模块型成员后接属性的调用都进入闭包——evidence 根、wire 根、compare／accept 两个读侧根各取一例，只改被间接调用的
Claude 实现时对应摘要必须变化、其他摘要不变；共享 control 模块按符号计入，wire 闭包经适配器碰到的 evidence 层模块
不计入；``orchestrator`` 被赋值、传递、``getattr`` 与不在服务面的名字一律拒绝；跨进程记忆化与重算逐字一致，开启时
改了文件不会命中旧结果。另在真实受管树副本上叠加同一组 Claude 文件做同样的突变，
Codex 的 wire、evidence、两个编排器闭包与四项评估器摘要必须不变。
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as v3
from tools.official_client_capture import codex_upgrade_tool_identity_policy as codex_identity
from tools.official_client_capture import upgrade_client_adapter

TOOL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = TOOL_ROOT.parents[1]
CONTROL_ROOT = TOOL_ROOT.parent / "official_client_control"
# 5a 收口（Claude 策略 v3）：Claude 控制面依赖、此前却在忽略清单里的共享文件，归 control 层。
CONTROL_PLANE_SHARED_FILES = (
    "codex_upgrade_campaign_run_rehearsal_receipt.py",
    "codex_upgrade_candidate_build.py",
    "codex_upgrade_job_rehearsal_receipt.py",
    "codex_upgrade_predispatch_stop.py",
    "codex_upgrade_zero_request_smoke.py",
)
# claude_21220 按用途归层：三份历史数据只被 evidence 层的 FW-E 完整 Campaign 与历史候选基线生成器读（基线产物本身在
# evidence 层），归 evidence；历史一致性检查器只由测试运行，归 control。
CLAUDE_21220_LAYERS = {
    "check_coverage.py": "control",
    "hitcc_2_1_197_coverage.json": "evidence_semantics",
    "rules_2_1_220.json": "evidence_semantics",
    "source_2_1_88_coverage.json": "evidence_semantics",
}
# 受管、非忽略模块直接导入被忽略文件的白名单：（导入方, 被导入方）→ 理由。都是共享控制模块里只服务 Codex 的路径。
IGNORED_IMPORT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("codex_upgrade_reconciler.py", "codex_upgrade.py"): "对账器顶层导入 Codex 编排器，只在 Codex 分支使用；Claude 经适配器的编排器服务面",
    ("codex_upgrade_vc0_closeout.py", "codex_upgrade.py"): "VC-0 收口顶层导入 Codex 编排器，Claude 收口经收口画像与编排器服务面（B2-11）",
    ("codex_upgrade_reconciler.py", "codex_upgrade_live_request_provenance.py"): (
        "顶层导入 Codex 的请求计量；对账时一律经适配器的 provenance 成员，Claude 绑定 claude_upgrade_live_request_provenance"
    ),
    ("codex_upgrade_supervisor.py", "build_assertion_bundle.py"): "Codex VC-5 断言证据包核验（ACC-02）；Claude VC-5 批次是否经过由 B4-06b 接批次时确认",
    ("codex_upgrade_supervisor.py", "codex_upgrade_seal_rehearsal.py"): "Codex VC-5 seal 预演门禁；Claude VC-5 批次是否经过由 B4-06b 接批次时确认",
    ("upgrade_control_plane.py", "codex_upgrade_seal_rehearsal.py"): "rehearse-candidate-seal 子命令（Codex 候选 seal 预演），Claude 不注册该子命令",
    ("codex_upgrade_vc0_closeout.py", "model_condition_receipts.py"): "Codex VC-0 收口的中继请求逐帧计数，只在 Codex 收口画像路径上调用",
    ("claude_upgrade_boundary_checks.py", "codex_upgrade_live_request_provenance.py"): (
        "B4-01 f 边界核对 codex-final-wire-isolation 的真实请求精确计数：判 Codex 的 final wire 用 Codex 自己的 mitm 逐请求计数"
        "（只调用、不改，Claude 侧不另写计数器），函数体内导入，只在这一项核对；判定工具一律以命令行调用，文件摘要记进运行报告"
    ),
    ("codex_upgrade_job_rehearsal_receipt.py", "build_evidence_catalog.py"): (
        "Codex 证据标签声明（codex_upgrade_evidence_labels_*）的完整性核对，函数体内导入，只在 Codex 画像路径；Claude 画像自带标签绑定"
    ),
    # 零请求冒烟模块：Claude 只用它的 _NetworkGuard（pre-A3 逐场景执行时把 socket.connect 换成失败关闭），模块顶层导入的
    # 下面四个 Codex 冒烟依赖只服务 Codex 的零请求冒烟本身。
    ("codex_upgrade_zero_request_smoke.py", "codex_upgrade_campaign_disposition.py"): "Codex 零请求冒烟的 Campaign 处置，Claude 只用 _NetworkGuard",
    ("codex_upgrade_zero_request_smoke.py", "codex_upgrade_live_request_provenance.py"): "Codex 零请求冒烟的请求计量，Claude 只用 _NetworkGuard",
    ("codex_upgrade_zero_request_smoke.py", "codex_upgrade_official_attempt_audit.py"): "Codex 零请求冒烟的官方 attempt 审计，Claude 只用 _NetworkGuard",
    ("codex_upgrade_zero_request_smoke.py", "codex_upgrade_time_reconciliation.py"): "Codex 零请求冒烟的时间对账，Claude 只用 _NetworkGuard",
}
POLICY: dict[str, Any] = {
    "schema_version": "tool-identity-policy/v3",
    "client": "claude",
    "policy_version": 1,
    "description": "测试夹具：双根受管树",
    "roots": ["tools/official_client_capture", "tools/official_client_control"],
    "suffixes": [".py", ".sh", ".json", ".mjs"],
    "ignored_prefixes": [],
    "layers": {
        "wire_producer": {"files": ["claude_drive_tui_cli.py"]},
        "evidence_semantics": {"files": [
            "claude_upgrade_live_request_provenance.py", "claude_rule_assertion.py",
            "claude_build_rule_assertion_results.py", "claude_bundle_ast.mjs", "official_client_control/gates.py",
        ]},
        "control": {"files": [
            "__init__.py", "upgrade_client_adapter.py", "upgrade_control_plane.py", "official_client_control/store.py",
        ]},
    },
    "orchestrator": {
        "file": "official_client_capture/claude_upgrade.py",
        "wire_roots": ["_run_job_impl"],
        "evidence_roots": ["scan_evidence"],
        "dynamic_call_names": ["eval", "exec", "__import__", "globals", "locals", "vars"],
    },
    "default_layer": "wire_producer",
    "runtime_bindings": [{"name": "node", "path": "runtime/claude-tooling/node", "sha256": "a" * 64, "version": "v20.0.0"}],
    "evaluator": {
        "checker": "official_client_capture/claude_rule_assertion.py",
        "builder": "official_client_capture/claude_build_rule_assertion_results.py",
        "compare_reader_roots": ["compare_campaign"],
        "accept_reader_roots": ["accept_campaign"],
    },
    "reader_barriers": {},
}
# 共享 control 模块：只以字面形式经适配器访问 Claude 实现。
SHARED = '''
from tools.official_client_capture import upgrade_client_adapter


def shared_flow(campaign_dir):
    adapter = upgrade_client_adapter.adapter_for("claude")
    digest = adapter.orchestrator._fingerprint({"campaign": str(campaign_dir)})
    proofs = adapter.segment_reuse_proofs(campaign_dir, "c", "a", "ar1", [])
    receipt = adapter.provenance.collect_campaign_provenance(campaign_dir, formal_campaign_id="x")
    return digest, proofs, receipt


def wire_flow(adapter, job):
    adapter.provenance.collect_campaign_provenance(job, formal_campaign_id="wire")
    return adapter.orchestrator._read_json(job, "job")


def accept_flow(adapter, campaign_dir):
    return {"campaign": str(campaign_dir), "at": adapter.orchestrator._utc_now()}
'''
# Claude 编排器：四类根都经共享 control 模块回到本模块的实现。
ORCHESTRATOR = '''
from tools.official_client_capture import upgrade_client_adapter, upgrade_control_plane


def _fingerprint(payload):
    return "fingerprint"


def _read_json(path, label):
    return {"label": label}


def _utc_now():
    return "2026-10-07T00:00:00Z"


def segment_reuse_proofs(*arguments):
    return {}


def _run_job_impl(job):
    return upgrade_control_plane.wire_flow(upgrade_client_adapter.adapter_for("claude"), job)


def scan_evidence(campaign_dir):
    return upgrade_control_plane.shared_flow(campaign_dir)


def compare_campaign(campaign_dir):
    return upgrade_control_plane.shared_flow(campaign_dir)


def accept_campaign(campaign_dir):
    return upgrade_control_plane.accept_flow(upgrade_client_adapter.adapter_for("claude"), campaign_dir)
'''
CLAUDE_FILES = {
    "official_client_capture/upgrade_control_plane.py": SHARED,
    "official_client_capture/claude_upgrade.py": ORCHESTRATOR,
    "official_client_capture/claude_upgrade_live_request_provenance.py": "def collect_campaign_provenance(campaign_dir, **kwargs):\n    return {}\n",
    "official_client_capture/claude_rule_assertion.py": "CHECK = 1\n",
    "official_client_capture/claude_build_rule_assertion_results.py": "BUILD = 1\n",
}
MINIMAL_FILES = {
    "official_client_capture/__init__.py": "",
    "official_client_capture/claude_bundle_ast.mjs": "export const marker = 1;\n",
    "official_client_capture/claude_drive_tui_cli.py": "def drive():\n    return 1\n",
    "official_client_capture/tests/test_ignored.py": "IGNORED = 1\n",
    "official_client_control/__init__.py": "",
    "official_client_control/store.py": "def append_fact():\n    return 1\n",
    "official_client_control/gates.py": "def gate():\n    return 1\n",
}
WIRE, EVIDENCE, CONTROL = "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"
COMPARE, ACCEPT = "compare_reader_sha256", "accept_reader_sha256"
# 每例只改一个被间接调用的 Claude 实现：标签 → (条目键, 原文, 新文本, 必须变化的摘要, 必须不变的摘要)。
MUTATIONS: dict[str, tuple[str, str, str, set[str], set[str]]] = {
    "evidence 与 compare 根经 orchestrator.<名字>": (
        "official_client_capture/claude_upgrade.py", 'return "fingerprint"', 'return "fingerprint-v2"',
        {EVIDENCE, COMPARE}, {WIRE, ACCEPT},
    ),
    "适配器方法成员 segment_reuse_proofs": (
        "official_client_capture/claude_upgrade.py", "    return {}\n", "    return {'changed': True}\n",
        {EVIDENCE, COMPARE}, {WIRE, ACCEPT},
    ),
    "模块型成员 provenance 后接属性": (
        "official_client_capture/claude_upgrade_live_request_provenance.py", "return {}", "return {'count': 1}",
        {EVIDENCE, COMPARE}, {WIRE, ACCEPT, CONTROL},
    ),
    "wire 根经适配器": (
        "official_client_capture/claude_upgrade.py", 'return {"label": label}', 'return {"label": label, "version": 2}',
        {WIRE}, {EVIDENCE, COMPARE, ACCEPT},
    ),
    "accept 读侧根经适配器": (
        "official_client_capture/claude_upgrade.py", 'return "2026-10-07T00:00:00Z"', 'return "2026-10-08T00:00:00Z"',
        {ACCEPT}, {WIRE, EVIDENCE, COMPARE},
    ),
    "共享 control 模块按符号计入": (
        "official_client_capture/upgrade_control_plane.py", '{"campaign": str(campaign_dir), "at"', '{"campaign_dir": str(campaign_dir), "at"',
        {ACCEPT, CONTROL}, {WIRE, EVIDENCE, COMPARE},
    ),
}


class _ClaudeTreeMixin:
    """在临时仓库里搭两个受管根：合成的最小树，或真实受管树副本；两者都叠加同一组 Claude 文件。"""

    REAL_TREE = False

    def setUp(self) -> None:
        memo = mock.patch.dict(os.environ, {codex_identity.IDENTITY_MEMO_ENV: ""})
        memo.start()
        self.addCleanup(memo.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name)
        tools = self.repo / "tools"
        if self.REAL_TREE:
            ignore = shutil.ignore_patterns("tests", "versions", "__pycache__")
            shutil.copytree(TOOL_ROOT, tools / "official_client_capture", ignore=ignore)
            shutil.copytree(CONTROL_ROOT, tools / "official_client_control", ignore=ignore)
            files = dict(CLAUDE_FILES)
        else:
            files = {**MINIMAL_FILES, **CLAUDE_FILES}
        for key, text in files.items():
            path = tools / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        if not self.REAL_TREE:
            shutil.copyfile(TOOL_ROOT / "upgrade_client_adapter.py", tools / "official_client_capture" / "upgrade_client_adapter.py")
        policy_path = self.repo / "policy.json"
        policy_path.write_text(json.dumps(POLICY), encoding="utf-8")
        self.policy = v3.load_policy(policy_path)

    def _claude_digests(self) -> dict[str, str]:
        identity = v3.compute_identity(self.policy, self.repo)
        return {
            **{field: identity[field] for field in (WIRE, EVIDENCE, CONTROL)},
            **v3.evaluator_dependency_digests(self.repo, self.policy),
        }

    def _edit(self, key: str, old: str, new: str) -> None:
        path = self.repo / "tools" / key
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"{key} 里找不到唯一的 {old!r}")  # type: ignore[attr-defined]
        path.write_text(text.replace(old, new), encoding="utf-8")

    def _check_mutations(self) -> None:
        """逐例累积突变：每例只比较这一步前后的 Claude 摘要。"""

        for label, (key, old, new, changed, unchanged) in MUTATIONS.items():
            with self.subTest(label=label):  # type: ignore[attr-defined]
                before = self._claude_digests()
                self._edit(key, old, new)
                after = self._claude_digests()
                for field in changed:
                    self.assertNotEqual(before[field], after[field], f"{label}：{field} 应变化")  # type: ignore[attr-defined]
                for field in unchanged:
                    self.assertEqual(before[field], after[field], f"{label}：{field} 不应变化")  # type: ignore[attr-defined]


class ClaudeIdentityEngineTests(_ClaudeTreeMixin, unittest.TestCase):
    def test_tree_covers_both_roots_and_mjs(self) -> None:
        digests = v3.managed_tree_digests(self.repo, self.policy)
        self.assertIn("official_client_capture/claude_bundle_ast.mjs", digests)
        self.assertIn("official_client_control/store.py", digests)
        self.assertNotIn("official_client_capture/tests/test_ignored.py", digests)
        identity = v3.compute_identity(self.policy, self.repo)
        self.assertEqual((identity["client"], identity["layer_counts"]["defaulted"]), ("claude", 0))
        self.assertEqual(set(v3.evaluator_dependency_digests(self.repo, self.policy)), set(v3.EVALUATOR_DIGEST_FIELDS))

    def test_indirect_calls_are_visible(self) -> None:
        self._check_mutations()

    def test_runtime_bindings_enter_control(self) -> None:
        before = v3.compute_identity(self.policy, self.repo)
        changed = dict(self.policy, runtime_bindings=[dict(POLICY["runtime_bindings"][0], sha256="b" * 64)])
        after = v3.compute_identity(changed, self.repo)
        self.assertNotEqual(before[CONTROL], after[CONTROL])
        self.assertEqual((before[WIRE], before[EVIDENCE]), (after[WIRE], after[EVIDENCE]))

    def test_non_literal_orchestrator_use_is_rejected(self) -> None:
        line = '    digest = adapter.orchestrator._fingerprint({"campaign": str(campaign_dir)})\n'
        for label, replacement in (
            ("赋值", "    orchestrator = adapter.orchestrator\n    digest = orchestrator._fingerprint({})\n"),
            ("传递", "    digest = str(adapter.orchestrator)\n"),
            ("getattr", '    digest = getattr(adapter.orchestrator, "_fingerprint")({})\n'),
            ("不在服务面", "    digest = adapter.orchestrator.not_in_surface()\n"),
        ):
            with self.subTest(label=label):
                path = self.repo / "tools" / "official_client_capture" / "upgrade_control_plane.py"
                original = path.read_text(encoding="utf-8")
                path.write_text(original.replace(line, replacement), encoding="utf-8")
                try:
                    with self.assertRaises(v3.ToolIdentityPolicyError):
                        v3.compute_identity(self.policy, self.repo)
                finally:
                    path.write_text(original, encoding="utf-8")

    def test_reaching_ignored_file_is_rejected(self) -> None:
        """闭包到达被策略忽略的另一客户端专属文件即失败；只导入、不在可达函数里使用不受影响。"""

        tools = self.repo / "tools" / "official_client_capture"
        (tools / "codex_only.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        policy_path = self.repo / "policy-ignored.json"
        policy_path.write_text(json.dumps(dict(POLICY, ignored_prefixes=["official_client_capture/codex_"])), encoding="utf-8")
        policy = v3.load_policy(policy_path)
        shared = tools / "upgrade_control_plane.py"
        original = shared.read_text(encoding="utf-8")
        imported = original.replace(
            "from tools.official_client_capture import upgrade_client_adapter\n",
            "from tools.official_client_capture import codex_only, upgrade_client_adapter\n",
        )
        shared.write_text(imported, encoding="utf-8")
        v3.compute_identity(policy, self.repo)
        shared.write_text(imported.replace("    return digest, proofs, receipt\n", "    return digest, proofs, receipt, codex_only.helper()\n"), encoding="utf-8")
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "shared_flow 引用了被策略忽略的文件 official_client_capture/codex_only.py"):
            v3.compute_identity(policy, self.repo)
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "被策略忽略的文件"):
            v3.evaluator_dependency_digests(self.repo, policy)

    def test_import_outside_managed_roots_is_rejected(self) -> None:
        """闭包里的模块导入受管根外的仓库模块即失败：部署树没有根外模块，根外的变化也不进身份（2026-10-07）。"""

        shared = self.repo / "tools" / "official_client_capture" / "upgrade_control_plane.py"
        original = shared.read_text(encoding="utf-8")
        head = "from tools.official_client_capture import upgrade_client_adapter\n"
        body = "    adapter = upgrade_client_adapter.adapter_for(\"claude\")\n"
        for label, old, new, target in (
            ("模块顶层 from tools import", head, head + "from tools import generate_claude_fw_g_profile\n", "tools.generate_claude_fw_g_profile"),
            ("函数内 from tools.<根外包>", body, body + "    from tools.ci import entry_gates\n", "tools.ci"),
            ("import tools.<根外模块>", head, head + "import tools.arm64_supervised_deploy\n", "tools.arm64_supervised_deploy"),
            ("相对导入跳出受管根", head, head + "from .. import check_ledger_completeness\n", "tools.check_ledger_completeness"),
            ("importlib 字面量", body, body + "    helper = importlib.import_module(\"tools.codex_closeout\")\n", "tools.codex_closeout"),
        ):
            with self.subTest(label=label):
                shared.write_text(original.replace(old, new), encoding="utf-8")
                try:
                    with self.assertRaisesRegex(v3.ToolIdentityPolicyError, f"导入了受管根外的仓库模块 {target}"):
                        v3.compute_identity(self.policy, self.repo)
                    with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "受管根外"):
                        v3.evaluator_dependency_digests(self.repo, self.policy)
                finally:
                    shared.write_text(original, encoding="utf-8")
        # 两个受管根之间互相导入照常。
        shared.write_text(original.replace(head, head + "from tools.official_client_control import store\n"), encoding="utf-8")
        v3.compute_identity(self.policy, self.repo)

    def test_memo_matches_recompute(self) -> None:
        plain = self._claude_digests()
        identity = v3.compute_identity(self.policy, self.repo)
        with tempfile.TemporaryDirectory() as memo, mock.patch.dict(os.environ, {codex_identity.IDENTITY_MEMO_ENV: memo}):
            first, first_identity = self._claude_digests(), v3.compute_identity(self.policy, self.repo)
            second, second_identity = self._claude_digests(), v3.compute_identity(self.policy, self.repo)
            self.assertEqual(len(list(Path(memo).iterdir())), 2)
            # 记忆化开启时突变仍逐例可见：键含受管树逐文件摘要，改了文件不会命中旧结果。
            self._check_mutations()
        self.assertEqual((first, second), (plain, plain))
        self.assertEqual(json.dumps(first_identity), json.dumps(identity))
        self.assertEqual(json.dumps(second_identity), json.dumps(identity))


class ClaudeImportedSymbolClosureTests(_ClaudeTreeMixin, unittest.TestCase):
    """导入别名与重导出必须跟到真实定义；导入环拒绝，正常函数递归、忽略文件与屏障维持原合同。"""

    def setUp(self) -> None:
        super().setUp()
        self._write("official_client_capture/claude_upgrade.py", """
            from .upgrade_control_plane import PublicError as ConfigurationError
        """)
        self._write("official_client_capture/upgrade_control_plane.py", """
            from tools.official_client_control.store import ImplementationError as PublicError
        """)
        self._write("official_client_control/store.py", '\n'.join([
            'DETAIL = "first"',
            '',
            'class ImplementationError(ValueError):',
            '    def describe(self):',
            '        return DETAIL',
            '',
            'def unused_helper():',
            '    return "unused"',
            '',
        ]))

    def _write(self, key: str, source: str) -> None:
        path = self.repo / "tools" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")

    def _closure(self, symbol: str = "ConfigurationError", *, barriers: tuple[str, ...] = ()) -> dict[str, Any]:
        return v3.closure(
            self.policy,
            self.repo,
            v3.managed_tree_digests(self.repo, self.policy),
            [("tools.official_client_capture.claude_upgrade", symbol)],
            layer=None,
            barriers=barriers,
        )

    def test_reexported_class_and_its_dependencies_enter_closure(self) -> None:
        before = self._closure()
        records = {(item["module"], item["name"]): item for item in before["symbols"]}
        self.assertEqual(records[("tools.official_client_capture.claude_upgrade", "ConfigurationError")]["kind"], "import")
        self.assertEqual(records[("tools.official_client_capture.upgrade_control_plane", "PublicError")]["kind"], "import")
        self.assertIn(("tools.official_client_control.store", "ImplementationError"), records)
        self.assertIn(("tools.official_client_control.store", "DETAIL"), records)
        self.assertEqual(before["modules"], [])
        self._edit("official_client_control/store.py", 'DETAIL = "first"', 'DETAIL = "second"')
        changed = self._closure()
        self.assertNotEqual(before["closure_sha256"], changed["closure_sha256"])
        self._edit("official_client_control/store.py", 'return "unused"', 'return "irrelevant"')
        self.assertEqual(changed["closure_sha256"], self._closure()["closure_sha256"])

    def test_import_target_is_bound_even_when_definitions_are_identical(self) -> None:
        original = (self.repo / "tools/official_client_control/store.py").read_text(encoding="utf-8")
        self._write("official_client_control/store_copy.py", original)
        control = self.policy["layers"]["control"]
        self.policy = {
            **self.policy,
            "layers": {
                **self.policy["layers"],
                "control": {**control, "files": [*control["files"], "official_client_control/store_copy.py"]},
            },
        }
        before = self._closure()
        self._edit("official_client_capture/upgrade_control_plane.py", "from tools.official_client_control.store import", "from tools.official_client_control.store_copy import")
        after = self._closure()
        old_edge = next(item for item in before["symbols"] if item["name"] == "PublicError")
        new_edge = next(item for item in after["symbols"] if item["name"] == "PublicError")
        self.assertEqual(new_edge["sha256"], codex_identity._fingerprint({
            "module": "tools.official_client_control.store_copy", "name": "ImplementationError",
        }))
        self.assertNotEqual(old_edge["sha256"], new_edge["sha256"])
        self.assertNotEqual(before["closure_sha256"], after["closure_sha256"])

    def test_import_cycle_is_rejected_and_explicit_barrier_stops_it(self) -> None:
        self._write("official_client_control/store.py", """
            from tools.official_client_capture.claude_upgrade import ConfigurationError as ImplementationError
        """)
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "导入符号构成循环"):
            self._closure()
        barrier = "tools.official_client_control.store:ImplementationError"
        stopped = self._closure(barriers=(barrier,))
        self.assertIn(barrier, stopped["barriers"])
        self.assertEqual({item["name"] for item in stopped["symbols"]}, {"ConfigurationError", "PublicError"})
        self.assertEqual(stopped["modules"], [])

    def test_function_recursion_through_import_alias_is_allowed(self) -> None:
        self._write("official_client_capture/claude_upgrade.py", """
            from . import upgrade_control_plane

            def recursive_function(depth):
                if depth:
                    return upgrade_control_plane.helper(depth - 1)
                return 0
        """)
        self._write("official_client_capture/upgrade_control_plane.py", """
            from .claude_upgrade import recursive_function as helper
        """)
        record = self._closure("recursive_function")
        self.assertEqual({item["name"]: item["kind"] for item in record["symbols"]}, {
            "recursive_function": "function", "helper": "import",
        })

    def test_missing_orchestrator_symbol_still_fails(self) -> None:
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "编排器符号不存在：MissingError"):
            self._closure("MissingError")
        self._write("official_client_control/store.py", """
            from tools.official_client_capture.claude_upgrade import MissingError as ImplementationError
        """)
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "编排器符号不存在：MissingError"):
            self._closure()

    def test_missing_other_module_symbol_keeps_whole_file_fallback(self) -> None:
        self._write("official_client_control/store.py", "UNRELATED = 1\n")
        record = self._closure()
        self.assertEqual([item["module"] for item in record["modules"]], ["tools.official_client_control.store"])
        self.assertEqual({item["name"] for item in record["symbols"]}, {"ConfigurationError", "PublicError"})

    def test_reexport_cannot_hide_an_ignored_definition(self) -> None:
        self.policy = {**self.policy, "ignored_prefixes": ["official_client_control/store.py"]}
        with self.assertRaisesRegex(v3.ToolIdentityPolicyError, "被策略忽略的文件 official_client_control/store.py"):
            self._closure()


class CodexUnaffectedByClaudeMutationTests(_ClaudeTreeMixin, unittest.TestCase):
    """真实受管树副本：Claude 侧的间接调用突变仍可见，Codex 身份敏感摘要不动。"""

    REAL_TREE = True

    def _codex_digests(self) -> dict[str, Any]:
        root = self.repo / "tools" / "official_client_capture"
        policy = codex_identity.load_policy(root / codex_identity.POLICY_FILENAME)
        digests = codex_identity._managed_tree_digests(root)
        entries = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
        identity = codex_identity.compute_identity_v2(policy, root, entries)
        return {
            WIRE: identity[WIRE],
            EVIDENCE: identity[EVIDENCE],
            "closures": {layer: item["closure_sha256"] for layer, item in identity["orchestrator_closures"].items()},
            **codex_identity.evaluator_dependency_digests(root, policy),
        }

    def test_claude_mutations_leave_codex_identity_unchanged(self) -> None:
        codex_before = self._codex_digests()
        self._check_mutations()
        self.assertEqual(self._codex_digests(), codex_before)


class ClaudePolicyFileTests(unittest.TestCase):
    """B1-08：Claude 身份策略对两个受管根逐文件归层；Codex 专属文件忽略，两客户端共用的文件与 Codex 策略同层。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = v3.load_policy()
        cls.codex = codex_identity.load_policy()
        cls.digests = v3.managed_tree_digests(REPOSITORY_ROOT, cls.policy)
        cls.grouped = v3.layer_entries(cls.policy, cls.digests)

    def test_every_managed_file_has_a_layer(self) -> None:
        self.assertEqual((self.policy["client"], self.policy["policy_version"]), ("claude", 13))
        self.assertEqual([entry["path"] for entry in self.grouped["defaulted"]], [])
        for layer in v3.LAYERS:
            self.assertTrue(self.grouped[layer], layer)

    def test_h2_files_have_explicit_semantic_layers(self) -> None:
        """H2字节解析与导入改变wire身份；导出语义和运行库证明进入evidence身份。"""
        expected = {
            "claude_mac_capture_h2.py": "wire_producer",
            "claude_mac_capture_h2_dependencies.py": "wire_producer",
            "claude_mac_capture_h2_contract.py": "evidence_semantics",
            "claude_mac_capture_h2_runtime.py": "evidence_semantics",
        }
        for name, layer in expected.items():
            with self.subTest(file=name):
                self.assertEqual(v3.classify_path(self.policy, "official_client_capture/" + name), layer)

    def test_mac_observation_and_recipe_layers(self) -> None:
        """原生观测与启动选择分别影响证据语义和wire生产身份，不留默认归层。"""
        self.assertEqual(v3.classify_path(self.policy, "official_client_capture/claude_mac_process_identity.py"), "evidence_semantics")
        self.assertEqual(v3.classify_path(self.policy, "official_client_capture/claude_mac_product_recipe.py"), "wire_producer")
        self.assertEqual(v3.classify_path(self.policy, "official_client_capture/claude_mac_product_recorder.py"), "wire_producer")
        self.assertEqual(v3.classify_path(self.policy, "official_client_control/qualification_bindings.py"), "evidence_semantics")
        self.assertEqual(v3.classify_path(self.policy, "official_client_control/replay_bindings.py"), "evidence_semantics")

    def test_native_process_projection_is_claude_only_and_explicitly_layered(self) -> None:
        """原生.m只进入Claude受管树；新源和接线不影响Codex敏感层。"""
        expected = {
            "claude_mac_process_events_native.m": "evidence_semantics",
            "claude_mac_process_events_contract.py": "evidence_semantics",
            "claude_mac_process_events.py": "evidence_semantics",
            "claude_mac_process_events_build.py": "control",
        }
        self.assertIn(".m", self.policy["suffixes"])
        self.assertNotIn("claude_mac_process_events_native.m", codex_identity._managed_tree_digests(TOOL_ROOT))
        for name, layer in expected.items():
            key = "official_client_capture/" + name
            with self.subTest(file=name):
                self.assertIn(key, self.digests)
                self.assertEqual(v3.classify_path(self.policy, key), layer)
                self.assertIsNone(codex_identity.classify_path(self.codex, name))

    def test_orchestrators_and_shared_control(self) -> None:
        for key in ("official_client_capture/codex_upgrade.py", "official_client_capture/codex_upgrade_client_adapter.py"):
            self.assertIsNone(v3.classify_path(self.policy, key), key)
        self.assertEqual(v3.classify_path(self.policy, self.policy["orchestrator"]["file"]), "control")
        for name in ("codex_upgrade_vc0_closeout.py", "upgrade_client_adapter.py", "upgrade_control_plane.py", "claude_tool_identity_policy_v2.json"):
            self.assertEqual(v3.classify_path(self.policy, f"official_client_capture/{name}"), "control", name)

    def test_adapter_bound_modules_are_not_ignored(self) -> None:
        """Claude 适配器绑定的模块都必须归层：绑定即依赖，被忽略的模块一到达闭包 v3 引擎就失败。"""

        for member, (module, _symbol) in upgrade_client_adapter.ADAPTER_BINDINGS["claude"].items():
            with self.subTest(member=member):
                self.assertIsNotNone(v3.classify_path(self.policy, f"official_client_capture/{module}.py"))

    def test_shared_files_match_codex_layers(self) -> None:
        shared = 0
        for key in sorted(self.digests):
            root, _, relative = key.partition("/")
            claude_layer = v3.classify_path(self.policy, key)
            codex_layer = codex_identity.classify_path(self.codex, relative) if root == "official_client_capture" else None
            if claude_layer is None or codex_layer is None:
                continue
            shared += 1
            with self.subTest(path=relative):
                self.assertEqual(claude_layer, codex_layer)
        self.assertGreater(shared, 100)

    def test_whole_file_layers_do_not_import_ignored_files(self) -> None:
        """wire／evidence 层整文件计入、不逐符号遍历，它们导入被忽略的文件会让该依赖在 Claude 身份里不可见。"""

        index = v3._module_index(self.policy, self.digests)
        modules = {key: module for module, key in index.items()}
        for key in sorted(self.digests):
            if v3.classify_path(self.policy, key) not in {"wire_producer", "evidence_semantics"} or key not in modules:
                continue
            tree = ast.parse((REPOSITORY_ROOT / "tools" / key).read_text(encoding="utf-8"))
            nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign))]
            aliases, symbols = v3._resolve_imports(nodes, modules[key], index)
            targets = set(aliases.values()) | {owner for owner, _symbol in symbols.values()}
            with self.subTest(path=key):
                self.assertEqual([target for target in sorted(targets) if v3.classify_path(self.policy, index[target]) is None], [])

    def test_control_plane_dependencies_are_layered(self) -> None:
        """5a 收口（策略 v3）：Claude 控制面依赖的六个共享文件归 control，claude_21220 按用途归层（历史数据归 evidence、
        只被测试运行的历史检查器归 control），不再留在忽略清单里。"""

        for name in CONTROL_PLANE_SHARED_FILES:
            with self.subTest(path=name):
                self.assertEqual(v3.classify_path(self.policy, f"official_client_capture/{name}"), "control")
        for name, layer in CLAUDE_21220_LAYERS.items():
            with self.subTest(path=name):
                self.assertEqual(v3.classify_path(self.policy, f"official_client_capture/claude_21220/{name}"), layer)
        self.assertEqual(
            sorted(key.removeprefix("official_client_capture/claude_21220/") for key in self.digests if key.startswith("official_client_capture/claude_21220/")),
            sorted(CLAUDE_21220_LAYERS),
            "claude_21220 新增文件要在这里按用途登记层",
        )

    def test_non_ignored_modules_import_ignored_files_only_on_allowlist(self) -> None:
        """防回归（5a 收口）：受管、非忽略的模块直接导入被忽略的文件，就是「Claude 依赖它、它的变化却不进 Claude 层摘要」的
        身份盲区。只允许 ``IGNORED_IMPORT_ALLOWLIST`` 里登记过理由的 Codex 专属路径；其余一律在策略里归层或改经适配器。"""

        index = v3._module_index(self.policy, self.digests)
        modules = {key: module for module, key in index.items()}
        found: set[tuple[str, str]] = set()
        for key in sorted(self.digests):
            if v3.classify_path(self.policy, key) is None or key not in modules:
                continue
            tree = ast.parse((REPOSITORY_ROOT / "tools" / key).read_text(encoding="utf-8"))
            nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign))]
            aliases, symbols = v3._resolve_imports(nodes, modules[key], index)
            for target in set(aliases.values()) | {owner for owner, _symbol in symbols.values()}:
                if v3.classify_path(self.policy, index[target]) is None:
                    found.add((key.removeprefix("official_client_capture/"), index[target].removeprefix("official_client_capture/")))
        self.assertEqual(sorted(found - set(IGNORED_IMPORT_ALLOWLIST)), [], "新的身份盲区：在策略里归层，或登记进白名单并写明理由")
        self.assertEqual(sorted(set(IGNORED_IMPORT_ALLOWLIST) - found), [], "白名单里有已经不存在的导入，删掉")

    def test_typescript_binding_matches_makefile(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        frozen = re.search(r"^CAPTURE_TYPESCRIPT_SHA256 := ([0-9a-f]{64})$", makefile, re.MULTILINE)
        bindings = {item["name"]: item for item in self.policy["runtime_bindings"]}
        self.assertIsNotNone(frozen)
        self.assertEqual(bindings["typescript"]["sha256"], frozen.group(1))


if __name__ == "__main__":
    unittest.main()
