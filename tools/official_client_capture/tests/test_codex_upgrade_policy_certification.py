"""A2.6：策略兼容收据与激活认证——版本必须升级、五摘要必须等于部署收据、替换后失效。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_policy_certification as certification
from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", "utf-8")
    path.chmod(0o600)
    return path


def _previous_policy(root: Path) -> Path:
    """旧策略：版本减一、少登记一个控制层文件（模拟 v2 → v3）。"""

    current = json.loads(tip.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    previous = json.loads(json.dumps(current))
    previous["policy_version"] = current["policy_version"] - 1
    previous["layers"]["control"]["files"] = [
        name for name in previous["layers"]["control"]["files"] if name != "codex_upgrade_pre_a3_certification.py"
    ]
    return _write_json(root / "previous-policy.json", previous)


def _deployment_receipt(root: Path, identity: dict, **overrides: object) -> Path:
    payload = {
        "schema_version": certification.DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "campaign_id": "deploy-fixture",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "policy_version": identity["policy_version"],
        **{name: identity[name] for name in certification.IDENTITY_FIELDS},
        "supervisor_sha256": "1" * 64,
    }
    payload.update(overrides)
    return _write_json(root / "deploy.json", payload)


class PolicyCertificationTests(unittest.TestCase):
    def test_compatibility_receipt_reports_layer_changes_and_campaign_dispositions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            previous = _previous_policy(root)
            campaign = root / "campaign-v2"
            previous_sha = tip.load_policy(previous)["policy_sha256"]
            _write_json(campaign / "campaign.json", {"campaign_id": "c-v2", "tool_identity": {"policy_sha256": previous_sha, "policy_version": 2}})
            legacy = root / "campaign-v1"
            _write_json(legacy / "campaign.json", {"campaign_id": "c-v1", "tool_identity": {"files_sha256": "0" * 64}})
            receipt = certification.build_compatibility_receipt(previous, campaign_dirs=[campaign, legacy])
            self.assertEqual(receipt["schema_version"], certification.POLICY_COMPATIBILITY_SCHEMA)
            self.assertEqual(receipt["previous"]["policy_sha256"], previous_sha)
            self.assertEqual(receipt["current"]["policy_sha256"], tip.load_policy()["policy_sha256"])
            self.assertEqual(receipt["newly_registered_files"], ["codex_upgrade_pre_a3_certification.py"])
            self.assertIn("codex_upgrade_pre_a3_certification.py", receipt["defaulted_paths_under_previous_policy"])
            self.assertEqual(receipt["defaulted_paths_under_current_policy"], [])
            self.assertNotEqual(receipt["identity_under_previous_policy"]["control_sha256"], receipt["identity_under_current_policy"]["control_sha256"])
            dispositions = {item["campaign_id"]: item["disposition"] for item in receipt["existing_campaigns"]}
            self.assertEqual(dispositions, {"c-v2": "retain_frozen_policy", "c-v1": "v1_identity_unaffected"})
            unsigned = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
            self.assertEqual(codex_upgrade._fingerprint(unsigned), receipt["receipt_sha256"])
            # 策略未变化或版本未升级都拒绝。
            with self.assertRaisesRegex(certification.PolicyCertificationError, "策略未变化"):
                certification.build_compatibility_receipt(tip.DEFAULT_POLICY_PATH)
            same_version = json.loads(previous.read_text(encoding="utf-8"))
            same_version["policy_version"] = tip.load_policy()["policy_version"]
            with self.assertRaisesRegex(certification.PolicyCertificationError, "严格升级"):
                certification.build_compatibility_receipt(_write_json(root / "same-version.json", same_version))

    def test_activation_binds_matching_deployment_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = certification.current_identity()
            previous = _previous_policy(root)
            compatibility_path = _write_json(root / "compat.json", certification.build_compatibility_receipt(previous))
            deployment = _deployment_receipt(root, identity)
            activation = certification.build_activation_certification(deployment, compatibility_path)
            self.assertEqual(activation["status"], "active")
            self.assertEqual(activation["identity"], {name: identity[name] for name in certification.IDENTITY_FIELDS})
            self.assertEqual(activation["authorized_scopes"], ["A2.5", "A3b"])
            self.assertIsNone(activation["superseded_by"])
            activation_path = _write_json(root / "activation.json", activation)
            verified = certification.verify_activation_certification(activation_path)
            self.assertEqual(verified["policy_sha256"], identity["policy_sha256"])
            # 部署收据五摘要不等于当前工具：拒绝签发。
            stale = _deployment_receipt(root / "stale", identity, wire_producer_sha256="0" * 64)
            with self.assertRaisesRegex(certification.PolicyCertificationError, "五摘要与当前工具身份不一致"):
                certification.build_activation_certification(stale, compatibility_path)
            # 兼容收据的新策略不是当前策略：拒绝签发。
            foreign = json.loads(compatibility_path.read_text(encoding="utf-8"))
            foreign["current"]["policy_sha256"] = "0" * 64
            foreign.pop("receipt_sha256")
            foreign["receipt_sha256"] = codex_upgrade._fingerprint(foreign)
            with self.assertRaisesRegex(certification.PolicyCertificationError, "不是当前策略"):
                certification.build_activation_certification(deployment, _write_json(root / "foreign.json", foreign))
            # 被替换或部署收据漂移后失效。
            superseded = dict(activation, superseded_by="tool-release-certification/v1")
            superseded.pop("receipt_sha256")
            superseded["receipt_sha256"] = codex_upgrade._fingerprint(superseded)
            with self.assertRaisesRegex(certification.PolicyCertificationError, "已被替换"):
                certification.verify_activation_certification(_write_json(root / "superseded.json", superseded))
            deployment.write_text(deployment.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(certification.PolicyCertificationError, "部署收据缺失或摘要漂移"):
                certification.verify_activation_certification(activation_path)

    def test_cli_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = certification.current_identity()
            previous = _previous_policy(root)
            deployment = _deployment_receipt(root, identity)
            compat_out = root / "out" / "compat.json"
            self.assertEqual(
                certification.main(["compatibility", "--previous-policy", str(previous), "--output", str(compat_out)]),
                0,
            )
            activation_out = root / "out" / "activation.json"
            self.assertEqual(
                certification.main(
                    ["activation", "--deployment-receipt", str(deployment), "--compatibility-receipt", str(compat_out), "--output", str(activation_out)]
                ),
                0,
            )
            self.assertEqual(certification.main(["verify-activation", "--certification", str(activation_out)]), 0)
            # 不可变输出：重复签发拒绝覆盖。
            self.assertEqual(
                certification.main(
                    ["activation", "--deployment-receipt", str(deployment), "--compatibility-receipt", str(compat_out), "--output", str(activation_out)]
                ),
                2,
            )


if __name__ == "__main__":
    unittest.main()
