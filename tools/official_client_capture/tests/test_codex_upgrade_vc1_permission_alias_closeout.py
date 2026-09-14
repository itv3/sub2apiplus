"""VC-1 只读／可写双别名权限收口的离线回归测试。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import (
    codex_upgrade_vc1_permission_alias_closeout as closeout,
)


class PermissionAliasCloseoutTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _fixture(self, root: Path) -> dict[str, object]:
        campaign_id = closeout.CAMPAIGN_ID
        attempt_id = closeout.ATTEMPT_ID
        runs_root = root / "runs"
        runs_root.mkdir(mode=0o700)
        external_roots: list[Path] = []
        for index in range(30):
            if index < 2:
                suffix = "" if index == 0 else "-ws-repeat"
                evidence_root = (
                    runs_root
                    / "official-client/oauth"
                    / f"oauth-{campaign_id}{suffix}"
                )
            else:
                evidence_root = runs_root / f"{campaign_id}-job-{index:02d}"
            evidence_root.mkdir(parents=True, mode=0o700)
            external_roots.append(evidence_root)
        gap = external_roots[0] / "direct"
        gap.mkdir(mode=0o755)
        gap.chmod(0o755)

        attempt_root = (
            root
            / "data/evidence/campaigns"
            / campaign_id
            / "official/attempts"
            / attempt_id
        )
        evidence_root = attempt_root / "evidence"
        logs_root = attempt_root / "logs"
        evidence_root.mkdir(parents=True, mode=0o700)
        logs_root.mkdir(mode=0o700)
        roots = [
            *(str(path) for path in external_roots),
            str(evidence_root),
            str(logs_root),
        ]
        results: list[dict[str, object]] = []
        for index in range(29):
            assigned = [str(external_roots[index])]
            if index == 0:
                assigned.append(str(external_roots[-1]))
            results.append(
                {
                    "job_id": f"job-{index:02d}",
                    "status": "complete",
                    "evidence_roots": assigned,
                }
            )
        attempt_path = attempt_root / "attempt.json"
        self._write_json(
            attempt_path,
            {
                "campaign_id": campaign_id,
                "attempt_id": attempt_id,
                "status": "awaiting_receipts",
                "results": results,
                "evidence_roots": roots,
            },
        )
        attempt_sha256 = hashlib.sha256(attempt_path.read_bytes()).hexdigest()
        roots_sha256 = closeout.sha256_bytes(closeout.canonical_bytes(roots))
        snapshot = closeout.inspect_permission_boundary(
            attempt_path=attempt_path,
            campaign_id=campaign_id,
            attempt_id=attempt_id,
            attempt_sha256=attempt_sha256,
            roots_sha256=roots_sha256,
            readonly_runs_root=runs_root,
            writable_runs_root=runs_root,
            require_mount_modes=False,
        )
        return {
            "campaign_id": campaign_id,
            "attempt_id": attempt_id,
            "attempt_path": attempt_path,
            "attempt_sha256": attempt_sha256,
            "roots_sha256": roots_sha256,
            "runs_root": runs_root,
            "gap": gap,
            "snapshot": snapshot,
        }

    def test_apply_closeout_uses_fd_and_publishes_zero_request_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = self._fixture(root)
            snapshot = fixture["snapshot"]
            self.assertEqual(len(snapshot.gaps), 1)
            receipt_parent = root / "control"
            receipt_parent.mkdir(mode=0o700)
            receipt = receipt_parent / "receipt.json"
            with (
                mock.patch.object(closeout, "validate_alias_roots", return_value=None),
                mock.patch.multiple(
                    closeout,
                    EXPECTED_ENTRY_COUNT=len(snapshot.entries),
                    EXPECTED_GAP_COUNT=1,
                    EXPECTED_GAP_SHA256=snapshot.gap_sha256,
                ),
            ):
                payload = closeout.apply_closeout(
                    attempt_path=fixture["attempt_path"],
                    campaign_id=fixture["campaign_id"],
                    attempt_id=fixture["attempt_id"],
                    attempt_sha256=fixture["attempt_sha256"],
                    roots_sha256=fixture["roots_sha256"],
                    readonly_runs_root=fixture["runs_root"],
                    writable_runs_root=fixture["runs_root"],
                    receipt_path=receipt,
                )
            self.assertEqual(fixture["gap"].stat().st_mode & 0o777, 0o700)
            self.assertEqual(payload["changed_count"], 1)
            self.assertEqual(payload["scanned_bytes"], 0)
            self.assertEqual(payload["live_request_count"], 0)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                payload["receipt_sha256"],
                closeout.sha256_bytes(
                    closeout.canonical_bytes(
                        {key: value for key, value in payload.items() if key != "receipt_sha256"}
                    )
                ),
            )

    def test_alias_roots_must_resolve_to_same_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            with self.assertRaisesRegex(
                closeout.PermissionAliasCloseoutError,
                "不是同一 inode",
            ):
                closeout.validate_alias_roots(
                    first,
                    second,
                    require_mount_modes=False,
                )

    def test_only_frozen_tcpdump_owner_is_allowed_for_nonroot_file(self) -> None:
        metadata = mock.Mock(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=closeout.TCPDUMP_UID,
            st_gid=closeout.TCPDUMP_GID,
            st_dev=1,
            st_ino=2,
            st_size=3,
            st_mtime_ns=4,
            st_nlink=1,
        )
        for filename in ("egress.pcap", "traffic.pcap"):
            with (
                self.subTest(filename=filename),
                mock.patch.object(closeout, "reject_symlink_components"),
                mock.patch.object(Path, "lstat", side_effect=[metadata, metadata]),
            ):
                snapshot = closeout._entry_snapshot(
                    Path("/readonly") / filename,
                    Path("/writable") / filename,
                    external_alias=True,
                )
                self.assertEqual((snapshot.uid, snapshot.gid), (100, 102))

        with (
            mock.patch.object(closeout, "reject_symlink_components"),
            mock.patch.object(Path, "lstat", side_effect=[metadata, metadata]),
        ):
            with self.assertRaisesRegex(
                closeout.PermissionAliasCloseoutError,
                "证据项属主漂移",
            ):
                closeout._entry_snapshot(
                    Path("/readonly/not-pcap.bin"),
                    Path("/writable/not-pcap.bin"),
                    external_alias=True,
                )

    def test_boundary_rejects_symlink_and_hardlink(self) -> None:
        for mutation in ("symlink", "hardlink"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                fixture = self._fixture(root)
                runs_root = fixture["runs_root"]
                target_root = runs_root / f"{closeout.CAMPAIGN_ID}-job-02"
                target = target_root / "target.txt"
                target.write_text("fixture", encoding="utf-8")
                target.chmod(0o600)
                if mutation == "symlink":
                    os.symlink(target, target_root / "alias.txt")
                    pattern = "符号链接"
                else:
                    os.link(target, target_root / "hardlink.txt")
                    pattern = "硬链接"
                with self.assertRaisesRegex(
                    closeout.PermissionAliasCloseoutError,
                    pattern,
                ):
                    closeout.inspect_permission_boundary(
                        attempt_path=fixture["attempt_path"],
                        campaign_id=fixture["campaign_id"],
                        attempt_id=fixture["attempt_id"],
                        attempt_sha256=fixture["attempt_sha256"],
                        roots_sha256=fixture["roots_sha256"],
                        readonly_runs_root=runs_root,
                        writable_runs_root=runs_root,
                        require_mount_modes=False,
                    )

    def test_receipt_is_strictly_create_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve() / "control"
            parent.mkdir(mode=0o700)
            path = parent / "receipt.json"
            closeout.secure_write_receipt(path, {"status": "passed"})
            with self.assertRaises(FileExistsError):
                closeout.secure_write_receipt(path, {"status": "passed"})


if __name__ == "__main__":
    unittest.main()
