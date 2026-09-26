"""工具演进登记（修好接着跑）：演进链、有效身份与逐作业影响，续跑闭集与 seal 核对，
b0 评估器授权迁移，以及候选审核（VC-5 采集失败）续跑的账本、对账与监督器后继协议。

背景：2026-09-26 c01570 194249z 的 VC-5 批次 9 里 candidate-frozen-core 因采集脚本不适配
0.157 失败，账本进入 candidate_review_required。修好脚本并部署后，旧工具只能作废候选或
停线：对账按当前身份判 identity_changed 永久终态，候选审核不允许续跑，也没有 VC-5 的续跑
后继协议。本文件覆盖改造后的整条路径与反例。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip
from tools.official_client_capture import codex_upgrade_wire_transition as wt
from tools.official_client_capture.codex_upgrade_supervisor import SupervisorError

TOOL_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(0o600)


class EvolutionFixture:
    """真实受管树身份做 plan，逐文件改摘要得到演进后的身份。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.campaign_dir = root / "campaign"
        self.policy = tip.load_policy()
        self.identity = codex_upgrade._tool_identity(include_git=False)
        self.manifest = {
            "campaign_id": "c-evolution",
            "campaign_mode": "formal",
            "target_version": "0.157.0",
            "tool_identity": dict(self.identity),
        }
        _write_json(self.campaign_dir / "campaign.json", self.manifest)

    def mutated_identity(self, base: dict, *paths: str, marker: str = "0") -> dict:
        entries = [dict(e, sha256=marker * 64) if e["path"] in paths else dict(e) for e in base["entries"]]
        v2 = tip.compute_identity_v2(self.policy, TOOL_ROOT, entries)
        return {
            **base,
            "entries": entries,
            **{k: v2[k] for k in ("wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "policy_sha256")},
            "files_sha256": codex_upgrade._fingerprint({"entries": entries}),
        }

    def manifest_sha256(self) -> str:
        return hashlib.sha256((self.campaign_dir / "campaign.json").read_bytes()).hexdigest()

    def payload(
        self,
        *,
        index: int,
        previous: dict | None,
        from_identity: dict,
        to_identity: dict,
        official_affected: list[str] | None = None,
        candidates: dict | None = None,
        evaluator_candidates: dict | None = None,
        changed_paths: list[str] | None = None,
    ) -> dict:
        payload = {
            "schema_version": wt.EVOLUTION_SCHEMA,
            "index": index,
            "campaign_id": self.manifest["campaign_id"],
            "campaign_manifest_sha256": self.manifest_sha256(),
            "previous_evolution_sha256": previous["receipt_sha256"] if previous else None,
            "from_summary": wt.identity_summary(from_identity),
            "to_summary": wt.identity_summary(to_identity),
            "to_identity": to_identity,
            "to_evaluator_digests": {},
            "changes": {"paths_by_layer": {"wire_producer": list(changed_paths or [])}},
            "impact": {
                "official": {
                    "planned_job_ids": ["official-core"],
                    "affected_job_ids": list(official_affected or []),
                    "sealed": True,
                },
                "candidates": candidates or {},
                "inactive_candidates": [],
            },
            "evaluator": {
                "from": {},
                "to": {"checker_sha256": "c" * 64, "builder_sha256": "b" * 64},
                "changed_fields": [],
                "candidates": evaluator_candidates or {},
            },
            "bindings": {"fix_commit": "f" * 40, "deployment_receipt": {"created_at_utc": "2026-09-27T00:00:00Z"}},
            "reason": "测试",
            "approved_sha256": "a" * 64,
            "approved_by": "tester",
            "approved_at_utc": "2026-09-27T00:00:01Z",
        }
        payload["receipt_sha256"] = wt._fingerprint(payload)
        return payload


class EvolutionChainTests(unittest.TestCase):
    def test_empty_chain_effective_identity_is_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            self.assertEqual(wt.load_evolutions(fixture.campaign_dir, fixture.manifest), [])
            effective = wt.effective_tool_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual(effective["index"], 0)
            self.assertEqual(effective["identity"]["files_sha256"], fixture.identity["files_sha256"])
            self.assertIsNone(wt.reservation_binding(effective))

    def test_written_chain_moves_effective_identity_and_wire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            first_to = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            first = fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=first_to,
                                    candidates={"cand": {"planned_job_ids": ["j1", "j2"], "affected_job_ids": ["j1"], "sealed": False}},
                                    changed_paths=["run_candidate_core_capture.sh"])
            wt.write_evolution(fixture.campaign_dir, fixture.manifest, first)
            second_to = fixture.mutated_identity(first_to, "codex_upgrade_supervisor.py", marker="1")
            second = fixture.payload(index=2, previous=first, from_identity=first_to, to_identity=second_to,
                                     candidates={"cand": {"planned_job_ids": ["j1", "j2"], "affected_job_ids": [], "sealed": False}})
            wt.write_evolution(fixture.campaign_dir, fixture.manifest, second)
            chain = wt.load_evolutions(fixture.campaign_dir, fixture.manifest)
            self.assertEqual([item["index"] for item in chain], [1, 2])
            effective = wt.effective_tool_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual((effective["index"], effective["identity"]["files_sha256"]), (2, second_to["files_sha256"]))
            wire = wt.effective_wire_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual((wire["source"], wire["wire_producer_sha256"]), ("evolution-02", second_to["wire_producer_sha256"]))
            self.assertEqual(wt.reservation_binding(effective), {"index": 2, "receipt_sha256": second["receipt_sha256"]})
            # 生产序号 0 的 attempt 看到两次演进的累计影响；序号 1 只看第二次。
            since0 = wt.evolution_impact_since(chain, 0, phase="candidate", candidate_id="cand")
            self.assertEqual((since0["affected_job_ids"], since0["evolution_indexes"]), (["j1"], [1, 2]))
            since1 = wt.evolution_impact_since(chain, 1, phase="candidate", candidate_id="cand")
            self.assertEqual((since1["affected_job_ids"], since1["evolution_indexes"]), ([], [2]))
            self.assertEqual(wt.evolution_identity_at(chain, fixture.manifest, 1)["files_sha256"], first_to["files_sha256"])
            self.assertEqual(wt.evolution_identity_at(chain, fixture.manifest, 0)["files_sha256"], fixture.identity["files_sha256"])
            # 链上没有记录的候选失败关闭。
            with self.assertRaisesRegex(wt.WireTransitionError, "没有候选 other 的影响记录"):
                wt.evolution_impact_since(chain, 0, phase="candidate", candidate_id="other")

    def test_broken_or_tampered_chain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            first_to = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            # 起点不是 plan 身份：拒绝写入。
            wrong_from = fixture.payload(index=1, previous=None, from_identity=first_to, to_identity=first_to)
            with self.assertRaisesRegex(wt.WireTransitionError, "起点|没有需要登记"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest, wrong_from)
            # to 与起点相同：拒绝。
            noop = fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=fixture.identity)
            with self.assertRaisesRegex(wt.WireTransitionError, "没有需要登记"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest, noop)
            # 策略变化：拒绝。
            policy_changed = dict(first_to, policy_sha256="9" * 64)
            with self.assertRaisesRegex(wt.WireTransitionError, "策略"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest,
                                   fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=policy_changed))
            # 编号跳号：拒绝。
            with self.assertRaisesRegex(wt.WireTransitionError, "编号"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest,
                                   fixture.payload(index=2, previous=None, from_identity=fixture.identity, to_identity=first_to))
            first = fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=first_to)
            path = wt.write_evolution(fixture.campaign_dir, fixture.manifest, first)
            # 写一次：同编号不可覆盖。
            with self.assertRaisesRegex(wt.WireTransitionError, "编号|禁止覆盖"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest, first)
            # 篡改受影响作业：自摘要拦住。
            payload = json.loads(path.read_text("utf-8"))
            payload["impact"]["official"]["affected_job_ids"] = []
            payload["impact"]["candidates"] = {"cand": {"affected_job_ids": []}}
            path.write_text(json.dumps(payload, sort_keys=True), "utf-8")
            with self.assertRaisesRegex(wt.WireTransitionError, "自摘要不一致"):
                wt.load_evolutions(fixture.campaign_dir, fixture.manifest)
            path.write_text(json.dumps(first, sort_keys=True), "utf-8")
            # 目录里出现非法文件：拒绝。
            (path.parent / "note.txt").write_text("x", "utf-8")
            with self.assertRaisesRegex(wt.WireTransitionError, "非法文件"):
                wt.load_evolutions(fixture.campaign_dir, fixture.manifest)
            (path.parent / "note.txt").unlink()
            # Campaign 清单字节变化（换了 Campaign）：拒绝。
            _write_json(fixture.campaign_dir / "campaign.json", {**fixture.manifest, "target_version": "0.158.0"})
            with self.assertRaisesRegex(wt.WireTransitionError, "清单摘要"):
                wt.load_evolutions(fixture.campaign_dir, fixture.manifest)

    def test_wire_transition_and_evolution_chains_cannot_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            current = fixture.mutated_identity(fixture.identity, "run_official_relay_scenario.sh")
            preview = wt.build_intent_preview(
                fixture.campaign_dir, fixture.manifest, current_identity=current, policy=fixture.policy,
                path_job_map={"run_official_relay_scenario.sh": {"official-core"}}, planned_job_ids=["official-core", "official-x"],
            )
            wt.approve_intent(fixture.campaign_dir, preview, preview["review_sha256"])
            with self.assertRaisesRegex(wt.WireTransitionError, "wire transition 链"):
                wt.write_evolution(fixture.campaign_dir, fixture.manifest,
                                   fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=current))
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            first_to = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            wt.write_evolution(fixture.campaign_dir, fixture.manifest,
                               fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=first_to))
            later = fixture.mutated_identity(first_to, "run_official_relay_scenario.sh", marker="2")
            with self.assertRaisesRegex(wt.WireTransitionError, "已登记工具演进"):
                wt.build_intent_preview(
                    fixture.campaign_dir, fixture.manifest, current_identity=later, policy=fixture.policy,
                    path_job_map={}, planned_job_ids=["official-core"],
                )

    def test_reservation_production_index_binds_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            first_to = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            first = fixture.payload(index=1, previous=None, from_identity=fixture.identity, to_identity=first_to)
            wt.write_evolution(fixture.campaign_dir, fixture.manifest, first)
            chain = wt.load_evolutions(fixture.campaign_dir, fixture.manifest)
            self.assertEqual(wt.reservation_production_index({}, chain), 0)
            self.assertEqual(
                wt.reservation_production_index({"tool_evolution": {"index": 1, "receipt_sha256": first["receipt_sha256"]}}, chain), 1
            )
            for bad in (
                {"index": 2, "receipt_sha256": first["receipt_sha256"]},
                {"index": 1, "receipt_sha256": "0" * 64},
                {"index": 1},
                {"index": True, "receipt_sha256": first["receipt_sha256"]},
            ):
                with self.subTest(bad=bad), self.assertRaises(wt.WireTransitionError):
                    wt.reservation_production_index({"tool_evolution": bad}, chain)


class CandidateReviewRecoveryLedgerTests(unittest.TestCase):
    """候选审核（VC-5 采集失败）下：对账登记失败 attempt → 恢复授权 → 同 revision 重开 VC-5。"""

    START = "2026-09-26T00:00:00+00:00"

    @staticmethod
    def _at(minutes: int) -> str:
        return (datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()

    @staticmethod
    def _bindings(root: Path, suffix: str = "") -> list[dict[str, str]]:
        """recovery_authorized 必须绑定账本目录内的恢复预览与恢复批准两份收据。"""

        bindings = []
        for role in ("recovery_approval", "recovery_preview"):
            path = root / "receipts" / f"{role}{suffix}.json"
            ledger._write_once(path, {"role": role, "status": "passed"})
            bindings.append({"role": role, "path": path.relative_to(root).as_posix(), "sha256": ledger._sha256_file(path)})
        return bindings

    def _review(self, root: Path, *, phase: str = "VC-5") -> int:
        ledger.create_ledger(
            root, upgrade_id="codex-0157", baseline_version="0.154.0", target_version="0.157.0",
            campaign_purpose="production_replacement", evidence_decision="recapture", started_at_utc=self.START,
        )
        minute = 1
        ledger.append_event(root, event_id="c0", phase="VC-0", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute))
        for name in ("VC-1", "VC-2", "VC-3"):
            minute += 1
            ledger.append_event(root, event_id=f"s-{name}", phase=name, event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute))
            minute += 1
            ledger.append_event(root, event_id=f"c-{name}", phase=name, event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute))
        minute += 1
        ledger.append_event(root, event_id="r1", phase="VC-4", event_type="stage_revision", revision=1, candidate_id="cand-a", revision_commit_sha256="a" * 64, next_action="x", recorded_at_utc=self._at(minute))
        ledger.append_event(root, event_id="s4", phase="VC-4", event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute + 1))
        if phase == "VC-5":
            ledger.append_event(root, event_id="c4", phase="VC-4", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute + 2))
            ledger.append_event(root, event_id="s5", phase="VC-5", event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute + 3))
        ledger.append_event(root, event_id="ab", phase=phase, event_type="stage_abandoned", root_cause_id="rc1-review", next_action="x", recorded_at_utc=self._at(minute + 4))
        summary = ledger.append_event(root, event_id="crr", phase=phase, event_type="candidate_review_required", candidate_id="cand-a", root_cause_id="rc1-review", next_action="reconcile", recorded_at_utc=self._at(minute + 5))
        self.assertEqual(summary["status"], "candidate_review_required")
        return minute + 6

    def test_vc5_capture_review_reopens_stage_after_reconciled_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            minute = self._review(root)
            ledger.append_event(root, event_id="as", phase="VC-5", event_type="attempt_started", attempt_id="att-1", next_action="reconcile-attempt", recorded_at_utc=self._at(minute))
            ledger.append_event(root, event_id="af", phase="VC-5", event_type="attempt_failed", attempt_id="att-1", root_cause_id="rc1-job", next_action="reconcile-attempt", recorded_at_utc=self._at(minute + 1))
            summary = ledger.inspect_ledger(root, now=self._at(minute + 1))
            self.assertEqual((summary["status"], summary["active_phase"]), ("candidate_review_required", None))
            # 授权必须绑定审核根因与唯一下一动作。
            bindings = self._bindings(root)
            for bad in ({"root_cause_id": "rc1-job", "next_action": "resume-rerun-failed"},
                        {"root_cause_id": "rc1-review", "next_action": "redispatch-same-batch"}):
                with self.subTest(bad=bad), self.assertRaisesRegex(ledger.TimingLedgerError, "候选审核的续跑授权"):
                    ledger.append_event(root, event_id="auth-bad", phase="VC-5", event_type="recovery_authorized", receipts=bindings, recorded_at_utc=self._at(minute + 2), **bad)
            summary = ledger.append_event(root, event_id="auth", phase="VC-5", event_type="recovery_authorized", root_cause_id="rc1-review", receipts=bindings, next_action="resume-rerun-failed", recorded_at_utc=self._at(minute + 2))
            self.assertEqual((summary["status"], summary["active_phase"], summary["current_revision"]), ("active", "VC-5", 1))
            state = ledger.phase_ledger_state(root, now=self._at(minute + 3))
            self.assertEqual(state["revision_phase_state"]["1"]["VC-5"], "started")
            # 重开后正常推进：同 revision 可完成 VC-5。
            ledger.append_event(root, event_id="c5", phase="VC-5", event_type="stage_completed", next_action="x", recorded_at_utc=self._at(minute + 4))
            self.assertEqual(ledger.phase_ledger_state(root, now=self._at(minute + 5))["revision_phase_state"]["1"]["VC-5"], "completed")

    def test_review_recovery_is_limited_to_vc5_and_reconciled_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            minute = self._review(root, phase="VC-4")
            # VC-4 审核不属于采集续跑：attempt_started 与授权都拒绝（VC-4 走阶段幂等重派证明）。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "attempt_started 与当前阶段"):
                ledger.append_event(root, event_id="as", phase="VC-4", event_type="attempt_started", attempt_id="att-1", next_action="x", recorded_at_utc=self._at(minute))
            with self.assertRaisesRegex(ledger.TimingLedgerError, "候选审核的续跑授权"):
                ledger.append_event(root, event_id="auth", phase="VC-4", event_type="recovery_authorized", root_cause_id="rc1-review", receipts=self._bindings(root), next_action="resume-rerun-failed", recorded_at_utc=self._at(minute))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "UpgradeTimingLedger"
            minute = self._review(root)
            # attempt 未对账（仍 active）时不得授权。
            ledger.append_event(root, event_id="as", phase="VC-5", event_type="attempt_started", attempt_id="att-1", next_action="x", recorded_at_utc=self._at(minute))
            with self.assertRaisesRegex(ledger.TimingLedgerError, "候选审核的续跑授权"):
                ledger.append_event(root, event_id="auth", phase="VC-5", event_type="recovery_authorized", root_cause_id="rc1-review", receipts=self._bindings(root), next_action="resume-rerun-failed", recorded_at_utc=self._at(minute + 1))
            # 审核期间其它派发类事件照旧拒绝。
            with self.assertRaisesRegex(ledger.TimingLedgerError, "candidate_review_required 期间"):
                ledger.append_event(root, event_id="s5-again", phase="VC-5", event_type="stage_started", next_action="x", recorded_at_utc=self._at(minute + 1))


class CandidateRecoverySuccessorTests(unittest.TestCase):
    """监督器 VC-5 候选采集续跑后继协议：采集失败 → N+1 零请求预览 → 补跑；失败分支各自 N+1。"""

    PREFIX = ["/usr/bin/python3", "/managed/tools/official_client_capture/codex_upgrade.py"]
    IDENTITY = {
        "--candidate-id": "cand-r5",
        "--build-receipt": "/campaign/candidates/cand-r5/build-receipt.json",
        "--runtime-image": "repo@sha256:" + "1" * 64,
        "--candidate-image-id": "sha256:" + "1" * 64,
        "--candidate-source": "/data/candidates/cand-r5/source",
        "--build-id": "cand-r5-build",
        "--deployed-version": "0.157.0",
        "--profile-id": "codex-0.157.0-official-r1",
        "--profile-digest": "2" * 64,
        "--candidate-purpose": "production_replacement",
    }

    def _capture_command(self, campaign: str) -> list[str]:
        tokens = [*self.PREFIX, "capture-candidate", "run", "--campaign-dir", campaign]
        for flag, value in self.IDENTITY.items():
            tokens.extend([flag, value])
        return [*tokens, "--max-wall-seconds", "21600", "--acknowledge-live-requests"]

    def _manifest(self, sequence: int, actions: list, *, campaign_id: str, revision: int = 1) -> dict:
        return {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": campaign_id,
            "campaign_plan_sha256": "1" * 64,
            "batch_id": f"vc-5-{sequence:04d}",
            "batch_sequence": sequence,
            "batch_sha256": str(sequence % 10) * 64,
            "phase": "VC-5",
            "predecessor_checkpoint": {"path": "control/vc/vc-4-checkpoint.json", "sha256": "3" * 64, "phase": "VC-4", "checkpoint_sha256": "4" * 64},
            "original_deadline_at_utc": "2099-09-14T12:00:00Z",
            "no_op": False,
            "candidate_id": "cand-r5",
            "candidate_revision": revision,
            "evaluation_baseline": None,
            "baseline_commit_sha256": None,
            "evaluator_digests": {"checker_sha256": "5" * 64, "builder_sha256": "6" * 64, "compare_reader_sha256": "7" * 64, "accept_reader_sha256": "8" * 64},
            "actions": actions,
            "execute_items": ["candidate-run"],
            "reuse_items": [],
        }

    def _failed_run(self, root: Path, name: str, *, campaign_id: str, action_id: str, failure: tuple[str, str, str]) -> tuple[dict, Path]:
        run_dir = root / name
        run_dir.mkdir(mode=0o700)
        nonce = hashlib.sha256(name.encode()).hexdigest()
        state = {"state": "failed", "campaign_id": campaign_id, "phase": "VC-5", "owner_pid": os.getpid(), "owner_nonce": nonce, "terminal_at_utc": "2026-09-26T21:40:00.000Z"}
        _write_json(run_dir / "state.json", state)
        stop = {
            "schema_version": supervisor.STOP_SCHEMA, "campaign_id": campaign_id, "detected_at_epoch": 1005.0,
            "detected_at_utc": "2026-09-26T21:40:00.000Z", "event_type": "failed", "owner_nonce": nonce,
            "owner_pid": os.getpid(), "phase": "VC-5", "reason": f"action-failed:{action_id}",
        }
        stop["receipt_sha256"] = supervisor._sha256(supervisor._canonical(stop))
        _write_json(run_dir / "stop-receipt.json", stop)
        failure_kind, error_type, failure_class = failure
        supervisor._write_action_diagnostic(
            supervisor._action_diagnostic_path(run_dir, action_id, create_directory=True),
            campaign_id=campaign_id, phase="VC-5", action_id=action_id, owner_pid=os.getpid(), owner_nonce=nonce,
            failure_kind=failure_kind, failure_class=failure_class, error_type=error_type, message="子命令以非零状态退出，未提供进一步的脱敏诊断。",
        )
        return state, run_dir

    def _preview_action(self, campaign: str) -> dict:
        return {
            "action_id": supervisor.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID,
            "operation": supervisor.CANDIDATE_RECOVERY_OPERATION,
            "timeout_seconds": 1800.0,
            "command": supervisor.candidate_recovery_preview_command(self.PREFIX, campaign, self.IDENTITY),
            "item_ids": ["candidate-run"],
        }

    def _run_action(self, campaign: str) -> dict:
        return {
            "action_id": supervisor.CANDIDATE_RECOVERY_RUN_ACTION_ID,
            "operation": supervisor.CANDIDATE_RECOVERY_OPERATION,
            "timeout_seconds": 21600.0,
            "command": supervisor.candidate_recovery_run_command(
                self.PREFIX, campaign, self.IDENTITY, campaign + "/control/reconciliation/attempt-x/recovery-preview-01.json"
            ),
            "item_ids": ["candidate-run"],
        }

    def _capture_history(self, root: Path, *, failure=("child-returncode", "ChildProcessError", "execution-failure")):
        campaign = str(root / "campaign")
        campaign_id = "c-vc5-recovery"
        capture = self._manifest(1, [{
            "action_id": "candidate-run", "operation": "VC-5:capture-candidate-run", "timeout_seconds": 21600.0,
            "command": self._capture_command(campaign), "item_ids": ["candidate-run"],
        }], campaign_id=campaign_id)
        state, run_dir = self._failed_run(root, "run-capture", campaign_id=campaign_id, action_id="candidate-run", failure=failure)
        return campaign, campaign_id, [(state, capture, run_dir)]

    def test_failed_capture_is_followed_by_zero_request_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, campaign_id, history = self._capture_history(root)
            preview = self._manifest(2, [self._preview_action(campaign)], campaign_id=campaign_id)
            # 修复后评估器摘要可以随工具演进变化（续跑批次按新授权编译）。
            preview["evaluator_digests"] = {**preview["evaluator_digests"], "checker_sha256": "9" * 64}
            ordered = supervisor._validate_batched_campaign_history(preview, history)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1])
            # 候选身份参数漂移（换了构建）：拒绝。
            drifted = copy.deepcopy(preview)
            drifted["actions"][0]["command"][drifted["actions"][0]["command"].index("--build-id") + 1] = "other-build"
            with self.assertRaisesRegex(SupervisorError, "候选身份参数"):
                supervisor._validate_batched_campaign_history(drifted, history)
            # 候选 revision 或 execute 分区变化：拒绝。
            for field, value in (("candidate_revision", 2), ("execute_items", ["candidate-run", "x"]), ("reuse_items", ["candidate-core-direct"])):
                changed = copy.deepcopy(preview)
                changed[field] = value
                with self.subTest(field=field), self.assertRaisesRegex(SupervisorError, "候选身份参数|批次结构"):
                    supervisor._validate_batched_campaign_history(changed, history)
            # 失败采集之后直接真实补跑（跳过零请求预览）：没有协议承接。
            direct_run = self._manifest(2, [self._run_action(campaign)], campaign_id=campaign_id)
            with self.assertRaisesRegex(SupervisorError, "唯一直接 v3 恢复后继"):
                supervisor._validate_batched_campaign_history(direct_run, history)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, campaign_id, history = self._capture_history(
                root, failure=("handled-error", "CampaignCleanupRequested", "deadline-expired")
            )
            preview = self._manifest(2, [self._preview_action(campaign)], campaign_id=campaign_id)
            with self.assertRaisesRegex(SupervisorError, "不是处理型失败"):
                supervisor._validate_batched_campaign_history(preview, history)

    def test_failed_preview_is_redispatched_verbatim_and_failed_run_gets_new_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, campaign_id, history = self._capture_history(root)
            preview = self._manifest(2, [self._preview_action(campaign)], campaign_id=campaign_id)
            preview_state, preview_dir = self._failed_run(
                root, "run-preview", campaign_id=campaign_id, action_id=supervisor.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID,
                failure=("handled-error", "ConfigurationError", "execution-failure"),
            )
            history = [*history, (preview_state, preview, preview_dir)]
            retry = copy.deepcopy(preview)
            retry.update(batch_id="vc-5-0003", batch_sequence=3, batch_sha256="3" * 64)
            ordered = supervisor._validate_batched_campaign_history(retry, history)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2])
            live = copy.deepcopy(retry)
            live["actions"] = [self._run_action(campaign)]
            with self.assertRaisesRegex(SupervisorError, "逐字沿用父预览批次"):
                supervisor._validate_batched_campaign_history(live, history)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign, campaign_id, history = self._capture_history(root)
            preview = self._manifest(2, [self._preview_action(campaign)], campaign_id=campaign_id)
            preview_state = {"state": "stopped", "campaign_id": campaign_id, "phase": "VC-5"}
            run = self._manifest(3, [self._run_action(campaign)], campaign_id=campaign_id)
            run_state, run_dir = self._failed_run(
                root, "run-recovery", campaign_id=campaign_id, action_id=supervisor.CANDIDATE_RECOVERY_RUN_ACTION_ID,
                failure=("child-returncode", "ChildProcessError", "execution-failure"),
            )
            history = [*history, (preview_state, preview, root / "run-preview-ok"), (run_state, run, run_dir)]
            next_preview = self._manifest(4, [self._preview_action(campaign)], campaign_id=campaign_id)
            ordered = supervisor._validate_batched_campaign_history(next_preview, history)
            self.assertEqual([item[1]["batch_sequence"] for item in ordered], [1, 2, 3])
            again = self._manifest(4, [self._run_action(campaign)], campaign_id=campaign_id)
            with self.assertRaisesRegex(SupervisorError, "零请求预览"):
                supervisor._validate_batched_campaign_history(again, history)

    def test_capture_command_parsing_and_action_classification(self) -> None:
        campaign = "/campaign"
        parsed = supervisor.candidate_capture_run_identity(self._capture_command(campaign))
        self.assertIsNotNone(parsed)
        prefix, parsed_campaign, identity = parsed
        self.assertEqual((prefix, parsed_campaign, identity), (self.PREFIX, campaign, self.IDENTITY))
        segment = self._capture_command(campaign)
        segment[-1:-1] = ["--attempt-recovery", "ar1"]
        rerun = [*self._capture_command(campaign), "--rerun-failed"]
        missing = [token for token in self._capture_command(campaign) if token not in {"--build-id", "cand-r5-build"}]
        duplicate = [*self._capture_command(campaign)[:-1], "--build-id", "x", "--acknowledge-live-requests"]
        for bad in (segment, rerun, missing, duplicate, self._capture_command(campaign)[:-1]):
            with self.subTest(bad=bad[-3:]):
                self.assertIsNone(supervisor.candidate_capture_run_identity(bad))
        manifest = self._manifest(1, [
            {"action_id": "candidate-run", "command": self._capture_command(campaign)},
            self._preview_action(campaign),
            self._run_action(campaign),
            {"action_id": "candidate-seal", "command": [*self.PREFIX, "capture-candidate", "seal", "--campaign-dir", campaign]},
        ], campaign_id="c")
        self.assertEqual(supervisor.candidate_capture_recovery_action(manifest, "candidate-run"), "capture")
        self.assertEqual(supervisor.candidate_capture_recovery_action(manifest, supervisor.CANDIDATE_RECOVERY_PREVIEW_ACTION_ID), "preview")
        self.assertEqual(supervisor.candidate_capture_recovery_action(manifest, supervisor.CANDIDATE_RECOVERY_RUN_ACTION_ID), "run")
        self.assertIsNone(supervisor.candidate_capture_recovery_action(manifest, "candidate-seal"))
        self.assertIsNone(supervisor.candidate_capture_recovery_action({**manifest, "phase": "VC-4"}, "candidate-run"))


class ToolEvolutionPreviewTests(unittest.TestCase):
    """编排器 tool-evolution 预览／批准：逐作业影响、拒绝条件、评估器 b0 授权迁移与落盘后的门禁。"""

    OFFICIAL = ["official-core", "official-relay"]
    CANDIDATE = ["candidate-core-direct", "candidate-frozen-core", "candidate-trace-test"]

    def _run(self, fixture: EvolutionFixture, current: dict, *, path_map: dict, sealed: set[str] = frozenset(),
             outputs: dict | None = None, approve: str | None = None, approved_by: str | None = "tester",
             deployment_created: str = "2026-09-27T01:00:00Z") -> dict:
        root = fixture.root
        stage_paths = {}
        for stage, cid in (("capture-official", None), ("capture-candidate", "cand")):
            path = root / "stage" / f"{stage}-{cid}.json"
            if f"{stage}:{cid}" in sealed:
                _write_json(path, {"status": "complete"})
            else:
                path.unlink(missing_ok=True)
            stage_paths[(stage, cid)] = path
        ledger_dir = root / "ledger"
        ledger_dir.mkdir(exist_ok=True)
        (fixture.campaign_dir / "candidates" / "cand" / "attempts").mkdir(parents=True, exist_ok=True)
        official_jobs = [SimpleNamespace(job_id=job_id) for job_id in self.OFFICIAL]
        candidate_jobs = [SimpleNamespace(job_id=job_id) for job_id in self.CANDIDATE]
        evaluator = {
            "checker_sha256": next(e["sha256"] for e in current["entries"] if e["path"] == "candidate_rule_assertion.py"),
            "builder_sha256": next(e["sha256"] for e in current["entries"] if e["path"] == "build_rule_assertion_results.py"),
            "compare_reader_sha256": "7" * 64,
            "accept_reader_sha256": "8" * 64,
        }
        deployment = {"path": "/control/deploy.json", "sha256": "d" * 64, "created_at_utc": deployment_created,
                      "tool_files_sha256": current["files_sha256"], "policy_sha256": current["policy_sha256"],
                      "wire_producer_sha256": current["wire_producer_sha256"]}
        arguments = SimpleNamespace(campaign_dir=fixture.campaign_dir, fix_commit="f" * 40, reason="修复 A15 采集脚本",
                                    control_root=root / "control", approve_sha256=approve, approved_by=approved_by)
        with mock.patch.multiple(
            codex_upgrade,
            _require_formal_campaign=mock.Mock(return_value=fixture.manifest),
            _tool_identity=mock.Mock(return_value=current),
            _campaign_jobs=mock.Mock(return_value=official_jobs),
            _tool_evolution_candidate_jobs=mock.Mock(return_value=(candidate_jobs, list(self.CANDIDATE))),
            _tool_path_job_map=mock.Mock(return_value={key: set(value) for key, value in path_map.items()}),
            _optional_campaign_timing_ledger_dir=mock.Mock(return_value=ledger_dir),
            _current_evaluation_baseline=mock.Mock(return_value=(0, None)),
            _candidate_b0_evaluation_outputs=mock.Mock(side_effect=lambda _cd, cid: list((outputs or {}).get(cid, []))),
            _stage_path=mock.Mock(side_effect=lambda _cd, stage, cid=None: (stage, stage_paths[(stage, cid)])),
            _tool_evolution_quiescence_problems=mock.Mock(return_value=[]),
        ), mock.patch.object(
            codex_upgrade.codex_upgrade_timing_ledger, "inspect_ledger",
            mock.Mock(return_value={"status": "active", "head_sequence": 14, "head_sha256": "e" * 64}),
        ), mock.patch.object(
            codex_upgrade.codex_upgrade_project_ledger, "find_project_ledger", mock.Mock(return_value=None),
        ), mock.patch.object(
            codex_upgrade.codex_upgrade_tool_identity_policy, "evaluator_dependency_digests", mock.Mock(return_value=evaluator),
        ), mock.patch.object(reconciler, "_deployment_receipt", mock.Mock(return_value=deployment)):
            return codex_upgrade._tool_evolution_command(arguments)

    def test_preview_maps_changed_capture_script_to_declared_candidate_jobs_and_apply_moves_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            current = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            path_map = {"run_candidate_core_capture.sh": {"candidate-core-direct", "candidate-frozen-core"}}
            preview = self._run(fixture, current, path_map=path_map, sealed={"capture-official:None"})
            self.assertEqual(preview["status"], "approval_required")
            self.assertEqual(preview["changes"]["impact_paths"], ["run_candidate_core_capture.sh"])
            self.assertEqual(preview["changes"]["unmapped_paths"], [])
            self.assertEqual(preview["impact"]["official"]["affected_job_ids"], [])
            self.assertEqual(preview["impact"]["candidates"]["cand"]["affected_job_ids"], ["candidate-core-direct", "candidate-frozen-core"])
            self.assertEqual(preview["evaluator"]["changed_fields"], [])
            # 批准摘要不一致、缺批准人：拒绝，不落盘。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "批准摘要"):
                self._run(fixture, current, path_map=path_map, sealed={"capture-official:None"}, approve="0" * 64)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "approved-by"):
                self._run(fixture, current, path_map=path_map, sealed={"capture-official:None"}, approve=preview["review_sha256"], approved_by="")
            self.assertEqual(wt.load_evolutions(fixture.campaign_dir, fixture.manifest), [])
            applied = self._run(fixture, current, path_map=path_map, sealed={"capture-official:None"}, approve=preview["review_sha256"])
            self.assertEqual(applied["status"], "evolution_applied")
            effective = codex_upgrade._campaign_effective_tool_identity(fixture.campaign_dir, fixture.manifest)
            self.assertEqual((effective["index"], effective["identity"]["files_sha256"]), (1, current["files_sha256"]))
            # 登记后当前树即有效身份：零写入预检放行；再有未登记变化则拒绝。
            self.assertEqual(
                codex_upgrade._require_tool_evolution_registered(fixture.campaign_dir, fixture.manifest, current=current, action="t")["index"], 1
            )
            later = fixture.mutated_identity(current, "codex_upgrade_supervisor.py", marker="3")
            with self.assertRaisesRegex(codex_upgrade.ToolEvolutionRequired, "control"):
                codex_upgrade._require_tool_evolution_registered(fixture.campaign_dir, fixture.manifest, current=later, action="t")
            # 同一树再预览：无需登记。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "无需登记"):
                self._run(fixture, current, path_map=path_map, sealed={"capture-official:None"})
            # 下一次演进的部署收据不得早于上一次。
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "不晚于上一次"):
                self._run(fixture, later, path_map=path_map, sealed={"capture-official:None"}, deployment_created="2026-09-27T00:30:00Z")

    def test_sealed_or_unmapped_impact_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            # 映射不到任何作业的产出侧变化：按全部作业受影响，官方已封存即拒绝。
            unmapped = fixture.mutated_identity(fixture.identity, "capture.py")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "已封存的 official"):
                self._run(fixture, unmapped, path_map={}, sealed={"capture-official:None"})
            # 官方作业确实依赖变化文件：拒绝。
            relay = fixture.mutated_identity(fixture.identity, "run_official_relay_scenario.sh")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "official-relay"):
                self._run(fixture, relay, path_map={"run_official_relay_scenario.sh": {"official-relay"}}, sealed={"capture-official:None"})
            # 已封存候选的作业受影响：拒绝（封存后补采走 evaluation-recover）。
            script = fixture.mutated_identity(fixture.identity, "run_candidate_core_capture.sh")
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "候选 cand 已封存"):
                self._run(fixture, script, path_map={"run_candidate_core_capture.sh": {"candidate-frozen-core"}},
                          sealed={"capture-official:None", "capture-candidate:cand"})
            # 官方未封存时映射不到的变化只把作业记为全部受影响，不拒绝。
            preview = self._run(fixture, unmapped, path_map={})
            self.assertEqual(preview["impact"]["official"]["affected_job_ids"], self.OFFICIAL)
            self.assertEqual(preview["impact"]["candidates"]["cand"]["affected_job_ids"], self.CANDIDATE)

    def test_checker_change_moves_b0_authorization_only_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            plan_digests = codex_upgrade._plan_evaluator_entry_digests(fixture.manifest)
            checker = fixture.mutated_identity(fixture.identity, "candidate_rule_assertion.py", marker="4")
            preview = self._run(fixture, checker, path_map={}, sealed={"capture-official:None"})
            self.assertEqual(preview["evaluator"]["changed_fields"], ["checker_sha256"])
            self.assertTrue(preview["evaluator"]["candidates"]["cand"]["b0_authorization_moved"])
            # checker 属于 evidence 层，不是产出侧变化：没有作业受影响。
            self.assertEqual(preview["changes"]["impact_paths"], [])
            self._run(fixture, checker, path_map={}, sealed={"capture-official:None"}, approve=preview["review_sha256"])
            authorized = codex_upgrade._b0_evaluator_authorized_digests(fixture.campaign_dir, fixture.manifest, "cand")
            self.assertEqual(authorized["checker_sha256"], "4" * 64)
            self.assertEqual(authorized["builder_sha256"], plan_digests["builder_sha256"])
            # 登记时还不存在的候选同样取迁移后的值。
            self.assertEqual(codex_upgrade._b0_evaluator_authorized_digests(fixture.campaign_dir, fixture.manifest, "new")["checker_sha256"], "4" * 64)
        with tempfile.TemporaryDirectory() as directory:
            fixture = EvolutionFixture(Path(directory).resolve())
            plan_digests = codex_upgrade._plan_evaluator_entry_digests(fixture.manifest)
            checker = fixture.mutated_identity(fixture.identity, "candidate_rule_assertion.py", marker="4")
            outputs = {"cand": ["assertions/cand/results.json"]}
            preview = self._run(fixture, checker, path_map={}, sealed={"capture-official:None"}, outputs=outputs)
            self.assertFalse(preview["evaluator"]["candidates"]["cand"]["b0_authorization_moved"])
            self._run(fixture, checker, path_map={}, sealed={"capture-official:None"}, outputs=outputs, approve=preview["review_sha256"])
            # b0 已有评估产出：授权不迁，仍是 plan 值（须 evaluation-recover 开新基线重评）。
            self.assertEqual(
                codex_upgrade._b0_evaluator_authorized_digests(fixture.campaign_dir, fixture.manifest, "cand"), plan_digests
            )


class EvolutionRecoveryScopeTests(unittest.TestCase):
    """续跑闭集、seal 核对与对账前置对工具演进的口径。"""

    def _scope(self, **overrides) -> dict:
        scope = {
            "planned_job_ids": ["a", "b", "c"],
            "completed_job_ids": ["a"],
            "failed_job_ids": ["b"],
            "pending_job_ids": [],
            "execute_job_ids": ["b", "c"],
            "tool_evolution": {"source_index": 0, "evolution_indexes": [1], "affected_job_ids": ["b", "c"], "invalidated_job_ids": ["c"]},
        }
        scope.update(overrides)
        return scope

    def _validate(self, scope: dict) -> tuple[set, set]:
        jobs = [SimpleNamespace(job_id=job_id) for job_id in ("a", "b", "c")]
        reservation = {"planned_jobs": [{"id": job_id, "execution_sha256": job_id * 64} for job_id in ("a", "b", "c")]}
        with mock.patch.multiple(
            codex_upgrade,
            _load_capture_reservation=mock.Mock(return_value=reservation),
            _job_execution_sha256=mock.Mock(side_effect=lambda job: job.job_id * 64),
        ):
            return codex_upgrade._validate_recovery_scope_plan(
                Path("/campaign"), phase="candidate", candidate_id="cand", source_root=Path("/campaign/a1"),
                scope=scope, planned_jobs=jobs,
            )

    def test_scope_plan_accepts_evolution_invalidated_jobs_only_as_declared(self) -> None:
        self.assertEqual(self._validate(self._scope()), ({"a"}, {"b", "c"}))
        # 没有 tool_evolution 声明时，执行集合只能是 failed ∪ pending。
        without = self._scope()
        without.pop("tool_evolution")
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "闭集不一致"):
            self._validate(without)
        # 失效作业不得与失败／已完成重叠，也不得漏出执行集合。
        for invalidated in (["b"], ["a"], []):
            scope = self._scope()
            scope["tool_evolution"] = dict(scope["tool_evolution"], invalidated_job_ids=invalidated)
            with self.subTest(invalidated=invalidated), self.assertRaisesRegex(codex_upgrade.ConfigurationError, "闭集不一致"):
                self._validate(scope)
        scope = self._scope()
        scope["tool_evolution"] = dict(scope["tool_evolution"], invalidated_job_ids=["c", "b"])
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "失效作业非法"):
            self._validate(scope)

    def test_seal_refuses_results_affected_after_production_index(self) -> None:
        attempt = {"results": [{"id": "a", "status": "complete"}, {"id": "b", "status": "complete"}]}
        with mock.patch.object(codex_upgrade, "_attempt_evolution_impact", mock.Mock(return_value={
            "index": 0, "affected_job_ids": ["b", "z"], "changed_paths": ["run_x.sh"], "evolution_indexes": [1],
        })):
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "结果已失效：b"):
                codex_upgrade._require_attempt_current_under_evolutions(
                    Path("/c"), {}, Path("/c/a1"), attempt, phase="candidate", candidate_id="cand"
                )
        with mock.patch.object(codex_upgrade, "_attempt_evolution_impact", mock.Mock(return_value={
            "index": 1, "affected_job_ids": [], "changed_paths": [], "evolution_indexes": [],
        })):
            codex_upgrade._require_attempt_current_under_evolutions(
                Path("/c"), {}, Path("/c/a1"), attempt, phase="candidate", candidate_id="cand"
            )

    def test_reconciler_refuses_unregistered_identity_for_live_campaigns_only(self) -> None:
        changed = {"policy_version": "v2", "unchanged": False}
        for status in ("active", "recovery_required", "candidate_review_required", "stage_review_required", "deadline_paused"):
            with self.subTest(status=status), self.assertRaisesRegex(reconciler.ReconcilerError, "tool-evolution"):
                reconciler._require_registered_tool_identity(changed, {"status": status})
        for identity, status in (
            (changed, "stopped"), (changed, "complete"),
            ({"policy_version": "v2", "unchanged": True}, "active"),
            ({"policy_version": "v1", "unchanged": False}, "active"),
        ):
            with self.subTest(identity=identity, status=status):
                reconciler._require_registered_tool_identity(identity, {"status": status})

    def test_candidate_review_binding_is_lenient_for_accounting_and_strict_for_authorization(self) -> None:
        """候选审核下：对账其它旧 attempt 仍按原规则只入账；续跑授权只属于引起审核的那次失败采集。"""

        review = {"event_type": "candidate_review_required", "phase": "VC-5", "candidate_id": "cand", "root_cause_id": "rc1-review", "event_id": "crr"}
        ledger_state = {"status": "candidate_review_required"}
        failed_run = {"run_id": "run-9", "started_at_epoch": 100.0}

        def call(*, attempt_id: str, strict: bool, window=(("att-9", None),), bind_error: bool = False):
            binder = mock.Mock(side_effect=codex_upgrade.ConfigurationError("无法唯一定位")) if bind_error else mock.Mock(return_value=failed_run)
            with mock.patch.object(reconciler, "_last_candidate_review_event", mock.Mock(return_value=review)), \
                    mock.patch.object(codex_upgrade, "_candidate_failed_run_for_review", binder), \
                    mock.patch.object(supervisor, "candidate_reservations_in_run_window", mock.Mock(return_value=list(window))):
                return reconciler._candidate_capture_review(
                    Path("/c"), {}, Path("/l"), ledger_state, phase="candidate", candidate_id="cand", attempt_id=attempt_id, strict=strict
                )

        self.assertEqual(call(attempt_id="att-9", strict=False)["root_cause_id"], "rc1-review")
        self.assertEqual(call(attempt_id="att-9", strict=True)["phase"], "VC-5")
        self.assertIsNone(call(attempt_id="att-old", strict=False))
        self.assertIsNone(call(attempt_id="att-9", strict=False, bind_error=True))
        with self.assertRaisesRegex(reconciler.ReconcilerError, "窗口内"):
            call(attempt_id="att-old", strict=True)
        with self.assertRaisesRegex(reconciler.ReconcilerError, "无法绑定失败父 run"):
            call(attempt_id="att-9", strict=True, bind_error=True)
        # 非候选审核、非 VC-5、别的候选：不适用。
        with mock.patch.object(reconciler, "_last_candidate_review_event", mock.Mock(return_value={**review, "phase": "VC-4"})):
            self.assertIsNone(reconciler._candidate_capture_review(Path("/c"), {}, Path("/l"), ledger_state, phase="candidate", candidate_id="cand", attempt_id="att-9", strict=True))
        self.assertIsNone(reconciler._candidate_capture_review(Path("/c"), {}, Path("/l"), {"status": "active"}, phase="candidate", candidate_id="cand", attempt_id="att-9", strict=True))

    def test_recovery_preview_moves_evolution_affected_complete_jobs_into_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory).resolve()
            receipt_dir = campaign_dir / "control" / "reconciliation" / "attempt-a1"
            receipt_dir.mkdir(parents=True)
            jobs = {
                "planned_job_ids": ["a", "b", "c"],
                "groups": {"complete": ["a", "c"], "failed": ["b"], "indeterminate": [], "pending": []},
            }

            def preview(impact: dict) -> dict:
                with mock.patch.object(reconciler, "_locate_attempt", mock.Mock(return_value=("candidate", "cand", campaign_dir / "a1"))), \
                        mock.patch.object(codex_upgrade, "_attempt_evolution_impact", mock.Mock(return_value=impact)):
                    return reconciler._recovery_preview(
                        campaign_dir, receipt_dir, manifest={"campaign_id": "c"}, attempt_id="a1", phase="candidate",
                        candidate_id="cand", attempt_exists=True, jobs=jobs, environment_status="restored",
                        provenance_copy={"jobs": []}, current={"policy_sha256": "p", "wire_producer_sha256": "w", "files_sha256": "f"},
                        reconciliation_receipt_sha256="r" * 64, campaign_ledger_head={"head_sequence": 3, "head_sha256": "h", "status": "candidate_review_required"},
                        project_ledger_head={"sequence": 9, "head_sha256": "x"}, now="2026-09-27T00:00:00Z",
                    )

            plain = preview({"index": 0, "affected_job_ids": [], "changed_paths": [], "evolution_indexes": []})
            self.assertEqual((plain["reuse_job_ids"], plain["execute_job_ids"]), (["a", "c"], ["b"]))
            self.assertNotIn("tool_evolution", plain)
            evolved = preview({"index": 0, "affected_job_ids": ["b", "c"], "changed_paths": ["run_x.sh"], "evolution_indexes": [1]})
            self.assertEqual((evolved["reuse_job_ids"], evolved["execute_job_ids"]), (["a"], ["b", "c"]))
            self.assertEqual(evolved["tool_evolution"], {"source_index": 0, "evolution_indexes": [1], "invalidated_job_ids": ["c"]})
            self.assertEqual(evolved["index"], 2)


if __name__ == "__main__":
    unittest.main()
