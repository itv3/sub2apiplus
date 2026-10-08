"""D4 单点兼容：两份 Codex reader 必须覆盖真实恢复证明及其三个读取边界。"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy

TOOL_ROOT = Path(__file__).resolve().parents[1]


def reader_closures(root: Path) -> dict[str, dict]:
    """按未经修改的正式 v2 算法重算完整闭包，不借用缓存或扩大屏障。"""

    active = policy.load_policy(root / policy.POLICY_FILENAME)
    digests = policy._managed_tree_digests(root)
    return {
        label: policy.evaluator_reader_closure(active, root, list(roots), digests)
        for label, roots in (("compare", policy.EVALUATOR_COMPARE_READER_ROOTS),
                             ("accept", policy.EVALUATOR_ACCEPT_READER_ROOTS))
    }


class StaticSegmentProofClosureTests(unittest.TestCase):
    def test_original_proof_predicates_are_in_both_readers(self) -> None:
        required = {"segment_reuse_proofs", "recovery_job_inventory", "_after_probe_bound_by_restoration"}
        for label, closure in reader_closures(TOOL_ROOT).items():
            with self.subTest(reader=label):
                names = {row["name"] for row in closure["symbols"] if row["module"] == "codex_upgrade_reconciler"}
                self.assertTrue(required <= names, f"{label} reader 遗漏恢复证明判据：{sorted(required - names)}")

    def test_real_proof_boundary_edits_change_both_readers(self) -> None:
        """只在副本翻转三个既有比较，不执行变异代码；摘要必须敏感于真实证明逻辑变化。"""

        changes = {
            "权威 J* 范围": ('if sorted(recovery["execute_jobs"]) != sorted(frozen_job_ids):',
                             'if sorted(recovery["execute_jobs"]) == sorted(frozen_job_ids):'),
            "逐文件 stat 稳定": (') != (\n                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns',
                                 ') == (\n                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns'),
            "after 内容摘要": ('or _file_sha256(path) != item.get("sha256")',
                               'or _file_sha256(path) == item.get("sha256")'),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            shutil.copytree(TOOL_ROOT, root, ignore=shutil.ignore_patterns("tests", "versions", "__pycache__"))
            target = root / "codex_upgrade_reconciler.py"
            original = target.read_text(encoding="utf-8")
            before = reader_closures(root)
            for boundary, (old, new) in changes.items():
                with self.subTest(boundary=boundary):
                    self.assertEqual(original.count(old), 1, "变异必须只命中指定的一个读取边界")
                    target.write_text(original.replace(old, new, 1), encoding="utf-8")
                    try:
                        after = reader_closures(root)
                        for label in before:
                            self.assertNotEqual(before[label]["closure_sha256"], after[label]["closure_sha256"],
                                                f"{label} reader 没有覆盖{boundary}")
                    finally:
                        target.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
