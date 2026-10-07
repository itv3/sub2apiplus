"""Claude Campaign 的 VC 控制制品（Claude 升级控制面设计 B2-07 起用，B3-01 补齐原子批次入口所需的部分）。

合同与 Codex 相同，全部经共享模块 ``codex_upgrade_vc_artifacts``：总计划 ``codex-upgrade-campaign-plan/v1``（由 VC-0 收口
写入 ``control/vc/campaign-plan.json``，总计划 ``vc_control.campaign_plan`` 绑定）、阶段 checkpoint
``codex-upgrade-vc-checkpoint/v1``、VC 批次 ``codex-upgrade-vc-batch/v3``；父监督器队列清单是 Claude 的新合同
``official-client-campaign-run/v1``（v2 结构加 ``client``，由监督器同一个解析器校验）。编排器服务面成员
``_vc_campaign_plan``、``_vc_checkpoint_path``、``_vc_checkpoint_reference``、``_replay_vc_checkpoint``、
``_vc_run_manifest_from_batch`` 由 ``claude_upgrade`` 转调本模块（B3-01）。

一、checkpoint 坐标（``vc_checkpoint_path``，与 Codex ``_vc_checkpoint_path`` 同一布局，测试按同一目录逐项对照）
=========================================================================================================
* Campaign 级阶段 VC-0～VC-3：``control/vc/<阶段>-checkpoint.json``，``revision`` 不参与；
* 候选级阶段 VC-4～VC-6：r1 与未给出 revision（历史隐含 r1）在原路径；r≥2 在 ``control/vc/revisions/r<N>/``；
* VC-5 在当前 revision 重开过（计时账本摘要 ``vc5_reopened_baseline`` 非空、``current_revision`` 等于该 revision）时落在
  同目录的 ``reopen-b<K>/``，与首次完成的只写一次制品并存（评估基线与重开由 B4-04／B4-05 产生，这里只按账本解析）。

二、重放（``replay_vc_checkpoint``）
==================================
合同、完成状态、阶段收据摘要与直接前序绑定逐级回溯到 VC-0：VC-4 的直接前序是 Campaign 级 VC-3，VC-5／VC-6 的前序是同一
revision 的上一阶段——与 Codex ``_replay_vc_checkpoint`` 同一判据。阶段收据路径可以是 Campaign 相对路径，也可以是绝对路径
（Formal 的 VC-0 收据绑定 P0 证据根里的门禁收据）；相对路径不得越出 Campaign 目录。

三、写入
========
* ``complete_campaign_phase`` 只写 Campaign 级阶段 VC-1～VC-3 的 checkpoint：已存在时除 ``completed_at_utc`` 与自摘要外
  逐字段比较，相同即复用（重跑收敛），不同即拒绝（阶段输入漂移）——与 Codex ``_complete_vc_phase`` 同一判据；完成时间由
  调用方给冻结值，重跑逐字相同。
* ``complete_candidate_phase`` 写候选级阶段 VC-4～VC-6 的 checkpoint，判据相同，按调用方显式给出的候选 revision 定位
  （B3-04 起 VC-4 由 ``record-candidate-build`` 在构建收据写出之后调用；VC-5／VC-6 随 B4）。
* ``write_first_formal_batch`` 写 VC-0 交接链的首批两件：``control/vc/batches/0001-vc-1.json`` 与同序号的队列清单，只写一次，
  返回两条文件绑定，供 VC-0 收口（B2-11）写进总计划 ``vc_control.first_formal_batch``／``first_campaign_run_manifest``。
  首批的内容由建 Campaign 决定（B2-11，``claude_upgrade_campaign`` 第六节：采集路径恰一个 ``capture-official`` 动作，只读
  复用前序是零请求 no-op），这里只保证批次与清单的确定性编译关系。

四、VC 控制制品语义（``validate_vc_control``，总计划加载时调用）
==============================================================
``vc_control`` 只接受四个字段：``campaign_plan``、``vc0_checkpoint``、``first_formal_batch``、``first_campaign_run_manifest``。
总计划存在时：身份五项（Campaign ID、模式、用途、基线与目标版本）与总计划相同，批次模型必须是 ``staging``（Claude 不支持
legacy）；VC-0 checkpoint 绑定存在时必须指向规范坐标、完成、零请求零扫描；首批两件必须同时出现，批次是从 VC-0 checkpoint
编译的 VC-1 序号 1，队列清单与 ``vc_run_manifest_from_batch`` 对它的确定性编译逐字相同。总计划与控制收据（P0、计时、
ARM64、Job 演练）的摘要对照、VC-0 阶段收据与 P0 门禁收据的对照在 ``claude_upgrade_campaign
.verify_vc_control_correspondence``（B2-11，建 Campaign 自检时核）。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger
from tools.official_client_capture import codex_upgrade_vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256

VC_PHASES = tuple(codex_upgrade_vc_artifacts.VC_PHASES)
CAMPAIGN_PHASES = ("VC-0", "VC-1", "VC-2", "VC-3")
CANDIDATE_PHASES = tuple(codex_upgrade_vc_artifacts.CANDIDATE_PHASES)
VC_DIRECTORY = Path("control") / "vc"
# 与 Codex 编排器同值：候选 revision 目录与 VC-5 重开目录前缀（共用布局，测试对照）。
REVISIONS_DIRNAME = "revisions"
VC5_REOPEN_DIRNAME_PREFIX = "reopen-b"
VC_CONTROL_FIELDS = ("campaign_plan", "vc0_checkpoint", "first_formal_batch", "first_campaign_run_manifest")
FIRST_BATCH_NAME = "0001-vc-1.json"
# Claude Campaign 只有 staging 批次模型（总计划 ``batch_model``）；legacy 是 Codex 改造 4 之前的历史形态。
STAGING_BATCH_MODEL = "staging"


def _campaign_relative(campaign_dir: Path, path: Path, label: str) -> str:
    try:
        return path.resolve(strict=True).relative_to(Path(campaign_dir).resolve(strict=True)).as_posix()
    except (OSError, ValueError) as error:
        raise ConfigurationError(f"{label}必须是 Campaign 目录内的现有文件：{path}") from error


def _campaign_file(campaign_dir: Path, relative: str, label: str) -> Path:
    """Campaign 相对路径 → 绝对路径；空串、绝对路径与 ``..`` 段一律拒绝（与编排器 ``_campaign_file`` 同一判据）。"""

    parsed = PurePosixPath(str(relative))
    if not str(relative) or parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ConfigurationError(f"{label}路径不规范：{relative!r}")
    return Path(campaign_dir) / Path(*parsed.parts)


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


def _bound_file(campaign_dir: Path, reference: Any, label: str) -> Path:
    """``{path, sha256}`` 绑定 → 现存普通文件，摘要必须相同。"""

    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise ConfigurationError(f"{label}绑定必须恰好是 {{path, sha256}}。")
    path = _campaign_file(campaign_dir, str(reference["path"]), label)
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference["sha256"]:
        raise ConfigurationError(f"{label}摘要漂移或路径不可信。")
    return path


def vc_control_binding(campaign_dir: Path, path: Path) -> dict[str, str]:
    """VC 控制制品的文件绑定：Campaign 相对路径与摘要。"""

    return {"path": _campaign_relative(campaign_dir, path, "VC 控制制品"), "sha256": file_sha256(path)}


def vc_campaign_plan(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """读取并重放总计划 ``vc_control.campaign_plan`` 绑定的 VC 总计划（服务面 ``_vc_campaign_plan``）。"""

    control = manifest.get("vc_control")
    reference = control.get("campaign_plan") if isinstance(control, Mapping) else None
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise ConfigurationError("Campaign 缺少 vc_control.campaign_plan 绑定（VC-0 收口写入）。")
    path = _campaign_file(campaign_dir, str(reference["path"]), "VC 总计划")
    if path.is_symlink() or not path.is_file() or file_sha256(path) != reference["sha256"]:
        raise ConfigurationError("VC 总计划摘要漂移或路径不可信。")
    try:
        plan = codex_upgrade_vc_artifacts.validate_campaign_plan(_read_json(path, "VC 总计划"))
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if plan["campaign_id"] != manifest.get("campaign_id"):
        raise ConfigurationError("VC 总计划不属于本 Campaign。")
    return plan


# ---------------------------------------------------------------------------
# 一、checkpoint 坐标
# ---------------------------------------------------------------------------


def candidate_revision_dir(campaign_dir: Path, revision: int) -> Path:
    """候选 revision 目录（与编排器 ``_candidate_revision_dir`` 同一坐标）。"""

    return Path(campaign_dir) / VC_DIRECTORY / REVISIONS_DIRNAME / f"r{int(revision)}"


def campaign_ledger_summary(campaign_dir: Path) -> dict[str, Any] | None:
    """轻量读取 Campaign 绑定的计时账本摘要：没有总计划或没有账本绑定时为 None，账本损坏失败关闭。

    与 Codex ``_campaign_ledger_summary_for_baseline`` 同一口径；Claude 没有控制纪元，只读总计划
    ``control_receipts.upgrade_timing``（解析与校验交给编排器 ``_campaign_timing_ledger_dir``，要求账本属于 Claude）。
    """

    manifest_path = Path(campaign_dir) / "campaign.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    manifest = _read_json(manifest_path, "Campaign 核心清单")
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping) or not isinstance(timing.get("ledger_dir"), str):
        return None
    from tools.official_client_capture import claude_upgrade

    ledger_dir = claude_upgrade._campaign_timing_ledger_dir(Path(campaign_dir), manifest)
    try:
        return codex_upgrade_timing_ledger.inspect_ledger(ledger_dir)
    except (OSError, codex_upgrade_timing_ledger.TimingLedgerError) as error:
        raise ConfigurationError(f"Campaign 时间账本无法重放：{error}") from error


def vc5_reopened_baseline(campaign_dir: Path, revision: int | None = None) -> int | None:
    """当前 revision 最近一次重开 VC-5 的评估基线号（账本 ``summary.vc5_reopened_baseline``，与 Codex 同一判据）。

    ``revision`` 给出且不是账本当前 revision、未重开或没有账本时为 None。
    """

    summary = campaign_ledger_summary(campaign_dir)
    if summary is None:
        return None
    if revision is not None and summary.get("current_revision") != revision:
        return None
    reopened = summary.get("vc5_reopened_baseline")
    if reopened is None:
        return None
    if isinstance(reopened, bool) or not isinstance(reopened, int) or reopened < 1:
        raise ConfigurationError("账本 vc5_reopened_baseline 非法。")
    return reopened


def vc_checkpoint_path(campaign_dir: Path, phase: str, *, revision: int | None = None) -> Path:
    """阶段 checkpoint 坐标（服务面 ``_vc_checkpoint_path``）：Campaign 级固定；候选级按 revision 与 VC-5 重开解析。"""

    if phase not in VC_PHASES:
        raise ConfigurationError(f"未知 VC 阶段：{phase}")
    base = Path(campaign_dir) / VC_DIRECTORY
    if phase in CANDIDATE_PHASES and revision is not None:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ConfigurationError("候选 revision 必须是正整数。")
        if revision >= 2:
            base = candidate_revision_dir(campaign_dir, revision)
        if phase == "VC-5":
            reopened = vc5_reopened_baseline(campaign_dir, revision)
            if reopened is not None:
                base = base / f"{VC5_REOPEN_DIRNAME_PREFIX}{reopened}"
    return base / f"{phase.lower()}-checkpoint.json"


def vc_checkpoint_reference(campaign_dir: Path, path: Path, checkpoint: Mapping[str, Any]) -> dict[str, str]:
    """后继阶段绑定前序 checkpoint 的引用：文件绑定加内部身份（与 Codex 同形）。"""

    return {
        **vc_control_binding(campaign_dir, path),
        "phase": str(checkpoint["phase"]),
        "checkpoint_sha256": str(checkpoint["checkpoint_sha256"]),
    }


# ---------------------------------------------------------------------------
# 二、重放
# ---------------------------------------------------------------------------


def replay_vc_checkpoint(
    campaign_dir: Path,
    plan: Mapping[str, Any],
    phase: str,
    *,
    revision: int | None = None,
) -> tuple[Path, dict[str, Any]]:
    """重放一个阶段 checkpoint（服务面 ``_replay_vc_checkpoint``）：合同、完成状态、阶段收据摘要与直接前序绑定，
    逐级回溯到 VC-0；候选级阶段按 ``revision`` 定位，VC-4 的前序固定为 Campaign 级 VC-3。"""

    path = vc_checkpoint_path(campaign_dir, phase, revision=revision)
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"{phase} checkpoint 缺失或路径不可信。")
    try:
        checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(_read_json(path, f"{phase} checkpoint"), plan)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if checkpoint["phase"] != phase or checkpoint["status"] != "complete":
        raise ConfigurationError(f"{phase} checkpoint 尚未成功封存。")
    stage = checkpoint.get("stage_receipt")
    if not isinstance(stage, Mapping):
        raise ConfigurationError(f"{phase} checkpoint 阶段收据绑定非法。")
    raw = str(stage.get("path", ""))
    stage_path = Path(raw) if Path(raw).is_absolute() else _campaign_file(campaign_dir, raw, f"{phase} 阶段收据")
    if stage_path.is_symlink() or not stage_path.is_file() or file_sha256(stage_path) != stage.get("sha256"):
        raise ConfigurationError(f"{phase} checkpoint 的阶段收据摘要漂移。")
    if phase != "VC-0":
        previous = VC_PHASES[VC_PHASES.index(phase) - 1]
        previous_path, previous_checkpoint = replay_vc_checkpoint(
            campaign_dir,
            plan,
            previous,
            revision=revision if previous in CANDIDATE_PHASES else None,
        )
        if checkpoint["predecessor_checkpoint"] != vc_checkpoint_reference(campaign_dir, previous_path, previous_checkpoint):
            raise ConfigurationError(f"{phase} checkpoint 的直接前序绑定漂移。")
    return path, checkpoint


# ---------------------------------------------------------------------------
# 三、写入
# ---------------------------------------------------------------------------


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

    if phase not in CAMPAIGN_PHASES or phase == "VC-0":
        raise ConfigurationError(
            "Campaign 级阶段完成器只接受 VC-1～VC-3（VC-0 只由收口写入，候选级阶段用 complete_candidate_phase）。"
        )
    return _complete_phase(
        campaign_dir,
        manifest,
        phase=phase,
        revision=None,
        stage_receipt_path=stage_receipt_path,
        completed_at_utc=completed_at_utc,
        execute_item_ids=execute_item_ids,
        reuse_item_ids=reuse_item_ids,
        live_request_count=live_request_count,
        scanned_bytes=scanned_bytes,
    )


def complete_candidate_phase(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    revision: int,
    stage_receipt_path: Path,
    completed_at_utc: str,
    execute_item_ids: Iterable[str],
    reuse_item_ids: Iterable[str],
    live_request_count: int,
    scanned_bytes: int,
) -> dict[str, Any]:
    """写候选级阶段 checkpoint（VC-4 由 B3-04 的 record-candidate-build 写，VC-5／VC-6 随 B4）：按候选 revision 定位
    （r1 在原路径、r≥2 在 revision 目录、VC-5 重开在 ``reopen-b<K>/``），VC-4 的直接前序是 Campaign 级 VC-3、VC-5／VC-6
    是同一 revision 的上一阶段；只写一次，重跑同输入复用、输入漂移拒绝（与 Codex ``_complete_vc_phase`` 的候选级分支
    同一判据；revision 由调用方显式给出，Claude 不认隐含 r1）。"""

    if phase not in CANDIDATE_PHASES:
        raise ConfigurationError("候选级阶段完成器只接受 VC-4～VC-6。")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ConfigurationError("候选级阶段完成器必须给出正整数候选 revision。")
    return _complete_phase(
        campaign_dir,
        manifest,
        phase=phase,
        revision=revision,
        stage_receipt_path=stage_receipt_path,
        completed_at_utc=completed_at_utc,
        execute_item_ids=execute_item_ids,
        reuse_item_ids=reuse_item_ids,
        live_request_count=live_request_count,
        scanned_bytes=scanned_bytes,
    )


def _complete_phase(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    revision: int | None,
    stage_receipt_path: Path,
    completed_at_utc: str,
    execute_item_ids: Iterable[str],
    reuse_item_ids: Iterable[str],
    live_request_count: int,
    scanned_bytes: int,
) -> dict[str, Any]:
    """两个完成器的共同部分：重放直接前序、绑定阶段收据、只写一次（已存在时除完成时刻与自摘要外逐字段比较）。"""

    output = vc_checkpoint_path(campaign_dir, phase, revision=revision)
    plan = vc_campaign_plan(campaign_dir, manifest)
    previous = VC_PHASES[VC_PHASES.index(phase) - 1]
    previous_path, previous_checkpoint = replay_vc_checkpoint(
        campaign_dir, plan, previous, revision=revision if previous in CANDIDATE_PHASES else None
    )
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


# ---------------------------------------------------------------------------
# 批次 → 父监督器队列清单
# ---------------------------------------------------------------------------


def vc_run_manifest_from_batch(batch: Mapping[str, Any], *, batch_model: str = "legacy") -> dict[str, Any]:
    """从已校验 VC 批次确定性编译父 run 队列清单（服务面 ``_vc_run_manifest_from_batch``）。

    合同取 ``official-client-campaign-run/v1``（``client`` 为 claude）；候选级绑定两字段与批次逐字一致（Campaign 级阶段为
    null），v3 批次另带评估基线三字段（非 VC-5 为 null）——字段口径与 Codex 的 staging 清单相同，只差合同名与 ``client``。
    参数默认值与 Codex 签名一致；Claude 只有 staging 批次模型，传入别的模型即拒绝。
    """

    if batch_model != STAGING_BATCH_MODEL:
        raise ConfigurationError(f"Claude Campaign 只使用 staging 批次模型，不能按 {batch_model!r} 编译队列清单。")
    extra: dict[str, Any] = {
        "candidate_revision": batch.get("candidate_revision"),
        "candidate_id": batch.get("candidate_id"),
    }
    if batch.get("schema_version") == codex_upgrade_vc_artifacts.VC_BATCH_SCHEMA:
        extra.update(
            {
                "evaluation_baseline": batch.get("evaluation_baseline"),
                "baseline_commit_sha256": batch.get("baseline_commit_sha256"),
                "evaluator_digests": (
                    dict(batch["evaluator_digests"]) if batch.get("evaluator_digests") is not None else None
                ),
            }
        )
    try:
        return codex_upgrade_supervisor.build_batched_campaign_run_manifest(
            campaign_id=str(batch["campaign_id"]),
            campaign_plan_sha256=str(batch["campaign_plan_sha256"]),
            batch_id=str(batch["batch_id"]),
            batch_sequence=int(batch["sequence"]),
            batch_sha256=str(batch["batch_sha256"]),
            phase=str(batch["phase"]),
            predecessor_checkpoint=batch["predecessor_checkpoint"],
            original_deadline_at_utc=str(batch["original_deadline_at_utc"]),
            actions=batch["actions"],
            execute_items=batch["execute_item_ids"],
            reuse_items=batch["reuse_item_ids"],
            client="claude",
            **extra,
        )
    except codex_upgrade_supervisor.SupervisorError as error:
        raise ConfigurationError(f"VC 批次无法编译成 Claude 队列清单：{error}") from error


# ---------------------------------------------------------------------------
# 四、VC-0 交接链
# ---------------------------------------------------------------------------


def write_first_formal_batch(
    campaign_dir: Path,
    *,
    plan: Mapping[str, Any],
    execute_item_ids: Sequence[str],
    reuse_item_ids: Sequence[str],
    actions: Sequence[Mapping[str, Any]],
    compiled_at_utc: str,
    must_start_by_utc: str,
) -> dict[str, dict[str, str]]:
    """写 VC-0 交接链的首批两件（序号 1 的 VC-1 批次与队列清单），只写一次；返回 ``vc_control`` 里的两条绑定。

    前序是规范坐标上已封存的 VC-0 checkpoint；队列清单由 ``vc_run_manifest_from_batch`` 确定性编译。没有动作时是零请求
    no-op 首批（只读复用前序证据的 Campaign）：原子批次入口派发第 2 批前由共享流程代跑成父 run 历史。
    """

    vc0_path, vc0 = replay_vc_checkpoint(campaign_dir, plan, "VC-0")
    try:
        batch = codex_upgrade_vc_artifacts.build_vc_batch(
            campaign_plan=plan,
            phase="VC-1",
            sequence=1,
            predecessor_checkpoint=vc_checkpoint_reference(campaign_dir, vc0_path, vc0),
            execute_item_ids=list(execute_item_ids),
            reuse_item_ids=list(reuse_item_ids),
            actions=[dict(action) for action in actions],
            compiled_at_utc=compiled_at_utc,
            must_start_by_utc=must_start_by_utc,
        )
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"无法编译首个 VC-1 批次：{error}") from error
    run_manifest = vc_run_manifest_from_batch(batch, batch_model=STAGING_BATCH_MODEL)
    root = Path(campaign_dir) / VC_DIRECTORY
    batch_path = root / "batches" / FIRST_BATCH_NAME
    manifest_path = root / "run-manifests" / FIRST_BATCH_NAME
    for directory in (batch_path.parent, manifest_path.parent):
        ensure_private_directory(directory, Path(campaign_dir))
    write_json_once(batch_path, batch)
    write_json_once(manifest_path, run_manifest)
    return {
        "first_formal_batch": vc_control_binding(campaign_dir, batch_path),
        "first_campaign_run_manifest": vc_control_binding(campaign_dir, manifest_path),
    }


def validate_vc_control(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """核对总计划 ``vc_control`` 的语义（见模块说明第四节）；没有总计划绑定时返回 None。"""

    control = manifest.get("vc_control")
    if control is None:
        return None
    if not isinstance(control, Mapping):
        raise ConfigurationError("Campaign vc_control 必须是对象。")
    unknown = sorted(set(control) - set(VC_CONTROL_FIELDS))
    if unknown:
        raise ConfigurationError(f"Campaign vc_control 含未登记字段：{unknown}")
    if control.get("campaign_plan") is None:
        if any(control.get(field) is not None for field in VC_CONTROL_FIELDS):
            raise ConfigurationError("Campaign vc_control 没有总计划绑定，却绑定了其它 VC 控制制品。")
        return None
    plan = vc_campaign_plan(campaign_dir, manifest)
    for field in ("campaign_mode", "campaign_purpose", "baseline_version", "target_version"):
        if plan[field] != manifest.get(field):
            raise ConfigurationError(f"VC 总计划的 {field} 与 Campaign 总计划不一致。")
    try:
        batch_model = codex_upgrade_vc_artifacts.campaign_plan_batch_model(plan)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if batch_model != STAGING_BATCH_MODEL:
        raise ConfigurationError("Claude Campaign 的 VC 总计划必须声明 staging 批次模型。")
    if control.get("vc0_checkpoint") is not None:
        path = _bound_file(campaign_dir, control["vc0_checkpoint"], "VC-0 checkpoint")
        if path.resolve() != vc_checkpoint_path(campaign_dir, "VC-0").resolve():
            raise ConfigurationError("vc_control.vc0_checkpoint 没有指向规范的 VC-0 checkpoint 坐标。")
        try:
            checkpoint = codex_upgrade_vc_artifacts.validate_vc_checkpoint(_read_json(path, "VC-0 checkpoint"), plan)
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
        if (
            checkpoint["phase"] != "VC-0"
            or checkpoint["status"] != "complete"
            or checkpoint["metrics"] != {"live_request_count": 0, "scanned_bytes": 0}
        ):
            raise ConfigurationError("VC-0 checkpoint 没有以零请求、零扫描完成。")
    first_batch = control.get("first_formal_batch")
    first_manifest = control.get("first_campaign_run_manifest")
    if (first_batch is None) != (first_manifest is None):
        raise ConfigurationError("首个 VC-1 批次与队列清单必须同时绑定。")
    if first_batch is not None:
        batch_path = _bound_file(campaign_dir, first_batch, "首个 VC-1 批次")
        try:
            batch = codex_upgrade_vc_artifacts.validate_vc_batch(_read_json(batch_path, "首个 VC-1 批次"), plan)
        except codex_upgrade_vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
        vc0_path, vc0 = replay_vc_checkpoint(campaign_dir, plan, "VC-0")
        if (
            batch["phase"] != "VC-1"
            or batch["sequence"] != 1
            or batch["predecessor_checkpoint"] != vc_checkpoint_reference(campaign_dir, vc0_path, vc0)
        ):
            raise ConfigurationError("首个 VC-1 批次不是从 VC-0 checkpoint 编译的序号 1。")
        run_path = _bound_file(campaign_dir, first_manifest, "首个 VC-1 队列清单")
        expected = vc_run_manifest_from_batch(batch, batch_model=STAGING_BATCH_MODEL)
        if _read_json(run_path, "首个 VC-1 队列清单") != expected:
            raise ConfigurationError("首个 VC-1 队列清单不是由首批确定性编译的。")
    return {"plan": plan}
