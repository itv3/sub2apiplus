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


def main() -> int:
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
    ap.add_argument("--log", default=None, help="把 TUI 原始输出写到这个文件")
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

        if args.prompt:
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
