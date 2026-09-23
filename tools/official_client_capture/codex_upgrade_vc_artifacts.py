#!/usr/bin/env python3
"""生成并校验 Codex VC-0～VC-6 的小型控制制品。

本模块不读取原始抓包正文，也不执行网络请求。调用方负责把返回值写入
Campaign 的不可变目录；这里负责字段闭合、集合闭合和自摘要复算。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any


DISCOVERY_INVENTORY_SCHEMA = "codex-upgrade-discovery-inventory/v1"
CAMPAIGN_PLAN_SCHEMA = "codex-upgrade-campaign-plan/v1"
VC_CHECKPOINT_SCHEMA = "codex-upgrade-vc-checkpoint/v1"
# 改造 2（候选级 revision）：batch v2 新增 candidate_revision／candidate_id（候选级阶段必填，
# Campaign 级阶段为 null）；v1 只读兼容。
# 改造 5（评估失败局部恢复）：batch v3 再加 evaluation_baseline／baseline_commit_sha256
# （候选级 VC-5 的 b0 与 Campaign 级为 null）、evaluator_digests（候选级 VC-5 必填四项，其余
# null）与动作可选 output_bindings；v2／v1 只读兼容。
VC_BATCH_SCHEMA = "codex-upgrade-vc-batch/v3"
VC_BATCH_V2_SCHEMA = "codex-upgrade-vc-batch/v2"
VC_BATCH_LEGACY_SCHEMA = "codex-upgrade-vc-batch/v1"
# 改造 5：评估基线 b<K>（候选内 append-only 的离线评估维度）控制制品。
EVALUATION_BASELINE_SCHEMA = "codex-upgrade-evaluation-baseline/v1"
EVALUATION_BASELINE_PREPARED_SCHEMA = "codex-upgrade-evaluation-baseline-prepared/v1"
EVALUATION_BASELINE_AUTHORIZATION_SCHEMA = (
    "codex-upgrade-evaluation-baseline-authorization/v1"
)
EVALUATION_BASELINE_COMMIT_SCHEMA = "codex-upgrade-evaluation-baseline-commit/v1"
EVALUATION_BASELINE_ABANDON_SCHEMA = "codex-upgrade-evaluation-baseline-abandon/v1"
EVALUATION_CHECKPOINT_SCHEMA = "codex-upgrade-evaluation-checkpoint/v1"
EVALUATION_RUN_SCHEMA = "codex-upgrade-evaluation-run/v1"
EVALUATION_FAILURE_DIAGNOSIS_SCHEMA = "codex-upgrade-evaluation-failure-diagnosis/v1"
ACTION_OUTPUT_BINDING_SCHEMA = "codex-upgrade-action-output-binding/v1"
MANIFEST_PROJECTION_SCHEMA = "codex-upgrade-evidence-manifest-projection/v1"
EFFECTIVE_RESULTS_SCHEMA = "codex-upgrade-effective-results/v1"
EVALUATION_BASELINE_KINDS = ("evaluator-only", "attempt-recovery")
# 失败来源只由失败父 run 的动作推出（三者互斥）；复用授权只由 stop-receipt 的
# action_outputs_sha256 是否为 null 决定，读侧不得以字段缺失表达语义。
FAILURE_SOURCES = ("assertion-failed", "offline-compare-failed", "offline-accept-failed")
REUSE_AUTHORITIES = ("anchored", "none")
ROOT_CAUSE_CLASSES = (
    "evaluator-defect",
    "transient-environment",
    "candidate-source",
    "approval-inputs",
)
EVALUATION_STAGES = ("capture-candidate", "compare", "assertions", "accept")
EVALUATOR_DIGEST_FIELDS = (
    "checker_sha256",
    "builder_sha256",
    "compare_reader_sha256",
    "accept_reader_sha256",
)
CANDIDATE_PHASES = ("VC-4", "VC-5", "VC-6")
CANDIDATE_REVISION_SCHEMA = "codex-upgrade-candidate-revision/v1"
CANDIDATE_REVISION_COMMIT_SCHEMA = "codex-upgrade-candidate-revision-commit/v1"
CANDIDATE_REVISION_SEAL_SCHEMA = "codex-upgrade-candidate-revision-seal/v1"
CANDIDATE_INVALIDATION_SCHEMA = "codex-upgrade-candidate-invalidation/v1"
CANDIDATE_INVALIDATION_DIAGNOSIS_SCHEMA = "candidate-invalidation-diagnosis/v1"
CANDIDATE_INVALIDATION_CONCLUSION = "candidate_source_change_required"
IDENTITY_SNAPSHOT_SOURCES = ("build_receipt", "attempt_candidate_identity", "candidate_source")
VC_ACTION_PLAN_SCHEMA = "codex-upgrade-vc-action-plan/v1"
INTERRUPTED_RECOVERY_CONTRACT_SCHEMA = (
    "codex-upgrade-interrupted-recovery-contract/v1"
)
GATE_REQUIREMENTS_SCHEMA = "codex-post-promotion-gate-requirements/v1"
GATE_MAPPING_SCHEMA = "codex-post-promotion-gate-mapping/v2"
GATE_PLAN_SCHEMA = "codex-post-promotion-gate-plan/v1"
LEGACY_CANDIDATE_BUILD_SCHEMA = "codex-upgrade-candidate-build-receipt/v1"
CANDIDATE_BUILD_SCHEMA = "codex-upgrade-candidate-build-receipt/v2"
CANDIDATE_DELIVERY_SCHEMA = "codex-upgrade-candidate-delivery-receipt/v1"
# 改造 4（staging/WAL）：批次先落 staging，父 run 取得执行权前不占正式序号；
# 唯一原子提交点是 control/vc/commits/ 下的 COMMIT 记录，永不移动、永不删除。
VC_COMMIT_SCHEMA = "codex-upgrade-vc-commit/v1"
STAGING_MARKER_SCHEMA = "codex-upgrade-vc-staging-prepared/v1"
STAGING_ABORT_SCHEMA = "codex-upgrade-staging-abort/v1"
PARENT_START_FAILURE_SCHEMA = "codex-upgrade-parent-start-failure/v1"
# Campaign 总计划的批次模型：legacy = 改造前直接写正式 batch；staging = 先 staging 再 COMMIT。
# 历史 plan 没有该字段，按 legacy 解释，其 plan_sha256 不变。
BATCH_MODELS = ("legacy", "staging")
# ABORT 的 stage：prepare／parent-run-create 是没有父 run 的 P1；parent-run 是父 run
# prepared 后被遗弃（P2）或 watchdog 中止；其余五个是 commit 步骤中 COMMIT 前的失败步骤
# （与监督器 stop reason ``staging-commit-failed:<step>`` 的后缀同名）。改造 5 在
# nonce-mismatch 之后插入 evaluator-digests：正式 COMMIT 前核对当前 evaluator 四项摘要
# 等于批次冻结值，不等即中止、序号不占、同序号重新编译。
STAGING_ABORT_STAGES = (
    "prepare",
    "parent-run-create",
    "parent-run",
    "nonce-mismatch",
    "evaluator-digests",
    "commit-ledger",
    "commit-publish",
    "commit-mark",
)
STAGING_ABORT_FAILURE_KINDS = (
    "abandoned",
    "prepare-failed",
    "commit-failed",
    "interrupted",
)
PARENT_START_FAILURE_KINDS = ("owner-lost", "state-write-failed")

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RULE_RE = re.compile(r"^SPEC-[A-Z0-9]+-[0-9]{3}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ARCHITECTURE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
VC_PHASES = ("VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6")
VC_DEPENDENCIES = {
    "VC-0": [],
    "VC-1": ["VC-0"],
    "VC-2": ["VC-1"],
    "VC-3": ["VC-2"],
    "VC-4": ["VC-3"],
    "VC-5": ["VC-4"],
    "VC-6": ["VC-5"],
}

PUBLIC_GATES: tuple[tuple[str, str], ...] = (
    ("catalog-projection", "验证晋升后的 Catalog、ReleaseGraph 与运行投影一致"),
    ("official-egress-version-leak-ast", "验证受管源码 AST 不含未登记版本泄漏"),
    ("version-leak", "验证生成资产和运行配置不含未登记版本泄漏"),
    ("version-leak-self-test", "验证版本泄漏门禁自身能识别正负夹具"),
)

# VC-5 步骤 7（production_replacement 的 canonical 交接）四个零请求项到
# codex_upgrade 子命令的冻结映射。批次里承载这些项的动作必须逐字落在映射内：
# canonical-import 只能是带批准摘要的 canonical-import，其余三项只能是对应
# ``--canonical-step`` 的 canonical-advance；反过来，任何 canonical 子命令都
# 不得挂在别的 item 名下。父监督器把动作失败升级为 post-run-tooling 时按同一
# 映射从命令里提取 Candidate／attempt，只检查该指定 attempt，不再要求全
# Campaign 恰好一个等待收据的 attempt。
CANONICAL_VC5_ITEM_COMMANDS: dict[str, tuple[str, str | None]] = {
    "canonical-import": ("canonical-import", None),
    "canonical-seal": ("canonical-advance", "seal"),
    "canonical-compare": ("canonical-advance", "compare"),
    "canonical-accept": ("canonical-advance", "accept"),
}
# VC-6 步骤（§4.6.7 生产激活链）同样是零请求 canonical 项：两个静态项由固定
# ``--canonical-step`` 承载，退休项的 item 名带版本（``retire-<版本>``），必须与
# ``--retire-version`` 精确一致，才能防止把别的版本的退休挂到冻结计划项下。
CANONICAL_VC6_ITEM_COMMANDS: dict[str, tuple[str, str | None]] = {
    "production-activation": ("canonical-advance", "production-activation"),
    "rollback-verification": ("canonical-advance", "rollback-verification"),
}
CANONICAL_RETIRE_ITEM_RE = re.compile(r"^retire-([0-9]+\.[0-9]+\.[0-9]+)$")
CANONICAL_RETIRE_STEP = "retire"
CANONICAL_ITEM_COMMANDS: dict[str, tuple[str, str | None]] = {
    **CANONICAL_VC5_ITEM_COMMANDS,
    **CANONICAL_VC6_ITEM_COMMANDS,
}
# 只含静态项；退休项是动态 item 名，判定一律走 ``canonical_item_phase``。
CANONICAL_ITEM_IDS = frozenset(CANONICAL_ITEM_COMMANDS)
# 父监督器按动作清单顺序执行，而清单又必须按 action_id 排序；canonical 各阶段有
# 严格前后依赖（VC-5：import → seal → compare → accept；VC-6：生产激活 → 回滚
# 验证 → 退休），编译期就按这个顺序校验动作的相对次序，操作员必须用带序号的
# action_id（例如 canonical-1-import、canonical-5-production-activation）表达它。
CANONICAL_VC5_ITEM_ORDER: tuple[str, ...] = tuple(CANONICAL_VC5_ITEM_COMMANDS)
CANONICAL_VC6_ITEM_ORDER: tuple[str, ...] = (
    "production-activation",
    "rollback-verification",
)
CANONICAL_ITEM_ORDER: tuple[str, ...] = CANONICAL_VC5_ITEM_ORDER
CANONICAL_ITEM_PHASES: dict[str, str] = {
    **{item: "VC-5" for item in CANONICAL_VC5_ITEM_COMMANDS},
    **{item: "VC-6" for item in CANONICAL_VC6_ITEM_COMMANDS},
}
CANONICAL_SUBCOMMANDS = frozenset(
    subcommand for subcommand, _step in CANONICAL_ITEM_COMMANDS.values()
)


def canonical_item_phase(item_id: Any) -> str | None:
    """返回 canonical item 所属的 VC 阶段；不是 canonical item 时返回 None。"""

    if not isinstance(item_id, str) or not item_id:
        return None
    phase = CANONICAL_ITEM_PHASES.get(item_id)
    if phase is not None:
        return phase
    return "VC-6" if CANONICAL_RETIRE_ITEM_RE.fullmatch(item_id) else None


def is_canonical_item(item_id: Any) -> bool:
    """item 名是否落在 canonical 冻结闭集（含动态退休项）。"""

    return canonical_item_phase(item_id) is not None


def canonical_item_command(item_id: str) -> tuple[str, str | None, str | None]:
    """返回 canonical item 冻结的 ``(子命令, --canonical-step, 退休版本)``。"""

    match = CANONICAL_RETIRE_ITEM_RE.fullmatch(item_id)
    if match is not None:
        return "canonical-advance", CANONICAL_RETIRE_STEP, match.group(1)
    subcommand, step = CANONICAL_ITEM_COMMANDS[item_id]
    return subcommand, step, None


def _canonical_item_rank(item_id: str) -> int:
    """同组内的冻结执行序位；退休项永远排在 VC-6 组末尾。"""

    if item_id in CANONICAL_VC5_ITEM_ORDER:
        return CANONICAL_VC5_ITEM_ORDER.index(item_id)
    if item_id in CANONICAL_VC6_ITEM_ORDER:
        return CANONICAL_VC6_ITEM_ORDER.index(item_id)
    return len(CANONICAL_VC6_ITEM_ORDER)
UPGRADE_CLI_BASENAMES = frozenset({"codex_upgrade.py", "codex-upgrade"})


class VCArtifactError(ValueError):
    """VC 控制制品字段、身份或摘要不可信。"""


def canonical_bytes(value: Any) -> bytes:
    """返回稳定 JSON 字节，供跨工具摘要绑定。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def digest(value: Any) -> str:
    """计算稳定 JSON SHA-256。"""

    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise VCArtifactError(f"{label} 不是小写 SHA-256")
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise VCArtifactError(f"{label} 不是安全标识")
    return value


def _version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise VCArtifactError(f"{label} 不是三段式版本号")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise VCArtifactError(f"{label} 不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VCArtifactError(f"{label} 不是有效时间") from error
    if parsed.tzinfo is None:
        raise VCArtifactError(f"{label} 缺少时区")
    return value


def _self_digest(payload: Mapping[str, Any], field: str, label: str) -> None:
    recorded = _sha256(payload.get(field), f"{label}.{field}")
    unsigned = dict(payload)
    unsigned.pop(field)
    if digest(unsigned) != recorded:
        raise VCArtifactError(f"{label}自摘要不一致")


def build_campaign_plan(
    *,
    campaign_id: str,
    campaign_mode: str,
    campaign_purpose: str,
    baseline_version: str,
    target_version: str,
    created_at_utc: str,
    original_deadline_at_utc: str,
    timing_checkpoint_sha256: str,
    arm64_environment_sha256: str,
    job_rehearsal_sha256: str | None,
    p0_gate_sha256: str | None,
    batch_model: str | None = "staging",
) -> dict[str, Any]:
    """冻结 VC-0 可知的总计划；禁止预填未来阶段才会产生的身份。

    ``batch_model`` 默认 ``staging``（改造 4 之后创建的 Campaign）；传 ``None`` 只用于
    构造与历史 plan 逐字一致的夹具，输出里不带该字段。
    """

    payload = {
        "schema_version": CAMPAIGN_PLAN_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "campaign_mode": campaign_mode,
        "campaign_purpose": campaign_purpose,
        "baseline_version": _version(baseline_version, "baseline_version"),
        "target_version": _version(target_version, "target_version"),
        "created_at_utc": _timestamp(created_at_utc, "created_at_utc"),
        "original_deadline_at_utc": _timestamp(
            original_deadline_at_utc,
            "original_deadline_at_utc",
        ),
        "phase_order": list(VC_PHASES),
        "dependencies": dict(VC_DEPENDENCIES),
        "initial_phase": "VC-0",
        "future_identity_placeholders": False,
        "controls": {
            "timing_checkpoint_sha256": _sha256(
                timing_checkpoint_sha256,
                "timing_checkpoint_sha256",
            ),
            "arm64_environment_sha256": _sha256(
                arm64_environment_sha256,
                "arm64_environment_sha256",
            ),
            "job_rehearsal_sha256": (
                _sha256(job_rehearsal_sha256, "job_rehearsal_sha256")
                if job_rehearsal_sha256 is not None
                else None
            ),
            "p0_gate_sha256": (
                _sha256(p0_gate_sha256, "p0_gate_sha256")
                if p0_gate_sha256 is not None
                else None
            ),
        },
    }
    if batch_model is not None:
        if batch_model not in BATCH_MODELS:
            raise VCArtifactError("Campaign 总计划 batch_model 非法")
        payload["batch_model"] = batch_model
    payload["plan_sha256"] = digest(payload)
    return validate_campaign_plan(payload)


def campaign_plan_batch_model(plan: Mapping[str, Any]) -> str:
    """返回总计划声明的批次模型；历史 plan 缺失即 legacy。"""

    model = plan.get("batch_model")
    if model is None:
        return "legacy"
    if model not in BATCH_MODELS:
        raise VCArtifactError("Campaign 总计划 batch_model 非法")
    return str(model)


def validate_campaign_plan(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "campaign_mode",
        "campaign_purpose",
        "baseline_version",
        "target_version",
        "created_at_utc",
        "original_deadline_at_utc",
        "phase_order",
        "dependencies",
        "initial_phase",
        "future_identity_placeholders",
        "controls",
        "plan_sha256",
    }
    # batch_model 是唯一可选字段：历史 plan 没有它（legacy），新 plan 必须是合法枚举值。
    if not isinstance(value, Mapping) or set(value) - {"batch_model"} != required:
        raise VCArtifactError("Campaign 总计划字段不闭合")
    payload = dict(value)
    if "batch_model" in payload and payload["batch_model"] not in BATCH_MODELS:
        raise VCArtifactError("Campaign 总计划 batch_model 非法")
    if (
        payload.get("schema_version") != CAMPAIGN_PLAN_SCHEMA
        or payload.get("campaign_mode") not in {"preflight_only", "formal"}
        or payload.get("campaign_purpose")
        not in {"validation_only", "production_replacement"}
        or payload.get("phase_order") != list(VC_PHASES)
        or payload.get("dependencies") != VC_DEPENDENCIES
        or payload.get("initial_phase") != "VC-0"
        or payload.get("future_identity_placeholders") is not False
    ):
        raise VCArtifactError("Campaign 总计划阶段、模式或身份边界非法")
    _safe_id(payload.get("campaign_id"), "Campaign plan campaign_id")
    _version(payload.get("baseline_version"), "Campaign plan baseline_version")
    _version(payload.get("target_version"), "Campaign plan target_version")
    created = _timestamp(payload.get("created_at_utc"), "Campaign plan created_at_utc")
    deadline = _timestamp(
        payload.get("original_deadline_at_utc"),
        "Campaign plan original_deadline_at_utc",
    )
    if datetime.fromisoformat(deadline.replace("Z", "+00:00")) <= datetime.fromisoformat(
        created.replace("Z", "+00:00")
    ):
        raise VCArtifactError("Campaign 总截止时间不得早于创建时间")
    controls = payload.get("controls")
    if not isinstance(controls, Mapping) or set(controls) != {
        "timing_checkpoint_sha256",
        "arm64_environment_sha256",
        "job_rehearsal_sha256",
        "p0_gate_sha256",
    }:
        raise VCArtifactError("Campaign 总计划控制绑定不闭合")
    _sha256(controls.get("timing_checkpoint_sha256"), "timing checkpoint")
    _sha256(controls.get("arm64_environment_sha256"), "ARM64 environment")
    if payload["campaign_mode"] == "formal":
        _sha256(controls.get("job_rehearsal_sha256"), "job rehearsal")
        _sha256(controls.get("p0_gate_sha256"), "P0 gate")
    elif any(
        controls.get(field) is not None
        for field in ("job_rehearsal_sha256", "p0_gate_sha256")
    ):
        raise VCArtifactError("preflight_only 总计划不得绑定 Formal Job 演练或 P0 收据")
    _self_digest(payload, "plan_sha256", "Campaign 总计划")
    return payload


def build_vc_checkpoint(
    *,
    campaign_plan: Mapping[str, Any],
    phase: str,
    status: str,
    predecessor_checkpoint: Mapping[str, Any] | None,
    stage_receipt: Mapping[str, Any],
    completed_at_utc: str,
    execute_item_ids: Sequence[str],
    reuse_item_ids: Sequence[str],
    live_request_count: int,
    scanned_bytes: int,
) -> dict[str, Any]:
    """生成阶段封存 checkpoint；后继批次只能引用其文件绑定。"""

    plan = validate_campaign_plan(campaign_plan)
    payload = {
        "schema_version": VC_CHECKPOINT_SCHEMA,
        "campaign_id": plan["campaign_id"],
        "campaign_plan_sha256": plan["plan_sha256"],
        "phase": phase,
        "status": status,
        "predecessor_checkpoint": (
            _checkpoint_reference(predecessor_checkpoint, "predecessor_checkpoint")
            if predecessor_checkpoint is not None
            else None
        ),
        "stage_receipt": _binding(stage_receipt, "stage_receipt"),
        "completed_at_utc": _timestamp(completed_at_utc, "completed_at_utc"),
        "execute_item_ids": sorted(set(execute_item_ids)),
        "reuse_item_ids": sorted(set(reuse_item_ids)),
        "metrics": {
            "live_request_count": live_request_count,
            "scanned_bytes": scanned_bytes,
        },
    }
    payload["checkpoint_sha256"] = digest(payload)
    return validate_vc_checkpoint(payload, plan)


def validate_vc_checkpoint(
    value: Any,
    campaign_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "campaign_plan_sha256",
        "phase",
        "status",
        "predecessor_checkpoint",
        "stage_receipt",
        "completed_at_utc",
        "execute_item_ids",
        "reuse_item_ids",
        "metrics",
        "checkpoint_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("VC checkpoint 字段不闭合")
    payload = dict(value)
    if (
        payload.get("schema_version") != VC_CHECKPOINT_SCHEMA
        or payload.get("phase") not in VC_PHASES
        or payload.get("status") not in {"complete", "blocked", "failed"}
    ):
        raise VCArtifactError("VC checkpoint schema、阶段或状态非法")
    _safe_id(payload.get("campaign_id"), "VC checkpoint campaign_id")
    _sha256(payload.get("campaign_plan_sha256"), "campaign_plan_sha256")
    _timestamp(payload.get("completed_at_utc"), "VC checkpoint completed_at_utc")
    predecessor = payload.get("predecessor_checkpoint")
    if payload["phase"] == "VC-0":
        if predecessor is not None:
            raise VCArtifactError("VC-0 checkpoint 不得存在前序 checkpoint")
    else:
        predecessor_reference = _checkpoint_reference(
            predecessor,
            "predecessor_checkpoint",
        )
        expected_phase = VC_PHASES[VC_PHASES.index(payload["phase"]) - 1]
        if predecessor_reference["phase"] != expected_phase:
            raise VCArtifactError(
                f"{payload['phase']} checkpoint 必须直接承接 {expected_phase}"
            )
    _binding(payload.get("stage_receipt"), "stage_receipt")
    for field in ("execute_item_ids", "reuse_item_ids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in values)
        ):
            raise VCArtifactError(f"VC checkpoint {field} 非法")
    if set(payload["execute_item_ids"]) & set(payload["reuse_item_ids"]):
        raise VCArtifactError("VC checkpoint execute/reuse 集合相交")
    metrics = payload.get("metrics")
    if (
        not isinstance(metrics, Mapping)
        or set(metrics) != {"live_request_count", "scanned_bytes"}
        or any(
            not isinstance(metrics.get(field), int)
            or isinstance(metrics.get(field), bool)
            or metrics[field] < 0
            for field in metrics
        )
    ):
        raise VCArtifactError("VC checkpoint metrics 非法")
    if campaign_plan is not None:
        plan = validate_campaign_plan(campaign_plan)
        if (
            payload["campaign_id"] != plan["campaign_id"]
            or payload["campaign_plan_sha256"] != plan["plan_sha256"]
        ):
            raise VCArtifactError("VC checkpoint 未绑定本轮 Campaign 总计划")
    _self_digest(payload, "checkpoint_sha256", "VC checkpoint")
    return payload


def build_vc_batch(
    *,
    campaign_plan: Mapping[str, Any],
    phase: str,
    sequence: int,
    predecessor_checkpoint: Mapping[str, Any],
    execute_item_ids: Sequence[str],
    reuse_item_ids: Sequence[str],
    actions: Sequence[Mapping[str, Any]],
    compiled_at_utc: str,
    must_start_by_utc: str,
    candidate_revision: int | None = None,
    candidate_id: str | None = None,
    evaluation_baseline: int | None = None,
    baseline_commit_sha256: str | None = None,
    evaluator_digests: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从前序 checkpoint 编译同一 Campaign 的一个不可变执行批次（v3）。

    候选级阶段（VC-4～VC-6）必须绑定当前 revision 与其候选；Campaign 级阶段两字段为 null。
    改造 5：候选级 VC-5 批次冻结当前评估基线（b0 为 null）与 evaluator 四项直接依赖摘要，
    其余阶段三字段为 null；动作可声明 output_bindings（Campaign 相对路径）。
    """

    plan = validate_campaign_plan(campaign_plan)
    payload = {
        "schema_version": VC_BATCH_SCHEMA,
        "campaign_id": plan["campaign_id"],
        "campaign_plan_sha256": plan["plan_sha256"],
        "batch_id": f"{phase.lower()}-{sequence:04d}",
        "sequence": sequence,
        "phase": phase,
        "predecessor_checkpoint": _checkpoint_reference(
            predecessor_checkpoint,
            "predecessor_checkpoint",
        ),
        "execute_item_ids": sorted(set(execute_item_ids)),
        "reuse_item_ids": sorted(set(reuse_item_ids)),
        # 动作经同一规范化（output_bindings 排序），保证 batch 与由它生成的清单逐字一致。
        "actions": _actions(
            [json.loads(json.dumps(dict(item), ensure_ascii=False)) for item in actions],
            execute_item_ids=sorted(set(execute_item_ids)),
            allow_output_bindings=True,
            phase=phase,
        ),
        "compiled_at_utc": _timestamp(compiled_at_utc, "compiled_at_utc"),
        "must_start_by_utc": _timestamp(must_start_by_utc, "must_start_by_utc"),
        "original_deadline_at_utc": plan["original_deadline_at_utc"],
        "candidate_revision": candidate_revision,
        "candidate_id": candidate_id,
        "evaluation_baseline": evaluation_baseline,
        "baseline_commit_sha256": baseline_commit_sha256,
        "evaluator_digests": (
            json.loads(json.dumps(dict(evaluator_digests), ensure_ascii=False))
            if evaluator_digests is not None
            else None
        ),
    }
    payload["batch_sha256"] = digest(payload)
    return validate_vc_batch(payload, plan)


def validate_evaluator_digests(value: Any, label: str) -> dict[str, str]:
    """evaluator 四项直接依赖摘要的精确闭集：checker／builder／compare-reader／accept-reader。"""

    if not isinstance(value, Mapping) or set(value) != set(EVALUATOR_DIGEST_FIELDS):
        raise VCArtifactError(f"{label} evaluator_digests 字段不闭合")
    return {field: _sha256(value.get(field), f"{label} evaluator_digests.{field}") for field in EVALUATOR_DIGEST_FIELDS}


def validate_batch_evaluation_binding(payload: Mapping[str, Any], phase: str, label: str) -> None:
    """改造 5：评估基线两字段成对（b0／Campaign 级为 null）；evaluator_digests 只在候选级 VC-5 非 null。"""

    baseline = payload.get("evaluation_baseline")
    commit_sha256 = payload.get("baseline_commit_sha256")
    digests = payload.get("evaluator_digests")
    if phase == "VC-5":
        if (baseline is None) != (commit_sha256 is None):
            raise VCArtifactError(f"{label} evaluation_baseline 与 baseline_commit_sha256 必须成对")
        if baseline is not None:
            if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 1:
                raise VCArtifactError(f"{label} evaluation_baseline 必须是正整数或 null")
            _sha256(commit_sha256, f"{label} baseline_commit_sha256")
        validate_evaluator_digests(digests, label)
    elif baseline is not None or commit_sha256 is not None or digests is not None:
        raise VCArtifactError(f"{label} 非 VC-5 阶段不得绑定评估基线或 evaluator_digests")


def _validate_batch_candidate_binding(payload: Mapping[str, Any], phase: str, label: str) -> None:
    """候选级阶段必须绑定正整数 revision 与候选 ID；Campaign 级阶段两者必须为 null。"""

    revision = payload.get("candidate_revision")
    candidate_id = payload.get("candidate_id")
    if phase in CANDIDATE_PHASES:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise VCArtifactError(f"{label} 候选级阶段必须绑定正整数 candidate_revision")
        _safe_id(candidate_id, f"{label} candidate_id")
    elif revision is not None or candidate_id is not None:
        raise VCArtifactError(f"{label} Campaign 级阶段不得绑定 candidate_revision／candidate_id")


def validate_vc_batch(
    value: Any,
    campaign_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "campaign_plan_sha256",
        "batch_id",
        "sequence",
        "phase",
        "predecessor_checkpoint",
        "execute_item_ids",
        "reuse_item_ids",
        "actions",
        "compiled_at_utc",
        "must_start_by_utc",
        "original_deadline_at_utc",
        "batch_sha256",
    }
    if not isinstance(value, Mapping):
        raise VCArtifactError("VC batch 字段不闭合")
    schema_version = value.get("schema_version")
    if schema_version == VC_BATCH_SCHEMA:
        required = required | {
            "candidate_revision",
            "candidate_id",
            "evaluation_baseline",
            "baseline_commit_sha256",
            "evaluator_digests",
        }
    elif schema_version == VC_BATCH_V2_SCHEMA:
        required = required | {"candidate_revision", "candidate_id"}
    elif schema_version != VC_BATCH_LEGACY_SCHEMA:
        raise VCArtifactError("VC batch schema、阶段、序号或身份非法")
    if set(value) != required:
        raise VCArtifactError("VC batch 字段不闭合")
    payload = dict(value)
    sequence = payload.get("sequence")
    phase = payload.get("phase")
    if schema_version in {VC_BATCH_SCHEMA, VC_BATCH_V2_SCHEMA}:
        _validate_batch_candidate_binding(payload, str(phase), "VC batch")
    if schema_version == VC_BATCH_SCHEMA:
        validate_batch_evaluation_binding(payload, str(phase), "VC batch")
    if (
        phase not in VC_PHASES[1:]
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 1
        or payload.get("batch_id") != f"{str(phase).lower()}-{sequence:04d}"
    ):
        raise VCArtifactError("VC batch schema、阶段、序号或身份非法")
    _safe_id(payload.get("campaign_id"), "VC batch campaign_id")
    _sha256(payload.get("campaign_plan_sha256"), "VC batch plan SHA")
    predecessor = _checkpoint_reference(
        payload.get("predecessor_checkpoint"),
        "predecessor_checkpoint",
    )
    expected_predecessor = VC_PHASES[VC_PHASES.index(str(phase)) - 1]
    if predecessor["phase"] != expected_predecessor:
        raise VCArtifactError(
            f"VC batch {phase} 必须直接承接 {expected_predecessor} checkpoint"
        )
    compiled = _timestamp(payload.get("compiled_at_utc"), "VC batch compiled_at_utc")
    must_start = _timestamp(payload.get("must_start_by_utc"), "VC batch must_start_by_utc")
    deadline = _timestamp(
        payload.get("original_deadline_at_utc"),
        "VC batch original_deadline_at_utc",
    )
    compiled_at = datetime.fromisoformat(compiled.replace("Z", "+00:00"))
    start_by = datetime.fromisoformat(must_start.replace("Z", "+00:00"))
    original_deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if not compiled_at < start_by <= original_deadline:
        raise VCArtifactError("VC batch 启动时限未承接原始 deadline")
    for field in ("execute_item_ids", "reuse_item_ids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in values)
        ):
            raise VCArtifactError(f"VC batch {field} 非法")
    if set(payload["execute_item_ids"]) & set(payload["reuse_item_ids"]):
        raise VCArtifactError("VC batch execute/reuse 集合相交")
    actions = _actions(
        payload.get("actions"),
        execute_item_ids=payload["execute_item_ids"],
        allow_output_bindings=(schema_version == VC_BATCH_SCHEMA),
        phase=str(phase),
    )
    if payload["execute_item_ids"] and not actions:
        raise VCArtifactError("VC batch 有 execute 项却没有动作")
    if not payload["execute_item_ids"] and actions:
        raise VCArtifactError("VC batch no-op 不得包含动作")
    if campaign_plan is not None:
        plan = validate_campaign_plan(campaign_plan)
        if (
            payload["campaign_id"] != plan["campaign_id"]
            or payload["campaign_plan_sha256"] != plan["plan_sha256"]
            or payload["original_deadline_at_utc"] != plan["original_deadline_at_utc"]
        ):
            raise VCArtifactError("VC batch 未继承同一 Campaign 或原始 deadline")
    _self_digest(payload, "batch_sha256", "VC batch")
    return payload


# ---------------------------------------------------------------------------
# 改造 4：staging／COMMIT／abort／父启动失败 四种小型控制制品
# ---------------------------------------------------------------------------

_ROOT_CAUSE_ID_RE = re.compile(r"^rc1-[0-9a-f]{20}$")


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise VCArtifactError(f"{label} 必须是正整数")
    return value


def _batch_phase(value: Any, label: str) -> str:
    if value not in VC_PHASES[1:]:
        raise VCArtifactError(f"{label} 不是可派发的 VC 阶段")
    return str(value)


def build_staging_prepared_marker(
    *,
    campaign_id: str,
    sequence: int,
    phase: str,
    attempt: int,
    batch_sha256: str,
    manifest_sha256: str,
    owner_nonce: str,
    prepared_at_utc: str,
) -> dict[str, Any]:
    """staging attempt 的 PREPARED 标记：绑定两份产物与入口预分配的父 run nonce。"""

    payload = {
        "schema_version": STAGING_MARKER_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "sequence": _positive_int(sequence, "sequence"),
        "phase": _batch_phase(phase, "phase"),
        "attempt": _positive_int(attempt, "attempt"),
        "batch_sha256": _sha256(batch_sha256, "batch_sha256"),
        "manifest_sha256": _sha256(manifest_sha256, "manifest_sha256"),
        "owner_nonce": _sha256(owner_nonce, "owner_nonce"),
        "prepared_at_utc": _timestamp(prepared_at_utc, "prepared_at_utc"),
    }
    payload["marker_sha256"] = digest(payload)
    return validate_staging_prepared_marker(payload)


def validate_staging_prepared_marker(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "sequence",
        "phase",
        "attempt",
        "batch_sha256",
        "manifest_sha256",
        "owner_nonce",
        "prepared_at_utc",
        "marker_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("staging PREPARED 标记字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != STAGING_MARKER_SCHEMA:
        raise VCArtifactError("staging PREPARED 标记 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "PREPARED campaign_id")
    _positive_int(payload.get("sequence"), "PREPARED sequence")
    _batch_phase(payload.get("phase"), "PREPARED phase")
    _positive_int(payload.get("attempt"), "PREPARED attempt")
    _sha256(payload.get("batch_sha256"), "PREPARED batch_sha256")
    _sha256(payload.get("manifest_sha256"), "PREPARED manifest_sha256")
    _sha256(payload.get("owner_nonce"), "PREPARED owner_nonce")
    _timestamp(payload.get("prepared_at_utc"), "PREPARED prepared_at_utc")
    _self_digest(payload, "marker_sha256", "staging PREPARED 标记")
    return payload


def build_vc_commit(
    *,
    campaign_id: str,
    sequence: int,
    phase: str,
    staging_attempt: int,
    batch_sha256: str,
    manifest_sha256: str,
    parent_run_dir: str,
    owner_nonce: str,
    ledger_event_ids: Sequence[str],
    committed_at_utc: str,
) -> dict[str, Any]:
    """正式 COMMIT：唯一原子提交点，写入即永久占用序号。"""

    if not isinstance(parent_run_dir, str) or not PurePosixPath(parent_run_dir).is_absolute():
        raise VCArtifactError("COMMIT parent_run_dir 必须是绝对路径")
    events = [str(item) for item in ledger_event_ids]
    if any(not item for item in events) or len(events) != len(set(events)):
        raise VCArtifactError("COMMIT ledger_event_ids 非法")
    payload = {
        "schema_version": VC_COMMIT_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "sequence": _positive_int(sequence, "sequence"),
        "phase": _batch_phase(phase, "phase"),
        "staging_attempt": _positive_int(staging_attempt, "staging_attempt"),
        "batch_sha256": _sha256(batch_sha256, "batch_sha256"),
        "manifest_sha256": _sha256(manifest_sha256, "manifest_sha256"),
        "parent_run_dir": parent_run_dir,
        "owner_nonce": _sha256(owner_nonce, "owner_nonce"),
        "ledger_event_ids": events,
        "committed_at_utc": _timestamp(committed_at_utc, "committed_at_utc"),
    }
    payload["commit_sha256"] = digest(payload)
    return validate_vc_commit(payload)


def validate_vc_commit(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "sequence",
        "phase",
        "staging_attempt",
        "batch_sha256",
        "manifest_sha256",
        "parent_run_dir",
        "owner_nonce",
        "ledger_event_ids",
        "committed_at_utc",
        "commit_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("VC COMMIT 字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != VC_COMMIT_SCHEMA:
        raise VCArtifactError("VC COMMIT schema_version 非法")
    _safe_id(payload.get("campaign_id"), "COMMIT campaign_id")
    _positive_int(payload.get("sequence"), "COMMIT sequence")
    _batch_phase(payload.get("phase"), "COMMIT phase")
    _positive_int(payload.get("staging_attempt"), "COMMIT staging_attempt")
    _sha256(payload.get("batch_sha256"), "COMMIT batch_sha256")
    _sha256(payload.get("manifest_sha256"), "COMMIT manifest_sha256")
    parent_run_dir = payload.get("parent_run_dir")
    if not isinstance(parent_run_dir, str) or not PurePosixPath(parent_run_dir).is_absolute():
        raise VCArtifactError("COMMIT parent_run_dir 必须是绝对路径")
    _sha256(payload.get("owner_nonce"), "COMMIT owner_nonce")
    events = payload.get("ledger_event_ids")
    if (
        not isinstance(events, list)
        or any(not isinstance(item, str) or not item for item in events)
        or len(events) != len(set(events))
    ):
        raise VCArtifactError("COMMIT ledger_event_ids 非法")
    _timestamp(payload.get("committed_at_utc"), "COMMIT committed_at_utc")
    _self_digest(payload, "commit_sha256", "VC COMMIT")
    return payload


def build_staging_abort(
    *,
    campaign_id: str,
    campaign_plan_sha256: str,
    phase: str,
    sequence: int,
    staging_attempt: int,
    stage: str,
    failure_kind: str,
    error_type: str,
    root_cause_id: str,
    batch_sha256: str | None,
    manifest_sha256: str | None,
    parent_run_dir: str | None,
    parent_run_state: str | None,
    reconciliation_receipt: Mapping[str, Any] | None,
    recorded_at_utc: str,
) -> dict[str, Any]:
    """staging 中止事实：只记录发生了什么，不携带任何"可续跑"授权字段。"""

    payload = {
        "schema_version": STAGING_ABORT_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "campaign_plan_sha256": _sha256(campaign_plan_sha256, "campaign_plan_sha256"),
        "phase": _batch_phase(phase, "phase"),
        "sequence": _positive_int(sequence, "sequence"),
        "staging_attempt": _positive_int(staging_attempt, "staging_attempt"),
        "stage": stage,
        "failure_kind": failure_kind,
        "error_type": error_type,
        "root_cause_id": root_cause_id,
        "batch_sha256": batch_sha256,
        "manifest_sha256": manifest_sha256,
        "parent_run_dir": parent_run_dir,
        "parent_run_state": parent_run_state,
        "reconciliation_receipt": (
            dict(reconciliation_receipt) if reconciliation_receipt is not None else None
        ),
        "live_request_count": 0,
        "scanned_bytes": 0,
        "recorded_at_utc": _timestamp(recorded_at_utc, "recorded_at_utc"),
    }
    payload["receipt_sha256"] = digest(payload)
    return validate_staging_abort(payload)


def validate_staging_abort(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "campaign_plan_sha256",
        "phase",
        "sequence",
        "staging_attempt",
        "stage",
        "failure_kind",
        "error_type",
        "root_cause_id",
        "batch_sha256",
        "manifest_sha256",
        "parent_run_dir",
        "parent_run_state",
        "reconciliation_receipt",
        "live_request_count",
        "scanned_bytes",
        "recorded_at_utc",
        "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("staging-abort 收据字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != STAGING_ABORT_SCHEMA:
        raise VCArtifactError("staging-abort 收据 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "staging-abort campaign_id")
    _sha256(payload.get("campaign_plan_sha256"), "staging-abort campaign_plan_sha256")
    _batch_phase(payload.get("phase"), "staging-abort phase")
    _positive_int(payload.get("sequence"), "staging-abort sequence")
    _positive_int(payload.get("staging_attempt"), "staging-abort staging_attempt")
    if payload.get("stage") not in STAGING_ABORT_STAGES:
        raise VCArtifactError("staging-abort stage 非法")
    if payload.get("failure_kind") not in STAGING_ABORT_FAILURE_KINDS:
        raise VCArtifactError("staging-abort failure_kind 非法")
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or not error_type or len(error_type) > 128:
        raise VCArtifactError("staging-abort error_type 非法")
    root_cause_id = payload.get("root_cause_id")
    if not isinstance(root_cause_id, str) or not _ROOT_CAUSE_ID_RE.fullmatch(root_cause_id):
        raise VCArtifactError("staging-abort root_cause_id 非法")
    for field in ("batch_sha256", "manifest_sha256"):
        if payload.get(field) is not None:
            _sha256(payload.get(field), f"staging-abort {field}")
    for field in ("parent_run_dir", "parent_run_state"):
        item = payload.get(field)
        if item is not None and (not isinstance(item, str) or not item):
            raise VCArtifactError(f"staging-abort {field} 非法")
    if payload.get("parent_run_dir") is not None and not PurePosixPath(
        str(payload["parent_run_dir"])
    ).is_absolute():
        raise VCArtifactError("staging-abort parent_run_dir 必须是绝对路径")
    receipt = payload.get("reconciliation_receipt")
    if receipt is not None:
        _binding(receipt, "staging-abort reconciliation_receipt")
    if payload.get("live_request_count") != 0 or payload.get("scanned_bytes") != 0:
        raise VCArtifactError("staging-abort 必须是零请求零扫描事实")
    _timestamp(payload.get("recorded_at_utc"), "staging-abort recorded_at_utc")
    _self_digest(payload, "receipt_sha256", "staging-abort 收据")
    return payload


def build_parent_start_failure(
    *,
    campaign_id: str,
    phase: str,
    batch_sequence: int,
    batch_sha256: str,
    commit_sha256: str,
    owner_pid: int,
    owner_nonce: str,
    failure_kind: str,
    error_type: str,
    recorded_at_utc: str,
) -> dict[str, Any]:
    """父启动失败诊断：COMMIT 已写但 run 未取得执行权；零动作、零 reservation、零请求。"""

    payload = {
        "schema_version": PARENT_START_FAILURE_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "phase": _batch_phase(phase, "phase"),
        "batch_sequence": _positive_int(batch_sequence, "batch_sequence"),
        "batch_sha256": _sha256(batch_sha256, "batch_sha256"),
        "commit_sha256": _sha256(commit_sha256, "commit_sha256"),
        "owner_pid": _positive_int(owner_pid, "owner_pid"),
        "owner_nonce": _sha256(owner_nonce, "owner_nonce"),
        "action_started": False,
        "reservation_exists": False,
        "live_request_count": 0,
        "failure_kind": failure_kind,
        "error_type": error_type,
        "recorded_at_utc": _timestamp(recorded_at_utc, "recorded_at_utc"),
    }
    payload["diagnostic_sha256"] = digest(payload)
    return validate_parent_start_failure(payload)


def validate_parent_start_failure(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "phase",
        "batch_sequence",
        "batch_sha256",
        "commit_sha256",
        "owner_pid",
        "owner_nonce",
        "action_started",
        "reservation_exists",
        "live_request_count",
        "failure_kind",
        "error_type",
        "recorded_at_utc",
        "diagnostic_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("父启动失败诊断字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != PARENT_START_FAILURE_SCHEMA:
        raise VCArtifactError("父启动失败诊断 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "父启动失败 campaign_id")
    _batch_phase(payload.get("phase"), "父启动失败 phase")
    _positive_int(payload.get("batch_sequence"), "父启动失败 batch_sequence")
    _sha256(payload.get("batch_sha256"), "父启动失败 batch_sha256")
    _sha256(payload.get("commit_sha256"), "父启动失败 commit_sha256")
    _positive_int(payload.get("owner_pid"), "父启动失败 owner_pid")
    _sha256(payload.get("owner_nonce"), "父启动失败 owner_nonce")
    if (
        payload.get("action_started") is not False
        or payload.get("reservation_exists") is not False
        or payload.get("live_request_count") != 0
    ):
        raise VCArtifactError("父启动失败诊断必须证明零动作、零 reservation、零请求")
    if payload.get("failure_kind") not in PARENT_START_FAILURE_KINDS:
        raise VCArtifactError("父启动失败 failure_kind 非法")
    error_type = payload.get("error_type")
    if not isinstance(error_type, str) or not error_type or len(error_type) > 128:
        raise VCArtifactError("父启动失败 error_type 非法")
    _timestamp(payload.get("recorded_at_utc"), "父启动失败 recorded_at_utc")
    _self_digest(payload, "diagnostic_sha256", "父启动失败诊断")
    return payload


# ---------------------------------------------------------------------------
# 改造 2：候选级 revision 的四种控制制品（revision.json／COMMIT／seal.json／invalidation.json）
# ---------------------------------------------------------------------------


def _optional_sha256(value: Any, label: str) -> str | None:
    return None if value is None else _sha256(value, label)


def _optional_safe_id(value: Any, label: str) -> str | None:
    return None if value is None else _safe_id(value, label)


def _identity_snapshot(value: Any, label: str) -> dict[str, Any]:
    """旧候选身份快照：git_commit 与 source_tree_sha256 至少一项必须取得。"""

    fields = {"git_commit", "source_tree_sha256", "image_id", "build_receipt_sha256", "snapshot_sources"}
    build_fields = {"binary_sha256", "image_digest", "build_parameters_sha256", "build_parameters_input_sha256", "build_inputs"}
    if not isinstance(value, Mapping) or set(value) not in (fields, fields | build_fields):
        raise VCArtifactError(f"{label} 身份快照字段不闭合")
    commit = value.get("git_commit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"^[0-9a-f]{40}$", commit)):
        raise VCArtifactError(f"{label}.git_commit 非法")
    tree = _optional_sha256(value.get("source_tree_sha256"), f"{label}.source_tree_sha256")
    image = value.get("image_id")
    if image is not None and (not isinstance(image, str) or not image or len(image) > 256):
        raise VCArtifactError(f"{label}.image_id 非法")
    build = _optional_sha256(value.get("build_receipt_sha256"), f"{label}.build_receipt_sha256")
    sources = value.get("snapshot_sources")
    if (
        not isinstance(sources, list)
        or not sources
        or any(item not in IDENTITY_SNAPSHOT_SOURCES for item in sources)
        or len(sources) != len(set(sources))
    ):
        raise VCArtifactError(f"{label}.snapshot_sources 非法")
    if commit is None and tree is None:
        raise VCArtifactError(f"{label} 身份快照必须至少含 git_commit 或 source_tree_sha256")
    result = {
        "git_commit": commit,
        "source_tree_sha256": tree,
        "image_id": image,
        "build_receipt_sha256": build,
        "snapshot_sources": list(sources),
    }
    if build_fields.issubset(value):
        for field in ("binary_sha256", "build_parameters_sha256", "build_parameters_input_sha256"):
            _optional_sha256(value[field], f"{label}.{field}")
        if value["image_digest"] is not None and not IMAGE_ID_RE.fullmatch(str(value["image_digest"])):
            raise VCArtifactError(f"{label}.image_digest 非法")
        if value["build_inputs"] is not None:
            from . import codex_upgrade_candidate_build as candidate_build
            try:
                candidate_build.validate_implementation_inputs(value["build_inputs"])
            except candidate_build.CandidateBuildError as error:
                raise VCArtifactError(f"{label}.build_inputs 非法：{error}") from error
        result.update({field: value[field] for field in build_fields})
    return result


def build_candidate_revision(
    *,
    campaign_id: str,
    revision: int,
    candidate_id: str,
    opened_at_utc: str,
    previous_revision_sha256: str | None,
    vc3_checkpoint: Mapping[str, Any],
    vc3_stage_receipt: Mapping[str, Any],
    supersedes: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """revision.json：一次候选级 revision 的登记事实（write-once，COMMIT 前是 pending）。"""

    payload = {
        "schema_version": CANDIDATE_REVISION_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "revision": _positive_int(revision, "revision"),
        "candidate_id": _safe_id(candidate_id, "candidate_id"),
        "opened_at_utc": _timestamp(opened_at_utc, "opened_at_utc"),
        "previous_revision_sha256": _optional_sha256(previous_revision_sha256, "previous_revision_sha256"),
        "vc3_checkpoint": _checkpoint_reference(vc3_checkpoint, "vc3_checkpoint"),
        "vc3_stage_receipt": _binding(vc3_stage_receipt, "vc3_stage_receipt"),
        "supersedes": dict(supersedes) if supersedes is not None else None,
    }
    payload["record_sha256"] = digest(payload)
    return validate_candidate_revision(payload)


def validate_candidate_revision(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "revision",
        "candidate_id",
        "opened_at_utc",
        "previous_revision_sha256",
        "vc3_checkpoint",
        "vc3_stage_receipt",
        "supersedes",
        "record_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("候选 revision 记录字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_REVISION_SCHEMA:
        raise VCArtifactError("候选 revision 记录 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "revision campaign_id")
    revision = _positive_int(payload.get("revision"), "revision")
    _safe_id(payload.get("candidate_id"), "revision candidate_id")
    _timestamp(payload.get("opened_at_utc"), "revision opened_at_utc")
    previous = _optional_sha256(payload.get("previous_revision_sha256"), "previous_revision_sha256")
    checkpoint = _checkpoint_reference(payload.get("vc3_checkpoint"), "vc3_checkpoint")
    if checkpoint["phase"] != "VC-3":
        raise VCArtifactError("候选 revision 必须绑定 Campaign 级 VC-3 checkpoint")
    _binding(payload.get("vc3_stage_receipt"), "vc3_stage_receipt")
    supersedes = payload.get("supersedes")
    if revision == 1:
        if supersedes is not None or previous is not None:
            raise VCArtifactError("r1 不取代任何 revision")
    else:
        if not isinstance(supersedes, Mapping) or set(supersedes) != {
            "revision",
            "candidate_id",
            "invalidation_receipt",
            "candidate_invalidated_event_sha256",
        }:
            raise VCArtifactError("r≥2 必须登记被取代的 revision")
        if supersedes.get("revision") != revision - 1:
            raise VCArtifactError("被取代的 revision 必须是直接前序")
        _safe_id(supersedes.get("candidate_id"), "supersedes.candidate_id")
        if supersedes.get("candidate_id") == payload.get("candidate_id"):
            raise VCArtifactError("新 revision 的候选不得与被取代候选同名")
        _binding(supersedes.get("invalidation_receipt"), "supersedes.invalidation_receipt")
        _sha256(supersedes.get("candidate_invalidated_event_sha256"), "supersedes.candidate_invalidated_event_sha256")
        if previous is None:
            raise VCArtifactError("r≥2 必须绑定前一 revision 的 record_sha256")
    _self_digest(payload, "record_sha256", "候选 revision 记录")
    return payload


def build_candidate_revision_commit(
    *,
    campaign_id: str,
    revision: int,
    candidate_id: str,
    record_sha256: str,
    committed_at_utc: str,
) -> dict[str, Any]:
    """revision 目录 COMMIT：目录有 COMMIT = pending，账本 stage_revision 引用其摘要 = active。"""

    payload = {
        "schema_version": CANDIDATE_REVISION_COMMIT_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "revision": _positive_int(revision, "revision"),
        "candidate_id": _safe_id(candidate_id, "candidate_id"),
        "record_sha256": _sha256(record_sha256, "record_sha256"),
        "committed_at_utc": _timestamp(committed_at_utc, "committed_at_utc"),
    }
    payload["commit_sha256"] = digest(payload)
    return validate_candidate_revision_commit(payload)


def validate_candidate_revision_commit(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "revision",
        "candidate_id",
        "record_sha256",
        "committed_at_utc",
        "commit_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("候选 revision COMMIT 字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_REVISION_COMMIT_SCHEMA:
        raise VCArtifactError("候选 revision COMMIT schema_version 非法")
    _safe_id(payload.get("campaign_id"), "revision COMMIT campaign_id")
    _positive_int(payload.get("revision"), "revision COMMIT revision")
    _safe_id(payload.get("candidate_id"), "revision COMMIT candidate_id")
    _sha256(payload.get("record_sha256"), "revision COMMIT record_sha256")
    _timestamp(payload.get("committed_at_utc"), "revision COMMIT committed_at_utc")
    _self_digest(payload, "commit_sha256", "候选 revision COMMIT")
    return payload


def _revision_identity_diff(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, Any]:
    """只比较双方都已取得的实物身份；缺失旧字段不能被解释成身份变化。"""

    return {
        field: {"before": previous.get(field), "after": current.get(field),
                "changed": None if previous.get(field) is None or current.get(field) is None
                else previous[field] != current[field]}
        for field in ("git_commit", "source_tree_sha256", "image_id", "binary_sha256",
                      "image_digest", "build_parameters_sha256", "build_parameters_input_sha256")
    }


def _revision_changed_layers(diff: Mapping[str, Any]) -> list[str]:
    """有输入投影时不把候选改名或目录迁移算成真实参数变化；原始参数 diff 仍完整保留。"""

    parameter_key = ("build_parameters_input_sha256" if diff.get("build_parameters_input_sha256", {}).get("changed") is not None
                     else "build_parameters_sha256")
    return [layer for layer, fields in (("source", ("git_commit", "source_tree_sha256")),
            ("build", ("image_id", "binary_sha256", "image_digest", parameter_key)))
            if any(diff.get(field, {}).get("changed") is True for field in fields)]


def build_candidate_revision_seal(
    *,
    campaign_id: str,
    revision: int,
    candidate_id: str,
    candidate_commit: str | None,
    source_tree_sha256: str,
    image_id: str | None,
    build_receipt_sha256: str,
    vc3_stage_receipt_sha256: str,
    superseded: Mapping[str, Any] | None,
    sealed_at_utc: str,
    binary_sha256: str | None = None,
    image_digest: str | None = None,
    build_parameters_sha256: str | None = None,
    build_parameters_input_sha256: str | None = None,
) -> dict[str, Any]:
    """seal.json：revision-seal 的结论——新候选最终身份、VC-3 字节一致与同一性变化证明。"""

    superseded_payload: dict[str, Any] | None = None
    identity_change: dict[str, Any] | None = None
    if superseded is not None:
        superseded_payload = {
            "revision": _positive_int(superseded.get("revision"), "superseded.revision"),
            "candidate_id": _safe_id(superseded.get("candidate_id"), "superseded.candidate_id"),
            "git_commit": superseded.get("git_commit"),
            "source_tree_sha256": _optional_sha256(superseded.get("source_tree_sha256"), "superseded.source_tree_sha256"),
            "image_id": superseded.get("image_id"),
            "binary_sha256": superseded.get("binary_sha256"),
            "image_digest": superseded.get("image_digest"),
            "build_parameters_sha256": superseded.get("build_parameters_sha256"),
            "build_parameters_input_sha256": superseded.get("build_parameters_input_sha256"),
        }
        identity_change = {
            "git_commit_changed": (
                None
                if superseded_payload["git_commit"] is None or candidate_commit is None
                else superseded_payload["git_commit"] != candidate_commit
            ),
            "source_tree_changed": (
                None
                if superseded_payload["source_tree_sha256"] is None
                else superseded_payload["source_tree_sha256"] != source_tree_sha256
            ),
            "image_changed": (
                None
                if superseded_payload["image_id"] is None or image_id is None
                else superseded_payload["image_id"] != image_id
            ),
        }
    payload = {
        "schema_version": CANDIDATE_REVISION_SEAL_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "revision": _positive_int(revision, "revision"),
        "candidate_id": _safe_id(candidate_id, "candidate_id"),
        "candidate_commit": candidate_commit,
        "source_tree_sha256": _sha256(source_tree_sha256, "source_tree_sha256"),
        "image_id": image_id,
        "binary_sha256": binary_sha256,
        "image_digest": image_digest,
        "build_parameters_sha256": build_parameters_sha256,
        "build_parameters_input_sha256": build_parameters_input_sha256,
        "build_receipt_sha256": _sha256(build_receipt_sha256, "build_receipt_sha256"),
        "vc3_stage_receipt_sha256": _sha256(vc3_stage_receipt_sha256, "vc3_stage_receipt_sha256"),
        "superseded": superseded_payload,
        "identity_change": identity_change,
        "sealed_at_utc": _timestamp(sealed_at_utc, "sealed_at_utc"),
    }
    field_diff = _revision_identity_diff(
        {**payload, "git_commit": candidate_commit}, superseded_payload
    ) if superseded_payload is not None else {}
    if identity_change is not None:
        identity_change.update({
            "binary_sha256_changed": field_diff["binary_sha256"]["changed"],
            "image_digest_changed": field_diff["image_digest"]["changed"],
            "build_parameters_changed": field_diff["build_parameters_sha256"]["changed"],
            "build_parameter_inputs_changed": field_diff["build_parameters_input_sha256"]["changed"],
        })
    payload["field_diff"] = field_diff
    payload["changed_layers"] = _revision_changed_layers(field_diff)
    payload["seal_sha256"] = digest(payload)
    return validate_candidate_revision_seal(payload)


def validate_candidate_revision_seal(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "revision",
        "candidate_id",
        "candidate_commit",
        "source_tree_sha256",
        "image_id",
        "build_receipt_sha256",
        "vc3_stage_receipt_sha256",
        "superseded",
        "identity_change",
        "sealed_at_utc",
        "seal_sha256",
    }
    extended = {"binary_sha256", "image_digest", "build_parameters_sha256", "build_parameters_input_sha256", "field_diff", "changed_layers"}
    if not isinstance(value, Mapping) or set(value) not in (required, required | extended):
        raise VCArtifactError("候选 revision seal 字段不闭合")
    payload = dict(value)
    is_extended = extended.issubset(payload)
    if payload.get("schema_version") != CANDIDATE_REVISION_SEAL_SCHEMA:
        raise VCArtifactError("候选 revision seal schema_version 非法")
    _safe_id(payload.get("campaign_id"), "seal campaign_id")
    revision = _positive_int(payload.get("revision"), "seal revision")
    _safe_id(payload.get("candidate_id"), "seal candidate_id")
    commit = payload.get("candidate_commit")
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"^[0-9a-f]{40}$", commit)):
        raise VCArtifactError("seal candidate_commit 非法")
    _sha256(payload.get("source_tree_sha256"), "seal source_tree_sha256")
    image = payload.get("image_id")
    if image is not None and (not isinstance(image, str) or not image):
        raise VCArtifactError("seal image_id 非法")
    if is_extended:
        for field in ("binary_sha256", "build_parameters_sha256", "build_parameters_input_sha256"):
            _optional_sha256(payload[field], f"seal {field}")
        if payload["image_digest"] is not None and not IMAGE_ID_RE.fullmatch(str(payload["image_digest"])):
            raise VCArtifactError("seal image_digest 非法")
    _sha256(payload.get("build_receipt_sha256"), "seal build_receipt_sha256")
    _sha256(payload.get("vc3_stage_receipt_sha256"), "seal vc3_stage_receipt_sha256")
    superseded = payload.get("superseded")
    change = payload.get("identity_change")
    if revision == 1:
        if superseded is not None or change is not None:
            raise VCArtifactError("r1 seal 不得携带被取代候选")
    else:
        old_fields = {
            "revision",
            "candidate_id",
            "git_commit",
            "source_tree_sha256",
            "image_id",
        }
        if is_extended:
            old_fields |= {"binary_sha256", "image_digest", "build_parameters_sha256", "build_parameters_input_sha256"}
        if not isinstance(superseded, Mapping) or set(superseded) != old_fields:
            raise VCArtifactError("r≥2 seal 必须登记被取代候选身份")
        if superseded.get("revision") != revision - 1:
            raise VCArtifactError("seal 被取代的 revision 必须是直接前序")
        flag_fields = {
            "git_commit_changed",
            "source_tree_changed",
            "image_changed",
        }
        if is_extended:
            flag_fields |= {"binary_sha256_changed", "image_digest_changed", "build_parameters_changed", "build_parameter_inputs_changed"}
        if not isinstance(change, Mapping) or set(change) != flag_fields:
            raise VCArtifactError("r≥2 seal 必须登记同一性变化证明")
        diff = _revision_identity_diff({**payload, "git_commit": commit}, superseded)
        flags = dict(zip(("git_commit_changed", "source_tree_changed", "image_changed",
                         "binary_sha256_changed", "image_digest_changed", "build_parameters_changed", "build_parameter_inputs_changed"),
                        (row["changed"] for row in diff.values())))
        if any(change[key] is not flags[key] for key in flag_fields):
            raise VCArtifactError("seal 变化标志与逐字段实物身份不一致")
        comparable = [change[key] for key in flag_fields if change[key] is not None]
        if not is_extended:
            comparable = [change[key] for key in ("git_commit_changed", "source_tree_changed") if change[key] is not None]
        if (is_extended and not _revision_changed_layers(diff)) or (not is_extended and (not comparable or not any(comparable))):
            raise VCArtifactError("被取代候选的源码层／构建层全部相同或不可比：不是新候选")
    if is_extended:
        diff = _revision_identity_diff({**payload, "git_commit": commit}, superseded) if superseded is not None else {}
        layers = _revision_changed_layers(diff)
        if payload["field_diff"] != diff or payload["changed_layers"] != layers:
            raise VCArtifactError("seal 逐字段 diff 或变化层与实物身份不一致")
    _timestamp(payload.get("sealed_at_utc"), "seal sealed_at_utc")
    _self_digest(payload, "seal_sha256", "候选 revision seal")
    return payload


def candidate_invalidation_review_sha256(draft: Mapping[str, Any]) -> str:
    """preview 输出的可复核摘要：只散列稳定字段，不含审核时间与自摘要。"""

    stable = {
        key: draft[key]
        for key in (
            "campaign_id",
            "campaign_manifest_sha256",
            "candidate_id",
            "revision",
            "reviewer",
            "conclusion",
            "evidence_refs",
            "project_ledger_head_sha256",
            "project_ledger_head_sequence",
            "root_cause_id",
            "identity_snapshot",
        )
    }
    return digest(stable)


def build_candidate_invalidation_diagnosis(
    *,
    campaign_id: str,
    campaign_manifest_sha256: str,
    candidate_id: str,
    revision: int,
    reviewer: str,
    reviewed_at_utc: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    project_ledger_head_sha256: str,
    project_ledger_head_sequence: int,
    root_cause_id: str,
    identity_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """工具签发的候选作废诊断收据：绑定当时总账 head 与旧候选身份快照。"""

    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 128:
        raise VCArtifactError("reviewer 非法")
    if isinstance(project_ledger_head_sequence, bool) or not isinstance(project_ledger_head_sequence, int) or project_ledger_head_sequence < 0:
        raise VCArtifactError("project_ledger_head_sequence 非法")
    refs = [_binding(item, "evidence_refs") for item in evidence_refs]
    if [item["path"] for item in refs] != sorted(item["path"] for item in refs) or len({item["path"] for item in refs}) != len(refs):
        raise VCArtifactError("evidence_refs 必须按 path 唯一排序")
    if not isinstance(root_cause_id, str) or not _ROOT_CAUSE_ID_RE.fullmatch(root_cause_id):
        raise VCArtifactError("root_cause_id 非法")
    payload = {
        "schema_version": CANDIDATE_INVALIDATION_DIAGNOSIS_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "campaign_manifest_sha256": _sha256(campaign_manifest_sha256, "campaign_manifest_sha256"),
        "candidate_id": _safe_id(candidate_id, "candidate_id"),
        "revision": _positive_int(revision, "revision"),
        "reviewer": reviewer.strip(),
        "reviewed_at_utc": _timestamp(reviewed_at_utc, "reviewed_at_utc"),
        "conclusion": CANDIDATE_INVALIDATION_CONCLUSION,
        "evidence_refs": refs,
        "project_ledger_head_sha256": _sha256(project_ledger_head_sha256, "project_ledger_head_sha256"),
        "project_ledger_head_sequence": project_ledger_head_sequence,
        "root_cause_id": root_cause_id,
        "identity_snapshot": _identity_snapshot(identity_snapshot, "诊断"),
    }
    payload["review_sha256"] = candidate_invalidation_review_sha256(payload)
    payload["receipt_sha256"] = digest(payload)
    return validate_candidate_invalidation_diagnosis(payload)


def validate_candidate_invalidation_diagnosis(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "campaign_manifest_sha256",
        "candidate_id",
        "revision",
        "reviewer",
        "reviewed_at_utc",
        "conclusion",
        "evidence_refs",
        "project_ledger_head_sha256",
        "project_ledger_head_sequence",
        "root_cause_id",
        "identity_snapshot",
        "review_sha256",
        "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("候选作废诊断收据字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_INVALIDATION_DIAGNOSIS_SCHEMA:
        raise VCArtifactError("候选作废诊断收据 schema_version 非法")
    if payload.get("conclusion") != CANDIDATE_INVALIDATION_CONCLUSION:
        raise VCArtifactError("候选作废诊断结论非法")
    _safe_id(payload.get("campaign_id"), "诊断 campaign_id")
    _sha256(payload.get("campaign_manifest_sha256"), "诊断 campaign_manifest_sha256")
    _safe_id(payload.get("candidate_id"), "诊断 candidate_id")
    _positive_int(payload.get("revision"), "诊断 revision")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise VCArtifactError("诊断 reviewer 非法")
    _timestamp(payload.get("reviewed_at_utc"), "诊断 reviewed_at_utc")
    refs = payload.get("evidence_refs")
    if not isinstance(refs, list):
        raise VCArtifactError("诊断 evidence_refs 必须是数组")
    for item in refs:
        _binding(item, "诊断 evidence_refs")
    _sha256(payload.get("project_ledger_head_sha256"), "诊断 project_ledger_head_sha256")
    sequence = payload.get("project_ledger_head_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise VCArtifactError("诊断 project_ledger_head_sequence 非法")
    root_cause_id = payload.get("root_cause_id")
    if not isinstance(root_cause_id, str) or not _ROOT_CAUSE_ID_RE.fullmatch(root_cause_id):
        raise VCArtifactError("诊断 root_cause_id 非法")
    _identity_snapshot(payload.get("identity_snapshot"), "诊断")
    if payload.get("review_sha256") != candidate_invalidation_review_sha256(payload):
        raise VCArtifactError("诊断 review_sha256 与稳定字段不一致")
    _self_digest(payload, "receipt_sha256", "候选作废诊断收据")
    return payload


def build_candidate_invalidation(
    *,
    campaign_id: str,
    candidate_id: str,
    revision: int,
    diagnosis: Mapping[str, Any],
    recorded_at_utc: str,
) -> dict[str, Any]:
    """candidates/<id>/invalidation.json：绑定诊断收据与旧候选身份快照（write-once）。"""

    receipt = validate_candidate_invalidation_diagnosis(diagnosis)
    if receipt["campaign_id"] != campaign_id or receipt["candidate_id"] != candidate_id or receipt["revision"] != revision:
        raise VCArtifactError("诊断收据与作废对象身份不一致")
    payload = {
        "schema_version": CANDIDATE_INVALIDATION_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "candidate_id": _safe_id(candidate_id, "candidate_id"),
        "revision": _positive_int(revision, "revision"),
        "diagnosis": receipt,
        "identity_snapshot": dict(receipt["identity_snapshot"]),
        "root_cause_id": receipt["root_cause_id"],
        "recorded_at_utc": _timestamp(recorded_at_utc, "recorded_at_utc"),
    }
    payload["receipt_sha256"] = digest(payload)
    return validate_candidate_invalidation(payload)


def validate_candidate_invalidation(value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "campaign_id",
        "candidate_id",
        "revision",
        "diagnosis",
        "identity_snapshot",
        "root_cause_id",
        "recorded_at_utc",
        "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("候选作废记录字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_INVALIDATION_SCHEMA:
        raise VCArtifactError("候选作废记录 schema_version 非法")
    diagnosis = validate_candidate_invalidation_diagnosis(payload.get("diagnosis"))
    if (
        diagnosis["campaign_id"] != payload.get("campaign_id")
        or diagnosis["candidate_id"] != payload.get("candidate_id")
        or diagnosis["revision"] != payload.get("revision")
        or diagnosis["root_cause_id"] != payload.get("root_cause_id")
        or dict(diagnosis["identity_snapshot"]) != payload.get("identity_snapshot")
    ):
        raise VCArtifactError("候选作废记录与诊断收据不一致")
    _timestamp(payload.get("recorded_at_utc"), "作废记录 recorded_at_utc")
    _self_digest(payload, "receipt_sha256", "候选作废记录")
    return payload


# ---------------------------------------------------------------------------
# 改造 5（评估失败局部恢复）：评估基线 b<K> 五件套、checkpoint／run 索引、动作输出绑定
# ---------------------------------------------------------------------------

RECOVERY_REVISION_RE = re.compile(r"^ar[1-9][0-9]*$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
ROOT_CAUSE_ID_RE = re.compile(r"^rc1-[0-9a-f]{20}$")
EVALUATION_SIDES = ("candidate", "official")
EVALUATION_RULE_STATUSES = ("pass", "fail", "pending")
VALIDATION_MODES = ("dual_wire", "candidate_profile")


def _non_negative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VCArtifactError(f"{label} 必须是非负整数")
    return value


def _sorted_safe_ids(values: Any, label: str) -> list[str]:
    if (
        not isinstance(values, list)
        or values != sorted(set(values))
        or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in values)
    ):
        raise VCArtifactError(f"{label} 必须是排序且无重复的安全标识数组")
    return list(values)


def _optional_binding(value: Any, label: str) -> dict[str, Any] | None:
    return None if value is None else _binding(value, label)


def _recovery_revision(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RECOVERY_REVISION_RE.fullmatch(value):
        raise VCArtifactError(f"{label} 不是 ar<k> 形式的恢复段编号")
    return value


def _baseline_identity(payload: Mapping[str, Any], label: str) -> None:
    """评估基线制品共用的候选／基线身份四字段。"""

    _safe_id(payload.get("campaign_id"), f"{label} campaign_id")
    _safe_id(payload.get("candidate_id"), f"{label} candidate_id")
    _positive_int(payload.get("candidate_revision"), f"{label} candidate_revision")
    _positive_int(payload.get("evaluation_baseline"), f"{label} evaluation_baseline")


def validate_stage_source(value: Any, stage: str, label: str) -> dict[str, Any]:
    """COMMIT.stage_sources 的 tagged union：reused 绑定历史 path＋sha256，local 只冻结规范写目标。"""

    if stage not in EVALUATION_STAGES:
        raise VCArtifactError(f"{label} 阶段 {stage} 不在评估阶段闭集内")
    if not isinstance(value, Mapping):
        raise VCArtifactError(f"{label}.{stage} 必须是对象")
    source = value.get("source")
    if source == "reused":
        if set(value) != {"source", "baseline", "path", "sha256"}:
            raise VCArtifactError(f"{label}.{stage} reused 字段不闭合")
        if stage in {"assertions", "accept"}:
            raise VCArtifactError(f"{label}.{stage} 评估基线一定重算断言与 accept，不得 reused")
        return {
            "source": "reused",
            "baseline": _non_negative_int(value.get("baseline"), f"{label}.{stage}.baseline"),
            "path": _relative_path(value.get("path"), f"{label}.{stage}.path"),
            "sha256": _sha256(value.get("sha256"), f"{label}.{stage}.sha256"),
        }
    if source == "local":
        if set(value) != {"source", "target"}:
            raise VCArtifactError(f"{label}.{stage} local 字段不闭合")
        return {
            "source": "local",
            "target": _relative_path(value.get("target"), f"{label}.{stage}.target"),
        }
    raise VCArtifactError(f"{label}.{stage}.source 只能是 reused 或 local")


def validate_stage_sources(value: Any, *, kind: str, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != set(EVALUATION_STAGES):
        raise VCArtifactError(f"{label} stage_sources 必须覆盖全部四个评估阶段")
    normalized = {stage: validate_stage_source(value.get(stage), stage, label) for stage in EVALUATION_STAGES}
    if kind == "evaluator-only":
        if normalized["capture-candidate"]["source"] != "reused":
            raise VCArtifactError(f"{label} evaluator-only 基线的 capture-candidate 必须 reused")
    elif kind == "attempt-recovery":
        if normalized["capture-candidate"]["source"] != "local" or normalized["compare"]["source"] != "local":
            raise VCArtifactError(f"{label} attempt-recovery 基线的 capture-candidate 与 compare 必须 local")
    else:
        raise VCArtifactError(f"{label} kind 非法")
    return normalized


def validate_evaluation_epoch_binding(value: Any, label: str) -> dict[str, Any] | None:
    """recovery 采用的 evaluation-epoch 绑定：Campaign 相对路径、文件摘要、链内序号、目标 evidence 摘要。"""

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256", "index", "to_evidence_semantics_sha256"}:
        raise VCArtifactError(f"{label} 字段不闭合")
    path = _relative_path(value.get("path"), f"{label} path")
    if not re.fullmatch(r"^.+/evaluation-epoch-\d{2}\.json$", path):
        raise VCArtifactError(f"{label} path 必须指向 attempt 目录内的 evaluation-epoch-NN.json")
    _sha256(value.get("sha256"), f"{label} sha256")
    index = value.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1 or index > 99:
        raise VCArtifactError(f"{label} index 必须是 1～99 的整数")
    if int(path[-7:-5]) != index:
        raise VCArtifactError(f"{label} index 与 path 序号不一致")
    _sha256(value.get("to_evidence_semantics_sha256"), f"{label} to_evidence_semantics_sha256")
    return {
        "path": path,
        "sha256": str(value["sha256"]),
        "index": index,
        "to_evidence_semantics_sha256": str(value["to_evidence_semantics_sha256"]),
    }


def build_evaluation_recovery(
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    kind: str,
    diagnosis: Mapping[str, Any],
    failure_source: str,
    reuse_authority: str,
    root_cause_class: str,
    root_cause_id: str,
    failed_step: str,
    previous_baseline: int,
    previous_baseline_commit_sha256: str | None,
    execute_rules: Sequence[str],
    reuse_rules: Sequence[str],
    execute_jobs: Sequence[str],
    reuse_jobs: Sequence[str],
    attempt_id: str | None,
    recovery_revision: str | None,
    fix_commit: str | None,
    deployment_receipt: Mapping[str, Any] | None,
    evaluation_epoch: Mapping[str, Any] | None,
    failed_evaluator_digests: Mapping[str, Any],
    current_evaluator_digests: Mapping[str, Any],
    reviewer: str,
    approved_at_utc: str,
) -> dict[str, Any]:
    """revisions/b<K>/recovery.json：apply 冻结的恢复合同（write-once）。

    ``evaluation_epoch``：evaluator-defect 下候选 attempt evaluation-epoch 链末的绑定
    （path／sha256／index／to_evidence_semantics_sha256），evidence 未变化（链为空）时为 None。
    """

    payload = {
        "schema_version": EVALUATION_BASELINE_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "kind": kind,
        "diagnosis": dict(diagnosis),
        "failure_source": failure_source,
        "reuse_authority": reuse_authority,
        "root_cause_class": root_cause_class,
        "root_cause_id": root_cause_id,
        "failed_step": failed_step,
        "previous_baseline": previous_baseline,
        "previous_baseline_commit_sha256": previous_baseline_commit_sha256,
        "execute_rules": sorted(set(execute_rules)),
        "reuse_rules": sorted(set(reuse_rules)),
        "execute_jobs": sorted(set(execute_jobs)),
        "reuse_jobs": sorted(set(reuse_jobs)),
        "attempt_id": attempt_id,
        "recovery_revision": recovery_revision,
        "fix_commit": fix_commit,
        "deployment_receipt": dict(deployment_receipt) if deployment_receipt is not None else None,
        "evaluation_epoch": dict(evaluation_epoch) if evaluation_epoch is not None else None,
        "failed_evaluator_digests": dict(failed_evaluator_digests),
        "current_evaluator_digests": dict(current_evaluator_digests),
        "reviewer": reviewer,
        "approved_at_utc": approved_at_utc,
    }
    payload["recovery_sha256"] = digest(payload)
    return validate_evaluation_recovery(payload)


def validate_evaluation_recovery(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline", "kind",
        "diagnosis", "failure_source", "reuse_authority", "root_cause_class", "root_cause_id", "failed_step",
        "previous_baseline", "previous_baseline_commit_sha256", "execute_rules", "reuse_rules", "execute_jobs",
        "reuse_jobs", "attempt_id", "recovery_revision", "fix_commit", "deployment_receipt", "evaluation_epoch",
        "failed_evaluator_digests", "current_evaluator_digests", "reviewer", "approved_at_utc", "recovery_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估基线 recovery 字段不闭合")
    payload = dict(value)
    label = "评估基线 recovery"
    if payload.get("schema_version") != EVALUATION_BASELINE_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _baseline_identity(payload, label)
    kind = payload.get("kind")
    if kind not in EVALUATION_BASELINE_KINDS:
        raise VCArtifactError(f"{label} kind 非法")
    _binding(payload.get("diagnosis"), f"{label} diagnosis")
    if payload.get("failure_source") not in FAILURE_SOURCES:
        raise VCArtifactError(f"{label} failure_source 非法")
    if payload.get("reuse_authority") not in REUSE_AUTHORITIES:
        raise VCArtifactError(f"{label} reuse_authority 非法")
    root_cause_class = payload.get("root_cause_class")
    if root_cause_class not in {"evaluator-defect", "transient-environment"}:
        raise VCArtifactError(f"{label} root_cause_class 只允许 evaluator-defect／transient-environment")
    if (root_cause_class == "evaluator-defect") != (kind == "evaluator-only"):
        raise VCArtifactError(f"{label} root_cause_class 与 kind 不对应")
    if not isinstance(payload.get("root_cause_id"), str) or not ROOT_CAUSE_ID_RE.fullmatch(payload["root_cause_id"]):
        raise VCArtifactError(f"{label} root_cause_id 非法")
    failed_step = payload.get("failed_step")
    if not isinstance(failed_step, str) or not failed_step or len(failed_step) > 128:
        raise VCArtifactError(f"{label} failed_step 非法")
    previous = _non_negative_int(payload.get("previous_baseline"), f"{label} previous_baseline")
    if previous >= payload["evaluation_baseline"]:
        raise VCArtifactError(f"{label} previous_baseline 必须小于本基线编号")
    previous_commit = _optional_sha256(
        payload.get("previous_baseline_commit_sha256"), f"{label} previous_baseline_commit_sha256"
    )
    if (previous == 0) != (previous_commit is None):
        raise VCArtifactError(f"{label} 只有 b0 前序没有 COMMIT 摘要")
    execute_rules = _rule_ids(payload.get("execute_rules"), f"{label} execute_rules")
    reuse_rules = _rule_ids(payload.get("reuse_rules"), f"{label} reuse_rules")
    if set(execute_rules) & set(reuse_rules):
        raise VCArtifactError(f"{label} execute_rules 与 reuse_rules 相交")
    execute_jobs = _sorted_safe_ids(payload.get("execute_jobs"), f"{label} execute_jobs")
    reuse_jobs = _sorted_safe_ids(payload.get("reuse_jobs"), f"{label} reuse_jobs")
    if set(execute_jobs) & set(reuse_jobs):
        raise VCArtifactError(f"{label} execute_jobs 与 reuse_jobs 相交")
    attempt_id = _optional_safe_id(payload.get("attempt_id"), f"{label} attempt_id")
    recovery_revision = payload.get("recovery_revision")
    if kind == "attempt-recovery":
        if attempt_id is None or not execute_jobs:
            raise VCArtifactError(f"{label} attempt-recovery 必须绑定原 attempt 与非空 execute_jobs")
        _recovery_revision(recovery_revision, f"{label} recovery_revision")
        if payload.get("failure_source") != "assertion-failed":
            raise VCArtifactError(f"{label} attempt-recovery 只能由断言失败触发")
        if payload.get("fix_commit") is not None or payload.get("deployment_receipt") is not None:
            raise VCArtifactError(f"{label} attempt-recovery 不绑定修复提交或部署收据")
    else:
        if attempt_id is not None or recovery_revision is not None or execute_jobs:
            raise VCArtifactError(f"{label} evaluator-only 不得绑定 attempt、恢复段或重采 Job")
        fix_commit = payload.get("fix_commit")
        if not isinstance(fix_commit, str) or not GIT_COMMIT_RE.fullmatch(fix_commit):
            raise VCArtifactError(f"{label} evaluator-defect 必须绑定完整修复提交")
        if payload.get("deployment_receipt") is None:
            raise VCArtifactError(f"{label} evaluator-defect 必须绑定部署收据")
    _optional_binding(payload.get("deployment_receipt"), f"{label} deployment_receipt")
    validate_evaluation_epoch_binding(payload.get("evaluation_epoch"), f"{label} evaluation_epoch")
    if kind == "attempt-recovery" and payload.get("evaluation_epoch") is not None:
        raise VCArtifactError(f"{label} attempt-recovery 不绑定 evaluation-epoch")
    validate_evaluator_digests(payload.get("failed_evaluator_digests"), f"{label} failed")
    validate_evaluator_digests(payload.get("current_evaluator_digests"), f"{label} current")
    if payload.get("reuse_authority") == "none" and reuse_rules:
        raise VCArtifactError(f"{label} reuse_authority=none 时不得声明任何复用规则")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer or len(reviewer) > 128:
        raise VCArtifactError(f"{label} reviewer 非法")
    _timestamp(payload.get("approved_at_utc"), f"{label} approved_at_utc")
    _self_digest(payload, "recovery_sha256", label)
    return payload


def build_evaluation_baseline_prepared(
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    recovery_sha256: str,
    project_ledger_head_sequence: int,
    project_ledger_head_sha256: str,
    prepared_at_utc: str,
) -> dict[str, Any]:
    """PREPARED：绑定 recovery 自摘要与裁定前的总账 head 快照；此时 b<K> 不是当前基线。"""

    payload = {
        "schema_version": EVALUATION_BASELINE_PREPARED_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "recovery_sha256": recovery_sha256,
        "project_ledger_head_sequence": project_ledger_head_sequence,
        "project_ledger_head_sha256": project_ledger_head_sha256,
        "prepared_at_utc": prepared_at_utc,
    }
    payload["marker_sha256"] = digest(payload)
    return validate_evaluation_baseline_prepared(payload)


def validate_evaluation_baseline_prepared(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline",
        "recovery_sha256", "project_ledger_head_sequence", "project_ledger_head_sha256", "prepared_at_utc",
        "marker_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估基线 PREPARED 字段不闭合")
    payload = dict(value)
    label = "评估基线 PREPARED"
    if payload.get("schema_version") != EVALUATION_BASELINE_PREPARED_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _baseline_identity(payload, label)
    _sha256(payload.get("recovery_sha256"), f"{label} recovery_sha256")
    _non_negative_int(payload.get("project_ledger_head_sequence"), f"{label} project_ledger_head_sequence")
    _sha256(payload.get("project_ledger_head_sha256"), f"{label} project_ledger_head_sha256")
    _timestamp(payload.get("prepared_at_utc"), f"{label} prepared_at_utc")
    _self_digest(payload, "marker_sha256", label)
    return payload


def build_evaluation_baseline_authorization(
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    recovery_sha256: str,
    ledger_operation_id: str,
    ledger_event_sha256: str,
    project_ledger_head_sequence: int,
    project_ledger_head_sha256: str,
    root_cause_id: str,
    root_cause_count: int,
    authorized_at_utc: str,
) -> dict[str, Any]:
    """AUTHORIZATION：总账二次判定通过后签发，绑定根因事件摘要、head 与判定。"""

    payload = {
        "schema_version": EVALUATION_BASELINE_AUTHORIZATION_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "recovery_sha256": recovery_sha256,
        "ledger_operation_id": ledger_operation_id,
        "ledger_event_sha256": ledger_event_sha256,
        "project_ledger_head_sequence": project_ledger_head_sequence,
        "project_ledger_head_sha256": project_ledger_head_sha256,
        "decision": "recoverable",
        "root_cause_id": root_cause_id,
        "root_cause_count": root_cause_count,
        "authorized_at_utc": authorized_at_utc,
    }
    payload["authorization_sha256"] = digest(payload)
    return validate_evaluation_baseline_authorization(payload)


def validate_evaluation_baseline_authorization(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline",
        "recovery_sha256", "ledger_operation_id", "ledger_event_sha256", "project_ledger_head_sequence",
        "project_ledger_head_sha256", "decision", "root_cause_id", "root_cause_count", "authorized_at_utc",
        "authorization_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估基线 AUTHORIZATION 字段不闭合")
    payload = dict(value)
    label = "评估基线 AUTHORIZATION"
    if payload.get("schema_version") != EVALUATION_BASELINE_AUTHORIZATION_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _baseline_identity(payload, label)
    _sha256(payload.get("recovery_sha256"), f"{label} recovery_sha256")
    operation = payload.get("ledger_operation_id")
    if not isinstance(operation, str) or not operation or len(operation) > 256:
        raise VCArtifactError(f"{label} ledger_operation_id 非法")
    _sha256(payload.get("ledger_event_sha256"), f"{label} ledger_event_sha256")
    _non_negative_int(payload.get("project_ledger_head_sequence"), f"{label} project_ledger_head_sequence")
    _sha256(payload.get("project_ledger_head_sha256"), f"{label} project_ledger_head_sha256")
    if payload.get("decision") != "recoverable":
        raise VCArtifactError(f"{label} 只在二次判定可恢复时签发")
    if not isinstance(payload.get("root_cause_id"), str) or not ROOT_CAUSE_ID_RE.fullmatch(payload["root_cause_id"]):
        raise VCArtifactError(f"{label} root_cause_id 非法")
    _non_negative_int(payload.get("root_cause_count"), f"{label} root_cause_count")
    _timestamp(payload.get("authorized_at_utc"), f"{label} authorized_at_utc")
    _self_digest(payload, "authorization_sha256", label)
    return payload


def build_evaluation_baseline_commit(
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    kind: str,
    recovery_sha256: str,
    authorization_sha256: str,
    stage_sources: Mapping[str, Any],
    committed_at_utc: str,
) -> dict[str, Any]:
    """COMMIT：绑定 recovery 与 AUTHORIZATION，并冻结逐阶段 stage_sources。"""

    payload = {
        "schema_version": EVALUATION_BASELINE_COMMIT_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "kind": kind,
        "recovery_sha256": recovery_sha256,
        "authorization_sha256": authorization_sha256,
        "stage_sources": {stage: dict(stage_sources[stage]) for stage in EVALUATION_STAGES},
        "committed_at_utc": committed_at_utc,
    }
    payload["commit_sha256"] = digest(payload)
    return validate_evaluation_baseline_commit(payload)


def validate_evaluation_baseline_commit(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline", "kind",
        "recovery_sha256", "authorization_sha256", "stage_sources", "committed_at_utc", "commit_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估基线 COMMIT 字段不闭合")
    payload = dict(value)
    label = "评估基线 COMMIT"
    if payload.get("schema_version") != EVALUATION_BASELINE_COMMIT_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _baseline_identity(payload, label)
    if payload.get("kind") not in EVALUATION_BASELINE_KINDS:
        raise VCArtifactError(f"{label} kind 非法")
    _sha256(payload.get("recovery_sha256"), f"{label} recovery_sha256")
    _sha256(payload.get("authorization_sha256"), f"{label} authorization_sha256")
    sources = validate_stage_sources(payload.get("stage_sources"), kind=str(payload["kind"]), label=label)
    for stage, source in sources.items():
        if source["source"] == "reused" and source["baseline"] >= payload["evaluation_baseline"]:
            raise VCArtifactError(f"{label}.{stage} reused 只能指向更小编号的基线")
    _timestamp(payload.get("committed_at_utc"), f"{label} committed_at_utc")
    _self_digest(payload, "commit_sha256", label)
    return payload


def build_evaluation_baseline_abandon(
    *,
    campaign_id: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    recovery_sha256: str,
    reason: str,
    abandoned_at_utc: str,
) -> dict[str, Any]:
    """ABANDON：未 COMMIT 的 PREPARED 基线显式作废，编号不复用。"""

    payload = {
        "schema_version": EVALUATION_BASELINE_ABANDON_SCHEMA,
        "campaign_id": campaign_id,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "recovery_sha256": recovery_sha256,
        "reason": reason,
        "abandoned_at_utc": abandoned_at_utc,
    }
    payload["abandon_sha256"] = digest(payload)
    return validate_evaluation_baseline_abandon(payload)


def validate_evaluation_baseline_abandon(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline",
        "recovery_sha256", "reason", "abandoned_at_utc", "abandon_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估基线 ABANDON 字段不闭合")
    payload = dict(value)
    label = "评估基线 ABANDON"
    if payload.get("schema_version") != EVALUATION_BASELINE_ABANDON_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _baseline_identity(payload, label)
    _sha256(payload.get("recovery_sha256"), f"{label} recovery_sha256")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > 512:
        raise VCArtifactError(f"{label} reason 非法")
    _timestamp(payload.get("abandoned_at_utc"), f"{label} abandoned_at_utc")
    _self_digest(payload, "abandon_sha256", label)
    return payload


def _reused_from(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"baseline", "checkpoint_sha256", "document_sha256"}:
        raise VCArtifactError(f"{label} reused_from 字段不闭合")
    return {
        "baseline": _non_negative_int(value.get("baseline"), f"{label} reused_from.baseline"),
        "checkpoint_sha256": _sha256(value.get("checkpoint_sha256"), f"{label} reused_from.checkpoint_sha256"),
        "document_sha256": _sha256(value.get("document_sha256"), f"{label} reused_from.document_sha256"),
    }


def validate_evaluation_checkpoint(value: Any) -> dict[str, Any]:
    """逐规则逐侧 write-once checkpoint（builder 写、accept 与恢复链读）。"""

    required = {
        "schema_version", "sequence", "rule", "side", "status", "document", "input_projection",
        "projection_sha256", "checker_sha256", "command_sha256", "context", "dependency_projection_sha256",
        "executed_by", "reused_from", "recorded_at_utc", "previous_checkpoint_sha256", "checkpoint_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估 checkpoint 字段不闭合")
    payload = dict(value)
    label = "评估 checkpoint"
    if payload.get("schema_version") != EVALUATION_CHECKPOINT_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _positive_int(payload.get("sequence"), f"{label} sequence")
    rule = payload.get("rule")
    if not isinstance(rule, str) or not RULE_RE.fullmatch(rule):
        raise VCArtifactError(f"{label} rule 非法")
    if payload.get("side") not in EVALUATION_SIDES:
        raise VCArtifactError(f"{label} side 非法")
    if payload.get("status") not in {"pass", "fail"}:
        raise VCArtifactError(f"{label} status 非法")
    _binding(payload.get("document"), f"{label} document")
    projection = _binding(payload.get("input_projection"), f"{label} input_projection")
    if _sha256(payload.get("projection_sha256"), f"{label} projection_sha256") != projection["sha256"]:
        raise VCArtifactError(f"{label} projection_sha256 必须等于投影文件摘要")
    _sha256(payload.get("checker_sha256"), f"{label} checker_sha256")
    _sha256(payload.get("command_sha256"), f"{label} command_sha256")
    context = payload.get("context")
    if not isinstance(context, Mapping) or set(context) != {
        "capture_manifest", "evidence_root", "profile_sha256", "rule_manifest_sha256",
    }:
        raise VCArtifactError(f"{label} context 字段不闭合")
    _binding(context.get("capture_manifest"), f"{label} context.capture_manifest")
    if not isinstance(context.get("evidence_root"), str) or not context["evidence_root"]:
        raise VCArtifactError(f"{label} context.evidence_root 非法")
    _sha256(context.get("profile_sha256"), f"{label} context.profile_sha256")
    _sha256(context.get("rule_manifest_sha256"), f"{label} context.rule_manifest_sha256")
    _sha256(payload.get("dependency_projection_sha256"), f"{label} dependency_projection_sha256")
    executed_by = payload.get("executed_by")
    if not isinstance(executed_by, Mapping) or set(executed_by) != {
        "builder_sha256", "run_dir", "owner_nonce", "run_manifest_sha256",
    }:
        raise VCArtifactError(f"{label} executed_by 字段不闭合")
    _sha256(executed_by.get("builder_sha256"), f"{label} executed_by.builder_sha256")
    if not isinstance(executed_by.get("run_dir"), str) or not executed_by["run_dir"]:
        raise VCArtifactError(f"{label} executed_by.run_dir 非法")
    _sha256(executed_by.get("owner_nonce"), f"{label} executed_by.owner_nonce")
    _sha256(executed_by.get("run_manifest_sha256"), f"{label} executed_by.run_manifest_sha256")
    reused = _reused_from(payload.get("reused_from"), label)
    if reused is not None and payload.get("status") != "pass":
        raise VCArtifactError(f"{label} 复用 checkpoint 只能是 pass")
    _timestamp(payload.get("recorded_at_utc"), f"{label} recorded_at_utc")
    _optional_sha256(payload.get("previous_checkpoint_sha256"), f"{label} previous_checkpoint_sha256")
    _self_digest(payload, "checkpoint_sha256", label)
    return payload


def validate_evaluation_run(value: Any) -> dict[str, Any]:
    """evaluation-run.json：只汇总 checkpoint 的评估运行索引。"""

    required = {
        "schema_version", "campaign_id", "candidate_id", "candidate_revision", "evaluation_baseline", "derived",
        "evaluator", "rules", "checkpoint_head_sha256", "recorded_at_utc", "run_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("evaluation-run 字段不闭合")
    payload = dict(value)
    label = "evaluation-run"
    if payload.get("schema_version") != EVALUATION_RUN_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _safe_id(payload.get("campaign_id"), f"{label} campaign_id")
    _safe_id(payload.get("candidate_id"), f"{label} candidate_id")
    _positive_int(payload.get("candidate_revision"), f"{label} candidate_revision")
    baseline = _non_negative_int(payload.get("evaluation_baseline"), f"{label} evaluation_baseline")
    derived = payload.get("derived")
    if not isinstance(derived, bool):
        raise VCArtifactError(f"{label} derived 必须是布尔值")
    if derived and baseline != 0:
        raise VCArtifactError(f"{label} 只有 b0 允许只读派生索引")
    validate_evaluator_digests(payload.get("evaluator"), label)
    rules = payload.get("rules")
    if not isinstance(rules, list):
        raise VCArtifactError(f"{label} rules 必须是数组")
    seen: list[str] = []
    for index, row in enumerate(rules, 1):
        if not isinstance(row, Mapping) or set(row) != {
            "rule", "validation_mode", "status", "candidate_checkpoint", "official_checkpoint",
            "dependency_projection_sha256", "reused_from",
        }:
            raise VCArtifactError(f"{label} 第 {index} 行字段不闭合")
        rule = row.get("rule")
        if not isinstance(rule, str) or not RULE_RE.fullmatch(rule):
            raise VCArtifactError(f"{label} 第 {index} 行 rule 非法")
        seen.append(rule)
        if row.get("validation_mode") not in VALIDATION_MODES:
            raise VCArtifactError(f"{label} {rule} validation_mode 非法")
        status = row.get("status")
        if status not in EVALUATION_RULE_STATUSES:
            raise VCArtifactError(f"{label} {rule} status 非法")
        candidate = _optional_binding(row.get("candidate_checkpoint"), f"{label} {rule} candidate_checkpoint")
        official = _optional_binding(row.get("official_checkpoint"), f"{label} {rule} official_checkpoint")
        _optional_sha256(row.get("dependency_projection_sha256"), f"{label} {rule} dependency_projection_sha256")
        reused = _reused_from(row.get("reused_from"), f"{label} {rule}")
        if derived:
            if candidate is not None or official is not None or reused is not None:
                raise VCArtifactError(f"{label} 派生索引不得引用 checkpoint 或复用")
        elif status == "pending":
            if reused is not None:
                raise VCArtifactError(f"{label} {rule} pending 行不得声明复用")
        elif candidate is None:
            raise VCArtifactError(f"{label} {rule} 已执行行必须绑定候选侧 checkpoint")
        if reused is not None and status != "pass":
            raise VCArtifactError(f"{label} {rule} 复用行只能是 pass")
    if seen != sorted(set(seen)):
        raise VCArtifactError(f"{label} rules 必须按规则编号唯一排序")
    _optional_sha256(payload.get("checkpoint_head_sha256"), f"{label} checkpoint_head_sha256")
    _timestamp(payload.get("recorded_at_utc"), f"{label} recorded_at_utc")
    _self_digest(payload, "run_sha256", label)
    return payload


EVALUATION_DIAGNOSIS_REVIEW_FIELDS = (
    "schema_version",
    "campaign_id",
    "campaign_manifest_sha256",
    "candidate_id",
    "candidate_revision",
    "evaluation_baseline",
    "failure_source",
    "reuse_authority",
    "failed_step",
    "failed_run",
    "action_outputs",
    "evaluation_run",
    "failure_scope",
    "failed_evaluator_digests",
    "admissible_classes",
    "project_ledger_head_sequence",
    "project_ledger_head_sha256",
)


def evaluation_diagnosis_review_sha256(payload: Mapping[str, Any]) -> str:
    """诊断的稳定字段摘要（apply 以 ``--approve-sha256`` 复算比对；不含 reviewer／时间／当前摘要）。"""

    return digest({field: payload[field] for field in EVALUATION_DIAGNOSIS_REVIEW_FIELDS})


def build_evaluation_failure_diagnosis(
    *,
    campaign_id: str,
    campaign_manifest_sha256: str,
    candidate_id: str,
    candidate_revision: int,
    evaluation_baseline: int,
    failure_source: str,
    reuse_authority: str,
    failed_step: str,
    failed_run: Mapping[str, Any],
    action_outputs: Mapping[str, Any] | None,
    evaluation_run: Mapping[str, Any] | None,
    failure_scope: Mapping[str, Any],
    failed_evaluator_digests: Mapping[str, Any],
    current_evaluator_digests: Mapping[str, Any],
    admissible_classes: Sequence[str],
    project_ledger_head_sequence: int,
    project_ledger_head_sha256: str,
    reviewer: str,
    reviewed_at_utc: str,
) -> dict[str, Any]:
    """evaluation-recover preview 签发的诊断收据（write-once；apply 复算 review_sha256）。"""

    payload: dict[str, Any] = {
        "schema_version": EVALUATION_FAILURE_DIAGNOSIS_SCHEMA,
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "candidate_id": candidate_id,
        "candidate_revision": candidate_revision,
        "evaluation_baseline": evaluation_baseline,
        "failure_source": failure_source,
        "reuse_authority": reuse_authority,
        "failed_step": failed_step,
        "failed_run": json.loads(json.dumps(dict(failed_run), ensure_ascii=False)),
        "action_outputs": (
            json.loads(json.dumps(dict(action_outputs), ensure_ascii=False)) if action_outputs is not None else None
        ),
        "evaluation_run": (
            json.loads(json.dumps(dict(evaluation_run), ensure_ascii=False)) if evaluation_run is not None else None
        ),
        "failure_scope": json.loads(json.dumps(dict(failure_scope), ensure_ascii=False)),
        "failed_evaluator_digests": dict(failed_evaluator_digests),
        "current_evaluator_digests": dict(current_evaluator_digests),
        "admissible_classes": sorted(set(admissible_classes)),
        "project_ledger_head_sequence": project_ledger_head_sequence,
        "project_ledger_head_sha256": project_ledger_head_sha256,
        "reviewer": reviewer,
        "reviewed_at_utc": reviewed_at_utc,
    }
    payload["review_sha256"] = evaluation_diagnosis_review_sha256(payload)
    payload["receipt_sha256"] = digest(payload)
    return validate_evaluation_failure_diagnosis(payload)


def validate_evaluation_failure_diagnosis(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "campaign_manifest_sha256", "candidate_id", "candidate_revision",
        "evaluation_baseline", "failure_source", "reuse_authority", "failed_step", "failed_run", "action_outputs",
        "evaluation_run", "failure_scope", "failed_evaluator_digests", "current_evaluator_digests",
        "admissible_classes", "project_ledger_head_sequence", "project_ledger_head_sha256", "reviewer",
        "reviewed_at_utc", "review_sha256", "receipt_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("评估失败诊断字段不闭合")
    payload = dict(value)
    label = "评估失败诊断"
    if payload.get("schema_version") != EVALUATION_FAILURE_DIAGNOSIS_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _safe_id(payload.get("campaign_id"), f"{label} campaign_id")
    _sha256(payload.get("campaign_manifest_sha256"), f"{label} campaign_manifest_sha256")
    _safe_id(payload.get("candidate_id"), f"{label} candidate_id")
    _positive_int(payload.get("candidate_revision"), f"{label} candidate_revision")
    _non_negative_int(payload.get("evaluation_baseline"), f"{label} evaluation_baseline")
    source = payload.get("failure_source")
    if source not in FAILURE_SOURCES:
        raise VCArtifactError(f"{label} failure_source 非法")
    authority = payload.get("reuse_authority")
    if authority not in REUSE_AUTHORITIES:
        raise VCArtifactError(f"{label} reuse_authority 非法")
    failed_step = payload.get("failed_step")
    if not isinstance(failed_step, str) or not failed_step or len(failed_step) > 128:
        raise VCArtifactError(f"{label} failed_step 非法")
    if source == "offline-compare-failed" and (failed_step != "compare" or authority != "none"):
        raise VCArtifactError(f"{label} compare 工具异常的 failed_step 固定为 compare 且禁止复用")
    if source == "offline-accept-failed" and failed_step != "acceptance":
        raise VCArtifactError(f"{label} accept 工具异常的 failed_step 固定为 acceptance")
    if source == "assertion-failed" and not RULE_RE.fullmatch(failed_step):
        raise VCArtifactError(f"{label} 断言失败的 failed_step 必须是首条失败规则编号")
    failed_run = payload.get("failed_run")
    if not isinstance(failed_run, Mapping) or set(failed_run) != {
        "run_id", "run_dir", "manifest_sha256", "state_sha256", "stop_receipt_sha256", "action_id",
        "action_diagnostic_sha256", "action_outputs_sha256",
    }:
        raise VCArtifactError(f"{label} failed_run 字段不闭合")
    _safe_id(failed_run.get("run_id"), f"{label} failed_run.run_id")
    if not isinstance(failed_run.get("run_dir"), str) or not failed_run["run_dir"]:
        raise VCArtifactError(f"{label} failed_run.run_dir 非法")
    for field in ("manifest_sha256", "state_sha256", "stop_receipt_sha256", "action_diagnostic_sha256"):
        _sha256(failed_run.get(field), f"{label} failed_run.{field}")
    _safe_id(failed_run.get("action_id"), f"{label} failed_run.action_id")
    outputs_sha256 = _optional_sha256(failed_run.get("action_outputs_sha256"), f"{label} failed_run.action_outputs_sha256")
    outputs = payload.get("action_outputs")
    if outputs is not None:
        if not isinstance(outputs, Mapping) or set(outputs) != {"path", "sha256", "evaluation_run", "checkpoint_head_sha256"}:
            raise VCArtifactError(f"{label} action_outputs 字段不闭合")
        if not isinstance(outputs.get("path"), str) or not outputs["path"]:
            raise VCArtifactError(f"{label} action_outputs.path 非法")
        _sha256(outputs.get("sha256"), f"{label} action_outputs.sha256")
        _optional_binding(outputs.get("evaluation_run"), f"{label} action_outputs.evaluation_run")
        _optional_sha256(outputs.get("checkpoint_head_sha256"), f"{label} action_outputs.checkpoint_head_sha256")
    if (outputs_sha256 is None) != (outputs is None):
        raise VCArtifactError(f"{label} action_outputs 与 failed_run.action_outputs_sha256 必须同时存在或同时为 null")
    if authority == "anchored" and (outputs is None or outputs.get("evaluation_run") is None):
        raise VCArtifactError(f"{label} anchored 授权必须由动作输出绑定中的 evaluation-run 摘要支撑")
    run_binding = payload.get("evaluation_run")
    if run_binding is not None:
        if not isinstance(run_binding, Mapping) or set(run_binding) != {"path", "sha256", "derived"}:
            raise VCArtifactError(f"{label} evaluation_run 字段不闭合")
        if not isinstance(run_binding.get("path"), str) or not run_binding["path"]:
            raise VCArtifactError(f"{label} evaluation_run.path 非法")
        _sha256(run_binding.get("sha256"), f"{label} evaluation_run.sha256")
        if not isinstance(run_binding.get("derived"), bool):
            raise VCArtifactError(f"{label} evaluation_run.derived 非法")
    scope = payload.get("failure_scope")
    if not isinstance(scope, Mapping) or set(scope) != {"failed_rules", "failed_checks", "jobs", "rules", "official_refs"}:
        raise VCArtifactError(f"{label} failure_scope 字段不闭合")
    _rule_ids(scope.get("failed_rules"), f"{label} failure_scope.failed_rules")
    _rule_ids(scope.get("rules"), f"{label} failure_scope.rules")
    _sorted_safe_ids(scope.get("jobs"), f"{label} failure_scope.jobs")
    checks = scope.get("failed_checks")
    if not isinstance(checks, list):
        raise VCArtifactError(f"{label} failure_scope.failed_checks 必须是数组")
    for item in checks:
        if not isinstance(item, Mapping) or set(item) != {"rule", "check_id", "evidence_paths", "jobs"}:
            raise VCArtifactError(f"{label} failure_scope.failed_checks 条目字段不闭合")
        if not isinstance(item.get("rule"), str) or not RULE_RE.fullmatch(item["rule"]):
            raise VCArtifactError(f"{label} failure_scope.failed_checks.rule 非法")
        if not isinstance(item.get("check_id"), str) or not item["check_id"]:
            raise VCArtifactError(f"{label} failure_scope.failed_checks.check_id 非法")
        if not isinstance(item.get("evidence_paths"), list) or any(
            not isinstance(path, str) or not path for path in item["evidence_paths"]
        ):
            raise VCArtifactError(f"{label} failure_scope.failed_checks.evidence_paths 非法")
        _sorted_safe_ids(item.get("jobs"), f"{label} failure_scope.failed_checks.jobs")
    refs = scope.get("official_refs")
    if not isinstance(refs, list) or any(not isinstance(path, str) or not path for path in refs):
        raise VCArtifactError(f"{label} failure_scope.official_refs 非法")
    if source != "assertion-failed" and (scope["failed_rules"] or scope["jobs"] or scope["rules"] or checks):
        raise VCArtifactError(f"{label} 离线动作失败的 failure-scope 规则集与 Job 集必须为空")
    validate_evaluator_digests(payload.get("failed_evaluator_digests"), f"{label} failed")
    validate_evaluator_digests(payload.get("current_evaluator_digests"), f"{label} current")
    classes = payload.get("admissible_classes")
    if (
        not isinstance(classes, list)
        or classes != sorted(set(classes))
        or not classes
        or any(item not in ROOT_CAUSE_CLASSES for item in classes)
    ):
        raise VCArtifactError(f"{label} admissible_classes 非法")
    if source != "assertion-failed" and "transient-environment" in classes:
        raise VCArtifactError(f"{label} 离线动作失败不允许裁定 transient-environment")
    _non_negative_int(payload.get("project_ledger_head_sequence"), f"{label} project_ledger_head_sequence")
    _sha256(payload.get("project_ledger_head_sha256"), f"{label} project_ledger_head_sha256")
    reviewer = payload.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer or len(reviewer) > 128:
        raise VCArtifactError(f"{label} reviewer 非法")
    _timestamp(payload.get("reviewed_at_utc"), f"{label} reviewed_at_utc")
    if _sha256(payload.get("review_sha256"), f"{label} review_sha256") != evaluation_diagnosis_review_sha256(payload):
        raise VCArtifactError(f"{label} review_sha256 与稳定字段不一致")
    _self_digest(payload, "receipt_sha256", label)
    return payload


def build_action_output_binding(
    *,
    campaign_id: str,
    phase: str,
    action_id: str,
    run_manifest_sha256: str,
    owner_nonce: str,
    bindings: Sequence[Mapping[str, Any]],
    recorded_at_utc: str,
) -> dict[str, Any]:
    """run-<id>/action-outputs/<action_id>.json：动作退出后、stop-receipt 前 write-once 写出的产物绑定。"""

    payload = {
        "schema_version": ACTION_OUTPUT_BINDING_SCHEMA,
        "campaign_id": campaign_id,
        "phase": phase,
        "action_id": action_id,
        "run_manifest_sha256": run_manifest_sha256,
        "owner_nonce": owner_nonce,
        "bindings": [dict(item) for item in bindings],
        "recorded_at_utc": recorded_at_utc,
    }
    payload["binding_sha256"] = digest(payload)
    return validate_action_output_binding(payload)


def validate_action_output_binding(value: Any) -> dict[str, Any]:
    required = {
        "schema_version", "campaign_id", "phase", "action_id", "run_manifest_sha256", "owner_nonce",
        "bindings", "recorded_at_utc", "binding_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("动作输出绑定字段不闭合")
    payload = dict(value)
    label = "动作输出绑定"
    if payload.get("schema_version") != ACTION_OUTPUT_BINDING_SCHEMA:
        raise VCArtifactError(f"{label} schema_version 非法")
    _safe_id(payload.get("campaign_id"), f"{label} campaign_id")
    phase = payload.get("phase")
    if not isinstance(phase, str) or not phase or len(phase) > 32:
        raise VCArtifactError(f"{label} phase 非法")
    _safe_id(payload.get("action_id"), f"{label} action_id")
    _sha256(payload.get("run_manifest_sha256"), f"{label} run_manifest_sha256")
    _sha256(payload.get("owner_nonce"), f"{label} owner_nonce")
    bindings = payload.get("bindings")
    if not isinstance(bindings, list) or not bindings or len(bindings) > 64:
        raise VCArtifactError(f"{label} bindings 必须是 1～64 项的数组")
    paths: list[str] = []
    for index, item in enumerate(bindings, 1):
        if not isinstance(item, Mapping) or set(item) != {"path", "exists", "sha256"}:
            raise VCArtifactError(f"{label} 第 {index} 项字段不闭合")
        paths.append(_relative_path(item.get("path"), f"{label} 第 {index} 项 path"))
        if not isinstance(item.get("exists"), bool):
            raise VCArtifactError(f"{label} 第 {index} 项 exists 必须是布尔值")
        _optional_sha256(item.get("sha256"), f"{label} 第 {index} 项 sha256")
        if not item["exists"] and item.get("sha256") is not None:
            raise VCArtifactError(f"{label} 第 {index} 项不存在的产物不得有摘要")
    if paths != sorted(set(paths)):
        raise VCArtifactError(f"{label} bindings 必须按路径唯一排序")
    _timestamp(payload.get("recorded_at_utc"), f"{label} recorded_at_utc")
    _self_digest(payload, "binding_sha256", label)
    return payload


def build_interrupted_recovery_contract(
    *,
    campaign_plan: Mapping[str, Any],
    batch_sequence: int,
    source_attempt: Mapping[str, Any],
    failed_supervisor: Mapping[str, Any],
    timing_ledger: Mapping[str, Any],
    deployment_receipt: Mapping[str, Any],
    tool_transition: Mapping[str, Any],
    compiled_at_utc: str,
    must_start_by_utc: str,
) -> dict[str, Any]:
    """生成 VC-1 中断恢复的单次使用控制合同。

    合同只保存小型绑定与集合，不读取或嵌入原始抓包正文。执行集合必须由
    checkpoint 的 failed/pending 闭集得出，已完成项只能进入复用集合。
    """

    plan = validate_campaign_plan(campaign_plan)
    payload = {
        "schema_version": INTERRUPTED_RECOVERY_CONTRACT_SCHEMA,
        "campaign_id": plan["campaign_id"],
        "campaign_plan_sha256": plan["plan_sha256"],
        "phase": "VC-1",
        "batch_sequence": batch_sequence,
        "source_attempt": json.loads(
            json.dumps(dict(source_attempt), ensure_ascii=False)
        ),
        "failed_supervisor": json.loads(
            json.dumps(dict(failed_supervisor), ensure_ascii=False)
        ),
        "timing_ledger": json.loads(
            json.dumps(dict(timing_ledger), ensure_ascii=False)
        ),
        "deployment_receipt": json.loads(
            json.dumps(dict(deployment_receipt), ensure_ascii=False)
        ),
        "tool_transition": json.loads(
            json.dumps(dict(tool_transition), ensure_ascii=False)
        ),
        "zero_request_boundary": {
            "reservation_exists": False,
            "live_request_count": 0,
            "scanned_bytes": 0,
        },
        "compiled_at_utc": _timestamp(compiled_at_utc, "compiled_at_utc"),
        "must_start_by_utc": _timestamp(
            must_start_by_utc, "must_start_by_utc"
        ),
        "original_deadline_at_utc": plan["original_deadline_at_utc"],
    }
    payload["contract_sha256"] = digest(payload)
    return validate_interrupted_recovery_contract(payload, plan)


def _sorted_safe_ids(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or value != sorted(set(value))
        or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in value)
    ):
        raise VCArtifactError(f"{label} 必须是排序且无重复的安全标识数组")
    return list(value)


def validate_interrupted_recovery_contract(
    value: Any,
    campaign_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """校验中断恢复合同的身份、checkpoint 闭集与零请求边界。"""

    required = {
        "schema_version",
        "campaign_id",
        "campaign_plan_sha256",
        "phase",
        "batch_sequence",
        "source_attempt",
        "failed_supervisor",
        "timing_ledger",
        "deployment_receipt",
        "tool_transition",
        "zero_request_boundary",
        "compiled_at_utc",
        "must_start_by_utc",
        "original_deadline_at_utc",
        "contract_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("中断恢复合同字段不闭合")
    payload = dict(value)
    sequence = payload.get("batch_sequence")
    if (
        payload.get("schema_version") != INTERRUPTED_RECOVERY_CONTRACT_SCHEMA
        or payload.get("phase") != "VC-1"
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 2
    ):
        raise VCArtifactError("中断恢复合同 schema、阶段或批次序号非法")
    _safe_id(payload.get("campaign_id"), "中断恢复 campaign_id")
    _sha256(payload.get("campaign_plan_sha256"), "中断恢复 plan SHA")

    source = payload.get("source_attempt")
    source_fields = {
        "attempt_id",
        "reservation",
        "run_nonce",
        "identity_sha256",
        "checkpoint",
        "planned_job_ids",
        "completed_job_ids",
        "failed_job_ids",
        "pending_job_ids",
        "execute_job_ids",
        "reuse_job_ids",
    }
    if not isinstance(source, Mapping) or set(source) != source_fields:
        raise VCArtifactError("中断恢复 source_attempt 字段不闭合")
    _safe_id(source.get("attempt_id"), "中断恢复 attempt_id")
    _sha256(source.get("run_nonce"), "中断恢复 run_nonce")
    _sha256(source.get("identity_sha256"), "中断恢复 identity SHA")
    reservation = source.get("reservation")
    if not isinstance(reservation, Mapping) or set(reservation) != {
        "path",
        "sha256",
        "reservation_digest",
    }:
        raise VCArtifactError("中断恢复 reservation 绑定不闭合")
    _binding(
        {"path": reservation.get("path"), "sha256": reservation.get("sha256")},
        "中断恢复 reservation",
    )
    _sha256(reservation.get("reservation_digest"), "reservation_digest")
    checkpoint = source.get("checkpoint")
    if not isinstance(checkpoint, Mapping) or set(checkpoint) != {
        "path",
        "record_count",
        "last_sequence",
        "last_sha256",
    }:
        raise VCArtifactError("中断恢复 checkpoint 绑定不闭合")
    if not isinstance(checkpoint.get("path"), str) or not checkpoint["path"]:
        raise VCArtifactError("中断恢复 checkpoint 路径为空")
    record_count = checkpoint.get("record_count")
    if (
        not isinstance(record_count, int)
        or isinstance(record_count, bool)
        or record_count < 1
        or checkpoint.get("last_sequence") != record_count
    ):
        raise VCArtifactError("中断恢复 checkpoint 计数或末序号非法")
    _sha256(checkpoint.get("last_sha256"), "中断恢复 checkpoint SHA")
    planned = set(
        _sorted_safe_ids(source.get("planned_job_ids"), "planned_job_ids", nonempty=True)
    )
    completed = set(_sorted_safe_ids(source.get("completed_job_ids"), "completed_job_ids"))
    failed = set(_sorted_safe_ids(source.get("failed_job_ids"), "failed_job_ids", nonempty=True))
    pending = set(_sorted_safe_ids(source.get("pending_job_ids"), "pending_job_ids"))
    execute = set(_sorted_safe_ids(source.get("execute_job_ids"), "execute_job_ids", nonempty=True))
    reuse = set(_sorted_safe_ids(source.get("reuse_job_ids"), "reuse_job_ids", nonempty=True))
    if (
        completed & failed
        or completed & pending
        or failed & pending
        or completed | failed | pending != planned
        or execute != failed | pending
        or reuse != completed
        or execute & reuse
    ):
        raise VCArtifactError("中断恢复 checkpoint 执行／复用闭集非法")

    supervisor = payload.get("failed_supervisor")
    if not isinstance(supervisor, Mapping) or set(supervisor) != {
        "run_dir",
        "state_sha256",
        "manifest_sha256",
        "stop_receipt_sha256",
        "owner_nonce",
        "terminal_at_utc",
        "state",
        "reason",
        "batch_id",
        "batch_sequence",
        "batch_sha256",
    }:
        raise VCArtifactError("中断恢复 failed_supervisor 字段不闭合")
    if (
        not isinstance(supervisor.get("run_dir"), str)
        or not supervisor["run_dir"].startswith("/")
        or supervisor.get("state") != "failed"
        or supervisor.get("reason") != "KeyboardInterrupt"
        or supervisor.get("batch_sequence") != sequence - 1
    ):
        raise VCArtifactError("中断恢复失败监督器状态或直接前序关系非法")
    _safe_id(supervisor.get("batch_id"), "失败监督器 batch_id")
    _sha256(supervisor.get("state_sha256"), "失败监督器 state SHA")
    _sha256(supervisor.get("manifest_sha256"), "失败监督器 manifest SHA")
    _sha256(supervisor.get("stop_receipt_sha256"), "失败监督器 stop SHA")
    _sha256(supervisor.get("owner_nonce"), "失败监督器 owner nonce")
    _sha256(supervisor.get("batch_sha256"), "失败监督器 batch SHA")
    _timestamp(supervisor.get("terminal_at_utc"), "失败监督器 terminal_at_utc")

    ledger = payload.get("timing_ledger")
    if not isinstance(ledger, Mapping) or set(ledger) != {
        "ledger_dir",
        "ledger_plan_sha256",
        "event_head_sequence",
        "event_head_sha256",
        "status",
        "total_live_request_count",
        "total_deadline_at_utc",
    }:
        raise VCArtifactError("中断恢复 timing_ledger 字段不闭合")
    if (
        not isinstance(ledger.get("ledger_dir"), str)
        or not ledger["ledger_dir"].startswith("/")
        or ledger.get("status") != "active"
        or not isinstance(ledger.get("event_head_sequence"), int)
        or isinstance(ledger.get("event_head_sequence"), bool)
        or ledger["event_head_sequence"] < 1
        or not isinstance(ledger.get("total_live_request_count"), int)
        or isinstance(ledger.get("total_live_request_count"), bool)
        or ledger["total_live_request_count"] < 1
    ):
        raise VCArtifactError("中断恢复 timing_ledger 状态、计数或路径非法")
    _sha256(ledger.get("ledger_plan_sha256"), "timing ledger plan SHA")
    _sha256(ledger.get("event_head_sha256"), "timing ledger head SHA")
    _timestamp(ledger.get("total_deadline_at_utc"), "timing ledger deadline")

    deployment = payload.get("deployment_receipt")
    if not isinstance(deployment, Mapping) or set(deployment) != {
        "path",
        "sha256",
        "tool_files_sha256",
    }:
        raise VCArtifactError("中断恢复 deployment_receipt 字段不闭合")
    if not isinstance(deployment.get("path"), str) or not deployment["path"].startswith("/"):
        raise VCArtifactError("中断恢复 deployment receipt 必须是绝对路径")
    _sha256(deployment.get("sha256"), "deployment receipt SHA")
    _sha256(deployment.get("tool_files_sha256"), "deployment tool SHA")

    transition = payload.get("tool_transition")
    if not isinstance(transition, Mapping) or set(transition) != {
        "from_tool_files_sha256",
        "to_tool_files_sha256",
        "changed_files",
        "allowed_production_paths",
        "affected_job_ids",
    }:
        raise VCArtifactError("中断恢复 tool_transition 字段不闭合")
    _sha256(transition.get("from_tool_files_sha256"), "transition from SHA")
    _sha256(transition.get("to_tool_files_sha256"), "transition to SHA")
    changed = transition.get("changed_files")
    if not isinstance(changed, list) or not changed:
        raise VCArtifactError("中断恢复 changed_files 不能为空")
    paths: list[str] = []
    production_paths: set[str] = set()
    changed_affected: set[str] = set()
    for item in changed:
        if not isinstance(item, Mapping) or set(item) != {
            "path",
            "from_sha256",
            "to_sha256",
            "classification",
            "affected_job_ids",
        }:
            raise VCArtifactError("中断恢复 changed_files 项字段不闭合")
        path = item.get("path")
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise VCArtifactError("中断恢复 changed_files 路径非法")
        if item.get("from_sha256") is not None:
            _sha256(item.get("from_sha256"), "changed_files from SHA")
        if item.get("to_sha256") is not None:
            _sha256(item.get("to_sha256"), "changed_files to SHA")
        classification = item.get("classification")
        if classification not in {
            "evaluation",
            "phase_scoped_hybrid",
            "failed_job_production",
        }:
            raise VCArtifactError("中断恢复 changed_files 分类非法")
        affected = set(
            _sorted_safe_ids(item.get("affected_job_ids"), "changed affected_job_ids")
        )
        if classification == "failed_job_production":
            if not affected:
                raise VCArtifactError("产出侧变化必须映射至少一个 Job")
            production_paths.add(path)
            changed_affected.update(affected)
        elif affected:
            raise VCArtifactError("评估／混合变化不得声明产出 Job")
        paths.append(path)
    if paths != sorted(set(paths)):
        raise VCArtifactError("中断恢复 changed_files 必须按路径唯一排序")
    allowed_paths = transition.get("allowed_production_paths")
    if (
        not isinstance(allowed_paths, list)
        or allowed_paths != sorted(set(allowed_paths))
        or set(allowed_paths) != production_paths
    ):
        raise VCArtifactError("中断恢复产出侧路径闭集非法")
    transition_affected = set(
        _sorted_safe_ids(transition.get("affected_job_ids"), "transition affected_job_ids", nonempty=True)
    )
    if (
        transition_affected != changed_affected
        or not transition_affected.issubset(execute)
        or transition_affected & reuse
    ):
        raise VCArtifactError("中断恢复产出侧变化越过 failed/pending 闭集")

    boundary = payload.get("zero_request_boundary")
    if boundary != {
        "reservation_exists": False,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }:
        raise VCArtifactError("中断恢复预览必须是零预约、零请求、零扫描")
    compiled = _timestamp(payload.get("compiled_at_utc"), "恢复合同 compiled_at_utc")
    start_by = _timestamp(payload.get("must_start_by_utc"), "恢复合同 must_start_by_utc")
    deadline = _timestamp(
        payload.get("original_deadline_at_utc"),
        "恢复合同 original_deadline_at_utc",
    )
    if not (
        datetime.fromisoformat(compiled.replace("Z", "+00:00"))
        < datetime.fromisoformat(start_by.replace("Z", "+00:00"))
        <= datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    ):
        raise VCArtifactError("中断恢复合同启动时限未承接原始 deadline")
    if ledger["total_deadline_at_utc"] != deadline:
        raise VCArtifactError("中断恢复 Ledger 与 Campaign 总 deadline 不一致")
    if deployment["tool_files_sha256"] != transition["to_tool_files_sha256"]:
        raise VCArtifactError("中断恢复部署摘要与目标工具身份不一致")
    if campaign_plan is not None:
        plan = validate_campaign_plan(campaign_plan)
        if (
            payload["campaign_id"] != plan["campaign_id"]
            or payload["campaign_plan_sha256"] != plan["plan_sha256"]
            or deadline != plan["original_deadline_at_utc"]
        ):
            raise VCArtifactError("中断恢复合同未绑定同一 Campaign 总计划")
    _self_digest(payload, "contract_sha256", "中断恢复合同")
    return payload


def _rule_ids(values: Any, label: str) -> list[str]:
    if (
        not isinstance(values, list)
        or not all(isinstance(value, str) and RULE_RE.fullmatch(value) for value in values)
        or values != sorted(set(values))
    ):
        raise VCArtifactError(f"{label} 必须是排序且无重复的规则编号数组")
    return list(values)


def _binding(value: Any, label: str, *, bytes_required: bool = False) -> dict[str, Any]:
    expected = {"path", "sha256", "bytes"} if bytes_required else {"path", "sha256"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise VCArtifactError(f"{label} 文件绑定字段不闭合")
    path = value.get("path")
    if not isinstance(path, str) or not path:
        raise VCArtifactError(f"{label}.path 不能为空")
    result: dict[str, Any] = {"path": path, "sha256": _sha256(value.get("sha256"), f"{label}.sha256")}
    if bytes_required:
        size = value.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise VCArtifactError(f"{label}.bytes 非法")
        result["bytes"] = size
    return result


def _checkpoint_reference(value: Any, label: str) -> dict[str, Any]:
    """校验后继阶段保存的直接前序 checkpoint 身份。"""

    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "sha256",
        "phase",
        "checkpoint_sha256",
    }:
        raise VCArtifactError(f"{label}字段不闭合")
    phase = value.get("phase")
    if phase not in VC_PHASES:
        raise VCArtifactError(f"{label}.phase 非法")
    return {
        **_binding(
            {"path": value.get("path"), "sha256": value.get("sha256")},
            label,
        ),
        "phase": phase,
        "checkpoint_sha256": _sha256(
            value.get("checkpoint_sha256"),
            f"{label}.checkpoint_sha256",
        ),
    }


def _positive_seconds(value: Any, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 < float(value) <= 24 * 60 * 60
    ):
        raise VCArtifactError(f"{label} 必须是一天内的正秒数")
    return float(value)


def _command_option(command: Sequence[str], name: str) -> str | None:
    """从冻结命令里提取一个 ``--name value``／``--name=value`` 选项；重复即非法。"""

    values: list[str] = []
    index = 0
    while index < len(command):
        token = command[index]
        if token == name:
            if index + 1 >= len(command):
                raise VCArtifactError(f"canonical 动作选项 {name} 缺少取值")
            values.append(command[index + 1])
            index += 2
            continue
        if token.startswith(f"{name}="):
            values.append(token[len(name) + 1 :])
        index += 1
    if len(values) > 1:
        raise VCArtifactError(f"canonical 动作选项 {name} 重复出现")
    return values[0] if values else None


def _upgrade_cli_subcommand(command: Sequence[str]) -> str | None:
    """返回 codex_upgrade CLI 动作的子命令；不是该 CLI 的动作返回 None。"""

    for index, token in enumerate(command[:4]):
        if PurePosixPath(token).name in UPGRADE_CLI_BASENAMES:
            if index + 1 >= len(command):
                raise VCArtifactError("codex_upgrade 动作缺少子命令")
            return command[index + 1]
    return None


def canonical_action_binding(action: Mapping[str, Any]) -> dict[str, Any] | None:
    """按工具冻结映射解析一个 canonical 动作；非 canonical 动作返回 None。

    失败关闭：canonical item 与子命令必须一一对应，动作只能承载一个 canonical
    item，命令里必须带可信的 ``--campaign-dir``／``--candidate-id``／``--attempt-id``，
    不得自带 ``--supervisor-run-dir``（时间锚只能来自派发它的父监督器），
    canonical-import 必须是带批准摘要的写入形态；给别的命令套 canonical item 名，
    或把 canonical 子命令挂在别的 item 名下，都在这里拒绝。
    """

    command = [str(item) for item in action.get("command", [])]
    item_ids = [str(item) for item in action.get("item_ids", [])]
    canonical_items = sorted(item for item in set(item_ids) if is_canonical_item(item))
    subcommand = _upgrade_cli_subcommand(command)
    if not canonical_items:
        if subcommand in CANONICAL_SUBCOMMANDS:
            raise VCArtifactError(
                f"canonical 子命令 {subcommand} 必须以冻结的 canonical item 登记"
            )
        return None
    if len(canonical_items) != 1 or item_ids != canonical_items:
        raise VCArtifactError("canonical 动作只能精确承载一个 canonical item")
    item_id = canonical_items[0]
    group = canonical_item_phase(item_id)
    expected_subcommand, expected_step, retire_version = canonical_item_command(item_id)
    if subcommand != expected_subcommand:
        raise VCArtifactError(
            f"canonical item {item_id} 只能由子命令 {expected_subcommand} 承载"
        )
    step = _command_option(command, "--canonical-step")
    if step != expected_step:
        raise VCArtifactError(
            f"canonical item {item_id} 的 --canonical-step 必须是 {expected_step!r}"
        )
    # 退休项的版本只认 item 名里冻结的那一个：缺参数、写成别的版本或挂到非退休的
    # canonical-advance 上都拒绝，防止把冻结计划外的运行画像退休记到本项下。
    # canonical-import 是例外——它用 --retire-version 把退休项冻结进计划本身。
    declared_retire = _command_option(command, "--retire-version")
    if retire_version is not None:
        if declared_retire != retire_version:
            raise VCArtifactError(
                f"canonical item {item_id} 的 --retire-version 必须是 {retire_version!r}"
            )
    elif item_id == "canonical-import":
        if declared_retire is None or not VERSION_RE.fullmatch(declared_retire):
            raise VCArtifactError("canonical-import 动作 --retire-version 非法")
    elif declared_retire is not None:
        raise VCArtifactError(
            f"canonical item {item_id} 不接受 --retire-version"
        )
    # VC-6 三步只从已生成并可独立重放的收据推进，必须逐字带上它；VC-5 四步反之。
    step_receipt = _command_option(command, "--step-receipt")
    if group == "VC-6":
        if step_receipt is None or not PurePosixPath(step_receipt).is_absolute():
            raise VCArtifactError(
                f"canonical item {item_id} 必须带绝对路径的 --step-receipt"
            )
    elif step_receipt is not None:
        raise VCArtifactError(f"canonical item {item_id} 不接受 --step-receipt")
    if _command_option(command, "--supervisor-run-dir") is not None:
        raise VCArtifactError(
            "批次内 canonical 动作不得自带 --supervisor-run-dir；时间锚只能来自父监督器"
        )
    campaign_dir = _command_option(command, "--campaign-dir")
    if campaign_dir is None or not PurePosixPath(campaign_dir).is_absolute():
        raise VCArtifactError("canonical 动作必须带绝对路径的 --campaign-dir")
    candidate_id = _safe_id(
        _command_option(command, "--candidate-id"), "canonical 动作 --candidate-id"
    )
    attempt_id = _safe_id(
        _command_option(command, "--attempt-id"), "canonical 动作 --attempt-id"
    )
    phase = _command_option(command, "--phase")
    if phase is not None and phase not in VC_PHASES:
        raise VCArtifactError("canonical 动作 --phase 非法")
    if phase is not None and group is not None and phase != group:
        raise VCArtifactError(
            f"canonical item {item_id} 的 --phase 必须是 {group}"
        )
    approval = _command_option(command, "--approve-import-sha256")
    if item_id == "canonical-import":
        _sha256(approval, "canonical-import 动作 --approve-import-sha256")
    elif approval is not None:
        raise VCArtifactError("canonical-advance 动作不接受 --approve-import-sha256")
    return {
        "item_id": item_id,
        "group": group,
        "subcommand": expected_subcommand,
        "canonical_step": expected_step,
        "retire_version": retire_version,
        "step_receipt": step_receipt,
        "campaign_dir": campaign_dir,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "phase": phase,
    }


def canonical_batch_binding(
    actions: Sequence[Mapping[str, Any]],
    *,
    execute_item_ids: Sequence[str],
    phase: str | None = None,
) -> dict[str, Any] | None:
    """返回一个批次的 canonical 绑定；批次不含 canonical item 时返回 None。

    含 canonical item 的批次必须是纯 canonical 批次：execute 项全部落在冻结
    映射内，每项恰由一个动作承载，全部动作指向同一 Campaign 目录、Candidate
    与 attempt。VC-5 与 VC-6 是两个独立的冻结组，不得同批；给出 ``phase`` 时，
    该组还必须与批次阶段一致。
    """

    bindings = [
        binding
        for binding in (canonical_action_binding(action) for action in actions)
        if binding is not None
    ]
    canonical_execute = sorted(
        item for item in set(execute_item_ids) if is_canonical_item(item)
    )
    if not bindings and not canonical_execute:
        return None
    if sorted(set(execute_item_ids)) != canonical_execute:
        raise VCArtifactError("canonical 批次不得混入其它 execute 项")
    if sorted(binding["item_id"] for binding in bindings) != canonical_execute:
        raise VCArtifactError("canonical execute 项必须各由恰好一个冻结动作承载")
    groups = {canonical_item_phase(item) for item in canonical_execute}
    if len(groups) != 1:
        raise VCArtifactError("canonical 批次不得混合 VC-5 与 VC-6 的冻结项")
    group = next(iter(groups))
    if phase is not None and phase != group:
        raise VCArtifactError(f"{group} 的 canonical 项不得编入 {phase} 批次")
    expected_order = sorted(canonical_execute, key=_canonical_item_rank)
    if [binding["item_id"] for binding in bindings] != expected_order:
        raise VCArtifactError(
            "canonical 动作必须按冻结次序排列（VC-5：import → seal → compare → accept；"
            "VC-6：生产激活 → 回滚验证 → 退休；用带序号的 action_id）"
        )
    identities = {
        (binding["campaign_dir"], binding["candidate_id"], binding["attempt_id"])
        for binding in bindings
    }
    if len(identities) != 1:
        raise VCArtifactError("canonical 批次的全部动作必须指向同一 Campaign／Candidate／attempt")
    campaign_dir, candidate_id, attempt_id = next(iter(identities))
    phases = {binding["phase"] for binding in bindings if binding["phase"] is not None}
    if len(phases) > 1:
        raise VCArtifactError("canonical 批次的动作 --phase 不一致")
    return {
        "campaign_dir": campaign_dir,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "phase": next(iter(phases)) if phases else None,
        "group": group,
        "item_ids": canonical_execute,
    }


ACTION_FIELDS = frozenset({"action_id", "operation", "timeout_seconds", "command", "item_ids"})


def validate_output_bindings(value: Any, label: str) -> list[str]:
    """改造 5：动作声明的产物路径闭集——Campaign 相对、规范、去重、已排序、最多 64 项。

    失败关闭：未按字节序排列或有重复一律拒绝，不做任何归一化（操作员声明必须与编译侧
    冻结声明逐字相等，全链保持声明顺序）。
    """

    if not isinstance(value, list) or not value or len(value) > 64:
        raise VCArtifactError(f"{label} output_bindings 必须是 1～64 项的数组")
    paths = [_relative_path(item, f"{label} output_bindings") for item in value]
    if len(set(paths)) != len(paths):
        raise VCArtifactError(f"{label} output_bindings 不得重复")
    if paths != sorted(paths):
        raise VCArtifactError(f"{label} output_bindings 必须按字节序排列（不做归一化）")
    return list(paths)


def _actions(
    value: Any,
    *,
    execute_item_ids: Sequence[str],
    allow_output_bindings: bool = True,
    phase: str | None = None,
) -> list[dict[str, Any]]:
    """校验动作对 execute 项的无重叠完整覆盖，以及 canonical 项的冻结映射。

    改造 5：动作可携带可选 ``output_bindings``（v3 批次与 action plan 允许，v2／v1 批次不得出现）。
    """

    if not isinstance(value, list) or len(value) > 256:
        raise VCArtifactError("VC batch actions 必须是最多 256 项的数组")
    normalized: list[dict[str, Any]] = []
    action_ids: list[str] = []
    covered: list[str] = []
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, Mapping) or not (
            set(raw) == ACTION_FIELDS
            or (allow_output_bindings and set(raw) == ACTION_FIELDS | {"output_bindings"})
        ):
            raise VCArtifactError(f"VC batch action 第 {index} 项字段不闭合")
        action_id = _safe_id(raw.get("action_id"), f"action[{index}].action_id")
        operation = raw.get("operation")
        command = raw.get("command")
        item_ids = raw.get("item_ids")
        if not isinstance(operation, str) or not operation or len(operation) > 256:
            raise VCArtifactError(f"VC batch action {action_id} operation 非法")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 64
            or not all(isinstance(item, str) and item and len(item) <= 4096 for item in command)
        ):
            raise VCArtifactError(f"VC batch action {action_id} command 非法")
        if (
            not isinstance(item_ids, list)
            or not item_ids
            or item_ids != sorted(set(item_ids))
            or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in item_ids)
        ):
            raise VCArtifactError(f"VC batch action {action_id} item_ids 非法")
        normalized_action = {
            "action_id": action_id,
            "operation": operation,
            "timeout_seconds": _positive_seconds(
                raw.get("timeout_seconds"),
                f"action[{index}].timeout_seconds",
            ),
            "command": list(command),
            "item_ids": list(item_ids),
        }
        if "output_bindings" in raw:
            normalized_action["output_bindings"] = validate_output_bindings(
                raw.get("output_bindings"), f"VC batch action {action_id}"
            )
        normalized.append(normalized_action)
        action_ids.append(action_id)
        covered.extend(item_ids)
    if action_ids != sorted(set(action_ids)):
        raise VCArtifactError("VC batch actions 必须按 action_id 唯一排序")
    if len(covered) != len(set(covered)) or sorted(covered) != list(execute_item_ids):
        raise VCArtifactError("VC batch actions 未无重叠地精确覆盖 execute_item_ids")
    canonical_batch_binding(
        normalized, execute_item_ids=execute_item_ids, phase=phase
    )
    return normalized


def validate_action_plan(value: Any, *, phase: str | None = None) -> dict[str, Any]:
    """校验操作员为下一批次声明的 execute／reuse 和动作映射。

    给出 ``phase`` 时，canonical 项所属的冻结组必须与该批次阶段一致——编译入口
    一律传入，避免把 VC-5 的交接项编进 VC-6 批次（或反过来）。
    """

    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "execute_item_ids",
        "reuse_item_ids",
        "actions",
    }:
        raise VCArtifactError("VC action plan 字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != VC_ACTION_PLAN_SCHEMA:
        raise VCArtifactError("VC action plan schema_version 非法")
    for field in ("execute_item_ids", "reuse_item_ids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or values != sorted(set(values))
            or not all(isinstance(item, str) and SAFE_ID_RE.fullmatch(item) for item in values)
        ):
            raise VCArtifactError(f"VC action plan {field} 非法")
    if set(payload["execute_item_ids"]) & set(payload["reuse_item_ids"]):
        raise VCArtifactError("VC action plan execute/reuse 集合相交")
    payload["actions"] = _actions(
        payload.get("actions"),
        execute_item_ids=payload["execute_item_ids"],
        phase=phase,
    )
    return payload


def _absolute_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise VCArtifactError(f"{label} 必须是 POSIX 绝对路径")
    parsed = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in parsed.parts[1:]):
        raise VCArtifactError(f"{label} 路径不规范")
    return value


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VCArtifactError(f"{label} 必须是 POSIX 相对路径")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise VCArtifactError(f"{label} 路径不规范")
    return value


def build_discovery_inventory(
    *,
    campaign_id: str,
    target_version: str,
    source_diff: Mapping[str, Any],
    official_diff: Mapping[str, Any],
    source_diff_binding: Mapping[str, Any],
    official_diff_binding: Mapping[str, Any],
    evidence_manifest_binding: Mapping[str, Any],
) -> dict[str, Any]:
    """把源码和动态表面增删项冻结成 VC-1 DiscoveryInventory。"""

    items: list[dict[str, Any]] = []
    for source, payload in (("source", source_diff), ("dynamic", official_diff)):
        for change in ("added", "removed"):
            values = payload.get(change)
            if not isinstance(values, list):
                raise VCArtifactError(f"{source} {change} 发现项必须是数组")
            count = payload.get(f"{change}_count")
            if count is not None and count != len(values):
                raise VCArtifactError(f"{source} {change} 计数与数组不一致")
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise VCArtifactError(f"{source} {change} 发现项必须是对象")
                fingerprint = _sha256(raw.get("fingerprint"), "发现项 fingerprint")
                identity = {"source": source, "change": change, "fingerprint": fingerprint}
                items.append(
                    {
                        "discovery_id": f"discovery-{digest(identity)[:24]}",
                        **identity,
                        "fact": json.loads(json.dumps(dict(raw), ensure_ascii=False)),
                    }
                )
    items.sort(key=lambda item: (item["source"], item["change"], item["fingerprint"]))
    identities = [item["discovery_id"] for item in items]
    if len(identities) != len(set(identities)):
        raise VCArtifactError("DiscoveryInventory 存在重复身份")
    counts = {
        source: {
            change: sum(
                1
                for item in items
                if item["source"] == source and item["change"] == change
            )
            for change in ("added", "removed")
        }
        for source in ("source", "dynamic")
    }
    payload = {
        "schema_version": DISCOVERY_INVENTORY_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "target_version": _version(target_version, "target_version"),
        "truncated": False,
        "expected_item_count": len(items),
        "item_count": len(items),
        "counts": counts,
        "inputs": {
            "source_diff": _binding(source_diff_binding, "source_diff"),
            "official_diff": _binding(official_diff_binding, "official_diff"),
            "evidence_manifest": _binding(
                evidence_manifest_binding,
                "evidence_manifest",
            ),
        },
        "items": items,
    }
    payload["inventory_sha256"] = digest(payload)
    return validate_discovery_inventory(payload)


def validate_discovery_inventory(value: Any) -> dict[str, Any]:
    """失败关闭地复算 DiscoveryInventory 的完整性与自摘要。"""

    required = {
        "schema_version",
        "campaign_id",
        "target_version",
        "truncated",
        "expected_item_count",
        "item_count",
        "counts",
        "inputs",
        "items",
        "inventory_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("DiscoveryInventory 顶层字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != DISCOVERY_INVENTORY_SCHEMA:
        raise VCArtifactError("DiscoveryInventory schema_version 非法")
    _safe_id(payload.get("campaign_id"), "DiscoveryInventory.campaign_id")
    _version(payload.get("target_version"), "DiscoveryInventory.target_version")
    if payload.get("truncated") is not False:
        raise VCArtifactError("DiscoveryInventory 禁止截断")
    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "source_diff",
        "official_diff",
        "evidence_manifest",
    }:
        raise VCArtifactError("DiscoveryInventory inputs 不闭合")
    for name in sorted(inputs):
        _binding(inputs[name], f"DiscoveryInventory.inputs.{name}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise VCArtifactError("DiscoveryInventory.items 必须是数组")
    identities: list[str] = []
    expected_order: list[tuple[str, str, str]] = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, Mapping) or set(item) != {
            "discovery_id",
            "source",
            "change",
            "fingerprint",
            "fact",
        }:
            raise VCArtifactError(f"DiscoveryInventory 第 {index} 项字段不闭合")
        source = item.get("source")
        change = item.get("change")
        fingerprint = _sha256(item.get("fingerprint"), "发现项 fingerprint")
        if source not in {"source", "dynamic"} or change not in {"added", "removed"}:
            raise VCArtifactError(f"DiscoveryInventory 第 {index} 项来源或变化非法")
        if not isinstance(item.get("fact"), Mapping) or item["fact"].get("fingerprint") != fingerprint:
            raise VCArtifactError(f"DiscoveryInventory 第 {index} 项事实摘要不一致")
        expected_id = f"discovery-{digest({'source': source, 'change': change, 'fingerprint': fingerprint})[:24]}"
        if item.get("discovery_id") != expected_id:
            raise VCArtifactError(f"DiscoveryInventory 第 {index} 项身份不一致")
        identities.append(expected_id)
        expected_order.append((str(source), str(change), fingerprint))
    if expected_order != sorted(expected_order) or len(identities) != len(set(identities)):
        raise VCArtifactError("DiscoveryInventory 未排序或存在重复项")
    if (
        payload.get("item_count") != len(items)
        or payload.get("expected_item_count") != len(items)
    ):
        raise VCArtifactError("DiscoveryInventory 计数未闭合")
    expected_counts = {
        source: {
            change: sum(
                1
                for item in items
                if item["source"] == source and item["change"] == change
            )
            for change in ("added", "removed")
        }
        for source in ("source", "dynamic")
    }
    if payload.get("counts") != expected_counts:
        raise VCArtifactError("DiscoveryInventory 分组计数不一致")
    recorded = _sha256(payload.get("inventory_sha256"), "DiscoveryInventory.inventory_sha256")
    unsigned = dict(payload)
    unsigned.pop("inventory_sha256")
    if digest(unsigned) != recorded:
        raise VCArtifactError("DiscoveryInventory 自摘要不一致")
    return payload


def build_gate_requirements(
    *,
    campaign_id: str,
    target_version: str,
    joint_manifest_sha256: str,
    affected_rule_ids: Sequence[str],
    inherited_rule_ids: Sequence[str],
    migration_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """从本轮批准规则分区派生动态 post-promotion 门禁需求。"""

    affected = sorted(set(affected_rule_ids))
    inherited = sorted(set(inherited_rule_ids))
    _rule_ids(affected, "affected_rule_ids")
    _rule_ids(inherited, "inherited_rule_ids")
    if set(affected) & set(inherited):
        raise VCArtifactError("affected 与 inherited 规则集合相交")
    requirements = [
        {
            "gate_id": f"affected-{rule_id.lower()}",
            "kind": "affected_rule",
            "rule_id": rule_id,
            "required_test_semantic": f"在晋升后的目标制品上重放 {rule_id} 的批准断言语义",
        }
        for rule_id in affected
    ]
    requirements.extend(
        {
            "gate_id": gate_id,
            "kind": "public",
            "rule_id": None,
            "required_test_semantic": semantic,
        }
        for gate_id, semantic in PUBLIC_GATES
    )
    requirements.sort(key=lambda item: item["gate_id"])
    payload = {
        "schema_version": GATE_REQUIREMENTS_SCHEMA,
        "campaign_id": _safe_id(campaign_id, "campaign_id"),
        "target_version": _version(target_version, "target_version"),
        "joint_manifest_sha256": _sha256(
            joint_manifest_sha256,
            "joint_manifest_sha256",
        ),
        "migration_manifest": _binding(migration_manifest, "migration_manifest"),
        "affected_rule_ids": affected,
        "inherited_rule_ids": inherited,
        "requirements": requirements,
        "requirement_count": len(requirements),
    }
    payload["requirements_sha256"] = digest(payload)
    return validate_gate_requirements(payload)


def validate_gate_requirements(value: Any) -> dict[str, Any]:
    """复算动态门禁需求及其规则闭集。"""

    required = {
        "schema_version",
        "campaign_id",
        "target_version",
        "joint_manifest_sha256",
        "migration_manifest",
        "affected_rule_ids",
        "inherited_rule_ids",
        "requirements",
        "requirement_count",
        "requirements_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("post-promotion 门禁需求字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != GATE_REQUIREMENTS_SCHEMA:
        raise VCArtifactError("post-promotion 门禁需求 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "gate requirements campaign_id")
    _version(payload.get("target_version"), "gate requirements target_version")
    _sha256(payload.get("joint_manifest_sha256"), "joint_manifest_sha256")
    _binding(payload.get("migration_manifest"), "migration_manifest")
    affected = _rule_ids(payload.get("affected_rule_ids"), "affected_rule_ids")
    inherited = _rule_ids(payload.get("inherited_rule_ids"), "inherited_rule_ids")
    if set(affected) & set(inherited):
        raise VCArtifactError("门禁需求规则分区相交")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        raise VCArtifactError("requirements 必须是数组")
    gate_ids: list[str] = []
    affected_from_rows: list[str] = []
    for index, row in enumerate(requirements, 1):
        if not isinstance(row, Mapping) or set(row) != {
            "gate_id",
            "kind",
            "rule_id",
            "required_test_semantic",
        }:
            raise VCArtifactError(f"门禁需求第 {index} 项字段不闭合")
        gate_id = _safe_id(row.get("gate_id"), f"门禁需求第 {index} 项 gate_id")
        semantic = row.get("required_test_semantic")
        if not isinstance(semantic, str) or not semantic.strip():
            raise VCArtifactError(f"门禁需求第 {index} 项测试语义为空")
        if row.get("kind") == "affected_rule":
            rule_id = row.get("rule_id")
            if not isinstance(rule_id, str) or not RULE_RE.fullmatch(rule_id):
                raise VCArtifactError(f"门禁需求第 {index} 项规则编号非法")
            if gate_id != f"affected-{rule_id.lower()}":
                raise VCArtifactError(f"门禁需求第 {index} 项 gate_id 与规则不一致")
            affected_from_rows.append(rule_id)
        elif row.get("kind") == "public":
            if row.get("rule_id") is not None:
                raise VCArtifactError(f"公共门禁第 {index} 项不得绑定规则编号")
        else:
            raise VCArtifactError(f"门禁需求第 {index} 项 kind 非法")
        gate_ids.append(gate_id)
    expected_public = dict(PUBLIC_GATES)
    actual_public = {
        row["gate_id"]: row["required_test_semantic"]
        for row in requirements
        if row.get("kind") == "public"
    }
    if (
        affected_from_rows != affected
        or actual_public != expected_public
        or gate_ids != sorted(gate_ids)
        or len(gate_ids) != len(set(gate_ids))
        or payload.get("requirement_count") != len(requirements)
    ):
        raise VCArtifactError("post-promotion 门禁需求集合未闭合")
    recorded = _sha256(payload.get("requirements_sha256"), "requirements_sha256")
    unsigned = dict(payload)
    unsigned.pop("requirements_sha256")
    if digest(unsigned) != recorded:
        raise VCArtifactError("post-promotion 门禁需求自摘要不一致")
    return payload


def build_gate_plan(
    requirements: Mapping[str, Any],
    mapping: Mapping[str, Any],
    *,
    mapping_sha256: str | None = None,
) -> dict[str, Any]:
    """把 VC-3 需求与 VC-4 的唯一测试和字面命令绑定为执行计划。"""

    requirements_payload = validate_gate_requirements(requirements)
    if not isinstance(mapping, Mapping) or set(mapping) != {
        "schema_version",
        "requirements_sha256",
        "gates",
    }:
        raise VCArtifactError("门禁映射字段不闭合")
    if mapping.get("schema_version") != GATE_MAPPING_SCHEMA:
        raise VCArtifactError("门禁映射 schema_version 非法")
    if mapping.get("requirements_sha256") != requirements_payload["requirements_sha256"]:
        raise VCArtifactError("门禁映射未绑定本轮 VC-3 需求")
    expected = {
        row["gate_id"]: row for row in requirements_payload["requirements"]
    }
    raw_gates = mapping.get("gates")
    if not isinstance(raw_gates, list):
        raise VCArtifactError("门禁映射 gates 必须是数组")
    gates: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_tests: set[str] = set()
    seen_commands: set[tuple[str, tuple[str, ...]]] = set()
    for index, row in enumerate(raw_gates, 1):
        if not isinstance(row, Mapping) or set(row) != {
            "gate_id",
            "test_id",
            "working_directory",
            "command",
            "requirement_sha256",
        }:
            raise VCArtifactError(f"门禁映射第 {index} 项字段不闭合")
        gate_id = _safe_id(row.get("gate_id"), f"门禁映射第 {index} 项 gate_id")
        test_id = _safe_id(row.get("test_id"), f"门禁映射第 {index} 项 test_id")
        command = row.get("command")
        working_directory = row.get("working_directory")
        command_identity = (
            str(working_directory),
            tuple(command) if isinstance(command, list) else (),
        )
        if (
            gate_id in seen
            or gate_id not in expected
            or test_id in seen_tests
            or command_identity in seen_commands
            or working_directory not in {".", "backend"}
            or not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
            or row.get("requirement_sha256") != digest(expected[gate_id])
        ):
            raise VCArtifactError(
                f"门禁映射第 {index} 项身份、需求摘要、目录或命令非法"
            )
        seen.add(gate_id)
        seen_tests.add(test_id)
        seen_commands.add(command_identity)
        gates.append(
            {
                "gate_id": gate_id,
                "test_id": test_id,
                "working_directory": working_directory,
                "command": list(command),
                "requirement_sha256": digest(expected[gate_id]),
            }
        )
    if seen != set(expected):
        raise VCArtifactError(
            "门禁映射未精确覆盖 VC-3 需求："
            f"缺少={sorted(set(expected) - seen)}，多余={sorted(seen - set(expected))}"
        )
    gates.sort(key=lambda item: item["gate_id"])
    plan = {
        "schema_version": GATE_PLAN_SCHEMA,
        "campaign_id": requirements_payload["campaign_id"],
        "target_version": requirements_payload["target_version"],
        "joint_manifest_sha256": requirements_payload["joint_manifest_sha256"],
        "requirements_sha256": requirements_payload["requirements_sha256"],
        "mapping_sha256": _sha256(
            mapping_sha256 or digest(mapping),
            "gate mapping sha256",
        ),
        "affected_rule_ids": requirements_payload["affected_rule_ids"],
        "inherited_rule_ids": requirements_payload["inherited_rule_ids"],
        "gates": gates,
        "gate_count": len(gates),
    }
    plan["plan_sha256"] = digest(plan)
    return validate_gate_plan(plan, requirements_payload)


def validate_gate_plan(
    value: Any,
    requirements: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """复算 VC-4 门禁执行计划；可选地与 VC-3 需求交叉验证。"""

    required = {
        "schema_version",
        "campaign_id",
        "target_version",
        "joint_manifest_sha256",
        "requirements_sha256",
        "mapping_sha256",
        "affected_rule_ids",
        "inherited_rule_ids",
        "gates",
        "gate_count",
        "plan_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("post-promotion 门禁计划字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != GATE_PLAN_SCHEMA:
        raise VCArtifactError("post-promotion 门禁计划 schema_version 非法")
    _safe_id(payload.get("campaign_id"), "gate plan campaign_id")
    _version(payload.get("target_version"), "gate plan target_version")
    _sha256(payload.get("joint_manifest_sha256"), "gate plan joint_manifest_sha256")
    _sha256(payload.get("requirements_sha256"), "gate plan requirements_sha256")
    _sha256(payload.get("mapping_sha256"), "gate plan mapping_sha256")
    affected = _rule_ids(payload.get("affected_rule_ids"), "gate plan affected_rule_ids")
    inherited = _rule_ids(payload.get("inherited_rule_ids"), "gate plan inherited_rule_ids")
    gates = payload.get("gates")
    if not isinstance(gates, list):
        raise VCArtifactError("gate plan gates 必须是数组")
    gate_ids: list[str] = []
    test_ids: list[str] = []
    command_ids: list[tuple[str, tuple[str, ...]]] = []
    for index, row in enumerate(gates, 1):
        if not isinstance(row, Mapping) or set(row) != {
            "gate_id",
            "test_id",
            "working_directory",
            "command",
            "requirement_sha256",
        }:
            raise VCArtifactError(f"gate plan 第 {index} 项字段不闭合")
        gate_ids.append(_safe_id(row.get("gate_id"), f"gate plan 第 {index} 项 gate_id"))
        test_ids.append(
            _safe_id(row.get("test_id"), f"gate plan 第 {index} 项 test_id")
        )
        _sha256(row.get("requirement_sha256"), f"gate plan 第 {index} 项 requirement_sha256")
        if (
            row.get("working_directory") not in {".", "backend"}
            or not isinstance(row.get("command"), list)
            or not row["command"]
            or not all(isinstance(item, str) and item for item in row["command"])
        ):
            raise VCArtifactError(f"gate plan 第 {index} 项命令非法")
        command_ids.append((str(row["working_directory"]), tuple(row["command"])))
    if (
        gate_ids != sorted(gate_ids)
        or len(gate_ids) != len(set(gate_ids))
        or len(test_ids) != len(set(test_ids))
        or len(command_ids) != len(set(command_ids))
        or payload.get("gate_count") != len(gates)
    ):
        raise VCArtifactError("gate plan 未排序、重复或计数不一致")
    if requirements is not None:
        requirement_payload = validate_gate_requirements(requirements)
        expected = {
            row["gate_id"]: row for row in requirement_payload["requirements"]
        }
        if (
            payload["campaign_id"] != requirement_payload["campaign_id"]
            or payload["target_version"] != requirement_payload["target_version"]
            or payload["joint_manifest_sha256"] != requirement_payload["joint_manifest_sha256"]
            or payload["requirements_sha256"] != requirement_payload["requirements_sha256"]
            or affected != requirement_payload["affected_rule_ids"]
            or inherited != requirement_payload["inherited_rule_ids"]
            or set(gate_ids) != set(expected)
            or any(
                row["requirement_sha256"] != digest(expected[row["gate_id"]])
                for row in gates
            )
        ):
            raise VCArtifactError("gate plan 未精确绑定 VC-3 需求")
    recorded = _sha256(payload.get("plan_sha256"), "gate plan plan_sha256")
    unsigned = dict(payload)
    unsigned.pop("plan_sha256")
    if digest(unsigned) != recorded:
        raise VCArtifactError("gate plan 自摘要不一致")
    return payload


def build_candidate_build_receipt(
    *,
    campaign_id: str,
    campaign_manifest_sha256: str,
    candidate_id: str,
    candidate_purpose: str,
    target_version: str,
    deployed_version: str,
    target_architecture: str,
    source: Mapping[str, Any],
    binary: Mapping[str, Any],
    image: Mapping[str, Any],
    build_id: str,
    build_parameters: Mapping[str, Any],
    profile: Mapping[str, Any],
    catalog_stage: Mapping[str, Any],
    source_transition: Mapping[str, Any],
    gate_requirements: Mapping[str, Any],
    gate_plan: Mapping[str, Any],
    implementation_tests: Mapping[str, Any],
    build_inventory: Mapping[str, Any],
    frontend_provenance: Mapping[str, Any],
    image_inspection: Mapping[str, Any],
    capability_probe: Mapping[str, Any],
    built_at_utc: str,
    build_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成 VC-4 Candidate 构建收据并冻结完整身份。"""

    payload = {
        "schema_version": CANDIDATE_BUILD_SCHEMA,
        "status": "complete",
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "candidate_id": candidate_id,
        "candidate_purpose": candidate_purpose,
        "target_version": target_version,
        "deployed_version": deployed_version,
        "target_architecture": target_architecture,
        "source": dict(source),
        "binary": dict(binary),
        "image": dict(image),
        "build": {
            "build_id": build_id,
            "parameters": json.loads(
                json.dumps(dict(build_parameters), ensure_ascii=False)
            ),
            "parameters_sha256": digest(build_parameters),
        },
        "profile": dict(profile),
        "catalog_stage": dict(catalog_stage),
        "source_transition": dict(source_transition),
        "gate_requirements": dict(gate_requirements),
        "gate_plan": dict(gate_plan),
        "implementation_tests": dict(implementation_tests),
        "build_inventory": dict(build_inventory),
        "frontend_provenance": dict(frontend_provenance),
        "image_inspection": dict(image_inspection),
        "capability_probe": dict(capability_probe),
        "built_at_utc": built_at_utc,
    }
    if build_inputs is not None:
        payload["build"]["inputs"] = dict(build_inputs)
    payload["receipt_digest"] = digest(payload)
    return validate_candidate_build_receipt(payload)


def validate_candidate_build_receipt(
    value: Any,
    *,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """校验 VC-4 构建收据的完整 Candidate 身份。

    v1 缺少四份机器实物收据，默认必须拒绝。只有调用方已经证明它来自
    受管的历史投影路径时，才可显式开启只读兼容。
    """

    required = {
        "schema_version",
        "status",
        "campaign_id",
        "campaign_manifest_sha256",
        "candidate_id",
        "candidate_purpose",
        "target_version",
        "deployed_version",
        "target_architecture",
        "source",
        "binary",
        "image",
        "build",
        "profile",
        "catalog_stage",
        "source_transition",
        "gate_requirements",
        "gate_plan",
        "implementation_tests",
        "build_inventory",
        "frontend_provenance",
        "image_inspection",
        "capability_probe",
        "built_at_utc",
        "receipt_digest",
    }
    if not isinstance(value, Mapping):
        raise VCArtifactError("Candidate 构建收据字段不闭合")
    schema_version = value.get("schema_version")
    if schema_version == LEGACY_CANDIDATE_BUILD_SCHEMA and not allow_legacy:
        raise VCArtifactError("Candidate v1 构建收据只允许受管历史投影重放")
    machine_fields = {
        "build_inventory",
        "frontend_provenance",
        "image_inspection",
        "capability_probe",
    }
    expected_fields = (
        required - machine_fields
        if schema_version == LEGACY_CANDIDATE_BUILD_SCHEMA
        else required
    )
    if set(value) != expected_fields:
        raise VCArtifactError("Candidate 构建收据字段不闭合")
    payload = dict(value)
    supported_schemas = {CANDIDATE_BUILD_SCHEMA}
    if allow_legacy:
        supported_schemas.add(LEGACY_CANDIDATE_BUILD_SCHEMA)
    if schema_version not in supported_schemas or payload.get("status") != "complete":
        raise VCArtifactError("Candidate 构建收据 schema 或状态非法")
    for field in ("campaign_id", "candidate_id"):
        _safe_id(payload.get(field), f"Candidate 构建收据 {field}")
    _sha256(payload.get("campaign_manifest_sha256"), "campaign_manifest_sha256")
    if payload.get("candidate_purpose") not in {"validation_only", "production_replacement"}:
        raise VCArtifactError("Candidate 构建用途非法")
    target_version = _version(payload.get("target_version"), "Candidate target_version")
    if payload.get("deployed_version") != target_version:
        raise VCArtifactError("Candidate deployed_version 必须等于目标版本")
    if not isinstance(payload.get("target_architecture"), str) or not ARCHITECTURE_RE.fullmatch(payload["target_architecture"]):
        raise VCArtifactError("Candidate 目标架构非法")
    source = payload.get("source")
    binary = payload.get("binary")
    image = payload.get("image")
    build = payload.get("build")
    profile = payload.get("profile")
    catalog = payload.get("catalog_stage")
    transition = payload.get("source_transition")
    gate_requirements = payload.get("gate_requirements")
    gate_plan = payload.get("gate_plan")
    implementation_tests = payload.get("implementation_tests")
    machine_receipts = {
        name: payload.get(name)
        for name in machine_fields
        if name in payload
    }
    if not isinstance(source, Mapping) or set(source) != {"root", "tree_sha256", "git_commit"}:
        raise VCArtifactError("Candidate source 身份不闭合")
    _sha256(source.get("tree_sha256"), "Candidate source tree_sha256")
    _absolute_path(source.get("root"), "Candidate source root")
    if (
        not isinstance(source.get("git_commit"), str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", source["git_commit"])
    ):
        raise VCArtifactError("Candidate git_commit 为空")
    if not isinstance(binary, Mapping) or set(binary) != {"path", "sha256", "bytes"}:
        raise VCArtifactError("Candidate binary 身份不闭合")
    _binding(binary, "Candidate binary", bytes_required=True)
    _absolute_path(binary.get("path"), "Candidate binary.path")
    if not isinstance(image, Mapping) or set(image) != {"reference", "manifest_digest", "image_id"}:
        raise VCArtifactError("Candidate image 身份不闭合")
    manifest_digest = image.get("manifest_digest")
    image_id = image.get("image_id")
    reference = image.get("reference")
    if not isinstance(manifest_digest, str) or not IMAGE_ID_RE.fullmatch(manifest_digest):
        raise VCArtifactError("Candidate image manifest digest 非法")
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        raise VCArtifactError("Candidate image id 非法")
    if (
        not isinstance(reference, str)
        or not IMAGE_REFERENCE_RE.fullmatch(reference)
        or not reference.endswith(f"@{manifest_digest}")
    ):
        raise VCArtifactError("Candidate image reference 非法")
    if not isinstance(build, Mapping) or set(build) not in (
        {"build_id", "parameters", "parameters_sha256"}, {"build_id", "parameters", "parameters_sha256", "inputs"}
    ):
        raise VCArtifactError("Candidate build 身份不闭合")
    _safe_id(build.get("build_id"), "Candidate build_id")
    if not isinstance(build.get("parameters"), Mapping) or digest(build["parameters"]) != build.get("parameters_sha256"):
        raise VCArtifactError("Candidate build parameters 摘要不一致")
    if not isinstance(profile, Mapping) or set(profile) != {"profile_id", "profile_digest", "derivation_receipt_sha256"}:
        raise VCArtifactError("Candidate profile 身份不闭合")
    _safe_id(profile.get("profile_id"), "Candidate profile_id")
    _sha256(profile.get("profile_digest"), "Candidate profile_digest")
    _sha256(profile.get("derivation_receipt_sha256"), "profile derivation receipt")
    expected_binding_fields = {
        "catalog_stage": {"path", "sha256", "catalog_tree_sha256"},
        "source_transition": {"path", "sha256"},
        "gate_requirements": {"path", "sha256", "requirements_sha256"},
        "gate_plan": {
            "path",
            "sha256",
            "plan_sha256",
            "requirements_sha256",
        },
    }
    for name, value in (
        ("catalog_stage", catalog),
        ("source_transition", transition),
        ("gate_requirements", gate_requirements),
        ("gate_plan", gate_plan),
    ):
        if not isinstance(value, Mapping) or set(value) != expected_binding_fields[name]:
            raise VCArtifactError(f"Candidate {name} 绑定不闭合")
        if not isinstance(value.get("path"), str) or not value["path"]:
            raise VCArtifactError(f"Candidate {name}.path 为空")
        _sha256(value.get("sha256"), f"Candidate {name}.sha256")
    for name, value in (
        ("catalog_stage", catalog),
        ("source_transition", transition),
        ("gate_plan", gate_plan),
    ):
        _absolute_path(value.get("path"), f"Candidate {name}.path")
    _relative_path(
        gate_requirements.get("path"),
        "Candidate gate_requirements.path",
    )
    if not isinstance(catalog.get("catalog_tree_sha256"), str) or not SHA256_RE.fullmatch(catalog["catalog_tree_sha256"]):
        raise VCArtifactError("Candidate catalog tree 摘要非法")
    _sha256(gate_requirements.get("requirements_sha256"), "Candidate gate requirements")
    _sha256(gate_plan.get("plan_sha256"), "Candidate gate plan")
    _sha256(gate_plan.get("requirements_sha256"), "Candidate gate plan requirements")
    if gate_plan["requirements_sha256"] != gate_requirements["requirements_sha256"]:
        raise VCArtifactError("Candidate gate plan 未绑定同一份 VC-3 门禁需求")
    if not isinstance(implementation_tests, Mapping) or set(implementation_tests) != {
        "evidence_root",
        "receipt",
        "receipt_digest",
    }:
        raise VCArtifactError("Candidate implementation_tests 绑定不闭合")
    _absolute_path(
        implementation_tests.get("evidence_root"),
        "Candidate implementation_tests.evidence_root",
    )
    receipt_binding = _binding(
        implementation_tests.get("receipt"),
        "Candidate implementation_tests.receipt",
        bytes_required=True,
    )
    _relative_path(
        receipt_binding["path"],
        "Candidate implementation_tests.receipt.path",
    )
    _sha256(
        implementation_tests.get("receipt_digest"),
        "Candidate implementation_tests.receipt_digest",
    )
    if "inputs" in build:
        # 承接收据必须与本构建收据绑定同一组输入。此检查位于 control 制品层，
        # 既有 evidence 读侧仍要求当前 Candidate 和原始实现测试证据完整可重放。
        from . import codex_upgrade_candidate_build as candidate_build
        from . import codex_upgrade_vc_receipt as vc_receipt
        try:
            inputs = candidate_build.validate_implementation_inputs(build["inputs"])
            implementation = vc_receipt.validate_build_input_binding(implementation_tests, inputs)
            if (inputs["source_tree_sha256"] != source["tree_sha256"]
                    or inputs["target_architecture"] != payload["target_architecture"]
                    or inputs["requirements_sha256"] != gate_requirements["requirements_sha256"]
                    or inputs["parameters_sha256"] != digest(candidate_build.implementation_parameter_projection(build["parameters"]))
                    or inputs["go_version"] != build["parameters"]["input_provenance"]["go_version"]
                    or inputs["base_images"] != build["parameters"]["input_provenance"]["base_images"]
                    or inputs["node_version"] != build["parameters"]["frontend"]["node_version"]
                    or inputs["pnpm_version"] != build["parameters"]["frontend"]["pnpm_version"]
                    or implementation["subject"]["candidate_id"] != payload["candidate_id"]):
                raise VCArtifactError("实现测试构建输入与当前构建身份不一致")
        except (candidate_build.CandidateBuildError, vc_receipt.VCReceiptError, OSError, KeyError) as error:
            raise VCArtifactError(f"实现测试输入绑定未通过：{error}") from error
    elif "input_provenance" in build["parameters"]:
        raise VCArtifactError("新版构建参数缺少实现测试输入证明")
    for name, machine_receipt in machine_receipts.items():
        if not isinstance(machine_receipt, Mapping) or set(machine_receipt) != {
            "path",
            "sha256",
            "bytes",
            "receipt_digest",
        }:
            raise VCArtifactError(f"Candidate {name} 机器收据绑定不闭合")
        _relative_path(machine_receipt.get("path"), f"Candidate {name}.path")
        _sha256(machine_receipt.get("sha256"), f"Candidate {name}.sha256")
        if not isinstance(machine_receipt.get("bytes"), int) or machine_receipt["bytes"] <= 0:
            raise VCArtifactError(f"Candidate {name}.bytes 非法")
        _sha256(
            machine_receipt.get("receipt_digest"),
            f"Candidate {name}.receipt_digest",
        )
    _timestamp(payload.get("built_at_utc"), "Candidate built_at_utc")
    recorded = _sha256(payload.get("receipt_digest"), "Candidate receipt_digest")
    unsigned = dict(payload)
    unsigned.pop("receipt_digest")
    if digest(unsigned) != recorded:
        raise VCArtifactError("Candidate 构建收据自摘要不一致")
    return payload


def build_candidate_delivery_receipt(
    *,
    campaign_id: str,
    campaign_manifest_sha256: str,
    campaign_purpose: str,
    candidate_id: str,
    attempt_id: str,
    target_version: str,
    candidate_identity_sha256: str,
    build_receipt: Mapping[str, Any],
    acceptance_fact: Mapping[str, Any],
    canonical_checkpoint: Mapping[str, Any] | None,
    production_state: str,
    issued_at_utc: str,
) -> dict[str, Any]:
    """生成 VC-6 候选交付或生产恢复终态收据。"""

    release_state = (
        "ready_for_operator_release"
        if campaign_purpose == "validation_only"
        else "production_active_restored"
    )
    payload = {
        "schema_version": CANDIDATE_DELIVERY_SCHEMA,
        "status": "complete",
        "release_state": release_state,
        "campaign_id": campaign_id,
        "campaign_manifest_sha256": campaign_manifest_sha256,
        "campaign_purpose": campaign_purpose,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "target_version": target_version,
        "candidate_identity_sha256": candidate_identity_sha256,
        "build_receipt": dict(build_receipt),
        "acceptance_fact": dict(acceptance_fact),
        "canonical_checkpoint": (
            dict(canonical_checkpoint)
            if canonical_checkpoint is not None
            else None
        ),
        "production_state": production_state,
        "metrics": {"scanned_bytes": 0, "live_request_count": 0},
        "issued_at_utc": issued_at_utc,
    }
    payload["receipt_digest"] = digest(payload)
    return validate_candidate_delivery_receipt(payload)


def validate_candidate_delivery_receipt(value: Any) -> dict[str, Any]:
    """校验 VC-6 交付收据的终点语义和自摘要。"""

    required = {
        "schema_version",
        "status",
        "release_state",
        "campaign_id",
        "campaign_manifest_sha256",
        "campaign_purpose",
        "candidate_id",
        "attempt_id",
        "target_version",
        "candidate_identity_sha256",
        "build_receipt",
        "acceptance_fact",
        "canonical_checkpoint",
        "production_state",
        "metrics",
        "issued_at_utc",
        "receipt_digest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("Candidate 交付收据字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_DELIVERY_SCHEMA or payload.get("status") != "complete":
        raise VCArtifactError("Candidate 交付收据 schema 或状态非法")
    purpose = payload.get("campaign_purpose")
    if purpose not in {"validation_only", "production_replacement"}:
        raise VCArtifactError("Candidate 交付用途非法")
    expected_state = (
        "ready_for_operator_release"
        if purpose == "validation_only"
        else "production_active_restored"
    )
    if payload.get("release_state") != expected_state:
        raise VCArtifactError("Candidate 交付终点与用途不一致")
    for field in ("campaign_id", "candidate_id", "attempt_id"):
        _safe_id(payload.get(field), f"Candidate 交付 {field}")
    _version(payload.get("target_version"), "Candidate 交付 target_version")
    for field in ("campaign_manifest_sha256", "candidate_identity_sha256"):
        _sha256(payload.get(field), f"Candidate 交付 {field}")
    build_binding = _binding(
        payload.get("build_receipt"),
        "Candidate build receipt",
        bytes_required=True,
    )
    acceptance_binding = _binding(
        payload.get("acceptance_fact"),
        "AcceptanceFact",
        bytes_required=True,
    )
    _relative_path(build_binding["path"], "Candidate build receipt.path")
    _relative_path(acceptance_binding["path"], "AcceptanceFact.path")
    checkpoint = payload.get("canonical_checkpoint")
    if purpose == "validation_only":
        if checkpoint is not None or payload.get("production_state") != "accepted_not_activated":
            raise VCArtifactError("validation_only 交付不得伪装生产状态")
    else:
        checkpoint_binding = _binding(
            checkpoint,
            "canonical checkpoint",
            bytes_required=True,
        )
        _relative_path(checkpoint_binding["path"], "canonical checkpoint.path")
        if payload.get("production_state") != "restored_active":
            raise VCArtifactError("production_replacement 缺少 restored_active 终态")
    metrics = payload.get("metrics")
    if metrics != {"scanned_bytes": 0, "live_request_count": 0}:
        raise VCArtifactError("Candidate 交付必须是零扫描、零请求重放")
    _timestamp(payload.get("issued_at_utc"), "Candidate 交付 issued_at_utc")
    recorded = _sha256(payload.get("receipt_digest"), "Candidate delivery receipt_digest")
    unsigned = dict(payload)
    unsigned.pop("receipt_digest")
    if digest(unsigned) != recorded:
        raise VCArtifactError("Candidate 交付收据自摘要不一致")
    return payload
