"""v4 轨迹合同（read-audit-trace/v4）的解析、去重、重放、兼容与接线。

* 解析：用 strace 6.8 的真实行格式（``-f -y``）构造 rename／renameat／renameat2／link／linkat，含 unfinished／resumed
  拆行、多进程交错、一端在盘外、失败行与截断行，核对 call／role／peer／errno／flags／epoch；
* 去重：30 万条相同只读事件去重后只剩几条且通过；超过上限的互不相同事件仍判 output-event-limit 失败（v3 上限 20 万，v4 上限见 WP-R2）；
* 重放：合成 strace 行 → ``filter_stream`` → ``replay_runtime`` 通过；篡改 call 配对、role、peer、epoch、errno 等任一字段都被拒绝；
* 兼容：v4 只由显式开关 ``trace_contract`` 启用，默认仍是 v3；v3 凭证按旧合同验真；control 四链（原样复用 read_audit.py）
  生产并重放的仍是 v3，不受影响；
* 接线：``strace_argv`` 的命令行、``filter`` 子命令、``prepare`` 写入请求、``run_child`` 把开关交给过滤器。
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import control_read_audit as control_audit
from tools.ci import control_read_audit_runtime as control_runtime
from tools.ci import read_audit as ra
from tools.ci import read_audit_runtime as runtime

V3_EVENT_KEYS = {"path", "kind", "name", "succeeded", "flags", "target"}
V4_EVENT_KEYS = V3_EVENT_KEYS | {"errno", "call", "role", "peer", "epoch"}


class AuditFixture(unittest.TestCase):
    """基类：搭一份带生产别名遮挡的隔离合同与上下文（与现有隔离测试同构），提供文档生成与重放的辅助方法。本类没有 test_ 方法。"""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.root = str(self.base / "private" / "output")
        self.contract = {"schema_version": runtime.SCHEMA, "temporary_root": self.root,
                         "mask_roots": [runtime.MASK_ROOT], "environment_paths": []}
        self.entry = runtime.contract_entry(self.contract)
        trace = ('1 mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL) = 0\n'
                 f'2 mount("tmpfs", "{runtime.MASK_ROOT}", "tmpfs", MS_RDONLY, "size=64k,mode=0755") = 0\n'
                 f'3 mount("tmpfs", "{self.root}", "tmpfs", MS_NOSUID|MS_NODEV, "size=512m,mode=0700") = 0\n')
        self.context = {"schema_version": runtime.CONTEXT_SCHEMA, "contract_entry": self.entry,
                        "parent_namespace": "mnt:[100]", "namespace": "mnt:[101]", "empty_temporary_root": True,
                        "private_mounts": True, "empty_masks": {runtime.MASK_ROOT: True}, "mounts": [
                            {"mount_id": 200, "target": runtime.MASK_ROOT, "filesystem": "tmpfs", "options": ["ro"]},
                            {"mount_id": 201, "target": self.root, "filesystem": "tmpfs", "options": ["rw", "nosuid", "nodev"]}],
                        "setup_commands": ["private", runtime.MASK_ROOT, self.root], "setup_success": True,
                        "setup_trace": trace, "setup_trace_sha256": hashlib.sha256(trace.encode()).hexdigest()}

    # ------------------------------------------------------------------ 上下文与文档

    def control_context(self) -> dict[str, Any]:
        """四条 control 的模型凭证（与现有隔离测试同构）：空遮挡由 control 启动建立，只用于核对 control 链仍是 v3。"""
        context = copy.deepcopy(self.context)
        context["contract_entry"] = runtime.contract_entry({**self.contract, "mask_roots": [],
                                                             "environment_paths": sorted(runtime.ENVIRONMENT_PATHS)})
        mask_mount = context["mounts"][0]
        context["mounts"] = [context["mounts"][1]]
        context["empty_masks"] = {}
        context["setup_commands"] = ["private", self.root]
        context["setup_trace"] = "\n".join(line for line in context["setup_trace"].splitlines() if runtime.MASK_ROOT not in line) + "\n"
        context["setup_trace_sha256"] = hashlib.sha256(context["setup_trace"].encode()).hexdigest()
        mask_trace = f'1 mount("tmpfs", "{runtime.MASK_ROOT}", "tmpfs", MS_RDONLY, "size=64k,mode=0755") = 0\n'
        context["control_chain"] = {
            "scenario": "vc-chain.claude-reconcile", "parent_network_namespace": "net:[200]", "network_namespace": "net:[201]",
            "interfaces": ["lo"], "data_mount": None, "mask_mount": mask_mount, "empty_mask": True,
            "context_path": str(self.base / "control.context.json"), "setup_trace": mask_trace,
            "setup_trace_sha256": hashlib.sha256(mask_trace.encode()).hexdigest(),
            "filesystem_ids": {self.root: "42", runtime.MASK_ROOT: "43"}}
        return context

    def completed(self, document: dict[str, Any], **extra: Any) -> dict[str, Any]:
        document["runtime"]["completion"] = {"command_finished": True, "host_inputs_unchanged": True,
                                            "environment_unchanged": True, "namespace_unchanged": True,
                                            "host_entries_sha256": runtime.digest([]), **extra}
        return document

    def document(self, lines: list[str], contract: str = "v4") -> dict[str, Any]:
        kwargs = {} if contract == "v3" else {"trace_contract": contract}
        return self.completed(ra.filter_stream(lines, ["/"], strict=True, runtime_context=self.context, **kwargs))

    def audit(self, document: dict[str, Any], entry: dict[str, Any] | None = None) -> dict[str, Any]:
        return ra.strict_audit_reads(document, [entry or self.entry], repo_root="/repo", data_root=None)

    def assertAccepted(self, document: dict[str, Any]) -> None:
        report = self.audit(document)
        self.assertTrue(report["coverage_complete"], (document["runtime"]["outputs"]["problems"], report["undeclared"]))

    def assertRejected(self, document: dict[str, Any], message: str = "") -> None:
        """重放函数本身必须抛合同错误，经 strict_audit_reads 汇总后也不能算覆盖完整。"""
        with self.assertRaises(runtime.RuntimeContractError, msg=message):
            runtime.replay_runtime(document, [self.entry])
        self.assertFalse(self.audit(document)["coverage_complete"], message)

    # ------------------------------------------------------------------ 事件与行的拼装

    def shape(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        """输出事件，路径里的私有根替换成 <R>，方便比对。"""
        result = []
        for event in document["runtime"]["output_events"]:
            item = dict(event)
            for key in ("path", "peer", "target"):
                if isinstance(item[key], str):
                    item[key] = item[key].replace(self.root, "<R>")
            result.append(item)
        return result

    def mkdir_line(self, relative: str = "d", pid: int = 1) -> str:
        return f'{pid} mkdir("{self.root}/{relative}", 0700) = 0'

    def create_line(self, relative: str, pid: int = 1) -> str:
        path = f"{self.root}/{relative}"
        return f'{pid} openat(AT_FDCWD</repo>, "{path}", O_WRONLY|O_CREAT|O_EXCL, 0600) = 3<{path}>'

    def read_line(self, relative: str, pid: int = 1) -> str:
        path = f"{self.root}/{relative}"
        return f'{pid} openat(AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{path}>'

    def prelude(self) -> list[str]:
        """建目录 d、建文件 d/a：后面的两路径调用都以它们为起点。"""
        return [self.mkdir_line("d"), self.create_line("d/a")]


class TraceParserTests(AuditFixture):
    """strace 行 → v4 事件：call／role／peer／errno／flags／epoch 的形状。"""

    PAIR_LINES = {
        "rename": '1 rename("{r}/d/a", "{r}/d/b") = 0',
        "renameat": '1 renameat(AT_FDCWD<{r}/d>, "a", AT_FDCWD<{r}/d>, "b") = 0',
        "renameat2": '1 renameat2(AT_FDCWD<{r}/d>, "a", AT_FDCWD<{r}/d>, "b", RENAME_NOREPLACE) = 0',
        "link": '1 link("{r}/d/a", "{r}/d/b") = 0',
        "linkat": '1 linkat(AT_FDCWD<{r}/d>, "a", AT_FDCWD<{r}/d>, "b", 0) = 0',
    }

    def test_two_path_calls_emit_a_source_and_a_destination_event_sharing_one_call(self) -> None:
        for name, template in self.PAIR_LINES.items():
            with self.subTest(name=name):
                document = self.document(self.prelude() + [template.format(r=self.root), self.read_line("d/b")])
                self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA_V4)
                self.assertEqual(document["coverage"]["unresolved"], 0)
                pair = [event for event in self.shape(document) if event["name"] == name]
                self.assertEqual(len(pair), 2)
                source, destination = pair
                flags = ["RENAME_NOREPLACE"] if name == "renameat2" else []
                self.assertEqual((source["role"], source["path"], source["peer"]), ("source", "<R>/d/a", "<R>/d/b"))
                self.assertEqual((destination["role"], destination["path"], destination["peer"]), ("destination", "<R>/d/b", "<R>/d/a"))
                for event in pair:
                    self.assertEqual((event["kind"], event["succeeded"], event["errno"], event["flags"], event["target"]),
                                     ("write", True, None, flags, None))
                self.assertEqual(source["call"], destination["call"])
                self.assertEqual(set(source), V4_EVENT_KEYS)
                self.assertAccepted(document)

    def test_dirfd_annotations_other_than_cwd_resolve_both_ends(self) -> None:
        line = f'1 renameat(5<{self.root}/d>, "a", 6<{self.root}/e>, "b") = 0'
        document = self.document([self.mkdir_line("d"), self.mkdir_line("e"), self.create_line("d/a"), line, self.read_line("e/b")])
        pair = [event for event in self.shape(document) if event["name"] == "renameat"]
        self.assertEqual([(e["role"], e["path"], e["peer"]) for e in pair],
                         [("source", "<R>/d/a", "<R>/e/b"), ("destination", "<R>/e/b", "<R>/d/a")])
        self.assertAccepted(document)

    def test_relative_paths_use_the_process_working_directory(self) -> None:
        lines = self.prelude() + [f'1 chdir("{self.root}/d") = 0', '1 rename("a", "b") = 0', self.read_line("d/b")]
        document = self.document(lines)
        pair = [event for event in self.shape(document) if event["name"] == "rename"]
        self.assertEqual([(e["role"], e["path"], e["peer"]) for e in pair],
                         [("source", "<R>/d/a", "<R>/d/b"), ("destination", "<R>/d/b", "<R>/d/a")])
        self.assertAccepted(document)

    def test_unfinished_and_resumed_two_path_call_is_one_call_numbered_at_completion(self) -> None:
        lines = self.prelude() + [
            f'7 renameat2(AT_FDCWD<{self.root}/d>, "a", AT_FDCWD<{self.root}/d>, "b", RENAME_NOREPLACE <unfinished ...>',
            self.read_line("d/a", pid=8),            # 别的进程在 rename 完成之前读旧名字：此时旧名字仍有效
            '7 <... renameat2 resumed>)                 = 0',
            self.read_line("d/b", pid=8),            # 完成之后读新名字
        ]
        document = self.document(lines)
        events = self.shape(document)
        names = [(e["name"], e["path"], e["call"]) for e in events]
        read_old, read_new = next(e for e in events if e["path"] == "<R>/d/a" and e["name"] == "openat" and e["kind"] == "read"), events[-1]
        pair = [e for e in events if e["name"] == "renameat2"]
        self.assertEqual(len({e["call"] for e in pair}), 1)
        self.assertLess(read_old["call"], pair[0]["call"], "unfinished 的调用在 resumed 行完成时才编号：" + str(names))
        self.assertLess(pair[0]["call"], read_new["call"])
        self.assertEqual(pair[0]["flags"], ["RENAME_NOREPLACE"])
        self.assertEqual(document["coverage"]["unfinished_calls"], 0)
        self.assertAccepted(document)

    def test_two_overlapping_unfinished_calls_from_two_pids_do_not_mix(self) -> None:
        lines = self.prelude() + [
            self.create_line("d/x"),
            f'7 rename("{self.root}/d/a", "{self.root}/d/b" <unfinished ...>',
            f'8 link("{self.root}/d/x", "{self.root}/d/y" <unfinished ...>',
            '8 <... link resumed>)                      = 0',
            '7 <... rename resumed>)                    = 0',
            self.read_line("d/b"), self.read_line("d/y"),
        ]
        document = self.document(lines)
        events = self.shape(document)
        link = [e for e in events if e["name"] == "link"]
        rename = [e for e in events if e["name"] == "rename"]
        self.assertEqual([(e["role"], e["path"], e["peer"]) for e in link],
                         [("source", "<R>/d/x", "<R>/d/y"), ("destination", "<R>/d/y", "<R>/d/x")])
        self.assertEqual([(e["role"], e["path"], e["peer"]) for e in rename],
                         [("source", "<R>/d/a", "<R>/d/b"), ("destination", "<R>/d/b", "<R>/d/a")])
        self.assertLess(link[0]["call"], rename[0]["call"], "先完成的调用编号在前")
        self.assertAccepted(document)

    def test_one_end_outside_the_private_root_is_recorded_as_peer_and_refused(self) -> None:
        cases = [
            (f'1 rename("{self.root}/d/a", "/host/new") = -1 EXDEV (Invalid cross-device link)',
             ("source", "<R>/d/a", "/host/new", False, "EXDEV")),
            (f'1 link("/host/old", "{self.root}/d/b") = 0', ("destination", "<R>/d/b", "/host/old", True, None)),
            (f'1 renameat2(AT_FDCWD</host>, "old", AT_FDCWD<{self.root}/d>, "b", RENAME_NOREPLACE) = 0',
             ("destination", "<R>/d/b", "/host/old", True, None)),
        ]
        for line, (role, path, peer, ok, errno) in cases:
            with self.subTest(line=line):
                document = self.document(self.prelude() + [line])
                last = self.shape(document)[-1]
                self.assertEqual((last["role"], last["path"], last["peer"], last["succeeded"], last["errno"]), (role, path, peer, ok, errno))
                self.assertEqual(len([e for e in document["runtime"]["output_events"] if e["call"] == last["call"]]), 1,
                                 "盘外的一端不产生事件，只有盘内一端")
                self.assertTrue(any("另一端不在私有临时盘内" in text for text in document["runtime"]["outputs"]["problems"]))
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_empty_path_end_has_no_peer_and_is_refused(self) -> None:
        line = f'1 linkat(5<{self.root}/d/a>, "", AT_FDCWD<{self.root}/d>, "b", AT_EMPTY_PATH) = 0'
        document = self.document(self.prelude() + [line])
        last = self.shape(document)[-1]
        self.assertEqual((last["name"], last["role"], last["peer"]), ("linkat", "destination", None))
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_failed_two_path_call_keeps_both_ends_with_the_errno(self) -> None:
        lines = self.prelude() + [self.create_line("d/b"),
                                  f'1 link("{self.root}/d/a", "{self.root}/d/b") = -1 EEXIST (File exists)']
        document = self.document(lines)
        pair = [e for e in self.shape(document) if e["name"] == "link"]
        self.assertEqual([(e["role"], e["succeeded"], e["errno"]) for e in pair], [("source", False, "EEXIST"), ("destination", False, "EEXIST")])
        self.assertAccepted(document)

    def test_rename_onto_the_same_path_keeps_two_events_with_swapped_peers(self) -> None:
        document = self.document(self.prelude() + [f'1 rename("{self.root}/d/a", "{self.root}/d/a") = 0', self.read_line("d/a")])
        pair = [e for e in self.shape(document) if e["name"] == "rename"]
        self.assertEqual([(e["role"], e["path"], e["peer"]) for e in pair],
                         [("source", "<R>/d/a", "<R>/d/a"), ("destination", "<R>/d/a", "<R>/d/a")])
        self.assertAccepted(document)

    def test_renameat2_flags_are_decoded_or_marked_unresolved(self) -> None:
        template = '1 renameat2(AT_FDCWD<{r}/d>, "a", AT_FDCWD<{r}/d>, "b", {flags}) = 0'
        for flags, expected in (("0", []), ("RENAME_NOREPLACE", ["RENAME_NOREPLACE"]),
                                ("RENAME_EXCHANGE", ["RENAME_EXCHANGE"]),
                                ("RENAME_NOREPLACE|RENAME_WHITEOUT", ["RENAME_NOREPLACE", "RENAME_WHITEOUT"])):
            with self.subTest(flags=flags):
                document = self.document(self.prelude() + [template.format(r=self.root, flags=flags)])
                pair = [e for e in document["runtime"]["output_events"] if e["name"] == "renameat2"]
                self.assertEqual([e["flags"] for e in pair], [expected, expected])
                self.assertEqual(document["coverage"]["unresolved"], 0)
        for flags in ("0x10", "RENAME_NOREPLACE|0x8"):
            with self.subTest(flags=flags):
                document = self.document(self.prelude() + [template.format(r=self.root, flags=flags)])
                self.assertEqual(document["coverage"]["unresolved_reasons"], {"rename-flags-not-decoded:renameat2": 1})
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_exchange_and_whiteout_are_recorded_and_refused_by_the_tracker(self) -> None:
        for flag in ("RENAME_EXCHANGE", "RENAME_WHITEOUT"):
            with self.subTest(flag=flag):
                line = f'1 renameat2(AT_FDCWD<{self.root}/d>, "a", AT_FDCWD<{self.root}/d>, "b", {flag}) = 0'
                document = self.document(self.prelude() + [self.create_line("d/b"), line])
                self.assertTrue(any("RENAME_EXCHANGE" in text for text in document["runtime"]["outputs"]["problems"]))
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_errno_is_carried_for_every_call_kind_and_none_on_success(self) -> None:
        lines = self.prelude() + [
            f'1 mkdir("{self.root}/d", 0700) = -1 EEXIST (File exists)',
            f'1 openat(AT_FDCWD</repo>, "{self.root}/d/none", O_RDONLY) = -1 ENOENT (No such file or directory)',
            f'1 unlink("{self.root}/d/none") = -1 ENOENT (No such file or directory)',
            self.read_line("d/a"),
        ]
        events = self.shape(self.document(lines))
        view = [(e["name"], e["kind"], e["succeeded"], e["errno"]) for e in events]
        self.assertIn(("mkdir", "write", False, "EEXIST"), view)
        self.assertIn(("openat", "missing", False, "ENOENT"), view)
        self.assertIn(("unlink", "write", False, "ENOENT"), view)
        self.assertTrue(all(errno is None for _, _, ok, errno in view if ok))
        self.assertTrue(all(errno is not None for _, _, ok, errno in view if not ok))

    def test_non_two_path_calls_have_no_role_or_peer_and_share_call_between_their_events(self) -> None:
        link_target = f'1 openat(AT_FDCWD</repo>, "{self.root}/d/alias", O_RDONLY) = 3<{self.root}/d/a>'
        document = self.document(self.prelude() + [link_target])
        events = self.shape(document)
        self.assertTrue(all(e["role"] is None and e["peer"] is None for e in events if e["name"] not in runtime.TWO_PATH_NAMES))
        opened = [e for e in events if e["path"] in {"<R>/d/alias", "<R>/d/a"} and e["kind"] == "read"]
        self.assertEqual({e["path"] for e in opened}, {"<R>/d/alias", "<R>/d/a"}, "请求路径与返回描述符路径各记一条")
        self.assertEqual(len({e["call"] for e in opened}), 1, "同一调用的所有事件共享编号")
        calls = [e["call"] for e in events]
        self.assertEqual(calls, sorted(calls))

    def test_epoch_counts_the_preceding_state_changing_events(self) -> None:
        lines = self.prelude() + [self.read_line("d/a"),
                                  f'1 rename("{self.root}/d/a", "{self.root}/d/b") = 0', self.read_line("d/b")]
        events = self.shape(self.document(lines))
        view = [(e["name"], e["path"], e["epoch"]) for e in events]
        # mkdir 改状态(0→1)；openat 创建 d/a 改状态(1→2)，其返回描述符那条事件已在纪元 2；
        # rename 的来源端事件改状态(2→3)，目的端事件在纪元 3；之后的读取也在纪元 3。
        self.assertEqual(view, [("mkdir", "<R>/d", 0), ("openat", "<R>/d/a", 1), ("openat", "<R>/d/a", 2),
                                ("openat", "<R>/d/a", 2), ("rename", "<R>/d/a", 2), ("rename", "<R>/d/b", 3),
                                ("openat", "<R>/d/b", 3)])

    def test_truncated_and_unfinished_two_path_lines_fail_closed(self) -> None:
        cases = {
            "缺返回值": [f'1 rename("{self.root}/d/a", "{self.root}/d/b")'],
            "未完成": [f'1 renameat2(AT_FDCWD<{self.root}/d>, "a", AT_FDCWD<{self.root}/d>, "b", RENAME_NOREPLACE <unfinished ...>'],
            "路径截断": [f'1 rename("{self.root}/d/a"..., "{self.root}/d/b") = 0'],
            "缺 resumed 的起点": ['1 <... rename resumed>) = 0'],
        }
        for label, tail in cases.items():
            with self.subTest(label=label):
                document = self.document(self.prelude() + tail)
                self.assertGreater(document["coverage"]["unresolved"] + document["coverage"]["unfinished_calls"], 0)
                self.assertFalse(self.audit(document)["coverage_complete"])

    def test_v3_default_parsing_is_unchanged_by_the_new_fields(self) -> None:
        lines = self.prelude() + [f'1 rename("{self.root}/d/a", "{self.root}/d/b") = 0', self.read_line("d/b")]
        document = self.document(lines, contract="v3")
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertTrue(all(set(event) == V3_EVENT_KEYS for event in document["runtime"]["output_events"]))
        self.assertTrue(any("链接或移动操作尚无输出归属证明" in text for text in document["runtime"]["outputs"]["problems"]),
                        "旧合同仍然不认 rename")
        self.assertFalse(self.audit(document)["coverage_complete"])


class DedupAndLimitTests(AuditFixture):
    """只读事件按变更纪元去重；v3 上限 200,000 条（v4 上限 WP-R2 起单列），去重后超限照旧判失败。"""

    def parser(self, contract: str = "v4") -> ra.TraceParser:
        parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract=contract)
        for line in self.prelude():
            parser.feed(line)
        return parser

    def push_read(self, parser: ra.TraceParser, relative: str, *, name: str = "openat", kind: str = "read",
                  target: str | None = None) -> None:
        """不经过逐行解析，直接模拟「解析器刚读完一个完整调用」后登记一条事件：测大规模流用，速度比逐行快得多。

        ``target`` 是返回描述符解析出的路径（默认等于请求路径）；换成同在私有盘内的不同路径，就能在不引入归属问题的前提下
        制造「互不相同」的事件。"""
        path = f"{self.root}/{relative}"
        parser.lines += 1
        parser.call_id += 1
        parser.call_errno = None
        parser.call_end = None
        parser.event = (name, f'AT_FDCWD</repo>, "{path}", O_RDONLY) = 3<{target or path}>', True)
        parser._add(path, kind, "合成")

    def finish(self, parser: ra.TraceParser) -> dict[str, Any]:
        return self.completed(ra.trace_document(parser, ["/"]))

    def test_three_hundred_thousand_identical_reads_collapse_and_still_pass(self) -> None:
        parser = self.parser()
        for _ in range(300_000):
            self.push_read(parser, "d/a")
        document = self.finish(parser)
        events = document["runtime"]["output_events"]
        self.assertLess(len(events), 10, "30 万条相同只读事件去重后只应剩首次出现的几条")
        self.assertEqual(document["coverage"]["unresolved"], 0)
        self.assertEqual(document["coverage"]["unresolved_reasons"], {})
        self.assertAccepted(document)
        self.assertIn((f"{self.root}/d/a", "read"), {(e["path"], e["kind"]) for e in events})

    def test_the_same_stream_under_v3_hits_the_limit_and_fails(self) -> None:
        """基线对照：旧合同不去重，同样的流会撞上 200,000 条上限。"""
        parser = self.parser("v3")
        prelude_events = len(parser.output_events)
        pushed = ra.MAX_OUTPUT_EVENTS + 5_000
        for _ in range(pushed):
            self.push_read(parser, "d/a")
        document = self.finish(parser)
        self.assertEqual(len(document["runtime"]["output_events"]), ra.MAX_OUTPUT_EVENTS)
        self.assertEqual(document["coverage"]["unresolved_reasons"],
                         {"output-event-limit": prelude_events + pushed - ra.MAX_OUTPUT_EVENTS})
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_more_than_the_limit_of_distinct_events_still_fails_with_output_event_limit(self) -> None:
        # WP-R2：v4 的上限单列为 MAX_OUTPUT_EVENTS_V4（1,000,000）；这里把它调小，只验证“去重后超限仍判失败”的机制，
        # 不用真的推 100 万条事件（真实上限的合成负载实测见 test_ci_read_audit_wp_r2）。
        limit = 20_000
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS_V4", limit):
            parser = self.parser()
            prelude_events = len(parser.output_events)
            extra = 100
            pushed = limit + extra
            for index in range(pushed):
                self.push_read(parser, "d/a", target=f"{self.root}/alias{index}")    # 目标路径各不相同 → 键各不相同 → 无法去重
            document = self.finish(parser)
        self.assertEqual(len(document["runtime"]["output_events"]), limit)
        self.assertEqual(document["coverage"]["unresolved_reasons"],
                         {"output-event-limit": prelude_events + pushed - limit})
        report = self.audit(document)
        self.assertFalse(report["coverage_complete"])
        # 事件本身都合法，轨迹判失败的唯一原因就是撞上了事件上限（登记为未解析调用）。
        self.assertEqual({finding["root"] for finding in report["undeclared"]}, {"audit"})

    def test_the_limit_applies_after_deduplication_in_line_mode_too(self) -> None:
        """逐行解析路径同样去重后计数：把上限临时调小，第 limit+1 条互不相同的事件登记 output-event-limit。"""
        with mock.patch.object(ra, "MAX_OUTPUT_EVENTS_V4", 12):
            lines = self.prelude() + [self.read_line("d/a")] * 50
            document = self.document(lines)
            self.assertLess(len(document["runtime"]["output_events"]), 12, "重复读取去重后不撞上限")
            self.assertEqual(document["coverage"]["unresolved"], 0)
            distinct = [f'1 newfstatat(AT_FDCWD</repo>, "{self.root}/d/p{index}", {{st_mode=S_IFREG|0644}}, 0) = 0' for index in range(30)]
            document = self.document(self.prelude() + distinct)
            self.assertEqual(len(document["runtime"]["output_events"]), 12)
            self.assertGreater(document["coverage"]["unresolved_reasons"]["output-event-limit"], 0)

    def test_reads_are_deduplicated_only_within_one_epoch(self) -> None:
        r = self.root
        lines = self.prelude() + [self.read_line("d/a")] * 3 + [
            f'1 rename("{r}/d/a", "{r}/d/b") = 0',
            f'1 rename("{r}/d/b", "{r}/d/a") = 0'] + [self.read_line("d/a")] * 3
        document = self.document(lines)
        reads = [e for e in self.shape(document) if e["name"] == "openat" and e["kind"] == "read" and e["path"] == "<R>/d/a"]
        self.assertEqual([e["epoch"] for e in reads], [2, 4], "状态改变之后同样的读取要重新保留一条，纪元不同")
        self.assertAccepted(document)

    def test_distinct_keys_are_never_merged(self) -> None:
        r = self.root
        lines = self.prelude() + [
            self.read_line("d/a"),
            f'1 openat(AT_FDCWD</repo>, "{r}/d/a", O_RDONLY|O_CLOEXEC) = 3<{r}/d/a>',        # 标志不同
            f'1 newfstatat(AT_FDCWD</repo>, "{r}/d/a", {{st_mode=S_IFREG|0644}}, 0) = 0',    # 调用名不同
            f'1 openat(AT_FDCWD</repo>, "{r}/d/none", O_RDONLY) = -1 ENOENT (No such file or directory)',
            f'1 openat(AT_FDCWD</repo>, "{r}/d/none", O_RDONLY) = -1 ENOTDIR (Not a directory)',   # 错误码不同
            f'1 openat(AT_FDCWD</repo>, "{r}/d/none", O_RDONLY) = -1 ENOENT (No such file or directory)',   # 与上面第一条相同：去重
        ]
        events = self.shape(self.document(lines))
        view = [(e["name"], e["path"], tuple(e["flags"]), e["errno"]) for e in events]
        self.assertEqual(sorted(v[3] for v in view if v[1] == "<R>/d/none"), ["ENOENT", "ENOTDIR"],
                         "错误码不同的两条保留，第三条与第一条相同被去重")
        self.assertEqual(len([v for v in view if v[1] == "<R>/d/a" and v[0] == "openat" and v[2] == ("O_RDONLY",)]), 1)
        self.assertEqual(len([v for v in view if v[1] == "<R>/d/a" and v[2] == ("O_CLOEXEC", "O_RDONLY")]), 1)
        self.assertEqual(len([v for v in view if v[0] == "newfstatat"]), 1)

    def test_state_changing_events_are_never_merged_even_when_identical(self) -> None:
        r = self.root
        cycle = [self.create_line("d/x"), f'1 unlink("{r}/d/x") = 0']
        document = self.document(self.prelude() + cycle * 4)
        events = self.shape(document)
        on_x = [e for e in events if e["path"] == "<R>/d/x"]
        self.assertEqual(len([e for e in on_x if e["name"] == "unlink"]), 4, "四次相同的删除都改变状态，一条都不能合并")
        self.assertEqual(len(on_x), 12, "四次创建各两条（请求路径与返回描述符，后者在新纪元里保留）加四次删除")
        epochs = [e["epoch"] for e in events]
        self.assertEqual(epochs, sorted(epochs), "纪元沿事件序列单调不减")
        self.assertAccepted(document)

    def test_deduplication_never_changes_the_tracker_verdict_on_random_streams(self) -> None:
        """去重的安全性质：把只读重复事件去掉，跟踪器最终的问题集合与名字表和「全量事件」完全一致，且去重后的事件流可重放。

        事件流来自跟踪器测试里的随机历史模拟器（合法操作为主），再随机插入重复的只读事件和少量非法事件（未证明的读取、
        符号链接、盘外对端），所以流里既有可去重的重复，也有会产生问题的事件。"""
        import collections
        import random
        from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as simulation
        helper = simulation.TrackerSimulationTests("test_random_valid_histories_match_the_real_filesystem_semantics")

        def translate(event: dict[str, Any]) -> dict[str, Any]:
            """模拟器用固定的根；换成本测试夹具的私有根。"""
            result = dict(event)
            for key in ("path", "peer", "target"):
                if isinstance(result[key], str):
                    result[key] = result[key].replace(simulation.ROOT, self.root)
            return result

        dropped_total = problem_streams = stale_reads = 0
        for seed in range(30):
            rng = random.Random(7000 + seed)
            fs, builder, statistics = simulation._SimFs(simulation.ROOT), simulation.Builder(), collections.Counter()
            stream: list[dict[str, Any]] = []
            read_paths: set[str] = set()
            for _ in range(250):
                events = helper.apply_random_operation(rng, fs, builder, statistics)
                stream.extend(events)
                reads = [event for event in events if event["kind"] in {"read", "stat", "missing"}]
                read_paths.update(event["path"] for event in events if event["kind"] == "read" and event["succeeded"])
                if reads and rng.random() < 0.6:
                    for _ in range(rng.randint(1, 3)):
                        for event in reads:
                            builder.call += 1
                            stream.append(dict(event, call=builder.call))        # 重复的只读事件：同键，新的调用编号
                if rng.random() < 0.02:
                    stream.append(builder.event("openat", simulation.ROOT + "/d1/ghost", kind="read", flags=("O_RDONLY",),
                                                target=simulation.ROOT + "/d1/ghost"))
                if rng.random() < 0.01:
                    stream.append(builder.event("symlink", simulation.ROOT + "/d2/s"))
                gone = [item for item in read_paths if fs.lookup(item) is None]
                if gone and rng.random() < 0.01:
                    # 过期读取：这个路径早先在某个纪元里读过，后来被移走或删除，现在又「成功」读它——必须是问题，
                    # 而且不能因为「之前读过同一个路径」就被去重吞掉。
                    stale = rng.choice(sorted(gone))
                    stale_reads += 1
                    stream.append(builder.event("openat", stale, kind="read", flags=("O_RDONLY",), target=stale))
            stream = [translate(event) for event in stream]
            reference = runtime.OutputTrackerV4(self.contract)
            for event in stream:
                reference.observe_event(dict(event))
            parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
            for event in stream:
                parser.call_id, parser.call_errno = event["call"], event["errno"]
                parser.call_end = (event["role"], event["peer"]) if event["role"] else None
                parser.call_flags = frozenset(flag for flag in event["flags"] if flag.startswith("RENAME_"))
                parser._record_output_event(event["path"], event["kind"], event["name"], event["succeeded"],
                                            {flag for flag in event["flags"] if not flag.startswith("RENAME_")}, event["target"])
            kept = parser.output_tracker
            self.assertEqual(kept.report(), reference.report(), f"seed={seed}：去重改变了问题集合或覆盖路径")
            self.assertEqual(kept.names, reference.names, f"seed={seed}：去重改变了名字表")
            self.assertEqual(kept.entries, reference.entries)
            self.assertLessEqual(len(parser.output_events), len(stream))
            dropped_total += len(stream) - len(parser.output_events)
            # 去重的核心安全性质：只重放保留下来的事件，得到的结论必须与重放全量事件完全相同（问题集合、覆盖路径、名字表）。
            replay_only_kept = runtime.OutputTrackerV4(self.contract)
            for event in parser.output_events:
                replay_only_kept.observe_event(dict(event))
            self.assertEqual(replay_only_kept.report(), reference.report(), f"seed={seed}：只重放保留事件得到不同的结论")
            self.assertEqual(replay_only_kept.names, reference.names)
            runtime_part = {"output_events": parser.output_events, "outputs": kept.report()}
            if kept.report()["problems"]:
                problem_streams += 1
                with self.assertRaises(runtime.RuntimeContractError):
                    runtime._replay_events_v4(runtime_part, self.contract)
            else:
                replayed, _seen = runtime._replay_events_v4(runtime_part, self.contract)
                self.assertEqual(replayed.names, reference.names, f"seed={seed}：去重后的事件流重放出不同的名字表")
        self.assertGreater(dropped_total, 1000, "随机流必须真的产生可去重的重复事件，否则这个性质测试是空转")
        self.assertGreater(problem_streams, 3, "随机流里必须有带问题的流，才能证明去重不会吞掉问题")
        self.assertGreater(stale_reads, 10, "必须有足够多的过期读取，才能证明跨纪元的重复不会被误去重")

    def test_deduplicated_document_still_covers_every_path_and_kind(self) -> None:
        lines = self.prelude() + [self.read_line("d/a")] * 5 + [
            f'1 getdents64(3<{self.root}/d>, 0x1 /* 1 entries */, 32768) = 24'] * 5
        document = self.document(lines)
        paths = {(path, kind) for path, kinds in document["accesses"] if path.startswith(self.root) for kind in kinds}
        kept = {(e["path"], e["kind"]) for e in document["runtime"]["output_events"]}
        self.assertEqual(kept, paths, "每个 (路径, 访问类型) 至少保留一条事件，否则重放的路径集合比对会失败")
        self.assertAccepted(document)


class ReplayTests(AuditFixture):
    """合成 strace 行 → 过滤器 → 重放通过；篡改任一字段都被拒绝。"""

    def lines(self) -> list[str]:
        r = self.root
        return self.prelude() + [
            f'1 rename("{r}/d/a", "{r}/d/b") = 0',
            f'1 linkat(AT_FDCWD<{r}/d>, "b", AT_FDCWD<{r}/d>, "c", 0) = 0',
            f'1 renameat2(AT_FDCWD<{r}/d>, "c", AT_FDCWD<{r}/d>, "e", RENAME_NOREPLACE) = 0',
            f'1 mkdir("{r}/d", 0700) = -1 EEXIST (File exists)',
            self.read_line("d/b"), self.read_line("d/b"),
            f'1 unlink("{r}/d/e") = 0',
            f'1 openat(AT_FDCWD</repo>, "{r}/d/zzz", O_RDONLY) = -1 ENOENT (No such file or directory)',
            f'1 link("{r}/d/b", "{r}/d/b") = -1 EEXIST (File exists)',
            f'1 rename("{r}/d/nope", "{r}/d/other") = -1 ENOENT (No such file or directory)',
        ]

    def good(self) -> dict[str, Any]:
        document = self.document(self.lines())
        self.assertAccepted(document)
        return document

    def tampered(self, mutate: Any) -> dict[str, Any]:
        document = copy.deepcopy(self.good())
        mutate(document["runtime"]["output_events"])
        return document

    def index(self, events: list[dict[str, Any]], name: str, role: str | None = None) -> int:
        return next(i for i, e in enumerate(events) if e["name"] == name and e["role"] == role)

    def test_valid_v4_document_replays_and_returns_every_proven_path(self) -> None:
        document = self.good()
        covered = runtime.replay_runtime(document, [self.entry])
        self.assertTrue({f"{self.root}/d", f"{self.root}/d/a", f"{self.root}/d/b", f"{self.root}/d/e"} <= covered)
        self.assertEqual(document["runtime"]["outputs"]["problems"], [])
        self.assertEqual(document["coverage"]["unresolved"], 0)

    def test_tampering_call_pairing_is_rejected(self) -> None:
        def move_destination_call(events):
            events[self.index(events, "rename", "destination")]["call"] += 1000
        def pull_destination_call_back(events):
            index = self.index(events, "rename", "destination")
            events[index]["call"] = events[index - 1]["call"] - 1
        def merge_two_calls(events):
            first, second = self.index(events, "linkat", "source"), self.index(events, "renameat2", "source")
            events[second]["call"] = events[first]["call"]
        def give_a_stranger_the_same_call(events):
            events[self.index(events, "unlink")]["call"] = events[self.index(events, "rename", "source")]["call"]
        def reuse_old_call_number(events):
            events[-1]["call"] = events[0]["call"]
        for mutate in (move_destination_call, pull_destination_call_back, merge_two_calls, give_a_stranger_the_same_call,
                       reuse_old_call_number):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_tampering_role_is_rejected(self) -> None:
        def swap_roles(events):
            source, destination = self.index(events, "rename", "source"), self.index(events, "rename", "destination")
            events[source]["role"], events[destination]["role"] = "destination", "source"
        def drop_role(events):
            events[self.index(events, "linkat", "source")]["role"] = None
        def role_on_plain_call(events):
            events[self.index(events, "unlink")]["role"] = "source"
        def bogus_role(events):
            events[self.index(events, "rename", "source")]["role"] = "both"
        def both_destination(events):
            events[self.index(events, "renameat2", "source")]["role"] = "destination"
        for mutate in (swap_roles, drop_role, role_on_plain_call, bogus_role, both_destination):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_tampering_peer_is_rejected(self) -> None:
        def retarget_source_peer(events):
            events[self.index(events, "rename", "source")]["peer"] = f"{self.root}/d/elsewhere"
        def clear_destination_peer(events):
            events[self.index(events, "rename", "destination")]["peer"] = None
        def peer_outside(events):
            events[self.index(events, "linkat", "source")]["peer"] = "/host/x"
        def peer_on_plain_call(events):
            events[self.index(events, "unlink")]["peer"] = f"{self.root}/d/b"
        def relative_peer(events):
            events[self.index(events, "rename", "source")]["peer"] = "d/b"
        for mutate in (retarget_source_peer, clear_destination_peer, peer_outside, peer_on_plain_call, relative_peer):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_tampering_epoch_is_rejected(self) -> None:
        def bump_one(events):
            events[3]["epoch"] += 1
        def lower_one(events):
            events[-1]["epoch"] -= 1
        def reset_all(events):
            for event in events:
                event["epoch"] = 0
        def pre_increment_style(events):
            # 错把「事件自身改变状态后的纪元」写进去：每条状态改变的事件的纪元都多 1
            document_events = events
            for event in document_events:
                if event["name"] in {"mkdir", "rename", "unlink"} and event["succeeded"] and event["role"] != "destination":
                    event["epoch"] += 1
        def non_integer(events):
            events[2]["epoch"] = "2"
        def boolean(events):
            events[0]["epoch"] = False
        for mutate in (bump_one, lower_one, reset_all, pre_increment_style, non_integer, boolean):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_tampering_errno_and_success_are_rejected(self) -> None:
        def failed(events, name, role=None):
            return next(i for i, e in enumerate(events) if e["errno"] == "EEXIST" and e["name"] == name and e["role"] == role)
        def clear_errno_of_failure(events):
            events[failed(events, "mkdir")]["errno"] = None
        def errno_on_success(events):
            events[self.index(events, "unlink")]["errno"] = "EEXIST"
        def eexist_becomes_enoent(events):
            events[failed(events, "mkdir")]["errno"] = "ENOENT"
        def lowercase_errno(events):
            events[failed(events, "mkdir")]["errno"] = "eexist"
        def errno_of_one_end_only(events):
            events[failed(events, "link", "source")]["errno"] = "EACCES"
        def relabel_failure_as_missing(events):
            events[failed(events, "mkdir")]["kind"] = "missing"      # 躲开写类失败的错误码判定
        def flip_success(events):
            events[self.index(events, "unlink")]["succeeded"] = False
        def failure_hidden_as_success(events):
            index = failed(events, "mkdir")
            events[index]["succeeded"], events[index]["errno"] = True, None
        for mutate in (clear_errno_of_failure, errno_on_success, eexist_becomes_enoent, lowercase_errno, errno_of_one_end_only,
                       relabel_failure_as_missing, flip_success, failure_hidden_as_success):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_tampering_structure_flags_and_order_is_rejected(self) -> None:
        def drop_destination(events):
            del events[self.index(events, "rename", "destination")]
        def drop_source(events):
            del events[self.index(events, "linkat", "source")]
        def duplicate_with_same_call(events):
            events.insert(self.index(events, "unlink") + 1, copy.deepcopy(events[self.index(events, "unlink")]))
            events.insert(self.index(events, "unlink") + 1, copy.deepcopy(events[self.index(events, "unlink")]))
        def swap_neighbours(events):
            index = self.index(events, "linkat", "source")
            events[index], events[index + 2] = events[index + 2], events[index]
        def remove_key(events):
            del events[0]["peer"]
        def add_key(events):
            events[0]["extra"] = 1
        def exchange_flag_added(events):
            events[self.index(events, "renameat2", "source")]["flags"] = ["RENAME_EXCHANGE", "RENAME_NOREPLACE"]
            events[self.index(events, "renameat2", "destination")]["flags"] = ["RENAME_EXCHANGE", "RENAME_NOREPLACE"]
        def unknown_flag(events):
            events[0]["flags"] = ["FOO"]
        def duplicated_flags(events):
            events[self.index(events, "renameat2", "source")]["flags"] = ["RENAME_NOREPLACE", "RENAME_NOREPLACE"]
        def kind_changed(events):
            events[self.index(events, "unlink")]["kind"] = "read"
        def path_escapes_root(events):
            events[0]["path"] = f"{self.root}/../escape"
        for mutate in (drop_destination, drop_source, duplicate_with_same_call, swap_neighbours, remove_key, add_key,
                       exchange_flag_added, unknown_flag, duplicated_flags, kind_changed, path_escapes_root):
            with self.subTest(mutation=mutate.__name__):
                self.assertRejected(self.tampered(mutate), mutate.__name__)

    def test_outputs_summary_must_match_the_replayed_tracker(self) -> None:
        document = self.good()
        document["runtime"]["outputs"]["problems"] = ["伪造的问题"]
        self.assertRejected(document)
        document = self.good()
        document["runtime"]["outputs"]["paths"].append(f"{self.root}/never-seen")
        self.assertRejected(document)

    def test_a_forged_clean_summary_cannot_hide_a_real_problem(self) -> None:
        r = self.root
        document = self.document(self.prelude() + [f'1 symlink("{r}/d/a", "{r}/d/s") = 0', self.read_line("d/s")])
        self.assertTrue(document["runtime"]["outputs"]["problems"])
        self.assertFalse(self.audit(document)["coverage_complete"])
        document["runtime"]["outputs"]["problems"] = []
        self.assertRejected(document)

    def test_v4_document_carrying_control_credentials_is_rejected(self) -> None:
        context = self.control_context()
        document = self.completed(ra.filter_stream(self.prelude()[:1], ["/"], strict=True, runtime_context=self.context,
                                                   trace_contract="v4"))
        document["runtime"]["context"] = context
        with self.assertRaises(runtime.RuntimeContractError):
            runtime.replay_runtime(document, [context["contract_entry"]])

    def test_event_list_over_the_limit_is_rejected(self) -> None:
        document = self.good()
        template = document["runtime"]["output_events"][0]
        # WP-R2：v4 重放端的上限单列为 MAX_OUTPUT_EVENTS_V4；调小后验证“超限即拒绝”的机制（真实数值见 test_ci_read_audit_wp_r2）。
        with mock.patch.object(runtime, "MAX_OUTPUT_EVENTS_V4", 50):
            document["runtime"]["output_events"] = [dict(template, call=i + 1) for i in range(runtime.MAX_OUTPUT_EVENTS_V4 + 1)]
            with self.assertRaisesRegex(runtime.RuntimeContractError, "超过上限"):
                runtime.replay_runtime(document, [self.entry])


class CompatibilityTests(AuditFixture):
    """v1～v3 的常量与凭证按旧合同只读验真；v4 只由显式开关启用。"""

    def test_constants_and_default_switch(self) -> None:
        self.assertEqual((ra.TRACE_SCHEMA, ra.STRICT_TRACE_SCHEMA, ra.RUNTIME_TRACE_SCHEMA),
                         ("read-audit-trace/v1", "read-audit-trace/v2", "read-audit-trace/v3"))
        self.assertEqual(ra.RUNTIME_TRACE_SCHEMA_V4, "read-audit-trace/v4")
        self.assertEqual(ra.HOST_SCHEMA, "read-audit-host-snapshot/v2", "宿主快照版本本变更集不动")
        self.assertEqual(ra.TRACE_CONTRACTS, {"v3": ra.RUNTIME_TRACE_SCHEMA, "v4": ra.RUNTIME_TRACE_SCHEMA_V4})
        self.assertEqual((runtime.TRACE_CONTRACT_V3, runtime.TRACE_CONTRACT_V4), (ra.TRACE_CONTRACT_V3, ra.TRACE_CONTRACT_V4))
        self.assertEqual(runtime.TRACE_SCHEMA_V4, ra.RUNTIME_TRACE_SCHEMA_V4, "两个文件里各存的一份版本字符串必须一致")
        document = ra.filter_stream(self.prelude(), ["/"], strict=True, runtime_context=self.context)
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA, "不传开关就是 v3")
        self.assertTrue(all(set(event) == V3_EVENT_KEYS for event in document["runtime"]["output_events"]))
        parser = ra.TraceParser(strict=True, runtime_context=self.context)
        self.assertFalse(parser.trace_v4)
        # read_audit 按路径加载自己的 runtime 实例，类对象与本测试导入的不是同一个，所以按类名与接口判断。
        self.assertEqual(type(parser.output_tracker).__name__, "OutputTracker")
        self.assertFalse(hasattr(parser.output_tracker, "observe_event"))

    def test_v3_credential_still_verifies_under_the_old_contract(self) -> None:
        r = self.root
        lines = self.prelude() + [self.read_line("d/a"), f'1 unlink("{r}/d/a") = 0']
        document = self.document(lines, contract="v3")
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertAccepted(document)
        runtime.replay_runtime(document, [self.entry])
        # 同一份流在 v4 下的结果也通过，但文档形状不同：旧凭证不会被新规则重新解释。
        self.assertNotEqual(document["runtime"]["output_events"], self.document(lines)["runtime"]["output_events"])

    def test_v3_credential_is_still_refused_by_the_old_contract(self) -> None:
        r = self.root
        for line in (f'1 rename("{r}/d/a", "{r}/d/b") = 0', f'1 link("{r}/d/a", "{r}/d/b") = 0',
                     f'1 symlink("{r}/d/a", "{r}/d/s") = 0', f'1 mkdir("{r}/d", 0700) = -1 EEXIST (File exists)'):
            with self.subTest(line=line):
                document = self.document(self.prelude() + [line], contract="v3")
                self.assertTrue(document["runtime"]["outputs"]["problems"])
                self.assertFalse(self.audit(document)["coverage_complete"])
        # 同样的失败探测在 v4 下是合法的：差别只来自合同版本。
        accepted = self.document(self.prelude() + [f'1 mkdir("{r}/d", 0700) = -1 EEXIST (File exists)'])
        self.assertAccepted(accepted)

    def test_hand_built_v3_credential_keeps_the_six_key_event_format(self) -> None:
        events = [{"path": f"{self.root}/d", "kind": "write", "name": "mkdir", "succeeded": True, "flags": [], "target": None},
                  {"path": f"{self.root}/d/a", "kind": "write", "name": "openat", "succeeded": True,
                   "flags": ["O_CREAT", "O_WRONLY"], "target": f"{self.root}/d/a"},
                  {"path": f"{self.root}/d/a", "kind": "read", "name": "openat", "succeeded": True,
                   "flags": ["O_RDONLY"], "target": f"{self.root}/d/a"}]
        tracker = runtime.OutputTracker(self.contract)
        for event in events:
            tracker.observe(event["path"], event["kind"], event["name"], "|".join(event["flags"]), event["succeeded"], event["target"])
        accesses = {}
        for event in events:
            accesses.setdefault(event["path"], set()).add(event["kind"])
        document = {"schema_version": ra.RUNTIME_TRACE_SCHEMA, "roots": ["/"], "lines": 3,
                    "accesses": [[path, sorted(kinds)] for path, kinds in sorted(accesses.items())],
                    "samples": {path: path for path in accesses},
                    "coverage": {"all_host_paths": True, "unresolved": 0, "stream_complete": True, "unresolved_reasons": {},
                                 "unfinished_calls": 0, "unresolved_process_events": 0},
                    "runtime": {"context": self.context, "output_events": events, "outputs": tracker.report(), "terminal_events": []}}
        self.completed(document)
        self.assertAccepted(document)

    def test_schema_labels_cannot_be_swapped(self) -> None:
        r = self.root
        lines = self.prelude() + [f'1 rename("{r}/d/a", "{r}/d/b") = 0', self.read_line("d/b")]
        v4 = self.document(lines)
        self.assertAccepted(v4)
        as_v3 = copy.deepcopy(v4)
        as_v3["schema_version"] = ra.RUNTIME_TRACE_SCHEMA
        self.assertRejected(as_v3, "十一键事件冒充 v3")
        v3 = self.document(self.prelude() + [self.read_line("d/a")], contract="v3")
        self.assertAccepted(v3)
        as_v4 = copy.deepcopy(v3)
        as_v4["schema_version"] = ra.RUNTIME_TRACE_SCHEMA_V4
        self.assertRejected(as_v4, "六键事件冒充 v4")

    def test_strict_audit_reads_dispatches_by_schema_and_rejects_unknown_versions(self) -> None:
        for contract in ("v3", "v4"):
            with self.subTest(contract=contract):
                self.assertAccepted(self.document(self.prelude() + [self.read_line("d/a")], contract=contract))
        document = self.document(self.prelude())
        document["schema_version"] = "read-audit-trace/v5"
        self.assertFalse(self.audit(document)["coverage_complete"])

    def test_narrow_input_scope_replays_a_v4_trace_and_excludes_proven_private_paths(self) -> None:
        """收窄提案对 v4 轨迹同样重放：私有盘里已证明的路径不进宿主输入，运行合同原样带进提案；篡改轨迹则拒绝。"""
        import importlib.util
        from tools.ci import unit_records as records
        name = "_read_audit_unit_records"          # narrow_input_scope 按这个名字缓存自己加载的记录模块
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, ra.HERE.with_name("unit_records.py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        loaded = sys.modules[name]
        r = self.root
        document = self.document(self.prelude() + [f'1 rename("{r}/d/a", "{r}/d/b") = 0', self.read_line("d/b")])
        self.assertAccepted(document)
        store = loaded.RecordStore(self.base / "store")
        source = self.base / "unit.trace.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        trace_sha256 = records.file_sha256(source)
        trace_path = store.put_log(source, trace_sha256)
        spec = {"type": "command", "unit_id": "cmd:v4", "argv": ["true"], "cwd": "/repo", "env": {}, "cores": 1,
                "memory_mb": 128, "exclusive": False, "timeout_seconds": 60, "reservation_restartable": False}
        inputs = [self.entry]
        record = loaded.seal_record({
            "unit_id": "cmd:v4", "unit_type": "command", "kind": "formal", "run": {"run_id": "run-v4"}, "spec": spec,
            "spec_sha256": loaded.sha256_json(spec), "inputs": inputs, "inputs_sha256": loaded.entries_sha256(inputs),
            "environment": [], "environment_sha256": loaded.entries_sha256([]), "policy_sha256": "policy",
            "executor": {"sha256": "executor"}, "completed_at_utc": loaded.utc_now(), "passed": True, "inheritable": True,
            "exit_code": 0, "signal": None, "timed_out": False,
            "read_audit": {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1",
                           "undeclared_count": 0, "inputs_sha256": loaded.entries_sha256(inputs), "repo_root": "/repo",
                           "data_root": None, "trace": {"sha256": trace_sha256}}})
        record_path = store.put_record(record)
        # 记录库层面的完整复核（来源清单、日志摘要等）不是本测试的对象，单独替身；轨迹重放部分走真实实现。
        with mock.patch.object(loaded, "check_record", return_value=[]):
            proposal = ra.narrow_input_scope(record_path, trace_path)
            self.assertEqual(proposal["inputs"]["host_paths"], [], "私有盘里已证明的路径不能进宿主输入")
            self.assertEqual(proposal["inputs"]["runtime_contract"], self.contract)
            self.assertTrue(proposal["requires_validation"])
            self.assertFalse(proposal["reuse_enabled"])
            # 篡改轨迹里的事件（去掉 rename 的目的端）：重放拒绝，提案不能生成。
            broken = copy.deepcopy(document)
            events = broken["runtime"]["output_events"]
            events.pop(next(i for i, e in enumerate(events) if e["name"] == "rename" and e["role"] == "destination"))
            source.write_text(json.dumps(broken), encoding="utf-8")
            broken_sha256 = records.file_sha256(source)
            broken_trace = store.put_log(source, broken_sha256)
            broken_record = loaded.seal_record({**{k: v for k, v in record.items() if k != "record_sha256"},
                                                "read_audit": {**record["read_audit"], "trace": {"sha256": broken_sha256}}})
            broken_path = store.put_record(broken_record)
            with self.assertRaises(ra.AuditError):
                ra.narrow_input_scope(broken_path, broken_trace)

    def test_load_trace_accepts_every_known_version_and_refuses_unknown_ones(self) -> None:
        for version in (ra.TRACE_SCHEMA, ra.STRICT_TRACE_SCHEMA, ra.RUNTIME_TRACE_SCHEMA, ra.RUNTIME_TRACE_SCHEMA_V4):
            path = self.base / "trace.json"
            path.write_text(json.dumps({"schema_version": version}), encoding="utf-8")
            self.assertEqual(ra.load_trace(path)["schema_version"], version)
        path.write_text(json.dumps({"schema_version": "read-audit-trace/v9"}), encoding="utf-8")
        with self.assertRaises(ra.AuditError):
            ra.load_trace(path)

    def test_v4_switch_requires_strict_runtime_audit_and_a_known_version(self) -> None:
        with self.assertRaises(ra.AuditError):
            ra.TraceParser(strict=True, trace_contract="v4")
        with self.assertRaises(ra.AuditError):
            ra.TraceParser(strict=False, runtime_context=None, trace_contract="v4")
        with self.assertRaises(ra.AuditError):
            ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v5")
        with self.assertRaises(ra.AuditError):
            ra.strace_argv(["true"], output=Path("/out/t.json"), roots=["/"], strict=True, trace_contract="v4")
        with self.assertRaises(ra.AuditError):
            ra.strace_argv(["true"], output=Path("/out/t.json"), roots=["/"], strict=True,
                           runtime_context=Path("/out/c.json"), trace_contract="v7")


class ControlChainUnaffectedTests(AuditFixture):
    """control 四链的 sidecar 原样复用 read_audit.py：不传开关，生产并重放的仍是 v3，字节行为不变。"""

    def control_lines(self) -> list[str]:
        r = self.root
        return [f'1 mkdir("{r}/d", 0700) = 0', self.create_line("d/a"), f'1 rename("{r}/d/a", "{r}/d/b") = 0', self.read_line("d/b")]

    def control_document(self, lines: list[str]) -> dict[str, Any]:
        document = control_audit.filter_stream(lines, ["/"], strict=True, runtime_context=self.control_context())
        return self.completed(document, control_isolation_unchanged=True)

    def control_audit_of(self, document: dict[str, Any]) -> dict[str, Any]:
        entry = (document["runtime"]["context"] or {})["contract_entry"]
        return control_audit.strict_audit_reads(document, [entry], repo_root="/repo", data_root=None)

    def test_control_entry_still_produces_and_replays_v3(self) -> None:
        document = self.control_document(self.control_lines())
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA, "control 链产出 v3")
        self.assertTrue(all(set(event) == V3_EVENT_KEYS for event in document["runtime"]["output_events"]),
                        "control 链的输出事件仍是六键，没有 errno／call／role／peer／epoch")
        report = self.control_audit_of(document)
        self.assertTrue(report["coverage_complete"], (document["runtime"]["outputs"]["problems"], report["undeclared"]))
        # 篡改 control 事件（去掉 rename 的目的端）仍被 control 的重放拒绝：沿用旧合同的拒绝边界。
        broken = copy.deepcopy(document)
        events = broken["runtime"]["output_events"]
        events.pop(next(i for i, e in enumerate(events) if e["name"] == "rename" and e["path"].endswith("/d/b")))
        self.assertFalse(self.control_audit_of(broken)["coverage_complete"])

    def test_control_parser_has_no_v4_state_switched_on(self) -> None:
        parser = control_audit.TraceParser(strict=True, runtime_context=self.control_context())
        self.assertFalse(parser.trace_v4)
        # control 适配器按路径加载自己的 runtime 实例，类对象与本测试导入的不是同一个，所以按类名与接口判断。
        self.assertEqual(type(parser.output_tracker).__name__, "OutputTracker")
        self.assertTrue(hasattr(parser.output_tracker, "observe"))
        self.assertFalse(hasattr(parser.output_tracker, "observe_event"))

    def test_control_adapter_with_an_ordinary_context_still_defaults_to_v3(self) -> None:
        """四 control 适配器下跑的普通单元（上下文没有 control_chain）：过滤器委派给历史实现，默认仍是 v3。"""
        document = self.completed(control_audit.filter_stream(self.prelude(), ["/"], strict=True, runtime_context=self.context))
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
        self.assertTrue(all(set(event) == V3_EVENT_KEYS for event in document["runtime"]["output_events"]))
        self.assertTrue(self.control_audit_of(document)["coverage_complete"])

    def test_control_adapter_can_still_verify_an_ordinary_v4_document(self) -> None:
        """适配器对没有 control_chain 的文档委派给历史实现，历史实现按文档版本分派，所以 v4 普通文档也能验真。"""
        document = self.document(self.prelude() + [self.read_line("d/a")])
        self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA_V4)
        self.assertTrue(self.control_audit_of(document)["coverage_complete"])

    def test_relabelling_a_control_document_as_v4_is_refused_by_both_verifiers(self) -> None:
        document = self.control_document(self.control_lines())
        relabelled = copy.deepcopy(document)
        relabelled["schema_version"] = ra.RUNTIME_TRACE_SCHEMA_V4
        self.assertFalse(self.control_audit_of(relabelled)["coverage_complete"], "control 验证器只认 v3")
        entry = relabelled["runtime"]["context"]["contract_entry"]
        self.assertFalse(ra.strict_audit_reads(relabelled, [entry], repo_root="/repo", data_root=None)["coverage_complete"],
                         "历史验证器遇到带 control 凭证的 v4 文档也拒绝")

    def test_control_strace_command_has_no_trace_contract_flag(self) -> None:
        for context in (self.context, self.control_context()):
            path = self.base / "context.json"
            path.write_text(json.dumps(context), encoding="utf-8")
            argv = control_audit.strace_argv(["true"], output=self.base / "t.json", roots=["/"], strict=True, runtime_context=path)
            pipe = argv[argv.index("-o") + 1]
            self.assertNotIn("trace-contract", pipe)
            self.assertIn(f"--runtime-context {path}", pipe)

    def test_control_runtime_copies_do_not_carry_the_v4_enabling_point(self) -> None:
        """v4 的启用点只在历史 runtime 的 prepare／run_child；control 自己的两个函数不能出现开关。"""
        for function in (control_runtime.run_child, control_runtime.prepare):
            self.assertNotIn("trace_contract", inspect.getsource(function), function.__name__)
        self.assertIsNot(control_runtime.run_child, runtime.run_child)
        self.assertIsNot(control_runtime.OutputTracker, runtime.OutputTracker)
        # control 子类的基类是按路径加载的另一份历史 runtime 实例：代码必须与本模块里保持原样的 v3 跟踪器逐字相同。
        self.assertEqual(inspect.getsource(control_runtime._legacy.OutputTracker), inspect.getsource(runtime.OutputTracker))
        self.assertTrue(issubclass(control_runtime.OutputTracker, control_runtime._legacy.OutputTracker))

    @staticmethod
    def free_names(path: Path) -> tuple[set[str], set[str]]:
        """control sidecar 里被读取、却不是本模块自己定义的全局名字（只能来自 globals().update 复用的历史模块），
        以及显式写成 ``_legacy.<名字>`` 的引用。用 AST 静态分析，sidecar 以后多复用一个名字，这个检查自动跟上。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = set(dir(builtins))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                arguments = node.args
                defined.update(arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                                                   arguments.vararg, arguments.kwarg] if arg is not None)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                defined.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Name):
                (defined if isinstance(node.ctx, (ast.Store, ast.Del)) else used).add(node.id)
        explicit = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == "_legacy"}
        return used - defined - {"__file__"}, explicit

    def test_every_historical_name_the_control_sidecars_reuse_still_exists_with_its_old_shape(self) -> None:
        """control 的 sidecar 经 globals().update(vars(_legacy)) 与 _legacy.<名字> 复用历史模块；
        新增 v4 不能删除、改名或改变其中任何一个名字（这里静态找出全部复用点，逐个核对仍然存在）。"""
        for sidecar, historical in (("control_read_audit.py", ra), ("control_read_audit_runtime.py", runtime)):
            free, explicit = self.free_names(ra.HERE.with_name(sidecar))
            self.assertGreater(len(free), 20, sidecar)
            missing = sorted(name for name in free | explicit if not hasattr(historical, name))
            self.assertEqual(missing, [], f"{sidecar} 复用的名字在历史模块里缺失")
        # 复用点中带调用约定的几个：签名不能变。控制子类按 (name, body, succeeded) 三元组解包 parser.event，
        # 并以 _add(path, kind, line) 的签名被基类调用；runtime 的 replay_runtime 与旧 observe 接口同样保持。
        self.assertEqual(list(inspect.signature(runtime.replay_runtime).parameters), ["document", "inputs", "historical"])
        self.assertEqual(list(inspect.signature(runtime.OutputTracker.observe).parameters),
                         ["self", "path", "kind", "name", "body", "succeeded", "target"])
        self.assertEqual(len(ra.TraceParser(strict=True).event), 3)
        self.assertEqual(list(inspect.signature(ra.TraceParser._add).parameters), ["self", "path", "kind", "line"])
        self.assertEqual(list(inspect.signature(ra.TraceParser.__init__).parameters)[:3], ["self", "strict", "runtime_context"])
        self.assertEqual(list(inspect.signature(runtime.prepare).parameters)[:6],
                         ["contract", "output", "argv", "cwd", "inputs", "test_spec"])


class WiringTests(AuditFixture):
    """strace 命令行、filter 子命令、prepare 写入请求、run_child 把开关交给过滤器。"""

    def test_strace_argv_default_is_unchanged_and_v4_adds_one_flag(self) -> None:
        context = Path("/out/context.json")
        default = ra.strace_argv(["true"], output=Path("/out/t.json"), roots=["/"], strict=True, runtime_context=context)
        explicit_v3 = ra.strace_argv(["true"], output=Path("/out/t.json"), roots=["/"], strict=True, runtime_context=context,
                                     trace_contract="v3")
        v4 = ra.strace_argv(["true"], output=Path("/out/t.json"), roots=["/"], strict=True, runtime_context=context,
                            trace_contract="v4")
        self.assertEqual(default, explicit_v3)
        pipe = default[default.index("-o") + 1]
        self.assertTrue(pipe.endswith("filter --output /out/t.json --strict --runtime-context /out/context.json --root /"), pipe)
        self.assertNotIn("trace-contract", pipe)
        v4_pipe = v4[v4.index("-o") + 1]
        self.assertTrue(v4_pipe.endswith("filter --output /out/t.json --strict --runtime-context /out/context.json "
                                         "--trace-contract v4 --root /"), v4_pipe)
        self.assertEqual([item for item in v4 if item != v4_pipe], [item for item in default if item != pipe])

    def test_filter_subcommand_produces_v4_only_when_asked(self) -> None:
        context = self.base / "context.json"
        context.write_text(json.dumps(self.context), encoding="utf-8")
        stream = "\n".join(self.prelude() + [f'1 rename("{self.root}/d/a", "{self.root}/d/b") = 0', self.read_line("d/b")]) + "\n"
        for flag, schema in ((["--trace-contract", "v4"], ra.RUNTIME_TRACE_SCHEMA_V4), ([], ra.RUNTIME_TRACE_SCHEMA)):
            with self.subTest(flag=flag):
                output = self.base / f"out-{schema.rsplit('/', 1)[-1]}.json"
                completed = subprocess.run([sys.executable, "-B", str(ra.HERE), "filter", "--output", str(output), "--strict",
                                            "--runtime-context", str(context), *flag, "--root", "/"],
                                           input=stream.encode(), capture_output=True, timeout=120)
                self.assertEqual(completed.returncode, 0, completed.stderr.decode())
                document = ra.load_trace(output)
                self.assertEqual(document["schema_version"], schema)
                self.completed(document)
                self.assertEqual(self.audit(document)["coverage_complete"], schema == ra.RUNTIME_TRACE_SCHEMA_V4)

    def test_filter_subcommand_refuses_v4_without_runtime_context(self) -> None:
        output = self.base / "refused.json"
        completed = subprocess.run([sys.executable, "-B", str(ra.HERE), "filter", "--output", str(output), "--strict",
                                    "--trace-contract", "v4", "--root", "/"], input=b"", capture_output=True, timeout=120)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(output.exists())

    # ------------------------------------------------------------------ prepare／run_child（用替身模拟 Linux 的挂载与 strace）

    def plain_contract(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """没有遮挡根的合同：run_child 会列出遮挡根目录，本机没有 /root/oauth-capture，所以接线测试用无遮挡合同。"""
        contract = {**self.contract, "mask_roots": []}
        return contract, runtime.contract_entry(contract)

    def make_request(self, **overrides: Any) -> tuple[Path, str, dict[str, Any]]:
        """用真实 prepare 生成受管请求（替身只负责平台判定与命名空间读取），返回请求路径、摘要与内容。"""
        contract, entry = self.plain_contract()
        output = self.base / "audit" / "unit.trace.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(runtime.sys, "platform", "linux"), \
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/unshare"), \
                mock.patch.object(runtime, "contract_entry", return_value=entry), \
                mock.patch.object(runtime.os, "readlink", return_value="mnt:[100]"):
            command = runtime.prepare(contract, output=output, argv=["true"], cwd=str(self.base), inputs=[entry], **overrides)
        request_path = Path(command[command.index("--request") + 1])
        digest = command[command.index("--request-sha256") + 1]
        self.assertTrue(Path(self.root).is_dir(), "prepare 建立了专用空挂载点")
        return request_path, digest, json.loads(request_path.read_text())

    def test_prepare_writes_v4_into_the_request_by_default_and_v3_requests_stay_byte_identical(self) -> None:
        _, digest, request = self.make_request()
        self.assertEqual(request["trace_contract"], "v4")
        self.assertEqual(digest, runtime.digest(request), "请求摘要覆盖开关，被测命令改不了")
        _, v3_digest, v3_request = self.make_request(trace_contract="v3")
        self.assertNotIn("trace_contract", v3_request, "显式 v3 的请求与改动前逐字节相同：不带该键")
        self.assertEqual(set(v3_request), {"contract_entry", "parent_namespace", "output", "argv", "cwd", "host_entries", "host_paths"})
        self.assertEqual(v3_digest, runtime.digest(v3_request))
        with self.assertRaises(runtime.RuntimeContractError):
            self.make_request(trace_contract="v9")

    def run_child_with(self, request_path: Path, digest: str) -> tuple[int, list[list[str]]]:
        """运行真实 run_child：挂载用的 strace 与被测命令的 strace 都换成替身，返回退出码与被测命令那次调用的 argv。"""
        _, entry = self.plain_contract()
        calls: list[list[str]] = []
        mount_lines = {
            0: '1 mount("none", "/", NULL, MS_REC|MS_PRIVATE, NULL) = 0\n',
            1: f'2 mount("tmpfs", "{self.root}", "tmpfs", MS_NOSUID|MS_NODEV, "size=512m,mode=0700") = 0\n'}

        def fake_run(argv, **kwargs):
            output = argv[argv.index("-o") + 1]
            if output.startswith("|"):
                calls.append(list(argv))     # 被测命令那一次：strace_argv 构造的命令行，输出走管道交给过滤器
            else:
                Path(output).write_text(mount_lines[int(output.rsplit("-", 1)[-1])], encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0)

        visible = [{"mount_id": 201, "target": self.root, "filesystem": "tmpfs", "options": ["rw", "nosuid", "nodev"]}]
        with mock.patch.object(runtime.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(runtime, "_mount_rows", return_value=[]), \
                mock.patch.object(runtime, "_visible_mounts", return_value=visible), \
                mock.patch.object(runtime.os, "readlink", return_value="mnt:[101]"), \
                mock.patch.object(runtime, "contract_entry", return_value=entry):
            code = runtime.run_child(request_path, digest)
        return code, calls

    def test_run_child_hands_the_switch_to_the_filter_only_for_v4_requests(self) -> None:
        request_path, digest, request = self.make_request()
        code, calls = self.run_child_with(request_path, digest)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        pipe = calls[0][calls[0].index("-o") + 1]
        self.assertIn("--runtime-context", pipe)
        self.assertIn("--trace-contract v4", pipe)
        self.assertEqual(calls[0][calls[0].index("--") + 1:], request["argv"])
        # 去掉开关键的请求（等价于历史 v3 请求）：命令行与改动前完全相同。
        legacy = {key: value for key, value in request.items() if key != "trace_contract"}
        legacy_path = request_path.with_name("legacy.request.json")
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        code, calls = self.run_child_with(legacy_path, runtime.digest(legacy))
        self.assertEqual(code, 0)
        self.assertNotIn("trace-contract", calls[0][calls[0].index("-o") + 1])
        # 请求里写了未知版本：拒绝，不启动被测命令。
        bogus = {**request, "trace_contract": "v9"}
        bogus_path = request_path.with_name("bogus.request.json")
        bogus_path.write_text(json.dumps(bogus), encoding="utf-8")
        with self.assertRaises(runtime.RuntimeContractError):
            self.run_child_with(bogus_path, runtime.digest(bogus))

    def test_control_prepare_for_ordinary_commands_delegates_and_control_scenarios_keep_their_own_request(self) -> None:
        argv = ["true"]
        output = self.base / "audit" / "control.trace.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        scenario = [sys.executable, "-m", control_runtime.CONTROL_MODULE, "run-scenario", "--client", "claude", "--name",
                    "vc-chain.claude-reconcile", "--staging-parent", self.root + "/staging"]
        contract, entry = self.plain_contract()
        # control 适配器按路径加载自己的一份历史 runtime（control_runtime._legacy），普通命令委派给它的 prepare。
        with mock.patch.object(runtime.sys, "platform", "linux"), \
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/unshare"), \
                mock.patch.object(control_runtime._legacy, "contract_entry", return_value=entry), \
                mock.patch.object(control_runtime, "contract_entry", return_value=entry), \
                mock.patch.object(runtime.os, "readlink", return_value="mnt:[100]"):
            ordinary = control_runtime.prepare(contract, output=output, argv=argv, cwd=str(self.base), inputs=[entry])
            ordinary_request = json.loads(Path(ordinary[ordinary.index("--request") + 1]).read_text())
            controlled = control_runtime.prepare(contract, output=output, argv=scenario, cwd=str(self.base), inputs=[entry])
            controlled_request = json.loads(Path(controlled[controlled.index("--request") + 1]).read_text())
        # 普通命令经 control 适配器委派给历史 prepare，由历史 prepare 的 v4 启用点决定；control 场景走自己的 prepare，没有开关键。
        self.assertEqual(ordinary_request.get("trace_contract"), "v4")
        self.assertEqual(Path(ordinary[ordinary.index("--request") - 1]), runtime.HERE, "普通命令仍由历史 runtime 子进程执行")
        self.assertNotIn("trace_contract", controlled_request)
        self.assertEqual(Path(controlled[controlled.index("--request") - 1]), control_runtime.HERE)
        self.assertEqual(controlled_request["control_scenario"], "vc-chain.claude-reconcile")


if __name__ == "__main__":
    unittest.main()
