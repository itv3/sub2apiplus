#!/usr/bin/env python3
"""采集容器内 Codex app-server daemon 的生命周期管理（0.157.0 起 TUI 默认经 daemon 发请求）。

背景
----
0.157.0 把 ``daemon_auto_start`` 转为默认开启：不带白名单外 CLI 覆盖（``--disable``、``-c``、
``--enable`` 等）的 PTY TUI 会先把当前客户端包复制到 ``$CODEX_HOME/packages/app-server-daemon``，
再以 ``setsid`` 拉起常驻的 ``codex app-server --listen unix:// --managed-daemon``，此后模型请求都由
daemon 发出。TUI 退出后 daemon 仍驻留：每 270 秒刷新一次模型目录，默认还会拉起 updater 定时下载并
执行安装脚本。这些请求一旦落在采集窗口之外，既会混进后续作业的中继样本，也可能在 hosts 还原后直连
真实上游，因此 daemon 必须由作业自己拉起、自己停掉，并证明没有残留。

本工具只服务于 ``run_official_relay_scenario.sh``，全部子命令都在采集容器内执行：

* ``sweep``：每个中继作业启动中继之前扫描容器。归属本工具命名空间（``<homes-parent>/.codex-daemon-*``）
  的进程只可能是先前异常退出的 daemon 作业留下的，按进程终止并删除对应 home（home 里有 auth.json
  副本，不能留存）；归属其它 CODEX_HOME 的 daemon／updater 说明有人在容器里手工启动过客户端，
  失败关闭交人工核查，本工具不动它。
* ``prepare``：为 daemon 作业建独立 CODEX_HOME。冷启动：不含任何 daemon 状态与模型缓存；复制
  auth.json 与 config.toml，另外复制 installation_id（请求元数据中的安装标识与其余作业一致）、
  version.json（更新检查缓存与其余 TUI 作业一致）与 .sandbox_migration（不触发一次性迁移）。
  功能开关写进 config.toml 的 ``[features]`` 表而不走 ``--disable``，因为白名单外的 CLI 覆盖会让
  TUI 退回内嵌模式；``app-server-daemon/settings.json`` 关闭自动更新并把停机宽限设为 10 秒。
* ``status``：以 ``codex app-server daemon version`` 与进程表判定本作业实际运行模式。
* ``stop``：``codex app-server daemon stop`` 优雅停止后，再终止该 home 名下仍存活的全部进程
  （含 updater 进程组），核验无残留。
* ``remove``：核验无残留后删除 home。

home 选在 ``/root`` 而不是 ``/tmp``：客户端以 ``std::env::temp_dir()`` 为界，CODEX_HOME 落在临时目录下
时拒绝创建 helper 别名并在 TUI 与 daemon 的 stderr 打印告警，与默认用户路径不一致。

每个子命令向 stdout 输出一行 JSON（不含任何凭据）。退出码：0 成功；3 生命周期条件不成立；
2 参数非法。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "codex-daemon-lifecycle/v1"
DEFAULT_HOMES_PARENT = Path("/root")
DEFAULT_SOURCE_HOME = Path("/root/.codex")
DEFAULT_PROC_ROOT = Path("/proc")
HOME_NAME_RE = re.compile(r"^\.codex-daemon-[A-Za-z0-9._-]+$")
FEATURE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
REQUIRED_FILES = ("auth.json",)
OPTIONAL_FILES = ("installation_id", "version.json", ".sandbox_migration")
DAEMON_SETTINGS = {"updater": {"autoUpdateEnabled": False}, "shutdownGraceSeconds": 10}
# daemon 包的安装位置固定在 CODEX_HOME 之下；进程 exe 落在这里即可反推所属 home。
PACKAGE_MARKER = "/packages/app-server-daemon/"
TERMINATE_GRACE_SECONDS = 15.0
KILL_GRACE_SECONDS = 5.0
POLL_SECONDS = 0.2
CLI_TIMEOUT_SECONDS = 90
STDERR_TAIL = 400


class LifecycleError(RuntimeError):
    """生命周期条件不成立（退出码 3）。"""


@dataclasses.dataclass(frozen=True)
class CodexProcess:
    """容器内一条与 Codex daemon 相关的进程；只保留判定所需字段，不保留环境变量原文。"""

    pid: int
    pgid: int
    role: str
    codex_home: str | None
    exe: str

    def summary(self) -> dict[str, Any]:
        return {"pid": self.pid, "role": self.role, "codex_home": self.codex_home}


# ---------------------------------------------------------------------------
# 进程表
# ---------------------------------------------------------------------------


def _role(argv: tuple[str, ...]) -> str:
    if "pid-update-loop" in argv:
        return "updater"
    if "app-server" in argv and "--managed-daemon" in argv:
        return "daemon"
    return "other"


def _home_from_exe(exe: str) -> str | None:
    if PACKAGE_MARKER in exe:
        return exe.split(PACKAGE_MARKER, 1)[0] or None
    return None


def read_process(proc_root: Path, pid: int) -> CodexProcess | None:
    """读取一条进程；僵尸、已退出或读不到的进程返回 None。"""

    base = proc_root / str(pid)
    try:
        stat = (base / "stat").read_text(encoding="utf-8", errors="replace")
        raw_argv = (base / "cmdline").read_bytes()
    except OSError:
        return None
    tail = stat[stat.rfind(")") + 2 :].split()
    if len(tail) < 3 or tail[0] in {"Z", "X"}:
        return None
    try:
        pgid = int(tail[2])
    except ValueError:
        return None
    argv = tuple(part.decode("utf-8", "replace") for part in raw_argv.split(b"\0") if part)
    try:
        environ = (base / "environ").read_bytes()
    except OSError:
        environ = b""
    codex_home = None
    for item in environ.split(b"\0"):
        if item.startswith(b"CODEX_HOME="):
            codex_home = item[len(b"CODEX_HOME=") :].decode("utf-8", "replace") or None
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = ""
    exe = exe.removesuffix(" (deleted)")
    return CodexProcess(
        pid=pid,
        pgid=pgid,
        role=_role(argv),
        codex_home=codex_home or _home_from_exe(exe),
        exe=exe,
    )


def scan(proc_root: Path) -> list[CodexProcess]:
    """列出与 daemon 相关、或带 CODEX_HOME 环境变量的全部存活进程（不含本进程）。"""

    own = os.getpid()
    found = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        process = read_process(proc_root, int(entry.name))
        if process is None:
            continue
        if process.role != "other" or process.codex_home is not None or PACKAGE_MARKER in process.exe:
            found.append(process)
    return sorted(found, key=lambda item: item.pid)


def owned_by(processes: Iterable[CodexProcess], home: Path) -> list[CodexProcess]:
    """归属某个 home 的进程：环境变量 CODEX_HOME 等于它，或可执行文件位于它之下。"""

    home_text = str(home)
    return [
        process
        for process in processes
        if process.codex_home == home_text or process.exe.startswith(home_text + "/")
    ]


def signal_group(pgid: int, sig: signal.Signals) -> None:
    os.killpg(pgid, sig)


def signal_process(pid: int, sig: signal.Signals) -> None:
    os.kill(pid, sig)


def terminate(proc_root: Path, home: Path, *, grace: float, kill_grace: float) -> dict[str, Any]:
    """终止某个 home 名下的全部进程：先 SIGTERM，宽限期满仍存活再 SIGKILL。

    只对组长本身属于该 home 的进程组发组信号（updater 的安装子进程与它同组）；其余进程逐个发信号，
    避免波及同组的无关进程。
    """

    targets = owned_by(scan(proc_root), home)
    report: dict[str, Any] = {"terminated": [item.summary() for item in targets], "killed": []}
    if not targets:
        return report

    def send(sig: signal.Signals, processes: list[CodexProcess]) -> None:
        pids = {item.pid for item in processes}
        for pgid in sorted({item.pgid for item in processes if item.pgid in pids and item.pgid > 1}):
            try:
                signal_group(pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in sorted(pids):
            try:
                signal_process(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def wait(seconds: float) -> list[CodexProcess]:
        deadline = time.monotonic() + seconds
        while True:
            remaining = owned_by(scan(proc_root), home)
            if not remaining or time.monotonic() >= deadline:
                return remaining
            time.sleep(POLL_SECONDS)

    send(signal.SIGTERM, targets)
    remaining = wait(grace)
    if remaining:
        report["killed"] = [item.summary() for item in remaining]
        send(signal.SIGKILL, remaining)
        wait(kill_grace)
    return report


# ---------------------------------------------------------------------------
# home 与配置
# ---------------------------------------------------------------------------


def validate_home(home: Path, homes_parent: Path) -> Path:
    """home 只能是 ``<homes-parent>/.codex-daemon-<名字>``，防止误删其它目录。"""

    if not home.is_absolute() or home.parent != homes_parent or not HOME_NAME_RE.fullmatch(home.name):
        raise ValueError(f"daemon home 必须是 {homes_parent}/.codex-daemon-<名字>：{home}")
    return home


def parse_features(text: str) -> list[str]:
    names = text.split()
    for name in names:
        if not FEATURE_NAME_RE.fullmatch(name):
            raise ValueError(f"功能开关名非法：{name}")
    if len(set(names)) != len(names):
        raise ValueError("功能开关名重复")
    return names


def render_config(source_text: str, disabled: list[str]) -> str:
    """在源 config.toml 末尾追加 ``[features]`` 表；源文件已含 features 时失败关闭（无法无损合并）。"""

    parsed = tomllib.loads(source_text)
    if "features" in parsed:
        raise LifecycleError("源 config.toml 已含 features 配置，无法无损追加 [features] 表")
    if not disabled:
        return source_text
    rendered = source_text.rstrip("\n") + "\n\n[features]\n" + "".join(f"{name} = false\n" for name in disabled)
    if tomllib.loads(rendered).get("features") != {name: False for name in disabled}:
        raise LifecycleError("追加的 [features] 表解析结果与预期不一致")
    return rendered


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def prepare(home: Path, source_home: Path, disabled: list[str]) -> dict[str, Any]:
    if home.exists() or home.is_symlink():
        raise LifecycleError(f"daemon home 已存在，拒绝复用：{home}")
    for name in (*REQUIRED_FILES, "config.toml"):
        if not _regular_file(source_home / name):
            raise LifecycleError(f"源 CODEX_HOME 缺少 {name}")
    config_text = render_config((source_home / "config.toml").read_text(encoding="utf-8"), disabled)
    home.mkdir(mode=0o700)
    copied = []
    for name in (*REQUIRED_FILES, *OPTIONAL_FILES):
        source = source_home / name
        if _regular_file(source):
            _write_private(home / name, source.read_bytes())
            copied.append(name)
    _write_private(home / "config.toml", config_text.encode("utf-8"))
    (home / "app-server-daemon").mkdir(mode=0o700)
    _write_private(
        home / "app-server-daemon" / "settings.json",
        (json.dumps(DAEMON_SETTINGS, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"),
    )
    return {
        "home": str(home),
        "copied_files": copied,
        "features": {name: False for name in disabled},
        "settings": DAEMON_SETTINGS,
    }


# ---------------------------------------------------------------------------
# 客户端命令
# ---------------------------------------------------------------------------


def run_cli(codex_bin: str, home: Path, *arguments: str) -> dict[str, Any]:
    """以该 home 为 CODEX_HOME 运行 ``codex app-server daemon <子命令>``，只保留结构化结论。"""

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    try:
        completed = subprocess.run(
            [codex_bin, "app-server", "daemon", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLI_TIMEOUT_SECONDS,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"returncode": None, "error": type(error).__name__, "output": None, "stderr_tail": ""}
    output = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                output = value
                break
    return {
        "returncode": completed.returncode,
        "output": output,
        "stderr_tail": completed.stderr.strip()[-STDERR_TAIL:],
    }


def _version_fields(output: dict[str, Any] | None) -> dict[str, Any]:
    output = output or {}
    return {
        key: output.get(key)
        for key in ("status", "backend", "cliVersion", "appServerVersion", "managedCodexVersion")
    }


def status(proc_root: Path, codex_bin: str, home: Path) -> dict[str, Any]:
    version = run_cli(codex_bin, home, "version")
    fields = _version_fields(version["output"])
    processes = owned_by(scan(proc_root), home)
    daemon_pids = [item.pid for item in processes if item.role == "daemon"]
    running = version["returncode"] == 0 and fields["status"] == "running" and bool(daemon_pids)
    return {
        "home": str(home),
        "mode": "daemon" if running else "embedded",
        "version_returncode": version["returncode"],
        "daemon": fields,
        "processes": [item.summary() for item in processes],
        "stderr_tail": version["stderr_tail"],
    }


def stop(proc_root: Path, codex_bin: str, home: Path, *, grace: float, kill_grace: float) -> dict[str, Any]:
    cli = run_cli(codex_bin, home, "stop")
    report = terminate(proc_root, home, grace=grace, kill_grace=kill_grace)
    remaining = owned_by(scan(proc_root), home)
    return {
        "home": str(home),
        "cli_stop": {
            "returncode": cli["returncode"],
            "status": (cli["output"] or {}).get("status"),
            "stderr_tail": cli["stderr_tail"],
        },
        **report,
        "remaining": [item.summary() for item in remaining],
    }


def remove(proc_root: Path, home: Path) -> dict[str, Any]:
    remaining = owned_by(scan(proc_root), home)
    if remaining:
        raise LifecycleError(f"home 名下仍有进程，拒绝删除：{[item.pid for item in remaining]}")
    existed = home.exists()
    if existed:
        shutil.rmtree(home)
    return {"home": str(home), "removed": existed}


def sweep(proc_root: Path, homes_parent: Path, *, grace: float, kill_grace: float) -> dict[str, Any]:
    processes = scan(proc_root)

    def ours(process: CodexProcess) -> bool:
        if process.codex_home is None:
            return False
        home = Path(process.codex_home)
        return home.parent == homes_parent and bool(HOME_NAME_RE.fullmatch(home.name))

    foreign = [
        process
        for process in processes
        if not ours(process) and (process.role != "other" or PACKAGE_MARKER in process.exe)
    ]
    if foreign:
        raise LifecycleError(
            "容器内存在不属于采集作业的 Codex daemon／updater，需人工核查后停止："
            + json.dumps([item.summary() for item in foreign], ensure_ascii=False)
        )
    homes = {Path(process.codex_home) for process in processes if ours(process)}
    homes |= {path for path in homes_parent.iterdir() if HOME_NAME_RE.fullmatch(path.name)} if homes_parent.is_dir() else set()
    terminated: list[dict[str, Any]] = []
    killed: list[dict[str, Any]] = []
    removed = []
    for home in sorted(homes):
        report = terminate(proc_root, home, grace=grace, kill_grace=kill_grace)
        terminated += report["terminated"]
        killed += report["killed"]
        if owned_by(scan(proc_root), home):
            raise LifecycleError(f"残留 daemon 进程未能终止：{home}")
        if home.is_symlink() or home.is_file():
            home.unlink()
        elif home.exists():
            shutil.rmtree(home)
        removed.append(home.name)
    return {"terminated": terminated, "killed": killed, "removed_homes": removed}


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proc-root", type=Path, default=DEFAULT_PROC_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--homes-parent", type=Path, default=DEFAULT_HOMES_PARENT, help=argparse.SUPPRESS)
    parser.add_argument("--grace", type=float, default=TERMINATE_GRACE_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--kill-grace", type=float, default=KILL_GRACE_SECONDS, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sweep", help="作业开始前清扫残留 daemon")
    prepare_parser = commands.add_parser("prepare", help="建立独立的冷启动 CODEX_HOME")
    prepare_parser.add_argument("--home", type=Path, required=True)
    prepare_parser.add_argument("--source-home", type=Path, default=DEFAULT_SOURCE_HOME)
    prepare_parser.add_argument("--disable-features", default="", help="空格分隔的功能开关名，写入 [features] 表")
    for name in ("status", "stop"):
        sub = commands.add_parser(name)
        sub.add_argument("--home", type=Path, required=True)
        sub.add_argument("--codex-bin", required=True)
        if name == "status":
            sub.add_argument("--require-mode", choices=("daemon", "embedded"))
            sub.add_argument("--expect-version")
    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("--home", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "command": args.command, "status": "failed"}
    try:
        home = validate_home(args.home, args.homes_parent) if hasattr(args, "home") else None
        if args.command == "sweep":
            result.update(sweep(args.proc_root, args.homes_parent, grace=args.grace, kill_grace=args.kill_grace))
        elif args.command == "prepare":
            result.update(prepare(home, args.source_home, parse_features(args.disable_features)))
        elif args.command == "status":
            result.update(status(args.proc_root, args.codex_bin, home))
            problems = []
            if args.require_mode and result["mode"] != args.require_mode:
                problems.append(f"实际模式 {result['mode']} 不是 {args.require_mode}")
            if args.expect_version and result["daemon"]["appServerVersion"] != args.expect_version:
                problems.append(f"daemon 版本 {result['daemon']['appServerVersion']} 不是 {args.expect_version}")
            if problems:
                raise LifecycleError("；".join(problems))
        elif args.command == "stop":
            result.update(stop(args.proc_root, args.codex_bin, home, grace=args.grace, kill_grace=args.kill_grace))
            if result["remaining"]:
                raise LifecycleError("停止后仍有进程残留")
        elif args.command == "remove":
            result.update(remove(args.proc_root, home))
        result["status"] = "passed"
        code = 0
    except tomllib.TOMLDecodeError as error:
        # TOMLDecodeError 是 ValueError 的子类，必须先于参数错误分支捕获：源配置损坏属于环境条件。
        result["error"] = f"源 config.toml 解析失败：{error}"
        code = 3
    except ValueError as error:
        result["error"] = str(error)
        code = 2
    except LifecycleError as error:
        result["error"] = str(error)
        code = 3
    except OSError as error:
        result["error"] = f"{type(error).__name__}: {error}"
        code = 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
