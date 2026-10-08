"""新 typed 世界的官方侧测试原件闭包；不运行完整链或登记正式来源。

三个父引用和全部请求、响应、伴随文件必须由原严格归档函数实读。
缺件、漂移及未知 JSON 的下层绑定都保持失败关闭；旧轻量来源不变。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture.tests import claude_evaluator_fixture as evaluator
from tools.official_client_capture.tests import claude_vc5_chain_fixture as fixture
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_control.sealed_gate_bindings import project_delivery_raw_bindings


class OfficialSourceClosureFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="typed-official-source-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.campaign = self.data / "evidence/campaigns/claude-code-2_1_285-fixture"
        self.campaign.mkdir(parents=True)
        self.side = evaluator.write_side(self.root / "evaluation-input", capture_id="eval-official")
        self.package = {"object_kind": "evidence_package", "sha256": "c" * 64}
        self.source_verifiers = dict(execution.SOURCE_VERIFIERS)

    def tearDown(self) -> None:
        self.assertEqual(execution.SOURCE_VERIFIERS, self.source_verifiers)

    def materialize(self) -> dict:
        return fixture.materialize_official_source(campaign=self.campaign, data_root=self.data,
            side=self.side, package_ref=self.package)

    def closure(self, source: dict) -> list[dict]:
        # 比较父合同以 Campaign 为根投影三个引用；普通 JSON 内部则直接
        # 使用数据根坐标。这里把同三份原件作为初始父，保留原严格递归。
        roots = [{**source[name], "path":
                  (self.campaign.relative_to(self.data) / source[name]["path"]).as_posix()}
                 for name in ("capture_manifest", "capture_index", "inventory")]
        channel = execution.VerifiedSource("0" * 64, "1" * 64, {}, "2" * 64,
                                           "regression-fixture", self.data)
        return project_delivery_raw_bindings(channel, roots, gate_projection=None)

    def test_default_source_keeps_original_placeholders_and_writes_nothing(self) -> None:
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        source = fixture.official_source(self.side, self.package)
        self.assertEqual(source["capture_manifest"],
            {"path": "seam/official/capture-manifest.json", "sha256": "e" * 64, "bytes": 1})
        self.assertEqual(source["capture_index"],
            {"path": "seam/official/capture-index.json", "sha256": "e" * 64, "bytes": 1})
        self.assertEqual(source["inventory"],
            {"path": "seam/official/evidence-manifest.json", "sha256": "e" * 64, "source": "seam"})
        self.assertEqual({path.relative_to(self.root).as_posix(): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}, before)

    def test_full_closure_reads_all_existing_request_response_and_manifest_bytes(self) -> None:
        originals = {path.relative_to(self.side.evidence_root).as_posix(): path.read_bytes()
                     for path in self.side.evidence_root.rglob("*") if path.is_file()}
        original_evaluation = self.side.manifest_path.read_bytes()
        source = self.materialize()
        source_root = self.campaign / "seam/official"
        for relative, raw in originals.items():
            self.assertEqual((source_root / "evidence" / relative).read_bytes(), raw)
            self.assertEqual((self.side.evidence_root / relative).read_bytes(), raw)
        self.assertEqual(self.side.manifest_path.read_bytes(), original_evaluation)
        closed = self.closure(source)
        expected = {path.relative_to(self.data).as_posix() for path in source_root.rglob("*") if path.is_file()}
        self.assertEqual({row["path"] for row in closed}, expected)
        self.assertEqual((len(originals), len(closed)), (12, 17))
        self.assertEqual(source["package_ref"], self.package)
        provenance = json.loads((source_root / "fixture-origin.json").read_bytes())
        self.assertTrue(provenance["fixture_only"])
        self.assertFalse(provenance["formal_version_semantics_certified"])

    def test_three_parent_references_and_nested_inventory_use_exact_roots(self) -> None:
        source = self.materialize()
        self.assertEqual(set(source), compare.SOURCE_FIELDS)
        self.assertEqual(source["evidence_root"], str(self.data))
        for name in ("capture_manifest", "capture_index", "inventory"):
            self.assertEqual(set(source[name]), {"path", "sha256", "bytes"})
            self.assertEqual(source[name], fixture.binding(self.campaign / source[name]["path"], self.campaign))
        inventory = json.loads((self.campaign / source["inventory"]["path"]).read_bytes())
        self.assertEqual(inventory["entry_count"], 16)
        self.assertEqual(inventory["total_bytes"], sum(row["bytes"] for row in inventory["entries"]))
        self.assertEqual(inventory["digest"], canonical_sha256({"entries": inventory["entries"]}))
        for row in inventory["entries"]:
            self.assertEqual(row, fixture.binding(self.data / row["path"], self.data))
        evaluation = source["evaluation_manifest"]
        self.assertEqual(len(evaluation["artifacts"]), 12)
        for artifact in evaluation["artifacts"]:
            self.assertEqual({key: artifact[key] for key in ("path", "sha256", "bytes")},
                             fixture.binding(self.data / artifact["path"], self.data))

    def test_index_preserves_actual_connections_and_request_lines(self) -> None:
        source = self.materialize()
        index = json.loads((self.campaign / source["capture_index"]["path"]).read_bytes())
        probes = {row["probe_id"]: row for row in index["jobs"][0]["probes"]}
        self.assertEqual([row["connection_id"] for row in probes["failure-529"]["connections"]], [1, 2])
        self.assertEqual([row["requests"] for row in probes["failure-529"]["connections"]],
            [["POST /v1/messages?beta=true HTTP/1.1"], ["POST /v1/messages?beta=true HTTP/1.1"]])
        self.assertEqual(probes["print"]["connections"][0]["requests"],
            ["POST /v1/messages?beta=true HTTP/1.1", "POST /v1/messages/count_tokens?beta=true HTTP/1.1"])
        self.assertEqual(source["probe_request_paths"], fixture.PROBE_REQUEST_PATHS)
        self.assertTrue(any(row["count"] == 3 and "/v1/messages?" in row["surface"]
                            for row in source["surfaces"]))

    def test_each_missing_or_drifted_parent_reference_is_rejected_by_original_archive_reader(self) -> None:
        source = self.materialize()
        for name in ("capture_manifest", "capture_index", "inventory"):
            with self.subTest(parent=name):
                path = self.campaign / source[name]["path"]
                raw = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaisesRegex(ControlError, "原件缺失"):
                        self.closure(source)
                finally:
                    path.write_bytes(raw)
            with self.subTest(parent=name, fault="drift"):
                path.write_bytes(raw + b" ")
                try:
                    with self.assertRaisesRegex(ControlError, "字节漂移"):
                        self.closure(source)
                finally:
                    path.write_bytes(raw)

    def test_every_missing_or_drifted_raw_companion_is_rejected(self) -> None:
        source = self.materialize()
        evidence_root = self.campaign / "seam/official/evidence"
        for path in sorted(path for path in evidence_root.rglob("*") if path.is_file()):
            raw = path.read_bytes()
            with self.subTest(raw=path.name, fault="missing"):
                path.unlink()
                try:
                    with self.assertRaisesRegex(ControlError, "原件缺失"):
                        self.closure(source)
                finally:
                    path.write_bytes(raw)
            with self.subTest(raw=path.name, fault="drift"):
                path.write_bytes(raw + b" ")
                try:
                    with self.assertRaisesRegex(ControlError, "字节漂移"):
                        self.closure(source)
                finally:
                    path.write_bytes(raw)

    def test_unknown_json_nested_binding_is_still_strictly_read(self) -> None:
        source = self.materialize()
        index_path = self.campaign / source["capture_index"]["path"]
        index = json.loads(index_path.read_bytes())
        missing = "evidence/campaigns/claude-code-2_1_285-fixture/seam/official/missing-child.json"
        index["unknown_fixture_metadata"] = {"child": {"path": missing, "sha256": "e" * 64, "bytes": 1}}
        fixture._write_json(index_path, index)
        inventory_path = self.campaign / source["inventory"]["path"]
        inventory = json.loads(inventory_path.read_bytes())
        for position, row in enumerate(inventory["entries"]):
            if row["path"] == index_path.relative_to(self.data).as_posix():
                inventory["entries"][position] = fixture.binding(index_path, self.data)
        inventory["total_bytes"] = sum(row["bytes"] for row in inventory["entries"])
        inventory["digest"] = canonical_sha256({"entries": inventory["entries"]})
        fixture._write_json(inventory_path, inventory)
        source["capture_index"] = fixture.binding(index_path, self.campaign)
        source["inventory"] = fixture.binding(inventory_path, self.campaign)
        with self.assertRaisesRegex(ControlError, missing):
            self.closure(source)

    def test_duplicate_materialization_rejects_without_overwriting_originals(self) -> None:
        self.materialize()
        before = {path: path.read_bytes() for path in self.campaign.rglob("*") if path.is_file()}
        with self.assertRaises(FileExistsError):
            self.materialize()
        self.assertEqual({path: path.read_bytes() for path in self.campaign.rglob("*") if path.is_file()}, before)

    def test_campaign_outside_data_root_rejects_before_any_materialization(self) -> None:
        outside = self.root / "outside-campaign"
        outside.mkdir()
        with self.assertRaises(ValueError):
            fixture.materialize_official_source(campaign=outside, data_root=self.data,
                side=self.side, package_ref=copy.deepcopy(self.package))
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
