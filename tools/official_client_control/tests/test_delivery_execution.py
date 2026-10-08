"""真实交付合同的 Store 消费、来源拒绝与历史兼容回归。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tools.official_client_control import contracts, delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.store import ControlStore


PERSONA = {"provider": "synthetic-provider", "official_product": "synthetic-cli", "auth_family": "oauth", "upstream_route_family": "synthetic-responses"}


class ExecutionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ControlStore.initialize(Path(self.temporary.name) / "store", "2026-10-08T00:00:00Z")
        self.projection = {"schema_version": "claude-upgrade-stable-environment-projection/v1", "application_service": "app", "compose": {"services": {"app": {}, "db": {"image": "fixed-db"}}}, "containers": {}}
        self.raw = canonical_json_bytes(self.projection)
        self.binding = {"path": "delivery/projection.json", "sha256": sha256_bytes(self.raw), "bytes": len(self.raw)}
        self.proof_raw = b"trusted-source-original"
        self.proof = {"path": "delivery/source-proof.bin", "sha256": sha256_bytes(self.proof_raw), "bytes": len(self.proof_raw)}
        self.facts = {"contract_version": "claude-delivery-stable-projection/v1", "result": "pass", "campaign_id": "campaign-1", "delivery_id": "candidate-d1", "origin": {"authority": "test-trusted-channel", "source_scope": "regression_fixture", "configuration_sha256": "a" * 64, "service_instance_id": "guard-instance-1", "operations_index_binding": self.proof}, "projection": deepcopy(self.projection), "projection_sha256": canonical_sha256(self.projection), "binding": self.binding}
        self.payload = {"schema_version": execution.OPERATIONAL_V2, "persona": PERSONA, "manifest_id": "candidate-d1-projection", "evidence_type": "stable_projection", "entries": [{"id": "stable_projection", "facts": self.facts}]}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def verifier(self, store, core, origin):
        return execution.VerifiedSource(canonical_sha256(core), "a" * 64, {self.binding["path"]: self.raw, self.proof["path"]: self.proof_raw}, "a" * 64, "guard-instance-1")

    def test_unregistered_self_hashed_upload_is_rejected_by_direct_store(self) -> None:
        contracts.validate_object_document({"schema_version": contracts.OBJECT_SCHEMA, "object_kind": "operational_evidence", "payload": self.payload})
        with self.assertRaisesRegex(ControlError, "受管通道验收"):
            self.store.seal_object("operational_evidence", self.payload)
        self.assertEqual(self.store.list_object_refs("operational_evidence"), [])

    def test_registered_channel_original_survives_write_and_graph_replay(self) -> None:
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {"test-trusted-channel": self.verifier}, clear=True):
            reference = self.store.seal_object("operational_evidence", self.payload)
            WorkflowGates(self.store).validate_object_graph(reference)
            self.assertTrue(self.store.seal_object_outcome("operational_evidence", self.payload, allow_existing=True)["reused"])

    def test_equal_digest_shape_does_not_replace_channel_bytes(self) -> None:
        mutated = deepcopy(self.payload)
        mutated["entries"][0]["facts"]["projection"]["compose"]["services"]["db"]["image"] = "changed-db"
        mutated["entries"][0]["facts"]["projection_sha256"] = canonical_sha256(mutated["entries"][0]["facts"]["projection"])
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {"test-trusted-channel": self.verifier}, clear=True):
            with self.assertRaisesRegex(ControlError, "原件不一致"):
                self.store.seal_object("operational_evidence", mutated)

    def test_source_core_binding_and_deployment_are_not_self_declared(self) -> None:
        for result in (execution.VerifiedSource("0" * 64, "a" * 64, {}, "a" * 64, "guard-instance-1"), execution.VerifiedSource(canonical_sha256({k: v for k, v in self.facts.items() if k != "origin"}), "a" * 64, {}, "b" * 64, "guard-instance-1"), True):
            with self.subTest(result=type(result).__name__):
                with mock.patch.dict(execution.SOURCE_VERIFIERS, {"test-trusted-channel": lambda *args: result}, clear=True):
                    with self.assertRaises(ControlError):
                        self.store.seal_object("operational_evidence", self.payload)

    def test_channel_missing_original_is_rejected(self) -> None:
        def missing(store, core, origin):
            return execution.VerifiedSource(canonical_sha256(core), "a" * 64, {self.binding["path"]: self.raw}, "a" * 64, "guard-instance-1")
        with mock.patch.dict(execution.SOURCE_VERIFIERS, {"test-trusted-channel": missing}, clear=True):
            with self.assertRaisesRegex(ControlError, "原件缺失"):
                self.store.seal_object("operational_evidence", self.payload)

    def test_typed_archive_includes_failures_without_private_prefix(self) -> None:
        binding = {"path": "journal/failed-and-restored.json", "sha256": "e" * 64, "bytes": 7}
        self.assertEqual(execution.typed_bindings({"operations": [{"status": "restored", "journal": binding}]}), [binding])
        with self.assertRaisesRegex(ControlError, "不同字节"):
            execution.typed_bindings([binding, {**binding, "sha256": "f" * 64}])

    def test_unknown_fields_and_type_mixing_do_not_fall_back_to_generic(self) -> None:
        for change in (lambda p: p.update(extra=True), lambda p: p.update(evidence_type="arbitrary"), lambda p: p["entries"][0]["facts"].update(arbitrary=True), lambda p: p["entries"][0].update(id="other")):
            payload = deepcopy(self.payload)
            change(payload)
            with self.subTest(payload=payload["evidence_type"]):
                with self.assertRaises(ControlError):
                    self.store.seal_object("operational_evidence", payload)

    def test_generic_operational_v1_remains_available_outside_new_delivery(self) -> None:
        reference = self.store.seal_object("operational_evidence", {"schema_version": "official-client-operational-evidence/v1", "persona": PERSONA, "manifest_id": "unrelated-fw-e", "entries": [{"id": "independent-evidence", "facts": {"result": "pass"}}]})
        WorkflowGates(self.store).validate_object_graph(reference)

    def test_model_requests_are_rejected_even_when_response_was_denied(self) -> None:
        for path in ("/v1/messages", "/v1/messages/count_tokens", "/v1/messages/batches"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ControlError, "模型 POST"):
                    execution._requests([{"method": "POST", "path": path, "request_binding": self.binding, "response_binding": None}])


if __name__ == "__main__":
    unittest.main()
