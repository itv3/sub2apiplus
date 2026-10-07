"""候选恢复报告的请求核算边界：完整绑定可识别，伪造、缺绑定和额外内容继续未决。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture
from tools.official_client_capture import claude_upgrade_live_request_provenance as provenance
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
        for failed, recovery in ((False, False), (True, False), (False, True)):
            with self.subTest(failed=failed, recovery=recovery):
                campaign, root, _job, _path, _report, _result = self._fixture(failed=failed, recovery=recovery)
                receipt = self._collect(campaign)
                self.assertEqual((receipt["status"], receipt["precise_total"], receipt["unresolved_job_ids"]), ("complete", 1, []))
                # 离线核算没有收据上下文，不能凭同名目录或报告中的自述放行。
                offline = provenance.collect_root_provenance([root])
                self.assertEqual((offline["precise_total"], offline["unresolved_roots"]), (1, [str(root)]))

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
