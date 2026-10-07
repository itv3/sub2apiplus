"""Claude VC-1 恢复链（Claude 升级控制面设计 B2-08）：全部零请求，取证与 seal 的真实代码路径照跑。

* 验收链 ``_exercise_reconcile_resume_failed_job``：首批采集有一个 Job 失败 → ``reconcile-attempt``（共享对账器入账判定、
  零请求恢复预览、R17 复算一致）→ 批准 → ``resume --rerun-failed`` 只重跑失败 Job（替身 relay 的调用记录只多出它的
  probe），其余作业只读复用；新 attempt 可进 seal 预览。链上把对账器里那些在 Codex 评估读侧闭包内、仍直接调用 Codex
  编排器的恢复段函数替换成一调用就失败，证明 VC-1 官方路径不经过它们。
* 其余：断网容器残留进程（对账识别 → 恢复批准下清理留证 → resume）、环境污染（清理 → 隔离 → 全部重跑）、被强杀的孤儿
  attempt（写到一半的证据被计量并取代归档）、无预约的父监督器 run 与 seal 链零请求失败（reconcile-supervisor-run）、
  批准延期之后再对账、工具演进（编排器链与 Store 两侧登记、作废作业只重跑它们；未登记时 run 被拒留下的停线租约不挡
  resume 与 seal；只改 control 层时不作废作业、seal 两层放行；Store Campaign 未在工具变化之前建立时演进登记零写入拒绝）、
  项目总账消费者门禁（只挡该根因）、同根因达上限只暂停 → campaign-resume 凭修复证据放行 → 再对账续跑、
  run 末权限收口失败（全部复用、零执行的 resume 重新收口，即不提供 harden-evidence-permissions 的依据）、R17 复算不一致
  拒绝批准与复用证据被改动时失败关闭、官方封存后的请求入账（幂等；只读复用封存不入账）、部署收据与有效 wire 身份经适配器、
  不提供的子命令。
* ``DeadlineExtensionReconcileTests._exercise_expired_deadline_pause_extend_reconcile`` 要真实等待预算到期（约一分钟），
  不进默认测试，供真实链外壳复用（暂停期间入口门禁拒绝 run 与 seal → 两层延期 → 再对账 → resume）。

``_exercise_*`` 方法自带全部夹具（``setUp`` 建临时数据根），可被真实链外壳在受管树副本里以
``python -m unittest <模块>.<类>._exercise_<名>`` 直接复用。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_evolution as evolution
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture import claude_upgrade_seal as seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture import codex_upgrade_wire_transition as wire_transition
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import claude_project_ledger_fixture as ledger_fixture
from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests
from tools.official_client_control import receipts as store_receipts

CAMPAIGN_ID = seal_tests.CAMPAIGN_ID
FAILING_JOB = "official-transport-baseline"
# 对账器里在 Codex 评估读侧闭包内、仍直接调用 Codex 编排器的恢复段函数（候选段路径）：VC-1 官方链一调用即失败。
SEGMENT_ONLY_RECONCILER_FUNCTIONS = (
    "recovery_job_inventory",
    "recovery_segment_summary",
    "_after_probe_bound_by_restoration",
    "segment_reuse_proofs",
    "reused_segment_job_result",
    "validate_segment_reuse_preview",
)

# B2-07 的完整替身 relay，加两件事：记下每次被调用的 probe；按失败表让某个 probe 前 N 次写完证据后以退出码 2 结束
# （「首批采集失败」：请求已经发出、证据完整，但该步骤失败）。
_RELAY_EPILOGUE = r'''
calls = os.environ.get("FAKE_RELAY_CALL_LOG")
if calls:
    with open(calls, "a", encoding="utf-8") as handle:
        handle.write(probe + "\n")
failures_path = os.environ.get("FAKE_RELAY_FAILURES")
failures = json.load(open(failures_path, encoding="utf-8")) if failures_path else {}
if probe in failures:
    counts_path = failures_path + ".counts"
    counts = json.load(open(counts_path, encoding="utf-8")) if os.path.exists(counts_path) else {}
    counts[probe] = counts.get(probe, 0) + 1
    json.dump(counts, open(counts_path, "w", encoding="utf-8"))
    if counts[probe] <= int(failures[probe]):
        print("injected relay failure", file=sys.stderr)
        sys.exit(2)
sys.exit(0)
'''
assert seal_tests.FULL_FAKE_RELAY.rstrip().endswith("sys.exit(0)")
RECOVERY_RELAY = seal_tests.FULL_FAKE_RELAY.rstrip()[: -len("sys.exit(0)")] + _RELAY_EPILOGUE


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class RecoveryChainCase(seal_tests._SealCampaignCase):
    """B2-06 取证夹具 + B2-07 seal 输入 + 对账所需的计时账本、Claude 项目总账、部署收据与冻结 Job 行，以及按 VC-0 收口形态
    预先建立的 Store Campaign。"""

    def setUp(self) -> None:
        super().setUp()
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(RECOVERY_RELAY, encoding="utf-8")
        relay.chmod(0o700)
        self.failures_file = self.root / "relay-failures.json"
        self.failures_file.write_text("{}", encoding="utf-8")
        self.call_log = self.root / "relay-call-log.txt"

    # ------------------------------------------------------------------ 夹具

    @contextlib.contextmanager
    def chain_patched(self):
        with self.seal_patched(), mock.patch.dict(
            os.environ, {"FAKE_RELAY_FAILURES": str(self.failures_file), "FAKE_RELAY_CALL_LOG": str(self.call_log)}
        ):
            yield

    @contextlib.contextmanager
    def segment_paths_forbidden(self):
        """对账器的候选恢复段函数一调用即失败：VC-1 官方链不得经过它们（它们在 Claude 作用域下仍直接调 Codex 编排器）。"""

        with contextlib.ExitStack() as stack:
            for name in SEGMENT_ONLY_RECONCILER_FUNCTIONS:
                stack.enter_context(
                    mock.patch.object(reconciler, name, side_effect=AssertionError(f"VC-1 官方链不应调用对账器 {name}"))
                )
            yield

    def fail_probe(self, probe: str, times: int) -> None:
        self.failures_file.write_text(json.dumps({probe: times}), encoding="utf-8")

    def relay_probe_calls(self) -> list[str]:
        if not self.call_log.exists():
            return []
        return [line for line in self.call_log.read_text(encoding="utf-8").splitlines() if line]

    def job_probes(self, job_id: str) -> list[str]:
        payload = capture_tests._reduced_payload({job_id})
        return [step["probe_id"] for step in payload["capture_jobs"][0]["steps"]]

    def build_recovery_campaign(self) -> Path:
        campaign = self.build_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        jobs = claude_upgrade._campaign_jobs(campaign, manifest, "official")
        rows = [
            {
                "id": job.job_id,
                "phase": "official",
                "execution_sha256": capture._job_execution_sha256(job),
                "evidence_roots": list(job.evidence_roots),
            }
            for job in jobs
        ]
        ledger = self.root / "timing-ledger"
        timing.create_ledger(
            ledger,
            upgrade_id=f"{CAMPAIGN_ID}-ledger",
            baseline_version=capture_tests.BASELINE,
            target_version=capture_tests.TARGET,
            campaign_purpose="validation_only",
            evidence_decision="recapture",
            client="claude",
        )
        for event_id, phase, event_type, action in (
            ("fixture-vc0-completed", "VC-0", "stage_completed", "VC-0 收口完成"),
            ("fixture-vc1-started", "VC-1", "stage_started", "VC-1 首批采集"),
        ):
            timing.append_event(ledger, event_id=event_id, phase=phase, event_type=event_type, next_action=action, receipts=[])
        seal_tests.rewrite_manifest(
            campaign,
            jobs=rows,
            control_receipts={
                "upgrade_timing": {"ledger_dir": str(ledger.resolve()), "ledger_plan_sha256": codex_upgrade.file_sha256(ledger / "ledger.json")}
            },
        )
        self.ledger_dir = ledger.resolve()
        # 生产 Campaign 的控制目录是私有的（总账写延期预览等要求 0700）；夹具里由 VC 产物替身顺手建出的目录收紧权限。
        for directory in (campaign / "control", campaign / "control" / "vc"):
            directory.chmod(0o700)
        self.project_root = ledger_fixture.install_claude_project_ledger(self.data)
        ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261007t000000z")
        # 生产中 Store Campaign 由 VC-0 收口建 Campaign 时一并建立（B2-11）：夹具在这里建，工具变化之后的演进才能登记到 Store
        # （Store 要求建 Campaign 时冻结身份等于现算值）。
        seal._ensure_store_campaign(campaign, claude_upgrade.load_campaign_manifest(campaign))
        return campaign

    # ------------------------------------------------------------------ 动作

    def capture_run(self, campaign: Path) -> dict[str, Any]:
        with self.chain_patched():
            code, result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual(code, 2, stderr)
        return result

    def reconcile(self, campaign: Path, attempt_id: str, *extra: str) -> dict[str, Any]:
        with self.chain_patched():
            code, result, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", attempt_id, *extra])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (0, "recoverable"), stderr)
        return result

    def resume(self, campaign: Path, preview: str) -> tuple[int, dict[str, Any] | None, str]:
        with self.chain_patched():
            return self.run_main(
                ["resume", "--campaign-dir", str(campaign), "--rerun-failed", "--recovery-preview", preview, "--acknowledge-live-requests"]
            )

    def approve(self, campaign: Path, attempt_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        approved = self.reconcile(campaign, attempt_id, "--approve-recovery-sha256", preview["review_sha256"])
        self.assertEqual(approved["recovery_approval"]["approved_sha256"], preview["review_sha256"])
        return approved

    def load_attempt(self, campaign: Path, attempt_id: str, *, replay: bool = True) -> tuple[Path, dict[str, Any]]:
        with self.chain_patched():
            return claude_upgrade._load_capture_attempt(campaign, "official", None, attempt_id, _replay_evidence_permissions=replay)


class ReconcileResumeChainTests(RecoveryChainCase):
    """验收链：首批采集失败 → 对账 → 预览 → resume 只重跑失败 Job。"""

    def test_first_batch_failure_reconcile_preview_resume_reruns_failed_job_only(self) -> None:
        self._exercise_reconcile_resume_failed_job()

    def _exercise_reconcile_resume_failed_job(self) -> None:
        campaign = self.build_recovery_campaign()
        failing_probes = self.job_probes(FAILING_JOB)
        # 第一个 probe 连续失败三次（同一 attempt 内的两次补跑也失败），Job 最终失败；其余 Job 全部完成。
        self.fail_probe(failing_probes[0], 3)
        with self.segment_paths_forbidden():
            first = self.capture_run(campaign)
            self.assertEqual(first["status"], "failed")
            statuses = {item["id"]: item["status"] for item in first["results"]}
            self.assertEqual(statuses.pop(FAILING_JOB), "failed")
            self.assertEqual(set(statuses.values()), {"complete"})
            calls_after_first = self.relay_probe_calls()

            # 对账：零请求、先入账后判定；恢复预览复用已完成作业、只执行失败作业；R17 复算一致；残留进程识别为干净。
            reconciled = self.reconcile(campaign, first["attempt_id"])
            preview = reconciled["recovery_preview"]
            self.assertEqual(preview["execute_job_ids"], [FAILING_JOB])
            self.assertEqual(preview["reuse_job_ids"], sorted(set(seal_tests.SEAL_JOBS) - {FAILING_JOB}))
            self.assertEqual((preview["live_request_count"], reconciled["live_request_count"]), (0, 0))
            self.assertEqual(reconciled["resume_reuse_check"]["status"], "consistent", reconciled["resume_reuse_check"])
            self.assertEqual(reconciled["residual_processes"]["status"], "clean")
            self.assertTrue((campaign / reconciled["residual_processes"]["probe_manifest"]["path"]).is_file())
            # 直接 CLI 派发（没有父监督器收账）时账本保持 VC-1 active；对账补登 attempt 的开始与失败事件。
            events = [event["event_id"] for event, _raw in timing._load_events(self.ledger_dir)]
            self.assertIn(f"reconcile-attempt-failed-{first['attempt_id']}", events)
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["active_phase"], "VC-1")
            self.assertEqual(self.relay_probe_calls(), calls_after_first)

            # 未批准的预览不能 resume；批准后 resume 只重跑失败 Job。
            code, _result, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
            self.assertEqual(code, 1)
            self.assertIn("尚未批准", stderr)
            self.approve(campaign, first["attempt_id"], preview)
            code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
            self.assertIsNotNone(resumed, stderr)
            self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        new_calls = self.relay_probe_calls()[len(calls_after_first):]
        self.assertEqual(new_calls, failing_probes, "resume 只应重跑失败 Job 的 probe，每个一次")
        dispositions = {item["id"]: (item["status"], item["disposition"]) for item in resumed["results"]}
        self.assertEqual(dispositions.pop(FAILING_JOB), ("complete", "executed"))
        self.assertEqual(set(dispositions.values()), {("complete", "reused")})
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")

        # 新 attempt：默认重放 run 末收口（含被复用的证据根）、恢复来源收据、复用结果的来源绑定与 checkpoint。
        attempt_root, attempt = self.load_attempt(campaign, resumed["attempt_id"])
        source = recovery.load_recovery_source(campaign, attempt_root)
        self.assertEqual(source["source_attempt_id"], first["attempt_id"])
        self.assertEqual(source["execute_job_ids"], [FAILING_JOB])
        self.assertEqual(attempt["recovery"]["source"]["sha256"], codex_upgrade.file_sha256(attempt_root / recovery.RECOVERY_SOURCE_FILENAME))
        for result in attempt["results"]:
            if result["id"] == FAILING_JOB:
                self.assertIsNotNone(result["job_closure"])
                continue
            self.assertIsNone(result["job_closure"])
            self.assertEqual(result["reused_from"]["attempt_id"], first["attempt_id"])
            self.assertEqual(result["reused_from"]["producer"]["attempt_id"], first["attempt_id"])
        records = incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False).records()
        self.assertEqual(sorted(record["disposition"] for record in records).count("reused"), len(seal_tests.SEAL_JOBS) - 1)
        with self.chain_patched():
            status = claude_upgrade.campaign_status(campaign)
        self.assertEqual(status["status"], "official_awaiting_receipts")
        self.assertEqual(status["official_attempt"]["recovery_source_attempt_id"], first["attempt_id"])
        # 来源 attempt 已被续跑：再拿旧预览 resume 或再对账来源都被拒绝（来源不是最新 attempt）。
        code, _result, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual(code, 1)
        self.assertIn("官方 attempt 正等待 seal", stderr)
        with self.chain_patched():
            with self.assertRaisesRegex(ConfigurationError, "续跑来源必须是最新的官方 attempt"):
                recovery.require_current_source(campaign, claude_upgrade.load_campaign_manifest(campaign), first["attempt_id"])

            # 新 attempt 可进 seal 预览：复用结果的 relay 清单摘要不变、证据视图覆盖全部 Job。
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, resumed["attempt_id"]))
        self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)



class _ProcessBoundaryMixin:
    """断网容器残留进程的离线替身：进程清单取自可被测试改写的环境文件，清理即把它清空（docker 边界替换为两个函数）。"""

    def inject_residual_processes(self, labels: list[str]) -> None:
        environment = _read(self.env_file)
        environment["processes"]["capture_processes"] = sorted(labels)
        self.env_file.write_text(json.dumps(environment), encoding="utf-8")

    @contextlib.contextmanager
    def fake_process_boundary(self):
        def listed(manifest):
            del manifest
            rows = []
            for index, label in enumerate(_read(self.env_file)["processes"]["capture_processes"]):
                role, _, name = label.partition(":")
                container = capture_tests.CAPTURE if role == "capture" else capture_tests.SYNTHETIC
                rows.append({
                    "role": role, "container": container, "container_id": capture_tests.CONTAINER_IDS[container],
                    "pid": 4000 + index, "start_ticks": 100 + index, "labels": [name],
                })
            return sorted(rows, key=lambda row: (row["role"], row["pid"]))

        def terminated(manifest, targets):
            del manifest
            environment = _read(self.env_file)
            environment["processes"]["capture_processes"] = []
            self.env_file.write_text(json.dumps(environment), encoding="utf-8")
            return [{**dict(target), "outcome": "terminated", "signals": ["TERM"]} for target in targets]

        with mock.patch.object(recovery, "list_residual_processes", side_effect=listed), mock.patch.object(
            recovery, "terminate_residual_processes", side_effect=terminated
        ):
            yield

    def cleanup(self, campaign: Path, *extra: str, action: str = "preview") -> tuple[int, dict[str, Any] | None, str]:
        with self.chain_patched(), self.fake_process_boundary():
            return self.run_main(["environment-cleanup", action, "--campaign-dir", str(campaign), "--reason", "强杀后断网容器残留", *extra])


class ResidualProcessRecoveryTests(_ProcessBoundaryMixin, RecoveryChainCase):
    """断网容器残留进程：对账识别并留证 → resume 在预约前拒绝 → 恢复批准下清理留证 → resume 只重跑失败 Job。"""

    def test_residual_processes_identified_cleaned_under_approval_then_resumed(self) -> None:
        self._exercise_residual_cleanup_then_resume()

    def _exercise_residual_cleanup_then_resume(self) -> None:
        campaign = self.build_recovery_campaign()
        failing_probes = self.job_probes(FAILING_JOB)
        self.fail_probe(failing_probes[0], 3)
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "failed")
        self.inject_residual_processes(["synthetic:claude", "synthetic:relay"])
        reconciled = self.reconcile(campaign, first["attempt_id"])
        residual = reconciled["residual_processes"]
        self.assertEqual((residual["status"], residual["capture_processes"]), ("residual", ["synthetic:claude", "synthetic:relay"]))
        self.assertIn("environment-cleanup", reconciled["next_command"])
        probe_manifest = _read(campaign / residual["probe_manifest"]["path"])
        self.assertEqual(probe_manifest["capture_processes"], ["synthetic:claude", "synthetic:relay"])
        self.approve(campaign, first["attempt_id"], reconciled["recovery_preview"])
        preview_path = reconciled["recovery_preview_path"]

        # resume 在预约之前拒绝：不建 attempt、不消费恢复授权、不发请求。
        head_before = timing.inspect_ledger(self.ledger_dir)["head_sha256"]
        calls_before = self.relay_probe_calls()
        code, _result, stderr = self.resume(campaign, preview_path)
        self.assertEqual(code, 1)
        self.assertIn("残留取证进程", stderr)
        self.assertEqual(len(list((campaign / "official" / "attempts").iterdir())), 1)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["head_sha256"], head_before)
        self.assertEqual(self.relay_probe_calls(), calls_before)

        # 清理必须有批准依据；预览列出两个进程，错误摘要被拒，批准后清理并写收据。
        code, _result, stderr = self.cleanup(campaign)
        self.assertEqual(code, 1)
        self.assertIn("明确的恢复批准", stderr)
        code, preview, stderr = self.cleanup(campaign, "--recovery-preview", preview_path)
        self.assertEqual((code, preview["status"]), (0, "approval_required"), stderr)
        self.assertEqual([row["labels"] for row in preview["processes"]], [["claude"], ["relay"]])
        self.assertEqual(preview["basis"]["kind"], "recovery_approval")
        code, _result, stderr = self.cleanup(campaign, "--recovery-preview", preview_path, "--approve-sha256", "0" * 64, "--approved-by", "测试", action="apply")
        self.assertEqual(code, 1)
        self.assertIn("批准作废", stderr)
        code, cleaned, stderr = self.cleanup(
            campaign, "--recovery-preview", preview_path, "--approve-sha256", preview["review_sha256"], "--approved-by", "测试", action="apply"
        )
        self.assertEqual((code, cleaned["status"]), (0, "cleaned"), stderr)
        receipt = _read(Path(cleaned["receipt_path"]))
        self.assertEqual(receipt["before_capture_processes"], ["synthetic:claude", "synthetic:relay"])
        self.assertEqual(receipt["after_capture_processes"], [])
        self.assertEqual(receipt["basis"]["recovery_preview"]["path"], Path(preview_path).relative_to(campaign).as_posix())
        for role in ("before_probe", "after_probe"):
            self.assertEqual(codex_upgrade.file_sha256(campaign / receipt[role]["path"]), receipt[role]["sha256"])
        self.assertIn("resume --rerun-failed", cleaned["next_command"])

        code, resumed, stderr = self.resume(campaign, preview_path)
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual(self.relay_probe_calls()[len(calls_before):], failing_probes)


class ContaminationIsolationTests(_ProcessBoundaryMixin, RecoveryChainCase):
    """残留进程即污染不放宽：污染 attempt → 清理留证 → environment-isolate → 延期 → 对账 → resume 全部重跑 → seal 预览。"""

    def test_contaminated_attempt_cleanup_isolation_resume_reruns_all(self) -> None:
        self._exercise_contaminated_cleanup_isolate_resume()

    def _exercise_contaminated_cleanup_isolate_resume(self) -> None:
        campaign = self.build_recovery_campaign()
        self.inject_residual_processes(["synthetic:relay"])
        first = self.capture_run(campaign)
        self.assertEqual((first["status"], first["results"]), ("environment_contaminated", []))
        self.assertTrue((campaign / capture.CONTAMINATION_MARKER).is_file())
        # 未隔离的污染封锁 Campaign：对账只入账并暂停预算（不出预览），seal 与 resume 都被拒。
        with self.chain_patched():
            code, paused, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
        self.assertEqual((code, paused["status"]), (3, "paused"), stderr)
        self.assertIn("environment", paused["decision"]["pause_kinds"])
        self.assertEqual(paused["deadline_pause"]["status"], "not_expired")
        self.assertNotIn("recovery_preview", paused)
        self.assertEqual(paused["residual_processes"]["status"], "residual")
        with self.chain_patched():
            status = claude_upgrade.campaign_status(campaign)
        self.assertEqual(status["status"], "environment_contaminated")
        code, _result, stderr = self.resume(campaign, str(campaign / "control" / "reconciliation" / "missing.json"))
        self.assertEqual(code, 1)
        self.assertIn("未隔离的环境污染", stderr)

        # 修复：污染之下清理（批准依据是未隔离的污染事实），清理收据作修复收据、清理后的探针清单作干净环境复核。
        code, preview, stderr = self.cleanup(campaign)
        self.assertEqual((code, preview["basis"]["kind"]), (0, "environment_contamination"), stderr)
        code, cleaned, stderr = self.cleanup(campaign, "--approve-sha256", preview["review_sha256"], "--approved-by", "测试", action="apply")
        self.assertEqual((code, cleaned["status"]), (0, "cleaned"), stderr)
        self.assertIn("environment-isolate", cleaned["next_command"])
        receipt = _read(Path(cleaned["receipt_path"]))
        isolate = [
            "environment-isolate", "--campaign-dir", str(campaign),
            "--environment-repair-receipt", cleaned["receipt_path"],
            "--clean-environment-receipt", str(campaign / receipt["after_probe"]["path"]),
            "--reason", "断网容器残留取证进程已在批准下清理",
        ]
        with self.chain_patched():
            code, isolation_preview, stderr = self.run_main(isolate)
            self.assertEqual((code, isolation_preview["status"]), (0, "approval_required"), stderr)
            code, isolated, stderr = self.run_main([*isolate, "--approve-sha256", isolation_preview["review_sha256"], "--approved-by", "测试"])
        self.assertEqual((code, isolated["status"]), (0, "isolated"), stderr)
        self.assertEqual([item["attempt_id"] for item in isolated["invalidated_attempts"]], [first["attempt_id"]])

        # 污染暂停只是判定口径：预算没有到期，不写预算暂停事实，隔离之后直接重新对账（被污染的 attempt 不复用）。
        reconciled = self.reconcile(campaign, first["attempt_id"])
        preview = reconciled["recovery_preview"]
        self.assertEqual(preview["reuse_job_ids"], [])
        self.assertEqual(preview["execute_job_ids"], sorted(seal_tests.SEAL_JOBS))
        self.assertEqual(reconciled["resume_reuse_check"]["status"], "consistent", reconciled["resume_reuse_check"])
        self.approve(campaign, first["attempt_id"], preview)
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual({item["disposition"] for item in resumed["results"]}, {"executed"})
        # 旁路标记仍在，但已被隔离覆盖：seal 不再被它挡住。
        self.assertTrue((campaign / capture.CONTAMINATION_MARKER).is_file())
        with self.chain_patched():
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, resumed["attempt_id"]))
        self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)


class OrphanAttemptTests(RecoveryChainCase):
    """被强杀的孤儿 attempt：写到一半的证据被计量；对账后全部重跑，占据固定证据根的旧目录先取代归档。"""

    def test_sigkilled_attempt_is_accounted_superseded_and_rerun(self) -> None:
        self._exercise_orphan_attempt_recovery()

    def _kill_during_second_job(self, campaign: Path) -> int:
        real_close = capture._close_job
        closed: list[str] = []
        pid = os.fork()
        if pid == 0:  # pragma: no cover - 子进程
            try:
                def killing(context, job, result):
                    closed.append(job.job_id)
                    if len(closed) == 2:
                        os.kill(os.getpid(), signal.SIGKILL)
                    return real_close(context, job, result)

                capture._close_job = killing
                with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    claude_upgrade.main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            finally:
                os._exit(99)
        _pid, status = os.waitpid(pid, 0)
        return status

    def _exercise_orphan_attempt_recovery(self) -> None:
        campaign = self.build_recovery_campaign()
        with self.chain_patched():
            status = self._kill_during_second_job(campaign)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL, status)
        attempts = sorted((campaign / "official" / "attempts").iterdir())
        self.assertEqual(len(attempts), 1)
        orphan = attempts[0]
        self.assertFalse((orphan / "attempt.json").exists())
        receipts = sorted(path.name for path in orphan.glob("job-*.json"))
        self.assertEqual(len(receipts), 1)
        with self.chain_patched():
            self.assertEqual(claude_upgrade.campaign_status(campaign)["status"], "official_capture_interrupted")
        code, _result, stderr = self.resume(campaign, str(campaign / "missing.json"))
        self.assertEqual(code, 1)
        self.assertIn("尚未对账", stderr)

        reconciled = self.reconcile(campaign, orphan.name)
        preview = reconciled["recovery_preview"]
        self.assertFalse(preview["source_attempt_receipt_exists"])
        self.assertEqual((preview["reuse_job_ids"], preview["execute_job_ids"]), ([], sorted(seal_tests.SEAL_JOBS)))
        self.assertEqual(reconciled["resume_reuse_check"]["status"], "not_applicable")
        # 写到一半的第二个 Job 没有收据，但它的证据根已经计入请求账（不因缺收据漏计）。
        provenance = _read(campaign / reconciled["provenance_receipt"]["path"])
        counted = {job["job_id"]: job["precise_count"] for job in provenance["jobs"]}
        in_progress = sorted(set(seal_tests.SEAL_JOBS) - {receipts[0][len("job-"):-len(".json")]})
        self.assertGreater(sum(counted.get(job_id, 0) for job_id in in_progress), 0)
        self.assertEqual(provenance["unresolved_job_ids"], [])
        with self.chain_patched():
            self.assertEqual(claude_upgrade._active_unsealed_attempts(campaign, "official"), [])

        self.approve(campaign, orphan.name, preview)
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        self.assertTrue(resumed["recovery"]["evidence_root_supersession"])
        supersession = _read(campaign / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{resumed['attempt_id']}.json")
        self.assertEqual(len(supersession["roots"]), 2)
        for row in supersession["roots"]:
            self.assertEqual(row["registered_by"], [])
            self.assertTrue(Path(row["archived_host_root"]).is_dir())
            self.assertTrue(Path(row["host_root"]).is_dir())
        self.load_attempt(campaign, resumed["attempt_id"])


def _vc1_run_dir(
    case: RecoveryChainCase,
    campaign: Path,
    name: str,
    *,
    inner: dict[str, Any],
    action_id: str,
    started_offset_seconds: float = -30.0,
    post_run_tooling: bool = False,
) -> Path:
    """一个已终止的 VC-1 父监督器 run 目录（对照 Codex ``_b0_run_dir``）：state、事件、分钟记录、清单、停止收据与动作诊断。"""

    control = case.data / "control"
    control.mkdir(mode=0o700, exist_ok=True)
    run_dir = control / f"run-{name}"
    run_dir.mkdir(mode=0o700)
    finished = subprocess.Popen(["true"])
    finished.wait()
    owner_pid = finished.pid
    started = time.time() + started_offset_seconds
    terminal = started + 1.0
    state = {
        "schema_version": supervisor.STATE_SCHEMA,
        "supervisor_schema_version": supervisor.SCHEMA_VERSION,
        "campaign_id": CAMPAIGN_ID,
        "phase": "VC-1",
        "owner_pid": owner_pid,
        "owner_nonce": "7" * 64,
        "started_at_utc": supervisor._epoch_to_utc(started),
        "started_at_epoch": started,
        "started_monotonic_ns": 1_000_000_000,
        "deadline_at_epoch": started + 3600.0,
        "deadline_monotonic_ns": 3_601_000_000_000,
        "heartbeat_seconds": 5.0,
        "watchdog_timeout_seconds": 30.0,
        "ledger_interval_seconds": 60.0,
        "state": "failed",
        "terminate_owner": False,
        "terminal_at_utc": supervisor._epoch_to_utc(terminal),
        "terminal_at_epoch": terminal,
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    supervisor._append_event(
        run_dir, event_type="dispatch", operation="dispatch", owner_pid=owner_pid, owner_nonce="7" * 64,
        campaign_id=CAMPAIGN_ID, phase="VC-1", status="failed", reason="action-failed",
    )
    supervisor._write_minute_record(
        run_dir, bucket_start=started, bucket_end=terminal, heartbeat=None, owner_alive=False, heartbeat_age=None, classification="failed",
    )
    (run_dir / "campaign-run-manifest.json").write_text(
        json.dumps({
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "manifest": inner,
            "manifest_sha256": supervisor._sha256(supervisor._canonical(inner)),
        }),
        encoding="utf-8",
    )
    supervisor._stop_receipt(
        run_dir, event_type="failed", reason=f"action-failed:{action_id}", detected_at_epoch=terminal,
        owner_pid=owner_pid, owner_nonce="7" * 64, campaign_id=CAMPAIGN_ID, phase="VC-1",
    )
    diagnostic = supervisor._write_action_diagnostic(
        supervisor._action_diagnostic_path(run_dir, action_id, create_directory=True),
        campaign_id=CAMPAIGN_ID, phase="VC-1", action_id=action_id, owner_pid=owner_pid, owner_nonce="7" * 64,
        failure_kind="handled-error", failure_class="execution-failure", failure_observations=None,
        error_type="ConfigurationError", message="动作在预约之前失败。",
    )
    if post_run_tooling:
        classification = supervisor.post_run_tooling_facts(campaign, inner, run_started_at_utc=str(state["started_at_utc"]))
        case.assertTrue(classification["qualifies"], classification["reasons"])
        supervisor._write_post_run_tooling_receipt(
            run_dir, campaign_id=CAMPAIGN_ID, phase="VC-1", action_id=action_id, owner_pid=owner_pid, owner_nonce="7" * 64,
            diagnostic_sha256=str(diagnostic["diagnostic_sha256"]), facts=classification["facts"],
        )
    for path in run_dir.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return run_dir


class SupervisorRunReconcileTests(RecoveryChainCase):
    """无预约的父监督器 run 与 seal 链零请求失败：reconcile-supervisor-run 入账判定，phase 保持 active 后重派。"""

    def _batch(self, campaign: Path, action_id: str, command: list[str], items: list[str]) -> dict[str, Any]:
        return {
            "schema_version": supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
            "campaign_id": CAMPAIGN_ID,
            "phase": "VC-1",
            "deadline_seconds": 3600,
            "no_op": False,
            "execute_items": items,
            "reuse_items": [],
            "actions": [{"action_id": action_id, "operation": f"VC-1:{action_id}", "timeout_seconds": 600, "command": command, "item_ids": items}],
        }

    def test_prereservation_failure_reconciles_for_redispatch(self) -> None:
        campaign = self.build_recovery_campaign()
        command = ["python3", "claude_upgrade.py", "capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"]
        run_dir = _vc1_run_dir(self, campaign, "prereserve", inner=self._batch(campaign, "capture-official", command, sorted(seal_tests.SEAL_JOBS)), action_id="capture-official")
        with self.chain_patched(), self.segment_paths_forbidden():
            code, result, stderr = self.run_main(["reconcile-supervisor-run", "--campaign-dir", str(campaign), "--run-dir", str(run_dir)])
            self.assertEqual((code, result["status"]), (0, "recoverable"), stderr)
            self.assertEqual(result["live_request_count"], 0)
            self.assertIn("compile-and-run-vc-batch", result["next_command"])
            self.assertEqual(timing.inspect_ledger(self.ledger_dir)["active_phase"], "VC-1")
            again = self.run_main(["reconcile-supervisor-run", "--campaign-dir", str(campaign), "--run-dir", str(run_dir)])[1]
        self.assertTrue(again["batch"]["reused"])

    def test_seal_chain_zero_request_failure_is_post_run_tooling(self) -> None:
        campaign = self.build_recovery_campaign()
        attempt = self.capture_run(campaign)
        self.assertEqual(attempt["status"], "awaiting_receipts")
        command = ["python3", "claude_upgrade.py", *self.seal_argv(campaign, attempt["attempt_id"])]
        run_dir = _vc1_run_dir(
            self, campaign, "seal", inner=self._batch(campaign, "seal-official-preview", command, ["seal-official-preview"]),
            action_id="seal-official-preview", started_offset_seconds=1.0, post_run_tooling=True,
        )
        with self.chain_patched(), self.segment_paths_forbidden():
            code, result, stderr = self.run_main(["reconcile-supervisor-run", "--campaign-dir", str(campaign), "--run-dir", str(run_dir)])
        self.assertEqual((code, result["status"]), (0, "recoverable"), stderr)
        receipt = _read(campaign / result["reconciliation_receipt"]["path"])
        self.assertEqual(receipt["failure_class"], "post-run-tooling")
        self.assertEqual(receipt["run"]["action_diagnostic"]["post_run_tooling"]["attempt_id"], attempt["attempt_id"])
        self.assertIn("官方 seal 链零请求失败已对账", result["next_command"])


class DeadlineExtensionReconcileTests(RecoveryChainCase):
    """批准延期改变 Campaign 有效截止之后，同一 attempt 仍能再对账（对账收据把截止当作易变的控制事实）。"""

    def _extend_campaign_deadline(self, campaign: Path, *, minutes: int, scope: str = "campaign", phase: str | None = None) -> None:
        (campaign / "control").chmod(0o700)
        extension = project_ledger.preview_deadline_extension(
            campaign, scope=scope, phase=phase,
            new_deadline_at_utc=(datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(), reason="B2-08 延期后再对账",
        )
        project_ledger.apply_deadline_extension(
            campaign, preview_path=Path(extension["preview_path"]), approve_sha256=extension["review_sha256"], approved_by="测试"
        )

    def test_extension_between_reconciles_keeps_attempt_reconcilable(self) -> None:
        campaign = self.build_recovery_campaign()
        self.fail_probe(self.job_probes(FAILING_JOB)[0], 3)
        first = self.capture_run(campaign)
        before = self.reconcile(campaign, first["attempt_id"])
        self._extend_campaign_deadline(campaign, minutes=3 * 24 * 60)
        after = self.reconcile(campaign, first["attempt_id"])
        self.assertEqual(after["reconciliation_receipt"], before["reconciliation_receipt"])
        self.assertEqual(after["recovery_preview"]["execute_job_ids"], [FAILING_JOB])

    def _exercise_expired_deadline_pause_extend_reconcile(self) -> None:
        """真实到期（总预算一分钟）：对账判预算暂停并写暂停事实 → 批准延期 → 从原对账继续 → 批准预览 → resume。

        要真实等待预算到期（约一分钟），只供真实链外壳复用，不进默认测试。
        """

        create = timing.create_ledger

        def short(*arguments, **options):
            options.update(total_budget_minutes=1, stage_budgets_minutes={phase: 1 for phase in timing.PHASE_ORDER})
            return create(*arguments, **options)

        with mock.patch.object(timing, "create_ledger", side_effect=short):
            campaign = self.build_recovery_campaign()
        self.fail_probe(self.job_probes(FAILING_JOB)[0], 3)
        first = self.capture_run(campaign)
        from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

        deadline = datetime.fromisoformat(vc_artifacts.effective_deadlines(campaign)["total_deadline_at_utc"].replace("Z", "+00:00"))
        while datetime.now(timezone.utc) <= deadline:
            time.sleep(0.5)
        with self.chain_patched():
            code, paused, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
        self.assertEqual((code, paused["status"], paused["deadline_pause"]["status"]), (3, "paused", "deadline_paused"), stderr)
        # 预算暂停期间：入口的项目总账消费者门禁拒绝 capture-official run 与 seal，不写 attempt。
        attempts_before = sorted(path.name for path in (campaign / "official" / "attempts").iterdir())
        with self.chain_patched():
            code, _result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            self.assertEqual(code, 1)
            self.assertIn("预算已暂停", stderr)
            code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("预算已暂停", stderr)
        self.assertEqual(sorted(path.name for path in (campaign / "official" / "attempts").iterdir()), attempts_before)
        # 总预算与 VC-1 阶段预算同时到期：两层各批准一次延期。
        self._extend_campaign_deadline(campaign, minutes=3 * 24 * 60)
        self._extend_campaign_deadline(campaign, minutes=2 * 24 * 60, scope="stage", phase="VC-1")
        reconciled = self.reconcile(campaign, first["attempt_id"])
        self.approve(campaign, first["attempt_id"], reconciled["recovery_preview"])
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)


EVOLVED_JOB = "official-tools"
EVOLVED_DEPENDENCY = "capturelib/claude_fw_f_v3.py"
# 只改 control 层的演进：Claude 策略按前缀归 control、不在 wire／evidence 闭包内的受管文件（与 B5a-05 外壳同一文件）。
CONTROL_DEPENDENCY = "claude_upgrade_store.py"


class ToolEvolutionChainTests(RecoveryChainCase):
    """工具演进：编排器链与 Store 两侧登记；被作废的作业只重跑它们，seal 两层都认新身份。"""

    def build_evolution_campaign(self) -> Path:
        original = capture_tests._reduced_payload

        def narrowed(job_ids):
            payload = original(job_ids)
            for job in payload["capture_jobs"]:
                if job["id"] != EVOLVED_JOB:
                    job["tool_dependencies"] = [item for item in job["tool_dependencies"] if item != EVOLVED_DEPENDENCY]
            return payload

        with mock.patch.object(capture_tests, "_reduced_payload", side_effect=narrowed):
            return self.build_recovery_campaign()

    def evolved_identity(self) -> dict[str, Any]:
        identity = json.loads(json.dumps(self.tool_identity))
        key = f"official_client_capture/{EVOLVED_DEPENDENCY}"
        for entry in identity["entries"]:
            if entry["path"] == key:
                entry["sha256"] = "f" * 64
        identity["files_sha256"] = claude_upgrade._fingerprint({"entries": identity["entries"]})
        identity["wire_producer_sha256"] = "e" * 64
        return identity

    def test_tool_evolution_registers_both_sides_and_resume_reruns_invalidated_jobs(self) -> None:
        self._exercise_tool_evolution_resume()

    def _exercise_tool_evolution_resume(self) -> None:
        campaign = self.build_evolution_campaign()
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "awaiting_receipts")
        with self.chain_patched():
            code, preview, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
        self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
        store_root = claude_upgrade_store.campaign_store_root(campaign)

        # 修工具并部署：编排器侧当前身份换成那个依赖文件变了的身份；Store 侧受管树副本里同一文件也改掉；部署收据随之换新。
        evolved = self.evolved_identity()
        key = f"official_client_capture/{EVOLVED_DEPENDENCY}"
        self.tree.write(key, self.tree.path(key).read_bytes() + b"\n# B2-08 evolution fixture\n")
        self.addCleanup(self.tree.restore)
        ledger_fixture.write_claude_deploy_receipt(self.data, evolved, stamp="20261007t010000z")
        evolution_argv = ["tool-evolution", "--campaign-dir", str(campaign), "--fix-commit", "a" * 40, "--reason", "修复 FW-F v3 判定"]
        with self.chain_patched(), mock.patch.object(claude_upgrade, "_tool_identity", side_effect=lambda **_: json.loads(json.dumps(evolved))):
            code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("尚未登记工具演进", stderr)
            # 未登记演进时 run 在执行租约内被拒：共享租约把异常退出记成停线（stop_the_line）。停线租约不挡后面的恢复：
            # 演进登记与对账不取执行租约，resume 照常取得租约并在结束时释放（见下文）。
            code, _result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            self.assertEqual(code, 1)
            self.assertIn("尚未登记工具演进", stderr)
            self.assertEqual(_read(campaign / ".campaign-lease.json")["state"], "stop_the_line")
            code, proposed, stderr = self.run_main(evolution_argv)
            self.assertEqual((code, proposed["status"]), (0, "approval_required"), stderr)
            self.assertEqual(proposed["impact"]["official"]["affected_job_ids"], [EVOLVED_JOB])
            self.assertEqual(proposed["changes"]["impact_paths"], [key])
            self.assertEqual((proposed["evaluator"]["from"]["status"], proposed["evaluator"]["to"]["status"]), ("not_established", "not_established"))
            self.assertEqual(proposed["evaluator"]["changed_fields"], [])
            code, applied, stderr = self.run_main([*evolution_argv, "--approve-sha256", proposed["review_sha256"], "--approved-by", "测试"])
            self.assertEqual((code, applied["status"], applied["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)
            self.assertEqual(len(store_receipts.tool_evolutions(*claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign)))), 1)
            # Store 登记的输入临时文件在 Store 根之外，用完即删。
            scratch = campaign / "official" / "seal" / "tool-evolution" / "store-inputs"
            self.assertEqual((scratch.is_dir(), list(scratch.iterdir())), (True, []))
            self.assertEqual(sorted(path.name for path in store_root.iterdir() if path.name.startswith(".tool-evolution")), [])
            # 以同一批准重跑：编排器收据已写，只核 Store 登记（已是当前身份），不重算预览。
            code, again, stderr = self.run_main([*evolution_argv, "--approve-sha256", proposed["review_sha256"], "--approved-by", "测试"])
            self.assertEqual((code, again["reused"], again["store_registration"]["status"]), (0, True, "current"), stderr)
            status = claude_upgrade.campaign_status(campaign)
            self.assertEqual(status["status"], "official_capture_failed")
            self.assertEqual(status["official_attempt"]["evolution_invalidated_job_ids"], [EVOLVED_JOB])
            code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("被工具演进作废", stderr)

            code, reconciled, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
            self.assertEqual((code, reconciled["status"]), (0, "recoverable"), stderr)
            self.assertEqual(reconciled["tool_evolution_invalidation"]["invalidated_job_ids"], [EVOLVED_JOB])
            preview = reconciled["recovery_preview"]
            self.assertEqual(preview["execute_job_ids"], [EVOLVED_JOB])
            self.assertEqual(reconciled["resume_reuse_check"]["status"], "consistent", reconciled["resume_reuse_check"])
            code, _approved, stderr = self.run_main(
                ["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"], "--approve-recovery-sha256", preview["review_sha256"]]
            )
            self.assertEqual(code, 0, stderr)
            calls_before = self.relay_probe_calls()
            code, resumed, stderr = self.run_main(
                ["resume", "--campaign-dir", str(campaign), "--rerun-failed", "--recovery-preview", reconciled["recovery_preview_path"], "--acknowledge-live-requests"]
            )
            self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
            self.assertEqual(_read(campaign / ".campaign-lease.json")["state"], "released")
            self.assertEqual(self.relay_probe_calls()[len(calls_before):], self.job_probes(EVOLVED_JOB))
            reservation = _read(campaign / "official" / "attempts" / resumed["attempt_id"] / "reservation.json")
            self.assertEqual(reservation["tool_evolution"]["index"], 1)
            # 新 attempt 在演进后的身份下产出：seal 两层核对都通过，进入预览。
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, resumed["attempt_id"]))
            self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)
        self.assertTrue((store_root / "store.json").is_file())


class ControlEvolutionSealTests(RecoveryChainCase):
    """只改 control 层的工具演进（B5a-05 tool-evolution 链第二部分的形态）：不作废任何作业；未登记时 seal 被拒、run 被拒留下
    停线租约 → 登记演进（编排器链与 Store 两侧）→ seal 两层放行、停线租约不挡，批准后封存完成。Store Campaign 没有在工具
    变化之前建立时，演进登记零写入拒绝。"""

    EVOLUTION_ARGV = ("--fix-commit", "c" * 40, "--reason", "修复 Store 封装")

    def control_evolved_identity(self) -> dict[str, Any]:
        identity = json.loads(json.dumps(self.tool_identity))
        key = f"official_client_capture/{CONTROL_DEPENDENCY}"
        matched = [entry for entry in identity["entries"] if entry["path"] == key]
        self.assertEqual(len(matched), 1)
        matched[0]["sha256"] = "c" * 64
        identity["files_sha256"] = claude_upgrade._fingerprint({"entries": identity["entries"]})
        identity["control_sha256"] = "d" * 64
        return identity

    def mutate_control_layer(self) -> dict[str, Any]:
        """修工具并部署：编排器侧当前身份只有 control 摘要变化，Store 侧受管树副本里同一文件也改掉，部署收据随之换新。"""

        evolved = self.control_evolved_identity()
        key = f"official_client_capture/{CONTROL_DEPENDENCY}"
        self.tree.write(key, self.tree.path(key).read_bytes() + b"\n# B2-08 control evolution fixture\n")
        self.addCleanup(self.tree.restore)
        ledger_fixture.write_claude_deploy_receipt(self.data, evolved, stamp="20261007t030000z")
        return evolved

    def evolved_scope(self, evolved: dict[str, Any]) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(self.chain_patched())
        stack.enter_context(mock.patch.object(claude_upgrade, "_tool_identity", side_effect=lambda **_: json.loads(json.dumps(evolved))))
        return stack

    def test_control_evolution_then_seal_passes_both_layers(self) -> None:
        self._exercise_control_evolution_seal()

    def _exercise_control_evolution_seal(self) -> None:
        campaign = self.build_recovery_campaign()
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "awaiting_receipts")
        evolved = self.mutate_control_layer()
        evolution_argv = ["tool-evolution", "--campaign-dir", str(campaign), *self.EVOLUTION_ARGV]
        with self.evolved_scope(evolved):
            code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("尚未登记工具演进", stderr)
            code, _result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            self.assertEqual(code, 1, stderr)
            self.assertEqual(_read(campaign / ".campaign-lease.json")["state"], "stop_the_line")

            code, proposed, stderr = self.run_main(evolution_argv)
            self.assertIsNotNone(proposed, stderr)
            self.assertEqual((code, proposed["status"]), (0, "approval_required"), stderr)
            self.assertEqual(proposed["impact"]["official"]["affected_job_ids"], [])
            self.assertEqual((proposed["changes"]["impact_paths"], proposed["changes"]["evaluation_paths"]), ([], []))
            code, applied, stderr = self.run_main([*evolution_argv, "--approve-sha256", proposed["review_sha256"], "--approved-by", "测试"])
            self.assertIsNotNone(applied, stderr)
            self.assertEqual((code, applied["status"], applied["store_registration"]["status"]), (0, "evolution_applied", "registered"), stderr)

            # seal 两层核对都通过、停线租约不挡；批准后封存完成。
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertIsNotNone(sealed_preview, stderr)
            self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)
            store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, claude_upgrade.load_campaign_manifest(campaign))
            self.assertEqual(len(store_receipts.tool_evolutions(store, dict(store_campaign))), 1)
            code, sealed, stderr = seal_tests.run_main(
                self.seal_argv(campaign, first["attempt_id"], "--approve-seal-sha256", sealed_preview["review_sha256"])
            )
            self.assertIsNotNone(sealed, stderr)
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)

    def test_tool_evolution_refused_without_store_campaign(self) -> None:
        campaign = self.build_recovery_campaign()
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "awaiting_receipts")
        # Store Campaign 没有在工具变化之前建立（夹具里删掉）：演进之后再也建不出来，登记演进时就零写入拒绝。
        shutil.rmtree(claude_upgrade_store.campaign_store_root(campaign))
        evolved = self.mutate_control_layer()
        with self.evolved_scope(evolved):
            code, _result, stderr = self.run_main(["tool-evolution", "--campaign-dir", str(campaign), *self.EVOLUTION_ARGV])
        self.assertEqual(code, 1)
        self.assertIn("Store Campaign 尚未建立", stderr)
        self.assertFalse((campaign / wire_transition.EVOLUTION_DIR).exists())


class ConsumerAdmissionTests(RecoveryChainCase):
    """项目总账消费者门禁：resume 只挡本恢复预览根因与达上限根因的交集；正式 run 与 seal 整版本保守挡。"""

    def test_root_cause_limit_blocks_only_the_retried_cause(self) -> None:
        campaign = self.build_recovery_campaign()
        self.fail_probe(self.job_probes(FAILING_JOB)[0], 3)
        first = self.capture_run(campaign)
        reconciled = self.reconcile(campaign, first["attempt_id"])
        self.approve(campaign, first["attempt_id"], reconciled["recovery_preview"])
        causes = reconciler.receipt_root_cause_ids(campaign / reconciled["reconciliation_receipt"]["path"])
        self.assertTrue(causes)
        attempts_before = sorted(path.name for path in (campaign / "official" / "attempts").iterdir())
        with mock.patch.object(project_ledger, "root_causes_at_limit_for", return_value=list(causes)):
            code, _result, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
            self.assertEqual(code, 1)
            self.assertIn("根因达上限", stderr)
            with self.chain_patched():
                code, _result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
            self.assertEqual(code, 1)
            self.assertIn("项目总账拒绝 capture-official", stderr)
            with self.chain_patched():
                code, _result, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual(code, 1)
            self.assertIn("根因达上限", stderr)
        self.assertEqual(sorted(path.name for path in (campaign / "official" / "attempts").iterdir()), attempts_before)
        # 别的根因达上限不挡这次续跑。
        with mock.patch.object(project_ledger, "root_causes_at_limit_for", return_value=["claude-unrelated-root-cause"]):
            code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)


class RootCauseLimitRepairTests(RecoveryChainCase):
    """同根因两次失败达上限：对账只暂停（root_cause_repair）、入口准入挡住该根因 → campaign-resume 凭修复证据放行（部署
    收据经适配器读 identity 子键）→ 再对账停线对象可恢复 → resume 只重跑失败 Job（复用的复用仍追到最初产出者）。"""

    def test_root_cause_limit_pause_campaign_resume_then_recover(self) -> None:
        self._exercise_root_cause_limit_repair_resume()

    def _exercise_root_cause_limit_repair_resume(self) -> None:
        campaign = self.build_recovery_campaign()
        failing_probes = self.job_probes(FAILING_JOB)
        # 首批与第一次续跑里失败 Job 都连续失败三次（同一根因失败两次），之后不再注入失败。
        self.fail_probe(failing_probes[0], 6)
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "failed")
        reconciled = self.reconcile(campaign, first["attempt_id"])
        self.approve(campaign, first["attempt_id"], reconciled["recovery_preview"])
        code, second, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertIsNotNone(second, stderr)
        self.assertEqual((code, second["status"]), (2, "failed"), stderr)

        # 第二次同根因失败入账：达上限只暂停（不写终态），账本 stop_required；入口准入挡住该根因。
        with self.chain_patched():
            code, paused, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", second["attempt_id"]])
        self.assertIsNotNone(paused, stderr)
        self.assertEqual((code, paused["status"], paused["decision"]["pause_kinds"]), (3, "paused", ["root_cause_repair"]), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "stop_required")
        cause = str(paused["root_cause"]["root_cause_id"])
        head = project_ledger.replay_head(self.project_root)
        self.assertNotIn(CAMPAIGN_ID, head["terminal_campaigns"])
        self.assertIn(cause, project_ledger.root_causes_at_limit_for(head, project_ledger.campaign_target_version(head, CAMPAIGN_ID)))
        with self.chain_patched():
            code, _result, stderr = self.run_main(["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"])
        self.assertEqual(code, 1)
        self.assertIn("根因达上限", stderr)

        # 修复后受监督部署（收据晚于停线）加离线回归收据 → campaign-resume 预览、批准：两本账清零该根因。
        regression = self.root / "regression-summary.txt"
        regression.write_text("test_claude_upgrade_recovery exit=0\n", encoding="utf-8")
        resume_argv = [
            "campaign-resume", "--campaign-dir", str(campaign), "--fix-commit", "b" * 40,
            "--regression-receipt", str(regression), "--reason", "修复了导致同根因连续失败的工具缺陷",
        ]
        with self.chain_patched():
            code, _result, stderr = self.run_main(resume_argv)
        self.assertEqual(code, 1)
        self.assertIn("部署收据早于停线", stderr)
        created = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        ledger_fixture.write_claude_deploy_receipt(self.data, self.tool_identity, stamp="20261007t020000z", created_at_utc=created)
        with self.chain_patched():
            code, preview, stderr = self.run_main(resume_argv)
        self.assertIsNotNone(preview, stderr)
        self.assertEqual((code, preview["status"]), (0, "approval_required"), stderr)
        self.assertEqual(preview["project"]["cleared_root_cause_ids"], [cause])
        with self.chain_patched():
            code, applied, stderr = self.run_main([*resume_argv, "--approve-sha256", preview["review_sha256"], "--approved-by", "测试"])
        self.assertIsNotNone(applied, stderr)
        self.assertEqual((code, applied["status"]), (0, "resumed"), stderr)
        self.assertEqual(timing.inspect_ledger(self.ledger_dir)["status"], "active")

        # 再对账停线对象：可恢复，预览只执行失败 Job；批准后 resume 续跑通过。
        calls_before = self.relay_probe_calls()
        replay = self.reconcile(campaign, second["attempt_id"])
        self.assertEqual(replay["recovery_preview"]["execute_job_ids"], [FAILING_JOB])
        self.approve(campaign, second["attempt_id"], replay["recovery_preview"])
        code, third, stderr = self.resume(campaign, replay["recovery_preview_path"])
        self.assertIsNotNone(third, stderr)
        self.assertEqual((code, third["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual(self.relay_probe_calls()[len(calls_before):], failing_probes)
        # 复用的复用：第三个 attempt 的复用结果绑定第二个 attempt，最初产出者仍是首批 attempt。
        _root, attempt = self.load_attempt(campaign, third["attempt_id"])
        reused = [item for item in attempt["results"] if item["disposition"] == "reused"]
        self.assertEqual(len(reused), len(seal_tests.SEAL_JOBS) - 1)
        self.assertEqual({item["reused_from"]["attempt_id"] for item in reused}, {second["attempt_id"]})
        self.assertEqual({item["reused_from"]["producer"]["attempt_id"] for item in reused}, {first["attempt_id"]})


class SealedAccountingTests(RecoveryChainCase):
    """官方阶段封存后的请求入账（account-sealed-official）：采集路径经共享流程写入 Claude 项目总账、同一 attempt 幂等；只读
    复用封存零请求，没有可入账的请求。"""

    def test_sealed_official_requests_are_accounted_once(self) -> None:
        campaign = self.build_recovery_campaign()
        first = self.capture_run(campaign)
        self.assertEqual(first["status"], "awaiting_receipts")
        with self.chain_patched():
            code, preview, stderr = seal_tests.run_main(self.seal_argv(campaign, first["attempt_id"]))
            self.assertEqual((code, preview["status"]), (2, "awaiting_seal_approval"), stderr)
            code, sealed, stderr = seal_tests.run_main(
                self.seal_argv(campaign, first["attempt_id"], "--approve-seal-sha256", preview["review_sha256"])
            )
            self.assertEqual((code, sealed["status"]), (0, "complete"), stderr)
            code, accounted, stderr = self.run_main(["account-sealed-official", "--campaign-dir", str(campaign)])
            self.assertIsNotNone(accounted, stderr)
            self.assertEqual((code, accounted["status"], accounted["attempt_id"]), (0, "accounted", first["attempt_id"]), stderr)
            self.assertGreater(accounted["request"]["identity_key_count_total"], 0)
            total = project_ledger.replay_head(self.project_root)["precise_total"]
            self.assertGreater(total, 0)
            # 同一 attempt 重跑：返回既有 batch，总账不重复计数。
            code, again, stderr = self.run_main(["account-sealed-official", "--campaign-dir", str(campaign)])
            self.assertEqual((code, again["status"]), (0, "accounted"), stderr)
            self.assertEqual(project_ledger.replay_head(self.project_root)["precise_total"], total)
            self.assertEqual(claude_upgrade.campaign_status(campaign)["status"], "official_sealed")

    def test_predecessor_reuse_seal_has_nothing_to_account(self) -> None:
        campaign = self.build_recovery_campaign()
        stage = {"mode": "predecessor_reuse", "status": "complete"}
        with self.chain_patched(), mock.patch.object(claude_upgrade, "_load_stage_result", return_value=stage):
            code, result, stderr = self.run_main(["account-sealed-official", "--campaign-dir", str(campaign)])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"], result["live_request_count"]), (0, "not_applicable", 0), stderr)


class RunCloseoutFailureTests(RecoveryChainCase):
    """run 末权限收口失败（不提供 harden-evidence-permissions 的依据）：attempt 记 failed，对账后 resume 全部复用、零执行、重新收口。"""

    def test_run_closeout_failure_recovers_by_reuse_only_resume(self) -> None:
        self._exercise_run_closeout_failure_resume()

    def _exercise_run_closeout_failure_resume(self) -> None:
        campaign = self.build_recovery_campaign()
        permissions = capture.codex_upgrade_evidence_permissions
        original = permissions.close_evidence_permissions

        def failing_run_closeout(root: Path, roots: Any, **options: Any) -> Any:
            # 只让 attempt 级（run 末）收口失败；Job 级收口（根是 <attempt>/evidence/jobs/<job>）照常。
            if Path(root).parent.name == "attempts":
                raise permissions.EvidencePermissionError("run 末收口失败（测试注入）")
            return original(root, roots, **options)

        with mock.patch.object(permissions, "close_evidence_permissions", side_effect=failing_run_closeout):
            first = self.capture_run(campaign)
        self.assertEqual(first["status"], "failed")
        self.assertEqual({item["status"] for item in first["results"]}, {"complete"})
        _root, attempt = self.load_attempt(campaign, first["attempt_id"])
        self.assertIsNone(attempt["evidence_permission_closeout"])
        self.assertEqual(set(attempt["evidence_permission_error"]), {"type", "message"})
        calls_after_first = self.relay_probe_calls()

        # 对账：全部作业都完成且 Job 级收口可重放，预览全部复用、执行集合为空。
        reconciled = self.reconcile(campaign, first["attempt_id"])
        preview = reconciled["recovery_preview"]
        self.assertEqual(preview["execute_job_ids"], [])
        self.assertEqual(preview["reuse_job_ids"], sorted(seal_tests.SEAL_JOBS))
        self.assertEqual(reconciled["resume_reuse_check"]["status"], "consistent", reconciled["resume_reuse_check"])
        self.approve(campaign, first["attempt_id"], preview)
        code, resumed, stderr = self.resume(campaign, reconciled["recovery_preview_path"])
        self.assertIsNotNone(resumed, stderr)
        self.assertEqual((code, resumed["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual(self.relay_probe_calls(), calls_after_first, "全部复用的 resume 不应调用 relay")
        self.assertEqual({item["disposition"] for item in resumed["results"]}, {"reused"})
        # 新 attempt 的 run 末收口通过（覆盖全部被复用的证据根）：默认读取即重放它；之后可进 seal 预览。
        _root, attempt = self.load_attempt(campaign, resumed["attempt_id"])
        self.assertIsNotNone(attempt["evidence_permission_closeout"])
        self.assertIsNone(attempt["evidence_permission_error"])
        with self.chain_patched():
            code, sealed_preview, stderr = seal_tests.run_main(self.seal_argv(campaign, resumed["attempt_id"]))
        self.assertEqual((code, sealed_preview["status"]), (2, "awaiting_seal_approval"), stderr)


class ReuseCheckAndStatusTests(RecoveryChainCase):
    """R17：复用证据在收口之后被改动时，预览与 resume 判定不一致、不可批准；status 的官方阶段状态。"""

    def test_reuse_check_inconsistency_blocks_approval_and_tampering_fails_closed(self) -> None:
        campaign = self.build_recovery_campaign()
        with self.chain_patched():
            self.assertEqual(claude_upgrade.campaign_status(campaign)["status"], "planned")
            code, status, stderr = self.run_main(["status", "--campaign-dir", str(campaign)])
        self.assertEqual((code, status["status"]), (0, "planned"), stderr)
        self.fail_probe(self.job_probes(FAILING_JOB)[0], 3)
        first = self.capture_run(campaign)

        # resume 的复用判定不成立（这里让复用证据复核失败）：预览照常落盘留痕，但 R17 判不一致、拒绝批准。
        with mock.patch.object(recovery, "_verify_reusable_evidence", side_effect=ConfigurationError("复用证据复核未通过（测试注入）")):
            with self.chain_patched():
                code, result, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
            self.assertIsNotNone(result, stderr)
            self.assertEqual((code, result["status"]), (0, "recoverable"), stderr)
            self.assertEqual(result["resume_reuse_check"]["status"], "inconsistent")
            self.assertIn("测试注入", result["resume_reuse_check"]["reason"])
            self.assertIn("不可批准", result["next_command"])
            with self.chain_patched():
                code, _approved, stderr = self.run_main(
                    ["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"],
                     "--approve-recovery-sha256", result["recovery_preview"]["review_sha256"]]
                )
            self.assertEqual(code, 1)
            self.assertIn("复用判定不一致", stderr)
        with self.chain_patched():
            self.assertEqual(claude_upgrade.campaign_status(campaign)["status"], "official_capture_failed")
            with self.assertRaisesRegex(claude_upgrade.ClaudeOrchestratorPending, "B2-11"):
                claude_upgrade.campaign_status(campaign, "c1")

        # 收口之后证据被改动：读取 attempt 时的权限收口重放失败关闭，对账不写任何收据。
        _root, attempt = self.load_attempt(campaign, first["attempt_id"], replay=False)
        victim = next(item for item in attempt["results"] if item["status"] == "complete")
        manifest_path = Path(victim["relay_checks"][0]["manifest"]["path"])
        manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        receipt = campaign / result["reconciliation_receipt"]["path"]
        receipt_sha256 = codex_upgrade.file_sha256(receipt)
        with self.chain_patched():
            code, _result, stderr = self.run_main(["reconcile-attempt", "--campaign-dir", str(campaign), "--attempt-id", first["attempt_id"]])
        self.assertEqual(code, 1)
        self.assertIn("权限收口", stderr)
        self.assertEqual(codex_upgrade.file_sha256(receipt), receipt_sha256)


class AdapterMemberTests(unittest.TestCase):
    """B2-08 新增的三个适配器成员：Codex 取值逐字等于原写法，Claude 按自己的收据形状。"""

    def test_deploy_receipt_identity_and_effective_wire_identity(self) -> None:
        codex, claude = uca.adapter_for("codex"), uca.adapter_for("claude")
        payload = {"tool_files_sha256": "1" * 64, "identity": {"tool_files_sha256": "2" * 64}, "client": "claude"}
        self.assertIs(codex.deploy_receipt_identity(payload), payload)
        self.assertEqual(claude.deploy_receipt_identity(payload), {"tool_files_sha256": "2" * 64})
        self.assertEqual(claude.deploy_receipt_identity({**payload, "client": "codex"}), {})
        self.assertIs(codex.effective_wire_identity, wire_transition.effective_wire_identity)
        self.assertIs(claude.effective_wire_identity, evolution.effective_wire_identity)
        restored = {"environment": {"before_probe": {}, "after_probe": {}, "drifted_kinds": []}}
        self.assertTrue(claude.attempt_environment_restored(restored))
        self.assertFalse(claude.attempt_environment_restored({"environment": {"before_probe": {}, "after_probe": {}, "drifted_kinds": ["processes"]}}))
        self.assertFalse(codex.attempt_environment_restored(restored))
        self.assertTrue(codex.attempt_environment_restored({"environment": {"after_probe": {}, "restoration_report": {}}}))
        self.assertEqual(evolution.POLICY_ENTRY, "official_client_capture/claude_tool_identity_policy_v2.json")


class RefusedSubcommandTests(unittest.TestCase):
    """harden-evidence-permissions 与 wire transition：参数与 Codex 同名同参，执行时明确拒绝并说明理由。"""

    def test_refusals_explain_reasons(self) -> None:
        cases = (
            (["harden-evidence-permissions", "preview", "--campaign-dir", "/nonexistent", "--attempt-id", "a1"], "Job 终态与 run 末"),
            (["wire-transition-intent", "--campaign-dir", "/nonexistent"], "tool-evolution"),
            (["wire-transition-final", "--campaign-dir", "/nonexistent", "--attempt-id", "a1"], "tool-evolution"),
        )
        for argv, message in cases:
            with self.subTest(command=argv[0]):
                code, _result, stderr = seal_tests.run_main(argv)
                self.assertEqual(code, 1)
                self.assertIn("Claude 不提供", stderr)
                self.assertIn(message, stderr)


if __name__ == "__main__":
    unittest.main()
