"""Claude 资格读取索引的独立专项；不启动守卫、Docker、网络或 VC-6。

小型 v2 Campaign 由原生产器生成，首次和收尾都使用原 Store 验链。
测试覆盖重复引用的扫描次数、返回副本、原件变化拒绝，以及新旧
资格报告逐字一致；这里的通过只代表合成回归，不代表正式认证。
"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_control.canonical import canonical_json_bytes
from tools.official_client_control.contracts import campaign_identity_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import replay_receipt
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign


class _LegacyQualificationStoreView(executor._QualificationStoreView):
    """保留基线的三个事实读取方法，仅用于新旧报告比较。"""

    def load_campaign(self, campaign_id: str) -> dict:
        return self._store.load_campaign(campaign_id)

    def list_facts(self, campaign_id: str, dimension: str | None = None) -> list[dict]:
        if dimension == "deployment":
            return []
        return [fact for fact in self._store.list_facts(campaign_id, dimension)
                if fact["dimension"] != "deployment"]

    def load_fact(self, reference: dict) -> dict:
        if reference.get("dimension") == "deployment":
            raise guard.GuardError("验收子图不接受交付或生产激活事实")
        return self._store.load_fact(reference)

    def verify_fact_snapshots(self) -> None:
        pass


class QualificationReadSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="claude-qread-", dir="/private/tmp")
        cls.template_root = Path(cls.temporary.name)
        cls.tree = ManagedToolTree(cls.template_root / "repo")
        cls.active = cls.tree.active()
        cls.active.__enter__()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.addClassCleanup(cls.active.__exit__, None, None, None)
        campaign_root = cls.template_root / "campaign"
        campaign_root.mkdir(mode=0o700)
        cls.template = SyntheticCampaign(campaign_root, schema="v2")
        fixture = cls.template
        fixture.bootstrap_and_campaign()
        fixture.discovery_and_evidence()
        fixture.profile_objects()
        fixture.profile_approve()
        fixture.strict_validate()
        cls.qualification = {
            "campaign_id": fixture.campaign_id,
            "candidate_id": "synthetic-candidate-strict",
            "candidate_ref": fixture.references["candidate-strict"],
            "build_receipt_ref": fixture.references["candidate-build-receipt"],
            "validation_ref": fixture.references["validation-completed"],
            "acceptance_ref": fixture.references["acceptance-strict"],
        }

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-qread-case-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(self.template.root, self.root)
        self.writer = ControlStore(self.root / "store")
        self.reader = ControlStore.open_read_only(self.writer.root)
        self.campaign_id = self.template.campaign_id
        self.view = executor._QualificationStoreView(self.reader)
        self.fixture = copy.copy(self.template)
        self.fixture.references = copy.deepcopy(self.template.references)
        self.fixture.root = self.root
        self.fixture.store_root = self.writer.root
        self.fixture.store = self.writer
        self.fixture.external_root = self.root / "external"

    def fact_path(self, fact: dict) -> Path:
        return (self.writer.root / "facts" / fact["campaign_id"] / fact["dimension"]
                / f"{fact['sequence']:08d}-{fact['_sha256']}.json")

    def append_observation(self) -> dict:
        payload = self.reader.load_fact(self.fixture.references["validation-selector-after"])["payload"]
        return self.writer.append_fact(self.campaign_id, "selector_observed", payload,
                                       self.fixture._time())

    def replay(self) -> dict:
        return executor.replay_qualification_graph(
            self.reader, self.qualification, self.fixture.external_root, self.root)

    def test_writer_is_rejected(self) -> None:
        with self.assertRaisesRegex(guard.GuardError, "只读"):
            executor._QualificationStoreView(self.writer)

    def test_repeated_references_use_one_initial_and_one_final_full_scan(self) -> None:
        with mock.patch.object(self.reader, "list_facts", wraps=self.reader.list_facts) as scans:
            facts = self.view.list_facts(self.campaign_id)
            for _ in range(8):
                for fact in facts:
                    self.assertEqual(self.view.load_fact(self.reader.fact_ref(fact)), fact)
                self.assertEqual(self.view.list_facts(self.campaign_id, "validation"),
                                 [fact for fact in facts if fact["dimension"] == "validation"])
            self.assertEqual(scans.call_args_list, [mock.call(self.campaign_id)])
            self.view.verify_fact_snapshots()
            self.assertEqual(scans.call_args_list,
                             [mock.call(self.campaign_id), mock.call(self.campaign_id)])

    def test_reference_schema_missing_fact_and_wrong_digest_are_rejected(self) -> None:
        reference = self.qualification["candidate_ref"]
        invalid = [None, [], "fact", {}, {**reference, "extra": True},
                   {key: value for key, value in reference.items() if key != "sha256"},
                   {**reference, "sequence": True}, {**reference, "sequence": 0},
                   {**reference, "dimension": "unknown"},
                   {**reference, "campaign_id": "../outside"},
                   {**reference, "sha256": "invalid"}]
        for value in invalid:
            with self.subTest(reference=value), self.assertRaises(ControlError):
                self.view.load_fact(value)
        with self.assertRaisesRegex(ControlError, "不存在"):
            self.view.load_fact({**reference, "sequence": 9999})
        with self.assertRaisesRegex(ControlError, "摘要不匹配"):
            self.view.load_fact({**reference, "sha256": "f" * 64})
        with self.assertRaisesRegex(ControlError, "维度非法"):
            self.view.list_facts(self.campaign_id, "unknown")

    def test_returned_facts_and_campaigns_do_not_mutate_the_view(self) -> None:
        expected = self.view.load_fact(self.qualification["candidate_ref"])
        returned = self.view.load_fact(self.qualification["candidate_ref"])
        returned["payload"]["candidate_id"] = "caller-mutation"
        returned["formed_under"]["candidate_revision"] = 999
        self.assertEqual(self.view.load_fact(self.qualification["candidate_ref"]), expected)
        listed = self.view.list_facts(self.campaign_id)
        listed[0]["payload"].clear()
        listed.clear()
        self.assertEqual(self.view.load_fact(self.qualification["candidate_ref"]), expected)
        campaign = self.view.load_campaign(self.campaign_id)
        campaign["tool_identity"].clear()
        self.assertTrue(self.view.load_campaign(self.campaign_id)["tool_identity"])
        self.view.verify_fact_snapshots()

    def test_initial_read_refuses_gaps_corruption_modes_and_symlinks(self) -> None:
        fact = self.reader.list_facts(self.campaign_id, "validation")[0]
        path = self.fact_path(fact)
        original = path.read_bytes()
        for mutation in ("gap", "bytes", "mode", "symlink"):
            with self.subTest(mutation=mutation):
                if mutation == "gap":
                    path.unlink()
                elif mutation == "bytes":
                    path.write_bytes(original + b" ")
                elif mutation == "mode":
                    path.chmod(0o644)
                else:
                    target = self.root / "fact-target.json"
                    target.write_bytes(original)
                    path.unlink()
                    path.symlink_to(target)
                try:
                    with self.assertRaises(ControlError):
                        executor._QualificationStoreView(self.reader).list_facts(self.campaign_id)
                finally:
                    path.unlink(missing_ok=True)
                    path.write_bytes(original)
                    path.chmod(0o600)

    def test_final_read_refuses_a_valid_fact_append(self) -> None:
        original = self.view.list_facts(self.campaign_id)
        added = self.append_observation()
        self.assertEqual(len(self.reader.list_facts(self.campaign_id)), len(original) + 1)
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()
        # 新操作必须从新图重新完整读验，不能沿用上一视图的索引。
        fresh = executor._QualificationStoreView(self.reader)
        self.assertEqual(fresh.load_fact(added), self.reader.load_fact(added))
        self.assertEqual(len(fresh.list_facts(self.campaign_id)), len(original) + 1)
        fresh.verify_fact_snapshots()

    def test_final_read_refuses_fact_deletion(self) -> None:
        facts = self.view.list_facts(self.campaign_id, "runtime_selector")
        self.fact_path(facts[-1]).unlink()
        self.reader.list_facts(self.campaign_id)
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()

    def test_final_read_refuses_same_bytes_at_replaced_file_coordinate(self) -> None:
        fact = self.view.list_facts(self.campaign_id)[0]
        path = self.fact_path(fact)
        original = path.read_bytes()
        before = path.stat()
        replacement = self.root / "replacement.json"
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assertNotEqual(before.st_ino, path.stat().st_ino)
        self.assertEqual(self.reader.list_facts(self.campaign_id), self.view.list_facts(self.campaign_id))
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()

    def test_final_read_refuses_allowed_readonly_permission_drift(self) -> None:
        fact = self.view.list_facts(self.campaign_id)[0]
        self.fact_path(fact).chmod(0o400)
        self.reader.list_facts(self.campaign_id)
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()

    def test_final_read_refuses_timestamp_drift(self) -> None:
        fact = self.view.list_facts(self.campaign_id)[0]
        path = self.fact_path(fact)
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.reader.list_facts(self.campaign_id)
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()

    def test_final_read_rechecks_real_bytes_and_rejects_corruption(self) -> None:
        fact = self.view.list_facts(self.campaign_id)[0]
        path = self.fact_path(fact)
        before = path.stat()
        content = path.read_bytes()
        path.write_bytes(content.replace(b"2026", b"2025", 1))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaisesRegex(ControlError, "事实摘要漂移"):
            self.view.verify_fact_snapshots()

    def test_campaign_raw_whitespace_change_is_rejected_without_fact_reads(self) -> None:
        expected = self.view.load_campaign(self.campaign_id)
        path = self.writer.root / "campaigns" / f"{self.campaign_id}.json"
        path.write_bytes(path.read_bytes() + b"\n ")
        self.assertEqual(self.reader.load_campaign(self.campaign_id), expected)
        with self.assertRaisesRegex(guard.GuardError, "Campaign 发生变化"):
            self.view.verify_fact_snapshots()

    def test_campaign_valid_identity_change_is_rejected(self) -> None:
        document = self.view.load_campaign(self.campaign_id)
        document["oauth_org_id"] = "another-synthetic-org"
        document["identity_sha256"] = campaign_identity_sha256(document)
        path = self.writer.root / "campaigns" / f"{self.campaign_id}.json"
        path.write_bytes(canonical_json_bytes(document))
        self.reader.load_campaign(self.campaign_id)
        with self.assertRaisesRegex(guard.GuardError, "Campaign 发生变化"):
            self.view.verify_fact_snapshots()

    def test_campaign_allowed_permission_change_is_rejected(self) -> None:
        self.view.load_campaign(self.campaign_id)
        path = self.writer.root / "campaigns" / f"{self.campaign_id}.json"
        path.chmod(0o400)
        self.reader.load_campaign(self.campaign_id)
        with self.assertRaisesRegex(guard.GuardError, "Campaign 发生变化"):
            self.view.verify_fact_snapshots()

    def test_deployment_append_is_conservatively_rejected_without_entering_subgraph(self) -> None:
        # 原生产器先生成可晋升的历史候选，再追加有效 deployment 事实。
        # 这样无需启动 VC-6，也没有绕过严格候选的交付收据准入。
        self.fixture.candidate_and_accept()
        facts = self.view.list_facts(self.campaign_id)
        self.fixture.promote()
        deployment = self.reader.list_facts(self.campaign_id, "deployment")
        self.assertTrue(deployment)
        self.assertEqual(self.view.list_facts(self.campaign_id), facts)
        self.assertEqual(self.view.list_facts(self.campaign_id, "deployment"), [])
        with self.assertRaisesRegex(guard.GuardError, "交付或生产激活事实"):
            self.view.load_fact(self.reader.fact_ref(deployment[0]))
        with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
            self.view.verify_fact_snapshots()

    def test_delivery_objects_receipts_and_typed_evidence_remain_forbidden(self) -> None:
        source = mock.Mock(read_only=True, root=self.root)
        source.load_object.return_value = {"payload": {
            "schema_version": "official-client-operational-evidence/v2"}}
        view = executor._QualificationStoreView(source)
        with self.assertRaisesRegex(guard.GuardError, "真实交付执行证据"):
            view.load_object({"object_kind": "operational_evidence", "sha256": "a" * 64})
        source.load_object.assert_called_once()
        source.load_object.reset_mock()
        with self.assertRaises(guard.GuardError):
            view.load_object({"object_kind": "candidate_delivery_plan", "sha256": "a" * 64})
        source.load_object.assert_not_called()
        with self.assertRaises(guard.GuardError):
            view.load_receipt({"receipt_kind": "candidate_delivery", "sha256": "a" * 64})
        source.load_receipt.assert_not_called()
        self.assertEqual(view.list_object_refs("candidate_delivery_plan"), [])
        self.assertEqual(view.list_receipt_refs("candidate_delivery"), [])

    def test_objects_receipts_and_frozen_identity_still_use_original_readers(self) -> None:
        object_ref = self.fixture.references["bootstrap"]
        receipt_ref = self.qualification["build_receipt_ref"]
        with mock.patch.object(self.reader, "load_object", wraps=self.reader.load_object) as objects, \
             mock.patch.object(self.reader, "object_bytes", wraps=self.reader.object_bytes) as raw, \
             mock.patch.object(self.reader, "load_receipt", wraps=self.reader.load_receipt) as receipts:
            self.assertEqual(self.view.load_object(object_ref), self.writer.load_object(object_ref))
            self.assertEqual(self.view.object_bytes(object_ref), self.writer.object_bytes(object_ref))
            self.assertEqual(self.view.load_receipt(receipt_ref), self.writer.load_receipt(receipt_ref))
            objects.assert_called()
            raw.assert_called_once_with(object_ref)
            receipts.assert_called_once_with(receipt_ref)
        replay_receipt(self.view, receipt_ref)
        self.view.verify_fact_snapshots()

    def test_new_and_legacy_qualification_reports_are_identical(self) -> None:
        with mock.patch.object(guard, "GuardClient", side_effect=AssertionError("禁止启动守卫")):
            current = self.replay()
            with mock.patch.object(executor, "_QualificationStoreView", _LegacyQualificationStoreView):
                previous = self.replay()
        self.assertEqual(canonical_json_bytes(current), canonical_json_bytes(previous))
        self.assertEqual(current["result"], "pass")

    def test_frozen_identity_replays_after_current_tool_tree_changes(self) -> None:
        before = self.replay()
        key = "official_client_control/store.py"
        self.tree.write(key, self.tree.path(key).read_bytes() + "\n# 合成工具演进\n".encode("utf-8"))
        try:
            self.assertEqual(canonical_json_bytes(self.replay()), canonical_json_bytes(before))
        finally:
            self.tree.restore()

    def test_original_external_binding_verification_remains_active(self) -> None:
        binding = self.fixture.official_binding
        path = self.fixture.external_root / binding["path"]
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaises(ControlError):
            self.replay()

    def test_graph_cannot_succeed_if_fact_changes_after_semantic_replay(self) -> None:
        original = WorkflowGates.replay_campaign

        def replay_then_change(gates: WorkflowGates, campaign: dict, facts: list[dict]) -> object:
            result = original(gates, campaign, facts)
            self.fact_path(facts[0]).chmod(0o400)
            return result

        with mock.patch.object(WorkflowGates, "replay_campaign", replay_then_change):
            with self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
                self.replay()


if __name__ == "__main__":
    unittest.main()
