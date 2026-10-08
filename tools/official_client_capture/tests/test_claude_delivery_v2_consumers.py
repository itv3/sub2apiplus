"""真实交付的直接发布与 VC-6 消费不能借历史收据取得新完成权。"""

from __future__ import annotations

import json
from copy import deepcopy

from tools.official_client_capture import claude_upgrade_delivery as delivery, claude_upgrade_vc, codex_upgrade_vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests.test_claude_upgrade_delivery import _DeliveryCase, CANDIDATE
from tools.official_client_control import contracts, delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256
from tools.official_client_control.errors import ControlError


class DeliveryCurrentConsumerTests(_DeliveryCase):
    def _new_result_over_old_receipt(self):
        self.finalized()
        path = delivery.stage_result_path(self.campaign, CANDIDATE)
        value = json.loads(path.read_bytes())
        value["schema_version"] = delivery.STAGE_RESULT_V2_SCHEMA
        path.write_bytes(canonical_json_bytes(value))
        return path, value

    def test_current_result_cannot_promote_historical_receipt(self):
        self._new_result_over_old_receipt()
        with self.assertRaisesRegex(ConfigurationError, "Receipt v4"):
            delivery.load_stage_result(self.campaign, CANDIDATE)

    def test_resealed_checkpoint_still_runs_delivery_semantics(self):
        stage_path, result = self._new_result_over_old_receipt()
        path = claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-6", revision=1)
        checkpoint = json.loads(path.read_bytes())
        checkpoint["stage_receipt"] = claude_upgrade_vc.vc_control_binding(self.campaign, stage_path)
        checkpoint["checkpoint_sha256"] = codex_upgrade_vc_artifacts.digest({key: item for key, item in checkpoint.items() if key != "checkpoint_sha256"})
        path.write_bytes(canonical_json_bytes(checkpoint))
        codex_upgrade_vc_artifacts.validate_vc_checkpoint(checkpoint, self.plan)
        with self.assertRaisesRegex(ConfigurationError, "Receipt v4"):
            claude_upgrade_vc.replay_vc_checkpoint(self.campaign, self.plan, "VC-6", revision=1)

    def test_checkpoint_write_rejects_new_result_with_old_delivery(self):
        stage_path, result = self._new_result_over_old_receipt()
        with self.assertRaisesRegex(ConfigurationError, "Receipt v4"):
            claude_upgrade_vc.complete_candidate_phase(self.campaign, delivery._orchestrator().load_campaign_manifest(self.campaign), phase="VC-6", revision=1, stage_receipt_path=stage_path, completed_at_utc=result["completed_at_utc"], execute_item_ids=[], reuse_item_ids=[], live_request_count=0, scanned_bytes=0)

    def test_direct_store_rejects_self_hashed_v4_over_legacy_package(self):
        final = self.finalized()
        store = self.store()
        value = deepcopy(store.load_receipt(final["receipt_ref"]))
        package = store.load_object(value["candidate_delivery_package_ref"])["payload"]
        reference = package["public_evidence_index_refs"][0]
        additions = {"executor_configuration_ref": reference, "stable_environment_projection_ref": reference, "probe_recipe_ref": reference, "rollback_state_ref": reference, "deployment_configuration_ref": reference, "publication_intent_bindings": [self.fixture.private_binding], "execution_evidence_refs": [reference], "restoration_evidence_refs": [], "operation_index_binding": self.fixture.private_binding, "operation_index_origin": {"authority": "test-trusted-channel", "source_scope": "regression_fixture", "configuration_sha256": "a" * 64, "service_instance_id": "test-guard-1", "operations_index_binding": self.fixture.private_binding}}
        value.update(additions)
        value["schema_version"] = execution.RECEIPT_V4
        value["input_facts_sha256"] = canonical_sha256([*contracts._candidate_delivery_receipt_inputs(value), *[value[key] for key in sorted(execution.RECEIPT_EXTRA_FIELDS)]])
        value["identity_sha256"] = canonical_sha256({key: item for key, item in value.items() if key != "identity_sha256"})
        contracts.validate_receipt_document(value, "candidate_delivery")
        before = store.list_receipt_refs("candidate_delivery")
        with self.assertRaisesRegex(ControlError, "Package v2"):
            store.write_receipt_outcome("candidate_delivery", value, allow_existing=True)
        self.assertEqual(store.list_receipt_refs("candidate_delivery"), before)
