"""Claude VC-5 验收的 Store 写入层（Claude 升级控制面设计 B4-06a）。

全部离线、零请求。Store 侧用 Store 测试夹具建到 ProfileApproval（批准用途 production_replacement），再按本模块的候选门禁计划
形状冻结候选、建 ValidationExecutionPlan 与 ValidationAttempt、走完场景四阶段并封存 CandidateEvidencePackage（VC-5 前缀，
B4-01／B4-02 落地前由夹具代建）；编排器侧用 B4-04 的合成 Claude Campaign（计时账本与评估基线目录）。候选外部门禁收据用
``codex_upgrade_gate_receipt`` 原样生成与重放（环境收据按 Codex 门禁收据测试同一做法换成替身）。覆盖：

* b0 全链：PAIR v2 → 三条门禁定稿 → Acceptance v2 → selector 后态 → ``validation_completed``，Store 到 ``ready``、生产仍
  ``not_activated``；同一请求重跑逐步复用、事实与收据数不变；
* 每一步之后被强杀，重跑同一请求收敛到与一次跑完相同的事实集合；
* 零写入拒绝：subject 漂移、执行计划主机与收据不符、b0 规则行不全、selector 后态 Catalog 变化、同一 attempt 的门禁已有
  不同结果、门禁已在别的 attempt 通过；
* 评估基线：b1 重跑受影响规则写 PAIR／Acceptance v3；b2 不重跑任何规则时 Acceptance v3 沿用 b1 的有效 PAIR；
* 门禁计划形状、需求摘要、处置映射、版本门槛合同（Claude 版本恒走完整 VC 合同）与收据绑定重放。
"""

from __future__ import annotations

import collections
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade_accept as accept
from tools.official_client_capture import claude_upgrade_baseline as baseline_module
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_baseline as baseline_tests
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.contracts import candidate_identity_sha256
from tools.official_client_control.receipts import finalize_candidate_build
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

CANDIDATE = baseline_tests.CANDIDATE
TARGET_VERSION = "2.1.285"
PURPOSE = "production_replacement"
HOST = "runner-1"
ARCHITECTURE = "linux/amd64"
ISSUED_AT = "2026-09-02T00:00:00Z"
IDENTITY = {
    "profile_id": "claude-2.1.285",
    "profile_digest": "1" * 64,
    "candidate_package_digest": "2" * 64,
    "candidate_image_id": f"sha256:{'4' * 64}",
    "candidate_image_reference": f"registry/sub2api@sha256:{'4' * 64}",
}


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _binding(path: Path, root: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class AcceptWorld:
    """一个独立的验收世界：Store 夹具到 VC-5 前缀、合成 Claude Campaign、候选外部门禁收据根。"""

    def __init__(self, root: Path, *, plan_overrides: dict[str, dict[str, str]] | None = None) -> None:
        root.mkdir(parents=True, mode=0o700)
        self.root = root
        store_root = root / "store-fixture"
        store_root.mkdir(mode=0o700)
        self.fixture = SyntheticCampaign(store_root, schema="v2")
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve(PURPOSE)
        self.store = self.fixture.store
        self.campaign_id = self.fixture.campaign_id
        self.refs = self.fixture.references
        self.freeze_candidate()
        self.plan_gates = accept.candidate_external_gate_plan_entries(host_id=HOST, architecture=ARCHITECTURE, overrides=plan_overrides)
        self.attempt_ref, self.package_ref, self.scenario_ref = self.open_attempt(1, self.plan_gates)
        self.campaign = baseline_tests.ClaudeBaselineCampaign(root / "orchestrator")
        self.campaign.manifest.update(
            {"campaign_id": self.campaign_id, "campaign_purpose": PURPOSE, "target_version": TARGET_VERSION}
        )
        (self.campaign.campaign_dir / "campaign.json").write_text(json.dumps(self.campaign.manifest, ensure_ascii=False), encoding="utf-8")
        self.gate_root = root / "gate-evidence"
        self.gate_root.mkdir(mode=0o700)

    # -- Store：候选冻结与 VC-5 前缀（B4-01／B4-02 落地前由夹具代建） --------------------------------------

    def freeze_candidate(self) -> None:
        fixture = self.fixture
        build_input = fixture.candidate_build_input("strict")
        self.refs["candidate-build-receipt"] = finalize_candidate_build(self.store, self.campaign_id, build_input)
        candidate = {
            "schema_version": "official-client-validation-candidate/v2",
            "candidate_id": CANDIDATE,
            "profile_approval_ref": self.refs["profile-approval"],
            "release_artifact_ref": self.refs["release"],
            "support_envelope_ref": self.refs["support"],
            "source_tree_sha256": "7" * 64,
            "test_tree_sha256": "8" * 64,
            "dependency_lock_sha256": "9" * 64,
            "target_architecture": ARCHITECTURE,
            "build_id": "synthetic-build-strict",
            "image_digest": f"sha256:{'1' * 64}",
            "candidate_purpose": build_input["candidate_purpose"],
            "candidate_build_receipt_ref": self.refs["candidate-build-receipt"],
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        self.candidate_payload = candidate
        self.refs["candidate-strict"] = self.store.append_fact(self.campaign_id, "candidate_frozen", candidate, fixture._time())
        self.refs["runtime-before"] = self.snapshot("d" * 64)
        self.refs["selector-before"] = self.store.append_fact(
            self.campaign_id,
            "selector_observed",
            {"catalog_snapshot_ref": self.refs["runtime-before"], "observation_kind": "read_only"},
            fixture._time(),
        )

    def snapshot(self, catalog_digest: str) -> dict[str, Any]:
        return self.store.seal_object(
            "runtime_catalog_snapshot",
            {
                "schema_version": "official-client-runtime-catalog-snapshot/v1",
                "persona": self.fixture.persona,
                "catalog_digest": catalog_digest,
                "production_active_ref": None,
                "production_rollback_ref": None,
                "observed_at_utc": self.fixture._time(),
                "source_ref": self.fixture.runtime_binding,
            },
        )

    def open_attempt(self, number: int, plan_gates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """封存执行计划、建 ValidationAttempt、场景四阶段与 CandidateEvidencePackage；返回 (attempt, package, 场景批准) 引用。"""

        fixture = self.fixture
        purpose_tool = fixture.tool_sha256
        run_conditions = {
            "run_nonce": f"nonce-accept-{number}",
            "environment_sha256": "4" * 64,
            "tool_sha256": purpose_tool,
            "configuration_sha256": "5" * 64,
            "network_sha256": "6" * 64,
            "account_model_sha256": "a" * 64,
        }
        execute = sorted(["baseline", *(gate["gate_id"] for gate in plan_gates if gate["source"] == "execute")])
        reuse = sorted(gate["gate_id"] for gate in plan_gates if gate["source"] == "reuse")
        vircs = {"ownership": "operator_managed", "verification": "unverified", "action": "not_touched"}
        plan = {
            "schema_version": "official-client-validation-execution-plan/v1",
            "campaign_id": self.campaign_id,
            "persona": fixture.persona,
            "plan_id": f"validation-plan-accept-{number}",
            "candidate_id": CANDIDATE,
            "candidate_ref": self.refs["candidate-strict"],
            "candidate_build_receipt_ref": self.refs["candidate-build-receipt"],
            "profile_approval_ref": self.refs["profile-approval"],
            "scenario_plan_ref": self.refs["scenario-plan"],
            "atomic_assertion_ledger_ref": self.refs["atomic-assertion-ledger"],
            "validation_scope_sha256": fixture.candidate_build_input("strict")["validation_scope_sha256"],
            "execute_item_ids": execute,
            "reuse_item_ids": reuse,
            "external_gates": plan_gates,
            "run_conditions": run_conditions,
            "production_selector_before_ref": self.refs["selector-before"],
            "vircs_state": vircs,
            "identity_sha256": "",
        }
        plan["identity_sha256"] = canonical_sha256({key: value for key, value in plan.items() if key != "identity_sha256"})
        plan_ref = self.store.seal_object("validation_execution_plan", plan)
        attempt = {
            "schema_version": "official-client-validation-attempt/v1",
            "attempt_id": f"attempt-accept-{number}",
            "candidate_id": CANDIDATE,
            "candidate_ref": self.refs["candidate-strict"],
            "candidate_build_receipt_ref": self.refs["candidate-build-receipt"],
            "validation_execution_plan_ref": plan_ref,
            "run_conditions": run_conditions,
            "production_selector_before_ref": self.refs["selector-before"],
            "vircs_state": vircs,
            "result": "created",
            "identity_sha256": "",
        }
        attempt["identity_sha256"] = canonical_sha256({key: value for key, value in attempt.items() if key != "identity_sha256"})
        attempt_ref = self.store.append_fact(self.campaign_id, "validation_attempt_created", attempt, fixture._time())
        previous: dict[str, Any] | None = None
        for stage, kind, result in (
            ("prepare", "scenario_prepared", "prepared"),
            ("capture", "scenario_captured", "pass"),
            ("seal", "scenario_sealed", "pass"),
            ("approve", "scenario_approved", "pass"),
        ):
            scenario: dict[str, Any] = {
                "schema_version": "official-client-scenario-stage/v2",
                "candidate_id": CANDIDATE,
                "scenario_id": "baseline",
                "attempt_id": attempt["attempt_id"],
                "validation_attempt_ref": attempt_ref,
                "stage": stage,
                "previous_stage_ref": previous,
                "artifact_refs": [] if stage == "prepare" else [self.refs["operational-evidence"]],
                "result": result,
            }
            if stage == "approve":
                scenario |= {"reviewer": "synthetic-reviewer", "review_ref": f"review/scenario-accept-{number}"}
            previous = self.store.append_fact(self.campaign_id, kind, scenario, fixture._time())
        assert previous is not None
        package = {
            "schema_version": "official-client-candidate-evidence-package/v1",
            "campaign_id": self.campaign_id,
            "persona": fixture.persona,
            "target_version": "1.0.0",
            "package_id": f"candidate-evidence-accept-{number}",
            "candidate_id": CANDIDATE,
            "attempt_id": attempt["attempt_id"],
            "candidate_ref": self.refs["candidate-strict"],
            "candidate_build_receipt_ref": self.refs["candidate-build-receipt"],
            "validation_attempt_ref": attempt_ref,
            "atomic_assertion_ledger_ref": self.refs["atomic-assertion-ledger"],
            "scenario_approval_refs": [previous],
            "scenario_assertion_results": [],
            "raw_evidence_inventory": [fixture.evidence_binding],
            "environment_before_ref": self.refs["operational-evidence"],
            "environment_after_ref": self.refs["operational-evidence"],
            "recovery_evidence_ref": self.refs["operational-evidence"],
            "secret_scan_evidence_ref": self.refs["operational-evidence"],
            "artifact_count": 1,
            "inventory_sha256": canonical_sha256([fixture.evidence_binding]),
            "identity_sha256": "",
        }
        package["identity_sha256"] = canonical_sha256({key: value for key, value in package.items() if key != "identity_sha256"})
        package_ref = self.store.seal_object("candidate_evidence_package", package)
        return attempt_ref, package_ref, previous

    # -- 候选外部门禁收据 ----------------------------------------------------------------------------------

    def subject(self, identity: dict[str, Any] | None = None) -> dict[str, Any]:
        expected = accept.expected_gate_subject(self.campaign.manifest, self.candidate_payload, identity or IDENTITY)
        return {**expected, "production_tree_sha256": None, "acceptance_sha256": None, "promotion_receipt_sha256": None}

    def gate_receipt(self, name: str = "candidate-receipt.json", *, stdout: str = "6", host: str = HOST, identity: dict[str, Any] | None = None) -> str:
        """用 codex_upgrade_gate_receipt 原样生成一份全部通过的候选外部门禁收据，返回证据根内的相对路径。"""

        stem = Path(name).stem
        gates: list[dict[str, Any]] = []
        for index, gate_id in enumerate(sorted(gate_receipt.CANDIDATE_COMMANDS)):
            working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
            evidence = _write_json(self.gate_root / "evidence" / f"{stem}-{gate_id}.json", {"gate_id": gate_id, "passed": True})
            gates.append(
                {
                    "gate_id": gate_id, "command": list(command), "working_directory": working_directory, "host": host,
                    "architecture": ARCHITECTURE, "started_at_utc": f"2026-08-23T00:{index:02d}:00Z",
                    "completed_at_utc": f"2026-08-23T00:{index:02d}:30Z", "exit_code": 0, "status": "passed",
                    "passed_count": 1, "failed_count": 0, "skipped_count": 0, "stdout_sha256": stdout * 64,
                    "stderr_sha256": "7" * 64, "evidence": [_binding(evidence, self.gate_root)],
                }
            )
        attempt_id = f"gate-{stem}"
        environment = {
            role: _binding(_write_json(self.gate_root / "environment" / f"{attempt_id}-{role}.json", {"attempt_id": attempt_id, "role": role}), self.gate_root)
            for role in ("before", "after")
        }
        facts = {
            "schema_version": gate_receipt.FACTS_SCHEMA, "phase": gate_receipt.CANDIDATE_PHASE,
            "attempt": {"attempt_id": attempt_id, "root_cause_id": None, "previous_receipt": None},
            "subject": self.subject(identity), "inputs": [], "gate_plan": None, "environment": environment, "gates": gates,
        }
        _write_json(self.gate_root / f"{stem}-facts.json", facts)
        gate_receipt.finalize(self.gate_root, f"{stem}-facts.json", name)
        return name

    def rows(self) -> list[dict[str, Any]]:
        operational = self.refs["operational-evidence"]
        return [
            {
                "spec_id": "SPEC-001",
                "condition_sha256": "c" * 64,
                "scenario_approval_refs": [self.scenario_ref],
                "official_result": {
                    "ingress_id": "official-cli", "translation": "lossless", "result": "pass",
                    "final_wire_sha256": "a" * 64, "evidence_refs": [operational],
                },
                "candidate_result": {"result": "pass", "final_wire_sha256": "a" * 64, "evidence_refs": [operational]},
                "third_party_results": [
                    {
                        "protocol_class": "responses", "ingress_id": "third-responses", "translation": "lossless",
                        "result": "pass", "final_wire_sha256": "a" * 64, "evidence_refs": [operational],
                    }
                ],
                "dynamic_field_checks": [{"id": "dynamic-session", "dimensions": ["format", "lifecycle", "relation", "source"], "result": "pass"}],
                "assertion_results": [
                    {"assertion_id": "ASSERT-SPEC-001-001", "owner_kind": "required_rule", "owner_id": "SPEC-001", "result": "pass", "source": "execute"}
                ],
                "implementation_anchors": ["backend/synthetic.go:1"],
                "test_anchors": ["backend/synthetic_test.go:1"],
                "result_source": "execute",
                "comparison_mode": "dual_wire",
            }
        ]

    def request(self, *, receipt: str = "candidate-receipt.json", rows: list[dict[str, Any]] | None = None, snapshot: dict[str, Any] | None = None, identity: dict[str, Any] | None = None, issued_at: str = ISSUED_AT) -> dict[str, Any]:
        return {
            "candidate_id": CANDIDATE,
            "validation_attempt_ref": self.attempt_ref,
            "candidate_evidence_package_ref": self.package_ref,
            "pair_rows": self.rows() if rows is None else rows,
            "external_gate": {"evidence_root": str(self.gate_root), "receipt_path": receipt, "identity": dict(identity or IDENTITY)},
            "inventory_assertion_refs": [self.refs["operational-evidence"]],
            "selector_after_snapshot_ref": snapshot or self.refs["runtime-before"],
            "issued_at_utc": issued_at,
        }

    # -- 观察 -------------------------------------------------------------------------------------------

    def footprint(self) -> tuple[dict[str, int], int]:
        kinds = collections.Counter(fact["fact_kind"] for fact in self.store.list_facts(self.campaign_id))
        return dict(kinds), len(self.store.list_receipt_refs("validation_gate"))

    def status(self) -> dict[str, Any]:
        return dict(claude_upgrade_seal.store_cli(["status", "--store", self.fixture.store_root, "--campaign", self.campaign_id]))

    def run(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return accept.record_acceptance_chain(self.campaign.campaign_dir, self.campaign.manifest, request or self.request())


class _AcceptCase(unittest.TestCase):
    tree: ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="claude-accept-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        self.addCleanup(self.tree.restore)
        # 环境收据替身（与 Codex 门禁收据测试同一做法）：前后两份都按 attempt 身份通过、连续性摘要相同。
        environment = gate_receipt.codex_upgrade_arm64_environment_receipt
        for name, side_effect in (
            ("replay", self._replay_environment),
            ("receipts_equivalent", lambda left_root, left, right_root, right: left["continuity_identity_sha256"] == right["continuity_identity_sha256"]),
        ):
            patcher = mock.patch.object(environment, name, side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.worlds: dict[Path, AcceptWorld] = {}
        patcher = mock.patch.object(claude_upgrade_store, "open_campaign_store", side_effect=self._open_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _replay_environment(root: Path, relative: str) -> dict[str, Any]:
        del root
        attempt_id, role = Path(relative).stem.rsplit("-", 1)
        return {
            "status": "passed",
            "phase": "gate_before" if role == "before" else "gate_after",
            "subject_id": attempt_id,
            "continuity_identity_sha256": "8" * 64,
        }

    def _open_store(self, campaign_dir: Path, manifest: Any) -> tuple[Any, dict[str, Any]]:
        del manifest
        world = self.worlds[Path(campaign_dir).resolve()]
        return world.store, world.store.load_campaign(world.campaign_id)

    def world(self, name: str = "world", **options: Any) -> AcceptWorld:
        world = AcceptWorld(self.root / name, **options)
        self.worlds[world.campaign.campaign_dir.resolve()] = world
        return world


class ChainTests(_AcceptCase):
    def test_b0_chain_reaches_ready_and_rerun_reuses_everything(self) -> None:
        world = self.world()
        world.gate_receipt()
        result = world.run()
        self.assertEqual((result["evaluation_baseline"], result["pair_schema"], result["acceptance_schema"]), (0, "official-client-pair/v2", "official-client-acceptance/v2"))
        status = world.status()
        self.assertEqual((status["checkpoint"], status["production_state"]), ("ready", "not_activated"))
        acceptance = world.store.load_fact(result["acceptance"]["fact_ref"])["payload"]
        self.assertEqual((acceptance["result"], acceptance["acceptance_purpose"]), ("accepted", PURPOSE))
        self.assertEqual(sorted(record["gate_id"] for record in result["gate_receipts"]), sorted(gate_receipt.CANDIDATE_COMMANDS))
        footprint = world.footprint()
        self.assertEqual(footprint[1], 3)
        self.assertEqual(
            {kind: footprint[0][kind] for kind in ("pair_recorded", "acceptance_recorded", "selector_observed", "validation_completed")},
            {"pair_recorded": 1, "acceptance_recorded": 1, "selector_observed": 2, "validation_completed": 1},
        )
        # 同一请求重跑：每一步都复用原事实或原收据，Store 不增一条。
        again = world.run()
        self.assertTrue(all(item["reused"] for item in again["pairs"]))
        self.assertTrue(all(item["reused"] for item in again["gate_receipts"]))
        self.assertTrue(again["acceptance"]["reused"] and again["selector_after"]["reused"] and again["validation_completed"]["reused"])
        self.assertEqual(again["validation_completed"]["fact_ref"], result["validation_completed"]["fact_ref"])
        self.assertEqual(world.footprint(), footprint)
        # 门禁收据绑定可从根与相对路径独立重放。
        payload = accept.replay_bound_candidate_external_gate(result["external_gate"], expected_subject=accept.expected_gate_subject(world.campaign.manifest, world.candidate_payload, IDENTITY))
        self.assertEqual(payload["status"], "passed")

    def test_kill_after_each_store_write_converges(self) -> None:
        reference = self.world("reference")
        reference.gate_receipt()
        reference.run()
        expected = reference.footprint()
        real_cli = claude_upgrade_seal.store_cli
        # 写入顺序：PAIR(1) → 门禁定稿(2～4) → Acceptance(5) → selector 后态(6) → validation-finalize(7)。
        for kill_after in (1, 3, 5, 6, 7):
            with self.subTest(kill_after=kill_after):
                world = self.world(f"kill-{kill_after}")
                world.gate_receipt()
                calls = {"count": 0}

                def killing_cli(argv: Any) -> Any:
                    outcome = real_cli(argv)
                    if argv[0] != "status":
                        calls["count"] += 1
                        if calls["count"] == kill_after:
                            raise KeyboardInterrupt
                    return outcome

                with mock.patch.object(claude_upgrade_seal, "store_cli", side_effect=killing_cli):
                    with self.assertRaises(KeyboardInterrupt):
                        world.run()
                world.run()
                self.assertEqual(world.footprint(), expected)
                self.assertEqual(world.status()["checkpoint"], "ready")


class RejectionTests(_AcceptCase):
    def assert_zero_write(self, world: AcceptWorld, request: dict[str, Any], pattern: str) -> None:
        before = world.footprint()
        with self.assertRaisesRegex(ConfigurationError, pattern):
            world.run(request)
        self.assertEqual(world.footprint(), before)

    def test_precheck_rejections_write_nothing(self) -> None:
        world = self.world()
        world.gate_receipt()
        wrong = dict(IDENTITY, profile_digest="9" * 64)
        self.assert_zero_write(world, world.request(identity=wrong), r"subject 与 Campaign／候选身份不一致（profile_digest）")
        self.assert_zero_write(world, world.request(rows=[]), r"b0 的规则行必须覆盖批准的目标规则全集，缺 SPEC-001")
        self.assert_zero_write(world, world.request(snapshot=world.snapshot("e" * 64)), r"Active／Rollback／Catalog 发生变化（catalog_digest）")
        bad_row = dict(world.rows()[0], result_source="sometimes")
        self.assert_zero_write(world, world.request(rows=[bad_row]), r"规则 SPEC-001 的 PAIR 不符合合同")
        self.assert_zero_write(world, world.request(rows=[dict(world.rows()[0], extra=1)]), r"规则行的字段必须恰好是")
        # 门禁结果不同：先完整跑一遍，再拿另一份 stdout 不同的收据重跑——同一 attempt 的门禁收据不得覆盖。
        world.run()
        world.gate_receipt("other-receipt.json", stdout="9")
        self.assert_zero_write(world, world.request(receipt="other-receipt.json"), r"外部门禁 check-egress-spec 已有不同结果（output_sha256）")

    def test_plan_host_mismatch_writes_nothing(self) -> None:
        world = self.world(plan_overrides={"full-regression": {"host_id": "runner-2"}})
        world.gate_receipt()
        self.assert_zero_write(world, world.request(), r"门禁 full-regression 的执行与执行计划不一致（host_id）")

    def test_gate_passed_in_earlier_attempt_cannot_be_executed_again(self) -> None:
        world = self.world()
        world.gate_receipt()
        world.run()
        attempt_ref, package_ref, _scenario = world.open_attempt(2, world.plan_gates)
        request = dict(world.request(), validation_attempt_ref=attempt_ref, candidate_evidence_package_ref=package_ref)
        self.assert_zero_write(world, request, r"门禁 check-egress-spec 已在别的 attempt 通过：必须复用原收据")


class BaselineTests(_AcceptCase):
    def test_b1_rewrites_rules_as_v3_and_b2_inherits_effective_pairs(self) -> None:
        world = self.world()
        world.gate_receipt()
        b0 = world.run()
        campaign_dir = world.campaign.campaign_dir
        commit1 = world.campaign.commit_baseline(1)
        baseline_module.converge_store_baseline(campaign_dir, world.campaign.manifest, CANDIDATE)
        b1 = world.run(world.request(issued_at="2026-09-03T00:00:00Z"))
        self.assertEqual((b1["evaluation_baseline"], b1["pair_schema"], b1["acceptance_schema"]), (1, "official-client-pair/v3", "official-client-acceptance/v3"))
        pair = world.store.load_fact(b1["pairs"][0]["fact_ref"])["payload"]
        self.assertEqual((pair["evaluation_baseline"], pair["baseline_commit_sha256"]), (1, commit1["commit_sha256"]))
        self.assertNotEqual(b1["effective_pair_refs"]["SPEC-001"], b0["effective_pair_refs"]["SPEC-001"])
        acceptance = world.store.load_fact(b1["acceptance"]["fact_ref"])["payload"]
        self.assertEqual((acceptance["evaluation_baseline"], acceptance["pair_refs"]), (1, [b1["effective_pair_refs"]["SPEC-001"]]))
        # 门禁收据属于同一 attempt：b1 复用 b0 定稿的三份。
        self.assertTrue(all(item["reused"] for item in b1["gate_receipts"]))
        self.assertEqual(world.status()["checkpoint"], "ready")
        # b2 不重跑任何规则：Acceptance v3 沿用 b1 的有效 PAIR。
        world.campaign.commit_baseline(2, previous=commit1)
        baseline_module.converge_store_baseline(campaign_dir, world.campaign.manifest, CANDIDATE)
        b2 = world.run(world.request(rows=[], issued_at="2026-09-04T00:00:00Z"))
        self.assertEqual((b2["evaluation_baseline"], b2["pairs"]), (2, []))
        self.assertEqual(b2["effective_pair_refs"], b1["effective_pair_refs"])
        self.assertEqual(world.status()["checkpoint"], "ready")


class GatePlanTests(unittest.TestCase):
    def test_plan_entries_requirements_and_version_threshold(self) -> None:
        entries = accept.candidate_external_gate_plan_entries(host_id=HOST, architecture=ARCHITECTURE)
        self.assertEqual([item["gate_id"] for item in entries], sorted(gate_receipt.CANDIDATE_COMMANDS))
        self.assertTrue(all(item["source"] == "execute" and item["reused_receipt_ref"] is None for item in entries))
        self.assertEqual(
            [item["requirement_sha256"] for item in entries],
            [accept.candidate_gate_requirement_sha256(gate_id) for gate_id in sorted(gate_receipt.CANDIDATE_COMMANDS)],
        )
        self.assertEqual(len({item["requirement_sha256"] for item in entries}), 3)
        receipt_ref = {"receipt_kind": "validation_gate", "sha256": "f" * 64}
        reused = accept.candidate_external_gate_plan_entries(host_id=HOST, architecture=ARCHITECTURE, reused_receipt_refs={"full-regression": receipt_ref})
        self.assertEqual([item["source"] for item in reused], ["execute", "reuse", "execute"])
        for options, pattern in (
            ({"reused_receipt_refs": {"x": receipt_ref}}, "非候选外部门禁"),
            ({"reused_receipt_refs": {"full-regression": receipt_ref}, "previous_failed_receipt_refs": {"full-regression": receipt_ref}}, "既复用又重跑"),
            ({"overrides": {"target-platform": {"command": "x"}}}, "只能给 host_id 与 architecture"),
        ):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ConfigurationError, pattern):
                accept.candidate_external_gate_plan_entries(host_id=HOST, architecture=ARCHITECTURE, **options)
        with self.assertRaisesRegex(ConfigurationError, "不是候选外部门禁"):
            accept.candidate_gate_requirement_sha256("version-leak")
        # 版本门槛合同：codex_upgrade_gate_receipt 以 0.154.0 为完整 VC 合同门槛，Claude 的版本号恒走完整合同。
        for version in ("2.0.0", "2.1.226", "2.1.285", "3.0.0"):
            self.assertTrue(gate_receipt._is_complete_vc_target(version), version)

    def test_gate_result_inputs_dispositions_and_output_digest(self) -> None:
        plan = accept.candidate_external_gate_plan_entries(host_id=HOST, architecture=ARCHITECTURE)
        gates = []
        for gate_id in sorted(gate_receipt.CANDIDATE_COMMANDS):
            working_directory, command = gate_receipt.CANDIDATE_COMMANDS[gate_id]
            gates.append(
                {
                    "gate_id": gate_id, "command": list(command), "working_directory": working_directory, "host": HOST,
                    "architecture": ARCHITECTURE, "started_at_utc": "2026-08-23T00:00:00Z", "completed_at_utc": "2026-08-23T00:00:30Z",
                    "exit_code": 0, "status": "passed", "passed_count": 1, "failed_count": 0, "skipped_count": 0,
                    "stdout_sha256": "6" * 64, "stderr_sha256": "7" * 64, "evidence": [{"path": "e.json", "sha256": "5" * 64, "bytes": 2}],
                    "disposition": "executed", "carried_from_attempt": None,
                }
            )
        inputs = accept.gate_result_inputs({"effective_gates": gates}, plan)
        self.assertEqual([item["result"] for item in inputs], ["pass"] * 3)
        # 处置标记不进输出摘要：携带的同一次执行与原执行摘要相同。
        carried = dict(gates[0], disposition="carried", carried_from_attempt="gate-a")
        self.assertEqual(accept.gate_output_sha256(carried), accept.gate_output_sha256(gates[0]))
        self.assertNotEqual(accept.gate_output_sha256(dict(gates[0], stdout_sha256="9" * 64)), accept.gate_output_sha256(gates[0]))
        with self.assertRaisesRegex(ConfigurationError, "处置却是 carried"):
            accept.gate_result_inputs({"effective_gates": [carried, *gates[1:]]}, plan)
        with self.assertRaisesRegex(ConfigurationError, "有效门禁集合与执行计划不一致"):
            accept.gate_result_inputs({"effective_gates": gates[1:]}, plan)
        tampered = [dict(item, requirement_sha256="0" * 64) if item["gate_id"] == "target-platform" else item for item in plan]
        with self.assertRaisesRegex(ConfigurationError, "需求摘要不是候选外部门禁的冻结需求"):
            accept.gate_result_inputs({"effective_gates": gates}, tampered)


if __name__ == "__main__":
    unittest.main()
