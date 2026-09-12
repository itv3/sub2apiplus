"""FW-D 合成 Persona 的完整受管闭环。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import replay_receipt
from tools.official_client_control.tests.fixtures import SyntheticCampaign


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = SyntheticCampaign(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_campaign_replays_to_restored_active(self) -> None:
        self.fixture.complete()
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "restored_active")
        self.assertTrue(status["production_active_proven"])

        replay = self.fixture.store.replay(
            external_root=self.fixture.external_root,
            require_external=True,
        )
        self.assertEqual(replay["result"], "passed")
        self.assertEqual(replay["campaigns"], 1)
        self.assertEqual(replay["receipts"], 2)
        self.assertTrue(replay["external_verified"])

    def test_vc_1_to_vc_3_stays_in_one_campaign(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.record_discovery_and_evidence()
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "evidence_recorded")

        package = self.fixture.store.load_object(
            self.fixture.references["evidence-package"]
        )["payload"]
        self.assertEqual(package["schema_version"], "official-client-evidence-package/v3")
        self.assertNotIn("rules", package)

        self.fixture.classify_rules()
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "evidence_recorded")
        evidence_kinds = [
            fact["fact_kind"]
            for fact in self.fixture.store.list_facts(
                self.fixture.campaign_id, "evidence"
            )
        ]
        self.assertEqual(
            evidence_kinds,
            ["evidence_recorded", "rule_classification_recorded"],
        )

        self.fixture.approve_evidence()
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "official_sealed")

        self.fixture.profile_objects()
        self.fixture.profile_approve()
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "profile_approved")
        campaign_ids = {
            fact["campaign_id"]
            for fact in self.fixture.store.list_facts(self.fixture.campaign_id)
        }
        self.assertEqual(campaign_ids, {self.fixture.campaign_id})

    def test_legacy_evidence_package_v1_remains_replayable(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_legacy_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        replay = self.fixture.store.replay(
            external_root=self.fixture.external_root,
            require_external=True,
        )
        self.assertEqual(replay["result"], "passed")
        self.assertEqual(
            WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)[
                "checkpoint"
            ],
            "profile_approved",
        )

    def test_legacy_evidence_package_v2_remains_replayable(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_legacy_evidence(
            schema_version="official-client-evidence-package/v2"
        )
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        replay = self.fixture.store.replay(
            external_root=self.fixture.external_root,
            require_external=True,
        )
        self.assertEqual(replay["result"], "passed")

    def test_receipts_are_deterministically_rebuilt(self) -> None:
        self.fixture.complete()
        promotion = replay_receipt(
            self.fixture.store, self.fixture.references["promotion-receipt"]
        )
        activation = replay_receipt(
            self.fixture.store, self.fixture.references["activation-receipt"]
        )
        self.assertEqual(promotion["campaign_id"], self.fixture.campaign_id)
        self.assertEqual(activation["final_state"], "restored_active")
        self.assertEqual(len(activation["deployment_fact_refs"]), 5)

    def test_orthogonal_dimensions_remain_separate(self) -> None:
        self.fixture.complete()
        expected = {
            "discovery",
            "evidence",
            "approval",
            "validation",
            "runtime_selector",
            "deployment",
        }
        actual = {
            fact["dimension"]
            for fact in self.fixture.store.list_facts(self.fixture.campaign_id)
        }
        self.assertEqual(actual, expected)
        approval_kinds = {
            fact["fact_kind"]
            for fact in self.fixture.store.list_facts(
                self.fixture.campaign_id, "approval"
            )
        }
        self.assertEqual(approval_kinds, {"evidence_approved", "profile_approved"})
        evidence_kinds = {
            fact["fact_kind"]
            for fact in self.fixture.store.list_facts(
                self.fixture.campaign_id, "evidence"
            )
        }
        self.assertEqual(
            evidence_kinds,
            {"evidence_recorded", "rule_classification_recorded"},
        )

    def test_restored_fact_without_activation_receipt_is_unverified(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        self.fixture.candidate_and_accept()
        self.fixture.promote()
        self.fixture.deploy(finalize_activation_receipt=False)
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "restored_active")
        self.assertEqual(status["production_state"], "production_unverified")
        self.assertFalse(status["production_active_proven"])

    def test_strict_vc_5_and_vc_6_replay_to_operator_release(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()

        validation_status = WorkflowGates(self.fixture.store).status(
            self.fixture.campaign_id
        )
        self.assertEqual(validation_status["checkpoint"], "ready")
        self.assertEqual(validation_status["production_state"], "not_activated")
        self.assertEqual(
            validation_status["delivery_state"], "not_ready_for_operator_release"
        )

        self.fixture.strict_deliver()
        delivery_status = WorkflowGates(self.fixture.store).status(
            self.fixture.campaign_id
        )
        self.assertEqual(delivery_status["checkpoint"], "ready_for_operator_release")
        self.assertEqual(delivery_status["delivery_state"], "ready_for_operator_release")
        self.assertEqual(delivery_status["production_state"], "not_activated")
        self.assertFalse(delivery_status["production_active_proven"])

        receipt = replay_receipt(
            self.fixture.store,
            self.fixture.references["candidate-delivery-receipt"],
        )
        self.assertEqual(receipt["release_state"], "ready_for_operator_release")
        self.assertEqual(receipt["campaign_purpose"], "production_replacement")
        replay = self.fixture.store.replay(
            external_root=self.fixture.external_root,
            require_external=True,
        )
        self.assertEqual(replay["result"], "passed")
        self.assertEqual(replay["receipts"], 3)

    def test_validation_only_also_reaches_candidate_delivery_terminal(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve("validation_only")
        self.fixture.strict_validate()
        validation_status = WorkflowGates(self.fixture.store).status(
            self.fixture.campaign_id
        )
        self.assertEqual(validation_status["checkpoint"], "validation_only")
        self.assertEqual(validation_status["production_state"], "not_activated")

        self.fixture.strict_deliver()
        delivery_status = WorkflowGates(self.fixture.store).status(
            self.fixture.campaign_id
        )
        self.assertEqual(delivery_status["checkpoint"], "ready_for_operator_release")
        self.assertEqual(delivery_status["production_state"], "not_activated")
        receipt = replay_receipt(
            self.fixture.store,
            self.fixture.references["candidate-delivery-receipt"],
        )
        self.assertEqual(receipt["campaign_purpose"], "validation_only")

    def test_strict_acceptance_does_not_advance_before_vc_5_finalizer(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate(finalize=False)
        status = WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)
        self.assertEqual(status["checkpoint"], "validation_pending")
        self.assertEqual(status["delivery_state"], "not_ready_for_operator_release")


if __name__ == "__main__":
    unittest.main()
