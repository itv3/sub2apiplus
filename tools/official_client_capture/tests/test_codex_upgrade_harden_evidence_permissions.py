"""两步式证据权限收口：预览只读、批准后 metadata-only 收口、内容漂移拒绝、幂等重放。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_tcpdump_owned_pcap_is_frozen_owner_boundary(self) -> None:
        """tcpdump 固定身份写出的 traffic.pcap 允许保留 100:102；其他文件或目录归别人即失败关闭。"""

        real_lstat = Path.lstat

        def fake_lstat(self: Path, owners: dict[str, tuple[int, int]]) -> os.stat_result:
            metadata = real_lstat(self)
            owner = owners.get(self.name)
            if owner is None:
                return metadata
            values = list(metadata)
            values[stat.ST_UID], values[stat.ST_GID] = owner
            return os.stat_result(tuple(values))

        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            pcap_dir = fixture.run_root / "direct" / "codex-http" / "s1"
            pcap_dir.mkdir(parents=True, mode=0o755)
            pcap_dir.chmod(0o755)
            pcap = pcap_dir / "traffic.pcap"
            pcap.write_bytes(b"\xd4\xc3\xb2\xa1pcap")
            pcap.chmod(0o640)
            with mock.patch.object(Path, "lstat", lambda self: fake_lstat(self, {"traffic.pcap": (100, 102)})):
                preview = harden.preview(fixture.campaign_dir, fixture.attempt_id)
                entry = next(e for e in preview["entries"] if e["path"] == str(pcap))
                self.assertEqual((entry["uid"], entry["gid"]), (100, 102))
                applied = harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256=preview["review_sha256"])
                self.assertEqual(applied["status"], "applied")
                self.assertEqual(harden.replay(fixture.campaign_dir, fixture.attempt_id)["status"], "passed")
            self.assertEqual(stat.S_IMODE(pcap.lstat().st_mode), 0o600)
            # 同样的数值身份落在非 pcap 文件上，或 gid 不是 tcpdump 组，都不在边界内。
            with mock.patch.object(Path, "lstat", lambda self: fake_lstat(self, {"manifest.json": (100, 102)})):
                with self.assertRaisesRegex(harden.HardenError, "属主"):
                    harden.preview(fixture.campaign_dir, fixture.attempt_id)
            with mock.patch.object(Path, "lstat", lambda self: fake_lstat(self, {"traffic.pcap": (100, 0)})):
                with self.assertRaisesRegex(harden.HardenError, "属主"):
                    harden.preview(fixture.campaign_dir, fixture.attempt_id)

    def test_upgrade_closeout_records_receipt(self) -> None:
        """upgrade-closeout 调用 seal 侧升级并把升级收据摘要登记到 control/evidence-permissions。"""

        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            attempt_path = fixture.attempt_root / "attempt.json"
            payload = json.loads(attempt_path.read_text("utf-8"))
            payload["evidence_permission_closeout"] = {"path": "evidence-permission-closeout.json", "sha256": "a" * 64, "bytes": 10}
            _write_json(attempt_path, payload)
            with self.assertRaisesRegex(harden.HardenError, "升级失败"):
                harden.upgrade_closeout(fixture.campaign_dir, fixture.attempt_id)
            upgrade_path = fixture.attempt_root / "evidence-permission-closeout-upgrade.json"
            _write_json(upgrade_path, {"boundary_sha256": "b" * 64})
            fake = (upgrade_path, {"predecessor": {"path": "evidence-permission-closeout.json", "sha256": "a" * 64, "bytes": 10}, "boundary_sha256": "b" * 64, "entry_count": 6})
            with mock.patch.object(harden.evidence_permissions, "upgrade_evidence_permission_closeout", return_value=fake) as upgrade:
                record = harden.upgrade_closeout(fixture.campaign_dir, fixture.attempt_id)
            self.assertEqual(record["status"], "upgraded")
            self.assertEqual(record["predecessor"]["sha256"], "a" * 64)
            self.assertEqual(upgrade.call_args.kwargs["managed_data_root"], fixture.data)
            self.assertTrue((fixture.campaign_dir / "control" / "evidence-permissions" / fixture.attempt_id / "closeout-upgrade-01.json").is_file())
            payload.pop("evidence_permission_closeout")
            _write_json(attempt_path, payload)
            with self.assertRaisesRegex(harden.HardenError, "无需升级"):
                harden.upgrade_closeout(fixture.campaign_dir, fixture.attempt_id)

    def test_rebind_boundary_records_metadata_drift_and_refuses_content_change(self) -> None:
        """第三批 R3：已封存清单只有 ctime 漂移时 rebind-boundary 在 attempt 根写收据成链并登记控制记录；无清单、无漂移、
        内容改变、权限位改变各拒；候选 attempt 走 candidate_id；CLI 往返。"""

        from tools.official_client_capture import codex_upgrade_evidence_manifest as evidence_manifest

        with tempfile.TemporaryDirectory() as directory:
            fixture = HardenFixture(Path(directory).resolve())
            with self.assertRaisesRegex(harden.HardenError, "没有 EvidenceManifest"):
                harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            preview = harden.preview(fixture.campaign_dir, fixture.attempt_id)
            harden.apply(fixture.campaign_dir, fixture.attempt_id, approve_sha256=preview["review_sha256"])
            _attempt_root, roots, _attempt = harden._evidence_roots(fixture.campaign_dir, fixture.attempt_id)
            manifest = evidence_manifest.build_evidence_manifest(
                roots, checkpoint_path=fixture.root / "manifest.checkpoint.json", secret_env_names=()
            )
            manifest_path = fixture.attempt_root / "evidence-manifest.json"
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(harden.HardenError, "无需 rebind"):
                harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            for root in roots:
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        path.chmod(0o400)
                        path.chmod(0o600)
            record = harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id, operator="tester")
            self.assertEqual(
                (record["status"], record["index"], record["phase"], record["candidate_id"], record["rebind_receipt"]["index"]),
                ("rebound", 1, "official", None, 1),
            )
            self.assertGreater(record["drifted_entry_count"], 0)
            rebind_path = fixture.attempt_root / "evidence-manifest-rebind-01.json"
            self.assertTrue(rebind_path.is_file())
            self.assertEqual(rebind_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(
                (fixture.campaign_dir / "control" / "evidence-permissions" / fixture.attempt_id / "boundary-rebind-01.json").is_file()
            )
            chain = evidence_manifest.load_boundary_rebinds(manifest_path, manifest)
            self.assertEqual(evidence_manifest.verify_manifest_boundary(manifest, roots, rebinds=chain)["rebind_index"], 1)
            with self.assertRaisesRegex(harden.HardenError, "无需 rebind"):
                harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            # 内容改一字节（大小不变）：复算内容拒绝；还原内容后（mtime／ctime 变）可再 rebind 成链。
            probe = fixture.attempt_root / "evidence" / "probe.json"
            probe.write_text("[]\n", "utf-8")
            with self.assertRaisesRegex(harden.HardenError, "内容与 EvidenceManifest 不一致"):
                harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            probe.write_text("{}\n", "utf-8")
            second = harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            self.assertEqual((second["index"], second["rebind_receipt"]["index"]), (2, 2))
            # 权限位漂移：完整性异常，拒绝。
            probe.chmod(0o400)
            with self.assertRaisesRegex(harden.HardenError, "不能 rebind"):
                harden.rebind_boundary(fixture.campaign_dir, fixture.attempt_id)
            probe.chmod(0o600)
            # 候选 attempt：另一 attempt_id 只在 candidates/ 下存在，不带 candidate_id 找不到 official attempt。
            cand_id = "20260914T232051Z-cand000000000001"
            candidate_root = fixture.campaign_dir / "candidates" / "cand" / "attempts" / cand_id
            (candidate_root / "evidence").mkdir(parents=True)
            for path in (
                fixture.campaign_dir / "candidates", fixture.campaign_dir / "candidates" / "cand",
                fixture.campaign_dir / "candidates" / "cand" / "attempts", candidate_root, candidate_root / "evidence",
            ):
                path.chmod(0o700)
            kilo = candidate_root / "evidence" / "kilo.json"
            kilo.write_text("{}\n", "utf-8")
            kilo.chmod(0o600)
            _write_json(candidate_root / "attempt.json", {"attempt_id": cand_id, "campaign_id": "c1", "evidence_roots": [str(candidate_root / "evidence")]})
            cand_manifest = evidence_manifest.build_evidence_manifest(
                [candidate_root / "evidence"], checkpoint_path=fixture.root / "cand.checkpoint.json", secret_env_names=()
            )
            _write_json(candidate_root / "evidence-manifest.json", cand_manifest)
            kilo.chmod(0o400)
            kilo.chmod(0o600)
            with self.assertRaises(harden.closeout.VC0CloseoutError):
                harden.rebind_boundary(fixture.campaign_dir, cand_id)
            cand_record = harden.rebind_boundary(fixture.campaign_dir, cand_id, candidate_id="cand", operator="tester")
            self.assertEqual((cand_record["phase"], cand_record["candidate_id"], cand_record["index"]), ("candidate", "cand", 1))
            self.assertTrue((candidate_root / "evidence-manifest-rebind-01.json").is_file())
            # CLI 往返：official 第三次漂移。
            probe.chmod(0o400)
            probe.chmod(0o600)
            self.assertEqual(
                harden.main(["rebind-boundary", "--campaign-dir", str(fixture.campaign_dir), "--attempt-id", fixture.attempt_id, "--operator", "cli"]),
                0,
            )
            self.assertTrue((fixture.attempt_root / "evidence-manifest-rebind-03.json").is_file())

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
