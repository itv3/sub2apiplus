"""修好接着跑第 66 项：隔离封存预演（OverlayFS 副本）里复核证据根取代状态只比 inode、忽略设备号。

2026-09-28 194249z VC-5 批次 30（assertion bundle）预演：断言包动作本身通过，收尾审计 ``_supersession_row_state`` 按
（st_dev, st_ino）比对取代收据绑定的原目录；预演在私有 mount namespace 的 OverlayFS 副本上执行，目录 st_dev 是 overlay
自己的设备号，与收据记录的正式设备号（2049）必然不同，三份收据全被判“原目录既不在归档路径也不在原路径（inode 漂移）”，
封存链停下。与 EvidenceManifest 同一判据：预演标记为 1 且证据根确在 overlay 上时只忽略 device，inode 照常比较；
正式目录上判据不变。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_evidence_manifest


class SupersessionRowStateRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="supersession-rehearsal-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.temp, ignore_errors=True))
        self.runs = self.temp / "runs"
        self.runs.mkdir()
        self.host_root = self.runs / "campaign-candidate-direct-core"
        self.archived_root = self.runs / "campaign-candidate-direct-core.superseded-attempt-b"
        self.archived_root.mkdir()
        self.host_root.mkdir()  # 后一 attempt 在原路径新建的目录（inode 不同）
        archived = self.archived_root.lstat()
        # 收据记录的是正式目录的设备号；叠加层里 st_dev 与之不同（这里用 +7 模拟），inode 相同。
        self.row = {
            "logical_root": "/root/oauth-capture/runs/campaign-candidate-direct-core",
            "host_root": str(self.host_root),
            "archived_host_root": str(self.archived_root),
            "archived_logical_root": "/root/oauth-capture/runs/campaign-candidate-direct-core.superseded-attempt-b",
            "device": archived.st_dev + 7,
            "inode": archived.st_ino,
        }

    def _rehearsal(self, fstype: str | None = "overlay"):
        env = mock.patch.dict(os.environ, {codex_upgrade_evidence_manifest.REHEARSAL_CONTEXT_ENV: "1"})
        mount = mock.patch.object(codex_upgrade_evidence_manifest, "_mount_fstype_of", return_value=fstype)
        return env, mount

    def test_formal_directory_keeps_device_and_inode_comparison(self) -> None:
        # 正式目录（无预演标记）：设备号不同即不一致，判据不变。
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(codex_upgrade_evidence_manifest.REHEARSAL_CONTEXT_ENV, None)
            with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "inode 漂移"):
                codex_upgrade._supersession_row_state(self.row, [self.row])

    def test_isolated_rehearsal_on_overlay_ignores_device_only(self) -> None:
        env, mount = self._rehearsal("overlay")
        with env, mount:
            self.assertEqual(codex_upgrade._supersession_row_state(self.row, [self.row]), "archived")

    def test_isolated_rehearsal_still_detects_inode_drift(self) -> None:
        drifted = {**self.row, "inode": self.row["inode"] + 1000003}
        env, mount = self._rehearsal("overlay")
        with env, mount, self.assertRaisesRegex(codex_upgrade.ConfigurationError, "inode 漂移"):
            codex_upgrade._supersession_row_state(drifted, [drifted])

    def test_rehearsal_flag_without_overlay_keeps_formal_judgement(self) -> None:
        # 带预演标记却不在 overlay 上（例如在正式目录上误带标记执行）：照旧失败关闭。
        env, mount = self._rehearsal("ext4")
        with env, mount, self.assertRaisesRegex(codex_upgrade.ConfigurationError, "inode 漂移"):
            codex_upgrade._supersession_row_state(self.row, [self.row])

    def test_isolated_rehearsal_pending_and_taken_over_states(self) -> None:
        # pending：收据已写、rename 未完成（原路径仍是原目录）。
        current = self.host_root.lstat()
        pending = {**self.row, "device": current.st_dev + 7, "inode": current.st_ino,
                   "archived_host_root": str(self.runs / "never-created")}
        # taken_over：原目录后来由另一张收据归档。
        taken = {**self.row, "archived_host_root": str(self.runs / "not-here")}
        env, mount = self._rehearsal("overlay")
        with env, mount:
            self.assertEqual(codex_upgrade._supersession_row_state(pending, [pending]), "pending")
            self.assertEqual(codex_upgrade._supersession_row_state(taken, [taken, self.row]), "taken_over")


if __name__ == "__main__":
    unittest.main()
