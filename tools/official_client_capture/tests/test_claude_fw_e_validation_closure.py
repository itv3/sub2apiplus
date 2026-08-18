"""Claude FW-E validation-only 闭合集生成测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.claude_fw_e_validation_closure import (
    PROVEN_NOT_TRAFFIC_SINK_ID,
    ValidationClosureError,
    build_validation_closure,
)
from tools.official_client_control.canonical import canonical_json_bytes


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


class ValidationClosureTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path]:
        source_root = root / "local-analysis/sources/claude-code-2.1.88"
        source_file = source_root / "src/client.ts"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("export const request = () => send();\n", encoding="utf-8")
        hitcc_root = root / "local-analysis/sources/hitcc-2.1.197/docs"
        mapped_doc = hitcc_root / "mapped.md"
        unmapped_doc = hitcc_root / "unmapped.md"
        mapped_doc.parent.mkdir(parents=True, exist_ok=True)
        mapped_doc.write_text("# 已映射\n\n- OAuth 请求使用 bearer。\n", encoding="utf-8")
        unmapped_doc.write_text(
            "# 未映射\n\n- 第一条请求命题。\n"
            "  这是同一条的续行。\n\n"
            "```text\n- 代码围栏内不是文档命题。\n```\n\n"
            "1. 第二条 payload 命题。\n",
            encoding="utf-8",
        )

        target_network = "TN-SINK-AAAABBBBCCCCDDDD-1"
        target_rows = [
            {
                "sink_id": target_network,
                "source_kind": "ast_call",
                "category": "fetch",
                "semantic_sha256": "1" * 64,
                "source_start": 10,
                "source_end": 30,
                "owner_symbol": "send",
            },
            {
                "sink_id": PROVEN_NOT_TRAFFIC_SINK_ID,
                "source_kind": "ast_call",
                "category": "anthropic_resource_call",
                "semantic_sha256": "2" * 64,
                "source_start": 40,
                "source_end": 66,
                "owner_symbol": "state",
            },
        ]
        target_inventory = root / "target-inventory.json"
        write_json(
            target_inventory,
            {
                "schema_version": "claude-code-target-sink-inventory/v1",
                "target_version": "2.1.226",
                "sink_total": 2,
                "sinks": target_rows,
            },
        )
        containment = root / "containment.json"
        write_json(
            containment,
            {
                "schema_version": "claude-code-fw-e-sink-containment-evidence/v1",
                "target_version": "2.1.226",
                "completeness": {
                    "result": "passed",
                    "unmatched_sink_ids": [],
                },
                "evidence": [
                    {
                        **target_rows[0],
                        "structural_finding": "exact_ast_call",
                        "structural_reason": "精确调用",
                        "source_window": {"sha256": "3" * 64, "excerpt": "fetch(url)"},
                        "call": {
                            "excerpt": "fetch(url)",
                            "callee_tail": ["fetch"],
                            "argument_shapes": ["identifier"],
                        },
                    },
                    {
                        **target_rows[1],
                        "structural_finding": "exact_ast_call",
                        "structural_reason": "精确调用",
                        "source_window": {
                            "sha256": "4" * 64,
                            "excerpt": "r.currentTurn.files.get(l)",
                        },
                        "call": {
                            "excerpt": "r.currentTurn.files.get(l)",
                            "callee_tail": ["r", "currentTurn", "files", "get"],
                            "argument_shapes": ["identifier"],
                        },
                    },
                ],
            },
        )
        source = root / "source.json"
        write_json(
            source,
            {
                "source_version": "2.1.88",
                "rules": [
                    {
                        "source_rule_id": "SRC2188-REQ-999",
                        "category": "请求",
                        "proposition": "历史请求通过 send 发出。",
                        "source_paths": ["src/client.ts:1"],
                    }
                ],
            },
        )
        hitcc = root / "hitcc.json"
        mapped_relative = mapped_doc.relative_to(root).as_posix()
        unmapped_relative = unmapped_doc.relative_to(root).as_posix()
        write_json(
            hitcc,
            {
                "source_version": "2.1.197",
                "clues": [
                    {
                        "clue_id": "HITCC-AUTH-999",
                        "proposition": "OAuth 请求使用 bearer。",
                        "source_path": mapped_relative,
                    }
                ],
                "document_inventory": [
                    {
                        "path": mapped_relative,
                        "disposition": "clue_source",
                        "mapping_status": "mapped",
                        "clue_ids": ["HITCC-AUTH-999"],
                    },
                    {
                        "path": unmapped_relative,
                        "disposition": "clue_source",
                        "mapping_status": "unmapped",
                        "clue_ids": [],
                    },
                ],
            },
        )
        prior = root / "prior.json"
        write_json(
            prior,
            {
                "schema_version": "claude-code-fw-e-cross-source-dispositions/v1",
                "target_version": "2.1.226",
                "target_sinks": [
                    {
                        "sink_id": row["sink_id"],
                        "traffic_class": "unknown",
                        "disposition": "unclassified",
                        "rationale": "待闭合。",
                        "spec_ids": [],
                        "scenario_ids": [],
                        "evidence_paths": ["containment.json"],
                        "migration_decision": "change",
                    }
                    for row in target_rows
                ],
                "historical_source_candidates": [
                    {
                        "source_rule_id": "SRC2188-REQ-999",
                        "disposition": "unclassified",
                        "spec_ids": [],
                        "rationale": "待闭合。",
                        "evidence_paths": ["source.json"],
                    }
                ],
                "hitcc_clues": [
                    {
                        "clue_id": "HITCC-AUTH-999",
                        "disposition": "unclassified",
                        "spec_ids": [],
                        "rationale": "待闭合。",
                        "evidence_paths": ["hitcc.json"],
                    }
                ],
                "hitcc_documents": [
                    {
                        "path": mapped_relative,
                        "disposition": "unclassified",
                        "spec_ids": [],
                        "rationale": "待闭合。",
                        "evidence_paths": [mapped_relative, "hitcc.json"],
                    },
                    {
                        "path": unmapped_relative,
                        "disposition": "unclassified",
                        "spec_ids": [],
                        "rationale": "待闭合。",
                        "evidence_paths": [unmapped_relative, "hitcc.json"],
                    },
                ],
                "runtime_observations": [],
            },
        )
        capture = root / "capture.json"
        write_json(
            capture,
            {
                "schema_version": "claude-code-fw-e-capture-index/v1",
                "target_version": "2.1.226",
                "result": "passed",
            },
        )
        producer = root / "validation-closure.py"
        producer.write_text("# 测试生产器\n", encoding="utf-8")
        return {
            "prior": prior,
            "target": target_inventory,
            "containment": containment,
            "source": source,
            "source_root": source_root,
            "hitcc": hitcc,
            "capture": capture,
            "producer": producer,
            "output": root / "output",
        }

    def _build(self, root: Path, paths: dict[str, Path]) -> dict:
        return build_validation_closure(
            workspace_root=root,
            prior_dispositions_path=paths["prior"],
            target_inventory_path=paths["target"],
            sink_containment_path=paths["containment"],
            source_2188_path=paths["source"],
            source_2188_root=paths["source_root"],
            hitcc_path=paths["hitcc"],
            capture_index_path=paths["capture"],
            output_root=paths["output"],
            producer_path=paths["producer"],
        )

    def test_builds_closed_validation_only_ledger_without_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            review = self._build(root, paths)
            self.assertEqual(review["prior_unclassified_total"], 6)
            self.assertEqual(review["final_unclassified_total"], 0)
            self.assertEqual(review["candidate_rule_count"], 5)
            self.assertEqual(
                review["candidate_counts_by_evidence_level"],
                {"blocked": 4, "observed": 1},
            )
            dispositions = json.loads(
                (paths["output"] / "dispositions.json").read_text()
            )
            self.assertEqual(
                dispositions["schema_version"],
                "claude-code-fw-e-cross-source-dispositions/v2",
            )
            false_positive = next(
                row
                for row in dispositions["target_sinks"]
                if row["sink_id"] == PROVEN_NOT_TRAFFIC_SINK_ID
            )
            self.assertEqual(false_positive["disposition"], "out_of_scope_proven")
            self.assertEqual(false_positive["traffic_class"], "not_traffic")
            atoms = json.loads((paths["output"] / "document-atoms.json").read_text())
            self.assertEqual(atoms["atom_count"], 2)
            texts = [
                atom["text"]
                for document in atoms["documents"]
                for atom in document["atoms"]
            ]
            self.assertIn("第一条请求命题。 这是同一条的续行。", texts)
            self.assertNotIn("代码围栏内不是文档命题。", texts)

    def test_rejects_drift_in_proven_non_traffic_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self._fixture(root)
            containment = json.loads(paths["containment"].read_text())
            false_positive = next(
                row
                for row in containment["evidence"]
                if row["sink_id"] == PROVEN_NOT_TRAFFIC_SINK_ID
            )
            false_positive["call"]["excerpt"] = "client.messages.get(l)"
            write_json(paths["containment"], containment)
            with self.assertRaisesRegex(
                ValidationClosureError, "非网络调用的结构证据发生漂移"
            ):
                self._build(root, paths)


if __name__ == "__main__":
    unittest.main()
