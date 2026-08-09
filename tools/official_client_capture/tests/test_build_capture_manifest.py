"""capture manifest 生成器不得静默漏证据或放宽形态约束。

official 侧此前每轮手写一次性脚本、候选侧根本没有产出方。正规化之后必须守住：规则匹配
不到文件要失败关闭（漏登记会让断言在不完整集合上通过），kind／parser／场景标识只认闭集。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_capture_manifest as builder  # noqa: E402


def _seed(root: Path) -> None:
    for relative in (
        "direct/codex-http/s1/traffic.pcap",
        "direct/codex-ws/s1/traffic.pcap",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode("utf-8"))


BASE_RULE = {
    "glob": "direct/*/s1/traffic.pcap",
    "kind": "pcap",
    "parser": "pcap_client_hello",
    "scenario_ids": ["A01"],
    "labels": {"side": "official", "transport": "direct"},
    "labels_from_path": {"surface": 1},
}


class CaptureManifestBuilderTest(unittest.TestCase):
    def test_按规则登记并计算真实摘要(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            manifest = builder.build_manifest(
                evidence_root=root,
                codex_version="0.147.0",
                capture_id="official-test-core",
                rules=[BASE_RULE],
            )
            self.assertEqual(manifest["schema_version"], builder.MANIFEST_SCHEMA)
            self.assertEqual(len(manifest["artifacts"]), 2)
            first = manifest["artifacts"][0]
            expected = hashlib.sha256(
                (root / first["path"]).read_bytes()
            ).hexdigest()
            self.assertEqual(first["sha256"], expected)
            # labels_from_path 让 surface 随目录变化，而不是写死在规则里。
            surfaces = {item["labels"]["surface"] for item in manifest["artifacts"]}
            self.assertEqual(surfaces, {"codex-http", "codex-ws"})

    def test_规则匹配不到文件必须失败关闭(self) -> None:
        """漏登记会让断言在一个不完整的证据集合上通过，比缺少 manifest 更危险。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            with self.assertRaises(builder.CaptureManifestError):
                builder.build_manifest(
                    evidence_root=root,
                    codex_version="0.147.0",
                    capture_id="official-test-core",
                    rules=[{**BASE_RULE, "glob": "direct/*/s9/traffic.pcap"}],
                )

    def test_同一份证据不得被重复登记(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            with self.assertRaises(builder.CaptureManifestError):
                builder.build_manifest(
                    evidence_root=root,
                    codex_version="0.147.0",
                    capture_id="official-test-core",
                    rules=[BASE_RULE, dict(BASE_RULE)],
                )

    def test_形态与场景标识只认闭集(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            for bad in (
                {"kind": "screenshot"},
                {"parser": "eyeball"},
                {"scenario_ids": ["first"]},
                {"scenario_ids": []},
            ):
                with self.subTest(rule=bad):
                    with self.assertRaises(builder.CaptureManifestError):
                        builder.build_manifest(
                            evidence_root=root,
                            codex_version="0.147.0",
                            capture_id="official-test-core",
                            rules=[{**BASE_RULE, **bad}],
                        )

    def test_产物按路径排序且可复算(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            kwargs = {
                "evidence_root": root,
                "codex_version": "0.147.0",
                "capture_id": "official-test-core",
                "rules": [BASE_RULE],
            }
            first = builder.build_manifest(**kwargs)
            second = builder.build_manifest(**kwargs)
            self.assertEqual(first, second)
            paths = [item["path"] for item in first["artifacts"]]
            self.assertEqual(paths, sorted(paths))

    def test_版本必须是三段数字(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _seed(root)
            with self.assertRaises(builder.CaptureManifestError):
                builder.build_manifest(
                    evidence_root=root,
                    codex_version="0.147",
                    capture_id="official-test-core",
                    rules=[BASE_RULE],
                )


if __name__ == "__main__":
    unittest.main()
