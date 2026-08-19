#!/usr/bin/env python3
"""在 Vircs Docker 宿主机串行执行冻结的 Claude FW-F 完整 Campaign。"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


SCHEMA_EXECUTION = "claude-code-fw-f-complete-execution/v1"
SCHEMA_EVENT = "claude-code-fw-f-complete-execution-event/v1"
CAMPAIGN_SCHEMA = "claude-code-fw-e-f-complete-campaign/v1"
RUNTIME_RECEIPT_SCHEMA = "official-client-runtime-host-receipt/v1"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class CompleteCampaignRunnerError(RuntimeError):
    """完整 Campaign 的宿主编排或边界验证失败。"""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CompleteCampaignRunnerError(f"{label} 不是可信绝对普通文件：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CompleteCampaignRunnerError(f"{label} 不是合法 JSON：{path}") from error
    if not isinstance(value, dict):
        raise CompleteCampaignRunnerError(f"{label} 顶层必须是对象：{path}")
    return value


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
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


def _write_new_json(path: Path, value: Any) -> None:
    _write_new(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        + b"\n",
    )


def _run_json(command: list[str], label: str) -> Any:
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise CompleteCampaignRunnerError(f"{label} 失败。")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise CompleteCampaignRunnerError(f"{label} 没有返回合法 JSON。") from error


def _require_safe_name(value: str, label: str) -> None:
    if not SAFE_NAME_RE.fullmatch(value):
        raise CompleteCampaignRunnerError(f"{label} 格式非法：{value!r}")


def _validate_campaign(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise CompleteCampaignRunnerError("--campaign-root 必须是可信绝对目录。")
    campaign = _load_json(root / "campaign.json", "Campaign")
    catalog = _load_json(root / "scenario-catalog.json", "场景目录")
    denominator = _load_json(root / "candidate-denominator.json", "候选分母")
    if campaign.get("schema_version") != CAMPAIGN_SCHEMA:
        raise CompleteCampaignRunnerError("Campaign schema 不匹配。")
    if campaign.get("status") != "frozen_not_executed":
        raise CompleteCampaignRunnerError("只允许执行 frozen_not_executed Campaign。")
    campaign_id = str(campaign.get("campaign_id", ""))
    _require_safe_name(campaign_id, "campaign_id")
    if catalog.get("campaign_id") != campaign_id or denominator.get("campaign_id") != campaign_id:
        raise CompleteCampaignRunnerError("Campaign 三份冻结清单身份不一致。")
    catalog_binding = campaign.get("scenario_catalog")
    denominator_binding = campaign.get("candidate_denominator")
    if not isinstance(catalog_binding, dict) or not isinstance(denominator_binding, dict):
        raise CompleteCampaignRunnerError("Campaign 缺少目录或候选分母绑定。")
    if _sha256_file(root / "scenario-catalog.json") != catalog_binding.get("sha256"):
        raise CompleteCampaignRunnerError("场景目录摘要漂移。")
    if _sha256_file(root / "candidate-denominator.json") != denominator_binding.get("sha256"):
        raise CompleteCampaignRunnerError("候选分母摘要漂移。")
    if denominator.get("total_orthogonal_candidates") != 593:
        raise CompleteCampaignRunnerError("候选分母不再是冻结的 593 项。")
    expected_counts = {
        "target_send_points": 331,
        "source_mechanisms_2_1_88": 102,
        "hitcc_documents_2_1_197": 71,
        "historical_rules": 57,
        "semantic_candidate_families": 32,
    }
    if denominator.get("counts") != expected_counts:
        raise CompleteCampaignRunnerError("候选分母五组计数漂移。")
    probes = catalog.get("probes")
    if not isinstance(probes, list) or len(probes) != 77:
        raise CompleteCampaignRunnerError("正式场景必须恰好为 77 个。")
    probe_ids: list[str] = []
    for probe in probes:
        if not isinstance(probe, dict):
            raise CompleteCampaignRunnerError("场景目录包含非对象条目。")
        probe_id = str(probe.get("probe_id", ""))
        _require_safe_name(probe_id, "probe_id")
        probe_ids.append(probe_id)
    if len(probe_ids) != len(set(probe_ids)):
        raise CompleteCampaignRunnerError("场景目录 probe_id 不唯一。")
    policy = campaign.get("execution_policy")
    required_policy = {
        "all_candidates_require_target_measurement": True,
        "attempts_are_append_only": True,
        "existing_capture_cli_reuse_allowed": False,
        "failed_attempts_are_preserved": True,
        "isolated_temporary_container_required": True,
        "production_container_or_network_mutation_allowed": False,
        "telemetry_or_nonessential_absence_generates_rule": False,
        "unmeasured_feature_boundary_allowed": False,
    }
    if policy != required_policy:
        raise CompleteCampaignRunnerError("Campaign 执行策略漂移。")
    return campaign, probes


def _validate_source(root: Path, campaign: dict[str, Any]) -> Path:
    tool_root = root / "source/tools/official_client_capture"
    binding = campaign.get("capture_source_bundle")
    if not isinstance(binding, dict) or not isinstance(binding.get("files"), list):
        raise CompleteCampaignRunnerError("Campaign 缺少执行源绑定。")
    for item in binding["files"]:
        if not isinstance(item, dict):
            raise CompleteCampaignRunnerError("执行源绑定条目非法。")
        relative = Path(str(item.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise CompleteCampaignRunnerError("执行源绑定路径越界。")
        path = tool_root / relative
        if path.is_symlink() or not path.is_file():
            raise CompleteCampaignRunnerError(f"执行源缺失：{relative}")
        if path.stat().st_size != item.get("size") or _sha256_file(path) != item.get("sha256"):
            raise CompleteCampaignRunnerError(f"执行源摘要漂移：{relative}")
    runtime_receipt = tool_root / "runtime_host_receipt.py"
    relay = tool_root / "claude_fw_e_relay.py"
    runner = tool_root / "claude_fw_f_complete_runner.py"
    if (
        not runtime_receipt.is_file()
        or not relay.is_file()
        or not runner.is_file()
    ):
        raise CompleteCampaignRunnerError(
            "Campaign source 缺少宿主收据、R 执行器或正式编排器。"
        )
    return tool_root


def _docker_inspect(container: str) -> dict[str, Any]:
    _require_safe_name(container, "container")
    value = _run_json(["docker", "inspect", container], f"检查容器 {container}")
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise CompleteCampaignRunnerError(f"容器检查结果非法：{container}")
    return value[0]


def _mount_by_destination(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mounts = item.get("Mounts")
    if not isinstance(mounts, list):
        return {}
    return {
        str(mount.get("Destination", "")): mount
        for mount in mounts
        if isinstance(mount, dict)
    }


def _validate_container(
    *,
    item: dict[str, Any],
    campaign_root: Path,
    synthetic: bool,
    forbidden_networks: set[str],
) -> None:
    state = item.get("State") if isinstance(item.get("State"), dict) else {}
    host = item.get("HostConfig") if isinstance(item.get("HostConfig"), dict) else {}
    if state.get("Running") is not True:
        raise CompleteCampaignRunnerError("抓包容器未运行。")
    network_mode = str(host.get("NetworkMode", ""))
    networks = (
        item.get("NetworkSettings", {}).get("Networks", {})
        if isinstance(item.get("NetworkSettings"), dict)
        else {}
    )
    network_names = set(networks) if isinstance(networks, dict) else set()
    if synthetic:
        none_binding = networks.get("none", {}) if isinstance(networks, dict) else {}
        has_routing_identity = bool(
            isinstance(none_binding, dict)
            and any(
                none_binding.get(key)
                for key in ("Gateway", "IPAddress", "IPv6Gateway", "GlobalIPv6Address")
            )
        )
        if network_mode != "none" or network_names not in (set(), {"none"}) or has_routing_identity:
            raise CompleteCampaignRunnerError("合成容器必须使用 --network none。")
    else:
        if network_mode in {"", "none", "host", "default", "bridge"}:
            raise CompleteCampaignRunnerError("真实取证容器必须使用独立命名网络。")
        if network_names != {network_mode}:
            raise CompleteCampaignRunnerError("真实取证容器网络绑定不闭合。")
    if network_names & forbidden_networks or network_mode in forbidden_networks:
        raise CompleteCampaignRunnerError("抓包容器连接了生产网络。")
    mounts = _mount_by_destination(item)
    required = {
        "/campaign": (str(campaign_root), True),
        "/campaign/source": (str(campaign_root / "source"), False),
        "/campaign/campaign.json": (str(campaign_root / "campaign.json"), False),
        "/campaign/scenario-catalog.json": (
            str(campaign_root / "scenario-catalog.json"),
            False,
        ),
        "/campaign/candidate-denominator.json": (
            str(campaign_root / "candidate-denominator.json"),
            False,
        ),
        "/opt/claude": (None, False),
        "/run/claude-secrets": (None, False),
        "/run/mitm/ca.pem": (None, False),
        "/run/mitm/ca-cert.pem": (None, False),
        "/etc/hosts": (None, True),
    }
    for destination, (expected_source, writable) in required.items():
        mount = mounts.get(destination)
        if not isinstance(mount, dict):
            raise CompleteCampaignRunnerError(f"抓包容器缺少挂载：{destination}")
        if expected_source is not None and mount.get("Source") != expected_source:
            raise CompleteCampaignRunnerError(f"抓包容器挂载源错误：{destination}")
        if mount.get("RW") is not writable:
            raise CompleteCampaignRunnerError(f"抓包容器挂载权限错误：{destination}")
    secret_source = Path(str(mounts["/run/claude-secrets"].get("Source", "")))
    hosts_source = Path(str(mounts["/etc/hosts"].get("Source", "")))
    if not secret_source.is_relative_to(Path("/dev/shm")):
        raise CompleteCampaignRunnerError("凭据副本不在宿主内存文件系统。")
    if not hosts_source.is_relative_to(Path("/dev/shm")):
        raise CompleteCampaignRunnerError("隔离 hosts 不在宿主内存文件系统。")
    if campaign_root.name not in secret_source.name:
        raise CompleteCampaignRunnerError("凭据副本未绑定当前 Campaign 身份。")
    if campaign_root.name not in hosts_source.name:
        raise CompleteCampaignRunnerError("隔离 hosts 未绑定当前 Campaign 身份。")
    tmpfs = host.get("Tmpfs") if isinstance(host.get("Tmpfs"), dict) else {}
    if "/tmp" not in tmpfs or "/dev/shm" not in tmpfs:
        raise CompleteCampaignRunnerError("抓包容器缺少独立 /tmp 或 /dev/shm tmpfs。")


def _receipt_command(
    *,
    tool_root: Path,
    container: str,
    runtime_image: str,
    receipt: Path,
    nonce: str,
) -> list[str]:
    return [
        sys.executable,
        str(tool_root / "runtime_host_receipt.py"),
        "--container",
        container,
        "--runtime-image",
        runtime_image,
        "--capture-tool-root",
        str(tool_root),
        "--output",
        str(receipt),
        "--run-nonce",
        nonce,
    ]


def _capture_command(
    *,
    container: str,
    probe: dict[str, Any],
    attempt_root: str,
    receipt: str,
    receipt_sha256: str,
    nonce: str,
    runtime_image: str,
    expected_version: str,
    expected_sha256: str,
    upstream_ip: str,
    model: str,
    timeout: int,
) -> list[str]:
    synthetic = bool(probe.get("response_plan"))
    command = [
        "docker",
        "exec",
        container,
        "python3",
        "/campaign/source/tools/official_client_capture/claude_fw_e_relay.py",
        "capture",
        "--execute",
        "--acknowledge-synthetic-responses" if synthetic else "--acknowledge-live-requests",
        "--run-id",
        f"{probe['probe_id']}-attempt-001",
        "--probe-id",
        str(probe["probe_id"]),
        "--fw-f-v4-probe",
        str(probe["probe_id"]),
        "--output-root",
        attempt_root,
        "--claude-bin",
        "/opt/claude",
        "--expected-version",
        expected_version,
        "--expected-sha256",
        expected_sha256,
        "--claude-credentials-file",
        "/run/claude-secrets/credentials.json",
        "--claude-global-state-file",
        "/run/claude-secrets/global.json",
        "--model",
        model,
        "--ca-signing-pem",
        "/run/mitm/ca.pem",
        "--ca-cert",
        "/run/mitm/ca-cert.pem",
        "--host-runtime-receipt",
        receipt,
        "--host-runtime-receipt-sha256",
        receipt_sha256,
        "--run-nonce",
        nonce,
        "--runtime-image",
        runtime_image,
        "--hosts-file",
        "/etc/hosts",
        "--lock-file",
        "/tmp/official-client-capture.lock",
        "--timeout",
        str(timeout),
    ]
    if not synthetic:
        command.extend(("--upstream-ip", upstream_ip))
    return command


def execute_campaign(arguments: argparse.Namespace) -> dict[str, Any]:
    root = arguments.campaign_root.resolve(strict=True)
    campaign, probes = _validate_campaign(root)
    tool_root = _validate_source(root, campaign)
    if not SHA256_RE.fullmatch(arguments.expected_sha256):
        raise CompleteCampaignRunnerError("--expected-sha256 格式非法。")
    if not re.fullmatch(r"\d+\.\d+\.\d+", arguments.expected_version):
        raise CompleteCampaignRunnerError("--expected-version 格式非法。")
    if arguments.timeout <= 0:
        raise CompleteCampaignRunnerError("--timeout 必须大于 0。")
    forbidden_networks = {
        value.strip()
        for value in arguments.forbidden_networks.split(",")
        if value.strip()
    }
    real = _docker_inspect(arguments.real_container)
    synthetic = _docker_inspect(arguments.synthetic_container)
    _validate_container(
        item=real,
        campaign_root=root,
        synthetic=False,
        forbidden_networks=forbidden_networks,
    )
    _validate_container(
        item=synthetic,
        campaign_root=root,
        synthetic=True,
        forbidden_networks=forbidden_networks,
    )
    if _mount_by_destination(real)["/run/claude-secrets"].get("Source") != _mount_by_destination(synthetic)["/run/claude-secrets"].get("Source"):
        raise CompleteCampaignRunnerError("真实与合成容器必须绑定同一份 Campaign 专用凭据副本。")
    if _mount_by_destination(real)["/etc/hosts"].get("Source") == _mount_by_destination(synthetic)["/etc/hosts"].get("Source"):
        raise CompleteCampaignRunnerError("真实与合成容器必须使用独立 hosts 文件。")
    before = root / "environment/production-before.json"
    if not before.is_file() or before.is_symlink():
        raise CompleteCampaignRunnerError("缺少执行前生产只读快照。")
    attempts_root = root / "attempts"
    receipts_root = root / "runtime-receipts"
    execution_root = root / "execution"
    for path, label in (
        (attempts_root, "attempts"),
        (receipts_root, "runtime-receipts"),
        (execution_root, "execution"),
    ):
        if path.exists() and any(path.iterdir()):
            raise CompleteCampaignRunnerError(f"正式 Campaign 的 {label} 已有内容，拒绝混写。")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = _utc_now()
    events: list[dict[str, Any]] = []
    for ordinal, probe in enumerate(probes, start=1):
        probe_id = str(probe["probe_id"])
        use_synthetic = bool(probe.get("response_plan"))
        container = arguments.synthetic_container if use_synthetic else arguments.real_container
        print(
            f"[{ordinal:02d}/{len(probes):02d}] 开始 {probe_id} "
            f"({'synthetic' if use_synthetic else 'official-live'})",
            file=sys.stderr,
            flush=True,
        )
        receipt_path = receipts_root / f"{probe_id}-attempt-001.json"
        attempt_path = attempts_root / probe_id / "attempt-001"
        stdout_path = execution_root / f"{probe_id}-attempt-001.stdout.log"
        stderr_path = execution_root / f"{probe_id}-attempt-001.stderr.log"
        event_path = execution_root / f"{probe_id}-attempt-001.event.json"
        for path in (receipt_path, attempt_path, stdout_path, stderr_path, event_path):
            if path.exists() or path.is_symlink():
                raise CompleteCampaignRunnerError(f"正式 attempt 已存在，拒绝覆盖：{path}")
        nonce = secrets.token_hex(32)
        receipt_command = _receipt_command(
            tool_root=tool_root,
            container=container,
            runtime_image=arguments.runtime_image,
            receipt=receipt_path,
            nonce=nonce,
        )
        receipt_completed = subprocess.run(
            receipt_command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if receipt_completed.returncode != 0:
            raise CompleteCampaignRunnerError(f"{probe_id} 宿主运行收据签发失败。")
        receipt_value = _load_json(receipt_path, f"{probe_id} 宿主运行收据")
        if receipt_value.get("schema_version") != RUNTIME_RECEIPT_SCHEMA:
            raise CompleteCampaignRunnerError(f"{probe_id} 宿主运行收据 schema 错误。")
        if receipt_value.get("run_nonce") != nonce:
            raise CompleteCampaignRunnerError(f"{probe_id} 宿主运行收据 nonce 不一致。")
        if receipt_value.get("capture_source_bundle", {}).get("sha256") != campaign.get("capture_source_bundle", {}).get("sha256"):
            raise CompleteCampaignRunnerError(f"{probe_id} 执行源摘要与 Campaign 不一致。")
        receipt_sha = _sha256_file(receipt_path)
        capture_command = _capture_command(
            container=container,
            probe=probe,
            attempt_root=f"/campaign/attempts/{probe_id}/attempt-001",
            receipt=f"/campaign/runtime-receipts/{receipt_path.name}",
            receipt_sha256=receipt_sha,
            nonce=nonce,
            runtime_image=arguments.runtime_image,
            expected_version=arguments.expected_version,
            expected_sha256=arguments.expected_sha256,
            upstream_ip=arguments.upstream_ip,
            model=arguments.model,
            timeout=arguments.timeout,
        )
        attempt_started = _utc_now()
        try:
            completed = subprocess.run(
                capture_command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=arguments.timeout + 300,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as error:
            returncode = 124
            stdout = error.stdout or b""
            stderr = error.stderr or b""
            timed_out = True
        _write_new(stdout_path, stdout)
        _write_new(stderr_path, stderr)
        manifest_path = attempt_path / "relay-manifest.json"
        manifest_status = "missing"
        m_complete = False
        secret_scan_passed = False
        dimension_result = "missing"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            manifest = _load_json(manifest_path, f"{probe_id} R manifest")
            manifest_status = str(manifest.get("status", ""))
            m_complete = manifest.get("m_binding", {}).get("complete") is True
            secret_scan_passed = manifest.get("secret_scan", {}).get("passed") is True
            dimension = manifest.get("dimension_evidence")
            dimension_result = str(
                dimension.get("result", "missing")
                if isinstance(dimension, dict)
                else "missing"
            )
        passed = (
            returncode == 0
            and manifest_status == "complete"
            and m_complete
            and secret_scan_passed
            and dimension_result == "passed"
        )
        event = {
            "schema_version": SCHEMA_EVENT,
            "campaign_id": campaign["campaign_id"],
            "ordinal": ordinal,
            "probe_id": probe_id,
            "attempt": 1,
            "container": container,
            "response_mode": "synthetic" if use_synthetic else "official-live",
            "response_plan": probe.get("response_plan"),
            "started_at_utc": attempt_started,
            "ended_at_utc": _utc_now(),
            "timed_out": timed_out,
            "returncode": returncode,
            "manifest_status": manifest_status,
            "m_complete": m_complete,
            "secret_scan_passed": secret_scan_passed,
            "dimension_result": dimension_result,
            "passed": passed,
            "run_nonce_sha256": _sha256_bytes(nonce.encode("ascii")),
            "host_receipt_sha256": receipt_sha,
            "capture_command_sha256": _sha256_bytes(_canonical_bytes(capture_command)),
        }
        _write_new_json(event_path, event)
        events.append(event)
        print(
            f"[{ordinal:02d}/{len(probes):02d}] "
            f"{'通过' if passed else '失败'} {probe_id}",
            file=sys.stderr,
            flush=True,
        )
    passed_count = sum(1 for event in events if event["passed"])
    result = {
        "schema_version": SCHEMA_EXECUTION,
        "campaign_id": campaign["campaign_id"],
        "campaign_sha256": _sha256_file(root / "campaign.json"),
        "capture_source_bundle_sha256": campaign["capture_source_bundle"]["sha256"],
        "target_version": arguments.expected_version,
        "target_binary_sha256": arguments.expected_sha256,
        "runtime_image": arguments.runtime_image,
        "started_at_utc": started,
        "ended_at_utc": _utc_now(),
        "probe_count": len(events),
        "passed_count": passed_count,
        "failed_count": len(events) - passed_count,
        "result": "passed" if passed_count == len(events) else "failed",
        "producer": {
            "path": "source/tools/official_client_capture/claude_fw_f_complete_runner.py",
            "sha256": _sha256_file(Path(__file__).resolve()),
        },
        "events": [
            {
                "probe_id": event["probe_id"],
                "event": f"execution/{event['probe_id']}-attempt-001.event.json",
                "passed": event["passed"],
            }
            for event in events
        ],
    }
    _write_new_json(root / "execution-summary.json", result)
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--real-container", required=True)
    parser.add_argument("--synthetic-container", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--upstream-ip", required=True)
    parser.add_argument("--expected-version", default="2.1.226")
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--forbidden-networks",
        default="proxy-network,sub2apiplus_sub2api-network",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        result = execute_campaign(_build_parser().parse_args(argv))
    except (CompleteCampaignRunnerError, OSError, subprocess.SubprocessError) as error:
        print(f"Claude FW-F 正式 Campaign 拒绝：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
