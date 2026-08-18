#!/usr/bin/env python3
"""用冻结的官方 Claude Code 在内存文件系统中受管刷新 OAuth 凭据。

本工具只解决 FW-E 取证前的过期访问令牌，不生成任何画像、Snapshot、Persona
或 production binding。刷新请求由指定且已校验摘要的官方 Claude Code 自身发出；
旧刷新令牌只进入当前进程和官方子进程环境，官方临时状态固定写入 tmpfs。只有在
官方客户端成功返回、输出凭据通过完整校验且证据根精确秘密扫描通过后，才原子替换
唯一的长期 credentials 文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.environment import (  # noqa: E402
    PRIVACY_ENV,
    clean_environment,
)
from tools.official_client_capture.capturelib.identity import (  # noqa: E402
    capture_source_bundle_identity,
)
from tools.official_client_capture.capturelib.model import (  # noqa: E402
    ConfigurationError,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    argv_manifest_view,
    file_sha256,
    redact_known_secret,
    scan_for_secrets,
)


SCHEMA_RECEIPT = "claude-code-oauth-managed-refresh/v1"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCOPE_RE = re.compile(r"^[A-Za-z0-9:_./-]+$")
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 16 * 1024


class ClaudeOAuthRefreshError(RuntimeError):
    """表示官方刷新、凭据校验或终态秘密门禁没有闭合。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Claude credentials 存在重复键：{key}")
        result[key] = value
    return result


def _validate_secret(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise ConfigurationError(f"Claude {label} 缺失、为空或长度异常。")
    if any(ord(character) < 0x21 or ord(character) == 0x7F for character in value):
        raise ConfigurationError(f"Claude {label} 含空白或控制字符。")
    return value


def _validate_credentials_path(path: Path) -> os.stat_result:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError("--credentials-file 必须是可信绝对普通文件。")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ConfigurationError("Claude credentials 必须归当前用户所有且禁止组/其他用户读取。")
    if metadata.st_size <= 0 or metadata.st_size > MAX_CREDENTIAL_BYTES:
        raise ConfigurationError("Claude credentials 大小异常。")
    parent = path.parent
    parent_metadata = parent.stat()
    if parent.is_symlink() or not parent.is_dir() or parent_metadata.st_uid != os.geteuid():
        raise ConfigurationError("Claude credentials 父目录不可信。")
    if parent_metadata.st_mode & 0o077:
        raise ConfigurationError("Claude credentials 父目录必须禁止组/其他用户访问。")
    return metadata


def load_claude_credentials_document(path: Path) -> dict[str, Any]:
    """读取并校验 Claude credentials；调用方不得记录返回的令牌值。"""

    _validate_credentials_path(path)
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("Claude credentials 不是合法 UTF-8 JSON。") from error
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        raise ConfigurationError("Claude credentials 缺少 claudeAiOauth。")
    access_token = _validate_secret(oauth.get("accessToken"), "OAuth access token")
    refresh_token = _validate_secret(oauth.get("refreshToken"), "OAuth refresh token")
    if access_token == refresh_token:
        raise ConfigurationError("Claude OAuth access/refresh token 相互冲突。")
    return payload


def load_claude_credentials(path: Path) -> tuple[str, str]:
    """返回访问令牌和刷新令牌，仅供当前受管进程内存使用。"""

    document = load_claude_credentials_document(path)
    oauth = document["claudeAiOauth"]
    return str(oauth["accessToken"]), str(oauth["refreshToken"])


def _validated_scopes(oauth: dict[str, Any]) -> list[str]:
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise ConfigurationError("受管刷新要求 credentials 提供非空 scopes。")
    result: list[str] = []
    for scope in scopes:
        if not isinstance(scope, str) or not SCOPE_RE.fullmatch(scope):
            raise ConfigurationError("Claude OAuth scopes 含非法值。")
        if scope not in result:
            result.append(scope)
    return result


def _validate_timestamp(value: Any, label: str, *, required: bool) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"Claude {label} 不是毫秒时间戳。")
    timestamp = int(value)
    if timestamp <= 0:
        raise ConfigurationError(f"Claude {label} 不是正数。")
    return timestamp


def _oauth_metadata(document: dict[str, Any], *, require_fresh: bool) -> dict[str, Any]:
    oauth = document["claudeAiOauth"]
    access_token = _validate_secret(oauth.get("accessToken"), "OAuth access token")
    refresh_token = _validate_secret(oauth.get("refreshToken"), "OAuth refresh token")
    scopes = _validated_scopes(oauth)
    expires_at = _validate_timestamp(oauth.get("expiresAt"), "expiresAt", required=True)
    refresh_expires_at = _validate_timestamp(
        oauth.get("refreshTokenExpiresAt"), "refreshTokenExpiresAt", required=False
    )
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    if require_fresh and (expires_at is None or expires_at <= now_ms + 60_000):
        raise ConfigurationError("官方刷新结果的 access token 有效期不足 60 秒。")
    if require_fresh and refresh_expires_at is not None and refresh_expires_at <= now_ms:
        raise ConfigurationError("官方刷新结果的 refresh token 已过期。")
    client_id = oauth.get("clientId")
    if client_id is not None and (
        not isinstance(client_id, str) or not client_id or len(client_id) > 256
    ):
        raise ConfigurationError("Claude OAuth clientId 非法。")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scopes": scopes,
        "expires_at": expires_at,
        "refresh_token_expires_at": refresh_expires_at,
        "client_id": client_id,
        "subscription_type": oauth.get("subscriptionType"),
        "rate_limit_tier": oauth.get("rateLimitTier"),
    }


def _decode_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _is_memory_backed(path: Path) -> bool:
    """只接受 Linux mountinfo 明确标记为 tmpfs／ramfs 的最长匹配挂载点。"""

    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.is_file():
        return False
    resolved = path.resolve()
    best: tuple[int, str] | None = None
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        fields = line.split()
        if "-" not in fields or len(fields) < 7:
            continue
        separator = fields.index("-")
        if separator + 1 >= len(fields):
            continue
        mountpoint = Path(_decode_mount_field(fields[4])).resolve()
        try:
            matched = resolved == mountpoint or resolved.is_relative_to(mountpoint)
        except ValueError:
            matched = False
        if matched and (best is None or len(str(mountpoint)) > best[0]):
            best = (len(str(mountpoint)), fields[separator + 1])
    return bool(best and best[1] in {"tmpfs", "ramfs"})


def _validate_binary(path: Path, expected_version: str, expected_sha256: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigurationError("--claude-bin 必须是可信绝对普通文件。")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022 or not os.access(path, os.X_OK):
        raise ConfigurationError("Claude 二进制所有者、权限或可执行位不安全。")
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        raise ConfigurationError("Claude 二进制 SHA-256 与冻结身份不一致。")
    completed = subprocess.run(
        [str(path), "--version"],
        env=clean_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        start_new_session=True,
    )
    version_output = (completed.stdout or completed.stderr).strip()
    match = re.fullmatch(r"(?P<version>\d+\.\d+\.\d+)(?: \(Claude Code\))?", version_output)
    if completed.returncode != 0 or not match or match.group("version") != expected_version:
        raise ConfigurationError("Claude 二进制版本与冻结身份不一致。")
    return {
        "version": version_output,
        "sha256": actual_sha256,
        "bytes": metadata.st_size,
    }


def _atomic_replace_credentials(
    path: Path,
    document: dict[str, Any],
    expected_identity: tuple[int, int, int],
) -> None:
    current = _validate_credentials_path(path)
    if (current.st_dev, current.st_ino, current.st_mtime_ns) != expected_identity:
        raise ClaudeOAuthRefreshError("刷新期间 credentials 被其他进程修改，拒绝覆盖。")
    content = _json_bytes(document)
    if len(content) > MAX_CREDENTIAL_BYTES:
        raise ClaudeOAuthRefreshError("官方刷新结果超出 credentials 大小上限。")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.refresh-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_once(path: Path, content: bytes) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise ConfigurationError("--receipt 必须是尚不存在的非符号链接绝对路径。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_failure_output(stdout: str, stderr: str, secrets: Sequence[str]) -> str:
    text = (stderr or stdout).strip()
    for secret in secrets:
        text = redact_known_secret(text, secret)
    text = " ".join(text.split())
    return text[-500:] if text else "官方客户端未返回错误文本"


def refresh_claude_credentials(arguments: argparse.Namespace) -> dict[str, Any]:
    """执行一次官方刷新并写入不可覆盖的安全收据。"""

    if not arguments.execute or not arguments.acknowledge_credential_rotation:
        raise ConfigurationError("真实刷新必须同时确认 --execute 和 --acknowledge-credential-rotation。")
    if not VERSION_RE.fullmatch(arguments.expected_version):
        raise ConfigurationError("--expected-version 必须是精确三段版本。")
    if not SHA256_RE.fullmatch(arguments.expected_sha256):
        raise ConfigurationError("--expected-sha256 格式非法。")
    if not arguments.scan_root.is_absolute() or arguments.scan_root.is_symlink() or not arguments.scan_root.is_dir():
        raise ConfigurationError("--scan-root 必须是已有的非符号链接绝对目录。")
    if not arguments.receipt.is_absolute() or not arguments.receipt.resolve(strict=False).is_relative_to(
        arguments.scan_root.resolve()
    ):
        raise ConfigurationError("--receipt 必须位于 --scan-root 内。")
    if arguments.receipt.exists() or arguments.receipt.is_symlink():
        raise ConfigurationError("--receipt 已存在或为符号链接，拒绝覆盖。")
    if not arguments.memory_root.is_absolute() or arguments.memory_root.is_symlink() or not arguments.memory_root.is_dir():
        raise ConfigurationError("--memory-root 必须是已有的非符号链接绝对目录。")
    if not _is_memory_backed(arguments.memory_root):
        raise ConfigurationError("--memory-root 必须由 tmpfs 或 ramfs 承载。")

    credential_metadata = _validate_credentials_path(arguments.credentials_file)
    credential_identity = (
        credential_metadata.st_dev,
        credential_metadata.st_ino,
        credential_metadata.st_mtime_ns,
    )
    old_document = load_claude_credentials_document(arguments.credentials_file)
    old = _oauth_metadata(old_document, require_fresh=False)
    binary = _validate_binary(
        arguments.claude_bin, arguments.expected_version, arguments.expected_sha256
    )
    source = capture_source_bundle_identity(Path(__file__).resolve().parent)
    old_secrets = {
        "old_access_token": old["access_token"],
        "old_refresh_token": old["refresh_token"],
    }
    before_scan = scan_for_secrets(arguments.scan_root, old_secrets)
    if before_scan.get("passed") is not True:
        raise ClaudeOAuthRefreshError("刷新前证据根已含 OAuth 秘密或存在扫描错误。")

    scratch = Path(tempfile.mkdtemp(prefix="claude-oauth-refresh-", dir=arguments.memory_root))
    scratch.chmod(0o700)
    config_root = scratch / ".claude"
    config_root.mkdir(mode=0o700)
    command = [str(arguments.claude_bin), "auth", "login"]
    environment = clean_environment()
    environment.update(PRIVACY_ENV)
    environment.update(
        {
            "HOME": str(scratch),
            "CLAUDE_CONFIG_DIR": str(config_root),
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": old["refresh_token"],
            "CLAUDE_CODE_OAUTH_SCOPES": " ".join(old["scopes"]),
        }
    )
    if old["client_id"]:
        environment["CLAUDE_CODE_OAUTH_CLIENT_ID"] = str(old["client_id"])

    completed: subprocess.CompletedProcess[str] | None = None
    new_document: dict[str, Any] | None = None
    new: dict[str, Any] | None = None
    try:
        completed = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            start_new_session=True,
        )
        if completed.returncode != 0:
            detail = _safe_failure_output(
                completed.stdout,
                completed.stderr,
                [old["access_token"], old["refresh_token"]],
            )
            raise ClaudeOAuthRefreshError(
                f"官方 Claude Code 刷新失败，退出码 {completed.returncode}：{detail}"
            )
        refreshed_path = config_root / ".credentials.json"
        new_document = load_claude_credentials_document(refreshed_path)
        new = _oauth_metadata(new_document, require_fresh=True)
        if new["access_token"] == old["access_token"]:
            raise ClaudeOAuthRefreshError("官方刷新成功但 access token 未变化。")
        if new["access_token"] in {old["refresh_token"], new["refresh_token"]}:
            raise ClaudeOAuthRefreshError("官方刷新结果中的 access/refresh token 相互冲突。")
        if not set(old["scopes"]).issubset(new["scopes"]):
            raise ClaudeOAuthRefreshError("官方刷新结果丢失既有 OAuth scope。")
        if old["client_id"] and new["client_id"] != old["client_id"]:
            raise ClaudeOAuthRefreshError("官方刷新结果改变了自定义 OAuth clientId。")
    finally:
        shutil.rmtree(scratch, ignore_errors=False)
    if scratch.exists():
        raise ClaudeOAuthRefreshError("tmpfs 中的官方临时状态未清理。")
    if completed is None or new_document is None or new is None:
        raise ClaudeOAuthRefreshError("官方刷新未产生可验证凭据。")

    all_secrets = {
        **old_secrets,
        "new_access_token": new["access_token"],
        "new_refresh_token": new["refresh_token"],
    }
    final_scan = scan_for_secrets(arguments.scan_root, all_secrets)
    if final_scan.get("passed") is not True:
        raise ClaudeOAuthRefreshError("刷新后证据根含 OAuth 秘密或存在扫描错误。")

    receipt = {
        "schema_version": SCHEMA_RECEIPT,
        "status": "complete",
        "started_from_expired_access_token": bool(
            old["expires_at"] and old["expires_at"] <= int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        ),
        "completed_at_utc": _utc_now(),
        "driver": {
            "kind": "official_claude_code_auth_login_refresh_env",
            "client": binary,
            "invocation": argv_manifest_view(command),
            "refresh_token_transport": "child_environment_only",
            "temporary_state": "tmpfs_only",
            "temporary_state_removed": True,
            "requested_scopes": old["scopes"],
            "custom_client_id": old["client_id"] is not None,
        },
        "credential_update": {
            "path": str(arguments.credentials_file),
            "atomic_replace": True,
            "access_token_rotated": new["access_token"] != old["access_token"],
            "refresh_token_rotated": new["refresh_token"] != old["refresh_token"],
            "expires_at_before": old["expires_at"],
            "expires_at_after": new["expires_at"],
            "refresh_token_expires_at_before": old["refresh_token_expires_at"],
            "refresh_token_expires_at_after": new["refresh_token_expires_at"],
            "scopes_before": old["scopes"],
            "scopes_after": new["scopes"],
            "subscription_type_preserved": new["subscription_type"] == old["subscription_type"],
            "rate_limit_tier_preserved": new["rate_limit_tier"] == old["rate_limit_tier"],
        },
        "secret_scan": final_scan,
        "capture_execution_sources": source,
        "production_route_or_sink_changed": False,
        "evidence_approval_issued": False,
    }
    receipt_bytes = _json_bytes(receipt)
    for secret in all_secrets.values():
        if secret.encode("utf-8") in receipt_bytes:
            raise ClaudeOAuthRefreshError("OAuth 刷新收据序列化结果含秘密。")

    _atomic_replace_credentials(arguments.credentials_file, new_document, credential_identity)
    _write_once(arguments.receipt, receipt_bytes)
    return receipt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-bin", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--scan-root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--memory-root", type=Path, default=Path("/dev/shm"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-credential-rotation", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args(argv)
    if not arguments.execute:
        plan = {
            "schema_version": "claude-code-oauth-managed-refresh-plan/v1",
            "official_client_version": arguments.expected_version,
            "credentials_file": str(arguments.credentials_file),
            "scan_root": str(arguments.scan_root),
            "receipt": str(arguments.receipt),
            "temporary_state": "tmpfs_only",
            "live_oauth_request": True,
            "credential_rotation": True,
            "production_route_or_sink_changes": False,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    try:
        receipt = refresh_claude_credentials(arguments)
    except (ClaudeOAuthRefreshError, ConfigurationError, OSError, subprocess.SubprocessError) as error:
        print(f"Claude OAuth 受管刷新拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
