"""B0：两个 reconciler、先入账后判定、恢复预览与批准（方案第十一版 B0）。

两个只读取证据、不伪造事实的对账命令：

* ``reconcile-supervisor-run --run-dir``：父监督器在派发前失败、被 SIGKILL 或中断，
  尚无 attempt。输入 run manifest、state、events、minute ledger、部署收据、Campaign
  账本与项目总账，输出独立不可变 ``supervisor-run-reconciliation/v1``。
* ``reconcile-attempt --campaign-dir --attempt-id``：reservation 之后的任何中断。输入
  reservation、checkpoints、逐 Job 收据、provenance 审计、部署收据、before／after 环境
  探针、Campaign 账本与项目总账，输出独立不可变 ``attempt-reconciliation/v1``，不回写
  ``attempt.json``，不新增 attempt 状态枚举。

统一顺序（每步幂等，中断后从第一步重放）：

1. Campaign 侧写 reconciliation 收据；attempt 分支先在 Campaign 账本追加无收据的
   ``attempt_failed``（带 A0a-3 根因）。账本从未登记过 attempt 事件时先补登
   ``attempt_started``；账本已 ``stop_required`` 且没有 active attempt 时无法登记，
   如实记录为跳过，禁止执行 Job。
2. 写一个 outbox batch，目标事件 ``reconciliation_committed``：请求部分是从 provenance
   核算的未入账身份键与估计 delta（无法精确且无估计依据时 ``unresolved``，不写 0），
   根因部分是 A0a-3 编码；entry 绑定 reconciliation 收据 SHA，attempt 分支还绑定
   ``attempt_failed`` 事件 SHA；写 ``COMMIT``。
3. ``reconcile-project-ledger`` 把整个 batch 推成一个项目事件。
4. 锁内重放总账，得到根因计数、累计请求、剩余预算、blocked。
5. 判定：总账 blocked、完整性异常和重试／请求上限仍永久停线；三层时间预算到期
   只暂停，等批准延期；身份与环境满足既有规则且预算有效时可恢复。
6. 可恢复：supervisor-run 追加 ``receipt_passed`` 绑定收据；attempt 生成零请求
   ``recovery-preview/v1``，操作员 ``--approve-recovery-sha256`` 后才能
   ``resume --rerun-failed --recovery-preview``。永久停线：``stage_abandoned`` 与
   ``stop_the_line``，再写 ``campaign_terminal`` batch 并推入总账。预算暂停只追加
   ``deadline_paused``／``campaign_paused``，保留阶段和恢复状态。

R4 的 ``stage_review_required`` 先完成同样的账务判定，再核验动作幂等合同；无法
证明的半成品继续只读等待，不能因账务可恢复就重派。可证明时单独写 stage-replay
收据，与原对账收据一并绑定时间账本，再按 COMMIT 状态裁定序号。

两个 reconciler 自身的模型请求数为零。
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture import codex_upgrade_wire_transition as wire_transition
from tools.official_client_capture import incremental_recovery

# 与监督器后继协议共用同一常量：改造 2 的候选 revision 后继会完整重放这份收据。
SUPERVISOR_RUN_SCHEMA = supervisor.SUPERVISOR_RUN_RECONCILIATION_SCHEMA
ATTEMPT_SCHEMA = supervisor.ATTEMPT_RECONCILIATION_SCHEMA
RECOVERY_PREVIEW_SCHEMA = "recovery-preview/v1"
RECOVERY_APPROVAL_SCHEMA = "recovery-approval/v1"
RECONCILIATION_DIR = "reconciliation"
ATTEMPT_RECEIPT_NAME = "attempt-reconciliation.json"
SUPERVISOR_RUN_RECEIPT_NAME = "supervisor-run-reconciliation.json"
# 修好接着跑第 37 项：对账收据只写一次，中断／账务暂停（第 12 项）后重新对账以首次落盘的收据为准。root_cause 单值与
# failure_observations／root_causes 数组同源——账务暂停时是 attempt.accounting-unresolved，补账后重算成真实根因——
# 三者都必须是易变字段，否则数组合同下续接被"既有对账收据与当前事实不一致，拒绝覆盖"卡死（2026-09-28 194249z A2）。
RECEIPT_ROOT_CAUSE_VOLATILE_FIELDS = ("root_cause", "failure_observations", "root_causes")
PREVIEW_RE = re.compile(r"^recovery-preview-(\d{2})\.json$")
APPROVAL_RE = re.compile(r"^recovery-approval-(\d{2})\.json$")
COMPONENT = "reconciler"
DECISION_RECOVERABLE = "recoverable"
DECISION_STOP = "permanent_stop"
DECISION_PAUSED = "paused"
# 改造 4：父 run 取得执行权之前的失败分类（无动作诊断，按 state／stop reason 判定）。
PARENT_PREPARE_ABANDONED_CLASS = "parent-prepare-abandoned"
PARENT_START_FAILED_CLASS = supervisor.PARENT_START_FAILED_REASON
# 改造 5 M2（R2 的 attempt-recovery 变体）：单动作恢复段 run 已成功、父 run 终态化前 owner 丢失。
PARENT_FINALIZE_LOST_CLASS = supervisor.PARENT_FINALIZE_LOST_REASON
COMMIT_INTEGRITY_MISMATCH_CLASS = supervisor.COMMIT_INTEGRITY_MISMATCH_CLASS
# 2026-09-22：动作诊断 declared 为 evidence-integrity（生产者：EvidenceManifest 不可变 stat 边界
# 漂移，见 codex_upgrade.EvidenceIntegrityError）的父 run 与 COMMIT 完整性异常同属"不可变控制或
# 证据制品完整性异常"：无论总账与账本状态如何都固定终态 integrity_mismatch，不生成
# post-run-tooling 收据、不给出同批次重派建议（v14r4 批次 15 事故：ctime 不可回写、manifest
# write-once，当前 attempt 不可恢复）。
EVIDENCE_INTEGRITY_CLASS = "evidence-integrity"
# 第三批 R3：已封存证据只有 mtime／ctime／inode 漂移（内容未变）——可恢复，不进 INTEGRITY_MISMATCH；
# 对账后 rebind-boundary 再逐字重派。
EVIDENCE_METADATA_DRIFT_CLASS = "evidence-metadata-drift"
INTEGRITY_MISMATCH_FAILURE_CLASSES = frozenset(
    {COMMIT_INTEGRITY_MISMATCH_CLASS, EVIDENCE_INTEGRITY_CLASS}
)
STAGING_FAILURE_CLASSES = frozenset(
    {
        PARENT_PREPARE_ABANDONED_CLASS,
        PARENT_START_FAILED_CLASS,
        PARENT_FINALIZE_LOST_CLASS,
        COMMIT_INTEGRITY_MISMATCH_CLASS,
    }
)
# 可恢复分类对应的账本 next_action：序号未占 → 同序号重派；序号已占 → 同批次 N+1 重派。
NEXT_ACTION_SAME_SEQUENCE = "redispatch-same-sequence"
NEXT_ACTION_SAME_BATCH = "redispatch-same-batch"
JOB_STATES = ("complete", "failed", "indeterminate", "pending")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024


class ReconcilerError(ValueError):
    """对账输入不可信、事实互相矛盾或账本／总账拒绝写入。"""


def _job_egress_trusted(result: Mapping[str, Any]) -> bool:
    """Job 出口时段绑定核验；父 run 记录或暂停事实缺失、被改动时明确失败关闭。

    不把无法核验的绑定静默改判为可复用或补跑：父 run、暂停记录与最后可信状态必须随 attempt 完整保留。
    """

    try:
        return supervisor.job_egress_trusted(result)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(
            f"Job 出口时段绑定无法核验（父 run 记录与暂停事实须随 attempt 完整保留）：{error}"
        ) from error


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ReconcilerError(f"{label} 必须是 RFC3339 字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReconcilerError(f"{label} 不是合法时间：{value}") from error
    if parsed.tzinfo is None:
        raise ReconcilerError(f"{label} 缺少时区")
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str:
    return codex_upgrade._fingerprint(value)


def _file_sha256(path: Path) -> str:
    return codex_upgrade.file_sha256(path)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReconcilerError(f"{label}不存在或不可信：{path}")
    if path.stat().st_size > MAX_RECEIPT_BYTES:
        raise ReconcilerError(f"{label}超过大小上限：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReconcilerError(f"{label}不是有效 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise ReconcilerError(f"{label}必须是 JSON 对象：{path}")
    return payload


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    """只写一次的不可变收据；父目录按 0700 创建。"""

    parent = path.parent
    if parent.is_symlink():
        raise ReconcilerError(f"收据目录不可信：{parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(parent.stat().st_mode) != 0o700:
        parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        raise ReconcilerError(f"不可变收据已存在：{path}")
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_or_verify(path: Path, payload: Mapping[str, Any], *, volatile: Iterable[str]) -> dict[str, Any]:
    """幂等落盘：已存在时逐字段核对（忽略易变字段），不同即失败关闭。"""

    if path.exists():
        existing = _read_json(path, "既有对账收据")
        skip = set(volatile)
        left = {k: v for k, v in existing.items() if k not in skip}
        right = {k: v for k, v in payload.items() if k not in skip}
        if left != right:
            raise ReconcilerError(f"既有对账收据与当前事实不一致，拒绝覆盖：{path}")
        return existing
    _write_once(path, payload)
    return dict(payload)


def _binding(campaign_dir: Path, path: Path, role: str) -> dict[str, str]:
    return {
        "role": role,
        "path": path.resolve(strict=True).relative_to(campaign_dir.resolve(strict=True)).as_posix(),
        "sha256": _file_sha256(path),
    }


def _reconciliation_dir(campaign_dir: Path, subject: str) -> Path:
    return campaign_dir / "control" / RECONCILIATION_DIR / subject


def _latest_indexed(directory: Path, pattern: re.Pattern[str]) -> tuple[int, Path | None]:
    latest = (0, None)
    if not directory.is_dir():
        return latest
    for child in sorted(directory.iterdir()):
        match = pattern.fullmatch(child.name)
        if match and int(match.group(1)) > latest[0]:
            latest = (int(match.group(1)), child)
    return latest


# ---------------------------------------------------------------------------
# 共同事实：工具身份、部署收据、账本、总账
# ---------------------------------------------------------------------------


def _current_identity() -> dict[str, Any]:
    return codex_upgrade._tool_identity(include_git=False)


def _identity_facts(campaign_dir: Path, manifest: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """身份不变 = 当前有效 wire 身份与有效 policy_sha256 相等（v1 Campaign 比整树）。

    第三批 R1：策略经策略演进承接后，有效策略是最新演进的 to 身份策略；这里对照有效策略而不是 plan 冻结值。
    """

    frozen = manifest.get("tool_identity")
    if not isinstance(frozen, Mapping):
        raise ReconcilerError("Campaign 缺少冻结工具身份")
    if codex_upgrade._is_policy_v2_identity(frozen):
        try:
            effective = wire_transition.effective_wire_identity(campaign_dir, manifest)
        except wire_transition.WireTransitionError as error:
            raise ReconcilerError(f"当前有效 wire 身份无法重放：{error}") from error
        unchanged = (
            str(current.get("wire_producer_sha256")) == str(effective["wire_producer_sha256"])
            and str(current.get("policy_sha256")) == str(effective["policy_sha256"])
        )
        return {
            "policy_version": "v2",
            "unchanged": unchanged,
            "effective_wire_producer_sha256": effective["wire_producer_sha256"],
            "effective_source": effective.get("source"),
            "pending_intent": effective.get("pending_intent"),
            "current_wire_producer_sha256": current.get("wire_producer_sha256"),
            "frozen_policy_sha256": frozen.get("policy_sha256"),
            "effective_policy_sha256": effective["policy_sha256"],
            "current_policy_sha256": current.get("policy_sha256"),
        }
    unchanged = str(current.get("files_sha256")) == str(frozen.get("files_sha256"))
    return {
        "policy_version": "v1",
        "unchanged": unchanged,
        "frozen_files_sha256": frozen.get("files_sha256"),
        "current_files_sha256": current.get("files_sha256"),
    }


def _last_candidate_review_event(ledger_dir: Path) -> dict[str, Any] | None:
    """账本里最后一条 candidate_review_required 事件（候选审核的阶段与根因不进入摘要，只能从事件取）。"""

    review: dict[str, Any] | None = None
    for event, _raw in timing_ledger._load_events(ledger_dir):
        if isinstance(event, Mapping) and event.get("event_type") == "candidate_review_required":
            review = dict(event)
    return review


def _candidate_capture_review(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    ledger_dir: Path,
    ledger: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
    strict: bool,
) -> dict[str, Any] | None:
    """候选审核下的 VC-5 采集续跑上下文；不适用时返回 None。

    适用条件：账本处于 candidate_review_required、审核事件属于 VC-5 与本候选、审核事件唯一绑定的
    失败父 run 窗口内发布了本 attempt 的预约。返回审核阶段、根因与失败父 run，供对账登记 attempt
    事件和授权写 recovery_authorized。

    ``strict=False``（对账）：绑定不成立时返回 None，按原规则只入账、不登记 attempt 事件——候选审核
    下对账其它旧 attempt（作废前的账务核对需要）不能被续跑判定挡住。``strict=True``（授权）：绑定
    不成立即拒绝，续跑授权只属于引起审核的那次失败采集。
    """

    if ledger.get("status") != "candidate_review_required" or phase != "candidate" or not candidate_id:
        return None
    review = _last_candidate_review_event(ledger_dir)
    if review is None or review.get("phase") != "VC-5" or review.get("candidate_id") != candidate_id:
        return None
    try:
        failed_run = codex_upgrade._candidate_failed_run_for_review(
            campaign_dir, manifest, str(candidate_id), review
        )
    except codex_upgrade.ConfigurationError as error:
        if not strict:
            return None
        raise ReconcilerError(f"候选审核无法绑定失败父 run：{error}") from error
    window = supervisor.candidate_reservations_in_run_window(
        campaign_dir, candidate_id=str(candidate_id), started_at_epoch=float(failed_run["started_at_epoch"])
    )
    if attempt_id not in {name for name, _root in window}:
        if not strict:
            return None
        raise ReconcilerError(
            f"attempt {attempt_id} 不是引起候选审核的失败父 run（{failed_run['run_id']}）窗口内发布的预约"
        )
    return {
        "phase": "VC-5",
        "root_cause_id": str(review["root_cause_id"]),
        "review_event_id": str(review["event_id"]),
        "failed_run_id": str(failed_run["run_id"]),
    }


def _require_registered_tool_identity(identity: Mapping[str, Any], ledger: Mapping[str, Any]) -> None:
    """活 Campaign 的零写入前置：v2 身份与有效身份（最新工具演进）不一致时拒绝对账。

    修好工具并部署后若还没登记工具演进，按当前身份对账会把失败判为身份变化并写永久终态；
    这里在任何收据、账本或总账写入之前拒绝，提示先登记演进。已 stopped／complete 的旧
    Campaign 不受影响：身份变化只作为不能恢复的原因之一记入对账收据。
    """

    if identity.get("policy_version") != "v2" or identity.get("unchanged"):
        return
    if ledger.get("status") in {"stopped", "complete"}:
        return
    raise ReconcilerError(
        "当前受管工具的 wire／策略身份与 Campaign 有效工具身份（最新工具演进）不一致：修好工具并受监督"
        "部署后，先执行 tool-evolution 登记本次修复，再对账；本次未写入任何文件"
    )


def _deployment_receipt(control_root: Path, current: Mapping[str, Any], *, required: bool) -> dict[str, Any] | None:
    """最近一份通过且五摘要等于当前工具身份的部署收据；生产总账下必须存在。"""

    if not control_root.is_dir() or control_root.is_symlink():
        if required:
            raise ReconcilerError(f"控制根不存在：{control_root}")
        return None
    candidates: list[tuple[datetime, Path, dict[str, Any]]] = []
    for path in sorted(control_root.glob(wire_transition.DEPLOY_RECEIPT_GLOB)):
        if path.is_symlink() or not path.is_file():
            continue
        payload = _read_json(path, f"部署收据 {path.name}")
        if payload.get("status") != "passed":
            continue
        try:
            created = _timestamp(payload.get("created_at_utc"), "部署收据 created_at_utc")
        except ReconcilerError:
            continue
        candidates.append((created, path, payload))
    candidates.sort(key=lambda item: item[0])
    for created, path, payload in reversed(candidates):
        matches = (
            payload.get("tool_files_sha256") == current.get("files_sha256")
            and payload.get("policy_sha256") == current.get("policy_sha256")
            and payload.get("wire_producer_sha256") == current.get("wire_producer_sha256")
            and payload.get("evidence_semantics_sha256") == current.get("evidence_semantics_sha256")
            and payload.get("control_sha256") == current.get("control_sha256")
        )
        if matches:
            return {
                "path": str(path),
                "sha256": _file_sha256(path),
                "created_at_utc": payload.get("created_at_utc"),
                "tool_files_sha256": payload.get("tool_files_sha256"),
                "policy_sha256": payload.get("policy_sha256"),
                "wire_producer_sha256": payload.get("wire_producer_sha256"),
            }
    if required:
        raise ReconcilerError("当前工具身份没有对应的通过部署收据；对账命令必须在已受管部署的工具上运行")
    return None


def _campaign_ledger_dir(manifest: Mapping[str, Any]) -> Path:
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping) or not isinstance(timing.get("ledger_dir"), str):
        raise ReconcilerError("Campaign 缺少 upgrade_timing 账本绑定")
    ledger_dir = Path(timing["ledger_dir"])
    if not ledger_dir.is_absolute() or ledger_dir.is_symlink() or not ledger_dir.is_dir():
        raise ReconcilerError(f"Campaign 账本目录不存在或不可信：{ledger_dir}")
    return ledger_dir


def _ledger_event_sha256(ledger_dir: Path, event_id: str) -> str | None:
    for event, raw in timing_ledger._load_events(ledger_dir):
        if event.get("event_id") == event_id:
            return timing_ledger._sha256_bytes(raw)
    return None


def build_stage_replay_proof(
    *,
    campaign_id: Any,
    run_id: Any,
    review_event_id: Any,
    review_root_cause_id: Any,
    reconciliation_receipt_sha256: Any,
    commit_sha256: Any,
    next_action: Any,
    replay: Mapping[str, Any],
) -> dict[str, Any]:
    """按固定字段顺序构造阶段幂等重派证明；与历史写出的证明逐字节一致，重复对账可原样核对。"""

    return {
        "schema_version": vc_artifacts.STAGE_REPLAY_SCHEMA, "decision": "recoverable",
        "campaign_id": campaign_id, "run_id": run_id,
        "review_event_id": review_event_id, "review_root_cause_id": review_root_cause_id,
        "reconciliation_receipt_sha256": reconciliation_receipt_sha256,
        "commit_sha256": commit_sha256,
        "next_action": next_action, **replay,
    }


def _ledger_receipt_bindings(
    ledger_dir: Path,
    subject_id: str,
    receipt_path: Path,
    provenance_path: Path,
    *,
    stage_replay_path: Path | None = None,
) -> list[dict[str, str]]:
    """账本事件只能绑定账本目录内的收据：把对账收据与 provenance 副本一次性发布进账本。"""

    target_dir = ledger_dir / "receipts" / RECONCILIATION_DIR / subject_id
    if target_dir.is_symlink():
        raise ReconcilerError("账本对账收据目录不可信")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (ledger_dir / "receipts", ledger_dir / "receipts" / RECONCILIATION_DIR, target_dir):
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            directory.chmod(0o700)
    bindings: list[dict[str, str]] = []
    sources = [("reconciliation", receipt_path), ("provenance", provenance_path)]
    if stage_replay_path is not None:
        sources.append(("stage_replay", stage_replay_path))
    for role, source in sources:
        payload = _read_json(source, f"{role} 收据")
        target = target_dir / f"{role}.json"
        try:
            timing_ledger._publish_once(target, payload, f"{role} 账本副本")
        except timing_ledger.TimingLedgerError as error:
            raise ReconcilerError(str(error)) from error
        bindings.append(
            {"role": role, "path": target.relative_to(ledger_dir).as_posix(), "sha256": _file_sha256(target)}
        )
    return sorted(bindings, key=lambda item: item["role"])


def _ledger_recovery_authorization_bindings(
    ledger_dir: Path,
    attempt_id: str,
    preview_path: Path,
    approval_path: Path,
    *,
    recovery_revision: str | None = None,
) -> list[dict[str, str]]:
    """把恢复预览与批准复制进时间账本，供 recovery_authorized 重放（恢复段按 attempt-<id>-ar<k> 存放）。"""

    subject = _recovery_segment_subject(attempt_id, recovery_revision).replace(":", "-")
    target_dir = ledger_dir / "receipts" / RECONCILIATION_DIR / f"attempt-{subject}"
    if target_dir.is_symlink():
        raise ReconcilerError("账本恢复批准目录不可信")
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (
        ledger_dir / "receipts",
        ledger_dir / "receipts" / RECONCILIATION_DIR,
        target_dir,
    ):
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            directory.chmod(0o700)
    bindings: list[dict[str, str]] = []
    for role, source in (
        ("recovery_approval", approval_path),
        ("recovery_preview", preview_path),
    ):
        payload = _read_json(source, f"{role} 收据")
        target = target_dir / source.name
        try:
            timing_ledger._publish_once(target, payload, f"{role} 账本副本")
        except timing_ledger.TimingLedgerError as error:
            raise ReconcilerError(str(error)) from error
        bindings.append(
            {
                "role": role,
                "path": target.relative_to(ledger_dir).as_posix(),
                "sha256": _file_sha256(target),
            }
        )
    return sorted(bindings, key=lambda item: item["role"])


def _ledger_facts(ledger_dir: Path, *, now: str) -> dict[str, Any]:
    summary = timing_ledger.inspect_ledger(ledger_dir, now=now)
    active = timing_ledger._active_attempts(timing_ledger._load_events(ledger_dir))
    return {
        "ledger_dir": str(ledger_dir),
        "status": summary["status"],
        "active_phase": summary.get("active_phase"),
        "recovery_phase": summary.get("recovery_phase"),
        "recovery_root_cause_id": summary.get("recovery_root_cause_id"),
        "review_phase": summary.get("review_phase"),
        "review_root_cause_id": summary.get("review_root_cause_id"),
        "last_event_id": summary.get("last_event_id"),
        "head_sequence": summary.get("head_sequence"),
        "head_sha256": summary.get("head_sha256"),
        "total_deadline_at_utc": summary.get("total_deadline_at_utc"),
        "stage_deadline_at_utc": summary.get("stage_deadline_at_utc"),
        "total_live_request_count": summary.get("total_live_request_count"),
        "same_root_cause_failures": summary.get("same_root_cause_failures"),
        "active_attempts": [{"attempt_id": a, "phase": p} for a, p in active],
    }


def _project_root(campaign_dir: Path) -> Path:
    root = project_ledger.find_project_ledger(campaign_dir)
    if root is None:
        raise ReconcilerError("找不到项目总账；0.154 起 formal Campaign 的对账必须在总账内进行")
    return root


def _project_facts(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        with project_ledger.project_lock(root):
            plan, _raw = project_ledger._load_plan(root)
            head = project_ledger.replay_head(root)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账重放失败：{error}") from error
    return plan, head


def _pause_time(observed: str) -> datetime:
    """登记暂停事实的时刻：不早于本次对账已追加的账本事件（对账开始时刻之后可能已写 attempt 事件）。"""

    return max(_timestamp(observed, "now"), datetime.now(timezone.utc))


def _paused_next_command(decision: Mapping[str, Any], resume_from: str) -> str:
    """暂停判定的下一步：到期的时间层先 deadline-extend，请求预算耗尽先 request-budget-extend。"""

    kinds = list(decision.get("pause_kinds") or ["deadline"])
    steps = []
    if "deadline" in kinds:
        steps.append("deadline-extend preview/apply")
    if "request_budget" in kinds:
        steps.append("request-budget-extend preview/apply")
    if "accounting" in kinds:
        steps.append("accounting-resolve preview/apply（为未决 operation 补账）")
    if "environment" in kinds:
        steps.append("修复环境并取得晚于污染的干净环境复核后 environment-isolate preview/apply（隔离污染 attempt）")
    if "root_cause_repair" in kinds:
        # 第三批 B3-9：同根因重试上限只暂停，登记修复证据（清零达上限的根因）后重新对账即可继续。
        steps.append(
            "登记根因修复证据：本 Campaign 账本已 stop_required 用 campaign-resume preview/apply（绑定修复提交、离线回归与部署收据）；"
            "只是项目总账根因达上限用 codex_upgrade_project_ledger record-root-cause-repair（code：修复提交／回归收据／部署收据）；"
            "随后重新执行本对账"
        )
    return "；".join(steps) + f"；批准后{resume_from}"


def _campaign_resume_epoch(campaign_dir: Path, campaign_id: str) -> int:
    root = project_ledger.find_project_ledger(campaign_dir)
    if root is None:
        return 0
    try:
        return project_ledger.campaign_resume_epoch_snapshot(root, campaign_id)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账事件读取失败：{error}") from error


def _require_resume_closed(campaign_dir: Path, manifest: Mapping[str, Any], ledger_dir: Path) -> None:
    """campaign-resume 半完成（计时账本已写恢复、总账未写 campaign_resumed）时零写入拒绝对账。

    先写计时账本、后写总账：半完成期间自动对账若按旧终态再停线，会把这次恢复作废；重跑同一批准的
    campaign-resume apply 即补齐。
    """

    try:
        timing_epoch = timing_ledger.resume_epoch(ledger_dir)
    except timing_ledger.TimingLedgerError as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    project_epoch = _campaign_resume_epoch(campaign_dir, str(manifest["campaign_id"]))
    if timing_epoch != project_epoch:
        raise ReconcilerError(
            f"campaign-resume 未完成：计时账本已恢复 {timing_epoch} 次、项目总账 {project_epoch} 次；先以同一批准重跑 "
            "campaign-resume apply 补齐，再对账；本次未写入任何文件"
        )


def _campaign_event_scope(root: Path, campaign_id: str) -> dict[str, Any]:
    try:
        return project_ledger.campaign_event_scope(root, campaign_id)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账事件读取失败：{error}") from error


def _recovery_preview_ledger_problems(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    frozen_scope: Mapping[str, Any],
    receipt_path: Path,
    *,
    now: str,
) -> list[str]:
    """消费已批准恢复预览时的项目总账现场复核，口径与对账判定 ``_decide`` 相同。

    预览只冻结本 Campaign 的总账事件；其它 Campaign 造成的 blocked、同版本根因累计、请求预算消耗，
    以及截止与暂停，都在这里按消费时刻的事实判定。截止已到或已暂停时先批准延期——延期不改变
    本 Campaign 的事件摘要，批准延期后同一份预览仍可消费。
    """

    campaign_id = str(manifest["campaign_id"])
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    problems: list[str] = []
    if _campaign_event_scope(project_root, campaign_id) != dict(frozen_scope):
        problems.append("预览生成后本 Campaign 在项目总账出现新事件（对账、终态、账务解决或更正），必须重新对账")
    blocking = project_ledger.campaign_blocked(head, campaign_id)
    if blocking:
        # 第三批 B3-10：只有本 Campaign 或无归属的未决账务才挡消费；其它 Campaign 的账务问题不挡本预览。
        problems.append(f"项目总账 blocked：未决账务 {blocking}")
    if campaign_id in head.get("terminal_campaigns", {}):
        problems.append("本 Campaign 已在项目总账终态")
    if campaign_id in head.get("paused_campaigns", {}):
        problems.append("本 Campaign 预算已暂停：先批准延期（延期不会使本预览作废）")
    # 第 38 项：有效根因（重归属收据优先），真实根因达上限时不会因读到账务占位根因而放行。
    cause_ids = receipt_root_cause_ids(receipt_path)
    target_version = project_ledger.campaign_target_version(head, campaign_id)
    at_limit = sorted(set(cause_ids) & set(project_ledger.root_causes_at_limit_for(head, target_version)))
    if at_limit:
        problems.append(f"根因 {at_limit} 累计失败已达上限")
    remaining = head.get("remaining_live_requests")
    if remaining is not None and int(remaining) <= 0:
        problems.append("项目请求预算已耗尽")
    current = _timestamp(now, "now")
    if current >= _timestamp(
        head.get("effective_absolute_deadline_utc", plan["absolute_deadline_utc"]), "absolute_deadline_utc"
    ):
        problems.append("项目有效截止已到：先批准延期（延期不会使本预览作废）")
    campaign_deadline = codex_upgrade._campaign_plan_deadline(campaign_dir)
    if campaign_deadline is not None and current >= _timestamp(campaign_deadline, "Campaign deadline"):
        problems.append("Campaign 总预算有效截止已到：先批准延期（延期不会使本预览作废）")
    return problems


def _control_root(campaign_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve(strict=False)
    return closeout._formal_host_data_root(campaign_dir) / "control"


# ---------------------------------------------------------------------------
# 请求部分：从 provenance 核算未入账身份键与估计 delta
# ---------------------------------------------------------------------------


def _request_part(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    receipt_dir: Path,
    *,
    plan: Mapping[str, Any],
    head: Mapping[str, Any],
    project_root: Path,
    now: str,
    phase: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """返回 (batch 请求部分, provenance 副本绑定, 副本绝对路径)。

    身份键按总账索引与初始清单去重；估计按来源（producer_run_id）去重，避免同一
    direct 分支被多次对账重复计入上界。无法精确且无估计依据 → ``unresolved``。
    """

    campaign_id = str(manifest["campaign_id"])
    try:
        receipt = provenance.collect_campaign_provenance(
            campaign_dir,
            formal_campaign_id=campaign_id,
            estimation_policy=str(plan["estimation_policy"]),
            observed_at_utc=now,
        )
    except (provenance.ProvenanceError, closeout.VC0CloseoutError, OSError, ValueError) as error:
        raise ReconcilerError(f"provenance 核算失败：{error}") from error
    if phase is not None:
        if phase not in {"official", "candidate"}:
            raise ReconcilerError(f"provenance 阶段过滤非法：{phase!r}")
        receipt = dict(receipt)
        filtered_jobs = [
            dict(item)
            for item in receipt.get("jobs", [])
            if isinstance(item, Mapping) and item.get("phase") == phase
        ]
        job_ids = {str(item["job_id"]) for item in filtered_jobs}
        filtered_requests = [
            dict(item)
            for item in receipt.get("requests", [])
            if isinstance(item, Mapping) and item.get("job_id") in job_ids
        ]
        receipt["jobs"] = filtered_jobs
        receipt["requests"] = filtered_requests
        for field in (
            "unresolved_job_ids",
            "pending_job_ids",
            "pre_request_zero_job_ids",
        ):
            receipt[field] = [
                str(job_id)
                for job_id in receipt.get(field, [])
                if str(job_id) in job_ids
            ]
        receipt["precise_total"] = len(filtered_requests)
        receipt["estimated_total"] = sum(
            int(item.get("estimated_count", 0)) for item in filtered_jobs
        )
        receipt["status"] = (
            "accounting_unresolved"
            if receipt["unresolved_job_ids"]
            else "complete"
        )
        receipt["identity_keys_sha256"] = _fingerprint(
            sorted(str(item["identity_key"]) for item in filtered_requests)
        )
    # 副本文件名按去掉观测时间的稳定摘要命名：中断后重放得到同一份副本，账本与 batch 绑定不漂移。
    stable_sha256 = _fingerprint({key: value for key, value in receipt.items() if key != "observed_at_utc"})
    copy_path = receipt_dir / f"provenance-{stable_sha256[:16]}.json"
    stored = _write_or_verify(copy_path, receipt, volatile=("observed_at_utc",))
    with project_ledger.project_lock(project_root):
        initial_keys = project_ledger._initial_keys(project_root, plan)
    accounting = request_accounting(stored, head=head, initial_keys=initial_keys, campaign_id=campaign_id)
    identity_keys = accounting["identity_keys"]
    new_keys = accounting["new_keys"]
    estimated_sources = accounting["estimated_sources"]
    unresolved = accounting["unresolved_job_ids"]
    if unresolved:
        status = "unresolved"
    elif estimated_sources:
        status = "estimated"
    else:
        status = "resolved"
    part = {
        "status": status,
        "identity_keys": new_keys,
        "identity_key_count_total": len(identity_keys),
        "estimated_delta": sum(int(item["estimated_count"]) for item in estimated_sources),
        "estimated_sources": estimated_sources,
        "unresolved_job_ids": unresolved,
        "counting_rule": stored.get("counting_rule"),
        "estimation_policy": stored.get("estimation_policy"),
        "provenance_receipt_sha256": _file_sha256(copy_path),
    }
    binding = _binding(campaign_dir, copy_path, "provenance")
    return part, binding, copy_path


def request_accounting(
    stored: Mapping[str, Any],
    *,
    head: Mapping[str, Any],
    initial_keys: set[str],
    campaign_id: str,
) -> dict[str, Any]:
    """按总账已入账索引，从来源核算结果算出本次应入账的内容（纯计算，不写文件）。

    返回全部身份键、未入账的新身份键、未入账的估计来源（按 producer run 去重）与仍未决的作业；
    accounting-resolve 已核清且特征未变的作业不再算未决。对账（_request_part）与 accounting-resolve
    共用本函数，保证补账与对账同一口径。

    修好接着跑第 63 项（计数规则 v3）：同一 Campaign 内续跑取代复用目录名时，
    精确键已由 provenance 按世代换键；估计来源 ID 与未决作业特征在这里按世代区分，见
    ``_estimated_source_id`` 与 ``_resolution_covers``。v2 写入的来源核算（没有世代字段）按原规则计算，结果不变。
    """

    accounted = set(head.get("accounted_identity_index", []))
    accounted_estimates = set(head.get("accounted_estimated_sources", []))
    identity_keys = sorted(
        {
            str(item["identity_key"])
            for item in stored.get("requests", [])
            if isinstance(item, Mapping) and isinstance(item.get("identity_key"), str)
        }
    )
    new_keys = [key for key in identity_keys if key not in accounted and key not in initial_keys]
    estimated_sources: list[dict[str, Any]] = []
    for job in stored.get("jobs", []):
        if not isinstance(job, Mapping) or job.get("status") != "estimated":
            continue
        for root_entry in job.get("roots", []):
            if not isinstance(root_entry, Mapping) or root_entry.get("first_owner_job_id") != job.get("job_id"):
                continue
            count = 0
            for branch in root_entry.get("branches", []):
                if isinstance(branch, Mapping) and branch.get("status") == "estimated":
                    count += int(branch.get("estimated_count", 0))
            if count <= 0:
                continue
            source_id = _estimated_source_id(campaign_id, root_entry)
            if source_id in accounted_estimates:
                continue
            estimated_sources.append(
                {"source_id": source_id, "job_id": str(job.get("job_id")), "estimated_count": count}
            )
    unresolved = [str(item) for item in stored.get("unresolved_job_ids", [])]
    # 修好接着跑第 12 项：accounting-resolve 已核清、且未决事实（证据根与分支状态）没有变化的作业不再判为未决；
    # 出现新证据时特征随之变化，照常重新核算。第 63 项：续跑取代产生的新一代也算新证据（特征含世代）。
    records = project_ledger.accounting_resolution_records(head, campaign_id)
    if unresolved and records:
        signatures = {(str(item.get("job_id")), str(item.get("signature_sha256"))) for item in records}
        entries = {
            str(item.get("job_id")): item
            for item in stored.get("jobs", [])
            if isinstance(item, Mapping) and item.get("status") == "unresolved"
        }
        unresolved = [
            job_id
            for job_id in unresolved
            if not _resolution_covers(entries.get(job_id, {"job_id": job_id}), job_id, signatures, records)
        ]
    return {
        "identity_keys": identity_keys,
        "new_keys": new_keys,
        "estimated_sources": estimated_sources,
        "unresolved_job_ids": unresolved,
    }


def _estimated_source_id(campaign_id: str, root_entry: Mapping[str, Any]) -> str:
    """证据根整根估计上界的来源 ID（总账按它去重，同一来源只计一次上界）。

    v2：``<campaign>:<producer run 名>``。第 63 项：续跑取代后第 n 代（证据根条目带 ``supersession``）
    若更早世代也有 direct 估计分支，同名来源已被更早世代占用，第 n 代改用 ``…#superseded-<n>``；更早世代
    没有 direct 分支时同名来源从未被占用，沿用 v2 ID（v2 口径下已为这一代入账的不会重复）。更早世代
    无法解析时 provenance 记 ``prior_estimate_branches=True``，按新来源计（可能多计上界、不会漏计）。
    判定只依赖不可变证据，同一代反复对账得到同一 ID，幂等。
    """

    legacy = f"{campaign_id}:{root_entry.get('producer_run_id')}"
    supersession = root_entry.get("supersession")
    if not isinstance(supersession, Mapping):
        return legacy
    generation = supersession.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        return legacy
    if supersession.get("prior_estimate_branches") is False:
        return legacy
    return f"{legacy}#superseded-{generation}"


def _resolution_covers(
    entry: Mapping[str, Any],
    job_id: str,
    signatures: set[tuple[str, str]],
    records: Sequence[Mapping[str, Any]],
) -> bool:
    """未决作业是否已被 accounting-resolve 登记的特征覆盖（第 12 项；第 63 项按世代收紧）。

    先按当前（v3）特征精确匹配。v3 部署前登记的特征是 v2 形态（不含世代，ARM64 第 304 条即是）：只有作业
    涉及续跑取代的根时两种形态才不同，此时 v2 形态特征只覆盖登记时已经存在的那一代——登记时刻（总账事件
    ``recorded_at_utc``）不早于当前这一代的产生时刻（最近一次取代收据的时刻）。更晚重采出的世代、或产生时刻
    查不到（取代收据缺失／损坏），一律不覆盖，重新判未决由 accounting-resolve 补账（失败关闭到账务暂停）。
    """

    if (job_id, unresolved_job_signature(entry)) in signatures:
        return True
    superseded = entry.get("superseded_roots")
    if not isinstance(superseded, list) or not superseded:
        return False
    legacy = unresolved_job_signature(entry, legacy=True)
    try:
        produced = max(
            _timestamp(item.get("superseded_at_utc") if isinstance(item, Mapping) else None, "续跑取代时刻")
            for item in superseded
        )
    except ReconcilerError:
        return False
    for record in records:
        if str(record.get("job_id")) != job_id or str(record.get("signature_sha256")) != legacy:
            continue
        try:
            registered = _timestamp(record.get("recorded_at_utc"), "特征登记时刻")
        except ReconcilerError:
            continue
        if registered >= produced:
            return True
    return False


# 第 63 项：v3 在证据根条目加 ``supersession``（改键与估计的审计事实）、在作业条目加 ``superseded_roots``（世代）。
# 特征只用作业级世代区分代次；证据根条目上的审计事实不进特征，v2 形态与 v2 逐字相同。
_SIGNATURE_ROOT_EXCLUDED_FIELDS = frozenset({"root", "supersession"})


def unresolved_job_signature(job_entry: Mapping[str, Any], *, legacy: bool = False) -> str:
    """未决作业的特征摘要：作业、证据根与各分支状态（不含观测时间）。accounting-resolve 按它登记覆盖范围。

    证据根只取 producer run 名、种类、首个归属作业与分支内容，不取绝对路径：同一 Campaign 经别名根
    （如 ARM64 的 /root/oauth-capture）或真实路径访问时特征必须一致，否则补账后重新对账仍判未决。

    第 63 项：作业涉及续跑取代的根（作业条目带 ``superseded_roots``）时，v3 特征并入各根的世代号，
    重采出的新一代即使形状相同也是不同特征；世代号只由文件系统里的归档个数决定，不含产生时刻，
    取代收据是否可读不影响特征。``legacy=True`` 给出 v2 形态（不含世代），只供兼容 v3 部署前登记的特征。
    作业不涉及续跑取代时两种形态逐字相同。
    """

    roots = [
        {key: value for key, value in item.items() if key not in _SIGNATURE_ROOT_EXCLUDED_FIELDS}
        if isinstance(item, Mapping)
        else item
        for item in job_entry.get("roots", [])
    ]
    payload: dict[str, Any] = {
        "job_id": job_entry.get("job_id"),
        "phase": job_entry.get("phase"),
        "roots": roots,
        "reason": job_entry.get("reason"),
    }
    superseded = job_entry.get("superseded_roots")
    if not legacy and superseded:
        payload["superseded_roots"] = [
            {"producer_run_id": item.get("producer_run_id"), "generation": item.get("generation")}
            if isinstance(item, Mapping)
            else item
            for item in superseded
        ]
    return _fingerprint(payload)


def _require_segment_accounting_scope(
    campaign_dir: Path, summary_path: Path, *, candidate_id: str, attempt_id: str, recovery_revision: str,
) -> None:
    """R11 请求计量：恢复段入账前核对执行集合与复用集合不相交、并集等于基线冻结的 J*，且段结果的执行／复用
    标记与预约逐项一致；复用 Job 的请求已在来源段入账，本段只能按实际执行的 Job 计量。没有复用字段的历史
    预约按“全部执行”核对。"""

    segment_root = summary_path.parent
    try:
        reservation = codex_upgrade._load_attempt_recovery_reservation(
            campaign_dir, segment_root, candidate_id=candidate_id, attempt_id=attempt_id,
            recovery_revision=recovery_revision,
        )
        frozen = sorted(str(item) for item in codex_upgrade._authoritative_recovery_execute_jobs(
            campaign_dir, candidate_id, reservation)[2].get("execute_jobs", []))
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(f"恢复段入账无法取得预约或冻结 J*：{error}") from error
    planned = sorted(str(row.get("id")) for row in reservation.get("planned_jobs", []) if isinstance(row, Mapping))
    execute = [str(item) for item in reservation.get("execute_job_ids", planned)]
    reuse = [str(item) for item in reservation.get("reuse_job_ids", [])]
    if (
        not frozen
        or planned != frozen
        or set(execute) & set(reuse)
        or sorted(set(execute) | set(reuse)) != frozen
        or len(set(execute)) != len(execute)
        or len(set(reuse)) != len(reuse)
    ):
        raise ReconcilerError(
            f"恢复段入账的执行／复用集合非法：execute={sorted(execute)}，reuse={sorted(reuse)}，J*={frozen}"
        )
    summary = _read_json(summary_path, "恢复段摘要")
    reservation_path = segment_root / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME
    if not isinstance(summary.get("reservation"), Mapping) or summary["reservation"].get("sha256") != _file_sha256(reservation_path):
        raise ReconcilerError("恢复段摘要与预约绑定漂移，不能按段入账")
    results = summary.get("results")
    if not isinstance(results, list) or not all(isinstance(row, Mapping) for row in results):
        raise ReconcilerError("恢复段摘要 results 非法")
    dispositions: dict[str, str] = {}
    for row in results:
        job_id = str(row.get("id"))
        if job_id in dispositions:
            raise ReconcilerError(f"恢复段摘要 Job 重复：{job_id}")
        dispositions[job_id] = str(row.get("disposition", "executed"))
    if (
        sorted(dispositions) != frozen
        or sorted(job for job, kind in dispositions.items() if kind == "reused") != sorted(reuse)
        or sorted(job for job, kind in dispositions.items() if kind != "reused") != sorted(execute)
    ):
        raise ReconcilerError("恢复段结果的执行／复用标记与预约不一致，不能按段入账")


def _account_sealed_capture(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    now: str | None,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """把已封存抓包阶段的请求写入项目总账（自身零请求，幂等）。

    总账此前只在失败对账（reconciliation_committed）时入账，成功封存的 Campaign 只停在
    计时账本与 provenance 收据里。这里复用同一套请求部分核算：精确身份键按总账索引与初始
    清单去重，估计上界按 producer run 去重，写一份不带根因的 reconciliation_committed batch
    并立即推送；同一 attempt 重复执行返回既有 batch。
    """

    if phase not in {"official", "candidate"}:
        raise ReconcilerError(f"成功抓包入账阶段非法：{phase!r}")
    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("成功抓包入账只用于 0.154.0 起的完整 VC 链 Campaign")
    if phase == "candidate" and (
        not isinstance(candidate_id, str)
        or not codex_upgrade.SAFE_ID_RE.fullmatch(candidate_id)
    ):
        raise ReconcilerError("account-sealed-candidate 必须提供合法 candidate-id")
    if phase == "official" and candidate_id is not None:
        raise ReconcilerError("official 成功入账不得携带 candidate-id")
    if recovery_revision is not None and (
        phase != "candidate" or not vc_artifacts.RECOVERY_REVISION_RE.fullmatch(recovery_revision)
    ):
        raise ReconcilerError("--attempt-recovery 只用于候选阶段且必须是 ar<k>")
    stage = "capture-official" if phase == "official" else "capture-candidate"
    try:
        sealed = codex_upgrade._load_stage_result(
            campaign_dir,
            stage,
            candidate_id,
            _replay_machine_receipts=False,
        )
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(f"{phase} 阶段结果不可用：{error}") from error
    attempt_binding = sealed.get("attempt")
    if sealed.get("status") != "complete" or not isinstance(attempt_binding, Mapping):
        raise ReconcilerError(f"{phase} 阶段尚未完整封存，先 seal 再入账")
    attempt_path = codex_upgrade._campaign_file(campaign_dir, str(attempt_binding.get("path", "")))
    if not attempt_path.is_file() or _file_sha256(attempt_path) != attempt_binding.get("sha256"):
        raise ReconcilerError("official 阶段绑定的 attempt.json 摘要漂移")
    attempt_id = attempt_path.parent.name
    if not codex_upgrade.SAFE_ID_RE.fullmatch(attempt_id):
        raise ReconcilerError("attempt_id 格式非法")
    # 改造 5 M2：attempt-recovery 基线的增量封存结果绑定恢复段 run-summary；入账按段幂等，
    # 只记本段新增请求（精确身份键按总账索引去重）。
    recovery_binding = sealed.get("recovery")
    if recovery_revision is not None:
        if not isinstance(recovery_binding, Mapping) or recovery_binding.get("recovery_revision") != recovery_revision:
            raise ReconcilerError(f"当前候选阶段结果不是恢复段 {recovery_revision} 的增量封存结果")
        recovery_path = codex_upgrade._campaign_file(campaign_dir, str(recovery_binding.get("path", "")))
        if not recovery_path.is_file() or _file_sha256(recovery_path) != recovery_binding.get("sha256"):
            raise ReconcilerError("阶段结果绑定的 attempt-recovery.json 摘要漂移")
        _require_segment_accounting_scope(
            campaign_dir, recovery_path, candidate_id=str(candidate_id), attempt_id=attempt_id,
            recovery_revision=recovery_revision,
        )
    elif isinstance(recovery_binding, Mapping):
        raise ReconcilerError("当前候选阶段结果是恢复段的增量封存结果，请以 --attempt-recovery ar<k> 入账")
    observed = now or _utc_now()
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    subject = (
        f"sealed-official-{attempt_id}"
        if phase == "official"
        else f"sealed-candidate-{candidate_id}-{attempt_id}"
        + (f"-{recovery_revision}" if recovery_revision is not None else "")
    )
    receipt_dir = _reconciliation_dir(campaign_dir, subject)
    request_part, provenance_binding, _copy_path = _request_part(
        campaign_dir,
        manifest,
        receipt_dir,
        plan=plan,
        head=head,
        project_root=project_root,
        now=observed,
        phase=phase,
    )
    if request_part["status"] == "unresolved":
        raise ReconcilerError(
            f"已封存 {phase} 阶段仍有请求数无法确定的 Job，不能入账："
            + "、".join(request_part["unresolved_job_ids"])
        )
    stage_path = codex_upgrade._stage_path(campaign_dir, stage, candidate_id)[1]
    operation_subject = attempt_id if phase == "official" else f"{candidate_id}:{attempt_id}"
    if recovery_revision is not None:
        operation_subject = f"{operation_subject}:{recovery_revision}"
    payload = {
        "campaign_id": str(manifest["campaign_id"]),
        "subject_kind": f"sealed_{phase}_stage",
        "subject_id": operation_subject,
        "phase": phase,
        "candidate_id": candidate_id,
        "request": request_part,
        "stage_result_sha256": _file_sha256(stage_path),
        "attempt_sha256": str(attempt_binding.get("sha256")),
    }
    if recovery_revision is not None:
        payload["recovery_revision"] = recovery_revision
        payload["attempt_recovery_sha256"] = str(recovery_binding.get("sha256"))
    batch = _commit_batch(
        campaign_dir,
        operation_id=f"account-sealed-{phase}:{operation_subject}",
        event_type="reconciliation_committed",
        payload=payload,
        source={
            "kind": f"sealed_{phase}_accounting",
            "sha256": provenance_binding["sha256"],
        },
        receipt_bindings=[provenance_binding],
    )
    try:
        pushed = project_ledger.reconcile_project_ledger(project_root, campaign_dir=campaign_dir)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账推送失败：{error}") from error
    with project_ledger.project_lock(project_root):
        new_head = project_ledger.replay_head(project_root)
    return {
        "status": "accounted",
        "campaign_id": str(manifest["campaign_id"]),
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "request": {
            "status": request_part["status"],
            "new_identity_keys": len(request_part["identity_keys"]),
            "identity_key_count_total": request_part["identity_key_count_total"],
            "estimated_delta": request_part["estimated_delta"],
            "provenance_receipt_sha256": request_part["provenance_receipt_sha256"],
        },
        "batch": batch,
        "pushed": {k: v for k, v in pushed.items() if k in {"blocked", "head_sequence", "head_sha256"}},
        "project_ledger": {
            "precise_total": new_head.get("precise_total"),
            "estimated_total": new_head.get("estimated_total"),
            "head_sequence": new_head.get("sequence"),
        },
    }


def account_sealed_official(campaign_dir: Path, *, now: str | None = None) -> dict[str, Any]:
    """把已封存 official 阶段的模型请求幂等写入项目总账。"""

    return _account_sealed_capture(
        campaign_dir,
        phase="official",
        candidate_id=None,
        now=now,
    )


def account_sealed_candidate(
    campaign_dir: Path,
    candidate_id: str,
    *,
    now: str | None = None,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """把已封存 Candidate 阶段的模型请求幂等写入项目总账（``recovery_revision``：恢复段的增量封存）。"""

    return _account_sealed_capture(
        campaign_dir,
        phase="candidate",
        candidate_id=candidate_id,
        now=now,
        recovery_revision=recovery_revision,
    )


# ---------------------------------------------------------------------------
# 决策表
# ---------------------------------------------------------------------------


def _decide(
    *,
    head: Mapping[str, Any],
    plan: Mapping[str, Any],
    ledger: Mapping[str, Any],
    identity: Mapping[str, Any],
    environment_status: str,
    campaign_deadline_at_utc: str | None,
    root_cause_id: str,
    root_cause_ids: Iterable[str] | None = None,
    request_status: str,
    now: str,
    forced_terminal_reason: str | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """步骤 5：先入账后判定。返回 decision 与 terminal_reason（停线时）。

    ``campaign_id`` 用于按总账注册的目标版本取根因上限与计数（_ledger_facts 不含版本，不能只靠
    ``ledger``）；缺省时才退回 ``ledger`` 中的 target_version，两者都没有即全局口径（失败关闭）。

    ``forced_terminal_reason`` 用于分类本身即不可恢复的对象（改造 4 的 COMMIT 完整性
    异常，以及 2026-09-22 起动作诊断 declared 为 evidence-integrity 的已封存证据完整性
    异常）：无论总账与账本状态如何都固定停线，其他原因仍逐条登记供审计。
    """

    current = _timestamp(now, "now")
    reasons: list[str] = []
    terminal_reason: str | None = None
    deadline_paused = False
    # 第三批 B3-9（第 10 项②③）：同根因重试上限不再写终态，改为暂停到修复证据登记（campaign-resume 清零该根因）为止。
    root_cause_paused = False

    def stop(reason: str, note: str) -> None:
        nonlocal terminal_reason
        reasons.append(note)
        if terminal_reason is None:
            terminal_reason = reason

    if forced_terminal_reason is not None:
        if forced_terminal_reason not in project_ledger.TERMINAL_REASONS:
            raise ReconcilerError(f"强制终态原因非法：{forced_terminal_reason}")
        stop(forced_terminal_reason, "对象分类本身不可恢复（不可变控制或证据制品完整性异常）")
    # 修好接着跑第 12 项：账务无法核清只暂停（accounting-resolve 补账后继续），不再写 accounting_unresolved 终态。
    accounting_paused = False
    blocking = project_ledger.campaign_blocked(head, campaign_id)
    if blocking:
        # 第三批 B3-10：只有本 Campaign 或无归属的未决账务才暂停本 Campaign；其它 Campaign 的未决不计。
        accounting_paused = True
        reasons.append(f"总账 blocked：{blocking}（暂停：accounting-resolve 补账后继续）")
    elif request_status == "unresolved":
        accounting_paused = True
        reasons.append("本次请求账务无法确定（暂停：accounting-resolve 补账后继续）")
    # 修好接着跑第 13 项：未隔离的环境污染只暂停（修复环境后 environment-isolate 隔离污染 attempt 继续）；
    # 官方已封存且官方侧受污染时 Campaign 内无法重采官方证据，才写 environment_contaminated 终态。
    environment_paused = False
    if environment_status == "official_sealed_contaminated":
        stop("environment_contaminated", "官方已封存且官方侧存在未隔离的环境污染：Campaign 内无法重采官方证据")
    elif environment_status == "contaminated":
        environment_paused = True
        reasons.append("存在未隔离的环境污染（暂停：修复环境、取得干净环境复核后以 environment-isolate 隔离继续）")
    ledger_status = ledger.get("status")
    if ledger_status == "abandoned":
        raise ReconcilerError("Campaign 已显式放弃，不再生成恢复批准；两账未闭合时重跑原 campaign-abandon")
    # 已经写入 stop_the_line 的旧 Campaign 不能被后续工具或策略身份变化改写终态。
    # 身份漂移仍加入 reasons，供审计判断当前工具为何不能恢复旧 attempt。
    if ledger_status == "stopped":
        stop("prior_stop_the_line", "Campaign 账本此前已写 stop_the_line，禁止恢复旧 attempt")
    if not identity.get("unchanged"):
        stop("identity_changed", "当前有效 wire 身份或策略摘要已变化")
    if ledger_status == "stop_required":
        # 第三批 B3-9（第 10 项②）：账本同根因重试上限不再写终态——暂停，campaign-resume 登记修复证据、清零该根因后继续。
        root_cause_paused = True
        reasons.append("Campaign 账本同根因重试已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）")
    elif ledger_status == "deadline_paused":
        deadline_paused = True
        reasons.append("Campaign 计时预算已暂停")
    elif ledger_status == "complete":
        stop("prior_upgrade_complete", "Campaign 账本此前已完成，禁止再对账旧 attempt")
    if campaign_deadline_at_utc is not None and current >= _timestamp(campaign_deadline_at_utc, "Campaign deadline"):
        deadline_paused = True
        reasons.append("Campaign 总预算有效截止已到")
    if current >= _timestamp(head.get("effective_absolute_deadline_utc", plan["absolute_deadline_utc"]), "absolute_deadline_utc"):
        deadline_paused = True
        reasons.append("项目绝对有效截止已到")
    remaining = head.get("remaining_live_requests")
    budget_paused = False
    if remaining is not None and int(remaining) <= 0:
        # 修好接着跑第 14 项：请求预算耗尽只暂停（与时间预算同口径），批准 request-budget-extend 后继续；
        # 不再写 deadline_live_requests 终态（该原因只供历史回放）。
        budget_paused = True
        reasons.append("项目请求预算已耗尽（暂停：批准请求预算延长后继续）")
    evaluated_root_causes = list(
        dict.fromkeys(root_cause_ids or [root_cause_id])
    )
    # 根因上限与计数按本 Campaign 的目标版本取：旧版本项目的同步骤记录不再累计进来。
    target_version = (
        project_ledger.campaign_target_version(head, campaign_id) if campaign_id else None
    ) or ledger.get("target_version")
    at_limit = sorted(
        set(evaluated_root_causes)
        & set(project_ledger.root_causes_at_limit_for(head, target_version))
    )
    if at_limit:
        # 第三批 B3-9（第 10 项②③）：只挡本次要重试的根因，且不写终态——暂停到修复证据登记为止。
        root_cause_paused = True
        reasons.append(f"根因 {at_limit} 累计失败已达上限（暂停：campaign-resume 登记修复证据、清零该根因后继续）")
    decision = (
        DECISION_STOP
        if terminal_reason is not None
        else DECISION_PAUSED
        if deadline_paused or budget_paused or accounting_paused or environment_paused or root_cause_paused
        else DECISION_RECOVERABLE
    )
    scoped_counts = project_ledger.root_cause_counts_for(head, target_version)
    root_cause_counts = {
        cause_id: int(scoped_counts.get(cause_id, 0))
        for cause_id in evaluated_root_causes
    }
    extra: dict[str, Any] = {}
    if decision == DECISION_PAUSED:
        # 暂停种类只在暂停时写入，其余判定的输出字节不变。
        extra["pause_kinds"] = [
            kind
            for kind, on in (
                ("deadline", deadline_paused),
                ("request_budget", budget_paused),
                ("accounting", accounting_paused),
                ("environment", environment_paused),
                ("root_cause_repair", root_cause_paused),
            )
            if on
        ]
    return {
        **extra,
        "decision": decision,
        "terminal_reason": terminal_reason,
        "reasons": reasons,
        "root_cause_count": root_cause_counts.get(root_cause_id, 0),
        "root_cause_counts": root_cause_counts,
        "remaining_live_requests": remaining,
        "blocked": bool(head.get("blocked")),
    }


# ---------------------------------------------------------------------------
# 账本与总账写入（步骤 1、2、3、6）
# ---------------------------------------------------------------------------


def _append_ledger_event(ledger_dir: Path, **kwargs: Any) -> dict[str, Any]:
    event_id = str(kwargs["event_id"])
    existing = _ledger_event_sha256(ledger_dir, event_id)
    if existing is not None:
        return {"event_id": event_id, "event_sha256": existing, "appended": False}
    try:
        timing_ledger.append_event(ledger_dir, **kwargs)
    except timing_ledger.TimingLedgerError as error:
        raise ReconcilerError(f"Campaign 账本拒绝 {kwargs.get('event_type')}：{error}") from error
    return {"event_id": event_id, "event_sha256": _ledger_event_sha256(ledger_dir, event_id), "appended": True}


def _commit_batch(
    campaign_dir: Path,
    *,
    operation_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
    receipt_bindings: list[Mapping[str, Any]],
) -> dict[str, Any]:
    try:
        with project_ledger.campaign_ledger_lock(campaign_dir) as ledger_dir:
            return project_ledger.write_batch(
                ledger_dir,
                operation_id=operation_id,
                event_type=event_type,
                payload=payload,
                source=source,
                receipt_bindings=receipt_bindings,
            )
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"outbox batch 写入失败：{error}") from error


def _push_and_replay(project_root: Path, campaign_dir: Path, *, now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        pushed = project_ledger.reconcile_project_ledger(
            project_root, campaign_dir=campaign_dir, now=_timestamp(now, "now")
        )
        head = project_ledger.replay_head(project_root)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账推送或重放失败：{error}") from error
    return pushed, head


def _permanent_stop(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    ledger_dir: Path,
    *,
    subject_id: str,
    root_cause_id: str,
    terminal_reason: str,
    receipt_bindings: list[dict[str, str]],
    ledger_receipts: list[dict[str, str]],
    live_request_count: int,
    reconciliation_receipt_sha256: str,
    ledger_facts: Mapping[str, Any],
) -> dict[str, Any]:
    """步骤 6 停线分支：stage_abandoned → stop_the_line → campaign_terminal batch。"""

    next_action = f"permanent-stop-{terminal_reason}"
    # 修好接着跑第 11 项：campaign-resume 撤销终态后同一对象可能再次停线；事件 ID 与总账 operation 按恢复
    # 纪元加后缀，否则会被幂等键静默吞掉（纪元 0 保持原字节，历史幂等不变）。
    epoch = _campaign_resume_epoch(campaign_dir, str(manifest["campaign_id"]))
    suffix = f"-e{epoch}" if epoch else ""
    events: list[dict[str, Any]] = []
    status = str(ledger_facts.get("status"))
    if status not in {"stopped", "complete"}:
        summary = timing_ledger.inspect_ledger(ledger_dir)
        active_phase = summary.get("active_phase")
        if active_phase is not None:
            events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-stage-abandoned-{subject_id}{suffix}",
                    phase=str(active_phase),
                    event_type="stage_abandoned",
                    root_cause_id=root_cause_id,
                    next_action=next_action,
                )
            )
        summary = timing_ledger.inspect_ledger(ledger_dir)
        if summary["status"] != "stopped":
            events_raw = timing_ledger._load_events(ledger_dir)
            last_phase = str(events_raw[-1][0]["phase"])
            events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-stop-the-line-{subject_id}{suffix}",
                    phase=last_phase,
                    event_type="stop_the_line",
                    root_cause_id=root_cause_id,
                    live_request_count=live_request_count,
                    receipts=[dict(item) for item in ledger_receipts],
                    next_action=next_action,
                )
            )
    batch = _commit_batch(
        campaign_dir,
        operation_id=f"campaign-terminal:{manifest['campaign_id']}" + (f":e{epoch}" if epoch else ""),
        event_type="campaign_terminal",
        payload={
            "campaign_id": str(manifest["campaign_id"]),
            "terminal_reason": terminal_reason,
            "root_cause_id": root_cause_id,
            "subject_id": subject_id,
            "reconciliation_receipt_sha256": reconciliation_receipt_sha256,
        },
        source={"kind": "reconciliation", "sha256": reconciliation_receipt_sha256},
        receipt_bindings=[dict(item) for item in receipt_bindings],
    )
    return {"ledger_events": events, "terminal_batch": batch, "next_action": next_action}


# ---------------------------------------------------------------------------
# reconcile-attempt
# ---------------------------------------------------------------------------


def _locate_attempt(campaign_dir: Path, attempt_id: str) -> tuple[str, str | None, Path]:
    if not codex_upgrade.SAFE_ID_RE.fullmatch(attempt_id):
        raise ReconcilerError("attempt_id 格式非法")
    for phase, candidate_id, attempt_root in codex_upgrade._campaign_attempt_roots(campaign_dir):
        if attempt_root.name == attempt_id:
            return phase, candidate_id, attempt_root
    raise ReconcilerError(f"Campaign 内没有 attempt：{attempt_id}")


def _checkpoint_facts(attempt_root: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """重放追加式 checkpoint 链；返回 (records, 按 item 的最新记录, 链摘要)。"""

    checkpoint_root = attempt_root / "checkpoints"
    if not checkpoint_root.is_dir() or checkpoint_root.is_symlink():
        return [], {}, None
    try:
        store = incremental_recovery.CheckpointStore(checkpoint_root, create=False)
        records = store.records()
    except (OSError, incremental_recovery.IncrementalRecoveryError) as error:
        raise ReconcilerError(f"attempt 的 checkpoint 链无法重放：{error}") from error
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        item = record.get("item_id")
        if isinstance(item, str):
            latest[item] = record
    chain = {
        "path": str(checkpoint_root),
        "record_count": len(records),
        "last_sequence": records[-1].get("checkpoint_sequence") if records else None,
        "last_sha256": records[-1].get("checkpoint_sha256") if records else None,
    }
    return records, latest, chain


def _job_evidence_present(campaign_dir: Path, manifest: Mapping[str, Any], job_id: str) -> bool:
    """证据目录只用于请求计数与 indeterminate 判定，不当作完成证明。"""

    configuration = manifest.get("configuration") or {}
    capture_root = Path(str(configuration.get("capture_root", "")))
    try:
        host_data_root = closeout._formal_host_data_root(campaign_dir)
    except closeout.VC0CloseoutError:
        return False
    for item in manifest.get("jobs", []):
        if not isinstance(item, Mapping) or item.get("id") != job_id:
            continue
        for value in item.get("evidence_roots", []) or []:
            candidate = Path(str(value))
            if candidate.is_absolute() and (candidate == host_data_root or host_data_root in candidate.parents):
                mapped = candidate
            else:
                try:
                    mapped = closeout._map_container_evidence_root(
                        value, capture_root=capture_root, host_data_root=host_data_root
                    )
                except closeout.VC0CloseoutError:
                    continue
            for root in closeout._failed_attempt_roots(mapped):
                if root.is_dir() and not root.is_symlink() and any(root.iterdir()):
                    return True
    return False


def _classify_jobs(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    reservation: Mapping[str, Any],
    attempt: Mapping[str, Any] | None,
    latest_checkpoints: Mapping[str, Mapping[str, Any]],
    attempt_root: Path,
) -> dict[str, Any]:
    """Job 完成态：complete 须同时有有效 result 与对应 checkpoint；有证据无终态 checkpoint 为 indeterminate。"""

    planned = [str(row["id"]) for row in reservation["planned_jobs"]]
    execution = {str(row["id"]): str(row["execution_sha256"]) for row in reservation["planned_jobs"]}
    results: dict[str, Mapping[str, Any]] = {}
    if attempt is not None:
        for item in attempt.get("results", []):
            if isinstance(item, Mapping) and isinstance(item.get("id"), str):
                results[item["id"]] = item
    else:
        for job_id in planned:
            job_path = attempt_root / f"job-{job_id}.json"
            if job_path.is_file() and not job_path.is_symlink():
                payload = _read_json(job_path, f"Job 收据 {job_id}")
                results[job_id] = payload
    states: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    for job_id in planned:
        record = latest_checkpoints.get(job_id)
        result = results.get(job_id)
        checkpoint_status = record.get("status") if isinstance(record, Mapping) else None
        result_status = result.get("status") if isinstance(result, Mapping) else None
        execution_ok = (
            isinstance(result, Mapping) and result.get("execution_sha256") == execution[job_id]
        )
        egress_trusted = _job_egress_trusted(result) if isinstance(result, Mapping) else False
        if checkpoint_status == "complete" and result_status == "complete" and execution_ok and egress_trusted:
            state = "complete"
        elif result_status == "failed" or checkpoint_status == "failed":
            state = "failed"
        elif record is None and result is None:
            state = "indeterminate" if _job_evidence_present(campaign_dir, manifest, job_id) else "pending"
        else:
            # 有 checkpoint 或 result 但未构成一致的终态：视为不确定，归入失败集合。
            state = "indeterminate"
        states[job_id] = state
        details[job_id] = {
            "checkpoint_status": checkpoint_status,
            "result_status": result_status,
            "execution_sha256_matches": execution_ok,
            "disposition": result.get("disposition") if isinstance(result, Mapping) else None,
            **({"runtime_egress_trusted": egress_trusted} if isinstance(result, Mapping) and result.get("runtime_egress") is not None else {}),
        }
    grouped = {state: sorted(j for j, s in states.items() if s == state) for state in JOB_STATES}
    return {"planned_job_ids": sorted(planned), "states": states, "groups": grouped, "details": details}


def _environment_decision_status(decision: Mapping[str, Any]) -> str:
    """把 codex_upgrade._campaign_environment_decision 映射为 _decide 的环境口径（clear 记为 restored，旧字节不变）。"""

    status = str(decision["status"])
    return "restored" if status == "clear" else status


def _environment_facts(
    campaign_dir: Path,
    attempt_root: Path,
    attempt: Mapping[str, Any] | None,
    contamination: list[str],
    *,
    decision: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """环境已恢复（after 探针与恢复收据在位）／未恢复／污染。

    ``status`` 是本 attempt 自身的环境（决定能否复用已完成作业）；``decision_status`` 是判定口径
    （修好接着跑第 13 项）：只看未隔离的污染事实与官方已封存受污染，本 attempt 自身已被隔离时不再停线，
    before 探针都没取到（没有执行任何 Job）时的恢复错误也不再误判为污染。
    """

    evidence_root = attempt_root / "evidence"
    before = evidence_root / "environment" / "before" / "probe-manifest.json"
    after = evidence_root / "environment" / "after" / "probe-manifest.json"
    restoration = evidence_root / "receipts" / "restoration-report.json"
    status = "unrestored"
    restoration_error = None
    if contamination:
        status = "contaminated"
    elif attempt is not None:
        environment = attempt.get("environment") if isinstance(attempt.get("environment"), Mapping) else {}
        restoration_error = attempt.get("restoration_error")
        if (
            attempt.get("status") == "environment_contaminated"
            or restoration_error is not None
            # Kilo 后环境恢复失败（seal-failure）：该 attempt 的结果永不复用。
            or (attempt_root / "seal-failure.json").exists()
        ):
            status = "contaminated"
        elif (
            str(attempt.get("phase")),
            attempt.get("candidate_id"),
            str(attempt.get("attempt_id")),
        ) in codex_upgrade._conflict_quarantined_attempts(campaign_dir):
            # 修好接着跑第 24 项：被证据根冲突隔离的 attempt 证据不可信（被误归档、增量复用或覆写），不复用、全部重跑。
            status = "conflict_quarantined"
        elif codex_upgrade._attempt_continuity_drifted(attempt):
            # 修好接着跑第 22 项：因环境连续性漂移失败的 attempt，它承接的结果证据前提不成立，不复用、全部重跑。
            status = "continuity_drift"
        elif environment.get("after_probe") is not None and environment.get("restoration_report") is not None:
            status = "restored"
    elif after.is_file() and restoration.is_file():
        status = "restored"
    return {
        "decision_status": (
            _environment_decision_status(decision)
            if decision is not None
            else ("contaminated" if contamination else status)
        ),
        "status": status,
        "before_probe_present": before.is_file(),
        "after_probe_present": after.is_file(),
        "restoration_report_present": restoration.is_file(),
        "restoration_error": restoration_error,
        "contamination_records": list(contamination),
    }


def _attempt_root_cause(
    *,
    phase: str,
    attempt: Mapping[str, Any] | None,
    environment_status: str,
    identity_unchanged: bool,
    deadline_expired: bool,
    jobs: Mapping[str, Any],
) -> dict[str, Any]:
    """生成硬停线或历史 fallback 根因；账本 ``stopped`` 本身不是 deadline 证据。

    修好接着跑第 38 项：请求账务无法核清不再是根因。账务未决是"失败之后请求数核算不了"的记账状态，不是 attempt
    失败的原因：它由判定按请求部分 unresolved／总账 blocked 暂停（accounting-resolve 补账后继续），不进根因计数。
    否则它会盖住环境污染、中断等真实原因——修复证据只能绑到账务根因上，而不同真实原因引起的账务未决又都累计到
    同一个账务根因（与第 32 项同构）。优先级：环境污染 > 工具身份变化 > 到期 > 中断。
    """

    groups = jobs["groups"]
    failed_step = "reservation"
    if groups["failed"]:
        failed_step = groups["failed"][0]
    elif groups["indeterminate"]:
        failed_step = groups["indeterminate"][0]
    elif attempt is not None and attempt.get("restoration_error") is not None:
        failed_step = "restoration"
    elif groups["complete"]:
        failed_step = "after-" + groups["complete"][-1]
    code = "attempt.interrupted"
    if environment_status == "contaminated":
        code = "attempt.environment-contaminated"
    elif not identity_unchanged:
        code = "attempt.identity-changed"
    elif deadline_expired:
        code = "attempt.deadline-expired"
    try:
        return root_cause.describe_root_cause(
            component=COMPONENT,
            stable_error_code=code,
            failed_step=failed_step,
            stable_dimensions={"phase": phase},
        )
    except root_cause.RootCauseError as error:
        raise ReconcilerError(f"根因编码失败：{error}") from error


def _attempt_deadline_expired(
    *,
    attempt: Mapping[str, Any] | None,
    ledger: Mapping[str, Any],
    plan: Mapping[str, Any],
    campaign_deadline_at_utc: str | None,
    now: str,
    project_ledger_root: Path | None = None,
) -> bool:
    """只用 attempt 当时的时间或 timeout 收据识别根因，不能从 stopped 倒推。

    已封存 attempt 在次日对账时，``now`` 只能影响是否还允许恢复，不能把此前的
    Job 故障改写成 deadline 根因；没有 attempt 的 reservation 孤儿才使用对账时间。
    """

    if attempt is not None and (
        (
            isinstance(attempt.get("watchdog"), Mapping)
            and attempt["watchdog"].get("timeout_checkpoint") is not None
        )
        or attempt.get("deadline_orphan_finalization") is not None
    ):
        return True
    reference_value = (
        attempt.get("completed_at_utc") if attempt is not None else now
    )
    reference = _timestamp(
        str(reference_value),
        "attempt.completed_at_utc" if attempt is not None else "now",
    )
    # R8 审核修正：Campaign 层与阶段层只计参考时刻之前已批准的延期。某层在参考时刻之后第一份延期记录的
    # original_deadline_at_utc 就是该层在参考时刻的有效截止；先延期后对账不得把当时的到期改判成别的根因。
    extensions = [row for row in ledger.get("deadline_extensions") or [] if isinstance(row, Mapping)]
    later = [row for row in extensions if _timestamp(str(row.get("approved_at_utc")), "延期批准时间") > reference]
    current_phase = ledger.get("active_phase") or ledger.get("review_phase")

    def as_of_reference(value: Any, scope: str) -> Any:
        rows = [row for row in later if row.get("scope") == scope and (scope != "stage" or row.get("phase") == current_phase)]
        return rows[0].get("original_deadline_at_utc") if rows else value

    deadlines = [
        as_of_reference(campaign_deadline_at_utc, "campaign"),
        as_of_reference(ledger.get("total_deadline_at_utc"), "campaign"),
        as_of_reference(ledger.get("stage_deadline_at_utc"), "stage"),
        (project_ledger.effective_project_deadline(project_ledger_root, as_of=reference)
         if project_ledger_root is not None else plan.get("absolute_deadline_utc")),
    ]
    return any(
        isinstance(value, str) and reference >= _timestamp(value, "deadline")
        for value in deadlines
    )


def _attempt_recorded_failures(
    attempt: Mapping[str, Any] | None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], bool]:
    """读取新数组；历史 attempt 只读地从既有 Job 枚举字段派生。"""

    if attempt is None:
        return [], [], False
    if "failure_observations" not in attempt and "root_causes" not in attempt:
        try:
            observations, causes = codex_upgrade._attempt_failure_facts(attempt)
        except codex_upgrade.ConfigurationError as error:
            raise ReconcilerError(f"历史 attempt 失败观测无法重放：{error}") from error
        # 历史文件保持原字节不变；只在本次追加式 reconciliation 中发布可复算数组。
        return observations, causes, bool(observations or causes)
    raw_observations = attempt.get("failure_observations")
    raw_causes = attempt.get("root_causes")
    if not isinstance(raw_observations, list) or not isinstance(raw_causes, list):
        raise ReconcilerError("attempt 失败观测与根因数组不完整")
    observations = [dict(item) for item in raw_observations if isinstance(item, Mapping)]
    causes = [dict(item) for item in raw_causes if isinstance(item, Mapping)]
    if len(observations) != len(raw_observations) or len(causes) != len(raw_causes):
        raise ReconcilerError("attempt 失败观测或根因数组含非对象项")
    observation_ids = [str(item.get("root_cause_id", "")) for item in observations]
    cause_ids = [str(item.get("root_cause_id", "")) for item in causes]
    if (
        len(set(observation_ids)) != len(observation_ids)
        or any(not root_cause.is_structured(value) for value in observation_ids)
        or any(not root_cause.is_structured(value) for value in cause_ids)
        or set(observation_ids) - set(cause_ids)
    ):
        raise ReconcilerError("attempt 失败观测与根因身份不闭合")
    return observations, causes, True


def _merge_root_causes(
    primary: Mapping[str, Any],
    recorded: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """保持主根因在首位，并按稳定 ID 去重。"""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [primary, *recorded]:
        cause_id = str(item.get("root_cause_id", ""))
        if cause_id in seen:
            continue
        seen.add(cause_id)
        merged.append(dict(item))
    return merged


def _attempt_effective_root_causes(
    *,
    phase: str,
    attempt: Mapping[str, Any] | None,
    environment_status: str,
    identity_unchanged: bool,
    deadline_expired: bool,
    jobs: Mapping[str, Any],
    ledger_status: Any,
    recorded_root_causes: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """attempt 失败的有效根因：主根因与按稳定 ID 去重的根因数组（主根因在首位）。

    结构化 Job／门禁观测优先于普通 interrupted，也不能被历史 stopped 倒推成 deadline。旧 Campaign 已经 stopped 时，
    当前部署造成的身份漂移只决定不能恢复，不能追溯改写 attempt 当时已经结构化记录的失败根因。首次对账与第 38 项的
    续接重归属共用本函数，保证同一组事实得到同一根因。
    """

    fallback = _attempt_root_cause(
        phase=phase,
        attempt=attempt,
        environment_status=environment_status,
        identity_unchanged=identity_unchanged,
        deadline_expired=deadline_expired,
        jobs=jobs,
    )
    recorded_preferred = fallback["stable_error_code"] == "attempt.interrupted" or (
        ledger_status == "stopped" and fallback["stable_error_code"] == "attempt.identity-changed"
    )
    cause = dict(recorded_root_causes[0]) if recorded_root_causes and recorded_preferred else fallback
    return cause, _merge_root_causes(cause, recorded_root_causes)


# 修好接着跑第 38 项：第 38 项之前的首次对账会把 attempt.accounting-unresolved 写成主根因（下称"账务占位根因"）。对账收据
# 只写一次、续接以首次收据为准（第 37 项），占位根因于是永远盖住真实根因：补账后真实根因不入账，修复证据只能绑到
# 账务根因上。续接时识别占位根因，按首次收据记录的事实（不看当前环境、身份与账务）重算真实根因，写 write-once 的
# 重归属收据，再以追加式历史更正（reconciliation_corrected）把总账里该 operation 的根因改记为真实根因：原事件字节
# 不动，重放时计数从占位根因移到真实根因（占位根因不计数）。
ACCOUNTING_PLACEHOLDER_CODE = "attempt.accounting-unresolved"
ROOT_CAUSE_REATTRIBUTION_SCHEMA = "attempt-root-cause-reattribution/v1"
ROOT_CAUSE_REATTRIBUTION_NAME = "root-cause-reattribution.json"
_PAYLOAD_ROOT_CAUSE_FIELDS = ("root_cause_id", "stable_error_code", "failed_step", "stable_dimensions", "component")


def _payload_root_cause(cause: Mapping[str, Any]) -> dict[str, Any]:
    """总账 payload 与重归属收据里的根因形态：只留身份字段，不含随枚举表变化的 codes_sha256。"""

    return {field: cause[field] for field in _PAYLOAD_ROOT_CAUSE_FIELDS if field in cause}


def _legacy_accounting_placeholder(stored: Mapping[str, Any]) -> bool:
    """首次对账收据的主根因是否为第 38 项之前的账务占位根因。"""

    cause = stored.get("root_cause")
    return isinstance(cause, Mapping) and cause.get("stable_error_code") == ACCOUNTING_PLACEHOLDER_CODE


def _reattributed_root_causes(
    stored: Mapping[str, Any],
    *,
    phase: str,
    attempt: Mapping[str, Any] | None,
    plan: Mapping[str, Any],
    project_root: Path,
    array_contract: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """按首次对账收据记录的事实重算真实根因，返回（主根因、根因数组、重算依据）。

    只读首次收据里不可变的事实（环境状态、工具身份是否未变、账本状态与截止、Job 分组、观测时刻）与不可变的 attempt，
    所以任何时候续接都得到同一结果；数组合同下保留首次收据里除占位根因外的已记录根因。
    """

    environment = stored.get("environment")
    identity = stored.get("tool_identity")
    ledger = stored.get("campaign_ledger")
    jobs = stored.get("jobs")
    if not (
        isinstance(environment, Mapping)
        and isinstance(environment.get("status"), str)
        and isinstance(identity, Mapping)
        and isinstance(identity.get("unchanged"), bool)
        and isinstance(ledger, Mapping)
        and isinstance(jobs, Mapping)
        and isinstance(jobs.get("groups"), Mapping)
        and isinstance(stored.get("observed_at_utc"), str)
    ):
        raise ReconcilerError("首次对账收据缺少重算真实根因所需的事实（environment／tool_identity／campaign_ledger／jobs）")
    deadline_expired = _attempt_deadline_expired(
        attempt=attempt,
        ledger=ledger,
        plan=plan,
        campaign_deadline_at_utc=stored.get("campaign_deadline_at_utc"),
        now=str(stored["observed_at_utc"]),
        project_ledger_root=project_root,
    )
    recorded = (
        [
            dict(item)
            for item in stored.get("root_causes") or []
            if isinstance(item, Mapping) and item.get("stable_error_code") != ACCOUNTING_PLACEHOLDER_CODE
        ]
        if array_contract
        else []
    )
    cause, root_causes = _attempt_effective_root_causes(
        phase=phase,
        attempt=attempt,
        environment_status=str(environment["status"]),
        identity_unchanged=bool(identity["unchanged"]),
        deadline_expired=deadline_expired,
        jobs=jobs,
        ledger_status=ledger.get("status"),
        recorded_root_causes=recorded,
    )
    basis = {
        "environment_status": str(environment["status"]),
        "identity_unchanged": bool(identity["unchanged"]),
        "deadline_expired": deadline_expired,
        "ledger_status": ledger.get("status"),
        "job_groups": {state: list(jobs["groups"].get(state, [])) for state in JOB_STATES},
    }
    return cause, root_causes, basis


def _write_root_cause_reattribution(
    receipt_path: Path,
    *,
    campaign_id: str,
    attempt_id: str,
    recovery_revision: str | None,
    operation_id: str,
    placeholder: Mapping[str, Any],
    cause: Mapping[str, Any],
    root_causes: Sequence[Mapping[str, Any]],
    basis: Mapping[str, Any],
) -> dict[str, Any]:
    """写重归属收据（与对账收据同目录、write-once、不含时间戳，重跑逐字相同）并返回其绑定。"""

    payload = {
        "schema_version": ROOT_CAUSE_REATTRIBUTION_SCHEMA,
        "campaign_id": campaign_id,
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "operation_id": operation_id,
        "reconciliation_receipt_sha256": _file_sha256(receipt_path),
        "placeholder_root_cause": _payload_root_cause(placeholder),
        "root_cause": _payload_root_cause(cause),
        "root_causes": [_payload_root_cause(item) for item in root_causes],
        "basis": dict(basis),
        "reason": "修好接着跑第 38 项：请求账务无法核清只是暂停原因、不是失败根因；按首次对账收据的事实把账务占位根因重归属为真实根因。",
    }
    path = receipt_path.parent / ROOT_CAUSE_REATTRIBUTION_NAME
    _write_or_verify(path, payload, volatile=())
    return {"path": path, "sha256": _file_sha256(path)}


def _root_cause_identity(payload: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """payload 的根因身份：主根因 ID 与全部根因 ID（排序去重）。只比 ID，不受 codes_sha256 等审计字段影响。"""

    primary = payload.get("root_cause")
    primary_id = str(primary["root_cause_id"]) if isinstance(primary, Mapping) and primary.get("root_cause_id") else None
    ids = {primary_id} if primary_id is not None else set()
    for item in payload.get("root_causes") or []:
        if isinstance(item, Mapping) and item.get("root_cause_id"):
            ids.add(str(item["root_cause_id"]))
    return primary_id, tuple(sorted(ids))


def _ensure_reattribution_correction(
    project_root: Path,
    *,
    operation_id: str,
    placeholder_id: str,
    cause: Mapping[str, Any],
    root_causes: Sequence[Mapping[str, Any]],
    reattribution_sha256: str,
) -> dict[str, Any]:
    """让总账中该 operation 的有效根因等于重归属后的真实根因（幂等）。

    - 原事件已按真实根因写入（outbox 在本次续接中才由新代码生成）→ ``not_needed``；
    - 已有同一真实根因的历史更正 → ``already_corrected``；已有别的更正 → 失败关闭（与人工更正冲突，需审计）；
    - 否则原事件根因必须含占位根因，追加 ``reconciliation_corrected``：corrected_payload 是原 payload 的副本，只把
      主根因换成真实根因、根因数组去掉占位项并以真实根因为首；请求部分、失败观测与 Campaign 归属逐字不变，所以
      账务（含其后的 accounting_resolved 补账）与按目标版本分桶都照旧重放。
    """

    expected = (
        str(cause["root_cause_id"]),
        tuple(sorted({str(item["root_cause_id"]) for item in root_causes} | {str(cause["root_cause_id"])})),
    )
    try:
        events = project_ledger._load_events(project_root)
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"项目总账事件读取失败：{error}") from error
    original = next((event for event in events if event["operation_id"] == operation_id), None)
    if original is None or original["event_type"] != "reconciliation_committed":
        raise ReconcilerError(f"对账 operation {operation_id} 未以 reconciliation_committed 进入项目总账，无法重归属根因")
    corrections = [
        event
        for event in events
        if event["event_type"] == "reconciliation_corrected"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("original_operation_id") == operation_id
    ]
    if corrections:
        corrected_payload = corrections[0]["payload"].get("corrected_payload")
        if isinstance(corrected_payload, Mapping) and _root_cause_identity(corrected_payload) == expected:
            return {
                "status": "already_corrected",
                "operation_id": corrections[0]["operation_id"],
                "corrected_payload_sha256": corrections[0]["payload"].get("corrected_payload_sha256"),
            }
        raise ReconcilerError(
            f"operation {operation_id} 已有其它历史更正，其根因与真实根因重归属不一致；需人工审计后再续接"
        )
    original_payload = original["payload"]
    original_identity = _root_cause_identity(original_payload)
    if original_identity == expected:
        return {"status": "not_needed", "operation_id": None, "corrected_payload_sha256": None}
    if placeholder_id not in original_identity[1]:
        raise ReconcilerError(
            f"operation {operation_id} 在总账的根因既不是账务占位根因也不是重算的真实根因，拒绝自动更正"
        )
    corrected = json.loads(json.dumps(original_payload, ensure_ascii=False))
    corrected["root_cause"] = _payload_root_cause(cause)
    if "root_causes" in original_payload:
        kept = [
            dict(item)
            for item in original_payload.get("root_causes") or []
            if isinstance(item, Mapping) and item.get("root_cause_id") != placeholder_id
        ]
        primary = next((item for item in kept if item.get("root_cause_id") == cause["root_cause_id"]), dict(cause))
        corrected["root_causes"] = _merge_root_causes(primary, kept)
    reason = (
        f"修好接着跑第 38 项：账务未决不是失败根因。把账务占位根因 {placeholder_id}（{ACCOUNTING_PLACEHOLDER_CODE}）"
        f"重归属为真实根因 {cause['root_cause_id']}（{cause['stable_error_code']}），依据首次对账收据的事实；"
        f"重归属收据 sha256 {reattribution_sha256}。"
    )
    try:
        written = project_ledger.record_historical_reconciliation_correction(
            project_root,
            original_operation_id=operation_id,
            corrected_payload=corrected,
            reason=reason,
            original_event_sha256=original["event_sha256"],
            original_payload_sha256=original["payload_sha256"],
        )
    except project_ledger.ProjectLedgerError as error:
        raise ReconcilerError(f"总账根因重归属更正失败：{error}") from error
    return {
        "status": written["status"],
        "operation_id": written["operation_id"],
        "corrected_payload_sha256": written["corrected_payload_sha256"],
    }


def receipt_root_cause_ids(receipt_path: Path) -> list[str]:
    """attempt 对账收据的有效根因 ID（主根因在前、去重）。

    修好接着跑第 38 项：同目录存在重归属收据时以它为准（首次收据的账务占位根因已在总账更正为真实根因）；重归属收据
    存在却不绑定本收据即失败关闭。续跑门禁（resume 只挡本次根因）与恢复预览现场复核共用本函数，真实根因达上限时
    不会因为读到占位根因而被绕过。
    """

    receipt = _read_json(receipt_path, "attempt 对账收据")
    source: Mapping[str, Any] = receipt
    reattribution_path = receipt_path.parent / ROOT_CAUSE_REATTRIBUTION_NAME
    if reattribution_path.exists() or reattribution_path.is_symlink():
        reattribution = _read_json(reattribution_path, "根因重归属收据")
        if (
            reattribution.get("schema_version") != ROOT_CAUSE_REATTRIBUTION_SCHEMA
            or reattribution.get("reconciliation_receipt_sha256") != _file_sha256(receipt_path)
        ):
            raise ReconcilerError("根因重归属收据与同目录的对账收据不绑定")
        source = reattribution
    ids: list[str] = []
    for item in [source.get("root_cause"), *(source.get("root_causes") or [])]:
        if isinstance(item, Mapping) and isinstance(item.get("root_cause_id"), str) and item["root_cause_id"] not in ids:
            ids.append(item["root_cause_id"])
    return ids


def recovery_job_inventory(roots: Sequence[str], *, scan_stats: dict[str, int] | None = None) -> dict[str, Any]:
    """R11：在 Job 完成 checkpoint 中冻结逐文件内容；复用时重新读取，不能只信元数据。"""

    files = []
    normalized = sorted(set(str(value) for value in roots))
    if not normalized:
        raise ReconcilerError("恢复段 Job 没有证据根")
    for value in normalized:
        root = Path(value)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ReconcilerError("恢复段 Job 证据根不可信")
        codex_upgrade._reject_symlink_components(root, Path(root.anchor), "恢复段复用证据")
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ReconcilerError("恢复段 Job 证据含非普通条目")
            if path.is_file():
                before = path.stat()
                digest = _file_sha256(path)
                if scan_stats is not None:
                    scan_stats["scanned_bytes"] = scan_stats.get("scanned_bytes", 0) + before.st_size
                after = path.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
                ):
                    raise ReconcilerError("恢复段证据在复算期间变化")
                files.append({"path": str(path), "bytes": after.st_size, "sha256": digest})
    if not files or len({item["path"] for item in files}) != len(files):
        raise ReconcilerError("恢复段 Job 证据为空或重复")
    return {"roots": normalized, "files": files, "content_sha256": _fingerprint(files)}


def recovery_segment_summary(
    campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str,
) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
    """失败段只读对账：严格校验控制摘要；证据边界是否可复用逐 Job 判定，不把坏证据当完成。"""

    attempt_root = codex_upgrade._capture_attempt_path(campaign_dir, "candidate", candidate_id, attempt_id)
    segment = codex_upgrade._attempt_recovery_segment_root(attempt_root, recovery_revision)
    reservation = codex_upgrade._load_attempt_recovery_reservation(
        campaign_dir, segment, candidate_id=candidate_id, attempt_id=attempt_id, recovery_revision=recovery_revision,
    )
    path = segment / codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
    if not path.exists() and not path.is_symlink():
        return segment, reservation, None
    codex_upgrade._reject_symlink_components(path, campaign_dir, "恢复段摘要")
    payload = _read_json(path, "恢复段摘要")
    unsigned = {key: value for key, value in payload.items() if key != "attempt_recovery_digest"}
    if (payload.get("schema_version") != codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_SCHEMA
            or payload.get("attempt_id") != attempt_id or payload.get("candidate_id") != candidate_id
            or payload.get("recovery_revision") != recovery_revision
            or payload.get("run_nonce") != reservation.get("run_nonce")
            or payload.get("attempt_recovery_digest") != _fingerprint(unsigned)
            or payload.get("reservation", {}).get("sha256") != _file_sha256(segment / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME)):
        raise ReconcilerError("恢复段摘要身份、预约绑定或自摘要不一致")
    return segment, reservation, payload


def _after_probe_bound_by_restoration(segment: Path, after: Path, restoration: Path, candidate_id: str) -> bool:
    """判据④（摘要缺失路径）：权限收口后、段摘要写出前被杀时，after 探针与恢复报告没有摘要里的绑定。

    改以机器 finalizer 重放过的恢复报告为准：重放按原输入重算五类 after 快照并逐字段比对，报告里 role=after 的
    引用就是快照内容的可信摘要。after 探针清单必须与这些引用逐项相同，清单列出的快照复算摘要与字节数一致；
    同大小改写并恢复 mtime 的篡改会让重放或复算不一致，不再只核对文件存在。
    """

    evidence_root = segment / "evidence"
    report = codex_upgrade._validate_restoration_report(
        restoration, [evidence_root], phase="candidate", candidate_id=candidate_id,
    )
    referenced: dict[str, str] = {}
    for check in report["checks"]:
        refs = [ref for ref in check.get("evidence_refs", []) if isinstance(ref, Mapping) and ref.get("role") == "after"]
        if len(refs) != 1 or str(refs[0].get("path")) in referenced:
            return False
        referenced[str(refs[0].get("path"))] = str(refs[0].get("sha256"))
    probe = _read_json(after, "恢复段 after 探针")
    snapshots = probe.get("snapshots")
    state_files = codex_upgrade.ENVIRONMENT_STATE_FILES
    if (
        probe.get("schema_version") != codex_upgrade.codex_upgrade_environment_probe.PROBE_MANIFEST_SCHEMA
        or probe.get("phase") != "after"
        or not isinstance(snapshots, list)
        or len(snapshots) != len(state_files)
    ):
        return False
    listed: dict[str, str] = {}
    for item in snapshots:
        if not isinstance(item, Mapping) or state_files.get(str(item.get("kind"))) != item.get("path"):
            return False
        path = after.parent / str(item["path"])
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item.get("bytes")
            or _file_sha256(path) != item.get("sha256")
        ):
            return False
        listed[path.relative_to(evidence_root).as_posix()] = str(item["sha256"])
    return len(listed) == len(state_files) and listed == referenced


def segment_reuse_proofs(
    campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str,
    frozen_job_ids: Sequence[str],
    *, scan_stats: dict[str, int] | None = None,
    _inventory_memo: dict[tuple[str, ...], dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """R11 四项复用判据的唯一实现；失败／证明不足返回 execute，控制链损坏仍拒绝。

    同一次复验内按证据根集合缓存逐文件清单：沿前序段递归时，同一份复用证据只读取、只计入扫描字节一次。
    """

    memo = {} if _inventory_memo is None else _inventory_memo
    segment, reservation, summary = recovery_segment_summary(campaign_dir, candidate_id, attempt_id, recovery_revision)
    _, _, recovery = codex_upgrade._authoritative_recovery_execute_jobs(campaign_dir, candidate_id, reservation)
    if sorted(recovery["execute_jobs"]) != sorted(frozen_job_ids):
        raise ReconcilerError("恢复段复用范围与基线冻结的 J* 不一致")
    planned = {row["id"]: row for row in reservation["planned_jobs"]}
    if sorted(planned) != sorted(frozen_job_ids):
        raise ReconcilerError("恢复段预约 planned_jobs 与基线冻结的 J* 不一致")
    original = codex_upgrade._load_capture_reservation(
        campaign_dir, segment.parent.parent, phase="candidate", candidate_id=candidate_id,
    )
    expected = {row["id"]: row["execution_sha256"] for row in original["planned_jobs"]}
    _, checkpoints, _ = _checkpoint_facts(segment)
    # SIGKILL 后没有摘要时，可从已发布的权限收口与实际 after／restoration 文件取得边界。
    permission_path = segment / codex_upgrade.codex_upgrade_evidence_permissions.RECEIPT_FILENAME
    after = segment / "evidence/environment/after/probe-manifest.json"
    restoration = segment / "evidence/receipts/restoration-report.json"
    if (not after.is_file() or not restoration.is_file() or not permission_path.is_file()
            or (summary is not None and (summary.get("restoration_error") is not None
                                        or summary.get("status") == "environment_contaminated"))):
        return {}
    try:
        if _read_json(restoration, "恢复段恢复报告").get("status") != "restored":
            return {}
        permission = _read_json(permission_path, "恢复段权限收口")
        binding = codex_upgrade.codex_upgrade_evidence_permissions.receipt_binding(segment, permission_path)
        if summary is not None and summary.get("evidence_permission_closeout") != binding:
            return {}
        roots = [Path(value) for value in permission["evidence_roots"]]
        codex_upgrade._replay_evidence_permission_closeout(segment, roots, binding)
        if summary is not None:
            for key, path in (("after_probe", after), ("restoration_report", restoration)):
                if summary.get("environment", {}).get(key) != codex_upgrade._attempt_evidence_binding(segment / "evidence", path):
                    return {}
        elif not _after_probe_bound_by_restoration(segment, after, restoration, candidate_id):
            return {}
    except (OSError, KeyError, ValueError, codex_upgrade.ConfigurationError,
            codex_upgrade.codex_upgrade_evidence_permissions.EvidencePermissionError):
        return {}
    previous_proofs = reservation.get("reuse_proofs", {})
    inherited = set(reservation.get("reuse_job_ids", []))
    if inherited:
        previous_revision = f"ar{int(recovery_revision[2:]) - 1}"
        prior = segment_reuse_proofs(campaign_dir, candidate_id, attempt_id, previous_revision, frozen_job_ids,
                                     scan_stats=scan_stats, _inventory_memo=memo)
    else:
        prior = {}
    proofs = {}
    results = {row["id"]: row for row in summary.get("results", [])} if summary else {}
    for job_id in sorted(frozen_job_ids):
        checkpoint = checkpoints.get(job_id)
        path = segment / f"job-{job_id}.json"
        try:
            if not checkpoint or checkpoint.get("status") != "complete" or not path.is_file() or path.is_symlink():
                continue
            result = _read_json(path, "恢复段 Job")
            source_execution = result.get("source_execution_sha256", planned[job_id].get("source_execution_sha256", result.get("execution_sha256")))
            if (result.get("status") != "complete" or result.get("id") != job_id
                    or checkpoint.get("run_nonce") != reservation["run_nonce"]
                    or checkpoint.get("attempt_id") != f"{attempt_id}.{recovery_revision}"
                    or checkpoint.get("phase") != "candidate"
                    or result.get("execution_sha256") != planned[job_id]["execution_sha256"]
                    or source_execution != expected.get(job_id)
                    or checkpoint.get("result_sha256") != incremental_recovery.digest(result)
                    or checkpoint.get("result") != result or (summary and results.get(job_id) != result)
                    or not _job_egress_trusted(result)):
                continue
            if job_id in inherited and (previous_proofs.get(job_id) != prior.get(job_id) or job_id not in prior):
                continue
            if any(Path(root) not in roots for root in result.get("evidence_roots", [])):
                continue
            memo_key = tuple(sorted(set(str(value) for value in result.get("evidence_roots", []))))
            inventory = memo.get(memo_key)
            if inventory is None:
                inventory = recovery_job_inventory(result.get("evidence_roots", []), scan_stats=scan_stats)
                memo[memo_key] = inventory
            if checkpoint.get("recovery_evidence") != inventory:
                continue
            checkpoint_path = segment / "checkpoints" / f"{checkpoint['checkpoint_sequence']:08d}.json"
            def bound(file: Path) -> dict[str, Any]:
                return {"path": file.relative_to(campaign_dir).as_posix(), "sha256": _file_sha256(file), "bytes": file.stat().st_size}
            proofs[job_id] = {
                "recovered_from": recovery_revision, "source_execution_sha256": source_execution,
                "result": bound(path), "checkpoint": bound(checkpoint_path),
                "permission_closeout": bound(permission_path), "after_probe": bound(after),
                "restoration_report": bound(restoration), "content_sha256": inventory["content_sha256"],
            }
        except (OSError, ValueError, KeyError, ReconcilerError, codex_upgrade.ConfigurationError):
            continue
    return proofs


def validate_segment_reuse_preview(
    campaign_dir: Path, preview: Mapping[str, Any], *, scan_stats: dict[str, int] | None = None,
) -> int:
    """批准和派发读侧重放四项判据；证明必须与预览逐字段相同。旧空复用预览保持兼容。

    返回本次复验实际读取的证据字节数（同一证据根在递归各层只读一次），并累加进 ``scan_stats``；
    调用方据此把复验成本记入自己的扫描统计。
    """

    reuse = preview.get("reuse_job_ids", [])
    supplied = preview.get("reuse_proofs", {})
    if not isinstance(supplied, Mapping) or set(supplied) != set(reuse):
        raise ReconcilerError("恢复预览复用集合与判据证明不一致")
    if not reuse:
        return 0
    stats = {"scanned_bytes": 0}
    actual = segment_reuse_proofs(campaign_dir, str(preview["candidate_id"]), str(preview["source_attempt_id"]),
                                 str(preview["recovery_revision"]), preview["planned_job_ids"], scan_stats=stats)
    if scan_stats is not None:
        scan_stats["scanned_bytes"] = scan_stats.get("scanned_bytes", 0) + stats["scanned_bytes"]
    if any(job_id not in actual or actual[job_id] != supplied[job_id] for job_id in reuse):
        raise ReconcilerError("恢复预览的 Job 复用判据漂移或证明不足")
    return stats["scanned_bytes"]


def reused_segment_job_result(campaign_dir: Path, proof: Mapping[str, Any]) -> dict[str, Any]:
    """显式引用来源，不伪装成本段新执行；读写两端使用完全相同的投影。"""

    binding = proof["result"]
    path = codex_upgrade._campaign_file(campaign_dir, binding["path"])
    if _file_sha256(path) != binding["sha256"] or path.stat().st_size != binding["bytes"]:
        raise ReconcilerError("恢复段复用来源收据漂移")
    source = _read_json(path, "恢复段复用 Job")
    result = {key: value for key, value in source.items() if key not in {"started_at_utc", "completed_at_utc"}}
    result.update(disposition="reused", recovered_from=proof["recovered_from"], source_receipt=dict(binding),
                  recovery_checkpoint=dict(proof["checkpoint"]), source_execution_sha256=proof["source_execution_sha256"])
    return result


def _recovery_preview(
    campaign_dir: Path,
    receipt_dir: Path,
    *,
    manifest: Mapping[str, Any],
    attempt_id: str,
    phase: str,
    candidate_id: str | None,
    attempt_exists: bool,
    jobs: Mapping[str, Any],
    environment_status: str,
    provenance_copy: Mapping[str, Any],
    current: Mapping[str, Any],
    reconciliation_receipt_sha256: str,
    campaign_ledger_head: Mapping[str, Any],
    project_ledger_scope: Mapping[str, Any],
    now: str,
    recovery_revision: str | None = None,
    recovery_execute_jobs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """零请求恢复预览：冻结四类 Job 闭集与预计新增请求数，等待操作员批准（段模式携带 recovery_revision）。

    段模式：execute 与 reuse 不相交且并集恰为权威链 J*；reuse 必须逐项通过四项判据，估算只覆盖 execute。

    项目总账只绑定本 Campaign 会改变恢复前提的事件（``project_ledger.campaign_event_scope``），不绑定
    整本总账 head：其它 Campaign 的事件与本 Campaign 的延期／暂停不再使预览作废，全局条件在消费时现场复核。
    """

    groups = jobs["groups"]
    scan_stats = {"scanned_bytes": 0}
    # 第三批 B3-12：只有非段模式且有可复用作业时才做预览期连续性检查；其余情况两者保持空值、预览字节不变。
    continuity: dict[str, Any] | None = None
    continuity_invalidated: list[str] = []
    if recovery_revision is not None:
        if recovery_execute_jobs is None:
            raise ReconcilerError("恢复段预览必须提供权威链取得的 execute_jobs")
        frozen = sorted(str(item) for item in recovery_execute_jobs)
        if sorted(str(item) for item in jobs["planned_job_ids"]) != frozen or len(set(frozen)) != len(frozen):
            raise ReconcilerError(
                f"恢复段预约的 planned_jobs {sorted(jobs['planned_job_ids'])} 与基线冻结的 J* {frozen} 不一致"
            )
        proofs = segment_reuse_proofs(campaign_dir, str(candidate_id), attempt_id, recovery_revision, frozen, scan_stats=scan_stats)
        reusable = sorted(set(groups["complete"]) & set(proofs))
        execute = sorted(set(frozen) - set(reusable))
    else:
        reusable = list(groups["complete"]) if (attempt_exists and environment_status == "restored") else []
        # 工具演进：源 attempt 生产序号之后登记的演进使部分已完成作业失效，与 resume 冻结闭集同一口径。
        # 没有演进时不读预约（_attempt_evolution_impact 在空链时直接返回空影响）。
        source_root = (
            campaign_dir / codex_upgrade._capture_attempt_relative(phase, candidate_id) / "attempts" / attempt_id
        )
        try:
            evolution_impact = codex_upgrade._attempt_evolution_impact(
                campaign_dir, manifest, source_root, phase=phase, candidate_id=candidate_id
            )
        except codex_upgrade.ConfigurationError as error:
            raise ReconcilerError(str(error)) from error
        evolution_invalidated = sorted(set(reusable) & set(evolution_impact["affected_job_ids"]))
        reusable = sorted(set(reusable) - set(evolution_invalidated))
        execute = sorted(set(jobs["planned_job_ids"]) - set(reusable))
        # 第三批 B3-12（第 22 项）：有可复用作业时先采一次只读探针核对环境连续性；漂移（或探针采不到，失败关闭）
        # 即写下漂移收据（resume 与 R17 复算从同一收据读同一事实），已完成作业全部移入执行集合，预览直接给出复用 0。
        if reusable:
            receipt_path = codex_upgrade._recovery_continuity_receipt_path(campaign_dir, attempt_id)
            try:
                recorded = codex_upgrade._attempt_continuity_drift_receipt(campaign_dir, attempt_id)
            except codex_upgrade.ConfigurationError as error:
                raise ReconcilerError(str(error)) from error
            if recorded is not None:
                probed = dict(recorded)
            else:
                probed = codex_upgrade.recovery_preview_continuity(
                    campaign_dir,
                    manifest=manifest,
                    phase=phase,
                    candidate_id=candidate_id,
                    source_attempt_id=attempt_id,
                    output_dir=receipt_dir / "continuity-probes" / now.replace(":", "").replace("-", ""),
                )
                if probed["status"] in codex_upgrade.RECOVERY_CONTINUITY_DRIFT_STATUSES:
                    _write_once(
                        receipt_path,
                        {
                            "schema_version": codex_upgrade.RECOVERY_CONTINUITY_DRIFT_RECEIPT_SCHEMA,
                            "source_attempt_id": attempt_id,
                            "phase": phase,
                            "candidate_id": candidate_id,
                            **{key: value for key, value in probed.items()},
                            "created_at_utc": now,
                        },
                    )
            # 预览里只放确定性的结论字段（探针目录、时间戳等留在收据与探针目录里），重跑预览字节不变。
            continuity = {
                key: probed[key]
                for key in ("status", "drifted_kinds", "compared_kinds", "reason", "source_after_probe_sha256")
                if key in probed
            }
            if probed["status"] in codex_upgrade.RECOVERY_CONTINUITY_DRIFT_STATUSES:
                continuity_invalidated = list(reusable)
                execute = sorted(set(execute) | set(reusable))
                reusable = []
    per_job: dict[str, int] = {}
    for job in provenance_copy.get("jobs", []):
        if isinstance(job, Mapping) and isinstance(job.get("job_id"), str):
            per_job[job["job_id"]] = int(job.get("precise_count", 0)) + int(job.get("estimated_count", 0))
    known = {job_id: per_job[job_id] for job_id in execute if job_id in per_job and per_job[job_id] > 0}
    preview = {
        "schema_version": RECOVERY_PREVIEW_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "phase": phase,
        "candidate_id": candidate_id,
        "source_attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "source_attempt_receipt_exists": attempt_exists,
        "reconciliation_receipt_sha256": reconciliation_receipt_sha256,
        "campaign_ledger_head": {
            "sequence": campaign_ledger_head.get("head_sequence"),
            "sha256": campaign_ledger_head.get("head_sha256"),
            "status": campaign_ledger_head.get("status"),
        },
        "project_ledger_scope": dict(project_ledger_scope),
        "planned_job_ids": list(jobs["planned_job_ids"]),
        "complete_job_ids": list(groups["complete"]),
        "failed_job_ids": list(groups["failed"]),
        "indeterminate_job_ids": list(groups["indeterminate"]),
        "pending_job_ids": list(groups["pending"]),
        "reuse_job_ids": reusable,
        "execute_job_ids": execute,
        "reuse_basis": (
            "失败段不可变；仅复用 result/checkpoint、权限边界与逐文件摘要、执行身份、after 环境四项均通过的 Job"
            if recovery_revision is not None
            else "source attempt 环境已恢复，complete Job 只读复用"
            if reusable
            else "source attempt 无 after 探针或环境未恢复，证据前提不成立，不复用"
        ),
        "expected_new_requests": {
            "known_total": sum(known.values()),
            "known_by_job": known,
            "unknown_job_ids": sorted(set(execute) - set(known)),
            "basis": "同 Campaign provenance 逐 Job 计数（precise+estimated）",
        },
        "tool_identity": {
            "policy_sha256": current.get("policy_sha256"),
            "wire_producer_sha256": current.get("wire_producer_sha256"),
            "files_sha256": current.get("files_sha256"),
        },
        "reservation_exists": False,
        "live_request_count": 0,
        "scanned_bytes": scan_stats["scanned_bytes"],
    }
    if recovery_revision is not None:
        preview["reuse_proofs"] = {job_id: proofs[job_id] for job_id in reusable}
    elif evolution_impact["evolution_indexes"]:
        # 只在确有演进时写入，没有演进的预览字节不变。
        preview["tool_evolution"] = {
            "source_index": int(evolution_impact["index"]),
            "evolution_indexes": list(evolution_impact["evolution_indexes"]),
            "invalidated_job_ids": evolution_invalidated,
        }
        preview["reuse_basis"] = (
            "source attempt 环境已恢复，complete Job 只读复用；工具演进受影响的已完成 Job 移入执行集合"
        )
    if continuity is not None:
        # 第三批 B3-12：只在做过连续性检查（有可复用作业）时写入，其余预览字节不变。
        preview["environment_continuity"] = continuity
        if continuity_invalidated:
            preview["continuity_drift"] = {"invalidated_job_ids": sorted(continuity_invalidated)}
            preview["reuse_basis"] = (
                "零请求预览采探针发现环境连续性漂移（或探针采不到，失败关闭）：已完成作业全部重跑，不承接"
            )
    index, latest = _latest_indexed(receipt_dir, PREVIEW_RE)
    if latest is not None:
        existing = _read_json(latest, "既有恢复预览")
        comparable = {k: v for k, v in existing.items() if k not in {"created_at_utc", "review_sha256", "index"}}
        if comparable == preview:
            return existing
    preview["index"] = index + 1
    preview["created_at_utc"] = now
    preview["review_sha256"] = _fingerprint({k: v for k, v in preview.items() if k not in {"created_at_utc", "review_sha256"}})
    _write_once(receipt_dir / f"recovery-preview-{preview['index']:02d}.json", preview)
    return preview


def _resume_reuse_check(
    campaign_dir: Path,
    *,
    phase: str,
    recovery_revision: str | None,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    """R17：批准前按 resume 同一复用判定只读复算恢复预览，返回 consistent／inconsistent／not_applicable。

    resume 会拒绝的情形（ConfigurationError）记为 inconsistent，由调用方拒绝批准。覆盖非段模式的
    official 恢复（VC-1）与候选采集续跑（VC-5）：恢复段逐项携带复用证明、由段合同校验；
    孤儿 attempt 没有 attempt.json，resume 按预览执行集合重跑、不复用。
    """

    if recovery_revision is not None:
        return {"status": "not_applicable", "reason": "恢复段预览逐项携带复用证明，由段合同校验"}
    if not preview.get("source_attempt_receipt_exists"):
        return {"status": "not_applicable", "reason": "孤儿 attempt 没有 attempt.json，resume 按预览执行集合重跑、不复用"}
    try:
        return codex_upgrade.recovery_reuse_check(
            campaign_dir,
            phase=phase,
            candidate_id=preview.get("candidate_id") if phase == "candidate" else None,
            source_attempt_id=str(preview["source_attempt_id"]),
            reuse_job_ids=preview["reuse_job_ids"],
            execute_job_ids=preview["execute_job_ids"],
        )
    except codex_upgrade.ConfigurationError as error:
        return {"status": "inconsistent", "reason": str(error)}


def approve_recovery_preview(
    campaign_dir: Path,
    attempt_id: str,
    *,
    approve_sha256: str,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """操作员按 review_sha256 批准恢复预览；批准收据只写一次，幂等返回既有。"""

    if not codex_upgrade.SHA256_RE.fullmatch(str(approve_sha256)):
        raise ReconcilerError("--approve-recovery-sha256 格式非法")
    receipt_dir = _reconciliation_dir(campaign_dir, f"attempt-{_recovery_segment_subject(attempt_id, recovery_revision).replace(':', '-')}")
    matched: tuple[int, Path, dict[str, Any]] | None = None
    if receipt_dir.is_dir():
        for child in sorted(receipt_dir.iterdir()):
            match = PREVIEW_RE.fullmatch(child.name)
            if not match:
                continue
            payload = _read_json(child, "恢复预览")
            if payload.get("review_sha256") == approve_sha256:
                matched = (int(match.group(1)), child, payload)
    if matched is None:
        raise ReconcilerError("批准摘要与任何恢复预览都不一致")
    index, preview_path, preview = matched
    for child in sorted(receipt_dir.iterdir()):
        if APPROVAL_RE.fullmatch(child.name):
            existing = _read_json(child, "恢复批准收据")
            if existing.get("preview_index") == index and existing.get("approved_sha256") == approve_sha256:
                return existing
    approval_index, _ = _latest_indexed(receipt_dir, APPROVAL_RE)
    current = _current_identity()
    if (
        preview.get("tool_identity", {}).get("wire_producer_sha256") != current.get("wire_producer_sha256")
        or preview.get("tool_identity", {}).get("policy_sha256") != current.get("policy_sha256")
    ):
        raise ReconcilerError("恢复预览生成后工具身份已变化，必须重新对账生成新预览")
    if preview.get("recovery_revision") != recovery_revision:
        raise ReconcilerError("恢复预览的恢复段编号与批准请求不一致")
    approval = {
        "schema_version": RECOVERY_APPROVAL_SCHEMA,
        "index": approval_index + 1,
        "campaign_id": preview.get("campaign_id"),
        "source_attempt_id": preview.get("source_attempt_id"),
        "recovery_revision": recovery_revision,
        "preview_index": index,
        "preview_path": preview_path.relative_to(campaign_dir).as_posix(),
        "preview_sha256": _file_sha256(preview_path),
        "approved_sha256": approve_sha256,
        "execute_job_ids": list(preview.get("execute_job_ids", [])),
        "reuse_job_ids": list(preview.get("reuse_job_ids", [])),
        "approved_at_utc": _utc_now(),
    }
    approval["receipt_sha256"] = _fingerprint({k: v for k, v in approval.items() if k != "approved_at_utc"})
    _write_once(receipt_dir / f"recovery-approval-{approval['index']:02d}.json", approval)
    return approval


def load_approved_recovery_preview(
    campaign_dir: Path,
    preview_path: Path | None,
    *,
    phase: str,
    candidate_id: str | None,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """resume 衔接：只接受当前 Campaign 内、已批准、工具身份仍相同的恢复预览（段模式按 ar<k> 定位）。"""

    if not isinstance(preview_path, Path):
        raise ReconcilerError("resume --rerun-failed 必须提供 --recovery-preview（已批准的 recovery-preview/v1）")
    if preview_path.is_symlink() or not preview_path.is_file():
        raise ReconcilerError(f"恢复预览不存在或不可信：{preview_path}")
    resolved = preview_path.resolve(strict=True)
    control_root = (campaign_dir / "control" / RECONCILIATION_DIR).resolve(strict=False)
    if control_root not in resolved.parents or not PREVIEW_RE.fullmatch(resolved.name):
        raise ReconcilerError("恢复预览必须位于本 Campaign 的 control/reconciliation/attempt-<id>/ 下")
    preview = _read_json(resolved, "恢复预览")
    unsigned = {k: v for k, v in preview.items() if k not in {"created_at_utc", "review_sha256"}}
    if (
        preview.get("schema_version") != RECOVERY_PREVIEW_SCHEMA
        or _fingerprint(unsigned) != preview.get("review_sha256")
        or preview.get("phase") != phase
        or preview.get("candidate_id") != candidate_id
        or preview.get("reservation_exists") is not False
        or preview.get("live_request_count") != 0
    ):
        raise ReconcilerError("恢复预览自摘要、阶段或零请求边界不满足")
    attempt_id = str(preview.get("source_attempt_id", ""))
    if preview.get("recovery_revision") != recovery_revision:
        raise ReconcilerError("恢复预览的恢复段编号与 resume 请求不一致")
    if resolved.parent.name != f"attempt-{_recovery_segment_subject(attempt_id, recovery_revision).replace(':', '-')}":
        raise ReconcilerError("恢复预览目录与其来源 attempt／恢复段不一致")
    approved: dict[str, Any] | None = None
    approval_path: Path | None = None
    for child in sorted(resolved.parent.iterdir()):
        if APPROVAL_RE.fullmatch(child.name):
            payload = _read_json(child, "恢复批准收据")
            approval_unsigned = {
                key: value
                for key, value in payload.items()
                if key not in {"approved_at_utc", "receipt_sha256"}
            }
            if (
                payload.get("schema_version") == RECOVERY_APPROVAL_SCHEMA
                and payload.get("preview_sha256") == _file_sha256(resolved)
                and payload.get("approved_sha256") == preview.get("review_sha256")
                and payload.get("receipt_sha256")
                == _fingerprint(approval_unsigned)
            ):
                approved = payload
                approval_path = child
    if approved is None or approval_path is None:
        raise ReconcilerError("恢复预览尚未批准；先执行 reconcile-attempt --approve-recovery-sha256 <review_sha256>")
    if recovery_revision is not None:
        validate_segment_reuse_preview(campaign_dir, preview)
    current = _current_identity()
    identity = preview.get("tool_identity") or {}
    if (
        identity.get("wire_producer_sha256") != current.get("wire_producer_sha256")
        or identity.get("policy_sha256") != current.get("policy_sha256")
    ):
        raise ReconcilerError("恢复预览批准后工具身份已变化，必须重新对账")
    receipt_path = resolved.parent / ATTEMPT_RECEIPT_NAME
    if _file_sha256(receipt_path) != preview.get("reconciliation_receipt_sha256"):
        raise ReconcilerError("恢复预览绑定的对账收据已漂移")

    # 恢复预览冻结 Campaign 账本 head 与本 Campaign 的项目总账事件摘要：本 Campaign 在总账出现新事件
    # （对账、终态、账务解决或更正）必须重新对账；其它 Campaign 的事件与本 Campaign 的延期／暂停只经
    # 现场复核生效。Campaign head 允许且只允许多出本预览对应的 recovery_authorized 事件，以保证重复
    # resume 幂等。旧预览（只有 project_ledger_head）保持整本总账 head 严格相等。
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    ledger_dir = _campaign_ledger_dir(manifest)
    campaign_head = preview.get("campaign_ledger_head")
    project_scope = preview.get("project_ledger_scope")
    project_head = preview.get("project_ledger_head")
    authorization_event: dict[str, Any] | None = None
    if isinstance(project_scope, Mapping):
        problems = _recovery_preview_ledger_problems(
            campaign_dir, manifest, project_scope, receipt_path, now=_utc_now()
        )
        if problems:
            raise ReconcilerError("恢复预览不能消费：" + "；".join(problems))
    elif isinstance(project_head, Mapping):
        project_root = _project_root(campaign_dir)
        _plan, current_project_head = _project_facts(project_root)
        if (
            current_project_head.get("sequence") != project_head.get("sequence")
            or current_project_head.get("head_sha256") != project_head.get("sha256")
        ):
            raise ReconcilerError("恢复预览生成后项目总账 head 已推进，必须重新对账")
    if isinstance(campaign_head, Mapping):
        sequence = campaign_head.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ReconcilerError("恢复预览绑定的 Campaign 账本 head 非法")
        try:
            frozen_campaign_head = timing_ledger.inspect_ledger(
                ledger_dir,
                limit=sequence,
            )
        except timing_ledger.TimingLedgerError as error:
            raise ReconcilerError(f"恢复预览绑定的 Campaign head 无法重放：{error}") from error
        if (
            frozen_campaign_head.get("head_sha256") != campaign_head.get("sha256")
            or frozen_campaign_head.get("status") != campaign_head.get("status")
        ):
            raise ReconcilerError("恢复预览绑定的 Campaign 账本 head 已漂移")
        authorizable = {"recovery_required", "stage_review_required", "candidate_review_required"}
        if campaign_head.get("status") in authorizable:
            event_id = (
                f"recovery-authorized-{attempt_id}-{int(preview['index']):02d}"
                if recovery_revision is None
                else f"recovery-authorized-{attempt_id}-{recovery_revision}-{int(preview['index']):02d}"
            )
            with codex_upgrade._campaign_lock(campaign_dir):
                current_campaign_head = _ledger_facts(ledger_dir, now=_utc_now())
                existing_sha256 = _ledger_event_sha256(ledger_dir, event_id)
                if current_campaign_head.get("status") in authorizable:
                    if (
                        current_campaign_head.get("head_sequence") != sequence
                        or current_campaign_head.get("head_sha256")
                        != campaign_head.get("sha256")
                    ) and not _campaign_head_advanced_only_by_deadline_control(ledger_dir, sequence):
                        raise ReconcilerError(
                            "恢复批准消费前 Campaign 账本 head 已推进（出现延期／预算暂停以外的事件），必须重新对账"
                        )
                    if current_campaign_head.get("status") == "candidate_review_required":
                        # 候选审核（VC-5 采集失败）续跑：阶段与根因取审核事件；审核必须唯一绑定
                        # 本 attempt 所在的失败父 run（同对账时的核对）。
                        review = _candidate_capture_review(
                            campaign_dir,
                            manifest,
                            ledger_dir,
                            current_campaign_head,
                            phase=phase,
                            candidate_id=candidate_id,
                            attempt_id=attempt_id,
                            strict=True,
                        )
                        if review is None or recovery_revision is not None:
                            raise ReconcilerError("候选审核只允许 VC-5 采集失败 attempt 的续跑授权")
                        authorized_phase = review["phase"]
                        authorized_cause = review["root_cause_id"]
                    else:
                        authorized_phase = str(
                            current_campaign_head.get("active_phase") or current_campaign_head["review_phase"]
                        )
                        authorized_cause = str(
                            current_campaign_head.get("recovery_root_cause_id")
                            or current_campaign_head["review_root_cause_id"]
                        )
                    receipts = _ledger_recovery_authorization_bindings(
                        ledger_dir,
                        attempt_id,
                        resolved,
                        approval_path,
                        recovery_revision=recovery_revision,
                    )
                    authorization_event = _append_ledger_event(
                        ledger_dir,
                        event_id=event_id,
                        phase=authorized_phase,
                        event_type="recovery_authorized",
                        root_cause_id=authorized_cause,
                        receipts=receipts,
                        next_action="resume-rerun-failed",
                    )
                elif (
                    current_campaign_head.get("status") == "active"
                    and existing_sha256 is not None
                ):
                    authorization_event = {
                        "event_id": event_id,
                        "event_sha256": existing_sha256,
                        "appended": False,
                    }
                else:
                    raise ReconcilerError(
                        "Campaign 不在 recovery_required，禁止消费恢复批准"
                    )
    return {
        **preview,
        "approval": approved,
        "preview_path": str(resolved),
        "timing_recovery_event": authorization_event,
    }


def _recovery_segment_subject(attempt_id: str, recovery_revision: str | None) -> str:
    return attempt_id if recovery_revision is None else f"{attempt_id}:{recovery_revision}"


# 修好接着跑第 41 项：预览冻结 Campaign 账本 head 之后，批准延期或预算暂停只追加截止控制事件，不改变本 attempt 的
# 失败事实与恢复范围（与 _recovery_preview_ledger_problems 的"延期不会使本预览作废"同口径）；其它任何事件——对账、
# 根因、阶段、attempt、放弃（campaign_abandoned 属截止控制但是终态，不容许）——仍要求重新对账。
PREVIEW_TOLERATED_CAMPAIGN_EVENT_TYPES = frozenset({"deadline_paused", "deadline_extended"})


def _campaign_head_advanced_only_by_deadline_control(ledger_dir: Path, frozen_sequence: int) -> bool:
    """冻结点（前缀已由调用方按 head_sha256 核对）之后是否只追加了延期／预算暂停事件。"""

    try:
        events = timing_ledger._load_events(ledger_dir)
    except timing_ledger.TimingLedgerError as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    tail = events[frozen_sequence:]
    return bool(tail) and all(
        event.get("event_type") in PREVIEW_TOLERATED_CAMPAIGN_EVENT_TYPES for event, _raw in tail
    )


def authorize_recovery_preview(
    campaign_dir: Path,
    attempt_id: str,
    preview_path: Path,
    *,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """改造 5 M2：消费已批准的恢复预览（账本 recovery_required → recovery_authorized → active），
    使后继恢复段的批次可以编译派发；幂等（已 active 且授权事件存在时不重复写）。"""

    campaign_dir = Path(campaign_dir)
    phase, candidate_id, _attempt_root = _locate_attempt(campaign_dir, attempt_id)
    if recovery_revision is not None and (phase != "candidate" or candidate_id is None):
        raise ReconcilerError("恢复段只存在于候选 attempt")
    consumed = load_approved_recovery_preview(
        campaign_dir, Path(preview_path), phase=phase, candidate_id=candidate_id, recovery_revision=recovery_revision
    )
    if str(consumed.get("source_attempt_id")) != attempt_id:
        raise ReconcilerError("恢复预览绑定的 attempt 与 --attempt-id 不一致")
    return {
        "schema_version": ATTEMPT_SCHEMA,
        "status": "authorized",
        "campaign_id": str(consumed.get("campaign_id", "")),
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "preview_path": consumed["preview_path"],
        "review_sha256": consumed.get("review_sha256"),
        "timing_recovery_event": consumed.get("timing_recovery_event"),
        "live_request_count": 0,
        "next_command": (
            f"capture-candidate run --attempt-recovery ar{int(recovery_revision[2:]) + 1} --rerun-failed --recovery-preview {consumed['preview_path']}"
            if recovery_revision is not None
            else f"resume --rerun-failed --recovery-preview {consumed['preview_path']}"
        ),
    }


def _evolution_invalidation_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    attempt: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    """等待封存的 attempt 若有已完成作业被其生产序号之后登记的工具演进作废，返回失效事实；否则 None。"""

    try:
        invalidated = codex_upgrade._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, attempt_root, attempt, phase=phase, candidate_id=candidate_id
        )
        if not invalidated:
            return None
        impact = codex_upgrade._attempt_evolution_impact(
            campaign_dir, manifest, attempt_root, phase=phase, candidate_id=candidate_id
        )
        chain = codex_upgrade._campaign_tool_evolutions(campaign_dir, manifest)
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(str(error)) from error
    latest = max(int(index) for index in impact["evolution_indexes"])
    return {
        "source_index": int(impact["index"]),
        "evolution_indexes": [int(index) for index in impact["evolution_indexes"]],
        "latest_evolution_index": latest,
        "latest_evolution_receipt_sha256": str(chain[latest - 1]["receipt_sha256"]),
        "invalidated_job_ids": list(invalidated),
    }


def _evolution_invalidation_cause(phase: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """演进失效的暂停原因：不是失败根因，只作计时账本 recovery_required／授权的原因标识，不入总账计数。"""

    return {
        "root_cause_id": f"tool-evolution-{int(facts['latest_evolution_index']):02d}",
        "stable_error_code": "attempt.tool-evolution-invalidated",
        "failed_step": "tool-evolution",
        "stable_dimensions": {"phase": phase},
        "component": COMPONENT,
    }


def _isolation_invalidation_facts(
    campaign_dir: Path,
    attempt: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> dict[str, Any] | None:
    """等待封存的 attempt 被环境隔离作废（Kilo 后环境恢复失败等，修好接着跑第 13 项）时返回失效事实。

    与工具演进作废同一协议：不是失败、不计根因；全部已完成作业失效（污染后结果永不复用），计时账本
    recovery_required 后按恢复预览全部重跑。
    """

    try:
        chain = codex_upgrade._environment_isolations(campaign_dir)
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(str(error)) from error
    for receipt in chain:
        for item in receipt["invalidated_attempts"]:
            if (
                isinstance(item, Mapping)
                and item.get("phase") == phase
                and item.get("candidate_id") == candidate_id
                and item.get("attempt_id") == attempt_id
                and item.get("recovery_revision") is None
            ):
                jobs = sorted(
                    str(result["id"])
                    for result in attempt.get("results", [])
                    if isinstance(result, Mapping) and result.get("status") == "complete" and isinstance(result.get("id"), str)
                )
                return {
                    "isolation_index": int(receipt["index"]),
                    "isolation_sha256": str(receipt["isolation_sha256"]),
                    "invalidated_job_ids": jobs,
                }
    return None


def _conflict_invalidation_facts(
    campaign_dir: Path,
    attempt: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> dict[str, Any] | None:
    """等待封存的 attempt 被证据根冲突隔离（修好接着跑第 24 项）时返回失效事实。

    与工具演进／环境隔离作废同一协议：不是失败、不计根因；全部已完成作业失效（证据不可信，永不复用），计时账本
    recovery_required 后按恢复预览全部重跑。
    """

    try:
        found = codex_upgrade._attempt_conflict_quarantine_receipt(campaign_dir, phase, candidate_id, attempt_id)
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(str(error)) from error
    if found is None:
        return None
    receipt, _item = found
    jobs = sorted(
        str(result["id"])
        for result in attempt.get("results", [])
        if isinstance(result, Mapping) and result.get("status") == "complete" and isinstance(result.get("id"), str)
    )
    return {
        "quarantine_index": int(receipt["index"]),
        "quarantine_sha256": str(receipt["quarantine_sha256"]),
        "invalidated_job_ids": jobs,
    }


INVALIDATION_FIELDS = (
    "tool_evolution_invalidation",
    "environment_isolation_invalidation",
    "evidence_conflict_invalidation",
)


def _recorded_invalidation(campaign_dir: Path, attempt_id: str) -> tuple[str, dict[str, Any]] | None:
    """既有 attempt 对账收据记录的作废来源（字段名与事实）；没有收据或收据不是作废对账时为 None。"""

    path = _reconciliation_dir(campaign_dir, f"attempt-{attempt_id}") / ATTEMPT_RECEIPT_NAME
    if path.is_symlink() or not path.is_file():
        return None
    payload = _read_json(path, "既有对账收据")
    if (
        payload.get("schema_version") != ATTEMPT_SCHEMA
        or payload.get("attempt_id") != attempt_id
        or payload.get("recovery_revision") is not None
    ):
        raise ReconcilerError(f"既有对账收据身份非法：{path}")
    present = [name for name in INVALIDATION_FIELDS if isinstance(payload.get(name), Mapping)]
    if not present:
        return None
    if len(present) > 1:
        raise ReconcilerError(f"既有对账收据的作废来源不唯一：{path}")
    return present[0], dict(payload[present[0]])


def _require_recorded_invalidation_current(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    field: str,
    recorded: Mapping[str, Any],
    current: Mapping[str, Any] | None,
) -> None:
    """记录的作废事实必须仍由当前链支持：隔离／冲突逐字段相等；演进须是当前事实的前缀（同一 source_index、
    序号前缀、该序号的演进收据摘要相同、作废作业子集）。否则失败关闭，不按当前事实改写 write-once 收据。"""

    consistent = False
    if current is not None:
        if field == "tool_evolution_invalidation":
            try:
                chain = codex_upgrade._campaign_tool_evolutions(campaign_dir, manifest)
            except codex_upgrade.ConfigurationError as error:
                raise ReconcilerError(str(error)) from error
            recorded_indexes = [int(item) for item in recorded.get("evolution_indexes") or []]
            current_indexes = [int(item) for item in current.get("evolution_indexes") or []]
            latest = recorded.get("latest_evolution_index")
            recorded_jobs = recorded.get("invalidated_job_ids")
            consistent = (
                bool(recorded_indexes)
                and isinstance(latest, int)
                and latest == max(recorded_indexes)
                and recorded.get("source_index") == current.get("source_index")
                and current_indexes[: len(recorded_indexes)] == recorded_indexes
                and 1 <= latest <= len(chain)
                and str(chain[latest - 1].get("receipt_sha256")) == recorded.get("latest_evolution_receipt_sha256")
                and isinstance(recorded_jobs, list)
                and bool(recorded_jobs)
                and set(recorded_jobs) <= set(current.get("invalidated_job_ids") or [])
            )
        else:
            consistent = dict(current) == dict(recorded)
    if not consistent:
        raise ReconcilerError(
            f"既有对账收据记录的作废事实（{field}）与当前链不一致，拒绝按当前事实改写 write-once 收据"
        )


def _attempt_invalidation_facts(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    attempt: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """等待封存 attempt 的三类作废事实（演进、隔离、证据根冲突）与叠加的其它来源。

    修好接着跑第 28 项：作废来源以首次落盘的对账收据为准。没有既有收据时按演进 > 隔离 > 冲突取其一（历史字节不变）；
    既有收据已记录某类作废时，核对该类事实仍由当前链支持后沿用记录（收据、总账、账本事件与暂停原因幂等），其余
    类别的当前事实作为叠加只返回给命令输出——它们对执行范围的影响由恢复预览按当前链重算承担。
    """

    current: dict[str, dict[str, Any] | None] = {
        "tool_evolution_invalidation": _evolution_invalidation_facts(
            campaign_dir, manifest, attempt_root, attempt, phase=phase, candidate_id=candidate_id
        ),
        "environment_isolation_invalidation": _isolation_invalidation_facts(
            campaign_dir, attempt, phase=phase, candidate_id=candidate_id, attempt_id=attempt_id
        ),
        "evidence_conflict_invalidation": _conflict_invalidation_facts(
            campaign_dir, attempt, phase=phase, candidate_id=candidate_id, attempt_id=attempt_id
        ),
    }
    recorded = _recorded_invalidation(campaign_dir, attempt_id)
    stacked: dict[str, dict[str, Any]] = {}
    if recorded is None:
        chosen_field = next((name for name in INVALIDATION_FIELDS if current[name] is not None), None)
    else:
        chosen_field, facts = recorded
        _require_recorded_invalidation_current(
            campaign_dir, manifest, field=chosen_field, recorded=facts, current=current[chosen_field]
        )
        latest_same_kind = current[chosen_field]
        if latest_same_kind is not None and dict(latest_same_kind) != dict(facts):
            # 同类叠加（演进作废后又登记了更多演进）：当前链的事实只留痕，收据沿用首次记录。
            stacked[chosen_field] = dict(latest_same_kind)
        current[chosen_field] = dict(facts)
    stacked.update({name: dict(value) for name, value in current.items() if name != chosen_field and value is not None})
    chosen = {name: (dict(current[name]) if name == chosen_field and current[name] is not None else None) for name in INVALIDATION_FIELDS}
    return (
        chosen["tool_evolution_invalidation"],
        chosen["environment_isolation_invalidation"],
        chosen["evidence_conflict_invalidation"],
        stacked,
    )


def _invalidation_ledger_event_id(
    attempt_id: str,
    isolation_invalidation: Mapping[str, Any] | None,
    conflict_invalidation: Mapping[str, Any] | None = None,
) -> str:
    """作废对账写计时账本 recovery_required 的事件 ID（演进、隔离、证据根冲突分开，互不吞并）。"""

    kind = (
        "isolation"
        if isolation_invalidation is not None
        else "conflict"
        if conflict_invalidation is not None
        else "evolution"
    )
    return f"reconcile-attempt-{kind}-{attempt_id}"


def _conflict_invalidation_cause(phase: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """证据根冲突隔离作废的暂停原因：只作计时账本 recovery_required／授权的原因标识，不入总账计数。"""

    return {
        "root_cause_id": f"evidence-conflict-{int(facts['quarantine_index']):02d}",
        "stable_error_code": "attempt.evidence-conflict-quarantined",
        "failed_step": "evidence-conflict-quarantine",
        "stable_dimensions": {"phase": phase},
        "component": COMPONENT,
    }


def _isolation_invalidation_cause(phase: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """隔离作废的暂停原因：只作计时账本 recovery_required／授权的原因标识，不入总账计数。"""

    return {
        "root_cause_id": f"environment-isolation-{int(facts['isolation_index']):02d}",
        "stable_error_code": "attempt.environment-isolation-invalidated",
        "failed_step": "environment-isolation",
        "stable_dimensions": {"phase": phase},
        "component": COMPONENT,
    }


def reconcile_attempt(
    campaign_dir: Path,
    attempt_id: str,
    *,
    control_root: Path | None = None,
    approve_recovery_sha256: str | None = None,
    now: str | None = None,
    recovery_revision: str | None = None,
) -> dict[str, Any]:
    """对账一个 reservation 之后中断的 attempt（步骤 1～6）。

    改造 5 M2：``recovery_revision=ar<k>`` 时对账的是该 attempt 的恢复段（段预约之后中断／失败）：
    定位 ``recovery/ar<k>/``，收据带 ``recovery_revision``，operation ``reconcile-attempt:<id>:ar<k>``，
    账本写 ``attempt_recovery_failed``（原 attempt 事件不动），恢复预览按四项判据分割基线冻结的 J*，
    批准后由后继段 ar<k+1> 只执行 execute 集合。
    """

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("reconcile-attempt 只用于 0.154.0 起的完整 VC 链 Campaign")
    observed = now or _utc_now()
    phase, candidate_id, attempt_root = _locate_attempt(campaign_dir, attempt_id)
    attempt: dict[str, Any] | None = None
    # 修好接着跑第 21 项：等待封存、但有已完成作业被其后登记的工具演进作废的 attempt（不是失败）。
    evolution_invalidation: dict[str, Any] | None = None
    # 修好接着跑第 13 项：等待封存、但被环境隔离作废的 attempt（同一协议，全部作业失效）。
    isolation_invalidation: dict[str, Any] | None = None
    # 修好接着跑第 24 项：等待封存、但被证据根冲突隔离的 attempt（同一协议，全部作业失效）。
    conflict_invalidation: dict[str, Any] | None = None
    # 修好接着跑第 28 项：首次作废之后叠加的其它作废来源（只记入输出）。
    stacked_invalidations: dict[str, dict[str, Any]] = {}
    if recovery_revision is not None:
        if phase != "candidate" or candidate_id is None:
            raise ReconcilerError("恢复段只存在于候选 attempt")
        if not vc_artifacts.RECOVERY_REVISION_RE.fullmatch(recovery_revision):
            raise ReconcilerError("--recovery-revision 必须是 ar<k>")
        segment_root = codex_upgrade._attempt_recovery_segment_root(attempt_root, recovery_revision)
        if segment_root.is_symlink() or not segment_root.is_dir():
            raise ReconcilerError(f"attempt {attempt_id} 没有恢复段 {recovery_revision}")
        reservation = codex_upgrade._load_attempt_recovery_reservation(
            campaign_dir, segment_root, candidate_id=candidate_id, attempt_id=attempt_id, recovery_revision=recovery_revision
        )
        # P1（授权闭包）：J* 只沿权威链取（段预约三元组 → COMMIT → recovery.json），任一环不等即失败关闭。
        try:
            _baseline, _commit, authoritative_recovery = codex_upgrade._authoritative_recovery_execute_jobs(
                campaign_dir, candidate_id, reservation
            )
        except codex_upgrade.ConfigurationError as error:
            raise ReconcilerError(f"恢复段 {recovery_revision} 的 J* 权威链不成立：{error}") from error
        recovery_execute_jobs = [str(item) for item in authoritative_recovery["execute_jobs"]]
        summary_path = segment_root / codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
        if summary_path.exists() or summary_path.is_symlink():
            _segment, _reservation, attempt = recovery_segment_summary(
                campaign_dir, candidate_id, attempt_id, recovery_revision
            )
            if attempt.get("status") == "awaiting_receipts" and not codex_upgrade._failed_job_ids(attempt.get("results")):
                raise ReconcilerError("恢复段正等待增量封存，不是中断；reconcile-attempt 只处理失败或中断的恢复段")
        current_stage = codex_upgrade._stage_path(campaign_dir, "capture-candidate", candidate_id)[1]
        if current_stage.is_file():
            sealed_recovery = _read_json(current_stage, "候选阶段结果").get("recovery")
            if isinstance(sealed_recovery, Mapping) and sealed_recovery.get("recovery_revision") == recovery_revision:
                raise ReconcilerError("该恢复段已增量封存，不再属于可对账的中断")
        work_root = segment_root
    else:
        reservation = codex_upgrade._load_capture_reservation(
            campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id, _manifest=manifest
        )
        attempt_path = attempt_root / "attempt.json"
        if attempt_path.exists() or attempt_path.is_symlink():
            _root, attempt = codex_upgrade._load_capture_attempt(
                campaign_dir, phase, candidate_id, attempt_id, _verified_campaign_manifest=manifest
            )
            if attempt.get("status") == "awaiting_receipts" and not codex_upgrade._failed_job_ids(attempt.get("results")):
                # 修好接着跑第 28 项：作废来源以首次落盘的对账收据为准；其后叠加的作废来源只记入输出，不改写 write-once 收据。
                (
                    evolution_invalidation,
                    isolation_invalidation,
                    conflict_invalidation,
                    stacked_invalidations,
                ) = _attempt_invalidation_facts(
                    campaign_dir, manifest, attempt_root, attempt, phase=phase, candidate_id=candidate_id, attempt_id=attempt_id
                )
                if evolution_invalidation is None and isolation_invalidation is None and conflict_invalidation is None:
                    raise ReconcilerError(
                        "attempt 正等待 seal，不是中断；reconcile-attempt 只处理失败、中断或被工具演进／环境隔离／"
                        "证据根冲突隔离作废的 attempt"
                    )
        stage_result = codex_upgrade._stage_path(campaign_dir, "capture-official" if phase == "official" else "capture-candidate", candidate_id)[1]
        if stage_result.exists():
            raise ReconcilerError("该阶段已封存，attempt 不再属于可对账的中断")
        work_root = attempt_root
        recovery_execute_jobs = None
    subject = _recovery_segment_subject(attempt_id, recovery_revision)
    records, latest_checkpoints, chain = _checkpoint_facts(work_root)
    jobs = _classify_jobs(campaign_dir, manifest, reservation, attempt, latest_checkpoints, work_root)
    environment_decision = codex_upgrade._campaign_environment_decision(campaign_dir, _manifest=manifest)
    environment = _environment_facts(
        campaign_dir, work_root, attempt, environment_decision["records"], decision=environment_decision
    )
    current = _current_identity()
    identity = _identity_facts(campaign_dir, manifest, current)
    ledger_dir = _campaign_ledger_dir(manifest)
    ledger = _ledger_facts(ledger_dir, now=observed)
    _require_registered_tool_identity(identity, ledger)
    _require_resume_closed(campaign_dir, manifest, ledger_dir)
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    deployment = _deployment_receipt(
        _control_root(campaign_dir, control_root), current, required=not bool(plan.get("fixture_only"))
    )
    campaign_deadline = codex_upgrade._campaign_plan_deadline(campaign_dir)
    receipt_dir = _reconciliation_dir(campaign_dir, f"attempt-{subject.replace(':', '-')}")

    # 历史 attempt 的失败数组必须在任何收据落盘前完成只读重放。否则损坏的
    # run-summary 会让命令失败，却先遗留一个看似可信的 provenance 副本。
    failure_observations, recorded_root_causes, array_contract = (
        _attempt_recorded_failures(attempt)
    )

    # 步骤 2 的请求部分随后核算。第 38 项起它只决定账务暂停（unresolved），不再是根因依据。
    request_part, provenance_binding, provenance_copy_path = _request_part(
        campaign_dir, manifest, receipt_dir, plan=plan, head=head, project_root=project_root, now=observed
    )
    failure_cause, failure_root_causes = _attempt_effective_root_causes(
        phase=phase,
        attempt=attempt,
        environment_status=environment["status"],
        identity_unchanged=bool(identity["unchanged"]),
        deadline_expired=_attempt_deadline_expired(
            attempt=attempt,
            ledger=ledger,
            plan=plan,
            campaign_deadline_at_utc=campaign_deadline,
            now=observed,
            project_ledger_root=project_root,
        ),
        jobs=jobs,
        ledger_status=ledger.get("status"),
        recorded_root_causes=recorded_root_causes,
    )
    if evolution_invalidation is not None or isolation_invalidation is not None or conflict_invalidation is not None:
        # 演进失效不是失败：不编失败根因、不计同根因次数（总账载荷不带 root_cause）。计时账本的暂停原因
        # 用 tool-evolution-NN（总账里不存在，不会触顶），授权按同一原因恢复阶段。环境隔离作废同一口径，
        # 原因用 environment-isolation-NN；证据根冲突隔离作废（第 24 项）用 evidence-conflict-NN。
        cause = (
            _evolution_invalidation_cause(phase, evolution_invalidation)
            if evolution_invalidation is not None
            else _isolation_invalidation_cause(phase, isolation_invalidation)
            if isolation_invalidation is not None
            else _conflict_invalidation_cause(phase, conflict_invalidation)
        )
        root_causes = [cause]
        array_contract = False
        failure_observations = []
    else:
        cause, root_causes = failure_cause, failure_root_causes

    # 步骤 1：Campaign 侧写 reconciliation 收据（写一次），再登记账本 attempt_failed。
    receipt = {
        "schema_version": ATTEMPT_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": _file_sha256(campaign_dir / "campaign.json"),
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "attempt_receipt_exists": attempt is not None,
        "attempt_status": attempt.get("status") if attempt is not None else None,
        "attempt_digest": (
            attempt.get("attempt_recovery_digest" if recovery_revision is not None else "attempt_digest")
            if attempt is not None
            else None
        ),
        "reservation": {
            "path": (
                work_root / (codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME if recovery_revision is not None else "reservation.json")
            ).relative_to(campaign_dir).as_posix(),
            "sha256": _file_sha256(
                work_root / (codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME if recovery_revision is not None else "reservation.json")
            ),
            "run_nonce": reservation["run_nonce"],
            "started_at_utc": reservation["started_at_utc"],
        },
        "checkpoint_chain": chain,
        "jobs": jobs,
        "environment": environment,
        "tool_identity": identity,
        "deployment_receipt": deployment,
        "campaign_ledger": ledger,
        "project_ledger": {
            "path": str(project_root),
            "plan_sha256": plan.get("plan_sha256"),
            "head_sequence_before": head.get("sequence"),
            "head_sha256_before": head.get("head_sha256"),
        },
        "campaign_deadline_at_utc": campaign_deadline,
        "root_cause": cause,
        "request_part_status": request_part["status"],
        "provenance_receipt_sha256": request_part["provenance_receipt_sha256"],
        "reservation_exists": True,
        "live_request_count": 0,
        "scanned_bytes": 0,
        "observed_at_utc": observed,
    }
    if array_contract:
        receipt["failure_observations"] = failure_observations
        receipt["root_causes"] = root_causes
    if evolution_invalidation is not None:
        receipt["tool_evolution_invalidation"] = dict(evolution_invalidation)
    if isolation_invalidation is not None:
        receipt["environment_isolation_invalidation"] = dict(isolation_invalidation)
    if conflict_invalidation is not None:
        receipt["evidence_conflict_invalidation"] = dict(conflict_invalidation)
    receipt_path = receipt_dir / ATTEMPT_RECEIPT_NAME
    with codex_upgrade._campaign_lock(campaign_dir):
        stored = _write_or_verify(
            receipt_path,
            receipt,
            volatile=(
                "observed_at_utc",
                "campaign_ledger",
                "project_ledger",
                "deployment_receipt",
                "tool_identity",
                *RECEIPT_ROOT_CAUSE_VOLATILE_FIELDS,
                "request_part_status",
                "provenance_receipt_sha256",
                # 修好接着跑第 13 项：污染记录与判定口径随 environment-isolate 变化，隔离后同一对象要能重新对账
                # （与监督器运行对账的 contamination_records 同口径）。
                "environment",
            ),
        )
        # 中断后重放：根因与请求状态以首次落盘的收据为准，后续步骤按同一根因幂等推进。
        cause = dict(stored["root_cause"])
        if array_contract:
            failure_observations = [
                dict(item) for item in stored["failure_observations"]
            ]
            root_causes = [dict(item) for item in stored["root_causes"]]
        # 修好接着跑第 38 项：首次收据（第 38 项之前写下）的主根因是账务占位根因时，按首次收据的事实重算真实根因并写
        # 重归属收据；之后的账本事件、outbox 与判定都用真实根因，推送后再把总账里的占位根因更正为真实根因。
        reattribution: dict[str, Any] | None = None
        if _legacy_accounting_placeholder(stored):
            placeholder = dict(cause)
            cause, root_causes, basis = _reattributed_root_causes(
                stored,
                phase=phase,
                attempt=attempt,
                plan=plan,
                project_root=project_root,
                array_contract=array_contract,
            )
            reattribution = {
                "placeholder": placeholder,
                "basis": basis,
                "receipt": _write_root_cause_reattribution(
                    receipt_path,
                    campaign_id=str(manifest["campaign_id"]),
                    attempt_id=attempt_id,
                    recovery_revision=recovery_revision,
                    operation_id=f"reconcile-attempt:{subject}",
                    placeholder=placeholder,
                    cause=cause,
                    root_causes=root_causes,
                    basis=basis,
                ),
            }
        receipt_binding = _binding(campaign_dir, receipt_path, "reconciliation")
        ledger_events: list[dict[str, Any]] = []
        ledger_note = "recorded"
        active_ids = {item["attempt_id"] for item in ledger["active_attempts"]}
        # 候选审核（VC-5 采集失败）下的续跑：attempt 事件登记在审核阶段，之后可批准恢复预览。
        capture_review = (
            _candidate_capture_review(
                campaign_dir,
                manifest,
                ledger_dir,
                ledger,
                phase=phase,
                candidate_id=candidate_id,
                attempt_id=attempt_id,
                strict=False,
            )
            if recovery_revision is None
            else None
        )
        if recovery_revision is not None:
            # 恢复段失败／中断：原 attempt 事件不动，只把 active 的恢复段登记为 failed（带根因，计入同根因）。
            segment_state = timing_ledger.inspect_ledger(ledger_dir, now=observed).get("attempt_recoveries", {}).get(
                f"{attempt_id}:{recovery_revision}", {}
            )
            if segment_state.get("status") == "active" and ledger["status"] in {"active", "recovery_required", "deadline_paused"}:
                ledger_events.append(
                    _append_ledger_event(
                        ledger_dir,
                        event_id=f"reconcile-attempt-recovery-failed-{attempt_id}-{recovery_revision}",
                        phase="VC-5",
                        event_type="attempt_recovery_failed",
                        attempt_id=attempt_id,
                        root_cause_id=cause["root_cause_id"],
                        next_action="reconcile-attempt --recovery-revision",
                        recovery_revision=recovery_revision,
                        candidate_id=candidate_id,
                    )
                )
            else:
                ledger_note = f"skipped:recovery_segment_{segment_state.get('status') or 'unknown'}"
        elif evolution_invalidation is not None or isolation_invalidation is not None or conflict_invalidation is not None:
            # attempt 已完成（账本是 attempt_completed），不补 attempt 事件；把所在阶段暂停为 recovery_required，
            # 授权后按恢复预览只重跑失效作业。阶段已因别的原因暂停时沿用该原因（授权一并消费）；到期暂停
            # 由判定提示先延期；其它状态只入账。
            expected_phase = "VC-1" if phase == "official" else "VC-5"
            if ledger["status"] == "active" and ledger.get("active_phase") == expected_phase:
                ledger_events.append(
                    _append_ledger_event(
                        ledger_dir,
                        event_id=_invalidation_ledger_event_id(attempt_id, isolation_invalidation, conflict_invalidation),
                        phase=expected_phase,
                        event_type="recovery_required",
                        root_cause_id=cause["root_cause_id"],
                        next_action="resume-rerun-failed",
                    )
                )
            else:
                ledger_note = (
                    f"skipped:evolution_invalidation_ledger_{ledger['status']}"
                    if evolution_invalidation is not None
                    else f"skipped:isolation_invalidation_ledger_{ledger['status']}"
                    if isolation_invalidation is not None
                    else f"skipped:conflict_invalidation_ledger_{ledger['status']}"
                )
        elif ledger["status"] == "active" and ledger.get("active_phase") is None:
            ledger_note = "skipped:ledger_active_without_phase"
        elif ledger["status"] in {"active", "recovery_required", "stage_review_required"} or (ledger["status"] == "deadline_paused" and attempt_id in active_ids):
            if attempt_id not in active_ids:
                ledger_events.append(
                    _append_ledger_event(
                        ledger_dir,
                        event_id=f"reconcile-attempt-started-{attempt_id}",
                        phase=str(ledger.get("active_phase") or ledger["review_phase"]),
                        event_type="attempt_started",
                        attempt_id=attempt_id,
                        next_action="reconcile-attempt",
                    )
                )
            ledger_events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-attempt-failed-{attempt_id}",
                    phase=str(ledger.get("active_phase") or ledger["review_phase"]),
                    event_type="attempt_failed",
                    attempt_id=attempt_id,
                    root_cause_id=cause["root_cause_id"],
                    next_action="reconcile-attempt",
                )
            )
        elif capture_review is not None:
            if attempt_id not in active_ids:
                ledger_events.append(
                    _append_ledger_event(
                        ledger_dir,
                        event_id=f"reconcile-attempt-started-{attempt_id}",
                        phase=capture_review["phase"],
                        event_type="attempt_started",
                        attempt_id=attempt_id,
                        next_action="reconcile-attempt",
                    )
                )
            ledger_events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-attempt-failed-{attempt_id}",
                    phase=capture_review["phase"],
                    event_type="attempt_failed",
                    attempt_id=attempt_id,
                    root_cause_id=cause["root_cause_id"],
                    next_action="reconcile-attempt",
                )
            )
        elif ledger["status"] == "stop_required" and attempt_id in active_ids:
            ledger_events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-attempt-failed-{attempt_id}",
                    phase=str(ledger["active_phase"]),
                    event_type="attempt_failed",
                    attempt_id=attempt_id,
                    root_cause_id=cause["root_cause_id"],
                    next_action="reconcile-attempt",
                )
            )
        else:
            ledger_note = f"skipped:ledger_{ledger['status']}_without_active_attempt"
        failed_event_id = (
            f"reconcile-attempt-recovery-failed-{attempt_id}-{recovery_revision}"
            if recovery_revision is not None
            else f"reconcile-attempt-failed-{attempt_id}"
        )
        failed_sha = next(
            (item["event_sha256"] for item in ledger_events if item["event_id"] == failed_event_id),
            _ledger_event_sha256(ledger_dir, failed_event_id),
        )

        # 步骤 2：一个 batch 一个事件 reconciliation_committed。
        reconciliation_payload: dict[str, Any] = {
            "campaign_id": str(manifest["campaign_id"]),
            "subject_kind": "attempt" if recovery_revision is None else "attempt_recovery",
            "subject_id": subject,
            "phase": phase,
            "request": request_part,
            "root_cause": {
                "root_cause_id": cause["root_cause_id"],
                "stable_error_code": cause["stable_error_code"],
                "failed_step": cause["failed_step"],
                "stable_dimensions": cause["stable_dimensions"],
                "component": cause["component"],
            },
            "reconciliation_receipt_sha256": receipt_binding["sha256"],
            "attempt_failed_event_sha256": failed_sha,
        }
        if array_contract:
            reconciliation_payload["failure_observations"] = failure_observations
            reconciliation_payload["root_causes"] = root_causes
        if recovery_revision is not None:
            reconciliation_payload["recovery_revision"] = recovery_revision
        if evolution_invalidation is not None or isolation_invalidation is not None or conflict_invalidation is not None:
            # 演进失效与环境隔离／证据根冲突隔离作废只核算请求，不带 root_cause：总账不把它计入同根因次数。
            del reconciliation_payload["root_cause"]
            if evolution_invalidation is not None:
                reconciliation_payload["tool_evolution_invalidation"] = dict(evolution_invalidation)
            elif isolation_invalidation is not None:
                reconciliation_payload["environment_isolation_invalidation"] = dict(isolation_invalidation)
            else:
                reconciliation_payload["evidence_conflict_invalidation"] = dict(conflict_invalidation)
            reconciliation_payload["recovery_required_event_sha256"] = _ledger_event_sha256(
                ledger_dir, _invalidation_ledger_event_id(attempt_id, isolation_invalidation, conflict_invalidation)
            )
        batch = _commit_batch(
            campaign_dir,
            operation_id=f"reconcile-attempt:{subject}",
            event_type="reconciliation_committed",
            payload=reconciliation_payload,
            source={"kind": "attempt_reconciliation", "sha256": receipt_binding["sha256"]},
            receipt_bindings=[receipt_binding, provenance_binding],
        )
    # 第 45 项：发布本预约的父 run 来不及收账（R2 封存或看门狗中止＋动作诊断）时，按父监督器同一收账函数补账，再按
    # 收口后的账本判定与生成恢复预览。放在本 attempt／恢复段的失败登记之后、Campaign 锁之外（收账可能登记预算暂停，
    # 那会自取 Campaign 锁）；作废对账（演进、隔离、证据根冲突）不是动作失败，不补。
    closeout_backfill: dict[str, Any] | None = None
    if evolution_invalidation is None and isolation_invalidation is None and conflict_invalidation is None:
        closeout_backfill = _backfill_attempt_owner_closeout(
            campaign_dir,
            manifest,
            ledger_dir,
            phase=phase,
            candidate_id=candidate_id,
            attempt_id=attempt_id,
            recovery_revision=recovery_revision,
            reservation=reservation,
            # 第 47 项：首批父 run 没有 COMMIT，按控制根下的监督器状态目录定位。
            control_root=control_root,
        )
        if closeout_backfill is not None:
            # 补做的收账事件按写入时刻记账；之后的账本重放与判定按不早于它的时刻观察。
            observed = _later_timestamp(observed, _utc_now())
            ledger = _ledger_facts(ledger_dir, now=observed)
    # 步骤 3、4：推送并锁内重放。
    pushed, head_after = _push_and_replay(project_root, campaign_dir, now=observed)
    reattribution_output: dict[str, Any] | None = None
    if reattribution is not None:
        # 第 38 项：总账原事件若是按占位根因写入的（首次对账已推送），追加历史更正把它改记为真实根因，再重放。
        correction = _ensure_reattribution_correction(
            project_root,
            operation_id=f"reconcile-attempt:{subject}",
            placeholder_id=str(reattribution["placeholder"]["root_cause_id"]),
            cause=cause,
            root_causes=root_causes,
            reattribution_sha256=str(reattribution["receipt"]["sha256"]),
        )
        if correction["status"] == "appended":
            _plan_after, head_after = _project_facts(project_root)
        reattribution_output = {
            "placeholder_root_cause_id": reattribution["placeholder"]["root_cause_id"],
            "root_cause_id": cause["root_cause_id"],
            "basis": reattribution["basis"],
            "receipt": _binding(campaign_dir, reattribution["receipt"]["path"], "root_cause_reattribution"),
            "project_correction": correction,
        }
    # 步骤 5：判定。
    decision = _decide(
        head=head_after,
        plan=plan,
        ledger=ledger,
        identity=identity,
        environment_status=environment["decision_status"],
        campaign_deadline_at_utc=campaign_deadline,
        root_cause_id=cause["root_cause_id"],
        root_cause_ids=[item["root_cause_id"] for item in root_causes],
        request_status=request_part["status"],
        now=observed,
        campaign_id=str(manifest["campaign_id"]),
    )
    result: dict[str, Any] = {
        "schema_version": ATTEMPT_SCHEMA,
        "status": decision["decision"],
        "campaign_id": str(manifest["campaign_id"]),
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "phase": phase,
        "reconciliation_receipt": receipt_binding,
        "provenance_receipt": provenance_binding,
        "root_cause": cause,
        "jobs": jobs["groups"],
        "environment_status": environment["status"],
        "identity_unchanged": identity["unchanged"],
        "ledger_attempt_events": ledger_note,
        "ledger_events": ledger_events,
        "batch": batch,
        "project_push": pushed,
        "project_head": {
            "sequence": head_after.get("sequence"),
            "head_sha256": head_after.get("head_sha256"),
            "blocked": head_after.get("blocked"),
            "remaining_live_requests": head_after.get("remaining_live_requests"),
            "root_cause_count": decision["root_cause_count"],
            "root_cause_counts": decision["root_cause_counts"],
        },
        "decision": decision,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    if array_contract:
        result["failure_observations"] = failure_observations
        result["root_causes"] = root_causes
    if closeout_backfill is not None:
        # 第 45 项：本次对账补做了预约所属父 run 未完成的失败收账（只进命令输出，不进 write-once 收据）。
        result["ledger_closeout_backfill"] = closeout_backfill
    if reattribution_output is not None:
        result["root_cause_reattribution"] = reattribution_output
    if evolution_invalidation is not None:
        result["tool_evolution_invalidation"] = dict(evolution_invalidation)
    if isolation_invalidation is not None:
        result["environment_isolation_invalidation"] = dict(isolation_invalidation)
    if conflict_invalidation is not None:
        result["evidence_conflict_invalidation"] = dict(conflict_invalidation)
    if stacked_invalidations:
        # 第 28 项：首次作废之后叠加的作废来源只在输出里留痕；收据、总账与账本沿用首次落盘的作废事实。
        result["stacked_invalidations"] = {name: dict(facts) for name, facts in stacked_invalidations.items()}
    # 步骤 6。
    if decision["decision"] == DECISION_RECOVERABLE:
        provenance_copy = _read_json(campaign_dir / provenance_binding["path"], "provenance 副本")
        preview = _recovery_preview(
            campaign_dir,
            receipt_dir,
            manifest=manifest,
            attempt_id=attempt_id,
            recovery_revision=recovery_revision,
            phase=phase,
            candidate_id=candidate_id,
            attempt_exists=attempt is not None,
            jobs=jobs,
            environment_status=environment["status"],
            provenance_copy=provenance_copy,
            current=current,
            reconciliation_receipt_sha256=receipt_binding["sha256"],
            campaign_ledger_head=_ledger_facts(ledger_dir, now=_utc_now()),
            project_ledger_scope=_campaign_event_scope(project_root, str(manifest["campaign_id"])),
            now=observed,
            recovery_execute_jobs=recovery_execute_jobs,
        )
        result["recovery_preview"] = preview
        result["scanned_bytes"] = preview["scanned_bytes"]
        result["recovery_preview_path"] = str(receipt_dir / f"recovery-preview-{int(preview['index']):02d}.json")
        if recovery_revision is not None:
            # R11：失败段不原地续跑，后继段仅启动批准的 execute，可信 complete Job 明确引用复用。
            number = int(recovery_revision[2:])
            resume_command = f"capture-candidate run --attempt-recovery ar{number + 1} --rerun-failed"
        else:
            resume_command = "resume --rerun-failed"
        result["next_command"] = (
            f"reconcile-attempt --approve-recovery-sha256 {preview['review_sha256']} 后 "
            f"{resume_command} --recovery-preview <preview path>"
        )
        # R17：批准前按 resume 同一复用判定只读复算。不一致的预览照常落盘留痕（对账记账不受影响），
        # 但不可批准：不派批次、不计根因次数，按原因修复后重新对账。
        result["resume_reuse_check"] = _resume_reuse_check(
            campaign_dir, phase=phase, recovery_revision=recovery_revision, preview=preview
        )
        if result["resume_reuse_check"]["status"] == "inconsistent":
            result["next_command"] = (
                "恢复预览与 resume 复用判定不一致（见 resume_reuse_check.reason），不可批准；"
                "按原因修复后重新执行 reconcile-attempt"
            )
        # 第 48 项：恢复段失败而账本处于候选审核（段 run 的审核类失败经补账进入候选审核，或第 48 项之前按旧口径收口的
        # 截止类段失败）时，授权只接 VC-5 采集失败 attempt 的续跑、拒绝恢复段，不能再提示"批准预览后开后继段"：改为候选
        # 审核的处置，并拒绝批准（批准了也无法授权）。
        segment_under_candidate_review = (
            recovery_revision is not None
            and _ledger_facts(ledger_dir, now=_utc_now()).get("status") == "candidate_review_required"
        )
        if segment_under_candidate_review:
            result["next_command"] = (
                "candidate_review_required：恢复段失败已入账；候选审核下不接受恢复段续跑授权（批准预览后开后继段会被授权拒绝）"
                "——判为候选源码问题则 invalidate-candidate preview/apply，否则以 close-campaign-ledger 显式停线"
            )
        if approve_recovery_sha256 is not None:
            if segment_under_candidate_review:
                raise ReconcilerError(
                    "候选审核下不接受恢复段续跑批准：授权只允许 VC-5 采集失败 attempt 的续跑；恢复段按候选审核处置"
                    "（invalidate-candidate 或 close-campaign-ledger）"
                )
            if result["resume_reuse_check"]["status"] == "inconsistent":
                raise ReconcilerError(
                    "恢复预览与 resume 复用判定不一致，拒绝批准："
                    + str(result["resume_reuse_check"]["reason"])
                )
            result["recovery_approval"] = approve_recovery_preview(
                campaign_dir, attempt_id, approve_sha256=approve_recovery_sha256, recovery_revision=recovery_revision
            )
            result["next_command"] = f"{resume_command} --recovery-preview {result['recovery_preview_path']}"
    elif decision["decision"] == DECISION_PAUSED:
        if approve_recovery_sha256 is not None:
            raise ReconcilerError("预算暂停期间不接受恢复批准；必须先批准延期")
        if set(decision.get("pause_kinds") or []) - {"root_cause_repair"}:
            # 纯根因修复暂停（第三批 B3-9）不写预算暂停：账本已处于 stop_required，预算暂停会被账本拒绝。
            result["deadline_pause"] = project_ledger.pause_campaign_deadline(campaign_dir, now=_pause_time(observed))
        result["next_command"] = _paused_next_command(decision, "从原对账 checkpoint 继续")
    else:
        if approve_recovery_sha256 is not None:
            raise ReconcilerError("判定为永久停线，不接受恢复批准")
        with codex_upgrade._campaign_lock(campaign_dir):
            stop = _permanent_stop(
                campaign_dir,
                manifest,
                ledger_dir,
                subject_id=attempt_id,
                root_cause_id=cause["root_cause_id"],
                terminal_reason=str(decision["terminal_reason"]),
                receipt_bindings=[receipt_binding, provenance_binding],
                ledger_receipts=_ledger_receipt_bindings(
                    ledger_dir, f"attempt-{attempt_id}", receipt_path, provenance_copy_path
                ),
                live_request_count=len(request_part["identity_keys"]),
                reconciliation_receipt_sha256=receipt_binding["sha256"],
                ledger_facts=ledger,
            )
        pushed_terminal, head_terminal = _push_and_replay(project_root, campaign_dir, now=observed)
        result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
        result["next_command"] = stop["next_action"]
    return result


# ---------------------------------------------------------------------------
# reconcile-supervisor-run
# ---------------------------------------------------------------------------


def _capture_attempt_settled(attempt_root: Path) -> bool:
    """主 attempt 的采集是否已完整收口：attempt 等待封存（awaiting_receipts）且结果里没有失败 Job。

    第三批 B3-5（第 17 项）：父 run 窗口内这样的 attempt 不是中断——采集已完成，只是之后的零请求后处理
    动作因评估器漂移没有执行；父 run 按 tool-evolution-required 对账，采集结果只读保留。
    """

    attempt_path = Path(attempt_root) / "attempt.json"
    if attempt_path.is_symlink() or not attempt_path.is_file():
        return False
    attempt_payload = _read_json(attempt_path, "attempt 收据")
    return attempt_payload.get("status") == "awaiting_receipts" and not codex_upgrade._failed_job_ids(
        attempt_payload.get("results")
    )


def _settled_capture_attempts_in_window(run_dir: Path, campaign_dir: Path) -> list[str]:
    """父 run 启动之后预约、且采集已完整收口的主 attempt 名（只读，供对账 next_command 区分情形）。"""

    state = supervisor._read_state(Path(run_dir))
    started_utc = datetime.fromtimestamp(float(state["started_at_epoch"]), tz=timezone.utc)
    settled: list[str] = []
    for _phase, _candidate, attempt_root in codex_upgrade._campaign_attempt_roots(campaign_dir):
        reservation_path = attempt_root / "reservation.json"
        if not reservation_path.is_file():
            continue
        try:
            begun = _timestamp(_read_json(reservation_path, "预约收据").get("started_at_utc"), "reservation.started_at_utc")
        except ReconcilerError:
            continue
        if begun >= started_utc and _capture_attempt_settled(attempt_root):
            settled.append(attempt_root.name)
    return settled


def _run_facts(run_dir: Path, campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    run_dir = Path(run_dir)
    if not run_dir.is_absolute() or run_dir.is_symlink() or not run_dir.is_dir():
        raise ReconcilerError(f"run 目录不存在或不可信：{run_dir}")
    try:
        state = supervisor._read_state(run_dir)
        audit = supervisor._audit_command(run_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"监督器 run 目录无法审计：{error}") from error
    manifest_path = run_dir / "campaign-run-manifest.json"
    run_manifest = _read_json(manifest_path, "campaign-run 清单") if manifest_path.is_file() else None
    inner = run_manifest.get("manifest") if isinstance(run_manifest, Mapping) else None
    if state.get("campaign_id") != manifest.get("campaign_id"):
        raise ReconcilerError("run 目录的 campaign_id 与 --campaign-dir 不一致")
    if isinstance(inner, Mapping) and inner.get("campaign_id") not in {None, manifest.get("campaign_id")}:
        raise ReconcilerError("campaign-run 清单的 campaign_id 与 Campaign 不一致")
    owner_alive = supervisor._owner_alive(int(state["owner_pid"]))
    if state.get("state") in supervisor.ACTIVE_STATES and owner_alive:
        raise ReconcilerError("父监督器仍在运行（或 prepared 且 owner 在线），禁止对账")
    if state.get("state") in supervisor.ACTIVE_STATES:
        raise ReconcilerError(
            "父 run 尚未终态化；prepared／committed 未启动的孤儿须先由 finalize_prepared_run 封存"
        )
    started = float(state["started_at_epoch"])
    started_utc = datetime.fromtimestamp(started, tz=timezone.utc)
    reservations_in_window: list[str] = []
    for _phase, _candidate, attempt_root in codex_upgrade._campaign_attempt_roots(campaign_dir):
        reservation_path = attempt_root / "reservation.json"
        if not reservation_path.is_file():
            continue
        payload = _read_json(reservation_path, "预约收据")
        try:
            begun = _timestamp(payload.get("started_at_utc"), "reservation.started_at_utc")
        except ReconcilerError:
            continue
        if begun < started_utc:
            continue
        # 第三批 B3-5（第 17 项）：同一父 run 内采集已经完整收口（attempt 等待封存、无失败 Job），之后的后处理动作
        # 才因评估器漂移未执行——这不是 attempt 中断，父 run 按 tool-evolution-required 对账；采集结果只读保留，
        # 登记演进后按作废作业续跑或改派 seal 链批次（判定与 _settled_capture_attempts_in_window 共用）。
        if _capture_attempt_settled(attempt_root):
            continue
        reservations_in_window.append(attempt_root.name)
    # 改造 5 M2：父 run 期间发布的恢复段预约同样分流到 reconcile-attempt --recovery-revision，但只针对
    # 未成功收口的段；已 awaiting_receipts 的段不是中断（父 run 在动作退出后崩溃属崩溃矩阵 R2 的
    # attempt-recovery 变体：父 run 对账后环境恢复重派，段 run 幂等返回）。
    for _phase, _candidate, attempt_root in codex_upgrade._campaign_attempt_roots(campaign_dir):
        recovery_root = attempt_root / codex_upgrade.ATTEMPT_RECOVERY_DIRNAME
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            continue
        for segment_root in sorted(recovery_root.iterdir()):
            if segment_root.is_symlink() or not segment_root.is_dir():
                continue
            if not vc_artifacts.RECOVERY_REVISION_RE.fullmatch(segment_root.name):
                continue
            reservation_path = segment_root / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME
            if not reservation_path.is_file():
                continue
            payload = _read_json(reservation_path, "恢复段预约收据")
            try:
                begun = _timestamp(payload.get("started_at_utc"), "recovery-reservation.started_at_utc")
            except ReconcilerError:
                continue
            if begun < started_utc:
                continue
            summary_path = segment_root / codex_upgrade.ATTEMPT_RECOVERY_SUMMARY_FILENAME
            if summary_path.is_file():
                summary = _read_json(summary_path, "恢复段 run-summary")
                if summary.get("status") == "awaiting_receipts" and not codex_upgrade._failed_job_ids(summary.get("results")):
                    continue
            reservations_in_window.append(f"{attempt_root.name}:{segment_root.name}")
    if reservations_in_window:
        raise ReconcilerError(
            "该 run 期间已产生 reservation，属于 attempt 中断；请改用 reconcile-attempt（恢复段加 --recovery-revision）："
            + "、".join(reservations_in_window)
        )
    events = audit.get("integrity_errors", [])
    last_operation = "dispatch"
    events_path = run_dir / "events.ndjson"
    if events_path.is_file():
        lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines:
            try:
                last = json.loads(lines[-1])
                if isinstance(last, dict) and isinstance(last.get("operation"), str):
                    last_operation = last["operation"]
            except json.JSONDecodeError:
                pass
    action_diagnostic: dict[str, Any] | None = None
    diagnostic_root = run_dir / "action-diagnostics"
    if diagnostic_root.exists():
        if diagnostic_root.is_symlink() or not diagnostic_root.is_dir():
            raise ReconcilerError("动作失败诊断目录不可信")
        diagnostic_paths = sorted(diagnostic_root.glob("action-*-failure.json"))
        if len(diagnostic_paths) > 1:
            raise ReconcilerError("父 run 含多份动作失败诊断，无法确定唯一失败分类")
        if diagnostic_paths:
            diagnostic_path = diagnostic_paths[0]
            action_id = diagnostic_path.name.removeprefix("action-").removesuffix(
                "-failure.json"
            )
            try:
                diagnostic = supervisor._validate_action_diagnostic(
                    diagnostic_path,
                    run_dir=run_dir,
                    campaign_id=str(manifest["campaign_id"]),
                    phase=str(state["phase"]),
                    action_id=action_id,
                    owner_pid=int(state["owner_pid"]),
                    owner_nonce=str(state["owner_nonce"]),
                )
            except supervisor.SupervisorError as error:
                raise ReconcilerError(f"动作失败诊断无法重放：{error}") from error
            # 子进程默认的 execution-failure 可能已被父监督器按文件事实升级为
            # post-run-tooling；有效分类必须经同一函数复算收据后才能采信。
            try:
                effective_class, post_run_receipt = supervisor.effective_action_failure_class(
                    run_dir,
                    diagnostic,
                    campaign_dir=campaign_dir,
                    inner_manifest=inner if isinstance(inner, Mapping) else None,
                    campaign_id=str(manifest["campaign_id"]),
                    phase=str(state["phase"]),
                    action_id=action_id,
                    owner_pid=int(state["owner_pid"]),
                    owner_nonce=str(state["owner_nonce"]),
                    run_started_at_utc=str(state.get("started_at_utc", "")),
                )
            except supervisor.SupervisorError as error:
                raise ReconcilerError(f"post-run-tooling 收据无法复算：{error}") from error
            action_diagnostic = {
                "schema_version": diagnostic["schema_version"],
                "path": diagnostic_path.relative_to(run_dir).as_posix(),
                "sha256": diagnostic["diagnostic_sha256"],
                "action_id": action_id,
                # 失败动作的稳定操作名（如 VC-5:accept）：无枚举观测时的根因 failed_step 用它，
                # 而不是父 run 最后事件（恒为 supervisor-stop）或带批内序号的 action_id。
                "operation": _failed_action_operation(inner, action_id),
                "failure_kind": diagnostic["failure_kind"],
                "failure_class": effective_class,
                "declared_failure_class": diagnostic["failure_class"],
                "failure_observations": list(
                    diagnostic["failure_observations"]
                ),
                "error_type": diagnostic["error_type"],
            }
            # 修好接着跑第 60 项：v4 诊断的归一化拒因签名（只有 handled-error／unexpected-error 才有）是动作失败细分根因的
            # 门控。v1～v3 历史诊断没有该字段，run 事实保持原形态，不补键。
            if diagnostic.get("error_signature") is not None:
                action_diagnostic["error_signature"] = diagnostic["error_signature"]
            if post_run_receipt is not None:
                action_diagnostic["post_run_tooling"] = {
                    "schema_version": post_run_receipt["schema_version"],
                    "path": Path(post_run_receipt["path"]).relative_to(run_dir).as_posix(),
                    "sha256": post_run_receipt["receipt_sha256"],
                    "recomputed": bool(post_run_receipt["recomputed"]),
                    "attempt_id": post_run_receipt["facts"].get("attempt_id"),
                    "candidate_id": post_run_receipt["facts"].get("candidate_id"),
                }
    stop_path = run_dir / "stop-receipt.json"
    stop_reason: str | None = None
    stop_action_outputs_sha256: str | None = None
    if stop_path.is_file() and not stop_path.is_symlink():
        try:
            stop_receipt = supervisor.read_stop_receipt(run_dir)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"stop-receipt 无法校验：{error}") from error
        stop_reason_value = stop_receipt.get("reason")
        stop_reason = stop_reason_value if isinstance(stop_reason_value, str) else None
        stop_action_outputs_sha256 = stop_receipt.get("action_outputs_sha256")
    staging = _staging_run_facts(run_dir, state, stop_reason, action_diagnostic, campaign_dir=campaign_dir)
    if staging is not None:
        failure_class = str(staging["failure_class"])
    elif action_diagnostic is not None:
        failure_class = str(action_diagnostic["failure_class"])
    else:
        failure_class = "legacy-interruption"
    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "state": state.get("state"),
        "stop_reason": stop_reason,
        "stop_action_outputs_sha256": stop_action_outputs_sha256,
        "staging": staging,
        "owner_pid": state.get("owner_pid"),
        "owner_alive": owner_alive,
        "phase": state.get("phase"),
        "started_at_utc": state.get("started_at_utc"),
        "terminal_at_utc": state.get("terminal_at_utc"),
        "manifest_sha256": run_manifest.get("manifest_sha256") if isinstance(run_manifest, Mapping) else None,
        "manifest_schema_version": run_manifest.get("schema_version") if isinstance(run_manifest, Mapping) else None,
        "batch_id": inner.get("batch_id") if isinstance(inner, Mapping) else None,
        "batch_sequence": inner.get("batch_sequence") if isinstance(inner, Mapping) else None,
        "batch_sha256": inner.get("batch_sha256") if isinstance(inner, Mapping) else None,
        "no_op": inner.get("no_op") if isinstance(inner, Mapping) else None,
        "execute_items": list(inner.get("execute_items", [])) if isinstance(inner, Mapping) else [],
        "reuse_items": list(inner.get("reuse_items", [])) if isinstance(inner, Mapping) else [],
        "event_count": audit.get("event_count"),
        "minute_record_count": audit.get("minute_record_count"),
        "classification_counts": audit.get("classification_counts"),
        "audit_incomplete": audit.get("audit_incomplete"),
        "integrity_errors": list(events),
        "last_operation": last_operation,
        "action_diagnostic": action_diagnostic,
        "failure_class": failure_class,
        "failure_observations": (
            list(action_diagnostic["failure_observations"])
            if action_diagnostic is not None
            else []
        ),
    }


def _candidate_capture_recovery_kind(run_dir: Path, run: Mapping[str, Any]) -> str | None:
    """父 run 的失败动作属于 VC-5 候选采集续跑链、且这一失败类别由父监督器收账路由到 recovery_required 时，返回动作
    种类（capture／preview／run），否则 None。

    判定与收账同一个函数（``supervisor.candidate_capture_recovery_route``：执行失败对三种动作都算，第 49 项起续跑预览
    与补跑的截止类失败 deadline-expired 也算）；没有动作诊断（无从判定类别与动作）时不算。
    """

    diagnostic = run.get("action_diagnostic")
    if not isinstance(diagnostic, Mapping):
        return None
    manifest_path = Path(run_dir) / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    if not isinstance(inner, Mapping):
        return None
    return supervisor.candidate_capture_recovery_route(
        inner, str(diagnostic.get("action_id")), str(run.get("failure_class"))
    )


def _candidate_capture_recovery_run(run_dir: Path, run: Mapping[str, Any]) -> bool:
    """父 run 的失败动作属于 VC-5 候选采集续跑链（采集／续跑预览／补跑），其失败进入 recovery_required。

    第 49 项：续跑预览与补跑的截止类失败（deadline-expired）与执行失败同样属于这里（与收账同一判定）；此前只认
    execution-failure，收账把截止失败路由为 recovery_required 后，reconcile-supervisor-run 会以"父动作分类不可恢复"拒绝入账。
    """

    if _candidate_capture_recovery_kind(run_dir, run) is not None:
        return True
    diagnostic = run.get("action_diagnostic")
    if run.get("failure_class") != "execution-failure" or not isinstance(diagnostic, Mapping):
        return False
    manifest_path = Path(run_dir) / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    # 第三批 B3-4：VC-5／VC-6 零请求后处理动作的 execution-failure 同样进入 recovery_required。
    return isinstance(inner, Mapping) and (
        supervisor.candidate_post_run_recovery_action(inner, str(diagnostic.get("action_id"))) is not None
    )


def _segment_prereservation_revision(
    run_dir: Path, run: Mapping[str, Any], campaign_dir: Path, ledger_dir: Path
) -> str | None:
    """第 53 项：父 run 的失败动作是恢复段 run、在段预约之前以执行失败或截止类失败收口时返回段编号，否则 None。

    判定与父监督器收账同一组函数（``supervisor.attempt_recovery_segment_reserved``＋
    ``supervisor._attempt_recovery_segment_redispatch``）：段目录不存在、账本没有登记该段。这类失败收账进 recovery_required，
    续跑是按同一段号 N+1 重派；此前 reconcile-supervisor-run 以"父动作分类不可恢复"拒绝入账（死路）。
    """

    diagnostic = run.get("action_diagnostic")
    if run.get("failure_class") not in supervisor.ATTEMPT_RECOVERY_SEGMENT_FAILURE_CLASSES or not isinstance(
        diagnostic, Mapping
    ):
        return None
    manifest_path = Path(run_dir) / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    if not isinstance(inner, Mapping):
        return None
    action_id = str(diagnostic.get("action_id"))
    revision = supervisor._attempt_recovery_run_revision(inner, action_id)
    if revision is None:
        return None
    try:
        summary = timing_ledger.inspect_ledger(ledger_dir)
    except (OSError, timing_ledger.TimingLedgerError) as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    reserved = supervisor.attempt_recovery_segment_reserved(campaign_dir, inner, revision, ledger_summary=summary)
    return supervisor._attempt_recovery_segment_redispatch(
        inner, action_id, str(run.get("failure_class")), segment_reserved=reserved
    )


def _vc1_published_bundle_continuation(run_dir: Path, campaign_dir: Path) -> bool:
    """第 43 项：VC-1 父批次含断言包动作，且该 attempt 的断言证据包已发布、能核对为同一冻结输入的产物（结构核对）。

    这时逐字重派的断言包动作必然被脚本 write-once 拒绝覆盖（监督器 VC-1 门禁也拒绝派发），唯一可行的后继是
    去掉断言包动作、只含 seal 预览（可加 seal 批准）的续派。
    """

    manifest_path = Path(run_dir) / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    actions = inner.get("actions") if isinstance(inner, Mapping) else None
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, Mapping) or action.get("action_id") != supervisor.OFFICIAL_ASSERTION_ACTION_ID:
            continue
        target = supervisor._seal_chain_attempt_target(action)
        if target is None or target[1] != "official":
            return False
        try:
            facts = supervisor.official_assertion_bundle_facts(campaign_dir, target[3], verify_content=False)
        except supervisor.SupervisorError:
            return False
        return bool(facts["published"] and facts["consistent"])
    return False


def _candidate_post_run_recovery_run(run_dir: Path, run: Mapping[str, Any]) -> bool:
    """第三批 B3-4：父 run 的失败动作是 VC-5／VC-6 的零请求后处理动作（post-run-tooling 判据未成立而以 execution-failure 收口）。"""

    diagnostic = run.get("action_diagnostic")
    if run.get("failure_class") != "execution-failure" or not isinstance(diagnostic, Mapping):
        return False
    manifest_path = Path(run_dir) / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return False
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    return (
        isinstance(inner, Mapping)
        and supervisor.candidate_post_run_recovery_action(inner, str(diagnostic.get("action_id"))) is not None
    )


def _failed_action_operation(inner: Any, action_id: str) -> str | None:
    """从 campaign-run 内层清单定位失败动作的 ``operation``。

    批次清单里的动作都带 operation；清单没有 actions 数组或找不到该动作（历史合成的单动作 run）时
    没有动作级操作名，返回 None，根因仍按父 run 最后事件编码。
    """

    if not isinstance(inner, Mapping):
        raise ReconcilerError(f"父动作 {action_id} 失败但 run 清单缺失，无法定位动作操作名")
    actions = inner.get("actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        if isinstance(action, Mapping) and action.get("action_id") == action_id:
            operation = action.get("operation")
            if not isinstance(operation, str) or not operation:
                raise ReconcilerError(f"父动作 {action_id} 的 operation 非法")
            return operation
    # 清单里没有该动作（历史合成的单动作 run：actions 为空、action_id 为 dispatch）：没有动作级操作名，
    # 根因退回父 run 最后事件编码；这不是放宽——清单本身缺失仍失败关闭。
    return None


def _staging_run_facts(
    run_dir: Path,
    state: Mapping[str, Any],
    stop_reason: str | None,
    action_diagnostic: Mapping[str, Any] | None,
    *,
    campaign_dir: Path | None = None,
) -> dict[str, Any] | None:
    """改造 4：按 state／stop reason 把取得执行权前的失败归入三类；非 staging run 返回 None。

    改造 5 M2 追加第四类（取得执行权之后）：``failed`` + ``parent-finalize-lost`` → 同名分类，根因复用
    ``supervisor-run.interrupted``（failed_step=parent-finalize），``next_action`` 同批次 N+1 逐字重派——
    单动作恢复段 run 已成功、绑定的段摘要当前字节一致且为成功终态，重派只会命中段 run 幂等返回（零请求）。

    - ``aborted_prepared`` + ``prepared-abandoned`` → ``parent-prepare-abandoned``，根因
      ``staging.abandoned``（stage=parent-run）；
    - ``aborted_prepared`` + ``staging-commit-failed:<step>`` → 同上分类；run 目录有提交步骤失败诊断（修好接着跑
      第 51 项起监督器封存前写入）时根因 ``staging.commit-step-failed``（维度 phase、stage、error_type、error_signature），
      没有诊断（第 51 项之前的监督器封存的历史 run，或诊断写入失败）时根因 ``staging.commit-failed``（stage=<step>，旧 ID 不变）；
    - ``failed`` + 有效 ``parent-start-failure.json`` → ``parent-start-failed``，根因
      ``parent-start.failed``（维度 phase）；
    - ``audit-incomplete`` + ``commit-integrity-mismatch`` → 同名分类，根因
      ``commit.integrity-mismatch``，判定固定永久停线。
    这四类都不读动作诊断；携带动作诊断的 run 不属于本分类。
    """

    binding = state.get("staging_binding")
    if not isinstance(binding, Mapping):
        return None
    if action_diagnostic is not None:
        return None
    run_state = state.get("state")
    facts: dict[str, Any] = {
        "staging_binding": dict(binding),
        "commit_classification": supervisor.classify_prepared_run(run_dir, state),
    }
    if run_state == "aborted_prepared":
        dimensions: dict[str, str]
        if stop_reason == supervisor.PREPARED_ABANDONED_REASON:
            stage = "parent-run"
            code = "staging.abandoned"
            dimensions = {"phase": str(state["phase"]), "stage": stage}
        elif isinstance(stop_reason, str) and stop_reason.startswith(
            supervisor.STAGING_COMMIT_FAILED_PREFIX
        ):
            stage = stop_reason[len(supervisor.STAGING_COMMIT_FAILED_PREFIX) :]
            if stage not in supervisor.STAGING_COMMIT_STEPS or stage == "commit-activate":
                raise ReconcilerError(f"aborted_prepared 的 stop reason 步骤非法：{stop_reason!r}")
            try:
                commit_failure = supervisor.read_staging_commit_failure(run_dir, state)
            except supervisor.SupervisorError as error:
                raise ReconcilerError(f"提交步骤失败诊断无法重放：{error}") from error
            if commit_failure is None:
                # 第 51 项之前的监督器封存的历史 run（或诊断写入失败）：按旧维度复算，旧 ID 逐字不变。
                code = "staging.commit-failed"
                dimensions = {"phase": str(state["phase"]), "stage": stage}
            else:
                if commit_failure["commit_step"] != stage:
                    raise ReconcilerError(
                        f"提交步骤失败诊断的步骤与 stop reason 不一致：{commit_failure['commit_step']!r} ≠ {stage!r}"
                    )
                code = "staging.commit-step-failed"
                dimensions = {
                    "phase": str(state["phase"]),
                    "stage": stage,
                    "error_type": staging_abort_error_type_dimension(str(commit_failure["error_type"])),
                    "error_signature": str(commit_failure["error_signature"]),
                }
                facts["staging_commit_failure"] = {
                    "schema_version": commit_failure["schema_version"],
                    "path": supervisor.STAGING_COMMIT_FAILURE_FILENAME,
                    "sha256": commit_failure["diagnostic_sha256"],
                    "commit_step": commit_failure["commit_step"],
                    "error_type": commit_failure["error_type"],
                    "error_signature": commit_failure["error_signature"],
                }
        else:
            raise ReconcilerError(f"aborted_prepared 父 run 的 stop reason 非法：{stop_reason!r}")
        if facts["commit_classification"] != "no_commit":
            raise ReconcilerError("aborted_prepared 父 run 不得拥有自己的 COMMIT")
        facts.update(
            {
                "failure_class": PARENT_PREPARE_ABANDONED_CLASS,
                "root_cause_component": "orchestrator",
                "root_cause_code": code,
                "failed_step": stage,
                "stable_dimensions": dimensions,
                "next_action": NEXT_ACTION_SAME_SEQUENCE,
            }
        )
        return facts
    if run_state == "failed" and stop_reason == supervisor.PARENT_START_FAILED_REASON:
        try:
            diagnostic = supervisor.read_parent_start_failure(run_dir, state)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"父启动失败诊断无法重放：{error}") from error
        if diagnostic is None:
            raise ReconcilerError("parent-start-failed 父 run 缺少 parent-start-failure 诊断")
        if facts["commit_classification"] != "committed":
            raise ReconcilerError("parent-start-failed 父 run 的 COMMIT 无效或缺失")
        commit = supervisor._read_vc_commit(Path(str(binding["commit_path"])))
        if diagnostic["commit_sha256"] != commit["commit_sha256"]:
            raise ReconcilerError("父启动失败诊断与 COMMIT 不一致")
        facts.update(
            {
                "failure_class": PARENT_START_FAILED_CLASS,
                "parent_start_failure": {
                    "schema_version": diagnostic["schema_version"],
                    "path": supervisor.PARENT_START_FAILURE_FILENAME,
                    "sha256": diagnostic["diagnostic_sha256"],
                    "failure_kind": diagnostic["failure_kind"],
                    "error_type": diagnostic["error_type"],
                },
                "commit_sha256": commit["commit_sha256"],
                "root_cause_component": "supervisor",
                "root_cause_code": "parent-start.failed",
                "failed_step": "commit-activate",
                "stable_dimensions": {"phase": str(state["phase"])},
                "next_action": NEXT_ACTION_SAME_BATCH,
            }
        )
        return facts
    if run_state == "failed" and stop_reason == supervisor.PARENT_FINALIZE_LOST_REASON:
        if facts["commit_classification"] != "committed":
            raise ReconcilerError("parent-finalize-lost 父 run 的 COMMIT 无效或缺失")
        orphan = supervisor.attempt_recovery_orphan_facts(run_dir, state, supervisor._run_inner_manifest(run_dir))
        if not orphan["complete"] or orphan["binding_mismatch"]:
            raise ReconcilerError(
                "parent-finalize-lost 父 run 的恢复段判定不成立：" + "、".join(orphan["reasons"])
            )
        if campaign_dir is None:
            raise ReconcilerError("parent-finalize-lost 对账必须绑定 Campaign 目录")
        try:
            output = supervisor.verify_attempt_recovery_orphan_output(campaign_dir, orphan)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"parent-finalize-lost 父 run 绑定的段摘要无法复算：{error}") from error
        facts.update(
            {
                "failure_class": PARENT_FINALIZE_LOST_CLASS,
                "attempt_recovery": {
                    "action_id": orphan["action_id"],
                    "operation": orphan["operation"],
                    "recovery_revision": orphan["recovery_revision"],
                    "binding_sha256": orphan["binding_sha256"],
                    "summary": output,
                },
                "root_cause_component": "reconciler",
                "root_cause_code": "supervisor-run.interrupted",
                "failed_step": "parent-finalize",
                "stable_dimensions": {"phase": str(state["phase"])},
                "next_action": NEXT_ACTION_SAME_BATCH,
            }
        )
        return facts
    if run_state == "audit-incomplete" and stop_reason == supervisor.COMMIT_INTEGRITY_MISMATCH_REASON:
        facts.update(
            {
                "failure_class": COMMIT_INTEGRITY_MISMATCH_CLASS,
                "root_cause_component": "supervisor",
                "root_cause_code": "commit.integrity-mismatch",
                "failed_step": "commit-verify",
                "stable_dimensions": {"phase": str(state["phase"])},
                "next_action": None,
            }
        )
        return facts
    # 其余 staging run（watchdog／监控异常等）沿用既有中断分类。
    return None


# 修好接着跑第 60 项：有诊断、无枚举观测、带归一化拒因签名的动作失败的根因码。
ACTION_ERROR_ROOT_CAUSE_CODE = "campaign-run.action-error"


def action_error_root_cause(
    phase: str,
    failed_step: str,
    *,
    failure_kind: str,
    error_type: str,
    error_signature: str,
) -> dict[str, Any]:
    """动作以异常失败（v4 诊断带归一化拒因签名）的根因 ``campaign-run.action-error``（修好接着跑第 60 项）。

    维度 phase、failure_kind、error_type、error_signature；failed_step 与 ``supervisor-run.interrupted`` 同源（失败动作的
    operation，冒号换成连字符）。同一动作的不同根本原因（异常类型或归一化原文不同）得到不同 ID、互不累计；同一原因只在
    路径、ID、时间戳、数字等波动片段上不同仍得同一 ID，上限保护不削弱。error_type 维度与 staging 中止同口径：异常类名
    原样，不是合法标识符时记 ``unrecognized``。
    """

    if failure_kind not in supervisor.ACTION_DIAGNOSTIC_SIGNED_FAILURE_KINDS:
        raise ReconcilerError(f"动作诊断的失败种类 {failure_kind!r} 不带归一化拒因签名，不能按 {ACTION_ERROR_ROOT_CAUSE_CODE} 编码")
    try:
        return root_cause.describe_root_cause(
            component=COMPONENT,
            stable_error_code=ACTION_ERROR_ROOT_CAUSE_CODE,
            failed_step=failed_step,
            stable_dimensions={
                "phase": phase,
                "failure_kind": failure_kind,
                "error_type": staging_abort_error_type_dimension(error_type),
                "error_signature": error_signature,
            },
        )
    except root_cause.RootCauseError as error:
        raise ReconcilerError(f"根因编码失败：{error}") from error


def _supervisor_run_failures(
    run: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """把动作诊断的每个枚举观测映射为独立、跨 Campaign 稳定根因。

    历史 v1/v2 诊断没有枚举观测，仍保守重放为原来的单一
    ``supervisor-run.interrupted``；新 v3 诊断不得再按错误正文或最后操作猜测。
    改造 4 的四类父 run 失败没有动作诊断，根因直接由分类事实生成。

    修好接着跑第 60 项：有诊断、无枚举观测的动作失败，诊断带归一化拒因签名（v4 诊断的 handled-error／unexpected-error，
    由写诊断的监督器从同一异常的完整原文生成）时按 ``campaign-run.action-error`` 细分（:func:`action_error_root_cause`）；
    没有签名的（v1～v3 历史诊断，以及 v4 的 interrupted／child-returncode）照旧 ``supervisor-run.interrupted``，维度与旧 ID
    逐字不变。签名只取自诊断文件本身，绝不从 message 复算——历史诊断因此不会被重算出新 ID。
    """

    staging = run.get("staging")
    if isinstance(staging, Mapping) and staging.get("failure_class") in STAGING_FAILURE_CLASSES:
        try:
            cause = root_cause.describe_root_cause(
                component=str(staging["root_cause_component"]),
                stable_error_code=str(staging["root_cause_code"]),
                failed_step=str(staging["failed_step"]),
                stable_dimensions=dict(staging["stable_dimensions"]),
            )
        except root_cause.RootCauseError as error:
            raise ReconcilerError(f"根因编码失败：{error}") from error
        return [], [cause]
    raw_observations = run.get("failure_observations", [])
    if not isinstance(raw_observations, list):
        raise ReconcilerError("父动作 failure_observations 不是数组")
    if not raw_observations:
        # 动作失败（有诊断、无枚举观测，如子进程非零退出／被杀）的稳定步骤是失败动作的
        # operation：父 run 最后事件恒为 supervisor-stop，会把 VC-5:assert 与 VC-5:accept 的失败
        # 编成同一根因，逐字重派后另一动作失败即被误判为同根因第二次而停线（M2-G0 真机暴露）。
        # 非动作失败（owner-loss／中断）仍按父 run 最后事件编码。
        # 修好接着跑第 60 项：只有 operation 还不够——同一动作操作（如 VC-5:candidate-recovery 的预览与补跑）先后因两个完全
        # 不同的原因失败会编成同一根因，第二次即达上限、总账暂停（194249z 项目总账第 287／300 条）。诊断带归一化拒因签名时
        # 再按失败种类、异常类型与签名细分；没有签名时维持原编码。
        diagnostic = run.get("action_diagnostic")
        operation = diagnostic.get("operation") if isinstance(diagnostic, Mapping) else None
        step_source = operation if isinstance(operation, str) and operation else run["last_operation"]
        signature = diagnostic.get("error_signature") if isinstance(diagnostic, Mapping) else None
        try:
            failed_step = str(step_source).replace(":", "-")[:128]
            if signature is not None:
                cause = action_error_root_cause(
                    str(run["phase"]),
                    failed_step,
                    failure_kind=str(diagnostic["failure_kind"]),
                    error_type=str(diagnostic["error_type"]),
                    error_signature=str(signature),
                )
            else:
                cause = root_cause.describe_root_cause(
                    component=COMPONENT,
                    stable_error_code="supervisor-run.interrupted",
                    failed_step=failed_step,
                    stable_dimensions={"phase": str(run["phase"])},
                )
        except (KeyError, root_cause.RootCauseError) as error:
            raise ReconcilerError(f"根因编码失败：{error}") from error
        return [], [cause]

    observations: list[dict[str, str]] = []
    causes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_observations):
        if not isinstance(item, Mapping) or set(item) != {
            "check_id",
            "failure_code",
        }:
            raise ReconcilerError(
                f"父动作 failure_observations[{index}] 字段不闭合"
            )
        check_id = item.get("check_id")
        failure_code = item.get("failure_code")
        if (
            not isinstance(check_id, str)
            or not check_id
            or not isinstance(failure_code, str)
            or not failure_code
        ):
            raise ReconcilerError(
                f"父动作 failure_observations[{index}] 身份非法"
            )
        key = (check_id, failure_code)
        if key in seen:
            continue
        seen.add(key)
        observation = {"check_id": check_id, "failure_code": failure_code}
        failed_step = "failure-observation-" + _fingerprint(observation)[:20]
        try:
            cause = root_cause.describe_root_cause(
                component="supervisor",
                stable_error_code="campaign-run.action-failed",
                failed_step=failed_step,
                stable_dimensions={"phase": str(run["phase"])},
            )
        except root_cause.RootCauseError as error:
            raise ReconcilerError(f"根因编码失败：{error}") from error
        observations.append(
            {**observation, "root_cause_id": str(cause["root_cause_id"])}
        )
        causes.append(
            {
                **cause,
                "check_id": check_id,
                "failure_code": failure_code,
            }
        )
    if not causes:
        raise ReconcilerError("父动作枚举观测没有生成任何根因")
    return observations, causes


def _finalize_orphaned_prepared_run(run_dir: Path) -> dict[str, Any] | None:
    """改造 4：prepared（或 committed 未启动）且 owner 已丢失的父 run，先按三分类封存终态。

    monitor 在线时它自己会封存；这里只覆盖 monitor 也已不在的孤儿。owner 仍在线时
    不动，由 ``_run_facts`` 拒绝对账。已终态的 run 直接返回 ``None``。
    """

    try:
        state = supervisor._read_state(run_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"监督器 run 目录无法读取：{error}") from error
    if state.get("state") not in supervisor.ACTIVE_STATES:
        return None
    if state.get("staging_binding") is None:
        return None
    if supervisor._owner_alive(int(state["owner_pid"])):
        return None
    if state.get("state") != supervisor.PREPARED_STATE and not supervisor.committed_run_never_started(
        run_dir, state
    ):
        return None
    monitor_pid = state.get("monitor_pid")
    if isinstance(monitor_pid, int) and not isinstance(monitor_pid, bool) and supervisor._owner_alive(monitor_pid):
        raise ReconcilerError("父 run 的 monitor 仍在线，等待其封存终态后再对账")
    try:
        return supervisor.finalize_prepared_run(run_dir, operation="reconciler:finalize-prepared")
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"prepared 父 run 终态化失败：{error}") from error


def _backfill_orphaned_action_failure(
    run_dir: Path,
    campaign_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any] | None:
    """改造 5（R2）前置锁段：owner 丢失后由 monitor 封存的 ``failed／action-failed:<id>`` run。

    Campaign 锁内：① 复算 ``evaluation_orphan_facts`` 并要求与 stop-receipt 的
    ``action_outputs_sha256`` 一致（不一致即失败关闭、不入账）；② 缺失 post-run-tooling 收据时
    以同一 ``post_run_tooling_facts`` 复算并 write-once 补写；解锁后由既有只读 ``_run_facts``
    重新生成事实。非 action-failed 终态或 owner 仍在线的 run 直接返回 None。
    """

    state = supervisor._read_state(run_dir)
    if state.get("state") != "failed":
        return None
    stop_path = run_dir / "stop-receipt.json"
    if stop_path.is_symlink() or not stop_path.is_file():
        return None
    try:
        stop = supervisor.read_stop_receipt(run_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"stop-receipt 无法校验：{error}") from error
    reason = stop.get("reason")
    if not isinstance(reason, str) or not reason.startswith("action-failed:"):
        return None
    if supervisor._owner_alive(int(state["owner_pid"])):
        return None
    # 只有 monitor 的 R2 确定性封存才留下 operation=supervisor:owner-check 的 failed 事件；
    # owner 自己经 stop-request 封存的 run（operation=supervisor:stop）与历史夹具沿用既有对账。
    try:
        events = supervisor.load_events(run_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"父 run 事件账本无法重放：{error}") from error
    if not any(
        event.get("event_type") == "failed" and event.get("operation") == "supervisor:owner-check"
        for event in events
    ):
        return None
    manifest_path = run_dir / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    record = _read_json(manifest_path, "campaign-run 清单")
    inner = record.get("manifest")
    if not isinstance(inner, Mapping):
        return None
    with codex_upgrade._campaign_lock(campaign_dir):
        try:
            orphan = supervisor.evaluation_orphan_facts(run_dir, state, inner)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"owner 丢失 run 的失败身份无法复算：{error}") from error
        if not orphan["complete"] or orphan["binding_mismatch"]:
            raise ReconcilerError(
                "failed／action-failed 终态的父 run 失败身份不完整或动作输出绑定不一致："
                + "、".join(orphan["reasons"])
            )
        if orphan["action_outputs_sha256"] != stop.get("action_outputs_sha256"):
            raise ReconcilerError(
                "stop-receipt 的 action_outputs_sha256 与当前动作输出绑定复算结果不一致"
            )
        action_id = str(orphan["action_id"])
        diagnostic_path = run_dir / "action-diagnostics" / f"action-{action_id}-failure.json"
        try:
            diagnostic = supervisor._validate_action_diagnostic(
                diagnostic_path,
                run_dir=run_dir,
                campaign_id=str(manifest["campaign_id"]),
                phase=str(state["phase"]),
                action_id=action_id,
                owner_pid=int(state["owner_pid"]),
                owner_nonce=str(state["owner_nonce"]),
            )
            receipt, backfilled = supervisor.ensure_post_run_tooling_receipt(
                run_dir,
                diagnostic,
                campaign_dir=campaign_dir,
                inner_manifest=inner,
                campaign_id=str(manifest["campaign_id"]),
                phase=str(state["phase"]),
                action_id=action_id,
                owner_pid=int(state["owner_pid"]),
                owner_nonce=str(state["owner_nonce"]),
                run_started_at_utc=str(state.get("started_at_utc", "")),
                owner_alive=False,
            )
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"post-run-tooling 收据无法复算或补写：{error}") from error
    return {
        "orphan_facts": {
            "action_id": action_id,
            "action_outputs_sha256": orphan["action_outputs_sha256"],
        },
        "backfilled": backfilled,
        "post_run_tooling_receipt_sha256": receipt["receipt_sha256"] if receipt is not None else None,
    }


# 第 50 项：父监督器收账当时被暂停的痕迹。父进程自己封存的失败 run（owner 在线时收账）遇账本 stop_required 只暂停、不写
# 任何事件（第 46 项），遇预算到期先登记预算暂停、不写路由事件；两者之后都要人工处理——campaign-resume 清零根因（账本写
# recovery_verified）、deadline-extend 延期（deadline_paused／deadline_extended）。补做收口只针对 run 开始之后账本出现过这类
# 事件的父 run；其余父进程自己封存的 run 视为收账已按当时账本完成（夹具与历史现场不受影响）。
_CLOSEOUT_PAUSE_FOOTPRINT_EVENTS = frozenset({"recovery_verified", "deadline_paused", "deadline_extended"})


def _closeout_paused_after(ledger_dir: Path, started_at_epoch: float) -> bool:
    """账本在父 run 开始之后出现过收账暂停的痕迹（campaign-resume 的恢复登记、预算暂停或延期）。"""

    started = datetime.fromtimestamp(float(started_at_epoch), tz=timezone.utc)
    try:
        events = timing_ledger._load_events(ledger_dir)
    except (OSError, timing_ledger.TimingLedgerError) as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    for event, _raw in events:
        if event.get("event_type") not in _CLOSEOUT_PAUSE_FOOTPRINT_EVENTS:
            continue
        try:
            recorded = _timestamp(event.get("recorded_at_utc"), "账本事件 recorded_at_utc")
        except ReconcilerError:
            continue
        if recorded >= started:
            return True
    return False


def _owner_check_sealed(run_dir: Path) -> bool:
    """monitor 按 R2 确定性封存留下的 ``supervisor:owner-check`` failed 事件；父进程自己封存的 run 没有它。"""

    try:
        events = supervisor.load_events(run_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"父 run 事件账本无法重放：{error}") from error
    return any(event.get("event_type") == "failed" and event.get("operation") == "supervisor:owner-check" for event in events)


def _latest_commit_sequence(campaign_dir: Path, campaign_id: Any) -> int:
    """本 Campaign 已登记 COMMIT 的最大序号（没有 COMMIT 为 0）。"""

    commits_root = campaign_dir / "control" / "vc" / "commits"
    if commits_root.is_symlink():
        raise ReconcilerError("COMMIT 目录不得是符号链接")
    latest = 0
    for path in sorted(commits_root.iterdir()) if commits_root.is_dir() else []:
        if codex_upgrade._VC_SEQUENCE_FILE_RE.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
            raise ReconcilerError(f"COMMIT 目录含非法条目：{path.name}")
        try:
            commit = supervisor._read_vc_commit(path)
        except vc_artifacts.VCArtifactError as error:
            raise ReconcilerError(f"COMMIT 无法校验：{path.name}：{error}") from error
        if commit["campaign_id"] == campaign_id:
            latest = max(latest, int(commit["sequence"]))
    return latest


# 第 39 项：阶段审核阶段（VC-1～VC-3）。这些阶段的非可恢复动作失败由父监督器收账为阶段审核（永久类停线），
# 阶段幂等重派证明只在账本处于阶段审核态时才写，所以收账缺失时后继协议全部无路。
# 第 44 项：候选级阶段（VC-4～VC-6）同构——非可恢复动作失败由父监督器收账为候选审核（VC-4 的 R18 幂等重派证明也只在
# 候选审核态才写；VC-5 候选采集续跑链与 VC-5／VC-6 零请求后处理的 execution-failure 收为 recovery_required），收账缺失时
# 对账同样按 active 提示"重新派发同一批次"，而后继协议拒绝重派与续跑预览。VC-0 不经 campaign-run 动作失败收账，不在此列。
ORPHANED_CLOSEOUT_BACKFILL_PHASES = frozenset({"VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6"})


def _backfill_orphaned_failure_closeout(
    run_dir: Path,
    campaign_dir: Path,
    run: Mapping[str, Any],
    orphan_backfill: Mapping[str, Any] | None,
    ledger_dir: Path,
) -> dict[str, Any] | None:
    """第 39 项：R2 确定性封存的父 run（owner 在账本收口前丢失）补做父监督器本应完成的失败收账。

    monitor 的 R2 只封存 run-local 终态（``failed／action-failed:<id>``），按设计不写 Campaign 账本；前置锁段
    ``_backfill_orphaned_action_failure`` 只补 post-run-tooling 收据，也不收账。VC-1～VC-3 的非可恢复失败因此让
    账本停在 active：对账按 active 写 receipt_passed、指向"重新派发同一批次"，却永远不写阶段幂等重派证明，
    阶段审核协议不可达、其余协议都不认 execution-failure——死路。这里以父监督器同一收账函数
    （``_close_failed_campaign_timing_ledger``，幂等、可从 stage_abandoned 续作）补齐 owner 本应写下的
    stage_abandoned＋stage_review_required（根因已达上限或账本已停线时它照旧停线，预算到期照旧暂停），随后
    沿用既有阶段审核对账：动作幂等合同成立才写证明并由阶段审核协议唯一承接 N+1 重派，不成立留在审核。

    只在同时满足时补做：前置锁段确认是 R2 封存且 owner 已死（``orphan_backfill`` 非 None）；阶段属
    VC-1～VC-3（第 44 项起含候选级阶段 VC-4～VC-6，收账函数按阶段路由为候选审核或 recovery_required）；有效失败类不在
    可恢复集合（它们的 receipt_passed 与逐字重派协议原本可走）；账本里还没有本次失败的审核、恢复、停线事件或对账
    许可，且账本不是终态（stopped／complete／abandoned）。已收口（含对账后已重开的阶段）一律不动，避免对同一次失败
    重复放弃阶段。

    第 38 项：永久失败类同样补做。此前永久类不补——``_decide`` 不看失败分类，永久失败类（完整性类以外）也被判
    recoverable、写 reconcile-run-passed 并提示"重新派发同一批次"，而后继协议全部拒绝；预算到期时还先暂停、指向延期，
    延期后照样落到这条死路。现在以同一收账函数停线（stage_abandoned＋stop_the_line，永久类不走预算暂停），对账随后
    按账本停线永久停线——与 owner 在线时父监督器自己收账、以及后继协议对永久类的拒绝一致。可恢复类仍不补：对账判定
    recoverable、许可逐字重派，与后继协议本就一致，预算到期、请求预算与根因上限都由对账判定暂停（根因上限按 B3-9
    暂停待修复）；第 46 项起收账对根因上限同样只暂停，补与不补结果相同，保持不补以免改动既有路径。

    第 39 项剩余形态（草表 D-07 口径）：看门狗中止（``watchdog-aborted``）且留有唯一动作诊断、owner 已丢失时同样
    补做——动作子进程写出诊断之后、父进程追加 action-failed 之前丢失，R2 判定不成立（缺 action-failed 生命周期
    事实），但诊断给出了失败动作与失败分类，与后继协议、入口 0-W 用的是同一份事实。可信性按
    ``supervisor.watchdog_action_failure_facts`` 核对（诊断唯一可重放、动作属批次清单、动作输出绑定未漂移），
    不可信即失败关闭、不补账；没有诊断的看门狗中止不补（它没有失败分类，照旧按 legacy-interruption 对账）。

    第 50 项：父进程自己封存的 failed 终态（owner 在线时父监督器收账）同样补做，但只在收账当时被暂停的情形——账本在 run
    开始之后出现过 campaign-resume 的恢复登记或预算暂停／延期（``_closeout_paused_after``）：收账遇 stop_required 只暂停、
    不写事件（第 46 项），遇预算到期只登记预算暂停，清零根因或批准延期后没有任何人再收口，账本停在 active——阶段审核类
    拿不到审核与幂等证明、永久类判 recoverable。补做前还要求账本（去掉预算暂停）是 active 或 stop_required 且当前阶段就是
    父 run 的阶段、父 run 的批次仍是本 Campaign 最新已提交的批次（此后没有更高序号的 COMMIT）：已经推进的现场不改写；
    stop_required 期间补账由收账函数照旧只暂停、不写事件，清零后重新对账即完成路由。
    """

    if run.get("phase") not in ORPHANED_CLOSEOUT_BACKFILL_PHASES:
        return None
    # 第 38 项：只有可恢复类不补（对账判定与逐字重派协议本就一致）；审核类与永久失败类都补，由收账函数按分类路由。
    failure_class = str(run.get("failure_class"))
    if (
        failure_class in supervisor.RECOVERABLE_ACTION_FAILURE_CLASSES
        or failure_class in supervisor.RECOVERABLE_PARENT_FAILURE_CLASSES
    ):
        return None
    if run.get("state") == "watchdog-aborted":
        if (
            run.get("owner_alive")
            or run.get("staging") is not None
            or not isinstance(run.get("action_diagnostic"), Mapping)
        ):
            return None
        try:
            watchdog_failure = supervisor.watchdog_action_failure_facts(
                supervisor._read_state(run_dir), run_dir, campaign_dir, prior_manifest=None, label="对账补做失败收账"
            )
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"看门狗中止父 run 的动作失败事实不可信，不能补做失败收账：{error}") from error
        if watchdog_failure is None:
            return None
        failed_action_id = str(watchdog_failure["action_id"])
    elif orphan_backfill is not None:
        failed_action_id = str(orphan_backfill["orphan_facts"]["action_id"])
    elif (
        run.get("state") == "failed"
        and not run.get("owner_alive")
        and run.get("staging") is None
        and not _owner_check_sealed(run_dir)
        and _closeout_paused_after(ledger_dir, float(supervisor._read_state(run_dir).get("started_at_epoch", 0.0)))
    ):
        # 第 50 项：父进程自己封存、收账当时被暂停。失败动作取父监督器收账同一失败事实；再核对账本仍停在这次失败的阶段、
        # 父 run 的批次仍是最新已提交批次。
        try:
            paused_facts = supervisor.campaign_run_failure_facts(run_dir, campaign_dir=campaign_dir)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"父 run 的失败事实不可信：{error}") from error
        if paused_facts is None:
            return None
        try:
            summary = timing_ledger.inspect_ledger(ledger_dir)
        except (OSError, timing_ledger.TimingLedgerError) as error:
            raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
        frontier_status = summary.get("status_before_pause") if summary.get("status") == "deadline_paused" else summary.get("status")
        if frontier_status not in {"active", "stop_required"} or summary.get("active_phase") != run.get("phase"):
            return None
        if _latest_commit_sequence(campaign_dir, paused_facts["campaign_id"]) > int(paused_facts["batch_sequence"]):
            return None
        failed_action_id = str(paused_facts["action_id"])
    else:
        return None
    try:
        facts = supervisor.campaign_run_failure_facts(run_dir, campaign_dir=campaign_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"父 run 的失败事实不可信：{error}") from error
    if facts is None or facts["action_id"] != failed_action_id:
        raise ReconcilerError("父 run 的失败动作与孤儿失败身份不一致，不能补做失败收账")
    prefix = f"{supervisor.CANDIDATE_REVIEW_EVENT_PREFIX}{facts['failure_digest'][:supervisor.FAILURE_DIGEST_PREFIX_LENGTH]}"
    settled_ids = {
        f"{prefix}-stage-review-required",
        f"{prefix}-recovery-required",
        f"{prefix}-stop-the-line",
        supervisor.candidate_review_event_id(str(facts["failure_digest"])),
        f"reconcile-run-passed-{run['run_id']}",
    }
    try:
        events = [event for event, _raw in timing_ledger._load_events(ledger_dir)]
    except (OSError, timing_ledger.TimingLedgerError) as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    if any(event.get("event_id") in settled_ids for event in events):
        return None
    # 第 38 项：账本已是终态（历史对账已停线、显式 close-campaign-ledger、放弃或完成）时收账无事可做——父监督器收账
    # 只把 active 阶段路由为恢复、审核或停线。这里不补，由对账按终态判定（与补账前相同）；否则历史上已由对账自拟
    # 停线的完整性类失败再次对账时，收账会以"已由其他根因停线"拒绝，对账无法幂等重放。
    try:
        ledger_status = timing_ledger.inspect_ledger(ledger_dir).get("status")
    except (OSError, timing_ledger.TimingLedgerError) as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    if ledger_status in {"stopped", "complete", "abandoned"}:
        return None
    inner = supervisor._read_json(Path(run_dir) / "campaign-run-manifest.json").get("manifest")
    if not isinstance(inner, Mapping):
        raise ReconcilerError("R2 封存父 run 缺少 campaign-run 清单，不能补做失败收账")
    try:
        closeout = supervisor._close_failed_campaign_timing_ledger(
            campaign_dir,
            inner,
            failed_action_id=str(facts["action_id"]),
            failure_class=str(facts["failure_class"]),
        )
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"R2 封存父 run 的失败收账补做失败：{error}") from error
    return {
        "action_id": str(facts["action_id"]),
        "failure_class": str(facts["failure_class"]),
        "ledger_status": closeout.get("ledger_status"),
        "idempotent": bool(closeout.get("idempotent")),
    }


# 第 45 项：有预约的父 run 来不及收账（有预约路径）。attempt 所属的 VC 阶段：官方 VC-1，候选与恢复段 VC-5。
_ATTEMPT_OWNER_VC_PHASES = {"official": "VC-1", "candidate": "VC-5"}
# 失败登记之后账本里仍可出现、却不代表续跑已推进的事件：预算暂停／延期与 campaign-resume 的恢复登记。
_ATTEMPT_BACKFILL_TOLERATED_EVENTS = frozenset({"deadline_paused", "deadline_extended", "recovery_verified"})


def _command_flag(command: Any, flag: str) -> str | None:
    """动作命令里 ``flag <值>`` 或 ``flag=<值>`` 的值；没有或形态不对返回 None。"""

    if not isinstance(command, list):
        return None
    for index, token in enumerate(command):
        if token == flag and index + 1 < len(command) and isinstance(command[index + 1], str):
            return command[index + 1]
        if isinstance(token, str) and token.startswith(f"{flag}="):
            return token.partition("=")[2]
    return None


def _attempt_reserving_action(
    inner: Mapping[str, Any],
    action_id: str,
    *,
    phase: str,
    candidate_id: str | None,
    recovery_revision: str | None,
) -> bool:
    """父 run 的失败动作是否正是发布这类预约的动作：恢复段 run（``--attempt-recovery ar<k>``）发布段预约；候选采集
    （``capture-candidate run``）与按预览补跑发布候选 attempt 预约；官方采集与按预览补跑发布官方 attempt 预约。候选与
    恢复段还要求动作命令的 ``--candidate-id`` 就是 attempt 所属候选。零请求预览不发布预约，不在其列。"""

    command = next(
        (
            action.get("command")
            for action in inner.get("actions", []) or []
            if isinstance(action, Mapping) and action.get("action_id") == action_id
        ),
        None,
    )
    segment = supervisor._attempt_recovery_run_revision(inner, action_id)
    if recovery_revision is not None:
        return segment == recovery_revision and _command_flag(command, "--candidate-id") == candidate_id
    if phase == "candidate":
        return (
            segment is None
            and supervisor.candidate_capture_recovery_action(inner, action_id) in {"capture", "run"}
            and _command_flag(command, "--candidate-id") == candidate_id
        )
    return action_id in {"capture-official", supervisor._OFFICIAL_RECOVERY_RUN_ACTION_ID}


def _first_batch_owner_runs(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    reserved: float,
    control_root: Path | None,
) -> list[tuple[Path, dict[str, Any]]]:
    """第 47 项：首批 VC-1 批次（序号 1）的父 run 候选——窗口包住预约时刻、队列清单等于 Campaign 绑定的首批清单。

    首批由 VC-0 收口以 ``_campaign_run_command`` 直接派发，没有 staging／COMMIT，``--supervisor-state-dir`` 只记在收口审计
    目录里、不在 Campaign 内。这里按 ``vc_control.first_campaign_run_manifest``（路径＋摘要，派发门禁 ``_validate_first_batch_binding``
    同一绑定）取首批清单，在控制根（``--control-root``，缺省 ``<宿主数据根>/control``）本身及其一级子目录（约定的
    ``<campaign_id>-supervisor``、驱动的 VC_STATE_DIR 等）里找 ``run-*``：同一 Campaign、队列清单与首批清单逐字相同
    （含清单摘要）、窗口（开始到终态时刻）包住预约时刻。绑定缺失或漂移、控制根不在规范宿主布局下时返回空（与修复前
    相同：定位不到、不补账）；首批清单含批次序号与 Campaign 身份，别的批次不会逐字相同。
    """

    campaign = _read_json(campaign_dir / "campaign.json", "Campaign 清单")
    vc_control = campaign.get("vc_control")
    binding = vc_control.get("first_campaign_run_manifest") if isinstance(vc_control, Mapping) else None
    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str) or not binding.get("path"):
        return []
    first_path = campaign_dir / str(binding["path"])
    if first_path.is_symlink() or not first_path.is_file() or _file_sha256(first_path) != binding.get("sha256"):
        return []
    try:
        first_manifest = supervisor._campaign_run_manifest(first_path)
    except supervisor.SupervisorError:
        return []
    if first_manifest.get("campaign_id") != manifest.get("campaign_id") or first_manifest.get("phase") != "VC-1":
        return []
    first_sha256 = supervisor._sha256(supervisor._canonical(first_manifest))
    try:
        root = _control_root(campaign_dir, control_root)
    except (closeout.VC0CloseoutError, OSError):
        return []
    if root.is_symlink() or not root.is_dir():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for state_path in [*sorted(root.glob("run-*/state.json")), *sorted(root.glob("*/run-*/state.json"))]:
        run_dir = state_path.parent
        if run_dir.is_symlink() or run_dir.parent.is_symlink() or state_path.is_symlink():
            continue
        try:
            state = supervisor._read_state(run_dir)
        except supervisor.SupervisorError:
            continue
        if state.get("campaign_id") != manifest.get("campaign_id"):
            continue
        record_path = run_dir / "campaign-run-manifest.json"
        if record_path.is_symlink() or not record_path.is_file():
            continue
        try:
            record = _read_json(record_path, "campaign-run 清单")
        except ReconcilerError:
            continue
        if record.get("manifest") != first_manifest or record.get("manifest_sha256") != first_sha256:
            continue
        started, terminal = state.get("started_at_epoch"), state.get("terminal_at_epoch")
        if (
            isinstance(started, (int, float))
            and isinstance(terminal, (int, float))
            and not isinstance(started, bool)
            and not isinstance(terminal, bool)
            and float(started) <= reserved <= float(terminal)
        ):
            found.append((run_dir, state))
    return found


def _attempt_owner_run(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
    recovery_revision: str | None,
    reservation: Mapping[str, Any],
    control_root: Path | None = None,
) -> dict[str, Any] | None:
    """第 45 项：定位发布本预约、又在失败收账前丢失 owner 的父 campaign-run；找不到返回 None。

    沿正式 COMMIT（``control/vc/commits``，与 invalidate-candidate 定位失败父 run 同一索引）取父 run：COMMIT 属本
    Campaign 与 attempt 所属的 VC 阶段、父 run 窗口（开始到终态时刻）包住预约时刻；窗口互不重叠，落在多个窗口即失败
    关闭。再要求：父 run 终态为 failed／watchdog-aborted 且 owner 已退出；COMMIT 与父 run 的 staging 绑定互相一致；失败
    动作（``campaign_run_failure_facts``，与父监督器收账同一失败摘要）正是发布这类预约的动作；并且是 owner 在失败收账前
    丢失的两种形态之一——monitor 按 R2 确定性封存的 failed（supervisor:owner-check），或看门狗中止且留有可信的动作
    诊断（``watchdog_action_failure_facts``，与入口 0-W 同一核对）。owner 自己收口的 run 原本不在此列；第 50 项起收账没能
    落地的同样纳入：收账当时被暂停（run 开始之后账本出现过 campaign-resume 的恢复登记或预算暂停／延期，
    ``_closeout_paused_after``——收账遇 stop_required 只暂停、不写事件，遇预算到期只登记预算暂停，清零或延期之后没有人再收口），
    以及恢复段 run 以不按段失败收口的类别失败（父监督器收账时段仍 active，stage_abandoned 必然被账本拒绝）。

    第 47 项：首批序号 1 由 VC-0 绑定在 campaign.json、没有 COMMIT 文件——没有 COMMIT 包住预约时刻的官方 attempt，改按
    ``_first_batch_owner_runs`` 在控制根下的监督器状态目录里定位首批父 run（队列清单须与 Campaign 绑定的首批清单逐字
    相同，并按派发门禁同一函数复核首批绑定、且没有 staging 绑定），其余核对与 COMMIT 定位的父 run 相同。此前定位不到、
    不补账：永久失败类仍判 recoverable 并提示续跑，而后继协议拒绝。legacy 批次仍定位不到（返回 None，与修复前相同）。
    """

    vc_phase = _ATTEMPT_OWNER_VC_PHASES.get(phase)
    if vc_phase is None:
        return None
    try:
        reserved = _timestamp(reservation.get("started_at_utc"), "预约 started_at_utc").timestamp()
    except ReconcilerError:
        return None
    commits_root = campaign_dir / "control" / "vc" / "commits"
    if commits_root.is_symlink():
        raise ReconcilerError("COMMIT 目录不得是符号链接")
    sequences: list[int] = []
    matches: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    for path in sorted(commits_root.iterdir()) if commits_root.is_dir() else []:
        if codex_upgrade._VC_SEQUENCE_FILE_RE.fullmatch(path.name) is None or path.is_symlink() or not path.is_file():
            raise ReconcilerError(f"COMMIT 目录含非法条目：{path.name}")
        try:
            commit = supervisor._read_vc_commit(path)
        except vc_artifacts.VCArtifactError as error:
            raise ReconcilerError(f"COMMIT 无法校验：{path.name}：{error}") from error
        if commit["campaign_id"] != manifest.get("campaign_id"):
            continue
        sequences.append(int(commit["sequence"]))
        if commit["phase"] != vc_phase:
            continue
        run_dir = Path(str(commit["parent_run_dir"]))
        if not run_dir.is_absolute() or run_dir.is_symlink() or not run_dir.is_dir():
            continue
        try:
            state = supervisor._read_state(run_dir)
        except supervisor.SupervisorError as error:
            raise ReconcilerError(f"COMMIT {path.name} 的父 run 状态不可信：{error}") from error
        started, terminal = state.get("started_at_epoch"), state.get("terminal_at_epoch")
        if (
            isinstance(started, (int, float))
            and isinstance(terminal, (int, float))
            and not isinstance(started, bool)
            and not isinstance(terminal, bool)
            and float(started) <= reserved <= float(terminal)
        ):
            matches.append((commit, run_dir, state))
    commit: dict[str, Any] | None
    if not matches:
        # 第 47 项：没有 COMMIT 包住预约时刻——官方 attempt 可能是 VC-0 收口派发的首批（序号 1）发布的。
        if vc_phase != "VC-1" or recovery_revision is not None:
            return None
        first_runs = _first_batch_owner_runs(campaign_dir, manifest, reserved=reserved, control_root=control_root)
        if not first_runs:
            return None
        if len(first_runs) > 1:
            raise ReconcilerError(
                "预约时刻落在多个首批父 run 的窗口内，无法唯一定位发布它的父 run：" + "、".join(run.name for run, _s in first_runs)
            )
        commit = None
        run_dir, state = first_runs[0]
        sequence = 1
    elif len(matches) > 1:
        raise ReconcilerError(
            "预约时刻落在多个父 run 的窗口内，无法唯一定位发布它的父 run：" + "、".join(run.name for _c, run, _s in matches)
        )
    else:
        commit, run_dir, state = matches[0]
        sequence = int(commit["sequence"])
    if state.get("state") not in {"failed", "watchdog-aborted"} or supervisor._owner_alive(int(state["owner_pid"])):
        return None
    manifest_path = run_dir / "campaign-run-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    inner = _read_json(manifest_path, "campaign-run 清单").get("manifest")
    if not isinstance(inner, Mapping):
        return None
    try:
        if commit is None:
            # 首批：派发门禁同一函数复核 Campaign 绑定的首批清单；首批不经 staging，不得带 staging 绑定。
            supervisor._validate_first_batch_binding(campaign_dir, inner)
            if state.get("staging_binding") is not None:
                raise ReconcilerError(f"首批父 run {run_dir.name} 不应带 staging 绑定")
        elif supervisor._staging_commit_for_run(campaign_dir, state, inner, run_dir) != commit:
            raise ReconcilerError(f"父 run {run_dir.name} 的 staging 绑定与定位它的 COMMIT 不一致")
        facts = supervisor.campaign_run_failure_facts(run_dir, campaign_dir=campaign_dir)
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"预约所属父 run {run_dir.name} 的失败事实不可信：{error}") from error
    if facts is None:
        return None
    action_id = str(facts["action_id"])
    if not _attempt_reserving_action(
        inner, action_id, phase=phase, candidate_id=candidate_id, recovery_revision=recovery_revision
    ):
        return None
    try:
        if state.get("state") == "failed":
            stop = supervisor.read_stop_receipt(run_dir)
            if _owner_check_sealed(run_dir):
                sealed = stop.get("reason") == f"action-failed:{action_id}"
            else:
                # 第 50 项：父进程自己封存（owner 在线时父监督器收账），只在收账没能落地的两种情形补账（是否仍停在这次失败
                # 现场由调用方的账本守卫判定）：
                # · 收账当时被暂停——run 开始之后账本出现过 campaign-resume 的恢复登记或预算暂停／延期；
                # · 恢复段 run 以不按段失败收口的类别失败（永久失败类等）——父监督器收账时段仍 active，stage_abandoned 必然被
                #   账本拒绝、收账失败；段对账把段登记为失败之后才能落地（第 48 项遗留）。
                segment_unlanded = recovery_revision is not None and supervisor._attempt_recovery_segment_failure(
                    inner, action_id, str(facts["failure_class"]), segment_reserved=True
                ) is None
                sealed = stop.get("event_type") == "failed" and (
                    segment_unlanded
                    or _closeout_paused_after(_campaign_ledger_dir(manifest), float(state.get("started_at_epoch", 0.0)))
                )
            if not sealed:
                return None
        else:
            watchdog = supervisor.watchdog_action_failure_facts(
                state, run_dir, campaign_dir, prior_manifest=inner, label="有预约的失败收账补做"
            )
            if watchdog is None or str(watchdog["action_id"]) != action_id:
                return None
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"预约所属父 run {run_dir.name} 的动作失败事实不可信，不能补做失败收账：{error}") from error
    return {
        "run_dir": run_dir,
        "inner": inner,
        "facts": facts,
        "sequence": sequence,
        # 首批之后若已有任何 COMMIT（序号 ≥ 2 的续跑批次已派发），下面的补账守卫据此不补。
        "latest_sequence": max([sequence, *sequences]),
    }


def _newer_reservation_exists(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    reserved_epoch: float,
) -> bool:
    """同一阶段（候选按同一候选）是否有晚于本预约发布的 attempt 预约或恢复段预约——有就说明已经续跑。"""

    def later(path: Path, label: str) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            return _timestamp(_read_json(path, label).get("started_at_utc"), f"{label}.started_at_utc").timestamp() > reserved_epoch
        except ReconcilerError:
            return False

    for root_phase, root_candidate, attempt_root in codex_upgrade._campaign_attempt_roots(campaign_dir):
        if root_phase != phase or root_candidate != candidate_id:
            continue
        if later(attempt_root / "reservation.json", "预约收据"):
            return True
        recovery_root = attempt_root / codex_upgrade.ATTEMPT_RECOVERY_DIRNAME
        if recovery_root.is_symlink() or not recovery_root.is_dir():
            continue
        for segment_root in sorted(recovery_root.iterdir()):
            if later(segment_root / codex_upgrade.ATTEMPT_RECOVERY_RESERVATION_FILENAME, "恢复段预约收据"):
                return True
    return False


def _backfill_attempt_owner_closeout(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    ledger_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
    recovery_revision: str | None,
    reservation: Mapping[str, Any],
    control_root: Path | None = None,
) -> dict[str, Any] | None:
    """第 45 项：有预约的父 run 来不及收账时，reconcile-attempt 补做父监督器本应完成的失败收账。

    run 期间发布了预约的父 run 只能走 reconcile-attempt（reconcile-supervisor-run 拒绝），第 39／44／38 项的补收账覆盖
    不到。父 run 在失败收账前丢失 owner（R2 封存或看门狗中止＋动作诊断）时账本停在 active：恢复段失败后批准并消费
    恢复预览不写 recovery_authorized（只有 recovery_required／审核态可授权），协议 15 拒绝后继段——死路；永久失败类
    判 recoverable、提示续跑，而后继协议全部拒绝。这里沿 ``_attempt_owner_run`` 定位父 run，以父监督器同一收账函数
    （``_close_failed_campaign_timing_ledger``，事件 ID 由同一失败摘要派生，幂等）补账，与 owner 在线时收账的结果
    相同：恢复段、候选采集续跑链与可恢复类进入 recovery_required（预算到期先暂停），阶段／候选审核类进审核，永久
    失败类停线。在对账已登记本 attempt／恢复段失败之后调用：此时它们不再是 active，收账才能关闭阶段。

    只在账本仍停在这次失败现场时补：本次失败没有收账或审核事件；账本（去掉预算暂停）是 active 且处于失败阶段；父 run
    之后没有更高序号的 COMMIT（续跑批次未派发）；同阶段（候选按同一候选）没有更晚的预约；账本在本 attempt／恢复段的
    最后一条事件之后只出现过预算控制或 campaign-resume 的恢复登记。于是旧工具已对账过、卡在死路上的现场，部署修复
    后重新对账即可补账接着跑；已经续跑推进的现场不会被补账改写。
    """

    owner = _attempt_owner_run(
        campaign_dir, manifest, phase=phase, candidate_id=candidate_id,
        recovery_revision=recovery_revision, reservation=reservation, control_root=control_root,
    )
    if owner is None:
        return None
    facts = owner["facts"]
    digest = str(facts["failure_digest"])
    prefix = f"{supervisor.CANDIDATE_REVIEW_EVENT_PREFIX}{digest[:supervisor.FAILURE_DIGEST_PREFIX_LENGTH]}"
    settled_ids = {
        f"{prefix}-stage-review-required",
        f"{prefix}-recovery-required",
        f"{prefix}-stop-the-line",
        supervisor.candidate_review_event_id(digest),
    }
    try:
        events = [event for event, _raw in timing_ledger._load_events(ledger_dir)]
        summary = timing_ledger.inspect_ledger(ledger_dir)
    except (OSError, timing_ledger.TimingLedgerError) as error:
        raise ReconcilerError(f"Campaign 账本无法重放：{error}") from error
    if any(event.get("event_id") in settled_ids for event in events):
        return None
    status = summary.get("status_before_pause") if summary.get("status") == "deadline_paused" else summary.get("status")
    if status != "active" or summary.get("active_phase") != facts["phase"]:
        return None
    if owner["latest_sequence"] > owner["sequence"]:
        return None
    reserved = _timestamp(reservation.get("started_at_utc"), "预约 started_at_utc").timestamp()
    if _newer_reservation_exists(campaign_dir, phase=phase, candidate_id=candidate_id, reserved_epoch=reserved):
        return None
    subject = [
        index
        for index, event in enumerate(events)
        if event.get("attempt_id") == attempt_id and event.get("recovery_revision") == recovery_revision
    ]
    if not subject or any(
        event.get("event_type") not in _ATTEMPT_BACKFILL_TOLERATED_EVENTS for event in events[subject[-1] + 1 :]
    ):
        return None
    try:
        routed = supervisor._close_failed_campaign_timing_ledger(
            campaign_dir,
            owner["inner"],
            failed_action_id=str(facts["action_id"]),
            failure_class=str(facts["failure_class"]),
            # 第 48 项：对段补账时段已发布预约（本次对账刚把它登记为失败，账本不再 active），显式告知收账。
            segment_reserved=True if recovery_revision is not None else None,
        )
    except supervisor.SupervisorError as error:
        raise ReconcilerError(f"预约所属父 run 的失败收账补做失败：{error}") from error
    return {
        "run_id": Path(owner["run_dir"]).name,
        "action_id": str(facts["action_id"]),
        "failure_class": str(facts["failure_class"]),
        "ledger_status": routed.get("ledger_status"),
        "idempotent": bool(routed.get("idempotent")),
    }


def _later_timestamp(first: str, second: str) -> str:
    """两个 UTC 时刻里较晚的一个（原样返回字符串）。"""

    return second if _timestamp(second, "时刻") > _timestamp(first, "时刻") else first


def reconcile_supervisor_run(
    run_dir: Path,
    campaign_dir: Path,
    *,
    control_root: Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """对账一个尚无 attempt 的父监督器 run（派发前失败、SIGKILL 或中断）。"""

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("reconcile-supervisor-run 只用于 0.154.0 起的完整 VC 链 Campaign")
    observed = now or _utc_now()
    resolved_run_dir = Path(run_dir).resolve(strict=True)
    # 身份前置先于孤儿 run 终态化与失败回填：未登记工具演进时零写入拒绝。
    current = _current_identity()
    identity = _identity_facts(campaign_dir, manifest, current)
    ledger_dir = _campaign_ledger_dir(manifest)
    _require_registered_tool_identity(identity, _ledger_facts(ledger_dir, now=observed))
    _require_resume_closed(campaign_dir, manifest, ledger_dir)
    _finalize_orphaned_prepared_run(resolved_run_dir)
    orphan_backfill = _backfill_orphaned_action_failure(resolved_run_dir, campaign_dir, manifest)
    run = _run_facts(resolved_run_dir, campaign_dir, manifest)
    if orphan_backfill is not None:
        action_diagnostic = run.get("action_diagnostic")
        if isinstance(action_diagnostic, dict) and isinstance(action_diagnostic.get("post_run_tooling"), dict):
            action_diagnostic["post_run_tooling"]["backfilled"] = bool(orphan_backfill["backfilled"])
        run["orphan_facts"] = orphan_backfill["orphan_facts"]
    # 第 39 项／第 44 项／第 38 项：R2 封存（以及看门狗中止＋动作诊断）的 VC-1～VC-6 非可恢复失败（审核类与永久失败类），
    # 先补做 owner 丢失前未完成的失败收账，再按账本现状对账。
    closeout_backfill = _backfill_orphaned_failure_closeout(
        resolved_run_dir, campaign_dir, run, orphan_backfill, ledger_dir
    )
    if closeout_backfill is not None:
        # 补做的收账事件按写入时刻记账，晚于入口观察时刻；此后的账本、总账判定都按补账之后的时刻观察，
        # 否则账本重放会以"检查时间早于最新 event"拒绝。
        observed = _utc_now()
    ledger = _ledger_facts(ledger_dir, now=observed)
    # 第 53 项：恢复段在段预约之前失败（收账已按同一段号重派收口为 recovery_required）。
    segment_redispatch = _segment_prereservation_revision(resolved_run_dir, run, campaign_dir, ledger_dir)
    if (
        ledger.get("status") == "recovery_required"
        and run.get("failure_class") not in supervisor.RECOVERABLE_ACTION_FAILURE_CLASSES
        and run.get("failure_class") not in supervisor.RECOVERABLE_PARENT_FAILURE_CLASSES
        and not _candidate_capture_recovery_run(resolved_run_dir, run)
        and segment_redispatch is None
    ):
        raise ReconcilerError(
            "Campaign 账本处于 recovery_required，但父动作分类不可恢复"
        )
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    deployment = _deployment_receipt(
        _control_root(campaign_dir, control_root), current, required=not bool(plan.get("fixture_only"))
    )
    campaign_deadline = codex_upgrade._campaign_plan_deadline(campaign_dir)
    receipt_dir = _reconciliation_dir(campaign_dir, f"run-{run['run_id']}")
    request_part, provenance_binding, provenance_copy_path = _request_part(
        campaign_dir, manifest, receipt_dir, plan=plan, head=head, project_root=project_root, now=observed
    )
    environment_decision = codex_upgrade._campaign_environment_decision(campaign_dir, _manifest=manifest)
    contamination = environment_decision["records"]
    failure_observations, root_causes = _supervisor_run_failures(run)
    cause = root_causes[0]
    receipt = {
        "schema_version": SUPERVISOR_RUN_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": _file_sha256(campaign_dir / "campaign.json"),
        "run": run,
        "failure_class": run["failure_class"],
        "attempt_events_fabricated": False,
        "tool_identity": identity,
        "deployment_receipt": deployment,
        "campaign_ledger": ledger,
        "project_ledger": {
            "path": str(project_root),
            "plan_sha256": plan.get("plan_sha256"),
            "head_sequence_before": head.get("sequence"),
            "head_sha256_before": head.get("head_sha256"),
        },
        "campaign_deadline_at_utc": campaign_deadline,
        "contamination_records": list(contamination),
        "root_cause": cause,
        "request_part_status": request_part["status"],
        "provenance_receipt_sha256": request_part["provenance_receipt_sha256"],
        "reservation_exists": False,
        "live_request_count": 0,
        "scanned_bytes": 0,
        "observed_at_utc": observed,
    }
    if failure_observations:
        receipt["failure_observations"] = failure_observations
        receipt["root_causes"] = root_causes
    receipt_path = receipt_dir / SUPERVISOR_RUN_RECEIPT_NAME
    with codex_upgrade._campaign_lock(campaign_dir):
        stored = _write_or_verify(
            receipt_path,
            receipt,
            volatile=(
                "observed_at_utc",
                "campaign_ledger",
                "project_ledger",
                "deployment_receipt",
                "tool_identity",
                "run",
                *RECEIPT_ROOT_CAUSE_VOLATILE_FIELDS,
                "request_part_status",
                "provenance_receipt_sha256",
                "contamination_records",
            ),
        )
        cause = dict(stored["root_cause"])
        if failure_observations:
            failure_observations = [
                dict(item) for item in stored["failure_observations"]
            ]
            root_causes = [dict(item) for item in stored["root_causes"]]
        receipt_binding = _binding(campaign_dir, receipt_path, "reconciliation")
        reconciliation_payload: dict[str, Any] = {
            "campaign_id": str(manifest["campaign_id"]),
            "subject_kind": "supervisor_run",
            "subject_id": run["run_id"],
            "phase": run["phase"],
            "request": request_part,
            "root_cause": {
                "root_cause_id": cause["root_cause_id"],
                "stable_error_code": cause["stable_error_code"],
                "failed_step": cause["failed_step"],
                "stable_dimensions": cause["stable_dimensions"],
                "component": cause["component"],
            },
            "reconciliation_receipt_sha256": receipt_binding["sha256"],
            "attempt_failed_event_sha256": None,
        }
        if failure_observations:
            reconciliation_payload["failure_observations"] = failure_observations
            reconciliation_payload["root_causes"] = root_causes
        batch = _commit_batch(
            campaign_dir,
            operation_id=f"reconcile-supervisor-run:{run['run_id']}",
            event_type="reconciliation_committed",
            payload=reconciliation_payload,
            source={"kind": "supervisor_run_reconciliation", "sha256": receipt_binding["sha256"]},
            receipt_bindings=[receipt_binding, provenance_binding],
        )
    pushed, head_after = _push_and_replay(project_root, campaign_dir, now=observed)
    environment_status = _environment_decision_status(environment_decision)
    decision = _decide(
        head=head_after,
        plan=plan,
        ledger=ledger,
        identity=identity,
        environment_status=environment_status,
        campaign_deadline_at_utc=campaign_deadline,
        root_cause_id=cause["root_cause_id"],
        root_cause_ids=[item["root_cause_id"] for item in root_causes],
        request_status=request_part["status"],
        now=observed,
        campaign_id=str(manifest["campaign_id"]),
        forced_terminal_reason=(
            "integrity_mismatch"
            if run.get("failure_class") in INTEGRITY_MISMATCH_FAILURE_CLASSES
            else None
        ),
    )
    result: dict[str, Any] = {
        "schema_version": SUPERVISOR_RUN_SCHEMA,
        "status": decision["decision"],
        "campaign_id": str(manifest["campaign_id"]),
        "run_id": run["run_id"],
        "reconciliation_receipt": receipt_binding,
        "provenance_receipt": provenance_binding,
        "root_cause": cause,
        "identity_unchanged": identity["unchanged"],
        "batch": batch,
        "project_push": pushed,
        "project_head": {
            "sequence": head_after.get("sequence"),
            "head_sha256": head_after.get("head_sha256"),
            "blocked": head_after.get("blocked"),
            "remaining_live_requests": head_after.get("remaining_live_requests"),
            "root_cause_count": decision["root_cause_count"],
            "root_cause_counts": decision["root_cause_counts"],
        },
        "decision": decision,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    if failure_observations:
        result["failure_observations"] = failure_observations
        result["root_causes"] = root_causes
    if closeout_backfill is not None:
        # 第 39 项：本次对账补做了 owner 丢失前未完成的失败收账（只进命令输出，不进 write-once 收据）。
        result["ledger_closeout_backfill"] = closeout_backfill
    if decision["decision"] == DECISION_RECOVERABLE:
        result["ledger_events"] = []
        staging = run.get("staging")
        ledger_next_action = (
            str(staging["next_action"])
            if isinstance(staging, Mapping) and staging.get("next_action")
            else NEXT_ACTION_SAME_BATCH
        )
        stage_replay_path = receipt_dir / "stage-replay.json"
        stage_review = ledger.get("status") == "stage_review_required"
        # R18：候选审核下失败阶段属于有幂等动作合同的候选级阶段（VC-4）时，同样核验并可凭证明重开阶段。
        candidate_review = (
            ledger.get("status") == "candidate_review_required"
            and run.get("phase") in timing_ledger.CANDIDATE_STAGE_REPLAY_PHASES
        )
        candidate_replay_allowed = False
        candidate_scope = False
        if stage_review or candidate_review or stage_replay_path.exists():
            facts = supervisor.campaign_run_failure_facts(resolved_run_dir, campaign_dir=campaign_dir)
            if facts is None:
                raise ReconcilerError("阶段 review 无法绑定失败父动作")
            ledger_events = [event for event, _ in timing_ledger._load_events(ledger_dir)]
            stage_review_event_id = f"{supervisor.CANDIDATE_REVIEW_EVENT_PREFIX}{facts['failure_digest'][:supervisor.FAILURE_DIGEST_PREFIX_LENGTH]}-stage-review-required"
            candidate_review_event_id = supervisor.candidate_review_event_id(str(facts["failure_digest"]))
            # 重复对账时阶段可能已重开（账本 active），按失败摘要对应的审核事件判定属于哪一类审核。
            candidate_scope = candidate_review or (
                not stage_review
                and run.get("phase") in timing_ledger.CANDIDATE_STAGE_REPLAY_PHASES
                and any(event.get("event_id") == candidate_review_event_id for event in ledger_events)
            )
            review_event_id, review_type, current_review = (
                (candidate_review_event_id, "candidate_review_required", candidate_review)
                if candidate_scope
                else (stage_review_event_id, "stage_review_required", stage_review)
            )
            reviews = [event for event in ledger_events
                       if event.get("event_id") == review_event_id and event.get("event_type") == review_type]
            if len(reviews) != 1 or (current_review and ledger.get("last_event_id") != review_event_id):
                raise ReconcilerError("阶段 review 与本次失败父 run 不一致")
            inner = supervisor._read_json(resolved_run_dir / "campaign-run-manifest.json")["manifest"]
            state = supervisor._read_state(resolved_run_dir)
            if state.get("staging_binding") is not None:
                commit = supervisor._staging_commit_for_run(campaign_dir, state, inner, resolved_run_dir)
                ledger_next_action = NEXT_ACTION_SAME_BATCH if commit is not None else NEXT_ACTION_SAME_SEQUENCE
            else:
                commit = None
                # 历史批次无 COMMIT 合同，保持原规则，不能用新协议自动重派。
                ledger_next_action = "review-required"
            replay = codex_upgrade._campaign_stage_replay_facts(campaign_dir, inner)
            if (ledger_next_action == "review-required" or not replay["allowed"]) and candidate_scope:
                # 候选审核：证明不了幂等时维持原规则只入账，由人工裁定作废候选或停线（或修复半成品后重新对账）。
                # 证明已写出（阶段已重开）后再对账却不再许可，说明输入或半成品在许可后漂移，失败关闭。
                if stage_replay_path.exists():
                    raise ReconcilerError("VC-4 阶段幂等重派证明已写出，但动作输入或半成品已漂移，禁止重派")
                result["stage_replay"] = replay
            elif ledger_next_action == "review-required" or not replay["allowed"]:
                review_command = "保持 stage_review_required：先修复不可幂等半成品或补齐受支持的恢复合同"
                if run.get("phase") == "VC-1" and replay["reasons"]:
                    # D-10：VC-1 官方 seal 链已有幂等动作合同；不成立时点名原因（断言包已发布、官方已封存、attempt 已作废
                    # 等），可修复的半成品修复后重新对账即取得许可。采集等已发布预约的批次仍只走 reconcile-attempt。
                    review_command = (
                        "保持 stage_review_required：VC-1 阶段幂等重派合同不成立（" + "；".join(replay["reasons"])
                        + "）；可修复的半成品修复后重新对账取得重派许可，否则以 close-campaign-ledger 显式停线；"
                        "已发布预约的采集批次只走 reconcile-attempt"
                    )
                result.update(status="stage_review_required", stage_replay=replay, next_command=review_command)
                result["decision"] = {**decision, "decision": "review_required", "reasons": replay["reasons"]}
                return result
            else:
                proof = build_stage_replay_proof(
                    campaign_id=manifest["campaign_id"], run_id=run["run_id"],
                    review_event_id=review_event_id, review_root_cause_id=reviews[0]["root_cause_id"],
                    reconciliation_receipt_sha256=receipt_binding["sha256"],
                    commit_sha256=commit["commit_sha256"] if commit else None,
                    next_action=ledger_next_action, replay=replay,
                )
                _write_or_verify(stage_replay_path, proof, volatile=())
                result["stage_replay"] = proof
                candidate_replay_allowed = candidate_scope
        if ledger.get("active_phase") is not None or stage_review or (candidate_review and candidate_replay_allowed):
            with codex_upgrade._campaign_lock(campaign_dir):
                event = _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-run-passed-{run['run_id']}",
                    phase=str(ledger.get("active_phase") or ledger.get("review_phase") or run["phase"]),
                    event_type="receipt_passed",
                    receipts=_ledger_receipt_bindings(
                        ledger_dir, f"run-{run['run_id']}", receipt_path, provenance_copy_path,
                        stage_replay_path=stage_replay_path if stage_replay_path.exists() else None,
                    ),
                    next_action=ledger_next_action,
                )
            result["ledger_events"] = [event]
        if stage_review and supervisor.stage_replay_completed_action_ids(result.get("stage_replay") or {}):
            # 第 43 项：证明记录断言包动作已完成（断言包已发布且一致），唯一可行的后继是去掉它的续派。
            result["next_command"] = (
                f"{ledger_next_action}：断言证据包已发布且一致（断言包动作已完成），以 compile-and-run-vc-batch 派发"
                "去掉断言包动作、只含 seal 预览的 N+1 续派批次；逐字重派会被断言包门禁拒绝，禁止跳阶段"
            )
        elif stage_review:
            result["next_command"] = f"{ledger_next_action}：按原命令与合法 checkpoint 重派，禁止跳阶段"
        elif candidate_review and candidate_replay_allowed:
            # R18：VC-4 零请求动作的工具缺陷已修复并部署，证明动作可幂等续作，同一 revision 重开 VC-4。
            result["next_command"] = (
                f"{ledger_next_action}：VC-4 已在同一 revision 重开，按原命令逐字重派同一批次；"
                "若判为候选源码问题，仍以 invalidate-candidate preview/apply 作废并开新 revision"
            )
        elif ledger.get("status") == "candidate_review_required":
            # 改造 2：候选级动作失败已把阶段关闭并进入只读等待，对账只负责入账；
            # 下一步由人工裁定：候选源码问题走 invalidate-candidate，否则显式停线。
            # R18：VC-4 动作证明不了幂等时（如已写半成品），修复半成品后可重新对账取得重派许可。
            result["next_command"] = (
                "candidate_review_required：对账已入账；判为候选源码问题则 invalidate-candidate "
                "preview/apply，否则以 close-campaign-ledger 显式停线"
                + ("；VC-4 工具缺陷须先修复不可幂等半成品再重新对账" if candidate_scope else "")
            )
        elif run.get("failure_class") == "tool-evolution-required":
            # 第三批 B3-5（第 17 项）：父 run 窗口内已有采集收口的 attempt 时，采集结果保留，只补后处理。
            settled = _settled_capture_attempts_in_window(resolved_run_dir, campaign_dir)
            if settled:
                result["next_command"] = (
                    f"phase 保持 active：批次内采集已收口（attempt {'、'.join(settled)} 等待封存），之后的动作执行前评估器摘要"
                    "已变化、动作未执行；登记 tool-evolution 后：演进作废了该 attempt 的作业则 reconcile-attempt 入账并批准"
                    "恢复预览、resume --rerun-failed 续跑；未作废则以 compile-and-run-vc-batch 改派同一 attempt 的 seal 链批次 N+1"
                )
            else:
                result["next_command"] = (
                    "phase 保持 active：动作执行前评估器摘要已变化、动作未执行、无请求；登记 tool-evolution 后以 "
                    "compile-and-run-vc-batch 按同一动作计划重新编译派发 N+1（b0 的 checker／builder 须是已登记演进"
                    "迁移到的授权口径，b≥1 改走 evaluation-recover）"
                )
        elif _candidate_post_run_recovery_run(resolved_run_dir, run):
            # 第三批 B3-4：零请求后处理动作以 execution-failure 收口（中断类失败或同 run 内有请求窗口），请求账已核算。
            result["next_command"] = (
                "phase 保持 active：候选级零请求后处理动作失败已对账（请求账已核算）；修复评估／控制工具或环境并受监督"
                "部署、登记 tool-evolution 后，以 compile-and-run-vc-batch 逐字重派同一批次（VC-6 canonical 步骤幂等）；"
                "判为候选源码问题则 invalidate-candidate preview/apply"
            )
        elif run.get("failure_class") == EVIDENCE_METADATA_DRIFT_CLASS:
            # 第三批 R3：内容未变、只有元数据漂移——不需要部署工具，rebind 后逐字重派。
            result["next_command"] = (
                "phase 保持 active：已封存证据只有 mtime／ctime／inode 漂移；执行 harden-evidence-permissions rebind-boundary "
                "--attempt-id <attempt>（候选加 --candidate-id）复算内容并绑定新边界，再以 compile-and-run-vc-batch 逐字重派同一批次"
            )
        elif run.get("failure_class") == "post-run-tooling" and run.get("phase") == "VC-1" and (
            _vc1_published_bundle_continuation(resolved_run_dir, campaign_dir)
        ):
            # 第 43 项：父批次的断言包动作已发布断言包（且一致），逐字重派会被断言包门禁拒绝，只能续派。
            result["next_command"] = (
                "phase 保持 active：官方 seal 链零请求失败已对账；断言证据包已发布且一致，以 compile-and-run-vc-batch "
                "改派同一 attempt 只含 seal 预览（可加 seal 批准）的 N+1 续派批次（逐字重派会被断言包门禁拒绝）；"
                "官方 Job 结果只读保留"
            )
        elif run.get("failure_class") == "post-run-tooling" and run.get("phase") == "VC-1":
            result["next_command"] = (
                "phase 保持 active：官方 seal 链零请求失败已对账；以 compile-and-run-vc-batch 逐字重派同一批次，"
                "若修复改变了 seal 预览，改派同一 attempt 的重新预览批次；官方 Job 结果只读保留"
            )
        elif run.get("failure_class") == "post-run-tooling":
            result["next_command"] = (
                "phase 保持 active：修复评估／控制工具并受监督部署后，以 compile-and-run-vc-batch "
                "逐字重派同一 seal 批次；Candidate Job 结果只读保留。若该批次要封存的 attempt 已被工具演进、环境隔离"
                "或证据根冲突隔离作废，改为 reconcile-attempt 按作废对账并批准恢复预览，再派发 N+1 零请求恢复预览"
            )
        elif run.get("failure_class") == PARENT_PREPARE_ABANDONED_CLASS:
            result["next_command"] = (
                "序号未占：以 compile-and-run-vc-batch 同序号重新 prepare 新 staging attempt"
            )
        elif run.get("failure_class") == PARENT_START_FAILED_CLASS:
            result["next_command"] = (
                "序号已占：以 compile-and-run-vc-batch 按 N+1 逐字重派同一批次内容"
            )
        elif run.get("failure_class") == PARENT_FINALIZE_LOST_CLASS:
            result["next_command"] = (
                "序号已占：以 compile-and-run-vc-batch 按 N+1 逐字重派同一批次内容"
                "（恢复段 run 幂等返回，零请求；随后按段 seal 批次继续）"
            )
        elif _candidate_capture_recovery_kind(resolved_run_dir, run) == "run":
            # 第 49 项：按预览真实补跑在预约前失败（执行失败或预算截止；run 期间有预约的走 reconcile-attempt，不到这里）。
            # 后继协议只承接 N+1 零请求续跑预览（协议 13），逐字重派同一补跑会被全部协议拒绝（它消费的恢复批准冻结的账本
            # head 也已推进）；此前落到下面的通用提示"重新派发同一批次"，与放行不一致。
            result["next_command"] = (
                "phase 保持 active：VC-5 按预览真实补跑在预约前失败已对账（本 run 未发布预约）；截止类失败先确认预算已延期，"
                "执行失败先修复并受监督部署；以 compile-and-run-vc-batch 派发 N+1 零请求续跑预览（resume --rerun-failed "
                "--preview-recovery，候选身份参数与父补跑逐字相同），批准预览后再补跑；逐字重派同一补跑会被后继协议拒绝"
            )
        elif _candidate_capture_recovery_kind(resolved_run_dir, run) == "preview":
            # 第 49 项：零请求续跑预览失败（执行失败或截止清理）由协议 12 承接 N+1 逐字重派同一预览。
            result["next_command"] = (
                "phase 保持 active：VC-5 候选零请求续跑预览失败已对账；截止类失败先确认预算已延期，执行失败先修复并受监督"
                "部署；以 compile-and-run-vc-batch 按 N+1 逐字重派同一预览批次"
            )
        elif segment_redispatch is not None:
            # 第 53 项：恢复段在段预约之前失败，段号仍可开；协议 15 的同段分支承接 N+1 同段重派（后继段 ar<k+1> 没有可授权的
            # 段预览，会被拒绝）。后继段重派还要消费前序失败段的恢复预览，本次入账已使原预览失效，步骤里点明先重新对账前序段。
            inner = _read_json(resolved_run_dir / "campaign-run-manifest.json", "campaign-run 清单").get("manifest")
            result["next_command"] = (
                f"phase 保持 active：恢复段 {segment_redispatch} 在段预约之前失败已对账（未开段、零请求）；截止类失败先确认预算"
                "已延期，执行失败先修复并受监督部署；"
                + supervisor.attempt_recovery_redispatch_steps(campaign_dir, inner, segment_redispatch)
                + f"；不作废候选、不需停线；{segment_redispatch} 本身没有预约，不对它做 reconcile-attempt --recovery-revision"
            )
        else:
            result["next_command"] = "phase 保持 active：以 compile-and-run-vc-batch 重新派发同一批次"
    elif decision["decision"] == DECISION_PAUSED:
        if set(decision.get("pause_kinds") or []) - {"root_cause_repair"}:
            # 纯根因修复暂停（第三批 B3-9）不写预算暂停：账本已处于 stop_required，预算暂停会被账本拒绝。
            result["deadline_pause"] = project_ledger.pause_campaign_deadline(campaign_dir, now=_pause_time(observed))
        result["next_command"] = _paused_next_command(decision, "从原对账 checkpoint 继续")
    else:
        with codex_upgrade._campaign_lock(campaign_dir):
            stop = _permanent_stop(
                campaign_dir,
                manifest,
                ledger_dir,
                subject_id=run["run_id"],
                root_cause_id=cause["root_cause_id"],
                terminal_reason=str(decision["terminal_reason"]),
                receipt_bindings=[receipt_binding, provenance_binding],
                ledger_receipts=_ledger_receipt_bindings(
                    ledger_dir, f"run-{run['run_id']}", receipt_path, provenance_copy_path
                ),
                live_request_count=len(request_part["identity_keys"]),
                reconciliation_receipt_sha256=receipt_binding["sha256"],
                ledger_facts=ledger,
            )
        pushed_terminal, head_terminal = _push_and_replay(project_root, campaign_dir, now=observed)
        result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
        result["next_command"] = stop["next_action"]
    return result


# ---------------------------------------------------------------------------
# 改造 4：无父 run 的 staging 中止（P1）对账
# ---------------------------------------------------------------------------

STAGING_ABORT_RECONCILIATION_SCHEMA = "staging-abort-reconciliation/v1"
ZERO_REQUEST_PART = {
    "status": "resolved",
    "identity_keys": [],
    "identity_key_count_total": 0,
    "estimated_delta": 0,
    "estimated_sources": [],
    "unresolved_job_ids": [],
}


def staging_abort_operation_id(sequence: int, staging_attempt: int) -> str:
    """P1 outbox 的 operation_id：同一 attempt 多次入口重放得到同一 batch（``reused``）。"""

    return f"staging-abort:{int(sequence):04d}:{int(staging_attempt)}"


_ERROR_TYPE_DIMENSION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def staging_abort_error_type_dimension(error_type: str) -> str:
    """``staging.attempt-failed`` 的 error_type 维度：异常类名原样；不是合法标识符时记 ``unrecognized``。

    类名来自 ``type(error).__name__``，恒为标识符；这层兜底只防动态造出的怪异类名让根因编码失败、盖住原始拒因。
    """

    return error_type if _ERROR_TYPE_DIMENSION_RE.fullmatch(error_type) else "unrecognized"


def staging_abort_root_cause(
    phase: str,
    stage: str,
    *,
    error_type: str | None = None,
    error_signature: str | None = None,
) -> dict[str, Any]:
    """无父 run 的 staging 中止根因。

    - 带 ``error_signature``（修好接着跑第 32 项：有原始异常的 prepare／parent-run-create 失败）→
      ``staging.attempt-failed``，维度 phase、stage、error_type、error_signature：同一步骤的不同拒因互不累计，
      同一拒因重复出现仍是同一 ID，上限保护不削弱。
    - 不带（入口孤儿扫描发现的遗弃 attempt，以及第 32 项之前写下的历史 ABORT）→ ``staging.abandoned``，维度
      phase、stage，旧 ID 逐字不变。
    """

    try:
        if error_signature is None:
            return root_cause.describe_root_cause(
                component="orchestrator",
                stable_error_code="staging.abandoned",
                failed_step=stage,
                stable_dimensions={"phase": phase, "stage": stage},
            )
        if not isinstance(error_type, str) or not error_type:
            raise ReconcilerError("带拒因签名的 staging 中止必须给出异常类型")
        return root_cause.describe_root_cause(
            component="orchestrator",
            stable_error_code="staging.attempt-failed",
            failed_step=stage,
            stable_dimensions={
                "phase": phase,
                "stage": stage,
                "error_type": staging_abort_error_type_dimension(error_type),
                "error_signature": error_signature,
            },
        )
    except root_cause.RootCauseError as error:
        raise ReconcilerError(f"根因编码失败：{error}") from error


def staging_abort_receipt_root_cause(abort: Mapping[str, Any]) -> dict[str, Any]:
    """按 ABORT 收据的形态复算根因：带 ``error_signature`` 走 ``staging.attempt-failed``，否则走历史码。

    收据形态由写入方在第一次落盘时决定、之后不可变，所以同一份收据无论何时重放都得到同一根因；第 32 项之前
    写下的收据没有该字段，按旧维度复算，旧 ID 不变。
    """

    signature = abort.get("error_signature")
    return staging_abort_root_cause(
        str(abort["phase"]),
        str(abort["stage"]),
        error_type=str(abort["error_type"]) if signature is not None else None,
        error_signature=str(signature) if signature is not None else None,
    )


def reconcile_staging_abort(
    campaign_dir: Path,
    abort_path: Path,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """对账一个没有父 run 的 staging attempt 中止（P1）：ABORT 即对账收据。

    步骤与 ``reconcile_supervisor_run`` 同序：outbox ``reconciliation_committed``
    （请求 0／resolved，根因按收据形态：带拒因签名为 ``staging.attempt-failed``，否则 ``staging.abandoned``）
    → 推总账 → 重放 → 判定；命中永久条件走现有停线合同。各步幂等：outbox 按 operation_id ``reused``、
    推送 ``duplicate``。
    """

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("staging 中止对账只用于 0.154.0 起的完整 VC 链 Campaign")
    observed = now or _utc_now()
    abort_path = Path(abort_path).resolve(strict=True)
    try:
        abort = vc_artifacts.validate_staging_abort(_read_json(abort_path, "staging-abort 收据"))
    except vc_artifacts.VCArtifactError as error:
        raise ReconcilerError(f"staging-abort 收据无法校验：{error}") from error
    if abort["campaign_id"] != manifest["campaign_id"]:
        raise ReconcilerError("staging-abort 收据的 campaign_id 与 Campaign 不一致")
    if abort["parent_run_dir"] is not None:
        raise ReconcilerError("有父 run 的 staging 中止必须走 reconcile-supervisor-run")
    cause = staging_abort_receipt_root_cause(abort)
    if cause["root_cause_id"] != abort["root_cause_id"]:
        raise ReconcilerError("staging-abort 收据的根因 ID 与其 phase／stage（及异常类型、拒因签名）不一致")
    subject_id = (
        f"staging-{int(abort['sequence']):04d}-{str(abort['phase']).lower()}"
        f"-attempt-{int(abort['staging_attempt'])}"
    )
    current = _current_identity()
    identity = _identity_facts(campaign_dir, manifest, current)
    ledger_dir = _campaign_ledger_dir(manifest)
    ledger = _ledger_facts(ledger_dir, now=observed)
    _require_registered_tool_identity(identity, ledger)
    _require_resume_closed(campaign_dir, manifest, ledger_dir)
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    campaign_deadline = codex_upgrade._campaign_plan_deadline(campaign_dir)
    environment_decision = codex_upgrade._campaign_environment_decision(campaign_dir, _manifest=manifest)
    receipt_binding = _binding(campaign_dir, abort_path, "reconciliation")
    payload = {
        "campaign_id": str(manifest["campaign_id"]),
        "subject_kind": "staging_attempt",
        "subject_id": subject_id,
        "phase": str(abort["phase"]),
        "request": dict(ZERO_REQUEST_PART),
        "root_cause": {
            "root_cause_id": cause["root_cause_id"],
            "stable_error_code": cause["stable_error_code"],
            "failed_step": cause["failed_step"],
            "stable_dimensions": cause["stable_dimensions"],
            "component": cause["component"],
        },
        "reconciliation_receipt_sha256": receipt_binding["sha256"],
        "attempt_failed_event_sha256": None,
    }
    with codex_upgrade._campaign_lock(campaign_dir):
        batch = _commit_batch(
            campaign_dir,
            operation_id=staging_abort_operation_id(int(abort["sequence"]), int(abort["staging_attempt"])),
            event_type="reconciliation_committed",
            payload=payload,
            source={"kind": "staging_abort", "sha256": receipt_binding["sha256"]},
            receipt_bindings=[receipt_binding],
        )
    pushed, head_after = _push_and_replay(project_root, campaign_dir, now=observed)
    decision = _decide(
        head=head_after,
        plan=plan,
        ledger=ledger,
        identity=identity,
        environment_status=_environment_decision_status(environment_decision),
        campaign_deadline_at_utc=campaign_deadline,
        root_cause_id=cause["root_cause_id"],
        request_status="resolved",
        now=observed,
        campaign_id=str(manifest["campaign_id"]),
    )
    result: dict[str, Any] = {
        "schema_version": STAGING_ABORT_RECONCILIATION_SCHEMA,
        "status": decision["decision"],
        "campaign_id": str(manifest["campaign_id"]),
        "subject_id": subject_id,
        "reconciliation_receipt": receipt_binding,
        "root_cause": cause,
        "identity_unchanged": identity["unchanged"],
        "batch": batch,
        "project_push": pushed,
        "project_head": {
            "sequence": head_after.get("sequence"),
            "head_sha256": head_after.get("head_sha256"),
            "blocked": head_after.get("blocked"),
            "remaining_live_requests": head_after.get("remaining_live_requests"),
            "root_cause_count": decision["root_cause_count"],
            "root_cause_counts": decision["root_cause_counts"],
        },
        "decision": decision,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    if decision["decision"] == DECISION_RECOVERABLE:
        result["next_command"] = "序号未占：以 compile-and-run-vc-batch 同序号重新 prepare 新 staging attempt"
    elif decision["decision"] == DECISION_PAUSED:
        result["deadline_pause"] = project_ledger.pause_campaign_deadline(campaign_dir, now=_pause_time(observed))
        result["next_command"] = _paused_next_command(decision, "从原 staging checkpoint 继续")
    else:
        with codex_upgrade._campaign_lock(campaign_dir):
            stop = _permanent_stop(
                campaign_dir,
                manifest,
                ledger_dir,
                subject_id=subject_id,
                root_cause_id=cause["root_cause_id"],
                terminal_reason=str(decision["terminal_reason"]),
                receipt_bindings=[receipt_binding],
                ledger_receipts=[],
                live_request_count=0,
                reconciliation_receipt_sha256=receipt_binding["sha256"],
                ledger_facts=ledger,
            )
        pushed_terminal, head_terminal = _push_and_replay(project_root, campaign_dir, now=observed)
        result["permanent_stop"] = {**stop, "project_push": pushed_terminal, "head_sha256": head_terminal.get("head_sha256")}
        result["next_command"] = stop["next_action"]
    return result


def attempt_reconciled_terminal(campaign_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    """返回该 attempt 的对账收据（若存在）；供 status／active 判定把已对账的孤儿视为终态。"""

    path = _reconciliation_dir(campaign_dir, f"attempt-{attempt_id}") / ATTEMPT_RECEIPT_NAME
    if not path.is_file() or path.is_symlink():
        return None
    payload = _read_json(path, "attempt 对账收据")
    if payload.get("schema_version") != ATTEMPT_SCHEMA or payload.get("attempt_id") != attempt_id:
        raise ReconcilerError("attempt 对账收据身份非法")
    return payload
