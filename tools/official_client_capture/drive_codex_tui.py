#!/usr/bin/env python3
"""用伪终端驱动 Codex TUI，触发 `codex exec` 走不到的链路。

存在的理由
----------
斜杠命令只在 TUI 的输入框解析（`tui/src/bottom_pane/chat_composer.rs`）。
`codex exec` 收到 `/compact` 会当成普通用户文本发给模型，模型"照字面理解"
做段摘要——看着像压缩，其实压缩链路根本没走（规格表 SPEC-EP-024 记了这次
失败尝试）。

因此要采 `CompactionReason::UserRequested`，只能真的开一个 TUI 并往输入框里
敲字。TUI 是全屏 ncurses 应用，必须有真伪终端（isatty 为真），管道不行。

判定就绪的方式
--------------
TUI 输出充满 ANSI 转义与重绘，逐帧解析不现实也不必要。这里只做两件事：
  - 剥掉 ANSI 序列后在**可见文本**里找关键字（提示符、结束标志）
  - 找不到就按超时兜底，继续下一步

因为最终判据不是 TUI 的显示内容，而是**中继抓到的字节**——TUI 只是触发器。
"""

from __future__ import annotations

import argparse
import os
import pty
import re
import select
import signal
import sys
import time
from typing import NamedTuple

ANSI = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]"   # CSI（含 `\x1b[0 q` 这类带中间字节的）
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b[78]"                    # DECSC / DECRC —— TUI 逐字符渲染时包在每个字符两侧
    rb"|\x1b[()][B0]"                # 字符集切换
    rb"|\x1b[=><]"                   # 键盘/光标模式
    rb"|[\r\x00-\x08\x0b\x0c\x0e-\x1f]"  # 裸控制符
)
# TUI 每渲染一个字符就换行+存取光标，剥完后满屏是被空白隔开的单字。
# 判关键字前先把空白压掉，否则 "READY" 在文本里长这样：" R \n E \n A \n D \n Y "。
SPACE = re.compile(r"[ \t\n]+")


def visible(buf: bytes, collapse: bool = False) -> str:
    """剥掉 ANSI 控制序列。collapse=True 时连空白一并压掉，供关键字匹配用。"""
    text = ANSI.sub(b" ", buf).decode("utf-8", "replace")
    return SPACE.sub("", text) if collapse else SPACE.sub(" ", text)


class Tui:
    def __init__(self, argv: list[str], env: dict[str, str], log_path: str | None):
        self.argv = argv
        self.env = env
        self.buf = b""
        self.log = open(log_path, "wb") if log_path else None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # 子进程：become the TUI
            os.execvpe(argv[0], argv, env)

    def pump(self, seconds: float) -> str:
        """读 `seconds` 秒输出，返回本段可见文本。"""
        end = time.monotonic() + seconds
        chunk = b""
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            chunk += data
            self.buf += data
            if self.log:
                self.log.write(data)
                self.log.flush()
        return visible(chunk)

    def wait_for(self, needles: list[str], timeout: float, label: str) -> bool:
        """等关键字出现，纯属**提前结束等待**的优化。

        TUI 走 alternate screen + 局部重绘，pty 拿到的是增量帧，想据此可靠判断
        "模型答完了没"并不现实。所以这里匹配不到就等满超时继续——真正的判据是
        中继抓到的字节，不是屏幕上显示了什么。
        """
        end = time.monotonic() + timeout
        seen = ""
        while time.monotonic() < end:
            self.pump(1.0)
            seen = visible(self.buf, collapse=True)  # 压掉空白再匹配
            for n in needles:
                if n.replace(" ", "") in seen:
                    print(f"    [{label}] 命中 {n!r}，提前继续", flush=True)
                    return True
        print(f"    [{label}] 等满 {timeout:.0f}s（未见 {needles}），按计划继续", flush=True)
        return False

    def send(self, text: str, enter: bool = True) -> None:
        os.write(self.fd, text.encode())
        if enter:
            time.sleep(0.3)  # 让 TUI 把字符渲染进输入框再回车
            os.write(self.fd, b"\r")

    def send_keys(self, raw: bytes) -> None:
        """发原始按键字节（不补回车）。Alt 组合键在终端里是 ESC 前缀加字符。"""
        os.write(self.fd, raw)

    def close(self) -> None:
        try:
            os.write(self.fd, b"\x03")  # Ctrl+C
            time.sleep(0.5)
            os.write(self.fd, b"\x03")
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            pass
        if self.log:
            self.log.close()


# ---------------------------------------------------------------------------
# 有序步骤（--step）：同一个 TUI 进程内按顺序做“会中途改设置”的动作
# ---------------------------------------------------------------------------
# 为什么需要：0.165 起 reasoning_effort_override 默认开启，官方客户端在会话中途改推理强度、换模型、
# 压缩后继续对话，请求形态各不相同（configuration_update 项、整段重放等），采这些场景需要在一个
# 会话里依次做“发提示词 → 改设置 → 再发提示词”。`--prompt` 只能连发普通提示词，无法穿插设置动作。
#
# 步骤写法：`动作[@保持秒数][:参数]`，保持秒数缺省时按动作类别取默认值。
#   prompt[@秒]:文本        发一条普通提示词，随后保持（默认 --prompt-hold）
#   effort[@秒]:档位        发 `/model --reasoning=档位`，只改推理强度（默认 --setting-hold）
#   model[@秒]:模型[,档位]  发 `/model 模型 [--reasoning=档位]`，中途换模型（默认 --setting-hold）
#   effort-up[@秒]          按 Alt+. 提高一档推理强度（键盘快捷键路径，默认 --setting-hold）
#   effort-down[@秒]        按 Alt+, 降低一档推理强度
#   compact[@秒]            发 `/compact`，随后保持（默认 --slash-hold）
#   slash[@秒]:命令         发任意斜杠命令原文（默认 --slash-hold）
#   wait@秒 或 wait:秒      只等待，不输入
# `/model` 的内联参数形态来自 TUI 源码 chatwidget/model_command.rs：位置参数是模型，
# `--reasoning=<level>` 是推理强度；没给模型时沿用当前模型。快捷键见 chatwidget/reasoning_shortcuts.rs。

EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MODEL_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STEP_RE = re.compile(r"^(?P<kind>[a-z][a-z-]*)(?:@(?P<hold>[0-9]+(?:\.[0-9]+)?))?(?::(?P<arg>.*))?$", re.DOTALL)
# 动作 → 默认保持时长的类别：prompt 用 --prompt-hold，setting 用 --setting-hold，slash 用 --slash-hold。
STEP_KINDS = {
    "prompt": "prompt",
    "effort": "setting",
    "model": "setting",
    "effort-up": "setting",
    "effort-down": "setting",
    "compact": "slash",
    "slash": "slash",
    "wait": "wait",
}
# 需要参数的动作与必须没有参数的动作。
STEP_NEEDS_ARG = {"prompt", "effort", "model", "slash"}
STEP_NO_ARG = {"effort-up", "effort-down", "compact"}
# Alt 组合键的终端编码：ESC 加字符（xterm 的 metaSendsEscape 行为，crossterm 解析为 Alt+字符）。
ALT_PERIOD = b"\x1b."
ALT_COMMA = b"\x1b,"


class Step(NamedTuple):
    """一个已校验的步骤：kind 是动作名，arg 是参数，hold 为 None 表示用该类别的默认保持时长。

    用 NamedTuple 而不是 dataclass：测试与探测会用 importlib 按文件路径加载本脚本且不登记进
    sys.modules，dataclass 在这种加载方式下会因找不到模块而报错。
    """

    kind: str
    arg: str | None
    hold: float | None


def parse_step(spec: str) -> Step:
    """解析并严格校验一条 `--step`；写错的步骤宁可当场报错，也不要让 TUI 对着一条残缺命令空等。"""
    matched = STEP_RE.match(spec)
    if not matched:
        raise ValueError(f"无法解析步骤：{spec!r}")
    kind, arg = matched.group("kind"), matched.group("arg")
    hold = float(matched.group("hold")) if matched.group("hold") is not None else None
    if kind not in STEP_KINDS:
        raise ValueError(f"未知步骤动作 {kind!r}（可用：{', '.join(sorted(STEP_KINDS))}）")
    if kind in STEP_NO_ARG and arg:
        raise ValueError(f"步骤 {kind} 不接受参数：{spec!r}")
    if kind in STEP_NEEDS_ARG and not (arg and arg.strip()):
        raise ValueError(f"步骤 {kind} 需要参数：{spec!r}")
    if kind == "effort":
        arg = arg.strip()
        if arg not in EFFORT_LEVELS:
            raise ValueError(f"effort 档位 {arg!r} 不在 {EFFORT_LEVELS} 内")
    elif kind == "model":
        model, _, level = arg.strip().partition(",")
        if not MODEL_SLUG.match(model):
            raise ValueError(f"model 步骤的模型名不合法：{model!r}")
        if level and level not in EFFORT_LEVELS:
            raise ValueError(f"model 步骤的档位 {level!r} 不在 {EFFORT_LEVELS} 内")
        arg = f"{model},{level}" if level else model
    elif kind == "slash":
        arg = arg.strip()
        if not arg.startswith("/"):
            raise ValueError(f"slash 步骤的命令必须以 / 开头：{spec!r}")
    elif kind == "wait":
        # wait 的秒数既可写 wait@30 也可写 wait:30；两者都没有则报错。
        if hold is None:
            if arg is None or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", arg.strip()):
                raise ValueError(f"wait 步骤需要秒数（wait@30 或 wait:30）：{spec!r}")
            hold, arg = float(arg.strip()), None
        elif arg:
            raise ValueError(f"wait 步骤只能给一个秒数：{spec!r}")
    return Step(kind=kind, arg=arg, hold=hold)


def parse_steps(specs: list[str]) -> list[Step]:
    return [parse_step(spec) for spec in specs]


def step_typed_text(step: Step) -> str | None:
    """需要在输入框里敲字并回车的步骤返回要敲的文本；按键步骤和等待步骤返回 None。"""
    if step.kind == "prompt":
        return step.arg
    if step.kind == "effort":
        return f"/model --reasoning={step.arg}"
    if step.kind == "model":
        model, _, level = (step.arg or "").partition(",")
        return f"/model {model} --reasoning={level}" if level else f"/model {model}"
    if step.kind == "compact":
        return "/compact"
    if step.kind == "slash":
        return step.arg
    return None


def step_raw_keys(step: Step) -> bytes | None:
    """按键步骤要发送的原始字节；其余步骤返回 None。"""
    if step.kind == "effort-up":
        return ALT_PERIOD
    if step.kind == "effort-down":
        return ALT_COMMA
    return None


def step_hold_seconds(step: Step, prompt_hold: float, setting_hold: float, slash_hold: float) -> float:
    if step.hold is not None:
        return step.hold
    category = STEP_KINDS[step.kind]
    return {"prompt": prompt_hold, "setting": setting_hold, "slash": slash_hold, "wait": 0.0}[category]


def run_steps(tui, steps: list[Step], prompt_hold: float, setting_hold: float, slash_hold: float,
              sleep=time.sleep) -> None:
    """按顺序执行步骤。tui 只需提供 send／send_keys／pump，单测里用替身验证调用顺序，不需要真终端。"""
    for index, step in enumerate(steps, start=1):
        label = step.kind if step.arg is None else f"{step.kind}:{step.arg}"
        hold = step_hold_seconds(step, prompt_hold, setting_hold, slash_hold)
        print(f"--- 步骤 {index}/{len(steps)}：{label}（保持 {hold:g} 秒）---", flush=True)
        text = step_typed_text(step)
        keys = step_raw_keys(step)
        if text is not None:
            tui.send(text)
        elif keys is not None:
            tui.send_keys(keys)
        tui.pump(hold)
        sleep(2)
    print("--- 步骤阶段结束 ---", flush=True)


def main() -> int:
    # TUI 日志可能包含用户输入和工具参数，创建时即限制为当前用户可见。
    os.umask(0o077)
    ap = argparse.ArgumentParser()
    ap.add_argument("--codex-bin", default="/root/.local/bin/codex")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--cwd", default="/tmp/tui-probe")
    ap.add_argument("--context-window", type=int, default=0,
                    help="非 0 则加 -c model_context_window=N")
    ap.add_argument("--warmup", default="请只回复 READY。",
                    help="先跑一轮，让会话里有可压缩的内容")
    ap.add_argument("--slash", default="/compact", help="要触发的斜杠命令")
    ap.add_argument("--warmup-ready", default="READY",
                    help="预热轮的完成标志（要与 --warmup 的提示词对应，"
                         "写错会白等满 timeout）")
    ap.add_argument("--slash-hold", type=float, default=75,
                    help="斜杠命令后保持多久。子代理（/review）要跑完整条链，"
                         "比 /compact 久得多；给短了只能采到发起帧")
    ap.add_argument("--prompt", action="append", default=[],
                    help="在同一个 TUI 进程内依次发送普通提示词，可重复。"
                         "指定后不再执行预热与斜杠命令流程")
    ap.add_argument("--prompt-hold", type=float, default=120,
                    help="每条普通提示词发送后保持多久")
    ap.add_argument("--step", action="append", default=[],
                    help="同一个 TUI 进程内按顺序执行的步骤，可重复，写法 `动作[@保持秒数][:参数]`："
                         "prompt:文本、effort:档位、model:模型[,档位]、effort-up、effort-down、compact、"
                         "slash:/命令、wait@秒。指定后不再执行 --prompt 与预热／斜杠流程，也不能与 --prompt 同用")
    ap.add_argument("--setting-hold", type=float, default=8,
                    help="--step 里设置类动作（effort／model／effort-up／effort-down）之后保持多久")
    ap.add_argument("--log", default=None, help="把 TUI 原始输出写到这个文件")
    ap.add_argument("--no-daemon", action="store_true",
                    help="传给 codex 的 --no-daemon：不经共享后台 daemon，用内嵌 app-server。"
                         "0.164.0 起 daemon 兼容检查不再有功能开关白名单，任何 --disable／--enable 都会让 TUI 去核对 "
                         "daemon 的功能状态，不一致即报错退出；要用 --disable 关掉 plugins／apps 等非必要流量的"
                         "非 daemon 场景必须显式带它")
    ap.add_argument("--disable", action="append", default=[],
                    help="传给 codex 的 --disable <FEATURE>，可重复")
    ap.add_argument("--enable", action="append", default=[],
                    help="传给 codex 的 --enable <FEATURE>，可重复。"
                         "采默认关闭的 feature（如 token_budget）时必需")
    ap.add_argument("--config", action="append", default=[],
                    help="传给 codex 的 -c <KEY=VALUE>，可重复")
    ap.add_argument("--no-bypass", action="store_true",
                    help="不使用 bypass；用于触发需要审批的 guardian 链")
    ap.add_argument("--approval-policy", default="on-request",
                    choices=["untrusted", "on-request", "never"],
                    help="--no-bypass 时采用的审批策略")
    ap.add_argument("--sandbox-mode", default="workspace-write",
                    choices=["read-only", "workspace-write", "danger-full-access"],
                    help="--no-bypass 时采用的沙箱模式")
    args = ap.parse_args()

    # 步骤先于启动校验：写错的步骤当场退出，不要先起一个 TUI 再失败。
    try:
        steps = parse_steps(args.step)
    except ValueError as error:
        print(f"步骤参数错误：{error}", flush=True)
        return 2
    if steps and args.prompt:
        print("步骤参数错误：--step 与 --prompt 不能同时使用（把提示词写成 prompt:文本 步骤）", flush=True)
        return 2

    os.makedirs(args.cwd, exist_ok=True)
    os.chdir(args.cwd)

    # TUI 的参数集与 exec 不同：没有 --skip-git-repo-check（那是 exec 专有的），
    # 传了会当场 usage 报错退出。--cd 用来替代"在某目录下启动"。
    argv = [args.codex_bin, "--model", args.model, "--cd", args.cwd]
    if args.no_bypass:
        argv += ["--ask-for-approval", args.approval_policy,
                 "--sandbox", args.sandbox_mode]
    else:
        argv += ["--dangerously-bypass-approvals-and-sandbox"]
    # 显式走内嵌 app-server（见 --no-daemon 的说明）。clap 不看选项顺序，放在 --disable／--enable 之前即可。
    if args.no_daemon:
        argv += ["--no-daemon"]
    # 关掉某个 feature——用于验证 legacy compact 是否可达：
    # tasks/compact.rs:40 的分派是 supports_remote_compaction() 为真进外层，
    # 再由 Feature::RemoteCompactionV2 为**假**才走 legacy。
    for f in (args.disable or []):
        argv += ["--disable", f]
    for f in (args.enable or []):
        argv += ["--enable", f]
    for config_override in (args.config or []):
        argv += ["-c", config_override]
    if args.enable:
        # ⚠ 打开任何 `Stage::UnderDevelopment` 的 feature，TUI 会先弹一条
        # "Under-development features enabled: … may behave unpredictably"
        # 警告横幅并**等待确认**，预热轮的提示词根本发不进去
        # （实测 relay-tokenbudget1/2 都卡在这里，全程零 responses 请求）。
        # 该开关是官方标准配置项（`core/src/config/mod.rs:1059`）。
        argv += ["-c", "suppress_unstable_features_warning=true"]
    if args.context_window:
        argv += ["-c", f"model_context_window={args.context_window}"]

    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")

    print(f"启动 TUI: {' '.join(argv)}", flush=True)
    tui = Tui(argv, env, args.log)
    try:
        # 1) 等 TUI 起来。
        #
        # 判据必须先排除 usage 报错——参数写错时 clap 打印的 usage 里同样含
        # "codex" 和 ">"，宽松的关键字匹配会把"进程已经崩了"误判成"已就绪"，
        # 后续每一步都在对着一个死进程发字符。首版就栽在这里。
        tui.pump(3.0)
        early = visible(tui.buf, collapse=True)
        if "error:" in early or "Usage:" in early:
            print("启动失败——TUI 报了 usage 错误：", flush=True)
            print("   ", " ".join(early.split())[:300], flush=True)
            return 2
        tui.wait_for(["Codex", "▌", "⏎"], 30, "启动")
        time.sleep(2)

        if steps:
            run_steps(tui, steps, args.prompt_hold, args.setting_hold, args.slash_hold)
        elif args.prompt:
            # 同一进程、同一会话连续触发同类工具，专门用于判断第二次调用是否复用连接。
            # 固定等待而不猜屏幕状态：真正完成与否最终仍由中继字节判定。
            for index, prompt in enumerate(args.prompt, start=1):
                print(f"--- 普通提示词 {index}/{len(args.prompt)}：{prompt} ---", flush=True)
                tui.send(prompt)
                tui.pump(args.prompt_hold)
                time.sleep(2)
            print("--- 普通提示词阶段结束 ---", flush=True)
        else:
            # 2) 先跑一轮，让会话里有内容可压
            print(f"--- 预热轮：{args.warmup} ---", flush=True)
            tui.send(args.warmup)
            tui.wait_for([args.warmup_ready, args.warmup_ready.lower()], 90, "预热")
            time.sleep(3)

            # 3) 真正要测的斜杠命令
            print(f"--- 发送 {args.slash} ---", flush=True)
            tui.send(args.slash)
            # 压缩要跑一个完整的模型往返，给足时间；子代理链更长，用 --slash-hold 调
            tui.pump(args.slash_hold)
            print("--- 斜杠命令阶段结束 ---", flush=True)
    finally:
        tui.close()

    text = visible(tui.buf)
    print(f"\nTUI 可见文本长度：{len(text)}", flush=True)
    for kw in ("compact", "Compact", "压缩", "summar", "Summar", "context"):
        if kw in text:
            print(f"  出现关键字：{kw!r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
