#!/usr/bin/env python3
"""读集审计（E3-04）：用 strace 记录单元实际打开、探测与执行过的路径，核对它们都落在单元声明的输入范围里。

为什么要有：E2-05 的步骤失效判定与 E3-01 的单元承接都靠「声明的输入范围」——漏声明一项，该重跑的单元会被判成可以
承接。审计要真跑才有读集，所以只在重新执行全集时带（入口门禁 ``--audit-reads``）：执行器把每个正式执行的单元包在
strace 下，单元结束后按它的输入明细核对，超出声明的读取逐条报出，整次运行判失败。

* 记录：``strace`` 按 ``STRACE_OPTIONS`` 只跟踪文件类调用与进程派生，输出用 ``-o '|…'`` 先经 grep 预筛、再交给本模块的
  ``filter`` 子命令：它只留测试树与数据根下的路径，进程结束时写一份紧凑 JSON（Go 编译、node 读 GOROOT 与依赖目录的
  海量行不落盘）。``-y`` 把每个 dirfd（含 ``AT_FDCWD``）标成当时的目录，相对路径据此还原，不用自己跟踪工作目录；
  只有不带 dirfd 的调用（execve、chdir 等）按同一进程最近一次标注的目录还原，子进程继承父进程的。
* 归类：``openat`` 等按标志分读、写、打开目录；stat、access、readlink 一类成功是元数据；execve 是执行；任何调用因
  路径不存在（ENOENT、ENOTDIR）失败都记为「探测不存在的路径」。只判读、执行、写与探测不存在的路径（改动后出现的文件
  会改变行为）；元数据（stat 成功、打开或列举目录）只计数不判——工具身份计算、整树比对一类遍历会 stat 树里每个文件再
  按名字排除，按「stat 了就算输入」会把每个单元都报成读了整个测试目录。导入系统试探的扩展模块变体（``.so``）与字节码
  缓存（``__pycache__``、``.pyc``）不看。
* 覆盖（以执行记录里的输入明细为准）：范围项（include／exclude 前缀）、文件项（闭包文件、声明的单个文件）、HEAD（读
  ``.git``）、已算明细里登记的绝对路径（数据根的冻结台账目录、录制数据等）；声明内容的上级目录算覆盖。只看元数据、
  不读内容的依赖（只列目录或只 stat 已存在的文件）审计看不到，这是已知的局限。
* 两个根：测试树单元读到数据根一律报出（测试不该碰生产数据）；pre-A3 场景在数据根运行（工作目录在数据根里），数据根
  与仓库同布局的部分（受管树、docs、部署脚本副本）按仓库相对路径核对，其余按已算明细与豁免表（``DATA_ROOT_EXEMPT``）。
* 只读 HEAD 提交号的模块（unit_records.HEAD_ID_ONLY_READERS）没声明 HEAD：它们读 ``.git`` 只许 HEAD、refs、配置一类，
  读到对象库（``.git/objects/``）照样报出。

子命令：``filter --output <json> --root <目录> [--root …]``（读标准输入的 strace 输出，见上）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TRACE_SCHEMA = "read-audit-trace/v1"
REPORT_SCHEMA = "read-audit-report/v1"
# strace 选项：只跟踪文件类调用与进程派生（子进程继承工作目录要用）；不解码 stat 等结构、不打印信号（省 strace 自己的
# 格式化开销）。10-02 ARM64 第一轮实测：原来带 getdents64、全部进程类调用并解码结构时，被测进程只拿到约 30% 的核。
STRACE_OPTIONS = ("-f", "-qq", "--seccomp-bpf", "-y", "-e", "verbose=none", "-e", "signal=none", "-e", "trace=%file,clone,clone3,fork,vfork")
HERE = Path(__file__).resolve()
# 每个单元报出的未声明读取最多列多少条（总数照记）。
MAX_FINDINGS = 200
# 样例行截断长度。
SAMPLE_CHARS = 300

# 数据根里不算输入的路径（数据根相对路径：以 / 结尾的是前缀，其余精确匹配 → 原因）。新增条目要写明为什么不影响结论。
# 依据 10-02 ARM64 两轮审计（pre-A3 全部 44 个场景）。
DATA_ROOT_EXEMPT: dict[str, str] = {
    "staging/pre-a3-scenarios/": "pre-A3 场景本次自己建的临时根（场景的输出，读回的是本次写下的内容）",
    ".git/": "git 在数据根里做仓库发现（数据根不是 git 仓库；用法同 HEAD_ID_ONLY_READERS，提交号只用在同一次运行里）",
    "HEAD": "git 仓库发现时按裸仓库检查 HEAD（同上）",
    "staging/.git/": "git 从 staging 下的临时目录往上做仓库发现（同上）",
    "staging/HEAD": "git 仓库发现时按裸仓库检查 HEAD（同上）",
    "tools/__init__.py": "数据根的 tools 是命名空间包（部署不放包标记，布局由受监督部署固定），导入系统每次都探测这个标记",
    "libgcc_s.so.1": "内核故障场景在数据根编译 eBPF 辅助程序，动态链接器在工作目录里找 libgcc_s（不存在、也不会放进数据根）",
}
# 同上，按正则整串匹配的（数据根相对路径）。
DATA_ROOT_EXEMPT_PATTERNS: dict[str, str] = {
    r"-l[A-Za-z0-9_+-]+": "内核故障场景在数据根编译 eBPF 辅助程序，编译器把链接参数（-lbpf、-lelf 等）当文件名在工作目录里试探",
    r"tools\.official_client_capture\.[A-Za-z0-9_.]+": "unittest 把命令行里的测试名（点分模块路径）当文件路径在工作目录里试探",
    r"(?:staging/)?(?:go\.mod|go\.work|\.hg|\.svn|\.fslckout|_FOSSIL_)":
        "Go 工具链在场景临时根里编译候选源码：从工作目录往上找 go.mod、go.work，并为 -buildvcs 找版本控制目录（.git 见上），"
        "一路探到 staging 与数据根顶层（都不存在；数据根不放 Go 模块与版本控制目录）",
    r"\.r18-namespace-probe-[0-9]+":
        "R18 录制回放链的命名空间自检：在宿主数据根试写探测文件，预期只读失败（EROFS）；写成功时场景自己判失败、删掉探测文件",
}

# 只读 HEAD 提交号的模块读 .git 时允许的部分：HEAD、引用、配置与仓库结构探测；对象库（历史内容）不许。
_GIT_META_ALLOWED = (".git", ".git/HEAD", ".git/config", ".git/packed-refs", ".git/commondir", ".git/shallow", ".git/objects",
                     ".git/refs", ".git/info", ".git/worktrees", ".git/objects/info")
_GIT_META_PREFIXES = (".git/refs/", ".git/info/", ".git/objects/info/", ".git/worktrees/")

# 系统调用分类。带 dirfd 的：路径按前面的 dirfd 标注还原。
_DIRFD_READ = frozenset({"openat", "openat2"})
_DIRFD_PROBE = frozenset({"newfstatat", "fstatat64", "statx", "faccessat", "faccessat2", "readlinkat", "name_to_handle_at"})
_DIRFD_WRITE = frozenset({"mkdirat", "unlinkat", "utimensat", "fchmodat", "fchmodat2", "fchownat", "mknodat", "renameat", "renameat2",
                          "linkat", "symlinkat", "futimesat"})
_DIRFD_EXEC = frozenset({"execveat"})
# 不带 dirfd 的：第一个（rename、link 是前两个）字符串参数是路径，相对路径按进程最近的工作目录还原。
_PLAIN_READ = frozenset({"open"})
_PLAIN_PROBE = frozenset({"stat", "lstat", "stat64", "lstat64", "access", "readlink", "statfs", "statfs64", "chdir", "getxattr", "lgetxattr",
                          "listxattr", "llistxattr", "inotify_add_watch", "uselib"})
_PLAIN_WRITE = frozenset({"creat", "mkdir", "rmdir", "unlink", "rename", "link", "symlink", "truncate", "truncate64", "chmod", "chown", "lchown",
                          "utime", "utimes", "setxattr", "lsetxattr", "removexattr", "lremovexattr", "mknod"})
_PLAIN_EXEC = frozenset({"execve"})
_TWO_PATHS = frozenset({"rename", "link", "renameat", "renameat2", "linkat"})
_SPAWN = frozenset({"clone", "clone3", "fork", "vfork"})
_WRITE_FLAGS = re.compile(r"\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b")
_MISSING_ERRNOS = frozenset({"ENOENT", "ENOTDIR"})
# 读取方式：判的（读内容、执行、写、探测不存在的路径）与只算元数据的（stat 成功、打开或列举目录）。元数据不判：工具身份
# 计算、整树比对这类遍历会 stat 树里每个文件再按名字排除，按「stat 了就算输入」会把每个测试单元都报成读了整个测试目录
# （10-02 ARM64 第一轮实测 6621 处全是这一类）；真正依赖文件内容的都会读，读会被核对到。
JUDGED_KINDS = frozenset({"read", "exec", "write", "missing"})
METADATA_KINDS = frozenset({"stat", "dir", "list"})

_LINE = re.compile(r"^(\d+)\s+(.*)$")
_CALL = re.compile(r"^([a-z_][a-z0-9_]*)\((.*)$", re.S)
_RESUMED = re.compile(r"^<\.\.\. ([a-z_][a-z0-9_]*) resumed>")
_ESC = r'(?:[^"\\]|\\.)*'
_ANN = r"(?:[^>\\]|\\.)*"
_PAIR = re.compile(r'(?:AT_FDCWD|-?\d+)<(' + _ANN + r')>,\s*"(' + _ESC + r')"')
_ANNOTATION = re.compile(r"(AT_FDCWD|-?\d+)<(" + _ANN + r")>")
_QUOTED = re.compile(r'"(' + _ESC + r')"')
# 返回值在行尾：「) = 值[<解析路径>][ 错误码 (说明)]」；从行尾匹配，参数里出现类似字样也不会误判。
_RESULT = re.compile(r"\)\s*=\s*(-?\d+|\?)(?:<" + _ANN + r">)?(?:\s+(E[A-Z0-9]+)(?:\s+\([^)]*\))?)?\s*$")


class AuditError(RuntimeError):
    """审计配置或轨迹文件不可用。"""


# ---------------------------------------------------------------------------
# 解析 strace 输出
# ---------------------------------------------------------------------------


def unescape(text: str) -> str:
    """strace 字符串字面量还原：``\\\\``、``\\"``、``\\n`` 一类与八进制、十六进制转义（非 ASCII 字节默认按八进制转义输出）。"""

    if "\\" not in text:
        return text
    out = bytearray()
    index = 0
    simple = {"n": 10, "t": 9, "r": 13, "v": 11, "f": 12, "a": 7, "b": 8, "\\": 92, '"': 34, "'": 39}
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.extend(char.encode("utf-8"))
            index += 1
            continue
        nxt = text[index + 1]
        if nxt in simple:
            out.append(simple[nxt])
            index += 2
        elif nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", text[index + 2:index + 4] or ""):
            out.append(int(text[index + 2:index + 4], 16))
            index += 4
        elif nxt in "01234567":
            digits = re.match(r"[0-7]{1,3}", text[index + 1:]).group(0)  # type: ignore[union-attr]
            out.append(int(digits, 8) & 0xFF)
            index += 1 + len(digits)
        else:
            out.extend(nxt.encode("utf-8"))
            index += 2
    return out.decode("utf-8", "surrogateescape")


def _join(directory: str | None, path: str) -> str | None:
    if path.startswith("/"):
        return os.path.normpath(path)
    if not directory or not directory.startswith("/"):
        return None
    return os.path.normpath(os.path.join(directory, path))


class TraceParser:
    """逐行解析 ``strace -f -y`` 的输出，累积（路径 → 读取方式集合）与每个路径的第一条样例行。"""

    def __init__(self) -> None:
        self.cwd: dict[str, str] = {}
        self.accesses: dict[str, set[str]] = {}
        self.samples: dict[str, str] = {}
        self.lines = 0

    def _add(self, path: str | None, kind: str, line: str) -> None:
        if not path:
            return
        self.accesses.setdefault(path, set()).add(kind)
        self.samples.setdefault(path, line[:SAMPLE_CHARS])

    def feed(self, raw: str) -> None:
        self.lines += 1
        line = raw.rstrip("\n")
        match = _LINE.match(line)
        pid, rest = (match.group(1), match.group(2)) if match else ("0", line)
        resumed = _RESUMED.match(rest)
        if resumed is not None:
            if resumed.group(1) in _SPAWN:
                child = _RESULT.search(rest)
                if child and child.group(1).isdigit() and pid in self.cwd:
                    self.cwd.setdefault(child.group(1), self.cwd[pid])
            return
        if rest.startswith(("---", "+++")):
            return
        call = _CALL.match(rest)
        if call is None:
            return
        name, body = call.group(1), call.group(2)
        unfinished = body.rstrip().endswith("<unfinished ...>")
        result = None if unfinished else _RESULT.search(body)
        errno = result.group(2) if result else None
        for annotation in _ANNOTATION.finditer(body):
            if annotation.group(1) == "AT_FDCWD":
                directory = unescape(annotation.group(2))
                if directory.startswith("/"):
                    self.cwd[pid] = directory
        if name in _SPAWN:
            if result and result.group(1).isdigit() and pid in self.cwd:
                self.cwd.setdefault(result.group(1), self.cwd[pid])
            return
        if name == "getdents64":
            annotation = _ANNOTATION.search(body)
            if annotation:
                path = unescape(annotation.group(2))
                if path.startswith("/"):
                    self._add(os.path.normpath(path), "list", line)
            return
        failed = errno is not None
        if name in _DIRFD_READ or name in _DIRFD_PROBE or name in _DIRFD_WRITE or name in _DIRFD_EXEC:
            pairs = [(unescape(directory), unescape(path)) for directory, path in _PAIR.findall(body)]
            if name not in _TWO_PATHS:
                pairs = pairs[:1]
            missing = errno in _MISSING_ERRNOS
            if name in _DIRFD_READ:
                kind = ("write" if _WRITE_FLAGS.search(body) else "missing" if missing else "stat" if failed
                        else "dir" if "O_DIRECTORY" in body else "read")
            elif name in _DIRFD_WRITE:
                kind = "write"
            elif name in _DIRFD_EXEC:
                kind = "missing" if missing else "stat" if failed else "exec"
            else:
                kind = "missing" if missing else "stat"
            for directory, path in pairs:
                if path == "":
                    continue  # AT_EMPTY_PATH：对已打开的文件描述符操作，打开那一下已经记过
                self._add(_join(directory, path), kind, line)
            return
        if name in _PLAIN_READ or name in _PLAIN_PROBE or name in _PLAIN_WRITE or name in _PLAIN_EXEC:
            strings = [unescape(item) for item in _QUOTED.findall(body.split("<unfinished ...>")[0])]
            if name == "symlink":
                strings = strings[1:2]  # 只有链接本身被创建；目标字符串不是被访问的路径
            elif name in _TWO_PATHS:
                strings = strings[:2]
            else:
                strings = strings[:1]
            missing = errno in _MISSING_ERRNOS
            if name in _PLAIN_READ:
                kind = ("write" if _WRITE_FLAGS.search(body) else "missing" if missing else "stat" if failed
                        else "dir" if "O_DIRECTORY" in body else "read")
            elif name in _PLAIN_WRITE:
                kind = "write"
            elif name in _PLAIN_EXEC:
                kind = "missing" if missing else "stat" if failed else "exec"
            elif name == "chdir":
                kind = "missing" if missing else "dir"
            else:
                kind = "missing" if missing else "stat"
            for path in strings:
                self._add(_join(self.cwd.get(pid), path), kind, line)
            if name == "chdir" and not failed and strings:
                joined = _join(self.cwd.get(pid), strings[0])
                if joined:
                    self.cwd[pid] = joined


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def trace_document(parser: TraceParser, roots: Sequence[str]) -> dict[str, Any]:
    """只留各个根之下的路径（字节码缓存不留），写成紧凑文档。"""

    roots = [os.path.normpath(str(root)) for root in roots]
    kept = {}
    for path, kinds in parser.accesses.items():
        if any(_under(path, root) for root in roots) and not _bytecode(path):
            kept[path] = sorted(kinds)
    return {"schema_version": TRACE_SCHEMA, "roots": roots, "lines": parser.lines,
            "accesses": [[path, kept[path]] for path in sorted(kept)],
            "samples": {path: parser.samples[path] for path in sorted(kept)}}


def _bytecode(path: str) -> bool:
    return "/__pycache__/" in f"/{path}/" or path.endswith((".pyc", ".pyo"))


def filter_stream(stream: Iterable[str], roots: Sequence[str]) -> dict[str, Any]:
    parser = TraceParser()
    for line in stream:
        parser.feed(line)
    return trace_document(parser, roots)


_ERE_SPECIAL = set(".[]()*+?{}|^$\\")


def prefilter_pattern(roots: Sequence[str]) -> str:
    """grep -E 预筛：只留路径参数落在某个根之下（绝对路径以根开头，或相对路径而 dirfd 标注在根之下）的行，以及派生、
    execve、chdir 行（还原工作目录要用）。Python 解释器读标准库、Go 与 node 读工具链的海量行在这里丢掉，过滤器只解析剩下
    的（10-02 ARM64 第一轮实测：不预筛时 Python 过滤器吃掉约四分之一个核）。"""

    escaped = ["".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in os.path.normpath(str(root))) for root in roots]
    alternation = "|".join(escaped)
    return ("\"(" + alternation + ")(/|\")" + "|<(" + alternation + ")[^>]*>, \"[^/\"]"
            + "|^[0-9]+ (execve|execveat|chdir|clone|clone3|fork|vfork)\\(|<[.][.][.] (clone|clone3|fork|vfork) resumed>")


def strace_argv(argv: Sequence[str], *, output: Path, roots: Sequence[str], python: str | None = None) -> list[str]:
    """把一个单元的命令包在 strace 下：输出先经 grep 预筛，再交给本模块 ``filter``，只留各个根之下的路径。"""

    command = [python or sys.executable, "-B", str(HERE), "filter", "--output", str(output)]
    for root in roots:
        command += ["--root", str(root)]
    for part in command:
        if any(ch.isspace() or ch in "'\"\\$`|;&<>" for ch in part):
            raise AuditError(f"审计输出命令里的路径带空白或 shell 特殊字符，不能交给 strace 的管道：{part}")
    pattern = prefilter_pattern(roots)
    if "'" in pattern:
        raise AuditError("预筛表达式里有单引号，不能交给 shell")
    pipe = f"grep --line-buffered -E '{pattern}' | " + " ".join(command)
    return ["strace", *STRACE_OPTIONS, "-o", "|" + pipe, "--", *argv]


# ---------------------------------------------------------------------------
# 覆盖判定
# ---------------------------------------------------------------------------


def _matches(path: str, prefix: str) -> bool:
    if prefix == "":
        return True
    return path.startswith(prefix) if prefix.endswith("/") else path == prefix


class Declared:
    """一个单元的输入明细能覆盖哪些路径。``inputs`` 是执行记录里的 ``inputs``（unit_records 与 entry_steps 的明细格式）。"""

    def __init__(self, inputs: Sequence[Mapping[str, Any]] | None) -> None:
        self.ranges: list[tuple[list[str], list[str]]] = []
        self.files: set[str] = set()
        self.absolute: list[str] = []
        self.head = False
        for entry in inputs or []:
            if not isinstance(entry, Mapping):
                continue
            detail = entry.get("detail") if isinstance(entry.get("detail"), Mapping) else {}
            if entry.get("name") == "head":
                self.head = True
            if isinstance(detail.get("include"), list):
                self.ranges.append(([str(item) for item in detail["include"]], [str(item) for item in detail.get("exclude") or []]))
                continue
            path = detail.get("path")
            if isinstance(path, str) and path:
                if path.startswith("/"):
                    self.absolute.append(os.path.normpath(path))
                else:
                    self.files.add(path)
            for root in detail.get("paths") or []:
                if isinstance(root, str) and root.startswith("/"):
                    self.absolute.append(os.path.normpath(root))

    def covers_repo(self, relative: str, *, head_id_only: bool = False) -> bool:
        """仓库相对路径是否在声明范围里（声明内容的上级目录只看元数据，算覆盖）。"""

        if relative in ("", "."):
            return True
        if relative == ".git" or relative.startswith(".git/"):
            if self.head:
                return True
            return head_id_only and (relative in _GIT_META_ALLOWED or relative.startswith(_GIT_META_PREFIXES))
        for include, exclude in self.ranges:
            if any(_matches(relative, prefix) for prefix in include) and not any(_matches(relative, prefix) for prefix in exclude):
                return True
        if relative in self.files:
            return True
        directory = relative.rstrip("/") + "/"
        if any(item.startswith(directory) for item in self.files):
            return True
        return any(prefix.startswith(directory) for include, _exclude in self.ranges for prefix in include)

    def covers_absolute(self, path: str) -> bool:
        return any(_under(path, root) for root in self.absolute)


# 声明了整个仓库的输入（unit_records.COMMAND_RANGES 四段：受管工具树、测试目录、docs、其余部分）。
WHOLE_REPO_RANGES = frozenset({"repo:managed", "repo:tests", "repo:docs", "repo:rest"})


def declares_whole_repo(inputs: Sequence[Mapping[str, Any]] | None) -> bool:
    """单元的输入是否已经覆盖整个仓库（四段标准范围齐全，或有一段 include 空串且没有 exclude）。这样的单元对仓库不可能
    有未声明读取，审计不包 strace（命令单元：后端、前端、lint、egress 子检查、部署脚本；ARM64 实测后端 go test 在 strace
    下 663 秒对 467 秒，白付开销）；它们在测试树里运行、生产别名已遮住，读数据根的可能性也只有这一层保护。"""

    names = set()
    for entry in inputs or []:
        if not isinstance(entry, Mapping):
            continue
        names.add(str(entry.get("name")))
        detail = entry.get("detail") if isinstance(entry.get("detail"), Mapping) else {}
        if isinstance(detail.get("include"), list) and "" in detail["include"] and not detail.get("exclude"):
            return True
    return WHOLE_REPO_RANGES <= names


def _exempt(relative: str) -> bool:
    """数据根相对路径在豁免表里：落在某个前缀下（含前缀目录本身）、精确相等，或整串匹配某个正则。"""

    if any((relative.rstrip("/") + "/").startswith(prefix) if prefix.endswith("/") else relative == prefix for prefix in DATA_ROOT_EXEMPT):
        return True
    return any(re.fullmatch(pattern, relative) for pattern in DATA_ROOT_EXEMPT_PATTERNS)


def _suggestion(relative: str, root: str) -> str:
    if root == "data":
        return "数据根内容没有声明：加进单元输入（已算明细），或确属不影响结论时登记 DATA_ROOT_EXEMPT 并写明原因"
    if relative == ".git" or relative.startswith(".git/"):
        return "读了 git：读历史对象的登记 GIT_READERS（加 HEAD 输入）；只读提交号的核对 HEAD_ID_ONLY_READERS"
    if "/tests/" in f"/{relative}":
        return "测试目录文件不在静态依赖闭包里：补成显式导入，或整目录读取的登记 WHOLE_TESTS_READERS"
    return "路径不在声明的输入范围里：补进门禁清单的输入声明"


def audit_reads(document: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]] | None, *, repo_root: str,
                data_root: str | None = None, in_data_root: bool = False, head_id_only: bool = False) -> dict[str, Any]:
    """核对一个单元的读集：返回 ``{"undeclared": [...], "undeclared_count", "repo_paths", "data_paths", "listed"}``。

    ``in_data_root``：单元在数据根里运行（pre-A3 场景），数据根与仓库同布局的部分按仓库相对路径核对；否则读到数据根一律报出。"""

    declared = Declared(inputs)
    repo_root = os.path.normpath(repo_root)
    data = os.path.normpath(data_root) if data_root else None
    findings: list[dict[str, Any]] = []
    counts = {"repo_paths": 0, "data_paths": 0, "metadata_only": 0, "import_probes": 0}
    samples = document.get("samples") if isinstance(document.get("samples"), Mapping) else {}
    for path, kinds in document.get("accesses") or []:
        judged = sorted({str(kind) for kind in kinds} & JUDGED_KINDS)
        if not judged:
            counts["metadata_only"] += 1
            continue
        if _bytecode(path):
            continue
        if judged == ["missing"] and path.endswith(".so"):
            # 导入系统按扩展名逐个试探（__init__.abi3.so、.cpython-312-…so、.so），仓库里没有编译扩展，不算输入。
            counts["import_probes"] += 1
            continue
        if _under(path, repo_root):
            counts["repo_paths"] += 1
            relative = os.path.relpath(path, repo_root) if path != repo_root else ""
            if declared.covers_repo(relative, head_id_only=head_id_only):
                continue
            root, shown = "repo", relative
        elif data is not None and _under(path, data):
            counts["data_paths"] += 1
            relative = os.path.relpath(path, data) if path != data else ""
            if declared.covers_absolute(path):
                continue
            if in_data_root and declared.covers_repo(relative, head_id_only=head_id_only):
                continue
            if in_data_root and _exempt(relative):
                continue
            root, shown = "data", relative
        else:
            continue
        findings.append({"root": root, "path": shown, "kinds": judged, "sample": samples.get(path, ""),
                         "suggestion": _suggestion(shown, root)})
    findings.sort(key=lambda item: (item["root"], item["path"]))
    return {"undeclared_count": len(findings), "undeclared": findings[:MAX_FINDINGS], **counts}


def load_trace(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditError(f"审计轨迹读不出来：{path}（{error}）") from error
    if not isinstance(document, dict) or document.get("schema_version") != TRACE_SCHEMA:
        raise AuditError(f"审计轨迹格式不对：{path}")
    return document


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter", help="读标准输入的 strace 输出，只留各个根之下的路径，写紧凑 JSON")
    filt.add_argument("--output", type=Path, required=True)
    filt.add_argument("--root", action="append", default=[], required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command == "filter":
        stream = (line.decode("utf-8", "surrogateescape") for line in sys.stdin.buffer)
        document = filter_stream(stream, args.root)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8", errors="surrogateescape")
        os.replace(temporary, args.output)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
