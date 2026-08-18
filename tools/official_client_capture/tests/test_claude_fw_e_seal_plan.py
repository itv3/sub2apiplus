"""Claude FW-E 封存计划生成测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_e_seal_plan import (
    SealPlanError,
    build_seal_plan,
)
from tools.official_client_control.canonical import canonical_json_bytes


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


class SealPlanTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, object]:
        route_source = root / "backend/routes.go"
        route_source.parent.mkdir(parents=True)
        route_source.write_text('gateway.POST("/messages", handler)\n', encoding="utf-8")
        egress_source = root / "backend/messages.go"
        egress_source.write_text("package backend\n", encoding="utf-8")
        ingress_catalog = root / "ingress.json"
        write_json(
            ingress_catalog,
            {
                "schema_version": 1,
                "campaign_stage": "FW-E",
                "source_bindings": [
                    {
                        "path": "backend/routes.go",
                        "required_markers": ['gateway.POST("/messages"'],
                    }
                ],
                "aliases": [
                    {
                        "alias_id": "alias-messages",
                        "logical_ingress_id": "messages-oauth",
                        "physical_route": "POST /v1/messages",
                        "caller_ids": ["handler.Messages"],
                    }
                ],
                "entries": [
                    {
                        "logical_ingress_id": "messages-oauth",
                        "physical_alias_ids": ["alias-messages"],
                        "caller_ids": ["handler.Messages"],
                        "adapter_id": "anthropic-messages-legacy",
                        "route_id": "claude-oauth-legacy",
                        "ingress_kind": "official",
                        "protocol_class": "anthropic-messages",
                        "current_disposition": "retained_legacy",
                    }
                ],
            },
        )
        egress_catalog = root / "egress.json"
        write_json(
            egress_catalog,
            {
                "schema_version": 1,
                "campaign_stage": "FW-E",
                "entries": [
                    {
                        "sink_id": "unclassified.claude.messages_inference",
                        "purpose": "claude_legacy.messages_inference",
                        "source_ref": "backend/messages.go:*Messages",
                        "endpoint_evidence": "external_persona",
                        "routes": [
                            {
                                "method": "POST",
                                "host": "api.anthropic.com",
                                "path": "/v1/messages",
                                "protocol": "https",
                            }
                        ],
                        "target_backend": "http_upstream",
                        "legacy_backends": ["http_upstream"],
                        "owner": "official-client-fw-e",
                        "expiry_condition": "FW-H 完成迁移",
                    }
                ],
            },
        )
        freeze_root = root / "freeze"
        artifact = freeze_root / "official/package.tgz"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"artifact")
        registry = freeze_root / "registry.json"
        write_json(registry, {"stable": "2.1.226"})
        write_json(
            freeze_root / "freeze.json",
            {
                "schema_version": "claude-code-fw-e-official-freeze/v1",
                "target_version": "2.1.226",
                "stable_at_start": "2.1.226",
                "stable_at_end": "2.1.226",
                "platforms": ["linux/amd64"],
                "entrypoint": "sdk-cli",
                "default_conditions": ["privacy=essential-traffic"],
                "registry_snapshot_path": "registry.json",
                "artifacts": [{"tarball_path": "official/package.tgz"}],
            },
        )
        assessments = root / "assessments.json"
        write_json(
            assessments,
            {
                "schema_version": "claude-code-fw-e-rule-assessments/v2",
                "target_version": "2.1.226",
                "rule_count": 1,
                "rules": [
                    {
                        "spec_id": "SPEC-001",
                        "evidence_level": "observed",
                        "rule_lifecycle": "candidate",
                        "compatibility_class": "request_egress",
                        "migration_decision": "change",
                        "decision_basis": "inheritance_not_proven",
                        "semantic_equivalence_proven": False,
                        "evidence_paths": ["rule.json"],
                        "applicability": ["platform=linux/amd64"],
                    }
                ],
            },
        )
        ordinary_paths: dict[str, Path] = {}
        for name in (
            "target.json",
            "matrix.json",
            "closure.json",
            "capture.json",
            "contract.json",
            "fw-c.json",
            "runtime.json",
            "inventory-evidence.json",
            "rule.json",
        ):
            path = root / name
            write_json(path, {"name": name})
            ordinary_paths[name] = path
        return {
            "freeze_root": freeze_root,
            "assessments": assessments,
            "ingress": ingress_catalog,
            "egress": egress_catalog,
            **ordinary_paths,
        }

    def _build(self, root: Path, paths: dict[str, object]) -> dict:
        return build_seal_plan(
            workspace_root=root,
            campaign_id="claude-fw-e-test",
            source_commit="1" * 40,
            freeze_root=paths["freeze_root"],
            rule_assessments_path=paths["assessments"],
            target_inventory_paths=[paths["target.json"]],
            cross_source_matrix_path=paths["matrix.json"],
            completeness_closure_path=paths["closure.json"],
            capture_index_path=paths["capture.json"],
            ingress_catalog_path=paths["ingress"],
            egress_catalog_path=paths["egress"],
            contract_source_paths=[paths["contract.json"]],
            fw_c_receipt_paths=[paths["fw-c.json"]],
            runtime_catalog_paths=[paths["runtime.json"]],
            inventory_evidence_paths=[paths["inventory-evidence.json"]],
            platforms=["linux/amd64"],
            created_at_utc="2026-08-18T00:00:00Z",
            discovered_at_utc="2026-08-18T00:00:01Z",
            inventory_observed_at_utc="2026-08-18T00:00:02Z",
            output_path=root / "plan.json",
        )

    def test_builds_current_fact_plan_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            plan = self._build(root, paths)
            self.assertEqual(plan["schema_version"], "official-client-fw-e-seal-plan/v3")
            self.assertEqual(len(plan["ingress_aliases"]), 1)
            self.assertEqual(len(plan["egress_entries"]), 1)
            self.assertEqual(
                plan["egress_entries"][0]["current_guard_state"], "legacy_observe"
            )
            self.assertEqual(
                plan["egress_entries"][0]["current_disposition"],
                "non_persona_managed",
            )
            self.assertEqual(
                {row["target_disposition"] for row in plan["target_proposals"]},
                {"migrated_strict", "persona_strict"},
            )

    def test_rejects_physical_route_marker_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            (root / "backend/routes.go").write_text("package backend\n", encoding="utf-8")
            with self.assertRaisesRegex(SealPlanError, "物理路由标记漂移"):
                self._build(root, paths)

    def test_rejects_unsorted_alias_callers_before_store_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            ingress = json.loads(Path(paths["ingress"]).read_text(encoding="utf-8"))
            ingress["aliases"][0]["caller_ids"] = ["z.caller", "a.caller"]
            write_json(Path(paths["ingress"]), ingress)
            with self.assertRaisesRegex(SealPlanError, "caller_ids 未排序或重复"):
                self._build(root, paths)

    def test_rejects_entry_caller_closure_drift_before_store_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            ingress = json.loads(Path(paths["ingress"]).read_text(encoding="utf-8"))
            ingress["aliases"][0]["caller_ids"].append("server.routes.Register")
            ingress["aliases"][0]["caller_ids"].sort()
            write_json(Path(paths["ingress"]), ingress)
            with self.assertRaisesRegex(SealPlanError, "caller_ids 与物理别名不闭合"):
                self._build(root, paths)


if __name__ == "__main__":
    unittest.main()
