"""只使用合成 Persona 和无秘密数据构造完整 FW-D Campaign。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
)
from tools.official_client_control.contracts import (
    candidate_identity_sha256,
    campaign_identity_sha256,
    capability_key,
    profile_approval_identity_sha256,
)
from tools.official_client_control.receipts import (
    control_tool_bundle_sha256,
    finalize_activation,
    finalize_promotion,
)
from tools.official_client_control.store import ControlStore


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
TARGET_IMAGE = f"sha256:{'1' * 64}"
ROLLBACK_IMAGE = f"sha256:{'2' * 64}"


class SyntheticCampaign:
    """按文档顺序生成可在单元测试中变异的完整受管事实链。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.external_root = root / "external"
        self.external_root.mkdir(mode=0o700)
        self.store_root = root / "store"
        self.store = ControlStore.initialize(
            self.store_root, "2026-08-18T00:00:00Z"
        )
        self.persona = {
            "provider": "synthetic-provider",
            "official_product": "synthetic-cli",
            "auth_family": "oauth",
            "upstream_route_family": "synthetic-responses",
        }
        self.campaign_id = "synthetic-1.0.0-campaign"
        self.tool_sha256 = control_tool_bundle_sha256()
        self._clock = datetime(2026, 8, 18, tzinfo=timezone.utc)
        self.references: dict[str, dict[str, Any]] = {}
        self._prepare_external_files()

    def _time(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.isoformat().replace("+00:00", "Z")

    def _write_external(self, relative: str, content: bytes) -> dict[str, Any]:
        path = self.external_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": relative, "sha256": sha256_file(path), "bytes": len(content)}

    def _prepare_external_files(self) -> None:
        self.contract_binding = self._write_external(
            "contract/shared-contract.txt", b"synthetic read-only contract\n"
        )
        self.receipt_binding = self._write_external(
            "receipts/fw-c.json", b'{"result":"stable"}\n'
        )
        self.catalog_binding = self._write_external(
            "runtime/catalog.json", b'{"persona":"codex","result":"stable"}\n'
        )
        self.official_binding = self._write_external(
            "official/synthetic-cli.bin", b"synthetic official artifact\n"
        )
        self.evidence_binding = self._write_external(
            "evidence/spec-001.json", b'{"spec":"SPEC-001","verified":true}\n'
        )
        self.runtime_binding = self._write_external(
            "runtime/selector.json", b'{"active":null,"rollback":null}\n'
        )

    def seal_manifest(
        self,
        kind: str,
        manifest_id: str,
        entries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": f"official-client-{kind.replace('_', '-')}/v1",
            "persona": self.persona,
            "manifest_id": manifest_id,
            "entries": entries
            or [{"id": "entry-1", "facts": {"result": "pass"}}],
        }
        reference = self.store.seal_object(kind, payload)
        self.references[manifest_id] = reference
        return reference

    def bootstrap_and_campaign(self) -> None:
        contract_sources = [self.contract_binding]
        bootstrap = {
            "schema_version": "official-client-control-bootstrap/v1",
            "source_commit": "1" * 40,
            "contract_sources": contract_sources,
            "contract_bundle_sha256": canonical_sha256(contract_sources),
            "fw_c_receipts": [self.receipt_binding],
            "runtime_catalog": [self.catalog_binding],
            "tool_bundle_sha256": self.tool_sha256,
            "result": "stable",
        }
        self.references["bootstrap"] = self.store.seal_object("bootstrap", bootstrap)
        campaign = {
            "schema_version": "official-client-control-campaign/v1",
            "campaign_id": self.campaign_id,
            "persona": self.persona,
            "target_version": "1.0.0",
            "official_artifacts": [self.official_binding],
            "platforms": ["linux/amd64"],
            "entrypoints": ["synthetic-cli"],
            "default_conditions": ["privacy=default"],
            "tool_bundle_sha256": self.tool_sha256,
            "bootstrap_ref": self.references["bootstrap"],
            "created_at_utc": self._time(),
            "identity_sha256": "",
        }
        campaign["identity_sha256"] = campaign_identity_sha256(campaign)
        self.store.create_campaign(campaign)

    def discovery_and_evidence(self, evidence_level: str = "verified") -> None:
        self.references["discovery"] = self.store.append_fact(
            self.campaign_id,
            "discovery_recorded",
            {
                "version": "1.0.0",
                "source": "synthetic-fixture",
                "discovered_at_utc": self._time(),
                "tool_sha256": self.tool_sha256,
                "artifact_refs": [self.official_binding],
            },
            self._time(),
        )
        self.references["comparison-policy"] = self.seal_manifest(
            "operational_evidence", "comparison-policy"
        )
        evidence = {
            "schema_version": "official-client-evidence-package/v1",
            "persona": self.persona,
            "version": "1.0.0",
            "official_artifacts": [self.official_binding],
            "platforms": ["linux/amd64"],
            "entrypoints": ["synthetic-cli"],
            "default_conditions": ["privacy=default"],
            "comparison_policy_ref": self.references["comparison-policy"],
            "producer_tool_sha256": self.tool_sha256,
            "rules": [
                {
                    "spec_id": "SPEC-001",
                    "evidence_level": evidence_level,
                    "rule_lifecycle": "candidate",
                    "compatibility_class": "request_egress",
                    "migration_decision": "add",
                    "evidence_refs": [self.evidence_binding],
                    "applicability": ["linux/amd64", "privacy=default"],
                }
            ],
        }
        self.references["evidence-package"] = self.store.seal_object(
            "evidence_package", evidence
        )
        self.references["evidence-fact"] = self.store.append_fact(
            self.campaign_id,
            "evidence_recorded",
            {
                "discovery_fact_ref": self.references["discovery"],
                "evidence_package_ref": self.references["evidence-package"],
            },
            self._time(),
        )
        self.references["evidence-approval"] = self.store.append_fact(
            self.campaign_id,
            "evidence_approved",
            {
                "evidence_fact_ref": self.references["evidence-fact"],
                "evidence_package_ref": self.references["evidence-package"],
                "reviewer": "synthetic-reviewer",
                "review_ref": "review/evidence-1",
            },
            self._time(),
        )

    def profile_objects(self) -> None:
        self.references["operational-evidence"] = self.seal_manifest(
            "operational_evidence", "operational-evidence"
        )
        op_ref = self.references["operational-evidence"]
        self.references["descriptor"] = self.store.seal_object(
            "persona_descriptor",
            {
                "schema_version": "official-client-persona-descriptor/v1",
                "persona": self.persona,
                "routes": ["synthetic-inference"],
                "sinks": ["synthetic-upstream"],
                "explicit_exclusions": ["api-key-auth"],
            },
        )
        self.references["profile-schema"] = self.store.seal_object(
            "profile_schema",
            {
                "schema_version": "official-client-profile-schema/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "schema_id": "synthetic-profile-v1",
                "document": {"fields": ["endpoint", "headers", "body", "transport"]},
            },
        )
        self.references["snapshot"] = self.store.seal_object(
            "snapshot",
            {
                "schema_version": "official-client-snapshot/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "profile_schema_ref": self.references["profile-schema"],
                "compiler_attestation_sha256": SHA_B,
                "document": {"endpoint": "/synthetic", "transport": "h2"},
            },
        )
        self.references["release-bundle"] = self.store.seal_object(
            "release_bundle",
            {
                "schema_version": "official-client-release-bundle/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "snapshot_ref": self.references["snapshot"],
                "endpoint_digest": "3" * 64,
                "transport_digest": "4" * 64,
                "state_digest": "5" * 64,
                "policy_digest": "6" * 64,
                "document": {"endpoint": "/synthetic", "transport": "h2"},
            },
        )
        self.references["release"] = self.store.seal_object(
            "release_artifact",
            {
                "schema_version": "official-client-release-artifact/v1",
                "persona": self.persona,
                "version": "1.0.0",
                "profile_digest": SHA_A,
                "snapshot_ref": self.references["snapshot"],
                "release_bundle_ref": self.references["release-bundle"],
            },
        )
        self.references["ingress-observation"] = self.store.seal_object(
            "ingress_observation",
            {
                "schema_version": "official-client-ingress-observation/v1",
                "persona": self.persona,
                "observed_at_utc": self._time(),
                "source_refs": [op_ref],
                "aliases": [
                    {
                        "alias_id": "alias-official",
                        "logical_ingress_id": "official-cli",
                        "physical_route": "internal://synthetic/official",
                        "caller_ids": ["official-driver"],
                    },
                    {
                        "alias_id": "alias-responses",
                        "logical_ingress_id": "third-responses",
                        "physical_route": "/v1/responses",
                        "caller_ids": ["gateway"],
                    },
                ],
            },
        )
        self.references["ingress-inventory"] = self.store.seal_object(
            "production_ingress_inventory",
            self.ingress_inventory_payload("retained_legacy"),
        )
        self.references["egress-observation"] = self.store.seal_object(
            "egress_observation",
            {
                "schema_version": "official-client-egress-observation/v1",
                "persona": self.persona,
                "observed_at_utc": self._time(),
                "source_refs": [op_ref],
                "egresses": [
                    {
                        "egress_id": "aux-refresh",
                        "route_id": "oauth-refresh",
                        "sink_id": "oauth-provider",
                        "oauth_related": True,
                        "kind": "lifecycle",
                    },
                    {
                        "egress_id": "infer-main",
                        "route_id": "synthetic-inference",
                        "sink_id": "synthetic-upstream",
                        "oauth_related": True,
                        "kind": "inference",
                    },
                    {
                        "egress_id": "unknown-aux",
                        "route_id": "unknown-auxiliary",
                        "sink_id": "synthetic-upstream",
                        "oauth_related": True,
                        "kind": "auxiliary",
                    },
                ],
            },
        )
        self.references["egress-inventory"] = self.store.seal_object(
            "egress_disposition_inventory",
            self.egress_inventory_payload(final=False),
        )
        self.references["boundary"] = self.seal_manifest(
            "operational_evidence", "boundary-assertion", [
                {"id": "outside-fail-close", "facts": {"result": "pass"}}
            ]
        )
        capabilities = sorted(
            [
                self.capability("official-cli", "official"),
                self.capability("third-responses", "responses"),
            ],
            key=capability_key,
        )
        self.references["support"] = self.store.seal_object(
            "support_envelope",
            {
                "schema_version": "official-client-support-envelope/v1",
                "persona": self.persona,
                "capabilities": capabilities,
                "unsupported_conditions": ["privacy=disabled"],
                "target_spec_ids": ["SPEC-001"],
                "production_ingress_inventory_ref": self.references["ingress-inventory"],
                "boundary_assertion_refs": [self.references["boundary"]],
            },
        )
        for kind, manifest_id in (
            ("persona_derivation", "persona-derivation"),
            ("compatibility_boundary", "compatibility-boundary"),
        ):
            self.references[manifest_id] = self.seal_manifest(kind, manifest_id)
        self.references["scenario-plan"] = self.store.seal_object(
            "scenario_plan",
            {
                "schema_version": "official-client-scenario-plan/v1",
                "persona": self.persona,
                "manifest_id": "scenario-plan",
                "scenarios": [
                    {
                        "id": "baseline",
                        "spec_ids": ["SPEC-001"],
                        "ingress_protocol_classes": ["official", "responses"],
                        "conditions": ["privacy=default"],
                        "assertion_ids": ["wire-convergence"],
                    }
                ],
            },
        )
        self.references["deployment-plan"] = self.store.seal_object(
            "deployment_plan",
            {
                "schema_version": "official-client-deployment-plan/v1",
                "persona": self.persona,
                "manifest_id": "deployment-plan",
                "active_support_capabilities": capabilities,
                "rollback_operational_capabilities": capabilities,
                "deployment_traffic_capabilities": capabilities,
                "rollback_target_kind": "legacy_deployment",
                "failure_policy": "persona_fail_close",
            },
        )

    def capability(self, ingress_id: str, protocol: str) -> dict[str, Any]:
        return {
            "platform": "linux/amd64",
            "logical_ingress_id": ingress_id,
            "protocol_class": protocol,
            "privacy_mode": "default",
            "model": "synthetic-model",
            "feature": "baseline",
            "endpoint": "/synthetic",
        }

    def ingress_inventory_payload(self, disposition: str) -> dict[str, Any]:
        evidence = [self.references["operational-evidence"]]
        return {
            "schema_version": "official-client-production-ingress-inventory/v1",
            "persona": self.persona,
            "observation_ref": self.references["ingress-observation"],
            "entries": [
                {
                    "logical_ingress_id": "official-cli",
                    "physical_alias_ids": ["alias-official"],
                    "caller_ids": ["official-driver"],
                    "adapter_id": "official-adapter",
                    "route_id": "official-route",
                    "ingress_kind": "official",
                    "protocol_class": "official",
                    "current_disposition": disposition,
                    "evidence_refs": evidence,
                },
                {
                    "logical_ingress_id": "third-responses",
                    "physical_alias_ids": ["alias-responses"],
                    "caller_ids": ["gateway"],
                    "adapter_id": "responses-adapter",
                    "route_id": "responses-route",
                    "ingress_kind": "third_party",
                    "protocol_class": "responses",
                    "current_disposition": disposition,
                    "evidence_refs": evidence,
                },
            ],
        }

    @staticmethod
    def managed_policy() -> dict[str, str]:
        return {
            "authentication": "trusted-oauth-refresh",
            "endpoint": "/oauth/token",
            "client": "managed-refresh-client",
            "timeout_policy": "10s",
            "retry_policy": "bounded-two-attempts",
            "secret_policy": "memory-only",
            "audit_policy": "metadata-only",
        }

    def egress_inventory_payload(self, *, final: bool) -> dict[str, Any]:
        evidence = [self.references["operational-evidence"]]
        return {
            "schema_version": "official-client-egress-disposition-inventory/v1",
            "persona": self.persona,
            "observation_ref": self.references["egress-observation"],
            "entries": [
                {
                    "egress_id": "aux-refresh",
                    "current_disposition": "non_persona_managed",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": [],
                    "managed_policy": self.managed_policy(),
                    "runtime_assertion_refs": evidence,
                },
                {
                    "egress_id": "infer-main",
                    "current_disposition": "persona_strict" if final else "denied",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": ["SPEC-001"] if final else [],
                    "managed_policy": None,
                    "runtime_assertion_refs": evidence,
                },
                {
                    "egress_id": "unknown-aux",
                    "current_disposition": "denied",
                    "current_guard_state": "enforced" if final else "legacy_observe",
                    "spec_ids": [],
                    "managed_policy": None,
                    "runtime_assertion_refs": evidence,
                },
            ],
        }

    def profile_approve(self, purpose: str = "production_replacement") -> None:
        evidence = [self.references["operational-evidence"]]
        payload = {
            "evidence_approval_ref": self.references["evidence-approval"],
            "approval_purpose": purpose,
            "persona_descriptor_ref": self.references["descriptor"],
            "profile_schema_ref": self.references["profile-schema"],
            "snapshot_ref": self.references["snapshot"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "production_ingress_inventory_ref": self.references["ingress-inventory"],
            "egress_disposition_inventory_ref": self.references["egress-inventory"],
            "persona_derivation_ref": self.references["persona-derivation"],
            "compatibility_boundary_ref": self.references["compatibility-boundary"],
            "scenario_plan_ref": self.references["scenario-plan"],
            "deployment_plan_ref": self.references["deployment-plan"],
            "target_spec_ids": ["SPEC-001"],
            "ingress_target_dispositions": [
                {
                    "logical_ingress_id": "official-cli",
                    "target_disposition": "migrated_strict",
                    "evidence_refs": evidence,
                },
                {
                    "logical_ingress_id": "third-responses",
                    "target_disposition": "migrated_strict",
                    "evidence_refs": evidence,
                },
            ],
            "egress_target_dispositions": [
                {
                    "egress_id": "aux-refresh",
                    "target_disposition": "non_persona_managed",
                    "spec_ids": [],
                    "managed_policy": self.managed_policy(),
                    "evidence_refs": evidence,
                },
                {
                    "egress_id": "infer-main",
                    "target_disposition": "persona_strict",
                    "spec_ids": ["SPEC-001"],
                    "managed_policy": None,
                    "evidence_refs": evidence,
                },
                {
                    "egress_id": "unknown-aux",
                    "target_disposition": "denied",
                    "spec_ids": [],
                    "managed_policy": None,
                    "evidence_refs": evidence,
                },
            ],
            "reviewer": "synthetic-reviewer",
            "review_ref": "review/profile-1",
            "identity_sha256": "",
        }
        payload["identity_sha256"] = profile_approval_identity_sha256(payload)
        self.references["profile-approval"] = self.store.append_fact(
            self.campaign_id, "profile_approved", payload, self._time()
        )

    def candidate_and_accept(self) -> None:
        self.freeze_candidate()
        self.run_scenario()
        self.record_pair_and_accept()

    def freeze_candidate(self) -> None:
        candidate = {
            "candidate_id": "synthetic-candidate-1",
            "profile_approval_ref": self.references["profile-approval"],
            "release_artifact_ref": self.references["release"],
            "support_envelope_ref": self.references["support"],
            "source_tree_sha256": "7" * 64,
            "test_tree_sha256": "8" * 64,
            "dependency_lock_sha256": "9" * 64,
            "target_architecture": "linux/amd64",
            "build_id": "synthetic-build-1",
            "image_digest": TARGET_IMAGE,
            "candidate_purpose": "production_replacement",
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        self.references["candidate"] = self.store.append_fact(
            self.campaign_id, "candidate_frozen", candidate, self._time()
        )

    def run_scenario(self, capture_result: str = "pass", attempt_id: str = "attempt-1") -> None:
        previous: dict[str, Any] | None = None
        for stage, kind, result in (
            ("prepare", "scenario_prepared", "prepared"),
            ("capture", "scenario_captured", capture_result),
            ("seal", "scenario_sealed", "pass"),
            ("approve", "scenario_approved", "pass"),
        ):
            payload: dict[str, Any] = {
                "candidate_id": "synthetic-candidate-1",
                "scenario_id": "baseline",
                "attempt_id": attempt_id,
                "stage": stage,
                "previous_stage_ref": previous,
                "artifact_refs": [] if stage == "prepare" else [
                    self.references["operational-evidence"]
                ],
                "result": result,
            }
            if stage == "approve":
                payload |= {
                    "reviewer": "synthetic-reviewer",
                    "review_ref": "review/scenario-1",
                }
            previous = self.store.append_fact(
                self.campaign_id, kind, payload, self._time()
            )
            if result == "failed":
                self.references["failed-scenario-stage"] = previous
                return
        self.references["scenario-approval"] = previous

    def record_pair_and_accept(self) -> None:
        self.references["pair"] = self.store.append_fact(
            self.campaign_id,
            "pair_recorded",
            {
                "pair_id": "PAIR-SPEC-001",
                "spec_id": "SPEC-001",
                "candidate_id": "synthetic-candidate-1",
                "release_artifact_ref": self.references["release"],
                "condition_sha256": SHA_C,
                "scenario_approval_refs": [self.references["scenario-approval"]],
                "official_result": {
                    "ingress_id": "official-cli",
                    "translation": "lossless",
                    "result": "pass",
                    "final_wire_sha256": SHA_A,
                },
                "third_party_results": [
                    {
                        "protocol_class": "responses",
                        "ingress_id": "third-responses",
                        "translation": "lossless",
                        "result": "pass",
                        "final_wire_sha256": SHA_A,
                    }
                ],
                "dynamic_field_checks": [
                    {
                        "id": "dynamic-session",
                        "dimensions": ["format", "lifecycle", "relation", "source"],
                        "result": "pass",
                    }
                ],
            },
            self._time(),
        )
        self.references["acceptance"] = self.store.append_fact(
            self.campaign_id,
            "acceptance_recorded",
            {
                "candidate_id": "synthetic-candidate-1",
                "profile_approval_ref": self.references["profile-approval"],
                "candidate_ref": self.references["candidate"],
                "pair_refs": [self.references["pair"]],
                "boundary_assertion_refs": [self.references["boundary"]],
                "inventory_assertion_refs": [self.references["operational-evidence"]],
                "acceptance_purpose": "production_replacement",
                "result": "accepted",
            },
            self._time(),
        )

    def promote(self) -> None:
        self.references["promotion-diff"] = self.seal_manifest(
            "promotion_diff",
            "promotion-diff",
            [{"id": "candidate-to-production", "facts": {"wire_delta": "none"}}],
        )
        self.references["promotion-fact"] = self.store.append_fact(
            self.campaign_id,
            "release_promoted",
            {
                "candidate_ref": self.references["candidate"],
                "acceptance_ref": self.references["acceptance"],
                "release_artifact_ref": self.references["release"],
                "promotion_diff_ref": self.references["promotion-diff"],
            },
            self._time(),
        )
        self.references["promotion-receipt"] = finalize_promotion(
            self.store, self.campaign_id, self.references["promotion-fact"]
        )

    def deploy(self, *, finalize_activation_receipt: bool = True) -> None:
        op_ref = self.references["operational-evidence"]
        self.references["runtime-before"] = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.persona,
                "catalog_digest": "d" * 64,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": self._time(),
                "source_ref": self.runtime_binding,
            },
        )
        self.references["selector-before"] = self.store.append_fact(
            self.campaign_id,
            "selector_observed",
            {
                "catalog_snapshot_ref": self.references["runtime-before"],
                "observation_kind": "read_only",
            },
            self._time(),
        )
        self.references["active-envelope"] = self.store.seal_object(
            "active_support_envelope",
            {
                "schema_version": "official-client-active-support-envelope/v1",
                "persona": self.persona,
                "support_envelope_ref": self.references["support"],
                "profile_approval_ref": self.references["profile-approval"],
                "acceptance_ref": self.references["acceptance"],
                "release_artifact_ref": self.references["release"],
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
            },
        )
        self.references["rollback-envelope"] = self.store.seal_object(
            "rollback_operational_envelope",
            {
                "schema_version": "official-client-rollback-operational-envelope/v1",
                "persona": self.persona,
                "rollback_kind": "legacy_deployment",
                "rollback_release_ref": None,
                "operational_bindings": {
                    "image_digest": ROLLBACK_IMAGE,
                    "selector_snapshot_ref": self.references["runtime-before"],
                    "configuration_ref": op_ref,
                    "dependencies_ref": op_ref,
                    "routes_ref": op_ref,
                    "exercise_receipt_ref": op_ref,
                },
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
                "wire_evidence_scope": "diagnostic_only",
            },
        )
        self.references["traffic-envelope"] = self.store.seal_object(
            "deployment_traffic_envelope",
            {
                "schema_version": "official-client-deployment-traffic-envelope/v1",
                "persona": self.persona,
                "active_support_envelope_ref": self.references["active-envelope"],
                "rollback_operational_envelope_ref": self.references["rollback-envelope"],
                "production_ingress_inventory_ref": self.references["ingress-inventory"],
                "capabilities": self.store.load_object(self.references["support"])["payload"][
                    "capabilities"
                ],
            },
        )
        self.references["runtime-after"] = self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.persona,
                "catalog_digest": "e" * 64,
                "production_active_ref": self.references["release"],
                "production_rollback_ref": None,
                "observed_at_utc": self._time(),
                "source_ref": self.runtime_binding,
            },
        )
        previous: dict[str, Any] | None = None
        for stage in (
            "accepted_not_activated",
            "canary_passed",
            "active",
            "rollback_verified",
            "restored_active",
        ):
            runtime_ref = (
                self.references["runtime-before"]
                if stage in {"accepted_not_activated", "canary_passed", "rollback_verified"}
                else self.references["runtime-after"]
            )
            image = ROLLBACK_IMAGE if stage == "rollback_verified" else TARGET_IMAGE
            previous = self.store.append_fact(
                self.campaign_id,
                stage,
                {
                    "deployment_id": "synthetic-deployment-1",
                    "stage": stage,
                    "candidate_id": "synthetic-candidate-1",
                    "previous_stage_ref": previous,
                    "acceptance_ref": self.references["acceptance"],
                    "promotion_receipt_ref": self.references["promotion-receipt"],
                    "active_support_envelope_ref": self.references["active-envelope"],
                    "rollback_operational_envelope_ref": self.references["rollback-envelope"],
                    "deployment_traffic_envelope_ref": self.references["traffic-envelope"],
                    "runtime_catalog_snapshot_ref": runtime_ref,
                    "image_digest": image,
                    "evidence_refs": [op_ref],
                },
                self._time(),
            )
            self.references[f"deployment-{stage}"] = previous
            if stage == "active":
                self.references["selector-after"] = self.store.append_fact(
                    self.campaign_id,
                    "selector_activated",
                    {
                        "catalog_snapshot_ref": self.references["runtime-after"],
                        "observation_kind": "activated",
                        "deployment_ref": previous,
                    },
                    self._time(),
                )
        self.references["final-ingress"] = self.store.seal_object(
            "production_ingress_inventory",
            self.ingress_inventory_payload("migrated_strict"),
        )
        self.references["final-egress"] = self.store.seal_object(
            "egress_disposition_inventory",
            self.egress_inventory_payload(final=True),
        )
        self.references["inventory-current"] = self.store.append_fact(
            self.campaign_id,
            "inventory_current_appended",
            {
                "deployment_ref": self.references["deployment-restored_active"],
                "production_ingress_inventory_ref": self.references["final-ingress"],
                "egress_disposition_inventory_ref": self.references["final-egress"],
            },
            self._time(),
        )
        if finalize_activation_receipt:
            self.references["activation-receipt"] = finalize_activation(
                self.store,
                self.campaign_id,
                self.references["deployment-restored_active"],
                self.references["selector-before"],
                self.references["selector-after"],
                self.references["inventory-current"],
            )

    def complete(self) -> None:
        self.bootstrap_and_campaign()
        self.discovery_and_evidence()
        self.profile_objects()
        self.profile_approve()
        self.candidate_and_accept()
        self.promote()
        self.deploy()

    def dump_reference(self, name: str, path: Path) -> None:
        path.write_bytes(canonical_json_bytes(self.references[name]))

    def debug_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
