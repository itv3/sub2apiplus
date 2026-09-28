"""R4：阶段审核不能绕过半成品、永久条件、预约分流或 write-once 收口。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_evidence_permissions as evidence_permissions
from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture.tests import runtime_egress_fixtures
from tools.official_client_capture.tests import test_codex_upgrade as upgrade_tests
from tools.official_client_capture.tests import test_codex_upgrade_supervisor as supervisor_tests
from tools.official_client_capture.tests import test_codex_upgrade_timing_ledger as timing_tests


class StageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = upgrade_tests.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_unknown_partial_action_remains_review_after_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self.case._vc_chain_fixture(root)
            campaign = fixture["campaign_dir"]
            plan = self.case._vc_chain_action_plan(root, campaign, "VC-2", fail=True)
            failed, code = upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-2", 2, plan))
            self.assertEqual(code, 1)
            result = reconciler.reconcile_supervisor_run(Path(failed["campaign_run"]["run_dir"]), campaign)
            self.assertEqual(result["status"], "stage_review_required")
            self.assertFalse(result["stage_replay"]["allowed"])
            self.assertEqual(timing.inspect_ledger(fixture["timing_ledger"])["status"], "stage_review_required")
            with self.assertRaisesRegex(upgrade.ConfigurationError, "stage_review_required"):
                upgrade.compile_and_run_vc_batch(self.case._vc_chain_arguments(fixture, "VC-2", 3, plan))

    def test_catalog_partial_directory_has_no_replay_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign = root / "campaign"
            campaign.mkdir()
            output = root / "catalog"
            output.mkdir()
            (output / "partial.json").write_text("{}")
            manifest = {"phase": "VC-3", "campaign_id": "campaign", "actions": [{
                "action_id": "stage", "command": [sys.executable, str(Path(upgrade.__file__).resolve()),
                    "stage-profile", "--campaign-dir", str(campaign), "--output", str(output)]}]}
            result = upgrade._campaign_stage_replay_facts(campaign, manifest)
            self.assertFalse(result["allowed"])
            self.assertTrue(result["reasons"])

    def test_vc1_reservation_uses_attempt_preview_and_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = self.case._b0_fixture(Path(directory).resolve())
            campaign, ledger = fixture["campaign_dir"], fixture["timing_ledger"]
            timing.append_event(ledger, event_id="vc0-done", phase="VC-0", event_type="stage_completed")
            timing.append_event(ledger, event_id="vc1-start", phase="VC-1", event_type="stage_started")
            run = self.case._b0_run_dir(fixture, "r4-reserved")
            attempt = self.case._b0_orphan_attempt(fixture)
            for event_type in ("stage_abandoned", "stage_review_required"):
                timing.append_event(ledger, event_id=event_type, phase="VC-1", event_type=event_type,
                                    root_cause_id="reservation-interrupted", next_action="reconcile-attempt")
            with self.assertRaisesRegex(reconciler.ReconcilerError, "改用 reconcile-attempt"):
                reconciler.reconcile_supervisor_run(run, campaign)
            result = reconciler.reconcile_attempt(campaign, attempt)
            self.assertEqual(result["status"], "recoverable", result)
            preview = Path(result["recovery_preview_path"])
            with self.assertRaisesRegex(reconciler.ReconcilerError, "尚未批准"):
                reconciler.load_approved_recovery_preview(campaign, preview, phase="official", candidate_id=None)
            reconciler.approve_recovery_preview(campaign, attempt, approve_sha256=result["recovery_preview"]["review_sha256"])
            approved = reconciler.load_approved_recovery_preview(campaign, preview, phase="official", candidate_id=None)
            self.assertEqual(approved["execute_job_ids"], ["official-test"])
            state = timing.inspect_ledger(ledger)
            self.assertEqual((state["status"], state["active_phase"]), ("active", "VC-1"))
            self.assertEqual(state["total_live_request_count"], 0)

    def test_sigkill_between_abandon_and_review_appends_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            campaign, ledger, manifest = supervisor_tests.SupervisorTests()._timing_closeout_fixture(root)
            script = '''import json,os,signal,sys
from pathlib import Path
from tools.official_client_capture import codex_upgrade_supervisor as s
original=s.timing_ledger.append_event
def append(*args,**kwargs):
    if kwargs.get("event_type")=="stage_review_required": os.kill(os.getpid(),signal.SIGKILL)
    return original(*args,**kwargs)
s.timing_ledger.append_event=append
s._close_failed_campaign_timing_ledger(Path(sys.argv[1]),json.loads(sys.argv[2]),failed_action_id="failing-action")
'''
            completed = subprocess.run([sys.executable, "-c", script, str(campaign), json.dumps(manifest)],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, -signal.SIGKILL, completed.stderr)
            originals = {p: p.read_bytes() for p in (ledger / "events").glob("*.json")}
            for _ in range(2):
                supervisor._close_failed_campaign_timing_ledger(campaign, manifest, failed_action_id="failing-action")
            events = [event for event, _ in timing._load_events(ledger)]
            self.assertEqual(sum(e["event_type"] == "stage_review_required" for e in events), 1)
            self.assertEqual(sum(e["event_type"] == "stage_abandoned" for e in events), 1)
            self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_integrity_and_existing_stop_required_still_stop(self):
        for failure in ("evidence-integrity", "stop_required"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                root.chmod(0o700)
                campaign, ledger, manifest = supervisor_tests.SupervisorTests()._timing_closeout_fixture(root)
                if failure == "stop_required":
                    for index in range(2):
                        timing.append_event(ledger, event_id=f"start-{index}", phase="VC-1", event_type="attempt_started", attempt_id=f"attempt-{index}")
                        timing.append_event(ledger, event_id=f"failed-{index}", phase="VC-1", event_type="attempt_failed", attempt_id=f"attempt-{index}", root_cause_id="same-cause")
                result = supervisor._close_failed_campaign_timing_ledger(campaign, manifest, failed_action_id="failing-action",
                    failure_class="evidence-integrity" if failure == "evidence-integrity" else "execution-failure")
                self.assertEqual(result["ledger_status"], "stopped")

    def test_legacy_checkpoint_without_review_fields_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ledger"
            helper = timing_tests.TimingLedgerTests()
            helper._create(root)
            receipt = timing.build_checkpoint(root, observed_at_utc=helper._at(1))
            receipt["summary"].pop("review_phase")
            receipt["summary"].pop("review_root_cause_id")
            timing._write_once(root / "receipts" / "legacy.json", receipt)
            self.assertEqual(timing.replay(root, "receipts/legacy.json"), receipt)


class OfficialSealChainStageReplayTests(unittest.TestCase):
    """D-10（草表 D-10，矩阵第 24 行）：VC-1 官方 seal 链零请求后处理动作以 execution-failure 失败后，
    凭阶段幂等重派合同修好接着跑。

    真实可达形态：只读导入的 Formal Campaign（no-op 首批、VC-1 尚未封存），待封存的官方 attempt 与
    reuse-official-evidence 导入的零执行 attempt 一样没有 Job checkpoint 目录，post-run-tooling 五条判据因此
    必然不成立。录制链同形的 VC-1 seal 预览批次（断言包准备＋seal 预览）经原子入口真实派发：断言包脚本非零
    退出、未发布断言包，父监督器按 child-returncode 记诊断、判据不成立保持 execution-failure，账本进入
    stage_abandoned＋stage_review_required。修复前 ``_campaign_stage_replay_facts`` 对 VC-1 固定不许可：
    对账只入账、账本停在审核、任何后继都被拒——死路。
    """

    ASSERTION = "prepare-official-assertion-bundle"
    PREVIEW = "seal-official-preview"
    APPROVE = "seal-official-approve"

    def setUp(self) -> None:
        self.case = upgrade_tests.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def _fixture(self, root: Path) -> dict:
        """只读导入形态的 Formal Campaign：no-op 首批、账本停在 active VC-0、VC-1 尚未封存（与
        ``_vc_chain_fixture`` 同形，只是不写 VC-1 checkpoint），外加一个待封存的零执行官方 attempt。"""

        self.case.enterContext(runtime_egress_fixtures.offline_campaign_egress())
        original = upgrade._create_initial_vc_control_artifacts

        def as_reuse(*args: object, **kwargs: object) -> dict:
            kwargs["reuse_official_jobs"] = True
            return original(*args, **kwargs)

        with mock.patch.object(upgrade, "_create_initial_vc_control_artifacts", side_effect=as_reuse):
            fixture = self.case._b0_fixture(root, campaign_id="upgrade-0154-d10")
        # 生产布局里受管数据根只有一个（工具树与 Campaign 同在 data 根下）；夹具的 Campaign 在临时 data 根，
        # 收口、attempt 读取与监督器 VC-1 断言包门禁都按这同一个数据根核对权限收口边界。
        self.case.enterContext(mock.patch.object(
            evidence_permissions, "_managed_data_root", return_value=Path(fixture["data"]).resolve(strict=True)
        ))
        campaign = fixture["campaign_dir"]
        manifest_path = campaign / "campaign.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["predecessor"] = {
            "campaign_dir": str(root / "predecessor-fixture"),
            "campaign_id": "upgrade-0154-d10-predecessor",
            "campaign_manifest_sha256": "0" * 64,
            "reason": upgrade.OFFICIAL_EVIDENCE_REUSE_REASON,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (campaign / "campaign.sha256").write_text(upgrade.file_sha256(manifest_path) + "\n", encoding="utf-8")
        fixture["manifest"] = manifest
        state_dir = root / "supervisor"
        state_dir.mkdir(mode=0o700)
        fixture["state_dir"] = state_dir
        fixture["attempt_root"] = self._zero_execution_official_attempt(fixture)
        return fixture

    def _zero_execution_official_attempt(self, fixture: dict) -> Path:
        """待封存的零执行官方 attempt：正式预约、证据权限收口与 attempt 收据，没有 Job checkpoint 与逐 Job 结果文件。"""

        campaign, manifest = fixture["campaign_dir"], fixture["manifest"]
        attempt_root, _reservation = upgrade._reserve_capture_attempt(
            campaign, phase="official", candidate_id=None, identity=dict(manifest["official_identity"]),
            jobs=fixture["jobs"], allow_failed_rerun=True,
        )
        results = [
            {
                "id": job.job_id, "phase": "official", "required": True,
                "execution_sha256": upgrade._job_execution_sha256(job), "status": "complete",
                "description": "零执行导入的官方 Job", "duration_seconds": 0.0, "steps": [], "evidence_roots": [],
                "missing_evidence_patterns": [], "empty_evidence_patterns": [], "covers": [],
                "scenario_ids": list(job.scenario_ids), "scenario_receipts": [], "scenario_receipt_failures": [],
                "track": "main", "model_id": "gpt-5.5", "expected_use_responses_lite": False,
                "required_model_receipt": False, "model_condition_receipt": None,
                "model_condition_receipt_failure": None, "disposition": "executed",
            }
            for job in fixture["jobs"]
        ]
        evidence_root, logs_root = attempt_root / "evidence", attempt_root / "logs"
        evidence_root.mkdir(mode=0o700, exist_ok=True)
        logs_root.mkdir(mode=0o700, exist_ok=True)
        closeout = upgrade._close_attempt_evidence_permissions(attempt_root, [evidence_root, logs_root])
        upgrade._write_capture_attempt(campaign, attempt_root, {
            "campaign_id": manifest["campaign_id"], "phase": "official", "candidate_id": None,
            "status": "awaiting_receipts", "identity": dict(manifest["official_identity"]), "results": results,
            "failure_observations": [], "evidence_roots": [str(evidence_root), str(logs_root)],
            "evidence_permission_closeout": closeout, "evidence_permission_error": None,
            "environment": {
                "evidence_root": str(evidence_root), "before_probe": None, "after_probe": {"status": "passed"},
                "restoration_report": {"status": "passed"}, "arm64_before_receipt": None, "arm64_after_receipt": None,
            },
            "binary_verification": None, "execution_error": None, "restoration_error": None,
            "next_gate": "零请求：执行 capture-official seal 生成预览，再以 --approve-seal-sha256 批准封存。",
        })
        self.assertFalse((attempt_root / "checkpoints").exists())
        return attempt_root

    def _assertion_action(self, campaign: Path, attempt_id: str) -> dict:
        """受管断言包脚本的 /usr/bin/env 形态（录制链同形；解释器取本机 /bin/bash）。"""

        module = Path(upgrade.__file__).resolve()
        repo_root = module.parents[2]
        assignments = {
            "CAMPAIGN_DIR": str(campaign), "ATTEMPT_ID": attempt_id, "SIDE": "official",
            "REPO_ROOT": str(repo_root), "TOOL_ROOT": str(module.parent),
        }
        return {
            "action_id": self.ASSERTION, "operation": "VC-1:prepare-official-assertion-bundle", "timeout_seconds": 120,
            "item_ids": [self.ASSERTION],
            "command": ["/usr/bin/env", *[f"{key}={value}" for key, value in assignments.items()], "/bin/bash",
                        str(repo_root / "tools" / "prepare_assertion_bundle.sh")],
        }

    def _seal_action(self, campaign: Path, attempt_id: str, *, approve: str | None = None, extra: tuple[str, ...] = ()) -> dict:
        """受管 Python 直接调用受管 codex_upgrade 的 capture-official seal（预览或批准）。"""

        module = Path(upgrade.__file__).resolve()
        bundle = campaign / "official" / "attempts" / attempt_id / "evidence" / "assertion-bundle"
        command = [sys.executable, str(module), "capture-official", "seal", "--campaign-dir", str(campaign),
                   "--attempt-id", attempt_id]
        if approve is None:
            command += ["--capture-manifest", str(bundle / "capture-manifest.json"), "--assertion-evidence-root", str(bundle)]
            action_id, operation = self.PREVIEW, "VC-1:capture-official-seal-preview"
        else:
            command += ["--approve-seal-sha256", approve]
            action_id, operation = self.APPROVE, "VC-1:capture-official-seal-approve"
        return {"action_id": action_id, "operation": operation, "timeout_seconds": 120, "item_ids": [action_id],
                "command": [*command, *extra]}

    def _preview_batch(self, campaign: Path, attempt_id: str) -> list[dict]:
        return [self._assertion_action(campaign, attempt_id), self._seal_action(campaign, attempt_id)]

    @staticmethod
    def _plan(root: Path, name: str, actions: list[dict]) -> Path:
        path = root / "action-plans" / f"{name}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": sorted(item for action in actions for item in action["item_ids"]),
            "reuse_item_ids": [], "actions": actions,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path.resolve(strict=True)

    @staticmethod
    def _manifest(fixture: dict, actions: list[dict], *, execute: list[str] | None = None,
                  reuse: list[str] | None = None) -> dict:
        return {
            "phase": "VC-1", "campaign_id": fixture["manifest"]["campaign_id"],
            "execute_items": execute if execute is not None else sorted(i for a in actions for i in a["item_ids"]),
            "reuse_items": [] if reuse is None else reuse, "actions": actions,
        }

    def test_seal_chain_execution_failure_reconciles_to_single_protocol_redispatch(self) -> None:
        """真实派发的 VC-1 seal 链批次失败 → 对账按合同写阶段幂等重派证明、账本重开 VC-1 → 有且只有阶段审核
        协议承接 N+1 重派（逐字或只调执行细节）→ N+1 被原子入口接纳并真实执行；对账前、半成品漂移、批次身份
        变化与官方证据封存后都失败关闭，全程零请求。"""

        root = self.base / "chain"
        root.mkdir(mode=0o700)
        fixture = self._fixture(root)
        campaign, ledger_dir, attempt_root = fixture["campaign_dir"], fixture["timing_ledger"], fixture["attempt_root"]
        bundle = attempt_root / "evidence" / "assertion-bundle"
        plan = self._plan(root, "seal-preview", self._preview_batch(campaign, attempt_root.name))
        arguments = upgrade_tests.CodexUpgradeTest._vc_chain_arguments
        failed, code = upgrade.compile_and_run_vc_batch(arguments(fixture, "VC-1", 2, plan))
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, f"action-failed:{self.ASSERTION}"), failed)
        diagnostic = failed["campaign_run"]["actions"][0]["diagnostic"]
        self.assertEqual(diagnostic["effective_failure_class"], "execution-failure", diagnostic)
        self.assertIn("checkpoint", " ".join(diagnostic["post_run_tooling_rejected"]))
        self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stage_review_required")
        self.assertFalse(bundle.exists() or any(path.name.startswith(".assertion-work.") for path in bundle.parent.iterdir()))
        run_dir = Path(failed["campaign_run"]["run_dir"])
        inner = supervisor._read_json(run_dir / "campaign-run-manifest.json")["manifest"]
        prior_state = supervisor._read_state(run_dir)
        successor = json.loads(json.dumps(inner))
        successor.update(batch_id="vc-1-0003", batch_sequence=3, batch_sha256="3" * 64)
        # 父 run 必须已对账：对账前没有阶段幂等重派证明，阶段审核协议不承接；原子入口按账本审核态零写入拒绝。
        self.assertFalse(
            supervisor._validate_batched_stage_review_successor(prior_state, inner, run_dir, successor, campaign_dir=campaign)
        )
        with self.assertRaisesRegex(upgrade.ConfigurationError, "stage_review_required"):
            upgrade.compile_and_run_vc_batch(arguments(fixture, "VC-1", 3, plan))

        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertEqual(result["status"], "recoverable", result)
        proof = result["stage_replay"]
        self.assertTrue(proof["allowed"], proof)
        self.assertEqual((proof["phase"], proof["next_action"]), ("VC-1", "redispatch-same-batch"))
        self.assertEqual([item["action_id"] for item in proof["actions"]], [self.ASSERTION, self.PREVIEW])
        attempt_relative = attempt_root.relative_to(campaign).as_posix()
        self.assertEqual(set(proof["actions"][0]["inputs"]), {
            "prepare_assertion_bundle.sh", f"{attempt_relative}/attempt.json",
            f"{attempt_relative}/evidence-permission-closeout.json",
        })
        self.assertEqual(proof["actions"][0]["outputs"], {})
        schema = json.loads(Path(upgrade.__file__).with_name("codex_upgrade_stage_replay.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(proof), set(schema["required"]))
        for key, rule in schema["properties"].items():
            if "const" in rule:
                self.assertEqual(proof[key], rule["const"], key)
            if "enum" in rule:
                self.assertIn(proof[key], rule["enum"], key)
        for action in proof["actions"]:
            self.assertEqual(set(action), set(schema["properties"]["actions"]["items"]["required"]))
        self.assertTrue((campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "stage-replay.json").is_file())
        state = timing.inspect_ledger(ledger_dir)
        self.assertEqual((state["status"], state["active_phase"], state["next_action"]),
                         ("active", "VC-1", "redispatch-same-batch"))
        head = state["head_sequence"]
        again = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertEqual(again["stage_replay"], proof)
        self.assertEqual(timing.inspect_ledger(ledger_dir)["head_sequence"], head)

        # 有且只有阶段审核协议承接 N+1 重派：逐字重派与只调执行细节（timeout）的重派一样，其余 14 条协议形态
        # 不符返回 False、不抛错（seal 链续派协议让位，不再在阶段审核协议之前失败关闭）。
        retimed = json.loads(json.dumps(successor))
        retimed["actions"][1]["timeout_seconds"] = 600.0
        for label, candidate in (("逐字", successor), ("调 timeout", retimed)):
            with self.subTest(successor=label):
                accepted = [name for name, protocol in supervisor._SUCCESSOR_PROTOCOLS
                            if protocol(prior_state, inner, run_dir, candidate, campaign_dir=campaign)]
                self.assertEqual(accepted, ["stage_review"])
        # 批次身份变化（去掉断言包动作的续派）不是重派：阶段审核协议按身份漂移失败关闭。
        history = supervisor._campaign_run_history(fixture["state_dir"], str(inner["campaign_id"]))
        repreview = json.loads(json.dumps(successor))
        repreview.update(actions=[repreview["actions"][1]], execute_items=[self.PREVIEW])
        with self.assertRaisesRegex(supervisor.SupervisorError, "阶段审核对账后只允许原批次内容重派"):
            supervisor._validate_batched_campaign_history(repreview, history, campaign_dir=campaign)
        # 直接后继派发前复算实物：断言包在对账后出现、或官方证据已封存，都判输入／半成品漂移并拒绝。
        stage_result = campaign / "official" / "result.json"
        for label, path, make, remove in (
            ("断言包已发布", bundle, lambda p: p.mkdir(mode=0o700), lambda p: p.rmdir()),
            ("官方已封存", stage_result, lambda p: p.write_text("{}\n", encoding="utf-8"), lambda p: p.unlink()),
        ):
            with self.subTest(drift=label):
                make(path)
                try:
                    with self.assertRaisesRegex(supervisor.SupervisorError, "漂移"):
                        supervisor._validate_batched_stage_review_successor(
                            prior_state, inner, run_dir, successor, campaign_dir=campaign
                        )
                finally:
                    remove(path)

        # N+1 逐字重派被原子入口接纳（后继协议链由阶段审核协议承接）并真实执行；夹具证据没有 Job 证据根，
        # 断言包脚本再次非零退出，回到阶段审核。全程零请求。
        again_failed, code = upgrade.compile_and_run_vc_batch(arguments(fixture, "VC-1", 3, plan))
        self.assertEqual((code, again_failed["campaign_run"]["reason"]), (1, f"action-failed:{self.ASSERTION}"), again_failed)
        self.assertTrue((campaign / "control" / "vc" / "commits" / "0003-vc-1.json").is_file())
        self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stage_review_required")
        self.assertFalse(bundle.exists())
        totals = project.replay_head(fixture["ledger"])
        self.assertEqual((totals["precise_total"], totals["estimated_total"]), (0, 0))

    def test_unreplayable_approve_batch_stays_in_review_with_named_reason(self) -> None:
        """单动作 seal 批准批次的批准摘要对不上任何冻结预览（例如摘要抄错）：真实派发后子进程拒绝批准、判据不成立
        保持 execution-failure；对账只入账、账本留在阶段审核，文案点名合同不成立的原因（逐字重派必然再被拒），
        不再笼统提示"补齐受支持的恢复合同"，也不写阶段幂等重派证明。"""

        root = self.base / "approve"
        root.mkdir(mode=0o700)
        fixture = self._fixture(root)
        campaign, ledger_dir, attempt_root = fixture["campaign_dir"], fixture["timing_ledger"], fixture["attempt_root"]
        plan = self._plan(root, "seal-approve", [self._seal_action(campaign, attempt_root.name, approve="c" * 64)])
        arguments = upgrade_tests.CodexUpgradeTest._vc_chain_arguments
        failed, code = upgrade.compile_and_run_vc_batch(arguments(fixture, "VC-1", 2, plan))
        self.assertEqual((code, failed["campaign_run"]["reason"]), (1, f"action-failed:{self.APPROVE}"), failed)
        diagnostic = failed["campaign_run"]["actions"][0]["diagnostic"]
        self.assertEqual(diagnostic["effective_failure_class"], "execution-failure", diagnostic)
        self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stage_review_required")
        run_dir = Path(failed["campaign_run"]["run_dir"])
        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertEqual(result["status"], "stage_review_required", result)
        self.assertFalse(result["stage_replay"]["allowed"])
        self.assertIn("VC-1 阶段幂等重派合同不成立", result["next_command"])
        self.assertIn("没有与批准摘要一致的 seal 预览", result["next_command"])
        self.assertNotIn("补齐受支持的恢复合同", result["next_command"])
        self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stage_review_required")
        self.assertFalse(any(event["event_id"] == f"reconcile-run-passed-{run_dir.name}"
                             for event, _ in timing._load_events(ledger_dir)))
        self.assertFalse((campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "stage-replay.json").exists())
        with self.assertRaisesRegex(upgrade.ConfigurationError, "stage_review_required"):
            upgrade.compile_and_run_vc_batch(arguments(fixture, "VC-1", 3, plan))

    def test_official_seal_chain_replay_contract_matrix(self) -> None:
        """合同只放行官方 seal 链三个零请求后处理动作且逐字重派确实幂等的批次；采集等动作维持原结论，已发布断言包、
        暂存残留、已封存、作废／隔离的 attempt、非受管命令与参数不闭合都留在阶段审核并给出原因。"""

        root = self.base / "matrix"
        root.mkdir(mode=0o700)
        fixture = self._fixture(root)
        campaign, attempt_root = fixture["campaign_dir"], fixture["attempt_root"]
        attempt_id = attempt_root.name
        facts = upgrade._campaign_stage_replay_facts
        attempt_relative = attempt_root.relative_to(campaign).as_posix()

        allowed = facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id)))
        self.assertTrue(allowed["allowed"], allowed)
        self.assertEqual(allowed["reasons"], [])
        self.assertEqual([item["action_id"] for item in allowed["actions"]], [self.ASSERTION, self.PREVIEW])
        self.assertEqual(set(allowed["actions"][1]["inputs"]),
                         {f"{attempt_relative}/attempt.json", f"{attempt_relative}/evidence-permission-closeout.json"})
        self.assertEqual(allowed["actions"][1]["outputs"], {})
        # 复算确定：同一实物两次得到逐字相同的证明内容。
        self.assertEqual(facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id))), allowed)

        # seal 批准：冻结草案与摘要一致的预览存在时，已写半成品按摘要绑定；缺草案或摘要不一致，批准必然被拒。
        review = "a" * 64
        draft, preview = attempt_root / "seal-draft.json", attempt_root / "seal-preview.json"
        preview.write_text(json.dumps({"review_sha256": review}) + "\n", encoding="utf-8")
        approve = self._manifest(fixture, [self._seal_action(campaign, attempt_id, approve=review)])
        self.assertIn("冻结草案", " ".join(facts(campaign, approve)["reasons"]))
        draft.write_text("{}\n", encoding="utf-8")
        approved = facts(campaign, approve)
        self.assertTrue(approved["allowed"], approved)
        self.assertEqual(set(approved["actions"][0]["outputs"]),
                         {f"{attempt_relative}/seal-draft.json", f"{attempt_relative}/seal-preview.json"})
        mismatch = facts(campaign, self._manifest(fixture, [self._seal_action(campaign, attempt_id, approve="b" * 64)]))
        self.assertIn("批准摘要", " ".join(mismatch["reasons"]))
        # 预览半成品绑定进 seal 预览动作的 outputs（write-or-verify 在其上续作）。
        with_preview = facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id)))
        self.assertTrue(with_preview["allowed"], with_preview)
        self.assertEqual(set(with_preview["actions"][1]["outputs"]),
                         {f"{attempt_relative}/seal-draft.json", f"{attempt_relative}/seal-preview.json"})
        draft.unlink()
        preview.unlink()

        other_python = root / "python-other"
        other_python.write_text("", encoding="utf-8")
        other_script = root / "prepare_assertion_bundle.sh"
        shutil.copyfile(Path(upgrade.__file__).resolve().parents[1] / "prepare_assertion_bundle.sh", other_script)
        other_module = root / "elsewhere" / "codex_upgrade.py"
        other_module.parent.mkdir(mode=0o700)
        shutil.copyfile(Path(upgrade.__file__).resolve(), other_module)
        preview_action = self._seal_action(campaign, attempt_id)
        other_attempt = dict(preview_action, command=[
            "20990101T000000Z-" + "0" * 16 if token == attempt_id else token for token in preview_action["command"]
        ])
        unmanaged = dict(preview_action, command=[str(other_python), *preview_action["command"][1:]])
        foreign_module = dict(preview_action, command=[sys.executable, str(other_module), *preview_action["command"][2:]])
        renamed = dict(preview_action, action_id=self.APPROVE, item_ids=[self.APPROVE])
        assertion = self._assertion_action(campaign, attempt_id)
        candidate_side = dict(assertion, command=[
            "SIDE=candidate" if token == "SIDE=official" else token for token in assertion["command"]
        ])
        foreign_script = dict(assertion, command=[*assertion["command"][:-1], str(other_script)])
        declared = dict(assertion, command=[assertion["command"][0], "DECLARATION=/tmp/labels.json", *assertion["command"][1:]])
        capture = {
            "action_id": "capture-official", "operation": "VC-1:capture-official", "timeout_seconds": 120,
            "item_ids": ["official-test"],
            "command": [sys.executable, str(Path(upgrade.__file__).resolve()), "capture-official", "run",
                        "--campaign-dir", str(campaign), "--acknowledge-live-requests"],
        }
        resume = {
            "action_id": "preview-official-recovery", "operation": "VC-1:official-recovery", "timeout_seconds": 120,
            "item_ids": ["official-test"],
            "command": [sys.executable, str(Path(upgrade.__file__).resolve()), "resume", "--campaign-dir",
                        str(campaign), "--rerun-failed", "--preview-recovery"],
        }
        original_reason = "VC-1 已发布预约只能走 attempt 恢复；无已知幂等动作合同"
        cases = {
            # 采集与恢复链动作不在合同内：维持原结论（已发布预约只能走 attempt 恢复）。
            original_reason: self._manifest(fixture, [capture]),
            f"{original_reason} ": self._manifest(fixture, [resume]),
            f"{original_reason}  ": self._manifest(fixture, [preview_action], execute=[self.PREVIEW, "official-test"]),
            "reuse_items 必须为空": self._manifest(fixture, [preview_action], reuse=["official-test"]),
            "与命令形态": self._manifest(fixture, [renamed]),
            "同一个官方 attempt": self._manifest(fixture, [assertion, other_attempt]),
            "解释器不是当前受管 Python": self._manifest(fixture, [unmanaged]),
            "不是受管 codex_upgrade 的直接调用": self._manifest(fixture, [foreign_module]),
            "参数没有精确闭合": self._manifest(fixture, [self._seal_action(campaign, attempt_id, extra=("--evidence-root", str(campaign)))]),
            "参数没有精确闭合 ": self._manifest(fixture, [self._seal_action(campaign, attempt_id, extra=("--acknowledge-live-requests",))]),
            "SIDE=official": self._manifest(fixture, [candidate_side, preview_action]),
            "坐标没有精确闭合": self._manifest(fixture, [declared, preview_action]),
            "不是当前受管树": self._manifest(fixture, [foreign_script, preview_action]),
        }
        for reason, manifest in cases.items():
            with self.subTest(reason=reason.strip()):
                result = facts(campaign, manifest)
                self.assertFalse(result["allowed"], result)
                self.assertEqual(result["actions"], [])
                self.assertIn(reason.strip(), " ".join(result["reasons"]))

        # 半成品与封存状态：断言包已发布（脚本 write-once 拒绝覆盖）、暂存残留、官方阶段结果或 VC-1 checkpoint 已存在。
        evidence = attempt_root / "evidence"
        stage_result = campaign / "official" / "result.json"
        checkpoint = upgrade._vc_checkpoint_path(campaign, "VC-1")
        states = (
            ("断言证据包已发布", evidence / "assertion-bundle", lambda p: p.mkdir(mode=0o700), lambda p: p.rmdir()),
            ("暂存残留", evidence / ".assertion-work.AbC123", lambda p: p.mkdir(mode=0o700), lambda p: p.rmdir()),
            ("已封存", stage_result, lambda p: p.write_text("{}\n", encoding="utf-8"), lambda p: p.unlink()),
            ("已封存 ", checkpoint, lambda p: p.write_text("{}\n", encoding="utf-8"), lambda p: p.unlink()),
        )
        for reason, path, make, remove in states:
            with self.subTest(state=reason.strip()):
                make(path)
                try:
                    result = facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id)))
                    self.assertFalse(result["allowed"], result)
                    self.assertIn(reason.strip(), " ".join(result["reasons"]))
                finally:
                    remove(path)
        # 断言包已发布只挡断言包动作：单独的 seal 批准批次不受影响。
        (evidence / "assertion-bundle").mkdir(mode=0o700)
        draft.write_text("{}\n", encoding="utf-8")
        preview.write_text(json.dumps({"review_sha256": review}) + "\n", encoding="utf-8")
        self.assertTrue(facts(campaign, approve)["allowed"])
        for path in (draft, preview):
            path.unlink()
        (evidence / "assertion-bundle").rmdir()

        # attempt 已被工具演进作废作业、环境隔离作废或证据根冲突隔离：永不 seal，改走 attempt 恢复链。
        key = ("official", None, attempt_id)
        for reason, patcher in (
            ("工具演进作废", mock.patch.object(upgrade, "_attempt_evolution_invalidated_job_ids", return_value=["official-test"])),
            ("环境隔离作废", mock.patch.object(upgrade, "_isolation_invalidated_attempts", return_value={key})),
            ("证据根冲突隔离", mock.patch.object(upgrade, "_conflict_quarantined_attempts", return_value={key})),
        ):
            with self.subTest(invalidated=reason), patcher:
                result = facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id)))
                self.assertFalse(result["allowed"], result)
                self.assertIn(reason, " ".join(result["reasons"]))
        self.assertTrue(facts(campaign, self._manifest(fixture, self._preview_batch(campaign, attempt_id)))["allowed"])

    def test_permanent_failure_class_still_stops_the_line(self) -> None:
        """永久失败类照旧拒绝：VC-1 seal 链批次以永久类失败时收账直接停线，不进阶段审核，合同无从生效。"""

        for failure_class in ("evidence-integrity", "identity-drift"):
            with self.subTest(failure_class=failure_class):
                root = self.base / f"permanent-{failure_class}"
                root.mkdir(mode=0o700)
                fixture = self._fixture(root)
                ledger_dir = fixture["timing_ledger"]
                timing.append_event(ledger_dir, event_id="d10-vc-0-completed", phase="VC-0", event_type="stage_completed",
                                    next_action="启动 VC-1")
                timing.append_event(ledger_dir, event_id="d10-vc-1-started", phase="VC-1", event_type="stage_started",
                                    next_action="运行父批次")
                inner = self.case._b0_vc1_seal_batch_manifest(
                    fixture, fixture["attempt_root"].name,
                    actions=self._preview_batch(fixture["campaign_dir"], fixture["attempt_root"].name),
                )
                closeout = supervisor._close_failed_campaign_timing_ledger(
                    fixture["campaign_dir"], inner, failed_action_id=self.PREVIEW, failure_class=failure_class
                )
                self.assertEqual(closeout["ledger_status"], "stopped", closeout)
                self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stopped")


# 第 39 项：owner 在失败收账前（或收账中途）丢失的父进程替身。独立子进程按夹具同一口径（离线出口、合成证据标签
# 声明）经原子入口真实派发；动作失败、诊断与 action-failed 生命周期事件都已落盘后 SIGKILL 自身（OOM／被杀的真实
# 时点），由独立会话里的 monitor 按 R2 确定性封存。杀点：``closeout`` 在失败收账入口；``before-review`` 在收账已写
# stage_abandoned、正要写 stage_review_required 时。
_OWNER_LOST_AT_CLOSEOUT = r'''
import argparse, json, os, signal, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as rehearsal
from tools.official_client_capture.tests import runtime_egress_fixtures

original = rehearsal._target_evidence_label_declaration_sha256
original_append = supervisor.timing_ledger.append_event


def declaration(target_version, target_scenario, **kwargs):
    try:
        return original(target_version, target_scenario, **kwargs)
    except rehearsal.JobRehearsalReceiptError:
        return "d" * 64


def owner_lost(*_args, **_kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


def append_until_review(*args, **kwargs):
    if kwargs.get("event_type") == "stage_review_required":
        owner_lost()
    return original_append(*args, **kwargs)


values = json.loads(sys.argv[2])
paths = {"campaign_dir", "state_dir", "predecessor_checkpoint", "action_plan"}
namespace = argparse.Namespace(**{key: Path(value) if key in paths else value for key, value in values.items()})
kill_point = (
    mock.patch.object(supervisor, "_close_failed_campaign_timing_ledger", side_effect=owner_lost)
    if sys.argv[3] == "closeout"
    else mock.patch.object(supervisor.timing_ledger, "append_event", side_effect=append_until_review)
)
with runtime_egress_fixtures.offline_campaign_egress(), \
        mock.patch.object(rehearsal, "_target_evidence_label_declaration_sha256", side_effect=declaration), \
        kill_point:
    upgrade.compile_and_run_vc_batch(namespace)
'''


class OrphanedStageFailureCloseoutTests(unittest.TestCase):
    """第 39 项：VC-1～VC-3 的阶段审核类动作失败后 owner 在失败收账前丢失，monitor 按 R2 确定性封存为
    ``failed／action-failed:<id>``——monitor 按设计不写 Campaign 账本，账本停在 active。修复前对账只按 active 写
    receipt_passed 并指向"重新派发同一批次"，阶段幂等重派证明只在账本处于阶段审核态时才写，后继协议全部拒绝。
    修复后对账先以父监督器同一收账函数补齐 stage_abandoned＋stage_review_required，再按既有阶段审核对账。
    """

    def setUp(self) -> None:
        self.case = upgrade_tests.CodexUpgradeTest()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    @staticmethod
    def _plan(root: Path, name: str, action: dict) -> Path:
        path = root / "action-plans" / f"{name}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": artifacts.VC_ACTION_PLAN_SCHEMA, "execute_item_ids": list(action["item_ids"]),
            "reuse_item_ids": [], "actions": [action],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path.resolve(strict=True)

    def _classify_plan(self, root: Path, campaign: Path) -> Path:
        """受管 CLI 的真实 classify 动作（阶段幂等合同可复核的形态）；夹具 Campaign 的官方证据是合成的，它会失败。"""

        return self._plan(root, "classify", {
            "action_id": "classify-draft", "operation": "VC-2:classify-draft", "timeout_seconds": 120,
            "item_ids": ["classify-draft"],
            "command": [sys.executable, str(Path(upgrade.__file__).resolve()), "classify", "--campaign-dir", str(campaign)],
        })

    def _declared_failure_plan(self, root: Path, failure_class: str) -> Path:
        """合成动作：以给定失败类写动作诊断后非零退出（诊断类别由子进程声明，与真实工具异常的 failure_class 同路）。"""

        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from tools.official_client_capture import codex_upgrade_supervisor as supervisor\n"
            "class StageFailure(RuntimeError):\n"
            "    failure_class = sys.argv[2]\n"
            "supervisor.write_campaign_run_action_diagnostic(\n"
            "    failure_kind='handled-error', error=StageFailure('合成阶段失败'), failure_class=sys.argv[2])\n"
            "sys.exit(1)\n"
        )
        return self._plan(root, f"declared-{failure_class}", {
            "action_id": "vc-2-declared", "operation": "VC-2:declared-failure", "timeout_seconds": 120,
            "item_ids": ["vc-2-declared"],
            "command": [sys.executable, "-c", script, str(Path(upgrade.__file__).resolve().parents[2]), failure_class],
        })

    def _dispatch_owner_lost_at_closeout(self, fixture: dict, sequence: int, plan: Path, *,
                                         kill_point: str = "closeout") -> Path:
        """独立子进程经原子入口派发 VC-2 批次，owner 在失败收账入口（或收账中途）被 SIGKILL；等 monitor 封存终态后
        返回父 run。"""

        arguments = upgrade_tests.CodexUpgradeTest._vc_chain_arguments(fixture, "VC-2", sequence, plan)
        values = {key: str(value) if isinstance(value, Path) else value for key, value in vars(arguments).items()}
        repo_root = Path(upgrade.__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-c", _OWNER_LOST_AT_CLOSEOUT, str(repo_root), json.dumps(values), kill_point],
            cwd=repo_root, capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(completed.returncode, -signal.SIGKILL, completed.stdout[-2000:] + completed.stderr[-2000:])
        runs = [path for path in fixture["state_dir"].glob("run-*")
                if supervisor._read_state(path).get("phase") == "VC-2"]
        self.assertEqual(len(runs), 1, runs)
        run_dir = runs[0]
        deadline = time.monotonic() + 60.0
        while supervisor._read_state(run_dir)["state"] not in supervisor.TERMINAL_STATES:
            self.assertLess(time.monotonic(), deadline, "monitor 未在 60 秒内封存 owner 丢失的父 run")
            time.sleep(0.2)
        return run_dir

    @staticmethod
    def _owner_check_sealed(run_dir: Path) -> bool:
        return any(event.get("event_type") == "failed" and event.get("operation") == "supervisor:owner-check"
                   for event in supervisor.load_events(run_dir))

    def test_r2_sealed_stage_failure_backfills_review_then_single_protocol_redispatch(self) -> None:
        """真实 classify 动作失败、owner 在收账前丢失 → monitor R2 封存、账本停在 active → 对账补齐阶段审核收账并按
        阶段幂等合同写证明、重开 VC-2 → 有且只有阶段审核协议承接 N+1 重派 → N+1 被原子入口接纳并真实执行；
        重复对账不再补账、不重复写事件；全程零请求。"""

        root = self.base / "r2-classify"
        root.mkdir(mode=0o700)
        fixture = self.case._vc_chain_fixture(root)
        campaign, ledger_dir = fixture["campaign_dir"], fixture["timing_ledger"]
        plan = self._classify_plan(root, campaign)
        run_dir = self._dispatch_owner_lost_at_closeout(fixture, 2, plan)
        self.assertEqual(supervisor._read_state(run_dir)["state"], "failed")
        self.assertEqual(supervisor.read_stop_receipt(run_dir)["reason"], "action-failed:classify-draft")
        self.assertTrue(self._owner_check_sealed(run_dir))
        diagnostic = json.loads((run_dir / "action-diagnostics" / "action-classify-draft-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["failure_class"], "execution-failure")
        before = timing.inspect_ledger(ledger_dir)
        self.assertEqual((before["status"], before["active_phase"]), ("active", "VC-2"))

        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertIn("ledger_closeout_backfill", result, result)
        self.assertEqual(result["ledger_closeout_backfill"], {
            "action_id": "classify-draft", "failure_class": "execution-failure",
            "ledger_status": "stage_review_required", "idempotent": False,
        })
        self.assertEqual(result["status"], "recoverable", result)
        proof = result["stage_replay"]
        self.assertTrue(proof["allowed"], proof)
        self.assertEqual((proof["phase"], proof["next_action"]), ("VC-2", "redispatch-same-batch"))
        self.assertTrue((campaign / "control" / "reconciliation" / f"run-{run_dir.name}" / "stage-replay.json").is_file())
        events = [event for event, _ in timing._load_events(ledger_dir)]
        self.assertEqual([event["event_type"] for event in events[len(events) - 3:]],
                         ["stage_abandoned", "stage_review_required", "receipt_passed"])
        self.assertEqual(sorted(item["role"] for item in events[-1]["receipts"]),
                         ["provenance", "reconciliation", "stage_replay"])
        state = timing.inspect_ledger(ledger_dir)
        self.assertEqual((state["status"], state["active_phase"], state["next_action"]),
                         ("active", "VC-2", "redispatch-same-batch"))
        head = state["head_sequence"]
        again = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertNotIn("ledger_closeout_backfill", again)
        self.assertEqual(again["stage_replay"], proof)
        self.assertEqual(timing.inspect_ledger(ledger_dir)["head_sequence"], head)

        inner = supervisor._read_json(run_dir / "campaign-run-manifest.json")["manifest"]
        prior_state = supervisor._read_state(run_dir)
        successor = json.loads(json.dumps(inner))
        successor.update(batch_id="vc-2-0003", batch_sequence=3, batch_sha256="3" * 64)
        accepted = [name for name, protocol in supervisor._SUCCESSOR_PROTOCOLS
                    if protocol(prior_state, inner, run_dir, successor, campaign_dir=campaign)]
        self.assertEqual(accepted, ["stage_review"])
        again_failed, code = upgrade.compile_and_run_vc_batch(
            upgrade_tests.CodexUpgradeTest._vc_chain_arguments(fixture, "VC-2", 3, plan)
        )
        self.assertEqual((code, again_failed["campaign_run"]["reason"]), (1, "action-failed:classify-draft"), again_failed)
        self.assertTrue((campaign / "control" / "vc" / "commits" / "0003-vc-2.json").is_file())
        self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stage_review_required")
        totals = project.replay_head(fixture["ledger"])
        self.assertEqual((totals["precise_total"], totals["estimated_total"]), (0, 0))

    def test_r2_sealed_after_stage_abandoned_completes_review_once(self) -> None:
        """owner 在收账中途（已写 stage_abandoned、正要写 stage_review_required）丢失：账本停在"已放弃、未审核"，修复前
        对账连 receipt_passed 都不写。对账以同一收账函数续作补齐审核，stage_abandoned 与 stage_review_required
        各只一条，随后按阶段幂等合同写证明并重开阶段。"""

        root = self.base / "r2-mid-closeout"
        root.mkdir(mode=0o700)
        fixture = self.case._vc_chain_fixture(root)
        campaign, ledger_dir = fixture["campaign_dir"], fixture["timing_ledger"]
        run_dir = self._dispatch_owner_lost_at_closeout(
            fixture, 2, self._classify_plan(root, campaign), kill_point="before-review"
        )
        self.assertTrue(self._owner_check_sealed(run_dir))
        events = [event for event, _ in timing._load_events(ledger_dir)]
        self.assertEqual(events[-1]["event_type"], "stage_abandoned")
        self.assertIsNone(timing.inspect_ledger(ledger_dir)["active_phase"])
        result = reconciler.reconcile_supervisor_run(run_dir, campaign)
        self.assertIn("ledger_closeout_backfill", result, result)
        self.assertEqual(result["ledger_closeout_backfill"]["ledger_status"], "stage_review_required")
        self.assertTrue(result["stage_replay"]["allowed"], result)
        events = [event for event, _ in timing._load_events(ledger_dir)]
        self.assertEqual(sum(event["event_type"] == "stage_abandoned" for event in events), 1)
        self.assertEqual(sum(event["event_type"] == "stage_review_required" for event in events), 1)
        state = timing.inspect_ledger(ledger_dir)
        self.assertEqual((state["status"], state["active_phase"], state["next_action"]),
                         ("active", "VC-2", "redispatch-same-batch"))

    def test_r2_sealed_recoverable_and_permanent_failures_keep_existing_paths(self) -> None:
        """可恢复类（environment-prerequisite）与永久类（evidence-integrity）的 R2 封存不补账、行为不变：前者对账后
        由环境／后处理重派协议唯一承接，后者照旧永久停线且没有任何后继协议。"""

        for failure_class in ("environment-prerequisite", "evidence-integrity"):
            with self.subTest(failure_class=failure_class):
                root = self.base / f"r2-{failure_class}"
                root.mkdir(mode=0o700)
                fixture = self.case._vc_chain_fixture(root)
                campaign, ledger_dir = fixture["campaign_dir"], fixture["timing_ledger"]
                run_dir = self._dispatch_owner_lost_at_closeout(fixture, 2, self._declared_failure_plan(root, failure_class))
                self.assertEqual(supervisor.read_stop_receipt(run_dir)["reason"], "action-failed:vc-2-declared")
                self.assertTrue(self._owner_check_sealed(run_dir))
                result = reconciler.reconcile_supervisor_run(run_dir, campaign)
                self.assertNotIn("ledger_closeout_backfill", result)
                self.assertIsNone(result.get("stage_replay"))
                inner = supervisor._read_json(run_dir / "campaign-run-manifest.json")["manifest"]
                prior_state = supervisor._read_state(run_dir)
                successor = json.loads(json.dumps(inner))
                successor.update(batch_id="vc-2-0003", batch_sequence=3, batch_sha256="3" * 64)
                accepted = []
                for name, protocol in supervisor._SUCCESSOR_PROTOCOLS:
                    try:
                        if protocol(prior_state, inner, run_dir, successor, campaign_dir=campaign):
                            accepted.append(name)
                    except supervisor.SupervisorError:
                        accepted.append(f"{name}:拒绝")
                if failure_class == "environment-prerequisite":
                    self.assertEqual(result["status"], "recoverable", result)
                    self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "active")
                    self.assertEqual(accepted, ["environment_redispatch"])
                else:
                    self.assertEqual(result["status"], "permanent_stop", result)
                    self.assertEqual(timing.inspect_ledger(ledger_dir)["status"], "stopped")
                    self.assertFalse([name for name in accepted if ":" not in name], accepted)


if __name__ == "__main__":
    unittest.main()
