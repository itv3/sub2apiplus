"""精确投影索引的权限边界：未知子绑定、同摘要异坐标及遍历中漂移仍拒绝。"""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest import mock

from tools.official_client_control import delivery_execution as execution
from tools.official_client_control import sealed_gate_bindings as bindings
from tools.official_client_control.canonical import canonical_json_bytes, sha256_bytes
from tools.official_client_control.errors import ControlError


class GateProjectionIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        leaf = b"payload"
        child = {"path": "leaf.bin", "sha256": sha256_bytes(leaf), "bytes": len(leaf)}
        parent = canonical_json_bytes({"unknown_schema": "strict", "child": child})
        self.raw = {"archive/parent.json": parent, "archive/leaf.bin": leaf}
        self.archives = tuple({"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)}
                              for path, raw in self.raw.items())
        self.materials = tuple({"root_id": "root-a", "source_relative_path": name,
            "content_sha256": row["sha256"], "content_bytes": row["bytes"], "archive_binding": row}
            for row, name in zip(self.archives, ("parent.json", "leaf.bin")))
        self.proof = bindings.VerifiedGateProjection((), (), self.archives, self.materials, bindings._TOKEN)
        self.source = execution.VerifiedSource("0" * 64, "1" * 64, self.raw, "2" * 64, "fixture")

    def close(self, proof=None):
        return bindings.project_delivery_raw_bindings(self.source, [self.archives[0]],
            gate_projection=proof or self.proof)

    def test_unknown_parent_keeps_complete_transitive_readset(self):
        self.assertEqual(self.close(), sorted(self.archives, key=lambda row: row["path"]))
        self.assertEqual(self.proof.project(self.archives[0], self.raw[self.archives[0]["path"]]),
                         [self.archives[1]])

    def test_duplicate_archive_mapping_is_rejected(self):
        proof = replace(self.proof, material_files=(*self.materials, deepcopy(self.materials[0])))
        with self.assertRaisesRegex(ControlError, "映射重复"):
            self.close(proof)

    def test_same_bytes_in_another_root_cannot_supply_child(self):
        changed = deepcopy(self.materials)
        changed[1]["root_id"] = "root-b"
        with self.assertRaisesRegex(ControlError, "精确同根映射"):
            self.close(replace(self.proof, material_files=changed))

    def test_same_bytes_in_another_coordinate_are_not_authorized(self):
        row = {**self.archives[0], "path": "unknown/parent.json"}
        self.raw[row["path"]] = self.raw[self.archives[0]["path"]]
        with self.assertRaises(ControlError):
            bindings.project_delivery_raw_bindings(self.source, [row], gate_projection=self.proof)

    def test_changed_raw_bytes_are_rejected_on_next_call(self):
        self.close()
        self.raw[self.archives[1]["path"]] = b"changed"
        with self.assertRaisesRegex(ControlError, "字节漂移"):
            self.close()

    def test_mapping_changed_during_read_cannot_reuse_index(self):
        original = execution._source_bytes

        def read(source, row):
            raw = original(source, row)
            if row == self.archives[0]:
                self.materials[1]["root_id"] = "root-b"
            return raw

        with mock.patch.object(execution, "_source_bytes", side_effect=read):
            with self.assertRaisesRegex(ControlError, "精确同根映射"):
                self.close()
