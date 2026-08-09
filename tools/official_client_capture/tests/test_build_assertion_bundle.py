"""ACC-02 断言证据包必须是可重放的只读字节复制，任何漂移都失败关闭。

bundle 是断言器唯一可见的证据根；收口不可信则后续 seal／accept 全部失真，
因此负例必须覆盖同名根／文件、路径逃逸、双类链接与两个方向的漂移。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parents[1]))

import build_assertion_bundle as bundle  # noqa: E402


class AssertionBundleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="assertion-bundle-test-"))
        self.addCleanup(self._cleanup)
        self.root_a = self.workdir / "oauth-campaign"
        self.root_b = self.workdir / "relay-compact"
        (self.root_a / "mitm").mkdir(parents=True)
        self.root_b.mkdir()
        (self.root_a / "mitm" / "models.jsonl").write_bytes(b"models-flow\n")
        (self.root_b / "conn001.bin").write_bytes(b"GET / HTTP/1.1\r\n")
        self.roots = {"oauth-campaign": self.root_a, "relay-compact": self.root_b}
        self.plan = [
            {
                "root": "oauth-campaign",
                "path": "mitm/models.jsonl",
                "target": "oauth-campaign/mitm/models.jsonl",
            },
            {
                "root": "relay-compact",
                "path": "conn001.bin",
                "target": "relay-compact/conn001.bin",
            },
        ]
        self.bundle_dir = self.workdir / "assertion-bundle"

    def _cleanup(self) -> None:
        for path in sorted(self.workdir.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.chmod(0o644) if path.is_file() and not path.is_symlink() else None
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.workdir.rmdir()

    def _write_plan(self) -> Path:
        path = self.workdir / "plan.json"
        path.write_text(
            json.dumps({"entries": self.plan}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path


class AssertionBundleBuildTest(AssertionBundleTestBase):
    def test_build_and_verify_round_trip(self) -> None:
        provenance = bundle.build_bundle(
            self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
        )
        self.assertEqual(provenance["entry_count"], 2)
        entry = provenance["entries"][0]
        self.assertEqual(
            entry["source_inventory_path"], "oauth-campaign/mitm/models.jsonl"
        )
        self.assertEqual(entry["source_sha256"], entry["target_sha256"])
        target = self.bundle_dir / entry["target_path"]
        self.assertEqual(target.read_bytes(), b"models-flow\n")
        self.assertFalse(os.access(target, os.W_OK))
        replay = bundle.verify_bundle(self.roots, self.bundle_dir)
        self.assertEqual(
            replay["provenance_sha256"], provenance["provenance_sha256"]
        )

    def test_rebuild_is_deterministic(self) -> None:
        plan = bundle.load_plan(self._write_plan())
        first = bundle.build_bundle(self.roots, plan, self.bundle_dir)
        second = bundle.build_bundle(
            self.roots, plan, self.workdir / "assertion-bundle-2"
        )
        self.assertEqual(first["provenance_sha256"], second["provenance_sha256"])
        self.assertEqual(first["entries"], second["entries"])

    def test_existing_bundle_dir_rejected(self) -> None:
        self.bundle_dir.mkdir()
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.build_bundle(
                self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
            )

    def test_duplicate_root_name_rejected(self) -> None:
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.parse_source_roots(
                [
                    f"oauth-campaign={self.root_a}",
                    f"oauth-campaign={self.root_b}",
                ]
            )

    def test_duplicate_target_rejected(self) -> None:
        self.plan.append(dict(self.plan[0], root="relay-compact", path="conn001.bin"))
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.load_plan(self._write_plan())

    def test_duplicate_source_rejected(self) -> None:
        self.plan.append(dict(self.plan[0], target="another/models.jsonl"))
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.load_plan(self._write_plan())

    def test_path_escape_rejected(self) -> None:
        for bad in ("../secret", "/etc/hosts", "a\\b", "a//b", "a/./b"):
            with self.subTest(path=bad):
                plan = [dict(self.plan[0], path=bad)]
                self.plan = plan
                with self.assertRaises(bundle.AssertionBundleError):
                    bundle.load_plan(self._write_plan())

    def test_symlink_source_rejected(self) -> None:
        link = self.root_a / "mitm" / "alias.jsonl"
        link.symlink_to(self.root_a / "mitm" / "models.jsonl")
        self.plan = [
            {
                "root": "oauth-campaign",
                "path": "mitm/alias.jsonl",
                "target": "oauth-campaign/mitm/alias.jsonl",
            }
        ]
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.build_bundle(
                self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
            )

    def test_symlink_directory_segment_rejected(self) -> None:
        (self.root_a / "linked").symlink_to(self.root_a / "mitm")
        self.plan = [
            {
                "root": "oauth-campaign",
                "path": "linked/models.jsonl",
                "target": "oauth-campaign/linked/models.jsonl",
            }
        ]
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.build_bundle(
                self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
            )

    def test_hardlink_source_rejected(self) -> None:
        hard = self.root_a / "mitm" / "hard.jsonl"
        os.link(self.root_a / "mitm" / "models.jsonl", hard)
        self.plan = [
            {
                "root": "oauth-campaign",
                "path": "mitm/hard.jsonl",
                "target": "oauth-campaign/mitm/hard.jsonl",
            }
        ]
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.build_bundle(
                self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
            )


class AssertionBundleVerifyTest(AssertionBundleTestBase):
    def setUp(self) -> None:
        super().setUp()
        bundle.build_bundle(
            self.roots, bundle.load_plan(self._write_plan()), self.bundle_dir
        )

    def test_source_drift_detected(self) -> None:
        (self.root_a / "mitm" / "models.jsonl").write_bytes(b"tampered\n")
        with self.assertRaises(bundle.AssertionBundleError) as raised:
            bundle.verify_bundle(self.roots, self.bundle_dir)
        self.assertIn("来源", str(raised.exception))

    def test_target_drift_detected(self) -> None:
        target = self.bundle_dir / "relay-compact" / "conn001.bin"
        target.chmod(0o644)
        target.write_bytes(b"tampered\n")
        with self.assertRaises(bundle.AssertionBundleError) as raised:
            bundle.verify_bundle(self.roots, self.bundle_dir)
        self.assertIn("bundle 内容", str(raised.exception))

    def test_unregistered_file_rejected(self) -> None:
        (self.bundle_dir / "stray.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(bundle.AssertionBundleError) as raised:
            bundle.verify_bundle(self.roots, self.bundle_dir)
        self.assertIn("未登记", str(raised.exception))

    def test_allowed_extra_prefix_passes(self) -> None:
        derived = self.bundle_dir / "derived"
        derived.mkdir()
        (derived / "obs.jsonl").write_text("{}", encoding="utf-8")
        bundle.verify_bundle(
            self.roots, self.bundle_dir, allowed_extra_prefixes=("derived/",)
        )

    def test_tampered_provenance_digest_rejected(self) -> None:
        path = self.bundle_dir / bundle.PROVENANCE_FILENAME
        document = json.loads(path.read_text(encoding="utf-8"))
        document["entries"][0]["target_sha256"] = "0" * 64
        path.chmod(0o644)
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.verify_bundle(self.roots, self.bundle_dir)

    def test_missing_root_rejected(self) -> None:
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.verify_bundle(
                {"oauth-campaign": self.root_a}, self.bundle_dir
            )


class ManifestKindCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.required = {
            "A01": ["pcap", "relay_binary"],
            "A09": ["process_trace", "relay_binary"],
        }
        self.manifest = {
            "artifacts": [
                {"kind": "pcap", "scenario_ids": ["A01"], "path": "a.pcap"},
                {"kind": "relay_binary", "scenario_ids": ["A01", "A09"], "path": "b.bin"},
                {"kind": "process_trace", "scenario_ids": ["A09"], "path": "c.jsonl"},
            ]
        }

    def test_full_coverage_passes(self) -> None:
        bundle.verify_manifest_kind_coverage(self.manifest, self.required)

    def test_missing_kind_fails_closed(self) -> None:
        self.manifest["artifacts"] = [
            artifact
            for artifact in self.manifest["artifacts"]
            if artifact["kind"] != "process_trace"
        ]
        with self.assertRaises(bundle.AssertionBundleError) as raised:
            bundle.verify_manifest_kind_coverage(self.manifest, self.required)
        self.assertIn("A09:process_trace", str(raised.exception))

    def test_empty_manifest_fails_closed(self) -> None:
        with self.assertRaises(bundle.AssertionBundleError):
            bundle.verify_manifest_kind_coverage({"artifacts": []}, self.required)


if __name__ == "__main__":
    unittest.main()
