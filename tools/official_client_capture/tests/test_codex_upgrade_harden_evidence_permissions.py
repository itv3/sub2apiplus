"""两步式证据权限收口：预览只读、批准后 metadata-only 收口、内容漂移拒绝、幂等重放。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_harden_evidence_permissions as harden


def _write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")
    path.chmod(mode)


class HardenFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.campaign_dir = self.data / "evidence" / "campaigns" / "c1"
        self.attempt_id = "20260914T232051Z-f735b7996999027b"
        attempt_root = self.campaign_dir / "official" / "attempts" / self.attempt_id
        self.run_root = self.data / "runs" / "c1-official-core"
        (self.run_root / "mitm").mkdir(parents=True, mode=0o755)
        # mkdir 的 mode 受进程 umask 影响，显式设成非达标模式，测试才不依赖前序用例留下的 umask。
        self.run_root.chmod(0o755)
        (self.run_root / "mitm").chmod(0o755)
        (self.run_root / "mitm" / "codex-http.jsonl").write_text("{}\n", "utf-8")
        (self.run_root / "mitm" / "codex-http.jsonl").chmod(0o644)
        (self.run_root / "manifest.json").write_text("{}\n", "utf-8")
        (self.run_root / "manifest.json").chmod(0o640)
        _write_json(self.campaign_dir / "campaign.json", {"campaign_id": "c1", "campaign_mode": "formal", "configuration": {"capture_root": "/capture"}})
        _write_json(attempt_root / "attempt.json", {"attempt_id": self.attempt_id, "campaign_id": "c1", "evidence_roots": ["/capture/runs/c1-official-core", str(attempt_root / "evidence")]})
        (attempt_root / "evidence").mkdir(mode=0o750)
        (attempt_root / "evidence").chmod(0o750)
        (attempt_root / "evidence" / "probe.json").write_text("{}\n", "utf-8")
        (attempt_root / "evidence" / "probe.json").chmod(0o600)
        for path in [self.data, self.data / "evidence", self.data / "evidence" / "campaigns", self.campaign_dir, self.campaign_dir / "official", self.campaign_dir / "official" / "attempts", attempt_root, self.data / "runs"]:
            path.chmod(0o700)
        self.attempt_root = attempt_root

    def modes(self) -> dict[str, str]:
        result = {}
        for path in [self.run_root, *self.run_root.rglob("*"), self.attempt_root / "evidence", self.attempt_root / "evidence" / "probe.json"]:
            result[str(path.relative_to(self.data))] = format(stat.S_IMODE(path.lstat().st_mode), "04o")
        return result


class HardenEvidencePermissionsTests(unittest.TestCase):
    def test_preview_then_apply_hardens_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            before = fixture.modes()
            preview = harden.preview(fixture.campaign_dir, fixture.attempt_id)
            # run 根、mitm 目录、attempt evidence 目录三个目录与两个文件需要收口
            self.assertEqual(preview["change_count"], 5)
            self.assertEqual(preview["change_summary"], {"directories": 3, "files": 2})
            self.assertEqual(fixture.modes(), before, "预览不得改动任何权限")
            content_before = {e["path"]: e.get("sha256") for e in preview["entries"] if e["kind"] == "file"}
            with self.assertRaisesRegex(harden.HardenError, "批准摘要"):
                harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256="0" * 64)
            applied = harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256=preview["review_sha256"])
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["changed_count"], 5)
            self.assertTrue(applied["content_unchanged"])
            after = fixture.modes()
            self.assertTrue(all(mode in {"0700", "0600"} for mode in after.values()), after)
            self.assertEqual(applied["content_sha256_after"], preview["content_sha256"])
            second = harden.preview(fixture.campaign_dir, fixture.attempt_id)
            self.assertEqual({e["path"]: e.get("sha256") for e in second["entries"] if e["kind"] == "file"}, content_before)
            again = harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256=preview["review_sha256"])
            self.assertEqual(again["receipt_sha256"], applied["receipt_sha256"])
            replayed = harden.replay(fixture.campaign_dir, fixture.attempt_id)
            self.assertEqual(replayed["status"], "passed", replayed)
            receipts = sorted(p.name for p in (fixture.campaign_dir / "control" / "evidence-permissions" / fixture.attempt_id).iterdir())
            self.assertEqual(receipts, ["apply-01.json", "preview-01.json", "preview-02.json"])
            self.assertFalse((fixture.attempt_root / "attempt.json").read_text("utf-8").find("hardening") >= 0)

    def test_apply_refuses_content_drift_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            preview = harden.preview(fixture.campaign_dir, fixture.attempt_id)
            (fixture.run_root / "manifest.json").write_text('{"tampered": true}\n', "utf-8")
            with self.assertRaisesRegex(harden.HardenError, "内容在预览后发生变化"):
                harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256=preview["review_sha256"])
            (fixture.run_root / "link").symlink_to(fixture.run_root / "manifest.json")
            with self.assertRaisesRegex(harden.HardenError, "符号链接"):
                harden.preview(fixture.campaign_dir, fixture.attempt_id)

    def test_cli_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            self.assertEqual(harden.main(["preview", "--campaign-dir", str(fixture.campaign_dir), "--attempt-id", fixture.attempt_id]), 0)
            preview_path = fixture.campaign_dir / "control" / "evidence-permissions" / fixture.attempt_id / "preview-01.json"
            review = json.loads(preview_path.read_text("utf-8"))["review_sha256"]
            self.assertEqual(harden.main(["apply", "--campaign-dir", str(fixture.campaign_dir), "--attempt-id", fixture.attempt_id, "--approve-sha256", review]), 0)
            self.assertEqual(harden.main(["replay", "--campaign-dir", str(fixture.campaign_dir), "--attempt-id", fixture.attempt_id]), 0)
            self.assertEqual(os.stat(fixture.run_root / "mitm" / "codex-http.jsonl").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
