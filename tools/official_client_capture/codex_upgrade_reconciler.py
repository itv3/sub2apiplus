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
5. 判定：总账 blocked 永久停线；身份不变、环境已恢复或可恢复、Campaign deadline 与
   总账剩余大于 0、根因累计未达上限则可恢复；否则永久停线。
6. 可恢复：supervisor-run 追加 ``receipt_passed`` 绑定收据；attempt 生成零请求
   ``recovery-preview/v1``，操作员 ``--approve-recovery-sha256`` 后才能
   ``resume --rerun-failed --recovery-preview``。永久停线：``stage_abandoned`` 与
   ``stop_the_line``，再写 ``campaign_terminal`` batch 并推入总账。

两个 reconciler 自身的模型请求数为零。
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_project_ledger as project_ledger
from tools.official_client_capture import codex_upgrade_root_cause as root_cause
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_wire_transition as wire_transition
from tools.official_client_capture import incremental_recovery

SUPERVISOR_RUN_SCHEMA = "supervisor-run-reconciliation/v1"
ATTEMPT_SCHEMA = "attempt-reconciliation/v1"
RECOVERY_PREVIEW_SCHEMA = "recovery-preview/v1"
RECOVERY_APPROVAL_SCHEMA = "recovery-approval/v1"
RECONCILIATION_DIR = "reconciliation"
ATTEMPT_RECEIPT_NAME = "attempt-reconciliation.json"
SUPERVISOR_RUN_RECEIPT_NAME = "supervisor-run-reconciliation.json"
PREVIEW_RE = re.compile(r"^recovery-preview-(\d{2})\.json$")
APPROVAL_RE = re.compile(r"^recovery-approval-(\d{2})\.json$")
COMPONENT = "reconciler"
DECISION_RECOVERABLE = "recoverable"
DECISION_STOP = "permanent_stop"
JOB_STATES = ("complete", "failed", "indeterminate", "pending")
MAX_RECEIPT_BYTES = 16 * 1024 * 1024


class ReconcilerError(ValueError):
    """对账输入不可信、事实互相矛盾或账本／总账拒绝写入。"""


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
    """身份不变 = 当前有效 wire 身份与 policy_sha256 相等（v1 Campaign 比整树）。"""

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
            and str(current.get("policy_sha256")) == str(frozen.get("policy_sha256"))
        )
        return {
            "policy_version": "v2",
            "unchanged": unchanged,
            "effective_wire_producer_sha256": effective["wire_producer_sha256"],
            "effective_source": effective.get("source"),
            "pending_intent": effective.get("pending_intent"),
            "current_wire_producer_sha256": current.get("wire_producer_sha256"),
            "frozen_policy_sha256": frozen.get("policy_sha256"),
            "current_policy_sha256": current.get("policy_sha256"),
        }
    unchanged = str(current.get("files_sha256")) == str(frozen.get("files_sha256"))
    return {
        "policy_version": "v1",
        "unchanged": unchanged,
        "frozen_files_sha256": frozen.get("files_sha256"),
        "current_files_sha256": current.get("files_sha256"),
    }


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


def _ledger_receipt_bindings(
    ledger_dir: Path,
    subject_id: str,
    receipt_path: Path,
    provenance_path: Path,
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
    for role, source in (("reconciliation", receipt_path), ("provenance", provenance_path)):
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


def _ledger_facts(ledger_dir: Path, *, now: str) -> dict[str, Any]:
    summary = timing_ledger.inspect_ledger(ledger_dir, now=now)
    active = timing_ledger._active_attempts(timing_ledger._load_events(ledger_dir))
    return {
        "ledger_dir": str(ledger_dir),
        "status": summary["status"],
        "active_phase": summary.get("active_phase"),
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
    # 副本文件名按去掉观测时间的稳定摘要命名：中断后重放得到同一份副本，账本与 batch 绑定不漂移。
    stable_sha256 = _fingerprint({key: value for key, value in receipt.items() if key != "observed_at_utc"})
    copy_path = receipt_dir / f"provenance-{stable_sha256[:16]}.json"
    stored = _write_or_verify(copy_path, receipt, volatile=("observed_at_utc",))
    with project_ledger.project_lock(project_root):
        initial_keys = project_ledger._initial_keys(project_root, plan)
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
            source_id = f"{campaign_id}:{root_entry.get('producer_run_id')}"
            if source_id in accounted_estimates:
                continue
            estimated_sources.append(
                {"source_id": source_id, "job_id": str(job.get("job_id")), "estimated_count": count}
            )
    unresolved = [str(item) for item in stored.get("unresolved_job_ids", [])]
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


def account_sealed_official(campaign_dir: Path, *, now: str | None = None) -> dict[str, Any]:
    """把已封存 official 阶段的模型请求写入项目总账（自身零请求，幂等）。

    总账此前只在失败对账（reconciliation_committed）时入账，成功封存的 Campaign 只停在
    计时账本与 provenance 收据里。这里复用同一套请求部分核算：精确身份键按总账索引与初始
    清单去重，估计上界按 producer run 去重，写一份不带根因的 reconciliation_committed batch
    并立即推送；同一 attempt 重复执行返回既有 batch。
    """

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("account-sealed-official 只用于 0.154.0 起的完整 VC 链 Campaign")
    try:
        official = codex_upgrade._load_stage_result(
            campaign_dir, "capture-official", _replay_machine_receipts=False
        )
    except codex_upgrade.ConfigurationError as error:
        raise ReconcilerError(f"official 阶段结果不可用：{error}") from error
    attempt_binding = official.get("attempt")
    if official.get("status") != "complete" or not isinstance(attempt_binding, Mapping):
        raise ReconcilerError("official 阶段尚未封存（official_sealed），先 seal 再入账")
    attempt_path = codex_upgrade._campaign_file(campaign_dir, str(attempt_binding.get("path", "")))
    if not attempt_path.is_file() or _file_sha256(attempt_path) != attempt_binding.get("sha256"):
        raise ReconcilerError("official 阶段绑定的 attempt.json 摘要漂移")
    attempt_id = attempt_path.parent.name
    if not codex_upgrade.SAFE_ID_RE.fullmatch(attempt_id):
        raise ReconcilerError("attempt_id 格式非法")
    observed = now or _utc_now()
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    receipt_dir = _reconciliation_dir(campaign_dir, f"sealed-official-{attempt_id}")
    request_part, provenance_binding, _copy_path = _request_part(
        campaign_dir, manifest, receipt_dir, plan=plan, head=head, project_root=project_root, now=observed
    )
    if request_part["status"] == "unresolved":
        raise ReconcilerError(
            "已封存 official 阶段仍有请求数无法确定的 Job，不能入账："
            + "、".join(request_part["unresolved_job_ids"])
        )
    official_path = codex_upgrade._stage_path(campaign_dir, "capture-official")[1]
    batch = _commit_batch(
        campaign_dir,
        operation_id=f"account-sealed-official:{attempt_id}",
        event_type="reconciliation_committed",
        payload={
            "campaign_id": str(manifest["campaign_id"]),
            "subject_kind": "sealed_official_stage",
            "subject_id": attempt_id,
            "phase": "official",
            "request": request_part,
            "official_result_sha256": _file_sha256(official_path),
            "attempt_sha256": str(attempt_binding.get("sha256")),
        },
        source={"kind": "sealed_official_accounting", "sha256": provenance_binding["sha256"]},
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
        "attempt_id": attempt_id,
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
    request_status: str,
    now: str,
) -> dict[str, Any]:
    """步骤 5：先入账后判定。返回 decision 与 terminal_reason（停线时）。"""

    current = _timestamp(now, "now")
    reasons: list[str] = []
    terminal_reason: str | None = None

    def stop(reason: str, note: str) -> None:
        nonlocal terminal_reason
        reasons.append(note)
        if terminal_reason is None:
            terminal_reason = reason

    if head.get("blocked"):
        stop("accounting_unresolved", f"总账 blocked：{head.get('unresolved_operation_ids')}")
    elif request_status == "unresolved":
        stop("accounting_unresolved", "本次请求账务无法确定")
    if environment_status == "contaminated":
        stop("environment_contaminated", "环境恢复失败或前后环境身份不连续")
    if not identity.get("unchanged"):
        stop("identity_changed", "当前有效 wire 身份或策略摘要已变化")
    if ledger.get("status") in {"stop_required", "stopped", "complete"}:
        stop("deadline_wall_clock", f"Campaign 账本状态 {ledger.get('status')}，禁止继续执行 Job")
    if campaign_deadline_at_utc is not None and current >= _timestamp(campaign_deadline_at_utc, "Campaign deadline"):
        stop("deadline_wall_clock", "Campaign 总计划 deadline 已到")
    if current >= _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
        stop("deadline_wall_clock", "项目绝对截止时间已到")
    remaining = head.get("remaining_live_requests")
    if remaining is not None and int(remaining) <= 0:
        stop("deadline_live_requests", "项目请求预算已耗尽")
    if root_cause_id in set(head.get("root_causes_at_limit", [])):
        stop("root_cause_limit", f"根因 {root_cause_id} 累计失败已达上限")
    decision = DECISION_STOP if terminal_reason is not None else DECISION_RECOVERABLE
    return {
        "decision": decision,
        "terminal_reason": terminal_reason,
        "reasons": reasons,
        "root_cause_count": int(dict(head.get("root_cause_counts", {})).get(root_cause_id, 0)),
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
    events: list[dict[str, Any]] = []
    status = str(ledger_facts.get("status"))
    if status not in {"stopped", "complete"}:
        summary = timing_ledger.inspect_ledger(ledger_dir)
        active_phase = summary.get("active_phase")
        if active_phase is not None:
            events.append(
                _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-stage-abandoned-{subject_id}",
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
                    event_id=f"reconcile-stop-the-line-{subject_id}",
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
        operation_id=f"campaign-terminal:{manifest['campaign_id']}",
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
        if checkpoint_status == "complete" and result_status == "complete" and execution_ok:
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
        }
    grouped = {state: sorted(j for j, s in states.items() if s == state) for state in JOB_STATES}
    return {"planned_job_ids": sorted(planned), "states": states, "groups": grouped, "details": details}


def _environment_facts(
    campaign_dir: Path,
    attempt_root: Path,
    attempt: Mapping[str, Any] | None,
    contamination: list[str],
) -> dict[str, Any]:
    """环境已恢复（after 探针与恢复收据在位）／未恢复／污染。"""

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
        if attempt.get("status") == "environment_contaminated" or restoration_error is not None:
            status = "contaminated"
        elif environment.get("after_probe") is not None and environment.get("restoration_report") is not None:
            status = "restored"
    elif after.is_file() and restoration.is_file():
        status = "restored"
    return {
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
    ledger_status: str,
    request_status: str | None,
    jobs: Mapping[str, Any],
) -> dict[str, Any]:
    """按稳定优先级选 A0a-3 根因；failed_step 只取稳定的 Job ID 或固定步骤名。"""

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
    elif ledger_status in {"stop_required", "stopped"} or (
        attempt is not None
        and (
            (isinstance(attempt.get("watchdog"), Mapping) and attempt["watchdog"].get("timeout_checkpoint") is not None)
            or attempt.get("deadline_orphan_finalization") is not None
        )
    ):
        code = "attempt.deadline-expired"
    elif not identity_unchanged:
        code = "attempt.identity-changed"
    elif request_status == "unresolved":
        code = "attempt.accounting-unresolved"
    try:
        return root_cause.describe_root_cause(
            component=COMPONENT,
            stable_error_code=code,
            failed_step=failed_step,
            stable_dimensions={"phase": phase},
        )
    except root_cause.RootCauseError as error:
        raise ReconcilerError(f"根因编码失败：{error}") from error


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
    now: str,
) -> dict[str, Any]:
    """零请求恢复预览：冻结四类 Job 闭集与预计新增请求数，等待操作员批准。"""

    groups = jobs["groups"]
    reusable = list(groups["complete"]) if (attempt_exists and environment_status == "restored") else []
    execute = sorted(set(jobs["planned_job_ids"]) - set(reusable))
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
        "source_attempt_receipt_exists": attempt_exists,
        "reconciliation_receipt_sha256": reconciliation_receipt_sha256,
        "planned_job_ids": list(jobs["planned_job_ids"]),
        "complete_job_ids": list(groups["complete"]),
        "failed_job_ids": list(groups["failed"]),
        "indeterminate_job_ids": list(groups["indeterminate"]),
        "pending_job_ids": list(groups["pending"]),
        "reuse_job_ids": reusable,
        "execute_job_ids": execute,
        "reuse_basis": (
            "source attempt 环境已恢复，complete Job 只读复用"
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
        "scanned_bytes": 0,
    }
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


def approve_recovery_preview(campaign_dir: Path, attempt_id: str, *, approve_sha256: str) -> dict[str, Any]:
    """操作员按 review_sha256 批准恢复预览；批准收据只写一次，幂等返回既有。"""

    if not codex_upgrade.SHA256_RE.fullmatch(str(approve_sha256)):
        raise ReconcilerError("--approve-recovery-sha256 格式非法")
    receipt_dir = _reconciliation_dir(campaign_dir, f"attempt-{attempt_id}")
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
    approval = {
        "schema_version": RECOVERY_APPROVAL_SCHEMA,
        "index": approval_index + 1,
        "campaign_id": preview.get("campaign_id"),
        "source_attempt_id": preview.get("source_attempt_id"),
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
) -> dict[str, Any]:
    """resume 衔接：只接受当前 Campaign 内、已批准、工具身份仍相同的恢复预览。"""

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
    if resolved.parent.name != f"attempt-{attempt_id}":
        raise ReconcilerError("恢复预览目录与其来源 attempt 不一致")
    approved = None
    for child in sorted(resolved.parent.iterdir()):
        if APPROVAL_RE.fullmatch(child.name):
            payload = _read_json(child, "恢复批准收据")
            if payload.get("preview_sha256") == _file_sha256(resolved) and payload.get("approved_sha256") == preview.get("review_sha256"):
                approved = payload
    if approved is None:
        raise ReconcilerError("恢复预览尚未批准；先执行 reconcile-attempt --approve-recovery-sha256 <review_sha256>")
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
    return {**preview, "approval": approved, "preview_path": str(resolved)}


def reconcile_attempt(
    campaign_dir: Path,
    attempt_id: str,
    *,
    control_root: Path | None = None,
    approve_recovery_sha256: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """对账一个 reservation 之后中断的 attempt（步骤 1～6）。"""

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = codex_upgrade._require_formal_campaign(campaign_dir)
    if not codex_upgrade._requires_complete_vc_artifacts(manifest):
        raise ReconcilerError("reconcile-attempt 只用于 0.154.0 起的完整 VC 链 Campaign")
    observed = now or _utc_now()
    phase, candidate_id, attempt_root = _locate_attempt(campaign_dir, attempt_id)
    reservation = codex_upgrade._load_capture_reservation(
        campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id, _manifest=manifest
    )
    attempt: dict[str, Any] | None = None
    attempt_path = attempt_root / "attempt.json"
    if attempt_path.exists() or attempt_path.is_symlink():
        _root, attempt = codex_upgrade._load_capture_attempt(
            campaign_dir, phase, candidate_id, attempt_id, _verified_campaign_manifest=manifest
        )
        if attempt.get("status") == "awaiting_receipts" and not codex_upgrade._failed_job_ids(attempt.get("results")):
            raise ReconcilerError("attempt 正等待 seal，不是中断；reconcile-attempt 只处理失败或中断的 attempt")
    stage_result = codex_upgrade._stage_path(campaign_dir, "capture-official" if phase == "official" else "capture-candidate", candidate_id)[1]
    if stage_result.exists():
        raise ReconcilerError("该阶段已封存，attempt 不再属于可对账的中断")
    records, latest_checkpoints, chain = _checkpoint_facts(attempt_root)
    jobs = _classify_jobs(campaign_dir, manifest, reservation, attempt, latest_checkpoints, attempt_root)
    contamination = codex_upgrade._campaign_contamination_records(campaign_dir, _manifest=manifest)
    environment = _environment_facts(campaign_dir, attempt_root, attempt, contamination)
    current = _current_identity()
    identity = _identity_facts(campaign_dir, manifest, current)
    ledger_dir = _campaign_ledger_dir(manifest)
    ledger = _ledger_facts(ledger_dir, now=observed)
    project_root = _project_root(campaign_dir)
    plan, head = _project_facts(project_root)
    deployment = _deployment_receipt(
        _control_root(campaign_dir, control_root), current, required=not bool(plan.get("fixture_only"))
    )
    campaign_deadline = codex_upgrade._campaign_plan_deadline(campaign_dir)
    receipt_dir = _reconciliation_dir(campaign_dir, f"attempt-{attempt_id}")

    # 步骤 2 的请求部分先核算（它也是根因 accounting-unresolved 的依据）。
    request_part, provenance_binding, provenance_copy_path = _request_part(
        campaign_dir, manifest, receipt_dir, plan=plan, head=head, project_root=project_root, now=observed
    )
    cause = _attempt_root_cause(
        phase=phase,
        attempt=attempt,
        environment_status=environment["status"],
        identity_unchanged=bool(identity["unchanged"]),
        ledger_status=str(ledger["status"]),
        request_status=request_part["status"],
        jobs=jobs,
    )

    # 步骤 1：Campaign 侧写 reconciliation 收据（写一次），再登记账本 attempt_failed。
    receipt = {
        "schema_version": ATTEMPT_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": _file_sha256(campaign_dir / "campaign.json"),
        "phase": phase,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "attempt_receipt_exists": attempt is not None,
        "attempt_status": attempt.get("status") if attempt is not None else None,
        "attempt_digest": attempt.get("attempt_digest") if attempt is not None else None,
        "reservation": {
            "path": (attempt_root / "reservation.json").relative_to(campaign_dir).as_posix(),
            "sha256": _file_sha256(attempt_root / "reservation.json"),
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
                "root_cause",
                "request_part_status",
                "provenance_receipt_sha256",
            ),
        )
        # 中断后重放：根因与请求状态以首次落盘的收据为准，后续步骤按同一根因幂等推进。
        cause = dict(stored["root_cause"])
        receipt_binding = _binding(campaign_dir, receipt_path, "reconciliation")
        ledger_events: list[dict[str, Any]] = []
        ledger_note = "recorded"
        active_ids = {item["attempt_id"] for item in ledger["active_attempts"]}
        if ledger["status"] == "active" and ledger.get("active_phase") is None:
            ledger_note = "skipped:ledger_active_without_phase"
        elif ledger["status"] == "active":
            if attempt_id not in active_ids:
                ledger_events.append(
                    _append_ledger_event(
                        ledger_dir,
                        event_id=f"reconcile-attempt-started-{attempt_id}",
                        phase=str(ledger["active_phase"]),
                        event_type="attempt_started",
                        attempt_id=attempt_id,
                        next_action="reconcile-attempt",
                    )
                )
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
        failed_sha = next(
            (item["event_sha256"] for item in ledger_events if item["event_id"] == f"reconcile-attempt-failed-{attempt_id}"),
            _ledger_event_sha256(ledger_dir, f"reconcile-attempt-failed-{attempt_id}"),
        )

        # 步骤 2：一个 batch 一个事件 reconciliation_committed。
        batch = _commit_batch(
            campaign_dir,
            operation_id=f"reconcile-attempt:{attempt_id}",
            event_type="reconciliation_committed",
            payload={
                "campaign_id": str(manifest["campaign_id"]),
                "subject_kind": "attempt",
                "subject_id": attempt_id,
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
            },
            source={"kind": "attempt_reconciliation", "sha256": receipt_binding["sha256"]},
            receipt_bindings=[receipt_binding, provenance_binding],
        )
    # 步骤 3、4：推送并锁内重放。
    pushed, head_after = _push_and_replay(project_root, campaign_dir, now=observed)
    # 步骤 5：判定。
    decision = _decide(
        head=head_after,
        plan=plan,
        ledger=ledger,
        identity=identity,
        environment_status=environment["status"],
        campaign_deadline_at_utc=campaign_deadline,
        root_cause_id=cause["root_cause_id"],
        request_status=request_part["status"],
        now=observed,
    )
    result: dict[str, Any] = {
        "schema_version": ATTEMPT_SCHEMA,
        "status": decision["decision"],
        "campaign_id": str(manifest["campaign_id"]),
        "attempt_id": attempt_id,
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
        },
        "decision": decision,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    # 步骤 6。
    if decision["decision"] == DECISION_RECOVERABLE:
        provenance_copy = _read_json(campaign_dir / provenance_binding["path"], "provenance 副本")
        preview = _recovery_preview(
            campaign_dir,
            receipt_dir,
            manifest=manifest,
            attempt_id=attempt_id,
            phase=phase,
            candidate_id=candidate_id,
            attempt_exists=attempt is not None,
            jobs=jobs,
            environment_status=environment["status"],
            provenance_copy=provenance_copy,
            current=current,
            reconciliation_receipt_sha256=receipt_binding["sha256"],
            now=observed,
        )
        result["recovery_preview"] = preview
        result["recovery_preview_path"] = str(receipt_dir / f"recovery-preview-{int(preview['index']):02d}.json")
        result["next_command"] = (
            f"reconcile-attempt --approve-recovery-sha256 {preview['review_sha256']} 后 "
            "resume --rerun-failed --recovery-preview <preview path>"
        )
        if approve_recovery_sha256 is not None:
            result["recovery_approval"] = approve_recovery_preview(
                campaign_dir, attempt_id, approve_sha256=approve_recovery_sha256
            )
            result["next_command"] = f"resume --rerun-failed --recovery-preview {result['recovery_preview_path']}"
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
    if state.get("state") == "running" and owner_alive:
        raise ReconcilerError("父监督器仍在运行，禁止对账")
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
        if begun >= started_utc:
            reservations_in_window.append(attempt_root.name)
    if reservations_in_window:
        raise ReconcilerError(
            "该 run 期间已产生 reservation，属于 attempt 中断；请改用 reconcile-attempt："
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
    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "state": state.get("state"),
        "owner_pid": state.get("owner_pid"),
        "owner_alive": owner_alive,
        "phase": state.get("phase"),
        "started_at_utc": state.get("started_at_utc"),
        "terminal_at_utc": state.get("terminal_at_utc"),
        "manifest_sha256": run_manifest.get("manifest_sha256") if isinstance(run_manifest, Mapping) else None,
        "manifest_schema_version": run_manifest.get("schema_version") if isinstance(run_manifest, Mapping) else None,
        "no_op": inner.get("no_op") if isinstance(inner, Mapping) else None,
        "execute_items": list(inner.get("execute_items", [])) if isinstance(inner, Mapping) else [],
        "reuse_items": list(inner.get("reuse_items", [])) if isinstance(inner, Mapping) else [],
        "event_count": audit.get("event_count"),
        "minute_record_count": audit.get("minute_record_count"),
        "classification_counts": audit.get("classification_counts"),
        "audit_incomplete": audit.get("audit_incomplete"),
        "integrity_errors": list(events),
        "last_operation": last_operation,
    }


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
    run = _run_facts(Path(run_dir).resolve(strict=True), campaign_dir, manifest)
    current = _current_identity()
    identity = _identity_facts(campaign_dir, manifest, current)
    ledger_dir = _campaign_ledger_dir(manifest)
    ledger = _ledger_facts(ledger_dir, now=observed)
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
    contamination = codex_upgrade._campaign_contamination_records(campaign_dir, _manifest=manifest)
    try:
        cause = root_cause.describe_root_cause(
            component=COMPONENT,
            stable_error_code="supervisor-run.interrupted",
            failed_step=str(run["last_operation"]).replace(":", "-")[:128],
            stable_dimensions={"phase": str(run["phase"])},
        )
    except root_cause.RootCauseError as error:
        raise ReconcilerError(f"根因编码失败：{error}") from error
    receipt = {
        "schema_version": SUPERVISOR_RUN_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "campaign_manifest_sha256": _file_sha256(campaign_dir / "campaign.json"),
        "run": run,
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
                "root_cause",
                "request_part_status",
                "provenance_receipt_sha256",
                "contamination_records",
            ),
        )
        cause = dict(stored["root_cause"])
        receipt_binding = _binding(campaign_dir, receipt_path, "reconciliation")
        batch = _commit_batch(
            campaign_dir,
            operation_id=f"reconcile-supervisor-run:{run['run_id']}",
            event_type="reconciliation_committed",
            payload={
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
            },
            source={"kind": "supervisor_run_reconciliation", "sha256": receipt_binding["sha256"]},
            receipt_bindings=[receipt_binding, provenance_binding],
        )
    pushed, head_after = _push_and_replay(project_root, campaign_dir, now=observed)
    environment_status = "contaminated" if contamination else "restored"
    decision = _decide(
        head=head_after,
        plan=plan,
        ledger=ledger,
        identity=identity,
        environment_status=environment_status,
        campaign_deadline_at_utc=campaign_deadline,
        root_cause_id=cause["root_cause_id"],
        request_status=request_part["status"],
        now=observed,
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
        },
        "decision": decision,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    if decision["decision"] == DECISION_RECOVERABLE:
        result["ledger_events"] = []
        if ledger.get("active_phase") is not None:
            with codex_upgrade._campaign_lock(campaign_dir):
                event = _append_ledger_event(
                    ledger_dir,
                    event_id=f"reconcile-run-passed-{run['run_id']}",
                    phase=str(ledger["active_phase"]),
                    event_type="receipt_passed",
                    receipts=_ledger_receipt_bindings(
                        ledger_dir, f"run-{run['run_id']}", receipt_path, provenance_copy_path
                    ),
                    next_action="redispatch-same-batch",
                )
            result["ledger_events"] = [event]
        result["next_command"] = "phase 保持 active：以 compile-and-run-vc-batch 重新派发同一批次"
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


def attempt_reconciled_terminal(campaign_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    """返回该 attempt 的对账收据（若存在）；供 status／active 判定把已对账的孤儿视为终态。"""

    path = _reconciliation_dir(campaign_dir, f"attempt-{attempt_id}") / ATTEMPT_RECEIPT_NAME
    if not path.is_file() or path.is_symlink():
        return None
    payload = _read_json(path, "attempt 对账收据")
    if payload.get("schema_version") != ATTEMPT_SCHEMA or payload.get("attempt_id") != attempt_id:
        raise ReconcilerError("attempt 对账收据身份非法")
    return payload
