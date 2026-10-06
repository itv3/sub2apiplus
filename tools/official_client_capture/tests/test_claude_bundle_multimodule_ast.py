"""Claude 多模块 AST、可达性三态、containment 与 analyze-bundles 的端到端测试（设计 B2-03、B2-12、B5a-03）。

需要 node 与锁定的 TypeScript（摘要等于 Makefile 的 CAPTURE_TYPESCRIPT_SHA256）：与 Makefile 一致优先取
``CLAUDE_AST_TYPESCRIPT_MODULE``，其次仓库 ``frontend/node_modules``；都没有时跳过。全部输入是合成 Bun SEA
二进制（见 ``claude_bundle_fixture``），每个用例只解析几百字节的模块。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_fw_e
from tools.official_client_capture import extract_claude_bundle as extractor
from tools.official_client_capture.claude_target_inventory import build_target_inventory
from tools.official_client_capture.tests import claude_bundle_fixture as fx

AST_TOOL = fx.ROOT / "tools/official_client_capture/claude_bundle_ast.mjs"
CONTAINMENT_TOOL = fx.ROOT / "tools/official_client_capture/claude_sink_containment.mjs"
NODE = shutil.which("node")
TYPESCRIPT = fx.typescript_module()
LEGACY_SINK_KEYS = (
    "category", "source_start", "source_end", "line", "column", "owner_symbol", "callee_tail",
    "argument_shapes", "node_sha256", "semantic_sha256", "sink_id", "reachability",
    "environment_keys", "privacy_keys", "relevant_literals",
)
LEGACY_COUNT_KEYS = ("function_count", "unique_function_count", "reachable_function_count", "alias_count")
CLI = "/$bunfs/root/cli"
CHUNK_A = "/$bunfs/root/chunk-aaaa1111.js"
CHUNK_B = "/$bunfs/root/chunk-bbbb2222.js"
CHUNK_C = "/$bunfs/root/chunk-cccc3333.js"
WORKER = "/$bunfs/root/src/hooks-worker.js"
WRAPPER = "/$bunfs/root/wrapper.js"
ADDON = "/$bunfs/root/addon.node"


def _node(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, *arguments], capture_output=True, text=True, check=False, timeout=120, cwd=fx.ROOT
    )


@unittest.skipUnless(NODE and TYPESCRIPT, "需要 node 与锁定的 TypeScript 解析器")
class MultiModuleAstTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(self, modules, entry, name: str = "out") -> dict:
        binary = self.root / f"{name}-claude"
        digest = fx.write_binary(binary, modules, entry_point_id=entry)
        result = extractor.extract(binary, digest)
        out = self.root / name
        extractor.write_modules(binary, result, out / "modules")
        manifest = extractor.bundle_manifest(result, out, out / "modules")
        manifest_path = out / "bundle-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lexical = extractor.build_reachability_index(manifest_path)
        ast_path = out / "ast.json"
        completed = _node(
            str(AST_TOOL), "--bundle-manifest", str(manifest_path), "--output", str(ast_path),
            "--expected-sha256", lexical["bundle_manifest_sha256"], "--typescript-module", str(TYPESCRIPT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        ast = json.loads(ast_path.read_text(encoding="utf-8"))
        return {"out": out, "result": result, "manifest": manifest_path, "lexical": lexical,
                "ast_path": ast_path, "ast": ast}

    def _sinks(self, ast: dict) -> dict[tuple[str, str | None], dict]:
        return {(row["module"], row["owner_symbol"]): row for row in ast["sinks"]}

    def _single_ast(self, main_file: Path, name: str = "single.json") -> dict:
        output = self.root / name
        completed = _node(
            str(AST_TOOL), "--bundle", str(main_file), "--output", str(output),
            "--expected-sha256", hashlib.sha256(main_file.read_bytes()).hexdigest(),
            "--typescript-module", str(TYPESCRIPT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(output.read_text(encoding="utf-8"))

    def test_single_main_manifest_degenerates_to_single_bundle_output(self) -> None:
        """规则 ①：单文件模式输出的每个字段在清单模式下逐字段相同（对象只追加键、sink 行只追加 module），新增字段只追加。"""

        prepared = self._prepare(*fx.legacy_single_main_modules())
        main_file = prepared["out"] / "modules/0000-cli.js"
        single = self._single_ast(main_file)
        multi = prepared["ast"]
        self.assertGreater(single["sink_total"], 0)
        self.assertNotIn("module", single["sinks"][0])
        for key, value in single.items():
            with self.subTest(field=key):
                if key == "sinks":
                    self.assertEqual([{k: v for k, v in row.items() if k != "module"} for row in multi["sinks"]], value)
                elif isinstance(value, dict):
                    self.assertEqual({k: multi[key][k] for k in value}, value)
                else:
                    self.assertEqual(multi[key], value)
        self.assertEqual(multi["bundle"]["identity_kind"], "legacy_main_module")
        self.assertEqual(multi["bundle"]["manifest_sha256"], extractor.sha256_file(prepared["manifest"]))
        self.assertEqual({row["module"] for row in multi["sinks"]}, {extractor.MAIN_MODULE})
        # 顶层计数只按旧主模块；全部模块合计另起字段，且包含没人加载的 napi 包装模块。
        self.assertEqual(multi["top_level_count_scope"], "entry_module")
        totals = multi["all_modules_counts"]
        self.assertEqual(totals["analyzed_module_count"], 2)
        self.assertGreater(totals["function_count"], multi["function_count"])
        self.assertEqual(totals["reachable_function_count"], multi["reachable_function_count"])
        graph = multi["module_graph"]
        self.assertEqual(graph["entry_confirmation"], "legacy_main_module")
        self.assertEqual(graph["confirmed_entries"], [extractor.MAIN_MODULE])
        # 没人加载的 napi 包装模块照常分析，但入口关系未决；原生模块只记边界。
        self.assertEqual(graph["unresolved_modules"],
                         [{"name": "/$bunfs/root/image-processor.js", "reason": "not_reachable_from_confirmed_entry"}])
        self.assertIn({"from": extractor.MAIN_MODULE, "to": "/$bunfs/root/image-processor.node", "kind": "require"},
                      graph["native_boundaries"])
        self.assertEqual(single["offset_unit"], "utf16")
        self.assertEqual(single["bundle"]["astral_code_point_count"], 0)

    def test_single_main_manifest_inventory_and_containment_match_single_bundle(self) -> None:
        """规则 ①：清单路径的 inventory 与 containment 与单文件旧路径逐行相同（只追加 module 等键）。"""

        prepared = self._prepare(*fx.legacy_single_main_modules())
        main_file = prepared["out"] / "modules/0000-cli.js"
        single_ast = self._single_ast(main_file)
        single_ast_path = self.root / "single.json"
        single_lexical = extractor.build_reachability_index(main_file)
        legacy_inventory = build_target_inventory(single_ast, single_lexical, target_version="9.9.9", platform="linux/amd64")
        manifest_inventory = build_target_inventory(
            prepared["ast"], prepared["lexical"], target_version="9.9.9", platform="linux/amd64"
        )
        drop = lambda row: {
            **{k: v for k, v in row.items() if k != "module"},
            "lexical_candidates": [
                {k: v for k, v in item.items() if k not in {"module", "offset_utf16"}} for item in row["lexical_candidates"]
            ],
        }
        self.assertEqual([drop(row) for row in manifest_inventory["sinks"]], legacy_inventory["sinks"])
        for key in ("schema_version", "bundle_sha256", "sink_total", "limitations"):
            self.assertEqual(manifest_inventory[key], legacy_inventory[key], key)
        for key, value in legacy_inventory["counts"].items():
            self.assertEqual(manifest_inventory["counts"][key], value, key)
        for key, value in legacy_inventory["completeness"].items():
            self.assertEqual(manifest_inventory["completeness"][key], value, key)

        outputs = {}
        for label, inventory, ast_path, source in (
            ("legacy", legacy_inventory, single_ast_path, ["--bundle", str(main_file)]),
            ("manifest", manifest_inventory, prepared["ast_path"], ["--bundle-manifest", str(prepared["manifest"])]),
        ):
            inventory_path = self.root / f"inventory-{label}.json"
            inventory_path.write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
            output = self.root / f"containment-{label}.json"
            completed = _node(
                str(CONTAINMENT_TOOL), *source, "--inventory", str(inventory_path), "--ast-inventory", str(ast_path),
                "--output", str(output), "--typescript-module", str(TYPESCRIPT),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            outputs[label] = json.loads(output.read_text(encoding="utf-8"))
        legacy, manifest = outputs["legacy"], outputs["manifest"]
        self.assertEqual(manifest["evidence_sha256"], legacy["evidence_sha256"])
        self.assertEqual(manifest["completeness"], legacy["completeness"])
        self.assertEqual({k: manifest["bundle"][k] for k in ("sha256", "byte_count")},
                         {k: legacy["bundle"][k] for k in ("sha256", "byte_count")})
        self.assertEqual(manifest["bundle"]["identity_kind"], "legacy_main_module")
        self.assertNotIn("module_bound_evidence_sha256", legacy)
        self.assertNotEqual(manifest["module_bound_evidence_sha256"], manifest["evidence_sha256"])

    def test_entry_candidates_are_confirmed_by_cross_module_edges(self) -> None:
        graph = self._prepare(*fx.chunked_modules())["ast"]["module_graph"]
        self.assertEqual(graph["entry_rule"], "non_chunk_js_candidates")
        self.assertEqual(graph["entry_confirmation"], "ast_confirmed")
        self.assertEqual(graph["confirmed_entries"], [CLI, WORKER])
        self.assertEqual(
            graph["rejected_entry_candidates"],
            [{"name": WRAPPER, "reason": "imported_by_other_module", "importers": [CLI]}],
        )
        self.assertEqual(graph["declared_entry"], CLI)
        self.assertTrue(graph["declared_entry_confirmed"])
        self.assertEqual(graph["entry_functions"], [{"module": CLI, "function": "bt"}, {"module": WORKER, "function": "w"}])

    def test_cross_module_calls_give_three_reachability_states(self) -> None:
        ast = self._prepare(*fx.chunked_modules())["ast"]
        sinks = self._sinks(ast)
        # 静态导入绑定：cli.bt → chunk-a.L。
        self.assertEqual(sinks[(CHUNK_A, "L")]["reachability"], "possible_from_entry")
        # 字面量动态 import：cli.bt → chunk-b 的导出 main。
        self.assertEqual(sinks[(CHUNK_B, "main")]["reachability"], "possible_from_entry")
        # 模块可从入口加载，但导出函数没人用：调用图不能说明可达。
        self.assertEqual(sinks[(CHUNK_A, "unusedA")]["reachability"], "unknown")
        # 孤立 chunk：入口关系未决。
        self.assertEqual(sinks[(CHUNK_C, "z")]["reachability"], "unresolved_entry")
        self.assertEqual(ast["reachability_states"], ["possible_from_entry", "unknown", "unresolved_entry"])
        self.assertEqual(ast["completeness"]["unresolved_entry_sink_count"], 1)
        ids = [row["sink_id"] for row in ast["sinks"]]
        self.assertEqual(len(ids), len(set(ids)))
        # 两个 fetch 语义相同、分属不同模块：全局序号保证 sink_id 不撞。
        fetch_ids = sorted(row["sink_id"] for row in ast["sinks"] if row["category"] == "fetch")
        self.assertEqual(len(fetch_ids), 3)

    def test_dynamic_loads_without_literal_targets_are_unresolved(self) -> None:
        ast = self._prepare(*fx.chunked_modules())["ast"]
        dynamic = [(row["from"], row["kind"], row.get("via_alias")) for row in ast["unresolved_dynamic_edges"]]
        self.assertEqual(
            sorted(dynamic, key=str),
            sorted([(CHUNK_B, "require", f"{CHUNK_A}#Ie"), (CHUNK_B, "dynamic_import", None)], key=str),
        )
        graph = ast["module_graph"]
        self.assertEqual(graph["unresolved_modules"], [{"name": CHUNK_C, "reason": "not_reachable_from_confirmed_entry"}])
        self.assertEqual(graph["unresolved_dynamic_edge_count"], 2)
        self.assertEqual(ast["completeness"]["unresolved_module_count"], 1)
        # Worker 路径字面量把 Worker 入口纳入闭包，但不否定它的入口身份。
        kinds = {(row["from"], row["to"]): row["kind"] for row in ast["module_edges"]}
        self.assertEqual(kinds[(CHUNK_B, WORKER)], "path_reference")
        self.assertEqual(kinds[(CLI, CHUNK_B)], "dynamic_import")

    def test_native_modules_are_boundaries_not_analyzed(self) -> None:
        ast = self._prepare(*fx.chunked_modules())["ast"]
        self.assertEqual(
            sorted((row["from"], row["to"]) for row in ast["module_graph"]["native_boundaries"]),
            [(CHUNK_B, ADDON), (WRAPPER, ADDON)],
        )
        self.assertNotIn(ADDON, {row["name"] for row in ast["modules"]})
        self.assertEqual(ast["completeness"]["module_count"], 6)
        self.assertEqual(ast["module_graph"]["missing_targets"], [])

    def test_no_entry_candidates_leave_every_module_unresolved(self) -> None:
        ast = self._prepare(*fx.chunked_without_candidates())["ast"]
        graph = ast["module_graph"]
        self.assertEqual(graph["entry_rule"], "no_entry_candidates")
        self.assertEqual(graph["entry_confirmation"], "unresolved")
        self.assertEqual(graph["confirmed_entries"], [])
        self.assertEqual({row["reason"] for row in graph["unresolved_modules"]}, {"no_confirmed_entry"})
        self.assertEqual(len(graph["unresolved_modules"]), 2)
        self.assertTrue(ast["sinks"])
        self.assertEqual({row["reachability"] for row in ast["sinks"]}, {"unresolved_entry"})

    def test_offsets_are_utf16_code_units_and_merge_with_lexical(self) -> None:
        prepared = self._prepare(*fx.chunked_modules())
        ast = prepared["ast"]
        self.assertEqual(ast["offset_unit"], "utf16")
        websocket = next(row for row in ast["sinks"] if row["category"] == "websocket")
        lexical = next(row for row in prepared["lexical"]["sinks"] if row["kind"] == "websocket")
        self.assertEqual(websocket["source_start"], lexical["offset_utf16"])
        self.assertEqual(websocket["source_start"] - lexical["offset"], 2)
        chunk_b = next(row for row in ast["modules"] if row["name"] == CHUNK_B)
        self.assertEqual(chunk_b["astral_code_point_count"], 2)
        inventory = build_target_inventory(
            ast, prepared["lexical"], target_version="9.9.9", platform="linux/arm64"
        )
        merged = next(row for row in inventory["sinks"] if row["sink_id"] == websocket["sink_id"])
        self.assertEqual(merged["discovery_sources"], ["ast", "lexical"])
        self.assertEqual(merged["lexical_candidates"][0]["offset_utf16"], websocket["source_start"])

    def test_inventory_and_containment_rebind_every_module_sink(self) -> None:
        prepared = self._prepare(*fx.chunked_modules())
        inventory = build_target_inventory(
            prepared["ast"], prepared["lexical"], target_version="9.9.9", platform="linux/arm64"
        )
        counts = inventory["counts"]
        self.assertEqual(
            sum(row["inventory_sink_count"] for row in counts["by_module"].values()),
            counts["inventory_sink_count"],
        )
        inventory_path = prepared["out"] / "inventory.json"
        inventory_path.write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
        output = prepared["out"] / "containment.json"
        completed = _node(
            str(CONTAINMENT_TOOL), "--bundle-manifest", str(prepared["manifest"]),
            "--inventory", str(inventory_path), "--ast-inventory", str(prepared["ast_path"]),
            "--output", str(output), "--typescript-module", str(TYPESCRIPT),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(evidence["completeness"]["result"], "passed")
        self.assertEqual(evidence["completeness"]["structural_finding_counts"],
                         {"exact_ast_call": 4, "non_executable_literal": 1})
        self.assertEqual([row["module"] for row in evidence["evidence"]],
                         [row["module"] for row in inventory["sinks"]])

        broken = json.loads(inventory_path.read_text(encoding="utf-8"))
        broken["sinks"][0]["module"] = ADDON
        broken_path = prepared["out"] / "inventory-broken.json"
        broken_path.write_text(json.dumps(broken), encoding="utf-8")
        completed = _node(
            str(CONTAINMENT_TOOL), "--bundle-manifest", str(prepared["manifest"]),
            "--inventory", str(broken_path), "--ast-inventory", str(prepared["ast_path"]),
            "--output", str(prepared["out"] / "containment-broken.json"), "--typescript-module", str(TYPESCRIPT),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("待分析模块", completed.stderr)

    def test_parser_records_node_identity(self) -> None:
        parser = self._prepare(*fx.chunked_modules())["ast"]["parser"]
        self.assertEqual(parser["node_sha256"], hashlib.sha256(Path(parser["node_path"]).read_bytes()).hexdigest())
        self.assertEqual(Path(parser["node_path"]).resolve(), Path(NODE).resolve())
        self.assertTrue(parser["node_version"].startswith("v"))
        self.assertEqual(parser["typescript_module_sha256"], fx.TYPESCRIPT_SHA256)

    def test_missing_explicit_typescript_does_not_fall_back(self) -> None:
        bundle = self.root / "cli.js"
        bundle.write_text("function main(){}main();\n", encoding="utf-8")
        completed = _node(
            str(AST_TOOL), "--bundle", str(bundle), "--output", str(self.root / "ast.json"),
            "--expected-sha256", hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "--typescript-module", str(self.root / "missing/typescript.js"),
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--typescript-module", completed.stderr)
        self.assertFalse((self.root / "ast.json").exists())

    def test_bundle_and_manifest_are_mutually_exclusive(self) -> None:
        completed = _node(
            str(AST_TOOL), "--bundle", "a.js", "--bundle-manifest", "m.json", "--output", str(self.root / "x.json"),
            "--expected-sha256", "0" * 64,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("只能给一个", completed.stderr)


@unittest.skipUnless(NODE and TYPESCRIPT, "需要 node 与锁定的 TypeScript 解析器")
class AnalyzeBundlesEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        modules, entry = fx.legacy_single_main_modules()
        digest = fx.write_binary(self.root / "baseline/claude", modules, entry_point_id=entry)
        result = extractor.extract(self.root / "baseline/claude", digest)
        extractor.write_modules(self.root / "baseline/claude", result, self.root / "baseline/modules")
        self.baseline_anchors = self.root / "baseline/anchors.json"
        self.baseline_anchors.write_text(json.dumps(result), encoding="utf-8")
        self.baseline_index = self.root / "baseline/index.json"
        self.baseline_index.write_text(
            json.dumps(extractor.build_reachability_index(self.root / "baseline/modules/0000-cli.js")),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _freeze(self, freeze: dict) -> Path:
        root = self.root / "freeze"
        (root / "freeze.json").write_text(json.dumps(freeze), encoding="utf-8")
        return root

    def test_v2_freeze_analyzes_only_the_frozen_platform(self) -> None:
        modules, entry = fx.chunked_modules()
        digest = fx.write_binary(self.root / "freeze/binaries/linux-arm64/claude", modules, entry_point_id=entry)
        row = {"path": "binaries/linux-arm64/claude", "sha256": digest}
        freeze_root = self._freeze({
            "schema_version": claude_fw_e.SCHEMA_FREEZE_V2,
            "target_version": "9.9.9",
            "platforms": ["linux/arm64"],
            "binaries": {"linux/arm64": {"npm": row, "gcs": row}},
            "binary_equivalence": "npm=gcs",
            "binary_sha256": digest,
        })
        output = self.root / "static"
        static = claude_fw_e.analyze_bundles(
            freeze_root, output, self.baseline_anchors, self.baseline_index,
            node_binary=NODE, typescript_module_path=TYPESCRIPT,
        )
        self.assertEqual(list(static["target_platforms"]), ["linux/arm64"])
        platform = static["target_platforms"]["linux/arm64"]
        graph = platform["module_graph"]
        self.assertEqual(graph["confirmed_entries"], [CLI, WORKER])
        self.assertEqual((graph["chunk_count"], graph["unresolved_module_count"],
                          graph["unresolved_dynamic_edge_count"], graph["unresolved_entry_sink_count"]), (3, 1, 2, 2))
        self.assertEqual((graph["ast_parse_diagnostic_count"], graph["ast_truncated"], graph["ast_timeout_seconds"]),
                         (0, False, 600))
        self.assertEqual(platform["bundle_sha256"], platform["bundle_manifest_sha256"])
        self.assertTrue((output / "linux-arm64/modules/0001-cli").is_file())
        self.assertEqual(json.loads((output / platform["target_native_ast_path"]).read_text(encoding="utf-8"))
                         ["top_level_count_scope"], "all_analyzed_modules")
        runtime = static["ast_runtime"]
        self.assertEqual(runtime["typescript_module_sha256"], claude_fw_e.PINNED_TYPESCRIPT_SHA256)
        ast = json.loads((output / platform["target_native_ast_path"]).read_text(encoding="utf-8"))
        self.assertEqual(ast["parser"]["node_sha256"], runtime["node_sha256"])
        inventory = json.loads((output / platform["target_sink_inventory_path"]).read_text(encoding="utf-8"))
        self.assertEqual(inventory["sink_total"], platform["target_sink_count"])
        self.assertEqual(len(inventory["unresolved_entries"]), 5)

    def test_v1_freeze_with_legacy_platform_keeps_single_main_semantics(self) -> None:
        modules, entry = fx.legacy_single_main_modules()
        digest = fx.write_binary(self.root / "freeze/binaries/linux-amd64/claude", modules, entry_point_id=entry)
        freeze_root = self._freeze({
            "schema_version": claude_fw_e.SCHEMA_FREEZE,
            "target_version": "9.9.9",
            "platforms": ["linux/amd64"],
            "binaries": {"linux/amd64": {"path": "binaries/linux-amd64/claude", "sha256": digest}},
        })
        static = claude_fw_e.analyze_bundles(
            freeze_root, self.root / "static", self.baseline_anchors, self.baseline_index,
            node_binary=NODE, typescript_module_path=TYPESCRIPT,
        )
        platform = static["target_platforms"]["linux/amd64"]
        graph = platform["module_graph"]
        self.assertEqual(graph["entry_rule"], "legacy_main_module")
        self.assertEqual(graph["confirmed_entries"], [extractor.MAIN_MODULE])
        # 规则 ① 的 bundle 身份保持旧含义：旧主模块内容摘要；模块清单另行绑定。
        anchors = json.loads((self.root / "static" / platform["anchors_path"]).read_text(encoding="utf-8"))
        self.assertEqual(platform["bundle_sha256"], anchors["modules"][0]["sha256"])
        self.assertNotEqual(platform["bundle_sha256"], platform["bundle_manifest_sha256"])
        diffs = {row["candidate"]: row for row in static["probe_diffs"]}
        self.assertTrue(all(row["baseline_target_equal"] for row in diffs.values()))


if __name__ == "__main__":
    unittest.main()
