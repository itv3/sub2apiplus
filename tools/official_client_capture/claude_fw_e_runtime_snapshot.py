#!/usr/bin/env python3
"""只读冻结并比较 Claude FW-E 期间的 Vircs 容器运行事实。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import (  # noqa: E402
    ConfigurationError,
    validate_safe_name,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    file_sha256,
    secure_write_json,
)


SCHEMA_SNAPSHOT = "claude-code-fw-e-runtime-snapshot/v1"
SCHEMA_COMPARISON = "claude-code-fw-e-runtime-comparison/v1"
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_LABEL_ALLOWLIST = {
    "org.opencontainers.image.revision",
    "org.opencontainers.image.version",
}
IMAGE_LABEL_PREFIXES = ("io.sub2apiplus.",)


class RuntimeSnapshotError(RuntimeError):
    """表示宿主运行事实无法复核或 before/after 发生漂移。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _docker_json(docker: Path, arguments: list[str]) -> Any:
    completed = subprocess.run(
        [str(docker), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeSnapshotError("Docker 只读身份查询失败。")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeSnapshotError("Docker 身份查询结果不是合法 JSON。") from error


def _container_snapshot(docker: Path, name: str) -> dict[str, Any]:
    payload = _docker_json(docker, ["inspect", name])
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeSnapshotError(f"容器身份查询不唯一：{name}")
    item = payload[0]
    container_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", ""))
    if not CONTAINER_ID_RE.fullmatch(container_id) or not IMAGE_ID_RE.fullmatch(image_id):
        raise RuntimeSnapshotError(f"容器或镜像身份非法：{name}")
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else None
    config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
    network_settings = (
        item.get("NetworkSettings")
        if isinstance(item.get("NetworkSettings"), dict)
        else {}
    )
    networks_raw = (
        network_settings.get("Networks")
        if isinstance(network_settings.get("Networks"), dict)
        else {}
    )
    ports_raw = (
        network_settings.get("Ports")
        if isinstance(network_settings.get("Ports"), dict)
        else {}
    )
    mounts: list[dict[str, Any]] = []
    for mount in item.get("Mounts", []):
        if not isinstance(mount, dict):
            continue
        mounts.append(
            {
                "type": str(mount.get("Type", "")),
                "source": str(mount.get("Source", "")),
                "destination": str(mount.get("Destination", "")),
                "mode": str(mount.get("Mode", "")),
                "rw": bool(mount.get("RW")),
                "propagation": str(mount.get("Propagation", "")),
            }
        )
    networks = [
        {
            "name": network_name,
            "network_id": str(network.get("NetworkID", "")),
            "endpoint_id": str(network.get("EndpointID", "")),
            "gateway": str(network.get("Gateway", "")),
            "ip_address": str(network.get("IPAddress", "")),
            "global_ipv6_address": str(network.get("GlobalIPv6Address", "")),
        }
        for network_name, network in sorted(networks_raw.items())
        if isinstance(network, dict)
    ]
    ports = [
        {
            "container_port": container_port,
            "bindings": sorted(
                [
                    {
                        "host_ip": str(binding.get("HostIp", "")),
                        "host_port": str(binding.get("HostPort", "")),
                    }
                    for binding in bindings or []
                    if isinstance(binding, dict)
                ],
                key=lambda binding: (binding["host_ip"], binding["host_port"]),
            ),
        }
        for container_port, bindings in sorted(ports_raw.items())
    ]
    images = _docker_json(docker, ["image", "inspect", image_id])
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise RuntimeSnapshotError(f"镜像身份查询不唯一：{image_id}")
    image = images[0]
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list):
        repo_digests = []
    image_config = image.get("Config") if isinstance(image.get("Config"), dict) else {}
    raw_labels = (
        image_config.get("Labels")
        if isinstance(image_config.get("Labels"), dict)
        else {}
    )
    image_labels = {
        str(key): str(value)
        for key, value in sorted(raw_labels.items())
        if str(key) in IMAGE_LABEL_ALLOWLIST
        or any(str(key).startswith(prefix) for prefix in IMAGE_LABEL_PREFIXES)
    }
    return {
        "name": name,
        "container_id": container_id,
        "image_id": image_id,
        "configured_image": str(config.get("Image", "")),
        "repo_digests": sorted(str(value) for value in repo_digests),
        # 只记录用于制品溯源的白名单标签。该字段不进入 before/after 稳定投影，
        # 以便旧版 before 快照仍可与部署后的只读快照比较。
        "image_labels": image_labels,
        "state": {
            "status": str(state.get("Status", "")),
            "running": state.get("Running") is True,
            "paused": state.get("Paused") is True,
            "restarting": state.get("Restarting") is True,
            "oom_killed": state.get("OOMKilled") is True,
            "dead": state.get("Dead") is True,
            "started_at_utc": str(state.get("StartedAt", "")),
            "health": (
                {
                    "status": str(health.get("Status", "")),
                    "failing_streak": int(health.get("FailingStreak", 0)),
                }
                if health is not None
                else None
            ),
        },
        "restart_count": int(item.get("RestartCount", 0)),
        "mounts": sorted(mounts, key=lambda mount: mount["destination"]),
        "networks": networks,
        "ports": ports,
    }


def build_snapshot(containers: list[str], phase: str) -> dict[str, Any]:
    docker_text = shutil.which("docker")
    if not docker_text:
        raise ConfigurationError("宿主机缺少 docker CLI。")
    docker = Path(docker_text)
    if docker.is_symlink() or not docker.is_file() or not os.access(docker, os.X_OK):
        raise ConfigurationError("docker CLI 不是可信可执行普通文件。")
    names = sorted(set(containers))
    if not names or len(names) != len(containers):
        raise ConfigurationError("--containers 必须是非空、无重复集合。")
    for name in names:
        validate_safe_name(name, "container name")
    server = _docker_json(docker, ["version", "--format", "{{json .Server}}"])
    if not isinstance(server, dict):
        raise RuntimeSnapshotError("Docker Server 身份格式非法。")
    return {
        "schema_version": SCHEMA_SNAPSHOT,
        "phase": phase,
        "observed_at_utc": _utc_now(),
        "host": {
            "node": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "docker": {
            "path": str(docker),
            "sha256": file_sha256(docker),
            "server_version": str(server.get("Version", "")),
            "server_os": str(server.get("Os", "")),
            "server_arch": str(server.get("Arch", "")),
        },
        "containers": [_container_snapshot(docker, name) for name in names],
        "producer": {
            "path": "tools/official_client_capture/claude_fw_e_runtime_snapshot.py",
            "sha256": file_sha256(Path(__file__)),
        },
    }


def _load_snapshot(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeSnapshotError(f"{label} 不是可信普通文件。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeSnapshotError(f"{label} 不是合法 JSON。") from error
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_SNAPSHOT:
        raise RuntimeSnapshotError(f"{label} schema 不匹配。")
    return value


def _stable_projection(container: dict[str, Any]) -> dict[str, Any]:
    state = container.get("state") if isinstance(container.get("state"), dict) else {}
    return {
        key: container.get(key)
        for key in (
            "name",
            "container_id",
            "image_id",
            "configured_image",
            "repo_digests",
            "restart_count",
            "mounts",
            "networks",
            "ports",
        )
    } | {
        "state": {
            key: state.get(key)
            for key in (
                "status",
                "running",
                "paused",
                "restarting",
                "oom_killed",
                "dead",
                "started_at_utc",
            )
        }
    }


def compare_snapshots(
    before: dict[str, Any], after: dict[str, Any], production_names: list[str]
) -> dict[str, Any]:
    before_by_name = {
        str(item.get("name")): item
        for item in before.get("containers", [])
        if isinstance(item, dict)
    }
    after_by_name = {
        str(item.get("name")): item
        for item in after.get("containers", [])
        if isinstance(item, dict)
    }
    names = sorted(set(production_names))
    if not names or len(names) != len(production_names):
        raise ConfigurationError("--production-containers 必须非空且无重复。")
    differences: list[dict[str, Any]] = []
    health: list[dict[str, Any]] = []
    for name in names:
        validate_safe_name(name, "production container")
        before_item = before_by_name.get(name)
        after_item = after_by_name.get(name)
        if before_item is None or after_item is None:
            differences.append({"container": name, "field": "presence"})
            continue
        before_projection = _stable_projection(before_item)
        after_projection = _stable_projection(after_item)
        if before_projection != after_projection:
            differences.append({"container": name, "field": "stable_runtime_projection"})
        before_health = before_item.get("state", {}).get("health")
        after_health = after_item.get("state", {}).get("health")
        acceptable = (
            after_item.get("state", {}).get("running") is True
            and (
                after_health is None
                or (
                    isinstance(after_health, dict)
                    and after_health.get("status") == "healthy"
                    and after_health.get("failing_streak") == 0
                )
            )
        )
        health.append(
            {
                "container": name,
                "before": before_health,
                "after": after_health,
                "acceptable": acceptable,
            }
        )
        if not acceptable:
            differences.append({"container": name, "field": "health"})
    return {
        "schema_version": SCHEMA_COMPARISON,
        "result": "passed" if not differences else "failed",
        "before_sha256": "",
        "after_sha256": "",
        "production_containers": names,
        "differences": differences,
        "health": health,
        "compared_at_utc": _utc_now(),
        "producer": {
            "path": "tools/official_client_capture/claude_fw_e_runtime_snapshot.py",
            "sha256": file_sha256(Path(__file__)),
        },
    }


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="生成只读运行态快照")
    snapshot.add_argument("--phase", choices=("before", "after"), required=True)
    snapshot.add_argument("--containers", required=True)
    snapshot.add_argument("--output", type=Path, required=True)
    compare = commands.add_parser("compare", help="比较生产 before/after")
    compare.add_argument("--before", type=Path, required=True)
    compare.add_argument("--after", type=Path, required=True)
    compare.add_argument("--production-containers", required=True)
    compare.add_argument("--output", type=Path, required=True)
    return parser


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.output.exists() or arguments.output.is_symlink():
        raise ConfigurationError("输出已存在，拒绝覆盖。")
    if arguments.command == "snapshot":
        result = build_snapshot(_csv(arguments.containers), arguments.phase)
    else:
        before = _load_snapshot(arguments.before, "before snapshot")
        after = _load_snapshot(arguments.after, "after snapshot")
        result = compare_snapshots(
            before, after, _csv(arguments.production_containers)
        )
        result["before_sha256"] = file_sha256(arguments.before)
        result["after_sha256"] = file_sha256(arguments.after)
    secure_write_json(arguments.output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        result = execute(_build_parser().parse_args(argv))
    except (
        ConfigurationError,
        RuntimeSnapshotError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"Claude FW-E 运行态快照拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("result", "passed") == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
