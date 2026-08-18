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
from tools.official_client_capture.claude_fw_e_dispositions import (
    DispositionError,
    build_dispositions,
)
from tools.official_client_capture.claude_target_inventory import (
    TargetInventoryError,
    build_target_inventory,
)
from tools.official_client_control.canonical import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[3]
AST_TOOL = ROOT / "tools/official_client_capture/claude_bundle_ast.mjs"
CONTAINMENT_TOOL = ROOT / "tools/official_client_capture/claude_sink_containment.mjs"
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

    @unittest.skipUnless(TYPESCRIPT_MODULE.is_file(), "需要锁定的 TypeScript 解析器")
    def test_ast_inventory_keeps_dynamic_url_http_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "cli.js"
            bundle.write_text(
                "function endpoint(){return base + '/settings';}"
                "async function main(){const url=endpoint();"
                "await client.get(url,{headers:{Authorization:'Bearer x'},timeout:1000});}"
                "main();",
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
            self.assertEqual(result["sink_total"], 1)
            self.assertEqual(
                result["sinks"][0]["category"], "http_client_method_candidate"
            )

    @unittest.skipUnless(TYPESCRIPT_MODULE.is_file(), "需要锁定的 TypeScript 解析器")
    def test_containment_rebinds_calls_and_proves_literal_is_not_traffic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "cli.js"
            source = (
                'async function main(){await fetch("https://example.test/value");}'
                'const docs="fetch is only documentation";main();'
            )
            bundle.write_text(source, encoding="utf-8")
            bundle_sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
            ast_output = root / "ast.json"
            ast_completed = subprocess.run(
                [
                    "node",
                    str(AST_TOOL),
                    "--bundle",
                    str(bundle),
                    "--output",
                    str(ast_output),
                    "--expected-sha256",
                    bundle_sha,
                    "--typescript-module",
                    str(TYPESCRIPT_MODULE),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(ast_completed.returncode, 0, ast_completed.stderr)
            ast_result = json.loads(ast_output.read_text())
            ast_rows = [
                {
                    **row,
                    "source_kind": "ast_call",
                    "discovery_sources": ["ast"],
                }
                for row in ast_result["sinks"]
            ]
            literal_start = source.index("fetch is only documentation")
            lexical_row = {
                "sink_id": "TN-LEXICAL-LITERAL",
                "source_kind": "lexical_only",
                "category": "fetch",
                "semantic_sha256": "8" * 64,
                "source_start": literal_start,
                "source_end": literal_start + len("fetch"),
                "discovery_sources": ["lexical"],
            }
            inventory = root / "inventory.json"
            write_json(
                inventory,
                merged_inventory(
                    "2.1.226",
                    bundle_sha,
                    "linux/amd64",
                    ast_rows + [lexical_row],
                ),
            )
            containment = root / "containment.json"
            completed = subprocess.run(
                [
                    "node",
                    str(CONTAINMENT_TOOL),
                    "--bundle",
                    str(bundle),
                    "--inventory",
                    str(inventory),
                    "--ast-inventory",
                    str(ast_output),
                    "--output",
                    str(containment),
                    "--typescript-module",
                    str(TYPESCRIPT_MODULE),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(containment.read_text())
            self.assertEqual(result["completeness"]["result"], "passed")
            self.assertEqual(
                {row["structural_finding"] for row in result["evidence"]},
                {"exact_ast_call", "non_executable_literal"},
            )


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
                "channels": ["P", "R", "J", "M"],
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
                    "version": "2.1.226",
                    "scenarios": ["s1"],
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
                "relay": {
                    "result": "passed",
                    "target": {
                        "version": "2.1.226",
                        "probe_ids": ["r-s1"],
                    },
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
                        "scenario_ids": ["s1"],
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
                        "scenario_ids": ["s1"],
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

    def test_clue_source_document_is_part_of_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            hitcc = json.loads(paths["hitcc"].read_text())
            hitcc["document_inventory"] = [
                {
                    "path": "docs/direct-clue.md",
                    "disposition": "clue_source",
                    "mapping_status": "unmapped",
                    "clue_ids": [],
                }
            ]
            write_json(paths["hitcc"], hitcc)
            _matrix, closure = build_matrix(
                paths["source"],
                paths["hitcc"],
                paths["ledger"],
                paths["baseline_inventory"],
                paths["target_inventory"],
                "2.1.226",
                paths["dispositions"],
                paths["capture"],
            )
            self.assertEqual(
                closure["unresolved"]["hitcc_document_paths"],
                ["docs/direct-clue.md"],
            )

    def test_closed_mode_requires_every_disposition_to_be_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            with self.assertRaisesRegex(CrosswalkError, "未逐项显式处置"):
                build_matrix(
                    paths["source"],
                    paths["hitcc"],
                    paths["ledger"],
                    paths["baseline_inventory"],
                    paths["target_inventory"],
                    "2.1.226",
                    paths["dispositions"],
                    paths["capture"],
                    require_explicit=True,
                )

    def test_explicit_review_and_evidence_closure_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            dispositions = json.loads(paths["dispositions"].read_text())
            dispositions["historical_source_candidates"] = [
                {
                    "source_rule_id": "SRC-001",
                    "disposition": "unclassified",
                    "spec_ids": [],
                    "rationale": "已逐项审阅，但仍缺少目标版本语义证据。",
                    "evidence_paths": ["evidence/source.json"],
                }
            ]
            dispositions["hitcc_clues"] = [
                {
                    "clue_id": "HITCC-001",
                    "disposition": "mapped_historical",
                    "spec_ids": ["SPEC-BASE-001"],
                    "rationale": "线索已映射到历史规则。",
                    "evidence_paths": ["evidence/hitcc.json"],
                }
            ]
            write_json(paths["dispositions"], dispositions)
            _matrix, closure = build_matrix(
                paths["source"],
                paths["hitcc"],
                paths["ledger"],
                paths["baseline_inventory"],
                paths["target_inventory"],
                "2.1.226",
                paths["dispositions"],
                paths["capture"],
                require_explicit=True,
            )
            self.assertEqual(closure["explicit_dispositions"]["result"], "passed")
            self.assertEqual(closure["result"], "blocked")
            self.assertEqual(closure["unresolved_total"], 1)

    def test_missing_relay_r_keeps_runtime_scope_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            capture = json.loads(paths["capture"].read_text())
            capture.pop("relay")
            capture["channels"] = ["P", "J", "M"]
            write_json(paths["capture"], capture)
            _matrix, closure = build_matrix(
                paths["source"],
                paths["hitcc"],
                paths["ledger"],
                paths["baseline_inventory"],
                paths["target_inventory"],
                "2.1.226",
                paths["dispositions"],
                paths["capture"],
            )
            self.assertEqual(
                closure["unresolved"]["runtime_capture_scope"],
                ["target_required_channels_missing", "target_relay_r_incomplete"],
            )


class DispositionBuilderTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path]:
        bundle = root / "cli.js"
        bundle.write_text(
            'async function main(){await fetch("https://example.test/value");}main();',
            encoding="utf-8",
        )
        semantic_sha = "7" * 64
        target_sink = {
            "sink_id": "TN-SINK-001",
            "source_kind": "ast_call",
            "category": "fetch",
            "semantic_sha256": semantic_sha,
            "reachability": "unknown",
            "privacy_keys": [],
            "discovery_sources": ["ast"],
        }
        source = root / "source.json"
        hitcc = root / "hitcc.json"
        ledger = root / "ledger.json"
        baseline_inventory = root / "baseline-inventory.json"
        target_inventory = root / "target-inventory.json"
        containment = root / "containment.json"
        capture = root / "capture.json"
        policy = root / "policy.json"
        write_json(
            source,
            {
                "rules": [
                    {
                        "source_rule_id": "SRC-UNKNOWN",
                        "target_static_status": "missing",
                        "spec_rule_ids": [],
                    }
                ]
            },
        )
        write_json(
            hitcc,
            {
                "clues": [
                    {
                        "clue_id": "HITCC-UNKNOWN",
                        "coverage": "missing",
                        "spec_rule_ids": [],
                    }
                ],
                "document_inventory": [
                    {
                        "path": "docs/unresolved-clue.md",
                        "disposition": "clue_source",
                        "mapping_status": "unmapped",
                        "clue_ids": ["HITCC-UNKNOWN"],
                    }
                ],
            },
        )
        write_json(
            ledger,
            {
                "target_version": "2.1.220",
                "rules": [],
                "replacement_rules": [],
                "additional_rules": [],
            },
        )
        write_json(baseline_inventory, {"target_version": "2.1.220"})
        write_json(
            target_inventory,
            merged_inventory(
                "2.1.226",
                hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "linux/amd64",
                [target_sink],
            ),
        )
        write_json(
            containment,
            {
                "schema_version": "claude-code-fw-e-sink-containment-evidence/v1",
                "target_version": "2.1.226",
                "inventory": {
                    "path": str(target_inventory),
                    "sha256": hashlib.sha256(target_inventory.read_bytes()).hexdigest(),
                },
                "bundle": {
                    "path": str(bundle),
                    "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                    "byte_count": len(bundle.read_bytes()),
                },
                "completeness": {
                    "result": "passed",
                    "target_sink_count": 1,
                    "evidence_row_count": 1,
                },
                "evidence": [
                    {
                        "sink_id": "TN-SINK-001",
                        "semantic_sha256": semantic_sha,
                        "nearest_function": {
                            "start": 0,
                            "end": len(bundle.read_text(encoding="utf-8")),
                        },
                    }
                ],
            },
        )
        privacy = {
            "required_values": {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
            },
            "result": "passed",
        }
        write_json(
            capture,
            {
                "schema_version": "claude-code-fw-e-capture-index/v1",
                "target_version": "2.1.226",
                "result": "passed",
                "channels": ["P", "R", "J", "M"],
                "control": {"privacy_controls": privacy},
                "target": {
                    "version": "2.1.226",
                    "scenarios": ["s1"],
                    "capture_host_scopes": ["all"],
                    "privacy_controls": privacy,
                    "network_observations": [
                        {"observation_id": "RUN-NET-001"}
                    ],
                },
                "relay": {
                    "result": "passed",
                    "target": {
                        "version": "2.1.226",
                        "probe_ids": ["r-s1"],
                    },
                },
            },
        )
        inputs = {
            "source_2_1_88": source,
            "hitcc_2_1_197": hitcc,
            "historical_rules": ledger,
            "historical_sink_inventory": baseline_inventory,
            "target_sink_inventory": target_inventory,
            "sink_containment": containment,
            "runtime_capture_index": capture,
        }
        write_json(
            policy,
            {
                "schema_version": "claude-code-fw-e-disposition-review-policy/v1",
                "policy_id": "test-policy",
                "target_version": "2.1.226",
                "input_sha256": {
                    key: hashlib.sha256(path.read_bytes()).hexdigest()
                    for key, path in inputs.items()
                },
                "spec_sets": {},
                "target_sinks": {
                    "mapped_strict": [],
                    "mapped_managed": [],
                    "record_only_disabled": [],
                },
                "historical_source_candidates": {"mapped_managed": []},
                "hitcc_clues": {"mapped_managed": []},
                "runtime_observations": [
                    {
                        "observation_id": "RUN-NET-001",
                        "sink_ids": ["TN-SINK-001"],
                        "rationale": "合成运行坐标映射到唯一调用点。",
                    }
                ],
            },
        )
        return {
            "policy": policy,
            "source": source,
            "hitcc": hitcc,
            "ledger": ledger,
            "baseline_inventory": baseline_inventory,
            "target_inventory": target_inventory,
            "containment": containment,
            "capture": capture,
        }

    def _build(self, paths: dict[str, Path]) -> tuple[dict, dict, dict]:
        return build_dispositions(
            policy_path=paths["policy"],
            source_path=paths["source"],
            hitcc_path=paths["hitcc"],
            ledger_path=paths["ledger"],
            baseline_inventory_path=paths["baseline_inventory"],
            target_inventory_path=paths["target_inventory"],
            containment_path=paths["containment"],
            capture_path=paths["capture"],
        )

    def test_builder_explicitly_covers_every_denominator_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dispositions, review, blockers = self._build(
                self._fixture(Path(directory))
            )
            self.assertEqual(review["denominator_total"], 5)
            self.assertEqual(review["explicit_disposition_total"], 5)
            self.assertEqual(review["result"], "passed")
            self.assertEqual(blockers["blocker_total"], 4)
            self.assertEqual(dispositions["target_sinks"][0]["disposition"], "unclassified")

    def test_unknown_items_remain_unclassified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dispositions, _review, _blockers = self._build(
                self._fixture(Path(directory))
            )
            for group in (
                "target_sinks",
                "historical_source_candidates",
                "hitcc_clues",
                "hitcc_documents",
            ):
                self.assertEqual(dispositions[group][0]["disposition"], "unclassified")

    def test_record_only_requires_direct_gate_in_nearest_function(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            policy = json.loads(paths["policy"].read_text())
            policy["target_sinks"]["record_only_disabled"] = [
                {
                    "sink_id": "TN-SINK-001",
                    "traffic_class": "nonessential",
                    "required_gate_symbols": ["va()"],
                    "migration_decision": "change",
                    "rationale": "只有直接 gate 才能签发 record-only。",
                }
            ]
            write_json(paths["policy"], policy)
            with self.assertRaisesRegex(DispositionError, "缺少直接 gate"):
                self._build(paths)

    def test_input_sha_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            source = json.loads(paths["source"].read_text())
            source["rules"][0]["target_static_status"] = "changed"
            write_json(paths["source"], source)
            with self.assertRaisesRegex(DispositionError, "摘要与实际输入不一致"):
                self._build(paths)

    def test_duplicate_policy_sink_in_same_group_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self._fixture(Path(directory))
            policy = json.loads(paths["policy"].read_text())
            row = {
                "sink_id": "TN-SINK-001",
                "migration_decision": "change",
                "rationale": "合成 managed 辅助路径。",
            }
            policy["target_sinks"]["mapped_managed"] = [row, row]
            write_json(paths["policy"], policy)
            with self.assertRaisesRegex(DispositionError, "mapped_managed sink_id 重复"):
                self._build(paths)


if __name__ == "__main__":
    unittest.main()
