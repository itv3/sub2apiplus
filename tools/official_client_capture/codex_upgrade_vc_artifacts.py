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
VC_BATCH_SCHEMA = "codex-upgrade-vc-batch/v1"
VC_ACTION_PLAN_SCHEMA = "codex-upgrade-vc-action-plan/v1"
GATE_REQUIREMENTS_SCHEMA = "codex-post-promotion-gate-requirements/v1"
GATE_MAPPING_SCHEMA = "codex-post-promotion-gate-mapping/v2"
GATE_PLAN_SCHEMA = "codex-post-promotion-gate-plan/v1"
CANDIDATE_BUILD_SCHEMA = "codex-upgrade-candidate-build-receipt/v1"
CANDIDATE_DELIVERY_SCHEMA = "codex-upgrade-candidate-delivery-receipt/v1"

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
) -> dict[str, Any]:
    """冻结 VC-0 可知的总计划；禁止预填未来阶段才会产生的身份。"""

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
    payload["plan_sha256"] = digest(payload)
    return validate_campaign_plan(payload)


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
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("Campaign 总计划字段不闭合")
    payload = dict(value)
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
) -> dict[str, Any]:
    """从前序 checkpoint 编译同一 Campaign 的一个不可变执行批次。"""

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
        "actions": [json.loads(json.dumps(dict(item), ensure_ascii=False)) for item in actions],
        "compiled_at_utc": _timestamp(compiled_at_utc, "compiled_at_utc"),
        "must_start_by_utc": _timestamp(must_start_by_utc, "must_start_by_utc"),
        "original_deadline_at_utc": plan["original_deadline_at_utc"],
    }
    payload["batch_sha256"] = digest(payload)
    return validate_vc_batch(payload, plan)


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
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("VC batch 字段不闭合")
    payload = dict(value)
    sequence = payload.get("sequence")
    phase = payload.get("phase")
    if (
        payload.get("schema_version") != VC_BATCH_SCHEMA
        or phase not in VC_PHASES[1:]
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


def _actions(
    value: Any,
    *,
    execute_item_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """校验动作对 execute 项的无重叠完整覆盖。"""

    if not isinstance(value, list) or len(value) > 256:
        raise VCArtifactError("VC batch actions 必须是最多 256 项的数组")
    normalized: list[dict[str, Any]] = []
    action_ids: list[str] = []
    covered: list[str] = []
    for index, raw in enumerate(value, 1):
        if not isinstance(raw, Mapping) or set(raw) != {
            "action_id",
            "operation",
            "timeout_seconds",
            "command",
            "item_ids",
        }:
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
        normalized.append(
            {
                "action_id": action_id,
                "operation": operation,
                "timeout_seconds": _positive_seconds(
                    raw.get("timeout_seconds"),
                    f"action[{index}].timeout_seconds",
                ),
                "command": list(command),
                "item_ids": list(item_ids),
            }
        )
        action_ids.append(action_id)
        covered.extend(item_ids)
    if action_ids != sorted(set(action_ids)):
        raise VCArtifactError("VC batch actions 必须按 action_id 唯一排序")
    if len(covered) != len(set(covered)) or sorted(covered) != list(execute_item_ids):
        raise VCArtifactError("VC batch actions 未无重叠地精确覆盖 execute_item_ids")
    return normalized


def validate_action_plan(value: Any) -> dict[str, Any]:
    """校验操作员为下一批次声明的 execute／reuse 和动作映射。"""

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
    built_at_utc: str,
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
        "built_at_utc": built_at_utc,
    }
    payload["receipt_digest"] = digest(payload)
    return validate_candidate_build_receipt(payload)


def validate_candidate_build_receipt(value: Any) -> dict[str, Any]:
    """校验 VC-4 构建收据的完整 Candidate 身份。"""

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
        "built_at_utc",
        "receipt_digest",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise VCArtifactError("Candidate 构建收据字段不闭合")
    payload = dict(value)
    if payload.get("schema_version") != CANDIDATE_BUILD_SCHEMA or payload.get("status") != "complete":
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
    if not isinstance(build, Mapping) or set(build) != {"build_id", "parameters", "parameters_sha256"}:
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
