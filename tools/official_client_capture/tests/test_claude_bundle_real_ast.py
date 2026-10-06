"""真实官方二进制的整份 AST 回归测试（设计 B2-02～B2-04、B2-12）。默认跳过。

整份 AST 会占用数 GB 内存，只在显式设置下列环境变量时运行（另需 node 与锁定的 TypeScript，取法同
``claude_bundle_fixture.typescript_module``）：

- ``CLAUDE_AST_REAL_2_1_220_BINARY``：2.1.220 linux-x64 官方二进制。按模块清单走新工具（提取 → 词法 → AST →
  inventory），核对规则 ① 下全部旧字段与改前工具逐字段相同（摘要夹具 ``claude_bundle_2_1_220_linux_x64_legacy_field_digests.json``），
  并核对模块图、跨模块边与未决清单。本机实测：进程树峰值约 4.0 GB（node 解析约 2.9 GB，其后 Python 载入
  AST 产物建 inventory），约 16 秒。
- ``CLAUDE_AST_REAL_2_1_285_FREEZE_ROOT``：B2-01 冻结目录（``freeze.json`` v2）。跑 ``claude_fw_e.analyze_bundles``，
  核对 ``truncated == false``、``parse_diagnostic_count == 0``、入口、跨模块边、未决清单与 sink 计数等于
  ``claude_bundle_2_1_285_linux_arm64_module_graph.json``。本机实测：进程树峰值约 1.3 GB，约 20 秒。

两项务必串行运行（一次一个测试进程）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from tools.official_client_capture import claude_fw_e
from tools.official_client_capture import extract_claude_bundle as extractor
from tools.official_client_capture.claude_target_inventory import build_target_inventory
from tools.official_client_capture.tests import claude_bundle_fixture as fx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEGACY_220 = FIXTURES / "claude_bundle_2_1_220_linux_x64_legacy_field_digests.json"
GRAPH_285 = FIXTURES / "claude_bundle_2_1_285_linux_arm64_module_graph.json"
AST_TOOL = fx.ROOT / "tools/official_client_capture/claude_bundle_ast.mjs"
NODE = shutil.which("node")
TYPESCRIPT = fx.typescript_module()
BINARY_220 = os.environ.get("CLAUDE_AST_REAL_2_1_220_BINARY")
FREEZE_285 = os.environ.get("CLAUDE_AST_REAL_2_1_285_FREEZE_ROOT")


@unittest.skipUnless(BINARY_220 and NODE and TYPESCRIPT, "需要 CLAUDE_AST_REAL_2_1_220_BINARY、node 与锁定的 TypeScript")
class Real2_1_220AstTests(unittest.TestCase):
    def test_manifest_mode_keeps_every_legacy_field(self) -> None:
        fixture = json.loads(LEGACY_220.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            binary = Path(BINARY_220)
            result = extractor.extract(binary, fixture["binary_sha256"])
            extractor.write_modules(binary, result, out / "modules")
            manifest = extractor.bundle_manifest(result, out, out / "modules")
            manifest_path = out / "bundle-manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            lexical = extractor.build_reachability_index(manifest_path)
            self.assertEqual(
                fx.legacy_field_digests(lexical, fixture["lexical"]["shape"]), fixture["lexical"]["field_sha256"]
            )
            ast_path = out / "ast.json"
            completed = subprocess.run(
                [NODE, str(AST_TOOL), "--bundle-manifest", str(manifest_path), "--output", str(ast_path),
                 "--expected-sha256", lexical["bundle_manifest_sha256"], "--typescript-module", str(TYPESCRIPT)],
                capture_output=True, text=True, check=False, timeout=1800, cwd=fx.ROOT,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            ast = json.loads(ast_path.read_text(encoding="utf-8"))
            self.assertEqual(fx.legacy_field_digests(ast, fixture["ast"]["shape"]), fixture["ast"]["field_sha256"])
            mode = fixture["manifest_mode"]
            self.assertEqual(ast["top_level_count_scope"], mode["top_level_count_scope"])
            self.assertEqual(ast["all_modules_counts"], mode["all_modules_counts"])
            self.assertEqual(ast["bundle"]["identity_kind"], mode["bundle_identity_kind"])
            self.assertEqual(ast["completeness"], mode["completeness"])
            self.assertEqual({k: v for k, v in ast["module_graph"].items() if k != "external_specifiers"}, mode["module_graph"])
            self.assertEqual(ast["module_graph"]["external_specifiers"], mode["external_specifiers"])
            self.assertEqual([{key: row[key] for key in mode["modules"][0]} for row in ast["modules"]], mode["modules"])
            self.assertEqual(
                [[e["from"], e["to"], e["kind"], e["target_role"], e["occurrences"]] for e in ast["module_edges"]],
                mode["module_edges"],
            )
            self.assertEqual(
                [[r["from"], r["kind"], r.get("via_alias"), r["owner_symbol"], r["source_start"], r["source_end"],
                  r["argument_shape"]] for r in ast["unresolved_dynamic_edges"]],
                mode["unresolved_dynamic_edges"],
            )
            inventory = build_target_inventory(ast, lexical, target_version="2.1.220", platform="linux/amd64")
            del ast
            self.assertEqual(
                fx.legacy_field_digests(inventory, fixture["inventory"]["shape"]), fixture["inventory"]["field_sha256"]
            )
            self.assertEqual(inventory["counts"], mode["inventory"]["counts"])
            self.assertEqual(inventory["completeness"], mode["inventory"]["completeness"])
            self.assertEqual(dict(Counter(row["kind"] for row in inventory["unresolved_entries"])),
                             mode["inventory"]["unresolved_entry_kinds"])


@unittest.skipUnless(FREEZE_285 and NODE and TYPESCRIPT, "需要 CLAUDE_AST_REAL_2_1_285_FREEZE_ROOT、node 与锁定的 TypeScript")
class Real2_1_285AnalyzeBundlesTests(unittest.TestCase):
    def test_analyze_bundles_matches_graph_fixture(self) -> None:
        fixture = json.loads(GRAPH_285.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 基线只用于 probe 差分说明，这里给最小基线文件；入口与模块图的核对不依赖它。
            baseline_anchors = root / "baseline-anchors.json"
            baseline_anchors.write_text(json.dumps({"binary_sha256": None}), encoding="utf-8")
            baseline_index = root / "baseline-index.json"
            baseline_index.write_text(json.dumps({"bundle_sha256": None, "probes": []}), encoding="utf-8")
            static = claude_fw_e.analyze_bundles(
                Path(FREEZE_285), root / "static", baseline_anchors, baseline_index,
                node_binary=NODE, typescript_module_path=TYPESCRIPT,
            )
            self.assertEqual(list(static["target_platforms"]), ["linux/arm64"])
            platform = static["target_platforms"]["linux/arm64"]
            self.assertEqual(platform["binary_sha256"], fixture["binary_sha256"])
            self.assertEqual(platform["bundle_manifest_sha256"], fixture["bundle_manifest_sha256"])
            manifest = json.loads((root / "static" / platform["bundle_manifest_path"]).read_text(encoding="utf-8"))
            index_of = {row["name"]: row["index"] for row in manifest["modules"]}
            ast = json.loads((root / "static" / platform["target_native_ast_path"]).read_text(encoding="utf-8"))
            inventory = json.loads((root / "static" / platform["target_sink_inventory_path"]).read_text(encoding="utf-8"))

        self.assertEqual(ast["completeness"], fixture["completeness"])
        self.assertIs(ast["completeness"]["truncated"], False)
        self.assertEqual(ast["completeness"]["parse_diagnostic_count"], 0)
        self.assertEqual(ast["top_level_count_scope"], fixture["top_level_count_scope"])
        self.assertEqual(ast["all_modules_counts"], fixture["all_modules_counts"])
        graph = ast["module_graph"]
        entry = fixture["entry"]
        self.assertEqual([index_of[name] for name in graph["entry_candidates"]], entry["entry_candidates"])
        self.assertEqual([index_of[name] for name in graph["confirmed_entries"]], entry["confirmed_entries"])
        self.assertEqual(graph["rejected_entry_candidates"], entry["rejected_entry_candidates"])
        self.assertEqual([[index_of[row["module"]], row["function"]] for row in graph["entry_functions"]],
                         entry["entry_functions"])
        unresolved = fixture["unresolved"]
        self.assertEqual([[index_of[row["name"]], row["reason"]] for row in graph["unresolved_modules"]], unresolved["modules"])
        self.assertEqual(
            [[index_of[r["from"]], r["kind"], r.get("via_alias"), r["owner_symbol"], r["source_start"], r["source_end"],
              r["argument_shape"]] for r in ast["unresolved_dynamic_edges"]],
            unresolved["dynamic_edges"],
        )
        self.assertEqual(dict(Counter(row["kind"] for row in inventory["unresolved_entries"])),
                         unresolved["inventory_entries_by_kind"])
        edges = fixture["edges"]
        self.assertEqual(graph["edge_counts_by_kind"], edges["edge_counts_by_kind"])
        self.assertEqual(len(ast["module_edges"]), edges["module_edge_count"])
        self.assertEqual([[index_of[r["from"]], index_of[r["to"]], r["kind"]] for r in graph["native_boundaries"]],
                         edges["native_boundaries"])
        self.assertEqual(graph["missing_targets"], edges["missing_targets"])
        self.assertEqual(graph["external_specifiers"], edges["external_specifiers"])
        rows = fx.module_edge_rows(ast, index_of)
        listed_kinds = {"export_from", "dynamic_import", "require", "worker", "path_reference"}
        self.assertEqual(
            sorted((row for row in rows if row[2] in listed_kinds or row[3] not in extractor.JS_ROLES), key=fx.compact_json),
            edges["listed_edges"],
        )
        imports: dict[int, list[list]] = {}
        for row in rows:
            if row[2] == "import":
                imports.setdefault(row[0], []).append(row)
        self.assertEqual(
            [[m["index"], m["status"], m["sink_count"], m["edge_count"], len(imports.get(m["index"], [])),
              fx.edge_rows_sha256(imports.get(m["index"], []))] for m in ast["modules"]],
            fixture["module_rows"],
        )
        sinks = fixture["sinks"]
        self.assertEqual(ast["sink_total"], sinks["sink_total"])
        self.assertEqual(ast["sink_category_counts"], sinks["sink_category_counts"])
        self.assertEqual({k: v for k, v in inventory["counts"].items() if k != "by_module"}, sinks["inventory_counts"])


if __name__ == "__main__":
    unittest.main()
