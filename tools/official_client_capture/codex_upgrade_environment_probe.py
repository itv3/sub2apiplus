#!/usr/bin/env python3
"""只读采集 Vircs 环境状态，供升级恢复验收生成可信快照。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    # 允许从仓库根目录直接执行本文件。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.candidate_evidence_guard import normalize_state


PROBE_MANIFEST_SCHEMA = "codex-upgrade-environment-probe/v1"
COMMAND_TIMEOUT_SECONDS = 30
# /etc/hosts 的比较基准：行排序后再哈希，吸收 Docker 重建文件时的行顺序抖动。
HOSTS_DIGEST_MODE = "sorted_lines_sha256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
DATABASE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_STATUS_VALUES = {
    "created",
    "running",
    "paused",
    "restarting",
    "removing",
    "exited",
    "dead",
}
CONTAINER_HEALTH_VALUES = {"none", "starting", "healthy", "unhealthy"}
MOUNT_TYPE_VALUES = {"bind", "volume", "tmpfs", "npipe", "cluster"}
# 服务端托管、随正常运行必然变动的 extra 键；它们的变化不表示"环境被污染"。
#
# 前五项是配额观测。后四项是 OpenAI 训练数据授权（privacy）状态：候选采集**经由
# Sub2API 服务**发请求，服务在该路径上会重新评估并原子写回 privacy 结果
# （`service/openai_privacy_service.go` 的 `ExtraUpdates`／`mergeOpenAIPrivacyManagedExtra`），
# 于是"采集必然改 extra → 门禁必然判污染"成为死结。官方采集直连 Codex CLI、不经服务，
# 所以从未踩到，k48 的候选采集是第一次暴露。
#
# 这四个键**逐字对齐**服务端的 `openAIPrivacyManagedExtraKeys`，不用通配符：privacy 前缀
# 下只有受管键才允许被忽略，将来若新增账号级 privacy 配置项，必须显式加入而不是被
# `privacy_*` 顺带放行——排除清单只放服务端声明托管的字段，不放宽污染检测本身。
ACCOUNT_MUTABLE_EXTRA_KEY_PATTERNS = (
    "codex_primary_*",
    "codex_secondary_*",
    "codex_5h_*",
    "codex_7d_*",
    "codex_usage_updated_at",
    "privacy_mode",
    "privacy_retry_after",
    "privacy_browser_persona",
    "privacy_rollout_key",
)
STATE_FILES = {
    "service": "service-state.json",
    "containers": "containers-state.json",
    "database": "database-state.json",
    "account": "account-state.json",
    "configuration": "configuration-state.json",
}


class EnvironmentProbeError(RuntimeError):
    """环境探针无法生成可信、无秘密的规范化状态。"""


@dataclass(frozen=True)
class TableSpec:
    """允许读取的持久表及其稳定主键。"""

    name: str
    primary_key_columns: tuple[str, ...]


PROTECTED_TABLES: tuple[TableSpec, ...] = (
    TableSpec("users", ("id",)),
    TableSpec("groups", ("id",)),
    TableSpec("accounts", ("id",)),
    TableSpec("api_keys", ("id",)),
    TableSpec("account_groups", ("account_id", "group_id")),
    TableSpec("settings", ("id",)),
    TableSpec("user_subscriptions", ("id",)),
    TableSpec("proxies", ("id",)),
)
WATERMARK_TABLES: tuple[TableSpec, ...] = (
    TableSpec("usage_logs", ("id",)),
    TableSpec("ops_error_logs", ("id",)),
    TableSpec("ops_system_logs", ("id",)),
)


@dataclass(frozen=True)
class ProbeArguments:
    """探针所需的显式环境定位参数。"""

    output_dir: Path
    service_container: str
    keeper_container: str
    postgres_container: str
    redis_container: str
    capture_container: str
    account_id: int
    api_key_id: int
    phase: str


def _run_command(
    arguments: Sequence[str],
    *,
    description: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """使用参数数组执行命令；错误消息绝不回显命令输出。"""

    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise EnvironmentProbeError(f"{description}执行失败") from error
    if completed.returncode != 0 and not allow_failure:
        raise EnvironmentProbeError(
            f"{description}执行失败（退出码 {completed.returncode}）"
        )
    return completed


def _load_single_json(text: str, description: str) -> Mapping[str, Any]:
    """解析单个 JSON 对象，拒绝将原始输出写入错误消息。"""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise EnvironmentProbeError(f"{description}未返回合法 JSON") from error
    if not isinstance(value, dict):
        raise EnvironmentProbeError(f"{description}未返回 JSON 对象")
    return value


def _docker_inspect(container: str, role: str) -> Mapping[str, Any]:
    completed = _run_command(
        ["docker", "inspect", container],
        description=f"{role} 容器检查",
    )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise EnvironmentProbeError(f"{role} 容器检查未返回合法 JSON") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise EnvironmentProbeError(f"{role} 容器检查结果结构非法")
    return value[0]


def _require_string(
    value: Any,
    description: str,
    *,
    allowed: set[str] | None = None,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise EnvironmentProbeError(f"{description}不是安全字符串")
    if allowed is not None and value not in allowed:
        raise EnvironmentProbeError(f"{description}不在允许范围")
    return value


def _require_bool(
    value: Any,
    description: str,
    *,
    nullable: bool = False,
) -> bool | None:
    if value is None and nullable:
        return None
    if not isinstance(value, bool):
        raise EnvironmentProbeError(f"{description}不是布尔值")
    return value


def _require_int(value: Any, description: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise EnvironmentProbeError(f"{description}不是整数")
    return value


def _container_state(
    role: str,
    container: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """仅从 inspect 结果提取容器身份和挂载，不保留 Env、标签或命令。"""

    container_id = _require_string(document.get("Id"), f"{role} 容器 ID")
    image_id = _require_string(document.get("Image"), f"{role} 镜像 ID")
    if not CONTAINER_ID_RE.fullmatch(container_id or ""):
        raise EnvironmentProbeError(f"{role} 容器 ID 格式非法")
    if not IMAGE_ID_RE.fullmatch(image_id or ""):
        raise EnvironmentProbeError(f"{role} 镜像 ID 格式非法")

    state = document.get("State")
    if not isinstance(state, dict):
        raise EnvironmentProbeError(f"{role} 容器状态缺失")
    running = _require_bool(state.get("Running"), f"{role} 运行状态")
    status = _require_string(
        state.get("Status"),
        f"{role} 容器状态",
        allowed=CONTAINER_STATUS_VALUES,
    )
    health_value = state.get("Health")
    if health_value is None:
        health = "none"
    elif isinstance(health_value, dict):
        health = _require_string(
            health_value.get("Status"),
            f"{role} 健康状态",
            allowed=CONTAINER_HEALTH_VALUES - {"none"},
        )
    else:
        raise EnvironmentProbeError(f"{role} 健康状态结构非法")

    mounts_value = document.get("Mounts", [])
    if not isinstance(mounts_value, list):
        raise EnvironmentProbeError(f"{role} 挂载信息结构非法")
    mounts: list[dict[str, Any]] = []
    for mount in mounts_value:
        if not isinstance(mount, dict):
            raise EnvironmentProbeError(f"{role} 挂载项结构非法")
        mount_type = _require_string(
            mount.get("Type"),
            f"{role} 挂载类型",
            allowed=MOUNT_TYPE_VALUES,
        )
        destination = _require_string(
            mount.get("Destination"),
            f"{role} 挂载目标",
        )
        if not destination or not destination.startswith("/"):
            raise EnvironmentProbeError(f"{role} 挂载目标必须是绝对路径")
        source = mount.get("Source", "")
        if not isinstance(source, str):
            raise EnvironmentProbeError(f"{role} 挂载来源结构非法")
        read_write = _require_bool(mount.get("RW"), f"{role} 挂载读写状态")
        mounts.append(
            {
                "destination": destination,
                "read_only": not bool(read_write),
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "type": mount_type,
            }
        )
    mounts.sort(key=lambda item: (item["destination"], item["type"]))
    # 不记录容器实例 ID：A11 的 Live attestation 注入必须 compose 重建服务容器，
    # 重建必然换实例 ID，而恢复检查按 byte_equal 比较，会把这种必然变化误判成
    # 环境污染。恢复要证明的是「同名容器在同一镜像、同样挂载下重新就绪」，
    # 实例 ID 不承载该语义；镜像 ID、挂载集合、运行与健康状态仍然逐字比较。
    return {
        "container": container,
        "health": health,
        "image": image_id,
        "mounts": mounts,
        "role": role,
        "running": running,
        "status": status,
    }


def _database_identity(postgres_document: Mapping[str, Any]) -> tuple[str, str]:
    """从内存中的容器环境定位数据库；不返回或保存密码。"""

    config = postgres_document.get("Config")
    if not isinstance(config, dict):
        raise EnvironmentProbeError("PostgreSQL 容器配置缺失")
    environment = config.get("Env", [])
    if not isinstance(environment, list):
        raise EnvironmentProbeError("PostgreSQL 容器环境结构非法")
    values: dict[str, str] = {}
    for item in environment:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in {"POSTGRES_USER", "POSTGRES_DB"}:
            values[key] = value
    user = values.get("POSTGRES_USER", "sub2api")
    database = values.get("POSTGRES_DB", "sub2api")
    if not DATABASE_IDENTIFIER_RE.fullmatch(user):
        raise EnvironmentProbeError("PostgreSQL 用户名格式不受支持")
    if not DATABASE_IDENTIFIER_RE.fullmatch(database):
        raise EnvironmentProbeError("PostgreSQL 数据库名格式不受支持")
    return user, database


def _psql_json(
    postgres_container: str,
    database_user: str,
    database_name: str,
    sql: str,
    description: str,
) -> Mapping[str, Any]:
    completed = _run_command(
        [
            "docker",
            "exec",
            postgres_container,
            "psql",
            "-X",
            "-qAt",
            "--no-psqlrc",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            database_user,
            "-d",
            database_name,
            "-c",
            sql,
        ],
        description=description,
    )
    return _load_single_json(completed.stdout.strip(), description)


def _table_exists(
    postgres_container: str,
    database_user: str,
    database_name: str,
    table: str,
) -> bool:
    result = _psql_json(
        postgres_container,
        database_user,
        database_name,
        (
            "SELECT json_build_object("
            f"'exists', to_regclass('public.{table}') IS NOT NULL);"
        ),
        f"{table} 表存在性检查",
    )
    return bool(_require_bool(result.get("exists"), f"{table} 表存在性"))


def _protected_table_state(
    postgres_container: str,
    database_user: str,
    database_name: str,
    spec: TableSpec,
) -> dict[str, Any]:
    if not _table_exists(
        postgres_container,
        database_user,
        database_name,
        spec.name,
    ):
        return {
            "exists": False,
            "name": spec.name,
            "primary_key_columns": list(spec.primary_key_columns),
            "primary_key_fingerprints": [],
            "row_count": None,
        }

    primary_key_values = ", ".join(spec.primary_key_columns)
    primary_key_order = ", ".join(spec.primary_key_columns)
    result = _psql_json(
        postgres_container,
        database_user,
        database_name,
        (
            "SELECT json_build_object("
            "'row_count', count(*), "
            "'primary_key_rows', "
            "COALESCE(json_agg("
            f"json_build_array({primary_key_values}) "
            f"ORDER BY {primary_key_order}), '[]'::json)) "
            f"FROM public.{spec.name};"
        ),
        f"{spec.name} 保护状态检查",
    )
    row_count = _require_int(result.get("row_count"), f"{spec.name} 行数")
    primary_key_rows = result.get("primary_key_rows")
    if (
        not isinstance(primary_key_rows, list)
        or len(primary_key_rows) != row_count
    ):
        raise EnvironmentProbeError(f"{spec.name} 主键行结构非法")
    fingerprints: list[str] = []
    for index, primary_key_row in enumerate(primary_key_rows):
        if (
            not isinstance(primary_key_row, list)
            or len(primary_key_row) != len(spec.primary_key_columns)
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, str))
                or (
                    isinstance(value, str)
                    and (
                        not value
                        or len(value) > 4096
                        or any(ord(character) < 0x20 for character in value)
                    )
                )
                for value in primary_key_row
            )
        ):
            raise EnvironmentProbeError(
                f"{spec.name} 第 {index} 个主键结构非法"
            )
        canonical = json.dumps(
            primary_key_row,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprints.append(hashlib.sha256(canonical).hexdigest())
    fingerprints.sort()
    if len(fingerprints) != len(set(fingerprints)):
        raise EnvironmentProbeError(f"{spec.name} 主键指纹重复")
    return {
        "exists": True,
        "name": spec.name,
        "primary_key_columns": list(spec.primary_key_columns),
        "primary_key_fingerprints": fingerprints,
        "row_count": row_count,
    }


def _watermark_table_state(
    postgres_container: str,
    database_user: str,
    database_name: str,
    spec: TableSpec,
) -> dict[str, Any]:
    if not _table_exists(
        postgres_container,
        database_user,
        database_name,
        spec.name,
    ):
        return {
            "exists": False,
            "max_id": None,
            "name": spec.name,
            "row_count": None,
        }
    result = _psql_json(
        postgres_container,
        database_user,
        database_name,
        (
            "SELECT json_build_object("
            "'row_count', count(*), 'max_id', max(id)) "
            f"FROM public.{spec.name};"
        ),
        f"{spec.name} 水位检查",
    )
    return {
        "exists": True,
        "max_id": _require_int(
            result.get("max_id"),
            f"{spec.name} 最大 ID",
            nullable=True,
        ),
        "name": spec.name,
        "row_count": _require_int(result.get("row_count"), f"{spec.name} 行数"),
    }


def _database_state(
    arguments: ProbeArguments,
    database_user: str,
    database_name: str,
) -> dict[str, Any]:
    protected = [
        _protected_table_state(
            arguments.postgres_container,
            database_user,
            database_name,
            spec,
        )
        for spec in sorted(PROTECTED_TABLES, key=lambda item: item.name)
    ]
    watermarks = [
        _watermark_table_state(
            arguments.postgres_container,
            database_user,
            database_name,
            spec,
        )
        for spec in sorted(WATERMARK_TABLES, key=lambda item: item.name)
    ]
    return {
        "append_only_watermarks": watermarks,
        "comparison_policy": {
            "protected_table_rule": "before_primary_key_fingerprints_subset",
            "watermark_rule": "row_count_and_max_id_non_decreasing",
        },
        "probe_kind": "database",
        "protected_tables": protected,
    }


def _optional_safe_text(value: Any, description: str) -> str | None:
    return _require_string(value, description, nullable=True)


def _account_state(
    arguments: ProbeArguments,
    database_user: str,
    database_name: str,
) -> dict[str, Any]:
    account_sql = f"""
WITH selected AS (
    SELECT to_jsonb(account_row) AS row_data
    FROM public.accounts AS account_row
    WHERE account_row.id = {arguments.account_id}
)
SELECT json_build_object(
    'exists', EXISTS(SELECT 1 FROM selected),
    'id', (SELECT (row_data->>'id')::bigint FROM selected),
    'platform', (SELECT row_data->>'platform' FROM selected),
    'type', (SELECT row_data->>'type' FROM selected),
    'status', (SELECT row_data->>'status' FROM selected),
    'schedulable', (SELECT (row_data->>'schedulable')::boolean FROM selected),
    'proxy_id', (SELECT (row_data->>'proxy_id')::bigint FROM selected),
    'proxy_fallback_origin_id',
        (SELECT (row_data->>'proxy_fallback_origin_id')::bigint FROM selected),
    'parent_account_id',
        (SELECT (row_data->>'parent_account_id')::bigint FROM selected),
    'credentials_present',
        (SELECT COALESCE(
            jsonb_typeof(row_data->'credentials') = 'object'
            AND row_data->'credentials' <> '{{}}'::jsonb,
            false
        ) FROM selected),
    'extra_digest',
        (SELECT md5(COALESCE((
            SELECT jsonb_object_agg(extra_entry.key, extra_entry.value)
            FROM jsonb_each(COALESCE(row_data->'extra', '{{}}'::jsonb))
                AS extra_entry(key, value)
            WHERE NOT (
                extra_entry.key LIKE 'codex_primary_%'
                OR extra_entry.key LIKE 'codex_secondary_%'
                OR extra_entry.key LIKE 'codex_5h_%'
                OR extra_entry.key LIKE 'codex_7d_%'
                OR extra_entry.key = 'codex_usage_updated_at'
                OR extra_entry.key = 'privacy_mode'
                OR extra_entry.key = 'privacy_retry_after'
                OR extra_entry.key = 'privacy_browser_persona'
                OR extra_entry.key = 'privacy_rollout_key'
            )
        ), '{{}}'::jsonb)::text) FROM selected)
);
"""
    key_sql = f"""
WITH selected AS (
    SELECT to_jsonb(key_row) AS row_data
    FROM public.api_keys AS key_row
    WHERE key_row.id = {arguments.api_key_id}
)
SELECT json_build_object(
    'exists', EXISTS(SELECT 1 FROM selected),
    'id', (SELECT (row_data->>'id')::bigint FROM selected),
    'user_id', (SELECT (row_data->>'user_id')::bigint FROM selected),
    'group_id', (SELECT (row_data->>'group_id')::bigint FROM selected),
    'status', (SELECT row_data->>'status' FROM selected)
);
"""
    account = _psql_json(
        arguments.postgres_container,
        database_user,
        database_name,
        account_sql,
        "目标账号状态检查",
    )
    access_key = _psql_json(
        arguments.postgres_container,
        database_user,
        database_name,
        key_sql,
        "目标访问 Key 状态检查",
    )
    account_exists = _require_bool(account.get("exists"), "目标账号存在性")
    key_exists = _require_bool(access_key.get("exists"), "目标访问 Key 存在性")
    extra_digest = _optional_safe_text(account.get("extra_digest"), "账号 extra 摘要")
    if extra_digest is not None and not MD5_RE.fullmatch(extra_digest):
        raise EnvironmentProbeError("账号 extra 摘要格式非法")
    return {
        "probe_kind": "account",
        "selected_account": {
            "credentials_present": _require_bool(
                account.get("credentials_present"),
                "账号凭据存在性",
                nullable=not bool(account_exists),
            ),
            "exists": account_exists,
            "extra_digest": extra_digest,
            "extra_digest_algorithm": "md5",
            "extra_digest_scope": "stable_extra_v1",
            "extra_ignored_key_patterns": list(ACCOUNT_MUTABLE_EXTRA_KEY_PATTERNS),
            "id": _require_int(
                account.get("id"),
                "账号 ID",
                nullable=not bool(account_exists),
            ),
            "parent_account_id": _require_int(
                account.get("parent_account_id"),
                "父账号 ID",
                nullable=True,
            ),
            "platform": _optional_safe_text(account.get("platform"), "账号平台"),
            "proxy_fallback_origin_id": _require_int(
                account.get("proxy_fallback_origin_id"),
                "账号代理回退 ID",
                nullable=True,
            ),
            "proxy_id": _require_int(
                account.get("proxy_id"),
                "账号代理 ID",
                nullable=True,
            ),
            "schedulable": _require_bool(
                account.get("schedulable"),
                "账号可调度状态",
                nullable=True,
            ),
            "status": _optional_safe_text(account.get("status"), "账号状态"),
            "type": _optional_safe_text(account.get("type"), "账号类型"),
        },
        "selected_access_key": {
            "exists": key_exists,
            "group_id": _require_int(
                access_key.get("group_id"),
                "访问 Key 分组 ID",
                nullable=True,
            ),
            "id": _require_int(
                access_key.get("id"),
                "访问 Key ID",
                nullable=not bool(key_exists),
            ),
            "status": _optional_safe_text(
                access_key.get("status"),
                "访问 Key 状态",
            ),
            "user_id": _require_int(
                access_key.get("user_id"),
                "访问 Key 用户 ID",
                nullable=not bool(key_exists),
            ),
        },
    }


def _service_state(
    arguments: ProbeArguments,
    containers: Mapping[str, Mapping[str, Any]],
    database_user: str,
    database_name: str,
) -> dict[str, Any]:
    _run_command(
        [
            "docker",
            "exec",
            arguments.postgres_container,
            "pg_isready",
            "-U",
            database_user,
            "-d",
            database_name,
        ],
        description="PostgreSQL 就绪检查",
    )
    redis = _run_command(
        [
            "docker",
            "exec",
            arguments.redis_container,
            "redis-cli",
            "--raw",
            "PING",
        ],
        description="Redis 就绪检查",
    )
    if redis.stdout.strip().upper() != "PONG":
        raise EnvironmentProbeError("Redis 就绪检查返回值非法")
    return {
        "dependencies": {
            "postgres_ready": True,
            "redis_ready": True,
        },
        "probe_kind": "service",
        "processes": [
            {
                "health": containers[role]["health"],
                "role": role,
                "running": containers[role]["running"],
                "status": containers[role]["status"],
            }
            for role in ("service", "keeper")
        ],
    }


def _container_file_sha256(container: str, path: str, role: str) -> str | None:
    exists = _run_command(
        ["docker", "exec", container, "test", "-f", path],
        description=f"{role} 配置文件存在性检查",
        allow_failure=True,
    )
    if exists.returncode != 0:
        return None
    completed = _run_command(
        ["docker", "exec", container, "sha256sum", path],
        description=f"{role} 配置文件摘要检查",
    )
    digest = completed.stdout.strip().split(maxsplit=1)[0] if completed.stdout else ""
    if not SHA256_RE.fullmatch(digest):
        raise EnvironmentProbeError(f"{role} 配置文件摘要格式非法")
    return digest


def _container_hosts_digest(container: str, role: str, container_id: str) -> str | None:
    """按行排序后计算容器 /etc/hosts 摘要。

    Docker 每次启动容器都会重建 /etc/hosts，多网络容器的地址行顺序取决于运行时
    遍历网络的顺序，同一环境连续两次重启即可得到不同字节序列。原始字节摘要会把
    这种顺序抖动误判成环境漂移，因此恢复比较基准改为行排序后的摘要：条目的新增、
    删除、地址或主机名改写仍然改变摘要，只有纯顺序变化被吸收。

    同理还要剔除 Docker 为容器自身写入的 `<容器 IP> <容器 ID 前 12 位>` 行：
    A11 的 Live attestation 注入必须 compose 重建服务容器，重建后实例 ID 与容器
    网段地址都会变，这两行随之改写。它们由 Docker 生成、不表达任何采集副作用，
    保留就会把必然发生的重建误判成环境污染。人为写入的劫持行（chatgpt.com 等）
    主机名不等于容器 ID，仍然会改变摘要。
    """

    exists = _run_command(
        ["docker", "exec", container, "test", "-f", "/etc/hosts"],
        description=f"{role} hosts 存在性检查",
        allow_failure=True,
    )
    if exists.returncode != 0:
        return None
    self_names = _container_self_hostnames(container_id)
    completed = _run_command(
        ["docker", "exec", container, "cat", "/etc/hosts"],
        description=f"{role} hosts 内容读取",
    )
    retained = [
        line
        for line in completed.stdout.splitlines()
        if not _is_container_self_reference(line, self_names)
    ]
    payload = "".join(f"{line}\n" for line in sorted(retained))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _container_self_hostnames(container_id: str) -> frozenset[str]:
    """容器自引用行使用的主机名：Docker 默认写入实例 ID 的前 12 位。"""

    identifier = container_id.strip()
    if not CONTAINER_ID_RE.fullmatch(identifier):
        raise EnvironmentProbeError("容器 ID 格式非法，无法识别 hosts 自引用行")
    return frozenset({identifier, identifier[:12]})


def _is_container_self_reference(line: str, self_names: frozenset[str]) -> bool:
    """判断该 hosts 行是否只是 Docker 为容器自身写的地址映射。"""

    fields = line.split()
    if len(fields) != 2:
        # 自引用行恒为「地址 + 单个主机名」；多主机名行一律保留比较。
        return False
    return fields[1] in self_names


def _configuration_state(
    arguments: ProbeArguments,
    container_ids: Mapping[str, str],
) -> dict[str, Any]:
    roles = (
        ("service", arguments.service_container),
        ("keeper", arguments.keeper_container),
        ("capture", arguments.capture_container),
    )
    records: list[dict[str, Any]] = []
    for role, container in roles:
        hosts_digest = _container_hosts_digest(container, role, container_ids[role])
        if hosts_digest is None:
            raise EnvironmentProbeError(f"{role} 容器缺少 /etc/hosts")
        ca_digest = _container_file_sha256(
            container,
            "/etc/ssl/certs/ca-certificates.crt",
            role,
        )
        records.append(
            {
                "ca_bundle_exists": ca_digest is not None,
                "ca_bundle_sha256": ca_digest,
                "container": container,
                "hosts_digest_mode": HOSTS_DIGEST_MODE,
                "hosts_sha256": hosts_digest,
                "role": role,
            }
        )
    return {
        "probe_kind": "configuration",
        "records": records,
    }


def _reject_symlink_components(path: Path) -> None:
    """拒绝输出目录自身为符号链接；系统级父目录可由平台合法映射。"""

    if path.exists() and path.is_symlink():
        raise EnvironmentProbeError(f"输出路径包含符号链接：{path}")


def _validate_output_targets(output_dir: Path) -> None:
    _reject_symlink_components(output_dir)
    if output_dir.exists() and not output_dir.is_dir():
        raise EnvironmentProbeError("输出路径不是目录")
    for name in (*STATE_FILES.values(), "probe-manifest.json"):
        if (output_dir / name).exists() or (output_dir / name).is_symlink():
            raise EnvironmentProbeError(f"输出文件已存在，拒绝覆盖：{name}")


def _prepare_output_directory(output_dir: Path) -> None:
    _validate_output_targets(output_dir)
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_dir.chmod(0o700)
    _validate_output_targets(output_dir)


def _exclusive_write(path: Path, payload: bytes) -> None:
    """以 0600 临时文件加独占硬链接原子落盘，永不覆盖已有结果。"""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise EnvironmentProbeError(f"输出文件已存在，拒绝覆盖：{path.name}") from error
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _snapshot_binding(path: Path, payload: bytes, kind: str) -> dict[str, Any]:
    comparison: dict[str, Any]
    if kind == "database":
        comparison = {
            "mode": "protected_plus_watermarks",
            "protected_table_path": "state.protected_tables",
            "protected_table_rule": "before_primary_key_fingerprints_subset",
            "watermark_path": "state.append_only_watermarks",
            "watermark_rule": "row_count_and_max_id_non_decreasing",
        }
    else:
        comparison = {"mode": "byte_equal"}
    return {
        "bytes": len(payload),
        "comparison": comparison,
        "kind": kind,
        "path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def run_probe(arguments: ProbeArguments) -> dict[str, Any]:
    """完成全量只读采集，并在全部命令成功后独占写入五份快照。"""

    if arguments.account_id <= 0 or arguments.api_key_id <= 0:
        raise EnvironmentProbeError("账号 ID 与访问 Key ID 必须为正整数")
    if arguments.phase not in {"before", "after"}:
        raise EnvironmentProbeError("phase 只能是 before 或 after")
    _validate_output_targets(arguments.output_dir)

    container_names = {
        "service": arguments.service_container,
        "keeper": arguments.keeper_container,
        "postgres": arguments.postgres_container,
        "redis": arguments.redis_container,
        "capture": arguments.capture_container,
    }
    inspected = {
        role: _docker_inspect(container, role)
        for role, container in container_names.items()
    }
    container_records = {
        role: _container_state(role, container_names[role], inspected[role])
        for role in container_names
    }
    container_ids = {
        role: str(inspected[role].get("Id", "")) for role in container_names
    }
    database_user, database_name = _database_identity(inspected["postgres"])

    states = {
        "service": _service_state(
            arguments,
            container_records,
            database_user,
            database_name,
        ),
        "containers": {
            "containers": [
                container_records[role] for role in sorted(container_records)
            ],
            "probe_kind": "containers",
        },
        "database": _database_state(arguments, database_user, database_name),
        "account": _account_state(arguments, database_user, database_name),
        "configuration": _configuration_state(arguments, container_ids),
    }
    payloads = {kind: normalize_state(state) for kind, state in states.items()}

    _prepare_output_directory(arguments.output_dir)
    bindings: list[dict[str, Any]] = []
    for kind in ("service", "containers", "database", "account", "configuration"):
        path = arguments.output_dir / STATE_FILES[kind]
        payload = payloads[kind]
        _exclusive_write(path, payload)
        bindings.append(_snapshot_binding(path, payload, kind))

    # 在全部状态采集完成后记录 UTC 时间，作为本次探针事实的可信时间上界。
    observed_at_utc = datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    manifest = {
        "observed_at_utc": observed_at_utc,
        "phase": arguments.phase,
        "schema_version": PROBE_MANIFEST_SCHEMA,
        "selected_account_id": arguments.account_id,
        "selected_key_id": arguments.api_key_id,
        "snapshots": bindings,
        "targets": {
            role: container_names[role]
            for role in ("service", "keeper", "postgres", "redis", "capture")
        },
    }
    manifest_payload = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _exclusive_write(arguments.output_dir / "probe-manifest.json", manifest_payload)
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读生成 Vircs 升级前后环境规范化状态",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--service-container", required=True)
    parser.add_argument("--keeper-container", required=True)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--redis-container", required=True)
    parser.add_argument("--capture-container", required=True)
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--api-key-id", type=int, required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parsed = _build_parser().parse_args(argv)
    arguments = ProbeArguments(
        output_dir=parsed.output_dir,
        service_container=parsed.service_container,
        keeper_container=parsed.keeper_container,
        postgres_container=parsed.postgres_container,
        redis_container=parsed.redis_container,
        capture_container=parsed.capture_container,
        account_id=parsed.account_id,
        api_key_id=parsed.api_key_id,
        phase=parsed.phase,
    )
    try:
        manifest = run_probe(arguments)
    except EnvironmentProbeError as error:
        print(f"环境探针失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "manifest": str(arguments.output_dir / "probe-manifest.json"),
                "phase": manifest["phase"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
