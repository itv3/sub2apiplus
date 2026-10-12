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

        # 完整重放只复用快照机制，必须继续看见全部 deployment；资格视图边界不变。
        from tools.official_client_capture.tests import claude_fault_chain_world as world
        complete = self.reader.list_facts(self.campaign_id)
        reference = self.reader.fact_ref(deployment[0])
        for _ in range(2):
            with mock.patch.object(self.reader, "list_facts", wraps=self.reader.list_facts) as scans:
                with world._read_only_fact_index(self.reader) as indexed:
                    self.assertEqual(indexed.list_facts(self.campaign_id), complete)
                    self.assertEqual(indexed.list_facts(self.campaign_id, "deployment"), deployment)
                    returned = indexed.load_fact(reference)
                    self.assertEqual(returned, deployment[0])
                    returned["payload"].clear()
                    self.assertEqual(indexed.load_fact(reference), deployment[0])
                    self.assertEqual(scans.call_args_list, [mock.call(self.campaign_id)])
                    indexed.verify_fact_snapshots()
                    self.assertEqual(scans.call_args_list, [mock.call(self.campaign_id)] * 2)
                with self.assertRaisesRegex(ValueError, "已退出"):
                    indexed.load_fact(reference)

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

        # 沿同一 UID 加入当前夹具读侧接线。只生成小型文件现场配置，不启动守卫／VC-6。
        # 原交接外再消费两次真实资格报告，测同一纯读方法内另开只读句柄的实际扫描。
        import contextlib
        from contextvars import copy_context
        from tools.official_client_capture import claude_upgrade_delivery as delivery
        from tools.official_client_capture.tests import claude_typed_vc6_fixture as typed
        from tools.official_client_capture.tests import claude_fault_chain_world as world
        from tools.official_client_capture.tests.test_claude_real_delivery_integration import QualifiedFileRuntime
        from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
        from tools.official_client_control import delivery_execution as execution, qualification_bindings

        configuration, context, _environment = fixture_world(self.root)
        qualification = {**self.qualification, "store_relative": "store", "external_root_relative": "external",
            "sealed_result_binding": configuration["candidate"]["qualification"]["sealed_result_binding"]}
        configuration["candidate"]["qualification"] = copy.deepcopy(qualification)
        # 此短验不消费回退资格；保留原文件现场的独立回退输入，仅对齐同根路径。
        configuration["rollback"]["qualification"].update(store_relative="store", external_root_relative="external")
        configuration_path = self.root / "inputs/scoped-configuration.json"
        guard.write_json_once(configuration_path, configuration)
        context.update(campaign_id=self.campaign_id, candidate_id=qualification["candidate_id"],
                       data_root=str(self.root), configuration_ref=guard.binding(self.root, configuration_path))
        control_path = self.root / "scoped-control.json"
        guard.write_json_once(control_path, {"schema_version": typed.CONTROL_SCHEMA, "fixture_only": True,
            "formal_version_semantics_certified": False, "context": context,
            "calls": str(self.root / "unused-calls.jsonl"), "physical_calls": str(self.root / "unused-physical.jsonl"),
            "failures": [], "kill_at": None, "crash_at": None, "failed_gates": []})
        campaign = self.writer.load_campaign(self.campaign_id)
        original_handoff, original_view = delivery._vc5_handoff, executor._QualificationStoreView
        original_list, original_index = ControlStore.list_facts, world._read_only_fact_index
        original_init, original_verify = original_view.__init__, original_view.verify_fact_snapshots
        original_runtime_check = QualifiedFileRuntime.check_qualification
        scans, views, indexes, contexts, comparisons, late_change = [], [], [], [], [], [None]
        other_path = self.root / "other-store"
        shutil.copytree(self.writer.root, other_path)
        other_reader = ControlStore.open_read_only(other_path)
        expected_fact = self.writer.load_fact(qualification["candidate_ref"])
        use_other, other_views = [False], []

        def scan(store, campaign_id, dimension=None):
            scans.append((store, campaign_id, dimension))
            return original_list(store, campaign_id, dimension)

        def remember_view(view, store):
            original_init(view, store)
            views.append(view)

        def verify(view):
            # 资格子视图的尾验继续执行；只在外层原 Store 的最终复核前注入真实漂移。
            if late_change[0] is not None and type(view._store) is ControlStore:
                mutate, late_change[0] = late_change[0], None
                mutate()
            return original_verify(view)

        @contextlib.contextmanager
        def observe_index(reader):
            with original_index(reader) as indexed:
                indexes.append(indexed)
                yield indexed

        def consume(store, observed_campaign, candidate_id):
            result = original_handoff(store, observed_campaign, candidate_id)
            for _ in range(2):
                qualification_bindings.validate_replay_report(current, qualification, store,
                    external_root=self.fixture.external_root, trusted_data_root=self.root)
            if use_other[0]:
                unrelated = executor._QualificationStoreView(other_reader)
                self.assertIs(unrelated._store, other_reader)
                self.assertEqual(unrelated.load_fact(qualification["candidate_ref"]), expected_fact)
                unrelated.verify_fact_snapshots()
                other_views.append(unrelated)
            contexts.append((copy_context(), executor._QualificationStoreView))
            return result

        with mock.patch.object(ControlStore, "list_facts", new=scan), \
                mock.patch.object(original_view, "__init__", new=remember_view), \
                mock.patch.object(original_view, "verify_fact_snapshots", new=verify), \
                mock.patch.object(world, "_read_only_fact_index", new=observe_index), \
                mock.patch.object(delivery, "_vc5_handoff", new=consume), \
                mock.patch.object(guard.GuardClient, "call", side_effect=AssertionError("短验禁止守卫 RPC")):
            baseline = consume(self.writer, campaign, qualification["candidate_id"])
            baseline_scans = len(scans)
            self.assertFalse(indexes)
            with typed.source_scope(control_path):
                self.assertIs(QualifiedFileRuntime.check_qualification, original_runtime_check)
                self.assertIs(typed.ControlledFileRuntime.check_qualification, original_runtime_check)
                for _ in range(2):
                    scans.clear()
                    begin = len(views)
                    self.assertEqual(delivery._vc5_handoff(self.writer, campaign, qualification["candidate_id"]), baseline)
                    self.assertEqual([(campaign_id, dimension) for _store, campaign_id, dimension in scans],
                                     [(self.campaign_id, None)] * 2)
                    self.assertLess(len(scans), baseline_scans)
                    comparisons.append({"原完整扫描": baseline_scans, "同次复用完整扫描": len(scans)})
                    scoped_views = [view for view in views[begin:] if view._store is indexes[-1]]
                    self.assertEqual(len(scoped_views), 2)
                    self.assertFalse(indexes[-1].active)
                    for view in scoped_views:
                        with self.assertRaisesRegex(ValueError, "已退出"):
                            view.list_facts(self.campaign_id)
                    copied, view_class = contexts[-1]
                    with self.assertRaisesRegex(ValueError, "已退出"):
                        copied.run(view_class, self.reader)
                    self.assertIsNone(execution._SOURCE_BATCH.get())
                    self.assertIsNone(execution._QUALIFICATION_READ_BATCH.get())
                self.assertIsNot(indexes[-1], indexes[-2])

                # 同一方法内另一个真实根也不得借当前索引；保留它独立的完整首读／尾验。
                scans.clear()
                use_other[0] = True
                try:
                    self.assertEqual(delivery._vc5_handoff(self.writer, campaign, qualification["candidate_id"]), baseline)
                finally:
                    use_other[0] = False
                self.assertIsNone(other_views[-1]._scoped_index)
                self.assertEqual(sum(store.root == other_path for store, _campaign, _dimension in scans), 2)
                self.assertEqual(sum(store.root == self.writer.root for store, _campaign, _dimension in scans), 2)

                # 只读输入和派生句柄仍按原接线，不把当前索引权限推广到其它消费者。
                class DerivedStore(ControlStore):
                    pass
                for store in (self.reader, DerivedStore(self.writer.root)):
                    scans.clear()
                    count = len(indexes)
                    self.assertEqual(delivery._vc5_handoff(store, campaign, qualification["candidate_id"]), baseline)
                    self.assertEqual(len(indexes), count)
                    self.assertEqual(len(scans), baseline_scans)
                with execution._qualification_read_batch(self.writer, current_execution=False):
                    scans.clear()
                    count = len(indexes)
                    self.assertEqual(delivery._vc5_handoff(self.writer, campaign, qualification["candidate_id"]), baseline)
                    self.assertEqual(len(indexes), count)
                    self.assertEqual(len(scans), baseline_scans)


                # 原完整 replay 自建索引也要覆盖；有同根父索引时，嵌套只登记原工厂的子索引。
                expected_replay = self.reader.replay(external_root=self.fixture.external_root, require_external=True)
                scans.clear()
                with world._read_only_fact_index(self.reader) as parent_index:
                    self.assertEqual(world.replay_read_only_store(parent_index,
                        external_root=self.fixture.external_root), expected_replay)
                    parent_index.verify_fact_snapshots()
                self.assertEqual(len(scans), 2)
                self.assertFalse(parent_index.active)

                # 来源批次是精确作用域对象；资格收口 active=False 时来源批次仍须活着且同一索引。
                scans.clear()
                with world._read_only_fact_index(self.reader) as indexed:
                    source_batch = execution._SourceBatch(indexed, {}, {})
                    source_token = execution._SOURCE_BATCH.set(source_batch)
                    try:
                        with execution._qualification_read_batch(indexed, current_execution=False):
                            execution._QUALIFICATION_READ_BATCH.get().active = False
                            for _ in range(2):
                                qualification_bindings.validate_replay_report(current, qualification, indexed,
                                    external_root=self.fixture.external_root, trusted_data_root=self.root)
                                self.assertIs(views[-1]._store, indexed)
                        indexed.verify_fact_snapshots()
                    finally:
                        source_batch.active = False
                        execution._SOURCE_BATCH.reset(source_token)
                self.assertEqual(len(scans), 2)
                self.assertFalse(indexed.active)
                with self.assertRaisesRegex(ValueError, "已退出"):
                    views[-1].list_facts(self.campaign_id)

                # 同根但不属于当前登记索引的批次不能借用；每份报告保留独立完整首读／尾验。
                scans.clear()
                with world._read_only_fact_index(self.reader) as indexed:
                    source_batch = execution._SourceBatch(self.reader, {}, {})
                    source_token = execution._SOURCE_BATCH.set(source_batch)
                    try:
                        qualification_bindings.validate_replay_report(current, qualification, indexed,
                            external_root=self.fixture.external_root, trusted_data_root=self.root)
                        self.assertIsNot(views[-1]._store, indexed)
                        indexed.verify_fact_snapshots()
                    finally:
                        source_batch.active = False
                        execution._SOURCE_BATCH.reset(source_token)
                self.assertEqual(len(scans), 2)
                self.assertFalse(indexed.active)

                # 包入口必须调用原拒绝并销毁索引；成功包验收仍由原 typed 组合 UID 承担。
                count = len(indexes)
                with self.assertRaisesRegex(ControlError, "Package v2"):
                    execution.validate_delivery_package(self.writer, {"schema_version": "invalid"})
                self.assertEqual(len(indexes), count + 1)
                self.assertFalse(indexes[-1].active)

                reference = qualification["candidate_ref"]
                fact_path = self.fact_path(self.writer.load_fact(reference))
                campaign_path = self.writer.root / "campaigns" / (self.campaign_id + ".json")
                fact_raw, campaign_raw = fact_path.read_bytes(), campaign_path.read_bytes()
                for label, mutate in (("事实原字节", lambda: fact_path.write_bytes(fact_raw + b" ")),
                                      ("事实权限", lambda: fact_path.chmod(0o400)),
                                      ("Campaign 原字节", lambda: campaign_path.write_bytes(campaign_raw + b" "))):
                    with self.subTest(late_drift=label):
                        late_change[0] = mutate
                        try:
                            with self.assertRaises((ControlError, guard.GuardError)):
                                delivery._vc5_handoff(self.writer, campaign, qualification["candidate_id"])
                            self.assertIsNone(late_change[0])
                            self.assertFalse(indexes[-1].active)
                        finally:
                            late_change[0] = None
                            fact_path.chmod(0o600)
                            fact_path.write_bytes(fact_raw)
                            campaign_path.write_bytes(campaign_raw)
                scans.clear()
                self.assertEqual(delivery._vc5_handoff(self.writer, campaign, qualification["candidate_id"]), baseline)
                self.assertEqual(len(scans), 2)
                self.assertFalse(indexes[-1].active)
            self.assertIs(executor._QualificationStoreView, original_view)
        self.assertFalse(self.writer.read_only)
        self.assertEqual(self.writer._file_modes, (0o600,))
        self.assertFalse((self.root / "unused-calls.jsonl").exists())
        self.assertFalse((self.root / "unused-physical.jsonl").exists())
        import json
        print(json.dumps({"同 UID 当前客户端只读扫描对照": comparisons}, ensure_ascii=False), flush=True)

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

        # 完整 Store 仍跑原语义重放；结束后的合法追加或只读权限变化均不得返回成功。
        from tools.official_client_capture.tests import claude_fault_chain_world as world
        first = self.reader.list_facts(self.campaign_id)[0]
        path = self.fact_path(first)
        path.chmod(0o600)
        for mutation in ("mode", "append"):
            changed = False

            def full_replay_then_change(gates: WorkflowGates, campaign: dict, facts: list[dict]) -> object:
                nonlocal changed
                result = original(gates, campaign, facts)
                if not changed:
                    changed = True
                    if mutation == "mode":
                        path.chmod(0o400)
                    else:
                        self.append_observation()
                return result

            with self.subTest(mutation=mutation), \
                    mock.patch.object(WorkflowGates, "replay_campaign", full_replay_then_change), \
                    self.assertRaisesRegex(guard.GuardError, "事实链发生变化"):
                world.replay_read_only_store(self.reader, external_root=self.fixture.external_root)
            self.assertTrue(changed, "必须已经经过真实语义重放才注入变化")
            if mutation == "mode":
                path.chmod(0o600)


class QualificationSubjectReadSnapshotTests(unittest.TestCase):
    """在真实 subject 消费入口核对索引接线，不只验证视图自身的方法。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="claude-subject-read-", dir="/private/tmp")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.tree = ManagedToolTree(Path(cls.temporary.name) / "repo")
        repository = Path(__file__).resolve().parents[3]
        shutil.copytree(repository / "tools/ci", cls.tree.root / "tools/ci",
                        ignore=shutil.ignore_patterns("__pycache__"))
        cls.active = cls.tree.active()
        cls.active.__enter__()
        cls.addClassCleanup(cls.active.__exit__, None, None, None)

    def setUp(self) -> None:
        from tools.official_client_capture.tests.test_claude_sealed_gate_materials import MaterialImportProofTests

        # 只复用生产世界，不运行改为 policy12 的类初始化，也不启动交付。
        self.helper = MaterialImportProofTests()
        self.helper.tree = self.tree
        self.addCleanup(self.helper.doCleanups)
        self.helper.setUp()
        self.reader = self.helper.store
        self.fixture = self.helper.fixture
        self.campaign_id = self.fixture.campaign_id

    def subject(self, **arguments):
        return self.helper.subject(**arguments)

    def test_consumer_returns_identical_subject_bindings_and_projections(self) -> None:
        from tools.official_client_capture import claude_qualification_material_source as material

        class LegacySubjectStoreView(_LegacyQualificationStoreView):
            """保留旧 consumer 的原 Store 读取，仅用于逐对象结果比较。"""

            def object_path(self, reference):
                return self._store.object_path(reference)

            def receipt_path(self, reference):
                return self._store.receipt_path(reference)

        bindings, projections = [], []
        current = self.subject(read_bindings=bindings, read_projections=projections)
        previous_bindings, previous_projections = [], []
        with mock.patch.object(material, "_qualification_subject_store", LegacySubjectStoreView):
            previous = self.subject(read_bindings=previous_bindings,
                                    read_projections=previous_projections)
        self.assertEqual(canonical_json_bytes(current), canonical_json_bytes(previous))
        self.assertEqual(bindings, previous_bindings)
        self.assertEqual(projections, previous_projections)
        self.assertTrue(bindings)
        self.assertTrue(projections)

    def test_consumer_performs_initial_and_final_full_scans_each_call(self) -> None:
        with mock.patch.object(self.reader, "list_facts", wraps=self.reader.list_facts) as scans:
            first = self.subject()
            self.assertEqual(scans.call_args_list,
                             [mock.call(self.campaign_id), mock.call(self.campaign_id)])
            self.assertEqual(self.subject(), first)
            self.assertEqual(scans.call_args_list,
                             [mock.call(self.campaign_id)] * 4)

    def test_writable_input_uses_a_new_readonly_handle_and_keeps_original(self) -> None:
        from tools.official_client_capture import claude_qualification_material_source as material

        writer = self.fixture.store
        expected = self.subject()
        self.helper.store = writer
        with mock.patch.object(ControlStore, "open_read_only", wraps=ControlStore.open_read_only) as opens:
            self.assertEqual(self.subject(), expected)
            opens.assert_called_once_with(writer.root)
        self.assertFalse(writer.read_only)
        view = material._qualification_subject_store(writer)
        self.assertTrue(view.read_only)
        self.assertTrue(view._store.read_only)
        self.assertIsNot(view._store, writer)

    def reject_before_output(self, mutate, expected_message, expected_error=guard.GuardError) -> None:
        from tools.official_client_capture import claude_qualification_material_source as material

        create_view = material._qualification_subject_store
        bindings, projections = [], []
        verified = []

        def mutate_at_final_verification(store):
            view = create_view(store)
            original = view.verify_fact_snapshots

            def verify():
                # 生产语义读取已完成，调用方尚未取得任何原件权限。
                self.assertTrue(view._fact_snapshots)
                self.assertEqual(bindings, [])
                self.assertEqual(projections, [])
                mutate()
                verified.append(True)
                return original()

            view.verify_fact_snapshots = verify
            return view

        with mock.patch.object(material, "_qualification_subject_store", mutate_at_final_verification):
            with self.assertRaisesRegex(expected_error, expected_message):
                self.subject(read_bindings=bindings, read_projections=projections)
        self.assertEqual(verified, [True])
        self.assertEqual(bindings, [])
        self.assertEqual(projections, [])

    def test_valid_fact_append_before_return_refuses_all_output_permissions(self) -> None:
        payload = self.reader.load_fact(self.fixture.references["validation-selector-after"])["payload"]

        def append():
            self.fixture.store.append_fact(self.campaign_id, "selector_observed", payload,
                                           self.fixture._time())

        self.reject_before_output(append, "事实链发生变化")

    def test_campaign_raw_drift_before_return_refuses_all_output_permissions(self) -> None:
        path = self.reader.root / "campaigns" / f"{self.campaign_id}.json"
        self.reject_before_output(lambda: path.write_bytes(path.read_bytes() + b"\n "),
                                  "Campaign 发生变化")

    def test_fact_raw_drift_before_return_refuses_all_output_permissions(self) -> None:
        fact = self.reader.list_facts(self.campaign_id)[0]
        path = (self.reader.root / "facts" / self.campaign_id / fact["dimension"]
                / f"{fact['sequence']:08d}-{fact['_sha256']}.json")
        self.reject_before_output(lambda: path.write_bytes(path.read_bytes() + b" "),
                                  "事实摘要漂移", ControlError)

    def test_fact_permission_drift_before_return_refuses_all_output_permissions(self) -> None:
        fact = self.reader.list_facts(self.campaign_id)[0]
        path = (self.reader.root / "facts" / self.campaign_id / fact["dimension"]
                / f"{fact['sequence']:08d}-{fact['_sha256']}.json")
        self.reject_before_output(lambda: path.chmod(0o400), "事实链发生变化")

    def test_identical_fact_at_another_inode_before_return_refuses_all_output_permissions(self) -> None:
        fact = self.reader.list_facts(self.campaign_id)[0]
        path = (self.reader.root / "facts" / self.campaign_id / fact["dimension"]
                / f"{fact['sequence']:08d}-{fact['_sha256']}.json")

        def replace():
            replacement = self.helper.root / "replacement-fact.json"
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o600)
            before = path.stat()
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            os.replace(replacement, path)
            self.assertNotEqual(path.stat().st_ino, before.st_ino)

        self.reject_before_output(replace, "事实链发生变化")


if __name__ == "__main__":
    unittest.main()
