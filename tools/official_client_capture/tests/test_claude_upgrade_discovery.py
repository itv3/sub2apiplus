"""VC-1 发现补全（Claude 升级控制面设计 B2-07 补充项）：完整发现清单、语义候选与正交候选分母。

全部离线、确定性：

* 冻结的历史候选基线：计数（发现项 29＋71＋6937、三组历史候选 102＋71＋57）、语义候选目录摘要与冻结时一致、篡改即
  失败关闭；历史发现项与语义候选族的来源引用双向闭合。
* 目标原生发现（合成 SinkInventory 与 containment）：精确 AST 调用按固定词表归并语义候选、非精确调用不自动归并；词法命中按
  containment 结构证据判非流量，否则未决；入口未决项未决；人工显式处置覆盖任一发现项；containment 与目标清单不同源、
  不闭合或撞号一律拒绝；同一输入两次生成逐字节相同。
* 2.1.226 对照（设置 ``CLAUDE_2_1_226_DISCOVERY_FIXTURE`` 指向含 2.1.226 目标清单、sink containment、历史
  DiscoveryInventory 与历史完整候选分母副本的目录时运行）：限定到历史口径（FW-E 已显式处置的 174 个目标 sink 之外）时，
  发现项 7368、候选 593 与历史产物逐项相同；新口径把每个目标 sink 都列为发现项（7542／620）。
* 冻结可复算（设置 ``CLAUDE_2_1_226_BASELINE_SOURCE`` 指向含 2.1.226 历史产物的仓库根时运行）：重新冻结得到的字节与
  受管根里的基线逐字节相同。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_fw_e_validation_closure as fw_e_closure
from tools.official_client_capture import claude_upgrade_classify as classify
from tools.official_client_capture import claude_upgrade_discovery as discovery
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256

TARGET = "2.1.285"
FIXTURE_ENV = "CLAUDE_2_1_226_DISCOVERY_FIXTURE"
SOURCE_ENV = "CLAUDE_2_1_226_BASELINE_SOURCE"
INVENTORY_PATH = "evidence/campaigns/c1/analysis/target-inventory.json"
CONTAINMENT_PATH = "evidence/campaigns/c1/analysis/sink-containment.json"
INVENTORY_SHA = "1" * 64


def _inventory() -> dict[str, Any]:
    """两个精确 AST 调用、一个非精确 AST 调用、两个词法命中，加一个入口未决模块。"""

    def sink(sink_id: str, kind: str, category: str, module: str, start: int) -> dict[str, Any]:
        return {
            "sink_id": sink_id, "source_kind": kind, "category": category, "module": module,
            "source_start": start, "source_end": start + 40, "owner_symbol": f"sym{start}",
        }

    return {
        "schema_version": "claude-code-target-sink-inventory/v1",
        "target_version": TARGET,
        "sinks": [
            sink("TN-SINK-0001-1", "ast_call", "fetch_call", "0000-cli.js", 100),
            sink("TN-SINK-0002-1", "ast_call", "anthropic_sdk_resource", "0001-sdk.js", 200),
            sink("TN-SINK-0003-1", "ast_call", "http_client_method_candidate", "0002-chunk.js", 300),
            sink("TN-LEX-0001", "lexical_only", "lexical_candidate_messages_create", "0003-docs.js", 400),
            sink("TN-LEX-0002", "lexical_only", "lexical_candidate_fetch", "0002-chunk.js", 500),
        ],
        "unresolved_entries": [
            {"unresolved_id": "TN-UNRESOLVED-MOD-0001", "kind": "module", "module": "0009-lazy.js", "reason": "no_confirmed_entry_path"},
        ],
    }


def _containment(inventory_sha: str = INVENTORY_SHA) -> dict[str, Any]:
    rows = [
        {
            "sink_id": "TN-SINK-0001-1", "category": "fetch_call", "structural_finding": "exact_ast_call",
            "call": {"excerpt": 'fetch(`${base}/v1/messages`,{method:"POST",headers:{"anthropic-beta":b}})', "callee_tail": ["fetch"]},
        },
        {
            "sink_id": "TN-SINK-0002-1", "category": "anthropic_sdk_resource", "structural_finding": "exact_ast_call",
            "call": {"excerpt": "this._client.messages.countTokens(params)", "callee_tail": ["this", "_client", "messages", "countTokens"]},
        },
        {"sink_id": "TN-SINK-0003-1", "category": "http_client_method_candidate", "structural_finding": "ast_call_unmatched"},
        {"sink_id": "TN-LEX-0001", "category": "lexical_candidate_messages_create", "structural_finding": "non_executable_literal"},
        {"sink_id": "TN-LEX-0002", "category": "lexical_candidate_fetch", "structural_finding": "unresolved_identifier"},
    ]
    return {
        "schema_version": discovery.CONTAINMENT_SCHEMA,
        "target_version": TARGET,
        "inventory": {"path": "analysis/target-inventory.json", "sha256": inventory_sha},
        "completeness": {"target_sink_count": 5, "evidence_row_count": 5, "unmatched_sink_ids": [], "result": "passed"},
        "evidence": rows,
    }


def _layers(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "campaign_id": "c1",
        "target_version": TARGET,
        "inventory": _inventory(),
        "inventory_path": INVENTORY_PATH,
        "inventory_sha256": INVENTORY_SHA,
        "containment": _containment(),
        "containment_path": CONTAINMENT_PATH,
    }
    arguments.update(overrides)
    return discovery.build_discovery_layers(**arguments)


class HistoricalBaselineTests(unittest.TestCase):
    """冻结的历史候选基线：计数、目录摘要、篡改即失败关闭、双向闭合。"""

    def test_baseline_counts_and_binding(self) -> None:
        baseline = discovery.load_historical_baseline()
        self.assertEqual(len(baseline["items"]), 7037)
        kinds = {kind: sum(item["source_kind"] == kind for item in baseline["items"]) for kind in discovery.HISTORICAL_SOURCE_KINDS}
        self.assertEqual(kinds, {"historical_source_2_1_88": 29, "hitcc_clue_2_1_197": 71, "hitcc_document_atom_2_1_197": 6937})
        self.assertEqual(
            {group: len(rows) for group, rows in baseline["candidate_groups"].items()},
            {"source_mechanisms_2_1_88": 102, "hitcc_documents_2_1_197": 71, "historical_rules": 57},
        )
        raw = discovery.baseline_path().read_bytes()
        self.assertEqual(baseline["binding"]["sha256"], discovery._sha256(raw))
        self.assertEqual(baseline["binding"]["path"], discovery.BASELINE_RELATIVE)
        payload = json.loads(raw)
        self.assertEqual(payload["catalog_sha256"], discovery.catalog_sha256())
        self.assertEqual(len(fw_e_closure.SEMANTIC_CANDIDATE_CATALOG), 32)
        # 提取收据记录 2.1.226 当时的提取工具与原始资料摘要。
        receipt = payload["extraction_receipt"]
        self.assertEqual(
            receipt["producers"]["semantic_closure"]["path"], "tools/official_client_capture/claude_fw_e_validation_closure.py"
        )
        self.assertEqual(receipt["raw_sources"]["hitcc_2_1_197"]["git_commit"], "f4556e5b18a65232023998219e53c2598cc17d82")
        self.assertEqual(receipt["historical_rule_count"], 57)

    def test_tampered_baseline_fails_closed(self) -> None:
        payload = json.loads(discovery.baseline_path().read_bytes())

        def mutate_catalog(value: dict[str, Any]) -> None:
            value["catalog_sha256"] = "0" * 64

        def mutate_counts(value: dict[str, Any]) -> None:
            value["counts"]["discovery_items"] += 1

        def duplicate_item(value: dict[str, Any]) -> None:
            value["items"].insert(1, copy.deepcopy(value["items"][0]))

        def unknown_candidate(value: dict[str, Any]) -> None:
            value["items"][0][4] = ["CAND-NOT-IN-CATALOG"]

        def target_kind(value: dict[str, Any]) -> None:
            value["tables"]["source_kinds"][0] = "target_ast_call"

        cases = (
            ("目录摘要", mutate_catalog, "语义候选目录"),
            ("计数", mutate_counts, "声明的计数"),
            ("重复条目", duplicate_item, "排序且不重复"),
            ("目录外候选", unknown_candidate, "目录外的语义候选"),
            ("非历史来源", target_kind, "不是历史资料"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for label, mutate, pattern in cases:
                with self.subTest(label=label):
                    changed = copy.deepcopy(payload)
                    mutate(changed)
                    path = Path(directory) / f"{label}.json"
                    path.write_bytes(canonical_json_bytes(changed))
                    with self.assertRaisesRegex(discovery.DiscoveryCompletionError, pattern):
                        discovery.load_historical_baseline(path)

    def test_semantic_families_are_bidirectionally_closed(self) -> None:
        layers = _layers()
        items = layers["discovery_inventory"]["items"]
        families = layers["candidate_denominator"]["candidate_groups"][discovery.SEMANTIC_FAMILIES]
        candidates = [
            {"candidate_id": row["candidate_id"], "candidate_group": discovery.SEMANTIC_FAMILIES, "proposition": row["proposition"], "source_ids": row["source_ids"]}
            for row in families
        ]
        discoveries = [{"discovery_id": item["discovery_id"], "semantic_candidate_ids": item["semantic_candidate_ids"]} for item in items]
        self.assertEqual(classify._reference_problems(discoveries, candidates), [])
        self.assertEqual(len(families), 32)
        semantic = layers["semantic_candidates"]
        self.assertEqual(semantic["candidate_count"], 32)
        self.assertEqual(semantic["input_bindings"]["historical_baseline"], layers["historical_baseline"])


class TargetDiscoveryTests(unittest.TestCase):
    """目标原生发现、人工显式处置、分母与输入核对。"""

    def test_target_items_and_vc1_dispositions(self) -> None:
        layers = _layers()
        inventory = layers["discovery_inventory"]
        by_id = {item["discovery_id"]: item for item in inventory["items"]}
        self.assertEqual(inventory["item_count"], 6 + 7037)
        exact = by_id["TN-SINK-0001-1"]
        self.assertEqual((exact["source_kind"], exact["disposition"]), ("target_ast_call", "mapped_semantic_candidate"))
        self.assertTrue(exact["proposition"].startswith(f"目标 {TARGET} bundle 模块 0000-cli.js 存在 fetch_call 精确 AST 调用："))
        self.assertIn("CAND-EP-FULLSET", exact["semantic_candidate_ids"])
        self.assertEqual(exact["evidence_paths"], sorted([INVENTORY_PATH, CONTAINMENT_PATH]))
        self.assertEqual(exact["rationale"], discovery.TARGET_AST_RATIONALE)
        sdk = by_id["TN-SINK-0002-1"]
        self.assertTrue({"CAND-EP-COUNTTOKENS", "CAND-EP-SDK-SURFACE", "CAND-EP-FULLSET"} <= set(sdk["semantic_candidate_ids"]))
        unmatched = by_id["TN-SINK-0003-1"]
        self.assertEqual((unmatched["disposition"], unmatched["semantic_candidate_ids"]), ("unclassified", []))
        literal = by_id["TN-LEX-0001"]
        self.assertEqual((literal["source_kind"], literal["disposition"]), ("target_lexical_only", "out_of_scope_proven"))
        self.assertIn("non_executable_literal", literal["rationale"])
        self.assertEqual(by_id["TN-LEX-0002"]["disposition"], "unclassified")
        self.assertEqual(
            (by_id["TN-UNRESOLVED-MOD-0001"]["source_kind"], by_id["TN-UNRESOLVED-MOD-0001"]["disposition"]),
            ("target_unresolved_entry", "unclassified"),
        )
        # 历史条目逐字重放。
        baseline = discovery.load_historical_baseline()
        self.assertTrue(all(by_id[item["discovery_id"]] == item for item in baseline["items"]))
        # 精确调用归并进来的族为 observed，其余族 blocked；词表不认识的调用只落在全集义务上。
        semantic = {row["id"]: row for row in layers["semantic_candidates"]["candidates"]}
        self.assertEqual(semantic["CAND-EP-COUNTTOKENS"]["evidence_level"], "observed")
        self.assertIn("TN-SINK-0002-1", semantic["CAND-EP-COUNTTOKENS"]["source_ids"])
        self.assertEqual(semantic["CAND-QUOTA-PROBE"]["evidence_level"], "blocked")

    def test_denominator_groups(self) -> None:
        denominator = _layers()["candidate_denominator"]
        self.assertEqual(
            denominator["counts"],
            {"historical_rules": 57, "hitcc_documents_2_1_197": 71, "semantic_candidate_families": 32, "source_mechanisms_2_1_88": 102, "target_send_points": 3},
        )
        self.assertEqual(denominator["total_orthogonal_candidates"], 265)
        self.assertEqual(
            [row["candidate_id"] for row in denominator["candidate_groups"]["target_send_points"]],
            ["TN-SINK-0001-1", "TN-SINK-0002-1", "TN-SINK-0003-1"],
        )
        self.assertEqual(denominator["candidate_groups_sha256"], canonical_sha256(denominator["candidate_groups"]))
        self.assertEqual(denominator["closure_policy"], discovery.CLOSURE_POLICY)
        # classify 的分母读法：各组计数与总数和内容一致。
        rows = classify._candidate_rows(denominator, TARGET)
        self.assertEqual(len(rows), 265)

    def test_explicit_dispositions_resolve_open_items(self) -> None:
        dispositions = {
            "schema_version": discovery.DISCOVERY_DISPOSITIONS_SCHEMA,
            "target_version": TARGET,
            "items": [
                {"discovery_id": "TN-SINK-0003-1", "disposition": "mapped_validation", "rationale": "运行证据确认是 bootstrap 请求。"},
                {"discovery_id": "TN-LEX-0002", "disposition": "out_of_scope_proven", "rationale": "命中在注释文本里。"},
                {"discovery_id": "TN-UNRESOLVED-MOD-0001", "disposition": "catalogued_context", "rationale": "模块只含 UI 资源。"},
            ],
        }
        path = "evidence/campaigns/c1/analysis/discovery-dispositions.json"
        layers = _layers(dispositions=dispositions, dispositions_path=path)
        counts = layers["discovery_inventory"]["counts_by_disposition"]
        self.assertNotIn("unclassified", counts)
        by_id = {item["discovery_id"]: item for item in layers["discovery_inventory"]["items"]}
        self.assertIn(path, by_id["TN-LEX-0002"]["evidence_paths"])
        self.assertEqual(by_id["TN-SINK-0003-1"]["rationale"], "运行证据确认是 bootstrap 请求。")
        for label, item, pattern in (
            ("不存在的发现项", {"discovery_id": "TN-NOPE", "disposition": "catalogued_context", "rationale": "x"}, "不存在"),
            ("非终态", {"discovery_id": "TN-LEX-0002", "disposition": "unclassified", "rationale": "x"}, "显式处置非法"),
            ("空理由", {"discovery_id": "TN-LEX-0002", "disposition": "catalogued_context", "rationale": " "}, "显式处置非法"),
        ):
            with self.subTest(label=label):
                bad = {**dispositions, "items": [item]}
                with self.assertRaisesRegex(discovery.DiscoveryCompletionError, pattern):
                    _layers(dispositions=bad, dispositions_path=path)

    def test_inputs_must_be_same_source_and_closed(self) -> None:
        missing_row = _containment()
        missing_row["evidence"] = missing_row["evidence"][:-1]
        blocked = _containment()
        blocked["completeness"]["result"] = "blocked"
        colliding = _inventory()
        colliding["sinks"][0]["sink_id"] = "SRC2188-AUTH-003"
        colliding_containment = _containment()
        colliding_containment["evidence"][0]["sink_id"] = "SRC2188-AUTH-003"
        unknown_kind = _inventory()
        unknown_kind["sinks"][0]["source_kind"] = "dynamic_guess"
        for label, overrides, pattern in (
            ("containment 不是对本次清单生成的", {"containment": _containment("2" * 64)}, "inventory 摘要不符"),
            ("containment 缺行", {"containment": missing_row}, "sink 集合不一致"),
            ("containment 未闭合", {"containment": blocked}, "无遗漏闭集"),
            ("目标版本不符", {"target_version": "2.1.999"}, "版本"),
            ("目标与历史撞号", {"inventory": colliding, "containment": colliding_containment}, "身份重复"),
            ("未知来源类别", {"inventory": unknown_kind}, "来源类别未知"),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(discovery.DiscoveryCompletionError, pattern):
                    _layers(**overrides)

    def test_generation_is_deterministic(self) -> None:
        first, second = _layers(), _layers()
        for name in ("discovery_inventory", "semantic_candidates", "candidate_denominator"):
            self.assertEqual(canonical_json_bytes(first[name]), canonical_json_bytes(second[name]), name)


@unittest.skipUnless(os.environ.get(FIXTURE_ENV), f"设置 {FIXTURE_ENV} 指向 2.1.226 发现补全回归夹具目录时运行")
class HistoricalReplayTests(unittest.TestCase):
    """2.1.226 对照：限定到历史口径时与历史产物逐项相同（7368／593），新口径把每个目标 sink 都列为发现项。"""

    def test_restricted_layers_equal_historical_artifacts(self) -> None:
        fixture = Path(os.environ[FIXTURE_ENV])

        def read(name: str) -> dict[str, Any]:
            return json.loads((fixture / name).read_text(encoding="utf-8"))

        inventory = read("target-sink-inventory.json")
        # 2.1.226 是单文件 bundle，没有入口未决的概念（B2-04 之前的清单不带该字段）。
        inventory.setdefault("unresolved_entries", [])
        containment = read("sink-containment.json")
        history = read("discovery-inventory.json")
        denominator = read("candidate-denominator.json")
        layers = discovery.build_discovery_layers(
            campaign_id="claude-code-2_1_226-replay",
            target_version="2.1.226",
            inventory=inventory,
            inventory_path="analysis/target-inventory.json",
            inventory_sha256=containment["inventory"]["sha256"],
            containment=containment,
            containment_path="analysis/sink-containment.json",
        )
        generated = layers["discovery_inventory"]
        self.assertEqual(
            (generated["item_count"], layers["candidate_denominator"]["total_orthogonal_candidates"]), (7542, 620)
        )
        by_id = {item["discovery_id"]: item for item in generated["items"]}
        residual = {item["discovery_id"]: item for item in history["items"]}
        self.assertEqual(len(residual), 7368)
        for identity, item in residual.items():
            mine = by_id[identity]
            fields = [field for field in discovery.DISCOVERY_ITEM_FIELDS if not (item["source_kind"] == "target_ast_call" and field == "evidence_paths")]
            self.assertEqual({field: mine[field] for field in fields}, {field: item[field] for field in fields}, identity)
        groups = layers["candidate_denominator"]["candidate_groups"]
        total = 0
        for group, rows in denominator["candidate_groups"].items():
            mine = {row["candidate_id"]: row for row in groups[group]}
            for row in rows:
                restricted = dict(mine[row["candidate_id"]])
                restricted["source_ids"] = [value for value in restricted.get("source_ids", []) if value in residual] if "source_ids" in row else None
                for field, value in row.items():
                    if field == "source_evidence_paths":
                        continue
                    self.assertEqual(restricted[field], value, f"{group}:{row['candidate_id']}:{field}")
            total += len(rows)
        self.assertEqual(total, 593)


@unittest.skipUnless(os.environ.get(SOURCE_ENV), f"设置 {SOURCE_ENV} 指向含 2.1.226 历史产物的仓库根时运行")
class BaselineFreezeReproducibilityTests(unittest.TestCase):
    """冻结基线逐字节可复算：同一组 2.1.226 历史产物重新冻结得到受管根里的同一份文件。"""

    def test_refreeze_is_byte_identical(self) -> None:
        data = discovery.build_historical_baseline(
            Path(os.environ[SOURCE_ENV]),
            semantic_closure_dir="local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/semantic-closure-v1-e577e144a",
            candidate_denominator="local-analysis/fw-f/claude-code-2.1.226/complete-v21-78fae770cbb5/candidate-denominator.json",
        )
        self.assertEqual(data, discovery.baseline_path().read_bytes())


if __name__ == "__main__":
    unittest.main()
