"""C1：受管工具发布认证——逐项重放输入、绑定部署收据与五摘要、替换策略激活认证。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import certify_release
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_campaign_run_rehearsal_receipt as rehearsal
from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
from tools.official_client_capture import codex_upgrade_pre_a3_certification as pre_a3
from tools.official_client_capture.tests import test_codex_upgrade_policy_certification as policy_tests
from tools.official_client_capture.tests.control_receipt_fixtures import create_job_rehearsal_receipt


def _job_contract() -> dict[str, object]:
    return {
        "schema_version": job_rehearsal.EXECUTION_CONTRACT_SCHEMA,
        "target_version": "0.154.0",
        "target_sha256": "1" * 64,
        "target_package_sha256": "2" * 64,
        "target_code_mode_host_sha256": "3" * 64,
        "suite": "full",
        "tool_files_sha256": codex_upgrade._tool_identity()["files_sha256"],
        "configuration": {
            field: (
                "/root/oauth-capture"
                if field == "capture_root"
                else "capture-cli"
                if field == "capture_container"
                else f"fixture-{field}"
            )
            for field in job_rehearsal.EXECUTION_CONFIGURATION_FIELDS
        },
        "job_count": 1,
        "job_ids": ["job-a"],
        "job_phases": {"job-a": "official"},
        "step_counts": {"job-a": 1},
        "phase_counts": {"official": 1, "candidate": 0},
        "c2pa_job_identities": {},
        "target_scenario_sha256": "4" * 64,
        "evidence_label_declaration_sha256": "6" * 64,
        "job_templates_sha256": "7" * 64,
        "extra_jobs_sha256": None,
    }


class CertifyReleaseTests(unittest.TestCase):
    def _inputs(self, root: Path) -> dict[str, Path]:
        identity = policy_certification.current_identity()
        previous = policy_tests._previous_policy(root)
        compatibility = policy_tests._write_json(
            root / "compat.json", policy_certification.build_compatibility_receipt(previous)
        )
        deployment = policy_tests._deployment_receipt(root, identity)
        activation = policy_tests._write_json(
            root / "activation.json",
            policy_certification.build_activation_certification(deployment, compatibility),
        )
        staging = root / "data" / "staging" / "pre-a3"
        pre_a3_receipt = pre_a3.run_certification(
            staging, deployment_receipt=deployment, policy_activation=activation, scenarios=()
        )
        pre_a3_path = root / "pre-a3.json"
        policy_certification._write_once(pre_a3_path, pre_a3_receipt)
        job_root = root / "job"
        job_receipt = create_job_rehearsal_receipt(
            job_root, contract=_job_contract(), preflight_campaign_id="preflight-0154"
        )
        atomic_root = root / "data" / "staging" / "atomic-double"
        atomic_root.mkdir(parents=True, mode=0o700)
        rehearsal.collect_atomic_double(atomic_root, "receipt.json", require_arm64=False)
        return {
            "deployment_receipt": deployment,
            "pre_a3_certification": pre_a3_path,
            "policy_activation": activation,
            "job_rehearsal_root": job_root,
            "job_rehearsal_receipt": job_receipt,
            "atomic_rehearsal_root": atomic_root,
            "atomic_rehearsal_receipt": Path("receipt.json"),
        }

    def test_issue_binds_inputs_and_verify_fails_closed_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs = self._inputs(root)
            output = root / "release-certification.json"
            certification = certify_release.issue(output, **inputs, require_arm64=False)
            self.assertEqual(certification["schema_version"], certify_release.SCHEMA_VERSION)
            self.assertEqual(certification["status"], "active")
            self.assertIsNone(certification["superseded_by"])
            identity = policy_certification.current_identity()
            self.assertEqual(
                certification["identity"],
                {name: identity[name] for name in policy_certification.IDENTITY_FIELDS},
            )
            self.assertEqual(certification["policy_version"], identity["policy_version"])
            self.assertEqual(certification["job_rehearsal"]["job_count"], 1)
            self.assertTrue(certification["job_rehearsal"]["failure_lifecycle_probe_sha256"])
            self.assertEqual(
                certification["atomic_double_rehearsal"]["campaign_ids"],
                ["atomic-vc0-vc1-1", "atomic-vc0-vc1-2"],
            )
            self.assertEqual(
                certification["supersedes"]["policy_activation"]["path"],
                str(inputs["policy_activation"].resolve()),
            )
            self.assertIsNone(certification["campaign_run_rehearsal"])
            verified = certify_release.verify(output)
            self.assertEqual(verified["receipt_sha256"], certification["receipt_sha256"])
            self.assertEqual(certify_release.main(["verify", "--certification", str(output)]), 0)
            # 只写一次。
            with self.assertRaises(policy_certification.PolicyCertificationError):
                certify_release.issue(output, **inputs, require_arm64=False)
            # 绑定文件漂移即失效：部署收据、pre-A3 认证、Job 演练收据。
            for key in ("deployment_receipt", "pre_a3_certification"):
                path = inputs[key]
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.subTest(drift=key), self.assertRaisesRegex(
                    certify_release.ReleaseCertificationError, "摘要漂移"
                ):
                    certify_release.verify(output)
                path.write_bytes(original)
            job_path = inputs["job_rehearsal_root"] / inputs["job_rehearsal_receipt"].name
            original = job_path.read_bytes()
            job_path.write_bytes(original + b"\n")
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "摘要漂移"):
                certify_release.verify(output)
            job_path.write_bytes(original)
            # 被替换后失效；自摘要篡改失效。
            superseded = json.loads(output.read_text(encoding="utf-8"))
            superseded["superseded_by"] = "tool-release-certification/v2"
            superseded.pop("receipt_sha256")
            superseded["receipt_sha256"] = codex_upgrade._fingerprint(superseded)
            path = policy_tests._write_json(root / "superseded.json", superseded)
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "已被替换"):
                certify_release.verify(path)
            tampered = json.loads(output.read_text(encoding="utf-8"))
            tampered["job_rehearsal"]["job_count"] = 2
            path = policy_tests._write_json(root / "tampered.json", tampered)
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "自摘要不一致"):
                certify_release.verify(path)

    def test_issue_can_delegate_atomic_replay_to_capture_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs = self._inputs(root)
            completed = mock.Mock(returncode=0, stdout=b'{"campaign_id": null, "live_request_count": 0, "status": "passed"}\n', stderr=b"")
            with mock.patch.object(certify_release.subprocess, "run", return_value=completed) as run:
                certification = certify_release.build_certification(
                    **inputs, atomic_container="capture-cli", data_root=root / "data"
                )
            command = run.call_args.args[0]
            self.assertEqual(command[:2], ["docker", "exec"])
            self.assertIn("/capture/staging/atomic-double", command)
            self.assertIn("atomic-double-replay", command)
            self.assertEqual(certification["atomic_double_rehearsal"]["replayed_in_container"], "capture-cli")
            failed = mock.Mock(returncode=1, stdout=b"", stderr=b"tampered")
            with mock.patch.object(certify_release.subprocess, "run", return_value=failed), self.assertRaisesRegex(
                certify_release.ReleaseCertificationError, "容器重放失败"
            ):
                certify_release.build_certification(**inputs, atomic_container="capture-cli", data_root=root / "data")
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "需要 --data-root"):
                certify_release.build_certification(**inputs, atomic_container="capture-cli")

    def test_issue_rejects_stale_deployment_and_mismatched_pre_a3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            inputs = self._inputs(root)
            stale = policy_tests._deployment_receipt(
                root / "stale", policy_certification.current_identity(), control_sha256="0" * 64
            )
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "五摘要与当前工具身份不一致"):
                certify_release.build_certification(**{**inputs, "deployment_receipt": stale}, require_arm64=False)
            # pre-A3 认证绑定的部署收据必须就是本次发布认证的部署收据。
            other = policy_tests._deployment_receipt(root / "other", policy_certification.current_identity())
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "不是本次发布认证的部署收据"):
                certify_release.build_certification(**{**inputs, "deployment_receipt": other}, require_arm64=False)
            with self.assertRaisesRegex(certify_release.ReleaseCertificationError, "同时提供"):
                certify_release.build_certification(
                    **inputs, campaign_run_rehearsal_root=root / "x", require_arm64=False
                )


if __name__ == "__main__":
    unittest.main()
