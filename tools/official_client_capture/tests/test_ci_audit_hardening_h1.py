"""审计器加固批次 H-1：独立验收（步骤 1、2、3、6、7）里一般缺陷 G1、G2、G5～G8 的回归测试。

每一节对应验收报告里的一条缺陷，正反例都有；凡是「修复行为」的用例，在加固之前都是红的。

* G1（跟踪器自检）：v4 输出归属跟踪器多认两种「内核不可能产生的成功」——``O_CREAT|O_EXCL`` 创建成功却落在已知名字上、
  ``RENAME_NOREPLACE`` 成功却目的名字已知——都判矛盾（与现有「硬链接成功却目的已知」同口径）；v3 跟踪器逐字不变。
  O_EXCL 一侧放过「紧挨着创建事件的同路径第二条事件」：解析器对一次 openat 产出两条紧挨着的事件，而既有的随机对拍测试用
  两个相邻事件表示一次调用，必须零改动通过；所以验收报告复现片段 1 里紧挨着的两次创建，在这里改写成隔一个事件的形状。
* G2（畸形来源记录）：来源记录里的 v3 宿主条目畸形（缺名字、名字或路径不是字符串……）时，补比函数
  ``host_inputs_inherit_problems`` 返回问题而不是抛 KeyError／TypeError／AttributeError，所以 ``check_record`` 不再被
  一条畸形记录崩掉。注意 ``unit_records.py`` 本身没有改：control 不变量测试（``test_ci_declared_inputs_records`` 的
  ``ControlInvariantTests``）要求它相对基线提交只新增声明级内容、既有定义逐节点不变，所以「自摘要不符时跳过补比」与
  「check_record 加宽 except」这两处改动被它挡住，全部加固都落在 ``read_audit.py`` 的补比函数里。
* G5（说明与提示）：后台验证模块说明里的选项归属（跨提交后继的两个选项只有 background_validation.py 的 start 支持）、
  通过提示显眼标出「祖先关系未验证」，以及 ``_host_unchanged`` 注释所述的实际行为（拿不到完整轨迹时只比基线↔结束）。
* G6（前提）：目录项只在 getdents64 事件上绑定，strace 也只跟踪 getdents64。
* G7（常量）：``MAX_OUTPUT_EVENTS`` 在解析器与重放端各有一份，必须相等（两个文件独立加载，不互相导入）。
* G8（git 隔离）：祖先验证的 git 子进程不继承全局 git 配置与钩子。

被验模块 ``read_audit.py``／``read_audit_runtime.py``／``unit_records.py`` 同时被 control 四链的 sidecar 动态复用，
所以这里同时有「良构输入的既有行为一字不变」的对照用例。
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import background_validation as bv
from tools.ci import entry_dryrun as dryrun
from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import background_cross_commit_fixture as cross
from tools.official_client_capture.tests import test_ci_background_validation as bvt
from tools.official_client_capture.tests import test_ci_read_audit_host_snapshot_v3 as hs
from tools.official_client_capture.tests import test_ci_read_audit_v4_trace as v4t
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as T
from tools.official_client_capture.tests import unit_inherit_fixture as fixture

CI_DIR = Path(__file__).resolve().parents[3] / "tools" / "ci"

# 跟踪器报出的问题文案里的关键片段（与实现保持一致；断言用子串，路径另外核对）。
EXCL_CONTRADICTION = "O_CREAT|O_EXCL 创建成功却落在已知名字上"
NOREPLACE_CONTRADICTION = "RENAME_NOREPLACE 成功却目标名字已存在"


def exclusive_create(builder: T.Builder, path: str) -> list[dict]:
    """O_CREAT|O_EXCL 成功创建（``T.Builder.create`` 默认就带 O_EXCL，这里写明意图）。"""
    return builder.create(path, extra=("O_EXCL",))


def exclusive_create_as_parsed(builder: T.Builder, path: str) -> list[dict]:
    """解析器对一次 O_CREAT|O_EXCL 的 openat 调用实际产出的形状：请求路径与返回描述符的真实路径各一条事件，共享调用编号。"""
    flags = ("O_WRONLY", "O_CREAT", "O_EXCL")
    first = builder.event("openat", path, flags=flags, target=path)
    second = builder.event("openat", path, flags=flags, target=path, call=first["call"])
    return [first, second]


def noreplace(builder: T.Builder, source: str, destination: str, **kw) -> list[dict]:
    """renameat2 + RENAME_NOREPLACE 的两路径调用（来源端、目的端各一条事件）。"""
    return builder.rename(source, destination, name="renameat2", flags=("RENAME_NOREPLACE",), **kw)


# ---------------------------------------------------------------------------------------------------------------------
# G1：跟踪器的两道矛盾自检
# ---------------------------------------------------------------------------------------------------------------------


class ExclusiveCreateSelfCheckTests(unittest.TestCase):
    """``O_CREAT|O_EXCL`` 创建成功却落在已知名字上：内核只会在路径不存在时让它成功，出现说明名字表与真实状态已脱节。"""

    def setUp(self) -> None:
        self.b = T.Builder()

    def problems_of(self, *groups: list[dict]) -> tuple[runtime.OutputTrackerV4, list[bool], list[str]]:
        tracker, changed = T.run(*groups)
        return tracker, changed, tracker.report()["problems"]

    def test_exclusive_create_succeeding_on_a_known_file_is_a_contradiction(self) -> None:
        """验收报告复现片段 1 的改写：两次 O_EXCL 创建之间隔着别的事件。改动前 problems 为空。
        原片段是紧挨着的两次创建——跟踪器分不清那是同一次调用的两条事件还是两次调用，有意放过，见下面的「紧邻豁免」一例。"""
        b = self.b
        tracker, changed, problems = self.problems_of(b.mkdir(T.D), exclusive_create(b, T.D + "/a"), b.stat(T.D + "/a"),
                                                      exclusive_create(b, T.D + "/a"))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(EXCL_CONTRADICTION, problems[0])
        self.assertTrue(problems[0].endswith(T.D + "/a"), problems[0])
        self.assertEqual(changed, [True, True, False, False], "矛盾的事件不改变名字表，也不推进变更纪元")
        self.assertEqual(tracker.entries[T.D], 1, "名字表保持原样：目录里仍只有一个子项")

    def test_the_same_holds_for_known_directories_hardlinked_names_and_plain_open(self) -> None:
        b = self.b
        cases = {
            "已知名字是目录": [b.mkdir(T.D), b.mkdir(T.D + "/d"), exclusive_create(b, T.D + "/d")],
            "硬链接出来的第二个名字": [b.mkdir(T.D), b.create(T.D + "/a"), b.link(T.D + "/a", T.D + "/h"),
                                   exclusive_create(b, T.D + "/h")],
            "不带 dirfd 的 open": [b.mkdir(T.D), b.create(T.D + "/a"), b.stat(T.D + "/a"),
                                 [b.event("open", T.D + "/a", flags=("O_WRONLY", "O_CREAT", "O_EXCL"), target=T.D + "/a")]],
            "rename 之后的新名字": [b.mkdir(T.D), b.create(T.D + "/a"), b.rename(T.D + "/a", T.D + "/n"),
                                 exclusive_create(b, T.D + "/n")],
        }
        for label, groups in cases.items():
            with self.subTest(label=label):
                _, _, problems = self.problems_of(*groups)
                self.assertTrue(any(EXCL_CONTRADICTION in problem for problem in problems), problems)

    def test_the_second_event_right_after_a_creation_is_tolerated_but_only_once(self) -> None:
        """紧邻豁免：解析器对一次 openat 调用产出两条紧挨着的事件（请求路径＋返回描述符的真实路径，路径相同）。第二条落在第一条
        刚创建的名字上，不是矛盾。真实形状（两条共享调用编号）和既有随机对拍的形状（两条调用编号不同的相邻事件）都放过；
        豁免只有一次——再下一条同路径的 O_EXCL 成功，仍判矛盾。"""
        b = self.b
        _, changed, problems = self.problems_of(b.mkdir(T.D), exclusive_create_as_parsed(b, T.D + "/a"))
        self.assertEqual(problems, [])
        self.assertEqual(changed, [True, True, False], "第二条事件不改变状态：创建只算一次")
        b = T.Builder()
        _, _, problems = self.problems_of(b.mkdir(T.D), b.create(T.D + "/a") + b.create(T.D + "/a"))
        self.assertEqual(problems, [], "既有随机对拍就是这样表示一次调用的（调用编号不同的相邻两条事件）")
        b = T.Builder()
        _, _, problems = self.problems_of(b.mkdir(T.D), exclusive_create_as_parsed(b, T.D + "/a"), exclusive_create(b, T.D + "/a"))
        self.assertEqual(len(problems), 1, "豁免只用一次：第三条紧挨着的同路径 O_EXCL 成功仍是矛盾")
        self.assertIn(EXCL_CONTRADICTION, problems[0])
        b = T.Builder()
        _, changed, problems = self.problems_of(b.mkdir(T.D), exclusive_create_as_parsed(b, T.D + "/a"),
                                                exclusive_create_as_parsed(b, T.D + "/a"))
        self.assertEqual(len(problems), 1, "后一次调用再 O_EXCL 创建同名：矛盾（它的两条事件报的是同一条问题）")
        self.assertEqual(changed, [True, True, False, False, False])

    def test_the_tolerance_is_for_a_file_just_created_not_for_other_known_names(self) -> None:
        """豁免只给「上一条事件刚创建的这个文件名」：刚创建的是目录、是硬链接的新名字，或者路径不同，都不适用。"""
        b = self.b
        for label, groups in {
            "刚创建的是目录": [b.mkdir(T.D), b.mkdir(T.D + "/d"), exclusive_create(b, T.D + "/d")],
            "刚做的是硬链接": [b.mkdir(T.D), b.create(T.D + "/a"), b.stat(T.D + "/a"), b.link(T.D + "/a", T.D + "/h"),
                           exclusive_create(b, T.D + "/h")],
            "路径不同（上一条创建的是 b，现在是已知的 a）": [b.mkdir(T.D), b.create(T.D + "/a"), b.create(T.D + "/b"),
                                                  exclusive_create(b, T.D + "/a")],
        }.items():
            with self.subTest(label=label):
                _, _, problems = self.problems_of(*groups)
                self.assertTrue(any(EXCL_CONTRADICTION in problem for problem in problems), problems)

    def test_legitimate_creations_and_reopens_stay_clean(self) -> None:
        """反向对照：不是「O_EXCL 成功落在已知名字」的情形一个问题都不能报。"""
        b = self.b
        cases = {
            "O_EXCL 创建未知名字": [b.mkdir(T.D), exclusive_create(b, T.D + "/a")],
            # 下面两例在创建与重新打开之间隔了一条别的事件，免得被「紧邻豁免」遮住：要检验的是 O_EXCL 自检本身不误伤。
            "O_CREAT 不带 O_EXCL 重新打开已知文件": [b.mkdir(T.D), b.create(T.D + "/a"), b.stat(T.D + "/a"), b.create(T.D + "/a", extra=())],
            "creat 重新打开已知文件": [b.mkdir(T.D), b.create(T.D + "/a"), b.stat(T.D + "/a"),
                                     [b.event("creat", T.D + "/a", flags=(), target=T.D + "/a")]],
            "O_EXCL 被拒绝（EEXIST）落在已知名字": [b.mkdir(T.D), b.create(T.D + "/a"),
                                              b.create(T.D + "/a", ok=False, errno="EEXIST")],
            "删除之后再 O_EXCL 创建同名": [b.mkdir(T.D), b.create(T.D + "/a"), b.unlink(T.D + "/a"), exclusive_create(b, T.D + "/a")],
            "rename 走之后再 O_EXCL 创建旧名": [b.mkdir(T.D), b.create(T.D + "/a"), b.rename(T.D + "/a", T.D + "/n"),
                                          exclusive_create(b, T.D + "/a")],
        }
        for label, groups in cases.items():
            with self.subTest(label=label):
                _, _, problems = self.problems_of(*groups)
                self.assertEqual(problems, [])


class NoReplaceSelfCheckTests(unittest.TestCase):
    """``RENAME_NOREPLACE`` 成功却目的名字已知：内核在目的存在时一律返回 EEXIST（含同一对象的两个名字、源与目的同名）。"""

    def setUp(self) -> None:
        self.b = T.Builder()

    def test_noreplace_succeeding_onto_a_known_name_is_a_contradiction(self) -> None:
        """验收报告的复现片段 2：改动前 problems 为空。"""
        b = self.b
        tracker, changed = T.run(b.mkdir(T.D), b.create(T.D + "/a"), b.create(T.D + "/b"), noreplace(b, T.D + "/a", T.D + "/b"))
        problems = tracker.report()["problems"]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(NOREPLACE_CONTRADICTION, problems[0])
        self.assertTrue(problems[0].endswith(T.D + "/b"), problems[0])
        self.assertEqual(changed, [True, True, True, False, False], "矛盾的事件不改变名字表（来源端、目的端都返回 False）")
        self.assertIn(T.D + "/a", tracker.names, "矛盾之后来源名字仍在")
        self.assertNotEqual(tracker.names[T.D + "/a"], tracker.names[T.D + "/b"], "两个名字仍是两个对象，没有被合并")

    def test_it_covers_directories_the_same_object_and_the_same_name(self) -> None:
        b = self.b
        cases = {
            "目的是已知的空目录": [b.mkdir(T.D), b.mkdir(T.D + "/a"), b.mkdir(T.D + "/b"), noreplace(b, T.D + "/a", T.D + "/b")],
            "同一对象的两个名字": [b.mkdir(T.D), b.create(T.D + "/a"), b.link(T.D + "/a", T.D + "/b"), noreplace(b, T.D + "/a", T.D + "/b")],
            "源与目的同名": [b.mkdir(T.D), b.create(T.D + "/a"), noreplace(b, T.D + "/a", T.D + "/a")],
        }
        for label, groups in cases.items():
            with self.subTest(label=label):
                tracker, _ = T.run(*groups)
                self.assertTrue(any(NOREPLACE_CONTRADICTION in problem for problem in tracker.report()["problems"]),
                                tracker.report()["problems"])

    def test_plain_rename_over_known_names_is_still_legitimate(self) -> None:
        """对照：不带 RENAME_NOREPLACE 的 rename 覆盖已知名字、同一对象两个名字之间的空操作，与过去完全相同。"""
        b = self.b
        tracker, changed = T.run(b.mkdir(T.D), b.create(T.D + "/a"), b.create(T.D + "/b"), b.rename(T.D + "/a", T.D + "/b"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed[-2:], [True, False], "覆盖已知文件：判定挂在来源端事件上，改变一次状态；目的端事件不改状态")
        self.assertNotIn(T.D + "/a", tracker.names)
        flagless = T.Builder()
        tracker, _ = T.run(flagless.mkdir(T.D), flagless.create(T.D + "/a"), flagless.create(T.D + "/b"),
                           flagless.rename(T.D + "/a", T.D + "/b", name="renameat2"))
        self.assertEqual(tracker.report()["problems"], [], "renameat2 不带 NOREPLACE 就是普通 rename")
        same_object = T.Builder()
        tracker, changed = T.run(same_object.mkdir(T.D), same_object.create(T.D + "/a"), same_object.link(T.D + "/a", T.D + "/b"),
                                 same_object.rename(T.D + "/a", T.D + "/b"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed[-2:], [False, False], "同一对象的两个名字之间：POSIX 成功但什么也不改")

    def test_noreplace_onto_an_unknown_name_and_failed_noreplace_stay_clean(self) -> None:
        b = self.b
        tracker, changed = T.run(b.mkdir(T.D), b.create(T.D + "/a"), noreplace(b, T.D + "/a", T.D + "/b"), b.read(T.D + "/b"))
        self.assertEqual(tracker.report()["problems"], [])
        self.assertEqual(changed, [True, True, True, False, False], "目的未知时照旧是一次普通移动")
        self.assertNotIn(T.D + "/a", tracker.names)
        self.assertIn(T.D + "/b", tracker.names)
        failed = T.Builder()
        tracker, _ = T.run(failed.mkdir(T.D), failed.create(T.D + "/a"), failed.create(T.D + "/b"),
                           noreplace(failed, T.D + "/a", T.D + "/b", ok=False, errno="EEXIST"))
        self.assertEqual(tracker.report()["problems"], [], "NOREPLACE 因目的存在而失败（EEXIST）是内核的正常结果")

    def test_earlier_checks_keep_their_own_messages(self) -> None:
        """来源未证明仍按原文案报，新自检只在目的名字已知且前面的检查都过了时介入。"""
        b = self.b
        tracker, _ = T.run(b.mkdir(T.D), b.create(T.D + "/b"), noreplace(b, T.D + "/ghost", T.D + "/b"))
        problems = tracker.report()["problems"]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("重命名的来源未证明由本轮生成", problems[0])


class ContradictionsAreRefusedAtReplayTests(unittest.TestCase):
    """矛盾事件写进轨迹之后，重放端（承接前的复核）用同一个跟踪器照样拒绝。"""

    @staticmethod
    def replay_document(*groups: list[dict]) -> dict:
        events = [dict(event) for group in groups for event in group]
        tracker = runtime.OutputTrackerV4(T.CONTRACT)
        epoch = 0
        for event in events:
            event["epoch"] = epoch
            if tracker.observe_event(event):
                epoch += 1
        return {"output_events": events, "outputs": tracker.report()}

    def test_contradictory_events_fail_the_replay_and_clean_ones_pass(self) -> None:
        b = T.Builder()
        clean = self.replay_document(b.mkdir(T.D), b.create(T.D + "/a"), noreplace(b, T.D + "/a", T.D + "/b"))
        tracker, _ = runtime._replay_events_v4(clean, T.CONTRACT)
        self.assertEqual(tracker.problems, set())
        b = T.Builder()
        double = self.replay_document(b.mkdir(T.D), exclusive_create(b, T.D + "/a"), b.stat(T.D + "/a"), exclusive_create(b, T.D + "/a"))
        b = T.Builder()
        onto = self.replay_document(b.mkdir(T.D), b.create(T.D + "/a"), b.create(T.D + "/b"), noreplace(b, T.D + "/a", T.D + "/b"))
        for label, document in (("O_EXCL 两次创建同名", double), ("NOREPLACE 覆盖已知名字", onto)):
            with self.subTest(label=label):
                with self.assertRaisesRegex(runtime.RuntimeContractError, "来源不明"):
                    runtime._replay_events_v4(document, T.CONTRACT)


class SelfChecksOnRealParserOutputTests(unittest.TestCase):
    """自检在真实解析器（strace 行 → ``filter_stream``）产出的事件上同样成立，真正的矛盾端到端被重放拒绝。"""

    def setUp(self) -> None:
        self.f = v4t.AuditFixture("assertAccepted")      # 基类没有 test_ 方法，借一个已有的方法名当实例名
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def noreplace_line(self, source: str, destination: str) -> str:
        root = self.f.root
        return f'1 renameat2(AT_FDCWD<{root}/d>, "{source}", AT_FDCWD<{root}/d>, "{destination}", RENAME_NOREPLACE) = 0'

    def test_one_openat_call_yields_two_events_and_is_not_a_contradiction(self) -> None:
        f = self.f
        document = f.document([f.mkdir_line("d"), f.create_line("d/a")])
        creating = [event for event in document["runtime"]["output_events"] if event["name"] == "openat"]
        self.assertEqual(len(creating), 2, "一次 openat 调用产出两条事件（请求路径＋返回描述符的真实路径）")
        self.assertEqual((creating[0]["call"], creating[0]["path"]), (creating[1]["call"], creating[1]["path"]))
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        f.assertAccepted(document)

    def test_a_second_exclusive_create_of_the_same_name_is_refused_end_to_end(self) -> None:
        f = self.f
        document = f.document([f.mkdir_line("d"), f.create_line("d/a"), f.create_line("d/a")])
        self.assertTrue(any(EXCL_CONTRADICTION in problem for problem in document["runtime"]["outputs"]["problems"]),
                        document["runtime"]["outputs"]["problems"])
        f.assertRejected(document)

    def test_noreplace_over_a_known_name_is_refused_end_to_end_and_over_an_unknown_one_accepted(self) -> None:
        f = self.f
        f.assertAccepted(f.document([f.mkdir_line("d"), f.create_line("d/a"), self.noreplace_line("a", "b")]))
        refused = f.document([f.mkdir_line("d"), f.create_line("d/a"), f.create_line("d/b"), self.noreplace_line("a", "b")])
        self.assertTrue(any(NOREPLACE_CONTRADICTION in problem for problem in refused["runtime"]["outputs"]["problems"]),
                        refused["runtime"]["outputs"]["problems"])
        f.assertRejected(refused)


class V3TrackerIsUntouchedTests(unittest.TestCase):
    """v3 跟踪器（历史凭证与 control 四链重放）逐字不变：新增的两道自检只作用于 v4。"""

    def test_v3_tracker_keeps_accepting_the_second_exclusive_create_and_still_refuses_every_rename(self) -> None:
        tracker = runtime.OutputTracker(T.CONTRACT)
        tracker.observe(T.D, "write", "mkdir", "", True)
        for _ in range(2):
            tracker.observe(T.D + "/a", "write", "openat", "O_WRONLY|O_CREAT|O_EXCL", True, T.D + "/a")
        self.assertEqual(tracker.report()["problems"], [], "v3 对第二次 O_EXCL 创建的判定与改动前相同（不报）")
        tracker.observe(T.D + "/a", "write", "renameat2", "", True)
        tracker.observe(T.D + "/b", "write", "renameat2", "", True)
        self.assertEqual(tracker.report()["problems"],
                         sorted(["链接或移动操作尚无输出归属证明：" + T.D + "/a", "链接或移动操作尚无输出归属证明：" + T.D + "/b"]),
                         "v3 对 rename 一律拒绝，文案不变")


# ---------------------------------------------------------------------------------------------------------------------
# G2：畸形来源记录
# ---------------------------------------------------------------------------------------------------------------------

# 让 v3 宿主条目「格式畸形」的变体：补比函数必须报出明确的问题。
BROKEN_V3_ENTRY = ("缺 name", "name 是整数", "name 是列表", "detail.path 缺失", "detail.path 是整数")
# 条目整体不再像 v3 条目，或只是混进来的不相干元素：补比函数照旧忽略它们，只要求不抛异常。
NOT_A_V3_ENTRY = ("detail 缺失", "条目是字符串", "inputs 里混入 None")


def broken_entry(entry: dict, how: str) -> dict:
    """深拷贝一个良构的 v3 宿主条目，弄坏成指定的畸形（BROKEN_V3_ENTRY 里的变体）。"""
    forged = copy.deepcopy(entry)
    if how == "缺 name":
        del forged["name"]
    elif how == "name 是整数":
        forged["name"] = 123
    elif how == "name 是列表":
        forged["name"] = ["host:/x"]
    elif how == "detail.path 缺失":
        del forged["detail"]["path"]
    elif how == "detail.path 是整数":
        forged["detail"]["path"] = 7
    else:
        raise AssertionError(how)
    return forged


def is_host_entry(entry: object) -> bool:
    return isinstance(entry, dict) and str(entry.get("name", "")).startswith("host:")


def damaged(record: dict, how: str) -> dict:
    """深拷贝来源记录，把其中的宿主条目弄坏成指定的畸形（BROKEN_V3_ENTRY 与 NOT_A_V3_ENTRY 里的全部变体）。"""
    forged = copy.deepcopy(record)
    inputs = forged["inputs"]
    index = next(i for i, entry in enumerate(inputs) if is_host_entry(entry))
    if how in BROKEN_V3_ENTRY:
        inputs[index] = broken_entry(inputs[index], how)
    elif how == "detail 缺失":
        del inputs[index]["detail"]
    elif how == "条目是字符串":
        inputs[index] = "host:/x"
    elif how == "inputs 里混入 None":
        inputs.insert(0, None)
    else:
        raise AssertionError(how)
    return forged


class MalformedSourceRecordTests(unittest.TestCase):
    """用 ``HostSnapshotRecordTests`` 的合成世界（一次严格读集审计过的来源记录）造畸形来源记录。"""

    def setUp(self) -> None:
        self.case = hs.HostSnapshotRecordTests("test_noise_does_not_block_inheritance_but_an_observed_content_change_does")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.record = self.case.publish()
        self.store = self.case.world.store
        self.trace = json.loads(self.store.log_path(self.case.world.trace_sha256).read_text(encoding="utf-8"))

    def current(self) -> ur.Current:
        """当前输入：策略声明（生产路径）+ 现取的 v3 宿主快照 + 要求读集审计，与验收报告的复现片段同做法。"""
        case = self.case
        current = case.world.current(case.UNIT)
        current.inputs = ur._unique([*case.world.base_inputs(case.UNIT), ra.host_snapshot_v3(str(case.host)),
                                     ur.value_entry("policy", "require-read-audit", "all-file-paths/v1")])
        current.inputs_sha256 = ur.entries_sha256(current.inputs)
        return current

    def check(self, record: dict, *, resealed: bool) -> list[str]:
        """对畸形记录跑 check_record；``resealed`` 为真时重算记录自摘要并入库（模拟能写记录库的人）。"""
        if resealed:
            sealed = ur.seal_record({key: value for key, value in record.items() if key != "record_sha256"})
            path = self.store.put_record(sealed)
        else:
            sealed, path = record, self.store.record_path(self.case.UNIT, self.record["record_sha256"])
        return ur.check_record(self.store, path, sealed, self.current(), self.case.world.facts(fixture.T0))

    # ------------------------------------------------------------------ check_record

    def test_missing_name_from_the_acceptance_report_is_reported_not_raised(self) -> None:
        """验收报告的复现：缺 name 且没有重算自摘要。改动前抛 KeyError: 'name'；记录自摘要不符的问题照旧产生。"""
        problems = self.check(damaged(self.record, "缺 name"), resealed=False)
        self.assertTrue(any("自摘要不符" in problem for problem in problems), problems)

    def test_every_variant_returns_problems_instead_of_raising(self) -> None:
        """每种畸形变体，不论记录自摘要有没有重算：check_record 都返回问题列表而不抛异常；v3 宿主条目本身畸形的变体还必须
        报出「格式畸形」；没重算自摘要的，「记录自摘要不符」的问题照旧产生。"""
        for how in (*BROKEN_V3_ENTRY, *NOT_A_V3_ENTRY):
            forged = damaged(self.record, how)
            for resealed in (False, True):
                with self.subTest(variant=how, resealed=resealed):
                    problems = self.check(forged, resealed=resealed)
                    self.assertIsInstance(problems, list)
                    self.assertTrue(problems, "畸形记录不能没有任何问题")
                    if not resealed:
                        self.assertTrue(any("自摘要不符" in problem for problem in problems), problems)
                    if how in BROKEN_V3_ENTRY:
                        self.assertTrue(any("格式畸形" in problem for problem in problems), problems)

    def test_well_formed_records_behave_exactly_as_before(self) -> None:
        """良构记录：补比照常运行——同长度的内容变化仍被发现，宿主噪声仍不阻断（既有行为，防止补比被误改成空操作）。"""
        self.assertEqual(self.case.check(self.record)[0], [])
        (self.case.host.parent / "noise.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.case.check(self.record)[0], [], "目录噪声不阻断")
        self.case.same_size_change()
        problems, _ = self.case.check(self.record)
        self.assertTrue(any("文件内容" in problem for problem in problems), problems)

    # ------------------------------------------------------------------ 补比函数本身

    def test_helper_reports_malformed_v3_entries_on_either_side(self) -> None:
        audit_block = self.record["read_audit"]
        good = next(entry for entry in self.record["inputs"] if is_host_entry(entry))
        others = [entry for entry in self.record["inputs"] if entry is not good]
        for how in BROKEN_V3_ENTRY:
            broken = broken_entry(good, how)
            with self.subTest(variant=how, side="来源记录"):
                problems = ra.host_inputs_inherit_problems(self.trace, [*others, broken], [*others, good], audit_block)
                self.assertTrue(any("格式畸形" in problem and "来源记录" in problem for problem in problems), problems)
            with self.subTest(variant=how, side="当前输入"):
                problems = ra.host_inputs_inherit_problems(self.trace, [*others, good], [*others, broken], audit_block)
                self.assertTrue(any("格式畸形" in problem and "当前输入" in problem for problem in problems), problems)

    def test_helper_ignores_elements_that_are_not_v3_entries(self) -> None:
        """不是 v3 条目的元素（None、字符串、没有 detail 的字典）不属于补比对象：与它们不存在时的结果相同。"""
        inputs, audit_block = self.record["inputs"], self.record["read_audit"]
        current_inputs = self.current().inputs
        baseline = ra.host_inputs_inherit_problems(self.trace, inputs, current_inputs, audit_block)
        self.assertEqual(baseline, [])
        noise = [None, "host:/x", 7, {"name": "host:/y"}, {"name": "host:/z", "detail": None}, {"detail": {"schema_version": "x"}}]
        self.assertEqual(ra.host_inputs_inherit_problems(self.trace, [*noise, *inputs], current_inputs, audit_block), baseline)
        self.assertEqual(ra.host_inputs_inherit_problems(self.trace, inputs, [*noise, *current_inputs], audit_block), baseline)

    def test_helper_turns_a_malformed_input_container_or_trace_into_a_problem(self) -> None:
        inputs, audit_block = self.record["inputs"], self.record["read_audit"]
        current_inputs = self.current().inputs
        problems = ra.host_inputs_inherit_problems(self.trace, 7, current_inputs, audit_block)
        self.assertTrue(any("来源记录" in problem for problem in problems), problems)
        broken_traces = {"轨迹不是对象": [], "访问记录不是列表": {**self.trace, "accesses": 5},
                         "覆盖信息不是对象": {**self.trace, "coverage": ["x"]}}
        for label, trace in broken_traces.items():
            with self.subTest(label=label):
                problems = ra.host_inputs_inherit_problems(trace, inputs, current_inputs, audit_block)
                self.assertTrue(any("无法确定访问方式" in problem for problem in problems), problems)

    def test_helper_is_unchanged_for_pure_v2_records(self) -> None:
        """没有 v3 条目（v2 记录）：补比函数不介入，对任何轨迹文档都返回空（既有行为）。"""
        v2 = [ra.host_snapshot(str(self.case.host))]
        for trace in ({}, self.trace, [], None):
            self.assertEqual(ra.host_inputs_inherit_problems(trace, v2, v2, {}), [])
        self.assertEqual(ra.host_inputs_inherit_problems({}, None, None, None), [])


# ---------------------------------------------------------------------------------------------------------------------
# G5：说明与提示
# ---------------------------------------------------------------------------------------------------------------------


class CrossCommitOptionOwnershipTests(unittest.TestCase):
    """后台验证模块说明里的选项归属：跨提交后继的两个选项只有 background_validation.py 的 start（经 background-validate.sh
    透传）支持；入口空跑 entry_dryrun.py 不支持。说明的事实依据在这里钉住，将来任何一边变了测试会提醒改说明。"""

    START = ["start", "--runroot", "/r", "--data-root", "/d", "--branch", "b", "--commit", "c"]

    def test_background_validation_start_accepts_both_options(self) -> None:
        args = bv._parse([*self.START, "--bundle", "/b", "--unit-inherit-request", "/q", "--unit-inherit-source-result", "/s.json",
                          "--allow-unverified-ancestry"])
        self.assertEqual((str(args.unit_inherit_source_result), args.allow_unverified_ancestry), ("/s.json", True))

    def test_entry_dryrun_start_rejects_both_options(self) -> None:
        base = ["start", "--runroot", "/r", "--data-root", "/d", "--vc-env", "/e", "--bundle", "/b", "--branch", "b", "--commit", "c"]
        dryrun._parse(base)           # 不带这两个选项的调用本身是合法的
        for extra in (["--unit-inherit-source-result", "/s.json"], ["--allow-unverified-ancestry"]):
            with self.subTest(extra=extra[0]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                    dryrun._parse([*base, *extra])
                self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_the_start_synopsis_lists_the_cross_commit_options(self) -> None:
        """模块说明里 start 的概要（从「* ``start 」那行起，到以「``：」结尾的那行止）列全这几个选项。"""
        lines = bv.__doc__.splitlines()
        first = next(index for index, line in enumerate(lines) if line.startswith("* ``start "))
        last = next(index for index in range(first, len(lines)) if lines[index].rstrip().endswith("``："))
        synopsis = " ".join(lines[first:last + 1])
        for option in ("--audit-policy", "--unit-inherit-request", "--unit-inherit-source-result", "--allow-unverified-ancestry"):
            self.assertIn(option, synopsis)

    def test_the_documentation_says_which_script_owns_the_cross_commit_options(self) -> None:
        """说明里明确写出：这两个选项只有本脚本的 start 支持（经 background-validate.sh 透传），空跑脚本不支持。"""
        doc = bv.__doc__
        self.assertIn("这两个选项只有本脚本的 ``start`` 支持", doc)
        self.assertIn("background-validate.sh start", doc)
        self.assertIn("entry_dryrun.py", doc)
        self.assertRegex(doc, r"没有这两个选项")

    def test_background_validate_sh_passes_extra_arguments_through(self) -> None:
        """透传的依据：驱动入口脚本把 start 的其余参数原样交给 background_validation.py start。"""
        script = (CI_DIR.parent / "arm64_capture_driver" / "driver" / "background-validate.sh").read_text(encoding="utf-8")
        self.assertIn('exec "${BV[@]}" start', script)
        self.assertIn('${rest[@]+"${rest[@]}"}', script)


class HostUnchangedFactTests(unittest.TestCase):
    """``_host_unchanged`` 注释所述的实际行为：拿不到完整轨迹时只比较「基线↔结束」，不比较「登记↔基线」。

    为什么仍然正确（注释里的论证）：登记↔基线的无条件比较集在子命名空间启动前已由 ``_host_baseline`` 比过；轨迹不完整的单元
    本身判失败，结束凭证也不写访问类型、重放拒绝，所以这条路径的「未变」结论永远不会被拿去承接。"""

    def setUp(self) -> None:
        self.case = hs.HostSnapshotRuntimeTests("test_end_of_run_check_follows_the_access_kinds_from_the_trace")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_without_a_complete_trace_only_start_against_end_is_compared(self) -> None:
        case = self.case
        planned = case.entries()
        case.file.write_text("ABCDEFGH", encoding="utf-8")        # 计划期之后、启动之前内容就变了；大小相同，启动的核心集比较看不出
        incomplete = ra.filter_stream(case.access_lines(), ["/"], strict=True)
        incomplete["coverage"]["unresolved"] = 2
        for document in (None, incomplete):
            with self.subTest(document="残缺轨迹" if document else "无轨迹"):
                self.assertEqual(case.end_check(planned, document, lambda: None), (True, ""),
                                 "没有完整轨迹：不比登记↔基线，基线↔结束没变就是未变")
        complete = ra.filter_stream(case.access_lines(), ["/"], strict=True)
        ok, reason = case.end_check(planned, complete, lambda: None)
        self.assertFalse(ok, "对照：轨迹完整时同样的变化会被登记↔基线的按访问方式比较发现（读过内容）")
        self.assertIn("文件内容", reason)

    def test_a_trace_that_cannot_be_complete_never_yields_a_replayable_receipt(self) -> None:
        """论证的后半：残缺轨迹下结束凭证不写访问类型，v3 条目的重放因此拒绝。"""
        case = self.case
        entries = case.entries()
        incomplete = case.document([*case.prelude(), *case.access_lines()], "v4")
        incomplete["coverage"]["unresolved"] = 1
        fields = runtime._host_completion(case.audit_module, case.request(entries), incomplete)
        self.assertNotIn("host_path_modes", fields)
        incomplete["runtime"]["completion"].update(fields)
        with self.assertRaisesRegex(runtime.RuntimeContractError, "访问类型"):
            runtime.replay_runtime(incomplete, [case.entry, *entries])


class PassedMessageShowsAncestryTests(bvt.CrossCommitCase):
    """``check()`` 的通过提示显眼写出祖先验证状态：用 ``--allow-unverified-ancestry`` 放行的后继不能看起来与已验证的无异。"""

    PASSED = re.compile(r"后台验证通过（.*）；跨提交后继：承接 \d+ 个、重跑 \d+ 个")

    def finished(self, name: str, *extra: str, real_ancestry: bool = False, drop_test_tree: bool = False):
        scene = self.prepared(real_git=True, name=name)
        if drop_test_tree:
            shutil.rmtree(scene.work / "test-tree")          # 没有完整历史：增量 bundle 的前置提交无处可寻 → 无法验证
        request = scene.request()
        path = scene.start_successor(request, *extra, real_ancestry=real_ancestry)
        payload = scene.finish_successor(path, request)
        self.assertEqual(payload["status"], "passed", payload)
        return scene, payload

    def test_unverified_ancestry_is_loud_in_the_message_of_both_checks(self) -> None:
        scene, payload = self.finished("unverified", "--allow-unverified-ancestry", real_ancestry=True, drop_test_tree=True)
        ancestry = payload["unit_inherit_source"]["ancestry"]
        self.assertEqual(ancestry["status"], "unverified")
        for require in (False, True):
            with self.subTest(require_passed=require):
                code, message = bv.check(scene.runroot, scene.data, require_passed=require)
                self.assertEqual(code, 0, message)
                self.assertIn("祖先关系未验证", message)
                self.assertIn("--allow-unverified-ancestry", message)
                self.assertIn(scene.c7[:12], message, "写出是哪个旧提交没有验证")
                self.assertIn(ancestry["reason"][:30], message, "写出为什么没验证成")
                self.assertIn("跨提交后继", message, "原有的通过信息仍在")

    def test_verified_ancestry_leaves_the_message_exactly_as_before(self) -> None:
        for name, real in (("verified-stub", False), ("verified-real", True)):
            with self.subTest(real_ancestry=real):
                scene, payload = self.finished(name, real_ancestry=real)
                self.assertEqual(payload["unit_inherit_source"]["ancestry"]["status"], "verified")
                for require in (False, True):
                    code, message = bv.check(scene.runroot, scene.data, require_passed=require)
                    self.assertEqual(code, 0, message)
                    self.assertIsNotNone(self.PASSED.fullmatch(message), message)
                    self.assertNotIn("警告", message)
                    self.assertNotIn("未验证", message)

    def test_an_unrecognizable_ancestry_record_is_loud_too(self) -> None:
        """结论里的祖先登记缺失或状态不认识：同样显眼提示，而不是悄悄当作已验证。"""
        scene, payload = self.finished("unrecognizable", real_ancestry=False)
        path = Path(scene.last_started["result"])
        for label, ancestry in (("缺失", None), ("状态不认识", {"status": "maybe"})):
            forged = copy.deepcopy(payload)
            forged["unit_inherit_source"]["ancestry"] = ancestry
            bv._write(path, forged)
            with self.subTest(label=label):
                code, message = bv.check(scene.runroot, scene.data, require_passed=False)
                # 登记被改后，批次边界的重放将来可能直接拒绝（退出码 3），那也是合理的；但只要放行（退出码 0），提示里就必须带警示，
                # 不能是与已验证后继一模一样的通过提示。
                self.assertIn(code, (0, 3), message)
                if code == 0:
                    self.assertIn("警告", message)
                    self.assertIn("祖先验证状态", message)
                    self.assertIsNone(self.PASSED.fullmatch(message), message)
                else:
                    self.assertIn("不能重放", message)


# ---------------------------------------------------------------------------------------------------------------------
# G6：目录枚举的前提
# ---------------------------------------------------------------------------------------------------------------------


class DirectoryEnumerationPremiseTests(unittest.TestCase):
    """目录项只在出现 getdents64 枚举事件时绑定；ARM64 没有 getdents（非 64）系统调用，当前环境不受影响。
    注释里写明的前提在这里钉住：strace 只跟踪 getdents64，解析器只把 getdents64 当枚举。"""

    def test_strict_strace_options_trace_getdents64_but_not_getdents(self) -> None:
        argv = ra.strace_argv(["true"], output=Path("/x/out.json"), roots=["/"], strict=True, runtime_context=Path("/x/ctx.json"))
        traced = [set(item[len("trace="):].split(",")) for item in argv if item.startswith("trace=")]
        enumerating = [names for names in traced if "getdents64" in names]
        self.assertEqual(len(enumerating), 1, traced)
        self.assertNotIn("getdents", enumerating[0])

    def test_only_getdents64_events_bind_directory_entries(self) -> None:
        listed = ra.filter_stream(["1 getdents64(3</tmp/x>, 0x7ffe /* 1 entries */, 32768) = 40"], ["/"], strict=True)
        self.assertEqual(listed["accesses"], [["/tmp/x", ["list"]]])
        self.assertEqual(listed["coverage"]["unresolved"], 0)
        # 假如 strace 输出了 getdents（非 64）：严格模式按「不支持的调用」失败关闭，而不是悄悄当成枚举。
        legacy = ra.filter_stream(["1 getdents(3</tmp/x>, 0x7ffe /* 1 entries */, 32768) = 40"], ["/"], strict=True)
        self.assertEqual(legacy["accesses"], [])
        self.assertEqual(legacy["coverage"]["unresolved_reasons"], {"unsupported-call:getdents": 1})


# ---------------------------------------------------------------------------------------------------------------------
# G7：两份 MAX_OUTPUT_EVENTS
# ---------------------------------------------------------------------------------------------------------------------


def load_standalone(filename: str, alias: str):
    """按路径独立加载 tools/ci 下的一个文件（不进 sys.modules、不经过 import 系统，和执行器按路径加载的方式一致）。"""
    spec = importlib.util.spec_from_file_location(alias, CI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OutputEventLimitAgreementTests(unittest.TestCase):
    """解析器（read_audit.py，写入时按上限截断并登记 output-event-limit）和重放端（read_audit_runtime.py，超限拒绝）各有一份上限。
    只改一边会让两端对「多少条算超限」意见不一：解析器多于重放上限→重放拒绝（失败关闭），反之重放放过解析器已判失败的轨迹。
    两个文件互不导入（重放端要能在新旧文件混用时验 v3 凭证），所以用等值断言守住。"""

    def test_parser_and_replay_limits_are_equal(self) -> None:
        parser = load_standalone("read_audit.py", "h1_parser_standalone")
        replay = load_standalone("read_audit_runtime.py", "h1_replay_standalone")
        self.assertIs(type(parser.MAX_OUTPUT_EVENTS), int)
        self.assertIs(type(replay.MAX_OUTPUT_EVENTS), int)
        self.assertGreater(parser.MAX_OUTPUT_EVENTS, 0)
        self.assertEqual(parser.MAX_OUTPUT_EVENTS, replay.MAX_OUTPUT_EVENTS)

    def test_parser_and_replay_v4_limits_are_equal(self) -> None:
        """v4 合同的上限（MAX_OUTPUT_EVENTS_V4）同样两端各一份，只改一边会让两端对“多少条算超限”意见不一：
        解析器侧大于重放侧→重放拒绝（失败关闭），反之重放放过解析器已按更小上限判失败的轨迹。验收发现注释声称这里守着、实际只比了 v3，现补上。"""
        parser = load_standalone("read_audit.py", "h1_parser_standalone_v4")
        replay = load_standalone("read_audit_runtime.py", "h1_replay_standalone_v4")
        self.assertIs(type(parser.MAX_OUTPUT_EVENTS_V4), int)
        self.assertIs(type(replay.MAX_OUTPUT_EVENTS_V4), int)
        self.assertGreater(parser.MAX_OUTPUT_EVENTS_V4, parser.MAX_OUTPUT_EVENTS, "v4 上限只会比 v3 大")
        self.assertEqual(parser.MAX_OUTPUT_EVENTS_V4, replay.MAX_OUTPUT_EVENTS_V4)
        self.assertEqual((parser.MAX_OUTPUT_EVENTS, replay.MAX_OUTPUT_EVENTS), (200000, 200000), "v3 上限不因 v4 而变")

    def test_the_two_modules_stay_independent(self) -> None:
        """等值靠测试守住，而不是改成互相导入：重放端的源码里不引用解析器的常量，解析器也不引用重放端。"""
        parser_source = (CI_DIR / "read_audit.py").read_text(encoding="utf-8")
        replay_source = (CI_DIR / "read_audit_runtime.py").read_text(encoding="utf-8")
        self.assertRegex(parser_source, r"(?m)^MAX_OUTPUT_EVENTS = \d+$")
        self.assertRegex(replay_source, r"(?m)^MAX_OUTPUT_EVENTS = \d+$")
        self.assertRegex(parser_source, r"(?m)^MAX_OUTPUT_EVENTS_V4 = \d+$")
        self.assertRegex(replay_source, r"(?m)^MAX_OUTPUT_EVENTS_V4 = \d+$")
        self.assertNotIn("from read_audit import", replay_source)
        self.assertNotIn("from read_audit_runtime import", parser_source)


# ---------------------------------------------------------------------------------------------------------------------
# G8：祖先验证的 git 子进程不继承全局 git 配置与钩子
# ---------------------------------------------------------------------------------------------------------------------

HOOK_NAMES = ("reference-transaction", "post-checkout", "post-merge", "post-commit", "post-update", "pre-auto-gc", "update")


def hostile_home(root: Path, *, hooks_path: bool, template_dir: bool) -> tuple[Path, Path]:
    """造一个带恶意 ``.gitconfig`` 的 HOME：``core.hooksPath`` 和／或 ``init.templateDir`` 指向会写标记文件的钩子。
    返回（HOME 目录，标记文件）；任何钩子被 git 执行，标记文件就会出现。"""
    home = root / "hostile-home"
    marker = root / "hostile-hook-ran"
    script = f"#!/bin/sh\ncat > /dev/null\necho \"$0 $*\" >> '{marker}'\nexit 0\n"
    hooks, template = home / "hooks", home / "template"
    for directory in (hooks, template / "hooks"):
        directory.mkdir(parents=True)
        for name in HOOK_NAMES:
            (directory / name).write_text(script, encoding="utf-8")
            (directory / name).chmod(0o755)
    lines = []
    if hooks_path:
        lines += ["[core]", f"\thooksPath = {hooks}"]
    if template_dir:
        lines += ["[init]", f"\ttemplateDir = {template}"]
    (home / ".gitconfig").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (home / "xdg").mkdir()
    return home, marker


def git_version() -> tuple[int, int]:
    """本机 git 的（主版本, 次版本）；取不到返回 (0, 0)。"""
    try:
        output = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return 0, 0
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def subcommand_index(argv: list[str]) -> int:
    """git 命令行里子命令的位置：跳过 git 自己的全局选项（``-C <目录>``、``-c <键=值>``、``--no-optional-locks`` 等）。"""
    index = 1
    while index < len(argv):
        if argv[index] in {"-C", "-c"}:
            index += 2
        elif argv[index].startswith("-"):
            index += 1
        else:
            return index
    raise AssertionError(argv)


class GitSubprocessIsolationTests(bvt.CrossCommitCase):
    """祖先验证里每个 git 子进程：全局配置（~/.gitconfig、XDG）不读，钩子不跑。验证本身仍然成功。"""

    VARIANTS = {"core.hooksPath": dict(hooks_path=True, template_dir=False),
                "init.templateDir": dict(hooks_path=False, template_dir=True),
                "两者都有": dict(hooks_path=True, template_dir=True)}

    def verify(self, scene: cross.Scenario, home: Path) -> dict:
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")}):
            return bv._verify_ancestry(scene.c7, scene.c8, bundle=scene.h8_bundle, branch=cross.BRANCH, work=scene.work)

    def new_home(self, prefix: str, label: str, **options) -> tuple[Path, Path]:
        root = self.root / (prefix + re.sub(r"\W+", "-", label))
        root.mkdir()
        return hostile_home(root, **options)

    def test_a_hostile_global_gitconfig_does_not_run_any_hook(self) -> None:
        scene = self.prepared(real_git=True)
        for label, options in self.VARIANTS.items():
            with self.subTest(label=label):
                home, marker = self.new_home("home-", label, **options)
                result = self.verify(scene, home)
                self.assertEqual(result["status"], "verified", result)
                self.assertFalse(marker.exists(), f"全局配置里的钩子被执行了：{marker.read_text() if marker.exists() else ''}")

    def test_the_command_line_alone_also_blocks_hooks_if_the_environment_variable_is_ignored(self) -> None:
        """纵深防御：老版本 git（2.32 之前）不认 GIT_CONFIG_GLOBAL，全局配置仍会被读到；命令行上的
        ``-c core.hooksPath=/dev/null`` 必须单独就挡住钩子（含从 init.templateDir 复制进临时库的模板钩子）。"""
        scene = self.prepared(real_git=True)

        def environment_without_the_global_override() -> dict[str, str]:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            environment.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", LC_ALL="C")
            return environment

        for label, options in self.VARIANTS.items():
            with self.subTest(label=label):
                home, marker = self.new_home("legacy-", label, **options)
                with mock.patch.object(bv, "_git_environment", environment_without_the_global_override):
                    result = self.verify(scene, home)
                self.assertEqual(result["status"], "verified", result)
                self.assertFalse(marker.exists(), "命令行防线失效：钩子被执行了")

    @unittest.skipIf(git_version() < (2, 28), "fetch 更新引用时触发 reference-transaction 钩子需要 git 2.28 及以上")
    def test_the_hostile_configuration_is_really_read_when_nothing_protects_it(self) -> None:
        """对照实验，证明上面两个用例不是空转：不加任何防护的 git（直接调用）确实会执行同一份恶意配置里的钩子。"""
        scene = self.prepared(real_git=True)
        probe_home, probe_marker = self.new_home("probe-", "executable", hooks_path=True, template_dir=False)
        try:
            subprocess.run([str(probe_home / "hooks" / "reference-transaction")], input=b"", capture_output=True, check=False)
        except OSError:
            self.skipTest("临时目录不允许执行脚本（例如 noexec 挂载），钩子对照实验做不了")
        probe_marker.unlink(missing_ok=True)          # 探针自己也会写标记，清掉再开始
        for label, options in self.VARIANTS.items():
            with self.subTest(label=label):
                home, marker = self.new_home("control-", label, **options)
                full_bundle = scene.git.bundle(self.root / ("full-" + re.sub(r"\W+", "-", label) + ".bundle"), None, cross.BRANCH)
                repo = self.root / ("unprotected-" + re.sub(r"\W+", "-", label) + ".git")
                environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                environment.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "xdg"), GIT_CONFIG_NOSYSTEM="1")
                subprocess.run(["git", "init", "--bare", "-q", str(repo)], check=True, capture_output=True, env=environment)
                subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--no-tags", str(full_bundle),
                                f"+refs/heads/{cross.BRANCH}:refs/heads/probe"], check=True, capture_output=True, env=environment)
                self.assertTrue(marker.exists(), "恶意配置里的 reference-transaction 钩子应当被执行（对照实验）")

    @unittest.skipIf(git_version() < (2, 32), "GIT_CONFIG_GLOBAL 需要 git 2.32 及以上")
    def test_the_environment_alone_hides_the_hostile_global_configuration(self) -> None:
        """环境变量这一道防线单独看：git 在 ``_git_environment()`` 的环境里读不到恶意的全局配置（命令行上的 ``-c`` 防线不参与）。
        端到端的钩子用例两道防线任一在就过，所以环境防线要由这条专项用例单独守住。"""
        home, _ = self.new_home("hidden-", "env", hooks_path=True, template_dir=True)
        with mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "xdg")}):
            protected = bv._git_environment()
        unprotected = {key: value for key, value in protected.items() if key != "GIT_CONFIG_GLOBAL"}
        command = ["git", "config", "--list", "--show-origin"]
        seen = subprocess.run(command, capture_output=True, text=True, env=unprotected, cwd=self.root, check=False).stdout.lower()
        self.assertIn("hookspath", seen, "对照：不屏蔽时恶意的全局配置确实读得到")
        self.assertIn("templatedir", seen)
        hidden = subprocess.run(command, capture_output=True, text=True, env=protected, cwd=self.root, check=False).stdout.lower()
        self.assertNotIn("hookspath", hidden)
        self.assertNotIn("templatedir", hidden)

    def test_environment_pins_the_global_and_system_configuration(self) -> None:
        polluted = {"GIT_CONFIG_GLOBAL": "/evil/config", "GIT_TEMPLATE_DIR": "/evil/template", "GIT_DIR": "/evil/dir",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/evil/hooks"}
        with mock.patch.dict(os.environ, polluted):
            environment = bv._git_environment()
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual({key for key in environment if key.startswith("GIT_")},
                         {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT"}, "继承来的其余 GIT_* 一律剥掉")

    def test_every_git_command_line_disables_hooks_before_the_subcommand(self) -> None:
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(bv.subprocess, "run", side_effect=fake_run):
            bv._git(Path("/some/repo"), "-c", "gc.auto=0", "fetch", "-q")
            bv._git(None, "init", "--bare", "-q", "/some/where")
        self.assertEqual(len(captured), 2)
        for argv in captured:
            self.assertEqual(argv[0], "git")
            position = argv.index("core.hooksPath=/dev/null")
            self.assertEqual(argv[position - 1], "-c")
            self.assertLess(position, subcommand_index(argv), "-c 必须出现在子命令之前才生效")
        self.assertEqual(captured[0][:4], ["git", "--no-optional-locks", "-C", "/some/repo"], "原有的前缀不变")
        self.assertEqual(captured[0][subcommand_index(captured[0])], "fetch")
        self.assertEqual(captured[1][subcommand_index(captured[1])], "init")


if __name__ == "__main__":
    unittest.main()
