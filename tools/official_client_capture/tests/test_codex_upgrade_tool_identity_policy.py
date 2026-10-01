"""工具身份四层策略：全树可分类、层级漂移归类、编排器闭包静态可解析。"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]

# 本文件的用例核对身份的计算过程（解析次数、缓存释放），一律关掉跨进程记忆化（统一调度执行器会给单元设上它）；
# IdentityMemoTest 自己在临时目录里打开。
_MEMO_OFF = mock.patch.dict(os.environ, {tip.IDENTITY_MEMO_ENV: ""})


def setUpModule() -> None:
    _MEMO_OFF.start()


def tearDownModule() -> None:
    _MEMO_OFF.stop()


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _standard_segments_split_once() -> Iterator[None]:
    """让标准 ``ast.get_source_segment`` 对同一份源码只分一次行。

    只缓存它内部的分行结果，取段的切片规则与分行规则仍是标准实现；不这样做，对 6.3 万行的编排器逐个取
    1000 多段，标准实现本身就要跑几十秒。解释器内部实现改名时不打补丁（结果仍正确，只是慢）。
    """

    original = getattr(ast, "_splitlines_no_ff", None)
    if original is None:
        yield
        return
    split: dict[str, list[str]] = {}

    def split_once(source: str, maxlines: int | None = None) -> list[str]:
        # 标准实现只读取节点首行到末行，返回全部行与按 maxlines 截断时读到的内容相同。
        if source not in split:
            split[source] = original(source)
        return split[source]

    with mock.patch.object(ast, "_splitlines_no_ff", split_once):
        yield


class ToolIdentityPolicyTests(unittest.TestCase):
    def test_every_managed_file_is_explicitly_classified(self) -> None:
        policy = tip.load_policy()
        self.assertEqual(policy["policy_version"], 7)
        self.assertEqual(len(policy["policy_sha256"]), 64)
        entries = codex_upgrade._tool_tree_entries(TOOL_ROOT)
        grouped = tip.layer_entries(policy, entries)
        self.assertEqual([e["path"] for e in grouped["defaulted"]], [], "新增受管文件必须在策略里登记层级")
        self.assertTrue(all(e["path"].startswith(("claude_", "capturelib/claude_fw_", "fixtures/claude_", "analyze_claude_", "extract_claude_bundle")) for e in grouped["ignored"]))
        for layer in tip.LAYERS:
            self.assertGreater(len(grouped[layer]), 0, layer)
        self.assertEqual(tip.classify_path(policy, "codex_upgrade.py"), "control")
        self.assertEqual(tip.classify_path(policy, "run_official_relay_scenario.sh"), "wire_producer")
        self.assertEqual(tip.classify_path(policy, "codex_upgrade_evidence_labels_0_154_0.json"), "evidence_semantics")
        self.assertEqual(tip.classify_path(policy, "codex_upgrade_supervisor_event.schema.json"), "control")
        self.assertEqual(tip.classify_path(policy, "brand_new_tool.py"), "wire_producer")
        self.assertIsNone(tip.classify_path(policy, "claude_fw_e.py"))

    def test_identity_v2_changes_only_the_touched_layer(self) -> None:
        policy = tip.load_policy()
        entries = codex_upgrade._tool_tree_entries(TOOL_ROOT)
        base = tip.compute_identity_v2(policy, TOOL_ROOT, entries)
        self.assertEqual(base["layer_counts"]["defaulted"], 0)
        self.assertGreater(base["orchestrator_closures"]["wire_producer"]["function_count"], 10)
        self.assertGreater(base["orchestrator_closures"]["evidence_semantics"]["function_count"], base["orchestrator_closures"]["wire_producer"]["function_count"])

        def mutate(path: str) -> dict:
            mutated = [dict(e, sha256="0" * 64) if e["path"] == path else dict(e) for e in entries]
            return tip.compute_identity_v2(policy, TOOL_ROOT, mutated)

        wire = mutate("run_official_relay_scenario.sh")
        self.assertNotEqual(wire["wire_producer_sha256"], base["wire_producer_sha256"])
        self.assertEqual(wire["evidence_semantics_sha256"], base["evidence_semantics_sha256"])
        self.assertEqual(wire["control_sha256"], base["control_sha256"])
        evidence = mutate("codex_upgrade_evidence_labels_0_154_0.json")
        self.assertEqual(evidence["wire_producer_sha256"], base["wire_producer_sha256"])
        self.assertNotEqual(evidence["evidence_semantics_sha256"], base["evidence_semantics_sha256"])
        control = mutate("codex_upgrade_supervisor.py")
        self.assertEqual(control["wire_producer_sha256"], base["wire_producer_sha256"])
        self.assertNotEqual(control["control_sha256"], base["control_sha256"])
        ignored = mutate("claude_fw_e.py")
        self.assertEqual((ignored["wire_producer_sha256"], ignored["evidence_semantics_sha256"], ignored["control_sha256"]), (base["wire_producer_sha256"], base["evidence_semantics_sha256"], base["control_sha256"]))
        drift = tip.layer_drift(policy, entries, [dict(e, sha256="0" * 64) if e["path"] in {"capture.py", "codex_upgrade_timing_ledger.py", "claude_fw_e.py"} else dict(e) for e in entries] + [{"path": "new_tool.py", "sha256": "1" * 64}])
        self.assertEqual(drift, {"wire_producer": ["capture.py", "new_tool.py"], "evidence_semantics": [], "control": ["codex_upgrade_timing_ledger.py"], "ignored": ["claude_fw_e.py"]})

    def test_orchestrator_closure_is_static_and_rejects_dynamic_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = json.loads(tip.DEFAULT_POLICY_PATH.read_text("utf-8"))
            policy["orchestrator"] = {"file": "orchestrator.py", "wire_roots": ["build_argv"], "evidence_roots": ["scan"], "dynamic_call_names": ["eval", "exec", "__import__", "globals", "locals", "vars"]}
            policy["policy_sha256"] = "f" * 64
            (root / "helper.py").write_text("X = 1\n", "utf-8")
            (root / "orchestrator.py").write_text(
                "from tools.official_client_capture import helper\n"
                "LIMIT = 3\n"
                "NAMES = frozenset({'a'}) | EXTRA\n"
                "EXTRA = frozenset({'b'})\n"
                "def build_argv(job):\n    return render(job) + [LIMIT, helper.X]\n"
                "def render(job):\n    return [getattr(job, 'name', None)] + sorted(NAMES)\n"
                "def scan(paths):\n    return [len(paths)]\n"
                "def unrelated():\n    return eval('1')\n",
                "utf-8",
            )
            digests = {"helper.py": "a" * 64, "orchestrator.py": "b" * 64}
            closure = tip.orchestrator_closure(policy, root, ["build_argv"], digests)
            self.assertEqual([f["name"] for f in closure["functions"]], ["build_argv", "render"])
            self.assertEqual([c["name"] for c in closure["constants"]], ["EXTRA", "LIMIT", "NAMES"])
            self.assertEqual(closure["modules"], [{"module": "helper", "path": "helper.py", "sha256": "a" * 64}])
            scan = tip.orchestrator_closure(policy, root, ["scan"], digests)
            self.assertEqual([f["name"] for f in scan["functions"]], ["scan"])
            self.assertNotEqual(scan["closure_sha256"], closure["closure_sha256"])
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "动态调用 eval"):
                tip.orchestrator_closure(policy, root, ["unrelated"], digests)
            (root / "orchestrator.py").write_text(
                "from tools.official_client_capture import helper\n"
                "def build_argv(job):\n    return getattr(helper, job)()\n",
                "utf-8",
            )
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "动态属性分发"):
                tip.orchestrator_closure(policy, root, ["build_argv"], digests)
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "根函数不存在"):
                tip.orchestrator_closure(policy, root, ["missing"], digests)

    def test_policy_shape_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            payload = json.loads(tip.DEFAULT_POLICY_PATH.read_text("utf-8"))
            payload["layers"].pop("control")
            path.write_text(json.dumps(payload), "utf-8")
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "恰好是"):
                tip.load_policy(path)
            payload = json.loads(tip.DEFAULT_POLICY_PATH.read_text("utf-8"))
            payload["policy_version"] = 1
            path.write_text(json.dumps(payload), "utf-8")
            with self.assertRaisesRegex(tip.ToolIdentityPolicyError, "policy_version"):
                tip.load_policy(path)

    def test_tool_identity_exports_policy_v2_digests(self) -> None:
        identity = codex_upgrade._tool_identity(include_git=False)
        policy = tip.load_policy()
        expected = tip.compute_identity_v2(policy, TOOL_ROOT, identity["entries"])
        for field in ("policy_version", "policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256"):
            self.assertEqual(identity[field], expected[field], field)
        self.assertEqual(identity["policy_version"], 7)
        # 旧字段保留，files_sha256 仍是整树
        self.assertEqual(identity["files_sha256"], codex_upgrade._fingerprint({"entries": identity["entries"]}))

    def test_fast_segments_match_standard_for_every_orchestrator_symbol(self) -> None:
        """E1-01：预切行取出的源码段与 ``ast.get_source_segment`` 对编排器全部函数与常量逐字节相同。"""

        source = (TOOL_ROOT / "codex_upgrade.py").read_text(encoding="utf-8")
        tip._parsed_orchestrator.cache_clear()
        functions, constants, _imports, lines = tip._parsed_orchestrator(_sha256(source), source)
        self.assertIsNotNone(lines, "编排器不含回车符，应走预切行路径")
        nodes = [("函数", name, node) for name, node in functions.items()]
        nodes += [("常量", name, node) for name, node in constants.items()]
        self.assertGreater(len(nodes), 1000)
        with _standard_segments_split_once():
            mismatched = [
                f"{kind} {name}"
                for kind, name, node in nodes
                if tip._reader_source_segment(source, lines, node) != (ast.get_source_segment(source, node) or "")
            ]
        self.assertEqual(mismatched, [])
        tip._parsed_orchestrator.cache_clear()

    def test_carriage_return_source_falls_back_to_standard_segments(self) -> None:
        """E1-01：源码含回车符时不预切行，源码段与闭包记录仍按标准实现取得。"""

        source = (
            "LIMIT = 1\r\n\r\n"
            "def root(job):\r\n    return helper(job) + LIMIT\r\n\r\n"
            "def helper(job):\r\n    return len(job)  # 中文注释\r\n"
        )
        digest = _sha256(source)
        tip._parsed_orchestrator.cache_clear()
        functions, constants, _imports, lines = tip._parsed_orchestrator(digest, source)
        self.assertIsNone(lines)
        for node in [*functions.values(), *constants.values()]:
            self.assertEqual(tip._reader_source_segment(source, lines, node), ast.get_source_segment(source, node) or "")

        def standard(node: ast.AST) -> str:
            return hashlib.sha256((ast.get_source_segment(source, node) or "").encode("utf-8")).hexdigest()

        records, constant_records, modules = tip._cached_symbol_closure.__wrapped__(digest, source, ("root",), ("eval",))
        self.assertEqual(list(records), [{"name": name, "sha256": standard(functions[name])} for name in ("helper", "root")])
        self.assertEqual(list(constant_records), [{"name": "LIMIT", "sha256": standard(constants["LIMIT"])}])
        self.assertEqual(modules, ())
        tip._parsed_orchestrator.cache_clear()

    def test_both_root_sets_share_one_parse_and_match_standard_closures(self) -> None:
        """E1-01：wire 与 evidence 两组根共用一次解析、算完即释放；两个闭包与按标准实现取段算出的逐项相同。"""

        policy = tip.load_policy()
        entries = codex_upgrade._tool_tree_entries(TOOL_ROOT)
        digests = {str(e["path"]): str(e["sha256"]) for e in entries}
        root_sets = (("wire_producer", "wire_roots"), ("evidence_semantics", "evidence_roots"))
        tip._cached_symbol_closure.cache_clear()
        tip._parsed_orchestrator.cache_clear()
        fast = {
            layer: tip.orchestrator_closure(policy, TOOL_ROOT, list(policy["orchestrator"][key]), digests, layer=layer)
            for layer, key in root_sets
        }
        info = tip._parsed_orchestrator.cache_info()
        self.assertEqual((info.misses, info.hits), (1, 1), "两组根应共用同一次解析")

        def standard(source: str, lines: tuple[bytes, ...] | None, node: ast.AST) -> str:
            return ast.get_source_segment(source, node) or ""

        tip._cached_symbol_closure.cache_clear()
        with _standard_segments_split_once(), mock.patch.object(tip, "_reader_source_segment", standard):
            for layer, key in root_sets:
                expected = tip.orchestrator_closure(policy, TOOL_ROOT, list(policy["orchestrator"][key]), digests, layer=layer)
                self.assertEqual(fast[layer], expected, layer)
        tip._cached_symbol_closure.cache_clear()
        tip.compute_identity_v2(policy, TOOL_ROOT, entries)
        self.assertEqual(tip._parsed_orchestrator.cache_info().currsize, 0, "算完两组根后应释放语法树")


class IdentityMemoTest(unittest.TestCase):
    """E2-02：跨进程记忆化按完整输入做键——命中与重算逐字节相同、受管文件一变必然重算、缓存坏了照常计算。"""

    def setUp(self) -> None:
        from tools.official_client_capture.tests import managed_tree_copy

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.memo = self.root / "memo"
        patcher = mock.patch.dict(os.environ, {tip.IDENTITY_MEMO_ENV: str(self.memo)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 在副本树上算：用例要改文件验证键会变，不能动原树。
        with mock.patch.object(sys, "pycache_prefix", None):
            self.tree = managed_tree_copy.tool_root(managed_tree_copy.copy_managed_tree(self.root / "copy", include_tests=False))
        self.policy = tip.load_policy(self.tree / tip.POLICY_FILENAME)

    def _identity(self) -> dict:
        return tip.compute_identity_v2(self.policy, self.tree, codex_upgrade._tool_tree_entries(self.tree))

    @staticmethod
    def _dump(value: dict) -> str:
        # 不排序：键顺序也必须与重算一致（调用方按插入顺序写收据）。
        return json.dumps(value, ensure_ascii=False)

    def test_hits_are_byte_identical_to_recomputation_and_skip_the_ast_work(self) -> None:
        identity = self._identity()
        evaluator = tip.evaluator_dependency_digests(self.tree)
        self.assertEqual(len(list(self.memo.glob("identity-v2-*.json"))), 1)
        self.assertEqual(len(list(self.memo.glob("evaluator-*.json"))), 1)
        with mock.patch.object(tip, "_compute_identity_v2", side_effect=AssertionError("命中时不得重算")), \
                mock.patch.object(tip, "evaluator_reader_closure", side_effect=AssertionError("命中时不得重算")):
            self.assertEqual(self._dump(self._identity()), self._dump(identity))
            self.assertEqual(self._dump(tip.evaluator_dependency_digests(self.tree)), self._dump(evaluator))
        with mock.patch.dict(os.environ, {tip.IDENTITY_MEMO_ENV: ""}):
            self.assertEqual(self._dump(self._identity()), self._dump(identity))
            self.assertEqual(self._dump(tip.evaluator_dependency_digests(self.tree)), self._dump(evaluator))

    def test_any_managed_file_change_forces_recomputation(self) -> None:
        identity = self._identity()
        evaluator = tip.evaluator_dependency_digests(self.tree)
        # 改一个与两类闭包都无关的 control 层文件：键里是整树摘要，照样必须重算。
        target = self.tree / "codex_upgrade_timing_ledger.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# E2-02 记忆化键随文件变化\n", encoding="utf-8")
        identity_calls: list[int] = []
        reader_calls: list[int] = []
        real_identity, real_reader = tip._compute_identity_v2, tip.evaluator_reader_closure
        with mock.patch.object(tip, "_compute_identity_v2", side_effect=lambda *a: identity_calls.append(1) or real_identity(*a)), \
                mock.patch.object(tip, "evaluator_reader_closure", side_effect=lambda *a: reader_calls.append(1) or real_reader(*a)):
            changed = self._identity()
            changed_evaluator = tip.evaluator_dependency_digests(self.tree)
        self.assertEqual((len(identity_calls), len(reader_calls)), (1, 2), "受管文件变了必须重算")
        self.assertNotEqual(changed["control_sha256"], identity["control_sha256"])
        self.assertEqual(changed["wire_producer_sha256"], identity["wire_producer_sha256"])
        self.assertEqual(changed_evaluator, evaluator, "这个文件不在评估器读侧闭包里，重算结果不变")
        self.assertEqual(len(list(self.memo.glob("identity-v2-*.json"))), 2)

    def test_source_change_recomputes_even_with_a_stale_file_list(self) -> None:
        """调用方传进来的清单没跟上源码（只算过一次清单）：键里还有按实际文件重算的整树摘要，必须重算。"""

        from tools.official_client_capture.tests import managed_tree_copy

        entries = codex_upgrade._tool_tree_entries(self.tree)
        identity = tip.compute_identity_v2(self.policy, self.tree, entries)
        root = self.policy["orchestrator"]["wire_roots"][0]
        managed_tree_copy._inject_after_docstring(self.tree.parents[1], f"def {root}(", "_e202_marker = 1")
        recomputed = tip.compute_identity_v2(self.policy, self.tree, entries)
        self.assertNotEqual(recomputed["wire_producer_sha256"], identity["wire_producer_sha256"])

    def test_entries_and_policy_are_part_of_the_key(self) -> None:
        entries = codex_upgrade._tool_tree_entries(self.tree)
        identity = tip.compute_identity_v2(self.policy, self.tree, entries)
        mutated = [dict(item, sha256="0" * 64) if item["path"] == "run_official_relay_scenario.sh" else dict(item) for item in entries]
        wire = tip.compute_identity_v2(self.policy, self.tree, mutated)
        self.assertNotEqual(wire["wire_producer_sha256"], identity["wire_producer_sha256"], "清单不同不得命中原条目")
        other_policy = dict(self.policy, policy_sha256="e" * 64)
        self.assertEqual(tip.compute_identity_v2(other_policy, self.tree, entries)["policy_sha256"], "e" * 64, "策略不同不得命中原条目")
        self.assertEqual(len(list(self.memo.glob("identity-v2-*.json"))), 3)

    def test_corrupt_or_mismatched_entries_are_misses(self) -> None:
        identity = self._identity()
        path = next(self.memo.glob("identity-v2-*.json"))
        path.write_text("{坏", encoding="utf-8")
        self.assertEqual(self._dump(self._identity()), self._dump(identity), "坏条目当作未命中，照常计算并重写")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["key"]["tree"] = "0" * 64
        payload["value"]["control_sha256"] = "f" * 64
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._identity()["control_sha256"], identity["control_sha256"], "键原文对不上的条目不得采用")

    def test_disabled_without_an_absolute_directory(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            for value in ("", "relative-memo"):
                with self.subTest(value), mock.patch.dict(os.environ, {tip.IDENTITY_MEMO_ENV: value}), contextlib.chdir(cwd):
                    self._identity()
                    tip.evaluator_dependency_digests(self.tree)
                    self.assertFalse((Path(cwd) / "relative-memo").exists())
        self.assertFalse(self.memo.exists(), "未启用时不写任何缓存")


if __name__ == "__main__":
    unittest.main()
