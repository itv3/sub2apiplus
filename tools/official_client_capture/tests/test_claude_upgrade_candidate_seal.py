"""Claude 候选证据封存的 Store 写入层（Claude 升级控制面设计 B4-02，第一步：不依赖证据格式的部分）。

全部离线、零请求。世界沿用 B4-06a 的验收夹具（``test_claude_upgrade_accept.AcceptWorld``：Store 夹具到 ProfileApproval、
严格 v2 候选冻结、合成 Claude Campaign、候选外部门禁收据替身），只是 VC-5 前缀——selector 前态、执行计划、ValidationAttempt、
场景四阶段与 CandidateEvidencePackage——改由本层写入，再让 B4-06a 的验收写入链接着跑到 ``ready``，证明两层的合同对得上。

覆盖：
* 首轮封存：外部门禁计划与 ``candidate_external_gate_plan_entries`` 逐字相同、三条门禁与候选 Job 一起进 execute 集合、
  ValidationAttempt ID 为 ``<采集 attempt>-v1``、证据包取值、Store 检查点仍是 ``candidate_sealed``、Store 重放通过；B4-06a
  接着写到 ``ready``；同一请求重跑逐步复用、事实与对象数不变；
* 每一次 Store 写入之后被强杀，重跑同一请求收敛到与一次跑完相同的事实与对象集合；
* 零写入拒绝：场景没有精确覆盖 ScenarioPlan、运行证据有未通过条目、场景证据或 capture manifest 不在原始证据清单、
  Job 与门禁重名、候选不是当前 revision 的候选、同一轮已封存过不同的执行计划、第 2 轮（门禁重试）报待实现；
* scenario-only 断言：按 VC-1 证据项的场景映射到候选场景批准、source 为 reuse，B4-06a 接着写到 ``ready``；映射不到
  场景时闭合门禁 blocked、一条不写。
"""

from __future__ import annotations

import collections
import hashlib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade_accept as accept
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_accept as accept_tests
from tools.official_client_control.contracts import atomic_assertion_ledger_identity_sha256
from tools.official_client_control.tests.fixtures import SyntheticCampaign

CANDIDATE = accept_tests.CANDIDATE
JOB = "claude-candidate-core"
SCENARIO_ONLY_ASSERTION = "SPEC-STATE-005"
SCENARIO_ONLY_OWNER = "SCENARIO-LOCAL-REJECTION"
SCENARIO_ONLY_EVIDENCE = "EVIDENCE-LOCAL-REJECTION"


def _external(path: str, seed: str, root: Path | None = None) -> dict[str, Any]:
    """外部证据绑定；给出 ``root``（Store 夹具的外部证据根）时同时写出文件，Store 重放能逐个核对。"""

    content = seed.encode()
    if root is not None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


class ScenarioOnlyCampaign(SyntheticCampaign):
    """Store 夹具的变体：VC-1 多一个证据项、VC-2 台账多一条 scenario-only 断言（官方客户端本地拒绝）。

    ``evidence_sources`` 是这个证据项的 ``source_ids``：取 ScenarioPlan 里的场景（baseline）时能映射，取规则 ID 时映射不到。
    """

    evidence_sources = ["baseline"]

    def evidence_package_payload(self) -> dict[str, Any]:
        payload = super().evidence_package_payload()
        extra = {
            "evidence_id": SCENARIO_ONLY_EVIDENCE,
            "source_ids": list(self.evidence_sources),
            "evidence_refs": [self.evidence_binding],
            "applicability": ["linux/amd64", "privacy=default"],
        }
        payload["evidence_items"] = sorted([*payload["evidence_items"], extra], key=lambda item: item["evidence_id"])
        return payload

    def classify_rules(self, evidence_level: str = "verified") -> None:
        real = self.store.seal_object

        def sealing(kind: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            if kind == "atomic_assertion_ledger":
                payload = dict(payload)
                payload["scenario_only_owner_ids"] = [SCENARIO_ONLY_OWNER]
                payload["assertions"] = [
                    *payload["assertions"],
                    {
                        "assertion_id": SCENARIO_ONLY_ASSERTION,
                        "owner_kind": "scenario_only",
                        "owner_id": SCENARIO_ONLY_OWNER,
                        "evidence_item_ids": [SCENARIO_ONLY_EVIDENCE],
                        "conditions": ["linux/amd64", "privacy=default"],
                        "channels": ["M"],
                        "result": "pass",
                    },
                ]
                payload["identity_sha256"] = atomic_assertion_ledger_identity_sha256(payload)
            return real(kind, payload, *args, **kwargs)

        with mock.patch.object(self.store, "seal_object", side_effect=sealing):
            super().classify_rules(evidence_level)


class UnmappedScenarioOnlyCampaign(ScenarioOnlyCampaign):
    evidence_sources = ["SPEC-001"]


class SealWorld(accept_tests.AcceptWorld):
    """B4-06a 的验收世界，VC-5 前缀改由 B4-02 的 Store 写入层写（构造时不开 attempt，``seal`` 写入后再填引用）。"""

    def open_attempt(self, number: int, plan_gates: list[dict[str, Any]]) -> tuple[Any, Any, Any]:
        del number, plan_gates
        return None, None, None

    def seal_request(self, number: int = 1, **overrides: Any) -> dict[str, Any]:
        evidence = self.fixture.evidence_binding
        external = self.fixture.external_root
        capture = _external("runs/candidate/claude-candidate-core/capture-manifest.json", f"capture-{number}", external)
        request: dict[str, Any] = {
            "candidate_id": CANDIDATE,
            "capture_attempt_id": f"capture-{number}",
            "validation_round": 1,
            "issued_at_utc": accept_tests.ISSUED_AT,
            "run_conditions": {
                "run_nonce": f"nonce-seal-{number}",
                "environment_sha256": "4" * 64,
                "configuration_sha256": "5" * 64,
                "network_sha256": "6" * 64,
                "account_model_sha256": "a" * 64,
            },
            "selector_before_snapshot": self.store.load_object(self.refs["runtime-before"])["payload"],
            "gate_execution": {"host_id": accept_tests.HOST, "architecture": accept_tests.ARCHITECTURE},
            "job_ids": {"execute": [JOB], "reuse": []},
            "scenario_evidence": {"baseline": [evidence]},
            "seal": {
                "generation": "g-0123456789abcdef",
                "review_sha256": "f" * 64,
                "reviewer": "reviewer-a",
                "evidence_manifest": _external("evidence/campaigns/c/candidates/seal/evidence-manifest.json", f"manifest-{number}", external),
                "capture_manifest": capture,
            },
            "raw_evidence_inventory": sorted([evidence, capture], key=lambda item: item["path"]),
            "operational_evidence": {
                name: [{"id": f"{name.replace('_', '-')}-check", "facts": {"result": "pass", "digest": "3" * 64}}]
                for name in seal.OPERATIONAL_EVIDENCE
            },
        }
        request.update(overrides)
        return request

    def seal(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        result = seal.record_candidate_seal_chain(self.campaign.campaign_dir, self.campaign.manifest, request or self.seal_request())
        self.attempt_ref = result["validation_attempt_ref"]
        self.package_ref = result["candidate_evidence_package_ref"]
        self.scenario_ref = result["scenario_approval_refs"]["baseline"]
        return result

    def store_footprint(self) -> tuple[dict[str, int], dict[str, int]]:
        facts = collections.Counter(fact["fact_kind"] for fact in self.store.list_facts(self.campaign_id))
        objects = {
            kind: len(self.store.list_object_refs(kind))
            for kind in ("operational_evidence", "runtime_catalog_snapshot", "validation_execution_plan", "candidate_evidence_package")
        }
        return dict(facts), objects


class _SealCase(accept_tests._AcceptCase):
    def seal_world(self, name: str = "world", campaign_class: type = SyntheticCampaign) -> SealWorld:
        with mock.patch.object(accept_tests, "SyntheticCampaign", campaign_class):
            world = SealWorld(self.root / name)
        self.worlds[world.campaign.campaign_dir.resolve()] = world
        return world

    def assert_zero_write(self, world: SealWorld, request: dict[str, Any], pattern: str) -> None:
        before = world.store_footprint()
        with self.assertRaisesRegex(ConfigurationError, pattern):
            world.seal(request)
        self.assertEqual(world.store_footprint(), before)


class FirstRoundTests(_SealCase):
    def test_first_round_writes_vc5_entry_and_accept_chain_reaches_ready(self) -> None:
        world = self.seal_world()
        before_facts, _objects = world.store_footprint()
        result = world.seal()
        self.assertEqual((result["validation_attempt_id"], result["validation_round"]), ("capture-1-v1", 1))
        plan = world.store.load_object(result["validation_execution_plan_ref"])["payload"]
        expected_gates = accept.candidate_external_gate_plan_entries(host_id=accept_tests.HOST, architecture=accept_tests.ARCHITECTURE)
        self.assertEqual(plan["external_gates"], expected_gates)
        self.assertEqual(plan["execute_item_ids"], sorted([JOB, *(gate["gate_id"] for gate in expected_gates)]))
        self.assertEqual(plan["reuse_item_ids"], [])
        self.assertEqual(plan["production_selector_before_ref"], result["selector_before"]["fact_ref"])
        self.assertEqual(plan["run_conditions"]["tool_sha256"], claude_upgrade_seal._require_store_tool_current(world.store, world.store.load_campaign(world.campaign_id)))
        attempt = world.store.load_fact(result["validation_attempt_ref"])["payload"]
        self.assertEqual((attempt["attempt_id"], attempt["validation_execution_plan_ref"]), ("capture-1-v1", result["validation_execution_plan_ref"]))
        package = world.store.load_object(result["candidate_evidence_package_ref"])["payload"]
        self.assertEqual(
            (package["package_id"], package["attempt_id"], package["validation_attempt_ref"], package["scenario_approval_refs"]),
            ("capture-1-v1-evidence", "capture-1-v1", result["validation_attempt_ref"], [result["scenario_approval_refs"]["baseline"]]),
        )
        self.assertEqual(package["scenario_assertion_results"], [])
        self.assertEqual(package["raw_evidence_inventory"], world.seal_request()["raw_evidence_inventory"])
        facts, objects = world.store_footprint()
        added = {kind: facts[kind] - before_facts.get(kind, 0) for kind in facts if facts[kind] != before_facts.get(kind, 0)}
        self.assertEqual(
            added,
            {"selector_observed": 1, "validation_attempt_created": 1, "scenario_prepared": 1, "scenario_captured": 1, "scenario_sealed": 1, "scenario_approved": 1},
        )
        self.assertEqual((objects["validation_execution_plan"], objects["candidate_evidence_package"]), (1, 1))
        self.assertEqual(world.status()["checkpoint"], "candidate_sealed")
        world.store.replay(external_root=world.fixture.external_root, require_external=True)
        # 同一请求重跑：每一步复用原对象与原事实。
        again = world.seal()
        self.assertTrue(again["selector_before"]["reused"] and again["validation_attempt"]["reused"])
        self.assertTrue(all(all(stages.values()) for stages in again["scenario_facts_reused"].values()))
        self.assertEqual(again["candidate_evidence_package_ref"], result["candidate_evidence_package_ref"])
        self.assertEqual(world.store_footprint(), (facts, objects))
        # B4-06a 的验收写入链接着跑：门禁定稿绑定本层的 ValidationAttempt，证据包与场景批准对得上，Store 到 ready。
        world.gate_receipt()
        accepted = world.run()
        self.assertEqual(sorted(record["gate_id"] for record in accepted["gate_receipts"]), [gate["gate_id"] for gate in expected_gates])
        self.assertEqual(world.status()["checkpoint"], "ready")

    def test_kill_after_each_store_write_converges(self) -> None:
        reference = self.seal_world("reference")
        reference.seal()
        expected = reference.store_footprint()
        real_cli = claude_upgrade_seal.store_cli
        # 写入顺序：对象 8 个（审核清单、场景证据与封存清单、四份运行证据、selector 快照）→ selector 前态(9) → 执行计划(10)
        # → ValidationAttempt(11) → 场景四阶段(12～15) → 证据包(16)。
        for kill_after in (1, 8, 9, 10, 11, 13, 15, 16):
            with self.subTest(kill_after=kill_after):
                world = self.seal_world(f"kill-{kill_after}")
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
                        world.seal()
                world.seal()
                self.assertEqual(world.store_footprint(), expected)
                self.assertEqual(world.status()["checkpoint"], "candidate_sealed")


class RejectionTests(_SealCase):
    def test_precheck_rejections_write_nothing(self) -> None:
        world = self.seal_world()
        request = world.seal_request()
        evidence = world.fixture.evidence_binding
        stray = _external("runs/candidate/stray.bin", "stray")
        self.assert_zero_write(world, dict(request, scenario_evidence={"other": [evidence]}), r"没有精确覆盖 VC-3 ScenarioPlan：缺 \['baseline'\]，多 \['other'\]")
        failed = dict(request["operational_evidence"], recovery=[{"id": "gateway-restore", "facts": {"result": "failed"}}])
        self.assert_zero_write(world, dict(request, operational_evidence=failed), r"条目 gateway-restore 没有通过")
        self.assert_zero_write(world, dict(request, scenario_evidence={"baseline": [stray]}), r"不在原始证据清单里")
        self.assert_zero_write(
            world,
            dict(request, seal=dict(request["seal"], capture_manifest=stray)),
            r"capture manifest 不在原始证据清单里",
        )
        self.assert_zero_write(world, dict(request, job_ids={"execute": ["full-regression"], "reuse": []}), r"候选 Job ID 与外部门禁 ID 重名")
        self.assert_zero_write(world, dict(request, candidate_id="claude-cand-other"), r"Store 没有候选 claude-cand-other")
        self.assert_zero_write(world, dict(request, validation_round=2), r"门禁重试")
        self.assert_zero_write(world, dict(request, extra=1), r"字段必须恰好是")
        # 同一轮已封存：执行计划不同（运行条件换了）即拒绝，一条不写。
        world.seal()
        changed = dict(request["run_conditions"], run_nonce="nonce-other")
        self.assert_zero_write(world, dict(request, run_conditions=changed), r"ValidationAttempt capture-1-v1 已经存在且与本次封存的执行计划不同")


class ScenarioOnlyTests(_SealCase):
    def test_scenario_only_assertions_reuse_mapped_scenario_approvals(self) -> None:
        world = self.seal_world(campaign_class=ScenarioOnlyCampaign)
        result = world.seal()
        approval = result["scenario_approval_refs"]["baseline"]
        self.assertEqual(
            result["scenario_assertion_results"],
            [
                {
                    "assertion_id": SCENARIO_ONLY_ASSERTION,
                    "owner_id": SCENARIO_ONLY_OWNER,
                    "result": "pass",
                    "source": "reuse",
                    "scenario_approval_refs": [approval],
                }
            ],
        )
        package = world.store.load_object(result["candidate_evidence_package_ref"])["payload"]
        self.assertEqual(package["scenario_assertion_results"], result["scenario_assertion_results"])
        world.gate_receipt()
        world.run()
        self.assertEqual(world.status()["checkpoint"], "ready")

    def test_unmapped_scenario_only_assertion_blocks_closure_without_writes(self) -> None:
        world = self.seal_world(campaign_class=UnmappedScenarioOnlyCampaign)
        planned = seal.plan_candidate_seal_chain(world.campaign.campaign_dir, world.campaign.manifest, world.seal_request())
        self.assertEqual(planned["closure"], {"status": "blocked", "reasons": ["scenario_only_unmapped"]})
        self.assertEqual(planned["scenario_only"], {SCENARIO_ONLY_ASSERTION: {"owner_id": SCENARIO_ONLY_OWNER, "scenarios": []}})
        self.assert_zero_write(world, world.seal_request(), r"闭合门禁 blocked")

    def test_mapping_function_rejects_unpassed_assertions(self) -> None:
        ledger = {
            "assertions": [
                {"assertion_id": "A-1", "owner_kind": "scenario_only", "owner_id": "G-1", "evidence_item_ids": ["E-1", "E-2"], "result": "failed"},
                {"assertion_id": "A-0", "owner_kind": "required_rule", "owner_id": "SPEC-001", "evidence_item_ids": ["E-1"], "result": "pass"},
            ]
        }
        package = {"evidence_items": [{"evidence_id": "E-1", "source_ids": ["s-b", "s-x"]}, {"evidence_id": "E-2", "source_ids": ["s-a"]}]}
        mapping, reasons = seal.scenario_only_mapping(ledger, package, ["s-a", "s-b"])
        self.assertEqual(mapping, {"A-1": {"owner_id": "G-1", "scenarios": ["s-a", "s-b"]}})
        self.assertEqual(reasons, ["scenario_only_not_passed"])


if __name__ == "__main__":
    unittest.main()
