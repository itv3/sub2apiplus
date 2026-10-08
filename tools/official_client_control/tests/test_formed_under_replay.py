"""Claude 升级 B1-12：有效工具身份、写入时有效性与形成期重放的验收用例。

设计验收逐条对应：
* 登记演进后新 finalizer 通过而未登记即拒绝；
* 演进后旧收据按 ``formed_under`` 重放通过；
* 候选换 revision 后旧 PAIR／Acceptance 重放通过但 ``status`` 不算当前验收；
* 旧结果冒充当前写入被拒。
"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.contracts import (
    VALIDATION_GATE_RECEIPT_SCHEMA,
    VALIDATION_GATE_RECEIPT_V2_SCHEMA,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    FormedContext,
    current_candidate_revision,
    effective_tool_identity,
    finalize_candidate_build,
    finalize_validation,
    formed_coordinates,
    replay_receipt,
    require_acceptance_formed_under,
    tool_evolutions,
)
from tools.official_client_control.tests.fixtures import HistoricalDeliveryToolTree, ManagedToolTree, SyntheticCampaign

PROBE = "official_client_capture/claude_upgrade_b112_probe.json"


class _FormedCase(unittest.TestCase):
    tree: ManagedToolTree
    TOOL_TREE_FACTORY = ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = cls.TOOL_TREE_FACTORY(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self._active = self.tree.active()
        self._active.__enter__()
        self.fixture = SyntheticCampaign(Path(self.temporary.name), schema="v2")
        self.fixture.bootstrap_and_campaign()
        self.fixture.discovery_and_evidence()
        self.fixture.profile_objects()
        self.fixture.profile_approve()

    def tearDown(self) -> None:
        self._active.__exit__(None, None, None)
        self.tree.restore()
        self.temporary.cleanup()

    def change_tools(self, marker: str = "1") -> None:
        """在受管树副本里改一个受管文件，模拟修好工具后的新代码（尚未登记演进）。"""

        self.tree.write(PROBE, f'{{"probe": "{marker}"}}\n'.encode("utf-8"))

    def campaign(self) -> dict[str, Any]:
        return self.fixture.store.load_campaign(self.fixture.campaign_id)

    def status(self) -> dict[str, Any]:
        return WorkflowGates(self.fixture.store).status(self.fixture.campaign_id)

    def replay(self) -> dict[str, Any]:
        return self.fixture.store.replay(
            external_root=self.fixture.external_root, require_external=True
        )

    def receipts(self, kind: str) -> list[dict[str, Any]]:
        return self.fixture.store.list_receipt_refs(kind)


class ToolEvolutionTests(_FormedCase):
    def test_unregistered_evolution_blocks_writes_until_registered(self) -> None:
        self.change_tools()
        with self.assertRaisesRegex(ControlError, "工具身份未登记演进：先执行 tool-evolution"):
            self.fixture.strict_validate()
        with self.assertRaisesRegex(ControlError, "工具身份未登记演进"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id,
                "selector_observed",
                {
                    "catalog_snapshot_ref": self.fixture.references["model-catalog"],
                    "observation_kind": "read_only",
                },
                self.fixture._time(),
            )
        registration = self.fixture.register_evolution()
        self.fixture.strict_validate()
        build = self.fixture.store.load_receipt(self.fixture.references["candidate-build-receipt"])
        self.assertEqual(build["formed_under"]["tool_identity_ref"], registration)
        self.assertEqual(build["producer_tool_sha256"], self.fixture.tool_sha256)
        self.assertEqual(
            effective_tool_identity(self.fixture.store, self.campaign())["tool_files_sha256"],
            self.fixture.tool_sha256,
        )
        status = self.status()
        self.assertEqual(status["checkpoint"], "ready")
        self.assertEqual(status["tool_identity_index"], 1)
        self.assertEqual(self.replay()["result"], "passed")

    def test_registration_must_be_current_code_at_chain_tail(self) -> None:
        self.change_tools()
        stale = self.fixture.evolution_payload()
        self.change_tools("2")
        with self.assertRaisesRegex(ControlError, "to_identity 不是当前受管代码的身份：tool_files_sha256"):
            self.fixture.store.seal_object("tool_evolution_registration", stale)
        payload = self.fixture.evolution_payload()
        wrong_source = deepcopy(payload)
        wrong_source["from_identity"]["control_sha256"] = "0" * 64
        with self.assertRaisesRegex(ControlError, "from_identity 未承接上一份有效工具身份"):
            self.fixture.store.seal_object("tool_evolution_registration", wrong_source)
        early = deepcopy(payload)
        early["approved_at_utc"] = "2026-08-17T00:00:00Z"
        with self.assertRaisesRegex(ControlError, "批准时间早于 Campaign 建立时间"):
            self.fixture.store.seal_object("tool_evolution_registration", early)
        unchanged = deepcopy(payload)
        unchanged["to_identity"] = dict(payload["from_identity"])
        with self.assertRaisesRegex(ControlError, "受管文件清单没有变化"):
            self.fixture.store.seal_object("tool_evolution_registration", unchanged)
        first = self.fixture.register_evolution()
        fork = deepcopy(payload)
        fork["approved_at_utc"] = self.fixture._time()
        with self.assertRaisesRegex(ControlError, "必须接在当前链尾"):
            self.fixture.store.seal_object("tool_evolution_registration", fork)
        self.change_tools("3")
        second = self.fixture.evolution_payload()
        self.assertEqual((second["index"], second["previous_ref"]), (2, first))
        self.fixture.register_evolution()
        chain = tool_evolutions(self.fixture.store, self.campaign())
        self.assertEqual([payload["index"] for _reference, payload in chain], [1, 2])
        self.assertEqual(self.replay()["result"], "passed")

    def test_plan_tool_identity_is_bound_to_effective_identity(self) -> None:
        self.fixture.strict_validate(finalize=False)
        plan = self.fixture.store.load_object(self.fixture.references["validation-plan"])["payload"]
        self.change_tools()
        self.fixture.register_evolution()
        stale = deepcopy(plan)
        stale["plan_id"] = "validation-plan-stale-tool"
        stale["run_conditions"]["run_nonce"] = "nonce-stale-tool"
        stale["identity_sha256"] = canonical_sha256(
            {key: value for key, value in stale.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "不是 Campaign 当前有效工具身份"):
            self.fixture.store.seal_object("validation_execution_plan", stale)
        # 已封存的旧计划重放时仍在有效身份链上。
        WorkflowGates(self.fixture.store).validate_object_graph(
            self.fixture.references["validation-plan"]
        )


class HistoricalDeliveryToolEvolutionTests(_FormedCase):
    """只回归 policy 3 的历史交付合同；不改变其余用例的当前身份策略。"""

    TOOL_TREE_FACTORY = HistoricalDeliveryToolTree

    def test_old_receipts_replay_by_formed_under_after_evolution(self) -> None:
        self.fixture.strict_validate()
        initial_tool = self.fixture.tool_sha256
        self.change_tools()
        # 未登记演进：当前代码已变，重放与当前代码无关，照常通过；写入被拒。
        self.assertEqual(self.replay()["result"], "passed")
        for kind in ("candidate_build", "validation_gate"):
            for reference in self.receipts(kind):
                receipt = replay_receipt(self.fixture.store, reference)
                self.assertIsNone(receipt["formed_under"]["tool_identity_ref"])
                self.assertEqual(receipt["producer_tool_sha256"], initial_tool)
        self.assertEqual(self.status()["checkpoint"], "ready")
        self.fixture.register_evolution()
        self.assertNotEqual(self.fixture.tool_sha256, initial_tool)
        # 演进后：旧收据按形成期（初始身份）重放，新写入按新有效身份形成。
        self.fixture.strict_deliver()
        delivery = self.fixture.store.load_receipt(
            self.fixture.references["candidate-delivery-receipt"]
        )
        self.assertEqual(delivery["formed_under"]["tool_identity_ref"]["object_kind"], "tool_evolution_registration")
        self.assertEqual(delivery["producer_tool_sha256"], self.fixture.tool_sha256)
        replay = self.replay()
        self.assertEqual((replay["result"], replay["receipts"]), ("passed", 3))
        self.assertEqual(self.status()["checkpoint"], "ready_for_operator_release")
        # 再改一次代码：所有收据仍按各自形成期重放通过。
        self.change_tools("2")
        self.assertEqual(self.replay()["result"], "passed")


class CandidateRevisionTests(_FormedCase):
    def test_revision_switch_keeps_old_results_replayable_but_not_current(self) -> None:
        self.fixture.strict_validate()
        self.assertEqual(self.status()["checkpoint"], "ready")
        old_acceptance = self.fixture.references["acceptance-strict"]
        old_pair = self.fixture.references["pair-strict"]
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        self.assertEqual(
            current_candidate_revision(self.fixture.store, self.campaign()),
            (2, "synthetic-candidate-r2"),
        )
        status = self.status()
        self.assertEqual(status["checkpoint"], "profile_approved")
        self.assertEqual(status["candidate_revision"], 2)
        # 旧 revision 的 PAIR／Acceptance 与收据仍按形成期坐标重放通过。
        self.assertEqual(self.replay()["result"], "passed")
        for reference in (old_acceptance, old_pair):
            fact = self.fixture.store.load_fact(reference)
            self.assertEqual(fact["formed_under"]["candidate_revision"], 1)
            self.assertEqual(
                formed_coordinates(self.fixture.store, self.campaign(), fact)["candidate_revision"], 1
            )
        for kind in ("candidate_build", "validation_gate"):
            for reference in self.receipts(kind):
                replay_receipt(self.fixture.store, reference)
        # 新 revision 的候选从新 attempt 完整执行后才重新成为当前验收。
        self.fixture.strict_validate(tag="r2")
        status = self.status()
        self.assertEqual((status["checkpoint"], status["current_candidate_id"]), ("ready", "synthetic-candidate-r2"))
        self.assertEqual(self.replay()["result"], "passed")

    def test_old_results_cannot_masquerade_as_current_writes(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        with self.assertRaisesRegex(ControlError, "不属于候选 revision r2"):
            finalize_validation(
                self.fixture.store,
                self.fixture.campaign_id,
                self.fixture.references["acceptance-strict"],
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
            )
        pair = self.fixture.store.load_fact(self.fixture.references["pair-strict"])["payload"]
        replayed = deepcopy(pair)
        replayed["pair_id"] = "PAIR-SPEC-001"
        with self.assertRaisesRegex(ControlError, "不属于候选 revision r2"):
            self.fixture.store.append_fact(
                self.fixture.campaign_id, "pair_recorded", replayed, self.fixture._time()
            )
        build_input = self.fixture.candidate_build_input()
        with self.assertRaisesRegex(ControlError, "CandidateBuildReceipt 的候选 synthetic-candidate-strict 不属于候选 revision r2"):
            finalize_candidate_build(self.fixture.store, self.fixture.campaign_id, build_input)
        # 旧收据原样改字段后写入，形成期坐标不是当前坐标。
        old_gate = self.fixture.store.load_receipt(self.fixture.references["validation-gate-receipt"])
        forged = deepcopy(old_gate)
        forged["output_sha256"] = "e" * 64
        forged["identity_sha256"] = canonical_sha256(
            {key: value for key, value in forged.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "旧结果不得冒充当前写入"):
            self.fixture.store.write_receipt("validation_gate", forged)
        legacy = {key: value for key, value in old_gate.items() if key != "formed_under"}
        legacy["schema_version"] = VALIDATION_GATE_RECEIPT_SCHEMA
        legacy["identity_sha256"] = canonical_sha256(
            {key: value for key, value in legacy.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "收据合同版本与 Campaign 版本不一致"):
            self.fixture.store.write_receipt("validation_gate", legacy)
        self.assertEqual(old_gate["schema_version"], VALIDATION_GATE_RECEIPT_V2_SCHEMA)
        self.assertEqual(self.replay()["result"], "passed")

    def test_old_receipt_cannot_masquerade_after_tool_evolution(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.change_tools()
        self.fixture.register_evolution()
        old_gate = self.fixture.store.load_receipt(self.fixture.references["validation-gate-receipt"])
        forged = deepcopy(old_gate)
        forged["completed_at_utc"] = self.fixture._time()
        forged["identity_sha256"] = canonical_sha256(
            {key: value for key, value in forged.items() if key != "identity_sha256"}
        )
        with self.assertRaisesRegex(ControlError, "旧结果不得冒充当前写入"):
            self.fixture.store.write_receipt("validation_gate", forged)

    def test_revision_fact_gate(self) -> None:
        self.fixture.strict_validate(finalize=False)

        def record(payload: dict[str, Any]) -> dict[str, Any]:
            return self.fixture.store.append_fact(
                self.fixture.campaign_id, "candidate_revision_recorded", payload, self.fixture._time()
            )

        base = {"revision": 2, "candidate_id": "synthetic-candidate-r2", "supersedes": None, "invalidation_ref": None}
        with self.assertRaisesRegex(ControlError, "首条候选 revision 事实只能"):
            record(dict(base, revision=3))
        with self.assertRaisesRegex(ControlError, "显式确认 r1 必须先于任何候选级事实"):
            record(dict(base, revision=1))
        with self.assertRaisesRegex(ControlError, "必须对应新的 Candidate ID"):
            record(dict(base, candidate_id="synthetic-candidate-strict"))
        first = record(base)
        with self.assertRaisesRegex(ControlError, "逐一递增并 supersedes 上一条"):
            record(dict(base, revision=3, candidate_id="synthetic-candidate-r3"))
        with self.assertRaisesRegex(ControlError, "逐一递增并 supersedes 上一条"):
            record(dict(base, revision=4, candidate_id="synthetic-candidate-r3", supersedes=first))
        with self.assertRaisesRegex(ControlError, "必须对应新的 Candidate ID"):
            record(dict(base, revision=3, candidate_id="synthetic-candidate-r2", supersedes=first))
        record(dict(base, revision=3, candidate_id="synthetic-candidate-r3", supersedes=first))
        self.assertEqual(
            current_candidate_revision(self.fixture.store, self.campaign()),
            (3, "synthetic-candidate-r3"),
        )
        self.assertEqual(self.replay()["result"], "passed")

    def test_explicit_r1_restricts_candidate_from_the_start(self) -> None:
        self.fixture.store.append_fact(
            self.fixture.campaign_id,
            "candidate_revision_recorded",
            {"revision": 1, "candidate_id": "synthetic-candidate-other", "supersedes": None, "invalidation_ref": None},
            self.fixture._time(),
        )
        with self.assertRaisesRegex(ControlError, "不属于候选 revision r1"):
            self.fixture.strict_validate()


class FormedUnderUnitTests(_FormedCase):
    def test_acceptance_currency_compares_revision_and_baseline(self) -> None:
        acceptance = {"formed_under": {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None}}
        current = {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None}
        require_acceptance_formed_under(acceptance, current, "测试")
        require_acceptance_formed_under(acceptance, None, "v1 不检查")
        for change in (
            {"candidate_revision": 2},
            {"evaluation_baseline": {"baseline": 1, "baseline_commit_sha256": "a" * 64}},
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(ControlError, "旧结果不得冒充当前写入"):
                    require_acceptance_formed_under(acceptance, dict(current, **change), "测试")

    def test_unverifiable_coordinates_fail_closed(self) -> None:
        context = FormedContext(self.fixture.store, self.campaign())
        base = {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None}
        context.coordinates({"formed_under": base})
        cases = [
            ({"candidate_revision": 2}, "r2 没有对应的 candidate_revision_recorded 事实"),
            (
                {"evaluation_baseline": {"baseline": 1, "baseline_commit_sha256": "a" * 64}},
                "评估基线事实由 B4-04 定义",
            ),
            (
                {"tool_identity_ref": {"object_kind": "tool_evolution_registration", "sha256": "a" * 64}},
                "不在本 Campaign 的工具演进登记链上",
            ),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ControlError, message):
                    context.coordinates({"formed_under": dict(base, **change)})

    def _write_raw_fact(
        self, dimension: str, fact_kind: str, payload: dict[str, Any], formed_under: dict[str, Any]
    ) -> None:
        """绕过门禁直接落盘一条形状合法的 fact v2，模拟被篡改或伪造坐标的历史数据。"""

        store = self.fixture.store
        chain = store.list_facts(self.fixture.campaign_id, dimension)
        document = {
            "schema_version": "official-client-control-fact/v2",
            "campaign_id": self.fixture.campaign_id,
            "dimension": dimension,
            "fact_kind": fact_kind,
            "sequence": len(chain) + 1,
            "previous_fact_sha256": chain[-1]["_sha256"] if chain else None,
            "issued_at_utc": self.fixture._time(),
            "payload": payload,
            "formed_under": formed_under,
            "idempotency_key": None,
            "producer_run": None,
        }
        content = canonical_json_bytes(document)
        write_once(
            store.root
            / "facts"
            / self.fixture.campaign_id
            / dimension
            / f"{len(chain) + 1:08d}-{sha256_bytes(content)}.json",
            content,
        )

    def test_replay_detects_forged_formed_coordinates(self) -> None:
        self.fixture.strict_validate()
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        self.fixture.strict_validate(tag="r2")
        stale = {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None}
        self._write_raw_fact(
            "deployment",
            "release_promoted",
            {
                "candidate_ref": self.fixture.references["candidate-r2"],
                "acceptance_ref": self.fixture.references["acceptance-r2"],
                "release_artifact_ref": self.fixture.references["release"],
                "promotion_diff_ref": self.fixture.seal_manifest("promotion_diff", "forged-diff"),
            },
            stale,
        )
        with self.assertRaisesRegex(ControlError, "形成期坐标早于其引用的事实"):
            self.replay()

    def test_replay_detects_coordinate_regression_within_a_chain(self) -> None:
        self.fixture.strict_validate()
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        self.fixture.strict_validate(tag="r2")
        self._write_raw_fact(
            "validation",
            "scenario_prepared",
            {
                "candidate_id": "synthetic-candidate-strict",
                "scenario_id": "baseline",
                "attempt_id": "attempt-forged",
                "stage": "prepare",
                "previous_stage_ref": None,
                "artifact_refs": [],
                "result": "prepared",
            },
            {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None},
        )
        with self.assertRaisesRegex(ControlError, "同一维度链内形成期坐标回退"):
            self.replay()

    def test_v2_production_chain_receipts_replay(self) -> None:
        self.fixture.candidate_and_accept()
        self.fixture.promote()
        self.fixture.deploy()
        status = self.status()
        self.assertEqual((status["checkpoint"], status["production_state"]), ("restored_active", "verified_active"))
        for kind in ("promotion", "activation"):
            (reference,) = self.receipts(kind)
            receipt = replay_receipt(self.fixture.store, reference)
            self.assertEqual(receipt["schema_version"], f"official-client-control-{kind}-receipt/v2")
        self.assertEqual(self.replay()["result"], "passed")
        # 生产链之后换 revision：旧部署与激活收据重放通过，但不再算当前有效生产验证。
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        status = self.status()
        self.assertEqual((status["checkpoint"], status["production_state"]), ("profile_approved", "not_activated"))
        self.assertEqual(self.replay()["result"], "passed")


if __name__ == "__main__":
    unittest.main()
