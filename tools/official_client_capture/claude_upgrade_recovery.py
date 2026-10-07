#!/usr/bin/env python3
"""Claude VC-1 恢复：对账、续跑闭集、resume、残留取证进程清理（Claude 升级控制面设计 B2-08）。

一、链路
========
* 有未收口预约（attempt 失败、被强杀或被作废）：``reconcile-attempt`` 在 Claude 适配器作用域里调用共享对账器，先入账后
  判定；可恢复时共享对账器写零请求恢复预览，并以 ``recovery_reuse_check``（本模块，R17）按 resume 同一复用判定只读复算，
  不一致即拒绝批准 → 操作员 ``reconcile-attempt --approve-recovery-sha256`` 批准 → ``resume --rerun-failed --recovery-preview``
  在 Campaign 租约下建新 attempt：复用已完成作业（只读承接原证据），只执行预览冻结的执行集合。
* 无预约（父监督器 run 在预约之前失败）：``reconcile-supervisor-run``（共享）入账判定，按提示以 N+1 批次重派。
* 封存批次（首批之后的静态分析与 seal，``vc1-claude.sh`` 派发）零请求失败：共享监督器的 post-run-tooling 判据只认 Codex
  的阶段项名，Claude 的 ``vc1-0X-*`` 动作失败按 execution-failure 收账、账本进 ``stage_review_required``；
  ``reconcile-supervisor-run`` 按 ``claude_upgrade_batch`` 的 VC-1 封存批次阶段幂等重派合同写证明、重开 VC-1，再重跑
  ``vc1-claude.sh`` 以 N+1 逐字重派同一批次（共享阶段审核协议承接）。合同不成立时（attempt 被隔离或作废、仍有失败 Job、
  已封存、修复改变了 seal 代号等）留在阶段审核，按对账提示走 ``reconcile-attempt`` 或显式停线。
* 封存后：``account-sealed-official``（共享）把已封存官方阶段的请求写入项目总账；只读复用封存（零请求）没有可入账的请求。

二、续跑闭集（与共享恢复预览同一公式，``recovery_plan``）
==========================================================
* 来源 attempt：最新的官方 attempt，状态为 failed、已隔离的 environment_contaminated、有作业被作废的 awaiting_receipts
  （工具演进作废、证据根冲突隔离），或已对账的孤儿预约（没有 attempt.json）。其它未收口的预约或待封存 attempt 一律拒绝。
* 复用集合 = 共享对账器 ``_classify_jobs`` 判为 complete 的作业（checkpoint 与结果都 complete、执行摘要一致），且 attempt
  收据存在、共享 ``_environment_facts`` 判环境 ``restored``（Claude 口径：前后探针都在、两个采集容器零漂移，不是污染、冲突
  隔离或连续性漂移），再去掉被工具演进作废的作业；共享预览写下连续性漂移收据时复用为空。执行集合 = 计划作业 − 复用集合。
* 复用证据逐项复核（失败关闭）：原产出 Job 的闭环收据摘要、Job 级权限收口重放（文件 inode／大小／mtime 与收口时一致），
  以及 relay 清单摘要。复用结果原样承接（``disposition`` 记 reused、``job_closure`` 记 null、``reused_from`` 绑定来源 attempt
  收据、Job 收据与最初产出它的 Job 闭环收据），证据根不动。

三、resume 的真实执行（``_run_recovery_attempt``）
=================================================
与 B2-06 的 ``run_capture_attempt`` 同一结构与同一 attempt 合同（预约、探针、逐 Job 执行与收口、run 末收口、只写一次的
attempt 收据），差别只在：

* 预约前多两道零写入门禁：两个采集容器里不得有残留取证进程（见第四节），并消费已批准的恢复预览（计时账本
  recovery_required → recovery_authorized）；预览冻结的复用／执行集合必须与当场重算的闭集逐项相同；
* 原子预约在同一临时目录里一起发布 ``reservation.json`` 与 ``recovery-source.json``（预约字段闭集不变，恢复来源单独成档）；
  来源必须仍是最新的官方 attempt，其余预约都已收口；
* before 探针之后：有复用时先核对环境连续性（来源 attempt 的 after 探针与本轮 before 探针在两个容器的 containers／
  configuration／client 三类快照逐字相等，不等即以 ``EnvironmentContinuityDrift`` 失败、零请求，共享对账器据此下次全部
  重跑），再写复用结果的 checkpoint 与 Job 收据；
* 执行集合作业的固定证据根若已存在（孤儿 attempt 写到一半的证据、或要重跑的已完成作业的证据），先写取代收据再改名为同级
  ``<名>.superseded-<本 attempt>``（中继拒绝覆盖已存在的输出目录）；取代收据的 ``logical_root`` 是容器坐标（与结果的
  ``evidence_roots`` 同一口径），``host_root`` 是宿主路径，被取代 attempt 的权限收口重放经编排器换算宿主路径后承接归档目录。

四、残留取证进程
================
强杀或超时后，断网容器（合成步骤所在，监督器只给 ``job:`` 操作建立抓包容器停止绑定）里可能残留 relay、claude 进程；
「残留进程即污染」不放宽：任何 attempt 在 before 探针看到残留进程都判环境污染。恢复链的处理：

* 对账时识别：``reconcile-attempt`` 在共享对账之后对两个采集容器采一次只读探针，留证在
  ``control/reconciliation/attempt-<id>/residual-probes/<时刻>/``，输出 ``residual_processes`` 并在下一步提示里写明；
* 清理在明确批准下进行并留证：``environment-cleanup preview`` 列出两个容器里的残留进程（PID、进程启动时刻与标签，不记命令
  行），批准依据必须是本 Campaign 已批准的恢复预览（来源是最新 attempt）或未隔离的环境污染；``apply`` 按
  ``--approve-sha256``／``--approved-by`` 在 Campaign 租约下重列、核对与预览逐项相同后先 TERM 后 KILL，前后各采一次探针，
  写只写一次的清理收据 ``control/environment/cleanup/cleanup-NN.json``。它可作 ``environment-isolate`` 的修复收据，清理后的
  探针清单可作干净环境复核收据；
* resume 在预约之前再查一次，有残留进程即零写入拒绝（不预约、不发请求），提示先清理。

五、不提供的子命令
==================
* ``harden-evidence-permissions``：Codex 的 A3a 两步式收口是为改造前没有 run 末收口（或只有 v1 收口）的历史 attempt 事后
  补做的。Claude 的 run 从第一个受管版本起就在每个 Job 终态与 run 末各做一次 ``evidence-permission-closeout/v2`` 收口
  （B2-06），收口重放随读取 attempt 默认执行（B2-07）；run 末收口失败的 attempt 记为 failed，经对账、批准恢复预览与
  ``resume --rerun-failed`` 由新 attempt 全部复用、零执行地重新收口。已封存证据的元数据漂移重绑（``rebind-boundary``）
  随候选证据封存（B4-02）统一评估。
* ``wire-transition-intent``／``wire-transition-final``：Claude Campaign 只用工具演进链，wire 层变化一律经 ``tool-evolution``
  登记（逐作业作废，resume 只重跑受影响作业）；两条链在共享合同里互斥，Claude 不建 wire transition 链。
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import codex_upgrade_evidence_permissions
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_wire_transition
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane

CLIENT = "claude"
PHASE = "official"
RECOVERY_SOURCE_FILENAME = "recovery-source.json"
RECOVERY_SOURCE_SCHEMA = "claude-upgrade-recovery-source/v1"
RECOVERY_SOURCE_FIELDS = frozenset(
    {
        "schema_version", "client", "campaign_id", "attempt_id", "reservation_digest", "source_attempt_id", "source_status",
        "source_reservation", "source_attempt", "recovery_preview", "recovery_approval", "timing_recovery_event",
        "reuse_job_ids", "execute_job_ids", "invalidated", "recorded_at_utc", "recovery_source_sha256",
    }
)
RESIDUAL_PROBE_DIRNAME = "residual-probes"
CLEANUP_DIR = Path("control") / "environment" / "cleanup"
CLEANUP_RECEIPT_RE = re.compile(r"^cleanup-(\d{2,})\.json$")
CLEANUP_PREVIEW_SCHEMA = "claude-upgrade-environment-cleanup-preview/v1"
CLEANUP_RECEIPT_SCHEMA = "claude-upgrade-environment-cleanup/v1"
CLEANUP_COMMAND_TIMEOUT_SECONDS = 60
CLEANUP_TERM_GRACE_SECONDS = 5.0
CLEANUP_KILL_GRACE_SECONDS = 5.0
CLEANUP_WALL_SECONDS = 15 * 60
CLEANUP_HEARTBEAT_SECONDS = 30
# 共享对账器的 attempt 判定（resume --rerun-failed 来源）识别 execution_error.type 这个类名。
CONTINUITY_DRIFT_ERROR_TYPE = claude_upgrade.CONTINUITY_DRIFT_ERROR_TYPE


class EnvironmentContinuityDrift(ConfigurationError):
    """被承接 attempt 的收尾环境与本轮起始环境不一致（类名即 attempt 收据 ``execution_error.type``）。"""


assert EnvironmentContinuityDrift.__name__ == CONTINUITY_DRIFT_ERROR_TYPE


def _adapter() -> Any:
    return upgrade_client_adapter.adapter_for(CLIENT)


def _resolved_campaign(value: Any) -> Path:
    campaign_dir = Path(value)
    claude_upgrade._validate_existing_campaign_path(campaign_dir)
    return campaign_dir.resolve(strict=True)


def _attempt_root(campaign_dir: Path, attempt_id: str) -> Path:
    if not capture.SAFE_ID_RE.fullmatch(str(attempt_id)):
        raise ConfigurationError("attempt-id 非法。")
    return campaign_dir / claude_upgrade._capture_attempt_relative(PHASE, None) / "attempts" / str(attempt_id)


def _binding(campaign_dir: Path, path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError(f"绑定的文件缺失或不可信：{path}")
    return {"path": path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(path)}


def _attempt_evolution_reconciled(campaign_dir: Path, attempt_id: str) -> bool:
    """等待封存的 attempt 已按「工具演进作废作业」对账（收据带 tool_evolution_invalidation）：不再算待封存（同 Codex）。"""

    receipt = capture._attempt_reconciled_terminal(campaign_dir, attempt_id)
    return isinstance(receipt, Mapping) and isinstance(receipt.get("tool_evolution_invalidation"), Mapping)


# ---------------------------------------------------------------------------
# 续跑闭集（resume 与 R17 复算共用，只读）
# ---------------------------------------------------------------------------


def require_current_source(campaign_dir: Path, manifest: Mapping[str, Any], source_attempt_id: str) -> None:
    """续跑来源必须是最新的官方 attempt，且其余官方预约都已收口（已对账的孤儿、被作废或已对账作废的待封存 attempt）。"""

    ordered = claude_upgrade._ordered_capture_attempts(campaign_dir, PHASE, None, _manifest=manifest)
    newest = ordered[0][0].name if ordered else None
    if newest != source_attempt_id:
        raise ConfigurationError(
            f"续跑来源必须是最新的官方 attempt：来源 {source_attempt_id}，最新 {newest}（来源已经续跑过，或其后另有 attempt；"
            "对最新 attempt 重新对账）。"
        )
    invalidated = capture._invalidated_attempts(campaign_dir)
    foreign: list[str] = []
    for root, _reservation in ordered[1:]:
        if not (root / "attempt.json").exists():
            if capture._attempt_reconciled_terminal(campaign_dir, root.name) is None:
                foreign.append(f"official:{root.name}:reserved_or_interrupted")
            continue
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, PHASE, None, root.name, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
        )
        if (
            attempt.get("status") == "awaiting_receipts"
            and (PHASE, None, root.name) not in invalidated
            and not _attempt_evolution_reconciled(campaign_dir, root.name)
        ):
            foreign.append(f"official:{root.name}")
    if foreign:
        raise ConfigurationError(f"Campaign 还有其它未收口的官方预约或待封存 attempt，禁止续跑：{foreign}")


def _producer(campaign_dir: Path, source_root: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    """被复用结果最初的产出 attempt 与 Job 闭环收据（Campaign 相对路径＋摘要）；承接链上一律指向最初执行它的 attempt。"""

    if result.get("disposition") == "reused":
        reused_from = result.get("reused_from")
        producer = reused_from.get("producer") if isinstance(reused_from, Mapping) else None
        if (
            not isinstance(producer, Mapping)
            or not capture.SAFE_ID_RE.fullmatch(str(producer.get("attempt_id", "")))
            or not isinstance(producer.get("job_closure"), Mapping)
        ):
            raise ConfigurationError(f"复用结果 {result.get('id')} 缺少原产出绑定。")
        return {"attempt_id": str(producer["attempt_id"]), "job_closure": dict(producer["job_closure"])}
    closure = result.get("job_closure")
    if not isinstance(closure, Mapping) or not isinstance(closure.get("path"), str):
        raise ConfigurationError(f"已完成 Job {result.get('id')} 没有闭环收据绑定，不能复用。")
    path = source_root / str(closure["path"])
    return {"attempt_id": source_root.name, "job_closure": {"path": path.relative_to(campaign_dir).as_posix(), "sha256": str(closure.get("sha256"))}}


def _verify_reusable_evidence(
    campaign_dir: Path,
    result: Mapping[str, Any],
    producer: Mapping[str, Any],
    layout: Any,
) -> None:
    """复用前逐项复核证据：Job 闭环收据摘要、Job 级权限收口重放、relay 清单摘要（任一不成立即失败关闭）。"""

    job_id = str(result.get("id"))
    binding = producer["job_closure"]
    closure_path = claude_upgrade._campaign_file(campaign_dir, str(binding.get("path", "")))
    if closure_path.is_symlink() or not closure_path.is_file() or file_sha256(closure_path) != binding.get("sha256"):
        raise ConfigurationError(f"复用 Job {job_id} 的闭环收据缺失或摘要漂移。")
    closure = claude_upgrade._read_json(closure_path, "Job 闭环收据")
    producer_root = _attempt_root(campaign_dir, str(producer["attempt_id"]))
    job_root = closure_path.parent
    if (
        closure.get("schema_version") != capture.JOB_CLOSURE_SCHEMA
        or closure.get("client") != CLIENT
        or closure.get("job_id") != job_id
        or closure.get("attempt_id") != producer["attempt_id"]
        or closure.get("status") != "complete"
        or closure.get("environment_contaminated")
        or closure.get("execution_sha256") != result.get("execution_sha256")
        or job_root != producer_root / "evidence" / capture.JOBS_DIRNAME / job_id
    ):
        raise ConfigurationError(f"复用 Job {job_id} 的闭环收据身份、状态或位置不一致。")
    closeout = closure.get("evidence_permission_closeout")
    receipt_name = codex_upgrade_evidence_permissions.RECEIPT_FILENAME
    if not isinstance(closeout, Mapping) or closeout.get("path") != (job_root / receipt_name).relative_to(producer_root).as_posix():
        raise ConfigurationError(f"复用 Job {job_id} 的 Job 级收口绑定非法。")
    roots = [job_root / "evidence", job_root / "logs", *(Path(str(item)) for item in closure.get("host_evidence_roots") or [])]
    try:
        codex_upgrade_evidence_permissions.replay_evidence_permission_closeout(
            job_root,
            roots,
            {"path": receipt_name, "sha256": closeout.get("sha256"), "bytes": closeout.get("bytes")},
            managed_data_root=layout.host_data_root,
            logical_runs_roots=(layout.host_runs_root,),
        )
    except (OSError, codex_upgrade_evidence_permissions.EvidencePermissionError) as error:
        raise ConfigurationError(f"复用 Job {job_id} 的 Job 级收口重放未通过（证据在收口之后被改动）：{error}") from error
    for row in result.get("relay_checks") or []:
        manifest = row.get("manifest") if isinstance(row, Mapping) and isinstance(row.get("manifest"), Mapping) else {}
        path = capture._host_path(str(manifest.get("path", "")), layout)
        if row.get("status") != "passed" or path.is_symlink() or not path.is_file() or file_sha256(path) != manifest.get("sha256"):
            raise ConfigurationError(f"复用 Job {job_id} 的 {row.get('probe_id')} relay 清单缺失、未通过或已漂移。")


def recovery_plan(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    source_attempt_id: str,
    jobs: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """冻结失败（或被作废、被强杀）官方 attempt 的续跑闭集；只读：不取租约、不写文件、不发请求、不采探针。"""

    campaign_dir = Path(campaign_dir)
    source_root = _attempt_root(campaign_dir, source_attempt_id)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ConfigurationError(f"Campaign 内没有官方 attempt：{source_attempt_id}")
    reservation = claude_upgrade._load_capture_reservation(campaign_dir, source_root, phase=PHASE, candidate_id=None, _manifest=manifest)
    planned = {str(row["id"]): str(row["execution_sha256"]) for row in reservation["planned_jobs"]}
    jobs = list(jobs) if jobs is not None else claude_upgrade._campaign_jobs(campaign_dir, dict(manifest), PHASE)
    if {job.job_id: claude_upgrade._job_execution_sha256(job) for job in jobs} != planned:
        raise ConfigurationError("Campaign 冻结的 official Job 与来源 attempt 的预约计划不一致（ID 或执行摘要漂移），禁止续跑。")
    attempt: dict[str, Any] | None = None
    if (source_root / "attempt.json").exists() or (source_root / "attempt.json").is_symlink():
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, PHASE, None, source_attempt_id, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
        )
    status = str(attempt.get("status")) if attempt is not None else "reserved_or_interrupted"
    key = (PHASE, None, source_attempt_id)
    rerun_all = key in capture._invalidated_attempts(campaign_dir)
    impact = claude_upgrade._attempt_evolution_impact(campaign_dir, manifest, source_root, phase=PHASE, candidate_id=None)
    if attempt is None:
        if capture._attempt_reconciled_terminal(campaign_dir, source_attempt_id) is None:
            raise ConfigurationError(f"被中断的 attempt {source_attempt_id} 尚未对账：先 reconcile-attempt 入账并批准恢复预览。")
    elif status == "environment_contaminated":
        if key not in claude_upgrade._isolation_invalidated_attempts(campaign_dir):
            raise ConfigurationError("环境污染 attempt 尚未隔离：先修复环境并 environment-isolate，再对账续跑。")
    elif status == "awaiting_receipts":
        evolved = claude_upgrade._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, source_root, attempt, phase=PHASE, candidate_id=None
        )
        if not rerun_all and not evolved:
            raise ConfigurationError(
                f"attempt {source_attempt_id} 正等待 seal、没有被作废的作业，不是续跑来源；按正常流程 capture-official seal。"
            )
    elif status != "failed":
        raise ConfigurationError(f"attempt 状态 {status} 不能作为续跑来源。")
    with reconciler._client_scope(campaign_dir, _adapter()):
        try:
            _records, latest, _chain = reconciler._checkpoint_facts(source_root)
            jobs_facts = reconciler._classify_jobs(campaign_dir, manifest, reservation, attempt, latest, source_root)
            environment = reconciler._environment_facts(campaign_dir, source_root, attempt, [])
        except reconciler.ReconcilerError as error:
            raise ConfigurationError(str(error)) from error
    complete = sorted(jobs_facts["groups"]["complete"])
    reuse = list(complete) if attempt is not None and environment["status"] == "restored" else []
    evolution_invalidated = sorted(set(reuse) & set(impact["affected_job_ids"]))
    reuse = sorted(set(reuse) - set(evolution_invalidated))
    continuity_invalidated: list[str] = []
    if reuse and claude_upgrade._attempt_continuity_drift_receipt(campaign_dir, source_attempt_id) is not None:
        continuity_invalidated, reuse = list(reuse), []
    execute = sorted(set(planned) - set(reuse))
    reuse_results: dict[str, dict[str, Any]] = {}
    if reuse:
        layout = capture._capture_layout(campaign_dir, manifest)
        results = {str(item.get("id")): item for item in (attempt or {}).get("results", []) if isinstance(item, Mapping)}
        for job_id in reuse:
            result = results[job_id]
            producer = _producer(campaign_dir, source_root, result)
            _verify_reusable_evidence(campaign_dir, result, producer, layout)
            reused = copy.deepcopy(dict(result))
            receipt_path = source_root / f"job-{job_id}.json"
            reused.update(
                {
                    "disposition": "reused",
                    "job_closure": None,
                    "reused_from": {
                        "attempt_id": source_attempt_id,
                        "attempt": _binding(campaign_dir, source_root / "attempt.json"),
                        "job_receipt": _binding(campaign_dir, receipt_path) if receipt_path.is_file() else None,
                        "producer": producer,
                    },
                }
            )
            reuse_results[job_id] = reused
    return {
        "source_attempt_id": source_attempt_id,
        "source_status": status,
        "source_reservation": _binding(campaign_dir, source_root / "reservation.json"),
        "source_attempt": _binding(campaign_dir, source_root / "attempt.json") if attempt is not None else None,
        "source_identity": dict(attempt["identity"]) if attempt is not None and isinstance(attempt.get("identity"), Mapping) else None,
        "planned_job_ids": sorted(planned),
        "complete_job_ids": complete,
        "reuse_job_ids": reuse,
        "execute_job_ids": execute,
        "invalidated": {
            "tool_evolution": evolution_invalidated,
            "rerun_all": rerun_all,
            "continuity_drift": continuity_invalidated,
            "environment_status": environment["status"],
        },
        "reuse_results": reuse_results,
    }


def recovery_reuse_check(
    campaign_dir: Path,
    *,
    phase: str,
    candidate_id: str | None,
    source_attempt_id: str,
    reuse_job_ids: Iterable[str],
    execute_job_ids: Iterable[str],
) -> dict[str, Any]:
    """R17：按 resume 同一复用判定只读复算恢复预览；resume 会拒绝的情形原样抛 ``ConfigurationError``。"""

    if phase != PHASE or candidate_id is not None:
        raise claude_upgrade._pending("recovery_reuse_check（候选阶段）", "B4-01（候选采集 attempt）")
    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    claude_upgrade._reject_contaminated_campaign(campaign_dir)
    if capture._official_sealed(campaign_dir):
        raise ConfigurationError("官方证据已经封存，禁止续跑。")
    require_current_source(campaign_dir, manifest, str(source_attempt_id))
    plan = recovery_plan(campaign_dir, manifest, source_attempt_id=str(source_attempt_id))
    preview_reuse = sorted({str(item) for item in reuse_job_ids})
    preview_execute = sorted({str(item) for item in execute_job_ids})
    if preview_reuse != plan["reuse_job_ids"] or preview_execute != plan["execute_job_ids"]:
        raise ConfigurationError(
            "恢复预览与续跑冻结闭集不一致："
            f"预览复用 {preview_reuse}、执行 {preview_execute}；resume 复用 {plan['reuse_job_ids']}、执行 {plan['execute_job_ids']}"
        )
    return {
        "status": "consistent",
        "source_attempt_id": str(source_attempt_id),
        "reuse_job_ids": plan["reuse_job_ids"],
        "execute_job_ids": plan["execute_job_ids"],
        "live_request_count": 0,
    }


# ---------------------------------------------------------------------------
# 残留取证进程：识别、列出与清理（docker 边界在两个函数里，离线测试替换它们）
# ---------------------------------------------------------------------------

# 在采集容器内执行的一次性脚本：按与环境探针相同的标签规则列出取证进程（PID、启动时刻、标签；不记命令行原文），
# kill 模式只对 PID 与启动时刻都与批准清单一致的进程先 TERM 后 KILL。
_PROCESS_SCRIPT = r"""
import json, os, signal, sys, time
args = json.loads(sys.argv[1])
own = {os.getpid(), os.getppid()}
def describe(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read().decode("ascii", "replace")
    except OSError:
        return None
    parts = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
    if not parts:
        return None
    labels = set()
    first = os.path.basename(parts[0])
    if first in args["binary_labels"]:
        labels.add(args["binary_labels"][first])
    for item in parts:
        base = os.path.basename(item)
        if base in args["script_labels"]:
            labels.add(args["script_labels"][base])
    if not labels:
        return None
    fields = stat.rsplit(")", 1)[-1].split()
    start = int(fields[19]) if len(fields) > 19 else None
    return {"pid": pid, "start_ticks": start, "labels": sorted(labels)}
def listing():
    rows = []
    for name in os.listdir("/proc"):
        if name.isdigit() and int(name) not in own:
            row = describe(int(name))
            if row is not None:
                rows.append(row)
    return sorted(rows, key=lambda row: row["pid"])
if args["mode"] == "list":
    print(json.dumps({"processes": listing()}, sort_keys=True))
    sys.exit(0)
results = []
for target in args["targets"]:
    current = describe(int(target["pid"]))
    if current is None:
        results.append({**target, "outcome": "already_gone", "signals": []})
        continue
    if current["start_ticks"] != target["start_ticks"] or current["labels"] != target["labels"]:
        results.append({**target, "outcome": "identity_changed", "signals": []})
        continue
    signals = []
    for name, grace in (("TERM", args["term_grace"]), ("KILL", args["kill_grace"])):
        try:
            os.kill(int(target["pid"]), getattr(signal, "SIG" + name))
        except ProcessLookupError:
            break
        signals.append(name)
        deadline = time.monotonic() + float(grace)
        while time.monotonic() < deadline:
            again = describe(int(target["pid"]))
            if again is None or again["start_ticks"] != target["start_ticks"]:
                break
            time.sleep(0.1)
        again = describe(int(target["pid"]))
        if again is None or again["start_ticks"] != target["start_ticks"]:
            break
    final = describe(int(target["pid"]))
    gone = final is None or final["start_ticks"] != target["start_ticks"]
    results.append({**target, "outcome": "terminated" if gone else "survived", "signals": signals})
print(json.dumps({"results": results}, sort_keys=True))
"""


def _process_script_arguments(mode: str, **extra: Any) -> str:
    return json.dumps(
        {
            "mode": mode,
            "binary_labels": capture.CAPTURE_PROCESS_BINARY_LABELS,
            "script_labels": capture.CAPTURE_PROCESS_SCRIPT_LABELS,
            **extra,
        },
        sort_keys=True,
    )


def _container_command(argv: Sequence[str], *, operation: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=CLEANUP_COMMAND_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(f"{operation} 执行失败：{type(error).__name__}") from error
    if completed.returncode != 0:
        raise ConfigurationError(f"{operation} 执行失败（退出码 {completed.returncode}）。")
    return completed


def _container_id(container: str) -> str:
    completed = _container_command(["docker", "inspect", "--format", "{{.Id}}", container], operation=f"cleanup:{container}:inspect")
    value = completed.stdout.strip()
    if not capture.CONTAINER_ID_RE.fullmatch(value):
        raise ConfigurationError(f"采集容器 {container} 的 ID 非法。")
    return value


def list_residual_processes(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """docker 边界：两个采集容器里的取证进程（角色、容器名与 ID、PID、启动时刻、标签）。"""

    rows: list[dict[str, Any]] = []
    for role, container in capture._probe_targets(manifest["configuration"]):
        container_id = _container_id(container)
        completed = _container_command(
            ["docker", "exec", container, "python3", "-c", _PROCESS_SCRIPT, _process_script_arguments("list")],
            operation=f"cleanup:{container}:list",
        )
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"采集容器 {container} 的进程清单不是合法 JSON。") from error
        for item in observed.get("processes") or []:
            rows.append(
                {
                    "role": role,
                    "container": container,
                    "container_id": container_id,
                    "pid": int(item["pid"]),
                    "start_ticks": item.get("start_ticks"),
                    "labels": sorted(str(label) for label in item.get("labels") or []),
                }
            )
    return sorted(rows, key=lambda row: (row["role"], row["pid"]))


def terminate_residual_processes(manifest: Mapping[str, Any], targets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """docker 边界：按批准清单逐容器先 TERM 后 KILL（只动 PID 与启动时刻都一致的进程），返回逐进程结果。"""

    results: list[dict[str, Any]] = []
    containers = dict(capture._probe_targets(manifest["configuration"]))
    for role in sorted({str(item["role"]) for item in targets}):
        container = containers[role]
        if _container_id(container) != {str(item["container_id"]) for item in targets if item["role"] == role}.pop():
            raise ConfigurationError(f"采集容器 {container} 在批准之后被重建，残留进程清单作废；重新预览。")
        rows = [
            {"pid": int(item["pid"]), "start_ticks": item.get("start_ticks"), "labels": list(item.get("labels") or [])}
            for item in targets
            if item["role"] == role
        ]
        completed = _container_command(
            [
                "docker", "exec", container, "python3", "-c", _PROCESS_SCRIPT,
                _process_script_arguments("kill", targets=rows, term_grace=CLEANUP_TERM_GRACE_SECONDS, kill_grace=CLEANUP_KILL_GRACE_SECONDS),
            ],
            operation=f"cleanup:{container}:terminate",
        )
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"采集容器 {container} 的清理结果不是合法 JSON。") from error
        for item in observed.get("results") or []:
            results.append({"role": role, "container": container, **dict(item)})
    return sorted(results, key=lambda row: (row["role"], int(row["pid"])))


def identify_residual_processes(campaign_dir: Path, manifest: Mapping[str, Any], attempt_id: str) -> dict[str, Any]:
    """对账时识别：两个采集容器采一次只读探针，留证在对账目录下；探针采不到时记 probe_failed，不改变对账结论。"""

    stamp = claude_upgrade._utc_now().replace(":", "").replace("-", "").replace(".", "")
    subject = campaign_dir / "control" / reconciler.RECONCILIATION_DIR / f"attempt-{attempt_id}"
    output_dir = subject / RESIDUAL_PROBE_DIRNAME / stamp
    try:
        capture._private_directory(output_dir.parent, campaign_dir)
        probe = capture._probe_capture_environment(dict(manifest), output_dir, "before")
    except (ConfigurationError, OSError, incremental_recovery.IncrementalRecoveryError) as error:
        return {"status": "probe_failed", "reason": f"{type(error).__name__}: {str(error)[:300]}", "capture_processes": None}
    manifest_path = output_dir / capture.PROBE_MANIFEST_NAME
    processes = list(probe.get("capture_processes") or [])
    return {
        "status": "residual" if processes else "clean",
        "capture_processes": processes,
        "probe_manifest": _binding(campaign_dir, manifest_path),
        "observed_at_utc": probe.get("observed_at_utc"),
    }


def _require_no_residual_processes(manifest: Mapping[str, Any], deadline: Any) -> None:
    """resume 预约前的零写入门禁：两个采集容器的只读快照里不得有取证进程。"""

    states = capture._collect_probe_states(dict(manifest), deadline=deadline, heartbeat=None)
    processes = list(states["processes"]["capture_processes"])
    if processes:
        raise ConfigurationError(
            f"采集容器存在残留取证进程 {processes}：直接续跑会被判环境污染。先 environment-cleanup preview（--recovery-preview "
            "<已批准的恢复预览>）列出进程，按 --approve-sha256 批准后清理并留证，再 resume；本次未预约、未发请求。"
        )


# ---------------------------------------------------------------------------
# environment-cleanup
# ---------------------------------------------------------------------------


def _approved_recovery_preview(campaign_dir: Path, manifest: Mapping[str, Any], preview_path: Path) -> dict[str, Any]:
    """只读核对已批准的恢复预览（不消费授权）：位置、自摘要、零请求边界、批准收据；来源必须仍是最新的官方 attempt。"""

    if preview_path.is_symlink() or not preview_path.is_file():
        raise ConfigurationError(f"恢复预览不存在或不可信：{preview_path}")
    resolved = preview_path.resolve(strict=True)
    root = (campaign_dir / "control" / reconciler.RECONCILIATION_DIR).resolve(strict=False)
    if root not in resolved.parents or not reconciler.PREVIEW_RE.fullmatch(resolved.name):
        raise ConfigurationError("恢复预览必须位于本 Campaign 的 control/reconciliation/attempt-<id>/ 下。")
    preview = claude_upgrade._read_json(resolved, "恢复预览")
    unsigned = {key: value for key, value in preview.items() if key not in {"created_at_utc", "review_sha256"}}
    if (
        preview.get("schema_version") != reconciler.RECOVERY_PREVIEW_SCHEMA
        or claude_upgrade._fingerprint(unsigned) != preview.get("review_sha256")
        or preview.get("phase") != PHASE
        or preview.get("candidate_id") is not None
        or preview.get("recovery_revision") is not None
        or preview.get("live_request_count") != 0
        or resolved.parent.name != f"attempt-{preview.get('source_attempt_id')}"
    ):
        raise ConfigurationError("恢复预览自摘要、阶段、零请求边界或位置不满足。")
    approval: dict[str, Any] | None = None
    for child in sorted(resolved.parent.iterdir()):
        if not reconciler.APPROVAL_RE.fullmatch(child.name):
            continue
        payload = claude_upgrade._read_json(child, "恢复批准收据")
        signed = {key: value for key, value in payload.items() if key not in {"approved_at_utc", "receipt_sha256"}}
        if (
            payload.get("schema_version") == reconciler.RECOVERY_APPROVAL_SCHEMA
            and payload.get("preview_sha256") == file_sha256(resolved)
            and payload.get("approved_sha256") == preview.get("review_sha256")
            and payload.get("receipt_sha256") == claude_upgrade._fingerprint(signed)
        ):
            approval = payload
    if approval is None:
        raise ConfigurationError("恢复预览尚未批准；先 reconcile-attempt --approve-recovery-sha256 <review_sha256>。")
    require_current_source(campaign_dir, manifest, str(preview["source_attempt_id"]))
    return {
        "kind": "recovery_approval",
        "source_attempt_id": str(preview["source_attempt_id"]),
        "recovery_preview": _binding(campaign_dir, resolved),
        "review_sha256": str(preview["review_sha256"]),
        "recovery_approval": {"index": approval.get("index"), "receipt_sha256": approval.get("receipt_sha256")},
    }


def _cleanup_basis(campaign_dir: Path, manifest: Mapping[str, Any], recovery_preview: Path | None) -> dict[str, Any]:
    if recovery_preview is not None:
        return _approved_recovery_preview(campaign_dir, manifest, Path(recovery_preview))
    records = claude_upgrade._campaign_contamination_records(campaign_dir, _manifest=manifest)
    if not records:
        raise ConfigurationError(
            "环境清理只在明确的恢复批准（--recovery-preview 指向本 Campaign 已批准的恢复预览）或未隔离的环境污染之下进行。"
        )
    return {"kind": "environment_contamination", "records": sorted(records)}


def _cleanup_chain(campaign_dir: Path) -> list[dict[str, Any]]:
    directory = campaign_dir / CLEANUP_DIR
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ConfigurationError("环境清理收据目录不可信。")
    indexed = sorted(
        (int(match.group(1)), child)
        for child in directory.iterdir()
        if (match := CLEANUP_RECEIPT_RE.fullmatch(child.name)) is not None
    )
    chain: list[dict[str, Any]] = []
    previous: str | None = None
    for position, (index, path) in enumerate(indexed, start=1):
        receipt = claude_upgrade._read_json(path, "环境清理收据")
        unsigned = {key: value for key, value in receipt.items() if key != "cleanup_sha256"}
        if (
            index != position
            or receipt.get("schema_version") != CLEANUP_RECEIPT_SCHEMA
            or receipt.get("index") != index
            or receipt.get("previous_cleanup_sha256") != previous
            or claude_upgrade._fingerprint(unsigned) != receipt.get("cleanup_sha256")
        ):
            raise ConfigurationError(f"环境清理收据序号、成链或自摘要不一致：{path.name}")
        previous = str(receipt["cleanup_sha256"])
        chain.append(receipt)
    return chain


def _cleanup_preview(campaign_dir: Path, manifest: Mapping[str, Any], basis: Mapping[str, Any], processes: Sequence[Mapping[str, Any]], reason: str) -> dict[str, Any]:
    preview = {
        "schema_version": CLEANUP_PREVIEW_SCHEMA,
        "client": CLIENT,
        "campaign_id": str(manifest["campaign_id"]),
        "basis": dict(basis),
        "processes": [dict(item) for item in processes],
        "reason": reason,
        "live_request_count": 0,
    }
    preview["review_sha256"] = claude_upgrade._fingerprint(preview)
    return preview


def environment_cleanup_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``environment-cleanup preview|apply``：在明确批准下清理两个采集容器里的残留取证进程，前后探针与清理收据留证。"""

    action = str(arguments.cleanup_action)
    campaign_dir = _resolved_campaign(arguments.campaign_dir)
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    reason = str(getattr(arguments, "reason", "") or "").strip()
    if not reason:
        raise ConfigurationError("--reason 不能为空：写明残留进程从哪次中断留下。")
    recovery_preview = getattr(arguments, "recovery_preview", None)
    basis = _cleanup_basis(campaign_dir, manifest, recovery_preview)
    if action == "preview":
        processes = list_residual_processes(manifest)
        if not processes:
            return {"status": "clean", "campaign_id": manifest["campaign_id"], "basis": basis, "processes": [], "live_request_count": 0}, 0
        preview = _cleanup_preview(campaign_dir, manifest, basis, processes, reason)
        return {
            "status": "approval_required",
            **preview,
            "next_command": "environment-cleanup apply（同样参数）--approve-sha256 <review_sha256> --approved-by <批准人>",
        }, 0
    approve = getattr(arguments, "approve_sha256", None)
    approved_by = str(getattr(arguments, "approved_by", "") or "").strip()
    if not isinstance(approve, str) or not capture.SHA256_RE.fullmatch(approve) or not approved_by:
        raise ConfigurationError("environment-cleanup apply 必须提供预览输出的 --approve-sha256 与 --approved-by。")
    deadline = incremental_recovery.WallClockDeadline(CLEANUP_WALL_SECONDS, label="environment-cleanup")
    deadline.heartbeat_seconds = CLEANUP_HEARTBEAT_SECONDS  # type: ignore[attr-defined]
    with claude_upgrade.CampaignLease(
        campaign_dir,
        phase=PHASE,
        candidate_id=None,
        deadline=deadline,
        command="environment-cleanup",
        allow_stale_recovery=True,
        campaign_id=str(manifest["campaign_id"]),
    ):
        with claude_upgrade._campaign_lock(campaign_dir):
            basis = _cleanup_basis(campaign_dir, manifest, recovery_preview)
            processes = list_residual_processes(manifest)
            preview = _cleanup_preview(campaign_dir, manifest, basis, processes, reason)
            if not processes:
                raise ConfigurationError("当前两个采集容器里没有残留取证进程，无需清理（批准作废）。")
            if preview["review_sha256"] != approve:
                raise ConfigurationError("残留进程清单或批准依据在预览之后已变化，批准作废；重新预览后再批准。")
            chain = _cleanup_chain(campaign_dir)
            index = len(chain) + 1
            directory = capture._private_directory(campaign_dir / CLEANUP_DIR, campaign_dir)
            evidence = capture._private_directory(directory / f"cleanup-{index:02d}", campaign_dir)
            before = capture._probe_capture_environment(dict(manifest), evidence / "before", "before")
            results = terminate_residual_processes(manifest, processes)
            after = capture._probe_capture_environment(dict(manifest), evidence / "after", "after")
            remaining = list(after.get("capture_processes") or [])
            cleaned = not remaining and all(item.get("outcome") in {"terminated", "already_gone"} for item in results)
            receipt = {
                "schema_version": CLEANUP_RECEIPT_SCHEMA,
                "client": CLIENT,
                "campaign_id": str(manifest["campaign_id"]),
                "index": index,
                "previous_cleanup_sha256": chain[-1]["cleanup_sha256"] if chain else None,
                "status": "cleaned" if cleaned else "incomplete",
                "basis": basis,
                "reason": reason,
                "approved_sha256": approve,
                "approved_by": approved_by,
                "processes": [dict(item) for item in processes],
                "results": results,
                "before_probe": _binding(campaign_dir, evidence / "before" / capture.PROBE_MANIFEST_NAME),
                "after_probe": _binding(campaign_dir, evidence / "after" / capture.PROBE_MANIFEST_NAME),
                "before_capture_processes": list(before.get("capture_processes") or []),
                "after_capture_processes": remaining,
                "observed_at_utc": after.get("observed_at_utc"),
                "recorded_at_utc": claude_upgrade._utc_now(),
                "live_request_count": 0,
            }
            receipt["cleanup_sha256"] = claude_upgrade._fingerprint(receipt)
            receipt_path = directory / f"cleanup-{index:02d}.json"
            claude_upgrade._secure_write_json_once(receipt_path, receipt)
    if basis["kind"] == "recovery_approval":
        next_command = f"resume --rerun-failed --recovery-preview {campaign_dir / basis['recovery_preview']['path']} --acknowledge-live-requests"
    else:
        next_command = (
            f"以 {receipt_path} 作 --environment-repair-receipt、以 {campaign_dir / receipt['after_probe']['path']} 作 "
            "--clean-environment-receipt 执行 environment-isolate preview/apply"
        )
    result = {
        "status": receipt["status"],
        "receipt_path": str(receipt_path),
        "cleanup_sha256": receipt["cleanup_sha256"],
        "results": results,
        "after_capture_processes": remaining,
        "next_command": next_command if cleaned else "仍有进程未清理（见 results／after_capture_processes）：人工处置后重新预览",
        "live_request_count": 0,
    }
    return result, 0 if cleaned else 3


# ---------------------------------------------------------------------------
# 对账与入账（共享流程在 Claude 适配器作用域里转调）
# ---------------------------------------------------------------------------


def reconcile_attempt_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``reconcile-attempt``：共享对账之后识别两个采集容器里的残留取证进程（只读探针留证）。"""

    if getattr(arguments, "recovery_revision", None) is not None:
        raise claude_upgrade._pending("reconcile-attempt --recovery-revision（候选恢复段）", "B4-05（评估失败局部恢复）")
    campaign_dir = _resolved_campaign(arguments.campaign_dir)
    arguments.campaign_dir = campaign_dir
    with upgrade_control_plane.adapter_scope(_adapter()):
        result = upgrade_control_plane._reconcile_attempt_command(arguments)
    if getattr(arguments, "authorize_recovery_preview", None) is None:
        manifest = claude_upgrade._require_formal_campaign(campaign_dir)
        residual = identify_residual_processes(campaign_dir, manifest, str(arguments.attempt_id))
        result["residual_processes"] = residual
        if residual["status"] == "residual":
            result["next_command"] = (
                f"采集容器残留取证进程 {residual['capture_processes']}（已留证）：resume 之前先 environment-cleanup preview/apply"
                "（--recovery-preview <已批准的恢复预览>）在恢复批准下清理并留证，否则续跑在预约前拒绝；"
                + str(result.get("next_command", ""))
            )
    return result, 0 if result.get("status") in {"recoverable", "authorized"} else 3


def reconcile_supervisor_run_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``reconcile-supervisor-run``：共享对账（无预约的父监督器 run），可恢复为 0。"""

    arguments.campaign_dir = _resolved_campaign(arguments.campaign_dir)
    with upgrade_control_plane.adapter_scope(_adapter()):
        result = upgrade_control_plane._reconcile_supervisor_run_command(arguments)
    return result, 0 if result.get("status") == "recoverable" else 3


def account_sealed_official_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``account-sealed-official``：采集路径封存的请求入总账（共享）；只读复用封存零请求，没有可入账的请求。"""

    campaign_dir = _resolved_campaign(arguments.campaign_dir)
    arguments.campaign_dir = campaign_dir
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    stage = claude_upgrade._load_stage_result(campaign_dir, "capture-official", None, _verified_campaign_manifest=manifest)
    if stage.get("mode") == "predecessor_reuse":
        return {
            "status": "not_applicable",
            "campaign_id": manifest["campaign_id"],
            "reason": "官方阶段以前序证据只读复用零请求封存，本 Campaign 没有新的官方请求可入账",
            "live_request_count": 0,
        }, 0
    with upgrade_control_plane.adapter_scope(_adapter()):
        return upgrade_control_plane._account_sealed_official_command(arguments), 0


# ---------------------------------------------------------------------------
# campaign_status（官方阶段；候选阶段的状态随候选采集 attempt 由 B4-01 完善）
# ---------------------------------------------------------------------------

STATUS_SCHEMA = "claude-upgrade-status/v1"


def _attempt_summary(campaign_dir: Path, manifest: Mapping[str, Any], attempt_root: Path) -> dict[str, Any]:
    """一个官方 attempt 的只读摘要（不重放证据边界）：状态与作废、隔离、连续性漂移等续跑判定事实。"""

    attempt_id = attempt_root.name
    if not (attempt_root / "attempt.json").exists():
        reconciled = capture._attempt_reconciled_terminal(campaign_dir, attempt_id) is not None
        return {"attempt_id": attempt_id, "status": "reconciled_interrupted" if reconciled else "reserved_or_interrupted"}
    _root, attempt = claude_upgrade._load_capture_attempt(
        campaign_dir, PHASE, None, attempt_id, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
    )
    summary: dict[str, Any] = {
        "attempt_id": attempt_id,
        "status": attempt.get("status"),
        "failed_job_ids": claude_upgrade._failed_job_ids(attempt.get("results")),
        "recovery_source_attempt_id": (attempt.get("recovery") or {}).get("source_attempt_id") if isinstance(attempt.get("recovery"), Mapping) else None,
    }
    key = (PHASE, None, attempt_id)
    if attempt.get("status") == "awaiting_receipts" and not summary["failed_job_ids"]:
        invalidated = claude_upgrade._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, attempt_root, attempt, phase=PHASE, candidate_id=None
        )
        if invalidated:
            summary["evolution_invalidated_job_ids"] = invalidated
    if key in claude_upgrade._isolation_invalidated_attempts(campaign_dir):
        summary["isolation_invalidated"] = True
    if key in claude_upgrade._conflict_quarantined_attempts(campaign_dir):
        summary["evidence_conflict_quarantine"] = True
    if claude_upgrade._attempt_continuity_drifted(attempt):
        summary["continuity_drift"] = True
    return summary


def campaign_status(campaign_dir: Path, candidate_id: str | None = None) -> dict[str, Any]:
    """官方阶段的 Campaign 状态与下一命令（只读浅检查）；状态名与 Codex ``campaign_status`` 的官方阶段一致。"""

    if candidate_id is not None:
        raise claude_upgrade._pending("campaign_status（候选阶段）", "B4-01（候选采集 attempt）")
    campaign_dir = Path(campaign_dir)
    manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
    mode, purpose = claude_upgrade._campaign_coordinates(manifest)
    base = {
        "schema_version": STATUS_SCHEMA,
        "verification_mode": "shallow",
        "campaign_id": manifest["campaign_id"],
        "campaign_mode": mode,
        "campaign_purpose": purpose,
        "live_request_count": 0,
    }
    if mode != "formal":
        return {
            **base,
            "status": "preflight_complete",
            "next_command": "Formal Campaign 只由 VC-0 收口创建：python3 -m tools.official_client_capture.claude_upgrade_vc0_closeout",
        }
    candidates_root = campaign_dir / "candidates"
    if candidates_root.is_dir() and not candidates_root.is_symlink() and any(child.is_dir() for child in candidates_root.iterdir()):
        raise claude_upgrade._pending("campaign_status（候选阶段已开始）", "B4-01（候选采集 attempt）")
    decision = claude_upgrade._campaign_environment_decision(campaign_dir, _manifest=manifest)
    sealed = capture._official_sealed(campaign_dir)
    ordered = claude_upgrade._ordered_capture_attempts(campaign_dir, PHASE, None, _manifest=manifest)
    sealed_attempt_id: str | None = None
    if sealed:
        stage = claude_upgrade._load_stage_result(campaign_dir, "capture-official", None, _verified_campaign_manifest=manifest)
        reference = stage.get("attempt")
        sealed_attempt_id = Path(str(reference.get("path", ""))).parent.name if isinstance(reference, Mapping) else None
    invalidated = capture._invalidated_attempts(campaign_dir)
    summaries = [_attempt_summary(campaign_dir, manifest, root) for root, _reservation in ordered]
    unsealed = [
        f"official:{item['attempt_id']}" + (":reserved_or_interrupted" if item["status"] == "reserved_or_interrupted" else "")
        for item in summaries
        if item["status"] == "reserved_or_interrupted"
        or (
            item["status"] == "awaiting_receipts"
            and item["attempt_id"] != sealed_attempt_id
            and (PHASE, None, item["attempt_id"]) not in invalidated
            and not item.get("evolution_invalidated_job_ids")
            and not _attempt_evolution_reconciled(campaign_dir, item["attempt_id"])
        )
    ]
    failed = [f"official:{item['attempt_id']}" for item in summaries if item["status"] in {"failed", "environment_contaminated"}]
    latest = summaries[0] if summaries else None
    if decision["status"] != "clear":
        status = "environment_contaminated"
        next_command = (
            "官方已封存且官方侧存在未隔离污染：Campaign 内无法重采官方证据，只读保留"
            if decision["status"] == "official_sealed_contaminated"
            else "修复环境（残留取证进程先 environment-cleanup 在明确批准下清理并留证）并取得晚于污染的干净环境复核后，"
            "environment-isolate preview/apply 隔离污染 attempt，再对账续跑"
        )
    elif sealed and unsealed:
        status = "capture_state_inconsistent"
        next_command = "官方已封存却还有未封存的预约或 attempt：人工审计"
    elif sealed:
        status = "official_sealed"
        next_command = "classify（VC-2，B3-02）"
    elif latest is None:
        status = "planned"
        next_command = "capture-official run --acknowledge-live-requests"
    elif latest.get("isolation_invalidated") or latest.get("evidence_conflict_quarantine"):
        status = "official_capture_failed"
        next_command = "该 attempt 已被隔离作废（结果永不复用、永不 seal）：reconcile-attempt 入账并批准恢复预览后，resume --rerun-failed 全部重跑"
    elif latest["status"] == "reconciled_interrupted":
        status = "official_capture_failed"
        next_command = "resume --rerun-failed --recovery-preview <已批准的 recovery-preview> --acknowledge-live-requests"
    elif latest["status"] == "reserved_or_interrupted":
        status = "official_capture_interrupted"
        next_command = "先执行 reconcile-attempt；按判定与已批准预览决定 resume 或永久停线"
    elif latest["status"] == "awaiting_receipts" and latest.get("evolution_invalidated_job_ids"):
        status = "official_capture_failed"
        next_command = (
            "工具演进使已完成作业失效（" + "、".join(latest["evolution_invalidated_job_ids"]) + "），禁止 seal：reconcile-attempt"
            " 入账并批准恢复预览后，resume --rerun-failed 只重跑失效作业，再 seal"
        )
    elif latest["status"] == "awaiting_receipts":
        status = "official_awaiting_receipts"
        next_command = f"capture-official seal --attempt-id {latest['attempt_id']}（先预览，再按 review_sha256 批准）"
    elif latest.get("continuity_drift"):
        status = "official_capture_failed"
        next_command = "上一轮结束后环境已漂移（承接前失败、零请求）：reconcile-attempt 对账后恢复预览全部重跑，批准后 resume --rerun-failed"
    else:
        status = "official_capture_failed"
        next_command = "reconcile-attempt 入账并批准恢复预览后，resume --rerun-failed 只重跑失败作业"
    return {
        **base,
        "status": status,
        "official_attempt": latest,
        "official_attempts": summaries,
        "official_sealed_attempt_id": sealed_attempt_id,
        "active_unsealed_attempts": {"official": unsealed},
        "failed_attempts": {"official": failed},
        "contamination_records": list(decision["records"]),
        "next_command": next_command,
    }


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

_CANDIDATE_RESUME_FIELDS = (
    "candidate_id", "runtime_image", "candidate_image_id", "candidate_source", "build_id", "deployed_version", "profile_id",
    "profile_digest", "build_receipt", "candidate_purpose",
)
_REUSE_SOURCE_FIELDS = (
    "candidate_reuse_source_campaign_dir", "candidate_reuse_source_candidate_id", "candidate_reuse_source_attempt_id",
    "approve_candidate_reuse_sha256",
)


def _reject_unsupported_resume(arguments: argparse.Namespace) -> None:
    if any(getattr(arguments, field, None) for field in _CANDIDATE_RESUME_FIELDS):
        raise claude_upgrade._pending("resume（候选阶段）", "B4-01（候选采集 attempt）")
    if any(getattr(arguments, field, None) for field in _REUSE_SOURCE_FIELDS):
        raise claude_upgrade._pending("resume 的分类事实纠正后继候选复用", "B4-01（候选采集 attempt）")
    if getattr(arguments, "assertions", None) or getattr(arguments, "external_gate_root", None) or getattr(arguments, "external_gate_receipt", None):
        raise claude_upgrade._pending("resume compare／accept", "B4-03／B4-06（Claude 评估器与 accept 门禁）")
    if getattr(arguments, "preview_recovery", False):
        raise claude_upgrade._pending("resume --preview-recovery（候选零请求续跑预览）", "B4-01（候选采集 attempt）")


def resume_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``resume``：按 Campaign 状态续跑（官方阶段）。失败或已对账中断的 attempt 只经已批准的恢复预览 ``--rerun-failed``。"""

    _reject_unsupported_resume(arguments)
    campaign_dir = _resolved_campaign(arguments.campaign_dir)
    arguments.campaign_dir = campaign_dir
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    status = claude_upgrade.campaign_status(campaign_dir)
    current = str(status["status"])
    if current == "environment_contaminated":
        raise ConfigurationError("存在未隔离的环境污染，Campaign 已封锁，不能自动 resume：" + str(status.get("next_command")))
    if current == "official_capture_interrupted":
        raise ConfigurationError("attempt 已中断且尚未对账；先执行 reconcile-attempt，按判定与已批准的恢复预览决定是否 resume --rerun-failed。")
    if current == "capture_state_inconsistent":
        raise ConfigurationError("存在额外的未封存预约：只能人工审计。")
    if current in {"official_awaiting_receipts", "official_awaiting_seal_approval"}:
        raise ConfigurationError("官方 attempt 正等待 seal；resume 不会代替 capture-official seal 的人工审核。")
    if current == "official_sealed":
        raise ConfigurationError("官方证据已封存，下一步是 VC-2 classify（B3-02）；resume 不会自动批准语义分类。")
    if not getattr(arguments, "acknowledge_live_requests", False):
        raise ConfigurationError("续跑会执行官方取证 Job 并发真实请求，必须确认 --acknowledge-live-requests。")
    if current == "planned":
        if getattr(arguments, "rerun_failed", False):
            raise ConfigurationError("Campaign 还没有官方 attempt，没有可以 --rerun-failed 的失败 attempt。")
        result = capture.run_capture_attempt(arguments, PHASE)
    elif current == "official_capture_failed":
        if not getattr(arguments, "rerun_failed", False):
            raise ConfigurationError("失败 attempt 只能显式使用 resume --rerun-failed --recovery-preview <已批准的恢复预览> 创建新 attempt。")
        if getattr(arguments, "recovery_preview", None) is None:
            raise ConfigurationError("resume --rerun-failed 必须提供 --recovery-preview（已批准的 recovery-preview/v1）。")
        result = _run_with_lease(campaign_dir, manifest, arguments)
    else:
        raise claude_upgrade._pending(f"resume（Campaign 状态 {current}）", "B4-01（候选采集 attempt：候选阶段的状态与续跑）")
    in_campaign_run = os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1"
    return result, 0 if result.get("status") == "awaiting_receipts" and in_campaign_run else 2


def _run_with_lease(campaign_dir: Path, manifest: dict[str, Any], arguments: argparse.Namespace) -> dict[str, Any]:
    """在 Campaign 租约下执行恢复 attempt：强杀留下的过期租约由本次显式续跑接管（写停线收据）。"""

    try:
        active = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
        if active is not None:
            if not claude_upgrade._lease_identity_matches(active, campaign_dir, phase=PHASE, candidate_id=None):
                raise ConfigurationError("resume 与当前 Campaign lease 的 phase／candidate 不一致。")
            deadline = capture._bind_attempt_deadline_metadata(active.deadline, PHASE)
            return _run_recovery_attempt(campaign_dir, manifest, arguments, lease=active, deadline=deadline)
        deadline = claude_upgrade._attempt_deadline(arguments, PHASE)
        with claude_upgrade.CampaignLease(
            campaign_dir,
            phase=PHASE,
            candidate_id=None,
            deadline=deadline,
            command="resume",
            allow_stale_recovery=True,
            campaign_id=str(manifest.get("campaign_id", "")),
        ) as lease:
            return _run_recovery_attempt(campaign_dir, manifest, arguments, lease=lease, deadline=deadline)
    except (incremental_recovery.IncrementalRecoveryError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(f"resume 在预约前停止：{type(error).__name__}：{error}") from error


def _consume_recovery_approval(campaign_dir: Path, preview_path: Path) -> dict[str, Any]:
    try:
        return reconciler.load_approved_recovery_preview(campaign_dir, Path(preview_path), phase=PHASE, candidate_id=None)
    except reconciler.ReconcilerError as error:
        raise ConfigurationError(str(error)) from error


def _require_plan_matches_preview(plan: Mapping[str, Any], consumed: Mapping[str, Any]) -> None:
    preview_reuse = sorted(str(item) for item in consumed.get("reuse_job_ids") or [])
    preview_execute = sorted(str(item) for item in consumed.get("execute_job_ids") or [])
    if preview_reuse != plan["reuse_job_ids"] or preview_execute != plan["execute_job_ids"]:
        raise ConfigurationError(
            "已批准的恢复预览与当场重算的续跑闭集不一致："
            f"预览复用 {preview_reuse}、执行 {preview_execute}；重算复用 {plan['reuse_job_ids']}、执行 {plan['execute_job_ids']}。"
            "按变化原因修复后重新 reconcile-attempt。"
        )


def _verify_environment_continuity(campaign_dir: Path, source_attempt_id: str, before_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """来源 attempt 的 after 探针与本轮 before 探针在承接比较的三类快照（两个容器）逐字相等，否则零请求失败。"""

    after_path = _attempt_root(campaign_dir, source_attempt_id) / "evidence" / "environment" / "after" / capture.PROBE_MANIFEST_NAME
    if after_path.is_symlink() or not after_path.is_file():
        raise EnvironmentContinuityDrift("被承接 attempt 缺少 after 环境探针，无法证明连续性。")
    previous = capture._probe_snapshot_digests(claude_upgrade._read_json(after_path, "被承接 attempt 的 after 探针"))
    current = capture._probe_snapshot_digests(before_manifest)
    drifted = [kind for kind in capture.CONTINUITY_PROBE_KINDS if previous.get(kind) != current.get(kind)]
    if drifted:
        raise EnvironmentContinuityDrift(
            "承接失败：上一轮结束后环境已漂移（" + "、".join(drifted) + "），被承接任务的证据前提不再成立；本 attempt 在任何 Job"
            "执行前失败（零请求）。reconcile-attempt 对账后恢复预览全部重跑（不承接），批准后 resume --rerun-failed。"
        )
    return {
        "schema_version": "claude-upgrade-attempt-continuity/v1",
        "source_attempt_id": source_attempt_id,
        "compared_kinds": list(capture.CONTINUITY_PROBE_KINDS),
        "source_after_probe_sha256": file_sha256(after_path),
    }


def _reserve_recovery_attempt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    jobs: Sequence[Any],
    deadline: Any,
    lease: Any,
    plan: Mapping[str, Any],
    consumed: Mapping[str, Any],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Campaign 锁内原子发布恢复 attempt：同一临时目录里写 reservation.json 与 recovery-source.json 后整体改名。"""

    deadline.check("attempt:reserve:start")
    lease.check("attempt:reserve:start")
    source_id = str(plan["source_attempt_id"])
    with claude_upgrade._campaign_lock(campaign_dir, deadline=deadline):
        if capture._official_sealed(campaign_dir):
            raise ConfigurationError("官方证据已经封存，禁止创建新 attempt。")
        require_current_source(campaign_dir, manifest, source_id)
        if not lease.acquired:
            raise ConfigurationError("reservation 缺少 active Campaign lease。")
        lease_payload = lease.payload
        if lease_payload.get("campaign_id") != manifest.get("campaign_id"):
            raise ConfigurationError("reservation 与 Campaign lease 身份不一致。")
        attempts_root = capture._private_directory(campaign_dir / capture._attempt_relative(capture.MODE_FORMAL) / "attempts", campaign_dir)
        attempt_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{secrets.token_hex(8)}"
        lease.bind_attempt(attempt_id)
        final_root = attempts_root / attempt_id
        if final_root.exists() or final_root.is_symlink():
            raise ConfigurationError("随机 attempt-id 发生冲突。")
        reservation: dict[str, Any] = {
            "schema_version": capture.CAPTURE_RESERVATION_SCHEMA,
            "client": CLIENT,
            "mode": capture.MODE_FORMAL,
            "campaign_id": manifest["campaign_id"],
            "campaign_mode": manifest["campaign_mode"],
            "campaign_purpose": manifest["campaign_purpose"],
            "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
            "phase": PHASE,
            "candidate_id": None,
            "candidate_purpose": None,
            "attempt_id": attempt_id,
            "run_nonce": secrets.token_hex(32),
            "started_at_utc": claude_upgrade._utc_now(),
            "identity_sha256": claude_upgrade._fingerprint(dict(identity)),
            "planned_jobs": [
                {"id": job.job_id, "required": job.required, "execution_sha256": capture._job_execution_sha256(job)} for job in jobs
            ],
        }
        effective = claude_upgrade._campaign_effective_tool_identity(campaign_dir, manifest)
        evolution = codex_upgrade_wire_transition.reservation_binding(effective)
        if evolution is not None:
            reservation["tool_evolution"] = evolution
        reservation["campaign_lease"] = {
            "path": claude_upgrade.CAMPAIGN_LEASE_FILENAME,
            "owner_nonce": str(lease_payload.get("owner_nonce")),
            "deadline_at_utc": str(lease_payload.get("deadline_at_utc")),
        }
        reservation["reservation_digest"] = claude_upgrade._fingerprint(reservation)
        timing_event = consumed.get("timing_recovery_event")
        source = {
            "schema_version": RECOVERY_SOURCE_SCHEMA,
            "client": CLIENT,
            "campaign_id": manifest["campaign_id"],
            "attempt_id": attempt_id,
            "reservation_digest": reservation["reservation_digest"],
            "source_attempt_id": source_id,
            "source_status": plan["source_status"],
            "source_reservation": dict(plan["source_reservation"]),
            "source_attempt": dict(plan["source_attempt"]) if plan["source_attempt"] is not None else None,
            "recovery_preview": _binding(campaign_dir, Path(str(consumed["preview_path"]))),
            "recovery_approval": {
                "index": (consumed.get("approval") or {}).get("index"),
                "receipt_sha256": (consumed.get("approval") or {}).get("receipt_sha256"),
            },
            "timing_recovery_event": (
                {"event_id": timing_event.get("event_id"), "event_sha256": timing_event.get("event_sha256")}
                if isinstance(timing_event, Mapping)
                else None
            ),
            "reuse_job_ids": list(plan["reuse_job_ids"]),
            "execute_job_ids": list(plan["execute_job_ids"]),
            "invalidated": dict(plan["invalidated"]),
            "recorded_at_utc": reservation["started_at_utc"],
        }
        source["recovery_source_sha256"] = claude_upgrade._fingerprint(source)
        temporary_root = Path(tempfile.mkdtemp(prefix=".reservation-", dir=attempts_root))
        temporary_root.chmod(0o700)
        try:
            claude_upgrade._secure_write_json_once(temporary_root / "reservation.json", reservation)
            claude_upgrade._secure_write_json_once(temporary_root / RECOVERY_SOURCE_FILENAME, source)
            os.rename(temporary_root, final_root)
        except BaseException:
            for name in ("reservation.json", RECOVERY_SOURCE_FILENAME):
                (temporary_root / name).unlink(missing_ok=True)
            try:
                temporary_root.rmdir()
            except OSError:
                pass
            raise
    return final_root, reservation, source


def load_recovery_source(campaign_dir: Path, attempt_root: Path) -> dict[str, Any] | None:
    """恢复 attempt 的来源收据（没有即 None）：字段闭集、自摘要、与本 attempt 的预约摘要一致。"""

    path = attempt_root / RECOVERY_SOURCE_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("恢复来源收据路径不可信。")
    payload = claude_upgrade._read_json(path, "恢复来源收据")
    unsigned = {key: value for key, value in payload.items() if key != "recovery_source_sha256"}
    reservation = claude_upgrade._read_json(attempt_root / "reservation.json", "抓包预约收据")
    if (
        set(payload) != RECOVERY_SOURCE_FIELDS
        or payload.get("schema_version") != RECOVERY_SOURCE_SCHEMA
        or payload.get("client") != CLIENT
        or payload.get("attempt_id") != attempt_root.name
        or payload.get("reservation_digest") != reservation.get("reservation_digest")
        or claude_upgrade._fingerprint(unsigned) != payload.get("recovery_source_sha256")
    ):
        raise ConfigurationError("恢复来源收据形态、自摘要或预约绑定不一致。")
    return payload


def _supersede_occupied_roots(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    jobs: Sequence[Any],
    layout: Any,
) -> dict[str, Any] | None:
    """执行集合作业的固定证据根已存在时先写取代收据、再改名为同级 ``<名>.superseded-<本 attempt>``（核对 inode）。"""

    registrants = claude_upgrade._evidence_root_registrants(
        campaign_dir, manifest, phase=PHASE, candidate_id=None, exclude_attempt_id=attempt_root.name
    )
    rows: list[dict[str, Any]] = []
    claimed: set[str] = set()
    for job in jobs:
        for logical in (str(item) for item in job.evidence_roots):
            host = capture._host_path(logical, layout)
            if not host.exists() and not host.is_symlink():
                continue
            if logical in claimed:
                raise ConfigurationError(f"同一证据根被执行集合中多个作业声明：{logical}")
            claimed.add(logical)
            identity = claude_upgrade._directory_identity(host)
            if identity is None:
                continue
            infix = codex_upgrade_evidence_permissions.SUPERSEDED_ROOT_INFIX + attempt_root.name
            archived_name = f"{host.name}{infix}"
            suffix = 1
            while host.with_name(archived_name).exists() or host.with_name(archived_name).is_symlink():
                suffix += 1
                archived_name = f"{host.name}{infix}-{suffix}"
            rows.append(
                {
                    "job_id": job.job_id,
                    "logical_root": logical,
                    "host_root": str(host),
                    "archived_logical_root": str(PurePosixPath(logical).with_name(archived_name)),
                    "archived_host_root": str(host.with_name(archived_name)),
                    "device": identity[0],
                    "inode": identity[1],
                    "registered_by": [dict(item) for item in registrants.get(logical, [])],
                }
            )
    if not rows:
        return None
    receipt: dict[str, Any] = {
        "schema_version": claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_SCHEMA,
        "campaign_id": str(manifest["campaign_id"]),
        "phase": PHASE,
        "candidate_id": None,
        "superseding_attempt_id": attempt_root.name,
        "recorded_at_utc": claude_upgrade._utc_now(),
        "roots": rows,
        "live_request_count": 0,
    }
    receipt["receipt_sha256"] = claude_upgrade._fingerprint(receipt)
    directory = capture._private_directory(campaign_dir / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR, campaign_dir)
    claude_upgrade._secure_write_json_once(directory / f"supersession-{attempt_root.name}.json", receipt)
    for row in rows:
        Path(row["host_root"]).rename(row["archived_host_root"])
        if claude_upgrade._directory_identity(Path(row["archived_host_root"])) != (row["device"], row["inode"]):
            raise ConfigurationError(f"证据根取代后归档目录 inode 不一致：{row['logical_root']}")
    return receipt


def _record_reused_result(
    attempt_root: Path,
    checkpoint_store: Any,
    reservation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    result: dict[str, Any],
) -> None:
    records = checkpoint_store.records()
    checkpoint_store.append(
        {
            "checkpoint_schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            "client": CLIENT,
            "mode": capture.MODE_FORMAL,
            "campaign_id": manifest["campaign_id"],
            "phase": PHASE,
            "attempt_id": attempt_root.name,
            "run_nonce": reservation["run_nonce"],
            "item_id": result["id"],
            "status": "complete",
            "disposition": "reused",
            "result_sha256": incremental_recovery.digest(result),
            "result_key": result.get("incremental_result_key"),
            "result": result,
            "previous_checkpoint_sha256": records[-1].get("checkpoint_sha256") if records else None,
        }
    )
    claude_upgrade._secure_write_json_once(attempt_root / f"job-{result['id']}.json", result)


def _run_recovery_attempt(
    campaign_dir: Path,
    manifest: dict[str, Any],
    arguments: argparse.Namespace,
    *,
    lease: Any,
    deadline: Any,
) -> dict[str, Any]:
    """执行一次恢复 attempt（正式）；attempt 合同与 B2-06 的 run 相同，复用结果只读承接，执行集合逐 Job 执行与收口。"""

    mode = capture.MODE_FORMAL
    # —— 预约前门禁（零写入；只有恢复授权的消费追加计时账本事件） ——
    claude_upgrade._reject_contaminated_campaign(campaign_dir)
    if capture._official_sealed(campaign_dir):
        raise ConfigurationError("官方证据已经封存，禁止续跑。")
    current_tool = claude_upgrade._tool_identity(include_git=False)
    claude_upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="resume --rerun-failed")
    jobs = claude_upgrade._campaign_jobs(campaign_dir, manifest, PHASE)
    layout = capture._capture_layout(campaign_dir, manifest)
    claude_upgrade._verify_execution_tree(capture._host_path(layout.capture_tool_root, layout).parents[1])
    binary_verification = claude_upgrade._verify_official_binaries(manifest, deadline=deadline)
    preconditions = capture._container_preconditions(manifest, layout, mode=mode, jobs=jobs, deadline=deadline, heartbeat=None)
    identity = capture._attempt_identity(manifest, layout, mode)
    _require_no_residual_processes(manifest, deadline)
    consumed = _consume_recovery_approval(campaign_dir, Path(arguments.recovery_preview))
    source_id = str(consumed["source_attempt_id"])
    plan = recovery_plan(campaign_dir, manifest, source_attempt_id=source_id, jobs=jobs)
    _require_plan_matches_preview(plan, consumed)
    if plan["source_identity"] is not None and dict(plan["source_identity"]) != dict(identity):
        raise ConfigurationError("来源 attempt 的官方运行坐标与当前不同，不能承接其结果。")
    reuse_ids = set(plan["reuse_job_ids"])
    execute_jobs = [job for job in jobs if job.job_id in set(plan["execute_job_ids"])]
    for job in execute_jobs:
        capture._job_tool_dependency_files(job, current_tool)
    capture._require_capture_budget(deadline, operation="attempt:reservation-admission", reservation=True)
    # —— 原子预约（此后一律写 attempt 收据） ——
    attempt_root, reservation, recovery_source = _reserve_recovery_attempt(
        campaign_dir, manifest, identity=identity, jobs=jobs, deadline=deadline, lease=lease, plan=plan, consumed=consumed
    )
    log_root = capture._private_directory(attempt_root / "logs", campaign_dir)
    evidence_root = capture._private_directory(attempt_root / "evidence", campaign_dir)
    environment_root = capture._private_directory(evidence_root / "environment", evidence_root)
    heartbeat_path = attempt_root / "watchdog-heartbeat.json"
    context = capture.ScenarioReceiptContext(
        campaign_id=str(manifest["campaign_id"]),
        attempt_id=attempt_root.name,
        run_nonce=str(reservation["run_nonce"]),
        evidence_root=evidence_root,
        campaign_dir=campaign_dir,
        attempt_root=attempt_root,
        mode=mode,
        layout=layout,
        manifest=manifest,
    )

    def heartbeat(operation: str) -> None:
        capture._write_attempt_heartbeat(heartbeat_path, deadline, operation=operation, attempt_root=attempt_root)

    results: list[dict[str, Any]] = []
    attempt_observations: list[dict[str, str]] = []
    execution_error: BaseException | None = None
    restoration_error: BaseException | None = None
    before_manifest: dict[str, Any] | None = None
    after_manifest: dict[str, Any] | None = None
    attempt_drift: list[str] = []
    checkpoint_store: Any | None = None
    timeout_checkpoint_path: Path | None = None
    contaminated = False
    continuity: dict[str, Any] | None = None
    supersession: dict[str, Any] | None = None
    try:
        checkpoint_store = incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
        capture._write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:reserved", force=True, attempt_root=attempt_root)
        deadline.check("attempt:before-probe")
        before_manifest = claude_upgrade._probe_capture_environment(dict(manifest), environment_root / "before", "before", deadline=deadline, heartbeat=heartbeat)
        if before_manifest.get("capture_processes"):
            attempt_observations.append({"check_id": "attempt.environment.processes", "failure_code": "residual"})
            raise ConfigurationError("采集容器（抓包或断网）存在残留取证进程，环境不干净，拒绝执行任何 Job。")
        if reuse_ids:
            continuity = _verify_environment_continuity(campaign_dir, source_id, before_manifest)
            for job in jobs:
                if job.job_id in reuse_ids:
                    reused = dict(plan["reuse_results"][job.job_id])
                    results.append(reused)
                    _record_reused_result(attempt_root, checkpoint_store, reservation, manifest, reused)
            capture._write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:reused-results-recorded", force=True, attempt_root=attempt_root)
        if execute_jobs:
            supersession = _supersede_occupied_roots(campaign_dir, manifest, attempt_root, execute_jobs, layout)
        for job in execute_jobs:
            capture._require_capture_budget(deadline, operation=f"job:{job.job_id}:admission")
            capture._write_attempt_heartbeat(heartbeat_path, deadline, operation=f"job:{job.job_id}:start", attempt_root=attempt_root)
            job_root = capture._private_directory(context.job_root(job.job_id), attempt_root)
            capture._private_directory(job_root / "evidence", attempt_root)
            job_log_root = capture._private_directory(job_root / "logs", attempt_root)
            result = claude_upgrade._run_job_with_retry(
                job, job_log_root, context, identity=identity, tool_identity=current_tool, deadline=deadline, heartbeat=heartbeat
            )
            closure_error: BaseException | None = None
            try:
                result["job_closure"] = capture._close_job(context, job, result)
            except ConfigurationError as error:
                closure_error = error
                result["status"] = "failed"
                result["job_closure"] = None
                result["job_closure_error"] = capture._error_record(error)
                result["failure_observations"] = capture._normalize_failure_observations(
                    [*result.get("failure_observations", []), {"check_id": f"job.{job.job_id}.evidence-permissions", "failure_code": "closeout-failed"}],
                    phase=job.phase,
                )
            results.append(result)
            records = checkpoint_store.records()
            checkpoint_store.append(
                {
                    "checkpoint_schema_version": capture.JOB_CHECKPOINT_SCHEMA,
                    "client": CLIENT,
                    "mode": mode,
                    "campaign_id": manifest["campaign_id"],
                    "phase": PHASE,
                    "attempt_id": attempt_root.name,
                    "run_nonce": reservation["run_nonce"],
                    "item_id": job.job_id,
                    "status": "complete" if result.get("status") == "complete" else "failed",
                    "disposition": "executed",
                    "result_sha256": incremental_recovery.digest(result),
                    "result_key": result.get("incremental_result_key"),
                    "result": result,
                    "previous_checkpoint_sha256": records[-1].get("checkpoint_sha256") if records else None,
                }
            )
            claude_upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
            capture._write_attempt_heartbeat(
                heartbeat_path, deadline, operation=f"job:{job.job_id}:complete", last_completed_job_id=job.job_id, force=True, attempt_root=attempt_root
            )
            if closure_error is not None:
                raise closure_error
            if result.get("environment_contaminated"):
                contaminated = True
                break
    except BaseException as error:
        execution_error = error
    finally:
        try:
            if before_manifest is not None and not deadline.expired:
                deadline.check("attempt:after-probe")
                after_manifest = claude_upgrade._probe_capture_environment(
                    dict(manifest), environment_root / "after", "after", deadline=deadline, heartbeat=heartbeat
                )
                attempt_drift = capture._probe_drift(before_manifest, after_manifest)
                if attempt_drift:
                    contaminated = True
                    attempt_observations.extend(
                        {"check_id": f"attempt.environment.{kind}", "failure_code": "drifted"} for kind in attempt_drift
                    )
        except BaseException as error:
            restoration_error = error
            if before_manifest is not None:
                contaminated = True
                attempt_observations.append({"check_id": "attempt.environment.after-probe", "failure_code": "probe-failed"})
    if isinstance(execution_error, EnvironmentContinuityDrift):
        attempt_observations.append({"check_id": "attempt.environment.continuity", "failure_code": "drifted"})
    if isinstance(execution_error, incremental_recovery.WallClockTimeoutError) or deadline.expired:
        try:
            capture._write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:timeout", force=True, allow_expired=True, attempt_root=attempt_root)
            timeout_checkpoint_path = capture._write_timeout_checkpoint(
                attempt_root,
                deadline,
                operation=getattr(execution_error, "operation", "attempt:deadline") or "attempt:deadline",
                last_completed_job_id=getattr(deadline, "last_completed_job_id", None),
            )
        except BaseException as error:
            execution_error = execution_error or error

    container_roots = sorted(
        {str(root) for result in results for root in [*(result.get("evidence_roots") or []), *(result.get("archived_evidence_roots") or [])]}
    )
    external_roots = capture._external_host_roots(container_roots, layout)
    closeout_roots = [evidence_root, log_root, *external_roots]
    permission_binding: dict[str, Any] | None = None
    permission_error: BaseException | None = None
    try:
        receipt_path, _receipt = codex_upgrade_evidence_permissions.close_evidence_permissions(
            attempt_root, closeout_roots, managed_data_root=layout.host_data_root, logical_runs_roots=(layout.host_runs_root,)
        )
        permission_binding = codex_upgrade_evidence_permissions.receipt_binding(attempt_root, receipt_path)
    except BaseException as error:
        permission_error = error
        execution_error = execution_error or error

    planned_ok = {job.job_id for job in jobs if job.required} <= {str(item.get("id")) for item in results if item.get("status") == "complete"}
    clean = execution_error is None and restoration_error is None and after_manifest is not None and permission_binding is not None
    if contaminated:
        status = "environment_contaminated"
    else:
        status = "awaiting_receipts" if clean and planned_ok else "failed"
    checkpoint_summary: dict[str, Any] | None = None
    if checkpoint_store is not None:
        try:
            records = checkpoint_store.records()
            checkpoint_summary = {
                "schema_version": capture.JOB_CHECKPOINT_SCHEMA,
                "path": (attempt_root / "checkpoints").relative_to(campaign_dir).as_posix(),
                "record_count": len(records),
                "last_sequence": records[-1].get("checkpoint_sequence") if records else None,
                "last_sha256": records[-1].get("checkpoint_sha256") if records else None,
            }
        except BaseException as error:
            execution_error = execution_error or error
    watchdog = {
        "schema_version": capture.WATCHDOG_HEARTBEAT_SCHEMA,
        "budget_seconds": deadline.budget_seconds,
        "heartbeat_seconds": getattr(deadline, "heartbeat_seconds", claude_upgrade.DEFAULT_HEARTBEAT_SECONDS),
        "elapsed_seconds": round(deadline.elapsed_seconds, 3),
        "remaining_seconds": round(deadline.remaining_seconds, 3),
        "heartbeat": (
            {"path": heartbeat_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(heartbeat_path), "bytes": heartbeat_path.stat().st_size}
            if heartbeat_path.is_file() and not heartbeat_path.is_symlink()
            else None
        ),
        "timeout_checkpoint": (
            {
                "path": timeout_checkpoint_path.relative_to(campaign_dir).as_posix(),
                "sha256": file_sha256(timeout_checkpoint_path),
                "bytes": timeout_checkpoint_path.stat().st_size,
            }
            if timeout_checkpoint_path is not None and timeout_checkpoint_path.is_file()
            else None
        ),
        "last_completed_job_id": getattr(deadline, "last_completed_job_id", None),
    }
    environment = {
        "evidence_root": str(evidence_root),
        "before_probe": capture._probe_binding(evidence_root, environment_root / "before" / capture.PROBE_MANIFEST_NAME) if before_manifest else None,
        "after_probe": capture._probe_binding(evidence_root, environment_root / "after" / capture.PROBE_MANIFEST_NAME) if after_manifest else None,
        "drifted_kinds": attempt_drift,
        "continuity_probe_kinds": list(capture.CONTINUITY_PROBE_KINDS),
    }
    attempt = capture._write_capture_attempt(
        campaign_dir,
        attempt_root,
        {
            "campaign_id": manifest["campaign_id"],
            "phase": PHASE,
            "candidate_id": None,
            "status": status,
            "identity": identity,
            "results": results,
            "attempt_failure_observations": (
                capture._normalize_failure_observations(attempt_observations, phase=PHASE) if attempt_observations else []
            ),
            "evidence_roots": [str(path) for path in closeout_roots],
            "evidence_permission_layout": {
                "managed_data_root": str(layout.host_data_root),
                "logical_runs_roots": [str(layout.host_runs_root)],
            },
            "evidence_permission_closeout": permission_binding,
            "evidence_permission_error": capture._error_record(permission_error),
            "layout": {
                "host_data_root": str(layout.host_data_root),
                "capture_root": str(layout.capture_root),
                "capture_container": layout.capture_container,
                "synthetic_capture_container": layout.synthetic_container,
                "capture_tool_root": str(layout.capture_tool_root),
                "runtime_image": layout.runtime_image,
            },
            "environment": environment,
            "binary_verification": binary_verification,
            "preconditions": preconditions,
            "watchdog": watchdog,
            "job_checkpoint": checkpoint_summary,
            "execution_error": capture._error_record(execution_error),
            "restoration_error": capture._error_record(restoration_error),
            "live_request_count_planned": None,
            "recovery": {
                "source": _binding(campaign_dir, attempt_root / RECOVERY_SOURCE_FILENAME),
                "source_attempt_id": source_id,
                "reuse_job_ids": sorted(reuse_ids),
                "execute_job_ids": sorted(job.job_id for job in execute_jobs),
                "continuity": continuity,
                "evidence_root_supersession": (
                    {"path": (campaign_dir / claude_upgrade.EVIDENCE_ROOT_SUPERSESSION_DIR / f"supersession-{attempt_root.name}.json").relative_to(campaign_dir).as_posix(), "sha256": supersession["receipt_sha256"]}
                    if supersession is not None
                    else None
                ),
            },
            "next_gate": (
                "capture-official seal（B2-07）"
                if status == "awaiting_receipts"
                else "环境隔离与恢复（environment-cleanup／environment-isolate，B2-08）"
                if status == "environment_contaminated"
                else "reconcile-attempt → 批准恢复预览 → resume --rerun-failed（B2-08）"
            ),
        },
        manifest=manifest,
        reservation=reservation,
        mode=mode,
    )
    if status == "environment_contaminated":
        try:
            claude_upgrade._secure_write_json_once(
                campaign_dir / capture.CONTAMINATION_MARKER,
                {
                    "schema_version": capture.ENVIRONMENT_CONTAMINATION_SCHEMA,
                    "client": CLIENT,
                    "mode": mode,
                    "phase": PHASE,
                    "attempt_id": attempt_root.name,
                    "drifted_kinds": attempt_drift,
                    "contaminated_jobs": sorted(str(item.get("id")) for item in results if item.get("environment_contaminated")),
                    "recorded_at_utc": claude_upgrade._utc_now(),
                },
            )
        except (ConfigurationError, OSError):
            pass
    if isinstance(execution_error, (KeyboardInterrupt, SystemExit)):
        raise execution_error
    return {
        "status": status,
        "mode": mode,
        "phase": PHASE,
        "attempt_id": attempt_root.name,
        "attempt": str(attempt_root),
        "attempt_digest": attempt["attempt_digest"],
        "run_nonce": reservation["run_nonce"],
        "started_at_utc": reservation["started_at_utc"],
        "recovery": {
            "source_attempt_id": source_id,
            "reuse_job_ids": sorted(reuse_ids),
            "execute_job_ids": sorted(job.job_id for job in execute_jobs),
            "recovery_source_sha256": recovery_source["recovery_source_sha256"],
            "evidence_root_supersession": supersession is not None,
        },
        "results": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "disposition": item.get("disposition"),
                "attempt_index": item.get("attempt_index"),
                "environment_contaminated": bool(item.get("environment_contaminated")),
            }
            for item in results
        ],
        "environment": environment,
        "execution_error": capture._error_record(execution_error),
        "next_gate": attempt["next_gate"],
    }


# ---------------------------------------------------------------------------
# 静止判据：停线租约与过期租约（B2-08 定协议，B3-06 补过期租约的停线固化与「已封存即了结」）
# ---------------------------------------------------------------------------


def _open_existing_lease_lock(campaign_dir: Path) -> int | None:
    """打开已有的租约锁文件（不创建）；口径与 ``CampaignLease._open_lock`` 相同：当前用户拥有的 0600 普通文件。"""

    lock_path = campaign_dir / claude_upgrade.CAMPAIGN_LEASE_LOCK_FILENAME
    claude_upgrade._reject_symlink_components(lock_path, campaign_dir, "Campaign lease 锁")
    try:
        descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
        os.close(descriptor)
        raise ConfigurationError("Campaign lease 锁必须是当前用户拥有的 0600 文件。")
    return descriptor


def _solidify_expired_lease(campaign_dir: Path, *, action: str) -> dict[str, Any] | None:
    """active 租约：非阻塞取租约锁，取到且已过期就写不可覆盖的停线收据（Codex 同语义），返回锁内重读的租约。

    执行者仍持锁（进程活着）即拒绝；锁空着但租约仍在有效期内（例如 PID 已被复用、心跳尚未超时）同样拒绝，等过期后再试。
    取锁期间租约变成 released／stop_the_line 时按新状态返回。租约文件本身不改写：停线收据是不可覆盖的事实，之后的取租约者
    （resume 接管、普通 run 拒绝）照 ``CampaignLease`` 原语义处理，重复发现只重放同一份收据。
    """

    descriptor = _open_existing_lease_lock(campaign_dir)
    try:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ConfigurationError(
                    f"Campaign 有未释放的执行租约，且执行者仍持有租约锁：{action}要求 Campaign 静止，等执行结束，"
                    "或执行者已挂起时按恢复协议处理。"
                ) from error
        current = claude_upgrade._read_campaign_lease(campaign_dir)
        if current is None or current.get("state") != "active":
            return current
        stale, reason, _owner_alive, _expired = claude_upgrade._campaign_lease_staleness(current)
        if not stale:
            raise ConfigurationError(
                f"Campaign 有未释放的执行租约且仍在有效期内：{action}要求 Campaign 静止；执行者仍在运行就等它结束，"
                "已被强杀则等心跳过期后重试（过期租约会固化为停线收据）。"
            )
        claude_upgrade._write_campaign_lease_stop_once(
            campaign_dir,
            current,
            reason=reason,
            next_action=(
                "执行者已退出：租约绑定的 attempt 已完成待封存时直接 capture-official seal；失败或中断的 attempt 先 "
                "reconcile-attempt，按批准的恢复预览 resume --rerun-failed"
            ),
        )
        return current
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _bound_attempt_sealed(campaign_dir: Path, lease: Mapping[str, Any]) -> bool:
    """租约绑定的官方 attempt 已经封存（官方阶段结果按采集路径绑定的就是它，且 VC-1 checkpoint 已绑定该结果）。

    已封存的 attempt 请求由 ``account-sealed-official`` 入账，不再需要对账，这份停线事实即视为了结。候选 attempt 的了结判定
    随候选证据封存（B4-02），这里一律不算了结。
    """

    bound = lease.get("attempt_id")
    if lease.get("phase") != PHASE or not isinstance(bound, str) or not bound:
        return False
    if not (campaign_dir / "official" / "result.json").is_file():
        return False
    try:
        stage = claude_upgrade._load_stage_result(campaign_dir, "capture-official")
    except ConfigurationError:
        return False
    binding = stage.get("attempt")
    return (
        stage.get("mode") == "captured"
        and isinstance(binding, Mapping)
        and binding.get("path") == f"official/attempts/{bound}/attempt.json"
    )


def require_quiescent_campaign_lease(campaign_dir: Path, *, action: str, attempt_id: str | None = None) -> None:
    """seal、前序证据导入、classify 与 VC-3 的静止门禁（调用方须已持 Campaign 锁）：没有仍在执行的租约。拒绝时唯一可能留下的
    写入是过期租约的停线收据（不可覆盖的事实，与 Codex 同语义），租约文件与其余状态都不动。

    * 没有租约或 ``released``：静止；
    * ``active``：先非阻塞取租约锁——取不到即执行者仍在运行，拒绝；取到且按 ``_campaign_lease_staleness`` 已过期（执行者
      被强杀），写不可覆盖的停线收据后按停线租约处理（与 Codex ``CampaignLease`` 发现过期租约时同一语义）；仍在有效期内拒绝；
    * ``stop_the_line``（含上一步固化的过期租约）：执行者已退出。租约未绑定 attempt、绑定的就是本次对象、或绑定的官方 attempt
      已经封存（视为了结），即静止；否则先 ``reconcile-attempt`` 对账那个 attempt。

    B3-06 修的死角：run 写完 attempt.json 之后、释放租约之前被强杀，租约停在 active；原先 seal 一律拒绝，而该 attempt 已完成待
    封存、resume 也没有可续跑的失败作业，于是两头都走不下去。现在 seal 先把过期租约固化为停线收据再继续；封存之后的
    classify、VC-3 与前序导入按「已封存即了结」放行，不会被同一份停线事实永久挡住。
    """

    lease = claude_upgrade._read_campaign_lease(campaign_dir)
    if lease is None or lease.get("state") == "released":
        return
    if lease.get("state") == "active":
        lease = _solidify_expired_lease(campaign_dir, action=action)
        if lease is None or lease.get("state") == "released":
            return
    if lease.get("state") not in {"active", "stop_the_line"}:
        raise ConfigurationError(f"Campaign 租约状态非法：{lease.get('state')}")
    bound = lease.get("attempt_id")
    if bound is None or (attempt_id is not None and str(bound) == str(attempt_id)) or _bound_attempt_sealed(campaign_dir, lease):
        return
    raise ConfigurationError(f"Campaign 停线租约绑定 attempt {bound}，不是本次{action}的对象：先 reconcile-attempt 对账该 attempt。")


# ---------------------------------------------------------------------------
# 明确拒绝的子命令
# ---------------------------------------------------------------------------


def harden_evidence_permissions_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``harden-evidence-permissions``：Claude 不提供（理由见模块说明第五节）。"""

    del arguments
    raise ConfigurationError(
        "Claude 不提供 harden-evidence-permissions：run 从第一个受管版本起就在每个 Job 终态与 run 末各做一次 v2 证据权限收口"
        "（B2-06），收口重放随读取 attempt 默认执行（B2-07），没有缺收口的历史 attempt 需要事后补做；run 末收口失败的 attempt"
        "记为 failed，经 reconcile-attempt → 批准恢复预览 → resume --rerun-failed 由新 attempt 全部复用、零执行地重新收口。"
        "已封存证据的元数据漂移重绑（rebind-boundary）随候选证据封存（B4-02）统一评估。"
    )


def wire_transition_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``wire-transition-intent``／``wire-transition-final``：Claude 不提供（只用工具演进链）。"""

    raise ConfigurationError(
        f"Claude 不提供 {arguments.command}：Claude Campaign 只用工具演进链，wire 层变化一律经 tool-evolution 预览／批准登记"
        "（逐作业作废，resume --rerun-failed 只重跑受影响作业）；wire transition 链与工具演进链在共享合同里互斥。"
    )


# ---------------------------------------------------------------------------
# 子命令参数（与 Codex 同名同参；environment-cleanup 是 Claude 新增）
# ---------------------------------------------------------------------------


def add_reconcile_attempt_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument("--attempt-id", required=True)
    target.add_argument("--recovery-revision", metavar="ar<k>", help="对账该 attempt 的恢复段（候选，B4-05）。")
    target.add_argument("--control-root", type=Path)
    target.add_argument("--approve-recovery-sha256", help="按 recovery-preview 的 review_sha256 批准冻结的恢复闭集。")
    target.add_argument(
        "--authorize-recovery-preview",
        type=Path,
        metavar="PATH",
        help="消费已批准的恢复预览（账本 recovery_required → active），使后继批次可派发。",
    )


def add_reconcile_supervisor_run_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument("--run-dir", type=Path, required=True)
    target.add_argument("--control-root", type=Path, help="部署收据所在控制根；默认 <宿主数据根>/control。")


def add_resume_arguments(target: argparse.ArgumentParser) -> None:
    for name in ("--candidate-id", "--runtime-image", "--candidate-image-id", "--build-id", "--deployed-version", "--profile-id", "--profile-digest"):
        target.add_argument(name)
    target.add_argument("--candidate-source", type=Path)
    target.add_argument("--build-receipt", type=Path)
    target.add_argument("--candidate-purpose", choices=sorted(claude_upgrade.CANDIDATE_PURPOSES))
    target.add_argument("--assertions", type=Path)
    target.add_argument("--external-gate-root", type=Path)
    target.add_argument("--external-gate-receipt", type=Path)
    target.add_argument("--rerun-failed", action="store_true")
    target.add_argument("--preview-recovery", action="store_true", help="只读输出失败补跑闭集（候选，B4-01）。")
    target.add_argument("--recovery-preview", type=Path, help="已批准的 recovery-preview/v1 路径；失败／中断 attempt 的 --rerun-failed 必需。")
    target.add_argument("--candidate-reuse-source-campaign-dir", type=Path)
    target.add_argument("--candidate-reuse-source-candidate-id")
    target.add_argument("--candidate-reuse-source-attempt-id")
    target.add_argument("--approve-candidate-reuse-sha256")
    target.add_argument("--max-wall-seconds", type=int, default=None, help=f"一次受管命令的单调墙钟预算（默认 {claude_upgrade.DEFAULT_ATTEMPT_WALL_SECONDS} 秒）。")
    target.add_argument("--heartbeat-seconds", type=int, default=None, help="watchdog heartbeat 间隔（秒）。")
    target.add_argument("--acknowledge-live-requests", action="store_true")


def add_harden_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument("harden_action", choices=("preview", "apply", "replay", "upgrade-closeout", "rebind-boundary"))
    target.add_argument("--attempt-id", required=True)
    target.add_argument("--approve-sha256")
    target.add_argument("--candidate-id")
    target.add_argument("--operator")


def add_wire_transition_arguments(target: argparse.ArgumentParser, *, final: bool) -> None:
    if final:
        target.add_argument("--attempt-id", required=True)
    else:
        target.add_argument("--approve-sha256")


def add_environment_cleanup_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument("cleanup_action", choices=("preview", "apply"))
    target.add_argument("--recovery-preview", type=Path, help="批准依据：本 Campaign 已批准的恢复预览（来源须是最新 attempt）。")
    target.add_argument("--reason", required=True, help="残留进程从哪次中断留下、为什么要清理。")
    target.add_argument("--approve-sha256", help="apply 必需：preview 输出的 review_sha256。")
    target.add_argument("--approved-by", help="apply 必需：批准人。")
