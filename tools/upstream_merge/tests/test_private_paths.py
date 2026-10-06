"""上游私有目录的隔离边界、Git 忽略保护与历史迁移读取测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.canonical import file_binding, validate_file_binding
from tools.upstream_merge.contracts import _assert_separate_roots, _safe_absolute_path
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.gitops import assert_clean, assert_private_path, run_git


class PrivatePathTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.repository = self.tmp / "repository"
        self.repository.mkdir()
        run_git(self.repository, "init", "-q", "-b", "main")
        run_git(self.repository, "config", "user.name", "Private Path Test")
        run_git(self.repository, "config", "user.email", "private@example.invalid")
        (self.repository / ".gitignore").write_text("/local-analysis/\n", encoding="utf-8")
        run_git(self.repository, "add", ".gitignore")
        run_git(self.repository, "commit", "-q", "-m", "私有目录测试基线")
        self.private = self.repository / "local-analysis" / "upstream"

    def test_external_and_designated_internal_roots_are_allowed(self) -> None:
        for parent in (self.tmp / "outside", self.private / "v1.0.1-20261006-001"):
            with self.subTest(parent=parent):
                _assert_separate_roots(self.repository, parent / "worktree", parent / "evidence")

    def test_other_repository_paths_and_broad_roots_are_rejected(self) -> None:
        for path in (
            self.repository,
            self.repository / "evidence",
            self.repository / "local-analysis" / "other",
            self.repository / "local-analysis" / "upstream-copy",
            Path.home(),
            Path("/"),
        ):
            with self.subTest(path=path), self.assertRaises(UpstreamMergeError):
                _assert_separate_roots(self.repository, path, self.tmp / "evidence")

    def test_internal_root_requires_whole_directory_ignore(self) -> None:
        (self.repository / ".gitignore").write_text("/local-analysis/upstream/*.json\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "整体被 Git 忽略"):
            assert_private_path(self.repository, self.private / "output.json", "证据")

    def test_force_tracked_file_blocks_private_root(self) -> None:
        self.private.mkdir(parents=True)
        tracked = self.private / "tracked.json"
        tracked.write_text("{}\n", encoding="utf-8")
        run_git(self.repository, "add", "-f", str(tracked))
        with self.assertRaisesRegex(UpstreamMergeError, "已跟踪文件"):
            assert_private_path(self.repository, self.private / "output.json", "证据")

    def test_symlinks_and_parent_traversal_cannot_open_source_directories(self) -> None:
        source = self.repository / "source"
        source.mkdir()
        self.private.parent.mkdir(parents=True)
        self.private.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(UpstreamMergeError, "主仓库之外"):
            assert_private_path(self.repository, self.private / "evidence", "证据")
        self.private.unlink()
        self.private.mkdir()
        (self.private / "escape").symlink_to(source, target_is_directory=True)
        for path in (self.private / "escape" / "evidence", self.private / ".." / "other"):
            with self.subTest(path=path), self.assertRaisesRegex(UpstreamMergeError, "主仓库之外"):
                assert_private_path(self.repository, path, "证据")

    def test_worktree_and_evidence_still_cannot_overlap(self) -> None:
        worktree = self.private / "worktree"
        for evidence in (worktree, worktree / "evidence", self.private):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(UpstreamMergeError, "不得互相嵌套"):
                _assert_separate_roots(self.repository, worktree, evidence)

    def test_nested_git_worktree_keeps_parent_repository_clean(self) -> None:
        worktree = self.private / "v1.0.1-20261006-001" / "worktree"
        _assert_separate_roots(self.repository, worktree, worktree.parent / "evidence")
        run_git(self.repository, "worktree", "add", "--detach", str(worktree), "HEAD")
        try:
            self.assertTrue((worktree / ".git").is_file())
            assert_private_path(self.repository, worktree, "隔离 worktree")
            assert_clean(self.repository, "主仓库")
        finally:
            run_git(self.repository, "worktree", "remove", str(worktree))

    def test_migrated_parent_alias_preserves_binding_and_detects_content_change(self) -> None:
        old = self.tmp / "old-upstream"
        evidence = old / "v1.0.1-20261006-001" / "evidence"
        evidence.mkdir(parents=True, mode=0o700)
        original = evidence / "receipt.json"
        original.write_text('{"result":"finalized"}\n', encoding="utf-8")
        binding = file_binding(original)
        self.private.parent.mkdir(parents=True)
        old.rename(self.private)
        old.symlink_to(self.private, target_is_directory=True)
        self.assertEqual(validate_file_binding(binding, "历史收据"), binding)
        actual = _safe_absolute_path(str(evidence), "历史 evidence")
        self.assertEqual(actual, self.private / "v1.0.1-20261006-001" / "evidence")
        _assert_separate_roots(self.repository, actual.parent / "worktree", actual)
        (actual / "receipt.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "内容摘要或字节数漂移"):
            validate_file_binding(binding, "历史收据")


if __name__ == "__main__":
    unittest.main()
