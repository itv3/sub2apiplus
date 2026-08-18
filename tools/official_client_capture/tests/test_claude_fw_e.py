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
from tools.official_client_capture.capturelib.security import canonical_json_sha256
from tools.official_client_control.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    load_json_file,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def manifest(version: str, binary_sha256: str, source_sha256: str) -> dict:
    environment_values = {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }
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
                "capture": {"host_scope": "all"},
                "analysis_path": "analysis/mitm/claude-http/s1.json",
                "scenario_result": {
                    "invocation": {
                        "environment": {
                            "schema_version": "official-client-environment/v1",
                            "values": environment_values,
                            "keys": sorted(environment_values),
                            "redacted_keys": [],
                            "sha256": canonical_json_sha256(environment_values),
                        }
                    }
                },
            }
        ],
    }


def write_manifest(root: Path, payload: dict) -> None:
    write_json(root / "manifest.json", payload)
    write_json(
        root / "analysis/mitm/claude-http/s1.json",
        {
            "schema_version": "official-client-capture-normalized/v1",
            "records": [],
            "network_lifecycle": [
                {
                    "event": "request",
                    "capture_host_scope": "all",
                    "method": "POST",
                    "scheme": "https",
                    "host": "api.anthropic.com",
                    "port": 443,
                    "path": "/v1/messages",
                }
            ],
        },
    )


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
            write_manifest(root / "a", manifest("2.1.226", "a" * 64, "b" * 64))
            write_manifest(root / "b", manifest("2.1.226", "a" * 64, "c" * 64))
            with self.assertRaisesRegex(FWEEvidenceError, "多份抓包执行源"):
                _scan_capture_group(root, "target", "2.1.226", "a" * 64)

    def test_capture_group_builds_all_host_path_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(root, manifest("2.1.226", "a" * 64, "b" * 64))
            result = _scan_capture_group(
                root, "target", "2.1.226", "a" * 64
            )
            self.assertEqual(result["capture_host_scopes"], ["all"])
            self.assertEqual(len(result["network_observations"]), 1)
            observation = result["network_observations"][0]
            self.assertEqual(observation["host"], "api.anthropic.com")
            self.assertEqual(observation["path"], "/v1/messages")
            self.assertEqual(result["privacy_controls"]["result"], "passed")

    def test_capture_group_rejects_wrong_privacy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = manifest("2.1.226", "a" * 64, "b" * 64)
            environment = payload["case_results"][0]["scenario_result"][
                "invocation"
            ]["environment"]
            environment["values"]["DISABLE_TELEMETRY"] = "0"
            environment["sha256"] = canonical_json_sha256(environment["values"])
            write_manifest(root, payload)
            with self.assertRaisesRegex(FWEEvidenceError, "隐私开关实际值非法"):
                _scan_capture_group(root, "target", "2.1.226", "a" * 64)

    def test_rule_assessments_use_closed_matrix_and_keep_target_add(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            matrix_path = workspace / "matrix.json"
            closure_path = workspace / "closure.json"
            static = workspace / "static.json"
            capture = workspace / "capture.json"
            matrix = {
                "schema_version": "claude-code-fw-e-cross-source-matrix/v1",
                "target_version": "2.1.226",
                "target_rules": [
                    {
                        "id": "SPEC-BASE-001",
                        "domain": "header",
                        "retained_claim": "历史规则",
                        "scope": "test",
                        "required_channels": ["J"],
                        "origin": "historical_rule",
                        "baseline_disposition": "verified",
                    },
                    {
                        "id": "SPEC-NEW-001",
                        "domain": "endpoint",
                        "retained_claim": "目标原生新增规则",
                        "scope": "test",
                        "required_channels": ["J"],
                        "origin": "target_native_add",
                        "baseline_disposition": None,
                    },
                ],
            }
            write_json(matrix_path, matrix)
            write_json(
                closure_path,
                {
                    "schema_version": "claude-code-fw-e-completeness/v1",
                    "target_version": "2.1.226",
                    "matrix_sha256": canonical_sha256(matrix),
                    "unresolved_total": 0,
                    "result": "passed",
                },
            )
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
                    "target_version": "2.1.226",
                    "channels": ["J"],
                },
            )
            result = build_rule_assessments(
                workspace,
                matrix_path,
                closure_path,
                static,
                capture,
                workspace / "rules",
                None,
            )
            self.assertEqual(result["rule_count"], 2)
            self.assertEqual(result["inherit_count"], 0)
            self.assertEqual(result["regressed_evidence_count"], 1)
            self.assertEqual(result["blocked_count"], 0)
            self.assertEqual(
                sum(row["migration_decision"] == "add" for row in result["rules"]),
                1,
            )
            written = load_json_file(
                workspace / "rules/rule-assessments.json", "rule assessments"
            )
            self.assertEqual(written["rule_count"], 2)

    def test_rule_assessments_reject_baseline_only_blocked_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            matrix = {
                "schema_version": "claude-code-fw-e-cross-source-matrix/v1",
                "target_version": "2.1.226",
                "target_rules": [],
            }
            for name, value in (
                ("matrix.json", matrix),
                (
                    "closure.json",
                    {
                        "schema_version": "claude-code-fw-e-completeness/v1",
                        "target_version": "2.1.226",
                        "matrix_sha256": canonical_sha256(matrix),
                        "unresolved_total": 1,
                        "result": "blocked",
                    },
                ),
                (
                    "static.json",
                    {
                        "schema_version": "claude-code-fw-e-static-diff/v1",
                        "target_version": "2.1.226",
                    },
                ),
                (
                    "capture.json",
                    {
                        "schema_version": "claude-code-fw-e-capture-index/v1",
                        "target_version": "2.1.226",
                        "result": "passed",
                        "channels": ["J"],
                    },
                ),
            ):
                write_json(workspace / name, value)
            with self.assertRaisesRegex(FWEEvidenceError, "四方闭集未通过"):
                build_rule_assessments(
                    workspace,
                    workspace / "matrix.json",
                    workspace / "closure.json",
                    workspace / "static.json",
                    workspace / "capture.json",
                    workspace / "rules",
                    None,
                )


if __name__ == "__main__":
    unittest.main()
