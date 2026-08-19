#!/usr/bin/env python3
"""执行 Claude Code FW-E 的受管 R 类原始应用字节取证。

本工具只在隔离抓包容器内临时覆盖 ``/etc/hosts``，把官方域名导向本地
字节中继；退出前必须逐字节恢复。中继仍连接冻结的真实上游 IP，只复制两条
TLS 腿之间的应用字节。原始凭据在终态前做等长替换，完整调用、环境、工具、
宿主镜像、恢复和秘密扫描共同构成 M 绑定。

本工具不会生成画像、Snapshot、Persona 或 production binding。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capture import _load_host_runtime_receipt  # noqa: E402
from tools.official_client_capture.capturelib.environment import (  # noqa: E402
    PRIVACY_ENV,
    clean_environment,
    parse_injected_env,
    prepare_claude_oauth_state,
    temporary_claude_refresh_state,
    temporary_claude_tui_state,
)
from tools.official_client_capture.capturelib.claude_fw_f_v3 import (  # noqa: E402
    PROBE_IDS as FW_F_V3_PROBE_IDS,
    ClaudeFWFProbeError,
    get_probe as get_fw_f_probe,
    run_claude_fw_f_probe,
)
from tools.official_client_capture.capturelib.claude_fw_f_v4 import (  # noqa: E402
    PROBE_IDS as FW_F_V4_PROBE_IDS,
    ClaudeFWFCompleteProbeError,
    get_probe as get_fw_f_v4_probe,
    run_claude_fw_f_complete_probe,
    validate_complete_probe_evidence,
)
from tools.official_client_capture.capturelib.identity import (  # noqa: E402
    capture_source_bundle_identity,
)
from tools.official_client_capture.capturelib.lifecycle import (  # noqa: E402
    CampaignLock,
    _popen_safety_options,
)
from tools.official_client_capture.capturelib.model import (  # noqa: E402
    SCENARIOS,
    ConfigurationError,
    validate_safe_name,
)
from tools.official_client_capture.capturelib.scenarios import (  # noqa: E402
    run_claude_scenario,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    argv_manifest_view,
    ensure_private_directory,
    file_sha256,
    inventory_artifacts,
    redact_known_secret,
    scan_for_secrets,
    secure_write_json,
    secure_write_text,
)
from tools.official_client_capture.claude_oauth_refresh import (  # noqa: E402
    load_claude_credentials,
)


SCHEMA_RUN = "claude-code-fw-e-relay-run/v1"
SCHEMA_INDEX = "claude-code-fw-e-relay-index/v1"
UPSTREAM_HOST = "api.anthropic.com"
HOSTS_MARKER = "official-client-fw-e-relay"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
RUNTIME_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
EMPTY_HANDSHAKE_TERMINATION_REASON = (
    "client_tls_handshake_terminated_before_application_data"
)
RELAY_SHUTDOWN_HANDSHAKE_TERMINATION_REASON = (
    "relay_shutdown_terminated_handshake_before_application_data"
)
CLIENT_RESET_HANDSHAKE_TERMINATION_REASON = (
    "client_reset_transport_before_tls_handshake"
)
ONE_SIDED_SHUTDOWN_TERMINATION_REASON = (
    "relay_shutdown_after_complete_client_request_before_upstream_response"
)
DEFAULT_RUNTIME_IMAGE = (
    "oauth-egress-capture-capture-cli@"
    "sha256:3438c4e0909d7401ff8e076a985258608a8f031629e65262db16c1979ab1771c"
)
FW_F_V3_VERSION = "2.1.226"


class RelayEvidenceError(RuntimeError):
    """表示 R 取证身份、完整性或恢复门禁未闭合。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RelayEvidenceError(f"{label} 不是可信普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RelayEvidenceError(f"{label} 不是合法 JSON：{path}") from error
    if not isinstance(value, dict):
        raise RelayEvidenceError(f"{label} 顶层必须是对象：{path}")
    return value


def _validate_static_file(path: Path, label: str, *, executable: bool = False) -> None:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label} 必须是可信绝对普通文件。")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise ConfigurationError(f"{label} 所有者或写权限不安全。")
    if executable and not os.access(path, os.X_OK):
        raise ConfigurationError(f"{label} 不可执行。")


def _validate_output_root(path: Path, *, create: bool) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ConfigurationError("--output-root 必须是非符号链接绝对路径。")
    resolved = path.resolve(strict=False)
    forbidden = {
        Path("/").resolve(),
        Path("/capture").resolve(),
        Path("/work").resolve(),
        Path.home().resolve(),
    }
    source_root = Path(__file__).resolve().parents[2]
    if resolved in forbidden or resolved.is_relative_to(source_root):
        raise ConfigurationError("--output-root 不能是宽泛目录、HOME 或源码树。")
    if path.exists():
        raise ConfigurationError("--output-root 已存在，拒绝覆盖或混写旧样本。")
    if create:
        ensure_private_directory(path)


def _command_output(command: list[str], environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ConfigurationError(f"命令执行失败：{Path(command[0]).name}")
    return (completed.stdout or completed.stderr).strip()


def _client_identity(
    claude_bin: Path, expected_version: str, expected_sha256: str
) -> dict[str, Any]:
    _validate_static_file(claude_bin, "Claude 二进制", executable=True)
    actual_sha256 = file_sha256(claude_bin)
    if actual_sha256 != expected_sha256:
        raise ConfigurationError("Claude 二进制 SHA-256 与本轮冻结身份不一致。")
    output = _command_output([str(claude_bin), "--version"], clean_environment())
    match = re.fullmatch(r"(?P<version>\d+\.\d+\.\d+)(?: \(Claude Code\))?", output)
    if not match or match.group("version") != expected_version:
        raise ConfigurationError(
            f"Claude 版本不符，预期 {expected_version}，实际 {output}。"
        )
    return {
        "path": str(claude_bin),
        "version": output,
        "sha256": actual_sha256,
        "expected_sha256": expected_sha256,
    }


def _validate_upstream_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ConfigurationError("--upstream-ip 必须是冻结的合法 IP 地址。") from error
    if not address.is_global:
        raise ConfigurationError("--upstream-ip 必须是公开上游地址。")
    return str(address)


def _tool_identity(tool_root: Path) -> dict[str, Any]:
    source = capture_source_bundle_identity(tool_root)
    openssl = Path(shutil.which("openssl") or "")
    _validate_static_file(openssl, "openssl", executable=True)
    relay = tool_root / "upstream_byte_relay.py"
    scrubber = tool_root / "scrub_raw_bytes.py"
    return {
        "execution_sources": source,
        "relay": {"path": str(relay), "sha256": file_sha256(relay)},
        "scrubber": {"path": str(scrubber), "sha256": file_sha256(scrubber)},
        "openssl": {
            "path": str(openssl),
            "sha256": file_sha256(openssl),
            "version": _command_output([str(openssl), "version"], clean_environment()),
        },
    }


def _host_receipt(arguments: argparse.Namespace, source: dict[str, Any]) -> dict[str, Any]:
    namespace = SimpleNamespace(
        host_runtime_receipt=arguments.host_runtime_receipt,
        host_runtime_receipt_sha256=arguments.host_runtime_receipt_sha256,
        run_nonce=arguments.run_nonce,
        runtime_image=arguments.runtime_image,
        require_complete_m=True,
    )
    receipt = _load_host_runtime_receipt(namespace, source)
    if receipt is None:
        raise ConfigurationError("R 通道必须绑定宿主运行镜像凭据。")
    return receipt


def _write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _rewrite_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RelayEvidenceError(f"拒绝改写非普通文件：{path}")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class HostsOverride:
    """只在当前隔离容器中追加域名覆盖，并证明退出后字节级恢复。"""

    def __init__(self, path: Path, host: str, backup_path: Path, run_id: str) -> None:
        self.path = path
        self.host = host
        self.backup_path = backup_path
        self.run_id = run_id
        self.before = b""
        self.record: dict[str, Any] = {
            "scope": "capture-container-only",
            "path": str(path),
            "restored": False,
        }

    def __enter__(self) -> "HostsOverride":
        if self.path.is_symlink() or not self.path.is_file():
            raise RelayEvidenceError("容器 hosts 不是可信普通文件。")
        self.before = self.path.read_bytes()
        if HOSTS_MARKER.encode("ascii") in self.before:
            raise RelayEvidenceError("容器 hosts 存在未恢复的历史 R 覆盖。")
        _write_bytes(self.backup_path, self.before)
        marker = (
            f"\n127.0.0.1 {self.host} # {HOSTS_MARKER}:{self.run_id}\n"
        ).encode("ascii")
        overridden = self.before.rstrip(b"\n") + marker
        try:
            _rewrite_file(self.path, overridden)
            actual = self.path.read_bytes()
            if actual != overridden:
                raise RelayEvidenceError("容器 hosts 覆盖写入后无法逐字节复核。")
        except BaseException:
            _rewrite_file(self.path, self.before)
            raise
        self.record.update(
            {
                "before_sha256": _sha256_bytes(self.before),
                "override_sha256": _sha256_bytes(actual),
                "backup_path": str(self.backup_path),
            }
        )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        _rewrite_file(self.path, self.before)
        after = self.path.read_bytes()
        self.record["after_sha256"] = _sha256_bytes(after)
        self.record["restored"] = after == self.before
        if not self.record["restored"]:
            raise RelayEvidenceError("容器 hosts 未恢复到覆盖前字节。")


def _generate_leaf_certificate(
    work_root: Path,
    ca_signing_pem: Path,
    ca_cert: Path,
    openssl: Path,
    target_host: str,
) -> tuple[Path, Path]:
    key_path = work_root / "leaf.key"
    csr_path = work_root / "leaf.csr"
    cert_path = work_root / "leaf.crt"
    chain_path = work_root / "chain.pem"
    extension_path = work_root / "leaf.cnf"
    secure_write_text(
        extension_path,
        f"subjectAltName=DNS:{target_host}\nextendedKeyUsage=serverAuth\n",
    )
    commands = (
        [
            str(openssl),
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-subj",
            f"/CN={target_host}",
            "-out",
            str(csr_path),
        ],
        [
            str(openssl),
            "x509",
            "-req",
            "-in",
            str(csr_path),
            "-CA",
            str(ca_signing_pem),
            "-CAkey",
            str(ca_signing_pem),
            "-set_serial",
            f"0x{secrets.token_hex(16)}",
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-extfile",
            str(extension_path),
        ],
    )
    for command in commands:
        completed = subprocess.run(
            command,
            env=clean_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RelayEvidenceError("无法签发 R 通道临时叶证书。")
    _write_bytes(chain_path, cert_path.read_bytes() + ca_cert.read_bytes())
    key_path.chmod(0o600)
    return chain_path, key_path


def _port_available(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _stop_process(process: subprocess.Popen[bytes] | None) -> bool:
    if process is None:
        return True
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    return process.poll() is not None


def _start_pcap(path: Path, log_path: Path) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """在当前隔离容器 loopback 被动采集客户端到本地中继的 ClientHello。"""

    tcpdump = Path(shutil.which("tcpdump") or "")
    _validate_static_file(tcpdump, "tcpdump", executable=True)
    command = [
        str(tcpdump),
        "-i",
        "lo",
        "-s",
        "0",
        "-U",
        "-Z",
        "root",
        "-w",
        str(path),
        "tcp dst port 443",
    ]
    log_stream = log_path.open("wb")
    try:
        process = subprocess.Popen(
            command,
            env=clean_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=log_stream,
            **_popen_safety_options(),
        )
    finally:
        log_stream.close()
    time.sleep(1)
    if process.poll() is not None:
        raise RelayEvidenceError("TLS P 通道 tcpdump 启动失败。")
    return process, {
        "required": True,
        "interface": "lo",
        "filter": "tcp dst port 443",
        "privilege_user": "root",
        "tool": {"path": str(tcpdump), "sha256": file_sha256(tcpdump)},
        "invocation": argv_manifest_view(command),
        "stopped": False,
        "parsed": False,
    }


def _stop_pcap(process: subprocess.Popen[bytes] | None) -> bool:
    if process is None:
        return True
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return _stop_process(process)
    return process.poll() is not None


def _parse_pcap(path: Path, target_host: str) -> dict[str, Any]:
    """解析 v4 TLS P 通道并要求至少一个匹配目标 SNI 的 ClientHello。"""

    if path.is_symlink() or not path.is_file() or path.stat().st_size <= 24:
        raise RelayEvidenceError("TLS P 通道没有生成可信 pcap。")
    from tools.official_client_capture.pcap_clienthello import (  # noqa: PLC0415
        iter_packets,
        parse_client_hello,
        tcp_payload,
    )

    rows: list[dict[str, Any]] = []
    for link, data in iter_packets(path):
        parsed = tcp_payload(link, data)
        if not parsed:
            continue
        _destination, destination_port, payload = parsed
        if destination_port != 443:
            continue
        hello = parse_client_hello(payload)
        if not hello:
            continue
        sni, extensions, ciphers, alpn = hello
        rows.append(
            {
                "sni": sni,
                "extension_types": extensions,
                "cipher_suites": ciphers,
                "alpn_offer": alpn,
            }
        )
    matched = [item for item in rows if item.get("sni") == target_host]
    if not matched:
        raise RelayEvidenceError("TLS P 通道没有匹配目标 host 的 ClientHello。")
    return {
        "required": True,
        "pcap_sha256": file_sha256(path),
        "pcap_bytes": path.stat().st_size,
        "client_hello_count": len(rows),
        "target_client_hello_count": len(matched),
        "target_host": target_host,
        "observations": matched,
        "parsed": True,
    }


def _scrub_relay(
    tool_root: Path, raw_root: Path, output_root: Path, log_path: Path
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(tool_root / "scrub_raw_bytes.py"),
        "--src",
        str(raw_root),
        "--dst",
        str(output_root),
        "--verify",
    ]
    completed = subprocess.run(
        command,
        env=clean_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    secure_write_text(log_path, completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise RelayEvidenceError("R 原始字节等长脱敏或复扫失败。")
    intervention_source = raw_root / "intervention.jsonl"
    if intervention_source.exists():
        if intervention_source.is_symlink() or not intervention_source.is_file():
            raise RelayEvidenceError("intervention.jsonl 不是可信普通文件。")
        _write_bytes(
            output_root / "intervention.jsonl",
            intervention_source.read_bytes(),
        )
    shutil.rmtree(raw_root)
    return {
        "method": "equal_length_replacement",
        "verified": True,
        "tool_sha256": file_sha256(tool_root / "scrub_raw_bytes.py"),
        "log_sha256": file_sha256(log_path),
    }


def _read_interventions(relay_root: Path) -> list[dict[str, Any]]:
    """读取中继逐次落盘的受控干预日志，并拒绝残缺或非对象记录。"""

    path = relay_root / "intervention.jsonl"
    if path.is_symlink() or not path.is_file():
        raise RelayEvidenceError("合成 R 证据缺少 intervention.jsonl。")
    rows: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RelayEvidenceError(
                    f"intervention 第 {line_number} 行必须是对象。"
                )
            rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RelayEvidenceError("intervention.jsonl 不是完整 UTF-8 JSONL。") from error
    if not rows:
        raise RelayEvidenceError("合成 R 证据没有任何受控干预记录。")
    return rows


def _message_attempt_from_wire(
    request: bytes, connection_id: int
) -> dict[str, Any]:
    """从单连接 H1 原始请求中提取不会泄露凭据的消息尝试事实。"""

    head, separator, body = request.partition(b"\r\n\r\n")
    if not separator:
        raise RelayEvidenceError("Claude 合成请求缺少完整 H1 首部。")
    request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if request_line == "HEAD /api/hello HTTP/1.1":
        return {
            "connection_id": connection_id,
            "request_line": request_line,
            "kind": "hello",
        }
    auxiliary = {
        "GET /api/claude_code/policy_limits HTTP/1.1": "policy_limits",
        "GET /api/claude_code/settings HTTP/1.1": "remote_settings",
    }
    if request_line in auxiliary:
        return {
            "connection_id": connection_id,
            "request_line": request_line,
            "kind": "auxiliary",
            "auxiliary_kind": auxiliary[request_line],
        }
    if request_line == "POST /v1/oauth/token HTTP/1.1":
        content_length: int | None = None
        for line in head.split(b"\r\n")[1:]:
            name, colon, value = line.partition(b":")
            if colon and name.strip().lower() == b"content-length":
                try:
                    content_length = int(value.strip())
                except ValueError as error:
                    raise RelayEvidenceError(
                        "Claude OAuth refresh Content-Length 非法。"
                    ) from error
        if content_length is None or content_length != len(body):
            raise RelayEvidenceError("Claude OAuth refresh body 长度不一致。")
        return {
            "connection_id": connection_id,
            "request_line": request_line,
            "kind": "oauth_refresh",
            "body_sha256": _sha256_bytes(body),
        }
    if request_line != "POST /v1/messages?beta=true HTTP/1.1":
        raise RelayEvidenceError(f"Claude 合成 R 出现未知端点：{request_line}")
    encoding = ""
    content_length: int | None = None
    for line in head.split(b"\r\n")[1:]:
        name, colon, value = line.partition(b":")
        if not colon:
            continue
        lowered = name.strip().lower()
        if lowered == b"content-encoding":
            encoding = value.strip().decode("ascii", "replace").lower()
        elif lowered == b"content-length":
            try:
                content_length = int(value.strip())
            except ValueError as error:
                raise RelayEvidenceError("Claude 合成请求 Content-Length 非法。") from error
    if content_length is None or content_length != len(body):
        raise RelayEvidenceError("Claude 合成请求 body 与 Content-Length 不一致。")
    if encoding:
        raise RelayEvidenceError("Claude 合成故障探针不允许压缩请求体。")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RelayEvidenceError("Claude 合成请求 body 不是合法 JSON。") from error
    if not isinstance(payload, dict):
        raise RelayEvidenceError("Claude 合成请求 body 顶层必须是对象。")
    return {
        "connection_id": connection_id,
        "request_line": request_line,
        "kind": "messages",
        "model": payload.get("model"),
        "stream_present": "stream" in payload,
        "stream": payload.get("stream"),
        "body_sha256": _sha256_bytes(body),
    }


def _validate_synthetic_plan(
    *,
    plan: str,
    attempts: list[dict[str, Any]],
    interventions: list[dict[str, Any]],
    synthetic_profile: str,
) -> dict[str, Any]:
    """闭合 Claude 合成故障计划的请求序号、响应动作和生产隔离。"""

    message_attempts = [item for item in attempts if item.get("kind") == "messages"]
    if plan != "oauth-refresh-reject" and not message_attempts:
        raise RelayEvidenceError("Claude 合成 R 没有 messages 请求。")
    response_rows = [
        item
        for item in interventions
        if item.get("type") == "synthetic_claude_response"
    ]
    if len(response_rows) != len(attempts):
        raise RelayEvidenceError("Claude 合成请求与受控响应记录数量不一致。")
    for item in interventions:
        if item.get("production_forwarded") is not False:
            raise RelayEvidenceError("Claude 合成干预没有证明 production_forwarded=false。")
        if item.get("type") != "synthetic_claude_response":
            raise RelayEvidenceError("Claude 合成 R 含非受管干预类型。")
        if item.get("profile") != synthetic_profile or item.get("plan") != plan:
            raise RelayEvidenceError("Claude 合成干预身份与冻结计划不一致。")
    by_connection = {
        item.get("connection_id"): item for item in response_rows
    }
    if len(by_connection) != len(response_rows):
        raise RelayEvidenceError("Claude 合成干预 connection_id 缺失或重复。")
    auxiliary_actions = {
        "policy_limits": "policy_limits_absent",
        "remote_settings": "remote_settings_absent",
    }
    for attempt in attempts:
        response = by_connection.get(attempt.get("connection_id"))
        if response is None:
            raise RelayEvidenceError("Claude 合成请求缺少逐连接响应归属。")
        if attempt.get("kind") == "auxiliary" and response.get("action") != (
            auxiliary_actions.get(str(attempt.get("auxiliary_kind")))
        ):
            raise RelayEvidenceError("Claude 合成辅助端点响应动作漂移。")
        if attempt.get("kind") == "hello" and response.get("action") != "hello_success":
            raise RelayEvidenceError("Claude 合成 hello 响应动作漂移。")
        if (
            attempt.get("kind") == "oauth_refresh"
            and response.get("action") != "oauth_refresh_rejected"
        ):
            raise RelayEvidenceError("Claude OAuth refresh 合成响应动作漂移。")
    message_rows = [
        item for item in response_rows if item.get("message_ordinal") not in {0, None}
    ]
    ordinals = [item.get("message_ordinal") for item in message_rows]
    if ordinals != list(range(1, len(message_attempts) + 1)):
        raise RelayEvidenceError("Claude 合成 messages ordinal 不连续。")

    actions = [str(item.get("action")) for item in message_rows]
    retry_once = {
        "retry-401",
        "retry-408",
        "retry-409",
        "retry-429",
        "retry-429-after-date",
        "retry-429-after-seconds",
        "retry-500",
        "retry-502",
        "retry-503",
        "retry-529",
    }
    if plan == "oauth-refresh-reject":
        oauth_attempts = [item for item in attempts if item.get("kind") == "oauth_refresh"]
        if len(oauth_attempts) != 1 or message_attempts or actions:
            raise RelayEvidenceError("Claude OAuth refresh 合成计划没有精确闭合。")
    elif plan in retry_once:
        if actions != [f"{plan}_fault", f"{plan}_success"]:
            raise RelayEvidenceError("Claude 单故障重试计划没有精确闭合为两次尝试。")
    elif plan in {"nonretry-400", "nonretry-403", "stall"}:
        expected_action = {
            "nonretry-400": "nonretry_400",
            "nonretry-403": "nonretry_403",
            "stall": "stall_without_response",
        }[plan]
        if len(message_attempts) != 1 or actions != [expected_action]:
            raise RelayEvidenceError("Claude 非重试计划出现了额外 messages 尝试。")
    elif plan == "disconnect-retry":
        if len(message_attempts) != 2 or actions[-1] != "disconnect_retry_success":
            raise RelayEvidenceError("Claude 断连重试计划没有闭合。")
    elif plan == "always-529":
        if not actions or any(action != "always_529" for action in actions):
            raise RelayEvidenceError("Claude 最大重试计划响应动作漂移。")
    elif plan == "fallback-model":
        models = [str(item.get("model") or "") for item in message_attempts]
        if len(set(models)) < 2 or "fallback_model_success" not in actions:
            raise RelayEvidenceError("Claude fallback model 计划没有观测到模型切换。")
    elif plan in {"stream-404-fallback", "stream-interrupt-fallback"}:
        streaming = any(
            item.get("stream_present") is True and item.get("stream") is True
            for item in message_attempts
        )
        nonstreaming = any(
            item.get("stream_present") is False for item in message_attempts
        )
        expected_actions = {
            "stream-404-fallback": ["stream_404", "nonstream_fallback_success"],
            "stream-interrupt-fallback": [
                "stream_interrupted",
                "interrupt_nonstream_success",
            ],
        }[plan]
        if not streaming or not nonstreaming or actions != expected_actions:
            raise RelayEvidenceError("Claude stream fallback 计划没有观测到流式到非流式切换。")
    elif plan == "stream-interrupt-no-fallback":
        if (
            len(message_attempts) != 1
            or message_attempts[0].get("stream_present") is not True
            or message_attempts[0].get("stream") is not True
            or actions != ["stream_interrupted"]
        ):
            raise RelayEvidenceError("Claude 禁用 nonstream fallback 的中断计划没有闭合。")
    else:
        raise RelayEvidenceError(f"未知 Claude 合成故障计划：{plan}")
    return {
        "plan": plan,
        "message_attempt_count": len(message_attempts),
        "actions": actions,
        "attempts": attempts,
        "production_forwarding_enabled": False,
    }


def _complete_h1_request_lines(request_stream: bytes) -> list[str]:
    """严格解析完整 H1 请求流，并拒绝无法证明边界的编码。"""

    if not request_stream:
        raise RelayEvidenceError("单向停机连接没有客户端请求字节。")
    cursor = 0
    request_lines: list[str] = []
    while cursor < len(request_stream):
        head_end = request_stream.find(b"\r\n\r\n", cursor)
        if head_end < 0:
            raise RelayEvidenceError("单向停机连接的 H1 首部不完整。")
        head = request_stream[cursor:head_end]
        lines = head.split(b"\r\n")
        if not lines or re.fullmatch(
            rb"[A-Z]+ [^\x00-\x20]+ HTTP/1\.1", lines[0]
        ) is None:
            raise RelayEvidenceError("单向停机连接的 H1 请求行非法。")
        content_lengths: list[int] = []
        for line in lines[1:]:
            if line[:1] in {b" ", b"\t"}:
                raise RelayEvidenceError("单向停机连接含折叠 H1 header。")
            name, separator, value = line.partition(b":")
            if (
                not separator
                or re.fullmatch(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name)
                is None
            ):
                raise RelayEvidenceError("单向停机连接含非法 H1 header。")
            lowered = name.lower()
            if lowered == b"transfer-encoding":
                raise RelayEvidenceError("单向停机连接不接受无法静态定界的传输编码。")
            if lowered == b"content-length":
                raw_length = value.strip()
                if re.fullmatch(rb"[0-9]+", raw_length) is None:
                    raise RelayEvidenceError("单向停机连接 Content-Length 非法。")
                content_lengths.append(int(raw_length))
        if len(set(content_lengths)) > 1:
            raise RelayEvidenceError("单向停机连接含冲突 Content-Length。")
        content_length = content_lengths[0] if content_lengths else 0
        body_start = head_end + 4
        body_end = body_start + content_length
        if body_end > len(request_stream):
            raise RelayEvidenceError("单向停机连接的 H1 body 不完整。")
        request_lines.append(lines[0].decode("latin-1"))
        cursor = body_end
    return request_lines


def _validate_one_sided_shutdown_connection(
    relay_root: Path,
    item: dict[str, Any],
    *,
    synthetic_plan: str | None,
) -> tuple[dict[str, Any], bytes] | None:
    """只接受中继主动停机窗口内、可完整复核的单向真实请求。"""

    marker_present = (
        "termination_reason" in item or "relay_stop_requested" in item
    )
    if not marker_present:
        return None
    if (
        synthetic_plan is not None
        or item.get("valid") is not True
        or "error" in item
        or item.get("termination_reason")
        != ONE_SIDED_SHUTDOWN_TERMINATION_REASON
        or item.get("relay_stop_requested") is not True
    ):
        raise RelayEvidenceError("relay 单向停机终态不是受管的真实请求形态。")

    connection_id = item["connection_id"]
    byte_counts = item.get("bytes")
    digests = item.get("sha256")
    if (
        not isinstance(byte_counts, dict)
        or set(byte_counts) != {"client_to_upstream"}
        or not isinstance(byte_counts.get("client_to_upstream"), int)
        or byte_counts["client_to_upstream"] <= 0
        or not isinstance(digests, dict)
        or set(digests) != {"client_to_upstream"}
        or SHA256_RE.fullmatch(str(digests["client_to_upstream"])) is None
    ):
        raise RelayEvidenceError("relay 单向停机终态的方向声明非法。")

    request_path = relay_root / f"conn{connection_id:03d}.client_to_upstream.bin"
    response_path = relay_root / f"conn{connection_id:03d}.upstream_to_client.bin"
    if request_path.is_symlink() or not request_path.is_file():
        raise RelayEvidenceError("relay 单向停机终态缺少可信客户端字节。")
    if response_path.exists() or response_path.is_symlink():
        raise RelayEvidenceError("relay 单向停机终态意外存在上游响应文件。")
    if (
        request_path.stat().st_size != byte_counts["client_to_upstream"]
        or file_sha256(request_path) != digests["client_to_upstream"]
    ):
        raise RelayEvidenceError("relay 单向停机客户端字节与 manifest 不一致。")

    segments = item.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RelayEvidenceError("relay 单向停机终态缺少客户端分段记录。")
    expected_offset = 0
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or segment.get("direction") != "client_to_upstream"
            or segment.get("offset") != expected_offset
            or not isinstance(segment.get("length"), int)
            or segment["length"] <= 0
        ):
            raise RelayEvidenceError("relay 单向停机客户端分段不连续。")
        expected_offset += segment["length"]
    if expected_offset != byte_counts["client_to_upstream"]:
        raise RelayEvidenceError("relay 单向停机客户端分段总长不一致。")

    request_bytes = request_path.read_bytes()
    request_lines = _complete_h1_request_lines(request_bytes)
    return (
        {
            "connection_id": connection_id,
            "client_to_upstream_sha256": digests["client_to_upstream"],
            "termination_reason": ONE_SIDED_SHUTDOWN_TERMINATION_REASON,
            "request_lines": request_lines,
        },
        request_bytes,
    )


def _validate_relay_integrity(
    relay_root: Path,
    *,
    synthetic_plan: str | None = None,
    synthetic_profile: str | None = None,
    message_request_expectation: str = "at-least-one",
    target_host: str = UPSTREAM_HOST,
) -> dict[str, Any]:
    if message_request_expectation not in {"at-least-one", "zero"}:
        raise RelayEvidenceError("R messages 请求期望值非法。")
    if synthetic_plan and synthetic_profile is None:
        synthetic_profile = "claude-fw-f-v3"
    manifest = _load_json(relay_root / "relay.json", "relay manifest")
    if manifest.get("schema_version") != "byte-relay/v1":
        raise RelayEvidenceError("relay manifest schema 不匹配。")
    if manifest.get("mode") != "direct" or manifest.get("upstream_host") != target_host:
        raise RelayEvidenceError("relay 没有绑定 Claude 官方域名。")
    if synthetic_plan:
        if (
            manifest.get("synthetic_profile") != synthetic_profile
            or manifest.get("claude_fault_plan") != synthetic_plan
            or manifest.get("production_forwarding_enabled") is not False
        ):
            raise RelayEvidenceError("relay 没有绑定冻结的 Claude 合成计划。")
    elif manifest.get("synthetic_profile") or (
        manifest.get("production_forwarding_enabled") is False
    ):
        raise RelayEvidenceError("真实上游 R 证据混入了合成画像。")
    scrubbing = manifest.get("credential_scrubbing")
    if not isinstance(scrubbing, dict) or scrubbing.get("byte_offsets_preserved") is not True:
        raise RelayEvidenceError("relay manifest 缺少等长脱敏证明。")
    connections = manifest.get("connections")
    if not isinstance(connections, list) or not connections:
        raise RelayEvidenceError("relay 未记录任何连接。")
    request_stream = b""
    request_attempts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    one_sided_shutdown_count = 0
    connection_ids: set[int] = set()
    for item in connections:
        if not isinstance(item, dict):
            raise RelayEvidenceError("relay connection 必须是对象。")
        connection_id = item.get("connection_id")
        if not isinstance(connection_id, int) or connection_id <= 0:
            raise RelayEvidenceError("relay connection_id 非法。")
        if connection_id in connection_ids:
            raise RelayEvidenceError("relay connection_id 重复。")
        connection_ids.add(connection_id)
        if item.get("valid") is not True:
            common_keys = {
                "connection_id",
                "client_alpn_offer",
                "alpn_source",
                "client_alpn",
                "bytes",
                "sha256",
                "segments",
                "opened_at_unix_ms",
                "closed_at_unix_ms",
            }
            optional_keys = {"sni"}
            actual_keys = set(item)
            opened_at = item.get("opened_at_unix_ms")
            closed_at = item.get("closed_at_unix_ms")
            common_empty_handshake = (
                item.get("client_alpn_offer") == ["http/1.1"]
                and item.get("alpn_source") == "assumed"
                and item.get("client_alpn") is None
                and item.get("bytes") == {}
                and item.get("sha256") == {}
                and item.get("segments") == []
                and isinstance(opened_at, int)
                and isinstance(closed_at, int)
                and opened_at <= closed_at
                and ("sni" not in item or item.get("sni") == target_host)
            )
            mismatch_keys = common_keys | {"upstream_alpn", "error", "valid"}
            exact_alpn_mismatch = (
                common_empty_handshake
                and mismatch_keys.issubset(actual_keys)
                and actual_keys.issubset(mismatch_keys | optional_keys)
                and item.get("valid") is False
                and item.get("upstream_alpn") == "http/1.1"
                and item.get("error")
                == "ALPN 不一致 client=None upstream=http/1.1"
            )
            exact_relay_shutdown = (
                common_empty_handshake
                and common_keys.issubset(actual_keys)
                and actual_keys.issubset(common_keys | optional_keys)
            )
            reset_keys = {
                "connection_id",
                "client_alpn_offer",
                "alpn_source",
                "bytes",
                "sha256",
                "segments",
                "opened_at_unix_ms",
                "closed_at_unix_ms",
                "error",
                "valid",
            }
            exact_client_reset = (
                item.get("client_alpn_offer") == ["http/1.1"]
                and item.get("alpn_source") == "assumed"
                and item.get("bytes") == {}
                and item.get("sha256") == {}
                and item.get("segments") == []
                and isinstance(opened_at, int)
                and isinstance(closed_at, int)
                and opened_at <= closed_at
                and item.get("valid") is False
                and item.get("error")
                in {
                    "ConnectionResetError: ",
                    "ConnectionResetError: [Errno 104] Connection reset by peer",
                }
                and reset_keys.issubset(actual_keys)
                and actual_keys.issubset(reset_keys | optional_keys)
                and ("sni" not in item or item.get("sni") == target_host)
            )
            unexpected_bytes = any(
                (relay_root / f"conn{connection_id:03d}.{direction}.bin").exists()
                for direction in ("client_to_upstream", "upstream_to_client")
            )
            if (
                not exact_alpn_mismatch
                and not exact_relay_shutdown
                and not exact_client_reset
            ) or unexpected_bytes:
                raise RelayEvidenceError("relay 含非受管的无效连接。")
            if exact_alpn_mismatch:
                reason = EMPTY_HANDSHAKE_TERMINATION_REASON
            elif exact_client_reset:
                reason = CLIENT_RESET_HANDSHAKE_TERMINATION_REASON
            else:
                reason = RELAY_SHUTDOWN_HANDSHAKE_TERMINATION_REASON
            excluded.append(
                {
                    "connection_id": connection_id,
                    "reason": reason,
                    "manifest_error": item.get("error"),
                }
            )
            continue
        client_alpn = item.get("client_alpn")
        upstream_alpn = item.get("upstream_alpn")
        if synthetic_plan:
            alpn_valid = client_alpn in {None, "http/1.1"} and (
                upstream_alpn == "http/1.1"
            )
        elif manifest.get("mirror_selected_alpn") is True:
            expected_offer = [client_alpn] if client_alpn else None
            alpn_valid = (
                client_alpn in {None, "http/1.1"}
                and upstream_alpn == client_alpn
                and item.get("upstream_alpn_offer") == expected_offer
            )
        else:
            alpn_valid = client_alpn == "http/1.1" and upstream_alpn == "http/1.1"
        if not alpn_valid:
            raise RelayEvidenceError("relay 两条 TLS 腿没有保持受管 ALPN 镜像。")
        one_sided = _validate_one_sided_shutdown_connection(
            relay_root,
            item,
            synthetic_plan=synthetic_plan,
        )
        if one_sided is not None:
            verified, request_bytes = one_sided
            request_stream += request_bytes
            rows.append(verified)
            one_sided_shutdown_count += 1
            continue
        verified: dict[str, Any] = {"connection_id": connection_id}
        for direction in ("client_to_upstream", "upstream_to_client"):
            path = relay_root / f"conn{connection_id:03d}.{direction}.bin"
            if not path.is_file() or path.is_symlink():
                raise RelayEvidenceError(f"relay 缺少连接字节：{path.name}")
            expected_bytes = item.get("bytes", {}).get(direction)
            expected_sha = item.get("sha256", {}).get(direction)
            if path.stat().st_size != expected_bytes or file_sha256(path) != expected_sha:
                raise RelayEvidenceError(f"relay 连接字节与 manifest 不一致：{path.name}")
            verified[f"{direction}_sha256"] = expected_sha
            if direction == "client_to_upstream":
                request_bytes = path.read_bytes()
                request_stream += request_bytes
                if synthetic_plan:
                    request_attempts.append(
                        _message_attempt_from_wire(request_bytes, connection_id)
                    )
        rows.append(verified)
    messages = request_stream.count(b"POST /v1/messages?beta=true HTTP/1.1\r\n")
    hello = request_stream.count(b"HEAD /api/hello HTTP/1.1\r\n")
    if message_request_expectation == "at-least-one" and messages < 1:
        raise RelayEvidenceError("R 证据中没有官方 messages 请求。")
    if message_request_expectation == "zero" and messages != 0:
        raise RelayEvidenceError("本地拒绝场景仍发出了 messages 请求。")
    result = {
        "result": "passed",
        "connection_count": len(rows),
        "total_connection_count": len(connections),
        "excluded_handshake_connection_count": len(excluded),
        "one_sided_shutdown_connection_count": one_sided_shutdown_count,
        "excluded_connections": excluded,
        "messages_request_count": messages,
        "message_request_expectation": message_request_expectation,
        "hello_request_count": hello,
        "connections": rows,
        "manifest_sha256": file_sha256(relay_root / "relay.json"),
    }
    if synthetic_plan:
        result["synthetic_plan"] = _validate_synthetic_plan(
            plan=synthetic_plan,
            attempts=request_attempts,
            interventions=_read_interventions(relay_root),
            synthetic_profile=str(synthetic_profile),
        )
    return result


def _manifest_binding(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def _invocation_binding_complete(
    summary: dict[str, Any] | None,
    result_root: Path,
    *,
    fw_f_probe: str | None,
    fw_f_v4_probe: str | None = None,
    response_plan: str | None,
) -> bool:
    """核验旧场景或 FW-F v3/v4 的 argv 与环境事实已绑定到结果。"""

    if not summary:
        return False
    if fw_f_v4_probe is not None:
        path = result_root / "v4-summary.json"
        if not path.is_file() or path.is_symlink():
            return False
        value = _load_json(path, "FW-F v4 result")
        if (
            value.get("schema_version") != "claude-code-fw-f-v4-probe-result/v1"
            or value.get("probe_id") != fw_f_v4_probe
            or value.get("response_plan") != response_plan
            or value.get("valid") is not True
        ):
            return False
        inner = value.get("inner_result")
        if not isinstance(inner, dict):
            return False
        if value.get("driver") == "v3":
            binding = inner.get("invocation")
            if not isinstance(binding, dict):
                return False
            invocation_path = result_root / str(binding.get("path", ""))
            if (
                not invocation_path.is_file()
                or invocation_path.is_symlink()
                or file_sha256(invocation_path) != binding.get("sha256")
                or invocation_path.stat().st_size != binding.get("bytes")
            ):
                return False
            invocation_value = _load_json(invocation_path, "FW-F v4/v3 invocation")
            invocations = invocation_value.get("invocations")
            return bool(
                invocation_value.get("schema_version")
                == "claude-code-fw-f-v3-invocations/v1"
                and invocation_value.get("probe_id") == value.get("source_v3_probe")
                and isinstance(invocations, list)
                and invocations
                and all(
                    isinstance(item, dict)
                    and item.get("argv_sha256")
                    and isinstance(item.get("environment"), dict)
                    and item["environment"].get("sha256")
                    for item in invocations
                )
            )
        invocation = inner.get("invocation")
        return bool(
            isinstance(invocation, dict)
            and invocation.get("argv_sha256")
            and isinstance(invocation.get("environment"), dict)
            and invocation["environment"].get("sha256")
        )
    if fw_f_probe is None:
        invocation = summary.get("invocation", {})
        return bool(
            isinstance(invocation, dict)
            and invocation.get("argv_sha256")
            and isinstance(invocation.get("environment"), dict)
            and invocation["environment"].get("sha256")
        )
    binding = summary.get("invocation")
    if not isinstance(binding, dict):
        return False
    path = result_root / str(binding.get("path", ""))
    if (
        not path.is_file()
        or path.is_symlink()
        or file_sha256(path) != binding.get("sha256")
        or path.stat().st_size != binding.get("bytes")
    ):
        return False
    value = _load_json(path, "FW-F v3 invocation")
    invocations = value.get("invocations")
    return bool(
        value.get("schema_version") == "claude-code-fw-f-v3-invocations/v1"
        and value.get("probe_id") == fw_f_probe
        and value.get("response_plan") == response_plan
        and isinstance(invocations, list)
        and invocations
        and all(
            isinstance(item, dict)
            and item.get("argv_sha256")
            and isinstance(item.get("environment"), dict)
            and item["environment"].get("sha256")
            for item in invocations
        )
    )


def _capture(arguments: argparse.Namespace) -> dict[str, Any]:
    validate_safe_name(arguments.run_id, "run_id")
    validate_safe_name(arguments.probe_id, "probe_id")
    if not VERSION_RE.fullmatch(arguments.expected_version):
        raise ConfigurationError("--expected-version 必须是精确三段版本号。")
    if not SHA256_RE.fullmatch(arguments.expected_sha256):
        raise ConfigurationError("--expected-sha256 格式非法。")
    if not RUNTIME_IMAGE_RE.fullmatch(arguments.runtime_image):
        raise ConfigurationError("--runtime-image 必须是 repository@sha256。")
    if not SHA256_RE.fullmatch(arguments.host_runtime_receipt_sha256):
        raise ConfigurationError("--host-runtime-receipt-sha256 格式非法。")
    if not SHA256_RE.fullmatch(arguments.run_nonce):
        raise ConfigurationError("--run-nonce 格式非法。")
    fw_f_probe_id = getattr(arguments, "fw_f_probe", None)
    fw_f_v4_probe_id = getattr(arguments, "fw_f_v4_probe", None)
    scenario = getattr(arguments, "scenario", None)
    if sum(bool(value) for value in (fw_f_probe_id, fw_f_v4_probe_id, scenario)) > 1:
        raise ConfigurationError("--scenario、--fw-f-probe 与 --fw-f-v4-probe 互斥。")
    fw_f_v4_probe = None
    if fw_f_probe_id:
        if fw_f_probe_id not in FW_F_V3_PROBE_IDS:
            raise ConfigurationError("--fw-f-probe 非法。")
        if arguments.probe_id != fw_f_probe_id:
            raise ConfigurationError("FW-F v3 的 --probe-id 必须等于 --fw-f-probe。")
        if arguments.expected_version != FW_F_V3_VERSION:
            raise ConfigurationError(
                f"FW-F v3 场景只绑定 Claude Code {FW_F_V3_VERSION}。"
            )
        fw_f_probe = get_fw_f_probe(fw_f_probe_id)
        if arguments.inject_env:
            raise ConfigurationError("FW-F v3 场景禁止额外 --inject-env。")
        injected = fw_f_probe.env_dict()
        response_plan = fw_f_probe.response_plan
        message_request_expectation = fw_f_probe.message_request_expectation
        target_host = UPSTREAM_HOST
        capture_mode = "fw-f-v3"
        synthetic_profile = "claude-fw-f-v3" if response_plan else None
        synthetic_success_marker = "FW_F_V3_OK" if response_plan else None
    elif fw_f_v4_probe_id:
        if fw_f_v4_probe_id not in FW_F_V4_PROBE_IDS:
            raise ConfigurationError("--fw-f-v4-probe 非法。")
        if arguments.probe_id != fw_f_v4_probe_id:
            raise ConfigurationError("FW-F v4 的 --probe-id 必须等于 --fw-f-v4-probe。")
        if arguments.expected_version != FW_F_V3_VERSION:
            raise ConfigurationError(
                f"FW-F v4 场景只绑定 Claude Code {FW_F_V3_VERSION}。"
            )
        if arguments.inject_env:
            raise ConfigurationError("FW-F v4 场景禁止额外 --inject-env。")
        fw_f_v4_probe = get_fw_f_v4_probe(fw_f_v4_probe_id)
        injected = fw_f_v4_probe.env_dict()
        response_plan = fw_f_v4_probe.response_plan
        message_request_expectation = fw_f_v4_probe.message_request_expectation
        target_host = fw_f_v4_probe.target_host
        capture_mode = "fw-f-v4"
        synthetic_profile = "claude-fw-f-v4" if response_plan else None
        synthetic_success_marker = (
            "FW_F_V3_OK"
            if response_plan and fw_f_v4_probe.source_v3_probe
            else "FW_F_V4_OK" if response_plan else None
        )
    else:
        scenario = scenario or "s1"
        if scenario not in SCENARIOS:
            raise ConfigurationError("--scenario 非法。")
        injected = parse_injected_env(arguments.inject_env)
        response_plan = None
        message_request_expectation = "at-least-one"
        target_host = UPSTREAM_HOST
        capture_mode = "legacy-scenario"
        synthetic_profile = None
        synthetic_success_marker = None
    if arguments.timeout <= 0:
        raise ConfigurationError("--timeout 必须大于 0。")
    if arguments.port != 443:
        raise ConfigurationError("Claude 官方域名 R 取证只允许隔离容器内端口 443。")
    v4_source_v3_probe = (
        get_fw_f_probe(str(fw_f_v4_probe.source_v3_probe))
        if fw_f_v4_probe is not None and fw_f_v4_probe.source_v3_probe
        else None
    )
    requires_tui_state = bool(
        (fw_f_probe_id and fw_f_probe.driver == "tui")
        or (
            fw_f_v4_probe is not None
            and (
                fw_f_v4_probe.driver == "tui"
                or fw_f_v4_probe.requires_account_state
                or (v4_source_v3_probe and v4_source_v3_probe.driver == "tui")
            )
        )
    )
    requires_refresh_state = bool(
        fw_f_v4_probe is not None and fw_f_v4_probe.driver == "oauth-refresh"
    )
    upstream_value = getattr(arguments, "upstream_ip", None)
    if response_plan:
        if upstream_value:
            raise ConfigurationError("Claude 合成故障场景禁止配置 --upstream-ip。")
        upstream_ip = None
        live_requests = False
    else:
        if not upstream_value:
            raise ConfigurationError("真实上游 R 场景必须提供 --upstream-ip。")
        upstream_ip = _validate_upstream_ip(upstream_value)
        live_requests = True
    _validate_output_root(arguments.output_root, create=arguments.execute)
    if arguments.dry_run:
        return {
            "schema_version": "claude-code-fw-e-relay-plan/v1",
            "run_id": arguments.run_id,
            "probe_id": arguments.probe_id,
            "version": arguments.expected_version,
            "capture_mode": capture_mode,
            "scenario": scenario,
            "fw_f_probe": fw_f_probe_id,
            "fw_f_v4_probe": fw_f_v4_probe_id,
            "response_plan": response_plan,
            "synthetic_success_marker": synthetic_success_marker,
            "message_request_expectation": message_request_expectation,
            "model": arguments.model,
            "upstream_host": target_host,
            "upstream_ip": upstream_ip,
            "injected_probe_env": injected,
            "live_requests": live_requests,
            "production_forwarding_enabled": live_requests,
            "production_changes": False,
        }
    acknowledge_synthetic = getattr(arguments, "acknowledge_synthetic_responses", False)
    if live_requests:
        if not arguments.acknowledge_live_requests or acknowledge_synthetic:
            raise ConfigurationError(
                "真实上游 --execute 必须只确认 --acknowledge-live-requests。"
            )
    elif not acknowledge_synthetic or arguments.acknowledge_live_requests:
        raise ConfigurationError(
            "合成故障 --execute 必须只确认 --acknowledge-synthetic-responses。"
        )

    run_root = arguments.output_root
    manifest_path = run_root / "relay-manifest.json"
    recovery_root = ensure_private_directory(run_root / "recovery", run_root)
    raw_root = run_root / "relay-raw-pending"
    relay_root = run_root / "relay"
    result_root = ensure_private_directory(run_root / "results", run_root)
    claude_oauth_home = prepare_claude_oauth_state(run_root)
    tui_state_receipt: dict[str, Any] = {
        "required": requires_tui_state,
        "storage_scope": "memory-backed-temporary-home",
        "credentials_copied": False,
        "global_state_copied": False,
        "privacy_settings_written": False,
        "onboarding_state_normalized": False,
        "onboarding_normalized_fields": [],
        "source_global_state_modified": False,
        "archived_in_evidence": False,
        "removed": not requires_tui_state,
    }
    refresh_state_receipt: dict[str, Any] = {
        "required": requires_refresh_state,
        "storage_scope": "memory-backed-expired-credential-copy",
        "credentials_copied": False,
        "global_state_copied": False,
        "privacy_settings_written": False,
        "expiry_forced_on_copy": False,
        "production_oauth_forwarding_enabled": False,
        "archived_in_evidence": False,
        "removed": not requires_refresh_state,
    }
    pcap_required = bool(fw_f_v4_probe is not None and fw_f_v4_probe.require_pcap)
    pcap_receipt: dict[str, Any] = {
        "required": pcap_required,
        "stopped": not pcap_required,
        "parsed": not pcap_required,
    }
    tool_root = Path(__file__).resolve().parent
    tool_identity = _tool_identity(tool_root)
    client = _client_identity(
        arguments.claude_bin, arguments.expected_version, arguments.expected_sha256
    )
    access_token, refresh_token = load_claude_credentials(
        arguments.claude_credentials_file
    )
    secrets = {
        "claude_oauth_access_token": access_token,
        "claude_oauth_refresh_token": refresh_token,
    }
    receipt = _host_receipt(arguments, tool_identity["execution_sources"])
    _validate_static_file(arguments.ca_signing_pem, "MITM CA signing PEM")
    _validate_static_file(arguments.ca_cert, "MITM CA certificate")
    if not arguments.hosts_file.is_absolute():
        raise ConfigurationError("--hosts-file 必须是绝对路径。")
    if not arguments.lock_file.is_absolute() or arguments.lock_file.is_symlink():
        raise ConfigurationError("--lock-file 必须是非符号链接绝对路径。")
    if not _port_available(arguments.port):
        raise ConfigurationError("R 中继端口已被占用。")

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_RUN,
        "run_id": arguments.run_id,
        "probe_id": arguments.probe_id,
        "status": "running",
        "started_at_utc": _utc_now(),
        "ended_at_utc": None,
        "client": client,
        "capture_mode": capture_mode,
        "scenario": scenario,
        "fw_f_probe": fw_f_probe_id,
        "fw_f_v4_probe": fw_f_v4_probe_id,
        "response_plan": response_plan,
        "synthetic_success_marker": synthetic_success_marker,
        "message_request_expectation": message_request_expectation,
        "model": arguments.model,
        "injected_probe_env": injected,
        "runtime": {
            "runtime_image_reference": arguments.runtime_image,
            "host_runtime_receipt": receipt,
            "capture_tools": tool_identity,
        },
        "upstream": {
            "host": target_host,
            "ip": upstream_ip,
            "port": 443,
            "relay_listen_port": arguments.port,
            "assumed_alpn": ["http/1.1"],
            "live_requests": live_requests,
            "production_forwarding_enabled": live_requests,
        },
        "cleanup": {
            "relay_stopped": False,
            "hosts_restored": False,
        },
        "secret_scan": {"performed": False, "passed": False, "matches": []},
        "tui_temporary_state": tui_state_receipt,
        "oauth_refresh_temporary_state": refresh_state_receipt,
        "tls_p_channel": pcap_receipt,
        "m_binding": {"complete": False, "requirements": {}},
        "artifacts": [],
    }
    secure_write_json(manifest_path, manifest)

    relay_process: subprocess.Popen[bytes] | None = None
    pcap_process: subprocess.Popen[bytes] | None = None
    relay_stopped = False
    hosts = HostsOverride(
        arguments.hosts_file,
        target_host,
        recovery_root / "hosts.before",
        arguments.run_id,
    )
    scenario_summary: dict[str, Any] | None = None
    relay_integrity: dict[str, Any] | None = None
    dimension_evidence: dict[str, Any] | None = None
    scrubbing: dict[str, Any] | None = None
    failure: BaseException | None = None
    with CampaignLock(arguments.lock_file):
        try:
            with tempfile.TemporaryDirectory(prefix="claude-fw-e-r-") as temporary:
                temporary_root = Path(temporary)
                chain, leaf_key = _generate_leaf_certificate(
                    temporary_root,
                    arguments.ca_signing_pem,
                    arguments.ca_cert,
                    Path(tool_identity["openssl"]["path"]),
                    target_host,
                )
                ensure_private_directory(raw_root, run_root)
                relay_command = [
                    sys.executable,
                    str(tool_root / "upstream_byte_relay.py"),
                    "--cert",
                    str(chain),
                    "--key",
                    str(leaf_key),
                    "--mode",
                    "direct",
                    "--port",
                    str(arguments.port),
                    "--upstream-host",
                    target_host,
                    "--assume-alpn",
                    "http/1.1",
                    "--mirror-selected-alpn",
                    "--output",
                    str(raw_root),
                    "--timeout",
                    str(arguments.timeout + 60),
                ]
                if response_plan:
                    relay_command.extend(
                        (
                            "--synthetic-profile",
                            str(synthetic_profile),
                            "--allow-synthetic-responses",
                            "--claude-version",
                            arguments.expected_version,
                            "--claude-fault-plan",
                            response_plan,
                            "--claude-success-marker",
                            str(synthetic_success_marker),
                        )
                    )
                else:
                    relay_command.extend(("--upstream-ip", str(upstream_ip)))
                secure_write_json(
                    run_root / "relay-invocation.json",
                    argv_manifest_view(relay_command),
                )
                relay_stdout = (run_root / "relay.stdout.log").open("wb")
                relay_stderr = (run_root / "relay.stderr.log").open("wb")
                try:
                    relay_process = subprocess.Popen(
                        relay_command,
                        env=clean_environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=relay_stdout,
                        stderr=relay_stderr,
                        **_popen_safety_options(),
                    )
                    time.sleep(1)
                    if relay_process.poll() is not None:
                        raise RelayEvidenceError("R 字节中继启动失败。")
                    if pcap_required:
                        pcap_process, pcap_receipt = _start_pcap(
                            run_root / "tls-clienthello.pcap",
                            run_root / "tcpdump.log",
                        )
                    with hosts:
                        environment = clean_environment(os.environ, injected)
                        environment.update(PRIVACY_ENV)
                        environment["CLAUDE_CODE_OAUTH_TOKEN"] = access_token
                        environment["NODE_EXTRA_CA_CERTS"] = str(arguments.ca_cert)
                        environment["CLAUDE_CONFIG_DIR"] = str(claude_oauth_home)
                        environment["HOME"] = str(claude_oauth_home)
                        def execute_selected_probe(
                            selected_environment: dict[str, str],
                        ) -> dict[str, Any]:
                            if fw_f_v4_probe_id:
                                return run_claude_fw_f_complete_probe(
                                    claude_bin=str(arguments.claude_bin),
                                    model=arguments.model,
                                    probe_id=fw_f_v4_probe_id,
                                    environment=selected_environment,
                                    output_dir=result_root,
                                    timeout=arguments.timeout,
                                    known_secrets=secrets,
                                )
                            if fw_f_probe_id:
                                return run_claude_fw_f_probe(
                                    claude_bin=str(arguments.claude_bin),
                                    model=arguments.model,
                                    probe_id=fw_f_probe_id,
                                    environment=selected_environment,
                                    output_dir=result_root,
                                    timeout=arguments.timeout,
                                    known_secrets=secrets,
                                )
                            return run_claude_scenario(
                                claude_bin=str(arguments.claude_bin),
                                model=arguments.model,
                                scenario=str(scenario),
                                environment=selected_environment,
                                output_dir=result_root,
                                timeout=arguments.timeout,
                                runtime_secret=access_token,
                                known_secrets=secrets,
                            )

                        if requires_tui_state:
                            with temporary_claude_tui_state(
                                arguments.claude_credentials_file,
                                arguments.claude_global_state_file,
                                expected_version=arguments.expected_version,
                            ) as (tui_home, _tui_config, state_receipt):
                                tui_state_receipt = state_receipt
                                environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                                # TUI 必须按默认 HOME 布局读取两份官方状态。
                                environment.pop("CLAUDE_CONFIG_DIR", None)
                                environment["HOME"] = str(tui_home)
                                scenario_summary = execute_selected_probe(environment)
                        elif requires_refresh_state:
                            with temporary_claude_refresh_state(
                                arguments.claude_credentials_file,
                                arguments.claude_global_state_file,
                            ) as (refresh_home, refresh_config, state_receipt):
                                refresh_state_receipt = state_receipt
                                environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                                environment["HOME"] = str(refresh_home)
                                environment["CLAUDE_CONFIG_DIR"] = str(refresh_config)
                                scenario_summary = execute_selected_probe(environment)
                        else:
                            scenario_summary = execute_selected_probe(environment)
                        if scenario_summary.get("valid") is not True:
                            raise RelayEvidenceError("Claude R 场景校验失败。")
                finally:
                    relay_stopped = _stop_process(relay_process)
                    pcap_stopped = _stop_pcap(pcap_process)
                    pcap_receipt["stopped"] = pcap_stopped
                    relay_stdout.close()
                    relay_stderr.close()
                if not relay_stopped:
                    raise RelayEvidenceError("R 字节中继未能停止。")
                if not pcap_receipt.get("stopped"):
                    raise RelayEvidenceError("TLS P 通道 tcpdump 未能停止。")
                if pcap_required:
                    parsed_pcap = _parse_pcap(
                        run_root / "tls-clienthello.pcap", target_host
                    )
                    pcap_receipt.update(parsed_pcap)
                scrubbing = _scrub_relay(
                    tool_root, raw_root, relay_root, run_root / "scrub.log"
                )
                relay_integrity = _validate_relay_integrity(
                    relay_root,
                    synthetic_plan=response_plan,
                    synthetic_profile=synthetic_profile,
                    message_request_expectation=message_request_expectation,
                    target_host=target_host,
                )
                if fw_f_v4_probe_id:
                    if scenario_summary is None:
                        raise RelayEvidenceError("FW-F v4 缺少场景结果。")
                    dimension_evidence = validate_complete_probe_evidence(
                        probe_id=fw_f_v4_probe_id,
                        relay_root=relay_root,
                        scenario_summary=scenario_summary,
                        relay_integrity=relay_integrity,
                        pcap_receipt=pcap_receipt,
                    )
                    secure_write_json(
                        run_root / "dimension-evidence.json", dimension_evidence
                    )
                    if dimension_evidence.get("result") != "passed":
                        raise RelayEvidenceError(
                            "Claude v4 实际 wire／工具／状态维度断言失败。"
                        )
        except BaseException as error:  # 失败也必须写恢复和秘密扫描事实
            failure = error
            relay_stopped = _stop_process(relay_process)
            pcap_receipt["stopped"] = _stop_pcap(pcap_process)
            if raw_root.is_dir() and (raw_root / "relay.json").is_file() and not relay_root.exists():
                try:
                    scrubbing = _scrub_relay(
                        tool_root, raw_root, relay_root, run_root / "scrub.log"
                    )
                except BaseException:
                    pass

    scan = scan_for_secrets(run_root, secrets)
    requirements = {
        "runtime_identity": receipt.get("container_runtime_binding", {}).get("verified")
        is True,
        "client_identity": bool(client.get("sha256")),
        "capture_execution_sources": bool(
            tool_identity["execution_sources"].get("sha256")
        ),
        "scenario_invocation_and_environment": _invocation_binding_complete(
            scenario_summary,
            result_root,
            fw_f_probe=fw_f_probe_id,
            fw_f_v4_probe=fw_f_v4_probe_id,
            response_plan=response_plan,
        ),
        "production_forwarding_boundary": live_requests
        or bool(
            relay_integrity
            and relay_integrity.get("synthetic_plan", {}).get(
                "production_forwarding_enabled"
            )
            is False
        ),
        "relay_integrity": bool(relay_integrity and relay_integrity.get("result") == "passed"),
        "actual_dimension_evidence": bool(
            fw_f_v4_probe_id is None
            or (
                dimension_evidence
                and dimension_evidence.get("result") == "passed"
            )
        ),
        "equal_length_scrubbing": bool(scrubbing and scrubbing.get("verified")),
        "exact_secret_scan": scan.get("passed") is True,
        "tui_temporary_state_cleanup": bool(
            tui_state_receipt.get("removed") is True
            and tui_state_receipt.get("archived_in_evidence") is False
            and (
                tui_state_receipt.get("required") is not True
                or (
                    tui_state_receipt.get("credentials_copied") is True
                    and tui_state_receipt.get("global_state_copied") is True
                    and tui_state_receipt.get("privacy_settings_written") is True
                    and tui_state_receipt.get("onboarding_state_normalized")
                    is True
                    and tui_state_receipt.get("source_global_state_modified")
                    is False
                )
            )
        ),
        "oauth_refresh_temporary_state_cleanup": bool(
            refresh_state_receipt.get("removed") is True
            and refresh_state_receipt.get("archived_in_evidence") is False
            and (
                refresh_state_receipt.get("required") is not True
                or (
                    refresh_state_receipt.get("credentials_copied") is True
                    and refresh_state_receipt.get("global_state_copied") is True
                    and refresh_state_receipt.get("privacy_settings_written") is True
                    and refresh_state_receipt.get("expiry_forced_on_copy") is True
                    and refresh_state_receipt.get(
                        "production_oauth_forwarding_enabled"
                    )
                    is False
                )
            )
        ),
        "tls_p_channel": bool(
            pcap_receipt.get("stopped") is True
            and pcap_receipt.get("parsed") is True
        ),
        "relay_stopped": relay_stopped,
        "hosts_restored": hosts.record.get("restored") is True,
        "campaign_status": failure is None,
    }
    manifest.update(
        {
            "status": "complete" if failure is None else "failed",
            "ended_at_utc": _utc_now(),
            "scenario_result": scenario_summary,
            "relay_integrity": relay_integrity,
            "dimension_evidence": dimension_evidence,
            "credential_scrubbing": scrubbing,
            "hosts_recovery": hosts.record,
            "cleanup": {
                "relay_stopped": relay_stopped,
                "hosts_restored": hosts.record.get("restored") is True,
            },
            "secret_scan": scan,
            "tui_temporary_state": tui_state_receipt,
            "oauth_refresh_temporary_state": refresh_state_receipt,
            "tls_p_channel": pcap_receipt,
            "m_binding": {
                "complete": all(requirements.values()),
                "requirements": requirements,
                "limitations": [
                    key for key, satisfied in requirements.items() if not satisfied
                ],
            },
        }
    )
    if failure is not None:
        safe_error = str(failure)
        for secret in secrets.values():
            safe_error = redact_known_secret(safe_error, secret)
        manifest["error"] = safe_error
    manifest["artifacts"] = [
        item
        for item in inventory_artifacts(run_root)
        if item.get("path") != manifest_path.name
    ]
    secure_write_json(manifest_path, manifest)
    if failure is not None:
        raise RelayEvidenceError(str(manifest.get("error", "R 取证失败"))) from failure
    if manifest["m_binding"]["complete"] is not True:
        raise RelayEvidenceError("R 取证完整 M 门禁未闭合。")
    return manifest


def _scan_group(
    root: Path,
    label: str,
    expected_version: str,
    expected_sha256: str,
    expected_probes: list[str],
) -> dict[str, Any]:
    manifests = sorted(root.rglob("relay-manifest.json"))
    if not manifests:
        raise RelayEvidenceError(f"{label} 没有 R manifest。")
    rows: list[dict[str, Any]] = []
    probes: list[str] = []
    sources: set[str] = set()
    for path in manifests:
        value = _load_json(path, f"{label} R manifest")
        if value.get("schema_version") != SCHEMA_RUN or value.get("status") != "complete":
            raise RelayEvidenceError(f"{label} R 运行未完成：{path}")
        if value.get("m_binding", {}).get("complete") is not True:
            raise RelayEvidenceError(f"{label} R 运行缺完整 M：{path}")
        if value.get("secret_scan", {}).get("passed") is not True:
            raise RelayEvidenceError(f"{label} R 终态秘密扫描失败：{path}")
        if value.get("cleanup") != {"relay_stopped": True, "hosts_restored": True}:
            raise RelayEvidenceError(f"{label} R 恢复证明失败：{path}")
        client = value.get("client")
        if not isinstance(client, dict):
            raise RelayEvidenceError(f"{label} R 缺少客户端身份：{path}")
        actual_version = str(client.get("version", "")).split(" ", 1)[0]
        if actual_version != expected_version or client.get("sha256") != expected_sha256:
            raise RelayEvidenceError(f"{label} R 客户端身份漂移：{path}")
        runtime = value.get("runtime")
        tools = runtime.get("capture_tools") if isinstance(runtime, dict) else None
        execution = tools.get("execution_sources") if isinstance(tools, dict) else None
        source_sha = execution.get("sha256") if isinstance(execution, dict) else None
        if not isinstance(source_sha, str) or not SHA256_RE.fullmatch(source_sha):
            raise RelayEvidenceError(f"{label} R 缺少执行源摘要：{path}")
        receipt = runtime.get("host_runtime_receipt") if isinstance(runtime, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("repo_digest_verified") is not True
            or receipt.get("container_runtime_binding", {}).get("verified") is not True
        ):
            raise RelayEvidenceError(f"{label} R 宿主运行身份未验证：{path}")
        if value.get("relay_integrity", {}).get("result") != "passed":
            raise RelayEvidenceError(f"{label} R relay 完整性失败：{path}")
        probe = str(value.get("probe_id", ""))
        validate_safe_name(probe, "probe_id")
        probes.append(probe)
        sources.add(source_sha)
        rows.append(
            {
                "probe_id": probe,
                "scenario": value.get("scenario"),
                "injected_probe_env": value.get("injected_probe_env"),
                "manifest": _manifest_binding(path, root),
                "runtime_image_reference": receipt.get("runtime_image_reference"),
            }
        )
    if len(probes) != len(set(probes)):
        raise RelayEvidenceError(f"{label} R probe 必须唯一。")
    actual_probes = sorted(probes)
    if actual_probes != expected_probes:
        missing = sorted(set(expected_probes) - set(actual_probes))
        extra = sorted(set(actual_probes) - set(expected_probes))
        raise RelayEvidenceError(f"{label} R probe 未闭合：missing={missing}, extra={extra}")
    if len(sources) != 1:
        raise RelayEvidenceError(f"{label} R 使用了多份执行源。")
    return {
        "label": label,
        "version": expected_version,
        "binary_sha256": expected_sha256,
        "capture_source_sha256": next(iter(sources)),
        "probe_ids": actual_probes,
        "runs": sorted(rows, key=lambda item: str(item["probe_id"])),
    }


def _build_index(arguments: argparse.Namespace) -> dict[str, Any]:
    expected_probes = sorted(
        set(item.strip() for item in arguments.expected_probes.split(",") if item.strip())
    )
    if not expected_probes:
        raise ConfigurationError("--expected-probes 不能为空。")
    for probe in expected_probes:
        validate_safe_name(probe, "expected probe")
    control = _scan_group(
        arguments.control_root,
        "control",
        arguments.control_version,
        arguments.control_sha256,
        expected_probes,
    )
    target = _scan_group(
        arguments.target_root,
        "target",
        arguments.target_version,
        arguments.target_sha256,
        expected_probes,
    )
    if control["capture_source_sha256"] != target["capture_source_sha256"]:
        raise RelayEvidenceError("控制组与目标组 R 没有使用同一冻结执行源。")
    control_shape = [
        (row["probe_id"], row["scenario"], row["injected_probe_env"])
        for row in control["runs"]
    ]
    target_shape = [
        (row["probe_id"], row["scenario"], row["injected_probe_env"])
        for row in target["runs"]
    ]
    if control_shape != target_shape:
        raise RelayEvidenceError("控制组与目标组 R 场景或条件不对称。")
    result = {
        "schema_version": SCHEMA_INDEX,
        "result": "passed",
        "capture_source_sha256": control["capture_source_sha256"],
        "expected_probe_ids": expected_probes,
        "control": control,
        "target": target,
        "producer": {
            "path": "tools/official_client_capture/claude_fw_e_relay.py",
            "sha256": file_sha256(Path(__file__)),
        },
        "generated_at_utc": _utc_now(),
    }
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ConfigurationError("R index 输出已存在，拒绝覆盖。")
    secure_write_json(arguments.output, result)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture", help="执行一轮受管 R 取证")
    mode = capture.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    capture.add_argument("--acknowledge-live-requests", action="store_true")
    capture.add_argument("--acknowledge-synthetic-responses", action="store_true")
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--probe-id", required=True)
    capture.add_argument("--output-root", type=Path, required=True)
    capture.add_argument("--claude-bin", type=Path, required=True)
    capture.add_argument("--expected-version", required=True)
    capture.add_argument("--expected-sha256", required=True)
    capture.add_argument("--claude-credentials-file", type=Path, required=True)
    capture.add_argument(
        "--claude-global-state-file",
        type=Path,
        default=Path("/root/.claude.json"),
        help="仅 TUI 探针使用的官方全局状态文件；只短暂复制到 /dev/shm",
    )
    probe_mode = capture.add_mutually_exclusive_group()
    probe_mode.add_argument("--scenario", choices=SCENARIOS)
    probe_mode.add_argument("--fw-f-probe", choices=FW_F_V3_PROBE_IDS)
    probe_mode.add_argument("--fw-f-v4-probe", choices=FW_F_V4_PROBE_IDS)
    capture.add_argument("--model", default="claude-sonnet-5")
    capture.add_argument("--inject-env", action="append", metavar="KEY=VALUE")
    capture.add_argument(
        "--upstream-ip",
        help="真实上游场景必填；FW-F v3 合成故障场景禁止提供",
    )
    capture.add_argument("--ca-signing-pem", type=Path, required=True)
    capture.add_argument("--ca-cert", type=Path, required=True)
    capture.add_argument("--host-runtime-receipt", type=Path, required=True)
    capture.add_argument("--host-runtime-receipt-sha256", required=True)
    capture.add_argument("--run-nonce", required=True)
    capture.add_argument("--runtime-image", default=DEFAULT_RUNTIME_IMAGE)
    capture.add_argument("--hosts-file", type=Path, default=Path("/etc/hosts"))
    capture.add_argument(
        "--lock-file", type=Path, default=Path("/run/official-client-capture.lock")
    )
    capture.add_argument("--port", type=int, default=443)
    capture.add_argument("--timeout", type=int, default=300)

    index = commands.add_parser("index", help="闭合控制组与目标组 R 证据")
    index.add_argument("--control-root", type=Path, required=True)
    index.add_argument("--target-root", type=Path, required=True)
    index.add_argument("--control-version", required=True)
    index.add_argument("--target-version", required=True)
    index.add_argument("--control-sha256", required=True)
    index.add_argument("--target-sha256", required=True)
    index.add_argument("--expected-probes", required=True)
    index.add_argument("--output", type=Path, required=True)
    return parser


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.command == "capture":
        return _capture(arguments)
    if arguments.command == "index":
        return _build_index(arguments)
    raise ConfigurationError(f"未处理命令：{arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        result = execute(_build_parser().parse_args(argv))
    except (
        ConfigurationError,
        ClaudeFWFProbeError,
        ClaudeFWFCompleteProbeError,
        RelayEvidenceError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Claude FW-E R 拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
