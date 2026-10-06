"""Claude 升级 B4-07：交付四阶段、交付恢复段 dr<k> 与经批准的阶段失效的 Store 侧验收用例。

设计验收逐条对应（测试名前缀即编号）：
① 第三阶段失败、上游未变 → dr1（anchor_kind=failure）第三阶段引用第二阶段 pass、第四阶段引用本段第三阶段 → 四阶段
  pass，收据取有效链最终 pass 并记录失败事实与段预约；
② 四阶段曾全部 pass 且未 finalize，批准第二阶段失效 → dr1（anchor_kind=approved_invalidation）覆盖二、三、四阶段，
  旧三、四 pass 失效；
③ 已 finalize 的 delivery 申请阶段失效被拒（预约恢复段、追加交付事实同样被拒）；
④ 无批准对象直接重跑已成功阶段被拒；
⑤ finalize 重复调用返回原收据（含工具演进之后），换交付包重签被拒。

门禁 ①～⑧ 另有逐条负例（``SegmentFactGateTests``、``ReservationGateTests``、``StageInvalidationGateTests``），重放对
伪造的段预约与失效对象的检测、v1 Campaign 拒绝与 CLI 见其余用例。
"""

from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.cli import build_parser, execute
from tools.official_client_control.contracts import (
    CANDIDATE_DELIVERY_RECEIPT_V2_SCHEMA,
    CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA,
    OBJECT_SCHEMA,
    invalidation_review_sha256,
)
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    build_candidate_delivery_receipt,
    delivery_chain_report,
    delivery_chain_state,
    finalize_candidate_delivery,
    finalize_candidate_delivery_outcome,
    replay_receipt,
)
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

PROBE = "official_client_capture/claude_upgrade_b407_probe.json"
ALL_STAGES = ["candidate_active", "rollback_verified", "candidate_restored", "stable_observed"]
MISSING_INVALIDATION = {"object_kind": "candidate_delivery_stage_invalidation", "sha256": "0" * 64}
MISSING_RECOVERY = {"object_kind": "candidate_delivery_recovery", "sha256": "0" * 64}


class _DeliveryCase(unittest.TestCase):
    """每个测试在受管树副本上建一个 VC-5 已完成（ready）的 Campaign v2。"""

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
        self.fixture.strict_validate()
        self.store = self.fixture.store
        self.campaign_id = self.fixture.campaign_id

    def tearDown(self) -> None:
        self._active.__exit__(None, None, None)
        self.tree.restore()
        self.temporary.cleanup()

    def status(self) -> dict[str, Any]:
        return WorkflowGates(self.store).status(self.campaign_id)

    def replay(self) -> dict[str, Any]:
        return self.store.replay(external_root=self.fixture.external_root, require_external=True)

    def state(self) -> Any:
        return delivery_chain_state(self.store, self.campaign_id, "delivery-strict-1")

    def first_round(self, fail_stage: str | None = None) -> list[dict[str, Any]]:
        """首轮交付（dr0，交付事实 v2）：按四阶段推进，``fail_stage`` 处失败并停止。"""

        self.fixture.seal_delivery_plan()
        return self.fixture.record_delivery_run(ALL_STAGES, fail_stage=fail_stage)

    def write_raw_object(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """绕过门禁直接落盘一个形状合法的对象，模拟被篡改的 Store。"""

        content = canonical_json_bytes(
            {"schema_version": OBJECT_SCHEMA, "object_kind": kind, "payload": payload}
        )
        digest = sha256_bytes(content)
        write_once(self.store.root / "objects" / kind / f"{digest}.json", content)
        return {"object_kind": kind, "sha256": digest}


class DeliveryRecoveryAcceptanceTests(_DeliveryCase):
    def test_1_failure_anchor_resumes_from_failed_stage(self) -> None:
        # 首轮交付沿用原夹具（交付事实 v1 形状）：第三阶段失败。
        self.fixture.strict_deliver(fail_stage="candidate_restored")
        s1, s2, s3 = self.fixture.references["delivery-fact-refs"]
        self.assertEqual(self.fixture.seal_delivery_plan(), self.fixture.references["delivery-plan"])
        self.assertEqual(self.store.load_fact(s3)["payload"]["result"], "failed")
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="failure",
            anchor_ref=s3,
            rerun_from_stage="candidate_restored",
            previous_valid_stage_ref=s2,
        )
        s3b, s4b = self.fixture.record_delivery_run(
            ["candidate_restored", "stable_observed"], previous=s2, recovery=(1, r1)
        )
        self.assertEqual(self.store.load_fact(s3b)["payload"]["previous_stage_ref"], s2)
        self.assertEqual(self.store.load_fact(s4b)["payload"]["previous_stage_ref"], s3b)
        self.assertEqual(self.state().valid_refs(self.store), [s1, s2, s3b, s4b])
        package = self.fixture.seal_delivery_package([s1, s2, s3b, s4b])
        reference = finalize_candidate_delivery(self.store, self.campaign_id, package)
        receipt = self.store.load_receipt(reference)
        self.assertEqual(receipt["schema_version"], CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA)
        self.assertEqual(receipt["delivery_fact_refs"], [s1, s2, s3b, s4b])
        self.assertEqual(
            (receipt["delivery_failure_refs"], receipt["stage_invalidation_refs"], receipt["recovery_refs"]),
            ([s3], [], [r1]),
        )
        status = self.status()
        self.assertEqual(
            (status["checkpoint"], status["delivery_state"]),
            ("ready_for_operator_release", "ready_for_operator_release"),
        )
        replay_receipt(self.store, reference)
        # 有恢复段的 delivery 不能按 B1-12 时期的 v2 合同出收据（v2 没有记录恢复输入的字段）。
        with self.assertRaisesRegex(ControlError, "候选交付收据 v2 不能承载交付恢复段"):
            build_candidate_delivery_receipt(
                self.store, self.campaign_id, package, schema=CANDIDATE_DELIVERY_RECEIPT_V2_SCHEMA
            )
        self.assertEqual(self.replay()["result"], "passed")

    def test_2_approved_invalidation_reruns_downstream_stages(self) -> None:
        s1, s2, s3, s4 = self.first_round()
        invalidation = self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        # 已批准但未消费的阶段失效截断有效链：旧四阶段不能封包。
        with self.assertRaisesRegex(ControlError, "只能在有效链四阶段全部通过、且没有待消费的阶段失效时封存"):
            self.fixture.seal_delivery_package([s1, s2, s3, s4], package_id="delivery-package-stale")
        self.assertIsNotNone(self.state().pending)
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="approved_invalidation",
            anchor_ref=invalidation,
            rerun_from_stage="rollback_verified",
            previous_valid_stage_ref=s1,
        )
        s2b, s3b, s4b = self.fixture.record_delivery_run(
            ["rollback_verified", "candidate_restored", "stable_observed"], previous=s1, recovery=(1, r1)
        )
        # 旧三、四阶段的 pass 已失效：混用旧事实或沿用旧链都不能封包。
        with self.assertRaisesRegex(ControlError, "没有沿 previous_stage_ref 衔接成一条链"):
            self.fixture.seal_delivery_package([s1, s2b, s3, s4], package_id="delivery-package-mixed")
        with self.assertRaisesRegex(ControlError, "必须绑定有效链四阶段的最终 pass"):
            self.fixture.seal_delivery_package([s1, s2, s3, s4], package_id="delivery-package-old")
        state = self.state()
        self.assertEqual(state.valid_refs(self.store), [s1, s2b, s3b, s4b])
        self.assertIsNone(state.pending)
        package = self.fixture.seal_delivery_package([s1, s2b, s3b, s4b])
        receipt = self.store.load_receipt(
            finalize_candidate_delivery(self.store, self.campaign_id, package)
        )
        self.assertEqual(
            (receipt["delivery_failure_refs"], receipt["stage_invalidation_refs"], receipt["recovery_refs"]),
            ([], [invalidation], [r1]),
        )
        self.assertEqual(self.status()["checkpoint"], "ready_for_operator_release")
        self.assertEqual(self.replay()["result"], "passed")

    def test_3_finalized_delivery_rejects_stage_invalidation(self) -> None:
        self.fixture.strict_deliver(fail_stage="candidate_restored")
        s1, s2, s3 = self.fixture.references["delivery-fact-refs"]
        self.fixture.seal_delivery_plan()
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="failure",
            anchor_ref=s3,
            rerun_from_stage="candidate_restored",
            previous_valid_stage_ref=s2,
        )
        s3b, s4b = self.fixture.record_delivery_run(
            ["candidate_restored", "stable_observed"], previous=s2, recovery=(1, r1)
        )
        finalize_candidate_delivery(
            self.store, self.campaign_id, self.fixture.seal_delivery_package([s1, s2, s3b, s4b])
        )
        with self.assertRaisesRegex(ControlError, "③ 候选交付 delivery-strict-1 已 finalize.*不得申请阶段失效"):
            self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        with self.assertRaisesRegex(ControlError, "已 finalize.*不得预约恢复段"):
            self.fixture.reserve_delivery_recovery(
                2,
                anchor_kind="failure",
                anchor_ref=s3,
                rerun_from_stage="candidate_restored",
                previous_valid_stage_ref=s2,
            )
        # 有效链四阶段都已通过，最新一段已到 stable_observed：任何新的交付事实都已无处衔接（终态）。
        with self.assertRaisesRegex(ControlError, "候选交付阶段不得覆盖"):
            self.fixture.record_delivery_stage("stable_observed", previous=s3b, recovery=(1, r1))
        self.assertEqual(self.status()["checkpoint"], "ready_for_operator_release")
        self.assertEqual(self.replay()["result"], "passed")

    def test_4_rerunning_a_passed_stage_requires_approval(self) -> None:
        s1, s2, _s3, _s4 = self.first_round()
        with self.assertRaisesRegex(ControlError, "failure 锚点必须是同一 delivery 在 rerun_from_stage 的失败阶段事实"):
            self.fixture.reserve_delivery_recovery(
                1,
                anchor_kind="failure",
                anchor_ref=s2,
                rerun_from_stage="rollback_verified",
                previous_valid_stage_ref=s1,
            )
        with self.assertRaisesRegex(ControlError, "对象引用不存在"):
            self.fixture.reserve_delivery_recovery(
                1,
                anchor_kind="approved_invalidation",
                anchor_ref=MISSING_INVALIDATION,
                rerun_from_stage="rollback_verified",
                previous_valid_stage_ref=s1,
            )
        with self.assertRaisesRegex(ControlError, "候选交付阶段不得覆盖"):
            self.fixture.record_delivery_stage("rollback_verified", previous=s1)
        with self.assertRaisesRegex(ControlError, "对象引用不存在"):
            self.fixture.record_delivery_stage(
                "rollback_verified", previous=s1, recovery=(1, MISSING_RECOVERY)
            )
        self.assertEqual(self.replay()["result"], "passed")

    def test_5_finalize_twice_returns_original_receipt(self) -> None:
        s1, s2, s3 = self.first_round(fail_stage="candidate_restored")
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="failure",
            anchor_ref=s3,
            rerun_from_stage="candidate_restored",
            previous_valid_stage_ref=s2,
        )
        s3b, s4b = self.fixture.record_delivery_run(
            ["candidate_restored", "stable_observed"], previous=s2, recovery=(1, r1)
        )
        package = self.fixture.seal_delivery_package([s1, s2, s3b, s4b])
        first = finalize_candidate_delivery_outcome(self.store, self.campaign_id, package)
        self.assertFalse(first["reused"])
        self.assertEqual(
            finalize_candidate_delivery_outcome(self.store, self.campaign_id, package),
            {"receipt_ref": first["receipt_ref"], "reused": True},
        )
        self.assertEqual(
            finalize_candidate_delivery(self.store, self.campaign_id, package), first["receipt_ref"]
        )
        # 工具演进之后再 finalize：终态收据按形成期重放后原样返回，不重新签发。
        self.tree.write(PROBE, b'{"probe": "b407"}\n')
        self.fixture.register_evolution()
        self.assertEqual(
            finalize_candidate_delivery_outcome(self.store, self.campaign_id, package),
            {"receipt_ref": first["receipt_ref"], "reused": True},
        )
        # 换一个交付包为已 finalize 的 delivery 重签：拒绝。
        other = self.fixture.seal_delivery_package([s1, s2, s3b, s4b], package_id="delivery-package-again")
        with self.assertRaisesRegex(ControlError, "已 finalize.*不得以其他交付包重新签发"):
            finalize_candidate_delivery(self.store, self.campaign_id, other)
        self.assertEqual(len(self.store.list_receipt_refs("candidate_delivery")), 1)
        report = delivery_chain_report(self.store, self.campaign_id, "delivery-strict-1")
        self.assertEqual(
            (report["receipt_refs"], report["failure_refs"], report["recovery_refs"], report["complete"]),
            ([first["receipt_ref"]], [s3], [r1], True),
        )
        # CLI：--allow-existing 输出 {receipt_ref, reused}。
        path = Path(self.temporary.name) / "package-ref.json"
        path.write_bytes(canonical_json_bytes(package))
        result = execute(
            build_parser().parse_args(
                [
                    "candidate-delivery-finalize",
                    "--store",
                    str(self.fixture.store_root),
                    "--campaign",
                    self.campaign_id,
                    "--package-ref",
                    str(path),
                    "--allow-existing",
                ]
            )
        )
        self.assertEqual(result, {"receipt_ref": first["receipt_ref"], "reused": True})
        self.assertEqual(self.replay()["result"], "passed")


class SegmentFactGateTests(_DeliveryCase):
    def test_segment_fact_gates(self) -> None:
        s1, s2, s3 = self.first_round(fail_stage="candidate_restored")
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="failure",
            anchor_ref=s3,
            rerun_from_stage="candidate_restored",
            previous_valid_stage_ref=s2,
        )
        record = self.fixture.record_delivery_stage
        with self.assertRaisesRegex(ControlError, "① dr1 的事实阶段 rollback_verified 不在段预约的 stages_to_rerun 内"):
            record("rollback_verified", previous=s1, recovery=(1, r1))
        with self.assertRaisesRegex(ControlError, "① dr2 的 recovery_ref 与 delivery、段编号或交付计划不一致"):
            record("candidate_restored", previous=s2, recovery=(2, r1))
        with self.assertRaisesRegex(ControlError, "② dr1 首个重跑阶段 candidate_restored 的 previous_stage_ref"):
            record("candidate_restored", previous=s1, recovery=(1, r1))
        with self.assertRaisesRegex(ControlError, "⑤ 交付恢复段 dr1 的 environment_sha256 与冻结计划不一致.*delivery_id"):
            record(
                "candidate_restored",
                previous=s2,
                recovery=(1, r1),
                overrides={"environment_sha256": "7" * 64},
            )
        s3b = record("candidate_restored", previous=s2, recovery=(1, r1))
        with self.assertRaisesRegex(ControlError, "③ dr1 的 stable_observed 必须衔接本段直接前一阶段的 pass"):
            record("stable_observed", previous=s3, recovery=(1, r1))
        with self.assertRaisesRegex(ControlError, "stable_observed 未达到冻结观察窗"):
            record("stable_observed", previous=s3b, recovery=(1, r1), observed_seconds=10)
        with self.assertRaisesRegex(ControlError, "候选交付阶段不得覆盖"):
            record("candidate_restored", previous=s2, recovery=(1, r1))
        # 首轮交付已被恢复段取代：再推进首轮（衔接失败阶段）照原文案被拒。
        with self.assertRaisesRegex(ControlError, "失败的候选交付阶段不得继续推进"):
            record("stable_observed", previous=s3)
        record("stable_observed", previous=s3b, recovery=(1, r1))
        self.assertTrue(self.state().complete())
        self.assertEqual(self.replay()["result"], "passed")


class ReservationGateTests(_DeliveryCase):
    def test_reservation_gates(self) -> None:
        self.fixture.seal_delivery_plan()
        s1, s2 = self.fixture.record_delivery_run(["candidate_active", "rollback_verified"])
        # ⑦ 首轮交付仍在推进时不能批准阶段失效，也就没有可锚定的失效。
        with self.assertRaisesRegex(ControlError, "阶段失效只能在最新交付段收口.*首轮交付仍在推进"):
            self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        (s3,) = self.fixture.record_delivery_run(
            ["candidate_restored"], previous=s2, fail_stage="candidate_restored"
        )
        reserve = self.fixture.reserve_delivery_recovery
        failure = {
            "anchor_kind": "failure",
            "anchor_ref": s3,
            "rerun_from_stage": "candidate_restored",
            "previous_valid_stage_ref": s2,
        }
        with self.assertRaisesRegex(ControlError, "恢复段编号必须接续：下一段是 dr1"):
            reserve(2, **failure)
        with self.assertRaisesRegex(ControlError, "stages_to_rerun 必须从 rerun_from_stage 覆盖到 stable_observed"):
            reserve(1, **failure, stages_to_rerun=["candidate_restored"])
        with self.assertRaisesRegex(ControlError, "⑤ 交付恢复段的环境身份 network_sha256 与冻结计划不一致.*delivery_id"):
            reserve(1, **failure, environment_overrides={"network_sha256": "7" * 64})
        with self.assertRaisesRegex(ControlError, "② previous_valid_stage_ref 必须是同一 delivery 在 rerun_from_stage 前一阶段的 pass"):
            reserve(1, **dict(failure, previous_valid_stage_ref=s1))
        with self.assertRaisesRegex(ControlError, "failure 锚点必须是同一 delivery 在 rerun_from_stage 的失败阶段事实"):
            reserve(1, **dict(failure, rerun_from_stage="rollback_verified", previous_valid_stage_ref=s1))
        r1 = reserve(1, **failure)
        (s3b,) = self.fixture.record_delivery_run(["candidate_restored"], previous=s2, recovery=(1, r1))
        # ⑦ dr1 未收口：不得再预约 dr2。
        with self.assertRaisesRegex(ControlError, "⑦ dr1尚未收口"):
            reserve(2, **failure)
        (s4b,) = self.fixture.record_delivery_run(
            ["stable_observed"], previous=s3b, recovery=(1, r1), fail_stage="stable_observed"
        )
        # 收口后的 failure 锚点只能是最新一段收口的失败事实；run_nonce 不复用。
        with self.assertRaisesRegex(ControlError, "failure 锚点必须是最新交付段收口时的失败事实"):
            reserve(2, **failure)
        stable_failure = {
            "anchor_kind": "failure",
            "anchor_ref": s4b,
            "rerun_from_stage": "stable_observed",
            "previous_valid_stage_ref": s3b,
        }
        payload = self.fixture.delivery_recovery_payload(2, **stable_failure)
        payload["run_nonce"] = self.store.load_object(r1)["payload"]["run_nonce"]
        with self.assertRaisesRegex(ControlError, "run_nonce 不得复用"):
            self.store.seal_object("candidate_delivery_recovery", payload)
        # ④／⑧ 有待消费的阶段失效时，下一段必须以 approved_invalidation 锚定它。
        invalidation = self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        with self.assertRaisesRegex(ControlError, "④ 存在已批准但未消费的阶段失效"):
            reserve(2, **stable_failure)
        with self.assertRaisesRegex(ControlError, "⑧ approved_invalidation 锚点必须是同一 delivery 对 rerun_from_stage 的已批准阶段失效"):
            reserve(
                2,
                anchor_kind="approved_invalidation",
                anchor_ref=invalidation,
                rerun_from_stage="candidate_restored",
                previous_valid_stage_ref=s2,
            )
        r2 = reserve(
            2,
            anchor_kind="approved_invalidation",
            anchor_ref=invalidation,
            rerun_from_stage="rollback_verified",
            previous_valid_stage_ref=s1,
        )
        tail = self.fixture.record_delivery_run(
            ["rollback_verified", "candidate_restored", "stable_observed"], previous=s1, recovery=(2, r2)
        )
        package = self.fixture.seal_delivery_package([s1, *tail])
        receipt = self.store.load_receipt(
            finalize_candidate_delivery(self.store, self.campaign_id, package)
        )
        self.assertEqual(
            (receipt["delivery_failure_refs"], receipt["stage_invalidation_refs"], receipt["recovery_refs"]),
            ([s3, s4b], [invalidation], [r1, r2]),
        )
        self.assertEqual(self.replay()["result"], "passed")


class StageInvalidationGateTests(_DeliveryCase):
    def test_stage_invalidation_gates(self) -> None:
        s1, s2, s3, s4 = self.first_round()
        mismatched = self.fixture.delivery_stage_invalidation_payload("candidate_restored", s2)
        with self.assertRaisesRegex(ControlError, "⑧ 阶段失效指向的事实不是同一 delivery 的同一阶段"):
            self.store.seal_object("candidate_delivery_stage_invalidation", mismatched)
        tampered = self.fixture.delivery_stage_invalidation_payload("rollback_verified", s2)
        tampered["reason"] = "批准之后改过的理由"
        with self.assertRaisesRegex(ControlError, "review_sha256 与审核内容不一致"):
            self.store.seal_object("candidate_delivery_stage_invalidation", tampered)
        invalidation = self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        with self.assertRaisesRegex(ControlError, "同一 delivery 已有待消费的阶段失效"):
            self.fixture.invalidate_delivery_stage("candidate_restored", s3)
        r1 = self.fixture.reserve_delivery_recovery(
            1,
            anchor_kind="approved_invalidation",
            anchor_ref=invalidation,
            rerun_from_stage="rollback_verified",
            previous_valid_stage_ref=s1,
        )
        (s2b,) = self.fixture.record_delivery_run(["rollback_verified"], previous=s1, recovery=(1, r1))
        with self.assertRaisesRegex(ControlError, "阶段失效只能在最新交付段收口.*dr1仍在推进"):
            self.fixture.invalidate_delivery_stage("rollback_verified", s2b)
        s3b, s4b = self.fixture.record_delivery_run(
            ["candidate_restored", "stable_observed"], previous=s2b, recovery=(1, r1)
        )
        # 已被恢复段取代的旧 pass 不在当前有效链上，不能再失效。
        with self.assertRaisesRegex(ControlError, "被失效的阶段事实不在当前有效链上"):
            self.fixture.invalidate_delivery_stage("candidate_restored", s3)
        # 失败阶段只能以 failure 锚点续跑，不能「失效」。
        self.fixture.seal_delivery_plan(delivery_id="delivery-strict-2")
        failed = self.fixture.record_delivery_run(
            ["candidate_active"], delivery_id="delivery-strict-2", fail_stage="candidate_active"
        )
        with self.assertRaisesRegex(ControlError, "⑧ 只能失效已通过的阶段"):
            self.fixture.invalidate_delivery_stage("candidate_active", failed[0], delivery_id="delivery-strict-2")
        self.assertEqual(self.state().valid_refs(self.store), [s1, s2b, s3b, s4b])
        self.assertEqual(self.replay()["result"], "passed")


class DeliveryReplayTests(_DeliveryCase):
    def test_replay_rejects_duplicate_segment_reservation(self) -> None:
        s1, s2, s3 = self.first_round(fail_stage="candidate_restored")
        failure = {
            "anchor_kind": "failure",
            "anchor_ref": s3,
            "rerun_from_stage": "candidate_restored",
            "previous_valid_stage_ref": s2,
        }
        self.fixture.reserve_delivery_recovery(1, **failure)
        forged = self.fixture.delivery_recovery_payload(1, **failure)
        forged["run_nonce"] = "forged-run"
        self.write_raw_object("candidate_delivery_recovery", forged)
        with self.assertRaisesRegex(ControlError, "恢复段 dr1 存在多份预约"):
            self.replay()

    def test_replay_rejects_two_pending_invalidations(self) -> None:
        s1, s2, s3, s4 = self.first_round()
        self.fixture.invalidate_delivery_stage("rollback_verified", s2)
        forged = self.fixture.delivery_stage_invalidation_payload("candidate_restored", s3)
        self.write_raw_object("candidate_delivery_stage_invalidation", forged)
        with self.assertRaisesRegex(ControlError, "至多一份待消费的阶段失效"):
            self.replay()

    def test_legacy_v2_receipt_replays_but_cannot_be_written(self) -> None:
        """B1-12 时期的候选交付收据 v2 按自身合同只读重放；新写入一律是 v3。"""

        s1, s2, s3, s4 = self.first_round()
        package = self.fixture.seal_delivery_package([s1, s2, s3, s4])
        legacy = build_candidate_delivery_receipt(
            self.store, self.campaign_id, package, schema=CANDIDATE_DELIVERY_RECEIPT_V2_SCHEMA
        )
        self.assertNotIn("recovery_refs", legacy)
        with self.assertRaisesRegex(ControlError, "候选交付收据 v2 只读重放"):
            self.store.write_receipt("candidate_delivery", legacy)
        # 模拟 B1-12 时期已落盘的 v2 收据：按 v2 合同重建、整库重放通过，status 照常认它。
        content = canonical_json_bytes(legacy)
        digest = sha256_bytes(content)
        write_once(self.store.root / "receipts" / "candidate_delivery" / f"{digest}.json", content)
        reference = {"receipt_kind": "candidate_delivery", "sha256": digest}
        self.assertEqual(replay_receipt(self.store, reference), legacy)
        self.assertEqual(self.status()["checkpoint"], "ready_for_operator_release")
        self.assertEqual(self.replay()["result"], "passed")
        # 已 finalize（有收据）的 delivery 同样不得再申请阶段失效。
        with self.assertRaisesRegex(ControlError, "已 finalize"):
            self.fixture.invalidate_delivery_stage("rollback_verified", s2)

    def test_v1_campaign_has_no_delivery_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = SyntheticCampaign(Path(directory))
            legacy.bootstrap_and_campaign()
            legacy.discovery_and_evidence()
            legacy.profile_objects()
            legacy.profile_approve()
            legacy.strict_validate()
            legacy.strict_deliver(fail_stage="candidate_restored")
            _s1, s2, s3 = legacy.references["delivery-fact-refs"]
            legacy.references["delivery-plan-delivery-strict-1"] = legacy.references["delivery-plan"]
            with self.assertRaisesRegex(ControlError, "v1 Campaign 不支持交付恢复段"):
                legacy.reserve_delivery_recovery(
                    1,
                    anchor_kind="failure",
                    anchor_ref=s3,
                    rerun_from_stage="candidate_restored",
                    previous_valid_stage_ref=s2,
                )
            with self.assertRaisesRegex(ControlError, "v1 Campaign 不支持交付阶段失效"):
                legacy.invalidate_delivery_stage("rollback_verified", s2)
            # v1 事实遇到交付事实 v2 按 v1 合同拒绝（多出的字段）。
            payload = legacy.delivery_stage_payload("candidate_restored", previous=s2)
            with self.assertRaisesRegex(ControlError, "字段不闭合"):
                legacy.store.append_fact(
                    legacy.campaign_id, "candidate_delivery_recorded", payload, legacy._time()
                )


class RecoverySchemaTests(unittest.TestCase):
    """机器 Schema 与 B3-05／B4-04／B4-07 运行时合同的一致性（新增在既有 schema 文件的 $defs 里，文件数不变）。"""

    def setUp(self) -> None:
        import json

        from tools.official_client_control import contracts

        self.contracts = contracts
        root = Path(contracts.__file__).with_name("schemas")
        self.schemas = {
            name: json.loads((root / f"{name}.schema.json").read_text(encoding="utf-8"))
            for name in ("object", "fact-v2", "candidate-delivery", "receipt", "scenario-pair", "validation-workflow")
        }

    def test_object_kinds_and_fact_kinds_match_runtime(self) -> None:
        contracts = self.contracts
        self.assertEqual(
            set(self.schemas["object"]["properties"]["object_kind"]["enum"]), set(contracts.OBJECT_KINDS)
        )
        self.assertEqual(
            set(self.schemas["fact-v2"]["properties"]["fact_kind"]["enum"]), set(contracts.FACT_DIMENSION)
        )
        definitions = self.schemas["object"]["$defs"]
        self.assertEqual(
            set(definitions["candidateInvalidation"]["required"]),
            {"campaign_id", "candidate_id", "revision", "revision_ref", "reason", "evidence_refs",
             *contracts.INVALIDATION_APPROVAL_FIELDS},
        )

    def test_new_contract_versions_match_runtime(self) -> None:
        contracts = self.contracts
        delivery = self.schemas["candidate-delivery"]["$defs"]
        self.assertEqual(
            delivery["factV2"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_DELIVERY_FACT_V2_SCHEMA,
        )
        self.assertEqual(
            delivery["recovery"]["properties"]["recovery_revision"]["$ref"], "#/$defs/recoveryRevision"
        )
        self.assertEqual(
            self.schemas["receipt"]["$defs"]["candidateDeliveryV3"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA,
        )
        self.assertEqual(
            self.schemas["scenario-pair"]["oneOf"][-1]["properties"]["schema_version"]["const"],
            contracts.PAIR_V3_SCHEMA,
        )
        workflow = self.schemas["validation-workflow"]["$defs"]
        self.assertEqual(
            workflow["acceptanceV3"]["properties"]["schema_version"]["const"], contracts.ACCEPTANCE_V3_SCHEMA
        )
        self.assertEqual(
            workflow["evaluationBaseline"]["properties"]["kind"]["enum"],
            list(contracts.EVALUATION_BASELINE_KINDS),
        )


class DeliveryCliTests(_DeliveryCase):
    def run_cli(self, argv: list[str]) -> dict[str, Any]:
        return execute(build_parser().parse_args(argv))

    def chain(self) -> dict[str, Any]:
        return self.run_cli(
            [
                "delivery-chain",
                "--store",
                str(self.fixture.store_root),
                "--campaign",
                self.campaign_id,
                "--delivery-id",
                "delivery-strict-1",
            ]
        )

    def test_cli_invalidate_and_reserve(self) -> None:
        s1, s2, s3, s4 = self.first_round()
        report = self.chain()
        self.assertEqual(report["schema_version"], "official-client-control-delivery-chain/v1")
        self.assertEqual(
            (report["latest_recovery_revision"], report["latest_closed"], report["complete"], report["terminal_ref"]),
            (None, True, True, s4),
        )
        self.assertEqual(list(report["valid_stage_refs"].values()), [s1, s2, s3, s4])
        work = Path(self.temporary.name)
        payload = self.fixture.delivery_stage_invalidation_payload("rollback_verified", s2)
        path = work / "stage-invalidation.json"
        path.write_bytes(canonical_json_bytes(payload))
        base = [
            "delivery-stage-invalidate",
            "--store",
            str(self.fixture.store_root),
            "--campaign",
            self.campaign_id,
            "--input",
            str(path),
        ]
        preview = self.run_cli([*base, "--preview"])
        self.assertEqual(preview["review_sha256"], invalidation_review_sha256(payload))
        invalidation = self.run_cli([*base, "--approve-sha256", preview["review_sha256"]])["object_ref"]
        # 待消费的失效截断有效链：编排器据此以 dr1、approved_invalidation、previous_valid=s1 预约恢复段。
        report = self.chain()
        self.assertEqual(
            (report["pending_invalidation_ref"], report["complete"], report["next_recovery_revision"]),
            (invalidation, False, "dr1"),
        )
        self.assertEqual(report["valid_stage_refs"], {"candidate_active": s1})
        reservation = self.fixture.delivery_recovery_payload(
            1,
            anchor_kind="approved_invalidation",
            anchor_ref=invalidation,
            rerun_from_stage="rollback_verified",
            previous_valid_stage_ref=s1,
        )
        reservation_path = work / "recovery.json"
        reservation_path.write_bytes(canonical_json_bytes(reservation))
        argv = [
            "delivery-recovery-reserve",
            "--store",
            str(self.fixture.store_root),
            "--campaign",
            self.campaign_id,
            "--input",
            str(reservation_path),
        ]
        reserved = self.run_cli(argv)["object_ref"]
        self.assertEqual(self.run_cli([*argv, "--allow-existing"]), {"object_ref": reserved, "reused": True})
        report = self.chain()
        self.assertEqual(
            (report["latest_recovery_revision"], report["latest_closed"], report["pending_invalidation_ref"]),
            ("dr1", False, None),
        )
        self.assertEqual(
            (report["consumed_invalidation_refs"], report["recovery_refs"], report["receipt_refs"]),
            ([invalidation], [reserved], []),
        )
        changed = deepcopy(reservation)
        changed["campaign_id"] = "other-campaign"
        reservation_path.write_bytes(canonical_json_bytes(changed))
        with self.assertRaisesRegex(ControlError, "campaign_id 与 --campaign 不一致"):
            self.run_cli(argv)


if __name__ == "__main__":
    unittest.main()
