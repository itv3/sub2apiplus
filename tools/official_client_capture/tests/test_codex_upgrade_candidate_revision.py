"""改造 2（候选级 revision）：revision-open／invalidate-candidate／revision-seal 与集成链。

集成夹具复用 ``test_codex_upgrade.CodexUpgradeTest._vc_chain_fixture``（只读导入形态的 0.154
Formal Campaign），阶段动作用合成脚本（子进程内以当前工具封存 checkpoint）；候选级阶段
的 checkpoint 路径、批次绑定、账本事件与总账入账全部走真实入口。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import test_codex_upgrade

ORDER = artifacts.VC_PHASES
R1 = "candidate-r1"
R2 = "candidate-r2"
R3 = "candidate-r3"


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class _ChainMixin:
    """集成用例共用的派发工具：按当前 revision 解析前序 checkpoint、可定制动作 id。"""

    helper: test_codex_upgrade.CodexUpgradeTest

    def _fixture(self, root: Path) -> dict[str, object]:
        return self.helper._vc_chain_fixture(root)

    @staticmethod
    def _events(fixture: dict[str, object]) -> list[tuple[str, str]]:
        return [
            (str(event["event_type"]), str(event["event_id"]))
            for event, _raw in timing_ledger._load_events(Path(str(fixture["timing_ledger"])))
        ]

    @staticmethod
    def _head(fixture: dict[str, object]) -> dict[str, object]:
        return project_ledger.replay_head(Path(str(fixture["ledger"])))

    @staticmethod
    def _summary(fixture: dict[str, object]) -> dict[str, object]:
        return timing_ledger.inspect_ledger(Path(str(fixture["timing_ledger"])))

    def _plan(self, root: Path, campaign_dir: Path, phase: str, *, tag: str, fail: bool = False, action_id: str | None = None) -> Path:
        path = self.helper._vc_chain_action_plan(root / f"plans-{tag}", campaign_dir, phase, fail=fail)
        if action_id is None:
            return path
        # 定制动作 id：同阶段第二次失败要落到不同的 supervisor 根因（账本 failed_step 取动作
        # id）；失败脚本同时写带枚举观测的诊断，让对账收据的根因也按观测区分而不是落到
        # 同一条 legacy ``supervisor-run.interrupted``。
        plan = _read(path)
        plan["execute_item_ids"] = [action_id]
        plan["actions"][0]["action_id"] = action_id
        plan["actions"][0]["item_ids"] = [action_id]
        plan["actions"][0]["operation"] = f"{phase}:{action_id}"
        if fail:
            repo_root = Path(codex_upgrade.__file__).resolve().parents[2]
            script = (
                "import sys\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "from tools.official_client_capture import codex_upgrade_supervisor as supervisor\n"
                "class SyntheticFailure(RuntimeError):\n"
                "    failure_observations = [{'check_id': 'synthetic-check', 'failure_code': sys.argv[1]}]\n"
                "supervisor.write_campaign_run_action_diagnostic(failure_kind='handled-error', error=SyntheticFailure('synthetic'))\n"
                "sys.exit(3)\n"
            )
            plan["actions"][0]["command"] = [sys.executable, "-c", script, action_id, str(repo_root)]
        target = path.with_name(f"{path.stem}-{action_id}.json")
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o600)
        return target

    def _arguments(self, fixture: dict[str, object], phase: str, sequence: int, action_plan: Path) -> argparse.Namespace:
        campaign_dir = Path(str(fixture["campaign_dir"]))
        manifest = codex_upgrade._require_formal_campaign(campaign_dir)
        predecessor = ORDER[ORDER.index(phase) - 1]
        revision = (
            codex_upgrade._current_candidate_revision(campaign_dir, manifest)
            if predecessor in codex_upgrade.CANDIDATE_VC_PHASES
            else None
        )
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            state_dir=fixture["state_dir"],
            phase=phase,
            sequence=sequence,
            predecessor_checkpoint=codex_upgrade._vc_checkpoint_path(campaign_dir, predecessor, revision=revision),
            action_plan=action_plan,
            heartbeat_seconds=0.2,
            watchdog_timeout_seconds=5.0,
            ledger_interval_seconds=0.2,
        )

    def _dispatch(self, fixture: dict[str, object], root: Path, phase: str, sequence: int, *, tag: str, fail: bool = False, action_id: str | None = None):
        campaign_dir = Path(str(fixture["campaign_dir"]))
        plan = self._plan(root, campaign_dir, phase, tag=tag, fail=fail, action_id=action_id)
        return codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, phase, sequence, plan))

    def _open(self, fixture: dict[str, object], candidate_id: str, *, initial: bool = False, supersedes: str | None = None) -> dict[str, object]:
        return codex_upgrade.open_candidate_revision(
            argparse.Namespace(
                campaign_dir=Path(str(fixture["campaign_dir"])),
                candidate_id=candidate_id,
                initial=initial,
                supersedes=supersedes,
            )
        )

    @staticmethod
    def _invalidate_arguments(fixture: dict[str, object], candidate_id: str, action: str, *, approve: str | None = None, source: Path | None = None, evidence: list[Path] | None = None) -> argparse.Namespace:
        return argparse.Namespace(
            campaign_dir=Path(str(fixture["campaign_dir"])),
            candidate_id=candidate_id,
            invalidate_action=action,
            reviewer="boss",
            evidence=[str(item) for item in (evidence or [])],
            candidate_source=source,
            approve_sha256=approve,
        )

    @staticmethod
    def _candidate_source(root: Path, name: str) -> Path:
        source = root / "sources" / name
        source.mkdir(parents=True, mode=0o700)
        (source / "main.go").write_text(f"package main // {name}\n", encoding="utf-8")
        return source

    def _advance_to_vc3(self, fixture: dict[str, object], root: Path) -> None:
        for sequence, phase in ((2, "VC-2"), (3, "VC-3")):
            result, returncode = self._dispatch(fixture, root, phase, sequence, tag=phase)
            self.assertEqual(returncode, 0, result)  # type: ignore[attr-defined]

    def _fail_vc5_into_review(self, fixture: dict[str, object], root: Path, *, sequence: int, tag: str, action_id: str | None = None) -> Path:
        """VC-5 动作失败 → stage_abandoned + candidate_review_required；返回失败父 run 目录。"""

        result, returncode = self._dispatch(fixture, root, "VC-5", sequence, tag=tag, fail=True, action_id=action_id)
        self.assertEqual(returncode, 1)  # type: ignore[attr-defined]
        self.assertEqual(result["status"], "failed")  # type: ignore[attr-defined]
        closeout = result["campaign_run"]["timing_closeout"]
        self.assertEqual(closeout["ledger_status"], "candidate_review_required", closeout)  # type: ignore[attr-defined]
        return Path(str(result["campaign_run"]["run_dir"]))


class CandidateRevisionIntegrationTests(_ChainMixin, unittest.TestCase):
    """T2.9：候选作废 → 新 revision → 最终阶段。"""

    def setUp(self) -> None:
        super().setUp()
        self.helper = test_codex_upgrade.CodexUpgradeTest(
            "test_bound_evidence_path_accepts_legacy_attempt_relative_binding"
        )
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    def test_invalidate_then_supersede_reaches_vc6_on_r2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            self._advance_to_vc3(fixture, root)

            # ---- 新 Campaign 无 active revision：VC-4 首批与 --supersedes 都被拒 ----
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision-open --initial"):
                self._dispatch(fixture, root, "VC-4", 4, tag="vc4-early")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "--initial"):
                self._open(fixture, R2, supersedes=R1)
            opened = self._open(fixture, R1, initial=True)
            self.assertEqual((opened["revision"], opened["idempotent"], opened["supersedes"]), (1, False, None))
            again = self._open(fixture, R1, initial=True)
            self.assertEqual((again["revision"], again["idempotent"]), (1, True))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "只能幂等重放 r1"):
                self._open(fixture, "candidate-other", initial=True)

            # ---- r1：VC-4 成功（原路径 checkpoint），VC-5 动作失败 → candidate_review_required ----
            result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
            self.assertEqual(returncode, 0, result)
            self.assertTrue((campaign_dir / "control" / "vc" / "vc-4-checkpoint.json").is_file())
            batch_r1 = _read(campaign_dir / "control" / "vc" / "batches" / "0004-vc-4.json")
            self.assertEqual((batch_r1["schema_version"], batch_r1["candidate_revision"], batch_r1["candidate_id"]), (artifacts.VC_BATCH_SCHEMA, 1, R1))
            manifest_r1 = _read(campaign_dir / "control" / "vc" / "run-manifests" / "0004-vc-4.json")
            self.assertEqual((manifest_r1["candidate_revision"], manifest_r1["candidate_id"]), (1, R1))
            failed_run = self._fail_vc5_into_review(fixture, root, sequence=5, tag="vc5-r1-fail")
            summary = self._summary(fixture)
            self.assertEqual((summary["status"], summary["active_phase"], summary["current_revision"]), ("candidate_review_required", None, 1))
            self.assertEqual(summary["revision_phase_state"], {"1": {"VC-4": "completed", "VC-5": "abandoned"}})
            # review 期间：派发被拒、直接 --supersedes 被拒（账本不在 revision_required）。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "candidate_review_required"):
                self._dispatch(fixture, root, "VC-5", 6, tag="vc5-review")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision_required"):
                self._open(fixture, R2, supersedes=R1)
            # 作废前提：失败父 run 未对账入账 → preview 拒绝。
            source_r1 = self._candidate_source(root, "r1")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "reconcile-supervisor-run"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_r1))

            # ---- 对账入账（账本仍 review；无 receipt_passed）----
            outcome = reconciler.reconcile_supervisor_run(failed_run, campaign_dir)
            self.assertEqual(outcome["status"], "recoverable")
            self.assertIn("invalidate-candidate", outcome["next_command"])
            self.assertEqual(outcome["ledger_events"], [])
            self.assertIn(f"reconcile-supervisor-run:{failed_run.name}", self._head(fixture)["operations"])
            self.assertEqual(self._summary(fixture)["status"], "candidate_review_required")

            # ---- invalidate-candidate preview → apply ----
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                # 旧候选既无 build receipt 也无 attempt：必须给 --candidate-source。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "--candidate-source"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview"))
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_r1))
                self.assertEqual((preview["status"], preview["revision"], preview["ledger_status"]), ("preview", 1, "candidate_review_required"))
                self.assertEqual(
                    [(item["run_id"], item["reconciliation"]) for item in preview["accounting"]["failed_runs"]],
                    [(failed_run.name, "supervisor-run")],
                )
                self.assertFalse(preview["accounting"]["zero_request"])
                self.assertEqual(preview["diagnosis"]["identity_snapshot"]["snapshot_sources"], ["candidate_source"])
                self.assertFalse((campaign_dir / "candidates" / R1 / "invalidation.json").exists())
                # apply 必须带 preview 的批准摘要；摘要错误拒绝且不落盘。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "--approve-sha256"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", source=source_r1))
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "重新 preview"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve="0" * 64, source=source_r1))
                self.assertFalse((campaign_dir / "candidates" / R1 / "invalidation.json").exists())
                applied = codex_upgrade.invalidate_candidate(
                    self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_r1)
                )
            self.assertEqual((applied["status"], applied["idempotent"], applied["decision"]["decision"]), ("applied", False, reconciler.DECISION_RECOVERABLE))
            invalidation_cause = root_cause.structured_root_cause(
                component="candidate",
                stable_error_code="candidate.source-change-required",
                failed_step="candidate-source-change-vc-5",
                stable_dimensions={"phase": "VC-5"},
            )
            self.assertEqual(applied["root_cause_id"], invalidation_cause)
            self.assertEqual([item["event_type"] for item in applied["ledger_events"]], ["candidate_invalidated"])
            invalidation_path = campaign_dir / "candidates" / R1 / "invalidation.json"
            invalidation = artifacts.validate_candidate_invalidation(_read(invalidation_path))
            self.assertEqual((invalidation["candidate_id"], invalidation["revision"], invalidation["identity_snapshot"]["git_commit"]), (R1, 1, "a" * 40))
            head = self._head(fixture)
            self.assertEqual(head["root_cause_counts"][invalidation_cause], 1)
            self.assertIn(f"invalidate-candidate:{R1}:r1", head["operations"])
            summary = self._summary(fixture)
            self.assertEqual((summary["status"], summary["current_revision"]), ("revision_required", 1))
            # 幂等：再次 apply 不重复入账、不重复写事件。
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                replay = codex_upgrade.invalidate_candidate(
                    self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_r1)
                )
            self.assertTrue(replay["idempotent"])
            self.assertEqual(self._head(fixture)["root_cause_counts"][invalidation_cause], 1)
            self.assertEqual(self._summary(fixture)["status"], "revision_required")
            # revision_required 期间不得派发；被作废候选只读。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision_required"):
                self._dispatch(fixture, root, "VC-4", 6, tag="vc4-blocked")

            # ---- revision-open --supersedes → r2 ----
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "同名"):
                self._open(fixture, R1, supersedes=R1)
            opened = self._open(fixture, R2, supersedes=R1)
            self.assertEqual((opened["revision"], opened["idempotent"]), (2, False))
            self.assertEqual(opened["supersedes"]["candidate_id"], R1)
            record_r2, commit_r2 = codex_upgrade._read_candidate_revision_record(campaign_dir, 2)
            self.assertEqual((record_r2["candidate_id"], record_r2["supersedes"]["revision"]), (R2, 1))
            self.assertEqual(commit_r2["record_sha256"], record_r2["record_sha256"])
            self.assertEqual((self._summary(fixture)["status"], self._summary(fixture)["current_revision"]), ("active", 2))
            self.assertEqual(self._open(fixture, R2, supersedes=R1)["idempotent"], True)
            # r2 已激活（账本 active）：换一个候选再 --supersedes 被拒。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision_required"):
                self._open(fixture, R3, supersedes=R1)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "被取代候选只读|不属于当前 revision"):
                codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R1, action="compare")

            # ---- r2：VC-4 首批（checkpoint 落到 revisions/r2）→ seal → VC-5 → VC-6 ----
            result, returncode = self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2")
            self.assertEqual(returncode, 0, result)
            r2_dir = campaign_dir / "control" / "vc" / "revisions" / "r2"
            self.assertTrue((r2_dir / "vc-4-checkpoint.json").is_file())
            batch_r2 = _read(campaign_dir / "control" / "vc" / "batches" / "0006-vc-4.json")
            self.assertEqual((batch_r2["candidate_revision"], batch_r2["candidate_id"]), (2, R2))
            self.assertEqual(batch_r2["predecessor_checkpoint"]["path"], "control/vc/vc-3-checkpoint.json")
            # r1 的 VC-4 checkpoint 原样只读。
            self.assertEqual(_read(campaign_dir / "control" / "vc" / "vc-4-checkpoint.json")["checkpoint_sha256"],
                             codex_upgrade._replay_vc_checkpoint(campaign_dir, codex_upgrade._vc_campaign_plan(campaign_dir, manifest), "VC-4", revision=1)[1]["checkpoint_sha256"])
            # record-candidate-build 的 revision-seal 段：同一性变化（commit 不同）→ seal.json + superseded-by.json。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "全部相同|同一性"):
                codex_upgrade._seal_candidate_revision(
                    campaign_dir,
                    record_r2,
                    candidate_commit="a" * 40,
                    source_tree_sha256=str(invalidation["identity_snapshot"]["source_tree_sha256"]),
                    image_id="sha256:image-r2",
                    build_receipt_sha256="c" * 64,
                    vc3_stage_receipt_sha256=str(record_r2["vc3_stage_receipt"]["sha256"]),
                )
            self.assertFalse((r2_dir / "seal.json").exists())
            seal = codex_upgrade._seal_candidate_revision(
                campaign_dir,
                record_r2,
                candidate_commit="b" * 40,
                source_tree_sha256="d" * 64,
                image_id="sha256:image-r2",
                build_receipt_sha256="c" * 64,
                vc3_stage_receipt_sha256=str(record_r2["vc3_stage_receipt"]["sha256"]),
            )
            self.assertEqual((seal["identity_change"]["git_commit_changed"], seal["identity_change"]["source_tree_changed"]), (True, True))
            self.assertEqual(seal["superseded"]["candidate_id"], R1)
            self.assertTrue((campaign_dir / "candidates" / R1 / "superseded-by.json").is_file())
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "被取代候选只读"):
                codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R1, action="accept")
            for sequence, phase in ((7, "VC-5"), (8, "VC-6")):
                result, returncode = self._dispatch(fixture, root, phase, sequence, tag=f"{phase}-r2")
                self.assertEqual(returncode, 0, result)
                self.assertTrue((r2_dir / f"{phase.lower()}-checkpoint.json").is_file(), phase)
                self.assertFalse((campaign_dir / "control" / "vc" / f"{phase.lower()}-checkpoint.json").exists(), phase)
            batch_vc5 = _read(campaign_dir / "control" / "vc" / "batches" / "0007-vc-5.json")
            self.assertEqual(batch_vc5["predecessor_checkpoint"]["path"], "control/vc/revisions/r2/vc-4-checkpoint.json")

            # ---- 终态断言：账本序列、revision 状态、总账、状态输出 ----
            summary = self._summary(fixture)
            self.assertEqual((summary["status"], summary["current_revision"], summary["active_phase"]), ("active", 2, None))
            self.assertEqual(
                summary["revision_phase_state"],
                {"1": {"VC-4": "completed", "VC-5": "abandoned"}, "2": {"VC-4": "completed", "VC-5": "completed", "VC-6": "completed"}},
            )
            self.assertEqual(summary["campaign_completed_phases"], ["VC-0", "VC-1", "VC-2", "VC-3"])
            state = timing_ledger.phase_ledger_state(Path(str(fixture["timing_ledger"])))
            self.assertEqual(state["completed_phases"], list(ORDER))
            events = self._events(fixture)
            tail = events[events.index(("stage_completed", "vc-batch-0003-vc-3-completed")) + 1 :]
            self.assertEqual(
                [event_type for event_type, _ in tail],
                [
                    "stage_revision",
                    "stage_started", "stage_completed",
                    "stage_started", "stage_abandoned", "candidate_review_required",
                    "candidate_invalidated",
                    "stage_revision",
                    "stage_started", "stage_completed",
                    "stage_started", "stage_completed",
                    "stage_started", "stage_completed",
                ],
            )
            self.assertEqual(tail[0], ("stage_revision", "stage-revision-r1"))
            self.assertEqual(tail[6], ("candidate_invalidated", f"candidate-invalidated-{R1}-r1"))
            self.assertEqual(tail[7], ("stage_revision", "stage-revision-r2"))
            self.assertEqual(tail[-1], ("stage_completed", "vc-batch-0008-vc-6-completed"))
            status = codex_upgrade.campaign_status(campaign_dir)
            revisions = status["candidate_revisions"]
            self.assertEqual(revisions["current_revision"], 2)
            self.assertEqual(revisions["revisions"]["r1"]["state"], "superseded")
            self.assertEqual((revisions["revisions"]["r2"]["state"], revisions["revisions"]["r2"]["sealed"]), ("active", True))
            self.assertEqual(revisions["revisions"]["r2"]["checkpoints"], ["vc-4-checkpoint.json", "vc-5-checkpoint.json", "vc-6-checkpoint.json"])
            # 已封存阶段不得在同 revision 重开。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "禁止重开"):
                self._dispatch(fixture, root, "VC-6", 9, tag="vc6-again")

    def _r1_invalidated(self, fixture: dict[str, object], root: Path) -> tuple[Path, Path, dict[str, object]]:
        """r1：VC-4 成功、VC-5 失败 → 对账 → invalidate apply；返回 (campaign_dir, 失败 run, apply 结果)。"""

        campaign_dir = Path(str(fixture["campaign_dir"]))
        self._advance_to_vc3(fixture, root)
        self._open(fixture, R1, initial=True)
        result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
        self.assertEqual(returncode, 0, result)
        failed_run = self._fail_vc5_into_review(fixture, root, sequence=5, tag="vc5-r1-fail")
        self.assertEqual(reconciler.reconcile_supervisor_run(failed_run, campaign_dir)["status"], "recoverable")
        source = self._candidate_source(root, "r1")
        with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
            preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source))
            applied = codex_upgrade.invalidate_candidate(
                self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source)
            )
        self.assertEqual(applied["status"], "applied")
        return campaign_dir, failed_run, applied

    def test_invalidated_candidate_is_read_only_and_writes_require_active_ledger(self) -> None:
        """审核阻断 1：作废候选（revision_required 窗口）与非 active 账本下的候选写入门都必须拒绝。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir, _failed_run, _applied = self._r1_invalidated(fixture, root)
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            self.assertEqual(self._summary(fixture)["status"], "revision_required")
            # r1 已有 invalidation.json 但尚未被 r2 取代：一切候选级写入口都只读。
            for action in ("compare", "accept", "deliver-candidate", "capture-candidate run", "record-candidate-build"):
                with self.subTest(action=action), self.assertRaisesRegex(codex_upgrade.ConfigurationError, "invalidation.json|作废|只读"):
                    codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R1, action=action)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "invalidation.json|作废|只读|active"):
                codex_upgrade._guard_candidate_revision_write(campaign_dir, manifest, R1, action="compare")
            status = codex_upgrade.campaign_status(campaign_dir)["candidate_revisions"]
            self.assertEqual(status["revisions"]["r1"]["state"], "invalidated")
            # r2 激活后账本 active：r2 可写、r1 仍只读。
            self._open(fixture, R2, supersedes=R1)
            codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R2, action="compare")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "invalidation.json|作废|只读"):
                codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R1, action="compare")
            # r2 的 VC-5 失败进入 candidate_review_required：账本非 active，r2 的写入口也拒绝。
            result, returncode = self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2")
            self.assertEqual(returncode, 0, result)
            self._fail_vc5_into_review(fixture, root, sequence=7, tag="vc5-r2-fail", action_id="vc-5-synthetic-b")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "active"):
                codex_upgrade._require_candidate_in_current_revision(campaign_dir, manifest, R2, action="compare")
            status = codex_upgrade.campaign_status(campaign_dir)["candidate_revisions"]
            self.assertEqual((status["revisions"]["r1"]["state"], status["revisions"]["r2"]["state"]), ("invalidated", "active"))

    def test_successor_replays_receipts_and_review_accounting_binds_current_failed_run(self) -> None:
        """审核阻断 3／2：新 revision 后继协议必须完整重放两份收据；作废前对账必须绑定本次失败 run。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir, failed_r1, _applied = self._r1_invalidated(fixture, root)
            self._open(fixture, R2, supersedes=R1)
            reconciliation = campaign_dir / "control" / "reconciliation" / f"run-{failed_r1.name}" / "supervisor-run-reconciliation.json"
            invalidation = campaign_dir / "candidates" / R1 / "invalidation.json"
            originals = {path: path.read_bytes() for path in (reconciliation, invalidation)}
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            history = supervisor._campaign_run_history(Path(str(fixture["state_dir"])), str(manifest["campaign_id"]))
            prior_state, prior_manifest, prior_dir = next(item for item in history if item[2].name == failed_r1.name)
            successor_manifest = {"phase": "VC-4", "candidate_revision": 2, "candidate_id": R2, "campaign_id": manifest["campaign_id"]}
            # 阻断 3（协议函数级）：完整原件通过；任一收据被替换为形状不完整／未绑定的内容即失败关闭，
            # 且入口条件不满足（非候选级前序）时返回 False 交给其他协议。
            self.assertTrue(supervisor._validate_candidate_revision_successor(prior_state, prior_manifest, prior_dir, successor_manifest, campaign_dir=campaign_dir))
            self.assertFalse(supervisor._validate_candidate_revision_successor(prior_state, dict(prior_manifest, phase="VC-2", candidate_revision=None, candidate_id=None), prior_dir, successor_manifest, campaign_dir=campaign_dir))
            forgeries = (
                (reconciliation, b"{}\n", "对账收据 schema 或身份不闭合"),
                # 任意单字节改动：身份核对或总账摘要绑定至少一处失败关闭。
                (reconciliation, originals[reconciliation].replace(b'"failed"', b'"stopped"', 1), "身份不闭合|项目总账绑定不一致"),
                (invalidation, b'{"revision": 1}\n', "invalidation.json 非法"),
            )
            for path, forged, message in forgeries:
                with self.subTest(path=path.name, message=message):
                    path.write_bytes(forged)
                    try:
                        with self.assertRaisesRegex(supervisor.SupervisorError, message):
                            supervisor._validate_candidate_revision_successor(prior_state, prior_manifest, prior_dir, successor_manifest, campaign_dir=campaign_dir)
                    finally:
                        path.write_bytes(originals[path])
            # 收据内容"合法"但与总账绑定的摘要不同（重签一份字段相同、时间戳不同的作废收据）也拒绝。
            payload = _read(invalidation)
            resigned = artifacts.build_candidate_invalidation(
                campaign_id=str(payload["campaign_id"]), candidate_id=R1, revision=1, diagnosis=payload["diagnosis"], recorded_at_utc="2099-01-01T00:00:00Z"
            )
            invalidation.write_text(json.dumps(resigned, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(supervisor.SupervisorError, "项目总账绑定不一致"):
                    supervisor._validate_candidate_revision_successor(prior_state, prior_manifest, prior_dir, successor_manifest, campaign_dir=campaign_dir)
            finally:
                invalidation.write_bytes(originals[invalidation])
            # 阻断 3（派发级）：作废收据被篡改时 r2 的 VC-4 首批在取得执行权前被拒（staging 中止），恢复原件后同序号重派成功。
            invalidation.write_bytes(b'{"revision": 1}\n')
            try:
                with self.assertRaisesRegex((codex_upgrade.ConfigurationError, supervisor.SupervisorError), "候选 revision 后继|invalidation"):
                    self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2-forged")
            finally:
                invalidation.write_bytes(originals[invalidation])
            self.assertFalse((campaign_dir / "control" / "vc" / "revisions" / "r2" / "vc-4-checkpoint.json").exists())
            result, returncode = self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2")
            self.assertEqual(returncode, 0, result)
            self.assertEqual(result["staging_attempt"], 2)
            # 阻断 2：r2 在 VC-5 以同一动作失败但尚未对账，历史 r1 的同阶段同动作对账收据不得冒充本次。
            self._fail_vc5_into_review(fixture, root, sequence=7, tag="vc5-r2-fail")
            source = self._candidate_source(root, "r2")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="b" * 40):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "尚未对账"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R2, "preview", source=source))

    def _reservation_fail_plan(self, root: Path, campaign_dir: Path, *, tag: str, action_id: str, failed_job: bool) -> Path:
        """VC-5 合成动作：在父 run 内为 R1／R2 发布候选 reservation（可选再写一份 failed Job 收据）后非零退出。

        这就是"候选 Job 已产生 reservation 后失败"的真实形态：父 run 对账被拒，只能 reconcile-attempt。
        """

        base = self._plan(root, campaign_dir, "VC-5", tag=tag, fail=True, action_id=action_id)
        plan = _read(base)
        repo_root = Path(codex_upgrade.__file__).resolve().parents[2]
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[4])\n"
            "from unittest import mock\n"
            "from tools.official_client_capture import codex_upgrade\n"
            "from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal\n"
            "campaign_dir = Path(sys.argv[1]); candidate_id = sys.argv[2]; failed_job = sys.argv[3] == '1'\n"
            "with mock.patch.object(rehearsal, '_target_evidence_label_declaration_sha256', return_value='d' * 64):\n"
            "    manifest = codex_upgrade._require_formal_campaign(campaign_dir)\n"
            "    jobs = codex_upgrade._campaign_jobs(campaign_dir, manifest, 'official')\n"
            "    identity = {'candidate_purpose': manifest['campaign_purpose'], 'candidate_id': candidate_id}\n"
            "    attempt_root, reservation = codex_upgrade._reserve_capture_attempt(campaign_dir, phase='candidate', candidate_id=candidate_id, identity=identity, jobs=jobs, allow_failed_rerun=True)\n"
            "    if failed_job:\n"
            "        # 用 failed checkpoint 让第二次 attempt 的根因 failed_step 落到 Job id（与第一次的 reservation 不同）。\n"
            "        job = jobs[0]\n"
            "        store = codex_upgrade.incremental_recovery.CheckpointStore(attempt_root / 'checkpoints')\n"
            "        store.append({'checkpoint_schema_version': codex_upgrade.JOB_CHECKPOINT_SCHEMA, 'campaign_id': manifest['campaign_id'], 'phase': 'candidate', 'attempt_id': attempt_root.name, 'run_nonce': reservation['run_nonce'], 'item_id': job.job_id, 'status': 'failed', 'disposition': 'executed', 'result_sha256': None, 'result_key': None, 'result': None, 'source_receipt': None, 'previous_checkpoint_sha256': None})\n"
            "sys.exit(3)\n"
        )
        plan["actions"][0]["command"] = [sys.executable, "-c", script, str(campaign_dir), R1 if not failed_job else R2, "1" if failed_job else "0", str(repo_root)]
        target = base.with_name(f"{base.stem}-reservation.json")
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.chmod(0o600)
        return target

    def test_reservation_failure_reconciles_by_attempt_then_supersedes_and_continues_on_r2(self) -> None:
        """审核阻断（reservation 分流）：候选 Job 产生 reservation 后失败 → reconcile-attempt → invalidate → r2 → VC-4 续跑。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            self._advance_to_vc3(fixture, root)
            self._open(fixture, R1, initial=True)
            result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
            self.assertEqual(returncode, 0, result)
            # r1：VC-5 动作在父 run 内发布 reservation 后失败 → candidate_review_required。
            plan = self._reservation_fail_plan(root, campaign_dir, tag="vc5-r1-reservation", action_id="vc-5-capture-a", failed_job=False)
            result, returncode = codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", 5, plan))
            self.assertEqual((returncode, result["campaign_run"]["timing_closeout"]["ledger_status"]), (1, "candidate_review_required"), result)
            failed_r1 = Path(str(result["campaign_run"]["run_dir"]))
            attempts_r1 = sorted(path.name for path in (campaign_dir / "candidates" / R1 / "attempts").iterdir())
            self.assertEqual(len(attempts_r1), 1)
            attempt_r1 = attempts_r1[0]
            # 有 reservation：父 run 对账被拒，只能 reconcile-attempt。
            with self.assertRaisesRegex(reconciler.ReconcilerError, "reconcile-attempt"):
                reconciler.reconcile_supervisor_run(failed_r1, campaign_dir)
            source_r1 = self._candidate_source(root, "r1")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "reconcile-attempt"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_r1))
                outcome = reconciler.reconcile_attempt(campaign_dir, attempt_r1)
                self.assertEqual(outcome["status"], "recoverable", outcome)
                self.assertIn(f"reconcile-attempt:{attempt_r1}", self._head(fixture)["operations"])
                self.assertEqual(self._summary(fixture)["status"], "candidate_review_required")
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_r1))
                self.assertEqual(
                    [(item["run_id"], item["reconciliation"], item["attempts"]) for item in preview["accounting"]["failed_runs"]],
                    [(failed_r1.name, "attempt", [attempt_r1])],
                )
                self.assertEqual([item["attempt_id"] for item in preview["accounting"]["attempts"]], [attempt_r1])
                self.assertFalse(preview["accounting"]["zero_request"])
                applied = codex_upgrade.invalidate_candidate(
                    self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_r1)
                )
            self.assertEqual((applied["status"], self._summary(fixture)["status"]), ("applied", "revision_required"))
            # r2：--supersedes → VC-4 首批（后继协议走 attempt 对账分支）→ 续跑成功。
            opened = self._open(fixture, R2, supersedes=R1)
            self.assertEqual(opened["revision"], 2)
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            history = supervisor._campaign_run_history(Path(str(fixture["state_dir"])), str(manifest["campaign_id"]))
            prior_state, prior_manifest, prior_dir = next(item for item in history if item[2].name == failed_r1.name)
            successor_manifest = {"phase": "VC-4", "candidate_revision": 2, "candidate_id": R2, "campaign_id": manifest["campaign_id"]}
            self.assertTrue(supervisor._validate_candidate_revision_successor(prior_state, prior_manifest, prior_dir, successor_manifest, campaign_dir=campaign_dir))
            # 协议级负例：attempt 对账收据被替换即拒绝（不再看父 run 收据）。
            attempt_receipt = campaign_dir / "control" / "reconciliation" / f"attempt-{attempt_r1}" / "attempt-reconciliation.json"
            original = attempt_receipt.read_bytes()
            attempt_receipt.write_bytes(b"{}\n")
            try:
                with self.assertRaisesRegex(supervisor.SupervisorError, "attempt .* 的对账收据"):
                    supervisor._validate_candidate_revision_successor(prior_state, prior_manifest, prior_dir, successor_manifest, campaign_dir=campaign_dir)
            finally:
                attempt_receipt.write_bytes(original)
            self.assertFalse((campaign_dir / "control" / "reconciliation" / f"run-{failed_r1.name}").exists())
            result, returncode = self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2")
            self.assertEqual(returncode, 0, result)
            self.assertTrue((campaign_dir / "control" / "vc" / "revisions" / "r2" / "vc-4-checkpoint.json").is_file())
            # r2：再次 reservation 后失败（带 failed Job 收据 → 不同 attempt 根因）；历史 r1 的已对账 attempt
            # 不能冒充本次：preview 拒绝，直到 r2 自己的 attempt 完成 reconcile-attempt。
            plan = self._reservation_fail_plan(root, campaign_dir, tag="vc5-r2-reservation", action_id="vc-5-capture-b", failed_job=True)
            result, returncode = codex_upgrade.compile_and_run_vc_batch(self._arguments(fixture, "VC-5", 7, plan))
            self.assertEqual((returncode, result["campaign_run"]["timing_closeout"]["ledger_status"]), (1, "candidate_review_required"), result)
            failed_r2 = Path(str(result["campaign_run"]["run_dir"]))
            attempt_r2 = sorted(path.name for path in (campaign_dir / "candidates" / R2 / "attempts").iterdir())[0]
            source_r2 = self._candidate_source(root, "r2")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="b" * 40):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "reconcile-attempt"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R2, "preview", source=source_r2))
                outcome = reconciler.reconcile_attempt(campaign_dir, attempt_r2)
                self.assertEqual(outcome["status"], "recoverable", outcome)
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R2, "preview", source=source_r2))
            self.assertEqual(
                [(item["run_id"], item["reconciliation"], item["attempts"]) for item in preview["accounting"]["failed_runs"]],
                [(failed_r2.name, "attempt", [attempt_r2])],
            )
            # 无关历史 attempt（r1 的）与本次无关：不在本候选目录下，也不在 r2 失败父 run 窗口内。
            self.assertEqual([item["attempt_id"] for item in preview["accounting"]["attempts"]], [attempt_r2])

    def test_second_invalidation_with_same_root_cause_hits_limit_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            self._advance_to_vc3(fixture, root)
            self._open(fixture, R1, initial=True)
            result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
            self.assertEqual(returncode, 0, result)
            failed_r1 = self._fail_vc5_into_review(fixture, root, sequence=5, tag="vc5-r1-fail")
            self.assertEqual(reconciler.reconcile_supervisor_run(failed_r1, campaign_dir)["status"], "recoverable")
            source_r1 = self._candidate_source(root, "r1")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_r1))
                applied = codex_upgrade.invalidate_candidate(
                    self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_r1)
                )
            self.assertEqual(applied["status"], "applied")
            cause = str(applied["root_cause_id"])
            self._open(fixture, R2, supersedes=R1)
            result, returncode = self._dispatch(fixture, root, "VC-4", 6, tag="vc4-r2")
            self.assertEqual(returncode, 0, result)
            # 第二次 VC-5 失败用不同动作 id：supervisor 根因不同，只有作废根因重复。
            failed_r2 = self._fail_vc5_into_review(fixture, root, sequence=7, tag="vc5-r2-fail", action_id="vc-5-synthetic-b")
            self.assertEqual(reconciler.reconcile_supervisor_run(failed_r2, campaign_dir)["status"], "recoverable")
            source_r2 = self._candidate_source(root, "r2")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="b" * 40):
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R2, "preview", source=source_r2))
                self.assertEqual(preview["diagnosis"]["root_cause_id"], cause)
                stopped = codex_upgrade.invalidate_candidate(
                    self._invalidate_arguments(fixture, R2, "apply", approve=str(preview["review_sha256"]), source=source_r2)
                )
            self.assertEqual(stopped["status"], "permanent_stop")
            self.assertEqual(stopped["decision"]["terminal_reason"], "root_cause_limit")
            head = self._head(fixture)
            self.assertEqual(head["root_cause_counts"][cause], 2)
            self.assertIn(cause, head["root_causes_at_limit"])
            summary = self._summary(fixture)
            self.assertEqual(summary["status"], "stopped")
            # 停线合同：不写 candidate_invalidated；invalidation.json 仍存在（作废事实已入账）。
            self.assertNotIn(("candidate_invalidated", f"candidate-invalidated-{R2}-r2"), self._events(fixture))
            self.assertTrue((campaign_dir / "candidates" / R2 / "invalidation.json").is_file())
            # 总账已登记 Campaign 终态：任何 revision 命令在 admission 即被拒。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已终态：root_cause_limit"):
                self._open(fixture, R3, supersedes=R2)


class CandidateRevisionUnitTests(_ChainMixin, unittest.TestCase):
    """T2.3～T2.8 的函数级合同：路径分流、batch/v2、revision-open／invalidate 崩溃点、seal、三分支。"""

    def setUp(self) -> None:
        super().setUp()
        self.helper = test_codex_upgrade.CodexUpgradeTest(
            "test_bound_evidence_path_accepts_legacy_attempt_relative_binding"
        )
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)

    # ------------------------------------------------------------------
    # T2.3：当前 revision 解析与 checkpoint 路径
    # ------------------------------------------------------------------

    def test_current_revision_resolution_and_checkpoint_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            vc_root = campaign_dir / "control" / "vc"
            # Campaign 级阶段忽略 revision；候选级 r1／None 走原路径，r≥2 走 revisions 目录。
            self.assertEqual(codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-3", revision=2), vc_root / "vc-3-checkpoint.json")
            for revision in (None, 1):
                self.assertEqual(codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-4", revision=revision), vc_root / "vc-4-checkpoint.json")
            self.assertEqual(codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-6", revision=3), vc_root / "revisions" / "r3" / "vc-6-checkpoint.json")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision"):
                codex_upgrade._vc_checkpoint_path(campaign_dir, "VC-5", revision=0)
            # 新 Campaign：既无 stage_revision 也无历史 VC-4 checkpoint → None。
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest), (None, None))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision-open --initial"):
                codex_upgrade._require_candidate_revision(campaign_dir, manifest, action="x")
            # 历史 Campaign：原路径 VC-4 checkpoint 存在且无 revisions 目录 → 隐含 r1（无记录）。
            legacy = vc_root / "vc-4-checkpoint.json"
            legacy.write_text("{}\n", encoding="utf-8")
            legacy.chmod(0o600)
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest), (1, None))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "隐含 r1"):
                self._open(fixture, R1, initial=True)
            # 一旦出现 revisions 目录，隐含规则失效：必须由账本 stage_revision 决定。
            (vc_root / "revisions").mkdir(mode=0o700)
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest), (None, None))
            # 符号链接不算历史 checkpoint。
            (vc_root / "revisions").rmdir()
            legacy.unlink()
            legacy.symlink_to(vc_root / "vc-3-checkpoint.json")
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest), (None, None))

    def test_candidate_writes_require_active_revision_and_reject_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            self._advance_to_vc3(fixture, root)
            source = self._candidate_source(root, "r1")
            gates = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id=R1,
                candidate_source=source,
                mapping=source / "mapping.json",
                output=source / "gate-plan.json",
            )
            build = argparse.Namespace(
                campaign_dir=campaign_dir,
                candidate_id=R1,
                candidate_purpose=manifest["campaign_purpose"],
                deployed_version=manifest["target_version"],
                target_architecture="linux/arm64",
                build_id="build-1",
                runtime_image="registry.local/sub2api@sha256:" + "a" * 64,
                candidate_image_id="sha256:" + "b" * 64,
            )
            with mock.patch.object(codex_upgrade, "campaign_status", return_value={"status": "profile_approved"}):
                for entry, label in ((codex_upgrade.plan_candidate_gates, gates), (codex_upgrade.record_candidate_build, build)):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision-open --initial"):
                        entry(label)
                self._open(fixture, R1, initial=True)
                # r1 激活后候选不符仍拒绝；被取代标记存在即只读。
                gates.candidate_id = "candidate-other"
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不属于当前 revision"):
                    codex_upgrade.plan_candidate_gates(gates)
                marker = campaign_dir / "candidates" / R1 / "superseded-by.json"
                marker.parent.mkdir(parents=True, mode=0o700)
                marker.write_text("{}\n", encoding="utf-8")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已被取代"):
                    codex_upgrade.record_candidate_build(build)
            # invalidate 也只接受当前候选。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不是当前 revision"):
                codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, "candidate-other", "preview"))

    # ------------------------------------------------------------------
    # T2.4：batch/v2 与清单绑定
    # ------------------------------------------------------------------

    def test_batch_v2_and_run_manifest_candidate_binding_contracts(self) -> None:
        plan = artifacts.build_campaign_plan(
            campaign_id="codex-0_154_0-campaign",
            campaign_mode="formal",
            campaign_purpose="validation_only",
            baseline_version="0.151.0",
            target_version="0.154.0",
            created_at_utc="2026-09-14T00:00:00Z",
            original_deadline_at_utc="2099-09-14T12:00:00Z",
            timing_checkpoint_sha256="1" * 64,
            arm64_environment_sha256="2" * 64,
            job_rehearsal_sha256="3" * 64,
            p0_gate_sha256="4" * 64,
        )
        predecessor = {"path": "control/vc/vc-3-checkpoint.json", "sha256": "5" * 64, "phase": "VC-3", "checkpoint_sha256": "6" * 64}
        common = dict(
            campaign_plan=plan,
            sequence=4,
            predecessor_checkpoint=predecessor,
            execute_item_ids=["gate"],
            reuse_item_ids=[],
            actions=[{"action_id": "gate", "operation": "VC-4:gate", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["gate"]}],
            compiled_at_utc="2026-09-14T01:00:00Z",
            must_start_by_utc="2026-09-14T01:01:00Z",
        )
        batch = artifacts.build_vc_batch(phase="VC-4", candidate_revision=2, candidate_id=R2, **common)
        self.assertEqual((batch["schema_version"], batch["candidate_revision"], batch["candidate_id"]), (artifacts.VC_BATCH_SCHEMA, 2, R2))
        self.assertEqual(artifacts.validate_vc_batch(batch, plan), batch)
        # 候选级阶段缺绑定／Campaign 级阶段带绑定都拒绝；成对性也拒绝单边。
        with self.assertRaisesRegex(artifacts.VCArtifactError, "candidate"):
            artifacts.build_vc_batch(phase="VC-4", **common)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "candidate"):
            artifacts.build_vc_batch(phase="VC-4", candidate_revision=2, **common)
        campaign_level = dict(common, sequence=2, predecessor_checkpoint={**predecessor, "path": "control/vc/vc-1-checkpoint.json", "phase": "VC-1"})
        with self.assertRaisesRegex(artifacts.VCArtifactError, "candidate"):
            artifacts.build_vc_batch(phase="VC-2", candidate_revision=1, candidate_id=R1, **campaign_level)
        campaign_batch = artifacts.build_vc_batch(phase="VC-2", **campaign_level)
        self.assertEqual((campaign_batch["candidate_revision"], campaign_batch["candidate_id"]), (None, None))
        # v1 只读兼容：历史 batch 没有两字段，校验通过且不被补写。
        legacy = {key: value for key, value in campaign_batch.items() if key not in {"candidate_revision", "candidate_id", "batch_sha256"}}
        legacy["schema_version"] = artifacts.VC_BATCH_LEGACY_SCHEMA
        legacy["batch_sha256"] = artifacts.digest(legacy)
        validated = artifacts.validate_vc_batch(legacy, plan)
        self.assertNotIn("candidate_revision", validated)
        # 篡改 v2 两字段即摘要不符。
        tampered = dict(batch, candidate_id=R1)
        with self.assertRaisesRegex(artifacts.VCArtifactError, "batch_sha256|摘要"):
            artifacts.validate_vc_batch(tampered, plan)
        # campaign-run 清单：staging 模型携带成对字段（Campaign 级 null）；legacy 不含。
        staging_manifest = codex_upgrade._vc_run_manifest_from_batch(batch, batch_model="staging")
        self.assertEqual((staging_manifest["candidate_revision"], staging_manifest["candidate_id"]), (2, R2))
        legacy_manifest = codex_upgrade._vc_run_manifest_from_batch(batch, batch_model="legacy")
        self.assertNotIn("candidate_revision", legacy_manifest)
        null_manifest = codex_upgrade._vc_run_manifest_from_batch(campaign_batch, batch_model="staging")
        self.assertEqual((null_manifest["candidate_revision"], null_manifest["candidate_id"]), (None, None))
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "未知批次模型"):
            codex_upgrade._vc_run_manifest_from_batch(batch, batch_model="other")
        with self.assertRaisesRegex(supervisor.SupervisorError, "同时给出"):
            supervisor.build_batched_campaign_run_manifest(
                campaign_id=batch["campaign_id"],
                campaign_plan_sha256=batch["campaign_plan_sha256"],
                batch_id=batch["batch_id"],
                batch_sequence=4,
                batch_sha256=batch["batch_sha256"],
                phase="VC-4",
                predecessor_checkpoint=predecessor,
                original_deadline_at_utc=batch["original_deadline_at_utc"],
                actions=batch["actions"],
                execute_items=["gate"],
                reuse_items=[],
                candidate_revision=2,
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def load(payload: dict[str, object]) -> dict[str, object]:
                path = root / "manifest.json"
                path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                path.chmod(0o600)
                return supervisor._campaign_run_manifest(path)
            # 无新字段的旧 v2 清单只读兼容；候选级 null／Campaign 级非 null／单边字段都拒绝。
            self.assertNotIn("candidate_revision", load(legacy_manifest))
            self.assertEqual(load(staging_manifest)["candidate_id"], R2)
            with self.assertRaisesRegex(supervisor.SupervisorError, "成对"):
                load({key: value for key, value in staging_manifest.items() if key != "candidate_id"})
            with self.assertRaisesRegex(supervisor.SupervisorError, "候选级阶段"):
                load(dict(staging_manifest, candidate_revision=None, candidate_id=None))
            with self.assertRaisesRegex(supervisor.SupervisorError, "Campaign 级阶段"):
                load(dict(null_manifest, candidate_revision=1, candidate_id=R1))

    def test_staging_attempt_rejects_manifest_candidate_binding_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            result, returncode = self._dispatch(fixture, root, "VC-2", 2, tag="vc2")
            self.assertEqual(returncode, 0, result)
            attempt_dir = campaign_dir / "control" / "vc" / "staging" / "0002-vc-2" / "attempt-1"
            loaded = codex_upgrade._load_prepared_staging_attempt(campaign_dir, attempt_dir, owner_nonce=None, sequence=2, phase="VC-2")
            self.assertEqual((loaded["run_manifest"]["candidate_revision"], loaded["run_manifest"]["candidate_id"]), (None, None))
            drifted = dict(loaded["run_manifest"], candidate_revision=1, candidate_id=R1)
            with mock.patch.object(supervisor, "_campaign_run_manifest", return_value=drifted):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "candidate_revision／candidate_id 缺失或与 batch 不一致"):
                    codex_upgrade._load_prepared_staging_attempt(campaign_dir, attempt_dir, owner_nonce=None, sequence=2, phase="VC-2")
            missing = {key: value for key, value in loaded["run_manifest"].items() if key not in {"candidate_revision", "candidate_id"}}
            with mock.patch.object(supervisor, "_campaign_run_manifest", return_value=missing):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "缺失或与 batch 不一致"):
                    codex_upgrade._load_prepared_staging_attempt(campaign_dir, attempt_dir, owner_nonce=None, sequence=2, phase="VC-2")

    # ------------------------------------------------------------------
    # T2.5：revision-open 崩溃点与内容核对
    # ------------------------------------------------------------------

    def test_revision_open_resumes_after_directory_written_and_rejects_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            campaign_dir = Path(str(fixture["campaign_dir"]))
            manifest = codex_upgrade._require_formal_campaign(campaign_dir)
            self._advance_to_vc3(fixture, root)
            # 崩溃点：revision.json + COMMIT 已写、账本 stage_revision 未写 → 再执行即补账本，不重写目录。
            record, commit = codex_upgrade._write_candidate_revision_pending(campaign_dir, manifest, revision=1, candidate_id=R1, supersedes=None)
            r1_dir = campaign_dir / "control" / "vc" / "revisions" / "r1"
            record_bytes = (r1_dir / "revision.json").read_bytes()
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest), (None, None))
            opened = self._open(fixture, R1, initial=True)
            self.assertEqual((opened["revision"], opened["idempotent"], opened["ledger_event"]["appended"]), (1, False, True))
            self.assertEqual((r1_dir / "revision.json").read_bytes(), record_bytes)
            self.assertEqual(opened["commit_sha256"], commit["commit_sha256"])
            # 内容核对：同 revision 换候选即拒绝，文件字节不变。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "内容不一致"):
                codex_upgrade._write_candidate_revision_pending(campaign_dir, manifest, revision=1, candidate_id="candidate-other", supersedes=None)
            self.assertEqual((r1_dir / "revision.json").read_bytes(), record_bytes)
            # 目录 COMMIT 与账本引用不一致 → 解析失败关闭。
            commit_path = r1_dir / "COMMIT"
            original = commit_path.read_bytes()
            forged = artifacts.build_candidate_revision_commit(
                campaign_id=str(manifest["campaign_id"]), revision=1, candidate_id=R1, record_sha256=str(record["record_sha256"]), committed_at_utc="2099-01-01T00:00:00Z"
            )
            commit_path.write_text(json.dumps(forged, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "COMMIT 与账本 stage_revision 不一致"):
                codex_upgrade._current_candidate_revision_record(campaign_dir, manifest)
            commit_path.write_bytes(original)
            self.assertEqual(codex_upgrade._current_candidate_revision_record(campaign_dir, manifest)[0], 1)
            # admission 拒绝：总计划 deadline 已到。
            with mock.patch.object(codex_upgrade, "_campaign_plan_deadline", return_value="2000-01-01T00:00:00Z"):
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "deadline 已到"):
                    self._open(fixture, R1, initial=True)

    # ------------------------------------------------------------------
    # T2.6：invalidate-candidate 的前提、漂移与崩溃点
    # ------------------------------------------------------------------

    def _r1_ready(self, root: Path) -> tuple[dict[str, object], Path, dict[str, object]]:
        fixture = self._fixture(root)
        campaign_dir = Path(str(fixture["campaign_dir"]))
        self._advance_to_vc3(fixture, root)
        self._open(fixture, R1, initial=True)
        result, returncode = self._dispatch(fixture, root, "VC-4", 4, tag="vc4-r1")
        self.assertEqual(returncode, 0, result)
        return fixture, campaign_dir, codex_upgrade._require_formal_campaign(campaign_dir)

    def test_invalidate_from_active_ledger_is_zero_request_and_recovers_after_crash_before_ledger_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, _manifest = self._r1_ready(root)
            source = self._candidate_source(root, "r1")
            self.assertEqual(self._summary(fixture)["status"], "active")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source))
                self.assertTrue(preview["accounting"]["zero_request"])
                self.assertEqual(preview["diagnosis"]["root_cause_id"], root_cause.structured_root_cause(
                    component="candidate", stable_error_code="candidate.source-change-required",
                    failed_step="candidate-source-change-vc-4", stable_dimensions={"phase": "VC-4"},
                ))
                # 崩溃点：invalidation.json 与总账已写，账本 candidate_invalidated 未写。
                with mock.patch.object(codex_upgrade, "_publish_ledger_receipt_copies", side_effect=OSError("simulated crash")):
                    with self.assertRaises(OSError):
                        codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source))
                self.assertTrue((campaign_dir / "candidates" / R1 / "invalidation.json").is_file())
                self.assertIn(f"invalidate-candidate:{R1}:r1", self._head(fixture)["operations"])
                self.assertEqual(self._summary(fixture)["status"], "active")
                applied = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source))
            self.assertEqual((applied["status"], applied["idempotent"], applied["batch"]["reused"]), ("applied", False, True))
            self.assertEqual(self._summary(fixture)["status"], "revision_required")
            counts = self._head(fixture)["root_cause_counts"]
            self.assertEqual(counts[str(applied["root_cause_id"])], 1)

    def test_invalidate_rejects_unaccounted_sealed_candidate_head_drift_and_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, _manifest = self._r1_ready(root)
            source_a = self._candidate_source(root, "r1")
            source_b = self._candidate_source(root, "r1-drift")
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_a))
                # --candidate-source 漂移（tree 不同）→ 草案摘要不同 → apply 拒绝且不落盘。
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "重新 preview"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_b))
                # 总账 head 漂移（preview 后又有事件入账）→ 同样拒绝。
                head = self._head(fixture)
                with mock.patch.object(reconciler, "_project_facts", return_value=({"absolute_deadline_utc": "2099-01-01T00:00:00Z"}, dict(head, head_sha256="f" * 64, sequence=int(head["sequence"]) + 1))):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "重新 preview"):
                        codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source_a))
                self.assertFalse((campaign_dir / "candidates" / R1 / "invalidation.json").exists())
                # 已 seal 候选未入账：result.json complete 绑定 attempt，总账缺 account-sealed-candidate:<cid>:<attempt>。
                candidate_root = campaign_dir / "candidates" / R1
                attempt_root = candidate_root / "attempts" / "attempt-a"
                attempt_root.mkdir(parents=True, mode=0o700)
                (attempt_root / "attempt.json").write_text("{}\n", encoding="utf-8")
                (candidate_root / "result.json").write_text(
                    json.dumps({"status": "complete", "attempt": {"path": "candidates/candidate-r1/attempts/attempt-a/attempt.json", "sha256": "0" * 64}}) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, f"account-sealed-candidate:{R1}:attempt-a"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_a))
                # 另一候选的同名 attempt 已入账不能替代本候选：operation 以候选 id 参与。
                plan, head = reconciler._project_facts(reconciler._project_root(campaign_dir.resolve()))
                other = dict(head, operations={**head["operations"], "account-sealed-candidate:candidate-other:attempt-a": {}})
                with mock.patch.object(reconciler, "_project_facts", return_value=(plan, other)):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, f"account-sealed-candidate:{R1}:attempt-a"):
                        codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_a))
                mine = dict(head, operations={**head["operations"], f"account-sealed-candidate:{R1}:attempt-a": {}})
                with mock.patch.object(reconciler, "_project_facts", return_value=(plan, mine)):
                    preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_a))
                self.assertEqual(preview["accounting"]["sealed"]["attempt_id"], "attempt-a")
                self.assertFalse(preview["accounting"]["zero_request"])
                # 已 accepted 的候选不得作废。
                acceptance = campaign_dir / "acceptance" / R1 / "result.json"
                acceptance.parent.mkdir(parents=True, mode=0o700)
                acceptance.write_text(json.dumps({"status": "complete", "accepted": True}) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已 accepted"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source_a))

    def test_invalidate_admission_rejects_expired_deadline_and_bad_ledger_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, _manifest = self._r1_ready(root)
            source = self._candidate_source(root, "r1")
            ledger_dir = Path(str(fixture["timing_ledger"]))
            with mock.patch.object(codex_upgrade, "_git_commit", return_value="a" * 40):
                preview = codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source))
                # preview 后总计划 deadline 到期：apply 在 admission 即拒，不落盘。
                with mock.patch.object(codex_upgrade, "_campaign_plan_deadline", return_value="2000-01-01T00:00:00Z"):
                    with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "deadline 已到"):
                        codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "apply", approve=str(preview["review_sha256"]), source=source))
                self.assertFalse((campaign_dir / "candidates" / R1 / "invalidation.json").exists())
                # 账本 recovery_required／stopped 时 preview 拒绝并给出提示。
                timing_ledger.append_event(ledger_dir, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
                timing_ledger.append_event(ledger_dir, event_id="pause", phase="VC-5", event_type="recovery_required", root_cause_id="rc-fixture", live_request_count=0, next_action="reconcile")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "recovery_required"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source))
                timing_ledger.append_event(ledger_dir, event_id="abandon", phase="VC-5", event_type="stage_abandoned", root_cause_id="rc-fixture", next_action="stop")
                timing_ledger.append_event(ledger_dir, event_id="stop", phase="VC-5", event_type="stop_the_line", root_cause_id="rc-fixture", next_action="stop")
                with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "stopped"):
                    codex_upgrade.invalidate_candidate(self._invalidate_arguments(fixture, R1, "preview", source=source))

    # ------------------------------------------------------------------
    # T2.7：候选级动作失败三分支
    # ------------------------------------------------------------------

    def test_candidate_failure_three_branches_and_campaign_level_still_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, manifest = self._r1_ready(root)
            ledger_dir = Path(str(fixture["timing_ledger"]))
            plan = codex_upgrade._vc_campaign_plan(campaign_dir, manifest)
            batch = artifacts.build_vc_batch(
                campaign_plan=plan,
                phase="VC-5",
                sequence=5,
                predecessor_checkpoint={"path": "control/vc/vc-4-checkpoint.json", "sha256": "5" * 64, "phase": "VC-4", "checkpoint_sha256": "6" * 64},
                execute_item_ids=["seal"],
                reuse_item_ids=[],
                actions=[{"action_id": "seal", "operation": "VC-5:seal", "timeout_seconds": 5.0, "command": ["true"], "item_ids": ["seal"]}],
                compiled_at_utc="2026-09-14T01:00:00Z",
                must_start_by_utc=str(plan["original_deadline_at_utc"]),
                candidate_revision=1,
                candidate_id=R1,
            )
            run_manifest = codex_upgrade._vc_run_manifest_from_batch(batch, batch_model="staging")

            def close(failure_class: str) -> dict[str, object]:
                return supervisor._close_failed_campaign_timing_ledger(campaign_dir, run_manifest, failed_action_id="seal", failure_class=failure_class)

            timing_ledger.append_event(ledger_dir, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
            # 分支 1：可恢复类不变 → recovery_required（阶段保持 active）。
            paused = close("environment-prerequisite")
            self.assertEqual((paused["ledger_status"], self._summary(fixture)["active_phase"]), ("recovery_required", "VC-5"))
            # 分支 2：永久条件（分类本身不可恢复）→ 停线合同不变。
            with tempfile.TemporaryDirectory() as second:
                fixture2, campaign2, manifest2 = self._r1_ready(Path(second).resolve())
                ledger2 = Path(str(fixture2["timing_ledger"]))
                plan2 = codex_upgrade._vc_campaign_plan(campaign2, manifest2)
                batch2 = artifacts.build_vc_batch(campaign_plan=plan2, phase="VC-5", sequence=5, predecessor_checkpoint=batch["predecessor_checkpoint"], execute_item_ids=["seal"], reuse_item_ids=[], actions=batch["actions"], compiled_at_utc="2026-09-14T01:00:00Z", must_start_by_utc=str(plan2["original_deadline_at_utc"]), candidate_revision=1, candidate_id=R1)
                manifest_2 = codex_upgrade._vc_run_manifest_from_batch(batch2, batch_model="staging")
                timing_ledger.append_event(ledger2, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
                stopped = supervisor._close_failed_campaign_timing_ledger(campaign2, manifest_2, failed_action_id="seal", failure_class="identity-drift")
                self.assertEqual(stopped["ledger_status"], "stopped")
                self.assertEqual(timing_ledger.inspect_ledger(ledger2)["status"], "stopped")
            # 分支 2'：永久条件来自总账（根因已达上限）→ 也停线。
            with tempfile.TemporaryDirectory() as third:
                fixture3, campaign3, manifest3 = self._r1_ready(Path(third).resolve())
                ledger3 = Path(str(fixture3["timing_ledger"]))
                plan3 = codex_upgrade._vc_campaign_plan(campaign3, manifest3)
                batch3 = artifacts.build_vc_batch(campaign_plan=plan3, phase="VC-5", sequence=5, predecessor_checkpoint=batch["predecessor_checkpoint"], execute_item_ids=["seal"], reuse_item_ids=[], actions=batch["actions"], compiled_at_utc="2026-09-14T01:00:00Z", must_start_by_utc=str(plan3["original_deadline_at_utc"]), candidate_revision=1, candidate_id=R1)
                manifest_3 = codex_upgrade._vc_run_manifest_from_batch(batch3, batch_model="staging")
                timing_ledger.append_event(ledger3, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
                real_head = project_ledger.replay_head
                with mock.patch.object(project_ledger, "replay_head", side_effect=lambda r: dict(real_head(r), root_causes_at_limit=["rc-x"])):
                    stopped = supervisor._close_failed_campaign_timing_ledger(campaign3, manifest_3, failed_action_id="seal", failure_class="execution-failure")
                self.assertEqual(stopped["ledger_status"], "stopped")
            # 分支 3：其余 → stage_abandoned + candidate_review_required，幂等。
            with tempfile.TemporaryDirectory() as fourth:
                fixture4, campaign4, manifest4 = self._r1_ready(Path(fourth).resolve())
                ledger4 = Path(str(fixture4["timing_ledger"]))
                plan4 = codex_upgrade._vc_campaign_plan(campaign4, manifest4)
                batch4 = artifacts.build_vc_batch(campaign_plan=plan4, phase="VC-5", sequence=5, predecessor_checkpoint=batch["predecessor_checkpoint"], execute_item_ids=["seal"], reuse_item_ids=[], actions=batch["actions"], compiled_at_utc="2026-09-14T01:00:00Z", must_start_by_utc=str(plan4["original_deadline_at_utc"]), candidate_revision=1, candidate_id=R1)
                manifest_4 = codex_upgrade._vc_run_manifest_from_batch(batch4, batch_model="staging")
                timing_ledger.append_event(ledger4, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
                review = supervisor._close_failed_campaign_timing_ledger(campaign4, manifest_4, failed_action_id="seal", failure_class="execution-failure")
                self.assertEqual((review["ledger_status"], review["idempotent"]), ("candidate_review_required", False))
                summary = timing_ledger.inspect_ledger(ledger4)
                self.assertEqual((summary["status"], summary["active_phase"], summary["revision_phase_state"]["1"]["VC-5"]), ("candidate_review_required", None, "abandoned"))
                self.assertNotIn(("stop_the_line", summary["last_event_id"]), [(summary["last_event_id"], summary["last_event_id"])])
                replay = supervisor._close_failed_campaign_timing_ledger(campaign4, manifest_4, failed_action_id="seal", failure_class="execution-failure")
                self.assertEqual((replay["ledger_status"], replay["idempotent"]), ("candidate_review_required", True))
                with self.assertRaisesRegex(supervisor.SupervisorError, "其他根因"):
                    supervisor._close_failed_campaign_timing_ledger(campaign4, manifest_4, failed_action_id="other", failure_class="execution-failure")
                # legacy 清单（无候选绑定）即使在候选级阶段也保持停线现状。
                legacy_manifest = codex_upgrade._vc_run_manifest_from_batch(batch4, batch_model="legacy")
                self.assertNotIn("candidate_id", legacy_manifest)
            with tempfile.TemporaryDirectory() as fifth:
                fixture5, campaign5, manifest5 = self._r1_ready(Path(fifth).resolve())
                ledger5 = Path(str(fixture5["timing_ledger"]))
                plan5 = codex_upgrade._vc_campaign_plan(campaign5, manifest5)
                batch5 = artifacts.build_vc_batch(campaign_plan=plan5, phase="VC-5", sequence=5, predecessor_checkpoint=batch["predecessor_checkpoint"], execute_item_ids=["seal"], reuse_item_ids=[], actions=batch["actions"], compiled_at_utc="2026-09-14T01:00:00Z", must_start_by_utc=str(plan5["original_deadline_at_utc"]), candidate_revision=1, candidate_id=R1)
                timing_ledger.append_event(ledger5, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x")
                stopped = supervisor._close_failed_campaign_timing_ledger(campaign5, codex_upgrade._vc_run_manifest_from_batch(batch5, batch_model="legacy"), failed_action_id="seal", failure_class="execution-failure")
                self.assertEqual(stopped["ledger_status"], "stopped")

    # ------------------------------------------------------------------
    # T2.8：record-candidate-build 的 revision-seal 段
    # ------------------------------------------------------------------

    def test_seal_contract_image_only_change_rejected_and_resume_points_are_idempotent(self) -> None:
        superseded = {"revision": 1, "candidate_id": R1, "git_commit": "a" * 40, "source_tree_sha256": "1" * 64, "image_id": "sha256:" + "2" * 64}
        common = dict(campaign_id="codex-0_154_0-campaign", revision=2, candidate_id=R2, build_receipt_sha256="c" * 64, vc3_stage_receipt_sha256="d" * 64, superseded=superseded, sealed_at_utc="2026-09-19T00:00:00Z")
        # 只有 image 变化：commit／tree 全同 → 拒绝。
        with self.assertRaisesRegex(artifacts.VCArtifactError, "全部相同"):
            artifacts.build_candidate_revision_seal(candidate_commit="a" * 40, source_tree_sha256="1" * 64, image_id="sha256:" + "3" * 64, **common)
        # image 相同但 tree 变化 → 通过（image 不强制）。
        sealed = artifacts.build_candidate_revision_seal(candidate_commit="a" * 40, source_tree_sha256="9" * 64, image_id="sha256:" + "2" * 64, **common)
        self.assertEqual(sealed["identity_change"], {"git_commit_changed": False, "source_tree_changed": True, "image_changed": False})
        # r1（无 superseded）不做同一性判定。
        first = artifacts.build_candidate_revision_seal(candidate_commit="a" * 40, source_tree_sha256="1" * 64, image_id=None, **dict(common, revision=1, candidate_id=R1, superseded=None))
        self.assertIsNone(first["identity_change"])
        with self.assertRaisesRegex(artifacts.VCArtifactError, "被取代候选身份"):
            artifacts.build_candidate_revision_seal(candidate_commit="a" * 40, source_tree_sha256="9" * 64, image_id=None, **dict(common, superseded=None))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, manifest = self._r1_ready(root)
            record, _commit = codex_upgrade._read_candidate_revision_record(campaign_dir, 1)
            # r1 的 seal 正例；再次调用（seal.json 已写、build receipt 未写的断点）幂等且字节不变。
            seal = codex_upgrade._seal_candidate_revision(campaign_dir, record, candidate_commit="a" * 40, source_tree_sha256="1" * 64, image_id="sha256:" + "2" * 64, build_receipt_sha256="c" * 64, vc3_stage_receipt_sha256=str(record["vc3_stage_receipt"]["sha256"]))
            seal_path = campaign_dir / "control" / "vc" / "revisions" / "r1" / "seal.json"
            seal_bytes = seal_path.read_bytes()
            again = codex_upgrade._seal_candidate_revision(campaign_dir, record, candidate_commit="a" * 40, source_tree_sha256="1" * 64, image_id="sha256:" + "2" * 64, build_receipt_sha256="c" * 64, vc3_stage_receipt_sha256=str(record["vc3_stage_receipt"]["sha256"]))
            self.assertEqual((again, seal_path.read_bytes()), (seal, seal_bytes))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "内容不一致"):
                codex_upgrade._seal_candidate_revision(campaign_dir, record, candidate_commit="b" * 40, source_tree_sha256="1" * 64, image_id="sha256:" + "2" * 64, build_receipt_sha256="c" * 64, vc3_stage_receipt_sha256=str(record["vc3_stage_receipt"]["sha256"]))
            # build receipt 已写、VC-4 checkpoint 未写的断点：收据核对（豁免易变字段）后封存 checkpoint。
            receipt_path = campaign_dir / "candidates" / R1 / "build-receipt.json"
            receipt_path.parent.mkdir(parents=True, mode=0o700)
            payload = {"candidate_id": R1, "built_at_utc": "2026-09-19T00:00:00Z", "receipt_digest": "e" * 64, "source": {"git_commit": "a" * 40}}
            codex_upgrade._write_or_verify_json_ignoring(receipt_path, payload, volatile=("built_at_utc", "receipt_digest"))
            codex_upgrade._write_or_verify_json_ignoring(receipt_path, dict(payload, built_at_utc="2026-09-19T00:01:00Z", receipt_digest="f" * 64), volatile=("built_at_utc", "receipt_digest"))
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "内容不一致"):
                codex_upgrade._write_or_verify_json_ignoring(receipt_path, dict(payload, source={"git_commit": "b" * 40}), volatile=("built_at_utc", "receipt_digest"))
            self.assertEqual(_read(receipt_path)["receipt_digest"], "e" * 64)

    def _build_arguments(self, campaign_dir: Path, manifest: dict[str, object], root: Path, *, catalog_receipt: bytes) -> argparse.Namespace:
        source = root / "candidate-source"
        catalog = source / "catalog-stage"
        catalog.mkdir(parents=True, mode=0o700)
        (catalog / "catalog-stage-receipt.json").write_bytes(catalog_receipt)
        binary = root / "bin" / "sub2api"
        binary.parent.mkdir(mode=0o700)
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        for name in ("build-tree", "docker-context", "frontend-dist"):
            (root / name).mkdir(mode=0o700)
        return argparse.Namespace(
            campaign_dir=campaign_dir,
            candidate_id=R1,
            candidate_purpose=manifest["campaign_purpose"],
            deployed_version=manifest["target_version"],
            target_architecture="linux/arm64",
            build_id="build-1",
            runtime_image="registry.local/sub2api@sha256:" + "a" * 64,
            candidate_image_id="sha256:" + "b" * 64,
            candidate_source=source,
            candidate_binary=binary,
            build_parameters=root / "build-parameters.json",
            build_tree=root / "build-tree",
            docker_context=root / "docker-context",
            frontend_dist_source=root / "frontend-dist",
            catalog_stage_dir=catalog,
            source_transition=root / "source-transition.json",
        )

    def test_record_candidate_build_seal_step_checks_vc3_bytes_only_for_recorded_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture, campaign_dir, manifest = self._r1_ready(root)
            record, _commit = codex_upgrade._read_candidate_revision_record(campaign_dir, 1)
            vc3_receipt = codex_upgrade._campaign_file(campaign_dir, str(record["vc3_stage_receipt"]["path"]))
            def binding(path: Path, _label: str) -> dict[str, object]:
                return {"path": str(path), "sha256": "1" * 64, "bytes": path.stat().st_size}

            class Reached(RuntimeError):
                """到达 source_transition 读取即表示已越过 VC-3 字节校验。"""

            def run(arguments: argparse.Namespace):
                with (
                    mock.patch.object(codex_upgrade, "campaign_status", return_value={"status": "profile_approved"}),
                    mock.patch.object(codex_upgrade, "_active_unsealed_attempts", return_value=[]),
                    mock.patch.object(codex_upgrade, "_git_commit", return_value="b" * 40),
                    mock.patch.object(codex_upgrade, "_require_clean_candidate_source"),
                    mock.patch.object(codex_upgrade, "_external_file_binding", side_effect=binding),
                    mock.patch.object(codex_upgrade, "_external_json_object", side_effect=[({}, {"path": "p", "sha256": "2" * 64}), Reached("reached")]),
                    mock.patch.object(codex_upgrade.codex_upgrade_candidate_build, "validate_build_parameters", return_value={}),
                    mock.patch.object(codex_upgrade, "_verify_catalog_stage_output"),
                ):
                    return codex_upgrade.record_candidate_build(arguments)

            # 候选树内 catalog-stage-receipt.json 与 revision 绑定的 VC-3 阶段收据不一致 → 拒绝。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "revision-seal 拒绝"):
                run(self._build_arguments(campaign_dir, manifest, root / "drift", catalog_receipt=b'{"drift": true}\n'))
            # 逐字节一致 → 越过字节校验（到达下一步）。
            with self.assertRaises(Reached):
                run(self._build_arguments(campaign_dir, manifest, root / "same", catalog_receipt=vc3_receipt.read_bytes()))
            # 历史隐含 r1（无 revision 记录）不做字节校验：不一致的收据也越过该步。
            with mock.patch.object(codex_upgrade, "_require_candidate_in_current_revision", return_value=(1, None)):
                with self.assertRaises(Reached):
                    run(self._build_arguments(campaign_dir, manifest, root / "implicit", catalog_receipt=b'{"drift": true}\n'))


if __name__ == "__main__":
    unittest.main()
