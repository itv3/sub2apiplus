"""Claude bundle 模块图提取、模块清单、多模块词法索引与目标 inventory 合并的离线测试（设计 B2-02、B2-04、B2-12）。

不需要 node：真实版本用模块表夹具与 AST 实测夹具（只含元数据），代码路径用合成 Bun SEA 二进制。真实二进制的
结构校验在设置环境变量时运行：``CLAUDE_2_1_220_LINUX_X64_BINARY``（2.1.220 linux-x64 官方二进制）、
``CLAUDE_2_1_285_LINUX_ARM64_BINARY``（B2-01 冻结的 2.1.285 linux-arm64 二进制）；整份 AST 的真实回归测试在
``test_claude_bundle_real_ast``，另用环境变量开启。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tools.official_client_capture import claude_fw_e
from tools.official_client_capture import extract_claude_bundle as extractor
from tools.official_client_capture.claude_target_inventory import (
    TargetInventoryError,
    UNRESOLVED_DISPOSITION,
    UNRESOLVED_SOURCE_KIND,
    build_target_inventory,
    unresolved_discovery_items,
)
from tools.official_client_capture.tests import claude_bundle_fixture as fx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_220 = FIXTURES / "claude_bundle_2_1_220_linux_x64_module_table.json"
FIXTURE_285 = FIXTURES / "claude_bundle_2_1_285_linux_arm64_module_table.json"
GRAPH_285 = FIXTURES / "claude_bundle_2_1_285_linux_arm64_module_graph.json"
LEGACY_220 = FIXTURES / "claude_bundle_2_1_220_linux_x64_legacy_field_digests.json"
EXTRACTOR = Path(extractor.__file__)


def _rows_220() -> tuple[list[dict], str]:
    fixture = json.loads(FIXTURE_220.read_text(encoding="utf-8"))
    return [dict(row) for row in fixture["modules"]], fixture["offsets_reserved_hex"]


def _rows_285() -> tuple[list[dict], str]:
    fixture = json.loads(FIXTURE_285.read_text(encoding="utf-8"))
    columns = fixture["columns"]
    rows = []
    for values in fixture["rows"]:
        row = dict(zip(columns, values))
        row["record_tail_hex"] = "00" * 24 + row.pop("record_flags_hex")
        rows.append(row)
    return rows, fixture["offsets_reserved_hex"]


def _flags_row(index: int, name: str, flags: str) -> dict:
    return {"index": index, "name": name, "record_tail_hex": "00" * 24 + flags, "kind": "text"}


class ModuleGraphFixtureTests(unittest.TestCase):
    """真实模块表夹具上的入口识别与分类计数。"""

    def test_2_1_220_is_single_main_bundle_with_one_entry(self) -> None:
        rows, reserved = _rows_220()
        graph = extractor.build_module_graph(rows, reserved)
        self.assertEqual(graph["entry_rule"], extractor.ENTRY_RULE_LEGACY)
        self.assertEqual(graph["entry_candidates"], [extractor.MAIN_MODULE])
        self.assertEqual(graph["declared_entry_index"], 0)
        self.assertEqual(graph["declared_entry"], extractor.MAIN_MODULE)
        self.assertEqual(
            (graph["module_count"], graph["js_module_count"], graph["chunk_count"],
             graph["asset_count"], graph["native_count"], graph["unresolved_count"]),
            (8, 3, 0, 3, 2, 0),
        )
        roles = {row["name"]: row["role"] for row in rows}
        self.assertEqual(roles[extractor.MAIN_MODULE], "entry")
        self.assertEqual(roles["/$bunfs/root/image-processor.js"], "js_module")
        self.assertEqual(roles["/$bunfs/root/mermaid.min.js"], "asset")
        self.assertEqual(roles["/$bunfs/root/audio-capture.node"], "native")

    def test_2_1_285_chunked_bundle_counts_and_candidates(self) -> None:
        rows, reserved = _rows_285()
        graph = extractor.build_module_graph(rows, reserved)
        self.assertEqual(graph["entry_rule"], extractor.ENTRY_RULE_CANDIDATES)
        self.assertEqual(graph["entry_confirmation"], "pending_ast")
        self.assertEqual(
            (graph["module_count"], graph["js_module_count"], graph["chunk_count"],
             graph["asset_count"], graph["native_count"], graph["unresolved_count"]),
            (2352, 2123, 2119, 227, 2, 0),
        )
        self.assertEqual(
            graph["entry_candidates"],
            [
                "/$bunfs/root/cli",
                "/$bunfs/root/audio-capture.js",
                "/$bunfs/root/clipboard-napi.js",
                "/$bunfs/root/src/plugins/functionHooks/hooks-worker/hooks-worker.js",
            ],
        )
        self.assertEqual((graph["declared_entry_index"], graph["declared_entry"]), (5, "/$bunfs/root/cli"))
        self.assertEqual(graph["loader_counts"], {"file": 137, "js": 2123, "napi": 2, "text": 90})

    def test_no_candidates_marks_every_chunk_unresolved(self) -> None:
        rows = [
            _flags_row(0, "/$bunfs/root/chunk-a1.js", "01010100"),
            _flags_row(1, "/$bunfs/root/chunk-b2.js", "01010100"),
            _flags_row(2, "/$bunfs/root/payload.template.html.asset", "00050001"),
            _flags_row(3, "/$bunfs/root/image-processor.node", "000a0001"),
        ]
        graph = extractor.build_module_graph(rows, "01000000" + "00" * 12)
        self.assertEqual(graph["entry_rule"], extractor.ENTRY_RULE_NONE)
        self.assertEqual(graph["entry_confirmation"], "unresolved")
        self.assertEqual(graph["entry_candidates"], [])
        self.assertEqual(graph["unresolved"], ["/$bunfs/root/chunk-a1.js", "/$bunfs/root/chunk-b2.js"])
        self.assertEqual(graph["unresolved_count"], 2)
        # Bun 声明的入口只作核对信息，不据此把 chunk 升格为入口。
        self.assertEqual(graph["declared_entry"], "/$bunfs/root/chunk-b2.js")

    def test_native_by_suffix_and_loader(self) -> None:
        rows = [
            _flags_row(0, "/$bunfs/root/cli", "01010100"),
            _flags_row(1, "/$bunfs/root/odd.node", "00050001"),
            _flags_row(2, "/$bunfs/root/module.wasm", "00090001"),
        ]
        extractor.build_module_graph(rows, "00" * 16)
        self.assertEqual([row["role"] for row in rows], ["entry_candidate", "native", "native"])

    def test_unknown_record_flags_are_rejected(self) -> None:
        rows = [_flags_row(0, "/$bunfs/root/cli", "01630100")]
        with self.assertRaisesRegex(extractor.ExtractError, "loader"):
            extractor.build_module_graph(rows, "00" * 16)

    def test_duplicate_module_names_are_rejected(self) -> None:
        rows = [
            _flags_row(0, "/$bunfs/root/cli", "01010100"),
            _flags_row(1, "/$bunfs/root/cli", "01010100"),
        ]
        with self.assertRaisesRegex(extractor.ExtractError, "重名"):
            extractor.build_module_graph(rows, "00" * 16)


class ExtractSyntheticBinaryTests(unittest.TestCase):
    """合成 Bun SEA 二进制上的提取、落盘命名与模块清单。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _extract(self, modules, entry):
        binary = self.root / "claude"
        digest = fx.write_binary(binary, modules, entry_point_id=entry)
        return binary, extractor.extract(binary, digest)

    def test_legacy_shape_keeps_legacy_fields_and_index_prefixed_names(self) -> None:
        binary, result = self._extract(*fx.legacy_single_main_modules())
        self.assertEqual(result["module_graph"]["entry_rule"], extractor.ENTRY_RULE_LEGACY)
        for key in ("binary_sha256", "container", "bun_section", "blob_base", "byte_count",
                    "modules_offset", "modules_length", "offsets_reserved_hex", "extractor_sha256"):
            self.assertIn(key, result)
        main = result["modules"][0]
        for key in ("index", "name", "contents_offset", "contents_length", "sourcemap_length",
                    "record_tail_hex", "sha256", "kind"):
            self.assertIn(key, main)
        self.assertEqual((main["kind"], main["role"], main["loader"], main["encoding"]), ("text", "entry", "js", "latin1"))
        extractor.write_modules(binary, result, self.root / "modules")
        self.assertEqual(
            sorted(path.name for path in (self.root / "modules").iterdir()),
            ["0000-cli.js", "0001-image-processor.js", "0002-image-processor.node", "0003-mermaid.min.js"],
        )
        self.assertEqual(main["extracted_name"], "0000-cli.js")

    def test_same_basename_in_different_directories_does_not_overwrite(self) -> None:
        modules = [
            fx.js("/$bunfs/root/a/x.js", "export function A(){}\n"),
            fx.js("/$bunfs/root/b/x.js", "export function B(){}\n"),
        ]
        binary, result = self._extract(modules, 0)
        extractor.write_modules(binary, result, self.root / "modules")
        self.assertEqual((self.root / "modules/0000-x.js").read_text(), "export function A(){}\n")
        self.assertEqual((self.root / "modules/0001-x.js").read_text(), "export function B(){}\n")

    def test_bundle_manifest_is_relative_and_location_independent(self) -> None:
        binary, result = self._extract(*fx.chunked_modules())
        manifests = []
        for name in ("first", "second"):
            target = self.root / name
            again = copy.deepcopy(result)
            extractor.write_modules(binary, again, target / "modules")
            manifest = extractor.bundle_manifest(again, target, target / "modules")
            manifests.append(json.dumps(manifest, sort_keys=True))
            rows = {row["name"]: row for row in manifest["modules"]}
            self.assertEqual(rows["/$bunfs/root/cli"]["path"], "modules/0001-cli")
            self.assertTrue(rows["/$bunfs/root/cli"]["analyze"])
            self.assertFalse(rows["/$bunfs/root/addon.node"]["analyze"])
            self.assertEqual(rows["/$bunfs/root/chunk-bbbb2222.js"]["encoding"], "utf16")
        self.assertEqual(manifests[0], manifests[1])
        with self.assertRaisesRegex(extractor.ExtractError, "之下"):
            extractor.bundle_manifest(result, self.root / "first" / "modules", self.root / "second" / "modules")

    def test_manifest_module_tampering_is_detected(self) -> None:
        binary, result = self._extract(*fx.chunked_modules())
        extractor.write_modules(binary, result, self.root / "out/modules")
        manifest = extractor.bundle_manifest(result, self.root / "out", self.root / "out/modules")
        manifest_path = self.root / "out/bundle-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.root / "out/modules/0001-cli").write_text("// tampered\n", encoding="latin-1")
        with self.assertRaisesRegex(extractor.ExtractError, "不一致"):
            extractor.build_reachability_index(manifest_path)

    def test_js_module_must_use_a_text_encoding(self) -> None:
        modules = [fx.FixtureModule("/$bunfs/root/cli", b"export{};\n", 0, fx.LOADER_JS, fx.ESM)]
        binary = self.root / "claude"
        fx.write_binary(binary, modules, entry_point_id=0)
        with self.assertRaisesRegex(extractor.ExtractError, "编码"):
            extractor.extract(binary, None)

    def _cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(EXTRACTOR), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=120,
        )

    def test_check_only_prints_module_graph(self) -> None:
        modules, entry = fx.chunked_modules()
        fx.write_binary(self.root / "claude", modules, entry_point_id=entry)
        completed = self._cli("--binary", str(self.root / "claude"), "--check-only")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        graph = json.loads(completed.stdout[completed.stdout.index("{"):])
        self.assertEqual(graph["entry_rule"], extractor.ENTRY_RULE_CANDIDATES)
        self.assertEqual(graph["chunk_count"], 3)
        self.assertFalse((self.root / "modules").exists())

    def test_cli_requires_manifest_for_chunked_reachability_index(self) -> None:
        modules, entry = fx.chunked_modules()
        fx.write_binary(self.root / "claude", modules, entry_point_id=entry)
        completed = self._cli(
            "--binary", str(self.root / "claude"), "--out-dir", str(self.root / "out/modules"),
            "--emit-reachability-index", str(self.root / "out/index.json"),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--emit-bundle-manifest", completed.stderr)
        completed = self._cli(
            "--binary", str(self.root / "claude"), "--out-dir", str(self.root / "out/modules"),
            "--emit-bundle-manifest", str(self.root / "out/bundle-manifest.json"),
            "--emit-reachability-index", str(self.root / "out/index.json"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        index = json.loads((self.root / "out/index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["bundle_kind"], "bun_module_manifest")


class LexicalIndexTests(unittest.TestCase):
    """多模块词法索引、单主模块退化与偏移单位。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(self, modules, entry) -> tuple[Path, dict]:
        binary = self.root / "claude"
        digest = fx.write_binary(binary, modules, entry_point_id=entry)
        result = extractor.extract(binary, digest)
        extractor.write_modules(binary, result, self.root / "out/modules")
        manifest = extractor.bundle_manifest(result, self.root / "out", self.root / "out/modules")
        path = self.root / "out/bundle-manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path, result

    def test_single_main_manifest_degenerates_to_legacy_index(self) -> None:
        """规则 ①：旧单文件口径的每个字段在清单模式下逐字段相同（行内只追加 module／offset_utf16），新增字段只追加。"""

        manifest_path, result = self._manifest(*fx.legacy_single_main_modules())
        legacy = extractor.build_reachability_index(self.root / "out/modules/0000-cli.js")
        multi = extractor.build_reachability_index(manifest_path)
        self.assertGreater(legacy["sink_total"], 0)
        strip = lambda row: {key: value for key, value in row.items() if key not in {"module", "offset_utf16"}}
        for key, value in legacy.items():
            with self.subTest(field=key):
                if key == "sinks":
                    self.assertEqual([strip(row) for row in multi["sinks"]], value)
                elif key == "probes":
                    self.assertEqual(
                        [{**probe, "hits": [strip(hit) for hit in probe["hits"]]} for probe in multi["probes"]], value
                    )
                else:
                    self.assertEqual(multi[key], value)
        self.assertEqual(multi["bundle_sha256"], result["modules"][0]["sha256"])
        self.assertEqual(multi["bundle_identity_kind"], "legacy_main_module")
        self.assertEqual(multi["bundle_manifest_sha256"], extractor.sha256_file(manifest_path))
        self.assertEqual(multi["top_level_count_scope"], "entry_module")
        totals = multi["all_modules_counts"]
        self.assertEqual(totals["analyzed_module_count"], 2)
        self.assertGreater(totals["declaration_segments"], multi["declaration_segments"])
        self.assertTrue(all(row["module"] == extractor.MAIN_MODULE for row in multi["sinks"]))

    def test_offset_mapper_counts_supplementary_characters(self) -> None:
        source = "a\U0001F600b\U0001F680c"
        mapper = extractor.utf16_offset_mapper(source)
        self.assertEqual([mapper(offset) for offset in range(len(source) + 1)], [0, 1, 3, 4, 6, 7])
        self.assertEqual(extractor.utf16_offset_mapper("ascii")(3), 3)

    def test_manifest_index_carries_module_and_utf16_offsets(self) -> None:
        manifest_path, _ = self._manifest(*fx.chunked_modules())
        index = extractor.build_reachability_index(manifest_path)
        self.assertEqual(index["bundle_sha256"], extractor.sha256_file(manifest_path))
        self.assertEqual(index["bundle_manifest_sha256"], index["bundle_sha256"])
        self.assertEqual((index["bundle_identity_kind"], index["top_level_count_scope"]),
                         ("bundle_manifest", "all_analyzed_modules"))
        self.assertEqual(index["declaration_segments"], index["all_modules_counts"]["declaration_segments"])
        websocket = [row for row in index["sinks"] if row["kind"] == "websocket"]
        self.assertEqual(len(websocket), 1)
        # chunk-b 在 WebSocket 之前有两个增补平面字符：码点偏移比 UTF-16 偏移少 2。
        self.assertEqual(websocket[0]["module"], "/$bunfs/root/chunk-bbbb2222.js")
        self.assertEqual(websocket[0]["offset_utf16"] - websocket[0]["offset"], 2)
        source = (self.root / "out/modules/0002-chunk-bbbb2222.js").read_bytes().decode("utf-16-le")
        self.assertTrue(source[websocket[0]["offset"]:].startswith("new WebSocket"))
        self.assertEqual(
            source.encode("utf-16-le")[: websocket[0]["offset_utf16"] * 2].decode("utf-16-le"),
            source[: websocket[0]["offset"]],
        )
        self.assertEqual({row["name"] for row in index["modules"]} & {"/$bunfs/root/addon.node"}, set())


def _ast_module(
    name: str, index: int, *, sinks: int, status: str = "resolved", astral: int = 0, sha256: str | None = None
) -> dict:
    return {
        "name": name,
        "index": index,
        "sha256": sha256 or hashlib.sha256(name.encode("utf-8")).hexdigest(),
        "sink_count": sinks,
        "status": status,
        "astral_code_point_count": astral,
    }


def _ast_sink(sink_id: str, module: str, start: int, end: int, reachability: str) -> dict:
    return {
        "sink_id": sink_id,
        "module": module,
        "category": "fetch",
        "semantic_sha256": "5" * 64,
        "source_start": start,
        "source_end": end,
        "owner_symbol": "f",
        "reachability": reachability,
        "privacy_keys": [],
        "relevant_literals": [],
    }


def _multi_ast(modules: list[dict], sinks: list[dict], **extra) -> dict:
    return {
        "schema_version": "claude-code-target-native-inventory/v1",
        "bundle": {"kind": "bun_module_manifest", "sha256": "a" * 64, "manifest_sha256": "a" * 64},
        "parser": {"parse_diagnostics": []},
        "offset_unit": "utf16",
        "completeness": {"truncated": False},
        "modules": modules,
        "sink_total": len(sinks),
        "sinks": sinks,
        **extra,
    }


def _lexical(rows: list[dict]) -> dict:
    return {
        "bundle_sha256": "a" * 64,
        "bundle_manifest_sha256": "a" * 64,
        "sink_inventory_truncated": False,
        "sink_total": len(rows),
        "sinks": rows,
    }


class TargetInventoryMultiModuleTests(unittest.TestCase):
    """B2-04：多模块合并、逐模块计数、无重复 sink_id 与「未决」。"""

    A = "/$bunfs/root/chunk-a.js"
    B = "/$bunfs/root/chunk-b.js"

    def _inventory(self, **overrides) -> dict:
        ast = _multi_ast(
            [_ast_module(self.A, 0, sinks=1), _ast_module(self.B, 1, sinks=1, status="unresolved")],
            [
                _ast_sink("TN-SINK-1", self.A, 10, 30, "possible_from_entry"),
                _ast_sink("TN-SINK-2", self.B, 10, 30, "unresolved_entry"),
            ],
            module_graph={"unresolved_modules": [{"name": self.B, "reason": "not_reachable_from_confirmed_entry"}]},
            unresolved_dynamic_edges=[
                {"from": self.A, "kind": "dynamic_import", "owner_symbol": "f", "source_start": 40,
                 "source_end": 50, "argument_shape": "identifier"}
            ],
        )
        lexical = _lexical([
            {"kind": "fetch", "offset": 12, "offset_utf16": 12, "module": self.A, "nearest_symbol": "f"},
            {"kind": "fetch", "offset": 12, "offset_utf16": 12, "module": self.B, "nearest_symbol": "f"},
            {"kind": "fetch", "offset": 80, "offset_utf16": 80, "module": self.A, "nearest_symbol": "doc"},
            {"kind": "fetch", "offset": 80, "offset_utf16": 80, "module": self.B, "nearest_symbol": "doc"},
        ])
        for key, value in overrides.items():
            (ast if key == "ast" else lexical).update(value)
        return build_target_inventory(ast, lexical, target_version="9.9.9", platform="linux/arm64")

    def test_counts_equal_sum_of_modules_and_sink_ids_are_unique(self) -> None:
        result = self._inventory()
        counts = result["counts"]
        for key in ("ast_sink_count", "lexical_candidate_count", "lexical_matched_ast_count",
                    "lexical_only_count", "inventory_sink_count"):
            self.assertEqual(sum(row[key] for row in counts["by_module"].values()), counts[key])
        self.assertEqual(counts["inventory_sink_count"], 4)
        identities = [row["sink_id"] for row in result["sinks"]]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertTrue(all(row["module"] in {self.A, self.B} for row in result["sinks"]))

    def test_same_offset_in_two_modules_gets_distinct_lexical_ids(self) -> None:
        result = self._inventory()
        lexical_only = [row for row in result["sinks"] if row["source_kind"] == "lexical_only"]
        self.assertEqual(len(lexical_only), 2)
        self.assertNotEqual(lexical_only[0]["sink_id"], lexical_only[1]["sink_id"])
        by_module = {row["module"]: row for row in lexical_only}
        self.assertEqual(by_module[self.A]["reachability"], "unknown")
        self.assertEqual(by_module[self.B]["reachability"], "unresolved_entry")

    def test_lexical_ids_are_content_addressed_per_module(self) -> None:
        # 模块内容摘要等于旧单文件 bundle 摘要时，词法候选身份与单文件口径相同（单主模块退化）。
        module_sha = "e" * 64
        multi = self._inventory(ast={"modules": [
            _ast_module(self.A, 0, sinks=1, sha256=module_sha), _ast_module(self.B, 1, sinks=1, status="unresolved"),
        ]})
        single = build_target_inventory(
            {
                "schema_version": "claude-code-target-native-inventory/v1",
                "bundle": {"sha256": module_sha},
                "parser": {"parse_diagnostics": []},
                "completeness": {"truncated": False},
                "sink_total": 1,
                "sinks": [_ast_sink("TN-SINK-1", self.A, 10, 30, "possible_from_entry")],
            },
            {**_lexical([{"kind": "fetch", "offset": 80, "nearest_symbol": "doc"}]), "bundle_sha256": module_sha},
            target_version="9.9.9",
            platform="linux/arm64",
        )
        single_id = next(row["sink_id"] for row in single["sinks"] if row["source_kind"] == "lexical_only")
        multi_id = next(
            row["sink_id"] for row in multi["sinks"] if row["source_kind"] == "lexical_only" and row["module"] == self.A
        )
        self.assertEqual(multi_id, single_id)

    def test_identical_module_contents_collide_and_fail_closed(self) -> None:
        same = "f" * 64
        with self.assertRaisesRegex(TargetInventoryError, "sink_id 冲突"):
            self._inventory(ast={"modules": [
                _ast_module(self.A, 0, sinks=1, sha256=same), _ast_module(self.B, 1, sinks=1, sha256=same),
            ]})

    def test_manifest_binding_must_match(self) -> None:
        with self.assertRaisesRegex(TargetInventoryError, "模块清单"):
            self._inventory(lexical={"bundle_manifest_sha256": "9" * 64})

    def test_legacy_limitations_are_kept_and_module_notes_are_appended(self) -> None:
        result = self._inventory()
        self.assertEqual(len(result["limitations"]), 3)
        self.assertIn("unresolved_entry", result["multi_module_limitations"][0])
        self.assertEqual(result["bundle_manifest_sha256"], "a" * 64)

    def test_lexical_rows_only_merge_within_their_module(self) -> None:
        result = self._inventory()
        for row in result["sinks"]:
            for candidate in row["lexical_candidates"]:
                self.assertEqual(candidate["module"], row["module"])

    def test_unresolved_entries_cover_modules_edges_and_sinks(self) -> None:
        result = self._inventory()
        kinds = sorted(row["kind"] for row in result["unresolved_entries"])
        self.assertEqual(kinds, ["dynamic_edge", "module", "sink", "sink"])
        self.assertEqual(result["completeness"]["unresolved_entry_sink_count"], 2)
        self.assertEqual(result["completeness"]["unresolved_module_count"], 1)
        self.assertEqual(result["completeness"]["unresolved_dynamic_edge_count"], 1)
        items = unresolved_discovery_items(result, ["local-analysis/x/target-sink-inventory.json"])
        self.assertEqual(len(items), 4)
        for item in items:
            self.assertEqual(
                set(item),
                {"discovery_id", "source_kind", "proposition", "disposition",
                 "semantic_candidate_ids", "spec_ids", "evidence_paths", "rationale"},
            )
            self.assertEqual(item["source_kind"], UNRESOLVED_SOURCE_KIND)
            self.assertEqual(item["disposition"], UNRESOLVED_DISPOSITION)
        self.assertEqual(len({item["discovery_id"] for item in items}), 4)

    def test_multi_module_lexical_without_utf16_offset_is_rejected(self) -> None:
        lexical_rows = [{"kind": "fetch", "offset": 12, "module": self.A, "nearest_symbol": "f"}]
        with self.assertRaisesRegex(TargetInventoryError, "offset_utf16"):
            self._inventory(lexical={"sinks": lexical_rows, "sink_total": 1})

    def test_lexical_module_must_be_analyzed_by_ast(self) -> None:
        lexical_rows = [{"kind": "fetch", "offset": 1, "offset_utf16": 1, "module": "/$bunfs/root/other.js"}]
        with self.assertRaisesRegex(TargetInventoryError, "未分析的模块"):
            self._inventory(lexical={"sinks": lexical_rows, "sink_total": 1})

    def test_module_sink_count_must_match_ast_sinks(self) -> None:
        with self.assertRaisesRegex(TargetInventoryError, "计数"):
            self._inventory(ast={"modules": [_ast_module(self.A, 0, sinks=5), _ast_module(self.B, 1, sinks=1)]})

    def test_legacy_lexical_offsets_need_astral_free_ast(self) -> None:
        def single(astral: int) -> dict:
            return {
                "schema_version": "claude-code-target-native-inventory/v1",
                "bundle": {"sha256": "a" * 64, "astral_code_point_count": astral},
                "parser": {"parse_diagnostics": []},
                "offset_unit": "utf16",
                "completeness": {"truncated": False},
                "sink_total": 1,
                "sinks": [{**_ast_sink("TN-SINK-1", self.A, 10, 30, "unknown"), "module": None}],
            }

        lexical = _lexical([{"kind": "fetch", "offset": 12, "nearest_symbol": "f"}])
        accepted = build_target_inventory(single(0), lexical, target_version="9.9.9", platform="linux/arm64")
        self.assertEqual(accepted["counts"]["lexical_matched_ast_count"], 1)
        self.assertNotIn("by_module", accepted["counts"])
        with self.assertRaisesRegex(TargetInventoryError, "增补平面"):
            build_target_inventory(single(2), lexical, target_version="9.9.9", platform="linux/arm64")

    def test_unknown_offset_unit_is_rejected(self) -> None:
        with self.assertRaisesRegex(TargetInventoryError, "坐标单位"):
            self._inventory(ast={"offset_unit": "bytes"})


class AnalyzeBundlesPolicyTests(unittest.TestCase):
    """analyze-bundles 的冻结平台、两渠道等价、容器、受管运行时与超时（不运行 node）。"""

    def test_v1_freeze_lists_only_frozen_platforms(self) -> None:
        rows = claude_fw_e._frozen_platform_binaries({
            "schema_version": claude_fw_e.SCHEMA_FREEZE,
            "platforms": ["linux/amd64"],
            "binaries": {"linux/amd64": {"path": "binaries/linux-amd64/claude", "sha256": "a" * 64},
                         "darwin/arm64": {"path": "binaries/darwin-arm64/claude", "sha256": "b" * 64}},
        })
        self.assertEqual(rows, [{"platform": "linux/amd64", "path": "binaries/linux-amd64/claude", "sha256": "a" * 64}])

    def _v2(self, **overrides) -> dict:
        row = {"path": "binaries/linux-arm64/claude", "sha256": "c" * 64, "bytes": 1}
        freeze = {
            "schema_version": claude_fw_e.SCHEMA_FREEZE_V2,
            "platforms": ["linux/arm64"],
            "binaries": {"linux/arm64": {"npm": dict(row), "gcs": dict(row)}},
            "binary_equivalence": "npm=gcs",
            "binary_sha256": "c" * 64,
        }
        freeze.update(overrides)
        return freeze

    def test_v2_freeze_uses_equivalent_channels(self) -> None:
        self.assertEqual(
            claude_fw_e._frozen_platform_binaries(self._v2()),
            [{"platform": "linux/arm64", "path": "binaries/linux-arm64/claude", "sha256": "c" * 64}],
        )

    def test_v2_divergent_channels_are_refused(self) -> None:
        with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "divergent"):
            claude_fw_e._frozen_platform_binaries(self._v2(binary_equivalence="divergent", binary_sha256=None))
        mismatched = self._v2()
        mismatched["binaries"]["linux/arm64"]["gcs"]["sha256"] = "d" * 64
        with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "不一致"):
            claude_fw_e._frozen_platform_binaries(mismatched)

    def test_unknown_freeze_schema_is_refused(self) -> None:
        with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "schema"):
            claude_fw_e._frozen_platform_binaries({**self._v2(), "schema_version": "x"})

    def test_darwin_platform_with_elf_binary_is_refused_before_macho_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modules, entry = fx.legacy_single_main_modules()
            digest = fx.write_binary(root / "freeze/binaries/darwin-arm64/claude", modules, entry_point_id=entry)
            (root / "freeze/freeze.json").write_text(json.dumps({
                "schema_version": claude_fw_e.SCHEMA_FREEZE,
                "platforms": ["darwin/arm64"],
                "binaries": {"darwin/arm64": {"path": "binaries/darwin-arm64/claude", "sha256": digest}},
            }), encoding="utf-8")
            typescript = root / "typescript.js"
            typescript.write_text("ts", encoding="utf-8")
            with mock.patch.object(claude_fw_e, "_resolve_ast_runtime", return_value={}), \
                    mock.patch.object(extractor.subprocess, "run") as otool:
                with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "容器"):
                    claude_fw_e.analyze_bundles(root / "freeze", root / "out", root / "a.json", root / "r.json")
            otool.assert_not_called()

    def test_default_managed_runtime_missing_fails_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "opt/claude-tooling/node"
            with mock.patch.object(claude_fw_e, "MANAGED_NODE_BINARY", missing):
                with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "受管 node 不存在"):
                    claude_fw_e._resolve_ast_runtime(None, None)

    def test_typescript_digest_must_match_pin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "typescript.js"
            fake.write_text("module.exports = {}\n", encoding="utf-8")
            with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "锁定"):
                claude_fw_e._resolve_ast_runtime(sys.executable, fake)
            with mock.patch.object(claude_fw_e, "MANAGED_TYPESCRIPT_MODULE", Path(directory) / "missing.js"):
                with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "受管 TypeScript 不存在"):
                    claude_fw_e._resolve_ast_runtime(sys.executable, None)

    def test_explicit_node_command_must_exist(self) -> None:
        with self.assertRaisesRegex(claude_fw_e.FWEEvidenceError, "PATH"):
            claude_fw_e._resolve_ast_runtime("no-such-node-binary-for-test", None)

    def test_ast_timeout_scales_with_module_count(self) -> None:
        self.assertEqual(claude_fw_e._ast_timeout_seconds(3), 600)
        self.assertEqual(claude_fw_e._ast_timeout_seconds(256), 600)
        self.assertEqual(claude_fw_e._ast_timeout_seconds(257), 1200)
        self.assertEqual(claude_fw_e._ast_timeout_seconds(2123), 5400)

    def test_pinned_typescript_matches_makefile_and_policy(self) -> None:
        makefile = (fx.ROOT / "Makefile").read_text(encoding="utf-8")
        pinned = re.search(r"^CAPTURE_TYPESCRIPT_SHA256 := ([0-9a-f]{64})$", makefile, re.MULTILINE)
        self.assertIsNotNone(pinned)
        self.assertEqual(claude_fw_e.PINNED_TYPESCRIPT_SHA256, pinned.group(1))
        policy = json.loads(
            (fx.ROOT / "tools/official_client_capture/claude_tool_identity_policy_v2.json").read_text(encoding="utf-8")
        )
        bindings = {item["name"]: item for item in policy["runtime_bindings"]}
        self.assertEqual(bindings["typescript"]["sha256"], claude_fw_e.PINNED_TYPESCRIPT_SHA256)
        self.assertEqual(fx.TYPESCRIPT_SHA256, claude_fw_e.PINNED_TYPESCRIPT_SHA256)


class RealGraphFixtureTests(unittest.TestCase):
    """真实版本 AST 实测夹具与模块表夹具之间的一致性（入口、跨模块边、未决清单、原生边界），不需要 node。"""

    @classmethod
    def setUpClass(cls) -> None:
        rows, reserved = _rows_285()
        cls.module_graph_285 = extractor.build_module_graph(rows, reserved)
        cls.roles_285 = {row["index"]: row["role"] for row in rows}
        cls.graph = json.loads(GRAPH_285.read_text(encoding="utf-8"))
        rows_220, reserved_220 = _rows_220()
        extractor.build_module_graph(rows_220, reserved_220)
        cls.roles_220 = {row["name"]: row["role"] for row in rows_220}
        cls.legacy = json.loads(LEGACY_220.read_text(encoding="utf-8"))

    def test_2_1_285_acceptance_counts(self) -> None:
        completeness = self.graph["completeness"]
        self.assertIs(completeness["truncated"], False)
        self.assertEqual(completeness["parse_diagnostic_count"], 0)
        self.assertEqual(completeness["module_count"], self.module_graph_285["js_module_count"])
        js_indices = {index for index, role in self.roles_285.items() if role in extractor.JS_ROLES}
        self.assertEqual({row[0] for row in self.graph["module_rows"]}, js_indices)
        self.assertEqual(self.graph["top_level_count_scope"], "all_analyzed_modules")

    def test_2_1_285_entries_are_confirmed_against_module_table(self) -> None:
        entry = self.graph["entry"]
        candidates = [index for index, role in sorted(self.roles_285.items()) if role == "entry_candidate"]
        self.assertEqual(entry["entry_candidates"], candidates)
        self.assertEqual(entry["confirmed_entries"], candidates)
        self.assertEqual(entry["rejected_entry_candidates"], [])
        self.assertEqual(entry["declared_entry"], self.module_graph_285["declared_entry_index"])
        self.assertEqual({row[0] for row in entry["entry_functions"]}, {5, 144})

    def test_2_1_285_unresolved_counts_agree_across_stages(self) -> None:
        unresolved = self.graph["unresolved"]
        completeness = self.graph["completeness"]
        # 提取阶段（规则 ②）没有未决；AST 阶段的未决模块只能是 chunk，且与 inventory 的未决清单逐项对应。
        self.assertEqual(self.module_graph_285["unresolved_count"], 0)
        self.assertEqual(len(unresolved["modules"]), completeness["unresolved_module_count"])
        self.assertEqual(len(unresolved["dynamic_edges"]), completeness["unresolved_dynamic_edge_count"])
        self.assertEqual(unresolved["entry_sink_count"], completeness["unresolved_entry_sink_count"])
        self.assertEqual(
            unresolved["inventory_entries_by_kind"],
            {"module": len(unresolved["modules"]), "dynamic_edge": len(unresolved["dynamic_edges"])},
        )
        self.assertTrue(all(self.roles_285[index] == "chunk" for index, _reason in unresolved["modules"]))
        statuses = {row[0]: row[1] for row in self.graph["module_rows"]}
        self.assertEqual({index for index, status in statuses.items() if status == "unresolved"},
                         {index for index, _reason in unresolved["modules"]})
        self.assertTrue(all(self.roles_285[row[0]] in extractor.JS_ROLES for row in unresolved["dynamic_edges"]))
        self.assertEqual((len(unresolved["modules"]), len(unresolved["dynamic_edges"])), (5, 8))

    def test_2_1_285_cross_module_edges_match_module_table(self) -> None:
        edges = self.graph["edges"]
        counts = edges["edge_counts_by_kind"]
        self.assertEqual(sum(counts.values()), edges["module_edge_count"])
        self.assertEqual(sum(row[3] for row in self.graph["module_rows"]), edges["module_edge_count"])
        self.assertEqual(sum(row[4] for row in self.graph["module_rows"]), counts["import"])
        listed = edges["listed_edges"]
        listed_counts = Counter(row[2] for row in listed)
        for kind, count in counts.items():
            if kind != "import":
                self.assertEqual(listed_counts[kind], count, kind)
        for source, target, kind, target_role, _occurrences in listed:
            self.assertIn(self.roles_285[source], extractor.JS_ROLES)
            if isinstance(target, int):
                self.assertEqual(self.roles_285[target], target_role)
            else:
                self.assertIn(target_role, {"external", "missing"})
        self.assertEqual(edges["missing_targets"], [])

    def test_2_1_285_native_boundaries_point_to_native_modules(self) -> None:
        boundaries = self.graph["edges"]["native_boundaries"]
        self.assertEqual(len(boundaries), self.module_graph_285["native_count"])
        listed = {tuple(row[:3]) for row in self.graph["edges"]["listed_edges"]}
        for source, target, kind in boundaries:
            self.assertEqual(self.roles_285[target], "native")
            self.assertIn((source, target, kind), listed)

    def test_2_1_285_sink_counts_are_consistent(self) -> None:
        sinks = self.graph["sinks"]
        counts = sinks["inventory_counts"]
        self.assertEqual(sum(row[2] for row in self.graph["module_rows"]), sinks["sink_total"])
        self.assertEqual(sum(sinks["sink_category_counts"].values()), sinks["sink_total"])
        self.assertEqual(counts["ast_sink_count"], sinks["sink_total"])
        self.assertEqual(counts["inventory_sink_count"], counts["ast_sink_count"] + counts["lexical_only_count"])
        self.assertEqual(sum(counts["reachability_counts"].values()), counts["inventory_sink_count"])

    def test_2_1_220_manifest_mode_degenerates_to_single_main(self) -> None:
        mode = self.legacy["manifest_mode"]
        graph = mode["module_graph"]
        self.assertEqual(mode["top_level_count_scope"], "entry_module")
        self.assertEqual(mode["bundle_identity_kind"], "legacy_main_module")
        self.assertEqual(graph["confirmed_entries"], [extractor.MAIN_MODULE])
        self.assertEqual(
            sorted(row["name"] for row in graph["unresolved_modules"]),
            sorted(name for name, role in self.roles_220.items() if role == "js_module"),
        )
        self.assertIs(mode["completeness"]["truncated"], False)
        self.assertEqual(mode["completeness"]["parse_diagnostic_count"], 0)
        self.assertEqual(mode["all_modules_counts"]["function_count"], sum(row["function_count"] for row in mode["modules"]))
        main = mode["modules"][0]
        self.assertEqual((main["name"], main["status"], main["sink_count"]), (extractor.MAIN_MODULE, "resolved", sum(
            row["sink_count"] for row in mode["modules"])))
        self.assertEqual(mode["inventory"]["unresolved_entry_kinds"], {
            "module": mode["completeness"]["unresolved_module_count"],
            "dynamic_edge": mode["completeness"]["unresolved_dynamic_edge_count"],
        })
        for source, target, _kind, target_role, _occurrences in mode["module_edges"]:
            if target in self.roles_220:
                self.assertEqual(self.roles_220[target], target_role)
        self.assertEqual(set(self.legacy), {"description", "binary_sha256", "ast", "manifest_mode", "lexical", "inventory"})
        for label in ("ast", "lexical", "inventory"):
            self.assertEqual(set(self.legacy[label]["shape"]), set(self.legacy[label]["field_sha256"]))


@unittest.skipUnless(os.environ.get("CLAUDE_2_1_220_LINUX_X64_BINARY"), "需要 2.1.220 linux-x64 官方二进制")
class Real2_1_220BinaryTests(unittest.TestCase):
    def test_module_table_and_anchor_fields_match_fixture(self) -> None:
        fixture = json.loads(FIXTURE_220.read_text(encoding="utf-8"))
        result = extractor.extract(Path(os.environ["CLAUDE_2_1_220_LINUX_X64_BINARY"]), fixture["binary_sha256"])
        for key in ("binary_size", "container", "bun_section", "bun_section_offset", "bun_section_size",
                    "blob_base", "byte_count", "modules_offset", "modules_length", "offsets_reserved_hex"):
            self.assertEqual(result[key], fixture[key], key)
        actual = [{key: row[key] for key in fixture["modules"][0]} for row in result["modules"]]
        self.assertEqual(actual, fixture["modules"])
        self.assertEqual(result["module_graph"]["entry_candidates"], [extractor.MAIN_MODULE])
        self.assertEqual(result["module_graph"]["module_count"], 8)


@unittest.skipUnless(os.environ.get("CLAUDE_2_1_285_LINUX_ARM64_BINARY"), "需要 2.1.285 linux-arm64 冻结二进制")
class Real2_1_285BinaryTests(unittest.TestCase):
    def test_module_table_matches_fixture(self) -> None:
        fixture = json.loads(FIXTURE_285.read_text(encoding="utf-8"))
        result = extractor.extract(Path(os.environ["CLAUDE_2_1_285_LINUX_ARM64_BINARY"]), fixture["binary_sha256"])
        self.assertEqual(result["offsets_reserved_hex"], fixture["offsets_reserved_hex"])
        actual = [
            [row["index"], row["name"], row["contents_length"], row["record_tail_hex"][-8:], row["kind"]]
            for row in result["modules"]
        ]
        self.assertEqual(actual, fixture["rows"])
        graph = result["module_graph"]
        self.assertEqual((graph["module_count"], graph["chunk_count"], len(graph["entry_candidates"])), (2352, 2119, 4))


if __name__ == "__main__":
    unittest.main()
