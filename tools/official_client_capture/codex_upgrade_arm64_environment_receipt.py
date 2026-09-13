#!/usr/bin/env python3
"""生成并重放 Codex 升级所需的 ARM64 网络与磁盘硬门禁收据。"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools.official_client_capture import incremental_recovery


FACTS_SCHEMA = "codex-upgrade-arm64-environment-facts/v1"
RECEIPT_SCHEMA = "codex-upgrade-arm64-environment-receipt/v1"
PRODUCER_SCHEMA = "codex-upgrade-arm64-environment-producer/v1"
PRODUCER_VERSION = "4"
PRODUCER_TOOL_RELATIVE = (
    "tools/official_client_capture/codex_upgrade_arm64_environment_receipt.py"
)
# v1 只用于重放已封存历史收据；新 facts 和新收据仍只能由 v2 生成。
LEGACY_REPLAY_PRODUCERS = {
    "1": "97b96fcd9e341dc7ecff4c0359b12723dae747ec2f4bc9c0138a5bf8f6769d15",
}
# 受管的历史 v2 producer 摘要。绝对根目录只是当次工作树坐标，重放时只
# 信任规范相对坐标和精确字节摘要；未知摘要仍必须失败关闭。
REGISTERED_REPLAY_PRODUCER_HASHES = {
    "1": frozenset({LEGACY_REPLAY_PRODUCERS["1"]}),
    # 28f15/7633/a62a 为已登记的历史后继；317e 为本次 heartbeat 标签修复前
    # 已生成 P0／attempt 收据的受管版本。它们都只允许重放，不允许生成新 facts。
    "2": frozenset(
        {
            "687e28781d5e6300e829f83ca603a916e1388fa7df0a0702d777c5f66e7a139f",
            "28f15f366b9fc1761179256f5cb7d06f7f45e76ddf467383565469d1965a8053",
            "317ea2c842cf32afabc919583b08a5dfd11f6f4aa97103cc0805fa178d3770e4",
            "7633ad1f101a8320126fb6c76417362bf8571faed9f14e5dcf20ec616a593048",
            "a62a269e5e4cb0e64aac21e5223ddbde8b884ecbe383b405c560e3c6ebcea527",
        }
    ),
    # v3 首次把 wg1 持久配置、运行时 MTU 和配置摘要纳入收据。切换到
    # BWG 后，旧 DMIT 合同只能按原字段和原出口只读重放，不能生成新事实。
    "3": frozenset(
        {
            "36f9d717f847cd71ff66b569cf3daa5ce6c9a2f419f204db7f3c39a4beb4a1ac",
        }
    ),
}
PUBLIC_EGRESS_URL = "https://api.ipify.org"
EXPECTED_EGRESS_PROVIDER = "BWG"
EXPECTED_PUBLIC_EGRESS = "144.34.230.210"
LEGACY_DMIT_PUBLIC_EGRESS = "179.255.100.158"
WIREGUARD_INTERFACE = "wg1"
WIREGUARD_CONFIG = Path("/etc/wireguard/wg1.conf")
# BWG 当前受管 wg1 MTU 已独立核验并冻结为 1420。ARM64 的持久配置和
# 运行时值必须同时与该对端值一致，不能只检查 IP、rule 和 route。
EXPECTED_WG1_MTU = 1420
LEGACY_DMIT_WG1_MTU = 1420
LEGACY_NETWORK_CONTRACT_SHA256 = (
    "9e342c764883ee1107b998ef7a26650402ff4e8d82207926f518528f83dc4ec8"
)
LEGACY_V3_NETWORK_CONTRACT_SHA256 = (
    "551c8faf877eb4889edd7f4426c007110107b2d657f22b59b70785869902736b"
)
ROOT_MAX_USED_PERCENT = 69
ROOT_MIN_AVAILABLE_BYTES = 30 * 1024 * 1024 * 1024
PHASES = frozenset(
    {
        "p0",
        "attempt_before",
        "attempt_after",
        "kilo_before",
        "kilo_after",
        "gate_before",
        "gate_after",
        "canary_before",
        "canary_after",
        "deployment_before",
        "deployment_after",
    }
)
CONTAINER_CONTRACTS: dict[str, dict[str, str]] = {
    "capture-cli": {
        "network": "capture-network",
        "ipv4_address": "172.30.0.10",
        "gateway": "172.30.0.1",
    },
    "sub2apiplus": {
        "network": "proxy-network",
        "ipv4_address": "172.25.0.3",
        "gateway": "172.25.0.1",
    },
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEARTBEAT_OPERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
MAX_JSON_BYTES = 4 * 1024 * 1024


class Arm64EnvironmentReceiptError(ValueError):
    """ARM64 硬门禁事实不完整、发生漂移或无法重放。"""


# 一个 ARM64 收据包含多个 docker inspect/exec 命令；用作用域确保它们共享
# attempt 的同一条 deadline，而不是每个命令各自重新获得固定 30 秒。
_ACTIVE_DEADLINE: incremental_recovery.WallClockDeadline | None = None
_ACTIVE_HEARTBEAT: Any | None = None


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Arm64EnvironmentReceiptError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise Arm64EnvironmentReceiptError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _current_producer() -> dict[str, str]:
    producer = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(producer),
        "tool_sha256": _sha256_file(producer),
        "version": PRODUCER_VERSION,
    }


def _producer_tool_coordinate(value: Any) -> tuple[str, ...] | None:
    """提取 producer 的规范相对坐标，忽略可迁移的工作树绝对根。"""

    if not isinstance(value, str) or not value or not value.startswith("/"):
        return None
    try:
        parsed = PurePosixPath(value)
        parts = parsed.parts
        relative = tuple(PurePosixPath(PRODUCER_TOOL_RELATIVE).parts)
    except (TypeError, ValueError):
        return None
    if (
        str(parsed) != value
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) < len(relative)
        or parts[-len(relative) :] != relative
    ):
        return None
    return relative


def _validated_producer_version(
    value: Any,
    *,
    allow_legacy_replay: bool,
) -> str:
    """验证当前 producer，或只读承接已登记的历史 producer。"""

    producer = _expect(
        value,
        {"schema_version", "tool", "tool_sha256", "version"},
        "producer",
    )
    current = _current_producer()
    # 同一字节 producer 在不同工作树根目录生成的收据可以直接承接；路径
    # 只需落在相同的受管相对坐标，不能作为身份本身。
    if (
        producer.get("schema_version") == current["schema_version"]
        and producer.get("version") == current["version"]
        and producer.get("tool_sha256") == current["tool_sha256"]
        and _producer_tool_coordinate(producer.get("tool")) is not None
        and _producer_tool_coordinate(current.get("tool")) is not None
    ):
        return PRODUCER_VERSION
    version = producer.get("version")
    if (
        allow_legacy_replay
        and producer.get("schema_version") == PRODUCER_SCHEMA
        and isinstance(version, str)
        and _producer_tool_coordinate(producer.get("tool")) is not None
        and _producer_tool_coordinate(current.get("tool")) is not None
        and producer.get("tool_sha256")
        in REGISTERED_REPLAY_PRODUCER_HASHES.get(version, frozenset())
    ):
        return version
    raise Arm64EnvironmentReceiptError("ARM64 事实采集器身份漂移")


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise Arm64EnvironmentReceiptError(f"{label}不是安全标识")
    return value


def _rfc3339(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise Arm64EnvironmentReceiptError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Arm64EnvironmentReceiptError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise Arm64EnvironmentReceiptError(f"{label}缺少时区")
    return value


def _private_root(root: Path) -> Path:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise Arm64EnvironmentReceiptError("evidence root 必须是现有非符号链接绝对目录")
    resolved = root.resolve(strict=True)
    if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise Arm64EnvironmentReceiptError("evidence root 权限必须是 0700")
    return resolved


def _relative(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise Arm64EnvironmentReceiptError(f"{label}必须是证据根内 POSIX 相对路径")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise Arm64EnvironmentReceiptError(f"{label}路径不规范")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise Arm64EnvironmentReceiptError(f"{label}路径包含符号链接")
    try:
        current.resolve(strict=current.exists()).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise Arm64EnvironmentReceiptError(f"{label}越过 evidence root") from error
    return current


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise Arm64EnvironmentReceiptError(f"{label}不是可信普通文件")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise Arm64EnvironmentReceiptError(f"{label}权限必须是 0600")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise Arm64EnvironmentReceiptError(f"{label}大小非法")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Arm64EnvironmentReceiptError(f"{label}不是合法 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise Arm64EnvironmentReceiptError(f"{label}顶层必须是对象")
    return payload, raw


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise Arm64EnvironmentReceiptError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise Arm64EnvironmentReceiptError("输出父目录必须是 0700 非符号链接目录")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def contract_sha256() -> str:
    """返回文档固定网络与资源门禁的稳定摘要。"""

    return _sha256_bytes(
        _canonical(
            {
                "containers": CONTAINER_CONTRACTS,
                "egress_provider": EXPECTED_EGRESS_PROVIDER,
                "public_egress_ip": EXPECTED_PUBLIC_EGRESS,
                "public_egress_url": PUBLIC_EGRESS_URL,
                "wireguard": {
                    "interface": WIREGUARD_INTERFACE,
                    "expected_mtu": EXPECTED_WG1_MTU,
                },
                "root_max_used_percent": ROOT_MAX_USED_PERCENT,
                "root_min_available_bytes": ROOT_MIN_AVAILABLE_BYTES,
                "architecture": "linux/arm64",
            }
        )
    )


def _run(
    argv: list[str],
    label: str,
    timeout: int = 30,
    *,
    operation: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> bytes:
    # 中文 label 只用于错误诊断；heartbeat operation 属于机器审计字段，
    # 必须使用固定 ASCII 标签，不能把诊断文本或命令参数直接写入心跳。
    if not HEARTBEAT_OPERATION_RE.fullmatch(operation):
        raise Arm64EnvironmentReceiptError("ARM64 探针 heartbeat operation 非法")
    active_deadline = deadline if deadline is not None else _ACTIVE_DEADLINE
    active_heartbeat = heartbeat if heartbeat is not None else _ACTIVE_HEARTBEAT
    try:
        if active_deadline is not None:
            completed = incremental_recovery.run_bounded_subprocess(
                argv,
                timeout=timeout,
                deadline=active_deadline,
                operation=operation,
                check=False,
                capture_output=True,
                heartbeat=active_heartbeat,
            )
        else:
            completed = subprocess.run(
                argv,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise Arm64EnvironmentReceiptError(f"{label}执行失败") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[:300].strip()
        raise Arm64EnvironmentReceiptError(f"{label}失败：{message}")
    return completed.stdout


def _parse_default_route(raw: bytes, container: str) -> dict[str, str]:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as error:
        raise Arm64EnvironmentReceiptError(f"{container} 路由表编码非法") from error
    routes: list[dict[str, str]] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000":
            continue
        try:
            gateway_bytes = bytes.fromhex(fields[2])
            gateway = str(ipaddress.IPv4Address(gateway_bytes[::-1]))
            flags = int(fields[3], 16)
        except (ValueError, ipaddress.AddressValueError) as error:
            raise Arm64EnvironmentReceiptError(f"{container} 默认路由格式非法") from error
        if flags & 0x3 == 0x3:
            routes.append({"interface": fields[0], "gateway": gateway})
    if len(routes) != 1:
        raise Arm64EnvironmentReceiptError(
            f"{container} 必须且只能有一条启用网关的 IPv4 默认路由"
        )
    return routes[0]


def _container_observation(name: str) -> dict[str, Any]:
    inspect_raw = _run(
        ["docker", "inspect", name],
        f"{name} docker inspect",
        operation=f"arm64:docker-inspect:{name}",
    )
    try:
        inspected = json.loads(inspect_raw)
    except json.JSONDecodeError as error:
        raise Arm64EnvironmentReceiptError(f"{name} inspect 不是合法 JSON") from error
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise Arm64EnvironmentReceiptError(f"{name} inspect 结果格式非法")
    item = inspected[0]
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    network_settings = (
        item.get("NetworkSettings")
        if isinstance(item.get("NetworkSettings"), dict)
        else {}
    )
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings.get("Networks"), dict)
        else {}
    )
    container_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", ""))
    if state.get("Running") is not True:
        raise Arm64EnvironmentReceiptError(f"{name} 当前未运行")
    if not CONTAINER_ID_RE.fullmatch(container_id) or not IMAGE_ID_RE.fullmatch(image_id):
        raise Arm64EnvironmentReceiptError(f"{name} 容器或镜像身份非法")
    normalized_networks = [
        {
            "name": network_name,
            "network_id": str(network.get("NetworkID", "")),
            "endpoint_id": str(network.get("EndpointID", "")),
            "ipv4_address": str(network.get("IPAddress", "")),
            "gateway": str(network.get("Gateway", "")),
        }
        for network_name, network in sorted(networks.items())
        if isinstance(network, dict)
    ]
    expected = CONTAINER_CONTRACTS[name]
    selected = next(
        (network for network in normalized_networks if network["name"] == expected["network"]),
        None,
    )
    if selected is None:
        raise Arm64EnvironmentReceiptError(f"{name} 缺少固定网络 {expected['network']}")

    route_raw = _run(
        ["docker", "exec", name, "cat", "/proc/net/route"],
        f"{name} 默认路由读取",
        operation=f"arm64:default-route:{name}",
    )
    default_route = _parse_default_route(route_raw, name)
    egress_raw = _run(
        [
            "docker",
            "exec",
            name,
            "/usr/bin/curl",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--silent",
            "--show-error",
            "--fail",
            "--max-time",
            "15",
            PUBLIC_EGRESS_URL,
        ],
        f"{name} 公网出口查询",
        timeout=25,
        operation=f"arm64:public-egress:{name}",
    )
    try:
        public_ip = str(ipaddress.ip_address(egress_raw.decode("ascii").strip()))
    except (UnicodeError, ValueError) as error:
        raise Arm64EnvironmentReceiptError(f"{name} 公网出口响应不是 IP 地址") from error
    return {
        "name": name,
        "container_id": container_id,
        "image_id": image_id,
        "selected_network": selected,
        "network_bindings": normalized_networks,
        "default_route": default_route,
        "public_egress": {
            "url": PUBLIC_EGRESS_URL,
            "ip_address": public_ip,
            "response_sha256": _sha256_bytes(egress_raw),
        },
        "raw_sha256": {
            "docker_inspect": _sha256_bytes(inspect_raw),
            "proc_net_route": _sha256_bytes(route_raw),
        },
    }


def _wireguard_observation() -> dict[str, Any]:
    """读取 ARM64 wg1 的持久配置与运行时 MTU，不暴露配置内容。"""

    config = WIREGUARD_CONFIG
    if config.is_symlink() or not config.is_file():
        raise Arm64EnvironmentReceiptError("ARM64 wg1 配置不是可信普通文件")
    metadata = config.stat()
    if (
        metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise Arm64EnvironmentReceiptError("ARM64 wg1 配置必须为 root:root 0600")
    try:
        raw = config.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise Arm64EnvironmentReceiptError("ARM64 wg1 配置不可读") from error

    section: str | None = None
    configured_values: list[int] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section != "interface" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() != "mtu":
            continue
        try:
            configured_values.append(int(value.strip()))
        except ValueError as error:
            raise Arm64EnvironmentReceiptError("ARM64 wg1 配置 MTU 非整数") from error
    if configured_values != [EXPECTED_WG1_MTU]:
        raise Arm64EnvironmentReceiptError(
            f"ARM64 wg1 配置 MTU 必须唯一且等于 "
            f"{EXPECTED_EGRESS_PROVIDER} {EXPECTED_WG1_MTU}"
        )

    runtime_path = Path(f"/sys/class/net/{WIREGUARD_INTERFACE}/mtu")
    try:
        runtime_mtu = int(runtime_path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeError, ValueError) as error:
        raise Arm64EnvironmentReceiptError("ARM64 wg1 运行时 MTU 不可读") from error
    if runtime_mtu != EXPECTED_WG1_MTU:
        raise Arm64EnvironmentReceiptError(
            f"ARM64 wg1 运行时 MTU 与 "
            f"{EXPECTED_EGRESS_PROVIDER} {EXPECTED_WG1_MTU} 不一致"
        )
    return {
        "interface": WIREGUARD_INTERFACE,
        "egress_provider": EXPECTED_EGRESS_PROVIDER,
        "configured_mtu": configured_values[0],
        "runtime_mtu": runtime_mtu,
        "expected_mtu": EXPECTED_WG1_MTU,
        "config_path": str(config),
        "config_sha256": _sha256_bytes(raw),
    }


def _collect_facts(*, phase: str, subject_id: str) -> dict[str, Any]:
    """只读采集宿主、两个容器、固定出口和根文件系统事实。"""

    if phase not in PHASES:
        raise Arm64EnvironmentReceiptError(f"phase 必须属于 {sorted(PHASES)}")
    _safe_id(subject_id, "subject_id")
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        raise Arm64EnvironmentReceiptError("本门禁只能在 ARM64 宿主机执行")
    filesystem = os.statvfs("/")
    block_size = filesystem.f_frsize or filesystem.f_bsize
    total_bytes = filesystem.f_blocks * block_size
    available_bytes = filesystem.f_bavail * block_size
    used_bytes = (filesystem.f_blocks - filesystem.f_bfree) * block_size
    denominator = used_bytes + available_bytes
    used_percent = (
        (used_bytes * 100 + denominator - 1) // denominator if denominator else 100
    )
    producer = Path(__file__).resolve()
    return {
        "schema_version": FACTS_SCHEMA,
        "phase": phase,
        "subject_id": subject_id,
        "observed_at_utc": _utc_now(),
        "contract_sha256": contract_sha256(),
        "host": {
            "hostname": socket.gethostname(),
            "architecture": "linux/arm64",
        },
        "root_filesystem": {
            "mountpoint": "/",
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "available_bytes": available_bytes,
            "used_percent": used_percent,
        },
        "wireguard": _wireguard_observation(),
        "containers": [
            _container_observation(name) for name in sorted(CONTAINER_CONTRACTS)
        ],
        "collector": {
            "schema_version": PRODUCER_SCHEMA,
            "tool": str(producer),
            "tool_sha256": _sha256_file(producer),
            "version": PRODUCER_VERSION,
        },
    }


def collect_facts(
    *,
    phase: str,
    subject_id: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """采集 ARM64 事实，并让全部容器命令共享受管 deadline。"""

    global _ACTIVE_DEADLINE, _ACTIVE_HEARTBEAT
    previous_deadline = _ACTIVE_DEADLINE
    previous_heartbeat = _ACTIVE_HEARTBEAT
    _ACTIVE_DEADLINE = deadline
    _ACTIVE_HEARTBEAT = heartbeat
    try:
        return _collect_facts(phase=phase, subject_id=subject_id)
    finally:
        _ACTIVE_DEADLINE = previous_deadline
        _ACTIVE_HEARTBEAT = previous_heartbeat


def _validate_container(
    value: Any,
    expected_name: str,
    *,
    expected_public_egress: str,
) -> dict[str, Any]:
    container = _expect(
        value,
        {
            "name",
            "container_id",
            "image_id",
            "selected_network",
            "network_bindings",
            "default_route",
            "public_egress",
            "raw_sha256",
        },
        f"containers.{expected_name}",
    )
    if container.get("name") != expected_name:
        raise Arm64EnvironmentReceiptError("容器列表名称或顺序漂移")
    if not CONTAINER_ID_RE.fullmatch(str(container.get("container_id", ""))):
        raise Arm64EnvironmentReceiptError(f"{expected_name} container_id 非法")
    if not IMAGE_ID_RE.fullmatch(str(container.get("image_id", ""))):
        raise Arm64EnvironmentReceiptError(f"{expected_name} image_id 非法")
    expected = CONTAINER_CONTRACTS[expected_name]
    selected = _expect(
        container.get("selected_network"),
        {"name", "network_id", "endpoint_id", "ipv4_address", "gateway"},
        f"{expected_name}.selected_network",
    )
    if (
        selected.get("name") != expected["network"]
        or selected.get("ipv4_address") != expected["ipv4_address"]
        or selected.get("gateway") != expected["gateway"]
    ):
        raise Arm64EnvironmentReceiptError(
            f"{expected_name} 固定网络坐标不一致；禁止继续或修改网络"
        )
    for key in ("network_id", "endpoint_id"):
        if not isinstance(selected.get(key), str) or not selected[key]:
            raise Arm64EnvironmentReceiptError(f"{expected_name} {key} 为空")
    bindings = container.get("network_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise Arm64EnvironmentReceiptError(f"{expected_name} 网络绑定为空")
    if bindings != sorted(bindings, key=lambda item: item.get("name", "") if isinstance(item, dict) else ""):
        raise Arm64EnvironmentReceiptError(f"{expected_name} 网络绑定未排序")
    if selected not in bindings:
        raise Arm64EnvironmentReceiptError(
            f"{expected_name} 固定网络没有对应的完整网络绑定"
        )
    route = _expect(
        container.get("default_route"), {"interface", "gateway"}, f"{expected_name}.default_route"
    )
    if route.get("gateway") != expected["gateway"] or not route.get("interface"):
        raise Arm64EnvironmentReceiptError(
            f"{expected_name} 默认路由未使用固定网关；禁止继续或修改路由"
        )
    egress = _expect(
        container.get("public_egress"),
        {"url", "ip_address", "response_sha256"},
        f"{expected_name}.public_egress",
    )
    if (
        egress.get("url") != PUBLIC_EGRESS_URL
        or egress.get("ip_address") != expected_public_egress
    ):
        raise Arm64EnvironmentReceiptError(
            f"{expected_name} 公网出口不是 {expected_public_egress}"
        )
    if not SHA256_RE.fullmatch(str(egress.get("response_sha256", ""))):
        raise Arm64EnvironmentReceiptError(f"{expected_name} 出口响应摘要非法")
    raw_sha = _expect(
        container.get("raw_sha256"),
        {"docker_inspect", "proc_net_route"},
        f"{expected_name}.raw_sha256",
    )
    if any(not SHA256_RE.fullmatch(str(raw_sha.get(key, ""))) for key in raw_sha):
        raise Arm64EnvironmentReceiptError(f"{expected_name} 原始事实摘要非法")
    return container


def validate_facts(
    facts: dict[str, Any],
    *,
    allow_legacy_replay: bool = False,
) -> dict[str, Any]:
    """严格校验原始事实并返回用于前后连续性比较的稳定身份。"""

    producer_version = _validated_producer_version(
        facts.get("collector"),
        allow_legacy_replay=allow_legacy_replay,
    )
    fact_fields = {
        "schema_version",
        "phase",
        "subject_id",
        "observed_at_utc",
        "contract_sha256",
        "host",
        "root_filesystem",
        "containers",
        "collector",
    }
    if producer_version in {"3", PRODUCER_VERSION}:
        fact_fields.add("wireguard")
    _expect(
        facts,
        fact_fields,
        "facts",
    )
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise Arm64EnvironmentReceiptError("facts.schema_version 不匹配")
    if facts.get("phase") not in PHASES:
        raise Arm64EnvironmentReceiptError("facts.phase 非法")
    _safe_id(facts.get("subject_id"), "facts.subject_id")
    _rfc3339(facts.get("observed_at_utc"), "facts.observed_at_utc")
    if producer_version == PRODUCER_VERSION:
        expected_contract = contract_sha256()
        expected_public_egress = EXPECTED_PUBLIC_EGRESS
    elif producer_version == "3":
        expected_contract = LEGACY_V3_NETWORK_CONTRACT_SHA256
        expected_public_egress = LEGACY_DMIT_PUBLIC_EGRESS
    else:
        expected_contract = LEGACY_NETWORK_CONTRACT_SHA256
        expected_public_egress = LEGACY_DMIT_PUBLIC_EGRESS
    if facts.get("contract_sha256") != expected_contract:
        raise Arm64EnvironmentReceiptError("固定网络或资源合同摘要漂移")
    host = _expect(facts.get("host"), {"hostname", "architecture"}, "facts.host")
    if host.get("architecture") != "linux/arm64" or not isinstance(host.get("hostname"), str) or not host["hostname"]:
        raise Arm64EnvironmentReceiptError("宿主机不是可信 ARM64 身份")
    filesystem = _expect(
        facts.get("root_filesystem"),
        {"mountpoint", "total_bytes", "used_bytes", "available_bytes", "used_percent"},
        "facts.root_filesystem",
    )
    if filesystem.get("mountpoint") != "/":
        raise Arm64EnvironmentReceiptError("根文件系统挂载点非法")
    for key in ("total_bytes", "used_bytes", "available_bytes", "used_percent"):
        if not isinstance(filesystem.get(key), int) or isinstance(filesystem.get(key), bool):
            raise Arm64EnvironmentReceiptError(f"根文件系统 {key} 非整数")
    if (
        filesystem["total_bytes"] <= 0
        or filesystem["used_bytes"] < 0
        or filesystem["available_bytes"] < ROOT_MIN_AVAILABLE_BYTES
        or filesystem["used_percent"] > ROOT_MAX_USED_PERCENT
    ):
        raise Arm64EnvironmentReceiptError(
            "ARM64 根文件系统达到停线水位（使用率须低于 70%，可用空间须不少于 30 GiB）"
        )
    containers = facts.get("containers")
    expected_names = sorted(CONTAINER_CONTRACTS)
    if not isinstance(containers, list) or [item.get("name") for item in containers if isinstance(item, dict)] != expected_names:
        raise Arm64EnvironmentReceiptError("容器事实必须唯一且完整覆盖固定双容器")
    normalized = [
        _validate_container(
            item,
            name,
            expected_public_egress=expected_public_egress,
        )
        for item, name in zip(containers, expected_names, strict=True)
    ]
    wireguard: dict[str, Any] | None = None
    if producer_version == "3":
        wireguard = _expect(
            facts.get("wireguard"),
            {
                "interface",
                "configured_mtu",
                "runtime_mtu",
                "expected_dmit_mtu",
                "config_path",
                "config_sha256",
            },
            "wireguard",
        )
        if (
            wireguard.get("interface") != WIREGUARD_INTERFACE
            or wireguard.get("configured_mtu") != LEGACY_DMIT_WG1_MTU
            or wireguard.get("runtime_mtu") != LEGACY_DMIT_WG1_MTU
            or wireguard.get("expected_dmit_mtu") != LEGACY_DMIT_WG1_MTU
            or wireguard.get("config_path") != str(WIREGUARD_CONFIG)
            or not SHA256_RE.fullmatch(str(wireguard.get("config_sha256", "")))
        ):
            raise Arm64EnvironmentReceiptError(
                "历史 ARM64 wg1 持久配置或运行时 MTU 与 DMIT 冻结值不一致"
            )
    elif producer_version == PRODUCER_VERSION:
        wireguard = _expect(
            facts.get("wireguard"),
            {
                "interface",
                "egress_provider",
                "configured_mtu",
                "runtime_mtu",
                "expected_mtu",
                "config_path",
                "config_sha256",
            },
            "wireguard",
        )
        if (
            wireguard.get("interface") != WIREGUARD_INTERFACE
            or wireguard.get("egress_provider") != EXPECTED_EGRESS_PROVIDER
            or wireguard.get("configured_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("runtime_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("expected_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("config_path") != str(WIREGUARD_CONFIG)
            or not SHA256_RE.fullmatch(str(wireguard.get("config_sha256", "")))
        ):
            raise Arm64EnvironmentReceiptError(
                "ARM64 wg1 持久配置或运行时 MTU 与 BWG 冻结值不一致"
            )
    # Docker restart／compose recreate 会更换 container_id、EndpointID 和容器内接口名，
    # 但不会改变受管网络本身。候选抓包按设计会执行这两类操作；若把这些临时值纳入
    # 连续性身份，每次正常恢复都会被误判为网络污染。连续性只绑定真正不可变的镜像、
    # 网络 ID、地址、网关和公网出口；完整临时值仍保留在 facts 中供审计。
    def stable_network(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": value["name"],
            "network_id": value["network_id"],
            "ipv4_address": value["ipv4_address"],
            "gateway": value["gateway"],
        }

    if producer_version == "1":
        # v1 历史收据必须按生成时的临时身份算法逐字重放，
        # 不得用 v2 稳定网络身份重写当时结论。
        continuity_identity = {
            "host": host,
            "containers": [
                {
                    "name": item["name"],
                    "container_id": item["container_id"],
                    "image_id": item["image_id"],
                    "selected_network": item["selected_network"],
                    "network_bindings": item["network_bindings"],
                    "default_route": item["default_route"],
                    "public_egress": {
                        "url": item["public_egress"]["url"],
                        "ip_address": item["public_egress"]["ip_address"],
                    },
                }
                for item in normalized
            ],
        }
    else:
        continuity_identity = {
            "host": host,
            "containers": [
                {
                    "name": item["name"],
                    "image_id": item["image_id"],
                    "selected_network": stable_network(item["selected_network"]),
                    "network_bindings": [
                        stable_network(binding)
                        for binding in item["network_bindings"]
                    ],
                    "default_route": {
                        "gateway": item["default_route"]["gateway"],
                    },
                    "public_egress": {
                        "url": item["public_egress"]["url"],
                        "ip_address": item["public_egress"]["ip_address"],
                    },
                }
                for item in normalized
            ],
        }
        if wireguard is not None:
            continuity_identity["wireguard"] = wireguard
    return {
        "producer_version": producer_version,
        "continuity_identity_sha256": _sha256_bytes(_canonical(continuity_identity)),
        "resource_gate": {
            "used_percent": filesystem["used_percent"],
            "available_bytes": filesystem["available_bytes"],
            "passed": True,
        },
    }


def _build_receipt(
    root: Path,
    facts_relative: str,
    *,
    replay_producer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _private_root(root)
    facts_path = _relative(root, facts_relative, "facts")
    facts, raw = _load_json(facts_path, "facts")
    validation = validate_facts(
        facts,
        allow_legacy_replay=replay_producer is not None,
    )
    producer = _current_producer() if replay_producer is None else replay_producer
    if facts.get("collector") != producer:
        raise Arm64EnvironmentReceiptError("facts 与 receipt producer 身份不一致")
    if validation["producer_version"] != producer.get("version"):
        raise Arm64EnvironmentReceiptError("facts 与 receipt producer 版本不一致")
    return {
        "schema_version": RECEIPT_SCHEMA,
        "status": "passed",
        "phase": facts["phase"],
        "subject_id": facts["subject_id"],
        "observed_at_utc": facts["observed_at_utc"],
        "contract_sha256": facts["contract_sha256"],
        "continuity_identity_sha256": validation["continuity_identity_sha256"],
        "resource_gate": validation["resource_gate"],
        "facts": {
            "path": facts_relative,
            "sha256": _sha256_bytes(raw),
            "bytes": len(raw),
        },
        "producer": producer,
    }


def build_receipt(root: Path, facts_relative: str) -> dict[str, Any]:
    """只使用当前 producer 生成新收据。"""

    return _build_receipt(root, facts_relative)


def collect(
    root: Path,
    output_relative: str,
    *,
    phase: str,
    subject_id: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    root = _private_root(root)
    output = _relative(root, output_relative, "facts output")
    facts = collect_facts(
        phase=phase,
        subject_id=subject_id,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    _write_once(output, facts)
    return facts


def finalize(root: Path, facts_relative: str, output_relative: str) -> dict[str, Any]:
    root = _private_root(root)
    output = _relative(root, output_relative, "receipt output")
    receipt = build_receipt(root, facts_relative)
    _write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    root = _private_root(root)
    path = _relative(root, receipt_relative, "receipt")
    receipt, raw = _load_json(path, "receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise Arm64EnvironmentReceiptError("receipt.schema_version 不匹配")
    facts = receipt.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("path"), str):
        raise Arm64EnvironmentReceiptError("receipt.facts 缺失")
    producer = receipt.get("producer")
    _validated_producer_version(producer, allow_legacy_replay=True)
    expected = _build_receipt(
        root,
        facts["path"],
        replay_producer=producer,
    )
    if _canonical(expected) != raw:
        raise Arm64EnvironmentReceiptError("ARM64 环境收据重放结果不一致")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="只读采集 ARM64 原始环境事实")
    collect_parser.add_argument("--evidence-root", type=Path, required=True)
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    collect_parser.add_argument("--subject-id", required=True)
    finalize_parser = commands.add_parser("finalize", help="封存 ARM64 环境收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放 ARM64 环境收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "collect":
            result = collect(
                arguments.evidence_root,
                arguments.output,
                phase=arguments.phase,
                subject_id=arguments.subject_id,
            )
        elif arguments.command == "finalize":
            result = finalize(
                arguments.evidence_root, arguments.facts, arguments.output
            )
        else:
            result = replay(arguments.evidence_root, arguments.receipt)
    except (OSError, Arm64EnvironmentReceiptError) as error:
        print(f"Codex ARM64 环境收据失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": result.get("status", "collected"),
                "phase": result["phase"],
                "subject_id": result["subject_id"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
