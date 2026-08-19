"""驱动 Claude Code 交互模式（TUI），穿过首次运行引导并提交一次 prompt。

为什么需要：`CLAUDE_CODE_ENTRYPOINT` 由客户端在启动时按模式自设
（`e ? "sdk-cli" : "cli"`），带 `-p` 时即使外部预设 `cli` 也会被改回 `sdk-cli`。
因此 `cli` 入口的出站形态只能靠真正跑交互模式取得。

状态机要点：`❯` 同时可能是菜单光标或主输入框，不能单独作为就绪判据。2.1.226
只有在同一轮渲染同时出现 `❯` 与 `? for shortcuts` 时才是空闲主输入框；登录页
属于认证门禁，必须立即失败关闭，绝不能自动回车或把 prompt 发进登录流程。
"""
import os, pty, re, select, signal, time

ANSI = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[>=78]|\x1b\][^\x07]*(?:\x07|\x1b\\)"
)
# 引导类页面：一律回车接受高亮项／继续
MENU_MARKERS = (
    "Choose the text style", "Press Enter to continue", "Security notes",
    "trust the files", "Do you trust", "I trust this folder", "recommended",
)
AUTH_GATE_MARKERS = (
    "Select login method",
    "Opening browser to sign in",
    "Paste code here if prompted",
)


def clean(b: bytes) -> str:
    return ANSI.sub(b"", b).decode("utf-8", "replace")


def flatten(text: str) -> str:
    """去掉空白后比较。

    TUI 用 `ESC[<n>G` 光标列定位代替空格排版，ANSI 清理后
    `Choose the text style` 会变成 `Choosethetextstyle`，
    直接按原文匹配永远失败。
    """
    return "".join(text.split())


def classify_screen(text: str) -> str:
    """把一轮 TUI 渲染归入可测试的严格状态。"""

    view = flatten(text)
    if any(flatten(marker) in view for marker in AUTH_GATE_MARKERS):
        return "auth_gate"
    if any(flatten(marker) in view for marker in MENU_MARKERS):
        return "menu"
    # Ink 只重绘发生变化的 footer，任务结束时不会再次输出已留在屏幕上的 ``❯``。
    # 因此累积 PTY 字节里可能同时存在旧的 ``esc to interrupt`` 与新的
    # ``? for shortcuts``。以最后一次 footer 标记为当前状态，仍要求本轮流里实际
    # 出现过主输入指针，不能仅凭 shortcut 文案放行。
    shortcut_index = view.rfind("?forshortcuts")
    busy_index = view.rfind("esctointerrupt")
    if "❯" in view and shortcut_index >= 0 and shortcut_index > busy_index:
        return "idle_prompt"
    if busy_index >= 0 and busy_index > shortcut_index:
        return "busy"
    return "waiting"


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
    auth_gate_detected = False

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
        view = clean(bytes(transcript[scan_start:]))
        screen_state = classify_screen(view)

        if screen_state == "auth_gate":
            auth_gate_detected = True
            push("auth-gate-detected")
            break

        if sent_prompt:
            if prompt_at and time.time() - prompt_at > quiet_after_prompt:
                push("response-window-done")
                break
            continue

        # 引导菜单：回车接受默认高亮项，限速避免连续误发
        if screen_state == "menu" and time.time() - last_menu > 2.0:
            os.write(fd, b"\r")
            last_menu = time.time()
            # 后续只扫描本次操作之后的新渲染，避免旧菜单仍留在 transcript
            # 时每两秒重复发送回车。
            scan_from = len(transcript)
            push("enter-menu")
            time.sleep(1.5)
            continue

        if screen_state == "idle_prompt" and time.time() - start > 3:
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
    return {
        "steps": steps,
        "sent_prompt": sent_prompt,
        "auth_gate_detected": auth_gate_detected,
        "transcript": clean(bytes(transcript)),
    }


def drive_sequence(
    claude: str,
    model: str,
    inputs: tuple[str, ...],
    env: dict,
    total_timeout: int = 150,
    quiet_after_last_input: int = 25,
    extra_args: tuple[str, ...] = (),
) -> dict:
    """在同一真实 TUI 会话逐项提交固定输入，供 compact 等跨轮场景使用。"""

    if not inputs or any(not isinstance(value, str) or not value for value in inputs):
        raise ValueError("TUI 固定输入序列不能为空。")
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(claude, [claude, "--model", model, *extra_args], env)
    os.set_blocking(fd, False)
    transcript = bytearray()
    start = time.time()
    deadline = start + total_timeout
    steps: list[tuple[float, str]] = []
    sent: list[str] = []
    last_input_at: float | None = None
    last_menu = 0.0
    scan_from = 0
    auth_gate_detected = False

    def push(tag):
        steps.append((round(time.time() - start, 1), tag))

    while time.time() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.5)
        if readable:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            transcript += chunk
        scan_start = max(scan_from, len(transcript) - 12000)
        view = clean(bytes(transcript[scan_start:]))
        screen_state = classify_screen(view)
        if screen_state == "auth_gate":
            auth_gate_detected = True
            push("auth-gate-detected")
            break
        if len(sent) == len(inputs):
            if last_input_at and time.time() - last_input_at > quiet_after_last_input:
                push("response-window-done")
                break
            continue
        if screen_state == "menu" and time.time() - last_menu > 2.0:
            os.write(fd, b"\r")
            last_menu = time.time()
            scan_from = len(transcript)
            push("enter-menu")
            time.sleep(1.5)
            continue
        if screen_state == "idle_prompt" and time.time() - start > 3:
            value = inputs[len(sent)]
            os.write(fd, value.encode())
            time.sleep(0.4)
            os.write(fd, b"\r")
            sent.append(value)
            last_input_at = time.time()
            scan_from = len(transcript)
            push(f"sent-input-{len(sent)}")
            # 下一项必须等待本次提交之后重新渲染输入框，不能复用旧光标。
            time.sleep(1.0)

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
    return {
        "steps": steps,
        "sent_inputs": sent,
        "all_inputs_sent": tuple(sent) == inputs,
        "auth_gate_detected": auth_gate_detected,
        "transcript": clean(bytes(transcript)),
    }


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
