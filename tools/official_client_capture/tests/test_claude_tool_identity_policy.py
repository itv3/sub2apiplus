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
        self.assertEqual((self.policy["client"], self.policy["policy_version"]), ("claude", 1))
        self.assertEqual([entry["path"] for entry in self.grouped["defaulted"]], [])
        for layer in v3.LAYERS:
            self.assertTrue(self.grouped[layer], layer)

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

    def test_typescript_binding_matches_makefile(self) -> None:
        makefile = (REPOSITORY_ROOT / "Makefile").read_text(encoding="utf-8")
        frozen = re.search(r"^CAPTURE_TYPESCRIPT_SHA256 := ([0-9a-f]{64})$", makefile, re.MULTILINE)
        bindings = {item["name"]: item for item in self.policy["runtime_bindings"]}
        self.assertIsNotNone(frozen)
        self.assertEqual(bindings["typescript"]["sha256"], frozen.group(1))


if __name__ == "__main__":
    unittest.main()
