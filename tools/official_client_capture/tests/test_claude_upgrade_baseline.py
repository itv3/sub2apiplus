"""Claude 评估基线 b<K>（Claude 升级控制面设计 B4-04 编排器侧）：服务面成员、评估器授权与 Store 分层。

编排器侧用合成的 Claude Campaign（Claude 计时账本推进到 VC-5、显式 r1；评估基线目录的 recovery.json／COMMIT 由共享制品
合同的构造器生成），Store 侧用 Store 测试夹具的严格候选链（Campaign v2、候选冻结、ValidationAttempt、b0 验收），两者以
同一候选 ID 衔接。覆盖：

* 读取判据与 Codex 同名函数逐项相同（COMMIT、recovery、各基线状态、b≥1 阶段读源与写目标）；
* 两份登记：编排器先写、Store 后写，Store 落后时当前基线拒绝、补写后一致，重跑复用原事实；后一条基线 supersedes 前一条；
* evaluation-recover 包装：预检不过零写入、共享流程返回 applied 后写 Store 事实、两步之间被强杀后重跑先补写收敛、预览不写；
* 评估器授权：plan 口径、演进迁移（含尚未建立的 to）、b≥1 授权、核对失败关闭；风险③合同——演进链全部已建立时与共享
  实现逐字相同，原样（带 status）交给共享实现必然被拒；
* 工具演进的评估器候选侧：活跃／只读候选、b0 授权是否迁移。
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_tool_identity_policy as claude_identity
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_baseline as baseline_module
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.tests.fixtures import ManagedToolTree, SyntheticCampaign

CANDIDATE = "synthetic-candidate-strict"
START = "2026-09-01T00:00:00+00:00"
CHECKER = "c1" * 32
BUILDER = "b1" * 32
DIGESTS = {
    "checker_sha256": CHECKER,
    "builder_sha256": BUILDER,
    "compare_reader_sha256": "c3" * 32,
    "accept_reader_sha256": "d4" * 32,
}


def _at(minutes: int) -> str:
    return (datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _evaluator_keys() -> tuple[str, str]:
    policy = claude_identity.load_policy()
    return str(policy["evaluator"]["checker"]), str(policy["evaluator"]["builder"])


class ClaudeBaselineCampaign:
    """合成的 Claude Campaign 编排器侧：总计划、Claude 计时账本（推进到 VC-5、显式 r1）与评估基线目录。"""

    def __init__(self, root: Path, *, plan_established: bool = True) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.campaign_dir = root / "campaign"
        self.campaign_dir.mkdir(mode=0o700)
        self.ledger_dir = root / "UpgradeTimingLedger"
        timing.create_ledger(
            self.ledger_dir,
            started_at_utc=START,
            upgrade_id="claude-2-1-226-to-2-1-285",
            baseline_version="2.1.226",
            target_version="2.1.285",
            campaign_purpose="validation_only",
            evidence_decision="recapture",
            client="claude",
        )
        self.minute = self._to_vc5()
        checker_key, builder_key = _evaluator_keys()
        entries = [{"path": "official_client_capture/claude_upgrade.py", "sha256": "e1" * 32}]
        if plan_established:
            entries += [{"path": checker_key, "sha256": CHECKER}, {"path": builder_key, "sha256": BUILDER}]
        self.manifest = {
            "campaign_id": "claude-code-2_1_285-baseline-fixture",
            "campaign_mode": "formal",
            "control_receipts": {
                "upgrade_timing": {
                    "ledger_dir": str(self.ledger_dir.resolve()),
                    "ledger_plan_sha256": file_sha256(self.ledger_dir / "ledger.json"),
                }
            },
            "tool_identity": {
                "client": "claude",
                "entries": sorted(entries, key=lambda item: item["path"]),
                "policy_sha256": "a0" * 32,
                "wire_producer_sha256": "a1" * 32,
                "evidence_semantics_sha256": "a2" * 32,
            },
        }
        (self.campaign_dir / "campaign.json").write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")
        # b0 的候选阶段结果（b≥1 的 capture-candidate 以 reused 引用它）。
        self.candidate_result = self.campaign_dir / "candidates" / CANDIDATE / "result.json"
        self.candidate_result.parent.mkdir(parents=True)
        self.candidate_result.write_text(json.dumps({"status": "complete"}), encoding="utf-8")

    def _to_vc5(self) -> int:
        minute = 1
        timing.append_event(self.ledger_dir, event_id="c0", phase="VC-0", event_type="stage_completed", next_action="x", recorded_at_utc=_at(minute))
        for phase in ("VC-1", "VC-2", "VC-3"):
            minute += 1
            timing.append_event(self.ledger_dir, event_id=f"s-{phase}", phase=phase, event_type="stage_started", next_action="x", recorded_at_utc=_at(minute))
            minute += 1
            timing.append_event(self.ledger_dir, event_id=f"c-{phase}", phase=phase, event_type="stage_completed", next_action="x", recorded_at_utc=_at(minute))
        minute += 1
        timing.append_event(
            self.ledger_dir, event_id="r1", phase="VC-4", event_type="stage_revision", revision=1, candidate_id=CANDIDATE,
            revision_commit_sha256="a" * 64, next_action="x", recorded_at_utc=_at(minute),
        )
        timing.append_event(self.ledger_dir, event_id="s4", phase="VC-4", event_type="stage_started", next_action="x", recorded_at_utc=_at(minute + 1))
        timing.append_event(self.ledger_dir, event_id="c4", phase="VC-4", event_type="stage_completed", next_action="x", recorded_at_utc=_at(minute + 2))
        timing.append_event(self.ledger_dir, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x", recorded_at_utc=_at(minute + 3))
        return minute + 4

    def baseline_dir(self, number: int) -> Path:
        return self.campaign_dir / "candidates" / CANDIDATE / "revisions" / f"b{number}"

    def commit_baseline(
        self,
        number: int,
        *,
        previous: dict[str, Any] | None = None,
        current_digests: dict[str, str] | None = None,
        ledger: bool = True,
    ) -> dict[str, Any]:
        """写 b<number> 的 recovery.json、PREPARED、AUTHORIZATION、COMMIT（tool-evolution 种类），并（可选）登记账本事件。"""

        directory = self.baseline_dir(number)
        directory.mkdir(parents=True)
        previous_number = 0 if previous is None else int(previous["evaluation_baseline"])
        recovery = artifacts.build_evaluation_recovery(
            campaign_id=self.manifest["campaign_id"], candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=number,
            kind="tool-evolution", diagnosis={"path": f"candidates/{CANDIDATE}/revisions/b{number}/reevaluation.json", "sha256": "1" * 64},
            failure_source="tool-evolution", reuse_authority="none", root_cause_class="tool-evolution", root_cause_id="rc1-" + "0" * 20,
            failed_step=f"tool-evolution-{number:02d}", previous_baseline=previous_number,
            previous_baseline_commit_sha256=None if previous is None else previous["commit_sha256"],
            execute_rules=["SPEC-EP-006"], reuse_rules=[], execute_jobs=[], reuse_jobs=["job-a"], attempt_id=None, recovery_revision=None,
            fix_commit="a" * 40, deployment_receipt={"path": "/deploy/receipt.json", "sha256": "3" * 64}, evaluation_epoch=None,
            failed_evaluator_digests=DIGESTS, current_evaluator_digests=current_digests or dict(DIGESTS, checker_sha256="f6" * 32),
            reviewer="boss", approved_at_utc=_at(self.minute),
        )
        (directory / "recovery.json").write_text(json.dumps(recovery), encoding="utf-8")
        (directory / "PREPARED").write_text("{}", encoding="utf-8")
        (directory / "AUTHORIZATION").write_text("{}", encoding="utf-8")
        capture = baseline_module.stage_read_source(self.campaign_dir, CANDIDATE, previous_number, "capture-candidate") if previous_number else {
            "baseline_of_record": 0,
            "path": self.candidate_result,
            "sha256": file_sha256(self.candidate_result),
        }
        commit = artifacts.build_evaluation_baseline_commit(
            campaign_id=self.manifest["campaign_id"], candidate_id=CANDIDATE, candidate_revision=1, evaluation_baseline=number,
            kind="tool-evolution", recovery_sha256=recovery["recovery_sha256"], authorization_sha256="4" * 64,
            stage_sources={
                "capture-candidate": {
                    "source": "reused",
                    "baseline": int(capture["baseline_of_record"]),
                    "path": Path(capture["path"]).relative_to(self.campaign_dir).as_posix(),
                    "sha256": str(capture["sha256"]),
                },
                "compare": {"source": "local", "target": f"comparisons/{CANDIDATE}/revisions/b{number}/result.json"},
                "assertions": {"source": "local", "target": f"assertions/{CANDIDATE}/revisions/b{number}"},
                "accept": {"source": "local", "target": f"acceptance/{CANDIDATE}/revisions/b{number}/result.json"},
            },
            committed_at_utc=_at(self.minute + 1),
        )
        (directory / "COMMIT").write_text(json.dumps(commit), encoding="utf-8")
        if ledger:
            self.append_ledger_event(commit)
        return commit

    def append_ledger_event(self, commit: dict[str, Any]) -> None:
        self.minute += 2
        timing.append_event(
            self.ledger_dir, event_id=f"b{commit['evaluation_baseline']}", phase="VC-5", event_type="evaluation_baseline",
            candidate_id=CANDIDATE, evaluation_baseline=commit["evaluation_baseline"], baseline_commit_sha256=commit["commit_sha256"],
            baseline_kind=commit["kind"], next_action="x", recorded_at_utc=_at(self.minute),
        )


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
        self._temporary = tempfile.TemporaryDirectory(prefix="claude-baseline-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()
        active = self.tree.active()
        active.__enter__()
        self.addCleanup(active.__exit__, None, None, None)
        self.addCleanup(self.tree.restore)
        store_root = self.root / "store-fixture"
        store_root.mkdir(mode=0o700)
        self.store_fixture = SyntheticCampaign(store_root, schema="v2")
        self.store_fixture.bootstrap_and_campaign()
        self.store_fixture.discovery_and_evidence()
        self.store_fixture.profile_objects()
        self.store_fixture.profile_approve()
        self.store_fixture.strict_validate()
        self.store = self.store_fixture.store
        self.campaign = ClaudeBaselineCampaign(self.root)
        patcher = mock.patch.object(claude_upgrade_store, "open_campaign_store", side_effect=self._open_store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open_store(self, campaign_dir: Path, manifest: Any) -> tuple[Any, dict[str, Any]]:
        del campaign_dir, manifest
        return self.store, self.store.load_campaign(self.store_fixture.campaign_id)

    def store_baseline(self) -> dict[str, Any] | None:
        return store_receipts.current_evaluation_baseline(self.store, self.store.load_campaign(self.store_fixture.campaign_id))

    def baseline_facts(self) -> list[dict[str, Any]]:
        return store_receipts.evaluation_baseline_facts(self.store, self.store.load_campaign(self.store_fixture.campaign_id))


class ReadingAndStoreLayerTests(_BaselineCase):
    def test_b0_then_b1_two_registrations_converge(self) -> None:
        campaign_dir = self.campaign.campaign_dir
        self.assertEqual(claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE), (0, None))
        self.assertEqual(claude_upgrade._assertions_root(campaign_dir, CANDIDATE), campaign_dir / "assertions" / CANDIDATE)
        commit = self.campaign.commit_baseline(1)
        # 编排器先写、Store 后写：Store 还没有事实时当前基线拒绝，提示重跑收敛。
        self.assertEqual(baseline_module.orchestrator_current_baseline(campaign_dir, CANDIDATE), (1, commit))
        with self.assertRaisesRegex(ConfigurationError, "两份登记不一致.*重跑"):
            claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE)
        record = baseline_module.converge_store_baseline(campaign_dir, self.campaign.manifest, CANDIDATE)
        self.assertFalse(record["reused"])
        payload = record["payload"]
        self.assertEqual(
            (payload["baseline"], payload["baseline_commit_sha256"], payload["candidate_id"], payload["kind"], payload["supersedes"]),
            (1, commit["commit_sha256"], CANDIDATE, "tool-evolution", None),
        )
        self.assertEqual(payload["candidate_ref"], self.store_fixture.references["candidate-strict"])
        self.assertEqual(self.store_baseline(), {"baseline": 1, "baseline_commit_sha256": commit["commit_sha256"]})
        self.assertEqual(claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE), (1, commit))
        # 重跑复用原事实，不新增。
        again = baseline_module.converge_store_baseline(campaign_dir, self.campaign.manifest, CANDIDATE)
        self.assertTrue(again["reused"])
        self.assertEqual(len(self.baseline_facts()), 1)
        # b1 的断言目录是 COMMIT 冻结的 local 目标；结果字段走 v3。
        self.assertEqual(claude_upgrade._assertions_root(campaign_dir, CANDIDATE), campaign_dir / "assertions" / CANDIDATE / "revisions" / "b1")
        fields = baseline_module.result_baseline_fields(campaign_dir, CANDIDATE)
        self.assertEqual((fields["pair_schema"], fields["payload_fields"]), ("official-client-pair/v3", {"evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"]}))
        # 下一条基线 supersedes 上一条。
        commit2 = self.campaign.commit_baseline(2, previous=commit)
        record2 = baseline_module.converge_store_baseline(campaign_dir, self.campaign.manifest, CANDIDATE)
        self.assertEqual(record2["payload"]["supersedes"], record["fact_ref"])
        self.assertEqual(claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE), (2, commit2))

    def test_b0_result_fields_and_store_ahead_fails_closed(self) -> None:
        campaign_dir = self.campaign.campaign_dir
        fields = baseline_module.result_baseline_fields(campaign_dir, CANDIDATE)
        self.assertEqual((fields["evaluation_baseline"], fields["pair_schema"], fields["acceptance_schema"], fields["payload_fields"]), (0, "official-client-pair/v2", "official-client-acceptance/v2", {}))
        # Store 领先（编排器侧没有 b1）：登记顺序被破坏，失败关闭。
        self.store_fixture.open_evaluation_baseline(1)
        with self.assertRaisesRegex(ConfigurationError, "两份登记不一致"):
            claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE)

    def test_readers_match_codex(self) -> None:
        """读取判据与 Codex 同名函数逐项相同（同一组目录）。"""

        campaign_dir = self.campaign.campaign_dir
        commit = self.campaign.commit_baseline(1)
        abandoned = self.campaign.baseline_dir(3)
        abandoned.mkdir(parents=True)
        (abandoned / "PREPARED").write_text("{}", encoding="utf-8")
        (abandoned / "ABANDON").write_text("{}", encoding="utf-8")
        self.assertEqual(baseline_module.read_evaluation_baseline_commit(campaign_dir, CANDIDATE, 1), codex_upgrade._read_evaluation_baseline_commit(campaign_dir, CANDIDATE, 1))
        self.assertEqual(baseline_module.read_evaluation_baseline_commit(campaign_dir, CANDIDATE, 1), commit)
        self.assertEqual(claude_upgrade._load_evaluation_baseline_recovery(campaign_dir, CANDIDATE, 1), codex_upgrade._load_evaluation_baseline_recovery(campaign_dir, CANDIDATE, 1))
        states = claude_upgrade._evaluation_baseline_states(campaign_dir, CANDIDATE)
        self.assertEqual(states, codex_upgrade._evaluation_baseline_states(campaign_dir, CANDIDATE))
        self.assertEqual(states[3], {"prepared": True, "authorization": False, "commit": False, "abandon": True})
        for stage in ("capture-candidate", "compare", "assertions", "accept"):
            with self.subTest(stage=stage):
                self.assertEqual(claude_upgrade._stage_read_source(campaign_dir, CANDIDATE, 1, stage), codex_upgrade._stage_read_source(campaign_dir, CANDIDATE, 1, stage))
        for stage in ("compare", "assertions", "accept"):
            with self.subTest(write=stage):
                self.assertEqual(claude_upgrade._stage_write_target(campaign_dir, CANDIDATE, 1, stage), codex_upgrade._stage_write_target(campaign_dir, CANDIDATE, 1, stage))
        with self.assertRaisesRegex(ConfigurationError, "reused），禁止写入"):
            claude_upgrade._stage_write_target(campaign_dir, CANDIDATE, 1, "capture-candidate")
        # 被引用的 b0 结果漂移：reused 读取失败关闭。
        self.campaign.candidate_result.write_text(json.dumps({"status": "changed"}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "缺失或摘要漂移"):
            claude_upgrade._stage_read_source(campaign_dir, CANDIDATE, 1, "capture-candidate")

    def test_outputs_and_tampered_commit(self) -> None:
        campaign_dir = self.campaign.campaign_dir
        self.assertEqual(claude_upgrade._evaluation_baseline_has_outputs(campaign_dir, CANDIDATE, 0), [])
        b0_results = campaign_dir / "assertions" / CANDIDATE / "results.json"
        b0_results.parent.mkdir(parents=True)
        b0_results.write_text("{}", encoding="utf-8")
        self.assertEqual(claude_upgrade._evaluation_baseline_has_outputs(campaign_dir, CANDIDATE, 0), [f"assertions/{CANDIDATE}/results.json"])
        commit = self.campaign.commit_baseline(1)
        baseline_module.converge_store_baseline(campaign_dir, self.campaign.manifest, CANDIDATE)
        self.assertEqual(claude_upgrade._evaluation_baseline_has_outputs(campaign_dir, CANDIDATE, 1), [])
        local = campaign_dir / "comparisons" / CANDIDATE / "revisions" / "b1" / "result.json"
        local.parent.mkdir(parents=True)
        local.write_text("{}", encoding="utf-8")
        self.assertEqual(claude_upgrade._evaluation_baseline_has_outputs(campaign_dir, CANDIDATE, 1), [f"comparisons/{CANDIDATE}/revisions/b1/result.json"])
        # COMMIT 被改：自摘要不符失败关闭。
        path = self.campaign.baseline_dir(1) / "COMMIT"
        path.write_text(json.dumps(dict(commit, committed_at_utc=_at(999))), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "COMMIT 无法校验"):
            claude_upgrade._current_evaluation_baseline(campaign_dir, CANDIDATE)


class EvaluationRecoverWrapperTests(_BaselineCase):
    def arguments(self, action: str, *, approve: str | None = "9" * 64) -> argparse.Namespace:
        return argparse.Namespace(
            command="evaluation-recover", recover_action=action, campaign_dir=self.campaign.campaign_dir, candidate_id=CANDIDATE,
            reviewer="boss", approve_sha256=approve, root_cause_class=None,
        )

    def run_wrapper(self, action: str, shared: Any, *, approve: str | None = "9" * 64) -> dict[str, Any]:
        with mock.patch.object(claude_upgrade, "_require_formal_campaign", return_value=dict(self.campaign.manifest)), mock.patch.object(
            upgrade_control_plane, "evaluation_recover", side_effect=shared
        ):
            return baseline_module.evaluation_recover(self.arguments(action, approve=approve))

    def test_applied_reevaluate_records_store_fact(self) -> None:
        commits: list[dict[str, Any]] = []

        def shared(arguments: argparse.Namespace) -> dict[str, Any]:
            commits.append(self.campaign.commit_baseline(1))
            return {"status": "applied", "evaluation_baseline": 1}

        result = self.run_wrapper("reevaluate", shared)
        self.assertEqual(result["store"]["payload"]["baseline_commit_sha256"], commits[0]["commit_sha256"])
        self.assertFalse(result["store"]["reused"])
        self.assertEqual(claude_upgrade._current_evaluation_baseline(self.campaign.campaign_dir, CANDIDATE), (1, commits[0]))

    def test_preview_does_not_touch_store(self) -> None:
        result = self.run_wrapper("reevaluate", lambda arguments: {"status": "preview"}, approve=None)
        self.assertEqual(result["status"], "preview")
        self.assertNotIn("store", result)
        self.assertEqual(self.baseline_facts(), [])

    def test_precheck_rejects_before_any_write(self) -> None:
        shared = mock.Mock(return_value={"status": "applied"})
        no_attempt = [
            fact for fact in self.store.list_facts(self.store_fixture.campaign_id, "validation") if fact["fact_kind"] != "validation_attempt_created"
        ]
        with mock.patch.object(baseline_module, "_validation_facts", return_value=no_attempt):
            with self.assertRaisesRegex(ConfigurationError, "尚未进入 VC-5（没有 ValidationAttempt）"):
                self.run_wrapper("reevaluate", shared)
        shared.assert_not_called()
        with mock.patch.object(baseline_module, "_store_candidate_fact", return_value=None):
            with self.assertRaisesRegex(ConfigurationError, "没有候选 .*的冻结事实"):
                self.run_wrapper("apply", shared)
        shared.assert_not_called()
        self.assertEqual(self.baseline_facts(), [])

    def test_kill_between_ledger_and_store_converges_on_rerun(self) -> None:
        def committed_then_killed(arguments: argparse.Namespace) -> dict[str, Any]:
            self.campaign.commit_baseline(1)
            return {"status": "applied"}

        with mock.patch.object(baseline_module, "converge_store_baseline", side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.run_wrapper("reevaluate", committed_then_killed)
        with self.assertRaisesRegex(ConfigurationError, "两份登记不一致"):
            claude_upgrade._current_evaluation_baseline(self.campaign.campaign_dir, CANDIDATE)

        def rejected(arguments: argparse.Namespace) -> dict[str, Any]:
            raise ConfigurationError("review_sha256 与当前预览不一致")

        with self.assertRaisesRegex(ConfigurationError, r"已补写 Store 评估基线 b1（两份登记现已一致）；随后共享流程拒绝：review_sha256"):
            self.run_wrapper("reevaluate", rejected)
        commit = baseline_module.read_evaluation_baseline_commit(self.campaign.campaign_dir, CANDIDATE, 1)
        self.assertEqual(claude_upgrade._current_evaluation_baseline(self.campaign.campaign_dir, CANDIDATE), (1, commit))
        self.assertEqual(len(self.baseline_facts()), 1)


class EvaluatorAuthorizationTests(_BaselineCase):
    def evolution(self, index: int, to: dict[str, Any], *, candidates: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"index": index, "evaluator": {"to": to, "candidates": dict(candidates or {})}}

    @staticmethod
    def established(checker: str, builder: str) -> dict[str, Any]:
        return {"status": "established", "checker_sha256": checker, "builder_sha256": builder}

    @staticmethod
    def not_established() -> dict[str, Any]:
        return {"status": "not_established", "checker_sha256": None, "builder_sha256": None}

    def history(self, chain: list[dict[str, Any]], manifest: dict[str, Any] | None = None) -> list[dict[str, str]]:
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=chain):
            return claude_upgrade._b0_evaluator_authorized_digest_history(self.campaign.campaign_dir, manifest or self.campaign.manifest, CANDIDATE)

    def shared_history(self, chain: list[dict[str, Any]], manifest: dict[str, Any] | None = None) -> list[dict[str, str]]:
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=chain), upgrade_control_plane.adapter_scope(uca.adapter_for("claude")):
            return upgrade_control_plane._b0_evaluator_authorized_digest_history(self.campaign.campaign_dir, manifest or self.campaign.manifest, CANDIDATE)

    def test_history_matches_shared_implementation_when_all_established(self) -> None:
        """风险③合同：演进链全部已建立时，to 投影成两键交给共享实现，算出的历史与 Claude 实现逐字相同。"""

        chain = [
            self.evolution(1, self.established("e1" * 32, "f1" * 32)),
            self.evolution(2, self.established("e2" * 32, "f2" * 32), candidates={CANDIDATE: {"b0_authorization_moved": False}}),
            self.evolution(3, self.established("e3" * 32, "f3" * 32), candidates={CANDIDATE: {"b0_authorization_moved": True}}),
            self.evolution(4, self.established("e1" * 32, "f1" * 32), candidates={"other": {"b0_authorization_moved": False}}),
        ]
        projected = [
            dict(item, evaluator=dict(item["evaluator"], to={key: value for key, value in item["evaluator"]["to"].items() if key != "status"}))
            for item in chain
        ]
        expected = [
            {"checker_sha256": CHECKER, "builder_sha256": BUILDER},
            {"checker_sha256": "e1" * 32, "builder_sha256": "f1" * 32},
            {"checker_sha256": "e3" * 32, "builder_sha256": "f3" * 32},
        ]
        self.assertEqual(self.history(chain), expected)
        self.assertEqual(self.shared_history(projected), expected)
        # 原样（带 status）交给共享实现必然被判非法：这正是 Claude 不转调它的原因。
        with self.assertRaisesRegex(ConfigurationError, "评估器 to 摘要非法"):
            self.shared_history(chain)
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=chain):
            self.assertEqual(
                baseline_module.b0_evaluator_authorized_digests(self.campaign.campaign_dir, self.campaign.manifest, CANDIDATE),
                {"checker_sha256": "e1" * 32, "builder_sha256": "f1" * 32},
            )

    def test_not_established_plan_and_evolutions(self) -> None:
        unestablished = ClaudeBaselineCampaign(self.root / "late", plan_established=False)
        manifest = unestablished.manifest
        with self.assertRaisesRegex(ConfigurationError, "plan 时尚未建立"):
            claude_upgrade._plan_evaluator_entry_digests(manifest)
        chain = [self.evolution(1, self.not_established()), self.evolution(2, self.established("e2" * 32, "f2" * 32))]
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=chain):
            self.assertEqual(
                claude_upgrade._b0_evaluator_authorized_digest_history(unestablished.campaign_dir, manifest, CANDIDATE),
                [{"checker_sha256": "e2" * 32, "builder_sha256": "f2" * 32}],
            )
            self.assertEqual(
                claude_upgrade._authorized_evaluator_digests(unestablished.campaign_dir, manifest, CANDIDATE, 0),
                {"checker_sha256": "e2" * 32, "builder_sha256": "f2" * 32},
            )
        # 最新适用的迁移是「尚未建立」：b0 没有授权口径；历史里也没有任何授权即拒绝。
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=[self.evolution(1, self.not_established())]):
            with self.assertRaisesRegex(ConfigurationError, "尚未建立"):
                baseline_module.b0_evaluator_authorized_digests(unestablished.campaign_dir, manifest, CANDIDATE)
            with self.assertRaisesRegex(ConfigurationError, "没有任何评估器授权"):
                claude_upgrade._b0_evaluator_authorized_digest_history(unestablished.campaign_dir, manifest, CANDIDATE)
        # 形态非法的 to 失败关闭。
        bad = self.evolution(1, {"status": "established", "checker_sha256": None, "builder_sha256": None})
        with self.assertRaisesRegex(ConfigurationError, "评估器 to 摘要非法"):
            self.history([bad])
        with self.assertRaisesRegex(ConfigurationError, "Campaign 总计划必须是对象"):
            claude_upgrade._b0_evaluator_authorized_digest_history(self.campaign.campaign_dir, object(), CANDIDATE)  # type: ignore[arg-type]

    def test_authorized_digests_and_verification(self) -> None:
        campaign_dir = self.campaign.campaign_dir
        manifest = self.campaign.manifest
        with mock.patch.object(claude_upgrade, "_campaign_tool_evolutions", return_value=[]):
            self.assertEqual(claude_upgrade._authorized_evaluator_digests(campaign_dir, manifest, CANDIDATE, 0), {"checker_sha256": CHECKER, "builder_sha256": BUILDER})
            claude_upgrade._verify_evaluator_digests_authorized(campaign_dir, manifest, CANDIDATE, 0, DIGESTS, label="编译冻结 evaluator 摘要")
            with self.assertRaisesRegex(ConfigurationError, r"编译冻结 evaluator 摘要：evaluator 摘要与b0 授权口径.*checker_sha256"):
                claude_upgrade._verify_evaluator_digests_authorized(campaign_dir, manifest, CANDIDATE, 0, dict(DIGESTS, checker_sha256="0" * 64), label="编译冻结 evaluator 摘要")
        current = dict(DIGESTS, checker_sha256="f6" * 32)
        self.campaign.commit_baseline(1, current_digests=current)
        self.assertEqual(claude_upgrade._authorized_evaluator_digests(campaign_dir, manifest, CANDIDATE, 1), current)
        claude_upgrade._verify_evaluator_digests_authorized(campaign_dir, manifest, CANDIDATE, 1, current, label="核对")
        with self.assertRaisesRegex(ConfigurationError, "b1 recovery.json 授权不一致（accept_reader_sha256）"):
            claude_upgrade._verify_evaluator_digests_authorized(campaign_dir, manifest, CANDIDATE, 1, dict(current, accept_reader_sha256="0" * 64), label="核对")


class EvolutionCandidateTests(_BaselineCase):
    def test_candidates_and_b0_authorization_records(self) -> None:
        campaign_dir = self.campaign.campaign_dir
        superseded = campaign_dir / "candidates" / "old-candidate"
        superseded.mkdir(parents=True)
        (superseded / "superseded-by.json").write_text("{}", encoding="utf-8")
        invalidated = campaign_dir / "candidates" / "bad-candidate"
        invalidated.mkdir(parents=True)
        (invalidated / "invalidation.json").write_text("{}", encoding="utf-8")
        active, inactive = baseline_module.evolution_candidates(campaign_dir)
        self.assertEqual((active, inactive), ([CANDIDATE], ["bad-candidate", "old-candidate"]))
        self.assertFalse(baseline_module.candidate_has_capture_attempts(campaign_dir, CANDIDATE))
        records = baseline_module.evaluator_candidate_records(campaign_dir, active)
        self.assertEqual(records, {CANDIDATE: {"current_evaluation_baseline": 0, "b0_evaluation_outputs": [], "b0_authorization_moved": True}})
        results = campaign_dir / "assertions" / CANDIDATE / "evaluation-run.json"
        results.parent.mkdir(parents=True)
        results.write_text("{}", encoding="utf-8")
        records = baseline_module.evaluator_candidate_records(campaign_dir, active)
        self.assertEqual(records[CANDIDATE]["b0_authorization_moved"], False)
        self.assertEqual(records[CANDIDATE]["b0_evaluation_outputs"], [f"assertions/{CANDIDATE}/evaluation-run.json"])
        attempts = campaign_dir / "candidates" / CANDIDATE / "attempts" / "attempt-001"
        attempts.mkdir(parents=True)
        self.assertTrue(baseline_module.candidate_has_capture_attempts(campaign_dir, CANDIDATE))


if __name__ == "__main__":
    unittest.main()
