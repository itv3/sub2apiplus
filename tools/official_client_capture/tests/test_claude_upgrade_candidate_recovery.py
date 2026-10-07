"""Claude 候选续跑、候选状态与工具演进的候选作业影响（Claude 升级控制面设计 B4-01 c1）。

沿用候选 run 的离线夹具（``test_claude_upgrade_candidate_capture.CandidateFixture``：宿主脚本替身、候选中继替身、网关事实
与激活事实替身、正式入口门替身），另加：

* 「官方已封存」：候选只在 VC-1 封存之后出现；夹具把 ``_official_sealed`` 换成真，官方阶段结果读成一份没有 attempt 的
  封存结果（官方 attempt 列表本来就是空的）；
* 已批准的恢复预览：共享对账器写预览与批准、消费授权推进计时账本（Codex 同一实现，官方续跑链已有真实链验收）；这里把
  ``_consume_recovery_approval`` 换成返回同一形状的已消费授权，专注候选续跑本身（复用、执行、网关连续性与 attempt 合同）。

覆盖：候选状态（无 attempt、等待客户端收据、等待 seal 批准、失败、选择候选、官方未封存即不一致）；resume 的候选分支
（拒绝 Codex 身份参数、只读续跑闭集、只重跑失败作业并只读承接已完成作业、网关连续性漂移零请求失败）；R17 候选复算；
对账时的网关残留识别；激活事实的 docker 边界；工具演进对候选取证作业的影响。
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_evolution as evolution
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests

CANDIDATE = candidate_tests.CANDIDATE
LIVE_JOB = candidate_tests.LIVE_JOB
REFRESH_JOB = candidate_tests.REFRESH_JOB
REFRESH_PROBE = "v4-oauth-refresh"


class CandidateRecoveryFixture(candidate_tests.CandidateFixture):
    """候选 run 夹具 + 官方已封存 + 已消费的恢复授权。"""

    def setUp(self) -> None:
        super().setUp()
        self.consumed: dict[str, Any] | None = None

    @contextlib.contextmanager
    def recovery_patched(self):
        original_stage = claude_upgrade._load_stage_result

        def stage(campaign_dir, name, candidate_id=None, **kwargs):
            if name == "capture-official":
                return {"attempt": {"path": "official/attempts/sealed-elsewhere/attempt.json"}}
            return original_stage(campaign_dir, name, candidate_id, **kwargs)

        def consume(campaign_dir, preview_path, *, phase="official", candidate_id=None):
            assert (phase, candidate_id) == ("candidate", CANDIDATE)
            assert self.consumed is not None and Path(preview_path) == Path(self.consumed["preview_path"])
            return dict(self.consumed)

        with contextlib.ExitStack() as stack:
            stack.enter_context(self.candidate_patched())
            stack.enter_context(mock.patch.object(capture, "_official_sealed", lambda _campaign: True))
            stack.enter_context(mock.patch.object(claude_upgrade, "_load_stage_result", stage))
            stack.enter_context(mock.patch.object(recovery, "_consume_recovery_approval", consume))
            yield

    def failed_attempt(self, campaign: Path) -> dict[str, Any]:
        """首个候选 run：刷新 Job 的唯一步骤在只读前检被拒（没有改动、不是污染），其余 Job 完成。"""

        self.set_candidate_control({"precheck_fail": [REFRESH_PROBE]})
        with mock.patch.object(claude_upgrade, "JOB_RETRY_LIMIT", 0):
            _code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(result.get("status"), "failed", stderr)
        self.set_candidate_control({})
        return result

    def approve(self, campaign: Path, source_attempt_id: str, plan: dict[str, Any]) -> Path:
        preview = campaign / "control" / "reconciliation" / f"attempt-{source_attempt_id}" / "recovery-preview-01.json"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(json.dumps({"fixture": True}), encoding="utf-8")
        self.consumed = {
            "preview_path": str(preview),
            "source_attempt_id": source_attempt_id,
            "reuse_job_ids": list(plan["reuse_job_ids"]),
            "execute_job_ids": list(plan["execute_job_ids"]),
            "approval": {"index": 1, "receipt_sha256": "1" * 64},
            "timing_recovery_event": None,
        }
        return preview

    def plan(self, campaign: Path, source_attempt_id: str) -> dict[str, Any]:
        with self.recovery_patched():
            return recovery.recovery_plan(
                campaign, claude_upgrade.load_campaign_manifest(campaign), source_attempt_id=source_attempt_id,
                phase="candidate", candidate_id=CANDIDATE,
            )

    def status(self, campaign: Path, candidate_id: str | None = CANDIDATE) -> dict[str, Any]:
        with self.recovery_patched():
            return claude_upgrade.campaign_status(campaign, candidate_id)

    def resume(self, campaign: Path, *extra: str) -> tuple[int, dict[str, Any] | None, str]:
        with self.recovery_patched():
            return self.run_main(["resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, *extra])


class CandidateStatusTests(CandidateRecoveryFixture):
    def test_status_follows_candidate_capture(self) -> None:
        campaign = self.write_candidate_campaign()
        (campaign / "candidates" / CANDIDATE).mkdir(parents=True)
        status = self.status(campaign)
        self.assertEqual((status["status"], status["candidate_id"]), ("profile_approved", CANDIDATE))
        self.assertIn("record-candidate-build", status["next_command"])
        receipt = claude_upgrade._candidate_build_receipt_path(campaign, CANDIDATE)
        receipt.write_text("{}", encoding="utf-8")
        self.assertTrue(self.status(campaign)["next_command"].startswith("capture-candidate run"))
        # 只有一个活跃候选时不带 --candidate-id 也给出它的状态；两个时要求选择。
        self.assertEqual(self.status(campaign, None)["candidate_id"], CANDIDATE)
        (campaign / "candidates" / "claude-cand-other").mkdir()
        self.assertEqual(self.status(campaign, None)["status"], "candidate_selection_required")
        (campaign / "candidates" / "claude-cand-other").rmdir()
        # 官方证据尚未封存却已有候选：不一致（只能人工审计）。
        with self.candidate_patched():
            self.assertEqual(claude_upgrade.campaign_status(campaign, CANDIDATE)["status"], "capture_state_inconsistent")
        with self.assertRaisesRegex(ConfigurationError, "没有候选 nope"):
            self.status(campaign, "nope")

        _code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(result.get("status"), "awaiting_receipts", stderr)
        status = self.status(campaign)
        self.assertEqual(status["status"], "candidate_awaiting_client_checkpoint")
        self.assertIn("client-evidence", status["next_command"])
        self.assertEqual(status["active_unsealed_attempts"]["candidate"], [f"{CANDIDATE}:{result['attempt_id']}"])
        preview = campaign / "candidates" / CANDIDATE / "seal" / "g1" / "seal-preview.json"
        preview.parent.mkdir(parents=True)
        preview.write_text(json.dumps({"attempt_id": result["attempt_id"]}), encoding="utf-8")
        self.assertEqual(self.status(campaign)["status"], "candidate_awaiting_seal_approval")

    def test_failed_candidate_attempt_status(self) -> None:
        campaign = self.write_candidate_campaign()
        first = self.failed_attempt(campaign)
        status = self.status(campaign)
        self.assertEqual(status["status"], "candidate_capture_failed")
        self.assertEqual(status["candidate_attempt"]["attempt_id"], first["attempt_id"])
        self.assertEqual(status["failed_attempts"]["candidate"], [f"{CANDIDATE}:{first['attempt_id']}"])
        self.assertIn("--rerun-failed", status["next_command"])


class CandidateResumeTests(CandidateRecoveryFixture):
    def test_codex_identity_flags_are_refused(self) -> None:
        campaign = self.write_candidate_campaign()
        code, _result, stderr = self.resume(campaign, "--build-id", "b1", "--acknowledge-live-requests")
        self.assertEqual(code, 1)
        self.assertIn("只来自 VC-4 构建收据", stderr)
        code, _result, stderr = self.run_main(["resume", "--campaign-dir", str(campaign), "--preview-recovery"])
        self.assertEqual(code, 1)
        self.assertIn("只用于候选续跑", stderr)

    def test_rerun_failed_reuses_completed_and_reruns_failed_only(self) -> None:
        campaign = self.write_candidate_campaign()
        first = self.failed_attempt(campaign)
        calls_after_first = len(self.candidate_calls())

        # 只读续跑闭集：零写入、零请求。
        attempts_root = campaign / "candidates" / CANDIDATE / "attempts"
        before = sorted(path.name for path in attempts_root.iterdir())
        code, preview, stderr = self.resume(campaign, "--preview-recovery")
        self.assertEqual(code, 0, stderr)
        self.assertEqual((preview["schema_version"], preview["live_request_count"]), (recovery.RECOVERY_PLAN_PREVIEW_SCHEMA, 0))
        self.assertEqual((preview["reuse_job_ids"], preview["execute_job_ids"]), ([LIVE_JOB], [REFRESH_JOB]))
        self.assertEqual(sorted(path.name for path in attempts_root.iterdir()), before)

        plan = self.plan(campaign, first["attempt_id"])
        reused_source = plan["reuse_results"][LIVE_JOB]
        self.assertEqual(reused_source["disposition"], "reused")
        self.assertEqual(len(reused_source["restoration_checks"]), 4)
        preview_path = self.approve(campaign, first["attempt_id"], plan)
        # R17：候选复算与 resume 同一判定；预览集合不同即不一致。
        with self.recovery_patched():
            check = recovery.recovery_reuse_check(
                campaign, phase="candidate", candidate_id=CANDIDATE, source_attempt_id=first["attempt_id"],
                reuse_job_ids=[LIVE_JOB], execute_job_ids=[REFRESH_JOB],
            )
            self.assertEqual(check["status"], "consistent")
            with self.assertRaisesRegex(ConfigurationError, "不一致"):
                recovery.recovery_reuse_check(
                    campaign, phase="candidate", candidate_id=CANDIDATE, source_attempt_id=first["attempt_id"],
                    reuse_job_ids=[], execute_job_ids=[LIVE_JOB, REFRESH_JOB],
                )

        code, resumed, stderr = self.resume(campaign, "--rerun-failed", "--recovery-preview", str(preview_path), "--acknowledge-live-requests")
        self.assertIsNotNone(resumed, stderr)
        self.assertEqual((code, resumed["status"], resumed["candidate_id"]), (2, "awaiting_receipts", CANDIDATE), stderr)
        new_calls = self.candidate_calls()[calls_after_first:]
        self.assertEqual([call["probe"] for call in new_calls], [REFRESH_PROBE], "只应重跑失败 Job 的 probe")
        root, attempt = self.load_attempt(campaign, resumed["attempt_id"])
        self.assertEqual(root.parent, attempts_root)
        dispositions = {item["id"]: (item["status"], item["disposition"]) for item in attempt["results"]}
        self.assertEqual(dispositions, {LIVE_JOB: ("complete", "reused"), REFRESH_JOB: ("complete", "executed")})
        reused = next(item for item in attempt["results"] if item["id"] == LIVE_JOB)
        self.assertEqual(reused["reused_from"]["attempt_id"], first["attempt_id"])
        self.assertEqual(reused["restoration_checks"], reused_source["restoration_checks"])
        # 恢复来源收据沿用 v1 字段闭集；连续性另核网关；候选字段覆盖复用与新执行的全部步骤。
        source = recovery.load_recovery_source(campaign, root)
        self.assertEqual((source["source_attempt_id"], source["execute_job_ids"]), (first["attempt_id"], [REFRESH_JOB]))
        self.assertEqual(set(source), recovery.RECOVERY_SOURCE_FIELDS)
        gateway_continuity = attempt["recovery"]["continuity"]["gateway"]
        self.assertEqual(gateway_continuity["compared_fields"], list(candidate_capture.GATEWAY_COMPARED_FIELDS))
        restorations = [entry for entry in attempt["candidate_gateway"]["recovery_verification"] if entry["id"].startswith("gateway-restoration-")]
        self.assertEqual(len(restorations), 5)
        self.assertTrue(all(entry["facts"]["result"] == "pass" for entry in restorations))
        self.assertIsNotNone(attempt["gateway_environment"]["activation_fact"])
        self.assertEqual(attempt["environment"]["restoration_report"], attempt["gateway_recovery"]["file"])
        reservation = json.loads((root / "reservation.json").read_text(encoding="utf-8"))
        self.assertEqual((reservation["phase"], reservation["candidate_id"]), ("candidate", CANDIDATE))
        # 续跑之后：新 attempt 等待客户端收据；来源已经续跑过，再用旧来源续跑被拒。
        self.assertEqual(self.status(campaign)["status"], "candidate_awaiting_client_checkpoint")
        with self.recovery_patched():
            with self.assertRaisesRegex(ConfigurationError, f"续跑来源必须是最新的候选 {CANDIDATE} 的 attempt"):
                recovery.require_current_source(
                    campaign, claude_upgrade.load_campaign_manifest(campaign), first["attempt_id"], phase="candidate", candidate_id=CANDIDATE
                )
            # B4-02 证据侧按同一读法读续跑 attempt：复用 Job 的恢复报告仍在原证据根、摘要不变。
            from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal

            manifest = claude_upgrade.load_campaign_manifest(campaign)
            layout = capture._capture_layout(campaign, manifest)
            jobs = {job.job_id: job for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}
            candidate_seal._attempt_candidate_context(attempt)
            for row in attempt["results"]:
                candidate_seal._verify_restoration_checks(jobs[row["id"]], row, layout, capture._host_path(row["evidence_roots"][0], layout))

    def test_gateway_continuity_drift_fails_before_any_job(self) -> None:
        campaign = self.write_candidate_campaign()
        first = self.failed_attempt(campaign)
        preview_path = self.approve(campaign, first["attempt_id"], self.plan(campaign, first["attempt_id"]))
        calls_after_first = len(self.candidate_calls())
        drifted = json.loads(json.dumps(candidate_tests.BASE_GATEWAY))
        drifted["service"]["hosts_sha256"] = "e" * 64
        self.gateway_file.write_text(json.dumps(drifted), encoding="utf-8")
        code, resumed, stderr = self.resume(campaign, "--rerun-failed", "--recovery-preview", str(preview_path), "--acknowledge-live-requests")
        self.assertIsNotNone(resumed, stderr)
        self.assertEqual((code, resumed["status"]), (2, "failed"), stderr)
        self.assertEqual(resumed["execution_error"]["type"], recovery.CONTINUITY_DRIFT_ERROR_TYPE)
        self.assertIn("service.hosts_sha256", resumed["execution_error"]["message"])
        self.assertEqual(len(self.candidate_calls()), calls_after_first, "连续性漂移必须在任何 Job 之前失败（零请求）")
        _root, attempt = self.load_attempt(campaign, resumed["attempt_id"])
        self.assertEqual(attempt["results"], [])
        self.assertTrue(claude_upgrade._attempt_continuity_drifted(attempt))


class GatewayResidueTests(CandidateRecoveryFixture):
    def test_reconcile_identifies_gateway_residue_read_only(self) -> None:
        campaign = self.write_candidate_campaign()
        first = self.failed_attempt(campaign)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.recovery_patched():
            clean = recovery.identify_gateway_residue(campaign, manifest, first["attempt_id"], CANDIDATE)
            self.assertEqual((clean["status"], clean["failures"]), ("clean", []))
            self.assertTrue((campaign / clean["snapshot"]["path"]).is_file())
            dirty = json.loads(json.dumps(candidate_tests.BASE_GATEWAY))
            dirty["residual_candidate_proxies"] = 1
            dirty["service"]["custom_ca_present"] = True
            self.gateway_file.write_text(json.dumps(dirty), encoding="utf-8")
            residual = recovery.identify_gateway_residue(campaign, manifest, first["attempt_id"], CANDIDATE)
        self.assertEqual((residual["status"], residual["failures"]), ("residual", ["custom-ca-present", "residual-candidate-proxy"]))
        self.assertEqual(recovery._attempt_scope(campaign, first["attempt_id"]), ("candidate", CANDIDATE))


class ActivationFactBoundaryTests(unittest.TestCase):
    """激活事实的 docker 边界：路径取自服务容器环境、容器内只读 cat；路径缺失或不安全即失败关闭。"""

    MANIFEST = {"configuration": dict(candidate_tests.CANDIDATE_CONFIGURATION)}

    def runner(self, env: str, content: str):
        commands: list[list[str]] = []

        def run(argv, *, operation, deadline=None, heartbeat=None, check=True, timeout_seconds=30):
            del operation, deadline, heartbeat, check, timeout_seconds
            commands.append(list(argv))
            if argv[:3] == ["docker", "inspect", "--format"]:
                return subprocess.CompletedProcess(argv, 0, env, "")
            if argv[:2] == ["docker", "exec"] and argv[3] == "cat":
                return subprocess.CompletedProcess(argv, 0, content, "")
            raise AssertionError(argv)

        return run, commands

    def test_read_and_reject(self) -> None:
        run, commands = self.runner(f"{candidate_capture.ACTIVATION_FACT_PATH_ENV}=/app/data/fact.json\nJWT_SECRET=x\n", "{}")
        with mock.patch.object(capture, "_run_probe_command", run):
            self.assertEqual(candidate_capture._read_activation_fact(self.MANIFEST), ("/app/data/fact.json", "{}"))
        self.assertEqual(commands[-1], ["docker", "exec", candidate_tests.SERVICE, "cat", "/app/data/fact.json"])
        for env in ("JWT_SECRET=x\n", f"{candidate_capture.ACTIVATION_FACT_PATH_ENV}=relative/fact.json\n"):
            run, _commands = self.runner(env, "{}")
            with self.subTest(env=env), mock.patch.object(capture, "_run_probe_command", run), self.assertRaises(ConfigurationError):
                candidate_capture._read_activation_fact(self.MANIFEST)


class EvolutionCandidateImpactTests(CandidateRecoveryFixture):
    """工具演进对候选取证作业的影响：已有 attempt 的活跃候选按 attempt 展开作业，与官方作业同一映射。"""

    def test_candidate_jobs_mapped_by_declared_dependencies(self) -> None:
        campaign = self.write_candidate_campaign()
        _code, result, stderr = self.run_candidate(campaign, "--acknowledge-live-requests")
        self.assertEqual(result.get("status"), "awaiting_receipts", stderr)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.recovery_patched():
            official, candidates, unmapped = evolution.evolution_job_impact(
                campaign, manifest, impact_paths=["official_client_capture/claude_run_candidate_capture.sh"], ignored_paths=[],
                wire_closure_changed=False, active_candidates=[CANDIDATE],
            )
            self.assertEqual((official["affected_job_ids"], unmapped), ([], []))
            self.assertEqual(candidates[CANDIDATE]["affected_job_ids"], sorted([LIVE_JOB, REFRESH_JOB]))
            self.assertFalse(candidates[CANDIDATE]["sealed"])
            # 编排器 wire 闭包变化：全部受影响；没有 attempt 的候选不记。
            _official, every, _unmapped = evolution.evolution_job_impact(
                campaign, manifest, impact_paths=[], ignored_paths=[], wire_closure_changed=True, active_candidates=[CANDIDATE, "idle"],
            )
            self.assertEqual(set(every), {CANDIDATE})
            self.assertEqual(every[CANDIDATE]["affected_job_ids"], sorted([LIVE_JOB, REFRESH_JOB]))
            # 已封存候选的作业受影响即拒绝登记。
            sealed = candidate_capture.legacy_stage_result_path(campaign, CANDIDATE)
            sealed.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "已封存的作业"):
                evolution.evolution_job_impact(
                    campaign, manifest, impact_paths=[], ignored_paths=[], wire_closure_changed=True, active_candidates=[CANDIDATE],
                )


if __name__ == "__main__":
    unittest.main()
