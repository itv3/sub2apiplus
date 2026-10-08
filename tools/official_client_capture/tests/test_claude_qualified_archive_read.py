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
        extra = replace(self.projection, archive_bindings=(*self.projection.archive_bindings, row))
        self.assertIn(row, extra.archive_bindings)
        with self.assertRaises(guard.GuardError):
            self.engine._read_qualification_archive_binding(row, extra)

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

    def test_validated_data_root_cannot_be_replaced_by_a_symlink(self) -> None:
        path = self.root / self.staged["path"]
        raw, before = path.read_bytes(), path.stat()
        parked = self.root.with_name(self.root.name + "-archive-read-original")
        self.root.rename(parked)
        try:
            self.root.symlink_to(parked, target_is_directory=True)
            with self.assertRaises(guard.GuardError):
                self.engine._read_qualification_archive_binding(self.staged, self.projection)
        finally:
            if self.root.is_symlink():
                self.root.unlink()
            parked.rename(self.root)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_data_root_ancestor_symlink_does_not_relocate_verified_permission(self) -> None:
        # 仅在新建测试世界中加目录别名，不改已验证原件或数据根的实际目录。
        alias = self.root.parent / (self.root.name + "-archive-read-parent-alias")
        alias.symlink_to(self.root.parent, target_is_directory=True)
        original_root = self.engine.data_root
        path = self.root / self.staged["path"]
        raw, before = path.read_bytes(), path.stat()
        try:
            self.engine.data_root = alias / self.root.name
            self.assertFalse(self.engine.data_root.is_symlink())
            with self.assertRaises(guard.GuardError):
                self.engine._read_qualification_archive_binding(self.staged, self.projection)
        finally:
            self.engine.data_root = original_root
            alias.unlink()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_verified_relative_directory_cannot_be_replaced_by_a_symlink(self) -> None:
        path = self.root / self.staged["path"]
        raw, before = path.read_bytes(), path.stat()
        directory = path.parent
        parked = directory.with_name(directory.name + "-archive-read-original")
        directory.rename(parked)
        try:
            directory.symlink_to(parked, target_is_directory=True)
            with self.assertRaises(guard.GuardError):
                self.engine._read_qualification_archive_binding(self.staged, self.projection)
        finally:
            if directory.is_symlink():
                directory.unlink()
            parked.rename(directory)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_staged_release_exception_preserves_original_path_syntax(self) -> None:
        campaign_id = self.configuration["candidate"]["qualification"]["campaign_id"]
        prefix = f"evidence/campaigns/{campaign_id}/profile/release/"
        row = self.copied(prefix + "bad\\name.json")
        invalid = {**row, "path": prefix + "bad\x00name.json"}
        for item in (row, invalid):
            with self.subTest(path=repr(item["path"])):
                extra = replace(self.projection, archive_bindings=(*self.projection.archive_bindings, item))
                self.assertIn(item, extra.archive_bindings)
                with self.assertRaises(guard.GuardError):
                    guard.relative_path(item["path"])
                with self.assertRaises(guard.GuardError):
                    self.engine._read_qualification_archive_binding(item, extra)

    def test_staged_release_exception_preserves_all_other_protected_components(self) -> None:
        campaign_id = self.configuration["candidate"]["qualification"]["campaign_id"]
        prefix = f"evidence/campaigns/{campaign_id}/profile/release/"
        for component in ("production", "production_active", "production_candidate", "production_previous", "releases", "release"):
            for spelling in (component, component.upper()):
                with self.subTest(component=spelling):
                    row = self.copied(prefix + spelling + "/copied.json")
                    extra = replace(self.projection, archive_bindings=(*self.projection.archive_bindings, row))
                    self.assertIn(row, extra.archive_bindings)
                    with self.assertRaises(guard.GuardError):
                        guard.relative_path(row["path"])
                    with self.assertRaises(guard.GuardError):
                        self.engine._read_qualification_archive_binding(row, extra)

    def test_missing_binding_or_invalid_token_cannot_grant_staged_permission(self) -> None:
        missing = replace(self.projection, archive_bindings=tuple(row for row in self.projection.archive_bindings if row != self.staged))
        invalid_token = replace(self.projection, token=object())
        for value in (missing, invalid_token):
            with self.subTest(proof="missing-binding" if value is missing else "invalid-token"):
                with self.assertRaisesRegex(guard.GuardError, "缺精确已验证来源权限"):
                    self.engine._read_qualification_archive_binding(self.staged, value)

    def test_verified_original_byte_drift_is_rejected_without_repair(self) -> None:
        path = self.root / self.staged["path"]
        changed = bytearray(path.read_bytes())
        changed[0] ^= 1
        raw = bytes(changed)
        path.write_bytes(raw)
        with self.assertRaisesRegex(guard.GuardError, "字节漂移"):
            self.engine._read_qualification_archive_binding(self.staged, self.projection)
        self.assertEqual(path.read_bytes(), raw)
