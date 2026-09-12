"""FW-D 文档退出门禁与关键协同约束的负例。"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.receipts import (
    build_activation_receipt,
    build_promotion_receipt,
    finalize_candidate_delivery,
    finalize_validation,
    finalize_validation_gate,
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

    def test_v3_evidence_approval_requires_vc_2_classification(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.record_discovery_and_evidence()
        with self.assertRaisesRegex(ControlError, "必须先绑定 VC-2 规则分类事实"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "evidence_approved",
                {
                    "evidence_fact_ref": self.fixture.references["evidence-fact"],
                    "evidence_package_ref": self.fixture.references[
                        "evidence-package"
                    ],
                    "reviewer": "synthetic-reviewer",
                    "review_ref": "review/evidence-without-classification",
                },
                self.fixture._time(),
            )

    def test_vc_2_classification_blocks_unresolved_rule(self) -> None:
        self.fixture.bootstrap_and_campaign()
        self.fixture.record_discovery_and_evidence()
        with self.assertRaisesRegex(ControlError, "仍包含 blocked 规则"):
            self.fixture.classify_rules("blocked")

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

    def test_blocks_unknown_egress_guard_state(self) -> None:
        self._through_profile_objects()
        invalid = self.fixture.egress_inventory_payload(final=False)
        invalid["entries"][0]["current_guard_state"] = "unknown"
        with self.assertRaisesRegex(ControlError, "current_guard_state 非法"):
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

    def test_production_receipts_reject_cross_campaign_facts(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.candidate_and_accept()
        self.fixture.promote()
        self.fixture.deploy()
        other_campaign = deepcopy(
            self.fixture.store.load_campaign(self.fixture.campaign_id)
        )
        other_campaign["campaign_id"] = "synthetic-campaign-other"
        self.fixture.store.create_campaign(other_campaign)

        with self.assertRaisesRegex(ControlError, "不得跨 Campaign 引用晋升事实"):
            build_promotion_receipt(
                self.fixture.store,
                other_campaign["campaign_id"],
                self.fixture.references["promotion-fact"],
            )
        with self.assertRaisesRegex(ControlError, "不得跨 Campaign 引用 DeploymentFact"):
            build_activation_receipt(
                self.fixture.store,
                other_campaign["campaign_id"],
                self.fixture.references["deployment-restored_active"],
                self.fixture.references["selector-before"],
                self.fixture.references["selector-after"],
                self.fixture.references["inventory-current"],
            )

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

    def test_vc_5_finalizer_blocks_production_selector_drift(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate(finalize=False)
        drifted_runtime = self.fixture.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.fixture.persona,
                "catalog_digest": "f" * 64,
                "production_active_ref": self.fixture.references["release"],
                "production_rollback_ref": None,
                "observed_at_utc": self.fixture._time(),
                "source_ref": self.fixture.runtime_binding,
            },
        )
        selector_after = self.fixture.store.append_fact(
            self.fixture.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": drifted_runtime,
                "observation_kind": "read_only",
            },
            self.fixture._time(),
        )
        with self.assertRaisesRegex(ControlError, "Active／Rollback／selector 发生变化"):
            finalize_validation(
                self.fixture.store,
                self.fixture.campaign_id,
                self.fixture.references["acceptance-strict"],
                selector_after,
                self.fixture._time(),
            )

    def test_vc_6_rejects_candidate_rebuild_or_wrong_image(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver()
        first_ref = self.fixture.references["delivery-fact-refs"][0]
        first = self.fixture.store.load_fact(first_ref)["payload"]
        invalid = deepcopy(first)
        invalid["image_digest"] = "sha256:" + "2" * 64
        invalid["identity_sha256"] = "0" * 64
        invalid["identity_sha256"] = canonical_sha256(
            {key: value for key, value in invalid.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "正确镜像摘要"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "candidate_delivery_recorded",
                invalid,
                self.fixture._time(),
            )

    def test_vc_6_preserves_failed_stage_and_blocks_advancement(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver(fail_stage="candidate_active")
        failed_ref = self.fixture.references["failed-delivery-stage"]
        failed = self.fixture.store.load_fact(failed_ref)["payload"]
        self.assertEqual(failed["result"], "failed")

        plan = self.fixture.store.load_object(
            self.fixture.references["delivery-plan"]
        )["payload"]
        invalid_next = deepcopy(failed)
        invalid_next.update(
            {
                "stage": "rollback_verified",
                "previous_stage_ref": failed_ref,
                "runtime_profile_sha256": plan[
                    "rollback_runtime_profile_sha256"
                ],
                "image_digest": plan["rollback_image_digest"],
                "check_ids": plan["stage_checks"]["rollback_verified"],
                "evidence_refs": [self.fixture.references["operational-evidence"]],
                "result": "pass",
            }
        )
        invalid_next["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in invalid_next.items()
                if key != "identity_sha256"
            }
        )
        with self.assertRaisesRegex(ControlError, "失败的候选交付阶段不得继续推进"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "candidate_delivery_recorded",
                invalid_next,
                self.fixture._time(),
            )
        replay = self.fixture.store.replay(
            external_root=self.fixture.external_root,
            require_external=True,
        )
        self.assertEqual(replay["result"], "passed")

    def test_vc_6_preserves_incomplete_stability_window_as_failure(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver(fail_stage="stable_observed")
        failed = self.fixture.store.load_fact(
            self.fixture.references["failed-delivery-stage"]
        )["payload"]
        self.assertEqual(failed["stage"], "stable_observed")
        self.assertEqual(failed["observed_seconds"], 0)
        self.assertEqual(failed["result"], "failed")

    def test_validation_gate_slot_is_append_only_per_attempt(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        with self.assertRaisesRegex(ControlError, "门禁槽位不得覆盖"):
            finalize_validation_gate(
                self.fixture.store,
                self.fixture.campaign_id,
                self.fixture.references["validation-attempt"],
                {
                    "gate_id": "gate-runtime",
                    "started_at_utc": self.fixture._time(),
                    "completed_at_utc": self.fixture._time(),
                    "exit_code": 0,
                    "output_sha256": "d" * 64,
                    "result": "pass",
                },
            )

    def test_validation_gate_retry_binds_latest_failed_receipt(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate(gate_result="failed", stop_after_gate=True)
        failed_ref = self.fixture.references["validation-gate-receipt"]
        original_plan = self.fixture.store.load_object(
            self.fixture.references["validation-plan"]
        )["payload"]
        original_attempt = self.fixture.store.load_fact(
            self.fixture.references["validation-attempt"]
        )["payload"]

        def create_retry(
            suffix: str, previous_failed_ref: dict[str, object] | None
        ) -> dict[str, object]:
            plan = deepcopy(original_plan)
            plan["plan_id"] = f"validation-plan-retry-{suffix}"
            plan["run_conditions"]["run_nonce"] = f"nonce-retry-{suffix}"
            plan["external_gates"][0][
                "previous_failed_receipt_ref"
            ] = previous_failed_ref
            plan["identity_sha256"] = canonical_sha256(
                {key: value for key, value in plan.items() if key != "identity_sha256"}
            )
            plan_ref = self.fixture.store.seal_object(
                "validation_execution_plan", plan
            )
            attempt = deepcopy(original_attempt)
            attempt["attempt_id"] = f"attempt-retry-{suffix}"
            attempt["validation_execution_plan_ref"] = plan_ref
            attempt["run_conditions"] = plan["run_conditions"]
            attempt["identity_sha256"] = canonical_sha256(
                {
                    key: value
                    for key, value in attempt.items()
                    if key != "identity_sha256"
                }
            )
            return self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "validation_attempt_created",
                attempt,
                self.fixture._time(),
            )

        missing_ref_attempt = create_retry("missing", None)
        with self.assertRaisesRegex(ControlError, "必须绑定最近一份前序失败收据"):
            finalize_validation_gate(
                self.fixture.store,
                self.fixture.campaign_id,
                missing_ref_attempt,
                {
                    "gate_id": "gate-runtime",
                    "started_at_utc": self.fixture._time(),
                    "completed_at_utc": self.fixture._time(),
                    "exit_code": 0,
                    "output_sha256": "d" * 64,
                    "result": "pass",
                },
            )

        retry_attempt = create_retry("linked", failed_ref)
        retry_ref = finalize_validation_gate(
            self.fixture.store,
            self.fixture.campaign_id,
            retry_attempt,
            {
                "gate_id": "gate-runtime",
                "started_at_utc": self.fixture._time(),
                "completed_at_utc": self.fixture._time(),
                "exit_code": 0,
                "output_sha256": "e" * 64,
                "result": "pass",
            },
        )
        retry = self.fixture.store.load_receipt(retry_ref)
        self.assertEqual(retry["previous_failed_receipt_ref"], failed_ref)

    def test_reused_gate_cannot_relabel_execution_identity(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        original = self.fixture.store.load_object(
            self.fixture.references["validation-plan"]
        )["payload"]
        invalid = deepcopy(original)
        invalid["plan_id"] = "validation-plan-invalid-reuse"
        invalid["execute_item_ids"] = ["baseline"]
        invalid["reuse_item_ids"] = ["gate-runtime"]
        invalid["external_gates"][0]["source"] = "reuse"
        invalid["external_gates"][0]["command"] = ["python3", "changed_gate.py"]
        invalid["external_gates"][0]["reused_receipt_ref"] = self.fixture.references[
            "validation-gate-receipt"
        ]
        invalid["identity_sha256"] = canonical_sha256(
            {key: value for key, value in invalid.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "command 与计划不一致"):
            self.fixture.store.seal_object("validation_execution_plan", invalid)

    def test_delivery_package_must_use_frozen_rollback_materials(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver()
        package = self.fixture.store.load_object(
            self.fixture.references["delivery-package"]
        )["payload"]
        replacement = self.fixture.seal_manifest(
            "operational_evidence", "different-rollback-material"
        )
        invalid = deepcopy(package)
        invalid["rollback_material_refs"] = [replacement]
        invalid["identity_sha256"] = canonical_sha256(
            {key: value for key, value in invalid.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "回退材料与冻结计划不一致"):
            self.fixture.store.seal_object("candidate_delivery_package", invalid)

    def test_candidate_delivery_receipt_is_unique_per_acceptance(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver()
        with self.assertRaisesRegex(ControlError, "候选交付收据不得覆盖"):
            finalize_candidate_delivery(
                self.fixture.store,
                self.fixture.campaign_id,
                self.fixture.references["delivery-package"],
            )

    def test_delivery_package_requires_passed_public_evidence_and_exact_count(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        self.fixture.strict_deliver()
        package = self.fixture.store.load_object(
            self.fixture.references["delivery-package"]
        )["payload"]
        failed_evidence = self.fixture.seal_manifest(
            "operational_evidence",
            "failed-public-evidence",
            [{"id": "public-index", "facts": {"result": "failed"}}],
        )
        invalid_evidence = deepcopy(package)
        invalid_evidence["public_evidence_index_refs"] = [failed_evidence]
        invalid_evidence["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in invalid_evidence.items()
                if key != "identity_sha256"
            }
        )
        with self.assertRaisesRegex(ControlError, "存在未通过证据项"):
            self.fixture.store.seal_object(
                "candidate_delivery_package", invalid_evidence
            )

        invalid_count = deepcopy(package)
        invalid_count["artifact_count"] += 1
        invalid_count["identity_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in invalid_count.items()
                if key != "identity_sha256"
            }
        )
        with self.assertRaisesRegex(ControlError, "artifact_count"):
            self.fixture.store.seal_object("candidate_delivery_package", invalid_count)

    def test_strict_candidate_cannot_promote_before_candidate_delivery(self) -> None:
        self._through_profile_objects()
        self.fixture.profile_approve()
        self.fixture.strict_validate()
        promotion_diff = self.fixture.seal_manifest(
            "promotion_diff",
            "strict-promotion-diff",
            [{"id": "candidate-to-production", "facts": {"result": "pass"}}],
        )
        payload = {
            "candidate_ref": self.fixture.references["candidate-strict"],
            "acceptance_ref": self.fixture.references["acceptance-strict"],
            "release_artifact_ref": self.fixture.references["release"],
            "promotion_diff_ref": promotion_diff,
        }
        with self.assertRaisesRegex(ControlError, "必须先形成唯一且可重放"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "release_promoted",
                payload,
                self.fixture._time(),
            )

        self.fixture.strict_deliver()
        reference = self.fixture.store.append_fact(
            self.fixture.campaign_id,
            "release_promoted",
            payload,
            self.fixture._time(),
        )
        self.assertEqual(
            self.fixture.store.load_fact(reference)["fact_kind"], "release_promoted"
        )


if __name__ == "__main__":
    unittest.main()
