#!/usr/bin/env python3
"""Claude 客户端启动探测运行器（Claude 升级控制面设计 B2-10）：在采集容器的私有命名空间内完成一次 TUI 启动探测。

与 Codex 运行器（``client_launch_probe_runner.py``）同一种用法：宿主侧 ``client_launch_probe.py --client claude`` 以
``docker exec -i <容器> unshare --net --mount --pid --fork --mount-proc --propagation private python3 - <参数 JSON>`` 把它
经标准输入送进去执行，结果以 ``RESULT_MARKER`` 开头的一行 JSON 打印到 stdout。本文件不在容器里落盘。

隔离与零副作用
--------------
* 网络：新网络命名空间只有回环；本地替身（TLS）在回环 443 端口终结 ``api.anthropic.com`` 与 ``platform.claude.com`` 的
  全部请求，报文出不了命名空间。启动时核对接口集合恰好是 ``lo``。
* 文件：``/tmp``、``/var/tmp``、``/work`` 各叠加一层 tmpfs 覆盖层，``/dev/shm`` 换成私有 tmpfs，``/etc/hosts`` 以私有副本绑定
  挂载。客户端的两个状态位置（``$HOME/.claude/`` 与 ``$HOME/.claude.json``，全局状态路径取
  ``CLAUDE_CONFIG_DIR || homedir()``）都落在覆盖层 ``/work`` 下的私有 HOME ``/work/probe-home``，容器里真实的
  ``/root/.claude`` 与 ``/root/.claude.json`` 不被打开写入（宿主侧另做前后指纹比对）。
* 凭据：不读取真实凭据。私有 HOME 按中继 TUI 临时 HOME（``temporary_claude_tui_state``）的同一布局建：
  ``.claude/.credentials.json`` 是合成的（本地替身不核对令牌），``.claude/settings.json`` 是同一份隐私设置，``.claude.json``
  是全局状态文件的规范化副本（与中继同样补齐 ``theme``、``hasCompletedOnboarding``、``lastOnboardingVersion``）；全局
  状态文件不存在（ARM64 尚未做 Claude OAuth 登录）时从空对象开始，缺 ``oauthAccount`` 时补一份合成占位。``~/.claude``
  的其余内容不复制：作业的 TUI 临时 HOME 里也没有它们。
* 进程：本进程是私有 PID 命名空间的 1 号进程，退出时内核回收命名空间内全部子进程。

判据
----
按作业相同的方式启动 TUI：工作目录与夹具由受管 ``capturelib/claude_fw_f_v4._fixture_paths`` 建（与中继同一函数），
客户端参数与 ``_run_tui_probe`` 的拼法相同，环境与中继 TUI 路径相同（最小白名单、隐私变量、``NODE_EXTRA_CA_CERTS``、私有
HOME），经受管驱动入口 ``claude_drive_tui_cli.py``（同一状态机：引导菜单回车接受、登录页立即停）只把提示词换成探测口令。
替身收到正文含完整口令的 ``POST /v1/messages`` 才算通过。交互屏（信任目录、登录、onboarding、主题选择、模型迁移等）
从会话文本里识别，写进结果作诊断：驱动能自动确认的屏（例如信任目录）不影响判定，拦住首帧的屏让口令进不了输入框，
替身收不到带口令的请求，判为失败。

替身的应答：``POST /v1/messages`` 回 400（不触发重试），其余一律 404。结果只记录主机、方法、路径与头部名称（另有
口令是否出现、正文能否解码与替身自己的应答码），任何头部取值与正文都不写进结果。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Mapping

RESULT_MARKER = "CLIENT_LAUNCH_PROBE_RESULT "
RUN_SCHEMA = "official-client-launch-probe-run/v1"
SCRATCH = Path("/mnt")
PROBE_HOME = Path("/work/probe-home")
PROBE_RUN = Path("/work/probe-run")
SHM = Path("/dev/shm")
DRIVE_CLI = "claude_drive_tui_cli.py"
DRIVE_MODULE = "drive_claude_tui.py"
MESSAGES_PATH = "/v1/messages"
MAX_BODY_BYTES = 64 * 1024 * 1024
# 合成凭据：替身不核对令牌；有效期取远未来，客户端不会在探测期间刷新。
SYNTHETIC_CREDENTIALS = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-oat01-client-launch-probe-synthetic",
        "refreshToken": "sk-ant-ort01-client-launch-probe-synthetic",
        "expiresAt": 4102444800000,
        "scopes": ["user:inference", "user:profile"],
    }
}
# 全局状态缺 oauthAccount 时补的合成占位（不含任何真实账号字段）。
SYNTHETIC_OAUTH_ACCOUNT = {
    "accountUuid": "00000000-0000-4000-8000-000000000000",
    "emailAddress": "client-launch-probe@invalid.example",
    "organizationUuid": "00000000-0000-4000-8000-000000000001",
    "organizationName": "client-launch-probe",
}
# 中继补齐的三项引导状态（capturelib/environment._normalized_tui_global_state）。
ONBOARDING_FIELDS = ("theme", "hasCompletedOnboarding", "lastOnboardingVersion")
# 已知会出现在首帧之前的交互屏（去掉空白与框线后匹配），只用于诊断，不参与判定。
KNOWN_SCREENS = (
    ("trust_directory", ("Doyoutrustthefilesinthisfolder", "Itrustthisfolder", "Trustthisfolder", "trustthefiles", "Quicksafetycheck")),
    ("login", ("Selectloginmethod", "Openingbrowsertosignin", "Pastecodehereifprompted", "Pleaserun/login")),
    ("onboarding", ("Securitynotes", "Let'sgetstarted")),
    ("theme", ("Choosethetextstyle",)),
    ("model_migration", ("modelisnolongeravailable", "isnowthedefaultmodel", "Switchtothenewmodel", "Wehaveupdatedthedefaultmodel")),
    ("api_key_prompt", ("DetectedacustomAPIkey", "DoyouwanttousethisAPIkey")),
)
DECORATION_RE = re.compile("[─-▟•›·❯✻⎿]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+|(?<![A-Za-z0-9-])sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)"
)
TOKEN_RE = re.compile(r"^[A-Z]{16,64}$")
MUTATION_RE = re.compile(r"^untrust:(/[A-Za-z0-9_./-]*)$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@+=-]+$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
LAUNCH_FIELDS = ("fixture", "safe_mode", "debug_log", "cli_args", "injected_env")
CONFIG_FIELDS = (
    "combo_id", "tool_root", "claude_bin", "model", "target_version", "global_state_file", "probe_id", "launch",
    "token", "deadline_seconds", "quiet_after_seconds", "overlay_targets", "stub_hosts", "test_mutations", "diagnostic_window",
)


class ProbeError(RuntimeError):
    """环境搭建失败：探测无法成立，按失败关闭处理。"""


# ---------------------------------------------------------------------------
# 纯函数（离线单测覆盖）
# ---------------------------------------------------------------------------


def redact(text: str) -> str:
    """去掉可能出现在屏幕或驱动输出里的邮箱、JWT、API Key 与 Bearer 凭据。"""

    return SECRET_RE.sub("[REDACTED]", EMAIL_RE.sub("[EMAIL]", text))


def parse_link_names(ip_output: str) -> list[str]:
    """解析 ``ip -o link show`` 的接口名（去掉 ``@peer`` 后缀）。"""

    names = []
    for line in ip_output.splitlines():
        parts = line.split(":", 2)
        if len(parts) >= 2 and parts[1].strip():
            names.append(parts[1].strip().split("@", 1)[0])
    return names


def build_hosts(original: str, hosts: list[str]) -> str:
    """私有 hosts：去掉原有同名映射，把替身负责的域名全部指向回环。"""

    kept = [
        line for line in original.splitlines()
        if not any(name in hosts for name in line.split("#", 1)[0].split()[1:])
    ]
    return "\n".join(kept + [f"127.0.0.1 {host}" for host in hosts]) + "\n"


def workspace_path() -> PurePosixPath:
    """作业的工作目录形状：中继输出根下 ``results/workspace``（探测把输出根放在覆盖层 ``/work/probe-run``）。"""

    return PurePosixPath(str(PROBE_RUN)) / "results" / "workspace"


def untrusted_paths(directory: str, workspace: PurePosixPath) -> list[str]:
    """``untrust:<目录>`` 要撤销信任的路径：该目录与它的全部上级。目录必须是工作目录本身或它的上级，否则注入对探测无效。"""

    target = PurePosixPath(directory)
    if not target.is_absolute() or ".." in target.parts or (target != workspace and target not in workspace.parents):
        raise ProbeError(f"验收变更的目录不是探测工作目录或其上级：{directory}（工作目录 {workspace}）")
    return [str(path) for path in (target, *target.parents)]


def normalized_global_state(
    raw: bytes | None,
    target_version: str,
    *,
    mutations: Iterable[str] = (),
    workspace: PurePosixPath | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """私有 HOME 里的 ``.claude.json``：全局状态的规范化副本（与中继同样补齐引导状态；缺账号时补合成占位）。

    ``untrust:<目录>`` 验收变更撤销该目录及其全部上级在 ``projects`` 里的信任确认，使信任目录交互屏必然出现。返回规范化
    后的字节与只含字段名和布尔事实的摘要（不含任何状态取值）。
    """

    if raw is None:
        state: Any = {}
        source = "absent"
    else:
        try:
            state = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProbeError("全局状态文件不是合法 JSON") from error
        source = "present"
    if not isinstance(state, dict):
        raise ProbeError("全局状态文件顶层必须是对象")
    expected = {"theme": "dark", "hasCompletedOnboarding": True, "lastOnboardingVersion": target_version}
    normalized_fields = []
    for field in ONBOARDING_FIELDS:
        if state.get(field) != expected[field]:
            state[field] = expected[field]
            normalized_fields.append(field)
    account = "global_state"
    if not isinstance(state.get("oauthAccount"), dict) or not state["oauthAccount"]:
        state["oauthAccount"] = dict(SYNTHETIC_OAUTH_ACCOUNT)
        account = "synthetic"
    untrusted: list[str] = []
    for mutation in mutations:
        match = MUTATION_RE.fullmatch(mutation)
        if match is None:
            raise ProbeError(f"不支持的验收变更：{mutation}")
        paths = untrusted_paths(match.group(1), workspace or workspace_path())
        projects = state.get("projects")
        if not isinstance(projects, dict):
            projects = state["projects"] = {}
        for path in paths:
            entry = projects.get(path)
            if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is not False:
                entry["hasTrustDialogAccepted"] = False
                untrusted.append(path)
    payload = (json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    return payload, {
        "source": source,
        "normalized_fields": normalized_fields,
        "oauth_account": account,
        "untrusted_projects": untrusted,
    }


def tui_launch_args(launch: Mapping[str, Any], fixture_args: list[str], debug_path: str | None) -> list[str]:
    """客户端参数：与 ``capturelib/claude_fw_f_v4._run_tui_probe`` 同一拼法（测试逐项对照）。"""

    args = [
        "--no-chrome", "--prompt-suggestions", "false", "--max-budget-usd", "3.00",
        *fixture_args, *[str(item) for item in launch["cli_args"]],
    ]
    if debug_path is not None:
        args.extend(("--debug-file", debug_path))
    if launch["safe_mode"]:
        args.insert(0, "--safe-mode")
    return args


def build_cli_argv(config: Mapping[str, Any], claude_bin: str, workspace: str, transcript: str, args: list[str]) -> list[str]:
    """受管驱动入口的命令行：单次提交探测口令，会话上限取探测截止前 15 秒，提交后再等 ``quiet_after_seconds``。"""

    return [
        "python3", "-B", f"{config['tool_root']}/{DRIVE_CLI}",
        "--claude-bin", claude_bin, "--model", config["model"], "--cwd", workspace,
        "--prompt", config["token"],
        "--total-timeout", str(max(15, int(config["deadline_seconds"]) - 15)),
        "--quiet-after", str(int(config["quiet_after_seconds"])),
        "--transcript", transcript,
        "--", *args,
    ]


def decode_body(encoding: str, body: bytes) -> tuple[bytes, bool]:
    """按 Content-Encoding 解码请求体；解码失败只影响口令判定。"""

    try:
        if encoding in {"", "identity"}:
            return body, True
        if encoding == "gzip":
            return gzip.decompress(body), True
        if encoding == "zstd":
            try:
                from compression import zstd  # Python 3.14+

                return zstd.decompress(body), True
            except ImportError:
                import zstandard  # 采集镜像随 mitmproxy 自带

                return zstandard.ZstdDecompressor().decompressobj().decompress(body), True
    except Exception:  # noqa: BLE001 - 解码失败不影响替身应答
        return b"", False
    return b"", False


def detect_screens(text: str) -> list[str]:
    """在会话文本里识别已知交互屏（先去空白与框线）。"""

    view = DECORATION_RE.sub("", "".join(text.split()))
    return [name for name, needles in KNOWN_SCREENS if any(needle in view for needle in needles)]


def decide_response(method: str, path: str) -> tuple[int, str, bytes]:
    """替身的应答：首个 turn（``POST /v1/messages``）回 400，其余一律 404。"""

    if method == "POST" and path == MESSAGES_PATH:
        return 400, "Bad Request", (
            b'{"type":"error","error":{"type":"invalid_request_error","message":"claude client launch probe stub"}}'
        )
    return 404, "Not Found", b'{"type":"error","error":{"type":"not_found_error","message":"claude client launch probe stub"}}'


def serve_request(reader: BinaryIO, writer: Callable[[bytes], Any], *, token: bytes) -> dict[str, Any] | None:
    """读一条 HTTP/1.1 请求、应答并返回记录（只记主机、方法、路径与头部名称）；连接空闲关闭时返回 None。"""

    request_line = reader.readline(65537).decode("latin-1").rstrip("\r\n")
    if not request_line:
        return None
    method, target, _version = (request_line.split(" ", 2) + ["", ""])[:3]
    headers: dict[str, str] = {}
    names: set[str] = set()
    for _ in range(256):
        line = reader.readline(65537).decode("latin-1").rstrip("\r\n")
        if not line:
            break
        name, _, value = line.partition(":")
        names.add(name.strip().lower())
        headers[name.strip().lower()] = value.strip()
    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size = int(reader.readline(1024).split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                reader.readline(1024)
                break
            body += reader.read(size)
            reader.readline(1024)
            if len(body) > MAX_BODY_BYTES:
                raise ValueError("请求体过大")
    elif headers.get("content-length"):
        length = int(headers["content-length"])
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体长度非法")
        body = reader.read(length)
    decoded, decoded_ok = decode_body(headers.get("content-encoding", "identity").lower(), body)
    path = target.split("?", 1)[0]
    status, reason, payload = decide_response(method, path)
    writer(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n".encode("ascii") + payload
    )
    return {
        "kind": "request",
        "host": headers.get("host", "").split(":", 1)[0],
        "method": method,
        "path": path,
        "header_names": sorted(names),
        "body_decoded": decoded_ok,
        "token_found": bool(decoded_ok and token in decoded),
        "response_status": status,
    }


def is_token_request(record: Mapping[str, Any]) -> bool:
    """判据：正文含完整口令的 ``POST /v1/messages``。"""

    return record.get("method") == "POST" and record.get("path") == MESSAGES_PATH and record.get("token_found") is True


def validate_config(config: Any) -> dict[str, Any]:
    """运行器参数闭集校验：宿主侧生成，这里再失败关闭一次（私有 HOME 与工作目录必须在覆盖层内）。"""

    if not isinstance(config, dict) or set(config) != set(CONFIG_FIELDS):
        raise ProbeError("运行器参数键集合非法")
    for key in ("combo_id", "tool_root", "claude_bin", "model", "target_version", "global_state_file", "probe_id"):
        if not isinstance(config[key], str) or not config[key] or not SAFE_VALUE_RE.fullmatch(config[key]):
            raise ProbeError(f"运行器参数 {key} 非法")
    for key in ("tool_root", "claude_bin", "global_state_file"):
        if not config[key].startswith("/") or ".." in config[key].split("/"):
            raise ProbeError(f"运行器参数 {key} 必须是绝对路径")
    if not VERSION_RE.fullmatch(config["target_version"]):
        raise ProbeError("运行器参数 target_version 必须是三段版本号")
    if not isinstance(config["token"], str) or not TOKEN_RE.fullmatch(config["token"]):
        raise ProbeError("探测口令必须是 16～64 个大写字母（不含数字，避免误选编号选项）")
    for key in ("overlay_targets", "stub_hosts", "test_mutations"):
        value = config[key]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProbeError(f"运行器参数 {key} 必须是非空字符串数组")
    if not config["stub_hosts"] or not config["overlay_targets"]:
        raise ProbeError("替身域名与覆盖层目标不能为空")
    for mutation in config["test_mutations"]:
        match = MUTATION_RE.fullmatch(mutation)
        if match is None:
            raise ProbeError(f"不支持的验收变更：{mutation}")
        untrusted_paths(match.group(1), workspace_path())
    for key in ("deadline_seconds", "quiet_after_seconds"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] <= 0:
            raise ProbeError(f"运行器参数 {key} 必须是正整数")
    window = config["diagnostic_window"]
    if window is not None and (
        not isinstance(window, list) or len(window) != 2
        or not all(isinstance(item, int) and not isinstance(item, bool) and 10 <= item <= 500 for item in window)
    ):
        raise ProbeError("诊断窗口必须是 [行, 列]")
    launch = config["launch"]
    if (
        not isinstance(launch, dict)
        or set(launch) != set(LAUNCH_FIELDS)
        or not (launch["fixture"] is None or isinstance(launch["fixture"], str))
        or not isinstance(launch["safe_mode"], bool)
        or not isinstance(launch["debug_log"], bool)
        or not isinstance(launch["cli_args"], list)
        or not all(isinstance(item, str) and item for item in launch["cli_args"])
        or not isinstance(launch["injected_env"], dict)
        or not all(isinstance(key, str) and isinstance(value, str) for key, value in launch["injected_env"].items())
    ):
        raise ProbeError("运行器参数 launch 非法")
    for label, path in (("私有 HOME", str(PROBE_HOME)), ("工作目录", str(workspace_path()))):
        if not any(path == target or path.startswith(target.rstrip("/") + "/") for target in config["overlay_targets"]):
            raise ProbeError(f"{label}不在覆盖层保护范围内：{path}")
    return config


def probe_launch(probe: Any) -> dict[str, Any]:
    """受管目录里一个 TUI probe 的启动身份（与宿主侧组合展开同一口径）。"""

    return {
        "fixture": probe.fixture,
        "safe_mode": bool(probe.safe_mode),
        "debug_log": bool(probe.require_debug_log),
        "cli_args": [str(item) for item in probe.cli_args],
        "injected_env": dict(sorted(probe.env_dict().items())),
    }


# ---------------------------------------------------------------------------
# 命名空间内的环境搭建（只在采集容器里执行）
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise ProbeError(f"{argv[0]} 失败（{completed.returncode}）：{completed.stderr.strip()[:300]}")
    return completed.stdout


def _isolate_network() -> list[str]:
    _run(["ip", "link", "set", "lo", "up"])
    names = parse_link_names(_run(["ip", "-o", "link", "show"]))
    if names != ["lo"]:
        raise ProbeError(f"私有网络命名空间出现回环以外的接口：{names}")
    return names


def _mount_overlays(targets: list[str]) -> list[str]:
    if not SCRATCH.is_dir():
        raise ProbeError("容器内缺少 /mnt，无法挂载私有 tmpfs")
    _run(["mount", "-t", "tmpfs", "-o", "mode=0700,size=1024m", "tmpfs", str(SCRATCH)])
    mounted = []
    for index, target in enumerate(targets):
        if not os.path.isdir(target) or os.path.islink(target):
            raise ProbeError(f"覆盖层目标不是目录：{target}")
        if any(char in target for char in ",:\\ "):
            raise ProbeError(f"覆盖层目标含非法字符：{target}")
        upper = SCRATCH / "overlay" / str(index) / "upper"
        work = SCRATCH / "overlay" / str(index) / "work"
        upper.mkdir(parents=True)
        work.mkdir(parents=True)
        _run([
            "mount", "-t", "overlay", "overlay", "-o",
            f"lowerdir={target},upperdir={upper},workdir={work}", target,
        ])
        mounted.append(target)
    return mounted


def _private_shm() -> str:
    """受管调试日志落在 /dev/shm（``_new_debug_path``）：命名空间内换成私有 tmpfs，容器真实的 /dev/shm 不受影响。"""

    if not SHM.is_dir() or SHM.is_symlink():
        raise ProbeError("容器内缺少 /dev/shm")
    _run(["mount", "-t", "tmpfs", "-o", "mode=1777,size=256m", "tmpfs", str(SHM)])
    return str(SHM)


def _install_hosts(hosts: list[str]) -> None:
    private = SCRATCH / "hosts"
    private.write_text(build_hosts(Path("/etc/hosts").read_text(encoding="utf-8", errors="replace"), hosts), encoding="utf-8")
    _run(["mount", "--bind", str(private), "/etc/hosts"])


def _install_tls(hosts: list[str]) -> tuple[Path, Path, Path]:
    """一次性 CA 与替身证书；客户端经 ``NODE_EXTRA_CA_CERTS`` 信任这一份 CA（与中继 TUI 路径同一机制，不改系统信任库）。"""

    tls = SCRATCH / "tls"
    tls.mkdir(mode=0o700)
    ca_key, ca_crt = tls / "ca.key", tls / "ca.crt"
    leaf_key, leaf_csr, leaf_crt, leaf_ext = tls / "leaf.key", tls / "leaf.csr", tls / "leaf.crt", tls / "leaf.ext"
    _run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
        "-out", str(ca_crt), "-days", "1", "-subj", "/CN=claude-client-launch-probe-ca",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    ])
    _run([
        "openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(leaf_key),
        "-out", str(leaf_csr), "-subj", f"/CN={hosts[0]}",
    ])
    leaf_ext.write_text(
        "subjectAltName=" + ",".join(f"DNS:{host}" for host in hosts) + "\n"
        "basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    _run([
        "openssl", "x509", "-req", "-in", str(leaf_csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(leaf_crt), "-days", "1", "-sha256", "-extfile", str(leaf_ext),
    ])
    return ca_crt, leaf_crt, leaf_key


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def build_private_home(
    home: Path,
    *,
    global_state_file: Path,
    target_version: str,
    privacy_env: Mapping[str, str],
    mutations: Iterable[str],
    workspace: PurePosixPath,
) -> dict[str, Any]:
    """建私有 HOME（布局同中继 TUI 临时 HOME）。只读全局状态文件，不碰凭据文件；返回不含取值的摘要。"""

    home.mkdir(mode=0o700, parents=True)
    config_dir = home / ".claude"
    config_dir.mkdir(mode=0o700)
    _write_private(config_dir / ".credentials.json", (json.dumps(SYNTHETIC_CREDENTIALS, separators=(",", ":")) + "\n").encode("utf-8"))
    settings = {"skipWebFetchPreflight": True, "env": dict(privacy_env)}
    _write_private(config_dir / "settings.json", (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    raw: bytes | None = None
    if global_state_file.is_file() and not global_state_file.is_symlink():
        raw = global_state_file.read_bytes()
    elif global_state_file.exists() or global_state_file.is_symlink():
        raise ProbeError(f"全局状态文件不是普通文件：{global_state_file}")
    payload, summary = normalized_global_state(raw, target_version, mutations=mutations, workspace=workspace)
    _write_private(home / ".claude.json", payload)
    return {"credentials": "synthetic", "settings": "privacy", "global_state": summary}


def _managed(tool_root: str) -> tuple[Any, Any, Any]:
    """只读导入受管目录模块（v5 目录、v4 夹具与环境白名单）；受管工具树里不得出现 __pycache__。"""

    sys.dont_write_bytecode = True
    repository = str(PurePosixPath(tool_root).parents[1])
    if repository not in sys.path:
        sys.path.insert(0, repository)
    from tools.official_client_capture.capturelib import claude_fw_f_v4, claude_fw_f_v5, environment  # noqa: PLC0415

    return claude_fw_f_v5, claude_fw_f_v4, environment


def _window_shim(real_claude: str, rows: int, cols: int) -> Path:
    """诊断复跑用的窗口尺寸垫片：先给自己的伪终端设尺寸，再原样 exec 真实客户端。

    判定只看真实条件（驱动不设尺寸，窗口 0x0）的那次运行；失败后宿主侧才用本垫片在全新命名空间里复跑一次，取得可读
    屏幕识别是哪类交互屏拦住了首帧。
    """

    shim = SCRATCH / "claude-window-shim"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import fcntl, os, struct, sys, termios\n"
        f"fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', {int(rows)}, {int(cols)}, 0, 0))\n"
        f"os.execv({real_claude!r}, [{real_claude!r}] + sys.argv[1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    return shim


class Stub:
    """本地替身：只在回环上终结请求，不转发、不记录任何头部取值与正文。"""

    def __init__(self, cert: Path, key: Path, token: str, started: float) -> None:
        self.token = token.encode("ascii")
        self.started = started
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        context.set_alpn_protocols(["http/1.1"])
        self.context = context
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 443))
        self.listener.listen(64)
        threading.Thread(target=self._accept, daemon=True).start()

    def token_request(self) -> dict[str, Any] | None:
        with self.lock:
            return next((dict(record) for record in self.records if is_token_request(record)), None)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(record) for record in self.records]

    def _record(self, record: dict[str, Any]) -> None:
        with self.lock:
            record["seq"] = len(self.records) + 1
            record["elapsed_ms"] = int((time.monotonic() - self.started) * 1000)
            self.records.append(record)

    def _accept(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(raw,), daemon=True).start()

    def _handle(self, raw: socket.socket) -> None:
        raw.settimeout(30)
        try:
            conn = self.context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError) as error:
            self._record({"kind": "tls_error", "error": type(error).__name__})
            raw.close()
            return
        try:
            while True:
                record = serve_request(conn.makefile("rb"), conn.sendall, token=self.token)
                if record is None:
                    break
                self._record(record)
                break
        except (OSError, ValueError, ssl.SSLError) as error:
            self._record({"kind": "request_error", "error": f"{type(error).__name__}: {str(error)[:120]}"})
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _hard_deadline(seconds: int) -> None:
    """硬截止兜底：本进程是私有 PID 命名空间的 1 号进程，必须显式安装 SIGALRM 处理器（同 Codex 运行器）。"""

    def _expire(_signum: int, _frame: object) -> None:
        print(RESULT_MARKER + json.dumps({"schema_version": RUN_SCHEMA, "status": "failed", "reason": "hard_deadline"}), flush=True)
        os._exit(3)

    signal.signal(signal.SIGALRM, _expire)
    signal.alarm(seconds)


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cli_summary(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                return payload
    return {}


def main() -> int:
    started = time.monotonic()
    result: dict[str, Any] = {"schema_version": RUN_SCHEMA, "status": "failed"}
    try:
        config = validate_config(json.loads(sys.argv[1]))
        _hard_deadline(config["deadline_seconds"] + 45)
        result["combo_id"] = config["combo_id"]
        if os.getpid() != 1:
            raise ProbeError("运行器必须是私有 PID 命名空间的 1 号进程")
        result["network_interfaces"] = _isolate_network()
        result["overlays"] = _mount_overlays(config["overlay_targets"])
        result["private_shm"] = _private_shm()
        _install_hosts(config["stub_hosts"])
        ca, cert, key = _install_tls(config["stub_hosts"])
        tool_root = Path(config["tool_root"])
        result["drive_sha256"] = {name: _sha256_file(tool_root / name) for name in (DRIVE_CLI, DRIVE_MODULE)}
        catalog, fixtures, environment = _managed(config["tool_root"])
        probe = catalog.get_probe(config["probe_id"])
        if probe.driver != "tui" or probe_launch(probe) != config["launch"]:
            raise ProbeError(f"受管目录里 {config['probe_id']} 的启动身份与组合不一致")
        workspace_parent = PROBE_RUN / "results"
        workspace_parent.mkdir(mode=0o700, parents=True)
        result["test_mutations"] = list(config["test_mutations"])
        result["private_home"] = build_private_home(
            PROBE_HOME,
            global_state_file=Path(config["global_state_file"]),
            target_version=config["target_version"],
            privacy_env=environment.PRIVACY_ENV,
            mutations=config["test_mutations"],
            workspace=workspace_path(),
        )
        # 工作目录与夹具：与中继同一函数（输出根下 results/workspace 与夹具文件、夹具参数）。
        workspace, fixture_args = fixtures._fixture_paths(workspace_parent, probe)
        if PurePosixPath(str(workspace)) != workspace_path():
            raise ProbeError(f"受管夹具建的工作目录不是预期位置：{workspace}")
        debug_path = fixtures._new_debug_path(probe)
        args = tui_launch_args(config["launch"], [str(item) for item in fixture_args], None if debug_path is None else str(debug_path))
        drive_env = environment.clean_environment(os.environ, config["launch"]["injected_env"])
        drive_env.update(environment.PRIVACY_ENV)
        drive_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        drive_env.pop("CLAUDE_CONFIG_DIR", None)
        drive_env["NODE_EXTRA_CA_CERTS"] = str(ca)
        drive_env["HOME"] = str(PROBE_HOME)
        stub = Stub(cert, key, config["token"], started)
        claude_bin = config["claude_bin"]
        result["diagnostic_window"] = config["diagnostic_window"]
        if config["diagnostic_window"]:
            claude_bin = str(_window_shim(claude_bin, *config["diagnostic_window"]))
        transcript = SCRATCH / "tui-transcript.txt"
        argv = build_cli_argv(config, claude_bin, str(workspace), str(transcript), args)
        remaining = max(5.0, started + config["deadline_seconds"] - time.monotonic())
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=remaining, env=drive_env, check=False)
            result["cli_exit_code"] = completed.returncode
            summary = _cli_summary(completed.stdout)
            cli_tail = completed.stdout[-1500:] + completed.stderr[-1500:]
        except subprocess.TimeoutExpired as error:
            result["cli_exit_code"] = None
            summary = {}
            cli_tail = str(error.stdout or "")[-1500:]
        steps = summary.get("steps") if isinstance(summary.get("steps"), list) else []
        result["driver"] = {
            "input_complete": summary.get("input_complete") is True,
            "auth_gate_detected": summary.get("auth_gate_detected") is True,
            "menus_accepted": sum(1 for step in steps if isinstance(step, list) and len(step) == 2 and step[1] == "enter-menu"),
            "step_tags": [str(step[1]) for step in steps if isinstance(step, list) and len(step) == 2],
        }
        hit = stub.token_request()
        result["stub_requests"] = stub.snapshot()
        result["token_request"] = hit
        text = transcript.read_text(encoding="utf-8", errors="replace") if transcript.is_file() else ""
        result["detected_screens"] = detect_screens(text)
        result["transcript_bytes"] = len(text.encode("utf-8"))
        result["tui_visible_tail"] = redact(text[-1500:])
        result["drive_output_tail"] = redact(cli_tail)
        result["status"] = "passed" if hit is not None else "failed"
        if hit is not None:
            result["reason"] = "token_request_observed"
        elif result["driver"]["auth_gate_detected"]:
            result["reason"] = "auth_gate"
        elif result["cli_exit_code"] is None:
            result["reason"] = "timeout"
        else:
            result["reason"] = "token_request_missing"
    except (ProbeError, OSError, KeyError, TypeError, ValueError, ImportError, subprocess.SubprocessError) as error:
        result["reason"] = "probe_error"
        result["error"] = redact(f"{type(error).__name__}: {error}")[:600]
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    print(RESULT_MARKER + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
