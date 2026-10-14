"""采样器（``tools/ci/declared_sampler.py``）侧的 git“固定用法”判定规格 v2：步骤 5 的 WP4d（S2、S5）。

规格 v2 是调度者在独立验收 #3、#4 之后统一的口径（``git_fixed_spec_v2.md``）：采样器 ``git_usage_problem`` 与登记侧 ``unit_records._declared_git_usage_problem``
必须对同一条 argv 给出同样的“固定／不固定”。两个模块各自是被钉住的纯函数，**互相不 import**；同口径靠测试交叉核对：

* 这个模块是**采样器这一侧**——把规格里的常量表和一份语料的期望值写成字面量，断言采样器的实现与它们相等、逐条判定一致。**这里不和登记侧做任何对拍**
  （登记侧正由另一个工作包同时修改）；
* 登记侧的对拍测试由那个工作包在合入之后补：它可以直接 ``from tools.official_client_capture.tests.test_ci_declared_sampler_git_spec import …``
  取下面这些模块级常量，同时 import 采样器和登记侧两个模块做断言（测试允许，被测模块之间不允许互相 import）：

  - ``GIT_SPEC_V2_SUBCOMMANDS``：子命令 → (精确选项集, 前缀集, 修订参数处理方式) 的表；
  - ``GIT_SPEC_V2_PLAIN_GLOBALS``：子命令名之前允许出现的全局选项；
  - ``GIT_SPEC_V2_FORMAT_PLACEHOLDERS``：格式串里放行的占位符（不含 ``%`` 本身；``%%`` 与 ``%x`` 加两位十六进制另列）、``GIT_SPEC_V2_FORMAT_HEX_RE``；
  - ``GIT_SPEC_V2_DASHDASH_REVISION_SUBCOMMANDS``：``--`` 之后仍是修订（argv 里出现 ``--`` 一律不固定）的子命令；
  - ``GIT_SPEC_V2_ALLOWED_PLACEHOLDERS`` / ``GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS``：每条放行／禁止的占位符各一个例子；
  - ``GIT_SPEC_V2_CORPUS``：至少 400 条 ``(argv, 期望是否固定)``，覆盖规格列出的全部边界。期望值是按规格**手工**写下的（由构造规则决定），不是拿实现跑出来的；
  - ``GIT_SPEC_V2_UNSPECIFIED``：语料里规格没有明说的那几处取舍，``(argv, 期望, 取舍说明)``（它们同时也在语料里）。

语料里**规格没有明说**的几处取舍（采样器侧取保守判定，登记侧同步时如有不同要一起裁决）：
  ① 不含 ``%``、没有 ``format:``／``tformat:`` 前缀、又不是精确 ``raw`` 的格式值（``--format=oneline``、``--pretty=medium``、``--pretty=myalias``、空串）——在 git 里是命名格式或
     ``pretty.<名字>`` 配置别名，不是自定义格式串，一律不固定；
  ② ``--pretty=%H``（无前缀但含 ``%``）：git 把它当隐含的 tformat，与 ``--format=%H`` 同样处理，固定；
  ③ ``--format=format:%H``／``--format=tformat:%H``：前缀照样剥掉，按剩下的串判，固定。
"""

from __future__ import annotations

import unittest

from tools.ci import declared_sampler as ds

A = "a" * 40
B = "b" * 40
C = "c" * 40
GIT = "git"

# ---------------------------------------------------------------------------
# 常量表（规格 v2 §1、§2、§3）：字面量，与采样器的实现逐项相等
# ---------------------------------------------------------------------------

# 子命令 -> (精确选项集, 前缀集, 修订参数的处理方式)。规格 §1：相对 WP1e 的表，只删去 show 与 log 的 --oneline，其余不变。
#   all：-- 之前的位置参数全是修订，至少一个；first：只有第一个位置参数是修订，后面是路径（ls-tree）；
#   range：log、rev-list，位置参数是固定提交、两端都是完整提交号的 A..B／A...B、或 ^固定提交，至少一个正向修订。
GIT_SPEC_V2_SUBCOMMANDS = {
    "show": (frozenset({"-s", "--no-patch", "--quiet", "--stat", "--name-only", "--name-status", "--no-color", "-z"}), ("--format=", "--pretty="), "all"),
    "cat-file": (frozenset({"-t", "-s", "-e", "-p"}), (), "all"),
    "rev-parse": (frozenset({"--verify", "-q", "--quiet"}), (), "all"),
    "merge-base": (frozenset({"--is-ancestor", "--all"}), (), "all"),
    "ls-tree": (frozenset({"-r", "-d", "-t", "-l", "--long", "--name-only", "--name-status", "--full-name", "--full-tree", "-z"}), ("--abbrev=",), "first"),
    "diff-tree": (frozenset({"-r", "-t", "-z", "-M", "-C", "--root", "--no-commit-id", "--name-only", "--name-status", "--raw", "--stat", "--no-renames",
                             "--find-renames", "--quiet"}), (), "all"),
    "log": (frozenset({"--parents", "--no-walk", "--reverse", "--topo-order", "--date-order", "--first-parent", "--no-merges", "--name-only", "--name-status",
                       "--stat", "--quiet", "--no-color", "--count", "-z"}), ("--format=", "--pretty=", "--max-count="), "range"),
    "rev-list": (frozenset({"--parents", "--reverse", "--topo-order", "--date-order", "--first-parent", "--no-merges", "--count", "--quiet", "--objects", "--not",
                            "-z"}), ("--max-count=",), "range"),
}
# 子命令名之前允许出现的全局选项（只有这两个不改变命令作用的仓库与结果）。
GIT_SPEC_V2_PLAIN_GLOBALS = frozenset({"--no-pager", "--no-optional-locks"})
# 规格 §3：这三个子命令里 -- 只是“选项结束”，其后仍是修订：argv 里出现 -- 一律不固定。
GIT_SPEC_V2_DASHDASH_REVISION_SUBCOMMANDS = frozenset({"merge-base", "cat-file", "rev-parse"})
# 规格 §2：格式串里放行的占位符（写在 % 之后的部分）。``%%``（字面百分号）用 "%" 表示；``%x`` 加恰好两位十六进制另由 GIT_SPEC_V2_FORMAT_HEX_RE 描述。
GIT_SPEC_V2_FORMAT_PLACEHOLDERS = frozenset({"H", "T", "P", "an", "ae", "aI", "at", "cn", "ce", "cI", "ct", "s", "b", "B", "n", "%"})
GIT_SPEC_V2_FORMAT_HEX_RE = r"x[0-9a-fA-F]{2}"
# 规格 §2：命名格式里只放行精确的 raw；自定义格式串的前缀。
GIT_SPEC_V2_FORMAT_NAMES = frozenset({"raw"})
GIT_SPEC_V2_FORMAT_PREFIXES = ("format:", "tformat:")

# 规格 §2：每条放行的占位符一个例子（占位符本身 → 夹在普通字符里的格式串）与每条禁止的占位符一个例子。放行的与禁止的各有一个反方向的对照：
# 放行的占位符前面加 git 的魔法修饰符 ``+`` 就不在表里了；禁止的占位符前面再加一个 ``%`` 转义成字面文字（``%%D``）就只剩普通字符。
GIT_SPEC_V2_ALLOWED_PLACEHOLDERS = {
    "%H": "提交 %H 完毕", "%T": "tree=%T", "%P": "parents: %P", "%an": "%an wrote", "%ae": "<%ae>", "%aI": "at %aI", "%at": "ts=%at", "%cn": "%cn committed",
    "%ce": "<%ce>", "%cI": "at %cI", "%ct": "ts=%ct", "%s": "subject: %s", "%b": "body:%b", "%B": "raw:%B", "%n": "line1%nline2", "%%": "100%% sure",
    "%x00": "sep%x00sep", "%x0a": "nl%x0a", "%x0A": "NL%x0A", "%xff": "ff%xff", "%xFF": "FF%xFF",
}
GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS = {
    "%D": "refs: %D", "%d": " (%d)", "%N": "notes: %N", "%S": "src=%S", "%gd": "%gd", "%gD": "%gD", "%gn": "%gn", "%gN": "%gN", "%ge": "%ge", "%gE": "%gE", "%gs": "%gs",
    "%(describe)": "%(describe)", "%(decorate)": "%(decorate)", "%(trailers)": "%(trailers)", "%(trailers:only)": "%(trailers:only)", "%(contents)": "%(contents)",
    "%h": "short %h", "%t": "short tree %t", "%p": "short parents %p", "%ad": "date %ad", "%cd": "date %cd", "%aD": "date %aD", "%cD": "date %cD", "%ar": "ago %ar",
    "%cr": "ago %cr", "%aN": "%aN", "%aE": "%aE", "%cN": "%cN", "%cE": "%cE",
}
# 不在白名单里的其它 %… 序列（规格“不在表里的一律不固定”）：日期的其它写法、mailmap 的小写变体、f／e、验签、颜色、排版、魔法修饰符、不完整的转义。
GIT_SPEC_V2_OTHER_UNLISTED_SEQUENCES = (
    "%ai", "%ci", "%as", "%cs", "%ah", "%ch", "%al", "%cl", "%aL", "%cL", "%f", "%e", "%G?", "%GG", "%GS", "%GK", "%GF", "%GP", "%GT", "%Cred", "%Cgreen", "%Creset",
    "%C(auto)", "%w(0)", "%<(10)", "%>(10)", "%<|(10)", "%m", "%+b", "%-b", "% b", "%+n", "%x", "%x0", "%x0g", "%xg0", "%xzz", "%x-1", "%z", "%1", "%", "%(", "%a", "%c",
    "%ax", "%cx", "%g", "%gx", "%G", "%H%", "%%%",
)


def _git(*args: str) -> list[str]:
    return [GIT, *args]


def _build_corpus() -> tuple[list[tuple[list[str], bool]], list[tuple[list[str], bool, str]]]:
    """规格 v2 的语料：返回 (语料, 规格没有明说的取舍)。语料每条 (argv, 期望是否固定)，期望值由构造规则按规格手工决定，不调用被测函数；
    “规格没有明说”的条目同时进语料（采样器侧取保守判定）和第二个列表（带一句取舍说明），方便登记侧同步时逐条裁决。"""
    corpus: list[tuple[list[str], bool]] = []
    unspecified: list[tuple[list[str], bool, str]] = []

    def fixed(*argv: str, note: str | None = None) -> None:
        corpus.append((list(argv), True))
        if note:
            unspecified.append((list(argv), True, note))

    def unfixed(*argv: str, note: str | None = None) -> None:
        corpus.append((list(argv), False))
        if note:
            unspecified.append((list(argv), False, note))

    # ---- 1. 规格 §5 点名的边界，逐条原样放进来 ----
    unfixed(GIT, "merge-base", A, B, "--", "HEAD")
    unfixed(GIT, "merge-base", "--all", A, B, "--", "main")
    unfixed(GIT, "cat-file", "-p", A, "--", "HEAD")
    unfixed(GIT, "rev-parse", A, "--", "HEAD")
    unfixed(GIT, "show", "-s", "--format=%D", A)
    fixed(GIT, "show", "-s", "--format=%H%n%an", A)
    unfixed(GIT, "show", "--pretty=format:%h", A)
    unfixed(GIT, "log", "--oneline", A)
    unfixed(GIT, "log", "-n", "²", A)
    fixed(GIT, "show", A, "--", "HEAD")
    fixed(GIT, "ls-tree", A, "--", "HEAD")
    fixed(GIT, "show", f"{A}:HEAD")
    unfixed(GIT, "show", A + "0")  # 41 位
    unfixed(GIT, "show", A[:-1])  # 39 位
    unfixed(GIT, "show", A.upper())  # 大写
    unfixed(GIT, "show", A + "^")
    unfixed(GIT, "show", A + "~1")
    unfixed(GIT, "show", "HEAD@{1}")
    unfixed(GIT, "show", "@{u}")
    unfixed(GIT, "show", "abcdef0")  # 缩写
    unfixed(GIT, "show", "refs/heads/x")
    unfixed(GIT, "log", "--all", A)
    unfixed(GIT, "rev-list", "--all")
    unfixed(GIT, "rev-list", "--stdin")
    unfixed(GIT, "cat-file", "--batch")
    unfixed(GIT, "-C", "/tmp/x", "show", A)
    unfixed(GIT, "-c", "user.name=x", "show", A)
    unfixed(GIT, "--git-dir=/tmp/x.git", "show", A)
    unfixed(GIT, "--work-tree=/tmp/x", "show", A)
    unfixed(GIT, "-p", "show", A)

    # ---- 2. 最典型的固定用法（八个子命令各自的常见写法）----
    for argv in (
        ("show", A), ("show", "-s", A), ("show", "--stat", A), ("show", f"{A}:docs/egress/a.json"), ("show", f"{A}:path with space.txt", f"{B}:b.txt"),
        ("show", f"{A}^{{commit}}"), ("show", f"{A}^{{tree}}"), ("show", f"{A}:refs/heads/x"), ("show", A, B),
        ("cat-file", "-t", A), ("cat-file", "-e", f"{A}^{{commit}}"), ("cat-file", "blob", A), ("cat-file", "tree", A), ("cat-file", "commit", A), ("cat-file", "tag", A),
        ("cat-file", "-p", f"{A}:a/b.txt"), ("cat-file", "-s", A),
        ("rev-parse", "--verify", f"{A}^{{commit}}"), ("rev-parse", "--verify", "-q", A), ("rev-parse", "-q", "--verify", A), ("rev-parse", "--quiet", "--verify", A),
        ("rev-parse", A), ("rev-parse", A, B), ("rev-parse", f"{A}^{{tree}}"), ("rev-parse", f"{A}:docs/a.json"),
        ("merge-base", A, B), ("merge-base", "--is-ancestor", A, B), ("merge-base", "--all", A, B), ("merge-base", A, B, C), ("merge-base", "--is-ancestor", f"{A}^{{commit}}", B),
        ("ls-tree", A), ("ls-tree", "-r", A), ("ls-tree", "--name-only", A, ".github/workflows/"), ("ls-tree", "-r", f"{A}^{{tree}}"), ("ls-tree", A, "docs/"),
        ("ls-tree", A, "HEAD"), ("ls-tree", A, "--", "origin", "main"), ("ls-tree", f"{A}:docs"),
        ("diff-tree", A), ("diff-tree", A, B), ("diff-tree", "-r", A, B), ("diff-tree", "-r", "--name-only", A, B), ("diff-tree", "--no-commit-id", "--name-only", "-r", A),
        ("diff-tree", "-r", A, B, "--", "HEAD"), ("diff-tree", "--root", "-r", A),
        ("log", A), ("log", "-1", A), ("log", "-n", "3", A), ("log", "-3", A), ("log", "--max-count=2", A), ("log", f"{A}..{B}"), ("log", f"{A}...{B}"),
        ("log", A, f"^{B}"), ("log", f"^{B}", A), ("log", f"{A}^{{commit}}..{B}^{{commit}}"), ("log", A, "--", "backend/"), ("log", A, "--", "HEAD", "refs/x"),
        ("log", "--format=%B", "-1", A), ("log", "--parents", f"{A}..{B}"),
        ("rev-list", A), ("rev-list", "--count", A), ("rev-list", "--count", B, f"^{A}"), ("rev-list", f"{A}..{B}"), ("rev-list", "--parents", "-n", "1", A),
        ("rev-list", "--objects", A), ("rev-list", A, "--not", B), ("rev-list", "--max-count=5", A), ("rev-list", A, "--", "HEAD"),
    ):
        fixed(GIT, *argv)
    # 全局选项：--no-pager、--no-optional-locks 可以出现在子命令之前（任意顺序、可重复）。
    for prefix in ((), ("--no-pager",), ("--no-optional-locks",), ("--no-pager", "--no-optional-locks"), ("--no-optional-locks", "--no-pager"), ("--no-pager", "--no-pager")):
        fixed(GIT, *prefix, "show", A)
        fixed(GIT, *prefix, "log", "-1", A)
    fixed("/usr/bin/git", "show", A)
    fixed("/usr/lib/git-core/git", "show", A)
    fixed("/opt/tools/git", "--no-pager", "show", "-s", A)

    # ---- 3. 修订的写法矩阵：同一个槽位放各种写法 ----
    all_mode_templates = (
        lambda rev: ("show", rev), lambda rev: ("cat-file", "-p", rev), lambda rev: ("rev-parse", rev), lambda rev: ("rev-parse", "--verify", rev),
        lambda rev: ("merge-base", rev, B), lambda rev: ("merge-base", B, rev), lambda rev: ("diff-tree", "-r", rev), lambda rev: ("diff-tree", "-r", rev, B),
        lambda rev: ("ls-tree", rev),
    )
    range_mode_templates = (lambda rev: ("log", rev), lambda rev: ("log", "-1", rev), lambda rev: ("rev-list", rev), lambda rev: ("rev-list", "--count", rev))
    valid_everywhere = (A, f"{A}^{{commit}}")
    valid_only_in_all_mode = (f"{A}^{{tree}}", f"{A}:docs/a.json", f"{A}:HEAD", f"{A}:refs/heads/x", f"{A}:a b.txt")
    invalid_everywhere = (
        "HEAD", "HEAD~1", "HEAD^", "HEAD^{commit}", "HEAD@{1}", "@", "@{u}", "main", "origin/main", "refs/heads/x", "refs/tags/v1", "v1.0.1", "abcdef0", "abcdef0123456789",
        A[:39], A + "0", A.upper(), A[:20] + A[20:].upper(), A + "~1", A + "^", A + "^{}", A + "^0", A + "^2", A + "@{1}", A + "x", "FETCH_HEAD", "ORIG_HEAD", "MERGE_HEAD",
        "stash@{0}", ":/fix", ":0:docs/a.json", A + "^{/foo}", A + "^{blob}", A + "^{tag}", "", " " + A, A + " ", "-" + A, "g" * 40,
    )
    for template in all_mode_templates:
        for rev in valid_everywhere + valid_only_in_all_mode:
            fixed(GIT, *template(rev))
        for rev in invalid_everywhere:
            unfixed(GIT, *template(rev))
    for template in range_mode_templates:
        for rev in valid_everywhere:
            fixed(GIT, *template(rev))
        for rev in invalid_everywhere + valid_only_in_all_mode:  # log／rev-list 的范围参数不接受 :路径 与 ^{tree}
            unfixed(GIT, *template(rev))
    # 范围写法（log／rev-list）：两端都要是完整提交号。
    for command in (("log",), ("rev-list",), ("rev-list", "--count")):
        for rev in (f"{A}..{B}", f"{A}...{B}", f"{A}^{{commit}}..{B}", f"{A}..{B}^{{commit}}", f"^{B}"):
            if rev.startswith("^"):
                unfixed(GIT, *command, rev)  # 只有排除的修订，没有正向修订
                fixed(GIT, *command, A, rev)
                fixed(GIT, *command, rev, A)
            else:
                fixed(GIT, *command, rev)
        for rev in (f"{A}..HEAD", f"HEAD..{A}", f"{A}..", f"..{A}", f"{A}..{B}..{C}", f"{A}....{B}", f"{A}..{B[:39]}", f"{A.upper()}..{B}", f"{A}..main", "^HEAD", f"^{A[:39]}",
                    f"{A}...HEAD", f"{A}:docs..{B}"):
            unfixed(GIT, *command, rev)
            unfixed(GIT, *command, A, rev)  # 有一个固定的正向修订也救不了不固定的那一个

    # ---- 4. 选项：每个子命令放行的选项各一条固定，清单之外的选项各一条不固定 ----
    tails = {"show": (A,), "cat-file": (A,), "rev-parse": (A,), "merge-base": (A, B), "ls-tree": (A,), "diff-tree": (A, B), "log": (A,), "rev-list": (A,)}
    for name, (exact, _prefixes, _mode) in GIT_SPEC_V2_SUBCOMMANDS.items():
        for option in sorted(exact):
            fixed(GIT, name, option, *tails[name])
            fixed(GIT, name, *tails[name], option)  # 选项放在修订之后
    outside_options = ("--all", "--stdin", "--batch", "--batch-check", "--batch-all-objects", "--follow-symlinks", "--since=1.day.ago", "--until=now", "--author=x", "--grep=x",
                       "--decorate", "--source", "--abbrev-commit", "--date=iso", "--relative-date", "--walk-reflogs", "-g", "--branches", "--tags", "--remotes", "--glob=refs/*",
                       "--exclude=x", "--ext-diff", "--textconv", "--output=/tmp/x", "-u", "--patch", "--no-abbrev", "--use-mailmap", "--mailmap", "--show-signature",
                       "--notes", "--no-notes", "--show-toplevel", "--git-dir", "--abbrev-ref", "--symbolic", "--oneline", "-", "--format", "--pretty", "--max-count", "--abbrev")
    for name in tails:
        for option in outside_options:
            if option in GIT_SPEC_V2_SUBCOMMANDS[name][0]:
                continue
            unfixed(GIT, name, option, *tails[name])
    unfixed(GIT, "show", "-p", A)  # -p 不在 show 的清单里（cat-file -p 才在）
    unfixed(GIT, "log", "-p", A)
    unfixed(GIT, "ls-tree", "--abbrev", A)
    unfixed(GIT, "rev-parse", "--abbrev-ref", A)
    unfixed(GIT, "rev-list", "--objects", "--stdin", "--not", "--all")

    # ---- 5. -- 的含义（规格 §3）----
    for name in ("merge-base", "cat-file", "rev-parse"):
        for argv in {
            "merge-base": (("merge-base", A, B, "--", "HEAD"), ("merge-base", "--all", A, B, "--", "main"), ("merge-base", "--", A, B), ("merge-base", A, "--", B),
                           ("merge-base", A, B, "--"), ("merge-base", "--is-ancestor", A, B, "--", "x"), ("merge-base", A, B, "--", A)),
            "cat-file": (("cat-file", "-p", A, "--", "HEAD"), ("cat-file", "--", A), ("cat-file", "-p", "--", A), ("cat-file", "-t", A, "--"), ("cat-file", "blob", A, "--", A)),
            "rev-parse": (("rev-parse", A, "--", "HEAD"), ("rev-parse", "--verify", A, "--"), ("rev-parse", "--", A), ("rev-parse", A, B, "--", C), ("rev-parse", A, "--", A)),
        }[name]:
            unfixed(GIT, *argv)
    for argv in (("show", A, "--"), ("show", A, "--", "HEAD"), ("show", A, "--", "docs/a.json", "docs/b.json"), ("show", A, "--", "--"), ("show", A, B, "--", "x"),
                 ("log", A, "--", "HEAD", "refs/x"), ("log", A, "--", "backend/"), ("log", A, "--", "--", "x"), ("rev-list", A, "--", "HEAD"), ("rev-list", "--count", A, "--", "x"),
                 ("ls-tree", A, "--", "HEAD"), ("ls-tree", A, "--", "origin", "main"), ("ls-tree", "-r", A, "--", "docs/"), ("diff-tree", "-r", A, B, "--", "HEAD"),
                 ("diff-tree", A, "--", "main")):
        fixed(GIT, *argv)
    for argv in (("show", "--", A), ("show", "--"), ("log", "--", A), ("log", "-1", "--", A), ("rev-list", "--", A), ("ls-tree", "--", A), ("ls-tree", "-r", "--", A),
                 ("diff-tree", "-r", "--", A, B), ("diff-tree", "--", A)):
        unfixed(GIT, *argv)  # -- 之前没有修订：缺省落到 HEAD

    # ---- 6. --format=／--pretty= 的取值（规格 §2）----
    for subcommand in ("show", "log"):
        # 精确的 raw。
        fixed(GIT, subcommand, "--pretty=raw", A)
        fixed(GIT, subcommand, "--format=raw", A)
        for value in ("Raw", "RAW", "raw ", " raw", "rawx", "xraw", "raw:"):
            unfixed(GIT, subcommand, f"--pretty={value}", A)
            unfixed(GIT, subcommand, f"--format={value}", A)
        # 命名格式与配置别名、空串。--pretty= 后面没有 format:／tformat: 前缀的，规格的写法清单里本来就没有，不固定；--format= 后面的值规格说“不带前缀，等价 tformat”，
        # 字面读法把 oneline 这类不含 % 的普通字符也当成自定义串——但在 git 里它是命名格式（medium 的 Date 行受 log.date 影响，reference 用缩写提交号），所以保守判不固定。
        for value in ("oneline", "short", "medium", "full", "fuller", "reference", "email", "mboxrd", "myalias", "onelin", "Oneline", "format", "tformat", ""):
            unfixed(GIT, subcommand, f"--pretty={value}", A)
            unfixed(GIT, subcommand, f"--format={value}", A,
                    note="--format= 后面是不含 % 又无前缀的值（命名格式／配置别名／空串）：规格字面读法像‘普通字符的自定义串’，git 里却是命名格式，采样器保守判不固定")
        # 允许的占位符：单独一个，和夹在普通字符里。
        for placeholder, context in GIT_SPEC_V2_ALLOWED_PLACEHOLDERS.items():
            fixed(GIT, subcommand, f"--format={placeholder}", A)
            fixed(GIT, subcommand, f"--format={context}", A)
            fixed(GIT, subcommand, f"--pretty=format:{context}", A)
            fixed(GIT, subcommand, f"--pretty=tformat:{context}", A)
        # 禁止的占位符：单独、夹在允许的占位符中间、三种写法。
        for placeholder, context in GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS.items():
            unfixed(GIT, subcommand, f"--format={placeholder}", A)
            unfixed(GIT, subcommand, f"--format=%H {placeholder} %an", A)
            unfixed(GIT, subcommand, f"--pretty=format:{context}", A)
            unfixed(GIT, subcommand, f"--pretty=tformat:{context}", A)
            fixed(GIT, subcommand, f"--format=%{placeholder}", A)  # 前面再加一个 %：转义成字面文字，只剩普通字符
    for sequence in GIT_SPEC_V2_OTHER_UNLISTED_SEQUENCES:
        unfixed(GIT, "show", f"--format={sequence}", A)
    # 三种前缀写法的组合（--format= 不带前缀等价 tformat；--pretty= 的 format:／tformat: 前缀；无前缀含 % 的 --pretty= 值 git 当隐含 tformat）。
    for value, expected in (("%H", True), ("format:%H", True), ("tformat:%H", True), ("format:", True), ("tformat:", True), ("format:plain", True), ("tformat:plain text", True),
                            ("format:raw", True), ("format:%D", False), ("tformat:%d", False), ("format:%H%", False), ("plain %H text", True), ("plain %D text", False)):
        for option in ("--format=", "--pretty="):
            implicit = option == "--pretty=" and expected and not value.startswith(("format:", "tformat:"))
            (fixed if expected else unfixed)(
                GIT, "show", f"{option}{value}", A,
                note="--pretty= 后面无前缀但含 %：规格的写法清单里没有这一项，git 把它当隐含的 tformat（与 --format= 同样解析），采样器判固定" if implicit else None)
    # 普通字符：空格、字母、标点、非 ASCII，都不含 %。
    for text in ("commit: %H", "hello %H world", "refs/heads/%H", "HEAD:%H", "%H|%an|%ae|%s", "[%an] %s (%ct)", "提交 %H，作者 %an", "a\tb %H", "100%% %H", "%H%%", "%%%H"):
        fixed(GIT, "log", f"--format={text}", A)
        fixed(GIT, "show", f"--pretty=format:{text}", A)
    # --format／--pretty 不带 = 的写法、别的子命令的 --format=／--pretty=（不在它们的清单里）。
    for argv in (("show", "--format", "%H", A), ("show", "--pretty", A), ("log", "--format", "%H", A), ("log", "--pretty", "raw", A)):
        unfixed(GIT, *argv)
    for name in ("rev-list", "cat-file", "ls-tree", "diff-tree", "merge-base", "rev-parse"):
        for option in ("--format=%H", "--pretty=raw", "--format=raw", "--pretty=format:%H"):
            unfixed(GIT, name, option, *tails[name])

    # ---- 7. 数字（规格 §4）：只接受 ASCII 数字 ----
    for argv in (("log", "-n", "3", A), ("log", "-n", "0", A), ("log", "-n", "10", A), ("log", "-3", A), ("log", "-0", A), ("log", "--max-count=3", A), ("log", "--max-count=0", A),
                 ("rev-list", "-n", "1", A), ("rev-list", "-5", A), ("rev-list", "--max-count=2", A), ("ls-tree", "--abbrev=7", A), ("ls-tree", "--abbrev=40", A),
                 ("log", "-n", "007", A)):
        fixed(GIT, *argv)
    for argv in (("log", "-n", "²", A), ("log", "-n", "٣", A), ("log", "-n", "１２", A), ("log", "-n", "-1", A), ("log", "-n", "", A), ("log", "-n", A), ("log", "-n"), ("log", A, "-n"),
                 ("log", "-²", A), ("log", "-٣", A), ("log", "-３", A), ("log", "-3x", A), ("log", "-", A), ("log", "--max-count=²", A), ("log", "--max-count=٣", A),
                 ("log", "--max-count=", A), ("log", "--max-count=-1", A), ("log", "--max-count=3x", A), ("log", "--max-count=3.5", A), ("log", "--max-count= 3", A),
                 ("rev-list", "-n", "²", A), ("rev-list", "-n", "x", A), ("rev-list", "-²", A), ("rev-list", "--max-count=²", A), ("rev-list", "--max-count=", A),
                 ("ls-tree", "--abbrev=²", A), ("ls-tree", "--abbrev=٣", A), ("ls-tree", "--abbrev=", A), ("ls-tree", "--abbrev=abc", A), ("ls-tree", "--abbrev=-1", A),
                 ("show", "-n", "3", A), ("show", "-3", A), ("show", "--max-count=3", A), ("cat-file", "-n", "3", A), ("merge-base", "-n", "3", A, B), ("diff-tree", "-3", A, B)):
        unfixed(GIT, *argv)

    # ---- 8. 命令行本身：不是 git、参数不可见、没有子命令、子命令不在清单里、没给修订、全局选项 ----
    unfixed("python3", "x.py")
    unfixed("gitx", "show", A)
    unfixed("/usr/bin/gitx", "show", A)
    unfixed("git-show", A)
    unfixed("GIT", "show", A)
    unfixed("/usr/bin/env", "git", "show", A)
    unfixed()
    unfixed(GIT, ds.GIT_ARGV_HIDDEN)
    unfixed(GIT, "show", A, ds.GIT_ARGV_HIDDEN)
    unfixed(GIT)
    unfixed(GIT, "--no-pager")
    unfixed(GIT, "--no-pager", "--no-optional-locks")
    for name in ("tag", "branch", "status", "diff", "grep", "describe", "for-each-ref", "ls-remote", "remote", "config", "stash", "checkout", "fetch", "update-ref", "commit", "init",
                 "symbolic-ref", "worktree", "ls-files", "blame", "shortlog", "name-rev", "archive", "bundle", "notes", "reflog", "show-ref", "show-branch", "cherry", "cherry-pick",
                 "format-patch", "verify-commit", "whatchanged", "count-objects", "clone", "add", "reset", "merge", "rebase", "log-x", "Show", "SHOW", "rev_list"):
        unfixed(GIT, name, A)
    for argv in (("-C", "/tmp/x", "show", A), ("-C/tmp/x", "show", A), ("-c", "user.name=x", "show", A), ("-cuser.name=x", "show", A), ("--git-dir=/tmp/x.git", "show", A),
                 ("--git-dir", "/tmp/x.git", "show", A), ("--work-tree=/tmp/x", "show", A), ("--work-tree", "/tmp/x", "show", A), ("-p", "show", A), ("--paginate", "show", A),
                 ("-P", "show", A), ("--no-pager", "-C", "/tmp/x", "show", A), ("--no-optional-locks", "-p", "show", A), ("--exec-path=/x", "show", A),
                 ("--no-replace-objects", "show", A), ("--literal-pathspecs", "show", A), ("--namespace=x", "show", A), ("--bare", "show", A), ("--help", "show", A),
                 ("--version",), ("-h",), ("--shallow-file", "", "pack-objects"), ("--no-pager=1", "show", A), ("--no-pagers", "show", A), ("--", "show", A)):
        unfixed(GIT, *argv)
    for argv in (("show",), ("log", "-1"), ("log", "-n", "3"), ("rev-list", "--count"), ("ls-tree", "-r"), ("rev-parse", "--verify"), ("cat-file", "-t"), ("cat-file", "blob"),
                 ("merge-base", A), ("merge-base", "--is-ancestor", A), ("diff-tree", "-r"), ("rev-parse",), ("log", "--format=%B"), ("show", "-s"), ("log", f"^{A}"),
                 ("rev-list", f"^{A}", f"^{B}"), ("rev-parse", "--verify", A, B), ("cat-file", "blob", A, B), ("cat-file", "-p", A, B)):
        unfixed(GIT, *argv)
    return corpus, unspecified


GIT_SPEC_V2_CORPUS, GIT_SPEC_V2_UNSPECIFIED = _build_corpus()


class GitSpecV2Tables(unittest.TestCase):
    """常量表：采样器的实现与规格 v2 的字面量逐项相等。"""

    def test_the_subcommand_table_equals_the_spec(self) -> None:
        self.assertEqual(ds._GIT_FIXED_SUBCOMMANDS, GIT_SPEC_V2_SUBCOMMANDS)
        self.assertEqual(sorted(ds._GIT_FIXED_SUBCOMMANDS), sorted(["show", "cat-file", "rev-parse", "merge-base", "ls-tree", "diff-tree", "log", "rev-list"]))
        for name, (exact, prefixes, mode) in ds._GIT_FIXED_SUBCOMMANDS.items():
            self.assertIsInstance(exact, frozenset, name)
            self.assertIsInstance(prefixes, tuple, name)
            self.assertNotIn("--oneline", exact, f"{name}：规格 §1 删去 --oneline")
        self.assertEqual(ds._GIT_FIXED_SUBCOMMANDS["show"][1], ("--format=", "--pretty="))
        self.assertEqual(ds._GIT_FIXED_SUBCOMMANDS["log"][1], ("--format=", "--pretty=", "--max-count="))

    def test_plain_globals_dashdash_subcommands_and_numeric_prefixes_equal_the_spec(self) -> None:
        self.assertEqual(ds._GIT_PLAIN_GLOBALS, GIT_SPEC_V2_PLAIN_GLOBALS)
        self.assertEqual(ds._GIT_DASHDASH_REVISION_SUBCOMMANDS, GIT_SPEC_V2_DASHDASH_REVISION_SUBCOMMANDS)
        self.assertEqual(ds._GIT_NUMERIC_PREFIXES, ("--max-count=", "--abbrev="))
        self.assertEqual(ds._GIT_OBJECT_TYPES, frozenset({"blob", "tree", "commit", "tag"}))

    def test_the_format_whitelist_equals_the_spec(self) -> None:
        self.assertEqual(ds._GIT_FORMAT_PLACEHOLDERS, GIT_SPEC_V2_FORMAT_PLACEHOLDERS)
        self.assertEqual(ds._GIT_FORMAT_HEX.pattern, GIT_SPEC_V2_FORMAT_HEX_RE)
        self.assertEqual(ds._GIT_FORMAT_NAMES, GIT_SPEC_V2_FORMAT_NAMES)
        self.assertEqual(tuple(ds._GIT_FORMAT_PREFIXES), GIT_SPEC_V2_FORMAT_PREFIXES)
        self.assertEqual(sorted(placeholder for placeholder in GIT_SPEC_V2_FORMAT_PLACEHOLDERS if len(placeholder) == 2),
                         sorted(["an", "ae", "aI", "at", "cn", "ce", "cI", "ct"]))
        for forbidden in ("h", "t", "p", "d", "D", "N", "S", "ad", "cd", "aD", "cD", "ar", "cr", "aN", "aE", "cN", "cE", "gd", "f", "e", "ai", "ci"):
            self.assertNotIn(forbidden, GIT_SPEC_V2_FORMAT_PLACEHOLDERS, forbidden)

    def test_the_spec_example_tables_are_consistent_with_the_whitelist(self) -> None:
        allowed = {key[1:] for key in GIT_SPEC_V2_ALLOWED_PLACEHOLDERS if not key.startswith("%x")}
        self.assertEqual(allowed, set(GIT_SPEC_V2_FORMAT_PLACEHOLDERS), "放行的占位符每条都有例子，例子表里没有多余的")
        self.assertEqual({key for key in GIT_SPEC_V2_ALLOWED_PLACEHOLDERS if key.startswith("%x")}, {"%x00", "%x0a", "%x0A", "%xff", "%xFF"})
        for key in GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS:
            self.assertNotIn(key[1:], GIT_SPEC_V2_FORMAT_PLACEHOLDERS, f"{key} 既禁止又放行")
        for forbidden in ("%D", "%d", "%N", "%S", "%gd", "%(describe)", "%(decorate)", "%h", "%t", "%p", "%ad", "%cd", "%aD", "%cD", "%ar", "%cr", "%aN", "%aE", "%cN", "%cE"):
            self.assertIn(forbidden, GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS, f"规格点名禁止的 {forbidden} 要有例子")


class GitSpecV2Corpus(unittest.TestCase):
    """语料：每条 argv 的判定与规格 v2 的期望一致（只比“固定／不固定”，不比原因文字）。"""

    def test_the_corpus_is_large_and_has_both_verdicts_in_quantity(self) -> None:
        self.assertGreaterEqual(len(GIT_SPEC_V2_CORPUS), 400)
        fixed = sum(1 for _argv, expected in GIT_SPEC_V2_CORPUS if expected)
        self.assertGreaterEqual(fixed, 150)
        self.assertGreaterEqual(len(GIT_SPEC_V2_CORPUS) - fixed, 250)
        self.assertTrue(all(isinstance(argv, list) and all(isinstance(item, str) for item in argv) and isinstance(expected, bool) for argv, expected in GIT_SPEC_V2_CORPUS))
        seen: dict[tuple[str, ...], bool] = {}
        for argv, expected in GIT_SPEC_V2_CORPUS:
            self.assertEqual(seen.setdefault(tuple(argv), expected), expected, f"同一条 argv 两个期望：{argv}")

    def test_every_boundary_the_spec_names_is_in_the_corpus_with_the_expected_verdict(self) -> None:
        index = {tuple(argv): expected for argv, expected in GIT_SPEC_V2_CORPUS}
        named = {
            (GIT, "merge-base", A, B, "--", "HEAD"): False, (GIT, "merge-base", "--all", A, B, "--", "main"): False, (GIT, "cat-file", "-p", A, "--", "HEAD"): False,
            (GIT, "rev-parse", A, "--", "HEAD"): False, (GIT, "show", "-s", "--format=%D", A): False, (GIT, "show", "-s", "--format=%H%n%an", A): True,
            (GIT, "show", "--pretty=format:%h", A): False, (GIT, "log", "--oneline", A): False, (GIT, "log", "-n", "²", A): False, (GIT, "show", A, "--", "HEAD"): True,
            (GIT, "ls-tree", A, "--", "HEAD"): True, (GIT, "show", f"{A}:HEAD"): True, (GIT, "show", A + "0"): False, (GIT, "show", A[:-1]): False,
            (GIT, "show", A.upper()): False, (GIT, "show", A + "^"): False, (GIT, "show", A + "~1"): False, (GIT, "show", "HEAD@{1}"): False,
            (GIT, "show", "abcdef0"): False, (GIT, "show", "refs/heads/x"): False, (GIT, "log", "--all", A): False, (GIT, "rev-list", "--stdin"): False,
            (GIT, "cat-file", "--batch"): False, (GIT, "-C", "/tmp/x", "show", A): False, (GIT, "-c", "user.name=x", "show", A): False,
            (GIT, "--git-dir=/tmp/x.git", "show", A): False, (GIT, "--work-tree=/tmp/x", "show", A): False, (GIT, "-p", "show", A): False,
        }
        for argv, expected in named.items():
            self.assertIn(argv, index, f"语料里缺规格点名的边界：{argv}")
            self.assertEqual(index[argv], expected, argv)

    def test_the_decisions_the_spec_does_not_state_are_listed_and_part_of_the_corpus(self) -> None:
        index = {tuple(argv): expected for argv, expected in GIT_SPEC_V2_CORPUS}
        self.assertGreaterEqual(len(GIT_SPEC_V2_UNSPECIFIED), 20)
        kinds = set()
        for argv, expected, note in GIT_SPEC_V2_UNSPECIFIED:
            self.assertEqual(index[tuple(argv)], expected, argv)
            self.assertTrue(note)
            kinds.add(note.split("：")[0])
        self.assertEqual(len(kinds), 2, "目前有两类取舍：--format= 后的命名格式／别名／空串，和 --pretty= 后无前缀但含 % 的值")

    def test_the_sampler_judges_every_corpus_entry_as_the_spec_expects(self) -> None:
        wrong = []
        for argv, expected in GIT_SPEC_V2_CORPUS:
            problem = ds.git_usage_problem(argv)
            if (problem is None) != expected:
                wrong.append((argv, "期望固定" if expected else "期望不固定", problem))
        self.assertEqual(wrong, [], f"{len(wrong)} 条与规格期望不一致")

    def test_every_unfixed_verdict_comes_with_a_reason_and_no_fixed_one_does(self) -> None:
        for argv, expected in GIT_SPEC_V2_CORPUS:
            problem = ds.git_usage_problem(argv)
            if expected:
                self.assertIsNone(problem, argv)
            else:
                self.assertIsInstance(problem, str, argv)
                self.assertTrue(problem, argv)


class GitSpecV2Placeholders(unittest.TestCase):
    """规格 §2：每条放行的占位符和每条禁止的占位符各有例子，且各有反方向的对照。"""

    def verdict(self, value: str, option: str = "--format=") -> bool:
        return ds.git_usage_problem([GIT, "show", f"{option}{value}", A]) is None

    def test_every_allowed_placeholder_is_accepted_alone_and_inside_plain_text_but_not_with_a_magic_modifier(self) -> None:
        for placeholder, context in GIT_SPEC_V2_ALLOWED_PLACEHOLDERS.items():
            with self.subTest(placeholder=placeholder):
                self.assertTrue(self.verdict(placeholder), "单独一个")
                self.assertTrue(self.verdict(context), "夹在普通字符里")
                self.assertTrue(self.verdict(context, "--pretty=format:") and self.verdict(context, "--pretty=tformat:"))
                self.assertIsNone(ds.git_format_problem(placeholder))
                self.assertIsNone(ds.git_format_problem(context))
                if placeholder != "%%":
                    self.assertFalse(self.verdict("%+" + placeholder[1:]), "对照：加上魔法修饰符 + 就不在表里了")
                    self.assertFalse(self.verdict("%-" + placeholder[1:]))
                    self.assertFalse(self.verdict("% " + placeholder[1:]))
        self.assertFalse(self.verdict("%"), "%% 的对照：落单的 %")

    def test_every_forbidden_placeholder_is_refused_but_its_escaped_form_is_plain_text(self) -> None:
        for placeholder, context in GIT_SPEC_V2_FORBIDDEN_PLACEHOLDERS.items():
            with self.subTest(placeholder=placeholder):
                self.assertFalse(self.verdict(placeholder), "单独一个")
                self.assertFalse(self.verdict(f"%H {placeholder} %an"), "夹在放行的占位符中间")
                self.assertFalse(self.verdict(context, "--pretty=format:") or self.verdict(context, "--pretty=tformat:"))
                self.assertIsNotNone(ds.git_format_problem(placeholder))
                self.assertTrue(self.verdict("%" + placeholder), "对照：前面再加一个 % 就成了字面文字（%%D），只剩普通字符")
                self.assertIn("不在白名单内", ds.git_format_problem(placeholder))

    def test_unlisted_sequences_are_refused_whatever_they_are(self) -> None:
        for sequence in GIT_SPEC_V2_OTHER_UNLISTED_SEQUENCES:
            with self.subTest(sequence=sequence):
                self.assertFalse(self.verdict(sequence))

    def test_hex_escapes_need_exactly_two_hex_digits(self) -> None:
        for value, expected in (("%x00", True), ("%x0a", True), ("%x0A", True), ("%xfF", True), ("%x00%x0a", True), ("%x000", True), ("a%x41b", True),
                                ("%x", False), ("%x0", False), ("%x0g", False), ("%xg0", False), ("%x-1", False), ("%x 0", False), ("%X00", False), ("%x%x00", False)):
            with self.subTest(value=value):
                self.assertEqual(self.verdict(value), expected)

    def test_raw_and_the_three_prefix_forms(self) -> None:
        for option in ("--pretty=", "--format="):
            self.assertTrue(self.verdict("raw", option))
            self.assertFalse(self.verdict("oneline", option))
            self.assertFalse(self.verdict("", option))
        self.assertTrue(self.verdict("format:%H", "--pretty=") and self.verdict("tformat:%H", "--pretty="))
        self.assertTrue(self.verdict("%H", "--format="), "--format= 的值不带前缀，等价 tformat")
        self.assertFalse(self.verdict("format:%D", "--pretty="))
        self.assertIn("不是精确的 raw", ds.git_format_problem("oneline"))
        self.assertIsNone(ds.git_format_problem("raw"))

    def test_digits_are_ascii_only(self) -> None:
        for text, expected in (("3", True), ("0", True), ("12345", True), ("007", True), ("", False), ("²", False), ("٣", False), ("１２", False), ("3²", False), ("-3", False),
                               ("3.5", False), (" 3", False), ("3 ", False), ("x", False)):
            self.assertEqual(ds._ascii_digits(text), expected, text)


if __name__ == "__main__":
    unittest.main()
