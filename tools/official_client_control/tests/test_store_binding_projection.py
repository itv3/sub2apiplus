"""通用重放读集投影的正反专项；不启动守卫、Docker、网络或完整升级链。

每个正例都调用真实 ControlStore.replay(external_root=..., require_external=True)，
完整重建对象图、事实链与收据。反例只改变新的私有夹具，不改旧证据或正式策略。
"""

from __future__ import annotations

import copy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from tools.official_client_control import contracts, qualification_bindings, receipts
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.replay_bindings import ReplayBindingOwnership
from tools.official_client_control.store import ControlStore
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign


HOST_METADATA = {"path": "/never-open-build-host/source", "sha256": "a" * 64, "bytes": 19}


class StoreBindingProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="generic-binding-tests-", dir="/private/tmp")
        cls.root = Path(cls.temporary.name)
        cls.tree = ManagedToolTree(cls.root / "tools-repo")
        cls.active = cls.tree.active()
        cls.active.__enter__()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.addClassCleanup(cls.active.__exit__, None, None, None)
        template_root = cls.root / "template"
        template_root.mkdir(mode=0o700)
        cls.template = SyntheticCampaign(template_root, schema="v2")
        fixture = cls.template
        fixture.bootstrap_and_campaign()
        fixture.discovery_and_evidence()
        original_seal = fixture.store.seal_object

        def seal(kind: str, payload: dict, **options: object) -> dict:
            if kind == "snapshot":
                payload = copy.deepcopy(payload)
                payload["document"]["history_source"] = copy.deepcopy(HOST_METADATA)
            return original_seal(kind, payload, **options)

        with mock.patch.object(fixture.store, "seal_object", side_effect=seal):
            fixture.profile_objects()
        fixture.profile_approve()
        original_input = fixture.candidate_build_input

        def build_input(tag: str = "strict") -> dict:
            result = original_input(tag)
            parameters = {"schema_version": qualification_bindings.STRICT_BUILD_PARAMETERS_SCHEMA,
                          "binary": copy.deepcopy(HOST_METADATA),
                          "frontend": {"builder": {"receipt": copy.deepcopy(HOST_METADATA)}}}
            result["build_identity"]["parameters"] = parameters
            result["build_identity"]["parameters_sha256"] = canonical_sha256(parameters)
            entries = []
            for name in sorted(qualification_bindings.STRICT_ARTIFACT_ROLES):
                binding = fixture._write_external("artifacts/" + name, ("真实制品夹具：" + name).encode())
                (fixture.external_root / binding["path"]).chmod(0o755)
                entries.append({"role": name, "binding": binding, "source_sha256": "7" * 64})
            result["artifact_inventory"] = {
                "entries": entries, "file_count": len(entries),
                "total_bytes": sum(row["binding"]["bytes"] for row in entries),
                "inventory_sha256": canonical_sha256(entries),
            }
            return result

        with mock.patch.object(fixture, "candidate_build_input", side_effect=build_input):
            fixture.strict_validate()

    def setUp(self) -> None:
        self.temporary_case = tempfile.TemporaryDirectory(prefix="generic-binding-case-", dir="/private/tmp")
        self.addCleanup(self.temporary_case.cleanup)
        self.addCleanup(self.tree.restore)
        self.world = Path(self.temporary_case.name) / "world"
        shutil.copytree(self.template.root, self.world)
        self.fixture = copy.copy(self.template)
        self.fixture.root = self.world
        self.fixture.references = copy.deepcopy(self.template.references)
        self.fixture.store_root = self.world / "store"
        self.fixture.external_root = self.world / "external"
        self.fixture.store = ControlStore(self.fixture.store_root)
        self.reader = ControlStore.open_read_only(self.fixture.store_root)

    def replay(self) -> dict:
        return self.reader.replay(external_root=self.fixture.external_root, require_external=True)

    def original_documents(self) -> tuple[list, list, list, list]:
        campaigns = [self.reader.load_campaign(self.fixture.campaign_id)]
        objects = [(ref, self.reader.load_object(ref)) for kind in sorted(contracts.OBJECT_KINDS)
                   for ref in self.reader.list_object_refs(kind)]
        facts = [(self.reader.fact_ref(fact), fact) for fact in self.reader.list_facts(self.fixture.campaign_id)]
        receipt_documents = [(ref, self.reader.load_receipt(ref)) for kind in contracts.RECEIPT_KINDS
                            for ref in self.reader.list_receipt_refs(kind)]
        return campaigns, objects, facts, receipt_documents

    def test_complete_replay_keeps_real_read_set_and_marks_two_parent_domains(self) -> None:
        opened = []
        verify = self.reader._verify_external_binding

        def record(root: Path, binding: dict) -> None:
            opened.append(copy.deepcopy(binding))
            verify(root, binding)

        with mock.patch.object(self.reader, "_verify_external_binding", side_effect=record):
            result = self.replay()
        self.assertEqual(result["schema_version"], "official-client-control-replay-result/v2")
        self.assertTrue(result["external_verified"])
        self.assertEqual(result["binding_projection"]["schema_version"], qualification_bindings.PROJECTION_SCHEMA)
        self.assertEqual({row["domain_pointer"] for row in result["binding_projection"]["parent_domains"]},
                         {"/payload/document", "/build_identity/parameters"})
        self.assertNotIn(HOST_METADATA, opened)
        for binding in (self.fixture.contract_binding, self.fixture.receipt_binding, self.fixture.catalog_binding,
                        self.fixture.official_binding, self.fixture.evidence_binding, self.fixture.runtime_binding):
            self.assertIn(binding, opened)
        receipt = self.reader.load_receipt(self.fixture.references["candidate-build-receipt"])
        for entry in receipt["artifact_inventory"]["entries"]:
            self.assertIn(entry["binding"], opened)
        self.assertEqual(len(result["binding_projection"]["artifact_file_requirements"]), 3)
        self.assertEqual(result["external_bindings"], len(opened))

    def test_require_external_still_requires_material_root(self) -> None:
        with self.assertRaisesRegex(ControlError, "完整重放要求提供 external_root"):
            self.reader.replay(require_external=True)

    def test_real_artifact_bytes_are_read(self) -> None:
        path = self.fixture.external_root / "artifacts/sub2api"
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ControlError, "外部证据摘要漂移"):
            self.replay()

    def test_real_artifact_missing_is_rejected(self) -> None:
        (self.fixture.external_root / "artifacts/sub2api").unlink()
        with self.assertRaisesRegex(ControlError, "外部证据不存在或不可信"):
            self.replay()

    def test_real_artifact_mode_is_read(self) -> None:
        (self.fixture.external_root / "artifacts/sub2api").chmod(0o600)
        with self.assertRaisesRegex(ControlError, "0755 权限漂移"):
            self.replay()

    def test_other_true_binding_still_requires_original_bytes(self) -> None:
        path = self.fixture.external_root / self.fixture.official_binding["path"]
        path.write_bytes(b"changed official")
        with self.assertRaisesRegex(ControlError, "外部证据摘要漂移"):
            self.replay()

    def test_parent_object_raw_bytes_still_require_content_addressing(self) -> None:
        reference = self.fixture.references["snapshot"]
        path = self.reader.object_path(reference)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ControlError, "对象摘要漂移"):
            self.replay()

    def test_parent_receipt_raw_bytes_still_require_content_addressing(self) -> None:
        reference = self.fixture.references["candidate-build-receipt"]
        path = self.reader.receipt_path(reference)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ControlError, "收据摘要漂移"):
            self.replay()

    def test_receipt_reconstruction_still_rejects_semantic_forgery(self) -> None:
        document = copy.deepcopy(self.reader.load_receipt(self.fixture.references["candidate-build-receipt"]))
        document["producer_tool_sha256"] = "f" * 64
        document["identity_sha256"] = canonical_sha256({key: value for key, value in document.items()
                                                      if key != "identity_sha256"})
        contracts.validate_receipt_document(document, "candidate_build")
        raw = canonical_json_bytes(document)
        path = self.fixture.store.receipt_path({"receipt_kind": "candidate_build", "sha256": sha256_bytes(raw)})
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaisesRegex(ControlError, "收据与事实独立复算结果不匹配"):
            self.replay()

    def test_unknown_parent_cannot_hide_binding_behind_nested_snapshot_schema(self) -> None:
        self.fixture.seal_manifest("operational_evidence", "unknown-parent", [{"id": "row", "facts": {
            "schema_version": "official-client-snapshot/v1", "source": copy.deepcopy(HOST_METADATA)}}])
        with self.assertRaisesRegex(ControlError, "根内规范相对路径"):
            self.replay()

    def test_orphan_snapshot_cannot_claim_claude_via_document_metadata(self) -> None:
        payload = copy.deepcopy(self.reader.load_object(self.fixture.references["snapshot"])["payload"])
        payload["document"].update(client="claude", orphan=True)
        self.fixture.store.seal_object("snapshot", payload)
        with self.assertRaisesRegex(ControlError, "根内规范相对路径"):
            self.replay()

    def test_unknown_consumer_keeps_owned_snapshot_on_original_strict_path(self) -> None:
        self.fixture.seal_manifest("operational_evidence", "unknown-consumer", [{"id": "row", "facts": {
            "snapshot_ref": self.fixture.references["snapshot"]}}])
        with self.assertRaisesRegex(ControlError, "根内规范相对路径"):
            self.replay()

    def test_cross_client_reference_is_not_given_claude_projection(self) -> None:
        # 只构造合同负例验证分派，不签发 Codex 当前身份或宣称 Codex v2 可执行。
        campaigns, objects, facts, receipt_documents = self.original_documents()
        foreign = copy.deepcopy(campaigns[0])
        foreign.update(campaign_id="codex-schema-negative", client="codex")
        foreign["tool_identity"]["client"] = "codex"
        foreign["identity_sha256"] = contracts.campaign_identity_sha256(foreign)
        contracts.validate_campaign(foreign)
        # 这里只验证合同级归属分派的拒绝切面；原完整收据门禁仍会拒绝
        # 跨 Campaign 引用。本负例没有签发当前权限，也没有调用写入入口。
        foreign_receipt = copy.deepcopy(next(doc for ref, doc in receipt_documents
                                            if ref["receipt_kind"] == "validation_gate"))
        foreign_receipt["campaign_id"] = foreign["campaign_id"]
        foreign_receipt["identity_sha256"] = canonical_sha256({key: value for key, value in foreign_receipt.items()
                                                             if key != "identity_sha256"})
        contracts.validate_receipt_document(foreign_receipt, "validation_gate")
        receipt_documents.append(({"receipt_kind": "validation_gate", "sha256": canonical_sha256(foreign_receipt)},
                                  foreign_receipt))
        ownership = ReplayBindingOwnership([*campaigns, foreign], objects, facts, receipt_documents)
        self.assertFalse(ownership.is_claude_parent(self.fixture.references["candidate-build-receipt"]))
        self.assertFalse(ownership.is_claude_parent(self.fixture.references["snapshot"]))

    def import_snapshot_target(self, mode: str) -> SyntheticCampaign:
        root = self.world.parent / "target"
        root.mkdir(mode=0o700)
        target = SyntheticCampaign(root, schema="v2")
        target.campaign_id = "synthetic-import-target"
        target.bootstrap_and_campaign()
        source_campaign = self.reader.load_campaign(self.fixture.campaign_id)
        target_campaign = target.store.load_campaign(target.campaign_id)
        imported = receipts.predecessor_object_closure(self.reader, [self.fixture.references["snapshot"]])
        payload = {"client": "claude", "campaign_id": target.campaign_id,
                   "predecessor": {"store_root": str(self.reader.root), "campaign_id": self.fixture.campaign_id,
                                   "campaign_identity_sha256": source_campaign["identity_sha256"]},
                   "imported_objects": imported, "imported_fact_refs": [],
                   "identity_conditions": contracts.derive_identity_conditions(target_campaign)[0],
                   "source_identity_conditions": contracts.derive_identity_conditions(source_campaign)[0],
                   "mode": mode, "approved_by": "synthetic-operator", "approved_at_utc": target._time()}
        reference = receipts.register_predecessor_import(target.store, payload)["import_ref"]
        receipts.finalize_predecessor_import(target.store, reference, target._time())
        return target

    def test_predecessor_source_does_not_gain_projection_from_target_client(self) -> None:
        target = self.import_snapshot_target("read_only_reuse")
        with self.assertRaisesRegex(ControlError, "根内规范相对路径"):
            target.store.replay(external_root=target.external_root, require_external=True)

    def test_fixture_only_imported_snapshot_keeps_original_external_filter(self) -> None:
        target = self.import_snapshot_target("regression_fixture")
        result = target.store.replay(external_root=target.external_root, require_external=True)
        self.assertEqual(result["result"], "passed")
        self.assertNotIn("binding_projection", result)

    def test_tool_drift_does_not_grant_current_execution_authority(self) -> None:
        self.tree.write("official_client_control/store.py", '"""变更后的工具替身。"""\n'.encode())
        self.assertEqual(self.replay()["result"], "passed")
        campaign = self.reader.load_campaign(self.fixture.campaign_id)
        with self.assertRaisesRegex(ControlError, "工具身份未登记演进"):
            receipts._require_effective_tool(self.reader, campaign)


class StrictReplayCompatibilityTests(unittest.TestCase):
    def test_legacy_complete_store_result_keeps_original_v1_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="generic-v1-compat-", dir="/private/tmp") as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture.complete()
            result = fixture.store.replay(external_root=fixture.external_root, require_external=True)
            self.assertEqual(result["schema_version"], "official-client-control-replay-result/v1")
            self.assertEqual(set(result), {"schema_version", "result", "objects", "campaigns", "facts", "receipts",
                                           "external_bindings", "external_verified"})

    def test_codex_persona_without_explicit_claude_campaign_keeps_strict_scan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="generic-codex-strict-", dir="/private/tmp") as temporary:
            fixture = SyntheticCampaign(Path(temporary))
            fixture.persona = {"provider": "openai", "official_product": "codex-cli", "auth_family": "oauth",
                               "upstream_route_family": "responses"}
            fixture.bootstrap_and_campaign()
            fixture.discovery_and_evidence()
            fixture.profile_objects()
            payload = copy.deepcopy(fixture.store.load_object(fixture.references["snapshot"])["payload"])
            payload["document"]["origin"] = copy.deepcopy(HOST_METADATA)
            fixture.store.seal_object("snapshot", payload)
            with self.assertRaisesRegex(ControlError, "根内规范相对路径"):
                fixture.store.replay(external_root=fixture.external_root, require_external=True)
