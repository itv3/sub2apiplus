"""驱动 Claude Code 交互模式（TUI），穿过首次运行引导并提交一次 prompt。

为什么需要：`CLAUDE_CODE_ENTRYPOINT` 由客户端在启动时按模式自设
（`e ? "sdk-cli" : "cli"`），带 `-p` 时即使外部预设 `cli` 也会被改回 `sdk-cli`。
因此 `cli` 入口的出站形态只能靠真正跑交互模式取得。

状态机要点：`❯` 是**菜单光标**不是输入框。把它当输入框就绪的标志会把 prompt
发进菜单，从而错误地走进登录选择页。输入框只认 `│ >`。
"""
import os, pty, re, select, signal, time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[>=78]|\x1b\][^\x07]*\x07")
# 引导类页面：一律回车接受高亮项／继续
MENU_MARKERS = (
    "Choose the text style", "Select login method", "Press Enter to continue",
    "Security notes", "trust the files", "Do you trust", "I trust this folder",
    "recommended",
)
INPUT_READY = ("│ >", "│  >")


def clean(b: bytes) -> str:
    return ANSI.sub(b"", b).decode("utf-8", "replace")


def flatten(text: str) -> str:
    """去掉空白后比较。

    TUI 用 `ESC[<n>G` 光标列定位代替空格排版，ANSI 清理后
    `Choose the text style` 会变成 `Choosethetextstyle`，
    直接按原文匹配永远失败。
    """
    return "".join(text.split())


def drive(
    claude: str,
    model: str,
    prompt: str,
    env: dict,
    total_timeout: int = 75,
    quiet_after_prompt: int = 20,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """运行真实交互入口，并只接受调用方给出的固定参数元组。"""

    pid, fd = pty.fork()
    if pid == 0:
        os.execve(claude, [claude, "--model", model, *extra_args], env)
    os.set_blocking(fd, False)

    transcript = bytearray()
    start = time.time()
    deadline = start + total_timeout
    steps: list[tuple[float, str]] = []
    sent_prompt = False
    prompt_at = None
    last_menu = 0.0
    scan_from = 0

    def push(tag):
        steps.append((round(time.time() - start, 1), tag))

    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
        scan_start = max(scan_from, len(transcript) - 8000)
        view = flatten(clean(bytes(transcript[scan_start:])))

        if sent_prompt:
            if prompt_at and time.time() - prompt_at > quiet_after_prompt:
                push("response-window-done")
                break
            continue

        # 引导菜单：回车接受默认高亮项，限速避免连续误发
        if any(flatten(m) in view for m in MENU_MARKERS) and time.time() - last_menu > 2.0:
            os.write(fd, b"\r")
            last_menu = time.time()
            # 后续只扫描本次操作之后的新渲染，避免旧菜单仍留在 transcript
            # 时每两秒重复发送回车。
            scan_from = len(transcript)
            push("enter-menu")
            time.sleep(1.5)
            continue

        # 2.1.226 主输入框使用 `❯`；早期边框形态仍兼容 `│ >`。
        # 已知菜单在上方先消费，因此这里的裸指针才可视为主输入入口。
        input_ready = any(flatten(m) in view for m in INPUT_READY) or "❯" in view
        if input_ready and time.time() - start > 3:
            os.write(fd, prompt.encode())
            time.sleep(0.4)
            os.write(fd, b"\r")
            sent_prompt = True
            prompt_at = time.time()
            push("sent-prompt")
            continue

    os.write(fd, b"\x03")
    time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGINT)
        time.sleep(0.8)
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except Exception:
        pass
    return {"steps": steps, "sent_prompt": sent_prompt, "transcript": clean(bytes(transcript))}


if __name__ == "__main__":
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    res = drive("/root/.local/bin/claude", "claude-sonnet-5",
                "只回复 TUI_OK，不调用任何工具。", env)
    import sys
    print("步骤:", res["steps"], flush=True)
    print("发出 prompt:", res["sent_prompt"], "| TUI_OK 出现:", "TUI_OK" in res["transcript"])
    print("尾部 400 字符:\n", res["transcript"][-400:], flush=True)
    sys.stdout.flush()
