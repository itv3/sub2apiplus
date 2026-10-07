"""Claude 候选交付（VC-6）编排器验收（Claude 升级控制面设计 B4-07 编排器部分）。

全部离线、零请求，验收一律走 ``claude_upgrade.main`` 入口。夹具：Store 夹具 ``SyntheticCampaign(schema="v2")`` 走到
``strict_validate``（VC-5 已在 Store 完成）后作为 Campaign 的 control-store，Campaign ID 与六项冻结身份和总计划同源；
编排器侧补最小前置——Claude 计时账本（VC-0～VC-5 完成、r1 激活、VC-6 进行中）、r1 的 revision.json 与 COMMIT、Store 的
``candidate_revision_recorded`` r1。交付环境执行器用测试替身（登记到 ``ENVIRONMENT_EXECUTORS``），可按「段、阶段」注入失败、
中途崩溃与环境漂移。

设计验收逐条对应（测试名前缀即编号）：
① 第三阶段失败、上游未变 → dr1（failure）第三阶段承接第二阶段 pass、第四阶段承接本段第三阶段 → 四阶段 pass → 收据；
② 四阶段全部 pass 且未 finalize，批准第二阶段失效 → dr1（approved_invalidation）覆盖二、三、四阶段，旧三、四 pass 失效；
③ 已 finalize 的 delivery 申请阶段失效被拒（零写入）；
④ 没有批准对象，直接重跑已通过的阶段被拒（零写入）；
⑤ finalize 重复调用返回原收据，换审核摘要被拒。
另有：环境变化开新 delivery_id（含收口旧 delivery 推进中的段）、Store 与账本之间被打断后补账收敛、同根因达上限在 Store
写入之前拒绝、执行器未登记报待落实、父监督器上下文拒绝阶段失效。

B4-07 收尾（第八、九节）：夹具补齐编排器侧的 VC 封存链——VC 总计划、VC-0～VC-3 checkpoint、r1 的 VC-4 checkpoint（绑定
与 Store 候选同一镜像 digest 的构建收据）与 VC-5 checkpoint（绑定 accept 阶段结果；结果登记的 Store 引用取自 Store 交接，
证据封印登记 VC-4 构建收据），入口重放与 VC-6 checkpoint 写入真跑。另验：finalize 写交付阶段结果与 VC-6 checkpoint
（validation_only）或只写结果、``production_activation_pending``（production_replacement，``ProductionReplacementDeliveryTests``）；
入口重放的三处一致性失败关闭；直接 CLI 的交付写入被拒（不带批准的 finalize 照旧可查）。

替身登记：本模块验交付链逻辑，直接调用编排器入口；交付写入「只经父批次」的限制（``claude_upgrade_delivery._require_parent_batch``）
在 setUp 换成放行替身，``ParentBatchGuardTests`` 撤掉替身验限制本身。真实父批次派发、父批次退出码、批次失败后的对账与逐字
重派由 ``test_claude_upgrade_vc6_batch`` 在 B4-06b 的真实 VC-5 链上验。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_acceptance
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_control.contracts import CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA, derive_identity_conditions
from tools.official_client_control.store import ControlStore
from tools.official_client_capture.tests.claude_vc6_delivery_fixture import FixtureExecutor
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

CAMPAIGN_ID = "claude-code-2_1_285-b407"
CANDIDATE = "synthetic-candidate-strict"
# Store 夹具的合成 Campaign 固定目标版本 1.0.0（Discovery 事实写死），总计划与账本取同一对版本。
BASELINE = "0.9.0"
TARGET = "1.0.0"
STAGES = list(delivery.STAGES)
# 父批次限制的真身（setUp 换成放行替身之前取，ParentBatchGuardTests 用它验限制本身）。
REQUIRE_PARENT_BATCH = delivery._require_parent_batch


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def run_main(argv: list[str]) -> tuple[int, dict | None, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = claude_upgrade.main(argv)
    text = stdout.getvalue()
    return code, (json.loads(text) if text.strip() else None), stderr.getvalue()


def tree_digests(*roots: Path) -> dict[str, str]:
    """目录下全部普通文件（锁文件除外）的路径 → 摘要（判零写入用）。"""

    digests: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.name.endswith(".lock"):
                digests[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


class _DeliveryCase(unittest.TestCase):
    tree: ManagedToolTree
    # Campaign 用途：Store 夹具、计时账本、VC 总计划与 Campaign 总计划同取（production_replacement 子类覆盖）。
    PURPOSE = "validation_only"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tree_directory = tempfile.TemporaryDirectory()
        cls.tree = ManagedToolTree(Path(cls._tree_directory.name) / "repo")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tree_directory.cleanup()

    def setUp(self) -> None:
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.data = self.root / "data"
        self.campaign = self.data / "evidence" / "campaigns" / CAMPAIGN_ID
        self.campaign.mkdir(parents=True, mode=0o700)
        for path in (self.data, self.data / "evidence", self.data / "evidence" / "campaigns"):
            path.chmod(0o700)
        conditions = self._store()
        commit = self._revision()
        self.ledger = self._ledger(commit)
        self._manifest(conditions)
        self.write_vc_chain()
        self.executor = FixtureExecutor()
        patcher = mock.patch.dict(delivery.ENVIRONMENT_EXECUTORS, {"fixture": lambda: self.executor})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 替身（模块说明）：交付写入只经父批次的限制在这里放行；ParentBatchGuardTests 撤掉它验限制本身。
        self.parent_guard = mock.patch.object(delivery, "_require_parent_batch", lambda label: None)
        self.parent_guard.start()
        self.addCleanup(self.parent_guard.stop)

    # ---- 夹具 ----

    def _store(self) -> dict[str, Any]:
        """Store 夹具走到 VC-5 完成（含 Store 的 r1 revision 事实），移成 Campaign 的 control-store。"""

        scratch = self.root / "store-build"
        scratch.mkdir(mode=0o700)
        fixture = SyntheticCampaign(scratch, schema="v2")
        fixture.campaign_id = CAMPAIGN_ID
        fixture.bootstrap_and_campaign(
            platforms=["linux/arm64"],
            entrypoints=["claude-cli"],
            default_conditions=["privacy=default"],
            campaign_purpose=self.PURPOSE,
            absolute_deadline_utc="2026-12-31T00:00:00Z",
        )
        fixture.discovery_and_evidence()
        fixture.profile_objects()
        fixture.profile_approve(self.PURPOSE)
        fixture.store.append_fact(
            CAMPAIGN_ID,
            "candidate_revision_recorded",
            {"revision": 1, "candidate_id": CANDIDATE, "supersedes": None, "invalidation_ref": None},
            fixture._time(),
        )
        fixture.strict_validate()
        self.fixture = fixture
        shutil.move(str(fixture.store_root), str(claude_upgrade_store.campaign_store_root(self.campaign)))
        shutil.copytree(fixture.external_root, self.data, dirs_exist_ok=True)
        return derive_identity_conditions(fixture.campaign_document)[0]

    def _revision(self) -> dict[str, Any]:
        record = vc_artifacts.build_candidate_revision(
            campaign_id=CAMPAIGN_ID,
            revision=1,
            candidate_id=CANDIDATE,
            opened_at_utc=_now(),
            previous_revision_sha256=None,
            vc3_checkpoint={"path": "control/vc/vc-3-checkpoint.json", "sha256": "3" * 64, "phase": "VC-3", "checkpoint_sha256": "4" * 64},
            vc3_stage_receipt={"path": "profile/result.json", "sha256": "5" * 64},
            supersedes=None,
        )
        commit = vc_artifacts.build_candidate_revision_commit(
            campaign_id=CAMPAIGN_ID, revision=1, candidate_id=CANDIDATE, record_sha256=record["record_sha256"],
            committed_at_utc=_now(),
        )
        directory = claude_upgrade._candidate_revision_dir(self.campaign, 1)
        for path in (self.campaign / "control", self.campaign / "control" / "vc", directory.parent, directory):
            path.mkdir(exist_ok=True, mode=0o700)
        _write_json(directory / "revision.json", record)
        _write_json(directory / "COMMIT", commit)
        return commit

    def _ledger(self, commit: dict[str, Any]) -> Path:
        ledger = self.root / "timing-ledger"
        timing.create_ledger(
            ledger, upgrade_id=CAMPAIGN_ID, baseline_version=BASELINE, target_version=TARGET,
            campaign_purpose=self.PURPOSE, evidence_decision="recapture", client="claude",
        )

        def append(event_id: str, phase: str, event_type: str, **fields: Any) -> None:
            timing.append_event(ledger, event_id=event_id, phase=phase, event_type=event_type, next_action="夹具", **fields)

        append("vc0-done", "VC-0", "stage_completed")
        for phase in ("VC-1", "VC-2", "VC-3"):
            append(f"{phase}-started", phase, "stage_started")
            append(f"{phase}-done", phase, "stage_completed")
        append("stage-revision-r1", "VC-4", "stage_revision", revision=1, candidate_id=CANDIDATE,
               revision_commit_sha256=commit["commit_sha256"])
        for phase in ("VC-4", "VC-5"):
            append(f"{phase}-started", phase, "stage_started")
            append(f"{phase}-done", phase, "stage_completed")
        append("VC-6-started", "VC-6", "stage_started")
        return ledger

    def _manifest(self, conditions: dict[str, Any]) -> None:
        rules = _write_json(self.campaign / "inputs" / "rules.json", {"rules": []})
        self.plan = vc_artifacts.build_campaign_plan(
            campaign_id=CAMPAIGN_ID, campaign_mode="formal", campaign_purpose=self.PURPOSE, baseline_version=BASELINE,
            target_version=TARGET, created_at_utc="2026-10-07T00:00:00Z", original_deadline_at_utc="2026-12-31T00:00:00Z",
            timing_checkpoint_sha256="1" * 64, arm64_environment_sha256="2" * 64, job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )
        plan_path = _write_json(self.campaign / "control" / "vc" / "campaign-plan.json", self.plan)
        manifest = {
            "schema_version": "official-client-upgrade-campaign/v1", "client": "claude", "campaign_id": CAMPAIGN_ID,
            "created_at_utc": "2026-10-07T00:00:00Z", "campaign_mode": "formal", "campaign_purpose": self.PURPOSE,
            "baseline_version": BASELINE, "target_version": TARGET, "target_sha256": "a" * 64,
            "official_identity": {"npm": {}, "gcs": {}, "binary_sha256": "b" * 64, "binary_equivalence": "npm=gcs"},
            "baseline_identity": {}, "tool_identity": {"client": "claude"},
            "control_receipts": {
                "upgrade_timing": {
                    "ledger_dir": str(self.ledger),
                    "ledger_plan_sha256": claude_upgrade.file_sha256(self.ledger / "ledger.json"),
                }
            },
            "inputs": {"rules": {"path": "inputs/rules.json", "sha256": claude_upgrade.file_sha256(rules)}},
            "configuration": {
                key: conditions[key] for key in ("platform", "entrypoint", "privacy_mode", "oauth_org_id", "model_catalog_sha256")
            },
            "required_rules": [], "coverage_plan": {}, "jobs": [], "predecessor": None,
            "vc_control": {"campaign_plan": {"path": "control/vc/campaign-plan.json", "sha256": claude_upgrade.file_sha256(plan_path)}},
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (self.campaign / "campaign.json").write_bytes(raw)
        (self.campaign / "campaign.json").chmod(0o600)
        (self.campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")

    def write_checkpoint(self, phase: str, stage: Path, previous: tuple[Path, dict[str, Any]] | None) -> tuple[Path, dict[str, Any]]:
        revision = 1 if phase in claude_upgrade_vc.CANDIDATE_PHASES else None
        checkpoint = vc_artifacts.build_vc_checkpoint(
            campaign_plan=self.plan,
            phase=phase,
            status="complete",
            predecessor_checkpoint=(
                None if previous is None else claude_upgrade_vc.vc_checkpoint_reference(self.campaign, previous[0], previous[1])
            ),
            stage_receipt={"path": stage.relative_to(self.campaign).as_posix(), "sha256": claude_upgrade.file_sha256(stage)},
            completed_at_utc="2026-10-07T01:00:00Z",
            execute_item_ids=[],
            reuse_item_ids=[],
            live_request_count=0,
            scanned_bytes=0,
        )
        path = claude_upgrade_vc.vc_checkpoint_path(self.campaign, phase, revision=revision)
        _write_json(path, checkpoint)
        return path, checkpoint

    def write_vc_chain(self, *, store: dict[str, Any] | None = None, sealed_build: dict[str, Any] | None = None,
                       image_digest: str | None = None) -> None:
        """编排器侧 VC 封存链（模块说明）：VC-0～VC-3 → r1 的 VC-4（构建收据）→ VC-5（accept 阶段结果）。三个关键字参数各注入
        一处不一致（accept 结果的 Store 引用、证据封印登记的构建收据、构建收据的镜像 digest），其余照常，供入口重放失败关闭用例。"""

        handoff = self.handoff()
        previous: tuple[Path, dict[str, Any]] | None = None
        for phase in ("VC-0", "VC-1", "VC-2", "VC-3"):
            stage = _write_json(self.campaign / "stages" / f"{phase.lower()}.json", {"phase": phase})
            previous = self.write_checkpoint(phase, stage, previous)
        build = _write_json(
            claude_upgrade._candidate_build_receipt_path(self.campaign, CANDIDATE),
            {"candidate_id": CANDIDATE, "image": {"manifest_digest": image_digest or handoff["candidate_image_digest"]}},
        )
        previous = self.write_checkpoint("VC-4", build, previous)
        accept_dir = self.campaign / "acceptance" / CANDIDATE
        bound = {
            name: _write_json(accept_dir / f"{name}.json", {"fixture": name})
            for name in ("comparison", "results", "evaluation-run", "runtime-audit")
        }
        binding = lambda path: {"path": path.relative_to(self.campaign).as_posix(), "sha256": claude_upgrade.file_sha256(path)}  # noqa: E731
        seal = _write_json(accept_dir / "evidence-seal.json", {"build_receipt": sealed_build or binding(build)})
        refs = {key: handoff[key] for key in ("validation_completed_ref", "acceptance_ref", "candidate_evidence_package_ref")}
        accept = _write_json(
            accept_dir / "result.json",
            {
                "schema_version": claude_upgrade_acceptance.ACCEPT_SCHEMA, "client": "claude", "campaign_id": CAMPAIGN_ID,
                "stage": "accept", "candidate_id": CANDIDATE, "status": "complete", "accepted": True,
                "campaign_purpose": self.PURPOSE, "candidate_purpose": self.PURPOSE, "production_state": "not_activated",
                "candidate_revision": 1, "validation_round": 1, "evaluation_baseline": 0,
                "comparison": binding(bound["comparison"]),
                "assertions": {"results": binding(bound["results"]), "evaluation_run": binding(bound["evaluation-run"])},
                "external_gate": {}, "runtime_audit": {**binding(bound["runtime-audit"]), "activation_fact": {}},
                "client_evidence": {}, "pair_rows": {}, "store": {**refs, **(store or {})}, "evidence_seal": binding(seal),
                "accepted_at_utc": "2026-10-07T02:00:00Z",
            },
        )
        self.write_checkpoint("VC-5", accept, previous)

    def handoff(self) -> dict[str, Any]:
        manifest = claude_upgrade.load_campaign_manifest(self.campaign)
        store, campaign = claude_upgrade_store.open_campaign_store(self.campaign, manifest)
        return delivery._vc5_handoff(store, campaign, CANDIDATE)

    # ---- 入口与读侧 ----

    def run_delivery(self, *extra: str) -> tuple[int, dict | None, str]:
        return run_main(
            ["deliver-candidate", "run", "--campaign-dir", str(self.campaign), "--candidate-id", CANDIDATE,
             "--environment-executor", "fixture", "--observation-window-seconds", "60", *extra]
        )

    def finalize(self, approve: str | None = None) -> tuple[int, dict | None, str]:
        extra = ["--approve-sha256", approve] if approve is not None else []
        return run_main(["deliver-candidate", "finalize", "--campaign-dir", str(self.campaign), "--candidate-id", CANDIDATE, *extra])

    def invalidate(self, action: str, stage: str, *extra: str) -> tuple[int, dict | None, str]:
        return run_main(
            ["delivery-stage-invalidate", action, "--campaign-dir", str(self.campaign), "--candidate-id", CANDIDATE,
             "--stage", stage, "--reason", f"{stage} 的通过结论需要重跑核实", *extra]
        )

    def store(self) -> ControlStore:
        return ControlStore(claude_upgrade_store.campaign_store_root(self.campaign))

    def chain(self, delivery_id: str = f"{CANDIDATE}-d1") -> dict[str, Any]:
        from tools.official_client_control.receipts import delivery_chain_report

        return delivery_chain_report(self.store(), CAMPAIGN_ID, delivery_id)

    def fact(self, reference: dict[str, Any]) -> dict[str, Any]:
        return self.store().load_fact(reference)["payload"]

    def ledger_events(self) -> list[tuple[Any, ...]]:
        return [
            (event["event_type"], event["delivery_id"], event["delivery_recovery_revision"], event["delivery_stage"], event["root_cause_id"])
            for event, _raw in timing._load_events(self.ledger)
            if event["event_type"] in timing.DELIVERY_EVENT_TYPES
        ]

    def written(self) -> dict[str, str]:
        return tree_digests(claude_upgrade_store.campaign_store_root(self.campaign), self.ledger, self.campaign / "control" / "delivery")

    def complete_first_round(self) -> dict[str, Any]:
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, (result or {}).get("status")), (2, "awaiting_finalize"), stderr)
        return result

    def finalized(self) -> dict[str, Any]:
        review = self.complete_first_round()["package_review"]["review_sha256"]
        code, result, stderr = self.finalize(review)
        self.assertEqual((code, (result or {}).get("status")), (0, "finalized"), stderr)
        return result


class DeliveryAcceptanceTests(_DeliveryCase):
    """设计 B4-07 验收 ①～⑤，全部经编排器入口。"""

    def test_1_third_stage_failure_recovers_through_dr1(self) -> None:
        self.executor.failures[("dr0", "candidate_restored")] = None
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"], result["failed_stage"]), (3, "stage_failed", "candidate_restored"), stderr)
        self.assertEqual(result["root_cause_id"], "delivery-candidate_restored")
        self.assertEqual(self.ledger_events(), [])  # 首轮交付的失败不单独记账
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (2, "awaiting_finalize"), stderr)
        self.assertEqual(
            (result["recovery"]["recovery_revision"], result["recovery"]["anchor_kind"], result["recovery"]["rerun_from_stage"]),
            ("dr1", "failure", "candidate_restored"),
        )
        self.assertEqual(
            [(segment, stage) for segment, stage, _nonce in self.executor.calls],
            [("dr0", "candidate_active"), ("dr0", "rollback_verified"), ("dr0", "candidate_restored"),
             ("dr1", "candidate_restored"), ("dr1", "stable_observed")],
        )
        chain = self.chain()
        valid = chain["valid_stage_refs"]
        first_round = {self.fact(valid[stage])["recovery_revision"] for stage in STAGES[:2]}
        self.assertEqual(first_round, {None})
        restored, observed = self.fact(valid["candidate_restored"]), self.fact(valid["stable_observed"])
        self.assertEqual((restored["recovery_revision"], observed["recovery_revision"]), ("dr1", "dr1"))
        # 第三阶段承接首轮第二阶段的 pass，第四阶段承接本段第三阶段。
        self.assertEqual(restored["previous_stage_ref"], valid["rollback_verified"])
        self.assertEqual(observed["previous_stage_ref"], valid["candidate_restored"])
        self.assertEqual(
            self.ledger_events(),
            [
                ("delivery_recovery_started", f"{CANDIDATE}-d1", "dr1", "candidate_restored", "delivery-candidate_restored"),
                ("delivery_recovery_completed", f"{CANDIDATE}-d1", "dr1", "stable_observed", None),
            ],
        )
        code, finalized, stderr = self.finalize(result["package_review"]["review_sha256"])
        self.assertEqual((code, finalized["status"], finalized["reused"]), (0, "finalized", False), stderr)
        receipt = self.store().load_receipt(finalized["receipt_ref"])
        self.assertEqual(receipt["schema_version"], CANDIDATE_DELIVERY_RECEIPT_V3_SCHEMA)
        self.assertEqual(receipt["delivery_fact_refs"], [valid[stage] for stage in STAGES])
        self.assertEqual(receipt["delivery_failure_refs"], chain["failure_refs"])
        self.assertEqual(receipt["recovery_refs"], chain["recovery_refs"])
        self.assertEqual(timing.inspect_ledger(self.ledger)["status"], "active")

    def test_2_approved_invalidation_reruns_from_second_stage(self) -> None:
        self.complete_first_round()
        before = self.chain()["valid_stage_refs"]
        snapshot = self.written()
        code, preview, stderr = self.invalidate("preview", "rollback_verified")
        self.assertEqual((code, preview["status"]), (2, "preview"), stderr)
        self.assertEqual(preview["review"]["invalidated_stage_ref"], before["rollback_verified"])
        self.assertEqual(self.written(), snapshot)  # preview 零写入
        code, applied, stderr = self.invalidate(
            "apply", "rollback_verified", "--approve-sha256", preview["review_sha256"], "--approved-by", "reviewer-a"
        )
        self.assertEqual((code, applied["status"], applied["reused"]), (0, "applied", False), stderr)
        # 重跑 apply 命中同一份待消费失效：复用，不再写。
        code, again, stderr = self.invalidate(
            "apply", "rollback_verified", "--approve-sha256", preview["review_sha256"], "--approved-by", "reviewer-a"
        )
        self.assertEqual((code, again["reused"], again["invalidation_ref"]), (0, True, applied["invalidation_ref"]), stderr)
        self.assertEqual(self.chain()["pending_invalidation_ref"], applied["invalidation_ref"])
        self.executor.calls.clear()
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (2, "awaiting_finalize"), stderr)
        self.assertEqual(
            (result["recovery"]["anchor_kind"], result["recovery"]["rerun_from_stage"], result["recovery"]["root_cause_id"]),
            ("approved_invalidation", "rollback_verified", None),
        )
        self.assertEqual([stage for _segment, stage, _nonce in self.executor.calls], STAGES[1:])
        after = self.chain()["valid_stage_refs"]
        self.assertEqual(after["candidate_active"], before["candidate_active"])
        for stage in STAGES[1:]:
            self.assertNotEqual(after[stage], before[stage])  # 起点之后的旧 pass 一律失效
            self.assertEqual(self.fact(after[stage])["recovery_revision"], "dr1")
        self.assertEqual(self.fact(after["rollback_verified"])["previous_stage_ref"], before["candidate_active"])
        self.assertEqual(
            [event[0] for event in self.ledger_events()],
            ["delivery_stage_invalidated", "delivery_recovery_started", "delivery_recovery_completed"],
        )
        code, finalized, stderr = self.finalize(result["package_review"]["review_sha256"])
        self.assertEqual((code, finalized["status"]), (0, "finalized"), stderr)
        receipt = self.store().load_receipt(finalized["receipt_ref"])
        self.assertEqual(receipt["stage_invalidation_refs"], [applied["invalidation_ref"]])

    def test_3_finalized_delivery_rejects_stage_invalidation(self) -> None:
        self.finalized()
        snapshot = self.written()
        for action in ("preview", "apply"):
            extra = ["--approve-sha256", "0" * 64, "--approved-by", "reviewer-a"] if action == "apply" else []
            code, _result, stderr = self.invalidate(action, "rollback_verified", *extra)
            self.assertEqual(code, 1)
            self.assertIn("已 finalize", stderr)
        self.assertEqual(self.written(), snapshot)
        # 已 finalize 的交付再 run 只返回终态，不再调用执行器的阶段动作。
        self.executor.calls.clear()
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (0, "finalized"), stderr)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.written(), snapshot)

    def test_4_rerunning_a_passed_stage_without_approval_is_rejected(self) -> None:
        self.complete_first_round()
        snapshot = self.written()
        self.executor.calls.clear()
        code, _result, stderr = self.run_delivery("--rerun-from", "rollback_verified")
        self.assertEqual(code, 1)
        self.assertIn("先 delivery-stage-invalidate", stderr)
        # 不带核对参数重跑：有效链已全部通过，停在 awaiting_finalize，不重跑任何阶段。
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (2, "awaiting_finalize"), stderr)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.written(), snapshot)

    def test_5_repeated_finalize_returns_the_original_receipt(self) -> None:
        first = self.finalized()
        code, review, stderr = self.finalize()
        self.assertEqual((code, review["status"]), (0, "finalized"), stderr)
        code, second, stderr = self.finalize(review["package_review"]["review_sha256"])
        self.assertEqual((code, second["receipt_ref"], second["reused"]), (0, first["receipt_ref"], True), stderr)
        code, _result, stderr = self.finalize("f" * 64)
        self.assertEqual(code, 1)
        self.assertIn("审核摘要不一致", stderr)
        self.assertEqual(len(self.store().list_receipt_refs("candidate_delivery")), 1)


class DeliveryRecoveryTests(_DeliveryCase):
    """环境变化、补账收敛、同根因纪律与入口边界。"""

    def test_failure_rerun_from_must_match_the_derived_stage(self) -> None:
        self.executor.failures[("dr0", "rollback_verified")] = "arm64-rollback-timeout"
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["root_cause_id"]), (3, "arm64-rollback-timeout"), stderr)
        snapshot = self.written()
        code, _result, stderr = self.run_delivery("--rerun-from", "candidate_active")
        self.assertEqual(code, 1)
        self.assertIn("推导出的续跑起点是 rollback_verified", stderr)
        self.assertEqual(self.written(), snapshot)
        code, result, stderr = self.run_delivery("--rerun-from", "rollback_verified")
        self.assertEqual((code, result["status"]), (2, "awaiting_finalize"), stderr)
        self.assertEqual(self.ledger_events()[0][4], "arm64-rollback-timeout")

    def test_environment_change_opens_a_new_delivery(self) -> None:
        self.executor.failures[("dr0", "rollback_verified")] = None
        code, result, stderr = self.run_delivery()
        self.assertEqual(code, 3, stderr)
        self.executor.failures.clear()
        self.executor.environment["compose_sha256"] = "9" * 64
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"], result["delivery_id"]), (2, "awaiting_finalize", f"{CANDIDATE}-d2"), stderr)
        self.assertEqual(result["environment_changed"]["previous_delivery_id"], f"{CANDIDATE}-d1")
        self.assertEqual(set(result["environment_changed"]["drift"]), {"compose_sha256"})
        self.assertIsNone(result["environment_changed"]["closed"])  # d1 的首轮已以失败收口
        plan = self.store().load_object(result["delivery_plan_ref"])["payload"]
        self.assertEqual(plan["compose_sha256"], "9" * 64)
        self.assertEqual(self.chain()["recovery_refs"], [])  # 旧 delivery 不预约恢复段
        code, finalized, stderr = self.finalize(result["package_review"]["review_sha256"])
        self.assertEqual((code, finalized["delivery_id"]), (0, f"{CANDIDATE}-d2"), stderr)

    def test_environment_change_closes_an_open_segment_first(self) -> None:
        self.executor.failures[("dr0", "candidate_restored")] = None
        self.assertEqual(self.run_delivery()[0], 3)
        self.executor.crash_at = ("dr1", "stable_observed")
        code, _result, stderr = self.run_delivery()
        self.assertEqual(code, 1)
        self.assertIn("中途崩溃", stderr)
        self.assertFalse(self.chain()["latest_closed"])  # dr1 停在 candidate_restored 之后
        self.executor.crash_at = None
        self.executor.failures.clear()
        self.executor.environment["network_sha256"] = "8" * 64
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["delivery_id"]), (2, f"{CANDIDATE}-d2"), stderr)
        closed = result["environment_changed"]["closed"]
        self.assertEqual((closed["segment"], closed["stage"]), ("dr1", "stable_observed"))
        old = self.chain()
        self.assertTrue(old["latest_closed"])
        self.assertEqual(old["terminal_result"], "failed")
        self.assertEqual(
            self.ledger_events(),
            [
                ("delivery_recovery_started", f"{CANDIDATE}-d1", "dr1", "candidate_restored", "delivery-candidate_restored"),
                ("delivery_recovery_failed", f"{CANDIDATE}-d1", "dr1", "stable_observed", delivery.ROOT_CAUSE_ENVIRONMENT_DRIFT),
            ],
        )

    def test_ledger_catches_up_after_interruption_between_store_and_ledger(self) -> None:
        self.executor.failures[("dr0", "candidate_restored")] = None
        self.assertEqual(self.run_delivery()[0], 3)
        real = timing.append_event

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            raise timing.TimingLedgerError("模拟进程在 Store 写入之后、记账之前被打断")

        with mock.patch.object(timing, "append_event", interrupted):
            code, _result, stderr = self.run_delivery()
        self.assertEqual(code, 1)
        self.assertIn("Store 已写、账本待补", stderr)
        self.assertEqual(self.chain()["latest_recovery_revision"], "dr1")  # 段预约已在 Store
        self.assertEqual(self.ledger_events(), [])
        self.assertIs(timing.append_event, real)
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (2, "awaiting_finalize"), stderr)
        self.assertEqual(result["ledger_events"][0], f"delivery-{CANDIDATE}-d1-dr1-started")
        self.assertEqual(
            [event[0] for event in self.ledger_events()], ["delivery_recovery_started", "delivery_recovery_completed"]
        )
        self.assertEqual(len(self.chain()["recovery_refs"]), 1)

    def test_same_root_cause_limit_stops_before_store_writes(self) -> None:
        self.executor.failures[("dr0", "candidate_restored")] = None
        self.executor.failures[("dr1", "candidate_restored")] = None
        self.assertEqual(self.run_delivery()[0], 3)
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"]), (3, "stage_failed"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger)["status"], "stop_required")
        snapshot = self.written()
        code, _result, stderr = self.run_delivery()
        self.assertEqual(code, 1)
        self.assertIn("campaign-resume", stderr)
        self.assertEqual(self.written(), snapshot)

    def test_failed_delivery_gates_write_nothing(self) -> None:
        self.executor.failed_gates = {"secret-scan"}
        code, result, stderr = self.run_delivery()
        self.assertEqual((code, result["status"], result["failed_gates"]), (3, "delivery_gates_failed", ["secret-scan"]), stderr)
        self.assertEqual(self.executor.calls, [])
        self.assertEqual(self.store().list_object_refs("candidate_delivery_plan"), [])

    def test_unregistered_executor_reports_pending(self) -> None:
        code, _result, stderr = run_main(
            ["deliver-candidate", "run", "--campaign-dir", str(self.campaign), "--candidate-id", CANDIDATE,
             "--environment-executor", "arm64-isolated"]
        )
        self.assertEqual(code, 1)
        self.assertIn("B4-07 遗留项", stderr)
        self.assertEqual(delivery.ENVIRONMENT_EXECUTORS.keys() - {"fixture"}, set())

    def test_stage_invalidation_is_rejected_in_campaign_run(self) -> None:
        self.complete_first_round()
        with mock.patch.dict(os.environ, {codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
            code, _result, stderr = self.invalidate("preview", "rollback_verified")
        self.assertEqual(code, 1)
        self.assertIn("批次控制面命令", stderr)

    def test_other_candidate_is_rejected(self) -> None:
        code, _result, stderr = run_main(
            ["deliver-candidate", "run", "--campaign-dir", str(self.campaign), "--candidate-id", "claude-cand-other",
             "--environment-executor", "fixture"]
        )
        self.assertEqual(code, 1)
        self.assertIn("不是当前候选 revision r1 的候选", stderr)



class Vc6CompletionTests(_DeliveryCase):
    """第八节：入口重放、交付阶段结果与 VC-6 checkpoint（validation_only）、status 用的交付状态。"""

    def vc6_checkpoint(self) -> Path:
        return claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-6", revision=1)

    def test_finalize_writes_stage_result_and_vc6_checkpoint(self) -> None:
        self.assertEqual(delivery.delivery_status(self.campaign, CANDIDATE)["status"], "ready")
        review = self.complete_first_round()["package_review"]["review_sha256"]
        status = delivery.delivery_status(self.campaign, CANDIDATE)
        self.assertEqual((status["status"], status["review_sha256"]), ("delivery_awaiting_finalize", review))
        code, finalized, stderr = self.finalize(review)
        self.assertEqual((code, finalized["status"], finalized["vc6_status"]), (0, "finalized", "complete"), stderr)
        result = delivery.load_stage_result(self.campaign, CANDIDATE)
        self.assertEqual(finalized["stage_result"]["path"], f"delivery/{CANDIDATE}/result.json")
        self.assertEqual(
            (result["receipt_ref"], result["package_ref"], result["vc6_status"], result["release_state"], result["production_state"]),
            (finalized["receipt_ref"], finalized["package_ref"], "complete", "ready_for_operator_release", "not_activated"),
        )
        vc5_path = claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-5", revision=1)
        self.assertEqual(result["vc5_checkpoint"]["path"], vc5_path.relative_to(self.campaign).as_posix())
        self.assertEqual(result["accept_result"]["path"], f"acceptance/{CANDIDATE}/result.json")
        self.assertEqual(result["store"]["acceptance_ref"], self.handoff()["acceptance_ref"])
        # VC-6 checkpoint：前序是同 revision 的 VC-5，阶段收据是交付阶段结果，零请求、零扫描。
        _path, checkpoint = claude_upgrade_vc.replay_vc_checkpoint(self.campaign, self.plan, "VC-6", revision=1)
        self.assertEqual(checkpoint["stage_receipt"], finalized["stage_result"])
        self.assertEqual(checkpoint["predecessor_checkpoint"], result["vc5_checkpoint"])
        self.assertEqual(checkpoint["metrics"], {"live_request_count": 0, "scanned_bytes": 0})
        self.assertEqual(finalized["vc6_checkpoint"]["checkpoint_sha256"], checkpoint["checkpoint_sha256"])
        # stage_completed 由批次层在父 run 成功后补写；直接调用（替身放行）不写。
        self.assertEqual(timing.phase_ledger_state(self.ledger)["active_phase"], "VC-6")
        self.assertEqual(delivery.delivery_status(self.campaign, CANDIDATE)["status"], "ready_for_operator_release")
        # 重复 finalize：收据、阶段结果与 checkpoint 逐字复用。
        before = (self.vc6_checkpoint().read_bytes(), delivery.stage_result_path(self.campaign, CANDIDATE).read_bytes())
        code, again, stderr = self.finalize(review)
        self.assertEqual((code, again["reused"], again["stage_result"]), (0, True, finalized["stage_result"]), stderr)
        self.assertEqual((self.vc6_checkpoint().read_bytes(), delivery.stage_result_path(self.campaign, CANDIDATE).read_bytes()), before)
        code, reviewed, stderr = self.finalize()
        self.assertEqual((code, reviewed["stage_result"]["vc6_status"]), (0, "complete"), stderr)

    def test_interrupted_after_receipt_completes_on_rerun(self) -> None:
        review = self.complete_first_round()["package_review"]["review_sha256"]
        with mock.patch.object(claude_upgrade_vc, "complete_candidate_phase", side_effect=RuntimeError("模拟收据签发后被打断")):
            code, _result, stderr = self.finalize(review)
        self.assertEqual(code, 1)
        self.assertIn("模拟收据签发后被打断", stderr)
        self.assertEqual(len(self.store().list_receipt_refs("candidate_delivery")), 1)
        self.assertFalse(self.vc6_checkpoint().exists())
        self.assertEqual(delivery.delivery_status(self.campaign, CANDIDATE)["status"], "delivery_completion_pending")
        code, finalized, stderr = self.finalize(review)
        self.assertEqual((code, finalized["reused"], finalized["vc6_status"]), (0, True, "complete"), stderr)
        self.assertTrue(self.vc6_checkpoint().is_file())

    def test_entry_replay_fails_closed_without_writes(self) -> None:
        other = {"kind": "fact", "dimension": "validation", "fact_id": "f" * 64, "sha256": "f" * 64}
        cases = {
            "accept 结果的 Store 引用": ({"store": {"acceptance_ref": other}}, "accept 阶段结果登记的 acceptance_ref 与 Store 交接不一致"),
            "封印登记的构建收据": ({"sealed_build": {"path": "stages/vc-3.json", "sha256": "0" * 64}}, "不是 VC-4 checkpoint 绑定的那份"),
            "构建收据的镜像": ({"image_digest": "sha256:" + "ab" * 32}, "镜像 digest 与 Store 交接"),
        }
        for label, (fields, message) in cases.items():
            with self.subTest(label):
                self.write_vc_chain(**fields)
                snapshot = self.written()
                code, _result, stderr = self.run_delivery()
                self.assertEqual(code, 1)
                self.assertIn(message, stderr)
                self.assertEqual(self.written(), snapshot)
                self.assertEqual(self.executor.calls, [])
        self.write_vc_chain()
        self.complete_first_round()

    def test_other_revision_accept_result_is_rejected(self) -> None:
        accept_path = self.campaign / "acceptance" / CANDIDATE / "result.json"
        payload = json.loads(accept_path.read_text(encoding="utf-8"))
        _write_json(accept_path, {**payload, "candidate_revision": 2})
        vc4 = claude_upgrade_vc.replay_vc_checkpoint(self.campaign, self.plan, "VC-4", revision=1)
        self.write_checkpoint("VC-5", accept_path, vc4)
        code, _result, stderr = self.run_delivery()
        self.assertEqual(code, 1)
        self.assertIn("不属于当前候选 revision r1", stderr)


class ProductionReplacementDeliveryTests(_DeliveryCase):
    """production_replacement：候选交付终点同一份交付阶段结果，不写 VC-6 checkpoint，等 B4-08 生产分支。"""

    PURPOSE = "production_replacement"

    def test_finalize_records_delivery_endpoint_and_leaves_vc6_open(self) -> None:
        review = self.complete_first_round()["package_review"]["review_sha256"]
        code, finalized, stderr = self.finalize(review)
        self.assertEqual((code, finalized["status"], finalized["vc6_status"]), (0, "finalized", "production_activation_pending"), stderr)
        self.assertIsNone(finalized["vc6_checkpoint"])
        self.assertIn("B4-08", finalized["next_command"])
        result = delivery.load_stage_result(self.campaign, CANDIDATE)
        self.assertEqual((result["campaign_purpose"], result["vc6_status"]), ("production_replacement", "production_activation_pending"))
        self.assertFalse(claude_upgrade_vc.vc_checkpoint_path(self.campaign, "VC-6", revision=1).exists())
        self.assertEqual(timing.phase_ledger_state(self.ledger)["active_phase"], "VC-6")
        status = delivery.delivery_status(self.campaign, CANDIDATE)
        self.assertEqual(status["status"], "production_activation_pending")


class ParentBatchGuardTests(_DeliveryCase):
    """第九节：Formal Campaign 的交付写入只经父批次；不带批准的 finalize 照旧可以直接查看。"""

    def test_direct_delivery_writes_are_rejected(self) -> None:
        review = self.complete_first_round()["package_review"]["review_sha256"]
        snapshot = self.written()
        with mock.patch.object(delivery, "_require_parent_batch", REQUIRE_PARENT_BATCH):
            code, _result, stderr = self.run_delivery()
            self.assertEqual(code, 1)
            self.assertIn("deliver-candidate run 是 VC-6 批次动作", stderr)
            code, _result, stderr = self.finalize(review)
            self.assertEqual(code, 1)
            self.assertIn("deliver-candidate finalize --approve-sha256 是 VC-6 批次动作", stderr)
            self.assertEqual(self.written(), snapshot)
            self.assertFalse(delivery.stage_result_path(self.campaign, CANDIDATE).exists())
            code, reviewed, stderr = self.finalize()
            self.assertEqual((code, reviewed["status"], reviewed["package_review"]["review_sha256"]), (2, "awaiting_finalize", review), stderr)
            # 父监督器上下文里限制放行（真实派发见 test_claude_upgrade_vc6_batch）。
            with mock.patch.dict(os.environ, {codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV: "1"}):
                self.assertIsNone(delivery._require_parent_batch("deliver-candidate run"))

    def test_batch_actions_are_recognized_only_in_exact_form(self) -> None:
        manifest = {"phase": "VC-6", "campaign_id": CAMPAIGN_ID, "candidate_id": CANDIDATE}
        run = delivery.run_action(self.campaign, CANDIDATE, environment_executor="fixture", observation_window_seconds=60)
        finalize = delivery.finalize_action(self.campaign, CANDIDATE, "a" * 64)
        self.assertEqual((run["timeout_seconds"], finalize["timeout_seconds"]), (60 + delivery.RUN_TIMEOUT_MARGIN_SECONDS, 900))
        self.assertEqual(delivery.vc6_action_plan(run)["execute_item_ids"], [delivery.RUN_ACTION_ID])
        self.assertEqual((delivery.batch_action_kind(run, manifest), delivery.batch_action_kind(finalize, manifest)), ("run", "finalize"))
        swapped = dict(run, action_id=delivery.FINALIZE_ACTION_ID, item_ids=[delivery.FINALIZE_ACTION_ID])
        refused = {
            "别的阶段": (run, {**manifest, "phase": "VC-5"}),
            "别的 Campaign": (run, {**manifest, "campaign_id": "claude-code-other"}),
            "别的候选": (run, {**manifest, "candidate_id": "claude-cand-other"}),
            "项名不同": (dict(run, item_ids=["compare"]), manifest),
            "子命令与动作不对应": (swapped, manifest),
            "多余旗标": (dict(run, command=[*run["command"], "--rerun-from", "candidate_active"]), manifest),
            "缺旗标": (dict(finalize, command=finalize["command"][:-2]), manifest),
            "批准摘要非法": (dict(finalize, command=[*finalize["command"][:-1], "x"]), manifest),
            "别的解释器": (dict(run, command=["/bin/sh", *run["command"][1:]]), manifest),
            "不是映射": ([run], manifest),
        }
        for label, (action, owner) in refused.items():
            with self.subTest(refused=label):
                self.assertIsNone(delivery.batch_action_kind(action, owner))


if __name__ == "__main__":
    unittest.main()
