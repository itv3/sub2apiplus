"""v2 逐规则编排：check 全集契约、分模式绑定与 inventory 逻辑路径。

accept 只接受 status="pass"；机器结果的 check 集合必须与批准画像的 check
全集逐项一致，否则"官方通过、候选也通过"比较的根本不是同一件事。
candidate_profile 规则不携带官方侧机器结果，官方权威是批准画像链摘要。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_rule_assertion_results as builder  # noqa: E402


def _document(checks: list[dict], rule_id: str = "SPEC-TLS-001") -> dict:
    return {
        "schema_version": builder.SINGLE_SCHEMA,
        "rule_id": rule_id,
        "status": "pass",
        "checks": checks,
    }


PASSING = [
    {
        "id": "scenario-artifact-coverage",
        "passed": True,
        "evidence_paths": ["mitm/a.pcap"],
    },
    {"id": "cipher-count", "passed": True, "evidence_paths": ["mitm/a.pcap"]},
    {"id": "no-alpn", "passed": True, "evidence_paths": ["mitm/a.pcap"]},
]
EXPECTED_CHECKS = ["scenario-artifact-coverage", "cipher-count", "no-alpn"]
AUTHORITY = {
    "assertion_profile_sha256": "a" * 64,
    "classification_package_digest": "b" * 64,
    "review_sha256": "c" * 64,
}


class CheckClosureTest(unittest.TestCase):
    def test_check_closure_matches_contract(self) -> None:
        builder.verify_check_closure(
            _document(PASSING), EXPECTED_CHECKS, rule_id="SPEC-TLS-001", label="候选"
        )

    def test_missing_check_rejected(self) -> None:
        with self.assertRaises(builder.RuleAssertionError):
            builder.verify_check_closure(
                _document(PASSING[:2]),
                EXPECTED_CHECKS,
                rule_id="SPEC-TLS-001",
                label="候选",
            )

    def test_failed_check_rejected(self) -> None:
        checks = [dict(PASSING[0]), dict(PASSING[1]), dict(PASSING[2], passed=False)]
        with self.assertRaises(builder.RuleAssertionError):
            builder.verify_check_closure(
                _document(checks), EXPECTED_CHECKS, rule_id="SPEC-TLS-001", label="候选"
            )

    def test_extra_check_rejected(self) -> None:
        checks = PASSING + [
            {"id": "surprise", "passed": True, "evidence_paths": ["mitm/a.pcap"]}
        ]
        with self.assertRaises(builder.RuleAssertionError):
            builder.verify_check_closure(
                _document(checks), EXPECTED_CHECKS, rule_id="SPEC-TLS-001", label="候选"
            )


class EvidenceBindingTest(unittest.TestCase):
    def test_bindings_use_inventory_logical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mitm").mkdir()
            (root / "mitm" / "a.pcap").write_bytes(b"evidence")
            refs = builder.collect_evidence_bindings(
                _document(PASSING), root, "oauth-campaign"
            )
            self.assertEqual(refs[0]["path"], "oauth-campaign/mitm/a.pcap")
            self.assertEqual(len(refs[0]["sha256"]), 64)

    def test_missing_evidence_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(builder.RuleAssertionError):
                builder.collect_evidence_bindings(
                    _document(PASSING), root, "oauth-campaign"
                )

    def test_empty_prefix_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mitm").mkdir()
            (root / "mitm" / "a.pcap").write_bytes(b"x")
            with self.assertRaises(builder.RuleAssertionError):
                builder.collect_evidence_bindings(_document(PASSING), root, "  ")


class OfficialAuthorityTest(unittest.TestCase):
    def test_valid_authority_accepted(self) -> None:
        self.assertEqual(builder.validate_official_authority(AUTHORITY), AUTHORITY)

    def test_missing_field_rejected(self) -> None:
        broken = dict(AUTHORITY)
        del broken["review_sha256"]
        with self.assertRaises(builder.RuleAssertionError):
            builder.validate_official_authority(broken)

    def test_non_sha_rejected(self) -> None:
        with self.assertRaises(builder.RuleAssertionError):
            builder.validate_official_authority(
                {**AUTHORITY, "review_sha256": "short"}
            )


class RuleResultTest(unittest.TestCase):
    def test_dual_wire_result_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mitm").mkdir()
            (root / "mitm" / "a.pcap").write_bytes(b"evidence")
            official_out = root / "o.json"
            candidate_out = root / "c.json"
            official_out.write_text(json.dumps(_document(PASSING)), encoding="utf-8")
            candidate_out.write_text(json.dumps(_document(PASSING)), encoding="utf-8")
            result = builder.build_dual_wire_result(
                rule_id="SPEC-TLS-001",
                official_expected_check_ids=EXPECTED_CHECKS,
                candidate_expected_check_ids=EXPECTED_CHECKS,
                official=(["python3", "assert.py"], _document(PASSING), official_out),
                candidate=(["python3", "assert.py"], _document(PASSING), candidate_out),
                official_root=root,
                candidate_root=root,
                official_prefix="official-run",
                candidate_prefix="candidate-run",
                results_root=root,
                rationale="双侧独立执行并通过",
            )
            self.assertEqual(result["validation_mode"], "dual_wire")
            self.assertEqual(result["status"], "pass")
            self.assertNotIn("positive_assertions", result)
            self.assertEqual(
                result["official_evidence_refs"][0]["path"], "official-run/mitm/a.pcap"
            )
            self.assertEqual(
                result["candidate_evidence_refs"][0]["path"],
                "candidate-run/mitm/a.pcap",
            )

    def test_candidate_profile_result_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mitm").mkdir()
            (root / "mitm" / "a.pcap").write_bytes(b"evidence")
            candidate_out = root / "c.json"
            document = _document(PASSING, rule_id="SPEC-EP-006")
            candidate_out.write_text(json.dumps(document), encoding="utf-8")
            result = builder.build_candidate_profile_result(
                rule_id="SPEC-EP-006",
                expected_check_ids=EXPECTED_CHECKS,
                candidate=(["python3", "assert.py"], document, candidate_out),
                candidate_root=root,
                candidate_prefix="candidate-run",
                official_authority=AUTHORITY,
                results_root=root,
                rationale="候选侧机器断言通过",
            )
            self.assertEqual(result["validation_mode"], "candidate_profile")
            self.assertNotIn("official_machine_result", result)
            self.assertEqual(result["official_authority"], AUTHORITY)


class ResultsDocumentTest(unittest.TestCase):
    def test_document_fields_and_ordering(self) -> None:
        document = builder.build_results_document(
            candidate_id="candidate-x",
            target_version="0.147.0",
            profile_id="codex-0.147.0-official-k30-v1",
            profile_digest="0" * 64,
            official_package_digest="1" * 64,
            candidate_package_digest="2" * 64,
            comparison_package_digest="3" * 64,
            acceptance_contract_sha256_value="4" * 64,
            rules=[{"rule": "SPEC-WS-004"}, {"rule": "SPEC-TLS-001"}],
        )
        self.assertEqual(document["schema_version"], builder.RESULTS_SCHEMA_V2)
        self.assertEqual(document["acceptance_contract_sha256"], "4" * 64)
        self.assertEqual(
            [item["rule"] for item in document["rules"]],
            ["SPEC-TLS-001", "SPEC-WS-004"],
        )

    def test_empty_rules_rejected(self) -> None:
        with self.assertRaises(builder.RuleAssertionError):
            builder.build_results_document(
                candidate_id="candidate-x",
                target_version="0.147.0",
                profile_id="p",
                profile_digest="0" * 64,
                official_package_digest="1" * 64,
                candidate_package_digest="2" * 64,
                comparison_package_digest="3" * 64,
                acceptance_contract_sha256_value="4" * 64,
                rules=[],
            )


class MachineLayoutTest(unittest.TestCase):
    def test_campaign_layout_matches_compare_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary).resolve()
            results_dir = campaign / "assertions/candidate-x/machine"
            results_dir.mkdir(parents=True)
            root, official, candidate = builder.resolve_machine_layout(
                {
                    "campaign_dir": str(campaign),
                    "candidate_id": "candidate-x",
                },
                results_dir,
            )
            self.assertEqual(root, campaign)
            self.assertEqual(official, results_dir / "official")
            self.assertEqual(candidate, results_dir / "candidate")
            self.assertTrue(official.is_dir())
            self.assertTrue(candidate.is_dir())

    def test_campaign_layout_rejects_noncanonical_results_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary).resolve()
            wrong = campaign / "machine"
            wrong.mkdir()
            with self.assertRaises(builder.RuleAssertionError):
                builder.resolve_machine_layout(
                    {
                        "campaign_dir": str(campaign),
                        "candidate_id": "candidate-x",
                    },
                    wrong,
                )


if __name__ == "__main__":
    unittest.main()
