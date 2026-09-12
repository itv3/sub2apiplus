"""ARM64 工具与活动文档共同部署事务的离线测试。"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import arm64_supervised_deploy as deploy
from tools.official_client_capture import codex_upgrade_supervisor as supervisor


class Arm64SupervisedDeployTest(unittest.TestCase):
    @staticmethod
    def _write_documents(root: Path, prefix: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for name in deploy.MANAGED_DOCUMENTS:
            (root / name).write_text(f"{prefix}:{name}\n", encoding="utf-8")

    @staticmethod
    def _write_runtime_documents(root: Path, prefix: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for name in deploy.MANAGED_RUNTIME_DOCUMENTS:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{prefix}:{name}\n", encoding="utf-8")

    @staticmethod
    def _exchange_contents(first: Path, second: Path) -> None:
        """在非 Linux 测试机上模拟 renameat2 的最终交换语义。"""

        if first.is_file() and second.is_file():
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()
            first.write_bytes(second_bytes)
            second.write_bytes(first_bytes)
            return
        if first.is_dir() and second.is_dir():
            first_marker = (first / "marker").read_bytes()
            second_marker = (second / "marker").read_bytes()
            (first / "marker").write_bytes(second_marker)
            (second / "marker").write_bytes(first_marker)
            return
        raise AssertionError("测试交换对象类型不一致")

    def test_document_switch_and_joint_rollback_restore_old_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_docs = root / "docs"
            transaction = production_docs / ".transaction"
            candidates = transaction / "candidates"
            backups = transaction / "backups"
            self._write_documents(production_docs, "old")
            self._write_documents(candidates, "new")
            self._write_runtime_documents(production_docs, "old")
            self._write_runtime_documents(candidates, "new")
            backups.mkdir(parents=True)

            production_tool = root / "production-tool"
            backup_tool = root / "backup-tool"
            production_tool.mkdir()
            backup_tool.mkdir()
            (production_tool / "marker").write_text("new", encoding="utf-8")
            (backup_tool / "marker").write_text("old", encoding="utf-8")

            switched: list[str] = []
            with (
                mock.patch.object(
                    deploy,
                    "atomic_exchange",
                    side_effect=self._exchange_contents,
                ),
                mock.patch.object(deploy.os, "chown"),
            ):
                deploy._switch_documents(transaction, production_docs, switched)
                self.assertEqual(
                    switched,
                    [*deploy.MANAGED_DOCUMENTS, *deploy.MANAGED_RUNTIME_DOCUMENTS],
                )
                for name in (*deploy.MANAGED_DOCUMENTS, *deploy.MANAGED_RUNTIME_DOCUMENTS):
                    self.assertTrue((production_docs / name).read_text().startswith("new:"))

                result = deploy._rollback_deployment(
                    backup_tool,
                    production_tool,
                    transaction,
                    production_docs,
                    switched,
                )

            self.assertEqual(result["rollback"], "completed")
            self.assertEqual((production_tool / "marker").read_text(), "old")
            for name in (*deploy.MANAGED_DOCUMENTS, *deploy.MANAGED_RUNTIME_DOCUMENTS):
                self.assertTrue((production_docs / name).read_text().startswith("old:"))

    def test_new_runtime_documents_are_removed_by_joint_rollback(self) -> None:
        """首次纳管的运行时依赖也必须在事务失败时恢复为不存在。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_docs = root / "docs"
            transaction = production_docs / ".transaction"
            candidates = transaction / "candidates"
            backups = transaction / "backups"
            self._write_documents(production_docs, "old")
            self._write_documents(candidates, "new")
            self._write_runtime_documents(candidates, "new")
            backups.mkdir(parents=True)

            production_tool = root / "production-tool"
            backup_tool = root / "backup-tool"
            production_tool.mkdir()
            backup_tool.mkdir()
            (production_tool / "marker").write_text("new", encoding="utf-8")
            (backup_tool / "marker").write_text("old", encoding="utf-8")

            switched: list[str] = []
            installed: list[str] = []
            with (
                mock.patch.object(
                    deploy,
                    "atomic_exchange",
                    side_effect=self._exchange_contents,
                ),
                mock.patch.object(deploy.os, "chown"),
            ):
                deploy._switch_documents(
                    transaction,
                    production_docs,
                    switched,
                    installed_documents=installed,
                )
                self.assertEqual(installed, list(deploy.MANAGED_RUNTIME_DOCUMENTS))
                result = deploy._rollback_deployment(
                    backup_tool,
                    production_tool,
                    transaction,
                    production_docs,
                    switched,
                    installed_documents=installed,
                )

            self.assertEqual(
                result["removed_installed_documents"],
                list(reversed(deploy.MANAGED_RUNTIME_DOCUMENTS)),
            )
            for name in deploy.MANAGED_RUNTIME_DOCUMENTS:
                self.assertFalse((production_docs / name).exists())

    def test_document_archive_switches_and_rolls_back_with_active_documents(self) -> None:
        """活动文档与 repository-docs 必须共用同一交换和回滚事务。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_docs = root / "docs"
            production_archive = production_docs / "repository-docs"
            transaction = production_docs / ".transaction"
            candidates = transaction / "candidates"
            candidate_archive = candidates / "repository-docs"
            backups = transaction / "backups"
            production_archive.mkdir(parents=True)
            candidate_archive.mkdir(parents=True)
            backups.mkdir(parents=True)
            backup_archive = backups / "repository-docs"
            (production_docs / "OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md").write_text(
                "old-active\n", encoding="utf-8"
            )
            (production_docs / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md").write_text(
                "old-active\n", encoding="utf-8"
            )
            for name in deploy.MANAGED_DOCUMENTS:
                (production_archive / name).write_text("old-archive\n", encoding="utf-8")
                (candidates / name).write_text("new-active\n", encoding="utf-8")
                (candidate_archive / name).write_text("new-archive\n", encoding="utf-8")
            self._write_runtime_documents(production_docs, "old")
            self._write_runtime_documents(candidates, "new")

            # 归档测试同时提供工具树回滚坐标；该测试只断言文档事务，
            # 但回滚原语会按合同收口工具树，因此不能省略其测试对象。
            production_tool = root / "production-tool"
            backup_tool = root / "backup-tool"
            production_tool.mkdir()
            backup_tool.mkdir()
            (production_tool / "marker").write_text("new", encoding="utf-8")
            (backup_tool / "marker").write_text("old", encoding="utf-8")

            switched: list[str] = []
            switched_archived: list[str] = []
            with (
                mock.patch.object(
                    deploy,
                    "atomic_exchange",
                    side_effect=self._exchange_contents,
                ),
                mock.patch.object(deploy.os, "chown"),
            ):
                deploy._switch_documents(
                    transaction,
                    production_docs,
                    switched,
                    switched_archived,
                )
                self.assertEqual(
                    switched,
                    [*deploy.MANAGED_DOCUMENTS, *deploy.MANAGED_RUNTIME_DOCUMENTS],
                )
                self.assertEqual(switched_archived, list(deploy.MANAGED_DOCUMENTS))
                for name in deploy.MANAGED_DOCUMENTS:
                    self.assertEqual((production_docs / name).read_text(), "new-active\n")
                    self.assertEqual((production_archive / name).read_text(), "new-archive\n")

                result = deploy._rollback_deployment(
                    root / "backup-tool",
                    root / "production-tool",
                    transaction,
                    production_docs,
                    switched,
                    switched_archived,
                )

            self.assertEqual(result["rollback"], "completed")
            for name in deploy.MANAGED_DOCUMENTS:
                self.assertEqual((production_docs / name).read_text(), "old-active\n")
                self.assertEqual((production_archive / name).read_text(), "old-archive\n")

    def test_default_digests_match_current_managed_tool_tree(self) -> None:
        tool_root = Path(deploy.__file__).resolve().parent / "official_client_capture"
        digest, _ = deploy.tool_digest(tool_root)
        self.assertEqual(deploy.DEFAULT_TOOL_DIGEST, digest)
        self.assertEqual(
            deploy.DEFAULT_SUPERVISOR_DIGEST,
            deploy.file_sha256(tool_root / "codex_upgrade_supervisor.py"),
        )
        assertion_preparer = Path(deploy.__file__).resolve().parent / (
            deploy.MANAGED_ASSERTION_PREPARER
        )
        self.assertEqual(
            deploy.DEFAULT_ASSERTION_PREPARER_DIGEST,
            deploy.file_sha256(assertion_preparer),
        )

    def test_assertion_preparer_switch_and_joint_rollback_are_atomic(self) -> None:
        """主工具树外的 bundle 入口必须随事务切换并可共同回滚。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_docs = root / "docs"
            transaction = production_docs / ".transaction"
            candidates = transaction / "candidates"
            backups = transaction / "backups"
            self._write_documents(production_docs, "old")
            self._write_documents(candidates, "new")
            self._write_runtime_documents(production_docs, "old")
            self._write_runtime_documents(candidates, "new")
            backups.mkdir(parents=True)

            production_tool = root / "production-tool"
            backup_tool = root / "backup-tool"
            production_tool.mkdir()
            backup_tool.mkdir()
            (production_tool / "marker").write_text("new", encoding="utf-8")
            (backup_tool / "marker").write_text("old", encoding="utf-8")

            auxiliary_candidate = root / "aux-candidate"
            auxiliary_production = root / "aux-production"
            auxiliary_backup = root / "aux-backup"
            auxiliary_candidate.write_text("new-aux", encoding="utf-8")
            auxiliary_production.write_text("old-aux", encoding="utf-8")

            switched: list[str] = []
            with (
                mock.patch.object(
                    deploy,
                    "atomic_exchange",
                    side_effect=self._exchange_contents,
                ),
                mock.patch.object(deploy, "reject_untrusted_file"),
                mock.patch.object(deploy.os, "chown"),
            ):
                deploy._switch_assertion_preparer(
                    auxiliary_candidate,
                    auxiliary_production,
                    auxiliary_backup,
                )
                self.assertEqual(auxiliary_production.read_text(), "new-aux")
                self.assertEqual(auxiliary_backup.read_text(), "old-aux")
                result = deploy._rollback_deployment(
                    backup_tool,
                    production_tool,
                    transaction,
                    production_docs,
                    switched,
                    auxiliary_backup=auxiliary_backup,
                    auxiliary_production=auxiliary_production,
                    auxiliary_switched=True,
                )

            self.assertTrue(result["restored_assertion_preparer"])
            self.assertEqual(auxiliary_production.read_text(), "old-aux")
            self.assertEqual((production_tool / "marker").read_text(), "old")

    def test_legacy_document_only_exempts_non_root_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "legacy.md"
            document.write_text("legacy\n", encoding="utf-8")
            document.chmod(0o644)
            facts = deploy.legacy_production_document_facts(document)
            self.assertEqual(facts["mode"], 0o644)
            self.assertEqual(facts["sha256"], deploy.file_sha256(document))

            document.chmod(0o664)
            with self.assertRaisesRegex(deploy.DeploymentError, "组或其他用户写入"):
                deploy.legacy_production_document_facts(document)
            document.chmod(stat.S_IFREG | 0o644)

    def test_runtime_document_rejects_symlink_parent(self) -> None:
        """运行时文档的任一父目录都不能由符号链接冒充。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            (root / "egress").symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(deploy.DeploymentError, "符号链接"):
                deploy.managed_document_path(
                    root,
                    "egress/maintenance/fact.json",
                    label="测试运行时文档",
                    allow_missing=True,
                )

    def test_runtime_document_metadata_uses_path_values(self) -> None:
        """带斜杠的仓库坐标只能作为值写入监督器 metadata。"""

        path = deploy.MANAGED_RUNTIME_DOCUMENTS[0]
        payload = {
            "runtime_document_bindings": [
                {"path": path, "sha256": "a" * 64},
            ],
            "legacy_runtime_documents": [
                {"path": path, "status": "absent"},
            ],
        }
        self.assertEqual(supervisor._metadata(payload), payload)

    def test_prepared_document_metadata_passes_supervisor_cleaning(self) -> None:
        """候选准备函数的真实返回值必须能直接写入监督器事件。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging_docs = root / "staging-docs"
            production_docs = root / "production-docs"
            transaction = production_docs / ".transaction"
            self._write_documents(staging_docs, "new")
            self._write_documents(staging_docs / "repository-docs", "new")
            self._write_runtime_documents(staging_docs, "new")
            self._write_documents(production_docs, "old")
            with (
                mock.patch.object(deploy, "reject_untrusted_file"),
                mock.patch.object(deploy.os, "chown"),
            ):
                result = deploy._prepare_document_candidates(
                    staging_docs,
                    production_docs,
                    transaction,
                )
            self.assertEqual(supervisor._metadata(result), result)
            self.assertEqual(
                [item["path"] for item in result["runtime_document_bindings"]],
                list(deploy.MANAGED_RUNTIME_DOCUMENTS),
            )

    def test_second_document_failure_can_rollback_first_and_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_docs = root / "docs"
            transaction = production_docs / ".transaction"
            candidates = transaction / "candidates"
            backups = transaction / "backups"
            self._write_documents(production_docs, "old")
            self._write_documents(candidates, "new")
            self._write_runtime_documents(production_docs, "old")
            self._write_runtime_documents(candidates, "new")
            backups.mkdir(parents=True)

            production_tool = root / "production-tool"
            backup_tool = root / "backup-tool"
            production_tool.mkdir()
            backup_tool.mkdir()
            (production_tool / "marker").write_text("new", encoding="utf-8")
            (backup_tool / "marker").write_text("old", encoding="utf-8")

            switched: list[str] = []
            failed_once = False

            def exchange_with_failure(first: Path, second: Path) -> None:
                nonlocal failed_once
                if (
                    not failed_once
                    and first.name == deploy.MANAGED_DOCUMENTS[1]
                    and first.parent.name == "candidates"
                ):
                    failed_once = True
                    raise OSError("injected document switch failure")
                self._exchange_contents(first, second)

            with (
                mock.patch.object(
                    deploy,
                    "atomic_exchange",
                    side_effect=exchange_with_failure,
                ),
                mock.patch.object(deploy.os, "chown"),
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    deploy._switch_documents(transaction, production_docs, switched)
                self.assertEqual(switched, [deploy.MANAGED_DOCUMENTS[0]])
                deploy._rollback_deployment(
                    backup_tool,
                    production_tool,
                    transaction,
                    production_docs,
                    switched,
                )

            self.assertEqual((production_tool / "marker").read_text(), "old")
            for name in deploy.MANAGED_DOCUMENTS:
                self.assertTrue((production_docs / name).read_text().startswith("old:"))

    def test_scenario_source_spec_is_recomputed_from_active_guide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool_root = root / "tools" / "official_client_capture"
            guide = root / "docs" / "CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
            tool_root.mkdir(parents=True)
            guide.parent.mkdir(parents=True)
            guide.write_text(
                "# 第一部分\n前言\n"
                "# 第二部分 Codex CLI 客户端规则画像\n规则正文\n"
                "# 第三部分\n后文\n",
                encoding="utf-8",
            )
            expected = deploy.source_spec_section_sha256(guide, "第二章")
            manifest = {
                "source_spec": {
                    "path": "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
                    "fragment": "第二章",
                    "sha256": expected,
                }
            }
            (tool_root / deploy.TARGET_SCENARIO_MANIFEST).write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with mock.patch.object(deploy, "reject_untrusted_file"):
                result = deploy.verify_scenario_source_spec(root, tool_root)
            self.assertEqual(result["source_spec_sha256"], expected)

            manifest["source_spec"]["sha256"] = "0" * 64
            (tool_root / deploy.TARGET_SCENARIO_MANIFEST).write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            with mock.patch.object(deploy, "reject_untrusted_file"):
                with self.assertRaisesRegex(deploy.DeploymentError, "第二章摘要"):
                    deploy.verify_scenario_source_spec(root, tool_root)


if __name__ == "__main__":
    unittest.main()
