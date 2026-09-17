"""工具身份四层策略：全树可分类、层级漂移归类、编排器闭包静态可解析。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
