"""Claude Code 2.1.226 FW-F 实测规则台账测试。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.claude_fw_f_measured_rules import (
    DEFAULT_EGRESS_ID,
    FORBIDDEN_RULE_IDS,
    RULE_EGRESS_IDS,
    RULE_DEFINITIONS,
    MeasuredRuleError,
    build_ledger,
)
from tools.official_client_capture.claude_fw_f_profile import (
    guide_rule_ids,
    validate_required_rule_manifest,
)


ROOT = Path(__file__).resolve().parents[3]
DISCOVERY_POLICY_PATH = ROOT / "tools/official_client_capture/claude_fw_f_discovery_policy_2_1_226.json"
PROFILE_POLICY_PATH = ROOT / "tools/official_client_capture/claude_fw_f_profile_policy_2_1_226.json"
CAMPAIGN_ROOT = (
    ROOT
    / "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/runtime-relay-205d7f58f/campaign"
)
IDENTITY_PATH = CAMPAIGN_ROOT / "identity.json"
RELAY_INDEX_PATH = CAMPAIGN_ROOT / "indexes/relay-index.json"
GUIDE_PATH = ROOT / "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md"
RULE_MANIFEST_PATH = (
    ROOT / "tools/official_client_capture/claude_required_rules_2_1_226.json"
)
FINAL_LEDGER_PATH = (
    ROOT
    / "local-analysis/fw-f/claude-code-2.1.226/discovery-clearance-v5-final/measured-rule-ledger.json"
)


def load(path: Path) -> dict[str, object]:
    """读取测试所需的受管 JSON。"""

    return json.loads(path.read_text(encoding="utf-8"))


class MeasuredRuleLedgerTests(unittest.TestCase):
    """验证活动规则只能来自当前版本真实 R/M 断言。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.discovery_policy = load(DISCOVERY_POLICY_PATH)
        cls.profile_policy = load(PROFILE_POLICY_PATH)

    def _build(self) -> dict[str, object]:
        return build_ledger(
            copy.deepcopy(self.discovery_policy),
            copy.deepcopy(self.profile_policy),
            IDENTITY_PATH,
            RELAY_INDEX_PATH,
        )

    def test_all_rules_bind_raw_r_and_m_evidence(self) -> None:
        ledger = self._build()
        self.assertEqual(ledger["result"], "passed")
        self.assertEqual(ledger["rule_count"], len(RULE_DEFINITIONS))
        self.assertEqual(
            [value["spec_id"] for value in ledger["entries"]],
            sorted(RULE_DEFINITIONS),
        )
        self.assertEqual(
            ledger["evidence_level_counts"],
            {"observed": len(RULE_DEFINITIONS), "verified": 0},
        )
        self.assertFalse(set(RULE_DEFINITIONS) & FORBIDDEN_RULE_IDS)
        for rule in ledger["entries"]:
            with self.subTest(spec_id=rule["spec_id"]):
                self.assertEqual(rule["assertion_result"], "passed")
                self.assertEqual(rule["compatibility_class"], "request_egress")
                self.assertEqual(
                    rule["egress_ids"],
                    sorted(RULE_EGRESS_IDS.get(rule["spec_id"], (DEFAULT_EGRESS_ID,))),
                )
                self.assertEqual(
                    rule["sample_scope"]["matched_count"],
                    rule["sample_scope"]["eligible_count"],
                )
                self.assertIn(
                    "binary_sha256=4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555",
                    rule["applicability"],
                )
                self.assertEqual(set(rule["evidence_channels"]), {"M", "R"})
                self.assertTrue(
                    any(
                        value["channel"] == "R" and value["path"].endswith(".bin")
                        for value in rule["evidence_refs"]
                    )
                )
                self.assertTrue(any(value["channel"] == "M" for value in rule["evidence_refs"]))
        self.assertEqual(
            ledger["evidence_boundaries"],
            {
                "native_pcap_present": False,
                "clienthello_fingerprint_rules_allowed": False,
                "client_alpn_offer_observed": False,
                "traffic_presence_comparison": "excluded_by_official_privacy_configuration",
                "unmeasured_feature_rules_allowed": False,
            },
        )

    def test_missing_raw_r_binding_is_rejected(self) -> None:
        with mock.patch(
            "tools.official_client_capture.claude_fw_f_measured_rules.sample_evidence",
            return_value=[],
        ):
            with self.assertRaisesRegex(MeasuredRuleError, "缺少真实 R 证据"):
                self._build()

    def test_clienthello_and_alpn_offer_cannot_enter_rule_set(self) -> None:
        for forbidden_id in ("SPEC-TLS-001", "SPEC-TLS-002"):
            with self.subTest(spec_id=forbidden_id):
                discovery_policy = copy.deepcopy(self.discovery_policy)
                discovery_policy["measured_rule_ids"] = sorted(RULE_DEFINITIONS)
                discovery_policy["measured_rule_ids"][-1] = forbidden_id
                discovery_policy["measured_rule_ids"] = sorted(
                    set(discovery_policy["measured_rule_ids"])
                )
                with self.assertRaisesRegex(MeasuredRuleError, "不得重新预设|规则闭集不一致|禁用规则"):
                    build_ledger(
                        discovery_policy,
                        copy.deepcopy(self.profile_policy),
                        IDENTITY_PATH,
                        RELAY_INDEX_PATH,
                    )

    def test_guide_required_rules_and_atomic_ledger_are_reconciled(self) -> None:
        ledger = load(FINAL_LEDGER_PATH)
        manifest = load(RULE_MANIFEST_PATH)
        validate_required_rule_manifest(manifest, ledger)
        spec_ids = guide_rule_ids(GUIDE_PATH)
        self.assertEqual(
            spec_ids,
            sorted(value["spec_id"] for value in manifest["required_rules"]),
        )
        self.assertEqual(len(spec_ids), 40)
        mapped_atomic_ids = sorted(
            [
                assertion_id
                for value in manifest["required_rules"]
                for assertion_id in value["atomic_assertion_ids"]
            ]
            + [
                assertion_id
                for value in manifest["scenario_only_groups"]
                for assertion_id in value["atomic_assertion_ids"]
            ]
        )
        self.assertEqual(
            mapped_atomic_ids,
            sorted(value["spec_id"] for value in ledger["entries"]),
        )
        self.assertEqual(len(mapped_atomic_ids), 110)
        self.assertEqual(
            sum(value["domain"] == "tls" for value in ledger["entries"]),
            3,
        )


if __name__ == "__main__":
    unittest.main()
