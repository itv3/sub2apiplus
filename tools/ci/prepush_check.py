#!/usr/bin/env python3
"""推送前一键预检（Codex 升级工具优化第 2 项）：一条命令把“推 main 前常踩、却要等 CI 或门禁跑很久才暴露”的非代码坑一次查完。

为什么要有：10-30、10-31 两天，非代码原因白跑过约 8 次——冻结 dry-run 不覆盖指南第二部分的章节摘要（改了 SPEC-* 所在的第二部分，
capture-tools 十个单元红，白跑约 25 分钟）、前端审计遇到刚发布 1～2 天的新公告、改了冻结路径却没补承接收据、新工作树缺 TypeScript 解析器或 Codex
源码树、本机收据没做、``--output`` 写成相对路径、选择器没给 40 位提交号。这些全都能在几秒内查清，所以推送前先查，红的一次报全，每项写明原因和修法。

用法（在要推送的检出里跑，默认范围 ``origin/main..HEAD``）::

    PYTHONDONTWRITEBYTECODE=1 python3 -B tools/ci/prepush_check.py            # 全部八项
    PYTHONDONTWRITEBYTECODE=1 python3 -B tools/ci/prepush_check.py --list     # 只列检查项
    PYTHONDONTWRITEBYTECODE=1 python3 -B tools/ci/prepush_check.py --fast     # 不跑联网的两项（前端审计、govulncheck）
    PYTHONDONTWRITEBYTECODE=1 python3 -B tools/ci/prepush_check.py --range <基点>..<被推提交> --allow-part2-change

八项检查（互相独立，一项红其余照查，最后汇总；有红项退出码 1，用法／环境错误退出码 2，只有绿和跳过退出码 0；``--strict`` 把跳过也算失败）：

* ``worktree-state`` 工作树状态：HEAD 必须就是被推送的提交（后面几项读的是工作树）、没有未提交改动、没有落后基点（落后会被远端拒绝）、``tools`` 下没有
  ``__pycache__`` 或 ``.pyc``（门禁前置检查按文件系统查，有就 exit 9）。
* ``commit-hygiene`` 提交说明与目录卫生：被推提交说明里不能有 ``[skip ci]`` 一类字面量（GitHub 在说明任意位置匹配到就整次推送都不跑工作流，09-23 出过事）；
  ``docs/egress/maintenance`` 下不能放 ``.md``（Go 冻结门禁要求为零）。
* ``freeze-successor`` 冻结承接：以“每个冻结路径的当前摘要是否已有承接收据”为真值（与后台验证起跑检查、门禁里的冻结测试同一张承接图）；范围里还没有收据时再跑
  ``freeze-successor-generate --dry-run`` 看命中与注册表待办，没触及冻结路径就写“未触及”；范围里已含收据（源码与收据一起推）时 dry-run 不适用（工具会按自引用拒绝），只用覆盖复算。
  未承接时给出带完整提交号和绝对路径的生成命令（收据必须与源码分开提交）。
* ``guide-part2-digest`` 指南第二部分摘要：``source_spec_section_sha256(指南, "第二章")`` 必须等于登记值（取自基点上最新一份 ``codex_upgrade_scenarios_*.json`` 的
  ``source_spec.sha256``，不手抄常量）。第二部分是冻结章，各版场景清单与规则清单钉着它的摘要，改一个字节就让十来个单元红；``--allow-part2-change`` 用于本轮就是在做
  第二章摘要连锁的情况，放行“与基点不同”，但仍要求登记值已同步到新摘要、且没有文件还留着旧摘要。
* ``frontend-audit`` 前端审计：本地 ``pnpm audit --prod --audit-level=high --json`` 再跑 ``tools/check_pnpm_audit_exceptions.py``（与 Security Scan 同一套）。审计结果写系统临时目录，
  不碰受跟踪的 ``frontend/audit.json``。联网失败记“跳过”，不当成通过。
* ``govulncheck`` 在 ``backend`` 下跑 ``govulncheck ./...``（只拦代码调用到的漏洞，退出码 3）；另核四个工作流里 ``go version | grep`` 的版本与 ``backend/go.mod`` 一致。
* ``local-receipt`` 被推提交（tip）的本机完整 ``make test-capture-tools`` 收据：在收据目录里找 ``capture-tools-<完整提交号>.json``，用 ``opening_checks.check_capture_receipt`` 核验
  （提交号一致、命令是完整目标、退出码 0、工作树干净、耗时合理、封口对得上）。收据目录依次取 ``--receipt-dir``、环境变量 ``LOCAL_RECEIPT_DIR``、主检出的
  ``local-analysis/local-receipts``。收据另要上传 ARM64 才会被起跑检查看到——本脚本不连任何远端，这一步不核。
* ``env-paths`` 环境变量与路径：``CAPTURE_TYPESCRIPT_MODULE``（绝对路径、普通文件、摘要等于 Makefile 里的 ``CAPTURE_TYPESCRIPT_SHA256``）、``CODEX_SPEC_SOURCE_ROOT``
  （绝对路径、目录、有 ``codex-rs/Cargo.lock``、版本目录名等于 Makefile 的 ``CODEX_SPEC_SOURCE_VERSION``）、``LOCAL_RECEIPT_DIR`` 若设了必须是绝对路径。能自动补的（主检出里有现成的）
  把应当 export 的值打印出来；不能补的报红。

只读：不改任何受跟踪文件，不写工作树（审计与摘要计算的中间文件都在系统临时目录，用完即删）；所有 git 调用带 ``GIT_OPTIONAL_LOCKS=0``，不刷新索引。
某项依赖的工具或网络不可用时报“跳过：原因”，绝不静默当作通过。范围为空（HEAD 等于基点）时，只有“收据”这类只对被推提交有意义的项会跳过。
没有慢检查（``--fast``）时通常几秒内完成；带联网的两项并行跑，整体用时取决于较慢的那个。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.dont_write_bytecode = True

SCHEMA = "prepush-check/v1"
HERE = Path(__file__).resolve().parent
EXIT_OK, EXIT_RED, EXIT_USAGE = 0, 1, 2

PASS, FAIL, SKIP = "绿", "红", "跳过"
GUIDE_PATH = "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md"
GUIDE_FRAGMENT = "第二章"
SCENARIOS_DIR = "tools/official_client_capture"
_SCENARIOS_NAME = re.compile(r"codex_upgrade_scenarios_(\d+)_(\d+)_(\d+)\.json")
MAINTENANCE_DIR = "docs/egress/maintenance"
AUDIT_EXCEPTIONS = ".github/audit-exceptions.yml"
AUDIT_CHECKER = "tools/check_pnpm_audit_exceptions.py"
RECEIPT_SCRIPT = "tools/arm64_capture_driver/driver/local/local-capture-tools-receipt.sh"
DEFAULT_RECEIPT_SUBDIR = "local-analysis/local-receipts"
# GitHub 在提交说明任意位置匹配到这些字面量就跳过整次推送的全部工作流（09-23 出过事）。
_SKIP_CI = re.compile(r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]|skip-checks:\s*true", re.IGNORECASE)
_SAFE_TAG = re.compile(r"[^A-Za-z0-9._-]+")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")

# 检查项的固定顺序与说明：--list 与输出顺序都按它；第三个字段标明是否联网（--fast 会跳过）。
CHECK_TABLE: tuple[tuple[str, str, bool], ...] = (
    ("worktree-state", "工作树状态：HEAD 就是被推送的提交、无未提交改动、未落后基点、tools 下无 __pycache__／.pyc", False),
    ("commit-hygiene", "提交说明无 [skip ci] 类字面量；docs/egress/maintenance 下无 .md", False),
    ("freeze-successor", "冻结承接：范围触及冻结路径时跑 freeze-successor-generate --dry-run 并复算承接收据覆盖", False),
    ("guide-part2-digest", "指南第二部分（第二章）摘要等于登记值（--allow-part2-change 放行连锁）", False),
    ("frontend-audit", "前端审计与例外检查（pnpm audit＋check_pnpm_audit_exceptions.py，联网）", True),
    ("govulncheck", "govulncheck ./...（只拦被调用到的漏洞，联网）＋工作流 go 版本与 go.mod 一致", True),
    ("local-receipt", "被推提交的本机完整 make test-capture-tools 收据存在且绿", False),
    ("env-paths", "CAPTURE_TYPESCRIPT_MODULE／CODEX_SPEC_SOURCE_ROOT／LOCAL_RECEIPT_DIR 等环境变量与路径", False),
)
CHECK_NAMES = tuple(name for name, _, _ in CHECK_TABLE)


class PrecheckError(RuntimeError):
    """用法或环境错误（不是检查没通过），退出码 2。"""


@dataclasses.dataclass
class Result:
    """一项检查的结论：status 是 绿／红／跳过；summary 一行（绿给证据，跳过给理由）；红项另有原因与修法。"""

    name: str
    status: str
    summary: str
    reasons: list[str] = dataclasses.field(default_factory=list)
    fixes: list[str] = dataclasses.field(default_factory=list)
    seconds: float = 0.0


def green(name: str, summary: str) -> Result:
    return Result(name, PASS, summary)


def red(name: str, summary: str, reasons: Sequence[str] = (), fixes: Sequence[str] = ()) -> Result:
    return Result(name, FAIL, summary, list(reasons), list(fixes))


def skipped(name: str, reason: str) -> Result:
    return Result(name, SKIP, reason)


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def run_process(argv: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str] | None = None) -> "subprocess.CompletedProcess[str]":
    """跑一个子进程并收集输出。找不到可执行文件记 127、超时记 124，都不抛异常（由调用方决定是红还是跳过）。

    固定带 ``PYTHONDONTWRITEBYTECODE=1``（工作树里不留 ``__pycache__``）与 ``GIT_OPTIONAL_LOCKS=0``（git status 不刷新索引，保持只读）。
    """

    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0", **(env or {})}
    try:
        return subprocess.run(list(argv), cwd=str(cwd), env=environment, capture_output=True, text=True, errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(list(argv), 127, "", f"找不到可执行文件：{error}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"超时（{timeout:.0f} 秒）")


@dataclasses.dataclass
class Options:
    allow_part2_change: bool = False
    allow_dirty: bool = False
    fast: bool = False
    strict: bool = False
    receipt_dirs: list[Path] = dataclasses.field(default_factory=list)
    skip: dict[str, str] = dataclasses.field(default_factory=dict)
    only: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Context:
    """一次预检共用的事实：仓库、范围（基点／合并基／被推提交）、环境变量、选项与可注入的子进程执行器。"""

    repo: Path
    main_root: Path | None
    base_ref: str
    tip_ref: str
    base: str
    before: str
    tip: str
    head: str
    behind: int
    commits: list[str]
    env: Mapping[str, str]
    options: Options
    run: Runner = run_process

    @property
    def tip_checked_out(self) -> bool:
        return self.head == self.tip

    def git(self, *args: str, check: bool = True, timeout: float = 120) -> "subprocess.CompletedProcess[str]":
        completed = self.run(["git", *args], cwd=self.repo, timeout=timeout)
        if check and completed.returncode != 0:
            raise PrecheckError(f"git {' '.join(args)} 失败（退出码 {completed.returncode}）：{(completed.stderr or completed.stdout).strip()[-400:]}")
        return completed

    def blob(self, rev: str, path: str) -> str | None:
        """读某个提交里的文件内容；不存在记 None。"""

        completed = self.git("show", f"{rev}:{path}", check=False)
        return completed.stdout if completed.returncode == 0 else None


def short(sha: str) -> str:
    return sha[:12]


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:]


def tip_checkout_skip(ctx: Context, name: str) -> Result | None:
    """依赖工作树内容的检查：HEAD 不是被推提交时读到的不是被推的内容，只能跳过并写明。"""

    if ctx.tip_checked_out:
        return None
    return skipped(name, f"工作树 HEAD（{short(ctx.head)}）不是被推送的提交（{short(ctx.tip)}），本项读工作树，请在被推提交的检出里运行")


def _sibling(name: str) -> Any:
    """按路径加载同目录的模块（background_preflight、opening_checks），不依赖 ``tools`` 包能否导入，也不写字节码。"""

    key = f"{name}_prepush_sibling"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise PrecheckError(f"找不到同目录的 {name}.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


# ---------------------------------------------------------------------------
# 1. 工作树状态
# ---------------------------------------------------------------------------


def _pycache_residue(tools_dir: Path, limit: int = 5) -> tuple[int, list[str]]:
    """数 ``tools`` 下的 ``__pycache__`` 目录与 ``.pyc`` 文件（门禁前置检查按文件系统查，.gitignore 忽略了 git status 也看不到）。"""

    count, shown = 0, []
    for current, directories, files in os.walk(tools_dir):
        for name in directories:
            if name == "__pycache__":
                count += 1
                if len(shown) < limit:
                    shown.append(str(Path(current, name)))
        for name in files:
            if name.endswith(".pyc"):
                count += 1
                if len(shown) < limit:
                    shown.append(str(Path(current, name)))
    return count, shown


def check_worktree_state(ctx: Context) -> Result:
    name = "worktree-state"
    reasons: list[str] = []
    fixes: list[str] = []
    if not ctx.tip_checked_out:
        reasons.append(f"HEAD 是 {short(ctx.head)}，被推送的提交是 {short(ctx.tip)}：frontend-audit、govulncheck、冻结与环境检查读的是工作树，看的不是被推送的内容")
        fixes.append(f"在被推提交的检出里运行（git switch --detach {ctx.tip}）")
    if ctx.behind > 0:
        reasons.append(f"基点 {ctx.base_ref}（{short(ctx.base)}）有 {ctx.behind} 个提交不在被推提交里：直接推送会被远端拒绝（non-fast-forward）")
        fixes.append(f"先 git fetch origin，再把分支 rebase 或快进到 {ctx.base_ref} 之后重新预检；若冻结收据钉着旧基点号，要在 rebase 后重新生成")
    status = ctx.git("status", "--porcelain", "--untracked-files=all", check=False)
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty and not ctx.options.allow_dirty:
        reasons.append(f"工作树有 {len(dirty)} 处未提交或未跟踪的改动（如 {'；'.join(item.strip() for item in dirty[:3])}）：检查读的是工作树，推送的却是提交，两者会不一致")
        fixes.append("先提交或 git stash；确实不相干时加 --allow-dirty")
    count, shown = _pycache_residue(ctx.repo / "tools")
    if count:
        reasons.append(f"tools 下有 {count} 处 __pycache__／.pyc（如 {'；'.join(shown[:3])}）：门禁前置检查按文件系统查，会 exit 9，并拖慢字节码共享层")
        fixes.append("按字面路径删掉这些缓存（find tools -name __pycache__ -type d 列出再逐个删）；以后在工作树里的每条 python 命令都带 PYTHONDONTWRITEBYTECODE=1")
    if reasons:
        return red(name, "工作树状态不适合直接推送", reasons, fixes)
    notes = "，工作树干净" if not dirty else f"，有 {len(dirty)} 处未提交改动（已按 --allow-dirty 放行）"
    return green(name, f"HEAD 就是被推提交 {short(ctx.tip)}，未落后 {ctx.base_ref}{notes}，tools 下无 __pycache__／.pyc")


# ---------------------------------------------------------------------------
# 2. 提交说明与目录卫生
# ---------------------------------------------------------------------------


def check_commit_hygiene(ctx: Context) -> Result:
    name = "commit-hygiene"
    reasons: list[str] = []
    fixes: list[str] = []
    for commit in ctx.commits:
        message = ctx.git("show", "-s", "--format=%B", commit, check=False).stdout
        hit = _SKIP_CI.search(message)
        if hit:
            reasons.append(f"提交 {short(commit)} 的说明里有 “{hit.group(0)}”：GitHub 在说明任意位置匹配到就跳过整次推送的全部工作流（引用别的提交也算）")
            fixes.append("改写该提交的说明去掉这个字面量（未推送前可 git rebase 或 commit --amend）")
    listing = ctx.git("ls-tree", "-r", "--name-only", ctx.tip, "--", MAINTENANCE_DIR, check=False).stdout.splitlines()
    markdown = [path for path in listing if path.endswith(".md")]
    if markdown:
        reasons.append(f"{MAINTENANCE_DIR} 下有 {len(markdown)} 个 .md（{'；'.join(markdown[:3])}）：Go 冻结门禁要求该目录 .md 为零")
        fixes.append("报告类文档放桌面或 local-analysis，不放 maintenance 目录")
    if reasons:
        return red(name, "提交说明或目录不合规", reasons, fixes)
    return green(name, f"{len(ctx.commits)} 个被推提交的说明里没有 skip-ci 类字面量；{MAINTENANCE_DIR} 下无 .md")


# ---------------------------------------------------------------------------
# 3. 冻结承接
# ---------------------------------------------------------------------------


def _generate_command(ctx: Context) -> str:
    """给出带完整提交号和绝对输出路径的收据生成命令（--output 必须绝对路径，--before／--after 必须完整 40 位）。"""

    branch = ctx.git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip() or "prepush"
    tag = _SAFE_TAG.sub("-", branch).strip("-") or "prepush"
    output = ctx.repo / MAINTENANCE_DIR / f"{tag}-freeze-successor.json"
    return (f"PYTHONDONTWRITEBYTECODE=1 python3 -B -m tools.upstream_merge freeze-successor-generate --before {ctx.before} --after {ctx.tip} "
            f"--tag {tag} --output {output}")


def _receipts_in_range(ctx: Context) -> list[str]:
    """范围内新增或修改的冻结台账收据（``docs/egress/maintenance/*.json``）。"""

    changed = ctx.git("diff", "--name-only", "--diff-filter=AM", ctx.before, ctx.tip, "--", MAINTENANCE_DIR, check=False).stdout.split()
    return [path for path in changed if path.endswith(".json")]


def check_freeze_successor(ctx: Context) -> Result:
    """冻结承接。以“每个冻结路径当前内容是否已有承接收据”为准（与门禁里 Go／Python 冻结测试同一张承接图，推上去会不会红的真值）；
    范围里还没有收据时再跑 ``freeze-successor-generate --dry-run`` 看命中与注册表待办。范围里已经含收据（源码与收据一起推）时 dry-run 不适用：
    工具会按“收据在本区间内变化，存在自引用风险”拒绝，所以只用覆盖复算。"""

    name = "freeze-successor"
    if not ctx.commits:
        return green(name, "未触及冻结范围（范围内没有提交，没有改动路径）")
    early = tip_checkout_skip(ctx, name)
    if early:
        return early
    coverage = _sibling("background_preflight").check_freeze_receipts(ctx.repo, runner=ctx.run)
    uncovered = list(coverage.get("detail", {}).get("uncovered", []))
    registered = coverage.get("detail", {}).get("registered")
    if not coverage.get("passed") and not uncovered:
        return red(name, "冻结覆盖算不出来", list(coverage.get("problems", [])), ["先单独跑 tools.upstream_merge.freeze.load_frozen_edges 看台账读取为什么失败"])
    reasons: list[str] = []
    fixes: list[str] = []
    hits: list[str] = []
    receipts = _receipts_in_range(ctx)
    plan: dict[str, Any] = {}
    if not receipts:
        dry = ctx.run([sys.executable, "-B", "-m", "tools.upstream_merge", "freeze-successor-generate", "--dry-run", "--before", ctx.before,
                       "--after", ctx.tip, "--tag", "prepush", "--repository", str(ctx.repo)], cwd=ctx.repo, timeout=180,
                      env={"PYTHONPATH": str(ctx.repo)})
        if dry.returncode == 127:
            return skipped(name, f"跑不起 tools.upstream_merge：{_tail(dry.stderr)}")
        if dry.returncode != 0:
            message = _tail(dry.stderr or dry.stdout)
            reasons.append(f"freeze-successor-generate --dry-run 退出码 {dry.returncode}：{message}")
            fixes.append(f"区间内删除了冻结路径时补 --deletion-reason（及 --historical-reader）：{_generate_command(ctx)}" if "deletion-reason" in message
                         else "按提示处理后重跑；前序摘要不在已登记收据里（链断裂）时不要凭空补边，先查是谁改了冻结文件却没承接")
        else:
            try:
                plan = json.loads(dry.stdout)
            except ValueError:
                reasons.append(f"freeze-successor-generate --dry-run 的输出不是 JSON：{_tail(dry.stdout, 200)}")
                fixes.append("先单独跑同一条命令看工具报什么")
        hits = [item["path"] for item in plan.get("transitions", [])]
        for action in plan.get("required_manual_actions", [])[:5]:
            reasons.append(f"注册表规则 {action.get('rule_id')} 命中 {action.get('path')}：{action.get('instruction')}")
        if plan.get("required_manual_actions"):
            fixes.append("按规则要求的手工动作处理后，用 freeze-successor-generate 生成收据")
        if plan.get("deleted_frozen_paths"):
            reasons.append(f"区间内删除了冻结路径：{'；'.join(plan['deleted_frozen_paths'][:5])}")
            fixes.append(f"生成收据时补 --deletion-reason（Python 模块再补 --historical-reader）：{_generate_command(ctx)}")
    if uncovered:
        shown = "；".join(uncovered[:8]) + (f" 等共 {len(uncovered)} 个" if len(uncovered) > 8 else "")
        reasons.append(f"{len(uncovered)} 个冻结路径的当前内容没有承接收据登记：{shown}（冻结测试必红）")
        fixes.append("生成承接收据并【单独提交】（源码与收据不能同一提交，否则留冻结缺口）：" + _generate_command(ctx)
                     + ("（若工具以自引用拒绝，把 --after 换成最后一个源码提交）" if receipts else ""))
    if reasons:
        return red(name, f"范围命中 {len(hits)} 个冻结路径，承接不完整" if hits else "冻结承接不完整", reasons, fixes)
    if receipts:
        return green(name, f"范围内已含 {len(receipts)} 个承接收据文件，dry-run 不适用（工具会按自引用拒绝）；全部 {registered} 个冻结路径的当前摘要均已有承接收据登记")
    if not hits:
        return green(name, f"未触及冻结范围（{plan.get('changed_path_count', 0)} 个改动路径，0 个冻结命中，dry-run 结论 {plan.get('result')}）")
    return green(name, f"范围命中 {len(hits)} 个冻结路径（{'；'.join(hits[:3])}{'…' if len(hits) > 3 else ''}），当前摘要均已有承接收据（登记 {registered} 个冻结路径）")


# ---------------------------------------------------------------------------
# 4. 指南第二部分摘要
# ---------------------------------------------------------------------------

# 在被检仓库里用它自己的函数算摘要（权威实现，不在这里重抄一遍算法）。
_DIGEST_SNIPPET = """\
import pathlib, sys
sys.dont_write_bytecode = True
from tools.official_client_capture.candidate_rule_assertion import source_spec_section_sha256
print(source_spec_section_sha256(pathlib.Path(sys.argv[1]), sys.argv[2]))
"""


def latest_registered_digest(ctx: Context, rev: str) -> tuple[str, str] | None:
    """某个提交里最新一份 ``codex_upgrade_scenarios_*.json`` 登记的第二章摘要：（文件路径，摘要）；没有这类文件记 None。"""

    listing = ctx.git("ls-tree", "--name-only", rev, f"{SCENARIOS_DIR}/", check=False).stdout.splitlines()
    versions = []
    for path in listing:
        matched = _SCENARIOS_NAME.fullmatch(Path(path).name)
        if matched:
            versions.append((tuple(int(part) for part in matched.groups()), path))
    if not versions:
        return None
    path = max(versions)[1]
    text = ctx.blob(rev, path)
    try:
        spec = json.loads(text or "")["source_spec"]
    except (ValueError, KeyError, TypeError):
        raise PrecheckError(f"{rev[:12]}:{path} 不是带 source_spec 的清单 JSON，无法读取第二章登记摘要")
    if not isinstance(spec, dict) or spec.get("path") != GUIDE_PATH or spec.get("fragment") != GUIDE_FRAGMENT or not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("sha256"))):
        raise PrecheckError(f"{rev[:12]}:{path} 的 source_spec 不是指南第二章的登记（{spec!r}）")
    return path, spec["sha256"]


def compute_guide_digest(ctx: Context, rev: str) -> str | None:
    """被推提交里指南第二章的摘要（用被检仓库自己的 ``source_spec_section_sha256``）；指南不存在记 None。"""

    if ctx.git("cat-file", "-e", f"{rev}:{GUIDE_PATH}", check=False).returncode != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="prepush-guide-") as directory:
        guide = Path(directory) / "guide.md"
        # 按字节取出（不经文本解码），保证摘要和提交里的文件逐字节一致。
        raw = subprocess.run(["git", "show", f"{rev}:{GUIDE_PATH}"], cwd=ctx.repo, capture_output=True, check=False,
                             env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if raw.returncode != 0:
            raise PrecheckError(f"读不出 {rev[:12]}:{GUIDE_PATH}：{_tail(raw.stderr.decode('utf-8', 'replace'))}")
        guide.write_bytes(raw.stdout)
        completed = ctx.run([sys.executable, "-B", "-c", _DIGEST_SNIPPET, str(guide), GUIDE_FRAGMENT], cwd=ctx.repo, timeout=120,
                            env={"PYTHONPATH": str(ctx.repo)})
    digest = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PrecheckError(f"算不出指南第二章摘要（退出码 {completed.returncode}）：{_tail(completed.stderr or completed.stdout)}")
    return digest


def check_guide_part2(ctx: Context) -> Result:
    name = "guide-part2-digest"
    actual = compute_guide_digest(ctx, ctx.tip)
    base_entry = latest_registered_digest(ctx, ctx.before)
    tip_entry = latest_registered_digest(ctx, ctx.tip)
    if actual is None:
        return red(name, "被推提交里没有指南", [f"{short(ctx.tip)} 里找不到 {GUIDE_PATH}"], ["指南是冻结文件，不能删除；恢复它"])
    if tip_entry is None:
        # 基点里有而被推提交里没有，也是红：不能让“登记处被删掉”这种改动靠回退到基点值而通过。
        return red(name, "找不到登记第二章摘要的清单" if base_entry is None else "被推提交里登记第二章摘要的清单不见了",
                   [f"{SCENARIOS_DIR} 下没有 codex_upgrade_scenarios_*.json（基点{'也' if base_entry is None else '里'}{'没有' if base_entry is None else '有 ' + Path(base_entry[0]).name}）"],
                   ["恢复场景清单；它们是第二章摘要的登记处，各版清单与规则清单都钉着它"])
    expected = (base_entry or tip_entry)[1]
    expected_tip = tip_entry[1]
    if actual == expected and expected_tip == expected:
        where = (base_entry or tip_entry)[0]
        return green(name, f"第二章摘要 {expected[:12]}… 与登记值一致（登记处 {Path(where).name}，基点 {short(ctx.before)}）")
    detail = [f"被推提交的第二章摘要：{actual}", f"基点登记值（{Path((base_entry or tip_entry)[0]).name}）：{expected}", f"被推提交里的登记值：{expected_tip}"]
    if actual == expected and expected_tip != expected:
        return red(name, "登记值被改了，指南第二章却没变", detail,
                   ["清单里的 source_spec.sha256 与指南第二章必须同源；撤销对登记值的改动，或同步做完第二章摘要连锁"])
    if not ctx.options.allow_part2_change:
        guide_diff = f"git diff {short(ctx.before)} {short(ctx.tip)} -- {GUIDE_PATH}"
        return red(name, f"指南第二部分变了：{expected[:12]}… → {actual[:12]}…", detail,
                   [f"第二部分是冻结章，各版场景清单、规则清单和工具常量钉着它的摘要，改了 capture-tools 十几个单元红（10-30 白跑约 25 分钟）；用 {guide_diff} 看改了什么，"
                    "把改动移出第二部分：新结论写第三部分 3.9「当前实施状态与兼容边界」末尾或第四部分，第二部分恢复原样",
                    "若本轮确实就是在做第二章摘要连锁（先例 563d6a30a0：约 23 个文件各一行摘要替换，加驱动 manifest 重建、事实映射重绑、身份基线重封、收据），加 --allow-part2-change，预检会改查连锁是否做全"])
    # 放行连锁：登记值必须已同步到新摘要，且不能还有文件留着旧摘要。
    problems = []
    if expected_tip != actual:
        problems.append(f"登记值还是 {expected_tip[:12]}…，没有同步到新摘要 {actual[:12]}…")
    leftover = ctx.git("grep", "-l", "-F", expected, ctx.tip, "--", "tools", check=False).stdout.splitlines()
    leftover = [item.split(":", 1)[-1] for item in leftover]
    if leftover:
        problems.append(f"还有 {len(leftover)} 个文件留着旧摘要 {expected[:12]}…（{'；'.join(leftover[:4])}{'…' if len(leftover) > 4 else ''}）")
    if problems:
        return red(name, "第二章摘要连锁没做全", [*detail, *problems], ["把所有登记了第二章摘要的文件一并换成新摘要，再补事实映射重绑／身份基线重封／冻结承接收据"])
    return green(name, f"第二章摘要已变（{expected[:12]}… → {actual[:12]}…），按 --allow-part2-change 放行：登记值已同步，无文件残留旧摘要")


# ---------------------------------------------------------------------------
# 5. 前端审计
# ---------------------------------------------------------------------------


def _parse_exceptions(text: str) -> list[dict[str, str]]:
    """轻量解析 ``audit-exceptions.yml``（每条以 ``- package:`` 开头的扁平键值，与检查器自己的解析口径一致）。"""

    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            if current:
                entries.append(current)
            current, line = {}, line[2:].strip()
        matched = re.match(r"(\w+):\s*[\"']?(.*?)[\"']?\s*$", line)
        if matched:
            current[matched.group(1)] = matched.group(2)
    if current:
        entries.append(current)
    return entries


def _high_advisories(audit: Mapping[str, Any]) -> set[tuple[str, str]]:
    """审计结果里高危以上公告的（包名，公告号）集合（公告号小写，与检查器的匹配口径一致）。"""

    found: set[tuple[str, str]] = set()
    advisories = audit.get("advisories")
    if isinstance(advisories, dict):
        for item in advisories.values():
            if isinstance(item, dict) and str(item.get("severity", "")).lower() in ("high", "critical"):
                identity = item.get("github_advisory_id") or item.get("url") or item.get("id")
                found.add((str(item.get("module_name") or item.get("name") or "").strip(), str(identity).strip().lower()))
    return found


def _used_exception_expiry(entries: Sequence[Mapping[str, str]], used: set[tuple[str, str]]) -> date | None:
    """正在被当前公告用到的例外里最早到期的日期（没有用到的例外不算，过期的无关例外不会让检查器报错）。"""

    dates = []
    for entry in entries:
        if (entry.get("package", "").strip(), entry.get("advisory", "").strip().lower()) in used:
            try:
                dates.append(date.fromisoformat(entry.get("expires_on", "")))
            except ValueError:
                continue
    return min(dates) if dates else None


def check_frontend_audit(ctx: Context) -> Result:
    name = "frontend-audit"
    if ctx.options.fast:
        return skipped(name, "按 --fast 跳过（联网检查）")
    early = tip_checkout_skip(ctx, name)
    if early:
        return early
    frontend, checker, exceptions = ctx.repo / "frontend", ctx.repo / AUDIT_CHECKER, ctx.repo / AUDIT_EXCEPTIONS
    for path in (frontend / "pnpm-lock.yaml", checker, exceptions):
        if not path.is_file():
            return skipped(name, f"缺少 {path.relative_to(ctx.repo)}，没有可审计的前端")
    if shutil.which("pnpm", path=ctx.env.get("PATH")) is None:
        return skipped(name, "本机没有 pnpm，没法跑前端审计（npm i -g pnpm@9 或用 corepack 启用）")
    with tempfile.TemporaryDirectory(prefix="prepush-audit-") as directory:
        audit_file = Path(directory) / "audit.json"
        completed = ctx.run(["pnpm", "audit", "--prod", "--audit-level=high", "--json"], cwd=frontend, timeout=180, env={"PATH": ctx.env.get("PATH", "")})
        audit_file.write_text(completed.stdout, encoding="utf-8")
        try:
            audit = json.loads(completed.stdout)
        except ValueError:
            audit = None
        if completed.returncode in (124, 127) or not isinstance(audit, dict) or ("advisories" not in audit and "vulnerabilities" not in audit and "metadata" not in audit):
            error = audit.get("error") if isinstance(audit, dict) else None
            detail = f"{error.get('code')}：{error.get('message')}" if isinstance(error, dict) else _tail(completed.stderr or completed.stdout, 200)
            return skipped(name, f"pnpm audit 没拿到有效审计结果（多半联网失败或审计端点变化）：{detail}")
        checked = ctx.run([sys.executable, "-B", str(checker), "--audit", str(audit_file), "--exceptions", str(exceptions)], cwd=ctx.repo, timeout=60)
    entries = _parse_exceptions(exceptions.read_text(encoding="utf-8"))
    used = _high_advisories(audit)
    nearest = _used_exception_expiry(entries, used)
    if checked.returncode != 0:
        output = [line for line in (checked.stdout + "\n" + checked.stderr).splitlines() if line.strip()]
        return red(name, "前端审计有未覆盖的高危公告或例外已过期", output[:8],
                   ["有现成修复版本就升依赖或加 pnpm override（先看上游有没有已修的提交）；确实无修复且不可达时，在 .github/audit-exceptions.yml 登记例外并写明缓解理由与到期日",
                    "这些路径（frontend/package.json、锁文件、audit-exceptions.yml）在冻结台账里，改完要另提交 freeze-successor 收据",
                    "本机前端 OpsDashboardHeader.timezone.spec.ts 3 例必红，与此无关"])
    tail = f"，最早到期的是 {nearest.isoformat()}（{(nearest - date.today()).days} 天后）" if nearest else ""
    return green(name, f"pnpm audit 高危以上 {len(used)} 个公告，均有未过期的例外（清单共 {len(entries)} 条{tail}）；Audit exceptions validated"
                 if used else f"pnpm audit 没有高危以上公告（例外清单共 {len(entries)} 条）；Audit exceptions validated")


# ---------------------------------------------------------------------------
# 6. govulncheck
# ---------------------------------------------------------------------------


def _go_mod_version(text: str) -> str | None:
    matched = re.search(r"(?m)^go\s+(\d+\.\d+\.\d+)\s*$", text)
    return matched.group(1) if matched else None


def _workflow_go_versions(repo: Path) -> dict[str, set[str]]:
    versions: dict[str, set[str]] = {}
    for workflow in sorted((repo / ".github" / "workflows").glob("*.yml")):
        found = set(re.findall(r"go version \| grep -q 'go(\d+\.\d+\.\d+)'", workflow.read_text(encoding="utf-8")))
        if found:
            versions[workflow.name] = found
    return versions


def check_govulncheck(ctx: Context) -> Result:
    name = "govulncheck"
    if ctx.options.fast:
        return skipped(name, "按 --fast 跳过（联网检查）")
    early = tip_checkout_skip(ctx, name)
    if early:
        return early
    backend = ctx.repo / "backend"
    go_mod = backend / "go.mod"
    if not go_mod.is_file():
        return skipped(name, "没有 backend/go.mod")
    # 工作流里 go version | grep 的版本要与 go.mod 一致（升 Go 时 Security Scan 的 Verify Go version 一步会因此红，四个工作流要一起改）。
    required = _go_mod_version(go_mod.read_text(encoding="utf-8"))
    mismatched = [f"{file}（{'、'.join(sorted(found))}）" for file, found in _workflow_go_versions(ctx.repo).items() if required and found != {required}]
    if mismatched:
        return red(name, "工作流里的 Go 版本校验与 go.mod 不一致", [f"backend/go.mod 要求 go {required}，这些工作流校验的是别的版本：{'；'.join(mismatched)}"],
                   ["同步四个工作流（backend-ci.yml 两处、release.yml、security-scan.yml）的 go version | grep 与三个 Dockerfile 的 GOLANG_IMAGE；这些是冻结文件，另提交承接收据"])
    try:
        home_bin = str(Path.home() / "go" / "bin")   # go install 的默认落点；HOME 取不到（精简沙箱）时就只用 PATH
    except RuntimeError:
        home_bin = ""
    path_value = os.pathsep.join(filter(None, [ctx.env.get("PATH"), home_bin]))
    binary = shutil.which("govulncheck", path=path_value)
    if binary is None:
        return skipped(name, "本机没有 govulncheck（go install golang.org/x/vuln/cmd/govulncheck@latest）")
    if shutil.which("go", path=path_value) is None:
        return skipped(name, "本机没有 go")
    nice = shutil.which("nice")
    argv = ([nice, "-n", "10"] if nice else []) + [binary, "./..."]
    completed = ctx.run(argv, cwd=backend, timeout=420, env={"PATH": path_value})
    text = completed.stdout + "\n" + completed.stderr
    flat = " ".join(text.split())
    if completed.returncode == 0:
        called = re.search(r"Your code is affected by (\d+) vulnerabilit", flat)
        module_only = re.search(r"and (\d+) vulnerabilities in modules you require", flat)
        called_text = f"代码调用到 {called.group(1)} 个漏洞" if called else "没有发现漏洞"
        extra = f"，依赖层 {module_only.group(1)} 个未被调用的公告不拦" if module_only else ""
        gate = f"；工作流 go 版本与 go.mod 一致（{required}）" if required and _workflow_go_versions(ctx.repo) else ""
        return green(name, f"govulncheck ./...：{called_text}{extra}{gate}")
    if completed.returncode == 3:
        found = re.findall(r"Vulnerability #\d+: (GO-\d+-\d+)", text)
        fixed = re.findall(r"Fixed in: (\S+)", text)
        reasons = [f"{vuln}（修复版本 {version}）" for vuln, version in zip(found, fixed + [""] * len(found))] or [_tail(text, 300)]
        return red(name, f"govulncheck 报出 {len(found) or '若干'} 个被代码调用到的漏洞", reasons,
                   ["看上游有没有现成修复（标准库漏洞通常升 Go 小版本：同步 go.mod 的 go 指令、四个工作流的版本校验、三个 Dockerfile）；x/net 等依赖漏洞升对应模块并 go mod tidy",
                    "这类公告 1～2 天就会让 Security Scan 变红，推 main 前务必本机先跑一遍"])
    return skipped(name, f"govulncheck 没跑出结论（退出码 {completed.returncode}，多半联网失败或工具链取不到）：{_tail(completed.stderr or completed.stdout, 200)}")


# ---------------------------------------------------------------------------
# 7. 本机收据
# ---------------------------------------------------------------------------


def receipt_dirs(ctx: Context) -> list[Path]:
    """收据目录的查找顺序：--receipt-dir（可多个）、环境变量 LOCAL_RECEIPT_DIR、主检出的 local-analysis/local-receipts。"""

    found = list(ctx.options.receipt_dirs)
    if ctx.env.get("LOCAL_RECEIPT_DIR"):
        found.append(Path(ctx.env["LOCAL_RECEIPT_DIR"]))
    if ctx.main_root is not None:
        found.append(ctx.main_root / DEFAULT_RECEIPT_SUBDIR)
    return found


def check_local_receipt(ctx: Context) -> Result:
    name = "local-receipt"
    if not ctx.commits:
        return skipped(name, "范围内没有提交（HEAD 等于基点），没有要推送的提交，不需要收据")
    checks = _sibling("opening_checks")
    directories = receipt_dirs(ctx)
    how = (f"bash {RECEIPT_SCRIPT} --out-dir {directories[0] if directories else '<收据目录>'}"
           "（在被推提交的干净检出里跑，完整 make test-capture-tools 本机约 20～48 分钟，走重负载锁；红的也会如实写）")
    for directory in directories:
        if not checks.capture_receipt_path(directory, ctx.tip).exists():
            continue
        problems = checks.check_capture_receipt(directory, ctx.tip)
        if problems:
            return red(name, f"收据 {checks.capture_receipt_path(directory, ctx.tip).name} 不合格", problems, [f"修好后对被推提交重跑：{how}"])
        receipt = json.loads(checks.capture_receipt_path(directory, ctx.tip).read_text(encoding="utf-8"))
        return green(name, f"收据 {checks.capture_receipt_path(directory, ctx.tip).name} 绿：退出码 {receipt.get('exit_code')}，耗时 {receipt.get('duration_seconds')} 秒，"
                           f"主机 {receipt.get('host')}，工作树干净，封口吻合（在 {directory}；是否已上传 ARM64 未核）")
    searched = "；".join(str(item) for item in directories) or "（没有可查的目录：不在主检出的 worktree 里，也没给 --receipt-dir）"
    return red(name, f"没有被推提交 {short(ctx.tip)} 的本机完整测试收据", [f"查过的收据目录：{searched}"],
               [f"生成收据：{how}", "收据还要上传 ARM64 的 $RUNROOT/local-receipts/ 才会被入口门禁／后台验证的起跑检查看到；本项不连远端，不核这一步",
                "只推送不进门禁时，可用 --skip local-receipt --skip-reason <理由> 显式跳过"])


# ---------------------------------------------------------------------------
# 8. 环境变量与路径
# ---------------------------------------------------------------------------


def parse_make_variable(makefile: str, name: str) -> str | None:
    matched = re.search(rf"(?m)^{re.escape(name)}\s*(?:\?=|:=|=)\s*(\S+)\s*$", makefile)
    return matched.group(1) if matched else None


def typescript_problem(raw: str, expected_sha: str) -> str | None:
    """与 Makefile 的 test-capture-tools-prerequisites 同一判据：绝对路径、普通文件（本身不能是符号链接）、摘要吻合。"""

    path = Path(raw)
    if not path.is_absolute():
        return f"不是绝对路径：{raw}"
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return f"文件不存在：{raw}"
    if path.is_symlink() or not resolved.is_file():
        return f"不是普通文件（或本身是符号链接）：{raw}"
    actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return None if actual == expected_sha else f"摘要不符（{actual[:12]}…，Makefile 要求 {expected_sha[:12]}…）：{raw}"


def spec_root_problem(raw: str, version: str) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        return f"不是绝对路径：{raw}"
    if not (path / "codex-rs" / "Cargo.lock").is_file():
        return f"目录不存在或缺 codex-rs/Cargo.lock：{raw}"
    if path.name != f"codex-cli-{version}":
        return f"版本目录名是 {path.name}，Makefile 的 CODEX_SPEC_SOURCE_VERSION 是 {version}（核对客户端行为必须用当前仿真版本的源码）：{raw}"
    return None


def check_env_paths(ctx: Context) -> Result:
    name = "env-paths"
    makefile_path = ctx.repo / "Makefile"
    if not makefile_path.is_file():
        return skipped(name, "仓库根没有 Makefile，没有可对照的变量定义")
    makefile = makefile_path.read_text(encoding="utf-8")
    expected_sha = parse_make_variable(makefile, "CAPTURE_TYPESCRIPT_SHA256")
    version = parse_make_variable(makefile, "CODEX_SPEC_SOURCE_VERSION")
    reasons: list[str] = []
    fixes: list[str] = []
    evidence: list[str] = []
    if expected_sha:
        configured = ctx.env.get("CAPTURE_TYPESCRIPT_MODULE")
        raw = configured or str(ctx.repo / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js")
        problem = typescript_problem(raw, expected_sha)
        if problem:
            reasons.append(f"CAPTURE_TYPESCRIPT_MODULE{'（环境变量）' if configured else '（未设置，Makefile 默认值）'}不可用：{problem}")
            candidate = str(ctx.main_root / "frontend" / "node_modules" / "typescript" / "lib" / "typescript.js") if ctx.main_root else None
            if candidate and typescript_problem(candidate, expected_sha) is None:
                fixes.append(f"export CAPTURE_TYPESCRIPT_MODULE={candidate}（主检出里有现成的，摘要吻合）")
            else:
                fixes.append("在主检出里 pnpm --dir frontend install --frozen-lockfile --offline 装好前端依赖，再设 CAPTURE_TYPESCRIPT_MODULE 为其中 typescript/lib/typescript.js 的绝对路径")
        else:
            evidence.append(f"CAPTURE_TYPESCRIPT_MODULE 可用（{'环境变量' if configured else '默认路径'}，摘要吻合）")
    if version:
        configured = ctx.env.get("CODEX_SPEC_SOURCE_ROOT") or ctx.env.get("CODEX_0_149_1_SOURCE_ROOT")
        raw = configured or str(ctx.repo / "local-analysis" / "sources" / f"codex-cli-{version}")
        problem = spec_root_problem(raw, version)
        if problem:
            reasons.append(f"CODEX_SPEC_SOURCE_ROOT{'（环境变量）' if configured else '（未设置，Makefile 默认值）'}不可用：{problem}")
            candidate = str(ctx.main_root / "local-analysis" / "sources" / f"codex-cli-{version}") if ctx.main_root else None
            if candidate and spec_root_problem(candidate, version) is None:
                fixes.append(f"export CODEX_SPEC_SOURCE_ROOT={candidate}（主检出里有现成的源码树；make check-egress-spec 的 local-source 子检查必须有它）")
            else:
                fixes.append(f"准备 codex-cli-{version} 源码树（只读）到主检出的 local-analysis/sources/，再设 CODEX_SPEC_SOURCE_ROOT 为它的绝对路径")
        else:
            evidence.append(f"CODEX_SPEC_SOURCE_ROOT 可用（codex-cli-{version}，有 Cargo.lock）")
    receipt_dir = ctx.env.get("LOCAL_RECEIPT_DIR")
    if receipt_dir:
        if not Path(receipt_dir).is_absolute():
            reasons.append(f"LOCAL_RECEIPT_DIR 不是绝对路径：{receipt_dir}")
            fixes.append("改成绝对路径（收据脚本会 cd 到仓库根，相对路径指向的位置会错）")
        else:
            evidence.append("LOCAL_RECEIPT_DIR 是绝对路径")
    if not expected_sha and not version:
        return skipped(name, "Makefile 里没有 CAPTURE_TYPESCRIPT_SHA256／CODEX_SPEC_SOURCE_VERSION 的定义，没有可核对的变量")
    if reasons:
        return red(name, "环境变量或路径不可用，相关门禁会在开头就红", reasons, fixes)
    return green(name, "；".join(evidence))


# ---------------------------------------------------------------------------
# 调度、输出
# ---------------------------------------------------------------------------

CHECK_FUNCTIONS: dict[str, Callable[[Context], Result]] = {
    "worktree-state": check_worktree_state,
    "commit-hygiene": check_commit_hygiene,
    "freeze-successor": check_freeze_successor,
    "guide-part2-digest": check_guide_part2,
    "frontend-audit": check_frontend_audit,
    "govulncheck": check_govulncheck,
    "local-receipt": check_local_receipt,
    "env-paths": check_env_paths,
}


def run_check(ctx: Context, name: str) -> Result:
    started = time.monotonic()
    if name in ctx.options.skip:
        result = skipped(name, f"按 --skip 显式跳过：{ctx.options.skip[name]}")
    else:
        try:
            result = CHECK_FUNCTIONS[name](ctx)
        except PrecheckError as error:
            result = red(name, "检查没法完成", [str(error)], ["按错误信息处理后重跑"])
        except Exception:  # noqa: BLE001 —— 预检自己出错必须失败关闭，不能当通过
            result = red(name, "预检脚本自身异常（失败关闭）", [_tail(traceback.format_exc(), 600)], ["把这段回溯连同命令发给维护者"])
    result.seconds = round(time.monotonic() - started, 1)
    return result


def run_checks(ctx: Context, names: Sequence[str], jobs: int = 4) -> list[Result]:
    """并行跑选中的检查（联网的两项互不依赖），结果按固定顺序返回。"""

    if jobs <= 1 or len(names) <= 1:
        return [run_check(ctx, name) for name in names]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(names))) as pool:
        futures = {name: pool.submit(run_check, ctx, name) for name in names}
        return [futures[name].result() for name in names]


def render(ctx: Context, results: Sequence[Result], elapsed: float) -> str:
    lines = [f"推送前预检（{SCHEMA}）",
             f"范围：{ctx.base_ref}（{short(ctx.base)}）..{ctx.tip_ref}（{short(ctx.tip)}），合并基 {short(ctx.before)}，共 {len(ctx.commits)} 个被推提交；仓库 {ctx.repo}"
             f"（{ctx.base_ref} 以本地跟踪分支为准，未 fetch）",
             f"完整提交号（选择器、--before／--after 都要完整 40 位，别手写）：基点 {ctx.base}，合并基 {ctx.before}，被推提交 {ctx.tip}"]
    for result in results:
        lines.append(f"[{result.status}] {result.name}  {result.summary}  ({result.seconds}s)")
        if result.status == FAIL:
            lines.extend(f"      原因：{reason}" for reason in result.reasons)
            lines.extend(f"      修法：{fix}" for fix in result.fixes)
    counts = {status: sum(1 for item in results if item.status == status) for status in (PASS, FAIL, SKIP)}
    lines.append(f"汇总：{counts[PASS]} 绿、{counts[FAIL]} 红、{counts[SKIP]} 跳过，用时 {elapsed:.1f} 秒")
    return "\n".join(lines)


def exit_code(results: Sequence[Result], strict: bool) -> int:
    if any(item.status == FAIL for item in results):
        return EXIT_RED
    if strict and any(item.status == SKIP for item in results):
        return EXIT_RED
    return EXIT_OK


def _git_text(repo: Path, *args: str, runner: Runner = run_process) -> str:
    completed = runner(["git", *args], cwd=repo, timeout=60)
    if completed.returncode != 0:
        raise PrecheckError(f"git {' '.join(args)} 失败：{_tail(completed.stderr or completed.stdout)}")
    return completed.stdout.strip()


def build_context(repo_arg: str | None, base_ref: str, tip_ref: str, options: Options, env: Mapping[str, str], runner: Runner = run_process,
                  fetch: bool = False) -> Context:
    start = Path(repo_arg).resolve() if repo_arg else Path.cwd()
    try:
        repo = Path(_git_text(start, "rev-parse", "--show-toplevel", runner=runner))
    except PrecheckError as error:
        raise PrecheckError(f"{start} 不在 git 仓库里：{error}") from error
    if fetch:
        fetched = runner(["git", "fetch", "origin"], cwd=repo, timeout=300)
        if fetched.returncode != 0:
            raise PrecheckError(f"git fetch origin 失败：{_tail(fetched.stderr)}")

    def resolve(ref: str, label: str) -> str:
        completed = runner(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo, timeout=60)
        value = completed.stdout.strip()
        if completed.returncode != 0 or not _FULL_SHA.fullmatch(value):
            raise PrecheckError(f"{label} {ref!r} 不是仓库里的提交（本地没有远端跟踪分支时先 git fetch origin，或用 --base 指定基点）")
        return value

    base, tip, head = resolve(base_ref, "基点"), resolve(tip_ref, "被推提交"), resolve("HEAD", "HEAD")
    before = _git_text(repo, "merge-base", base, tip, runner=runner)
    if not _FULL_SHA.fullmatch(before):
        raise PrecheckError("基点与被推提交没有共同祖先，无法定范围")
    behind = int(_git_text(repo, "rev-list", "--count", f"{tip}..{base}", runner=runner) or 0)
    commits = _git_text(repo, "rev-list", "--reverse", f"{before}..{tip}", runner=runner).split()
    common = runner(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo, timeout=60)
    main_root = None
    if common.returncode == 0 and Path(common.stdout.strip()).name == ".git":
        main_root = Path(common.stdout.strip()).parent
    return Context(repo=repo, main_root=main_root, base_ref=base_ref, tip_ref=tip_ref, base=base, before=before, tip=tip, head=head, behind=behind,
                   commits=commits, env=dict(env), options=options, run=runner)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="仓库（默认当前目录所在的仓库）")
    parser.add_argument("--range", dest="range_spec", help="范围 <基点>..<被推提交>，如 origin/main..HEAD（默认）；两端各自可省略成默认值")
    parser.add_argument("--base", help="基点（默认 origin/main）；与 --range 二选一")
    parser.add_argument("--tip", help="被推提交（默认 HEAD）；与 --range 二选一")
    parser.add_argument("--fetch", action="store_true", help="先 git fetch origin（默认不联网刷新，基点以本地 origin/main 为准）")
    parser.add_argument("--list", action="store_true", help="只打印检查项")
    parser.add_argument("--only", action="append", default=[], metavar="NAME", help="只跑这些检查项（可重复）")
    parser.add_argument("--skip", action="append", default=[], metavar="NAME", help="显式跳过某项（可重复，必须同时给 --skip-reason）")
    parser.add_argument("--skip-reason", help="--skip 的理由，会原样写进输出")
    parser.add_argument("--fast", action="store_true", help="不跑联网的两项（前端审计、govulncheck），记为跳过")
    parser.add_argument("--strict", action="store_true", help="把跳过也当失败（退出码 1）")
    parser.add_argument("--allow-part2-change", action="store_true", help="本轮就是在做指南第二章摘要连锁：放行“第二章摘要与基点不同”，但仍查连锁是否做全")
    parser.add_argument("--allow-dirty", action="store_true", help="工作树有未提交改动也不算红")
    parser.add_argument("--receipt-dir", action="append", default=[], type=Path, help="本机收据目录（可重复；默认依次查 LOCAL_RECEIPT_DIR 与主检出的 local-analysis/local-receipts）")
    parser.add_argument("--jobs", type=int, default=4, help="并行度（默认 4；1＝逐项串行）")
    parser.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, runner: Runner = run_process) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        for name, description, networked in CHECK_TABLE:
            print(f"{name:20s} {'[联网] ' if networked else ''}{description}")
        return EXIT_OK
    try:
        for name in [*args.only, *args.skip]:
            if name not in CHECK_NAMES:
                raise PrecheckError(f"没有这个检查项：{name}（可选：{'、'.join(CHECK_NAMES)}）")
        if args.skip and not args.skip_reason:
            raise PrecheckError("--skip 必须同时给 --skip-reason（跳过要写明理由，输出里会原样带上）")
        if args.range_spec:
            if args.base or args.tip:
                raise PrecheckError("--range 与 --base／--tip 只能给一种")
            if "..." in args.range_spec or args.range_spec.count("..") != 1:
                raise PrecheckError("--range 要写成 <基点>..<被推提交>（两点）")
            left, right = args.range_spec.split("..")
            base_ref, tip_ref = left or "origin/main", right or "HEAD"
        else:
            base_ref, tip_ref = args.base or "origin/main", args.tip or "HEAD"
        options = Options(allow_part2_change=args.allow_part2_change, allow_dirty=args.allow_dirty, fast=args.fast, strict=args.strict,
                          receipt_dirs=list(args.receipt_dir), skip={name: args.skip_reason for name in args.skip}, only=list(args.only))
        started = time.monotonic()
        ctx = build_context(args.repo, base_ref, tip_ref, options, os.environ if env is None else env, runner=runner, fetch=args.fetch)
    except PrecheckError as error:
        print(f"预检无法开始：{error}", file=sys.stderr)
        return EXIT_USAGE
    names = [name for name in CHECK_NAMES if not options.only or name in options.only]
    results = run_checks(ctx, names, jobs=args.jobs)
    elapsed = time.monotonic() - started
    if args.as_json:
        print(json.dumps({"schema": SCHEMA, "range": {"base": ctx.base, "before": ctx.before, "tip": ctx.tip, "commits": len(ctx.commits)},
                          "results": [dataclasses.asdict(item) for item in results], "elapsed_seconds": round(elapsed, 1),
                          "exit_code": exit_code(results, options.strict)}, ensure_ascii=False, indent=2))
    else:
        print(render(ctx, results, elapsed))
    return exit_code(results, options.strict)


if __name__ == "__main__":
    sys.exit(main())
