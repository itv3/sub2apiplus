"""FW-D 对象图、Campaign、Inventory、Envelope 与状态转换门禁。"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .canonical import canonical_sha256
from .contracts import (
    CANDIDATE_DELIVERY_STAGES,
    DEPLOYMENT_STAGES,
    EVIDENCE_PACKAGE_V3_SCHEMA,
    FACT_DIMENSION,
    SCENARIO_FACT_STAGE,
    capability_key,
    persona_key,
    validate_fact_document,
    validate_object_document,
)
from .errors import ControlError

if TYPE_CHECKING:
    from .store import ControlStore


class WorkflowGates:
    """只根据不可变对象和追加事实作出准入判断。"""

    def __init__(self, store: "ControlStore") -> None:
        self.store = store

    @staticmethod
    def dimension_for(fact_kind: str) -> str:
        try:
            return FACT_DIMENSION[fact_kind]
        except KeyError as error:
            raise ControlError(f"未登记的 fact_kind：{fact_kind}") from error

    def _object(self, reference: dict[str, Any], expected_kind: str | None = None) -> dict[str, Any]:
        document = self.store.load_object(reference)
        if expected_kind is not None and document["object_kind"] != expected_kind:
            raise ControlError(
                f"对象引用类型错误：期望 {expected_kind}，实际 {document['object_kind']}"
            )
        return document["payload"]

    def _fact(self, reference: dict[str, Any], expected_kind: str | None = None) -> dict[str, Any]:
        fact = self.store.load_fact(reference)
        if expected_kind is not None and fact["fact_kind"] != expected_kind:
            raise ControlError(
                f"事实引用类型错误：期望 {expected_kind}，实际 {fact['fact_kind']}"
            )
        return fact

    @staticmethod
    def _same_persona(left: Any, right: Any, label: str) -> None:
        if persona_key(left) != persona_key(right):
            raise ControlError(f"{label} 的 Persona 不一致")

    def validate_new_object(self, document: dict[str, Any]) -> None:
        validate_object_document(document)
        self._validate_object_payload_graph(document["object_kind"], document["payload"])

    def validate_object_graph(self, reference: dict[str, Any]) -> None:
        document = self.store.load_object(reference)
        self._validate_object_payload_graph(document["object_kind"], document["payload"])

    def _validate_object_payload_graph(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "snapshot":
            schema = self._object(payload["profile_schema_ref"], "profile_schema")
            self._same_persona(payload["persona"], schema["persona"], "Snapshot/ProfileSchema")
            if payload["version"] != schema["version"]:
                raise ControlError("Snapshot 与 ProfileSchema 版本不一致")
        elif kind == "release_bundle":
            snapshot = self._object(payload["snapshot_ref"], "snapshot")
            self._require_release_coordinate(payload, snapshot, "ReleaseBundle/Snapshot")
        elif kind == "release_artifact":
            snapshot = self._object(payload["snapshot_ref"], "snapshot")
            bundle = self._object(payload["release_bundle_ref"], "release_bundle")
            self._require_release_coordinate(payload, snapshot, "ReleaseArtifact/Snapshot")
            self._require_release_coordinate(payload, bundle, "ReleaseArtifact/ReleaseBundle")
            if bundle["snapshot_ref"] != payload["snapshot_ref"]:
                raise ControlError("ReleaseArtifact 的 ReleaseBundle 未绑定同一 Snapshot")
        elif kind == "production_ingress_inventory":
            self._validate_ingress_closure(payload)
        elif kind == "egress_disposition_inventory":
            self._validate_egress_closure(payload)
        elif kind == "support_envelope":
            inventory = self._object(
                payload["production_ingress_inventory_ref"],
                "production_ingress_inventory",
            )
            self._same_persona(payload["persona"], inventory["persona"], "SupportEnvelope/Inventory")
            known_ingresses = {item["logical_ingress_id"] for item in inventory["entries"]}
            used_ingresses = {item["logical_ingress_id"] for item in payload["capabilities"]}
            unknown = sorted(used_ingresses - known_ingresses)
            if unknown:
                raise ControlError(f"SupportEnvelope 引用了未知生产入口：{unknown}")
        elif kind == "active_support_envelope":
            self._validate_active_support_envelope(payload)
        elif kind == "rollback_operational_envelope":
            if payload["rollback_release_ref"] is not None:
                release = self._object(payload["rollback_release_ref"], "release_artifact")
                self._same_persona(
                    payload["persona"], release["persona"], "RollbackEnvelope/Release"
                )
            self._object(
                payload["operational_bindings"]["selector_snapshot_ref"],
                "runtime_catalog_snapshot",
            )
        elif kind == "deployment_traffic_envelope":
            self._validate_deployment_traffic_envelope(payload)
        elif kind == "rule_migration_ledger":
            self._validate_rule_migration_ledger(payload)
        elif kind == "atomic_assertion_ledger":
            self._validate_atomic_assertion_ledger(payload)
        elif kind == "validation_execution_plan":
            self._validate_validation_execution_plan(payload)
        elif kind == "candidate_evidence_package":
            self._validate_candidate_evidence_package(payload)
        elif kind == "candidate_delivery_plan":
            self._validate_candidate_delivery_plan(payload)
        elif kind == "candidate_delivery_package":
            self._validate_candidate_delivery_package(payload)
        elif kind == "private_archive_manifest":
            campaign = self.store.load_campaign(payload["campaign_id"])
            self._same_persona(
                campaign["persona"], payload["persona"], "Campaign/PrivateArchiveManifest"
            )

    def _validate_rule_migration_ledger(self, payload: dict[str, Any]) -> None:
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        if package["schema_version"] != EVIDENCE_PACKAGE_V3_SCHEMA:
            raise ControlError("RuleMigrationLedger 只能绑定不含迁移结论的 EvidencePackage v3")
        self._same_persona(
            payload["persona"], package["persona"], "RuleMigrationLedger/EvidencePackage"
        )
        if payload["target_version"] != package["version"]:
            raise ControlError("RuleMigrationLedger 与 EvidencePackage 目标版本不一致")
        evidence_ids = {item["evidence_id"] for item in package["evidence_items"]}
        unknown = sorted(
            {
                evidence_id
                for rule in payload["rules"]
                for evidence_id in rule["evidence_item_ids"]
            }
            - evidence_ids
        )
        if unknown:
            raise ControlError(f"RuleMigrationLedger 引用了未知证据项：{unknown}")

    def _validate_atomic_assertion_ledger(self, payload: dict[str, Any]) -> None:
        ledger = self._object(
            payload["rule_migration_ledger_ref"], "rule_migration_ledger"
        )
        for key in ("campaign_id", "target_version"):
            if payload[key] != ledger[key]:
                raise ControlError(f"AtomicAssertionLedger 与 RuleMigrationLedger 的 {key} 不一致")
        self._same_persona(
            payload["persona"], ledger["persona"], "AtomicAssertionLedger/RuleMigrationLedger"
        )
        package = self._object(ledger["evidence_package_ref"], "evidence_package")
        evidence_ids = {item["evidence_id"] for item in package["evidence_items"]}
        unknown_evidence = sorted(
            {
                evidence_id
                for assertion in payload["assertions"]
                for evidence_id in assertion["evidence_item_ids"]
            }
            - evidence_ids
        )
        if unknown_evidence:
            raise ControlError(
                f"AtomicAssertionLedger 引用了未知证据项：{unknown_evidence}"
            )
        required_owners = {
            assertion["owner_id"]
            for assertion in payload["assertions"]
            if assertion["owner_kind"] == "required_rule"
        }
        target_specs = set(ledger["target_spec_ids"])
        if required_owners != target_specs:
            missing = sorted(target_specs - required_owners)
            extra = sorted(required_owners - target_specs)
            raise ControlError(
                "AtomicAssertionLedger 未唯一闭合目标 RequiredRules："
                f"missing={missing}, extra={extra}"
            )

    @staticmethod
    def _require_manifest_pass(payload: dict[str, Any], label: str) -> None:
        """要求通用操作证据的每个登记项都显式通过。"""

        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ControlError(f"{label} 缺少可复算证据项")
        failed = sorted(
            entry.get("id", "unknown")
            for entry in entries
            if not isinstance(entry, dict)
            or not isinstance(entry.get("facts"), dict)
            or entry["facts"].get("result") != "pass"
        )
        if failed:
            raise ControlError(f"{label} 存在未通过证据项：{failed}")

    @staticmethod
    def _manifest_has_failure(payload: dict[str, Any]) -> bool:
        """判断通用操作证据是否至少记录一项显式失败。"""

        entries = payload.get("entries")
        return isinstance(entries, list) and any(
            isinstance(entry, dict)
            and isinstance(entry.get("facts"), dict)
            and entry["facts"].get("result") == "failed"
            for entry in entries
        )

    def _candidate_build_matches(
        self,
        campaign: dict[str, Any],
        candidate_payload: dict[str, Any],
        build_receipt: dict[str, Any],
    ) -> None:
        expected = {
            "campaign_id": campaign["campaign_id"],
            "candidate_id": candidate_payload["candidate_id"],
            "candidate_purpose": candidate_payload["candidate_purpose"],
            "target_version": campaign["target_version"],
            "profile_approval_ref": candidate_payload["profile_approval_ref"],
            "release_artifact_ref": candidate_payload["release_artifact_ref"],
            "support_envelope_ref": candidate_payload["support_envelope_ref"],
        }
        for key, expected_value in expected.items():
            if build_receipt[key] != expected_value:
                raise ControlError(f"CandidateBuildReceipt 与 candidate_frozen 的 {key} 不一致")
        source = build_receipt["source_identity"]
        build = build_receipt["build_identity"]
        image = build_receipt["image"]
        for key in ("source_tree_sha256", "test_tree_sha256", "dependency_lock_sha256"):
            if source[key] != candidate_payload[key]:
                raise ControlError(f"CandidateBuildReceipt 与 candidate_frozen 的 {key} 不一致")
        for receipt_key, candidate_key in (
            ("build_id", "build_id"),
            ("target_architecture", "target_architecture"),
        ):
            if build[receipt_key] != candidate_payload[candidate_key]:
                raise ControlError(
                    f"CandidateBuildReceipt 与 candidate_frozen 的 {candidate_key} 不一致"
                )
        if image["image_digest"] != candidate_payload["image_digest"]:
            raise ControlError("CandidateBuildReceipt 与 candidate_frozen 的镜像摘要不一致")

    def _require_validation_gate_closure(
        self,
        *,
        campaign_id: str,
        candidate_id: str,
        candidate_ref: dict[str, Any],
        candidate_build_receipt_ref: dict[str, Any],
        validation_attempt_ref: dict[str, Any],
        validation_execution_plan_ref: dict[str, Any],
        supplied_refs: list[dict[str, Any]],
        plan: dict[str, Any],
    ) -> None:
        """要求 attempt 的外部门禁收据完整、唯一、通过且可重放。"""

        from .receipts import replay_receipt

        matching: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for reference in self.store.list_receipt_refs("validation_gate"):
            receipt = self.store.load_receipt(reference)
            if receipt["validation_attempt_ref"] == validation_attempt_ref:
                matching.append((reference, receipt))

        supplied_keys = {
            (reference["receipt_kind"], reference["sha256"])
            for reference in supplied_refs
        }
        matching_keys = {
            (reference["receipt_kind"], reference["sha256"])
            for reference, _receipt in matching
        }
        if supplied_keys != matching_keys or len(supplied_refs) != len(matching):
            raise ControlError(
                "Acceptance 外部门禁收据不是该 ValidationAttempt 的完整只写集合"
            )

        planned_by_id = {item["gate_id"]: item for item in plan["external_gates"]}
        actual_gate_ids = [receipt["gate_id"] for _reference, receipt in matching]
        if set(actual_gate_ids) != set(planned_by_id) or len(actual_gate_ids) != len(
            planned_by_id
        ):
            raise ControlError("外部门禁收据未唯一覆盖 ValidationExecutionPlan")

        expected_identity = {
            "campaign_id": campaign_id,
            "candidate_id": candidate_id,
            "candidate_ref": candidate_ref,
            "candidate_build_receipt_ref": candidate_build_receipt_ref,
            "validation_attempt_ref": validation_attempt_ref,
            "validation_execution_plan_ref": validation_execution_plan_ref,
        }
        for reference, receipt in matching:
            for key, expected in expected_identity.items():
                if receipt[key] != expected:
                    raise ControlError(f"外部门禁收据的 {key} 与当前验收链不一致")
            planned = planned_by_id[receipt["gate_id"]]
            for key in (
                "requirement_sha256",
                "source",
                "command",
                "working_directory",
                "host_id",
                "architecture",
                "reused_receipt_ref",
                "previous_failed_receipt_ref",
            ):
                if receipt[key] != planned[key]:
                    raise ControlError(
                        f"外部门禁收据 {receipt['gate_id']} 的 {key} 与冻结计划不一致"
                    )
            if receipt["result"] != "pass":
                raise ControlError("失败的外部门禁收据不得进入 Acceptance")
            replay_receipt(self.store, reference)

    def _require_candidate_delivery_receipt(
        self,
        candidate_ref: dict[str, Any],
        acceptance_ref: dict[str, Any],
    ) -> None:
        """严格 Candidate 进入生产晋升前必须先完成候选交付。"""

        from .receipts import replay_receipt

        matches: list[dict[str, Any]] = []
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if (
                receipt["candidate_ref"] == candidate_ref
                and receipt["acceptance_ref"] == acceptance_ref
            ):
                matches.append(reference)
        if len(matches) != 1:
            raise ControlError(
                "严格 Candidate 必须先形成唯一且可重放的候选交付收据"
            )
        replay_receipt(self.store, matches[0])

    def _validate_validation_execution_plan(self, payload: dict[str, Any]) -> None:
        from .receipts import replay_receipt

        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/ValidationExecutionPlan"
        )
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if candidate["campaign_id"] != payload["campaign_id"]:
            raise ControlError("ValidationExecutionPlan 的 Candidate 跨 Campaign")
        candidate_payload = candidate["payload"]
        if candidate_payload.get("schema_version") is None:
            raise ControlError("ValidationExecutionPlan 只能绑定严格 v2 Candidate")
        if candidate_payload["candidate_id"] != payload["candidate_id"]:
            raise ControlError("ValidationExecutionPlan 与 Candidate ID 不一致")
        if candidate_payload["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("ValidationExecutionPlan 与 Candidate 批准引用不一致")
        if candidate_payload["candidate_build_receipt_ref"] != payload[
            "candidate_build_receipt_ref"
        ]:
            raise ControlError("ValidationExecutionPlan 与 CandidateBuildReceipt 引用不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        if build["campaign_id"] != payload["campaign_id"]:
            raise ControlError("ValidationExecutionPlan 的 CandidateBuildReceipt 跨 Campaign")
        self._candidate_build_matches(campaign, candidate_payload, build)
        if build["validation_scope_sha256"] != payload["validation_scope_sha256"]:
            raise ControlError("ValidationExecutionPlan 与 VC-4 验证闭集摘要不一致")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        if approval["payload"]["scenario_plan_ref"] != payload["scenario_plan_ref"]:
            raise ControlError("ValidationExecutionPlan 未绑定批准的 ScenarioPlan")
        evidence_approval = self._fact(
            approval["payload"]["evidence_approval_ref"], "evidence_approved"
        )
        classification_ref = evidence_approval["payload"].get("classification_fact_ref")
        if classification_ref is None:
            raise ControlError("严格 ValidationExecutionPlan 要求 VC-2 AtomicAssertionLedger")
        classification = self._fact(classification_ref, "rule_classification_recorded")
        if classification["payload"]["atomic_assertion_ledger_ref"] != payload[
            "atomic_assertion_ledger_ref"
        ]:
            raise ControlError("ValidationExecutionPlan 未绑定 VC-2 AtomicAssertionLedger")
        selector = self._fact(
            payload["production_selector_before_ref"], "selector_observed"
        )
        self._object(selector["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot")
        if payload["run_conditions"]["tool_sha256"] != campaign["tool_bundle_sha256"]:
            raise ControlError("ValidationExecutionPlan 工具身份与 Campaign 不一致")
        for gate in payload["external_gates"]:
            if gate["source"] == "reuse":
                source_receipt = replay_receipt(
                    self.store, gate["reused_receipt_ref"]
                )
                expected_result = "pass"
                label = "复用门禁收据"
            elif gate["previous_failed_receipt_ref"] is not None:
                source_receipt = replay_receipt(
                    self.store, gate["previous_failed_receipt_ref"]
                )
                expected_result = "failed"
                label = "前序失败门禁收据"
                if (
                    source_receipt["campaign_id"] != payload["campaign_id"]
                    or source_receipt["candidate_ref"] != payload["candidate_ref"]
                    or source_receipt["candidate_build_receipt_ref"]
                    != payload["candidate_build_receipt_ref"]
                ):
                    raise ControlError(
                        f"{label}跨越了 Campaign／Candidate：{gate['gate_id']}"
                    )
            else:
                continue
            for key in (
                "gate_id",
                "requirement_sha256",
                "command",
                "working_directory",
                "host_id",
                "architecture",
            ):
                if source_receipt[key] != gate[key]:
                    raise ControlError(
                        f"{label}的 {key} 与计划不一致：{gate['gate_id']}"
                    )
            if source_receipt["result"] != expected_result:
                raise ControlError(
                    f"{label}的结果必须是 {expected_result}：{gate['gate_id']}"
                )

    def _validate_candidate_evidence_package(self, payload: dict[str, Any]) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateEvidencePackage"
        )
        if payload["target_version"] != campaign["target_version"]:
            raise ControlError("CandidateEvidencePackage 目标版本与 Campaign 不一致")
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        attempt = self._fact(payload["validation_attempt_ref"], "validation_attempt_created")
        if (
            candidate["campaign_id"] != payload["campaign_id"]
            or attempt["campaign_id"] != payload["campaign_id"]
        ):
            raise ControlError("CandidateEvidencePackage 引用了其他 Campaign 的事实")
        candidate_payload = candidate["payload"]
        attempt_payload = attempt["payload"]
        for key in ("candidate_id", "candidate_build_receipt_ref"):
            if payload[key] != candidate_payload[key]:
                raise ControlError(f"CandidateEvidencePackage 与 Candidate 的 {key} 不一致")
        if payload["attempt_id"] != attempt_payload["attempt_id"]:
            raise ControlError("CandidateEvidencePackage 与 ValidationAttempt ID 不一致")
        for key in ("candidate_ref", "candidate_build_receipt_ref"):
            if payload[key] != attempt_payload[key]:
                raise ControlError(f"CandidateEvidencePackage 与 ValidationAttempt 的 {key} 不一致")
        plan = self._object(
            attempt_payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        if payload["atomic_assertion_ledger_ref"] != plan["atomic_assertion_ledger_ref"]:
            raise ControlError("CandidateEvidencePackage 与执行计划断言台账不一致")
        scenario_plan = self._object(plan["scenario_plan_ref"], "scenario_plan")
        approvals = [
            self._fact(reference, "scenario_approved")
            for reference in payload["scenario_approval_refs"]
        ]
        actual_scenarios: list[str] = []
        for approval in approvals:
            item = approval["payload"]
            if (
                item.get("schema_version") is None
                or item["validation_attempt_ref"] != payload["validation_attempt_ref"]
                or item["candidate_id"] != payload["candidate_id"]
                or item["attempt_id"] != payload["attempt_id"]
            ):
                raise ControlError("CandidateEvidencePackage 场景批准跨越 Candidate／attempt")
            actual_scenarios.append(item["scenario_id"])
        expected_scenarios = [item["id"] for item in scenario_plan["scenarios"]]
        if sorted(actual_scenarios) != sorted(expected_scenarios) or len(
            actual_scenarios
        ) != len(set(actual_scenarios)):
            raise ControlError("CandidateEvidencePackage 未唯一覆盖 ScenarioPlan 全集")
        ledger = self._object(
            payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
        )
        expected_scenario_assertions = {
            item["assertion_id"]: item
            for item in ledger["assertions"]
            if item["owner_kind"] == "scenario_only"
        }
        actual_scenario_assertions = {
            item["assertion_id"]: item
            for item in payload["scenario_assertion_results"]
        }
        if set(actual_scenario_assertions) != set(expected_scenario_assertions):
            raise ControlError("CandidateEvidencePackage 未唯一闭合 scenario-only 原子断言")
        for assertion_id, expected in expected_scenario_assertions.items():
            if actual_scenario_assertions[assertion_id]["owner_id"] != expected["owner_id"]:
                raise ControlError(f"scenario-only 断言 owner 不一致：{assertion_id}")
        for key in (
            "environment_before_ref",
            "environment_after_ref",
            "recovery_evidence_ref",
            "secret_scan_evidence_ref",
        ):
            evidence = self._object(payload[key], "operational_evidence")
            self._require_manifest_pass(evidence, f"CandidateEvidencePackage/{key}")

    def _validate_candidate_delivery_plan(self, payload: dict[str, Any]) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateDeliveryPlan"
        )
        completed = self._fact(payload["validation_completed_ref"], "validation_completed")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if any(
            fact["campaign_id"] != payload["campaign_id"]
            for fact in (completed, acceptance, candidate)
        ):
            raise ControlError("CandidateDeliveryPlan 引用了其他 Campaign 的事实")
        candidate_payload = candidate["payload"]
        completed_payload = completed["payload"]
        for key in ("candidate_id", "candidate_ref", "candidate_build_receipt_ref", "acceptance_ref"):
            expected = completed_payload[key] if key in completed_payload else payload[key]
            if payload[key] != expected:
                raise ControlError(f"CandidateDeliveryPlan 与 VC-5 完成事实的 {key} 不一致")
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("CandidateDeliveryPlan 与 Acceptance Candidate 不一致")
        if payload["candidate_evidence_package_ref"] != completed_payload[
            "candidate_evidence_package_ref"
        ]:
            raise ControlError("CandidateDeliveryPlan 与 VC-5 CandidateEvidencePackage 不一致")
        if payload["candidate_image_digest"] != candidate_payload["image_digest"]:
            raise ControlError("CandidateDeliveryPlan 未使用 VC-4 固定 Candidate 镜像")
        if payload["architecture"] != candidate_payload["target_architecture"]:
            raise ControlError("CandidateDeliveryPlan 架构与 Candidate 不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        if payload["runtime_profile_sha256"] != build["profile_identity"]["profile_digest"]:
            raise ControlError("CandidateDeliveryPlan 运行 Profile 与 CandidateBuildReceipt 不一致")
        self._require_manifest_pass(
            self._object(payload["environment_freeze_ref"], "operational_evidence"),
            "CandidateDeliveryPlan/environment_freeze_ref",
        )
        for reference in payload["rollback_material_refs"]:
            self._require_manifest_pass(
                self._object(reference), "CandidateDeliveryPlan/rollback_material_refs"
            )

    def _validate_candidate_delivery_package(self, payload: dict[str, Any]) -> None:
        campaign = self.store.load_campaign(payload["campaign_id"])
        self._same_persona(
            campaign["persona"], payload["persona"], "Campaign/CandidateDeliveryPackage"
        )
        plan = self._object(payload["delivery_plan_ref"], "candidate_delivery_plan")
        if plan["campaign_id"] != payload["campaign_id"]:
            raise ControlError("CandidateDeliveryPackage 与交付计划 Campaign 不一致")
        for key in (
            "delivery_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "candidate_evidence_package_ref",
        ):
            if payload[key] != plan[key]:
                raise ControlError(f"CandidateDeliveryPackage 与交付计划的 {key} 不一致")
        if payload["candidate_image_digest"] != plan["candidate_image_digest"]:
            raise ControlError("CandidateDeliveryPackage 镜像与交付计划不一致")
        facts = [
            self._fact(reference, "candidate_delivery_recorded")
            for reference in payload["delivery_fact_refs"]
        ]
        if [fact["payload"]["stage"] for fact in facts] != list(
            CANDIDATE_DELIVERY_STAGES
        ):
            raise ControlError("CandidateDeliveryPackage 未绑定有序四阶段候选交付事实")
        for fact in facts:
            if fact["payload"]["delivery_plan_ref"] != payload["delivery_plan_ref"]:
                raise ControlError("CandidateDeliveryPackage 混入其他交付计划事实")
            if fact["payload"]["result"] != "pass":
                raise ControlError("CandidateDeliveryPackage 不得封装失败的候选交付阶段")
        if payload["rollback_material_refs"] != plan["rollback_material_refs"]:
            raise ControlError("CandidateDeliveryPackage 回退材料与冻结计划不一致")
        deployment_configuration = self._object(
            payload["deployment_configuration_ref"], "operational_evidence"
        )
        self._require_manifest_pass(
            deployment_configuration,
            "CandidateDeliveryPackage/deployment_configuration_ref",
        )
        for key in ("public_evidence_index_refs", "rollback_material_refs"):
            for reference in payload[key]:
                self._require_manifest_pass(
                    self._object(reference), f"CandidateDeliveryPackage/{key}"
                )
        expected_artifact_count = (
            1
            + len(payload["public_evidence_index_refs"])
            + len(payload["rollback_material_refs"])
        )
        if payload["artifact_count"] != expected_artifact_count:
            raise ControlError(
                "CandidateDeliveryPackage artifact_count 与部署配置、公开索引和回退材料不一致"
            )
        archive = self._object(
            payload["private_archive_manifest_ref"], "private_archive_manifest"
        )
        if archive["campaign_id"] != payload["campaign_id"]:
            raise ControlError("CandidateDeliveryPackage 私有归档清单跨 Campaign")
        self._same_persona(payload["persona"], archive["persona"], "DeliveryPackage/Archive")

    def _require_release_coordinate(
        self, left: dict[str, Any], right: dict[str, Any], label: str
    ) -> None:
        self._same_persona(left["persona"], right["persona"], label)
        for key in ("version", "profile_digest"):
            if left[key] != right[key]:
                raise ControlError(f"{label} 的 {key} 不一致")

    def _validate_ingress_closure(self, inventory: dict[str, Any]) -> None:
        observation = self._object(inventory["observation_ref"], "ingress_observation")
        self._same_persona(inventory["persona"], observation["persona"], "Ingress Inventory")
        observed = {item["alias_id"]: item for item in observation["aliases"]}
        claimed: dict[str, str] = {}
        for entry in inventory["entries"]:
            union_callers: set[str] = set()
            for alias_id in entry["physical_alias_ids"]:
                if alias_id not in observed:
                    raise ControlError(f"Inventory 声明了未发现的入口别名：{alias_id}")
                if alias_id in claimed:
                    raise ControlError(f"入口物理别名被重复处置：{alias_id}")
                alias = observed[alias_id]
                if alias["logical_ingress_id"] != entry["logical_ingress_id"]:
                    raise ControlError(f"入口别名归属与逻辑入口不一致：{alias_id}")
                claimed[alias_id] = entry["logical_ingress_id"]
                union_callers.update(alias["caller_ids"])
            if entry["caller_ids"] != sorted(union_callers):
                raise ControlError(
                    f"入口 {entry['logical_ingress_id']} 的调用方没有与发现清单闭合"
                )
        missing = sorted(set(observed) - set(claimed))
        if missing:
            raise ControlError(f"ProductionIngressInventory 遗漏物理别名：{missing}")

    def _validate_egress_closure(self, inventory: dict[str, Any]) -> None:
        observation = self._object(inventory["observation_ref"], "egress_observation")
        self._same_persona(inventory["persona"], observation["persona"], "Egress Inventory")
        observed = {item["egress_id"]: item for item in observation["egresses"]}
        claimed = {item["egress_id"]: item for item in inventory["entries"]}
        missing = sorted(set(observed) - set(claimed))
        extra = sorted(set(claimed) - set(observed))
        if missing or extra:
            raise ControlError(
                f"EgressDispositionInventory 未闭合：missing={missing}, extra={extra}"
            )
        for egress_id, observation_item in observed.items():
            entry = claimed[egress_id]
            if observation_item["oauth_related"] and not entry["current_disposition"]:
                raise ControlError(f"OAuth 出站未处置：{egress_id}")

    def _validate_active_support_envelope(self, payload: dict[str, Any]) -> None:
        support = self._object(payload["support_envelope_ref"], "support_envelope")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        self._same_persona(payload["persona"], support["persona"], "Active/Support Envelope")
        self._same_persona(payload["persona"], release["persona"], "Active Envelope/Release")
        approval_payload = approval["payload"]
        acceptance_payload = acceptance["payload"]
        if approval_payload["approval_purpose"] != "production_replacement":
            raise ControlError("ActiveSupportEnvelope 不能来自 validation-only 批准")
        if acceptance_payload["result"] != "accepted":
            raise ControlError("ActiveSupportEnvelope 必须来自 production-replacement Acceptance")
        if acceptance_payload.get("schema_version") is not None:
            acceptance_ref = payload["acceptance_ref"]
            completed = [
                fact
                for fact in self.store.list_facts(acceptance["campaign_id"], "validation")
                if fact["fact_kind"] == "validation_completed"
                and fact["payload"]["acceptance_ref"] == acceptance_ref
            ]
            if len(completed) != 1:
                raise ControlError("严格 Acceptance 必须先形成唯一 VC-5 完成事实")
        if approval_payload["support_envelope_ref"] != payload["support_envelope_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 ProfileApproval 的范围不一致")
        if approval_payload["release_artifact_ref"] != payload["release_artifact_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 ProfileApproval 的 Release 不一致")
        if acceptance_payload["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("ActiveSupportEnvelope 与 Acceptance 的批准引用不一致")
        support_set = {capability_key(item) for item in support["capabilities"]}
        active_set = {capability_key(item) for item in payload["capabilities"]}
        if active_set != support_set:
            raise ControlError("ActiveSupportEnvelope 必须精确等于已批准且验收的 SupportEnvelope")

    def _validate_deployment_traffic_envelope(self, payload: dict[str, Any]) -> None:
        active = self._object(payload["active_support_envelope_ref"], "active_support_envelope")
        rollback = self._object(
            payload["rollback_operational_envelope_ref"],
            "rollback_operational_envelope",
        )
        inventory = self._object(
            payload["production_ingress_inventory_ref"],
            "production_ingress_inventory",
        )
        self._same_persona(payload["persona"], active["persona"], "Deployment/Active Envelope")
        self._same_persona(payload["persona"], rollback["persona"], "Deployment/Rollback Envelope")
        self._same_persona(payload["persona"], inventory["persona"], "Deployment/Inventory")
        if active["support_envelope_ref"] is None:
            raise ControlError("ActiveSupportEnvelope 缺少来源")
        deployment = {capability_key(item) for item in payload["capabilities"]}
        active_set = {capability_key(item) for item in active["capabilities"]}
        rollback_set = {capability_key(item) for item in rollback["capabilities"]}
        gap = sorted(deployment - (active_set & rollback_set))
        if gap:
            raise ControlError(
                "DeploymentTrafficEnvelope 不属于 Active 与 Rollback 范围交集："
                f"{gap}"
            )
        known_ingresses = {item["logical_ingress_id"] for item in inventory["entries"]}
        unknown = sorted(
            {item["logical_ingress_id"] for item in payload["capabilities"]}
            - known_ingresses
        )
        if unknown:
            raise ControlError(f"DeploymentTrafficEnvelope 引用了未知入口：{unknown}")

    def validate_append(self, campaign: dict[str, Any], document: dict[str, Any]) -> None:
        validate_fact_document(document)
        facts = self.store.list_facts(campaign["campaign_id"])
        facts_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            facts_by_kind[fact["fact_kind"]].append(fact)
        self._validate_fact_semantics(campaign, document, facts_by_kind)

    def _validate_fact_semantics(
        self,
        campaign: dict[str, Any],
        document: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        kind = document["fact_kind"]
        payload = document["payload"]
        if kind == "discovery_recorded":
            self._gate_discovery(campaign, payload, facts_by_kind)
        elif kind == "evidence_recorded":
            self._gate_evidence_recorded(campaign, payload, facts_by_kind)
        elif kind == "rule_classification_recorded":
            self._gate_rule_classification(campaign, payload, facts_by_kind)
        elif kind == "evidence_approved":
            self._gate_evidence_approved(payload, facts_by_kind)
        elif kind == "profile_approved":
            self._gate_profile_approved(campaign, payload, facts_by_kind)
        elif kind == "candidate_frozen":
            self._gate_candidate(campaign, payload, facts_by_kind)
        elif kind in SCENARIO_FACT_STAGE:
            self._gate_scenario(kind, payload, facts_by_kind)
        elif kind == "pair_recorded":
            self._gate_pair(payload, facts_by_kind)
        elif kind == "acceptance_recorded":
            self._gate_acceptance(payload, facts_by_kind)
        elif kind == "validation_attempt_created":
            self._gate_validation_attempt(campaign, payload, facts_by_kind)
        elif kind == "validation_completed":
            self._gate_validation_completed(campaign, payload, facts_by_kind)
        elif kind in {"selector_observed", "selector_activated"}:
            self._gate_selector(kind, payload, facts_by_kind)
        elif kind == "release_promoted":
            self._gate_release_promoted(payload, facts_by_kind)
        elif kind in DEPLOYMENT_STAGES:
            self._gate_deployment_stage(kind, payload, facts_by_kind)
        elif kind == "inventory_current_appended":
            self._gate_inventory_current(payload, facts_by_kind)
        elif kind == "candidate_delivery_recorded":
            self._gate_candidate_delivery(payload, facts_by_kind)

    def _gate_discovery(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        if payload["version"] != campaign["target_version"]:
            raise ControlError("Discovery 版本与不可变 Campaign 目标不一致")
        if payload["tool_sha256"] != campaign["tool_bundle_sha256"]:
            raise ControlError("Discovery 工具身份与 Campaign 不一致")
        if facts_by_kind["discovery_recorded"]:
            raise ControlError("同一 Campaign 不得覆盖 DiscoveryFact")

    def _gate_evidence_recorded(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        discovery = self._fact(payload["discovery_fact_ref"], "discovery_recorded")
        if discovery["campaign_id"] != campaign["campaign_id"]:
            raise ControlError("EvidenceFact 引用了其他 Campaign 的 Discovery")
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        self._same_persona(campaign["persona"], package["persona"], "Campaign/EvidencePackage")
        if package["version"] != campaign["target_version"]:
            raise ControlError("EvidencePackage 版本与 Campaign 不一致")
        if package["producer_tool_sha256"] != campaign["tool_bundle_sha256"]:
            raise ControlError("EvidencePackage 工具身份漂移，必须新建 Campaign")
        if package["official_artifacts"] != campaign["official_artifacts"]:
            raise ControlError("EvidencePackage 官方产物与 Campaign 身份不一致")
        if facts_by_kind["evidence_recorded"]:
            raise ControlError("同一 Campaign 不得原位替换 EvidencePackage")

    def _gate_rule_classification(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence_fact = self._fact(payload["evidence_fact_ref"], "evidence_recorded")
        if evidence_fact["payload"]["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("规则分类未绑定 EvidenceFact 中的同一 EvidencePackage")
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        if package["schema_version"] != EVIDENCE_PACKAGE_V3_SCHEMA:
            raise ControlError("rule_classification_recorded 只适用于 EvidencePackage v3")
        ledger = self._object(
            payload["rule_migration_ledger_ref"], "rule_migration_ledger"
        )
        atomic = self._object(
            payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
        )
        if ledger["campaign_id"] != campaign["campaign_id"]:
            raise ControlError("RuleMigrationLedger 与 Campaign 身份不一致")
        if ledger["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("RuleMigrationLedger 未绑定 VC-1 的 EvidencePackage")
        if atomic["rule_migration_ledger_ref"] != payload["rule_migration_ledger_ref"]:
            raise ControlError("AtomicAssertionLedger 未绑定同一 RuleMigrationLedger")
        for label, value in (("RuleMigrationLedger", ledger), ("AtomicAssertionLedger", atomic)):
            self._same_persona(campaign["persona"], value["persona"], f"Campaign/{label}")
            if value["target_version"] != campaign["target_version"]:
                raise ControlError(f"{label} 目标版本与 Campaign 不一致")
        blocked = sorted(
            rule["spec_id"]
            for rule in ledger["rules"]
            if rule["evidence_level"] == "blocked"
        )
        if blocked:
            raise ControlError(f"VC-2 分类仍包含 blocked 规则：{blocked}")
        self.validate_object_graph(payload["rule_migration_ledger_ref"])
        self.validate_object_graph(payload["atomic_assertion_ledger_ref"])
        if facts_by_kind["rule_classification_recorded"]:
            raise ControlError("同一 Campaign 不得覆盖规则分类事实")

    def _gate_evidence_approved(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence = self._fact(payload["evidence_fact_ref"], "evidence_recorded")
        if evidence["payload"]["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("EvidenceApproval 未批准 EvidenceFact 中的同一 EvidencePackage")
        package = self._object(payload["evidence_package_ref"], "evidence_package")
        classification_ref = payload.get("classification_fact_ref")
        if package["schema_version"] == EVIDENCE_PACKAGE_V3_SCHEMA:
            if classification_ref is None:
                raise ControlError("EvidencePackage v3 必须先绑定 VC-2 规则分类事实")
            classification = self._fact(
                classification_ref, "rule_classification_recorded"
            )
            classification_payload = classification["payload"]
            if (
                classification_payload["evidence_fact_ref"]
                != payload["evidence_fact_ref"]
                or classification_payload["evidence_package_ref"]
                != payload["evidence_package_ref"]
            ):
                raise ControlError("EvidenceApproval 与规则分类事实没有绑定同一 VC-1 证据")
        elif classification_ref is not None:
            raise ControlError("历史 EvidencePackage v1/v2 不得伪造 VC-2 分类事实引用")
        if facts_by_kind["evidence_approved"]:
            raise ControlError("Evidence 批准变化必须建立新的 Campaign，禁止覆盖")

    def _gate_profile_approved(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence_approval = self._fact(payload["evidence_approval_ref"], "evidence_approved")
        evidence = self._object(
            evidence_approval["payload"]["evidence_package_ref"], "evidence_package"
        )
        migration_ledger: dict[str, Any] | None = None
        if evidence["schema_version"] == EVIDENCE_PACKAGE_V3_SCHEMA:
            classification = self._fact(
                evidence_approval["payload"]["classification_fact_ref"],
                "rule_classification_recorded",
            )
            migration_ledger = self._object(
                classification["payload"]["rule_migration_ledger_ref"],
                "rule_migration_ledger",
            )
            self.validate_object_graph(
                classification["payload"]["atomic_assertion_ledger_ref"]
            )
        descriptor = self._object(payload["persona_descriptor_ref"], "persona_descriptor")
        profile_schema = self._object(payload["profile_schema_ref"], "profile_schema")
        snapshot = self._object(payload["snapshot_ref"], "snapshot")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        support = self._object(payload["support_envelope_ref"], "support_envelope")
        ingress = self._object(
            payload["production_ingress_inventory_ref"], "production_ingress_inventory"
        )
        egress = self._object(
            payload["egress_disposition_inventory_ref"], "egress_disposition_inventory"
        )
        deployment_plan = self._object(
            payload["deployment_plan_ref"], "deployment_plan"
        )
        scenario_plan = self._object(payload["scenario_plan_ref"], "scenario_plan")
        for label, value in (
            ("Descriptor", descriptor),
            ("ProfileSchema", profile_schema),
            ("Snapshot", snapshot),
            ("Release", release),
            ("SupportEnvelope", support),
            ("IngressInventory", ingress),
            ("EgressInventory", egress),
            ("DeploymentPlan", deployment_plan),
            ("ScenarioPlan", scenario_plan),
        ):
            self._same_persona(campaign["persona"], value["persona"], f"Campaign/{label}")
        for label, value in (
            ("ProfileSchema", profile_schema),
            ("Snapshot", snapshot),
            ("Release", release),
        ):
            if value["version"] != campaign["target_version"]:
                raise ControlError(f"{label} 版本与 Campaign 不一致")
        self.validate_object_graph(payload["production_ingress_inventory_ref"])
        self.validate_object_graph(payload["egress_disposition_inventory_ref"])
        self.validate_object_graph(payload["support_envelope_ref"])
        self.validate_object_graph(payload["release_artifact_ref"])
        target_specs = set(payload["target_spec_ids"])
        if target_specs != set(support["target_spec_ids"]):
            raise ControlError("ProfileApproval 与 SupportEnvelope 的目标规则全集不一致")
        support_capabilities = {
            capability_key(item) for item in support["capabilities"]
        }
        planned_active = {
            capability_key(item)
            for item in deployment_plan["active_support_capabilities"]
        }
        if planned_active != support_capabilities:
            raise ControlError("DeploymentPlan 的 Active 范围与 SupportEnvelope 不一致")
        if migration_ledger is None:
            evidence_rules = {rule["spec_id"]: rule for rule in evidence["rules"]}
        else:
            if target_specs != set(migration_ledger["target_spec_ids"]):
                raise ControlError(
                    "ProfileApproval 与 RuleMigrationLedger 的目标规则全集不一致"
                )
            evidence_rules = {
                rule["spec_id"]: rule
                for rule in migration_ledger["rules"]
                if rule["spec_id"] in set(migration_ledger["target_spec_ids"])
            }
        missing_rules = sorted(target_specs - set(evidence_rules))
        if missing_rules:
            source = "RuleMigrationLedger" if migration_ledger is not None else "EvidencePackage"
            raise ControlError(f"ProfileApproval 引用了 {source} 不存在的规则：{missing_rules}")
        for spec_id in sorted(target_specs):
            rule = evidence_rules[spec_id]
            if rule["compatibility_class"] != "request_egress":
                raise ControlError(f"SupportEnvelope 目标规则不是 request_egress：{spec_id}")
            if (
                payload["approval_purpose"] == "production_replacement"
                and rule["evidence_level"] != "verified"
            ):
                raise ControlError(
                    f"production_replacement 规则没有达到 verified：{spec_id}={rule['evidence_level']}"
                )
        ingress_ids = {item["logical_ingress_id"] for item in ingress["entries"]}
        ingress_targets = {
            item["logical_ingress_id"]: item
            for item in payload["ingress_target_dispositions"]
        }
        if set(ingress_targets) != ingress_ids:
            raise ControlError("入口 target_disposition 没有覆盖完整当前 Inventory")
        migrated_ids = {
            ingress_id
            for ingress_id, item in ingress_targets.items()
            if item["target_disposition"] == "migrated_strict"
        }
        support_ids = {item["logical_ingress_id"] for item in support["capabilities"]}
        if support_ids != migrated_ids:
            raise ControlError(
                "SupportEnvelope 只能且必须覆盖 target_disposition=migrated_strict 的入口"
            )
        planned_specs = {
            spec_id
            for scenario in scenario_plan["scenarios"]
            for spec_id in scenario["spec_ids"]
        }
        if planned_specs != target_specs:
            raise ControlError("ScenarioPlan 没有精确覆盖批准的目标规则全集")
        migrated_protocols = {
            item["protocol_class"]
            for item in ingress["entries"]
            if item["logical_ingress_id"] in migrated_ids
        }
        planned_protocols = {
            protocol
            for scenario in scenario_plan["scenarios"]
            for protocol in scenario["ingress_protocol_classes"]
        }
        if not migrated_protocols.issubset(planned_protocols):
            raise ControlError("ScenarioPlan 遗漏 migrated_strict 入口协议类别")
        egress_ids = {item["egress_id"] for item in egress["entries"]}
        egress_targets = {item["egress_id"]: item for item in payload["egress_target_dispositions"]}
        if set(egress_targets) != egress_ids:
            raise ControlError("出站 target_disposition 没有覆盖完整当前 Inventory")
        for item in egress_targets.values():
            if item["target_disposition"] == "persona_strict":
                unknown_specs = sorted(set(item["spec_ids"]) - target_specs)
                if unknown_specs:
                    raise ControlError(f"strict 出站绑定了范围外 SPEC：{unknown_specs}")
        identities = {
            fact["payload"]["identity_sha256"]
            for fact in facts_by_kind["profile_approved"]
        }
        if payload["identity_sha256"] in identities:
            raise ControlError("相同 ProfileApprovalFact 不得重复追加")

    def _gate_candidate(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        approval_payload = approval["payload"]
        if payload["release_artifact_ref"] != approval_payload["release_artifact_ref"]:
            raise ControlError("Candidate 没有引用批准的 ReleaseArtifact")
        if payload["support_envelope_ref"] != approval_payload["support_envelope_ref"]:
            raise ControlError("Candidate 没有冻结批准的 SupportEnvelope")
        if payload["candidate_purpose"] != approval_payload["approval_purpose"]:
            raise ControlError("Candidate 用途与 ProfileApproval 不一致")
        release = self._object(payload["release_artifact_ref"], "release_artifact")
        self._same_persona(campaign["persona"], release["persona"], "Campaign/Candidate Release")
        if payload.get("schema_version") is not None:
            build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
            self._candidate_build_matches(campaign, payload, build)
        existing_ids = {
            fact["payload"]["candidate_id"] for fact in facts_by_kind["candidate_frozen"]
        }
        existing_identities = {
            fact["payload"]["identity_sha256"] for fact in facts_by_kind["candidate_frozen"]
        }
        if payload["candidate_id"] in existing_ids or payload["identity_sha256"] in existing_identities:
            raise ControlError("Candidate ID 或不可变身份重复，禁止覆盖")

    def _candidate_by_id(
        self, candidate_id: str, facts_by_kind: dict[str, list[dict[str, Any]]]
    ) -> dict[str, Any]:
        matches = [
            fact
            for fact in facts_by_kind["candidate_frozen"]
            if fact["payload"]["candidate_id"] == candidate_id
        ]
        if len(matches) != 1:
            raise ControlError(f"candidate_id 没有唯一冻结事实：{candidate_id}")
        return matches[0]

    def _gate_scenario(
        self,
        kind: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._candidate_by_id(payload["candidate_id"], facts_by_kind)
        if payload.get("schema_version") is not None:
            attempt = self._fact(
                payload["validation_attempt_ref"], "validation_attempt_created"
            )
            if (
                attempt["payload"]["candidate_ref"] != self.store.fact_ref(candidate)
                or attempt["payload"]["candidate_id"] != payload["candidate_id"]
                or attempt["payload"]["attempt_id"] != payload["attempt_id"]
            ):
                raise ControlError("严格场景事实未绑定同一 ValidationAttempt／Candidate")
        stage = SCENARIO_FACT_STAGE[kind]
        stages = ("prepare", "capture", "seal", "approve")
        identity = (
            payload["candidate_id"],
            payload["scenario_id"],
            payload["attempt_id"],
        )
        fact_kind_by_stage = {value: key for key, value in SCENARIO_FACT_STAGE.items()}
        for existing_kind in (fact_kind_by_stage[item] for item in stages):
            for fact in facts_by_kind[existing_kind]:
                existing = fact["payload"]
                if (
                    existing["candidate_id"],
                    existing["scenario_id"],
                    existing["attempt_id"],
                ) == identity and existing_kind == kind:
                    raise ControlError(f"场景阶段不得覆盖：{identity}/{stage}")
        if stage == "prepare":
            return
        previous_stage = stages[stages.index(stage) - 1]
        previous = self._fact(
            payload["previous_stage_ref"], fact_kind_by_stage[previous_stage]
        )
        previous_payload = previous["payload"]
        previous_identity = (
            previous_payload["candidate_id"],
            previous_payload["scenario_id"],
            previous_payload["attempt_id"],
        )
        if previous_identity != identity:
            raise ControlError("场景四阶段跨越了 candidate/scenario/attempt 边界")
        required_result = "prepared" if previous_stage == "prepare" else "pass"
        if previous_payload["result"] != required_result:
            raise ControlError("失败 attempt 不得继续推进，必须建立新 attempt")

    def _gate_pair(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._candidate_by_id(payload["candidate_id"], facts_by_kind)
        if payload["release_artifact_ref"] != candidate["payload"]["release_artifact_ref"]:
            raise ControlError("PAIR 与 Candidate Release 不一致")
        if payload.get("schema_version") is not None:
            if payload["profile_approval_ref"] != candidate["payload"]["profile_approval_ref"]:
                raise ControlError("严格 PAIR 与 Candidate 的 ProfileApproval 不一致")
            package = self._object(
                payload["candidate_evidence_package_ref"],
                "candidate_evidence_package",
            )
            if (
                package["candidate_id"] != payload["candidate_id"]
                or package["candidate_ref"] != self.store.fact_ref(candidate)
            ):
                raise ControlError("严格 PAIR 未绑定同一 CandidateEvidencePackage")
            if payload["atomic_assertion_ledger_ref"] != package[
                "atomic_assertion_ledger_ref"
            ]:
                raise ControlError("严格 PAIR 与 CandidateEvidencePackage 的断言台账不一致")
            ledger = self._object(
                payload["atomic_assertion_ledger_ref"], "atomic_assertion_ledger"
            )
            expected_assertions = {
                item["assertion_id"]
                for item in ledger["assertions"]
                if item["owner_kind"] == "required_rule"
                and item["owner_id"] == payload["spec_id"]
            }
            actual_assertions = {
                item["assertion_id"] for item in payload["assertion_results"]
            }
            if actual_assertions != expected_assertions:
                raise ControlError("严格 PAIR 未唯一闭合本规则的原子断言")
            migration = self._object(
                ledger["rule_migration_ledger_ref"], "rule_migration_ledger"
            )
            expected_source = (
                "execute" if payload["spec_id"] in migration["affected_rules"] else "reuse"
            )
            if payload["result_source"] != expected_source or any(
                item["source"] != expected_source for item in payload["assertion_results"]
            ):
                raise ControlError("严格 PAIR 的 execute／reuse 来源与 RuleMigrationLedger 不一致")
        for reference in payload["scenario_approval_refs"]:
            scenario = self._fact(reference, "scenario_approved")
            if scenario["payload"]["candidate_id"] != payload["candidate_id"]:
                raise ControlError("PAIR 引用了其他 Candidate 的场景批准")
            if payload.get("schema_version") is not None and reference not in package[
                "scenario_approval_refs"
            ]:
                raise ControlError("严格 PAIR 引用了 CandidateEvidencePackage 范围外场景")
        if any(
            fact["payload"]["pair_id"] == payload["pair_id"]
            and fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in facts_by_kind["pair_recorded"]
        ):
            raise ControlError("同一 Candidate 的 PAIR 不得重复或覆盖")
        approval = self._fact(candidate["payload"]["profile_approval_ref"], "profile_approved")
        approval_payload = approval["payload"]
        if payload["spec_id"] not in approval_payload["target_spec_ids"]:
            raise ControlError("PAIR 不属于已批准目标规则全集")
        ingress = self._object(
            approval_payload["production_ingress_inventory_ref"],
            "production_ingress_inventory",
        )
        target = {
            item["logical_ingress_id"]: item["target_disposition"]
            for item in approval_payload["ingress_target_dispositions"]
        }
        migrated = [
            item
            for item in ingress["entries"]
            if target[item["logical_ingress_id"]] == "migrated_strict"
        ]
        official_ids = {
            item["logical_ingress_id"] for item in migrated if item["ingress_kind"] == "official"
        }
        if payload["official_result"]["ingress_id"] not in official_ids:
            raise ControlError("PAIR 官方结果没有命中 migrated_strict 官方入口")
        third_party_by_protocol: dict[str, set[str]] = defaultdict(set)
        for item in migrated:
            if item["ingress_kind"] == "third_party":
                third_party_by_protocol[item["protocol_class"]].add(item["logical_ingress_id"])
        actual_protocols = {
            item["protocol_class"]: item["ingress_id"]
            for item in payload["third_party_results"]
        }
        if set(actual_protocols) != set(third_party_by_protocol):
            raise ControlError("PAIR 未覆盖每类 lossless 第三方标准 API 入口")
        for protocol, ingress_id in actual_protocols.items():
            if ingress_id not in third_party_by_protocol[protocol]:
                raise ControlError(f"PAIR 第三方入口与协议类别不一致：{protocol}/{ingress_id}")

    def _gate_acceptance(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        if candidate["payload"]["candidate_id"] != payload["candidate_id"]:
            raise ControlError("Acceptance 的 Candidate ID 与引用不一致")
        if candidate["payload"]["profile_approval_ref"] != payload["profile_approval_ref"]:
            raise ControlError("Acceptance 与 Candidate 的 ProfileApproval 不一致")
        approval = self._fact(payload["profile_approval_ref"], "profile_approved")
        if approval["payload"]["approval_purpose"] != payload["acceptance_purpose"]:
            raise ControlError("Acceptance 用途与 ProfileApproval 不一致")
        pair_facts = [self._fact(reference, "pair_recorded") for reference in payload["pair_refs"]]
        if payload.get("schema_version") is not None:
            if candidate["payload"].get("schema_version") is None:
                raise ControlError("严格 Acceptance 只能绑定严格 v2 Candidate")
            if payload["candidate_build_receipt_ref"] != candidate["payload"][
                "candidate_build_receipt_ref"
            ]:
                raise ControlError("Acceptance 与 CandidateBuildReceipt 不一致")
            attempt = self._fact(
                payload["validation_attempt_ref"], "validation_attempt_created"
            )
            if attempt["payload"]["candidate_ref"] != payload["candidate_ref"]:
                raise ControlError("Acceptance 与 ValidationAttempt Candidate 不一致")
            package = self._object(
                payload["candidate_evidence_package_ref"],
                "candidate_evidence_package",
            )
            if (
                package["validation_attempt_ref"] != payload["validation_attempt_ref"]
                or package["candidate_ref"] != payload["candidate_ref"]
                or package["candidate_build_receipt_ref"]
                != payload["candidate_build_receipt_ref"]
            ):
                raise ControlError("Acceptance 与 CandidateEvidencePackage 身份不一致")
            if package["atomic_assertion_ledger_ref"] != payload[
                "atomic_assertion_ledger_ref"
            ]:
                raise ControlError("Acceptance 与 AtomicAssertionLedger 不一致")
            for pair in pair_facts:
                pair_payload = pair["payload"]
                if pair_payload.get("schema_version") is None:
                    raise ControlError("严格 Acceptance 不得消费历史 PAIR")
                if (
                    pair_payload["profile_approval_ref"] != payload["profile_approval_ref"]
                    or pair_payload["candidate_evidence_package_ref"]
                    != payload["candidate_evidence_package_ref"]
                    or pair_payload["atomic_assertion_ledger_ref"]
                    != payload["atomic_assertion_ledger_ref"]
                ):
                    raise ControlError("严格 Acceptance 混入其他 Profile／证据／断言台账的 PAIR")
            plan = self._object(
                attempt["payload"]["validation_execution_plan_ref"],
                "validation_execution_plan",
            )
            expected_gates = {item["gate_id"] for item in plan["external_gates"]}
            gate_receipts = [
                self.store.load_receipt(reference)
                for reference in payload["external_gate_receipt_refs"]
            ]
            actual_gates = [item["gate_id"] for item in gate_receipts]
            if set(actual_gates) != expected_gates or len(actual_gates) != len(expected_gates):
                raise ControlError("Acceptance 外部门禁收据未唯一覆盖执行计划")
            for gate in gate_receipts:
                if (
                    gate["result"] != "pass"
                    or gate["validation_attempt_ref"] != payload["validation_attempt_ref"]
                    or gate["candidate_build_receipt_ref"]
                    != payload["candidate_build_receipt_ref"]
                ):
                    raise ControlError("Acceptance 存在失败或身份漂移的外部门禁收据")
            self._require_validation_gate_closure(
                campaign_id=candidate["campaign_id"],
                candidate_id=payload["candidate_id"],
                candidate_ref=payload["candidate_ref"],
                candidate_build_receipt_ref=payload[
                    "candidate_build_receipt_ref"
                ],
                validation_attempt_ref=payload["validation_attempt_ref"],
                validation_execution_plan_ref=attempt["payload"][
                    "validation_execution_plan_ref"
                ],
                supplied_refs=payload["external_gate_receipt_refs"],
                plan=plan,
            )
        for pair in pair_facts:
            if pair["payload"]["candidate_id"] != payload["candidate_id"]:
                raise ControlError("Acceptance 引用了其他 Candidate 的 PAIR")
        expected_specs = set(approval["payload"]["target_spec_ids"])
        actual_specs = [pair["payload"]["spec_id"] for pair in pair_facts]
        if set(actual_specs) != expected_specs or len(actual_specs) != len(expected_specs):
            raise ControlError("Acceptance 的逐规则 PAIR 未唯一覆盖目标全集")
        support = self._object(approval["payload"]["support_envelope_ref"], "support_envelope")
        expected_boundaries = {
            canonical_sha256(item) for item in support["boundary_assertion_refs"]
        }
        actual_boundaries = {
            canonical_sha256(item) for item in payload["boundary_assertion_refs"]
        }
        if actual_boundaries != expected_boundaries:
            raise ControlError("Acceptance 未精确绑定 SupportEnvelope 的范围外 fail-close 断言")
        if payload.get("schema_version") is not None:
            for reference in payload["inventory_assertion_refs"]:
                evidence = self._object(reference)
                self._require_manifest_pass(evidence, "Acceptance/inventory_assertion_refs")
        if any(
            fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in facts_by_kind["acceptance_recorded"]
        ):
            raise ControlError("同一 Candidate 的 AcceptanceFact 不得覆盖")

    def _gate_validation_attempt(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        candidate_payload = candidate["payload"]
        if candidate_payload.get("schema_version") is None:
            raise ControlError("ValidationAttempt 只能绑定严格 v2 Candidate")
        if (
            candidate_payload["candidate_id"] != payload["candidate_id"]
            or candidate_payload["candidate_build_receipt_ref"]
            != payload["candidate_build_receipt_ref"]
        ):
            raise ControlError("ValidationAttempt 与 Candidate／构建收据不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        plan = self._object(
            payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        expected = {
            "candidate_id": payload["candidate_id"],
            "candidate_ref": payload["candidate_ref"],
            "candidate_build_receipt_ref": payload["candidate_build_receipt_ref"],
            "run_conditions": payload["run_conditions"],
            "production_selector_before_ref": payload["production_selector_before_ref"],
            "vircs_state": payload["vircs_state"],
        }
        for key, expected_value in expected.items():
            if plan[key] != expected_value:
                raise ControlError(f"ValidationAttempt 与 ValidationExecutionPlan 的 {key} 不一致")
        if any(
            fact["payload"]["attempt_id"] == payload["attempt_id"]
            or fact["payload"]["validation_execution_plan_ref"]
            == payload["validation_execution_plan_ref"]
            for fact in facts_by_kind["validation_attempt_created"]
        ):
            raise ControlError("ValidationAttempt ID 或执行计划不得重复使用")

    def _gate_validation_completed(
        self,
        campaign: dict[str, Any],
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        acceptance_payload = acceptance["payload"]
        if acceptance_payload.get("schema_version") is None:
            raise ControlError("validation_completed 只能绑定严格 v2 Acceptance")
        attempt = self._fact(
            payload["validation_attempt_ref"], "validation_attempt_created"
        )
        attempt_payload = attempt["payload"]
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        candidate_payload = candidate["payload"]
        expected_from_acceptance = {
            "candidate_id": acceptance_payload["candidate_id"],
            "candidate_ref": acceptance_payload["candidate_ref"],
            "candidate_build_receipt_ref": acceptance_payload[
                "candidate_build_receipt_ref"
            ],
            "validation_attempt_ref": acceptance_payload["validation_attempt_ref"],
            "candidate_evidence_package_ref": acceptance_payload[
                "candidate_evidence_package_ref"
            ],
            "pair_refs": acceptance_payload["pair_refs"],
            "external_gate_receipt_refs": acceptance_payload[
                "external_gate_receipt_refs"
            ],
        }
        for key, expected_value in expected_from_acceptance.items():
            if payload[key] != expected_value:
                raise ControlError(f"validation_completed 与 Acceptance 的 {key} 不一致")
        if payload["attempt_id"] != attempt_payload["attempt_id"]:
            raise ControlError("validation_completed 与 ValidationAttempt ID 不一致")
        if payload["validation_execution_plan_ref"] != attempt_payload[
            "validation_execution_plan_ref"
        ]:
            raise ControlError("validation_completed 与 ValidationExecutionPlan 不一致")
        if attempt_payload["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("validation_completed 与 ValidationAttempt Candidate 不一致")
        build = self.store.load_receipt(payload["candidate_build_receipt_ref"])
        self._candidate_build_matches(campaign, candidate_payload, build)
        self.validate_object_graph(payload["candidate_evidence_package_ref"])
        ledger = self._object(
            acceptance_payload["atomic_assertion_ledger_ref"],
            "atomic_assertion_ledger",
        )
        expected_required = {
            item["assertion_id"]
            for item in ledger["assertions"]
            if item["owner_kind"] == "required_rule"
        }
        actual_required: list[str] = []
        for reference in payload["pair_refs"]:
            pair = self._fact(reference, "pair_recorded")
            actual_required.extend(
                item["assertion_id"] for item in pair["payload"]["assertion_results"]
            )
        if set(actual_required) != expected_required or len(actual_required) != len(
            expected_required
        ):
            raise ControlError("validation_completed 未唯一闭合 RequiredRules 原子断言")
        plan = self._object(
            payload["validation_execution_plan_ref"], "validation_execution_plan"
        )
        expected_gate_ids = {item["gate_id"] for item in plan["external_gates"]}
        gate_receipts = [
            self.store.load_receipt(reference)
            for reference in payload["external_gate_receipt_refs"]
        ]
        actual_gate_ids = [item["gate_id"] for item in gate_receipts]
        if set(actual_gate_ids) != expected_gate_ids or len(actual_gate_ids) != len(
            expected_gate_ids
        ):
            raise ControlError("validation_completed 外部门禁未唯一覆盖执行计划")
        if any(item["result"] != "pass" for item in gate_receipts):
            raise ControlError("validation_completed 不得消费失败外部门禁")
        self._require_validation_gate_closure(
            campaign_id=campaign["campaign_id"],
            candidate_id=payload["candidate_id"],
            candidate_ref=payload["candidate_ref"],
            candidate_build_receipt_ref=payload["candidate_build_receipt_ref"],
            validation_attempt_ref=payload["validation_attempt_ref"],
            validation_execution_plan_ref=payload[
                "validation_execution_plan_ref"
            ],
            supplied_refs=payload["external_gate_receipt_refs"],
            plan=plan,
        )
        before = self._fact(
            attempt_payload["production_selector_before_ref"], "selector_observed"
        )
        after = self._fact(
            payload["production_selector_after_ref"], "selector_observed"
        )
        before_snapshot = self._object(
            before["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        after_snapshot = self._object(
            after["payload"]["catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        for key in ("catalog_digest", "production_active_ref", "production_rollback_ref"):
            if before_snapshot[key] != after_snapshot[key]:
                raise ControlError("VC-5 期间 production Active／Rollback／selector 发生变化")
        if any(
            fact["payload"]["attempt_id"] == payload["attempt_id"]
            or fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in facts_by_kind["validation_completed"]
        ):
            raise ControlError("同一 Candidate／attempt 的 VC-5 完成事实不得覆盖")

    def _gate_candidate_delivery(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        plan = self._object(payload["delivery_plan_ref"], "candidate_delivery_plan")
        completed = self._fact(payload["validation_completed_ref"], "validation_completed")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        expected = {
            "delivery_id": plan["delivery_id"],
            "candidate_id": plan["candidate_id"],
            "candidate_ref": plan["candidate_ref"],
            "candidate_build_receipt_ref": plan["candidate_build_receipt_ref"],
            "validation_completed_ref": plan["validation_completed_ref"],
            "acceptance_ref": plan["acceptance_ref"],
            "host_id": plan["host_id"],
            "architecture": plan["architecture"],
            "compose_sha256": plan["compose_sha256"],
            "environment_sha256": plan["environment_sha256"],
            "configuration_sha256": plan["configuration_sha256"],
            "network_sha256": plan["network_sha256"],
        }
        for key, expected_value in expected.items():
            if payload[key] != expected_value:
                raise ControlError(f"候选交付阶段的 {key} 与冻结计划不一致")
        if completed["payload"]["acceptance_ref"] != payload["acceptance_ref"]:
            raise ControlError("候选交付未绑定 VC-5 完成的 Acceptance")
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("候选交付与 Acceptance Candidate 不一致")
        if candidate["payload"]["candidate_build_receipt_ref"] != payload[
            "candidate_build_receipt_ref"
        ]:
            raise ControlError("候选交付与 CandidateBuildReceipt 不一致")
        stage = payload["stage"]
        expected_image = (
            plan["rollback_image_digest"]
            if stage == "rollback_verified"
            else plan["candidate_image_digest"]
        )
        if payload["image_digest"] != expected_image:
            raise ControlError(f"{stage} 未使用冻结的正确镜像摘要")
        expected_profile = (
            plan["rollback_runtime_profile_sha256"]
            if stage == "rollback_verified"
            else plan["runtime_profile_sha256"]
        )
        if payload["runtime_profile_sha256"] != expected_profile:
            raise ControlError(f"{stage} 运行 Profile 与冻结计划不一致")
        if payload["check_ids"] != plan["stage_checks"][stage]:
            raise ControlError(f"{stage} 未执行冻结的交付检查闭集")
        evidence_payloads = [
            self._object(reference) for reference in payload["evidence_refs"]
        ]
        if payload["result"] == "pass":
            for evidence in evidence_payloads:
                self._require_manifest_pass(
                    evidence, f"CandidateDelivery/{stage}/evidence_refs"
                )
        elif not any(
            self._manifest_has_failure(evidence) for evidence in evidence_payloads
        ):
            raise ControlError(f"{stage} 失败事实缺少显式失败证据")
        if (
            stage == "stable_observed"
            and payload["result"] == "pass"
            and payload["observed_seconds"] < plan["observation_window_seconds"]
        ):
            raise ControlError("stable_observed 未达到冻结观察窗")
        existing = [
            fact
            for fact in facts_by_kind["candidate_delivery_recorded"]
            if fact["payload"]["delivery_id"] == payload["delivery_id"]
        ]
        if any(fact["payload"]["stage"] == stage for fact in existing):
            raise ControlError("候选交付阶段不得覆盖")
        stage_index = CANDIDATE_DELIVERY_STAGES.index(stage)
        if stage_index == 0:
            return
        previous = self._fact(
            payload["previous_stage_ref"], "candidate_delivery_recorded"
        )
        expected_previous_stage = CANDIDATE_DELIVERY_STAGES[stage_index - 1]
        if previous["payload"]["stage"] != expected_previous_stage:
            raise ControlError(f"{stage} 必须直接承接 {expected_previous_stage}")
        if previous["payload"]["result"] != "pass":
            raise ControlError("失败的候选交付阶段不得继续推进")
        for key in (
            "delivery_id",
            "candidate_id",
            "candidate_ref",
            "candidate_build_receipt_ref",
            "validation_completed_ref",
            "acceptance_ref",
            "delivery_plan_ref",
            "host_id",
            "architecture",
            "compose_sha256",
            "environment_sha256",
            "configuration_sha256",
            "network_sha256",
        ):
            if payload[key] != previous["payload"][key]:
                raise ControlError(f"候选交付四阶段的 {key} 发生漂移")

    def _gate_selector(
        self,
        kind: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        snapshot = self._object(payload["catalog_snapshot_ref"], "runtime_catalog_snapshot")
        if kind == "selector_activated":
            deployment = self._fact(payload["deployment_ref"], "active")
            candidate = self._candidate_by_id(deployment["payload"]["candidate_id"], facts_by_kind)
            if snapshot["production_active_ref"] != candidate["payload"]["release_artifact_ref"]:
                raise ControlError("selector 激活没有指向已晋升 Candidate Release")

    def _gate_release_promoted(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        candidate = self._fact(payload["candidate_ref"], "candidate_frozen")
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        if acceptance["payload"]["result"] != "accepted":
            raise ControlError("validation-only Candidate 不得晋升生产")
        if acceptance["payload"].get("schema_version") is not None:
            completed = [
                fact
                for fact in facts_by_kind["validation_completed"]
                if fact["payload"]["acceptance_ref"] == payload["acceptance_ref"]
            ]
            if len(completed) != 1:
                raise ControlError("严格 Acceptance 未完成 VC-5，不得晋升生产")
            self._require_candidate_delivery_receipt(
                payload["candidate_ref"], payload["acceptance_ref"]
            )
        if acceptance["payload"]["candidate_ref"] != payload["candidate_ref"]:
            raise ControlError("晋升引用的 Candidate 与 Acceptance 不一致")
        if candidate["payload"]["release_artifact_ref"] != payload["release_artifact_ref"]:
            raise ControlError("晋升 Release 与 Candidate 不一致")
        if facts_by_kind["release_promoted"]:
            candidate_ids = {
                self._fact(fact["payload"]["candidate_ref"], "candidate_frozen")["payload"][
                    "candidate_id"
                ]
                for fact in facts_by_kind["release_promoted"]
            }
            if candidate["payload"]["candidate_id"] in candidate_ids:
                raise ControlError("同一 Candidate 不得重复晋升")

    def _gate_deployment_stage(
        self,
        stage: str,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        acceptance = self._fact(payload["acceptance_ref"], "acceptance_recorded")
        promotion_receipt = self.store.load_receipt(payload["promotion_receipt_ref"])
        if acceptance["payload"]["candidate_id"] != payload["candidate_id"]:
            raise ControlError("Deployment 与 Acceptance Candidate 不一致")
        if promotion_receipt["candidate_ref"] != acceptance["payload"]["candidate_ref"]:
            raise ControlError("Deployment 的 PromotionReceipt 与 Acceptance 不一致")
        candidate = self._fact(acceptance["payload"]["candidate_ref"], "candidate_frozen")
        active_envelope = self._object(
            payload["active_support_envelope_ref"], "active_support_envelope"
        )
        rollback_envelope = self._object(
            payload["rollback_operational_envelope_ref"],
            "rollback_operational_envelope",
        )
        runtime_snapshot = self._object(
            payload["runtime_catalog_snapshot_ref"], "runtime_catalog_snapshot"
        )
        candidate_release_ref = candidate["payload"]["release_artifact_ref"]
        if active_envelope["release_artifact_ref"] != candidate_release_ref:
            raise ControlError("Deployment ActiveSupportEnvelope 未指向 Candidate Release")
        expected_image = (
            rollback_envelope["operational_bindings"]["image_digest"]
            if stage == "rollback_verified"
            else candidate["payload"]["image_digest"]
        )
        if payload["image_digest"] != expected_image:
            raise ControlError(f"Deployment {stage} 的实际镜像与冻结身份不一致")
        if stage in {"active", "restored_active"}:
            if runtime_snapshot["production_active_ref"] != candidate_release_ref:
                raise ControlError(f"Deployment {stage} RuntimeCatalogSnapshot 未指向 Candidate Release")
        if stage == "rollback_verified":
            frozen_rollback_snapshot_ref = rollback_envelope["operational_bindings"][
                "selector_snapshot_ref"
            ]
            if payload["runtime_catalog_snapshot_ref"] != frozen_rollback_snapshot_ref:
                raise ControlError("rollback_verified 未恢复冻结的 RuntimeCatalogSnapshot")
        self.validate_object_graph(payload["active_support_envelope_ref"])
        self.validate_object_graph(payload["rollback_operational_envelope_ref"])
        self.validate_object_graph(payload["deployment_traffic_envelope_ref"])
        existing = [
            fact
            for current_stage in DEPLOYMENT_STAGES
            for fact in facts_by_kind[current_stage]
            if fact["payload"]["deployment_id"] == payload["deployment_id"]
        ]
        if any(fact["fact_kind"] == stage for fact in existing):
            raise ControlError("Deployment 阶段不得覆盖")
        stage_index = DEPLOYMENT_STAGES.index(stage)
        if stage_index == 0:
            return
        previous_stage = DEPLOYMENT_STAGES[stage_index - 1]
        previous = self._fact(payload["previous_stage_ref"], previous_stage)
        previous_payload = previous["payload"]
        for key in (
            "deployment_id",
            "candidate_id",
            "acceptance_ref",
            "promotion_receipt_ref",
            "active_support_envelope_ref",
            "rollback_operational_envelope_ref",
            "deployment_traffic_envelope_ref",
        ):
            if payload[key] != previous_payload[key]:
                raise ControlError(f"Deployment 五阶段的 {key} 发生漂移")
        if stage == "restored_active":
            active_matches = [
                fact
                for fact in facts_by_kind["active"]
                if fact["payload"]["deployment_id"] == payload["deployment_id"]
            ]
            if len(active_matches) != 1:
                raise ControlError("restored_active 无法唯一关联 active 阶段")
            active_payload = active_matches[0]["payload"]
            if (
                payload["image_digest"] != active_payload["image_digest"]
                or payload["runtime_catalog_snapshot_ref"]
                != active_payload["runtime_catalog_snapshot_ref"]
            ):
                raise ControlError("restored_active 未闭合 active 的镜像与运行目录身份")

    def _gate_inventory_current(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        deployment = self._fact(payload["deployment_ref"], "restored_active")
        acceptance = self._fact(deployment["payload"]["acceptance_ref"], "acceptance_recorded")
        approval = self._fact(acceptance["payload"]["profile_approval_ref"], "profile_approved")
        ingress = self._object(
            payload["production_ingress_inventory_ref"], "production_ingress_inventory"
        )
        egress = self._object(
            payload["egress_disposition_inventory_ref"], "egress_disposition_inventory"
        )
        ingress_target = {
            item["logical_ingress_id"]: item["target_disposition"]
            for item in approval["payload"]["ingress_target_dispositions"]
        }
        ingress_current = {
            item["logical_ingress_id"]: item["current_disposition"] for item in ingress["entries"]
        }
        if ingress_current != ingress_target:
            raise ControlError("Deployment 后入口 current_disposition 未精确实现批准目标")
        egress_target = {
            item["egress_id"]: item["target_disposition"]
            for item in approval["payload"]["egress_target_dispositions"]
        }
        egress_current = {
            item["egress_id"]: item["current_disposition"] for item in egress["entries"]
        }
        if egress_current != egress_target:
            raise ControlError("Deployment 后出站 current_disposition 未精确实现批准目标")
        stale_enforcement = sorted(
            item["egress_id"]
            for item in egress["entries"]
            if item["current_guard_state"] != "enforced"
            and not (
                item["current_disposition"] == "denied"
                and item["current_guard_state"] == "source_absent"
            )
        )
        if stale_enforcement:
            raise ControlError(
                "Deployment 后出站仍未进入 enforced：" + ", ".join(stale_enforcement)
            )
        if facts_by_kind["inventory_current_appended"]:
            raise ControlError("同一 Campaign 的实际 current Inventory 不得覆盖")

    def replay_campaign(
        self, campaign: dict[str, Any], facts: list[dict[str, Any]]
    ) -> None:
        """在完整事实集上复核引用、唯一性和最终状态，不依赖可变状态文件。"""

        facts_by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            validate_fact_document({key: value for key, value in fact.items() if key != "_sha256"})
            for reference in self._all_fact_refs(fact["payload"]):
                referenced = self.store.load_fact(reference)
                if referenced["campaign_id"] != campaign["campaign_id"]:
                    raise ControlError("事实链重放发现跨 Campaign 引用")
            facts_by_kind[fact["fact_kind"]].append(fact)
        for fact in facts:
            without_current: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for kind, items in facts_by_kind.items():
                without_current[kind] = [item for item in items if item is not fact]
            self._validate_fact_semantics(campaign, fact, without_current)
        if len(facts_by_kind["discovery_recorded"]) > 1:
            raise ControlError("Campaign 存在多个 DiscoveryFact")
        if len(facts_by_kind["evidence_recorded"]) > 1:
            raise ControlError("Campaign 存在多个 EvidencePackage")
        if len(facts_by_kind["rule_classification_recorded"]) > 1:
            raise ControlError("Campaign 存在多个规则分类事实")
        if len(facts_by_kind["evidence_approved"]) > 1:
            raise ControlError("Campaign 存在多个 EvidenceApprovalFact")
        candidate_ids = [fact["payload"]["candidate_id"] for fact in facts_by_kind["candidate_frozen"]]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ControlError("Campaign 存在重复 Candidate ID")
        pair_keys = [
            (fact["payload"]["candidate_id"], fact["payload"]["pair_id"])
            for fact in facts_by_kind["pair_recorded"]
        ]
        if len(pair_keys) != len(set(pair_keys)):
            raise ControlError("Campaign 存在重复 PAIR")
        for deployment_id in {
            fact["payload"]["deployment_id"]
            for stage in DEPLOYMENT_STAGES
            for fact in facts_by_kind[stage]
        }:
            present = [
                stage
                for stage in DEPLOYMENT_STAGES
                if any(
                    fact["payload"]["deployment_id"] == deployment_id
                    for fact in facts_by_kind[stage]
                )
            ]
            expected = list(DEPLOYMENT_STAGES[: len(present)])
            if present != expected:
                raise ControlError(f"Deployment 阶段链存在越权或缺口：{deployment_id}/{present}")
        for delivery_id in {
            fact["payload"]["delivery_id"]
            for fact in facts_by_kind["candidate_delivery_recorded"]
        }:
            present = [
                stage
                for stage in CANDIDATE_DELIVERY_STAGES
                if any(
                    fact["payload"]["delivery_id"] == delivery_id
                    and fact["payload"]["stage"] == stage
                    for fact in facts_by_kind["candidate_delivery_recorded"]
                )
            ]
            expected = list(CANDIDATE_DELIVERY_STAGES[: len(present)])
            if present != expected:
                raise ControlError(f"候选交付阶段链存在越权或缺口：{delivery_id}/{present}")
        validation_gate_keys: list[tuple[str, str]] = []
        for reference in self.store.list_receipt_refs("validation_gate"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] == campaign["campaign_id"]:
                validation_gate_keys.append(
                    (
                        receipt["validation_attempt_ref"]["sha256"],
                        receipt["gate_id"],
                    )
                )
        if len(validation_gate_keys) != len(set(validation_gate_keys)):
            raise ControlError("同一 ValidationAttempt 存在重复外部门禁收据")
        delivery_receipt_keys: list[tuple[str, str]] = []
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] == campaign["campaign_id"]:
                delivery_receipt_keys.append(
                    (
                        receipt["candidate_ref"]["sha256"],
                        receipt["acceptance_ref"]["sha256"],
                    )
                )
        if len(delivery_receipt_keys) != len(set(delivery_receipt_keys)):
            raise ControlError("同一 Candidate／Acceptance 存在重复候选交付收据")

    @staticmethod
    def _all_fact_refs(value: Any) -> list[dict[str, Any]]:
        from .contracts import iter_fact_refs

        return list(iter_fact_refs(value))

    def status(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.load_campaign(campaign_id)
        facts = self.store.list_facts(campaign_id)
        self.replay_campaign(campaign, facts)
        kinds = {fact["fact_kind"] for fact in facts}
        from .receipts import replay_receipt

        delivery_receipts: list[dict[str, Any]] = []
        delivery_verified = False
        for reference in self.store.list_receipt_refs("candidate_delivery"):
            receipt = self.store.load_receipt(reference)
            if receipt["campaign_id"] != campaign_id:
                continue
            delivery_receipts.append(reference)
            try:
                replay_receipt(self.store, reference)
            except ControlError:
                continue
            delivery_verified = True
        if "restored_active" in kinds:
            checkpoint = "restored_active"
        elif "active" in kinds:
            checkpoint = "active"
        elif "accepted_not_activated" in kinds:
            checkpoint = "accepted_not_activated"
        elif delivery_verified:
            checkpoint = "ready_for_operator_release"
        elif "validation_completed" in kinds:
            completed = next(
                fact
                for fact in reversed(facts)
                if fact["fact_kind"] == "validation_completed"
            )
            acceptance = self._fact(
                completed["payload"]["acceptance_ref"], "acceptance_recorded"
            )
            checkpoint = (
                "ready"
                if acceptance["payload"]["result"] == "accepted"
                else "validation_only"
            )
        elif "acceptance_recorded" in kinds:
            strict_pending = any(
                fact["payload"].get("schema_version") is not None
                for fact in facts
                if fact["fact_kind"] == "acceptance_recorded"
            )
            if strict_pending:
                checkpoint = "validation_pending"
            else:
                accepted = any(
                    fact["payload"]["result"] == "accepted"
                    for fact in facts
                    if fact["fact_kind"] == "acceptance_recorded"
                )
                checkpoint = "ready" if accepted else "validation_only"
        elif "candidate_frozen" in kinds:
            checkpoint = "candidate_sealed"
        elif "profile_approved" in kinds:
            checkpoint = "profile_approved"
        elif "evidence_approved" in kinds:
            checkpoint = "official_sealed"
        elif "evidence_recorded" in kinds:
            checkpoint = "evidence_recorded"
        elif "discovery_recorded" in kinds:
            checkpoint = "discovered"
        else:
            checkpoint = "campaign_created"
        activation_verified = False
        activation_receipts = []
        if "restored_active" in kinds:
            for reference in self.store.list_receipt_refs("activation"):
                receipt = self.store.load_receipt(reference)
                if receipt["campaign_id"] != campaign_id:
                    continue
                activation_receipts.append(reference)
                try:
                    replay_receipt(self.store, reference)
                except ControlError:
                    continue
                activation_verified = True
        if "active" in kinds and not activation_verified:
            production_state = "production_unverified"
        elif activation_verified:
            production_state = "verified_active"
        else:
            production_state = "not_activated"
        return {
            "schema_version": "official-client-control-status/v1",
            "campaign_id": campaign_id,
            "checkpoint": checkpoint,
            "fact_counts": {
                dimension: len(self.store.list_facts(campaign_id, dimension))
                for dimension in FACT_DIMENSION.values()
            },
            "activation_receipt_count": len(activation_receipts),
            "candidate_delivery_receipt_count": len(delivery_receipts),
            "delivery_state": (
                "ready_for_operator_release"
                if delivery_verified
                else "not_ready_for_operator_release"
            ),
            "production_state": production_state,
            "production_active_proven": activation_verified,
        }
