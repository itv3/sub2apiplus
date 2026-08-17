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


if __name__ == "__main__":
    unittest.main()
