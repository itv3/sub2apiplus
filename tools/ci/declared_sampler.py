#!/usr/bin/env python3
"""声明级承接的读集采样器（``declared-inputs/v1`` 的证据来源）。

为什么要有：命令单元（backend 的 go test、frontend 的 lint、egress-spec 的整条 make 目标等）原先声明“整个仓库四段加 HEAD”，
每换一次提交就全部失效。要改成“该命令真实读到的目录和文件”，就必须先知道真实读集——这个工具用 strace 把一条命令的实际
文件访问采下来，归并成可机器核查的证据，再据此起草声明范围草案和人工评审清单。采样只在 ARM64 上由调度者执行；本文件
在本机只用假 strace 和合成轨迹测试，因此不依赖 macOS 或 Linux 的专有行为（需要 Python 3.12、strace 6.x、git）。

四个子命令（数据流：run 产出 raw，merge 把若干 raw 合并成 sample，draft 把 sample 起草成登记表草案和评审清单）：

* ``filter``：作为 ``strace -o '|…'`` 管道的另一端读标准输入，流式输出 ``declared-sample-raw/v1``。内存受控：每处理
  ``--chunk-lines`` 行就把 ``TraceParser`` 里累积的路径排空到自己的紧凑聚合里，因此峰值内存只与不同路径数有关，与总行数
  无关。它也是将来“正式执行自带采样”要复用的那一端（见 ``StreamAggregator``）。
* ``run``：从门禁清单取指定命令单元的 argv、cwd、env，构造 strace 包装并执行，保存 raw。启动前缀（launcher，ARM64 上是
  私有挂载命名空间加 tmpfs 遮蔽）放在 strace **外侧**：命令看到的挂载命名空间与正式执行完全相同，而 unshare、mount 这类
  未建模的调用不会出现在 trace 里（``TraceParser`` 严格模式对未建模调用一律记 ``unsupported-call``，会让采样白白变无效）。
  现有 ReadAuditor 的做法是 strace 在最外层、靠 runtime.prepare 剥掉 launcher 再重装；本工具直接把 launcher 放外面，省去重装。
* ``merge``：多次 raw 取并集；用 ``git ls-files`` 给仓库内路径标 ``tracked``；按采样有效性判据判定 ``valid``，不满足时写明
  原因（绝不为了“让采样过”放宽规则或删路径）。
* ``draft``：把 sample 归并成 ``declared-scope/v1`` 草案和 ``review.md``。归并只做机械规则，不做任何语义判断的自动放行；
  过宽、被枚举的目录、写入集合、未跟踪读取、网络、冷热差异等全部交给人工评审。

设计要点（改动前先读）：

* 只复用 ``read_audit.TraceParser(strict=True)``（不带 runtime_context：没有输出归属，也没有 200000 事件上限），以及它的
  公共属性 accesses、lines、unresolved、unresolved_reasons、pending、deferred_count，外加清空 accesses 与 samples 来裁剪内存。
  不调用 ``trace_document``：它会把全部路径序列化成一个大 JSON，也会按根目录筛路径，不适合目录粒度的采样；不 import
  read_audit_runtime，不改 read_audit.py（它的摘要被钉在受管工具里，control 四链的 sidecar 还会动态复用它）。
  测试里的 T-API 钉住这些依赖，审计器改名或改语义会立刻红。
* 仓库内路径完整保留（标 tracked、逐个核对），仓库外路径按目录聚合（kinds 并集加计数）：仓库内路径的内容进入范围摘要，
  必须逐个能说清；仓库外的 GOCACHE 内容寻址文件、GOROOT 源码是海量文件，逐个登记既不可能也没有意义，它们靠合同和类别
  绑定（工具链、缓存、系统库），只需知道读了哪些目录。语义分类只发生在验证侧和 draft 的前缀表里，采样本身不分类。
* 路径别名：证据与草案里只出现 ``{TREE}``（测试树根）、``{WORK}``（Go 缓存的父目录）、``{HOME}``、``{TMP}``，采样时的
  具体绝对路径只在 ``aliases`` 里留痕，因此跨工作目录不失效。
* strace 选项：沿用审计器的文件类调用加进程派生，另加 ``connect``（收集网络目的地）。ARM64 实测（strace 6.8）：沿用审计器的
  ``-e verbose=none`` 时，execve 的 argv 和 connect 的 sockaddr 都被打印成不透明指针——读 git 历史的提交号取不到，连接的
  目的地也判不了——所以这里改成只让 execve、execveat、connect 以 verbose 形式解码（``-e verbose=execve,execveat,connect``），
  其它调用的结构仍缩写成指针。若 argv 或 sockaddr 仍然不可见，filter 会记下哨兵值或 ``unparsed``，merge 判采样无效，绝不静默漏判。
* 多线程下 connect 会被拆成 ``connect(… <unfinished ...>`` 与 ``<... connect resumed>) = 返回值`` 两行，中间夹着别的进程的行。
  filter 把这两种行都按进程号配对截走、不交给 TraceParser（它对 connect 记 unsupported-call，对没有起始行的 resumed 记
  resume-without-start）；缺 start、重叠、流结束仍悬着都记成未解析原因，采样随之无效。
* 过滤器是 strace 的管道子进程，与被跟踪命令同一个进程组：它忽略 SIGTERM 与 SIGINT，超时终止整组时仍能读到 EOF 后把已有读集
  写出来。``run`` 在采样树运行前就不干净时直接拒绝（树不干净的采样没有意义，别白跑几十分钟）；运行后不干净则照常写 raw，
  由 merge 判无效。冷缓存遇到非空旧目录只改名保留，不删除。

证据文件约定（登记侧的覆盖检查按这些约定读取 sample）：

* ``repo_accesses`` 的键是 ``{TREE}/相对路径``（``{TREE}`` 自身表示测试树根）；第三项 tracked 表示该路径是 git 跟踪的文件，
  或含跟踪文件的目录。唯一的目录聚合键是 ``{TREE}/.git/objects/``（内容寻址的对象文件成千上万，逐个保留没有意义）。
* ``outside_accesses`` 的键以 ``/`` 结尾表示目录聚合：目录自身被枚举（list）算在该目录的键下，其余访问算在父目录的键下；
  不以 ``/`` 结尾的是精确路径，只出现在 /dev、/proc、/sys、/etc、``{HOME}`` 这几个需要逐条看清的前缀下和别名根自身。
  ``{WORK}/go-tmp/`` 与 ``{TMP}/`` 是暂存区，整体折叠成前缀本身。计数是各聚合块内去重后的路径出现次数之和，只作规模提示，
  不参与覆盖判定。进程号和线程号规范化：``/proc/<进程号>/`` 记成 ``/proc/self/``，``/proc/self/task/<线程号>/`` 记成
  ``/proc/self/task/{TID}/``。
* ``coverage.unresolved`` 等于 ``unresolved_reasons`` 各计数之和；采样器自己的规模保护和流结束时仍悬着的调用也写成显式原因
  （``sampler:*``、``unfinished-calls-at-eof``、``deferred-process-events-at-eof``），它们都不在容忍集合里。
* ``git_commands`` 里可能有哨兵 ``["git", "<argv-not-visible>"]``；``network`` 的 family 除 AF_INET、AF_INET6、AF_UNIX、
  AF_UNSPEC 外还可能是别的 AF_* 或 ``unparsed``（认不出结构，一律不能证明是本机连接）。

ARM64 使用方法（调度者执行；变量一律 ``${VAR}``）::

    python3 -B ${DRV}/declared_sampler.py run --manifest ${SAMPLE}/gates-manifest.json --unit backend:go-test \\
        --out ${SAMPLE}/raw --tree ${TREE} --work ${SAMPLE} --cache cold --launcher-json "${LAUNCHER}"
    python3 -B ${DRV}/declared_sampler.py run ... --cache warm --launcher-json "${LAUNCHER}"
    python3 -B ${DRV}/declared_sampler.py merge --raw ${SAMPLE}/raw/backend-go-test.cold.raw.json \\
        --raw ${SAMPLE}/raw/backend-go-test.warm.raw.json --tree ${TREE} --out ${SAMPLE}/samples/backend-go-test.sample.json
    python3 -B ${DRV}/declared_sampler.py draft --samples ${SAMPLE}/samples --tree ${TREE} \\
        --registry-out ${SAMPLE}/command_input_scopes.draft.json --review-out ${SAMPLE}/review.md

退出码：0 成功；1 仅 ``run``（命令退出码非零或超时，raw 仍已写出，merge 会判采样无效）；2 参数、环境或输入非法（未采样）；
3 仅 ``run``（过滤器没有产出，采样基础设施故障）。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

HERE = Path(__file__).resolve()

RAW_SCHEMA = "declared-sample-raw/v1"
SAMPLE_SCHEMA = "declared-sample/v1"
SCOPE_SCHEMA = "declared-scope/v1"
REGISTRY_SCHEMA = "declared-inputs-registry/v1"
GATES_SCHEMA = "unit-executor-gates/v1"

# 采样有效性里“可以容忍的 unresolved 原因”前缀。pnpm 的 node_modules 全是符号链接，Go 的 os.Lstat 也会发
# AT_SYMLINK_NOFOLLOW 的 newfstatat；严格解析对这类调用只增加计数、不丢路径（路径照常记录），所以可以容忍。其余原因
# （unrecognized-line、unsupported-call、resume-without-start 等）都意味着有读取没被看见，一律判采样无效。
DECLARED_TOLERATED_UNRESOLVED = ("symlink-metadata-not-bound:",)

ALIAS_NAMES = ("TREE", "WORK", "HOME", "TMP")

# 读取方式（与 read_audit 的 kinds 同名）。用位掩码聚合：几十万个路径时，一个小整数比一个 set 对象省得多。
KIND_BITS = {"read": 1, "exec": 2, "write": 4, "missing": 8, "stat": 16, "dir": 32, "list": 64}
KIND_NAMES = sorted(KIND_BITS)  # 输出一律按字母序，与 read_audit.trace_document 的 sorted(kinds) 一致。
_READ_EXEC = KIND_BITS["read"] | KIND_BITS["exec"]
_WRITE = KIND_BITS["write"]
_LIST = KIND_BITS["list"]

# strace 选项：文件类调用、目录枚举、进程派生、fchdir（还原相对路径要用），加 connect（收集网络目的地）。execve、execveat 与
# connect 以 verbose 形式解码：execve 才能看到 git 子进程的 argv，connect 才能看到目的地址（ARM64 strace 6.8 实测，
# verbose=none 时两者都只打印成不透明指针）；其它调用的结构仍缩写成指针，与 verbose=none 等价（省 strace 自己的格式化开销，
# 也避免把环境变量打进轨迹）。
STRACE_OPTIONS = ("-f", "-qq", "--seccomp-bpf", "-y", "-s", "4096", "-e", "verbose=execve,execveat,connect", "-e", "signal=none",
                  "-e", "trace=%file,getdents64,clone,clone3,fork,vfork,fchdir,connect")

# 已知的 ARM64 启动前缀（与门禁入口脚本传给 plan 的 --launcher-json 同一份；这里独立保存一份只为“未显式给出时自动识别”，
# 不 import read_audit_runtime）。识别不出来时 run 会拒绝而不是猜测。
KNOWN_LAUNCHER = ["unshare", "-m", "--propagation", "private", "bash", "-c",
                  'mount -t tmpfs -o ro,size=64k,mode=0755 tmpfs /root/oauth-capture && exec "$@"', "entry-gates"]

# 过滤器内存与规模保护。超过任何一项都会在 unresolved_reasons 里记 sampler:limit:*，采样随之判无效，而不是悄悄丢数据。
DEFAULT_CHUNK_LINES = 100_000          # 每处理这么多行就排空一次 TraceParser 的路径字典
MAX_REPO_PATHS = 2_000_000             # 仓库内不同路径数上限（约 250 字节一条，最坏约 500 MB）
OUTSIDE_DIR_LIMIT = 100_000            # 仓库外目录键数上限，超过就折叠到祖先目录
OUTSIDE_FULL_LIMIT = 20_000            # 下面这些前缀下“完整保留”的路径数上限
MAX_GIT_COMMANDS = 20_000
MAX_NETWORK_TARGETS = 20_000
# 仓库外路径里需要逐个看清的前缀：/dev（要认出 /dev/null 这种无害写入）、/proc 与 /sys（虚拟文件，数量有限）、/etc（环境
# 指纹只覆盖包管理器管理的文件，手改的文件要逐个评审）、HOME（配置文件要逐路径登记存在性与内容）。其余仓库外路径按目录聚合。
OUTSIDE_FULL_PREFIXES = ("/dev/", "/proc/", "/sys/", "/etc/", "{HOME}/")
OUTSIDE_FULL_EXACT = frozenset({"/dev", "/proc", "/sys", "/etc", "{HOME}"})
# 暂存区：里面读写什么都不绑定（go-build 临时目录名带随机数，t.TempDir 也是），按前缀整体折叠，免得目录键数以万计。
OUTSIDE_FOLD_PREFIXES = ("{WORK}/go-tmp/", "{TMP}/")
ALIAS_ROOTS = frozenset({"{WORK}", "{HOME}", "{TMP}"})

# strace 在线程于系统调用中途被分离或进程退出时打印的标记行：调用号未知、没有路径、也没有成功返回，不携带读取信息。
# 必须在交给 TraceParser 之前精确识别并单独计数，否则严格解析会把它们记成 unrecognized-line。这条正则要精确：
# 任何别的未识别行仍然让采样无效。
_MARKER = re.compile(r"^\d+\s+\?\?\?\( <(?:detached|unfinished) \.\.\.>\s*$")
_CONNECT_CALL = re.compile(r"^(?:\d+\s+)?connect\((.*)$", re.S)
_CONNECT_RESUMED = re.compile(r"^(?:\d+\s+)?<\.\.\. connect resumed>")
_EXECVE_CALL = re.compile(r'^(?:\d+\s+)?execve\("((?:[^"\\]|\\.)*)"(?:\.\.\.)?, ')
_SOCK_INET = re.compile(r'sa_family=AF_INET, sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]*)"\)')
_SOCK_INET6 = re.compile(r'sa_family=AF_INET6, sin6_port=htons\((\d+)\),.*?inet_pton\(AF_INET6, "([^"]*)"')
_SOCK_UNIX = re.compile(r'sa_family=AF_UNIX, sun_path=(@?)"((?:[^"\\]|\\.)*)"(\.\.\.)?')
_PID = re.compile(r"^(\d+)\s+")
_SOCK_FAMILY = re.compile(r"sa_family=(AF_[A-Z0-9_]+)")
_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_PROC_PID = re.compile(r"^/proc/\d+(?=/|$)")
_PROC_TASK = re.compile(r"^(/proc/self/task)/\d+(?=/|$)")

# git 子进程的 argv 不可见（strace 没有解码 execve 参数）时留下的哨兵：merge 看到它就判采样无效。
GIT_ARGV_HIDDEN = "<argv-not-visible>"
# 写入 /dev/null 没有任何可观察状态，既不是输入也不会改变结果；Go 的 os/exec 在子进程输出未接管时就会打开它。这是调研里
# 没有写明、但 Go 测试采样必然遇到的情形，唯一的写入豁免，merge 会在 review.writes.sinks 里留痕供评审。
DECLARED_WRITE_SINKS = ("/dev/null",)


class SamplerError(RuntimeError):
    """参数、环境或输入文件不合法（命令行返回 2）；采样本身的好坏不走这个异常，而是写进 valid 与 invalid_reasons。"""


# ---------------------------------------------------------------------------
# 摘要、时间与序列化
# ---------------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    """与 unit_records.canonical 逐字相同的规范化，command_sha256 和 launcher_sha256 要和登记侧的算法一致。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(unit_id: str) -> str:
    """单元 ID 变成文件名安全的短名（backend:go-test 变成 backend-go-test）。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", unit_id)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(", ", ": "))


def _pretty(value: Any, indent: int = 0) -> str:
    """确定性的半展开 JSON：短值一行，长列表每个元素一行。

    证据文件会提交进仓库并被人 diff，所以要“每条路径一行”；同时文件字节必须稳定（evidence_sha256 取文件字节摘要）。
    一律 ASCII 转义：strace 里的非 UTF-8 路径经 surrogateescape 读入后是孤立代理字符，ensure_ascii 把它们写成合法的
    JSON 转义，读回来还是原样，文件本身永远是纯 ASCII。
    """
    one = _compact(value)
    if not isinstance(value, (dict, list)) or not value or len(one) + indent <= 120:
        return one
    pad = " " * (indent + 2)
    if isinstance(value, list):
        rows = [pad + (_compact(item) if len(_compact(item)) <= 600 else _pretty(item, indent + 2)) for item in value]
        return "[\n" + ",\n".join(rows) + "\n" + " " * indent + "]"
    rows = [pad + json.dumps(key, ensure_ascii=True) + ": " + _pretty(value[key], indent + 2) for key in sorted(value)]
    return "{\n" + ",\n".join(rows) + "\n" + " " * indent + "}"


def dumps_document(document: Mapping[str, Any]) -> str:
    return _pretty(dict(document)) + "\n"


def write_document(path: Path, document: Mapping[str, Any], *, exclusive: bool = False) -> None:
    """原子写入（临时文件加 replace）；exclusive 时目标已存在就拒绝覆盖，证据不能被静默改写。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise SamplerError(f"拒绝覆盖已存在的文件：{path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(dumps_document(document), encoding="utf-8")
    os.replace(temporary, path)


def load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SamplerError(f"读不出 JSON 文件：{path}（{error}）") from error
    if not isinstance(value, dict):
        raise SamplerError(f"JSON 顶层必须是对象：{path}")
    return value


def kinds_of(mask: int) -> list[str]:
    return [name for name in KIND_NAMES if mask & KIND_BITS[name]]


def mask_of(kinds: Iterable[str]) -> int:
    mask = 0
    for kind in kinds:
        mask |= KIND_BITS[kind]
    return mask


# ---------------------------------------------------------------------------
# 加载审计器的解析器（只用 TraceParser）
# ---------------------------------------------------------------------------

_READ_AUDIT_MODULE = None


def read_audit_module():
    """按文件路径加载同目录的 read_audit.py。

    驱动目录不是 Python 包（ARM64 上执行器从驱动目录运行），所以不能写 ``from tools.ci import read_audit``。加载期间强制
    不写字节码：在驱动目录或测试树里留下 __pycache__ 会让门禁前置检查直接退出。
    """
    global _READ_AUDIT_MODULE
    if _READ_AUDIT_MODULE is None:
        name = "_declared_sampler_read_audit"
        spec = importlib_util.spec_from_file_location(name, HERE.with_name("read_audit.py"))
        if spec is None or spec.loader is None:
            raise SamplerError("找不到同目录的 read_audit.py")
        module = importlib_util.module_from_spec(spec)
        previous = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            sys.modules[name] = module
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous
        _READ_AUDIT_MODULE = module
    return _READ_AUDIT_MODULE


# ---------------------------------------------------------------------------
# 路径别名
# ---------------------------------------------------------------------------


class AliasTable:
    """绝对路径换成别名路径。

    最长前缀优先：采样根里测试树常在 WORK 之下（``${SAMPLE}/test-tree`` 与 ``${SAMPLE}/go-build-cache``），必须先认 TREE。
    每个别名同时登记原样路径和 realpath：内核经 ``-y`` 给出的是解析后的真实路径，而命令行参数里的字面路径可能没解析。
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        if set(values) != set(ALIAS_NAMES):
            raise SamplerError(f"别名必须恰为 {'、'.join(ALIAS_NAMES)}，收到：{sorted(values)}")
        self.values: dict[str, str] = {}
        pairs: list[tuple[str, str]] = []
        for name in ALIAS_NAMES:
            raw = values[name]
            if not isinstance(raw, str) or not os.path.isabs(raw):
                raise SamplerError(f"别名 {name} 必须是绝对路径：{raw!r}")
            normal = os.path.normpath(raw)
            if normal == "/":
                raise SamplerError(f"别名 {name} 不能是根目录")
            self.values[name] = normal
            for variant in {normal, os.path.realpath(normal)}:
                pairs.append((variant, name))
        pairs.sort(key=lambda pair: (-len(pair[0]), pair[1]))
        self._pairs = pairs

    def apply(self, path: str) -> str:
        for prefix, name in self._pairs:
            if path == prefix:
                return "{" + name + "}"
            if path.startswith(prefix + "/"):
                return "{" + name + "}" + path[len(prefix):]
        return path


def parse_aliases(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or name not in ALIAS_NAMES or name in values:
            raise SamplerError(f"--alias 必须是 NAME=绝对路径，NAME 取 {'、'.join(ALIAS_NAMES)} 且不重复：{item!r}")
        values[name] = value
    missing = [name for name in ALIAS_NAMES if name not in values]
    if missing:
        raise SamplerError(f"缺少别名：{'、'.join(missing)}")
    return values


def _volatile(path: str) -> str:
    """进程号和线程号每次都不同：/proc/3106409/cgroup 与 /proc/self/cgroup 是同一件事，规范成 self，免得目录键无限增长。"""
    if path.startswith("/proc/"):
        path = _PROC_PID.sub("/proc/self", path, count=1)
        path = _PROC_TASK.sub(r"\1/{TID}", path, count=1)
    return path


# ---------------------------------------------------------------------------
# strace 行的旁路解析：connect 与 execve
# ---------------------------------------------------------------------------


def _unescape(text: str) -> str:
    """strace 字符串字面量还原（反斜杠转义、八进制、十六进制）。

    自带一份而不用 read_audit.unescape：与审计器的耦合面只留 TraceParser 一处，审计器改名或改语义不会静默改变这里的结果。
    """
    if "\\" not in text:
        return text
    out = bytearray()
    index = 0
    simple = {"n": 10, "t": 9, "r": 13, "v": 11, "f": 12, "a": 7, "b": 8, "\\": 92, '"': 34, "'": 39}
    while index < len(text):
        char = text[index]
        if char != "\\" or index + 1 >= len(text):
            out.extend(char.encode("utf-8", "surrogateescape"))
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
            out.extend(nxt.encode("utf-8", "surrogateescape"))
            index += 2
    return out.decode("utf-8", "surrogateescape")


def _parse_string_array(text: str, start: int) -> list[str] | None:
    """从 text[start] 的 [ 起解析 strace 打印的字符串数组；格式不符（例如被打印成 0x7ffd... 指针）返回 None。"""
    if start >= len(text) or text[start] != "[":
        return None
    index, items = start + 1, []
    while index < len(text):
        while index < len(text) and text[index] == " ":
            index += 1
        if index >= len(text):
            return None
        if text[index] == "]":
            return items
        if text[index] == '"':
            token = _STRING.match(text, index)
            if token is None:
                return None
            items.append(_unescape(token.group(1)))
            index = token.end()
            if text.startswith("...", index):  # 字符串被 -s 截断
                index += 3
        elif text.startswith("...", index):  # 数组元素被省略
            items.append("...")
            index += 3
        else:
            return None
        while index < len(text) and text[index] == " ":
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
    return None


def parse_git_execve(line: str) -> tuple[str, ...] | None:
    """execve 行里 argv[0] 是 git 时返回完整 argv；argv 不可见但可执行文件名是 git 时返回哨兵；不是 git 返回 None。"""
    match = _EXECVE_CALL.match(line)
    if match is None:
        return None
    argv = _parse_string_array(line, match.end())
    if argv:
        return tuple(argv) if os.path.basename(argv[0]) == "git" else None
    if os.path.basename(_unescape(match.group(1))) == "git":
        return ("git", GIT_ARGV_HIDDEN)
    return None


def parse_connect(line: str) -> tuple[str, str] | None:
    """connect 行的 (地址族, 目的地)；不是 connect 行返回 None。

    目的地：AF_INET 为 ``地址:端口``，AF_INET6 为 ``[地址]:端口``（地址取自 ``inet_pton(AF_INET6, "…", &sin6_addr)``），
    AF_UNIX 为路径，抽象套接字统一记成 ``@名字``（strace 6.x 写 ``@"名字"``，旧写法是以 NUL 开头的 ``"\\0名字"``）。
    sockaddr 不是可解码结构（仍是不透明指针）或路径被 ``-s`` 截断时记 ``unparsed``——目的地不可判，宁可让 merge 因无法证明是
    本机连接而判无效，也不能当作没有网络。只看调用里的地址，不看返回值：非阻塞 connect 连不可达的外部地址同样返回
    EINPROGRESS，那也是一次外连尝试。
    """
    call = _CONNECT_CALL.match(line)
    if call is None:
        return None
    body = call.group(1)
    unix = _SOCK_UNIX.search(body)
    if unix:
        if unix.group(3):  # 路径被截断，目的地不完整
            return ("unparsed", body[:120])
        path = _unescape(unix.group(2))
        if unix.group(1) == "@":
            return ("AF_UNIX", "@" + path)
        return ("AF_UNIX", "@" + path[1:] if path.startswith("\x00") else path)
    inet = _SOCK_INET.search(body)
    if inet:
        return ("AF_INET", f"{inet.group(2)}:{inet.group(1)}")
    inet6 = _SOCK_INET6.search(body)
    if inet6:
        return ("AF_INET6", f"[{inet6.group(2)}]:{inet6.group(1)}")
    family = _SOCK_FAMILY.search(body)
    if family and family.group(1) == "AF_UNSPEC":
        return ("AF_UNSPEC", "")  # UDP 套接字断开关联，不是外连
    if family:
        return (family.group(1), "")
    return ("unparsed", body[:120])


# ---------------------------------------------------------------------------
# 流式聚合（filter 的核心，将来正式执行自带采样时直接复用）
# ---------------------------------------------------------------------------


class StreamAggregator:
    """逐行喂 strace 输出，流式得到 ``declared-sample-raw/v1``。

    内存模型：TraceParser 对每个不同路径存两份东西——accesses 里的 kinds 集合和 samples 里的一条样例行（最长 300 字符）。
    300 万行、180 万个不同路径的合成流里，仅 samples 就要近 1 GB。本类每 chunk_lines 行把 accesses 并入自己的紧凑聚合（路径
    到位掩码），然后清空 TraceParser 的 accesses 与 samples。TraceParser 的跨行状态（cwd、pending、deferred）都在别的属性里，
    清空这两个纯累积字典不会影响解析结果；T-API 测试钉住这一点。chunk_lines 为 0 表示只在结束时排空（等价于不裁剪，压力脚本
    用它做对照）。
    """

    def __init__(self, aliases: Mapping[str, str], *, unit_id: str, chunk_lines: int = DEFAULT_CHUNK_LINES) -> None:
        if chunk_lines < 0:
            raise SamplerError("chunk_lines 不能为负")
        self.alias_table = AliasTable(aliases)
        self.unit_id = unit_id
        self.chunk_lines = chunk_lines
        self.parser = read_audit_module().TraceParser(strict=True)
        self.lines = 0
        self.markers = 0
        self.repo: dict[str, int] = {}
        self.outside: dict[str, list[int]] = {}
        self.git_commands: dict[tuple[str, ...], None] = {}
        self.network: dict[tuple[str, str], None] = {}
        # 进程号 -> 正在进行中的 connect（start 行已见、resumed 行未见）；用来给 connect 的 unfinished 与 resumed 成对。
        self.connect_pending: dict[str, tuple[str, str]] = {}
        self.sampler_reasons: dict[str, int] = {}
        self._since_drain = 0
        self._full_keys = 0

    # -- 输入 -----------------------------------------------------------------
    def feed(self, raw: str) -> None:
        self.lines += 1
        line = raw.rstrip("\n")
        if "???(" in line and _MARKER.match(line):
            self.markers += 1
            return
        if (" connect(" in line or line.startswith("connect(") or "<... connect resumed>" in line) and self._side_connect(line):
            return
        if " execve(" in line or line.startswith("execve("):
            self._side_execve(line)
        self.parser.feed(raw)
        self._since_drain += 1
        if self.chunk_lines and self._since_drain >= self.chunk_lines:
            self._drain()

    def _limit(self, reason: str) -> None:
        self.sampler_reasons["sampler:" + reason] = self.sampler_reasons.get("sampler:" + reason, 0) + 1

    def _side_connect(self, line: str) -> bool:
        """connect 的 start 行与 resumed 行都在这里截走，一律不交给 TraceParser：它对未建模的 connect 记 unsupported-call，
        对没有起始行的 resumed 记 resume-without-start，都会让采样白白变无效。

        多线程下 connect 会被拆成 ``connect(… <unfinished ...>`` 和 ``<... connect resumed>) = 返回值`` 两行，中间可以夹着别的
        进程的行。目的地在 start 行里（完整调用也一样），所以在 start 行收集；这里按进程号把 start 与 resumed 配对：
        resumed 找不到对应的 start、同一进程又来一个 unfinished、流结束时仍有 start 没等到 resumed，都意味着有一次 connect 的
        目的地我们没看全，各记一个未解析原因，采样随之无效——和 TraceParser 对别的调用要求配对是同一个道理。
        返回值不参与判定：只看地址，不看返回值（非阻塞 connect 连不可达的外部地址同样是 EINPROGRESS，仍是一次外连尝试）。
        """
        match = _PID.match(line)
        pid = match.group(1) if match else "0"
        if _CONNECT_RESUMED.match(line):
            if self.connect_pending.pop(pid, None) is None:
                self._limit("connect-resume-without-start")
            return True
        target = parse_connect(line)
        if target is None:
            return False
        if line.rstrip().endswith("<unfinished ...>"):
            if pid in self.connect_pending:
                self._limit("connect-overlapping-unfinished")
            self.connect_pending[pid] = target
        if target not in self.network:
            if len(self.network) >= MAX_NETWORK_TARGETS:
                self._limit("limit:network-targets")
            else:
                self.network[target] = None
        return True

    def _side_execve(self, line: str) -> None:
        """execve 行同时交给 TraceParser（它要记 exec 路径），这里另外收集 git 子进程的 argv，用于确定读了哪些历史提交。"""
        argv = parse_git_execve(line)
        if argv is not None and argv not in self.git_commands:
            if len(self.git_commands) >= MAX_GIT_COMMANDS:
                self._limit("limit:git-commands")
            else:
                self.git_commands[argv] = None

    # -- 排空与聚合 -------------------------------------------------------------
    def _drain(self) -> None:
        accesses = self.parser.accesses
        for path, kinds in accesses.items():
            self._absorb(path, kinds)
        accesses.clear()
        samples = getattr(self.parser, "samples", None)
        if isinstance(samples, dict):
            samples.clear()
        self._since_drain = 0

    def _absorb(self, path: str, kinds: Iterable[str]) -> None:
        mask = 0
        for kind in kinds:
            bit = KIND_BITS.get(kind)
            if bit is None:
                self._limit("unknown-kind:" + str(kind))
            else:
                mask |= bit
        if not mask:
            return
        aliased = self.alias_table.apply(_volatile(path))
        if aliased == "{TREE}" or aliased.startswith("{TREE}/"):
            self._absorb_repo(aliased, mask)
        else:
            self._absorb_outside(aliased, mask)

    def _absorb_repo(self, aliased: str, mask: int) -> None:
        # git 对象库里是成千上万个内容寻址文件：逐个保留没有意义，聚合到目录级（其余 .git 内路径仍逐个保留，数量很少）。
        key = "{TREE}/.git/objects/" if aliased == "{TREE}/.git/objects" or aliased.startswith("{TREE}/.git/objects/") else aliased
        current = self.repo.get(key)
        if current is not None:
            self.repo[key] = current | mask
        elif len(self.repo) >= MAX_REPO_PATHS:
            self._limit("limit:repo-paths")
        else:
            self.repo[key] = mask

    def _absorb_outside(self, aliased: str, mask: int) -> None:
        """仓库外：按目录聚合。目录自身被枚举（list）算在该目录的键下，其余访问算在父目录的键下；完整保留前缀下按精确路径。"""
        listed, rest = mask & _LIST, mask & ~_LIST
        full = aliased.startswith(OUTSIDE_FULL_PREFIXES) or aliased in OUTSIDE_FULL_EXACT
        if full and self._full_keys < OUTSIDE_FULL_LIMIT:
            if rest:
                self._bump(aliased, rest, full=True)
            if listed:
                self._bump(aliased.rstrip("/") + "/", listed, full=True)
            return
        if rest:
            parent = aliased if aliased in ALIAS_ROOTS else aliased.rsplit("/", 1)[0] + "/"
            self._bump(self._fold(parent), rest)
        if listed:
            self._bump(self._fold(aliased.rstrip("/") + "/"), listed)

    @staticmethod
    def _fold(key: str) -> str:
        for prefix in OUTSIDE_FOLD_PREFIXES:
            if key.startswith(prefix):
                return prefix
        return key

    def _bump(self, key: str, mask: int, *, full: bool = False) -> None:
        entry = self.outside.get(key)
        if entry is None:
            if len(self.outside) >= OUTSIDE_DIR_LIMIT:
                # 目录键数失控：折叠到最近的已有祖先，保住内存，同时让采样判无效（目录基数爆炸说明分类前缀需要补充）。
                self._limit("limit:outside-directories")
                while key not in self.outside and key != "/":
                    key = key.rstrip("/").rsplit("/", 1)[0] + "/"
                entry = self.outside.setdefault(key, [0, 0])
            else:
                entry = self.outside[key] = [0, 0]
                if full:
                    self._full_keys += 1
        entry[0] |= mask
        # 计数是各块内去重后的路径出现次数之和，只作规模提示，不参与覆盖判定。
        entry[1] += 1

    # -- 输出 -----------------------------------------------------------------
    def finish(self) -> dict[str, Any]:
        """结束流，返回 raw 文档（run 为 null，由 run 子命令补全）。"""
        self._drain()
        parser = self.parser
        reasons: dict[str, int] = dict(parser.unresolved_reasons)
        # 把 EOF 时仍悬着的调用也写成显式原因，保持“unresolved 等于各原因计数之和”，merge 不必再单独推断。
        if parser.pending:
            reasons["unfinished-calls-at-eof"] = len(parser.pending)
        if parser.deferred_count:
            reasons["deferred-process-events-at-eof"] = parser.deferred_count
        if self.connect_pending:
            reasons["sampler:connect-unfinished-at-eof"] = len(self.connect_pending)
        for reason, count in self.sampler_reasons.items():
            reasons[reason] = reasons.get(reason, 0) + count
        return {
            "schema_version": RAW_SCHEMA,
            "unit_id": self.unit_id,
            "aliases": dict(self.alias_table.values),
            "chunk_lines": self.chunk_lines,
            "lines": self.lines,
            "coverage": {"stream_complete": True, "unresolved": sum(reasons.values()), "unresolved_reasons": dict(sorted(reasons.items())),
                         "unfinished_calls": len(parser.pending) + len(self.connect_pending), "unresolved_process_events": parser.deferred_count,
                         "lines": self.lines, "detached_markers": self.markers},
            "repo_accesses": [[path, kinds_of(self.repo[path])] for path in sorted(self.repo)],
            "outside_accesses": [[key, kinds_of(self.outside[key][0]), self.outside[key][1]] for key in sorted(self.outside)],
            "git_commands": [list(argv) for argv in self.git_commands],
            "network": [{"family": family, "target": target} for family, target in sorted(self.network)],
            "run": None,
        }


def filter_stream(lines: Iterable[str], aliases: Mapping[str, str], *, unit_id: str,
                  chunk_lines: int = DEFAULT_CHUNK_LINES) -> dict[str, Any]:
    aggregator = StreamAggregator(aliases, unit_id=unit_id, chunk_lines=chunk_lines)
    for line in lines:
        aggregator.feed(line)
    return aggregator.finish()


# ---------------------------------------------------------------------------
# 文档校验（字段闭合）
# ---------------------------------------------------------------------------

COVERAGE_KEYS = frozenset({"stream_complete", "unresolved", "unresolved_reasons", "unfinished_calls", "unresolved_process_events",
                           "lines", "detached_markers"})
RAW_KEYS = frozenset({"schema_version", "unit_id", "aliases", "chunk_lines", "lines", "coverage", "repo_accesses", "outside_accesses",
                      "git_commands", "network", "run"})
COMMAND_KEYS = frozenset({"launcher", "argv", "cwd_rel", "sha256"})
RUN_KEYS = frozenset({"label", "cache", "exit_code", "signal", "timed_out", "seconds", "started_utc", "finished_utc", "command",
                      "git_status_before", "git_status_after", "commit", "kernel", "strace", "sampler_sha256", "env_overrides",
                      "docker", "filter_wait_seconds"})
SAMPLE_KEYS = frozenset({"schema_version", "unit_id", "command", "sampled", "aliases", "coverage", "repo_accesses", "outside_accesses",
                         "git_commands", "network", "exit_code", "valid", "invalid_reasons", "review"})
SAMPLED_KEYS = frozenset({"commit", "utc", "kernel", "strace", "sampler_sha256", "runs"})
SAMPLED_RUN_KEYS = frozenset({"cache", "exit_code", "lines", "seconds"})
REVIEW_KEYS = frozenset({"runs", "git_status", "untracked_reads", "writes", "network", "run_differences", "docker", "allowances",
                         "git_argv_hidden"})
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_access_rows(problems: list[str], name: str, rows: Any, shape: str) -> None:
    """shape：raw-repo 是 [路径, kinds]，sample-repo 是 [路径, kinds, tracked]，outside 是 [目录或路径, kinds, 计数]。"""
    if not isinstance(rows, list):
        problems.append(f"{name} 必须是列表")
        return
    for row in rows:
        if not isinstance(row, list) or not row or not isinstance(row[0], str):
            problems.append(f"{name} 的条目格式非法：{str(row)[:80]}")
            return
        kinds = row[1] if len(row) > 1 else None
        if not isinstance(kinds, list) or not kinds or any(kind not in KIND_BITS for kind in kinds):
            problems.append(f"{name} 的读取方式非法：{str(row)[:80]}")
            return
        extra_ok = {"raw-repo": len(row) == 2, "sample-repo": len(row) == 3 and isinstance(row[2], bool),
                    "outside": len(row) == 3 and _is_int(row[2]) and row[2] >= 0}[shape]
        if not extra_ok:
            problems.append(f"{name} 的条目长度或类型非法：{str(row)[:80]}")
            return


def _check_common(problems: list[str], document: Mapping[str, Any], *, tracked: bool) -> None:
    coverage = document.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != COVERAGE_KEYS:
        problems.append("coverage 的字段集合不符")
    else:
        if not isinstance(coverage["stream_complete"], bool):
            problems.append("coverage.stream_complete 必须是布尔值")
        for key in ("unresolved", "unfinished_calls", "unresolved_process_events", "lines", "detached_markers"):
            if not _is_int(coverage[key]) or coverage[key] < 0:
                problems.append(f"coverage.{key} 必须是非负整数")
        reasons = coverage["unresolved_reasons"]
        if (not isinstance(reasons, dict) or not all(isinstance(key, str) and _is_int(count) and count > 0 for key, count in reasons.items())
                or sum(reasons.values() if isinstance(reasons, dict) else []) != coverage["unresolved"]):
            problems.append("coverage.unresolved_reasons 必须是原因到正计数的映射，且计数之和等于 unresolved")
    aliases = document.get("aliases")
    if not isinstance(aliases, dict) or set(aliases) != set(ALIAS_NAMES) or not all(isinstance(value, str) for value in aliases.values()):
        problems.append("aliases 必须恰为 TREE、WORK、HOME、TMP 四个字符串")
    _check_access_rows(problems, "repo_accesses", document.get("repo_accesses"), "sample-repo" if tracked else "raw-repo")
    _check_access_rows(problems, "outside_accesses", document.get("outside_accesses"), "outside")
    commands = document.get("git_commands")
    if (not isinstance(commands, list) or not all(isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv)
                                                  for argv in commands)):
        problems.append("git_commands 必须是非空字符串列表的列表")
    network = document.get("network")
    if (not isinstance(network, list) or not all(isinstance(item, dict) and set(item) == {"family", "target"}
                                                 and all(isinstance(value, str) for value in item.values()) for item in network)):
        problems.append("network 的条目必须恰有 family 与 target 两个字符串")


def _check_command(problems: list[str], command: Any) -> None:
    if not isinstance(command, dict) or set(command) != COMMAND_KEYS:
        problems.append("command 的字段集合不符")
        return
    for key in ("launcher", "argv"):
        if not isinstance(command[key], list) or not all(isinstance(part, str) and part for part in command[key]):
            problems.append(f"command.{key} 必须是字符串列表")
    if not command["argv"]:
        problems.append("command.argv 不能为空")
    if not isinstance(command["cwd_rel"], str) or not command["cwd_rel"] or os.path.isabs(command["cwd_rel"]) or ".." in Path(command["cwd_rel"]).parts:
        problems.append("command.cwd_rel 必须是相对测试树的路径")
    if command["sha256"] != command_digest(command["argv"], command["cwd_rel"]):
        problems.append("command.sha256 与 argv、cwd_rel 不符")


def command_digest(argv: Sequence[str], cwd_rel: str) -> str:
    """命令身份：去掉 launcher 前缀的 argv 加相对测试树的 cwd。登记侧用同一个算法核对，命令一变证据就不再适用。"""
    return sha256_json({"argv": list(argv), "cwd_rel": cwd_rel})


def validate_raw(document: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(document, dict) or set(document) != RAW_KEYS or document.get("schema_version") != RAW_SCHEMA:
        return ["raw 的 schema_version 或字段集合不符"]
    if not isinstance(document["unit_id"], str) or not document["unit_id"]:
        problems.append("unit_id 必须是非空字符串")
    if not _is_int(document["lines"]) or document["lines"] < 0 or not _is_int(document["chunk_lines"]) or document["chunk_lines"] < 0:
        problems.append("lines 与 chunk_lines 必须是非负整数")
    _check_common(problems, document, tracked=False)
    run = document["run"]
    if run is None:
        return problems
    if not isinstance(run, dict) or set(run) != RUN_KEYS:
        return problems + ["run 的字段集合不符"]
    _check_command(problems, run["command"])
    for key in ("label", "cache", "started_utc", "finished_utc", "commit", "kernel", "strace", "sampler_sha256"):
        if not isinstance(run[key], str) or not run[key]:
            problems.append(f"run.{key} 必须是非空字符串")
    if run["cache"] not in ("cold", "warm"):
        problems.append("run.cache 只能是 cold 或 warm")
    if not isinstance(run["timed_out"], bool) or not (run["exit_code"] is None or _is_int(run["exit_code"])):
        problems.append("run.exit_code 与 run.timed_out 类型非法")
    if not isinstance(run["seconds"], (int, float)) or isinstance(run["seconds"], bool):
        problems.append("run.seconds 必须是数字")
    for key in ("git_status_before", "git_status_after"):
        if not isinstance(run[key], list) or not all(isinstance(item, str) for item in run[key]):
            problems.append(f"run.{key} 必须是字符串列表")
    if not isinstance(run["env_overrides"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in run["env_overrides"].items()):
        problems.append("run.env_overrides 必须是字符串映射")
    return problems


def validate_sample(document: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(document, dict) or set(document) != SAMPLE_KEYS or document.get("schema_version") != SAMPLE_SCHEMA:
        return ["sample 的 schema_version 或字段集合不符"]
    if not isinstance(document["unit_id"], str) or not document["unit_id"]:
        problems.append("unit_id 必须是非空字符串")
    _check_command(problems, document["command"])
    sampled = document["sampled"]
    if not isinstance(sampled, dict) or set(sampled) != SAMPLED_KEYS:
        problems.append("sampled 的字段集合不符")
    else:
        if not HEX40.fullmatch(str(sampled["commit"])) or not HEX64.fullmatch(str(sampled["sampler_sha256"])):
            problems.append("sampled.commit 或 sampler_sha256 不是十六进制摘要")
        runs = sampled["runs"]
        if (not isinstance(runs, list) or not runs or not all(isinstance(item, dict) and set(item) == SAMPLED_RUN_KEYS for item in runs)):
            problems.append("sampled.runs 的条目字段集合不符")
    _check_common(problems, document, tracked=True)
    if not (document["exit_code"] is None or _is_int(document["exit_code"])):
        problems.append("exit_code 类型非法")
    if not isinstance(document["valid"], bool) or not isinstance(document["invalid_reasons"], list) \
            or not all(isinstance(item, str) for item in document["invalid_reasons"]):
        problems.append("valid 与 invalid_reasons 类型非法")
    elif document["valid"] == bool(document["invalid_reasons"]):
        problems.append("valid 必须恰好等于 invalid_reasons 为空")
    review = document["review"]
    if not isinstance(review, dict) or set(review) != REVIEW_KEYS:
        problems.append("review 的字段集合不符")
    return problems


# ---------------------------------------------------------------------------
# run：从门禁清单取命令，构造 strace 包装并执行
# ---------------------------------------------------------------------------

# 与入口门禁脚本给 Go 的三个缓存目录同名同位置（WORK 下固定名字）：别名 {WORK} 就是它们的父目录，证据与正式运行才能对上。
CACHE_DIRS = (("GOCACHE", "go-build-cache"), ("GOMODCACHE", "go-mod-cache"), ("GOTMPDIR", "go-tmp"))
FILTER_WAIT_SECONDS = 600.0
DEFAULT_UNIT_TIMEOUT = 7200.0
# strace 对命令的放大实测 1.1 到 1.4 倍；超时按清单超时乘这个系数，免得采样被自己的开销误杀。
DEFAULT_TIMEOUT_FACTOR = 2.5
_PIPE_SAFE = re.compile(r"[A-Za-z0-9_./:=@%+,-]+")


def load_unit(manifest: Path, unit_id: str) -> dict[str, Any]:
    payload = load_document(manifest)
    if payload.get("schema_version") != GATES_SCHEMA:
        raise SamplerError(f"不是门禁清单（{GATES_SCHEMA}）：{manifest}")
    units = payload.get("units")
    if not isinstance(units, list):
        raise SamplerError("门禁清单没有 units 列表")
    for unit in units:
        if isinstance(unit, dict) and unit.get("unit_id") == unit_id:
            break
    else:
        raise SamplerError(f"清单里没有命令单元 {unit_id}（测试单元不由本工具采样）")
    argv, cwd = unit.get("argv"), unit.get("cwd")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        raise SamplerError(f"单元 {unit_id} 的 argv 非法")
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise SamplerError(f"单元 {unit_id} 的 cwd 必须是绝对路径")
    env = unit.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise SamplerError(f"单元 {unit_id} 的环境变量非法")
    return unit


def split_launcher(argv: Sequence[str], launcher_json: str | None) -> tuple[list[str], list[str]]:
    """清单里命令单元的 argv 是 [*launcher, *命令]，两者没有分界标记，必须靠调用方告知或认出已知的启动前缀。

    显式给 ``--launcher-json`` 时要求 argv 以它开头；不给时只认已知的 ARM64 前缀。看起来像启动前缀（以 unshare 开头）
    却认不出来时拒绝：悄悄把 unshare 当成命令的一部分，trace 里会满是 mount 之类的未建模调用，整次采样白跑。
    """
    if launcher_json is None:
        launcher = list(KNOWN_LAUNCHER) if list(argv[:len(KNOWN_LAUNCHER)]) == KNOWN_LAUNCHER else []
        if not launcher and argv[0] == "unshare":
            raise SamplerError("清单命令以 unshare 开头但不是已知的启动前缀；请显式给出 --launcher-json（与生成清单时的值相同）")
    else:
        try:
            launcher = json.loads(launcher_json)
        except ValueError as error:
            raise SamplerError(f"--launcher-json 不是合法 JSON：{error}") from error
        if not isinstance(launcher, list) or not all(isinstance(part, str) and part for part in launcher):
            raise SamplerError("--launcher-json 必须是非空字符串组成的列表")
        if list(argv[:len(launcher)]) != launcher:
            raise SamplerError("清单里的命令不以 --launcher-json 给出的启动前缀开头")
    command = list(argv[len(launcher):])
    if not command:
        raise SamplerError("去掉启动前缀后没有命令")
    return list(launcher), command


def resolve_cwd(unit: Mapping[str, Any], tree: Path) -> tuple[str, str]:
    cwd = Path(unit["cwd"]).resolve()
    if cwd != tree and tree not in cwd.parents:
        raise SamplerError(f"单元 {unit['unit_id']} 的 cwd 不在采样树内：{cwd}（--tree 必须与生成清单时 plan 的 --tree 相同）")
    if not cwd.is_dir():
        raise SamplerError(f"单元 {unit['unit_id']} 的 cwd 不存在：{cwd}")
    return str(cwd), os.path.relpath(cwd, tree)


def build_environment(*, base: Mapping[str, str], unit: Mapping[str, Any], work: Path, out_dir: Path,
                      overrides: Mapping[str, str]) -> dict[str, str]:
    """与 Scheduler.launch 给命令单元的环境逐项对应：额度核数、GOMAXPROCS、清单自带 env、UNIT_EXECUTOR_* 标记、禁写字节码。

    base 是调度者按门禁白名单准备好的环境（env -i 加 PATH、LANG、HOME 等），这里不再剥离也不再补充，采样必须看到与正式
    运行相同的环境。Go 的三个缓存目录钉在 WORK 下的固定名字，环境里若已有别的值就拒绝（别名对不上，证据没法和正式运行比）。
    """
    env = dict(base)
    cores = unit.get("cores", 1)
    count = str(max(1, math.ceil(cores if isinstance(cores, (int, float)) and not isinstance(cores, bool) else 1)))
    env["UNIT_EXECUTOR_CORES"] = count
    env["GOMAXPROCS"] = count
    env.update(unit.get("env") or {})
    env["UNIT_EXECUTOR_UNIT"] = unit["unit_id"]
    env["UNIT_EXECUTOR_KIND"] = "formal"
    env["UNIT_EXECUTOR_RECORDS_MODULE"] = str(HERE.with_name("unit_records.py"))
    env["UNIT_EXECUTOR_PARENT_STATE_DIR"] = str(Path(out_dir) / "sampler-state")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    names = {name for name, _ in CACHE_DIRS}
    for name, sub in CACHE_DIRS:
        expected = str(work / sub)
        if env.get(name) not in (None, "", expected):
            raise SamplerError(f"环境变量 {name}={env[name]} 与 --work 下的固定位置 {expected} 不一致；去掉它，或让 --work 指向它的父目录")
        env[name] = expected
    for name, value in overrides.items():
        if name in names:
            raise SamplerError(f"--env 不能覆盖 {name}：Go 缓存位置必须固定在 --work 下")
        env[name] = value
    return env


def prepare_caches(work: Path, cache: str) -> list[str]:
    """冷缓存＝一个空的 GOCACHE；热缓存＝紧接着复用上一次写热的那个。返回需要告诉调度者的提示。

    不删除任何目录：冷缓存遇到非空旧目录时只把它改名保留（调度者之后用 Python 清理），改名可逆，删除不可逆。
    """
    notes: list[str] = []
    build = work / "go-build-cache"
    if build.is_symlink():
        raise SamplerError(f"{build} 不能是符号链接")
    if cache == "cold":
        if build.exists():
            if not build.is_dir():
                raise SamplerError(f"{build} 不是目录")
            if any(build.iterdir()):
                renamed = work / f"go-build-cache.replaced-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')}-{os.getpid()}"
                os.rename(build, renamed)
                notes.append(f"冷缓存：旧的非空 GOCACHE 已改名保留为 {renamed}（未删除）")
        build.mkdir(mode=0o700, exist_ok=True)
    elif not build.is_dir() or not any(build.iterdir()):
        raise SamplerError("热缓存要求 --work 下的 go-build-cache 非空；请先对同一单元跑一次 --cache cold")
    (work / "go-tmp").mkdir(mode=0o700, exist_ok=True)
    if not (work / "go-mod-cache").is_dir():
        notes.append("警告：--work 下没有 go-mod-cache；离线环境下 Go 类单元会因缺依赖而失败")
    return notes


def filter_pipe(*, output: Path, unit_id: str, aliases: Mapping[str, str], chunk_lines: int) -> str:
    """strace -o '|…' 管道另一端的命令行。strace 会把它交给 sh 解释，所以任何空白或 shell 特殊字符都拒绝。"""
    parts = [sys.executable, "-B", str(HERE), "filter", "--output", str(output), "--unit-id", unit_id, "--chunk-lines", str(chunk_lines)]
    for name in ALIAS_NAMES:
        parts += ["--alias", f"{name}={aliases[name]}"]
    for part in parts:
        if not _PIPE_SAFE.fullmatch(part):
            raise SamplerError(f"过滤器命令里的片段带空白或 shell 特殊字符，不能交给 strace 的管道：{part}")
    return " ".join(parts)


def build_strace_argv(*, launcher: Sequence[str], strace: str, pipe: str, cwd: str, command: Sequence[str]) -> list[str]:
    """launcher 在最外侧，然后是 strace，被跟踪的是“切目录再 exec 命令”的 sh——与 Scheduler.launch 的命令单元包装一致。

    为什么 launcher 放 strace 外侧：它的 unshare、bash、mount 不属于命令本身，放进 trace 只会产生 unsupported-call；放外侧
    则命令仍然在同一个私有挂载命名空间里运行（launcher 最后 exec 的就是 strace），读到的文件系统与正式执行完全一样。
    """
    return [*launcher, strace, *STRACE_OPTIONS, "-o", "|" + pipe, "--", "/bin/sh", "-c", 'cd -- "$0" && exec "$@"', cwd, *command]


def git_status_lines(tree: Path) -> list[str]:
    """运行前后各取一次：非空说明命令改了跟踪文件或留下了未忽略的新文件，采样证据不可用。--no-optional-locks 避免刷新 index。"""
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(tree), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        return [f"<git status 失败 rc={result.returncode}>"]
    return [line for line in result.stdout.decode("utf-8", "surrogateescape").splitlines() if line]


def git_head(tree: Path) -> str:
    result = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    commit = result.stdout.strip()
    if result.returncode or not HEX40.fullmatch(commit):
        raise SamplerError(f"{tree} 不是有提交的 git 仓库")
    return commit


def strace_version(strace: str, env: Mapping[str, str]) -> str:
    try:
        result = subprocess.run([strace, "--version"], capture_output=True, text=True, env=dict(env), timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first = result.stdout.splitlines()[0] if result.stdout else ""
    found = re.search(r"version\s+(\S+)", first)
    return found.group(1) if found else (first or "unknown")


class DockerWatch:
    """integration 类单元的容器前后对比（docker ps -a 前后差集，加 docker events 的 create 事件）。

    只报告、不清理：自动 docker rm 在采样机上有误删风险，残留容器的 ID 与镜像写进 raw，由调度者决定怎么处理。Docker 不可用时
    只记录错误，不中断采样（命令本身没有 Docker 会自己失败）。
    """

    def __init__(self, env: Mapping[str, str], events_path: Path) -> None:
        self.env = dict(env)
        self.events_path = Path(events_path)
        self.before: dict[str, str] = {}
        self.error: str | None = None
        self._events: subprocess.Popen[bytes] | None = None
        self._sink = None

    def _ps(self) -> dict[str, str]:
        result = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}} {{.Image}}"], env=self.env, capture_output=True, text=True,
                                timeout=60, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise SamplerError(f"docker ps 失败 rc={result.returncode}")
        rows: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if parts:
                rows[parts[0]] = parts[1] if len(parts) > 1 else ""
        return rows

    def start(self) -> None:
        try:
            self.before = self._ps()
            self._sink = self.events_path.open("wb")
            self._events = subprocess.Popen(
                ["docker", "events", "--filter", "type=container", "--filter", "event=create", "--format", "{{json .}}"],
                env=self.env, stdin=subprocess.DEVNULL, stdout=self._sink, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(0.5)  # 给 events 订阅一点就绪时间，漏掉的由前后差集补上
        except (OSError, subprocess.SubprocessError, SamplerError) as error:
            self.error = str(error)

    def finish(self) -> dict[str, Any]:
        created: dict[str, str] = {}
        after: dict[str, str] = {}
        if self._events is not None:
            time.sleep(0.5)
            self._events.terminate()
            try:
                self._events.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._events.kill()
                self._events.wait()
        if self._sink is not None:
            self._sink.close()
            for line in self.events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                    actor = event.get("Actor") or {}
                    identifier = str(actor.get("ID") or event.get("id") or "")
                    image = str((actor.get("Attributes") or {}).get("image") or event.get("from") or "")
                except (ValueError, AttributeError):
                    continue
                if identifier:
                    created[identifier] = image
        if self.error is None:
            try:
                after = self._ps()
            except (OSError, subprocess.SubprocessError, SamplerError) as error:
                self.error = str(error)
        new = {identifier: image for identifier, image in after.items() if identifier not in self.before}
        return {"enabled": True, "error": self.error,
                "new_containers": [{"id": key, "image": new[key]} for key in sorted(new)],
                "created": [{"id": key, "image": created[key]} for key in sorted(created)]}


def _kill_group(process: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_sample(*, manifest: Path, unit_id: str, out_dir: Path, tree: Path, work: Path, cache: str, label: str | None,
               launcher_json: str | None, env_overrides: Mapping[str, str], docker_hooks: bool, timeout_factor: float,
               chunk_lines: int, base_env: Mapping[str, str] | None = None) -> tuple[int, Path, dict[str, Any]]:
    """采样一次：返回（退出码, raw 路径, 摘要）。退出码语义见模块说明。"""
    tree, work, out_dir = Path(tree).resolve(), Path(work).resolve(), Path(out_dir).resolve()
    if cache not in ("cold", "warm"):
        raise SamplerError("--cache 只能是 cold 或 warm")
    label = label or cache
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise SamplerError("--label 只能含字母、数字、下划线、点和连字符")
    if not tree.is_dir() or not work.is_dir():
        raise SamplerError("--tree 与 --work 必须是已存在的目录")
    unit = load_unit(manifest, unit_id)
    launcher, command = split_launcher(unit["argv"], launcher_json)
    cwd, cwd_rel = resolve_cwd(unit, tree)
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stem = f"{safe_name(unit_id)}.{label}"
    raw_path, log_path = out_dir / f"{stem}.raw.json", out_dir / f"{stem}.log"
    if raw_path.exists() or log_path.exists():
        raise SamplerError(f"{raw_path.name} 或其日志已存在；同一单元同一标签不能重复采样（换 --label）")
    environment = build_environment(base=os.environ if base_env is None else base_env, unit=unit, work=work, out_dir=out_dir,
                                    overrides=env_overrides)
    aliases = {"TREE": str(tree), "WORK": str(work), "HOME": environment.get("HOME") or os.path.expanduser("~"),
               "TMP": environment.get("TMPDIR") or "/tmp"}
    AliasTable(aliases)  # 提前校验别名合法
    strace = shutil.which("strace", path=environment.get("PATH"))
    if strace is None:
        raise SamplerError("PATH 里找不到 strace")
    before = git_status_lines(tree)
    if before:
        raise SamplerError("采样树运行前就不干净（git status --porcelain --untracked-files=all 非空），拒绝采样：" + "; ".join(before[:5]))
    # 所有只读校验做完之后才动缓存目录（冷缓存会改名旧目录），免得后面的参数错误留下半截副作用。
    pipe = filter_pipe(output=raw_path, unit_id=unit_id, aliases=aliases, chunk_lines=chunk_lines)
    argv = build_strace_argv(launcher=launcher, strace=strace, pipe=pipe, cwd=cwd, command=command)
    commit, version = git_head(tree), strace_version(strace, environment)
    notes = prepare_caches(work, cache)
    watch = DockerWatch(environment, out_dir / f"{stem}.docker-events.jsonl") if docker_hooks else None
    if watch is not None:
        watch.start()
    timeout = float(unit.get("timeout_seconds") or DEFAULT_UNIT_TIMEOUT) * timeout_factor
    started_utc, started = utc_now(), time.monotonic()
    timed_out = False
    try:
        with log_path.open("xb") as log:
            try:
                process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            except OSError as error:
                raise SamplerError(f"启动失败（启动前缀或 strace 不可执行）：{error}") from error
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    _kill_group(process, signal.SIGKILL)
                    process.wait()
    except BaseException:
        if watch is not None:
            watch.finish()  # 不管怎么失败，docker events 子进程都要收掉
        raise
    seconds = round(time.monotonic() - started, 3)
    finished_utc = utc_now()
    waited = time.monotonic()
    while not raw_path.exists() and time.monotonic() - waited < FILTER_WAIT_SECONDS:
        time.sleep(0.2)
    filter_wait = round(time.monotonic() - waited, 3)
    docker = watch.finish() if watch is not None else None
    after = git_status_lines(tree)
    summary: dict[str, Any] = {"unit_id": unit_id, "label": label, "raw": str(raw_path), "log": str(log_path), "seconds": seconds,
                               "notes": notes}
    if not raw_path.exists():
        summary["error"] = ("命令超时被终止，过滤器没有来得及产出 raw" if timed_out else "过滤器没有产出 raw；查看日志中过滤器的报错")
        return (1 if timed_out else 3), raw_path, summary
    raw = load_document(raw_path)
    problems = validate_raw(raw)
    if problems:
        summary["error"] = "过滤器产出的 raw 不合法：" + "；".join(problems[:3])
        return 3, raw_path, summary
    returncode = process.returncode
    raw["run"] = {
        "label": label, "cache": cache, "exit_code": returncode if returncode >= 0 else None,
        "signal": -returncode if returncode < 0 else None, "timed_out": timed_out, "seconds": seconds,
        "started_utc": started_utc, "finished_utc": finished_utc,
        "command": {"launcher": launcher, "argv": command, "cwd_rel": cwd_rel, "sha256": command_digest(command, cwd_rel)},
        "git_status_before": before, "git_status_after": after, "commit": commit, "kernel": platform.release(), "strace": version,
        "sampler_sha256": file_sha256(HERE), "env_overrides": dict(sorted(env_overrides.items())), "docker": docker,
        "filter_wait_seconds": filter_wait,
    }
    write_document(raw_path, raw)
    summary.update(exit_code=raw["run"]["exit_code"], signal=raw["run"]["signal"], timed_out=timed_out, lines=raw["lines"],
                   unresolved=raw["coverage"]["unresolved"], git_status_after=len(after))
    return (0 if returncode == 0 and not timed_out else 1), raw_path, summary


# ---------------------------------------------------------------------------
# 仓库外路径的分类（merge 判写入合规、draft 起草 outside 共用同一张表）
# ---------------------------------------------------------------------------

OUTSIDE_CLASSES = ("toolchain", "dependency-content", "performance-cache", "scratch", "system", "virtual", "home-config")
# 这几类在 scope 里必须挂合同（home-config 的合同可以由路径派生，所以允许省略）。
CLASSES_NEEDING_CONTRACT = frozenset({"toolchain", "dependency-content", "performance-cache"})
# 允许被命令写入的仓库外类别：只影响速度的缓存和不绑定的暂存。往工具链、依赖内容、系统或 HOME 配置里写都说明命令在改变
# 自己的输入（例如 GOMODCACHE 被写意味着在联网下载），采样一律判无效。
WRITABLE_CLASSES = frozenset({"performance-cache", "scratch"})

# （前缀, 类别, 合同）。前缀以 / 结尾表示目录前缀，否则表示精确路径及其子路径。注意 /usr/local 与 /opt 下除工具链之外的
# 内容故意不归类：手装二进制不在 dpkg 包摘要的覆盖范围内，必须逐条人工评审，不能被一个宽前缀悄悄吞掉。
BUILTIN_CLASSES: tuple[tuple[str, str, str | None], ...] = (
    ("/usr/local/go/", "toolchain", "toolchain:go"),
    ("/opt/node-v20/", "toolchain", "toolchain:node"),
    ("{WORK}/go-build-cache/", "performance-cache", "cache:go-build-cache"),
    ("{WORK}/go-mod-cache/", "dependency-content", "cache:go-mod-cache"),
    ("{WORK}/go-tmp/", "scratch", None),
    ("{TMP}/", "scratch", None),
    ("/usr/lib/", "system", None), ("/usr/lib64/", "system", None), ("/usr/libexec/", "system", None),
    ("/usr/share/", "system", None), ("/usr/bin/", "system", None), ("/usr/sbin/", "system", None),
    ("/usr/include/", "system", None), ("/lib/", "system", None), ("/lib64/", "system", None),
    ("/bin/", "system", None), ("/sbin/", "system", None), ("/etc/", "system", None),
    ("/proc/", "virtual", None), ("/sys/", "virtual", None), ("/dev/", "virtual", None),
    ("{HOME}/", "home-config", None),
)


class Classifier:
    """最长前缀匹配；额外登记的前缀（--class-prefix）与内置前缀等长时额外登记的优先。"""

    def __init__(self, extra: Sequence[tuple[str, str, str | None]] = ()) -> None:
        entries = [*BUILTIN_CLASSES, *extra]
        ordered = sorted(enumerate(entries), key=lambda item: (-len(item[1][0]), -item[0]))
        self.entries = [entry for _, entry in ordered]
        self.prefixes = [entry[0] for entry in self.entries]

    def classify(self, key: str) -> tuple[str, str | None, str] | None:
        probe = key if key.endswith("/") else key + "/"
        for prefix, cls, contract in self.entries:
            if probe.startswith(prefix if prefix.endswith("/") else prefix + "/"):
                return cls, contract, prefix
        return None


def parse_class_prefix(spec: str) -> tuple[str, str, str | None]:
    """``前缀=类别[:合同]`` 的人工登记，用于内置表没有覆盖、但经评审认定可以接受的路径（例如 Go 的 telemetry 计数目录）。"""
    prefix, separator, rest = spec.partition("=")
    cls, _, contract = rest.partition(":")
    if (not separator or cls not in OUTSIDE_CLASSES
            or not (prefix.startswith("/") or prefix.startswith(("{WORK}/", "{HOME}/", "{TMP}/")))):
        raise SamplerError(f"--class-prefix 必须是 前缀=类别[:合同]，前缀以 / 或 {{WORK}}/、{{HOME}}/、{{TMP}}/ 开头，类别取 {'、'.join(OUTSIDE_CLASSES)}：{spec!r}")
    if cls in CLASSES_NEEDING_CONTRACT and not contract:
        raise SamplerError(f"类别 {cls} 必须带合同（前缀=类别:合同）：{spec!r}")
    return prefix, cls, contract or None


def home_contract(key: str) -> str:
    """HOME 下配置按路径派生合同编号：{HOME}/.config/go/env 变成 host-config:config-go-env。"""
    relative = key[len("{HOME}"):].strip("/") if key.startswith("{HOME}") else key.strip("/")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", relative.lstrip(".")).strip("-").lower()
    return "host-config:" + (slug or "home")


def network_problem(family: str, target: str, allowed_unix: Iterable[str]) -> str | None:
    """网络目的地是否满足“只有本机和登记的 AF_UNIX”。目的地不可判（sockaddr 没解码出来、地址解析不了、认不出的地址族）的一律
    算问题：宁可误判无效，不可漏判，更不能当成没有网络。"""
    if family == "AF_UNSPEC":
        return None
    if family == "AF_UNIX":
        return None if target in set(allowed_unix) else f"AF_UNIX 目标未登记：{target}"
    if family in ("AF_INET", "AF_INET6"):
        host = target.rsplit(":", 1)[0].strip("[]")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return f"网络目的地不可判（地址解析不了）：{family} {target}"
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return None if address.is_loopback else f"非本机地址：{family} {target}"
    if family == "unparsed":
        return f"网络目的地不可判（sockaddr 没有解码成结构，可能仍是指针或路径被截断；检查 -e verbose=…,connect）：{target}"
    return f"网络目的地不可判（无法证明是本机连接）：{family} {target}"


# ---------------------------------------------------------------------------
# merge：多次 raw 取并集，标 tracked，判有效性
# ---------------------------------------------------------------------------


def repo_rel(path: str) -> str:
    """别名路径换成相对仓库根的路径（{TREE} 本身是空串）。"""
    return "" if path == "{TREE}" else path[len("{TREE}/"):]


def tracked_sets(tree: Path) -> tuple[set[str], set[str]]:
    """git 跟踪的文件集合，和含跟踪文件的目录集合（含根的空串）。范围摘要只覆盖跟踪文件，所以 tracked 是核心判据。"""
    result = subprocess.run(["git", "-C", str(tree), "ls-files", "-z"], capture_output=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise SamplerError(f"git ls-files 失败：{tree} 不是 git 仓库？")
    files = {name.decode("utf-8", "surrogateescape") for name in result.stdout.split(b"\0") if name}
    directories = {""}
    for name in files:
        parent = os.path.dirname(name)
        while parent and parent not in directories:
            directories.add(parent)
            parent = os.path.dirname(parent)
    return files, directories


def _check_ignore(tree: Path, relatives: Sequence[str]) -> tuple[int, set[str]]:
    """一次 git check-ignore（--no-index：不看是否跟踪，只评估规则）。目录规则 out/ 对已删除的路径要带斜杠才命中，
    所以文件形式和目录形式各问一次，任一命中即算被忽略。"""
    payload = b"".join(rel.encode("utf-8", "surrogateescape") + b"\0" + rel.encode("utf-8", "surrogateescape") + b"/\0" for rel in relatives)
    result = subprocess.run(["git", "-C", str(tree), "check-ignore", "--no-index", "--stdin", "-z"], input=payload, capture_output=True)
    hits = {name.decode("utf-8", "surrogateescape").rstrip("/") for name in result.stdout.split(b"\0") if name}
    return result.returncode, {rel for rel in relatives if rel in hits}


def git_ignored(tree: Path, relatives: Sequence[str]) -> set[str]:
    """哪些路径被 .gitignore 规则忽略。

    批量询问时只要有一条路径让 git 直接报错（例如越过了仓库里的符号链接，返回 128），整批都会没有结果；这时逐条重试，
    报错的那条当作“没有被忽略”——它会作为违规写入进入判定，而不是让一条坏路径拖垮整次合并。
    """
    if not relatives:
        return set()
    code, ignored = _check_ignore(Path(tree), relatives)
    if code in (0, 1):
        return ignored
    ignored = set()
    for rel in relatives:
        code, single = _check_ignore(Path(tree), [rel])
        if code in (0, 1):
            ignored |= single
    return ignored


def _top(relative: str, depth: int = 2) -> str:
    parts = relative.split("/")
    return "/".join(parts[:depth]) + ("/" if len(parts) > depth else "")


def _differences(sets: Sequence[set[str]], limit: int = 200) -> dict[str, Any]:
    """不是每次运行都出现的路径，附出现在哪几次（按 raw 顺序的序号）。冷热差异必须逐条评审。"""
    union: set[str] = set().union(*sets) if sets else set()
    partial = sorted(item for item in union if not all(item in each for each in sets))
    return {"count": len(partial), "items": [[item, [index for index, each in enumerate(sets) if item in each]] for item in partial[:limit]]}


def merge_raws(raws: Sequence[Mapping[str, Any]], *, tree: Path, allow_unix: Sequence[str] = (),
               class_prefixes: Sequence[tuple[str, str, str | None]] = ()) -> dict[str, Any]:
    """若干次 run 的 raw 合并成 ``declared-sample/v1``。

    并集语义：仓库内路径取 kinds 并集；仓库外目录取 kinds 并集，计数取各次的最大值（两次运行读的是同一批文件，求和会翻倍）。
    有效性判据一条不放宽：每一条不满足都写进 invalid_reasons（带 [规则] 前缀，便于测试和评审按子串匹配）。
    """
    tree = Path(tree).resolve()
    if not raws:
        raise SamplerError("至少需要一份 raw")
    for index, raw in enumerate(raws):
        problems = validate_raw(raw)
        if problems:
            raise SamplerError(f"第 {index + 1} 份 raw 不合法：{'；'.join(problems[:3])}")
        if raw["run"] is None:
            raise SamplerError(f"第 {index + 1} 份 raw 缺少 run 信息：它不是 run 子命令的产物（没有退出码就无法判断采样是否有效）")
    first = raws[0]
    runs = [raw["run"] for raw in raws]
    reasons: list[str] = []

    def note(rule: str, text: str) -> None:
        item = f"[{rule}] {text}"
        if item not in reasons:
            reasons.append(item)

    # 一致性：同一单元、同一条命令、同一提交、同一别名、同一采样器；任何不一致都说明不是同一次采样会话。
    if any(raw["unit_id"] != first["unit_id"] for raw in raws):
        raise SamplerError("raw 来自不同的单元，不能合并")
    if any(run["command"] != runs[0]["command"] for run in runs):
        raise SamplerError("raw 记录的命令（启动前缀、argv、cwd）不一致，不能合并")
    for key, text in (("commit", "采样提交"), ("kernel", "内核"), ("strace", "strace 版本"), ("sampler_sha256", "采样器摘要")):
        if len({run[key] for run in runs}) > 1:
            note("consistency", f"{text}在各次运行间不一致：{sorted({run[key] for run in runs})}")
    if any(raw["aliases"] != first["aliases"] for raw in raws):
        note("consistency", "各次运行的别名（采样根）不一致，不是同一次采样会话")

    # 并集。
    repo: dict[str, int] = {}
    outside: dict[str, list[int]] = {}
    repo_sets: list[set[str]] = []
    outside_sets: list[set[str]] = []
    for raw in raws:
        repo_sets.append({path for path, _kinds in raw["repo_accesses"]})
        outside_sets.append({key for key, _kinds, _count in raw["outside_accesses"]})
        for path, kinds in raw["repo_accesses"]:
            repo[path] = repo.get(path, 0) | mask_of(kinds)
        for key, kinds, count in raw["outside_accesses"]:
            entry = outside.setdefault(key, [0, 0])
            entry[0] |= mask_of(kinds)
            entry[1] = max(entry[1], count)
    git_commands: dict[tuple[str, ...], None] = {}
    network: dict[tuple[str, str], None] = {}
    for raw in raws:
        for argv in raw["git_commands"]:
            git_commands.setdefault(tuple(argv), None)
        for item in raw["network"]:
            network.setdefault((item["family"], item["target"]), None)

    # tracked 标注。.git 内部不是跟踪文件，由 git_history 规则另行处理。
    files, directories = tracked_sets(tree)

    def is_tracked(path: str) -> bool:
        relative = repo_rel(path)
        if relative == ".git" or relative.startswith(".git/"):
            return False
        return relative in files or relative in directories

    # 运行信息与流完整性。
    for run, raw in zip(runs, raws):
        label = run["label"]
        if run["timed_out"]:
            note("exit", f"{label} 超时被终止")
        elif run["signal"] is not None:
            note("exit", f"{label} 被信号 {run['signal']} 终止")
        elif run["exit_code"] != 0:
            note("exit", f"{label} 退出码 {run['exit_code']}，不是 0")
        if run["git_status_before"] or run["git_status_after"]:
            note("git-status", f"{label} 运行前后 git status --porcelain --untracked-files=all 非空：前 {len(run['git_status_before'])} 条、"
                               f"后 {len(run['git_status_after'])} 条（命令改了跟踪文件或留下未忽略的新文件）")
        if not raw["coverage"]["stream_complete"]:
            note("stream", f"{label} 的 strace 流不完整")
        if raw["lines"] <= 0:
            note("stream", f"{label} 没有任何 strace 行（strace 没有跟踪到命令）")
    coverage_reasons: dict[str, int] = {}
    for raw in raws:
        for reason, count in raw["coverage"]["unresolved_reasons"].items():
            coverage_reasons[reason] = coverage_reasons.get(reason, 0) + count
    intolerable = {reason: count for reason, count in coverage_reasons.items()
                   if not reason.startswith(DECLARED_TOLERATED_UNRESOLVED)}
    if intolerable:
        note("unresolved", "存在不在容忍集合内的未解析原因：" + "、".join(f"{reason}×{count}" for reason, count in sorted(intolerable.items())))

    # 未跟踪读取：命令读了摘要覆盖不到的文件，依赖就没有进输入。
    untracked = sorted(path for path, mask in repo.items()
                       if mask & _READ_EXEC and not is_tracked(path) and not repo_rel(path).startswith(".git/") and repo_rel(path) != ".git")
    if untracked:
        top: dict[str, int] = {}
        for path in untracked:
            top[_top(repo_rel(path))] = top.get(_top(repo_rel(path)), 0) + 1
        note("untracked-read", f"读取了 {len(untracked)} 个未跟踪路径，主要在：" +
             "、".join(f"{prefix}（{count}）" for prefix, count in sorted(top.items(), key=lambda item: -item[1])[:5]))

    # 网络：只有本机与登记的 AF_UNIX。
    allowed_unix = list(dict.fromkeys(allow_unix))
    loopback = 0
    network_violations: list[str] = []
    for family, target in sorted(network):
        problem = network_problem(family, target, allowed_unix)
        if problem is not None:
            network_violations.append(problem)
        elif family in ("AF_INET", "AF_INET6"):
            loopback += 1
    if network_violations:
        note("network", "网络目的地不满足“只有本机与登记的 AF_UNIX”：" + "；".join(network_violations[:5]))

    # git 子进程 argv 不可见：读了哪个历史提交说不清。
    hidden = sum(1 for argv in git_commands if argv[1:2] == (GIT_ARGV_HIDDEN,))
    if hidden:
        note("git-argv", f"{hidden} 个 git 子进程的 argv 不可见（strace 没有以 verbose 解码 execve），无法确定读了哪些历史提交；"
                         "检查 strace 选项 -e verbose=execve,execveat 是否生效后重采")

    # 写入集合：仓库内只许落在被忽略的未跟踪路径；仓库外只许落在 performance-cache、scratch，或 /dev/null。
    classifier = Classifier(class_prefixes)
    violations: list[dict[str, str]] = []
    write_candidates: list[str] = []
    for path, mask in sorted(repo.items()):
        if not mask & _WRITE:
            continue
        relative = repo_rel(path)
        if relative == ".git" or relative.startswith(".git/"):
            violations.append({"path": path, "reason": "写入 .git 内部"})
        elif is_tracked(path):
            violations.append({"path": path, "reason": "写入了被跟踪的路径"})
        else:
            write_candidates.append(relative)
    ignored = git_ignored(tree, write_candidates)
    for relative in write_candidates:
        if relative not in ignored:
            violations.append({"path": "{TREE}/" + relative, "reason": "写入了未被 .gitignore 忽略的未跟踪路径"})
    sinks: list[str] = []
    outside_writes: list[list[str]] = []
    for key in sorted(outside):
        if not outside[key][0] & _WRITE:
            continue
        if key in DECLARED_WRITE_SINKS:
            sinks.append(key)
            continue
        found = classifier.classify(key)
        outside_writes.append([key, found[0] if found else "未归类"])
        if found is None:
            violations.append({"path": key, "reason": "写入了未归类的仓库外路径"})
        elif found[0] not in WRITABLE_CLASSES:
            violations.append({"path": key, "reason": f"写入了 {found[0]} 类路径（只允许 performance-cache、scratch）"})
    if violations:
        note("write", f"{len(violations)} 处写入不在 scratch、performance-cache 或被忽略的路径内，例如：" +
             "；".join(f"{item['path']}（{item['reason']}）" for item in violations[:3]))

    # 组装。
    repo_rows = [[path, kinds_of(repo[path]), is_tracked(path)] for path in sorted(repo)]
    outside_rows = [[key, kinds_of(outside[key][0]), outside[key][1]] for key in sorted(outside)]
    coverage = {
        "stream_complete": all(raw["coverage"]["stream_complete"] for raw in raws),
        "unresolved": sum(coverage_reasons.values()), "unresolved_reasons": dict(sorted(coverage_reasons.items())),
        "unfinished_calls": sum(raw["coverage"]["unfinished_calls"] for raw in raws),
        "unresolved_process_events": sum(raw["coverage"]["unresolved_process_events"] for raw in raws),
        "lines": sum(raw["coverage"]["lines"] for raw in raws),
        "detached_markers": sum(raw["coverage"]["detached_markers"] for raw in raws),
    }
    exit_code = next((run["exit_code"] for run in runs if run["exit_code"] != 0), 0)
    review = {
        "runs": [{"label": run["label"], "cache": run["cache"], "signal": run["signal"], "timed_out": run["timed_out"],
                  "started_utc": run["started_utc"], "finished_utc": run["finished_utc"], "env_overrides": run["env_overrides"],
                  "filter_wait_seconds": run["filter_wait_seconds"]} for run in runs],
        "git_status": [{"label": run["label"], "before": run["git_status_before"], "after": run["git_status_after"]} for run in runs],
        "untracked_reads": {"count": len(untracked), "sample": [repo_rel(path) for path in untracked[:200]],
                            "by_prefix": _prefix_counts(untracked)},
        "writes": {"repo_ignored": sorted({os.path.dirname(rel) + "/" for rel in ignored if os.path.dirname(rel)})[:200],
                   "violations": {"count": len(violations), "items": violations[:200]},
                   "outside": outside_writes[:200], "sinks": sinks},
        "network": {"loopback": loopback, "unix_allowed": [target for family, target in sorted(network)
                                                          if family == "AF_UNIX" and target in allowed_unix],
                    "violations": network_violations[:50]},
        "run_differences": {"repo": _differences(repo_sets), "outside": _differences(outside_sets)},
        "docker": [{"label": run["label"], **run["docker"]} for run in runs if run["docker"] is not None],
        "allowances": {"unix_sockets": allowed_unix, "class_prefixes": [[prefix, cls, contract] for prefix, cls, contract in class_prefixes]},
        "git_argv_hidden": hidden,
    }
    sample = {
        "schema_version": SAMPLE_SCHEMA, "unit_id": first["unit_id"], "command": dict(runs[0]["command"]),
        "sampled": {"commit": runs[0]["commit"], "utc": max(run["finished_utc"] for run in runs), "kernel": runs[0]["kernel"],
                    "strace": runs[0]["strace"], "sampler_sha256": runs[0]["sampler_sha256"],
                    "runs": [{"cache": run["cache"], "exit_code": run["exit_code"], "lines": raw["lines"], "seconds": run["seconds"]}
                             for run, raw in zip(runs, raws)]},
        "aliases": dict(first["aliases"]), "coverage": coverage, "repo_accesses": repo_rows, "outside_accesses": outside_rows,
        "git_commands": [list(argv) for argv in git_commands],
        "network": [{"family": family, "target": target} for family, target in sorted(network)],
        "exit_code": exit_code, "valid": not reasons, "invalid_reasons": reasons, "review": review,
    }
    problems = validate_sample(sample)
    if problems:
        raise SamplerError("内部错误：合并结果自检不通过：" + "；".join(problems[:3]))
    return sample


def _prefix_counts(untracked: Sequence[str], limit: int = 20) -> list[list[Any]]:
    counts: dict[str, int] = {}
    for path in untracked:
        prefix = _top(repo_rel(path))
        counts[prefix] = counts.get(prefix, 0) + 1
    return [[prefix, count] for prefix, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


# ---------------------------------------------------------------------------
# draft：sample 归并成 scope 草案和人工评审清单
# ---------------------------------------------------------------------------

HIT_RATIO = 0.5         # 目录里被读到的跟踪文件占比达到这个值，整个目录进 include
LEAF_MAX = 20           # 被读过且跟踪文件不超过这个数的目录整体进 include
MAX_RANGES = 12         # 范围数、单文件数超过上限只是标记待评审，草案仍照常输出
MAX_FILES = 40
LIST_LIMIT = 200        # review.md 每节最多逐条列出多少项
EVIDENCE_DIR = "tools/ci/command_input_samples"
ROOT_FILES = "(根目录文件)"
_COMMIT_IN_ARGV = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_LEDGER_COMMIT = re.compile(r'"(?:before_commit|after_commit)"\s*:\s*"([0-9a-f]{40})"')


def directory_totals(files: Iterable[str]) -> dict[str, int]:
    """每个目录（含根的空串）下的跟踪文件总数。"""
    totals: dict[str, int] = {}
    for name in files:
        parent = os.path.dirname(name)
        while True:
            totals[parent] = totals.get(parent, 0) + 1
            if not parent:
                break
            parent = os.path.dirname(parent)
    return totals


def derive_repo_scope(sample: Mapping[str, Any], files: set[str], totals: Mapping[str, int]) -> dict[str, Any]:
    """仓库内路径归并成 ranges 与 files。纯机械：

    从根往下逐层看每个目录 D：被 list 枚举（枚举结果依赖目录项集合，目录里增删文件命令行为就可能变）、或被读过且跟踪文件
    不超过 LEAF_MAX、或被读到的跟踪文件占比达到 HIT_RATIO，就整个 D 进 include，不再下钻；否则下钻到有读取的子目录，
    D 下直接被读的文件进 files。根目录永不整体 include（那等于重新声明整个仓库）。过宽与否不在这里判断，度量全部进评审清单。
    """
    reads: set[str] = set()
    listed: set[str] = set()
    for path, kinds, _tracked in sample["repo_accesses"]:
        relative, mask = repo_rel(path), mask_of(kinds)
        if relative == ".git" or relative.startswith(".git/"):
            continue  # .git 内部由 git_history 规则处理，不进 ranges
        if mask & _READ_EXEC and relative in files:
            reads.add(relative)
        if mask & _LIST:
            listed.add(relative)
    dir_read: dict[str, int] = {}
    direct: dict[str, list[str]] = {}
    for relative in reads:
        direct.setdefault(os.path.dirname(relative), []).append(relative)
        parent = os.path.dirname(relative)
        while True:
            dir_read[parent] = dir_read.get(parent, 0) + 1
            if not parent:
                break
            parent = os.path.dirname(parent)
    interesting = set(dir_read)
    for directory in listed:
        while True:
            interesting.add(directory)
            if not directory:
                break
            directory = os.path.dirname(directory)
    children: dict[str, set[str]] = {}
    for directory in interesting:
        if directory:
            children.setdefault(os.path.dirname(directory), set()).add(directory)
    include: list[str] = []
    picked: list[str] = []

    def decide(directory: str) -> None:
        total, read = totals.get(directory, 0), dir_read.get(directory, 0)
        if directory and (directory in listed or (read and total <= LEAF_MAX) or (total and read / total >= HIT_RATIO)):
            include.append(directory)
            return
        for child in sorted(children.get(directory, ())):
            decide(child)
        picked.extend(sorted(direct.get(directory, ())))

    decide("")
    include.sort()  # 登记表要确定性：范围与单文件都按路径排序，不依赖遍历顺序
    picked.sort()
    names: list[str] = []
    for directory in include:
        base, name, number = directory.replace("/", "-"), directory.replace("/", "-"), 2
        while name in names:
            name, number = f"{base}-{number}", number + 1
        names.append(name)
    return {"ranges": [{"name": name, "include": [directory + "/"], "exclude": []} for name, directory in zip(names, include)],
            "files": picked, "reads": len(reads), "listed": sorted(listed), "include_dirs": include, "dir_read": dir_read,
            "read_files": reads}


def rollup(read_files: Iterable[str], depth: int) -> list[tuple[str, int]]:
    """被读到的跟踪文件按前 depth 层目录汇总（目录, 被读文件数），被读多的在前；根目录下的文件归入“(根目录文件)”。

    真实读集常常横跨很多目录（backend 全量 go test 实测会读 frontend、docs、deploy 等处几千个文件），机械归并出的范围会很碎，
    评审者要决定合并到多粗，所以评审清单里附这张纯度量表。
    """
    counts: dict[str, int] = {}
    for relative in read_files:
        directory = os.path.dirname(relative)
        key = "/".join(directory.split("/")[:depth]) if directory else ROOT_FILES
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def covering_include(directory: str, include: Sequence[str]) -> str | None:
    for item in include:
        if directory == item or directory.startswith(item + "/"):
            return item
    return None


def scan_ledger_commits(tree: Path, limit_files: int = 5000) -> set[str]:
    """扫描 docs/egress 下台账里的 before_commit 与 after_commit，供与采样到的 git 调用交叉核对（只提示，不自动并入）。"""
    root = Path(tree) / "docs" / "egress"
    commits: set[str] = set()
    if not root.is_dir():
        return commits
    for count, path in enumerate(sorted(root.rglob("*.json"))):
        if count >= limit_files:
            break
        try:
            if path.stat().st_size <= 8 * 1024 * 1024:
                commits.update(_LEDGER_COMMIT.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return commits


def _is_ancestor_probe(key: str, kinds: Sequence[str], prefixes: Sequence[str]) -> bool:
    """仅元数据（stat、dir、missing）的上层目录探测：例如 PATH 搜索会探测 /、/usr/；仍列出评审，但不算“未归类”。"""
    if set(kinds) - {"stat", "dir", "missing"}:
        return False
    probe = key if key.endswith("/") else key + "/"
    return key == "/" or any(prefix.startswith(probe) for prefix in prefixes)


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _items(lines: list[str], items: Sequence[Any], render: Any, total: int | None = None) -> None:
    for item in items[:LIST_LIMIT]:
        lines.append("- " + render(item))
    total = len(items) if total is None else total
    if total > LIST_LIMIT:
        lines.append(f"- ……共 {total} 项，只列前 {LIST_LIMIT} 项（完整内容见证据文件）")


def draft_unit(sample: Mapping[str, Any], *, evidence_sha: str, safe: str, tree: Path, files: set[str], totals: Mapping[str, int],
               classifier: Classifier, head: str | None, ledger: set[str] | None) -> tuple[dict[str, Any], list[str]]:
    """一个 sample 的登记表条目和评审小节。无效采样只给 rerun-only，评审清单照样把原因和证据摘要列全。"""
    review, coverage = sample["review"], sample["coverage"]
    unit_id = sample["unit_id"]
    repo = derive_repo_scope(sample, files, totals)

    # 写入且被忽略的路径所在目录，作为 repo_ignored 草案。
    writes = [repo_rel(path) for path, kinds, tracked in sample["repo_accesses"]
              if "write" in kinds and not tracked and not repo_rel(path).startswith(".git")]
    ignored = git_ignored(tree, writes)
    repo_ignored = sorted({os.path.dirname(rel) + "/" for rel in ignored if os.path.dirname(rel)})

    # 仓库外：按分类前缀归并；HOME 下逐条（要登记存在性与内容）；归不进去的逐条列未归类。
    outside: dict[str, tuple[str, str | None]] = {}
    unclassified: list[list[Any]] = []
    ancestors: list[list[Any]] = []
    by_class: dict[str, int] = {}
    etc_items: list[list[Any]] = []
    home_items: list[list[Any]] = []
    for key, kinds, count in sample["outside_accesses"]:
        found = classifier.classify(key)
        # HOME 根目录自身的 stat 也是祖先探测，不是一份要登记的配置文件。
        if found is not None and key in ("{HOME}", "{HOME}/") and not set(kinds) - {"stat", "dir", "missing"}:
            ancestors.append([key, kinds, count])
            continue
        if found is None:
            (ancestors if _is_ancestor_probe(key, kinds, classifier.prefixes) else unclassified).append([key, kinds, count])
            continue
        cls, contract, prefix = found
        by_class[cls] = by_class.get(cls, 0) + 1
        if cls == "home-config":
            outside[key] = (cls, contract or home_contract(key))
            home_items.append([key, kinds, count])
        else:
            outside[prefix] = (cls, contract)
            if prefix == "/etc/":
                etc_items.append([key, kinds, count])
    outside_rows = [{"prefix": prefix, "class": cls, **({"contract": contract} if contract else {})}
                    for prefix, (cls, contract) in sorted(outside.items())]
    contracts = sorted({contract for _cls, contract in outside.values() if contract})

    # git 历史：从采样到的 git 子进程 argv 里机械提取 40 位提交号，符号引用（HEAD 等）只提示。
    commits: list[str] = []
    symbolic: list[str] = []
    for argv in sample["git_commands"]:
        for part in argv[1:]:
            for found in _COMMIT_IN_ARGV.findall(part):
                if found not in commits:
                    commits.append(found)
            if re.search(r"\bHEAD\b|refs/", part) and part not in symbolic:
                symbolic.append(part)
    git_internal = [path for path, kinds, _tracked in sample["repo_accesses"]
                    if repo_rel(path).startswith(".git/") and ("read" in kinds or "exec" in kinds)]
    scope_repo: dict[str, Any] = {"ranges": repo["ranges"], "files": repo["files"], "list_ok": [], "repo_ignored": repo_ignored, "head": False}
    if commits:
        scope_repo["git_history"] = {"commits": commits, "reason": "由采样到的 git 子进程 argv 机械提取；须与 docs/egress 台账里的提交号交叉核对"}
    scope = {"schema_version": SCOPE_SCHEMA, "repo": scope_repo, "make_expansion": [], "outside": outside_rows, "contracts": contracts,
             "network": "loopback-only"}

    if sample["valid"]:
        entry: dict[str, Any] = {"status": "declared", "scope": scope, "sample": {
            "evidence": f"{EVIDENCE_DIR}/{safe}.sample.json", "evidence_sha256": evidence_sha, "command_sha256": sample["command"]["sha256"],
            "launcher_sha256": sha256_json(sample["command"]["launcher"]), "sampled_commit": sample["sampled"]["commit"],
            "sampled_at_utc": sample["sampled"]["utc"], "sampler_sha256": sample["sampled"]["sampler_sha256"],
            "runs": len(sample["sampled"]["runs"])}}
    else:
        entry = {"status": "rerun-only", "reason": ("采样无效：" + "；".join(sample["invalid_reasons"]))[:800]}

    lines = [f"## 单元 `{_md(unit_id)}`", ""]
    lines += [f"- 采样有效：**{'是' if sample['valid'] else '否'}**；登记状态草案：`{entry['status']}`；证据文件 `{safe}.sample.json`，摘要 `{evidence_sha}`"]
    lines += [f"- 采样提交 `{sample['sampled']['commit']}`；命令 `{_md(' '.join(sample['command']['argv']))}`，cwd 相对测试树 `{_md(sample['command']['cwd_rel'])}`；"
              f"共 {len(sample['sampled']['runs'])} 次运行（{'、'.join(str(item['cache']) for item in sample['sampled']['runs'])}）"]
    if head is not None and head != sample["sampled"]["commit"]:
        lines.append(f"- **注意**：当前树 HEAD `{head}` 与采样提交不同，过宽度量按当前树计算，评审时留意。")
    lines += ["", "### 1 采样有效性", ""]
    lines += [f"- {_md(reason)}" for reason in sample["invalid_reasons"]] or ["- 全部判据满足：退出码 0、运行前后 git status 为空、流完整、"
                                                                              "unresolved 都在容忍集合内、无未跟踪读取、网络只有本机、写入都在允许范围。"]
    lines += ["", "### 2 过宽度量（每个 include）", ""]
    if repo["include_dirs"]:
        lines += ["| include | 跟踪文件数 | 被读文件数 | 过宽（未读） | 命中率 | 被枚举 |", "|---|---|---|---|---|---|"]
        for directory in repo["include_dirs"]:
            total, read = totals.get(directory, 0), repo["dir_read"].get(directory, 0)
            ratio = f"{read / total:.3f}" if total else "无跟踪文件"
            lines.append(f"| `{_md(directory)}/` | {total} | {read} | {total - read} | {ratio} | {'是' if directory in repo['listed'] else '否'} |")
    else:
        lines.append("没有 include 目录。")
    root_files = sum(1 for name in files if "/" not in name)
    for title, depth, limit in (("顶层目录", 1, None), ("被读文件最多的二级目录（前 10）", 2, 10)):
        lines += ["", f"按{title}汇总（决定把碎片范围合并到多粗时的参考；只是度量，不是建议）：", "",
                  "| 目录 | 跟踪文件数 | 被读文件数 | 命中率 |", "|---|---|---|---|"]
        for name, read in rollup(repo["read_files"], depth)[:limit]:
            total = root_files if name == ROOT_FILES else totals.get(name, 0)
            shown = name if name == ROOT_FILES else name + "/"
            lines.append(f"| `{_md(shown)}` | {total} | {read} | {f'{read / total:.3f}' if total else '无跟踪文件'} |")
    lines += ["", f"- 单文件（files）共 {len(repo['files'])} 个，被读到的跟踪文件共 {repo['reads']} 个。"]
    if len(repo["ranges"]) > MAX_RANGES or len(repo["files"]) > MAX_FILES:
        lines.append(f"- **超限待评审**：ranges {len(repo['ranges'])}（上限 {MAX_RANGES}）、files {len(repo['files'])}（上限 {MAX_FILES}）；"
                     "可参考上面的汇总表决定合并到多粗。")
    _items(lines, repo["files"], lambda item: f"`{_md(item)}`")
    lines += ["", "### 3 被 list 枚举的目录（逐个决定保持 include 还是降为 list_ok）", ""]
    if repo["listed"]:
        for directory in repo["listed"]:
            cover = covering_include(directory, repo["include_dirs"]) if directory else None
            state = ("本身已 include" if cover == directory else f"被 include `{_md(cover)}/` 覆盖" if cover is not None
                     else "根目录，不会整体 include，需决定是否列入 list_ok" if not directory else "未被任何 include 覆盖")
            total, read = totals.get(directory, 0), repo["dir_read"].get(directory, 0)
            lines.append(f"- `{_md(directory or '.')}/`：跟踪文件 {total}、被读 {read}；{state}")
    else:
        lines.append("没有被枚举的仓库内目录。")
    lines += ["", "### 4 写入集合", ""]
    wr = review["writes"]
    lines += [f"- 仓库内被忽略的写入目录（repo_ignored 草案）：{('、'.join('`' + _md(item) + '`' for item in repo_ignored)) or '无'}",
              f"- 违规写入 {wr['violations']['count']} 处" + ("" if not wr["violations"]["count"] else "（下列为前几处）")]
    _items(lines, wr["violations"]["items"], lambda item: f"`{_md(item['path'])}`：{_md(item['reason'])}", wr["violations"]["count"])
    lines.append(f"- 仓库外写入目录与类别：{('、'.join('`' + _md(key) + '`→' + _md(cls) for key, cls in wr['outside'])) or '无'}")
    lines.append(f"- 已豁免的无害写入：{('、'.join('`' + _md(item) + '`' for item in wr['sinks'])) or '无'}（写入 /dev/null 没有可观察状态）")
    lines += ["", "### 5 untracked_reads（读取了未跟踪路径，必须为空）", ""]
    untracked = review["untracked_reads"]
    lines.append(f"- 共 {untracked['count']} 个" + ("；按前两级目录：" + "、".join(f"`{_md(prefix)}`（{count}）" for prefix, count in untracked["by_prefix"])
                                                 if untracked["count"] else ""))
    _items(lines, untracked["sample"], lambda item: f"`{_md(item)}`", untracked["count"])
    lines += ["", "### 6 network", ""]
    net = review["network"]
    lines.append(f"- 本机连接目的地 {net['loopback']} 个；已登记的 AF_UNIX：{('、'.join('`' + _md(x) + '`' for x in net['unix_allowed'])) or '无'}")
    _items(lines, sample["network"], lambda item: f"{_md(item['family'])} `{_md(item['target'])}`")
    lines += [f"- 违规 {len(net['violations'])} 条" + ("：" + "；".join(_md(item) for item in net["violations"]) if net["violations"] else "")]
    if any(item["family"] == "AF_UNIX" for item in sample["network"]):
        lines.append("- 提示：`declared-scope/v1` 目前没有承载“登记的 AF_UNIX 目标”的字段，草案无法写入；待登记表 schema 确认后补。")
    lines += ["", "### 7 冷热差异（不是每次运行都出现的路径）", ""]
    for name, key in (("仓库内路径", "repo"), ("仓库外目录", "outside")):
        diff = review["run_differences"][key]
        lines.append(f"- {name}：{diff['count']} 项")
        _items(lines, diff["items"], lambda item: f"`{_md(item[0])}` 出现在第 {'、'.join(str(i + 1) for i in item[1])} 次", diff["count"])
    lines += ["", "### 8 git status 前后（必须都为空）", ""]
    for item in review["git_status"]:
        lines.append(f"- {_md(item['label'])}：前 {len(item['before'])} 条、后 {len(item['after'])} 条"
                     + ("：" + "；".join(f"`{_md(line)}`" for line in (item['before'] + item['after'])[:5]) if item["before"] or item["after"] else ""))
    for item in review["docker"]:
        lines.append(f"- docker（{_md(item['label'])}）：新增容器 {len(item['new_containers'])} 个、create 事件 {len(item['created'])} 个"
                     + (f"，错误 {_md(item['error'])}" if item["error"] else ""))
    lines += ["", "### 9 unresolved_reasons", ""]
    lines.append(f"- unresolved 总数 {coverage['unresolved']}；strace 标记行（detached 或 unfinished）{coverage['detached_markers']} 条已单独计数")
    lines += [f"- `{_md(reason)}`：{count}" for reason, count in coverage["unresolved_reasons"].items()]
    lines += ["", "### 10 仓库外目录归类", ""]
    lines.append("- 已归类：" + ("、".join(f"{_md(cls)} {count}" for cls, count in sorted(by_class.items())) or "无"))
    lines += [f"- 未归类 {len(unclassified)} 项（必须逐条评审，或用 `--class-prefix` 登记后重新 merge）"]
    _items(lines, unclassified, lambda item: f"`{_md(item[0])}` {_md(item[1])} ×{item[2]}")
    lines += [f"- 仅元数据的上层目录探测 {len(ancestors)} 项（PATH 搜索、Go 往上找 go.mod 等）：{('、'.join('`' + _md(item[0]) + '`' for item in ancestors[:30])) or '无'}"]
    lines += [f"- HOME 下配置 {len(home_items)} 项（要逐路径登记存在性与内容）"]
    _items(lines, home_items, lambda item: f"`{_md(item[0])}` {_md(item[1])}")
    lines += [f"- /etc 下读取 {len(etc_items)} 项（环境指纹只覆盖包管理器管理的文件，手改的文件要评审）"]
    _items(lines, etc_items, lambda item: f"`{_md(item[0])}` {_md(item[1])}")
    lines += ["", "### 11 git 历史与其它提示", ""]
    lines.append(f"- 采样到 {len(sample['git_commands'])} 条 git 子进程命令；机械提取的提交号 {len(commits)} 个；"
                 f"读取 .git 内部的路径 {len(git_internal)} 个")
    _items(lines, [" ".join(argv) for argv in sample["git_commands"]], lambda item: f"`{_md(item)}`")
    if symbolic:
        lines.append("- **符号引用**（HEAD、refs/ 等）：" + "、".join(f"`{_md(item)}`" for item in symbolic[:10]) + "；读 HEAD 的单元通常要保留 head 输入，不能机械归并")
    if ledger is not None and (commits or sample["git_commands"] or git_internal):
        extra = sorted(ledger - set(commits))
        lines.append(f"- 台账交叉核对：docs/egress 台账里出现 {len(ledger)} 个提交号，其中 {len(extra)} 个没有出现在采样的 git 调用里（只提示，不自动并入）")
        _items(lines, extra, lambda item: f"`{item}`")
    if git_internal and not commits:
        lines.append("- **注意**：读取了 .git 内部却没有提取到提交号，需要人工确认 git_history。")
    if sample["command"]["argv"][:1] == ["make"]:
        lines.append("- 该单元经 make 运行：要决定是用整个 `Makefile` 文件做输入，还是用 make_expansion 合同（草案里 make_expansion 为空）。")
    lines.append("")
    return entry, lines


def draft_registry(samples_dir: Path, tree: Path, *, class_prefixes: Sequence[tuple[str, str, str | None]] = ()) -> tuple[dict[str, Any], str]:
    tree, samples_dir = Path(tree).resolve(), Path(samples_dir)
    paths = sorted(samples_dir.glob("*.sample.json"))
    if not paths:
        raise SamplerError(f"{samples_dir} 里没有 *.sample.json")
    loaded: list[tuple[dict[str, Any], str, str]] = []
    seen: dict[str, str] = {}
    for path in paths:
        document = load_document(path)
        problems = validate_sample(document)
        if problems:
            raise SamplerError(f"{path.name} 不合法：{'；'.join(problems[:3])}")
        safe = safe_name(document["unit_id"])
        if path.name != f"{safe}.sample.json":
            raise SamplerError(f"{path.name} 的文件名必须是 {safe}.sample.json（登记表按这个名字引用证据）")
        if safe in seen:
            raise SamplerError(f"单元 {document['unit_id']} 与 {seen[safe]} 的安全文件名冲突")
        seen[safe] = document["unit_id"]
        loaded.append((document, safe, file_sha256(path)))
    files, _directories = tracked_sets(tree)
    totals = directory_totals(files)
    try:
        head: str | None = git_head(tree)
    except SamplerError:
        head = None
    ledger = scan_ledger_commits(tree) if any(doc["git_commands"] or any(r[0].startswith("{TREE}/.git/") for r in doc["repo_accesses"])
                                              for doc, _safe, _sha in loaded) else None
    units: dict[str, Any] = {}
    sections: list[list[str]] = []
    rows: list[str] = []
    for document, safe, evidence_sha in loaded:
        # 采样时用 --class-prefix 登记过的前缀随 sample 留痕，draft 自动沿用，命令行追加的再叠加。
        try:
            recorded = [tuple(item) for item in document["review"]["allowances"]["class_prefixes"]]
            classifier = Classifier([*recorded, *class_prefixes])  # type: ignore[list-item]
            entry, section = draft_unit(document, evidence_sha=evidence_sha, safe=safe, tree=tree, files=files, totals=totals,
                                        classifier=classifier, head=head, ledger=ledger)
        except (KeyError, TypeError, IndexError, ValueError) as error:
            # validate_sample 只校验到 review 的顶层键；内层结构被手工改坏时给出明确错误，而不是回溯。
            raise SamplerError(f"单元 {document['unit_id']} 的 sample 内层结构不符（review 被改动过？）：{type(error).__name__} {error}") from error
        units[document["unit_id"]] = entry
        sections.append(section)
        scope = entry.get("scope")
        rows.append(f"| `{_md(document['unit_id'])}` | {'是' if document['valid'] else '**否**'} | {len(document['repo_accesses'])} | "
                    f"{len(document['outside_accesses'])} | {len(scope['repo']['ranges']) if scope else '-'} | "
                    f"{len(scope['repo']['files']) if scope else '-'} | {_md(entry['status'])} |")
    valid = sum(1 for doc, _s, _h in loaded if doc["valid"])
    text = ["# 声明级承接采样评审清单", "",
            "> 由 `declared_sampler.py draft` 机械生成：只按目录命中率等规则归并，**不做任何语义判断的自动放行**。每一节都需要人工确认；",
            "> 草案里的 `list_ok` 与 `make_expansion` 一律为空，由评审者决定。", "",
            f"- 采样器摘要：`{file_sha256(HERE)}`", f"- 当前树 HEAD：`{head or '未知'}`", f"- 单元 {len(loaded)} 个，采样有效 {valid} 个，无效 {len(loaded) - valid} 个", "",
            "## 总览", "", "| 单元 | 采样有效 | 仓库内路径 | 仓库外目录 | ranges | files | 登记状态草案 |", "|---|---|---|---|---|---|---|", *rows, ""]
    for section in sections:
        text += section
    registry = {"schema_version": REGISTRY_SCHEMA, "units": dict(sorted(units.items()))}
    return registry, "\n".join(text) + "\n"


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="declared_sampler.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    filt = sub.add_parser("filter", help="读标准输入的 strace 输出，流式输出 declared-sample-raw/v1（作为 strace -o '|…' 管道的另一端）")
    filt.add_argument("--output", type=Path, required=True)
    filt.add_argument("--unit-id", required=True)
    filt.add_argument("--alias", action="append", default=[], help="NAME=绝对路径，TREE、WORK、HOME、TMP 都要给")
    filt.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES, help="每处理多少行排空一次解析器的路径字典；0 表示只在结束时排空")
    run = sub.add_parser("run", help="从门禁清单取命令单元，在 strace 下执行一次并保存 raw")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--unit", required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--tree", type=Path, required=True, help="采样用的测试树；必须与生成清单时 plan 的 --tree 相同")
    run.add_argument("--work", type=Path, required=True, help="工作根；Go 的三个缓存固定在其下 go-build-cache、go-mod-cache、go-tmp")
    run.add_argument("--cache", choices=("cold", "warm"), default="cold",
                     help="cold：为 GOCACHE 准备空目录（旧的非空目录改名保留，不删除）；warm：复用上一次写热的目录。前端的 tsbuildinfo、vitest 缓存需自行准备")
    run.add_argument("--label", help="文件名标签，默认等于 --cache；同一单元要多采几次（例如离线验证）时用不同标签")
    run.add_argument("--launcher-json", help="生成清单时的启动前缀（JSON 字符串列表）；不给则只认已知的 ARM64 前缀")
    run.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="追加或覆盖环境变量（例如 GOPROXY=off），记入 raw 留痕")
    run.add_argument("--docker-hooks", action="store_true", help="记录 docker ps -a 前后差集与 docker events（integration 类单元用）")
    run.add_argument("--timeout-factor", type=float, default=DEFAULT_TIMEOUT_FACTOR, help="超时 = 清单超时 × 该系数")
    run.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES)
    merge = sub.add_parser("merge", help="多份 raw 取并集，标 tracked，判有效性，生成 declared-sample/v1")
    merge.add_argument("--raw", type=Path, action="append", required=True)
    merge.add_argument("--tree", type=Path, required=True)
    merge.add_argument("--out", type=Path, required=True)
    merge.add_argument("--allow-unix", action="append", default=[], metavar="PATH", help="登记允许连接的 AF_UNIX 目标（如 /var/run/docker.sock）")
    merge.add_argument("--class-prefix", action="append", default=[], metavar="前缀=类别[:合同]",
                       help="登记内置表没有覆盖、但经评审可以接受的仓库外前缀；会随 sample 留痕，draft 自动沿用")
    draft = sub.add_parser("draft", help="把 sample 起草成登记表草案和评审清单")
    draft.add_argument("--samples", type=Path, required=True)
    draft.add_argument("--tree", type=Path, required=True)
    draft.add_argument("--registry-out", type=Path, required=True)
    draft.add_argument("--review-out", type=Path, required=True)
    draft.add_argument("--class-prefix", action="append", default=[], metavar="前缀=类别[:合同]")
    return parser.parse_args(list(argv))


def _parse_env(items: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise SamplerError(f"--env 必须是 KEY=VALUE：{item!r}")
        values[name] = value
    return values


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "filter":
            # 过滤器是 strace 的管道子进程，与被跟踪命令在同一个进程组：超时终止命令时整组都会收到 SIGTERM，交互运行时
            # 按 Ctrl-C 也是整组 SIGINT。忽略这两个信号，让它读到 strace 退出后关闭管道的 EOF，再把已有的读集写出来——
            # 超时的采样虽然无效，读集仍有诊断价值，而不是让 run 误报成“过滤器故障”。SIGKILL 不可忽略，兜底仍在。
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            stream = (line.decode("utf-8", "surrogateescape") for line in sys.stdin.buffer)
            document = filter_stream(stream, parse_aliases(args.alias), unit_id=args.unit_id, chunk_lines=args.chunk_lines)
            write_document(args.output, document)
            return 0
        if args.command == "run":
            code, _raw, summary = run_sample(
                manifest=args.manifest, unit_id=args.unit, out_dir=args.out, tree=args.tree, work=args.work, cache=args.cache,
                label=args.label, launcher_json=args.launcher_json, env_overrides=_parse_env(args.env), docker_hooks=args.docker_hooks,
                timeout_factor=args.timeout_factor, chunk_lines=args.chunk_lines)
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
            if code == 1:
                print("declared_sampler 警告：命令退出码非零或超时，raw 已写出但 merge 会判采样无效", file=sys.stderr)
            elif code == 3:
                print("declared_sampler 错误：" + str(summary.get("error")), file=sys.stderr)
            return code
        if args.command == "merge":
            raws = [load_document(path) for path in args.raw]
            sample = merge_raws(raws, tree=args.tree, allow_unix=args.allow_unix,
                                class_prefixes=[parse_class_prefix(item) for item in args.class_prefix])
            write_document(args.out, sample)
            print(json.dumps({"unit_id": sample["unit_id"], "valid": sample["valid"], "invalid_reasons": sample["invalid_reasons"],
                              "repo_paths": len(sample["repo_accesses"]), "outside_directories": len(sample["outside_accesses"])},
                             ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "draft":
            registry, review = draft_registry(args.samples, args.tree, class_prefixes=[parse_class_prefix(item) for item in args.class_prefix])
            write_document(args.registry_out, registry)
            args.review_out.parent.mkdir(parents=True, exist_ok=True)
            args.review_out.write_text(review, encoding="utf-8")
            declared = sum(1 for entry in registry["units"].values() if entry["status"] == "declared")
            print(json.dumps({"units": len(registry["units"]), "declared": declared, "rerun_only": len(registry["units"]) - declared},
                             ensure_ascii=False, sort_keys=True))
            return 0
    except SamplerError as error:
        print(f"declared_sampler 拒绝：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
