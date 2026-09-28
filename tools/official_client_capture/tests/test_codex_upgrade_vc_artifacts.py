"""Codex VC-0～VC-6 小型控制制品的闭集与防篡改测试。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts


class CodexUpgradeVCArtifactsTests(unittest.TestCase):
    @staticmethod
    def _campaign_plan() -> dict[str, object]:
        return artifacts.build_campaign_plan(
            campaign_id="codex-0_154_0-campaign",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-14T00:00:00Z",
            original_deadline_at_utc="2026-09-14T12:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )

    def _interrupted_recovery_contract(self) -> dict[str, object]:
        plan = self._campaign_plan()
        return artifacts.build_interrupted_recovery_contract(
            campaign_plan=plan,
            batch_sequence=2,
            source_attempt={
                "attempt_id": "attempt-a",
                "reservation": {
                    "path": "official/attempts/attempt-a/reservation.json",
                    "sha256": "5" * 64,
                    "reservation_digest": "6" * 64,
                },
                "run_nonce": "7" * 64,
                "identity_sha256": "8" * 64,
                "checkpoint": {
                    "path": "official/attempts/attempt-a/checkpoints",
                    "record_count": 2,
                    "last_sequence": 2,
                    "last_sha256": "9" * 64,
                },
                "planned_job_ids": ["job-a", "job-b", "job-c"],
                "completed_job_ids": ["job-a"],
                "failed_job_ids": ["job-b"],
                "pending_job_ids": ["job-c"],
                "execute_job_ids": ["job-b", "job-c"],
                "reuse_job_ids": ["job-a"],
            },
            failed_supervisor={
                "run_dir": "/srv/control/run-a",
                "state_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "stop_receipt_sha256": "c" * 64,
                "owner_nonce": "d" * 64,
                "terminal_at_utc": "2026-09-14T01:00:00Z",
                "state": "failed",
                "reason": "KeyboardInterrupt",
                "batch_id": "vc-1-0001",
                "batch_sequence": 1,
                "batch_sha256": "e" * 64,
            },
            timing_ledger={
                "ledger_dir": "/srv/control/ledger",
                "ledger_plan_sha256": "f" * 64,
                "event_head_sequence": 6,
                "event_head_sha256": "0" * 64,
                "status": "active",
                "total_live_request_count": 26,
                "total_deadline_at_utc": plan["original_deadline_at_utc"],
            },
            deployment_receipt={
                "path": "/srv/control/deploy.json",
                "sha256": "1" * 64,
                "tool_files_sha256": "2" * 64,
            },
            tool_transition={
                "from_tool_files_sha256": "3" * 64,
                "to_tool_files_sha256": "2" * 64,
                "changed_files": [
                    {
                        "path": "run_official_relay_scenario.sh",
                        "from_sha256": "4" * 64,
                        "to_sha256": "5" * 64,
                        "classification": "failed_job_production",
                        "affected_job_ids": ["job-b", "job-c"],
                    }
                ],
                "allowed_production_paths": ["run_official_relay_scenario.sh"],
                "affected_job_ids": ["job-b", "job-c"],
            },
            compiled_at_utc="2026-09-14T01:01:00Z",
            must_start_by_utc="2026-09-14T01:03:00Z",
        )

    def _requirements(
        self,
        affected: list[str] | None = None,
    ) -> dict[str, object]:
        return artifacts.build_gate_requirements(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            joint_manifest_sha256="1" * 64,
            affected_rule_ids=affected or ["SPEC-HDR-005"],
            inherited_rule_ids=["SPEC-BODY-001"],
            migration_manifest={"path": "rules/rule-migration.json", "sha256": "2" * 64},
        )

    @staticmethod
    def _mapping(requirements: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": artifacts.GATE_MAPPING_SCHEMA,
            "requirements_sha256": requirements["requirements_sha256"],
            "gates": [
                {
                    "gate_id": row["gate_id"],
                    "test_id": f"gate-test-{index:03d}",
                    "working_directory": "backend" if index % 2 else ".",
                    "command": ["python3", "-m", f"gate_test_{index:03d}"],
                    "requirement_sha256": artifacts.digest(row),
                }
                for index, row in enumerate(requirements["requirements"], 1)
            ],
        }

    def _gate_plan(self) -> tuple[dict[str, object], dict[str, object]]:
        requirements = self._requirements()
        return requirements, artifacts.build_gate_plan(
            requirements,
            self._mapping(requirements),
        )

    def _candidate_build_receipt(self) -> dict[str, object]:
        requirements, gate_plan = self._gate_plan()
        return artifacts.build_candidate_build_receipt(
            campaign_id="codex-0_154_0-campaign",
            campaign_manifest_sha256="3" * 64,
            candidate_id="candidate-a",
            candidate_purpose="validation_only",
            target_version="0.154.0",
            deployed_version="0.154.0",
            target_architecture="linux/arm64",
            source={
                "root": "/srv/candidate-source",
                "tree_sha256": "4" * 64,
                "git_commit": "5" * 40,
            },
            binary={"path": "/srv/build/codex", "sha256": "6" * 64, "bytes": 42},
            image={
                "reference": f"registry/sub2api@sha256:{'7' * 64}",
                "manifest_digest": f"sha256:{'7' * 64}",
                "image_id": f"sha256:{'8' * 64}",
            },
            build_id="build-a",
            build_parameters={"offline": True, "target": "linux/arm64"},
            profile={
                "profile_id": "codex-0.154.0",
                "profile_digest": "9" * 64,
                "derivation_receipt_sha256": "a" * 64,
            },
            catalog_stage={
                "path": "/srv/candidate-source/catalog-stage-receipt.json",
                "sha256": "b" * 64,
                "catalog_tree_sha256": "c" * 64,
            },
            source_transition={
                "path": "/srv/control/source-transition.json",
                "sha256": "d" * 64,
            },
            gate_requirements={
                "path": "controls/post-promotion-gate-requirements.json",
                "sha256": "e" * 64,
                "requirements_sha256": requirements["requirements_sha256"],
            },
            gate_plan={
                "path": "/srv/candidate-source/post-promotion-gate-plan.json",
                "sha256": "f" * 64,
                "plan_sha256": gate_plan["plan_sha256"],
                "requirements_sha256": gate_plan["requirements_sha256"],
            },
            implementation_tests={
                "evidence_root": "/srv/control/implementation-tests",
                "receipt": {
                    "path": "receipt.json",
                    "sha256": "0" * 64,
                    "bytes": 10,
                },
                "receipt_digest": "1" * 64,
            },
            build_inventory={
                "path": "candidates/candidate-a/build-evidence/build-inventory.json",
                "sha256": "2" * 64,
                "bytes": 10,
                "receipt_digest": "3" * 64,
            },
            frontend_provenance={
                "path": "candidates/candidate-a/build-evidence/frontend-provenance.json",
                "sha256": "4" * 64,
                "bytes": 10,
                "receipt_digest": "5" * 64,
            },
            image_inspection={
                "path": "candidates/candidate-a/build-evidence/image-inspection.json",
                "sha256": "6" * 64,
                "bytes": 10,
                "receipt_digest": "7" * 64,
            },
            capability_probe={
                "path": "candidates/candidate-a/build-evidence/capability-probe.json",
                "sha256": "8" * 64,
                "bytes": 10,
                "receipt_digest": "9" * 64,
            },
            built_at_utc="2026-09-12T08:00:00Z",
        )

    def test_discovery_inventory_is_complete_and_not_truncated(self) -> None:
        inventory = artifacts.build_discovery_inventory(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            source_diff={
                "added_count": 2,
                "removed_count": 0,
                "added": [
                    {"fingerprint": "1" * 64, "surface": "header"},
                    {"fingerprint": "2" * 64, "surface": "model"},
                ],
                "removed": [],
            },
            official_diff={
                "added_count": 1,
                "removed_count": 1,
                "added": [{"fingerprint": "3" * 64, "surface": "request"}],
                "removed": [{"fingerprint": "4" * 64, "surface": "legacy"}],
            },
            source_diff_binding={"path": "source-diff.json", "sha256": "5" * 64},
            official_diff_binding={"path": "official-diff.json", "sha256": "6" * 64},
            evidence_manifest_binding={"path": "evidence.json", "sha256": "7" * 64},
        )
        self.assertFalse(inventory["truncated"])
        self.assertEqual(inventory["item_count"], 4)
        self.assertEqual(inventory["expected_item_count"], 4)
        self.assertEqual(artifacts.validate_discovery_inventory(inventory), inventory)

    def test_discovery_inventory_rejects_item_or_count_tampering(self) -> None:
        inventory = artifacts.build_discovery_inventory(
            campaign_id="codex-0_154_0-campaign",
            target_version="0.154.0",
            source_diff={
                "added_count": 1,
                "removed_count": 0,
                "added": [{"fingerprint": "1" * 64, "surface": "header"}],
                "removed": [],
            },
            official_diff={"added_count": 0, "removed_count": 0, "added": [], "removed": []},
            source_diff_binding={"path": "source.json", "sha256": "2" * 64},
            official_diff_binding={"path": "official.json", "sha256": "3" * 64},
            evidence_manifest_binding={"path": "evidence.json", "sha256": "4" * 64},
        )
        tampered = copy.deepcopy(inventory)
        tampered["items"][0]["fact"]["fingerprint"] = "5" * 64
        unsigned = dict(tampered)
        unsigned.pop("inventory_sha256")
        tampered["inventory_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "事实摘要"):
            artifacts.validate_discovery_inventory(tampered)

        wrong_count = copy.deepcopy(inventory)
        wrong_count["item_count"] = 0
        unsigned = dict(wrong_count)
        unsigned.pop("inventory_sha256")
        wrong_count["inventory_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "计数"):
            artifacts.validate_discovery_inventory(wrong_count)

    def test_affected_rule_change_changes_gate_requirements_digest(self) -> None:
        first = self._requirements(["SPEC-HDR-005"])
        second = self._requirements(["SPEC-HDR-005", "SPEC-MODEL-001"])
        self.assertNotEqual(first["requirements_sha256"], second["requirements_sha256"])

    def test_mapping_must_exactly_cover_requirements(self) -> None:
        requirements = self._requirements()
        mapping = self._mapping(requirements)
        mapping["gates"].pop()
        with self.assertRaisesRegex(artifacts.VCArtifactError, "精确覆盖"):
            artifacts.build_gate_plan(requirements, mapping)

    def test_mapping_rejects_wrong_requirement_digest(self) -> None:
        requirements = self._requirements()
        mapping = self._mapping(requirements)
        mapping["gates"][0]["requirement_sha256"] = "0" * 64
        with self.assertRaisesRegex(artifacts.VCArtifactError, "需求摘要"):
            artifacts.build_gate_plan(requirements, mapping)

    def test_gate_plan_requires_unique_test_and_literal_command(self) -> None:
        requirements, plan = self._gate_plan()
        self.assertEqual(plan["gate_count"], len(requirements["requirements"]))
        tampered = copy.deepcopy(plan)
        tampered["gates"][1]["test_id"] = tampered["gates"][0]["test_id"]
        unsigned = dict(tampered)
        unsigned.pop("plan_sha256")
        tampered["plan_sha256"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "重复"):
            artifacts.validate_gate_plan(tampered, requirements)

    def test_candidate_build_receipt_is_self_bound(self) -> None:
        candidate = self._candidate_build_receipt()
        self.assertEqual(artifacts.validate_candidate_build_receipt(candidate), candidate)
        tampered = copy.deepcopy(candidate)
        tampered["build"]["parameters"]["offline"] = False
        with self.assertRaisesRegex(artifacts.VCArtifactError, "parameters 摘要"):
            artifacts.validate_candidate_build_receipt(tampered)

    def test_legacy_candidate_build_receipt_remains_replayable(self) -> None:
        """v7 已封存的 v1 收据没有四份 v2 机器收据，仍须只读重放。"""

        candidate = self._candidate_build_receipt()
        candidate["schema_version"] = artifacts.LEGACY_CANDIDATE_BUILD_SCHEMA
        for field in (
            "build_inventory",
            "frontend_provenance",
            "image_inspection",
            "capability_probe",
        ):
            candidate.pop(field)
        unsigned = dict(candidate)
        unsigned.pop("receipt_digest")
        candidate["receipt_digest"] = artifacts.digest(unsigned)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "受管历史投影"):
            artifacts.validate_candidate_build_receipt(candidate)
        self.assertEqual(
            artifacts.validate_candidate_build_receipt(
                candidate,
                allow_legacy=True,
            ),
            candidate,
        )

    def test_candidate_delivery_has_distinct_purpose_endpoints(self) -> None:
        build_receipt = self._candidate_build_receipt()
        common = {
            "campaign_id": "codex-0_154_0-campaign",
            "campaign_manifest_sha256": "1" * 64,
            "candidate_id": "candidate-a",
            "attempt_id": "attempt-a",
            "target_version": "0.154.0",
            "candidate_identity_sha256": "2" * 64,
            "issued_at_utc": "2026-09-12T09:00:00Z",
        }
        build_binding = {
            "path": "candidate/build.json",
            "sha256": "3" * 64,
            "bytes": 10,
        }
        acceptance_binding = {
            "path": "candidate/acceptance.json",
            "sha256": "4" * 64,
            "bytes": 10,
        }
        validation = artifacts.build_candidate_delivery_receipt(
            **common,
            campaign_purpose="validation_only",
            build_receipt=build_binding,
            acceptance_fact=acceptance_binding,
            canonical_checkpoint=None,
            production_state="accepted_not_activated",
        )
        self.assertEqual(validation["release_state"], "ready_for_operator_release")

        production = artifacts.build_candidate_delivery_receipt(
            **common,
            campaign_purpose="production_replacement",
            build_receipt=build_binding,
            acceptance_fact=acceptance_binding,
            canonical_checkpoint={"path": "canonical/latest.json", "sha256": "5" * 64, "bytes": 10},
            production_state="restored_active",
        )
        self.assertEqual(production["release_state"], "production_active_restored")
        self.assertEqual(build_receipt["candidate_purpose"], "validation_only")

    def test_interrupted_recovery_contract_closes_execute_reuse_and_zero_boundary(self) -> None:
        contract = self._interrupted_recovery_contract()
        self.assertEqual(
            artifacts.validate_interrupted_recovery_contract(
                contract,
                self._campaign_plan(),
            ),
            contract,
        )

        wrong_partition = copy.deepcopy(contract)
        wrong_partition["source_attempt"]["completed_job_ids"] = ["job-a", "job-b"]
        wrong_partition["contract_sha256"] = artifacts.digest(
            {key: value for key, value in wrong_partition.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "执行／复用闭集"):
            artifacts.validate_interrupted_recovery_contract(wrong_partition)

        wrong_boundary = copy.deepcopy(contract)
        wrong_boundary["zero_request_boundary"]["reservation_exists"] = True
        wrong_boundary["contract_sha256"] = artifacts.digest(
            {key: value for key, value in wrong_boundary.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "零预约"):
            artifacts.validate_interrupted_recovery_contract(wrong_boundary)

    def test_interrupted_recovery_rejects_production_change_on_completed_job(self) -> None:
        contract = self._interrupted_recovery_contract()
        tampered = copy.deepcopy(contract)
        changed = tampered["tool_transition"]["changed_files"][0]
        changed["affected_job_ids"] = ["job-a"]
        tampered["tool_transition"]["affected_job_ids"] = ["job-a"]
        tampered["contract_sha256"] = artifacts.digest(
            {key: value for key, value in tampered.items() if key != "contract_sha256"}
        )
        with self.assertRaisesRegex(artifacts.VCArtifactError, "越过 failed/pending"):
            artifacts.validate_interrupted_recovery_contract(tampered)

    @staticmethod
    def _canonical_action(item_id: str, **overrides: object) -> dict[str, object]:
        subcommand, step = artifacts.CANONICAL_ITEM_COMMANDS[item_id]
        command = [
            "/usr/bin/python3",
            "/data/tools/official_client_capture/codex_upgrade.py",
            subcommand,
            "--campaign-dir",
            "/data/evidence/campaigns/c-1",
            "--candidate-id",
            "cand-1",
            "--attempt-id",
            "20260918T201610Z-2cd24cb43d446278",
        ]
        if step is None:
            command += ["--phase", "VC-5", "--retire-version", "0.149.1", "--approve-import-sha256", "a" * 64]
        else:
            command += ["--canonical-step", step]
        action = {
            "action_id": f"canonical-{artifacts.CANONICAL_VC5_ITEM_ORDER.index(item_id) + 1}-{step or 'import'}",
            "operation": f"VC-5:{item_id}",
            "timeout_seconds": 1800,
            "command": command,
            "item_ids": [item_id],
        }
        action.update(overrides)
        return action

    def _canonical_plan(self, actions: list[dict[str, object]], execute: list[str]) -> dict[str, object]:
        return {
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": sorted(execute),
            "reuse_item_ids": ["candidate-core-direct"],
            "actions": sorted(actions, key=lambda item: str(item["action_id"])),
        }

    def test_canonical_actions_follow_frozen_item_command_mapping(self) -> None:
        """canonical 四项与子命令一一对应，动作绑定从命令冻结提取，套名双向拒绝。"""

        items = sorted(artifacts.CANONICAL_VC5_ITEM_COMMANDS)
        plan = artifacts.validate_action_plan(
            self._canonical_plan([self._canonical_action(item) for item in items], items)
        )
        self.assertEqual(
            [action["item_ids"][0] for action in plan["actions"]],
            list(artifacts.CANONICAL_VC5_ITEM_ORDER),
        )
        binding = artifacts.canonical_batch_binding(
            plan["actions"], execute_item_ids=plan["execute_item_ids"]
        )
        self.assertEqual(
            binding,
            {
                "campaign_dir": "/data/evidence/campaigns/c-1",
                "candidate_id": "cand-1",
                "attempt_id": "20260918T201610Z-2cd24cb43d446278",
                "phase": "VC-5",
                "group": "VC-5",
                "item_ids": items,
            },
        )
        self.assertIsNone(
            artifacts.canonical_action_binding(
                {"command": ["/usr/bin/python3", "-c", "raise SystemExit(3)"], "item_ids": ["candidate-seal"]}
            )
        )

        def rejected(message: str, actions: list[dict[str, object]], execute: list[str]) -> None:
            with self.assertRaisesRegex(artifacts.VCArtifactError, message):
                artifacts.validate_action_plan(self._canonical_plan(actions, execute))

        # 给别的命令套 canonical item 名。
        rejected(
            "只能由子命令",
            [self._canonical_action("canonical-seal", command=["/usr/bin/python3", "-c", "raise SystemExit(0)"])],
            ["canonical-seal"],
        )
        # canonical 子命令挂在别的 item 名下。
        stray = self._canonical_action("canonical-seal", action_id="candidate-seal", item_ids=["candidate-seal"])
        rejected("必须以冻结的 canonical item 登记", [stray], ["candidate-seal"])
        # step 与 item 不符。
        wrong_step = self._canonical_action("canonical-seal")
        wrong_step["command"][-1] = "compare"
        rejected("--canonical-step 必须是", [wrong_step], ["canonical-seal"])
        # 批次内不得自带时间锚。
        anchored = self._canonical_action("canonical-import")
        anchored["command"] += ["--supervisor-run-dir", "/root/canon-anchor/run-x"]
        rejected("不得自带 --supervisor-run-dir", [anchored], ["canonical-import"])
        # canonical-import 必须是批准形态。
        preview = self._canonical_action("canonical-import")
        preview["command"] = preview["command"][:-2]
        rejected("approve-import-sha256", [preview], ["canonical-import"])
        # 一个动作承载两个 canonical 项。
        doubled = self._canonical_action("canonical-seal", item_ids=["canonical-compare", "canonical-seal"])
        rejected("只能精确承载一个", [doubled], ["canonical-compare", "canonical-seal"])
        # 混入其它 execute 项。
        rejected(
            "不得混入",
            [
                self._canonical_action("canonical-seal"),
                {
                    "action_id": "seal",
                    "operation": "VC-5:seal",
                    "timeout_seconds": 5,
                    "command": ["/usr/bin/python3", "-c", "raise SystemExit(0)"],
                    "item_ids": ["candidate-seal"],
                },
            ],
            ["candidate-seal", "canonical-seal"],
        )
        # 次序错误：按字母序排 action_id 会让 accept 先于 import 执行。
        unordered = [self._canonical_action(item, action_id=item) for item in items]
        rejected("import → seal → compare → accept", unordered, items)
        # 动作指向不同 attempt。
        other = self._canonical_action("canonical-compare")
        other["command"][other["command"].index("--attempt-id") + 1] = "20260918T000000Z-0000000000000000"
        rejected("同一 Campaign／Candidate／attempt", [self._canonical_action("canonical-seal"), other], ["canonical-compare", "canonical-seal"])
        # 缺少 --candidate-id。
        missing = self._canonical_action("canonical-accept")
        index = missing["command"].index("--candidate-id")
        del missing["command"][index : index + 2]
        rejected("--candidate-id", [missing], ["canonical-accept"])

    def test_new_schema_files_match_runtime_versions(self) -> None:
        root = Path(artifacts.__file__).resolve().parent
        expected = {
            "codex_upgrade_campaign_plan.schema.json": artifacts.CAMPAIGN_PLAN_SCHEMA,
            "codex_upgrade_vc_checkpoint.schema.json": artifacts.VC_CHECKPOINT_SCHEMA,
            "codex_upgrade_vc_batch.schema.json": artifacts.VC_BATCH_SCHEMA,
            "codex_upgrade_vc_action_plan.schema.json": artifacts.VC_ACTION_PLAN_SCHEMA,
            "codex_upgrade_interrupted_recovery_contract.schema.json": artifacts.INTERRUPTED_RECOVERY_CONTRACT_SCHEMA,
            "codex_upgrade_gate_requirements.schema.json": artifacts.GATE_REQUIREMENTS_SCHEMA,
            "codex_upgrade_gate_mapping.schema.json": artifacts.GATE_MAPPING_SCHEMA,
            "codex_upgrade_gate_plan.schema.json": artifacts.GATE_PLAN_SCHEMA,
            "codex_upgrade_candidate_build_receipt.schema.json": artifacts.CANDIDATE_BUILD_SCHEMA,
            "codex_upgrade_candidate_delivery_receipt.schema.json": artifacts.CANDIDATE_DELIVERY_SCHEMA,
            # 改造 4（staging/WAL）四种控制制品。
            "codex_upgrade_vc_commit.schema.json": artifacts.VC_COMMIT_SCHEMA,
            "codex_upgrade_vc_staging_marker.schema.json": artifacts.STAGING_MARKER_SCHEMA,
            "codex_upgrade_staging_abort.schema.json": artifacts.STAGING_ABORT_SCHEMA,
            "codex_upgrade_parent_start_failure.schema.json": artifacts.PARENT_START_FAILURE_SCHEMA,
            # 改造 2（候选级 revision）五种控制制品。
            "codex_upgrade_candidate_revision.schema.json": artifacts.CANDIDATE_REVISION_SCHEMA,
            "codex_upgrade_candidate_revision_commit.schema.json": artifacts.CANDIDATE_REVISION_COMMIT_SCHEMA,
            "codex_upgrade_candidate_revision_seal.schema.json": artifacts.CANDIDATE_REVISION_SEAL_SCHEMA,
            "codex_upgrade_candidate_invalidation_diagnosis.schema.json": artifacts.CANDIDATE_INVALIDATION_DIAGNOSIS_SCHEMA,
            "codex_upgrade_candidate_invalidation.schema.json": artifacts.CANDIDATE_INVALIDATION_SCHEMA,
            # 改造 5（评估失败局部恢复）十一种控制制品。
            "codex_upgrade_evaluation_baseline.schema.json": artifacts.EVALUATION_BASELINE_SCHEMA,
            "codex_upgrade_evaluation_baseline_prepared.schema.json": artifacts.EVALUATION_BASELINE_PREPARED_SCHEMA,
            "codex_upgrade_evaluation_baseline_authorization.schema.json": artifacts.EVALUATION_BASELINE_AUTHORIZATION_SCHEMA,
            "codex_upgrade_evaluation_baseline_commit.schema.json": artifacts.EVALUATION_BASELINE_COMMIT_SCHEMA,
            "codex_upgrade_evaluation_baseline_abandon.schema.json": artifacts.EVALUATION_BASELINE_ABANDON_SCHEMA,
            "codex_upgrade_evaluation_checkpoint.schema.json": artifacts.EVALUATION_CHECKPOINT_SCHEMA,
            "codex_upgrade_evaluation_run.schema.json": artifacts.EVALUATION_RUN_SCHEMA,
            "codex_upgrade_evaluation_failure_diagnosis.schema.json": artifacts.EVALUATION_FAILURE_DIAGNOSIS_SCHEMA,
            # 第三批 B3-3：tool-evolution 基线的重评触发事实。
            "codex_upgrade_evaluation_reevaluation.schema.json": artifacts.EVALUATION_REEVALUATION_SCHEMA,
            # 第三批 R5：approval-revision 基线的批准修订记录。
            "codex_upgrade_approval_revision.schema.json": artifacts.APPROVAL_REVISION_SCHEMA,
            "codex_upgrade_action_output_binding.schema.json": artifacts.ACTION_OUTPUT_BINDING_SCHEMA,
            "codex_upgrade_manifest_projection.schema.json": artifacts.MANIFEST_PROJECTION_SCHEMA,
            "codex_upgrade_effective_results.schema.json": artifacts.EFFECTIVE_RESULTS_SCHEMA,
            # R4：阶段幂等重派证明。
            "codex_upgrade_stage_replay.schema.json": artifacts.STAGE_REPLAY_SCHEMA,
            # R11：恢复段预约（执行／复用边界）；版本常量定义在编排器，由恢复段读写两端共用。
            "codex_upgrade_attempt_recovery_reservation.schema.json": upgrade.ATTEMPT_RECOVERY_RESERVATION_SCHEMA,
            # R8：三层预算的批准延期收据。
            "codex_upgrade_deadline_extension.schema.json": artifacts.DEADLINE_EXTENSION_SCHEMA,
            # 修好接着跑第 14 项：请求预算延长批准收据。
            "codex_upgrade_live_request_budget_extension.schema.json": artifacts.LIVE_REQUEST_BUDGET_EXTENSION_SCHEMA,
        }
        for name, schema_version in expected.items():
            with self.subTest(name=name):
                schema = json.loads((root / name).read_text(encoding="utf-8"))
                self.assertEqual(schema["properties"]["schema_version"]["const"], schema_version)
        # 第三批 B3-5 自查补：评估基线 recovery／COMMIT schema 的闭集枚举必须逐项等于运行时闭集
        # （B3-3 加 tool-evolution 时只改了 kind，漏了 failure_source 与 root_cause_class）。
        baseline_schema = json.loads((root / "codex_upgrade_evaluation_baseline.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(baseline_schema["properties"]["kind"]["enum"]), artifacts.EVALUATION_BASELINE_KINDS)
        self.assertEqual(tuple(baseline_schema["properties"]["failure_source"]["enum"]), artifacts.FAILURE_SOURCES)
        self.assertEqual(
            tuple(baseline_schema["properties"]["root_cause_class"]["enum"]),
            tuple(dict.fromkeys(artifacts.EVALUATION_BASELINE_ROOT_CAUSE_CLASSES.values())),
        )
        commit_schema = json.loads((root / "codex_upgrade_evaluation_baseline_commit.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(commit_schema["properties"]["kind"]["enum"]), artifacts.EVALUATION_BASELINE_KINDS)
        ledger_schema = json.loads((root / "codex_upgrade_timing_ledger.schema.json").read_text(encoding="utf-8"))
        ledger_baseline = ledger_schema["properties"]["summary"]["properties"]["current_evaluation_baseline"]["oneOf"][0]
        self.assertEqual(tuple(ledger_baseline["properties"]["baseline_kind"]["enum"]), artifacts.EVALUATION_BASELINE_KINDS)
        # batch/v3：候选两字段与评估基线三字段必填（Campaign 级／b0 为 null）；v2／v1 只读兼容由 validate_vc_batch 覆盖。
        batch_schema = json.loads((root / "codex_upgrade_vc_batch.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(batch_schema["properties"]["schema_version"]["const"], artifacts.VC_BATCH_SCHEMA)
        self.assertEqual(artifacts.VC_BATCH_SCHEMA, "codex-upgrade-vc-batch/v3")
        self.assertEqual(artifacts.VC_BATCH_V2_SCHEMA, "codex-upgrade-vc-batch/v2")
        self.assertEqual(artifacts.VC_BATCH_LEGACY_SCHEMA, "codex-upgrade-vc-batch/v1")
        self.assertTrue(
            {"candidate_revision", "candidate_id", "evaluation_baseline", "baseline_commit_sha256", "evaluator_digests"}
            <= set(batch_schema["required"])
        )
        self.assertIn("output_bindings", batch_schema["$defs"]["action"]["properties"])
        self.assertNotIn("output_bindings", batch_schema["$defs"]["action"]["required"])
        self.assertEqual(
            tuple(batch_schema["$defs"]["evaluatorDigests"]["required"]), artifacts.EVALUATOR_DIGEST_FIELDS
        )
        plan_schema_actions = json.loads((root / "codex_upgrade_vc_action_plan.schema.json").read_text(encoding="utf-8"))
        self.assertIn("output_bindings", plan_schema_actions["$defs"]["actions"]["items"]["properties"])
        # campaign-run v2／v3 清单：候选两字段与评估基线三字段成对可选（存在性由批次模型决定）。
        run_schema = json.loads((root / "codex_upgrade_campaign_run.schema.json").read_text(encoding="utf-8"))
        for version in ("v2", "v3"):
            definition = run_schema["$defs"][version]
            for field in ("candidate_revision", "candidate_id", "evaluation_baseline", "baseline_commit_sha256", "evaluator_digests"):
                self.assertIn(field, definition["properties"])
                self.assertNotIn(field, definition.get("required", []))
        self.assertIn("output_bindings", run_schema["$defs"]["actionV2"]["properties"])
        # stop-receipt v2：显式 action_outputs_sha256（sha256|null）必填；request 仍是 v1。
        stop_schema = json.loads((root / "codex_upgrade_supervisor_stop.schema.json").read_text(encoding="utf-8"))
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor

        self.assertEqual(stop_schema["properties"]["schema_version"]["const"], supervisor.STOP_RECEIPT_SCHEMA)
        self.assertEqual(supervisor.STOP_RECEIPT_SCHEMA, "codex-upgrade-supervisor-stop/v2")
        self.assertEqual(supervisor.STOP_REQUEST_SCHEMA, "codex-upgrade-supervisor-stop/v1")
        self.assertEqual(supervisor.STOP_RECEIPT_LEGACY_SCHEMA, "codex-upgrade-supervisor-stop/v1")
        self.assertIn("action_outputs_sha256", stop_schema["required"])
        self.assertEqual(supervisor.STAGING_COMMIT_STEPS[1], "evaluator-digests")
        # 单规则结果：投影模式两字段可选（历史文档没有）。
        result_schema = json.loads((root / "candidate_rule_assertion_result.schema.json").read_text(encoding="utf-8"))
        self.assertIn("projection_sha256", result_schema["properties"])
        self.assertNotIn("projection_sha256", result_schema["required"])
        # 计时账本：两个新状态与三个 revision 摘要字段。
        ledger_schema = json.loads((root / "codex_upgrade_timing_ledger.schema.json").read_text(encoding="utf-8"))
        summary = ledger_schema["properties"]["summary"]["properties"]
        self.assertEqual(
            summary["status"]["enum"],
            ["active", "recovery_required", "candidate_review_required", "stage_review_required", "revision_required", "stop_required", "stopped", "complete", "deadline_paused", "abandoned"],
        )
        for field in ("current_revision", "revision_phase_state", "campaign_completed_phases"):
            self.assertIn(field, summary)
        self.assertEqual(
            tuple(summary["revision_phase_state"]["additionalProperties"]["propertyNames"]["enum"]),
            artifacts.CANDIDATE_PHASES,
        )
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger

        self.assertEqual(timing_ledger.CANDIDATE_PHASES, artifacts.CANDIDATE_PHASES)
        self.assertTrue({"candidate_review_required", "candidate_invalidated", "stage_revision"} <= set(timing_ledger.EVENT_TYPES))
        self.assertTrue(
            {"evaluation_baseline", "attempt_recovery_started", "attempt_recovery_completed", "attempt_recovery_failed"}
            <= set(timing_ledger.EVENT_TYPES)
        )
        self.assertTrue(
            {"evaluation_baseline", "baseline_commit_sha256", "baseline_kind", "recovery_revision"}
            <= set(timing_ledger.EVENT_REVISION_FIELDS)
        )
        for field in ("current_evaluation_baseline", "attempt_recoveries"):
            self.assertIn(field, summary)

        # 根因表：候选源码变更根因已登记（component candidate，维度 phase）。
        from tools.official_client_capture import codex_upgrade_root_cause as root_cause

        entry = root_cause.load_codes()["codes"]["candidate.source-change-required"]
        self.assertEqual((entry["component"], tuple(entry["stable_dimensions"])), ("candidate", ("phase",)))
        codes = root_cause.load_codes()["codes"]
        self.assertEqual(
            (codes["evaluation.rule-failed"]["component"], tuple(codes["evaluation.rule-failed"]["stable_dimensions"])),
            ("evaluator", ("phase",)),
        )
        self.assertEqual(
            (codes["attempt.job-transient-failure"]["component"], tuple(codes["attempt.job-transient-failure"]["stable_dimensions"])),
            ("reconciler", ("phase",)),
        )
        abort_schema = json.loads((root / "codex_upgrade_staging_abort.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(abort_schema["properties"]["stage"]["enum"]), artifacts.STAGING_ABORT_STAGES)
        self.assertEqual(tuple(abort_schema["properties"]["failure_kind"]["enum"]), artifacts.STAGING_ABORT_FAILURE_KINDS)
        failure_schema = json.loads((root / "codex_upgrade_parent_start_failure.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(failure_schema["properties"]["failure_kind"]["enum"]), artifacts.PARENT_START_FAILURE_KINDS)
        plan_schema = json.loads((root / "codex_upgrade_campaign_plan.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(plan_schema["properties"]["batch_model"]["enum"]), artifacts.BATCH_MODELS)
        self.assertNotIn("batch_model", plan_schema["required"])

    # ------------------------------------------------------------------
    # 改造 4：总计划 batch_model 与四种 staging 制品
    # ------------------------------------------------------------------

    def _plan_kwargs(self) -> dict[str, object]:
        return {
            "campaign_id": "codex-0_154_0-campaign",
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "created_at_utc": "2026-09-14T00:00:00Z",
            "original_deadline_at_utc": "2026-09-14T12:00:00Z",
            "timing_checkpoint_sha256": "1" * 64,
            "arm64_environment_sha256": "2" * 64,
            "job_rehearsal_sha256": "3" * 64,
            "p0_gate_sha256": "4" * 64,
        }

    def test_campaign_plan_batch_model_defaults_to_staging_and_legacy_plan_digest_is_unchanged(self) -> None:
        staging = artifacts.build_campaign_plan(**self._plan_kwargs())
        legacy = artifacts.build_campaign_plan(**self._plan_kwargs(), batch_model=None)
        self.assertEqual(staging["batch_model"], "staging")
        self.assertNotIn("batch_model", legacy)
        self.assertEqual(artifacts.campaign_plan_batch_model(staging), "staging")
        self.assertEqual(artifacts.campaign_plan_batch_model(legacy), "legacy")
        # 历史 plan（无字段）的自摘要只由既有字段决定：与 legacy 构造完全一致。
        unsigned = {key: value for key, value in legacy.items() if key != "plan_sha256"}
        self.assertEqual(artifacts.digest(unsigned), legacy["plan_sha256"])
        self.assertNotEqual(staging["plan_sha256"], legacy["plan_sha256"])
        explicit_legacy = artifacts.build_campaign_plan(**self._plan_kwargs(), batch_model="legacy")
        self.assertEqual(explicit_legacy["batch_model"], "legacy")
        with self.assertRaisesRegex(artifacts.VCArtifactError, "batch_model"):
            artifacts.build_campaign_plan(**self._plan_kwargs(), batch_model="wal")
        tampered = dict(staging)
        tampered["batch_model"] = "wal"
        with self.assertRaisesRegex(artifacts.VCArtifactError, "batch_model"):
            artifacts.validate_campaign_plan(tampered)
        extra = dict(staging)
        extra["candidate_revision"] = 1
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不闭合"):
            artifacts.validate_campaign_plan(extra)

    def _staging_artifacts(self) -> dict[str, dict[str, object]]:
        marker = artifacts.build_staging_prepared_marker(
            campaign_id="codex-0_154_0-campaign",
            sequence=2,
            phase="VC-2",
            attempt=1,
            batch_sha256="5" * 64,
            manifest_sha256="6" * 64,
            owner_nonce="7" * 64,
            prepared_at_utc="2026-09-19T01:00:00Z",
        )
        commit = artifacts.build_vc_commit(
            campaign_id="codex-0_154_0-campaign",
            sequence=2,
            phase="VC-2",
            staging_attempt=1,
            batch_sha256="5" * 64,
            manifest_sha256="6" * 64,
            parent_run_dir="/srv/state/run-" + "7" * 64,
            owner_nonce="7" * 64,
            ledger_event_ids=["vc-batch-0002-vc-2-vc-2-started"],
            committed_at_utc="2026-09-19T01:00:05Z",
        )
        abort = artifacts.build_staging_abort(
            campaign_id="codex-0_154_0-campaign",
            campaign_plan_sha256="8" * 64,
            phase="VC-2",
            sequence=2,
            staging_attempt=1,
            stage="commit-publish",
            failure_kind="commit-failed",
            error_type="ConfigurationError",
            root_cause_id="rc1-" + "9" * 20,
            batch_sha256="5" * 64,
            manifest_sha256="6" * 64,
            parent_run_dir="/srv/state/run-" + "7" * 64,
            parent_run_state="aborted_prepared",
            reconciliation_receipt={"path": "control/reconciliation/run-x/supervisor-run-reconciliation.json", "sha256": "a" * 64},
            recorded_at_utc="2026-09-19T01:01:00Z",
        )
        failure = artifacts.build_parent_start_failure(
            campaign_id="codex-0_154_0-campaign",
            phase="VC-2",
            batch_sequence=2,
            batch_sha256="5" * 64,
            commit_sha256=str(commit["commit_sha256"]),
            owner_pid=4242,
            owner_nonce="7" * 64,
            failure_kind="owner-lost",
            error_type="OwnerProcessLost",
            recorded_at_utc="2026-09-19T01:02:00Z",
        )
        return {"marker": marker, "commit": commit, "abort": abort, "failure": failure}

    def test_staging_artifacts_are_closed_and_self_digested(self) -> None:
        built = self._staging_artifacts()
        validators = {
            "marker": (artifacts.validate_staging_prepared_marker, "marker_sha256"),
            "commit": (artifacts.validate_vc_commit, "commit_sha256"),
            "abort": (artifacts.validate_staging_abort, "receipt_sha256"),
            "failure": (artifacts.validate_parent_start_failure, "diagnostic_sha256"),
        }
        for name, (validate, digest_field) in validators.items():
            with self.subTest(name=name):
                payload = built[name]
                self.assertEqual(validate(payload), payload)
                unsigned = {key: value for key, value in payload.items() if key != digest_field}
                self.assertEqual(artifacts.digest(unsigned), payload[digest_field])
                extra = dict(payload)
                extra["adopt"] = True
                with self.assertRaisesRegex(artifacts.VCArtifactError, "不闭合"):
                    validate(extra)
        self.assertEqual(built["abort"]["live_request_count"], 0)
        self.assertFalse(built["failure"]["action_started"])
        self.assertFalse(built["failure"]["reservation_exists"])

    def test_staging_abort_optional_error_message_and_cleaning(self) -> None:
        """修好接着跑第 29 项：ABORT 收据可选携带原始异常文本 error_message（参与自摘要）；缺省时字段缺席、与历史
        收据同形；未知键仍不闭合；空串／超长／控制字符／首尾空白／非字符串拒绝；清洗函数去控制字符、截断、空退回类型名；
        schema 文件同步（可选、不进 required）。"""

        base = self._staging_artifacts()["abort"]
        self.assertNotIn("error_message", base)
        params = {
            key: value
            for key, value in base.items()
            if key not in {"schema_version", "live_request_count", "scanned_bytes", "receipt_sha256"}
        }
        original = "失败父 run run-x 尚未对账（缺对账收据）；先执行 reconcile-supervisor-run。"
        with_message = artifacts.build_staging_abort(**params, error_message=original)
        self.assertEqual(with_message["error_message"], original)
        self.assertEqual(artifacts.validate_staging_abort(with_message), with_message)
        unsigned = {key: value for key, value in with_message.items() if key != "receipt_sha256"}
        self.assertEqual(artifacts.digest(unsigned), with_message["receipt_sha256"])
        self.assertNotEqual(with_message["receipt_sha256"], base["receipt_sha256"])
        self.assertEqual(set(with_message) - set(base), {"error_message"})
        self.assertEqual(artifacts.build_staging_abort(**params), base)
        extra = dict(with_message)
        extra["note"] = "x"
        with self.assertRaisesRegex(artifacts.VCArtifactError, "不闭合"):
            artifacts.validate_staging_abort(extra)
        for bad in ("", "x" * (artifacts.STAGING_ABORT_ERROR_MESSAGE_MAX_CHARS + 1), "a\x00b", " padded", "tail ", 42):
            tampered = dict(with_message)
            tampered["error_message"] = bad
            tampered["receipt_sha256"] = artifacts.digest({k: v for k, v in tampered.items() if k != "receipt_sha256"})
            with self.subTest(bad=bad), self.assertRaisesRegex(artifacts.VCArtifactError, "error_message 非法"):
                artifacts.validate_staging_abort(tampered)
        self.assertEqual(artifacts.staging_abort_error_message(RuntimeError("  a\x1fb\nc  ")), "a b c")
        self.assertEqual(
            len(artifacts.staging_abort_error_message("y" * 5000)), artifacts.STAGING_ABORT_ERROR_MESSAGE_MAX_CHARS
        )
        self.assertEqual(artifacts.staging_abort_error_message(ValueError()), "ValueError")
        self.assertEqual(artifacts.staging_abort_error_message("   "), "unknown")
        schema_path = Path(artifacts.__file__).resolve().parent / "codex_upgrade_staging_abort.schema.json"
        abort_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            abort_schema["properties"]["error_message"],
            {"type": "string", "minLength": 1, "maxLength": artifacts.STAGING_ABORT_ERROR_MESSAGE_MAX_CHARS},
        )
        self.assertNotIn("error_message", abort_schema["required"])

    def test_staging_abort_error_message_truncation_never_leaves_trailing_whitespace(self) -> None:
        """修好接着跑第 32 项前置（第 29 项遗留）：原文第 2000 个字符恰是换行、制表或空格时，截断结果不得以空白结尾。

        否则 build_staging_abort 以"error_message 非法"拒绝，_write_staging_abort 抛配置错误盖住原始拒因、ABORT 写不出、
        根因无法入账；下一次入口的孤儿扫描再把这个 attempt 记成无原文的 staging.abandoned。链审计兜底文案（15 条协议
        拒因）多行且可超过 2000 字符，触发概率不低。
        """

        base = self._staging_artifacts()["abort"]
        params = {
            key: value
            for key, value in base.items()
            if key not in {"schema_version", "live_request_count", "scanned_bytes", "receipt_sha256"}
        }
        limit = artifacts.STAGING_ABORT_ERROR_MESSAGE_MAX_CHARS
        for separator in ("\n", " ", "\t", "\r\n"):
            with self.subTest(separator=repr(separator)):
                message = artifacts.staging_abort_error_message(RuntimeError("x" * (limit - 1) + separator + "y" * 600))
                self.assertEqual(message, "x" * (limit - 1))
                abort = artifacts.build_staging_abort(**params, error_message=message)
                self.assertEqual(artifacts.validate_staging_abort(abort)["error_message"], message)

    @staticmethod
    def _chain_audit_refusal(prior_run_hex: str, sequence: int, reason: str) -> str:
        """与监督器 _unclaimed_failed_batch_message 同形的链审计兜底文案（194249z 批次 17 实测形态）。"""

        return (
            "失败批次只能由唯一直接 v3 恢复后继承接。"
            f"前序 run run-{prior_run_hex}：phase VC-5、序号 {sequence - 1}、终态 failed／supervisor-stop／"
            "action-failed:candidate-recovery-run（种类 action-failed）、失败动作 candidate-recovery-run（来源 stop-reason）；"
            f"后继批次序号 {sequence}（phase VC-5，动作 candidate-recovery-preview）。"
            "各协议拒因：evolution_recovery：形态不符；seal_chain：形态不符；"
            f"candidate_recovery_run_retry：{reason}。"
            "未对账的失败前序先执行 reconcile-supervisor-run（run 期间无预约）或 reconcile-attempt（有预约）。"
        )

    def test_staging_abort_error_signature_is_stable_across_volatile_fragments(self) -> None:
        """修好接着跑第 32 项：归一化拒因签名 es1。

        同一拒因只在波动片段（run／attempt／Campaign ID、绝对路径、时间戳、十六进制摘要、0x 地址、UUID、数字、
        Python 集合字面量顺序）上不同时签名相同——上限保护不削弱；不同拒因签名不同——194249z 批次 17 的"失败父 run
        尚未对账"与"补跑失败后的预览的父动作诊断不是处理型失败"不再合并。签名基于清洗后的完整原文（不受 2000 字符截断
        影响），未截断时与收据原文复算一致；格式能作为根因维度（不触发波动值拒绝）。
        """

        signature = artifacts.staging_abort_error_signature
        run_one = "0123456789abcdef" * 4
        run_two = "fedcba9876543210" * 4
        unreconciled = lambda run: f"VC-5 补跑失败后继：失败父 run {run} 尚未对账（缺对账收据）；先执行 reconcile-supervisor-run"
        not_handled = "VC-5 补跑失败后的预览的父动作诊断不是处理型失败"
        first = signature(RuntimeError(self._chain_audit_refusal(run_one, 17, unreconciled(run_one))))
        again = signature(RuntimeError(self._chain_audit_refusal(run_two, 18, unreconciled(run_two))))
        second = signature(RuntimeError(self._chain_audit_refusal(run_one, 17, not_handled)))
        self.assertRegex(first, r"^es1-[0-9a-f]{16}$")
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        same_reason_pairs = [
            (
                "正式产物路径不可信：/root/docker/capture-cli/data/evidence/campaigns/c0157-formal-arm64-20260926t194249z/control/vc/batches/0017-vc-5.json",
                "正式产物路径不可信：/srv/other/campaigns/c0154-formal-bwg-20260914t223835z/control/vc/batches/0003-vc-2.json",
            ),
            ("attempt 20260927T231538Z-1f51c72cdc7dca99 尚未对账", "attempt 20260928T010203Z-0a1b2c3d4e5f6789 尚未对账"),
            ("截止 2026-09-28T10:08:05.123+08:00 已到", "截止 2026-10-02T00:00:00Z 已到"),
            ("批次摘要 3f2a9c0d1e 不一致", "批次摘要 9c81d0e7ab 不一致"),
            ("对象 <Job at 0x7f3a2b1c> 状态非法", "对象 <Job at 0x10a2b3c4d> 状态非法"),
            ("预约 123e4567-e89b-12d3-a456-426614174000 已占用", "预约 00000000-0000-4000-8000-00000000abcd 已占用"),
            ("漂移字段：{'actions', 'inputs'}", "漂移字段：{'inputs', 'actions'}"),
            ("owner pid 4242 已退出，重试 3 次", "owner pid 17 已退出，重试 12 次"),
            ("多行\n拒因\t文本", "多行 拒因  文本"),
        ]
        for left, right in same_reason_pairs:
            with self.subTest(left=left):
                self.assertEqual(signature(left), signature(right))
        different_reason_pairs = [
            ("漂移字段：{'actions'}", "漂移字段：{'inputs'}"),
            ("父动作诊断不是处理型失败", "父动作诊断缺失"),
            ("前序 run 已 stopped", "前序 run 未终态化"),
        ]
        for left, right in different_reason_pairs:
            with self.subTest(left=left):
                self.assertNotEqual(signature(left), signature(right))
        # 签名基于清洗后的完整原文：前 2000 字符相同、之后不同的两条拒因，收据原文相同而签名不同。
        limit = artifacts.STAGING_ABORT_ERROR_MESSAGE_MAX_CHARS
        long_left = RuntimeError("甲" * limit + "拒因一")
        long_right = RuntimeError("甲" * limit + "拒因二")
        self.assertEqual(
            artifacts.staging_abort_error_message(long_left), artifacts.staging_abort_error_message(long_right)
        )
        self.assertNotEqual(signature(long_left), signature(long_right))
        # 未截断时，由异常对象算出的签名与由收据原文复算的签名一致；空文本退回异常类型名（与原文同一口径）。
        for error in (RuntimeError(" 链审计\x1f拒绝\n "), ValueError(), RuntimeError(self._chain_audit_refusal(run_one, 17, not_handled))):
            with self.subTest(error=repr(error)[:40]):
                self.assertEqual(signature(error), signature(artifacts.staging_abort_error_message(error)))
        self.assertEqual(signature(ValueError()), signature("ValueError"))
        self.assertNotEqual(signature(ValueError()), signature(KeyError()))

    def test_staging_abort_optional_error_signature(self) -> None:
        """修好接着跑第 32 项：ABORT 可选携带 error_signature（参与自摘要）；缺省时字段缺席、与历史收据同形；签名必须伴随
        原文；格式非法拒绝；schema 文件同步（可选、pattern 与运行时一致、依赖 error_message）。"""

        base = self._staging_artifacts()["abort"]
        params = {
            key: value
            for key, value in base.items()
            if key not in {"schema_version", "live_request_count", "scanned_bytes", "receipt_sha256"}
        }
        message = "父 run 创建失败：失败父 run 0123 尚未对账（缺对账收据）"
        error_signature = artifacts.staging_abort_error_signature(message)
        signed = artifacts.build_staging_abort(**params, error_message=message, error_signature=error_signature)
        self.assertEqual(signed["error_signature"], error_signature)
        self.assertEqual(artifacts.validate_staging_abort(signed), signed)
        unsigned = {key: value for key, value in signed.items() if key != "receipt_sha256"}
        self.assertEqual(artifacts.digest(unsigned), signed["receipt_sha256"])
        self.assertEqual(set(signed) - set(base), {"error_message", "error_signature"})
        # 历史形态（第 29 项之前无原文、第 29 项起只有原文）字节不变。
        self.assertEqual(artifacts.build_staging_abort(**params), base)
        self.assertNotIn("error_signature", artifacts.build_staging_abort(**params, error_message=message))
        with self.assertRaisesRegex(artifacts.VCArtifactError, "error_signature"):
            artifacts.build_staging_abort(**params, error_signature=error_signature)
        for bad in ("es2-" + "0" * 16, "es1-" + "g" * 16, "es1-" + "0" * 15, "", 42):
            tampered = dict(signed)
            tampered["error_signature"] = bad
            tampered["receipt_sha256"] = artifacts.digest({k: v for k, v in tampered.items() if k != "receipt_sha256"})
            with self.subTest(bad=bad), self.assertRaisesRegex(artifacts.VCArtifactError, "error_signature 非法"):
                artifacts.validate_staging_abort(tampered)
        schema_path = Path(artifacts.__file__).resolve().parent / "codex_upgrade_staging_abort.schema.json"
        abort_schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            abort_schema["properties"]["error_signature"],
            {"type": "string", "pattern": artifacts.STAGING_ABORT_ERROR_SIGNATURE_RE.pattern},
        )
        self.assertNotIn("error_signature", abort_schema["required"])
        self.assertEqual(abort_schema["dependentRequired"], {"error_signature": ["error_message"]})

    def test_staging_artifacts_reject_semantic_tampering(self) -> None:
        built = self._staging_artifacts()
        cases = [
            ("marker", artifacts.validate_staging_prepared_marker, {"attempt": 0}, "marker_sha256"),
            ("commit", artifacts.validate_vc_commit, {"parent_run_dir": "relative/run"}, "commit_sha256"),
            ("commit", artifacts.validate_vc_commit, {"ledger_event_ids": ["a", "a"]}, "commit_sha256"),
            ("abort", artifacts.validate_staging_abort, {"stage": "commit-nonce"}, "receipt_sha256"),
            ("abort", artifacts.validate_staging_abort, {"live_request_count": 1}, "receipt_sha256"),
            ("abort", artifacts.validate_staging_abort, {"root_cause_id": "rc2-" + "9" * 20}, "receipt_sha256"),
            ("failure", artifacts.validate_parent_start_failure, {"action_started": True}, "diagnostic_sha256"),
            ("failure", artifacts.validate_parent_start_failure, {"failure_kind": "watchdog"}, "diagnostic_sha256"),
        ]
        for name, validate, patch, digest_field in cases:
            with self.subTest(name=name, patch=patch):
                # 只改字段不改摘要 → 摘要不一致；重算摘要 → 语义校验拒绝。
                tampered = dict(built[name])
                tampered.update(patch)
                with self.assertRaises(artifacts.VCArtifactError):
                    validate(tampered)
                unsigned = {key: value for key, value in tampered.items() if key != digest_field}
                rehashed = {**tampered, digest_field: artifacts.digest(unsigned)}
                with self.assertRaises(artifacts.VCArtifactError):
                    validate(rehashed)


if __name__ == "__main__":
    unittest.main()
