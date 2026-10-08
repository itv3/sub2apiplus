"""只读能力检查专项；制品是真临时文件，不运行 CLI／GUI 或联网。"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture.claude_mac_capability_probe import CapabilityProbe, CapabilityProbeError, DIAGNOSTIC_SCHEMA
from tools.official_client_capture.claude_mac_recorder import measure_artifact


class CapabilityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.binary = self.root / "frozen-client"
        self.binary.write_bytes("真实冻结制品的测试字节".encode("utf-8"))
        self.artifact = {"artifact_id": "client-bin", "content": measure_artifact(self.binary)}

    def diagnostic(self, result="available") -> dict:
        path = self.root / "diagnostic.json"
        raw = json.dumps({"schema_version": DIAGNOSTIC_SCHEMA, "capability": "socket_events", "result": result,
                          "observer": "external-unverified", "observed_at_utc": "2026-10-08T01:00:00Z"}).encode()
        path.write_bytes(raw)
        return {"evidence": {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}

    def test_matching_real_artifact_still_does_not_grant_launch_or_provider(self) -> None:
        registry = dict(contract.SOURCE_ADAPTERS)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("不得启动任何产品")):
            result = CapabilityProbe.check([self.artifact], registered=True)
        self.assertTrue(result["artifacts_match"])
        self.assertEqual(result["artifacts"][0]["status"], "matched")
        self.assertTrue(all(row["status"] == "unknown" for row in result["capabilities"]))
        self.assertFalse(result["formal_ready"])
        self.assertFalse(result["launch_allowed"])
        self.assertFalse(result["registration_verified"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(contract.SOURCE_ADAPTERS, registry)

    def test_missing_or_changed_artifact_is_not_match(self) -> None:
        for operation in (lambda: self.binary.write_bytes(b"changed"), lambda: self.binary.unlink()):
            operation()
            result = CapabilityProbe.check([self.artifact])
            self.assertFalse(result["artifacts_match"])
            self.assertFalse(result["launch_allowed"])
        self.assertEqual(result["artifacts"][0]["status"], "missing")

    def test_target_symlink_and_escaping_directory_link_are_rejected(self) -> None:
        leaf = self.root / "alias"
        leaf.symlink_to(self.binary)
        artifact = copy.deepcopy(self.artifact)
        artifact["content"]["path"] = str(leaf)
        self.assertEqual(CapabilityProbe.check([artifact])["artifacts"][0]["status"], "invalid")
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "outside").symlink_to(self.binary)
        artifact["content"] = {"kind": "directory", "path": str(bundle), "sha256": "a" * 64, "bytes": 0}
        self.assertEqual(CapabilityProbe.check([artifact])["artifacts"][0]["status"], "invalid")

    def test_real_directory_measurement_is_readonly(self) -> None:
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "module").write_bytes(b"module")
        (bundle / "alias").symlink_to("module")
        artifact = {"artifact_id": "bundle", "content": measure_artifact(bundle)}
        before = {p.name: (p.lstat().st_mode, p.readlink().as_posix() if p.is_symlink() else p.read_bytes()) for p in bundle.iterdir()}
        self.assertTrue(CapabilityProbe.check([artifact])["artifacts_match"])
        after = {p.name: (p.lstat().st_mode, p.readlink().as_posix() if p.is_symlink() else p.read_bytes()) for p in bundle.iterdir()}
        self.assertEqual(after, before)

    def test_boolean_self_report_is_invalid(self) -> None:
        for value in (True, False, {"available": True}, {"result": "available"}):
            with self.subTest(value=value):
                result = CapabilityProbe.check([self.artifact], {"socket_events": value})
                row = next(r for r in result["capabilities"] if r["capability"] == "socket_events")
                self.assertEqual(row["status"], "invalid")
                self.assertFalse(result["launch_allowed"])

    def test_diagnostic_original_is_integrity_only_not_available(self) -> None:
        for supplied in ("available", "missing", "invalid", "unknown"):
            result = CapabilityProbe.check([self.artifact], {"socket_events": self.diagnostic(supplied)})
            row = next(r for r in result["capabilities"] if r["capability"] == "socket_events")
            self.assertEqual(row["status"], "unknown")
            self.assertEqual(row["origin"], "supplied_unverified")
            self.assertEqual(row["supplied_result"], supplied)

    def test_diagnostic_bad_hash_missing_file_and_symlink_are_invalid(self) -> None:
        ref = self.diagnostic()
        ref["evidence"]["sha256"] = "0" * 64
        self.assertEqual(CapabilityProbe.check([self.artifact], {"socket_events": ref})["capabilities"][2]["status"], "invalid")
        ref = self.diagnostic()
        path = Path(ref["evidence"]["path"])
        target = self.root / "unread"
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(CapabilityProbe.check([self.artifact], {"socket_events": ref})["capabilities"][2]["status"], "invalid")

    def test_invalid_names_and_unbounded_artifact_list_are_usage_errors(self) -> None:
        for artifacts, results in [([], None), ([self.artifact] * 17, None), ([self.artifact], {"invented": True})]:
            with self.subTest(artifacts=len(artifacts)):
                with self.assertRaises(CapabilityProbeError):
                    CapabilityProbe.check(artifacts, results)

    def test_boolean_byte_count_cannot_match_a_one_byte_artifact(self) -> None:
        self.binary.write_bytes(b"x")
        artifact = {"artifact_id": "single", "content": measure_artifact(self.binary)}
        artifact["content"]["bytes"] = True
        self.assertEqual(CapabilityProbe.check([artifact])["artifacts"][0]["status"], "invalid")

    def test_malformed_diagnostic_time_is_not_verified_as_available(self) -> None:
        ref = self.diagnostic()
        path = Path(ref["evidence"]["path"])
        value = json.loads(path.read_bytes())
        value["observed_at_utc"] = "not-a-time"
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        ref["evidence"]["sha256"] = hashlib.sha256(raw).hexdigest()
        ref["evidence"]["bytes"] = len(raw)
        result = CapabilityProbe.check([self.artifact], {"socket_events": ref})
        self.assertEqual(result["capabilities"][2]["status"], "invalid")
        self.assertFalse(result["formal_ready"])


if __name__ == "__main__":
    unittest.main()
