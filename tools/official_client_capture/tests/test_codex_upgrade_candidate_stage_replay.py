"""R18：候选审核下 VC-4 零请求动作的工具缺陷修好接着跑。

VC-4 的 plan-candidate-gates／record-candidate-build 父批次失败后账本进入 candidate_review_required。对账证明
动作可幂等续作时写阶段幂等重派证明，并在同一 revision 重开 VC-4，逐字重派同一批次；证明不了幂等（包装脚本、
参数不闭合、候选已作废、已写不可覆盖的输出、已写半成品与动作不一致等）时维持只入账，由人工作废候选或停线。
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

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests
from tools.official_client_capture.tests import test_codex_upgrade_timing_ledger as timing_tests

CANDIDATE = "candidate-r1"
IMAGE_ID = "sha256:" + "e" * 64
BUILD_ID = "build-r18-test"


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class CandidateReviewLedgerTests(unittest.TestCase):
    """账本状态机：候选审核只在 VC-4 凭同阶段、同审核根因的证明重开，且阶段仍归当前 revision。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "UpgradeTimingLedger"
        self.helper = timing_tests.TimingLedgerTests()
        self.helper._create(self.root)
        self.minute = self.helper._advance_to_vc3(self.root)
        self._append("r1", "VC-4", "stage_revision", revision=1, candidate_id="cand-a", revision_commit_sha256="a" * 64)

    def _append(self, event_id: str, phase: str, event_type: str, **fields: object) -> dict:
        self.minute += 1
        fields.setdefault("next_action", "x")
        return ledger.append_event(self.root, event_id=event_id, phase=phase, event_type=event_type,
                                   recorded_at_utc=self.helper._at(self.minute), **fields)

    def _review(self, phase: str, cause: str = "rc-vc4") -> None:
        if phase == "VC-5":
            self._append("s4", "VC-4", "stage_started")
            self._append("c4", "VC-4", "stage_completed")
        self._append(f"s-{phase}", phase, "stage_started")
        self._append(f"a-{phase}", phase, "stage_abandoned", root_cause_id=cause)
        self._append(f"crr-{phase}", phase, "candidate_review_required", candidate_id="cand-a", root_cause_id=cause,
                     next_action="candidate_review_required：先对账")

    def _bind(self, role: str, payload: dict) -> dict:
        path = self.root / "receipts" / f"{role}-{self.minute}.json"
        ledger._write_once(path, payload)
        return {"role": role, "path": path.relative_to(self.root).as_posix(), "sha256": ledger._sha256_file(path)}

    def _receipts(self, *, phase: str = "VC-4", cause: str = "rc-vc4", roles=("provenance", "reconciliation", "stage_replay")) -> list:
        reconciliation = {"status": "recoverable", "reservation_exists": False, "minute": self.minute}
        digest = hashlib.sha256(
            (json.dumps(reconciliation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest()
        proof = reconciler.build_stage_replay_proof(
            campaign_id="codex-r18", run_id="run-0004", review_event_id=f"crr-{phase}", review_root_cause_id=cause,
            reconciliation_receipt_sha256=digest, commit_sha256="c" * 64, next_action="redispatch-same-batch",
            replay={"phase": phase, "allowed": True, "reasons": [], "actions": [{
                "action_id": "record-candidate-build", "inputs": {}, "outputs": {},
                "command": ["python3", "codex_upgrade.py", "record-candidate-build"],
            }]},
        )
        payloads = {"provenance": {"role": "provenance"}, "reconciliation": reconciliation, "stage_replay": proof}
        return sorted((self._bind(role, payloads[role]) for role in roles), key=lambda item: item["role"])

    def test_vc4_review_reopens_same_revision_with_replay_proof(self) -> None:
        self._review("VC-4")
        summary = self._append("pass", "VC-4", "receipt_passed", receipts=self._receipts(),
                               next_action="redispatch-same-batch")
        self.assertEqual((summary["status"], summary["active_phase"], summary["next_action"]),
                         ("active", "VC-4", "redispatch-same-batch"))
        state = ledger.phase_ledger_state(self.root, now=self.helper._at(self.minute + 1))
        self.assertEqual(state["revision_phase_state"], {"1": {"VC-4": "started"}})
        # 重开后的阶段仍是当前 revision：候选级完成事件按 r1 归属，阶段随后正常关闭。
        self._append("c4-after", "VC-4", "stage_completed")
        state = ledger.phase_ledger_state(self.root, now=self.helper._at(self.minute + 1))
        self.assertEqual(state["completed_phases"], ["VC-0", "VC-1", "VC-2", "VC-3", "VC-4"])
        self.assertEqual(state["revision_phase_state"], {"1": {"VC-4": "completed"}})

    def test_reopened_vc4_can_still_be_invalidated(self) -> None:
        """重开不剥夺人工裁定：判为候选源码问题时仍可放弃阶段并作废候选。"""

        self._review("VC-4")
        self._append("pass", "VC-4", "receipt_passed", receipts=self._receipts(), next_action="redispatch-same-batch")
        self._append("a4-again", "VC-4", "stage_abandoned", root_cause_id="rc-source")
        summary = self._append("inv", "VC-4", "candidate_invalidated", candidate_id="cand-a", root_cause_id="rc-inv",
                               next_action="revision-open --supersedes cand-a")
        self.assertEqual(summary["status"], "revision_required")

    def test_candidate_review_replay_rejects_other_phase_cause_or_roles(self) -> None:
        for label in ("vc5", "cause", "roles"):
            with self.subTest(label=label):
                self.setUp()
                phase = "VC-5" if label == "vc5" else "VC-4"
                self._review(phase)
                if label == "vc5":
                    receipts, pattern = self._receipts(phase="VC-5"), "只允许 VC-4"
                elif label == "cause":
                    receipts, pattern = self._receipts(cause="rc-other"), "未证明动作可幂等重派"
                else:
                    receipts, pattern = self._receipts(roles=("reconciliation", "stage_replay")), "只允许 VC-4"
                with self.assertRaisesRegex(ledger.TimingLedgerError, pattern):
                    self._append("pass", phase, "receipt_passed", receipts=receipts, next_action="redispatch-same-batch")
                self.assertEqual(ledger.inspect_ledger(self.root, now=self.helper._at(self.minute + 1))["status"],
                                 "candidate_review_required")

    def test_receipt_without_replay_proof_keeps_candidate_review(self) -> None:
        self._review("VC-4")
        self._append("rp", "VC-4", "receipt_passed", receipts=self._receipts(roles=("provenance", "reconciliation")))
        summary = ledger.inspect_ledger(self.root, now=self.helper._at(self.minute + 1))
        self.assertEqual((summary["status"], summary["active_phase"]), ("candidate_review_required", None))


class CandidateStageReplayChainTests(unittest.TestCase):
    """真实子进程链：VC-4 record 父批次失败 → 对账证明 → 同一 revision N+1 逐字重派；不可幂等时留在候选审核。"""

    def setUp(self) -> None:
        self.case = upgrade_tests.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def _to_vc4(self, root: Path) -> dict:
        fixture = self.case._vc_chain_fixture(root)
        campaign = fixture["campaign_dir"]
        for sequence, phase in ((2, "VC-2"), (3, "VC-3")):
            plan = self.case._vc_chain_action_plan(root, campaign, phase)
            result, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, phase, sequence, plan))
            self.assertEqual(code, 0, result)
        upgrade.open_candidate_revision(argparse.Namespace(campaign_dir=campaign, candidate_id=CANDIDATE, initial=True, supersedes=None))
        return fixture

    def _record_flags(self, root: Path, fixture: dict) -> dict[str, str]:
        source = root / "candidate-source"
        _write(source / "gates" / "gate-plan.json", {"plan": "fixture"})
        _write(source / "catalog" / "catalog-stage-receipt.json", {"catalog": "fixture"})
        binary = root / "artifacts" / "sub2api"
        binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary.write_bytes(b"fixture-binary")
        binary.chmod(0o755)
        for name in ("build-tree", "docker-context", "frontend-dist"):
            (root / name).mkdir(mode=0o700)
            (root / name / "file.txt").write_text(name, encoding="utf-8")
        implementation = root / "implementation"
        _write(implementation / "receipt.json", {"kind": "implementation_tests"})
        manifest = fixture["manifest"]
        return {
            "--campaign-dir": str(fixture["campaign_dir"]), "--candidate-id": CANDIDATE,
            "--candidate-purpose": str(manifest["campaign_purpose"]),
            "--candidate-source": str(source), "--candidate-binary": str(binary),
            "--runtime-image": f"sub2apiplus@sha256:{'9' * 64}", "--candidate-image-id": IMAGE_ID,
            "--build-id": BUILD_ID, "--deployed-version": str(manifest["target_version"]),
            "--target-architecture": "linux/arm64",
            "--build-parameters": str(_write(root / "artifacts" / "build-parameters.json", {"params": 1})),
            "--build-tree": str(root / "build-tree"), "--docker-context": str(root / "docker-context"),
            "--frontend-dist-source": str(root / "frontend-dist"),
            "--catalog-stage-dir": str(source / "catalog"),
            "--source-transition": str(_write(root / "artifacts" / "source-transition.json", {"transition": 1})),
            "--gate-plan": str(source / "gates" / "gate-plan.json"),
            "--implementation-test-root": str(implementation),
            "--implementation-test-receipt": str(implementation / "receipt.json"),
        }

    @staticmethod
    def _command(action: str, flags: dict[str, str]) -> list[str]:
        return [sys.executable, str(Path(upgrade.__file__).resolve()), action,
                *[item for pair in flags.items() for item in pair]]

    def _plan(self, root: Path, name: str, action: str, flags: dict[str, str]) -> Path:
        path = root / "action-plans" / f"{name}.json"
        _write(path, {
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": [action], "reuse_item_ids": [],
            "actions": [{"action_id": action, "operation": f"VC-4:{action}", "timeout_seconds": 120,
                         "command": self._command(action, flags), "item_ids": [action]}],
        })
        return path

    @staticmethod
    def _manifest(fixture: dict, command: list[str], **overrides: object) -> dict:
        return {"phase": "VC-4", "campaign_id": fixture["manifest"]["campaign_id"], "candidate_id": CANDIDATE,
                "candidate_revision": 1, "actions": [{"action_id": "record-candidate-build", "command": command}],
                **overrides}

    def test_record_failure_reconciles_to_verbatim_redispatch_in_same_revision(self) -> None:
        root = self.base / "chain"
        root.mkdir(mode=0o700)
        fixture = self._to_vc4(root)
        campaign, timing = fixture["campaign_dir"], fixture["timing_ledger"]
        flags = self._record_flags(root, fixture)
        plan = self._plan(root, "record", "record-candidate-build", flags)
        failed, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-4", 4, plan))
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, "action-failed:record-candidate-build"), failed)
        self.assertEqual(ledger.inspect_ledger(timing)["status"], "candidate_review_required")
        run_dir = Path(failed["campaign_run"]["run_dir"])

        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertEqual(result["status"], "recoverable", result)
        proof = result["stage_replay"]
        self.assertTrue(proof["allowed"], proof)
        self.assertEqual((proof["phase"], proof["next_action"]), ("VC-4", "redispatch-same-batch"))
        self.assertEqual(set(proof["actions"][0]["inputs"]), {
            "--candidate-binary", "--build-parameters", "--source-transition", "--implementation-test-receipt",
            "--gate-plan", "--candidate-source", "--build-tree", "--docker-context", "--frontend-dist-source",
            "--catalog-stage-dir", "--implementation-test-root",
        })
        schema = json.loads(Path(upgrade.__file__).with_name("codex_upgrade_stage_replay.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(proof), set(schema["required"]))
        self.assertIn(proof["phase"], schema["properties"]["phase"]["enum"])
        self.assertIn("同一 revision 重开", result["next_command"])
        state = ledger.inspect_ledger(timing)
        self.assertEqual((state["status"], state["active_phase"], state["next_action"]),
                         ("active", "VC-4", "redispatch-same-batch"))
        head = state["head_sequence"]
        # 重复对账幂等：证明与账本都不重复写。
        again = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertEqual(again["stage_replay"], proof)
        self.assertEqual(ledger.inspect_ledger(timing)["head_sequence"], head)

        inner = supervisor._read_json(run_dir / "campaign-run-manifest.json")["manifest"]
        prior_state = supervisor._read_state(run_dir)
        self.assertTrue(supervisor._validate_batched_stage_review_successor(prior_state, inner, run_dir, inner, campaign_dir=campaign))
        changed = json.loads(json.dumps(inner))
        changed["actions"][0]["command"] = [*changed["actions"][0]["command"], "--heartbeat-seconds", "5"]
        with self.assertRaisesRegex(supervisor.SupervisorError, "原批次内容重派"):
            supervisor._validate_batched_stage_review_successor(prior_state, inner, run_dir, changed, campaign_dir=campaign)
        parameters = Path(flags["--build-parameters"])
        original = parameters.read_bytes()
        parameters.write_bytes(original + b"\n")
        with self.assertRaisesRegex(supervisor.SupervisorError, "漂移"):
            supervisor._validate_batched_stage_review_successor(prior_state, inner, run_dir, inner, campaign_dir=campaign)
        parameters.write_bytes(original)

        # 同一 revision N+1 逐字重派被后继协议接纳并真实执行；夹具输入不是真构建，动作再次失败回到候选审核。
        again_failed, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-4", 5, plan))
        self.assertEqual((code, again_failed["campaign_run"]["reason"]), (1, "action-failed:record-candidate-build"), again_failed)
        self.assertTrue((campaign / "control" / "vc" / "commits" / "0005-vc-4.json").is_file())
        self.assertEqual(ledger.inspect_ledger(timing)["status"], "candidate_review_required")

    def test_existing_plan_output_keeps_candidate_review_after_accounting(self) -> None:
        root = self.base / "plan-gates"
        root.mkdir(mode=0o700)
        fixture = self._to_vc4(root)
        campaign, timing = fixture["campaign_dir"], fixture["timing_ledger"]
        source = root / "plan-source"
        _write(source / "gates" / "mapping.json", {"mapping": 1})
        output = _write(source / "gates" / "gate-plan-dispatch.json", {"half": True})
        flags = {"--campaign-dir": str(campaign), "--candidate-id": CANDIDATE, "--candidate-source": str(source),
                 "--mapping": str(source / "gates" / "mapping.json"), "--output": str(output)}
        plan = self._plan(root, "plan-gates", "plan-candidate-gates", flags)
        failed, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-4", 4, plan))
        self.assertEqual(code, 1, failed)
        run_dir = Path(failed["campaign_run"]["run_dir"])
        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertFalse(result["stage_replay"]["allowed"])
        self.assertIn("输出已存在", " ".join(result["stage_replay"]["reasons"]))
        self.assertIn("invalidate-candidate", result["next_command"])
        self.assertIn("修复不可幂等半成品", result["next_command"])
        self.assertEqual(ledger.inspect_ledger(timing)["status"], "candidate_review_required")
        self.assertFalse(any(event["event_id"] == f"reconcile-run-passed-{run_dir.name}"
                             for event, _ in ledger._load_events(timing)))
        # 移走半成品后重新对账即取得重派许可（维持原收据，重开 VC-4）。
        output.unlink()
        retried = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertTrue(retried["stage_replay"]["allowed"], retried)
        self.assertEqual(ledger.inspect_ledger(timing)["active_phase"], "VC-4")

    def test_vc4_replay_contract_matrix(self) -> None:
        root = self.base / "matrix"
        root.mkdir(mode=0o700)
        fixture = self._to_vc4(root)
        campaign = fixture["campaign_dir"]
        flags = self._record_flags(root, fixture)
        facts = upgrade._campaign_stage_replay_facts
        allowed = facts(campaign, self._manifest(fixture, self._command("record-candidate-build", flags)))
        self.assertTrue(allowed["allowed"], allowed)
        self.assertEqual(allowed["actions"][0]["outputs"], {})
        relative = facts(campaign, self._manifest(fixture, self._command(
            "record-candidate-build", {**flags, "--implementation-test-receipt": "receipt.json"})))
        self.assertEqual(relative["actions"][0]["inputs"], allowed["actions"][0]["inputs"])

        receipt_path = upgrade._candidate_build_receipt_path(campaign, CANDIDATE)
        _write(receipt_path, {"candidate_id": CANDIDATE, "build": {"build_id": BUILD_ID}, "image": {"image_id": IMAGE_ID}})
        written = facts(campaign, self._manifest(fixture, self._command("record-candidate-build", flags)))
        self.assertTrue(written["allowed"], written)
        self.assertEqual(set(written["actions"][0]["outputs"]), {f"candidates/{CANDIDATE}/build-receipt.json"})
        _write(receipt_path, {"candidate_id": CANDIDATE, "build": {"build_id": "other-build"}, "image": {"image_id": IMAGE_ID}})

        link = root / "linked-parameters.json"
        link.symlink_to(Path(flags["--build-parameters"]))
        cases = {
            "构建标识不一致": self._manifest(fixture, self._command("record-candidate-build", flags)),
            "不是受管 codex_upgrade 的直接调用": self._manifest(fixture, [sys.executable, "-c", "pass", "record-candidate-build"]),
            "没有幂等合同": self._manifest(fixture, self._command("capture-candidate", flags)),
            "参数没有精确闭合": self._manifest(fixture, self._command("record-candidate-build", {**flags, "--unknown": "x"})),
            "不属于当前 revision": self._manifest(fixture, self._command("record-candidate-build", {**flags, "--candidate-id": "other"})),
            "输入文件不可信": self._manifest(fixture, self._command("record-candidate-build", {**flags, "--build-parameters": str(link)})),
        }
        missing = dict(flags)
        missing.pop("--gate-plan")
        cases["参数没有精确闭合 "] = self._manifest(fixture, self._command("record-candidate-build", missing))
        cases["与失败批次绑定不一致"] = self._manifest(fixture, self._command("record-candidate-build", flags), candidate_revision=2)
        for reason, manifest in cases.items():
            with self.subTest(reason=reason.strip()):
                result = facts(campaign, manifest)
                self.assertFalse(result["allowed"], result)
                self.assertIn(reason.strip(), " ".join(result["reasons"]))
        receipt_path.unlink()
        marker = campaign / "candidates" / CANDIDATE / upgrade.CANDIDATE_INVALIDATION_FILENAME
        _write(marker, {"invalidated": True})
        invalidated = facts(campaign, self._manifest(fixture, self._command("record-candidate-build", flags)))
        self.assertIn("已作废或被取代", " ".join(invalidated["reasons"]))
        vc5 = facts(campaign, {**self._manifest(fixture, self._command("record-candidate-build", flags)), "phase": "VC-5"})
        self.assertIn("无已知幂等动作合同", " ".join(vc5["reasons"]))
        self.assertTrue(os.path.islink(link))


if __name__ == "__main__":
    unittest.main()
