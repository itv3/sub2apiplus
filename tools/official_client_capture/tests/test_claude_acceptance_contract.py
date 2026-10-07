"""Claude 验收契约（升级控制面设计 B4-03）：分组、check 全集、侧别限定、分侧覆盖与契约摘要都由画像机器推导。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_acceptance_contract as contract
from tools.official_client_capture.tests import claude_evaluator_fixture as fixture


class ClaudeAcceptanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = fixture.build_profile()

    def test_modes_follow_record_types(self) -> None:
        modes = contract.derive_validation_modes(self.profile)
        self.assertEqual(modes, {"SPEC-FAIL-001": "dual_wire", "SPEC-HDR-001": "dual_wire", "SPEC-META-001": "candidate_profile", "SPEC-TLS-001": "dual_wire"})
        # 线上记录类型与取证运行时记录类型互斥，合起来是 checker 认的全部记录类型。
        self.assertEqual(contract.WIRE_RECORD_TYPES & contract.INTERNAL_RECORD_TYPES, frozenset())
        self.assertEqual(
            contract.KNOWN_RECORD_TYPES,
            {"claude_http_request", "claude_http_response", "relay_connection", "relay_intervention", "probe_run", "tls_client_hello", "runtime_receipt"},
        )
        self.assertEqual(contract.INTERNAL_RECORD_TYPES, {"runtime_receipt"})
        # 一条线上规则只要多选一种取证运行时记录，就整条改为 candidate_profile。
        mutated = json.loads(json.dumps(self.profile))
        rule = next(item for item in mutated["rules"] if item["rule_id"] == "SPEC-TLS-001")
        rule["checks"].append({**rule["checks"][0], "id": "spec-tls-001.receipt", "select": {"record_type": "runtime_receipt"}})
        self.assertEqual(contract.derive_validation_modes(mutated)["SPEC-TLS-001"], "candidate_profile")

    def test_expected_check_ids_put_coverage_first(self) -> None:
        expected = contract.derive_expected_check_ids(self.profile)
        self.assertEqual(
            expected["SPEC-HDR-001"],
            ["scenario-artifact-coverage", "spec-hdr-001.version-header", "spec-hdr-001.authorization-shape", "spec-hdr-002.body-key-prefix"],
        )
        for rule_id, ids in expected.items():
            with self.subTest(rule=rule_id):
                self.assertEqual(ids[0], contract.COVERAGE_CHECK_ID)
                self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_or_coverage_named_checks_are_rejected(self) -> None:
        for check_id in ("spec-hdr-001.version-header", contract.COVERAGE_CHECK_ID):
            mutated = json.loads(json.dumps(self.profile))
            rule = next(item for item in mutated["rules"] if item["rule_id"] == "SPEC-HDR-001")
            rule["checks"].append({**rule["checks"][0], "id": check_id})
            with self.subTest(check=check_id), self.assertRaisesRegex(contract.AcceptanceContractError, "check ID 重复"):
                contract.derive_expected_check_ids(mutated)

    def test_unknown_record_type_fails_closed(self) -> None:
        mutated = json.loads(json.dumps(self.profile))
        mutated["rules"][0]["checks"][0]["select"]["record_type"] = "codex_websocket_frame"
        with self.assertRaisesRegex(contract.AcceptanceContractError, "未登记的记录类型"):
            contract.build_contract_payload(mutated)

    def test_side_coverage_official_only_dual_wire(self) -> None:
        payload = contract.build_contract_payload(self.profile)
        self.assertEqual(sorted(payload["side_coverage"]["candidate"]), ["failure.529", "transport.http", "transport.native_tls"])
        # SPEC-META-001 是 candidate_profile，但它的场景 transport.http 也被 dual_wire 的 SPEC-HDR-001 引用。
        self.assertEqual(sorted(payload["side_coverage"]["official"]), ["failure.529", "transport.http", "transport.native_tls"])
        self.assertEqual(payload["side_coverage"]["candidate"]["failure.529"], ["relay_binary", "relay_manifest", "synthetic_intervention"])
        only_meta = json.loads(json.dumps(self.profile))
        only_meta["rules"] = [rule for rule in only_meta["rules"] if rule["rule_id"] == "SPEC-META-001"]
        coverage = contract.build_contract_payload(only_meta)["side_coverage"]
        self.assertEqual((sorted(coverage["candidate"]), coverage["official"]), (["transport.http"], {}))
        self.assertEqual(payload["rule_counts"], {"dual_wire": 3, "candidate_profile": 1})

    def test_contract_digest_is_version_free_and_content_bound(self) -> None:
        payload = contract.build_contract_payload(self.profile)
        self.assertNotIn("claude_version", json.dumps(payload))
        digest = contract.contract_sha256(payload)
        renamed = dict(self.profile, claude_version="2.1.999")
        self.assertEqual(contract.contract_sha256(contract.build_contract_payload(renamed)), digest)
        mutated = json.loads(json.dumps(self.profile))
        mutated["rules"][0]["checks"].pop()
        self.assertNotEqual(contract.contract_sha256(contract.build_contract_payload(mutated)), digest)

    def test_side_restricted_table_is_projected_and_fail_closed(self) -> None:
        self.assertEqual(contract.SIDE_RESTRICTED_CHECKS, {})
        table = {("SPEC-TLS-001", "spec-tls-001.no-alpn"): ("candidate",), ("SPEC-NOT-999", "x"): ("official",)}
        with mock.patch.object(contract, "SIDE_RESTRICTED_CHECKS", table):
            payload = contract.build_contract_payload(self.profile)
            # 画像里没有的规则跳过（规则可删），在画像里的规则只对登记侧生效。
            self.assertEqual(payload["side_restricted_checks"], {"SPEC-TLS-001": {"spec-tls-001.no-alpn": ["candidate"]}})
            self.assertEqual(contract.expected_check_ids_for_side(payload, "SPEC-TLS-001", "official"), ["scenario-artifact-coverage", "spec-tls-001.sni"])
            self.assertEqual(len(contract.expected_check_ids_for_side(payload, "SPEC-TLS-001", "candidate")), 3)
        with mock.patch.object(contract, "SIDE_RESTRICTED_CHECKS", {("SPEC-TLS-001", "spec-tls-001.missing"): ("candidate",)}):
            with self.assertRaisesRegex(contract.AcceptanceContractError, "不在画像里"):
                contract.build_contract_payload(self.profile)
        with self.assertRaisesRegex(contract.AcceptanceContractError, "验收侧非法"):
            contract.expected_check_ids_for_side(contract.build_contract_payload(self.profile), "SPEC-TLS-001", "both")

    def test_frozen_contract_verification(self) -> None:
        # 只登记了仓库基线 2.1.226；合成夹具的版本没有登记。
        self.assertEqual(set(contract.FROZEN_CONTRACT_SHA256S), {"2.1.226"})
        with self.assertRaisesRegex(contract.AcceptanceContractError, "没有登记"):
            contract.verify_frozen_contract(self.profile, fixture.TARGET_VERSION)
        digest = contract.contract_sha256(contract.build_contract_payload(self.profile))
        with mock.patch.dict(contract.FROZEN_CONTRACT_SHA256S, {fixture.TARGET_VERSION: digest}):
            self.assertEqual(contract.verify_frozen_contract(self.profile, fixture.TARGET_VERSION)["rule_counts"]["dual_wire"], 3)
        with mock.patch.dict(contract.FROZEN_CONTRACT_SHA256S, {fixture.TARGET_VERSION: "0" * 64}):
            with self.assertRaisesRegex(contract.AcceptanceContractError, "摘要漂移"):
                contract.verify_frozen_contract(self.profile, fixture.TARGET_VERSION)

    def test_cli_prints_digest_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="claude-contract-") as temporary:
            root = Path(temporary)
            profile_path = root / "profile.json"
            fixture.write_profile(profile_path)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(contract.main(["--profile", str(profile_path), "--print-digest"]), 0)
            self.assertEqual(output.getvalue().strip(), contract.contract_sha256(contract.build_contract_payload(self.profile)))
            target = root / "contract.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(contract.main(["--profile", str(profile_path), "--output", str(target)]), 0)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), contract.build_contract_payload(self.profile))
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(contract.main(["--profile", str(profile_path), "--output", str(target)]), 1)
                self.assertEqual(contract.main(["--profile", str(profile_path), "--verify-frozen", fixture.TARGET_VERSION]), 1)
            self.assertIn("拒绝覆盖", errors.getvalue())
            self.assertIn("没有登记", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
