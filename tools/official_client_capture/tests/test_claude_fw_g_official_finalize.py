"""验证 Claude FW-G 独立官方复测的映射与脱敏门禁。"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.official_client_capture import claude_fw_g_official_finalize as finalizer


ROOT = Path(__file__).resolve().parents[3]


class ClaudeFWGOfficialFinalizeTests(unittest.TestCase):
    """使用无秘密合成数据验证 40/110 和 593 项闭合。"""

    @staticmethod
    def _manifest() -> dict:
        return finalizer.load_json(
            ROOT / "tools/official_client_capture/claude_required_rules_2_1_226.json"
        )

    @staticmethod
    def _measured_entry(spec_id: str) -> dict:
        domain = "tls" if spec_id.startswith("SPEC-TLS-") else "body"
        channels = ["M", "P"] if domain == "tls" else ["M", "R"]
        refs = [
            {
                "path": f"fw-g/evidence/{spec_id}.json",
                "channel": channel,
                "sha256": ("a" if channel == "M" else "b") * 64,
                "bytes": 100,
            }
            for channel in channels
        ]
        return {
            "spec_id": spec_id,
            "assertion_id": f"PAIR-{spec_id}",
            "assertion_result": "passed",
            "evidence_level": "observed",
            "domain": domain,
            "egress_ids": ["egress-claude-messages-inference"],
            "compatibility_class": "request_egress",
            "evidence_channels": channels,
            "evidence_refs": refs,
            "official_positive": {
                "assertion_id": f"PAIR-{spec_id}-POSITIVE",
                "kind": "official_positive",
                "scenarios": ["baseline"],
                "sample_count": 1,
                "result": "passed",
            },
            "official_negative": {
                "assertion_id": f"PAIR-{spec_id}-NEGATIVE",
                "kind": "zero_violation_denominator",
                "scenarios": ["baseline"],
                "sample_count": 1,
                "violation_count": 0,
                "result": "passed",
            },
        }

    def test_maps_all_atomic_assertions_once(self) -> None:
        manifest = self._manifest()
        required, profile_owner, scenario_owner = finalizer.validate_required_rules(
            manifest
        )
        all_ids = sorted(set(profile_owner) | set(scenario_owner))
        measured = {"entries": [self._measured_entry(value) for value in all_ids]}
        atomic = finalizer.build_atomic_verification(
            measured, profile_owner, scenario_owner
        )
        rules = finalizer.build_required_rule_verification(required, atomic)
        self.assertEqual(atomic["atomic_assertion_count"], 110)
        self.assertEqual(atomic["profile_atomic_assertion_count"], 106)
        self.assertEqual(atomic["scenario_only_assertion_count"], 4)
        self.assertEqual(rules["required_rule_count"], 40)
        self.assertEqual(rules["promotion_eligibility"], "blocked_until_implementation_pair_and_negative_gates")

    def test_rejects_unowned_atomic_assertion(self) -> None:
        manifest = self._manifest()
        _, profile_owner, scenario_owner = finalizer.validate_required_rules(manifest)
        measured = {
            "entries": [
                self._measured_entry(value)
                for value in sorted(set(profile_owner) | set(scenario_owner))
            ]
        }
        measured["entries"][0]["spec_id"] = "SPEC-UNOWNED-001"
        measured["entries"][0]["assertion_id"] = "PAIR-SPEC-UNOWNED-001"
        with self.assertRaisesRegex(finalizer.OfficialFinalizeError, "无归属"):
            finalizer.build_atomic_verification(
                measured, profile_owner, scenario_owner
            )

    def test_secret_scan_rejects_oauth_callback(self) -> None:
        documents = {
            "bad.json": {
                "value": "abcdefghijklmnopqrstuvwx#ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
            }
        }
        with self.assertRaisesRegex(finalizer.OfficialFinalizeError, "oauth_callback"):
            finalizer.scan_documents(documents)

    def test_compacts_all_orthogonal_candidates(self) -> None:
        dimensions = {
            "result": "passed",
            "resolved_count": 49,
            "unresolved_count": 0,
            "disposition_counts": {"rule_bound": 49},
            "entries": [
                {
                    "dimension": f"dimension-{index:02d}",
                    "status": "resolved",
                    "disposition": "rule_bound",
                    "binding_ids": ["SPEC-BODY-001"],
                }
                for index in range(49)
            ],
        }
        candidates = {
            "result": "passed",
            "resolved_count": 593,
            "unresolved_count": 0,
            "group_counts": {"synthetic": 593},
            "disposition_counts": {"supporting_fact_bound": 593},
            "entries": [
                {
                    "candidate_id": f"candidate-{index:03d}",
                    "candidate_group": "synthetic",
                    "status": "resolved",
                    "disposition": "supporting_fact_bound",
                    "binding_ids": [f"FACT-{index:03d}"],
                    "proposition": "该正文不得进入可携带制品",
                }
                for index in range(593)
            ],
        }
        closure = finalizer.build_orthogonal_closure(dimensions, candidates)
        self.assertEqual(closure["candidate_count"], 593)
        self.assertEqual(closure["unresolved_count"], 0)
        self.assertTrue(
            all("proposition" not in value for value in closure["candidates"])
        )


if __name__ == "__main__":
    unittest.main()
