"""Attempt 发布前通用证据权限收口测试。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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

    def test_isolated_rehearsal_context_skips_only_device_bound_boundary_digest(self) -> None:
        """OverlayFS 预演副本上边界摘要（含 st_dev）不可复算；隔离预演只跳过摘要比较，其余判据与正式目录相同。"""

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
            binding = permissions.receipt_binding(attempt_root, receipt_path)
            # 收据文件名固定；用"摘要被改写"的同名收据模拟 overlay 副本上必然不同的设备号。
            tampered = dict(receipt)
            tampered["boundary_sha256"] = "f" * 64
            unsigned = {k: v for k, v in tampered.items() if k != "receipt_sha256"}
            tampered["receipt_sha256"] = permissions._sha256_bytes(permissions._canonical(unsigned))
            receipt_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            receipt_path.chmod(0o600)
            tampered_binding = permissions.receipt_binding(attempt_root, receipt_path)
            replay = lambda b: permissions.replay_evidence_permission_closeout(  # noqa: E731
                attempt_root, evidence_roots, b,
                managed_data_root=runs_root.parent, logical_runs_roots=(runs_root,),
            )
            # 正式目录：摘要不一致即失败。
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                replay(tampered_binding)
            # 带预演标记但不在 overlay 上：仍失败关闭。
            with mock.patch.dict(os.environ, {permissions.REHEARSAL_CONTEXT_ENV: "1"}), mock.patch.object(
                permissions, "_mount_fstype_of", return_value="ext4"
            ):
                with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                    replay(tampered_binding)
            # 隔离预演：跳过摘要比较，但条目数漂移仍失败。
            with mock.patch.dict(os.environ, {permissions.REHEARSAL_CONTEXT_ENV: "1"}), mock.patch.object(
                permissions, "_mount_fstype_of", return_value="overlay"
            ):
                self.assertEqual(replay(tampered_binding)["entry_count"], receipt["entry_count"])
                (evidence_roots[0] / "extra-drift.bin").write_bytes(b"x")
                with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                    replay(tampered_binding)

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

    def _write_v1_receipt(self, attempt_root: Path, evidence_roots: list[Path], runs_root: Path) -> dict[str, object]:
        """按 v1 规则手工写一份历史形状的 run 末收口收据（当前工具只签发 v2）。"""

        boundary = permissions.inspect_evidence_boundary(
            attempt_root,
            evidence_roots,
            managed_data_root=runs_root.parent,
            logical_runs_roots=(runs_root,),
            rule=permissions.BOUNDARY_RULE_V1,
        )
        for entry in boundary.entries:
            if entry.mode != entry.target_mode:
                permissions._harden_entry(entry)
        boundary = permissions.inspect_evidence_boundary(
            attempt_root,
            evidence_roots,
            managed_data_root=runs_root.parent,
            logical_runs_roots=(runs_root,),
            rule=permissions.BOUNDARY_RULE_V1,
        )
        receipt = {
            "schema_version": permissions.SCHEMA_VERSION_V1,
            "status": "passed",
            "recorded_at_utc": permissions._utc_now(),
            "attempt_root": str(attempt_root),
            "evidence_roots": [str(root) for root in permissions._normalized_roots(evidence_roots)],
            "managed_runs_root": str(runs_root.parent.resolve(strict=True) / "runs"),
            "logical_runs_roots": [str(runs_root)],
            "entry_count": len(boundary.entries),
            "changed_entry_count": 0,
            "external_alias_entry_count": boundary.external_alias_entry_count,
            "boundary_sha256": boundary.boundary_sha256,
            "pre_closeout_gap_sha256": boundary.gap_sha256,
            "scanned_bytes": 0,
            "live_request_count": 0,
        }
        receipt["receipt_sha256"] = permissions._sha256_bytes(permissions._canonical(receipt))
        permissions._write_receipt_once(attempt_root / permissions.RECEIPT_FILENAME, receipt)
        return permissions.receipt_binding(attempt_root, attempt_root / permissions.RECEIPT_FILENAME)

    def test_v2_boundary_tolerates_assertion_bundle_publication(self) -> None:
        """v2：seal 前把 assertion-bundle 发布进证据根、父目录 mtime/nlink 随之变化，重放仍通过；其他新增文件仍拒绝。"""

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
            self.assertEqual(receipt["schema_version"], permissions.SCHEMA_VERSION)
            binding = permissions.receipt_binding(attempt_root, receipt_path)
            bundle = attempt_root / "evidence" / permissions.ASSERTION_BUNDLE_DIRNAME
            bundle.mkdir(mode=0o700)
            (bundle / "capture-manifest.json").write_text("{}\n", encoding="utf-8")
            (bundle / "capture-manifest.json").chmod(0o600)
            os.utime(attempt_root / "evidence", ns=(1, 2))
            replayed = permissions.replay_evidence_permission_closeout(
                attempt_root,
                evidence_roots,
                binding,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            self.assertEqual(replayed["schema_version"], permissions.SCHEMA_VERSION)
            (attempt_root / "evidence" / "late.json").write_text("{}\n", encoding="utf-8")
            (attempt_root / "evidence" / "late.json").chmod(0o600)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                permissions.replay_evidence_permission_closeout(
                    attempt_root,
                    evidence_roots,
                    binding,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )

    def test_v2_boundary_tolerates_only_declared_post_run_client_artifacts(self) -> None:
        """Kilo 后置路径由 checkpoint／finalizer 绑定；相邻的未声明新增项仍须失败。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            evidence = attempt_root / "evidence"
            (evidence / "environment").mkdir(mode=0o700)
            (evidence / "receipts").mkdir(mode=0o700)
            receipt_path, _receipt = permissions.close_evidence_permissions(
                attempt_root,
                evidence_roots,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            binding = permissions.receipt_binding(attempt_root, receipt_path)

            client = evidence / "client" / "raw"
            client.mkdir(parents=True, mode=0o700)
            (client / "kilo-facts.json").write_text("{}\n", encoding="utf-8")
            (client / "kilo-facts.json").chmod(0o600)
            client_after = evidence / "environment" / "client-after"
            client_after.mkdir(mode=0o700)
            (client_after / "probe-manifest.json").write_text("{}\n", encoding="utf-8")
            (client_after / "probe-manifest.json").chmod(0o600)
            restoration = evidence / "receipts" / "client-restoration-report.json"
            restoration.write_text("{}\n", encoding="utf-8")
            restoration.chmod(0o600)

            replayed = permissions.replay_evidence_permission_closeout(
                attempt_root,
                evidence_roots,
                binding,
                managed_data_root=runs_root.parent,
                logical_runs_roots=(runs_root,),
            )
            self.assertEqual(replayed["schema_version"], permissions.SCHEMA_VERSION)

            undeclared = evidence / "receipts" / "late.json"
            undeclared.write_text("{}\n", encoding="utf-8")
            undeclared.chmod(0o600)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                permissions.replay_evidence_permission_closeout(
                    attempt_root,
                    evidence_roots,
                    binding,
                    managed_data_root=runs_root.parent,
                    logical_runs_roots=(runs_root,),
                )

    def test_v1_receipt_upgrade_chain(self) -> None:
        """v1 收据在 bundle 发布后必然漂移；bundle 发布前升级为 v2 后，链式重放按 v2 通过，篡改或重复升级被拒。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            root.chmod(0o700)
            attempt_root, runs_root, evidence_roots = self._fixture(root)
            binding = self._write_v1_receipt(attempt_root, evidence_roots, runs_root)
            kwargs = {"managed_data_root": runs_root.parent, "logical_runs_roots": (runs_root,)}
            self.assertEqual(
                permissions.replay_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)["schema_version"],
                permissions.SCHEMA_VERSION_V1,
            )
            evidence_dir = attempt_root / "evidence"
            original = evidence_dir.lstat()
            bundle = evidence_dir / permissions.ASSERTION_BUNDLE_DIRNAME
            bundle.mkdir(mode=0o700)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                permissions.replay_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "边界漂移"):
                permissions.upgrade_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)
            bundle.rmdir()
            # v1 含父目录 mtime；只有把父目录 mtime 精确恢复到收口时的值，v1 才能重放并允许升级。
            os.utime(evidence_dir, ns=(original.st_atime_ns, original.st_mtime_ns))
            upgrade_path, upgrade = permissions.upgrade_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)
            self.assertEqual(upgrade["schema_version"], permissions.SCHEMA_VERSION)
            self.assertEqual(upgrade["predecessor"]["sha256"], binding["sha256"])
            bundle.mkdir(mode=0o700)
            (bundle / "capture-manifest.json").write_text("{}\n", encoding="utf-8")
            (bundle / "capture-manifest.json").chmod(0o600)
            replayed = permissions.replay_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)
            self.assertEqual(replayed["schema_version"], permissions.SCHEMA_VERSION)
            self.assertEqual(replayed["upgraded_from"], permissions.SCHEMA_VERSION_V1)
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "已经存在"):
                permissions.upgrade_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)
            tampered = json.loads(upgrade_path.read_text(encoding="utf-8"))
            tampered["boundary_sha256"] = "0" * 64
            upgrade_path.write_text(json.dumps(tampered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(permissions.EvidencePermissionError, "身份或零读取边界非法"):
                permissions.replay_evidence_permission_closeout(attempt_root, evidence_roots, binding, **kwargs)

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
