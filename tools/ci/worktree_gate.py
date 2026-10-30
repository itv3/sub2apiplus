#!/usr/bin/env python3
"""新工作树跑门禁的包装（Codex 升级工具优化第 2 项）：给一个提交和一个 make 目标，自己建临时工作树、配好环境、走本机重负载锁跑目标、
不论成败都清掉临时工作树，日志的最后一行固定是 ``EXIT=<退出码>``。

为什么要有：在新建的工作树里跑门禁，10-30、10-31 两天靠手工踩了好几个环境坑——工作树没有被忽略的 ``frontend/node_modules``（``make test-capture-tools`` 要
``CAPTURE_TYPESCRIPT_MODULE``）、没有被忽略的 ``local-analysis``（``make check-egress-spec`` 要 ``CODEX_SPEC_SOURCE_ROOT``）、忘了走重负载锁把 16 GiB 的本机
跑死、跑完忘了清工作树。包装把这些固定下来。

用法（经 ``tools/ci/worktree_gate.sh`` 或直接 ``python3 -B tools/ci/worktree_gate.py``）::

    bash tools/ci/worktree_gate.sh <提交> <make目标> [--log <日志绝对路径>] [选项] [-- <传给 make 的参数>]
    bash tools/ci/worktree_gate.sh HEAD check-egress-spec --log /abs/path/egress.log
    bash tools/ci/worktree_gate.sh "$(git rev-parse HEAD)" test-capture-tools --log /abs/path/capture.log -- CAPTURE_TEST_PARALLELISM=3

做的事（按顺序）：

1. 把提交解析成完整的 40 位提交号（分支名、短号都行）；在 ``<主检出>-wt/gate-<短号>-<时间>-<进程号>`` 建临时工作树（``git worktree add --detach``）。
   放在主检出的同级 ``-wt`` 目录而不是 ``/private/tmp``：系统临时目录的上层属主是 root，受管数据根检查会假红。父目录可用 ``WORKTREE_GATE_PARENT`` 改。
2. 配环境：``CAPTURE_TYPESCRIPT_MODULE``（环境变量优先，否则取主检出 ``frontend/node_modules`` 里的 TypeScript 解析器，并按临时工作树 Makefile 里的
   ``CAPTURE_TYPESCRIPT_SHA256`` 核摘要）、``CODEX_SPEC_SOURCE_ROOT``（环境变量优先，否则取主检出 ``local-analysis/sources/codex-cli-<Makefile 的
   CODEX_SPEC_SOURCE_VERSION>``，核有 ``codex-rs/Cargo.lock``）、``PYTHONDONTWRITEBYTECODE=1``。找不到或不对时给出明确错误并停下（退出码 64），
   不会带着坏环境去跑几十分钟。目标名带 ``frontend`` 或是 ``test``／``build`` 时，把主检出的 ``frontend/node_modules`` 接成符号链接（``--node-modules link|none|auto``）。
3. 走重负载锁：``<主检出>/local-analysis/opt-0165/heavy-run.sh``（主检出由 ``git rev-parse --git-common-dir`` 推出；``HEAVY_RUN`` 环境变量可改路径；
   ``--no-lock`` 显式不走锁）。排队等锁是正常的，等待信息写进日志。
4. 在临时工作树里跑 ``make <目标> [make 参数]``，输出全部进日志。
5. 清理：``git worktree remove --force``，残留（只读文件等）放开权限后删，再 ``git worktree prune``。收到 SIGINT／SIGTERM／SIGHUP 时把信号转给 make 所在进程组，
   等它退出再清理，退出码记 128+信号号。除非给了 ``--keep``（调试用），无论成败都清。
6. 日志最后一行写 ``EXIT=<码>``：make 的退出码（配方失败时 make 自己退出 2）；准备阶段出错记 64；包装自己异常记 70。清理失败不改变 EXIT，但会在日志与终端写明残留路径。

不改主检出的任何文件（只有 ``.git/worktrees`` 下的工作树登记随建随清）。日志默认写主检出的 ``local-analysis/gate-logs/``。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
# 准备阶段出错的退出码取 sysexits 的 EX_USAGE（64），不用 2：make 自己在配方失败时就退出 2，两者混在一起分不清是环境没配好还是目标红了。
EXIT_SETUP = 64
EXIT_INTERNAL = 70
HEAVY_RUN_RELATIVE = "local-analysis/opt-0165/heavy-run.sh"
DEFAULT_LOG_RELATIVE = "local-analysis/gate-logs"
# 目标名命中这些时默认接前端依赖（auto）；其他目标不需要 node_modules，不接（避免多出一个未跟踪的符号链接）。
FRONTEND_TARGETS = {"test", "build", "lint"}
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
FORWARDED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
KILL_GRACE_SECONDS = 30.0


class GateSetupError(RuntimeError):
    """准备阶段的明确错误（环境、路径、参数），退出码 64。"""


class Interrupted(Exception):
    """准备或清理阶段收到终止信号。"""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _sibling(name: str):
    """按路径加载同目录的 prepush_check（复用它对 TypeScript 解析器／Codex 源码树的判据，只维护一份）。"""

    key = f"{name}_worktree_gate_sibling"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise GateSetupError(f"找不到同目录的 {name}.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Log:
    """追加写的日志。每次写入都重新以追加方式打开，make 子进程与本包装的输出按到达顺序交错，不会互相覆盖；终端只回显关键行。"""

    def __init__(self, path: Path, echo: bool = True) -> None:
        self.path, self.echo = path, echo

    def line(self, text: str, *, terminal: bool = False) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        if terminal and self.echo:
            print(text, file=sys.stderr, flush=True)

    def note(self, text: str, *, terminal: bool = False) -> None:
        self.line(f"[worktree_gate] {text}", terminal=terminal)

    def open_for_child(self):
        return self.path.open("ab")


def git(repo: Path, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    completed = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, check=False,
                               env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if check and completed.returncode != 0:
        raise GateSetupError(f"git {' '.join(args)} 失败（退出码 {completed.returncode}）：{(completed.stderr or completed.stdout).strip()[-400:]}")
    return completed


def find_roots(repo_arg: str | None) -> tuple[Path, Path | None]:
    """（当前仓库根，主检出根）。主检出由公共 git 目录推出：公共目录名是 ``.git`` 时取它的父目录；裸仓库等情形记 None。"""

    start = Path(repo_arg).resolve() if repo_arg else Path.cwd()
    if not start.is_dir():
        raise GateSetupError(f"--repo 指向的目录不存在：{start}")
    repo = Path(git(start, "rev-parse", "--show-toplevel").stdout.strip())
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    return repo, (common.parent if common.name == ".git" else None)


def resolve_commit(repo: Path, commit: str) -> str:
    completed = git(repo, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    value = completed.stdout.strip()
    if completed.returncode != 0 or not _FULL_SHA.fullmatch(value):
        raise GateSetupError(f"{commit!r} 不是仓库里的提交")
    return value


def resolve_heavy_run(main_root: Path | None, env: Mapping[str, str], no_lock: bool) -> Path | None:
    """重负载锁脚本：环境变量 HEAVY_RUN 优先，否则 ``<主检出>/local-analysis/opt-0165/heavy-run.sh``。显式 ``--no-lock`` 时不走锁（返回 None）。"""

    if no_lock:
        return None
    configured = env.get("HEAVY_RUN")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or not path.is_file():
            raise GateSetupError(f"HEAVY_RUN 要是存在的绝对路径文件：{configured}")
        return path
    if main_root is None:
        raise GateSetupError("推不出主检出（git 公共目录不是 .git），找不到重负载锁脚本：设环境变量 HEAVY_RUN=<heavy-run.sh 的绝对路径>，或明确加 --no-lock")
    path = main_root / HEAVY_RUN_RELATIVE
    if not path.is_file():
        raise GateSetupError(f"找不到重负载锁脚本 {path}：设环境变量 HEAVY_RUN=<heavy-run.sh 的绝对路径>，或明确加 --no-lock（本机 16 GiB 跑重负载不走锁会把机器跑死）")
    return path


def gate_environment(worktree: Path, main_root: Path | None, env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """算出传给 make 的环境变量（只含要新增或覆盖的），并返回给日志看的说明行。找不到或不对时抛 ``GateSetupError``。"""

    checks = _sibling("prepush_check")
    makefile = (worktree / "Makefile").read_text(encoding="utf-8") if (worktree / "Makefile").is_file() else ""
    expected_sha = checks.parse_make_variable(makefile, "CAPTURE_TYPESCRIPT_SHA256")
    version = checks.parse_make_variable(makefile, "CODEX_SPEC_SOURCE_VERSION")
    if not expected_sha or not version:
        raise GateSetupError("该提交的 Makefile 里没有 CAPTURE_TYPESCRIPT_SHA256／CODEX_SPEC_SOURCE_VERSION 的定义，无法配置门禁环境")
    notes: list[str] = []
    result: dict[str, str] = {"PYTHONDONTWRITEBYTECODE": "1"}

    typescript = env.get("CAPTURE_TYPESCRIPT_MODULE")
    source = "环境变量"
    if not typescript:
        source = "主检出"
        typescript = str(main_root / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js") if main_root else ""
    problem = checks.typescript_problem(typescript, expected_sha) if typescript else "没有设置，也推不出主检出"
    if problem:
        raise GateSetupError(f"CAPTURE_TYPESCRIPT_MODULE 不可用：{problem}。在主检出里 pnpm --dir frontend install --frozen-lockfile --offline，"
                             "或把环境变量 CAPTURE_TYPESCRIPT_MODULE 设成摘要吻合的 typescript.js 绝对路径")
    result["CAPTURE_TYPESCRIPT_MODULE"] = typescript
    notes.append(f"CAPTURE_TYPESCRIPT_MODULE={typescript}（来源：{source}，摘要吻合）")

    spec_root = env.get("CODEX_SPEC_SOURCE_ROOT")
    source = "环境变量"
    if not spec_root:
        source = "主检出"
        spec_root = str(main_root / "local-analysis" / "sources" / f"codex-cli-{version}") if main_root else ""
    problem = checks.spec_root_problem(spec_root, version) if spec_root else "没有设置，也推不出主检出"
    if problem:
        raise GateSetupError(f"CODEX_SPEC_SOURCE_ROOT 不可用：{problem}。把 codex-cli-{version} 源码树（只读）放到主检出的 local-analysis/sources/，"
                             "或把环境变量 CODEX_SPEC_SOURCE_ROOT 设成它的绝对路径")
    result["CODEX_SPEC_SOURCE_ROOT"] = spec_root
    notes.append(f"CODEX_SPEC_SOURCE_ROOT={spec_root}（来源：{source}，版本 {version}，有 Cargo.lock）")
    return result, notes


def wants_node_modules(target: str, mode: str) -> bool:
    if mode == "link":
        return True
    if mode == "none":
        return False
    return "frontend" in target or target in FRONTEND_TARGETS


def link_node_modules(worktree: Path, main_root: Path | None) -> str:
    """把主检出的 ``frontend/node_modules`` 接成工作树里的符号链接（pnpm 的内部链接都是相对的，整目录接过来可用）。返回给日志看的说明。"""

    if main_root is None or not (main_root / "frontend" / "node_modules").is_dir():
        raise GateSetupError("目标需要前端依赖，但主检出里没有 frontend/node_modules：先在主检出 pnpm --dir frontend install --frozen-lockfile --offline，或加 --node-modules none")
    destination = worktree / "frontend" / "node_modules"
    if destination.exists() or destination.is_symlink():
        return "工作树里已有 frontend/node_modules，不再接链接"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(main_root / "frontend" / "node_modules")
    return f"已接 frontend/node_modules → {main_root / 'frontend' / 'node_modules'}（临时工作树里会多一个未跟踪的符号链接，用完随工作树清掉）"


def remove_worktree(repo: Path, worktree: Path, parent: Path, log: Log) -> bool:
    """清掉临时工作树；返回是否清干净。先 ``git worktree remove --force``，不行再放开权限删，最后 prune。"""

    for flags in (("--force",), ("--force", "--force")):
        if not worktree.exists():
            break
        completed = git(repo, "worktree", "remove", *flags, str(worktree), check=False)
        if completed.returncode != 0:
            log.note(f"git worktree remove 退出码 {completed.returncode}：{(completed.stderr or completed.stdout).strip()[-300:]}")
    if worktree.exists():
        # 兜底：只读文件（快照、权限被收紧的缓存）会让 git 半删。只删“父目录之下、名字以 gate- 开头”的路径，防止误删。
        if worktree.parent.resolve() == parent.resolve() and worktree.name.startswith("gate-") and not worktree.is_symlink():
            def writable(function, path, _info):
                try:
                    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR | stat.S_IXUSR)
                    function(path)
                except OSError:
                    pass
            for current, directories, files in os.walk(worktree):
                for name in [*directories, *files]:
                    target = Path(current, name)
                    if not target.is_symlink():
                        try:
                            os.chmod(target, os.stat(target).st_mode | stat.S_IWUSR)
                        except OSError:
                            pass
            shutil.rmtree(worktree, onerror=writable)
    git(repo, "worktree", "prune", check=False)
    return not worktree.exists()


def run_target(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], log: Log, state: dict) -> int:
    """跑 make，输出直接写日志。子进程放在自己的会话里，收到终止信号时转给整个进程组，超过宽限期再 SIGKILL。返回退出码（被信号终止记 128+信号号）。"""

    with log.open_for_child() as sink:
        process = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True)
        state["child"] = process
        deadline = None
        while True:
            try:
                code = process.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if state.get("signal") is not None:
                if deadline is None:
                    deadline = time.monotonic() + KILL_GRACE_SECONDS
                elif time.monotonic() > deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        state["child"] = None
    if state.get("signal") is not None:
        return 128 + int(state["signal"])
    return code if code >= 0 else 128 + (-code)


def install_signal_handlers(state: dict) -> dict:
    """终止信号：有子进程就转给它的进程组（并记下信号，等它退出后清理）；没有子进程就抛 ``Interrupted`` 让准备／清理阶段落进 finally。"""

    def handler(signum, _frame):
        state["signal"] = signum
        child = state.get("child")
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass
            return
        if not state.get("cleaning"):
            raise Interrupted(signum)

    try:
        return {number: signal.signal(number, handler) for number in FORWARDED_SIGNALS}
    except ValueError:   # 不在主线程（被别的程序当库调用）时没法装信号处理，退化为不拦截
        return {}


def execute(args: argparse.Namespace, env: Mapping[str, str], log: Log, state: dict) -> int:
    """真正的流程；返回 EXIT 码。准备阶段出错抛 ``GateSetupError``。清理放在 finally 里。"""

    repo, main_root = find_roots(args.repo)
    commit = resolve_commit(repo, args.commit)
    if not _TARGET.fullmatch(args.target):
        raise GateSetupError(f"make 目标名不合法：{args.target!r}")
    heavy_run = resolve_heavy_run(main_root, env, args.no_lock)
    configured_parent = env.get("WORKTREE_GATE_PARENT")
    if configured_parent:
        parent = Path(configured_parent)
        if not parent.is_absolute():
            raise GateSetupError(f"WORKTREE_GATE_PARENT 要是绝对路径：{configured_parent}")
    elif main_root is not None:
        parent = main_root.parent / f"{main_root.name}-wt"
    else:
        raise GateSetupError("推不出主检出，没法决定临时工作树放哪：设环境变量 WORKTREE_GATE_PARENT=<绝对路径>")
    parent.mkdir(parents=True, exist_ok=True)
    worktree = parent / f"gate-{commit[:12]}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}"
    log.note(f"开始 {utc_now()}：提交 {commit}，make 目标 {args.target}{(' ' + ' '.join(args.make_args)) if args.make_args else ''}", terminal=True)
    created = False
    code = EXIT_INTERNAL
    try:
        added = git(repo, "worktree", "add", "--detach", str(worktree), commit, check=False)
        created = worktree.exists()
        if added.returncode != 0 or not created:
            raise GateSetupError(f"建临时工作树失败：{(added.stderr or added.stdout).strip()[-400:]}")
        log.note(f"临时工作树 {worktree}")
        extra_env, notes = gate_environment(worktree, main_root, env)
        for note in notes:
            log.note(note)
        if wants_node_modules(args.target, args.node_modules):
            log.note(link_node_modules(worktree, main_root))
        child_env = {**env, **extra_env}
        # 锁脚本一律经 bash 调用（不依赖它有没有可执行位）。
        argv = (["bash", str(heavy_run)] if heavy_run else []) + ["make", args.target, *args.make_args]
        log.note(("经重负载锁 " + str(heavy_run) if heavy_run else "不走重负载锁（--no-lock）") + "；命令：" + " ".join(argv))
        started = time.monotonic()
        code = run_target(argv, cwd=worktree, env=child_env, log=log, state=state)
        log.note(f"make 退出码 {code}，用时 {time.monotonic() - started:.0f} 秒")
    finally:
        state["cleaning"] = True
        if created and not args.keep:
            clean = remove_worktree(repo, worktree, parent, log)
            if clean:
                log.note("临时工作树已清理")
            else:
                log.note(f"警告：临时工作树没清干净，残留 {worktree}；手工 git worktree remove --force {worktree}", terminal=True)
        elif created:
            log.note(f"按 --keep 保留临时工作树 {worktree}（用完请手工 git worktree remove --force）", terminal=True)
    return code


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                     usage="worktree_gate.sh <提交> <make目标> [--log 日志绝对路径] [选项] [-- make 参数...]")
    parser.add_argument("commit", help="要跑门禁的提交（分支名、短号都行，内部解析成完整 40 位）")
    parser.add_argument("target", help="make 目标，如 check-egress-spec、test-capture-tools")
    parser.add_argument("--log", type=Path, help="日志文件（绝对路径，追加写）；默认写主检出 local-analysis/gate-logs/")
    parser.add_argument("--repo", help="仓库（默认当前目录所在的仓库）")
    parser.add_argument("--no-lock", action="store_true", help="不走重负载锁（本机 16 GiB 请只在确认没有别的重任务时用）")
    parser.add_argument("--node-modules", choices=("auto", "link", "none"), default="auto", help="是否把主检出的 frontend/node_modules 接进临时工作树（默认 auto：目标需要前端时接）")
    parser.add_argument("--keep", action="store_true", help="跑完保留临时工作树（调试用；默认无论成败都清理）")
    parser.add_argument("make_args", nargs="*", help="-- 之后的内容原样传给 make（如 CAPTURE_TEST_PARALLELISM=3）")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw)
    environment = dict(os.environ if env is None else env)
    state: dict = {"signal": None, "child": None, "cleaning": False}
    previous = install_signal_handlers(state)
    log: Log | None = None
    code = EXIT_INTERNAL
    try:
        try:
            log_path = args.log
            if log_path is None:
                _repo, main_root = find_roots(args.repo)
                if main_root is None:
                    raise GateSetupError("推不出主检出，请用 --log 指定日志文件")
                log_path = main_root / DEFAULT_LOG_RELATIVE / f"{args.target}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{os.getpid()}.log"
            if not log_path.is_absolute():
                raise GateSetupError(f"--log 要是绝对路径：{log_path}")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = Log(log_path)
            log.note(f"日志 {log_path}", terminal=True)   # 第一时间告诉操作员日志在哪：准备阶段出错、排队等锁时也找得到
            code = execute(args, environment, log, state)
        except GateSetupError as error:
            code = EXIT_SETUP
            if log is not None:
                log.note(f"准备阶段出错：{error}", terminal=True)
            else:
                print(f"[worktree_gate] 准备阶段出错：{error}", file=sys.stderr)
        except Interrupted as stop:
            code = 128 + stop.signum
            if log is not None:
                log.note(f"收到信号 {stop.signum}，已中止", terminal=True)
        except Exception:  # noqa: BLE001 —— 包装自己出错也要把 EXIT 写下来
            code = EXIT_INTERNAL
            if log is not None:
                log.note("包装自身异常：\n" + traceback.format_exc(), terminal=True)
            else:
                traceback.print_exc()
        # 清理完成之后才写最后一行；之后不再往日志里写任何东西。
        if state.get("signal") is not None and code < 128:
            code = 128 + int(state["signal"])
        if log is not None:
            log.line(f"EXIT={code}", terminal=True)
        else:
            print(f"EXIT={code}", file=sys.stderr)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return code


if __name__ == "__main__":
    sys.exit(main())
