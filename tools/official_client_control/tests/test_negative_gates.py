"""FW-D 文档退出门禁与关键协同约束的负例。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tools.official_client_control.errors import ControlError
from tools.official_client_control.receipts import (
    build_promotion_receipt,
    replay_receipt,
)
from tools.official_client_control.tests.fixtures import SyntheticCampaign


class NegativeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = SyntheticCampaign(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _through_profile_objects(self, evidence_level: str = "verified") -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence(evidence_level)
        self.fixture.profile_objects()

    def test_blocks_unauthorized_transition(self) -> None:
        self.fixture.bootstrap_and_campaign()
        with self.assertRaisesRegex(ControlError, "引用不存在"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "evidence_approved",
                {
                    "evidence_fact_ref": {
                        "campaign_id": self.fixture.campaign_id,
                        "dimension": "evidence",
                        "sequence": 1,
                        "sha256": "0" * 64,
                    },
                    "evidence_package_ref": {
                        "object_kind": "evidence_package",
                        "sha256": "0" * 64,
                    },
                    "reviewer": "reviewer",
                    "review_ref": "review/invalid",
                },
                self.fixture._time(),
            )

    def test_blocks_in_place_overwrite_even_for_identical_content(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.seal_manifest("operational_evidence", "same-object")
        with self.assertRaisesRegex(ControlError, "禁止覆盖"):
            self.fixture.seal_manifest("operational_evidence", "same-object")

    def test_detects_digest_drift(self) -> None:
        self.fixture.bootstrap_and_campaign()
        reference = self.fixture.seal_manifest("operational_evidence", "drift-object")
        path = self.fixture.store.object_path(reference)
        path.write_text('{"tampered":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ControlError, "摘要漂移"):
            self.fixture.store.load_object(reference)

    def test_candidate_cannot_borrow_production_selector(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.freeze_candidate()
        candidate = self.fixture.store.load_fact(self.fixture.references["candidate"])[
            "payload"
        ]
        invalid = deepcopy(candidate)
        invalid["candidate_id"] = "synthetic-candidate-selector"
        invalid["production_rollback"] = "previous"
        with self.assertRaisesRegex(ControlError, "字段不闭合|不得借用"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "candidate_frozen",
                invalid,
                self.fixture._time(),
            )

    def test_blocks_missing_physical_ingress_alias(self) -> None:
        self._through_profile_objects()
        invalid = self.fixture.ingress_inventory_payload("retained_legacy")
        invalid["entries"] = invalid["entries"][:1]
        with self.assertRaisesRegex(ControlError, "遗漏物理别名"):
            self.fixture.store.seal_object("production_ingress_inventory", invalid)

    def test_blocks_unhandled_oauth_egress(self) -> None:
        self._through_profile_objects()
        invalid = self.fixture.egress_inventory_payload(final=False)
        invalid["entries"] = invalid["entries"][:-1]
        with self.assertRaisesRegex(ControlError, "未闭合"):
            self.fixture.store.seal_object("egress_disposition_inventory", invalid)

    def test_blocks_envelope_scope_gap(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.candidate_and_accept()
        self.fixture.promote()
        self.fixture.deploy()
        active = self.fixture.store.load_object(self.fixture.references["active-envelope"])[
            "payload"
        ]
        invalid = {
            "schema_version": "official-client-deployment-traffic-envelope/v1",
            "persona": self.fixture.persona,
            "active_support_envelope_ref": self.fixture.references["active-envelope"],
            "rollback_operational_envelope_ref": self.fixture.references[
                "rollback-envelope"
            ],
            "production_ingress_inventory_ref": self.fixture.references["ingress-inventory"],
            "capabilities": deepcopy(active["capabilities"]),
        }
        invalid["capabilities"][0]["feature"] = "outside-rollback"
        invalid["capabilities"].sort(
            key=lambda item: "\x00".join(str(item[key]) for key in sorted(item))
        )
        with self.assertRaisesRegex(ControlError, "不属于 Active 与 Rollback"):
            self.fixture.store.seal_object("deployment_traffic_envelope", invalid)

    def test_receipt_mismatch_is_rejected_on_replay(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.candidate_and_accept()
        self.fixture.promote()
        receipt = build_promotion_receipt(
            self.fixture.store,
            self.fixture.campaign_id,
            self.fixture.references["promotion-fact"],
        )
        receipt["completed_at_utc"] = "2026-08-19T00:00:00Z"
        mismatched_ref = self.fixture.store.write_receipt("promotion", receipt)
        with self.assertRaisesRegex(ControlError, "独立复算结果不匹配"):
            replay_receipt(self.fixture.store, mismatched_ref)

    def test_observed_rule_cannot_be_production_replacement(self) -> None:
        self._through_profile_objects("observed")
        with self.assertRaisesRegex(ControlError, "没有达到 verified"):
            self.fixture.profile_approve("production_replacement")

    def test_failed_attempt_is_preserved_and_cannot_advance(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.freeze_candidate()
        self.fixture.run_scenario(capture_result="failed")
        failed_ref = self.fixture.references["failed-scenario-stage"]
        with self.assertRaisesRegex(ControlError, "失败 attempt 不得继续推进"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "scenario_sealed",
                {
                    "candidate_id": "synthetic-candidate-1",
                    "scenario_id": "baseline",
                    "attempt_id": "attempt-1",
                    "stage": "seal",
                    "previous_stage_ref": failed_ref,
                    "artifact_refs": [self.fixture.references["operational-evidence"]],
                    "result": "pass",
                },
                self.fixture._time(),
            )
        self.fixture.run_scenario(attempt_id="attempt-2")
        capture_facts = [
            fact
            for fact in self.fixture.store.list_facts(
                self.fixture.campaign_id, "validation"
            )
            if fact["fact_kind"] == "scenario_captured"
        ]
        self.assertEqual([fact["payload"]["result"] for fact in capture_facts], ["failed", "pass"])


if __name__ == "__main__":
    unittest.main()
