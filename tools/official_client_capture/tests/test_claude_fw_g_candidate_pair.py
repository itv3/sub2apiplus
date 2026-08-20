"""验证 Claude FW-G 候选 PAIR 的 40／106 映射与负例门禁。"""

from __future__ import annotations

import unittest
from pathlib import Path

from tools.official_client_capture import claude_fw_g_candidate_pair as pair


ROOT = Path(__file__).resolve().parents[3]


class ClaudeFWGCandidatePairTests(unittest.TestCase):
    """使用冻结 manifest 和无秘密合成结果验证候选晋升前门禁。"""

    @staticmethod
    def _inputs() -> tuple[dict, dict]:
        required = pair.load_json(
            ROOT / "tools/official_client_capture/claude_required_rules_2_1_226.json"
        )
        coverage = pair.load_json(
            ROOT
            / "tools/official_client_capture/claude_fw_g_implementation_coverage_2_1_226.json"
        )
        return required, coverage

    @staticmethod
    def _test_results(coverage_map: dict[str, dict]) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for entry in coverage_map.values():
            for anchor in entry["test_anchors"]:
                package = pair.go_package_for_path(anchor["path"])
                key = f"{package}#{anchor['symbol']}"
                results[key] = {
                    "package": package,
                    "test": anchor["symbol"],
                    "elapsed_seconds": 0.01,
                    "result": "passed",
                }
        for tests in pair.NEGATIVE_GATES.values():
            for package_suffix, test in tests:
                package = f"{pair.GO_MODULE}/{package_suffix}"
                results[f"{package}#{test}"] = {
                    "package": package,
                    "test": test,
                    "elapsed_seconds": 0.01,
                    "result": "passed",
                }
        return results

    def test_builds_exact_106_atomic_and_40_rule_pairs(self) -> None:
        required, coverage = self._inputs()
        rules, coverage_map, atomic_owner = pair.validate_rule_and_coverage(
            required, coverage
        )
        official_entries = [
            {
                "spec_id": atomic_id,
                "profile_required_rule_id": owner,
                "scenario_only_group_id": None,
                "official_retest_result": "passed",
            }
            for atomic_id, owner in sorted(atomic_owner.items())
        ]
        for group in required["scenario_only_groups"]:
            for atomic_id in group["atomic_assertion_ids"]:
                official_entries.append(
                    {
                        "spec_id": atomic_id,
                        "profile_required_rule_id": None,
                        "scenario_only_group_id": group["group_id"],
                        "official_retest_result": "passed",
                    }
                )
        official_atomic = {
            "result": "passed",
            "atomic_assertion_count": 110,
            "profile_atomic_assertion_count": 106,
            "entries": official_entries,
        }
        official_rules = {"result": "passed", "required_rule_count": 40}
        source_files = {
            anchor["path"]: {"path": anchor["path"], "sha256": "a" * 64, "bytes": 1}
            for entry in coverage_map.values()
            for field in ("implementation_anchors", "test_anchors")
            for anchor in entry[field]
        }
        atomic, rule_pairs, negatives = pair.build_pair_documents(
            official_atomic,
            official_rules,
            rules,
            coverage_map,
            atomic_owner,
            self._test_results(coverage_map),
            {"commit": "b" * 40, "tree": "c" * 40, "clean": True},
            source_files,
            {"path": "profile.json", "sha256": "d" * 64, "bytes": 1},
            {"path": "wire.json", "sha256": "e" * 64, "bytes": 1},
        )
        self.assertEqual(atomic["profile_atomic_pair_count"], 106)
        self.assertEqual(len(atomic["entries"]), 106)
        self.assertEqual(rule_pairs["required_rule_count"], 40)
        self.assertEqual(len(rule_pairs["entries"]), 40)
        self.assertEqual(negatives["unresolved_count"], 0)
        self.assertEqual(
            rule_pairs["promotion_eligibility"],
            "blocked_until_dmit_acceptance_and_rollback",
        )

    def test_rejects_missing_rule_test_result(self) -> None:
        required, coverage = self._inputs()
        _, coverage_map, _ = pair.validate_rule_and_coverage(required, coverage)
        go_tests: dict[tuple[str, str], dict] = {}
        with self.assertRaisesRegex(pair.CandidatePairError, "测试没有通过"):
            pair.validate_source_anchors(ROOT, coverage_map, go_tests)

    def test_rejects_non_backend_test_path(self) -> None:
        with self.assertRaisesRegex(pair.CandidatePairError, "backend/internal"):
            pair.go_package_for_path("tools/not-a-go-test.py")


if __name__ == "__main__":
    unittest.main()
