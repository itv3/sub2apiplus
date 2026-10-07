"""Claude Campaign 的 VC 控制制品：B2-07 只落实 VC-1 checkpoint 必需的 Campaign 级最小部分（其余归 B3-01）。

合同与 Codex 相同，全部经共享模块 ``codex_upgrade_vc_artifacts``：总计划 ``codex-upgrade-campaign-plan/v1``（由 VC-0 收口
写入 ``control/vc/campaign-plan.json``，总计划 ``vc_control.campaign_plan`` 绑定）、阶段 checkpoint
``codex-upgrade-vc-checkpoint/v1``（``control/vc/<阶段>-checkpoint.json``，VC-0 由收口写入）。

本模块的边界（交给 B3-01 接手时以此为准）：
* 只处理 Campaign 级阶段 VC-0～VC-3；候选级阶段 VC-4～VC-6 的 revision 目录、VC-5 重开目录不在这里，传入即拒绝。
* ``complete_campaign_phase`` 只写一次：已存在时除 ``completed_at_utc`` 与自摘要外逐字段比较，相同即复用（重跑收敛），
  不同即拒绝（阶段输入漂移）——与 Codex ``_complete_vc_phase`` 同一判据；完成时间由调用方给冻结值，重跑逐字相同。
* 编排器服务面成员 ``_vc_campaign_plan``、``_vc_checkpoint_path``、``_vc_checkpoint_reference``、``_replay_vc_checkpoint``
  仍是待实现（B3-01）：共享流程怎样经服务面消费 checkpoint 由 B3-01 定，届时可直接转调本模块。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256

CAMPAIGN_PHASES = ("VC-0", "VC-1", "VC-2", "VC-3")
VC_DIRECTORY = Path("control") / "vc"


def _campaign_relative(campaign_dir: Path, path: Path, label: str) -> str:
    try:
        return path.resolve(strict=True).relative_to(Path(campaign_dir).resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"{label}必须是 Campaign 目录内的现有文件：{path}") from error


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{label}缺失或路径不可信：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"无法读取{label}：{error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label}必须是 JSON 对象。")
    return payload


def vc_control_binding(campaign_dir: Path, path: Path) -> dict[str, str]:
    """VC 控制制品的文件绑定：Campaign 相对路径与摘要。"""

    return {"path": _campaign_relative(campaign_dir, path, "VC 控制制品"), "sha256": file_sha256(path)}


def vc_campaign_plan(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """读取并重放总计划 ``vc_control.campaign_plan`` 绑定的 VC 总计划。"""

    control = manifest.get("vc_control")
    reference = control.get("campaign_plan") if isinstance(control, Mapping) else None
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise ConfigurationError("Campaign 缺少 vc_control.campaign_plan 绑定（VC-0 收口写入）。")
    path = Path(campaign_dir) / str(reference["path"])
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference["sha256"]:
        raise ConfigurationError("VC 总计划摘要漂移或路径不可信。")
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(_read_json(path, "VC 总计划"))
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if plan["campaign_id"] != manifest.get("campaign_id"):
        raise ConfigurationError("VC 总计划不属于本 Campaign。")
    return plan


def vc_checkpoint_path(campaign_dir: Path, phase: str) -> Path:
    """Campaign 级阶段 checkpoint 的固定路径；候选级阶段归 B3-01。"""

    if phase not in CAMPAIGN_PHASES:
        raise ConfigurationError(f"{phase} 不是 Campaign 级阶段：候选级 checkpoint（revision 目录）由 B3-01 落实。")
    return Path(campaign_dir) / VC_DIRECTORY / f"{phase.lower()}-checkpoint.json"


def vc_checkpoint_reference(campaign_dir: Path, path: Path, checkpoint: Mapping[str, Any]) -> dict[str, str]:
    """后继阶段绑定前序 checkpoint 的引用：文件绑定加内部身份（与 Codex 同形）。"""

    return {
        **vc_control_binding(campaign_dir, path),
        "phase": str(checkpoint["phase"]),
        "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
    }


def replay_vc_checkpoint(campaign_dir: Path, plan: Mapping[str, Any], phase: str) -> tuple[Path, dict[str, Any]]:
    """重放一个 Campaign 级阶段 checkpoint：合同、完成状态、阶段收据摘要与直接前序绑定，逐级回溯到 VC-0。"""

    path = vc_checkpoint_path(campaign_dir, phase)
    try:
        checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(_read_json(path, f"{phase} checkpoint"), plan)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if checkpoint["phase"] != phase or checkpoint["status"] != "complete":
        raise ConfigurationError(f"{phase} checkpoint 尚未成功封存。")
    stage = checkpoint["stage_receipt"]
    raw = Path(str(stage.get("path", "")))
    stage_path = raw if raw.is_absolute() else Path(campaign_dir) / raw
    if stage_path.is_symlink() or not stage_path.is_file() or file_sha256(stage_path) != stage.get("sha256"):
        raise ConfigurationError(f"{phase} checkpoint 的阶段收据摘要漂移。")
    if phase != "VC-0":
        previous = CAMPAIGN_PHASES[CAMPAIGN_PHASES.index(phase) - 1]
        previous_path, previous_checkpoint = replay_vc_checkpoint(campaign_dir, plan, previous)
        if checkpoint["predecessor_checkpoint"] != vc_checkpoint_reference(campaign_dir, previous_path, previous_checkpoint):
            raise ConfigurationError(f"{phase} checkpoint 的直接前序绑定漂移。")
    return path, checkpoint


def _comparable(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in checkpoint.items() if key not in {"completed_at_utc", "checkpoint_sha256"}}


def complete_campaign_phase(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    stage_receipt_path: Path,
    completed_at_utc: str,
    execute_item_ids: Iterable[str],
    reuse_item_ids: Iterable[str],
    live_request_count: int,
    scanned_bytes: int,
) -> dict[str, Any]:
    """写 Campaign 级阶段 checkpoint（VC-1～VC-3）：只写一次，重跑同输入复用、输入漂移拒绝。"""

    if phase == "VC-0":
        raise ConfigurationError("VC-0 checkpoint 只由 VC-0 收口写入。")
    output = vc_checkpoint_path(campaign_dir, phase)
    plan = vc_campaign_plan(campaign_dir, manifest)
    previous = CAMPAIGN_PHASES[CAMPAIGN_PHASES.index(phase) - 1]
    previous_path, previous_checkpoint = replay_vc_checkpoint(campaign_dir, plan, previous)
    stage_binding = vc_control_binding(campaign_dir, Path(stage_receipt_path))
    try:
        checkpoint = codex_upgrade_vc_artifacts.build_vc_checkpoint(
            campaign_plan=plan,
            phase=phase,
            status="complete",
            predecessor_checkpoint=vc_checkpoint_reference(campaign_dir, previous_path, previous_checkpoint),
            stage_receipt=stage_binding,
            completed_at_utc=completed_at_utc,
            execute_item_ids=list(execute_item_ids),
            reuse_item_ids=list(reuse_item_ids),
            live_request_count=int(live_request_count),
            scanned_bytes=int(scanned_bytes),
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if output.exists() or output.is_symlink():
        existing = _read_json(output, f"既有 {phase} checkpoint")
        if _comparable(existing) != _comparable(checkpoint):
            raise ConfigurationError(f"{phase} checkpoint 已存在但阶段输入发生漂移。")
        try:
            return codex_upgrade_vc_artifacts.validate_vc_checkpoint(existing, plan)
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
    ensure_private_directory(output.parent, Path(campaign_dir))
    write_json_once(output, checkpoint)
    return checkpoint


def write_json_once(path: Path, payload: Mapping[str, Any]) -> None:
    """以硬链接发布临时文件：跨进程原子、不可覆盖（与编排器 ``_secure_write_json_once`` 同一做法）。"""

    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ConfigurationError(f"不可变文件已经存在，禁止覆盖：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
