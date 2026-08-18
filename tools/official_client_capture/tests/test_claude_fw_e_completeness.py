"""Claude FW-E 目标原生 inventory 与四方闭集门禁测试。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_e_crosswalk import (
    CrosswalkError,
    build_matrix,
)
from tools.official_client_capture.claude_target_inventory import (
    TargetInventoryError,
    build_target_inventory,
)
from tools.official_client_control.canonical import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[3]
AST_TOOL = ROOT / "tools/official_client_capture/claude_bundle_ast.mjs"
TYPESCRIPT_MODULE = ROOT / "frontend/node_modules/typescript/lib/typescript.js"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def ast_inventory(bundle_sha256: str, sinks: list[dict]) -> dict:
    return {
        "schema_version": "claude-code-target-native-inventory/v1",
        "bundle": {"sha256": bundle_sha256},
        "parser": {"parse_diagnostics": []},
        "completeness": {"truncated": False},
        "sink_total": len(sinks),
        "sinks": sinks,
    }


def ast_sink(
    sink_id: str,
    semantic: str,
    start: int,
    end: int,
    category: str = "fetch",
) -> dict:
    return {
        "sink_id": sink_id,
        "category": category,
        "semantic_sha256": semantic,
        "source_start": start,
        "source_end": end,
        "owner_symbol": "main",
        "reachability": "unknown",
        "privacy_keys": [],
        "relevant_literals": [],
    }


def lexical_index(bundle_sha256: str, sinks: list[dict]) -> dict:
    return {
        "bundle_sha256": bundle_sha256,
        "sink_inventory_truncated": False,
        "sink_total": len(sinks),
        "sinks": sinks,
    }


def merged_inventory(
    version: str, bundle_sha256: str, platform: str, sinks: list[dict]
) -> dict:
    return {
        "schema_version": "claude-code-target-sink-inventory/v1",
        "target_version": version,
        "platform": platform,
        "bundle_sha256": bundle_sha256,
        "completeness": {
            "truncated": False,
            "ast_parse_diagnostic_count": 0,
            "ambiguous_lexical_match_count": 0,
            "duplicate_sink_id_count": 0,
        },
        "sink_total": len(sinks),
        "sinks": sinks,
    }


class TargetInventoryTests(unittest.TestCase):
    def test_merges_ast_and_keeps_lexical_only_candidates(self) -> None:
        bundle_sha = "a" * 64
        result = build_target_inventory(
            ast_inventory(
                bundle_sha,
                [ast_sink("TN-SINK-001", "b" * 64, 10, 30)],
            ),
            lexical_index(
                bundle_sha,
                [
                    {"kind": "fetch", "offset": 12, "nearest_symbol": "main"},
                    {"kind": "fetch", "offset": 80, "nearest_symbol": "docs"},
                ],
            ),
            target_version="2.1.226",
            platform="linux/amd64",
        )
        self.assertEqual(result["sink_total"], 2)
        self.assertEqual(result["counts"]["lexical_matched_ast_count"], 1)
        self.assertEqual(result["counts"]["lexical_only_count"], 1)
        self.assertEqual(
            {row["source_kind"] for row in result["sinks"]},
            {"ast_call", "lexical_only"},
        )
        self.assertFalse(result["completeness"]["truncated"])

    def test_rejects_duplicate_lexical_identity(self) -> None:
        bundle_sha = "a" * 64
        duplicate = {"kind": "fetch", "offset": 12, "nearest_symbol": "main"}
        with self.assertRaisesRegex(TargetInventoryError, "词法 sink 重复"):
            build_target_inventory(
                ast_inventory(
                    bundle_sha,
                    [ast_sink("TN-SINK-001", "b" * 64, 10, 30)],
                ),
                lexical_index(bundle_sha, [duplicate, duplicate]),
                target_version="2.1.226",
                platform="linux/amd64",
            )

    @unittest.skipUnless(TYPESCRIPT_MODULE.is_file(), "需要锁定的 TypeScript 解析器")
    def test_ast_inventory_never_truncates_over_200_sinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "cli.js"
            bundle.write_text(
                "function main(){" + ";".join(
                    f'fetch("https://example.com/{index}")' for index in range(205)
                ) + ";}main();",
                encoding="utf-8",
            )
            output = root / "ast.json"
            completed = subprocess.run(
                [
                    "node",
                    str(AST_TOOL),
                    "--bundle",
                    str(bundle),
                    "--output",
                    str(output),
                    "--expected-sha256",
                    hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "--typescript-module",
                    str(TYPESCRIPT_MODULE),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(output.read_text())
            self.assertEqual(result["sink_total"], 205)
            self.assertEqual(len(result["sinks"]), 205)
            self.assertFalse(result["completeness"]["truncated"])


class CrosswalkTests(unittest.TestCase):
    def _fixture(self, root: Path, *, invalid_privacy: bool = False) -> dict[str, Path]:
        baseline_sha = "1" * 64
        target_sha = "2" * 64
        baseline_sink = {
            "sink_id": "TN-SINK-BASE",
            "source_kind": "ast_call",
            "category": "fetch",
            "semantic_sha256": "3" * 64,
            "reachability": "unknown",
            "privacy_keys": [],
            "discovery_sources": ["ast"],
        }
        target_base = {**baseline_sink, "sink_id": "TN-SINK-TARGET"}
        target_new = {
            **baseline_sink,
            "sink_id": "TN-SINK-NEW",
            "semantic_sha256": "4" * 64,
            "category": "websocket",
        }
        source = root / "source.json"
        hitcc = root / "hitcc.json"
        ledger = root / "ledger.json"
        baseline_inventory = root / "baseline-inventory.json"
        target_inventory = root / "target-inventory.json"
        dispositions = root / "dispositions.json"
        capture = root / "capture.json"
        write_json(
            source,
            {
                "rules": [
                    {
                        "source_rule_id": "SRC-001",
                        "proposition": "历史源码命题",
                        "source_paths": ["src/client.ts:1"],
                        "target_static_status": "static_only",
                        "spec_rule_ids": ["SPEC-BASE-001"],
                    }
                ]
            },
        )
        write_json(
            hitcc,
            {
                "clues": [
                    {
                        "clue_id": "HITCC-001",
                        "proposition": "HitCC 命题",
                        "source_path": "docs/clue.md",
                        "source_lines": "L1",
                        "coverage": "covered",
                        "spec_rule_ids": ["SPEC-BASE-001"],
                    }
                ],
                "document_inventory": [],
            },
        )
        write_json(
            ledger,
            {
                "target_version": "2.1.220",
                "rules": [
                    {
                        "id": "SPEC-BASE-001",
                        "domain": "header",
                        "retained_claim": "历史规则",
                        "scope": "test",
                        "required_channels": ["J"],
                        "status": {"disposition": "verified"},
                    }
                ],
                "replacement_rules": [],
                "additional_rules": [],
            },
        )
        write_json(
            baseline_inventory,
            merged_inventory("2.1.220", baseline_sha, "linux/amd64", [baseline_sink]),
        )
        write_json(
            target_inventory,
            merged_inventory(
                "2.1.226", target_sha, "linux/amd64", [target_base, target_new]
            ),
        )
        write_json(
            capture,
            {
                "schema_version": "claude-code-fw-e-capture-index/v1",
                "target_version": "2.1.226",
                "result": "passed",
                "control": {
                    "privacy_controls": {
                        "required_values": {
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            "DISABLE_TELEMETRY": "1",
                        },
                        "case_count": 1,
                        "environment_manifest_sha256s": ["5" * 64],
                        "result": "passed",
                    }
                },
                "target": {
                    "capture_host_scopes": ["all"],
                    "privacy_controls": {
                        "required_values": {
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            "DISABLE_TELEMETRY": "1",
                        },
                        "case_count": 1,
                        "environment_manifest_sha256s": ["6" * 64],
                        "result": "passed",
                    },
                    "network_observations": [
                        {
                            "observation_id": "RUN-NET-001",
                            "transport": "http",
                            "method": "POST",
                            "scheme": "https",
                            "host": "api.anthropic.com",
                            "port": "443",
                            "path": "/v1/messages",
                        }
                    ],
                },
            },
        )
        first_disposition = "mapped_strict" if invalid_privacy else "mapped_strict"
        first_traffic = "telemetry" if invalid_privacy else "essential"
        write_json(
            dispositions,
            {
                "schema_version": "claude-code-fw-e-cross-source-dispositions/v1",
                "target_version": "2.1.226",
                "target_sinks": [
                    {
                        "sink_id": "TN-SINK-NEW",
                        "traffic_class": "essential",
                        "disposition": "mapped_strict",
                        "rationale": "目标原生新增发送点。",
                        "spec_ids": ["SPEC-NEW-001"],
                        "scenario_ids": ["SCENARIO-NEW"],
                        "evidence_paths": ["evidence/new.json"],
                        "migration_decision": "add",
                        "new_rule": {
                            "id": "SPEC-NEW-001",
                            "domain": "endpoint",
                            "retained_claim": "新增 WebSocket 端点",
                            "scope": "test",
                            "required_channels": ["J"],
                        },
                    },
                    {
                        "sink_id": "TN-SINK-TARGET",
                        "traffic_class": first_traffic,
                        "disposition": first_disposition,
                        "rationale": "目标继续承接历史规则。",
                        "spec_ids": ["SPEC-BASE-001"],
                        "scenario_ids": ["SCENARIO-BASE"],
                        "evidence_paths": ["evidence/base.json"],
                        "migration_decision": "change",
                    },
                ],
                "historical_source_candidates": [],
                "hitcc_clues": [],
                "hitcc_documents": [],
                "runtime_observations": [
                    {
                        "observation_id": "RUN-NET-001",
                        "disposition": "mapped_sink",
                        "sink_ids": ["TN-SINK-TARGET"],
                        "rationale": "运行请求映射到目标消息发送点。",
                        "evidence_paths": ["evidence/runtime.json"],
                    }
                ],
            },
        )
        return {
            "source": source,
            "hitcc": hitcc,
            "ledger": ledger,
            "baseline_inventory": baseline_inventory,
            "target_inventory": target_inventory,
            "dispositions": dispositions,
            "capture": capture,
        }

    def test_closed_crosswalk_can_add_target_native_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            matrix, closure = build_matrix(
                paths["source"],
                paths["hitcc"],
                paths["ledger"],
                paths["baseline_inventory"],
                paths["target_inventory"],
                "2.1.226",
                paths["dispositions"],
                paths["capture"],
            )
            self.assertEqual(closure["result"], "passed")
            self.assertEqual(closure["unresolved_total"], 0)
            self.assertEqual(closure["target_add_rule_count"], 1)
            self.assertIn(
                "SPEC-NEW-001", {row["id"] for row in matrix["target_rules"]}
            )

    def test_privacy_disabled_sink_cannot_be_silently_strict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory), invalid_privacy=True)
            with self.assertRaisesRegex(CrosswalkError, "只能 record_only"):
                build_matrix(
                    paths["source"],
                    paths["hitcc"],
                    paths["ledger"],
                    paths["baseline_inventory"],
                    paths["target_inventory"],
                    "2.1.226",
                    paths["dispositions"],
                    paths["capture"],
                )

    def test_missing_dispositions_keep_closure_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            _matrix, closure = build_matrix(
                paths["source"],
                paths["hitcc"],
                paths["ledger"],
                paths["baseline_inventory"],
                paths["target_inventory"],
                "2.1.226",
                None,
                None,
            )
            self.assertEqual(closure["result"], "blocked")
            self.assertGreater(closure["unresolved_total"], 0)
            self.assertEqual(
                closure["unresolved"]["runtime_capture_scope"],
                ["capture_index_missing"],
            )


if __name__ == "__main__":
    unittest.main()
