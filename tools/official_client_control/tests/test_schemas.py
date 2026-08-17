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
        self.assertEqual(len(paths), 9)
        for path in paths:
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(value["title"])

    def test_core_schema_versions_match_runtime_contract(self) -> None:
        bootstrap = json.loads((self.root / "bootstrap.schema.json").read_text())
        campaign = json.loads((self.root / "campaign.schema.json").read_text())
        fact = json.loads((self.root / "fact.schema.json").read_text())
        receipt = json.loads((self.root / "receipt.schema.json").read_text())
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


if __name__ == "__main__":
    unittest.main()
