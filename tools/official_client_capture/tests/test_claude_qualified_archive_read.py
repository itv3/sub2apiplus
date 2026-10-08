"""资格归档只读边界：使用原生产、导入和校验合同，生产写入禁令不变。"""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture.tests.test_claude_real_delivery_integration import (
    QualifiedFileRuntime, qualified_configuration_fixture,
)
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256, sha256_bytes
from tools.official_client_control.qualification_material_import import projection_for_configuration
from tools.official_client_control.tests.fixtures import ManagedToolTree


class QualifiedArchiveReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory(prefix="qualified-archive-tree-", dir="/private/tmp")
        cls.tree = ManagedToolTree(Path(cls.directory.name) / "repo")
        cls.addClassCleanup(cls.directory.cleanup)

    def setUp(self) -> None:
        self.enterContext(self.tree.active())
        helper, configuration = qualified_configuration_fixture(self, self.tree)
        self.root, self.configuration = helper.root, configuration
        candidate = configuration["candidate"]["qualification"]
        context_path = self.root / "control/archive-reader-configuration.json"
        guard.write_json_once(context_path, configuration)
        context = {"campaign_id": candidate["campaign_id"], "candidate_id": candidate["candidate_id"],
            "delivery_id": candidate["candidate_id"] + "-archive-read", "segment": "dr0",
            "run_nonce": candidate["candidate_id"] + "-archive-read-dr0", "data_root": str(self.root),
            "configuration_ref": guard.binding(self.root, context_path)}
        identity = executor.guard_identity(configuration, context, formal=False)
        self.engine = guard.GuardEngine(configuration, context, QualifiedFileRuntime(configuration, context), identity)
        self.addCleanup(self.engine.close)
        source = execution.VerifiedSource("0" * 64,
            configuration["guard"]["deployment_receipt_binding"]["sha256"], {},
            canonical_sha256(configuration), "qualification-readonly", self.root)
        self.projection = projection_for_configuration(helper.fixture.store, configuration, source, purpose="formed_replay")
        self.staged = next(row for row in self.projection.archive_bindings if "/profile/release/" in row["path"])

    def copied(self, relative: str) -> dict:
        """创建字节完全相同的新坐标，检验权限不会随摘要迁移。"""
        raw = (self.root / self.staged["path"]).read_bytes()
        path = self.root / relative
        guard.private_directory(path.parent)
        path.write_bytes(raw)
        path.chmod(0o600)
        return {"path": relative, "sha256": sha256_bytes(raw), "bytes": len(raw)}

    def test_verified_staged_and_content_addressed_store_releases_are_read_only(self) -> None:
        rows = [row for row in self.projection.archive_bindings
                if "/profile/release/" in row["path"] or "/objects/release_artifact/" in row["path"]]
        self.assertTrue(any("/objects/release_artifact/" in row["path"] for row in rows))
        for row in rows:
            with self.subTest(path=row["path"]):
                path = self.root / row["path"]
                before = path.stat()
                raw = path.read_bytes()
                self.assertEqual(self.engine._read_qualification_archive_binding(row, self.projection), raw)
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
                self.assertEqual(path.read_bytes(), raw)
                if "/profile/release/" in row["path"]:
                    with self.assertRaises(guard.GuardError):
                        guard.managed_path(self.root, row["path"])
                else:
                    self.assertEqual(guard.managed_path(self.root, row["path"]), path)

    def test_same_bytes_at_another_candidate_coordinate_have_no_permission(self) -> None:
        row = self.copied("evidence/campaigns/other/profile/release/copied.json")
        with self.assertRaises(guard.GuardError):
            self.engine._read_qualification_archive_binding(row, self.projection)

    def test_projection_json_and_production_namespace_cannot_grant_permission(self) -> None:
        with self.assertRaises(guard.GuardError):
            self.engine._read_qualification_archive_binding(self.staged, self.projection.manifest())
        row = self.copied("production_active/copied.json")
        # 即使内部调用者错误地拼进列表，生产坐标仍不能获得候选只读权限。
        extra = replace(self.projection, archive_bindings=(*self.projection.archive_bindings, row))
        with self.assertRaises(guard.GuardError):
            self.engine._read_qualification_archive_binding(row, extra)

    def test_validated_parent_cannot_read_a_replaced_symlink(self) -> None:
        path = self.root / self.staged["path"]
        raw = path.read_bytes()
        copy = self.root / "control/same-original.json"
        copy.write_bytes(raw)
        copy.chmod(0o600)
        path.unlink()
        path.symlink_to(copy)
        with self.assertRaises(guard.GuardError):
            self.engine._read_qualification_archive_binding(self.staged, self.projection)
