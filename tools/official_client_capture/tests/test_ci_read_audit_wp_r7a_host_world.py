"""含宿主目录的端到端对拍（WP-R7a，第三轮验收“测试盲区”）。

背景：第三轮独立验收指出，作者的差分对拍（``test_ci_read_audit_wp_r3.DifferentialOracleTests``）在**事件层**工作，它的模拟器里只有私有盘内的
文件系统——造不出“解析器侧的路径写法”问题（S-1：``link/x/..``）和“文本工作目录过期”问题（S-2：目录被改名后用 plain 系统调用走相对路径），
因为这两类都发生在“strace 行 → 规范路径”那一步，而且需要宿主目录才能判断“内核到底碰没碰宿主”。本模块补上：

* ``HostWorld``：独立于被测代码的“内核语义”模型。一棵带符号链接的目录树，私有盘 ``root`` 之内是被测单元的世界，``/etc``、``/etc/ssh``、
  ``/tmp``、``/repo`` 是宿主。路径解析按 Linux 规则：中间分量是链接必跟随，末分量是链接时按调用是否跟随，末尾斜杠迫使跟随，``.``／``..``，
  40 次链接上限。每次解析都记录“是否触碰宿主”：在私有盘之外的目录里做了查找（通往私有盘的祖先链上的必经查找除外），或最终对象在私有盘之外。
* ``Generator``：随机序列。先建一个确定的前缀（目录、文件、指向宿主的链接，一半的序列再来一个“chdir 进目录、在里面建链接、把目录改名”的 S-2 场景），
  再逐步提议随机操作（mkdir／symlink／lstat／stat／open／unlink／rmdir／rename／readlink／chdir／access／execve／truncate，at 类调用的 dirfd 有
  AT_FDCWD 与真实目录描述符两种，plain 类调用走相对路径），把每个操作渲染成 strace 行，并给出模型对“是否触碰宿主”的判断。
* **逐步接受**：每个提议先在状态的深拷贝上喂给解析器，只有审计仍然干净（没有新的未解析原因、没有新的归属问题、没有新的盘外访问）才接受；
  被拒绝的提议丢弃。这样序列能一直走到很深的状态，不会被第一个合法的拒绝打断（直接随机拼接时 99% 的序列在前几步就失败，等于没测）。
* **判定**：整套严格审计通过，但模型说某一步触碰了宿主 → 违例（该拒的被放行了）。

``HostDifferentialTests.test_the_differential_has_teeth`` 证明对拍有牙：把解析器的路径形态检查、工作目录改写、两路径先行检查，或跟踪器的中间分量检查、
链接名字判定改宽，对拍都会在有限个种子内报出违例。
"""

from __future__ import annotations

import copy
import random
from typing import Any
from unittest import mock

from tools.ci import read_audit as ra
from tools.official_client_capture.tests.test_ci_read_audit_wp_r7a import R7aFixture, _hazard_before_wp_r7a

ERRNO_TEXT = {"ENOENT": "No such file or directory", "ENOTDIR": "Not a directory", "EEXIST": "File exists", "EISDIR": "Is a directory",
              "EINVAL": "Invalid argument", "ELOOP": "Too many levels of symbolic links", "EBUSY": "Device or resource busy",
              "ENOTEMPTY": "Directory not empty", "EACCES": "Permission denied"}
NAMES = ["d", "e", "ln", "ln2", "f", "ssh", "passwd", "config", "x", "y"]


def fmt_err(code: str) -> str:
    return f"-1 {code} ({ERRNO_TEXT[code]})"


class Node:
    """目录树节点：dir／file／symlink。``text`` 只给符号链接（链接文本）。"""

    __slots__ = ("kind", "children", "text", "parent", "name", "ino")
    counter = 0

    def __init__(self, kind: str, text: str | None = None) -> None:
        self.kind, self.text = kind, text
        self.children: dict[str, Node] | None = {} if kind == "dir" else None
        self.parent: Node | None = None
        self.name = ""
        Node.counter += 1
        self.ino = Node.counter


class Res:
    """一次路径解析的结果：错误码、父目录、末分量名、末分量对象、是否触碰宿主、末分量的写法（norm／dot／dotdot／dir）。"""

    __slots__ = ("error", "parent", "name", "node", "touched", "last")

    def __init__(self, error: str | None = None, parent: Node | None = None, name: str | None = None, node: Node | None = None,
                 touched: bool = False, last: str | None = None) -> None:
        self.error, self.parent, self.name, self.node, self.touched, self.last = error, parent, name, node, touched, last


class HostWorld:
    """见模块说明。注意 ``chain`` 存的是节点对象而不是 ``id()``：逐步接受要深拷贝整个世界，节点会换成新对象。"""

    def __init__(self, root: str) -> None:
        Node.counter = 0
        self.top = Node("dir")
        self.top.parent = self.top
        self.root = self.mk(root)
        for path in ("/etc", "/etc/ssh", "/tmp", "/repo"):
            self.mk(path)
        self.add(self.walk("/etc"), "passwd", Node("file"))
        self.add(self.walk("/etc/ssh"), "config", Node("file"))
        self.cwd = self.walk("/repo")
        self.chain: set[Node] = set()
        node = self.root
        while True:
            self.chain.add(node)
            if node is self.top:
                break
            node = node.parent

    def mk(self, path: str) -> Node:
        cur = self.top
        for part in [x for x in path.split("/") if x]:
            if part not in cur.children:
                self.add(cur, part, Node("dir"))
            cur = cur.children[part]
        return cur

    def walk(self, path: str) -> Node:
        cur = self.top
        for part in [x for x in path.split("/") if x]:
            cur = cur.children[part]
        return cur

    def add(self, parent: Node, name: str, node: Node) -> None:
        node.parent, node.name = parent, name
        parent.children[name] = node

    def inside(self, node: Node) -> bool:
        while True:
            if node is self.root:
                return True
            if node is self.top:
                return False
            node = node.parent

    def path_of(self, node: Node) -> str:
        parts = []
        while node is not self.top:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def resolve(self, path: str, follow_last: bool, want_parent: bool = False, start: Node | None = None) -> Res:
        """按 Linux 规则解析 ``path``。``want_parent``：创建／删除／改名类调用（返回父目录与末分量，末分量对象可以不存在）；
        ``start``：相对路径的起点目录（dirfd 或工作目录）。"""
        touched = False
        links = 0
        if path == "":
            return Res("ENOENT")

        def lookup_touch(directory: Node, child: str) -> None:
            nonlocal touched
            if self.inside(directory):
                return
            if directory in self.chain:
                nxt = directory.children.get(child) if directory.children is not None else None
                if nxt is not None and (nxt in self.chain or nxt is self.root):
                    return                       # 通往私有盘的祖先链上的必经查找，不算触碰
            touched = True

        cur = self.top if path.startswith("/") else (start or self.cwd)
        trailing = path.endswith("/")
        queue = [c for c in path.split("/") if c != ""]
        if not queue:
            return Res(None, cur.parent, "", cur, not self.inside(cur), "dir")
        while queue:
            comp = queue.pop(0)
            last = not queue
            if comp == ".":
                if last:
                    return Res(None, cur.parent, ".", cur, not self.inside(cur), "dot")
                continue
            if comp == "..":
                if not self.inside(cur):
                    touched = True
                if last:
                    return Res(None, cur, "..", cur.parent, touched or not self.inside(cur.parent), "dotdot")
                cur = cur.parent
                continue
            if cur.kind != "dir":
                return Res("ENOTDIR", touched=touched)
            lookup_touch(cur, comp)
            node = cur.children.get(comp)
            if last and want_parent:
                return Res(None, cur, comp, node, touched or (node is not None and not self.inside(node)), "norm")
            if node is None:
                return Res("ENOENT", cur, comp, None, touched)
            if node.kind == "symlink" and (not last or follow_last or trailing):
                links += 1
                if links > 40:
                    return Res("ELOOP", touched=touched)
                text = node.text
                if text.startswith("/"):
                    cur = self.top
                queue = [c for c in text.split("/") if c != ""] + queue
                if not queue and last:
                    return Res(None, cur.parent, "", cur, touched or not self.inside(cur), "dir")
                trailing = trailing or (text.endswith("/") and last)
                continue
            if last:
                if trailing and node.kind != "dir":
                    return Res("ENOTDIR", touched=touched)
                return Res(None, cur, comp, node, touched or not self.inside(node), "norm")
            cur = node
        return Res(None, cur.parent, "", cur, touched, "dir")


class Generator:
    """见模块说明。``lines`` 是 strace 行，``touched[i]`` 是模型对第 i 行“内核是否触碰宿主”的判断。"""

    LINK_TEXTS = ["/etc", "/etc", "/etc/ssh", "/etc/ssh", "/etc/passwd", "/", "..", "d", "ln2", "../..", ".", "ln", "/tmp", "e/", "/etc/"]
    OPS = ["mkdir", "symlink", "lstat", "stat", "open_c", "open_r", "open_nf", "unlink", "rmdir", "rename", "readlink", "chdir", "access",
           "exec", "truncate"]
    WEIGHTS = [14, 6, 6, 3, 8, 2, 2, 4, 1, 5, 3, 2, 1, 6, 3]

    def __init__(self, root: str, seed: int) -> None:
        self.root = root
        self.rnd = random.Random(seed)
        self.world = HostWorld(root)
        self.lines: list[str] = []
        self.touched: list[bool] = []

    # ---- 路径与描述符 ----
    def rpath(self) -> str:
        rnd = self.rnd
        comps = [rnd.choices(NAMES + [".", "..", ""], weights=[3, 2, 18, 4, 2, 12, 5, 1, 2, 2] + [2, 12, 1])[0] for _ in range(rnd.randint(1, 5))]
        tail = "/" if rnd.random() < 0.12 else ""
        base = rnd.choice([self.root, self.root, self.root, self.root + "/d", "", ""])
        text = base + "/" + "/".join(comps) + tail if base else "/".join(comps) + tail
        if base and rnd.random() < 0.7:
            text = text.replace("//", "/")
        return text or "x"

    def emit(self, line: str, touched: bool) -> None:
        self.lines.append(line)
        self.touched.append(touched)

    def annotation(self) -> str:
        return f"AT_FDCWD<{self.world.path_of(self.world.cwd)}>"

    def pick_start(self, use_at: bool) -> tuple[Node, str]:
        """at 类调用的 dirfd：多数是 AT_FDCWD（工作目录），其余是私有盘内某个目录上的真实描述符。返回 (起点目录节点, 描述符标注)。"""
        world = self.world
        if not use_at or self.rnd.random() < 0.6:
            return world.cwd, self.annotation()
        directories, stack = [], [world.root]
        while stack:
            node = stack.pop()
            directories.append(node)
            stack.extend(child for child in node.children.values() if child.kind == "dir")
        node = self.rnd.choice(directories)
        return node, f"{self.rnd.choice([3, 4, 5, 6])}<{world.path_of(node)}>"

    # ---- 前缀 ----
    def prefix(self) -> None:
        w, r, rnd = self.world, self.root, self.rnd
        w.add(w.root, "d", Node("dir"))
        self.emit(f'1 mkdir("{r}/d", 0777) = 0', False)
        w.cwd = w.root
        self.emit(f'1 chdir("{r}") = 0', False)
        w.add(w.root.children["d"], "s", Node("dir"))
        self.emit(f'1 mkdir("{r}/d/s", 0777) = 0', False)
        w.add(w.root, "f", Node("file"))
        self.emit(f'1 openat(AT_FDCWD<{r}>, "{r}/f", O_WRONLY|O_CREAT|O_EXCL) = 3<{r}/f>', False)
        for name, parent, parent_path in (("ln", w.root, r), ("ln2", w.root, r), ("dl", w.root.children["d"], r + "/d")):
            text = rnd.choice(self.LINK_TEXTS)
            w.add(parent, name, Node("symlink", text))
            self.emit(f'1 symlink("{text}", "{parent_path}/{name}") = 0', False)
        if rnd.random() < 0.5:
            # S-2 场景：chdir 进 x，在 x 里建链接，再把 x 改名为 y（之后相对路径要跟着目录走）。
            x = Node("dir")
            w.add(w.root, "x", x)
            self.emit(f'1 mkdir("{r}/x", 0777) = 0', False)
            w.cwd = x
            self.emit(f'1 chdir("{r}/x") = 0', False)
            text = rnd.choice(["/etc", "/etc/ssh"])
            w.add(x, "ln", Node("symlink", text))
            self.emit(f'1 symlink("{text}", "{r}/x/ln") = 0', False)
            del w.root.children["x"]
            w.add(w.root, "y", x)
            self.emit(f'1 rename("{r}/x", "{r}/y") = 0', False)

    # ---- 随机操作（每次提议一步；调用方决定是否接受） ----
    def step(self) -> None:
        w, rnd = self.world, self.rnd
        op = rnd.choices(self.OPS, weights=self.WEIGHTS)[0]
        path = self.rpath()
        use_at = rnd.random() < 0.55
        start, ann = self.pick_start(use_at)
        if op == "mkdir":
            res = w.resolve(path, False, True, start)
            result = self.creation_result(res, lambda: w.add(res.parent, res.name, Node("dir")))
            self.emit(f'1 mkdirat({ann}, "{path}", 0777) = {result}' if use_at else f'1 mkdir("{path}", 0777) = {result}', res.touched)
        elif op == "symlink":
            text = rnd.choice(self.LINK_TEXTS)
            res = w.resolve(path, False, True, start)
            if not res.error and res.last == "norm" and res.node is None and res.parent.kind == "dir" and path.endswith("/"):
                result = fmt_err("ENOENT")
            else:
                result = self.creation_result(res, lambda: w.add(res.parent, res.name, Node("symlink", text)))
            self.emit(f'1 symlinkat("{text}", {ann}, "{path}") = {result}' if use_at else f'1 symlink("{text}", "{path}") = {result}', res.touched)
        elif op in ("lstat", "stat"):
            follow = op == "stat"
            res = w.resolve(path, follow, False, start)
            if res.error:
                result, struct = fmt_err(res.error), "0x7ffd0"
            else:
                mode = {"dir": "S_IFDIR|0755", "file": "S_IFREG|0644", "symlink": "S_IFLNK|0777"}[res.node.kind]
                size = len(res.node.text) if res.node.kind == "symlink" else 0
                struct = "{st_dev=makedev(0, 0x69), st_ino=%d, st_mode=%s, st_nlink=1, st_uid=0, st_gid=0, st_size=%d}" % (res.node.ino, mode, size)
                result = "0"
            if use_at:
                self.emit(f'1 newfstatat({ann}, "{path}", {struct}, {"0" if follow else "AT_SYMLINK_NOFOLLOW"}) = {result}', res.touched)
            else:
                self.emit(f'1 {"stat" if follow else "lstat"}("{path}", {struct}) = {result}', res.touched)
        elif op in ("open_c", "open_r", "open_nf"):
            flags = {"open_c": "O_WRONLY|O_CREAT|O_EXCL", "open_r": "O_RDONLY", "open_nf": "O_RDONLY|O_NOFOLLOW"}[op]
            if op == "open_c":
                res = w.resolve(path, False, True, start)
                created: list[Node] = []

                def create_file() -> None:
                    node = Node("file")
                    w.add(res.parent, res.name, node)
                    created.append(node)

                if not res.error and res.last == "norm" and res.node is None and res.parent.kind == "dir" and path.endswith("/"):
                    result = fmt_err("EISDIR")
                else:
                    result = self.creation_result(res, create_file)
                if result == "0":
                    result = f"3<{w.path_of(created[0])}>"
            else:
                res = w.resolve(path, op == "open_r", False, start)
                if res.error:
                    result = fmt_err(res.error)
                elif op == "open_nf" and res.node.kind == "symlink":
                    result = fmt_err("ELOOP")
                else:
                    result = f"3<{w.path_of(res.node)}>"
            self.emit(f'1 openat({ann}, "{path}", {flags}) = {result}', res.touched)
        elif op == "unlink":
            res = w.resolve(path, False, True, start)
            result = "0"
            if res.error:
                result = fmt_err(res.error)
            elif res.last in ("dot", "dotdot", None):
                result = fmt_err("EISDIR")
            elif res.node is None:
                result = fmt_err("ENOENT")
            elif res.node.kind == "dir":
                result = fmt_err("EISDIR")
            elif path.endswith("/"):
                result = fmt_err("ENOTDIR")
            else:
                del res.parent.children[res.name]
            self.emit(f'1 unlinkat({ann}, "{path}", 0) = {result}' if use_at else f'1 unlink("{path}") = {result}', res.touched)
        elif op == "rmdir":
            res = w.resolve(path, False, True)
            result = "0"
            if res.error:
                result = fmt_err(res.error)
            elif res.last in ("dot", "dotdot", None):
                result = fmt_err("EINVAL")
            elif res.node is None:
                result = fmt_err("ENOENT")
            elif res.node.kind != "dir":
                result = fmt_err("ENOTDIR")
            elif res.node.children:
                result = fmt_err("ENOTEMPTY")
            elif res.node is w.root or res.node is w.cwd:
                result = fmt_err("EBUSY")
            else:
                del res.parent.children[res.name]
            self.emit(f'1 rmdir("{path}") = {result}', res.touched)
        elif op == "rename":
            self.rename(path)
        elif op == "readlink":
            res = w.resolve(path, False, False, start)
            if res.error:
                result, buffer = fmt_err(res.error), "0x7ffd0"
            elif res.node.kind != "symlink":
                result, buffer = fmt_err("EINVAL"), "0x7ffd0"
            else:
                buffer, result = '"%s"' % res.node.text, str(len(res.node.text))
            self.emit(f'1 readlinkat({ann}, "{path}", {buffer}, 4096) = {result}' if use_at else f'1 readlink("{path}", {buffer}, 4096) = {result}',
                      res.touched)
        elif op == "chdir":
            res = w.resolve(path, True)
            if res.error:
                result = fmt_err(res.error)
            elif res.node.kind != "dir":
                result = fmt_err("ENOTDIR")
            else:
                result = "0"
                w.cwd = res.node
            self.emit(f'1 chdir("{path}") = {result}', res.touched)
        elif op == "access":
            res = w.resolve(path, True)
            self.emit(f'1 access("{path}", F_OK) = {fmt_err(res.error) if res.error else "0"}', res.touched)
        elif op == "exec":
            res = w.resolve(path, True)
            self.emit(f'1 execve("{path}", ["x"], 0x7ffd /* 1 var */) = {fmt_err(res.error) if res.error else fmt_err("EACCES")}', res.touched)
        else:                                          # truncate
            res = w.resolve(path, True)
            if res.error:
                result = fmt_err(res.error)
            else:
                result = fmt_err("EISDIR") if res.node.kind == "dir" else "0"
            self.emit(f'1 truncate("{path}", 0) = {result}', res.touched)

    @staticmethod
    def creation_result(res: Res, create: Any) -> str:
        """mkdir／symlink／open(O_CREAT|O_EXCL) 共用的结果判定；成功时调用 ``create()`` 修改世界。"""
        if res.error:
            return fmt_err(res.error)
        if res.last in ("dot", "dotdot", None) or res.node is not None:
            return fmt_err("EEXIST")
        if res.parent.kind != "dir":
            return fmt_err("ENOTDIR")
        create()
        return "0"

    def rename(self, path: str) -> None:
        w, rnd = self.world, self.rnd
        path2 = self.rpath()
        if rnd.random() < 0.3:
            # 一端的写法经过另一端自己（rename("a", "a/../b")）：目的端的解析依赖来源端在调用之前的状态。
            path2 = path.rstrip("/") + "/" + rnd.choice(["..", "../" + rnd.choice(NAMES), "../../" + rnd.choice(NAMES)])
        plain = rnd.random() < 0.5
        if plain:
            start1 = start2 = w.cwd
            ann1 = ann2 = ""
        else:
            start1, ann1 = self.pick_start(True)
            start2, ann2 = self.pick_start(True)
        first, second = w.resolve(path, False, True, start1), w.resolve(path2, False, True, start2)
        result = "0"
        if first.error:
            result = fmt_err(first.error)
        elif second.error:
            result = fmt_err(second.error)
        elif first.last in ("dot", "dotdot", None) or second.last in ("dot", "dotdot", None):
            result = fmt_err("EBUSY")
        elif first.node is None:
            result = fmt_err("ENOENT")
        elif (path.endswith("/") or path2.endswith("/")) and first.node.kind != "dir":
            result = fmt_err("ENOTDIR")
        elif second.node is first.node:
            result = "0"
        elif second.parent.kind != "dir":
            result = fmt_err("ENOTDIR")
        elif second.node is not None and (second.node.kind == "dir") != (first.node.kind == "dir"):
            result = fmt_err("ENOTDIR" if first.node.kind == "dir" else "EISDIR")
        elif second.node is not None and second.node.kind == "dir" and second.node.children:
            result = fmt_err("ENOTEMPTY")
        else:
            ancestor, inside_itself = second.parent, False
            while True:
                if ancestor is first.node:
                    inside_itself = True
                    break
                if ancestor is w.top:
                    break
                ancestor = ancestor.parent
            if inside_itself and first.node.kind == "dir":
                result = fmt_err("EINVAL")
            elif first.node is w.root:
                result = fmt_err("EBUSY")
            else:
                del first.parent.children[first.name]
                if second.node is not None:
                    del second.parent.children[second.name]
                w.add(second.parent, second.name, first.node)
        touched = first.touched or second.touched
        if plain:
            self.emit(f'1 rename("{path}", "{path2}") = {result}', touched)
        else:
            self.emit(f'1 renameat2({ann1}, "{path}", {ann2}, "{path2}", 0) = {result}', touched)


class HostDifferentialTests(R7aFixture):
    """含宿主目录的端到端对拍：整套严格审计通过的序列，模型不得说它触碰过宿主。"""

    PROPOSALS = 30

    def parser(self) -> ra.TraceParser:
        return ra.TraceParser(strict=True, runtime_context=self.context, trace_contract="v4")

    def outside(self, parser: ra.TraceParser) -> set[str]:
        return {path for path in parser.accesses if not (path == self.root or path.startswith(self.root + "/"))}

    def still_clean(self, before: ra.TraceParser, after: ra.TraceParser) -> bool:
        """一个提议被接受的条件：没有新的未解析原因、没有新的归属问题、没有新的盘外访问（盘外访问需要声明，审计会判未声明项）、没有悬空的调用。"""
        return (after.unresolved == before.unresolved and len(after.output_tracker.problems) == len(before.output_tracker.problems)
                and self.outside(after) == self.outside(before) and not after.pending and not after.deferred_count)

    def build(self, seed: int) -> tuple[Generator, dict[str, int]]:
        generator = Generator(self.root, seed)
        generator.prefix()
        parser = self.parser()
        for line in generator.lines:
            parser.feed(line)
        self.assertEqual((parser.unresolved, set(parser.output_tracker.problems)), (0, set()), "前缀必须是合法历史")
        stats = {"accepted": 0, "touching_accepted": 0, "touching_rejected": 0}
        for _ in range(self.PROPOSALS):
            trial, trial_parser = copy.deepcopy(generator), copy.deepcopy(parser)
            before = len(trial.lines)
            trial.step()
            for line in trial.lines[before:]:
                trial_parser.feed(line)
            touching = any(trial.touched[before:])
            if self.still_clean(parser, trial_parser):
                generator, parser = trial, trial_parser
                stats["accepted"] += 1
                stats["touching_accepted"] += touching
            else:
                generator.rnd = trial.rnd          # 被拒绝的提议丢弃，但随机数继续前进（下一次提议不会重复同一个操作）
                stats["touching_rejected"] += touching
        return generator, stats

    def violations(self, seeds: range, *, stop_at_first: bool = False) -> tuple[list[tuple[int, Generator]], dict[str, int]]:
        found: list[tuple[int, Generator]] = []
        totals = {"accepted": 0, "touching_accepted": 0, "touching_rejected": 0, "sequences": 0, "audit_passed": 0}
        for seed in seeds:
            generator, stats = self.build(seed)
            for key, value in stats.items():
                totals[key] += value
            totals["sequences"] += 1
            document = self.document(generator.lines)
            if self.audit(document)["coverage_complete"]:
                totals["audit_passed"] += 1
                if any(generator.touched):
                    found.append((seed, generator))
                    if stop_at_first:
                        break
        return found, totals

    def describe(self, seed: int, generator: Generator) -> str:
        shown = [("HOST " if touched else "     ") + line.split(" ", 1)[1].replace(self.root, "<R>") for line, touched in zip(generator.lines, generator.touched)]
        return f"seed={seed}\n" + "\n".join(shown)

    def test_no_sequence_the_audit_accepts_touched_the_host(self) -> None:
        found, totals = self.violations(range(0, 500))
        self.assertEqual(found, [], "\n\n".join(self.describe(seed, generator) for seed, generator in found[:2]))
        # 对拍不是空转：序列走得够深，模型确实在大量提议里看到了宿主触碰，而且它们几乎都被审计拒绝了。
        self.assertEqual(totals["audit_passed"], totals["sequences"], totals)
        self.assertGreater(totals["accepted"] / totals["sequences"], 10, totals)
        self.assertGreater(totals["touching_rejected"], 500, totals)

    def test_the_differential_has_teeth(self) -> None:
        """人为把被测代码改宽，对拍必须在有限个种子内报出违例；每个变异单独试，且试完还原（``mock.patch`` 的上下文）。"""
        runtime_class = ra._runtime_module().OutputTrackerV4
        original_check = ra.TraceParser._check_path_form

        def old_checked_join(self_, directory, path, *, check=True):          # 回到“交给跟踪器之后再检查目的端”的旧顺序
            original_check(self_, directory, path)
            return ra._join(directory, path)

        real_hazard = ra._path_form_hazard

        def without_trailing(tracker, base, raw):                              # 路径形态检查不管结尾斜杠与结尾点
            return real_hazard(tracker, base, raw.rstrip("/.") if raw.endswith(("/", "/.")) else raw)

        mutants = {
            "S-1：.. 之后落点是链接不判风险": mock.patch.object(ra, "_path_form_hazard", _hazard_before_wp_r7a),
            "S-2：rename 后工作目录不改写": mock.patch.object(ra.TraceParser, "_relocate_working_directories", lambda self_, name, ends: None),
            "两路径调用的目的端在事件之后才检查": mock.patch.multiple(ra.TraceParser, _check_path_form=lambda self_, directory, path: None,
                                                                      _checked_join=old_checked_join),
            "路径形态检查整个关闭": mock.patch.object(ra, "_path_form_hazard", lambda tracker, base, raw: False),
            "路径形态检查不管结尾斜杠与点": mock.patch.object(ra, "_path_form_hazard", without_trailing),
            "跟踪器不查中间分量是链接": mock.patch.object(runtime_class, "_through_symlink", lambda self_, path: False),
            "跟踪器对链接名字上的任何调用都放行": mock.patch.object(runtime_class, "_symlink_name_verdict", lambda self_, event: "ok"),
        }
        for label, patcher in mutants.items():
            with self.subTest(label=label), patcher:
                found, _totals = self.violations(range(0, 400), stop_at_first=True)
                self.assertTrue(found, f"变异“{label}”在 400 个种子内没有被对拍发现：对拍没有牙")
        found, _totals = self.violations(range(0, 3), stop_at_first=True)
        self.assertEqual(found, [], "变异全部还原之后，同样的种子又是干净的")
