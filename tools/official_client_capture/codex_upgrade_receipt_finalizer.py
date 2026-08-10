#!/usr/bin/env python3
"""从封闭的结构化运行事实生成不可覆盖的 Codex 升级收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    # 允许从仓库根目录直接执行本文件。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture import scenario_receipts
from tools.official_client_capture.candidate_evidence_guard import (
    EvidenceGuardError,
    normalize_state,
    verify_restoration,
)


RESTORATION_SCHEMA = "codex-egress-restoration-report/v2"
OBSERVED_PROFILE_SCHEMA = "codex-egress-observed-profile/v2"
CLIENT_BINDING_SCHEMA = "codex-egress-client-binding/v2"
SCENARIO_RECEIPT_SCHEMA = "codex-egress-scenario-receipt/v1"
PRODUCER_SCHEMA = "codex-egress-receipt-producer/v1"
RUNTIME_AUDIT_SCHEMA = "codex-egress-runtime-audit/v1"
KILO_INGRESS_SCHEMA = "kilo-ingress-witness/v1"
KILO_RESPONSE_SCHEMA = "kilo-response-witness/v1"
USAGE_AUDIT_SCHEMA = "sub2api-usage-audit/v1"
KILO_INSTALLATION_SCHEMA = "kilo-installation/v1"
CLIENT_REQUEST_PROOF_SCHEMA = "codex-egress-client-request-evidence/v1"
CLIENT_RESPONSE_PROOF_SCHEMA = "codex-egress-client-response-evidence/v1"
STATE_SCHEMA = "codex-candidate-normalized-state/v1"

MAX_JSON_BYTES = 16 * 1024 * 1024
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
DATABASE_PROTECTED_TABLES: dict[str, tuple[str, ...]] = {
    "account_groups": ("account_id", "group_id"),
    "accounts": ("id",),
    "api_keys": ("id",),
    "groups": ("id",),
    "proxies": ("id",),
    "settings": ("id",),
    "user_subscriptions": ("id",),
    "users": ("id",),
}
DATABASE_WATERMARK_TABLES = frozenset(
    {"ops_error_logs", "ops_system_logs", "usage_logs"}
)
DATABASE_COMPARISON_POLICY = {
    "protected_table_rule": "before_primary_key_fingerprints_subset",
    "watermark_rule": "row_count_and_max_id_non_decreasing",
}
RECEIPT_SUBCOMMAND_BY_SCHEMA = {
    RESTORATION_SCHEMA: "restoration",
    OBSERVED_PROFILE_SCHEMA: "observed-profile",
    CLIENT_BINDING_SCHEMA: "kilo-binding",
    SCENARIO_RECEIPT_SCHEMA: "scenario",
}
REPLAY_INPUT_NAMES = {
    "restoration": (
        "service_before",
        "service_after",
        "container_before",
        "container_after",
        "database_before",
        "database_after",
        "account_before",
        "account_after",
        "configuration_before",
        "configuration_after",
    ),
    "observed-profile": ("runtime_audit",),
    "kilo-binding": (
        "installation",
        "ingress",
        "response_witness",
        "runtime_audit",
        "usage_audit",
    ),
    "scenario": ("facts",),
}
REPLAY_CANONICAL_FIELDS = {
    "restoration": {
        "evidence_root",
        "output",
        "phase",
        "candidate_id",
    },
    "observed-profile": {
        "evidence_root",
        "output",
        "campaign_id",
        "attempt_id",
        "run_nonce",
        "attempt_started_at_utc",
        "client_checkpoint_at_utc",
        "candidate_id",
        "target_version",
        "profile_id",
        "profile_digest",
        "image_id",
        "image_reference",
        "source_tree_sha256",
        "build_id",
        "deployed_version",
    },
    "kilo-binding": {
        "evidence_root",
        "output",
        "campaign_id",
        "attempt_id",
        "run_nonce",
        "attempt_started_at_utc",
        "client_checkpoint_at_utc",
        "client_id",
        "candidate_id",
        "target_version",
        "profile_id",
        "profile_digest",
        "candidate_image_id",
        "source_tree_sha256",
        "build_id",
        "deployed_version",
        "model",
    },
    "scenario": {
        "evidence_root",
        "output",
        "scenario_id",
        "job_id",
        "campaign_id",
        "attempt_id",
        "run_nonce",
        "run_id",
    },
}
ALLOWED_BRANCHES = frozenset(
    {"http", "websocket", "lite", "models", "cache", "prewarm", "fallback"}
)
CLIENT_CONTRACTS = {
    "kilo-compatible": {
        "protocol": "openai-compatible",
        "entrypoint": "/v1/chat/completions",
    },
    "kilo-responses": {
        "protocol": "openai-responses",
        "entrypoint": "/v1/responses",
    },
}
RESTORATION_INPUTS = (
    ("service_state_restored", "service_before", "service_after", "byte_equal"),
    (
        "container_state_restored",
        "container_before",
        "container_after",
        "byte_equal",
    ),
    (
        "database_state_preserved",
        "database_before",
        "database_after",
        "before_subset",
    ),
    ("account_state_preserved", "account_before", "account_after", "byte_equal"),
    (
        "configuration_state_restored",
        "configuration_before",
        "configuration_after",
        "byte_equal",
    ),
)


class ReceiptFinalizerError(ValueError):
    """输入事实不能生成可信收据。"""


@dataclass(frozen=True)
class RootContext:
    """证据根目录的调用路径与真实路径。"""

    alias: Path
    resolved: Path


@dataclass(frozen=True)
class BoundDocument:
    """一次安全读取后绑定的 JSON 文件。"""

    path: Path
    relative: str
    sha256: str
    size: int
    inode: tuple[int, int]
    content: bytes
    payload: dict[str, Any]

    def binding(self, *, name: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.relative,
            "sha256": self.sha256,
            "bytes": self.size,
        }
        if name is not None:
            result = {"name": name, **result}
        return result


@dataclass(frozen=True)
class ProtectedTableState:
    """单张保护表的存在性和不可逆主键集合。"""

    exists: bool
    primary_key_columns: tuple[str, ...]
    primary_key_fingerprints: frozenset[str]
    row_count: int | None


@dataclass(frozen=True)
class WatermarkTableState:
    """只追加日志表的行数与最大 ID 水位。"""

    exists: bool
    max_id: int | None
    row_count: int | None


@dataclass(frozen=True)
class DatabaseState:
    """数据库恢复比较所需的保护表集合与只追加水位。"""

    protected_tables: Mapping[str, ProtectedTableState]
    watermarks: Mapping[str, WatermarkTableState]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复键，避免同一字节串产生两种字段解释。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptFinalizerError(f"JSON 对象包含重复字段：{key}")
        result[key] = value
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    """生成稳定的紧凑 JSON 字节。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_root(raw_root: Path) -> RootContext:
    """固定一个私有、非符号链接的证据根目录。"""

    alias = Path(os.path.abspath(raw_root))
    if alias.is_symlink() or not alias.is_dir():
        raise ReceiptFinalizerError(f"证据根目录必须是非符号链接目录：{raw_root}")
    metadata = alias.stat()
    if metadata.st_uid != os.geteuid():
        raise ReceiptFinalizerError("证据根目录必须归当前用户所有")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ReceiptFinalizerError("证据根目录权限必须禁止组用户和其他用户访问")
    return RootContext(alias=alias, resolved=alias.resolve(strict=True))


def _relative_candidate(
    root: RootContext,
    raw_path: Path,
    *,
    must_exist: bool,
    label: str,
) -> tuple[Path, str]:
    """把输入限制在证据根内，并拒绝根内任一符号链接组件。"""

    raw_text = os.fspath(raw_path)
    if "\x00" in raw_text or "\\" in raw_text:
        raise ReceiptFinalizerError(f"{label}路径包含非法字符")
    lexical_parts = raw_text.split("/")
    if (
        any(part in {".", ".."} for part in lexical_parts)
        or any(not part for part in lexical_parts[1:-1])
    ):
        raise ReceiptFinalizerError(f"{label}路径必须使用规范化组件")
    candidate = raw_path if raw_path.is_absolute() else root.alias / raw_path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root.alias)
        walk_root = root.alias
    except ValueError:
        try:
            resolved_candidate = candidate.resolve(strict=must_exist)
            relative = resolved_candidate.relative_to(root.resolved)
            walk_root = root.resolved
        except (OSError, RuntimeError, ValueError) as error:
            raise ReceiptFinalizerError(f"{label}必须位于证据根目录内") from error
    if (
        not relative.parts
        or ".." in relative.parts
        or any(part in {"", "."} for part in relative.parts)
    ):
        raise ReceiptFinalizerError(f"{label}路径不能指向根目录或发生逃逸")
    current = walk_root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ReceiptFinalizerError(f"{label}路径包含符号链接：{current}")
        if index < len(relative.parts) - 1 and not current.is_dir():
            raise ReceiptFinalizerError(f"{label}父目录不存在或不是目录：{current}")
    if must_exist:
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(root.resolved)
        except (OSError, RuntimeError, ValueError) as error:
            raise ReceiptFinalizerError(f"{label}不存在或越过证据根目录") from error
    return current, PurePosixPath(*relative.parts).as_posix()


def _read_fd(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(1024 * 1024, MAX_JSON_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_JSON_BYTES:
            raise ReceiptFinalizerError("JSON 输入超过大小上限")
    content = b"".join(chunks)
    if len(content) != expected_size:
        raise ReceiptFinalizerError("JSON 输入在读取期间发生变化")
    return content


def _load_document(
    root: RootContext,
    raw_path: Path,
    label: str,
) -> BoundDocument:
    """以 no-follow 文件描述符读取私有 JSON，并绑定同一批字节的哈希。"""

    path, relative = _relative_candidate(
        root, raw_path, must_exist=True, label=label
    )
    if path.suffix.lower() != ".json":
        raise ReceiptFinalizerError(f"{label}必须使用 .json 文件")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReceiptFinalizerError(f"无法安全打开{label}：{error}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_size <= 0
            or before.st_size > MAX_JSON_BYTES
        ):
            raise ReceiptFinalizerError(f"{label}必须是当前用户拥有的非空普通文件")
        if stat.S_IMODE(before.st_mode) & 0o077:
            raise ReceiptFinalizerError(f"{label}权限必须是私有的")
        content = _read_fd(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ReceiptFinalizerError(f"{label}在读取期间发生变化")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReceiptFinalizerError(f"{label}不是合法 UTF-8 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise ReceiptFinalizerError(f"{label}顶层必须是对象")
    return BoundDocument(
        path=path,
        relative=relative,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        inode=(before.st_dev, before.st_ino),
        content=content,
        payload=payload,
    )


def _require_unique_documents(documents: Iterable[BoundDocument]) -> None:
    """禁止一份事实或硬链接同时冒充多个独立观测来源。"""

    paths: set[str] = set()
    inodes: set[tuple[int, int]] = set()
    for document in documents:
        if document.relative in paths or document.inode in inodes:
            raise ReceiptFinalizerError("不同输入角色必须绑定不同普通文件和 inode")
        paths.add(document.relative)
        inodes.add(document.inode)


def _expect_exact(
    payload: Mapping[str, Any],
    fields: set[str],
    label: str,
) -> None:
    observed = set(payload)
    if observed != fields:
        missing = sorted(fields - observed)
        extra = sorted(observed - fields)
        raise ReceiptFinalizerError(
            f"{label}字段不闭合；缺失={missing}，多余={extra}"
        )


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是安全标识")
    return value


def _nonempty_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ReceiptFinalizerError(f"{label}必须是无控制字符的非空文本")
    return value


def _version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是三段式版本号")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是小写 SHA-256")
    return value


def _run_nonce(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RUN_NONCE_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}必须是 64 位小写十六进制")
    return value


def _image_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IMAGE_ID_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是镜像 ID")
    return value


def _image_reference(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IMAGE_REFERENCE_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是 OCI 仓库不可变引用")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReceiptFinalizerError(f"{label}必须是正整数")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReceiptFinalizerError(f"{label}必须是非负整数")
    return value


def _http_status(value: Any, label: str) -> int:
    """入站响应必须是成功状态：2xx，或 WebSocket 升级成功的 101。

    Kilo 的 Responses 入口走 WebSocket（`usage_logs.openai_ws_mode` 为真），服务端
    据此记录 `101 Switching Protocols`——那是该链路成功的唯一正确状态码，与 2xx 同属
    成功语义。原判据只认 2xx，会把 Kilo 的真实行为判成失败；验收要覆盖客户端的真实
    形态，而不是反过来要求客户端改用 HTTP 迁就判据。101 之外的 1xx 仍然拒绝。
    """

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (value != 101 and not 200 <= value < 300)
    ):
        raise ReceiptFinalizerError(f"{label}必须是 2xx 整数或 WebSocket 升级的 101")
    return value


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ReceiptFinalizerError(f"{label}不是带时区的 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReceiptFinalizerError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise ReceiptFinalizerError(f"{label}缺少时区")
    return parsed


def _attempt_binding_from_args(args: argparse.Namespace) -> dict[str, str]:
    """读取一次运行不可跨 attempt 重放的三元关联。"""

    return {
        "campaign_id": _safe_id(args.campaign_id, "campaign_id"),
        "attempt_id": _safe_id(args.attempt_id, "attempt_id"),
        "run_nonce": _run_nonce(args.run_nonce, "run_nonce"),
    }


def _time_window_from_args(
    args: argparse.Namespace,
) -> tuple[dict[str, str], datetime, datetime]:
    """读取并验证 attempt 到客户端检查点的闭合时间窗。"""

    values = {
        "attempt_started_at_utc": args.attempt_started_at_utc,
        "client_checkpoint_at_utc": args.client_checkpoint_at_utc,
    }
    started_at = _rfc3339(
        values["attempt_started_at_utc"],
        "attempt_started_at_utc",
    )
    checkpoint_at = _rfc3339(
        values["client_checkpoint_at_utc"],
        "client_checkpoint_at_utc",
    )
    if started_at > checkpoint_at:
        raise ReceiptFinalizerError(
            "attempt_started_at_utc 不能晚于 client_checkpoint_at_utc"
        )
    return values, started_at, checkpoint_at


def _require_in_time_window(
    observed_at: datetime,
    label: str,
    started_at: datetime,
    checkpoint_at: datetime,
) -> None:
    """要求请求相关事实位于当前 attempt 的闭合时间窗内。"""

    if observed_at < started_at or observed_at > checkpoint_at:
        raise ReceiptFinalizerError(
            f"{label}不在当前 attempt 的客户端证据时间窗内"
        )


def _absolute_posix_path(value: Any, label: str) -> str:
    value = _nonempty_text(value, label, maximum=4096)
    if "\\" in value or "//" in value:
        raise ReceiptFinalizerError(f"{label}不是规范化 POSIX 绝对路径")
    path = PurePosixPath(value)
    if (
        path == PurePosixPath("/")
        or not path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or str(path) != value
    ):
        raise ReceiptFinalizerError(f"{label}不是规范化 POSIX 绝对路径")
    return value


def _endpoint(value: Any, label: str) -> str:
    value = _nonempty_text(value, label, maximum=2048)
    if (
        not value.startswith("/")
        or "\\" in value
        or any(part == ".." for part in value.split("/"))
    ):
        raise ReceiptFinalizerError(f"{label}不是安全的上游相对端点")
    return value


def _branches(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(item, str) or item not in ALLOWED_BRANCHES
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ReceiptFinalizerError(f"{label}包含非法、空或重复分支")
    return list(value)


def _validate_guard_snapshot(document: BoundDocument, label: str) -> dict[str, Any]:
    _expect_exact(document.payload, {"schema_version", "state"}, label)
    if document.payload.get("schema_version") != STATE_SCHEMA:
        raise ReceiptFinalizerError(f"{label}不是 guard 规范化快照")
    try:
        expected = normalize_state(document.payload.get("state"))
    except EvidenceGuardError as error:
        raise ReceiptFinalizerError(f"{label}状态非法：{error}") from error
    if document.content != expected:
        raise ReceiptFinalizerError(f"{label}不是 guard 的稳定紧凑字节格式")
    state = document.payload.get("state")
    if not isinstance(state, dict):
        raise ReceiptFinalizerError(f"{label} state 必须是对象")
    return state


def _database_state(
    document: BoundDocument,
    label: str,
) -> DatabaseState:
    """严格读取探针生成的保护表主键指纹和只追加水位。"""

    state = _validate_guard_snapshot(document, label)
    _expect_exact(
        state,
        {
            "append_only_watermarks",
            "comparison_policy",
            "probe_kind",
            "protected_tables",
        },
        f"{label}.state",
    )
    if state.get("probe_kind") != "database":
        raise ReceiptFinalizerError(f"{label}.state.probe_kind 必须是 database")
    if state.get("comparison_policy") != DATABASE_COMPARISON_POLICY:
        raise ReceiptFinalizerError(f"{label}.state.comparison_policy 不匹配")

    tables = state.get("protected_tables")
    if not isinstance(tables, list):
        raise ReceiptFinalizerError(f"{label}.protected_tables 必须是数组")
    protected: dict[str, ProtectedTableState] = {}
    observed_names: list[str] = []
    for index, table in enumerate(tables):
        if not isinstance(table, dict):
            raise ReceiptFinalizerError(
                f"{label}.protected_tables[{index}] 必须是对象"
            )
        _expect_exact(
            table,
            {
                "exists",
                "name",
                "primary_key_columns",
                "primary_key_fingerprints",
                "row_count",
            },
            f"{label}.protected_tables[{index}]",
        )
        name = table.get("name")
        if not isinstance(name, str) or not TABLE_NAME_RE.fullmatch(name):
            raise ReceiptFinalizerError(
                f"{label}.protected_tables[{index}].name 非法"
            )
        if name not in DATABASE_PROTECTED_TABLES:
            raise ReceiptFinalizerError(f"{label}包含未批准保护表：{name}")
        if name in protected:
            raise ReceiptFinalizerError(f"{label}包含重复保护表：{name}")
        exists = table.get("exists")
        if not isinstance(exists, bool):
            raise ReceiptFinalizerError(f"{label}.{name}.exists 必须是布尔值")
        columns = table.get("primary_key_columns")
        expected_columns = list(DATABASE_PROTECTED_TABLES[name])
        if columns != expected_columns:
            raise ReceiptFinalizerError(f"{label}.{name} 主键列不匹配")
        fingerprints = table.get("primary_key_fingerprints")
        if (
            not isinstance(fingerprints, list)
            or any(not isinstance(value, str) or not SHA256_RE.fullmatch(value)
                   for value in fingerprints)
            or fingerprints != sorted(fingerprints)
            or len(fingerprints) != len(set(fingerprints))
        ):
            raise ReceiptFinalizerError(
                f"{label}.{name}.primary_key_fingerprints "
                "必须是排序、唯一的小写 SHA-256 数组"
            )
        row_count = table.get("row_count")
        if exists:
            row_count = _nonnegative_integer(row_count, f"{label}.{name}.row_count")
            if row_count != len(fingerprints):
                raise ReceiptFinalizerError(
                    f"{label}.{name} 行数与主键指纹数量不一致"
                )
        elif row_count is not None or fingerprints:
            raise ReceiptFinalizerError(
                f"{label}.{name} 不存在时不得声明行数或主键指纹"
            )
        observed_names.append(name)
        protected[name] = ProtectedTableState(
            exists=exists,
            primary_key_columns=tuple(expected_columns),
            primary_key_fingerprints=frozenset(fingerprints),
            row_count=row_count,
        )
    if observed_names != sorted(observed_names):
        raise ReceiptFinalizerError(f"{label}.protected_tables 必须按 name 排序")
    if set(protected) != set(DATABASE_PROTECTED_TABLES):
        missing = sorted(set(DATABASE_PROTECTED_TABLES) - set(protected))
        raise ReceiptFinalizerError(f"{label}缺少保护表：{missing}")

    watermark_values = state.get("append_only_watermarks")
    if not isinstance(watermark_values, list):
        raise ReceiptFinalizerError(f"{label}.append_only_watermarks 必须是数组")
    watermarks: dict[str, WatermarkTableState] = {}
    observed_watermark_names: list[str] = []
    for index, watermark in enumerate(watermark_values):
        if not isinstance(watermark, dict):
            raise ReceiptFinalizerError(
                f"{label}.append_only_watermarks[{index}] 必须是对象"
            )
        _expect_exact(
            watermark,
            {"exists", "max_id", "name", "row_count"},
            f"{label}.append_only_watermarks[{index}]",
        )
        name = watermark.get("name")
        if not isinstance(name, str) or name not in DATABASE_WATERMARK_TABLES:
            raise ReceiptFinalizerError(f"{label}包含未批准水位表")
        if name in watermarks:
            raise ReceiptFinalizerError(f"{label}包含重复水位表：{name}")
        exists = watermark.get("exists")
        if not isinstance(exists, bool):
            raise ReceiptFinalizerError(f"{label}.{name}.exists 必须是布尔值")
        row_count = watermark.get("row_count")
        max_id = watermark.get("max_id")
        if exists:
            row_count = _nonnegative_integer(
                row_count,
                f"{label}.{name}.row_count",
            )
            if row_count == 0:
                if max_id is not None:
                    raise ReceiptFinalizerError(
                        f"{label}.{name} 空表的 max_id 必须为 null"
                    )
            else:
                max_id = _positive_integer(max_id, f"{label}.{name}.max_id")
        elif row_count is not None or max_id is not None:
            raise ReceiptFinalizerError(
                f"{label}.{name} 不存在时水位必须为 null"
            )
        observed_watermark_names.append(name)
        watermarks[name] = WatermarkTableState(
            exists=exists,
            max_id=max_id,
            row_count=row_count,
        )
    if observed_watermark_names != sorted(observed_watermark_names):
        raise ReceiptFinalizerError(
            f"{label}.append_only_watermarks 必须按 name 排序"
        )
    if set(watermarks) != DATABASE_WATERMARK_TABLES:
        missing = sorted(DATABASE_WATERMARK_TABLES - set(watermarks))
        raise ReceiptFinalizerError(f"{label}缺少水位表：{missing}")
    return DatabaseState(protected_tables=protected, watermarks=watermarks)


def _producer(
    subcommand: str,
    root: RootContext,
    output_relative: str,
    arguments: Mapping[str, Any],
    documents: Mapping[str, BoundDocument],
) -> dict[str, Any]:
    """把工具本体、所有标量参数和输入字节绑定进可重放命令摘要。"""

    tool_path = Path(__file__).resolve(strict=True)
    if tool_path.is_symlink() or not tool_path.is_file():
        raise ReceiptFinalizerError("finalizer 工具路径不可信")
    input_bindings = [
        document.binding(name=name)
        for name, document in sorted(documents.items())
    ]
    core = {
        "schema_version": PRODUCER_SCHEMA,
        "tool": {
            "path": str(tool_path),
            "sha256": _file_sha256(tool_path),
        },
        "subcommand": subcommand,
        "canonical_arguments": {
            "evidence_root": str(root.resolved),
            "output": output_relative,
            **dict(arguments),
        },
        "input_bindings": input_bindings,
    }
    return {**core, "command_sha256": _fingerprint(core)}


def _output_location(
    root: RootContext,
    raw_path: Path,
) -> tuple[Path, str]:
    path, relative = _relative_candidate(
        root, raw_path, must_exist=False, label="输出文件"
    )
    if path.suffix.lower() != ".json":
        raise ReceiptFinalizerError("输出文件必须使用 .json 后缀")
    if path.exists() or path.is_symlink():
        raise ReceiptFinalizerError(f"输出文件已存在，拒绝覆盖：{path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ReceiptFinalizerError("输出父目录必须是已存在的非符号链接目录")
    return path, relative


def _existing_output_location(
    root: RootContext,
    raw_path: Path,
) -> tuple[Path, str]:
    """重放时只解析既有收据位置，绝不创建或覆盖该文件。"""

    path, relative = _relative_candidate(
        root,
        raw_path,
        must_exist=True,
        label="既有收据",
    )
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ReceiptFinalizerError("既有收据必须是 .json 普通文件")
    return path, relative


def _write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    """原子且独占地创建 0600 JSON，永不覆盖既有收据。"""

    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    linked = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            linked = True
        except FileExistsError as error:
            raise ReceiptFinalizerError(
                f"输出文件已存在，拒绝覆盖：{path}"
            ) from error
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            path.unlink(missing_ok=True)
            raise ReceiptFinalizerError("输出收据不是 0600 普通文件")
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        if linked:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _reload_same(
    root: RootContext,
    document: BoundDocument,
    label: str,
) -> BoundDocument:
    reloaded = _load_document(root, Path(document.relative), label)
    if (
        reloaded.sha256 != document.sha256
        or reloaded.size != document.size
        or reloaded.inode != document.inode
    ):
        raise ReceiptFinalizerError(f"{label}在验证后发生变化")
    return reloaded


def finalize_restoration(
    args: argparse.Namespace,
    *,
    write_output: bool = True,
) -> dict[str, Any]:
    """从五组 guard 快照生成恢复收据。"""

    root = _validate_root(args.evidence_root)
    if write_output:
        output_path, output_relative = _output_location(root, args.output)
    else:
        output_path, output_relative = _existing_output_location(root, args.output)
    phase = args.phase
    candidate_id = args.candidate_id
    if phase == "official":
        if candidate_id is not None:
            raise ReceiptFinalizerError("official 恢复收据不得设置 candidate_id")
    else:
        candidate_id = _safe_id(candidate_id, "candidate_id")

    documents: dict[str, BoundDocument] = {}
    for _, before_name, after_name, _ in RESTORATION_INPUTS:
        documents[before_name] = _load_document(
            root, getattr(args, before_name), before_name
        )
        documents[after_name] = _load_document(
            root, getattr(args, after_name), after_name
        )
    _require_unique_documents(documents.values())

    checks: list[dict[str, Any]] = []
    for check_id, before_name, after_name, comparator in RESTORATION_INPUTS:
        before = documents[before_name]
        after = documents[after_name]
        if comparator == "byte_equal":
            try:
                result = verify_restoration(before.path, after.path)
            except EvidenceGuardError as error:
                raise ReceiptFinalizerError(
                    f"{check_id} 恢复验证失败：{error}"
                ) from error
            before = _reload_same(root, before, before_name)
            after = _reload_same(root, after, after_name)
            if (
                result.get("passed") is not True
                or result.get("different_inode") is not True
                or result.get("byte_identical") is not True
                or result.get("sha256") != before.sha256
                or before.sha256 != after.sha256
            ):
                raise ReceiptFinalizerError(f"{check_id} guard 结果与输入绑定不一致")
            expected = {"sha256": before.sha256, "bytes": before.size}
            actual = dict(expected)
        else:
            before_state = _database_state(before, before_name)
            after_state = _database_state(after, after_name)
            if before.inode == after.inode:
                raise ReceiptFinalizerError(
                    "database before 与 after 不能是同一文件或硬链接"
                )
            missing: dict[str, list[str]] = {}
            for table, before_table in before_state.protected_tables.items():
                after_table = after_state.protected_tables[table]
                if before_table.exists != after_table.exists:
                    raise ReceiptFinalizerError(
                        f"database_state_preserved 的 {table}.exists 发生变化"
                    )
                absent = (
                    before_table.primary_key_fingerprints
                    - after_table.primary_key_fingerprints
                )
                if absent:
                    missing[table] = sorted(absent)
            if missing:
                raise ReceiptFinalizerError(
                    f"database_state_preserved 缺少前置主键指纹：{missing}"
                )

            watermark_summary: dict[str, dict[str, int | None]] = {}
            for table, before_watermark in before_state.watermarks.items():
                after_watermark = after_state.watermarks[table]
                if before_watermark.exists != after_watermark.exists:
                    raise ReceiptFinalizerError(
                        f"database_state_preserved 的 {table}.exists 发生变化"
                    )
                if before_watermark.exists:
                    if (
                        after_watermark.row_count is None
                        or before_watermark.row_count is None
                        or after_watermark.row_count < before_watermark.row_count
                    ):
                        raise ReceiptFinalizerError(
                            f"database_state_preserved 的 {table} 行数水位下降"
                        )
                    before_max_id = before_watermark.max_id or 0
                    after_max_id = after_watermark.max_id or 0
                    if after_max_id < before_max_id:
                        raise ReceiptFinalizerError(
                            f"database_state_preserved 的 {table} 最大 ID 水位下降"
                        )
                    watermark_summary[table] = {
                        "before_max_id": before_watermark.max_id,
                        "before_row_count": before_watermark.row_count,
                        "after_max_id": after_watermark.max_id,
                        "after_row_count": after_watermark.row_count,
                    }
            before = _reload_same(root, before, before_name)
            after = _reload_same(root, after, after_name)
            before_count = sum(
                len(table.primary_key_fingerprints)
                for table in before_state.protected_tables.values()
            )
            after_count = sum(
                len(table.primary_key_fingerprints)
                for table in after_state.protected_tables.values()
            )
            verification = {
                "before_snapshot_sha256": before.sha256,
                "after_snapshot_sha256": after.sha256,
                "required_primary_key_count": before_count,
                "after_primary_key_count": after_count,
                "verified_primary_key_count": before_count,
                "missing_primary_key_count": 0,
                "primary_key_fingerprints_sha256": _fingerprint(
                    {
                        table: sorted(value.primary_key_fingerprints)
                        for table, value in sorted(
                            before_state.protected_tables.items()
                        )
                    }
                ),
                "watermarks": watermark_summary,
            }
            expected = verification
            actual = dict(verification)
        checks.append(
            {
                "id": check_id,
                "comparator": comparator,
                "passed": True,
                "expected": expected,
                "actual": actual,
                "evidence_refs": [
                    {"role": "before", **before.binding()},
                    {"role": "after", **after.binding()},
                ],
            }
        )

    producer = _producer(
        "restoration",
        root,
        output_relative,
        {"phase": phase, "candidate_id": candidate_id},
        documents,
    )
    receipt = {
        "schema_version": RESTORATION_SCHEMA,
        "phase": phase,
        "candidate_id": candidate_id,
        "status": "restored",
        "checks": checks,
        "producer": producer,
    }
    if write_output:
        _write_json_once(output_path, receipt)
    return receipt


def _observed_identity_from_args(args: argparse.Namespace) -> dict[str, str]:
    image_id = _image_id(args.image_id, "image_id")
    return {
        **_attempt_binding_from_args(args),
        "candidate_id": _safe_id(args.candidate_id, "candidate_id"),
        "target_version": _version(args.target_version, "target_version"),
        "profile_id": _safe_id(args.profile_id, "profile_id"),
        "profile_digest": _sha256(args.profile_digest, "profile_digest"),
        "image_id": image_id,
        "image_reference": _image_reference(args.image_reference, "image_reference"),
        "source_tree_sha256": _sha256(
            args.source_tree_sha256, "source_tree_sha256"
        ),
        "build_id": _safe_id(args.build_id, "build_id"),
        "deployed_version": _safe_id(args.deployed_version, "deployed_version"),
    }


def finalize_observed_profile(
    args: argparse.Namespace,
    *,
    write_output: bool = True,
) -> dict[str, Any]:
    """从运行审计事件与命令行身份绑定生成画像观测收据。"""

    root = _validate_root(args.evidence_root)
    if write_output:
        output_path, output_relative = _output_location(root, args.output)
    else:
        output_path, output_relative = _existing_output_location(root, args.output)
    identity = _observed_identity_from_args(args)
    time_window, started_at, checkpoint_at = _time_window_from_args(args)
    runtime = _load_document(root, args.runtime_audit, "runtime_audit")
    fields = {
        "schema_version",
        "source",
        "event_type",
        "event_id",
        "campaign_id",
        "attempt_id",
        "run_nonce",
        "candidate_id",
        "target_version",
        "profile_id",
        "profile_digest",
        "image_id",
        "image_reference",
        "source_tree_sha256",
        "build_id",
        "deployed_version",
        "observed_at_utc",
    }
    _expect_exact(runtime.payload, fields, "runtime_audit")
    expected = {
        "schema_version": RUNTIME_AUDIT_SCHEMA,
        "source": "sub2api-runtime",
        "event_type": "profile_activated",
        **identity,
    }
    for field, value in expected.items():
        if runtime.payload.get(field) != value:
            raise ReceiptFinalizerError(f"runtime_audit.{field} 与命令行绑定不一致")
    event_id = _safe_id(runtime.payload.get("event_id"), "runtime_audit.event_id")
    observed_at = _rfc3339(
        runtime.payload.get("observed_at_utc"),
        "runtime_audit.observed_at_utc",
    )
    _require_in_time_window(
        observed_at,
        "runtime_audit.observed_at_utc",
        started_at,
        checkpoint_at,
    )
    runtime = _reload_same(root, runtime, "runtime_audit")
    producer = _producer(
        "observed-profile",
        root,
        output_relative,
        {**identity, **time_window},
        {"runtime_audit": runtime},
    )
    receipt = {
        "schema_version": OBSERVED_PROFILE_SCHEMA,
        "status": "active",
        **identity,
        **time_window,
        "source": "sub2api-runtime",
        "event_id": event_id,
        "observed_at_utc": runtime.payload["observed_at_utc"],
        "runtime_event": runtime.binding(),
        "producer": producer,
    }
    if write_output:
        _write_json_once(output_path, receipt)
    return receipt


def _kilo_identity_from_args(args: argparse.Namespace) -> dict[str, str]:
    client_id = args.client_id
    if client_id not in CLIENT_CONTRACTS:
        raise ReceiptFinalizerError("client_id 必须是批准的 Kilo 协议入口")
    return {
        **_attempt_binding_from_args(args),
        "client_id": client_id,
        "candidate_id": _safe_id(args.candidate_id, "candidate_id"),
        "target_version": _version(args.target_version, "target_version"),
        "profile_id": _safe_id(args.profile_id, "profile_id"),
        "profile_digest": _sha256(args.profile_digest, "profile_digest"),
        "candidate_image_id": _image_id(
            args.candidate_image_id, "candidate_image_id"
        ),
        "source_tree_sha256": _sha256(
            args.source_tree_sha256, "source_tree_sha256"
        ),
        "build_id": _safe_id(args.build_id, "build_id"),
        "deployed_version": _safe_id(args.deployed_version, "deployed_version"),
        "model": _nonempty_text(args.model, "model"),
    }


def _validate_installation(document: BoundDocument) -> dict[str, Any]:
    payload = document.payload
    _expect_exact(
        payload,
        {
            "schema_version",
            "source",
            "installation_id",
            "product_id",
            "display_name",
            "client_version",
            "executable_path",
            "executable_sha256",
            "observed_at_utc",
        },
        "kilo_installation",
    )
    if payload.get("schema_version") != KILO_INSTALLATION_SCHEMA:
        raise ReceiptFinalizerError("kilo_installation.schema_version 不匹配")
    if payload.get("source") != "kilo-installation":
        raise ReceiptFinalizerError("kilo_installation.source 不匹配")
    if payload.get("product_id") != "kilo":
        raise ReceiptFinalizerError("安装事实不是 Kilo 客户端")
    _safe_id(payload.get("installation_id"), "kilo_installation.installation_id")
    _nonempty_text(payload.get("display_name"), "kilo_installation.display_name")
    _nonempty_text(
        payload.get("client_version"), "kilo_installation.client_version"
    )
    _absolute_posix_path(
        payload.get("executable_path"), "kilo_installation.executable_path"
    )
    _sha256(
        payload.get("executable_sha256"), "kilo_installation.executable_sha256"
    )
    _rfc3339(
        payload.get("observed_at_utc"), "kilo_installation.observed_at_utc"
    )
    return payload


def _validate_ingress(
    document: BoundDocument,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    payload = document.payload
    _expect_exact(
        payload,
        {
            "schema_version",
            "source",
            "witness_id",
            "request_id",
            "campaign_id",
            "attempt_id",
            "run_nonce",
            "installation_id",
            "client_id",
            "client_version",
            "protocol",
            "entrypoint",
            "model",
            "candidate_id",
            "target_version",
            "received_at_utc",
        },
        "kilo_ingress",
    )
    contract = CLIENT_CONTRACTS[identity["client_id"]]
    expected = {
        "schema_version": KILO_INGRESS_SCHEMA,
        "source": "kilo-ingress",
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "client_id": identity["client_id"],
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": identity["model"],
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ReceiptFinalizerError(f"kilo_ingress.{field} 绑定不一致")
    _safe_id(payload.get("witness_id"), "kilo_ingress.witness_id")
    _nonempty_text(payload.get("request_id"), "kilo_ingress.request_id")
    _safe_id(payload.get("installation_id"), "kilo_ingress.installation_id")
    _nonempty_text(payload.get("client_version"), "kilo_ingress.client_version")
    _rfc3339(payload.get("received_at_utc"), "kilo_ingress.received_at_utc")
    return payload


def _validate_request_runtime(
    document: BoundDocument,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    payload = document.payload
    _expect_exact(
        payload,
        {
            "schema_version",
            "source",
            "event_type",
            "event_id",
            "request_id",
            "campaign_id",
            "attempt_id",
            "run_nonce",
            "ingress_witness_id",
            "installation_id",
            "client_id",
            "protocol",
            "entrypoint",
            "model",
            "candidate_id",
            "target_version",
            "profile_id",
            "profile_digest",
            "image_id",
            "source_tree_sha256",
            "build_id",
            "deployed_version",
            "auth_mode",
            "oauth_account_id",
            "upstream_endpoint",
            "transport",
            "affected_branches",
            "observed_at_utc",
        },
        "runtime_audit",
    )
    contract = CLIENT_CONTRACTS[identity["client_id"]]
    expected = {
        "schema_version": RUNTIME_AUDIT_SCHEMA,
        "source": "sub2api-runtime",
        "event_type": "oauth_request_forwarded",
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "client_id": identity["client_id"],
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": identity["model"],
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "image_id": identity["candidate_image_id"],
        "source_tree_sha256": identity["source_tree_sha256"],
        "build_id": identity["build_id"],
        "deployed_version": identity["deployed_version"],
        "auth_mode": "oauth",
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ReceiptFinalizerError(f"runtime_audit.{field} 绑定不一致")
    _safe_id(payload.get("event_id"), "runtime_audit.event_id")
    _nonempty_text(payload.get("request_id"), "runtime_audit.request_id")
    _safe_id(payload.get("ingress_witness_id"), "runtime_audit.ingress_witness_id")
    _safe_id(payload.get("installation_id"), "runtime_audit.installation_id")
    _positive_integer(
        payload.get("oauth_account_id"), "runtime_audit.oauth_account_id"
    )
    _endpoint(payload.get("upstream_endpoint"), "runtime_audit.upstream_endpoint")
    transport = payload.get("transport")
    if transport not in {"http", "websocket"}:
        raise ReceiptFinalizerError("runtime_audit.transport 非法")
    branches = _branches(
        payload.get("affected_branches"), "runtime_audit.affected_branches"
    )
    if transport not in branches:
        raise ReceiptFinalizerError("runtime_audit 未覆盖实际传输分支")
    if identity["client_id"] == "kilo-compatible" and transport != "http":
        raise ReceiptFinalizerError("kilo-compatible 必须使用 HTTP 传输事实")
    _rfc3339(payload.get("observed_at_utc"), "runtime_audit.observed_at_utc")
    return payload


def _validate_response(
    document: BoundDocument,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    payload = document.payload
    _expect_exact(
        payload,
        {
            "schema_version",
            "source",
            "witness_id",
            "request_id",
            "campaign_id",
            "attempt_id",
            "run_nonce",
            "installation_id",
            "client_id",
            "candidate_id",
            "http_status",
            "response_id",
            "completed_at_utc",
        },
        "kilo_response",
    )
    if payload.get("schema_version") != KILO_RESPONSE_SCHEMA:
        raise ReceiptFinalizerError("kilo_response.schema_version 不匹配")
    if payload.get("source") != "kilo-response":
        raise ReceiptFinalizerError("kilo_response.source 不匹配")
    for field in ("campaign_id", "attempt_id", "run_nonce"):
        if payload.get(field) != identity[field]:
            raise ReceiptFinalizerError(f"kilo_response.{field} 绑定不一致")
    _safe_id(payload.get("witness_id"), "kilo_response.witness_id")
    _nonempty_text(payload.get("request_id"), "kilo_response.request_id")
    _safe_id(payload.get("installation_id"), "kilo_response.installation_id")
    _http_status(payload.get("http_status"), "kilo_response.http_status")
    _nonempty_text(payload.get("response_id"), "kilo_response.response_id")
    _rfc3339(payload.get("completed_at_utc"), "kilo_response.completed_at_utc")
    return payload


def _validate_usage(
    document: BoundDocument,
    identity: Mapping[str, str],
) -> dict[str, Any]:
    payload = document.payload
    _expect_exact(
        payload,
        {
            "schema_version",
            "source",
            "event_id",
            "request_id",
            "campaign_id",
            "attempt_id",
            "run_nonce",
            "response_id",
            "candidate_id",
            "usage_id",
            "oauth_account_id",
            "recorded_at_utc",
        },
        "usage_audit",
    )
    if payload.get("schema_version") != USAGE_AUDIT_SCHEMA:
        raise ReceiptFinalizerError("usage_audit.schema_version 不匹配")
    if payload.get("source") != "sub2api-usage":
        raise ReceiptFinalizerError("usage_audit.source 不匹配")
    for field in ("campaign_id", "attempt_id", "run_nonce"):
        if payload.get(field) != identity[field]:
            raise ReceiptFinalizerError(f"usage_audit.{field} 绑定不一致")
    _safe_id(payload.get("event_id"), "usage_audit.event_id")
    _nonempty_text(payload.get("request_id"), "usage_audit.request_id")
    _nonempty_text(payload.get("response_id"), "usage_audit.response_id")
    _nonempty_text(payload.get("usage_id"), "usage_audit.usage_id")
    _positive_integer(
        payload.get("oauth_account_id"), "usage_audit.oauth_account_id"
    )
    _rfc3339(payload.get("recorded_at_utc"), "usage_audit.recorded_at_utc")
    return payload


def _require_equal(
    label: str,
    *values: Any,
) -> None:
    if not values or any(value != values[0] for value in values[1:]):
        raise ReceiptFinalizerError(f"Kilo 五份事实的 {label} 关联不一致")


def finalize_kilo_binding(
    args: argparse.Namespace,
    *,
    write_output: bool = True,
) -> dict[str, Any]:
    """从五份独立原始事实推导 Kilo 请求成功与 usage 闭环。"""

    root = _validate_root(args.evidence_root)
    if write_output:
        output_path, output_relative = _output_location(root, args.output)
    else:
        output_path, output_relative = _existing_output_location(root, args.output)
    identity = _kilo_identity_from_args(args)
    time_window, started_at, checkpoint_at = _time_window_from_args(args)
    documents = {
        "installation": _load_document(
            root, args.installation, "kilo_installation"
        ),
        "ingress": _load_document(root, args.ingress, "kilo_ingress"),
        "runtime_audit": _load_document(
            root, args.runtime_audit, "runtime_audit"
        ),
        "response_witness": _load_document(
            root, args.response_witness, "kilo_response"
        ),
        "usage_audit": _load_document(root, args.usage_audit, "usage_audit"),
    }
    _require_unique_documents(documents.values())
    installation = _validate_installation(documents["installation"])
    ingress = _validate_ingress(documents["ingress"], identity)
    runtime = _validate_request_runtime(documents["runtime_audit"], identity)
    response = _validate_response(documents["response_witness"], identity)
    usage = _validate_usage(documents["usage_audit"], identity)

    _require_equal(
        "installation_id",
        installation["installation_id"],
        ingress["installation_id"],
        runtime["installation_id"],
        response["installation_id"],
    )
    _require_equal(
        "client_version",
        installation["client_version"],
        ingress["client_version"],
    )
    _require_equal(
        "request_id",
        ingress["request_id"],
        runtime["request_id"],
        response["request_id"],
        usage["request_id"],
    )
    _require_equal(
        "ingress_witness_id",
        ingress["witness_id"],
        runtime["ingress_witness_id"],
    )
    _require_equal(
        "client_id",
        identity["client_id"],
        ingress["client_id"],
        runtime["client_id"],
        response["client_id"],
    )
    _require_equal(
        "candidate_id",
        identity["candidate_id"],
        ingress["candidate_id"],
        runtime["candidate_id"],
        response["candidate_id"],
        usage["candidate_id"],
    )
    _require_equal("response_id", response["response_id"], usage["response_id"])
    _require_equal(
        "oauth_account_id",
        runtime["oauth_account_id"],
        usage["oauth_account_id"],
    )

    installed_at = _rfc3339(
        installation["observed_at_utc"], "kilo_installation.observed_at_utc"
    )
    ingress_at = _rfc3339(
        ingress["received_at_utc"], "kilo_ingress.received_at_utc"
    )
    runtime_at = _rfc3339(
        runtime["observed_at_utc"], "runtime_audit.observed_at_utc"
    )
    response_at = _rfc3339(
        response["completed_at_utc"], "kilo_response.completed_at_utc"
    )
    usage_at = _rfc3339(usage["recorded_at_utc"], "usage_audit.recorded_at_utc")
    if not installed_at <= ingress_at <= runtime_at <= response_at <= usage_at:
        raise ReceiptFinalizerError("Kilo 五份事实的时间顺序不成立")
    for label, observed_at in (
        ("kilo_ingress.received_at_utc", ingress_at),
        ("runtime_audit.observed_at_utc", runtime_at),
        ("kilo_response.completed_at_utc", response_at),
        ("usage_audit.recorded_at_utc", usage_at),
    ):
        _require_in_time_window(
            observed_at,
            label,
            started_at,
            checkpoint_at,
        )

    for name, document in tuple(documents.items()):
        documents[name] = _reload_same(root, document, name)

    contract = CLIENT_CONTRACTS[identity["client_id"]]
    request_proof = {
        "schema_version": CLIENT_REQUEST_PROOF_SCHEMA,
        "source": "sub2api-runtime-audit",
        "request_id": runtime["request_id"],
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "client_id": identity["client_id"],
        "client_version": installation["client_version"],
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": identity["model"],
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
        "auth_mode": "oauth",
        "oauth_account_id": runtime["oauth_account_id"],
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "candidate_image_id": identity["candidate_image_id"],
        "upstream_endpoint": runtime["upstream_endpoint"],
        "transport": runtime["transport"],
        "affected_branches": runtime["affected_branches"],
        "observed_at_utc": runtime["observed_at_utc"],
    }
    response_proof = {
        "schema_version": CLIENT_RESPONSE_PROOF_SCHEMA,
        "source": "sub2api-usage-audit",
        "request_id": runtime["request_id"],
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        "client_id": identity["client_id"],
        "candidate_id": identity["candidate_id"],
        "status": "success",
        "http_status": response["http_status"],
        "response_id": response["response_id"],
        "usage_recorded": True,
        "usage_id": usage["usage_id"],
        "usage_account_id": usage["oauth_account_id"],
        "completed_at_utc": response["completed_at_utc"],
    }
    producer = _producer(
        "kilo-binding",
        root,
        output_relative,
        {**identity, **time_window},
        documents,
    )
    raw_evidence = {
        name: document.binding() for name, document in sorted(documents.items())
    }
    receipt = {
        "schema_version": CLIENT_BINDING_SCHEMA,
        "status": "success",
        "campaign_id": identity["campaign_id"],
        "attempt_id": identity["attempt_id"],
        "run_nonce": identity["run_nonce"],
        **time_window,
        "client_id": identity["client_id"],
        "client_version": installation["client_version"],
        "protocol": contract["protocol"],
        "entrypoint": contract["entrypoint"],
        "model": identity["model"],
        "candidate_id": identity["candidate_id"],
        "target_version": identity["target_version"],
        "profile_id": identity["profile_id"],
        "profile_digest": identity["profile_digest"],
        "candidate_image_id": identity["candidate_image_id"],
        "source_tree_sha256": identity["source_tree_sha256"],
        "build_id": identity["build_id"],
        "deployed_version": identity["deployed_version"],
        "request_evidence": [
            documents[name].binding()
            for name in ("installation", "ingress", "runtime_audit")
        ],
        "response_evidence": [
            documents[name].binding()
            for name in ("response_witness", "usage_audit")
        ],
        "request_proof": request_proof,
        "response_proof": response_proof,
        "raw_evidence": raw_evidence,
        "producer": producer,
    }
    if write_output:
        _write_json_once(output_path, receipt)
    return receipt


def _scenario_identity_from_args(args: argparse.Namespace) -> dict[str, str]:
    scenario_id = _safe_id(args.scenario_id, "scenario_id")
    if scenario_id not in scenario_receipts.SUPPORTED_SCENARIOS:
        raise ReceiptFinalizerError("scenario_id 不是已登记的真实性门禁场景")
    return {
        "scenario_id": scenario_id,
        "job_id": _safe_id(args.job_id, "job_id"),
        "campaign_id": _safe_id(args.campaign_id, "campaign_id"),
        "attempt_id": _safe_id(args.attempt_id, "attempt_id"),
        "run_nonce": _run_nonce(args.run_nonce, "run_nonce"),
        "run_id": _safe_id(args.run_id, "run_id"),
    }


def finalize_scenario(
    args: argparse.Namespace,
    *,
    write_output: bool = True,
) -> dict[str, Any]:
    """把采集侧的原始场景事实承接为带 attempt 身份的成功收据。

    本函数只承接、不推断：final_state、observed_at_utc、evidence_bindings 与
    facts 全部原样取自事实文件，编排器不得根据 job 退出码补写目标字段。事实
    文件本身只在目标协议分支真实成立时才由采集侧产出。
    """

    root = _validate_root(args.evidence_root)
    if write_output:
        output_path, output_relative = _output_location(root, args.output)
    else:
        output_path, output_relative = _existing_output_location(root, args.output)
    identity = _scenario_identity_from_args(args)
    facts_document = _load_document(root, args.facts, "facts")
    try:
        validated = scenario_receipts.validate_facts_document(
            facts_document.payload,
            scenario_id=identity["scenario_id"],
            job_id=identity["job_id"],
            run_id=identity["run_id"],
        )
    except scenario_receipts.ScenarioReceiptError as error:
        raise ReceiptFinalizerError(f"场景原始事实不可信：{error}") from error
    facts_document = _reload_same(root, facts_document, "facts")
    producer = _producer(
        "scenario",
        root,
        output_relative,
        identity,
        {"facts": facts_document},
    )
    receipt = {
        "schema_version": SCENARIO_RECEIPT_SCHEMA,
        **identity,
        "status": "success",
        "final_state": validated["final_state"],
        "observed_at_utc": validated["observed_at_utc"],
        "evidence_bindings": validated["evidence_bindings"],
        "facts": validated["facts"],
        "producer": producer,
    }
    try:
        scenario_receipts.validate_receipt(
            receipt,
            scenario_id=identity["scenario_id"],
            job_id=identity["job_id"],
            campaign_id=identity["campaign_id"],
            attempt_id=identity["attempt_id"],
            run_nonce=identity["run_nonce"],
            run_id=identity["run_id"],
        )
    except scenario_receipts.ScenarioReceiptError as error:
        raise ReceiptFinalizerError(f"场景收据未通过结构校验：{error}") from error
    if write_output:
        _write_json_once(output_path, receipt)
    return receipt


def _replay_input_paths(
    producer: Mapping[str, Any],
    subcommand: str,
    receipt_relative: str,
    root: RootContext,
    receipt_inode: tuple[int, int],
) -> dict[str, Path]:
    """严格解析 producer 的输入绑定，不信任收据中的任意路径或摘要。"""

    bindings = producer.get("input_bindings")
    if not isinstance(bindings, list):
        raise ReceiptFinalizerError("producer.input_bindings 必须是数组")
    expected_names = sorted(REPLAY_INPUT_NAMES[subcommand])
    observed_names: list[str] = []
    observed_paths: set[str] = set()
    observed_inodes: set[tuple[int, int]] = set()
    result: dict[str, Path] = {}
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ReceiptFinalizerError(
                f"producer.input_bindings[{index}] 必须是对象"
            )
        _expect_exact(
            binding,
            {"name", "path", "sha256", "bytes"},
            f"producer.input_bindings[{index}]",
        )
        name = binding.get("name")
        if not isinstance(name, str) or name not in REPLAY_INPUT_NAMES[subcommand]:
            raise ReceiptFinalizerError("producer 包含未批准输入角色")
        path_value = binding.get("path")
        if (
            not isinstance(path_value, str)
            or not path_value
            or "\\" in path_value
            or any(ord(character) < 0x20 for character in path_value)
        ):
            raise ReceiptFinalizerError(f"producer 输入 {name} 路径非法")
        path = PurePosixPath(path_value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
            or str(path) != path_value
            or path_value == receipt_relative
        ):
            raise ReceiptFinalizerError(f"producer 输入 {name} 路径非法")
        _sha256(binding.get("sha256"), f"producer 输入 {name} SHA-256")
        _positive_integer(binding.get("bytes"), f"producer 输入 {name} 字节数")
        if name in result or path_value in observed_paths:
            raise ReceiptFinalizerError("producer 输入角色或路径重复")
        document = _load_document(
            root,
            Path(path_value),
            f"producer 输入 {name}",
        )
        if (
            document.sha256 != binding["sha256"]
            or document.size != binding["bytes"]
        ):
            raise ReceiptFinalizerError(f"producer 输入 {name} 文件绑定不一致")
        if document.inode == receipt_inode or document.inode in observed_inodes:
            raise ReceiptFinalizerError("producer 输入不得复用收据或其他输入 inode")
        observed_names.append(name)
        observed_paths.add(path_value)
        observed_inodes.add(document.inode)
        result[name] = Path(path_value)
    if observed_names != expected_names:
        raise ReceiptFinalizerError(
            "producer 输入角色必须完整且按名称排序"
        )
    return result


def _validate_replay_producer(
    receipt: BoundDocument,
    root: RootContext,
    expected_subcommand: str | None,
) -> tuple[str, Mapping[str, Any], dict[str, Path]]:
    """验证工具身份、规范参数、命令摘要和全部输入绑定。"""

    schema_version = receipt.payload.get("schema_version")
    subcommand = RECEIPT_SUBCOMMAND_BY_SCHEMA.get(schema_version)
    if subcommand is None:
        raise ReceiptFinalizerError("收据 schema_version 不受重放器支持")
    if expected_subcommand is not None:
        if (
            not isinstance(expected_subcommand, str)
            or expected_subcommand not in REPLAY_CANONICAL_FIELDS
        ):
            raise ReceiptFinalizerError("expected_subcommand 不受支持")
        if expected_subcommand != subcommand:
            raise ReceiptFinalizerError("收据类型与 expected_subcommand 不一致")

    producer = receipt.payload.get("producer")
    if not isinstance(producer, dict):
        raise ReceiptFinalizerError("收据缺少 producer")
    _expect_exact(
        producer,
        {
            "schema_version",
            "tool",
            "subcommand",
            "canonical_arguments",
            "input_bindings",
            "command_sha256",
        },
        "producer",
    )
    if producer.get("schema_version") != PRODUCER_SCHEMA:
        raise ReceiptFinalizerError("producer.schema_version 不匹配")
    if producer.get("subcommand") != subcommand:
        raise ReceiptFinalizerError("producer.subcommand 与收据类型不一致")

    tool = producer.get("tool")
    if not isinstance(tool, dict):
        raise ReceiptFinalizerError("producer.tool 必须是对象")
    _expect_exact(tool, {"path", "sha256"}, "producer.tool")
    tool_path = Path(__file__).resolve(strict=True)
    if tool.get("path") != str(tool_path):
        raise ReceiptFinalizerError("producer.tool.path 不是当前 finalizer")
    if tool.get("sha256") != _file_sha256(tool_path):
        raise ReceiptFinalizerError("producer.tool.sha256 与当前 finalizer 不一致")

    command_sha256 = _sha256(
        producer.get("command_sha256"),
        "producer.command_sha256",
    )
    producer_core = {
        key: value
        for key, value in producer.items()
        if key != "command_sha256"
    }
    if _fingerprint(producer_core) != command_sha256:
        raise ReceiptFinalizerError("producer.command_sha256 校验失败")

    canonical = producer.get("canonical_arguments")
    if not isinstance(canonical, dict):
        raise ReceiptFinalizerError("producer.canonical_arguments 必须是对象")
    _expect_exact(
        canonical,
        REPLAY_CANONICAL_FIELDS[subcommand],
        "producer.canonical_arguments",
    )
    if canonical.get("evidence_root") != str(root.resolved):
        raise ReceiptFinalizerError("producer.evidence_root 与重放根目录不一致")
    if canonical.get("output") != receipt.relative:
        raise ReceiptFinalizerError("producer.output 与收据路径不一致")
    input_paths = _replay_input_paths(
        producer,
        subcommand,
        receipt.relative,
        root,
        receipt.inode,
    )
    return subcommand, canonical, input_paths


def replay_receipt(
    receipt_path: Path | str,
    evidence_root: Path | str,
    expected_subcommand: str | None = None,
) -> dict[str, Any]:
    """只读重放三类收据，并要求重算结果与原收据逐字段完全相等。"""

    root = _validate_root(Path(evidence_root))
    receipt = _load_document(root, Path(receipt_path), "待重放收据")
    subcommand, canonical, input_paths = _validate_replay_producer(
        receipt,
        root,
        expected_subcommand,
    )
    namespace_values = {
        key: value
        for key, value in canonical.items()
        if key not in {"evidence_root", "output"}
    }
    namespace_values.update(
        {
            "evidence_root": root.alias,
            "output": Path(receipt.relative),
            **input_paths,
        }
    )
    arguments = argparse.Namespace(**namespace_values)
    if subcommand == "restoration":
        recomputed = finalize_restoration(arguments, write_output=False)
    elif subcommand == "observed-profile":
        recomputed = finalize_observed_profile(arguments, write_output=False)
    elif subcommand == "scenario":
        recomputed = finalize_scenario(arguments, write_output=False)
    else:
        recomputed = finalize_kilo_binding(arguments, write_output=False)
    receipt = _reload_same(root, receipt, "待重放收据")
    if recomputed != receipt.payload:
        raise ReceiptFinalizerError("收据内容与只读重放结果不一致")
    return recomputed


def _add_common_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)


def _add_candidate_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--attempt-started-at-utc", required=True)
    parser.add_argument("--client-checkpoint-at-utc", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--profile-digest", required=True)
    parser.add_argument("--source-tree-sha256", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--deployed-version", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从原始结构化事实生成不可覆盖的 Codex 升级收据"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    restoration = subparsers.add_parser(
        "restoration", help="生成五组环境恢复检查收据"
    )
    _add_common_output(restoration)
    restoration.add_argument(
        "--phase", choices=("official", "candidate"), required=True
    )
    restoration.add_argument("--candidate-id")
    for _, before_name, after_name, _ in RESTORATION_INPUTS:
        restoration.add_argument(
            f"--{before_name.replace('_', '-')}",
            dest=before_name,
            type=Path,
            required=True,
        )
        restoration.add_argument(
            f"--{after_name.replace('_', '-')}",
            dest=after_name,
            type=Path,
            required=True,
        )
    restoration.set_defaults(handler=finalize_restoration)

    observed = subparsers.add_parser(
        "observed-profile", help="生成运行画像观测收据"
    )
    _add_common_output(observed)
    _add_candidate_identity(observed)
    observed.add_argument("--image-id", required=True)
    observed.add_argument("--image-reference", required=True)
    observed.add_argument("--runtime-audit", type=Path, required=True)
    observed.set_defaults(handler=finalize_observed_profile)

    kilo = subparsers.add_parser(
        "kilo-binding", help="生成 Kilo 协议入口绑定收据"
    )
    _add_common_output(kilo)
    _add_candidate_identity(kilo)
    kilo.add_argument(
        "--client-id", choices=tuple(sorted(CLIENT_CONTRACTS)), required=True
    )
    kilo.add_argument("--candidate-image-id", required=True)
    kilo.add_argument("--model", required=True)
    kilo.add_argument("--installation", type=Path, required=True)
    kilo.add_argument("--ingress", type=Path, required=True)
    kilo.add_argument("--runtime-audit", type=Path, required=True)
    kilo.add_argument("--response-witness", type=Path, required=True)
    kilo.add_argument("--usage-audit", type=Path, required=True)
    kilo.set_defaults(handler=finalize_kilo_binding)

    scenario = subparsers.add_parser(
        "scenario", help="把采集侧场景原始事实承接为真实性收据"
    )
    _add_common_output(scenario)
    scenario.add_argument(
        "--scenario-id",
        choices=tuple(scenario_receipts.SUPPORTED_SCENARIOS),
        required=True,
    )
    scenario.add_argument("--job-id", required=True)
    scenario.add_argument("--campaign-id", required=True)
    scenario.add_argument("--attempt-id", required=True)
    scenario.add_argument("--run-nonce", required=True)
    scenario.add_argument("--run-id", required=True)
    scenario.add_argument("--facts", type=Path, required=True)
    scenario.set_defaults(handler=finalize_scenario)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        receipt = args.handler(args)
    except (ReceiptFinalizerError, EvidenceGuardError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    output = Path(args.output)
    print(
        json.dumps(
            {
                "output": str(output),
                "schema_version": receipt["schema_version"],
                "sha256": _file_sha256(
                    output
                    if output.is_absolute()
                    else Path(args.evidence_root) / output
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
