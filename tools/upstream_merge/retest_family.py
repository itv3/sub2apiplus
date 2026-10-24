"""失败项的“复验族”：一个失败项必须由属于它自己的命令来复验，而不是随便一条通过的命令。

验收时发现：填写者把类别写成 functional、推进一个无关提交、复验写 ``python3 -c "print('ok')"``，就能把
``egress-spec`` 或 lint 的失败关掉。复验族由工具按失败项 id 推导，不接受填写者自报：

* ``lint/golangci-lint-<tag>``：``golangci-lint run``，``--build-tags`` 与失败步骤相同（默认组不带），在 ``backend`` 下；
* ``go-tests/go-test-<tag>``：``go test``，``-tags`` 与失败步骤相同（默认组不带），在 ``backend`` 下，包与 ``-run`` 可收窄；
* ``egress-spec/check-egress-spec``：``make`` 的 ``check-egress-spec``／``check-egress-spec-ci``／任一 ``egress-spec-*`` 目标，
  或覆盖冻结包的 ``go test``；
* ``frontend/*``：``pnpm --dir frontend`` 的对应脚本，或 ``make test-frontend``（lint／typecheck／vitest 都含），
  vitest 步骤另认 ``make test-frontend-critical``；
* ``capture-tools/*``：``make test-capture-tools*``；
* 其余执行组失败（没有步骤信息的组）与非执行组门禁：必须是该门禁自己的展开命令；
* ``worktree-status``：由工具自己核对候选树干净，不需要填写者提供复验命令。

识别 ``make`` 目标时处理 ``-k`` 等开关、``VAR=值`` 赋值参数、``-C`` 目录以及 ``-n``／``-q``／``-t`` 这类根本不执行的开关
（视为没有复验）；``go test`` 的 ``-tags``／``-run``／包参数同理，``-c``／``-n``／``-list`` 不执行测试，一律不认。

命令的相对目录一律按仓库根折算：同一条命令在 ``backend`` 下还是仓库根下执行，对应的 Makefile 与包路径完全不同。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 一条复验命令：(argv, 相对仓库根的 cwd)
Retest = tuple[Sequence[str], str]

# 工具自己执行的“候选树干净”核对（worktree-status 失败项的复验）：输出必须为空。
WORKTREE_CLEAN_ARGV = ("git", "status", "--porcelain=v1", "--untracked-files=all")
WORKTREE_FAILURE_ID = "worktree-status"

FROZEN_PACKAGES = ("internal/officialegress", "internal/service")
FROZEN_RUN_MARKER = "Frozen"
FROZEN_MAKE_TARGETS = ("check-egress-spec", "check-egress-spec-ci", "egress-spec-go-test")
EGRESS_SPEC_PREFIX = "egress-spec-"
FRONTEND_FULL_TARGET = "test-frontend"
FRONTEND_CRITICAL_TARGET = "test-frontend-critical"
CAPTURE_TARGET_PREFIX = "test-capture-tools"

# make 的开关：带必选参数的短开关字母、带可选数字参数的短开关字母、不执行任何命令的开关。
_MAKE_ARGUMENT_LETTERS = frozenset("CfIoW")
_MAKE_OPTIONAL_NUMBER_LETTERS = frozenset("jl")
_MAKE_NO_EXECUTION_LETTERS = frozenset("nqt")
_MAKE_NO_EXECUTION_LONG = frozenset(
    {"--dry-run", "--just-print", "--recon", "--question", "--touch"}
)
_MAKE_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*[:+?!]*=")

# go test 带值的开关（不带 = 时下一个参数是它的值，不能误当成包）。
_GO_VALUE_FLAGS = frozenset(
    {
        "tags", "run", "skip", "p", "count", "timeout", "parallel", "cpu", "bench", "benchtime", "coverprofile",
        "covermode", "coverpkg", "gcflags", "ldflags", "asmflags", "mod", "modfile", "o", "outputdir", "shuffle",
        "vet", "exec", "fuzz", "fuzztime", "fuzzminimizetime", "list", "blockprofile", "cpuprofile", "memprofile",
        "mutexprofile", "trace", "overlay", "pkgdir", "toolexec", "installsuffix", "C", "blockprofilerate",
        "memprofilerate", "mutexprofilefraction", "coverpkg", "test.run",
    }
)
_GO_NO_EXECUTION_FLAGS = frozenset({"n", "c", "list"})
_PNPM_VALUE_OPTIONS = frozenset({"dir", "C", "filter", "workspace-root", "reporter", "loglevel"})


def _join_directory(base: str, extra: str | None) -> str | None:
    """把命令自带的目录参数折算到仓库根；指向仓库外或无法判断（绝对路径）返回 None。"""

    start = posixpath.normpath(base or ".")
    joined = posixpath.normpath(posixpath.join(start, extra)) if extra else start
    if posixpath.isabs(joined) or joined == ".." or joined.startswith("../"):
        return None
    return joined


# ---------------------------------------------------------------------------
# 命令解析
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MakeCall:
    directory: str | None
    targets: tuple[str, ...]
    assignments: tuple[str, ...]
    executes: bool


def parse_make(argv: Sequence[str], cwd: str) -> MakeCall | None:
    """解析 ``make`` 命令行；不是 make 返回 None。目录按 ``-C``／``--directory`` 累计折算到仓库根。"""

    if not argv or posixpath.basename(argv[0]) != "make":
        return None
    directory: str | None = posixpath.normpath(cwd or ".")
    targets: list[str] = []
    assignments: list[str] = []
    executes = True
    index = 1
    only_targets = False

    def change(extra: str) -> None:
        nonlocal directory
        directory = _join_directory(directory, extra) if directory is not None else None

    while index < len(argv):
        token = argv[index]
        index += 1
        if only_targets or not token.startswith("-") or token == "-":
            if _MAKE_ASSIGNMENT_RE.match(token):
                assignments.append(token)
            else:
                targets.append(token)
            continue
        if token == "--":
            only_targets = True
            continue
        if token.startswith("--"):
            name, _, value = token.partition("=")
            if name in _MAKE_NO_EXECUTION_LONG:
                executes = False
            elif name == "--directory":
                if value:
                    change(value)
                elif index < len(argv):
                    change(argv[index])
                    index += 1
            elif name in ("--file", "--makefile", "--include-dir", "--old-file", "--what-if", "--assume-old", "--new-file", "--assume-new"):
                if not value:
                    index += 1
            elif name in ("--jobs", "--load-average"):
                if not value and index < len(argv) and argv[index].isdigit():
                    index += 1
            continue
        # 短开关簇，如 -k、-nk、-j4、-C dir、-Cdir
        position = 1
        while position < len(token):
            letter = token[position]
            position += 1
            if letter in _MAKE_NO_EXECUTION_LETTERS:
                executes = False
            elif letter in _MAKE_ARGUMENT_LETTERS:
                value = token[position:]
                position = len(token)
                if not value and index < len(argv):
                    value = argv[index]
                    index += 1
                if letter == "C":
                    change(value)
            elif letter in _MAKE_OPTIONAL_NUMBER_LETTERS:
                rest = token[position:]
                position = len(token)
                if not rest and index < len(argv) and argv[index].isdigit():
                    index += 1
    return MakeCall(directory, tuple(targets), tuple(assignments), executes)


@dataclass(frozen=True)
class GoTestCall:
    directory: str | None
    tags: frozenset[str] | None
    run: str | None
    packages: tuple[str, ...]
    executes: bool


def parse_go_test(argv: Sequence[str], cwd: str) -> GoTestCall | None:
    """解析 ``go test`` 命令行；不是 go test 返回 None。"""

    if not argv or posixpath.basename(argv[0]) != "go":
        return None
    directory: str | None = posixpath.normpath(cwd or ".")
    index = 1
    # go -C dir test ...
    while index < len(argv) and argv[index].startswith("-"):
        name, _, value = argv[index].lstrip("-").partition("=")
        index += 1
        if name == "C":
            if not value and index < len(argv):
                value = argv[index]
                index += 1
            directory = _join_directory(directory, value) if directory is not None else None
        else:
            return None
    if index >= len(argv) or argv[index] != "test":
        return None
    index += 1
    tags: frozenset[str] | None = None
    run: str | None = None
    packages: list[str] = []
    executes = True
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == "-args":
            break
        if token.startswith("-") and len(token) > 1:
            name, equals, value = token.lstrip("-").partition("=")
            if name in _GO_VALUE_FLAGS and not equals:
                value = argv[index] if index < len(argv) else ""
                index += 1
            if name in _GO_NO_EXECUTION_FLAGS:
                executes = False
            elif name == "tags":
                tags = frozenset(part for part in re.split(r"[,\s]+", value) if part)
            elif name in ("run", "test.run"):
                run = value
            elif name == "C":
                directory = _join_directory(directory, value) if directory is not None else None
            continue
        packages.append(token)
    return GoTestCall(directory, tags, run, tuple(packages), executes)


@dataclass(frozen=True)
class GolangciCall:
    directory: str | None
    build_tags: frozenset[str]
    executes: bool


def parse_golangci_lint(argv: Sequence[str], cwd: str) -> GolangciCall | None:
    """解析 ``golangci-lint run``；二进制按基名识别（``UPSTREAM_GATE_GOLANGCI_LINT`` 可指向绝对路径）。"""

    if not argv or posixpath.basename(argv[0]) != "golangci-lint":
        return None
    directory: str | None = posixpath.normpath(cwd or ".")
    tags: set[str] = set()
    subcommand: str | None = None
    index = 1
    executes = True
    while index < len(argv):
        token = argv[index]
        index += 1
        if token.startswith("-"):
            name, equals, value = token.lstrip("-").partition("=")
            if name in ("build-tags", "b"):
                if not equals and index < len(argv):
                    value = argv[index]
                    index += 1
                tags.update(part for part in re.split(r"[,\s]+", value) if part)
            elif name in ("config", "c", "timeout", "concurrency", "out-format", "color", "path-prefix", "go", "output.text.path"):
                if not equals:
                    index += 1
            elif name == "path":  # 非标准，忽略
                pass
            elif name in ("help", "h", "version", "v"):
                executes = False
            continue
        if subcommand is None:
            subcommand = token
    if subcommand != "run":
        return None
    return GolangciCall(directory, frozenset(tags), executes)


@dataclass(frozen=True)
class PnpmCall:
    directory: str | None
    command: str | None
    argument: str | None


def parse_pnpm(argv: Sequence[str], cwd: str) -> PnpmCall | None:
    """解析 ``pnpm [--dir X] run <脚本>`` 与 ``pnpm [--dir X] exec <程序>``。"""

    if not argv or posixpath.basename(argv[0]) != "pnpm":
        return None
    directory: str | None = posixpath.normpath(cwd or ".")
    command: str | None = None
    argument: str | None = None
    index = 1
    while index < len(argv):
        token = argv[index]
        index += 1
        if token.startswith("-") and len(token) > 1:
            name, equals, value = token.lstrip("-").partition("=")
            if name in _PNPM_VALUE_OPTIONS and not equals and index < len(argv):
                value = argv[index]
                index += 1
            if name in ("dir", "C"):
                directory = _join_directory(directory, value) if directory is not None else None
            continue
        if command is None:
            command = token
            continue
        if argument is None:
            argument = token
            break
    return PnpmCall(directory, command, argument)


# ---------------------------------------------------------------------------
# 复验族
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Family:
    """一个失败项的复验族：``satisfied`` 判断它的复验命令集合里是否至少有一条（或合起来）属于该族。"""

    description: str
    satisfied: Callable[[Sequence[Retest]], bool]
    # 由工具自己核对，不要求填写者提供命令（worktree-status）。
    tool_checked: bool = False


def _package_covers(package: str, target: str) -> bool:
    """go 包参数（./internal/service、./internal/...、./...）是否覆盖仓库内的 target 包路径。"""

    text = package[2:] if package.startswith("./") else package
    if text == "...":
        return True
    if text.endswith("/..."):
        base = text[: -len("/...")]
        return target == base or target.startswith(base + "/")
    return text == target


def _go_test_runs(call: GoTestCall | None) -> bool:
    return call is not None and call.executes


def frozen_retests_satisfied(retests: Sequence[Retest]) -> bool:
    """冻结摘要类失败的复验：``make check-egress-spec／check-egress-spec-ci／egress-spec-go-test``，或在 ``backend``
    下对 ``./internal/officialegress`` 与 ``./internal/service`` 两个包（或 ``./...``）执行 ``-run`` 含 Frozen 的 go test。"""

    covered: set[str] = set()
    for argv, cwd in retests:
        make = parse_make(argv, cwd)
        if make is not None and make.executes and make.directory == "." and set(make.targets) & set(FROZEN_MAKE_TARGETS):
            return True
        go = parse_go_test(argv, cwd)
        if _go_test_runs(go) and go.directory == "backend" and go.run and FROZEN_RUN_MARKER in go.run:
            for target in FROZEN_PACKAGES:
                if any(_package_covers(package, target) for package in go.packages):
                    covered.add(target)
    return covered == set(FROZEN_PACKAGES)


def _lint_family(tag: str) -> Family:
    wanted = frozenset() if tag == "default" else frozenset({tag})
    flag = "不带 --build-tags" if tag == "default" else f"--build-tags={tag}"

    def satisfied(retests: Sequence[Retest]) -> bool:
        for argv, cwd in retests:
            call = parse_golangci_lint(argv, cwd)
            if call is not None and call.executes and call.directory == "backend" and call.build_tags == wanted:
                return True
        return False

    return Family(f"在 backend 下执行 golangci-lint run（{flag}，包范围可收窄）", satisfied)


def _go_test_family(tag: str) -> Family:
    wanted: frozenset[str] | None = None if tag == "default" else frozenset({tag})
    flag = "不带 -tags" if tag == "default" else f"-tags={tag}"

    def satisfied(retests: Sequence[Retest]) -> bool:
        for argv, cwd in retests:
            call = parse_go_test(argv, cwd)
            if not _go_test_runs(call) or call.directory != "backend":
                continue
            if (call.tags or None) == wanted:
                return True
        return False

    return Family(f"在 backend 下执行 go test（{flag}，包与 -run 可收窄）", satisfied)


def _egress_spec_family() -> Family:
    def satisfied(retests: Sequence[Retest]) -> bool:
        for argv, cwd in retests:
            make = parse_make(argv, cwd)
            if make is None or not make.executes or make.directory != ".":
                continue
            if any(target in ("check-egress-spec", "check-egress-spec-ci") or target.startswith(EGRESS_SPEC_PREFIX) for target in make.targets):
                return True
        return frozen_retests_satisfied(retests)

    return Family(
        "仓库根下 make check-egress-spec／check-egress-spec-ci／egress-spec-* 子目标，"
        "或在 backend 下对 ./internal/officialegress 与 ./internal/service 执行 -run 含 Frozen 的 go test",
        satisfied,
    )


def _frontend_family(step: str) -> Family:
    scripts = {"frontend-lint": "lint:check", "frontend-typecheck": "typecheck"}
    make_targets = {FRONTEND_FULL_TARGET}
    if step == "frontend-critical-vitest":
        make_targets.add(FRONTEND_CRITICAL_TARGET)

    def satisfied(retests: Sequence[Retest]) -> bool:
        for argv, cwd in retests:
            make = parse_make(argv, cwd)
            if make is not None and make.executes and make.directory == "." and set(make.targets) & make_targets:
                return True
            pnpm = parse_pnpm(argv, cwd)
            if pnpm is None or pnpm.directory != "frontend":
                continue
            if step in scripts and pnpm.command == "run" and pnpm.argument == scripts[step]:
                return True
            if step == "frontend-critical-vitest":
                if pnpm.command == "exec" and pnpm.argument == "vitest":
                    # vitest 不带 run 会进入 watch 模式，不会结束；必须带 run 子命令
                    if "run" in list(argv)[list(argv).index("vitest") + 1 :]:
                        return True
                if pnpm.command == "run" and pnpm.argument == "test:run":
                    return True
        return False

    if step in scripts:
        description = f"pnpm --dir frontend run {scripts[step]}，或仓库根 make {FRONTEND_FULL_TARGET}"
    else:
        description = (
            f"pnpm --dir frontend exec vitest run／run test:run，或仓库根 make {FRONTEND_CRITICAL_TARGET}／{FRONTEND_FULL_TARGET}"
        )
    return Family(description, satisfied)


def _capture_family() -> Family:
    def satisfied(retests: Sequence[Retest]) -> bool:
        for argv, cwd in retests:
            make = parse_make(argv, cwd)
            if make is not None and make.executes and make.directory == "." and any(
                target.startswith(CAPTURE_TARGET_PREFIX) for target in make.targets
            ):
                return True
        return False

    return Family(f"仓库根下 make {CAPTURE_TARGET_PREFIX}*（分片自检、全量或分片均可）", satisfied)


def _exact_command_family(name: str, expected: tuple[Sequence[str], str] | None) -> Family:
    """没有步骤级信息的执行组与非执行组门禁：必须是该门禁自己的展开命令。"""

    if expected is None:
        return Family(f"门禁 {name} 的展开命令（无法由 plan 推导，当前无法复验）", lambda _retests: False)
    expected_argv, expected_cwd = list(expected[0]), posixpath.normpath(expected[1] or ".")

    def satisfied(retests: Sequence[Retest]) -> bool:
        return any(list(argv) == expected_argv and posixpath.normpath(cwd or ".") == expected_cwd for argv, cwd in retests)

    return Family(f"门禁 {name} 的展开命令：{' '.join(expected_argv)}（cwd {expected_cwd}）", satisfied)


def _worktree_family() -> Family:
    return Family("由工具自己核对候选树干净（不需要填写复验命令）", lambda _retests: True, tool_checked=True)


def family_for_item(item: Mapping[str, Any], group_commands: Mapping[str, tuple[Sequence[str], str]]) -> Family:
    """按失败项推导复验族。``item`` 是 ``failure_items`` 的一项（kind、gate_group、runner_step）。

    步骤不认识时退回“该执行组自己的展开命令”，即只收更严的，不放宽。
    """

    kind = item.get("kind")
    if kind == "worktree":
        return _worktree_family()
    group = item.get("gate_group")
    if kind == "group_step":
        step = str(item.get("runner_step") or "")
        match = re.fullmatch(r"lint/golangci-lint-(default|unit|integration)", step)
        if match:
            return _lint_family(match.group(1))
        match = re.fullmatch(r"go-tests/go-test-(default|unit|integration)", step)
        if match:
            return _go_test_family(match.group(1))
        if step == "egress-spec/check-egress-spec":
            return _egress_spec_family()
        if step in ("frontend/frontend-lint", "frontend/frontend-typecheck", "frontend/frontend-critical-vitest"):
            return _frontend_family(step.split("/", 1)[1])
        if step in ("capture-tools/capture-tools-shard-check", "capture-tools/capture-tools"):
            return _capture_family()
    return _exact_command_family(str(group or item.get("id")), group_commands.get(str(group)))
