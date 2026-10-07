"""Claude 后继候选复用（Claude 升级控制面设计 B4-01 c3a：恢复侧、未封存来源）。

沿用 c1 候选续跑的夹具（``test_claude_upgrade_candidate_recovery.CandidateRecoveryFixture``：宿主脚本替身、候选中继替身、
网关事实与激活事实替身、正式入口门替身、官方已封存），在同一个宿主数据根里建两个 Formal Campaign：前序 P 先跑出一个等待
封存的候选 attempt；后继 S 的总计划冻结指向 P 的只读复用前序（P 的 Store 身份经可替换的读取函数取得），再在 S 上走
``resume --candidate-reuse-source-*`` 的预览与批准。两个 Campaign 的入口门替身给出同一个候选构建（身份子集相等）。

覆盖：预览零写入零请求、批准后全部复用零请求（复用结果改绑执行摘要、``reused_from.campaign``／``producer.campaign``、
证据根仍指向前序、复用来源收据、无连续性核对）、执行定义或工具依赖变化的 Job 进执行集合并核连续性、批准摘要漂移与
各条拒绝（同 Campaign 跨 revision、没冻结前序、前序 ID 或 Store 身份不符、来源不是唯一待封存、已封存来源待 c3b、身份
不同、已有 attempt、缺参数、执行集合非空未确认真实请求）、复用 attempt 之后的承接链读法；以及项目总账跨 Campaign 的
重复请求身份键不计数。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_candidate_reuse as candidate_reuse
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_recovery as recovery
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.tests import test_claude_upgrade_candidate_capture as candidate_tests
from tools.official_client_capture.tests import test_claude_upgrade_candidate_recovery as recovery_tests
from tools.official_client_capture.tests import test_codex_upgrade_project_ledger as ledger_tests

CANDIDATE = candidate_tests.CANDIDATE
LIVE_JOB = candidate_tests.LIVE_JOB
REFRESH_JOB = candidate_tests.REFRESH_JOB
REFRESH_PROBE = recovery_tests.REFRESH_PROBE
PREDECESSOR = "claude-code-2_1_285-p1"
SUCCESSOR = "claude-code-2_1_285-s1"
STORE_IDENTITY = "a" * 64


class CandidateReuseFixture(recovery_tests.CandidateRecoveryFixture):
    """两个 Campaign：前序 P（候选 attempt 等待封存）与冻结指向 P 的后继 S。"""

    def setUp(self) -> None:
        super().setUp()
        self.store_identity = STORE_IDENTITY
        self.identity_overrides: dict[str, Any] = {}

    def fake_admission(self, campaign_dir, manifest, candidate_id, *, deadline=None, heartbeat=None):
        admission = super().fake_admission(campaign_dir, manifest, candidate_id, deadline=deadline, heartbeat=heartbeat)
        facts = dict(admission.facts)
        if Path(campaign_dir).name == SUCCESSOR:
            # 后继 Campaign 自己的 VC-4：构建收据与 Store 引用是本 Campaign 的，构建 ID 标签也可以不同。
            facts["build_receipt"] = {"path": f"candidates/{candidate_id}/build-receipt.json", "sha256": "9" * 64, "receipt_digest": "9" * 64}
            facts["candidate_frozen_ref"] = {"fact_id": "successor"}
            facts["build_id"] = "claude-build-successor"
            facts.update(self.identity_overrides)
        return candidate_capture.CandidateAdmission(candidate_id, admission.manifest, admission.variables, facts, persona=admission.persona)

    @contextlib.contextmanager
    def reuse_patched(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.recovery_patched())
            stack.enter_context(mock.patch.object(candidate_reuse, "_source_store_identity", lambda _dir, _id: self.store_identity))
            yield

    def predecessor(self) -> tuple[Path, str]:
        campaign = self.write_candidate_campaign(campaign_id=PREDECESSOR)
        with self.recovery_patched():
            _code, result, stderr = self.run_main(
                ["capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--acknowledge-live-requests"]
            )
        self.assertEqual((result or {}).get("status"), "awaiting_receipts", stderr)
        return campaign, str(result["attempt_id"])

    def successor(self, *, predecessor: dict[str, Any] | None = None) -> Path:
        campaign = self.write_candidate_campaign(campaign_id=SUCCESSOR)
        manifest = json.loads((campaign / "campaign.json").read_text(encoding="utf-8"))
        manifest["predecessor"] = predecessor if predecessor is not None else {
            "mode": "read_only_reuse", "campaign_id": PREDECESSOR, "campaign_identity_sha256": STORE_IDENTITY,
        }
        raw = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        (campaign / "campaign.json").write_bytes(raw)
        (campaign / "campaign.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="utf-8")
        (campaign / "candidates" / CANDIDATE).mkdir(parents=True)
        return campaign

    def reuse_arguments(self, source: Path, attempt_id: str, *extra: str, candidate: str = CANDIDATE) -> list[str]:
        return [
            "--rerun-failed", "--candidate-reuse-source-campaign-dir", str(source),
            "--candidate-reuse-source-candidate-id", candidate, "--candidate-reuse-source-attempt-id", attempt_id, *extra,
        ]

    def resume_reuse(self, campaign: Path, source: Path, attempt_id: str, *extra: str) -> tuple[int, dict | None, str]:
        with self.reuse_patched():
            return self.run_main(["resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, *self.reuse_arguments(source, attempt_id, *extra)])

    def preview(self, campaign: Path, source: Path, attempt_id: str) -> dict[str, Any]:
        code, result, stderr = self.resume_reuse(campaign, source, attempt_id, "--preview-recovery")
        self.assertEqual(code, 0, stderr)
        assert result is not None
        return result


class ReuseAllTests(CandidateReuseFixture):
    def test_preview_then_approve_reuses_everything_with_zero_requests(self) -> None:
        source, source_attempt = self.predecessor()
        campaign = self.successor()
        calls = len(self.candidate_calls())
        attempts_root = campaign / "candidates" / CANDIDATE / "attempts"
        preview = self.preview(campaign, source, source_attempt)
        self.assertEqual(preview["schema_version"], candidate_reuse.PREVIEW_SCHEMA)
        self.assertEqual((preview["reuse_job_ids"], preview["execute_job_ids"], preview["not_reusable"]), (sorted([LIVE_JOB, REFRESH_JOB]), [], []))
        self.assertEqual((preview["continuity_required"], preview["live_request_count"]), (False, 0))
        self.assertEqual(preview["source_campaign"]["campaign_id"], PREDECESSOR)
        self.assertIn("--approve-candidate-reuse-sha256 " + preview["preview_sha256"], preview["next_command"])
        self.assertFalse(attempts_root.exists())
        # 预览确定：同一现场重算逐字相同。
        self.assertEqual(self.preview(campaign, source, source_attempt)["preview_sha256"], preview["preview_sha256"])

        code, result, stderr = self.resume_reuse(campaign, source, source_attempt, "--approve-candidate-reuse-sha256", preview["preview_sha256"])
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual(len(self.candidate_calls()), calls, "全部复用不发任何请求")
        self.assertEqual(result["recovery"]["source_campaign_id"], PREDECESSOR)
        root, attempt = self.load_attempt(campaign, result["attempt_id"])
        manifest = claude_upgrade.load_campaign_manifest(campaign)
        jobs = {job.job_id: job for job in claude_upgrade._campaign_jobs(campaign, manifest, "candidate", candidate_id=CANDIDATE)}
        _source_root, source_payload = self.load_source_attempt(source, source_attempt)
        source_results = {item["id"]: item for item in source_payload["results"]}
        for item in attempt["results"]:
            self.assertEqual((item["status"], item["disposition"], item["job_closure"]), ("complete", "reused", None))
            self.assertEqual(item["execution_sha256"], claude_upgrade._job_execution_sha256(jobs[item["id"]]))
            self.assertNotEqual(item["execution_sha256"], source_results[item["id"]]["execution_sha256"])
            self.assertEqual(item["evidence_roots"], source_results[item["id"]]["evidence_roots"])
            self.assertIn(PREDECESSOR, item["evidence_roots"][0])
            origin = item["reused_from"]
            self.assertEqual(origin["campaign"]["campaign_id"], PREDECESSOR)
            self.assertEqual(origin["producer"]["campaign"], origin["campaign"])
            self.assertEqual(origin["producer"]["execution_sha256"], source_results[item["id"]]["execution_sha256"])
            self.assertEqual(origin["rebased_from"]["execution_sha256"], source_results[item["id"]]["execution_sha256"])
            self.assertTrue((source / origin["attempt"]["path"]).is_file())
        self.assertIsNone(attempt["recovery"]["continuity"])
        receipt = candidate_reuse.load_reuse_source(root)
        assert receipt is not None
        self.assertEqual(receipt["preview"], {key: value for key, value in preview.items() if key != "next_command"})
        self.assertEqual(receipt["source_gateway"]["run_conditions"], source_payload["candidate_gateway"]["run_conditions"])
        self.assertEqual(attempt["recovery"]["source"]["path"], root.relative_to(campaign).joinpath(candidate_reuse.SOURCE_FILENAME).as_posix())
        # 复用 attempt 之后的承接链：同 Campaign 续跑按 producer.campaign 读到前序的闭环收据并逐项复核。
        with self.reuse_patched():
            layout = capture._capture_layout(campaign, manifest)
            for item in attempt["results"]:
                producer = recovery._producer(campaign, root, item)
                self.assertEqual(producer["campaign"]["campaign_id"], PREDECESSOR)
                recovery._verify_reusable_evidence(campaign, item, producer, layout, phase="candidate", candidate_id=CANDIDATE)
            # 前序总计划被改动：产出 Campaign 绑定失效，承接失败关闭。
            item = attempt["results"][0]
            tampered = json.loads(json.dumps(recovery._producer(campaign, root, item)))
            tampered["campaign"]["campaign_manifest_sha256"] = "0" * 64
            with self.assertRaisesRegex(ConfigurationError, "产出 Campaign 总计划"):
                recovery._verify_reusable_evidence(campaign, item, tampered, layout, phase="candidate", candidate_id=CANDIDATE)
        self.assertEqual(self.status(campaign)["status"], "candidate_awaiting_client_checkpoint")

    def load_source_attempt(self, source: Path, attempt_id: str) -> tuple[Path, dict]:
        with self.reuse_patched():
            return claude_upgrade._load_capture_attempt(source, "candidate", CANDIDATE, attempt_id, _replay_evidence_permissions=False)


class PartialReuseTests(CandidateReuseFixture):
    def changed_dependency(self):
        original = capture._job_tool_dependency_files

        def files(job, tool_identity):
            result = original(job, tool_identity)
            if job.job_id == REFRESH_JOB:
                result = {**result, "official_client_capture/claude_upgrade_plan.py": "0" * 64}
            return result

        return mock.patch.object(capture, "_job_tool_dependency_files", files)

    def test_changed_jobs_execute_with_continuity_and_live_acknowledgement(self) -> None:
        source, source_attempt = self.predecessor()
        campaign = self.successor()
        calls = len(self.candidate_calls())
        with self.changed_dependency():
            preview = self.preview(campaign, source, source_attempt)
            self.assertEqual((preview["reuse_job_ids"], preview["execute_job_ids"]), ([LIVE_JOB], [REFRESH_JOB]))
            self.assertEqual(preview["not_reusable"], [{"job_id": REFRESH_JOB, "reasons": ["tool-dependencies-changed"]}])
            self.assertTrue(preview["continuity_required"])
            code, _result, stderr = self.resume_reuse(campaign, source, source_attempt, "--approve-candidate-reuse-sha256", preview["preview_sha256"])
            self.assertEqual(code, 1)
            self.assertIn("--acknowledge-live-requests", stderr)
            self.assertFalse((campaign / "candidates" / CANDIDATE / "attempts").exists())
            code, result, stderr = self.resume_reuse(
                campaign, source, source_attempt, "--approve-candidate-reuse-sha256", preview["preview_sha256"], "--acknowledge-live-requests"
            )
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "awaiting_receipts"), stderr)
        self.assertEqual([call["probe"] for call in self.candidate_calls()[calls:]], [REFRESH_PROBE])
        _root, attempt = self.load_attempt(campaign, result["attempt_id"])
        self.assertEqual({item["id"]: item["disposition"] for item in attempt["results"]}, {LIVE_JOB: "reused", REFRESH_JOB: "executed"})
        continuity = attempt["recovery"]["continuity"]
        self.assertEqual(continuity["source_attempt_id"], source_attempt)
        self.assertEqual(continuity["gateway"]["compared_fields"], list(candidate_capture.GATEWAY_COMPARED_FIELDS))

    def test_continuity_drift_fails_before_any_job(self) -> None:
        source, source_attempt = self.predecessor()
        campaign = self.successor()
        calls = len(self.candidate_calls())
        with self.changed_dependency():
            preview = self.preview(campaign, source, source_attempt)
            drifted = json.loads(json.dumps(candidate_tests.BASE_GATEWAY))
            drifted["service"]["hosts_sha256"] = "e" * 64
            self.gateway_file.write_text(json.dumps(drifted), encoding="utf-8")
            code, result, stderr = self.resume_reuse(
                campaign, source, source_attempt, "--approve-candidate-reuse-sha256", preview["preview_sha256"], "--acknowledge-live-requests"
            )
        self.assertIsNotNone(result, stderr)
        self.assertEqual((code, result["status"]), (2, "failed"), stderr)
        self.assertEqual(result["execution_error"]["type"], recovery.CONTINUITY_DRIFT_ERROR_TYPE)
        self.assertEqual(len(self.candidate_calls()), calls)


class ReuseRefusalTests(CandidateReuseFixture):
    def assert_refused(self, campaign: Path, source: Path, attempt_id: str, message: str, *extra: str) -> None:
        code, _result, stderr = self.resume_reuse(campaign, source, attempt_id, *(extra or ("--preview-recovery",)))
        self.assertEqual(code, 1, stderr)
        self.assertIn(message, stderr)

    def test_refusals(self) -> None:
        source, source_attempt = self.predecessor()
        campaign = self.successor()
        # 同一 Campaign（换 revision 之后的旧候选）一律拒绝。
        self.assert_refused(source, source, source_attempt, "revision 已换构建，旧候选证据不复用")
        # 参数：缺来源、缺 --rerun-failed、预览与批准二选一、没给 --candidate-id。
        with self.reuse_patched():
            code, _result, stderr = self.run_main(
                ["resume", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--rerun-failed", "--preview-recovery",
                 "--candidate-reuse-source-campaign-dir", str(source)]
            )
        self.assertEqual(code, 1)
        self.assertIn("缺少显式来源参数", stderr)
        with self.reuse_patched():
            code, _result, stderr = self.run_main(["resume", "--campaign-dir", str(campaign), "--candidate-reuse-source-attempt-id", "x"])
        self.assertIn("必须同时给出 --candidate-id", stderr)
        self.assert_refused(campaign, source, source_attempt, "必须且只能给出", "--preview-recovery", "--approve-candidate-reuse-sha256", "1" * 64)
        # 前序 Store 身份与冻结绑定不符。
        self.store_identity = "b" * 64
        self.assert_refused(campaign, source, source_attempt, "Store 身份与冻结的前序绑定不一致")
        self.store_identity = STORE_IDENTITY
        # 候选身份子集不同（不是同一个候选构建）。
        self.identity_overrides = {"image_id": "sha256:" + "6" * 64}
        self.assert_refused(campaign, source, source_attempt, "候选身份与当前候选不同（['image_id']）")
        self.identity_overrides = {}
        # 批准摘要不是现场重算的预览。
        self.assert_refused(campaign, source, source_attempt, "批准摘要与现场重算的复用预览不一致", "--approve-candidate-reuse-sha256", "1" * 64)
        self.assertFalse((campaign / "candidates" / CANDIDATE / "attempts").exists())

    def test_predecessor_binding_and_source_state(self) -> None:
        source, source_attempt = self.predecessor()
        # 没冻结前序的 Campaign 不能事后挑选来源。
        unbound = self.write_candidate_campaign(campaign_id="claude-code-2_1_285-s2")
        (unbound / "candidates" / CANDIDATE).mkdir(parents=True)
        self.assert_refused(unbound, source, source_attempt, "没有冻结前序")
        # 冻结的前序是另一个 Campaign。
        other = self.successor(predecessor={"mode": "read_only_reuse", "campaign_id": "claude-code-other", "campaign_identity_sha256": STORE_IDENTITY})
        self.assert_refused(other, source, source_attempt, "不是当前 Campaign 冻结的前序")

    def test_source_must_be_unique_unsealed_awaiting(self) -> None:
        source, source_attempt = self.predecessor()
        campaign = self.successor()
        # 已封存的来源属于 c3b。
        result_path = candidate_capture.legacy_stage_result_path(source, CANDIDATE)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("{}", encoding="utf-8")
        self.assert_refused(campaign, source, source_attempt, "B4-01 c3b")
        result_path.unlink()
        # 来源有 seal 失败收据。
        failure = source / "candidates" / CANDIDATE / "attempts" / source_attempt / candidate_reuse.SEAL_FAILURE_FILENAME
        failure.write_text("{}", encoding="utf-8")
        self.assert_refused(campaign, source, source_attempt, "seal-failure.json")
        failure.unlink()
        # 来源不存在。
        self.assert_refused(campaign, source, "no-such-attempt", "")

    def test_failed_source_and_existing_attempt_refused(self) -> None:
        source = self.write_candidate_campaign(campaign_id=PREDECESSOR)
        failed = self.failed_attempt(source)
        campaign = self.successor()
        self.assert_refused(campaign, source, failed["attempt_id"], "必须是 awaiting_receipts")
        # 后继候选已有 attempt：复用只用于首个 attempt。
        with self.reuse_patched():
            self.run_main(["capture-candidate", "run", "--campaign-dir", str(campaign), "--candidate-id", CANDIDATE, "--acknowledge-live-requests"])
        self.assert_refused(campaign, source, failed["attempt_id"], "只用于还没有任何 attempt 的候选")


class CrossCampaignAccountingTests(unittest.TestCase):
    """复用证据的请求只入账一次：项目总账按身份键（证据根相对宿主数据根的路径＋原生坐标）去重，跨 Campaign 同样不计数。"""

    def test_reused_identity_keys_are_not_counted_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger_root = ledger_tests._create(root, initial_identity_keys=[], initial_estimated_count=0)
            predecessor = ledger_tests._campaign(root, "p1", target_version="2.1.285")
            successor = ledger_tests._campaign(root, "s1", target_version="2.1.285")
            ledger_tests._register(root, predecessor, "p1", target_version="2.1.285")
            ledger_tests._reconciliation(predecessor, operation_id="p1-candidate", keys=["candidate-k1", "candidate-k2"])
            project_ledger.reconcile_project_ledger(ledger_root, campaign_dir=predecessor)
            ledger_tests._register(root, successor, "s1", target_version="2.1.285")
            # 后继复用了两个 Job 的证据（同样的身份键），另执行了一个 Job（新身份键）。
            ledger_tests._reconciliation(successor, operation_id="s1-candidate", keys=["candidate-k1", "candidate-k2", "candidate-k3"])
            project_ledger.reconcile_project_ledger(ledger_root, campaign_dir=successor)
            head = project_ledger.replay_head(ledger_root)
            self.assertEqual(head["precise_total"], 3)
            self.assertEqual(sorted(item["identity_key"] for item in head["duplicate_identity_keys"]), ["candidate-k1", "candidate-k2"])


if __name__ == "__main__":
    unittest.main()
