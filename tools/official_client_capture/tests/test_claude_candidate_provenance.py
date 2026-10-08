"""候选恢复报告的请求核算边界：完整绑定可识别，伪造、缺绑定和额外内容继续未决。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import incremental_recovery as incremental
from tools.official_client_capture.tests import test_claude_upgrade_candidate_reuse as reuse_tests
from tools.official_client_capture.tests.test_claude_upgrade_live_request_provenance import _messages, _relay_root, _write


class CandidateRestorationAccountingTests(unittest.TestCase):
    CAMPAIGN_ID = "claude-candidate-provenance"
    CANDIDATE_ID = "candidate-r1"
    JOB_ID = "candidate-job"
    PROBE_ID = "v4-a"

    def _fixture(self, *, failed: bool = False, recovery: bool = False) -> tuple[Path, Path, Path, Path, dict, dict]:
        """按生产目录与收据合同构造一个候选 Job，容器路径必须经过真实宿主映射。"""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        data = Path(directory.name).resolve() / "data"
        campaign = data / "evidence" / "campaigns" / self.CAMPAIGN_ID
        run_prefix = f"{self.CAMPAIGN_ID}-{self.CANDIDATE_ID}"
        job_name = self.JOB_ID + (".failed-attempt1" if failed else "")
        relative = Path("runs") / "claude-candidate" / run_prefix / job_name
        root, logical_root = data / relative, Path("/capture") / relative
        _relay_root(root / self.PROBE_ID, {1: _messages()})
        report_path = root / "candidate-gateway" / f"{self.PROBE_ID}.json"
        report = {
            "schema_version": "claude-candidate-gateway-restoration/v1", "run_id": f"{run_prefix}-{self.PROBE_ID}",
            "probe_id": self.PROBE_ID, "account_id": 7, "api_key_id": 9,
            "status": "capture_failed" if failed else "complete", "capture_exit_code": 6 if failed else 0,
            "account_proxy_restored_equal": True, "account_gate_restored_equal": True,
            "hosts_sha256_equal": True, "ca_bundle_sha256_equal": True, "runtime_secrets_removed": True,
        }
        _write(report_path, json.dumps(report))
        result = {
            "id": self.JOB_ID, "phase": "candidate", "mode": "formal", "evidence_roots": [str(logical_root)],
            "steps": [{"probe_id": self.PROBE_ID, "return_code": report["capture_exit_code"], "argv": [
                "python3", "claude_fw_e_relay.py", "capture", "--output-root", str(logical_root / self.PROBE_ID),
                "--run-id", report["run_id"], "--probe-id", self.PROBE_ID,
            ]}],
            "restoration_checks": [{"probe_id": self.PROBE_ID, "status": "passed", "report": {
                "path": str(logical_root / "candidate-gateway" / report_path.name),
                "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            }}],
        }
        attempt = campaign / "candidates" / self.CANDIDATE_ID / "attempts" / "a1"
        _write(attempt / "reservation.json", "{}")
        if recovery:
            attempt = attempt / "recovery" / "ar1"
            _write(attempt / "recovery-reservation.json", "{}")
        job_path = attempt / f"job-{self.JOB_ID}.json"
        _write(job_path, json.dumps(result))
        manifest = {
            "schema_version": provenance.CAMPAIGN_SCHEMA, "client": "claude", "campaign_id": self.CAMPAIGN_ID,
            "campaign_mode": "formal", "configuration": {
                "capture_root": "/capture", "claude_account_id": "7", "api_key_id": "9",
            },
            "jobs": [{"id": self.JOB_ID, "phase": "candidate", "evidence_roots": []}],
        }
        self._manifest(campaign, manifest)
        return campaign, root, job_path, report_path, report, result

    def _manifest(self, campaign: Path, manifest: dict) -> None:
        raw = json.dumps(manifest).encode()
        _write(campaign / "campaign.json", raw)
        _write(campaign / "campaign.sha256", hashlib.sha256(raw).hexdigest() + "\n")

    def _collect(self, campaign: Path) -> dict:
        return provenance.collect_campaign_provenance(campaign, formal_campaign_id=campaign.name)

    def test_bound_restoration_reports_preserve_candidate_accounting(self) -> None:
        self.assertEqual(provenance.CANDIDATE_RESTORATION_DIRNAME, candidate_capture.RESTORATION_REPORT_DIRNAME)
        self.assertEqual(provenance.CANDIDATE_RESTORATION_SCHEMA, candidate_capture.RESTORATION_REPORT_SCHEMA)
        self.assertEqual(provenance.CANDIDATE_RESTORATION_EQUAL_FIELDS, candidate_capture.RESTORATION_EQUAL_FIELDS)
        for failed, recovery in ((False, False), (True, False)):
            with self.subTest(failed=failed, recovery=recovery):
                campaign, root, _job, _path, _report, _result = self._fixture(failed=failed, recovery=recovery)
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]), ("complete", 1, []))
                # 离线核算没有收据上下文，不能凭同名目录或报告中的自述放行。
                offline = provenance.collect_root_provenance([root])
                self.assertEqual((offline["precise_total"], offline["unresolved_roots"]), (1, [str(root)]))

    def test_recovery_directory_without_reservation_authority_stays_unresolved(self) -> None:
        campaign, _root, _job, _path, _report, _result = self._fixture(recovery=True)
        receipt = self._collect(campaign)
        self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]),
                         ("accounting_unresolved", 1, [self.JOB_ID]))

    def test_forged_incomplete_or_unknown_restoration_evidence_stays_unresolved(self) -> None:
        cases = (
            "未绑定", "坏摘要", "错目录绑定", "错步骤目录", "错候选步骤", "错候选报告", "错账号", "错密钥",
            "错合同", "错探针", "错状态", "错退出码", "恢复未通过", "恢复项为假", "损坏报告", "未知文件",
            "额外报告", "嵌套目录", "伪装运行根", "符号链接文件", "错阶段", "错候选收据目录", "官方 Job",
        )
        for case in cases:
            with self.subTest(case=case):
                campaign, root, job, path, report, result = self._fixture()
                check = result["restoration_checks"][0]
                if case == "未绑定":
                    result.pop("restoration_checks")
                elif case == "错目录绑定":
                    check["report"]["path"] = str(root / "other" / path.name)
                elif case == "错步骤目录":
                    result["steps"][0]["argv"][4] = str(root / "other")
                elif case == "错候选步骤":
                    result["steps"][0]["argv"][6] = "other-candidate-v4-a"
                elif case == "恢复未通过":
                    check["status"] = "failed"
                elif case == "错阶段":
                    result["phase"] = "official"
                elif case == "错候选收据目录":
                    candidate = job.parent.parent.parent
                    candidate.rename(candidate.with_name("other-candidate"))
                    job = candidate.with_name("other-candidate") / "attempts" / "a1" / job.name
                elif case == "官方 Job":
                    official = campaign / "official" / "attempts" / "a1"
                    official.parent.mkdir(parents=True)
                    job.parent.rename(official)
                    job = official / job.name
                    manifest = json.loads((campaign / "campaign.json").read_bytes())
                    manifest["jobs"][0]["phase"] = "official"
                    self._manifest(campaign, manifest)
                else:
                    change = {
                        "错候选报告": ("run_id", "other-candidate-v4-a"), "错账号": ("account_id", 8),
                        "错密钥": ("api_key_id", 10), "错合同": ("schema_version", "forged/v1"),
                        "错探针": ("probe_id", "v4-b"), "错状态": ("status", "restoration_failed"),
                        "错退出码": ("capture_exit_code", 1), "恢复项为假": ("runtime_secrets_removed", False),
                    }.get(case)
                    if change is not None:
                        report[change[0]] = change[1]
                _write(path, "非 JSON" if case == "损坏报告" else json.dumps(report))
                check["report"]["sha256"] = "0" * 64 if case == "坏摘要" else hashlib.sha256(path.read_bytes()).hexdigest()
                if case in {"未知文件", "额外报告", "嵌套目录"}:
                    extra = {"未知文件": "notes.txt", "额外报告": "v4-b.json", "嵌套目录": "nested/report.json"}[case]
                    _write(path.parent / extra, "{}")
                elif case == "伪装运行根":
                    _relay_root(path.parent, {1: _messages()})
                elif case == "符号链接文件":
                    target = path.with_name("target.json")
                    path.rename(target)
                    path.symlink_to(target)
                _write(job, json.dumps(result))
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]),
                                 ("accounting_unresolved", 1, [self.JOB_ID]))

    def test_bindings_do_not_authorize_an_unbound_failed_archive(self) -> None:
        campaign, root, _job, _path, _report, _result = self._fixture()
        archive = root.with_name(root.name + ".failed-attempt1")
        _relay_root(archive / self.PROBE_ID, {1: _messages()})
        _write(archive / "candidate-gateway" / f"{self.PROBE_ID}.json", "{}")
        receipt = self._collect(campaign)
        self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]),
                         ("accounting_unresolved", 2, [self.JOB_ID]))

    def _cross_campaign_fixture(self) -> tuple[Path, Path, Path, Path, Path, dict]:
        """先真实收口来源证据，再按 c3 的原样保留合同建立后继 Job 收据，不替换任何核验函数。"""

        source, root, source_job, report, _payload, result = self._fixture()
        data = source.parent.parent.parent
        job_root = source_job.parent / "evidence" / "jobs" / self.JOB_ID
        job_root.mkdir(parents=True, mode=0o700)
        for name in ("evidence", "logs"):
            (job_root / name).mkdir(parents=True, mode=0o700)
        permissions = provenance.evidence_permissions
        path, _receipt = permissions.close_evidence_permissions(
            job_root, [job_root / "evidence", job_root / "logs", root],
            managed_data_root=data, logical_runs_roots=(data / "runs",),
        )
        binding = permissions.receipt_binding(job_root, path)
        closure = {
            "schema_version": "claude-upgrade-job-closure/v1", "client": "claude", "mode": "formal",
            "campaign_id": source.name, "attempt_id": "a1", "job_id": self.JOB_ID, "execution_sha256": "a" * 64,
            "status": "complete", "environment_contaminated": False, "evidence_roots": result["evidence_roots"],
            "host_evidence_roots": [str(root)], "evidence_permission_closeout": {
                **binding, "path": path.relative_to(source_job.parent).as_posix(),
            },
        }
        closure_path = job_root / "job-closure.json"
        _write(closure_path, json.dumps(closure))
        closure_binding = {"path": closure_path.relative_to(source).as_posix(),
                           "sha256": hashlib.sha256(closure_path.read_bytes()).hexdigest()}
        result.update(status="complete", disposition="executed", execution_sha256="a" * 64, job_closure={
            **closure_binding, "path": closure_path.relative_to(source_job.parent).as_posix(),
        })
        _write(source_job, json.dumps(result))
        origin = {"campaign_dir": str(source), "campaign_id": source.name, "candidate_id": self.CANDIDATE_ID,
                  "campaign_manifest_sha256": hashlib.sha256((source / "campaign.json").read_bytes()).hexdigest()}
        campaign = source.with_name("claude-successor")
        manifest = json.loads((source / "campaign.json").read_bytes())
        manifest["campaign_id"] = campaign.name
        self._manifest(campaign, manifest)
        result = json.loads(json.dumps(result))
        result.update(disposition="reused", execution_sha256="b" * 64, job_closure=None, reused_from={
            "campaign": origin, "job_receipt": {"path": source_job.relative_to(source).as_posix(),
                                                 "sha256": hashlib.sha256(source_job.read_bytes()).hexdigest()},
            "producer": {"campaign": origin, "attempt_id": "a1", "execution_sha256": "a" * 64,
                         "job_closure": closure_binding},
        })
        job = campaign / "candidates" / "candidate-r2" / "attempts" / "a2" / f"job-{self.JOB_ID}.json"
        _write(job.parent / "reservation.json", "{}")
        _write(job, json.dumps(result))
        return campaign, job, source, report, closure_path, result

    def test_cross_campaign_original_producer_and_later_reuse_are_verified(self) -> None:
        campaign, job, _source, _report, _closure, result = self._cross_campaign_fixture()
        self.assertEqual((self._collect(campaign)["status"], self._collect(campaign)["precise_total"]), ("complete", 1))
        # 同 Campaign 再续跑：即时来源已经是复用结果，原生产者仍是前序 Campaign。
        again = json.loads(json.dumps(result))
        again["reused_from"] = {
            "producer": result["reused_from"]["producer"],
            "job_receipt": {"path": job.relative_to(campaign).as_posix(), "sha256": hashlib.sha256(job.read_bytes()).hexdigest()},
        }
        successor = job.parent.with_name("a3")
        _write(successor / "reservation.json", "{}")
        _write(successor / job.name, json.dumps(again))
        receipt = self._collect(campaign)
        self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]), ("complete", 1, []))

    def test_cross_campaign_forged_or_drifted_producer_stays_unresolved(self) -> None:
        for case in ("来源身份", "总计划摘要", "闭环摘要", "闭环路径", "执行摘要", "源收据摘要", "缺源收据", "源报告篡改", "闭环范围"):
            with self.subTest(case=case):
                campaign, job, source, report, closure, result = self._cross_campaign_fixture()
                producer = result["reused_from"]["producer"]
                if case == "来源身份":
                    producer["campaign"]["candidate_id"] = "forged-candidate"
                elif case == "总计划摘要":
                    producer["campaign"]["campaign_manifest_sha256"] = "0" * 64
                elif case == "闭环摘要":
                    producer["job_closure"]["sha256"] = "0" * 64
                elif case == "闭环路径":
                    producer["job_closure"]["path"] = "forged/job-closure.json"
                elif case == "执行摘要":
                    producer["execution_sha256"] = "0" * 64
                elif case == "源收据摘要":
                    result["reused_from"]["job_receipt"]["sha256"] = "0" * 64
                elif case == "缺源收据":
                    (source / result["reused_from"]["job_receipt"]["path"]).unlink()
                elif case == "源报告篡改":
                    report.chmod(0o600)
                    _write(report, "{}")
                elif case == "闭环范围":
                    payload = json.loads(closure.read_bytes())
                    payload["host_evidence_roots"] = []
                    _write(closure, json.dumps(payload))
                    producer["job_closure"]["sha256"] = hashlib.sha256(closure.read_bytes()).hexdigest()
                _write(job, json.dumps(result))
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]),
                                 ("accounting_unresolved", 1, [self.JOB_ID]))


class RecoverySegmentRestorationAccountingTests(unittest.TestCase):
    """合成已发布合同制品并使用真实验证器、checkpoint 存储和请求解析；不模拟 provenance。"""

    CAMPAIGN_ID = CandidateRestorationAccountingTests.CAMPAIGN_ID
    CANDIDATE_ID = CandidateRestorationAccountingTests.CANDIDATE_ID
    JOB_ID = CandidateRestorationAccountingTests.JOB_ID
    PROBE_ID = CandidateRestorationAccountingTests.PROBE_ID
    _fixture = CandidateRestorationAccountingTests._fixture
    _manifest = CandidateRestorationAccountingTests._manifest
    _collect = CandidateRestorationAccountingTests._collect

    @staticmethod
    def _json(path: Path, value: dict, digest: str | None = None) -> dict:
        if digest is not None:
            value[digest] = incremental.digest({key: item for key, item in value.items() if key != digest})
        _write(path, json.dumps(value, ensure_ascii=False, sort_keys=True))
        return value

    @staticmethod
    def _binding(campaign: Path, path: Path) -> dict:
        raw = path.read_bytes()
        return {"path": path.relative_to(campaign).as_posix(), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def _publish(self, campaign: Path, segment: Path, reservation: dict, result: dict, *, summary: bool = False) -> None:
        self._json(segment / "recovery-reservation.json", reservation, "reservation_digest")
        self._json(segment / f"job-{self.JOB_ID}.json", result)
        incremental.CheckpointStore(segment / "checkpoints").append({
            "checkpoint_schema_version": "claude-upgrade-job-checkpoint/v1", "client": "claude", "mode": "formal",
            "phase": "candidate", "campaign_id": campaign.name, "attempt_id": f"a1.{segment.name}",
            "run_nonce": reservation["run_nonce"], "item_id": self.JOB_ID,
            "status": "complete" if result["status"] == "complete" else "failed", "disposition": result["disposition"],
            "result": result, "result_sha256": incremental.digest(result), "previous_checkpoint_sha256": None,
        })
        if summary:
            attempt = json.loads((segment.parent.parent / "attempt.json").read_bytes())
            self._json(segment / "attempt-recovery.json", {
                "schema_version": "claude-upgrade-attempt-recovery/v1",
                **{key: reservation[key] for key in ("campaign_id", "candidate_id", "attempt_id", "recovery_revision",
                                                     "evaluation_baseline", "baseline_commit_sha256", "run_nonce")},
                "identity": attempt["identity"], "reservation": self._binding(campaign, segment / "recovery-reservation.json"),
                "results": [result],
            }, "attempt_recovery_digest")

    def _recovery_fixture(self, *, summary: bool = False, failed: bool = False) -> tuple[Path, Path, dict, dict]:
        campaign, root, job, _report, _report_value, original_result = self._fixture()
        manifest = json.loads((campaign / "campaign.json").read_bytes())
        manifest["campaign_purpose"] = "validation_only"
        self._manifest(campaign, manifest)
        identity = {"candidate_id": self.CANDIDATE_ID, "git_commit": "a" * 40}
        common = {
            "client": "claude", "mode": "formal", "phase": "candidate", "campaign_id": campaign.name,
            "campaign_mode": "formal", "campaign_purpose": "validation_only", "candidate_purpose": "validation_only",
            "campaign_manifest_sha256": hashlib.sha256((campaign / "campaign.json").read_bytes()).hexdigest(),
            "candidate_id": self.CANDIDATE_ID, "attempt_id": "a1", "run_nonce": "a" * 64,
        }
        original_reservation = self._json(job.parent / "reservation.json", {
            **common, "schema_version": "claude-upgrade-capture-reservation/v1", "identity_sha256": incremental.digest(identity),
            "planned_jobs": [{"id": self.JOB_ID, "required": True, "execution_sha256": "1" * 64}],
        }, "reservation_digest")
        original_result.update(status="complete", disposition="executed", execution_sha256="1" * 64)
        self._json(job, original_result)
        original_attempt = self._json(job.parent / "attempt.json", {
            **common, "schema_version": "claude-upgrade-capture-attempt/v1", "identity": identity,
            "reservation": self._binding(campaign, job.parent / "reservation.json"), "results": [original_result],
        }, "attempt_digest")
        digests = {field: "d" * 64 for field in artifacts.EVALUATOR_DIGEST_FIELDS}
        recovery = artifacts.build_evaluation_recovery(
            campaign_id=campaign.name, candidate_id=self.CANDIDATE_ID, candidate_revision=1, evaluation_baseline=1,
            kind="attempt-recovery", diagnosis={"path": "control/diagnosis.json", "sha256": "d" * 64},
            failure_source="assertion-failed", reuse_authority="anchored", root_cause_class="transient-environment",
            root_cause_id="rc1-" + "f" * 20, failed_step="SPEC-HDR-001", previous_baseline=0,
            previous_baseline_commit_sha256=None, execute_rules=["SPEC-HDR-001"], reuse_rules=[],
            execute_jobs=[self.JOB_ID], reuse_jobs=[], attempt_id="a1", recovery_revision="ar1",
            fix_commit=None, deployment_receipt=None, evaluation_epoch=None, failed_evaluator_digests=digests,
            current_evaluator_digests=digests, reviewer="段来源核验", approved_at_utc="2026-10-08T00:00:00Z",
        )
        baseline = campaign / "candidates" / self.CANDIDATE_ID / "revisions" / "b1"
        self._json(baseline / "recovery.json", recovery)
        commit = artifacts.build_evaluation_baseline_commit(
            campaign_id=campaign.name, candidate_id=self.CANDIDATE_ID, candidate_revision=1, evaluation_baseline=1,
            kind="attempt-recovery", recovery_sha256=recovery["recovery_sha256"], authorization_sha256="e" * 64,
            stage_sources={stage: {"source": "local", "target": f"candidates/{self.CANDIDATE_ID}/revisions/b1/{stage}/result.json"}
                           for stage in artifacts.EVALUATION_STAGES}, committed_at_utc="2026-10-08T00:00:00Z",
        )
        self._json(baseline / "COMMIT", commit)
        new_root = root.with_name(root.name + "-recovery-ar1")
        shutil.copytree(root, new_root)
        logical = original_result["evidence_roots"][0]
        result = json.loads(json.dumps(original_result).replace(logical, logical + "-recovery-ar1"))
        result["execution_sha256"] = "2" * 64
        if failed:
            result["status"] = "failed"
        segment = job.parent / "recovery" / "ar1"
        reservation = {
            "schema_version": "claude-upgrade-attempt-recovery-reservation/v1",
            **{key: value for key, value in common.items() if key not in {"client", "mode", "run_nonce"}},
            "recovery_revision": "ar1", "evaluation_baseline": 1, "baseline_commit_sha256": commit["commit_sha256"],
            "recovery_sha256": recovery["recovery_sha256"], "run_nonce": "b" * 64, "identity_sha256": incremental.digest(identity),
            "original_reservation": {**self._binding(campaign, job.parent / "reservation.json"), "run_nonce": original_reservation["run_nonce"]},
            "original_attempt": {**self._binding(campaign, job.parent / "attempt.json"), "attempt_digest": original_attempt["attempt_digest"]},
            "planned_jobs": [{"id": self.JOB_ID, "required": True, "execution_sha256": "2" * 64,
                              "source_execution_sha256": "1" * 64}], "reuse_jobs": [],
        }
        self._publish(campaign, segment, reservation, result, summary=summary)
        return campaign, segment, reservation, result

    def _segment_bindings(self, campaign: Path, segment: Path, result: dict):
        manifest = json.loads((campaign / "campaign.json").read_bytes())
        return provenance._candidate_restoration_bindings(
            campaign, segment, result, configuration=manifest["configuration"], host_data_root=campaign.parent.parent.parent,
        )

    def _inherited_fixture(self):
        campaign, first, reservation, result = self._recovery_fixture(summary=True)
        segment = first.with_name("ar2")
        proof = {
            "recovered_from": "ar1", "source_execution_sha256": "1" * 64,
            "result": self._binding(campaign, first / f"job-{self.JOB_ID}.json"),
            "checkpoint": self._binding(campaign, first / "checkpoints" / "00000001.json"),
        }
        inherited = dict(result, disposition="reused", recovered_from="ar1", source_receipt=proof["result"],
                         recovery_checkpoint=proof["checkpoint"], source_execution_sha256="1" * 64)
        reservation = json.loads(json.dumps(reservation))
        reservation.update(recovery_revision="ar2", run_nonce="c" * 64, execute_job_ids=[], reuse_job_ids=[self.JOB_ID],
                           reuse_proofs={self.JOB_ID: proof}, reuse_proofs_sha256=incremental.digest({self.JOB_ID: proof}),
                           recovery_review_sha256="d" * 64)
        self._publish(campaign, segment, reservation, inherited, summary=True)
        return campaign, segment, reservation, inherited

    def test_formal_segment_counts_distinct_producer_roots_and_interrupted_publication(self):
        for summary, failed in ((False, False), (True, False), (True, True)):
            with self.subTest(summary=summary, failed=failed):
                campaign, segment, _reservation, result = self._recovery_fixture(summary=summary, failed=failed)
                self.assertIsNotNone(self._segment_bindings(campaign, segment, result))
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"]), ("complete", 2))
                self.assertEqual(len({row["identity_key"] for row in receipt["requests"]}), 2)
                self.assertEqual(len({row["producer_run_id"] for row in receipt["requests"]}), 2)

    def test_invalid_segment_authority_keeps_raw_requests_but_blocks_accounting(self):
        cases = ("坏预约摘要", "错原预约", "错原摘要", "错原执行摘要", "错执行摘要", "错裁定", "错候选", "错段根",
                 "坏 checkpoint", "错 checkpoint 身份", "错摘要结果", "空复用 checkpoint")
        for case in cases:
            with self.subTest(case=case):
                campaign, segment, reservation, result = self._recovery_fixture(summary=True)
                if case == "坏预约摘要":
                    reservation["reservation_digest"] = "0" * 64
                elif case == "错原预约":
                    reservation["original_reservation"]["path"] = "candidates/other/attempts/a1/reservation.json"
                elif case == "错原摘要":
                    reservation["original_attempt"]["attempt_digest"] = "0" * 64
                elif case == "错原执行摘要":
                    reservation["planned_jobs"][0]["source_execution_sha256"] = "0" * 64
                elif case == "错执行摘要":
                    reservation["planned_jobs"][0]["execution_sha256"] = "0" * 64
                elif case == "错裁定":
                    reservation["recovery_sha256"] = "0" * 64
                elif case == "错候选":
                    reservation["candidate_id"] = "other"
                elif case == "错段根":
                    old = result["evidence_roots"][0]
                    result = json.loads(json.dumps(result).replace(old, old.replace("-recovery-ar1", "-recovery-ar2")))
                    data = campaign.parent.parent.parent
                    current_root = data / Path(old).relative_to("/capture")
                    current_root.rename(current_root.with_name(current_root.name.replace("-ar1", "-ar2")))
                    shutil.rmtree(segment / "checkpoints")
                    self._publish(campaign, segment, reservation, result, summary=True)
                elif case in {"坏 checkpoint", "错 checkpoint 身份"}:
                    path = segment / "checkpoints" / "00000001.json"
                    checkpoint = json.loads(path.read_bytes())
                    checkpoint["run_nonce"] = "0" * 64
                    self._json(path, checkpoint, "checkpoint_sha256" if case == "错 checkpoint 身份" else None)
                elif case == "错摘要结果":
                    path = segment / "attempt-recovery.json"
                    document = json.loads(path.read_bytes())
                    document["results"] = []
                    self._json(path, document, "attempt_recovery_digest")
                else:
                    reservation.update(execute_job_ids=[], reuse_job_ids=[self.JOB_ID], reuse_proofs={self.JOB_ID: {"checkpoint": None}})
                self._json(segment / "recovery-reservation.json", reservation,
                           None if case == "坏预约摘要" else "reservation_digest")
                # 保持摘要对预约的外层绑定有效，确保每个变体命中它声称检验的内部闭包。
                summary_path = segment / "attempt-recovery.json"
                summary = json.loads(summary_path.read_bytes())
                summary["reservation"] = self._binding(campaign, segment / "recovery-reservation.json")
                self._json(summary_path, summary, "attempt_recovery_digest")
                self.assertIsNone(self._segment_bindings(campaign, segment, result))
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]),
                                 ("accounting_unresolved", 2, [self.JOB_ID]))

    def test_successor_inherits_only_direct_predecessor_without_double_counting(self):
        campaign, segment, _reservation, result = self._inherited_fixture()
        self.assertIsNotNone(self._segment_bindings(campaign, segment, result))
        receipt = self._collect(campaign)
        self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]), ("complete", 2, []))

    def test_successor_rejects_forged_producer_checkpoint_and_authority(self):
        for case in ("错生产段", "跳段来源", "空 checkpoint", "错原执行摘要", "错审阅摘要", "部分复用字段", "跨基线"):
            with self.subTest(case=case):
                campaign, segment, reservation, result = self._inherited_fixture()
                proof = reservation["reuse_proofs"][self.JOB_ID]
                if case == "错生产段":
                    proof["recovered_from"] = result["recovered_from"] = "ar2"
                elif case == "跳段来源":
                    proof["result"]["path"] = proof["result"]["path"].replace("/ar1/", "/ar3/")
                elif case == "空 checkpoint":
                    proof["checkpoint"] = result["recovery_checkpoint"] = None
                elif case == "错原执行摘要":
                    proof["source_execution_sha256"] = "0" * 64
                elif case == "错审阅摘要":
                    reservation["recovery_review_sha256"] = None
                elif case == "部分复用字段":
                    reservation.pop("recovery_review_sha256")
                else:
                    old = campaign / "candidates" / self.CANDIDATE_ID / "revisions" / "b1"
                    new = old.with_name("b2")
                    recovery = json.loads((old / "recovery.json").read_bytes())
                    recovery["evaluation_baseline"] = 2
                    self._json(new / "recovery.json", recovery, "recovery_sha256")
                    commit = json.loads((old / "COMMIT").read_bytes())
                    commit.update(evaluation_baseline=2, recovery_sha256=recovery["recovery_sha256"])
                    self._json(new / "COMMIT", commit, "commit_sha256")
                    reservation.update(evaluation_baseline=2, baseline_commit_sha256=commit["commit_sha256"],
                                       recovery_sha256=recovery["recovery_sha256"])
                reservation["reuse_proofs_sha256"] = incremental.digest(reservation["reuse_proofs"])
                shutil.rmtree(segment / "checkpoints")
                self._publish(campaign, segment, reservation, result, summary=True)
                self.assertIsNone(self._segment_bindings(campaign, segment, result))


class SealedCandidateRestorationAccountingTests(reuse_tests.CandidateReuseFixture):
    """沿用 c3b 的真实候选来源及封存清单；正式 cover、重绑、收据链和 provenance 均不替换。"""

    def write_candidate_campaign(self, **kwargs):
        campaign = super().write_candidate_campaign(**kwargs)
        manifest = reuse_tests.claude_upgrade.load_campaign_manifest(campaign)
        # 既有 c3 夹具未冻结 Job 行；补齐生产总计划中的模板行，再运行和封存，避免事后修改来源身份。
        jobs = [job for phase in ("official", "candidate")
                for job in reuse_tests.capture._expand_phase_jobs(campaign, manifest, phase, candidate_id=None)]
        manifest["jobs"] = [{"id": job.job_id, "phase": job.phase,
                             "execution_sha256": reuse_tests.capture._job_execution_sha256(job),
                             "evidence_roots": list(job.evidence_roots)} for job in jobs]
        _write(campaign / "campaign.json", json.dumps(manifest))
        _write(campaign / "campaign.sha256", hashlib.sha256((campaign / "campaign.json").read_bytes()).hexdigest())
        return campaign

    def _world(self):
        source, source_attempt = reuse_tests.SealedReuseTests.sealed_predecessor(self)
        campaign = self.successor()
        preview = self.preview(campaign, source, source_attempt)
        code, result, stderr = self.resume_reuse(
            campaign, source, source_attempt, "--approve-candidate-reuse-sha256", preview["preview_sha256"]
        )
        self.assertEqual((code, (result or {}).get("status")), (2, "awaiting_receipts"), stderr)
        attempt_root, attempt = self.load_attempt(campaign, result["attempt_id"])
        return source, source_attempt, campaign, attempt_root, attempt["results"]

    def _bindings(self, campaign, attempt_root, row):
        with self.reuse_patched():
            manifest = reuse_tests.claude_upgrade.load_campaign_manifest(campaign)
            return provenance._candidate_restoration_bindings(
                campaign, attempt_root, row, configuration=manifest["configuration"], host_data_root=self.data
            )

    def test_sealed_source_uses_real_cover_without_touching_predecessor(self):
        source, source_attempt, campaign, attempt_root, rows = self._world()
        before = reuse_tests.SealedReuseTests.source_boundary(self, source, source_attempt)
        for row in rows:
            self.assertIsNotNone(self._bindings(campaign, attempt_root, row))
        self.assertEqual(reuse_tests.SealedReuseTests.source_boundary(self, source, source_attempt), before)

    def test_metadata_rebind_replaces_old_job_closeout_only_with_cover(self):
        from tools.official_client_capture import codex_upgrade_harden_evidence_permissions as harden

        source, source_attempt, campaign, attempt_root, rows = self._world()
        row = rows[0]
        report = Path(row["restoration_checks"][0]["report"]["path"])
        metadata = report.stat()
        os.utime(report, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
        self.assertIsNone(self._bindings(campaign, attempt_root, row), "未登记元数据重绑时必须未决")
        harden.rebind_boundary(source, source_attempt, candidate_id=reuse_tests.CANDIDATE, operator="候选账务回归")
        before = reuse_tests.SealedReuseTests.source_boundary(self, source, source_attempt)
        self.assertIsNotNone(self._bindings(campaign, attempt_root, row), "正式重绑后沿同一封存 cover 重放")
        without_cover = json.loads(json.dumps(row))
        without_cover["reused_from"]["producer"].pop(reuse_tests.predecessor_sealed.COVER_FIELD)
        self.assertIsNone(self._bindings(campaign, attempt_root, without_cover), "没有 cover 仍重放旧 Job 收口，不能静默放行")
        self.assertEqual(reuse_tests.SealedReuseTests.source_boundary(self, source, source_attempt), before)

    def test_forged_cover_and_tampered_report_stay_unresolved(self):
        source, source_attempt, campaign, attempt_root, rows = self._world()
        row = rows[0]
        for case in ("错 Campaign", "错候选", "错 attempt", "错阶段摘要", "错清单摘要", "未知字段", "空证明"):
            with self.subTest(case=case):
                altered = json.loads(json.dumps(row))
                producer = altered["reused_from"]["producer"]
                cover = producer[reuse_tests.predecessor_sealed.COVER_FIELD]
                if case == "错 Campaign":
                    cover["campaign"]["campaign_id"] = "forged-source"
                elif case == "错候选":
                    cover["campaign"]["candidate_id"] = "forged-candidate"
                elif case == "错 attempt":
                    cover["attempt_id"] = "forged-attempt"
                elif case == "错阶段摘要":
                    cover["stage_result"]["sha256"] = "0" * 64
                elif case == "错清单摘要":
                    cover["sealed_evidence_manifest"]["sha256"] = "0" * 64
                elif case == "未知字段":
                    cover["unknown"] = True
                else:
                    producer[reuse_tests.predecessor_sealed.COVER_FIELD] = None
                self.assertIsNone(self._bindings(campaign, attempt_root, altered))
        report = Path(row["restoration_checks"][0]["report"]["path"])
        report.chmod(0o600)
        _write(report, report.read_bytes() + b" ")
        self.assertIsNone(self._bindings(campaign, attempt_root, row))

    def test_valid_cover_that_omits_the_producer_root_stays_unresolved(self):
        """重建一份内容有效但漏掉当前外部根的来源清单：cover 自身可重放，provenance 的根覆盖核验必须拒绝。"""

        source, source_attempt, campaign, attempt_root, rows = self._world()
        row = rows[0]
        seal = reuse_tests.candidate_seal
        producer = row["reused_from"]["producer"]
        cover = producer[reuse_tests.predecessor_sealed.COVER_FIELD]
        source_root = source / "candidates" / reuse_tests.CANDIDATE / "attempts" / source_attempt
        attempt_path = source_root / "attempt.json"
        source_payload = json.loads(attempt_path.read_bytes())
        excluded = set(row["evidence_roots"])
        source_payload["evidence_roots"] = [value for value in source_payload["evidence_roots"] if value not in excluded]
        _write(attempt_path, json.dumps(source_payload))
        document = reuse_tests.evidence_manifest.build_evidence_manifest(
            seal._sealed_evidence_roots(source, source_attempt, reuse_tests.CANDIDATE),
            checkpoint_path=source_root / "provenance-partial-cover-checkpoint.json",
        )
        manifest_path = source_root / seal.SEALED_EVIDENCE_MANIFEST_FILENAME
        _write(manifest_path, json.dumps(document))
        stage_path = source / cover["stage_result"]["path"]
        stage = json.loads(stage_path.read_bytes())
        stage["attempt"] = seal._stage_binding(attempt_path, source)
        stage[seal.SEALED_EVIDENCE_MANIFEST_FIELD] = seal._stage_binding(manifest_path, source)
        _write(stage_path, json.dumps(stage))
        cover["stage_result"] = seal._stage_binding(stage_path, source)
        cover["sealed_evidence_manifest"] = stage[seal.SEALED_EVIDENCE_MANIFEST_FIELD]
        with self.reuse_patched():
            reuse_tests.predecessor_sealed.verify_cover(cover)
        self.assertIsNone(self._bindings(campaign, attempt_root, row))
