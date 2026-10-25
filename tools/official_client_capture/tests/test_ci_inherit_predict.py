"""第 11 项：修复轮影响集预览 ``tools/ci/inherit_predict.py``。

预览是权威判定（``unit_records.check_record`` 的窄化分支）的保守超集——任何被权威拒绝的单元必须在预览里，多报可以，漏报不行。
本文件用真实文件系统改动逐场景对拍：同一个改动先让权威判一次（``NarrowedWorld.check``），再把同一改动写成变更集交给 ``predict_affected``，
断言“权威拒绝 ⇒ 预览里有”，并固定预览自己的预期（哪些该报、哪些不该报）。另测 git 变更集的解析、一票否决、整树遍历签名下的大小豁免和命令行。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ci import inherit_predict as predict
from tools.official_client_capture.tests import test_ci_inherit_view as view
from tools.official_client_capture.tests.test_ci_inherit_view import FILE_MODE, make_dir, same_size_change, write_file


class PredictBase(view.NarrowedWorld):
    def predicted(self, record: dict, changes: predict.ChangeSet, **options) -> list[str]:
        return predict.predict_affected({self.UNIT: record}, changes, **options).get(self.UNIT, [])

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.tree))


class AgainstTheAuthorityTests(PredictBase):
    def scenario(self, record: dict, *, change: predict.ChangeSet, must_predict: bool | None, label: str) -> None:
        """现在文件系统已改动：权威拒绝 ⇒ 预览必须有；预览自己的预期（must_predict）单独断言。"""
        problems, _ = self.check(record)
        reasons = self.predicted(record, change)
        if problems:
            self.assertTrue(reasons, f"{label}：权威拒绝了，预览却漏报：{problems}")
        if must_predict is not None:
            self.assertEqual(bool(reasons), must_predict, f"{label}：预览的预期不符：{reasons}")

    def test_t_p1_rejected_by_the_authority_implies_predicted_for_every_kind_of_edit(self) -> None:
        record = self.publish()
        cases = []

        def case(label, action, undo, change, must_predict):
            cases.append((label, action, undo, change, must_predict))

        read, stat, unused = self.read, self.stat, self.unused
        case("改被读文件", lambda: same_size_change(read), lambda: same_size_change(read), predict.ChangeSet(modified={self.rel(read)}), True)
        case("改被 stat 文件（同大小，权威放过，预览多报）", lambda: same_size_change(stat), lambda: same_size_change(stat),
             predict.ChangeSet(modified={self.rel(stat)}), True)
        case("改没被访问的文件", lambda: unused.write_text("没被访问的新内容"), lambda: unused.write_text("unused-12345678"),
             predict.ChangeSet(modified={self.rel(unused)}), False)
        case("删除被读文件", lambda: read.unlink(), lambda: write_file(read, "read-me-12345678"), predict.ChangeSet(deleted={self.rel(read)}), True)
        extra = self.listed / "c.txt"
        case("被枚举目录新增文件", lambda: write_file(extra, "丙"), lambda: extra.unlink(), predict.ChangeSet(added={self.rel(extra)}), True)
        new_stat = self.statdir / "new.txt"
        case("只 stat 的目录新增文件", lambda: write_file(new_stat, "新"), lambda: new_stat.unlink(), predict.ChangeSet(added={self.rel(new_stat)}), False)
        victim = self.listed / "a.txt"
        case("被枚举目录删除文件", lambda: victim.unlink(), lambda: write_file(victim, "甲"), predict.ChangeSet(deleted={self.rel(victim)}), True)
        case("来源判不存在的路径现在存在", lambda: write_file(self.absent, "出现"), lambda: self.absent.unlink(),
             predict.ChangeSet(added={self.rel(self.absent)}), True)
        case("改权限", lambda: os.chmod(stat, 0o600), lambda: os.chmod(stat, FILE_MODE),
             predict.ChangeSet(modified={self.rel(stat)}, mode_changed={self.rel(stat)}), True)
        newtop = self.tree / "newfile.txt"
        case("仓库根新增文件（根目录没被访问）", lambda: write_file(newtop, "新"), lambda: newtop.unlink(), predict.ChangeSet(added={"newfile.txt"}), False)
        for label, action, undo, change, must_predict in cases:
            with self.subTest(case=label):
                action()
                try:
                    self.scenario(record, change=change, must_predict=must_predict, label=label)
                finally:
                    undo()

    def test_t_p2_adding_a_new_top_level_name_under_a_listed_ancestor_is_reported_but_a_known_name_is_not(self) -> None:
        """目录项只有名字：p 在被枚举祖先下的第一层名字原本就在目录项里（新增），说明那一层目录早就存在，祖先的目录项不变。"""
        record = self.publish()
        known = predict.ChangeSet(added={"listed/sub/deeper.txt"})
        self.assertTrue(self.predicted(record, known), "listed 的直接子名 sub 不在来源目录项里：listed 的目录项会多一项")
        statdir_deeper = predict.ChangeSet(added={"statdir/x.txt/never.txt"})
        self.assertEqual(self.predicted(record, statdir_deeper), [], "statdir 没被枚举")
        inside = predict.ChangeSet(added={"listed/a.txt/oops.txt"})
        self.assertEqual(self.predicted(record, inside), [], "第一层名字 a.txt 原本就在 listed 的目录项里：listed 的目录项不变")

    def test_t_p3_veto_makes_every_unit_affected(self) -> None:
        record = self.publish()
        veto = predict.ChangeSet(type_changed={"somewhere/else.txt"}, veto=["somewhere/else.txt：类型变化"])
        result = predict.predict_affected({self.UNIT: record, "other_unit": record}, veto)
        self.assertEqual(set(result), {self.UNIT, "other_unit"})
        self.assertIn("一票否决", result[self.UNIT][0])

    def test_t_p4_units_without_a_narrowable_source_always_rerun(self) -> None:
        legacy = self.world.records[self.UNIT]
        result = predict.predict_affected({self.UNIT: legacy, "no_source": None}, predict.ChangeSet())
        self.assertEqual(set(result), {self.UNIT, "no_source"})
        self.assertIn("必重跑", result[self.UNIT][0])

    def test_t_p5_git_readers_rerun_when_the_commit_changes(self) -> None:
        git_head = self.tree / ".git" / "HEAD"
        write_file(git_head, "ref: refs/heads/main\n")
        lines = [*self.trace_lines(), f'1 openat(AT_FDCWD<{self.tree}>, "{git_head}", O_RDONLY) = 3<{git_head}>']
        inputs = [*self.record_inputs(), view.ra.host_snapshot_v3(str(git_head))]
        record = self.publish(run_id="git-reader", inputs=inputs, lines=lines)
        self.assertEqual(self.predicted(record, predict.ChangeSet(head_changed=False)), [])
        reasons = self.predicted(record, predict.ChangeSet(head_changed=True))
        self.assertTrue(any(".git/HEAD" in reason for reason in reasons), reasons)

    def test_t_p6_changed_whole_entries_are_reported(self) -> None:
        record = self.publish()
        self.assertEqual(self.predicted(record, predict.ChangeSet()), [])
        self.assertTrue(self.predicted(record, predict.ChangeSet(), changed_entries={"runtime-test-spec"}))
        self.assertEqual(self.predicted(record, predict.ChangeSet(), changed_entries={"not-an-entry"}), [])

    def test_t_p7_a_directory_removed_as_a_whole_affects_units_that_looked_at_it(self) -> None:
        record = self.publish()
        gone = predict.ChangeSet(deleted={"statdir/x.txt"})
        reasons = self.predicted(record, gone)
        self.assertTrue(any("statdir" in reason and "整个被删除" in reason for reason in reasons), reasons)
        self.assertEqual(self.predicted(record, predict.ChangeSet(deleted={"unlisted/y.txt"})), [], "没被访问的目录整个删掉与本单元无关")


class WalkPreviewTests(view.WalkExemptionTests):
    """整树遍历签名下只被 stat 的测试文件：预览与权威一致地放过大小变化，权限变化、删除、新增仍报。"""

    def predicted(self, record: dict, changes: predict.ChangeSet) -> list[str]:
        return predict.predict_affected({self.UNIT: record}, changes).get(self.UNIT, [])

    def test_t_p8_stat_only_test_file_edits_do_not_affect_a_walking_unit(self) -> None:
        record = self.publish()
        edited = self.test_files[9]
        relative = str(edited.relative_to(self.tree))
        self.assertEqual(self.predicted(record, predict.ChangeSet(modified={relative})), [])
        self.assertTrue(self.predicted(record, predict.ChangeSet(modified={relative}, mode_changed={relative})))
        self.assertTrue(self.predicted(record, predict.ChangeSet(deleted={relative})))
        self.assertTrue(self.predicted(record, predict.ChangeSet(added={str(self.tests_dir.relative_to(self.tree) / "test_new.py")})))
        # 权威对同一类改动的判定：改大小放过，与预览一致。
        edited.write_text("# 改了大小\n" * 5, encoding="utf-8")
        self.assertEqual(self.check(record)[0], [])


class GitChangeSetTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name).resolve() / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        for name, text in (("keep.txt", "k\n"), ("edit.txt", "1\n"), ("gone.txt", "g\n"), ("mode.sh", "echo\n"), ("flip.txt", "f\n")):
            (self.repo / name).write_text(text, encoding="utf-8")
        self.commit("基线")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        completed = subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                    "-c", "commit.gpgsign=false", *args], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def test_changes_are_classified(self) -> None:
        (self.repo / "edit.txt").write_text("2\n", encoding="utf-8")
        (self.repo / "gone.txt").unlink()
        (self.repo / "new.txt").write_text("n\n", encoding="utf-8")
        os.chmod(self.repo / "mode.sh", 0o755)
        new = self.commit("改动")
        changes = predict.changeset_from_git(self.repo, self.base, new)
        self.assertEqual((changes.added, changes.deleted, changes.mode_changed), ({"new.txt"}, {"gone.txt"}, {"mode.sh"}))
        self.assertEqual(changes.modified, {"edit.txt", "mode.sh"})
        self.assertTrue(changes.head_changed)
        self.assertEqual(changes.veto, [])
        same = predict.changeset_from_git(self.repo, new, new)
        self.assertFalse(same.head_changed)
        self.assertEqual((same.added, same.deleted, same.modified), (set(), set(), set()))

    def test_type_change_symlink_and_gitmodules_veto(self) -> None:
        (self.repo / "flip.txt").unlink()
        (self.repo / "flip.txt").symlink_to("keep.txt")
        new = self.commit("文件变符号链接")
        changes = predict.changeset_from_git(self.repo, self.base, new)
        self.assertEqual(changes.type_changed, {"flip.txt"})
        self.assertTrue(changes.veto and "类型变化" in changes.veto[0])
        (self.repo / ".gitmodules").write_text("[submodule \"x\"]\n", encoding="utf-8")
        newer = self.commit("子模块配置")
        self.assertTrue(any(".gitmodules" in item for item in predict.changeset_from_git(self.repo, new, newer).veto))
        link = predict.parse_raw_diff(b":000000 120000 " + b"0" * 40 + b" " + b"1" * 40 + b" A\0link\0")
        self.assertTrue(link.veto, "新增符号链接（模式 120000）也否决")

    def test_malformed_diff_output_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            predict.parse_raw_diff(b"not a diff\0path\0")


class CommandLineTests(PredictBase):
    def test_t_p9_the_command_prints_affected_units(self) -> None:
        record = self.publish(run_id="predict-run")
        repo = self.world.root / "repo-for-diff"
        repo.mkdir()
        run = lambda *args: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",  # noqa: E731
                                            "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, check=True).stdout
        run("init", "-q", "-b", "main")
        (repo / "read.txt").write_text("a\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "一")
        old = run("rev-parse", "HEAD").strip()
        (repo / "read.txt").write_text("b\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "二")
        new = run("rev-parse", "HEAD").strip()
        completed = subprocess.run(
            ["python3", "-B", str(Path(predict.__file__)), "affected", "--store", str(self.world.store.root), "--run-id", "predict-run",
             "--repo", str(repo), "--old", old, "--new", new], capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual((payload["status"], payload["units"], payload["changes"]["modified"]), ("ok", 1, 1))
        self.assertIn(self.UNIT, payload["result"], "read.txt 被来源单元读过，被改了：受影响")
        bad = subprocess.run(["python3", "-B", str(Path(predict.__file__)), "affected", "--store", str(self.world.store.root), "--run-id", "no-such",
                              "--repo", str(repo), "--old", old, "--new", new], capture_output=True, text=True,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(bad.returncode, 3)
        self.assertIn("refused", bad.stdout)


if __name__ == "__main__":
    unittest.main()
