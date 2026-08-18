"""Claude FW-E 冻结、同工具门禁和 57 条保守迁移测试。"""

from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capture import _load_claude_credentials
from tools.official_client_capture.claude_fw_e import (
    FWEEvidenceError,
    _scan_capture_group,
    _verify_integrity,
    build_rule_assessments,
)
from tools.official_client_control.canonical import canonical_json_bytes, load_json_file


ROOT = Path(__file__).resolve().parents[3]
BASELINE_LEDGER = (
    ROOT / "tools/official_client_capture/claude_21220/rules_2_1_220.json"
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def manifest(version: str, binary_sha256: str, source_sha256: str) -> dict:
    return {
        "schema_version": "official-client-capture/v1",
        "status": "complete",
        "batch_id": f"batch-{version.replace('.', '-')}",
        "cleanup": {"successful": True},
        "m_binding": {"complete": True},
        "secret_scan": {"passed": True, "matches": []},
        "clients": {
            "claude": {
                "version": f"{version} (Claude Code)",
                "sha256": binary_sha256,
            }
        },
        "runtime": {
            "capture_tools": {"execution_sources": {"sha256": source_sha256}},
            "host_runtime_receipt": {
                "repo_digest_verified": True,
                "container_runtime_binding": {"verified": True},
                "runtime_image_reference": "capture@sha256:" + "3" * 64,
            },
        },
        "case_results": [
            {
                "status": "complete",
                "evidence": "mitm",
                "scenario": "s1",
            }
        ],
    }


class ClaudeFWETests(unittest.TestCase):
    def test_credentials_loader_keeps_values_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            write_json(
                path,
                {
                    "claudeAiOauth": {
                        "accessToken": "access-secret-value",
                        "refreshToken": "refresh-secret-value",
                    }
                },
            )
            path.chmod(0o600)
            self.assertEqual(
                _load_claude_credentials(path),
                ("access-secret-value", "refresh-secret-value"),
            )

    def test_credentials_loader_rejects_readable_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            write_json(
                path,
                {
                    "claudeAiOauth": {
                        "accessToken": "access-secret-value",
                        "refreshToken": "refresh-secret-value",
                    }
                },
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "禁止组/其他用户读取"):
                _load_claude_credentials(path)

    def test_verifies_both_sri_and_legacy_shasum(self) -> None:
        content = b"official-tarball"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
        _verify_integrity(content, integrity, hashlib.sha1(content).hexdigest())
        with self.assertRaisesRegex(FWEEvidenceError, "integrity"):
            _verify_integrity(content + b"drift", integrity, hashlib.sha1(content).hexdigest())

    def test_capture_group_requires_complete_m(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest("2.1.226", "a" * 64, "b" * 64)
            payload["m_binding"]["complete"] = False
            write_json(root / "run/manifest.json", payload)
            with self.assertRaisesRegex(FWEEvidenceError, "M 不完整"):
                _scan_capture_group(root, "target", "2.1.226", "a" * 64)

    def test_capture_group_binds_one_execution_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "a/manifest.json", manifest("2.1.226", "a" * 64, "b" * 64))
            write_json(root / "b/manifest.json", manifest("2.1.226", "a" * 64, "c" * 64))
            with self.assertRaisesRegex(FWEEvidenceError, "多份抓包执行源"):
                _scan_capture_group(root, "target", "2.1.226", "a" * 64)

    def test_builds_closed_conservative_57_rule_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            ledger = workspace / "baseline.json"
            ledger.write_bytes(BASELINE_LEDGER.read_bytes())
            static = workspace / "static.json"
            capture = workspace / "capture.json"
            write_json(
                static,
                {
                    "schema_version": "claude-code-fw-e-static-diff/v1",
                    "target_version": "2.1.226",
                },
            )
            write_json(
                capture,
                {
                    "schema_version": "claude-code-fw-e-capture-index/v1",
                    "result": "passed",
                    "channels": ["A1", "J", "L", "M", "P", "R"],
                },
            )
            result = build_rule_assessments(
                workspace,
                ledger,
                static,
                capture,
                workspace / "rules",
                None,
            )
            self.assertEqual(result["rule_count"], 57)
            self.assertEqual(result["inherit_count"], 0)
            self.assertEqual(result["regressed_evidence_count"], 12)
            self.assertEqual(result["blocked_count"], 0)
            self.assertEqual(
                sum(row["migration_decision"] == "delete" for row in result["rules"]),
                1,
            )
            written = load_json_file(
                workspace / "rules/rule-assessments.json", "rule assessments"
            )
            self.assertEqual(written["rule_count"], 57)


if __name__ == "__main__":
    unittest.main()
