"""第 13 项：反向补丁注入器的单元测试（纯文本，本机可跑）。"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import chain_rehearsal_inject as inject

BEFORE = "line1\nline2\nOLD-A\nline4\nline5\nline6\nline7\nOLD-B\nline9\n"
AFTER = "line1\nline2\nNEW-A\nline4\nline5\nline6\nline7\nNEW-B\nNEW-B2\nline9\n"

# 修复补丁：把 BEFORE 改成 AFTER（两个块）。
PATCH = """diff --git a/pkg/file.txt b/pkg/file.txt
index 1111111..2222222 100644
--- a/pkg/file.txt
+++ b/pkg/file.txt
@@ -1,5 +1,5 @@
 line1
 line2
-OLD-A
+NEW-A
 line4
 line5
@@ -6,4 +6,5 @@
 line6
 line7
-OLD-B
+NEW-B
+NEW-B2
 line9
"""


class ReversePatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="inject-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        (self.root / "pkg").mkdir()
        self.target = self.root / "pkg" / "file.txt"
        self.target.write_text(AFTER, encoding="utf-8")

    def test_reverse_apply_restores_the_pre_fix_code(self) -> None:
        changed = inject.apply_reverse(self.root, PATCH)
        self.assertEqual(changed, ["pkg/file.txt"])
        self.assertEqual(self.target.read_text(encoding="utf-8"), BEFORE)

    def test_context_drift_raises_and_leaves_the_file_untouched(self) -> None:
        drifted = AFTER.replace("line5", "line5-changed-later")
        self.target.write_text(drifted, encoding="utf-8")
        with self.assertRaises(inject.InjectionError) as caught:
            inject.apply_reverse(self.root, PATCH)
        self.assertIn("上下文已漂移", str(caught.exception))
        self.assertEqual(self.target.read_text(encoding="utf-8"), drifted)

    def test_second_hunk_failure_does_not_leave_a_half_applied_file(self) -> None:
        drifted = AFTER.replace("NEW-B2\n", "")
        self.target.write_text(drifted, encoding="utf-8")
        with self.assertRaises(inject.InjectionError):
            inject.apply_reverse(self.root, PATCH)
        self.assertEqual(self.target.read_text(encoding="utf-8"), drifted)

    def test_missing_target_file_raises(self) -> None:
        self.target.unlink()
        with self.assertRaises(inject.InjectionError):
            inject.apply_reverse(self.root, PATCH)

    def test_ambiguous_block_picks_the_one_nearest_to_the_hunk_line(self) -> None:
        patch = """--- a/f.txt
+++ b/f.txt
@@ -10,3 +10,3 @@
 same
-old
+new
 tail
"""
        lines = ["pad"] * 2 + ["same", "new", "tail"] + ["pad"] * 4 + ["same", "new", "tail"] + ["pad"]
        path = self.root / "f.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        inject.apply_reverse(self.root, patch)
        result = path.read_text(encoding="utf-8").split("\n")
        # 补丁块标注在第 10 行：第二处（下标 9）离得近，第一处不动。
        self.assertEqual(result[3], "new")
        self.assertEqual(result[10], "old")

    def test_trailing_newline_state_is_preserved(self) -> None:
        self.target.write_text(AFTER.rstrip("\n"), encoding="utf-8")
        inject.apply_reverse(self.root, PATCH)
        self.assertEqual(self.target.read_text(encoding="utf-8"), BEFORE.rstrip("\n"))

    def test_new_file_and_deleted_file_patches_are_rejected(self) -> None:
        with self.assertRaises(inject.InjectionError):
            inject.parse_patch("--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+a\n")
        with self.assertRaises(inject.InjectionError):
            inject.parse_patch("--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n")

    def test_empty_or_hunkless_patch_is_rejected(self) -> None:
        for text in ("", "diff --git a/x b/x\nindex 1..2\n", "--- a/x\n+++ b/x\n"):
            with self.subTest(text=text):
                with self.assertRaises(inject.InjectionError):
                    inject.parse_patch(text)

    def test_no_newline_marker_is_ignored(self) -> None:
        patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n keep\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        path = self.root / "f.txt"
        path.write_text("keep\nnew", encoding="utf-8")
        inject.apply_reverse(self.root, patch)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep\nold")


class ExactReplacementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="inject-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.target = self.root / "a.py"
        self.target.write_text("if cond:\n    go()\n", encoding="utf-8")

    def test_replaces_exactly_once(self) -> None:
        inject.inject(self.root, inject.ExactReplacement("a.py", "if cond:", "if False and cond:"))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "if False and cond:\n    go()\n")

    def test_zero_or_many_occurrences_are_drift(self) -> None:
        with self.assertRaises(inject.InjectionError):
            inject.apply_replacement(self.root, inject.ExactReplacement("a.py", "missing", "x"))
        self.target.write_text("x\nx\n", encoding="utf-8")
        with self.assertRaises(inject.InjectionError):
            inject.apply_replacement(self.root, inject.ExactReplacement("a.py", "x", "y"))

    def test_unknown_injection_type_is_rejected(self) -> None:
        with self.assertRaises(inject.InjectionError):
            inject.inject(self.root, "not-an-injection")  # type: ignore[arg-type]


class FixtureDirectoryTest(unittest.TestCase):
    def test_load_patch_rejects_missing_fixture(self) -> None:
        with self.assertRaises(inject.InjectionError):
            inject.load_patch("no-such.patch")

    def test_every_committed_patch_parses(self) -> None:
        patches = sorted(inject.FIXTURE_DIRECTORY.glob("*.patch"))
        self.assertTrue(patches)
        for path in patches:
            with self.subTest(patch=path.name):
                files = inject.parse_patch(path.read_text(encoding="utf-8"))
                self.assertTrue(all(item.path.startswith("tools/") for item in files), [item.path for item in files])
                # 补丁只允许改非测试文件：测试文件是缺陷回来时必须失败的回归用例，不能被一起改回去。
                self.assertFalse(
                    any("/tests/" in item.path and item.path not in inject.ALLOWED_TEST_INFRASTRUCTURE for item in files),
                    [item.path for item in files],
                )


if __name__ == "__main__":
    unittest.main()
