"""typed VC-6 新世界的门禁环境合同专项，不运行完整批次链。

离线合成事实经原环境与门禁 finalizer 封存，再用父进程原 replay 和
独立 -I -B -S worker 核全文及缺件拒绝。默认旧夹具行为另行保留。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as client_contract
from tools.official_client_capture import claude_gate_material_worker as worker
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_gate_receipt as gate
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests.test_claude_sealed_gate_materials import view_spec
from tools.official_client_control import delivery_execution


REPOSITORY = Path(__file__).resolve().parents[3]


class GateEnvironmentFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = worker.compute_gate_reader_identity(REPOSITORY, formed_context={"fixture_only": True})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vc5-real-environment-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.gate_root = self.root / "gate"
        self.config = chain._write_json(self.root / "seams.json", {"gate_environment_mode": chain.REAL_GATE_ENVIRONMENT})
        self.subject = {
            "campaign_id": "claude-gate-environment-fixture", "campaign_mode": "formal",
            "campaign_purpose": "validation_only", "candidate_id": "candidate-current",
            "candidate_purpose": "validation_only", "target_version": "2.1.285",
            "target_architecture": "linux/arm64", "profile_id": "claude-code-fixture",
            "profile_digest": "1" * 64, "candidate_package_digest": "2" * 64,
            "candidate_source_tree_sha256": "3" * 64, "candidate_image_id": "sha256:" + "4" * 64,
            "candidate_image_reference": "fixture/sub2api@sha256:" + "4" * 64,
            "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None,
        }

    def tearDown(self):
        self.assertEqual(client_contract.SOURCE_ADAPTERS, {})
        self.assertEqual(delivery.ENVIRONMENT_EXECUTORS, {})
        self.assertEqual(delivery_execution.SOURCE_VERIFIERS, {})

    def create_gate(self, *, environment_mode=chain.REAL_GATE_ENVIRONMENT):
        return chain.gate_receipt_file(self.gate_root, name="candidate-receipt.json", subject=self.subject,
            host="ARM64", architecture="linux/arm64", environment_mode=environment_mode)

    def independent_replay(self, receipt):
        files, directories, missing = view_spec(self.reader, self.gate_root, self.gate_root)
        return worker.run_gate_material_reader(self.reader, files, directories, missing,
            str(self.gate_root), receipt, view="original", expected_subject=self.subject)

    def environment_receipt(self, receipt, role):
        document = json.loads((self.gate_root / receipt).read_text())
        return self.gate_root / document["environment"][role]["path"]

    def assert_both_replays_reject(self, receipt):
        with self.assertRaises(gate.GateReceiptError):
            gate.replay(self.gate_root, receipt)
        with self.assertRaises(worker.ReaderError):
            self.independent_replay(receipt)

    def test_real_mode_keeps_original_readers_and_matches_isolated_worker_full_result(self):
        original_replay, original_equivalence = arm.replay, arm.receipts_equivalent
        with chain.seams(self.config):
            self.assertIs(arm.replay, original_replay)
            self.assertIs(arm.receipts_equivalent, original_equivalence)
            receipt = self.create_gate()
            parent = gate.replay(self.gate_root, receipt)
        isolated = self.independent_replay(receipt)
        self.assertEqual(isolated["result"], parent)
        self.assertTrue(any(path.endswith("-before-facts.json") for path in isolated["reader_readset"]))
        for role, phase in (("before", "gate_before"), ("after", "gate_after")):
            path = self.environment_receipt(receipt, role)
            result = arm.replay(self.gate_root, path.relative_to(self.gate_root).as_posix())
            self.assertEqual((result["status"], result["phase"], result["subject_id"]),
                ("passed", phase, "gate-candidate-receipt"))
            self.assertTrue((self.gate_root / result["facts"]["path"]).is_file())

    def test_missing_environment_facts_rejects_in_both_original_readers(self):
        receipt = self.create_gate()
        environment = json.loads(self.environment_receipt(receipt, "before").read_text())
        (self.gate_root / environment["facts"]["path"]).unlink()
        self.assert_both_replays_reject(receipt)

    def test_action_subprocess_installs_fixture_without_environment_replay_hooks(self):
        receipt = self.create_gate()
        expected = gate.replay(self.gate_root, receipt)
        action_id = "gate-environment-probe"
        environment = chain.subprocess_environment(self.root / "inject", self.config, [action_id])
        environment[chain.supervisor.CAMPAIGN_RUN_ACTION_ID_ENV] = action_id
        script = '''
import inspect, json, sys
from pathlib import Path
from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
from tools.official_client_capture import codex_upgrade_gate_receipt as gate
assert contract.SOURCE_ADAPTERS, "动作输入源接缝没有安装"
assert inspect.isfunction(arm.replay) and inspect.isfunction(arm.receipts_equivalent), "原环境函数被替换"
assert Path(inspect.getsourcefile(arm.replay)).resolve() == Path(arm.__file__).resolve()
assert Path(inspect.getsourcefile(arm.receipts_equivalent)).resolve() == Path(arm.__file__).resolve()
sys.stdout.write(json.dumps(gate.replay(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, sort_keys=True))
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.gate_root), receipt],
            env={**os.environ, **environment}, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_corrupted_environment_receipt_rejects_in_both_original_readers(self):
        receipt = self.create_gate()
        path = self.environment_receipt(receipt, "before")
        document = json.loads(path.read_text())
        document["schema_version"] = "invalid-environment/v1"
        chain._write_json(path, document)
        self.assert_both_replays_reject(receipt)

    def test_bad_schema_before_gate_formation_is_rejected_without_environment_reader_hooks(self):
        original_create = chain.create_arm_receipt

        def damaged_environment(*args, **kwargs):
            path = original_create(*args, **kwargs)
            document = json.loads(path.read_text())
            document["schema_version"] = "invalid-environment/v1"
            chain._write_json(path, document)
            return path

        # 只注入新事实源的损坏；原环境 reader 和门禁 finalizer 都不替换。
        with chain.seams(self.config), mock.patch.object(chain, "create_arm_receipt", side_effect=damaged_environment):
            with self.assertRaisesRegex(gate.GateReceiptError, "receipt.schema_version"):
                self.create_gate()
        self.assertFalse((self.gate_root / "candidate-receipt.json").exists())

    def test_default_legacy_fixture_keeps_minimal_payload_and_parent_seams(self):
        chain._write_json(self.config, {})
        self.assertEqual(chain.gate_environment_mode({}), chain.LEGACY_GATE_ENVIRONMENT)
        original_replay, original_equivalence = arm.replay, arm.receipts_equivalent
        with chain.seams(self.config):
            self.assertIsNot(arm.replay, original_replay)
            self.assertIsNot(arm.receipts_equivalent, original_equivalence)
            receipt = chain.gate_receipt_file(self.gate_root, name="candidate-receipt.json", subject=self.subject,
                host="ARM64", architecture="linux/arm64")
            self.assertEqual(gate.replay(self.gate_root, receipt)["status"], "passed")
            for role in ("before", "after"):
                self.assertEqual(json.loads(self.environment_receipt(receipt, role).read_text()),
                    {"attempt_id": "gate-candidate-receipt", "role": role})
        self.assertIs(arm.replay, original_replay)
        self.assertIs(arm.receipts_equivalent, original_equivalence)
        with self.assertRaisesRegex(worker.ReaderError, "receipt.schema_version"):
            self.independent_replay(receipt)

    def test_unknown_mode_rejects_before_new_gate_files_are_written(self):
        with self.assertRaisesRegex(ValueError, "门禁环境夹具模式"):
            chain.patchers({"gate_environment_mode": "real_receipt_typo"})
        with self.assertRaisesRegex(ValueError, "门禁环境夹具模式"):
            self.create_gate(environment_mode="real_receipt_typo")
        self.assertFalse(self.gate_root.exists())


if __name__ == "__main__":
    unittest.main()
