"""Attempt 发布前通用证据权限收口测试。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.official_client_capture import codex_upgrade_evidence_permissions as permissions


class EvidencePermissionCloseoutTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, list[Path]]:
        """建立同时包含外部、OAuth 嵌套和 Attempt 内部根的夹具。"""

        data_root = root / "data"
        runs_root = data_root / "runs"
        runs_root.mkdir(parents=True, mode=0o700)
        runs_root.chmod(0o700)
        campaign_dir = data_root / "evidence" / "campaigns" / "campaign-a"
        attempt_root = campaign_dir / "official" / "attempts" / "attempt-a"
        evidence_root = attempt_root / "evidence"
        logs_root = attempt_root / "logs"
        evidence_root.mkdir(parents=True, mode=0o700)
        logs_root.mkdir(mode=0o700)
        attempt_root.chmod(0o700)
        evidence_root.chmod(0o755)
        logs_root.chmod(0o755)

        direct_root = runs_root / "campaign-a-official-core"
        oauth_root = runs_root / "official-client" / "oauth" / "oauth-campaign-a"
        direct_root.mkdir(parents=True, mode=0o700)
        oauth_root.mkdir(parents=True, mode=0o700)
        direct_root.chmod(0o755)
        oauth_root.chmod(0o755)
        for path in (
            direct_root / "capture.json",
            oauth_root / "tui.log",
            evidence_root / "restoration.json",
            logs_root / "job.log",
        ):
            path.write_text('{"secret":"不得读取正文"}\n', encoding="utf-8")
            path.chmod(0o644)
        return attempt_root, runs_root, [direct_root, oauth_root, evidence_root, logs_root]

    def test_closes_real_0755_and_0644_gaps_and_replays(self) -> None:
        """通用收口必须处理两种外部根及 Attempt 内部根。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            receipt_path, receipt = permissions.close_evidence_permissions(
                attempt_root,
                evidence_roots,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )

            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(receipt["scanned_bytes"], 0)
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertGreaterEqual(receipt["changed_entry_count"], 8)
            self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
            for evidence_root in evidence_roots:
                for current, directories, files in os.walk(evidence_root):
                    self.assertEqual(stat.S_IMODE(Path(current).stat().st_mode) & 0o077, 0)
                    for name in directories:
                        self.assertEqual(
                            stat.S_IMODE((Path(current) / name).stat().st_mode) & 0o077,
                            0,
                        )
                    for name in files:
                        self.assertEqual(
                            stat.S_IMODE((Path(current) / name).stat().st_mode) & 0o077,
                            0,
                        )

            binding = permissions.receipt_binding(attempt_root, receipt_path)
            replayed = permissions.replay_evidence_permission_closeout(
                attempt_root,
                evidence_roots,
                binding,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            self.assertEqual(replayed, receipt)
            self.assertEqual(replayed["boundary_sha256"], receipt["boundary_sha256"])

    def test_rejects_symlink_and_regular_file_hardlink(self) -> None:
        """符号链接与边界外硬链接都必须在 chmod 前失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            target = evidence_roots[0] / "capture.json"
            link = evidence_roots[0] / "capture-link.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "符号链接"):
                permissions.close_evidence_permissions(
                    attempt_root,
                    evidence_roots,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )
            link.unlink()
            hardlink = root / "outside-hardlink.json"
            os.link(target, hardlink)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "硬链接"):
                permissions.close_evidence_permissions(
                    attempt_root,
                    evidence_roots,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )

    def test_rejects_alias_inode_drift(self) -> None:
        """逻辑 runs 与受管宿主 runs 不同源时不得修改任何权限。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            unrelated = root / "unrelated-runs"
            unrelated.mkdir(mode=0o700)
            logical_root = unrelated / evidence_roots[0].relative_to(runs_root)
            logical_root.mkdir(parents=True, mode=0o700)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "同一 inode"):
                permissions.close_evidence_permissions(
                    attempt_root,
                    [logical_root, *evidence_roots[2:]],
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(unrelated,),
                )

    def test_receipt_is_write_once_and_boundary_drift_is_rejected(self) -> None:
        """收据不得覆盖，收口后的元数据边界也不得漂移。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            receipt_path, _receipt = permissions.close_evidence_permissions(
                attempt_root,
                evidence_roots,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "已经存在"):
                permissions.close_evidence_permissions(
                    attempt_root,
                    evidence_roots,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )
            binding = permissions.receipt_binding(attempt_root, receipt_path)
            (evidence_roots[0] / "new.json").write_text("{}\n", encoding="utf-8")
            (evidence_roots[0] / "new.json").chmod(0o600)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                permissions.replay_evidence_permission_closeout(
                    attempt_root,
                    evidence_roots,
                    binding,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )

    def test_tcpdump_owner_exception_is_narrow(self) -> None:
        """固定 tcpdump 数值属主只允许两种 pcap 文件名。"""

        tcpdump_metadata = SimpleNamespace(st_uid=100, st_gid=102)
        self.assertTrue(
            permissions._owner_allowed(Path("traffic.pcap"), "file", tcpdump_metadata)
        )
        self.assertTrue(
            permissions._owner_allowed(Path("egress.pcap"), "file", tcpdump_metadata)
        )
        self.assertFalse(
            permissions._owner_allowed(Path("tcpdump.log"), "file", tcpdump_metadata)
        )
        self.assertFalse(
            permissions._owner_allowed(Path("traffic.pcap"), "directory", tcpdump_metadata)
        )

    def test_receipt_contains_no_evidence_content(self) -> None:
        """权限收口收据只能包含元数据，不得泄露证据正文。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            receipt_path, receipt = permissions.close_evidence_permissions(
                attempt_root,
                evidence_roots,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            raw = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn("不得读取正文", raw)
            self.assertNotIn("secret", json.dumps(receipt, ensure_ascii=False))
            self.assertEqual(receipt["scanned_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
