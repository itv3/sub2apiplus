"""来源接口的小型原件世界；合成回归不能充当正式来源。

Store 四根由已有生产器生成；总计划、VC checkpoint、构建收据与暂存
Release 使用原合同生成器。accept 和首轮阶段结果按真实键集写入，不
添加 candidate_identity 或 campaign_mode，也不修改任何已有失败世界。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade_acceptance as acceptance
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import claude_upgrade_vc as vc
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests.test_claude_upgrade_seal import write_vc_control
from tools.official_client_capture.tests.test_claude_real_delivery_integration import DeliveryCampaign
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes
from tools.official_client_control.contracts import derive_identity_conditions


def write(path: Path, document: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(canonical_json_bytes(document))
    path.chmod(0o600)
    return path


def binding(path: Path, root: Path, *, with_bytes: bool = False) -> dict[str, Any]:
    value = {"path": path.relative_to(root).as_posix(), "sha256": sha256_bytes(path.read_bytes())}
    if with_bytes:
        value["bytes"] = path.stat().st_size
    return value


class QualificationDeliveryCampaign(DeliveryCampaign):
    """在原生产器中冻结实际画像文档，不用只改 digest 的旧简化夹具。"""

    def __init__(self, root: Path, *, data_root: Path, schema: str,
                 store: Any = None, external_root: Path | None = None,
                 external_prefix: str = ""):
        super().__init__(root, schema=schema, store=store,
                         external_root=external_root, external_prefix=external_prefix)
        self.material_data_root = data_root

    def select_profile(self, name: str) -> None:
        from copy import deepcopy
        import json
        material = self.materials[name]
        document = json.loads((self.material_data_root / material["profile_binding"]["path"]).read_bytes())
        for kind, key in (("snapshot", "snapshot"), ("release_bundle", "release-bundle"), ("release_artifact", "release")):
            payload = deepcopy(self.store.load_object(self.references[key])["payload"])
            payload["profile_digest"] = material["runtime_profile_sha256"]
            if kind in {"snapshot", "release_bundle"}:
                payload["document"] = deepcopy(document)
            if kind != "snapshot":
                payload["snapshot_ref"] = self.references["snapshot"]
            if kind == "release_artifact":
                payload["release_bundle_ref"] = self.references["release-bundle"]
            self.references[key] = self.store.seal_object(kind, payload)


class QualificationSubjectWorld:
    """生成独立身份来源；gate 的 subject 从这些原件派生，反向读取禁止。"""

    def __init__(self, data_root: Path, fixture: Any, qualification: dict[str, Any], profile_path: Path):
        self.data_root = data_root
        self.fixture = fixture
        self.qualification = qualification
        self.campaign = data_root / "evidence/campaigns" / qualification["campaign_id"]
        store = fixture.store
        stored_build = store.load_receipt(qualification["build_receipt_ref"])
        candidate = store.load_fact(qualification["candidate_ref"])
        self.revision = candidate["formed_under"]["candidate_revision"]
        conditions, _unprovable = derive_identity_conditions(fixture.campaign_document)
        manifest = {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude",
            "campaign_id": fixture.campaign_id, "created_at_utc": "2026-08-18T00:00:00Z",
            "campaign_mode": "formal", "campaign_purpose": stored_build["candidate_purpose"],
            "baseline_version": "0.9.0", "target_version": fixture.campaign_document["target_version"],
            "target_sha256": fixture.official_binding["sha256"], "official_identity": {}, "baseline_identity": {},
            "tool_identity": fixture.tool_identity, "control_receipts": {}, "inputs": {},
            "configuration": {key: conditions[key] for key in ("platform", "entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256")},
            "required_rules": [], "coverage_plan": {}, "jobs": [], "vc_control": {}, "predecessor": None}
        write(self.campaign / "campaign.json", manifest)
        (self.campaign / "campaign.sha256").write_text(sha256_bytes((self.campaign / "campaign.json").read_bytes()) + "\n")
        write_vc_control(self.campaign)
        self.manifest = acceptance._orchestrator()._require_formal_campaign(self.campaign)
        manifest_sha = sha256_bytes((self.campaign / "campaign.json").read_bytes())

        profile_raw = profile_path.read_bytes()
        profile_digest = sha256_bytes(profile_raw)
        if profile_digest != stored_build["profile_identity"]["profile_digest"]:
            raise AssertionError("夹具画像原字节必须对应原 Store 构建")
        profile_relative = f"catalogdata/claude/profiles/{self.manifest['target_version']}/{profile_digest}.json"
        release_raw = canonical_json_bytes(store.load_object(stored_build["release_artifact_ref"]))
        entry = {"version": self.manifest["target_version"], "release_sha256": sha256_bytes(release_raw),
            "profile": {"path": profile_relative, "sha256": profile_digest}}
        files = {profile_relative: profile_raw, "release-artifact.json": release_raw}
        staged = release.staged_release_receipt(campaign_id=fixture.campaign_id,
            target_version=self.manifest["target_version"], platform="linux/arm64", generation="g-fixture",
            joint_sha256=canonical_sha256(stored_build["release_artifact_ref"]), entry=entry,
            files=files, store_refs={"release_artifact_ref": stored_build["release_artifact_ref"]})
        release.publish_staged_release(self.campaign, files, staged)
        staged_path = self.campaign / release.STAGED_RELEASE_ROOT / release.STAGED_RECEIPT_FILENAME
        self.profile_path = self.campaign / release.STAGED_RELEASE_ROOT / profile_relative
        self.release_path = staged_path

        source_root = self.campaign / "fixture-source"
        source_root.mkdir(mode=0o700)
        transition_path = write(source_root / "transition.json", {"fixture_only": True})
        implementation = write(self.campaign / "fixture-materials/implementation.json", {"fixture_only": True})
        machine_paths = {name: write(self.campaign / f"fixture-materials/{name}.json", {"fixture_only": True, "kind": name})
            for name in ("build_inventory", "frontend_provenance", "image_inspection", "capability_probe")}
        def machine(name: str) -> dict[str, Any]:
            return {**binding(machine_paths[name], self.campaign, with_bytes=True),
                "receipt_digest": canonical_sha256({"fixture_only": True, "kind": name})}
        binary_binding = stored_build["artifact_inventory"]["entries"][0]["binding"]
        binary = fixture.external_root / binary_binding["path"]
        self.build = artifacts.build_official_client_candidate_build_receipt(client="claude",
            campaign_id=fixture.campaign_id, campaign_manifest_sha256=manifest_sha,
            candidate_id=qualification["candidate_id"], candidate_revision=self.revision,
            candidate_purpose=stored_build["candidate_purpose"], target_version=self.manifest["target_version"],
            deployed_version=self.manifest["target_version"], target_architecture=stored_build["build_identity"]["target_architecture"],
            source={"root": str(source_root), "git_commit": stored_build["source_identity"]["git_commit"],
                "tree_sha256": stored_build["source_identity"]["source_tree_sha256"],
                **{key: stored_build["source_identity"][key] for key in ("test_tree_sha256", "dependency_lock_sha256", "clean")}},
            binary={"path": str(binary), "sha256": binary_binding["sha256"], "bytes": binary_binding["bytes"]},
            image={key: stored_build["image"][key] for key in ("reference", "manifest_digest", "image_id")},
            build_id=stored_build["build_identity"]["build_id"], toolchain=stored_build["build_identity"]["toolchain"],
            build_parameters=stored_build["build_identity"]["parameters"],
            profile={"profile_digest": profile_digest, "release_sha256": entry["release_sha256"],
                **{key: canonical_sha256({key: "fixture_only"}) for key in ("derivation_receipt_sha256", "wire_sha256", "model_capability_catalog_sha256", "bundle_sha256")}},
            catalog_stage={"path": str(staged_path), "sha256": sha256_bytes(staged_path.read_bytes()),
                "catalog_tree_sha256": staged["inventory_sha256"], "release_catalog_sha256": entry["release_sha256"]},
            source_transition={"path": str(transition_path), "sha256": sha256_bytes(transition_path.read_bytes())},
            gate_requirements={"requirements_sha256": stored_build["validation_scope_sha256"],
                "vc2_stage_result": binding(implementation, self.campaign), "vc3_stage_result": binding(staged_path, self.campaign)},
            gate_plan={"path": str(implementation), "sha256": sha256_bytes(implementation.read_bytes()),
                "plan_sha256": stored_build["validation_scope_sha256"], "requirements_sha256": stored_build["validation_scope_sha256"]},
            implementation_tests={"evidence_root": str(implementation.parent),
                "receipt": binding(implementation, implementation.parent, with_bytes=True), "receipt_digest": canonical_sha256({"fixture_only": True})},
            build_inventory=machine("build_inventory"), frontend_provenance=machine("frontend_provenance"),
            image_inspection=machine("image_inspection"), capability_probe=machine("capability_probe"),
            validation_scope_sha256=stored_build["validation_scope_sha256"],
            store={"candidate_frozen_ref": qualification["candidate_ref"], "candidate_build_receipt_ref": qualification["build_receipt_ref"],
                **{key: stored_build[key] for key in ("profile_approval_ref", "release_artifact_ref", "support_envelope_ref")},
                "model_capability_catalog_ref": stored_build["profile_identity"]["model_capability_catalog_ref"]},
            built_at_utc="2026-08-18T01:00:00Z")
        self.build_path = write(self.campaign / "candidates" / qualification["candidate_id"] / "build-receipt.json", self.build)
        self.plan = vc.vc_campaign_plan(self.campaign, self.manifest)
        previous_path, previous = vc.replay_vc_checkpoint(self.campaign, self.plan, "VC-0")
        for phase in ("VC-1", "VC-2", "VC-3", "VC-4"):
            stage = self.build_path if phase == "VC-4" else write(self.campaign / f"fixture-materials/{phase}.json", {"fixture_only": True, "phase": phase})
            previous_path, previous = self.checkpoint(phase, stage, previous_path, previous)

        package_ref = store.load_fact(qualification["validation_ref"])["payload"]["candidate_evidence_package_ref"]
        stage = {"schema_version": candidate_seal.STAGE_RESULT_SCHEMA, "client": "claude", "campaign_id": fixture.campaign_id,
            "stage": candidate_seal.STAGE_NAME, "candidate_id": qualification["candidate_id"], "status": "complete",
            "attempt": binding(implementation, self.campaign), "seal": {},
            "store": {"candidate_evidence_package_ref": package_ref}, "client_evidence": {},
            candidate_seal.SEALED_EVIDENCE_MANIFEST_FIELD: binding(implementation, self.campaign),
            candidate_seal.ASSERTION_CONTEXT_FIELD: {candidate_seal.EVALUATION_MANIFEST_FIELD: binding(implementation, self.campaign)},
            "execute_item_ids": [], "reuse_item_ids": [], "live_request_count": 0,
            "scanned_bytes": binary.stat().st_size, "sealed_at_utc": "2026-08-18T01:30:00Z"}
        self.round_path = write(self.campaign / "candidates" / qualification["candidate_id"] / "result.json", stage)
        self.identity = compare.candidate_gate_identity(self.campaign, self.manifest, qualification["candidate_id"], self.build)
        self.material = implementation

    def checkpoint(self, phase: str, stage_path: Path, previous_path: Path, previous: dict) -> tuple[Path, dict]:
        checkpoint = artifacts.build_vc_checkpoint(campaign_plan=self.plan, phase=phase, status="complete",
            predecessor_checkpoint=vc.vc_checkpoint_reference(self.campaign, previous_path, previous),
            stage_receipt=binding(stage_path, self.campaign), completed_at_utc="2026-08-18T02:00:00Z",
            execute_item_ids=[], reuse_item_ids=[], live_request_count=0, scanned_bytes=0)
        path = vc.vc_checkpoint_path(self.campaign, phase, revision=self.revision if phase in ("VC-4", "VC-5") else None)
        return write(path, checkpoint), checkpoint

    def seal_accept(self, external_gate: dict[str, Any]) -> dict[str, Any]:
        q = self.qualification
        store = {"candidate_evidence_package_ref": {"object_kind": "candidate_evidence_package", "sha256": self.identity["candidate_package_digest"]},
            "acceptance_ref": q["acceptance_ref"], "validation_completed_ref": q["validation_ref"]}
        seal = {"schema_version": acceptance.EVIDENCE_SEAL_SCHEMA, "campaign_id": q["campaign_id"],
            "candidate_id": q["candidate_id"], "campaign_manifest_sha256": sha256_bytes((self.campaign / "campaign.json").read_bytes()),
            "campaign_purpose": self.manifest["campaign_purpose"], "candidate_purpose": self.manifest["campaign_purpose"],
            "production_state": "not_activated", "evaluation_baseline": 0, "candidate_external_gate": external_gate,
            "candidate_package_digest": self.identity["candidate_package_digest"], "build_receipt": binding(self.build_path, self.campaign),
            "store": store}
        seal_path = write(self.campaign / "acceptance" / q["candidate_id"] / "evidence-seal.json", seal)
        parent = {"schema_version": acceptance.ACCEPT_SCHEMA, "client": "claude", "campaign_id": q["campaign_id"],
            "stage": acceptance.STAGE_NAME, "candidate_id": q["candidate_id"], "status": "complete", "accepted": True,
            "campaign_purpose": self.manifest["campaign_purpose"], "candidate_purpose": self.manifest["campaign_purpose"],
            "production_state": "not_activated", "candidate_revision": self.revision, "validation_round": 1, "evaluation_baseline": 0,
            "comparison": binding(self.material, self.campaign),
            "assertions": {"results": binding(self.material, self.campaign), "evaluation_run": binding(self.material, self.campaign)},
            "external_gate": external_gate, "runtime_audit": binding(self.material, self.campaign),
            "client_evidence": {}, "pair_rows": {}, "store": store, "evidence_seal": binding(seal_path, self.campaign),
            "accepted_at_utc": "2026-08-18T03:00:00Z"}
        if set(parent) != acceptance.STAGE_RESULT_FIELDS:
            raise AssertionError("accept 夹具必须使用正式键集")
        path = write(self.campaign / "acceptance" / q["candidate_id"] / "result.json", parent)
        q["sealed_result_binding"] = binding(path, self.data_root, with_bytes=True)
        previous_path, previous = vc.replay_vc_checkpoint(self.campaign, self.plan, "VC-4", revision=self.revision)
        self.checkpoint("VC-5", path, previous_path, previous)
        return parent
