#!/usr/bin/env python3
"""顺序执行相互独立的 OAuth 与 API 官方客户端抓包任务。"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.analysis import (  # noqa: E402
    normalize_direct_pcap,
    normalize_mitm_directory,
    validate_tshark_client_hello_fields,
)
from tools.official_client_capture.capturelib.environment import (  # noqa: E402
    build_case_environment,
    clean_environment,
    parse_injected_env,
    prepare_api_state,
)
from tools.official_client_capture.capturelib.identity import (  # noqa: E402
    CAPTURE_SOURCE_RELATIVE_PATHS,
    capture_source_bundle_identity,
)
from tools.official_client_capture.capturelib.lifecycle import (  # noqa: E402
    CampaignLock,
    build_capture_process,
    ensure_mitm_port_available,
    resolve_target_addresses,
)
from tools.official_client_capture.capturelib.manifest import Manifest  # noqa: E402
from tools.official_client_capture.capturelib.model import (  # noqa: E402
    CLAUDE_AGENT_SCENARIOS,
    EVIDENCE_MODES,
    SCENARIOS,
    SUBJECTS,
    CampaignPlan,
    CaptureCase,
    ConfigurationError,
    build_suite_plans,
    make_batch_id,
    utc_now,
    validate_choice_list,
    validate_safe_name,
)
from tools.official_client_capture.capturelib.scenarios import (  # noqa: E402
    CODEX_HOOK_PATH,
    build_codex_config_preflight_command,
    run_claude_scenario,
    run_codex_scenario,
)
from tools.official_client_capture.capturelib.recovery import (  # noqa: E402
    RecoveryJournal,
    find_unclean_journals,
)
from tools.official_client_capture.capturelib.security import (  # noqa: E402
    ensure_private_directory,
    file_sha256,
    redact_known_secret,
    scan_for_secrets,
    scrub_known_secret,
    secure_write_text,
)


ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DEFAULT_CLAUDE_SHA256 = "674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863"
DEFAULT_CODEX_SHA256 = "a2a05dafaa1acb002a45eaec0a462de5b13694fcfcd7bc43305f14781ce7be14"
DEFAULT_RUNTIME_IMAGE = (
    "oauth-egress-capture-capture-cli@"
    "sha256:3438c4e0909d7401ff8e076a985258608a8f031629e65262db16c1979ab1771c"
)
RUN_ROOT_MARKER = "official-client-capture-root/v1\n"
HOST_RECEIPT_SCHEMA = "official-client-runtime-host-receipt/v1"
RUN_NONCE_RE = re.compile(r"^[a-f0-9]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{64}$")
CONTAINER_MOUNTINFO_RE = re.compile(
    r"/var/lib/docker/containers/(?P<id>[a-f0-9]{64})/"
    r"(?:hostname|hosts|resolv\.conf)(?:\s|$)"
)


class CaptureInterrupted(RuntimeError):
    """收到终止信号，要求先清理抓包进程和写完 manifest。"""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"收到终止信号 {signum}。")


class SecretLeakDetected(RuntimeError):
    """运行时秘密曾进入文本产物；文件已立即脱敏，但任务必须失败。"""

    def __init__(self, matches: list[str]) -> None:
        self.matches = list(matches)
        super().__init__("检测到运行时秘密残留：" + ", ".join(matches))


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


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
        raise RuntimeError(f"命令执行失败：{Path(command[0]).name}")
    return (completed.stdout or completed.stderr).strip()


def _client_info(
    *,
    claude_bin: Path,
    codex_bin: Path,
    expected_claude_version: str,
    expected_codex_version: str,
    expected_claude_sha256: str,
    expected_codex_sha256: str,
    api_key_env: str,
    subjects: tuple[str, ...] = SUBJECTS,
) -> dict[str, Any]:
    """在任何真实请求前固定客户端版本和二进制哈希。"""

    required_clients = {
        "claude" if subject.startswith("claude-") else "codex"
        for subject in subjects
    }
    paths = {
        "claude": claude_bin,
        "codex": codex_bin,
    }
    for name in sorted(required_clients):
        path = paths[name]
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ConfigurationError(f"客户端二进制不存在或不可执行：{path}")
    environment = clean_environment(os.environ)
    environment.pop(api_key_env, None)
    clients: dict[str, Any] = {}
    if "claude" in required_clients:
        claude_sha256 = file_sha256(claude_bin)
        if claude_sha256 != expected_claude_sha256:
            raise ConfigurationError("Claude 二进制 SHA-256 与固定基线不符。")
        claude_version = _command_output([str(claude_bin), "--version"], environment)
        claude_match = re.fullmatch(
            r"(?P<version>\d+\.\d+\.\d+)(?: \(Claude Code\))?", claude_version
        )
        if (
            not claude_match
            or claude_match.group("version") != expected_claude_version
        ):
            raise ConfigurationError(
                "Claude 版本不符，"
                f"预期 {expected_claude_version}，实际 {claude_version}。"
            )
        clients["claude"] = {
            "path": str(claude_bin),
            "version": claude_version,
            "sha256": claude_sha256,
            "expected_sha256": expected_claude_sha256,
        }
    if "codex" in required_clients:
        codex_sha256 = file_sha256(codex_bin)
        if codex_sha256 != expected_codex_sha256:
            raise ConfigurationError("Codex 二进制 SHA-256 与固定基线不符。")
        codex_version = _command_output([str(codex_bin), "--version"], environment)
        codex_match = re.fullmatch(
            r"codex-cli (?P<version>\d+\.\d+\.\d+)", codex_version
        )
        if (
            not codex_match
            or codex_match.group("version") != expected_codex_version
        ):
            raise ConfigurationError(
                "Codex 版本不符，"
                f"预期 {expected_codex_version}，实际 {codex_version}。"
            )
        clients["codex"] = {
            "path": str(codex_bin),
            "version": codex_version,
            "sha256": codex_sha256,
            "expected_sha256": expected_codex_sha256,
        }
    return clients


def _validate_static_file(path: Path, description: str, *, executable: bool) -> None:
    """拒绝符号链接、非普通文件和可被其他用户改写的运行资产。"""

    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{description} 不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise ConfigurationError(f"{description} 所有者或写权限不安全：{path}")
    if executable and not os.access(path, os.X_OK):
        raise ConfigurationError(f"{description} 不可执行：{path}")


def _validate_static_directory(path: Path, description: str) -> None:
    """拒绝符号链接及可被其他用户改写的运行目录。"""

    if path.is_symlink() or not path.is_dir():
        raise ConfigurationError(f"{description} 不是可信目录：{path}")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise ConfigurationError(f"{description} 所有者或写权限不安全：{path}")


def _container_id_from_mountinfo(
    path: Path = Path("/proc/self/mountinfo"),
) -> str:
    """从 Docker 管理的 /etc 绑定挂载反向取得当前容器 ID。"""

    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError("无法读取当前容器 mountinfo。") from error
    container_ids = {
        match.group("id") for match in CONTAINER_MOUNTINFO_RE.finditer(contents)
    }
    if len(container_ids) != 1:
        raise ConfigurationError(
            "mountinfo 未唯一绑定当前 Docker 容器的完整 ID。"
        )
    return next(iter(container_ids))


def _load_host_runtime_receipt(
    arguments: argparse.Namespace,
    source_identity: dict[str, Any],
) -> dict[str, Any] | None:
    """校验宿主 Docker daemon 生成的运行镜像凭据。"""

    path = arguments.host_runtime_receipt
    if path is None:
        if arguments.require_complete_m:
            raise ConfigurationError("完整 M 模式必须提供 --host-runtime-receipt。")
        return None
    _validate_static_file(path, "宿主运行镜像凭据", executable=False)
    actual_sha256 = file_sha256(path)
    if actual_sha256 != arguments.host_runtime_receipt_sha256:
        raise ConfigurationError("宿主运行镜像凭据 SHA-256 不匹配。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigurationError("宿主运行镜像凭据不是合法 JSON。") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != HOST_RECEIPT_SCHEMA:
        raise ConfigurationError("宿主运行镜像凭据 schema 不匹配。")
    if payload.get("run_nonce") != arguments.run_nonce:
        raise ConfigurationError("宿主运行镜像凭据 nonce 不匹配。")
    if payload.get("runtime_image_reference") != arguments.runtime_image:
        raise ConfigurationError("宿主运行镜像凭据与 --runtime-image 不一致。")
    if payload.get("repo_digest_verified") is not True:
        raise ConfigurationError("宿主运行镜像凭据未确认 RepoDigest。")
    if not IMAGE_ID_RE.fullmatch(str(payload.get("runtime_image_id", ""))):
        raise ConfigurationError("宿主运行镜像凭据的 image ID 非法。")
    receipt_source = payload.get("capture_source_bundle")
    if (
        not isinstance(receipt_source, dict)
        or receipt_source.get("sha256") != source_identity.get("sha256")
        or receipt_source.get("files") != source_identity.get("files")
    ):
        raise ConfigurationError("宿主与容器内抓包执行源摘要不一致。")
    container = payload.get("container")
    if not isinstance(container, dict):
        raise ConfigurationError("宿主运行镜像凭据缺少容器身份。")
    hostname = socket.gethostname()
    container_id = str(container.get("id", ""))
    if container.get("hostname") != hostname:
        raise ConfigurationError("宿主凭据绑定的容器与当前容器不一致。")
    if not CONTAINER_ID_RE.fullmatch(container_id):
        raise ConfigurationError("宿主凭据中的完整容器 ID 非法。")
    if _container_id_from_mountinfo() != container_id:
        raise ConfigurationError(
            "宿主凭据的完整容器 ID 与当前 mountinfo 不一致。"
        )
    try:
        issued = dt.datetime.fromisoformat(str(payload["issued_at_utc"]))
    except (KeyError, ValueError) as error:
        raise ConfigurationError("宿主运行镜像凭据签发时间非法。") from error
    if issued.tzinfo is None:
        raise ConfigurationError("宿主运行镜像凭据签发时间缺少时区。")
    age = dt.datetime.now(dt.timezone.utc) - issued.astimezone(dt.timezone.utc)
    if age < dt.timedelta(seconds=-60) or age > dt.timedelta(minutes=15):
        raise ConfigurationError("宿主运行镜像凭据已过期或来自未来。")
    return {
        "path": str(path),
        "sha256": actual_sha256,
        "schema_version": payload["schema_version"],
        "issued_at_utc": payload["issued_at_utc"],
        "run_nonce": payload["run_nonce"],
        "container": container,
        "container_runtime_binding": {
            "verified": True,
            "method": "docker-managed-etc-mountinfo-and-hostname",
            "container_id": container_id,
            "hostname": hostname,
        },
        "runtime_image_reference": payload["runtime_image_reference"],
        "runtime_image_id": payload["runtime_image_id"],
        "repo_digest_verified": True,
        "capture_source_bundle_sha256": receipt_source["sha256"],
        "producer": payload.get("producer"),
        "docker_server": payload.get("docker_server"),
    }


def _validate_run_root(arguments: argparse.Namespace, *, initialize: bool) -> None:
    """限定专用运行根，禁止把原始抓包写进源码树或宽泛目录。"""

    run_root = arguments.run_root
    if not run_root.is_absolute() or run_root.is_symlink():
        raise ConfigurationError("--run-root 必须是非符号链接的绝对路径。")
    resolved = run_root.resolve(strict=False)
    forbidden = {
        Path("/").resolve(),
        Path.home().resolve(),
        Path("/tmp").resolve(),
        Path("/capture").resolve(),
        Path("/capture/runs").resolve(),
    }
    tool_root = Path(__file__).resolve().parent
    source_root = next(
        (parent for parent in tool_root.parents if (parent / ".git").exists()),
        tool_root,
    )
    if resolved in forbidden or resolved.is_relative_to(source_root):
        raise ConfigurationError("--run-root 不能指向宽泛目录、HOME 或源码树。")
    marker = run_root / ".official-client-capture-root"
    if run_root.exists():
        if not run_root.is_dir():
            raise ConfigurationError("--run-root 已存在但不是目录。")
        metadata = run_root.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o777 != 0o700:
            raise ConfigurationError("已有 --run-root 必须归当前用户所有且权限为 0700。")
        entries = list(run_root.iterdir())
        if entries and (
            not marker.is_file()
            or marker.is_symlink()
            or marker.read_text(encoding="utf-8") != RUN_ROOT_MARKER
        ):
            raise ConfigurationError("已有 --run-root 缺少本工具专用标记。")
    if initialize:
        ensure_private_directory(run_root)
        secure_write_text(marker, RUN_ROOT_MARKER)


def _preflight_dependencies(
    *,
    arguments: argparse.Namespace,
    plans: tuple[CampaignPlan, ...],
    evidence: tuple[str, ...],
) -> dict[str, Any]:
    """在任何模型请求前校验运行资产并生成可复现元数据。"""

    for plan in plans:
        run_dir = arguments.run_root / plan.task / plan.run_id
        if run_dir.exists():
            raise ConfigurationError(f"运行目录已存在，拒绝覆盖：{run_dir}")

    tool_root = Path(__file__).resolve().parent
    for relative in CAPTURE_SOURCE_RELATIVE_PATHS:
        _validate_static_file(
            tool_root / relative,
            f"抓包执行源 {relative}",
            executable=False,
        )
    source_identity = capture_source_bundle_identity(tool_root)
    host_receipt = _load_host_runtime_receipt(arguments, source_identity)
    capture_tools: dict[str, Any] = {
        "execution_sources": source_identity,
    }
    _validate_static_file(CODEX_HOOK_PATH, "Codex PreToolUse hook", executable=False)
    capture_tools["codex_hook"] = {
        "path": str(CODEX_HOOK_PATH),
        "sha256": file_sha256(CODEX_HOOK_PATH),
    }
    if "direct" in evidence:
        for name, path, version_args in (
            ("tcpdump", arguments.tcpdump_bin, ["--version"]),
            ("tshark", arguments.tshark_bin, ["--version"]),
        ):
            _validate_static_file(path, name, executable=True)
            capture_tools[name] = {
                "path": str(path),
                "version": _command_output(
                    [str(path), *version_args], clean_environment(os.environ)
                ).splitlines()[0],
                "sha256": file_sha256(path),
            }
        capture_tools["tshark"]["client_hello_fields"] = list(
            validate_tshark_client_hello_fields(
                tshark_bin=str(arguments.tshark_bin),
                environment=clean_environment(os.environ),
            )
        )
        for plan in plans:
            for case in plan.cases:
                if case.evidence == "direct":
                    parsed_port = (
                        urllib.parse.urlsplit(case.base_url).port
                        if case.base_url
                        else None
                    )
                    resolve_target_addresses(
                        case.target_hosts,
                        target_port=parsed_port or 443,
                    )

    ca_info: dict[str, Any] | None = None
    if "mitm" in evidence:
        # 本轮含 mitm case 时，在任何请求发出前先确认端口空闲。CaptureProcess.start
        # 里也有同一检查，但那要排到第一个 mitm case 才触发——前面的 direct case 已经
        # 跑掉了，前轮残留的 mitmdump 于是在采集中途才暴露（k61）。
        ensure_mitm_port_available(arguments.mitm_port)
        _validate_static_file(arguments.mitmdump_bin, "mitmdump", executable=True)
        _validate_static_file(arguments.mitm_addon, "MITM addon", executable=False)
        _validate_static_file(arguments.ca_bundle, "MITM CA", executable=False)
        _validate_static_directory(arguments.mitm_confdir, "MITM confdir")
        capture_tools["mitmdump"] = {
            "path": str(arguments.mitmdump_bin),
            "version": _command_output(
                [str(arguments.mitmdump_bin), "--version"],
                clean_environment(os.environ),
            ).splitlines()[0],
            "sha256": file_sha256(arguments.mitmdump_bin),
            "addon_sha256": file_sha256(arguments.mitm_addon),
        }
        ca_info = {
            "path": str(arguments.ca_bundle),
            "sha256": file_sha256(arguments.ca_bundle),
        }

    _validate_static_directory(Path("/work"), "Codex 固定工作目录 /work")
    validated_codex_configs = 0
    seen_codex_configs: set[tuple[str, ...]] = set()
    with tempfile.TemporaryDirectory(
        prefix=".codex-config-preflight-", dir=arguments.run_root
    ) as temporary_directory:
        preflight_home = Path(temporary_directory)
        preflight_home.chmod(0o700)
        preflight_environment = clean_environment(os.environ)
        preflight_environment.update(
            {"HOME": str(preflight_home), "CODEX_HOME": str(preflight_home)}
        )
        for plan in plans:
            for case in plan.cases:
                if case.product != "codex":
                    continue
                for scenario in case.scenarios:
                    command = build_codex_config_preflight_command(
                        codex_bin=str(arguments.codex_bin),
                        case=case,
                        api_key_env=arguments.api_key_env,
                        scenario=scenario,
                        hook_audit_path=preflight_home / "hook-audit.jsonl",
                    )
                    command_key = tuple(command)
                    if command_key in seen_codex_configs:
                        continue
                    seen_codex_configs.add(command_key)
                    _command_output(command, preflight_environment)
                    validated_codex_configs += 1
    return {
        "runtime_image_claim": arguments.runtime_image,
        "runtime_image_verified": host_receipt is not None,
        "runtime_image_limitation": (
            None
            if host_receipt is not None
            else "未提供经宿主 Docker daemon 验证并与本轮 nonce 绑定的运行镜像凭据。"
        ),
        "host_runtime_receipt": host_receipt,
        "m_binding_requested": bool(arguments.require_complete_m),
        "profile_version": arguments.profile_version,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "interface": arguments.interface,
        "capture_tools": capture_tools,
        "ca_bundle": ca_info,
        "codex_config_preflight": {
            "mode": "features_list_no_model_request",
            "configuration_count": validated_codex_configs,
        },
        "clean_environment_keys": sorted(clean_environment(os.environ)),
        # 探针注入项必须进 manifest：条件规则的正负例靠它区分，
        # 不记录就无法证明某次样本属于哪一侧。
        "injected_probe_env": dict(sorted(getattr(arguments, "injected_env", {}).items())),
        # 故障注入同样必须可审计：注入过的样本不能与自然样本混用。
        "injected_fault_spec": getattr(arguments, "fault_spec", "") or None,
    }


def _verify_oauth_state(
    arguments: argparse.Namespace,
    selected_subjects: tuple[str, ...],
    oauth_claude_secret: str | None,
) -> dict[str, Any]:
    """只读取本地登录状态，不发送模型请求、不记录账号标识。"""

    environment = clean_environment(os.environ)
    result: dict[str, Any] = {}
    if "claude-http" in selected_subjects:
        if oauth_claude_secret:
            result["claude"] = {
                "logged_in": True,
                "auth_method": "runtime_oauth_token_override",
                "api_provider": "firstParty",
            }
        else:
            claude_status = _command_output(
                [str(arguments.claude_bin), "auth", "status", "--json"],
                environment,
            )
            try:
                claude_payload = json.loads(claude_status)
            except ValueError as error:
                raise ConfigurationError("Claude OAuth 状态不是合法 JSON。") from error
            if not claude_payload.get("loggedIn"):
                raise ConfigurationError("Claude OAuth 状态未登录。")
            result["claude"] = {
                "logged_in": True,
                "auth_method": claude_payload.get("authMethod"),
                "api_provider": claude_payload.get("apiProvider"),
            }
    if any(subject.startswith("codex-") for subject in selected_subjects):
        codex_status = _command_output(
            [str(arguments.codex_bin), "login", "status"], environment
        )
        if "logged in" not in codex_status.lower():
            raise ConfigurationError("Codex OAuth 状态未登录。")
        result["codex"] = {"logged_in": True, "method": "chatgpt_oauth_state"}
    return result


@contextlib.contextmanager
def _termination_guard():
    """第一枚终止信号触发解栈；清理期间忽略后续信号。"""

    previous = {item: signal.getsignal(item) for item in (signal.SIGINT, signal.SIGTERM)}
    triggered = False

    def handle(signum: int, _frame: Any) -> None:
        nonlocal triggered
        if triggered:
            return
        triggered = True
        for item in previous:
            signal.signal(item, signal.SIG_IGN)
        raise CaptureInterrupted(signum)

    for item in previous:
        signal.signal(item, handle)
    try:
        yield
    finally:
        for item, handler in previous.items():
            signal.signal(item, handler)


@contextlib.contextmanager
def _block_termination_signals():
    """在登记新子进程的极短窗口内延后 SIGINT/SIGTERM。"""

    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    blocked = {signal.SIGINT, signal.SIGTERM}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _case_output_dir(run_dir: Path, case: CaptureCase, scenario: str) -> Path:
    return run_dir / case.evidence / case.subject / scenario


def _case_result_dir(run_dir: Path, case: CaptureCase, scenario: str) -> Path:
    return run_dir / "results" / case.evidence / case.subject / scenario


def _case_analysis_path(run_dir: Path, case: CaptureCase, scenario: str) -> Path:
    return run_dir / "analysis" / case.evidence / case.subject / f"{scenario}.json"


def _validate_mitm_shape(case: CaptureCase, payload: dict[str, Any]) -> None:
    records = payload.get("records") if isinstance(payload.get("records"), list) else []
    if not records:
        raise RuntimeError("MITM 未记录到目标主机请求。")
    expected_path = "/messages" if case.product == "claude" else "/responses"

    def is_model_path(value: Any) -> bool:
        path = urllib.parse.urlsplit(str(value)).path.rstrip("/")
        return path.endswith(expected_path)

    client_ws_frames = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("kind") == "websocket_frame"
        and record.get("from_client")
        and is_model_path(record.get("path"))
    ]
    model_posts = [
        record
        for record in records
        if isinstance(record, dict)
        and record.get("kind") == "http_exchange"
        and isinstance(record.get("request"), dict)
        and str(record["request"].get("method", "")).upper() == "POST"
        and is_model_path(record["request"].get("path"))
    ]
    if case.transport == "ws" and not client_ws_frames:
        raise RuntimeError("预期 WebSocket 的 case 未记录到目标 Responses 客户端帧。")
    if case.transport == "ws" and model_posts:
        raise RuntimeError("预期 WebSocket 的 case 出现了 Responses HTTP 回退。")
    if case.transport == "http" and not model_posts:
        raise RuntimeError("预期 HTTP 的 case 未记录到对应产品 POST 模型请求。")
    if case.transport == "http" and any(
        record.get("kind") == "websocket_frame"
        and is_model_path(record.get("path"))
        for record in records
        if isinstance(record, dict)
    ):
        raise RuntimeError("预期 HTTP 的 case 意外出现了 WebSocket 帧。")


def _run_case_scenario(
    *,
    case: CaptureCase,
    run_dir: Path,
    arguments: argparse.Namespace,
    api_secret: str | None,
    oauth_claude_secret: str | None,
    claude_api_home: Path | None,
    codex_api_home: Path | None,
    scenario: str,
    journal: RecoveryJournal,
    known_secrets: dict[str, str],
) -> dict[str, Any]:
    """执行一个不可混合的 product×transport×evidence×scenario 单元。"""

    started_at = utc_now()
    output_dir = _case_output_dir(run_dir, case, scenario)
    analysis_path = _case_analysis_path(run_dir, case, scenario)
    environment = build_case_environment(
        case=case,
        source=os.environ,
        api_secret=api_secret,
        api_key_env=arguments.api_key_env,
        claude_api_home=claude_api_home,
        codex_api_home=codex_api_home,
        proxy_url=f"http://127.0.0.1:{arguments.mitm_port}",
        ca_bundle=arguments.ca_bundle,
        oauth_claude_secret=oauth_claude_secret,
        injected_env=getattr(arguments, "injected_env", None),
    )
    capture_environment = clean_environment(os.environ)
    capture_environment.pop(arguments.api_key_env, None)
    capture = build_capture_process(
        fault_spec=arguments.fault_spec,
        case=case,
        output_dir=output_dir,
        base_environment=capture_environment,
        tcpdump_bin=str(arguments.tcpdump_bin),
        mitmdump_bin=str(arguments.mitmdump_bin),
        mitm_addon=arguments.mitm_addon,
        mitm_confdir=arguments.mitm_confdir,
        mitm_port=arguments.mitm_port,
        interface=arguments.interface,
        scenario=scenario,
    )

    result_dir = _case_result_dir(run_dir, case, scenario)
    ensure_private_directory(result_dir, run_dir)
    capture_started = False
    journal_active = False
    try:
        with _block_termination_signals():
            try:
                capture.start()
                capture_started = True
            finally:
                # start() 的就绪校验或首次清理失败时，process 仍可能存活。
                # 必须先登记账本再解除信号屏蔽，避免形成不可追踪的孤儿进程。
                if capture.process is not None:
                    journal.activate(
                        case=case,
                        scenario=scenario,
                        role=(
                            "tcpdump" if case.evidence == "direct" else "mitmdump"
                        ),
                        pid=capture.process.pid,
                        pgid=capture.process.pid,
                        output_dir=output_dir,
                        port=(
                            arguments.mitm_port
                            if case.evidence == "mitm"
                            else None
                        ),
                    )
                    journal_active = True
        runtime_secret = (
            api_secret
            if case.task == "api"
            else oauth_claude_secret if case.product == "claude" else None
        )
        if case.product == "claude":
            summary = run_claude_scenario(
                claude_bin=str(arguments.claude_bin),
                model=arguments.claude_model,
                scenario=scenario,
                environment=environment,
                output_dir=result_dir,
                timeout=arguments.timeout,
                runtime_secret=runtime_secret,
                known_secrets=known_secrets,
            )
        else:
            summary = run_codex_scenario(
                codex_bin=str(arguments.codex_bin),
                model=arguments.codex_model,
                case=case,
                scenario=scenario,
                environment=environment,
                output_dir=result_dir,
                timeout=arguments.timeout,
                runtime_secret=runtime_secret,
                api_key_env=arguments.api_key_env,
                codex_version=arguments.expected_codex_version,
            )
        if not summary.get("valid"):
            raise RuntimeError(
                f"{case.subject}/{case.evidence}/{scenario} 场景校验失败。"
            )
    finally:
        try:
            if capture_started or capture.process is not None:
                capture.stop()
        finally:
            if journal_active:
                journal.deactivate(
                    cleanup_successful=(
                        capture.cleanup_successful and capture.process is None
                    )
                )
            elif not capture.cleanup_successful:
                # 抓包进程可能已退出但端口恢复校验失败，此时没有可登记 PID，
                # 仍必须把账本标成不干净以阻止下一次任务静默继续。
                journal.deactivate(cleanup_successful=False)

    ensure_private_directory(analysis_path.parent, run_dir)
    if case.evidence == "direct":
        analysis = normalize_direct_pcap(
            pcap_path=output_dir / "traffic.pcap",
            output_path=analysis_path,
            target_hosts=case.target_hosts,
            tshark_bin=str(arguments.tshark_bin),
        )
    else:
        analysis = normalize_mitm_directory(output_dir, analysis_path)
        _validate_mitm_shape(case, analysis)

    if case.evidence == "direct":
        protocol_observation = {
            "kind": "client_hello_offered_alpn",
            "values": sorted(
                {
                    protocol
                    for hello in analysis.get("client_hellos", [])
                    if isinstance(hello, dict)
                    for protocol in hello.get("alpn", [])
                }
            ),
        }
    else:
        records = analysis.get("records", [])
        protocol_observation = {
            "kind": "mitm_application_protocol",
            "values": sorted(
                {
                    "websocket"
                    if record.get("kind") == "websocket_frame"
                    else str(
                        record.get("request", {}).get("http_version", "unknown")
                    )
                    for record in records
                    if isinstance(record, dict)
                }
            ),
        }

    return {
        "subject": case.subject,
        "product": case.product,
        "transport": case.transport,
        "evidence": case.evidence,
        "scenario": scenario,
        "boundary": case.boundary,
        "started_at": started_at,
        "ended_at": utc_now(),
        "status": "complete",
        "scenario_result": summary,
        "capture": capture.metadata,
        "protocol_observation": protocol_observation,
        "analysis_path": str(analysis_path.relative_to(run_dir)),
    }


def _run_campaign(
    *,
    plan: CampaignPlan,
    arguments: argparse.Namespace,
    clients: dict[str, Any],
    api_secret: str | None,
    oauth_claude_secret: str | None,
    runtime: dict[str, Any],
    known_secrets: dict[str, str],
) -> dict[str, Any]:
    run_dir = arguments.run_root / plan.task / plan.run_id
    if run_dir.exists():
        raise ConfigurationError(f"运行目录已存在，拒绝覆盖：{run_dir}")
    ensure_private_directory(run_dir, arguments.run_root)
    manifest = Manifest(plan, run_dir)
    manifest.set_clients(clients)
    manifest.set_runtime(runtime)
    journal = RecoveryJournal(run_dir)
    claude_api_home: Path | None = None
    codex_api_home: Path | None = None
    if plan.task == "api":
        claude_api_home, codex_api_home = prepare_api_state(run_dir)

    def scan_report() -> dict[str, Any]:
        return scan_for_secrets(run_dir, known_secrets)

    def match_paths(report: dict[str, Any]) -> list[str]:
        return sorted(
            str(item.get("path"))
            for item in report.get("matches", [])
            if isinstance(item, dict) and item.get("path")
        )

    def scrub_all_known_secrets() -> None:
        for value in known_secrets.values():
            scrub_known_secret(run_dir, value)

    try:
        for case in plan.cases:
            for scenario in case.scenarios:
                result = _run_case_scenario(
                    case=case,
                    run_dir=run_dir,
                    arguments=arguments,
                    api_secret=api_secret,
                    oauth_claude_secret=oauth_claude_secret,
                    claude_api_home=claude_api_home,
                    codex_api_home=codex_api_home,
                    scenario=scenario,
                    journal=journal,
                    known_secrets=known_secrets,
                )
                manifest.add_case_result(result)
                interim_report = scan_report()
                secret_matches = match_paths(interim_report)
                if secret_matches:
                    scrub_all_known_secrets()
                    raise SecretLeakDetected(secret_matches)
                if interim_report.get("scan_errors"):
                    raise RuntimeError("秘密扫描无法读取全部运行产物。")
        final_scan_report = scan_report()
        secret_matches = match_paths(final_scan_report)
        if arguments.require_complete_m and final_scan_report.get("passed") is not True:
            raise RuntimeError("完整 M 模式的精确秘密扫描未通过。")
        journal.finalize(status="complete", cleanup_successful=True)
        manifest.finalize(
            status="complete",
            cleanup_successful=True,
            secret_matches=secret_matches,
            secret_scan_report=final_scan_report,
            m_binding_required=arguments.require_complete_m,
        )
        if arguments.require_complete_m and manifest.data["m_binding"]["complete"] is not True:
            raise RuntimeError("完整 M 门禁缺少必要绑定。")
        return {
            "task": plan.task,
            "run_id": plan.run_id,
            "status": "complete",
            "manifest": str(manifest.path),
        }
    except BaseException as error:
        cleanup_successful = (
            journal.data.get("active_resource") is None
            and bool(journal.data.get("cleanup_successful"))
        )
        status = (
            "interrupted"
            if isinstance(error, (CaptureInterrupted, KeyboardInterrupt))
            else "failed"
        )
        if isinstance(error, CaptureInterrupted):
            journal.note_signal(error.signum)
        journal.finalize(status=status, cleanup_successful=cleanup_successful)
        safe_error = str(error)
        for value in known_secrets.values():
            safe_error = redact_known_secret(safe_error, value)
        failed_scan_report = scan_report()
        secret_matches = (
            error.matches
            if isinstance(error, SecretLeakDetected)
            else match_paths(failed_scan_report)
        )
        if secret_matches:
            scrub_all_known_secrets()
            failed_scan_report = scan_report()
        manifest.finalize(
            status=status,
            cleanup_successful=cleanup_successful,
            secret_matches=secret_matches,
            secret_scan_report=failed_scan_report,
            m_binding_required=arguments.require_complete_m,
            error=safe_error,
        )
        raise
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行 OAuth 与 API 两套相互独立的官方客户端抓包任务。"
    )
    parser.add_argument("--task", choices=("oauth", "api", "all"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只输出脱敏计划")
    mode.add_argument("--execute", action="store_true", help="执行真实请求和抓包")
    parser.add_argument(
        "--acknowledge-live-requests",
        action="store_true",
        help="确认本轮会产生真实模型请求；--execute 必须显式提供",
    )
    parser.add_argument("--batch-id", default=make_batch_id())
    parser.add_argument("--scenarios", default="s1,s2,s4")
    parser.add_argument("--subjects", default=",".join(SUBJECTS))
    parser.add_argument("--evidence", default="direct,mitm")
    parser.add_argument("--sub2api-base-url")
    parser.add_argument("--api-key-env", default="SUB2API_CAPTURE_API_KEY")
    parser.add_argument(
        "--claude-oauth-token-env",
        default="",
        help="可选的 Claude OAuth token 环境变量名；值只进入 Claude OAuth 子进程",
    )
    parser.add_argument("--claude-model", default="claude-sonnet-5")
    parser.add_argument("--codex-model", default="gpt-5.6-luna")
    parser.add_argument("--expected-claude-version", default="2.1.220")
    parser.add_argument("--expected-codex-version", default="0.145.0")
    parser.add_argument(
        "--expected-claude-sha256", default=DEFAULT_CLAUDE_SHA256
    )
    parser.add_argument(
        "--expected-codex-sha256", default=DEFAULT_CODEX_SHA256
    )
    parser.add_argument(
        "--runtime-image",
        default=DEFAULT_RUNTIME_IMAGE,
        help="capture-cli 的不可变 repository@sha256 镜像引用",
    )
    parser.add_argument(
        "--host-runtime-receipt",
        type=Path,
        help="由 runtime_host_receipt.py 在 Docker 宿主机生成的运行身份凭据",
    )
    parser.add_argument(
        "--host-runtime-receipt-sha256",
        default="",
        help="宿主运行身份凭据文件的 SHA-256",
    )
    parser.add_argument(
        "--run-nonce",
        default="",
        help="与宿主运行身份凭据逐轮绑定的 64 位十六进制 nonce",
    )
    parser.add_argument(
        "--require-complete-m",
        action="store_true",
        help="缺少宿主身份、实际调用、完整脱敏环境或精确秘密扫描时失败关闭",
    )
    parser.add_argument(
        "--profile-version",
        default="official-cli-2.1.220-codex-0.145.0-baseline-v1",
    )
    parser.add_argument("--claude-bin", type=Path, default=Path("/root/.local/bin/claude"))
    parser.add_argument(
        "--codex-bin", type=Path, default=Path("/usr/local/bin/codex-capture")
    )
    parser.add_argument("--tcpdump-bin", type=Path, default=Path("/usr/bin/tcpdump"))
    parser.add_argument("--tshark-bin", type=Path, default=Path("/usr/bin/tshark"))
    parser.add_argument("--mitmdump-bin", type=Path, default=Path("/usr/bin/mitmdump"))
    parser.add_argument(
        "--mitm-addon",
        type=Path,
        default=Path(__file__).resolve().parent / "addons" / "mitm_capture.py",
    )
    parser.add_argument("--mitm-confdir", type=Path, default=Path("/opt/mitm"))
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        default=Path("/opt/mitm/mitmproxy-ca-cert.pem"),
    )
    parser.add_argument(
        "--run-root", type=Path, default=Path("/capture/runs/official-client")
    )
    parser.add_argument(
        "--lock-file", type=Path, default=Path("/run/official-client-capture.lock")
    )
    parser.add_argument("--interface", default="any")
    parser.add_argument("--mitm-port", type=int, default=18080)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--fault-spec",
        default="",
        metavar="SPEC",
        help=(
            "受控故障注入，形如 status=500,count=1 或 kill=1,count=1。仅 MITM 证据可用，"
            "用于给重试链路取正例。注入样本只证明客户端收到该输入后的反应，"
            "不等于自然成功链，引用时必须声明。"
        ),
    )
    parser.add_argument(
        "--inject-env",
        action="append",
        metavar="KEY=VALUE",
        help=(
            "向被测客户端注入一个探针环境变量，可重复。只接受官方客户端自身读取的"
            "条件开关（见 environment.INJECTABLE_PROBE_KEYS），用于条件规则取正负例；"
            "注入项会写进 manifest 供审计。凭据、上游地址、代理和 CA 类变量一律拒绝。"
        ),
    )
    parser.add_argument(
        "--secret-scan-env",
        action="append",
        metavar="ENV_NAME",
        help="额外加入精确值秘密扫描、但不传给客户端的环境变量名；可重复",
    )
    return parser


def _validate_arguments(
    arguments: argparse.Namespace,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    validate_safe_name(arguments.batch_id, "batch_id")
    if not ENV_NAME_RE.fullmatch(arguments.api_key_env):
        raise ConfigurationError("--api-key-env 必须是合法的大写环境变量名。")
    scenarios = validate_choice_list(
        _parse_csv(arguments.scenarios), SCENARIOS, "scenarios"
    )
    evidence = validate_choice_list(
        _parse_csv(arguments.evidence), EVIDENCE_MODES, "evidence"
    )
    subjects = validate_choice_list(
        _parse_csv(arguments.subjects), SUBJECTS, "subjects"
    )
    if any(item in CLAUDE_AGENT_SCENARIOS for item in scenarios) and subjects != (
        "claude-http",
    ):
        raise ConfigurationError("a1/a2/a3 嵌套 Agent 场景只能单独用于 claude-http。")
    if arguments.claude_oauth_token_env and not ENV_NAME_RE.fullmatch(
        arguments.claude_oauth_token_env
    ):
        raise ConfigurationError(
            "--claude-oauth-token-env 必须是合法的大写环境变量名。"
        )
    for value in arguments.secret_scan_env or ():
        if not ENV_NAME_RE.fullmatch(value):
            raise ConfigurationError("--secret-scan-env 必须是合法的大写环境变量名。")
    if arguments.timeout <= 0:
        raise ConfigurationError("--timeout 必须大于 0。")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", arguments.interface):
        raise ConfigurationError("--interface 格式非法。")
    for field, value in (
        ("--expected-claude-sha256", arguments.expected_claude_sha256),
        ("--expected-codex-sha256", arguments.expected_codex_sha256),
    ):
        if not SHA256_RE.fullmatch(value):
            raise ConfigurationError(f"{field} 必须是 64 位小写 SHA-256。")
    for field, value in (
        ("--expected-claude-version", arguments.expected_claude_version),
        ("--expected-codex-version", arguments.expected_codex_version),
    ):
        if not VERSION_RE.fullmatch(value):
            raise ConfigurationError(f"{field} 必须是精确的三段版本号。")
    validate_safe_name(arguments.profile_version, "profile_version")
    if not arguments.runtime_image.strip() or "sha256:" not in arguments.runtime_image:
        raise ConfigurationError("--runtime-image 必须包含镜像引用和 sha256 digest。")
    receipt_fields = (
        arguments.host_runtime_receipt is not None,
        bool(arguments.host_runtime_receipt_sha256),
        bool(arguments.run_nonce),
    )
    if any(receipt_fields) and not all(receipt_fields):
        raise ConfigurationError(
            "宿主运行凭据必须同时提供文件、SHA-256 与 run nonce。"
        )
    if arguments.host_runtime_receipt_sha256 and not SHA256_RE.fullmatch(
        arguments.host_runtime_receipt_sha256
    ):
        raise ConfigurationError("--host-runtime-receipt-sha256 格式非法。")
    if arguments.run_nonce and not RUN_NONCE_RE.fullmatch(arguments.run_nonce):
        raise ConfigurationError("--run-nonce 必须是 64 位小写十六进制。")
    _validate_run_root(arguments, initialize=False)
    if (
        not arguments.lock_file.is_absolute()
        or arguments.lock_file.is_symlink()
        or arguments.lock_file.suffix != ".lock"
    ):
        raise ConfigurationError("--lock-file 必须是非符号链接的绝对 .lock 路径。")
    for field, value in (
        ("--claude-model", arguments.claude_model),
        ("--codex-model", arguments.codex_model),
    ):
        if not MODEL_NAME_RE.fullmatch(value):
            raise ConfigurationError(f"{field} 格式非法。")
    if arguments.execute and not arguments.acknowledge_live_requests:
        raise ConfigurationError(
            "--execute 会产生真实模型请求，必须同时提供 --acknowledge-live-requests。"
        )
    if arguments.require_complete_m and arguments.execute:
        if subjects != ("claude-http",):
            raise ConfigurationError("当前完整 M 门禁只允许单独采集 claude-http。")
        if arguments.task in {"oauth", "all"} and not arguments.claude_oauth_token_env:
            raise ConfigurationError(
                "OAuth 完整 M 必须通过 --claude-oauth-token-env 提供本轮实际访问令牌。"
            )
    # 解析后就地固化，后续所有环境构造与 manifest 记录都用这一份，避免两处解析漂移。
    arguments.injected_env = parse_injected_env(arguments.inject_env)
    return scenarios, evidence, subjects


def main() -> int:
    os.umask(0o077)
    parser = _build_parser()
    arguments = parser.parse_args()
    try:
        scenarios, evidence, subjects = _validate_arguments(arguments)
        plans = build_suite_plans(
            task=arguments.task,
            batch_id=arguments.batch_id,
            scenarios=scenarios,
            evidence_modes=evidence,
            sub2api_base_url=arguments.sub2api_base_url,
            api_key_env=arguments.api_key_env,
            subjects=subjects,
            oauth_claude_token_env=arguments.claude_oauth_token_env or None,
        )
        safe_plan = {
            "schema_version": "official-client-capture-suite/v1",
            "batch_id": arguments.batch_id,
            "execution_order": [plan.task for plan in plans],
            "plans": [plan.to_dict() for plan in plans],
            "external_ab_executed": False,
            "m_binding": {
                "required": bool(arguments.require_complete_m),
                "host_runtime_receipt_required": bool(arguments.require_complete_m),
                "exact_secret_scan_required": bool(arguments.require_complete_m),
            },
        }
        if arguments.dry_run:
            print(json.dumps(safe_plan, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        api_secret = (
            os.environ.get(arguments.api_key_env)
            if any(plan.task == "api" for plan in plans)
            else None
        )
        oauth_claude_secret = (
            os.environ.get(arguments.claude_oauth_token_env)
            if arguments.claude_oauth_token_env
            and any(plan.task == "oauth" for plan in plans)
            else None
        )
        if any(plan.task == "api" for plan in plans) and not api_secret:
            raise ConfigurationError(
                f"API 任务缺少环境变量 {arguments.api_key_env}。"
            )
        if arguments.claude_oauth_token_env and not oauth_claude_secret:
            raise ConfigurationError(
                f"Claude OAuth 任务缺少环境变量 {arguments.claude_oauth_token_env}。"
            )
        additional_secrets: dict[str, str] = {}
        for name in dict.fromkeys(arguments.secret_scan_env or ()):
            value = os.environ.get(name)
            if not value:
                raise ConfigurationError(f"秘密扫描环境变量 {name} 不存在或为空。")
            additional_secrets[f"operator_scan_env:{name}"] = value
        results: list[dict[str, Any]] = []
        with CampaignLock(arguments.lock_file):
            _validate_run_root(arguments, initialize=True)
            unclean_journals = find_unclean_journals(arguments.run_root)
            if unclean_journals:
                raise ConfigurationError(
                    "发现未清理的历史恢复账本：" + ", ".join(unclean_journals)
                )
            clients = _client_info(
                claude_bin=arguments.claude_bin,
                codex_bin=arguments.codex_bin,
                expected_claude_version=arguments.expected_claude_version,
                expected_codex_version=arguments.expected_codex_version,
                expected_claude_sha256=arguments.expected_claude_sha256,
                expected_codex_sha256=arguments.expected_codex_sha256,
                api_key_env=arguments.api_key_env,
                subjects=subjects,
            )
            runtime = _preflight_dependencies(
                arguments=arguments,
                plans=plans,
                evidence=evidence,
            )
            oauth_state = (
                _verify_oauth_state(arguments, subjects, oauth_claude_secret)
                if any(plan.task == "oauth" for plan in plans)
                else None
            )
            with _termination_guard():
                for plan in plans:
                    task_runtime = dict(runtime)
                    task_runtime["auth_preflight"] = (
                        oauth_state
                        if plan.task == "oauth"
                        else {"kind": "sub2api_runtime_key", "present": True}
                    )
                    known_secrets = dict(additional_secrets)
                    if plan.task == "api" and api_secret:
                        known_secrets["api_runtime_key_value"] = api_secret
                    if plan.task == "oauth" and oauth_claude_secret:
                        known_secrets[
                            "claude_oauth_runtime_access_token_value"
                        ] = oauth_claude_secret
                    results.append(
                        _run_campaign(
                            plan=plan,
                            arguments=arguments,
                            clients=clients,
                            api_secret=api_secret if plan.task == "api" else None,
                            oauth_claude_secret=(
                                oauth_claude_secret if plan.task == "oauth" else None
                            ),
                            runtime=task_runtime,
                            known_secrets=known_secrets,
                        )
                    )
        print(json.dumps({**safe_plan, "results": results}, ensure_ascii=False, indent=2))
        return 0
    except CaptureInterrupted as error:
        print(f"抓包任务已中断：{error}", file=sys.stderr)
        return 128 + error.signum
    except KeyboardInterrupt:
        print("抓包任务已中断：收到 SIGINT。", file=sys.stderr)
        return 130
    except (
        ConfigurationError,
        RuntimeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        print(f"抓包任务失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
