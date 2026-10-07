"""B5b-04：发布必需链不会随尚未完成的可执行登记缩小；当前 Formal 准入拒绝缺链认证。

夹具只构造绑定文件与已签结果，验证器仍重放自摘要、附件绑定和逐链结果；不把这些离线夹具登记为 ARM64 实测通过。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import certify_release as release
from tools.official_client_capture import claude_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_policy_certification as policy
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3


class ClaudeReleaseAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-release-admission-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.identity = {field: "a" * 64 for field in policy.IDENTITY_FIELDS}
        self.identity["policy_version"] = 2
        self.coverage = [
            {"id": name, "test": f"fixtures:{name}", "status": "passed"}
            for name in release.RELEASE_REQUIRED_CHAINS["claude"]
        ]

    def write(self, name: str, payload: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def certificate(self, coverage: list[dict] | None) -> Path:
        """生成可被真实 verify 重放的最小认证附件；自签并不预先赋予准入资格。"""

        rows = coverage or []
        paths = self.write("pre-a3.json", {
            "schema_version": pre_a3.SCHEMA_VERSION_V3, "client": "claude",
            "scenarios": [{"name": row["id"], "test": row["test"], "status": row["status"]} for row in rows],
            "real_chain_registration": [{"id": row["id"], "test": row["test"]} for row in rows],
        })
        deployment = self.write("deployment.json", {"fixture": "部署附件"})
        job = self.write("job.json", {"fixture": "Job 演练附件"})
        atomic = self.write("atomic.json", {"fixture": "原子双跑附件"})

        def binding(path: Path) -> dict:
            return release._bind_file(path, "测试附件", "claude")

        document = release.compose_certification(
            identity=self.identity, deployment_receipt=binding(deployment), pre_a3_certification=binding(paths),
            policy_activation=None, job_rehearsal={"evidence_root": str(self.root), "receipt": job.name,
                "sha256": binding(job)["sha256"], "failure_lifecycle_probe_sha256": "b" * 64},
            atomic_double_rehearsal={"evidence_root": str(self.root), "receipt": atomic.name,
                "sha256": binding(atomic)["sha256"]},
            campaign_run_rehearsal=None, real_chain_coverage=coverage, client="claude",
        )
        return self.write("release.json", document)

    def test_required_set_includes_unimplemented_recorded_and_late_stage_chains(self) -> None:
        self.assertEqual(set(release.RELEASE_REQUIRED_CHAINS["claude"]), {
            "vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause",
            "vc-chain.claude-tool-evolution", "vc-chain.claude-vc1-capture", "vc-chain.claude-full-validation-only",
            "vc-chain.claude-recovery-chain", "vc-chain.claude-late-stage-faults",
        })

    def test_verify_rejects_every_missing_required_chain(self) -> None:
        for absent in self.coverage:
            with self.subTest(chain=absent["id"]):
                path = self.certificate([row for row in self.coverage if row != absent])
                with self.assertRaisesRegex(release.ReleaseCertificationError, "发布必需链未认证"):
                    release.verify(path, expected_identity=self.identity, client="claude")

    def test_verify_requires_coverage_field_for_claude(self) -> None:
        path = self.certificate(None)
        with self.assertRaisesRegex(release.ReleaseCertificationError, "发布必需链覆盖缺失"):
            release.verify(path, expected_identity=self.identity, client="claude")

    def test_verify_rejects_failure_skip_duplicate_and_pre_a3_drift(self) -> None:
        for status in ("failed", "skipped"):
            with self.subTest(status=status):
                path = self.certificate([{**row, "status": status} if index == 0 else row for index, row in enumerate(self.coverage)])
                with self.assertRaises(release.ReleaseCertificationError):
                    release.verify(path, expected_identity=self.identity, client="claude")
        path = self.certificate([*self.coverage, self.coverage[0]])
        with self.assertRaises(release.ReleaseCertificationError):
            release.verify(path, expected_identity=self.identity, client="claude")
        path = self.certificate(self.coverage)
        self.write("pre-a3.json", {"篡改": True})
        with self.assertRaises(release.ReleaseCertificationError):
            release.verify(path, expected_identity=self.identity, client="claude")

    def test_complete_bound_coverage_passes_without_rerunning_chains(self) -> None:
        path = self.certificate(self.coverage)
        result = release.verify(path, expected_identity=self.identity, client="claude")
        self.assertEqual(result["real_chain_coverage"], self.coverage)

    def test_issue_rejects_partial_registration_before_output_or_rehearsal(self) -> None:
        output = self.root / "new-certification.json"
        inputs = dict(deployment_receipt=self.root / "deployment.json", pre_a3_certification=self.root / "pre-a3.json",
                      policy_activation=None, job_rehearsal_root=self.root, job_rehearsal_receipt=Path("job.json"),
                      atomic_rehearsal_root=self.root, atomic_rehearsal_receipt=Path("atomic.json"), client="claude")
        with mock.patch.object(policy, "current_identity", return_value=self.identity), \
                mock.patch.object(release, "_require_release_platform"), \
                mock.patch.object(policy, "load_deployment_receipt", return_value={}), \
                mock.patch.object(pre_a3, "verify_certification", return_value={}), \
                mock.patch.object(pre_a3, "real_chain_coverage", return_value=self.coverage[:4]), \
                mock.patch.object(release, "replay_job_rehearsal") as jobs:
            with self.assertRaisesRegex(release.ReleaseCertificationError, "发布必需链未认证"):
                release.issue(output, **inputs)
            self.assertFalse(output.exists())
            jobs.assert_not_called()

    def test_closeout_rejects_partial_certificate_before_formal_creation(self) -> None:
        path = self.certificate(self.coverage[:4])
        with mock.patch.object(policy, "current_identity", return_value=self.identity):
            with self.assertRaisesRegex(closeout.VC0CloseoutError, "发布必需链未认证"):
                closeout._validate_release_certification(path, {}, None, self.root / "preflight", {})
        self.assertFalse((self.root / "preflight").exists())

    def test_codex_keeps_historical_optional_coverage_contract(self) -> None:
        release.require_release_chain_coverage(None, client="codex")
        release.require_release_chain_coverage([], client="codex")


if __name__ == "__main__":
    unittest.main()
