"""v4 整条流水线的随机对拍：随机合法历史 → 渲染成 strace 6.8 行 → 解析器 → 跟踪器 → 去重 → 重放。

跟踪器测试已经用独立实现的「真实文件系统」验证了名字表语义；解析器测试逐个核对了行格式。这里把两头接起来：
同一份随机历史既是真实文件系统的操作序列，也被渲染成多种写法的 strace 行（绝对路径、带目录描述符标注的相对路径、
进程工作目录下的相对路径、rename／renameat／renameat2、link／linkat、unfinished／resumed 拆行并夹杂别的进程的调用），
经 ``TraceParser`` 产出 v4 文档后必须满足：

* 没有未解析调用、没有任何归属问题，重放通过；
* 解析器里跟踪器最终的名字表、对象身份与子项计数，与真实文件系统逐项一致；
* 同一份行流按 v3 合同解析，结论与改动前一致（rename／link 被旧合同拒绝），说明开关只影响 v4。
"""

from __future__ import annotations

import collections
import random
from typing import Any

from tools.ci import read_audit as ra
from tools.official_client_capture.tests import test_ci_read_audit_v4_tracker as simulation
from tools.official_client_capture.tests.test_ci_read_audit_v4_trace import AuditFixture

ERRNO_TEXT = {"EEXIST": "File exists", "ENOENT": "No such file or directory", "ENOTDIR": "Not a directory"}


class RandomPipelineTests(AuditFixture):
    """随机历史走完整条流水线。"""

    def render(self, events: list[dict[str, Any]], rng: random.Random) -> list[str]:
        """把模拟器产出的一个调用的事件渲染成 strace 行；同一调用的重复事件（请求路径与返回描述符）只渲染一行。"""
        lines: list[str] = []
        index = 0
        while index < len(events):
            event = events[index]
            index += 1
            if event["role"] == "source":
                partner = events[index]
                index += 1
                lines.append(self.render_pair(event, partner, rng))
                continue
            if (index < len(events) and events[index]["name"] == event["name"] and events[index]["path"] == event["path"]
                    and events[index]["flags"] == event["flags"] and events[index]["succeeded"] == event["succeeded"]):
                index += 1                              # create 在模拟器里是两条相同事件，真实调用只有一行
            lines.append(self.render_single(event, rng))
        return lines

    def real(self, path: str) -> str:
        return path.replace(simulation.ROOT, self.root)

    def result(self, event: dict[str, Any], success: str) -> str:
        if event["succeeded"]:
            return success
        return f"-1 {event['errno']} ({ERRNO_TEXT[event['errno']]})"

    def location(self, path: str, rng: random.Random, plain_ok: bool) -> tuple[str, str]:
        """路径的三种写法：(目录描述符标注, 参数里的字符串)。

        * 绝对路径：标注是进程真实的工作目录 ``AT_FDCWD<私有根>``（strace 的 -y 对 AT_FDCWD 打印的就是当前工作目录，
          解析器也据此还原进程的工作目录，所以标注不能乱写）；
        * 真实的目录描述符加相对名字：``5<父目录>`` 加叶子名；
        * 进程已 chdir 到私有根时，不带描述符的调用（mkdir、unlink、rmdir）可以直接用相对私有根的路径，标注返回空串。
        """
        parent, _, leaf = path.rpartition("/")
        choice = rng.random()
        if choice < 0.4:
            return self.cwd_annotation, path
        if choice < 0.8 or not plain_ok:
            return f"{rng.choice([3, 4, 5, 6])}<{parent}>", leaf
        return "", path[len(self.root) + 1:]

    @property
    def cwd_annotation(self) -> str:
        return f"AT_FDCWD<{self.root}>"

    def render_single(self, event: dict[str, Any], rng: random.Random) -> str:
        path = self.real(event["path"])
        name = event["name"]
        dirfd, argument = self.location(path, rng, plain_ok=name in {"mkdir", "unlink", "rmdir"} and path != self.root)
        anchor = dirfd or self.cwd_annotation
        if name in {"mkdir", "mkdirat"}:
            call = f'mkdir("{argument}", 0700)' if not dirfd else f'mkdirat({dirfd}, "{argument}", 0700)'
            return f"1 {call} = {self.result(event, '0')}"
        if name == "openat" and event["kind"] == "read":
            return f'1 openat({anchor}, "{argument}", O_RDONLY) = 3<{path}>'
        if name == "openat" and "O_CREAT" in event["flags"]:
            tail = f"3<{path}>" if event["succeeded"] else self.result(event, "")
            return f'1 openat({anchor}, "{argument}", {"|".join(event["flags"])}, 0600) = {tail}'
        if name == "newfstatat" and event["kind"] == "stat":
            return f'1 newfstatat({anchor}, "{argument}", {{st_mode=S_IFREG|0644}}, 0) = 0'
        if name == "newfstatat":                                     # 缺失探测
            return f'1 newfstatat({anchor}, "{argument}", 0x1, 0) = {self.result(event, "0")}'
        if name == "unlink":
            call = f'unlink("{argument}")' if not dirfd else f'unlinkat({dirfd}, "{argument}", 0)'
            return f"1 {call} = {self.result(event, '0')}"
        if name == "rmdir":
            call = f'rmdir("{argument}")' if not dirfd else f'unlinkat({dirfd}, "{argument}", AT_REMOVEDIR)'
            return f"1 {call} = {self.result(event, '0')}"
        if name == "utimensat":
            return f'1 utimensat({anchor}, "{argument}", NULL, 0) = 0'
        raise AssertionError(f"渲染器不认识这个事件：{event}")

    def render_pair(self, source: dict[str, Any], destination: dict[str, Any], rng: random.Random) -> str:
        first, second = self.real(source["path"]), self.real(destination["path"])
        name = source["name"]
        left_fd, left = self.location(first, rng, plain_ok=False)
        right_fd, right = self.location(second, rng, plain_ok=False)
        result = self.result(source, "0")
        if name == "link" and rng.random() < 0.5:
            name = "linkat"
        if name in {"rename", "link"}:
            return f'1 {name}("{first}", "{second}") = {result}'
        if name == "renameat":
            return f'1 renameat({left_fd}, "{left}", {right_fd}, "{right}") = {result}'
        if name == "renameat2":
            flags = "|".join(source["flags"]) or "0"
            return f'1 renameat2({left_fd}, "{left}", {right_fd}, "{right}", {flags}) = {result}'
        if name == "linkat":
            return f'1 linkat({left_fd}, "{left}", {right_fd}, "{right}", 0) = {result}'
        raise AssertionError(f"渲染器不认识这个两路径调用：{name}")

    def interleave(self, lines: list[str], rng: random.Random) -> list[str]:
        """约两成的调用拆成 unfinished／resumed 两行，中间夹一个别的进程对「永远不存在的路径」的探测（不改变任何状态）。"""
        result: list[str] = []
        never = f"{self.root}/zz-never-created"
        for line in lines:
            if rng.random() < 0.2 and ") = " in line and "chdir" not in line:
                head, _, tail = line.rpartition(") = ")
                call = line.split(" ", 1)[1].split("(", 1)[0]
                result += [f"{head} <unfinished ...>", f'2 newfstatat(AT_FDCWD</repo>, "{never}", 0x1, 0) = -1 ENOENT (No such file or directory)',
                           f"1 <... {call} resumed>) = {tail}"]
            else:
                result.append(line)
        return result

    def history(self, seed: int, steps: int) -> tuple[list[str], simulation._SimFs]:
        rng = random.Random(seed)
        fs, builder, statistics = simulation._SimFs(simulation.ROOT), simulation.Builder(), collections.Counter()
        helper = simulation.TrackerSimulationTests("test_random_valid_histories_match_the_real_filesystem_semantics")
        lines = [f'1 chdir("{self.root}") = 0']
        for _ in range(steps):
            events = helper.apply_random_operation(rng, fs, builder, statistics)
            if events:
                lines += self.render(events, rng)
        return self.interleave(lines, rng), fs

    def test_random_histories_pass_the_whole_v4_pipeline_and_match_the_filesystem(self) -> None:
        checked = successful_pairs = failed_pairs = 0
        for seed in range(12):
            lines, fs = self.history(500 + seed, 160)
            parser = ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")
            for line in lines:
                parser.feed(line + "\n")
            document = self.completed(ra.trace_document(parser, ["/"]))
            tracker = parser.output_tracker
            context = f"seed={500 + seed} 行数={len(lines)}"
            self.assertEqual(document["coverage"]["unresolved"], 0, (document["coverage"]["unresolved_reasons"], context))
            self.assertEqual(tracker.report()["problems"], [], context)
            self.assertAccepted(document)
            # 解析器里的跟踪器与真实文件系统一致（名字、类型、对象身份分组、子项计数）。
            expected = {path.replace(simulation.ROOT, self.root): ident for path, ident in fs.snapshot().items()}
            kinds = {path.replace(simulation.ROOT, self.root): kind for path, kind in fs.kinds().items()}
            self.assertEqual(set(tracker.names), set(expected), context)
            groups_real: dict[int, set[str]] = collections.defaultdict(set)
            groups_tracked: dict[int, set[str]] = collections.defaultdict(set)
            for path, ident in expected.items():
                groups_real[ident].add(path)
                groups_tracked[tracker.names[path]].add(path)
                self.assertEqual(tracker.kinds[tracker.names[path]], kinds[path], f"{path} {context}")
            self.assertEqual({frozenset(group) for group in groups_real.values()},
                             {frozenset(group) for group in groups_tracked.values()}, context)
            checked += len(lines)
            pairs = [event for event in parser.output_events if event["role"] == "source"]
            successful_pairs += sum(1 for event in pairs if event["succeeded"])
            failed_pairs += sum(1 for event in pairs if not event["succeeded"])
        self.assertGreater(checked, 1500, "随机流水线测试必须真的跑过足够多的调用")
        self.assertGreater(successful_pairs, 150, "成功的 rename／link 要足够多，否则流水线对拍是空转")
        self.assertGreater(failed_pairs, 100, "失败的 rename／link（ENOENT／EEXIST）要足够多")

    def test_the_same_random_streams_under_the_old_contract_still_reject_what_v3_never_supported(self) -> None:
        """同样的行流按 v3 合同解析：解析本身不受开关影响（没有未解析调用），但 rename／link 与失败探测仍被旧合同拒绝。"""
        rejected = 0
        for seed in range(4):
            lines, _fs = self.history(900 + seed, 160)
            document = self.completed(ra.filter_stream([line + "\n" for line in lines], ["/"], strict=True,
                                                       runtime_context=self.context))
            self.assertEqual(document["schema_version"], ra.RUNTIME_TRACE_SCHEMA)
            self.assertEqual(document["coverage"]["unresolved"], 0)
            self.assertTrue(all(len(event) == 6 for event in document["runtime"]["output_events"]))
            if any(line.split("(", 1)[0].split(" ", 1)[-1] in {"rename", "renameat", "renameat2", "link", "linkat"} for line in lines):
                rejected += 1
                self.assertTrue(document["runtime"]["outputs"]["problems"])
                self.assertFalse(self.audit(document)["coverage_complete"])
        self.assertGreater(rejected, 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
