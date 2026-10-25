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
from unittest import mock

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


class SymlinkTargetTests(PredictBase):
    """符号链接：来源经链接访问一个路径，真正被读到的是链接解析后的目标——目标被改，预览必须报出（权威会拒绝）。"""

    def test_editing_the_target_of_an_accessed_symlink_is_reported(self) -> None:
        record = self.publish()                                      # alias（符号链接）→ real.txt，来源对 alias 做了 stat
        self.real.write_text("symlink-target-changed-and-longer-size", encoding="utf-8")
        problems, _ = self.check(record)
        self.assertRejected(problems, "大小")
        reasons = self.predicted(record, predict.ChangeSet(modified={self.rel(self.real)}))
        self.assertTrue(any("real.txt" in reason for reason in reasons), reasons)

    def test_editing_a_file_reached_through_a_directory_link_is_reported(self) -> None:
        record = self.publish()                                      # ldir（符号链接）→ realdir，来源 stat 了 ldir/f.txt
        target = self.realdir / "f.txt"
        target.write_text("through-link-changed-and-longer-size", encoding="utf-8")
        self.assertRejected(self.check(record)[0], "大小")
        reasons = self.predicted(record, predict.ChangeSet(modified={self.rel(target)}))
        self.assertTrue(any("realdir/f.txt" in reason for reason in reasons), reasons)

    def test_unrelated_files_and_unaccessed_twins_are_not_reported(self) -> None:
        record = self.publish()
        self.assertEqual(self.predicted(record, predict.ChangeSet(modified={self.rel(self.other)})), [])
        twin = self.realdir2 / "f.txt"
        self.assertEqual(self.predicted(record, predict.ChangeSet(modified={self.rel(twin)})), [], "没被访问的同名文件")


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

    def test_t_p8_nested_directories_under_the_walk_are_exempt_in_the_preview_too(self) -> None:
        record = self.publish()
        nested = str(self.nested_files[0].relative_to(self.tree))
        self.assertEqual(self.predicted(record, predict.ChangeSet(modified={nested})), [])
        self.assertTrue(self.predicted(record, predict.ChangeSet(modified={nested}, mode_changed={nested})))
        self.nested_files[0].write_text('{"i": 0, "bytes": "more"}\n', encoding="utf-8")
        self.assertEqual(self.check(record)[0], [], "权威同样放过")

    def test_t_p8_a_promoted_python_file_and_a_read_file_are_reported(self) -> None:
        """G-P 提升（镜像字节码被读）的 ``.py`` 与既读又 stat 的文件不享受遍历豁免：预览报出，权威也拒绝。"""
        record = self.publish()
        for edited in (self.promoted, self.read_and_stat):
            with self.subTest(file=edited.name):
                relative = str(edited.relative_to(self.tree))
                self.assertTrue(self.predicted(record, predict.ChangeSet(modified={relative})))
                original = edited.read_text(encoding="utf-8")
                edited.write_text(original * 3, encoding="utf-8")
                self.assertTrue(self.check(record)[0], "权威拒绝")
                edited.write_text(original, encoding="utf-8")


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

    def test_type_change_veto_helper_for_the_authority_paths(self) -> None:
        """权威路径（后台验证承接、收尾承接）用的一票否决：文件变目录（名字不变）、变符号链接都返回原因；普通改动、同一提交返回空；读不出变更集抛 ValueError。"""
        (self.repo / "edit.txt").write_text("2\n", encoding="utf-8")
        plain = self.commit("普通改动")
        self.assertEqual(predict.type_change_veto(self.repo, self.base, plain), [])
        self.assertEqual(predict.type_change_veto(self.repo, plain, plain), [])
        (self.repo / "flip.txt").unlink()
        (self.repo / "flip.txt").mkdir()
        (self.repo / "flip.txt" / "inside.txt").write_text("i\n", encoding="utf-8")
        flipped = self.commit("文件变目录")
        reasons = predict.type_change_veto(self.repo, plain, flipped)
        self.assertTrue(reasons and "flip.txt" in reasons[0], reasons)
        self.assertTrue(predict.type_change_veto(self.repo, self.base, flipped), "跨多个提交累计的变更也否决")
        with self.assertRaises(ValueError):
            predict.type_change_veto(self.repo, "0" * 40, flipped)
        # 反方向：目录变成同名文件（git 里同样是一条新增加若干条删除，不是类型变化 T）。
        (self.repo / "flip.txt" / "inside.txt").unlink()
        (self.repo / "flip.txt").rmdir()
        (self.repo / "flip.txt").write_text("f\n", encoding="utf-8")
        back = self.commit("目录变回文件")
        self.assertTrue(any("flip.txt" in item and "目录变成了文件" in item for item in predict.type_change_veto(self.repo, flipped, back)))
        # 新增整个目录或只删除整个目录都不是翻转。
        (self.repo / "brand").mkdir()
        (self.repo / "brand" / "x.txt").write_text("x\n", encoding="utf-8")
        added = self.commit("新增目录")
        self.assertEqual(predict.type_change_veto(self.repo, back, added), [])

    def test_veto_for_runs_fails_closed_on_what_it_cannot_find(self) -> None:
        (self.repo / "flip.txt").unlink()
        (self.repo / "flip.txt").mkdir()
        (self.repo / "flip.txt" / "inside.txt").write_text("i\n", encoding="utf-8")
        flipped = self.commit("文件变目录")
        commits = {"run-clean": flipped, "run-old": self.base, "run-unknown": None, "run-missing-object": "1" * 40}
        with mock.patch.object(predict, "source_run_commit", side_effect=lambda store, run_id: commits[run_id]):
            self.assertEqual(predict.veto_for_runs("/store", ["run-clean"], self.repo, flipped), [], "来源就是候选提交：没有变更")
            veto = predict.veto_for_runs("/store", ["run-old"], self.repo, flipped)
            self.assertTrue(veto and "flip.txt" in veto[0], veto)
            unknown = predict.veto_for_runs("/store", ["run-unknown"], self.repo, flipped)
            self.assertTrue(unknown and "解析不出来" in unknown[0], unknown)
            missing = predict.veto_for_runs("/store", ["run-missing-object"], self.repo, flipped)
            self.assertTrue(missing and "读不出" in missing[0], missing)


class SourceRunCommitTests(PredictBase):
    """来源运行的提交：读它入口门禁摘要里的提交，且摘要登记的清单就是这次运行的那一份；解析不出给 None（失败关闭）。"""

    def publish_with_summary(self, run_id: str, commit: str | None, *, manifest_sha256: str | None = None) -> None:
        run_dir = self.world.root / "bg" / run_id
        (run_dir / "executor").mkdir(parents=True)
        entry = self.world.entry(self.world.records[self.UNIT], "executed")
        manifest = view.ur.build_manifest(
            run_id=run_id, mode="re-execute", inheritance_max_age_hours=168.0, record_store=str(self.world.store.root),
            out_dir=str(run_dir / "executor"), decided_at_utc=view.fixture.stamp(view.fixture.T0), completed_at_utc=view.fixture.stamp(view.fixture.T0),
            policy_sha256=self.world.policy, environment=self.world.environment, environment_sha256=view.ur.entries_sha256(self.world.environment),
            executor=self.world.executor, planned_units=[self.UNIT], units=[entry], diagnostic=[], test_groups={}, counts={})
        self.world.store.put_manifest(manifest)
        if commit is not None:
            summary = {"source": {"commit": commit}, "unit_manifest": {"manifest_sha256": manifest_sha256 or manifest["manifest_sha256"]}}
            (run_dir / "entry-gates.json").write_text(json.dumps(summary), encoding="utf-8")

    def test_the_commit_comes_from_the_entry_gates_summary_of_that_very_manifest(self) -> None:
        good = "a" * 40
        self.publish_with_summary("run-good", good)
        self.publish_with_summary("run-no-summary", None)
        self.publish_with_summary("run-other-manifest", good, manifest_sha256="b" * 64)
        self.publish_with_summary("run-bad-commit", "not-a-commit")
        store = self.world.store.root
        self.assertEqual(predict.source_run_commit(store, "run-good"), good)
        self.assertIsNone(predict.source_run_commit(store, "run-no-summary"))
        self.assertIsNone(predict.source_run_commit(store, "run-other-manifest"), "摘要登记的清单不是这次运行的那一份")
        self.assertIsNone(predict.source_run_commit(store, "run-bad-commit"))
        self.assertIsNone(predict.source_run_commit(store, "run-not-published"))


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
