#!/usr/bin/env python3
"""生成并重放 Codex VC-0 的 campaign-run 分批演练收据。

本工具只执行两个无网络、无业务副作用的合成动作，用于证明
``campaign-run v2`` 的批次序号、原始 deadline 承接、execute/reuse
分离和父监督器终态。第三个批次故意漂移 deadline，必须在动作前被拒绝。
工具不创建 Formal Campaign，不调用 Codex CLI，不发送任何官方请求。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_evidence_permissions
from tools.official_client_capture import codex_upgrade_predispatch_stop
from tools.official_client_capture import codex_upgrade_project_ledger
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts


RECEIPT_SCHEMA = "codex-p0-campaign-run-rehearsal/v1"
EXECUTIONS_SCHEMA = "codex-p0-campaign-run-executions/v1"
ACTION_RESULT_SCHEMA = "codex-p0-campaign-run-action-result/v1"
ATOMIC_RECEIPT_SCHEMA = "codex-atomic-vc0-vc1-rehearsal/v2"
MAX_JSON_BYTES = 16 * 1024 * 1024
MINIMUM_REMAINING_SECONDS = 120
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CampaignRunRehearsalError(RuntimeError):
    """campaign-run 演练输入、执行或收据未闭合。"""


@dataclass(frozen=True)
class PreflightInputs:
    """分批演练所需的不可变 preflight 输入。"""

    campaign_dir: Path
    manifest: dict[str, Any]
    campaign_plan: dict[str, Any]
    campaign_plan_path: Path
    vc0_checkpoint: dict[str, Any]
    vc0_checkpoint_path: Path
    supervisor_sha256: str
    producer_sha256: str


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise CampaignRunRehearsalError(f"{label}不是安全标识")
    return value


def _private_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise CampaignRunRehearsalError(f"{label}必须是可信绝对目录")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CampaignRunRehearsalError(f"{label}权限不得允许 group/other 访问")
    return resolved


def _mount_directory(path: Path, label: str) -> Path:
    """校验生产挂载点本身；挂载点无需具备证据目录的 0700 权限。"""

    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise CampaignRunRehearsalError(f"{label}必须是非符号链接绝对目录")
    resolved = path.resolve(strict=True)
    if resolved.stat().st_uid != os.geteuid():
        raise CampaignRunRehearsalError(f"{label}必须由当前用户拥有")
    return resolved


def _relative_file(root: Path, value: str | Path, label: str) -> Path:
    text = str(value)
    parsed = PurePosixPath(text)
    if (
        not text
        or parsed.is_absolute()
        or str(parsed) != text
        or "\\" in text
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise CampaignRunRehearsalError(f"{label}不是规范相对路径")
    candidate = root
    for part in parsed.parts:
        candidate /= part
        if candidate.is_symlink():
            raise CampaignRunRehearsalError(f"{label}包含符号链接")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CampaignRunRehearsalError(f"{label}越过受管根") from error
    if not resolved.is_file() or resolved.is_symlink():
        raise CampaignRunRehearsalError(f"{label}不是可信普通文件")
    return resolved


def _new_output(root: Path, value: str | Path, label: str) -> Path:
    text = str(value)
    parsed = PurePosixPath(text)
    if (
        not text
        or parsed.is_absolute()
        or str(parsed) != text
        or "\\" in text
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise CampaignRunRehearsalError(f"{label}不是规范相对路径")
    candidate = root / Path(*parsed.parts)
    if candidate.parent != root or candidate.exists() or candidate.is_symlink():
        raise CampaignRunRehearsalError(f"{label}必须是根目录下尚不存在的普通文件")
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CampaignRunRehearsalError(f"{label}不是可信普通文件")
    size = path.stat().st_size
    if not 1 <= size <= MAX_JSON_BYTES:
        raise CampaignRunRehearsalError(f"{label}大小非法")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CampaignRunRehearsalError(f"{label}不是有效 JSON") from error
    if not isinstance(value, dict):
        raise CampaignRunRehearsalError(f"{label}必须是 JSON 对象")
    return value


def _expect_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise CampaignRunRehearsalError(f"{label}字段不闭合")
    return dict(value)


def _private_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CampaignRunRehearsalError(f"{label}不是可信绝对普通文件")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise CampaignRunRehearsalError(f"{label}权限不得允许 group/other 访问")
    return resolved


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise CampaignRunRehearsalError(f"输出已存在，禁止覆盖：{path}")
    parent = _private_directory(path.parent, "输出父目录")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise CampaignRunRehearsalError(
                f"输出已存在，禁止覆盖：{path}"
            ) from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_file(
    root: Path,
    binding: Any,
    label: str,
) -> Path:
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256"}:
        raise CampaignRunRehearsalError(f"{label}绑定字段不闭合")
    path = _relative_file(root, str(binding["path"]), label)
    expected = binding["sha256"]
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise CampaignRunRehearsalError(f"{label} SHA-256 非法")
    if _sha256_file(path) != expected:
        raise CampaignRunRehearsalError(f"{label}摘要漂移")
    return path


def _tool_sha256(manifest: Mapping[str, Any], relative: str) -> str:
    """读取 preflight 冻结的单个工具字节身份。"""

    identity = manifest.get("tool_identity")
    entries = identity.get("entries") if isinstance(identity, Mapping) else None
    matches = [
        item
        for item in entries or []
        if isinstance(item, Mapping)
        and item.get("path") == relative
    ]
    if len(matches) != 1:
        raise CampaignRunRehearsalError(
            f"preflight 工具清单缺少唯一 {relative}"
        )
    value = matches[0].get("sha256")
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CampaignRunRehearsalError(f"preflight {relative} 摘要非法")
    return value


def _load_preflight_inputs(campaign_dir: Path) -> PreflightInputs:
    root = _private_directory(campaign_dir, "preflight Campaign")
    try:
        manifest = codex_upgrade.load_campaign_manifest(
            root,
            _skip_control_validation=True,
        )
    except (OSError, ValueError, codex_upgrade.ConfigurationError) as error:
        raise CampaignRunRehearsalError(
            f"preflight Campaign 重放失败：{error}"
        ) from error
    if manifest.get("campaign_mode") != "preflight_only":
        raise CampaignRunRehearsalError("分批演练只能消费 preflight_only Campaign")
    control = manifest.get("vc_control")
    if not isinstance(control, Mapping):
        raise CampaignRunRehearsalError("preflight 缺少 VC 控制制品")
    plan_path = _manifest_file(root, control.get("campaign_plan"), "Campaign plan")
    checkpoint_path = _manifest_file(
        root,
        control.get("vc0_checkpoint"),
        "VC-0 checkpoint",
    )
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(
            _load_json(plan_path, "Campaign plan")
        )
        checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(
            _load_json(checkpoint_path, "VC-0 checkpoint"),
            plan,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError(f"preflight VC 制品非法：{error}") from error
    if (
        plan.get("campaign_id") != manifest.get("campaign_id")
        or plan.get("campaign_mode") != "preflight_only"
        or checkpoint.get("phase") != "VC-0"
        or checkpoint.get("status") != "complete"
        or checkpoint.get("metrics", {}).get("live_request_count") != 0
        or checkpoint.get("metrics", {}).get("scanned_bytes") != 0
    ):
        raise CampaignRunRehearsalError("preflight Campaign plan 或 VC-0 checkpoint 身份漂移")
    try:
        deadline = datetime.fromisoformat(
            str(plan["original_deadline_at_utc"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as error:
        raise CampaignRunRehearsalError("Campaign 原始 deadline 非法") from error
    remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining < MINIMUM_REMAINING_SECONDS:
        raise CampaignRunRehearsalError(
            f"Campaign 原始 deadline 仅剩 {remaining} 秒，不足以执行分批演练"
        )
    supervisor_sha256 = _tool_sha256(
        manifest,
        "codex_upgrade_supervisor.py",
    )
    producer_sha256 = _tool_sha256(manifest, Path(__file__).name)
    if (
        supervisor_sha256
        != _sha256_file(Path(codex_upgrade_supervisor.__file__).resolve())
        or producer_sha256 != _sha256_file(Path(__file__).resolve())
    ):
        raise CampaignRunRehearsalError(
            "preflight 冻结工具与当前 campaign-run 演练执行字节不一致"
        )
    return PreflightInputs(
        campaign_dir=root,
        manifest=manifest,
        campaign_plan=plan,
        campaign_plan_path=plan_path,
        vc0_checkpoint=checkpoint,
        vc0_checkpoint_path=checkpoint_path,
        supervisor_sha256=supervisor_sha256,
        producer_sha256=producer_sha256,
    )


def _action_command(
    *,
    campaign_id: str,
    phase: str,
    action_id: str,
    output: Path,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "action-worker",
        "--campaign-id",
        campaign_id,
        "--phase",
        phase,
        "--action-id",
        action_id,
        "--output",
        str(output),
    ]


def _run_campaign(state_dir: Path, manifest_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )


def _action_worker(arguments: argparse.Namespace) -> dict[str, Any]:
    """在父 campaign-run 下生成一份零请求动作事实。"""

    campaign_id = _safe_id(arguments.campaign_id, "campaign_id")
    phase = _safe_id(arguments.phase, "phase")
    action_id = _safe_id(arguments.action_id, "action_id")
    run_dir_text = os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_DIR_ENV)
    if not run_dir_text:
        raise CampaignRunRehearsalError("action-worker 必须由 campaign-run 派发")
    run_dir = _private_directory(Path(run_dir_text), "父监督器 run_dir")
    output = Path(arguments.output)
    if (
        not output.is_absolute()
        or output.exists()
        or output.is_symlink()
        or output.parent.resolve(strict=True) != run_dir.parent.resolve(strict=True)
    ):
        raise CampaignRunRehearsalError("action-worker 输出越过演练根或已存在")
    if (
        os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_ID_ENV) != campaign_id
        or os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_PHASE_ENV) != phase
        or os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_ACTION_ID_ENV)
        != action_id
    ):
        raise CampaignRunRehearsalError("action-worker 父监督器身份漂移")
    client = codex_upgrade_supervisor.SupervisorClient.attach_from_environment()
    if client is None or not client.attached:
        raise CampaignRunRehearsalError("action-worker 未附着到父监督器")
    operation = "p0:campaign-run-rehearsal-child"
    client.event_start(operation)
    payload = {
        "schema_version": ACTION_RESULT_SCHEMA,
        "status": "passed",
        "campaign_id": campaign_id,
        "phase": phase,
        "action_id": action_id,
        "parent_run_dir": str(run_dir),
        "network_used": False,
        "live_request_count": 0,
    }
    _write_once(output, payload)
    client.event_end(operation, metadata={"live_request_count": 0})
    return payload


def _run_summary(
    result: Mapping[str, Any],
    *,
    expected_campaign_id: str,
    expected_batch_id: str,
    expected_sequence: int,
    expected_deadline: str,
    expected_action_id: str,
    expected_execute: str,
    expected_reuse: str,
    marker_path: Path,
) -> dict[str, Any]:
    run_dir = _private_directory(Path(str(result.get("run_dir", ""))), "rehearsal run")
    try:
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except (OSError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise CampaignRunRehearsalError(f"rehearsal run 审计失败：{error}") from error
    marker = _load_json(marker_path, "rehearsal action marker")
    expected_marker = {
        "schema_version": ACTION_RESULT_SCHEMA,
        "status": "passed",
        "campaign_id": expected_campaign_id,
        "phase": f"VC-{expected_sequence}",
        "action_id": expected_action_id,
        "parent_run_dir": str(run_dir),
        "network_used": False,
        "live_request_count": 0,
    }
    if (
        result.get("campaign_id") != expected_campaign_id
        or result.get("batch_id") != expected_batch_id
        or result.get("batch_sequence") != expected_sequence
        or result.get("original_deadline_at_utc") != expected_deadline
        or result.get("status") != "stopped"
        or result.get("reason") != "queue-complete"
        or result.get("actions")
        != [{"action_id": expected_action_id, "returncode": 0, "status": "passed"}]
        or result.get("execute_items") != [expected_execute]
        or result.get("reuse_items") != [expected_reuse]
        or marker != expected_marker
        or audit.get("state") != "stopped"
        or audit.get("audit_incomplete") is not False
    ):
        raise CampaignRunRehearsalError("campaign-run 批次未形成完整零请求终态")
    return {
        "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
        "batch_id": expected_batch_id,
        "batch_sequence": expected_sequence,
        "original_deadline_at_utc": expected_deadline,
        "state": audit["state"],
        "audit_incomplete": audit["audit_incomplete"],
        "event_count": audit["event_count"],
        "execute_items": [expected_execute],
        "reuse_items": [expected_reuse],
        "run_dir": str(run_dir),
    }


def collect(
    evidence_root: Path,
    output_relative: str | Path,
    *,
    campaign_dir: Path,
) -> dict[str, Any]:
    """执行两个成功批次和一个 deadline 漂移负例。"""

    root = _private_directory(evidence_root, "campaign-run rehearsal 根")
    if any(root.iterdir()):
        raise CampaignRunRehearsalError("新 campaign-run rehearsal 根必须为空")
    output = _new_output(root, output_relative, "rehearsal receipt 输出")
    inputs = _load_preflight_inputs(campaign_dir)
    campaign_id = _safe_id(inputs.manifest.get("campaign_id"), "campaign_id")
    plan_sha256 = str(inputs.campaign_plan["plan_sha256"])
    original_deadline = str(inputs.campaign_plan["original_deadline_at_utc"])
    original_deadline_value = datetime.fromisoformat(
        original_deadline.replace("Z", "+00:00")
    )
    predecessor = {
        "path": str(inputs.vc0_checkpoint_path),
        "sha256": _sha256_file(inputs.vc0_checkpoint_path),
        "phase": "VC-0",
        "checkpoint_sha256": str(inputs.vc0_checkpoint["checkpoint_sha256"]),
    }
    runs: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    for sequence in (1, 2):
        phase = f"VC-{sequence}"
        action_id = f"p0-rehearsal-action-{sequence}"
        execute_id = f"p0-fixture-execute-{sequence}"
        reuse_id = f"p0-fixture-reuse-{sequence}"
        marker_path = root / f"action-{sequence}.json"
        action = {
            "action_id": action_id,
            "operation": f"{phase}:p0-campaign-run-rehearsal",
            "timeout_seconds": 30.0,
            "command": _action_command(
                campaign_id=campaign_id,
                phase=phase,
                action_id=action_id,
                output=marker_path,
            ),
            "item_ids": [execute_id],
        }
        compiled_at = datetime.now(timezone.utc)
        must_start_by = min(
            compiled_at + timedelta(seconds=30),
            original_deadline_value,
        )
        try:
            batch = codex_upgrade_vc_artifacts.build_vc_batch(
                campaign_plan=inputs.campaign_plan,
                phase=phase,
                sequence=sequence,
                predecessor_checkpoint=predecessor,
                execute_item_ids=[execute_id],
                reuse_item_ids=[reuse_id],
                actions=[action],
                compiled_at_utc=compiled_at.isoformat(),
                must_start_by_utc=must_start_by.isoformat(),
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise CampaignRunRehearsalError(
                f"无法编译 rehearsal VC batch {sequence}：{error}"
            ) from error
        batch_path = root / f"vc-batch-{sequence}.json"
        _write_once(batch_path, batch)
        manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=campaign_id,
            campaign_plan_sha256=plan_sha256,
            batch_id=str(batch["batch_id"]),
            batch_sequence=int(batch["sequence"]),
            batch_sha256=str(batch["batch_sha256"]),
            phase=phase,
            predecessor_checkpoint=predecessor,
            original_deadline_at_utc=original_deadline,
            actions=[action],
            execute_items=[execute_id],
            reuse_items=[reuse_id],
        )
        manifest_path = root / f"batch-{sequence}.json"
        _write_once(manifest_path, manifest)
        completed = _run_campaign(root, manifest_path)
        execution = {
            "batch": batch_path.name,
            "manifest": manifest_path.name,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        executions.append(execution)
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or "子进程没有输出"
            )
            raise CampaignRunRehearsalError(
                f"campaign-run 批次 {sequence} 失败："
                f"returncode={completed.returncode}；{detail}"
            )
        try:
            result = json.loads(completed.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CampaignRunRehearsalError(
                f"campaign-run 批次 {sequence} 输出非法"
            ) from error
        if not isinstance(result, Mapping):
            raise CampaignRunRehearsalError(
                f"campaign-run 批次 {sequence} 输出必须是对象"
            )
        runs.append(
            _run_summary(
                result,
                expected_campaign_id=campaign_id,
                expected_batch_id=str(batch["batch_id"]),
                expected_sequence=sequence,
                expected_deadline=original_deadline,
                expected_action_id=action_id,
                expected_execute=execute_id,
                expected_reuse=reuse_id,
                marker_path=marker_path,
            )
        )
        try:
            checkpoint = codex_upgrade_vc_artifacts.build_vc_checkpoint(
                campaign_plan=inputs.campaign_plan,
                phase=phase,
                status="complete",
                predecessor_checkpoint=predecessor,
                stage_receipt={
                    "path": marker_path.name,
                    "sha256": _sha256_file(marker_path),
                },
                completed_at_utc=datetime.now(timezone.utc).isoformat(),
                execute_item_ids=[execute_id],
                reuse_item_ids=[reuse_id],
                live_request_count=0,
                scanned_bytes=0,
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise CampaignRunRehearsalError(
                f"无法封存 rehearsal VC-{sequence} checkpoint：{error}"
            ) from error
        checkpoint_path = root / f"vc-{sequence}-checkpoint.json"
        _write_once(checkpoint_path, checkpoint)
        predecessor = {
            "path": str(checkpoint_path),
            "sha256": _sha256_file(checkpoint_path),
            "phase": phase,
            "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
        }

    drifted_deadline = (original_deadline_value - timedelta(seconds=1)).isoformat()
    negative_marker = root / "must-not-run.json"
    negative_action_id = "must-not-run"
    negative_execute_id = "p0-fixture-execute-3"
    negative_reuse_id = "p0-fixture-reuse-3"
    negative_action = {
        "action_id": negative_action_id,
        "operation": "VC-3:must-not-run",
        "timeout_seconds": 30.0,
        "command": _action_command(
            campaign_id=campaign_id,
            phase="VC-3",
            action_id=negative_action_id,
            output=negative_marker,
        ),
        "item_ids": [negative_execute_id],
    }
    compiled_at = datetime.now(timezone.utc)
    must_start_by = min(
        compiled_at + timedelta(seconds=30),
        original_deadline_value,
    )
    try:
        negative_batch = codex_upgrade_vc_artifacts.build_vc_batch(
            campaign_plan=inputs.campaign_plan,
            phase="VC-3",
            sequence=3,
            predecessor_checkpoint=predecessor,
            execute_item_ids=[negative_execute_id],
            reuse_item_ids=[negative_reuse_id],
            actions=[negative_action],
            compiled_at_utc=compiled_at.isoformat(),
            must_start_by_utc=must_start_by.isoformat(),
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError(
            f"无法编译 deadline 负例的原始 VC batch：{error}"
        ) from error
    negative_batch_path = root / "vc-batch-3.json"
    _write_once(negative_batch_path, negative_batch)
    negative_manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
        campaign_id=campaign_id,
        campaign_plan_sha256=plan_sha256,
        batch_id=str(negative_batch["batch_id"]),
        batch_sequence=3,
        batch_sha256=str(negative_batch["batch_sha256"]),
        phase="VC-3",
        predecessor_checkpoint=predecessor,
        original_deadline_at_utc=drifted_deadline,
        actions=[negative_action],
        execute_items=[negative_execute_id],
        reuse_items=[negative_reuse_id],
    )
    negative_path = root / "batch-3-deadline-drift.json"
    _write_once(negative_path, negative_manifest)
    run_directories_before = sorted(
        item.name for item in root.glob("run-*") if item.is_dir()
    )
    negative = _run_campaign(root, negative_path)
    run_directories_after = sorted(
        item.name for item in root.glob("run-*") if item.is_dir()
    )
    executions.append(
        {
            "batch": negative_batch_path.name,
            "manifest": negative_path.name,
            "returncode": negative.returncode,
            "stdout": negative.stdout,
            "stderr": negative.stderr,
        }
    )
    if (
        negative.returncode == 0
        or run_directories_after != run_directories_before
        or negative_marker.exists()
        or negative_marker.is_symlink()
        or "原始 deadline" not in negative.stderr
    ):
        raise CampaignRunRehearsalError("deadline 漂移负例未在动作前失败关闭")
    _write_once(
        root / "batch-executions.json",
        {
            "schema_version": EXECUTIONS_SCHEMA,
            "tool_path": str(Path(codex_upgrade_supervisor.__file__).resolve()),
            "tool_sha256": inputs.supervisor_sha256,
            "producer_path": str(Path(__file__).resolve()),
            "producer_sha256": inputs.producer_sha256,
            "executions": executions,
        },
    )
    inventory = sorted(
        item.name for item in root.iterdir() if item != output
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "status": "passed",
        "campaign_id": campaign_id,
        "inputs": {
            "campaign_plan_sha256": plan_sha256,
            "preflight_campaign": str(inputs.campaign_dir),
            "tool_sha256": inputs.supervisor_sha256,
            "vc0_checkpoint_file_sha256": _sha256_file(
                inputs.vc0_checkpoint_path
            ),
        },
        "multi_batch_passed": True,
        "original_deadline_inherited": True,
        "deadline_drift_rejected": True,
        "live_request_count": 0,
        "runs": runs,
        "negative_fixture": {
            "kind": "original_deadline_drift",
            "rejected_before_action": True,
            "returncode": negative.returncode,
            "stderr": negative.stderr.strip(),
        },
        "temporary_asset_inventory": inventory,
    }
    _write_once(output, receipt)
    return replay(root, output.name, campaign_dir=inputs.campaign_dir)


def replay(
    evidence_root: Path,
    receipt_relative: str | Path,
    *,
    campaign_dir: Path,
) -> dict[str, Any]:
    """独立重放分批演练收据与两个父监督器终态。"""

    root = _private_directory(evidence_root, "campaign-run rehearsal 根")
    path = _relative_file(root, receipt_relative, "campaign-run rehearsal 收据")
    if path.parent != root:
        raise CampaignRunRehearsalError("campaign-run rehearsal 收据必须位于证据根")
    _private_file(path, "campaign-run rehearsal 收据")
    inputs = _load_preflight_inputs(campaign_dir)
    try:
        from tools.official_client_capture import codex_upgrade_vc0_closeout

        receipt = codex_upgrade_vc0_closeout._validate_campaign_run_rehearsal(
            path,
            inputs.campaign_dir,
            inputs.manifest,
        )
    except (
        OSError,
        ValueError,
        codex_upgrade_vc0_closeout.VC0CloseoutError,
    ) as error:
        raise CampaignRunRehearsalError(
            f"campaign-run rehearsal 收据重放失败：{error}"
        ) from error
    expected_inventory = sorted(
        item.name for item in root.iterdir() if item != path
    )
    if receipt.get("temporary_asset_inventory") != expected_inventory:
        raise CampaignRunRehearsalError("campaign-run rehearsal 临时资产 inventory 漂移")

    runs = sorted(receipt["runs"], key=lambda item: item["batch_sequence"])
    run_names: list[str] = []
    for sequence, run in enumerate(runs, 1):
        run_dir = _private_directory(
            Path(str(run["run_dir"])),
            f"campaign-run rehearsal run {sequence}",
        )
        if run_dir.parent != root:
            raise CampaignRunRehearsalError("campaign-run rehearsal run 越过证据根")
        run_names.append(run_dir.name)

    required_inventory = {
        codex_upgrade_supervisor.CAMPAIGN_RUN_LOCK_FILENAME,
        "action-1.json",
        "action-2.json",
        "batch-1.json",
        "batch-2.json",
        "batch-3-deadline-drift.json",
        "batch-executions.json",
        "vc-1-checkpoint.json",
        "vc-2-checkpoint.json",
        "vc-batch-1.json",
        "vc-batch-2.json",
        "vc-batch-3.json",
        *run_names,
    }
    if set(expected_inventory) != required_inventory:
        raise CampaignRunRehearsalError(
            "campaign-run rehearsal 临时资产集合缺项或含未登记对象"
        )
    for name in expected_inventory:
        asset = root / name
        if name in run_names:
            _private_directory(asset, f"campaign-run rehearsal 资产 {name}")
        else:
            _private_file(asset, f"campaign-run rehearsal 资产 {name}")

    executions_document = _expect_fields(
        _load_json(root / "batch-executions.json", "campaign-run 执行记录"),
        {
            "schema_version",
            "tool_path",
            "tool_sha256",
            "producer_path",
            "producer_sha256",
            "executions",
        },
        "campaign-run 执行记录",
    )
    executions = executions_document["executions"]
    if (
        executions_document["schema_version"] != EXECUTIONS_SCHEMA
        or executions_document["tool_path"]
        != str(Path(codex_upgrade_supervisor.__file__).resolve())
        or executions_document["tool_sha256"] != inputs.supervisor_sha256
        or executions_document["producer_path"] != str(Path(__file__).resolve())
        or executions_document["producer_sha256"] != inputs.producer_sha256
        or not isinstance(executions, list)
        or len(executions) != 3
    ):
        raise CampaignRunRehearsalError("campaign-run 执行记录工具身份或批次数非法")

    original_deadline = str(inputs.campaign_plan["original_deadline_at_utc"])
    predecessor = {
        "path": str(inputs.vc0_checkpoint_path),
        "sha256": _sha256_file(inputs.vc0_checkpoint_path),
        "phase": "VC-0",
        "checkpoint_sha256": str(inputs.vc0_checkpoint["checkpoint_sha256"]),
    }
    for sequence in (1, 2, 3):
        phase = f"VC-{sequence}"
        action_id = (
            f"p0-rehearsal-action-{sequence}"
            if sequence < 3
            else "must-not-run"
        )
        execute_id = f"p0-fixture-execute-{sequence}"
        reuse_id = f"p0-fixture-reuse-{sequence}"
        marker_path = (
            root / f"action-{sequence}.json"
            if sequence < 3
            else root / "must-not-run.json"
        )
        action = {
            "action_id": action_id,
            "operation": (
                f"{phase}:p0-campaign-run-rehearsal"
                if sequence < 3
                else "VC-3:must-not-run"
            ),
            "timeout_seconds": 30.0,
            "command": _action_command(
                campaign_id=str(receipt["campaign_id"]),
                phase=phase,
                action_id=action_id,
                output=marker_path,
            ),
            "item_ids": [execute_id],
        }
        batch_path = root / f"vc-batch-{sequence}.json"
        try:
            batch = codex_upgrade_vc_artifacts.validate_vc_batch(
                _load_json(batch_path, f"rehearsal VC batch {sequence}"),
                inputs.campaign_plan,
            )
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise CampaignRunRehearsalError(
                f"rehearsal VC batch {sequence} 重放失败：{error}"
            ) from error
        if (
            batch["sequence"] != sequence
            or batch["phase"] != phase
            or batch["predecessor_checkpoint"] != predecessor
            or batch["execute_item_ids"] != [execute_id]
            or batch["reuse_item_ids"] != [reuse_id]
            or batch["actions"] != [action]
            or batch["original_deadline_at_utc"] != original_deadline
        ):
            raise CampaignRunRehearsalError(
                f"rehearsal VC batch {sequence} 身份、集合或前序链漂移"
            )

        manifest_name = (
            f"batch-{sequence}.json"
            if sequence < 3
            else "batch-3-deadline-drift.json"
        )
        manifest_path = root / manifest_name
        try:
            manifest = codex_upgrade_supervisor._campaign_run_manifest(
                manifest_path
            )
        except codex_upgrade_supervisor.SupervisorError as error:
            raise CampaignRunRehearsalError(
                f"campaign-run manifest {sequence} 重放失败：{error}"
            ) from error
        expected_deadline = original_deadline
        if sequence == 3:
            expected_deadline = (
                datetime.fromisoformat(original_deadline.replace("Z", "+00:00"))
                - timedelta(seconds=1)
            ).isoformat()
        expected_manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=str(batch["campaign_id"]),
            campaign_plan_sha256=str(batch["campaign_plan_sha256"]),
            batch_id=str(batch["batch_id"]),
            batch_sequence=int(batch["sequence"]),
            batch_sha256=str(batch["batch_sha256"]),
            phase=str(batch["phase"]),
            predecessor_checkpoint=batch["predecessor_checkpoint"],
            original_deadline_at_utc=expected_deadline,
            actions=batch["actions"],
            execute_items=batch["execute_item_ids"],
            reuse_items=batch["reuse_item_ids"],
        )
        if manifest != expected_manifest:
            raise CampaignRunRehearsalError(
                f"campaign-run manifest {sequence} 未由对应 VC batch 精确编译"
            )

        execution = _expect_fields(
            executions[sequence - 1],
            {"batch", "manifest", "returncode", "stdout", "stderr"},
            f"campaign-run execution {sequence}",
        )
        if execution["batch"] != batch_path.name or execution["manifest"] != manifest_name:
            raise CampaignRunRehearsalError(
                f"campaign-run execution {sequence} 文件绑定漂移"
            )
        if sequence < 3:
            run = runs[sequence - 1]
            run_dir = Path(str(run["run_dir"]))
            marker = _load_json(marker_path, f"rehearsal action marker {sequence}")
            if marker != {
                "schema_version": ACTION_RESULT_SCHEMA,
                "status": "passed",
                "campaign_id": receipt["campaign_id"],
                "phase": phase,
                "action_id": action_id,
                "parent_run_dir": str(run_dir),
                "network_used": False,
                "live_request_count": 0,
            }:
                raise CampaignRunRehearsalError(
                    f"rehearsal action marker {sequence} 身份或零请求事实漂移"
                )
            recorded = _expect_fields(
                _load_json(
                    run_dir / "campaign-run-manifest.json",
                    f"run {sequence} 不可变 manifest",
                ),
                {"schema_version", "manifest_sha256", "manifest"},
                f"run {sequence} 不可变 manifest",
            )
            if (
                recorded["schema_version"]
                != codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA
                or recorded["manifest"] != manifest
                or recorded["manifest_sha256"] != _sha256_bytes(_canonical(manifest))
            ):
                raise CampaignRunRehearsalError(
                    f"run {sequence} 实际执行 manifest 摘要漂移"
                )
            try:
                stdout = json.loads(str(execution["stdout"]))
            except json.JSONDecodeError as error:
                raise CampaignRunRehearsalError(
                    f"campaign-run execution {sequence} stdout 非法"
                ) from error
            if (
                execution["returncode"] != 0
                or execution["stderr"] != ""
                or stdout.get("run_dir") != str(run_dir)
                or stdout.get("batch_sha256") != batch["batch_sha256"]
                or stdout.get("status") != "stopped"
                or stdout.get("reason") != "queue-complete"
            ):
                raise CampaignRunRehearsalError(
                    f"campaign-run execution {sequence} 结果漂移"
                )
            checkpoint_path = root / f"vc-{sequence}-checkpoint.json"
            try:
                checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(
                    _load_json(
                        checkpoint_path,
                        f"rehearsal VC-{sequence} checkpoint",
                    ),
                    inputs.campaign_plan,
                )
            except codex_upgrade_vc_artifacts.VCArtifactError as error:
                raise CampaignRunRehearsalError(
                    f"rehearsal VC-{sequence} checkpoint 重放失败：{error}"
                ) from error
            if (
                checkpoint["phase"] != phase
                or checkpoint["status"] != "complete"
                or checkpoint["predecessor_checkpoint"] != predecessor
                or checkpoint["stage_receipt"]
                != {"path": marker_path.name, "sha256": _sha256_file(marker_path)}
                or checkpoint["execute_item_ids"] != [execute_id]
                or checkpoint["reuse_item_ids"] != [reuse_id]
                or checkpoint["metrics"]
                != {"live_request_count": 0, "scanned_bytes": 0}
            ):
                raise CampaignRunRehearsalError(
                    f"rehearsal VC-{sequence} checkpoint 链或零请求事实漂移"
                )
            predecessor = {
                "path": str(checkpoint_path),
                "sha256": _sha256_file(checkpoint_path),
                "phase": phase,
                "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
            }
        elif (
            execution["returncode"] == 0
            or execution["stdout"] != ""
            or str(execution["stderr"]).strip()
            != receipt["negative_fixture"]["stderr"]
            or "原始 deadline" not in str(execution["stderr"])
        ):
            raise CampaignRunRehearsalError("deadline 漂移执行记录未失败关闭")

    if (root / "must-not-run.json").exists() or (
        root / "must-not-run.json"
    ).is_symlink():
        raise CampaignRunRehearsalError("deadline 负例动作曾被执行")
    return receipt


def _atomic_environment(*, require_arm64: bool) -> dict[str, Any]:
    """验证 capture-cli 的生产挂载形态；本地单测可显式关闭环境门禁。"""

    machine = os.uname().machine.lower()
    if not require_arm64:
        return {
            "enforced": False,
            "architecture": machine,
        }
    if machine not in {"aarch64", "arm64"}:
        raise CampaignRunRehearsalError("原子双跑只能在 ARM64 capture-cli 内执行")
    capture_root = _mount_directory(Path("/capture"), "capture 只读根")
    staging_root = _mount_directory(Path("/capture/staging"), "capture staging 根")
    logical_runs = _mount_directory(
        Path("/root/oauth-capture/runs"),
        "逻辑 runs 根",
    )
    writable_runs = _mount_directory(Path("/capture/runs"), "可写 runs 根")
    readonly_flag = getattr(os, "ST_RDONLY", 1)
    capture_readonly = bool(os.statvfs(capture_root).f_flag & readonly_flag)
    staging_readonly = bool(os.statvfs(staging_root).f_flag & readonly_flag)
    logical_metadata = logical_runs.stat()
    writable_metadata = writable_runs.stat()
    same_inode = (
        logical_metadata.st_dev,
        logical_metadata.st_ino,
    ) == (
        writable_metadata.st_dev,
        writable_metadata.st_ino,
    )
    logical_readonly = bool(os.statvfs(logical_runs).f_flag & readonly_flag)
    writable_readonly = bool(os.statvfs(writable_runs).f_flag & readonly_flag)
    if (
        not capture_readonly
        or staging_readonly
        or not same_inode
        or logical_readonly
        or writable_readonly
    ):
        raise CampaignRunRehearsalError(
            "capture/staging 或 runs 双别名挂载形态与生产合同不一致"
        )
    return {
        "enforced": True,
        "architecture": "aarch64",
        "capture_root": str(capture_root),
        "capture_root_readonly": True,
        "staging_root": str(staging_root),
        "staging_root_readonly": False,
        "logical_runs_root": str(logical_runs),
        "logical_runs_readonly": False,
        "writable_runs_root": str(writable_runs),
        "writable_runs_readonly": False,
        "runs_device": logical_metadata.st_dev,
        "runs_inode": logical_metadata.st_ino,
        "runs_same_inode": True,
    }


def _atomic_tool_identity() -> dict[str, Any]:
    """冻结原子入口及其真实 producer／consumer 的当前字节身份。"""

    tool_root = Path(__file__).resolve().parent
    paths = sorted(
        {
            Path(__file__).resolve(),
            Path(codex_upgrade.__file__).resolve(),
            Path(codex_upgrade_evidence_permissions.__file__).resolve(),
            Path(codex_upgrade_predispatch_stop.__file__).resolve(),
            Path(codex_upgrade_supervisor.__file__).resolve(),
            Path(codex_upgrade_timing_ledger.__file__).resolve(),
            Path(codex_upgrade_vc_artifacts.__file__).resolve(),
        },
        key=lambda item: item.relative_to(tool_root).as_posix(),
    )
    files = [
        {
            "path": path.relative_to(tool_root).as_posix(),
            "sha256": _sha256_file(path),
        }
        for path in paths
    ]
    return {
        "files": files,
        "bundle_sha256": codex_upgrade_vc_artifacts.digest(files),
    }


def _atomic_inventory(root: Path) -> list[dict[str, Any]]:
    """枚举一套演练的完整小型控制树，额外文件会改变 inventory。"""

    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid():
            raise CampaignRunRehearsalError(
                f"原子演练 inventory 属主漂移：{relative}"
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise CampaignRunRehearsalError(f"原子演练 inventory 含符号链接：{relative}")
        if stat.S_ISDIR(metadata.st_mode):
            if mode != 0o700:
                raise CampaignRunRehearsalError(
                    f"原子演练 inventory 目录权限不是 0700：{relative}"
                )
            entries.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "mode": "0700",
                }
            )
        elif stat.S_ISREG(metadata.st_mode):
            if mode != 0o600 or metadata.st_nlink != 1:
                raise CampaignRunRehearsalError(
                    f"原子演练 inventory 文件权限或硬链接数漂移：{relative}"
                )
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": "0600",
                    "bytes": metadata.st_size,
                    "sha256": _sha256_file(path),
                }
            )
        else:
            raise CampaignRunRehearsalError(
                f"原子演练 inventory 含特殊文件：{relative}"
            )
    return entries


def _write_atomic_campaign(
    root: Path,
    *,
    campaign_id: str,
) -> tuple[Path, Path, dict[str, Any], Path, dict[str, Any]]:
    """建立只含 VC-0 小型控制制品的独立离线 Campaign。"""

    campaign_dir = root / "campaign"
    state_dir = root / "state"
    campaign_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    now = datetime.now(timezone.utc)
    plan = codex_upgrade_vc_artifacts.build_campaign_plan(
        campaign_id=campaign_id,
        campaign_mode="formal",
        campaign_purpose="validation_only",
        baseline_version="0.151.0",
        target_version="0.154.0",
        created_at_utc=now.isoformat(),
        original_deadline_at_utc=(now + timedelta(minutes=10)).isoformat(),
        timing_checkpoint_sha256="1" * 64,
        arm64_environment_sha256="2" * 64,
        job_rehearsal_sha256="3" * 64,
        p0_gate_sha256="4" * 64,
    )
    plan_path = campaign_dir / "control/vc/campaign-plan.json"
    plan_path.parent.mkdir(mode=0o700, parents=True)
    (campaign_dir / "control").chmod(0o700)
    plan_path.parent.chmod(0o700)
    _write_once(plan_path, plan)
    stage_receipt_path = campaign_dir / "control/vc/vc0-stage.json"
    _write_once(
        stage_receipt_path,
        {
            "schema_version": "codex-atomic-vc0-stage/v1",
            "status": "passed",
            "campaign_id": campaign_id,
            "live_request_count": 0,
            "scanned_bytes": 0,
        },
    )
    checkpoint = codex_upgrade_vc_artifacts.build_vc_checkpoint(
        campaign_plan=plan,
        phase="VC-0",
        status="complete",
        predecessor_checkpoint=None,
        stage_receipt={
            "path": stage_receipt_path.relative_to(campaign_dir).as_posix(),
            "sha256": _sha256_file(stage_receipt_path),
        },
        completed_at_utc=datetime.now(timezone.utc).isoformat(),
        execute_item_ids=[],
        reuse_item_ids=[],
        live_request_count=0,
        scanned_bytes=0,
    )
    checkpoint_path = campaign_dir / "control/vc/vc-0-checkpoint.json"
    _write_once(checkpoint_path, checkpoint)
    timing_root = root / "timing-ledger"
    codex_upgrade_timing_ledger.create_ledger(
        timing_root,
        upgrade_id=f"{campaign_id}-upgrade",
        baseline_version="0.151.0",
        target_version="0.154.0",
        campaign_purpose="validation_only",
        evidence_decision="recapture",
    )
    codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=f"{campaign_id}-vc0-completed",
        phase="VC-0",
        event_type="stage_completed",
        next_action="启动原子离线 VC-1",
    )
    codex_upgrade_timing_ledger.append_event(
        timing_root,
        event_id=f"{campaign_id}-vc1-started",
        phase="VC-1",
        event_type="stage_started",
        next_action="执行原子离线父批次",
    )
    _write_once(
        campaign_dir / "campaign.json",
        {
            "campaign_id": campaign_id,
            "campaign_mode": "formal",
            "campaign_purpose": "validation_only",
            "baseline_version": "0.151.0",
            "target_version": "0.154.0",
            "vc_control": {
                "campaign_plan": {
                    "path": plan_path.relative_to(campaign_dir).as_posix(),
                    "sha256": _sha256_file(plan_path),
                }
            },
            "control_receipts": {
                "upgrade_timing": {
                    "ledger_dir": str(timing_root),
                    "ledger_plan_sha256": _sha256_file(timing_root / "ledger.json"),
                    "upgrade_id": f"{campaign_id}-upgrade",
                }
            },
        },
    )
    # 演练实例是 staging 树内的 formal 0.154 Campaign，派发前必须经项目总账门禁；
    # 这里为实例根创建 fixture_only 总账并完成注册，生产总账不受影响。
    codex_upgrade_project_ledger.create_fixture_ledger(root)
    codex_upgrade_project_ledger.register_existing_campaign(campaign_dir)
    return campaign_dir, state_dir, plan, checkpoint_path, checkpoint


def _atomic_compiler(
    *,
    plan: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    checkpoint_path: Path,
) -> Any:
    """返回与正式编译器使用同一 builders 和不可覆盖写入器的离线闭包。

    改造 4：入口按 Campaign 总计划的 ``batch_model`` 决定调用形态——staging 模型
    传入 ``staging_attempt_dir`` 与 ``owner_nonce``，闭包用正式编译器同一
    ``_write_staging_batch_artifacts`` 写 staging 三件套；legacy 形态保持原样。
    """

    def compile_batch(
        arguments: argparse.Namespace,
        *,
        staging_attempt_dir: Path | None = None,
        owner_nonce: str | None = None,
    ) -> dict[str, Any]:
        action_plan = codex_upgrade_vc_artifacts.validate_action_plan(
            _load_json(arguments.action_plan, "原子演练 action plan")
        )
        if (
            arguments.predecessor_checkpoint.resolve(strict=True)
            != checkpoint_path.resolve(strict=True)
        ):
            raise CampaignRunRehearsalError("原子演练 predecessor 路径漂移")
        predecessor = {
            "path": Path(arguments.predecessor_checkpoint)
            .relative_to(arguments.campaign_dir)
            .as_posix(),
            "sha256": _sha256_file(arguments.predecessor_checkpoint),
            "phase": checkpoint["phase"],
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        }
        now = datetime.now(timezone.utc)
        batch = codex_upgrade_vc_artifacts.build_vc_batch(
            campaign_plan=plan,
            phase=arguments.phase,
            sequence=arguments.sequence,
            predecessor_checkpoint=predecessor,
            execute_item_ids=action_plan["execute_item_ids"],
            reuse_item_ids=action_plan["reuse_item_ids"],
            actions=action_plan["actions"],
            compiled_at_utc=now.isoformat(),
            must_start_by_utc=(now + timedelta(seconds=30)).isoformat(),
        )
        name = f"{arguments.sequence:04d}-{arguments.phase.lower()}.json"
        batch_path = arguments.campaign_dir / "control/vc/batches" / name
        manifest_path = (
            arguments.campaign_dir / "control/vc/run-manifests" / name
        )
        if (staging_attempt_dir is None) != (owner_nonce is None):
            raise CampaignRunRehearsalError("原子演练 staging 参数必须成对给出")
        if staging_attempt_dir is not None:
            assert owner_nonce is not None
            return codex_upgrade._write_staging_batch_artifacts(
                arguments.campaign_dir,
                staging_attempt_dir,
                plan=plan,
                batch=batch,
                run_manifest=codex_upgrade._vc_run_manifest_from_batch(batch),
                owner_nonce=owner_nonce,
                prepared_at_utc=now.isoformat(),
            )
        codex_upgrade._secure_write_json_once(batch_path, batch)
        manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=batch["campaign_id"],
            campaign_plan_sha256=batch["campaign_plan_sha256"],
            batch_id=batch["batch_id"],
            batch_sequence=batch["sequence"],
            batch_sha256=batch["batch_sha256"],
            phase=batch["phase"],
            predecessor_checkpoint=batch["predecessor_checkpoint"],
            original_deadline_at_utc=batch["original_deadline_at_utc"],
            actions=batch["actions"],
            execute_items=batch["execute_item_ids"],
            reuse_items=batch["reuse_item_ids"],
        )
        codex_upgrade._secure_write_json_once(manifest_path, manifest)
        return {
            "status": "complete",
            "campaign_id": batch["campaign_id"],
            "phase": batch["phase"],
            "batch_sequence": batch["sequence"],
            "batch": str(batch_path),
            "batch_sha256": batch["batch_sha256"],
            "campaign_run_manifest": str(manifest_path),
            "original_deadline_at_utc": batch["original_deadline_at_utc"],
            "execute_item_ids": batch["execute_item_ids"],
            "reuse_item_ids": batch["reuse_item_ids"],
        }

    return compile_batch


def _atomic_negative_checks(
    *,
    instance_root: Path,
    state_dir: Path,
    plan: Mapping[str, Any],
    batch_path: Path,
    manifest_path: Path,
) -> dict[str, bool]:
    """执行 canonical、已有 run、deadline 和额外文件四个负例。"""

    batch = _load_json(batch_path, "原子演练 VC batch")
    tampered = dict(batch)
    tampered["reuse_item_ids"] = ["tampered-reuse"]
    try:
        codex_upgrade_vc_artifacts.validate_vc_batch(tampered, plan)
    except codex_upgrade_vc_artifacts.VCArtifactError:
        canonical_rejected = True
    else:
        raise CampaignRunRehearsalError("canonical 篡改负例没有失败关闭")

    manifest = codex_upgrade_supervisor._campaign_run_manifest(manifest_path)
    try:
        codex_upgrade_predispatch_stop._history_summary(state_dir, manifest)
    except codex_upgrade_predispatch_stop.PredispatchStopError as error:
        if "已有父 run" not in str(error):
            raise CampaignRunRehearsalError(
                "已有父 run 负例被其他原因拒绝"
            ) from error
        existing_run_rejected = True
    else:
        raise CampaignRunRehearsalError("已有父 run 负例没有失败关闭")

    checkpoint_reference = {
        "path": "control/vc/vc-1-checkpoint.json",
        "sha256": "7" * 64,
        "phase": "VC-1",
        "checkpoint_sha256": "8" * 64,
    }
    deadline = datetime.fromisoformat(
        str(plan["original_deadline_at_utc"]).replace("Z", "+00:00")
    )
    drifted = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
        campaign_id=str(plan["campaign_id"]),
        campaign_plan_sha256=str(plan["plan_sha256"]),
        batch_id="vc-2-0002",
        batch_sequence=2,
        batch_sha256="9" * 64,
        phase="VC-2",
        predecessor_checkpoint=checkpoint_reference,
        original_deadline_at_utc=(deadline - timedelta(seconds=1)).isoformat(),
        actions=[],
        execute_items=[],
        reuse_items=[],
    )
    history = codex_upgrade_supervisor._campaign_run_history(
        state_dir,
        str(plan["campaign_id"]),
    )
    try:
        codex_upgrade_supervisor._validate_batched_campaign_history(
            drifted,
            history,
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        if "deadline" not in str(error):
            raise CampaignRunRehearsalError(
                "deadline 漂移负例被其他原因拒绝"
            ) from error
        deadline_drift_rejected = True
    else:
        raise CampaignRunRehearsalError("deadline 漂移负例没有失败关闭")

    baseline_inventory = _atomic_inventory(instance_root)
    extra_path = instance_root / "unregistered-negative.json"
    try:
        _write_once(extra_path, {"negative_fixture": "extra-file"})
        if _atomic_inventory(instance_root) == baseline_inventory:
            raise CampaignRunRehearsalError("额外文件负例未改变 inventory")
        extra_file_detected = True
    finally:
        extra_path.unlink(missing_ok=True)
        codex_upgrade_supervisor._fsync_directory(instance_root)
    if _atomic_inventory(instance_root) != baseline_inventory:
        raise CampaignRunRehearsalError("额外文件负例清理后 inventory 未恢复")
    return {
        "canonical_tamper_rejected": canonical_rejected,
        "existing_parent_run_rejected": existing_run_rejected,
        "deadline_drift_rejected": deadline_drift_rejected,
        "extra_file_detected_by_inventory": extra_file_detected,
    }


def _atomic_action(campaign_id: str, marker_path: Path) -> dict[str, Any]:
    """生成原子演练唯一的零网络动作。"""

    action_id = "atomic-offline-action"
    execute_id = "atomic-offline-execute"
    return {
        "action_id": action_id,
        "operation": "VC-1:atomic-offline-rehearsal",
        "timeout_seconds": 30.0,
        "command": _action_command(
            campaign_id=campaign_id,
            phase="VC-1",
            action_id=action_id,
            output=marker_path,
        ),
        "item_ids": [execute_id],
    }


def _atomic_permission_closeout(root: Path, campaign_id: str) -> dict[str, Any]:
    """真实制造 0755/0644 缺口，并用通用工具收口、重放。"""

    data_root = root / "data"
    runs_root = data_root / "runs"
    attempt_root = (
        data_root
        / "evidence"
        / "campaigns"
        / campaign_id
        / "official"
        / "attempts"
        / "permission-attempt"
    )
    evidence_root = attempt_root / "evidence"
    logs_root = attempt_root / "logs"
    direct_root = runs_root / f"{campaign_id}-official-core"
    oauth_root = runs_root / "official-client" / "oauth" / f"oauth-{campaign_id}"
    directories = (
        data_root,
        runs_root,
        data_root / "evidence",
        data_root / "evidence" / "campaigns",
        data_root / "evidence" / "campaigns" / campaign_id,
        data_root / "evidence" / "campaigns" / campaign_id / "official",
        data_root
        / "evidence"
        / "campaigns"
        / campaign_id
        / "official"
        / "attempts",
        attempt_root,
        evidence_root,
        logs_root,
        runs_root / "official-client",
        runs_root / "official-client" / "oauth",
        direct_root,
        oauth_root,
    )
    for directory in directories:
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    gap_roots = (direct_root, oauth_root, evidence_root, logs_root)
    for directory in gap_roots:
        directory.chmod(0o755)
    evidence_files = (
        direct_root / "capture.json",
        oauth_root / "tui.log",
        evidence_root / "restoration.json",
        logs_root / "job.log",
    )
    for path in evidence_files:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o644,
        )
        try:
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    roots = [direct_root, oauth_root, evidence_root, logs_root]
    receipt_path, receipt = (
        codex_upgrade_evidence_permissions.close_evidence_permissions(
            attempt_root,
            roots,
            managed_data_root=data_root,
            logical_runs_roots=(runs_root,),
        )
    )
    binding = codex_upgrade_evidence_permissions.receipt_binding(
        attempt_root,
        receipt_path,
    )
    replayed = (
        codex_upgrade_evidence_permissions.replay_evidence_permission_closeout(
            attempt_root,
            roots,
            binding,
            managed_data_root=data_root,
            logical_runs_roots=(runs_root,),
        )
    )
    if (
        replayed != receipt
        or receipt.get("changed_entry_count") != 8
        or receipt.get("external_alias_entry_count") != 4
        or receipt.get("entry_count") != 8
        or receipt.get("scanned_bytes") != 0
        or receipt.get("live_request_count") != 0
    ):
        raise CampaignRunRehearsalError("原子演练权限收口或重放事实漂移")
    return {
        "schema_version": codex_upgrade_evidence_permissions.SCHEMA_VERSION,
        "status": "passed",
        "attempt_root": attempt_root.relative_to(root).as_posix(),
        "evidence_roots": [path.relative_to(root).as_posix() for path in roots],
        "receipt": {
            "path": receipt_path.relative_to(root).as_posix(),
            "sha256": binding["sha256"],
            "bytes": binding["bytes"],
        },
        "entry_count": receipt["entry_count"],
        "changed_entry_count": receipt["changed_entry_count"],
        "external_alias_entry_count": receipt["external_alias_entry_count"],
        "boundary_sha256": receipt["boundary_sha256"],
        "pre_closeout_gap_sha256": receipt["pre_closeout_gap_sha256"],
        "receipt_replayed": True,
        "scanned_bytes": 0,
        "live_request_count": 0,
    }


def _atomic_failing_parent(
    root: Path,
    *,
    campaign_dir: Path,
    campaign_id: str,
    state_name: str,
    action_id: str,
) -> dict[str, Any]:
    """经真实 campaign-run 执行一个零网络失败动作并冻结父结果。"""

    state_dir = root / state_name
    manifest_path = (
        campaign_dir / "control" / "vc" / "run-manifests" / f"{action_id}.json"
    )
    manifest = codex_upgrade_supervisor.build_campaign_run_manifest(
        campaign_id,
        "VC-1",
        30,
        actions=[
            {
                "action_id": action_id,
                "operation": f"VC-1:{action_id}",
                "timeout_seconds": 5,
                "command": [sys.executable, "-c", "raise SystemExit(7)"],
            }
        ],
    )
    _write_once(manifest_path, manifest)
    returncode, result = codex_upgrade_supervisor._campaign_run_command(
        argparse.Namespace(
            manifest=manifest_path,
            state_dir=state_dir,
            heartbeat_seconds=0.05,
            watchdog_timeout_seconds=0.5,
            ledger_interval_seconds=0.05,
        )
    )
    run_dir = _private_directory(
        Path(str(result.get("run_dir", ""))),
        "原子演练失败父 run",
    )
    try:
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise CampaignRunRehearsalError("原子演练失败父 run 无法审计") from error
    actions = result.get("actions")
    timing_closeout = result.get("timing_closeout")
    if (
        returncode != 1
        or result.get("campaign_id") != campaign_id
        or result.get("status") != "failed"
        or result.get("reason") != f"action-failed:{action_id}"
        or not isinstance(actions, list)
        or len(actions) != 1
        or actions[0].get("action_id") != action_id
        or actions[0].get("returncode") != 7
        or actions[0].get("status") != "failed"
        or not isinstance(actions[0].get("diagnostic"), Mapping)
        or not isinstance(timing_closeout, Mapping)
        or audit.get("state") != "failed"
        or audit.get("audit_incomplete") is not False
    ):
        raise CampaignRunRehearsalError("原子演练失败父结果未闭合")
    state = _load_json(run_dir / "state.json", "原子演练失败父状态")
    diagnostic_path = run_dir / "action-diagnostics" / f"action-{action_id}-failure.json"
    try:
        codex_upgrade_supervisor._validate_action_diagnostic(
            diagnostic_path,
            run_dir=run_dir,
            campaign_id=campaign_id,
            phase="VC-1",
            action_id=action_id,
            owner_pid=int(state["owner_pid"]),
            owner_nonce=str(state["owner_nonce"]),
        )
    except (KeyError, TypeError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise CampaignRunRehearsalError("原子演练失败动作诊断无法重放") from error
    return {
        "action_id": action_id,
        "state_dir": state_dir.relative_to(root).as_posix(),
        "run_dir": run_dir.relative_to(root).as_posix(),
        "state": "failed",
        "reason": f"action-failed:{action_id}",
        "audit_incomplete": False,
        "event_count": audit["event_count"],
        "timing_closeout": dict(timing_closeout),
        "live_request_count": 0,
        "scanned_bytes": 0,
        "network_used": False,
    }


def _atomic_timing_failure_closeout(
    root: Path,
    *,
    campaign_dir: Path,
    campaign_id: str,
) -> dict[str, Any]:
    """证明失败父 run 关闭 active 阶段，并显式报告闭合失败负例。"""

    failure_action_id = "atomic-offline-failure"
    closeout_failure_action_id = "atomic-closeout-failure"
    failure_parent = _atomic_failing_parent(
        root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        state_name="failure-state",
        action_id=failure_action_id,
    )
    if failure_parent["timing_closeout"].get("status") != "passed":
        raise CampaignRunRehearsalError("原子演练父失败没有关闭时间账本")
    ledger_root = root / "timing-ledger"
    stopped = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)
    events = [
        event["event_type"]
        for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_root)
    ]
    if (
        stopped.get("status") != "stopped"
        or stopped.get("active_phase") is not None
        or events[-2:] != ["stage_abandoned", "stop_the_line"]
    ):
        raise CampaignRunRehearsalError("原子演练父失败后时间账本仍为 active")
    closeout_failure_parent = _atomic_failing_parent(
        root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        state_name="closeout-failure-state",
        action_id=closeout_failure_action_id,
    )
    closeout_failure = closeout_failure_parent["timing_closeout"]
    after_negative = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)
    if (
        closeout_failure.get("status") != "failed"
        or closeout_failure.get("error_type")
        != "SupervisorError"
        or "已由其他根因停线" not in str(closeout_failure.get("message", ""))
        or after_negative.get("status") != "stopped"
        or after_negative.get("active_phase") is not None
        or after_negative.get("head_sequence") != stopped.get("head_sequence")
        or after_negative.get("head_sha256") != stopped.get("head_sha256")
        or after_negative.get("last_event_id") != stopped.get("last_event_id")
    ):
        raise CampaignRunRehearsalError("原子演练账本闭合失败负例未显式报告")
    return {
        "ledger_dir": ledger_root.relative_to(root).as_posix(),
        "ledger_plan_sha256": _sha256_file(ledger_root / "ledger.json"),
        "status": "stopped",
        "active_phase": None,
        "head_sequence": stopped["head_sequence"],
        "head_sha256": stopped["head_sha256"],
        "last_event_id": stopped["last_event_id"],
        "next_action": stopped["next_action"],
        "event_types": events,
        "failure_parent": failure_parent,
        "closeout_failure_parent": closeout_failure_parent,
        "live_request_count": 0,
        "scanned_bytes": 0,
        "network_used": False,
    }


def _collect_atomic_instance(root: Path, index: int) -> dict[str, Any]:
    """从全新 VC-0 控制树经真实原子入口完成一次 VC-1。"""

    root.mkdir(mode=0o700)
    campaign_id = f"atomic-vc0-vc1-{index}"
    campaign_dir, state_dir, plan, checkpoint_path, checkpoint = (
        _write_atomic_campaign(root, campaign_id=campaign_id)
    )
    permission_closeout = _atomic_permission_closeout(root, campaign_id)
    action_id = "atomic-offline-action"
    execute_id = "atomic-offline-execute"
    reuse_id = "atomic-offline-reuse"
    marker_path = state_dir / "atomic-action.json"
    action_plan_path = campaign_dir / "control/vc/vc1-action-plan.json"
    _write_once(
        action_plan_path,
        {
            "schema_version": codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
            "execute_item_ids": [execute_id],
            "reuse_item_ids": [reuse_id],
            "actions": [_atomic_action(campaign_id, marker_path)],
        },
    )
    arguments = argparse.Namespace(
        campaign_dir=campaign_dir,
        state_dir=state_dir,
        phase="VC-1",
        sequence=1,
        predecessor_checkpoint=checkpoint_path,
        action_plan=action_plan_path,
        heartbeat_seconds=1,
        watchdog_timeout_seconds=5.0,
        ledger_interval_seconds=1.0,
    )
    result, returncode = codex_upgrade.compile_and_run_vc_batch(
        arguments,
        _compiler=_atomic_compiler(
            plan=plan,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        ),
    )
    run = result.get("campaign_run")
    if returncode != 0 or not isinstance(run, Mapping):
        raise CampaignRunRehearsalError("原子 VC-0→VC-1 演练未形成父 run 终态")
    run_summary = _run_summary(
        run,
        expected_campaign_id=campaign_id,
        expected_batch_id="vc-1-0001",
        expected_sequence=1,
        expected_deadline=str(plan["original_deadline_at_utc"]),
        expected_action_id=action_id,
        expected_execute=execute_id,
        expected_reuse=reuse_id,
        marker_path=marker_path,
    )
    batch_path = campaign_dir / "control/vc/batches/0001-vc-1.json"
    manifest_path = campaign_dir / "control/vc/run-manifests/0001-vc-1.json"
    vc1_checkpoint = codex_upgrade_vc_artifacts.build_vc_checkpoint(
        campaign_plan=plan,
        phase="VC-1",
        status="complete",
        predecessor_checkpoint={
            "path": checkpoint_path.relative_to(campaign_dir).as_posix(),
            "sha256": _sha256_file(checkpoint_path),
            "phase": "VC-0",
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        },
        stage_receipt={
            "path": str(marker_path),
            "sha256": _sha256_file(marker_path),
        },
        completed_at_utc=datetime.now(timezone.utc).isoformat(),
        execute_item_ids=[execute_id],
        reuse_item_ids=[reuse_id],
        live_request_count=0,
        scanned_bytes=0,
    )
    vc1_checkpoint_path = campaign_dir / "control/vc/vc-1-checkpoint.json"
    _write_once(vc1_checkpoint_path, vc1_checkpoint)
    timing_failure_closeout = _atomic_timing_failure_closeout(
        root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
    )
    negatives = _atomic_negative_checks(
        instance_root=root,
        state_dir=state_dir,
        plan=plan,
        batch_path=batch_path,
        manifest_path=manifest_path,
    )
    return {
        "index": index,
        "campaign_id": campaign_id,
        "root": str(root),
        "campaign_dir": str(campaign_dir),
        "state_dir": str(state_dir),
        "vc0_checkpoint": {
            "path": checkpoint_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(checkpoint_path),
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        },
        "vc1_batch": {
            "path": batch_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(batch_path),
        },
        "vc1_manifest": {
            "path": manifest_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(manifest_path),
        },
        "vc1_checkpoint": {
            "path": vc1_checkpoint_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(vc1_checkpoint_path),
            "checkpoint_sha256": vc1_checkpoint["checkpoint_sha256"],
        },
        "action_marker": {
            "path": marker_path.relative_to(root).as_posix(),
            "sha256": _sha256_file(marker_path),
        },
        "permission_closeout": permission_closeout,
        "timing_failure_closeout": timing_failure_closeout,
        "parent_run": run_summary,
        "negative_fixtures": negatives,
        "live_request_count": 0,
        "scanned_bytes": 0,
        "inventory": _atomic_inventory(root),
    }


def collect_atomic_double(
    evidence_root: Path,
    output_relative: str | Path,
    *,
    require_arm64: bool = True,
) -> dict[str, Any]:
    """在两个完全独立的新根中连续完成 VC-0→VC-1 原子闭环。"""

    root = _private_directory(evidence_root, "原子双跑证据根")
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise CampaignRunRehearsalError("原子双跑证据根权限必须精确为 0700")
    if any(root.iterdir()):
        raise CampaignRunRehearsalError("原子双跑证据根必须为空")
    output = _new_output(root, output_relative, "原子双跑收据输出")
    environment = _atomic_environment(require_arm64=require_arm64)
    if require_arm64:
        try:
            root.relative_to(Path("/capture/staging").resolve(strict=True))
        except ValueError as error:
            raise CampaignRunRehearsalError(
                "原子双跑证据根必须位于 /capture/staging"
            ) from error
    instances = [
        _collect_atomic_instance(root / "instance-1", 1),
        _collect_atomic_instance(root / "instance-2", 2),
    ]
    first_root = str(root / "instance-1").encode("utf-8")
    for path in (root / "instance-2").rglob("*"):
        if path.is_file() and first_root in path.read_bytes():
            raise CampaignRunRehearsalError("第二次演练读取或引用了第一次状态")
    receipt: dict[str, Any] = {
        "schema_version": ATOMIC_RECEIPT_SCHEMA,
        "status": "passed",
        "collected_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "environment": environment,
        "tool_identity": _atomic_tool_identity(),
        "instances": instances,
        "isolation": {
            "distinct_campaign_ids": True,
            "distinct_state_dirs": True,
            "second_references_first": False,
        },
        "live_request_count": 0,
        "scanned_bytes": 0,
        "network_used": False,
    }
    receipt["receipt_sha256"] = codex_upgrade_vc_artifacts.digest(receipt)
    _write_once(output, receipt)
    return receipt


def _atomic_bound_file(
    root: Path,
    value: Any,
    expected_relative: str,
    label: str,
    *,
    identity_field: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """校验原子演练收据中的规范相对路径、摘要和可选身份字段。"""

    fields = {"path", "sha256"}
    if identity_field is not None:
        fields.add(identity_field)
    binding = _expect_fields(value, fields, label)
    if binding.get("path") != expected_relative:
        raise CampaignRunRehearsalError(f"{label}路径漂移")
    path = _relative_file(root, expected_relative, label)
    if (
        not isinstance(binding.get("sha256"), str)
        or not SHA256_RE.fullmatch(str(binding["sha256"]))
        or binding["sha256"] != _sha256_file(path)
    ):
        raise CampaignRunRehearsalError(f"{label}摘要漂移")
    return binding, path


def _replay_atomic_permission_closeout(
    root: Path,
    campaign_id: str,
    value: Any,
) -> None:
    """从证据树元数据重放一轮通用权限收口收据。"""

    closeout = _expect_fields(
        value,
        {
            "schema_version",
            "status",
            "attempt_root",
            "evidence_roots",
            "receipt",
            "entry_count",
            "changed_entry_count",
            "external_alias_entry_count",
            "boundary_sha256",
            "pre_closeout_gap_sha256",
            "receipt_replayed",
            "scanned_bytes",
            "live_request_count",
        },
        "原子演练权限收口",
    )
    attempt_relative = (
        f"data/evidence/campaigns/{campaign_id}/official/attempts/permission-attempt"
    )
    root_relatives = [
        f"data/runs/{campaign_id}-official-core",
        f"data/runs/official-client/oauth/oauth-{campaign_id}",
        f"{attempt_relative}/evidence",
        f"{attempt_relative}/logs",
    ]
    receipt_relative = f"{attempt_relative}/evidence-permission-closeout.json"
    receipt_binding = _expect_fields(
        closeout.get("receipt"),
        {"path", "sha256", "bytes"},
        "原子演练权限收口绑定",
    )
    if (
        closeout.get("schema_version")
        != codex_upgrade_evidence_permissions.SCHEMA_VERSION
        or closeout.get("status") != "passed"
        or closeout.get("attempt_root") != attempt_relative
        or closeout.get("evidence_roots") != root_relatives
        or receipt_binding.get("path") != receipt_relative
        or closeout.get("entry_count") != 8
        or closeout.get("changed_entry_count") != 8
        or closeout.get("external_alias_entry_count") != 4
        or closeout.get("receipt_replayed") is not True
        or closeout.get("scanned_bytes") != 0
        or closeout.get("live_request_count") != 0
    ):
        raise CampaignRunRehearsalError("原子演练权限收口字段或零读取事实漂移")
    attempt_root = _private_directory(root / attempt_relative, "原子演练权限 Attempt")
    evidence_roots = [
        _private_directory(root / relative, "原子演练权限证据根")
        for relative in root_relatives
    ]
    receipt_path = _relative_file(root, receipt_relative, "原子演练权限收口收据")
    binding = {
        "path": receipt_path.relative_to(attempt_root).as_posix(),
        "sha256": receipt_binding.get("sha256"),
        "bytes": receipt_binding.get("bytes"),
    }
    try:
        replayed = (
            codex_upgrade_evidence_permissions.replay_evidence_permission_closeout(
                attempt_root,
                evidence_roots,
                binding,
                managed_data_root=root / "data",
                logical_runs_roots=(root / "data/runs",),
            )
        )
    except codex_upgrade_evidence_permissions.EvidencePermissionError as error:
        raise CampaignRunRehearsalError("原子演练权限收口收据无法重放") from error
    if (
        replayed.get("entry_count") != closeout.get("entry_count")
        or replayed.get("changed_entry_count") != closeout.get("changed_entry_count")
        or replayed.get("external_alias_entry_count")
        != closeout.get("external_alias_entry_count")
        or replayed.get("boundary_sha256") != closeout.get("boundary_sha256")
        or replayed.get("pre_closeout_gap_sha256")
        != closeout.get("pre_closeout_gap_sha256")
    ):
        raise CampaignRunRehearsalError("原子演练权限收口重放摘要漂移")


def _replay_atomic_failing_parent(
    root: Path,
    *,
    campaign_dir: Path,
    campaign_id: str,
    state_name: str,
    action_id: str,
    value: Any,
    expect_closeout_failure: bool,
) -> None:
    """重放一个真实失败父 run 及其动作诊断和账本闭合结果。"""

    parent = _expect_fields(
        value,
        {
            "action_id",
            "state_dir",
            "run_dir",
            "state",
            "reason",
            "audit_incomplete",
            "event_count",
            "timing_closeout",
            "live_request_count",
            "scanned_bytes",
            "network_used",
        },
        f"原子演练失败父结果 {action_id}",
    )
    state_dir = _private_directory(root / state_name, "原子演练失败父 state-dir")
    run_relative = parent.get("run_dir")
    if not isinstance(run_relative, str):
        raise CampaignRunRehearsalError("原子演练失败父 run 路径非法")
    run_dir = _private_directory(root / run_relative, "原子演练失败父 run")
    run_directories = sorted(
        path.resolve()
        for path in state_dir.iterdir()
        if path.name.startswith("run-") and path.is_dir() and not path.is_symlink()
    )
    if (
        parent.get("action_id") != action_id
        or parent.get("state_dir") != state_name
        or run_dir.parent != state_dir
        or run_directories != [run_dir]
        or parent.get("state") != "failed"
        or parent.get("reason") != f"action-failed:{action_id}"
        or parent.get("audit_incomplete") is not False
        or parent.get("live_request_count") != 0
        or parent.get("scanned_bytes") != 0
        or parent.get("network_used") is not False
    ):
        raise CampaignRunRehearsalError("原子演练失败父身份或零请求事实漂移")
    try:
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise CampaignRunRehearsalError("原子演练失败父 run 无法重放") from error
    state = _load_json(run_dir / "state.json", "原子演练失败父状态")
    stop_receipt = _load_json(
        run_dir / "stop-receipt.json",
        "原子演练失败父 stop receipt",
    )
    if (
        audit.get("state") != "failed"
        or audit.get("audit_incomplete") is not False
        or audit.get("event_count") != parent.get("event_count")
        or state.get("state") != "failed"
        or stop_receipt.get("reason") != f"action-failed:{action_id}"
    ):
        raise CampaignRunRehearsalError("原子演练失败父终态漂移")
    manifest_path = (
        campaign_dir / "control" / "vc" / "run-manifests" / f"{action_id}.json"
    )
    manifest = codex_upgrade_supervisor._campaign_run_manifest(manifest_path)
    expected_manifest = codex_upgrade_supervisor.build_campaign_run_manifest(
        campaign_id,
        "VC-1",
        30,
        actions=[
            {
                "action_id": action_id,
                "operation": f"VC-1:{action_id}",
                "timeout_seconds": 5,
                "command": [sys.executable, "-c", "raise SystemExit(7)"],
            }
        ],
    )
    recorded_manifest = _expect_fields(
        _load_json(run_dir / "campaign-run-manifest.json", "失败父不可变 manifest"),
        {"schema_version", "manifest_sha256", "manifest"},
        "失败父不可变 manifest",
    )
    if (
        manifest != expected_manifest
        or recorded_manifest
        != {
            "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_SCHEMA,
            "manifest_sha256": codex_upgrade_supervisor._sha256(
                codex_upgrade_supervisor._canonical(expected_manifest)
            ),
            "manifest": expected_manifest,
        }
    ):
        raise CampaignRunRehearsalError("原子演练失败父 manifest 漂移")
    try:
        codex_upgrade_supervisor._validate_action_diagnostic(
            run_dir / "action-diagnostics" / f"action-{action_id}-failure.json",
            run_dir=run_dir,
            campaign_id=campaign_id,
            phase="VC-1",
            action_id=action_id,
            owner_pid=int(state["owner_pid"]),
            owner_nonce=str(state["owner_nonce"]),
        )
    except (KeyError, TypeError, ValueError, codex_upgrade_supervisor.SupervisorError) as error:
        raise CampaignRunRehearsalError("原子演练失败动作诊断重放失败") from error

    timing_closeout = parent.get("timing_closeout")
    if not isinstance(timing_closeout, Mapping):
        raise CampaignRunRehearsalError("原子演练失败父缺少账本闭合结果")
    if expect_closeout_failure:
        try:
            codex_upgrade_supervisor._close_failed_campaign_timing_ledger(
                campaign_dir,
                manifest,
                failed_action_id=action_id,
            )
        except codex_upgrade_supervisor.SupervisorError as error:
            expected_closeout = {
                "status": "failed",
                "error_type": type(error).__name__,
                "message": str(error)[:1000],
            }
        else:
            raise CampaignRunRehearsalError("原子演练账本闭合失败负例未被拒绝")
    else:
        try:
            expected_closeout = (
                codex_upgrade_supervisor._close_failed_campaign_timing_ledger(
                    campaign_dir,
                    manifest,
                    failed_action_id=action_id,
                )
            )
        except codex_upgrade_supervisor.SupervisorError as error:
            raise CampaignRunRehearsalError("原子演练账本闭合无法幂等重放") from error
        expected_closeout["idempotent"] = False
    if dict(timing_closeout) != expected_closeout:
        raise CampaignRunRehearsalError("原子演练失败父账本闭合结果漂移")


def _replay_atomic_timing_failure_closeout(
    root: Path,
    *,
    campaign_dir: Path,
    campaign_id: str,
    value: Any,
) -> None:
    """重放失败父账本终态和闭合失败负例。"""

    closeout = _expect_fields(
        value,
        {
            "ledger_dir",
            "ledger_plan_sha256",
            "status",
            "active_phase",
            "head_sequence",
            "head_sha256",
            "last_event_id",
            "next_action",
            "event_types",
            "failure_parent",
            "closeout_failure_parent",
            "live_request_count",
            "scanned_bytes",
            "network_used",
        },
        "原子演练失败时间账本闭合",
    )
    ledger_root = _private_directory(root / "timing-ledger", "原子演练时间账本")
    summary = codex_upgrade_timing_ledger.inspect_ledger(ledger_root)
    event_types = [
        event["event_type"]
        for event, _raw in codex_upgrade_timing_ledger._load_events(ledger_root)
    ]
    if (
        closeout.get("ledger_dir") != "timing-ledger"
        or closeout.get("ledger_plan_sha256")
        != _sha256_file(ledger_root / "ledger.json")
        or closeout.get("status") != summary.get("status")
        or closeout.get("active_phase") != summary.get("active_phase")
        or closeout.get("head_sequence") != summary.get("head_sequence")
        or closeout.get("head_sha256") != summary.get("head_sha256")
        or closeout.get("last_event_id") != summary.get("last_event_id")
        or closeout.get("next_action") != summary.get("next_action")
        or closeout.get("event_types") != event_types
        or summary.get("status") != "stopped"
        or summary.get("active_phase") is not None
        or event_types[-2:] != ["stage_abandoned", "stop_the_line"]
        or closeout.get("live_request_count") != 0
        or closeout.get("scanned_bytes") != 0
        or closeout.get("network_used") is not False
    ):
        raise CampaignRunRehearsalError("原子演练失败时间账本终态漂移")
    _replay_atomic_failing_parent(
        root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        state_name="failure-state",
        action_id="atomic-offline-failure",
        value=closeout.get("failure_parent"),
        expect_closeout_failure=False,
    )
    _replay_atomic_failing_parent(
        root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        state_name="closeout-failure-state",
        action_id="atomic-closeout-failure",
        value=closeout.get("closeout_failure_parent"),
        expect_closeout_failure=True,
    )


def _atomic_expected_inventory_paths(
    run_name: str,
    failure_run_name: str,
    closeout_failure_run_name: str,
    campaign_id: str,
) -> set[str]:
    """返回单次合成 VC-0→VC-1 控制树允许存在的完整路径集合。"""

    def supervisor_paths(
        state_name: str,
        current_run_name: str,
        *,
        failure_action_id: str | None = None,
    ) -> set[str]:
        current_run = f"{state_name}/{current_run_name}"
        paths = {
            state_name,
            f"{state_name}/.campaign-run.lock",
            current_run,
            f"{current_run}/.supervisor.lock",
            f"{current_run}/action-diagnostics",
            f"{current_run}/campaign-run-manifest.json",
            f"{current_run}/events.ndjson",
            f"{current_run}/heartbeat.json",
            f"{current_run}/minute-ledger.ndjson",
            f"{current_run}/state.json",
            f"{current_run}/stop-receipt.json",
            f"{current_run}/stop-request.json",
            f"{current_run}/watchdog-heartbeats.ndjson",
        }
        if failure_action_id is not None:
            paths.add(
                f"{current_run}/action-diagnostics/"
                f"action-{failure_action_id}-failure.json"
            )
        return paths

    attempt = (
        f"data/evidence/campaigns/{campaign_id}/official/attempts/permission-attempt"
    )
    paths = {
        "campaign",
        "campaign/campaign.json",
        # 项目总账门禁：实例根内的 fixture_only 总账与 Campaign 侧注册 batch。
        "upgrade-project-ledger",
        "upgrade-project-ledger/.project-ledger.lock",
        "upgrade-project-ledger/plan.json",
        "upgrade-project-ledger/initial-identity-keys.json",
        "upgrade-project-ledger/head.json",
        "upgrade-project-ledger/events",
        "upgrade-project-ledger/events/000001.json",
        "upgrade-project-ledger/repairs",
        "upgrade-project-ledger/repairs/outbox",
        "upgrade-project-ledger/repairs/receipts",
        "campaign/ledger",
        "campaign/ledger/.ledger.lock",
        "campaign/ledger/plan.json",
        "campaign/ledger/outbox",
        "campaign/ledger/outbox/batch-000001",
        "campaign/ledger/outbox/batch-000001/entry-01.json",
        "campaign/ledger/outbox/batch-000001/COMMIT",
        "campaign/control",
        "campaign/control/vc",
        "campaign/control/vc/batches",
        "campaign/control/vc/batches/0001-vc-1.json",
        "campaign/control/vc/campaign-plan.json",
        # 改造 4（staging/WAL）：演练 Campaign 的总计划是 staging 模型，原子入口先写
        # staging attempt 三件套，COMMIT 后才发布正式 batch／run-manifest。
        "campaign/control/vc/staging",
        "campaign/control/vc/staging/0001-vc-1",
        "campaign/control/vc/staging/0001-vc-1/attempt-1",
        "campaign/control/vc/staging/0001-vc-1/attempt-1/batch.json",
        "campaign/control/vc/staging/0001-vc-1/attempt-1/run-manifest.json",
        "campaign/control/vc/staging/0001-vc-1/attempt-1/PREPARED",
        "campaign/control/vc/commits",
        "campaign/control/vc/commits/0001-vc-1.json",
        "campaign/control/vc/run-manifests",
        "campaign/control/vc/run-manifests/0001-vc-1.json",
        "campaign/control/vc/run-manifests/atomic-offline-failure.json",
        "campaign/control/vc/run-manifests/atomic-closeout-failure.json",
        "campaign/control/vc/vc-0-checkpoint.json",
        "campaign/control/vc/vc-1-checkpoint.json",
        "campaign/control/vc/vc0-stage.json",
        "campaign/control/vc/vc1-action-plan.json",
        "state/atomic-action.json",
        "timing-ledger",
        "timing-ledger/.vc0-closeout.lock",
        "timing-ledger/events",
        "timing-ledger/events/000001.json",
        "timing-ledger/events/000002.json",
        "timing-ledger/events/000003.json",
        "timing-ledger/events/000004.json",
        "timing-ledger/events/000005.json",
        "timing-ledger/ledger.json",
        "timing-ledger/receipts",
        "data",
        "data/evidence",
        "data/evidence/campaigns",
        f"data/evidence/campaigns/{campaign_id}",
        f"data/evidence/campaigns/{campaign_id}/official",
        f"data/evidence/campaigns/{campaign_id}/official/attempts",
        attempt,
        f"{attempt}/evidence",
        f"{attempt}/evidence/restoration.json",
        f"{attempt}/logs",
        f"{attempt}/logs/job.log",
        f"{attempt}/evidence-permission-closeout.json",
        "data/runs",
        f"data/runs/{campaign_id}-official-core",
        f"data/runs/{campaign_id}-official-core/capture.json",
        "data/runs/official-client",
        "data/runs/official-client/oauth",
        f"data/runs/official-client/oauth/oauth-{campaign_id}",
        f"data/runs/official-client/oauth/oauth-{campaign_id}/tui.log",
    }
    paths.update(supervisor_paths("state", run_name))
    paths.update(
        supervisor_paths(
            "failure-state",
            failure_run_name,
            failure_action_id="atomic-offline-failure",
        )
    )
    paths.update(
        supervisor_paths(
            "closeout-failure-state",
            closeout_failure_run_name,
            failure_action_id="atomic-closeout-failure",
        )
    )
    return paths


def _replay_atomic_instance(
    root: Path,
    index: int,
    value: Any,
) -> None:
    """从实际文件和父 run 重建一个原子演练实例，拒绝重摘要伪造。"""

    instance = _expect_fields(
        value,
        {
            "index",
            "campaign_id",
            "root",
            "campaign_dir",
            "state_dir",
            "vc0_checkpoint",
            "vc1_batch",
            "vc1_manifest",
            "vc1_checkpoint",
            "action_marker",
            "permission_closeout",
            "timing_failure_closeout",
            "parent_run",
            "negative_fixtures",
            "live_request_count",
            "scanned_bytes",
            "inventory",
        },
        f"原子双跑实例 {index}",
    )
    instance_root = _private_directory(root / f"instance-{index}", "原子演练实例根")
    campaign_dir = _private_directory(instance_root / "campaign", "原子演练 Campaign")
    state_dir = _private_directory(instance_root / "state", "原子演练 state-dir")
    campaign_id = f"atomic-vc0-vc1-{index}"
    if (
        instance.get("index") != index
        or instance.get("campaign_id") != campaign_id
        or instance.get("root") != str(instance_root)
        or instance.get("campaign_dir") != str(campaign_dir)
        or instance.get("state_dir") != str(state_dir)
        or instance.get("live_request_count") != 0
        or instance.get("scanned_bytes") != 0
        or instance.get("negative_fixtures")
        != {
            "canonical_tamper_rejected": True,
            "existing_parent_run_rejected": True,
            "deadline_drift_rejected": True,
            "extra_file_detected_by_inventory": True,
        }
    ):
        raise CampaignRunRehearsalError(f"原子双跑实例 {index} 身份或零请求边界漂移")

    plan_path = _relative_file(
        campaign_dir,
        "control/vc/campaign-plan.json",
        "原子演练 Campaign plan",
    )
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(
            _load_json(plan_path, "原子演练 Campaign plan")
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError("原子演练 Campaign plan 非法") from error
    if (
        plan.get("campaign_id") != campaign_id
        or plan.get("campaign_mode") != "formal"
        or plan.get("campaign_purpose") != "validation_only"
        or plan.get("baseline_version") != "0.151.0"
        or plan.get("target_version") != "0.154.0"
        or plan.get("controls")
        != {
            "timing_checkpoint_sha256": "1" * 64,
            "arm64_environment_sha256": "2" * 64,
            "job_rehearsal_sha256": "3" * 64,
            "p0_gate_sha256": "4" * 64,
        }
    ):
        raise CampaignRunRehearsalError("原子演练 Campaign plan 身份漂移")

    campaign_document = _expect_fields(
        _load_json(campaign_dir / "campaign.json", "原子演练 Campaign 清单"),
        {
            "campaign_id",
            "campaign_mode",
            "campaign_purpose",
            "baseline_version",
            "target_version",
            "vc_control",
            "control_receipts",
        },
        "原子演练 Campaign 清单",
    )
    if campaign_document != {
        "campaign_id": campaign_id,
        "campaign_mode": "formal",
        "campaign_purpose": "validation_only",
        "baseline_version": "0.151.0",
        "target_version": "0.154.0",
        "vc_control": {
            "campaign_plan": {
                "path": "control/vc/campaign-plan.json",
                "sha256": _sha256_file(plan_path),
            }
        },
        "control_receipts": {
            "upgrade_timing": {
                "ledger_dir": str(instance_root / "timing-ledger"),
                "ledger_plan_sha256": _sha256_file(
                    instance_root / "timing-ledger/ledger.json"
                ),
                "upgrade_id": f"{campaign_id}-upgrade",
            }
        },
    }:
        raise CampaignRunRehearsalError("原子演练 Campaign 清单漂移")

    stage_path = _relative_file(
        campaign_dir,
        "control/vc/vc0-stage.json",
        "原子演练 VC-0 阶段事实",
    )
    if _load_json(stage_path, "原子演练 VC-0 阶段事实") != {
        "schema_version": "codex-atomic-vc0-stage/v1",
        "status": "passed",
        "campaign_id": campaign_id,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }:
        raise CampaignRunRehearsalError("原子演练 VC-0 阶段事实漂移")

    vc0_binding, vc0_path = _atomic_bound_file(
        instance_root,
        instance.get("vc0_checkpoint"),
        "campaign/control/vc/vc-0-checkpoint.json",
        "原子演练 VC-0 checkpoint",
        identity_field="checkpoint_sha256",
    )
    try:
        vc0 = codex_upgrade_vc_artifacts.validate_vc_checkpoint(
            _load_json(vc0_path, "原子演练 VC-0 checkpoint"),
            plan,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError("原子演练 VC-0 checkpoint 非法") from error
    if (
        vc0_binding.get("checkpoint_sha256") != vc0.get("checkpoint_sha256")
        or vc0.get("phase") != "VC-0"
        or vc0.get("status") != "complete"
        or vc0.get("predecessor_checkpoint") is not None
        or vc0.get("stage_receipt")
        != {
            "path": "control/vc/vc0-stage.json",
            "sha256": _sha256_file(stage_path),
        }
        or vc0.get("execute_item_ids") != []
        or vc0.get("reuse_item_ids") != []
        or vc0.get("metrics") != {"live_request_count": 0, "scanned_bytes": 0}
    ):
        raise CampaignRunRehearsalError("原子演练 VC-0 checkpoint 语义漂移")

    marker_path = state_dir / "atomic-action.json"
    action = _atomic_action(campaign_id, marker_path)
    action_plan_path = _relative_file(
        campaign_dir,
        "control/vc/vc1-action-plan.json",
        "原子演练 VC-1 action plan",
    )
    try:
        action_plan = codex_upgrade_vc_artifacts.validate_action_plan(
            _load_json(action_plan_path, "原子演练 VC-1 action plan")
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError("原子演练 VC-1 action plan 非法") from error
    if action_plan != {
        "schema_version": codex_upgrade_vc_artifacts.VC_ACTION_PLAN_SCHEMA,
        "execute_item_ids": ["atomic-offline-execute"],
        "reuse_item_ids": ["atomic-offline-reuse"],
        "actions": [action],
    }:
        raise CampaignRunRehearsalError("原子演练 VC-1 action plan 漂移")

    predecessor = {
        "path": "control/vc/vc-0-checkpoint.json",
        "sha256": _sha256_file(vc0_path),
        "phase": "VC-0",
        "checkpoint_sha256": vc0["checkpoint_sha256"],
    }
    _batch_binding, batch_path = _atomic_bound_file(
        instance_root,
        instance.get("vc1_batch"),
        "campaign/control/vc/batches/0001-vc-1.json",
        "原子演练 VC-1 batch",
    )
    try:
        batch = codex_upgrade_vc_artifacts.validate_vc_batch(
            _load_json(batch_path, "原子演练 VC-1 batch"),
            plan,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError("原子演练 VC-1 batch 非法") from error
    if (
        batch.get("sequence") != 1
        or batch.get("phase") != "VC-1"
        or batch.get("predecessor_checkpoint") != predecessor
        or batch.get("execute_item_ids") != ["atomic-offline-execute"]
        or batch.get("reuse_item_ids") != ["atomic-offline-reuse"]
        or batch.get("actions") != [action]
        or batch.get("original_deadline_at_utc")
        != plan.get("original_deadline_at_utc")
    ):
        raise CampaignRunRehearsalError("原子演练 VC-1 batch 语义漂移")

    _manifest_binding, manifest_path = _atomic_bound_file(
        instance_root,
        instance.get("vc1_manifest"),
        "campaign/control/vc/run-manifests/0001-vc-1.json",
        "原子演练 VC-1 manifest",
    )
    try:
        manifest = codex_upgrade_supervisor._campaign_run_manifest(manifest_path)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise CampaignRunRehearsalError("原子演练 VC-1 manifest 非法") from error
    expected_manifest = codex_upgrade_supervisor.build_batched_campaign_run_manifest(
        campaign_id=campaign_id,
        campaign_plan_sha256=str(plan["plan_sha256"]),
        batch_id=str(batch["batch_id"]),
        batch_sequence=1,
        batch_sha256=str(batch["batch_sha256"]),
        phase="VC-1",
        predecessor_checkpoint=predecessor,
        original_deadline_at_utc=str(plan["original_deadline_at_utc"]),
        actions=[action],
        execute_items=["atomic-offline-execute"],
        reuse_items=["atomic-offline-reuse"],
    )
    if manifest != expected_manifest:
        raise CampaignRunRehearsalError("原子演练 VC-1 manifest 未由 batch 确定性编译")

    parent = _expect_fields(
        instance.get("parent_run"),
        {
            "schema_version",
            "batch_id",
            "batch_sequence",
            "original_deadline_at_utc",
            "state",
            "audit_incomplete",
            "event_count",
            "execute_items",
            "reuse_items",
            "run_dir",
        },
        "原子演练父 run",
    )
    run_dir = _private_directory(Path(str(parent.get("run_dir", ""))), "原子演练父 run")
    run_directories = sorted(
        path.resolve()
        for path in state_dir.iterdir()
        if path.name.startswith("run-") and path.is_dir() and not path.is_symlink()
    )
    if run_dir.parent != state_dir or run_directories != [run_dir]:
        raise CampaignRunRehearsalError("原子演练父 run 数量或路径漂移")
    try:
        audit = codex_upgrade_supervisor._audit_command(run_dir)
    except codex_upgrade_supervisor.SupervisorError as error:
        raise CampaignRunRehearsalError("原子演练父 run 无法审计") from error
    expected_parent = {
        "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
        "batch_id": batch["batch_id"],
        "batch_sequence": 1,
        "original_deadline_at_utc": plan["original_deadline_at_utc"],
        "state": "stopped",
        "audit_incomplete": False,
        "event_count": audit["event_count"],
        "execute_items": ["atomic-offline-execute"],
        "reuse_items": ["atomic-offline-reuse"],
        "run_dir": str(run_dir),
    }
    if parent != expected_parent or audit.get("state") != "stopped" or audit.get(
        "audit_incomplete"
    ) is not False:
        raise CampaignRunRehearsalError("原子演练父 run 终态漂移")
    recorded_manifest = _expect_fields(
        _load_json(run_dir / "campaign-run-manifest.json", "父 run 不可变 manifest"),
        {"schema_version", "manifest_sha256", "manifest"},
        "父 run 不可变 manifest",
    )
    if recorded_manifest != {
        "schema_version": codex_upgrade_supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
        "manifest_sha256": _sha256_bytes(
            codex_upgrade_vc_artifacts.canonical_bytes(manifest)
        ),
        "manifest": manifest,
    }:
        raise CampaignRunRehearsalError("父 run 实际执行 manifest 漂移")

    marker_binding, marker_file = _atomic_bound_file(
        instance_root,
        instance.get("action_marker"),
        "state/atomic-action.json",
        "原子演练动作事实",
    )
    if marker_file != marker_path or _load_json(marker_file, "原子演练动作事实") != {
        "schema_version": ACTION_RESULT_SCHEMA,
        "status": "passed",
        "campaign_id": campaign_id,
        "phase": "VC-1",
        "action_id": "atomic-offline-action",
        "parent_run_dir": str(run_dir),
        "network_used": False,
        "live_request_count": 0,
    }:
        raise CampaignRunRehearsalError("原子演练动作事实漂移")

    vc1_binding, vc1_path = _atomic_bound_file(
        instance_root,
        instance.get("vc1_checkpoint"),
        "campaign/control/vc/vc-1-checkpoint.json",
        "原子演练 VC-1 checkpoint",
        identity_field="checkpoint_sha256",
    )
    try:
        vc1 = codex_upgrade_vc_artifacts.validate_vc_checkpoint(
            _load_json(vc1_path, "原子演练 VC-1 checkpoint"),
            plan,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise CampaignRunRehearsalError("原子演练 VC-1 checkpoint 非法") from error
    if (
        vc1_binding.get("checkpoint_sha256") != vc1.get("checkpoint_sha256")
        or vc1.get("phase") != "VC-1"
        or vc1.get("status") != "complete"
        or vc1.get("predecessor_checkpoint") != predecessor
        or vc1.get("stage_receipt")
        != {"path": str(marker_path), "sha256": marker_binding["sha256"]}
        or vc1.get("execute_item_ids") != ["atomic-offline-execute"]
        or vc1.get("reuse_item_ids") != ["atomic-offline-reuse"]
        or vc1.get("metrics") != {"live_request_count": 0, "scanned_bytes": 0}
    ):
        raise CampaignRunRehearsalError("原子演练 VC-1 checkpoint 语义漂移")

    _replay_atomic_permission_closeout(
        instance_root,
        campaign_id,
        instance.get("permission_closeout"),
    )
    timing_value = instance.get("timing_failure_closeout")
    _replay_atomic_timing_failure_closeout(
        instance_root,
        campaign_dir=campaign_dir,
        campaign_id=campaign_id,
        value=timing_value,
    )
    if not isinstance(timing_value, Mapping):
        raise CampaignRunRehearsalError("原子演练失败时间账本字段非法")
    failure_parent = timing_value.get("failure_parent")
    closeout_failure_parent = timing_value.get("closeout_failure_parent")
    if not isinstance(failure_parent, Mapping) or not isinstance(
        closeout_failure_parent, Mapping
    ):
        raise CampaignRunRehearsalError("原子演练失败父结果字段非法")
    failure_run_name = Path(str(failure_parent.get("run_dir", ""))).name
    closeout_failure_run_name = Path(
        str(closeout_failure_parent.get("run_dir", ""))
    ).name
    inventory = _atomic_inventory(instance_root)
    if (
        instance.get("inventory") != inventory
        or {entry["path"] for entry in inventory}
        != _atomic_expected_inventory_paths(
            run_dir.name,
            failure_run_name,
            closeout_failure_run_name,
            campaign_id,
        )
    ):
        raise CampaignRunRehearsalError(f"原子双跑实例 {index} inventory 漂移")


def replay_atomic_double(
    evidence_root: Path,
    receipt_relative: str | Path,
    *,
    require_arm64: bool | None = None,
) -> dict[str, Any]:
    """重放双跑收据、工具身份、隔离关系及完整文件 inventory。"""

    root = _private_directory(evidence_root, "原子双跑证据根")
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise CampaignRunRehearsalError("原子双跑证据根权限必须精确为 0700")
    receipt_path = _relative_file(root, receipt_relative, "原子双跑收据")
    receipt_metadata = _private_file(receipt_path, "原子双跑收据").stat()
    if (
        receipt_path.parent != root
        or stat.S_IMODE(receipt_metadata.st_mode) != 0o600
        or receipt_metadata.st_nlink != 1
    ):
        raise CampaignRunRehearsalError("原子双跑收据位置、权限或硬链接数漂移")
    receipt = _load_json(receipt_path, "原子双跑收据")
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_sha256", None)
    expected_fields = {
        "schema_version",
        "status",
        "collected_at_utc",
        "environment",
        "tool_identity",
        "instances",
        "isolation",
        "live_request_count",
        "scanned_bytes",
        "network_used",
        "receipt_sha256",
    }
    try:
        collected_at = datetime.fromisoformat(
            str(receipt.get("collected_at_utc", "")).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise CampaignRunRehearsalError("原子双跑 collected_at_utc 非法") from error
    normalized_collected_at = collected_at.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if (
        set(receipt) != expected_fields
        or receipt.get("schema_version") != ATOMIC_RECEIPT_SCHEMA
        or receipt.get("status") != "passed"
        or receipt.get("live_request_count") != 0
        or receipt.get("scanned_bytes") != 0
        or receipt.get("network_used") is not False
        or receipt.get("collected_at_utc") != normalized_collected_at
        or digest != codex_upgrade_vc_artifacts.digest(unsigned)
        or receipt.get("tool_identity") != _atomic_tool_identity()
        or receipt.get("isolation")
        != {
            "distinct_campaign_ids": True,
            "distinct_state_dirs": True,
            "second_references_first": False,
        }
    ):
        raise CampaignRunRehearsalError("原子双跑收据身份、自摘要或零请求边界漂移")
    environment = receipt.get("environment")
    if not isinstance(environment, Mapping):
        raise CampaignRunRehearsalError("原子双跑环境字段非法")
    enforce = bool(environment.get("enforced")) if require_arm64 is None else require_arm64
    if environment != _atomic_environment(require_arm64=enforce):
        raise CampaignRunRehearsalError("原子双跑生产环境挂载身份漂移")
    instances = receipt.get("instances")
    if not isinstance(instances, list) or len(instances) != 2:
        raise CampaignRunRehearsalError("原子双跑必须恰好包含两个独立实例")
    expected_top = {"instance-1", "instance-2", receipt_path.name}
    if {path.name for path in root.iterdir()} != expected_top:
        raise CampaignRunRehearsalError("原子双跑证据根含未登记额外文件")
    for index, raw in enumerate(instances, 1):
        _replay_atomic_instance(root, index, raw)
    first_root = str(root / "instance-1").encode("utf-8")
    if any(
        path.is_file() and first_root in path.read_bytes()
        for path in (root / "instance-2").rglob("*")
    ):
        raise CampaignRunRehearsalError("第二次演练引用了第一次状态")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="执行并封存分批演练")
    collect_parser.add_argument("--campaign-dir", type=Path, required=True)
    collect_parser.add_argument("--evidence-root", type=Path, required=True)
    collect_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放分批演练")
    replay_parser.add_argument("--campaign-dir", type=Path, required=True)
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    atomic_collect_parser = commands.add_parser(
        "atomic-double-collect",
        help="仅在 ARM64 capture-cli 内连续执行两次零网络 VC-0→VC-1",
    )
    atomic_collect_parser.add_argument("--evidence-root", type=Path, required=True)
    atomic_collect_parser.add_argument("--output", required=True)
    atomic_replay_parser = commands.add_parser(
        "atomic-double-replay",
        help="仅在 ARM64 capture-cli 内重放原子双跑收据",
    )
    atomic_replay_parser.add_argument("--evidence-root", type=Path, required=True)
    atomic_replay_parser.add_argument("--receipt", required=True)
    worker_parser = commands.add_parser("action-worker", help=argparse.SUPPRESS)
    worker_parser.add_argument("--campaign-id", required=True)
    worker_parser.add_argument("--phase", required=True)
    worker_parser.add_argument("--action-id", required=True)
    worker_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "collect":
            result = collect(
                arguments.evidence_root,
                arguments.output,
                campaign_dir=arguments.campaign_dir,
            )
        elif arguments.command == "replay":
            result = replay(
                arguments.evidence_root,
                arguments.receipt,
                campaign_dir=arguments.campaign_dir,
            )
        elif arguments.command == "atomic-double-collect":
            result = collect_atomic_double(
                arguments.evidence_root,
                arguments.output,
                require_arm64=True,
            )
        elif arguments.command == "atomic-double-replay":
            result = replay_atomic_double(
                arguments.evidence_root,
                arguments.receipt,
                require_arm64=True,
            )
        else:
            result = _action_worker(arguments)
    except (
        OSError,
        subprocess.SubprocessError,
        CampaignRunRehearsalError,
        RuntimeError,
        ValueError,
    ) as error:
        print(f"Codex campaign-run 演练失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "campaign_id": result.get("campaign_id"),
                "live_request_count": result.get("live_request_count", 0),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
