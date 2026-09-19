#!/usr/bin/env python3
"""封存已编译但尚未创建父 run 的 Codex VC 批次。

本工具只记录控制面停线事实，不执行批次动作、不读取抓包正文，也不能作为
任何后继批次的 predecessor。独立调用会非阻塞取得 ``.campaign-run.lock``；
原子编译派发入口已经持锁时，使用内部 locked API 写入同一份不可覆盖收据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts


SCHEMA_VERSION = "codex-upgrade-predispatch-stop/v1"
NEXT_ACTION = "new-vc-0-after-tool-closure"
FAILURE_KINDS = frozenset(
    {
        "compile-failed-after-artifact-write",
        "dispatch-before-parent-run",
        "interrupted-before-parent-run",
        "start-window-expired",
        "deadline-expired",
        "operator-recovery",
    }
)
SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")


class PredispatchStopError(RuntimeError):
    """预派发停线输入、历史或不可变收据不可信。"""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise PredispatchStopError(f"{label}必须是非符号链接绝对目录。")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PredispatchStopError(f"{label}必须由当前用户拥有且权限为 0700。")
    return resolved


def _private_file(path: Path, label: str) -> bytes:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise PredispatchStopError(f"{label}必须是非符号链接绝对普通文件。")
    metadata = path.stat()
    if (
        metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise PredispatchStopError(
            f"{label}必须由当前用户拥有、权限为 0600 且只有一个硬链接。"
        )
    return path.read_bytes()


def _load_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PredispatchStopError(f"{label}不是有效 JSON。") from error
    if not isinstance(payload, dict):
        raise PredispatchStopError(f"{label}必须是 JSON 对象。")
    return payload


def _inside(root: Path, path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise PredispatchStopError(f"{label}越过 Campaign 根或不存在。") from error
    return resolved


def _binding(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
    }


def _load_campaign_contract(
    campaign_dir: Path,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    campaign_path = campaign_dir / "campaign.json"
    campaign = _load_json(_private_file(campaign_path, "Campaign 清单"), "Campaign 清单")
    control = campaign.get("vc_control")
    reference = control.get("campaign_plan") if isinstance(control, Mapping) else None
    if (
        campaign.get("campaign_mode") != "formal"
        or not isinstance(reference, Mapping)
        or set(reference) != {"path", "sha256"}
        or not isinstance(reference.get("path"), str)
        or not isinstance(reference.get("sha256"), str)
    ):
        raise PredispatchStopError("Campaign 未冻结 Formal VC 总计划。")
    plan_path = _inside(campaign_dir, campaign_dir / reference["path"], "Campaign 总计划")
    plan_raw = _private_file(plan_path, "Campaign 总计划")
    if _sha256_bytes(plan_raw) != reference["sha256"]:
        raise PredispatchStopError("Campaign 总计划文件摘要漂移。")
    try:
        plan = vc_artifacts.validate_campaign_plan(_load_json(plan_raw, "Campaign 总计划"))
    except vc_artifacts.VCArtifactError as error:
        raise PredispatchStopError(str(error)) from error
    if (
        plan.get("campaign_id") != campaign.get("campaign_id")
        or plan.get("campaign_mode") != campaign.get("campaign_mode")
        or plan.get("campaign_purpose") != campaign.get("campaign_purpose")
        or plan.get("baseline_version") != campaign.get("baseline_version")
        or plan.get("target_version") != campaign.get("target_version")
    ):
        raise PredispatchStopError("Campaign 总计划与 Campaign 身份漂移。")
    return campaign, plan_path, plan


def _load_batch_and_manifest(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    batch_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    batch_path = _inside(campaign_dir, batch_path, "VC batch")
    manifest_path = _inside(campaign_dir, manifest_path, "campaign-run 清单")
    batch_raw = _private_file(batch_path, "VC batch")
    manifest_raw = _private_file(manifest_path, "campaign-run 清单")
    try:
        batch = vc_artifacts.validate_vc_batch(_load_json(batch_raw, "VC batch"), plan)
        manifest = supervisor._campaign_run_manifest(manifest_path)
    except (vc_artifacts.VCArtifactError, supervisor.SupervisorError) as error:
        raise PredispatchStopError(str(error)) from error
    expected_name = f"{int(batch['sequence']):04d}-{str(batch['phase']).lower()}.json"
    if (
        batch_path != campaign_dir / "control" / "vc" / "batches" / expected_name
        or manifest_path
        != campaign_dir / "control" / "vc" / "run-manifests" / expected_name
    ):
        raise PredispatchStopError("VC batch 或 campaign-run 清单不在规范路径。")
    # v2 manifest 与 batch 使用同一动作结构，包含 item_ids。此前删除该字段
    # 会让所有非 no-op 的真实 batch 被误判为“非确定性编译”，并导致原子
    # 入口在首个派发错误后连停线收据也无法写入。
    expected_actions = [dict(action) for action in batch["actions"]]
    if (
        manifest.get("schema_version") != supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA
        or manifest.get("campaign_id") != batch.get("campaign_id")
        or manifest.get("campaign_plan_sha256") != batch.get("campaign_plan_sha256")
        or manifest.get("batch_id") != batch.get("batch_id")
        or manifest.get("batch_sequence") != batch.get("sequence")
        or manifest.get("batch_sha256") != batch.get("batch_sha256")
        or manifest.get("phase") != batch.get("phase")
        or manifest.get("predecessor_checkpoint")
        != batch.get("predecessor_checkpoint")
        or manifest.get("original_deadline_at_utc")
        != batch.get("original_deadline_at_utc")
        or manifest.get("actions") != expected_actions
        or manifest.get("execute_items") != batch.get("execute_item_ids")
        or manifest.get("reuse_items") != batch.get("reuse_item_ids")
        or manifest.get("no_op") != (not batch.get("actions"))
    ):
        raise PredispatchStopError("campaign-run 清单未由目标 VC batch 确定性编译。")
    return batch, manifest


def _history_summary(
    state_dir: Path,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        history = supervisor._campaign_run_history(
            state_dir,
            str(manifest["campaign_id"]),
        )
    except supervisor.SupervisorError as error:
        raise PredispatchStopError(str(error)) from error
    sequence = int(manifest["batch_sequence"])
    ordered = sorted(history, key=lambda item: int(item[1].get("batch_sequence", -1)))
    actual_sequences = [item[1].get("batch_sequence") for item in ordered]
    if actual_sequences != list(range(1, sequence)):
        raise PredispatchStopError(
            "预派发停线前的父 run 序号不连续或目标 batch 已有父 run。"
        )
    summaries: list[dict[str, Any]] = []
    for state, prior, run_dir in ordered:
        if (
            # 续接／收尾条款及其 run schema 已于 2026-09-16 删除；新 Campaign 的父
            # run 只可能是批次或 v3 恢复两种形态。引用已删除常量会让停线收据永远
            # 写不出来，派发前失败因此无法封口（v10 accept 重派时实际发生）。
            prior.get("schema_version")
            not in {
                supervisor.CAMPAIGN_RUN_BATCHED_SCHEMA,
                supervisor.CAMPAIGN_RUN_RECOVERY_SCHEMA,
            }
            or prior.get("campaign_plan_sha256")
            != manifest.get("campaign_plan_sha256")
            or prior.get("original_deadline_at_utc")
            != manifest.get("original_deadline_at_utc")
            or state.get("state") not in supervisor.TERMINAL_STATES
        ):
            raise PredispatchStopError("预派发停线前的 Campaign 历史身份或终态非法。")
        if (
            prior.get("batch_id") == manifest.get("batch_id")
            or prior.get("batch_sha256") == manifest.get("batch_sha256")
        ):
            raise PredispatchStopError("目标 batch 已创建父 run，不能记录预派发停线。")
        summaries.append(
            {
                "batch_sequence": prior["batch_sequence"],
                "batch_id": prior["batch_id"],
                "batch_sha256": prior["batch_sha256"],
                "run_name": run_dir.name,
                "state": state["state"],
                "state_file_sha256": _file_sha256(run_dir / "state.json"),
                "manifest_file_sha256": _file_sha256(
                    run_dir / "campaign-run-manifest.json"
                ),
            }
        )
    return summaries


def _timing_state(batch: Mapping[str, Any], now: datetime) -> str:
    start_by = datetime.fromisoformat(
        str(batch["must_start_by_utc"]).replace("Z", "+00:00")
    )
    deadline = datetime.fromisoformat(
        str(batch["original_deadline_at_utc"]).replace("Z", "+00:00")
    )
    if now > deadline:
        return "deadline-expired"
    if now > start_by:
        return "start-window-expired"
    return "within-start-window"


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise PredispatchStopError(f"{label}必须是带时区时间字符串。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PredispatchStopError(f"{label}不是有效时间。") from error
    if parsed.tzinfo is None:
        raise PredispatchStopError(f"{label}缺少时区。")
    normalized = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    if value != normalized:
        raise PredispatchStopError(f"{label}不是规范 UTC 毫秒格式。")
    return parsed


def _validate_failure(failure_kind: Any, error_type: Any) -> None:
    if not isinstance(failure_kind, str) or failure_kind not in FAILURE_KINDS:
        raise PredispatchStopError("预派发停线 failure_kind 非法。")
    if not isinstance(error_type, str) or not SAFE_ERROR_TYPE_RE.fullmatch(error_type):
        raise PredispatchStopError("预派发停线 error_type 非法。")


def _write_once(
    path: Path,
    payload: Mapping[str, Any],
    *,
    campaign_dir: Path,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = _private_directory(path.parent, "预派发停线收据目录")
    expected_parent = campaign_dir / "control" / "vc" / "predispatch-stops"
    if parent != expected_parent:
        raise PredispatchStopError("预派发停线收据目录包含符号链接或越过 Campaign。")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise PredispatchStopError("预派发停线收据输出身份非法。")
        supervisor._write_all(descriptor, vc_artifacts.canonical_bytes(payload))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    supervisor._fsync_directory(path.parent)


def _record_locked(
    *,
    campaign_dir: Path,
    state_dir: Path,
    batch_path: Path,
    manifest_path: Path,
    failure_kind: str,
    error_type: str,
) -> tuple[Path, dict[str, Any]]:
    """在调用方已持有 ``.campaign-run.lock`` 时写入停线收据。"""

    _reject_staging_model_campaign(campaign_dir)
    _validate_failure(failure_kind, error_type)
    campaign_dir = _private_directory(campaign_dir, "Campaign 目录")
    state_dir = _private_directory(state_dir, "监督器 state-dir")
    campaign, plan_path, plan = _load_campaign_contract(campaign_dir)
    batch_path = _inside(campaign_dir, batch_path, "VC batch")
    manifest_path = _inside(campaign_dir, manifest_path, "campaign-run 清单")
    batch, manifest = _load_batch_and_manifest(
        campaign_dir,
        plan,
        batch_path,
        manifest_path,
    )
    history = _history_summary(state_dir, manifest)
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "stopped",
        "created_at_utc": now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "campaign_id": campaign["campaign_id"],
        "campaign_plan": {
            **_binding(campaign_dir, plan_path),
            "plan_sha256": plan["plan_sha256"],
        },
        "batch": {
            **_binding(campaign_dir, batch_path),
            "batch_id": batch["batch_id"],
            "batch_sequence": batch["sequence"],
            "batch_sha256": batch["batch_sha256"],
            "phase": batch["phase"],
            "compiled_at_utc": batch["compiled_at_utc"],
            "must_start_by_utc": batch["must_start_by_utc"],
            "original_deadline_at_utc": batch["original_deadline_at_utc"],
        },
        "campaign_run_manifest": {
            **_binding(campaign_dir, manifest_path),
            "schema_version": manifest["schema_version"],
        },
        "state_dir": str(state_dir),
        "prior_runs": history,
        "source_failure": {
            "kind": failure_kind,
            "error_type": error_type,
        },
        "timing_state": _timing_state(batch, now),
        "source_run_created": False,
        "successor_eligible": False,
        "metrics": {
            "live_request_count": 0,
            "scanned_bytes": 0,
        },
        "next_action": NEXT_ACTION,
    }
    payload["receipt_sha256"] = vc_artifacts.digest(payload)
    receipt_path = (
        campaign_dir
        / "control"
        / "vc"
        / "predispatch-stops"
        / batch_path.name
    )
    try:
        _write_once(receipt_path, payload, campaign_dir=campaign_dir)
    except FileExistsError as error:
        raise PredispatchStopError(
            f"预派发停线收据已经存在，禁止覆盖：{receipt_path}"
        ) from error
    return receipt_path, payload


def _reject_staging_model_campaign(campaign_dir: Path) -> None:
    """改造 4：staging 模型 Campaign 不再产生 predispatch-stop/v1；历史收据只读重放不变。"""

    vc_root = Path(campaign_dir) / "control" / "vc"
    if (vc_root / "staging").exists() or (vc_root / "commits").exists():
        raise PredispatchStopError(
            "staging 模型 Campaign 不再产生预派发停线收据；派发前失败由 staging ABORT 与对账登记。"
        )
    try:
        model = supervisor.campaign_batch_model(Path(campaign_dir))
    except supervisor.SupervisorError as error:
        raise PredispatchStopError(str(error)) from error
    if model == "staging":
        raise PredispatchStopError(
            "staging 模型 Campaign 不再产生预派发停线收据；派发前失败由 staging ABORT 与对账登记。"
        )


def record(
    *,
    campaign_dir: Path,
    state_dir: Path,
    batch_path: Path,
    manifest_path: Path,
    failure_kind: str,
    error_type: str,
) -> tuple[Path, dict[str, Any]]:
    """非阻塞取锁并封存一份通用预派发停线收据。"""

    _reject_staging_model_campaign(campaign_dir)
    try:
        descriptor, locked_state_dir = supervisor._campaign_run_lock(state_dir)
    except supervisor.SupervisorError as error:
        raise PredispatchStopError(str(error)) from error
    try:
        return _record_locked(
            campaign_dir=campaign_dir,
            state_dir=locked_state_dir,
            batch_path=batch_path,
            manifest_path=manifest_path,
            failure_kind=failure_kind,
            error_type=error_type,
        )
    finally:
        os.close(descriptor)


def replay(
    *,
    campaign_dir: Path,
    state_dir: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    """重放收据及其当前文件、父 run 历史和零请求边界。"""

    campaign_dir = _private_directory(campaign_dir, "Campaign 目录")
    receipt_path = _inside(campaign_dir, receipt_path, "预派发停线收据")
    receipt = _load_json(
        _private_file(receipt_path, "预派发停线收据"),
        "预派发停线收据",
    )
    required = {
        "schema_version",
        "status",
        "created_at_utc",
        "campaign_id",
        "campaign_plan",
        "batch",
        "campaign_run_manifest",
        "state_dir",
        "prior_runs",
        "source_failure",
        "timing_state",
        "source_run_created",
        "successor_eligible",
        "metrics",
        "next_action",
        "receipt_sha256",
    }
    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_sha256", None)
    created_at = _timestamp(receipt.get("created_at_utc"), "预派发停线创建时间")
    source_failure = receipt.get("source_failure")
    if not isinstance(source_failure, Mapping) or set(source_failure) != {
        "kind",
        "error_type",
    }:
        raise PredispatchStopError("预派发停线 source_failure 字段不闭合。")
    _validate_failure(source_failure.get("kind"), source_failure.get("error_type"))
    if (
        set(receipt) != required
        or receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("status") != "stopped"
        or receipt.get("state_dir") != str(state_dir.resolve(strict=True))
        or receipt.get("source_run_created") is not False
        or receipt.get("successor_eligible") is not False
        or receipt.get("metrics")
        != {"live_request_count": 0, "scanned_bytes": 0}
        or receipt.get("next_action") != NEXT_ACTION
        or digest != vc_artifacts.digest(unsigned)
    ):
        raise PredispatchStopError("预派发停线收据字段、自摘要或零请求边界漂移。")
    batch_binding = receipt.get("batch")
    manifest_binding = receipt.get("campaign_run_manifest")
    if not isinstance(batch_binding, Mapping) or not isinstance(
        manifest_binding, Mapping
    ):
        raise PredispatchStopError("预派发停线收据缺少 batch 或 manifest 绑定。")
    descriptor, locked_state_dir = supervisor._campaign_run_lock(state_dir)
    try:
        campaign, plan_path, plan = _load_campaign_contract(campaign_dir)
        batch_path = _inside(
            campaign_dir,
            campaign_dir / str(batch_binding.get("path", "")),
            "VC batch",
        )
        manifest_path = _inside(
            campaign_dir,
            campaign_dir / str(manifest_binding.get("path", "")),
            "campaign-run 清单",
        )
        batch, manifest = _load_batch_and_manifest(
            campaign_dir,
            plan,
            batch_path,
            manifest_path,
        )
        expected_receipt_path = (
            campaign_dir
            / "control"
            / "vc"
            / "predispatch-stops"
            / batch_path.name
        )
        if (
            receipt_path != expected_receipt_path
            or receipt.get("campaign_id") != campaign.get("campaign_id")
            or receipt.get("campaign_plan")
            != {**_binding(campaign_dir, plan_path), "plan_sha256": plan["plan_sha256"]}
            or batch_binding
            != {
                **_binding(campaign_dir, batch_path),
                "batch_id": batch["batch_id"],
                "batch_sequence": batch["sequence"],
                "batch_sha256": batch["batch_sha256"],
                "phase": batch["phase"],
                "compiled_at_utc": batch["compiled_at_utc"],
                "must_start_by_utc": batch["must_start_by_utc"],
                "original_deadline_at_utc": batch["original_deadline_at_utc"],
            }
            or manifest_binding
            != {
                **_binding(campaign_dir, manifest_path),
                "schema_version": manifest["schema_version"],
            }
            or receipt.get("prior_runs")
            != _history_summary(locked_state_dir, manifest)
            or receipt.get("timing_state") != _timing_state(batch, created_at)
        ):
            raise PredispatchStopError("预派发停线收据文件绑定或父 run 历史漂移。")
    except supervisor.SupervisorError as error:
        raise PredispatchStopError(str(error)) from error
    finally:
        os.close(descriptor)
    return receipt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record_parser = commands.add_parser("record", help="封存未派发批次的停线事实")
    replay_parser = commands.add_parser("replay", help="重放未派发批次的停线事实")
    for target in (record_parser, replay_parser):
        target.add_argument("--campaign-dir", required=True, type=Path)
        target.add_argument("--state-dir", required=True, type=Path)
    record_parser.add_argument("--batch", required=True, type=Path)
    record_parser.add_argument("--manifest", required=True, type=Path)
    record_parser.add_argument("--failure-kind", required=True, choices=sorted(FAILURE_KINDS))
    record_parser.add_argument("--error-type", required=True)
    replay_parser.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "record":
            path, receipt = record(
                campaign_dir=arguments.campaign_dir,
                state_dir=arguments.state_dir,
                batch_path=arguments.batch,
                manifest_path=arguments.manifest,
                failure_kind=arguments.failure_kind,
                error_type=arguments.error_type,
            )
            result = {"receipt": str(path), **receipt}
        else:
            result = replay(
                campaign_dir=arguments.campaign_dir,
                state_dir=arguments.state_dir,
                receipt_path=arguments.receipt,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, PredispatchStopError, supervisor.SupervisorError) as error:
        print(f"Codex 预派发停线失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
