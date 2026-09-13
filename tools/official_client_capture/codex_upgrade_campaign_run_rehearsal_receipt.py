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
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts


RECEIPT_SCHEMA = "codex-p0-campaign-run-rehearsal/v1"
EXECUTIONS_SCHEMA = "codex-p0-campaign-run-executions/v1"
ACTION_RESULT_SCHEMA = "codex-p0-campaign-run-action-result/v1"
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
        else:
            result = _action_worker(arguments)
    except (
        OSError,
        subprocess.SubprocessError,
        CampaignRunRehearsalError,
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
