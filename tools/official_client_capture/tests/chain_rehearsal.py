"""第 13 项：VC-0～VC-6 整链零请求空跑的引擎（测试夹具，不进受管身份）。

为什么要有它
------------
0.163 升级里，VC-0 入口之后的问题是“一个一个冒出来”的：容器 ID 当主机名、收尾断言漏改 Lite、Kilo 用量顺序、
收据生成器重放登记、恢复预览与续跑口径不一致……每个缺陷都要等真实运行走到那一步才暴露，修一个、部署一次、
等下一个。把 seal 预演改成“放开遇错停止”也不行：seal、compare、accept 前后依赖，上游失败后下游只会报
“前序产物不存在”，真正的缺陷测不到。

这里的办法（任务第 13 项）
--------------------------
1. **探针（Probe）**：每个历史缺陷一个独立探针，自带“独立夹具”（自己的单元用例、自己的临时目录、自己的子进程），
   或者从该步骤之前的**合法检查点**（Checkpoint）单独重放；探针之间不共享前序步骤的成功。
2. **一轮汇总**：``run_round`` 把全部探针各自跑一遍，每个探针一行结果；任何一个探针失败都不影响别的探针。
3. **依赖被阻断标“未执行”**：探针需要的能力（Linux root、录制 Campaign、Docker…）或检查点不存在时，
   该探针记 ``not_run`` 并写明阻断来源，**绝不记成通过**；用例被跳过同理（跳过 ≠ 通过）。
4. **历史缺陷重放（自检）**：``replay_history`` 把每个历史缺陷的修复补丁**反向**打进受管树副本（重新引入缺陷），
   要求对应探针报出，并且失败信息命中缺陷签名；同一探针在干净树上必须通过，证明它不是因别的原因失败。
   这是验收“空跑能否在一轮里把 0.163 的缺陷全部报出”的手段。
5. **完整链**：汇总之后另跑一遍完整链（``run_chain``），沿用 ``rehearse-candidate-seal`` 的“遇首个失败即停”语义，
   后面的步骤标 ``not_run``，用来确认整体能通；它不负责“一次查全”。

本文件不含任何对受管树的改动；回放用的真实链、故障注入链、缺陷目录登记在 ``chain_rehearsal_catalog``。
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA = "chain-rehearsal-summary/v1"
# 子进程里的单元用例运行器把结果写成带这个前缀的一行 JSON（用例自己的输出不会冒充结果）。
UNITTEST_RESULT_MARKER = "CHAIN_REHEARSAL_UNITTEST_RESULT "
REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_MODULE = "tools.official_client_capture.tests.chain_rehearsal"

# 探针结果状态。
PASSED = "passed"          # 该步骤在合法检查点上按预期通过，缺陷没有复现
FAILED = "failed"          # 当前树上该步骤失败：缺陷被报出（这正是空跑要找的东西）
REPRODUCED = "reproduced"  # 历史重放：注入缺陷后探针按预期失败，且失败信息命中缺陷签名
MISSED = "missed"          # 历史重放：注入了缺陷，探针却没报出（或报出的不是这个缺陷）——探针有盲区
NOT_RUN = "not_run"        # 未执行：能力、检查点或前序步骤缺失，或用例被跳过；绝不等于通过
ERROR = "error"            # 探针自身出错：超时、崩溃、用例不存在、输出无法解析
ALL_STATUSES = (PASSED, FAILED, REPRODUCED, MISSED, NOT_RUN, ERROR)
STATUS_LABELS = {
    PASSED: "通过", FAILED: "报出缺陷", REPRODUCED: "复现", MISSED: "漏报", NOT_RUN: "未执行", ERROR: "探针出错",
}

# 模式。
MODE_ROUND = "round"              # 当前树，一轮查全
MODE_REPLAY = "replay-history"    # 历史缺陷重放（自检）
MODE_CHAIN = "full-chain"         # 完整链（遇首个失败即停）

# 汇总结论。
VERDICT_CLEAN = "clean"                 # 全部探针都有预期结果，且没有任何未执行
VERDICT_DEFECTS = "defects_found"       # 至少一个探针报出缺陷（或历史重放漏报）
VERDICT_INCOMPLETE = "incomplete"       # 没发现缺陷，但有未执行项：不能当成通过
VERDICT_HARNESS_ERROR = "harness_error" # 探针自身出错，结论不可信
EXIT_CODES = {VERDICT_CLEAN: 0, VERDICT_DEFECTS: 1, VERDICT_HARNESS_ERROR: 2, VERDICT_INCOMPLETE: 3}

OUTPUT_TAIL_CHARS = 3000
DEFAULT_PROBE_TIMEOUT_SECONDS = 900.0


class RehearsalError(RuntimeError):
    """引擎自身的用法或配置错误（探针定义非法、能力名未登记等）。"""


# ---------------------------------------------------------------------------
# 能力探测：探针需要什么环境，缺了就“未执行”
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    reason: str


def _which(binary: str) -> tuple[bool, str]:
    found = shutil.which(binary)
    return (found is not None, found or f"PATH 里没有 {binary}")


def _go_available() -> tuple[bool, str]:
    if shutil.which("go"):
        return True, shutil.which("go") or "go"
    candidate = Path("/usr/local/go/bin/go")
    return (candidate.is_file(), str(candidate) if candidate.is_file() else "没有 go（PATH 与 /usr/local/go/bin 都找不到）")


def _linux_root_unshare() -> tuple[bool, str]:
    """私有挂载／网络命名空间回放链的前置：Linux、root、unshare 三样都要有。"""

    if not sys.platform.startswith("linux"):
        return False, f"不是 Linux（{platform.system()}）"
    if os.geteuid() != 0:
        return False, "不是 root"
    if shutil.which("unshare") is None:
        return False, "没有 unshare"
    return True, "Linux root 且有 unshare"


def _recorded_vc1_campaign() -> tuple[bool, str]:
    from tools.official_client_capture.tests import vc1_recorded_replay as replay

    if replay.available():
        return True, str(replay.recorded_campaign_dir())
    return False, "缺少 VC-1 录制 Campaign（或不是 Linux root + unshare）"


def _git_history() -> tuple[bool, str]:
    """本仓库是 git 工作树且不是浅克隆（生成器重放登记门禁要读全部部署边界的历史提交）。"""

    if shutil.which("git") is None:
        return False, "PATH 里没有 git"
    try:
        shallow = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--is-shallow-repository"],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"git 无法执行：{error}"
    if shallow.returncode != 0:
        return False, "不是 git 工作树"
    if shallow.stdout.strip() == "true":
        return False, "浅克隆（CI 需要 fetch-depth: 0）"
    return True, "完整历史"


def _driver_sources() -> tuple[bool, str]:
    """被测树里有驱动目录源码（仓库检出才有；ARM64 的部署树 /root/oauth-capture 没有，驱动另装在 /root/arm64-capture-driver）。
    VC-2 离线判据门、阶段解析这类测试按仓库相对路径读驱动脚本，没有它们就没法跑。"""

    marker = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "phase_context.py"
    return (True, str(marker.parent)) if marker.is_file() else (False, f"被测树里没有 {marker.relative_to(REPO_ROOT)}（不是仓库检出）")


def _arm64_docker_go() -> tuple[bool, str]:
    """候选身份夹具的前提：docker 守护进程可用、有 go 工具链、宿主是 linux/arm64（候选合同固定该架构）。"""

    from tools.official_client_capture.tests import candidate_identity_fixture as fixture

    return (True, "linux/arm64 + docker + go") if fixture.available() else (False, "需要 linux/arm64 且 docker 守护进程与 go 工具链可用")


# 能力登记表：名称 → 探测函数。目录模块可以追加（例如候选侧录制证据）。
CAPABILITY_PROBES: dict[str, Callable[[], tuple[bool, str]]] = {
    "linux": lambda: (sys.platform.startswith("linux"), platform.system()),
    "root": lambda: (os.geteuid() == 0, "euid=%d" % os.geteuid()),
    "unshare": lambda: _which("unshare"),
    "git": lambda: _which("git"),
    "docker": lambda: _which("docker"),
    "go": _go_available,
    "linux_root_unshare": _linux_root_unshare,
    "recorded_vc1_campaign": _recorded_vc1_campaign,
    "git_history": _git_history,
    "arm64_docker_go": _arm64_docker_go,
    "driver_sources": _driver_sources,
}


def detect_capabilities(names: Iterable[str] | None = None) -> dict[str, Capability]:
    """逐项探测能力；探测函数自己抛错也只当“不可用”，不能让整轮空跑中断。"""

    result: dict[str, Capability] = {}
    for name in (sorted(CAPABILITY_PROBES) if names is None else list(names)):
        probe = CAPABILITY_PROBES.get(name)
        if probe is None:
            raise RehearsalError(f"能力名未登记：{name}")
        try:
            available, reason = probe()
        except Exception as error:  # noqa: BLE001 - 探测失败等同不可用
            available, reason = False, f"探测出错：{error!r}"
        result[name] = Capability(name, bool(available), str(reason))
    return result


# ---------------------------------------------------------------------------
# 探针、检查点与执行器
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RunContext:
    """一次探针执行的上下文。``tree`` 是被测受管树的仓库根（历史重放时是注入过缺陷的副本）。"""

    tree: Path
    work: Path
    capabilities: Mapping[str, Capability]
    checkpoints: Mapping[str, Path]
    injected: bool = False
    # 历史重放时 ``tree`` 是注入过缺陷的副本；``origin_tree`` 始终是原始被测树（取 git 历史等只读信息用）。
    origin_tree: Path | None = None


@dataclasses.dataclass
class Outcome:
    """执行器的原始结论，由引擎统一翻译成探针状态。"""

    verdict: str  # pass | fail | skip | error
    detail: str = ""
    output: str = ""
    tests: list[dict[str, str]] | None = None
    # 用例根本加载不了（模块导入失败、类／方法不存在）。干净树上这是目录写错（探针出错）；注入了历史缺陷的副本上，
    # 回归用例按修复后的接口写，缺陷回来时它们可能连导入都过不了，这本身就是缺陷被报出，见 ``replay_history``。
    load_error: bool = False
    # 加载失败时各用例的导入错误正文（``ImportError: cannot import name 'x' …``），不含被请求的用例名；历史重放的缺陷签名
    # 在加载失败路径上只匹配这些正文——否则探针引用的用例名本身就能让任何加载失败“命中”。
    load_errors: list[str] | None = None


class Runner:
    """探针执行器基类：只负责把“跑一遍”变成 ``Outcome``，不决定状态翻译。"""

    description = ""

    def run(self, context: RunContext) -> Outcome:  # pragma: no cover - 接口
        raise NotImplementedError


def _origin_git_dir(origin: Path) -> str:
    """原始树的真实 git 目录（``.git`` 在工作树副本里可能是文件，要问 git 要绝对路径）；取不到返回空串。"""

    try:
        completed = subprocess.run(["git", "-C", str(origin), "rev-parse", "--absolute-git-dir"],
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def expand_placeholders(value: str, context: RunContext) -> str:
    """展开命令与环境变量里的占位符：``{tree}`` 被测树（可能是注入后的副本）、``{work}`` 探针的临时目录、
    ``{origin}`` 原始被测树、``{origin_git_dir}`` 原始树的 git 目录。

    ``{origin_git_dir}`` 让“必须读 git 历史、又要在注入过缺陷的副本上跑”的探针（生成器重放登记门禁）可以把 ``GIT_DIR``
    指回真实仓库：副本里没有 ``.git``，但门禁只读历史对象，不需要副本是 git 工作树。
    """

    origin = context.origin_tree or context.tree
    result = value.replace("{tree}", str(context.tree)).replace("{work}", str(context.work)).replace("{origin}", str(origin))
    if "{origin_git_dir}" in result:
        result = result.replace("{origin_git_dir}", _origin_git_dir(origin))
    return result


def child_environment(tree: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """子进程环境：PYTHONPATH 只含被测树根（禁止回落导入别处的仓库），并且不写字节码。

    故意不用 ``managed_tree_copy.subprocess_env``：它把字节码前缀建在树根的上一级目录，对真实仓库根会在 worktrees
    目录里留下 ``.pycache-prefix``；这里一律不写字节码（工作树里出现 __pycache__ 会让门禁前置检查退出 9）。
    """

    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONPYCACHEPREFIX"}}
    env["PYTHONPATH"] = str(tree)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    go_bin = Path("/usr/local/go/bin")
    if shutil.which("go") is None and (go_bin / "go").is_file():
        env["PATH"] = f"{go_bin}:{env.get('PATH', '')}"
    if extra:
        env.update(extra)
    return env


def _tail(text: str, limit: int = OUTPUT_TAIL_CHARS) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _decode(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


class UnittestRunner(Runner):
    """在被测树里用独立子进程跑一组单元用例，并按用例逐个取结论。

    判定（宁可严格，不放过）：用例不存在 → ``error``；任一用例失败／出错 → ``fail``；没有失败但有跳过 → ``skip``
    （跳过的用例没验证任何东西，探针记“未执行”）；全部通过 → ``pass``。
    """

    def __init__(self, test_ids: Sequence[str], *, timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
                 extra_env: Mapping[str, str] | None = None) -> None:
        if not test_ids:
            raise RehearsalError("单元用例探针至少要有一个用例")
        self.test_ids = tuple(test_ids)
        self.timeout_seconds = timeout_seconds
        self.extra_env = dict(extra_env or {})
        self.description = "unittest " + " ".join(self.test_ids)

    def run(self, context: RunContext) -> Outcome:
        argv = [sys.executable, "-B", "-m", PACKAGE_MODULE, "_unittest", *self.test_ids]
        extra = {key: expand_placeholders(value, context) for key, value in self.extra_env.items()}
        try:
            completed = subprocess.run(
                argv, cwd=str(context.tree), env=child_environment(context.tree, extra),
                capture_output=True, text=True, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            return Outcome("error", f"超时（{self.timeout_seconds:g} 秒）", _tail(_decode(error.stdout) + _decode(error.stderr)))
        payload = None
        for line in completed.stdout.splitlines():
            if line.startswith(UNITTEST_RESULT_MARKER):
                try:
                    payload = json.loads(line[len(UNITTEST_RESULT_MARKER):])
                except json.JSONDecodeError:
                    payload = None
        # 结果标记行里有全部用例 id 与报错，不能留在“输出”里（输出会参与缺陷签名匹配）。
        output = "\n".join(line for line in (completed.stdout + completed.stderr).splitlines()
                           if not line.startswith(UNITTEST_RESULT_MARKER))
        if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
            return Outcome("error", f"运行器没有给出结果（退出码 {completed.returncode}）", _tail(output))
        return classify_tests(payload["tests"], payload.get("missing") or [], output, payload.get("load_errors") or [])


def classify_tests(tests: list[dict[str, str]], missing: Sequence[str], output: str = "",
                   load_errors: Sequence[str] = ()) -> Outcome:
    """把逐用例结论翻译成一个 ``Outcome``（独立成函数，方便单测）。"""

    if missing:
        return Outcome("error", "探针引用的用例不存在或无法导入：" + "；".join(str(item) for item in missing[:5]), _tail(output), tests,
                       load_error=True, load_errors=[str(item) for item in load_errors])
    failed = [item for item in tests if item.get("outcome") in {"fail", "error"}]
    skipped = [item for item in tests if item.get("outcome") == "skip"]
    if failed:
        detail = "；".join(f"{item['id']}: {item.get('detail', '')[:200]}" for item in failed[:3])
        return Outcome("fail", f"{len(failed)} 个用例失败：{detail}", _tail(output), tests)
    if not tests:
        return Outcome("error", "一个用例都没跑", _tail(output), tests)
    if skipped:
        reasons = sorted({item.get("detail", "") for item in skipped})
        return Outcome("skip", f"{len(skipped)}/{len(tests)} 个用例被跳过：" + "；".join(reasons[:3]), _tail(output), tests)
    return Outcome("pass", f"{len(tests)} 个用例通过", "", tests)


class CommandRunner(Runner):
    """跑一条命令：退出码 0 通过，``skip_codes`` 里的退出码算跳过，其余算失败。命令里的 ``{tree}`` 替换成被测树根。"""

    def __init__(self, argv: Sequence[str], *, timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
                 skip_codes: Sequence[int] = (), extra_env: Mapping[str, str] | None = None) -> None:
        if not argv:
            raise RehearsalError("命令探针的 argv 不能为空")
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
        self.skip_codes = tuple(skip_codes)
        self.extra_env = dict(extra_env or {})
        self.description = "command " + " ".join(self.argv)

    def run(self, context: RunContext) -> Outcome:
        argv = [expand_placeholders(item, context) for item in self.argv]
        extra = {key: expand_placeholders(value, context) for key, value in self.extra_env.items()}
        try:
            completed = subprocess.run(
                argv, cwd=str(context.tree), env=child_environment(context.tree, extra),
                capture_output=True, text=True, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            return Outcome("error", f"超时（{self.timeout_seconds:g} 秒）", _tail(_decode(error.stdout) + _decode(error.stderr)))
        except OSError as error:
            return Outcome("error", f"命令无法启动：{error}", "")
        output = completed.stdout + completed.stderr
        if completed.returncode == 0:
            return Outcome("pass", "退出码 0", _tail(output))
        if completed.returncode in self.skip_codes:
            return Outcome("skip", f"退出码 {completed.returncode}（约定为条件不满足）", _tail(output))
        return Outcome("fail", f"退出码 {completed.returncode}", _tail(output))


class CallableRunner(Runner):
    """在本进程里调用一个函数（主要给引擎单测用）：返回 ``Outcome``；抛异常记 ``fail``，``unittest.SkipTest`` 记 ``skip``。"""

    def __init__(self, function: Callable[[RunContext], Outcome | None], description: str = "callable") -> None:
        self.function = function
        self.description = description

    def run(self, context: RunContext) -> Outcome:
        try:
            result = self.function(context)
        except unittest.SkipTest as error:
            return Outcome("skip", str(error))
        except AssertionError as error:
            return Outcome("fail", f"断言失败：{error}", traceback.format_exc()[-OUTPUT_TAIL_CHARS:])
        except Exception as error:  # noqa: BLE001 - 探针里的异常就是该步骤的失败
            return Outcome("fail", f"{type(error).__name__}: {error}", traceback.format_exc()[-OUTPUT_TAIL_CHARS:])
        return result if isinstance(result, Outcome) else Outcome("pass", "")


@dataclasses.dataclass(frozen=True)
class Checkpoint:
    """合法检查点：某个步骤**之前**的阶段产物，探针从它开始单独重放，不依赖前序步骤“本轮”成功。

    ``builder(directory, context)`` 把检查点建在 ``directory`` 里；建不出来（抛异常）时，依赖它的探针全部标“未执行”，
    阻断来源写成这个检查点，而不是让探针各自报“前序产物不存在”。
    """

    checkpoint_id: str
    title: str
    builder: Callable[[Path, RunContext], None]
    needs: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Probe:
    """一个独立探针。

    * ``phase``／``step``：缺陷触发的 VC 阶段与步骤（给汇总表看，也用来按阶段筛选）；
    * ``kind``：``defect`` 历史缺陷，``recovery`` 恢复分支故障注入，``chain`` 完整链里的一环；
    * ``needs``：运行所需的能力名（见 ``CAPABILITY_PROBES``），缺任何一项即“未执行”；
    * ``checkpoint``：需要的合法检查点名，建不出来即“未执行”；
    * ``requires_probes``：必须先通过的前序探针（完整链用；独立探针不要写）；
    * ``signature``：缺陷存在时失败输出里应出现的片段（任一命中即算命中），历史重放用；
    * ``injections``：重新引入缺陷的注入器名单，见 ``chain_rehearsal_inject``；没有注入器的探针不参加历史重放；
    * ``copy_extra``：历史重放复制树时额外要带上的仓库相对路径（默认只复制受管树、驱动与 tools/ci；个别门禁还要读 docs 下的收据）。
    """

    probe_id: str
    title: str
    phase: str
    step: str
    runner: Runner
    kind: str = "defect"
    needs: tuple[str, ...] = ()
    checkpoint: str | None = None
    requires_probes: tuple[str, ...] = ()
    history: tuple[str, ...] = ()
    signature: tuple[str, ...] = ()
    injections: tuple[Any, ...] = ()
    copy_extra: tuple[str, ...] = ()


@dataclasses.dataclass
class ProbeResult:
    probe_id: str
    title: str
    phase: str
    step: str
    kind: str
    status: str
    detail: str = ""
    seconds: float = 0.0
    blocked_by: dict[str, str] | None = None
    signature_hit: bool | None = None
    baseline_status: str | None = None
    history: tuple[str, ...] = ()
    output_tail: str = ""
    failures: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.probe_id, "title": self.title, "phase": self.phase, "step": self.step, "kind": self.kind,
            "status": self.status, "label": STATUS_LABELS[self.status], "detail": self.detail,
            "seconds": round(self.seconds, 3), "blocked_by": self.blocked_by, "signature_hit": self.signature_hit,
            "baseline_status": self.baseline_status, "history": list(self.history), "output_tail": self.output_tail,
            "failures": self.failures,
        }


def validate_probes(probes: Sequence[Probe], checkpoints: Mapping[str, Checkpoint] | None = None) -> None:
    """目录自检：id 唯一、能力名和检查点名都已登记、前序探针存在且不成环。"""

    checkpoints = checkpoints or {}
    seen: set[str] = set()
    for probe in probes:
        if probe.probe_id in seen:
            raise RehearsalError(f"探针 id 重复：{probe.probe_id}")
        seen.add(probe.probe_id)
        for name in probe.needs:
            if name not in CAPABILITY_PROBES:
                raise RehearsalError(f"{probe.probe_id}：能力名未登记 {name}")
        if probe.checkpoint is not None and probe.checkpoint not in checkpoints:
            raise RehearsalError(f"{probe.probe_id}：检查点未登记 {probe.checkpoint}")
    ids = {probe.probe_id for probe in probes}
    for probe in probes:
        missing = [item for item in probe.requires_probes if item not in ids]
        if missing:
            raise RehearsalError(f"{probe.probe_id}：前序探针不存在 {missing}")
    # 前序关系不能成环（成环意味着两个步骤互相等对方先通过，永远“未执行”）。
    graph = {probe.probe_id: probe.requires_probes for probe in probes}
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise RehearsalError(f"前序探针成环：{node}")
        visiting.add(node)
        for parent in graph[node]:
            visit(parent)
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node)


# ---------------------------------------------------------------------------
# 单个探针的执行与状态翻译
# ---------------------------------------------------------------------------


class _CheckpointStore:
    """检查点的懒构建与缓存：同一个检查点只建一次；建失败也只记一次，依赖它的探针共用同一个阻断原因。"""

    def __init__(self, checkpoints: Mapping[str, Checkpoint], work: Path) -> None:
        self.checkpoints = checkpoints
        self.work = work
        self.built: dict[str, Path] = {}
        self.failed: dict[str, str] = {}

    def ensure(self, checkpoint_id: str, tree: Path, capabilities: Mapping[str, Capability]) -> tuple[Path | None, str]:
        if checkpoint_id in self.built:
            return self.built[checkpoint_id], ""
        if checkpoint_id in self.failed:
            return None, self.failed[checkpoint_id]
        checkpoint = self.checkpoints[checkpoint_id]
        for name in checkpoint.needs:
            capability = capabilities.get(name) or detect_capabilities([name])[name]
            if not capability.available:
                reason = f"检查点需要能力 {name}：{capability.reason}"
                self.failed[checkpoint_id] = reason
                return None, reason
        directory = self.work / "checkpoints" / checkpoint_id
        directory.mkdir(parents=True, exist_ok=True)
        context = RunContext(tree=tree, work=directory, capabilities=capabilities, checkpoints=dict(self.built), origin_tree=tree)
        try:
            checkpoint.builder(directory, context)
        except Exception as error:  # noqa: BLE001 - 建不出检查点就是阻断原因
            reason = f"检查点构建失败：{type(error).__name__}: {error}"
            self.failed[checkpoint_id] = reason
            return None, reason
        self.built[checkpoint_id] = directory
        return directory, ""


def _blocked(probe: Probe, kind: str, source: str, reason: str) -> ProbeResult:
    return ProbeResult(
        probe.probe_id, probe.title, probe.phase, probe.step, probe.kind, NOT_RUN,
        detail=f"未执行：{reason}", blocked_by={"type": kind, "id": source, "reason": reason}, history=probe.history,
    )


def _prepare(probe: Probe, *, capabilities: Mapping[str, Capability], store: _CheckpointStore, tree: Path,
             results: Mapping[str, ProbeResult] | None) -> ProbeResult | None:
    """跑之前的阻断检查：前序探针 → 能力 → 检查点。任何一项不满足就返回“未执行”的结果，满足返回 None。"""

    for parent in probe.requires_probes:
        done = (results or {}).get(parent)
        if done is None or done.status != PASSED:
            state = "尚未执行" if done is None else f"状态为{STATUS_LABELS[done.status]}"
            return _blocked(probe, "step", parent, f"前序步骤 {parent} {state}")
    for name in probe.needs:
        capability = capabilities.get(name) or detect_capabilities([name])[name]
        if not capability.available:
            return _blocked(probe, "capability", name, f"缺少能力 {name}（{capability.reason}）")
    if probe.checkpoint is not None:
        directory, reason = store.ensure(probe.checkpoint, tree, capabilities)
        if directory is None:
            return _blocked(probe, "checkpoint", probe.checkpoint, reason)
    return None


def _execute(probe: Probe, context: RunContext) -> tuple[Outcome, float]:
    started = time.monotonic()
    try:
        outcome = probe.runner.run(context)
    except Exception as error:  # noqa: BLE001 - 执行器自己的故障：记探针出错，不中断整轮
        outcome = Outcome("error", f"执行器异常：{type(error).__name__}: {error}", traceback.format_exc()[-OUTPUT_TAIL_CHARS:])
    return outcome, time.monotonic() - started


def run_probe(probe: Probe, *, tree: Path, work: Path, capabilities: Mapping[str, Capability], store: _CheckpointStore,
              results: Mapping[str, ProbeResult] | None = None) -> ProbeResult:
    """当前树模式：探针期望通过。通过 → ``passed``；失败 → ``failed``（缺陷被报出）；跳过／被阻断 → ``not_run``。"""

    blocked = _prepare(probe, capabilities=capabilities, store=store, tree=tree, results=results)
    if blocked is not None:
        return blocked
    scratch = work / "probes" / probe.probe_id
    scratch.mkdir(parents=True, exist_ok=True)
    context = RunContext(tree=tree, work=scratch, capabilities=capabilities, checkpoints=dict(store.built), origin_tree=tree)
    outcome, seconds = _execute(probe, context)
    result = ProbeResult(probe.probe_id, probe.title, probe.phase, probe.step, probe.kind, ERROR,
                         detail=outcome.detail, seconds=seconds, history=probe.history, output_tail=outcome.output)
    if outcome.verdict == "pass":
        result.status = PASSED
    elif outcome.verdict == "fail":
        result.status = FAILED
        result.failures = failure_excerpts(outcome)
        if probe.signature:
            result.signature_hit = _signature_hit(probe, outcome)
    elif outcome.verdict == "skip":
        result.status = NOT_RUN
        result.blocked_by = {"type": "skipped", "id": probe.probe_id, "reason": outcome.detail}
        result.detail = f"未执行：{outcome.detail}"
    else:
        result.status = ERROR
    return result


_TRACEBACK_FRAME = re.compile(r'^\s*File ".*", line \d+(, in .*)?$')
_IMPORT_HEADER = re.compile(r"^ImportError: Failed to import test module: .*$")


def exception_text(detail: str) -> str:
    """去掉 traceback 里的 ``File "…", line N, in 函数`` 行、``Traceback`` 头与 unittest 的“Failed to import test module: 请求的
    模块名”头，保留异常类型、异常消息和调用栈帧里的源码行。

    ``in 函数`` 里是测试方法名，导入头里是探针请求的模块名——它们都由探针自己的引用决定，任何失败（甚至注入了无关的
    语法错误）都会带上，拿来当缺陷签名等于“探针失败就命中”。留下的是缺陷现象本身：断言消息、缺失的符号名、被改坏的值，
    以及抛错的那一行源码（例如 ``start = source.index("def _decoded_request_body")`` 说明脚本里该函数不见了）。
    """

    kept: list[str] = []
    for line in detail.splitlines():
        if _TRACEBACK_FRAME.match(line) or line.startswith("Traceback (most recent call last)") or _IMPORT_HEADER.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def signature_haystack(outcome: Outcome) -> str:
    """缺陷签名只在这里面找：

    * 加载失败（回归用例连导入都过不了）：只看导入错误正文，不看被请求的用例名；
    * 单元用例失败：只看各失败用例的异常消息与源码行（已去掉 ``in 测试方法名`` 的栈帧行）和进程输出，不看用例 id；
    * 命令／函数探针：看摘要与输出。
    """

    if outcome.load_error:
        return "\n".join(exception_text(item) for item in (outcome.load_errors or []))
    if outcome.tests is not None:
        # 进程输出（stdout／stderr）也算现象：有的缺陷让工具把拒绝原因打到标准错误，断言消息里只剩 “2 != 0”。结果标记行
        # （含全部用例 id 与报错）在 UnittestRunner 里已经剔除。
        failing = "\n".join(exception_text(str(item.get("detail", ""))) for item in outcome.tests
                            if item.get("outcome") in {"fail", "error"})
        return failing + "\n" + exception_text(outcome.output)
    return exception_text(outcome.detail + "\n" + outcome.output)


def _signature_hit(probe: Probe, outcome: Outcome) -> bool:
    haystack = signature_haystack(outcome)
    return any(fragment in haystack for fragment in probe.signature)


def failure_excerpts(outcome: Outcome, limit: int = 5, width: int = 800) -> list[dict[str, str]]:
    """失败现象摘录（汇总里留痕，也是挑缺陷签名的依据）：失败用例的 id 与异常消息，加载错误正文，或命令探针的输出尾部。"""

    if outcome.tests is None and not outcome.load_error:
        return [{"id": "（命令输出）", "message": (outcome.detail + "\n" + outcome.output).strip()[-width:]}]
    if outcome.load_error:
        return [{"id": "（加载失败）", "message": exception_text(item)[:width]} for item in (outcome.load_errors or [])[:limit]]
    return [{"id": str(item.get("id", "")), "message": exception_text(str(item.get("detail", "")))[:width]}
            for item in (outcome.tests or []) if item.get("outcome") in {"fail", "error"}][:limit]


def run_round(probes: Sequence[Probe], *, tree: Path, work: Path, checkpoints: Mapping[str, Checkpoint] | None = None,
              capabilities: Mapping[str, Capability] | None = None, stop_on_first_failure: bool = False,
              progress: Callable[[ProbeResult], None] | None = None) -> list[ProbeResult]:
    """一轮查全：每个探针各自独立执行并各出一行结果。

    ``stop_on_first_failure=False``（独立探针，默认）：任何探针失败都不影响别的探针；
    ``True``（完整链）：首个失败／出错之后，其余探针一律标“未执行”并写明被谁阻断——沿用 ``rehearse-candidate-seal``
    遇首个失败即停的语义，用来确认整体能通，不负责一次查全。因能力缺失而未执行的步骤不算失败，不触发停止。
    """

    validate_probes(probes, checkpoints)
    capabilities = dict(capabilities) if capabilities is not None else detect_capabilities()
    work.mkdir(parents=True, exist_ok=True)
    store = _CheckpointStore(checkpoints or {}, work)
    results: dict[str, ProbeResult] = {}
    stopper: ProbeResult | None = None
    for probe in probes:
        if stopper is not None:
            result = _blocked(probe, "step", stopper.probe_id,
                              f"完整链在 {stopper.probe_id} 处停止（{STATUS_LABELS[stopper.status]}），后续步骤不再执行")
        else:
            result = run_probe(probe, tree=tree, work=work, capabilities=capabilities, store=store, results=results)
        results[probe.probe_id] = result
        if progress is not None:
            progress(result)
        # 只有真正的失败／出错才让完整链停下；因能力或前序依赖而“未执行”的步骤不连累别的独立步骤（它们各自的
        # requires_probes 已经表达了真实依赖）。
        if stop_on_first_failure and stopper is None and result.status in {FAILED, ERROR, MISSED}:
            stopper = result
    return [results[probe.probe_id] for probe in probes]


# ---------------------------------------------------------------------------
# 历史缺陷重放（自检）
# ---------------------------------------------------------------------------


# 复制一棵被测树：``(目标目录, 额外要带上的仓库相对路径) -> 副本根``。
TreeCopier = Callable[[Path, Sequence[str]], Path]
Injector = Callable[[Path, Any], Any]


def replay_history(probes: Sequence[Probe], *, tree: Path, work: Path, copy_tree: TreeCopier, inject: Injector,
                   checkpoints: Mapping[str, Checkpoint] | None = None,
                   capabilities: Mapping[str, Capability] | None = None,
                   progress: Callable[[ProbeResult], None] | None = None) -> list[ProbeResult]:
    """把每个历史缺陷重新引入受管树副本，要求对应探针在**一轮**里各自报出。

    每个探针单独复制一棵树，在这棵副本上依次做两件事：
    1. **未注入的基线**：先在刚复制出来、还没打任何补丁的副本上跑一遍，必须通过。副本是被测环境的一部分——复制漏掉
       探针依赖的文件（驱动目录、docs 收据……）会让注入后的失败看起来像“缺陷复现”，其实只是副本不完整；基线在副本上
       通过，才证明注入前后的唯一差别就是这个缺陷。基线失败记 ``failed``，被跳过记 ``not_run``，出错记 ``error``；
    2. **注入后重跑**：打上它自己的注入器（探针之间互不污染）再跑；失败且失败现象命中缺陷签名 → ``reproduced``；
       通过 → ``missed``；失败但现象不含签名 → ``missed``（报出的不是这个缺陷）；跳过 → ``not_run``。
    能力与检查点的阻断检查仍针对原始树做（它们描述的是环境，不是副本）。没有注入器的探针不参加历史重放。
    """

    candidates = [probe for probe in probes if probe.injections]
    validate_probes(candidates, checkpoints)
    for probe in candidates:
        if not probe.signature:
            raise RehearsalError(f"{probe.probe_id}：带注入器的探针必须有缺陷签名，否则无法确认报出的是这个缺陷")
    capabilities = dict(capabilities) if capabilities is not None else detect_capabilities()
    work.mkdir(parents=True, exist_ok=True)
    store = _CheckpointStore(checkpoints or {}, work)
    results: list[ProbeResult] = []

    def finish(result: ProbeResult) -> None:
        results.append(result)
        if progress is not None:
            progress(result)

    for probe in candidates:
        blocked = _prepare(dataclasses.replace(probe, requires_probes=()), capabilities=capabilities, store=store,
                           tree=tree, results=None)
        if blocked is not None:
            finish(blocked)
            continue
        started = time.monotonic()
        injected_tree: Path | None = None
        try:
            try:
                injected_tree = copy_tree(work / "trees" / probe.probe_id, probe.copy_extra)
            except Exception as error:  # noqa: BLE001 - 副本都复制不出来，无法重放
                finish(_blocked(probe, "copy", probe.probe_id, f"无法复制被测树：{type(error).__name__}: {error}"))
                continue
            scratch = work / "probes" / probe.probe_id
            scratch.mkdir(parents=True, exist_ok=True)
            # 1) 未注入副本上的基线。
            base_context = RunContext(tree=injected_tree, work=scratch / "baseline", capabilities=capabilities,
                                      checkpoints=dict(store.built), injected=False, origin_tree=tree)
            base_context.work.mkdir(parents=True, exist_ok=True)
            base_outcome, _ = _execute(probe, base_context)
            if base_outcome.verdict != "pass":
                status = {"fail": FAILED, "skip": NOT_RUN}.get(base_outcome.verdict, ERROR)
                failed = ProbeResult(probe.probe_id, probe.title, probe.phase, probe.step, probe.kind, status,
                                     detail=f"未注入的副本基线未通过（副本可能漏复制了依赖，或探针本身有问题）：{base_outcome.detail}",
                                     seconds=time.monotonic() - started, baseline_status=status, history=probe.history,
                                     output_tail=base_outcome.output, failures=failure_excerpts(base_outcome))
                if status == NOT_RUN:
                    failed.blocked_by = {"type": "skipped", "id": probe.probe_id, "reason": base_outcome.detail}
                finish(failed)
                continue
            # 2) 注入缺陷后重跑。
            try:
                for injection in probe.injections:
                    inject(injected_tree, injection)
            except Exception as error:  # noqa: BLE001 - 注入器应用不上（上下文漂移等）：这个缺陷无法重放，标未执行
                result = _blocked(probe, "injection", probe.probe_id, f"历史缺陷无法重新引入：{type(error).__name__}: {error}")
                result.baseline_status = PASSED
                finish(result)
                continue
            context = RunContext(tree=injected_tree, work=scratch / "injected", capabilities=capabilities,
                                 checkpoints=dict(store.built), injected=True, origin_tree=tree)
            context.work.mkdir(parents=True, exist_ok=True)
            outcome, _ = _execute(probe, context)
            if outcome.verdict == "error" and outcome.load_error:
                # 基线（未注入副本）上同一批用例能加载、能通过，注入缺陷之后连加载都失败：修复时新增的接口被改回去了。
                # 这是缺陷被报出，不是探针出错；签名只在导入错误正文里找（不看请求的用例名）。
                outcome = Outcome("fail", "回归用例在注入缺陷后无法加载（修复时新增的接口缺失）：" + "；".join(outcome.load_errors or [outcome.detail]),
                                  outcome.output, outcome.tests, load_error=True, load_errors=outcome.load_errors)
            result = ProbeResult(probe.probe_id, probe.title, probe.phase, probe.step, probe.kind, ERROR, detail=outcome.detail,
                                 seconds=(time.monotonic() - started), baseline_status=PASSED, history=probe.history,
                                 output_tail=outcome.output)
            if outcome.verdict == "fail":
                result.failures = failure_excerpts(outcome)
                hit = _signature_hit(probe, outcome)
                result.signature_hit = hit
                if hit:
                    result.status = REPRODUCED
                else:
                    result.status = MISSED
                    result.detail = f"失败了，但失败现象不含缺陷签名 {list(probe.signature)}（可能是无关原因）：{outcome.detail}"
            elif outcome.verdict == "pass":
                result.status = MISSED
                result.detail = "注入了历史缺陷，探针却通过了：探针有盲区"
            elif outcome.verdict == "skip":
                result.status = NOT_RUN
                result.blocked_by = {"type": "skipped", "id": probe.probe_id, "reason": outcome.detail}
                result.detail = f"未执行：{outcome.detail}"
            finish(result)
        finally:
            # 每个探针用完立刻删掉自己的树副本（约 70 MB），整轮不会同时留着十几棵副本把根盘填满。
            if injected_tree is not None:
                shutil.rmtree(injected_tree, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def verdict_of(results: Sequence[ProbeResult], *, mode: str) -> str:
    """汇总结论。**未执行不算通过**：只要有未执行项，最好也只能是 ``incomplete``。

    优先级：探针出错（结论不可信）> 发现缺陷（当前树有失败；历史重放有漏报／基线失败）> 有未执行 > clean。
    """

    statuses = [item.status for item in results]
    if ERROR in statuses:
        return VERDICT_HARNESS_ERROR
    if any(status in {FAILED, MISSED} for status in statuses):
        return VERDICT_DEFECTS
    if NOT_RUN in statuses:
        return VERDICT_INCOMPLETE
    if not results:
        return VERDICT_INCOMPLETE
    return VERDICT_CLEAN


def build_summary(results: Sequence[ProbeResult], *, mode: str, tree: Path, capabilities: Mapping[str, Capability],
                  started_at: str, finished_at: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    totals = {status: 0 for status in ALL_STATUSES}
    for item in results:
        totals[item.status] += 1
    blocked_sources: dict[str, list[str]] = {}
    for item in results:
        if item.blocked_by:
            key = f"{item.blocked_by['type']}:{item.blocked_by['id']}"
            blocked_sources.setdefault(key, []).append(item.probe_id)
    summary: dict[str, Any] = {
        "schema_version": SCHEMA,
        "mode": mode,
        "tree": str(tree),
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "capabilities": {name: {"available": cap.available, "reason": cap.reason} for name, cap in sorted(capabilities.items())},
        "totals": totals,
        "total": len(results),
        "not_run_sources": {key: sorted(ids) for key, ids in sorted(blocked_sources.items())},
        "verdict": verdict_of(results, mode=mode),
        "rows": [item.to_json() for item in results],
    }
    if extra:
        summary.update(dict(extra))
    summary["summary_sha256"] = hashlib.sha256(
        json.dumps({k: v for k, v in summary.items() if k != "summary_sha256"}, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return summary


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x2E80 else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _clip(text: str, width: int) -> str:
    """按显示宽度截断（中文占两格），超出的部分用省略号。"""

    if _display_width(text) <= width:
        return text
    out, used = "", 0
    for char in text:
        step = 2 if ord(char) > 0x2E80 else 1
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "…"


def render_table(summary: Mapping[str, Any]) -> str:
    """汇总的人读表：编号、阶段、缺陷／步骤、结果、阻断来源。未执行单列原因，绝不和通过混在一起。"""

    rows = summary["rows"]
    lines = [f"模式 {summary['mode']}；被测树 {summary['tree']}；结论 {summary['verdict']}"]
    if summary.get("scope_note"):
        lines.append(f"范围：{summary['scope_note']}")
    header = ("编号", "阶段", "缺陷／步骤", "结果", "说明")
    table = [header]
    for row in rows:
        note = row["detail"]
        if row.get("blocked_by"):
            blocked = row["blocked_by"]
            note = f"被阻断（{blocked['type']}:{blocked['id']}）：{blocked['reason']}"
        table.append((row["id"], row["phase"], _clip(row["title"], 56), row["label"], _clip(note.replace("\n", " "), 90)))
    widths = [max(_display_width(str(line[column])) for line in table) for column in range(len(header))]
    widths[-1] = min(widths[-1], 90)
    for index, line in enumerate(table):
        lines.append("  ".join(_pad(str(cell), widths[position]) for position, cell in enumerate(line)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * widths[position] for position in range(len(header))))
    totals = summary["totals"]
    lines.append("合计：" + "，".join(f"{STATUS_LABELS[status]} {totals[status]}" for status in ALL_STATUSES if totals[status]))
    return "\n".join(lines)


def exit_code(summary: Mapping[str, Any]) -> int:
    return EXIT_CODES[summary["verdict"]]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_summary(summary: Mapping[str, Any], out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / f"{name}.txt").write_text(render_table(summary) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 子进程里的单元用例运行器（内部子命令 _unittest）
# ---------------------------------------------------------------------------


class _RecordingResult(unittest.TestResult):
    """逐用例记录结论，不依赖 -v 的输出格式（多行 docstring 会让文本解析出错）。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, str]] = []

    @staticmethod
    def _clip(text: str, head: int = 2500, tail: int = 1500) -> str:
        """超长报错（断言消息里常带整份脚本）保留开头和结尾：开头有异常类型与断言消息，结尾有最后一帧。"""

        return text if len(text) <= head + tail else text[:head] + "\n…（中间省略）…\n" + text[-tail:]

    def _add(self, test: unittest.TestCase, outcome: str, detail: str = "") -> None:
        self.records.append({"id": test.id(), "outcome": outcome, "detail": self._clip(detail)})

    def addSuccess(self, test):  # noqa: N802
        super().addSuccess(test)
        self._add(test, "ok")

    def addFailure(self, test, err):  # noqa: N802
        super().addFailure(test, err)
        self._add(test, "fail", self.failures[-1][1])

    def addError(self, test, err):  # noqa: N802
        super().addError(test, err)
        self._add(test, "error", self.errors[-1][1])

    def addSkip(self, test, reason):  # noqa: N802
        super().addSkip(test, reason)
        self._add(test, "skip", str(reason))

    def addSubTest(self, test, subtest, err):  # noqa: N802
        super().addSubTest(test, subtest, err)
        if err is not None:
            text = self.failures[-1][1] if self.failures and self.failures[-1][0] is subtest else (self.errors[-1][1] if self.errors else "")
            self._add(test, "fail", text)


def run_unittest_ids(test_ids: Sequence[str]) -> dict[str, Any]:
    """加载并运行用例，返回 ``{"tests": [...], "missing": [...]}``。

    用例名不存在时 ``unittest`` 不抛异常，而是塞一个会在运行时报 ImportError／AttributeError 的占位用例；这里把占位用例
    单独挑出来记为 ``missing``，让目录里写错的用例名当场暴露，而不是被当成一次普通失败。
    """

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    missing: list[str] = []
    load_errors: list[str] = []
    for name in test_ids:
        try:
            loaded = loader.loadTestsFromName(name)
        except Exception as error:  # noqa: BLE001 - 导入时抛的 SyntaxError 等不是 ImportError，unittest 不会替我们包成占位用例
            reason = "".join(traceback.format_exception_only(type(error), error)).strip()
            missing.append(f"{name}（{reason}）")
            load_errors.append(reason)
            continue
        stack = [loaded]
        while stack:
            item = stack.pop()
            if isinstance(item, unittest.TestSuite):
                stack.extend(item)
            elif item.__class__.__name__ == "_FailedTest":
                exception = getattr(item, "_exception", None)
                reason = "".join(traceback.format_exception_only(type(exception), exception)).strip() if exception else item.id()
                missing.append(f"{name}（{reason}）")
                load_errors.append(reason)
        if loaded.countTestCases() == 0:
            missing.append(f"{name}（没有匹配到任何用例）")
            load_errors.append("没有匹配到任何用例")
        suite.addTest(loaded)
    if missing:
        return {"tests": [], "missing": missing, "load_errors": load_errors}
    result = _RecordingResult()
    suite.run(result)
    return {"tests": result.records, "missing": [], "load_errors": []}


def _unittest_main(test_ids: Sequence[str]) -> int:
    payload = run_unittest_ids(test_ids)
    sys.stdout.write("\n" + UNITTEST_RESULT_MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0




# ---------------------------------------------------------------------------
# 登记表与命令
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Registry:
    """一次空跑用到的全部登记项（由 ``chain_rehearsal_catalog.registry()`` 给出，单测里可以换成合成的）。

    * ``probes``：独立探针——0.163 的历史缺陷与恢复分支故障注入，各自独立，一轮汇总；
    * ``chain``：完整链步骤，有序，沿用“遇首个失败即停”；
    * ``checkpoints``：合法检查点；
    * ``copy_tree``／``inject``：历史重放用的受管树副本与缺陷注入器。
    """

    probes: tuple[Probe, ...]
    chain: tuple[Probe, ...]
    checkpoints: Mapping[str, Checkpoint]
    copy_tree: TreeCopier
    inject: Injector
    # 完整链的范围说明，原样写进完整链汇总（``scope_note``）并打印在表头，避免把它误读成覆盖了整条 VC-0～VC-6。
    chain_note: str = ""


def _select(probes: Sequence[Probe], ids: Sequence[str] | None, phases: Sequence[str] | None) -> list[Probe]:
    selected = list(probes)
    if ids:
        known = {probe.probe_id for probe in probes}
        unknown = [item for item in ids if item not in known]
        if unknown:
            raise RehearsalError(f"没有这些探针：{unknown}")
        selected = [probe for probe in selected if probe.probe_id in set(ids)]
    if phases:
        selected = [probe for probe in selected if probe.phase in set(phases)]
    # 选中的探针依赖的前序探针一并带上（完整链里 chain-vc6-canonical 要求前面的步骤先通过；只挑后面的阶段跑也要把前序
    # 先跑掉，否则它们永远是“前序步骤尚未执行”）。保持登记顺序。
    by_id = {probe.probe_id: probe for probe in probes}
    needed: set[str] = set()
    stack = [probe.probe_id for probe in selected]
    while stack:
        current = stack.pop()
        if current in needed:
            continue
        needed.add(current)
        stack.extend(by_id[current].requires_probes)
    return [probe for probe in probes if probe.probe_id in needed]


def _print_progress(result: ProbeResult) -> None:
    print(f"[{STATUS_LABELS[result.status]}] {result.probe_id} {result.title}（{result.seconds:.1f} 秒）",
          file=sys.stderr, flush=True)


def describe(registry: Registry) -> str:
    """``list`` 子命令：探针、完整链步骤与能力一览。"""

    lines = ["独立探针（一轮查全，各自独立）："]
    for probe in registry.probes:
        flags = "可历史重放" if probe.injections else "无注入器"
        needs = ",".join(probe.needs) or "-"
        lines.append(f"  {probe.probe_id:<34} {probe.phase:<5} {probe.kind:<8} {flags}  需要:{needs}  {probe.title}")
    lines.append("完整链（有序，遇首个失败即停）：")
    if registry.chain_note:
        lines.append(f"  范围：{registry.chain_note}")
    for probe in registry.chain:
        needs = ",".join(probe.needs) or "-"
        lines.append(f"  {probe.probe_id:<34} {probe.phase:<5} 需要:{needs}  {probe.title}")
    lines.append("能力：")
    for name, capability in detect_capabilities().items():
        lines.append(f"  {name:<24} {'可用' if capability.available else '缺失'}  {capability.reason}")
    return "\n".join(lines)


def _default_work_root() -> Path:
    base = Path("/var/tmp") if sys.platform.startswith("linux") else Path(tempfile.gettempdir())
    return Path(tempfile.mkdtemp(prefix="chain-rehearsal-", dir=str(base)))


def default_summary_dir() -> Path:
    """默认汇总目录：不放在会被清理的工作目录里（工作目录默认跑完即删，汇总是要留给人看的），按 UTC 时间戳分目录常驻。"""

    base = Path("/var/tmp") if sys.platform.startswith("linux") else Path(tempfile.gettempdir())
    return base / "chain-rehearsal-summaries" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _prepare_work(arguments: argparse.Namespace) -> Path:
    if arguments.work is not None:
        arguments.work.mkdir(parents=True, exist_ok=True)
        return arguments.work
    return _default_work_root()


def run_round_command(registry: Registry, arguments: argparse.Namespace, work: Path, out: Path) -> dict[str, Any]:
    """一轮查全。"""

    started = utc_now()
    probes = _select(registry.probes, arguments.probe, arguments.phase)
    capabilities = detect_capabilities()
    results = run_round(probes, tree=arguments.tree, work=work / "round", checkpoints=registry.checkpoints,
                        capabilities=capabilities, progress=_print_progress)
    summary = build_summary(results, mode=MODE_ROUND, tree=arguments.tree, capabilities=capabilities,
                            started_at=started, finished_at=utc_now())
    write_summary(summary, out, "round")
    return summary


def run_replay_command(registry: Registry, arguments: argparse.Namespace, work: Path, out: Path) -> dict[str, Any]:
    """历史缺陷重放（自检）。"""

    started = utc_now()
    probes = _select(registry.probes, arguments.probe, arguments.phase)
    capabilities = detect_capabilities()
    results = replay_history(probes, tree=arguments.tree, work=work / "replay", copy_tree=registry.copy_tree,
                             inject=registry.inject, checkpoints=registry.checkpoints, capabilities=capabilities,
                             progress=_print_progress)
    summary = build_summary(results, mode=MODE_REPLAY, tree=arguments.tree, capabilities=capabilities,
                            started_at=started, finished_at=utc_now())
    write_summary(summary, out, "replay-history")
    return summary


def run_chain_command(registry: Registry, arguments: argparse.Namespace, work: Path, out: Path,
                      *, blocked_reason: str | None = None) -> dict[str, Any]:
    """完整链。``blocked_reason`` 不为空表示前面的一轮查全没过、默认不跑：全部步骤如实标“未执行”。"""

    started = utc_now()
    steps = _select(registry.chain, arguments.probe, arguments.phase)
    capabilities = detect_capabilities()
    if blocked_reason is not None:
        results = [_blocked(step, "round", "round", blocked_reason) for step in steps]
    else:
        results = run_round(steps, tree=arguments.tree, work=work / "chain", checkpoints=registry.checkpoints,
                            capabilities=capabilities, stop_on_first_failure=True, progress=_print_progress)
    summary = build_summary(results, mode=MODE_CHAIN, tree=arguments.tree, capabilities=capabilities,
                            started_at=started, finished_at=utc_now(),
                            extra={"scope_note": registry.chain_note} if registry.chain_note else None)
    write_summary(summary, out, "full-chain")
    return summary


def run_rehearse_command(registry: Registry, arguments: argparse.Namespace, work: Path, out: Path) -> dict[str, Any]:
    """开工空跑：先一轮查全并汇总，再另跑一遍完整链确认整体能通。

    一轮查全发现缺陷（或探针出错）时默认不跑完整链——先把汇总里的缺陷一次修完再重跑，完整链要几十分钟到几小时，
    在已知有缺陷的树上跑只会重复撞同一批问题；``--chain-anyway`` 可强制照跑。完整链没跑就如实标“未执行”。
    """

    round_summary = run_round_command(registry, arguments, work, out)
    blocked = None
    if round_summary["verdict"] in {VERDICT_DEFECTS, VERDICT_HARNESS_ERROR} and not arguments.chain_anyway:
        blocked = f"一轮查全结论为 {round_summary['verdict']}，先修完汇总里的缺陷再跑完整链（--chain-anyway 可强制）"
    chain_summary = run_chain_command(registry, arguments, work, out, blocked_reason=blocked)
    verdicts = [round_summary["verdict"], chain_summary["verdict"]]
    order = [VERDICT_HARNESS_ERROR, VERDICT_DEFECTS, VERDICT_INCOMPLETE, VERDICT_CLEAN]
    overall = next(item for item in order if item in verdicts)
    combined = {
        "schema_version": SCHEMA, "mode": "rehearse", "tree": str(arguments.tree), "verdict": overall,
        "round_verdict": round_summary["verdict"], "chain_verdict": chain_summary["verdict"],
        "round_summary_sha256": round_summary["summary_sha256"], "chain_summary_sha256": chain_summary["summary_sha256"],
        "finished_at_utc": utc_now(),
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "rehearse.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render_table(round_summary))
    print(render_table(chain_summary))
    print(f"开工空跑总结论：{overall}（一轮查全 {round_summary['verdict']}；完整链 {chain_summary['verdict']}）")
    return combined


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chain_rehearsal", description="VC-0～VC-6 整链零请求空跑（第 13 项）")
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tree", type=Path, default=REPO_ROOT, help="被测仓库根（默认本仓库根）")
    common.add_argument("--out", type=Path, help="汇总输出目录（默认 /var/tmp/chain-rehearsal-summaries/<UTC>，不随工作目录清理）")
    common.add_argument("--work", type=Path, help="工作目录（默认临时目录；历史重放会在里面复制多棵受管树）")
    common.add_argument("--keep", action="store_true", help="结束后保留工作目录")
    common.add_argument("--probe", action="append", help="只跑指定探针（可重复）")
    common.add_argument("--phase", action="append", help="只跑指定阶段（VC-0…VC-6，可重复）")
    sub.add_parser("list", help="列出探针、完整链步骤与能力")
    sub.add_parser("round", parents=[common], help="一轮查全：全部历史缺陷与恢复分支探针各自独立执行")
    sub.add_parser("replay-history", parents=[common], help="历史缺陷重放：反向打补丁重新引入缺陷，要求探针一轮全部报出")
    sub.add_parser("full-chain", parents=[common], help="完整链：遇首个失败即停，后续标未执行")
    rehearse = sub.add_parser("rehearse", parents=[common], help="开工空跑：先一轮查全，汇总后再跑完整链")
    rehearse.add_argument("--chain-anyway", action="store_true", help="一轮查全发现缺陷时也照跑完整链（默认先修再跑）")
    unit = sub.add_parser("_unittest", help="内部：在被测树里运行指定用例并输出逐用例结果")
    unit.add_argument("ids", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None, *, registry: Registry | None = None) -> int:
    arguments = _build_parser().parse_args(list(argv) if argv is not None else None)
    if arguments.command == "_unittest":
        return _unittest_main(arguments.ids)
    if registry is None:
        from tools.official_client_capture.tests import chain_rehearsal_catalog

        registry = chain_rehearsal_catalog.registry()
    if arguments.command == "list":
        print(describe(registry))
        return 0
    arguments.tree = Path(arguments.tree).resolve()
    work = _prepare_work(arguments)
    out = arguments.out or default_summary_dir()
    try:
        runner = {"round": run_round_command, "replay-history": run_replay_command,
                  "full-chain": run_chain_command, "rehearse": run_rehearse_command}[arguments.command]
        summary = runner(registry, arguments, work, out)
        if arguments.command != "rehearse":
            print(render_table(summary))
        print(f"汇总文件目录：{out}", file=sys.stderr)
        return EXIT_CODES[summary["verdict"]]
    except RehearsalError as error:
        print(f"chain-rehearsal: 用法或登记错误：{error}", file=sys.stderr)
        return 2
    finally:
        if not arguments.keep and arguments.work is None:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
