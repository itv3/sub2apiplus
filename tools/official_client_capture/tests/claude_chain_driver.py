"""Claude 连续故障链的驱动（B5b-03 B 组）：同一 Campaign 内执行候选取证、对账、批准与续跑。

本模块不登记测试入口。被验收的对账、恢复批准消费、续跑、请求核算、两本账与工具演进全部调用真实实现。
输入源沿用已有夹具，集中在 ``source_campaign``、``source_context`` 和 ``install_relay_source`` 三个接口：

* CandidateFixture 提供宿主脚本、Docker、网关／激活事实与 VC-4 准入来源；
* _SealCampaignCase 提供 Store bootstrap／Campaign 和 VC-0 已完成的输入；官方封存读侧沿用
  CandidateRecoveryFixture 的「前序已经封存、本 Campaign 没有官方 attempt」来源；
* FULL_FAKE_RELAY 按既有连接序列写原始请求字节，CANDIDATE_RELAY 提供候选封套和 dry-run；不替换 provenance 判定；
* VC-4 已完成、VC-5 开始的账本起点属于输入夹具；从首个候选 attempt 起全部真实追加。

B5b-02 可覆盖上述三个来源接口换成录制来源，不需要替换下方的控制动作。当前覆盖不是完整无接缝升级链。
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_recovery as recovery_tests
from tools.official_client_capture.tests import test_claude_upgrade_seal as seal_tests

CANDIDATE = candidate_tests.CANDIDATE
LIVE_JOB = candidate_tests.LIVE_JOB
REFRESH_JOB = candidate_tests.REFRESH_JOB
REFRESH_PROBE = "v4-oauth-refresh"
EVOLVED_DEPENDENCY = "claude_run_candidate_capture.sh"


class ClaudeCandidateChainDriver(candidate_tests.CandidateFixture, recovery_tests.RecoveryChainCase):
    """复用已有采集与 Store 夹具；把真实控制动作集中起来，场景只负责注入故障与核对结果。"""

    def setUp(self) -> None:
        super().setUp()
        self.install_relay_source()

    def install_relay_source(self) -> None:
        """组合已有两份中继来源：保留候选封套，真实原始字节由完整中继夹具写出，再沿用失败注入表。

        只在临时数据根生成可执行替身；受管源码不变。干运行在候选来源的分支里直接退出，不执行完整来源。
        """

        candidate_source, candidate_exit = candidate_tests.CANDIDATE_RELAY.rsplit("sys.exit(0)", 1)
        full_source, full_exit = seal_tests.FULL_FAKE_RELAY.rsplit("sys.exit(0)", 1)
        self.assertFalse(candidate_exit.strip())
        self.assertFalse(full_exit.strip())
        envelope = '''
candidate_envelope = manifest
'''
        merge = '''
manifest["side"] = candidate_envelope["side"]
manifest["candidate_gateway"] = candidate_envelope["candidate_gateway"]
manifest["upstream"].update(candidate_envelope["upstream"])
with open(os.path.join(output, "relay-manifest.json"), "w", encoding="utf-8") as handle:
    json.dump(manifest, handle)
'''
        relay = self.tool_copy / "claude_fw_e_relay.py"
        relay.write_text(candidate_source + envelope + full_source + merge + recovery_tests._RELAY_EPILOGUE, encoding="utf-8")
        relay.chmod(0o700)

    def source_campaign(self) -> Path:
        """冻结两个候选 Job 及镜像官方 Job；Store 的官方制品、身份条件和 bootstrap 沿用 seal 来源。"""

        payload = candidate_tests._payload()
        labels = json.loads(plan.evidence_labels_path(seal_tests.TARGET).read_text(encoding="utf-8"))
        official_ids = {job["id"] for job in payload["capture_jobs"] if job["phase"] == "official"}
        labels["entries"] = [entry for entry in labels["entries"] if entry["job_id"] in official_ids]
        campaign = seal_tests._SealCampaignCase.build_campaign(self, inputs=(payload, labels))
        catalog = seal_tests._write_json(campaign / "inputs" / "active-release-catalog.json", candidate_tests.ACTIVE_CATALOG)
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        seal_tests.rewrite_manifest(
            campaign,
            configuration={**manifest["configuration"], **candidate_tests.CANDIDATE_CONFIGURATION},
            inputs={**manifest["inputs"], "active_release_catalog": {
                "path": catalog.relative_to(campaign).as_posix(), "sha256": claude_upgrade.file_sha256(catalog),
            }},
        )
        seal_tests._write_json(campaign / "official" / "result.json", {
            "attempt": {"path": "official/attempts/sealed-elsewhere/attempt.json"},
        })
        return campaign

    def build_campaign(self) -> Path:
        """供 RecoveryChainCase 的总账／Store 建立器调用来源接口。"""

        return self.source_campaign()

    @contextlib.contextmanager
    def source_context(self) -> Iterator[None]:
        """仅安装已有来源替身；不替换对账器、批准消费、恢复计划、工具演进或任何账本写入。"""

        original_stage = claude_upgrade._load_stage_result

        def stage(campaign_dir: Path, name: str, candidate_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
            if name == "capture-official":
                return json.loads((Path(campaign_dir) / "official" / "result.json").read_text(encoding="utf-8"))
            return original_stage(campaign_dir, name, candidate_id, **kwargs)

        with self.candidate_patched(), mock.patch.object(claude_upgrade, "_load_stage_result", stage), mock.patch.dict(os.environ, {
                    "FAKE_RELAY_SEQUENCES": str(seal_tests.SEQUENCES),
                    "FAKE_RELAY_MUTATIONS": str(self.mutations_file),
                    "FAKE_RELAY_FAILURES": str(self.failures_file),
                    "FAKE_RELAY_CALL_LOG": str(self.call_log),
                }):
            yield

    def chain_patched(self) -> contextlib.AbstractContextManager[None]:
        """兼容既有恢复辅助方法的来源上下文名称。"""

        return self.source_context()

    def candidate_world(self) -> Path:
        """建立已冻结来源、真实 Store 和两本账，并冻结候选 Job 供请求来源核算读取。"""

        campaign = self.build_recovery_campaign()
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.source_context():
            # 与 plan 一致：冻结未带 candidate-id 的模板摘要，实际取证路径由正式运行展开并写入 Job 收据。
            jobs = capture._expand_phase_jobs(campaign, manifest, "candidate", candidate_id=None)
        seal_tests.rewrite_manifest(campaign, jobs=[*manifest["jobs"], *[
            {"id": job.job_id, "phase": "candidate", "execution_sha256": capture._job_execution_sha256(job),
             "evidence_roots": list(job.evidence_roots)} for job in jobs
        ]])
        # 输入夹具的阶段起点：前序证据已封存，候选构建完成。真实 append_event 校验每次状态迁移。
        for number in range(1, 4):
            phase = f"VC-{number}"
            if number > 1:
                timing.append_event(self.ledger_dir, event_id=f"fixture-vc{number}-started", phase=phase,
                                    event_type="stage_started", next_action="连续链输入阶段开始", receipts=[])
            timing.append_event(self.ledger_dir, event_id=f"fixture-vc{number}-completed", phase=phase,
                                event_type="stage_completed", next_action="连续链输入阶段完成", receipts=[])
        binding = seal_tests._write_json(campaign / "inputs" / "candidate-chain-source.json", {
            "candidate_id": CANDIDATE, "candidate_revision": 1, "source": "CandidateFixture.fake_admission",
        })
        timing.append_event(self.ledger_dir, event_id="fixture-candidate-r1", phase="VC-4", event_type="stage_revision",
                            revision=1, candidate_id=CANDIDATE, revision_commit_sha256=claude_upgrade.file_sha256(binding),
                            next_action="连续链输入候选 r1", receipts=[])
        for phase, event in (("VC-4", "stage_started"), ("VC-4", "stage_completed"), ("VC-5", "stage_started")):
            timing.append_event(self.ledger_dir, event_id=f"fixture-{phase.lower()}-{event}", phase=phase,
                                event_type=event, next_action="连续链候选采集起点", receipts=[])
        return campaign

    def candidate_run(self, campaign: Path, *, fail_refresh: bool = False) -> dict[str, Any]:
        """真实候选首批；故障在中继写完请求字节之后触发，修复只清空既有失败表。"""

        self.fail_probe(REFRESH_PROBE, 1 if fail_refresh else 0)
        with self.source_context(), mock.patch.object(claude_upgrade, "JOB_RETRY_LIMIT", 0):
            code, result, stderr = self.run_main([
                "capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE,
                "--acknowledge-live-requests",
            ])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "failed" if fail_refresh else "awaiting_receipts"), stderr)
        self.fail_probe(REFRESH_PROBE, 0)
        return result

    def candidate_reconcile(self, campaign: Path, attempt_id: str, *extra: str) -> dict[str, Any]:
        """经正式 CLI 对账候选；作用域从真实预约确定，不伪造恢复预览。"""

        return self.reconcile(campaign, attempt_id, *extra)

    def candidate_approve(self, campaign: Path, attempt_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        """经正式对账入口发布恢复批准；授权消费留给真实 resume。"""

        return self.approve(campaign, attempt_id, preview)

    def candidate_resume(self, campaign: Path, preview_path: str) -> dict[str, Any]:
        """真实消费恢复批准，复算闭集，生成后继候选 attempt。"""

        with self.source_context():
            code, result, stderr = self.run_main([
                "resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--rerun-failed",
                "--recovery-preview", preview_path, "--acknowledge-live-requests",
            ])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        return result

    def candidate_attempt(self, campaign: Path, attempt_id: str) -> tuple[Path, dict[str, Any]]:
        """完整重放候选 attempt（包含权限边界），让来源收据与续跑产物走同一正式读侧。"""

        with self.source_context():
            return claude_upgrade._load_capture_attempt(campaign, "candidate", CANDIDATE, attempt_id)

    def candidate_job_roots(self, campaign: Path) -> dict[str, Path]:
        """从冻结总计划展开候选 Job 的真实宿主证据根。"""

        manifest = claude_upgrade.load_campaign_manifest(campaign)
        with self.source_context():
            layout = capture._capture_layout(campaign, manifest)
            return {job.job_id: capture._host_path(job.evidence_roots[0], layout)
                    for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}
