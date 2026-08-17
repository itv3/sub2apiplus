"""FW-D 对象图、Campaign、Inventory、Envelope 与状态转换门禁。"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from .canonical import canonical_sha256
from .contracts import (
    DEPLOYMENT_STAGES,
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
        elif kind == "deployment_traffic_envelope":
            self._validate_deployment_traffic_envelope(payload)

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
        elif kind in {"selector_observed", "selector_activated"}:
            self._gate_selector(kind, payload, facts_by_kind)
        elif kind == "release_promoted":
            self._gate_release_promoted(payload, facts_by_kind)
        elif kind in DEPLOYMENT_STAGES:
            self._gate_deployment_stage(kind, payload, facts_by_kind)
        elif kind == "inventory_current_appended":
            self._gate_inventory_current(payload, facts_by_kind)

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

    def _gate_evidence_approved(
        self,
        payload: dict[str, Any],
        facts_by_kind: dict[str, list[dict[str, Any]]],
    ) -> None:
        evidence = self._fact(payload["evidence_fact_ref"], "evidence_recorded")
        if evidence["payload"]["evidence_package_ref"] != payload["evidence_package_ref"]:
            raise ControlError("EvidenceApproval 未批准 EvidenceFact 中的同一 EvidencePackage")
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
        evidence_rules = {rule["spec_id"]: rule for rule in evidence["rules"]}
        missing_rules = sorted(target_specs - set(evidence_rules))
        if missing_rules:
            raise ControlError(f"ProfileApproval 引用了 EvidencePackage 不存在的规则：{missing_rules}")
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
        self._candidate_by_id(payload["candidate_id"], facts_by_kind)
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
        for reference in payload["scenario_approval_refs"]:
            scenario = self._fact(reference, "scenario_approved")
            if scenario["payload"]["candidate_id"] != payload["candidate_id"]:
                raise ControlError("PAIR 引用了其他 Candidate 的场景批准")
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
        if any(
            fact["payload"]["candidate_id"] == payload["candidate_id"]
            for fact in facts_by_kind["acceptance_recorded"]
        ):
            raise ControlError("同一 Candidate 的 AcceptanceFact 不得覆盖")

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

    @staticmethod
    def _all_fact_refs(value: Any) -> list[dict[str, Any]]:
        from .contracts import iter_fact_refs

        return list(iter_fact_refs(value))

    def status(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.load_campaign(campaign_id)
        facts = self.store.list_facts(campaign_id)
        kinds = {fact["fact_kind"] for fact in facts}
        if "restored_active" in kinds:
            checkpoint = "restored_active"
        elif "active" in kinds:
            checkpoint = "active"
        elif "accepted_not_activated" in kinds:
            checkpoint = "accepted_not_activated"
        elif "acceptance_recorded" in kinds:
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
        elif "discovery_recorded" in kinds:
            checkpoint = "discovered"
        else:
            checkpoint = "campaign_created"
        activation_verified = False
        activation_receipts = []
        if "restored_active" in kinds:
            from .receipts import replay_receipt

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
            "production_state": production_state,
            "production_active_proven": activation_verified,
        }
