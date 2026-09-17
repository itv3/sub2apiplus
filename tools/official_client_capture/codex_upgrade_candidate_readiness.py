#!/usr/bin/env python3
"""VC-5 Candidate 派发前的静态就绪门禁与受控 ``/models`` probe。

静态层不发送模型请求；所有检查先形成不可覆盖收据，任一失败都在 reservation
之前结束。受控 probe 把每次 HTTP dispatch 的 intent 与 result 先落盘，再由项目
总账逐条计量。HTTP 已完成、总账提交前崩溃时，重放只补账、不重复发请求；只有
intent 而没有 result 时按账务不确定失败关闭。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol


STATIC_RECEIPT_SCHEMA = "codex-upgrade-candidate-readiness-static/v1"
STATIC_RECHECK_SCHEMA = "codex-upgrade-candidate-readiness-recheck/v1"
PROBE_SESSION_SCHEMA = "codex-upgrade-candidate-models-probe-session/v1"
PROBE_INTENT_SCHEMA = "codex-upgrade-candidate-models-probe-intent/v1"
PROBE_RESULT_SCHEMA = "codex-upgrade-candidate-models-probe-result/v1"
PROBE_RECEIPT_SCHEMA = "codex-upgrade-candidate-models-probe-receipt/v1"
PROBE_CACHE_EPOCH_SCHEMA = "codex-upgrade-candidate-models-cache-epoch/v1"
ACCOUNTING_CATEGORY = "candidate_readiness_models_probe/v1"
ACCOUNTING_POLICY = "one-project-ledger-event-per-wire-dispatch/v1"
CACHE_POLICY = "restart-before-and-after-models-probe/v1"
STATIC_CHECK_FAILURE_CODES = {
    "candidate-readiness.storage": "host-or-container-path-not-writable",
    "candidate-readiness.runtime-files": "runtime-file-missing-or-unexecutable",
    "candidate-readiness.root-filesystem": "disk-watermark-reached",
    "candidate-readiness.routing-snapshot": "routing-snapshot-unavailable",
    "candidate-readiness.routing-platform": "group-platform-or-account-invalid",
    "candidate-readiness.account-isolation": "eligible-account-set-invalid",
    "candidate-readiness.model-mapping": "model-mapping-not-clean",
    "candidate-readiness.image": "runtime-image-or-capability-mismatch",
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}(?:[-+][A-Za-z0-9.-]+)?$")

ROOT_MAX_USED_PERCENT = 69
ROOT_MIN_AVAILABLE_BYTES = 30 * 1024 * 1024 * 1024
DEFAULT_TTL_SECONDS = 10 * 60
DEFAULT_MAX_DISPATCHES = 2
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
HOST_DATA_ROOT = Path("/root/docker/capture-cli/data")
RUNTIME_FILES = (
    ("codex-fingerprint-capture-proxy", True),
    ("codex-profile-{target_version}.json", False),
)

STATIC_RECEIPT_FIELDS = {
    "schema_version",
    "campaign_id",
    "candidate_id",
    "image_id",
    "build_receipt_sha256",
    "observed_at_utc",
    "expires_at_utc",
    "ttl_seconds",
    "status",
    "checks",
    "failure_observations",
    "state_sha256",
    "live_request_count",
    "receipt_digest",
}
PROBE_SESSION_FIELDS = {
    "schema_version",
    "session_id",
    "campaign_id",
    "candidate_id",
    "image_id",
    "build_receipt_sha256",
    "static_receipt_digest",
    "target_version",
    "codex_account_id",
    "api_key_id",
    "ttl_seconds",
    "max_dispatches",
    "accounting_category",
    "accounting_policy",
    "cache_policy",
    "created_at_utc",
    "session_digest",
}
PROBE_INTENT_FIELDS = {
    "schema_version",
    "session_digest",
    "dispatch_index",
    "dispatch_id",
    "identity_key",
    "nonce",
    "nonce_sha256",
    "method",
    "path",
    "created_at_utc",
    "intent_digest",
}
PROBE_RESULT_FIELDS = {
    "schema_version",
    "session_digest",
    "dispatch_index",
    "dispatch_id",
    "identity_key",
    "intent_sha256",
    "response_status",
    "response_bytes",
    "response_body_sha256",
    "models_payload_valid",
    "models_count",
    "completed_at_utc",
    "result_digest",
}
PROBE_CACHE_EPOCH_FIELDS = {
    "schema_version",
    "session_digest",
    "observed_at_utc",
    "epoch",
    "receipt_digest",
}
PROBE_RECEIPT_FIELDS = {
    "schema_version",
    "session_id",
    "session_digest",
    "campaign_id",
    "candidate_id",
    "image_id",
    "build_receipt_sha256",
    "static_receipt_digest",
    "target_version",
    "codex_account_id",
    "api_key_id",
    "ttl_seconds",
    "max_dispatches",
    "status",
    "dispatch_count",
    "dispatch_result_sha256s",
    "accounting_operation_ids",
    "accounting_category",
    "accounting_policy",
    "project_head_sequence",
    "project_head_sha256",
    "cache_isolation",
    "observed_at_utc",
    "expires_at_utc",
    "failure_observations",
    "receipt_digest",
}


class CandidateReadinessError(RuntimeError):
    """就绪检查失败；属性供 supervisor 生成机器可复算诊断。"""

    failure_class = "environment-prerequisite"

    def __init__(
        self,
        message: str,
        *,
        observations: Sequence[Mapping[str, str]],
        receipt_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_observations = [dict(item) for item in observations]
        self.check_id = (
            self.failure_observations[0]["check_id"]
            if self.failure_observations
            else "candidate-readiness"
        )
        self.failure_code = (
            self.failure_observations[0]["failure_code"]
            if self.failure_observations
            else "unknown"
        )
        self.receipt_path = str(receipt_path) if receipt_path is not None else None


class ProbeAccountingUncertainError(CandidateReadinessError):
    """wire dispatch 是否完成不可判定，禁止重试造成漏账或重计。"""

    failure_class = "request-accounting-uncertain"


class ProbeBudgetExhaustedError(CandidateReadinessError):
    """probe 已消耗最后预算，禁止重试与 reservation。"""

    failure_class = "request-budget-exhausted"


class RuntimeAdmissionProtocol(Protocol):
    @property
    def head_sequence(self) -> int: ...

    @property
    def head_sha256(self) -> str: ...

    @property
    def remaining_live_requests(self) -> int | None: ...

    def account_candidate_probe(self, **kwargs: Any) -> Mapping[str, Any]: ...


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Dispatch = Callable[[str, str, str, str], tuple[int, bytes]]
Restart = Callable[[str], Mapping[str, Any]]
CrashHook = Callable[[str], None]


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _project_ledger_digest(value: Any) -> str:
    """复算项目总账既有的无尾换行 canonical JSON 摘要。"""

    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ValueError(f"{label}时间格式非法")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{label}非法")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label}必须是正整数")
    return value


def _private_directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"目录不得是符号链接：{path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(f"目录必须由当前用户持有且权限为 0700：{path}")
    return path


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"不可变文件已存在：{path}")
    _private_directory(path.parent)
    raw = _canonical(payload)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ValueError(f"不可变文件已存在：{path}") from error
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
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label}不是可信普通文件")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(f"{label}权限或属主非法")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return payload


def _default_runner(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
        shell=False,
    )


def _run_checked(
    arguments: Sequence[str],
    label: str,
    runner: CommandRunner,
) -> str:
    try:
        result = runner(arguments)
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise RuntimeError(f"{label}执行失败") from error
    if result.returncode != 0:
        raise RuntimeError(f"{label}退出码非零")
    return result.stdout.strip()


def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    failure_code: str,
    function: Callable[[], Mapping[str, Any]],
) -> None:
    """收集全部独立静态失败，错误内容只记录类型，避免意外保存秘密。"""

    try:
        evidence = dict(function())
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as error:
        checks.append(
            {
                "check_id": check_id,
                "failure_code": failure_code,
                "status": "failed",
                "error_type": type(error).__name__,
                "evidence": {},
            }
        )
    else:
        checks.append(
            {
                "check_id": check_id,
                "failure_code": failure_code,
                "status": "passed",
                "error_type": None,
                "evidence": evidence,
            }
        )


def _root_filesystem_fact() -> dict[str, Any]:
    filesystem = os.statvfs("/")
    block_size = filesystem.f_frsize or filesystem.f_bsize
    used_bytes = (filesystem.f_blocks - filesystem.f_bfree) * block_size
    available_bytes = filesystem.f_bavail * block_size
    denominator = used_bytes + available_bytes
    used_percent = (
        (used_bytes * 100 + denominator - 1) // denominator if denominator else 100
    )
    if (
        used_percent > ROOT_MAX_USED_PERCENT
        or available_bytes < ROOT_MIN_AVAILABLE_BYTES
    ):
        raise RuntimeError("根文件系统达到停线水位")
    return {
        "used_percent": used_percent,
        "available_bytes": available_bytes,
        "maximum_used_percent": ROOT_MAX_USED_PERCENT,
        "minimum_available_bytes": ROOT_MIN_AVAILABLE_BYTES,
        "identity": {"watermark_policy": "root-69-percent-and-30-gib/v1"},
    }


def _runtime_file_fact(target_version: str, runner: CommandRunner) -> dict[str, Any]:
    runtime_root = HOST_DATA_ROOT / "runtime"
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RuntimeError("宿主 runtime 目录不存在或不可信")
    records: list[dict[str, Any]] = []
    for template, executable in RUNTIME_FILES:
        name = template.format(target_version=target_version)
        path = runtime_root / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"runtime 文件缺失：{name}")
        metadata = path.stat()
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid() or mode & 0o022:
            raise RuntimeError(f"runtime 文件属主或权限非法：{name}")
        if executable and not os.access(path, os.X_OK):
            raise RuntimeError(f"runtime 文件不可执行：{name}")
        if not executable:
            try:
                payload = _load_json(path, f"runtime {name}")
            except ValueError as error:
                raise RuntimeError(f"runtime 画像不可信：{name}") from error
            if not payload:
                raise RuntimeError(f"runtime 画像为空：{name}")
        container_path = f"/capture/runtime/{name}"
        container_sha = _run_checked(
            ["docker", "exec", "__CAPTURE_CONTAINER__", "sha256sum", container_path],
            f"容器 runtime {name} 摘要",
            runner,
        ).split(maxsplit=1)[0]
        host_sha = _file_sha256(path)
        if container_sha != host_sha:
            raise RuntimeError(f"宿主与容器 runtime 文件不同源：{name}")
        if executable:
            _run_checked(
                ["docker", "exec", "__CAPTURE_CONTAINER__", "test", "-x", container_path],
                f"容器 runtime {name} 可执行性",
                runner,
            )
        records.append(
            {
                "name": name,
                "sha256": host_sha,
                "bytes": metadata.st_size,
                "mode": mode,
            }
        )
    return {"files": records, "identity": {"files": records}}


def _runner_for_capture_container(
    capture_container: str,
    runner: CommandRunner,
) -> CommandRunner:
    """把 runtime checker 的占位容器替换为冻结的 capture 容器。"""

    def wrapped(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return runner(
            [capture_container if value == "__CAPTURE_CONTAINER__" else value for value in arguments]
        )

    return wrapped


def _postgres_identity(container: str, runner: CommandRunner) -> tuple[str, str]:
    raw = _run_checked(
        ["docker", "inspect", container],
        "PostgreSQL 容器 inspect",
        runner,
    )
    payload = json.loads(raw)
    if not isinstance(payload, list) or len(payload) != 1:
        raise RuntimeError("PostgreSQL 容器 inspect 结构非法")
    environment = payload[0].get("Config", {}).get("Env", [])
    values: dict[str, str] = {}
    for item in environment if isinstance(environment, list) else []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            if key in {"POSTGRES_USER", "POSTGRES_DB"}:
                values[key] = value
    user = values.get("POSTGRES_USER", "sub2api")
    database = values.get("POSTGRES_DB", "sub2api")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]{0,62}", user) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_$-]{0,62}", database
    ):
        raise RuntimeError("PostgreSQL 身份非法")
    return user, database


def _psql(
    postgres_container: str,
    sql: str,
    runner: CommandRunner,
) -> str:
    user, database = _postgres_identity(postgres_container, runner)
    return _run_checked(
        [
            "docker",
            "exec",
            postgres_container,
            "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            database,
            "-c",
            sql,
        ],
        "Candidate 路由状态 SQL",
        runner,
    )


def _routing_snapshot(
    configuration: Mapping[str, Any], runner: CommandRunner
) -> dict[str, Any]:
    """一次读取 Candidate 路由状态，供两个独立门禁复用同一快照。"""

    account_id = int(configuration["codex_account_id"])
    api_key_id = int(configuration["api_key_id"])
    if account_id <= 0 or api_key_id <= 0:
        raise RuntimeError("账号或 API Key ID 非法")
    sql = f"""
WITH target_key AS (
  SELECT id, group_id, status FROM api_keys WHERE id = {api_key_id}
), target_account AS (
  SELECT id, platform, type, status, schedulable, parent_account_id, credentials
  FROM accounts WHERE id = {account_id}
), eligible AS (
  SELECT array_agg(a.id ORDER BY a.id) AS ids
  FROM account_groups ag JOIN accounts a ON a.id = ag.account_id
  WHERE ag.group_id = (SELECT group_id FROM target_key)
    AND a.platform = 'openai' AND a.type = 'oauth'
    AND a.status = 'active' AND a.schedulable = true
)
SELECT json_build_object(
  'account_id', (SELECT id FROM target_account),
  'account_platform', (SELECT platform FROM target_account),
  'account_type', (SELECT type FROM target_account),
  'account_status', (SELECT status FROM target_account),
  'account_schedulable', (SELECT schedulable FROM target_account),
  'parent_account_id', (SELECT parent_account_id FROM target_account),
  'token_present', (SELECT length(coalesce(credentials->>'access_token','')) > 0 FROM target_account),
  'model_mapping_type', (SELECT CASE WHEN NOT (credentials ? 'model_mapping') THEN 'missing' ELSE coalesce(jsonb_typeof(credentials->'model_mapping'),'null') END FROM target_account),
  'model_mapping_count', (SELECT CASE WHEN jsonb_typeof(credentials->'model_mapping') = 'object' THEN (SELECT count(*) FROM jsonb_object_keys(credentials->'model_mapping')) ELSE 0 END FROM target_account),
  'api_key_id', (SELECT id FROM target_key),
  'api_key_status', (SELECT status FROM target_key),
  'group_id', (SELECT group_id FROM target_key),
  'group_platform', (SELECT platform FROM groups WHERE id = (SELECT group_id FROM target_key)),
  'group_status', (SELECT status FROM groups WHERE id = (SELECT group_id FROM target_key)),
  'eligible_account_ids', coalesce((SELECT ids FROM eligible), ARRAY[]::bigint[])
);
"""
    try:
        payload = json.loads(
            _psql(str(configuration["postgres_container"]), sql, runner)
        )
    except json.JSONDecodeError as error:
        raise RuntimeError("Candidate 路由状态不是合法 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Candidate 路由状态不是对象")
    return payload


def _routing_platform_fact(
    configuration: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """校验 API Key、分组与目标账号本身的可调度平台身份。"""

    account_id = int(configuration["codex_account_id"])
    api_key_id = int(configuration["api_key_id"])
    expected = {
        "account_id": account_id,
        "account_platform": "openai",
        "account_type": "oauth",
        "account_status": "active",
        "account_schedulable": True,
        "parent_account_id": None,
        "token_present": True,
        "api_key_id": api_key_id,
        "api_key_status": "active",
        "group_platform": "openai",
        "group_status": "active",
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Candidate 分组平台或账号可调度身份不符合要求")
    identity = {key: payload.get(key) for key in sorted(expected)}
    identity["group_id"] = payload.get("group_id")
    return {**identity, "identity": identity}


def _account_isolation_fact(
    configuration: Mapping[str, Any], payload: Mapping[str, Any]
) -> dict[str, Any]:
    """单独校验分组的可用账号闭集。"""

    account_id = int(configuration["codex_account_id"])
    if payload.get("eligible_account_ids") != [account_id]:
        raise RuntimeError("Candidate 分组没有严格隔离到目标账号")
    identity = {
        "account_id": payload.get("account_id"),
        "group_id": payload.get("group_id"),
        "eligible_account_ids": payload.get("eligible_account_ids"),
    }
    return {**identity, "identity": identity}


def _model_mapping_fact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """单独校验目标账号的 model_mapping 初始状态。"""

    if payload.get("model_mapping_type") not in {"missing", "object"} or int(
        payload.get("model_mapping_count", -1)
    ) != 0:
        raise RuntimeError("Candidate 账号 model_mapping 未清空")
    identity = {
        key: payload.get(key)
        for key in (
            "account_id",
            "model_mapping_type",
            "model_mapping_count",
        )
    }
    return {**identity, "identity": identity}


def _service_image_fact(
    configuration: Mapping[str, Any],
    identity: Mapping[str, Any],
    candidate_id: str,
    build_receipt_sha256: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    image_id = identity.get("image_id")
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeError("Candidate image ID 非法")
    raw = _run_checked(
        ["docker", "inspect", str(configuration["service_container"])],
        "Candidate 服务容器 inspect",
        runner,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("Candidate 服务容器 inspect 不是合法 JSON") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("Candidate 服务容器 inspect 结构非法")
    container = payload[0]
    state = container.get("State")
    running = isinstance(state, dict) and state.get("Running") is True
    health = (
        state.get("Health", {}).get("Status")
        if isinstance(state, dict) and isinstance(state.get("Health"), dict)
        else state.get("Status") if isinstance(state, dict) else None
    )
    if container.get("Image") != image_id or not running or health not in {"healthy", "running"}:
        raise RuntimeError("运行服务容器未绑定当前 Candidate 镜像或未就绪")
    evidence = {
        "candidate_id": candidate_id,
        "image_id": image_id,
        "container_name": str(configuration["service_container"]),
        "container_id": container.get("Id"),
        "health": health,
        "build_receipt_sha256": build_receipt_sha256,
    }
    return {**evidence, "identity": evidence}


def collect_static_checks(
    *,
    configuration: Mapping[str, Any],
    target_version: str,
    candidate_id: str,
    identity: Mapping[str, Any],
    build_receipt_sha256: str,
    storage_probe: Mapping[str, Any],
    runner: CommandRunner | None = None,
) -> list[dict[str, Any]]:
    """采集静态层全部检查；调用方随后写不可变 receipt 再决定是否继续。"""

    _safe_id(candidate_id, "candidate_id")
    if not SHA256_RE.fullmatch(build_receipt_sha256):
        raise ValueError("build_receipt_sha256 非法")
    runner = runner or _default_runner
    checks: list[dict[str, Any]] = []
    routing_snapshot: dict[str, Any] | None = None

    def routing() -> dict[str, Any]:
        nonlocal routing_snapshot
        if routing_snapshot is None:
            routing_snapshot = _routing_snapshot(configuration, runner)
        return routing_snapshot

    def routing_fact() -> Mapping[str, Any]:
        snapshot = routing()
        identity = {
            key: snapshot.get(key)
            for key in sorted(
                {
                    "account_id",
                    "account_platform",
                    "account_type",
                    "account_status",
                    "account_schedulable",
                    "parent_account_id",
                    "token_present",
                    "model_mapping_type",
                    "model_mapping_count",
                    "api_key_id",
                    "api_key_status",
                    "group_id",
                    "group_platform",
                    "group_status",
                    "eligible_account_ids",
                }
            )
        }
        return {"identity": identity}

    def skipped(check_id: str, failure_code: str) -> None:
        checks.append(
            {
                "check_id": check_id,
                "failure_code": failure_code,
                "status": "skipped",
                "error_type": None,
                "evidence": {
                    "dependency_check_id": "candidate-readiness.routing-snapshot"
                },
            }
        )

    def storage() -> Mapping[str, Any]:
        if storage_probe.get("status") != "passed":
            raise RuntimeError("宿主／容器可写探针未通过")
        namespaces = storage_probe.get("writable_namespaces")
        if (
            not isinstance(namespaces, list)
            or not all(isinstance(item, Mapping) for item in namespaces)
            or {item.get("name") for item in namespaces if isinstance(item, Mapping)}
            != {"runs", "runtime"}
            or any(item.get("cleanup_verified") is not True for item in namespaces)
        ):
            raise RuntimeError("可写 runs/runtime 闭集不完整")
        namespace_facts = [
            {
                key: item.get(key)
                for key in (
                    "name",
                    "source",
                    "source_mode",
                    "source_uid",
                    "source_gid",
                    "source_device",
                    "source_inode",
                    "cleanup_verified",
                )
            }
            for item in namespaces
        ]
        raw_host_root = storage_probe.get("host_data_root")
        host_root = (
            {
                key: raw_host_root.get(key)
                for key in ("path", "mode", "uid", "gid", "device", "inode")
            }
            if isinstance(raw_host_root, Mapping)
            else None
        )
        identity_value = {
            "capture_container": storage_probe.get("capture_container"),
            "capture_root": storage_probe.get("capture_root"),
            "host_data_root": host_root,
            "writable_namespaces": namespace_facts,
            "job_roots_sha256": storage_probe.get("job_roots_sha256"),
        }
        return {"identity": identity_value}

    _check(
        checks,
        "candidate-readiness.storage",
        "host-or-container-path-not-writable",
        storage,
    )
    _check(
        checks,
        "candidate-readiness.runtime-files",
        "runtime-file-missing-or-unexecutable",
        lambda: _runtime_file_fact(
            target_version,
            _runner_for_capture_container(
                str(configuration["capture_container"]), runner
            ),
        ),
    )
    _check(
        checks,
        "candidate-readiness.root-filesystem",
        "disk-watermark-reached",
        _root_filesystem_fact,
    )
    _check(
        checks,
        "candidate-readiness.routing-snapshot",
        "routing-snapshot-unavailable",
        routing_fact,
    )
    if checks[-1]["status"] == "passed":
        _check(
            checks,
            "candidate-readiness.routing-platform",
            "group-platform-or-account-invalid",
            lambda: _routing_platform_fact(configuration, routing()),
        )
        _check(
            checks,
            "candidate-readiness.account-isolation",
            "eligible-account-set-invalid",
            lambda: _account_isolation_fact(configuration, routing()),
        )
        _check(
            checks,
            "candidate-readiness.model-mapping",
            "model-mapping-not-clean",
            lambda: _model_mapping_fact(routing()),
        )
    else:
        skipped(
            "candidate-readiness.routing-platform",
            "group-platform-or-account-invalid",
        )
        skipped(
            "candidate-readiness.account-isolation",
            "eligible-account-set-invalid",
        )
        skipped(
            "candidate-readiness.model-mapping",
            "model-mapping-not-clean",
        )
    _check(
        checks,
        "candidate-readiness.image",
        "runtime-image-or-capability-mismatch",
        lambda: _service_image_fact(
            configuration,
            identity,
            candidate_id,
            build_receipt_sha256,
            runner,
        ),
    )
    return checks


def _observations(checks: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "check_id": str(item["check_id"]),
            "failure_code": str(item["failure_code"]),
        }
        for item in checks
        if item.get("status") == "failed"
    ]


def _validate_static_checks(checks: Any) -> list[dict[str, Any]]:
    """验证静态检查的固定闭集；缺项、重复、未知项一律拒绝。"""

    if not isinstance(checks, list) or len(checks) != len(
        STATIC_CHECK_FAILURE_CODES
    ):
        raise ValueError("Candidate 静态门禁检查数量不闭合")
    rows = [dict(item) if isinstance(item, Mapping) else {} for item in checks]
    if [item.get("check_id") for item in rows] != list(
        STATIC_CHECK_FAILURE_CODES
    ):
        raise ValueError("Candidate 静态门禁检查 ID 缺失、重复或顺序非法")
    for item in rows:
        check_id = str(item["check_id"])
        evidence = item.get("evidence")
        status = item.get("status")
        if (
            set(item)
            != {"check_id", "failure_code", "status", "error_type", "evidence"}
            or item.get("failure_code") != STATIC_CHECK_FAILURE_CODES[check_id]
            or status not in {"passed", "failed", "skipped"}
            or not isinstance(evidence, Mapping)
        ):
            raise ValueError(f"Candidate 静态门禁检查结构非法：{check_id}")
        if status == "passed":
            if item.get("error_type") is not None or not isinstance(
                evidence.get("identity"), Mapping
            ):
                raise ValueError(f"Candidate 静态通过检查缺少稳定身份：{check_id}")
        elif status == "failed" and (
            not isinstance(item.get("error_type"), str)
            or not SAFE_ID_RE.fullmatch(str(item["error_type"]))
            or dict(evidence)
        ):
            raise ValueError(f"Candidate 静态失败检查错误事实非法：{check_id}")
        elif status == "skipped" and (
            check_id
            not in {
                "candidate-readiness.routing-platform",
                "candidate-readiness.account-isolation",
                "candidate-readiness.model-mapping",
            }
            or
            item.get("error_type") is not None
            or dict(evidence)
            != {"dependency_check_id": "candidate-readiness.routing-snapshot"}
        ):
            raise ValueError(f"Candidate 静态跳过检查依赖事实非法：{check_id}")
    routing_snapshot_status = rows[3]["status"]
    dependent_statuses = [item["status"] for item in rows[4:7]]
    if (
        routing_snapshot_status == "passed"
        and "skipped" in dependent_statuses
    ) or (
        routing_snapshot_status == "failed"
        and dependent_statuses != ["skipped", "skipped", "skipped"]
    ):
        raise ValueError("Candidate 静态路由检查依赖状态不闭合")
    return rows


def _static_state(checks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        str(item["check_id"]): (
            item.get("evidence", {}).get("identity")
            if isinstance(item.get("evidence"), Mapping)
            else None
        )
        for item in checks
        if item.get("status") == "passed"
    }


def write_static_receipt(
    root: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    image_id: str,
    build_receipt_sha256: str,
    checks: Sequence[Mapping[str, Any]],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    observed_at_utc: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """写一次不可覆盖静态门禁收据；失败收据同样保留。"""

    _safe_id(campaign_id, "campaign_id")
    _safe_id(candidate_id, "candidate_id")
    if not IMAGE_ID_RE.fullmatch(image_id) or not SHA256_RE.fullmatch(
        build_receipt_sha256
    ):
        raise ValueError("Candidate 静态门禁身份非法")
    if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 3600:
        raise ValueError("静态门禁 TTL 必须在 1..3600 秒")
    observed = observed_at_utc or _utc_now()
    observed_time = _parse_time(observed, "observed_at_utc")
    expires = (observed_time + timedelta(seconds=ttl_seconds)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    check_rows = _validate_static_checks(list(checks))
    observations = _observations(check_rows)
    state = _static_state(check_rows)
    payload: dict[str, Any] = {
        "schema_version": STATIC_RECEIPT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "image_id": image_id,
        "build_receipt_sha256": build_receipt_sha256,
        "observed_at_utc": observed,
        "expires_at_utc": expires,
        "ttl_seconds": ttl_seconds,
        "status": "passed" if not observations else "failed",
        "checks": check_rows,
        "failure_observations": observations,
        "state_sha256": _digest(state),
        "live_request_count": 0,
    }
    payload["receipt_digest"] = _digest(payload)
    target = _private_directory(root) / (
        f"static-{observed_time.strftime('%Y%m%dT%H%M%S%fZ')}-{payload['receipt_digest'][:12]}.json"
    )
    _write_once(target, payload)
    return target, payload


def validate_static_receipt(
    payload: Mapping[str, Any],
    *,
    campaign_id: str,
    candidate_id: str,
    image_id: str,
    build_receipt_sha256: str,
    now: datetime | None = None,
    require_fresh: bool = True,
) -> dict[str, Any]:
    """验证静态收据的自摘要、身份、TTL 与闭合检查结论。"""

    if not SHA256_RE.fullmatch(build_receipt_sha256):
        raise ValueError("Candidate 静态门禁 build receipt 摘要非法")
    receipt = dict(payload)
    if set(receipt) != STATIC_RECEIPT_FIELDS:
        raise ValueError("Candidate 静态门禁收据字段闭集非法")
    digest = receipt.pop("receipt_digest", None)
    if not isinstance(digest, str) or digest != _digest(receipt):
        raise ValueError("Candidate 静态门禁收据自摘要不一致")
    receipt["receipt_digest"] = digest
    check_rows = _validate_static_checks(receipt.get("checks"))
    observed_time = _parse_time(receipt.get("observed_at_utc"), "observed_at_utc")
    expires_time = _parse_time(receipt.get("expires_at_utc"), "expires_at_utc")
    ttl_seconds = receipt.get("ttl_seconds")
    current_time = now or datetime.now(timezone.utc)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 3600
        or expires_time != observed_time + timedelta(seconds=ttl_seconds)
        or observed_time > current_time + timedelta(minutes=5)
    ):
        raise ValueError("Candidate 静态门禁时间与 TTL 合同非法")
    image_check = next(
        item for item in check_rows if item["check_id"] == "candidate-readiness.image"
    )
    image_identity = (
        image_check["evidence"].get("identity")
        if image_check["status"] == "passed"
        else None
    )
    if (
        receipt.get("schema_version") != STATIC_RECEIPT_SCHEMA
        or receipt.get("campaign_id") != campaign_id
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("image_id") != image_id
        or receipt.get("build_receipt_sha256") != build_receipt_sha256
        or receipt.get("live_request_count") != 0
        or receipt.get("failure_observations") != _observations(check_rows)
        or receipt.get("state_sha256") != _digest(_static_state(check_rows))
        or (
            isinstance(image_identity, Mapping)
            and (
                image_identity.get("image_id") != image_id
                or image_identity.get("candidate_id") != candidate_id
                or image_identity.get("build_receipt_sha256")
                != build_receipt_sha256
            )
        )
    ):
        raise ValueError("Candidate 静态门禁收据身份或检查闭集非法")
    if require_fresh and current_time >= expires_time:
        raise CandidateReadinessError(
            "Candidate 静态就绪收据已过期",
            observations=[
                {"check_id": "candidate-readiness.ttl", "failure_code": "receipt-expired"}
            ],
        )
    observations = list(receipt["failure_observations"])
    if receipt.get("status") != ("passed" if not observations else "failed"):
        raise ValueError("Candidate 静态门禁状态与失败集合不一致")
    return receipt


def assert_static_passed(path: Path, payload: Mapping[str, Any]) -> None:
    observations = _observations(payload.get("checks", []))
    if observations:
        raise CandidateReadinessError(
            "Candidate 派发前静态就绪门禁未通过",
            observations=observations,
            receipt_path=path,
        )


def write_job_recheck(
    root: Path,
    *,
    initial: Mapping[str, Any],
    fresh_checks: Sequence[Mapping[str, Any]],
    job_id: str,
    now: datetime | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Job 启动前重采外部状态；必须继续通过且稳定身份未漂移。"""

    _safe_id(job_id, "job_id")
    initial = validate_static_receipt(
        initial,
        campaign_id=str(initial.get("campaign_id", "")),
        candidate_id=str(initial.get("candidate_id", "")),
        image_id=str(initial.get("image_id", "")),
        build_receipt_sha256=str(initial.get("build_receipt_sha256", "")),
        now=now,
        require_fresh=False,
    )
    assert_static_passed(Path("<embedded-static-receipt>"), initial)
    check_rows = _validate_static_checks(list(fresh_checks))
    observations = _observations(check_rows)
    fresh_state_sha256 = _digest(_static_state(check_rows))
    if not observations and fresh_state_sha256 != initial.get("state_sha256"):
        observations = [
            {
                "check_id": "candidate-readiness.toctou",
                "failure_code": "external-state-drift",
            }
        ]
    payload: dict[str, Any] = {
        "schema_version": STATIC_RECHECK_SCHEMA,
        "campaign_id": initial.get("campaign_id"),
        "candidate_id": initial.get("candidate_id"),
        "image_id": initial.get("image_id"),
        "job_id": job_id,
        "observed_at_utc": _utc_text(now or datetime.now(timezone.utc)),
        "source_static_receipt_digest": initial.get("receipt_digest"),
        "source_state_sha256": initial.get("state_sha256"),
        "fresh_state_sha256": fresh_state_sha256,
        "checks": check_rows,
        "status": "passed" if not observations else "failed",
        "failure_observations": observations,
        "live_request_count": 0,
    }
    payload["receipt_digest"] = _digest(payload)
    target = _private_directory(root) / (
        f"job-{job_id}-{payload['receipt_digest'][:12]}.json"
    )
    _write_once(target, payload)
    if observations:
        raise CandidateReadinessError(
            f"Job {job_id} 启动前外部状态复核未通过",
            observations=observations,
            receipt_path=target,
        )
    return target, payload


def _service_port(service_container: str, runner: CommandRunner) -> int:
    raw = _run_checked(
        ["docker", "port", service_container, "8080/tcp"],
        "Candidate 服务端口",
        runner,
    )
    matches = re.findall(
        r"^(?:127\.0\.0\.1|0\.0\.0\.0|\[::\]):([0-9]{1,5})$",
        raw,
        flags=re.MULTILINE,
    )
    if len(set(matches)) != 1:
        raise RuntimeError("无法解析 Candidate 服务宿主端口")
    port = int(matches[0])
    if not 1 <= port <= 65535:
        raise RuntimeError("Candidate 服务宿主端口非法")
    return port


def _api_key(configuration: Mapping[str, Any], runner: CommandRunner) -> str:
    api_key_id = int(configuration["api_key_id"])
    value = _psql(
        str(configuration["postgres_container"]),
        f"SELECT key FROM api_keys WHERE id = {api_key_id};",
        runner,
    )
    if (
        not value
        or len(value) > 16 * 1024
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RuntimeError("Candidate readiness probe 无法读取唯一 API Key")
    return value


def _default_dispatch(
    url: str,
    api_key: str,
    target_version: str,
    nonce: str,
) -> tuple[int, bytes]:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Candidate models probe 只允许直连回环 HTTP 服务")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            newurl: str,
        ) -> None:
            return None

    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "sub2apiplus-candidate-readiness/1.0",
            "Originator": "sub2apiplus_candidate_readiness",
            "Version": target_version,
            "X-Candidate-Readiness-Probe": nonce,
        },
    )
    # 不读取宿主 HTTP(S)_PROXY，也不跟随重定向；Authorization 永远只发送给
    # 已验证的 127.0.0.1 端口。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=60) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_RESPONSE_BYTES + 1)
        status = int(error.code)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Candidate models probe 响应超过限制")
    return status, body


def _models_response_fact(status: int, body: bytes) -> tuple[bool, int]:
    """只把完整 HTTP 200 模型目录视为路由成功，避免空 2xx 假阳性。"""

    if status != 200:
        return False, 0
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, 0
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not models or not all(
        isinstance(item, dict)
        and isinstance(item.get("slug"), str)
        and bool(item["slug"].strip())
        for item in models
    ):
        return False, 0
    return True, len(models)


def _models_request_path(target_version: str) -> str:
    return "/backend-api/codex/models?client_version=" + urllib.parse.quote(
        target_version, safe=""
    )


def _probe_identity_key(
    *,
    campaign_id: str,
    candidate_id: str,
    session_digest: str,
    dispatch_id: str,
    nonce: str,
    request_path: str,
) -> str:
    return _digest(
        {
            "accounting_category": ACCOUNTING_CATEGORY,
            "campaign_id": campaign_id,
            "candidate_id": candidate_id,
            "session_digest": session_digest,
            "dispatch_id": dispatch_id,
            "nonce": nonce,
            "method": "GET",
            "path": request_path,
        }
    )


def _default_restart(service_container: str, runner: CommandRunner) -> dict[str, Any]:
    _run_checked(
        ["docker", "restart", service_container],
        "Candidate 服务清理 models 缓存",
        runner,
    )
    deadline = time.monotonic() + 120
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        raw = _run_checked(
            ["docker", "inspect", service_container],
            "Candidate 服务重启后 inspect",
            runner,
        )
        payload = json.loads(raw)
        if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
            last = payload[0]
            state = last.get("State")
            status = state.get("Status") if isinstance(state, dict) else None
            health = (
                state.get("Health", {}).get("Status")
                if isinstance(state, dict) and isinstance(state.get("Health"), dict)
                else status
            )
            if (
                isinstance(state, dict)
                and state.get("Running") is True
                and health in {"healthy", "running"}
            ):
                epoch = {
                    "container_id": last.get("Id"),
                    "image_id": last.get("Image"),
                    "started_at_utc": state.get("StartedAt"),
                    "health": health,
                }
                if not isinstance(epoch["started_at_utc"], str):
                    raise RuntimeError("Candidate 服务 cache epoch 缺少 StartedAt")
                return epoch
        time.sleep(1)
    raise RuntimeError("Candidate 服务重启后未在时限内就绪")


def _validate_cache_epoch(
    value: Mapping[str, Any],
    *,
    service_container: str,
    image_id: str,
) -> dict[str, Any]:
    epoch = dict(value)
    base_fields = {"container_id", "image_id", "started_at_utc", "health"}
    if (
        frozenset(epoch)
        not in {frozenset(base_fields), frozenset(base_fields | {"service_container"})}
        or not isinstance(epoch.get("container_id"), str)
        or not epoch["container_id"]
        or epoch.get("image_id") != image_id
        or epoch.get("health") not in {"healthy", "running"}
        or not isinstance(epoch.get("started_at_utc"), str)
        or (
            "service_container" in epoch
            and epoch.get("service_container") != service_container
        )
    ):
        raise CandidateReadinessError(
            "Candidate 服务 cache epoch 身份非法",
            observations=[
                {
                    "check_id": "candidate-readiness.models-cache",
                    "failure_code": "restart-identity-invalid",
                }
            ],
        )
    _parse_time(epoch["started_at_utc"], "cache epoch started_at_utc")
    epoch["service_container"] = service_container
    return epoch


def _probe_root(campaign_dir: Path, candidate_id: str) -> Path:
    return _private_directory(
        campaign_dir / "control" / "candidate-readiness" / candidate_id / "models-probes"
    )


def _session_directories(root: Path) -> list[Path]:
    # stage 目录尚未对调用方发布，且 wire dispatch 只会在 _new_session 返回后
    # 发生；进程若在发布前中断，可安全清理该精确临时目录后重新创建。
    for stage in sorted(root.glob(".session-stage-*")):
        if stage.is_symlink() or not stage.is_dir():
            raise ValueError("Candidate models probe 含不可信 session stage")
        children = list(stage.iterdir())
        if any(child.is_symlink() or not child.is_file() for child in children):
            raise ValueError("Candidate models probe session stage 含非法条目")
        for child in children:
            child.unlink()
        stage.rmdir()
    result: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_dir() or not path.name.startswith("session-"):
            raise ValueError("Candidate models probe 根含非法条目")
        metadata = path.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("Candidate models probe session 权限或属主非法")
        result.append(path)
    return result


def _validate_session_payload(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """验证 session 固定字段闭集及其自摘要。"""

    session = dict(payload)
    unsigned = dict(session)
    recorded_digest = unsigned.pop("session_digest", None)
    session_id = session.get("session_id")
    if (
        set(session) != PROBE_SESSION_FIELDS
        or session.get("schema_version") != PROBE_SESSION_SCHEMA
        or not isinstance(recorded_digest, str)
        or not SHA256_RE.fullmatch(recorded_digest)
        or recorded_digest != _digest(unsigned)
        or not isinstance(session_id, str)
        or not SAFE_ID_RE.fullmatch(session_id)
        or session_id != path.name.removeprefix("session-")
        or not isinstance(session.get("campaign_id"), str)
        or not SAFE_ID_RE.fullmatch(session["campaign_id"])
        or not isinstance(session.get("candidate_id"), str)
        or not SAFE_ID_RE.fullmatch(session["candidate_id"])
        or not isinstance(session.get("image_id"), str)
        or not IMAGE_ID_RE.fullmatch(session["image_id"])
        or not isinstance(session.get("build_receipt_sha256"), str)
        or not SHA256_RE.fullmatch(session["build_receipt_sha256"])
        or not isinstance(session.get("static_receipt_digest"), str)
        or not SHA256_RE.fullmatch(session["static_receipt_digest"])
        or not isinstance(session.get("target_version"), str)
        or not VERSION_RE.fullmatch(session["target_version"])
        or isinstance(session.get("codex_account_id"), bool)
        or not isinstance(session.get("codex_account_id"), int)
        or session["codex_account_id"] <= 0
        or isinstance(session.get("api_key_id"), bool)
        or not isinstance(session.get("api_key_id"), int)
        or session["api_key_id"] <= 0
        or isinstance(session.get("ttl_seconds"), bool)
        or not isinstance(session.get("ttl_seconds"), int)
        or not 1 <= session["ttl_seconds"] <= 3600
        or isinstance(session.get("max_dispatches"), bool)
        or not isinstance(session.get("max_dispatches"), int)
        or not 1 <= session["max_dispatches"] <= 5
        or session.get("accounting_category") != ACCOUNTING_CATEGORY
        or session.get("accounting_policy") != ACCOUNTING_POLICY
        or session.get("cache_policy") != CACHE_POLICY
    ):
        raise ValueError("Candidate models probe session 自摘要或身份非法")
    _parse_time(session.get("created_at_utc"), "probe created_at_utc")
    return session


def _matching_session(
    root: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    image_id: str,
    build_receipt_sha256: str,
    static_receipt_digest: str,
    target_version: str,
    codex_account_id: int,
    api_key_id: int,
    ttl_seconds: int,
    max_dispatches: int,
) -> tuple[Path, dict[str, Any]] | None:
    matching: list[tuple[datetime, str, Path, dict[str, Any]]] = []
    incomplete: list[tuple[Path, dict[str, Any]]] = []
    for path in _session_directories(root):
        session_path = path / "session.json"
        if not session_path.is_file() or session_path.is_symlink():
            raise ValueError("Candidate models probe session 缺少可信 session.json")
        payload = _validate_session_payload(
            path,
            _load_json(session_path, "Candidate models probe session"),
        )
        created = _parse_time(payload.get("created_at_utc"), "probe created_at_utc")
        intents, results = _probe_files(path)
        try:
            intent_indexes = {
                int(item.name.split("-", 1)[1].split(".", 1)[0]) for item in intents
            }
            result_indexes = {
                int(item.name.split("-", 1)[1].split(".", 1)[0]) for item in results
            }
        except (IndexError, ValueError) as error:
            raise ValueError("Candidate models probe dispatch 文件名非法") from error
        if intent_indexes - result_indexes:
            missing = min(intent_indexes - result_indexes)
            missing_path = next(
                item
                for item in intents
                if int(item.name.split("-", 1)[1].split(".", 1)[0]) == missing
            )
            raise ProbeAccountingUncertainError(
                "Candidate models probe 存在只有 intent、没有 result 的 dispatch",
                observations=[
                    {
                        "check_id": "candidate-readiness.models-probe",
                        "failure_code": "dispatch-result-missing",
                    }
                ],
                receipt_path=missing_path,
            )
        if result_indexes - intent_indexes:
            raise ValueError("Candidate models probe result 缺少 intent")
        receipt_path = path / "receipt.json"
        if receipt_path.exists() and (
            receipt_path.is_symlink() or not receipt_path.is_file()
        ):
            raise ValueError("Candidate models probe receipt 不是可信普通文件")
        is_matching = (
            payload.get("campaign_id") == campaign_id
            and payload.get("candidate_id") == candidate_id
            and payload.get("image_id") == image_id
            and payload.get("build_receipt_sha256") == build_receipt_sha256
            and payload.get("static_receipt_digest") == static_receipt_digest
            and payload.get("target_version") == target_version
            and payload.get("codex_account_id") == codex_account_id
            and payload.get("api_key_id") == api_key_id
            and payload.get("ttl_seconds") == ttl_seconds
            and payload.get("max_dispatches") == max_dispatches
            and payload.get("accounting_category") == ACCOUNTING_CATEGORY
            and payload.get("accounting_policy") == ACCOUNTING_POLICY
            and payload.get("cache_policy") == CACHE_POLICY
        )
        if not receipt_path.exists():
            if not is_matching:
                raise ValueError("存在属于其它 Candidate 身份的未完成 probe session")
            incomplete.append((path, payload))
        if is_matching:
            matching.append((created, str(payload["session_id"]), path, payload))
    if len(incomplete) > 1:
        raise ValueError("Candidate models probe 存在多个未完成 session")
    if incomplete:
        return incomplete[0]
    if matching:
        _created, _session_id, path, payload = max(
            matching, key=lambda item: (item[0], item[1])
        )
        return path, payload
    return None


def _new_session(
    root: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    image_id: str,
    build_receipt_sha256: str,
    static_receipt_digest: str,
    target_version: str,
    codex_account_id: int,
    api_key_id: int,
    ttl_seconds: int,
    max_dispatches: int,
    created_at_utc: str,
) -> tuple[Path, dict[str, Any]]:
    session_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(8)}"
    path = root / f"session-{session_id}"
    stage = root / f".session-stage-{session_id}"
    stage.mkdir(mode=0o700)
    payload = {
        "schema_version": PROBE_SESSION_SCHEMA,
        "session_id": session_id,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "image_id": image_id,
        "build_receipt_sha256": build_receipt_sha256,
        "static_receipt_digest": static_receipt_digest,
        "target_version": target_version,
        "codex_account_id": codex_account_id,
        "api_key_id": api_key_id,
        "ttl_seconds": ttl_seconds,
        "max_dispatches": max_dispatches,
        "accounting_category": ACCOUNTING_CATEGORY,
        "accounting_policy": ACCOUNTING_POLICY,
        "cache_policy": CACHE_POLICY,
        "created_at_utc": created_at_utc,
    }
    payload["session_digest"] = _digest(payload)
    try:
        _write_once(stage / "session.json", payload)
        os.rename(stage, path)
        directory_descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        # 只清理尚未发布、且此函数返回前不可能触发 wire 的精确 stage。
        if stage.is_dir() and not stage.is_symlink():
            for child in stage.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
            try:
                stage.rmdir()
            except OSError:
                pass
        raise
    return path, payload


def _probe_files(session_root: Path) -> tuple[list[Path], list[Path]]:
    intent_pattern = re.compile(r"^dispatch-([0-9]{2})\.intent\.json$")
    result_pattern = re.compile(r"^dispatch-([0-9]{2})\.result\.json$")
    intents: list[Path] = []
    results: list[Path] = []
    for path in session_root.iterdir():
        if path.name in {
            "session.json",
            "receipt.json",
            "cache-epoch-before.json",
            "cache-epoch-after.json",
        }:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Candidate models probe session 含不可信控制文件")
            continue
        if intent_pattern.fullmatch(path.name):
            intents.append(path)
        elif result_pattern.fullmatch(path.name):
            results.append(path)
        else:
            raise ValueError("Candidate models probe session 含非法文件")
        if path.is_symlink() or not path.is_file():
            raise ValueError("Candidate models probe dispatch 不是可信普通文件")
    intents.sort()
    results.sort()
    return intents, results


def _load_cache_epoch_receipt(
    path: Path,
    *,
    session: Mapping[str, Any],
    service_container: str,
    image_id: str,
    label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = _load_json(path, f"Candidate models probe {label} cache epoch")
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_digest", None)
    if (
        set(receipt) != PROBE_CACHE_EPOCH_FIELDS
        or receipt.get("schema_version") != PROBE_CACHE_EPOCH_SCHEMA
        or not isinstance(digest, str)
        or not SHA256_RE.fullmatch(digest)
        or digest != _digest(unsigned)
        or receipt.get("session_digest") != session.get("session_digest")
        or not isinstance(receipt.get("epoch"), Mapping)
    ):
        raise ValueError(f"Candidate models probe {label} cache epoch 收据非法")
    _parse_time(receipt.get("observed_at_utc"), "cache epoch observed_at_utc")
    epoch = _validate_cache_epoch(
        receipt["epoch"],
        service_container=service_container,
        image_id=image_id,
    )
    return receipt, epoch


def _write_cache_epoch_receipt(
    path: Path,
    *,
    session: Mapping[str, Any],
    epoch: Mapping[str, Any],
) -> dict[str, Any]:
    """原子发布一份与 probe session 绑定的独立 cache epoch 收据。"""

    receipt = {
        "schema_version": PROBE_CACHE_EPOCH_SCHEMA,
        "session_digest": session["session_digest"],
        "observed_at_utc": _utc_now(),
        "epoch": dict(epoch),
    }
    receipt["receipt_digest"] = _digest(receipt)
    _write_once(path, receipt)
    return receipt


def _validate_probe_receipt(
    payload: Mapping[str, Any],
    *,
    session: Mapping[str, Any],
    service_container: str,
    image_id: str,
    current_time: datetime,
) -> dict[str, Any]:
    """验证 probe 收据的身份、时间、计量与缓存字段闭集。"""

    receipt = dict(payload)
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_digest", None)
    dispatch_count = receipt.get("dispatch_count")
    status = receipt.get("status")
    result_sha256s = receipt.get("dispatch_result_sha256s")
    operation_ids = receipt.get("accounting_operation_ids")
    failure_observations = receipt.get("failure_observations")
    max_dispatches = int(session["max_dispatches"])
    identity_fields = {
        "session_id",
        "session_digest",
        "campaign_id",
        "candidate_id",
        "image_id",
        "build_receipt_sha256",
        "static_receipt_digest",
        "target_version",
        "codex_account_id",
        "api_key_id",
        "ttl_seconds",
        "max_dispatches",
        "accounting_category",
        "accounting_policy",
    }
    if (
        set(receipt) != PROBE_RECEIPT_FIELDS
        or receipt.get("schema_version") != PROBE_RECEIPT_SCHEMA
        or not isinstance(digest, str)
        or not SHA256_RE.fullmatch(digest)
        or digest != _digest(unsigned)
        or any(receipt.get(key) != session.get(key) for key in identity_fields)
        or status not in {"passed", "failed"}
        or isinstance(dispatch_count, bool)
        or not isinstance(dispatch_count, int)
        or not 0 <= dispatch_count <= max_dispatches
        or not isinstance(result_sha256s, list)
        or len(result_sha256s) != dispatch_count
        or not all(
            isinstance(item, str) and SHA256_RE.fullmatch(item)
            for item in result_sha256s
        )
        or not isinstance(operation_ids, list)
        or len(operation_ids) != dispatch_count
        or len(set(operation_ids)) != len(operation_ids)
        or not all(isinstance(item, str) and bool(item) for item in operation_ids)
        or isinstance(receipt.get("project_head_sequence"), bool)
        or not isinstance(receipt.get("project_head_sequence"), int)
        or receipt["project_head_sequence"] < 0
        or not isinstance(receipt.get("project_head_sha256"), str)
        or not SHA256_RE.fullmatch(receipt["project_head_sha256"])
        or not isinstance(failure_observations, list)
    ):
        raise ValueError("Candidate models probe receipt 身份或字段闭集非法")

    allowed_failures = {
        "no-successful-response",
        "live-request-budget-exhausted",
    }
    if status == "passed":
        if dispatch_count < 1 or failure_observations != []:
            raise ValueError("Candidate models probe 通过状态与请求事实不一致")
    else:
        if (
            len(failure_observations) != 1
            or not isinstance(failure_observations[0], Mapping)
            or set(failure_observations[0]) != {"check_id", "failure_code"}
            or failure_observations[0].get("check_id")
            != "candidate-readiness.models-probe"
            or failure_observations[0].get("failure_code") not in allowed_failures
            or (
                failure_observations[0].get("failure_code")
                == "no-successful-response"
                and dispatch_count != max_dispatches
            )
            or (
                failure_observations[0].get("failure_code")
                == "live-request-budget-exhausted"
                and dispatch_count >= max_dispatches
            )
        ):
            raise ValueError("Candidate models probe 失败状态与请求事实不一致")

    observed = _parse_time(receipt.get("observed_at_utc"), "probe observed_at_utc")
    expires = _parse_time(receipt.get("expires_at_utc"), "probe expires_at_utc")
    if (
        expires != observed + timedelta(seconds=int(session["ttl_seconds"]))
        or observed > current_time + timedelta(minutes=5)
    ):
        raise ValueError("Candidate models probe 收据时间与 TTL 合同非法")

    cache = receipt.get("cache_isolation")
    if not isinstance(cache, Mapping) or set(cache) != {
        "policy",
        "cache_epoch_before_receipt_sha256",
        "cache_epoch_after_receipt_sha256",
        "restart_before_probe",
        "restart_after_probe",
        "cache_epoch_after",
    }:
        raise ValueError("Candidate models probe 缓存隔离字段闭集非法")
    if (
        cache.get("policy") != CACHE_POLICY
        or not isinstance(cache.get("cache_epoch_before_receipt_sha256"), str)
        or not SHA256_RE.fullmatch(cache["cache_epoch_before_receipt_sha256"])
        or not isinstance(cache.get("cache_epoch_after_receipt_sha256"), str)
        or not SHA256_RE.fullmatch(cache["cache_epoch_after_receipt_sha256"])
        or cache.get("restart_after_probe") is not True
        or not isinstance(cache.get("restart_before_probe"), Mapping)
        or not isinstance(cache.get("cache_epoch_after"), Mapping)
    ):
        raise ValueError("Candidate models probe 缓存隔离身份非法")
    before = _validate_cache_epoch(
        cache["restart_before_probe"],
        service_container=service_container,
        image_id=image_id,
    )
    after = _validate_cache_epoch(
        cache["cache_epoch_after"],
        service_container=service_container,
        image_id=image_id,
    )
    if _parse_time(after["started_at_utc"], "cache epoch after") <= _parse_time(
        before["started_at_utc"], "cache epoch before"
    ):
        raise ValueError("Candidate models probe 前后 cache epoch 未递增")
    return receipt


def _load_dispatch_pair(
    *,
    index: int,
    intent_path: Path,
    result_path: Path,
    session: Mapping[str, Any],
    request_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    intent = _load_json(intent_path, f"Candidate models probe intent {index}")
    intent_unsigned = dict(intent)
    intent_digest = intent_unsigned.pop("intent_digest", None)
    nonce = intent.get("nonce")
    dispatch_id = intent.get("dispatch_id")
    identity_key = intent.get("identity_key")
    if (
        set(intent) != PROBE_INTENT_FIELDS
        or not isinstance(intent_digest, str)
        or not SHA256_RE.fullmatch(intent_digest)
        or intent_digest != _digest(intent_unsigned)
        or intent.get("schema_version") != PROBE_INTENT_SCHEMA
        or intent.get("session_digest") != session.get("session_digest")
        or intent.get("dispatch_index") != index
        or not isinstance(dispatch_id, str)
        or not SAFE_ID_RE.fullmatch(dispatch_id)
        or not isinstance(nonce, str)
        or not re.fullmatch(r"[0-9a-f]{48}", nonce)
        or intent.get("nonce_sha256")
        != hashlib.sha256(nonce.encode("ascii")).hexdigest()
        or intent.get("method") != "GET"
        or intent.get("path") != request_path
        or identity_key
        != _probe_identity_key(
            campaign_id=str(session["campaign_id"]),
            candidate_id=str(session["candidate_id"]),
            session_digest=str(session["session_digest"]),
            dispatch_id=dispatch_id,
            nonce=nonce,
            request_path=request_path,
        )
    ):
        raise ValueError("Candidate models probe intent 绑定非法")
    intent_time = _parse_time(intent.get("created_at_utc"), "probe intent created_at_utc")

    result = _load_json(result_path, f"Candidate models probe dispatch {index}")
    result_unsigned = dict(result)
    result_digest = result_unsigned.pop("result_digest", None)
    if (
        set(result) != PROBE_RESULT_FIELDS
        or not isinstance(result_digest, str)
        or not SHA256_RE.fullmatch(result_digest)
        or result_digest != _digest(result_unsigned)
        or result.get("schema_version") != PROBE_RESULT_SCHEMA
        or result.get("session_digest") != session.get("session_digest")
        or result.get("dispatch_index") != index
        or result.get("dispatch_id") != dispatch_id
        or result.get("identity_key") != identity_key
        or result.get("intent_sha256") != _file_sha256(intent_path)
        or not isinstance(result.get("response_status"), int)
        or isinstance(result.get("response_status"), bool)
        or not 100 <= result["response_status"] <= 599
        or not isinstance(result.get("response_bytes"), int)
        or isinstance(result.get("response_bytes"), bool)
        or not 0 <= result["response_bytes"] <= MAX_RESPONSE_BYTES
        or not isinstance(result.get("response_body_sha256"), str)
        or not SHA256_RE.fullmatch(result["response_body_sha256"])
        or not isinstance(result.get("models_payload_valid"), bool)
        or not isinstance(result.get("models_count"), int)
        or isinstance(result.get("models_count"), bool)
        or result["models_count"] < 0
        or (result["models_payload_valid"] and result["models_count"] < 1)
        or (not result["models_payload_valid"] and result["models_count"] != 0)
        or (result["models_payload_valid"] and result["response_status"] != 200)
    ):
        raise ValueError("Candidate models probe result 绑定非法")
    if _parse_time(result.get("completed_at_utc"), "probe completed_at_utc") < intent_time:
        raise ValueError("Candidate models probe result 完成时间早于 intent")
    return intent, result


def _ledger_head_sha256_at(
    events: Sequence[Mapping[str, Any]],
    plan_sha256: str,
    sequence: int,
) -> str:
    """返回不可变项目总账在指定序号处的权威 head。"""

    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or sequence > len(events)
    ):
        raise ValueError("Candidate models probe 项目总账祖先序号非法")
    if sequence == 0:
        return plan_sha256
    value = events[sequence - 1].get("event_sha256")
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("Candidate models probe 项目总账祖先摘要非法")
    return value


def _validate_project_ledger_history(
    value: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """纯内存重放传入的项目总账事件链，不读取或改写总账文件。"""

    if set(value) != {
        "plan_sha256",
        "events",
        "head_sequence",
        "head_sha256",
    }:
        raise ValueError("Candidate models probe 项目总账历史字段不闭合")
    plan_sha256 = value.get("plan_sha256")
    raw_events = value.get("events")
    head_sequence = value.get("head_sequence")
    head_sha256 = value.get("head_sha256")
    if (
        not isinstance(plan_sha256, str)
        or not SHA256_RE.fullmatch(plan_sha256)
        or not isinstance(raw_events, list)
        or isinstance(head_sequence, bool)
        or not isinstance(head_sequence, int)
        or head_sequence != len(raw_events)
        or not isinstance(head_sha256, str)
        or not SHA256_RE.fullmatch(head_sha256)
    ):
        raise ValueError("Candidate models probe 项目总账历史身份非法")

    event_fields = {
        "schema_version",
        "sequence",
        "operation_id",
        "recorded_at_utc",
        "event_type",
        "payload",
        "payload_sha256",
        "source_batch_sha256",
        "previous_event_sha256",
        "event_sha256",
    }
    events: list[dict[str, Any]] = []
    previous: str | None = None
    seen_operations: set[str] = set()
    for expected_sequence, raw_event in enumerate(raw_events, 1):
        if not isinstance(raw_event, Mapping):
            raise ValueError("Candidate models probe 项目总账事件不是对象")
        event = dict(raw_event)
        unsigned = dict(event)
        event_sha256 = unsigned.pop("event_sha256", None)
        operation_id = event.get("operation_id")
        payload = event.get("payload")
        source_batch_sha256 = event.get("source_batch_sha256")
        if (
            set(event) != event_fields
            or event.get("sequence") != expected_sequence
            or not isinstance(operation_id, str)
            or not operation_id
            or operation_id in seen_operations
            or not isinstance(payload, Mapping)
            or event.get("payload_sha256")
            != _project_ledger_digest(dict(payload))
            or event.get("previous_event_sha256") != previous
            or not isinstance(event_sha256, str)
            or not SHA256_RE.fullmatch(event_sha256)
            or event_sha256 != _project_ledger_digest(unsigned)
            or (
                source_batch_sha256 is not None
                and (
                    not isinstance(source_batch_sha256, str)
                    or not SHA256_RE.fullmatch(source_batch_sha256)
                )
            )
        ):
            raise ValueError("Candidate models probe 项目总账事件链非法")
        _parse_time(event.get("recorded_at_utc"), "project ledger event recorded_at_utc")
        seen_operations.add(operation_id)
        previous = event_sha256
        events.append(event)
    expected_head = events[-1]["event_sha256"] if events else plan_sha256
    if head_sha256 != expected_head:
        raise ValueError("Candidate models probe 项目总账当前 head 漂移")
    return plan_sha256, events


def replay_models_probe_bundle(
    session_root: Path,
    *,
    service_container: str,
    image_id: str,
    project_ledger_history: Mapping[str, Any],
    reservation_head_sequence: int,
    reservation_head_sha256: str,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """纯只读重放一份已完成 probe 的全部文件与项目总账祖先。

    本函数不调用 runner、HTTP dispatch、服务重启或总账追加接口。reservation
    loader 用它证明 session、每次 intent/result、前后 cache epoch、最终 receipt
    和逐 dispatch 账务 operation 仍组成同一条不可变证据链。
    """

    if session_root.is_symlink() or not session_root.is_dir():
        raise ValueError("Candidate models probe session 目录不可信")
    metadata = session_root.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("Candidate models probe session 权限或属主非法")
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ValueError("Candidate models probe 镜像身份非法")
    if (
        isinstance(reservation_head_sequence, bool)
        or not isinstance(reservation_head_sequence, int)
        or reservation_head_sequence < 0
        or not isinstance(reservation_head_sha256, str)
        or not SHA256_RE.fullmatch(reservation_head_sha256)
    ):
        raise ValueError("Candidate models probe reservation 总账 head 非法")

    session = _validate_session_payload(
        session_root,
        _load_json(session_root / "session.json", "Candidate models probe session"),
    )
    receipt = _validate_probe_receipt(
        _load_json(session_root / "receipt.json", "Candidate models probe receipt"),
        session=session,
        service_container=service_container,
        image_id=image_id,
        current_time=current_time or datetime.now(timezone.utc),
    )

    intents, results = _probe_files(session_root)
    intents_by_index = {
        int(path.name.split("-", 1)[1].split(".", 1)[0]): path
        for path in intents
    }
    results_by_index = {
        int(path.name.split("-", 1)[1].split(".", 1)[0]): path
        for path in results
    }
    dispatch_count = int(receipt["dispatch_count"])
    expected_indexes = set(range(1, dispatch_count + 1))
    if (
        len(intents_by_index) != len(intents)
        or len(results_by_index) != len(results)
        or set(intents_by_index) != expected_indexes
        or set(results_by_index) != expected_indexes
    ):
        raise ValueError("Candidate models probe bundle 的 dispatch 文件闭集非法")

    request_path = _models_request_path(str(session["target_version"]))
    dispatches: list[dict[str, Any]] = []
    result_sha256s: list[str] = []
    for index in range(1, dispatch_count + 1):
        intent_path = intents_by_index[index]
        result_path = results_by_index[index]
        intent, result = _load_dispatch_pair(
            index=index,
            intent_path=intent_path,
            result_path=result_path,
            session=session,
            request_path=request_path,
        )
        result_sha256s.append(_file_sha256(result_path))
        dispatches.append({"intent": intent, "result": result})
    if receipt.get("dispatch_result_sha256s") != result_sha256s:
        raise ValueError("Candidate models probe receipt 与 result 文件摘要不一致")

    cache_before_path = session_root / "cache-epoch-before.json"
    _cache_receipt, cache_epoch_before = _load_cache_epoch_receipt(
        cache_before_path,
        session=session,
        service_container=service_container,
        image_id=image_id,
        label="前置",
    )
    cache_after_path = session_root / "cache-epoch-after.json"
    _cache_after_receipt, cache_epoch_after = _load_cache_epoch_receipt(
        cache_after_path,
        session=session,
        service_container=service_container,
        image_id=image_id,
        label="后置",
    )
    cache = receipt["cache_isolation"]
    receipt_cache_before = _validate_cache_epoch(
        cache["restart_before_probe"],
        service_container=service_container,
        image_id=image_id,
    )
    receipt_cache_after = _validate_cache_epoch(
        cache["cache_epoch_after"],
        service_container=service_container,
        image_id=image_id,
    )
    if (
        _file_sha256(cache_before_path)
        != cache.get("cache_epoch_before_receipt_sha256")
        or cache_epoch_before != receipt_cache_before
    ):
        raise ValueError("Candidate models probe receipt 与前置 cache epoch 漂移")
    if (
        _file_sha256(cache_after_path)
        != cache.get("cache_epoch_after_receipt_sha256")
        or cache_epoch_after != receipt_cache_after
    ):
        raise ValueError("Candidate models probe receipt 与后置 cache epoch 漂移")

    plan_sha256, ledger_events = _validate_project_ledger_history(
        project_ledger_history
    )
    receipt_head_sequence = int(receipt["project_head_sequence"])
    if (
        receipt_head_sequence > reservation_head_sequence
        or _ledger_head_sha256_at(
            ledger_events,
            plan_sha256,
            receipt_head_sequence,
        )
        != receipt.get("project_head_sha256")
        or _ledger_head_sha256_at(
            ledger_events,
            plan_sha256,
            reservation_head_sequence,
        )
        != reservation_head_sha256
    ):
        raise ValueError("Candidate models probe 与项目总账 head 祖先链不一致")

    events_by_operation = {
        str(event["operation_id"]): event for event in ledger_events
    }
    operation_ids = receipt.get("accounting_operation_ids")
    if not isinstance(operation_ids, list):
        raise ValueError("Candidate models probe 缺少账务 operation 列表")
    accounting_events: list[dict[str, Any]] = []
    accounting_sequences: list[int] = []
    for index, operation_id in enumerate(operation_ids, 1):
        event = events_by_operation.get(str(operation_id))
        result_path = results_by_index[index]
        result = dispatches[index - 1]["result"]
        expected_operation_id = (
            "candidate-probe:"
            + hashlib.sha256(str(result["dispatch_id"]).encode("utf-8")).hexdigest()[:32]
        )
        expected_payload = {
            "campaign_id": session["campaign_id"],
            "candidate_id": session["candidate_id"],
            "dispatch_id": result["dispatch_id"],
            "accounting_category": ACCOUNTING_CATEGORY,
            "response_status": result["response_status"],
            "receipt_sha256": _file_sha256(result_path),
            "request": {
                "status": "resolved",
                "identity_keys": [result["identity_key"]],
                "estimated_delta": 0,
            },
        }
        if (
            operation_id != expected_operation_id
            or event is None
            or event.get("event_type") != "candidate_probe_accounted"
            or dict(event.get("payload") or {}) != expected_payload
        ):
            raise ValueError("Candidate models probe 账务 operation 与 dispatch 不一致")
        sequence = event.get("sequence")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence > receipt_head_sequence
        ):
            raise ValueError("Candidate models probe 账务 operation 不属于 receipt head")
        accounting_sequences.append(sequence)
        accounting_events.append(dict(event))
    if (
        accounting_sequences != sorted(accounting_sequences)
        or not accounting_sequences
        or accounting_sequences[-1] != receipt_head_sequence
    ):
        raise ValueError("Candidate models probe 账务 operation 顺序或 head 绑定非法")

    return {
        "session": session,
        "receipt": receipt,
        "dispatches": dispatches,
        "cache_epoch_before": cache_epoch_before,
        "cache_epoch_after": cache_epoch_after,
        "accounting_events": accounting_events,
        "reservation_head_sequence": reservation_head_sequence,
        "reservation_head_sha256": reservation_head_sha256,
    }


def _validated_accounting_result(
    value: Mapping[str, Any], admission: RuntimeAdmissionProtocol
) -> dict[str, Any]:
    account = dict(value)
    remaining = account.get("remaining_live_requests")
    if (
        set(account)
        != {
            "operation_id",
            "status",
            "head_sequence",
            "head_sha256",
            "remaining_live_requests",
        }
        or not isinstance(account.get("operation_id"), str)
        or not account["operation_id"]
        or account.get("status") not in {"appended", "duplicate"}
        or isinstance(account.get("head_sequence"), bool)
        or not isinstance(account.get("head_sequence"), int)
        or account["head_sequence"] < 0
        or not isinstance(account.get("head_sha256"), str)
        or not SHA256_RE.fullmatch(account["head_sha256"])
        or account["head_sequence"] != admission.head_sequence
        or account["head_sha256"] != admission.head_sha256
        or remaining != admission.remaining_live_requests
        or (
            remaining is not None
            and (
                isinstance(remaining, bool)
                or not isinstance(remaining, int)
                or remaining < 0
            )
        )
    ):
        raise ValueError("Candidate models probe 总账回执非法")
    return account


def ensure_models_probe(
    campaign_dir: Path,
    *,
    campaign_id: str,
    candidate_id: str,
    image_id: str,
    build_receipt_sha256: str,
    static_receipt_digest: str,
    target_version: str,
    configuration: Mapping[str, Any],
    admission: RuntimeAdmissionProtocol,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    max_dispatches: int = DEFAULT_MAX_DISPATCHES,
    runner: CommandRunner | None = None,
    dispatch: Dispatch | None = None,
    restart: Restart | None = None,
    crash_hook: CrashHook | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """确保受控 probe 已逐 dispatch 入账，并以重启证明不会预热 A15 缓存。"""

    _safe_id(campaign_id, "campaign_id")
    _safe_id(candidate_id, "candidate_id")
    if (
        not IMAGE_ID_RE.fullmatch(image_id)
        or not SHA256_RE.fullmatch(build_receipt_sha256)
        or not SHA256_RE.fullmatch(static_receipt_digest)
    ):
        raise ValueError("Candidate models probe 身份非法")
    if not VERSION_RE.fullmatch(target_version):
        raise ValueError("Candidate models probe 目标版本非法")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 3600
        or isinstance(max_dispatches, bool)
        or not isinstance(max_dispatches, int)
        or not 1 <= max_dispatches <= 5
    ):
        raise ValueError("Candidate models probe TTL 或重试上限非法")
    codex_account_id = _positive_int(
        configuration.get("codex_account_id"), "codex_account_id"
    )
    api_key_id = _positive_int(configuration.get("api_key_id"), "api_key_id")
    current_time = now or datetime.now(timezone.utc)
    created_at_utc = _utc_text(current_time)
    runner = runner or _default_runner
    dispatch = dispatch or _default_dispatch
    if restart is None:
        restart = lambda container: _default_restart(container, runner)
    crash_hook = crash_hook or (lambda _point: None)
    root = _probe_root(campaign_dir, candidate_id)
    matched = _matching_session(
        root,
        campaign_id=campaign_id,
        candidate_id=candidate_id,
        image_id=image_id,
        build_receipt_sha256=build_receipt_sha256,
        static_receipt_digest=static_receipt_digest,
        target_version=target_version,
        codex_account_id=codex_account_id,
        api_key_id=api_key_id,
        ttl_seconds=ttl_seconds,
        max_dispatches=max_dispatches,
    )
    if matched is None:
        session_root, session = _new_session(
            root,
            campaign_id=campaign_id,
            candidate_id=candidate_id,
            image_id=image_id,
            build_receipt_sha256=build_receipt_sha256,
            static_receipt_digest=static_receipt_digest,
            target_version=target_version,
            codex_account_id=codex_account_id,
            api_key_id=api_key_id,
            ttl_seconds=ttl_seconds,
            max_dispatches=max_dispatches,
            created_at_utc=created_at_utc,
        )
    else:
        session_root, session = matched

    receipt_path = session_root / "receipt.json"
    service_container = str(configuration["service_container"])
    completed_receipt: dict[str, Any] | None = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        receipt = _validate_probe_receipt(
            _load_json(receipt_path, "Candidate models probe receipt"),
            session=session,
            service_container=service_container,
            image_id=image_id,
            current_time=current_time,
        )
        if receipt["status"] == "passed" and current_time < _parse_time(
            receipt.get("expires_at_utc"), "probe expires_at_utc"
        ):
            # 即使收据仍新鲜，也必须在下方重放每个 result 的幂等总账操作，
            # 不能让“收据存在但账务缺项”静默通过。
            completed_receipt = receipt
        else:
            # 已完成的失败 session 或已过期的成功 session 均保持只读。环境修复后
            # 允许新建 session；旧 dispatch 已经逐条入账，不能复用或覆盖。
            session_root, session = _new_session(
                root,
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                image_id=image_id,
                build_receipt_sha256=build_receipt_sha256,
                static_receipt_digest=static_receipt_digest,
                target_version=target_version,
                codex_account_id=codex_account_id,
                api_key_id=api_key_id,
                ttl_seconds=ttl_seconds,
                max_dispatches=max_dispatches,
                created_at_utc=created_at_utc,
            )
            receipt_path = session_root / "receipt.json"

    intents, results = _probe_files(session_root)
    results_by_index = {
        int(path.name.split("-", 1)[1].split(".", 1)[0]): path for path in results
    }
    intents_by_index = {
        int(path.name.split("-", 1)[1].split(".", 1)[0]): path for path in intents
    }
    if set(intents_by_index) - set(results_by_index):
        missing = sorted(set(intents_by_index) - set(results_by_index))
        raise ProbeAccountingUncertainError(
            "Candidate models probe 存在只有 intent、没有 result 的 dispatch",
            observations=[
                {
                    "check_id": "candidate-readiness.models-probe",
                    "failure_code": "dispatch-result-missing",
                }
            ],
            receipt_path=intents_by_index[missing[0]],
        )
    if set(results_by_index) - set(intents_by_index):
        raise ValueError("Candidate models probe result 缺少 intent")
    expected_indexes = set(range(1, len(results_by_index) + 1))
    if (
        len(results_by_index) != len(results)
        or len(intents_by_index) != len(intents)
        or set(results_by_index) != expected_indexes
        or len(results_by_index) > max_dispatches
    ):
        raise ValueError("Candidate models probe dispatch 序号不连续或超过上限")

    cache_before_path = session_root / "cache-epoch-before.json"
    if cache_before_path.exists():
        _cache_before_receipt, cache_epoch_before = _load_cache_epoch_receipt(
            cache_before_path,
            session=session,
            service_container=service_container,
            image_id=image_id,
            label="前置",
        )
    else:
        if results:
            raise ValueError("Candidate models probe result 缺少前置 cache epoch 收据")
        cache_epoch_before = _validate_cache_epoch(
            dict(restart(service_container)),
            service_container=service_container,
            image_id=image_id,
        )
        _write_cache_epoch_receipt(
            cache_before_path,
            session=session,
            epoch=cache_epoch_before,
        )
    cache_before_sha256 = _file_sha256(cache_before_path)
    if completed_receipt is not None and (
        completed_receipt["cache_isolation"]["cache_epoch_before_receipt_sha256"]
        != cache_before_sha256
        or completed_receipt["cache_isolation"]["restart_before_probe"]
        != cache_epoch_before
    ):
        raise ValueError("Candidate models probe 收据与前置 cache epoch 不一致")

    cache_after_path = session_root / "cache-epoch-after.json"
    cache_epoch_after: dict[str, Any] | None = None
    cache_after_sha256: str | None = None
    if cache_after_path.exists():
        _cache_after_receipt, cache_epoch_after = _load_cache_epoch_receipt(
            cache_after_path,
            session=session,
            service_container=service_container,
            image_id=image_id,
            label="后置",
        )
        cache_after_sha256 = _file_sha256(cache_after_path)
    elif completed_receipt is not None:
        raise ValueError("Candidate models probe 收据缺少后置 cache epoch 收据")
    if completed_receipt is not None and (
        completed_receipt["cache_isolation"]["cache_epoch_after_receipt_sha256"]
        != cache_after_sha256
        or completed_receipt["cache_isolation"]["cache_epoch_after"]
        != cache_epoch_after
    ):
        raise ValueError("Candidate models probe 收据与后置 cache epoch 不一致")

    accounting: list[dict[str, Any]] = []
    result_payloads: list[dict[str, Any]] = []
    request_path = _models_request_path(target_version)
    for index in sorted(results_by_index):
        intent_path = intents_by_index[index]
        result_path = results_by_index[index]
        _intent_payload, result_payload = _load_dispatch_pair(
            index=index,
            intent_path=intent_path,
            result_path=result_path,
            session=session,
            request_path=request_path,
        )
        account = _validated_accounting_result(
            admission.account_candidate_probe(
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                dispatch_id=str(result_payload["dispatch_id"]),
                identity_key=str(result_payload["identity_key"]),
                receipt_sha256=_file_sha256(result_path),
                response_status=int(result_payload["response_status"]),
                expected_head_sha256=admission.head_sha256,
            ),
            admission,
        )
        accounting.append(account)
        result_payloads.append(result_payload)

    if completed_receipt is not None:
        current_result_sha256s = [
            _file_sha256(results_by_index[index])
            for index in sorted(results_by_index)
        ]
        current_operation_ids = [str(item["operation_id"]) for item in accounting]
        if (
            completed_receipt.get("dispatch_result_sha256s")
            != current_result_sha256s
            or completed_receipt.get("accounting_operation_ids")
            != current_operation_ids
        ):
            raise ValueError("Candidate models probe 收据与 dispatch／账务操作不一致")
        return completed_receipt

    success = next(
        (
            item
            for item in result_payloads
            if item.get("models_payload_valid") is True
        ),
        None,
    )

    terminal_failure: dict[str, str] | None = None
    if cache_epoch_after is not None and success is None:
        remaining = admission.remaining_live_requests
        if len(result_payloads) == max_dispatches:
            pass
        elif remaining == 0:
            terminal_failure = {
                "check_id": "candidate-readiness.models-probe",
                "failure_code": "live-request-budget-exhausted",
            }
        else:
            raise ValueError(
                "Candidate models probe 已有后置 cache epoch，但请求尚未达到终态"
            )
    while (
        cache_epoch_after is None
        and success is None
        and len(result_payloads) < max_dispatches
    ):
        remaining = admission.remaining_live_requests
        if remaining is not None and (
            isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0
        ):
            raise ValueError("Candidate models probe 剩余请求预算非法")
        if remaining == 0:
            terminal_failure = {
                "check_id": "candidate-readiness.models-probe",
                "failure_code": "live-request-budget-exhausted",
            }
            break
        index = len(result_payloads) + 1
        dispatch_id = f"{session['session_id']}-dispatch-{index}"
        nonce = secrets.token_hex(24)
        identity_key = _probe_identity_key(
            campaign_id=campaign_id,
            candidate_id=candidate_id,
            session_digest=str(session["session_digest"]),
            dispatch_id=dispatch_id,
            nonce=nonce,
            request_path=request_path,
        )
        # 所有纯本地前提均在 intent 前取得；intent 一旦落盘，后续任何异常都按
        # “wire 是否完成不可判定”处理，避免环境错误制造无谓的不确定账务。
        api_key = _api_key(configuration, runner)
        url = (
            f"http://127.0.0.1:{_service_port(service_container, runner)}"
            f"{request_path}"
        )
        intent = {
            "schema_version": PROBE_INTENT_SCHEMA,
            "session_digest": session["session_digest"],
            "dispatch_index": index,
            "dispatch_id": dispatch_id,
            "identity_key": identity_key,
            "nonce": nonce,
            "nonce_sha256": hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            "method": "GET",
            "path": request_path,
            "created_at_utc": _utc_now(),
        }
        intent["intent_digest"] = _digest(intent)
        intent_path = session_root / f"dispatch-{index:02d}.intent.json"
        try:
            _write_once(intent_path, intent)
        except Exception:
            # link 已发布但目录 fsync 报错时，调用方不能再区分 intent 是否持久；
            # 立即按未决 dispatch 停线，不能等到下次重放才升级为账务不确定。
            if intent_path.exists():
                raise ProbeAccountingUncertainError(
                    "Candidate models probe intent 已发布但持久化结果不确定",
                    observations=[
                        {
                            "check_id": "candidate-readiness.models-probe",
                            "failure_code": "dispatch-result-missing",
                        }
                    ],
                    receipt_path=intent_path,
                ) from None
            raise
        try:
            crash_hook("after-intent-before-dispatch")
            status, body = dispatch(url, api_key, target_version, nonce)
            if (
                isinstance(status, bool)
                or not isinstance(status, int)
                or not 100 <= status <= 599
                or not isinstance(body, bytes)
                or len(body) > MAX_RESPONSE_BYTES
            ):
                raise ValueError("Candidate models probe dispatch 返回值非法")
            models_payload_valid, models_count = _models_response_fact(status, body)
            result_payload = {
                "schema_version": PROBE_RESULT_SCHEMA,
                "session_digest": session["session_digest"],
                "dispatch_index": index,
                "dispatch_id": dispatch_id,
                "identity_key": identity_key,
                "intent_sha256": _file_sha256(intent_path),
                "response_status": int(status),
                "response_bytes": len(body),
                "response_body_sha256": hashlib.sha256(body).hexdigest(),
                "models_payload_valid": models_payload_valid,
                "models_count": models_count,
                "completed_at_utc": _utc_now(),
            }
            result_payload["result_digest"] = _digest(result_payload)
            result_path = session_root / f"dispatch-{index:02d}.result.json"
            _write_once(result_path, result_payload)
        except Exception:
            raise ProbeAccountingUncertainError(
                "Candidate models probe dispatch 后没有形成可提交 result",
                observations=[
                    {
                        "check_id": "candidate-readiness.models-probe",
                        "failure_code": "dispatch-result-missing",
                    }
                ],
                receipt_path=intent_path,
            ) from None
        finally:
            api_key = ""
        crash_hook("after-result-before-accounting")
        account = _validated_accounting_result(
            admission.account_candidate_probe(
                campaign_id=campaign_id,
                candidate_id=candidate_id,
                dispatch_id=dispatch_id,
                identity_key=identity_key,
                receipt_sha256=_file_sha256(result_path),
                response_status=int(status),
                expected_head_sha256=admission.head_sha256,
            ),
            admission,
        )
        accounting.append(account)
        result_payloads.append(result_payload)
        crash_hook("after-accounting-before-receipt")
        if models_payload_valid:
            success = result_payload

    if cache_epoch_after is None:
        cache_epoch_after = _validate_cache_epoch(
            dict(restart(service_container)),
            service_container=service_container,
            image_id=image_id,
        )
    if _parse_time(
        cache_epoch_after.get("started_at_utc"), "cache epoch after"
    ) <= _parse_time(
        cache_epoch_before.get("started_at_utc"), "cache epoch before"
    ):
        raise CandidateReadinessError(
            "Candidate models probe 后服务 cache epoch 未变化",
            observations=[
                {
                    "check_id": "candidate-readiness.models-cache",
                    "failure_code": "restart-not-proven",
                }
            ],
        )
    if cache_after_sha256 is None:
        _write_cache_epoch_receipt(
            cache_after_path,
            session=session,
            epoch=cache_epoch_after,
        )
        cache_after_sha256 = _file_sha256(cache_after_path)
    observed = now or datetime.now(timezone.utc)
    receipt: dict[str, Any] = {
        "schema_version": PROBE_RECEIPT_SCHEMA,
        "session_id": session["session_id"],
        "session_digest": session["session_digest"],
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "image_id": image_id,
        "build_receipt_sha256": build_receipt_sha256,
        "static_receipt_digest": static_receipt_digest,
        "target_version": target_version,
        "codex_account_id": codex_account_id,
        "api_key_id": api_key_id,
        "ttl_seconds": ttl_seconds,
        "max_dispatches": max_dispatches,
        "status": "passed" if success is not None else "failed",
        "dispatch_count": len(result_payloads),
        "dispatch_result_sha256s": [
            _file_sha256(session_root / f"dispatch-{index:02d}.result.json")
            for index in range(1, len(result_payloads) + 1)
        ],
        "accounting_operation_ids": [str(item["operation_id"]) for item in accounting],
        "accounting_category": ACCOUNTING_CATEGORY,
        "accounting_policy": ACCOUNTING_POLICY,
        "project_head_sequence": admission.head_sequence,
        "project_head_sha256": admission.head_sha256,
        "cache_isolation": {
            "policy": CACHE_POLICY,
            "cache_epoch_before_receipt_sha256": cache_before_sha256,
            "cache_epoch_after_receipt_sha256": cache_after_sha256,
            "restart_before_probe": cache_epoch_before,
            "restart_after_probe": True,
            "cache_epoch_after": cache_epoch_after,
        },
        "observed_at_utc": _utc_text(observed),
        "expires_at_utc": _utc_text(observed + timedelta(seconds=ttl_seconds)),
        "failure_observations": (
            []
            if success is not None
            else [
                terminal_failure
                or {
                    "check_id": "candidate-readiness.models-probe",
                    "failure_code": "no-successful-response",
                }
            ]
        ),
    }
    receipt["receipt_digest"] = _digest(receipt)
    _validate_probe_receipt(
        receipt,
        session=session,
        service_container=service_container,
        image_id=image_id,
        current_time=observed,
    )
    _write_once(receipt_path, receipt)
    crash_hook("after-receipt")
    if success is None:
        error_type = (
            ProbeBudgetExhaustedError
            if terminal_failure is not None
            else CandidateReadinessError
        )
        raise error_type(
            "Candidate models probe 没有成功响应",
            observations=receipt["failure_observations"],
            receipt_path=receipt_path,
        )
    return receipt
