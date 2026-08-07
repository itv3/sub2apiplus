"""候选证据状态恢复与秘密扫描测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.candidate_evidence_guard import (
    EvidenceGuardError,
    SCAN_CHUNK_SIZE,
    file_sha256,
    normalize_state,
    scan_files_for_secrets,
    verify_evidence_guard,
    verify_restoration,
    write_state_snapshot,
)


class CandidateEvidenceGuardTest(unittest.TestCase):
    def _state_input(self, root: Path, name: str, keeper: str = "running") -> Path:
        path = root / name
        path.write_text(
            json.dumps(
                {
                    "keeper": keeper,
                    "proxy": None,
                    "account": {
                        "schedulable": True,
                        "credential_sha256": "a" * 64,
                    },
                    "redis": {"run_owned_key_count": 0},
                    "volumes": ["postgres-data", "redis-data"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_state_normalization_is_deterministic(self) -> None:
        left = {"proxy": None, "keeper": "running"}
        right = {"keeper": "running", "proxy": None}
        self.assertEqual(normalize_state(left), normalize_state(right))
        self.assertTrue(normalize_state(left).endswith(b"\n"))

    def test_sensitive_and_volatile_state_keys_fail_closed(self) -> None:
        with self.assertRaisesRegex(EvidenceGuardError, "凭据原文"):
            normalize_state({"refresh_token": "should-never-be-saved"})
        with self.assertRaisesRegex(EvidenceGuardError, "凭据原文"):
            normalize_state({"oauth_refresh_token_value": "should-never-be-saved"})
        with self.assertRaisesRegex(EvidenceGuardError, "自然波动字段"):
            normalize_state({"keeper": "running", "uptime_seconds": 12})
        with self.assertRaisesRegex(EvidenceGuardError, "NaN"):
            normalize_state({"ratio": float("nan")})
        # 摘要和存在性标志属于允许的状态画像，不包含凭据原文。
        normalize_state(
            {"credential_sha256": "a" * 64, "cookie_present": False}
        )

    def test_snapshot_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "duplicate.json"
            source.write_text(
                '{"keeper":"running","keeper":"stopped"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvidenceGuardError, "重复字段"):
                write_state_snapshot(source, root / "snapshot.json")

    def test_distinct_snapshot_files_are_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._state_input(root, "state.json")
            before = root / "before.json"
            after = root / "after.json"
            write_state_snapshot(source, before)
            write_state_snapshot(source, after)
            result = verify_restoration(before, after)
            self.assertTrue(result["passed"])
            self.assertTrue(result["different_inode"])
            self.assertTrue(result["byte_identical"])
            self.assertEqual(before.read_bytes(), after.read_bytes())

    def test_changed_state_and_hardlink_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_source = self._state_input(root, "before-source.json")
            after_source = self._state_input(
                root, "after-source.json", keeper="stopped"
            )
            before = root / "before.json"
            after = root / "after.json"
            write_state_snapshot(before_source, before)
            write_state_snapshot(after_source, after)
            with self.assertRaisesRegex(EvidenceGuardError, "环境未恢复"):
                verify_restoration(before, after)

            hardlink = root / "before-hardlink.json"
            os.link(before, hardlink)
            with self.assertRaisesRegex(EvidenceGuardError, "硬链接"):
                verify_restoration(before, hardlink)

    def test_secret_scan_covers_binary_and_never_reports_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = "unit-secret-value-123456"
            binary = root / "capture.pcap"
            binary.write_bytes(b"\xd4\xc3\xb2\xa1\x00" + secret.encode("ascii"))
            with mock.patch.dict(os.environ, {"CANDIDATE_TEST_SECRET": secret}):
                result = scan_files_for_secrets(
                    [("candidate/capture.pcap", binary)],
                    secret_env_names=["CANDIDATE_TEST_SECRET"],
                )
            self.assertFalse(result["passed"])
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(secret, serialized)
            self.assertIn("known-secret-env:CANDIDATE_TEST_SECRET", serialized)

    def test_known_secret_crossing_chunk_boundary_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = "boundary-secret-value-123456"
            path = Path(directory) / "large.bin"
            path.write_bytes(
                b"x" * (SCAN_CHUNK_SIZE - 5) + secret.encode("ascii") + b"tail"
            )
            with mock.patch.dict(os.environ, {"BOUNDARY_SECRET": secret}):
                result = scan_files_for_secrets(
                    [("candidate/large.bin", path)],
                    secret_env_names=["BOUNDARY_SECRET"],
                )
            self.assertFalse(result["passed"])
            self.assertTrue(
                any(
                    finding["rule"] == "known-secret-env:BOUNDARY_SECRET"
                    for finding in result["findings"]
                )
            )

    def test_redacted_values_do_not_trigger_heuristic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.txt"
            path.write_text(
                "authorization: <redacted len=120>\n"
                '"refresh_token":"<secret>"\n',
                encoding="utf-8",
            )
            result = scan_files_for_secrets([("candidate/trace.txt", path)])
            self.assertTrue(result["passed"], result["findings"])

    def test_full_guard_passes_with_hashed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            artifact = evidence_root / "candidate/A09/trace.jsonl"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                '{"event":"capture-complete","credential_sha256":"'
                + "a" * 64
                + '"}\n',
                encoding="utf-8",
            )
            manifest = evidence_root / "capture-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "codex-candidate-capture-manifest/v1",
                        "codex_version": "0.147.0",
                        "capture_id": "guard-test",
                        "status": "complete",
                        "artifacts": [
                            {
                                "path": "candidate/A09/trace.jsonl",
                                "sha256": file_sha256(artifact),
                                "kind": "process_trace",
                                "parser": "observation_jsonl",
                                "scenario_ids": ["A09"],
                                "labels": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source = self._state_input(root, "state.json")
            before = evidence_root / "restoration/before.json"
            after = evidence_root / "restoration/after.json"
            write_state_snapshot(source, before)
            write_state_snapshot(source, after)
            report = verify_evidence_guard(
                before=before,
                after=after,
                capture_manifest=manifest,
                evidence_root=evidence_root,
                expected_codex_version="0.147.0",
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["codex_version"], "0.147.0")
            self.assertTrue(report["restoration"]["byte_identical"])
            self.assertEqual(report["secret_scan"]["file_count"], 4)
            with self.assertRaisesRegex(EvidenceGuardError, "期望 Codex 版本"):
                verify_evidence_guard(
                    before=before,
                    after=after,
                    capture_manifest=manifest,
                    evidence_root=evidence_root,
                    expected_codex_version="",
                )
            with self.assertRaisesRegex(EvidenceGuardError, "Campaign 目标"):
                verify_evidence_guard(
                    before=before,
                    after=after,
                    capture_manifest=manifest,
                    evidence_root=evidence_root,
                    expected_codex_version="0.146.0",
                )


if __name__ == "__main__":
    unittest.main()
