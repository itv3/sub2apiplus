#!/usr/bin/env python3
"""Claude 恢复：对账、续跑闭集、resume、残留取证进程清理（Claude 升级控制面设计 B2-08；候选续跑 B4-01 c1）。

一、链路
========
* 有未收口预约（attempt 失败、被强杀或被作废）：``reconcile-attempt`` 在 Claude 适配器作用域里调用共享对账器，先入账后
  判定；可恢复时共享对账器写零请求恢复预览，并以 ``recovery_reuse_check``（本模块，R17）按 resume 同一复用判定只读复算，
  不一致即拒绝批准 → 操作员 ``reconcile-attempt --approve-recovery-sha256`` 批准 → ``resume --rerun-failed --recovery-preview``
  在 Campaign 租约下建新 attempt：复用已完成作业（只读承接原证据），只执行预览冻结的执行集合。
* 无预约（父监督器 run 在预约之前失败）：``reconcile-supervisor-run``（共享）入账判定，按提示以 N+1 批次重派。
* 封存批次（首批之后的静态分析与 seal，``vc1-claude.sh`` 派发）零请求失败，分两种收账：
  - 采集路径、失败可升级（动作报错或非零退出）且五条文件事实成立：共享监督器经适配器认 Claude 的 ``vc1-0X-*`` 阶段项
    （静态分析按形态认定为不绑定 attempt 的零请求后处理），升级为 post-run-tooling；``reconcile-supervisor-run`` 入账后
    逐字重派同一批次，或修复改变了 seal 预览时改派同一 attempt 的新预览批次（共享 seal 链续派协议承接）。
  - 其余（只读复用路径没有 attempt、超时或被杀等不可升级的失败）：按 execution-failure 收账、账本进
    ``stage_review_required``；``reconcile-supervisor-run`` 按 ``claude_upgrade_batch`` 的 VC-1 封存批次阶段幂等重派合同写
    证明、重开 VC-1，再重跑 ``vc1-claude.sh`` 以 N+1 逐字重派（共享阶段审核协议承接）。合同不成立时（attempt 被隔离或
    作废、仍有失败 Job、已封存、修复改变了 seal 代号等）留在阶段审核，按对账提示走 ``reconcile-attempt`` 或显式停线。
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
* ``harden-evidence-permissions`` 的 preview／apply／replay／upgrade-closeout：Codex 的 A3a 两步式收口是为改造前没有 run 末
  收口（或只有 v1 收口）的历史 attempt 事后补做的。Claude 的 run 从第一个受管版本起就在每个 Job 终态与 run 末各做一次
  ``evidence-permission-closeout/v2`` 收口（B2-06），收口重放随读取 attempt 默认执行（B2-07）；run 末收口失败的 attempt 记为
  failed，经对账、批准恢复预览与 ``resume --rerun-failed`` 由新 attempt 全部复用、零执行地重新收口。只提供
  ``rebind-boundary``（B4-02 收尾）：已封存的候选 attempt 只有 mtime／ctime／inode 漂移时，转调共享重绑写重绑收据，封存后
  的读侧按封存证据清单加重绑链核对（``claude_upgrade_candidate_seal`` 第六节）；必须带 ``--candidate-id``。
* ``wire-transition-intent``／``wire-transition-final``：Claude Campaign 只用工具演进链，wire 层变化一律经 ``tool-evolution``
  登记（逐作业作废，resume 只重跑受影响作业）；两条链在共享合同里互斥，Claude 不建 wire transition 链。

六、候选续跑（B4-01 c1）
========================
官方阶段的同一套对账、续跑闭集、恢复来源收据与恢复 attempt，按阶段与候选 ID 参数化：

* 命令：``resume --candidate-id <cid> --rerun-failed --recovery-preview <已批准> --acknowledge-live-requests``；候选身份只来自
  VC-4 构建收据（Codex 用来重建候选身份的 ``--runtime-image``／``--build-id`` 等一律拒绝）。``resume --candidate-id`` 在候选
  还没有 attempt 时起首个候选 run；``--preview-recovery`` 只读输出续跑闭集（``claude-upgrade-candidate-recovery-plan/v1``）。
* 来源：本候选最新的 attempt；本候选其余预约都已收口，官方阶段与其它候选没有未封存的预约或 attempt。
* 复用证据多核一项：每步网关恢复报告的摘要（``restoration_checks``）。
* 恢复 attempt 与首个候选 attempt 同一合同：预约前走 ``CandidateRunHooks`` 的入口门、身份、网关只读就绪与 selector 观察
  （都在消费恢复授权之前），before 探针之后采网关 before 快照；有复用时连续性另核网关（来源 attempt 的网关 after 快照与本轮
  before 快照逐字段相等，记在 ``recovery.continuity.gateway``，不等即零请求失败）；attempt 末采网关 after 快照、写候选字段。
* 恢复来源收据沿用 v1 字段闭集：候选 ID 由所在目录与预约收据的 ``phase``／``candidate_id`` 表达。
* 对账：候选 attempt 另采一次网关只读快照留证（``control/reconciliation/attempt-<id>/gateway-probes/<时刻>/``），残留项写进
  ``gateway_residue``。
* 强杀后的网关残留（c2）：宿主脚本在第一处修改之前把运行前原值写进 ``<运行目录>/restore/state.json``；``environment-cleanup
  preview`` 在残留进程之外列出候选网关残留（带原值的运行目录、网关快照里的改动残留；没有原值可依的记 ``unrecoverable``，
  给 ``manual_required``），``apply`` 在同一批准下对每个运行目录串行执行宿主脚本 ``--restore-only``，前后网关快照与每个
  恢复报告（``claude-candidate-gateway-cleanup/v1``）记进清理收据的 ``gateway_residue``。
* 状态：``status --candidate-id``（或只有一个活跃候选时不带）给出候选采集状态，状态名与 Codex 同名（``profile_approved``、
  ``candidate_capture_failed``／``candidate_capture_interrupted``、``candidate_awaiting_client_checkpoint``、
  ``candidate_awaiting_seal_approval``、``candidate_sealed``）；多个活跃候选时是 ``candidate_selection_required``。封存之后
  （B4-07 收尾）接着是 VC-5 的 ``compared``、``ready``（VC-5 已完成），VC-6 的 ``delivery_in_progress``、
  ``delivery_stage_failed``、``delivery_invalidation_pending``、``delivery_awaiting_finalize``（附交付包审核摘要）、
  ``delivery_completion_pending``，终点 ``ready_for_operator_release``（validation_only，与 Codex 同名）或
  ``production_activation_pending``（production_replacement，等 B4-08）；VC-5 封存链与 Store 交接对不上时是
  ``delivery_state_inconsistent``。只读，不反映计时账本的暂停与审核（那些看对账输出）。

七、后继候选复用（B4-01 c3，判据与形状见 ``claude_upgrade_candidate_reuse``）
=========================================================================
``resume --candidate-id <cid> --rerun-failed --candidate-reuse-source-*``：候选还没有 attempt（``profile_approved``）时，
承接冻结前序 Campaign 同一候选构建的已完成 Job。走同一个恢复 attempt 主流程，差别只在：不消费恢复预览，复用闭集由
``CandidateReuse`` 在同一位置现场重算并核对 ``--approve-candidate-reuse-sha256``；预约时要求本候选还没有任何预约，来源收据
是 ``candidate-reuse-source.json``；执行集合非空时才核连续性（来源 attempt 在前序 Campaign）。被复用结果的 ``producer`` 在
别的 Campaign 时带 ``campaign`` 与产出时的 ``execution_sha256``，``_producer`` 沿承接链原样透传，``_verify_reusable_evidence``
按它的目录、候选与产出摘要核对，所以复用 attempt 之后的同 Campaign 续跑照常工作。
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
from tools.official_client_capture import claude_upgrade_vc
from tools.official_client_capture import codex_upgrade_evidence_permissions
from tools.official_client_capture import codex_upgrade_reconciler as reconciler
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_wire_transition
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane

CLIENT = "claude"
PHASE = "official"
CANDIDATE_PHASE = "candidate"
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


def _attempt_root(campaign_dir: Path, attempt_id: str, phase: str = PHASE, candidate_id: str | None = None) -> Path:
    if not capture.SAFE_ID_RE.fullmatch(str(attempt_id)):
        raise ConfigurationError("attempt-id 非法。")
    return campaign_dir / claude_upgrade._capture_attempt_relative(phase, candidate_id) / "attempts" / str(attempt_id)


def _scope(phase: str, candidate_id: str | None) -> tuple[str, str]:
    """(阶段在 attempt 列表里的前缀, 提示里的称呼)：官方是 ``official``／「官方」，候选是候选 ID／「候选 <ID> 的」。"""

    if phase == PHASE:
        if candidate_id is not None:
            raise ConfigurationError("official 阶段不得携带 candidate-id。")
        return "official", "官方"
    if phase != CANDIDATE_PHASE or not isinstance(candidate_id, str) or not capture.SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("候选阶段必须给出合法的 candidate-id。")
    return candidate_id, f"候选 {candidate_id} 的"


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


def require_current_source(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    source_attempt_id: str,
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
) -> None:
    """续跑来源必须是本阶段（官方，或本候选）最新的 attempt，且本阶段其余预约都已收口（已对账的孤儿、被作废或已对账作废的
    待封存 attempt）。候选续跑另要求官方阶段与其它候选没有未封存的预约或 attempt（同一时刻只有一条采集链）。"""

    prefix, label = _scope(phase, candidate_id)
    ordered = claude_upgrade._ordered_capture_attempts(campaign_dir, phase, candidate_id, _manifest=manifest)
    newest = ordered[0][0].name if ordered else None
    if newest != source_attempt_id:
        raise ConfigurationError(
            f"续跑来源必须是最新的{label} attempt：来源 {source_attempt_id}，最新 {newest}（来源已经续跑过，或其后另有 attempt；"
            "对最新 attempt 重新对账）。"
        )
    invalidated = capture._invalidated_attempts(campaign_dir)
    foreign: list[str] = []
    for root, _reservation in ordered[1:]:
        if not (root / "attempt.json").exists():
            if capture._attempt_reconciled_terminal(campaign_dir, root.name) is None:
                foreign.append(f"{prefix}:{root.name}:reserved_or_interrupted")
            continue
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, phase, candidate_id, root.name, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
        )
        if (
            attempt.get("status") == "awaiting_receipts"
            and (phase, candidate_id, root.name) not in invalidated
            and not _attempt_evolution_reconciled(campaign_dir, root.name)
        ):
            foreign.append(f"{prefix}:{root.name}")
    if phase == CANDIDATE_PHASE:
        foreign.extend(claude_upgrade._active_unsealed_attempts(campaign_dir, PHASE, _manifest=manifest))
        foreign.extend(
            item
            for item in claude_upgrade._active_unsealed_attempts(campaign_dir, CANDIDATE_PHASE, _manifest=manifest)
            if not item.startswith(f"{candidate_id}:")
        )
    if foreign:
        raise ConfigurationError(f"Campaign 还有其它未收口的{label}预约或待封存 attempt，禁止续跑：{foreign}")


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
        carried = {"attempt_id": str(producer["attempt_id"]), "job_closure": dict(producer["job_closure"])}
        # 后继候选复用（c3）：在别的 Campaign 产出的结果，承接链上原样带着产出 Campaign 与产出时的执行摘要。
        if "campaign" in producer:
            carried["campaign"] = dict(producer["campaign"])
            carried["execution_sha256"] = str(producer.get("execution_sha256"))
        from tools.official_client_capture import claude_upgrade_predecessor_sealed as predecessor_sealed

        if predecessor_sealed.COVER_FIELD in producer:
            carried[predecessor_sealed.COVER_FIELD] = copy.deepcopy(producer[predecessor_sealed.COVER_FIELD])
        return carried
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
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
    _producer_root: Path | None = None,
) -> None:
    """复用前逐项复核证据：Job 闭环收据摘要、Job 级权限收口重放、relay 清单摘要；候选另核每步恢复报告摘要（任一不成立即
    失败关闭）。"""

    job_id = str(result.get("id"))
    binding = producer["job_closure"]
    producer_campaign_dir, producer_candidate_id = Path(campaign_dir), candidate_id
    produced_execution_sha256 = result.get("execution_sha256")
    origin = producer.get("campaign")
    if origin is not None:
        # 后继候选复用（c3）：产出在别的 Campaign——按产出 Campaign 的目录与候选核对闭环收据，执行摘要比产出时的那一份；
        # 产出 Campaign 的总计划在复用之后被改动即失败关闭。
        if not isinstance(origin, Mapping) or not capture.SAFE_ID_RE.fullmatch(str(origin.get("candidate_id", ""))):
            raise ConfigurationError(f"复用 Job {job_id} 的产出 Campaign 绑定非法。")
        producer_campaign_dir = Path(str(origin.get("campaign_dir", "")))
        manifest_path = producer_campaign_dir / "campaign.json"
        if (
            not producer_campaign_dir.is_absolute()
            or manifest_path.is_symlink()
            or not manifest_path.is_file()
            or file_sha256(manifest_path) != origin.get("campaign_manifest_sha256")
        ):
            raise ConfigurationError(f"复用 Job {job_id} 的产出 Campaign 总计划缺失或摘要漂移。")
        producer_candidate_id = str(origin["candidate_id"])
        produced_execution_sha256 = producer.get("execution_sha256")
    closure_path = claude_upgrade._campaign_file(producer_campaign_dir, str(binding.get("path", "")))
    if closure_path.is_symlink() or not closure_path.is_file() or file_sha256(closure_path) != binding.get("sha256"):
        raise ConfigurationError(f"复用 Job {job_id} 的闭环收据缺失或摘要漂移。")
    closure = claude_upgrade._read_json(closure_path, "Job 闭环收据")
    producer_root = _attempt_root(producer_campaign_dir, str(producer["attempt_id"]), phase, producer_candidate_id) if _producer_root is None else _producer_root
    if _producer_root is not None:
        # B4-05b：恢复段复用由专属读侧传入原产出段，仍强制核对规范坐标，不能借内部参数绕过 Campaign／候选边界。
        from tools.official_client_capture import claude_upgrade_attempt_recovery

        revision = claude_upgrade_attempt_recovery.require_recovery_revision(producer_root.name)
        original_id = producer_root.parent.parent.name
        expected = _attempt_root(producer_campaign_dir, original_id, phase, producer_candidate_id) / claude_upgrade.ATTEMPT_RECOVERY_DIRNAME / revision
        if phase != CANDIDATE_PHASE or origin is not None or producer_root != expected or producer["attempt_id"] != f"{original_id}.{revision}":
            raise ConfigurationError("恢复段复用的 Job 产出坐标与声明身份不一致。")
        claude_upgrade._reject_symlink_components(producer_root, producer_campaign_dir, "恢复段 Job 产出目录")
    job_root = closure_path.parent
    if (
        closure.get("schema_version") != capture.JOB_CLOSURE_SCHEMA
        or closure.get("client") != CLIENT
        or closure.get("job_id") != job_id
        or closure.get("attempt_id") != producer["attempt_id"]
        or closure.get("status") != "complete"
        or closure.get("environment_contaminated")
        or closure.get("execution_sha256") != produced_execution_sha256
        or job_root != producer_root / "evidence" / capture.JOBS_DIRNAME / job_id
    ):
        raise ConfigurationError(f"复用 Job {job_id} 的闭环收据身份、状态或位置不一致。")
    closeout = closure.get("evidence_permission_closeout")
    receipt_name = codex_upgrade_evidence_permissions.RECEIPT_FILENAME
    if not isinstance(closeout, Mapping) or closeout.get("path") != (job_root / receipt_name).relative_to(producer_root).as_posix():
        raise ConfigurationError(f"复用 Job {job_id} 的 Job 级收口绑定非法。")
    roots = [job_root / "evidence", job_root / "logs", *(Path(str(item)) for item in closure.get("host_evidence_roots") or [])]
    try:
        from tools.official_client_capture import claude_upgrade_predecessor_sealed as predecessor_sealed

        if predecessor_sealed.COVER_FIELD in producer:
            if phase != CANDIDATE_PHASE or origin is None:
                raise ConfigurationError("前序封存覆盖证明只能用于带产出 Campaign 的候选结果。")
            predecessor_sealed.verify_cover(
                producer[predecessor_sealed.COVER_FIELD],
                required_roots=[*roots, *(capture._host_path(str(root), layout) for root in result.get("evidence_roots") or [])],
            )
        else:
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
    if phase == CANDIDATE_PHASE:
        rows = result.get("restoration_checks")
        if not isinstance(rows, list) or not rows:
            raise ConfigurationError(f"复用候选 Job {job_id} 缺少每步恢复报告核对。")
        for row in rows:
            report = row.get("report") if isinstance(row, Mapping) and isinstance(row.get("report"), Mapping) else {}
            path = capture._host_path(str(report.get("path", "")), layout)
            if row.get("status") != "passed" or path.is_symlink() or not path.is_file() or file_sha256(path) != report.get("sha256"):
                raise ConfigurationError(f"复用候选 Job {job_id} 的 {row.get('probe_id')} 网关恢复报告缺失、未通过或已漂移。")


def recovery_plan(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    source_attempt_id: str,
    jobs: Sequence[Any] | None = None,
    phase: str = PHASE,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """冻结失败（或被作废、被强杀）官方或候选 attempt 的续跑闭集；只读：不取租约、不写文件、不发请求、不采探针。"""

    campaign_dir = Path(campaign_dir)
    _prefix, label = _scope(phase, candidate_id)
    source_root = _attempt_root(campaign_dir, source_attempt_id, phase, candidate_id)
    if source_root.is_symlink() or not source_root.is_dir():
        raise ConfigurationError(f"Campaign 内没有{label} attempt：{source_attempt_id}")
    reservation = claude_upgrade._load_capture_reservation(
        campaign_dir, source_root, phase=phase, candidate_id=candidate_id, _manifest=manifest
    )
    planned = {str(row["id"]): str(row["execution_sha256"]) for row in reservation["planned_jobs"]}
    jobs = list(jobs) if jobs is not None else claude_upgrade._campaign_jobs(campaign_dir, dict(manifest), phase, candidate_id=candidate_id)
    if {job.job_id: claude_upgrade._job_execution_sha256(job) for job in jobs} != planned:
        raise ConfigurationError(f"Campaign 冻结的 {phase} Job 与来源 attempt 的预约计划不一致（ID 或执行摘要漂移），禁止续跑。")
    attempt: dict[str, Any] | None = None
    if (source_root / "attempt.json").exists() or (source_root / "attempt.json").is_symlink():
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, phase, candidate_id, source_attempt_id, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
        )
    status = str(attempt.get("status")) if attempt is not None else "reserved_or_interrupted"
    key = (phase, candidate_id, source_attempt_id)
    rerun_all = key in capture._invalidated_attempts(campaign_dir)
    impact = claude_upgrade._attempt_evolution_impact(campaign_dir, manifest, source_root, phase=phase, candidate_id=candidate_id)
    if attempt is None:
        if capture._attempt_reconciled_terminal(campaign_dir, source_attempt_id) is None:
            raise ConfigurationError(f"被中断的 attempt {source_attempt_id} 尚未对账：先 reconcile-attempt 入账并批准恢复预览。")
    elif status == "environment_contaminated":
        if key not in claude_upgrade._isolation_invalidated_attempts(campaign_dir):
            raise ConfigurationError("环境污染 attempt 尚未隔离：先修复环境并 environment-isolate，再对账续跑。")
    elif status == "awaiting_receipts":
        evolved = claude_upgrade._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, source_root, attempt, phase=phase, candidate_id=candidate_id
        )
        if not rerun_all and not evolved:
            raise ConfigurationError(
                f"attempt {source_attempt_id} 正等待 seal、没有被作废的作业，不是续跑来源；按正常流程 capture-{phase} seal。"
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
            _verify_reusable_evidence(campaign_dir, result, producer, layout, phase=phase, candidate_id=candidate_id)
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
    """R17：按 resume 同一复用判定只读复算恢复预览（官方与候选）；resume 会拒绝的情形原样抛 ``ConfigurationError``。

    候选阶段（c1）：生效清单经运行坐标覆盖（Claude 恒为原样或失败关闭）；候选必须未封存；不重走入口门（那要采 docker
    与 Store 事实，属于 resume 的预约前门禁），复用判定与 resume 是同一个 ``recovery_plan``。
    """

    _scope(phase, candidate_id)
    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    claude_upgrade._reject_contaminated_campaign(campaign_dir)
    if phase == PHASE:
        if capture._official_sealed(campaign_dir):
            raise ConfigurationError("官方证据已经封存，禁止续跑。")
    else:
        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

        candidate_capture.require_unsealed(campaign_dir, str(candidate_id), revision=None)
        manifest = candidate_capture.apply_runtime_override(campaign_dir, manifest, candidate_id)
    require_current_source(campaign_dir, manifest, str(source_attempt_id), phase=phase, candidate_id=candidate_id)
    plan = recovery_plan(campaign_dir, manifest, source_attempt_id=str(source_attempt_id), phase=phase, candidate_id=candidate_id)
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
    """只读核对已批准的恢复预览（不消费授权）：位置、自摘要、零请求边界、批准收据；来源必须仍是本阶段（官方或该候选）最新的
    attempt（候选续跑同样可以凭它在恢复批准下清理残留进程）。"""

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
        or preview.get("phase") not in {PHASE, CANDIDATE_PHASE}
        or (preview.get("phase") == PHASE) != (preview.get("candidate_id") is None)
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
    phase = str(preview["phase"])
    candidate_id = preview.get("candidate_id") if phase == CANDIDATE_PHASE else None
    require_current_source(campaign_dir, manifest, str(preview["source_attempt_id"]), phase=phase, candidate_id=candidate_id)
    return {
        "kind": "recovery_approval",
        # 候选续跑的恢复批准（B4-01 c2）另记候选 ID（官方的批准依据逐字不变）。
        **({"candidate_id": str(candidate_id)} if phase == CANDIDATE_PHASE else {}),
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


def _cleanup_preview(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    basis: Mapping[str, Any],
    processes: Sequence[Mapping[str, Any]],
    reason: str,
    gateway: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    preview = {
        "schema_version": CLEANUP_PREVIEW_SCHEMA,
        "client": CLIENT,
        "campaign_id": str(manifest["campaign_id"]),
        "basis": dict(basis),
        "processes": [dict(item) for item in processes],
        "reason": reason,
        "live_request_count": 0,
    }
    if gateway is not None:
        # 候选网关残留（B4-01 c2）：只在有残留时进预览（没有残留的预览与 c2 之前逐字相同）。
        preview["gateway_residue"] = dict(gateway)
    preview["review_sha256"] = claude_upgrade._fingerprint(preview)
    return preview


def _gateway_residue(manifest: Mapping[str, Any], campaign_dir: Path) -> dict[str, Any] | None:
    """候选网关残留（B4-01 c2；只读）：总计划没有候选变量时不适用（None）。"""

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    if not candidate_capture.gateway_cleanup_enabled(manifest):
        return None
    return candidate_capture.list_gateway_residue(manifest, capture._host_data_root(campaign_dir))


def environment_cleanup_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``environment-cleanup preview|apply``：在明确批准下清理两个采集容器里的残留取证进程与候选网关残留（B4-01 c2），
    前后探针、网关快照与清理收据留证。

    候选网关残留：宿主候选运行目录里每个带运行前原值（``restore/state.json``）的条目在 apply 时串行执行宿主脚本的
    ``--restore-only``（脚本按现场判定要恢复的项、恢复并逐项核验，报告只打到 stdout）；残留却没有原值可依、或运行目录没有
    原值时记「无法自动恢复」，预览给出 ``manual_required``、apply 拒绝，须人工处置。
    """

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

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
        residue = _gateway_residue(manifest, campaign_dir)
        gateway = residue["summary"] if residue is not None and candidate_capture.residue_present(residue["summary"]) else None
        if not processes and gateway is None:
            return {"status": "clean", "campaign_id": manifest["campaign_id"], "basis": basis, "processes": [], "live_request_count": 0}, 0
        preview = _cleanup_preview(campaign_dir, manifest, basis, processes, reason, gateway)
        if gateway is not None and gateway["unrecoverable"]:
            return {
                "status": "manual_required",
                **preview,
                "next_command": "候选网关有无法自动恢复的残留（见 gateway_residue.unrecoverable）：人工处置后重新预览",
            }, 3
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
            residue = _gateway_residue(manifest, campaign_dir)
            gateway = residue["summary"] if residue is not None and candidate_capture.residue_present(residue["summary"]) else None
            preview = _cleanup_preview(campaign_dir, manifest, basis, processes, reason, gateway)
            if not processes and gateway is None:
                raise ConfigurationError("当前两个采集容器里没有残留取证进程、候选网关也没有残留，无需清理（批准作废）。")
            if gateway is not None and gateway["unrecoverable"]:
                raise ConfigurationError("候选网关有无法自动恢复的残留（见预览 gateway_residue.unrecoverable）：人工处置后重新预览。")
            if preview["review_sha256"] != approve:
                raise ConfigurationError("残留进程清单、网关残留或批准依据在预览之后已变化，批准作废；重新预览后再批准。")
            chain = _cleanup_chain(campaign_dir)
            index = len(chain) + 1
            directory = capture._private_directory(campaign_dir / CLEANUP_DIR, campaign_dir)
            evidence = capture._private_directory(directory / f"cleanup-{index:02d}", campaign_dir)
            before = capture._probe_capture_environment(dict(manifest), evidence / "before", "before")
            results = terminate_residual_processes(manifest, processes) if processes else []
            gateway_record: dict[str, Any] | None = None
            gateway_cleaned = True
            if gateway is not None and residue is not None:
                gateway_record, gateway_cleaned = _restore_gateway_residue(campaign_dir, manifest, evidence, gateway, residue["snapshot"])
            after = capture._probe_capture_environment(dict(manifest), evidence / "after", "after")
            remaining = list(after.get("capture_processes") or [])
            cleaned = (
                not remaining
                and all(item.get("outcome") in {"terminated", "already_gone"} for item in results)
                and gateway_cleaned
            )
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
                # 候选网关残留的恢复（B4-01 c2）；没有网关部分时为 null（c2 之前的收据没有这个键，链校验不受影响）。
                "gateway_residue": gateway_record,
                "observed_at_utc": after.get("observed_at_utc"),
                "recorded_at_utc": claude_upgrade._utc_now(),
                "live_request_count": 0,
            }
            receipt["cleanup_sha256"] = claude_upgrade._fingerprint(receipt)
            receipt_path = directory / f"cleanup-{index:02d}.json"
            claude_upgrade._secure_write_json_once(receipt_path, receipt)
    if basis["kind"] == "recovery_approval":
        candidate_flag = f"--candidate-id {basis['candidate_id']} " if basis.get("candidate_id") else ""
        next_command = (
            f"resume {candidate_flag}--rerun-failed --recovery-preview {campaign_dir / basis['recovery_preview']['path']} "
            "--acknowledge-live-requests"
        )
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
        "gateway_residue": gateway_record,
        "next_command": (
            next_command
            if cleaned
            else "仍有残留未清理（见 results／after_capture_processes／gateway_residue）：人工处置后重新预览"
        ),
        "live_request_count": 0,
    }
    return result, 0 if cleaned else 3


def _restore_gateway_residue(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    evidence: Path,
    summary: Mapping[str, Any],
    before_snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """按批准对每个带运行前原值的运行目录串行执行 ``--restore-only``，前后网关快照留证；返回 ``(收据记录, 是否清理干净)``。"""

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    layout = capture._capture_layout(campaign_dir, manifest)
    host_data_root = capture._host_data_root(campaign_dir)
    before_path = evidence / "gateway-before.json"
    claude_upgrade._secure_write_json_once(before_path, dict(before_snapshot))
    restores = [candidate_capture.restore_runtime_entry(layout, host_data_root, entry) for entry in summary["entries"]]
    after_snapshot = candidate_capture.probe_gateway_environment(manifest, host_data_root, "readiness")
    after_path = evidence / "gateway-after.json"
    claude_upgrade._secure_write_json_once(after_path, after_snapshot)
    after_residue = sorted(set(candidate_capture.readiness_failures(after_snapshot)) & candidate_capture.RESIDUE_FAILURES)
    cleaned = all(item["status"] == "restored" for item in restores) and not after_residue
    record = {
        "entries": list(summary["entries"]),
        "residue_failures_before": list(summary["residue_failures"]),
        "restores": restores,
        "residue_failures_after": after_residue,
        "before_snapshot": _binding(campaign_dir, before_path),
        "after_snapshot": _binding(campaign_dir, after_path),
        "status": "restored" if cleaned else "incomplete",
    }
    return record, cleaned


# ---------------------------------------------------------------------------
# 对账与入账（共享流程在 Claude 适配器作用域里转调）
# ---------------------------------------------------------------------------


def reconcile_attempt_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``reconcile-attempt``：共享对账之后识别两个采集容器里的残留取证进程（只读探针留证）。"""

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
        phase, candidate_id = _attempt_scope(campaign_dir, str(arguments.attempt_id))
        if phase == CANDIDATE_PHASE:
            gateway = identify_gateway_residue(campaign_dir, manifest, str(arguments.attempt_id), str(candidate_id))
            result["gateway_residue"] = gateway
            if gateway["status"] == "residual":
                result["next_command"] = (
                    f"候选网关残留 {gateway['failures']}（只读快照已留证）：resume 之前先 environment-cleanup preview/apply"
                    "（--recovery-preview <已批准的恢复预览>）在恢复批准下按运行前原值恢复网关并留证，否则续跑在预约前拒绝；"
                    + str(result.get("next_command", ""))
                )
    return result, 0 if result.get("status") in {"recoverable", "authorized"} else 3


def _attempt_scope(campaign_dir: Path, attempt_id: str) -> tuple[str, str | None]:
    """attempt 所属阶段与候选（按 Campaign 内的 attempt 目录定位，与共享对账器同一取法）。"""

    for phase, candidate_id, attempt_root in claude_upgrade._campaign_attempt_roots(campaign_dir):
        if attempt_root.name == attempt_id:
            return phase, candidate_id
    raise ConfigurationError(f"Campaign 内没有 attempt：{attempt_id}")


GATEWAY_PROBE_DIRNAME = "gateway-probes"


def identify_gateway_residue(campaign_dir: Path, manifest: Mapping[str, Any], attempt_id: str, candidate_id: str) -> dict[str, Any]:
    """候选 attempt 对账时识别网关残留（B4-01 c1）：采一次网关只读快照留证在对账目录下，按候选 run 的就绪判据给出残留项；
    快照采不到时记 probe_failed，不改变对账结论。"""

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    stamp = claude_upgrade._utc_now().replace(":", "").replace("-", "").replace(".", "")
    output_dir = campaign_dir / "control" / reconciler.RECONCILIATION_DIR / f"attempt-{attempt_id}" / GATEWAY_PROBE_DIRNAME / stamp
    try:
        effective = candidate_capture.apply_runtime_override(campaign_dir, dict(manifest), candidate_id)
        snapshot = candidate_capture.probe_gateway_environment(effective, capture._host_data_root(campaign_dir), "readiness")
        capture._private_directory(output_dir, campaign_dir)
        path = output_dir / "gateway-readiness.json"
        claude_upgrade._secure_write_json_once(path, snapshot)
    except (ConfigurationError, OSError, incremental_recovery.IncrementalRecoveryError) as error:
        return {"status": "probe_failed", "reason": f"{type(error).__name__}: {str(error)[:300]}", "failures": None}
    failures = candidate_capture.readiness_failures(snapshot)
    return {
        "status": "residual" if failures else "clean",
        "failures": failures,
        "snapshot": _binding(campaign_dir, path),
        "observed_at_utc": snapshot.get("observed_at_utc"),
    }


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
# campaign_status（官方阶段 B2-08；候选阶段 B4-01 c1）
# ---------------------------------------------------------------------------

STATUS_SCHEMA = "claude-upgrade-status/v1"


def _attempt_summary(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    attempt_root: Path,
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """一个官方或候选 attempt 的只读摘要（不重放证据边界）：状态与作废、隔离、连续性漂移等续跑判定事实。"""

    attempt_id = attempt_root.name
    if not (attempt_root / "attempt.json").exists():
        reconciled = capture._attempt_reconciled_terminal(campaign_dir, attempt_id) is not None
        return {"attempt_id": attempt_id, "status": "reconciled_interrupted" if reconciled else "reserved_or_interrupted"}
    _root, attempt = claude_upgrade._load_capture_attempt(
        campaign_dir, phase, candidate_id, attempt_id, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
    )
    summary: dict[str, Any] = {
        "attempt_id": attempt_id,
        "status": attempt.get("status"),
        "failed_job_ids": claude_upgrade._failed_job_ids(attempt.get("results")),
        "recovery_source_attempt_id": (attempt.get("recovery") or {}).get("source_attempt_id") if isinstance(attempt.get("recovery"), Mapping) else None,
    }
    key = (phase, candidate_id, attempt_id)
    if attempt.get("status") == "awaiting_receipts" and not summary["failed_job_ids"]:
        invalidated = claude_upgrade._attempt_evolution_invalidated_job_ids(
            campaign_dir, manifest, attempt_root, attempt, phase=phase, candidate_id=candidate_id
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
    decision = claude_upgrade._campaign_environment_decision(campaign_dir, _manifest=manifest)
    sealed = capture._official_sealed(campaign_dir)
    if candidate_id is not None:
        return _candidate_status(campaign_dir, manifest, base, str(candidate_id), decision=decision, official_sealed=sealed)
    from tools.official_client_capture import claude_upgrade_baseline

    active_candidates, _inactive = claude_upgrade_baseline.evolution_candidates(campaign_dir)
    if sealed and decision["status"] == "clear" and active_candidates:
        # 官方已封存、有活跃候选：只有一个时就是它的状态（带 candidate_id）；多个时要求指定 --candidate-id（与 Codex 同名状态）。
        if len(active_candidates) == 1:
            return _candidate_status(campaign_dir, manifest, base, active_candidates[0], decision=decision, official_sealed=sealed)
        return {
            **base,
            "status": "candidate_selection_required",
            "candidates": active_candidates,
            "next_command": "从候选列表选择 candidate-id 执行 status／capture-candidate／resume",
        }
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
    candidate_attempts = [
        f"{current_candidate}:{root.name}"
        for current_phase, current_candidate, root in claude_upgrade._campaign_attempt_roots(campaign_dir)
        if current_phase == CANDIDATE_PHASE
    ]
    if decision["status"] != "clear":
        status = "environment_contaminated"
        next_command = (
            "官方已封存且官方侧存在未隔离污染：Campaign 内无法重采官方证据，只读保留"
            if decision["status"] == "official_sealed_contaminated"
            else "修复环境（残留取证进程先 environment-cleanup 在明确批准下清理并留证）并取得晚于污染的干净环境复核后，"
            "environment-isolate preview/apply 隔离污染 attempt，再对账续跑"
        )
    elif (sealed and unsealed) or (not sealed and candidate_attempts):
        status = "capture_state_inconsistent"
        next_command = (
            "官方已封存却还有未封存的预约或 attempt：人工审计"
            if sealed
            else f"官方证据尚未封存却已有候选 attempt {candidate_attempts}：人工审计"
        )
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


def _candidate_seal_preview_attempts(campaign_dir: Path, candidate_id: str) -> set[str]:
    """已经生成过候选 seal 预览的 attempt（``candidates/<cid>/seal/<代号>/seal-preview.json`` 的 ``attempt_id``，B4-02）。"""

    from tools.official_client_capture import claude_upgrade_candidate_seal as candidate_seal

    root = campaign_dir / "candidates" / candidate_id / candidate_seal.SEAL_DIRNAME
    attempts: set[str] = set()
    if not root.is_dir() or root.is_symlink():
        return attempts
    for generation in sorted(root.iterdir()):
        preview = generation / candidate_seal.SEAL_PREVIEW_FILENAME
        if generation.is_symlink() or not generation.is_dir() or preview.is_symlink() or not preview.is_file():
            continue
        attempt_id = claude_upgrade._read_json(preview, "候选 seal 预览").get("attempt_id")
        if isinstance(attempt_id, str):
            attempts.add(attempt_id)
    return attempts


def _candidate_status(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    base: Mapping[str, Any],
    candidate_id: str,
    *,
    decision: Mapping[str, Any],
    official_sealed: bool,
) -> dict[str, Any]:
    """一个候选的采集状态与下一命令（只读浅检查；状态名与 Codex ``campaign_status`` 的候选部分同名）。

    封存之后（B4-07 收尾）接着给出 VC-5 与 VC-6 的状态（``_evaluation_and_delivery_status``）：``candidate_sealed``（下一步 compare
    批次）、``compared``（assert-rules 与 accept 批次）、``ready``（VC-5 已完成，下一步 VC-6 交付 run 批次），以及交付各停点与
    VC-6 终点（``claude_upgrade_delivery.delivery_status``）。
    """

    from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

    if not capture.SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("status --candidate-id 格式非法。")
    candidate_root = campaign_dir / "candidates" / candidate_id
    if candidate_root.is_symlink() or not candidate_root.is_dir():
        raise ConfigurationError(f"Campaign 内没有候选 {candidate_id}（先完成 VC-4 revision-open 与 record-candidate-build）。")
    ordered = claude_upgrade._ordered_capture_attempts(campaign_dir, CANDIDATE_PHASE, candidate_id, _manifest=manifest)
    summaries = [
        _attempt_summary(campaign_dir, manifest, root, phase=CANDIDATE_PHASE, candidate_id=candidate_id) for root, _reservation in ordered
    ]
    stage_path = candidate_capture.legacy_stage_result_path(campaign_dir, candidate_id)
    sealed = stage_path.exists() or stage_path.is_symlink()
    sealed_attempt_id: str | None = None
    if sealed:
        stage = claude_upgrade._load_stage_result(campaign_dir, "capture-candidate", candidate_id, _verified_campaign_manifest=manifest, _baseline=0)
        reference = stage.get("attempt")
        sealed_attempt_id = Path(str(reference.get("path", ""))).parent.name if isinstance(reference, Mapping) else None
    invalidated = capture._invalidated_attempts(campaign_dir)
    unsealed = [
        f"{candidate_id}:{item['attempt_id']}" + (":reserved_or_interrupted" if item["status"] == "reserved_or_interrupted" else "")
        for item in summaries
        if item["status"] == "reserved_or_interrupted"
        or (
            item["status"] == "awaiting_receipts"
            and item["attempt_id"] != sealed_attempt_id
            and (CANDIDATE_PHASE, candidate_id, item["attempt_id"]) not in invalidated
            and not item.get("evolution_invalidated_job_ids")
            and not _attempt_evolution_reconciled(campaign_dir, item["attempt_id"])
        )
    ]
    failed = [f"{candidate_id}:{item['attempt_id']}" for item in summaries if item["status"] in {"failed", "environment_contaminated"}]
    latest = summaries[0] if summaries else None
    build_receipt = claude_upgrade._candidate_build_receipt_path(campaign_dir, candidate_id)
    delivery: dict[str, Any] | None = None
    run_command = f"capture-candidate run --campaign-dir <Campaign> --candidate-id {candidate_id} --acknowledge-live-requests"
    resume_command = f"resume --candidate-id {candidate_id} --rerun-failed --recovery-preview <已批准的 recovery-preview> --acknowledge-live-requests"
    if decision["status"] != "clear":
        status = "environment_contaminated"
        next_command = (
            "修复环境（残留取证进程先 environment-cleanup 在明确批准下清理并留证）并取得晚于污染的干净环境复核后，"
            "environment-isolate preview/apply 隔离污染 attempt，再对账续跑"
        )
    elif not official_sealed or (sealed and unsealed):
        status = "capture_state_inconsistent"
        next_command = (
            "官方证据尚未封存却已有候选：人工审计" if not official_sealed else "候选已封存却还有未封存的预约或 attempt：人工审计"
        )
    elif sealed:
        status, next_command, delivery = _evaluation_and_delivery_status(campaign_dir, manifest, candidate_id)
    elif latest is None:
        status = "profile_approved"
        next_command = (
            run_command
            if build_receipt.is_file() and not build_receipt.is_symlink()
            else "依次执行 plan-candidate-gates、record-candidate-build（VC-4），再执行 " + run_command
        )
    elif latest.get("isolation_invalidated") or latest.get("evidence_conflict_quarantine"):
        status = "candidate_capture_failed"
        next_command = "该 attempt 已被隔离作废（结果永不复用、永不 seal）：reconcile-attempt 入账并批准恢复预览后，" + resume_command + " 全部重跑"
    elif latest["status"] == "reconciled_interrupted":
        status = "candidate_capture_failed"
        next_command = resume_command
    elif latest["status"] == "reserved_or_interrupted":
        status = "candidate_capture_interrupted"
        next_command = "先执行 reconcile-attempt；按判定与已批准预览决定 resume 或永久停线"
    elif latest["status"] == "awaiting_receipts" and latest.get("failed_job_ids"):
        status = "candidate_capture_failed"
        next_command = "存在失败 Job，禁止 seal：reconcile-attempt 入账并批准恢复预览后，" + resume_command
    elif latest["status"] == "awaiting_receipts" and latest.get("evolution_invalidated_job_ids"):
        status = "candidate_capture_failed"
        next_command = (
            "工具演进使已完成作业失效（" + "、".join(latest["evolution_invalidated_job_ids"]) + "），禁止 seal：reconcile-attempt"
            " 入账并批准恢复预览后，" + resume_command + " 只重跑失效作业，再 seal"
        )
    elif latest["status"] == "awaiting_receipts" and latest["attempt_id"] in _candidate_seal_preview_attempts(campaign_dir, candidate_id):
        status = "candidate_awaiting_seal_approval"
        next_command = f"capture-candidate seal --candidate-id {candidate_id} --attempt-id {latest['attempt_id']} --approve-seal-sha256 <review_sha256>"
    elif latest["status"] == "awaiting_receipts":
        status = "candidate_awaiting_client_checkpoint"
        next_command = (
            "Mac 侧 Desktop／VS Code 客户端收据与 attempt 级边界核对收据放进 <attempt>/client-evidence/ 之后，"
            f"capture-candidate seal --candidate-id {candidate_id} --attempt-id {latest['attempt_id']}（先预览，再按 review_sha256 批准）"
        )
    elif latest.get("continuity_drift"):
        status = "candidate_capture_failed"
        next_command = "上一轮结束后环境已漂移（承接前失败、零请求）：reconcile-attempt 对账后恢复预览全部重跑，批准后 " + resume_command
    else:
        status = "candidate_capture_failed"
        next_command = "reconcile-attempt 入账并批准恢复预览后，" + resume_command + " 只重跑失败作业"
    return {
        **base,
        "status": status,
        "candidate_id": candidate_id,
        "candidate_attempt": latest,
        "candidate_attempts": summaries,
        "candidate_sealed_attempt_id": sealed_attempt_id,
        "active_unsealed_attempts": {
            "official": claude_upgrade._active_unsealed_attempts(campaign_dir, PHASE, _manifest=manifest),
            "candidate": unsealed,
        },
        "failed_attempts": {"candidate": failed},
        "contamination_records": list(decision["records"]),
        "next_command": next_command,
        **({"delivery": delivery} if delivery is not None else {}),
    }


def _evaluation_and_delivery_status(
    campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str
) -> tuple[str, str, dict[str, Any] | None]:
    """候选证据封存之后的状态（B4-07 收尾）：``(状态, 下一命令, 交付明细或 None)``。

    比较收据与 accept 阶段结果按当前评估基线的读来源判断是否已写（与 ``_load_stage_result`` 同一坐标）；accept 结果与
    VC-5 checkpoint 都在才算 VC-5 完成（accept 先写结果再写 checkpoint，只有结果是被打断的半成品，重派 accept 批次收敛）。
    VC-5 完成之后交给 ``claude_upgrade_delivery.delivery_status``（入口重放、Store 交付链与交付阶段结果）；它失败关闭时报
    ``delivery_state_inconsistent`` 并原样给出原因，交人工审计。
    """

    from tools.official_client_capture import claude_upgrade_attempt_segment

    segment_status = claude_upgrade_attempt_segment.segment_status(campaign_dir, candidate_id)
    if segment_status is not None:
        return segment_status["status"], segment_status["next_command"], None
    vc5_batch = "compile-and-run-vc-batch --phase VC-5"
    compared = claude_upgrade._stage_path(campaign_dir, "compare", candidate_id)[1]
    if not (compared.exists() or compared.is_symlink()):
        return "candidate_sealed", f"VC-5：{vc5_batch} 派 compare 批次（compare --candidate-id {candidate_id}）", None
    accepted = claude_upgrade._stage_path(campaign_dir, "accept", candidate_id)[1]
    revision, _record = claude_upgrade._require_candidate_revision(campaign_dir, manifest, action="status")
    checkpoint = claude_upgrade_vc.vc_checkpoint_path(campaign_dir, "VC-5", revision=revision)
    if not (accepted.is_file() and checkpoint.is_file()):
        return (
            "compared",
            f"VC-5：{vc5_batch} 依次派 assert-rules 批次（builder 按比较收据的配置）与 accept 批次（--external-gate-root、"
            "--external-gate-receipt）；accept 结果已写而 VC-5 checkpoint 未写时逐字重派 accept 批次补写",
            None,
        )
    from tools.official_client_capture import claude_upgrade_delivery

    try:
        delivery = claude_upgrade_delivery.delivery_status(campaign_dir, candidate_id)
    except ConfigurationError as error:
        return "delivery_state_inconsistent", f"人工审计 VC-5 封存链与交付状态：{error}", None
    return str(delivery["status"]), str(delivery["next_command"]), delivery


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

# Codex resume 用来重建候选身份的参数：Claude 的候选身份只来自 VC-4 构建收据（入口门重放），一律拒绝。
_CANDIDATE_IDENTITY_FIELDS = (
    "runtime_image", "candidate_image_id", "candidate_source", "build_id", "deployed_version", "profile_id",
    "profile_digest", "build_receipt", "candidate_purpose",
)
_REUSE_SOURCE_FIELDS = (
    "candidate_reuse_source_campaign_dir", "candidate_reuse_source_candidate_id", "candidate_reuse_source_attempt_id",
    "approve_candidate_reuse_sha256",
)


def _reject_unsupported_resume(arguments: argparse.Namespace) -> None:
    given = [field for field in _CANDIDATE_IDENTITY_FIELDS if getattr(arguments, field, None)]
    if given:
        raise ConfigurationError(
            f"Claude 候选身份只来自 VC-4 构建收据（resume 重走候选入口门），不接受 {['--' + item.replace('_', '-') for item in given]}；"
            "只给 --candidate-id。"
        )
    if any(getattr(arguments, field, None) for field in _REUSE_SOURCE_FIELDS) and not getattr(arguments, "candidate_id", None):
        raise ConfigurationError("--candidate-reuse-source-* 是后继候选复用（B4-01 c3），必须同时给出 --candidate-id。")
    if getattr(arguments, "assertions", None) or getattr(arguments, "external_gate_root", None) or getattr(arguments, "external_gate_receipt", None):
        raise claude_upgrade._pending("resume compare／accept", "B4-03／B4-06（Claude 评估器与 accept 门禁）")
    if getattr(arguments, "preview_recovery", False) and not getattr(arguments, "candidate_id", None):
        raise ConfigurationError("--preview-recovery 只用于候选续跑（给出 --candidate-id）；官方续跑的闭集由 reconcile-attempt 的恢复预览给出。")


def _resume_exit_code(result: Mapping[str, Any]) -> int:
    in_campaign_run = os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1"
    return 0 if result.get("status") == "awaiting_receipts" and in_campaign_run else 2


def resume_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``resume``：按 Campaign 状态续跑。失败或已对账中断的 attempt 只经已批准的恢复预览 ``--rerun-failed``。

    给出 ``--candidate-id`` 时是候选续跑（B4-01 c1，见 ``_resume_candidate``）；否则是官方阶段（B2-08）。
    """

    _reject_unsupported_resume(arguments)
    campaign_dir = _resolved_campaign(arguments.campaign_dir)
    arguments.campaign_dir = campaign_dir
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    candidate_id = getattr(arguments, "candidate_id", None)
    if getattr(arguments, "attempt_recovery", None) and not candidate_id:
        raise ConfigurationError("恢复段续跑必须提供 --candidate-id。")
    if candidate_id:
        return _resume_candidate(campaign_dir, manifest, arguments, str(candidate_id))
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
    if current.startswith("candidate_") or current == "profile_approved":
        raise ConfigurationError(f"Campaign 已进入候选阶段（{current}）：候选续跑必须给出 --candidate-id。{status.get('next_command', '')}")
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
        raise ConfigurationError(f"Campaign 状态 {current} 没有可续跑的官方动作：{status.get('next_command', '')}")
    return result, _resume_exit_code(result)


RECOVERY_PLAN_PREVIEW_SCHEMA = "claude-upgrade-candidate-recovery-plan/v1"


def _resume_candidate(
    campaign_dir: Path, manifest: dict[str, Any], arguments: argparse.Namespace, candidate_id: str
) -> tuple[dict[str, Any], int]:
    """候选续跑（B4-01 c1）：按候选状态续跑。

    * ``profile_approved``（VC-4 已构建、还没有 attempt）：起首个候选 run（与 ``capture-candidate run`` 同一入口）；
    * ``candidate_capture_failed``：``--preview-recovery`` 只读输出续跑闭集（零写入，``claude-upgrade-candidate-recovery-plan/v1``）；
      ``--rerun-failed --recovery-preview <已批准>`` 在 Campaign 租约下建候选恢复 attempt（复用已完成作业、只执行冻结的执行集合）；
    * 给出 ``--candidate-reuse-source-*``（B4-01 c3）：只在 ``profile_approved`` 时，``--preview-recovery`` 只读输出复用预览，
      ``--approve-candidate-reuse-sha256`` 在 Campaign 租约下建复用 attempt（``claude_upgrade_candidate_reuse``）；
    * 其余状态（中断未对账、等待 seal、已封存、污染、不一致）拒绝并给出下一命令。
    """

    if getattr(arguments, "attempt_recovery", None):
        from tools.official_client_capture import claude_upgrade_attempt_segment

        result = claude_upgrade_attempt_segment.run_segment(arguments)
        return result, _resume_exit_code(result)
    status = claude_upgrade.campaign_status(campaign_dir, candidate_id)
    current = str(status["status"])
    if current == "environment_contaminated":
        raise ConfigurationError("存在未隔离的环境污染，Campaign 已封锁，不能自动 resume：" + str(status.get("next_command")))
    from tools.official_client_capture import claude_upgrade_candidate_reuse as candidate_reuse

    if candidate_reuse.requested(arguments):
        # 后继候选复用（B4-01 c3）：只在候选的首个 attempt 之前，承接冻结前序 Campaign 同一候选构建的已完成 Job。
        candidate_reuse.validate_arguments(arguments)
        candidate_reuse.reject_same_campaign(campaign_dir, arguments, candidate_id)
        if current != "profile_approved":
            raise ConfigurationError(
                f"后继候选复用只用于还没有任何 attempt 的候选（profile_approved），候选 {candidate_id} 当前是 {current}；"
                "已有 attempt 的续跑走 reconcile-attempt 与 --recovery-preview。"
            )
        if getattr(arguments, "preview_recovery", False):
            return candidate_reuse.preview_command(campaign_dir, manifest, arguments, candidate_id), 0
        reuse = candidate_reuse.CandidateReuse(campaign_dir, manifest, arguments, candidate_id)
        result = _run_with_lease(campaign_dir, manifest, arguments, phase=CANDIDATE_PHASE, candidate_id=candidate_id, reuse=reuse)
        return result, _resume_exit_code(result)
    if current == "candidate_capture_interrupted":
        raise ConfigurationError("候选 attempt 已中断且尚未对账；先执行 reconcile-attempt，按判定与已批准的恢复预览决定是否 resume --rerun-failed。")
    if current == "capture_state_inconsistent":
        raise ConfigurationError("存在额外的未封存预约：只能人工审计。" + str(status.get("next_command", "")))
    if current in {"candidate_awaiting_client_checkpoint", "candidate_awaiting_seal_approval"}:
        raise ConfigurationError("候选 attempt 正等待 seal；resume 不会代替 capture-candidate seal 的人工审核。" + str(status.get("next_command", "")))
    if current == "candidate_sealed":
        raise ConfigurationError("候选证据已封存，下一步是 compare（B4-03）；resume 不会自动比较与验收。")
    if getattr(arguments, "preview_recovery", False):
        if current != "candidate_capture_failed":
            raise ConfigurationError(f"候选状态 {current} 没有可预览的续跑闭集。")
        source_id = str((status.get("candidate_attempt") or {}).get("attempt_id"))
        effective = claude_upgrade._apply_candidate_runtime_override(campaign_dir, manifest, candidate_id)
        require_current_source(campaign_dir, effective, source_id, phase=CANDIDATE_PHASE, candidate_id=candidate_id)
        plan = recovery_plan(campaign_dir, effective, source_attempt_id=source_id, phase=CANDIDATE_PHASE, candidate_id=candidate_id)
        return {
            "schema_version": RECOVERY_PLAN_PREVIEW_SCHEMA,
            "status": "recovery_plan",
            "campaign_id": manifest["campaign_id"],
            "candidate_id": candidate_id,
            **{key: plan[key] for key in (
                "source_attempt_id", "source_status", "source_reservation", "source_attempt", "planned_job_ids",
                "complete_job_ids", "reuse_job_ids", "execute_job_ids", "invalidated",
            )},
            "next_command": "reconcile-attempt 入账写恢复预览并批准后，resume --candidate-id <cid> --rerun-failed --recovery-preview <预览>",
            "live_request_count": 0,
        }, 0
    if not getattr(arguments, "acknowledge_live_requests", False):
        raise ConfigurationError("候选续跑会经候选网关发真实请求，必须确认 --acknowledge-live-requests。")
    if current == "profile_approved":
        if getattr(arguments, "rerun_failed", False):
            raise ConfigurationError(f"候选 {candidate_id} 还没有 attempt，没有可以 --rerun-failed 的失败 attempt。")
        result = capture.run_capture_attempt(arguments, CANDIDATE_PHASE)
    elif current == "candidate_capture_failed":
        if not getattr(arguments, "rerun_failed", False):
            raise ConfigurationError("失败候选 attempt 只能显式使用 resume --candidate-id <cid> --rerun-failed --recovery-preview <已批准的恢复预览>。")
        if getattr(arguments, "recovery_preview", None) is None:
            raise ConfigurationError("resume --rerun-failed 必须提供 --recovery-preview（已批准的 recovery-preview/v1）。")
        result = _run_with_lease(campaign_dir, manifest, arguments, phase=CANDIDATE_PHASE, candidate_id=candidate_id)
    else:
        raise ConfigurationError(f"候选状态 {current} 没有可续跑的动作：{status.get('next_command', '')}")
    return result, _resume_exit_code(result)


def _run_with_lease(
    campaign_dir: Path,
    manifest: dict[str, Any],
    arguments: argparse.Namespace,
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
    reuse: Any | None = None,
) -> dict[str, Any]:
    """在 Campaign 租约下执行恢复 attempt：强杀留下的过期租约由本次显式续跑接管（写停线收据）。"""

    try:
        active = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
        if active is not None:
            if not claude_upgrade._lease_identity_matches(active, campaign_dir, phase=phase, candidate_id=candidate_id):
                raise ConfigurationError("resume 与当前 Campaign lease 的 phase／candidate 不一致。")
            deadline = capture._bind_attempt_deadline_metadata(active.deadline, phase)
            return _run_recovery_attempt(
                campaign_dir, manifest, arguments, lease=active, deadline=deadline, phase=phase, candidate_id=candidate_id, reuse=reuse
            )
        deadline = claude_upgrade._attempt_deadline(arguments, phase)
        with claude_upgrade.CampaignLease(
            campaign_dir,
            phase=phase,
            candidate_id=candidate_id,
            deadline=deadline,
            command="resume",
            allow_stale_recovery=True,
            campaign_id=str(manifest.get("campaign_id", "")),
        ) as lease:
            return _run_recovery_attempt(
                campaign_dir, manifest, arguments, lease=lease, deadline=deadline, phase=phase, candidate_id=candidate_id, reuse=reuse
            )
    except (incremental_recovery.IncrementalRecoveryError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(f"resume 在预约前停止：{type(error).__name__}：{error}") from error


def _consume_recovery_approval(
    campaign_dir: Path, preview_path: Path, *, phase: str = PHASE, candidate_id: str | None = None
) -> dict[str, Any]:
    try:
        return reconciler.load_approved_recovery_preview(campaign_dir, Path(preview_path), phase=phase, candidate_id=candidate_id)
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


def _verify_environment_continuity(
    campaign_dir: Path,
    source_attempt_id: str,
    before_manifest: Mapping[str, Any],
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
    hooks: Any | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """来源 attempt 的 after 探针与本轮 before 探针在承接比较的三类快照（两个容器）逐字相等，否则零请求失败。

    候选续跑（c1）另核网关：来源 attempt 的网关 after 快照与本轮网关 before 快照逐字段相等（``GATEWAY_COMPARED_FIELDS``），
    结果记在 ``gateway``；不等同样按连续性漂移零请求失败。
    """

    # 后继候选复用（c3）的来源 attempt 在前序 Campaign，由调用方给出根目录。
    source_root = source_root if source_root is not None else _attempt_root(campaign_dir, source_attempt_id, phase, candidate_id)
    after_path = source_root / "evidence" / "environment" / "after" / capture.PROBE_MANIFEST_NAME
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
    continuity: dict[str, Any] = {
        "schema_version": "claude-upgrade-attempt-continuity/v1",
        "source_attempt_id": source_attempt_id,
        "compared_kinds": list(capture.CONTINUITY_PROBE_KINDS),
        "source_after_probe_sha256": file_sha256(after_path),
    }
    if hooks is not None:
        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

        try:
            continuity["gateway"] = hooks.verify_continuity(source_root)
        except candidate_capture.GatewayContinuityDrift as error:
            raise EnvironmentContinuityDrift(
                f"承接失败：{error}，被承接任务的证据前提不再成立；本 attempt 在任何 Job 执行前失败（零请求）。"
                "reconcile-attempt 对账后恢复预览全部重跑（不承接），批准后 resume --rerun-failed。"
            ) from error
    return continuity


def _reserve_recovery_attempt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    jobs: Sequence[Any],
    deadline: Any,
    lease: Any,
    plan: Mapping[str, Any],
    consumed: Mapping[str, Any] | None,
    phase: str = PHASE,
    candidate_id: str | None = None,
    reuse: Any | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Campaign 锁内原子发布恢复 attempt：同一临时目录里写 reservation.json 与 recovery-source.json 后整体改名。

    候选续跑的恢复来源收据沿用同一 v1 字段闭集：候选 ID 由所在目录与预约收据的 ``phase``／``candidate_id`` 表达。
    """

    deadline.check("attempt:reserve:start")
    lease.check("attempt:reserve:start")
    source_id = str(plan["source_attempt_id"])
    _scope(phase, candidate_id)
    with claude_upgrade._campaign_lock(campaign_dir, deadline=deadline):
        if phase == PHASE and capture._official_sealed(campaign_dir):
            raise ConfigurationError("官方证据已经封存，禁止创建新 attempt。")
        if phase == CANDIDATE_PHASE:
            from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

            candidate_capture.require_unsealed(campaign_dir, str(candidate_id), revision=None)
        if reuse is None:
            require_current_source(campaign_dir, manifest, source_id, phase=phase, candidate_id=candidate_id)
        else:
            # 后继候选复用（c3）：本候选还没有任何预约；官方阶段与其它候选没有未封存的预约或 attempt。
            if claude_upgrade._ordered_capture_attempts(campaign_dir, phase, candidate_id, _manifest=manifest):
                raise ConfigurationError(f"候选 {candidate_id} 已有 attempt，后继候选复用只用于首个 attempt。")
            foreign = [
                *claude_upgrade._active_unsealed_attempts(campaign_dir, PHASE, _manifest=manifest),
                *claude_upgrade._active_unsealed_attempts(campaign_dir, CANDIDATE_PHASE, _manifest=manifest),
            ]
            if foreign:
                raise ConfigurationError(f"Campaign 还有未收口的预约或待封存 attempt，禁止后继候选复用：{foreign}")
        if not lease.acquired:
            raise ConfigurationError("reservation 缺少 active Campaign lease。")
        lease_payload = lease.payload
        if lease_payload.get("campaign_id") != manifest.get("campaign_id"):
            raise ConfigurationError("reservation 与 Campaign lease 身份不一致。")
        attempts_root = capture._private_directory(
            campaign_dir / capture._attempt_relative(capture.MODE_FORMAL, phase, candidate_id) / "attempts", campaign_dir
        )
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
            "phase": phase,
            "candidate_id": candidate_id if phase == CANDIDATE_PHASE else None,
            "candidate_purpose": manifest["campaign_purpose"] if phase == CANDIDATE_PHASE else None,
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
        if reuse is not None:
            from tools.official_client_capture import claude_upgrade_candidate_reuse as candidate_reuse

            return _publish_reservation(
                attempts_root,
                final_root,
                reservation,
                candidate_reuse.SOURCE_FILENAME,
                reuse.source_receipt(attempt_id=attempt_id, reservation=reservation, campaign_id=str(manifest["campaign_id"])),
            )
        assert consumed is not None
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
        return _publish_reservation(attempts_root, final_root, reservation, RECOVERY_SOURCE_FILENAME, source)


def _publish_reservation(
    attempts_root: Path, final_root: Path, reservation: Mapping[str, Any], source_name: str, source: Mapping[str, Any]
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """同一临时目录里写预约收据与来源收据后整体改名（调用方持 Campaign 锁）。"""

    temporary_root = Path(tempfile.mkdtemp(prefix=".reservation-", dir=attempts_root))
    temporary_root.chmod(0o700)
    try:
        claude_upgrade._secure_write_json_once(temporary_root / "reservation.json", dict(reservation))
        claude_upgrade._secure_write_json_once(temporary_root / source_name, dict(source))
        os.rename(temporary_root, final_root)
    except BaseException:
        for name in ("reservation.json", source_name):
            (temporary_root / name).unlink(missing_ok=True)
        try:
            temporary_root.rmdir()
        except OSError:
            pass
        raise
    return final_root, dict(reservation), dict(source)


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
    *,
    phase: str = PHASE,
    candidate_id: str | None = None,
) -> dict[str, Any] | None:
    """执行集合作业的固定证据根已存在时先写取代收据、再改名为同级 ``<名>.superseded-<本 attempt>``（核对 inode）。"""

    registrants = claude_upgrade._evidence_root_registrants(
        campaign_dir, manifest, phase=phase, candidate_id=candidate_id, exclude_attempt_id=attempt_root.name
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
        "phase": phase,
        "candidate_id": candidate_id,
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


def _segment_checkpoint_evidence(result: Mapping[str, Any], layout: Any) -> dict[str, Any]:
    """恢复段检查点按宿主路径冻结内容清单，避免把容器逻辑路径交给文件读取器。"""

    from tools.official_client_capture import claude_upgrade_attempt_recovery

    roots = [str(capture._host_path(str(value), layout)) for value in result.get("evidence_roots", [])]
    return {"recovery_evidence": claude_upgrade_attempt_recovery.job_inventory(roots)}


def _record_reused_result(
    attempt_root: Path,
    checkpoint_store: Any,
    reservation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    result: dict[str, Any],
    phase: str = PHASE,
    *, execution_attempt_id: str | None = None, segment: bool = False, layout: Any | None = None,
) -> None:
    records = checkpoint_store.records()
    checkpoint_store.append(
        {
            "checkpoint_schema_version": capture.JOB_CHECKPOINT_SCHEMA,
            "client": CLIENT,
            "mode": capture.MODE_FORMAL,
            "campaign_id": manifest["campaign_id"],
            "phase": phase,
            "attempt_id": execution_attempt_id or attempt_root.name,
            "run_nonce": reservation["run_nonce"],
            "item_id": result["id"],
            "status": "complete",
            "disposition": "reused",
            "result_sha256": incremental_recovery.digest(result),
            "result_key": result.get("incremental_result_key"),
            "result": result,
            **(_segment_checkpoint_evidence(result, layout) if segment else {}),
            "previous_checkpoint_sha256": records[-1].get("checkpoint_sha256") if records else None,
        }
    )
    claude_upgrade._secure_write_json_once(attempt_root / f"job-{result['id']}.json", result)


def _recovery_source_name(reuse: Any | None) -> str:
    if reuse is None:
        return RECOVERY_SOURCE_FILENAME
    from tools.official_client_capture import claude_upgrade_candidate_reuse as candidate_reuse

    return candidate_reuse.SOURCE_FILENAME


def _run_recovery_attempt(
    campaign_dir: Path,
    manifest: dict[str, Any],
    arguments: argparse.Namespace,
    *,
    lease: Any,
    deadline: Any,
    phase: str = PHASE,
    candidate_id: str | None = None,
    reuse: Any | None = None,
) -> dict[str, Any]:
    """执行一次恢复 attempt（正式）；attempt 合同与 B2-06 的 run 相同，复用结果只读承接，执行集合逐 Job 执行与收口。

    后继候选复用（c3，``reuse`` 是 ``claude_upgrade_candidate_reuse.CandidateReuse``）：不消费恢复预览，复用闭集在同一位置由
    ``reuse`` 现场重算并核对批准摘要；来源 attempt 在前序 Campaign；执行集合非空时才核连续性。

    候选续跑（c1）：预约前走与 ``capture-candidate run`` 相同的候选环节（``CandidateRunHooks``：入口门、身份、管理凭据、
    网关只读就绪与 selector 观察），before 探针之后采网关 before 快照，有复用时另核网关连续性，attempt 末采网关 after
    快照并写候选字段——与首个候选 attempt 同一合同。
    """

    mode = capture.MODE_FORMAL
    _prefix, label = _scope(phase, candidate_id)
    # —— 预约前门禁（零写入；只有恢复授权的消费追加计时账本事件） ——
    claude_upgrade._reject_contaminated_campaign(campaign_dir)
    if phase == PHASE and capture._official_sealed(campaign_dir):
        raise ConfigurationError("官方证据已经封存，禁止续跑。")
    current_tool = claude_upgrade._tool_identity(include_git=False)
    claude_upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action="resume --rerun-failed")
    hooks: Any | None = None
    if phase == CANDIDATE_PHASE:
        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

        hooks = candidate_capture.CandidateRunHooks(campaign_dir, str(candidate_id), mode)
        manifest = hooks.admit(manifest, deadline=deadline)
    jobs = claude_upgrade._campaign_jobs(campaign_dir, manifest, phase, candidate_id=candidate_id)
    layout = capture._capture_layout(campaign_dir, manifest)
    claude_upgrade._verify_execution_tree(capture._host_path(layout.capture_tool_root, layout).parents[1])
    binary_verification = claude_upgrade._verify_official_binaries(manifest, deadline=deadline)
    preconditions = capture._container_preconditions(manifest, layout, mode=mode, jobs=jobs, deadline=deadline, heartbeat=None, phase=phase)
    identity = hooks.identity(manifest, layout) if hooks is not None else capture._attempt_identity(manifest, layout, mode)
    _require_no_residual_processes(manifest, deadline)
    if hooks is not None:
        # 网关只读就绪与 selector 观察在消费恢复授权之前（不合格时账本不动）；管理凭据按执行集合在闭集冻结之后核。
        hooks.pre_reservation(manifest, layout, (), deadline=deadline)
    consumed: dict[str, Any] | None = None
    if reuse is not None:
        # 后继候选复用：身份子集、逐 Job 判据与证据复核都在 compute 里；批准摘要必须等于现场重算的预览。
        reuse_plan = reuse.compute(manifest, jobs=jobs, identity=identity, current_tool=current_tool, layout=layout)
        reuse.approve()
        source_id = reuse.source_attempt_id
        plan = {"source_attempt_id": source_id, **reuse_plan}
    else:
        consumed = _consume_recovery_approval(campaign_dir, Path(arguments.recovery_preview), phase=phase, candidate_id=candidate_id)
        source_id = str(consumed["source_attempt_id"])
        plan = recovery_plan(campaign_dir, manifest, source_attempt_id=source_id, jobs=jobs, phase=phase, candidate_id=candidate_id)
        _require_plan_matches_preview(plan, consumed)
        if plan["source_identity"] is not None and dict(plan["source_identity"]) != dict(identity):
            raise ConfigurationError(f"来源 attempt 的{label}运行坐标与当前不同，不能承接其结果。")
    reuse_ids = set(plan["reuse_job_ids"])
    execute_jobs = [job for job in jobs if job.job_id in set(plan["execute_job_ids"])]
    for job in execute_jobs:
        capture._job_tool_dependency_files(job, current_tool)
    if hooks is not None:
        candidate_capture.require_admin_token_for(execute_jobs)
    capture._require_capture_budget(deadline, operation="attempt:reservation-admission", reservation=True)
    # —— 原子预约（此后一律写 attempt 收据） ——
    attempt_root, reservation, recovery_source = _reserve_recovery_attempt(
        campaign_dir,
        manifest,
        identity=identity,
        jobs=jobs,
        deadline=deadline,
        lease=lease,
        plan=plan,
        consumed=consumed,
        phase=phase,
        candidate_id=candidate_id,
        reuse=reuse,
    )
    return _execute_recovery_attempt(
        campaign_dir, manifest, lease=lease, deadline=deadline, phase=phase, candidate_id=candidate_id,
        attempt_root=attempt_root, reservation=reservation, recovery_source=recovery_source,
        hooks=hooks, jobs=jobs, execute_jobs=execute_jobs, identity=identity, current_tool=current_tool,
        layout=layout, binary_verification=binary_verification, preconditions=preconditions,
        plan=plan, source_id=source_id, reuse=reuse,
    )


def _execute_recovery_attempt(
    campaign_dir: Path, manifest: dict[str, Any], *, lease: Any, deadline: Any,
    phase: str, candidate_id: str | None, attempt_root: Path, reservation: Mapping[str, Any],
    recovery_source: Mapping[str, Any], hooks: Any | None, jobs: Sequence[Any], execute_jobs: Sequence[Any],
    identity: Mapping[str, Any], current_tool: Mapping[str, Any], layout: Any,
    binary_verification: Mapping[str, Any], preconditions: Any, plan: Mapping[str, Any], source_id: str,
    reuse: Any | None = None, segment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Claude 续跑与恢复段共用的预约后执行体：Job 执行、检查点、环境复核和两级权限收口。

    普通续跑仍生成 attempt.json；恢复段由已冻结的 segment 上下文生成段摘要，Job 身份使用原 attempt 加段号。
    恢复段只写新证据根，不取代旧根；逐 Job 检查点另冻结内容清单，供失败段的后继复用逐文件重放。
    """

    mode = capture.MODE_FORMAL
    reuse_ids = set(plan["reuse_job_ids"])
    execution_attempt_id = attempt_root.name if segment is None else f"{reservation['attempt_id']}.{reservation['recovery_revision']}"
    log_root = capture._private_directory(attempt_root / "logs", campaign_dir)
    evidence_root = capture._private_directory(attempt_root / "evidence", campaign_dir)
    environment_root = capture._private_directory(evidence_root / "environment", evidence_root)
    heartbeat_path = attempt_root / "watchdog-heartbeat.json"
    context = capture.ScenarioReceiptContext(
        campaign_id=str(manifest["campaign_id"]),
        attempt_id=execution_attempt_id,
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
        if hooks is not None:
            hooks.before_jobs(manifest, layout, environment_root, attempt_observations, deadline=deadline, heartbeat=heartbeat)
        if reuse_ids:
            if segment is None and (reuse is None or execute_jobs):
                # 后继候选复用在执行集合为空时不做连续性核对：不发请求、不新增证据，被复用证据的环境前提由来源 attempt
                # 自己的前后探针与网关恢复核验证明（绑定在复用来源收据里）。
                continuity = _verify_environment_continuity(
                    campaign_dir, source_id, before_manifest, phase=phase, candidate_id=candidate_id, hooks=hooks,
                    source_root=reuse.source_root if reuse is not None else None,
                )
            for job in jobs:
                if job.job_id in reuse_ids:
                    reused = dict(plan["reuse_results"][job.job_id])
                    results.append(reused)
                    _record_reused_result(attempt_root, checkpoint_store, reservation, manifest, reused, phase, execution_attempt_id=execution_attempt_id, segment=segment is not None, layout=layout)
            capture._write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:reused-results-recorded", force=True, attempt_root=attempt_root)
        if execute_jobs and segment is None:
            supersession = _supersede_occupied_roots(
                campaign_dir, manifest, attempt_root, execute_jobs, layout, phase=phase, candidate_id=candidate_id
            )
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
                    "phase": phase,
                    "attempt_id": execution_attempt_id,
                    "run_nonce": reservation["run_nonce"],
                    "item_id": job.job_id,
                    "status": "complete" if result.get("status") == "complete" else "failed",
                    "disposition": "executed",
                    "result_sha256": incremental_recovery.digest(result),
                    "result_key": result.get("incremental_result_key"),
                    "result": result,
                    **(_segment_checkpoint_evidence(result, layout) if segment is not None and result.get("status") == "complete" else {}),
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
        if hooks is not None:
            # 候选网关 after 快照：与 before 逐字段比较，漂移或采不到都判环境污染（deadline 到期也采）。
            gateway_contaminated, gateway_error = hooks.after_jobs(manifest, layout, environment_root, attempt_observations)
            contaminated = contaminated or gateway_contaminated
            restoration_error = restoration_error or gateway_error
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

    candidate_fields: dict[str, Any] = {}
    if hooks is not None:
        candidate_fields, fields_error = hooks.attempt_fields(
            manifest,
            evidence_root=evidence_root,
            environment_root=environment_root,
            results=results,
            capture_before=(environment_root / "before" / capture.PROBE_MANIFEST_NAME) if before_manifest else None,
            capture_after=(environment_root / "after" / capture.PROBE_MANIFEST_NAME) if after_manifest else None,
            capture_drifted=attempt_drift,
        )
        execution_error = execution_error or fields_error
    closeout_roots = [evidence_root, log_root]
    permission_binding: dict[str, Any] | None = None
    permission_error: BaseException | None = None
    try:
        from tools.official_client_capture import claude_upgrade_predecessor_sealed as predecessor_sealed

        container_roots = predecessor_sealed.closeout_roots(
            results if segment is None else [row for row in results if row.get("recovered_from") is None],
            layout, owned_roots=closeout_roots
        )
        closeout_roots.extend(capture._external_host_roots(container_roots, layout))
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
    if hooks is not None:
        # 与首个候选 attempt 同名：共享的候选恢复准入读 environment.restoration_report（网关隔离恢复核验文件）。
        environment["restoration_report"] = candidate_fields["gateway_recovery"]["file"]
    attempt_payload = {
            "campaign_id": manifest["campaign_id"],
            "phase": phase,
            "candidate_id": candidate_id,
            "status": status,
            "identity": identity,
            "results": results,
            "attempt_failure_observations": (
                capture._normalize_failure_observations(attempt_observations, phase=phase) if attempt_observations else []
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
                "source": _binding(campaign_dir, attempt_root / _recovery_source_name(reuse)) if segment is None else None,
                "source_attempt_id": source_id,
                **({"source_campaign_id": reuse.context["source_campaign"]["campaign_id"]} if reuse is not None else {}),
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
                f"capture-{phase} seal（{'B2-07' if phase == PHASE else 'B4-02'}）"
                if status == "awaiting_receipts"
                else "环境隔离与恢复（environment-cleanup／environment-isolate，B2-08）"
                if status == "environment_contaminated"
                else "reconcile-attempt → 批准恢复预览 → resume --rerun-failed（B2-08）"
            ),
            **candidate_fields,
        }
    if segment is not None:
        from tools.official_client_capture import claude_upgrade_attempt_segment

        attempt_payload.pop("recovery", None)
        attempt_payload.update(client=CLIENT, mode=mode, execute_jobs=sorted(job.job_id for job in execute_jobs))
        attempt = claude_upgrade_attempt_segment.write_summary(
            campaign_dir, manifest, attempt_root, reservation, attempt_payload, segment=segment
        )
    else:
        attempt = capture._write_capture_attempt(
            campaign_dir, attempt_root, attempt_payload, manifest=manifest, reservation=reservation, mode=mode
        )
    if status == "environment_contaminated":
        try:
            claude_upgrade._secure_write_json_once(
                campaign_dir / capture.CONTAMINATION_MARKER,
                {
                    "schema_version": capture.ENVIRONMENT_CONTAMINATION_SCHEMA,
                    "client": CLIENT,
                    "mode": mode,
                    "phase": phase,
                    "attempt_id": execution_attempt_id,
                    "drifted_kinds": attempt_drift,
                    "contaminated_jobs": sorted(str(item.get("id")) for item in results if item.get("environment_contaminated")),
                    **(hooks.contamination_marker_fields() if hooks is not None else {}),
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
        "phase": phase,
        **({"candidate_id": candidate_id} if phase == CANDIDATE_PHASE else {}),
        "attempt_id": attempt_root.name if segment is None else reservation["attempt_id"],
        "attempt": str(attempt_root),
        "attempt_digest": attempt.get("attempt_digest", attempt.get("attempt_recovery_digest")),
        **({"recovery_revision": reservation["recovery_revision"], "evaluation_baseline": reservation["evaluation_baseline"]} if segment is not None else {}),
        "run_nonce": reservation["run_nonce"],
        "started_at_utc": reservation["started_at_utc"],
        "recovery": {
            "source_attempt_id": source_id,
            **({"source_campaign_id": reuse.context["source_campaign"]["campaign_id"]} if reuse is not None else {}),
            "reuse_job_ids": sorted(reuse_ids),
            "execute_job_ids": sorted(job.job_id for job in execute_jobs),
            "recovery_source_sha256": recovery_source.get("recovery_source_sha256", recovery_source.get("reuse_source_sha256")),
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
    """``harden-evidence-permissions``：Claude 只提供 ``rebind-boundary``（B4-02 收尾），其余动作不提供（理由见模块说明第五节）。"""

    if getattr(arguments, "harden_action", None) == "rebind-boundary":
        from tools.official_client_capture import claude_upgrade_candidate_seal

        if getattr(arguments, "attempt_recovery", None):
            from tools.official_client_capture import claude_upgrade_segment_seal

            candidate_id = getattr(arguments, "candidate_id", None)
            if not candidate_id:
                raise ConfigurationError("恢复段边界重绑必须提供 --candidate-id。")
            return claude_upgrade_segment_seal.rebind_boundary(
                Path(arguments.campaign_dir), str(candidate_id), str(arguments.attempt_id), str(arguments.attempt_recovery),
                str(getattr(arguments, "operator", None) or "harden-evidence-permissions"),
            ), 0
        record = claude_upgrade_candidate_seal.rebind_sealed_boundary(
            Path(arguments.campaign_dir),
            str(arguments.attempt_id),
            candidate_id=getattr(arguments, "candidate_id", None),
            operator=getattr(arguments, "operator", None),
        )
        return record, 0
    raise ConfigurationError(
        "Claude 不提供 harden-evidence-permissions 的 preview／apply／replay／upgrade-closeout：run 从第一个受管版本起就在每个"
        " Job 终态与 run 末各做一次 v2 证据权限收口（B2-06），收口重放随读取 attempt 默认执行（B2-07），没有缺收口的历史 attempt"
        "需要事后补做；run 末收口失败的 attempt 记为 failed，经 reconcile-attempt → 批准恢复预览 → resume --rerun-failed 由新"
        " attempt 全部复用、零执行地重新收口。已封存候选证据只有元数据漂移时用 rebind-boundary（带 --candidate-id）。"
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
    target.add_argument("--attempt-recovery", help="已封存候选的 transient 恢复段 ar<k>。")
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
    target.add_argument(
        "--candidate-reuse-source-campaign-dir", type=Path, help="后继候选复用（B4-01 c3）：总计划冻结的前序 Campaign 目录。"
    )
    target.add_argument("--candidate-reuse-source-candidate-id", help="后继候选复用：前序 Campaign 里的候选 ID。")
    target.add_argument("--candidate-reuse-source-attempt-id", help="后继候选复用：前序唯一待封存的候选 attempt。")
    target.add_argument("--approve-candidate-reuse-sha256", help="后继候选复用：--preview-recovery 输出的 preview_sha256。")
    target.add_argument("--max-wall-seconds", type=int, default=None, help=f"一次受管命令的单调墙钟预算（默认 {claude_upgrade.DEFAULT_ATTEMPT_WALL_SECONDS} 秒）。")
    target.add_argument("--heartbeat-seconds", type=int, default=None, help="watchdog heartbeat 间隔（秒）。")
    target.add_argument("--acknowledge-live-requests", action="store_true")


def add_harden_arguments(target: argparse.ArgumentParser) -> None:
    target.add_argument("harden_action", choices=("preview", "apply", "replay", "upgrade-closeout", "rebind-boundary"))
    target.add_argument("--attempt-id", required=True)
    target.add_argument("--approve-sha256")
    target.add_argument("--candidate-id")
    target.add_argument("--operator")
    target.add_argument("--attempt-recovery", help="增量封存恢复段 ar<k> 的边界重绑。")


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
