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

以上为旧范围审计，始终标记 coverage_complete=false。B-11 严格模式不预筛路径、不跳过整仓声明，
逐路径绑定精确宿主快照，元数据、字节码和缺失状态同样核对；不稳定、写入或未解析调用失败关闭。

带隔离合同的轨迹有两个版本，由显式开关 ``trace_contract`` 选择，默认 v3（历史合同，行为不变）：
v3 的输出事件是 6 键，rename／link／符号链接与失败的写探测一律不认；v4 的输出事件是 11 键（另有 errno、call、role、
peer、epoch），两路径调用的两端按调用编号配对，失败调用带错误码，只读事件按「变更纪元」去重。重放按文档自己的
schema_version 分派，所以 v3 的已封存凭证永远按旧合同只读验真。control 四链的 sidecar 原样复用本模块，不传该开关，
仍然生产并重放 v3。

WP-R2（解析器加固第二轮）：ARM64 真实环境里把正常行为判成未解析的几处缺口，只在 v4 合同下放宽（v3 一字不变）：失败的 getcwd
（ERANGE／ENOENT）、``utimensat(fd, NULL, …)``（futimens）、解码后的 ``openat2``（标志取自 ``struct open_how``）、AF_UNIX 流式
套接字的创建，以及连接 AF_UNIX 绝对路径得到 ENOENT（登记为对该路径的 missing 探测）。v4 的 strace 选项因此多解码 openat2 与
connect、多跟踪 bind；v4 的输出事件上限单列为 ``MAX_OUTPUT_EVENTS_V4``。每处放宽的依据与相邻的仍被拒绝的情形见各辅助函数的说明。

WP-R3（解析器加固第三轮，同样只在 v4 合同下）：私有盘内“本单元自己创建的符号链接”被输出归属接受（``read_audit_runtime.OutputTrackerV4``
里的 ``symlink`` 对象，链接文本原样记在事件的 ``target`` 字段、从不解引用），并保证一切会解引用链接的访问仍然失败关闭。解析器侧
的配合有四处：（1）事件 ``flags`` 带上调用的 ``AT_*`` 标志（白名单 ``AT_FLAGS_V4``，名单之外登记 at-flag-not-decoded），lstat
才能与 stat 区分；（2）lstat 到跟踪器仍记着的、本单元创建的链接不再登记 symlink-metadata-not-bound，宿主上的真链接、不是本单元
创建的、返回结构没完整解码为 S_IFLNK 的仍登记；反方向（内核说普通文件、跟踪器说链接）登记 symlink-state-contradicts-stat；（3）存在符号链接
对象时，路径写法 ``link/``、``link/.``、``link/..`` 与链接后的 ``..`` 会让文本规范化与内核实际解析的对象不一致，登记
symlink-path-form-unsafe；（4）symlink／symlinkat 的链接文本缺失或被截断登记 symlink-text-not-decoded。``O_PATH|O_NOFOLLOW`` 的打开
不豁免。逐类放行／拒绝全表见 tools/official_client_capture/tests/test_ci_read_audit_wp_r3.py 的模块说明。

子命令：``filter --output <json> --root <目录> [--root …] [--strict] [--runtime-context <json> [--trace-contract v4]]``；
``scope --record <正式库记录> --trace <库内轨迹> --output <新提案>`` 生成须重新严格验收的收窄提案。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TRACE_SCHEMA = "read-audit-trace/v1"
STRICT_TRACE_SCHEMA = "read-audit-trace/v2"
RUNTIME_TRACE_SCHEMA = "read-audit-trace/v3"
# v4：带隔离合同的轨迹的新版事件合同（显式开关，默认仍是 v3）。输出事件 11 键，两路径调用的两端用 role／peer 配对，
# 失败调用带 errno，只读事件按变更纪元去重；配套的跟踪器是 read_audit_runtime.OutputTrackerV4。
# v1～v3 的常量与已封存凭证按旧合同继续只读验真，行为不变；control 四链的 sidecar 原样复用本模块，所以任何
# 新行为都必须由下面的 trace_contract 开关显式启用，默认路径不得有字节级差异。
RUNTIME_TRACE_SCHEMA_V4 = "read-audit-trace/v4"
TRACE_CONTRACT_V3 = "v3"
TRACE_CONTRACT_V4 = "v4"
TRACE_CONTRACTS = {TRACE_CONTRACT_V3: RUNTIME_TRACE_SCHEMA, TRACE_CONTRACT_V4: RUNTIME_TRACE_SCHEMA_V4}
HOST_SCHEMA = "read-audit-host-snapshot/v2"
REPORT_SCHEMA = "read-audit-report/v1"
# strace 选项：只跟踪文件类调用与进程派生（子进程继承工作目录要用）；不解码 stat 等结构、不打印信号（省 strace 自己的
# 格式化开销）。10-02 ARM64 第一轮实测：原来带 getdents64、全部进程类调用并解码结构时，被测进程只拿到约 30% 的核。
STRACE_OPTIONS = ("-f", "-qq", "--seccomp-bpf", "-y", "-e", "verbose=none", "-e", "signal=none", "-e", "trace=%file,clone,clone3,fork,vfork")
HERE = Path(__file__).resolve()
# 每个单元报出的未声明读取最多列多少条（总数照记）。
MAX_FINDINGS = 200
# 样例行截断长度。
SAMPLE_CHARS = 300
# 输出归属事件最多保留多少条（v4 是去重之后的条数）；超出登记 output-event-limit 未解析原因并判失败，不静默截断。
# 这是 v3 合同的上限（历史合同，含 control 四链与已封存凭证的重放），数值不变；v4 的上限单列为 MAX_OUTPUT_EVENTS_V4。
MAX_OUTPUT_EVENTS = 200000
# v4 合同的上限（WP-R2 第 6 项）。依据：10-15 ARM64 实测 1 小时写入密集单元（vc6_batch）在 v4 去重后，超出旧上限的事件有
# 358,504 条（每超出一条登记一次 output-event-limit），即该单元共约 558,504 条输出事件，旧上限 200000 把正常单元判成失败。
# 上限的唯一作用是内存／时间保护：合成负载实测（路径长度取真实值约 190 字符，事件互不相同、无法去重）100 万条时解析约 80 秒、
# 峰值约 3.2 GiB，轨迹 JSON 约 1.1 GiB，读回重放约 8 秒、峰值约 3.8 GiB；50 万条约 1.6／2.4 GiB（完整表见
# tools/official_client_capture/tests/test_ci_read_audit_wp_r2.py 的文档字符串）。
# 版本语义：只对 read-audit-trace/v4 生效，v3 与 control 四链不变；已被旧上限截断的轨迹带 output-event-limit 未解析原因
# （coverage.unresolved>0），验真永远判失败，与这里的数值无关——上限调高不会让任何旧的失败轨迹变成通过。
# read_audit_runtime.py 里有一份同值常量（重放端超限即拒绝），两处必须相等：由 test_ci_audit_hardening_h1 的
# test_parser_and_replay_v4_limits_are_equal 守住（test_ci_read_audit_wp_r2 的 OutputEventLimitTests.test_constants 另有一份同样的断言）。
MAX_OUTPUT_EVENTS_V4 = 1000000

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
                          "listxattr", "llistxattr", "inotify_add_watch", "uselib", "getcwd"})
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
_ANNOTATION = re.compile(r"(AT_FDCWD|-?\d+)<(" + _ANN + r")>")
_ANNOTATED_VALUE = re.compile(r"<" + _ANN + r">")
_QUOTED = re.compile(r'"(' + _ESC + r')"')
# 返回值在行尾：「) = 值[<解析路径>][ 错误码 (说明)]」；从行尾匹配，参数里出现类似字样也不会误判。
_RESULT = re.compile(r"\)\s*=\s*(-?\d+|\?)(?:<" + _ANN + r">)?(?:\s+(E[A-Z0-9]+)(?:\s+\([^)]*\))?)?\s*$")


def _call_arguments(body: str, result: re.Match | None) -> list[str]:
    """保留系统调用参数位置；字符串、描述符、数组及结构里的逗号不分割。

    不从全行捞取引号内容：路径缺失或截断时，后面的 argv、链接结果和扩展属性值不能补位。
    旧范围模式仍可读取 unfinished 前已完整打印的参数；严格模式先按 PID 拼好调用再进入这里。
    """
    text = body[:result.start()] if result is not None else body.removesuffix("<unfinished ...>").rstrip()
    arguments, stack = [], []
    start = index = 0
    while index < len(text):
        char = text[index]
        if char in {'"', '<'}:
            token = (_QUOTED if char == '"' else _ANNOTATED_VALUE).match(text, index)
            if token is None:
                return []
            index = token.end()
            continue
        if char in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return []
        elif char == "," and not stack:
            arguments.append(text[start:index].strip())
            start = index + 1
        index += 1
    if stack:
        return []
    arguments.append(text[start:].strip())
    return arguments


def _path_argument(arguments: Sequence[str], index: int) -> str | None:
    """只接受路径位置上的完整字符串；右引号之后的省略号表示截断，不能当作真实路径。"""
    value = _QUOTED.fullmatch(arguments[index]) if index < len(arguments) else None
    return unescape(value[1]) if value is not None else None


def _argument_flags(body: str) -> set[str]:
    """标志只取字符串和描述符路径之外的字段，文件名不能冒充打开或元数据标志。"""
    arguments = _QUOTED.sub('""', body)
    arguments = re.sub(r"<" + _ANN + r">", "", arguments)
    return set(re.findall(r"\b(?:O|AT)_[A-Z0-9_]+\b", arguments))


def _operation_flags(body: str) -> set[str]:
    """输出归属只消费打开标志，保持原事件格式。"""
    return {flag for flag in _argument_flags(body) if flag.startswith("O_")}


# v4 两路径调用：第一个路径是来源（旧名），第二个是目的（新名）。
_ROLES = ("source", "destination")
# renameat2 标志参数里允许出现的符号。strace 以符号输出，``0`` 表示没有标志；出现其他取值（十六进制残片等）
# 说明标志没有被完整解码，按未解析处理，不猜测。
_RENAME_FLAG_NAMES = frozenset({"RENAME_NOREPLACE", "RENAME_EXCHANGE", "RENAME_WHITEOUT"})


def _rename_flags(arguments: Sequence[str]) -> frozenset[str] | None:
    """renameat2 第 5 个参数里的符号标志；参数个数不对或无法完整解码时返回 None。"""
    if len(arguments) != 5:
        return None
    value = arguments[4].strip()
    if value == "0":
        return frozenset()
    names = value.split("|")
    return frozenset(names) if all(item in _RENAME_FLAG_NAMES for item in names) else None


# WP-R3：v4 事件 flags 里允许出现的 AT_* 标志（白名单）。read_audit_runtime.py 里有一份同值常量（两个文件互不导入），
# 两处必须相等，由 test_ci_read_audit_wp_r3 守住。名单之外的 AT_* 标志（AT_FDCWD 是描述符取值，不算标志）登记
# at-flag-not-decoded 未解析：看不懂的标志可能改变“是否跟随符号链接”之类的语义，不能默默丢掉。
AT_FLAGS_V4 = frozenset({"AT_SYMLINK_NOFOLLOW", "AT_SYMLINK_FOLLOW", "AT_REMOVEDIR", "AT_EMPTY_PATH", "AT_NO_AUTOMOUNT",
                         "AT_EACCESS", "AT_STATX_SYNC_AS_STAT", "AT_STATX_FORCE_SYNC", "AT_STATX_DONT_SYNC"})
_LSTAT_NAMES = frozenset({"lstat", "lstat64"})
_FSTATAT_NAMES = frozenset({"newfstatat", "fstatat64", "statx"})
_SYMLINK_CALLS = frozenset({"symlink", "symlinkat"})
# 带空路径与 AT_EMPTY_PATH 时作用于 dirfd 自己、会改对象元数据的调用（v4 按写登记；基线直接跳过，宿主文件只读打开后用它改时间不留任何 write）。
_EMPTY_PATH_WRITE_CALLS = frozenset({"utimensat", "fchownat", "fchmodat2"})


def _returned_non_link_stat(name: str, arguments: Sequence[str]) -> bool:
    """只认完整返回结构里明确的普通文件或目录，不能从路径、其他参数或缩略结构猜类型。"""
    positions = {"lstat": 1, "lstat64": 1, "newfstatat": 2, "fstatat64": 2, "statx": 4}
    position = positions.get(name)
    if position is None or position >= len(arguments):
        return False
    value = arguments[position].strip()
    if not value.startswith("{") or not value.endswith("}") or "..." in value:
        return False
    fields = _call_arguments(value[1:-1], None)
    key = "stx_mode" if name == "statx" else "st_mode"
    modes = [field for field in fields if re.match(re.escape(key) + r"\s*=", field)]
    return len(modes) == 1 and re.fullmatch(
        re.escape(key) + r"\s*=\s*(?:S_IFREG|S_IFDIR)(?:\|(?:0[0-7]+|S_ISUID|S_ISGID|S_ISVTX))*", modes[0]) is not None


def _returned_link_stat(name: str, arguments: Sequence[str]) -> bool:
    """与 ``_returned_non_link_stat`` 对称：完整返回结构里 ``st_mode``／``stx_mode`` 明确是符号链接（S_IFLNK）。

    只服务于 v4 的“lstat 到本单元自己创建的符号链接”豁免（见 ``TraceParser._tracked_symlink_lstat``）：返回结构必须被完整
    解码（没有省略号、恰好一个 mode 字段、类型就是 S_IFLNK），不能从路径或缩略结构猜类型。
    """
    positions = {"lstat": 1, "lstat64": 1, "newfstatat": 2, "fstatat64": 2, "statx": 4}
    position = positions.get(name)
    if position is None or position >= len(arguments):
        return False
    value = arguments[position].strip()
    if not value.startswith("{") or not value.endswith("}") or "..." in value:
        return False
    fields = _call_arguments(value[1:-1], None)
    key = "stx_mode" if name == "statx" else "st_mode"
    modes = [field for field in fields if re.match(re.escape(key) + r"\s*=", field)]
    return len(modes) == 1 and re.fullmatch(
        re.escape(key) + r"\s*=\s*S_IFLNK(?:\|(?:0[0-7]+|S_ISUID|S_ISGID|S_ISVTX))*", modes[0]) is not None


def _o_path_directory_open(flags: Iterable[str]) -> bool:
    """成功的 ``O_PATH|O_NOFOLLOW`` 打开同时带 ``O_DIRECTORY``：打开的是目录，不是“链接自身”（WP-R4，R6-1，仅 v4 使用）。

    内核语义：``O_DIRECTORY`` 在路径解析末尾核对最后一个分量必须是目录（``path_lookupat`` 的 ``LOOKUP_DIRECTORY`` 检查，
    ``O_PATH`` 路径同样经过它），``O_NOFOLLOW`` 又让最后一个分量是符号链接时不跟随，于是链接名字得到 ``ENOTDIR``；
    调用成功就说明最后一个分量（或结尾带斜杠时被内核跟随到的目标）是目录。这正是 ``tar``、``rm -rf``（fts）、Go 的 ``os.Root``
    这类“逐级安全下降”的惯用写法。能暴露的宿主信息：只有“它是目录”，与普通的打开目录一致；返回描述符注解里的真实路径
    照常登记（结尾斜杠被跟随时，登记的是跟随后的目标目录，需要自己的快照）。

    仍判 symlink-metadata-not-bound 的形态（此函数返回 False 或调用方不走这里）：没有 ``O_DIRECTORY``（可能打开链接自身，
    之后 ``openat(fd, "")`` 等能把描述符转成跟随）；调用失败（本函数只在成功分支被调用，失败的调用不进该分支）；标志没解码
    （openat2 的结构是指针时早就登记 open-flags-not-decoded）。输出归属一侧对私有盘内已知符号链接名字的成功打开另有
    ``_symlink_name_verdict``：O_PATH 的成功打开一律判问题，与本函数互不依赖。
    """
    return "O_DIRECTORY" in flags


def _path_form_hazard(tracker: Any, base: str | None, raw: str) -> bool:
    """原始路径参数的写法，会不会让“文本规范化后的路径”与内核实际解析的对象不是同一个——只在存在符号链接对象时才可能。

    解析器用 ``os.path.normpath`` 把路径规范化成绝对路径，这对没有符号链接的目录树是等价的；一旦有符号链接对象，
    有三种写法会让规范化“骗人”（都是 Linux 的真实语义）：

    * ``link/..``、``a/link/../b``：内核先解引用 link 再取目标的父目录，规范化却直接回到 link 的父目录；
    * ``link/``、``link/.``：结尾的斜杠或 ``.`` 迫使内核解引用最后的链接，哪怕调用本身是 lstat／readlink 这类“不跟随”的调用；
    * 同理 ``link/./x`` 之类经过链接的中间分量，规范化后是 ``…/link/x``，由跟踪器的“中间分量是符号链接”检查拦下，不在这里。

    本函数沿原始写法逐个分量走一遍（起点是 dirfd／工作目录的规范路径或根），遇到 ``..`` 时若当前位置是存活的符号链接名字、
    或结尾带斜杠／``.`` 时当前位置是符号链接名字，就判有风险。返回 True 时调用方登记未解析，整次审计失败关闭。
    """
    if not raw or not tracker.has_symlinks():
        return False
    current = "/" if raw.startswith("/") else base
    if current is None:
        return False
    parts = raw.split("/")
    for component in parts:
        if component in ("", "."):
            continue
        if component == "..":
            if tracker.is_symlink(current):
                return True
            current = os.path.dirname(current) or "/"
        else:
            current = os.path.join(current, component)
    return (raw.endswith("/") or parts[-1] == ".") and tracker.is_symlink(current)


# ---------------------------------------------------------------------------
# WP-R2：以下辅助函数与常量只服务于 v4 轨迹合同下的几处放宽（ARM64 真实环境把正常行为判成未解析的缺口）。
# 每一处放宽都写明“这个系统调用在这种情形下能暴露什么宿主信息”，暴露不了才放宽；解析器里的调用点都以 ``self.trace_v4`` 为
# 前提，v3（含 control 四链与 declared_sampler 复用的默认路径）的行为一字不变。
# ---------------------------------------------------------------------------

# 失败的 getcwd 只带这两个错误码：ERANGE（缓冲区装不下，glibc 会换更大的缓冲区重试，ARM64 实测约 5500 次）与 ENOENT
# （工作目录已被删除）。失败时内核不向缓冲区写入任何路径，缓冲区参数只是指针；两个结果都只取决于调用方自己的参数或进程自己的
# 工作目录状态（目录被删要么是被测程序自己删的——写调用已单独登记，要么是宿主目录漂移——快照比对会拦下），不读取宿主路径信息。
# 其他错误码（实测没有出现）和成功却取不到字符串的 getcwd 仍判未解析。
_GETCWD_BENIGN_ERRNOS = frozenset({"ERANGE", "ENOENT"})
_FD_ANNOTATION = re.compile(r"(\d+)<(" + _ANN + r")>")
_SOCKET_ANNOTATION = re.compile(r"\d+<socket:\[\d+\]>")
_OPEN_FLAG_NAME = re.compile(r"O_[A-Z0-9_]+")
_UNIX_DOMAINS = frozenset({"AF_UNIX", "AF_LOCAL"})
# 只放行 AF_UNIX 流式套接字：它能携带路径的系统调用只有 bind 与 connect（流式套接字用 sendto／sendmsg 带地址，内核不会按该地址查找路径：
# 已连接的得到 EISCONN，未连接的得到 EOPNOTSUPP），
# 而 v4 的 strace 选项已把 bind、connect 都纳入跟踪；数据报套接字可以 sendto 带路径地址、而 sendto 太常见不能逐条跟踪，所以仍判不支持。
_UNIX_STREAM_FLAGS = frozenset({"SOCK_CLOEXEC", "SOCK_NONBLOCK"})


def _futimens_target(arguments: Sequence[str]) -> str | None:
    """``utimensat(fd, NULL, times, 0)``（即 futimens）的作用对象：路径参数为 NULL 时系统调用作用于 fd 本身。

    只认完整形态：四个参数、路径是 NULL、标志是 0（NULL 路径配非零标志内核返回 EINVAL）、第一个参数是真实描述符
    （十进制编号，不是 AT_FDCWD——AT_FDCWD 配 NULL 路径内核得到 EFAULT）并带 ``-y`` 的路径标注，且标注是绝对路径
    （管道、套接字之类的标注不是路径）。返回标注里的路径；任何不符返回 None，由调用方登记未解析。
    暴露的信息：只是对该已打开对象改时间，不读取任何宿主内容；对象路径照常按“写”登记，宿主路径上的写由读集审计另行拦下。
    """
    if len(arguments) != 4 or arguments[1].strip() != "NULL" or arguments[3].strip() != "0":
        return None
    match = _FD_ANNOTATION.fullmatch(arguments[0].strip())
    if match is None:
        return None
    path = unescape(match.group(2))
    if not path.startswith("/") or "\x00" in path:
        return None
    return os.path.normpath(path)


def _openat2_flags(arguments: Sequence[str]) -> frozenset[str] | None:
    """``openat2`` 第三个参数 ``struct open_how`` 解码后 ``flags=`` 字段里的打开标志。

    结构只是指针（strace 未解码）、被省略号截断、没有恰好一个 flags 字段，或标志里有不是 ``O_*`` 符号的片段（十六进制残片等）
    时返回 None——这些情形下标志不可见，不能猜。
    """
    if len(arguments) != 4:
        return None
    value = arguments[2].strip()
    if not value.startswith("{") or not value.endswith("}"):
        return None
    fields = _call_arguments(value[1:-1], None)
    flag_fields = [field for field in fields if re.match(r"flags\s*=", field)]
    if not fields or len(flag_fields) != 1 or any(field == "..." for field in fields):
        return None
    names = flag_fields[0].split("=", 1)[1].strip().split("|")
    if not all(_OPEN_FLAG_NAME.fullmatch(name) for name in names):
        return None
    return frozenset(names)


# openat2 的 resolve= 里“不改变被探测路径”的符号：它们只会让调用更容易失败（BENEATH 拒绝逃出 dirfd 的路径，NO_SYMLINKS／NO_MAGICLINKS
# 拒绝经过符号链接，NO_XDEV 拒绝跨挂载点），成功时解析出的对象与不带它们完全相同。RESOLVE_IN_ROOT 会把 ``..`` 与绝对路径钳在 dirfd
# 之内（解析器按字面规范化就会记错路径），RESOLVE_CACHED 让结果取决于缓存状态——都不在此列。
_OPENAT2_SAFE_RESOLVE = frozenset({"RESOLVE_BENEATH", "RESOLVE_NO_SYMLINKS", "RESOLVE_NO_MAGICLINKS", "RESOLVE_NO_XDEV"})


def _openat2_resolve_ok(arguments: Sequence[str]) -> bool:
    """``openat2`` 的 ``struct open_how`` 里恰好一个 ``resolve=`` 字段，且取值是 ``0`` 或只由安全符号用 ``|`` 连接（不重复）。

    验收缺陷 M-1：``RESOLVE_IN_ROOT`` 的失败调用没有返回描述符的真实路径注解可核对，解析器按字面拼出的路径就会把内核真正探测的
    ``<dirfd>/…`` 宿主路径记成别的（可能恰在私有盘根下的）合法缺失探测。结构只是指针、被省略号截断、缺字段、重复字段、取值含未知符号或
    十六进制残片时一律返回 False——看不清就按失败关闭处理。
    """
    if len(arguments) != 4:
        return False
    value = arguments[2].strip()
    if not value.startswith("{") or not value.endswith("}"):
        return False
    fields = _call_arguments(value[1:-1], None)
    resolve_fields = [field for field in fields if re.match(r"resolve\s*=", field)]
    if not fields or len(resolve_fields) != 1 or any(field == "..." for field in fields):
        return False
    text = resolve_fields[0].split("=", 1)[1].strip()
    if text == "0":
        return True
    names = text.split("|")
    return all(name in _OPENAT2_SAFE_RESOLVE for name in names) and len(set(names)) == len(names)


def _is_unix_stream_socket(arguments: Sequence[str]) -> bool:
    """``socket(AF_UNIX|AF_LOCAL, SOCK_STREAM[|SOCK_CLOEXEC][|SOCK_NONBLOCK], 0)``。

    创建一个本机流式套接字本身不读取任何宿主信息（不碰文件系统路径）；之后携带路径的 bind／connect 都在跟踪里单独判。
    其他域（AF_INET／AF_INET6／AF_NETLINK／AF_PACKET……意味着网络或内核通信）、数据报与其他类型、非零协议一律不在此列。
    """
    if len(arguments) != 3:
        return False
    domain, kind, protocol = (argument.strip() for argument in arguments)
    parts = kind.split("|")
    return (domain in _UNIX_DOMAINS and protocol == "0" and parts[0] == "SOCK_STREAM"
            and set(parts[1:]) <= _UNIX_STREAM_FLAGS and len(set(parts)) == len(parts))


def _connect_unix_path(arguments: Sequence[str]) -> str | None:
    """``connect(fd<socket:[N]>, {sa_family=AF_UNIX, sun_path="/绝对路径"}, len)`` 里的路径。

    要求：描述符带套接字标注、地址结构被完整解码（strace 选项 verbose=…connect）、恰好两个字段、域是 AF_UNIX、sun_path 是
    完整的带引号字符串（抽象套接字 ``@"…"``、被截断、缺字段都不是）、路径是不含 NUL 的绝对路径。不符返回 None。
    """
    if len(arguments) != 3 or _SOCKET_ANNOTATION.fullmatch(arguments[0].strip()) is None or re.fullmatch(r"\d+", arguments[2].strip()) is None:
        return None
    value = arguments[1].strip()
    if not value.startswith("{") or not value.endswith("}"):
        return None
    fields = _call_arguments(value[1:-1], None)
    if len(fields) != 2 or fields[0] not in {"sa_family=" + domain for domain in _UNIX_DOMAINS}:
        return None
    match = re.fullmatch(r'sun_path="(' + _ESC + r')"', fields[1])
    if match is None:
        return None
    path = unescape(match.group(1))
    if not path.startswith("/") or "\x00" in path:
        return None
    return os.path.normpath(path)


class AuditError(RuntimeError):
    """审计配置或轨迹文件不可用。"""


def _runtime_module():
    """从受管同目录加载隔离合同，仓库和驱动副本使用同一入口。"""
    name = "_read_audit_runtime"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE.with_name("read_audit_runtime.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


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

    def __init__(self, *, strict: bool = False, runtime_context: dict[str, Any] | None = None,
                 trace_contract: str = TRACE_CONTRACT_V3) -> None:
        self.cwd: dict[str, str] = {}
        self.accesses: dict[str, set[str]] = {}
        self.samples: dict[str, str] = {}
        self.lines = 0
        self.strict = strict
        self.pending: dict[str, str] = {}
        # 子进程可以先于父进程 clone 的返回行执行；先缓存这段轨迹，得到父子关系后按原顺序重放。
        self.deferred: dict[str, list[str]] = {}
        self.spawn_cwd: dict[str, str | None] = {}
        self.deferred_count = 0
        self.unresolved = 0
        self.unresolved_reasons: dict[str, int] = {}
        self.runtime_context = runtime_context
        self.output_tracker = None
        self.output_events: list[dict[str, Any]] = []
        self.terminal_events: list[dict[str, Any]] = []
        # self.event 必须保持 (调用名, 参数体, 是否成功) 三元组：control 四链的子类直接解包它。
        self.event: tuple[str, str, bool] = ("", "", False)
        # v4 轨迹合同（显式开关，默认 v3）。以下状态只在 v4 下起作用；v3 与 control 子类的路径完全不读它们。
        if trace_contract not in TRACE_CONTRACTS:
            raise AuditError(f"未知的轨迹合同版本：{trace_contract}")
        self.trace_v4 = trace_contract == TRACE_CONTRACT_V4
        if self.trace_v4 and (not strict or runtime_context is None):
            raise AuditError("v4 轨迹合同只用于带隔离合同的严格审计")
        self.call_id = 0                           # 完整系统调用（unfinished／resumed 拼好之后）的递增编号
        self.call_errno: str | None = None         # 当前调用的错误码，成功为 None
        self.call_flags: frozenset[str] = frozenset()   # 当前调用的 RENAME_* 标志（renameat2）
        self.call_end: tuple[str, str | None] | None = None   # 当前正在登记的两路径调用端点：(角色, 另一端路径)
        self.call_link: str | None = None          # 当前 symlink／symlinkat 调用的链接文本（WP-R3，原样记下，不解引用）
        self.epoch = 0                             # 变更纪元：此前使名字空间状态改变的事件个数
        self.seen_events: set[tuple] = set()       # 当前纪元内已出现过的事件键，用于只读事件去重
        if runtime_context is not None:
            if not strict:
                raise AuditError("隔离合同必须使用严格审计")
            runtime = _runtime_module()
            contract = runtime.validate_context(runtime_context, [runtime_context["contract_entry"]])
            self.output_tracker = (runtime.OutputTrackerV4 if self.trace_v4 else runtime.OutputTracker)(contract)

    def _unresolved(self, reason: str) -> None:
        """仅登记调用名与分类，不保存可能含参数、凭据或正文的原始行。"""
        self.unresolved += 1
        self.unresolved_reasons[reason] = self.unresolved_reasons.get(reason, 0) + 1

    def _add(self, path: str | None, kind: str, line: str) -> None:
        if not path:
            if self.strict:
                self._unresolved("path-not-resolved")
            return
        self.accesses.setdefault(path, set()).add(kind)
        self.samples.setdefault(path, line[:SAMPLE_CHARS])
        if self.output_tracker is not None and path == "/dev/tty":
            name, body, succeeded = self.event
            result = _RESULT.search(body)
            self.terminal_events.append({"name": name, "kind": kind, "errno": result.group(2) if result else None,
                                         "succeeded": succeeded})
        if self.output_tracker is not None and _under(path, self.output_tracker.root):
            name, body, succeeded = self.event
            descriptor = re.search(r'\)\s*=\s*\d+<(' + _ANN + r')>\s*$', body)
            target = unescape(descriptor[1]) if descriptor else None
            flags = _operation_flags(body)
            if self.trace_v4:
                if name in _SYMLINK_CALLS:
                    # WP-R3：符号链接创建调用没有返回描述符，事件的 target 字段改存链接文本（原样、不解引用），跟踪器据此记录对象。
                    target = self.call_link
                self._record_output_event(path, kind, name, succeeded, flags, target)
                return
            self.output_tracker.observe(path, kind, name, "|".join(flags), succeeded, target)
            # 仅保存归属重放所需字段，不能把完整执行参数或文件内容写进轨迹。
            if len(self.output_events) < MAX_OUTPUT_EVENTS:
                self.output_events.append({"path": path, "kind": kind, "name": name, "succeeded": succeeded,
                                           "flags": sorted(flags), "target": target})
            else:
                self._unresolved("output-event-limit")

    def _record_output_event(self, path: str, kind: str, name: str, succeeded: bool, flags: set[str],
                             target: str | None) -> None:
        """v4：组装 11 键事件、交给跟踪器，并按变更纪元去重后保留。

        去重规则：事件没有使跟踪器状态改变，且同键事件在当前纪元已出现过，就不再追加（保留首次）。
        同一纪元内名字空间没有任何变化，所以同键的只读事件（反复 stat、反复读同一个文件、反复列目录）对归属判定
        不提供新信息；去掉它们是为了不撑爆事件上限（实测最大样本里 18.4 万条只读事件只有约 2300 种不同事件）。
        使状态改变的事件永不去重，并使纪元加一——事件自身携带的是加一之前的纪元。
        """
        role, peer = self.call_end if self.call_end is not None else (None, None)
        flags = flags | self.call_flags
        event = {"path": path, "kind": kind, "name": name, "succeeded": succeeded, "flags": sorted(flags), "target": target,
                 "errno": self.call_errno, "call": self.call_id, "role": role, "peer": peer, "epoch": self.epoch}
        if self.output_tracker.observe_event(event):
            self.epoch += 1
            self.seen_events.clear()
        else:
            key = (path, kind, name, succeeded, self.call_errno, tuple(event["flags"]), target, role, peer, self.epoch)
            if key in self.seen_events:
                return
            self.seen_events.add(key)
        # 仅保存归属重放所需字段，不能把完整执行参数或文件内容写进轨迹。v4 用自己的上限（见常量处的版本语义说明）。
        if len(self.output_events) < MAX_OUTPUT_EVENTS_V4:
            self.output_events.append(event)
        else:
            self._unresolved("output-event-limit")

    def feed(self, raw: str) -> None:
        self.lines += 1
        line = raw.rstrip("\n")
        match = _LINE.match(line)
        pid, rest = (match.group(1), match.group(2)) if match else ("0", line)
        if self.strict and pid in self.deferred:
            self._defer(pid, line)
            return
        resumed = _RESUMED.match(rest)
        if resumed is not None:
            if self.strict:
                original = self.pending.pop(pid, None)
                if original is None:
                    self._unresolved("resume-without-start:" + resumed.group(1))
                else:
                    self.feed(pid + " " + original + rest[resumed.end():])
                    self.lines -= 1
                return
            if resumed.group(1) in _SPAWN:
                child = _RESULT.search(rest)
                if child and child.group(1).isdigit() and pid in self.cwd:
                    self.cwd.setdefault(child.group(1), self.cwd[pid])
            return
        if rest.startswith(("---", "+++")):
            return
        call = _CALL.match(rest)
        if call is None:
            if self.strict and rest.strip():
                self._unresolved("unrecognized-line")
            return
        name, body = call.group(1), call.group(2)
        unfinished = body.rstrip().endswith("<unfinished ...>")
        if self.strict and unfinished:
            if pid in self.pending:
                self._unresolved("overlapping-unfinished:" + name)
            self.pending[pid] = rest[:rest.rfind("<unfinished ...>")]
            if name in _SPAWN:
                self.spawn_cwd[pid] = self.cwd.get(pid)
            return
        result = None if unfinished else _RESULT.search(body)
        if self.strict and (result is None or re.search(r'"\s*\.\.\.', body)):
            self._unresolved("result-or-string-incomplete:" + name)
        errno = result.group(2) if result else None
        self.event = (name, body, result is not None and errno is None and result.group(1) != "-1")
        arguments = _call_arguments(body, result)
        # v4 逐调用状态：每个完整调用一个递增编号，同一调用产生的所有事件共享它；错误码与 renameat2 标志也在这里取好。
        self.call_id += 1
        self.call_errno = errno
        self.call_flags = frozenset()
        self.call_end = None
        self.call_link = None
        if self.trace_v4 and name == "renameat2":
            decoded = _rename_flags(arguments)
            if decoded is None:
                self._unresolved("rename-flags-not-decoded:" + name)
            else:
                self.call_flags = decoded
        if self.trace_v4:
            # WP-R3：事件要带上调用的 AT_* 标志（lstat 与 stat、unlinkat 的 AT_REMOVEDIR 都靠它区分）。AT_FDCWD 是描述符取值，
            # 不是标志；名单之外的 AT_* 标志看不懂，登记未解析而不是默默丢掉。
            at_flags = frozenset(flag for flag in _argument_flags(body) if flag.startswith("AT_") and flag != "AT_FDCWD")
            if at_flags - AT_FLAGS_V4:
                self._unresolved("at-flag-not-decoded:" + name)
            self.call_flags = self.call_flags | (at_flags & AT_FLAGS_V4)
            if name in _SYMLINK_CALLS:
                self.call_link = _path_argument(arguments, 0)
                if self.call_link is None:
                    self._unresolved("symlink-text-not-decoded:" + name)
        if self.strict and result and result.group(1).isdigit() and errno is None:
            flags = _argument_flags(body)
            # v2 快照只绑定路径解析所需的链接属性，不绑定正常解析可能刷新的链接访问时间。
            # NOFOLLOW 也会用于普通对象；完整返回结构明确非链接且返回 0 时，沿用普通 stat 精确快照。
            # 真链接、未知或缩略类型，以及链接自身的描述符仍缺合同，必须拒绝承接。
            nofollow_stat = (name in {"lstat", "lstat64"}
                             or (name in {"newfstatat", "fstatat64", "statx"} and "AT_SYMLINK_NOFOLLOW" in flags))
            # WP-R4（R6-1，仅 v4）：成功的 O_PATH|O_NOFOLLOW|O_DIRECTORY 打开不是“链接自身的元数据”——见 ``_o_path_directory_open``。
            o_path_nofollow = (name in {"open", "openat", "openat2"} and {"O_PATH", "O_NOFOLLOW"} <= flags
                               and not (self.trace_v4 and _o_path_directory_open(flags)))
            if ((nofollow_stat and not (result.group(1) == "0" and _returned_non_link_stat(name, arguments))) or o_path_nofollow):
                # WP-R3（仅 v4）：lstat 到“本单元自己创建、跟踪器当前仍记着的符号链接”不登记 symlink-metadata-not-bound——
                # 那是链接自己的元数据，内容由被测代码自己写入，没有宿主依赖。宿主上的真链接、不在私有盘内的、不是本单元创建的
                # 一律仍登记。O_PATH|O_NOFOLLOW 的打开不豁免（得到的描述符后续可被 openat(fd, "") 等转成跟随，不可见）。
                if not (self.trace_v4 and nofollow_stat and self._tracked_symlink_lstat(name, arguments, pid, result.group(1))):
                    self._unresolved("symlink-metadata-not-bound:" + name)
            elif self.trace_v4 and nofollow_stat and self._single_path_is_tracked_symlink(name, arguments, pid):
                # 反方向的互相印证：内核说这是普通文件或目录（完整解码、返回 0），跟踪器却记着它是符号链接——名字表与真实状态已经脱节
                # （例如链接被某个没观察到的路径替换）。不能继续相信后面基于名字表的任何判定，登记未解析。
                self._unresolved("symlink-state-contradicts-stat:" + name)
        for argument in arguments:
            annotation = _ANNOTATION.fullmatch(argument)
            if annotation is None:
                continue
            if annotation.group(1) == "AT_FDCWD":
                directory = unescape(annotation.group(2))
                if directory.startswith("/"):
                    self.cwd[pid] = directory
        if name in _SPAWN:
            if self.strict and re.search(r"\bCLONE_(?:UNTRACED|NEWNS)\b", body):
                self._unresolved("unobservable-child:" + name)
            inherited = self.spawn_cwd.pop(pid, self.cwd.get(pid))
            if result and result.group(1).isdigit() and int(result.group(1)) > 0 and inherited:
                child = result.group(1)
                self.cwd.setdefault(child, inherited)
                buffered = self.deferred.pop(child, [])
                self.deferred_count -= len(buffered)
                for event in buffered:
                    self.feed(event)
                    self.lines -= 1
            return
        if name == "fchdir":
            annotation = _ANNOTATION.fullmatch(arguments[0]) if arguments else None
            if annotation and unescape(annotation[2]).startswith("/") and errno is None:
                directory = unescape(annotation[2])
                self.cwd[pid] = directory
                self._add(directory, "dir", line)
            elif self.strict:
                self._unresolved("directory-fd-not-resolved:fchdir")
            return
        # 目录枚举只认 getdents64。宿主快照 v3 只对「出现过 list 访问」的目录绑定直接目录项（见下文 v3 一节），而 list 访问只
        # 来自这里的 getdents64 事件；strace 选项（见 strace_argv）也只跟踪 getdents64。这个前提的影响：
        #   * ARM64 没有 getdents（非 64）这个系统调用，所以当前运行环境不受影响；
        #   * x86_64 上旧 libc 或静态链接的程序可能用 getdents 枚举目录：strace 的 trace 列表里没有它，不会输出这些调用行，
        #     解析器看不到，目录项就不会被绑定——这是「观察不到」（漏观察），不是失败关闭；
        #   * 要在 x86_64 上审计，需要同时做两件事：把 getdents 加进 strace 的 trace 列表，并在这里把它与 getdents64 同样处理
        #     （现在解析器若收到 getdents 行，会按「不支持的调用」登记 unsupported-call:getdents，整条轨迹失败关闭）。
        if name == "getdents64":
            annotation = _ANNOTATION.fullmatch(arguments[0]) if arguments else None
            if annotation:
                path = unescape(annotation.group(2))
                if path.startswith("/"):
                    self._add(os.path.normpath(path), "list", line)
                elif self.strict:
                    self._unresolved("directory-fd-not-resolved")
            elif self.strict:
                self._unresolved("directory-fd-not-annotated")
            return
        failed = errno is not None
        if name in _DIRFD_READ or name in _DIRFD_PROBE or name in _DIRFD_WRITE or name in _DIRFD_EXEC:
            if self.trace_v4 and name == "utimensat":
                # WP-R2 第 2 项：futimens(fd) 在系统调用层是 utimensat(fd, NULL, times, 0)，作用对象就是 fd 自己，路径参数为
                # NULL。按“对该描述符标注路径的写（改时间）”登记，kind 与带路径的 utimensat 一致，输出归属跟踪器照对已知对象的
                # 元数据写处理；没有路径标注的 fd、AT_FDCWD 配 NULL 等异常形态不在此列，落到下面的未解析判定。
                target_path = _futimens_target(arguments)
                if target_path is not None:
                    self._add(target_path, "write", line)
                    return
            positions = (1,) if name == "symlinkat" else (0, 2) if name in _TWO_PATHS else (0,)
            pairs = []
            for slot, position in enumerate(positions):
                directory = _ANNOTATION.fullmatch(arguments[position]) if position < len(arguments) else None
                path = _path_argument(arguments, position + 1)
                if directory is not None and path is not None:
                    pairs.append((slot, unescape(directory[2]), path))
            # v4：两路径调用要把两端的规范路径都算出来，哪怕另一端在盘外也要作为 peer 记下；
            # 空路径（AT_EMPTY_PATH）或无法还原的一端没有路径，peer 记 None。
            ends = None
            if self.trace_v4 and name in _TWO_PATHS:
                resolved = {slot: _join(directory, path) for slot, directory, path in pairs if path != ""}
                ends = [resolved.get(0), resolved.get(1)]
            missing = errno in _MISSING_ERRNOS
            if name in _DIRFD_READ:
                flags = _operation_flags(body)
                kind = ("write" if _WRITE_FLAGS.search("|".join(flags)) else "missing" if missing else "stat" if failed
                        else "dir" if "O_DIRECTORY" in flags else "read")
                if self.strict and name == "openat2":
                    if self.trace_v4:
                        # WP-R2 第 3 项：v4 的 strace 选项解码 ``struct open_how``，标志只认 ``flags=`` 字段，并要求与整行扫描
                        # 出的打开标志一致（两者不一致或结构没解码出来 = 标志不可见，判未解析）；v3 沿用“整行没有任何标志”的旧判据。
                        # 验收 M-1：``resolve=`` 也必须看得清且不改变路径解析（见 _openat2_resolve_ok），否则同样判未解析。
                        if _openat2_flags(arguments) != frozenset(flags) or not _openat2_resolve_ok(arguments):
                            self._unresolved("open-flags-not-decoded:openat2")
                    elif not flags:
                        self._unresolved("open-flags-not-decoded:openat2")
            elif name in _DIRFD_WRITE:
                kind = "write"
            elif name in _DIRFD_EXEC:
                kind = "missing" if missing else "stat" if failed else "exec"
            else:
                kind = "missing" if missing else "stat"
            for slot, directory, path in pairs:
                if path == "":
                    if self.trace_v4 and name in _EMPTY_PATH_WRITE_CALLS and "AT_EMPTY_PATH" in self.call_flags:
                        # 对 dirfd 自己的元数据写（utimensat／fchownat／fchmodat2 的 AT_EMPTY_PATH 形态，与 futimens 同类）：按对标注路径的写登记。
                        # 标注必须是绝对路径（AT_FDCWD 的标注就是工作目录）；管道、套接字之类的标注不是路径，看不清作用对象，判未解析。
                        if directory.startswith("/") and "\x00" not in directory:
                            self._add(os.path.normpath(directory), "write", line)
                        else:
                            self._unresolved("empty-path-fd-not-a-path:" + name)
                    continue  # AT_EMPTY_PATH：对已打开的文件描述符操作，打开那一下已经记过（读类调用沿用基线；上面的写类调用 v4 另行登记）
                if ends is not None:
                    self.call_end = (_ROLES[slot], ends[1 - slot])
                self._add(self._checked_join(directory, path), kind, line)
            self.call_end = None
            if self.strict:
                if len(pairs) != len(positions):
                    self._unresolved("dirfd-path-not-resolved:" + name)
                # 返回描述符展示解引用后的真实路径，防止用树内链接掩盖宿主机读取。
                target = re.search(r'\)\s*=\s*\d+<(' + _ANN + r')>\s*$', body)
                if target and name in _DIRFD_READ:
                    self._add(_join(None, unescape(target[1])), kind, line)
                elif name in _DIRFD_READ and not failed:
                    self._unresolved("opened-fd-not-resolved:" + name)
            return
        if name in _PLAIN_READ or name in _PLAIN_PROBE or name in _PLAIN_WRITE or name in _PLAIN_EXEC:
            positions = (1,) if name in {"symlink", "inotify_add_watch"} else (0, 1) if name in _TWO_PATHS else (0,)
            strings = [path for position in positions if (path := _path_argument(arguments, position)) is not None]
            if self.trace_v4 and name == "getcwd" and failed and not strings and errno in _GETCWD_BENIGN_ERRNOS:
                # WP-R2 第 1 项：失败的 getcwd（ERANGE／ENOENT）缓冲区参数是指针不是字符串，内核没有写出任何路径，不读取宿主
                # 路径信息：不登记访问、不登记未解析，也不更新工作目录。成功却取不到字符串的 getcwd 仍落到下面的未解析判定。
                return
            if self.strict and pid not in self.cwd and any(path and not path.startswith("/") for path in strings):
                self._defer(pid, line)
                return
            missing = errno in _MISSING_ERRNOS
            if name in _PLAIN_READ:
                flags = _operation_flags(body)
                kind = ("write" if _WRITE_FLAGS.search("|".join(flags)) else "missing" if missing else "stat" if failed
                        else "dir" if "O_DIRECTORY" in flags else "read")
            elif name in _PLAIN_WRITE:
                kind = "write"
            elif name in _PLAIN_EXEC:
                kind = "missing" if missing else "stat" if failed else "exec"
            elif name in {"chdir", "getcwd"}:
                kind = "missing" if missing else "dir"
            else:
                kind = "missing" if missing else "stat"
            # v4：strings 丢掉了解析失败的位置，这里单独记下每个字符串对应的端点序号，并算出两端的规范路径。
            slots = ends = None
            if self.trace_v4 and name in _TWO_PATHS:
                texts = [_path_argument(arguments, position) for position in positions]
                slots = [slot for slot, text in enumerate(texts) if text is not None]
                ends = [_join(self.cwd.get(pid), text) if text else None for text in texts]
            for index, path in enumerate(strings):
                if ends is not None:
                    self.call_end = (_ROLES[slots[index]], ends[1 - slots[index]])
                self._add(self._checked_join(self.cwd.get(pid), path), kind, line)
            self.call_end = None
            if self.strict:
                if len(strings) != len(positions):
                    self._unresolved("plain-path-not-resolved:" + name)
                target = re.search(r'\)\s*=\s*\d+<(' + _ANN + r')>\s*$', body)
                if target and name in _PLAIN_READ:
                    self._add(_join(None, unescape(target[1])), kind, line)
                elif name in _PLAIN_READ and not failed:
                    self._unresolved("opened-fd-not-resolved:" + name)
            if name in {"chdir", "getcwd"} and not failed and strings:
                joined = _join(self.cwd.get(pid), strings[0])
                if joined:
                    self.cwd[pid] = joined
            return
        if self.trace_v4 and name == "socket" and _is_unix_stream_socket(arguments):
            # WP-R2 第 4 项（一）：创建本机流式套接字本身不暴露宿主信息，不登记未解析；携带路径的 bind／connect 另行判定
            # （v4 的 strace 选项把 bind 也纳入跟踪，bind 落到下面的 unsupported-call:bind）。
            return
        if self.trace_v4 and name == "connect" and errno == "ENOENT":
            # WP-R2 第 4 项（二）：连接 AF_UNIX 绝对路径得到 ENOENT（glibc 的 nscd／NSS 探测）。结果取决于该宿主路径是否存在，
            # 所以登记为对该路径的“missing”探测访问，由策略的宿主路径声明决定通过与否。成功、ECONNREFUSED、EACCES 等任何其他
            # 结果（意味着与外部进程通信或权限探测）、相对路径、抽象套接字、网络地址都不进这里，保持 unsupported-call:connect。
            socket_path = _connect_unix_path(arguments)
            if socket_path is not None:
                self._add(socket_path, "missing", line)
                return
        if self.strict:
            self._unresolved("unsupported-call:" + name)

    def _checked_join(self, directory: str | None, path: str) -> str | None:
        """``_join`` 加 v4 的符号链接路径形态检查：存在符号链接对象时，``..``／结尾斜杠等写法可能让规范化后的路径
        与内核实际解析的对象不同，见 ``_path_form_hazard``。路径照常返回（事件照常记录），同时登记未解析让整次审计失败关闭。"""
        joined = _join(directory, path)
        if (self.trace_v4 and joined is not None and self.output_tracker.has_symlinks()
                and _path_form_hazard(self.output_tracker, directory, path)):
            self._unresolved("symlink-path-form-unsafe")
        return joined

    def _single_path(self, name: str, arguments: Sequence[str], pid: str) -> str | None:
        """lstat 一类单路径调用的规范路径（与后面通用分支的算法一致）；空路径（AT_EMPTY_PATH）或无法还原时返回 None。"""
        if name in _DIRFD_PROBE:
            directory = _ANNOTATION.fullmatch(arguments[0]) if arguments else None
            path = _path_argument(arguments, 1)
            if directory is None or not path:
                return None
            return _join(unescape(directory[2]), path)
        path = _path_argument(arguments, 0)
        return _join(self.cwd.get(pid), path) if path else None

    def _single_path_is_tracked_symlink(self, name: str, arguments: Sequence[str], pid: str) -> bool:
        path = self._single_path(name, arguments, pid)
        return (path is not None and self.output_tracker is not None and _under(path, self.output_tracker.root)
                and self.output_tracker.is_symlink(path))

    def _tracked_symlink_lstat(self, name: str, arguments: Sequence[str], pid: str, returned: str) -> bool:
        """lstat 一类调用成功得到“链接自己的元数据”，且该链接正是本单元在私有盘内创建、跟踪器仍记着的符号链接。

        条件全部满足才豁免 symlink-metadata-not-bound：返回值是 0、返回结构被完整解码且 st_mode 明确是符号链接、
        路径落在私有盘内、跟踪器此刻把这个名字登记为符号链接对象。这样跟踪器的认识与内核的返回互相印证——跟踪器说是链接而
        内核说是普通文件（或反之）、链接在宿主上、链接不是本单元创建的，都不满足，保持原判。路径写法有风险
        （``link/`` 迫使内核解引用）的情形由 ``_checked_join`` 另行登记。
        """
        if returned != "0" or self.output_tracker is None or not _returned_link_stat(name, arguments):
            return False
        path = self._single_path(name, arguments, pid)
        return path is not None and _under(path, self.output_tracker.root) and self.output_tracker.is_symlink(path)

    def _defer(self, pid: str, line: str) -> None:
        """等待父进程归属，限制缓存量；超过上限仍拒绝覆盖，不猜测工作目录。"""
        if self.deferred_count >= 10000:
            self._unresolved("deferred-process-limit")
            return
        self.deferred.setdefault(pid, []).append(line)
        self.deferred_count += 1


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def trace_document(parser: TraceParser, roots: Sequence[str]) -> dict[str, Any]:
    """只留各个根之下的路径（字节码缓存不留），写成紧凑文档。"""

    roots = [os.path.normpath(str(root)) for root in roots]
    kept = {}
    for path, kinds in parser.accesses.items():
        if any(_under(path, root) for root in roots) and (parser.strict or not _bytecode(path)):
            kept[path] = sorted(kinds)
    value = {"schema_version": STRICT_TRACE_SCHEMA if parser.strict else TRACE_SCHEMA, "roots": roots, "lines": parser.lines,
            "accesses": [[path, kept[path]] for path in sorted(kept)],
            "samples": {path: (path if parser.strict else parser.samples[path]) for path in sorted(kept)}}
    if parser.strict:
        value["coverage"] = {"all_host_paths": roots == ["/"], "unresolved": parser.unresolved + len(parser.pending) + parser.deferred_count,
                             "stream_complete": True, "unresolved_reasons": dict(sorted(parser.unresolved_reasons.items())),
                             "unfinished_calls": len(parser.pending), "unresolved_process_events": parser.deferred_count}
    if parser.output_tracker is not None:
        # 版本由解析器的显式开关决定；control 子类走 v3，不会因为本模块新增 v4 而改变产出。
        value["schema_version"] = RUNTIME_TRACE_SCHEMA_V4 if parser.trace_v4 else RUNTIME_TRACE_SCHEMA
        value["runtime"] = {"context": parser.runtime_context, "output_events": parser.output_events,
                            "outputs": parser.output_tracker.report(), "terminal_events": parser.terminal_events}
    return value


def _bytecode(path: str) -> bool:
    return "/__pycache__/" in f"/{path}/" or path.endswith((".pyc", ".pyo"))


def filter_stream(stream: Iterable[str], roots: Sequence[str], *, strict: bool = False,
                  runtime_context: dict[str, Any] | None = None, trace_contract: str = TRACE_CONTRACT_V3) -> dict[str, Any]:
    parser = TraceParser(strict=strict, runtime_context=runtime_context, trace_contract=trace_contract)
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


def strace_argv(argv: Sequence[str], *, output: Path, roots: Sequence[str], python: str | None = None,
                strict: bool = False, runtime_context: Path | None = None, trace_contract: str = TRACE_CONTRACT_V3) -> list[str]:
    """把一个单元的命令包在 strace 下：输出先经 grep 预筛，再交给本模块 ``filter``，只留各个根之下的路径。

    ``trace_contract`` 是 v4 的显式开关：默认 v3 时命令行与历史完全相同；只有传 ``v4`` 才给过滤器追加
    ``--trace-contract v4``（且必须同时是带隔离合同的严格审计）。
    """

    if trace_contract not in TRACE_CONTRACTS:
        raise AuditError(f"未知的轨迹合同版本：{trace_contract}")
    if trace_contract != TRACE_CONTRACT_V3 and (not strict or runtime_context is None):
        raise AuditError("v4 轨迹合同只用于带隔离合同的严格审计")
    command = [python or sys.executable, "-B", str(HERE), "filter", "--output", str(output)]
    if strict:
        roots = ["/"]
        command.append("--strict")
    if runtime_context is not None:
        command += ["--runtime-context", str(runtime_context)]
    if trace_contract != TRACE_CONTRACT_V3:
        command += ["--trace-contract", trace_contract]
    for root in roots:
        command += ["--root", str(root)]
    for part in command:
        if any(ch.isspace() or ch in "'\"\\$`|;&<>" for ch in part):
            raise AuditError(f"审计输出命令里的路径带空白或 shell 特殊字符，不能交给 strace 的管道：{part}")
    pattern = prefilter_pattern(roots)
    if "'" in pattern:
        raise AuditError("预筛表达式里有单引号，不能交给 shell")
    pipe = " ".join(command) if strict else f"grep --line-buffered -E '{pattern}' | " + " ".join(command)
    # 文件描述符切目录、命名空间变更和未建模的外部通信必须可见；未知调用保持失败关闭。
    # 目录枚举只跟踪 getdents64：目录项只在出现 getdents64 枚举事件时绑定（见解析器里 getdents64 分支的注释）。ARM64 没有
    # getdents（非 64）系统调用，当前环境不受影响；x86_64 上旧 libc 或静态链接程序用 getdents 枚举目录时，这里观察不到。
    verbose = "verbose=%%stat"
    traced = ("trace=%file,getdents64,clone,clone3,fork,vfork,fchdir,unshare,setns,"
              "open_by_handle_at,io_uring_setup,socket,connect,accept,accept4,sendmsg,recvmsg")
    if trace_contract == TRACE_CONTRACT_V4:
        # WP-R2：只加在 v4 合同上，v3 的命令行逐字节不变（由黄金值测试守住）。
        #   * 解码 openat2 的 ``struct open_how``（第 3 项，否则标志是指针、不可见）与 connect 的 ``sockaddr``（第 4 项，
        #     否则目的地是指针、不可见）；必须与 %%stat 合在同一个 verbose= 限定里，不依赖多个同类 -e 的叠加／覆盖语义；
        #   * 跟踪 bind：socket(AF_UNIX 流式) 放行后，携带路径的调用里 bind（会在宿主路径建套接字文件，或以 EADDRINUSE 暴露
        #     路径存在与否）原先因 socket 整体不支持而被间接拦住，现在必须自己可见、落到 unsupported-call:bind。
        verbose += ",openat2,connect"
        traced += ",bind"
    options = ["-s", "4096", "-e", verbose, "-e", "abbrev=!%%stat", "-e", traced] if strict else []
    return ["strace", *STRACE_OPTIONS, *options, "-o", "|" + pipe, "--", *argv]


def host_snapshot(raw: str) -> dict[str, Any]:
    """按精确路径登记宿主输入；目录只绑定直接目录项，不能代表递归内容覆盖。"""
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or str(path) != raw:
        raise AuditError("宿主输入必须是规范绝对路径")
    if any(_under(raw, root) for root in ("/proc", "/sys", "/dev")):
        raise AuditError("虚拟或设备路径不能冒充稳定文件快照")
    links = []
    for item in reversed((path, *path.parents)):
        if item.is_symlink():
            link_target = os.readlink(item)
            info = item.lstat()
            links.append({"path": str(item), "target": link_target, "mode": info.st_mode,
                          "uid": info.st_uid, "gid": info.st_gid, "device": info.st_dev, "inode": info.st_ino,
                          "size": info.st_size,
                          "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns})
    target = path.resolve()
    if any(_under(str(target), root) for root in ("/proc", "/sys", "/dev")):
        raise AuditError("符号链接指向虚拟或设备路径，不能作为稳定文件快照")
    try:
        info = target.stat()
    except (FileNotFoundError, NotADirectoryError):
        payload = {"kind": "missing", "target": str(target), "links": links}
    else:
        if stat.S_ISREG(info.st_mode):
            with target.open("rb") as stream:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            content = hasher.hexdigest()
            after = target.stat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise AuditError("宿主文件在快照期间变化")
            payload = {"kind": "file", "content_sha256": content}
            info = after
        elif stat.S_ISDIR(info.st_mode):
            payload = {"kind": "directory", "entries": sorted(item.name for item in target.iterdir())}
            info = target.stat()
        else:
            raise AuditError("宿主输入不是可绑定的普通文件、目录或缺失路径")
        payload.update(target=str(target), links=links, metadata={
            "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid, "size": info.st_size,
            "device": info.st_dev, "inode": info.st_ino, "atime_ns": info.st_atime_ns,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns})
    ancestors = []
    for parent in sorted(set(path.parents) | set(target.parents), key=str):
        try:
            info = parent.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        ancestors.append({"path": str(parent), "mode": info.st_mode, "uid": info.st_uid, "gid": info.st_gid,
                          "link": os.readlink(parent) if parent.is_symlink() else None})
    payload["ancestors"] = ancestors
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"category": "environment", "name": "host:" + raw, "sha256": hashlib.sha256(encoded).hexdigest(),
            "detail": {"path": raw, "schema_version": HOST_SCHEMA, "snapshot": payload}}


# ---------------------------------------------------------------------------
# 宿主快照 v3：按单元实际访问方式比较
#
# 为什么要有：v2 快照整份相等，里面含 atime／mtime／ctime／inode，目录还绑全部直接条目。宿主上的监控程序在根目录写一个
# 文件、relatime 的 24 小时 atime 刷新、别的进程读了该文件，都会让“只 stat 了根目录判类型”的单元整次作废（10-14 ARM64
# 公共后台因此重起四轮）。这些变化对该单元的结果没有影响，却让审计依赖“宿主绝对安静”。
#
# 安全论证：单元的结果只取决于它实际观察到的信息。路径级轨迹（strace）告诉我们每个路径被怎样访问：
#   * 读内容或执行（read／exec）→ 观察了文件内容；
#   * 枚举目录（list，即 getdents64）→ 观察了目录的全部直接条目；
#   * 只 stat／打开目录／读目录（stat／dir／read 于目录）→ 只观察了类型、权限、属主、（文件）大小，以及路径解析经过的
#     符号链接和祖先目录；
#   * 访问不存在的路径（missing）→ 观察了“不存在”。
# 没被观察的方面变了，单元的执行轨迹不可能不同，所以不必比较；被观察的方面一个字节都不放松。
#
# 这个论证对“时间字段、inode、device”不成立：轨迹里看不到 fstat(fd)（打开那一下已经记过，后面的 fstat 不再出现），也
# 分不清程序从 stat 结果里到底用了哪个字段。所以对这些字段采用的是“策略性放宽”，不是证明：默认认为单元不依赖它们，依赖
# 的必须在审计策略里逐路径声明（metadata_dependencies），并附源码或调用合同的审核依据。典型不成立的情形：make／find
# -newer／rsync 这类按时间比较的工具，以 mtime 做缓存键的程序，断言 mtime 的测试。“stat 路径集合一致”只证明路径覆盖，
# 不证明字段依赖。atime 尤其不可靠：快照自己读文件、列目录就会刷新它（relatime 下超过 24 小时即刷新），声明它基本不可满足。
#
# 条目的 sha256 只绑“无条件比较集”（kind／target／links／ancestors／core，加上被声明的易变字段及其值）。内容摘要、目录项
# 和 volatile 不进摘要：承接判定先比 inputs_sha256，摘要若含这些，噪声会在语义比较之前就让承接失败。代价是摘要相等不再
# 蕴含内容相等，所以承接必须经过 unit_records.check_record 里的 host_inputs_inherit_problems 按访问方式补比。
#
# v2 的常量、host_snapshot 与 strict_audit_reads 对 v2 条目的行为一概不动：control 四链 sidecar 动态复用本模块的函数对象，
# 继续产出并重放 v2；v3 只由下面这些新名字显式引入，调用点按“是不是 control 适配器”选择（unit_records.new_host_snapshot）。
# ---------------------------------------------------------------------------

HOST_SCHEMA_V3 = "read-audit-host-snapshot/v3"
# 历史重放与新生产都认的宿主快照合同：v2 条目走旧逻辑，v3 条目走按访问方式比较的新逻辑。
HOST_SCHEMAS = frozenset({HOST_SCHEMA, HOST_SCHEMA_V3})
# 易变元数据（volatile 分组的全部字段，也是策略唯一允许声明的依赖字段）。
VOLATILE_FIELDS = ("atime_ns", "mtime_ns", "ctime_ns", "inode", "device")
# 触发比较文件内容的访问类型 / 触发比较目录项的访问类型。
CONTENT_ACCESS_KINDS = frozenset({"read", "exec"})
LIST_ACCESS_KINDS = frozenset({"list"})
_V3_LINK_FIELDS = ("path", "target", "mode", "uid", "gid", "size")
_V3_ANCESTOR_FIELDS = ("path", "mode", "uid", "gid", "link")


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_int(value: Any) -> bool:
    return type(value) is int


def normalize_metadata_dependency(value: Any) -> dict[str, Any] | None:
    """校验并规范化一个路径的元数据依赖声明 ``{fields, basis}``：fields 是易变字段的非空子集（去重后排序），basis 是非空
    字符串（源码位置或调用合同的审核依据）。None 表示没有声明。"""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"fields", "basis"}:
        raise AuditError("元数据依赖声明须恰好含 fields 与 basis")
    fields, basis = value["fields"], value["basis"]
    if (not isinstance(fields, (list, tuple)) or not fields or not all(isinstance(item, str) for item in fields)
            or len(set(fields)) != len(fields) or not set(fields) <= set(VOLATILE_FIELDS)):
        raise AuditError("元数据依赖的字段须是 " + "／".join(VOLATILE_FIELDS) + " 的非空无重复子集")
    if not isinstance(basis, str) or not basis.strip():
        raise AuditError("元数据依赖须附非空的审核依据（源码位置或调用合同）")
    return {"fields": sorted(fields), "basis": basis}


def _check_v3_payload(payload: Any) -> None:
    """v3 载荷的结构校验：字段集合、类型都对才交给比较函数，篡改或残缺的封存内容一律抛 AuditError，不让比较函数撞上
    KeyError／TypeError。"""

    if not isinstance(payload, Mapping):
        raise AuditError("宿主快照 v3 载荷不是对象")
    kind = payload.get("kind")
    expected = {"kind", "target", "links", "ancestors"}
    if kind in ("file", "directory"):
        expected |= {"core", "volatile", "content_sha256" if kind == "file" else "entries"}
    elif kind != "missing":
        raise AuditError("宿主快照 v3 的类型不是文件、目录或缺失")
    if set(payload) != expected:
        raise AuditError("宿主快照 v3 的字段集合不符")
    if not isinstance(payload["target"], str) or not payload["target"].startswith("/"):
        raise AuditError("宿主快照 v3 的解析目标不是绝对路径")
    if (not isinstance(payload["links"], list)
            or not all(isinstance(link, Mapping) and set(link) == set(_V3_LINK_FIELDS) and isinstance(link["path"], str)
                       and isinstance(link["target"], str) and all(_is_int(link[key]) for key in ("mode", "uid", "gid", "size"))
                       for link in payload["links"])):
        raise AuditError("宿主快照 v3 的符号链接链格式不符")
    if (not isinstance(payload["ancestors"], list)
            or not all(isinstance(item, Mapping) and set(item) == set(_V3_ANCESTOR_FIELDS) and isinstance(item["path"], str)
                       and all(_is_int(item[key]) for key in ("mode", "uid", "gid"))
                       and (item["link"] is None or isinstance(item["link"], str)) for item in payload["ancestors"])):
        raise AuditError("宿主快照 v3 的祖先目录格式不符")
    if kind == "missing":
        return
    core_keys = {"mode", "uid", "gid"} | ({"size"} if kind == "file" else set())
    core, volatile = payload["core"], payload["volatile"]
    if (not isinstance(core, Mapping) or set(core) != core_keys or not all(_is_int(value) for value in core.values())):
        raise AuditError("宿主快照 v3 的核心属性格式不符")
    if (not isinstance(volatile, Mapping) or set(volatile) != set(VOLATILE_FIELDS)
            or not all(_is_int(value) for value in volatile.values())):
        raise AuditError("宿主快照 v3 的易变元数据格式不符")
    if kind == "file":
        if not isinstance(payload["content_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", payload["content_sha256"]) is None:
            raise AuditError("宿主快照 v3 的内容摘要格式不符")
    elif not isinstance(payload["entries"], list) or not all(isinstance(item, str) for item in payload["entries"]):
        raise AuditError("宿主快照 v3 的目录项格式不符")


def _v3_binding(payload: Mapping[str, Any], dependency: Mapping[str, Any] | None) -> dict[str, Any]:
    """条目 sha256 的输入：无条件比较集。内容摘要、目录项、volatile 都不在里面（原因见本节说明）；被声明依赖的易变字段
    连同声明（字段、审核依据）和当前值一起进来，声明一变、被声明字段一变，输入摘要就变。"""

    binding: dict[str, Any] = {"schema_version": HOST_SCHEMA_V3, "kind": payload["kind"], "target": payload["target"],
                               "links": payload["links"], "ancestors": payload["ancestors"]}
    if "core" in payload:
        binding["core"] = payload["core"]
    if dependency is not None:
        volatile = payload.get("volatile") or {}
        binding["metadata_dependency"] = {"fields": dependency["fields"], "basis": dependency["basis"],
                                          "values": {field: volatile.get(field) for field in dependency["fields"]}}
    return binding


def host_entry_v3(raw: str, payload: Mapping[str, Any], dependency: Any = None) -> dict[str, Any]:
    """由路径、v3 载荷和（可选的）元数据依赖声明构造 v3 条目。生产与历史重放的自洽复核共用这一个构造函数。"""

    normalized = normalize_metadata_dependency(dependency)
    _check_v3_payload(payload)
    detail: dict[str, Any] = {"path": raw, "schema_version": HOST_SCHEMA_V3, "snapshot": payload}
    if normalized is not None:
        detail["metadata_dependency"] = normalized
    return {"category": "environment", "name": "host:" + raw,
            "sha256": _canonical_digest(_v3_binding(payload, normalized)), "detail": detail}


def _v3_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """把 v2 载荷投影成 v3 分组：时间与 inode 挪进 volatile，符号链接链去掉自身时间和 inode，目录不带 size。"""

    kind = snapshot["kind"]
    payload: dict[str, Any] = {"kind": kind, "target": snapshot["target"],
                               "links": [{key: link[key] for key in _V3_LINK_FIELDS} for link in snapshot["links"]],
                               "ancestors": snapshot["ancestors"]}
    if kind == "missing":
        return payload
    metadata = snapshot["metadata"]
    core = {"mode": metadata["mode"], "uid": metadata["uid"], "gid": metadata["gid"]}
    if kind == "file":
        core["size"] = metadata["size"]
        payload["content_sha256"] = snapshot["content_sha256"]
    else:
        payload["entries"] = snapshot["entries"]
    payload["core"] = core
    payload["volatile"] = {field: metadata[field] for field in VOLATILE_FIELDS}
    return payload


def host_snapshot_v3(raw: str, metadata_dependency: Any = None) -> dict[str, Any]:
    """v3 宿主快照。路径合法性、符号链接解析、内容摘要与取快照期间的竞态检查都复用 v2 ``host_snapshot``（它一字不动），
    这里只做分组投影；``metadata_dependency`` 是该路径在策略里声明的 ``{fields, basis}``（没有则 None）。"""

    return host_entry_v3(raw, _v3_payload(host_snapshot(raw)["detail"]["snapshot"]), metadata_dependency)


def is_host_entry_v3(entry: Any) -> bool:
    return isinstance(entry, Mapping) and isinstance(entry.get("detail"), Mapping) \
        and entry["detail"].get("schema_version") == HOST_SCHEMA_V3


def entry_declared_fields(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """条目登记的元数据依赖字段（没有声明为空元组）。"""

    dependency = (entry.get("detail") or {}).get("metadata_dependency")
    return tuple(dependency["fields"]) if isinstance(dependency, Mapping) and isinstance(dependency.get("fields"), list) else ()


def check_host_entry_v3(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """v3 条目与封存内容自洽：由 detail 重建出的条目必须与传入的逐字相同（摘要、名字、声明都不能被改写而不被发现）。
    返回封存的载荷。"""

    detail = entry.get("detail") if isinstance(entry, Mapping) else None
    path = detail.get("path") if isinstance(detail, Mapping) else None
    if not isinstance(path, str) or not path.startswith("/") or entry.get("category") != "environment":
        raise AuditError("宿主快照 v3 条目缺少合法路径")
    if host_entry_v3(path, detail.get("snapshot"), detail.get("metadata_dependency")) != entry:
        raise AuditError("宿主快照 v3 条目的摘要、名字或声明与封存内容不符")
    return detail["snapshot"]


_V3_REASONS = {"kind": "类型变了（文件／目录／缺失）", "target": "符号链接解析后的真实路径变了", "links": "符号链接链变了",
               "ancestors": "祖先目录的权限、属主或链接变了", "core": "权限、属主或（文件）大小变了"}


def host_snapshot_matches(planned: Mapping[str, Any], current: Mapping[str, Any], access_kinds: Iterable[str] | None,
                          declared_fields: Iterable[str] = ()) -> tuple[bool, str]:
    """按单元对该路径的实际访问方式比较两份 v3 载荷，返回（是否一致，原因）。

    必比：kind、target、links、ancestors、core——不论怎样访问都会观察到（路径解析要穿过它们）。
    访问类型含 read／exec 且是文件：再比内容摘要；含 list 且是目录：再比直接目录项；目录只被 stat／打开／read 时目录项
    不比（保持“打开父目录不等于递归覆盖子文件”的语义，子文件各有自己的快照）。访问类型是 missing 时由 kind 比较保证
    当前仍不存在。``declared_fields`` 是策略声明的元数据依赖，额外比 volatile 里对应字段。
    ``access_kinds=None`` 表示访问方式未知（子命名空间启动时还没有轨迹、预约重派前）：只比必比项和声明字段。
    访问类型只能来自 ``trace_access_modes``（要求完整性已通过的轨迹），不要自己拼。"""

    for key in ("kind", "target", "links", "ancestors", "core"):
        if planned.get(key) != current.get(key):
            return False, _V3_REASONS[key]
    kinds = frozenset(access_kinds or ())
    kind = planned.get("kind")
    if kind == "file" and kinds & CONTENT_ACCESS_KINDS and planned.get("content_sha256") != current.get("content_sha256"):
        return False, "文件内容变了（该单元读取或执行过它）"
    if kind == "directory" and kinds & LIST_ACCESS_KINDS and planned.get("entries") != current.get("entries"):
        return False, "目录的直接条目变了（该单元枚举过它）"
    old, new = planned.get("volatile") or {}, current.get("volatile") or {}
    for field in sorted(declared_fields or ()):
        if field not in VOLATILE_FIELDS:
            return False, "未知的元数据依赖字段：" + str(field)
        if old.get(field) != new.get(field):
            return False, field + " 变了（策略声明该单元依赖它）"
    return True, ""


def host_snapshot_identical(planned: Mapping[str, Any], current: Mapping[str, Any]) -> tuple[bool, str]:
    """整份载荷相等（含 volatile）：拿不到完整轨迹、访问方式不可信时的失败关闭兜底，等价于 v2 的口径。"""

    return (True, "") if dict(planned) == dict(current) else (False, "快照整份不同（缺少完整轨迹，按最严格口径比较）")


def trace_is_complete(document: Mapping[str, Any]) -> bool:
    """轨迹完整性的判据：与 ``strict_audit_reads`` 开头一致的覆盖信息。只有严格轨迹（v2 起）带 coverage 块，所以不必
    另列模式版本。"""

    coverage = document.get("coverage") or {}
    return (document.get("roots") == ["/"] and coverage.get("all_host_paths") is True
            and coverage.get("stream_complete") is True and coverage.get("unresolved") == 0
            and bool(document.get("lines")) and bool(document.get("accesses")))


def trace_access_modes(document: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """从已通过完整性检查的严格轨迹取（路径 → 排序后的访问类型）。轨迹不完整或路径重复一律抛 AuditError：访问方式决定
    比较什么，来源不可信时不能放松任何比较，调用方只能经由这个函数得到访问类型。"""

    if not trace_is_complete(document):
        raise AuditError("完整轨迹缺失、未结束或含未解析调用，不能由它确定访问方式")
    modes: dict[str, tuple[str, ...]] = {}
    for item in document["accesses"]:
        if (not isinstance(item, (list, tuple)) or len(item) != 2 or not isinstance(item[0], str)
                or not isinstance(item[1], (list, tuple)) or not all(isinstance(kind, str) for kind in item[1])):
            raise AuditError("轨迹的访问记录格式不符")
        if item[0] in modes:
            raise AuditError("轨迹里同一路径出现了多条访问记录")
        modes[item[0]] = tuple(sorted(set(item[1])))
    return modes


def host_path_modes(document: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]] | None) -> dict[str, list[str]]:
    """每个 v3 宿主条目的路径在轨迹里的访问类型（没被访问的路径是空列表）。没有 v3 条目返回空字典；有 v3 条目而轨迹不
    完整抛 AuditError。run_child 的结束凭证、replay_runtime 的复核、记录的 ``read_audit.host_path_modes`` 封存，都用它。"""

    paths = sorted({entry["detail"]["path"] for entry in inputs or [] if is_host_entry_v3(entry)})
    if not paths:
        return {}
    modes = trace_access_modes(document)
    return {path: list(modes.get(path, ())) for path in paths}


def host_snapshot_like(entry: Mapping[str, Any]) -> dict[str, Any]:
    """按既有宿主条目的合同版本与元数据依赖声明，重新取同一路径的现场快照：v3 条目取 v3（带同样的声明，摘要才可比），
    其余（v2）取 v2，与过去的 ``host_snapshot(name[5:])`` 调用逐字相同。"""

    name = entry.get("name") if isinstance(entry, Mapping) else None
    if not isinstance(name, str) or not name.startswith("host:"):
        raise AuditError("不是宿主快照条目")
    if is_host_entry_v3(entry):
        return host_snapshot_v3(name[5:], entry["detail"].get("metadata_dependency"))
    return host_snapshot(name[5:])


def host_entry_drift(entry: Mapping[str, Any]) -> str | None:
    """预约中止后重派前的现场核对（没有轨迹，访问方式未知）：v2 条目整份相等；v3 条目只比任何访问方式下都必须一致的核心
    集与声明字段（真正按访问方式的比较留给运行结束时与承接时）。未漂移返回 None，否则返回原因。"""

    if not is_host_entry_v3(entry):
        return None if host_snapshot(str(entry["name"])[5:]) == entry else "宿主输入快照漂移"
    try:
        planned = check_host_entry_v3(entry)
        live = host_snapshot_like(entry)["detail"]["snapshot"]
    except (OSError, ValueError, TypeError, AuditError) as error:
        return str(error)
    ok, reason = host_snapshot_matches(planned, live, None, entry_declared_fields(entry))
    return None if ok else reason


def _v3_entries_by_name(inputs: Any, label: str) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """把输入明细里的 v3 宿主条目按名字归拢（同名后者覆盖前者，与过去一致），同时找出格式畸形的 v3 条目。

    畸形指：是 v3 条目（detail 带 v3 合同版本）但名字或路径不是字符串。这样的条目没法按名字归拢，也没法取访问类型，过去会在
    补比里抛 KeyError／TypeError，让整次承接评估崩溃；现在登记一条问题（失败关闭），由调用方整体返回。输入明细本身不是列表
    （缺省的 None 除外）同样算畸形。不是 v3 条目的元素（None、字符串、没有 detail 的字典、v2 条目……）不属于补比对象，照旧
    忽略——它们若真被改过，会反映在输入摘要或记录自摘要上，由调用方的其它核对发现。``label`` 只用于问题文案。"""

    found: dict[str, Mapping[str, Any]] = {}
    if inputs is None:
        return found, []
    if not isinstance(inputs, (list, tuple)):
        return found, [f"{label}的输入明细不是列表，无法核对 v3 宿主条目"]
    malformed: list[str] = []
    for index, entry in enumerate(inputs):
        if not is_host_entry_v3(entry):
            continue
        if not isinstance(entry.get("name"), str) or not isinstance(entry["detail"].get("path"), str):
            malformed.append(f"{label}的第 {index + 1} 项是格式畸形的 v3 宿主条目（名字或路径不是字符串）")
            continue
        found[entry["name"]] = entry
    return found, malformed


def host_inputs_inherit_problems(trace_document: Mapping[str, Any], recorded_inputs: Sequence[Mapping[str, Any]] | None,
                                 current_inputs: Sequence[Mapping[str, Any]] | None,
                                 audit_summary: Mapping[str, Any] | None) -> list[str]:
    """v3 宿主快照的承接专属核对（``unit_records.check_record`` 在轨迹重放之后调用）；没有 v3 条目时返回空，v2 记录继续走
    旧规则（摘要整份相等 + ``strict_audit_reads`` 现取快照）。

    条目摘要不含内容、目录项和易变元数据，所以摘要相等并不蕴含这些相等；这里补上：
    1. 取记录封存的 ``host_path_modes``，必须与由完整轨迹重算的结果逐项相等（篡改或缺失即失败关闭）；
    2. 把来源记录的每个 v3 条目与当前条目按访问方式比较——来源运行观察过的方面，现在必须仍然相同。

    来源记录是可被改写的输入：v3 条目缺名字、名字或路径类型不对，或读集轨迹本身畸形时，返回问题（失败关闭），不抛异常
    （否则一条畸形记录就能让整次承接评估中断）。良构输入的返回值与过去逐字相同。"""

    recorded, bad_recorded = _v3_entries_by_name(recorded_inputs, "来源记录")
    current, bad_current = _v3_entries_by_name(current_inputs, "当前输入")
    if bad_recorded or bad_current:
        # 有畸形的 v3 条目：失败关闭。畸形条目没法归拢，继续往下比较其余条目也没有意义。
        return [*bad_recorded, *bad_current]
    if not recorded and not current:
        return []
    try:
        modes = host_path_modes(trace_document, list(recorded.values()))
    except AuditError as error:
        return ["宿主快照 v3 无法确定访问方式：" + str(error)]
    except (KeyError, TypeError, AttributeError) as error:
        # 轨迹文档本身畸形（不是对象、覆盖信息或访问记录的类型不对）：和完整性不足同样失败关闭，不让异常穿出去。
        return [f"宿主快照 v3 无法确定访问方式：来源记录的读集轨迹格式畸形（{type(error).__name__}：{error}）"]
    problems: list[str] = []
    if (audit_summary or {}).get("host_path_modes") != modes:
        problems.append("记录封存的宿主访问类型缺失，或与完整轨迹重算的结果不符")
    for name in sorted(set(recorded) | set(current)):
        old, new = recorded.get(name), current.get(name)
        if old is None or new is None:
            problems.append(f"{name}：来源记录与当前输入的 v3 宿主条目集合不同")
            continue
        try:
            planned, live = check_host_entry_v3(old), check_host_entry_v3(new)
        except AuditError as error:
            problems.append(f"{name}：{error}")
            continue
        if entry_declared_fields(old) != entry_declared_fields(new) or old["detail"].get("metadata_dependency") != new["detail"].get("metadata_dependency"):
            problems.append(f"{name}：元数据依赖声明与来源记录不同")
            continue
        ok, reason = host_snapshot_matches(planned, live, modes.get(old["detail"]["path"], ()), entry_declared_fields(old))
        if not ok:
            problems.append(f"{name}：{reason}")
    return problems


def _verify_host_entry_v3(entry: Mapping[str, Any], modes: Mapping[str, Sequence[str]] | None, *, historical: bool) -> Mapping[str, Any]:
    """``strict_audit_reads`` 对一个 v3 条目的核对：先自洽复核；非历史重放时再现取 v3 快照，按本次轨迹的访问方式与条目
    比较（``modes`` 为 None 表示轨迹不完整，退到整份相等）。历史重放只校验封存内容自身，不读宿主。返回封存载荷。"""

    planned = check_host_entry_v3(entry)
    if not historical:
        path = entry["detail"]["path"]
        live = host_snapshot_v3(path)["detail"]["snapshot"]
        if modes is None:
            ok, reason = host_snapshot_identical(planned, live)
        else:
            ok, reason = host_snapshot_matches(planned, live, modes.get(path, ()), entry_declared_fields(entry))
        if not ok:
            raise AuditError("宿主输入快照漂移：" + reason)
    return planned


def strict_audit_reads(document: Mapping[str, Any], inputs: Sequence[Mapping[str, Any]] | None,
                       *, repo_root: str, data_root: str | None, historical: bool = False) -> dict[str, Any]:
    """完整观察与可承接分开判断：所有实际路径必须有事前绑定且运行后未变的精确快照。"""
    findings = []
    snapshots = {}
    coverage = document.get("coverage") or {}
    if (document.get("schema_version") not in {STRICT_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA_V4}
            or document.get("roots") != ["/"]
            or coverage.get("all_host_paths") is not True or coverage.get("stream_complete") is not True
            or coverage.get("unresolved") != 0 or not document.get("lines") or not document.get("accesses")):
        findings.append({"root": "audit", "path": "", "kinds": [], "sample": "", "suggestion": "完整轨迹缺失、未结束或含未解析调用，不可承接"})
    runtime_paths: set[str] = set()
    # 带隔离合同的轨迹（v3、v4）都交给运行时重放；replay_runtime 按文档自己的 schema_version 选用对应合同。
    if document.get("schema_version") in {RUNTIME_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA_V4}:
        try:
            runtime_paths = _runtime_module().replay_runtime(document, list(inputs or []), historical=historical)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            findings.append({"root": "runtime", "path": "", "kinds": [], "sample": "", "suggestion": str(error)})
    v3_modes = None
    if any(is_host_entry_v3(entry) for entry in inputs or []):
        try:
            v3_modes = trace_access_modes(document)
        except AuditError:
            # 轨迹不完整：上面已经报了 finding；v3 条目退到最严格的整份相等，不放松任何比较。
            v3_modes = None
    for entry in inputs or []:
        detail = entry.get("detail") or {}
        if detail.get("schema_version") == HOST_SCHEMA_V3:
            # 宿主快照 v3：按本次轨迹里该路径的访问方式比较（见上面 v3 一节）；v2 条目不经过这里。
            path = detail.get("path")
            try:
                snapshots[path] = _verify_host_entry_v3(entry, v3_modes, historical=historical)
            except (OSError, ValueError, TypeError, AuditError) as error:
                findings.append({"root": "host", "path": str(path), "kinds": [], "sample": "", "suggestion": str(error)})
            continue
        if detail.get("schema_version") != HOST_SCHEMA:
            continue
        path = detail.get("path")
        try:
            if historical:
                # 历史重放校验封存快照自身；当前承接仍须重读宿主实物。
                encoded = json.dumps(detail["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                current = {"category": "environment", "name": "host:" + path, "sha256": hashlib.sha256(encoded).hexdigest(),
                           "detail": detail}
            else:
                current = host_snapshot(path)
            if current != entry:
                raise AuditError("宿主输入快照漂移")
        except (OSError, ValueError, TypeError, AuditError) as error:
            findings.append({"root": "host", "path": str(path), "kinds": [], "sample": "", "suggestion": str(error)})
            continue
        snapshots[path] = detail["snapshot"]
    counts = {"repo_paths": 0, "data_paths": 0, "host_paths": 0, "metadata_only": 0, "import_probes": 0}
    for path, kinds in document.get("accesses") or []:
        root = "repo" if _under(path, repo_root) else "data" if data_root and _under(path, data_root) else "host"
        counts[root + "_paths"] += 1
        if path in runtime_paths:
            continue
        binding = snapshots.get(path)
        if binding is not None:
            kind = binding["kind"]
            if ((kind == "file" and set(kinds) <= {"read", "exec", "stat"})
                    # libc 也会不带 O_DIRECTORY 只读打开目录；快照已证明该精确路径是目录，直接目录项已绑定。
                    # 子文件仍须单独快照，不能因为打开了父目录就获得递归覆盖。
                    or (kind == "directory" and set(kinds) <= METADATA_KINDS | {"read"})
                    or (kind == "missing" and set(kinds) <= {"missing"})):
                continue
        findings.append({"root": root, "path": path, "kinds": kinds, "sample": path,
                         "suggestion": "实际路径缺少精确快照或访问类型超出绑定；目录声明不能覆盖子文件"})
    return {"undeclared_count": len(findings), "undeclared": findings[:MAX_FINDINGS], **counts,
            "coverage_complete": not findings, "coverage_scope": "all-file-paths/v1", "status": "failed" if findings else "passed"}


def narrow_input_scope(record_path: Path, trace_path: Path) -> dict[str, Any]:
    """从完整且仍能重放的正式记录生成收窄提案；提案必须再实际运行，不能直接承接旧宽范围记录。"""
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    for path in (record_path, trace_path):
        if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
            raise AuditError("收窄来源须为无符号链接的绝对路径")
    record = json.loads(record_path.read_text())
    body = {key: value for key, value in record.items() if key != "record_sha256"}
    audit = record.get("read_audit") or {}
    if (record.get("schema_version") != "unit-execution-record/v1" or digest(body) != record.get("record_sha256")
            or record.get("kind") != "formal" or record.get("unit_type") not in {"command", "test"}
            or record.get("inheritable") is not True
            or record.get("passed") is not True or record.get("exit_code") != 0 or record.get("signal") is not None
            or record.get("timed_out") is not False or audit.get("coverage_complete") is not True
            or audit.get("status") != "passed" or audit.get("undeclared_count") != 0
            or audit.get("coverage_scope") != "all-file-paths/v1"
            or not isinstance(record.get("spec"), dict)
            or digest(record.get("spec")) != record.get("spec_sha256")
            or record.get("spec", {}).get("type") != record.get("unit_type")
            or audit.get("inputs_sha256") != record.get("inputs_sha256")
            or hashlib.sha256(trace_path.read_bytes()).hexdigest() != audit.get("trace", {}).get("sha256")):
        raise AuditError("收窄输入必须引用完整通过的正式命令或测试单元及其原始轨迹")
    # 沿原记录库核对来源清单与日志，未正常结束的运行不能单凭一张自摘要记录生成提案。
    name = "_read_audit_unit_records"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE.with_name("unit_records.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    records = sys.modules[name]
    store = records.RecordStore(record_path.parents[2])
    if (store.record_path(record["unit_id"], record["record_sha256"]) != record_path
            or store.log_path(audit["trace"]["sha256"]) != trace_path
            or records.entries_sha256(record["inputs"]) != record["inputs_sha256"]
            or records.entries_sha256(record["environment"]) != record["environment_sha256"]):
        raise AuditError("收窄来源必须来自同一正式记录库，输入与环境摘要须闭合")
    current = records.Current(record["unit_id"], record["unit_type"], record["spec"], record["spec_sha256"],
                              record["inputs"], record["inputs_sha256"], True)
    completed = records.parse_utc(record.get("completed_at_utc"))
    if completed is None:
        raise AuditError("来源执行完成时间缺失")
    facts = records.RunFacts(record["policy_sha256"], record["environment"], record["environment_sha256"],
                             record["executor"], 24, completed, require_read_audit=True)
    problems = records.check_record(store, record_path, record, current, facts)
    if problems:
        raise AuditError("来源执行记录不能完整复核：" + "；".join(problems))
    trace = load_trace(trace_path)
    if not strict_audit_reads(trace, record.get("inputs"), repo_root=audit["repo_root"], data_root=audit.get("data_root"))["coverage_complete"]:
        raise AuditError("来源读集不能按当前环境重放，拒绝生成收窄提案")
    runtime_inputs = {}
    excluded = set()
    if trace.get("schema_version") in {RUNTIME_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA_V4}:
        excluded = _runtime_module().replay_runtime(trace, record["inputs"])
        runtime_inputs["runtime_contract"] = trace["runtime"]["context"]["contract_entry"]["detail"]["contract"]
    paths = sorted({path for path, _kinds in trace["accesses"]} - excluded)
    # 宿主快照 v3：来源记录里声明了元数据依赖的路径，收窄后必须保留声明；否则收窄会悄悄放松对 mtime 一类字段的绑定。
    kept = set(paths)
    dependencies = sorted(({"path": entry["detail"]["path"], **entry["detail"]["metadata_dependency"]}
                           for entry in record["inputs"]
                           if is_host_entry_v3(entry) and isinstance(entry["detail"].get("metadata_dependency"), Mapping)
                           and entry["detail"]["path"] in kept), key=lambda item: item["path"])
    return {"schema_version": "unit-read-scope-proposal/v1", "unit_id": record["unit_id"],
            "spec": record["spec"], "requires_validation": True, "reuse_enabled": False,
            "environment_sha256": record["environment_sha256"], "executor_sha256": record["executor"]["sha256"],
            "inputs": {"host_paths": paths, "require_read_audit": True, **runtime_inputs,
                       **({"metadata_dependencies": dependencies} if dependencies else {})},
            "source_record": {"path": str(record_path), "sha256": hashlib.sha256(record_path.read_bytes()).hexdigest()},
            "source_trace": {"path": str(trace_path), "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest()}}


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
                data_root: str | None = None, in_data_root: bool = False, head_id_only: bool = False,
                strict: bool = False) -> dict[str, Any]:
    """核对一个单元的读集：返回 ``{"undeclared": [...], "undeclared_count", "repo_paths", "data_paths", "listed"}``。

    ``in_data_root``：单元在数据根里运行（pre-A3 场景），数据根与仓库同布局的部分按仓库相对路径核对；否则读到数据根一律报出。"""

    if strict:
        return strict_audit_reads(document, inputs, repo_root=repo_root, data_root=data_root)
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
    return {"undeclared_count": len(findings), "undeclared": findings[:MAX_FINDINGS], **counts,
            "coverage_complete": False, "coverage_scope": "repo-and-data-only/v1",
            "status": "failed" if findings else "passed"}


def load_trace(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditError(f"审计轨迹读不出来：{path}（{error}）") from error
    if (not isinstance(document, dict)
            or document.get("schema_version") not in {TRACE_SCHEMA, STRICT_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA, RUNTIME_TRACE_SCHEMA_V4}):
        raise AuditError(f"审计轨迹格式不对：{path}")
    return document


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter", help="读标准输入的 strace 输出，只留各个根之下的路径，写紧凑 JSON")
    filt.add_argument("--output", type=Path, required=True)
    filt.add_argument("--root", action="append", default=[], required=True)
    filt.add_argument("--strict", action="store_true")
    filt.add_argument("--runtime-context", type=Path)
    filt.add_argument("--trace-contract", choices=sorted(TRACE_CONTRACTS), default=TRACE_CONTRACT_V3,
                      help="带隔离合同的轨迹版本，默认 v3；v4 须与 --strict、--runtime-context 同用")
    scope = sub.add_parser("scope", help="从完整正式轨迹生成待重新验收的单元输入提案")
    scope.add_argument("--record", type=Path, required=True)
    scope.add_argument("--trace", type=Path, required=True)
    scope.add_argument("--output", type=Path, required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.command == "scope":
        result = narrow_input_scope(args.record, args.trace)
        with args.output.open("x") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        return 0
    if args.command == "filter":
        stream = (line.decode("utf-8", "surrogateescape") for line in sys.stdin.buffer)
        runtime_context = json.loads(args.runtime_context.read_text()) if args.runtime_context else None
        document = filter_stream(stream, args.root, strict=args.strict, runtime_context=runtime_context,
                                 trace_contract=args.trace_contract)
        temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8", errors="surrogateescape")
        os.replace(temporary, args.output)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
