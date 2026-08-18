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
DEFAULT_RUNTIME_IMAGE = (
    "oauth-egress-capture-capture-cli@"
    "sha256:3438c4e0909d7401ff8e076a985258608a8f031629e65262db16c1979ab1771c"
)


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
    work_root: Path, ca_signing_pem: Path, ca_cert: Path, openssl: Path
) -> tuple[Path, Path]:
    key_path = work_root / "leaf.key"
    csr_path = work_root / "leaf.csr"
    cert_path = work_root / "leaf.crt"
    chain_path = work_root / "chain.pem"
    extension_path = work_root / "leaf.cnf"
    secure_write_text(
        extension_path,
        f"subjectAltName=DNS:{UPSTREAM_HOST}\nextendedKeyUsage=serverAuth\n",
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
            f"/CN={UPSTREAM_HOST}",
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
    shutil.rmtree(raw_root)
    return {
        "method": "equal_length_replacement",
        "verified": True,
        "tool_sha256": file_sha256(tool_root / "scrub_raw_bytes.py"),
        "log_sha256": file_sha256(log_path),
    }


def _validate_relay_integrity(relay_root: Path) -> dict[str, Any]:
    manifest = _load_json(relay_root / "relay.json", "relay manifest")
    if manifest.get("schema_version") != "byte-relay/v1":
        raise RelayEvidenceError("relay manifest schema 不匹配。")
    if manifest.get("mode") != "direct" or manifest.get("upstream_host") != UPSTREAM_HOST:
        raise RelayEvidenceError("relay 没有绑定官方直连上游。")
    scrubbing = manifest.get("credential_scrubbing")
    if not isinstance(scrubbing, dict) or scrubbing.get("byte_offsets_preserved") is not True:
        raise RelayEvidenceError("relay manifest 缺少等长脱敏证明。")
    connections = manifest.get("connections")
    if not isinstance(connections, list) or not connections:
        raise RelayEvidenceError("relay 未记录任何连接。")
    request_stream = b""
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
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
                and ("sni" not in item or item.get("sni") == UPSTREAM_HOST)
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
            unexpected_bytes = any(
                (relay_root / f"conn{connection_id:03d}.{direction}.bin").exists()
                for direction in ("client_to_upstream", "upstream_to_client")
            )
            if (not exact_alpn_mismatch and not exact_relay_shutdown) or unexpected_bytes:
                raise RelayEvidenceError("relay 含非受管的无效连接。")
            reason = (
                EMPTY_HANDSHAKE_TERMINATION_REASON
                if exact_alpn_mismatch
                else RELAY_SHUTDOWN_HANDSHAKE_TERMINATION_REASON
            )
            excluded.append(
                {
                    "connection_id": connection_id,
                    "reason": reason,
                    "manifest_error": item.get("error"),
                }
            )
            continue
        if item.get("client_alpn") != "http/1.1" or item.get("upstream_alpn") != "http/1.1":
            raise RelayEvidenceError("relay 两条 TLS 腿没有保持 HTTP/1.1 一致。")
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
                request_stream += path.read_bytes()
        rows.append(verified)
    messages = request_stream.count(b"POST /v1/messages?beta=true HTTP/1.1\r\n")
    hello = request_stream.count(b"HEAD /api/hello HTTP/1.1\r\n")
    if messages < 1:
        raise RelayEvidenceError("R 证据中没有官方 messages 请求。")
    return {
        "result": "passed",
        "connection_count": len(rows),
        "total_connection_count": len(connections),
        "excluded_handshake_connection_count": len(excluded),
        "excluded_connections": excluded,
        "messages_request_count": messages,
        "hello_request_count": hello,
        "connections": rows,
        "manifest_sha256": file_sha256(relay_root / "relay.json"),
    }


def _manifest_binding(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


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
    if arguments.scenario not in SCENARIOS:
        raise ConfigurationError("--scenario 非法。")
    if arguments.timeout <= 0:
        raise ConfigurationError("--timeout 必须大于 0。")
    if arguments.port != 443:
        raise ConfigurationError("Claude 官方域名 R 取证只允许隔离容器内端口 443。")
    injected = parse_injected_env(arguments.inject_env)
    upstream_ip = _validate_upstream_ip(arguments.upstream_ip)
    _validate_output_root(arguments.output_root, create=arguments.execute)
    if arguments.dry_run:
        return {
            "schema_version": "claude-code-fw-e-relay-plan/v1",
            "run_id": arguments.run_id,
            "probe_id": arguments.probe_id,
            "version": arguments.expected_version,
            "scenario": arguments.scenario,
            "model": arguments.model,
            "upstream_host": UPSTREAM_HOST,
            "upstream_ip": upstream_ip,
            "injected_probe_env": injected,
            "live_requests": True,
            "production_changes": False,
        }
    if not arguments.acknowledge_live_requests:
        raise ConfigurationError("--execute 必须显式确认 --acknowledge-live-requests。")

    run_root = arguments.output_root
    manifest_path = run_root / "relay-manifest.json"
    recovery_root = ensure_private_directory(run_root / "recovery", run_root)
    raw_root = run_root / "relay-raw-pending"
    relay_root = run_root / "relay"
    result_root = ensure_private_directory(run_root / "results", run_root)
    claude_oauth_home = prepare_claude_oauth_state(run_root)
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
        "scenario": arguments.scenario,
        "model": arguments.model,
        "injected_probe_env": injected,
        "runtime": {
            "runtime_image_reference": arguments.runtime_image,
            "host_runtime_receipt": receipt,
            "capture_tools": tool_identity,
        },
        "upstream": {
            "host": UPSTREAM_HOST,
            "ip": upstream_ip,
            "port": 443,
            "relay_listen_port": arguments.port,
            "assumed_alpn": ["http/1.1"],
        },
        "cleanup": {
            "relay_stopped": False,
            "hosts_restored": False,
        },
        "secret_scan": {"performed": False, "passed": False, "matches": []},
        "m_binding": {"complete": False, "requirements": {}},
        "artifacts": [],
    }
    secure_write_json(manifest_path, manifest)

    relay_process: subprocess.Popen[bytes] | None = None
    relay_stopped = False
    hosts = HostsOverride(
        arguments.hosts_file,
        UPSTREAM_HOST,
        recovery_root / "hosts.before",
        arguments.run_id,
    )
    scenario_summary: dict[str, Any] | None = None
    relay_integrity: dict[str, Any] | None = None
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
                    UPSTREAM_HOST,
                    "--upstream-ip",
                    upstream_ip,
                    "--assume-alpn",
                    "http/1.1",
                    "--output",
                    str(raw_root),
                    "--timeout",
                    str(arguments.timeout + 60),
                ]
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
                    with hosts:
                        environment = clean_environment(os.environ, injected)
                        environment.update(PRIVACY_ENV)
                        environment["CLAUDE_CODE_OAUTH_TOKEN"] = access_token
                        environment["NODE_EXTRA_CA_CERTS"] = str(arguments.ca_cert)
                        environment["CLAUDE_CONFIG_DIR"] = str(claude_oauth_home)
                        environment["HOME"] = str(claude_oauth_home)
                        scenario_summary = run_claude_scenario(
                            claude_bin=str(arguments.claude_bin),
                            model=arguments.model,
                            scenario=arguments.scenario,
                            environment=environment,
                            output_dir=result_root,
                            timeout=arguments.timeout,
                            runtime_secret=access_token,
                            known_secrets=secrets,
                        )
                        if scenario_summary.get("valid") is not True:
                            raise RelayEvidenceError("Claude R 场景校验失败。")
                finally:
                    relay_stopped = _stop_process(relay_process)
                    relay_stdout.close()
                    relay_stderr.close()
                if not relay_stopped:
                    raise RelayEvidenceError("R 字节中继未能停止。")
                scrubbing = _scrub_relay(
                    tool_root, raw_root, relay_root, run_root / "scrub.log"
                )
                relay_integrity = _validate_relay_integrity(relay_root)
        except BaseException as error:  # 失败也必须写恢复和秘密扫描事实
            failure = error
            relay_stopped = _stop_process(relay_process)
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
        "scenario_invocation_and_environment": bool(
            scenario_summary
            and scenario_summary.get("invocation", {}).get("argv_sha256")
            and scenario_summary.get("invocation", {})
            .get("environment", {})
            .get("sha256")
        ),
        "relay_integrity": bool(relay_integrity and relay_integrity.get("result") == "passed"),
        "equal_length_scrubbing": bool(scrubbing and scrubbing.get("verified")),
        "exact_secret_scan": scan.get("passed") is True,
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
            "credential_scrubbing": scrubbing,
            "hosts_recovery": hosts.record,
            "cleanup": {
                "relay_stopped": relay_stopped,
                "hosts_restored": hosts.record.get("restored") is True,
            },
            "secret_scan": scan,
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
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--probe-id", required=True)
    capture.add_argument("--output-root", type=Path, required=True)
    capture.add_argument("--claude-bin", type=Path, required=True)
    capture.add_argument("--expected-version", required=True)
    capture.add_argument("--expected-sha256", required=True)
    capture.add_argument("--claude-credentials-file", type=Path, required=True)
    capture.add_argument("--scenario", choices=SCENARIOS, default="s1")
    capture.add_argument("--model", default="claude-sonnet-5")
    capture.add_argument("--inject-env", action="append", metavar="KEY=VALUE")
    capture.add_argument("--upstream-ip", required=True)
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
