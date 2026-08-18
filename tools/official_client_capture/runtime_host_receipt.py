#!/usr/bin/env python3
"""在 Docker 宿主机生成官方客户端抓包运行镜像凭据。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.identity import (  # noqa: E402
    capture_source_bundle_identity,
)
from tools.official_client_capture.capturelib.model import (  # noqa: E402
    ConfigurationError,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    file_sha256,
    secure_write_json,
)


SCHEMA_VERSION = "official-client-runtime-host-receipt/v1"
PRODUCER_VERSION = "1"
IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_REFERENCE_RE = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")


def _docker_json(arguments: list[str]) -> Any:
    completed = subprocess.run(
        ["docker", *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ConfigurationError("Docker 身份查询失败。")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("Docker 身份查询结果不是合法 JSON。") from error


def _docker_version() -> dict[str, str]:
    payload = _docker_json(
        [
            "version",
            "--format",
            "{{json .Server}}",
        ]
    )
    if not isinstance(payload, dict):
        raise ConfigurationError("Docker Server 版本信息格式非法。")
    return {
        "version": str(payload.get("Version", "")),
        "os": str(payload.get("Os", "")),
        "arch": str(payload.get("Arch", "")),
    }


def build_receipt(
    *, container: str, runtime_image: str, tool_root: Path, run_nonce: str
) -> dict[str, Any]:
    """从宿主 Docker daemon 读取并交叉验证运行身份。"""

    if not IMAGE_REFERENCE_RE.fullmatch(runtime_image):
        raise ConfigurationError("--runtime-image 必须是 repository@sha256:<digest>。")
    if not re.fullmatch(r"[a-f0-9]{64}", run_nonce):
        raise ConfigurationError("--run-nonce 必须是 64 位小写十六进制。")
    containers = _docker_json(["inspect", container])
    if not isinstance(containers, list) or len(containers) != 1:
        raise ConfigurationError("Docker 容器身份结果格式非法。")
    item = containers[0]
    if not isinstance(item, dict):
        raise ConfigurationError("Docker 容器身份结果格式非法。")
    container_id = str(item.get("Id", ""))
    image_id = str(item.get("Image", ""))
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    config = item.get("Config") if isinstance(item.get("Config"), dict) else {}
    host_config = (
        item.get("HostConfig") if isinstance(item.get("HostConfig"), dict) else {}
    )
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
    hostname = str(config.get("Hostname", ""))
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise ConfigurationError("Docker 容器 ID 格式非法。")
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ConfigurationError("Docker image config ID 格式非法。")
    if state.get("Running") is not True:
        raise ConfigurationError("目标抓包容器当前未运行。")
    # Docker 允许显式配置 hostname，因此 hostname 不一定是容器 ID
    # 的前缀。宿主侧只负责从同一份 inspect 结果固化两者；容器内
    # 再通过 mountinfo 的 Docker 容器 ID 与当前 hostname 双重交叉验证。
    if not hostname:
        raise ConfigurationError("容器 hostname 为空。")

    network_bindings = [
        {
            "name": name,
            "network_id": str(network.get("NetworkID", "")),
            "endpoint_id": str(network.get("EndpointID", "")),
            "gateway": str(network.get("Gateway", "")),
            "ip_address": str(network.get("IPAddress", "")),
            "global_ipv6_address": str(network.get("GlobalIPv6Address", "")),
        }
        for name, network in sorted(networks_raw.items())
        if isinstance(network, dict)
    ]
    hosts_path = Path(str(item.get("HostsPath", "")))
    resolv_path = Path(str(item.get("ResolvConfPath", "")))
    if (
        not hosts_path.is_absolute()
        or hosts_path.is_symlink()
        or not hosts_path.is_file()
        or not resolv_path.is_absolute()
        or resolv_path.is_symlink()
        or not resolv_path.is_file()
    ):
        raise ConfigurationError("Docker 管理的 hosts/resolv.conf 路径不可复核。")

    images = _docker_json(["image", "inspect", image_id])
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ConfigurationError("Docker 镜像身份结果格式非法。")
    repo_digests = images[0].get("RepoDigests")
    if (
        not isinstance(repo_digests, list)
        or runtime_image not in repo_digests
        or any(not isinstance(value, str) for value in repo_digests)
    ):
        raise ConfigurationError("运行镜像的 RepoDigests 不含指定不可变引用。")

    producer = Path(__file__).resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "run_nonce": run_nonce,
        "container": {
            "name": container,
            "id": container_id,
            "hostname": hostname,
            "started_at_utc": str(state.get("StartedAt", "")),
            "network": {
                "mode": str(host_config.get("NetworkMode", "")),
                "bindings": network_bindings,
                "hosts_sha256": file_sha256(hosts_path),
                "resolv_conf_sha256": file_sha256(resolv_path),
            },
        },
        "runtime_image_reference": runtime_image,
        "runtime_image_id": image_id,
        "repo_digest_verified": True,
        "capture_source_bundle": capture_source_bundle_identity(tool_root),
        "producer": {
            "name": producer.name,
            "path": str(producer),
            "version": PRODUCER_VERSION,
            "sha256": file_sha256(producer),
        },
        "docker_server": _docker_version(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Docker 宿主机生成抓包运行镜像的可复核凭据。"
    )
    parser.add_argument("--container", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument(
        "--capture-tool-root",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--run-nonce",
        default="",
        help="可选的 64 位十六进制 nonce；省略时由宿主机安全生成。",
    )
    parser.add_argument(
        "--print-run-nonce",
        action="store_true",
        help="成功后只向 stdout 输出非秘密 run nonce。",
    )
    return parser


def main() -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args()
    try:
        nonce = arguments.run_nonce or secrets.token_hex(32)
        if arguments.output.exists() or arguments.output.is_symlink():
            raise ConfigurationError("宿主凭据输出已存在，拒绝覆盖。")
        payload = build_receipt(
            container=arguments.container,
            runtime_image=arguments.runtime_image,
            tool_root=arguments.capture_tool_root,
            run_nonce=nonce,
        )
        secure_write_json(arguments.output, payload)
        if arguments.print_run_nonce:
            print(nonce)
        return 0
    except (ConfigurationError, OSError, subprocess.SubprocessError) as error:
        print(f"宿主运行凭据生成失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
