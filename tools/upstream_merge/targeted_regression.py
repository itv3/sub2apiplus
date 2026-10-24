"""试验区快线的定向回归清单：根据冲突文件、实际修改和调用依赖列出要跑的回归，并写明每一项的选择理由。

为什么要有这份清单：v0.2.17 试验区全量门禁 64 分 29 秒，其中采集工具线 33 分 38 秒、go 测试线 14 分 29 秒；这两条线
查出的失败全部是冻结摘要类（试验区还没生成台账，本来就会红），没有一条功能问题。快线跳过这两条整线，功能问题改由
“冲突相关的定向回归”在试验区发现，正式 U-4 全量兜底。定向回归必须说得清覆盖了什么：覆盖范围说不清就补足验证，
必要时退回全量 go test。

输入事实（全部离线，来自 Git 对象与候选树）：

* ``merge_base``：fork 与上游的合并基点；
* **冲突文件**：fork 与上游相对 merge-base 都改过的文件（含两侧自动合并成功的）；
* **实际修改**：候选树相对 fork **且**相对上游都不同的文件（合并后又改，或冲突解决后与两侧都不一样）；
* **上游单侧改动**：只有上游改过、且候选与上游逐字节相同的文件。它们不算“实际修改”，但 Go 包仍按依赖规则纳入——
  上游改了某个包而 fork 在别处调用它，语义冲突只有跑到那个包和直接导入它的包才暴露；其余类别（前端、Python、
  部署脚本）这类文件不触发定向用例，因为它们与上游逐字节相同，上游自己的 CI 已经测过。

选择规则（每一项都写 ``reason``）：

* Go（``backend`` 模块）：受影响的包＝涉及文件所在的包＋模块内直接导入这些包的包（``go list -json`` 离线求反向依赖）。
  默认标签与 unit 标签各一组 ``go test -count=1``；integration 标签依赖 Docker，不在快线内，交给 U-4／CI，清单里写明。
  ``internal/officialegress`` 等已被出站规格线（``make check-egress-spec`` 里的 ``egress-spec-go-test``）按默认标签
  完整跑过的包，不在默认标签组里重复；这份包清单从 Makefile 该目标现取，不写死。
  ``backend/go.mod`` 的依赖版本变化按“依赖该模块的包”并入；``go``／``toolchain``／``replace``／``exclude``
  指令变化、go.sum 单独变化、Go 文件找不到所属包，一律退回全量（``fallback_full_go``）。
  估算耗时超过预算（见 ``TARGETED_GO_BUDGET_SECONDS``）也退回全量，因为定向已不比全量省。
* 前端：涉及 ``frontend/src`` 下的文件，按 import 依赖图选出（传递）引用它们的用例，只跑这些用例（``vitest related``
  在本仓库崩溃，不可用）；前端检查线本身已含 lint、typecheck 与关键用例。
* 部署脚本：``deploy/`` 下的改动执行 CI ``shell`` 作业里的那批命令（从 ``backend-ci.yml`` 现取）。
* ``tools/`` 下 Python：有测试目录的工具包（``upstream_merge``、``official_client_control``）跑它们的 unittest；
  被 Makefile 直接调用的脚本由出站规格线覆盖；采集工具树要整条采集线（约 33 分钟）才测得全，快线不含，列入 ``unresolved``。
* 文档、README、资源、CI 配置等不需要测试的文件列入 ``no_test_needed`` 并写明理由；归不了类的列入 ``unresolved``，
  快线输出会提示并要求补足验证（``backend/`` 下的 ``unresolved`` 直接触发 ``fallback_full_go``）。

清单本身写 schema、输入提交号、生成时间与自摘要（``identity_sha256``）；快线状态文件再绑定整个文件的 sha256，
快线执行前核对清单的候选提交与当前 HEAD 一致。
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    bind_identity,
    expect_exact_fields,
    expect_object,
    file_binding,
    load_json,
    validate_identity,
    write_json_once,
)
from .errors import UpstreamMergeError
from .gitops import assert_clean, assert_git_repository, assert_private_path, merge_base, rev_parse, run_git
from .toolchain import CI_WORKFLOW_PATH, parse_ci_workflow

TARGETED_SCHEMA = "official-egress-upstream-targeted-regression/v1"
GO_MODULE_DIR = "backend"
GO_TAG_SETS = (("default", ""), ("unit", "unit"))
GO_LIST_FIELDS = (
    "ImportPath,Dir,GoFiles,CgoFiles,TestGoFiles,XTestGoFiles,Imports,TestImports,XTestImports,"
    "EmbedFiles,TestEmbedFiles,XTestEmbedFiles"
)

# 估算耗时（秒）：go test 的各个包在同一次 go test 里并行执行，墙钟约等于最重的包加上其余包的一小部分，两组标签串行相加。
# 表是 (默认标签, unit 标签) 的实测值：officialegress、cmd/egressscan 取自 v0.2.17 试验区全量日志（默认 409／15、unit 417），
# 其余取自 v0.2.18 演示清单在本机的定向实测（15 个包：默认组墙钟 272 秒、unit 组 348 秒，service 257／317、handler 41／42、
# repository 8／10、routes 12／11）。表里没有的包按 GO_DEFAULT_PACKAGE_SECONDS 估。
GO_PACKAGE_SECONDS: dict[str, tuple[int, int]] = {
    "internal/officialegress": (410, 420),
    "internal/service": (260, 320),
    "internal/handler": (42, 43),
    "cmd/egressscan": (20, 20),
    "internal/server/routes": (12, 12),
    "internal/middleware": (11, 11),
    "internal/repository": (9, 10),
}
GO_DEFAULT_PACKAGE_SECONDS = 3
# 其余包在最重的包运行期间并行执行，只按这个比例计入墙钟（用 v0.2.18 实测核对：默认组估 279 对实测 272，unit 组估 340 对实测 348）。
GO_PARALLEL_SHARE = 0.25
# 定向 go test（两组标签合计）的预算。含 service 包的定向组实测约 10 分钟，所以预算放到 12 分钟：
# 预算再低，只要冲突涉及 service（几乎每次合并都是）就永远退回全量，定向就成了摆设；
# 超过 12 分钟说明受影响面已接近全量（v0.2.17 试验区默认＋unit 全量 14.5 分钟），定向不再比全量省。
TARGETED_GO_BUDGET_SECONDS = 720
# 另设一个包数上限，防止估算表陈旧时静默放大范围。
MAX_TARGETED_GO_PACKAGES = 60

INTEGRATION_NOTE = (
    "integration 标签的 go test 依赖本机 Docker，不在快线内；由正式 U-4（本机有 Docker 时执行，否则 awaiting_ci）"
    "与同一候选提交的 CI 证据覆盖"
)
UNIT_TEST_COMMAND = ("python3", "-m", "unittest", "discover", "-s", "{tests}", "-p", "test_*.py")
# 有测试目录、快线可直接跑 unittest 的工具包（相对仓库根）。
# 值是 (测试目录, 出站规格线里对应的 make 子检查名)：该子检查在 Makefile 的 EGRESS_SPEC_CI_CHECKS 里时，这些测试已被出站规格线完整执行。
PYTHON_TEST_PACKAGES = {
    "tools/upstream_merge": ("tools/upstream_merge/tests", "test-upstream-merge-tools"),
    "tools/official_client_control": ("tools/official_client_control/tests", "test-official-client-control"),
}
CAPTURE_TOOLS_PREFIX = "tools/official_client_capture/"
DEPLOY_PREFIX = "deploy/"

# 可能被 Go 代码按路径读取的数据文件类型；Dockerfile、Makefile 这类名字太泛，不做引用查找。
DATA_FILE_SUFFIXES = (".json", ".yaml", ".yml", ".txt", ".csv", ".tmpl", ".toml", ".xml", ".html", ".sql", ".golden", ".pem", ".bin", ".gz", ".zip")
STATIC_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2")
NO_TEST_DOC_SUFFIXES = (".md", ".txt", *STATIC_ASSET_SUFFIXES)
FRONTEND_TEST_RUNNER_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".css", ".scss", ".json")
FRONTEND_CONFIG_NAMES = (
    "package.json",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
    "vitest.config.ts",
    "postcss.config.js",
    "tailwind.config.js",
    "index.html",
    ".eslintrc.cjs",
    ".eslintrc.js",
    ".eslintrc.json",
    "eslint.config.js",
    "eslint.config.mjs",
)
# backend/ 下不影响 go test 的构建与配置文件，各有明确理由。
BACKEND_NO_TEST_FILES = {
    "backend/Makefile": "后端构建与测试目标定义；快线直接调用 go test，不经过它，其变化不改变被测代码",
    "backend/Dockerfile": "镜像构建文件；本机无镜像构建检查，发版流水线构建，U-4 同样不覆盖",
    "backend/.golangci.yml": "lint 配置；lint 检查线用候选树里的这份配置完整重跑",
    "backend/.golangci.yaml": "lint 配置；lint 检查线用候选树里的这份配置完整重跑",
    "backend/.gitignore": "忽略规则，不影响被测代码",
    "backend/.dockerignore": "镜像构建上下文规则，不影响 go test",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Git 事实
# ---------------------------------------------------------------------------


def _diff_names(root: Path, left: str, right: str) -> set[str]:
    """两个提交之间有差异的路径集合（含删除，不跟踪重命名：重命名按“删除＋新增”两条算）。"""

    raw = run_git(root, "diff", "--name-only", "--no-renames", "-z", left, right).stdout
    return {item for item in raw.split("\0") if item}


def _tree_files(root: Path, commit: str) -> set[str]:
    raw = run_git(root, "ls-tree", "-r", "--name-only", "-z", commit).stdout
    return {item for item in raw.split("\0") if item}


def _blob_text(root: Path, commit: str, relative: str) -> str | None:
    completed = run_git(root, "show", f"{commit}:{relative}", check=False)
    return completed.stdout if completed.returncode == 0 else None


@dataclass(frozen=True)
class GitFacts:
    """决定定向回归范围的 Git 事实。所有集合都是仓库相对路径。"""

    fork_commit: str
    upstream_commit: str
    candidate_commit: str
    merge_base: str
    fork_changed: frozenset[str]
    upstream_changed: frozenset[str]
    conflict: frozenset[str]
    actual_modified: frozenset[str]
    upstream_only: frozenset[str]
    candidate_files: frozenset[str]


def collect_git_facts(root: Path, fork: str, upstream: str) -> GitFacts:
    fork_commit = rev_parse(root, f"{fork}^{{commit}}")
    upstream_commit = rev_parse(root, f"{upstream}^{{commit}}")
    candidate = rev_parse(root, "HEAD^{commit}")
    base = merge_base(root, fork_commit, upstream_commit)
    fork_changed = _diff_names(root, base, fork_commit)
    upstream_changed = _diff_names(root, base, upstream_commit)
    differs_from_fork = _diff_names(root, fork_commit, candidate)
    differs_from_upstream = _diff_names(root, upstream_commit, candidate)
    conflict = fork_changed & upstream_changed
    actual = differs_from_fork & differs_from_upstream
    # 只有上游改过、fork 没动，且候选与上游逐字节相同。
    upstream_only = (upstream_changed - fork_changed) - differs_from_upstream
    return GitFacts(
        fork_commit=fork_commit,
        upstream_commit=upstream_commit,
        candidate_commit=candidate,
        merge_base=base,
        fork_changed=frozenset(fork_changed),
        upstream_changed=frozenset(upstream_changed),
        conflict=frozenset(conflict),
        actual_modified=frozenset(actual),
        upstream_only=frozenset(upstream_only),
        candidate_files=frozenset(_tree_files(root, candidate)),
    )


# ---------------------------------------------------------------------------
# Go 包索引（go list，离线）
# ---------------------------------------------------------------------------

GoList = Callable[[Path, str, str], "list[dict[str, Any]]"]


def _go_env() -> dict[str, str]:
    """go list 只读取本机缓存，不联网、不改 go.mod／go.sum。"""

    env = dict(os.environ)
    env.update({"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOWORK": "off"})
    return env


def run_go_list(backend: Path, tags: str, fields: str) -> list[dict[str, Any]]:
    argv = ["go", "list", "-e", f"-json={fields}"]
    if tags:
        argv.insert(2, f"-tags={tags}")
    argv.append("./...")
    completed = subprocess.run(argv, cwd=backend, capture_output=True, text=True, env=_go_env(), check=False)
    if completed.returncode != 0:
        detail = (completed.stderr.strip() or completed.stdout.strip())[-500:]
        raise UpstreamMergeError(f"go list 失败（标签 {tags or '默认'}）：{detail}")
    decoder = json.JSONDecoder()
    text, position, packages = completed.stdout, 0, []
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        item, position = decoder.raw_decode(text, position)
        packages.append(item)
    return packages


@dataclass
class GoPackage:
    import_path: str
    rel_dir: str
    # 各标签组下是否有可跑的测试文件、包本身是否存在。
    present: dict[str, bool] = field(default_factory=dict)
    has_tests: dict[str, bool] = field(default_factory=dict)
    imports: set[str] = field(default_factory=set)


@dataclass
class GoIndex:
    by_dir: dict[str, GoPackage]
    by_import: dict[str, GoPackage]
    embed_owner: dict[str, GoPackage]
    importers: dict[str, set[str]]


def build_go_index(backend: Path, go_list: GoList = run_go_list) -> GoIndex:
    """对默认、unit、integration 三组标签各调一次 go list，合并成一份包索引。

    integration 标签只用于求依赖与归属（有些包只有 integration 标签的文件），不进入快线的 go test。
    """

    backend_resolved = backend.resolve()
    by_dir: dict[str, GoPackage] = {}
    by_import: dict[str, GoPackage] = {}
    embed_owner: dict[str, GoPackage] = {}
    for label, tags in (("default", ""), ("unit", "unit"), ("integration", "integration")):
        for item in go_list(backend, tags, GO_LIST_FIELDS):
            import_path = str(item.get("ImportPath", ""))
            directory = Path(str(item.get("Dir", ""))).resolve()
            try:
                rel_dir = directory.relative_to(backend_resolved).as_posix()
            except ValueError:
                continue
            rel_dir = "" if rel_dir == "." else rel_dir
            package = by_import.get(import_path)
            if package is None:
                package = GoPackage(import_path=import_path, rel_dir=rel_dir)
                by_import[import_path] = package
                by_dir[rel_dir] = package
            files = list(item.get("GoFiles") or []) + list(item.get("CgoFiles") or [])
            tests = list(item.get("TestGoFiles") or []) + list(item.get("XTestGoFiles") or [])
            package.present[label] = bool(files or tests)
            package.has_tests[label] = bool(tests)
            for key in ("Imports", "TestImports", "XTestImports"):
                package.imports.update(str(value) for value in item.get(key) or [])
            for key in ("EmbedFiles", "TestEmbedFiles", "XTestEmbedFiles"):
                for embedded in item.get(key) or []:
                    embed_owner[posixpath.normpath(posixpath.join(rel_dir, str(embedded)))] = package
    importers: dict[str, set[str]] = defaultdict(set)
    for package in by_import.values():
        for imported in package.imports:
            if imported in by_import:
                importers[imported].add(package.import_path)
    return GoIndex(by_dir=by_dir, by_import=by_import, embed_owner=embed_owner, importers=importers)


def _nearest_package(index: GoIndex, rel_dir: str) -> GoPackage | None:
    current = rel_dir
    while True:
        if current in index.by_dir:
            return index.by_dir[current]
        if not current:
            return None
        current = posixpath.dirname(current)


def _referencing_go_packages(root: Path, commit: str, path: str, index: GoIndex) -> list[GoPackage]:
    """backend 里按文件名引用某个数据文件的 Go 源码所在的包（只读的 grep）。"""

    name = posixpath.basename(path)
    completed = run_git(root, "grep", "-l", "-F", "-e", name, commit, "--", ":(glob)backend/**/*.go", check=False)
    found: dict[str, GoPackage] = {}
    for line in completed.stdout.splitlines():
        _commit, _sep, source = line.partition(":")
        directory = posixpath.dirname(source[len("backend/") :]) if source.startswith("backend/") else ""
        package = index.by_dir.get(directory)
        if package is not None:
            found[package.import_path] = package
    return [found[key] for key in sorted(found)]


# ---------------------------------------------------------------------------
# go.mod 依赖变化
# ---------------------------------------------------------------------------

_REQUIRE_LINE_RE = re.compile(r"^\s*(?P<module>[^\s/][^\s]*)\s+(?P<version>v[^\s]+)")
_DIRECTIVE_RE = re.compile(r"^(?P<name>go|toolchain)\s+(?P<value>\S+)")


def parse_go_mod_requirements(text: str) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """返回 (require 的模块→版本, go／toolchain 指令, replace／exclude／retract 行)。"""

    requirements: dict[str, str] = {}
    directives: dict[str, str] = {}
    other: list[str] = []
    block = ""
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].rstrip()
        if not line.strip():
            continue
        if block:
            if line.strip() == ")":
                block = ""
                continue
            if block == "require":
                match = _REQUIRE_LINE_RE.match(line)
                if match:
                    requirements[match.group("module")] = match.group("version")
            else:
                other.append(f"{block} {line.strip()}")
            continue
        stripped = line.strip()
        if stripped.endswith("(") and stripped.split()[0] in ("require", "replace", "exclude", "retract"):
            block = stripped.split()[0]
            continue
        directive = _DIRECTIVE_RE.match(stripped)
        if directive:
            directives[directive.group("name")] = directive.group("value")
        elif stripped.startswith("require "):
            match = _REQUIRE_LINE_RE.match(stripped[len("require ") :])
            if match:
                requirements[match.group("module")] = match.group("version")
        elif stripped.split()[0] in ("replace", "exclude", "retract"):
            other.append(stripped)
    return requirements, directives, sorted(other)


def changed_modules(fork_text: str, candidate_text: str) -> tuple[dict[str, tuple[str | None, str | None]], list[str]]:
    """候选相对 fork 版本有变化的模块，以及需要退回全量的原因（指令或 replace／exclude 变化）。"""

    before, before_directives, before_other = parse_go_mod_requirements(fork_text)
    after, after_directives, after_other = parse_go_mod_requirements(candidate_text)
    reasons: list[str] = []
    if before_directives != after_directives:
        reasons.append(f"go／toolchain 指令变化：{before_directives} → {after_directives}")
    if before_other != after_other:
        reasons.append("replace／exclude／retract 指令变化")
    changes = {
        module: (before.get(module), after.get(module))
        for module in sorted(set(before) | set(after))
        if before.get(module) != after.get(module)
    }
    return changes, reasons


def go_dependents_of_modules(
    backend: Path,
    modules: Sequence[str],
    requirements: Mapping[str, str],
    go_list: GoList,
) -> dict[str, set[str]]:
    """依赖（含传递）指定模块里任一包的本模块包：{包导入路径: {触发它的模块}}。"""

    ordered = sorted(requirements, key=len, reverse=True)

    def owner(import_path: str) -> str | None:
        for module in ordered:
            if import_path == module or import_path.startswith(module + "/"):
                return module
        return None

    wanted = set(modules)
    result: dict[str, set[str]] = defaultdict(set)
    for tags in ("", "unit", "integration"):
        for item in go_list(backend, tags, "ImportPath,Deps"):
            for dep in item.get("Deps") or []:
                module = owner(str(dep))
                if module in wanted:
                    result[str(item["ImportPath"])].add(module)
    return result


# ---------------------------------------------------------------------------
# 非 Go 文件分类
# ---------------------------------------------------------------------------


@dataclass
class Classified:
    """一个文件的分类结论：``kind`` 决定走哪一类回归，``reason`` 写明依据。"""

    kind: str
    reason: str
    target: str | None = None


def classify_non_go_path(
    path: str,
    *,
    makefile_text: str,
    in_candidate: bool,
    egress_spec_checks: frozenset[str] = frozenset(),
) -> Classified:
    """``backend/`` 里 Go 源码与嵌入资源之外的文件，以及仓库其余部分的文件分类。

    Go 源码与 go:embed 资源由调用方按包索引处理；这里只处理其余文件。
    """

    name = posixpath.basename(path)
    if path.startswith("backend/"):
        if path in BACKEND_NO_TEST_FILES:
            return Classified("no_test_needed", BACKEND_NO_TEST_FILES[path])
        if path.endswith(".md"):
            return Classified("no_test_needed", "文档，不参与编译与测试")
        return Classified("unresolved", "backend 下既不是 Go 源码、嵌入资源，也不在已登记的构建／配置文件里，无法判断是否影响测试")
    if path.startswith("frontend/"):
        if not in_candidate:
            return Classified("no_test_needed", "文件在候选中已删除；残留引用由类型检查与 lint 检查线暴露")
        relative = path[len("frontend/") :]
        if relative.startswith("src/"):
            if relative.endswith(STATIC_ASSET_SUFFIXES):
                return Classified("no_test_needed", "图片与字体等静态资源，没有对应的用例")
            if relative.endswith(FRONTEND_TEST_RUNNER_EXTENSIONS):
                if relative in FRONTEND_GLOBAL_SETUP:
                    return Classified(
                        "unresolved",
                        "vitest 的 setupFiles：所有用例都会加载它，相关用例即全部用例；全部 vitest 用例不是 U-4／CI 的检查项，须人工确认影响",
                    )
                return Classified("frontend_related", "frontend/src 下的源码或用例：只跑 import 依赖图里引用它的用例", relative)
            return Classified("unresolved", "frontend/src 下无法判断是否被用例引用的文件类型")
        if relative in FRONTEND_CONFIG_NAMES:
            return Classified(
                "no_test_needed",
                "前端依赖或工具链配置：前端检查线（lint、typecheck、关键用例）按候选树重跑，与 U-4 和 CI 的前端线完全一致；"
                "全部 vitest 用例不是 U-4／CI 的检查项（本机基线上就有依赖时区的用例失败），快线不另跑",
            )
        if relative.startswith("public/") or relative.endswith(NO_TEST_DOC_SUFFIXES):
            return Classified("no_test_needed", "静态资源或文档，没有对应的用例")
        return Classified("unresolved", "frontend 下不在 src 里、也不是已登记的配置文件")
    if path.startswith(DEPLOY_PREFIX):
        return Classified("deploy", "deploy 下的部署脚本与编排文件：执行 CI shell 作业里的部署脚本检查")
    if path.startswith("tools/"):
        if path.startswith(CAPTURE_TOOLS_PREFIX):
            return Classified(
                "unresolved",
                "采集工具树要整条采集线（全量约 33 分钟）才测得全，快线不含；合并期间本不应改动这棵受管工具树，须人工确认",
            )
        # tools/upstream_merge_*.schema.json 是合并工具的 schema，由 tools/upstream_merge 的 unittest 校验，按该包处理。
        owner_path = f"tools/upstream_merge/{name}" if name.startswith("upstream_merge_") and name.endswith(".schema.json") else path
        for package, (tests, target) in PYTHON_TEST_PACKAGES.items():
            if owner_path == package or owner_path.startswith(package + "/"):
                if target in egress_spec_checks:
                    return Classified(
                        "no_test_needed",
                        f"{package} 的 unittest 是出站规格线的子检查 {target}，该线已完整执行",
                    )
                return Classified("python_tests", f"{package} 有自己的 unittest 目录，跑整个目录", tests)
        if re.search(r"(?<![\w/.-])" + re.escape(path) + r"(?![\w/.-])", makefile_text):
            return Classified("no_test_needed", "被 Makefile 的出站规格子检查直接调用，出站规格检查线覆盖")
        return Classified("unresolved", "tools 下没有对应测试目录、也不被出站规格子检查直接调用的文件")
    if path.startswith(".github/workflows/"):
        return Classified(
            "no_test_needed",
            "CI 工作流：候选 toolchain-check 核对其中 Go／lint 版本，作业覆盖由预检登记并由 U-4 的 CI 证据兜底",
        )
    if path.startswith(".github/release-tools/"):
        return Classified("unresolved", "release-helpers 作业只在 CI 跑（见 ci_job_coverage.json），合并前须在试验 worktree 先执行其命令")
    if path.startswith(".github/"):
        return Classified("no_test_needed", ".github 下的说明、模板或例外清单，不参与本机检查线")
    if path.startswith("docs/egress/"):
        return Classified("no_test_needed", "出站冻结台账与基线：其摘要由出站规格线（冻结摘要检查）覆盖，改动是否闭合由台账 revision 与 U-4 前的定向冻结检查确认")
    if path.startswith(("docs/", "assets/")) or path.endswith(NO_TEST_DOC_SUFFIXES):
        reason = "文档或静态资源，没有对应的用例"
        if path.startswith("docs/"):
            reason += "；docs 下被冻结台账登记的文件摘要由出站规格线（冻结摘要检查）覆盖"
        return Classified("no_test_needed", reason)
    if name in ("Dockerfile", "Dockerfile.goreleaser", ".dockerignore") or name.startswith("docker-compose"):
        return Classified("no_test_needed", "镜像与编排文件：本机无镜像构建检查，发版流水线构建，U-4 同样不覆盖")
    if name in (".gitignore", ".gitattributes", "LICENSE", "VERSION", ".gitmodules", ".editorconfig", ".mcp.json"):
        return Classified("no_test_needed", "仓库元数据，不参与编译与测试")
    if path == "Makefile":
        return Classified(
            "no_test_needed",
            "受管工具闭集成员：上游对它的改动由预检『闭集受扰清单』与基线提前处置；快线调用的 make 目标本身就取自候选树里的这份 Makefile",
        )
    if name == "go.mod" or name == "go.sum":
        return Classified("unresolved", "backend 之外的 Go 模块，快线不含")
    return Classified("unresolved", "无法归入任何已知的回归类别")


# ---------------------------------------------------------------------------
# 前端：按 import 依赖图找出引用改动文件的用例
# ---------------------------------------------------------------------------

# vitest 自带的 ``related`` 在本仓库（vitest 2.1.9）遍历依赖时对 .vue 文件解析报错（RollupError）而崩溃，实测不可用，
# 所以这里自己扫 import 依赖图：改动文件被哪些用例（传递）引用，就跑哪些用例，并且把对应关系写进清单。
FRONTEND_CODE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue")
FRONTEND_RESOLVE_SUFFIXES = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".json")
FRONTEND_INDEX_NAMES = ("index.ts", "index.tsx", "index.js", "index.jsx", "index.vue")
FRONTEND_SPEC_RE = re.compile(r"^src/.*\.(?:test|spec)\.(?:js|ts|jsx|tsx)$")
# 全部用例都会加载的测试公共文件（vitest.config.ts 的 setupFiles）：它变化等于所有用例相关。
FRONTEND_GLOBAL_SETUP = ("src/__tests__/setup.ts",)
_FRONTEND_IMPORT_RE = re.compile(
    r"(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+|\brequire\s*\(\s*|\bvi\.(?:mock|doMock|importActual|importMock)\s*\(\s*)"
    r"(['\"])(?P<spec>[^'\"\n]+)\1"
)


def load_frontend_sources(frontend_dir: Path) -> dict[str, str | None]:
    """frontend/src 下全部文件：代码文件读文本，其余（样式、json、图片）只记录存在，文本为 None。键是相对 frontend 的路径。"""

    sources: dict[str, str | None] = {}
    source_root = frontend_dir / "src"
    if not source_root.is_dir():
        return sources
    for current, _directories, names in os.walk(source_root):
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(frontend_dir).as_posix()
            if name.endswith(FRONTEND_CODE_SUFFIXES) and not path.is_symlink():
                sources[relative] = path.read_text(encoding="utf-8", errors="replace")
            else:
                sources[relative] = None
    return sources


def _resolve_frontend_import(specifier: str, importer: str, known: set[str]) -> str | None:
    if specifier.startswith("@/"):
        base = "src/" + specifier[2:]
    elif specifier.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    else:
        return None
    base = base.split("?", 1)[0]
    for suffix in FRONTEND_RESOLVE_SUFFIXES:
        if base + suffix in known:
            return base + suffix
    for index in FRONTEND_INDEX_NAMES:
        candidate = posixpath.join(base, index)
        if candidate in known:
            return candidate
    return None


def related_frontend_specs(sources: Mapping[str, str | None], changed: Sequence[str]) -> dict[str, list[str]]:
    """{用例: [经 import 依赖引用到的改动文件]}。改动文件本身若是用例也算（引用自己）。

    依赖边来自 ``import … from``、副作用 import、动态 ``import()``、``require`` 与 ``vi.mock`` 等写法，
    解析 ``@/`` 别名与相对路径；``import.meta.glob`` 这类运行期拼出的依赖解析不了，由前端检查线和关键用例兜底。
    """

    known = set(sources)
    importers: dict[str, set[str]] = defaultdict(set)
    for importer, text in sources.items():
        if text is None:
            continue
        for match in _FRONTEND_IMPORT_RE.finditer(text):
            target = _resolve_frontend_import(match.group("spec"), importer, known)
            if target is not None and target != importer:
                importers[target].add(importer)
    result: dict[str, list[str]] = defaultdict(list)
    for start in changed:
        if start not in known:
            continue
        seen = {start}
        queue = [start]
        while queue:
            current = queue.pop()
            if FRONTEND_SPEC_RE.match(current):
                result[current].append(start)
            for parent in importers.get(current, ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
    return {spec: sorted(set(files)) for spec, files in sorted(result.items())}


# ---------------------------------------------------------------------------
# CI 命令与 Makefile 现取
# ---------------------------------------------------------------------------

_MAKE_PACKAGE_RE = re.compile(r"^\./[\w./-]+$")
_SAFE_PACKAGE_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$|^$")


def egress_spec_go_test_patterns(makefile_text: str) -> list[str]:
    """Makefile 里 ``egress-spec-go-test`` 目标列出的包模式（相对 backend，形如 ``internal/officialegress/...``）。"""

    patterns: list[str] = []
    lines = makefile_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("egress-spec-go-test:"):
            for body in lines[index + 1 :]:
                if body and not body.startswith(("\t", " ")):
                    break
                for token in body.replace("\\", " ").split():
                    if _MAKE_PACKAGE_RE.match(token):
                        patterns.append(token[2:].rstrip("/"))
            break
    return patterns


def egress_spec_checks(makefile_text: str) -> frozenset[str]:
    """Makefile 里 ``EGRESS_SPEC_CI_CHECKS`` 列出的出站规格子检查目标名。"""

    continuation = chr(92)  # 反斜杠：Makefile 的续行符
    lines = makefile_text.splitlines()
    names: set[str] = set()
    for index, line in enumerate(lines):
        if line.startswith("EGRESS_SPEC_CI_CHECKS"):
            body = line.split(":=", 1)[1] if ":=" in line else ""
            cursor = index
            while True:
                names.update(token for token in body.replace(continuation, " ").split() if re.fullmatch(r"[\w.-]+", token))
                if not body.rstrip().endswith(continuation):
                    break
                cursor += 1
                if cursor >= len(lines):
                    break
                body = lines[cursor]
            break
    return frozenset(names)


def _pattern_covers(pattern: str, rel_dir: str) -> bool:
    if pattern.endswith("/..."):
        base = pattern[: -len("/...")]
        return rel_dir == base or rel_dir.startswith(base + "/")
    return rel_dir == pattern


def deploy_commands_from_workflow(workflow_text: str) -> list[list[str]]:
    """CI 工作流里与 deploy 相关的 shell 命令（``shell`` 作业与 test 作业的 simple-mode 步骤），逐行一条 argv。"""

    document = parse_ci_workflow(workflow_text)
    commands: list[list[str]] = []
    for step in document.steps:
        if not step.run or "deploy/" not in step.run:
            continue
        for line in step.run.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] in ("/bin/sh", "/bin/bash") and any(item.startswith("deploy/") for item in parts[1:]):
                if parts not in commands:
                    commands.append(parts)
    return commands


# ---------------------------------------------------------------------------
# 清单生成
# ---------------------------------------------------------------------------


def _origin_labels(path: str, facts: GitFacts) -> list[str]:
    labels = []
    if path in facts.conflict:
        labels.append("conflict")
    if path in facts.actual_modified:
        labels.append("actual_modified")
    if path in facts.upstream_only:
        labels.append("upstream_only")
    return labels


_ORIGIN_TEXT = {"conflict": "冲突文件", "actual_modified": "实际修改", "upstream_only": "上游单侧改动"}


def _file_origin_text(files: Sequence[tuple[str, list[str]]], limit: int = 3) -> str:
    counts: dict[str, int] = defaultdict(int)
    for _path, labels in files:
        for label in labels:
            counts[label] += 1
    parts = [f"{_ORIGIN_TEXT[label]} {count} 个" for label, count in sorted(counts.items())]
    examples = "、".join(path for path, _labels in files[:limit])
    more = "等" if len(files) > limit else ""
    return f"{'、'.join(parts)}（{examples}{more}）"


def estimate_seconds(rel_dirs: Sequence[str], label: str = "default") -> int:
    """一个标签组里这批包的 go test 估算墙钟：最重的包加其余包的 ``GO_PARALLEL_SHARE``。"""

    column = 1 if label == "unit" else 0
    seconds = [GO_PACKAGE_SECONDS[rel_dir][column] if rel_dir in GO_PACKAGE_SECONDS else GO_DEFAULT_PACKAGE_SECONDS for rel_dir in rel_dirs]
    if not seconds:
        return 0
    heaviest = max(seconds)
    return round(heaviest + GO_PARALLEL_SHARE * (sum(seconds) - heaviest))


def plan_targeted_regression(
    repository: Path,
    fork: str,
    upstream: str,
    *,
    go_list: GoList = run_go_list,
    budget_seconds: int = TARGETED_GO_BUDGET_SECONDS,
    max_packages: int = MAX_TARGETED_GO_PACKAGES,
    upstream_only_importers: bool = True,
    facts: GitFacts | None = None,
    now: Callable[[], str] = _now,
) -> dict[str, Any]:
    """生成定向回归清单文档（已带 ``identity_sha256``）。

    ``repository`` 是试验 worktree（候选树＝它的 HEAD，工作树必须干净）。不联网、不写仓库内任何文件。
    """

    root = assert_git_repository(repository)
    assert_clean(root, "试验 worktree")
    if facts is None:
        facts = collect_git_facts(root, fork, upstream)
    makefile_text = (root / "Makefile").read_text(encoding="utf-8") if (root / "Makefile").is_file() else ""
    spec_checks = egress_spec_checks(makefile_text)

    # 所有受关注的文件及其来源标签：冲突、实际修改，以及（只用于 Go）上游单侧改动。
    focus = sorted(facts.conflict | facts.actual_modified)
    upstream_only = sorted(facts.upstream_only)

    def in_candidate(path: str) -> bool:
        return path in facts.candidate_files

    no_test: list[dict[str, str]] = []
    unresolved: list[dict[str, Any]] = []
    frontend_related: dict[str, list[str]] = {}
    python_suites: dict[str, dict[str, Any]] = {}
    deploy_files: list[str] = []
    go_mod_paths: list[str] = []

    backend_prefix = GO_MODULE_DIR + "/"
    backend = root / GO_MODULE_DIR
    index: GoIndex | None = None
    go_relevant = [path for path in [*focus, *upstream_only] if path.startswith(backend_prefix)]
    if go_relevant and backend.is_dir():
        index = build_go_index(backend, go_list)

    seeds: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    data_references: set[str] = set()
    dependency_seeds: dict[str, set[str]] = defaultdict(set)
    removed_packages: list[dict[str, str]] = []
    fallback_reasons: list[str] = []

    def place_go_file(path: str) -> bool:
        """把 backend 下的文件归到 Go 包；归得上返回 True。找不到所属包时登记 fallback 原因。"""

        assert index is not None
        relative = path[len(backend_prefix) :]
        directory = posixpath.dirname(relative)
        is_go = relative.endswith(".go")
        # .go 文件按所在目录归包；其余文件只认 go:embed 嵌入资源与 testdata 夹具，不能凭“同目录”硬归。
        package = index.by_dir.get(directory) if is_go else index.embed_owner.get(relative)
        if package is None and "testdata" in relative.split("/"):
            package = _nearest_package(index, directory)
        if package is None and is_go and not in_candidate(path):
            # 删除的是该目录里最后一个 Go 文件：包已不存在，没有包可测；残留引用由 lint 检查线暴露。
            parent = posixpath.dirname(path)
            if not any(item.endswith(".go") and posixpath.dirname(item) == parent for item in facts.candidate_files):
                removed_packages.append({"path": path, "reason": "所在的包在候选中已不存在；引用残留由 lint 检查线暴露"})
                return True
        if package is None and is_go:
            # 目录里没有任何标签组下可编译的文件（例如只带 e2e 构建标签）：U-4 的三组 go test 与 lint 同样不编译它。
            if in_candidate(path) and path not in facts.upstream_only:
                no_test.append(
                    {
                        "path": path,
                        "origin": ",".join(_origin_labels(path, facts)),
                        "reason": "所在目录在默认、unit、integration 三组标签下都没有可编译的 Go 文件（构建标签排除，如 e2e），"
                        "U-4 的 go test 与 lint 同样不运行它",
                    }
                )
            return True
        if package is None:
            # 非 Go 数据文件（夹具、定价表等）：按文件名在 backend 的 Go 源码里找引用它的包。
            data_file = relative.endswith(DATA_FILE_SUFFIXES) and path not in BACKEND_NO_TEST_FILES
            referencing = _referencing_go_packages(root, facts.candidate_commit, path, index) if data_file and in_candidate(path) else []
            if not referencing:
                return False
            for referenced in referencing:
                seeds[referenced.import_path].append((path, _origin_labels(path, facts)))
            data_references.add(path)
            return True
        seeds[package.import_path].append((path, _origin_labels(path, facts)))
        return True

    for path in focus + upstream_only:
        origins = _origin_labels(path, facts)
        is_upstream_only = path in facts.upstream_only and path not in facts.conflict and path not in facts.actual_modified
        if path in (f"{GO_MODULE_DIR}/go.mod", f"{GO_MODULE_DIR}/go.sum"):
            go_mod_paths.append(path)
            continue
        if path.startswith(backend_prefix) and index is not None:
            if place_go_file(path):
                continue
            if is_upstream_only:
                # 上游单侧改动且与上游逐字节相同的非 Go 文件，不触发定向用例。
                continue
        if is_upstream_only:
            continue
        classified = classify_non_go_path(
            path,
            makefile_text=makefile_text,
            in_candidate=in_candidate(path),
            egress_spec_checks=spec_checks,
        )
        if classified.kind == "no_test_needed":
            no_test.append({"path": path, "origin": ",".join(origins), "reason": classified.reason})
        elif classified.kind == "frontend_related":
            frontend_related[str(classified.target)] = origins
        elif classified.kind == "python_tests":
            suite = python_suites.setdefault(str(classified.target), {"tests": str(classified.target), "files": [], "reason": classified.reason})
            suite["files"].append(path)
        elif classified.kind == "deploy":
            deploy_files.append(path)
        else:
            unresolved.append(
                {"path": path, "origin": ",".join(origins), "reason": classified.reason, "go_related": path.startswith(backend_prefix)}
            )

    # go.mod：依赖版本变化按“依赖该模块的包”并入。
    go_mod_section: dict[str, Any] = {"paths": sorted(go_mod_paths), "changed_modules": {}, "reasons": []}
    if go_mod_paths and backend.is_dir():
        fork_mod = _blob_text(root, facts.fork_commit, f"{GO_MODULE_DIR}/go.mod") or ""
        candidate_mod = _blob_text(root, facts.candidate_commit, f"{GO_MODULE_DIR}/go.mod") or ""
        changes, mod_reasons = changed_modules(fork_mod, candidate_mod)
        go_mod_section["changed_modules"] = {module: {"fork": old, "candidate": new} for module, (old, new) in changes.items()}
        fallback_reasons.extend(mod_reasons)
        if not changes and not mod_reasons:
            sum_only = [path for path in go_mod_paths if path.endswith("go.sum")]
            if sum_only:
                fallback_reasons.append("backend/go.sum 变化但 go.mod 的依赖版本没有变化，无法确定影响面")
        if changes:
            if index is None:
                index = build_go_index(backend, go_list)
            requirements, _directives, _other = parse_go_mod_requirements(candidate_mod)
            requirements = {**parse_go_mod_requirements(fork_mod)[0], **requirements}
            for package, modules in go_dependents_of_modules(backend, list(changes), requirements, go_list).items():
                dependency_seeds[package].update(modules)

    # ---- Go 受影响包 = 种子包 + 直接导入它们的包 ----
    packages: dict[str, dict[str, Any]] = {}
    if index is not None:
        for import_path, files in seeds.items():
            package = index.by_import[import_path]
            packages[import_path] = {
                "dir": package.rel_dir,
                "seed_files": [{"path": path, "origin": labels} for path, labels in sorted(files)],
                "dependency_modules": [],
                "importer_of": [],
                "reasons": [],
            }
        for import_path, modules in dependency_seeds.items():
            package = index.by_import[import_path]
            entry = packages.setdefault(
                import_path, {"dir": package.rel_dir, "seed_files": [], "dependency_modules": [], "importer_of": [], "reasons": []}
            )
            entry["dependency_modules"] = sorted(modules)
        # 直接导入者
        strong_seed = {
            import_path
            for import_path, entry in packages.items()
            if entry["dependency_modules"]
            or any(any(label != "upstream_only" for label in item["origin"]) for item in entry["seed_files"])
        }
        for import_path in sorted(packages):
            if not upstream_only_importers and import_path not in strong_seed:
                continue
            for importer in sorted(index.importers.get(import_path, ())):
                target = index.by_import[importer]
                entry = packages.setdefault(
                    importer, {"dir": target.rel_dir, "seed_files": [], "dependency_modules": [], "importer_of": [], "reasons": []}
                )
                if import_path not in entry["importer_of"]:
                    entry["importer_of"].append(import_path)
        for import_path, entry in packages.items():
            reasons: list[str] = []
            if entry["seed_files"]:
                files = [(item["path"], item["origin"]) for item in entry["seed_files"]]
                data = [path for path, _labels in files if path in data_references]
                suffix = f"（其中 {len(data)} 个数据文件按文件名被该包的 Go 代码引用）" if data else ""
                reasons.append(f"{_file_origin_text(files)} → 包 {entry['dir'] or '.'}{suffix}")
            if entry["dependency_modules"]:
                reasons.append(f"依赖版本变化的模块 {', '.join(entry['dependency_modules'])}（传递依赖）→ 包 {entry['dir'] or '.'}")
            if entry["importer_of"]:
                names = [index.by_import[item].rel_dir or "." for item in entry["importer_of"]]
                reasons.append(f"直接导入受影响的包 {', '.join(names[:5])}{' 等' if len(names) > 5 else ''}")
            entry["reasons"] = reasons

    # ---- 标签组与出站规格线去重 ----
    patterns = egress_spec_go_test_patterns(makefile_text)
    tag_sets: dict[str, dict[str, Any]] = {}
    for label, tags in GO_TAG_SETS:
        selected: list[str] = []
        covered: list[str] = []
        no_tests: list[str] = []
        for import_path in sorted(packages, key=lambda item: packages[item]["dir"]):
            assert index is not None
            package = index.by_import[import_path]
            if not package.present.get(label):
                continue
            rel_dir = package.rel_dir
            if not package.has_tests.get(label):
                no_tests.append(rel_dir)
                continue
            if label == "default" and any(_pattern_covers(pattern, rel_dir) for pattern in patterns):
                covered.append(rel_dir)
                continue
            selected.append(rel_dir)
        tag_sets[label] = {
            "tags": tags,
            "packages": selected,
            "covered_by_egress_spec_lane": covered,
            "no_test_files": no_tests,
            "estimated_seconds": estimate_seconds(selected, label),
        }
    total_packages = len({rel for data in tag_sets.values() for rel in data["packages"]})
    estimated = sum(data["estimated_seconds"] for data in tag_sets.values())
    unresolved_go = [item for item in unresolved if item["go_related"]]
    if unresolved_go:
        fallback_reasons.append(f"backend 下有 {len(unresolved_go)} 个归不了类的文件，影响面说不清（见 unresolved）")
    if total_packages > max_packages:
        fallback_reasons.append(f"受影响且有测试的包 {total_packages} 个，超过上限 {max_packages}，定向已不比全量清晰")
    if estimated > budget_seconds:
        fallback_reasons.append(f"定向 go test 估算 {estimated} 秒，超过预算 {budget_seconds} 秒，定向已不比全量省")
    fallback = bool(fallback_reasons)

    # ---- 其余类别 ----
    frontend_files = sorted(frontend_related)
    frontend_specs: dict[str, list[str]] = {}
    frontend_without_spec: list[str] = []
    if frontend_files:
        frontend_specs = related_frontend_specs(load_frontend_sources(root / "frontend"), frontend_files)
        reached = {file for files in frontend_specs.values() for file in files}
        frontend_without_spec = [path for path in frontend_files if path not in reached]
        for path in frontend_without_spec:
            no_test.append(
                {
                    "path": f"frontend/{path}",
                    "origin": ",".join(frontend_related[path]),
                    "reason": "没有任何用例（传递）import 它；类型检查与 lint 检查线、关键用例覆盖。运行期拼出的依赖（import.meta.glob）解析不了",
                }
            )
    deploy_commands: list[list[str]] = []
    if deploy_files:
        workflow_text = (root / CI_WORKFLOW_PATH).read_text(encoding="utf-8") if (root / CI_WORKFLOW_PATH).is_file() else ""
        deploy_commands = deploy_commands_from_workflow(workflow_text)
        if not deploy_commands:
            unresolved.append(
                {
                    "path": deploy_files[0],
                    "origin": "",
                    "reason": f"从 {CI_WORKFLOW_PATH} 里找不到部署脚本检查命令，deploy 下的 {len(deploy_files)} 个文件无法对应测试",
                    "go_related": False,
                }
            )

    python_list = [
        {
            "tests": suite["tests"],
            "argv": [part.replace("{tests}", suite["tests"]) for part in UNIT_TEST_COMMAND],
            "cwd": ".",
            "files": sorted(suite["files"]),
            "reason": suite["reason"],
        }
        for suite in sorted(python_suites.values(), key=lambda item: item["tests"])
    ]
    supplement_reasons = [f"{item['path']}：{item['reason']}" for item in unresolved]
    document: dict[str, Any] = {
        "schema_version": TARGETED_SCHEMA,
        "generated_at_utc": now(),
        "inputs": {
            "repository": str(root),
            "candidate_commit": facts.candidate_commit,
            "candidate_tree": rev_parse(root, f"{facts.candidate_commit}^{{tree}}"),
            "fork_commit": facts.fork_commit,
            "fork_ref": fork,
            "upstream_commit": facts.upstream_commit,
            "upstream_ref": upstream,
            "merge_base": facts.merge_base,
        },
        "facts": {
            "fork_changed_count": len(facts.fork_changed),
            "upstream_changed_count": len(facts.upstream_changed),
            "conflict_files": sorted(facts.conflict),
            "actual_modified_files": sorted(facts.actual_modified),
            "upstream_only_unchanged_count": len(facts.upstream_only),
            "upstream_only_unchanged_files": upstream_only,
        },
        "go": {
            "module_dir": GO_MODULE_DIR,
            "mode": "fallback_full" if fallback else "targeted",
            "fallback_full_go": fallback,
            "fallback_reasons": sorted(set(fallback_reasons)),
            "go_mod": go_mod_section,
            "removed_packages": removed_packages,
            "affected_packages": [
                {"package": import_path, "dir": entry["dir"], "reasons": entry["reasons"], **{k: entry[k] for k in ("seed_files", "dependency_modules", "importer_of")}}
                for import_path, entry in sorted(packages.items(), key=lambda item: item[1]["dir"])
            ],
            "tag_sets": tag_sets,
            "estimated_seconds": estimated,
            "budget_seconds": budget_seconds,
            "max_packages": max_packages,
            "upstream_only_importers": upstream_only_importers,
            "egress_spec_go_test_patterns": patterns,
            "integration": {"executed_in_fast": False, "reason": INTEGRATION_NOTE},
        },
        "frontend": {
            "changed_files": frontend_files,
            "origins": {path: frontend_related[path] for path in frontend_files},
            "spec_files": sorted(frontend_specs),
            "specs": {
                spec: {"changed_files": files, "reason": "经 import 依赖引用了改动文件 " + "、".join(files[:3]) + ("等" if len(files) > 3 else "")}
                for spec, files in frontend_specs.items()
            },
            "without_spec": frontend_without_spec,
            "command": frontend_vitest_argv(["<spec_files>"]) if frontend_specs else None,
            "note": "vitest related 在本仓库崩溃（见 related_frontend_specs），改用 import 依赖图选用例",
        },
        "python": python_list,
        "shell": {
            "files": sorted(deploy_files),
            "commands": deploy_commands,
            "reason": "deploy 下的文件变化：执行 CI shell 作业与 simple-mode 步骤里的部署脚本检查（从 backend-ci.yml 现取）" if deploy_files else "",
        },
        "no_test_needed": sorted(no_test, key=lambda item: item["path"]),
        "unresolved": sorted(unresolved, key=lambda item: item["path"]),
        "supplement_required": bool(unresolved),
        "supplement_reasons": supplement_reasons,
        "summary": {
            "conflict_file_count": len(facts.conflict),
            "actual_modified_count": len(facts.actual_modified),
            "affected_go_package_count": len(packages),
            "targeted_go_package_count": total_packages,
            "fallback_full_go": fallback,
            "frontend_changed_file_count": len(frontend_files),
            "frontend_spec_count": len(frontend_specs),
            "python_suite_count": len(python_list),
            "shell_command_count": len(deploy_commands),
            "no_test_needed_count": len(no_test),
            "unresolved_count": len(unresolved),
        },
    }
    return bind_identity(document)


def write_targeted_regression(
    repository: Path,
    fork: str,
    upstream: str,
    output: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """生成并写出清单（写一次、不覆盖）。输出必须在仓库外或 Git 忽略的私有目录，避免弄脏试验树。"""

    root = assert_git_repository(repository)
    assert_private_path(root, output, "定向回归清单")
    if output.exists() or output.is_symlink():
        raise UpstreamMergeError(f"定向回归清单已存在，禁止覆盖：{output}")
    document = plan_targeted_regression(root, fork, upstream, **kwargs)
    write_json_once(output, document)
    binding = file_binding(output)
    summary = document["summary"]
    return {
        "result": "planned",
        "output": str(output),
        "sha256": binding["sha256"],
        "candidate_commit": document["inputs"]["candidate_commit"],
        "fallback_full_go": summary["fallback_full_go"],
        "fallback_reasons": document["go"]["fallback_reasons"],
        "supplement_required": document["supplement_required"],
        "summary": summary,
        "estimated_go_seconds": document["go"]["estimated_seconds"],
    }


# ---------------------------------------------------------------------------
# 读取与校验（快线执行前）
# ---------------------------------------------------------------------------

_TOP_FIELDS = {
    "schema_version",
    "generated_at_utc",
    "inputs",
    "facts",
    "go",
    "frontend",
    "python",
    "shell",
    "no_test_needed",
    "unresolved",
    "supplement_required",
    "supplement_reasons",
    "summary",
    "identity_sha256",
}


def load_targeted_regression(path: Path) -> dict[str, Any]:
    """读取并校验清单：schema、字段闭合、自摘要。返回文档。"""

    document = expect_object(load_json(path, "定向回归清单"), "定向回归清单")
    if document.get("schema_version") != TARGETED_SCHEMA:
        raise UpstreamMergeError(f"定向回归清单 schema_version 必须是 {TARGETED_SCHEMA}")
    expect_exact_fields(document, _TOP_FIELDS, "定向回归清单")
    validate_identity(document, "定向回归清单")
    go = expect_object(document.get("go"), "定向回归清单.go")
    if not isinstance(go.get("fallback_full_go"), bool):
        raise UpstreamMergeError("定向回归清单.go.fallback_full_go 必须是布尔值")
    tag_sets = expect_object(go.get("tag_sets"), "定向回归清单.go.tag_sets")
    for label in ("default", "unit"):
        data = expect_object(tag_sets.get(label), f"定向回归清单.go.tag_sets.{label}")
        packages = data.get("packages")
        if not isinstance(packages, list) or not all(isinstance(item, str) and _SAFE_PACKAGE_DIR_RE.fullmatch(item) for item in packages):
            # 包目录相对 backend，形如 internal/service；不接受绝对路径、上跳或带空白、通配符的写法。
            raise UpstreamMergeError(f"定向回归清单.go.tag_sets.{label}.packages 必须是 backend 相对目录数组")
        if any(item.startswith("/") or ".." in item.split("/") for item in packages):
            raise UpstreamMergeError(f"定向回归清单.go.tag_sets.{label}.packages 含不安全的目录")
    specs = expect_object(document.get("frontend"), "定向回归清单.frontend").get("spec_files")
    if not isinstance(specs, list) or not all(
        isinstance(item, str) and FRONTEND_SPEC_RE.match(item) and ".." not in item.split("/") for item in specs
    ):
        raise UpstreamMergeError("定向回归清单.frontend.spec_files 必须是 frontend/src 下的用例相对路径数组")
    for suite in document.get("python") or []:
        suite = expect_object(suite, "定向回归清单.python")
        if suite.get("tests") not in {tests for tests, _target in PYTHON_TEST_PACKAGES.values()}:
            raise UpstreamMergeError(f"定向回归清单.python.tests 不在受管测试目录内：{suite.get('tests')}")
    shell = expect_object(document.get("shell"), "定向回归清单.shell")
    for command in shell.get("commands") or []:
        if (
            not isinstance(command, list)
            or len(command) < 2
            or command[0] not in ("/bin/sh", "/bin/bash")
            or not all(isinstance(part, str) and part for part in command)
            or any(part.startswith("/") or ".." in part.split("/") for part in command[1:] if not part.startswith("-"))
        ):
            raise UpstreamMergeError(f"定向回归清单.shell.commands 含不受信的命令：{command}")
    return document


def go_test_argv(tags: str, packages: Sequence[str]) -> list[str]:
    """定向 go test 命令（在 backend 目录执行）。"""

    argv = ["go", "test"]
    if tags:
        argv.append(f"-tags={tags}")
    argv += ["-count=1", *[f"./{item}" if item else "." for item in packages]]
    return argv


def frontend_vitest_argv(specs: Sequence[str]) -> list[str]:
    """只跑清单里选出的前端用例（与 ``make test-frontend-critical`` 同样的 vitest run 写法）。"""

    return ["pnpm", "--dir", "frontend", "exec", "vitest", "run", *specs]
