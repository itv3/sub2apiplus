#!/usr/bin/env python3
"""客户端启动探测运行器（R19）：在采集容器的私有命名空间内完成一次 TUI 启动探测。

本文件不在容器里落盘：宿主侧 ``client_launch_probe.py`` 以
``docker exec -i <容器> unshare --net --mount --pid --fork --mount-proc --propagation private
python3 - <参数 JSON>`` 把它经标准输入送进去执行，结果以 ``RESULT_MARKER`` 开头的一行 JSON 打印到 stdout。

隔离与零副作用
--------------
* 网络：新网络命名空间只有回环；本地替身（TLS）在回环 443 端口终结全部请求，报文出不了命名空间，
  不会产生任何真实请求。启动时核对接口集合恰好是 ``lo``。
* 文件：CODEX_HOME、/tmp、/var/tmp、/work 与系统证书目录各叠加一层 tmpfs 覆盖层，客户端与驱动的
  全部写入落在覆盖层上，命名空间结束即丢弃；/etc/hosts 以私有副本绑定挂载。容器内真实状态不变
  （宿主侧另做前后指纹比对）。
* 进程：本进程是私有 PID 命名空间的 1 号进程，退出时内核回收命名空间内全部子进程。

判据
----
以作业相同的参数调用受管 ``drive_codex_tui.py``（只把提示词换成探测口令、缩短保持时间），
替身收到正文含完整口令的请求（首个 turn：``POST …/responses``）才算通过。被信任目录、模型迁移、
登录、hooks 审查等交互屏拦住时，口令进不了输入框，回车只会确认弹窗默认项，替身收不到带口令的
请求，判为失败。TUI 可见文本只用于失败诊断，不参与判定。

替身只做 TUI 引导所必需的最小应答，其余一律 404：
* ``GET …/accounts/check``：工作区路由发现拿不到同账号条目时 TUI 直接报错退出，按真实上游实录的
  同构形态回显请求头里的账号（List 形态 1 个条目、两个路由字段均为 NO_CONSTRAINT）；
* ``GET …/codex/models``：用 CODEX_HOME 中客户端自己缓存的最近一次真实目录应答（迁移屏由目录的
  upgrade 字段决定，回 404 会让客户端改用内置目录，升级目标可能与真实上游不同）；
* WebSocket 升级回 426：客户端立即回退 HTTP，口令随后出现在 POST 正文里，替身不必实现 WebSocket；
* ``POST …/responses`` 回 400（不触发重试）。

任何头部取值（Authorization、Cookie、账号 ID 等）都不写进结果，只记录头部名称。
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
from pathlib import Path
from typing import Any, BinaryIO, Callable

RESULT_MARKER = "CLIENT_LAUNCH_PROBE_RESULT "
RUN_SCHEMA = "arm64-client-launch-probe-run/v1"
SCRATCH = Path("/mnt")
# 与作业相同：relay 场景不设 CODEX_HOME，客户端用容器默认的 ~/.codex。
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
MAX_BODY_BYTES = 64 * 1024 * 1024
# 已知会拦住首帧的交互屏（去掉空白与框线后匹配），只用于诊断，不参与判定。
KNOWN_SCREENS = (
    ("trust_directory", ("Trustthisfolder?", "Doyoutrustthefilesinthisfolder")),
    ("model_migration", ("Codexjustgotanupgrade", "Trynewmodel", "isnolongeravailable")),
    ("login", ("SigninwithChatGPT", "Provideyourownapikey")),
    ("hooks_review", ("Hooksneedreview",)),
    ("unstable_features", ("Under-developmentfeaturesenabled",)),
    ("update_prompt", ("Updatenow", "Skipuntilnextversion")),
    # 迁移屏被回车接受后的痕迹：口令被弹窗吞掉的旁证。
    ("model_changed", ("Modelchangedto",)),
)
# 0x0 窗口下 TUI 逐字渲染，每个字符两侧夹着框线；诊断匹配前去掉框线与装饰符。
DECORATION_RE = re.compile("[─-▟•›·]")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SECRET_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]+|(?<![A-Za-z0-9-])sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)"
)
TOKEN_RE = re.compile(r"^[A-Z]{16,64}$")
MUTATION_RE = re.compile(r"^(untrust|unack_migration):([A-Za-z0-9_./-]+)$")
# daemon 场景（daemon_auto_start 默认开启后的默认路径）的独立 CODEX_HOME：放在覆盖层 /work 下而不是 /tmp——客户端拒绝在临时目录
# 下的 CODEX_HOME 里建 helper 别名并告警，与作业路径（容器 /root/.codex-daemon-<run_id 摘要前 16 位>）不一致。建立、模式判定
# 与停止都调用受管 drive_codex_daemon.py，与作业同一实现。
DAEMON_HOMES_PARENT = Path("/work")
DAEMON_HOME = DAEMON_HOMES_PARENT / ".codex-daemon-launch-probe"
DAEMON_TOOL = "drive_codex_daemon.py"
FEATURE_RE = re.compile(r"^[a-z0-9_]+$")


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


def apply_config_mutation(text: str, mutation: str) -> str:
    """仅验收用：在覆盖层里的 config.toml 文本上制造环境缺陷（真实文件不受影响）。

    * ``untrust:<目录>``：删除 ``[projects."<目录>"]`` 整节；
    * ``unack_migration:<模型>``：删除 ``[notice.model_migrations]`` 里该模型的已确认记录。
    """

    match = MUTATION_RE.fullmatch(mutation)
    if match is None:
        raise ProbeError(f"不支持的验收变更：{mutation}")
    kind, value = match.groups()
    lines = text.splitlines()
    kept: list[str] = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        if kind == "untrust" and section == f'[projects."{value}"]':
            continue
        if (
            kind == "unack_migration"
            and section == "[notice.model_migrations]"
            and not stripped.startswith("[")
            and stripped.split("=", 1)[0].strip().strip('"') == value
        ):
            continue
        kept.append(line)
    if kept == lines:
        raise ProbeError(f"config.toml 中没有可删除的目标：{mutation}")
    return "\n".join(kept) + "\n"


def accounts_check_payload(account_id: str) -> bytes:
    """工作区路由发现的最小同构应答（与真实上游实录同形）。"""

    return json.dumps(
        {
            "accounts": [
                {
                    "id": account_id,
                    "workspace_backend_origin": "NO_CONSTRAINT",
                    "account_routing_override": "NO_CONSTRAINT",
                    "structure": "personal",
                }
            ],
            "account_ordering": [account_id],
            "default_account_id": account_id,
        }
    ).encode("utf-8")


def models_payload_from_cache(cache: Any) -> tuple[bytes | None, str | None, dict[str, Any]]:
    """把客户端缓存的模型目录转成 ``/models`` 应答正文，并给出来源摘要。"""

    if not isinstance(cache, dict):
        return None, None, {"source": "invalid"}
    models = cache.get("models")
    if not isinstance(models, list) or not models:
        return None, None, {"source": "invalid"}
    payload = json.dumps({"models": models}, ensure_ascii=False).encode("utf-8")
    etag = cache.get("etag") if isinstance(cache.get("etag"), str) else None
    return payload, etag, {
        "source": "codex_home_cache",
        "client_version": cache.get("client_version"),
        "fetched_at": cache.get("fetched_at"),
        "model_count": len(models),
        "models_sha256": hashlib.sha256(payload).hexdigest(),
    }


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


def detect_screens(collapsed: str) -> list[str]:
    """在压掉空白的可见文本里识别已知交互屏（先去框线）。"""

    text = DECORATION_RE.sub("", collapsed)
    return [name for name, needles in KNOWN_SCREENS if any(needle in text for needle in needles)]


def decide_response(
    method: str,
    path: str,
    headers: dict[str, str],
    models_payload: bytes | None,
    models_etag: str | None,
) -> tuple[int, str, bytes, dict[str, str]]:
    """替身的应答决策（见模块说明）；返回状态码、原因短语、正文与额外头部。"""

    if method == "GET" and path.endswith("/codex/models") and models_payload is not None:
        extra = {"ETag": models_etag} if models_etag else {}
        return 200, "OK", models_payload, extra
    if headers.get("upgrade", "").lower() == "websocket":
        return 426, "Upgrade Required", b"", {}
    if method == "POST" and path.endswith("/responses"):
        return 400, "Bad Request", (
            b'{"error":{"message":"codex client launch probe stub","type":"invalid_request_error"}}'
        ), {}
    if method == "GET" and path.endswith("/accounts/check") and headers.get("chatgpt-account-id"):
        return 200, "OK", accounts_check_payload(headers["chatgpt-account-id"]), {}
    return 404, "Not Found", b'{"detail":"codex client launch probe stub"}', {}


def serve_request(
    reader: BinaryIO,
    writer: Callable[[bytes], Any],
    *,
    token: bytes,
    models_payload: bytes | None,
    models_etag: str | None,
) -> dict[str, Any] | None:
    """读一条 HTTP/1.1 请求、应答并返回记录（不含任何头部取值）；连接空闲关闭时返回 None。"""

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
    encoding = headers.get("content-encoding", "identity").lower()
    decoded, decoded_ok = decode_body(encoding, body)
    path = target.split("?", 1)[0]
    status, reason, payload, extra = decide_response(method, path, headers, models_payload, models_etag)
    extra_lines = "".join(f"{key}: {value}\r\n" for key, value in extra.items())
    writer(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n{extra_lines}"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii") + payload
    )
    return {
        "kind": "request",
        "host": headers.get("host", "").split(":", 1)[0],
        "method": method,
        "path": path,
        "upgrade_websocket": headers.get("upgrade", "").lower() == "websocket",
        "content_encoding": encoding,
        "body_bytes": len(body),
        "body_decoded": decoded_ok,
        "token_found": bool(decoded_ok and token in decoded),
        "response_status": status,
        "header_names": sorted(names),
    }


def build_drive_argv(config: dict[str, Any], codex_bin: str, log_path: str) -> list[str]:
    """按作业参数拼受管驱动的命令行：提示词换成口令，其余与作业调用点一致。"""

    return [
        "python3", f"{config['tool_root']}/drive_codex_tui.py",
        "--codex-bin", codex_bin, "--model", config["model"], "--cwd", config["cwd"],
        *config["drive_options"],
        "--prompt", config["token"], "--prompt-hold", str(int(config["prompt_hold_seconds"])),
        "--log", log_path,
    ]


def validate_config(config: Any, *, codex_home: Path | None = None) -> dict[str, Any]:
    """运行器参数闭集校验：宿主侧生成，这里再失败关闭一次（工作目录与 CODEX_HOME 必须在覆盖层内）。"""

    expected = {
        "combo_id", "tool_root", "codex_bin", "model", "cwd", "drive_options", "token",
        "prompt_hold_seconds", "deadline_seconds", "overlay_targets", "stub_hosts",
        "test_mutations", "diagnostic_window", "daemon",
    }
    if not isinstance(config, dict) or set(config) != expected:
        raise ProbeError("运行器参数键集合非法")
    for key in ("combo_id", "tool_root", "codex_bin", "model", "cwd"):
        if not isinstance(config[key], str) or not config[key]:
            raise ProbeError(f"运行器参数 {key} 非法")
    for key in ("tool_root", "codex_bin", "cwd"):
        if not config[key].startswith("/") or ".." in config[key].split("/"):
            raise ProbeError(f"运行器参数 {key} 必须是绝对路径")
    if not isinstance(config["token"], str) or not TOKEN_RE.fullmatch(config["token"]):
        raise ProbeError("探测口令必须是 16～64 个大写字母（不含数字，避免误选编号选项）")
    for key in ("drive_options", "overlay_targets", "stub_hosts", "test_mutations"):
        value = config[key]
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ProbeError(f"运行器参数 {key} 必须是非空字符串数组")
    if not config["stub_hosts"] or not config["overlay_targets"]:
        raise ProbeError("替身域名与覆盖层目标不能为空")
    for key in ("prompt_hold_seconds", "deadline_seconds"):
        if not isinstance(config[key], int) or isinstance(config[key], bool) or config[key] <= 0:
            raise ProbeError(f"运行器参数 {key} 必须是正整数")
    window = config["diagnostic_window"]
    if window is not None and (
        not isinstance(window, list) or len(window) != 2
        or not all(isinstance(item, int) and not isinstance(item, bool) and 10 <= item <= 500 for item in window)
    ):
        raise ProbeError("诊断窗口必须是 [行, 列]")
    daemon = config["daemon"]
    if daemon is not None:
        features = daemon.get("features") if isinstance(daemon, dict) else None
        if (
            set(daemon) != {"features"}
            or not isinstance(features, list)
            or not all(isinstance(item, str) and FEATURE_RE.fullmatch(item) for item in features)
            or len(set(features)) != len(features)
        ):
            raise ProbeError("daemon 参数必须是 {\"features\": [功能开关名…]}")
    homes = [("工作目录", config["cwd"]), ("CODEX_HOME", str(codex_home or CODEX_HOME))]
    if daemon is not None:
        homes.append(("daemon CODEX_HOME", str(DAEMON_HOME)))
    for label, path in homes:
        if not any(path == target or path.startswith(target.rstrip("/") + "/") for target in config["overlay_targets"]):
            raise ProbeError(f"{label}不在覆盖层保护范围内：{path}")
    return config


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


def _install_hosts(hosts: list[str]) -> None:
    private = SCRATCH / "hosts"
    private.write_text(build_hosts(Path("/etc/hosts").read_text(encoding="utf-8", errors="replace"), hosts), encoding="utf-8")
    _run(["mount", "--bind", str(private), "/etc/hosts"])


def _install_tls(hosts: list[str]) -> tuple[Path, Path]:
    """一次性 CA 与替身证书，按 update-ca-certificates 的布局装进覆盖层里的系统信任库。

    不设 CODEX_CA_CERTIFICATE／SSL_CERT_FILE：设了会让客户端改走 rustls，与真实作业的 native-tls
    路径不一致（与 relay 场景同一约束）。
    """

    tls = SCRATCH / "tls"
    tls.mkdir(mode=0o700)
    ca_key, ca_crt = tls / "ca.key", tls / "ca.crt"
    leaf_key, leaf_csr, leaf_crt, leaf_ext = tls / "leaf.key", tls / "leaf.csr", tls / "leaf.crt", tls / "leaf.ext"
    _run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
        "-out", str(ca_crt), "-days", "1", "-subj", "/CN=codex-client-launch-probe-ca",
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
    certs = Path("/etc/ssl/certs")
    probe_ca = certs / "codex-client-launch-probe-ca.pem"
    probe_ca.write_bytes(ca_crt.read_bytes())
    with (certs / "ca-certificates.crt").open("ab") as handle:
        handle.write(b"\n" + ca_crt.read_bytes())
    subject_hash = _run(["openssl", "x509", "-hash", "-noout", "-in", str(ca_crt)]).strip()
    index = 0
    while (certs / f"{subject_hash}.{index}").exists():
        index += 1
    (certs / f"{subject_hash}.{index}").symlink_to(probe_ca.name)
    return leaf_crt, leaf_key


def _apply_test_mutations(mutations: list[str]) -> list[str]:
    config = CODEX_HOME / "config.toml"
    for mutation in mutations:
        config.write_text(apply_config_mutation(config.read_text(encoding="utf-8"), mutation), encoding="utf-8")
    return list(mutations)


def _load_models_catalog() -> tuple[bytes | None, str | None, dict[str, Any]]:
    path = CODEX_HOME / "models_cache.json"
    if not path.is_file():
        return None, None, {"source": "absent"}
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, {"source": "invalid"}
    return models_payload_from_cache(cache)


def _window_shim(real_codex: str, rows: int, cols: int) -> Path:
    """诊断复跑用的窗口尺寸垫片：先给自己的伪终端设尺寸，再原样 exec 真实客户端。

    判定只看真实条件（驱动不设尺寸，窗口 0x0）的那次运行；0x0 下弹窗可能不渲染，失败后宿主侧
    才用本垫片在全新命名空间里复跑一次，取得可读屏幕识别是哪类交互屏拦住了首帧。
    """

    shim = SCRATCH / "codex-window-shim"
    shim.write_text(
        "#!/usr/bin/python3\n"
        "import fcntl, os, struct, sys, termios\n"
        f"fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', {int(rows)}, {int(cols)}, 0, 0))\n"
        f"os.execv({real_codex!r}, [{real_codex!r}] + sys.argv[1:])\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)
    return shim


class Stub:
    """本地替身：只在回环上终结请求，不转发、不记录任何头部取值。"""

    def __init__(self, cert: Path, key: Path, token: str, started: float, models_payload: bytes | None, models_etag: str | None) -> None:
        self.token = token.encode("ascii")
        self.started = started
        self.models_payload = models_payload
        self.models_etag = models_etag
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
            return next((dict(record) for record in self.records if record.get("token_found")), None)

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
            record = serve_request(
                conn.makefile("rb"), conn.sendall, token=self.token,
                models_payload=self.models_payload, models_etag=self.models_etag,
            )
            if record is not None:
                self._record(record)
        except (OSError, ValueError, ssl.SSLError) as error:
            self._record({"kind": "request_error", "error": f"{type(error).__name__}: {str(error)[:120]}"})
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _visible_text(tool_root: str, raw: bytes) -> tuple[str, str]:
    """复用受管驱动的 ANSI 剥离规则（只读导入），与作业日志的阅读方式一致。"""

    sys.dont_write_bytecode = True  # 受管工具树不得出现 __pycache__
    sys.path.insert(0, tool_root)
    try:
        import drive_codex_tui  # noqa: PLC0415

        return drive_codex_tui.visible(raw), drive_codex_tui.visible(raw, collapse=True)
    finally:
        sys.path.remove(tool_root)


def _daemon_tool(tool_root: str, *arguments: str) -> dict[str, Any]:
    """在命名空间内调用受管 daemon 生命周期工具（与作业同一实现），返回其单行 JSON 与退出码。"""

    completed = subprocess.run(
        ["python3", "-B", f"{tool_root}/{DAEMON_TOOL}", "--homes-parent", str(DAEMON_HOMES_PARENT), *arguments],
        capture_output=True, text=True, timeout=180, check=False,
    )
    payload: dict[str, Any] = {}
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            break
    return {**payload, "exit_code": completed.returncode}


def _stop(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is None:
        # SIGINT 让驱动走 finally → tui.close()（与作业结束方式一致），再兜底 SIGKILL。
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return int(process.returncode)


def _hard_deadline(seconds: int) -> None:
    """硬截止兜底：宿主侧杀掉 docker exec 客户端并不会停止容器内进程，只能由运行器自己退出。

    本进程是私有 PID 命名空间的 1 号进程，内核对 1 号进程没有处理器的信号一律忽略，因此必须
    显式安装 SIGALRM 处理器；退出后内核回收命名空间内全部子进程。
    """

    def _expire(_signum: int, _frame: object) -> None:
        print(RESULT_MARKER + json.dumps({"schema_version": RUN_SCHEMA, "status": "failed", "reason": "hard_deadline"}), flush=True)
        os._exit(3)

    signal.signal(signal.SIGALRM, _expire)
    signal.alarm(seconds)


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
        _install_hosts(config["stub_hosts"])
        cert, key = _install_tls(config["stub_hosts"])
        result["test_mutations"] = _apply_test_mutations(config["test_mutations"])
        drive_path = Path(config["tool_root"]) / "drive_codex_tui.py"
        result["drive_codex_tui_sha256"] = hashlib.sha256(drive_path.read_bytes()).hexdigest()
        models_payload, models_etag, result["models_catalog"] = _load_models_catalog()
        daemon = config["daemon"]
        drive_env = dict(os.environ)
        if daemon is not None:
            prepared = _daemon_tool(
                config["tool_root"], "prepare", "--home", str(DAEMON_HOME),
                "--disable-features", " ".join(daemon["features"]),
            )
            result["daemon_prepare"] = {
                key: prepared.get(key) for key in ("status", "copied_files", "features", "error", "exit_code")
            }
            if prepared.get("status") != "passed":
                raise ProbeError("daemon 场景的独立 CODEX_HOME 建立失败")
            drive_env["CODEX_HOME"] = str(DAEMON_HOME)
        stub = Stub(cert, key, config["token"], started, models_payload, models_etag)
        codex_bin = config["codex_bin"]
        result["diagnostic_window"] = config["diagnostic_window"]
        if config["diagnostic_window"]:
            codex_bin = str(_window_shim(codex_bin, *config["diagnostic_window"]))
        drive = build_drive_argv(config, codex_bin, str(SCRATCH / "tui.log"))
        with (SCRATCH / "drive.out").open("wb") as drive_out:
            process = subprocess.Popen(drive, stdout=drive_out, stderr=subprocess.STDOUT, env=drive_env)
            deadline = started + config["deadline_seconds"]
            outcome = "timeout"
            while time.monotonic() < deadline:
                if stub.token_request() is not None:
                    outcome = "token_request_observed"
                    time.sleep(0.5)
                    break
                if process.poll() is not None:
                    outcome = "drive_exited"
                    break
                time.sleep(0.25)
            result["drive_exit_code"] = _stop(process)
        hit = stub.token_request()
        daemon_ok = True
        if daemon is not None:
            # 口令经 daemon 发出才算 daemon 路径成立：模式由 daemon version 与进程表判定，随后停止 daemon。
            status = _daemon_tool(
                config["tool_root"], "status", "--home", str(DAEMON_HOME),
                "--codex-bin", config["codex_bin"], "--require-mode", "daemon",
            )
            stopped = _daemon_tool(config["tool_root"], "stop", "--home", str(DAEMON_HOME), "--codex-bin", config["codex_bin"])
            result["daemon"] = {
                "mode": status.get("mode"),
                "status": status.get("status"),
                "app_server_version": (status.get("daemon") or {}).get("appServerVersion"),
                "stop_status": stopped.get("status"),
                "error": status.get("error") or stopped.get("error"),
            }
            daemon_ok = status.get("status") == "passed" and stopped.get("status") == "passed"
        result["stub_requests"] = stub.snapshot()
        result["token_request"] = hit
        raw = (SCRATCH / "tui.log").read_bytes() if (SCRATCH / "tui.log").exists() else b""
        text, collapsed = _visible_text(config["tool_root"], raw)
        result["detected_screens"] = detect_screens(collapsed)
        result["tui_log_bytes"] = len(raw)
        result["tui_visible_tail"] = redact(text[-1500:])
        result["drive_output_tail"] = redact((SCRATCH / "drive.out").read_bytes().decode("utf-8", "replace")[-1500:])
        result["status"] = "passed" if hit is not None and daemon_ok else "failed"
        if hit is not None and daemon_ok:
            result["reason"] = "token_request_observed"
        else:
            result["reason"] = "daemon_mode_not_established" if hit is not None else outcome
    except (ProbeError, OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as error:
        result["reason"] = "probe_error"
        result["error"] = redact(f"{type(error).__name__}: {error}")[:600]
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    print(RESULT_MARKER + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
