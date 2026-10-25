"""D1（第 10a 项配套）：后台验证的工作区放在固定测试树坐标上。

承接要求当前测试树的绝对路径等于来源记录里的 ``read_audit.repo_root``。原先后台验证的工作区按“完整提交＋部署绑定”各放一处，换一次提交就换一次
坐标，跨提交承接结构上不可能；现在缺省放在每客户端一个的固定位置 ``<数据根的上一级>/entry-gates-source-work/fixed/<客户端>``。显式 ``--work`` 仍可指向旧的
提交绑定坐标，前台入口门禁的工作区照旧被拒绝。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.ci import background_validation as bv
from tools.official_client_capture.tests.test_ci_background_validation import COMMIT_A, COMMIT_B, BackgroundValidationTests


class FixedWorkCoordinateTests(unittest.TestCase):
    def setUp(self) -> None:
        # 借用既有后台验证测试的世界（假入口门禁、真实后台子进程），不把它的用例再跑一遍。
        self.case = BackgroundValidationTests("test_passed_result_is_bound_to_commit_and_deployment")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def start_args(self, commit: str, *extra: str) -> list[str]:
        case = self.case
        return ["start", "--runroot", str(case.runroot), "--data-root", str(case.data), "--bundle", str(case.root / "x.bundle"),
                "--branch", "codex/test", "--commit", commit, "--entry-gates", str(case.gates), *extra]

    def test_the_default_work_is_the_fixed_coordinate_and_is_the_same_across_commits(self) -> None:
        case = self.case
        fixed = bv.fixed_work_root(case.data)
        self.assertEqual(fixed, case.root / "entry-gates-source-work" / "fixed" / "codex")
        first = case.wait(case.start(COMMIT_A)["result"])
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["work"], str(fixed))
        self.assertEqual(first["input_contract"]["work"], str(fixed))
        self.assertEqual(first["cache_roots"]["GOCACHE"], str(fixed / "go-build-cache"))
        second = case.wait(case.start(COMMIT_B)["result"])
        self.assertEqual(second["status"], "passed")
        self.assertEqual(second["work"], first["work"], "换一次提交，测试树坐标不变——跨提交承接才有可能")
        self.assertEqual(Path(first["work"]) / "test-tree", fixed / "test-tree")

    def test_the_fixed_coordinate_is_per_client(self) -> None:
        data = self.case.data
        self.assertNotEqual(bv.fixed_work_root(data, "codex"), bv.fixed_work_root(data, "claude"))
        self.assertEqual(bv.fixed_work_root(data, "claude").name, "claude")

    def test_explicit_commit_bound_work_is_still_accepted_and_the_foreground_work_is_still_refused(self) -> None:
        case = self.case
        sha = bv.latest_deployment(case.data)["sha256"]
        bound = case.runroot / "background-validation" / "work" / f"{COMMIT_A}-{sha}" / "legacy"
        completed = case.cli(*self.start_args(COMMIT_A, "--work", str(bound)))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = case.wait(json.loads(completed.stdout.strip().splitlines()[-1])["result"])
        self.assertEqual(payload["work"], str(bound))
        refused = case.cli(*self.start_args(COMMIT_B, "--work", str(case.root / "entry-gates-work")))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        elsewhere = case.cli(*self.start_args(COMMIT_B, "--work", str(case.root / "entry-gates-source-work" / "fixed" / "claude")))
        self.assertEqual(elsewhere.returncode, 2, "别的客户端的固定坐标不能借用：" + elsewhere.stderr)

    def test_the_fixed_work_subcommand_prints_the_coordinate(self) -> None:
        case = self.case
        completed = case.cli("fixed-work", "--data-root", str(case.data))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(bv.fixed_work_root(case.data)))


if __name__ == "__main__":
    unittest.main()
