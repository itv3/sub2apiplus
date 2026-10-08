"""Claude VC-5 的 compare／accept 读侧根（Claude 升级控制面设计 B4-06b）。

真实链部分沿用 B3-01 的真实工作树批次夹具（VC-3／VC-4 真实批次到 Store ``candidate_sealed``），之后 compare、assert-rules 与
accept 都以 ``compile-and-run-vc-batch`` 的 VC-5 批次在父监督器的动作子进程里执行；Store 的 VC-5 前缀由 B4-02 的真实写入层
写入，验收链由 B4-06a 的写入层写入。输入源层的接缝见 ``claude_vc5_chain_fixture`` 模块说明（逐条登记，B5b-02 接手时换成
真实来源）。覆盖：

* 通过路径（用例乙）：画像接缝产出与真实目标规则全集同为 39 条的画像 → compare 批次写比较收据、官方评估用 manifest 与
  builder 配置 → assert-rules 批次真实 builder 39 条全部通过 → accept 批次写 Store 验收链（PAIR 39 条）到用途对应的终点
  （批次夹具是 validation_only，Store 停在 ``validation_only``）、``production_state=not_activated``，写 accept 阶段结果、
  验收证据封印与 VC-5 checkpoint；PAIR 每个字段的来源写进 accept 结果；同一 accept 重跑逐字复用、Store 不增一条；
* 失败路径（用例甲）：同一套接缝，候选侧 anthropic-version 不符 → assert-rules 批次失败、对账 recoverable → accept 拒绝且
  Store 零写入；
* 外部门禁失败：accept 只把这一轮门禁定稿进 Store（``record_failed_gate_round``），返回 ``gate_failed``、不写 accept 阶段结果；
* 零接缝（画像与官方来源都不打桩）：compare 冻结真实的 39 条画像，在官方评估来源处失败关闭，不写比较收据。

单元部分：PAIR 规则行推导的逐字段合同与每个「找不到来源即失败关闭」分支、第三方 migrated_strict 报待设计；官方评估来源的
两种模式与失败关闭；网络面与覆盖；比较收据读侧的篡改检测。
"""

from __future__ import annotations

import copy
import json
import os
import site
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_acceptance_contract as acceptance_contract
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_acceptance as acceptance
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import claude_upgrade_evidence as evidence
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator_fixture
from tools.official_client_capture.tests import claude_vc5_chain_fixture as chain
from tools.official_client_capture.tests import test_claude_upgrade_batch as batch_tests

CANDIDATE = "claude-cand-r1"
ATTEMPT_ID = "attempt-0001"
HOST = "arm64-runner"
RUN_NONCE = "nonce-vc5-0001"
BUILDER = Path(compare.__file__).with_name("claude_build_rule_assertion_results.py")
run_main = batch_tests.run_main


def _read(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@unittest.skipUnless(site.ENABLE_USER_SITE, "VC-5 动作子进程里的输入源接缝经 usercustomize 注入，要求启用用户 site")
class CompareAcceptChainTests(batch_tests._RealTreeBatchCase):
    """真实批次链上的 compare → assert-rules → accept（见模块说明）。"""

    # -- 世界 ------------------------------------------------------------------------------------------------

    def assert_recorded_client_requests(self, world: dict[str, Any]) -> None:
        """两条模拟 Mac 请求须精确进入原 Campaign 总账；上游实时零请求由离线来源与外壳网络隔离证明。"""
        from tools.official_client_capture import claude_client_receipt_contract as contract
        from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
        from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture

        root = world["materialized"]["attempt_root"]
        self.assertEqual(project_ledger.find_project_ledger(world["campaign"], client="claude"), self.project_root)
        keys: set[str] = set()
        with client_fixture.source_scope():
            for client_id, path in world["materialized"]["clients"]["client_bindings"].items():
                verified = contract.verify_client_receipt(root, client_id, path)
                keys.update(item["identity_key"] for item in verified["requests"])
        head = project_ledger.replay_head(self.project_root)
        self.assertEqual(len(keys), 2)
        self.assertEqual(set(head["accounted_identity_index"]), keys)
        self.assertEqual((head["precise_total"], head["estimated_total"]), (2, 0))

    def vc5_world(self, *, anthropic_version: str = "2023-06-01", profile_seam: bool = True) -> dict[str, Any]:
        from tools.official_client_capture.tests.project_ledger_fixture import FIXTURE_ONLY_ENV

        # 本方法也供 VC-6 与连续链驱动借用；在首个 Campaign 建立前选择 staging，保留唯一真实项目总账。
        if "staging" not in self.root.parts:
            self.root = self.root / "staging"
            self.data = self.root / "data"
            (self.data / "evidence" / "campaigns").mkdir(parents=True, mode=0o700)
        environment = mock.patch.dict(os.environ, {FIXTURE_ONLY_ENV: "1"})
        environment.start()
        self.addCleanup(environment.stop)
        campaign = self.drive_vc3_vc4_to_candidate_sealed()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        data_root = compare.data_root(campaign)
        if profile_seam:
            # 接缝一：画像内容（冻结在测试进程里完成，之后 compare 子进程只读冻结结果）。
            with chain.profile_seam():
                from tools.official_client_capture import claude_upgrade_assertion_profile

                claude_upgrade_assertion_profile.freeze_approved_assertion_profile(campaign, manifest)
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, manifest)
        build, _binding = compare.load_candidate_build_receipt(campaign, manifest, CANDIDATE, 1)
        # 接缝三：候选证据落到候选 attempt 坐标，Store VC-5 前缀走 B4-02 真实写入层，候选阶段结果按合同写满。
        sides = self.root / "eval-sides"
        candidate_side = evaluator_fixture.write_side(sides / "candidate", capture_id=ATTEMPT_ID, anthropic_version=anthropic_version)
        materialized = chain.materialize_candidate(
            campaign=campaign, data_root=data_root, manifest=manifest, candidate_id=CANDIDATE, attempt_id=ATTEMPT_ID,
            side=candidate_side, build=build, run_nonce=RUN_NONCE,
        )
        approval = [fact for fact in store.list_facts(str(manifest["campaign_id"])) if fact["fact_kind"] == "profile_approved"][-1]
        plan = store.load_object(approval["payload"]["scenario_plan_ref"])["payload"]
        frozen = [fact for fact in store.list_facts(str(manifest["campaign_id"])) if fact["fact_kind"] == "candidate_frozen"][-1]
        architecture = str(frozen["payload"]["target_architecture"])
        snapshot = candidate_capture.runtime_catalog_snapshot(
            candidate_capture.observe_selectors(campaign, manifest, required=True), store_campaign["persona"], campaign_dir=campaign
        )
        request = chain.seal_request(
            campaign=campaign, data_root=data_root, candidate_id=CANDIDATE, attempt_id=ATTEMPT_ID,
            scenario_ids=[str(item["id"]) for item in plan["scenarios"]], materialized=materialized, selector_snapshot=snapshot,
            architecture=architecture, host_id=HOST, run_nonce=RUN_NONCE, issued_at=claude_upgrade._utc_now(),
        )
        recorded = candidate_seal.record_candidate_seal_chain(campaign, manifest, request)
        chain.candidate_stage_result(
            campaign=campaign, manifest=manifest, candidate_id=CANDIDATE, materialized=materialized, recorded=recorded,
            store_checkpoint="candidate_sealed",
        )
        # 接缝二：官方评估来源（评估器夹具官方侧）；接缝四：候选 attempt 上下文。
        official_side = evaluator_fixture.write_side(sides / "official", capture_id="eval-official")
        official = claude_upgrade._load_stage_result(campaign, "capture-official")
        official_source = chain._write_json(self.root / "seams" / "official-source.json", chain.official_source(official_side, official["store"]["evidence_package_ref"]))
        config = {"attempt_root": str(materialized["attempt_root"]), "attempt": str(materialized["attempt_path"])}
        if profile_seam:
            config["official_source"] = str(official_source)
        config_path = chain._write_json(self.root / "seams" / "config.json", config)
        gate_root = self.root / "gate-evidence"
        return {
            "campaign": campaign,
            "manifest": manifest,
            "store": store,
            "store_campaign": store_campaign,
            "build": build,
            "recorded": recorded,
            "materialized": materialized,
            "candidate_payload": dict(frozen["payload"]),
            "architecture": architecture,
            "config_path": config_path,
            "gate_root": gate_root,
            "approval": dict(approval["payload"]),
        }

    def dispatch_vc5(self, world: dict[str, Any], sequence: int, action: dict[str, Any]) -> tuple[int, dict | None, str]:
        """派发一个 VC-5 批次；动作子进程里安装输入源接缝。"""

        campaign = world["campaign"]
        plan = chain._write_json(self.root / "action-plans" / f"vc-5-{sequence:04d}.json", batch_tests.batch.action_plan([action], phase="VC-5"))
        environment = chain.subprocess_environment(self.root / "inject", world["config_path"], [str(action["action_id"])])
        with mock.patch.dict(os.environ, environment):
            return run_main(
                [
                    "compile-and-run-vc-batch", "--campaign-dir", campaign, "--phase", "VC-5", "--sequence", sequence,
                    "--predecessor-checkpoint", claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-4", revision=1),
                    "--action-plan", plan, "--state-dir", self.state_dir,
                ]
            )

    def compare_action(self, campaign: Path) -> dict[str, Any]:
        return batch_tests.batch.managed_action("compare", "VC-5:compare", ["compare", "--campaign-dir", campaign, "--candidate-id", CANDIDATE], item_ids=["compare"])

    def assert_action(self, campaign: Path, comparison: dict[str, Any]) -> dict[str, Any]:
        builder = comparison["builder"]
        return {
            "action_id": "assert-rules",
            "operation": "VC-5:assert-rules",
            "timeout_seconds": 900.0,
            "command": [
                sys.executable, str(BUILDER), "--config", str(campaign / builder["config"]["path"]), "--output", str(campaign / builder["output"]),
                "--results-dir", str(campaign / builder["results_dir"]), "--evaluation-baseline", str(builder["evaluation_baseline"]),
            ],
            "item_ids": ["assert-rules"],
        }

    def accept_action(self, campaign: Path, receipt: str, gate_root: Path) -> dict[str, Any]:
        return batch_tests.batch.managed_action(
            "acceptance",
            "VC-5:acceptance",
            ["accept", "--campaign-dir", campaign, "--candidate-id", CANDIDATE, "--external-gate-root", gate_root, "--external-gate-receipt", receipt],
            item_ids=["acceptance"],
        )

    def gate_receipt(self, world: dict[str, Any], name: str, *, failed_gate: str | None = None) -> str:
        # 父进程读取候选身份时也会重验客户端来源，须在既有测试接缝内完成；
        # 来源适配器只在此作用域登记；环境收据按新世界配置选择真实合同或默认旧替身。
        with chain.seams(world["config_path"]):
            identity = compare.candidate_gate_identity(world["campaign"], world["manifest"], CANDIDATE, world["build"])
            subject = chain.expected_subject(world["manifest"], world["candidate_payload"], identity)
            return chain.gate_receipt_file(world["gate_root"], name=name, subject=subject, host=HOST, architecture=world["architecture"],
                failed_gate=failed_gate, environment_mode=chain.gate_environment_mode(_read(world["config_path"])))

    def footprint(self, world: dict[str, Any]) -> tuple[list[str], int]:
        facts = [fact["fact_kind"] for fact in world["store"].list_facts(str(world["manifest"]["campaign_id"]))]
        return facts, len(world["store"].list_receipt_refs("validation_gate"))

    def run_compare_and_assert(self, world: dict[str, Any]) -> dict[str, Any]:
        campaign = world["campaign"]
        result = self.assert_batch_passed(self.dispatch_vc5(world, 9, self.compare_action(campaign)), 9, "VC-5")
        self.assertEqual(result["campaign_run"]["reason"], "queue-complete")
        comparison = claude_upgrade._load_stage_result(campaign, "compare", CANDIDATE)
        return comparison

    # -- 用例 ------------------------------------------------------------------------------------------------

    def test_pass_chain_reaches_ready_and_writes_vc5_checkpoint(self) -> None:
        world = self.vc5_world()
        campaign, manifest = world["campaign"], world["manifest"]
        comparison = self.run_compare_and_assert(world)
        rules = list(claude_upgrade._approved_rules(campaign, manifest, require_approved=True))
        self.assertEqual(len(rules), len(world["approval"]["target_spec_ids"]))
        self.assertEqual(sorted(rules), sorted(world["approval"]["target_spec_ids"]))
        self.assertEqual((comparison["rules"], comparison["coverage"]["complete"], comparison["evaluation_baseline"]), (rules, True, 0))
        self.assertEqual(comparison["identity"]["profile_id"], f"claude-code-{manifest['target_version']}-official")
        self.assertEqual(comparison["identity"]["candidate_package_digest"], world["recorded"]["candidate_evidence_package_ref"]["sha256"])
        config = _read(campaign / comparison["builder"]["config"]["path"])
        self.assertEqual((config["comparison_package_digest"], config["rules"]), (comparison["package_digest"], rules))
        # assert-rules：真实 Claude builder 按 compare 写的配置跑全部规则，全部通过。
        self.assert_batch_passed(self.dispatch_vc5(world, 10, self.assert_action(campaign, comparison)), 10, "VC-5")
        results = _read(campaign / comparison["builder"]["output"])
        self.assertEqual(sorted(row["rule"] for row in results["rules"]), sorted(rules))
        # accept：外部门禁通过 → Store 验收链到 ready，accept 阶段结果、验收证据封印与 VC-5 checkpoint。
        receipt = self.gate_receipt(world, "candidate-receipt.json")
        outcome = self.dispatch_vc5(world, 11, self.accept_action(campaign, receipt, world["gate_root"]))
        self.assert_batch_passed(outcome, 11, "VC-5")
        status = candidate_seal.claude_upgrade_seal.store_cli(["status", "--store", world["store"].root, "--campaign", str(manifest["campaign_id"])])
        # Store 终点按用途：validation_only 停在 ``validation_only``，production_replacement 停在 ``ready``（设计 B4-06 验收）。
        expected_checkpoint = "validation_only" if manifest["campaign_purpose"] == "validation_only" else "ready"
        self.assertEqual((status["checkpoint"], status["production_state"]), (expected_checkpoint, "not_activated"))
        accepted = claude_upgrade._load_stage_result(campaign, "accept", CANDIDATE)
        self.assertEqual((accepted["accepted"], accepted["production_state"], accepted["campaign_purpose"]), (True, "not_activated", manifest["campaign_purpose"]))
        self.assertEqual(sorted(accepted["pair_rows"]["rows"]), sorted(rules))
        self.assertEqual(sorted(accepted["store"]["pair_refs"]), sorted(rules))
        provenance = accepted["pair_rows"]["provenance"][rules[0]]
        self.assertEqual(provenance["official_result.ingress_id"]["basis"], "default_messages_route")
        self.assertIn("evaluation-run.json#rules[", provenance["condition_sha256"])
        facts, gates = self.footprint(world)
        self.assertEqual((facts.count("pair_recorded"), facts.count("acceptance_recorded"), facts.count("validation_completed"), gates), (len(rules), 1, 1, 3))
        checkpoint_path = claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1)
        self.assertTrue(checkpoint_path.is_file())
        self.assertEqual(_read(checkpoint_path)["stage_receipt"]["path"], f"acceptance/{CANDIDATE}/result.json")
        seal = _read(campaign / accepted["evidence_seal"]["path"])
        self.assertEqual(seal["store"]["acceptance_ref"], accepted["store"]["acceptance_ref"])
        # 同一 accept 重跑（测试进程里、同一组接缝）：逐字复用，Store 不增一条、checkpoint 不变。
        before = (self.footprint(world), checkpoint_path.read_bytes())
        with chain.seams(world["config_path"]):
            again = claude_upgrade.accept_campaign(campaign, CANDIDATE, None, world["gate_root"], Path(receipt))
        self.assertEqual(again["accepted_at_utc"], accepted["accepted_at_utc"])
        self.assertEqual((self.footprint(world), checkpoint_path.read_bytes()), before)
        self.assert_recorded_client_requests(world)

    def test_failing_candidate_assert_batch_fails_and_accept_refuses_without_writes(self) -> None:
        world = self.vc5_world(anthropic_version="2023-01-01")
        campaign = world["campaign"]
        comparison = self.run_compare_and_assert(world)
        code, result, stderr = self.dispatch_vc5(world, 10, self.assert_action(campaign, comparison))
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["campaign_run"]["reason"]), (1, "action-failed:assert-rules"), stderr)
        self.assertFalse((campaign / comparison["builder"]["output"]).exists())
        code, reconciled, stderr = run_main(["reconcile-supervisor-run", "--campaign-dir", campaign, "--run-dir", Path(result["campaign_run"]["run_dir"])])
        self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
        receipt = self.gate_receipt(world, "candidate-receipt.json")
        before = self.footprint(world)
        with chain.seams(world["config_path"]), self.assertRaisesRegex(ConfigurationError, "逐规则断言结果尚未生成"):
            claude_upgrade.accept_campaign(campaign, CANDIDATE, None, world["gate_root"], Path(receipt))
        self.assertEqual(self.footprint(world), before)
        self.assertFalse((campaign / "acceptance").exists())
        self.assert_recorded_client_requests(world)

    def test_failed_external_gate_records_round_without_accept_result(self) -> None:
        world = self.vc5_world()
        campaign = world["campaign"]
        comparison = self.run_compare_and_assert(world)
        self.assert_batch_passed(self.dispatch_vc5(world, 10, self.assert_action(campaign, comparison)), 10, "VC-5")
        receipt = self.gate_receipt(world, "candidate-receipt-failed.json", failed_gate="check-egress-spec")
        with chain.seams(world["config_path"]):
            first = claude_upgrade.accept_campaign(campaign, CANDIDATE, None, world["gate_root"], Path(receipt))
            facts, gates = self.footprint(world)
            again = claude_upgrade.accept_campaign(campaign, CANDIDATE, None, world["gate_root"], Path(receipt))
        self.assertEqual((first["status"], first["accepted"]), ("gate_failed", False))
        self.assertEqual(first["failed_round"]["failed_gate_ids"], ["check-egress-spec"])
        self.assertIn("--revalidate", first["next_command"])
        self.assertTrue(all(item["reused"] for item in again["failed_round"]["gate_receipts"]))
        self.assertEqual(gates, 3)
        self.assertNotIn("pair_recorded", facts)
        self.assertEqual(self.footprint(world), (facts, gates))
        self.assertFalse((campaign / "acceptance").exists())
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(campaign, "VC-5", revision=1).exists())

    def test_zero_seam_compare_freezes_real_profile_and_fails_closed_at_official_source(self) -> None:
        world = self.vc5_world(profile_seam=False)
        campaign, manifest = world["campaign"], world["manifest"]
        # 批次夹具的官方阶段结果是只读复用形态，复用来源只记了简化的 ``{mode, campaign_id}``（真实 seal 写的是
        # ``predecessor``、``import_ref`` 等），前序 Campaign 也没有官方 seal：官方评估来源在这里失败关闭。
        with chain.seams(world["config_path"]), self.assertRaisesRegex(ConfigurationError, "官方评估来源无法确定：只读复用的官方阶段结果没有登记前序 Campaign ID"):
            claude_upgrade.compare_campaign(campaign, CANDIDATE)
        # 画像按真实输入冻结（与目标规则全集一致），比较收据、官方评估用 manifest 与 builder 配置一个都没写。
        from tools.official_client_capture import claude_upgrade_assertion_profile

        frozen = claude_upgrade_assertion_profile.load_approved_assertion_profile(campaign, manifest)
        profile = acceptance_contract.load_profile(campaign / frozen["path"])
        self.assertEqual(sorted(rule["rule_id"] for rule in profile["rules"]), sorted(world["approval"]["target_spec_ids"]))
        self.assertFalse((campaign / "comparisons").exists())
        self.assert_recorded_client_requests(world)


# ---------------------------------------------------------------------------
# PAIR 规则行推导的合同（不建批次世界）
# ---------------------------------------------------------------------------


def _ref(kind: str, seed: str) -> dict[str, str]:
    return {"object_kind": kind, "sha256": seed * 64}


def _fact_ref(sequence: int) -> dict[str, Any]:
    return {"campaign_id": "campaign-x", "dimension": "validation", "sequence": sequence, "sha256": f"{sequence:x}" * 64}


def pair_inputs() -> dict[str, Any]:
    """两条规则：SPEC-A-001 受影响、dual_wire、请求路径命中 count_tokens 入口；SPEC-B-002 继承、candidate_profile、走默认入口。"""

    return {
        "rules": ["SPEC-A-001", "SPEC-B-002"],
        "modes": {"SPEC-A-001": "dual_wire", "SPEC-B-002": "candidate_profile"},
        "evaluation": {
            "SPEC-A-001": {
                "dependency_projection_sha256": "1" * 64,
                "candidate_projection_sha256": "2" * 64,
                "official_projection_sha256": "3" * 64,
                "sources": {"dependency_projection_sha256": "run#A.dep", "candidate_projection_sha256": "cp#A", "official_projection_sha256": "op#A"},
            },
            "SPEC-B-002": {
                "dependency_projection_sha256": "4" * 64,
                "candidate_projection_sha256": "5" * 64,
                "official_projection_sha256": None,
                "sources": {"dependency_projection_sha256": "run#B.dep", "candidate_projection_sha256": "cp#B", "official_projection_sha256": None},
            },
        },
        "official_authority": {"assertion_profile_sha256": "6" * 64, "classification_package_digest": "7" * 64, "review_sha256": "8" * 64},
        "ledger_assertions": [
            {"assertion_id": "SPEC-A-001-02", "owner_kind": "required_rule", "owner_id": "SPEC-A-001"},
            {"assertion_id": "SPEC-A-001-01", "owner_kind": "required_rule", "owner_id": "SPEC-A-001"},
            {"assertion_id": "SPEC-B-002", "owner_kind": "required_rule", "owner_id": "SPEC-B-002"},
            {"assertion_id": "SPEC-LOCAL-1", "owner_kind": "scenario_only", "owner_id": "SCENARIO-X"},
        ],
        "affected_rules": ["SPEC-A-001", "SPEC-OLD-009"],
        "scenario_plan": [{"id": "scenario.count", "spec_ids": ["SPEC-A-001"]}, {"id": "scenario.main", "spec_ids": ["SPEC-A-001", "SPEC-B-002"]}],
        "scenario_approval_refs": {"scenario.count": _fact_ref(11), "scenario.main": _fact_ref(12)},
        "ingress_entries": [
            {"logical_ingress_id": "official-count-tokens-oauth", "ingress_kind": "official", "physical_alias_ids": ["alias-count"], "protocol_class": "anthropic-messages"},
            {"logical_ingress_id": "official-messages-oauth", "ingress_kind": "official", "physical_alias_ids": ["alias-messages"], "protocol_class": "anthropic-messages"},
        ],
        "ingress_aliases": [
            {"alias_id": "alias-count", "physical_route": "/v1/messages/count_tokens"},
            {"alias_id": "alias-messages", "physical_route": "/v1/messages"},
        ],
        "ingress_target_dispositions": [
            {"logical_ingress_id": "official-count-tokens-oauth", "target_disposition": "migrated_strict"},
            {"logical_ingress_id": "official-messages-oauth", "target_disposition": "migrated_strict"},
        ],
        "rule_probes": {"SPEC-A-001": ["count-probe"], "SPEC-B-002": ["tls"]},
        "probe_request_paths": {"count-probe": ["/v1/messages/count_tokens"], "tls": []},
        "official_package_ref": _ref("evidence_package", "a"),
        "candidate_package_ref": _ref("candidate_evidence_package", "b"),
        "gate_plan_gates": [{"gate_id": "affected-spec-a-001", "test_id": "TestSpecA001", "working_directory": "backend"}],
        "source_tree_sha256": "c" * 64,
        "strict_endpoints": [{"binding_id": "binding-count", "spec_ids": ["SPEC-A-001"]}],
        "baseline_version": "2.1.226",
        "sources": {
            key: f"src:{key}"
            for key in (
                "assertions", "official_authority", "official_stage", "candidate_stage", "comparison", "profile", "build_receipt",
                "ledger", "migration", "scenario_plan", "ingress", "gate_plan", "release_profile",
            )
        },
    }


class DerivePairRowsTests(unittest.TestCase):
    def test_every_field_and_its_source(self) -> None:
        rows, provenance = acceptance.derive_pair_rows(pair_inputs())
        first, second = rows
        self.assertEqual(set(first), set(candidate_seal.claude_upgrade_accept.PAIR_ROW_FIELDS))
        # 1. condition_sha256 取 evaluation-run 的依赖摘要。
        self.assertEqual((first["condition_sha256"], second["condition_sha256"]), ("1" * 64, "4" * 64))
        # 2. 场景批准：ScenarioPlan 含该规则的场景。
        self.assertEqual(len(first["scenario_approval_refs"]), 2)
        self.assertEqual(second["scenario_approval_refs"], [_fact_ref(12)])
        # 3. 官方入口：请求路径恰好命中一个入口；没有请求路径时取 /v1/messages 入口。官方 final wire：dual_wire 取官方投影，
        #    candidate_profile 取官方权威画像摘要。
        self.assertEqual(first["official_result"]["ingress_id"], "official-count-tokens-oauth")
        self.assertEqual(provenance["SPEC-A-001"]["official_result.ingress_id"]["basis"], "request_path")
        self.assertEqual(second["official_result"]["ingress_id"], "official-messages-oauth")
        self.assertEqual(provenance["SPEC-B-002"]["official_result.ingress_id"]["basis"], "default_messages_route")
        self.assertEqual((first["official_result"]["final_wire_sha256"], second["official_result"]["final_wire_sha256"]), ("3" * 64, "6" * 64))
        self.assertEqual(first["official_result"]["evidence_refs"], [_ref("evidence_package", "a")])
        self.assertEqual(first["official_result"]["translation"], "lossless")
        # 4. 候选结果。
        self.assertEqual(first["candidate_result"], {"result": "pass", "final_wire_sha256": "2" * 64, "evidence_refs": [_ref("candidate_evidence_package", "b")]})
        # 5. 第三方：没有 migrated_strict 第三方入口时为空。
        self.assertEqual(first["third_party_results"], [])
        # 6. 动态字段检查。
        self.assertEqual(first["dynamic_field_checks"], [{"id": "claude-rule-assertions", "dimensions": ["format", "lifecycle", "relation", "source"], "result": "pass"}])
        # 7. 原子断言（只取 required_rule，排序）与来源。
        self.assertEqual([item["assertion_id"] for item in first["assertion_results"]], ["SPEC-A-001-01", "SPEC-A-001-02"])
        self.assertEqual((first["result_source"], second["result_source"]), ("execute", "reuse"))
        self.assertEqual({item["source"] for item in second["assertion_results"]}, {"reuse"})
        self.assertEqual((first["comparison_mode"], second["comparison_mode"]), ("dual_wire", "candidate_profile"))
        # 8. 锚点。
        self.assertEqual(first["test_anchors"], ["backend:TestSpecA001"])
        self.assertEqual(second["test_anchors"], ["inherited:2.1.226:SPEC-B-002"])
        self.assertEqual(first["implementation_anchors"], [f"candidate-source-tree:{'c' * 64}", "release-profile-strict-endpoint:binding-count"])
        self.assertEqual(second["implementation_anchors"], [f"candidate-source-tree:{'c' * 64}"])
        # 每个字段都写了来源。
        self.assertEqual(
            set(provenance["SPEC-A-001"]),
            {
                "condition_sha256", "scenario_approval_refs", "official_result.ingress_id", "official_result.final_wire_sha256",
                "official_result.evidence_refs", "candidate_result.final_wire_sha256", "candidate_result.evidence_refs",
                "third_party_results", "dynamic_field_checks", "assertion_results", "result_source", "comparison_mode",
                "implementation_anchors", "test_anchors",
            },
        )
        self.assertEqual(provenance["SPEC-A-001"]["condition_sha256"], "run#A.dep")
        self.assertEqual(provenance["SPEC-B-002"]["official_result.final_wire_sha256"], "src:official_authority#assertion_profile_sha256")

    def test_ambiguous_paths_fall_back_to_messages_route(self) -> None:
        inputs = pair_inputs()
        inputs["probe_request_paths"]["count-probe"] = ["/v1/messages", "/v1/messages/count_tokens"]
        rows, provenance = acceptance.derive_pair_rows(inputs)
        self.assertEqual(rows[0]["official_result"]["ingress_id"], "official-messages-oauth")
        self.assertEqual(provenance["SPEC-A-001"]["official_result.ingress_id"]["matched"], ["official-count-tokens-oauth", "official-messages-oauth"])

    def test_each_missing_source_fails_closed(self) -> None:
        def mutate(change: Any) -> dict[str, Any]:
            inputs = pair_inputs()
            change(inputs)
            return inputs

        cases = [
            (lambda value: value.pop("sources"), "字段不闭合"),
            (lambda value: value["modes"].pop("SPEC-A-001"), "没有验收模式或评估运行行"),
            (lambda value: value["evaluation"].pop("SPEC-B-002"), "没有验收模式或评估运行行"),
            (lambda value: value.__setitem__("scenario_plan", [{"id": "scenario.count", "spec_ids": ["SPEC-A-001"]}]), "不在 ScenarioPlan"),
            (lambda value: value["scenario_approval_refs"].pop("scenario.main"), "没有候选场景批准"),
            (lambda value: value.__setitem__("ledger_assertions", [item for item in value["ledger_assertions"] if item["owner_id"] != "SPEC-B-002"]), "没有规则 SPEC-B-002 的 required_rule"),
            (lambda value: value.__setitem__("ingress_target_dispositions", [dict(item, target_disposition="denied") for item in value["ingress_target_dispositions"]]), "没有 migrated_strict 官方入口"),
            (lambda value: value.__setitem__("ingress_aliases", value["ingress_aliases"][:1]), "别名在入口观测里不存在"),
            (lambda value: value.__setitem__("ingress_aliases", [dict(item, physical_route="/v2/other") for item in value["ingress_aliases"]]), "官方入口无法确定"),
            (lambda value: value["evaluation"]["SPEC-A-001"].__setitem__("official_projection_sha256", None), "官方 final wire 摘要没有来源"),
            (lambda value: value["official_authority"].pop("assertion_profile_sha256"), "官方 final wire 摘要没有来源"),
            (lambda value: value.__setitem__("gate_plan_gates", []), "门禁计划里没有门禁 affected-spec-a-001"),
        ]
        for change, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ConfigurationError, pattern):
                acceptance.derive_pair_rows(mutate(change))

    def test_migrated_third_party_ingress_is_pending_design(self) -> None:
        inputs = pair_inputs()
        inputs["ingress_entries"].append(
            {"logical_ingress_id": "third-party-messages-oauth", "ingress_kind": "third_party", "physical_alias_ids": [], "protocol_class": "anthropic-messages"}
        )
        inputs["ingress_target_dispositions"].append({"logical_ingress_id": "third-party-messages-oauth", "target_disposition": "migrated_strict"})
        with self.assertRaises(claude_upgrade.ClaudeOrchestratorPending) as caught:
            acceptance.derive_pair_rows(inputs)
        self.assertIn("third_party_results", str(caught.exception))
        # 第三方入口没有迁入 strict 时不受影响。
        inputs["ingress_target_dispositions"][-1]["target_disposition"] = "denied"
        rows, _provenance = acceptance.derive_pair_rows(inputs)
        self.assertEqual(rows[0]["third_party_results"], [])

    def test_rows_written_per_baseline(self) -> None:
        rows, _provenance = acceptance.derive_pair_rows(pair_inputs())
        evaluation = {"SPEC-A-001": {"reused": False}, "SPEC-B-002": {"reused": True}}
        select = acceptance.select_pair_rows
        # b0：全部规则。
        self.assertEqual([row["spec_id"] for row in select(rows, evaluation, baseline=0, earlier_effective=["SPEC-B-002"])], ["SPEC-A-001", "SPEC-B-002"])
        # b≥1：复用的规则若更早基线已有有效 PAIR 就不再写；没有（例如 b0 断言失败从没写过）则照写。
        self.assertEqual([row["spec_id"] for row in select(rows, evaluation, baseline=1, earlier_effective=["SPEC-B-002"])], ["SPEC-A-001"])
        self.assertEqual([row["spec_id"] for row in select(rows, evaluation, baseline=1, earlier_effective=[])], ["SPEC-A-001", "SPEC-B-002"])


# ---------------------------------------------------------------------------
# compare 的纯函数与官方评估来源（不建批次世界）
# ---------------------------------------------------------------------------


class CompareHelperTests(unittest.TestCase):
    def test_surfaces_are_diagnostic(self) -> None:
        left = [{"surface": "h POST /a HTTP/1.1", "count": 2}, {"surface": "h POST /b HTTP/1.1", "count": 1}]
        right = [{"surface": "h POST /a HTTP/1.1", "count": 1}, {"surface": "h POST /c HTTP/1.1", "count": 1}]
        result = compare.compare_surfaces(left, right)
        self.assertEqual(
            result,
            {
                "equal": False,
                "official_only": ["h POST /b HTTP/1.1"],
                "candidate_only": ["h POST /c HTTP/1.1"],
                "count_differences": [{"surface": "h POST /a HTTP/1.1", "official": 2, "candidate": 1}],
            },
        )
        self.assertTrue(compare.compare_surfaces(left, left)["equal"])

    def test_rule_coverage_by_mode(self) -> None:
        profile = {"rules": [{"rule_id": "SPEC-A-001", "scenario_ids": ["s1"]}, {"rule_id": "SPEC-B-002", "scenario_ids": ["s2"]}]}
        manifests = {
            "official": {"artifacts": [{"scenario_ids": ["s2"]}]},
            "candidate": {"artifacts": [{"scenario_ids": ["s1"]}, {"scenario_ids": ["s2"]}]},
        }
        complete = compare.rule_coverage(profile, {"SPEC-A-001": "candidate_profile", "SPEC-B-002": "dual_wire"}, manifests)
        self.assertTrue(complete["complete"])
        missing = compare.rule_coverage(profile, {"SPEC-A-001": "dual_wire", "SPEC-B-002": "dual_wire"}, manifests)
        self.assertEqual([row["missing_sides"] for row in missing["rules"]], [["official"], []])
        self.assertFalse(missing["complete"])

    def test_probe_request_paths_strip_query(self) -> None:
        index = {"jobs": [{"probes": [{"probe_id": "p", "connections": [{"requests": ["POST /v1/messages?beta=true HTTP/1.1", "garbage"]}]}]}]}
        self.assertEqual(compare._index_probe_request_paths(index), {"p": ["/v1/messages"]})


class OfficialEvaluationSourceTests(unittest.TestCase):
    """官方评估来源的两种模式与失败关闭：Store 读取换成内存替身，证据文件按真实证据形态写在临时宿主数据根。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = self.root / "data"
        self.campaign = self.data / "evidence" / "campaigns" / "claude-campaign"
        self.campaign.mkdir(parents=True)
        (self.campaign / "campaign.json").write_text(json.dumps({"campaign_id": "claude-campaign"}), encoding="utf-8")
        side = evaluator_fixture.write_side(self.root / "side", capture_id="attempt-official")
        evidence_root = self.campaign / "official" / "attempts" / "attempt-official" / "evidence"
        import shutil

        shutil.copytree(side.evidence_root, evidence_root)
        prefix = evidence_root.relative_to(self.data).as_posix()
        capture = copy.deepcopy(side.capture_manifest)
        capture["attempt"] = {"attempt_id": "attempt-official"}
        for artifact in capture["artifacts"]:
            artifact["path"] = f"{prefix}/{artifact['path']}"
        drafts = self.campaign / "official" / "seal" / "g1" / "drafts"
        capture_path = chain._write_json(drafts / "capture-manifest.json", capture)
        index_path = chain._write_json(
            drafts / "capture-index.json",
            {"jobs": [{"probes": [{"probe_id": "print", "connections": [{"requests": ["POST /v1/messages?beta=true HTTP/1.1"]}]}]}], "network_inventory": {"surfaces": [{"surface": "s", "count": 1}]}},
        )
        files = sorted(path for path in self.data.rglob("*") if path.is_file() and path.name != "campaign.json")
        entries = [{"path": path.relative_to(self.data).as_posix(), "sha256": chain._sha256(path), "bytes": path.stat().st_size} for path in files]
        self.inventory = chain._write_json(
            self.campaign / "official" / "seal" / "g1" / "evidence-manifest.json",
            {"schema_version": evidence.EVIDENCE_INVENTORY_SCHEMA, "entry_count": len(entries), "total_bytes": 0, "entries": entries, "digest": "0" * 64},
        )
        by_path = {item["path"]: item for item in entries}
        relay = by_path[f"{prefix}/job-main/print/relay-manifest.json"]
        self.completeness = {
            "entries": [
                {"id": "binding-0001", "facts": {"purpose": evidence.COMPLETENESS_PURPOSES["capture_manifest"], "binding": chain.binding(capture_path, self.data)}},
                {"id": "binding-0002", "facts": {"purpose": evidence.COMPLETENESS_PURPOSES["capture_index"], "binding": chain.binding(index_path, self.data)}},
            ]
        }
        self.package = {
            "schema_version": evidence.EVIDENCE_PACKAGE_V3_SCHEMA,
            "completeness_ref": _ref("operational_evidence", "c"),
            "evidence_items": [{"evidence_id": "EVIDENCE-print", "evidence_refs": [dict(relay)]}],
        }
        self.package_ref = _ref("evidence_package", "a")
        self.official = {
            "mode": "captured",
            "seal": {"evidence_manifest": chain.binding(self.inventory, self.campaign)},
            "store": {"evidence_package_ref": dict(self.package_ref)},
        }

    def source(self, official: dict[str, Any] | None = None) -> dict[str, Any]:
        store = mock.Mock()
        objects = {self.package_ref["sha256"]: self.package, "c" * 64: self.completeness}
        store.load_object.side_effect = lambda reference: {"payload": copy.deepcopy(objects[reference["sha256"]])}
        with mock.patch.object(claude_upgrade_store, "open_campaign_store", return_value=(store, {})):
            return compare.official_evaluation_source(self.campaign, {"target_version": evaluator_fixture.TARGET_VERSION}, official or self.official)

    def test_captured_mode_binds_companions_from_inventory(self) -> None:
        source = self.source()
        self.assertEqual(set(source), compare.SOURCE_FIELDS)
        kinds = [artifact["kind"] for artifact in source["evaluation_manifest"]["artifacts"]]
        self.assertIn("relay_index", kinds)
        self.assertIn("relay_response", kinds)
        self.assertEqual((source["capture_id"], source["evidence_root"], source["inventory"]["source"]), ("attempt-official", str(self.data), "official_seal"))
        self.assertEqual(source["probe_request_paths"], {"print": ["/v1/messages"]})

    def test_package_binding_must_match_inventory(self) -> None:
        self.package["evidence_items"][0]["evidence_refs"][0]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ConfigurationError, "不在深度扫描清单里或摘要不一致"):
            self.source()

    def test_missing_completeness_binding_fails_closed(self) -> None:
        self.completeness["entries"] = self.completeness["entries"][:1]
        with self.assertRaisesRegex(ConfigurationError, "没有 capture_index 绑定"):
            self.source()

    def test_predecessor_reuse_requires_readable_predecessor_seal(self) -> None:
        official = {"mode": "predecessor_reuse", "predecessor_reuse": {"predecessor": {"campaign_id": "missing-predecessor"}}, "store": {"evidence_package_ref": dict(self.package_ref)}}
        with self.assertRaisesRegex(ConfigurationError, "前序 Campaign missing-predecessor 的官方阶段结果不可读"):
            self.source(official)


# ---------------------------------------------------------------------------
# accept 的运行审计、客户端收据与比较收据读侧（不建批次世界）
# ---------------------------------------------------------------------------

BUILD = {
    "profile": {"profile_digest": "1" * 64, "wire_sha256": "2" * 64, "release_sha256": "3" * 64, "bundle_sha256": "4" * 64},
    "image": {"image_id": f"sha256:{'5' * 64}", "reference": f"registry/sub2api@sha256:{'6' * 64}"},
    "source": {"tree_sha256": "7" * 64},
    "build": {"build_id": "build-1"},
    "deployed_version": evaluator_fixture.TARGET_VERSION,
}


class RuntimeAuditAndClientReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.campaign = Path(directory.name).resolve() / "staging" / "data" / "evidence" / "campaigns" / "claude-campaign"
        self.attempt_root = self.campaign / "seam" / "attempt-1"
        self.manifest = {"campaign_id": "claude-campaign", "target_version": evaluator_fixture.TARGET_VERSION}
        fact = chain.activation_fact(BUILD, evaluator_fixture.TARGET_VERSION)
        self.fact_path = chain._write_json(self.attempt_root / "evidence" / "environment" / "claude-egress-activation-fact.json", fact)
        self.attempt = {
            "attempt_id": "attempt-1",
            "run_nonce": "nonce-1",
            "gateway_environment": {
                "activation_fact": {
                    "path": "environment/claude-egress-activation-fact.json",
                    "sha256": chain._sha256(self.fact_path),
                    "bytes": self.fact_path.stat().st_size,
                    "source_path": "/data/fact.json",
                    "schema_version": "claude-egress-activation-fact/v1",
                    "profile_mode": "validation_candidate",
                    "event_id": fact["event_id"],
                }
            },
        }

    def audit(self, attempt: dict[str, Any] | None = None, build: dict[str, Any] | None = None) -> dict[str, Any]:
        return acceptance.runtime_audit(self.campaign, self.manifest, "cand-1", self.attempt_root, attempt or self.attempt, build or BUILD)[0]

    def test_candidate_runtime_audit(self) -> None:
        audit = self.audit()
        self.assertEqual((audit["profile_mode"], audit["approval_digest"], audit["candidate_id"]), ("validation_candidate", "", "cand-1"))

    def test_runtime_audit_fails_closed(self) -> None:
        def attempt_with(**changes: Any) -> dict[str, Any]:
            attempt = copy.deepcopy(self.attempt)
            attempt["gateway_environment"]["activation_fact"].update(changes)
            return attempt

        missing = copy.deepcopy(self.attempt)
        missing["gateway_environment"] = {}
        drifted_build = copy.deepcopy(BUILD)
        drifted_build["image"]["image_id"] = f"sha256:{'9' * 64}"
        cases = [
            (missing, BUILD, "没有登记候选激活事实"),
            (attempt_with(profile_mode="active"), BUILD, "不是候选 Runtime"),
            (attempt_with(sha256="0" * 64), BUILD, "摘要与 attempt 登记不一致"),
            (attempt_with(path="../outside.json"), BUILD, "越出 attempt 证据目录"),
            (attempt_with(event_id="0" * 64), BUILD, "event_id 与 attempt 登记不一致"),
            (self.attempt, drifted_build, "image_id 与声明身份不一致"),
        ]
        for attempt, build, pattern in cases:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ConfigurationError, pattern):
                self.audit(attempt, build)

    def client_world(self, payload_changes: dict[str, Any] | None = None) -> dict[str, Any]:
        from tools.official_client_capture.tests import claude_client_receipt_fixture

        scenarios = chain._write_json(self.campaign / "inputs" / "scenarios.json", {"required_client_bindings": ["claude-desktop"], "required_boundary_checks": ["third-party-ingress-rejection"]})
        chain._write_json(self.campaign / "campaign.json", {**self.manifest, "client": "claude"})
        values = {"campaign_id": "claude-campaign", "candidate_id": "cand-1", "attempt_id": "attempt-1", "run_nonce": "nonce-1"}
        chain._write_json(self.attempt_root / "attempt.json", {**self.attempt, "candidate_id": "cand-1"})
        desktop = claude_client_receipt_fixture.write_receipt(self.attempt_root, "claude-desktop", values)
        if payload_changes:
            chain._write_json(desktop, {**json.loads(desktop.read_bytes()), **payload_changes})
        boundary = chain._write_json(self.attempt_root / "client-evidence" / "boundary.json", {"check_id": "third-party-ingress-rejection", **values, "result": "pass"})
        candidate = {
            "candidate_id": "cand-1",
            "client_evidence": {
                "client_bindings": {"claude-desktop": chain.binding(desktop, self.campaign, with_bytes=False)},
                "boundary_checks": {"third-party-ingress-rejection": chain.binding(boundary, self.campaign, with_bytes=False)},
            },
        }
        self.scenarios_path = scenarios
        return candidate

    def receipts(self, candidate: dict[str, Any]) -> dict[str, Any]:
        from tools.official_client_capture.tests import claude_client_receipt_fixture

        with claude_client_receipt_fixture.source_scope(), mock.patch.object(acceptance.claude_upgrade_seal, "_frozen_input", return_value=self.scenarios_path):
            return acceptance.client_receipts(self.campaign, self.manifest, candidate, self.attempt)

    def test_client_receipts(self) -> None:
        summary = self.receipts(self.client_world())
        self.assertEqual(sorted(summary["client_bindings"]), ["claude-desktop"])
        self.assertEqual(sorted(summary["boundary_checks"]), ["third-party-ingress-rejection"])
        with self.assertRaisesRegex(ConfigurationError, "字段不符：run_nonce、status"):
            self.receipts(self.client_world({"run_nonce": "other", "status": "failed"}))
        candidate = self.client_world()
        candidate["client_evidence"]["client_bindings"] = {}
        with self.assertRaisesRegex(ConfigurationError, "与场景清单登记的不一致"):
            self.receipts(candidate)


class ComparisonReaderTests(unittest.TestCase):
    """比较收据读侧：字段闭集、收据核心摘要复算与各绑定的摘要。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.campaign = Path(directory.name).resolve() / "campaign"
        self.campaign.mkdir()
        (self.campaign / "campaign.json").write_text(json.dumps({"campaign_id": "claude-campaign"}), encoding="utf-8")
        bound = {name: chain._write_json(self.campaign / "files" / f"{name}.json", {"name": name}) for name in ("stage", "official", "candidate", "config")}
        report: dict[str, Any] = {field: None for field in compare.STAGE_RESULT_FIELDS}
        report.update(
            {
                "schema_version": compare.COMPARISON_SCHEMA, "client": "claude", "campaign_id": "claude-campaign", "stage": "compare",
                "candidate_id": "cand-1", "status": "complete", "offline_only": True,
                "inputs": {"candidate_stage": chain.binding(bound["stage"], self.campaign, with_bytes=False)},
                "official": {"evaluation_manifest": chain.binding(bound["official"], self.campaign, with_bytes=False)},
                "candidate": {"evaluation_manifest": chain.binding(bound["candidate"], self.campaign, with_bytes=False)},
                "rules": ["SPEC-A-001"], "compared_at_utc": "2026-10-08T00:00:00Z",
            }
        )
        report["package_digest"] = compare._package_digest(report)
        report["builder"] = {"config": chain.binding(bound["config"], self.campaign, with_bytes=False)}
        self.path = self.campaign / "comparisons" / "cand-1" / "result.json"
        self.report = report
        self.bound = bound

    def load(self, report: dict[str, Any]) -> dict[str, Any]:
        chain._write_json(self.path, report)
        return compare.load_stage_result(self.campaign, self.path, "cand-1")

    def test_reader_accepts_and_detects_tampering(self) -> None:
        self.assertEqual(self.load(self.report)["rules"], ["SPEC-A-001"])
        with self.assertRaisesRegex(ConfigurationError, "package_digest 不一致"):
            self.load({**self.report, "rules": ["SPEC-A-001", "SPEC-B-002"]})
        with self.assertRaisesRegex(ConfigurationError, "合同、Campaign、候选或状态不一致"):
            self.load({**self.report, "candidate_id": "cand-2"})
        self.bound["config"].write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "builder 配置缺失或摘要漂移"):
            self.load(self.report)
        self.path.unlink()
        with self.assertRaisesRegex(ConfigurationError, "阶段尚未封存：compare"):
            compare.load_stage_result(self.campaign, self.path, "cand-1")


if __name__ == "__main__":
    unittest.main()
