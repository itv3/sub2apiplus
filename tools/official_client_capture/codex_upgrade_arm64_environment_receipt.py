#!/usr/bin/env python3
"""生成并重放 Codex 升级所需的 ARM64 网络与磁盘硬门禁收据。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    import incremental_recovery
else:
    from tools.official_client_capture import incremental_recovery


FACTS_SCHEMA = "codex-upgrade-arm64-environment-facts/v1"
RECEIPT_SCHEMA = "codex-upgrade-arm64-environment-receipt/v1"
PRODUCER_SCHEMA = "codex-upgrade-arm64-environment-producer/v1"
PRODUCER_VERSION = "8"
PRODUCER_TOOL_RELATIVE = (
    "tools/official_client_capture/codex_upgrade_arm64_environment_receipt.py"
)
# v1 只用于重放已封存历史收据；新 facts 和新收据只能由当前 producer 生成。
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
    # v4 首次冻结 BWG 出口与 MTU，但尚未验证 Endpoint、MSS clamp 和
    # Cloud Config／OpenAI TLS 就绪性。旧收据只能按原合同只读重放。
    "4": frozenset(
        {
            "5d7a9965022ad513b3751793388529a0185747e791609f086803df483ddd4d1d",
        }
    ),
    # v5 首次冻结 BWG IPv4 Endpoint、出站 MSS clamp 和双容器 curl TLS
    # 就绪性，但尚未验证回程 SYN-ACK MSS 与 Codex Rust TLS 栈。它只能按
    # 原字段只读重放，不能再生成新的 P0 事实。
    "5": frozenset(
        {
            "960172c6ac3263b27ab56d9318455982b22535f41726a936b1bd69ed8db85a52",
        }
    ),
    # v6 首次冻结回程 SYN-ACK MSS 与 Codex Rust TLS 栈就绪性，但对全部 12 个
    # phase 都执行根盘水位硬门禁：attempt 收尾时根盘只差几 MB 就把整个 attempt
    # 判成 environment_contaminated。它只能按原字段、原合同和全阶段硬门禁只读
    # 重放，不能生成新事实。
    "6": frozenset(
        {
            "a4421b7e179de5e8001b213dadc6f90c4b6213ac2234f29dba14f9e6063665f9",
        }
    ),
    # v7 的出口、WireGuard 与资源降级语义保持原样，仅允许重放已封存收据。
    "7": frozenset({"d0b6a0650cbb2f3ef33349d914e5aad24c323288d1af618fa6f50313cd570a5f"}),
}
PUBLIC_EGRESS_URL = "https://api.ipify.org"
TLS_READINESS_PROBES = (
    (
        "chatgpt-cloud-config",
        "https://chatgpt.com/backend-api/wham/config/bundle",
        401,
    ),
    ("openai-models", "https://api.openai.com/v1/models", 401),
)
TLS_READINESS_ATTEMPTS = 3
EXPECTED_EGRESS_PROVIDER = "BWG"
EXPECTED_PUBLIC_EGRESS = "144.34.230.210"
LEGACY_DMIT_PUBLIC_EGRESS = "179.255.100.158"
WIREGUARD_INTERFACE = "wg1"
WIREGUARD_CONFIG = Path("/etc/wireguard/wg1.conf")
EXPECTED_WG1_ENDPOINT = "144.34.230.210:51830"
# BWG 当前受管 wg1 MTU 已独立核验并冻结为 1420。ARM64 的持久配置和
# 运行时值必须同时与该对端值一致，不能只检查 IP、rule 和 route。
EXPECTED_WG1_MTU = 1420
EXPECTED_TCP_MSS = EXPECTED_WG1_MTU - 40
EXPECTED_TCPMSS_SOURCES = ("172.25.0.3/32", "172.30.0.0/16")
EXPECTED_TCPMSS_DESTINATIONS = EXPECTED_TCPMSS_SOURCES
EXPECTED_TCPMSS_MATCH_RANGE = f"{EXPECTED_TCP_MSS + 1}:65535"
RUST_TLS_PROBE_CONTAINER = "capture-cli"
# v6～v7 合同冻结的 0.154.0 探针目标，只供历史收据按原 producer 重放；v8 起探针目标由采集
# 参数给出本轮目标版本，二进制路径按固定模板派生，工具不随客户端版本修改。
LEGACY_RUST_TLS_PROBE_BINARY = "/opt/codex-0.154.0/bin/codex"
LEGACY_RUST_TLS_PROBE_CODEX_VERSION = "0.154.0"
RUST_TLS_PROBE_BINARY_TEMPLATE = "/opt/codex-{codex_version}/bin/codex"
CODEX_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
RUST_TLS_PROBE_HOST_RUNTIME_ROOT = Path("/root/docker/capture-cli/data/runtime")
RUST_TLS_PROBE_CONTAINER_RUNTIME_ROOT = PurePosixPath("/capture/runtime")
RUST_TLS_PROBE_TIMEOUT_SECONDS = 30
LEGACY_DMIT_WG1_MTU = 1420
LEGACY_NETWORK_CONTRACT_SHA256 = (
    "9e342c764883ee1107b998ef7a26650402ff4e8d82207926f518528f83dc4ec8"
)
LEGACY_V3_NETWORK_CONTRACT_SHA256 = (
    "551c8faf877eb4889edd7f4426c007110107b2d657f22b59b70785869902736b"
)
LEGACY_V4_NETWORK_CONTRACT_SHA256 = (
    "a0521c7c38ee3bb820772423b644c711f8bdb476bb98c68057c2c32c93e39057"
)
LEGACY_V5_NETWORK_CONTRACT_SHA256 = (
    "1b0130348650881625e47a4f880c3b3b5b3430365d6bbcce4fe3c390088a1e4f"
)
LEGACY_V6_NETWORK_CONTRACT_SHA256 = (
    "e2ab26a2eb1ccc6fa4152e0a78e034645fbc27ff2ea8c7702db1bdc7e1b64936"
)
LEGACY_V7_NETWORK_CONTRACT_SHA256 = "e4fc44a833f0e376bfd6c57c528da93e66b64100e390178f0c607f6aeadd4e2e"
ROOT_MAX_USED_PERCENT = 69
ROOT_MIN_AVAILABLE_BYTES = 30 * 1024 * 1024 * 1024
# v7 起：before／p0／deployment_before 等准入阶段保持根盘水位硬门禁；各
# ``*_after`` 收尾阶段低于水位只把 resource_gate 记为 degraded，收据本身通过，
# attempt 不因此判污染——连续性身份从不包含磁盘，下一次 before 探针天然复验。
RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX = "_after"
# 与 v6 及更早版本共用的字段形状：v6 的 TLS／WireGuard／Rust readiness 事实结构
# 与 v7 相同，差别只在合同摘要与 after 阶段的资源门禁语义。
RUST_TLS_READINESS_PRODUCER_VERSIONS = frozenset({"6", "7", PRODUCER_VERSION})
LEGACY_FULL_WIREGUARD_PRODUCER_VERSIONS = frozenset({"6", "7"})
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
EGRESS_POLICY_SCHEMA = "codex-runtime-egress-policy/v1"
EGRESS_STATUS_SCHEMA = "codex-runtime-egress-status/v1"
EGRESS_EQUIVALENCE_SCHEMA = "codex-arm64-environment-equivalence/v2"
EGRESS_POLICY_PATH = Path("/etc/sub2api-egress/policy.json")
EGRESS_STATUS_PATH = Path("/run/sub2api-egress/status.json")


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


def validate_egress_policy(value: Any) -> dict[str, Any]:
    """校验外部运维策略，不从当前网络观测学习出口，也不内置服务商选择。

    同一份策略供源宿主、出口宿主、持续守护和升级准入共同读取。私钥只存放在
    两端 root 专有文件中；策略仅登记公钥、允许路径与用户授权说明，便于审计。
    """

    policy = _expect(value, {
        "schema_version", "policy_id", "revision", "authorization", "services", "nodes",
        "allowed_public_ipv4", "probe_urls", "probe_quorum", "probe_refresh_seconds",
        "probe_max_age_seconds", "lease_seconds", "poll_seconds", "route_table", "rule_priority",
        "control_port",
    }, "出口策略")
    if policy["schema_version"] != EGRESS_POLICY_SCHEMA:
        raise Arm64EnvironmentReceiptError("出口策略 schema 未登记")
    _safe_id(policy["policy_id"], "出口策略 policy_id")
    def integer(number: Any, low: int, high: int, label: str) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise Arm64EnvironmentReceiptError(f"出口策略 {label} 必须为 {low}～{high} 的整数")
    integer(policy["revision"], 1, 2**31 - 1, "revision")
    authorization = _expect(policy["authorization"], {"actor", "authorized_at_utc", "reason"}, "出口授权")
    _rfc3339(authorization["authorized_at_utc"], "出口授权时间")
    if any(not isinstance(authorization[k], str) or not authorization[k].strip() for k in ("actor", "reason")):
        raise Arm64EnvironmentReceiptError("出口选择必须保留显式授权人和原因")
    addresses = policy["allowed_public_ipv4"]
    if (not isinstance(addresses, list) or not addresses
            or any(not isinstance(address, str) for address in addresses)
            or len(set(addresses)) != len(addresses)
            or any(not ipaddress.IPv4Address(address).is_global for address in addresses)):
        raise Arm64EnvironmentReceiptError("出口策略必须列出唯一的允许公网 IPv4")
    nodes = _expect(policy["nodes"], {"origin", "exit"}, "出口策略 nodes")
    for role, node in nodes.items():
        _expect(node, {"interface", "tunnel_ipv4", "public_key", "endpoint", "listen_port", "mtu", "public_interface"}, f"出口节点 {role}")
        for key in ("interface", "public_interface"):
            if not isinstance(node[key], str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}", node[key]):
                raise Arm64EnvironmentReceiptError(f"出口节点 {role}.{key} 不是安全接口名")
        tunnel = ipaddress.IPv4Interface(node["tunnel_ipv4"])
        if tunnel.ip.is_global or tunnel.network.prefixlen != 30:
            raise Arm64EnvironmentReceiptError("专用通道必须使用独立的私网 /30 地址段")
        try:
            public_key = base64.b64decode(node["public_key"], validate=True)
        except (TypeError, ValueError) as error:
            raise Arm64EnvironmentReceiptError("出口节点公钥格式非法") from error
        if len(public_key) != 32:
            raise Arm64EnvironmentReceiptError("出口节点公钥长度非法")
        endpoint = _expect(node["endpoint"], {"ipv4", "port"}, f"出口节点 {role}.endpoint")
        if not ipaddress.IPv4Address(endpoint["ipv4"]).is_global:
            raise Arm64EnvironmentReceiptError("出口节点 endpoint 必须是显式公网 IPv4")
        integer(endpoint["port"], 1, 65535, "endpoint.port")
        integer(node["listen_port"], 1, 65535, "listen_port")
        integer(node["mtu"], 1280, 1500, "mtu")
    origin = ipaddress.IPv4Interface(nodes["origin"]["tunnel_ipv4"])
    exit_node = ipaddress.IPv4Interface(nodes["exit"]["tunnel_ipv4"])
    if origin.network != exit_node.network or origin.ip == exit_node.ip:
        raise Arm64EnvironmentReceiptError("专用通道的两端必须是同一 /30 内的不同地址")
    services = policy["services"]
    if not isinstance(services, dict) or not services:
        raise Arm64EnvironmentReceiptError("出口策略没有受保护服务")
    for name, service in services.items():
        _safe_id(name, "受保护服务")
        _expect(service, {"cgroup_parent", "dns_servers", "dependencies", "ingress_tcp_ports"}, f"受保护服务 {name}")
        if not isinstance(service["cgroup_parent"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.slice", service["cgroup_parent"]):
            raise Arm64EnvironmentReceiptError("受保护服务必须使用固定的专用 systemd slice")
        if not isinstance(service["dns_servers"], list) or not service["dns_servers"]:
            raise Arm64EnvironmentReceiptError("受保护服务必须显式声明 DNS，禁止继承宿主代理解析路径")
        if any(not ipaddress.IPv4Address(address).is_global for address in service["dns_servers"]):
            raise Arm64EnvironmentReceiptError("外部 DNS 必须通过指定公网通道访问")
        if not isinstance(service["dependencies"], list):
            raise Arm64EnvironmentReceiptError("内网依赖必须是显式列表")
        for dependency in service["dependencies"]:
            _expect(dependency, {"container", "protocol", "ports"}, f"{name} 内网依赖")
            _safe_id(dependency["container"], "依赖容器")
            if dependency["protocol"] not in {"tcp", "udp"} or not isinstance(dependency["ports"], list) or not dependency["ports"]:
                raise Arm64EnvironmentReceiptError("内网依赖必须限定协议和端口")
            for port in dependency["ports"]:
                integer(port, 1, 65535, "dependency.port")
        if not isinstance(service["ingress_tcp_ports"], list):
            raise Arm64EnvironmentReceiptError("入站服务必须显式登记 TCP 端口")
        for port in service["ingress_tcp_ports"]:
            integer(port, 1, 65535, "ingress_tcp_ports")
    urls = policy["probe_urls"]
    if not isinstance(urls, list) or len(urls) < 2 or any(not isinstance(url, str) for url in urls):
        raise Arm64EnvironmentReceiptError("出口验证必须登记至少两个独立 HTTPS 来源")
    parsed = [urlsplit(url) for url in urls]
    if (len({url.hostname for url in parsed}) != len(parsed)
            or any(url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.port not in {None, 443} for url in parsed)):
        raise Arm64EnvironmentReceiptError("出口验证来源必须是无凭据、无 fragment 且主机不同的 HTTPS URL")
    integer(policy["probe_quorum"], 2, len(urls), "probe_quorum")
    integer(policy["probe_refresh_seconds"], 1, 30, "probe_refresh_seconds")
    integer(policy["probe_max_age_seconds"], policy["probe_refresh_seconds"] + 1, 60, "probe_max_age_seconds")
    integer(policy["lease_seconds"], 1, 5, "lease_seconds")
    poll = policy["poll_seconds"]
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not 0.1 <= poll <= policy["lease_seconds"] / 2:
        raise Arm64EnvironmentReceiptError("守护轮询间隔必须不大于放行租期的一半")
    integer(policy["route_table"], 256, 2**31 - 1, "route_table")
    integer(policy["rule_priority"], 10, 30000, "rule_priority")
    integer(policy["control_port"], 1024, 65535, "control_port")
    return policy


def egress_policy_sha256(policy: dict[str, Any]) -> str:
    """两端守护、安装记录和收据共用唯一策略摘要算法，包含 canonical 末尾换行。"""

    return _sha256_bytes(_canonical(policy))


def load_egress_policy(path: Path = EGRESS_POLICY_PATH) -> dict[str, Any]:
    """只接受 root 专有策略；切换须由运维显式替换配置，守护不得自行回写。"""

    payload = _read_egress_runtime_json(Path(path), private=True)
    try:
        return validate_egress_policy(payload)
    except (TypeError, ValueError, KeyError) as error:
        raise Arm64EnvironmentReceiptError(f"出口策略拒绝：{error}") from error


def _read_egress_runtime_json(path: Path, *, private: bool) -> dict[str, Any]:
    """用同一个文件描述符校验权限并读取，拒绝符号链接和可由其他用户替换的父目录。"""

    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise Arm64EnvironmentReceiptError("出口运行时文件必须使用规范绝对路径")
    for parent in path.parents:
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise Arm64EnvironmentReceiptError("出口运行时文件的父目录不受 root 独占管理")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        modes = {0o600} if private else {0o600, 0o644}
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) not in modes or metadata.st_size > MAX_JSON_BYTES):
            raise Arm64EnvironmentReceiptError("出口运行时文件的属主、权限、类型或大小非法")
        with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as stream:
            payload = json.load(stream)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise Arm64EnvironmentReceiptError("出口运行时文件必须是 JSON 对象")
    return payload


def egress_observations_compliant(policy: dict[str, Any], observations: Any, *, now_epoch: float) -> bool:
    """允许独立来源替代单点失败；任何成功来源冲突、重复或过期均不能放行。"""

    if not isinstance(observations, list) or len(observations) != len(policy["probe_urls"]):
        return False
    seen: set[str] = set()
    successes = 0
    for item in observations:
        if not isinstance(item, dict) or set(item) != {"url", "status", "ip_address", "observed_at_epoch", "response_sha256"}:
            return False
        url = item["url"]
        observed = item["observed_at_epoch"]
        if (url not in policy["probe_urls"] or url in seen or isinstance(observed, bool)
                or not isinstance(observed, (int, float)) or not 0 <= now_epoch - observed <= policy["probe_max_age_seconds"]):
            return False
        seen.add(url)
        if item["status"] == "failed":
            if item["ip_address"] is not None or item["response_sha256"] is not None:
                return False
            continue
        if (item["status"] != "passed" or item["ip_address"] not in policy["allowed_public_ipv4"]
                or not SHA256_RE.fullmatch(str(item["response_sha256"]))):
            return False
        successes += 1
    return successes >= policy["probe_quorum"]


def validate_egress_status(
    policy: dict[str, Any], status: Any, *, now_epoch: float,
    now_monotonic_ns: int | None = None, boot_id: str | None = None,
    _transitioning_service: str | None = None,
) -> dict[str, Any]:
    """核对逐容器与共享状态；历史事实用采集时间，实时准入额外核对本次启动与内核租期。"""

    value = _expect(status, {"schema_version", "policy_sha256", "role", "boot_id",
                             "observed_at_epoch", "observed_at_monotonic_ns", "valid_until_monotonic_ns",
                             "shared_protection", "services"}, "出口守护状态")
    if (value["schema_version"] != EGRESS_STATUS_SCHEMA or value["role"] != "origin"
            or value["policy_sha256"] != egress_policy_sha256(policy)):
        raise Arm64EnvironmentReceiptError("出口守护没有绑定当前授权策略")
    observed = value["observed_at_epoch"]
    observed_monotonic = value["observed_at_monotonic_ns"]
    expiry = value["valid_until_monotonic_ns"]
    if (isinstance(observed, bool) or not isinstance(observed, (int, float))
            or not 0 <= now_epoch - observed <= policy["lease_seconds"]
            or isinstance(expiry, bool) or not isinstance(expiry, int) or expiry <= 0):
        raise Arm64EnvironmentReceiptError("出口守护状态已过期或时间非法")
    if (not isinstance(observed_monotonic, int) or isinstance(observed_monotonic, bool)
            or not 0 <= observed_monotonic < expiry <= observed_monotonic + policy["lease_seconds"] * 10**9
            or not isinstance(value["boot_id"], str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["boot_id"])):
        raise Arm64EnvironmentReceiptError("出口守护启动身份或历史单调时钟租期非法")
    if now_monotonic_ns is not None and not now_monotonic_ns < expiry <= now_monotonic_ns + policy["lease_seconds"] * 10**9:
        raise Arm64EnvironmentReceiptError("出口放行租期已经失效或超过策略上限")
    if now_monotonic_ns is not None and now_monotonic_ns < observed_monotonic:
        raise Arm64EnvironmentReceiptError("出口状态记录了未来的单调时钟")
    if boot_id is not None and value["boot_id"] != boot_id:
        raise Arm64EnvironmentReceiptError("出口守护状态来自其他宿主启动周期")
    shared = _expect(value["shared_protection"], {"status", "checks", "reason"}, "共享出口保护")
    checks = _expect(shared["checks"], {"kernel_filter", "firewall", "wireguard", "routes", "remote_guard"}, "共享出口检查")
    if shared["status"] != "compliant" or any(result is not True for result in checks.values()):
        raise Arm64EnvironmentReceiptError(f"共享出口保护失效：{shared['reason']}；两个容器必须闭锁")
    services = _expect(value["services"], set(policy["services"]), "逐容器出口状态")
    for name, service in services.items():
        _expect(service, {"status", "admission_state", "container_id", "network_bindings", "observations", "reason", "blocked_at_epoch"}, f"{name} 出口状态")
        if (service["status"] not in {"blocked", "compliant"}
                or service["admission_state"] not in {"ready", "missing", "probing", "invalid"}
                or not isinstance(service["network_bindings"], list)
                or not isinstance(service["observations"], list)
                or any(not isinstance(item, dict) for item in service["observations"])):
            raise Arm64EnvironmentReceiptError("逐容器出口状态格式非法")
        if name == _transitioning_service and service["status"] == "blocked":
            # 仅独立监督器可在已绑定的本地维护命令存活期间请求此检查；普通准入与事实采集不传此参数。
            # 内核业务仍闭锁，且共享故障、其他容器故障、配置错误与出口观测冲突均不能等待放行。
            if (service["admission_state"] not in {"missing", "probing"}
                    or any(item.get("status") == "passed" and item.get("ip_address") not in policy["allowed_public_ipv4"]
                           for item in service["observations"])):
                raise Arm64EnvironmentReceiptError("受控重建期间出现路径或配置故障，必须中止维护等待")
            if service["admission_state"] == "missing":
                if service["container_id"] or service["network_bindings"] or service["observations"]:
                    raise Arm64EnvironmentReceiptError("受控重建的缺失容器状态不闭合")
                continue
            if not CONTAINER_ID_RE.fullmatch(str(service["container_id"])) or not service["network_bindings"]:
                raise Arm64EnvironmentReceiptError("受控重建的探针身份不完整")
        elif service["admission_state"] != "ready":
            raise Arm64EnvironmentReceiptError(f"{name} 尚未完成准入；升级必须暂停")
        else:
            if (service["status"] != "compliant" or not CONTAINER_ID_RE.fullmatch(str(service["container_id"]))
                    or not isinstance(service["network_bindings"], list) or not service["network_bindings"]
                    or not egress_observations_compliant(policy, service["observations"], now_epoch=now_epoch)):
                raise Arm64EnvironmentReceiptError(f"{name} 出口未通过独立验证：{service['reason']}；升级必须暂停")
        bindings = service["network_bindings"]
        for binding in bindings:
            _expect(binding, {"ifindex", "host_ifindex", "source_ipv4"}, f"{name} 出口网卡")
            if any(isinstance(binding[key], bool) or not isinstance(binding[key], int) or binding[key] <= 0 for key in ("ifindex", "host_ifindex")):
                raise Arm64EnvironmentReceiptError("出口网卡索引非法")
            ipaddress.IPv4Address(binding["source_ipv4"])
        if len({(item["ifindex"], item["source_ipv4"]) for item in bindings}) != len(bindings):
            raise Arm64EnvironmentReceiptError("出口网卡身份重复")
    return value


def _checked_runtime_egress(
    policy_path: Path = EGRESS_POLICY_PATH, status_path: Path = EGRESS_STATUS_PATH,
) -> tuple[dict[str, Any], datetime]:
    """读取当前策略与守护状态，并用读取之后的同一时刻完成实时校验；返回准入值与校验时刻。

    事实采集把这一时刻原样写成 ``observed_at_utc``，重放时按完全相同的时刻复算状态年龄与
    观测时效，避免"实时通过、重放因晚取时间而判过期"的边界不一致。
    """

    try:
        policy = load_egress_policy(policy_path)
        status = _read_egress_runtime_json(Path(status_path), private=False)
        checked_at = datetime.now(timezone.utc)
        validate_egress_status(
            policy, status, now_epoch=checked_at.timestamp(), now_monotonic_ns=time.monotonic_ns(),
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Arm64EnvironmentReceiptError(f"运行时出口准入拒绝：{error}") from error
    return {"policy": policy, "policy_sha256": status["policy_sha256"], "runtime": status}, checked_at


def require_runtime_egress(
    policy_path: Path = EGRESS_POLICY_PATH, status_path: Path = EGRESS_STATUS_PATH,
) -> dict[str, Any]:
    """每次准入读取持续守护的当前状态；内核租期过期会自行闭锁，旧认证不能替代此检查。"""

    value, _checked_at = _checked_runtime_egress(policy_path, status_path)
    return value


def campaign_requires_runtime_egress(campaign_dir: Path) -> bool:
    """正式派发必须实时准入；既有 staging 夹具总账只豁免监督器的离线演练。

    这不是环境变量开关：总账须完整重放，且 fixture_only 的规范目录边界须通过。
    真实环境采集器仍无条件调用 require_runtime_egress，夹具总账不能授权生产请求。
    """

    if __package__ in {None, ""}:
        import codex_upgrade_project_ledger as ledger
    else:
        from tools.official_client_capture import codex_upgrade_project_ledger as ledger

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest, _ = _load_json(campaign_dir / "campaign.json", "Campaign")
    if manifest.get("campaign_mode") != "formal":
        return False
    root = ledger.find_project_ledger(campaign_dir)
    if root is not None:
        ledger.replay_head(root)
        with ledger.project_lock(root):
            plan, _ = ledger._load_plan(root)
            ledger._check_fixture_only(root, plan, campaign_dir)
        if plan["fixture_only"]:
            return False
    return True


def environment_equivalence_projection(facts: dict[str, Any]) -> dict[str, Any]:
    """在原 producer 验证通过后投影真实依赖；出口观察和链路配置只用于溯源与准入。

    必须先调用 validate_facts／replay，不能拿未经验证的历史 JSON 直接计算此投影。
    容器镜像和抓包拓扑仍逐项比较，IP 漂移不会被出口解耦顺带放行。
    """

    def network(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in ("name", "network_id", "ipv4_address", "gateway")}
    return {
        "schema_version": EGRESS_EQUIVALENCE_SCHEMA,
        "host": facts["host"],
        "containers": [{"name": item["name"], "image_id": item["image_id"],
                        "selected_network": network(item["selected_network"]),
                        "network_bindings": [network(binding) for binding in item["network_bindings"]]}
                       for item in facts["containers"]],
    }


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


def _legacy_v7_contract_sha256() -> str:
    """返回文档固定网络与资源门禁的稳定摘要。"""

    return _sha256_bytes(
        _canonical(
            {
                "containers": CONTAINER_CONTRACTS,
                "egress_provider": EXPECTED_EGRESS_PROVIDER,
                "public_egress_ip": EXPECTED_PUBLIC_EGRESS,
                "public_egress_url": PUBLIC_EGRESS_URL,
                "tls_readiness": [
                    {
                        "name": name,
                        "url": url,
                        "expected_http_status": expected_status,
                        "attempts": TLS_READINESS_ATTEMPTS,
                    }
                    for name, url, expected_status in TLS_READINESS_PROBES
                ],
                "wireguard": {
                    "interface": WIREGUARD_INTERFACE,
                    "expected_mtu": EXPECTED_WG1_MTU,
                    "expected_endpoint": EXPECTED_WG1_ENDPOINT,
                    "tcp_mss_clamp_sources": list(EXPECTED_TCPMSS_SOURCES),
                    "tcp_mss_clamp_destinations": list(
                        EXPECTED_TCPMSS_DESTINATIONS
                    ),
                    "expected_tcp_mss": EXPECTED_TCP_MSS,
                },
                "rust_tls_readiness": {
                    "container": RUST_TLS_PROBE_CONTAINER,
                    "binary": LEGACY_RUST_TLS_PROBE_BINARY,
                    "codex_version": LEGACY_RUST_TLS_PROBE_CODEX_VERSION,
                    "isolated_empty_codex_home": True,
                    "expected_process_exit_code": 1,
                    "expected_overall_status": "fail",
                    "allowed_failed_checks": ["auth.credentials"],
                    "required_checks": {
                        "auth.credentials": "fail",
                        "config.load": "ok",
                        "network.provider_reachability": "ok",
                    },
                },
                "root_max_used_percent": ROOT_MAX_USED_PERCENT,
                "root_min_available_bytes": ROOT_MIN_AVAILABLE_BYTES,
                "resource_gate_degradable_phase_suffix": (
                    RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX
                ),
                "architecture": "linux/arm64",
            }
        )
    )


def contract_sha256() -> str:
    """v8 的合同绑定校验规则，不把用户的出口选择或隧道参数写入工具身份。"""

    return _sha256_bytes(_canonical({
        "schema_version": "codex-arm64-network-contract/v8",
        "containers": CONTAINER_CONTRACTS,
        "runtime_policy_schema": EGRESS_POLICY_SCHEMA,
        "runtime_status_schema": EGRESS_STATUS_SCHEMA,
        "equivalence_schema": EGRESS_EQUIVALENCE_SCHEMA,
        "tls_readiness": TLS_READINESS_PROBES,
        "tls_readiness_attempts": TLS_READINESS_ATTEMPTS,
        "rust_tls_readiness": {"container": RUST_TLS_PROBE_CONTAINER,
                               "binary_template": RUST_TLS_PROBE_BINARY_TEMPLATE,
                               "codex_version_source": "collect 参数：本轮目标版本"},
        "root_max_used_percent": ROOT_MAX_USED_PERCENT,
        "root_min_available_bytes": ROOT_MIN_AVAILABLE_BYTES,
        "resource_gate_degradable_phase_suffix": RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX,
        "architecture": "linux/arm64",
    }))


def _run_completed(
    argv: list[str],
    label: str,
    timeout: int = 30,
    *,
    operation: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """执行受 deadline 约束的探针，并校验其允许退出码闭集。"""

    # 中文 label 只用于错误诊断；heartbeat operation 属于机器审计字段，
    # 必须使用固定 ASCII 标签，不能把诊断文本或命令参数直接写入心跳。
    if not HEARTBEAT_OPERATION_RE.fullmatch(operation):
        raise Arm64EnvironmentReceiptError("ARM64 探针 heartbeat operation 非法")
    if (
        not isinstance(allowed_returncodes, frozenset)
        or not allowed_returncodes
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in allowed_returncodes
        )
    ):
        raise Arm64EnvironmentReceiptError("ARM64 探针允许退出码闭集非法")
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
    if completed.returncode not in allowed_returncodes:
        message = completed.stderr.decode("utf-8", errors="replace")[:300].strip()
        raise Arm64EnvironmentReceiptError(f"{label}失败：{message}")
    return completed


def _run(
    argv: list[str],
    label: str,
    timeout: int = 30,
    *,
    operation: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> bytes:
    """执行必须成功的普通探针并返回 stdout。"""

    return _run_completed(
        argv,
        label,
        timeout,
        operation=operation,
        deadline=deadline,
        heartbeat=heartbeat,
    ).stdout


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


def _tls_readiness_observation(container: str) -> list[dict[str, Any]]:
    """连续验证 Codex 启动前实际依赖的 ChatGPT 与 OpenAI TLS 路径。"""

    observations: list[dict[str, Any]] = []
    for probe_name, url, expected_status in TLS_READINESS_PROBES:
        attempts: list[dict[str, Any]] = []
        for attempt_index in range(1, TLS_READINESS_ATTEMPTS + 1):
            raw = _run(
                [
                    "docker",
                    "exec",
                    container,
                    "/usr/bin/curl",
                    "--disable",
                    "--proto",
                    "=https",
                    "--tlsv1.2",
                    "--silent",
                    "--show-error",
                    "--output",
                    "/dev/null",
                    "--write-out",
                    "%{http_code}\t%{remote_ip}\t%{time_appconnect}\n",
                    "--connect-timeout",
                    "6",
                    "--max-time",
                    "15",
                    url,
                ],
                f"{container} {probe_name} TLS 就绪探针第 {attempt_index} 次",
                timeout=20,
                operation=(
                    f"arm64:tls-ready:{container}:{probe_name}:{attempt_index}"
                ),
            )
            try:
                fields = raw.decode("ascii").strip().split("\t")
                if len(fields) != 3:
                    raise ValueError("字段数不一致")
                http_status = int(fields[0])
                remote_ip = str(ipaddress.ip_address(fields[1]))
                tls_seconds = float(fields[2])
            except (UnicodeError, ValueError) as error:
                raise Arm64EnvironmentReceiptError(
                    f"{container} {probe_name} TLS 就绪探针响应非法"
                ) from error
            if http_status != expected_status or not 0 < tls_seconds <= 15:
                raise Arm64EnvironmentReceiptError(
                    f"{container} {probe_name} TLS 就绪探针未通过"
                )
            attempts.append(
                {
                    "attempt": attempt_index,
                    "http_status": http_status,
                    "remote_ip": remote_ip,
                    "tls_seconds": tls_seconds,
                    "response_sha256": _sha256_bytes(raw),
                }
            )
        observations.append(
            {
                "name": probe_name,
                "url": url,
                "expected_http_status": expected_status,
                "required_successes": TLS_READINESS_ATTEMPTS,
                "attempts": attempts,
            }
        )
    return observations


def rust_tls_probe_target(codex_version: str) -> dict[str, str]:
    """v8 探针目标：本轮目标版本与按固定模板派生的容器内二进制路径。"""

    if not isinstance(codex_version, str) or not CODEX_VERSION_RE.fullmatch(codex_version):
        raise Arm64EnvironmentReceiptError("Rust TLS 探针目标版本必须是 x.y.z 形式的目标客户端版本")
    return {
        "container": RUST_TLS_PROBE_CONTAINER,
        "binary": RUST_TLS_PROBE_BINARY_TEMPLATE.format(codex_version=codex_version),
        "codex_version": codex_version,
    }


def _rust_tls_readiness_observation(target: dict[str, str]) -> dict[str, Any]:
    """用本轮目标版本的无凭据 Codex Doctor 验证实际 Rust TLS 请求路径。"""

    runtime_root = RUST_TLS_PROBE_HOST_RUNTIME_ROOT
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise Arm64EnvironmentReceiptError("Codex Rust TLS 临时运行根不可信")
    metadata = runtime_root.stat()
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise Arm64EnvironmentReceiptError(
            "Codex Rust TLS 临时运行根必须由当前执行用户拥有且权限为 0700"
        )

    with tempfile.TemporaryDirectory(
        prefix="codex-doctor-",
        dir=runtime_root,
    ) as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        if temporary.is_symlink() or any(temporary.iterdir()):
            raise Arm64EnvironmentReceiptError(
                "Codex Rust TLS 探针 CODEX_HOME 不是隔离空目录"
            )
        container_home = str(
            RUST_TLS_PROBE_CONTAINER_RUNTIME_ROOT / temporary.name
        )
        started = time.monotonic()
        completed = _run_completed(
            [
                "docker",
                "exec",
                RUST_TLS_PROBE_CONTAINER,
                "/usr/bin/env",
                "-i",
                f"CODEX_HOME={container_home}",
                f"HOME={container_home}",
                "PATH=/usr/bin:/bin",
                target["binary"],
                "doctor",
                "--json",
                "--no-color",
            ],
            f"capture-cli Codex {target['codex_version']} Rust TLS 就绪探针",
            timeout=RUST_TLS_PROBE_TIMEOUT_SECONDS,
            operation="arm64:rust-tls:codex-doctor",
            allowed_returncodes=frozenset({1}),
        )
        duration_seconds = time.monotonic() - started

    raw = completed.stdout
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise Arm64EnvironmentReceiptError("Codex Rust TLS Doctor 报告大小非法")
    try:
        report = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Arm64EnvironmentReceiptError(
            "Codex Rust TLS Doctor 报告不是合法 UTF-8 JSON"
        ) from error
    if not isinstance(report, dict):
        raise Arm64EnvironmentReceiptError("Codex Rust TLS Doctor 报告顶层必须是对象")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise Arm64EnvironmentReceiptError("Codex Rust TLS Doctor 缺少 checks")
    expected_checks = {
        "auth.credentials": "fail",
        "config.load": "ok",
        "network.provider_reachability": "ok",
    }
    observed_checks: dict[str, str] = {}
    for check_id, expected_status in expected_checks.items():
        check = checks.get(check_id)
        if not isinstance(check, dict) or check.get("status") != expected_status:
            raise Arm64EnvironmentReceiptError(
                f"Codex Rust TLS Doctor {check_id} 未通过冻结状态"
            )
        observed_checks[check_id] = expected_status
    failed_check_ids = sorted(
        str(check_id)
        for check_id, check in checks.items()
        if isinstance(check, dict) and check.get("status") == "fail"
    )
    auth_summary = str(checks["auth.credentials"].get("summary", ""))
    config_details = checks["config.load"].get("details")
    if (
        failed_check_ids != ["auth.credentials"]
        or "no codex credentials were found" not in auth_summary.lower()
        or not isinstance(config_details, dict)
        or config_details.get("CODEX_HOME") != container_home
    ):
        raise Arm64EnvironmentReceiptError(
            "Codex Rust TLS Doctor 未证明隔离 CODEX_HOME 无凭据"
        )
    if (
        report.get("schemaVersion") != 1
        or report.get("codexVersion") != target["codex_version"]
        or report.get("overallStatus") != "fail"
        or not 0 < duration_seconds <= RUST_TLS_PROBE_TIMEOUT_SECONDS
    ):
        raise Arm64EnvironmentReceiptError(
            "Codex Rust TLS Doctor 版本、终态或耗时与冻结合同不一致"
        )
    return {
        "container": target["container"],
        "binary": target["binary"],
        "codex_version": target["codex_version"],
        "isolated_empty_codex_home": True,
        "process_exit_code": completed.returncode,
        "overall_status": report["overallStatus"],
        "failed_check_ids": failed_check_ids,
        "checks": observed_checks,
        "duration_seconds": round(duration_seconds, 3),
        "report_sha256": _sha256_bytes(raw),
        "report_bytes": len(raw),
    }


def _container_observation(name: str, *, runtime_egress: dict[str, Any] | None = None) -> dict[str, Any]:
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
    if runtime_egress is not None:
        service = runtime_egress["runtime"]["services"][name]
        if service["container_id"] != container_id:
            raise Arm64EnvironmentReceiptError("容器在实时准入与环境采集之间发生重建，须重新验证")
        observed = next(item for item in service["observations"] if item["status"] == "passed")
        public_egress = {key: observed[key] for key in ("url", "ip_address", "response_sha256")}
    else:
        public_egress = _legacy_public_egress_observation(name)
    return {
        "name": name,
        "container_id": container_id,
        "image_id": image_id,
        "selected_network": selected,
        "network_bindings": normalized_networks,
        "default_route": default_route,
        "public_egress": public_egress,
        "tls_readiness": _tls_readiness_observation(name),
        "raw_sha256": {
            "docker_inspect": _sha256_bytes(inspect_raw),
            "proc_net_route": _sha256_bytes(route_raw),
        },
    }


def _legacy_public_egress_observation(name: str) -> dict[str, Any]:
    """保留旧采集器的窄单测入口；v8 正式 collect 只消费持续守护的独立观测。"""

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
            "url": PUBLIC_EGRESS_URL,
            "ip_address": public_ip,
            "response_sha256": _sha256_bytes(egress_raw),
    }


def _expected_tcpmss_config_commands() -> list[tuple[str, str]]:
    """返回 wg-quick 中必须精确存在的双向幂等 MSS 持久化命令。"""

    commands: list[tuple[str, str]] = []
    for source in EXPECTED_TCPMSS_SOURCES:
        base = (
            f"iptables -w 5 -t mangle -C FORWARD -s {source} "
            f"-o %i -p tcp --tcp-flags SYN,RST SYN -j TCPMSS "
            "--clamp-mss-to-pmtu"
        )
        append = base.replace(" -C FORWARD ", " -A FORWARD ", 1)
        delete = base.replace(" -C FORWARD ", " -D FORWARD ", 1)
        commands.append(("postup", f"{base} 2>/dev/null || {append}"))
        commands.append(
            ("postdown", f"{base} 2>/dev/null && {delete} || true")
        )
    for destination in EXPECTED_TCPMSS_DESTINATIONS:
        base = (
            f"iptables -w 5 -t mangle -C FORWARD -i %i -d {destination} "
            f"-p tcp --tcp-flags SYN,RST SYN -m tcpmss "
            f"--mss {EXPECTED_TCPMSS_MATCH_RANGE} -j TCPMSS "
            f"--set-mss {EXPECTED_TCP_MSS}"
        )
        append = base.replace(" -C FORWARD ", " -A FORWARD ", 1)
        delete = base.replace(" -C FORWARD ", " -D FORWARD ", 1)
        commands.append(("postup", f"{base} 2>/dev/null || {append}"))
        commands.append(
            ("postdown", f"{base} 2>/dev/null && {delete} || true")
        )
    return sorted(commands)


def _runtime_tcpmss_rules(raw: bytes) -> dict[str, list[str]]:
    """解析 iptables-save，确认出站 SYN 与回程 SYN-ACK 四条规则闭合。"""

    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError as error:
        raise Arm64EnvironmentReceiptError("ARM64 TCPMSS 运行时规则编码非法") from error
    observed_sources: list[str] = []
    observed_destinations: list[str] = []
    expected_sources = set(EXPECTED_TCPMSS_SOURCES)
    expected_destinations = set(EXPECTED_TCPMSS_DESTINATIONS)
    for line in lines:
        if not line.startswith("-A FORWARD ") or "TCPMSS" not in line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            raise Arm64EnvironmentReceiptError("ARM64 TCPMSS 运行时规则格式非法") from error
        normalized: list[str] = []
        index = 0
        while index < len(tokens):
            if tokens[index : index + 2] == ["-m", "tcp"]:
                index += 2
                continue
            normalized.append(tokens[index])
            index += 1
        source = (
            normalized[normalized.index("-s") + 1]
            if "-s" in normalized and normalized.index("-s") + 1 < len(normalized)
            else ""
        )
        destination = (
            normalized[normalized.index("-d") + 1]
            if "-d" in normalized
            and normalized.index("-d") + 1 < len(normalized)
            else ""
        )
        if source in expected_sources:
            expected = [
                "-A",
                "FORWARD",
                "-s",
                source,
                "-o",
                WIREGUARD_INTERFACE,
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-j",
                "TCPMSS",
                "--clamp-mss-to-pmtu",
            ]
            if normalized != expected:
                raise Arm64EnvironmentReceiptError(
                    f"ARM64 {source} 出站 TCPMSS 运行时规则与冻结合同不一致"
                )
            observed_sources.append(source)
            continue
        if destination in expected_destinations:
            # iptables-save 会把配置中的 ``-i wg1 -d ...`` 规范化为
            # ``-d ... -i wg1``；这里按真实序列精确校验，避免宽松包含判断。
            expected = [
                "-A",
                "FORWARD",
                "-d",
                destination,
                "-i",
                WIREGUARD_INTERFACE,
                "-p",
                "tcp",
                "--tcp-flags",
                "SYN,RST",
                "SYN",
                "-m",
                "tcpmss",
                "--mss",
                EXPECTED_TCPMSS_MATCH_RANGE,
                "-j",
                "TCPMSS",
                "--set-mss",
                str(EXPECTED_TCP_MSS),
            ]
            if normalized != expected:
                raise Arm64EnvironmentReceiptError(
                    f"ARM64 {destination} 回程 TCPMSS 运行时规则与冻结合同不一致"
                )
            observed_destinations.append(destination)
    if sorted(observed_sources) != sorted(EXPECTED_TCPMSS_SOURCES):
        raise Arm64EnvironmentReceiptError(
            "ARM64 出站 TCPMSS 运行时规则缺失、重复或来源漂移"
        )
    if sorted(observed_destinations) != sorted(EXPECTED_TCPMSS_DESTINATIONS):
        raise Arm64EnvironmentReceiptError(
            "ARM64 回程 TCPMSS 运行时规则缺失、重复或目标漂移"
        )
    return {
        "sources": sorted(observed_sources),
        "destinations": sorted(observed_destinations),
    }


def _runtime_tcpmss_sources(raw: bytes) -> list[str]:
    """兼容内部调用名；校验完整双向规则后返回出站来源。"""

    return _runtime_tcpmss_rules(raw)["sources"]


def _wireguard_observation() -> dict[str, Any]:
    """读取 wg1 的 MTU、固定端点与 MSS clamp，不暴露任何密钥。"""

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
    configured_endpoints: list[str] = []
    configured_tcpmss_commands: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip().lower()
        normalized_value = value.strip()
        if section == "peer" and normalized_key == "endpoint":
            configured_endpoints.append(normalized_value)
            continue
        if section != "interface":
            continue
        if normalized_key == "mtu":
            try:
                configured_values.append(int(normalized_value))
            except ValueError as error:
                raise Arm64EnvironmentReceiptError("ARM64 wg1 配置 MTU 非整数") from error
        elif normalized_key in {"postup", "postdown"} and "TCPMSS" in normalized_value:
            configured_tcpmss_commands.append((normalized_key, normalized_value))
    if configured_values != [EXPECTED_WG1_MTU]:
        raise Arm64EnvironmentReceiptError(
            f"ARM64 wg1 配置 MTU 必须唯一且等于 "
            f"{EXPECTED_EGRESS_PROVIDER} {EXPECTED_WG1_MTU}"
        )
    if configured_endpoints != [EXPECTED_WG1_ENDPOINT]:
        raise Arm64EnvironmentReceiptError(
            f"ARM64 wg1 持久 Endpoint 必须唯一且等于 {EXPECTED_WG1_ENDPOINT}"
        )
    if sorted(configured_tcpmss_commands) != _expected_tcpmss_config_commands():
        raise Arm64EnvironmentReceiptError(
            "ARM64 wg1 持久 TCPMSS 规则缺失、重复或不具备幂等上下线闭环"
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
    endpoint_raw = _run(
        ["wg", "show", WIREGUARD_INTERFACE, "endpoints"],
        "ARM64 wg1 运行时 Endpoint 读取",
        operation="arm64:wg1:endpoint",
    )
    try:
        endpoint_lines = endpoint_raw.decode("ascii").splitlines()
    except UnicodeError as error:
        raise Arm64EnvironmentReceiptError("ARM64 wg1 运行时 Endpoint 编码非法") from error
    endpoint_fields = endpoint_lines[0].split() if len(endpoint_lines) == 1 else []
    runtime_endpoint = endpoint_fields[1] if len(endpoint_fields) == 2 else ""
    if runtime_endpoint != EXPECTED_WG1_ENDPOINT:
        raise Arm64EnvironmentReceiptError(
            f"ARM64 wg1 运行时 Endpoint 不是固定 IPv4 {EXPECTED_WG1_ENDPOINT}"
        )
    iptables_raw = _run(
        ["iptables-save", "-t", "mangle"],
        "ARM64 TCPMSS 运行时规则读取",
        operation="arm64:wg1:tcpmss",
    )
    runtime_tcpmss = _runtime_tcpmss_rules(iptables_raw)
    return {
        "interface": WIREGUARD_INTERFACE,
        "egress_provider": EXPECTED_EGRESS_PROVIDER,
        "configured_mtu": configured_values[0],
        "runtime_mtu": runtime_mtu,
        "expected_mtu": EXPECTED_WG1_MTU,
        "configured_endpoint": configured_endpoints[0],
        "runtime_endpoint": runtime_endpoint,
        "expected_endpoint": EXPECTED_WG1_ENDPOINT,
        "configured_tcpmss_sources": sorted(EXPECTED_TCPMSS_SOURCES),
        "runtime_tcpmss_sources": runtime_tcpmss["sources"],
        "expected_tcpmss_sources": sorted(EXPECTED_TCPMSS_SOURCES),
        "configured_tcpmss_destinations": sorted(
            EXPECTED_TCPMSS_DESTINATIONS
        ),
        "runtime_tcpmss_destinations": runtime_tcpmss["destinations"],
        "expected_tcpmss_destinations": sorted(
            EXPECTED_TCPMSS_DESTINATIONS
        ),
        "expected_tcp_mss": EXPECTED_TCP_MSS,
        "config_path": str(config),
        "config_sha256": _sha256_bytes(raw),
    }


def _collect_facts(*, phase: str, subject_id: str, rust_tls_codex_version: str) -> dict[str, Any]:
    """只读采集真实抓包拓扑与资源，并在前后核验当前授权出口策略。

    Rust TLS 探针使用调用方给出的本轮目标版本；探针目标先于任何宿主读取完成校验。
    """

    if phase not in PHASES:
        raise Arm64EnvironmentReceiptError(f"phase 必须属于 {sorted(PHASES)}")
    _safe_id(subject_id, "subject_id")
    rust_tls_target = rust_tls_probe_target(rust_tls_codex_version)
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
    before = require_runtime_egress()
    if not set(CONTAINER_CONTRACTS).issubset(before["policy"]["services"]):
        raise Arm64EnvironmentReceiptError("运行时出口策略未保护升级所需的两个容器")
    containers = [_container_observation(name, runtime_egress=before) for name in sorted(CONTAINER_CONTRACTS)]
    rust_tls = _rust_tls_readiness_observation(rust_tls_target)
    after, checked_at = _checked_runtime_egress()
    if before["policy_sha256"] != after["policy_sha256"] or any(
        item["container_id"] != after["runtime"]["services"][item["name"]]["container_id"] for item in containers
    ):
        raise Arm64EnvironmentReceiptError("环境采集期间策略或容器身份变化，须重新准入")
    producer = Path(__file__).resolve()
    return {
        "schema_version": FACTS_SCHEMA,
        "phase": phase,
        "subject_id": subject_id,
        "observed_at_utc": checked_at.isoformat(timespec="microseconds"),
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
        "runtime_egress": after,
        "containers": containers,
        "rust_tls_readiness": rust_tls,
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
    rust_tls_codex_version: str,
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
        return _collect_facts(phase=phase, subject_id=subject_id, rust_tls_codex_version=rust_tls_codex_version)
    finally:
        _ACTIVE_DEADLINE = previous_deadline
        _ACTIVE_HEARTBEAT = previous_heartbeat


def _validate_container(
    value: Any,
    expected_name: str,
    *,
    expected_public_egress: str,
    producer_version: str,
    runtime_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields = {
        "name",
        "container_id",
        "image_id",
        "selected_network",
        "network_bindings",
        "default_route",
        "public_egress",
        "raw_sha256",
    }
    if producer_version in {"5", *RUST_TLS_READINESS_PRODUCER_VERSIONS}:
        fields.add("tls_readiness")
    container = _expect(
        value,
        fields,
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
    egress_matches = (egress.get("url") in runtime_policy["probe_urls"]
                      and egress.get("ip_address") in runtime_policy["allowed_public_ipv4"]) if runtime_policy else (
                          egress.get("url") == PUBLIC_EGRESS_URL and egress.get("ip_address") == expected_public_egress)
    if not egress_matches:
        raise Arm64EnvironmentReceiptError(
            f"{expected_name} 公网出口不符合本份收据绑定的出口合同"
        )
    if not SHA256_RE.fullmatch(str(egress.get("response_sha256", ""))):
        raise Arm64EnvironmentReceiptError(f"{expected_name} 出口响应摘要非法")
    if producer_version in {"5", *RUST_TLS_READINESS_PRODUCER_VERSIONS}:
        readiness = container.get("tls_readiness")
        if not isinstance(readiness, list) or len(readiness) != len(
            TLS_READINESS_PROBES
        ):
            raise Arm64EnvironmentReceiptError(
                f"{expected_name} TLS 就绪探针没有完整覆盖冻结端点"
            )
        for observed, (probe_name, url, expected_status) in zip(
            readiness,
            TLS_READINESS_PROBES,
            strict=True,
        ):
            probe = _expect(
                observed,
                {
                    "name",
                    "url",
                    "expected_http_status",
                    "required_successes",
                    "attempts",
                },
                f"{expected_name}.tls_readiness.{probe_name}",
            )
            attempts = probe.get("attempts")
            if (
                probe.get("name") != probe_name
                or probe.get("url") != url
                or probe.get("expected_http_status") != expected_status
                or probe.get("required_successes") != TLS_READINESS_ATTEMPTS
                or not isinstance(attempts, list)
                or len(attempts) != TLS_READINESS_ATTEMPTS
            ):
                raise Arm64EnvironmentReceiptError(
                    f"{expected_name} {probe_name} TLS 连续成功次数不足"
                )
            for attempt_index, observed_attempt in enumerate(attempts, 1):
                attempt = _expect(
                    observed_attempt,
                    {
                        "attempt",
                        "http_status",
                        "remote_ip",
                        "tls_seconds",
                        "response_sha256",
                    },
                    f"{expected_name}.{probe_name}.attempt-{attempt_index}",
                )
                try:
                    ipaddress.ip_address(str(attempt.get("remote_ip", "")))
                except ValueError as error:
                    raise Arm64EnvironmentReceiptError(
                        f"{expected_name} {probe_name} 远端 IP 非法"
                    ) from error
                tls_seconds = attempt.get("tls_seconds")
                if (
                    attempt.get("attempt") != attempt_index
                    or attempt.get("http_status") != expected_status
                    or isinstance(tls_seconds, bool)
                    or not isinstance(tls_seconds, (int, float))
                    or not 0 < tls_seconds <= 15
                    or not SHA256_RE.fullmatch(
                        str(attempt.get("response_sha256", ""))
                    )
                ):
                    raise Arm64EnvironmentReceiptError(
                        f"{expected_name} {probe_name} TLS 第 {attempt_index} 次未通过"
                    )
    raw_sha = _expect(
        container.get("raw_sha256"),
        {"docker_inspect", "proc_net_route"},
        f"{expected_name}.raw_sha256",
    )
    if any(not SHA256_RE.fullmatch(str(raw_sha.get(key, ""))) for key in raw_sha):
        raise Arm64EnvironmentReceiptError(f"{expected_name} 原始事实摘要非法")
    return container


def _validate_rust_tls_readiness(value: Any, *, producer_version: str) -> dict[str, Any]:
    """校验无凭据 Codex Doctor 留下的最小、无秘密 Rust TLS 事实。

    v8 的探针目标来自采集参数（本轮目标版本），此处只核对版本形态与派生路径；v6～v7 历史事实
    按原合同冻结的 0.154.0 目标重放。
    """

    observation = _expect(
        value,
        {
            "container",
            "binary",
            "codex_version",
            "isolated_empty_codex_home",
            "process_exit_code",
            "overall_status",
            "failed_check_ids",
            "checks",
            "duration_seconds",
            "report_sha256",
            "report_bytes",
        },
        "rust_tls_readiness",
    )
    expected_checks = {
        "auth.credentials": "fail",
        "config.load": "ok",
        "network.provider_reachability": "ok",
    }
    checks = _expect(
        observation.get("checks"),
        set(expected_checks),
        "rust_tls_readiness.checks",
    )
    duration = observation.get("duration_seconds")
    report_bytes = observation.get("report_bytes")
    if producer_version == PRODUCER_VERSION:
        expected_target = rust_tls_probe_target(observation.get("codex_version"))
    else:
        expected_target = {"container": RUST_TLS_PROBE_CONTAINER, "binary": LEGACY_RUST_TLS_PROBE_BINARY,
                           "codex_version": LEGACY_RUST_TLS_PROBE_CODEX_VERSION}
    if (
        observation.get("container") != expected_target["container"]
        or observation.get("binary") != expected_target["binary"]
        or observation.get("codex_version") != expected_target["codex_version"]
        or observation.get("isolated_empty_codex_home") is not True
        or observation.get("process_exit_code") != 1
        or observation.get("overall_status") != "fail"
        or observation.get("failed_check_ids") != ["auth.credentials"]
        or checks != expected_checks
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not 0 < float(duration) <= RUST_TLS_PROBE_TIMEOUT_SECONDS
        or isinstance(report_bytes, bool)
        or not isinstance(report_bytes, int)
        or not 0 < report_bytes <= MAX_JSON_BYTES
        or not SHA256_RE.fullmatch(str(observation.get("report_sha256", "")))
    ):
        raise Arm64EnvironmentReceiptError(
            "Codex Rust TLS 就绪事实与冻结合同不一致"
        )
    return observation


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
    if producer_version in {"3", "4", "5", *LEGACY_FULL_WIREGUARD_PRODUCER_VERSIONS}:
        fact_fields.add("wireguard")
    if producer_version == PRODUCER_VERSION:
        fact_fields.add("runtime_egress")
    if producer_version in RUST_TLS_READINESS_PRODUCER_VERSIONS:
        fact_fields.add("rust_tls_readiness")
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
        expected_public_egress = "运行时授权策略"
    elif producer_version == "7":
        expected_contract = LEGACY_V7_NETWORK_CONTRACT_SHA256
        expected_public_egress = EXPECTED_PUBLIC_EGRESS
    elif producer_version == "6":
        expected_contract = LEGACY_V6_NETWORK_CONTRACT_SHA256
        expected_public_egress = EXPECTED_PUBLIC_EGRESS
    elif producer_version == "5":
        expected_contract = LEGACY_V5_NETWORK_CONTRACT_SHA256
        expected_public_egress = EXPECTED_PUBLIC_EGRESS
    elif producer_version == "4":
        expected_contract = LEGACY_V4_NETWORK_CONTRACT_SHA256
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
    if filesystem["total_bytes"] <= 0 or filesystem["used_bytes"] < 0:
        raise Arm64EnvironmentReceiptError("根文件系统容量事实非法")
    resource_watermark_reached = (
        filesystem["available_bytes"] < ROOT_MIN_AVAILABLE_BYTES
        or filesystem["used_percent"] > ROOT_MAX_USED_PERCENT
    )
    # v7 起的 ``*_after`` 收尾阶段允许降级记录；v6 及更早的历史收据
    # 按生成时的全阶段硬门禁重放，准入阶段（p0／*_before）任何版本都硬失败。
    resource_gate_degradable = (
        producer_version in {"7", PRODUCER_VERSION}
        and str(facts["phase"]).endswith(RESOURCE_GATE_DEGRADABLE_PHASE_SUFFIX)
    )
    if resource_watermark_reached and not resource_gate_degradable:
        raise Arm64EnvironmentReceiptError(
            "ARM64 根文件系统达到停线水位（使用率须低于 70%，可用空间须不少于 30 GiB）"
        )
    containers = facts.get("containers")
    expected_names = sorted(CONTAINER_CONTRACTS)
    if not isinstance(containers, list) or [item.get("name") for item in containers if isinstance(item, dict)] != expected_names:
        raise Arm64EnvironmentReceiptError("容器事实必须唯一且完整覆盖固定双容器")
    runtime_policy = None
    if producer_version == PRODUCER_VERSION:
        runtime = _expect(facts["runtime_egress"], {"policy", "policy_sha256", "runtime"}, "运行时出口事实")
        runtime_policy = validate_egress_policy(runtime["policy"])
        if runtime["policy_sha256"] != _sha256_bytes(_canonical(runtime_policy)):
            raise Arm64EnvironmentReceiptError("运行时出口策略摘要不一致")
        observed_epoch = datetime.fromisoformat(facts["observed_at_utc"].replace("Z", "+00:00")).timestamp()
        validate_egress_status(runtime_policy, runtime["runtime"], now_epoch=observed_epoch)
        if not set(expected_names).issubset(runtime["runtime"]["services"]):
            raise Arm64EnvironmentReceiptError("运行时出口事实未覆盖固定双容器")
        for container in containers:
            service = runtime["runtime"]["services"][container["name"]]
            addresses = {binding["ipv4_address"] for binding in container["network_bindings"]}
            if (container["container_id"] != service["container_id"]
                    or addresses != {binding["source_ipv4"] for binding in service["network_bindings"]}):
                raise Arm64EnvironmentReceiptError("容器网络事实与出口守护登记不一致")
    normalized = [
        _validate_container(
            item,
            name,
            expected_public_egress=expected_public_egress,
            producer_version=producer_version,
            runtime_policy=runtime_policy,
        )
        for item, name in zip(containers, expected_names, strict=True)
    ]
    if producer_version in RUST_TLS_READINESS_PRODUCER_VERSIONS:
        _validate_rust_tls_readiness(facts.get("rust_tls_readiness"), producer_version=producer_version)
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
    elif producer_version == "4":
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
                "历史 ARM64 wg1 持久配置或运行时 MTU 与 BWG 冻结值不一致"
            )
    elif producer_version == "5":
        wireguard = _expect(
            facts.get("wireguard"),
            {
                "interface",
                "egress_provider",
                "configured_mtu",
                "runtime_mtu",
                "expected_mtu",
                "configured_endpoint",
                "runtime_endpoint",
                "expected_endpoint",
                "configured_tcpmss_sources",
                "runtime_tcpmss_sources",
                "expected_tcpmss_sources",
                "expected_tcp_mss",
                "config_path",
                "config_sha256",
            },
            "wireguard",
        )
        expected_sources = sorted(EXPECTED_TCPMSS_SOURCES)
        if (
            wireguard.get("interface") != WIREGUARD_INTERFACE
            or wireguard.get("egress_provider") != EXPECTED_EGRESS_PROVIDER
            or wireguard.get("configured_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("runtime_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("expected_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("configured_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("runtime_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("expected_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("configured_tcpmss_sources") != expected_sources
            or wireguard.get("runtime_tcpmss_sources") != expected_sources
            or wireguard.get("expected_tcpmss_sources") != expected_sources
            or wireguard.get("expected_tcp_mss") != EXPECTED_TCP_MSS
            or wireguard.get("config_path") != str(WIREGUARD_CONFIG)
            or not SHA256_RE.fullmatch(str(wireguard.get("config_sha256", "")))
        ):
            raise Arm64EnvironmentReceiptError(
                "历史 ARM64 wg1 Endpoint、MTU 或 TCPMSS 与 BWG 冻结值不一致"
            )
    elif producer_version in LEGACY_FULL_WIREGUARD_PRODUCER_VERSIONS:
        wireguard = _expect(
            facts.get("wireguard"),
            {
                "interface",
                "egress_provider",
                "configured_mtu",
                "runtime_mtu",
                "expected_mtu",
                "configured_endpoint",
                "runtime_endpoint",
                "expected_endpoint",
                "configured_tcpmss_sources",
                "runtime_tcpmss_sources",
                "expected_tcpmss_sources",
                "configured_tcpmss_destinations",
                "runtime_tcpmss_destinations",
                "expected_tcpmss_destinations",
                "expected_tcp_mss",
                "config_path",
                "config_sha256",
            },
            "wireguard",
        )
        expected_sources = sorted(EXPECTED_TCPMSS_SOURCES)
        expected_destinations = sorted(EXPECTED_TCPMSS_DESTINATIONS)
        if (
            wireguard.get("interface") != WIREGUARD_INTERFACE
            or wireguard.get("egress_provider") != EXPECTED_EGRESS_PROVIDER
            or wireguard.get("configured_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("runtime_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("expected_mtu") != EXPECTED_WG1_MTU
            or wireguard.get("configured_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("runtime_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("expected_endpoint") != EXPECTED_WG1_ENDPOINT
            or wireguard.get("configured_tcpmss_sources") != expected_sources
            or wireguard.get("runtime_tcpmss_sources") != expected_sources
            or wireguard.get("expected_tcpmss_sources") != expected_sources
            or wireguard.get("configured_tcpmss_destinations")
            != expected_destinations
            or wireguard.get("runtime_tcpmss_destinations")
            != expected_destinations
            or wireguard.get("expected_tcpmss_destinations")
            != expected_destinations
            or wireguard.get("expected_tcp_mss") != EXPECTED_TCP_MSS
            or wireguard.get("config_path") != str(WIREGUARD_CONFIG)
            or not SHA256_RE.fullmatch(str(wireguard.get("config_sha256", "")))
        ):
            raise Arm64EnvironmentReceiptError(
                "ARM64 wg1 Endpoint、MTU 或双向 TCPMSS 与 BWG 冻结值不一致"
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

    if producer_version == PRODUCER_VERSION:
        continuity_identity = environment_equivalence_projection(facts)
    elif producer_version == "1":
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
    resource_gate: dict[str, Any] = {
        "used_percent": filesystem["used_percent"],
        "available_bytes": filesystem["available_bytes"],
        "passed": not resource_watermark_reached,
    }
    if resource_watermark_reached:
        resource_gate["degraded"] = True
    return {
        "producer_version": producer_version,
        "continuity_identity_sha256": _sha256_bytes(_canonical(continuity_identity)),
        "equivalence_identity_sha256": _sha256_bytes(_canonical(environment_equivalence_projection(facts))),
        "resource_gate": resource_gate,
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
    receipt = {
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
    if validation["producer_version"] == PRODUCER_VERSION:
        receipt["environment_equivalence"] = {
            "schema_version": EGRESS_EQUIVALENCE_SCHEMA,
            "sha256": validation["equivalence_identity_sha256"],
        }
        receipt["runtime_egress"] = {
            "policy_sha256": facts["runtime_egress"]["policy_sha256"],
            "status_sha256": _sha256_bytes(_canonical(facts["runtime_egress"]["runtime"])),
        }
        receipt["rust_tls_probe"] = {
            "binary": facts["rust_tls_readiness"]["binary"],
            "codex_version": facts["rust_tls_readiness"]["codex_version"],
        }
    return receipt


def build_receipt(root: Path, facts_relative: str) -> dict[str, Any]:
    """只使用当前 producer 生成新收据。"""

    return _build_receipt(root, facts_relative)


def collect(
    root: Path,
    output_relative: str,
    *,
    phase: str,
    subject_id: str,
    rust_tls_codex_version: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    root = _private_root(root)
    output = _relative(root, output_relative, "facts output")
    facts = collect_facts(
        phase=phase,
        subject_id=subject_id,
        rust_tls_codex_version=rust_tls_codex_version,
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


def receipt_equivalence_sha256(root: Path, receipt: dict[str, Any]) -> str:
    """先按原 producer 逐字重建收据，再投影等价身份；不改写历史收据或读取当前策略。

    原 continuity_identity_sha256 仍用于旧记录自身的字节绑定；只有不同时间环境
    之间的等价比较使用此 API。镜像和抓包拓扑的变化仍然会产生不同的摘要。
    """

    root = _private_root(root)
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT_SCHEMA
            or not isinstance(receipt.get("facts"), dict) or not isinstance(receipt["facts"].get("path"), str)
            or not isinstance(receipt.get("producer"), dict)):
        raise Arm64EnvironmentReceiptError("环境等价比较缺少完整的 facts／producer 收据")
    expected = _build_receipt(root, receipt["facts"]["path"], replay_producer=receipt["producer"])
    if _canonical(receipt) != _canonical(expected):
        raise Arm64EnvironmentReceiptError("未经原 producer 完整重放的收据不能参与环境等价比较")
    facts, raw = _load_json(_relative(root, receipt["facts"]["path"], "等价投影 facts"), "等价投影 facts")
    if _sha256_bytes(raw) != receipt["facts"]["sha256"] or len(raw) != receipt["facts"]["bytes"]:
        raise Arm64EnvironmentReceiptError("等价投影读取期间 facts 发生变化")
    return _sha256_bytes(_canonical(environment_equivalence_projection(facts)))


def receipts_equivalent(before_root: Path, before: dict[str, Any], after_root: Path, after: dict[str, Any]) -> bool:
    """统一跨时间环境比较入口；两侧必须各自具备可按原合同重放的完整事实。"""

    return receipt_equivalence_sha256(before_root, before) == receipt_equivalence_sha256(after_root, after)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="只读采集 ARM64 原始环境事实")
    collect_parser.add_argument("--evidence-root", type=Path, required=True)
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--phase", choices=sorted(PHASES), required=True)
    collect_parser.add_argument("--subject-id", required=True)
    collect_parser.add_argument("--rust-tls-codex-version", required=True,
                                help="Rust TLS 就绪探针使用的本轮目标客户端版本（x.y.z）")
    finalize_parser = commands.add_parser("finalize", help="封存 ARM64 环境收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放 ARM64 环境收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    commands.add_parser("egress-check", help="核验当前持续守护和逐容器出口；历史收据不能替代实时准入")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "egress-check":
            result = require_runtime_egress()
            print(json.dumps({"status": "passed", "policy_sha256": result["policy_sha256"]}, sort_keys=True))
            return 0
        if arguments.command == "collect":
            result = collect(
                arguments.evidence_root,
                arguments.output,
                phase=arguments.phase,
                subject_id=arguments.subject_id,
                rust_tls_codex_version=arguments.rust_tls_codex_version,
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
