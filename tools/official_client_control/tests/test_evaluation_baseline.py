"""Claude 升级 B4-04：评估基线 b<K> 与 Store 分层的验收用例。

设计验收逐条对应：
* b1 重评只重跑受影响规则——Store 侧表达为：受影响规则在 b1 下重跑写 PAIR v3，b1 的 Acceptance v3 必须引用它，
  不得拿 b0 的旧结果冒充；未受影响的规则沿用 b0 的 PAIR，不必重跑；
* b0 结果重放通过但不算当前验收——开 b1 之后 b0 的 PAIR／Acceptance／VC-5 完成按形成期重放通过，status 回到
  candidate_sealed，b0 的 Acceptance 不能再推进 VC-5 完成。

另覆盖：PAIR／Acceptance v3 合同与形成期基线一致、唯一性 (candidate, pair_id, baseline) 与 (candidate, baseline)、
VC-5 完成按基线唯一、评估基线事实门禁（编号递增可跳号、supersedes、COMMIT 不复用、须已进入 VC-5、交付开始后不得再
开、只适用于严格候选）、换 revision 回到 b0、重放对不可达与回退基线的检测、v1 Campaign 逐字保持原拒绝文案、CLI。
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
from tools.official_client_control.cli import build_parser, execute
from tools.official_client_control.contracts import candidate_identity_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    current_evaluation_baseline,
    finalize_candidate_build,
    finalize_candidate_delivery,
    finalize_validation,
    replay_receipt,
)
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

STRICT = "synthetic-candidate-strict"


class _BaselineCase(unittest.TestCase):
    tree: ManagedToolTree

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

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
        self.store = self.fixture.store
        self.campaign_id = self.fixture.campaign_id

    def tearDown(self) -> None:
        self._active.__exit__(None, None, None)
        self.tree.restore()
        self.temporary.cleanup()

    def campaign(self) -> dict[str, Any]:
        return self.store.load_campaign(self.campaign_id)

    def status(self) -> dict[str, Any]:
        return WorkflowGates(self.store).status(self.campaign_id)

    def replay(self) -> dict[str, Any]:
        return self.store.replay(external_root=self.fixture.external_root, require_external=True)

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.append_fact(self.campaign_id, kind, payload, self.fixture._time())

    def payload(self, name: str) -> dict[str, Any]:
        return deepcopy(self.store.load_fact(self.fixture.references[name])["payload"])

    def baseline(self, number: int) -> dict[str, Any]:
        return {"baseline": number, "baseline_commit_sha256": self.fixture.baseline_commit(number)}

    def baseline_payload(self, number: int, **overrides: Any) -> dict[str, Any]:
        payload = {
            "baseline": number,
            "baseline_commit_sha256": self.fixture.baseline_commit(number),
            "candidate_id": STRICT,
            "candidate_ref": self.fixture.references.get("candidate-strict"),
            "supersedes": None,
            "kind": "evaluator-only",
        }
        payload.update(overrides)
        return payload

    def write_raw_fact(
        self, dimension: str, fact_kind: str, payload: dict[str, Any], formed_under: dict[str, Any]
    ) -> None:
        """绕过门禁直接落盘一条形状合法的 fact v2，模拟被篡改的历史数据。"""

        chain = self.store.list_facts(self.campaign_id, dimension)
        document = {
            "schema_version": "official-client-control-fact/v2",
            "campaign_id": self.campaign_id,
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
            self.store.root
            / "facts"
            / self.campaign_id
            / dimension
            / f"{len(chain) + 1:08d}-{sha256_bytes(content)}.json",
            content,
        )


class BaselineAcceptanceTests(_BaselineCase):
    def test_b0_results_replay_but_are_not_current_after_b1(self) -> None:
        """验收：b0 结果重放通过但不算当前验收；b1 重跑的受影响规则必须用新结果。"""

        self.fixture.strict_validate()
        self.assertEqual(self.status()["checkpoint"], "ready")
        b0_pair = self.fixture.references["pair-strict"]
        b0_acceptance = self.fixture.references["acceptance-strict"]
        b0_completed = self.fixture.references["validation-completed"]
        self.fixture.open_evaluation_baseline(1)
        self.assertEqual(current_evaluation_baseline(self.store, self.campaign()), self.baseline(1))
        status = self.status()
        # 采集层（候选冻结、场景、attempt）仍属于当前候选；评估层的 b0 结果不再是当前验收。
        self.assertEqual((status["checkpoint"], status["evaluation_baseline"]), ("candidate_sealed", self.baseline(1)))
        self.assertEqual(self.replay()["result"], "passed")
        for reference in (b0_pair, b0_acceptance, b0_completed):
            self.assertIsNone(self.store.load_fact(reference)["formed_under"]["evaluation_baseline"])
        for kind in ("candidate_build", "validation_gate"):
            for reference in self.store.list_receipt_refs(kind):
                replay_receipt(self.store, reference)
        # b0 的 Acceptance 不能冒充当前写入推进 VC-5 完成。
        with self.assertRaisesRegex(ControlError, "旧结果不得冒充当前写入"):
            finalize_validation(
                self.store,
                self.campaign_id,
                b0_acceptance,
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
            )
        # 受影响规则在 b1 重跑；b1 的 Acceptance 不得拿 b0 的旧结果冒充。
        b1_pair = self.fixture.rerun_pair_under_baseline()
        self.assertEqual(self.store.load_fact(b1_pair)["formed_under"]["evaluation_baseline"], self.baseline(1))
        stale = self.fixture.baseline_acceptance_payload([b0_pair])
        with self.assertRaisesRegex(ControlError, "PAIR-SPEC-001 不是该评估基线下的有效结果：b1 已重跑该规则"):
            self.append("acceptance_recorded", stale)
        acceptance = self.fixture.accept_under_baseline([b1_pair])
        self.assertEqual(self.store.load_fact(acceptance)["payload"]["evaluation_baseline"], 1)
        status = self.status()
        self.assertEqual((status["checkpoint"], status["evaluation_baseline"]), ("ready", self.baseline(1)))
        self.assertEqual(self.replay()["result"], "passed")

    def test_unaffected_rules_reuse_previous_baseline_pair(self) -> None:
        """验收：b1 重评只重跑受影响规则——未受影响的规则沿用 b0 的 PAIR，b1 的 Acceptance 直接引用它。"""

        self.fixture.strict_validate()
        b0_pair = self.fixture.references["pair-strict"]
        self.fixture.open_evaluation_baseline(1)
        acceptance = self.fixture.accept_under_baseline([b0_pair])
        payload = self.store.load_fact(acceptance)["payload"]
        self.assertEqual((payload["pair_refs"], payload["evaluation_baseline"]), ([b0_pair], 1))
        self.assertEqual(self.status()["checkpoint"], "ready")
        # b1 的 Acceptance 形成后该基线的 PAIR 集合封闭：事后在 b1 补跑规则被拒，改结论只能开新基线。
        with self.assertRaisesRegex(ControlError, "评估基线 b1 的 Acceptance 已形成：同一基线不得再追加 PAIR"):
            self.fixture.rerun_pair_under_baseline()
        # 再开 b3（编号可跳过放弃的 b2）：这次规则受影响，b3 必须用重跑的结果。
        self.fixture.open_evaluation_baseline(3, kind="approval-revision")
        b3_pair = self.fixture.rerun_pair_under_baseline()
        with self.assertRaisesRegex(ControlError, "b3 已重跑该规则，b0 的旧结果不得冒充"):
            self.append("acceptance_recorded", self.fixture.baseline_acceptance_payload([b0_pair]))
        self.fixture.accept_under_baseline([b3_pair])
        status = self.status()
        self.assertEqual((status["checkpoint"], status["evaluation_baseline"]["baseline"]), ("ready", 3))
        self.assertEqual(self.replay()["result"], "passed")


class BaselineContractTests(_BaselineCase):
    def test_result_contract_must_match_formed_baseline(self) -> None:
        self.fixture.strict_validate(finalize=False)
        pair = self.payload("pair-strict")
        v3 = dict(pair, schema_version="official-client-pair/v3", **{
            "evaluation_baseline": 1,
            "baseline_commit_sha256": self.fixture.baseline_commit(1),
        })
        with self.assertRaisesRegex(ControlError, r"PAIR v3 只能在评估基线 b<K>（K≥1）下形成"):
            self.append("pair_recorded", v3)
        self.fixture.open_evaluation_baseline(1)
        with self.assertRaisesRegex(ControlError, "PAIR 形成于评估基线 b1：基线下的结果必须使用 v3 合同"):
            self.append("pair_recorded", pair)
        wrong = dict(v3, baseline_commit_sha256="0" * 64)
        with self.assertRaisesRegex(ControlError, "PAIR 记录的评估基线 b1 不是形成期评估基线 b1"):
            self.append("pair_recorded", wrong)
        rerun = self.fixture.rerun_pair_under_baseline()
        with self.assertRaisesRegex(ControlError, r"同一 Candidate 的 PAIR 不得重复或覆盖（评估基线 b1）"):
            self.append("pair_recorded", self.store.load_fact(rerun)["payload"])
        with self.assertRaisesRegex(ControlError, "Acceptance 形成于评估基线 b1：基线下的结果必须使用 v3 合同"):
            self.append("acceptance_recorded", self.payload("acceptance-strict"))
        acceptance = self.fixture.accept_under_baseline([rerun], finalize=False)
        with self.assertRaisesRegex(ControlError, r"同一 Candidate 的 AcceptanceFact 不得覆盖（评估基线 b1）"):
            self.append("acceptance_recorded", self.store.load_fact(acceptance)["payload"])
        # validation_completed 绑定当前基线：同一候选在 b1 只完成一次。
        finalize_validation(
            self.store,
            self.campaign_id,
            acceptance,
            self.fixture.references["validation-selector-after"],
            self.fixture._time(),
        )
        with self.assertRaisesRegex(ControlError, "同一 Candidate／attempt 的 VC-5 完成事实不得覆盖"):
            finalize_validation(
                self.store,
                self.campaign_id,
                acceptance,
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
            )
        self.assertEqual(self.replay()["result"], "passed")

    def test_v1_campaign_keeps_original_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = SyntheticCampaign(Path(directory))
            legacy.bootstrap_and_campaign()
            legacy.discovery_and_evidence()
            legacy.profile_objects()
            legacy.profile_approve()
            legacy.strict_validate(finalize=False)
            pair = legacy.store.load_fact(legacy.references["pair-strict"])["payload"]
            acceptance = legacy.store.load_fact(legacy.references["acceptance-strict"])["payload"]
            fields = {"evaluation_baseline": 1, "baseline_commit_sha256": "a" * 64}
            cases = [
                ("pair_recorded", dict(pair, schema_version="official-client-pair/v3", **fields), "pair_recorded.schema_version 不匹配"),
                ("acceptance_recorded", dict(acceptance, schema_version="official-client-acceptance/v3", **fields), "acceptance_recorded.schema_version 不匹配"),
                ("evaluation_baseline_recorded", {
                    "baseline": 1,
                    "baseline_commit_sha256": "a" * 64,
                    "candidate_id": STRICT,
                    "candidate_ref": legacy.references["candidate-strict"],
                    "supersedes": None,
                    "kind": "evaluator-only",
                }, "只能出现在 fact v2"),
            ]
            for kind, payload, message in cases:
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(ControlError, message):
                        legacy.store.append_fact(legacy.campaign_id, kind, payload, legacy._time())


class BaselineFactGateTests(_BaselineCase):
    def test_numbering_supersedes_commit_and_kind(self) -> None:
        self.fixture.strict_validate(finalize=False)
        first = self.fixture.open_evaluation_baseline(2)
        with self.assertRaisesRegex(ControlError, "新评估基线编号必须大于当前基线 b2"):
            self.append("evaluation_baseline_recorded", self.baseline_payload(1, supersedes=first))
        with self.assertRaisesRegex(ControlError, "必须 supersedes 同一候选 revision 内的上一条基线事实"):
            self.append("evaluation_baseline_recorded", self.baseline_payload(3))
        with self.assertRaisesRegex(ControlError, "评估基线 COMMIT 摘要不得复用"):
            self.append(
                "evaluation_baseline_recorded",
                self.baseline_payload(
                    3, supersedes=first, baseline_commit_sha256=self.fixture.baseline_commit(2)
                ),
            )
        with self.assertRaisesRegex(ControlError, "evaluation_baseline_recorded.kind 非法"):
            self.append("evaluation_baseline_recorded", self.baseline_payload(3, supersedes=first, kind="made-up"))
        self.fixture.open_evaluation_baseline(3)
        self.assertEqual(current_evaluation_baseline(self.store, self.campaign())["baseline"], 3)
        self.assertEqual(self.replay()["result"], "passed")

    def test_baseline_requires_strict_candidate_in_vc5(self) -> None:
        # 历史形态的候选没有严格 VC-5 评估。
        self.fixture.freeze_candidate()
        with self.assertRaisesRegex(ControlError, "评估基线只适用于严格 v2 Candidate"):
            self.append(
                "evaluation_baseline_recorded",
                self.baseline_payload(
                    1, candidate_id="synthetic-candidate-1", candidate_ref=self.fixture.references["candidate"]
                ),
            )
        # 严格候选已冻结但还没有 ValidationAttempt：VC-5 尚未开始，没有可重评的评估。
        build_input = self.fixture.candidate_build_input("idle")
        build_ref = finalize_candidate_build(self.store, self.campaign_id, build_input)
        candidate = {
            "schema_version": "official-client-validation-candidate/v2",
            "candidate_id": "synthetic-candidate-idle",
            "profile_approval_ref": self.fixture.references["profile-approval"],
            "release_artifact_ref": self.fixture.references["release"],
            "support_envelope_ref": self.fixture.references["support"],
            "source_tree_sha256": "7" * 64,
            "test_tree_sha256": "8" * 64,
            "dependency_lock_sha256": "9" * 64,
            "target_architecture": "linux/amd64",
            "build_id": "synthetic-build-idle",
            "image_digest": build_input["image"]["image_digest"],
            "candidate_purpose": build_input["candidate_purpose"],
            "candidate_build_receipt_ref": build_ref,
            "identity_sha256": "",
        }
        candidate["identity_sha256"] = candidate_identity_sha256(candidate)
        idle = self.append("candidate_frozen", candidate)
        with self.assertRaisesRegex(ControlError, "候选尚未进入 VC-5"):
            self.append(
                "evaluation_baseline_recorded",
                self.baseline_payload(1, candidate_id="synthetic-candidate-idle", candidate_ref=idle),
            )
        with self.assertRaisesRegex(ControlError, "评估基线的 candidate_ref 与 candidate_id 不一致"):
            self.append(
                "evaluation_baseline_recorded",
                self.baseline_payload(1, candidate_id="synthetic-candidate-other", candidate_ref=idle),
            )

    def test_baseline_cannot_open_after_delivery_started(self) -> None:
        self.fixture.strict_validate()
        self.fixture.seal_delivery_plan()
        self.fixture.record_delivery_stage("candidate_active", previous=None)
        with self.assertRaisesRegex(ControlError, "已开始 VC-6 交付：交付开始后不得再开评估基线"):
            self.fixture.open_evaluation_baseline(1)

    def test_delivery_runs_under_current_baseline(self) -> None:
        """b1 重评完成后交付只认 b1 的 Acceptance：b0 的验收不能进交付，交付收据形成于 b1。"""

        self.fixture.strict_validate()
        self.fixture.open_evaluation_baseline(1)
        b1_pair = self.fixture.rerun_pair_under_baseline()
        b1_acceptance = self.fixture.accept_under_baseline([b1_pair])
        # 先试用 b0 的验收开始交付：交付门禁按形成期基线拒绝。
        self.fixture.seal_delivery_plan(delivery_id="delivery-b0")
        with self.assertRaisesRegex(ControlError, "候选交付阶段 引用的 Acceptance 形成于候选 revision r1、评估基线 None"):
            self.fixture.record_delivery_stage("candidate_active", previous=None, delivery_id="delivery-b0")
        self.fixture.references["acceptance-strict"] = b1_acceptance
        self.fixture.references["validation-completed"] = self.fixture.references["validation-completed-strict-b1"]
        self.fixture.seal_delivery_plan()
        refs = self.fixture.record_delivery_run(
            ["candidate_active", "rollback_verified", "candidate_restored", "stable_observed"]
        )
        with self.assertRaisesRegex(ControlError, "已开始 VC-6 交付"):
            self.fixture.open_evaluation_baseline(2)
        receipt = self.store.load_receipt(
            finalize_candidate_delivery(
                self.store, self.campaign_id, self.fixture.seal_delivery_package(refs)
            )
        )
        self.assertEqual(receipt["formed_under"]["evaluation_baseline"], self.baseline(1))
        self.assertEqual(self.status()["checkpoint"], "ready_for_operator_release")
        self.assertEqual(self.replay()["result"], "passed")

    def test_baselines_in_one_revision_belong_to_one_candidate(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.fixture.strict_validate(finalize=False, tag="second")
        self.fixture.open_evaluation_baseline(1)
        with self.assertRaisesRegex(ControlError, "同一候选 revision 内的评估基线必须属于同一候选"):
            self.fixture.open_evaluation_baseline(2, tag="second")

    def test_revision_switch_resets_to_b0(self) -> None:
        self.fixture.strict_validate()
        self.fixture.open_evaluation_baseline(1)
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        self.assertIsNone(current_evaluation_baseline(self.store, self.campaign()))
        self.fixture.strict_validate(tag="r2")
        status = self.status()
        self.assertEqual(
            (status["checkpoint"], status["candidate_revision"], status["evaluation_baseline"]),
            ("ready", 2, None),
        )
        self.assertEqual(self.replay()["result"], "passed")

    def test_cli_records_baseline(self) -> None:
        self.fixture.strict_validate(finalize=False)
        path = Path(self.temporary.name) / "baseline.json"
        path.write_bytes(canonical_json_bytes(self.baseline_payload(1)))
        reference = execute(
            build_parser().parse_args(
                [
                    "evaluation-baseline-record",
                    "--store",
                    str(self.fixture.store_root),
                    "--campaign",
                    self.campaign_id,
                    "--input",
                    str(path),
                    "--issued-at",
                    self.fixture._time(),
                ]
            )
        )
        self.assertEqual(self.store.load_fact(reference)["fact_kind"], "evaluation_baseline_recorded")
        self.assertEqual(self.status()["evaluation_baseline"], self.baseline(1))


class BaselineReplayTests(_BaselineCase):
    def test_replay_rejects_unreachable_baseline(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.fixture.open_evaluation_baseline(1)
        self.write_raw_fact(
            "runtime_selector",
            "selector_observed",
            {
                "catalog_snapshot_ref": self.fixture.references["validation-runtime-before"],
                "observation_kind": "read_only",
            },
            {
                "tool_identity_ref": None,
                "candidate_revision": 1,
                "evaluation_baseline": {"baseline": 5, "baseline_commit_sha256": canonical_sha256("b5")},
            },
        )
        with self.assertRaisesRegex(ControlError, "形成期评估基线 b5（r1）不可达.*评估基线事实由 B4-04 定义"):
            self.replay()

    def test_replay_rejects_baseline_regression_within_chain(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.fixture.open_evaluation_baseline(1)
        self.fixture.rerun_pair_under_baseline()
        # 开 b1 之后的 validation 链里再出现形成于 b0 的事实：同一 revision 内基线回退。
        self.write_raw_fact(
            "validation",
            "scenario_prepared",
            {
                "candidate_id": STRICT,
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


if __name__ == "__main__":
    unittest.main()
