"""Claude 恢复段写侧的边界回归：首次坐标、旧扫描复用、增量封存与元数据重绑。

使用真实文件、清单、投影、合并和重绑算法；只替换 Campaign 布局与基线来源，保持离线且零上游请求。
完整断言失败到 b1 ready 的链由连续链场景负责，本模块不能替代该验收。
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_attempt_recovery as reader
from tools.official_client_capture import claude_upgrade_attempt_segment as segment
from tools.official_client_capture import claude_upgrade_candidate_seal as seal
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_segment_seal as segment_seal
from tools.official_client_capture import codex_upgrade_evidence_manifest as boundary


class SegmentPathTests(unittest.TestCase):
    def test_first_segment_coordinate_does_not_create_directory_and_reader_stays_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary).resolve()
            path = segment.segment_path(campaign, "candidate", "attempt", "ar1")
            self.assertEqual(path, campaign / "candidates/candidate/attempts/attempt/recovery/ar1")
            self.assertFalse(path.exists())
            with self.assertRaisesRegex(upgrade.ConfigurationError, "不存在"):
                reader._segment_root(campaign, "candidate", "attempt", "ar1")
            path.parent.mkdir(parents=True)
            path.symlink_to(campaign)
            with self.assertRaisesRegex(upgrade.ConfigurationError, "符号链接"):
                segment.segment_path(campaign, "candidate", "attempt", "ar1")

    def test_first_baseline_returns_authorized_scope_without_requiring_a_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            campaign = Path(temporary).resolve()
            recovery = {"kind": "attempt-recovery", "recovery_sha256": "a" * 64, "recovery_revision": "ar1"}
            commit = {"recovery_sha256": "a" * 64}
            with mock.patch.object(upgrade, "_current_evaluation_baseline", return_value=(1, commit)), \
                    mock.patch.object(upgrade, "_load_evaluation_baseline_recovery", return_value=recovery):
                self.assertEqual(segment.current_baseline(campaign, "candidate", "ar1"), (1, commit, recovery, None))
                with self.assertRaisesRegex(upgrade.ConfigurationError, "首段直接"):
                    segment.current_baseline(campaign, "candidate", "ar1", require_preview=True)


class IncrementalBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name).resolve()
        self.campaign = self.data / "evidence/campaigns/campaign"
        self.attempt = self.campaign / "candidates/candidate/attempts/attempt"
        self.segment = self.attempt / "recovery/ar1"
        self.stage = self.campaign / "evaluation/candidate/b1/capture-candidate/result.json"
        self.seal_dir = self.stage.parent / "seal/g-1234567890abcdef"
        self.old = self.data / "runs/old-retained"
        self.dropped = self.data / "runs/old-replaced"
        self.delta = self.data / "runs/replaced-recovery-ar1"
        for root in (self.old, self.dropped, self.delta, self.segment / "evidence", self.segment / "logs"):
            root.mkdir(parents=True, mode=0o700)
            path = root / "receipt.txt"
            path.write_text(root.name + "\n")
            path.chmod(0o600)
        self.summary = {
            "campaign_id": "campaign", "candidate_id": "candidate", "attempt_id": "attempt", "recovery_revision": "ar1",
            "evaluation_baseline": 1, "status": upgrade.ATTEMPT_RECOVERY_SUCCESS_STATUS,
            "evidence_roots": [str(self.segment / "evidence"), str(self.segment / "logs"), str(self.delta)],
            "results": [{"id": "replace", "evidence_roots": [str(self.delta)], "status": "complete"}],
        }
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, self.summary)
        self.previous_boundary = boundary.build_evidence_manifest([self.old, self.dropped], checkpoint_path=self.campaign / "old-scan.json")
        final = [self.old, self.delta, self.segment / "evidence", self.segment / "logs"]
        self.context = {
            "data_root": self.data, "segment_root": self.segment, "original": {"candidate_id": "candidate", "attempt_id": "attempt"},
            "revision": "ar1", "previous_boundary": self.previous_boundary,
            "root_sets": {"retained_roots": [str(self.old)], "dropped_roots": [str(self.dropped)],
                          "delta_roots": sorted(str(path) for path in final if path != self.old),
                          "final_roots": sorted(str(path) for path in final)},
        }
        for patch in (
            mock.patch.object(upgrade, "load_campaign_manifest", return_value={"campaign_id": "campaign"}),
            mock.patch.object(upgrade, "_stage_write_target", return_value=self.stage),
            mock.patch.object(capture, "_capture_layout", return_value=SimpleNamespace(host_data_root=self.data, container_data_aliases=lambda: (str(self.data),))),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(value, ensure_ascii=False))
        path.chmod(0o600)

    def binding(self, path: Path) -> dict:
        return {"path": path.relative_to(self.campaign).as_posix(), "sha256": upgrade.file_sha256(path)}

    def seal_boundary(self) -> Path:
        inventory = upgrade._evidence_inventory([self.old, self.delta], base=self.data)
        return segment_seal.seal_boundary(self.campaign, self.context, self.seal_dir, inventory,
                                         [root.relative_to(self.data).as_posix() for root in (self.old, self.delta)])

    def test_incremental_inventory_never_rescans_retained_content(self) -> None:
        previous = upgrade._evidence_inventory([self.old, self.dropped], base=self.data)
        previous_path = self.campaign / "previous-inventory.json"
        self.write(previous_path, previous)
        context = {"previous": {"seal": {"evidence_manifest": self.binding(previous_path)}}}
        real_scan = upgrade._evidence_inventory
        with mock.patch.object(upgrade, "_evidence_inventory", wraps=real_scan) as scan:
            inventory, scanned_bytes = segment_seal.incremental_inventory(self.campaign, context, [self.old, self.delta], self.data)
        scan.assert_called_once_with([self.delta], base=self.data)
        self.assertEqual(scanned_bytes, (self.delta / "receipt.txt").stat().st_size)
        self.assertEqual({entry["path"] for entry in inventory["entries"]},
                         {(root / "receipt.txt").relative_to(self.data).as_posix() for root in (self.old, self.delta)})

    def test_boundary_keeps_old_root_replaces_target_and_reuses_half_seal(self) -> None:
        old_bytes = (self.old / "receipt.txt").read_bytes()
        real_scan = boundary.build_evidence_manifest
        with mock.patch.object(boundary, "build_evidence_manifest", wraps=real_scan) as scan:
            path = self.seal_boundary()
            manifest_bytes = path.read_bytes()
            self.assertEqual(self.seal_boundary(), path)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(path.read_bytes(), manifest_bytes)
        self.assertEqual((self.old / "receipt.txt").read_bytes(), old_bytes)
        manifest = json.loads(path.read_text())
        self.assertEqual({row["path"] for row in manifest["roots"]}, set(self.context["root_sets"]["final_roots"]))
        self.assertEqual(segment_seal.segment_boundary(self.campaign, self.segment, self.summary), path)

    def test_metadata_rebind_repairs_read_side_without_changing_seal_or_source(self) -> None:
        path = self.seal_boundary()
        frozen = path.read_bytes()
        target = self.delta / "receipt.txt"
        replacement = self.data / "replacement"
        shutil.copyfile(target, replacement)
        replacement.chmod(0o600)
        replacement.replace(target)
        with self.assertRaisesRegex(seal.SealedEvidenceBoundaryError, "--attempt-recovery ar1"):
            segment_seal.segment_boundary(self.campaign, self.segment, self.summary)
        with mock.patch.object(upgrade, "_require_quiescent_campaign_lease"), \
                mock.patch.object(reader, "recovery_segment_summary", return_value=(self.segment, {}, self.summary)), \
                mock.patch.object(reader, "authoritative_execute_jobs"):
            result = segment_seal.rebind_boundary(self.campaign, "candidate", "attempt", "ar1", "operator")
        self.assertEqual(result["status"], "rebound")
        self.assertEqual(path.read_bytes(), frozen)
        self.assertEqual(segment_seal.segment_boundary(self.campaign, self.segment, self.summary), path)
        self.assertEqual(len(boundary.load_boundary_rebinds(path, json.loads(frozen))), 1)
        projected_source = segment_seal.rebound_projection_source(path, json.loads(frozen))
        boundary.verify_manifest_boundary(projected_source, [Path(row["path"]) for row in projected_source["roots"]])
        self.assertEqual(projected_source["inventory"], json.loads(frozen)["inventory"])

    def test_changed_content_and_omitted_segment_root_are_rejected(self) -> None:
        path = self.seal_boundary()
        target = self.delta / "receipt.txt"
        target.write_bytes(b"x" * target.stat().st_size)
        with mock.patch.object(upgrade, "_require_quiescent_campaign_lease"), \
                mock.patch.object(reader, "recovery_segment_summary", return_value=(self.segment, {}, self.summary)), \
                mock.patch.object(reader, "authoritative_execute_jobs"):
            with self.assertRaisesRegex(upgrade.ConfigurationError, "内容.*不一致"):
                segment_seal.rebind_boundary(self.campaign, "candidate", "attempt", "ar1", "operator")
        self.assertEqual(boundary.load_boundary_rebinds(path, json.loads(path.read_text())), [])
        forged = copy.deepcopy(self.summary)
        forged["evidence_roots"].append(str(self.dropped))
        self.write(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME, forged)
        pointer = self.segment / segment_seal.SEGMENT_BOUNDARY_FILENAME
        payload = json.loads(pointer.read_text())
        payload["summary"] = self.binding(self.segment / upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME)
        self.write(pointer, payload)
        with self.assertRaisesRegex(upgrade.ConfigurationError, "完整覆盖"):
            segment_seal.segment_boundary(self.campaign, self.segment, forged, verify=False)


if __name__ == "__main__":
    unittest.main()
