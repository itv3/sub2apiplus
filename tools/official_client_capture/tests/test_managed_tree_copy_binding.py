"""execution_tree_binding_required 在三种主机形态下的判定。

CI runner 以非 root 用户运行，/root 存在但不可进入；此前 is_dir() 抛 PermissionError，
让依赖副本树的评估测试在 CI 上全部报错（本地 macOS 与 ARM64 都复现不了）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture.tests import managed_tree_copy


class ExecutionTreeBindingRequiredTest(unittest.TestCase):
    def test_permission_denied_means_no_production_copy(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError(13, "Permission denied")):
            self.assertFalse(managed_tree_copy.execution_tree_binding_required())
            self.assertFalse(managed_tree_copy.execution_tree_binding_available())

    def test_missing_production_copy(self) -> None:
        with mock.patch.object(Path, "is_dir", return_value=False):
            self.assertFalse(managed_tree_copy.execution_tree_binding_required())

    def test_present_production_copy_still_requires_binding(self) -> None:
        with mock.patch.object(Path, "is_dir", return_value=True):
            self.assertTrue(managed_tree_copy.execution_tree_binding_required())

    def test_other_os_errors_are_not_swallowed(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                managed_tree_copy.execution_tree_binding_required()


if __name__ == "__main__":
    unittest.main()
