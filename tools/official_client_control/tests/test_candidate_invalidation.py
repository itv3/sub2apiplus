"""Claude 升级 B3-05：候选 revision 与旧候选失效门禁的 Store 侧验收用例。

设计验收逐条对应：
* 作废后 ``pair-record`` 被拒——候选级事实、绑定候选的收据与执行计划一律被拒，直到新 revision 事实以
  ``invalidation_ref`` 引用作废对象（revision_required）；
* 旧 revision 的 PAIR 重放通过但不进交付——不进新 revision 的 Acceptance，旧 Acceptance 不能推进 VC-5 完成，
  status 不计入；
* 新 revision 的验证从新 attempt 完整执行——旧 attempt、旧执行计划与旧 revision 的门禁收据都不能沿用。

另覆盖：作废对象的门禁（审核摘要、revision 可达、只作废一次、只作废当前 revision、已验收候选不得作废）、revision
事实的 ``invalidation_ref`` 门禁、重放对「作废后不引用」的检测、status 的 revision_required 标记与 CLI 两步式入口。
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_bytes,
    write_once,
)
from tools.official_client_control.cli import build_parser, execute, main
from tools.official_client_control.contracts import invalidation_review_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.gates import WorkflowGates
from tools.official_client_control.receipts import (
    current_candidate_revision,
    finalize_candidate_build,
    finalize_validation,
    replay_receipt,
    revision_invalidation,
)
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

STRICT = "synthetic-candidate-strict"


class _RevisionCase(unittest.TestCase):
    """每个测试类复制一次受管树；每个测试在副本上建一个走到 Profile 批准的 Campaign v2。"""

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

    @staticmethod
    def with_identity(payload: dict[str, Any]) -> dict[str, Any]:
        payload["identity_sha256"] = canonical_sha256(
            {key: value for key, value in payload.items() if key != "identity_sha256"}
        )
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


class InvalidationAcceptanceTests(_RevisionCase):
    def test_pair_record_rejected_after_invalidation(self) -> None:
        """验收：作废后 pair-record 被拒；revision_required 期间候选级写入一律被拒，非候选级事实不受影响。"""

        self.fixture.strict_validate(finalize=False)
        pair = self.payload("pair-strict")
        invalidation = self.fixture.invalidate_candidate(STRICT)
        status = self.status()
        self.assertEqual(status["candidate_revision_invalidation"], invalidation)
        self.assertEqual(
            revision_invalidation(self.store, self.campaign(), 1)[0], invalidation
        )
        with self.assertRaisesRegex(ControlError, "pair_recorded 写入：候选 revision r1 已作废"):
            self.append("pair_recorded", pair)
        with self.assertRaisesRegex(ControlError, "candidate_frozen 写入：候选 revision r1 已作废"):
            self.append("candidate_frozen", self.payload("candidate-strict"))
        with self.assertRaisesRegex(ControlError, "validation_completed 写入：候选 revision r1 已作废"):
            finalize_validation(
                self.store,
                self.campaign_id,
                self.fixture.references["acceptance-strict"],
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
            )
        # 绑定候选的收据同样被拒（写入时有效性在 write_receipt 统一核对）。
        with self.assertRaisesRegex(ControlError, "收据写入：候选 revision r1 已作废"):
            finalize_candidate_build(
                self.store, self.campaign_id, self.fixture.candidate_build_input()
            )
        # 作废候选的执行计划也不得再封存。
        plan = deepcopy(
            self.store.load_object(self.fixture.references["validation-plan"])["payload"]
        )
        plan["plan_id"] = "validation-plan-after-invalidation"
        plan["run_conditions"]["run_nonce"] = "nonce-after-invalidation"
        with self.assertRaisesRegex(ControlError, "ValidationExecutionPlan 封存：候选 revision r1 已作废"):
            self.store.seal_object("validation_execution_plan", self.with_identity(plan))
        # 非候选级事实照常写入；整库重放通过（作废之前形成的事实按形成期通过）。
        self.append(
            "selector_observed",
            {
                "catalog_snapshot_ref": self.fixture.references["validation-runtime-before"],
                "observation_kind": "read_only",
            },
        )
        self.assertEqual(self.replay()["result"], "passed")

    def test_old_revision_pair_replays_but_never_enters_delivery(self) -> None:
        """验收：旧 revision 的 PAIR 重放通过但不进交付；新 revision 必须引用作废对象才能打开。"""

        self.fixture.strict_validate(finalize=False)
        old_pair = self.fixture.references["pair-strict"]
        old_acceptance = self.fixture.references["acceptance-strict"]
        invalidation = self.fixture.invalidate_candidate(STRICT)
        with self.assertRaisesRegex(ControlError, "被取代的 r1 已作废.*必须以 invalidation_ref 引用该作废对象"):
            self.fixture.open_candidate_revision("synthetic-candidate-r2")
        self.fixture.open_candidate_revision("synthetic-candidate-r2", invalidation_ref=invalidation)
        self.assertEqual(
            current_candidate_revision(self.store, self.campaign()), (2, "synthetic-candidate-r2")
        )
        status = self.status()
        self.assertEqual(
            (status["checkpoint"], status["candidate_revision"], status["candidate_revision_invalidation"]),
            ("profile_approved", 2, None),
        )
        # 旧 revision 的 PAIR、Acceptance 与收据按形成期坐标重放通过。
        self.assertEqual(self.replay()["result"], "passed")
        self.assertEqual(self.store.load_fact(old_pair)["formed_under"]["candidate_revision"], 1)
        for kind in ("candidate_build", "validation_gate"):
            for reference in self.store.list_receipt_refs(kind):
                replay_receipt(self.store, reference)
        # 旧 Acceptance 不能推进 VC-5 完成（交付的前提）。
        with self.assertRaisesRegex(ControlError, "不属于候选 revision r2"):
            finalize_validation(
                self.store,
                self.campaign_id,
                old_acceptance,
                self.fixture.references["validation-selector-after"],
                self.fixture._time(),
            )
        # 新 revision 从新 attempt 完整执行后才是当前验收。
        self.fixture.strict_validate(tag="r2")
        status = self.status()
        self.assertEqual(
            (status["checkpoint"], status["current_candidate_id"]), ("ready", "synthetic-candidate-r2")
        )
        # 旧 PAIR 进不了新 revision 的 Acceptance。
        acceptance = self.payload("acceptance-r2")
        acceptance["pair_refs"] = [old_pair]
        with self.assertRaisesRegex(ControlError, "PAIR"):
            self.append("acceptance_recorded", self.with_identity(acceptance))
        self.assertEqual(self.replay()["result"], "passed")

    def test_new_revision_cannot_reuse_old_attempt_plan_or_gate_receipts(self) -> None:
        """验收：新 revision 的验证从新 attempt 完整执行——旧执行计划、旧门禁收据都不能沿用。"""

        self.fixture.strict_validate(finalize=False)
        old_plan_ref = self.fixture.references["validation-plan"]
        old_gate = self.fixture.references["validation-gate-receipt"]
        invalidation = self.fixture.invalidate_candidate(STRICT)
        self.fixture.open_candidate_revision("synthetic-candidate-r2", invalidation_ref=invalidation)
        self.fixture.strict_validate(tag="r2")
        # 新候选的 attempt 不能绑定旧候选的执行计划。
        attempt = self.payload("validation-attempt-r2")
        old_plan = self.store.load_object(old_plan_ref)["payload"]
        attempt.update(
            {
                "attempt_id": "attempt-r2-on-old-plan",
                "validation_execution_plan_ref": old_plan_ref,
                "run_conditions": old_plan["run_conditions"],
            }
        )
        with self.assertRaisesRegex(ControlError, "ValidationAttempt 与 ValidationExecutionPlan 的"):
            self.append("validation_attempt_created", self.with_identity(attempt))
        # 新候选的执行计划不能「复用」旧 revision 的门禁收据。
        plan = deepcopy(
            self.store.load_object(self.fixture.references["validation-plan-r2"])["payload"]
        )
        plan["plan_id"] = "validation-plan-r2-reuse"
        plan["run_conditions"]["run_nonce"] = "nonce-r2-reuse"
        plan["execute_item_ids"] = ["baseline"]
        plan["reuse_item_ids"] = ["gate-runtime"]
        plan["external_gates"][0]["source"] = "reuse"
        plan["external_gates"][0]["reused_receipt_ref"] = old_gate
        with self.assertRaisesRegex(ControlError, "复用门禁收据形成于候选 revision r1，不是计划候选的 r2"):
            self.store.seal_object("validation_execution_plan", self.with_identity(plan))
        self.assertEqual(self.replay()["result"], "passed")


class InvalidationGateTests(_RevisionCase):
    def test_invalidation_object_contract_and_single_use(self) -> None:
        self.fixture.strict_validate(finalize=False)
        payload = self.fixture.candidate_invalidation_payload(STRICT)
        tampered = deepcopy(payload)
        tampered["reason"] = "批准之后改过的理由"
        with self.assertRaisesRegex(ControlError, "review_sha256 与审核内容不一致"):
            self.store.seal_object("candidate_invalidation", tampered)
        later = deepcopy(payload)
        later["revision"] = 2
        later["review_sha256"] = invalidation_review_sha256(later)
        with self.assertRaisesRegex(ControlError, "只有隐含 r1 可以不引用 revision 事实"):
            self.store.seal_object("candidate_invalidation", later)
        early = deepcopy(payload)
        early["approved_at_utc"] = "2026-08-17T00:00:00Z"
        with self.assertRaisesRegex(ControlError, "批准时间早于 Campaign 建立时间"):
            self.store.seal_object("candidate_invalidation", early)
        reference = self.store.seal_object("candidate_invalidation", payload)
        second = self.fixture.candidate_invalidation_payload(STRICT, reason="第二次作废")
        with self.assertRaisesRegex(ControlError, "候选 revision r1 只能作废一次"):
            self.store.seal_object("candidate_invalidation", second)
        self.assertEqual(
            self.store.seal_object_outcome("candidate_invalidation", payload, allow_existing=True),
            {"object_ref": reference, "reused": True},
        )
        self.assertEqual(self.replay()["result"], "passed")

    def test_accepted_candidate_cannot_be_invalidated(self) -> None:
        self.fixture.strict_validate()
        with self.assertRaisesRegex(ControlError, "已完成 VC-5 验收（validation_completed），不得作废"):
            self.fixture.invalidate_candidate(STRICT)

    def test_only_current_revision_and_its_candidate(self) -> None:
        self.fixture.strict_validate(finalize=False)
        stale = self.fixture.candidate_invalidation_payload(STRICT)
        self.fixture.open_candidate_revision("synthetic-candidate-r2")
        with self.assertRaisesRegex(ControlError, "只能作废当前候选 revision r2：r1 已不是当前 revision"):
            self.store.seal_object("candidate_invalidation", stale)
        other = self.fixture.candidate_invalidation_payload("synthetic-candidate-other")
        with self.assertRaisesRegex(ControlError, "候选作废与所引 revision 事实的 revision／候选不一致"):
            self.store.seal_object("candidate_invalidation", other)
        self.fixture.invalidate_candidate("synthetic-candidate-r2")
        self.assertIsNotNone(self.status()["candidate_revision_invalidation"])

    def test_explicit_r1_must_be_referenced(self) -> None:
        self.append(
            "candidate_revision_recorded",
            {"revision": 1, "candidate_id": STRICT, "supersedes": None, "invalidation_ref": None},
        )
        payload = self.fixture.candidate_invalidation_payload(STRICT)
        self.assertIsNotNone(payload["revision_ref"])
        implicit = deepcopy(payload)
        implicit["revision_ref"] = None
        implicit["review_sha256"] = invalidation_review_sha256(implicit)
        with self.assertRaisesRegex(ControlError, "r1 已有显式 revision 事实"):
            self.store.seal_object("candidate_invalidation", implicit)
        self.store.seal_object("candidate_invalidation", payload)

    def test_v1_campaign_has_no_candidate_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = SyntheticCampaign(Path(directory))
            legacy.bootstrap_and_campaign()
            legacy.discovery_and_evidence()
            legacy.profile_objects()
            payload = legacy.candidate_invalidation_payload(STRICT)
            with self.assertRaisesRegex(ControlError, "v1 Campaign 不支持候选作废"):
                legacy.store.seal_object("candidate_invalidation", payload)


class RevisionFactInvalidationTests(_RevisionCase):
    def test_invalidation_ref_contract(self) -> None:
        invalidation = self.fixture.invalidate_candidate("synthetic-ghost")
        with self.assertRaisesRegex(ControlError, "r1 不取代任何 revision，不得声明 invalidation_ref"):
            self.append(
                "candidate_revision_recorded",
                {"revision": 1, "candidate_id": "c-1", "supersedes": None, "invalidation_ref": invalidation},
            )
        with self.assertRaisesRegex(ControlError, "invalidation_ref 只能引用 candidate_invalidation"):
            self.append(
                "candidate_revision_recorded",
                {
                    "revision": 2,
                    "candidate_id": "c-2",
                    "supersedes": None,
                    "invalidation_ref": self.fixture.references["operational-evidence"],
                },
            )

    def test_superseding_revision_binds_its_invalidation(self) -> None:
        # 隐含 r1 下作废一个尚未冻结的候选：新 revision 不得复用它的 ID，必须引用该作废对象。
        first = self.fixture.invalidate_candidate("synthetic-ghost")
        with self.assertRaisesRegex(ControlError, "新 revision 的候选不得是被作废的候选"):
            self.fixture.open_candidate_revision("synthetic-ghost", invalidation_ref=first)
        self.fixture.open_candidate_revision("synthetic-candidate-r2", invalidation_ref=first)
        second = self.fixture.invalidate_candidate("synthetic-candidate-r2")
        with self.assertRaisesRegex(ControlError, "invalidation_ref 必须引用被取代 revision r2 的作废对象"):
            self.fixture.open_candidate_revision("synthetic-candidate-r3", invalidation_ref=first)
        self.fixture.open_candidate_revision("synthetic-candidate-r3", invalidation_ref=second)
        self.assertEqual(
            current_candidate_revision(self.store, self.campaign()), (3, "synthetic-candidate-r3")
        )
        self.assertEqual(self.replay()["result"], "passed")

    def test_replay_rejects_revision_that_ignores_invalidation(self) -> None:
        self.fixture.strict_validate(finalize=False)
        self.fixture.invalidate_candidate(STRICT)
        # 绕过写入门禁伪造一条不引用作废对象的 r2：重放发现被取代的 r1 已作废。
        self.write_raw_fact(
            "validation",
            "candidate_revision_recorded",
            {"revision": 2, "candidate_id": "synthetic-candidate-r2", "supersedes": None, "invalidation_ref": None},
            {"tool_identity_ref": None, "candidate_revision": 1, "evaluation_baseline": None},
        )
        with self.assertRaisesRegex(ControlError, "被取代的 r1 已作废"):
            self.replay()


class InvalidationCliTests(_RevisionCase):
    def run_cli(self, argv: list[str]) -> dict[str, Any]:
        return execute(build_parser().parse_args(argv))

    def test_candidate_invalidate_preview_and_apply(self) -> None:
        self.fixture.strict_validate(finalize=False)
        payload = self.fixture.candidate_invalidation_payload(STRICT)
        path = Path(self.temporary.name) / "invalidation.json"
        path.write_bytes(canonical_json_bytes(payload))
        base = ["candidate-invalidate", "--store", str(self.fixture.store_root), "--campaign", self.campaign_id, "--input", str(path)]
        preview = self.run_cli([*base, "--preview"])
        self.assertEqual(
            (preview["object_kind"], preview["review_sha256"]),
            ("candidate_invalidation", payload["review_sha256"]),
        )
        self.assertNotIn("approved_by", preview["review"])
        with self.assertRaisesRegex(ControlError, "--approve-sha256 与输入的 review_sha256 不一致"):
            self.run_cli([*base, "--approve-sha256", "0" * 64])
        applied = self.run_cli([*base, "--approve-sha256", payload["review_sha256"]])
        self.assertEqual(applied["object_ref"]["object_kind"], "candidate_invalidation")
        again = self.run_cli([*base, "--approve-sha256", payload["review_sha256"], "--allow-existing"])
        self.assertEqual(again, {"object_ref": applied["object_ref"], "reused": True})
        self.assertEqual(self.status()["candidate_revision_invalidation"], applied["object_ref"])
        wrong_campaign = [*base]
        wrong_campaign[wrong_campaign.index("--campaign") + 1] = "other-campaign"
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main([*wrong_campaign, "--preview"]), 2)


if __name__ == "__main__":
    unittest.main()
