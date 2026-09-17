#!/usr/bin/env python3
"""在 ARM64 上离线演练 Codex 升级全部 Job，并封存可重放收据。"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import incremental_recovery


FACTS_SCHEMA = "codex-upgrade-job-rehearsal-facts/v1"
RECEIPT_SCHEMA = "codex-upgrade-job-rehearsal-receipt/v1"
EXECUTION_CONTRACT_SCHEMA = "codex-upgrade-job-rehearsal-contract/v2"
PRODUCER_SCHEMA = "codex-upgrade-job-rehearsal-producer/v1"
PRODUCER_VERSION = "1"
INCREMENTAL_NOOP_SCHEMA = "codex-upgrade-incremental-noop/v1"
INCREMENTAL_NOOP_STATUS = "incremental-noop"
EXPECTED_ARCHITECTURE = "linux/arm64"
MAX_JSON_BYTES = 16 * 1024 * 1024
ZSTD_FRAME = bytes.fromhex("28b52ffd045829000068656c6c6fa36d9f88")
ZSTD_OUTPUT = b"hello"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

# CLI 与 Formal 编排器使用同一组默认上限；演练是正式抓包前的离线门禁，
# 也必须有明确的单调墙钟边界，不能靠每个 docker 命令的固定 timeout 叠加。
DEFAULT_ATTEMPT_WALL_SECONDS = 90 * 60
MAX_ATTEMPT_WALL_SECONDS = 6 * 60 * 60
DEFAULT_HEARTBEAT_SECONDS = 30
MAX_HEARTBEAT_SECONDS = 5 * 60

# Codex 正式 Job 同时通过宿主兼容路径和容器历史路径访问运行数据。两个
# 宽泛父根必须保持只读，只有 runs／runtime 两个登记子树可以写入；否则
# 语法演练会错误放行、正式抓包才在 mkdir 阶段失败。
EXPECTED_HOST_DATA_ROOT = PurePosixPath("/root/docker/capture-cli/data")
EXPECTED_CAPTURE_CONTAINER_ROOT = PurePosixPath("/root/oauth-capture")
CAPTURE_CONTAINER_ALIAS = PurePosixPath("/capture")
WRITABLE_CAPTURE_NAMESPACES = ("runs", "runtime")
FAILED_EVIDENCE_ARCHIVE_SUFFIX = ".failed-attempt1"
FAILURE_LIFECYCLE_SCHEMA = "codex-upgrade-failure-lifecycle-probe/v1"
FAILURE_LIFECYCLE_WORKER_SCHEMA = "codex-upgrade-failure-lifecycle-worker/v1"
FAILURE_LIFECYCLE_MARKER_SCHEMA = "codex-upgrade-failure-lifecycle-marker/v1"
FAILURE_LIFECYCLE_JOB_ID = "vc1-failure-lifecycle-probe"
FAILURE_LIFECYCLE_ATTEMPT_COUNT = 3
FAILURE_LIFECYCLE_DEADLINE_SECONDS = 120

# 完整演练包含大量 docker／脚本语法探针；所有层级都从这里读取同一条
# deadline，避免固定 timeout 的命令串联后突破 attempt 预算。
_ACTIVE_DEADLINE: incremental_recovery.WallClockDeadline | None = None
_ACTIVE_HEARTBEAT: Any | None = None

# 这些字段会改变 Job 的真实启动环境，preflight 与 Formal 必须逐项一致。
EXECUTION_CONFIGURATION_FIELDS = (
    "runtime_image",
    "model",
    "lite_model",
    "capture_root",
    "capture_container",
    "service_container",
    "keeper_container",
    "postgres_container",
    "redis_container",
    "capture_codex_bin",
    "relay_codex_bin",
    "capture_code_mode_host_bin",
    "relay_code_mode_host_bin",
)

# 不执行 Job；只确认所有 wrapper 与内联脚本可能依赖的命令在对应执行域可用。
HOST_REQUIRED_COMMANDS = (
    "awk",
    "base64",
    "bash",
    "chmod",
    "cp",
    "curl",
    "cut",
    "date",
    "docker",
    "find",
    "flock",
    "grep",
    "head",
    "install",
    "jq",
    "mkdir",
    "mv",
    "openssl",
    "python3",
    "rm",
    "rmdir",
    "sed",
    "sha256sum",
    "sort",
    "stat",
    "tail",
    "tee",
    "timeout",
    "uniq",
)
CAPTURE_CONTAINER_REQUIRED_COMMANDS = (
    "bash",
    "bwrap",
    "chmod",
    "cp",
    "curl",
    "cut",
    "env",
    "flock",
    "getent",
    "install",
    "jq",
    "mitmdump",
    "mv",
    "openssl",
    "pgrep",
    "pkill",
    "python3",
    "rm",
    "seq",
    "sha256sum",
    "sh",
    "sleep",
    "stat",
    "tar",
    "tcpdump",
    "timeout",
    "update-ca-certificates",
)

# Job 演练只关心会改变启动命令或路径探针的组件。未知路径归入 shared，
# 由调用方按依赖闭集处理，不会被误判为可复用。
_JOB_COMPONENT_FILES = {
    "producer": {
        "capture.py",
        "pcap_clienthello.py",
        "scrub_raw_bytes.py",
        "extract_capture_records.py",
        "h1_wire_probe.py",
        "relay_extract.py",
        "upstream_byte_relay.py",
    },
    "relay": {
        "run_official_relay_scenario.sh",
        "run_candidate_core_capture.sh",
        "run_candidate_aux_capture.sh",
        "run_sub2api_direct_matrix.sh",
        "run_sub2api_openai_mitm_matrix.sh",
        "run_h1_wire_probe.sh",
        "run_images_wire_probe.sh",
        "run_official_codex_compact_capture.sh",
        "run_official_http_fallback_baseline.sh",
    },
    "runtime": {
        "runtime_host_receipt.py",
        "runtime_image/README.md",
    },
}

# 评估器本身不会产生抓包字节。把它们单独列出，评估器修复时可以只重放受
# 影响的 Job／门禁；未登记的新文件仍然落到 shared，保持 fail-close。
_EVALUATOR_FILES = {
    "codex_upgrade.py",
    "codex_upgrade_evidence_permissions.py",
    "codex_upgrade_job_rehearsal_receipt.py",
    "incremental_recovery.py",
    "codex_upgrade_gate_receipt.py",
    "candidate_rule_assertion.py",
}

# 这些版本化清单只改变分类、断言或 Job 定义的生成输入，不会直接产生抓包
# 字节。Job 定义发生变化时由逐 Job document/result_key 精确失效；若把清单
# 本身归入 shared，会把一次评估侧修复错误扩大成全部 Job 重跑。
_EVALUATOR_FILE_PREFIXES = (
    "candidate_rule_expectations_",
    "codex_upgrade_rules_",
    "codex_upgrade_scenarios_",
    "codex_upgrade_evidence_labels_",
)

_CONTROL_FILE_PREFIXES = (
    "codex_upgrade_supervisor",
    "codex_upgrade_campaign_lease",
)

_MITM_FINGERPRINT_RUNNER_FILES = {
    "build_fingerprint_proxy.sh",
    "prewarm_codex_home.py",
    "runtime_scripts/run_fingerprint_mitm_pair.sh",
    "runtime_scripts/start_mitm.sh",
}


def _job_component_for_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    basename = path.rsplit("/", 1)[-1]
    if basename.startswith(_CONTROL_FILE_PREFIXES):
        return "control"
    # MITM checkpoint helper 只被对应矩阵 runner 导入。它不是全局 shared
    # 依赖；否则新增或修复 helper 会误使没有脚本 argv 的 frozen-aux Job
    # 失效，破坏“只补跑两个 MITM Job”的闭集。
    if (
        basename == "mitm_scenario_checkpoint.py"
        or normalized in _MITM_FINGERPRINT_RUNNER_FILES
        or normalized.startswith("fingerprint_proxy/")
    ):
        return "runner.run_sub2api_openai_mitm_matrix"
    if basename.startswith(("run_", "drive_")):
        stem = basename.rsplit(".", 1)[0]
        return f"runner.{stem}"
    for component, names in _JOB_COMPONENT_FILES.items():
        if basename in names:
            if component == "relay":
                stem = basename.rsplit(".", 1)[0]
                return f"runner.{stem}"
            return component
    if (
        path.endswith(".schema.json")
        or basename in _EVALUATOR_FILES
        or basename.startswith(_EVALUATOR_FILE_PREFIXES)
    ):
        return "evaluator"
    return "shared"


def _job_dependencies(document: Mapping[str, Any]) -> list[str]:
    """从 Job 文档的实际 argv 推导组件依赖。"""

    dependencies: set[str] = set()
    steps = document.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            argv = step.get("argv")
            if not isinstance(argv, list):
                continue
            if argv and argv[0] == "docker":
                dependencies.add("producer")
            for raw in argv:
                if not isinstance(raw, str):
                    continue
                normalized = raw.replace("\\", "/")
                if "tools/official_client_capture/" in normalized:
                    normalized = normalized.split(
                        "tools/official_client_capture/", 1
                    )[1]
                if normalized.endswith((".py", ".sh")):
                    dependencies.add(_job_component_for_path(normalized))
    return sorted(dependencies or {"shared"})


class JobRehearsalReceiptError(ValueError):
    """完整 Job 离线演练不完整、发生漂移或无法重放。"""


def _json_bytes(value: Any, *, newline: bool) -> bytes:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return raw + (b"\n" if newline else b"")


def _canonical(value: Any) -> bytes:
    return _json_bytes(value, newline=True)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value, newline=False)).hexdigest()


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
        raise JobRehearsalReceiptError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise JobRehearsalReceiptError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise JobRehearsalReceiptError(f"{label}不是安全标识")
    return value


def _rfc3339(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise JobRehearsalReceiptError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise JobRehearsalReceiptError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise JobRehearsalReceiptError(f"{label}缺少时区")
    return value


def _private_root(root: Path) -> Path:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise JobRehearsalReceiptError(
            "evidence root 必须是现有非符号链接绝对目录"
        )
    resolved = root.resolve(strict=True)
    if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise JobRehearsalReceiptError("evidence root 权限必须是 0700")
    return resolved


def _relative(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise JobRehearsalReceiptError(f"{label}必须是证据根内 POSIX 相对路径")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise JobRehearsalReceiptError(f"{label}路径不规范")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise JobRehearsalReceiptError(f"{label}路径包含符号链接")
    try:
        current.resolve(strict=current.exists()).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise JobRehearsalReceiptError(f"{label}越过 evidence root") from error
    return current


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise JobRehearsalReceiptError(f"{label}不是可信普通文件")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise JobRehearsalReceiptError(f"{label}权限必须是 0600")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise JobRehearsalReceiptError(f"{label}大小非法")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise JobRehearsalReceiptError(f"{label}不是合法 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise JobRehearsalReceiptError(f"{label}顶层必须是对象")
    return payload, raw


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise JobRehearsalReceiptError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise JobRehearsalReceiptError("输出父目录必须是 0700 非符号链接目录")
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


def _run(
    argv: list[str],
    label: str,
    timeout: int = 60,
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> bytes:
    active_deadline = deadline if deadline is not None else _ACTIVE_DEADLINE
    active_heartbeat = heartbeat if heartbeat is not None else _ACTIVE_HEARTBEAT
    try:
        if active_deadline is not None:
            completed = incremental_recovery.run_bounded_subprocess(
                argv,
                timeout=timeout,
                deadline=active_deadline,
                operation=label,
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
        raise JobRehearsalReceiptError(f"{label}执行失败：{error}") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[:500].strip()
        if not message and label == "VC-1 失败生命周期 campaign-run v2 演练":
            # campaign-run 对动作失败返回结构化 stdout 和非零退出码，stderr
            # 可能为空；必须保留这份父监督器诊断，不能再次退化成空错误。
            message = completed.stdout.decode("utf-8", errors="replace")[:500].strip()
        if not message:
            message = f"returncode={completed.returncode}，子进程未返回诊断"
        raise JobRehearsalReceiptError(f"{label}失败：{message}")
    return completed.stdout


def _job_templates(
    target_scenario: Mapping[str, Any],
    extra_jobs: Mapping[str, Any] | None,
    suite: str,
) -> list[dict[str, Any]]:
    raw_jobs = target_scenario.get("capture_jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise JobRehearsalReceiptError("target 场景清单没有 capture_jobs")
    combined = list(raw_jobs)
    if extra_jobs is not None:
        extra = extra_jobs.get("jobs")
        if not isinstance(extra, list):
            raise JobRehearsalReceiptError("extra_jobs.jobs 非法")
        combined.extend(extra)
    selected: list[dict[str, Any]] = []
    for index, raw in enumerate(combined, 1):
        if not isinstance(raw, dict):
            raise JobRehearsalReceiptError(f"Job 模板 {index} 不是对象")
        job_id = raw.get("id")
        phase = raw.get("phase")
        suites = raw.get("suites", ["full"])
        steps = raw.get("steps")
        if (
            not isinstance(job_id, str)
            or not SAFE_ID_RE.fullmatch(job_id)
            or phase not in {"official", "candidate"}
            or not isinstance(suites, list)
            or not all(isinstance(item, str) for item in suites)
            or not isinstance(steps, list)
            or not steps
        ):
            raise JobRehearsalReceiptError(f"Job 模板 {index} 身份或步骤非法")
        if suite in suites:
            selected.append(dict(raw))
    ids = [str(item["id"]) for item in selected]
    if not selected or len(ids) != len(set(ids)):
        raise JobRehearsalReceiptError("目标 Job 集为空或 ID 重复")
    return sorted(selected, key=lambda item: (str(item["phase"]), str(item["id"])))


def _target_evidence_label_declaration_sha256(
    target_version: str,
    target_scenario: Mapping[str, Any],
    *,
    tool_root: Path | None = None,
) -> str:
    """验证目标版本证据标签声明完整覆盖正式 Job 集。"""

    from tools.official_client_capture import build_evidence_catalog

    root = tool_root or Path(__file__).resolve().parent
    version_key = target_version.replace(".", "_")
    path = root / f"codex_upgrade_evidence_labels_{version_key}.json"
    if path.is_symlink() or not path.is_file():
        raise JobRehearsalReceiptError(
            f"目标版本 {target_version} 缺少证据标签声明：{path}"
        )
    try:
        declaration = build_evidence_catalog.load_label_declaration(
            path,
            expected_codex_version=target_version,
        )
    except (OSError, build_evidence_catalog.EvidenceCatalogError) as error:
        raise JobRehearsalReceiptError(
            f"目标版本 {target_version} 证据标签声明非法：{error}"
        ) from error

    raw_jobs = target_scenario.get("capture_jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise JobRehearsalReceiptError("target 场景清单没有 capture_jobs")
    scenario_jobs = {
        str(item.get("id")): str(item.get("phase"))
        for item in raw_jobs
        if isinstance(item, dict)
    }
    declared_jobs = {
        str(item.get("job_id")): str(item.get("side"))
        for item in declaration["entries"]
        if isinstance(item, dict)
    }
    # 声明必须覆盖 Campaign 冻结场景里的每个 Job 且阶段一致；声明多出的 Job
    # 允许存在。标签文件来自部署树当前版本，而场景冻结在 Campaign 内：同一目标
    # 版本新增 Job（2026-09-18 candidate-trace-test）后，旧 Campaign 的只读对账、
    # 状态查询若仍要求精确相等就会被整体锁死。多出的声明对旧 Campaign 无害——
    # 证据目录只按 Campaign 自己的 Job 取声明。
    missing = sorted(set(scenario_jobs) - set(declared_jobs))
    mismatched = sorted(
        job_id
        for job_id in set(scenario_jobs) & set(declared_jobs)
        if scenario_jobs[job_id] != declared_jobs[job_id]
    )
    if (
        len(scenario_jobs) != len(raw_jobs)
        or len(declared_jobs) != len(declaration["entries"])
        or missing
        or mismatched
    ):
        extra = sorted(set(declared_jobs) - set(scenario_jobs))
        raise JobRehearsalReceiptError(
            "目标证据标签声明未覆盖正式 Job 集："
            f"missing={missing} extra={extra} phase_mismatch={mismatched}"
        )
    return _sha256_file(path)


def build_execution_contract(
    *,
    target_version: str,
    target_sha256: str,
    target_package_sha256: str,
    target_code_mode_host_sha256: str,
    suite: str,
    tool_files_sha256: str,
    configuration: Mapping[str, Any],
    target_scenario: Mapping[str, Any],
    extra_jobs: Mapping[str, Any] | None,
    wire_producer_sha256: str | None = None,
    policy_sha256: str | None = None,
) -> dict[str, Any]:
    """生成 preflight 与 Formal 之间不含 Campaign ID 的稳定执行合同。

    A2-1：v2 Campaign 的合同额外记录当前有效 wire 身份与策略摘要；执行前的工具
    比对以它们为准，整树摘要只用于三副本互等与审计。
    """

    for label, value in (("wire_producer_sha256", wire_producer_sha256), ("policy_sha256", policy_sha256)):
        if value is not None and not SHA256_RE.fullmatch(str(value)):
            raise JobRehearsalReceiptError(f"{label} 不是 SHA-256")
    if (wire_producer_sha256 is None) != (policy_sha256 is None):
        raise JobRehearsalReceiptError("wire_producer_sha256 与 policy_sha256 必须同时给出")

    for label, value in (
        ("target_sha256", target_sha256),
        ("target_package_sha256", target_package_sha256),
        ("target_code_mode_host_sha256", target_code_mode_host_sha256),
        ("tool_files_sha256", tool_files_sha256),
    ):
        if not SHA256_RE.fullmatch(str(value)):
            raise JobRehearsalReceiptError(f"{label} 不是 SHA-256")
    if not isinstance(target_version, str) or not re.fullmatch(
        r"\d+\.\d+\.\d+", target_version
    ):
        raise JobRehearsalReceiptError("target_version 非法")
    if suite not in {"core", "full"}:
        raise JobRehearsalReceiptError("suite 非法")
    frozen_configuration = {
        field: configuration.get(field) for field in EXECUTION_CONFIGURATION_FIELDS
    }
    missing = [
        field
        for field, value in frozen_configuration.items()
        if value is None or (isinstance(value, str) and not value)
    ]
    if missing:
        raise JobRehearsalReceiptError(
            "Job 执行配置存在空值：" + "、".join(missing)
        )
    evidence_label_declaration_sha256 = (
        _target_evidence_label_declaration_sha256(
            target_version,
            target_scenario,
        )
    )
    templates = _job_templates(target_scenario, extra_jobs, suite)
    job_phases = {str(item["id"]): str(item["phase"]) for item in templates}
    step_counts = {
        str(item["id"]): len(item.get("steps", [])) for item in templates
    }
    c2pa: dict[str, dict[str, str]] = {}
    for item in templates:
        job_id = str(item["id"])
        if job_id not in {
            "official-relay-file-upload-c2pa-negative",
            "official-relay-file-upload-c2pa-positive",
        }:
            continue
        steps = item.get("steps")
        environment = steps[0].get("environment") if isinstance(steps, list) else None
        if not isinstance(environment, dict):
            raise JobRehearsalReceiptError(f"{job_id} 缺少环境变量")
        c2pa[job_id] = {
            "scenario_job_id": str(environment.get("SCENARIO_JOB_ID", "")),
            "expectation": str(environment.get("A14_C2PA_EXPECTATION", "")),
        }
    expected_c2pa = {
        "official-relay-file-upload-c2pa-negative": {
            "scenario_job_id": "official-relay-file-upload-c2pa-negative",
            "expectation": "negative",
        },
        "official-relay-file-upload-c2pa-positive": {
            "scenario_job_id": "official-relay-file-upload-c2pa-positive",
            "expectation": "positive",
        },
    }
    # 只在清单包含 A14 双 Job 时要求完整精确身份；旧版本测试清单不被强行扩写。
    if c2pa and c2pa != expected_c2pa:
        raise JobRehearsalReceiptError("A14 C2PA 正负 Job 身份未精确分离")
    phase_counts = {
        phase: sum(1 for value in job_phases.values() if value == phase)
        for phase in ("official", "candidate")
    }
    return {
        "schema_version": EXECUTION_CONTRACT_SCHEMA,
        "target_version": target_version,
        "target_sha256": target_sha256,
        "target_package_sha256": target_package_sha256,
        "target_code_mode_host_sha256": target_code_mode_host_sha256,
        "suite": suite,
        "tool_files_sha256": tool_files_sha256,
        **({"wire_producer_sha256": wire_producer_sha256, "policy_sha256": policy_sha256} if wire_producer_sha256 is not None else {}),
        "target_scenario_sha256": _fingerprint(target_scenario),
        "evidence_label_declaration_sha256": (
            evidence_label_declaration_sha256
        ),
        "extra_jobs_sha256": (
            _fingerprint(extra_jobs) if extra_jobs is not None else None
        ),
        "configuration": frozen_configuration,
        "job_count": len(templates),
        "phase_counts": phase_counts,
        "job_ids": sorted(job_phases),
        "job_phases": dict(sorted(job_phases.items())),
        "step_counts": dict(sorted(step_counts.items())),
        "job_templates_sha256": _fingerprint(templates),
        "c2pa_job_identities": c2pa,
    }


def execution_contract_sha256(contract: Mapping[str, Any]) -> str:
    validate_execution_contract(dict(contract))
    return _fingerprint(contract)


CONTRACT_V2_IDENTITY_FIELDS = frozenset({"wire_producer_sha256", "policy_sha256"})


def _contract_tool_matches(contract: Mapping[str, Any], managed_files_sha256: str, current_identity: Mapping[str, Any]) -> bool:
    """A2-1：合同带 wire 身份时按 wire＋策略比当前工具，否则按整树。"""

    if contract.get("wire_producer_sha256") is not None:
        return (
            current_identity.get("wire_producer_sha256") == contract.get("wire_producer_sha256")
            and current_identity.get("policy_sha256") == contract.get("policy_sha256")
        )
    return managed_files_sha256 == contract.get("tool_files_sha256")


def validate_execution_contract(contract: dict[str, Any]) -> dict[str, Any]:
    v2_fields = CONTRACT_V2_IDENTITY_FIELDS if isinstance(contract, dict) and CONTRACT_V2_IDENTITY_FIELDS & set(contract) else frozenset()
    _expect(
        contract,
        {
            "schema_version",
            "target_version",
            "target_sha256",
            "target_package_sha256",
            "target_code_mode_host_sha256",
            "suite",
            "tool_files_sha256",
            *v2_fields,
            "target_scenario_sha256",
            "evidence_label_declaration_sha256",
            "extra_jobs_sha256",
            "configuration",
            "job_count",
            "phase_counts",
            "job_ids",
            "job_phases",
            "step_counts",
            "job_templates_sha256",
            "c2pa_job_identities",
        },
        "execution_contract",
    )
    if contract.get("schema_version") != EXECUTION_CONTRACT_SCHEMA:
        raise JobRehearsalReceiptError("execution_contract.schema_version 不匹配")
    for field in (
        "target_sha256",
        "target_package_sha256",
        "target_code_mode_host_sha256",
        "tool_files_sha256",
        "target_scenario_sha256",
        "evidence_label_declaration_sha256",
        "job_templates_sha256",
    ):
        if not SHA256_RE.fullmatch(str(contract.get(field, ""))):
            raise JobRehearsalReceiptError(f"execution_contract.{field} 非法")
    extra_sha = contract.get("extra_jobs_sha256")
    if extra_sha is not None and not SHA256_RE.fullmatch(str(extra_sha)):
        raise JobRehearsalReceiptError("execution_contract.extra_jobs_sha256 非法")
    configuration = contract.get("configuration")
    if not isinstance(configuration, dict) or set(configuration) != set(
        EXECUTION_CONFIGURATION_FIELDS
    ):
        raise JobRehearsalReceiptError("execution_contract.configuration 不闭合")
    if any(
        value is None or (isinstance(value, str) and not value)
        for value in configuration.values()
    ):
        raise JobRehearsalReceiptError("execution_contract.configuration 含空值")
    job_ids = contract.get("job_ids")
    phases = contract.get("job_phases")
    counts = contract.get("step_counts")
    phase_counts = contract.get("phase_counts")
    if (
        not isinstance(job_ids, list)
        or not job_ids
        or job_ids != sorted(job_ids)
        or len(job_ids) != len(set(job_ids))
        or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in job_ids)
        or not isinstance(phases, dict)
        or set(phases) != set(job_ids)
        or not all(value in {"official", "candidate"} for value in phases.values())
        or not isinstance(counts, dict)
        or set(counts) != set(job_ids)
        or not all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in counts.values())
        or not isinstance(phase_counts, dict)
        or set(phase_counts) != {"official", "candidate"}
        or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in phase_counts.values())
        or contract.get("job_count") != len(job_ids)
        or sum(phase_counts.values()) != len(job_ids)
        or any(
            phase_counts[phase] != sum(value == phase for value in phases.values())
            for phase in phase_counts
        )
    ):
        raise JobRehearsalReceiptError("execution_contract Job 集非法")
    c2pa = contract.get("c2pa_job_identities")
    if not isinstance(c2pa, dict):
        raise JobRehearsalReceiptError("execution_contract C2PA 身份非法")
    return contract


def _host_tool_entries(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not root.is_dir() or root.is_symlink():
        raise JobRehearsalReceiptError(f"工具树不存在或不可信：{root}")
    entries: list[dict[str, str]] = []
    symlinks: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.is_symlink():
            if path.suffix in {".py", ".sh", ".json"}:
                symlinks.append(relative.as_posix())
            continue
        if (
            path.is_file()
            and path.suffix in {".py", ".sh", ".json"}
            and "tests" not in relative.parts
            and "versions" not in relative.parts
            and "__pycache__" not in relative.parts
        ):
            entries.append(
                {"path": relative.as_posix(), "sha256": _sha256_file(path)}
            )
    return entries, symlinks


def _tool_tree_summary(root: Path) -> dict[str, Any]:
    entries, symlinks = _host_tool_entries(root)
    if not entries or symlinks:
        raise JobRehearsalReceiptError(
            f"工具树为空或含受管符号链接：{root}；symlinks={symlinks[:5]}"
        )
    return {
        "root": str(root.resolve(strict=True)),
        "entry_count": len(entries),
        "files_sha256": _fingerprint({"entries": entries}),
        "entries": entries,
    }


def _component_summary(tree: Mapping[str, Any]) -> dict[str, Any]:
    entries = tree.get("entries")
    if not isinstance(entries, list) or any(
        not isinstance(item, Mapping)
        or set(item) != {"path", "sha256"}
        for item in entries
    ):
        raise JobRehearsalReceiptError("工具树缺少文件清单，无法生成组件摘要")
    normalized_entries = [dict(item) for item in entries]
    assignments = {
        str(item["path"]): _job_component_for_path(str(item["path"]))
        for item in normalized_entries
    }
    try:
        return incremental_recovery.build_component_identities(
            normalized_entries,
            assignments,
            default_component="shared",
        )
    except incremental_recovery.IncrementalRecoveryError as error:
        raise JobRehearsalReceiptError(f"工具组件摘要非法：{error}") from error


def _job_incremental_metadata(
    document: Mapping[str, Any],
    component_summary: Mapping[str, Any],
    *,
    environment_sha256: str | None = None,
) -> dict[str, Any]:
    dependencies = _job_dependencies(document)
    components = component_summary.get("components")
    if not isinstance(components, Mapping):
        raise JobRehearsalReceiptError("工具组件摘要缺少 components")
    rows = []
    component_digests: dict[str, str] = {}
    for name in dependencies:
        value = components.get(name)
        if not isinstance(value, Mapping) or not SHA256_RE.fullmatch(
            str(value.get("sha256", ""))
        ):
            raise JobRehearsalReceiptError(f"Job 依赖组件摘要缺失：{name}")
        component_digests[name] = str(value["sha256"])
        rows.append(
            {
                "id": f"tool:{name}",
                "status": "passed",
                "result_sha256": str(value["sha256"]),
            }
        )
    dependency_sha = incremental_recovery.dependency_digest(rows)
    input_sha = _fingerprint(document)
    env_sha = environment_sha256 or _fingerprint(
        {
            "phase": document.get("phase"),
            "job_id": document.get("id"),
        }
    )
    return {
        "components": dependencies,
        "component_digests": component_digests,
        "dependency_sha256": dependency_sha,
        "input_sha256": input_sha,
        "environment_sha256": env_sha,
        "result_key": incremental_recovery.result_key(
            component="job",
            item_id=str(document.get("id")),
            input_sha256=input_sha,
            environment_sha256=env_sha,
            dependency_sha256=dependency_sha,
        ),
    }


def _contract_reuse_identity(contract: Mapping[str, Any]) -> dict[str, Any]:
    """提取跨工具修复可比较的执行坐标。

    ``tool_files_sha256``、场景总摘要和 Job 集合摘要只是全局审计字段；把它们
    放进复用判定会导致改一个评估器或一个 Job 就把整轮清空。真正的失效由每个
    Job 的输入／环境／组件依赖键决定。目标二进制和运行配置仍属于共享前提，
    变化时全部结果失效；证据标签只影响已封存证据的离线编目，不属于请求
    生产合同。
    """

    stable_fields = (
        "schema_version",
        "target_version",
        "target_sha256",
        "target_package_sha256",
        "target_code_mode_host_sha256",
        "suite",
        "configuration",
        "c2pa_job_identities",
    )
    return {field: contract.get(field) for field in stable_fields}


def _facts_component_summary(facts: Mapping[str, Any]) -> dict[str, Any]:
    """按当前依赖映射只读重算历史组件摘要。

    历史 ``tool_components`` 仍由 facts 校验器保护，但不能把旧版粗粒度
    ``relay/shared`` 分组继续当作当前失效边界。只要历史工具树条目完整，
    就从原始路径和摘要重算；没有条目的更旧收据才回退到原分组。
    """

    trees = facts.get("tool_trees")
    tree = trees.get("managed_host") if isinstance(trees, Mapping) else None
    if isinstance(tree, Mapping) and isinstance(tree.get("entries"), list):
        return _component_summary(tree)
    value = facts.get("tool_components")
    if isinstance(value, Mapping):
        return dict(value)
    raise JobRehearsalReceiptError("前序 facts 缺少工具组件或 managed_host 摘要")


def _source_binding(
    receipt_relative: str,
    receipt_raw: bytes,
) -> dict[str, Any]:
    """生成复用结果指向的只读来源收据绑定。"""

    return {
        "path": receipt_relative,
        "sha256": _sha256_bytes(receipt_raw),
        "bytes": len(receipt_raw),
    }


CHECKPOINT_CONTEXT_SCHEMA = "codex-upgrade-rehearsal-checkpoint-context/v1"


def _checkpoint_context(
    *,
    campaign_id: str,
    contract: Mapping[str, Any],
    component_summary: Mapping[str, Any],
    plan: Mapping[str, Any],
    previous_source: Mapping[str, Any] | None,
) -> dict[str, str]:
    """生成 checkpoint 的不可变运行上下文。

    同一目录不能混入不同 Campaign、工具组件或增量计划的记录；否则恢复时
    很容易把另一轮的通过结果误当成当前结果。上下文只含摘要，不复制大体积
    证据。
    """

    core = {
        # checkpoint 存储器自身使用 ``schema_version`` 标识记录格式；
        # 运行上下文必须使用独立字段，避免在 append 时覆盖存储器 schema。
        "context_schema_version": CHECKPOINT_CONTEXT_SCHEMA,
        "campaign_id": str(campaign_id),
        "execution_contract_sha256": execution_contract_sha256(dict(contract)),
        "component_identity_sha256": incremental_recovery.digest(component_summary),
        "plan_sha256": str(plan.get("plan_sha256", "")),
        "previous_receipt_sha256": (
            str(previous_source.get("sha256"))
            if isinstance(previous_source, Mapping)
            else ""
        ),
    }
    for key in (
        "campaign_id",
        "execution_contract_sha256",
        "component_identity_sha256",
        "plan_sha256",
    ):
        if not core[key]:
            raise JobRehearsalReceiptError(f"checkpoint 上下文字段缺失：{key}")
    core["context_sha256"] = incremental_recovery.digest(core)
    return core


def _checkpoint_result_binding(
    store: incremental_recovery.CheckpointStore,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """把 checkpoint 记录本身作为只读复用来源。"""

    sequence = record.get("checkpoint_sequence")
    path = store.record_path(sequence)
    raw = path.read_bytes()
    return {
        "path": path.name,
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
    }


def _load_checkpoint_results(
    store: incremental_recovery.CheckpointStore,
    *,
    context: Mapping[str, str],
    jobs: Iterable[Any],
    component_summary: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """读取可续作的最后结果，并校验每条记录的运行上下文。"""

    job_map = {str(job.job_id): job for job in jobs}
    records = store.records()
    context_keys = (
        "context_sha256",
        "campaign_id",
        "execution_contract_sha256",
        "component_identity_sha256",
        "plan_sha256",
        "previous_receipt_sha256",
    )
    # 不能只检查每个 Job 的最后一条记录：若有人把另一轮记录追加到链中，
    # 后续再追加当前上下文会掩盖早期漂移。整条链的每一项都必须绑定同一
    # 个不可变运行上下文。
    if any(
        any(record.get(key) != context.get(key) for key in context_keys)
        for record in records
    ):
        raise JobRehearsalReceiptError("checkpoint 运行上下文漂移")
    latest = store.latest_by_item()
    output: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for item_id, record in latest.items():
        if item_id not in job_map:
            raise JobRehearsalReceiptError(
                f"checkpoint 含当前 Job 集之外的结果：{item_id}"
            )
        if any(record.get(key) != context.get(key) for key in context_keys):
            raise JobRehearsalReceiptError("checkpoint 运行上下文漂移")
        result = record.get("result")
        if not isinstance(result, dict):
            raise JobRehearsalReceiptError(
                f"checkpoint {item_id} 缺少可恢复的 Job 结果"
            )
        if record.get("result_sha256") != incremental_recovery.digest(result):
            raise JobRehearsalReceiptError(f"checkpoint {item_id} 结果摘要漂移")
        if result.get("id") != item_id:
            raise JobRehearsalReceiptError(f"checkpoint {item_id} 结果 ID 漂移")
        if result.get("status") not in {"passed", "failed", "complete"}:
            raise JobRehearsalReceiptError(f"checkpoint {item_id} 状态不可恢复")
        expected = _job_incremental_metadata(_job_document(job_map[item_id]), component_summary)
        if result.get("incremental_result_key") != expected["result_key"]:
            raise JobRehearsalReceiptError(f"checkpoint {item_id} 结果键漂移")
        output[item_id] = (record, result)
    return output


def _container_tool_tree(container: str, root: str) -> dict[str, Any]:
    probe = r'''
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
if not root.is_dir() or root.is_symlink():
    raise SystemExit("container tool root unavailable")
entries=[]; symlinks=[]
for path in sorted(root.rglob("*")):
    relative=path.relative_to(root)
    if path.is_symlink():
        if path.suffix in {".py",".sh",".json"}: symlinks.append(relative.as_posix())
        continue
    if (path.is_file() and path.suffix in {".py",".sh",".json"}
        and "tests" not in relative.parts and "versions" not in relative.parts
        and "__pycache__" not in relative.parts):
        entries.append({"path":relative.as_posix(),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
print(json.dumps({"root":str(root.resolve()),"entries":entries,"symlinks":symlinks},sort_keys=True))
'''
    raw = _run(
        ["docker", "exec", container, "python3", "-c", probe, root],
        "capture-cli 容器工具树探针",
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobRehearsalReceiptError("容器工具树探针输出非法") from error
    if (
        not isinstance(payload, dict)
        or payload.get("symlinks") != []
        or not isinstance(payload.get("entries"), list)
        or not payload["entries"]
    ):
        raise JobRehearsalReceiptError("容器工具树为空或含受管符号链接")
    entries = payload["entries"]
    return {
        "root": str(payload.get("root", "")),
        "entry_count": len(entries),
        "files_sha256": _fingerprint({"entries": entries}),
        "entries": entries,
    }


def _command_binding(name: str, path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise JobRehearsalReceiptError(f"命令不可执行：{name} -> {resolved}")
    return {
        "name": name,
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
        "status": "passed",
    }


def _host_dependencies() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for name in HOST_REQUIRED_COMMANDS:
        path = shutil.which(name)
        if not path:
            raise JobRehearsalReceiptError(f"ARM64 宿主缺少依赖：{name}")
        output.append(_command_binding(name, Path(path)))
    return output


def _container_dependencies(container: str) -> list[dict[str, Any]]:
    probe = r'''
import hashlib,json,os,pathlib,shutil,sys
output=[]
for name in sys.argv[1:]:
    found=shutil.which(name)
    if not found: raise SystemExit("missing:"+name)
    resolved=pathlib.Path(os.path.realpath(found))
    if not resolved.is_file() or not os.access(resolved,os.X_OK): raise SystemExit("not-executable:"+name)
    output.append({"name":name,"path":str(resolved),"sha256":hashlib.sha256(resolved.read_bytes()).hexdigest(),"status":"passed"})
print(json.dumps(output,sort_keys=True))
'''
    raw = _run(
        [
            "docker",
            "exec",
            container,
            "python3",
            "-c",
            probe,
            *CAPTURE_CONTAINER_REQUIRED_COMMANDS,
        ],
        "capture-cli 依赖探针",
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobRehearsalReceiptError("capture-cli 依赖探针输出非法") from error
    if not isinstance(payload, list):
        raise JobRehearsalReceiptError("capture-cli 依赖探针结果不是数组")
    return payload


def _container_facts(configuration: Mapping[str, Any]) -> list[dict[str, Any]]:
    names = sorted(
        {
            str(configuration[field])
            for field in (
                "capture_container",
                "service_container",
                "keeper_container",
                "postgres_container",
                "redis_container",
            )
        }
    )
    raw = _run(["docker", "inspect", *names], "Job 依赖容器状态探针")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobRehearsalReceiptError("docker inspect 输出非法") from error
    if not isinstance(payload, list) or len(payload) != len(names):
        raise JobRehearsalReceiptError("docker inspect 未完整覆盖 Job 依赖容器")
    output: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise JobRehearsalReceiptError("docker inspect 项不是对象")
        name = str(item.get("Name", "")).lstrip("/")
        state = item.get("State") if isinstance(item.get("State"), dict) else {}
        container_id = str(item.get("Id", ""))
        image_id = str(item.get("Image", ""))
        if (
            name not in names
            or state.get("Running") is not True
            or not CONTAINER_ID_RE.fullmatch(container_id)
            or not IMAGE_ID_RE.fullmatch(image_id)
        ):
            raise JobRehearsalReceiptError(f"Job 依赖容器未运行或身份非法：{name}")
        output.append(
            {
                "name": name,
                "container_id": container_id,
                "image_id": image_id,
                "running": True,
            }
        )
    return sorted(output, key=lambda item: item["name"])


def _absolute_posix_path(value: Any, label: str) -> PurePosixPath:
    """解析容器或宿主绝对路径，拒绝别名化和父目录跳转。"""

    if not isinstance(value, str) or not value or "\\" in value:
        raise JobRehearsalReceiptError(f"{label}不是规范 POSIX 绝对路径")
    parsed = PurePosixPath(value)
    if (
        not parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts[1:])
    ):
        raise JobRehearsalReceiptError(f"{label}不是规范 POSIX 绝对路径")
    return parsed


def _capture_archive_route_probe(
    container: str,
    aliases: list[PurePosixPath],
    host_runs_root: Path,
) -> dict[str, Any]:
    """实测容器创建、宿主归档、跨别名读取与清理闭环。"""

    source_name = f"codex-archive-route-{secrets.token_hex(12)}"
    archive_name = source_name + FAILED_EVIDENCE_ARCHIVE_SUFFIX
    marker_name = "archive-route-marker"
    marker_payload = b"codex-archive-route-probe"
    container_sources = [str(alias / "runs" / source_name) for alias in aliases]
    container_archives = [str(alias / "runs" / archive_name) for alias in aliases]
    host_source = host_runs_root / source_name
    host_archive = host_runs_root / archive_name
    if any(
        path.exists() or path.is_symlink()
        for path in (host_source, host_archive)
    ):
        raise JobRehearsalReceiptError("失败证据归档探针路径发生碰撞")

    create_script = r'''
import json,os,pathlib,stat,sys
sources=[pathlib.Path(item) for item in json.loads(sys.argv[1])]
marker_name=sys.argv[2]
payload=bytes.fromhex(sys.argv[3])
if any(path.exists() or path.is_symlink() for path in sources):
    raise SystemExit("archive-source-exists")
os.mkdir(sources[0],0o700)
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
descriptor=os.open(sources[0]/marker_name,flags,0o600)
with os.fdopen(descriptor,"wb") as stream:
    stream.write(payload)
    stream.flush()
    os.fsync(stream.fileno())
metadata=[path.stat() for path in sources]
if len({(item.st_dev,item.st_ino) for item in metadata})!=1:
    raise SystemExit("archive-source-alias-mismatch")
if any((path/marker_name).read_bytes()!=payload for path in sources):
    raise SystemExit("archive-source-cross-alias-read-failed")
print(json.dumps({
    "status":"created",
    "sources":[str(path) for path in sources],
    "created_via":str(sources[0]),
    "device":metadata[0].st_dev,
    "inode":metadata[0].st_ino,
},sort_keys=True))
'''
    verify_script = r'''
import json,pathlib,sys
sources=[pathlib.Path(item) for item in json.loads(sys.argv[1])]
archives=[pathlib.Path(item) for item in json.loads(sys.argv[2])]
marker_name=sys.argv[3]
payload=bytes.fromhex(sys.argv[4])
if any(path.exists() or path.is_symlink() for path in sources):
    raise SystemExit("archive-source-still-visible")
if any(not path.is_dir() or path.is_symlink() for path in archives):
    raise SystemExit("archive-target-missing")
metadata=[path.stat() for path in archives]
if len({(item.st_dev,item.st_ino) for item in metadata})!=1:
    raise SystemExit("archive-target-alias-mismatch")
if any((path/marker_name).read_bytes()!=payload for path in archives):
    raise SystemExit("archive-target-cross-alias-read-failed")
(archives[0]/marker_name).unlink()
archives[0].rmdir()
if any(path.exists() or path.is_symlink() for path in sources+archives):
    raise SystemExit("archive-route-cleanup-failed")
print(json.dumps({
    "status":"passed",
    "read_via":[str(path) for path in archives],
    "device":metadata[0].st_dev,
    "inode":metadata[0].st_ino,
    "cleanup_verified":True,
},sort_keys=True))
'''

    cleanup_error: OSError | None = None
    try:
        created_raw = _run(
            [
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                create_script,
                json.dumps(container_sources),
                marker_name,
                marker_payload.hex(),
            ],
            "capture-cli 失败证据归档创建探针",
            timeout=30,
        )
        try:
            created = json.loads(created_raw)
        except json.JSONDecodeError as error:
            raise JobRehearsalReceiptError(
                "失败证据归档创建探针输出非法"
            ) from error
        if (
            not isinstance(created, Mapping)
            or created.get("status") != "created"
            or created.get("sources") != container_sources
            or created.get("created_via") != container_sources[0]
            or not isinstance(created.get("device"), int)
            or isinstance(created.get("device"), bool)
            or created["device"] < 0
            or not isinstance(created.get("inode"), int)
            or isinstance(created.get("inode"), bool)
            or created["inode"] <= 0
        ):
            raise JobRehearsalReceiptError("失败证据归档创建事实不完整")
        if host_source.is_symlink() or not host_source.is_dir():
            raise JobRehearsalReceiptError("失败证据宿主映射不存在或不可信")
        source_metadata = host_source.stat()
        if (
            source_metadata.st_dev != created["device"]
            or source_metadata.st_ino != created["inode"]
            or source_metadata.st_dev != host_runs_root.stat().st_dev
        ):
            raise JobRehearsalReceiptError("失败证据容器与宿主映射不同源")
        try:
            host_source.rename(host_archive)
        except OSError as error:
            error_number = error.errno if isinstance(error.errno, int) else "none"
            error_name = (
                errno.errorcode.get(error.errno, "UNKNOWN")
                if isinstance(error.errno, int)
                else "UNKNOWN"
            )
            raise JobRehearsalReceiptError(
                "失败证据宿主归档失败："
                f"error_type={type(error).__name__} "
                f"errno={error_number}({error_name})"
            ) from error
        if host_archive.is_symlink() or not host_archive.is_dir():
            raise JobRehearsalReceiptError("失败证据宿主归档目标不可信")
        archive_metadata = host_archive.stat()
        if (
            archive_metadata.st_dev != created["device"]
            or archive_metadata.st_ino != created["inode"]
        ):
            raise JobRehearsalReceiptError("失败证据宿主归档后 inode 漂移")

        verified_raw = _run(
            [
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                verify_script,
                json.dumps(container_sources),
                json.dumps(container_archives),
                marker_name,
                marker_payload.hex(),
            ],
            "capture-cli 失败证据归档跨别名读取清理探针",
            timeout=30,
        )
        try:
            verified = json.loads(verified_raw)
        except json.JSONDecodeError as error:
            raise JobRehearsalReceiptError(
                "失败证据归档读取探针输出非法"
            ) from error
        if (
            not isinstance(verified, Mapping)
            or verified.get("status") != "passed"
            or verified.get("read_via") != container_archives
            or verified.get("device") != created["device"]
            or verified.get("inode") != created["inode"]
            or verified.get("cleanup_verified") is not True
            or any(
                path.exists() or path.is_symlink()
                for path in (host_source, host_archive)
            )
        ):
            raise JobRehearsalReceiptError("失败证据归档读取清理事实不完整")
        return {
            "status": "passed",
            "namespace": "runs",
            "source_name": source_name,
            "archive_name": archive_name,
            "host_source": str(host_source),
            "host_archive": str(host_archive),
            "container_sources": container_sources,
            "container_archives": container_archives,
            "created_via": container_sources[0],
            "archived_via": str(host_archive),
            "read_via": container_archives,
            "device": created["device"],
            "inode": created["inode"],
            "cleanup_verified": True,
        }
    finally:
        # 探针无论在哪一步失败都只清理本轮随机目录；不递归处理 runs 根，
        # 也不触碰任何正式证据。
        for path in (host_source, host_archive):
            try:
                if path.is_symlink():
                    path.unlink()
                    continue
                marker = path / marker_name
                if marker.is_symlink() or marker.is_file():
                    marker.unlink()
                if path.is_dir():
                    path.rmdir()
            except OSError as error:
                cleanup_error = error
        if cleanup_error is not None or any(
            path.exists() or path.is_symlink()
            for path in (host_source, host_archive)
        ):
            raise JobRehearsalReceiptError("失败证据归档探针清理失败") from cleanup_error


def _cleanup_failure_lifecycle_artifacts(
    host_runs_root: Path,
    source_name: str,
) -> None:
    """只清理一次失败生命周期探针预先声明的四个随机目录。"""

    paths = [host_runs_root / source_name]
    paths.extend(
        host_runs_root / f"{source_name}.failed-attempt{attempt_index}"
        for attempt_index in range(1, FAILURE_LIFECYCLE_ATTEMPT_COUNT + 1)
    )
    for path in paths:
        try:
            path.relative_to(host_runs_root)
        except ValueError as error:
            raise JobRehearsalReceiptError("失败生命周期清理路径越过 runs 根") from error
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _failure_lifecycle_worker(arguments: argparse.Namespace) -> dict[str, Any]:
    """在 campaign-run 子进程内走完真实 Job 失败、重试和归档链。"""

    from tools.official_client_capture import codex_upgrade

    campaign_id = _safe_id(arguments.campaign_id, "campaign_id")
    source_name = _safe_id(arguments.source_name, "source_name")
    container = _safe_id(arguments.capture_container, "capture_container")
    capture_root = _absolute_posix_path(arguments.capture_root, "capture_root")
    host_runs_root = Path(arguments.host_runs_root)
    campaign_dir = Path(arguments.campaign_dir)
    log_root = Path(arguments.log_root)
    output = Path(arguments.output)
    if (
        not host_runs_root.is_absolute()
        or host_runs_root.is_symlink()
        or not host_runs_root.is_dir()
        or not campaign_dir.is_absolute()
        or campaign_dir.is_symlink()
        or not campaign_dir.is_dir()
        or not log_root.is_absolute()
        or log_root.is_symlink()
        or not log_root.is_dir()
        or not output.is_absolute()
        or output.is_symlink()
        or output.exists()
    ):
        raise JobRehearsalReceiptError("失败生命周期 worker 路径不可信")
    if capture_root != EXPECTED_CAPTURE_CONTAINER_ROOT:
        raise JobRehearsalReceiptError("失败生命周期 worker 容器根漂移")
    if stat.S_IMODE(campaign_dir.stat().st_mode) != 0o700 or stat.S_IMODE(
        log_root.stat().st_mode
    ) != 0o700:
        raise JobRehearsalReceiptError("失败生命周期 worker 私有目录必须为 0700")
    try:
        output.parent.resolve(strict=True).relative_to(campaign_dir.parent.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise JobRehearsalReceiptError("失败生命周期 worker 输出越过夹具根") from error

    aliases = sorted({CAPTURE_CONTAINER_ALIAS, capture_root}, key=str)
    logical_source = capture_root / "runs" / source_name
    marker_name = "failure-lifecycle-marker.json"
    expected_paths = [host_runs_root / source_name]
    expected_paths.extend(
        host_runs_root / f"{source_name}.failed-attempt{attempt_index}"
        for attempt_index in range(1, FAILURE_LIFECYCLE_ATTEMPT_COUNT + 1)
    )
    if any(path.exists() or path.is_symlink() for path in expected_paths):
        raise JobRehearsalReceiptError("失败生命周期探针路径发生碰撞")

    # 每次 Job 启动都在新的 network namespace 中确认没有默认路由，再创建
    # 同一个固定证据根并以预期非零码退出。归档清空固定根后，下一次重试才能
    # 再次创建它；因此三次成功建目录本身就证明了重试与归档之间的先后关系。
    failure_script = r'''
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
source_name=sys.argv[2]
marker_name=sys.argv[3]
lines=pathlib.Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]
if any(len(line.split())>1 and line.split()[1]=='00000000' for line in lines):
    raise SystemExit('default-route-present')
attempt=1
while (root.parent/f'{source_name}.failed-attempt{attempt}').exists():
    attempt+=1
if attempt>3:
    raise SystemExit('unexpected-attempt')
if root.exists() or root.is_symlink():
    raise SystemExit('fixed-source-not-cleared')
os.mkdir(root,0o700)
payload={
    'schema_version':'codex-upgrade-failure-lifecycle-marker/v1',
    'source_name':source_name,
    'attempt_index':attempt,
    'network_isolated':True,
    'live_request_count':0,
}
raw=(json.dumps(payload,sort_keys=True,separators=(',',':'))+'\n').encode()
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
descriptor=os.open(root/marker_name,flags,0o600)
with os.fdopen(descriptor,'wb') as stream:
    stream.write(raw)
    stream.flush()
    os.fsync(stream.fileno())
raise SystemExit(23)
'''
    verify_script = r'''
import hashlib,json,pathlib,sys
aliases=[pathlib.Path(value) for value in json.loads(sys.argv[1])]
source_name=sys.argv[2]
marker_name=sys.argv[3]
entries=[]
for attempt in range(1,4):
    paths=[alias/'runs'/f'{source_name}.failed-attempt{attempt}' for alias in aliases]
    if any(not path.is_dir() or path.is_symlink() for path in paths):
        raise SystemExit('archive-missing')
    metadata=[path.stat() for path in paths]
    if len({(item.st_dev,item.st_ino) for item in metadata})!=1:
        raise SystemExit('archive-alias-mismatch')
    raw_values=[(path/marker_name).read_bytes() for path in paths]
    if len(set(raw_values))!=1:
        raise SystemExit('marker-alias-mismatch')
    marker=json.loads(raw_values[0])
    if marker.get('attempt_index')!=attempt or marker.get('network_isolated') is not True or marker.get('live_request_count')!=0:
        raise SystemExit('marker-invalid')
    entries.append({
        'attempt_index':attempt,
        'container_archives':[str(path) for path in paths],
        'device':metadata[0].st_dev,
        'inode':metadata[0].st_ino,
        'marker_sha256':hashlib.sha256(raw_values[0]).hexdigest(),
        'marker':marker,
    })
print(json.dumps({'status':'passed','entries':entries},sort_keys=True))
'''
    cleanup_script = r'''
import json,pathlib,sys
paths=[pathlib.Path(value) for value in json.loads(sys.argv[1])]
marker_name=sys.argv[2]
for path in paths:
    if path.is_symlink() or not path.is_dir():
        raise SystemExit('cleanup-target-invalid')
    children=list(path.iterdir())
    if len(children)!=1 or children[0].name!=marker_name or children[0].is_symlink() or not children[0].is_file():
        raise SystemExit('cleanup-target-not-bounded')
for path in paths:
    (path/marker_name).unlink()
    path.rmdir()
if any(path.exists() or path.is_symlink() for path in paths):
    raise SystemExit('cleanup-incomplete')
'''

    codex_upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT = host_runs_root
    codex_upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS = tuple(
        alias / "runs" for alias in aliases
    )
    if codex_upgrade.JOB_RETRY_LIMIT != 2:
        raise JobRehearsalReceiptError("生产 Job 重试上限不再是两次")
    # 只在这个独立、零网络 worker 进程中消除 30 秒退避；生产常量和调用方
    # 进程均不改变，演练仍完整执行首次加两次重试。
    codex_upgrade.JOB_RETRY_DELAY_SECONDS = 0
    job = codex_upgrade.Job(
        job_id=FAILURE_LIFECYCLE_JOB_ID,
        phase="official",
        suites=("full",),
        description="VC-1 失败、重试和证据归档离线演练",
        steps=(
            {
                "argv": [
                    "docker",
                    "exec",
                    container,
                    "bwrap",
                    "--unshare-pid",
                    "--unshare-net",
                    "--die-with-parent",
                    "--ro-bind",
                    "/",
                    "/",
                    "--bind",
                    str(capture_root / "runs"),
                    str(capture_root / "runs"),
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--",
                    "/usr/bin/python3",
                    "-c",
                    failure_script,
                    str(logical_source),
                    source_name,
                    marker_name,
                ],
                "environment": {},
                "timeout": 30,
            },
        ),
        evidence_roots=(str(logical_source),),
        covers=(),
    )
    deadline = incremental_recovery.WallClockDeadline(
        FAILURE_LIFECYCLE_DEADLINE_SECONDS,
        label="failure-lifecycle-worker",
    )
    worker_result: dict[str, Any] | None = None
    try:
        with codex_upgrade.CampaignLease(
            campaign_dir,
            phase="official",
            candidate_id=None,
            deadline=deadline,
            attempt_id="failure-lifecycle-attempt",
            command="failure-lifecycle-worker",
            campaign_id=campaign_id,
        ) as lease:
            result = codex_upgrade._run_job_with_retry(
                job,
                log_root,
                deadline=deadline,
                heartbeat=lambda operation: lease.heartbeat(operation),
            )
            expected_final_root = str(
                capture_root
                / "runs"
                / f"{source_name}.failed-attempt{FAILURE_LIFECYCLE_ATTEMPT_COUNT}"
            )
            if (
                result.get("status") != "failed"
                or result.get("attempt_index") != FAILURE_LIFECYCLE_ATTEMPT_COUNT
                or result.get("evidence_roots") != [expected_final_root]
            ):
                raise JobRehearsalReceiptError("失败生命周期 Job 未完成三次失败归档")

            archives: list[dict[str, Any]] = []
            for attempt_index in range(1, FAILURE_LIFECYCLE_ATTEMPT_COUNT + 1):
                archive_name = f"{source_name}.failed-attempt{attempt_index}"
                host_archive = host_runs_root / archive_name
                marker_path = host_archive / marker_name
                if (
                    host_archive.is_symlink()
                    or not host_archive.is_dir()
                    or marker_path.is_symlink()
                    or not marker_path.is_file()
                ):
                    raise JobRehearsalReceiptError("失败生命周期宿主归档缺失")
                marker_raw = marker_path.read_bytes()
                try:
                    marker = json.loads(marker_raw)
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise JobRehearsalReceiptError("失败生命周期 marker 非法") from error
                metadata = host_archive.stat()
                archives.append(
                    {
                        "attempt_index": attempt_index,
                        "archive_name": archive_name,
                        "host_archive": str(host_archive),
                        "container_archives": [
                            str(alias / "runs" / archive_name) for alias in aliases
                        ],
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "marker_sha256": _sha256_bytes(marker_raw),
                        "marker": marker,
                    }
                )

            verified = lease.run_command(
                [
                    "docker",
                    "exec",
                    container,
                    "python3",
                    "-c",
                    verify_script,
                    json.dumps([str(alias) for alias in aliases]),
                    source_name,
                    marker_name,
                ],
                operation="failure-lifecycle:verify-aliases",
                timeout_seconds=30,
                job_id=FAILURE_LIFECYCLE_JOB_ID,
            )
            try:
                container_facts = json.loads(verified.stdout)
            except (TypeError, UnicodeError, json.JSONDecodeError) as error:
                raise JobRehearsalReceiptError(
                    "失败生命周期容器验证输出非法"
                ) from error
            expected_container_entries = [
                {
                    "attempt_index": item["attempt_index"],
                    "container_archives": item["container_archives"],
                    "device": item["device"],
                    "inode": item["inode"],
                    "marker_sha256": item["marker_sha256"],
                    "marker": item["marker"],
                }
                for item in archives
            ]
            if container_facts != {
                "status": "passed",
                "entries": expected_container_entries,
            }:
                raise JobRehearsalReceiptError("失败生命周期宿主与容器事实不同源")

            cleanup = lease.run_command(
                [
                    sys.executable,
                    "-c",
                    cleanup_script,
                    json.dumps([item["host_archive"] for item in archives]),
                    marker_name,
                ],
                operation="failure-lifecycle:cleanup",
                timeout_seconds=30,
                job_id=FAILURE_LIFECYCLE_JOB_ID,
            )
            if cleanup.returncode != 0 or any(
                path.exists() or path.is_symlink() for path in expected_paths
            ):
                raise JobRehearsalReceiptError("失败生命周期受监督清理未闭合")
            worker_result = {
                "schema_version": FAILURE_LIFECYCLE_WORKER_SCHEMA,
                "status": "passed",
                "campaign_id": campaign_id,
                "job_id": FAILURE_LIFECYCLE_JOB_ID,
                "source_name": source_name,
                "capture_container": container,
                "capture_root": str(capture_root),
                "host_runs_root": str(host_runs_root),
                "retry_limit": codex_upgrade.JOB_RETRY_LIMIT,
                "attempt_count": FAILURE_LIFECYCLE_ATTEMPT_COUNT,
                "network_isolated": True,
                "live_request_count": 0,
                "archives": archives,
                "final_result": {
                    "status": result["status"],
                    "attempt_index": result["attempt_index"],
                    "evidence_roots": result["evidence_roots"],
                },
                "cleanup_verified": True,
            }
            _write_once(output, worker_result)
    finally:
        # 正常路径已经通过父监督器完成清理；这里只是异常兜底，仍仅触碰
        # 本轮随机名称对应的四个精确目录。
        _cleanup_failure_lifecycle_artifacts(host_runs_root, source_name)
    if worker_result is None:
        raise JobRehearsalReceiptError("失败生命周期 worker 未生成结果")
    return worker_result


def _capture_failure_lifecycle_probe(
    configuration: Mapping[str, Any],
    host_runs_root: Path,
) -> dict[str, Any]:
    """用真实 campaign-run v2 和父租约演练完整 VC-1 失败生命周期。"""

    from tools.official_client_capture import codex_upgrade_supervisor

    container = _safe_id(configuration.get("capture_container"), "capture_container")
    capture_root = _absolute_posix_path(configuration.get("capture_root"), "capture_root")
    if capture_root != EXPECTED_CAPTURE_CONTAINER_ROOT:
        raise JobRehearsalReceiptError("失败生命周期探针容器根漂移")
    runtime_root = host_runs_root.parent / "runtime"
    if (
        host_runs_root.is_symlink()
        or not host_runs_root.is_dir()
        or runtime_root.is_symlink()
        or not runtime_root.is_dir()
    ):
        raise JobRehearsalReceiptError("失败生命周期探针宿主运行根不可信")

    nonce = secrets.token_hex(12)
    source_name = f"codex-failure-lifecycle-{nonce}"
    campaign_id = f"p0-failure-lifecycle-{nonce}"
    fixture = Path(
        tempfile.mkdtemp(prefix=f".{source_name}-", dir=runtime_root)
    )
    fixture.chmod(0o700)
    campaign_dir = fixture / "campaign"
    state_dir = fixture / "supervisor"
    log_root = fixture / "logs"
    worker_output = fixture / "worker-result.json"
    manifest_path = fixture / "campaign-run.json"
    predecessor_path = fixture / "vc0-checkpoint.json"
    for directory in (campaign_dir, state_dir, log_root):
        directory.mkdir(mode=0o700)
    expected_paths = [host_runs_root / source_name]
    expected_paths.extend(
        host_runs_root / f"{source_name}.failed-attempt{attempt_index}"
        for attempt_index in range(1, FAILURE_LIFECYCLE_ATTEMPT_COUNT + 1)
    )
    try:
        _write_once(
            campaign_dir / "campaign.json",
            {"campaign_id": campaign_id, "campaign_mode": "formal"},
        )
        predecessor = {
            "schema_version": "codex-upgrade-failure-lifecycle-predecessor/v1",
            "campaign_id": campaign_id,
            "phase": "VC-0",
            "status": "completed",
            "live_request_count": 0,
        }
        _write_once(predecessor_path, predecessor)
        predecessor_sha256 = _sha256_file(predecessor_path)
        action = {
            "action_id": "failure-lifecycle",
            "operation": "VC-1:failure-lifecycle",
            "timeout_seconds": FAILURE_LIFECYCLE_DEADLINE_SECONDS,
            "command": [
                sys.executable,
                str(Path(__file__).resolve()),
                "failure-lifecycle-worker",
                "--campaign-dir",
                str(campaign_dir),
                "--campaign-id",
                campaign_id,
                "--capture-container",
                container,
                "--capture-root",
                str(capture_root),
                "--host-runs-root",
                str(host_runs_root),
                "--source-name",
                source_name,
                "--log-root",
                str(log_root),
                "--output",
                str(worker_output),
            ],
            "item_ids": [FAILURE_LIFECYCLE_JOB_ID],
        }
        original_deadline = datetime.now(timezone.utc) + timedelta(
            seconds=FAILURE_LIFECYCLE_DEADLINE_SECONDS + 30
        )
        manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=campaign_id,
            campaign_plan_sha256=_fingerprint(
                {"campaign_id": campaign_id, "probe": FAILURE_LIFECYCLE_SCHEMA}
            ),
            batch_id="failure-lifecycle-batch",
            batch_sequence=1,
            batch_sha256=_fingerprint(action),
            phase="official",
            predecessor_checkpoint={
                "path": str(predecessor_path),
                "sha256": predecessor_sha256,
                "phase": "VC-0",
                "checkpoint_sha256": _fingerprint(predecessor),
            },
            original_deadline_at_utc=original_deadline.isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            actions=[action],
            execute_items=[FAILURE_LIFECYCLE_JOB_ID],
            reuse_items=[],
        )
        _write_once(manifest_path, manifest)
        raw_campaign_result = _run(
            [
                sys.executable,
                str(Path(codex_upgrade_supervisor.__file__).resolve()),
                "campaign-run",
                "--state-dir",
                str(state_dir),
                "--manifest",
                str(manifest_path),
                "--heartbeat-seconds",
                "0.2",
                "--watchdog-timeout-seconds",
                "5",
                "--ledger-interval-seconds",
                "0.2",
            ],
            "VC-1 失败生命周期 campaign-run v2 演练",
            timeout=FAILURE_LIFECYCLE_DEADLINE_SECONDS + 30,
        )
        try:
            campaign_result = json.loads(raw_campaign_result)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise JobRehearsalReceiptError(
                "失败生命周期 campaign-run 输出非法"
            ) from error
        worker, _worker_raw = _load_json(worker_output, "失败生命周期 worker 结果")
        run_dir = Path(str(campaign_result.get("run_dir", "")))
        try:
            run_dir.resolve(strict=True).relative_to(state_dir.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise JobRehearsalReceiptError("失败生命周期父 run_dir 越界") from error
        audit = codex_upgrade_supervisor._audit_command(run_dir)
        events_path = run_dir / "events.ndjson"
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_pairs = {
            (str(item.get("event_type")), str(item.get("operation")))
            for item in events
            if isinstance(item, Mapping)
        }
        required_pairs = {
            (event_type, operation)
            for attempt_index in range(1, FAILURE_LIFECYCLE_ATTEMPT_COUNT + 1)
            for event_type, operation in (
                (
                    "action-started",
                    f"job:{FAILURE_LIFECYCLE_JOB_ID}:attempt-{attempt_index}",
                ),
                (
                    "action-failed",
                    f"job:{FAILURE_LIFECYCLE_JOB_ID}:attempt-{attempt_index}",
                ),
                (
                    "action-started",
                    f"job:{FAILURE_LIFECYCLE_JOB_ID}:step-1:attempt-{attempt_index}",
                ),
                (
                    "action-failed",
                    f"job:{FAILURE_LIFECYCLE_JOB_ID}:step-1:attempt-{attempt_index}",
                ),
            )
        }
        required_pairs.update(
            {
                ("action-started", "failure-lifecycle:verify-aliases"),
                ("action-finished", "failure-lifecycle:verify-aliases"),
                ("action-started", "failure-lifecycle:cleanup"),
                ("action-finished", "failure-lifecycle:cleanup"),
            }
        )
        actions = campaign_result.get("actions")
        if (
            campaign_result.get("campaign_id") != campaign_id
            or campaign_result.get("status") != "stopped"
            or campaign_result.get("reason") != "queue-complete"
            or campaign_result.get("execute_items") != [FAILURE_LIFECYCLE_JOB_ID]
            or campaign_result.get("reuse_items") != []
            or not isinstance(actions, list)
            or actions
            != [
                {
                    "action_id": "failure-lifecycle",
                    "returncode": 0,
                    "status": "passed",
                }
            ]
            or audit.get("state") != "stopped"
            or audit.get("audit_incomplete") is not False
            or not required_pairs.issubset(event_pairs)
        ):
            raise JobRehearsalReceiptError("失败生命周期父监督器未形成完整成功终态")
        probe = {
            "schema_version": FAILURE_LIFECYCLE_SCHEMA,
            "status": "passed",
            "campaign_run_schema_version": manifest["schema_version"],
            "campaign_id": campaign_id,
            "job_id": worker.get("job_id"),
            "source_name": worker.get("source_name"),
            "capture_container": worker.get("capture_container"),
            "capture_root": worker.get("capture_root"),
            "host_runs_root": worker.get("host_runs_root"),
            "retry_limit": worker.get("retry_limit"),
            "attempt_count": worker.get("attempt_count"),
            "network_isolated": worker.get("network_isolated"),
            "live_request_count": worker.get("live_request_count"),
            "archives": worker.get("archives"),
            "final_result": worker.get("final_result"),
            "parent_supervisor": {
                "run_state": audit.get("state"),
                "audit_incomplete": audit.get("audit_incomplete"),
                "event_count": audit.get("event_count"),
                "required_event_count": len(required_pairs),
                "actions": actions,
            },
            "cleanup_verified": worker.get("cleanup_verified") is True
            and not any(path.exists() or path.is_symlink() for path in expected_paths),
        }
        return _validate_failure_lifecycle_probe(
            probe,
            {
                "configuration": dict(configuration),
            },
        )
    finally:
        _cleanup_failure_lifecycle_artifacts(host_runs_root, source_name)
        # fixture 是 runtime 根下由 mkdtemp 原子创建的本轮唯一目录；先拒绝
        # 顶层符号链接，再只删除该精确目录，不扫描其他运行资产。
        if fixture.is_symlink():
            fixture.unlink()
        elif fixture.is_dir():
            shutil.rmtree(fixture)


def _capture_storage_probe(
    jobs: Iterable[Any],
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """验证全部 Job 的运行根，以及宿主／容器别名的同源可写性。"""

    container = _safe_id(configuration.get("capture_container"), "capture_container")
    capture_root = _absolute_posix_path(
        configuration.get("capture_root"), "capture_root"
    )
    if capture_root != EXPECTED_CAPTURE_CONTAINER_ROOT:
        raise JobRehearsalReceiptError(
            "capture_root 未绑定文档登记的容器运行根"
        )
    aliases = sorted({CAPTURE_CONTAINER_ALIAS, capture_root}, key=str)

    raw = _run(
        ["docker", "inspect", container],
        "capture-cli 运行目录挂载探针",
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobRehearsalReceiptError("capture-cli 挂载探针输出非法") from error
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], Mapping)
        or not isinstance(payload[0].get("Mounts"), list)
    ):
        raise JobRehearsalReceiptError("capture-cli 挂载探针结果不完整")

    mounts_by_destination: dict[str, dict[str, Any]] = {}
    for index, raw_mount in enumerate(payload[0]["Mounts"], 1):
        if not isinstance(raw_mount, Mapping):
            raise JobRehearsalReceiptError(
                f"capture-cli 第 {index} 个挂载不是对象"
            )
        destination = _absolute_posix_path(
            raw_mount.get("Destination"),
            f"capture-cli 第 {index} 个挂载目标",
        )
        source = _absolute_posix_path(
            raw_mount.get("Source"),
            f"capture-cli 第 {index} 个挂载来源",
        )
        if str(destination) in mounts_by_destination:
            raise JobRehearsalReceiptError(
                f"capture-cli 挂载目标重复：{destination}"
            )
        mounts_by_destination[str(destination)] = {
            "type": raw_mount.get("Type"),
            "source": str(source),
            "destination": str(destination),
            "read_only": raw_mount.get("RW") is False,
        }

    host_root = Path(str(EXPECTED_HOST_DATA_ROOT))
    if host_root.is_symlink() or not host_root.is_dir():
        raise JobRehearsalReceiptError("登记宿主数据根不存在或是符号链接")
    host_root_metadata = host_root.stat()
    if (
        stat.S_IMODE(host_root_metadata.st_mode) != 0o700
        or host_root_metadata.st_uid != os.geteuid()
    ):
        raise JobRehearsalReceiptError(
            "登记宿主数据根必须归当前执行用户所有且权限为 0700"
        )

    root_mounts: list[dict[str, Any]] = []
    for alias in aliases:
        mount = mounts_by_destination.get(str(alias))
        if (
            mount is None
            or mount.get("type") != "bind"
            or mount.get("source") != str(EXPECTED_HOST_DATA_ROOT)
            or mount.get("read_only") is not True
        ):
            raise JobRehearsalReceiptError(
                f"capture-cli 宽泛父根必须同源只读：{alias}"
            )
        root_mounts.append(dict(mount))

    namespace_mounts: dict[str, list[dict[str, Any]]] = {}
    namespace_sources: dict[str, Path] = {}
    for namespace in WRITABLE_CAPTURE_NAMESPACES:
        expected_source = EXPECTED_HOST_DATA_ROOT / namespace
        source_path = Path(str(expected_source))
        if source_path.is_symlink() or not source_path.is_dir():
            raise JobRehearsalReceiptError(
                f"登记宿主可写子树不存在或是符号链接：{expected_source}"
            )
        source_metadata = source_path.stat()
        if (
            source_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(source_metadata.st_mode) & 0o022
        ):
            raise JobRehearsalReceiptError(
                f"登记宿主可写子树所有权或权限过宽：{expected_source}"
            )
        current_mounts: list[dict[str, Any]] = []
        for alias in aliases:
            destination = alias / namespace
            mount = mounts_by_destination.get(str(destination))
            if (
                mount is None
                or mount.get("type") != "bind"
                or mount.get("source") != str(expected_source)
                or mount.get("read_only") is not False
            ):
                raise JobRehearsalReceiptError(
                    "capture-cli 缺少同源可写运行挂载："
                    f"source={expected_source} destination={destination}"
                )
            current_mounts.append(dict(mount))
        namespace_mounts[namespace] = current_mounts
        namespace_sources[namespace] = source_path

    job_roots: list[dict[str, Any]] = []
    evidence_root_count = 0
    allowed_run_roots = [alias / "runs" for alias in aliases]
    for job in sorted(jobs, key=lambda item: str(item.job_id)):
        roots = list(job.evidence_roots)
        if not roots:
            raise JobRehearsalReceiptError(
                f"{job.job_id} 没有登记 evidence_roots"
            )
        normalized_roots: list[str] = []
        for index, value in enumerate(roots, 1):
            current = _absolute_posix_path(
                value, f"{job.job_id} evidence_root {index}"
            )
            if not any(
                current != allowed and current.is_relative_to(allowed)
                for allowed in allowed_run_roots
            ):
                raise JobRehearsalReceiptError(
                    f"{job.job_id} evidence_root 未落在登记 runs 子树：{current}"
                )
            normalized_roots.append(str(current))
        job_roots.append(
            {
                "job_id": str(job.job_id),
                "evidence_roots": sorted(normalized_roots),
            }
        )
        evidence_root_count += len(normalized_roots)

    # 该探针不运行任何 Job，也不联网。它只在两个登记子树内创建唯一临时
    # 目录，并从两条容器别名交叉写读，最后在同一进程的 finally 中清理。
    probe_script = r'''
import json,os,pathlib,stat,sys
aliases=json.loads(sys.argv[1])
namespaces=json.loads(sys.argv[2])
nonce=sys.argv[3]
output=[]
for namespace in namespaces:
    bases=[pathlib.Path(alias)/namespace for alias in aliases]
    for base in bases:
        if not base.is_dir() or base.is_symlink():
            raise SystemExit("invalid-base:"+str(base))
    base_stats=[base.stat() for base in bases]
    if len({(item.st_dev,item.st_ino) for item in base_stats}) != 1:
        raise SystemExit("alias-source-mismatch:"+namespace)
    probe_name=".codex-job-rehearsal-"+nonce+"-"+namespace
    visible=[base/probe_name for base in bases]
    marker_names=["marker-"+str(index) for index in range(len(bases))]
    created=False
    try:
        os.mkdir(visible[0],0o700)
        created=True
        visible_stats=[path.stat() for path in visible]
        if len({(item.st_dev,item.st_ino) for item in visible_stats}) != 1:
            raise SystemExit("probe-alias-mismatch:"+namespace)
        for index,parent in enumerate(visible):
            payload=(namespace+":"+str(index)).encode("ascii")
            flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
            descriptor=os.open(parent/marker_names[index],flags,0o600)
            with os.fdopen(descriptor,"wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            for peer in visible:
                marker=peer/marker_names[index]
                if marker.read_bytes()!=payload or stat.S_IMODE(marker.stat().st_mode)!=0o600:
                    raise SystemExit("cross-alias-read-failed:"+namespace)
        output.append({
            "name":namespace,
            "destinations":[{
                "path":str(base),
                "device":metadata.st_dev,
                "inode":metadata.st_ino,
                "mode":stat.S_IMODE(metadata.st_mode),
                "uid":metadata.st_uid,
                "gid":metadata.st_gid,
            } for base,metadata in zip(bases,base_stats)],
            "created_via":aliases,
            "cleanup_verified":True,
        })
    finally:
        if created:
            for marker_name in marker_names:
                (visible[0]/marker_name).unlink(missing_ok=True)
            visible[0].rmdir()
        if any(path.exists() or path.is_symlink() for path in visible):
            raise SystemExit("cleanup-failed:"+namespace)
print(json.dumps({"status":"passed","namespaces":output},sort_keys=True))
'''
    token = secrets.token_hex(12)
    write_raw = _run(
        [
            "docker",
            "exec",
            container,
            "python3",
            "-c",
            probe_script,
            json.dumps([str(alias) for alias in aliases]),
            json.dumps(list(WRITABLE_CAPTURE_NAMESPACES)),
            token,
        ],
        "capture-cli 可写运行目录创建清理探针",
        timeout=30,
    )
    try:
        write_probe = json.loads(write_raw)
    except json.JSONDecodeError as error:
        raise JobRehearsalReceiptError(
            "capture-cli 可写运行目录探针输出非法"
        ) from error
    if (
        not isinstance(write_probe, Mapping)
        or write_probe.get("status") != "passed"
        or not isinstance(write_probe.get("namespaces"), list)
    ):
        raise JobRehearsalReceiptError(
            "capture-cli 可写运行目录探针未通过"
        )
    probed_by_name = {
        str(item.get("name")): item
        for item in write_probe["namespaces"]
        if isinstance(item, Mapping)
    }
    if set(probed_by_name) != set(WRITABLE_CAPTURE_NAMESPACES):
        raise JobRehearsalReceiptError(
            "capture-cli 可写运行目录探针未覆盖全部登记子树"
        )

    writable_namespaces: list[dict[str, Any]] = []
    for namespace in WRITABLE_CAPTURE_NAMESPACES:
        source_path = namespace_sources[namespace]
        source_metadata = source_path.stat()
        probed = probed_by_name[namespace]
        destinations = probed.get("destinations")
        expected_destinations = [str(alias / namespace) for alias in aliases]
        if (
            not isinstance(destinations, list)
            or [item.get("path") for item in destinations if isinstance(item, Mapping)]
            != expected_destinations
            or probed.get("created_via") != [str(alias) for alias in aliases]
            or probed.get("cleanup_verified") is not True
        ):
            raise JobRehearsalReceiptError(
                f"capture-cli {namespace} 创建清理探针事实不完整"
            )
        normalized_destinations: list[dict[str, Any]] = []
        for destination in destinations:
            if not isinstance(destination, Mapping):
                raise JobRehearsalReceiptError(
                    f"capture-cli {namespace} 目标统计非法"
                )
            normalized = {
                field: destination.get(field)
                for field in ("path", "device", "inode", "mode", "uid", "gid")
            }
            if (
                normalized["device"] != source_metadata.st_dev
                or normalized["inode"] != source_metadata.st_ino
                or normalized["mode"] != stat.S_IMODE(source_metadata.st_mode)
                or normalized["uid"] != source_metadata.st_uid
                or normalized["gid"] != source_metadata.st_gid
            ):
                raise JobRehearsalReceiptError(
                    f"capture-cli {namespace} 宿主／容器映射不同源"
                )
            normalized_destinations.append(normalized)
        writable_namespaces.append(
            {
                "name": namespace,
                "source": str(source_path),
                "source_mode": stat.S_IMODE(source_metadata.st_mode),
                "source_uid": source_metadata.st_uid,
                "source_gid": source_metadata.st_gid,
                "source_device": source_metadata.st_dev,
                "source_inode": source_metadata.st_ino,
                "mounts": namespace_mounts[namespace],
                "destinations": normalized_destinations,
                "created_via": list(probed["created_via"]),
                "cleanup_verified": True,
            }
        )

    archive_route = _capture_archive_route_probe(
        container,
        aliases,
        namespace_sources["runs"],
    )

    return {
        "status": "passed",
        "capture_container": container,
        "capture_root": str(capture_root),
        "host_data_root": {
            "path": str(host_root),
            "mode": stat.S_IMODE(host_root_metadata.st_mode),
            "uid": host_root_metadata.st_uid,
            "gid": host_root_metadata.st_gid,
            "device": host_root_metadata.st_dev,
            "inode": host_root_metadata.st_ino,
        },
        "root_mounts": root_mounts,
        "writable_namespaces": writable_namespaces,
        "archive_route": archive_route,
        "job_count": len(job_roots),
        "evidence_root_count": evidence_root_count,
        "job_roots_sha256": _fingerprint(job_roots),
        "job_roots": job_roots,
    }


def capture_storage_probe(
    jobs: Iterable[Any],
    configuration: Mapping[str, Any],
) -> dict[str, Any]:
    """公开执行零网络的宿主／容器可写路径探针，供 VC-5 就绪门禁复用。"""

    return _capture_storage_probe(jobs, configuration)


def _bwrap_probe(container: str) -> dict[str, Any]:
    version = _run(
        ["docker", "exec", container, "bwrap", "--version"],
        "bubblewrap 版本探针",
    ).decode("utf-8", errors="strict").strip()
    script = (
        "import pathlib;"
        "lines=pathlib.Path('/proc/net/route').read_text().splitlines()[1:];"
        "assert not any(len(x.split())>1 and x.split()[1]=='00000000' for x in lines);"
        "print('bwrap-offline-ok')"
    )
    output = _run(
        [
            "docker",
            "exec",
            container,
            "timeout",
            "15",
            "bwrap",
            "--unshare-user",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--unshare-pid",
            "--unshare-net",
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--",
            "/usr/bin/python3",
            "-c",
            script,
        ],
        "bubblewrap 离线沙箱探针",
        timeout=30,
    ).decode("utf-8", errors="strict").strip()
    if output != "bwrap-offline-ok":
        raise JobRehearsalReceiptError("bubblewrap 离线沙箱结果不一致")
    return {
        "status": "passed",
        "version": version,
        "network_isolated": True,
    }


def _zstd_probe(container: str, tool_root: str) -> dict[str, Any]:
    script = (
        "import sys;"
        "sys.path.insert(0,sys.argv[1]);"
        "from relay_extract import decompress_zstd;"
        "raw=bytes.fromhex(sys.argv[2]);"
        "out=decompress_zstd(raw);"
        "assert out==b'hello';"
        "sys.stdout.buffer.write(out)"
    )
    output = _run(
        [
            "docker",
            "exec",
            container,
            "python3",
            "-c",
            script,
            tool_root,
            ZSTD_FRAME.hex(),
        ],
        "capture-cli zstd 离线解压探针",
    )
    if output != ZSTD_OUTPUT:
        raise JobRehearsalReceiptError("zstd 已知帧解压结果不一致")
    return {
        "status": "passed",
        "input_sha256": _sha256_bytes(ZSTD_FRAME),
        "output_sha256": _sha256_bytes(output),
        "output": output.decode("ascii"),
    }


def _syntax_probe(argv: list[str], container: str, label: str) -> tuple[str, list[dict[str, str]]]:
    checks: list[dict[str, str]] = []
    if argv[0] == "bash":
        if len(argv) < 2:
            raise JobRehearsalReceiptError(f"{label} 缺少 bash 参数")
        if argv[1] == "-c":
            if len(argv) != 3:
                raise JobRehearsalReceiptError(f"{label} bash -c 参数不闭合")
            _run(["bash", "-n", "-c", argv[2]], f"{label} 宿主内联脚本语法")
            checks.append({"kind": "host_inline_bash", "sha256": _sha256_bytes(argv[2].encode())})
            return "host", checks
        script = Path(argv[1])
        if not script.is_absolute() or not script.is_file() or script.is_symlink():
            raise JobRehearsalReceiptError(f"{label} 宿主脚本路径不可信：{script}")
        _run(["bash", "-n", str(script)], f"{label} 宿主脚本语法")
        checks.append({"kind": "host_script", "path": str(script), "sha256": _sha256_file(script)})
        return "host", checks
    if argv[0] != "docker" or len(argv) < 5 or argv[1] != "exec":
        raise JobRehearsalReceiptError(f"{label} 启动器未登记：{argv[:3]}")
    if argv[2] != container:
        raise JobRehearsalReceiptError(f"{label} docker exec 未绑定 capture-cli")
    command = argv[3]
    if command == "python3":
        script = argv[4]
        raw = _run(
            [
                "docker",
                "exec",
                container,
                "python3",
                "-c",
                "import hashlib,json,pathlib,sys;p=pathlib.Path(sys.argv[1]);"
                "assert p.is_file() and not p.is_symlink();"
                "print(json.dumps({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))",
                script,
            ],
            f"{label} 容器 Python 路径",
        )
        payload = json.loads(raw)
        checks.append({"kind": "container_python_script", **payload})
        return "capture_container", checks
    if command == "bash" and argv[4] == "-c" and len(argv) == 6:
        _run(
            ["docker", "exec", container, "bash", "-n", "-c", argv[5]],
            f"{label} 容器内联脚本语法",
        )
        checks.append({"kind": "container_inline_bash", "sha256": _sha256_bytes(argv[5].encode())})
        return "capture_container", checks
    raise JobRehearsalReceiptError(f"{label} 容器启动器未登记：{argv[3:5]}")


def _job_document(job: Any) -> dict[str, Any]:
    return {
        "id": str(job.job_id),
        "phase": str(job.phase),
        "suites": list(job.suites),
        "description": str(job.description),
        "steps": [dict(step) for step in job.steps],
        "evidence_roots": list(job.evidence_roots),
        "covers": list(job.covers),
        "scenario_ids": list(job.scenario_ids),
        "required": bool(job.required),
        "required_scenario_receipts": list(job.required_scenario_receipts),
        "track": str(job.track),
        "model_id": str(job.model_id),
        "expected_use_responses_lite": bool(job.expected_use_responses_lite),
        "required_model_receipt": bool(job.required_model_receipt),
    }


def _relocate_rehearsal_campaign_coordinate(
    document: Mapping[str, Any],
    current_campaign_id: str,
    previous_campaign_id: str,
) -> dict[str, Any]:
    """把当前 Job 的受管 Campaign 坐标投影回前序坐标。

    只迁移会由 ``plan`` 注入 Campaign ID 的三个执行字段。调用方仍须把
    迁移后的完整 Job 摘要与前序收据逐字比对，不能用本函数放宽其他差异。
    """

    current = _safe_id(current_campaign_id, "当前 preflight campaign_id")
    previous = _safe_id(previous_campaign_id, "前序 preflight campaign_id")

    def relocate(value: Any) -> Any:
        return value.replace(current, previous) if isinstance(value, str) else value

    relocated = dict(document)
    roots = document.get("evidence_roots")
    if isinstance(roots, list):
        relocated["evidence_roots"] = [relocate(value) for value in roots]
    steps: list[dict[str, Any]] = []
    for raw_step in document.get("steps", []):
        if not isinstance(raw_step, Mapping):
            steps.append(dict(raw_step) if isinstance(raw_step, dict) else {})
            continue
        step = dict(raw_step)
        argv = raw_step.get("argv")
        if isinstance(argv, list):
            step["argv"] = [relocate(value) for value in argv]
        environment = raw_step.get("environment")
        if isinstance(environment, Mapping):
            step["environment"] = {
                str(key): relocate(value) for key, value in environment.items()
            }
        steps.append(step)
    relocated["steps"] = steps
    return relocated


def _job_probe(
    job: Any,
    container: str,
    *,
    component_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    document = _job_document(job)
    incremental = (
        _job_incremental_metadata(document, component_summary)
        if component_summary is not None
        else None
    )
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(document["steps"], 1):
        argv = step.get("argv")
        environment = step.get("environment")
        timeout = step.get("timeout")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(value, str) and value for value in argv)
            or not isinstance(environment, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in environment.items()
            )
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise JobRehearsalReceiptError(
                f"{job.job_id} 第 {index} 步命令、环境或超时非法"
            )
        launcher, path_checks = _syntax_probe(
            argv, container, f"{job.job_id}:step-{index}"
        )
        steps.append(
            {
                "index": index,
                "status": "passed",
                "launcher": launcher,
                "argv_sha256": _fingerprint(argv),
                "environment_sha256": _fingerprint(environment),
                "timeout_seconds": timeout,
                "path_checks": path_checks,
                "dependencies": (
                    [f"host:{name}" for name in HOST_REQUIRED_COMMANDS]
                    if launcher == "host"
                    else ["host:docker"]
                    + [
                        f"capture_container:{name}"
                        for name in CAPTURE_CONTAINER_REQUIRED_COMMANDS
                    ]
                ),
            }
        )
    c2pa = None
    if job.job_id in {
        "official-relay-file-upload-c2pa-negative",
        "official-relay-file-upload-c2pa-positive",
    }:
        environment = document["steps"][0]["environment"]
        expected = "negative" if job.job_id.endswith("negative") else "positive"
        if (
            environment.get("SCENARIO_JOB_ID") != job.job_id
            or environment.get("A14_C2PA_EXPECTATION") != expected
        ):
            raise JobRehearsalReceiptError(f"{job.job_id} C2PA 身份漂移")
        c2pa = {
            "scenario_job_id": environment["SCENARIO_JOB_ID"],
            "expectation": environment["A14_C2PA_EXPECTATION"],
        }
    if job.required_model_receipt:
        environment = document["steps"][0]["environment"]
        if (
            environment.get("SCENARIO_JOB_ID") != job.job_id
            or environment.get("REQUIRE_MODEL_CONDITION_RECEIPT") != "1"
            or environment.get("MODEL_TRACK") != job.track
            or environment.get("EXPECT_USE_RESPONSES_LITE")
            != ("true" if job.expected_use_responses_lite else "false")
        ):
            raise JobRehearsalReceiptError(f"{job.job_id} 模型条件收据身份漂移")
    result = {
        "id": job.job_id,
        "phase": job.phase,
        "status": "passed",
        "job_contract_sha256": _fingerprint(document),
        "step_count": len(steps),
        "steps": steps,
        "c2pa_identity": c2pa,
    }
    if incremental is not None:
        result.update(
            {
                "tool_components": incremental["components"],
                "tool_component_digests": incremental["component_digests"],
                "input_sha256": incremental["input_sha256"],
                "environment_sha256": incremental["environment_sha256"],
                "dependency_sha256": incremental["dependency_sha256"],
                "incremental_result_key": incremental["result_key"],
                "disposition": "executed",
            }
        )
    return result


def _failed_job_probe(
    job: Any,
    component_summary: Mapping[str, Any],
    error: BaseException,
    *,
    duration_seconds: float = 0.0,
) -> dict[str, Any]:
    """把单个 Job 的探针失败封存成可续作事实，而不是整轮丢弃。"""

    document = _job_document(job)
    incremental = _job_incremental_metadata(document, component_summary)
    c2pa_identity = None
    if str(job.job_id) in {
        "official-relay-file-upload-c2pa-negative",
        "official-relay-file-upload-c2pa-positive",
    }:
        first_environment = (
            document["steps"][0].get("environment")
            if document.get("steps")
            else None
        )
        if isinstance(first_environment, Mapping):
            c2pa_identity = {
                "scenario_job_id": str(first_environment.get("SCENARIO_JOB_ID", "")),
                "expectation": str(first_environment.get("A14_C2PA_EXPECTATION", "")),
            }
    return {
        "id": str(job.job_id),
        "phase": str(job.phase),
        "status": "failed",
        "job_contract_sha256": incremental["input_sha256"],
        "step_count": len(document["steps"]),
        "steps": [],
        "c2pa_identity": c2pa_identity,
        "error": str(error)[:1000] or type(error).__name__,
        "duration_seconds": max(0.0, float(duration_seconds)),
        "tool_components": incremental["components"],
        "tool_component_digests": incremental["component_digests"],
        "input_sha256": incremental["input_sha256"],
        "environment_sha256": incremental["environment_sha256"],
        "dependency_sha256": incremental["dependency_sha256"],
        "incremental_result_key": incremental["result_key"],
        "disposition": "executed",
    }


def _producer() -> dict[str, str]:
    tool = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(tool),
        "tool_sha256": _sha256_file(tool),
        "version": PRODUCER_VERSION,
    }


def _runtime_identity(facts: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            "host": facts["host"],
            "containers": facts["containers"],
            "tool_trees": facts["tool_trees"],
            "dependencies": facts["dependencies"],
            "binary_verification": facts["binary_verification"],
            "probes": facts["probes"],
            "jobs": facts["jobs"],
            "tool_components": facts.get("tool_components"),
        }
    )


def _load_previous_rehearsal(
    root: Path,
    receipt_relative: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取并校验前序演练；校验不要求当前采集器版本相同。

    旧收据是只读输入。只要自身摘要链和事实结构合法，即使本次修复改了
    evaluator／collector，也不能因此把历史结果判成损坏。
    """

    root = _private_root(root)
    receipt_path = _relative(root, receipt_relative, "前序 Job 收据")
    receipt, receipt_raw = _load_json(receipt_path, "前序 Job 收据")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise JobRehearsalReceiptError("前序 Job 收据版本不受支持")
    if receipt.get("status") == INCREMENTAL_NOOP_STATUS:
        raise JobRehearsalReceiptError(
            "incremental-noop 不是新的通过事实；前序输入必须直接引用原有 passed 收据"
        )
    facts_reference = receipt.get("facts")
    if not isinstance(facts_reference, dict):
        raise JobRehearsalReceiptError("前序 Job 收据缺少 facts 引用")
    facts_path = _relative(root, str(facts_reference.get("path", "")), "前序 Job facts")
    facts, raw = _load_json(facts_path, "前序 Job facts")
    if (
        _sha256_bytes(raw) != facts_reference.get("sha256")
        or len(raw) != facts_reference.get("bytes")
    ):
        raise JobRehearsalReceiptError("前序 Job facts 摘要漂移")
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise JobRehearsalReceiptError("前序 Job facts 版本不受支持")
    if facts.get("status") == INCREMENTAL_NOOP_STATUS or facts.get(
        "incremental_noop"
    ) is not None:
        raise JobRehearsalReceiptError(
            "incremental-noop 不能作为前序演练；请使用其 source_receipt 指向的原有 passed 收据"
        )
    if not isinstance(facts.get("jobs"), list) or not isinstance(
        facts.get("execution_contract"), dict
    ):
        raise JobRehearsalReceiptError("前序 Job facts 结构不完整")
    source_binding = {
        "path": receipt_path.relative_to(root).as_posix(),
        "sha256": _sha256_bytes(receipt_raw),
        "bytes": len(receipt_raw),
    }
    # 这里允许 collector 漂移，但仍执行完整结构、摘要和安全结论校验。
    validated = validate_facts(facts, allow_collector_drift=True)
    facts_relative = facts_path.relative_to(root).as_posix()
    if facts_reference.get("path") != facts_relative:
        raise JobRehearsalReceiptError("前序 Job facts 路径未规范化")
    if (
        receipt.get("status") != validated["status"]
        or receipt.get("execution_contract_sha256")
        != validated["execution_contract_sha256"]
        or receipt.get("job_count") != validated["job_count"]
        or receipt.get("job_set_sha256") != validated["job_set_sha256"]
        or not isinstance(receipt.get("producer"), Mapping)
    ):
        raise JobRehearsalReceiptError("前序 Job 收据摘要与 facts 不一致")
    return facts, source_binding


def _load_previous_facts(root: Path, receipt_relative: str) -> dict[str, Any]:
    """兼容旧调用方的前序 facts 读取接口。"""

    facts, _ = _load_previous_rehearsal(root, receipt_relative)
    return facts


def _select_rehearsal_plan(
    jobs: Iterable[Any],
    contract: Mapping[str, Any],
    component_summary: Mapping[str, Any],
    previous_facts: Mapping[str, Any] | None,
    *,
    previous_global_tool_sha256: str | None = None,
    current_campaign_id: str | None = None,
) -> dict[str, Any]:
    """选择最小 Job 执行集合；失败项始终优先，成功项按结果键复用。"""

    ordered_jobs = sorted(jobs, key=lambda item: (str(item.phase), str(item.job_id)))
    ordered_job_ids = [str(item.job_id) for item in ordered_jobs]
    if len(set(ordered_job_ids)) != len(ordered_job_ids):
        raise JobRehearsalReceiptError("Job 集包含重复 ID")
    previous_contract = (
        previous_facts.get("execution_contract")
        if isinstance(previous_facts, Mapping)
        else None
    )
    contract_identity_same = (
        isinstance(previous_contract, Mapping)
        and _contract_reuse_identity(previous_contract)
        == _contract_reuse_identity(contract)
    )
    # 全局工具摘要仅用于历史兼容和审计，不参与新结果键的失效判定。
    previous_by_id = {
        str(item.get("id")): item
        for item in (previous_facts.get("jobs", []) if isinstance(previous_facts, Mapping) else [])
        if isinstance(item, Mapping)
    }
    if isinstance(previous_facts, Mapping) and isinstance(previous_facts.get("jobs"), list):
        if len(previous_by_id) != len(
            [item for item in previous_facts["jobs"] if isinstance(item, Mapping)]
        ):
            raise JobRehearsalReceiptError("前序 facts 含重复 Job ID")
    previous_component_summary = (
        _facts_component_summary(previous_facts)
        if isinstance(previous_facts, Mapping)
        else None
    )
    previous_campaign = (
        previous_facts.get("preflight_campaign")
        if isinstance(previous_facts, Mapping)
        else None
    )
    previous_campaign_id = (
        previous_campaign.get("campaign_id")
        if isinstance(previous_campaign, Mapping)
        else None
    )

    def _legacy_dependency_match(document: Mapping[str, Any]) -> bool:
        """为没有 result_key 的历史 facts 现场比较直接组件摘要。

        旧收据只保存了 Job 合同摘要，不能把整个工具树摘要作为唯一条件；
        只要该 Job 实际依赖的组件未变，评估器或其他无关组件的修复不应
        清空已通过结果。
        """

        if previous_component_summary is None:
            return False
        old_components = previous_component_summary.get("components")
        new_components = component_summary.get("components")
        if not isinstance(old_components, Mapping) or not isinstance(new_components, Mapping):
            return False
        dependencies = _job_dependencies(document)
        for name in dependencies:
            old = old_components.get(name)
            new = new_components.get(name)
            if (
                not isinstance(old, Mapping)
                or not isinstance(new, Mapping)
                or old.get("sha256") != new.get("sha256")
            ):
                return False
        return True

    execute: list[str] = []
    reused: list[str] = []
    reasons: dict[str, str] = {}
    for job in ordered_jobs:
        job_id = str(job.job_id)
        previous = previous_by_id.get(job_id)
        current_document = _job_document(job)
        current_meta = _job_incremental_metadata(current_document, component_summary)
        if not isinstance(previous, Mapping):
            execute.append(job_id)
            reasons[job_id] = "no_previous_result"
            continue
        if previous.get("status") in {"failed", "blocked"}:
            execute.append(job_id)
            reasons[job_id] = "previous_failure"
            continue
        previous_key = previous.get("incremental_result_key")
        key_matches = previous_key == current_meta["result_key"]
        # v1 历史 facts 没有组件键，现场比较该 Job 的直接组件；只有组件
        # 摘要和 Job 合同都相同才允许只读复用。
        legacy_matches = (
            previous_key is None
            and previous.get("job_contract_sha256") == current_meta["input_sha256"]
            and _legacy_dependency_match(current_document)
        )
        coordinate_matches = False
        prior_coordinate_document: dict[str, Any] | None = None
        if (
            isinstance(current_campaign_id, str)
            and isinstance(previous_campaign_id, str)
            and current_campaign_id != previous_campaign_id
        ):
            prior_coordinate_document = _relocate_rehearsal_campaign_coordinate(
                current_document,
                current_campaign_id,
                previous_campaign_id,
            )
            coordinate_matches = (
                previous.get("job_contract_sha256")
                == _fingerprint(prior_coordinate_document)
                and previous.get("dependency_sha256")
                == current_meta["dependency_sha256"]
                and previous.get("environment_sha256")
                == current_meta["environment_sha256"]
                and previous.get("tool_components")
                == current_meta["components"]
                and previous.get("tool_component_digests")
                == current_meta["component_digests"]
            )
        # 旧收据可能使用 relay/shared 粗分组并已写入 result_key。只要完整
        # Job 文档（或唯一 Campaign 坐标迁移）相等，且从历史工具条目按
        # 当前映射重算后的直接依赖未变，就可安全复用；控制面变化不会再
        # 扩大为全部 Job。
        remapped_input_matches = previous.get("job_contract_sha256") in {
            current_meta["input_sha256"],
            (
                _fingerprint(prior_coordinate_document)
                if prior_coordinate_document is not None
                else ""
            ),
        }
        remapped_matches = (
            remapped_input_matches
            and _legacy_dependency_match(current_document)
        )
        if (
            contract_identity_same
            and previous.get("status") == "passed"
            and (
                key_matches
                or legacy_matches
                or coordinate_matches
                or remapped_matches
            )
        ):
            reused.append(job_id)
            if coordinate_matches and not (key_matches or legacy_matches):
                reasons[job_id] = "campaign_coordinate_relocated"
            elif remapped_matches and not (key_matches or legacy_matches):
                reasons[job_id] = "historical_component_map_reclassified"
            else:
                reasons[job_id] = "unchanged_dependency"
        else:
            execute.append(job_id)
            reasons[job_id] = (
                "contract_changed" if not contract_identity_same else "dependency_changed"
            )
    changed_components = (
        incremental_recovery.component_drift(
            previous_component_summary,
            component_summary,
        ).get("changed_components", [])
        if previous_component_summary is not None
        else []
    )
    # 没有前序结果时，所有 Job 都是首次执行；不要把当前工具的全部组件
    # 伪装成“发生漂移”，否则审计会误报受影响闭集。
    failed_job_ids = [
        job_id
        for job_id in ordered_job_ids
        if job_id in {
            str(item.get("id"))
            for item in previous_by_id.values()
            if item.get("status") in {"failed", "blocked"}
        }
    ]
    changed_list = sorted(str(item) for item in changed_components)
    plan_core = {
        "contract_sha256": execution_contract_sha256(dict(contract)),
        "execute_job_ids": execute,
        "reused_job_ids": reused,
        "failed_job_ids": failed_job_ids,
        "changed_components": changed_list,
        "reasons": reasons,
    }
    return {
        "schema_version": incremental_recovery.SCHEMA_VERSION,
        "contract_sha256": plan_core["contract_sha256"],
        "execute_job_ids": execute,
        "reused_job_ids": reused,
        "failed_job_ids": failed_job_ids,
        "changed_components": changed_list,
        "reasons": reasons,
        "plan_sha256": incremental_recovery.digest(plan_core),
    }


def _build_incremental_noop_facts(
    *,
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    component_summary: Mapping[str, Any],
    previous_facts: Mapping[str, Any],
    previous_source: Mapping[str, Any],
    previous_receipt_root: Path,
) -> dict[str, Any]:
    """构造不触碰运行时探针的增量空操作事实。

    ``incremental-noop`` 只表示本轮没有需要执行的 Job。它不复制或重新声明
    宿主、容器、二进制、bubblewrap、zstd 等运行事实，也不生成新的通过结论；
    后续 Formal 必须继续绑定 ``source_receipt`` 指向的原有通过收据。
    """

    planned = sorted(str(item) for item in contract.get("job_ids", []))
    execute = sorted(str(item) for item in plan.get("execute_job_ids", []))
    reused = sorted(str(item) for item in plan.get("reused_job_ids", []))
    failed = sorted(str(item) for item in plan.get("failed_job_ids", []))
    changed = sorted(str(item) for item in plan.get("changed_components", []))
    if execute or failed or reused != planned:
        raise JobRehearsalReceiptError(
            "只有完整复用且执行集合为空时才能生成 incremental-noop"
        )
    if not isinstance(previous_source, Mapping):
        raise JobRehearsalReceiptError("incremental-noop 缺少原有通过收据绑定")
    source_binding = dict(previous_source)
    source_root = previous_receipt_root.resolve(strict=True)
    source_summary = previous_facts.get("summary")
    if not isinstance(source_summary, Mapping):
        raise JobRehearsalReceiptError("原有通过收据缺少 Job 汇总")
    source_job_set_sha256 = source_summary.get("job_set_sha256")
    if not SHA256_RE.fullmatch(str(source_job_set_sha256 or "")):
        raise JobRehearsalReceiptError("原有通过收据的 Job 集摘要非法")
    source_job_count = source_summary.get("job_count")
    if (
        not isinstance(source_job_count, int)
        or isinstance(source_job_count, bool)
        or source_job_count != len(planned)
    ):
        raise JobRehearsalReceiptError("原有通过收据的 Job 数量与当前合同不一致")
    incremental_plan = {
        **dict(plan),
        "affected_job_ids": [],
        "previous_receipt": source_binding,
    }
    noop = {
        "schema_version": INCREMENTAL_NOOP_SCHEMA,
        "new_pass_fact": False,
        "planned_job_ids": planned,
        "execute_job_ids": [],
        "reused_job_ids": reused,
        "affected_job_ids": [],
        "failed_job_ids": [],
        "changed_components": changed,
        "plan_sha256": str(plan.get("plan_sha256", "")),
        "source_receipt": source_binding,
        "source_root": str(source_root),
        "source_status": "passed",
        "source_job_count": source_job_count,
        "source_job_set_sha256": str(source_job_set_sha256),
        "scanned_bytes": 0,
        "live_request_count": 0,
        "recorded_at_utc": _utc_now(),
    }
    return {
        "schema_version": FACTS_SCHEMA,
        "status": INCREMENTAL_NOOP_STATUS,
        "observed_at_utc": _utc_now(),
        "preflight_campaign": {
            "path": str(campaign_dir),
            "campaign_id": manifest["campaign_id"],
            "manifest_sha256": _sha256_file(campaign_dir / "campaign.json"),
            "campaign_mode": "preflight_only",
        },
        "execution_contract": dict(contract),
        "execution_contract_sha256": execution_contract_sha256(dict(contract)),
        "incremental_plan": incremental_plan,
        "tool_components": dict(component_summary),
        "incremental_noop": noop,
        "collector": _producer(),
    }


def _collect_facts(
    campaign_dir: Path,
    *,
    previous_receipt: Path | None = None,
    previous_receipt_root: Path | None = None,
    rerun_failed: bool = False,
    checkpoint_root: Path | None = None,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """展开 preflight Job，并按前序收据只执行失败／受影响项。

    该函数只做离线路径、依赖和运行时探针，不发送官方请求。没有前序收据时
    执行全部 Job；有前序收据时，已通过且结果键未变的 Job 仅写入 ``reused``
    记录，失败项和组件依赖变化项才重新探测。
    """

    from tools.official_client_capture import codex_upgrade

    if deadline is not None:
        deadline.check("job-rehearsal:start")

    if rerun_failed and previous_receipt is None:
        raise JobRehearsalReceiptError(
            "rerun_failed 必须绑定前序演练收据"
        )
    if previous_receipt is not None and not rerun_failed:
        raise JobRehearsalReceiptError(
            "提供前序演练收据时必须显式启用 rerun_failed"
        )

    campaign_dir = campaign_dir.resolve(strict=True)
    manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
    if manifest.get("campaign_mode") != "preflight_only":
        raise JobRehearsalReceiptError("完整 Job 演练只能消费 preflight_only Campaign")
    machine = platform.machine().lower()
    if platform.system().lower() != "linux" or machine not in {"aarch64", "arm64"}:
        raise JobRehearsalReceiptError("完整 Job 演练只能在 ARM64 Linux 宿主执行")
    inputs = manifest["inputs"]
    scenario_reference = inputs.get("target_discovery_scenarios")
    if not isinstance(scenario_reference, dict):
        raise JobRehearsalReceiptError("preflight 缺少 target 场景清单")
    target_scenario = json.loads(
        (campaign_dir / scenario_reference["path"]).read_text(encoding="utf-8")
    )
    extra_reference = inputs.get("extra_jobs")
    extra_jobs = (
        json.loads((campaign_dir / extra_reference["path"]).read_text(encoding="utf-8"))
        if isinstance(extra_reference, dict)
        else None
    )
    configuration = manifest["configuration"]
    contract = build_execution_contract(
        target_version=manifest["target_version"],
        target_sha256=manifest["target_sha256"],
        target_package_sha256=manifest["official_identity"]["package"]["asset_sha256"],
        target_code_mode_host_sha256=manifest["official_identity"]["package"]["code_mode_host_sha256"],
        suite=manifest["suite"],
        tool_files_sha256=manifest["tool_identity"]["files_sha256"],
        configuration=configuration,
        target_scenario=target_scenario,
        extra_jobs=extra_jobs,
        wire_producer_sha256=manifest["tool_identity"].get("wire_producer_sha256"),
        policy_sha256=manifest["tool_identity"].get("policy_sha256"),
    )
    jobs = [
        *codex_upgrade._campaign_jobs(
            campaign_dir,
            manifest,
            "official",
            use_approved_scenario=False,
        ),
        *codex_upgrade._campaign_jobs(
            campaign_dir,
            manifest,
            "candidate",
            use_approved_scenario=False,
        ),
    ]
    if sorted(job.job_id for job in jobs) != contract["job_ids"]:
        raise JobRehearsalReceiptError("展开 Job 集与目标模板不一致")
    # 计划阶段只读取 manifest、场景、Job 合同和当前受管工具树。任何可能触碰
    # Docker、官方二进制、执行副本、环境或运行容器的探针都必须放在空集判断之后。
    managed_root = Path(codex_upgrade.__file__).resolve().parent
    managed_tree = _tool_tree_summary(managed_root)
    # 工具树摘要只由当前受管树生成，不能从前序收据复制，避免把旧工具身份误当成当前身份。
    component_summary = _component_summary(managed_tree)
    previous_facts: dict[str, Any] | None = None
    previous_source: dict[str, Any] | None = None
    previous_global_tool_sha256: str | None = None
    source_root: Path | None = None
    if previous_receipt is not None:
        source_root = previous_receipt_root or campaign_dir
        if previous_receipt.is_absolute():
            try:
                previous_relative = previous_receipt.resolve(strict=True).relative_to(
                    source_root.resolve(strict=True)
                ).as_posix()
            except (OSError, ValueError) as error:
                raise JobRehearsalReceiptError(
                    "前序收据必须位于指定 evidence root 内"
                ) from error
        else:
            previous_relative = previous_receipt.as_posix()
        previous_facts, previous_source = _load_previous_rehearsal(
            source_root,
            previous_relative,
        )
        previous_contract = previous_facts.get("execution_contract")
        if isinstance(previous_contract, Mapping):
            value = previous_contract.get("tool_files_sha256")
            if isinstance(value, str):
                previous_global_tool_sha256 = value
    plan = _select_rehearsal_plan(
        jobs,
        contract,
        component_summary,
        previous_facts,
        previous_global_tool_sha256=previous_global_tool_sha256,
        current_campaign_id=str(manifest["campaign_id"]),
    )
    if not plan["execute_job_ids"]:
        # 失败项为空时必须在所有昂贵探针之前结束。此处不创建 checkpoint，
        # 不校验执行树，不启动 Docker／容器／bubblewrap／zstd，也不发送请求。
        if (
            previous_facts is None
            or previous_source is None
            or source_root is None
        ):
            raise JobRehearsalReceiptError(
                "空执行计划缺少可引用的原有通过收据"
            )
        return _build_incremental_noop_facts(
            campaign_dir=campaign_dir,
            manifest=manifest,
            contract=contract,
            plan=plan,
            component_summary=component_summary,
            previous_facts=previous_facts,
            previous_source=previous_source,
            previous_receipt_root=source_root,
        )

    # 从这里开始确实存在需要探针的 Job，才允许校验实际执行树、容器和二进制。
    capture_root = Path(str(configuration["capture_root"]))
    execution_root = capture_root / "tools" / "official_client_capture"
    codex_upgrade._verify_execution_tree(capture_root)
    execution_tree = _tool_tree_summary(execution_root)
    container_tree = _container_tool_tree(
        str(configuration["capture_container"]), str(execution_root)
    )
    if not (
        managed_tree["entries"]
        == execution_tree["entries"]
        == container_tree["entries"]
        and _contract_tool_matches(contract, managed_tree["files_sha256"], codex_upgrade._tool_identity(include_git=False))
    ):
        execute = list(plan["execute_job_ids"])
        raise JobRehearsalReceiptError(
            "受管、执行和 capture-cli 工具树不一致；"
            f"execute_count={len(execute)}，execute_job_ids={execute[:10]}"
        )
    storage_probe = _capture_storage_probe(jobs, configuration)
    failure_lifecycle_probe = _capture_failure_lifecycle_probe(
        configuration,
        Path(EXPECTED_HOST_DATA_ROOT) / "runs",
    )
    binary_verification = codex_upgrade._verify_official_binaries(
        manifest,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    checkpoint_context = (
        _checkpoint_context(
            campaign_id=str(manifest["campaign_id"]),
            contract=contract,
            component_summary=component_summary,
            plan=plan,
            previous_source=previous_source,
        )
        if checkpoint_root is not None
        else None
    )
    checkpoint = None
    if checkpoint_root is not None and checkpoint_context is not None:
        # checkpoint 按不可变运行上下文分目录。这样同一轮中断可以继续，
        # 但新一轮（新计划、前序收据或工具组件变化）不会误读旧记录而被
        # “上下文漂移”卡死，也不需要删除任何历史 checkpoint。
        if checkpoint_root.is_symlink() or (
            checkpoint_root.exists() and not checkpoint_root.is_dir()
        ):
            raise JobRehearsalReceiptError("checkpoint 基目录不是可信目录")
        checkpoint_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(checkpoint_root.stat().st_mode) != 0o700:
            raise JobRehearsalReceiptError("checkpoint 基目录权限必须是 0700")
        context_name = str(checkpoint_context["context_sha256"])
        scoped_root = (
            checkpoint_root
            if checkpoint_root.name == context_name
            else checkpoint_root / context_name
        )
        checkpoint = incremental_recovery.CheckpointStore(scoped_root)
    checkpoint_results = (
        _load_checkpoint_results(
            checkpoint,
            context=checkpoint_context,
            jobs=jobs,
            component_summary=component_summary,
        )
        if checkpoint is not None and checkpoint_context is not None
        else {}
    )
    previous_by_id = {
        str(item.get("id")): item
        for item in (previous_facts.get("jobs", []) if previous_facts else [])
        if isinstance(item, Mapping)
    }
    execute_ids = set(plan["execute_job_ids"])
    source_binding = previous_source
    job_results: list[dict[str, Any]] = []
    for job in sorted(jobs, key=lambda item: (item.phase, item.job_id)):
        if deadline is not None:
            deadline.check(f"job-rehearsal:{job.job_id}:start")
        if heartbeat is not None:
            heartbeat(f"job-rehearsal:{job.job_id}:start")
        job_id = str(job.job_id)
        started = time.monotonic()
        checkpoint_entry = checkpoint_results.get(job_id)
        reused_from_checkpoint = False
        if checkpoint_entry is not None and checkpoint_entry[1].get("status") in {
            "passed",
            "complete",
        }:
            # 中断后已经成功完成的 Job 直接从本轮 checkpoint 复用；不再触发
            # 命令或容器探针。来源绑定指向不可变 checkpoint 文件。
            prior = dict(checkpoint_entry[1])
            prior.update(
                {
                    "status": "passed",
                    "disposition": "reused",
                    "source_receipt": _checkpoint_result_binding(
                        checkpoint, checkpoint_entry[0]
                    ),
                }
            )
            result = prior
            reused_from_checkpoint = True
        elif job_id not in execute_ids:
            prior = previous_by_id.get(job_id)
            if not isinstance(prior, Mapping) or prior.get("status") != "passed":
                # 计划不应产生未知项；失败关闭而不是悄悄少写一条 Job。
                raise JobRehearsalReceiptError(
                    f"增量计划试图复用不存在或未通过的 Job：{job_id}"
                )
            current_meta = _job_incremental_metadata(
                _job_document(job), component_summary
            )
            reused = dict(prior)
            rebound_steps: list[dict[str, Any]] = []
            current_document = _job_document(job)
            current_steps = current_document["steps"]
            prior_steps = prior.get("steps")
            if (
                not isinstance(prior_steps, list)
                or len(prior_steps) != len(current_steps)
            ):
                raise JobRehearsalReceiptError(
                    f"复用 Job 的步骤数量漂移：{job_id}"
                )
            for prior_step, current_step in zip(prior_steps, current_steps):
                if not isinstance(prior_step, Mapping):
                    raise JobRehearsalReceiptError(
                        f"复用 Job 的步骤事实非法：{job_id}"
                    )
                rebound_step = dict(prior_step)
                rebound_step.update(
                    {
                        "argv_sha256": _fingerprint(current_step["argv"]),
                        "environment_sha256": _fingerprint(
                            current_step["environment"]
                        ),
                        "timeout_seconds": current_step["timeout"],
                    }
                )
                rebound_steps.append(rebound_step)
            reused.update(
                {
                    "status": "passed",
                    "disposition": "reused",
                    "source_receipt": dict(source_binding or {}),
                    "job_contract_sha256": current_meta["input_sha256"],
                    "steps": rebound_steps,
                    "tool_components": current_meta["components"],
                    "tool_component_digests": current_meta["component_digests"],
                    "input_sha256": current_meta["input_sha256"],
                    "environment_sha256": current_meta["environment_sha256"],
                    "dependency_sha256": current_meta["dependency_sha256"],
                    "incremental_result_key": current_meta["result_key"],
                }
            )
            result = reused
        else:
            try:
                result = _job_probe(
                    job,
                    str(configuration["capture_container"]),
                    component_summary=component_summary,
                )
            except (JobRehearsalReceiptError, OSError, subprocess.SubprocessError) as error:
                result = _failed_job_probe(
                    job,
                    component_summary,
                    error,
                    duration_seconds=time.monotonic() - started,
                )
        job_results.append(result)
        if deadline is not None:
            deadline.check(f"job-rehearsal:{job.job_id}:complete")
        if heartbeat is not None:
            heartbeat(f"job-rehearsal:{job.job_id}:complete")
        if checkpoint is not None and not reused_from_checkpoint:
            records = checkpoint.records()
            previous_digest = (
                records[-1].get("checkpoint_sha256") if records else None
            )
            checkpoint.append(
                {
                    **(checkpoint_context or {}),
                    "item_id": job_id,
                    "disposition": result.get("disposition", "executed"),
                    "status": result.get("status"),
                    "result_sha256": incremental_recovery.digest(result),
                    "result_key": result.get("incremental_result_key"),
                    "result": result,
                    "source_receipt": result.get("source_receipt"),
                    "error": result.get("error"),
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "scanned_bytes": 0,
                    "reused_bytes": 0,
                    "previous_checkpoint_sha256": previous_digest,
                }
            )
    facts: dict[str, Any] = {
        "schema_version": FACTS_SCHEMA,
        "observed_at_utc": _utc_now(),
        "preflight_campaign": {
            "path": str(campaign_dir),
            "campaign_id": manifest["campaign_id"],
            "manifest_sha256": _sha256_file(campaign_dir / "campaign.json"),
            "campaign_mode": "preflight_only",
        },
        "execution_contract": contract,
        "execution_contract_sha256": execution_contract_sha256(contract),
        "tool_components": component_summary,
        "incremental_plan": {
            **plan,
            "affected_job_ids": [
                job_id
                for job_id in plan["execute_job_ids"]
                if plan["reasons"].get(job_id)
                in {"dependency_changed", "contract_changed"}
            ],
            "previous_receipt": dict(source_binding) if source_binding else None,
        },
        "host": {
            "architecture": EXPECTED_ARCHITECTURE,
            "machine": machine,
        },
        "containers": _container_facts(configuration),
        "tool_trees": {
            "managed_host": managed_tree,
            "execution_host": execution_tree,
            "execution_container": container_tree,
        },
        "dependencies": {
            "host": _host_dependencies(),
            "capture_container": _container_dependencies(
                str(configuration["capture_container"])
            ),
        },
        "binary_verification": binary_verification,
        "probes": {
            "bubblewrap": _bwrap_probe(str(configuration["capture_container"])),
            "failure_lifecycle": failure_lifecycle_probe,
            "storage": storage_probe,
            "zstd": _zstd_probe(
                str(configuration["capture_container"]), str(execution_root)
            ),
        },
        "jobs": job_results,
        "summary": {
            "job_count": len(job_results),
            "passed_job_count": sum(
                item.get("status") == "passed" for item in job_results
            ),
            "phase_counts": contract["phase_counts"],
            "job_set_sha256": _fingerprint(
                [
                    {
                        "id": item["id"],
                        "phase": item["phase"],
                        "job_contract_sha256": item["job_contract_sha256"],
                    }
                    for item in job_results
                ]
            ),
            "live_requests_sent": False,
            "status": (
                "passed"
                if all(item.get("status") == "passed" for item in job_results)
                else "failed"
            ),
            "executed_job_ids": [
                item["id"]
                for item in job_results
                if item.get("disposition", "executed") == "executed"
            ],
            "reused_job_ids": [
                item["id"]
                for item in job_results
                if item.get("disposition") == "reused"
            ],
            "affected_job_ids": [
                job_id
                for job_id in plan["execute_job_ids"]
                if plan["reasons"].get(job_id)
                in {"dependency_changed", "contract_changed"}
            ],
            "failed_job_ids": [
                item["id"]
                for item in job_results
                if item.get("status") == "failed"
            ],
        },
        "collector": _producer(),
    }
    if checkpoint is not None:
        records = checkpoint.records()
        facts["checkpoint"] = {
            "path": str(checkpoint.root),
            "record_count": len(records),
            "last_sequence": records[-1].get("checkpoint_sequence") if records else 0,
            "last_sha256": records[-1].get("checkpoint_sha256") if records else None,
        }
    facts["runtime_identity_sha256"] = _runtime_identity(facts)
    validate_facts(facts)
    if deadline is not None:
        deadline.check("job-rehearsal:complete")
    return facts


def collect_facts(
    campaign_dir: Path,
    *,
    previous_receipt: Path | None = None,
    previous_receipt_root: Path | None = None,
    rerun_failed: bool = False,
    checkpoint_root: Path | None = None,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """运行完整 Job 演练，并把所有内部探针绑定到同一条 deadline。"""

    global _ACTIVE_DEADLINE, _ACTIVE_HEARTBEAT
    previous_deadline = _ACTIVE_DEADLINE
    previous_heartbeat = _ACTIVE_HEARTBEAT
    _ACTIVE_DEADLINE = deadline
    _ACTIVE_HEARTBEAT = heartbeat
    try:
        return _collect_facts(
            campaign_dir,
            previous_receipt=previous_receipt,
            previous_receipt_root=previous_receipt_root,
            rerun_failed=rerun_failed,
            checkpoint_root=checkpoint_root,
            deadline=deadline,
            heartbeat=heartbeat,
        )
    finally:
        _ACTIVE_DEADLINE = previous_deadline
        _ACTIVE_HEARTBEAT = previous_heartbeat


def _validate_tree(value: Any, label: str) -> dict[str, Any]:
    tree = _expect(
        value,
        {"root", "entry_count", "files_sha256", "entries"},
        label,
    )
    entries = tree.get("entries")
    if (
        not isinstance(tree.get("root"), str)
        or not tree["root"].startswith("/")
        or not isinstance(entries, list)
        or not entries
        or tree.get("entry_count") != len(entries)
        or tree.get("files_sha256") != _fingerprint({"entries": entries})
        or not all(
            isinstance(item, dict)
            and set(item) == {"path", "sha256"}
            and isinstance(item["path"], str)
            and not item["path"].startswith("/")
            and SHA256_RE.fullmatch(str(item["sha256"]))
            for item in entries
        )
    ):
        raise JobRehearsalReceiptError(f"{label}摘要或条目非法")
    return tree


def _validate_dependencies(value: Any, expected: Iterable[str], label: str) -> None:
    if not isinstance(value, list):
        raise JobRehearsalReceiptError(f"{label}必须是数组")
    names: list[str] = []
    for item in value:
        binding = _expect(item, {"name", "path", "sha256", "status"}, label)
        if (
            binding.get("status") != "passed"
            or not isinstance(binding.get("name"), str)
            or not isinstance(binding.get("path"), str)
            or not binding["path"].startswith("/")
            or not SHA256_RE.fullmatch(str(binding.get("sha256", "")))
        ):
            raise JobRehearsalReceiptError(f"{label}存在失败或非法依赖")
        names.append(binding["name"])
    if names != list(expected):
        raise JobRehearsalReceiptError(f"{label}命令集合漂移")


def _validate_archive_route_probe(
    value: Any,
    *,
    aliases: list[PurePosixPath],
    host_runs_root: PurePosixPath,
) -> dict[str, Any]:
    """重放失败证据归档的四段闭环事实。"""

    route = _expect(
        value,
        {
            "status",
            "namespace",
            "source_name",
            "archive_name",
            "host_source",
            "host_archive",
            "container_sources",
            "container_archives",
            "created_via",
            "archived_via",
            "read_via",
            "device",
            "inode",
            "cleanup_verified",
        },
        "probes.storage.archive_route",
    )
    source_name = _safe_id(route.get("source_name"), "archive_route.source_name")
    archive_name = _safe_id(
        route.get("archive_name"), "archive_route.archive_name"
    )
    expected_sources = [str(alias / "runs" / source_name) for alias in aliases]
    expected_archives = [str(alias / "runs" / archive_name) for alias in aliases]
    expected_host_source = str(host_runs_root / source_name)
    expected_host_archive = str(host_runs_root / archive_name)
    if (
        route.get("status") != "passed"
        or route.get("namespace") != "runs"
        or archive_name != source_name + FAILED_EVIDENCE_ARCHIVE_SUFFIX
        or route.get("host_source") != expected_host_source
        or route.get("host_archive") != expected_host_archive
        or route.get("container_sources") != expected_sources
        or route.get("container_archives") != expected_archives
        or route.get("created_via") != expected_sources[0]
        or route.get("archived_via") != expected_host_archive
        or route.get("read_via") != expected_archives
        or route.get("cleanup_verified") is not True
        or not isinstance(route.get("device"), int)
        or isinstance(route.get("device"), bool)
        or route["device"] < 0
        or not isinstance(route.get("inode"), int)
        or isinstance(route.get("inode"), bool)
        or route["inode"] <= 0
    ):
        raise JobRehearsalReceiptError("失败证据归档路由事实非法")
    return route


def _validate_storage_probe(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """重放正式 Job 的登记运行根和可写别名事实。"""

    storage = _expect(
        value,
        {
            "status",
            "capture_container",
            "capture_root",
            "host_data_root",
            "root_mounts",
            "writable_namespaces",
            "archive_route",
            "job_count",
            "evidence_root_count",
            "job_roots_sha256",
            "job_roots",
        },
        "probes.storage",
    )
    configuration = contract["configuration"]
    capture_root = _absolute_posix_path(
        configuration["capture_root"], "execution_contract.capture_root"
    )
    aliases = sorted({CAPTURE_CONTAINER_ALIAS, capture_root}, key=str)
    if (
        storage.get("status") != "passed"
        or storage.get("capture_container") != configuration["capture_container"]
        or storage.get("capture_root") != str(capture_root)
        or capture_root != EXPECTED_CAPTURE_CONTAINER_ROOT
    ):
        raise JobRehearsalReceiptError("可写运行目录探针身份非法")

    host_root = _expect(
        storage.get("host_data_root"),
        {"path", "mode", "uid", "gid", "device", "inode"},
        "probes.storage.host_data_root",
    )
    if (
        host_root.get("path") != str(EXPECTED_HOST_DATA_ROOT)
        or host_root.get("mode") != 0o700
    ):
        raise JobRehearsalReceiptError("登记宿主数据根事实非法")
    for field in ("uid", "gid", "device", "inode"):
        current = host_root.get(field)
        if (
            not isinstance(current, int)
            or isinstance(current, bool)
            or current < 0
            or (field == "inode" and current == 0)
        ):
            raise JobRehearsalReceiptError(
                f"登记宿主数据根 {field} 非法"
            )

    def validate_mount(
        raw_mount: Any,
        *,
        destination: PurePosixPath,
        source: PurePosixPath,
        read_only: bool,
        label: str,
    ) -> dict[str, Any]:
        mount = _expect(
            raw_mount,
            {"type", "source", "destination", "read_only"},
            label,
        )
        if (
            mount.get("type") != "bind"
            or mount.get("source") != str(source)
            or mount.get("destination") != str(destination)
            or mount.get("read_only") is not read_only
        ):
            raise JobRehearsalReceiptError(f"{label}挂载身份非法")
        return mount

    root_mounts = storage.get("root_mounts")
    if not isinstance(root_mounts, list) or len(root_mounts) != len(aliases):
        raise JobRehearsalReceiptError("只读父根挂载未完整覆盖容器别名")
    for mount, alias in zip(root_mounts, aliases, strict=True):
        validate_mount(
            mount,
            destination=alias,
            source=EXPECTED_HOST_DATA_ROOT,
            read_only=True,
            label=f"probes.storage.root_mounts.{alias}",
        )

    namespaces = storage.get("writable_namespaces")
    if (
        not isinstance(namespaces, list)
        or [item.get("name") for item in namespaces if isinstance(item, Mapping)]
        != list(WRITABLE_CAPTURE_NAMESPACES)
    ):
        raise JobRehearsalReceiptError("可写运行目录未完整覆盖登记子树")
    for raw_namespace, expected_name in zip(
        namespaces, WRITABLE_CAPTURE_NAMESPACES, strict=True
    ):
        namespace = _expect(
            raw_namespace,
            {
                "name",
                "source",
                "source_mode",
                "source_uid",
                "source_gid",
                "source_device",
                "source_inode",
                "mounts",
                "destinations",
                "created_via",
                "cleanup_verified",
            },
            f"probes.storage.{expected_name}",
        )
        expected_source = EXPECTED_HOST_DATA_ROOT / expected_name
        if (
            namespace.get("name") != expected_name
            or namespace.get("source") != str(expected_source)
            or namespace.get("cleanup_verified") is not True
            or namespace.get("created_via") != [str(alias) for alias in aliases]
        ):
            raise JobRehearsalReceiptError(
                f"可写运行目录 {expected_name} 身份或清理事实非法"
            )
        numeric_fields = (
            "source_mode",
            "source_uid",
            "source_gid",
            "source_device",
            "source_inode",
        )
        if any(
            not isinstance(namespace.get(field), int)
            or isinstance(namespace.get(field), bool)
            or namespace[field] < 0
            for field in numeric_fields
        ) or namespace["source_inode"] == 0:
            raise JobRehearsalReceiptError(
                f"可写运行目录 {expected_name} 宿主统计非法"
            )
        if namespace["source_mode"] & 0o022:
            raise JobRehearsalReceiptError(
                f"可写运行目录 {expected_name} 权限过宽"
            )
        mounts = namespace.get("mounts")
        destinations = namespace.get("destinations")
        if (
            not isinstance(mounts, list)
            or not isinstance(destinations, list)
            or len(mounts) != len(aliases)
            or len(destinations) != len(aliases)
        ):
            raise JobRehearsalReceiptError(
                f"可写运行目录 {expected_name} 别名数量非法"
            )
        for mount, destination_fact, alias in zip(
            mounts, destinations, aliases, strict=True
        ):
            destination = alias / expected_name
            validate_mount(
                mount,
                destination=destination,
                source=expected_source,
                read_only=False,
                label=f"probes.storage.{expected_name}.{alias}.mount",
            )
            current = _expect(
                destination_fact,
                {"path", "device", "inode", "mode", "uid", "gid"},
                f"probes.storage.{expected_name}.{alias}.stat",
            )
            if (
                current.get("path") != str(destination)
                or current.get("device") != namespace["source_device"]
                or current.get("inode") != namespace["source_inode"]
                or current.get("mode") != namespace["source_mode"]
                or current.get("uid") != namespace["source_uid"]
                or current.get("gid") != namespace["source_gid"]
            ):
                raise JobRehearsalReceiptError(
                    f"可写运行目录 {expected_name} 宿主／容器不同源"
                )

    archive_route = _validate_archive_route_probe(
        storage.get("archive_route"),
        aliases=aliases,
        host_runs_root=EXPECTED_HOST_DATA_ROOT / "runs",
    )
    runs_namespace = next(
        item
        for item in namespaces
        if isinstance(item, Mapping) and item.get("name") == "runs"
    )
    if archive_route["device"] != runs_namespace["source_device"]:
        raise JobRehearsalReceiptError("失败证据归档路由未落在登记 runs 设备")

    job_roots = storage.get("job_roots")
    if not isinstance(job_roots, list):
        raise JobRehearsalReceiptError("Job 运行根必须是数组")
    expected_job_ids = list(contract["job_ids"])
    actual_job_ids = [
        item.get("job_id") for item in job_roots if isinstance(item, Mapping)
    ]
    if actual_job_ids != expected_job_ids:
        raise JobRehearsalReceiptError("Job 运行根未完整覆盖冻结 Job 集")
    evidence_count = 0
    allowed_run_roots = [alias / "runs" for alias in aliases]
    for raw_job in job_roots:
        job = _expect(
            raw_job,
            {"job_id", "evidence_roots"},
            "probes.storage.job_roots",
        )
        roots = job.get("evidence_roots")
        if (
            not isinstance(roots, list)
            or not roots
            or roots != sorted(set(roots))
        ):
            raise JobRehearsalReceiptError(
                f"{job.get('job_id')} 运行根为空、重复或未排序"
            )
        for index, raw_root in enumerate(roots, 1):
            current = _absolute_posix_path(
                raw_root,
                f"{job.get('job_id')} evidence_root {index}",
            )
            if not any(
                current != allowed and current.is_relative_to(allowed)
                for allowed in allowed_run_roots
            ):
                raise JobRehearsalReceiptError(
                    f"{job.get('job_id')} 运行根越过登记 runs 子树"
                )
        evidence_count += len(roots)
    if (
        storage.get("job_count") != len(expected_job_ids)
        or storage.get("evidence_root_count") != evidence_count
        or storage.get("job_roots_sha256") != _fingerprint(job_roots)
    ):
        raise JobRehearsalReceiptError("Job 运行根汇总或摘要漂移")
    return storage


def _validate_failure_lifecycle_probe(
    value: Any,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """重放 campaign-run、父租约、三次失败归档与清理的联合事实。"""

    probe = _expect(
        value,
        {
            "schema_version",
            "status",
            "campaign_run_schema_version",
            "campaign_id",
            "job_id",
            "source_name",
            "capture_container",
            "capture_root",
            "host_runs_root",
            "retry_limit",
            "attempt_count",
            "network_isolated",
            "live_request_count",
            "archives",
            "final_result",
            "parent_supervisor",
            "cleanup_verified",
        },
        "probes.failure_lifecycle",
    )
    configuration = contract.get("configuration")
    if not isinstance(configuration, Mapping):
        raise JobRehearsalReceiptError("失败生命周期探针缺少执行配置")
    capture_root = _absolute_posix_path(
        configuration.get("capture_root"),
        "execution_contract.capture_root",
    )
    aliases = sorted({CAPTURE_CONTAINER_ALIAS, capture_root}, key=str)
    campaign_id = _safe_id(probe.get("campaign_id"), "failure_lifecycle.campaign_id")
    source_name = _safe_id(probe.get("source_name"), "failure_lifecycle.source_name")
    if (
        probe.get("schema_version") != FAILURE_LIFECYCLE_SCHEMA
        or probe.get("status") != "passed"
        or probe.get("campaign_run_schema_version")
        != "codex-upgrade-campaign-run/v2"
        or not campaign_id.startswith("p0-failure-lifecycle-")
        or not source_name.startswith("codex-failure-lifecycle-")
        or probe.get("job_id") != FAILURE_LIFECYCLE_JOB_ID
        or probe.get("capture_container") != configuration.get("capture_container")
        or probe.get("capture_root") != str(capture_root)
        or capture_root != EXPECTED_CAPTURE_CONTAINER_ROOT
        or probe.get("host_runs_root")
        != str(EXPECTED_HOST_DATA_ROOT / "runs")
        or probe.get("retry_limit") != 2
        or probe.get("attempt_count") != FAILURE_LIFECYCLE_ATTEMPT_COUNT
        or probe.get("network_isolated") is not True
        or probe.get("live_request_count") != 0
        or probe.get("cleanup_verified") is not True
    ):
        raise JobRehearsalReceiptError("失败生命周期探针身份、重试或零网络事实非法")

    archives = probe.get("archives")
    if (
        not isinstance(archives, list)
        or len(archives) != FAILURE_LIFECYCLE_ATTEMPT_COUNT
    ):
        raise JobRehearsalReceiptError("失败生命周期归档数量非法")
    seen_inodes: set[tuple[int, int]] = set()
    for attempt_index, raw_archive in enumerate(archives, 1):
        archive = _expect(
            raw_archive,
            {
                "attempt_index",
                "archive_name",
                "host_archive",
                "container_archives",
                "device",
                "inode",
                "marker_sha256",
                "marker",
            },
            f"probes.failure_lifecycle.archives.{attempt_index}",
        )
        archive_name = f"{source_name}.failed-attempt{attempt_index}"
        expected_host = str(EXPECTED_HOST_DATA_ROOT / "runs" / archive_name)
        expected_containers = [
            str(alias / "runs" / archive_name) for alias in aliases
        ]
        marker = _expect(
            archive.get("marker"),
            {
                "schema_version",
                "source_name",
                "attempt_index",
                "network_isolated",
                "live_request_count",
            },
            f"probes.failure_lifecycle.archives.{attempt_index}.marker",
        )
        device = archive.get("device")
        inode = archive.get("inode")
        if (
            archive.get("attempt_index") != attempt_index
            or archive.get("archive_name") != archive_name
            or archive.get("host_archive") != expected_host
            or archive.get("container_archives") != expected_containers
            or not isinstance(device, int)
            or isinstance(device, bool)
            or device < 0
            or not isinstance(inode, int)
            or isinstance(inode, bool)
            or inode <= 0
            or (device, inode) in seen_inodes
            or marker
            != {
                "schema_version": FAILURE_LIFECYCLE_MARKER_SCHEMA,
                "source_name": source_name,
                "attempt_index": attempt_index,
                "network_isolated": True,
                "live_request_count": 0,
            }
            or archive.get("marker_sha256") != _sha256_bytes(_canonical(marker))
        ):
            raise JobRehearsalReceiptError(
                f"失败生命周期第 {attempt_index} 次归档事实非法"
            )
        seen_inodes.add((device, inode))

    final_result = _expect(
        probe.get("final_result"),
        {"status", "attempt_index", "evidence_roots"},
        "probes.failure_lifecycle.final_result",
    )
    expected_final_root = str(
        capture_root
        / "runs"
        / f"{source_name}.failed-attempt{FAILURE_LIFECYCLE_ATTEMPT_COUNT}"
    )
    if final_result != {
        "status": "failed",
        "attempt_index": FAILURE_LIFECYCLE_ATTEMPT_COUNT,
        "evidence_roots": [expected_final_root],
    }:
        raise JobRehearsalReceiptError("失败生命周期最终 Job 结果非法")

    parent = _expect(
        probe.get("parent_supervisor"),
        {
            "run_state",
            "audit_incomplete",
            "event_count",
            "required_event_count",
            "actions",
        },
        "probes.failure_lifecycle.parent_supervisor",
    )
    event_count = parent.get("event_count")
    required_event_count = parent.get("required_event_count")
    if (
        parent.get("run_state") != "stopped"
        or parent.get("audit_incomplete") is not False
        or not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or not isinstance(required_event_count, int)
        or isinstance(required_event_count, bool)
        or required_event_count != 16
        or event_count < required_event_count
        or parent.get("actions")
        != [
            {
                "action_id": "failure-lifecycle",
                "returncode": 0,
                "status": "passed",
            }
        ]
    ):
        raise JobRehearsalReceiptError("失败生命周期父监督器终态非法")
    return probe


def _validate_component_summary(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JobRehearsalReceiptError(f"{label}必须是对象")
    components = value.get("components")
    if (
        value.get("schema_version") != incremental_recovery.SCHEMA_VERSION
        or not isinstance(components, Mapping)
        or value.get("component_count") != len(components)
        or not SHA256_RE.fullmatch(str(value.get("all_sha256", "")))
    ):
        raise JobRehearsalReceiptError(f"{label}摘要非法")
    all_entries: list[dict[str, str]] = []
    for name, component in components.items():
        if not isinstance(name, str) or not name or not isinstance(component, Mapping):
            raise JobRehearsalReceiptError(f"{label}组件名称或内容非法")
        entries = component.get("entries")
        if not isinstance(entries, list) or component.get("entry_count") != len(entries):
            raise JobRehearsalReceiptError(f"{label}.{name}条目数量非法")
        try:
            normalized = incremental_recovery.normalize_entries(entries)
        except (TypeError, incremental_recovery.IncrementalRecoveryError) as error:
            raise JobRehearsalReceiptError(f"{label}.{name}条目非法") from error
        if normalized != entries or component.get("sha256") != incremental_recovery.digest({"entries": entries}):
            raise JobRehearsalReceiptError(f"{label}.{name}摘要不一致")
        all_entries.extend(entries)
    try:
        normalized_all = incremental_recovery.normalize_entries(all_entries)
    except incremental_recovery.IncrementalRecoveryError as error:
        raise JobRehearsalReceiptError(f"{label}组件路径重复或非法") from error
    if value.get("all_sha256") != incremental_recovery.digest({"entries": normalized_all}):
        raise JobRehearsalReceiptError(f"{label}.all_sha256摘要不一致")
    return dict(value)


def _validate_incremental_plan(
    value: Any,
    job_ids: set[str],
    label: str = "incremental_plan",
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JobRehearsalReceiptError(f"{label}必须是对象")
    required = {
        "schema_version",
        "contract_sha256",
        "execute_job_ids",
        "reused_job_ids",
        "failed_job_ids",
        "changed_components",
        "reasons",
        "plan_sha256",
        "affected_job_ids",
        "previous_receipt",
    }
    if set(value) != required:
        raise JobRehearsalReceiptError(f"{label}字段不闭合")
    execute = value.get("execute_job_ids")
    reused = value.get("reused_job_ids")
    failed = value.get("failed_job_ids")
    affected = value.get("affected_job_ids")
    changed = value.get("changed_components")
    reasons = value.get("reasons")
    arrays = (execute, reused, failed, affected, changed)
    if any(
        not isinstance(item, list)
        or not all(isinstance(entry, str) and entry for entry in item)
        or item != sorted(set(item))
        for item in arrays
    ):
        raise JobRehearsalReceiptError(f"{label}列表非法")
    if (
        not set(execute).issubset(job_ids)
        or not set(reused).issubset(job_ids)
        or not set(failed).issubset(job_ids)
        or not set(affected).issubset(job_ids)
        or set(execute) & set(reused)
        or not isinstance(reasons, Mapping)
        or set(reasons) != job_ids
        or not all(isinstance(reason, str) and reason for reason in reasons.values())
        or value.get("schema_version") != incremental_recovery.SCHEMA_VERSION
        or not SHA256_RE.fullmatch(str(value.get("contract_sha256", "")))
        or not SHA256_RE.fullmatch(str(value.get("plan_sha256", "")))
    ):
        raise JobRehearsalReceiptError(f"{label}身份或集合非法")
    previous = value.get("previous_receipt")
    if previous is not None:
        if (
            not isinstance(previous, Mapping)
            or set(previous) != {"path", "sha256", "bytes"}
            or not isinstance(previous.get("path"), str)
            or not SHA256_RE.fullmatch(str(previous.get("sha256", "")))
            or not isinstance(previous.get("bytes"), int)
            or isinstance(previous.get("bytes"), bool)
            or previous.get("bytes") <= 0
        ):
            raise JobRehearsalReceiptError(f"{label}前序收据绑定非法")
    expected_core = {
        "contract_sha256": value["contract_sha256"],
        "execute_job_ids": execute,
        "reused_job_ids": reused,
        "failed_job_ids": failed,
        "changed_components": changed,
        "reasons": dict(reasons),
    }
    if value.get("plan_sha256") != incremental_recovery.digest(expected_core):
        raise JobRehearsalReceiptError(f"{label}摘要不一致")
    return dict(value)


def _validate_incremental_noop_facts(
    facts: Mapping[str, Any],
    *,
    allow_collector_drift: bool = False,
) -> dict[str, Any]:
    """校验不产生新通过事实的增量空操作。

    no-op 不携带运行时探针、容器身份或 Job 结果正文；这些事实只能从
    ``source_receipt`` 指向的原有 passed 收据读取。将 no-op 单独校验，避免
    把“没有需要执行”误判成一轮新的完整 ARM64 演练。
    """

    required = {
        "schema_version",
        "status",
        "observed_at_utc",
        "preflight_campaign",
        "execution_contract",
        "execution_contract_sha256",
        "incremental_plan",
        "tool_components",
        "incremental_noop",
        "collector",
    }
    if not isinstance(facts, Mapping) or set(facts) != required:
        raise JobRehearsalReceiptError("incremental-noop facts字段不闭合")
    if (
        facts.get("schema_version") != FACTS_SCHEMA
        or facts.get("status") != INCREMENTAL_NOOP_STATUS
    ):
        raise JobRehearsalReceiptError("incremental-noop facts身份非法")
    _rfc3339(facts.get("observed_at_utc"), "incremental-noop.observed_at_utc")
    campaign = _expect(
        facts.get("preflight_campaign"),
        {"path", "campaign_id", "manifest_sha256", "campaign_mode"},
        "incremental-noop.preflight_campaign",
    )
    if (
        not isinstance(campaign.get("path"), str)
        or not campaign["path"].startswith("/")
        or campaign.get("campaign_mode") != "preflight_only"
        or not SAFE_ID_RE.fullmatch(str(campaign.get("campaign_id", "")))
        or not SHA256_RE.fullmatch(str(campaign.get("manifest_sha256", "")))
    ):
        raise JobRehearsalReceiptError("incremental-noop Campaign 绑定非法")
    contract = validate_execution_contract(
        dict(facts.get("execution_contract") or {})
    )
    contract_sha = execution_contract_sha256(contract)
    if facts.get("execution_contract_sha256") != contract_sha:
        raise JobRehearsalReceiptError("incremental-noop execution contract 摘要漂移")
    job_ids = set(str(item) for item in contract["job_ids"])
    plan = _validate_incremental_plan(
        facts.get("incremental_plan"),
        job_ids,
        label="incremental-noop.incremental_plan",
    )
    if (
        plan.get("contract_sha256") != contract_sha
        or plan.get("execute_job_ids") != []
        or plan.get("failed_job_ids") != []
        or plan.get("affected_job_ids") != []
        or plan.get("reused_job_ids") != sorted(job_ids)
    ):
        raise JobRehearsalReceiptError("incremental-noop 计划不是完整复用空集")
    _validate_component_summary(
        facts.get("tool_components"), "incremental-noop.tool_components"
    )
    noop = _expect(
        facts.get("incremental_noop"),
        {
            "schema_version",
            "new_pass_fact",
            "planned_job_ids",
            "execute_job_ids",
            "reused_job_ids",
            "affected_job_ids",
            "failed_job_ids",
            "changed_components",
            "plan_sha256",
            "source_receipt",
            "source_root",
            "source_status",
            "source_job_count",
            "source_job_set_sha256",
            "scanned_bytes",
            "live_request_count",
            "recorded_at_utc",
        },
        "incremental-noop",
    )
    planned = noop.get("planned_job_ids")
    reused = noop.get("reused_job_ids")
    changed = noop.get("changed_components")
    if any(
        not isinstance(value, list)
        or value != sorted(set(value))
        or not all(isinstance(item, str) and item for item in value)
        for value in (planned, reused, changed)
    ):
        raise JobRehearsalReceiptError("incremental-noop 列表非法")
    if (
        planned != sorted(job_ids)
        or noop.get("execute_job_ids") != []
        or noop.get("affected_job_ids") != []
        or noop.get("failed_job_ids") != []
        or reused != sorted(job_ids)
        or noop.get("new_pass_fact") is not False
        or noop.get("schema_version") != INCREMENTAL_NOOP_SCHEMA
        or noop.get("plan_sha256") != plan.get("plan_sha256")
        or noop.get("source_status") != "passed"
        or noop.get("scanned_bytes") != 0
        or noop.get("live_request_count") != 0
        or not isinstance(noop.get("source_root"), str)
        or not noop["source_root"].startswith("/")
        or not isinstance(noop.get("source_job_count"), int)
        or isinstance(noop.get("source_job_count"), bool)
        or noop.get("source_job_count") != len(job_ids)
        or not SHA256_RE.fullmatch(str(noop.get("source_job_set_sha256", "")))
    ):
        raise JobRehearsalReceiptError("incremental-noop 事实或计数非法")
    source = noop.get("source_receipt")
    if (
        not isinstance(source, Mapping)
        or set(source) != {"path", "sha256", "bytes"}
        or not isinstance(source.get("path"), str)
        or source["path"].startswith("/")
        or "\\" in source["path"]
        or str(PurePosixPath(source["path"])) != source["path"]
        or any(part in {"", ".", ".."} for part in PurePosixPath(source["path"]).parts)
        or not SHA256_RE.fullmatch(str(source.get("sha256", "")))
        or not isinstance(source.get("bytes"), int)
        or isinstance(source.get("bytes"), bool)
        or source.get("bytes") <= 0
        or source != plan.get("previous_receipt")
    ):
        raise JobRehearsalReceiptError("incremental-noop 原有通过收据绑定非法")
    _rfc3339(noop.get("recorded_at_utc"), "incremental-noop.recorded_at_utc")
    collector = _expect(
        facts.get("collector"),
        {"schema_version", "tool", "tool_sha256", "version"},
        "incremental-noop.collector",
    )
    if not allow_collector_drift and collector != _producer():
        raise JobRehearsalReceiptError("incremental-noop 采集器身份漂移")
    return {
        "execution_contract_sha256": contract_sha,
        "runtime_identity_sha256": None,
        "job_count": len(job_ids),
        "job_set_sha256": str(noop["source_job_set_sha256"]),
        "status": INCREMENTAL_NOOP_STATUS,
        "passed_job_count": 0,
        "failed_job_ids": [],
    }


def validate_facts(
    facts: dict[str, Any],
    *,
    allow_collector_drift: bool = False,
) -> dict[str, Any]:
    """严格校验离线演练事实，并返回收据摘要。"""

    # no-op facts 使用严格的最小字段集；不能落入完整演练校验并被当作新通过事实。
    if (
        isinstance(facts, Mapping)
        and (
            facts.get("status") == INCREMENTAL_NOOP_STATUS
            or "incremental_noop" in facts
        )
    ):
        return _validate_incremental_noop_facts(
            facts,
            allow_collector_drift=allow_collector_drift,
        )

    required_fact_fields = {
        "schema_version",
        "observed_at_utc",
        "preflight_campaign",
        "execution_contract",
        "execution_contract_sha256",
        "host",
        "containers",
        "tool_trees",
        "dependencies",
        "binary_verification",
        "probes",
        "jobs",
        "summary",
        "runtime_identity_sha256",
        "collector",
    }
    optional_fact_fields = {"tool_components", "incremental_plan", "checkpoint"}
    if (
        not isinstance(facts, dict)
        or not required_fact_fields.issubset(facts)
        or not set(facts).issubset(required_fact_fields | optional_fact_fields)
    ):
        raise JobRehearsalReceiptError("facts字段不闭合")
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise JobRehearsalReceiptError("facts.schema_version 不匹配")
    _rfc3339(facts.get("observed_at_utc"), "facts.observed_at_utc")
    campaign = _expect(
        facts.get("preflight_campaign"),
        {"path", "campaign_id", "manifest_sha256", "campaign_mode"},
        "preflight_campaign",
    )
    if (
        not isinstance(campaign.get("path"), str)
        or not campaign["path"].startswith("/")
        or campaign.get("campaign_mode") != "preflight_only"
        or not SAFE_ID_RE.fullmatch(str(campaign.get("campaign_id", "")))
        or not SHA256_RE.fullmatch(str(campaign.get("manifest_sha256", "")))
    ):
        raise JobRehearsalReceiptError("preflight Campaign 绑定非法")
    contract = validate_execution_contract(facts.get("execution_contract"))
    contract_sha = execution_contract_sha256(contract)
    if facts.get("execution_contract_sha256") != contract_sha:
        raise JobRehearsalReceiptError("execution contract 摘要漂移")
    host = _expect(facts.get("host"), {"architecture", "machine"}, "host")
    if host.get("architecture") != EXPECTED_ARCHITECTURE or host.get("machine") not in {
        "aarch64",
        "arm64",
    }:
        raise JobRehearsalReceiptError("演练宿主不是 ARM64 Linux")
    configuration = contract["configuration"]
    expected_containers = sorted(
        {
            str(configuration[field])
            for field in (
                "capture_container",
                "service_container",
                "keeper_container",
                "postgres_container",
                "redis_container",
            )
        }
    )
    containers = facts.get("containers")
    if not isinstance(containers, list):
        raise JobRehearsalReceiptError("containers 必须是数组")
    container_names: list[str] = []
    for item in containers:
        current = _expect(
            item, {"name", "container_id", "image_id", "running"}, "containers"
        )
        if (
            current.get("running") is not True
            or not CONTAINER_ID_RE.fullmatch(str(current.get("container_id", "")))
            or not IMAGE_ID_RE.fullmatch(str(current.get("image_id", "")))
        ):
            raise JobRehearsalReceiptError("容器未运行或身份非法")
        container_names.append(str(current.get("name", "")))
    if container_names != expected_containers:
        raise JobRehearsalReceiptError("容器集合未完整覆盖 Job 依赖")
    trees = _expect(
        facts.get("tool_trees"),
        {"managed_host", "execution_host", "execution_container"},
        "tool_trees",
    )
    tree_values = [
        _validate_tree(trees[name], f"tool_trees.{name}")
        for name in ("managed_host", "execution_host", "execution_container")
    ]
    if (
        not all(tree["entries"] == tree_values[0]["entries"] for tree in tree_values)
        or tree_values[0]["files_sha256"] != contract["tool_files_sha256"]
    ):
        raise JobRehearsalReceiptError("三份工具树或 Campaign 工具摘要不一致")
    component_summary = facts.get("tool_components")
    if component_summary is not None:
        _validate_component_summary(component_summary, "tool_components")
    dependencies = _expect(
        facts.get("dependencies"), {"host", "capture_container"}, "dependencies"
    )
    _validate_dependencies(
        dependencies["host"], HOST_REQUIRED_COMMANDS, "dependencies.host"
    )
    _validate_dependencies(
        dependencies["capture_container"],
        CAPTURE_CONTAINER_REQUIRED_COMMANDS,
        "dependencies.capture_container",
    )
    binary = facts.get("binary_verification")
    if (
        not isinstance(binary, dict)
        or binary.get("passed") is not True
        or binary.get("expected_version") != contract["target_version"]
        or binary.get("expected_sha256") != contract["target_sha256"]
        or binary.get("runtime_image_reference")
        != configuration["runtime_image"]
        or not IMAGE_ID_RE.fullmatch(str(binary.get("runtime_image_id", "")))
    ):
        raise JobRehearsalReceiptError("Codex 二进制或运行镜像探针非法")
    package = binary.get("package")
    if (
        not isinstance(package, dict)
        or package.get("asset_sha256") != contract["target_package_sha256"]
        or package.get("code_mode_host_sha256")
        != contract["target_code_mode_host_sha256"]
    ):
        raise JobRehearsalReceiptError("Codex package 探针非法")
    identities = binary.get("identities")
    helpers = binary.get("helpers")
    if (
        not isinstance(identities, list)
        or {item.get("label") for item in identities if isinstance(item, dict)}
        != {
            "container:capture_codex_bin",
            "container:relay_codex_bin",
            "host:relay_codex_bin",
        }
        or any(item.get("sha256") != contract["target_sha256"] for item in identities)
        or not isinstance(helpers, list)
        or {item.get("label") for item in helpers if isinstance(item, dict)}
        != {
            "container:capture_code_mode_host_bin",
            "container:relay_code_mode_host_bin",
            "host:relay_code_mode_host_bin",
        }
        or any(
            item.get("sha256") != contract["target_code_mode_host_sha256"]
            for item in helpers
        )
    ):
        raise JobRehearsalReceiptError("Codex 主程序或 code-mode-host 身份未闭合")
    raw_probes = facts.get("probes")
    if not isinstance(raw_probes, dict):
        raise JobRehearsalReceiptError("probes必须是对象")
    probe_fields = set(raw_probes)
    legacy_probe_fields = {"bubblewrap", "zstd"}
    storage_probe_fields = {*legacy_probe_fields, "storage"}
    current_probe_fields = {*storage_probe_fields, "failure_lifecycle"}
    if probe_fields == legacy_probe_fields and allow_collector_drift:
        # 历史收据继续按原字节只读重放，但不能再作为新 Formal 的 P0 证明。
        probes = raw_probes
        storage_probe_sha256: str | None = None
        failure_lifecycle_probe_sha256: str | None = None
    elif probe_fields == storage_probe_fields and allow_collector_drift:
        # 失败生命周期门禁加入前生成的收据仍可逐字重放；它只证明旧版
        # storage 路由，不能作为当前 Formal 的完整 P0 证明。
        probes = raw_probes
        storage_probe = _validate_storage_probe(probes.get("storage"), contract)
        storage_probe_sha256 = _fingerprint(storage_probe)
        failure_lifecycle_probe_sha256 = None
    else:
        probes = _expect(raw_probes, current_probe_fields, "probes")
        storage_probe = _validate_storage_probe(probes.get("storage"), contract)
        storage_probe_sha256 = _fingerprint(storage_probe)
        failure_lifecycle_probe = _validate_failure_lifecycle_probe(
            probes.get("failure_lifecycle"), contract
        )
        failure_lifecycle_probe_sha256 = _fingerprint(failure_lifecycle_probe)
    bubblewrap = _expect(
        probes.get("bubblewrap"),
        {"status", "version", "network_isolated"},
        "probes.bubblewrap",
    )
    zstd = _expect(
        probes.get("zstd"),
        {"status", "input_sha256", "output_sha256", "output"},
        "probes.zstd",
    )
    if (
        bubblewrap.get("status") != "passed"
        or bubblewrap.get("network_isolated") is not True
        or not isinstance(bubblewrap.get("version"), str)
        or not bubblewrap["version"]
        or zstd.get("status") != "passed"
        or zstd.get("input_sha256") != _sha256_bytes(ZSTD_FRAME)
        or zstd.get("output_sha256") != _sha256_bytes(ZSTD_OUTPUT)
        or zstd.get("output") != ZSTD_OUTPUT.decode("ascii")
    ):
        raise JobRehearsalReceiptError("bubblewrap 或 zstd 离线探针失败")
    jobs = facts.get("jobs")
    if not isinstance(jobs, list) or len(jobs) != contract["job_count"]:
        raise JobRehearsalReceiptError("Job 演练结果数量不完整")
    seen: list[str] = []
    for item in jobs:
        required_job_fields = {
            "id",
            "phase",
            "status",
            "job_contract_sha256",
            "step_count",
            "steps",
            "c2pa_identity",
        }
        optional_job_fields = {
            "error",
            "duration_seconds",
            "disposition",
            "source_receipt",
            "tool_components",
            "tool_component_digests",
            "input_sha256",
            "environment_sha256",
            "dependency_sha256",
            "incremental_result_key",
        }
        if not isinstance(item, dict) or not set(item).issubset(
            required_job_fields | optional_job_fields
        ) or not required_job_fields.issubset(item):
            raise JobRehearsalReceiptError("jobs 字段不闭合")
        current = item
        job_id = str(current.get("id", ""))
        if (
            current.get("status") not in {"passed", "failed"}
            or contract["job_phases"].get(job_id) != current.get("phase")
            or contract["step_counts"].get(job_id) != current.get("step_count")
            or not SHA256_RE.fullmatch(str(current.get("job_contract_sha256", "")))
            or not isinstance(current.get("steps"), list)
            or len(current["steps"]) > current["step_count"]
            or (
                current.get("status") == "passed"
                and len(current["steps"]) != current["step_count"]
            )
        ):
            raise JobRehearsalReceiptError(f"Job 演练失败或身份非法：{job_id}")
        if current.get("disposition") not in {None, "executed", "reused"}:
            raise JobRehearsalReceiptError(f"{job_id} disposition 非法")
        if current.get("status") == "failed" and not isinstance(
            current.get("error"), str
        ):
            raise JobRehearsalReceiptError(f"{job_id} 失败缺少 error")
        if "duration_seconds" in current:
            duration = current.get("duration_seconds")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or duration < 0
            ):
                raise JobRehearsalReceiptError(
                    f"{job_id} duration_seconds 非法"
                )
        disposition = current.get("disposition")
        if disposition is not None and disposition not in {"executed", "reused"}:
            raise JobRehearsalReceiptError(f"{job_id} disposition 非法")
        if disposition == "reused":
            source = current.get("source_receipt")
            if (
                current.get("status") != "passed"
                or not isinstance(source, Mapping)
                or set(source) != {"path", "sha256", "bytes"}
                or not isinstance(source.get("path"), str)
                or not SHA256_RE.fullmatch(str(source.get("sha256", "")))
                or not isinstance(source.get("bytes"), int)
                or isinstance(source.get("bytes"), bool)
                or source.get("bytes") <= 0
            ):
                raise JobRehearsalReceiptError(f"{job_id} 复用来源非法")
        elif "source_receipt" in current and current.get("source_receipt") is not None:
            raise JobRehearsalReceiptError(f"{job_id} 非复用结果不得携带来源收据")
        components = current.get("tool_components")
        component_digests = current.get("tool_component_digests")
        if components is not None:
            if not isinstance(components, list) or any(
                not isinstance(value, str) for value in components
            ) or components != sorted(set(components)):
                raise JobRehearsalReceiptError(f"{job_id} 组件依赖非法")
            if not isinstance(component_digests, dict) or set(component_digests) != set(
                components
            ) or any(
                not SHA256_RE.fullmatch(str(value))
                for value in component_digests.values()
            ):
                raise JobRehearsalReceiptError(f"{job_id} 组件摘要非法")
            if not SHA256_RE.fullmatch(str(current.get("dependency_sha256", ""))):
                raise JobRehearsalReceiptError(f"{job_id} 依赖摘要非法")
            if not SHA256_RE.fullmatch(
                str(current.get("incremental_result_key", ""))
            ):
                raise JobRehearsalReceiptError(f"{job_id} 增量结果键非法")
            for field in ("input_sha256", "environment_sha256"):
                if not SHA256_RE.fullmatch(str(current.get(field, ""))):
                    raise JobRehearsalReceiptError(f"{job_id} {field}非法")
        for step_index, step in enumerate(current["steps"], 1):
            if (
                not isinstance(step, dict)
                or set(step)
                != {
                    "index",
                    "status",
                    "launcher",
                    "argv_sha256",
                    "environment_sha256",
                    "timeout_seconds",
                    "path_checks",
                    "dependencies",
                }
                or step.get("index") != step_index
                or step.get("status") != "passed"
                or step.get("launcher") not in {"host", "capture_container"}
                or not SHA256_RE.fullmatch(str(step.get("argv_sha256", "")))
                or not SHA256_RE.fullmatch(str(step.get("environment_sha256", "")))
                or not isinstance(step.get("timeout_seconds"), int)
                or step["timeout_seconds"] <= 0
                or not isinstance(step.get("path_checks"), list)
                or not step["path_checks"]
                or not isinstance(step.get("dependencies"), list)
                or not step["dependencies"]
            ):
                raise JobRehearsalReceiptError(f"{job_id} 步骤演练不完整")
        expected_c2pa = contract["c2pa_job_identities"].get(job_id)
        if current.get("c2pa_identity") != expected_c2pa:
            raise JobRehearsalReceiptError(f"{job_id} C2PA 身份不一致")
        seen.append(job_id)
    if sorted(seen) != contract["job_ids"] or len(seen) != len(set(seen)):
        raise JobRehearsalReceiptError("Job 演练结果遗漏或重复")
    summary_required = {
        "job_count",
        "passed_job_count",
        "phase_counts",
        "job_set_sha256",
        "live_requests_sent",
        "status",
    }
    summary_optional = {
        "executed_job_ids",
        "reused_job_ids",
        "affected_job_ids",
        "failed_job_ids",
    }
    raw_summary = facts.get("summary")
    if (
        not isinstance(raw_summary, Mapping)
        or not summary_required.issubset(raw_summary)
        or not set(raw_summary).issubset(summary_required | summary_optional)
    ):
        raise JobRehearsalReceiptError("summary字段不闭合")
    summary = dict(raw_summary)
    expected_job_set_sha = _fingerprint(
        [
            {
                "id": item["id"],
                "phase": item["phase"],
                "job_contract_sha256": item["job_contract_sha256"],
            }
            for item in jobs
        ]
    )
    passed_count = sum(item.get("status") == "passed" for item in jobs)
    expected_status = "passed" if passed_count == contract["job_count"] else "failed"
    if (
        summary.get("status") != expected_status
        or summary.get("live_requests_sent") is not False
        or summary.get("job_count") != contract["job_count"]
        or summary.get("passed_job_count") != passed_count
        or summary.get("phase_counts") != contract["phase_counts"]
        or summary.get("job_set_sha256") != expected_job_set_sha
    ):
        raise JobRehearsalReceiptError("完整 Job 演练汇总未通过")
    for field in ("executed_job_ids", "reused_job_ids", "affected_job_ids", "failed_job_ids"):
        if field in summary:
            values = summary[field]
            if (
                not isinstance(values, list)
                or not all(isinstance(value, str) and value in set(seen) for value in values)
                or values != sorted(set(values))
            ):
                raise JobRehearsalReceiptError(f"summary.{field}非法")
    if "failed_job_ids" in summary and set(summary["failed_job_ids"]) != {
        str(item["id"]) for item in jobs if item.get("status") == "failed"
    }:
        raise JobRehearsalReceiptError("summary.failed_job_ids与Job结果不一致")
    if "reused_job_ids" in summary and any(
        item.get("disposition") != "reused"
        for item in jobs
        if item.get("id") in set(summary["reused_job_ids"])
    ):
        raise JobRehearsalReceiptError("summary.reused_job_ids与Job结果不一致")
    incremental_plan = facts.get("incremental_plan")
    if incremental_plan is not None:
        _validate_incremental_plan(
            incremental_plan,
            {str(item["id"]) for item in jobs},
        )
    checkpoint = facts.get("checkpoint")
    if checkpoint is not None:
        if (
            not isinstance(checkpoint, Mapping)
            or set(checkpoint)
            != {"path", "record_count", "last_sequence", "last_sha256"}
            or not isinstance(checkpoint.get("path"), str)
            or not checkpoint["path"].startswith("/")
            or not isinstance(checkpoint.get("record_count"), int)
            or isinstance(checkpoint.get("record_count"), bool)
            or checkpoint["record_count"] < 1
            or checkpoint.get("last_sequence") != checkpoint["record_count"]
            or not SHA256_RE.fullmatch(str(checkpoint.get("last_sha256", "")))
        ):
            raise JobRehearsalReceiptError("checkpoint摘要非法")
    collector = _expect(
        facts.get("collector"),
        {"schema_version", "tool", "tool_sha256", "version"},
        "collector",
    )
    if not allow_collector_drift and collector != _producer():
        raise JobRehearsalReceiptError("Job 演练采集器身份漂移")
    runtime_sha = _runtime_identity(facts)
    if facts.get("runtime_identity_sha256") != runtime_sha:
        raise JobRehearsalReceiptError("Job 演练运行时身份摘要漂移")
    return {
        "execution_contract_sha256": contract_sha,
        "runtime_identity_sha256": runtime_sha,
        "job_count": contract["job_count"],
        "job_set_sha256": expected_job_set_sha,
        "status": expected_status,
        "passed_job_count": passed_count,
        "storage_probe_sha256": storage_probe_sha256,
        "failure_lifecycle_probe_sha256": failure_lifecycle_probe_sha256,
        "failed_job_ids": [
            str(item["id"]) for item in jobs if item.get("status") == "failed"
        ],
    }


def build_receipt(
    root: Path,
    facts_relative: str,
    *,
    allow_collector_drift: bool = False,
) -> dict[str, Any]:
    root = _private_root(root)
    facts_path = _relative(root, facts_relative, "facts")
    facts, raw = _load_json(facts_path, "facts")
    validated = validate_facts(
        facts,
        allow_collector_drift=allow_collector_drift,
    )
    campaign = facts["preflight_campaign"]
    if validated["status"] == INCREMENTAL_NOOP_STATUS:
        # no-op 收据保留当前计划和原有 passed 收据绑定，但明确不能作为
        # Formal 的完整 Job 演练证明。
        noop = facts.get("incremental_noop")
        if not isinstance(noop, Mapping):
            raise JobRehearsalReceiptError("incremental-noop facts 缺少事实标记")
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "status": INCREMENTAL_NOOP_STATUS,
            "observed_at_utc": facts["observed_at_utc"],
            "preflight_campaign": campaign,
            "execution_contract": facts["execution_contract"],
            "execution_contract_sha256": validated["execution_contract_sha256"],
            "runtime_identity_sha256": None,
            "job_count": validated["job_count"],
            "job_set_sha256": validated["job_set_sha256"],
            "facts": {
                "path": facts_relative,
                "sha256": _sha256_bytes(raw),
                "bytes": len(raw),
            },
            "producer": facts["collector"] if allow_collector_drift else _producer(),
            "failed_job_ids": [],
            "executed_job_ids": [],
            "reused_job_ids": list(noop["reused_job_ids"]),
            "affected_job_ids": [],
            "incremental_plan": dict(facts["incremental_plan"]),
            "incremental_noop": dict(noop),
        }
        return receipt
    summary = facts.get("summary", {})
    jobs = facts.get("jobs", [])
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": validated["status"],
        "observed_at_utc": facts["observed_at_utc"],
        "preflight_campaign": campaign,
        "execution_contract": facts["execution_contract"],
        "execution_contract_sha256": validated["execution_contract_sha256"],
        "runtime_identity_sha256": validated["runtime_identity_sha256"],
        "job_count": validated["job_count"],
        "job_set_sha256": validated["job_set_sha256"],
        "facts": {
            "path": facts_relative,
            "sha256": _sha256_bytes(raw),
            "bytes": len(raw),
        },
        "producer": facts["collector"] if allow_collector_drift else _producer(),
    }
    storage_probe_sha256 = validated.get("storage_probe_sha256")
    if storage_probe_sha256 is not None:
        receipt["storage_probe_sha256"] = storage_probe_sha256
    failure_lifecycle_probe_sha256 = validated.get(
        "failure_lifecycle_probe_sha256"
    )
    if failure_lifecycle_probe_sha256 is not None:
        receipt["failure_lifecycle_probe_sha256"] = (
            failure_lifecycle_probe_sha256
        )
    # 失败／增量字段保持可选，旧 v1 收据仍能按原结构重放。
    if validated["status"] != "passed":
        receipt["failed_job_ids"] = validated["failed_job_ids"]
    if isinstance(summary, Mapping):
        for field in ("executed_job_ids", "reused_job_ids", "affected_job_ids"):
            value = summary.get(field)
            if isinstance(value, list):
                receipt[field] = [str(item) for item in value]
    if isinstance(facts.get("incremental_plan"), Mapping):
        receipt["incremental_plan"] = dict(facts["incremental_plan"])
    if isinstance(facts.get("checkpoint"), Mapping):
        receipt["checkpoint"] = dict(facts["checkpoint"])
    return receipt


def collect(
    root: Path,
    output_relative: str,
    *,
    campaign_dir: Path,
    previous_receipt: str | None = None,
    previous_receipt_root: Path | None = None,
    rerun_failed: bool = False,
    checkpoint_relative: str | None = None,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    root = _private_root(root)
    output = _relative(root, output_relative, "facts output")
    if rerun_failed and not previous_receipt:
        raise JobRehearsalReceiptError(
            "--rerun-failed 必须同时提供 --previous-receipt"
        )
    if previous_receipt_root is not None and not previous_receipt:
        raise JobRehearsalReceiptError(
            "--previous-receipt-root 必须同时提供 --previous-receipt"
        )
    checkpoint_root = (
        _relative(root, checkpoint_relative, "checkpoint root")
        if checkpoint_relative
        else root / "checkpoints"
    )
    facts = collect_facts(
        campaign_dir,
        previous_receipt=(Path(previous_receipt) if previous_receipt else None),
        previous_receipt_root=(previous_receipt_root or root),
        rerun_failed=rerun_failed,
        checkpoint_root=checkpoint_root,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    _write_once(output, facts)
    return facts


def _deadline_from_cli(arguments: argparse.Namespace) -> incremental_recovery.WallClockDeadline:
    """从 CLI 参数冻结一次完整演练的全阶段 deadline。"""

    budget = (
        DEFAULT_ATTEMPT_WALL_SECONDS
        if arguments.max_wall_seconds is None
        else arguments.max_wall_seconds
    )
    heartbeat_seconds = (
        DEFAULT_HEARTBEAT_SECONDS
        if arguments.heartbeat_seconds is None
        else arguments.heartbeat_seconds
    )
    if (
        isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget <= 0
        or budget > MAX_ATTEMPT_WALL_SECONDS
    ):
        raise JobRehearsalReceiptError(
            f"--max-wall-seconds 必须在 1～{MAX_ATTEMPT_WALL_SECONDS} 秒之间"
        )
    if (
        isinstance(heartbeat_seconds, bool)
        or not isinstance(heartbeat_seconds, int)
        or heartbeat_seconds <= 0
        or heartbeat_seconds > MAX_HEARTBEAT_SECONDS
    ):
        raise JobRehearsalReceiptError(
            f"--heartbeat-seconds 必须在 1～{MAX_HEARTBEAT_SECONDS} 秒之间"
        )
    try:
        deadline = incremental_recovery.WallClockDeadline(
            budget,
            label="job-rehearsal",
        )
    except incremental_recovery.IncrementalRecoveryError as error:
        raise JobRehearsalReceiptError(str(error)) from error
    deadline.heartbeat_seconds = heartbeat_seconds  # type: ignore[attr-defined]
    deadline.phase = "job-rehearsal"  # type: ignore[attr-defined]
    return deadline


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
        raise JobRehearsalReceiptError("receipt.schema_version 不匹配")
    facts = receipt.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("path"), str):
        raise JobRehearsalReceiptError("receipt.facts 缺失")
    expected = build_receipt(
        root,
        facts["path"],
        allow_collector_drift=True,
    )
    if _canonical(expected) != raw:
        raise JobRehearsalReceiptError("Job 演练收据重放结果不一致")
    return receipt


def assert_formal_compatible(
    receipt: Mapping[str, Any], expected_contract: Mapping[str, Any]
) -> None:
    """拒绝 Formal 使用不同 Job、工具、容器、二进制或运行镜像。"""

    if receipt.get("status") == INCREMENTAL_NOOP_STATUS:
        raise JobRehearsalReceiptError(
            "incremental-noop 不是完整 Job 演练通过事实，Formal 必须绑定原有 passed 收据"
        )
    validate_execution_contract(dict(expected_contract))
    actual = receipt.get("execution_contract")
    if not isinstance(actual, dict):
        raise JobRehearsalReceiptError("Job 演练收据缺少 execution_contract")
    validate_execution_contract(actual)
    if actual != dict(expected_contract):
        raise JobRehearsalReceiptError("Formal 执行合同与 ARM64 完整 Job 演练不一致")
    if (
        receipt.get("status") != "passed"
        or receipt.get("execution_contract_sha256")
        != execution_contract_sha256(dict(expected_contract))
        or receipt.get("job_count") != expected_contract.get("job_count")
        or not SHA256_RE.fullmatch(
            str(receipt.get("storage_probe_sha256", ""))
        )
        or not SHA256_RE.fullmatch(
            str(receipt.get("failure_lifecycle_probe_sha256", ""))
        )
    ):
        raise JobRehearsalReceiptError("Formal 所需完整 Job 演练收据未通过")


def assert_recovery_compatible(
    receipt: Mapping[str, Any], expected_contract: Mapping[str, Any]
) -> dict[str, Any]:
    """校验恢复用演练，并返回承载原始通过事实的收据。

    普通 Formal 仍只能直接绑定 ``passed`` 收据。评估工具恢复允许使用
    ``incremental-noop`` 证明当前 preflight、工具和 Job 合同无需重跑，但
    no-op 本身不产生新的通过事实；因此必须现场重放其 ``source_receipt``，
    并从该原始 ``passed`` 收据取得运行时身份。
    """

    if receipt.get("status") != INCREMENTAL_NOOP_STATUS:
        assert_formal_compatible(receipt, expected_contract)
        return dict(receipt)

    validate_execution_contract(dict(expected_contract))
    actual = receipt.get("execution_contract")
    if not isinstance(actual, dict):
        raise JobRehearsalReceiptError(
            "恢复 incremental-noop 缺少 execution_contract"
        )
    validate_execution_contract(actual)
    expected = dict(expected_contract)
    if (
        actual != expected
        or receipt.get("execution_contract_sha256")
        != execution_contract_sha256(expected)
        or receipt.get("job_count") != expected.get("job_count")
    ):
        raise JobRehearsalReceiptError(
            "恢复 incremental-noop 与当前执行合同不一致"
        )

    noop = receipt.get("incremental_noop")
    if not isinstance(noop, Mapping):
        raise JobRehearsalReceiptError("恢复 incremental-noop 缺少来源绑定")
    source_root_raw = noop.get("source_root")
    source_binding = noop.get("source_receipt")
    if (
        not isinstance(source_root_raw, str)
        or not source_root_raw.startswith("/")
        or not isinstance(source_binding, Mapping)
        or set(source_binding) != {"path", "sha256", "bytes"}
        or not isinstance(source_binding.get("path"), str)
    ):
        raise JobRehearsalReceiptError("恢复 incremental-noop 来源坐标非法")

    source_root = _private_root(Path(source_root_raw))
    source_relative = str(source_binding["path"])
    source_path = _relative(source_root, source_relative, "原始通过收据")
    try:
        source_raw = source_path.read_bytes()
    except OSError as error:
        raise JobRehearsalReceiptError("恢复 incremental-noop 原始通过收据不存在") from error
    if (
        _sha256_bytes(source_raw) != source_binding.get("sha256")
        or len(source_raw) != source_binding.get("bytes")
    ):
        raise JobRehearsalReceiptError("恢复 incremental-noop 原始通过收据漂移")

    source = replay(source_root, source_relative)
    source_contract = source.get("execution_contract")
    if not isinstance(source_contract, dict):
        raise JobRehearsalReceiptError("原始通过收据缺少执行合同")
    validate_execution_contract(source_contract)
    runtime_identity = source.get("runtime_identity_sha256")
    if (
        source.get("status") != "passed"
        or _contract_reuse_identity(source_contract)
        != _contract_reuse_identity(expected)
        or source.get("job_count") != receipt.get("job_count")
        or source.get("job_set_sha256") != receipt.get("job_set_sha256")
        or source.get("job_count") != noop.get("source_job_count")
        or source.get("job_set_sha256") != noop.get("source_job_set_sha256")
        or not SHA256_RE.fullmatch(str(runtime_identity or ""))
        or not SHA256_RE.fullmatch(
            str(source.get("storage_probe_sha256", ""))
        )
        or not SHA256_RE.fullmatch(
            str(source.get("failure_lifecycle_probe_sha256", ""))
        )
    ):
        raise JobRehearsalReceiptError(
            "恢复 incremental-noop 的原始通过事实不完整或不兼容"
        )
    return dict(source)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="在 ARM64 离线演练全部 Job")
    collect_parser.add_argument("--campaign-dir", type=Path, required=True)
    collect_parser.add_argument("--evidence-root", type=Path, required=True)
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument(
        "--previous-receipt",
        help="前一轮演练收据（相对 evidence-root），用于只重跑失败／受影响 Job",
    )
    collect_parser.add_argument(
        "--previous-receipt-root",
        type=Path,
        help="前一轮演练收据的原始 0700 evidence root；跨 Campaign 复用时必须提供。",
    )
    collect_parser.add_argument(
        "--rerun-failed",
        action="store_true",
        help="显式启用失败项定向重跑；必须同时提供 --previous-receipt",
    )
    collect_parser.add_argument(
        "--checkpoint-root",
        help="checkpoint 目录（相对 evidence-root，默认 checkpoints）",
    )
    collect_parser.add_argument(
        "--max-wall-seconds",
        type=int,
        default=None,
        help=(
            f"一次完整演练的单调墙钟预算（默认 {DEFAULT_ATTEMPT_WALL_SECONDS} 秒，"
            f"上限 {MAX_ATTEMPT_WALL_SECONDS} 秒）"
        ),
    )
    collect_parser.add_argument(
        "--heartbeat-seconds",
        type=int,
        default=None,
        help=(
            f"watchdog heartbeat 间隔（默认 {DEFAULT_HEARTBEAT_SECONDS} 秒，"
            f"上限 {MAX_HEARTBEAT_SECONDS} 秒）"
        ),
    )
    finalize_parser = commands.add_parser("finalize", help="封存完整 Job 演练收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放完整 Job 演练收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    worker_parser = commands.add_parser(
        "failure-lifecycle-worker",
        help=argparse.SUPPRESS,
    )
    worker_parser.add_argument("--campaign-dir", type=Path, required=True)
    worker_parser.add_argument("--campaign-id", required=True)
    worker_parser.add_argument("--capture-container", required=True)
    worker_parser.add_argument("--capture-root", required=True)
    worker_parser.add_argument("--host-runs-root", type=Path, required=True)
    worker_parser.add_argument("--source-name", required=True)
    worker_parser.add_argument("--log-root", type=Path, required=True)
    worker_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "collect":
            deadline = _deadline_from_cli(arguments)
            result = collect(
                arguments.evidence_root,
                arguments.output,
                campaign_dir=arguments.campaign_dir,
                previous_receipt=arguments.previous_receipt,
                previous_receipt_root=arguments.previous_receipt_root,
                rerun_failed=arguments.rerun_failed,
                checkpoint_relative=arguments.checkpoint_root,
                deadline=deadline,
            )
        elif arguments.command == "finalize":
            result = finalize(
                arguments.evidence_root, arguments.facts, arguments.output
            )
        elif arguments.command == "replay":
            result = replay(arguments.evidence_root, arguments.receipt)
        else:
            try:
                result = _failure_lifecycle_worker(arguments)
            except Exception as error:
                # 该 worker 是 campaign-run 的隐藏动作；让父监督器取得有界、
                # 脱敏的真实异常类型，而不是只有一个无上下文退出码。
                try:
                    from tools.official_client_capture import codex_upgrade_supervisor

                    codex_upgrade_supervisor.write_campaign_run_action_diagnostic(
                        failure_kind="handled-error",
                        error=error,
                    )
                except Exception:
                    pass
                raise JobRehearsalReceiptError(
                    f"失败生命周期 worker 未通过：{type(error).__name__}: {error}"
                ) from error
    except (
        OSError,
        JobRehearsalReceiptError,
        incremental_recovery.IncrementalRecoveryError,
    ) as error:
        print(f"Codex 完整 Job 离线演练失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": result.get("status", "collected"),
                "job_count": (
                    result.get("job_count")
                    or result.get("summary", {}).get("job_count")
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
