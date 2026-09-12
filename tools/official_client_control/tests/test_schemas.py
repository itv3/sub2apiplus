"""机器 Schema 与运行时合同版本的最小一致性门禁。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.official_client_control import contracts


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(contracts.__file__).with_name("schemas")

    def test_all_schema_files_are_strict_json(self) -> None:
        paths = sorted(self.root.glob("*.json"))
        self.assertEqual(len(paths), 13)
        for path in paths:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(value["title"])

    def test_core_schema_versions_match_runtime_contract(self) -> None:
        bootstrap = json.loads((self.root / "bootstrap.schema.json").read_text())
        campaign = json.loads((self.root / "campaign.schema.json").read_text())
        fact = json.loads((self.root / "fact.schema.json").read_text())
        receipt = json.loads((self.root / "receipt.schema.json").read_text())
        migration = json.loads(
            (self.root / "rule-migration-ledger.schema.json").read_text()
        )
        atomic = json.loads(
            (self.root / "atomic-assertion-ledger.schema.json").read_text()
        )
        self.assertEqual(
            bootstrap["properties"]["schema_version"]["const"],
            contracts.BOOTSTRAP_SCHEMA,
        )
        self.assertEqual(
            campaign["properties"]["schema_version"]["const"],
            contracts.CAMPAIGN_SCHEMA,
        )
        self.assertEqual(
            fact["properties"]["schema_version"]["const"], contracts.FACT_SCHEMA
        )
        self.assertEqual(
            receipt["$defs"]["promotion"]["properties"]["schema_version"]["const"],
            contracts.PROMOTION_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            receipt["$defs"]["activation"]["properties"]["schema_version"]["const"],
            contracts.ACTIVATION_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            receipt["$defs"]["candidateBuild"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_BUILD_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            receipt["$defs"]["validationGate"]["properties"]["schema_version"]["const"],
            contracts.VALIDATION_GATE_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            receipt["$defs"]["candidateDelivery"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_DELIVERY_RECEIPT_SCHEMA,
        )
        self.assertEqual(
            migration["properties"]["schema_version"]["const"],
            contracts.RULE_MIGRATION_LEDGER_SCHEMA,
        )
        self.assertEqual(
            atomic["properties"]["schema_version"]["const"],
            contracts.ATOMIC_ASSERTION_LEDGER_SCHEMA,
        )
        validation = json.loads(
            (self.root / "validation-workflow.schema.json").read_text()
        )
        delivery = json.loads(
            (self.root / "candidate-delivery.schema.json").read_text()
        )
        self.assertEqual(
            validation["$defs"]["plan"]["properties"]["schema_version"]["const"],
            contracts.VALIDATION_EXECUTION_PLAN_SCHEMA,
        )
        self.assertEqual(
            validation["$defs"]["evidence"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_EVIDENCE_PACKAGE_SCHEMA,
        )
        self.assertEqual(
            delivery["$defs"]["plan"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_DELIVERY_PLAN_SCHEMA,
        )
        self.assertEqual(
            delivery["$defs"]["package"]["properties"]["schema_version"]["const"],
            contracts.CANDIDATE_DELIVERY_PACKAGE_SCHEMA,
        )

    def test_scenario_schema_distinguishes_legacy_and_strict_payloads(self) -> None:
        scenario = json.loads(
            (self.root / "scenario-pair.schema.json").read_text()
        )
        alternatives = scenario["oneOf"]
        legacy = alternatives[0]
        strict = alternatives[1]
        self.assertFalse(legacy["additionalProperties"])
        self.assertNotIn("schema_version", legacy["properties"])
        self.assertFalse(strict["additionalProperties"])
        self.assertEqual(
            strict["properties"]["schema_version"]["const"],
            contracts.SCENARIO_STAGE_V2_SCHEMA,
        )
        self.assertIn("validation_attempt_ref", strict["required"])


if __name__ == "__main__":
    unittest.main()
