"""Claude 零请求 smoke（Claude 升级控制面设计 B5a-09）：全部步骤通过、网络守卫生效、收据只写一次、步骤失败如实报出，
模块只导入 Claude 身份覆盖的文件。"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade_zero_request_smoke as smoke

MODULE_FILE = Path(smoke.__file__).resolve()


class ClaudeZeroRequestSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()

    def test_smoke_passes_on_staging_fixture_and_blocks_network(self) -> None:
        receipt = smoke.run_smoke(self.base / "staging" / "smoke")
        self.assertEqual(receipt["status"], "passed", receipt["steps"])
        self.assertEqual((receipt["schema_version"], receipt["client"]), ("zero-request-smoke/v1", "claude"))
        self.assertEqual((receipt["network_attempts"], receipt["live_request_count"]), (0, 0))
        self.assertEqual([step["step"] for step in receipt["steps"]], [
            "provenance.fixture", "provenance.collect-root", "provenance.campaign-fixture", "provenance.collect-campaign",
            "root-cause.load-codes", "root-cause.client-dimensions", "ledger.close-campaign-ledger", "project-ledger.chain",
            "orchestrator.import-only",
        ])
        summary = receipt["summary"]
        self.assertEqual(summary["provenance_roots"]["auxiliary_by_category"], {"token_refresh": 1})
        self.assertEqual((summary["provenance_campaign"]["status"], summary["provenance_campaign"]["precise_total"]), ("complete", 2))
        self.assertEqual(summary["provenance_campaign"]["counting_rule"], "claude_model_requests/v1")
        self.assertEqual((summary["ledger_close"]["status"], summary["ledger_close"]["resulting_total"]), ("closed", 2))
        self.assertTrue(summary["root_cause"]["volatile_campaign_id_rejected"])
        self.assertTrue(all(summary["project_ledger"]["admitted"].values()))
        self.assertFalse(summary["project_ledger"]["blocked"])
        self.assertEqual(summary["orchestrator"]["module"], "tools.official_client_capture.claude_upgrade")
        self.assertTrue((self.base / "staging" / "smoke" / "project-ledger" / "upgrade-project-ledger-claude" / "plan.json").is_file())
        with smoke._NetworkGuard() as guard:
            with socket.socket() as guarded_socket:
                with self.assertRaises(smoke.SmokeError):
                    guarded_socket.connect(("127.0.0.1", 9))
        self.assertEqual(len(guard.attempts), 1)
        self.assertIs(socket.socket.connect, guard._original_connect, "网络守卫退出后必须恢复原 socket.connect")

    def test_smoke_rejects_non_staging_root_and_writes_receipt_once(self) -> None:
        with self.assertRaisesRegex(smoke.SmokeError, "staging"):
            smoke.run_smoke(self.base / "not-staging")
        output = self.base / "audit" / "zero-request-smoke-x.json"
        argv = ["--staging-root", str(self.base / "staging" / "s1"), "--output", str(output)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(smoke.main(argv), 0)
            receipt = json.loads(output.read_text("utf-8"))
            self.assertEqual((receipt["status"], receipt["client"]), ("passed", "claude"))
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            # 夹具根非空、收据已存在都拒绝，原收据不被改写。
            self.assertEqual(smoke.main(argv), 2)
            self.assertEqual(smoke.main(["--staging-root", str(self.base / "staging" / "s2"), "--output", str(output)]), 2)
        self.assertFalse((self.base / "staging" / "s2").exists())
        self.assertEqual(json.loads(output.read_text("utf-8")), receipt)

    def test_failed_step_is_reported_and_no_receipt_is_written(self) -> None:
        output = self.base / "audit" / "smoke.json"
        stderr = io.StringIO()
        with mock.patch.object(smoke.provenance, "collect_campaign_provenance", side_effect=smoke.provenance.ProvenanceError("夹具坏了")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(smoke.main(["--staging-root", str(self.base / "staging" / "s1"), "--output", str(output)]), 2)
        self.assertIn("provenance.collect-campaign", stderr.getvalue())
        self.assertFalse(output.exists())

    def test_imports_only_files_covered_by_claude_identity(self) -> None:
        """smoke 的变化与它依赖的文件变化都要让 Claude 的步骤记录失效：直接导入的受管模块不得被 Claude 身份策略忽略。"""

        policy = claude_identity.load_policy()
        self.assertEqual(claude_identity.classify_path(policy, "official_client_capture/" + MODULE_FILE.name), "control")
        tree = ast.parse(MODULE_FILE.read_text(encoding="utf-8"))
        managed = [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "tools.official_client_capture"
            for alias in node.names
        ]
        self.assertTrue(managed)
        for name in managed:
            with self.subTest(module=name):
                self.assertIn(claude_identity.classify_path(policy, f"official_client_capture/{name}.py"),
                              {"control", "evidence_semantics"})
        self.assertFalse([node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                          and str(node.module).startswith("tools.official_client_capture.tests")])


if __name__ == "__main__":
    unittest.main()
