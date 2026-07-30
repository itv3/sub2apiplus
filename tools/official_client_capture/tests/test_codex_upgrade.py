"""Codex CLI 升级编排器的离线门禁。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.codex_upgrade import (
    build_coverage,
    compare_inventory,
    compare_surfaces,
    load_rule_manifest,
    scan_evidence,
    scan_source_tree,
)
from tools.official_client_capture.codex_upgrade import Job


class CodexUpgradeTest(unittest.TestCase):
    def test_0145_rule_manifest_contains_exact_required_scope(self) -> None:
        manifest = (
            Path(__file__).resolve().parents[1]
            / "codex_upgrade_rules_0_145_0.json"
        )
        rules = load_rule_manifest(manifest, "0.145.0")
        self.assertEqual(len(rules), 42)
        self.assertIn("SPEC-EP-023", rules)
        self.assertNotIn("SPEC-H2-001", rules)
        self.assertNotIn("SPEC-WS-003", rules)
        self.assertNotIn("SPEC-BODY-007", rules)

    def test_source_inventory_detects_endpoint_and_dependency_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            for source in (baseline, target):
                (source / "src").mkdir(parents=True)
            (baseline / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.12.28"\n',
                encoding="utf-8",
            )
            (target / "Cargo.lock").write_text(
                '[[package]]\nname = "reqwest"\nversion = "0.13.0"\n',
                encoding="utf-8",
            )
            (baseline / "src/client.rs").write_text(
                'client.post("/backend-api/codex/responses").send();\n',
                encoding="utf-8",
            )
            (target / "src/client.rs").write_text(
                (
                    'client.post("/backend-api/codex/responses").send();\n'
                    'client.post("/backend-api/codex/new-egress").send();\n'
                ),
                encoding="utf-8",
            )
            baseline_inventory = scan_source_tree(baseline, "0.145.0")
            target_inventory = scan_source_tree(target, "0.146.0")
            difference = compare_inventory(
                baseline_inventory, target_inventory
            )
            added_values = {
                item["value"] for item in difference["added"]
            }
            self.assertIn(
                "/backend-api/codex/new-egress", added_values
            )
            self.assertTrue(
                any("reqwest|0.13.0" in value for value in added_values)
            )

    def test_dynamic_surface_detects_new_route_and_body_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline"
            target = root / "target"
            baseline.mkdir()
            target.mkdir()
            common = {
                "method": "POST",
                "path": "/backend-api/codex/responses",
                "http_version": "HTTP/1.1",
                "headers": [["version", "0.145.0"], ["host", "chatgpt.com"]],
                "json_shape": {"model": "<string>", "input": []},
            }
            (baseline / "surface.json").write_text(
                json.dumps({"records": [{"request": common}]}),
                encoding="utf-8",
            )
            new_request = {
                **common,
                "path": "/backend-api/codex/new-egress?token=secret",
                "json_shape": {"model": "<string>", "new_field": True},
            }
            (target / "surface.json").write_text(
                json.dumps(
                    {"records": [{"request": common}, {"request": new_request}]}
                ),
                encoding="utf-8",
            )
            baseline_surface = scan_evidence([baseline], "baseline")
            target_surface = scan_evidence([target], "target")
            difference = compare_surfaces(baseline_surface, target_surface)
            self.assertEqual(difference["added_count"], 1)
            self.assertEqual(
                difference["added"][0]["path"],
                "/backend-api/codex/new-egress?token",
            )
            serialized = json.dumps(difference, ensure_ascii=False)
            self.assertNotIn("secret", serialized)

    def test_raw_mitm_body_is_reduced_to_shape_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = {
                "request": {
                    "method": "POST",
                    "path": "/backend-api/codex/responses",
                    "http_version": "HTTP/1.1",
                    "headers": [["authorization", "SECRET-TOKEN"]],
                    "body": {
                        "text": json.dumps(
                            {
                                "model": "gpt-test",
                                "input": [{"role": "user", "content": "SECRET-TEXT"}],
                            }
                        )
                    },
                }
            }
            (root / "capture.jsonl").write_text(
                json.dumps(record) + "\n",
                encoding="utf-8",
            )
            result = scan_evidence([root], "candidate")
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("SECRET-TOKEN", serialized)
            self.assertNotIn("SECRET-TEXT", serialized)
            self.assertEqual(result["surface_count"], 1)
            self.assertIsNotNone(
                result["surfaces"][0]["body_shape_sha256"]
            )

    def test_h2_request_signature_includes_host_and_query_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = {
                "connections": [
                    {
                        "protocol": "h2",
                        "frames": [
                            {
                                "type": "HEADERS",
                                "header_names_in_order": [
                                    ":method",
                                    ":scheme",
                                    ":authority",
                                    ":path",
                                    "content-type",
                                ],
                                "headers": [
                                    {"name": ":method", "value": "POST"},
                                    {"name": ":scheme", "value": "https"},
                                    {
                                        "name": ":authority",
                                        "value": "api.openai.com",
                                    },
                                    {
                                        "name": ":path",
                                        "value": "/v1/new?token=<redacted>",
                                    },
                                    {
                                        "name": "content-type",
                                        "value": "application/json",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
            (root / "relay.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            result = scan_evidence([root], "h2")
            self.assertEqual(result["surface_count"], 1)
            surface = result["surfaces"][0]
            self.assertEqual(surface["host"], "api.openai.com")
            self.assertEqual(surface["path"], "/v1/new?token")
            self.assertEqual(surface["protocol"], "h2")
            self.assertEqual(
                surface["header_names"], summary["connections"][0]["frames"][0]["header_names_in_order"]
            )

    def test_rule_coverage_requires_official_and_candidate_evidence(self) -> None:
        official = Job(
            job_id="official",
            phase="official",
            suites=("full",),
            description="official",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        candidate = Job(
            job_id="candidate",
            phase="candidate",
            suites=("full",),
            description="candidate",
            steps=(),
            evidence_roots=(),
            covers=("SPEC-H1-001",),
        )
        incomplete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "failed"},
            ],
        )
        self.assertFalse(incomplete["complete"])
        complete = build_coverage(
            ("SPEC-H1-001",),
            [official, candidate],
            [
                {"id": "official", "status": "complete"},
                {"id": "candidate", "status": "complete"},
            ],
        )
        self.assertTrue(complete["complete"])


if __name__ == "__main__":
    unittest.main()
