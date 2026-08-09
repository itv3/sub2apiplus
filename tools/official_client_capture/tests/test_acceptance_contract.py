"""ACC-01 验收契约必须完全由画像推导，禁止手写并且失败关闭。

25／17 分组、分侧覆盖矩阵与 check 全集是 accept 的判据来源；契约与画像
任何一侧漂移都必须立即暴露，不能靠人工核对清单。
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import acceptance_contract as contract  # noqa: E402
import candidate_test_trace  # noqa: E402


def _load_repository_profile() -> dict:
    return contract.load_profile(contract.repository_profile_path())


class AcceptanceContractDerivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = _load_repository_profile()
        cls.payload = contract.build_contract_payload(cls.profile)

    def test_frozen_contract_matches_repository_profile(self) -> None:
        payload = contract.verify_frozen_contract(self.profile)
        self.assertEqual(
            contract.contract_sha256(payload), contract.FROZEN_CONTRACT_SHA256
        )

    def test_rule_groups_are_25_dual_wire_and_17_candidate_profile(self) -> None:
        counts = self.payload["rule_counts"]
        self.assertEqual(counts[contract.MODE_DUAL_WIRE], 25)
        self.assertEqual(counts[contract.MODE_CANDIDATE_PROFILE], 17)
        modes = self.payload["validation_modes"]
        self.assertEqual(len(modes), 42)
        for rule_id in ("SPEC-TLS-001", "SPEC-TLS-003", "SPEC-PROTO-001"):
            self.assertEqual(modes[rule_id], contract.MODE_DUAL_WIRE)

    def test_side_coverage_matrix_is_derived_per_side(self) -> None:
        coverage = self.payload["side_coverage"]
        self.assertEqual(sorted(coverage), ["candidate", "official"])
        self.assertEqual(len(coverage["candidate"]), 15)
        self.assertEqual(
            sorted(coverage["official"]),
            [
                "A01",
                "A02",
                "A03",
                "A04",
                "A05",
                "A06",
                "A09",
                "A11",
                "A12",
                "A13",
                "A14",
                "A15",
            ],
        )
        official_kinds = {
            kind
            for kinds in coverage["official"].values()
            for kind in kinds
        }
        # 官方侧矩阵包含结构化 kind，正是 ACC-02b 派生器必须补齐的部分。
        self.assertIn("process_trace", official_kinds)
        self.assertIn("websocket_trace", official_kinds)

    def test_expected_check_ids_start_with_coverage_check(self) -> None:
        expected = self.payload["expected_check_ids"]
        self.assertEqual(len(expected), 42)
        for rule in self.profile["rules"]:
            check_ids = expected[rule["rule_id"]]
            self.assertEqual(check_ids[0], contract.COVERAGE_CHECK_ID)
            self.assertEqual(
                check_ids[1:], [check["id"] for check in rule["checks"]]
            )

    def test_internal_record_types_match_trace_producer(self) -> None:
        self.assertEqual(
            contract.INTERNAL_RECORD_TYPES,
            candidate_test_trace.ALLOWED_RECORD_TYPES,
        )
        self.assertFalse(
            contract.WIRE_RECORD_TYPES & contract.INTERNAL_RECORD_TYPES
        )

    def test_payload_digest_ignores_codex_version(self) -> None:
        mutated = copy.deepcopy(self.profile)
        mutated["codex_version"] = "0.147.0"
        self.assertEqual(
            contract.contract_sha256(contract.build_contract_payload(mutated)),
            contract.FROZEN_CONTRACT_SHA256,
        )


class AcceptanceContractMutationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = _load_repository_profile()

    def test_unknown_record_type_fails_closed(self) -> None:
        self.profile["rules"][0]["checks"][0]["select"]["record_type"] = (
            "novel_surface"
        )
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_missing_record_type_fails_closed(self) -> None:
        del self.profile["rules"][0]["checks"][0]["select"]["record_type"]
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_duplicate_rule_fails_closed(self) -> None:
        self.profile["rules"].append(copy.deepcopy(self.profile["rules"][0]))
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_rule_referencing_unknown_scenario_fails_closed(self) -> None:
        self.profile["rules"][0]["scenario_ids"] = ["A99"]
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_check_id_colliding_with_coverage_check_fails_closed(self) -> None:
        self.profile["rules"][0]["checks"][0]["id"] = contract.COVERAGE_CHECK_ID
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_duplicate_check_id_fails_closed(self) -> None:
        rule = self.profile["rules"][0]
        rule["checks"] = [rule["checks"][0], copy.deepcopy(rule["checks"][0])]
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_duplicate_scenario_definition_fails_closed(self) -> None:
        self.profile["scenarios"].append(
            copy.deepcopy(self.profile["scenarios"][0])
        )
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_duplicate_artifact_kind_fails_closed(self) -> None:
        scenario = self.profile["scenarios"][0]
        scenario["required_artifact_kinds"] = (
            list(scenario["required_artifact_kinds"])
            + [scenario["required_artifact_kinds"][0]]
        )
        with self.assertRaises(contract.AcceptanceContractError):
            contract.build_contract_payload(self.profile)

    def test_record_type_flip_breaks_frozen_digest(self) -> None:
        for rule in self.profile["rules"]:
            if rule["rule_id"] == "SPEC-TLS-001":
                rule["checks"][0]["select"]["record_type"] = "header_assembly"
        with self.assertRaises(contract.AcceptanceContractError):
            contract.verify_frozen_contract(self.profile)

    def test_wrong_profile_schema_rejected(self) -> None:
        mutated = dict(self.profile)
        mutated["schema_version"] = "codex-candidate-rule-expectations/v0"
        path = Path(self.enterContext(_temporary_profile(mutated)))
        with self.assertRaises(contract.AcceptanceContractError):
            contract.load_profile(path)


class _temporary_profile:
    """把变异画像落成临时文件供 load_profile 走完整读取路径。"""

    def __init__(self, document: dict) -> None:
        self._document = document
        self._path: Path | None = None

    def __enter__(self) -> str:
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(self._document, handle, ensure_ascii=False)
        handle.close()
        self._path = Path(handle.name)
        return str(self._path)

    def __exit__(self, *_args: object) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
