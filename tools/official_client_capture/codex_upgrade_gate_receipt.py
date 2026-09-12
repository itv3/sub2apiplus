#!/usr/bin/env python3
"""从外部门禁执行事实生成可重放、不可覆盖的 Codex 升级门禁收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture import codex_upgrade_arm64_environment_receipt
from tools.official_client_capture import codex_upgrade_vc_artifacts


LEGACY_FACTS_SCHEMA = "codex-upgrade-external-gate-facts/v3"
LEGACY_RECEIPT_SCHEMA = "codex-upgrade-external-gate-receipt/v3"
LEGACY_PRODUCER_SCHEMA = "codex-upgrade-external-gate-producer/v3"
FACTS_SCHEMA = "codex-upgrade-external-gate-facts/v4"
RECEIPT_SCHEMA = "codex-upgrade-external-gate-receipt/v4"
PRODUCER_SCHEMA = "codex-upgrade-external-gate-producer/v4"
SAME_ROOT_CAUSE_RETRY_LIMIT = 2
CANDIDATE_PHASE = "candidate_external"
POST_PROMOTION_PHASE = "post_promotion"
CANONICAL_ACCEPTANCE_SCHEMA = "codex-upgrade-canonical-step/v1"
PHASES = frozenset({CANDIDATE_PHASE, POST_PROMOTION_PHASE})
CANDIDATE_PURPOSES = frozenset({"validation_only", "production_replacement"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ARCHITECTURE_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
MAX_JSON_BYTES = 16 * 1024 * 1024

CANDIDATE_COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "check-egress-spec": (".", ("make", "check-egress-spec")),
    "full-regression": (".", ("make", "test")),
    "target-platform": (".", ("make", "test")),
}
# 该静态集合只用于重放已经生成的 v3 历史收据。v4 的 post-promotion
# 合同必须从 VC-4 门禁计划读取，禁止再把上一版本的 affected rule 写死。
POST_PROMOTION_COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "affected-spec-ep-002": (
        "backend",
        (
            "go",
            "test",
            "./internal/service",
            "-run",
            "^TestUploadOfficialCodexFileC2PAReservationReusesCreateBodyOnRetry$",
            "-count=1",
        ),
    ),
    "affected-spec-hdr-005": (
        "backend",
        (
            "go",
            "test",
            "./internal/service",
            "-run",
            "^TestCandidateTraceCodex0145RuntimeAndBoundaryFacts$",
            "-count=1",
        ),
    ),
    "catalog-projection": (
        "backend",
        (
            "go",
            "test",
            "./internal/service",
            "-run",
            "^TestOfficialCodexProjectionUsesFormalReleaseCatalog$",
            "-count=1",
        ),
    ),
    "official-egress-version-leak-ast": (
        "backend",
        (
            "go",
            "test",
            "./internal/service",
            "-run",
            "^TestOfficialEgressVersionLeakAST$",
            "-count=1",
        ),
    ),
    "version-leak": (".", ("python3", "tools/check_version_leak.py")),
    "version-leak-self-test": (
        ".",
        ("python3", "tools/check_version_leak.py", "--self-test"),
    ),
}


def _is_complete_vc_target(version: str) -> bool:
    return tuple(int(part) for part in version.split(".")) >= (0, 154, 0)


class GateReceiptError(ValueError):
    """外部门禁事实不足或无法可信重放。"""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateReceiptError(f"JSON 存在重复字段：{key}")
        result[key] = value
    return result


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
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateReceiptError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise GateReceiptError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise GateReceiptError(f"{label}不是安全标识")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise GateReceiptError(f"{label}不是小写 SHA-256")
    return value


def _nullable_sha256(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, label)


def _version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise GateReceiptError(f"{label}不是三段式版本号")
    return value


def _candidate_purpose(value: Any, label: str) -> str:
    if value not in CANDIDATE_PURPOSES:
        raise GateReceiptError(
            f"{label}必须是 validation_only 或 production_replacement"
        )
    return str(value)


def _rfc3339(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise GateReceiptError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GateReceiptError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise GateReceiptError(f"{label}缺少时区")
    return parsed


def _private_root(root: Path) -> Path:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise GateReceiptError("evidence root 必须是现有的非符号链接绝对目录")
    resolved = root.resolve(strict=True)
    if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise GateReceiptError("evidence root 权限必须是 0700")
    return resolved


def _relative(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise GateReceiptError(f"{label}必须是证据根内 POSIX 相对路径")
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or str(parsed) != relative
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise GateReceiptError(f"{label}路径不规范或发生逃逸")
    path = root.joinpath(*parsed.parts)
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise GateReceiptError(f"{label}路径包含符号链接：{relative}")
    try:
        path.resolve(strict=path.exists()).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise GateReceiptError(f"{label}越过 evidence root") from error
    return path


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise GateReceiptError(f"{label}不是可信普通文件：{path}")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise GateReceiptError(f"{label}权限必须是 0600：{path}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise GateReceiptError(f"{label}大小非法：{path}")
    content = path.read_bytes()
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise GateReceiptError(f"{label}不是合法 UTF-8 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise GateReceiptError(f"{label}顶层必须是对象：{path}")
    return payload, content


def _binding(root: Path, value: dict[str, Any], label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    reference = _expect(value, {"path", "sha256"}, label)
    relative = reference.get("path")
    expected = _sha256(reference.get("sha256"), f"{label}.sha256")
    path = _relative(root, relative, f"{label}.path")
    payload, content = _load_json(path, label)
    actual = _sha256_bytes(content)
    if actual != expected:
        raise GateReceiptError(f"{label}摘要不一致")
    return {
        "path": relative,
        "sha256": actual,
        "bytes": len(content),
    }, payload


def _validate_subject(value: Any, phase: str) -> dict[str, Any]:
    subject = _expect(
        value,
        {
            "campaign_id",
            "campaign_mode",
            "campaign_purpose",
            "candidate_id",
            "candidate_purpose",
            "target_version",
            "target_architecture",
            "profile_id",
            "profile_digest",
            "candidate_package_digest",
            "candidate_source_tree_sha256",
            "candidate_image_id",
            "candidate_image_reference",
            "production_tree_sha256",
            "acceptance_sha256",
            "promotion_receipt_sha256",
        },
        "subject",
    )
    normalized = {
        "campaign_id": _safe_id(subject.get("campaign_id"), "subject.campaign_id"),
        "campaign_mode": subject.get("campaign_mode"),
        "campaign_purpose": _candidate_purpose(
            subject.get("campaign_purpose"), "subject.campaign_purpose"
        ),
        "candidate_id": _safe_id(subject.get("candidate_id"), "subject.candidate_id"),
        "candidate_purpose": _candidate_purpose(
            subject.get("candidate_purpose"), "subject.candidate_purpose"
        ),
        "target_version": _version(subject.get("target_version"), "subject.target_version"),
        "target_architecture": subject.get("target_architecture"),
        "profile_id": _safe_id(subject.get("profile_id"), "subject.profile_id"),
        "profile_digest": _sha256(subject.get("profile_digest"), "subject.profile_digest"),
        "candidate_package_digest": _sha256(
            subject.get("candidate_package_digest"),
            "subject.candidate_package_digest",
        ),
        "candidate_source_tree_sha256": _sha256(
            subject.get("candidate_source_tree_sha256"),
            "subject.candidate_source_tree_sha256",
        ),
        "candidate_image_id": subject.get("candidate_image_id"),
        "candidate_image_reference": subject.get("candidate_image_reference"),
        "production_tree_sha256": _nullable_sha256(
            subject.get("production_tree_sha256"),
            "subject.production_tree_sha256",
        ),
        "acceptance_sha256": _nullable_sha256(
            subject.get("acceptance_sha256"), "subject.acceptance_sha256"
        ),
        "promotion_receipt_sha256": _nullable_sha256(
            subject.get("promotion_receipt_sha256"),
            "subject.promotion_receipt_sha256",
        ),
    }
    if not isinstance(normalized["candidate_image_id"], str) or not IMAGE_ID_RE.fullmatch(
        normalized["candidate_image_id"]
    ):
        raise GateReceiptError("subject.candidate_image_id 非法")
    if not isinstance(normalized["target_architecture"], str) or not ARCHITECTURE_RE.fullmatch(
        normalized["target_architecture"]
    ):
        raise GateReceiptError("subject.target_architecture 非法")
    if not isinstance(normalized["candidate_image_reference"], str) or not IMAGE_REFERENCE_RE.fullmatch(
        normalized["candidate_image_reference"]
    ):
        raise GateReceiptError("subject.candidate_image_reference 非法")
    if normalized["campaign_mode"] != "formal":
        raise GateReceiptError("subject.campaign_mode 必须为 formal")
    if normalized["candidate_purpose"] != normalized["campaign_purpose"]:
        raise GateReceiptError("subject candidate purpose 与 Campaign 用途不一致")
    if phase == CANDIDATE_PHASE:
        if any(
            normalized[key] is not None
            for key in (
                "production_tree_sha256",
                "acceptance_sha256",
                "promotion_receipt_sha256",
            )
        ):
            raise GateReceiptError("candidate_external 禁止携带生产或 promotion 身份")
    else:
        if normalized["candidate_purpose"] != "production_replacement":
            raise GateReceiptError(
                "post_promotion 只允许 production_replacement candidate"
            )
        if any(
            normalized[key] is None
            for key in (
                "production_tree_sha256",
                "acceptance_sha256",
                "promotion_receipt_sha256",
            )
        ):
            raise GateReceiptError("post_promotion 缺少生产、acceptance 或 promotion 身份")
    return normalized


def _static_contracts(
    commands: Mapping[str, tuple[str, tuple[str, ...]]],
) -> dict[str, dict[str, Any]]:
    """把历史静态命令规范化为统一的门禁合同结构。"""

    return {
        gate_id: {
            "working_directory": working_directory,
            "command": tuple(command),
            "test_id": None,
        }
        for gate_id, (working_directory, command) in commands.items()
    }


def _validate_gate_plan(
    root: Path,
    value: Any,
    *,
    phase: str,
    subject: Mapping[str, Any],
    legacy: bool,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], bool]:
    """返回（计划绑定、执行合同、gate 行是否必须携带 test_id）。"""

    if legacy:
        contracts = (
            CANDIDATE_COMMANDS
            if phase == CANDIDATE_PHASE
            else POST_PROMOTION_COMMANDS
        )
        return None, _static_contracts(contracts), False
    if phase == CANDIDATE_PHASE:
        if value is not None:
            raise GateReceiptError("candidate_external 不得携带 post-promotion 门禁计划")
        return None, _static_contracts(CANDIDATE_COMMANDS), False
    if not isinstance(value, dict):
        raise GateReceiptError("post_promotion v4 必须绑定 VC-4 门禁执行计划")
    binding, raw_plan = _binding(root, value, "gate_plan")
    try:
        plan = codex_upgrade_vc_artifacts.validate_gate_plan(raw_plan)
    except codex_upgrade_vc_artifacts.VCArtifactError as error:
        raise GateReceiptError(f"VC-4 门禁执行计划无法重放：{error}") from error
    if (
        plan.get("campaign_id") != subject.get("campaign_id")
        or plan.get("target_version") != subject.get("target_version")
    ):
        raise GateReceiptError("VC-4 门禁执行计划未绑定当前 Campaign 或目标版本")
    contracts = {
        str(gate["gate_id"]): {
            "working_directory": gate["working_directory"],
            "command": tuple(gate["command"]),
            "test_id": gate["test_id"],
        }
        for gate in plan["gates"]
    }
    if not contracts:
        raise GateReceiptError("VC-4 门禁执行计划不能为空")
    return {
        **binding,
        "plan_sha256": plan["plan_sha256"],
        "requirements_sha256": plan["requirements_sha256"],
    }, contracts, True


def _validate_inputs(
    root: Path,
    values: Any,
    phase: str,
    subject: dict[str, Any],
    gate_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise GateReceiptError("inputs 必须是数组")
    expected_roles = [] if phase == CANDIDATE_PHASE else ["acceptance", "promotion"]
    roles = [item.get("role") for item in values if isinstance(item, dict)]
    if roles != expected_roles:
        raise GateReceiptError(f"{phase} inputs 必须严格为 {expected_roles}")
    normalized: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any]] = {}
    for item in values:
        reference = _expect(item, {"role", "path", "sha256"}, "inputs 项")
        role = reference["role"]
        binding, payload = _binding(
            root,
            {"path": reference["path"], "sha256": reference["sha256"]},
            f"inputs.{role}",
        )
        normalized.append({"role": role, **binding})
        payloads[role] = payload
    if phase == POST_PROMOTION_PHASE:
        acceptance = payloads["acceptance"]
        canonical_acceptance = (
            acceptance.get("schema_version") == CANONICAL_ACCEPTANCE_SCHEMA
            and acceptance.get("item_id") == "acceptance"
        )
        common_invalid = (
            acceptance.get("status") != "complete"
            or acceptance.get("accepted") is not True
            or acceptance.get("production_state") != "accepted_not_activated"
            or acceptance.get("candidate_id") != subject["candidate_id"]
        )
        legacy_invalid = not canonical_acceptance and (
            acceptance.get("campaign_mode") != subject["campaign_mode"]
            or acceptance.get("campaign_purpose") != subject["campaign_purpose"]
            or acceptance.get("candidate_purpose") != subject["candidate_purpose"]
            or acceptance.get("target_version") != subject["target_version"]
            or acceptance.get("profile_id") != subject["profile_id"]
            or acceptance.get("profile_digest") != subject["profile_digest"]
            or acceptance.get("candidate_package_digest")
            != subject["candidate_package_digest"]
        )
        if common_invalid or legacy_invalid:
            raise GateReceiptError("post_promotion acceptance 身份不一致或尚未通过")
        if normalized[0]["sha256"] != subject["acceptance_sha256"]:
            raise GateReceiptError("post_promotion acceptance_sha256 不一致")
        promotion = payloads["promotion"]
        if (
            promotion.get("schema_version") != "official-egress-catalog-promotion/v1"
            or promotion.get("campaign_id") != subject["campaign_id"]
            or promotion.get("acceptance_sha256") != subject["acceptance_sha256"]
            or promotion.get("target_version") != subject["target_version"]
            or promotion.get("target_profile_digest") != subject["profile_digest"]
            or promotion.get("production_selector_changed") is not True
        ):
            raise GateReceiptError("post_promotion promotion receipt 身份不一致")
        if normalized[1]["sha256"] != subject["promotion_receipt_sha256"]:
            raise GateReceiptError("post_promotion promotion_receipt_sha256 不一致")
        if _is_complete_vc_target(subject["target_version"]):
            candidate_identity = acceptance.get("candidate_identity")
            accepted_plan = (
                candidate_identity.get("gate_plan")
                if isinstance(candidate_identity, dict)
                else None
            )
            if (
                gate_plan is None
                or not isinstance(accepted_plan, dict)
                or accepted_plan.get("sha256") != gate_plan["sha256"]
                or accepted_plan.get("plan_sha256") != gate_plan["plan_sha256"]
                or accepted_plan.get("requirements_sha256")
                != gate_plan["requirements_sha256"]
            ):
                raise GateReceiptError(
                    "post_promotion 门禁计划未绑定 VC-5 AcceptanceFact"
                )
    return normalized


def _validate_gates(
    root: Path,
    values: Any,
    phase: str,
    architecture: str,
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    require_test_id: bool,
) -> tuple[list[dict[str, Any]], str, list[str], list[str]]:
    if not isinstance(values, list) or not values:
        raise GateReceiptError("gates 不能为空")
    ids: list[str] = []
    for index, item in enumerate(values, 1):
        if not isinstance(item, dict):
            raise GateReceiptError(f"gates 第 {index} 项必须是对象")
        ids.append(_safe_id(item.get("gate_id"), f"gates 第 {index} 项 gate_id"))
    if ids != sorted(ids) or len(set(ids)) != len(ids) or any(
        gate_id not in contracts for gate_id in ids
    ):
        raise GateReceiptError(f"{phase} 本次执行 gates 必须唯一、排序且属于冻结合同")
    normalized: list[dict[str, Any]] = []
    passed_ids: list[str] = []
    failed_ids: list[str] = []
    latest: datetime | None = None
    latest_raw = ""
    for item in values:
        fields = {
            "gate_id",
            "command",
            "working_directory",
            "host",
            "architecture",
            "started_at_utc",
            "completed_at_utc",
            "exit_code",
            "status",
            "passed_count",
            "failed_count",
            "skipped_count",
            "stdout_sha256",
            "stderr_sha256",
            "evidence",
        }
        if require_test_id:
            fields.add("test_id")
        gate = _expect(
            item,
            fields,
            "gate",
        )
        gate_id = gate["gate_id"]
        contract = contracts[gate_id]
        expected_cwd = contract["working_directory"]
        expected_command = contract["command"]
        if (
            gate.get("working_directory") != expected_cwd
            or gate.get("command") != list(expected_command)
            or (
                require_test_id
                and gate.get("test_id") != contract.get("test_id")
            )
        ):
            raise GateReceiptError(
                f"门禁 {gate_id} 的 test ID、命令或工作目录不符合冻结合同"
            )
        gate_architecture = gate.get("architecture")
        if not isinstance(gate_architecture, str) or not gate_architecture:
            raise GateReceiptError(f"门禁 {gate_id} architecture 为空")
        if gate_id == "target-platform" and gate_architecture != architecture:
            raise GateReceiptError("target-platform 未在候选目标架构执行")
        started = _rfc3339(gate.get("started_at_utc"), f"{gate_id}.started_at_utc")
        completed = _rfc3339(gate.get("completed_at_utc"), f"{gate_id}.completed_at_utc")
        if started > completed:
            raise GateReceiptError(f"门禁 {gate_id} 时间顺序非法")
        for count_name in ("passed_count", "failed_count", "skipped_count"):
            count = gate.get(count_name)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise GateReceiptError(f"门禁 {gate_id} 的 {count_name} 非法")
        if gate.get("skipped_count") != 0:
            raise GateReceiptError(f"门禁 {gate_id} 存在非预期跳过")
        status_value = gate.get("status")
        if status_value == "passed":
            if (
                gate.get("exit_code") != 0
                or gate.get("passed_count") <= 0
                or gate.get("failed_count") != 0
            ):
                raise GateReceiptError(f"门禁 {gate_id} passed 计数或退出码矛盾")
            passed_ids.append(gate_id)
        elif status_value == "failed":
            if gate.get("exit_code") == 0 and gate.get("failed_count") == 0:
                raise GateReceiptError(f"门禁 {gate_id} failed 缺少失败事实")
            failed_ids.append(gate_id)
        else:
            raise GateReceiptError(f"门禁 {gate_id} status 只能为 passed 或 failed")
        evidence = gate.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise GateReceiptError(f"门禁 {gate_id} 缺少证据")
        normalized_evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for reference in evidence:
            binding, _ = _binding(root, reference, f"{gate_id}.evidence")
            if binding["path"] in seen:
                raise GateReceiptError(f"门禁 {gate_id} 证据路径重复")
            seen.add(binding["path"])
            normalized_evidence.append(binding)
        normalized.append(
            {
                **gate,
                "host": _safe_id(gate.get("host"), f"{gate_id}.host"),
                "stdout_sha256": _sha256(
                    gate.get("stdout_sha256"), f"{gate_id}.stdout_sha256"
                ),
                "stderr_sha256": _sha256(
                    gate.get("stderr_sha256"), f"{gate_id}.stderr_sha256"
                ),
                "evidence": normalized_evidence,
            }
        )
        if latest is None or completed > latest:
            latest = completed
            latest_raw = gate["completed_at_utc"]
    return normalized, latest_raw, passed_ids, failed_ids


def _validate_environment(
    root: Path,
    value: Any,
    attempt_id: str,
) -> dict[str, Any]:
    environment = _expect(value, {"before", "after"}, "environment")
    normalized: dict[str, Any] = {}
    receipts: dict[str, dict[str, Any]] = {}
    for role, expected_phase in (("before", "gate_before"), ("after", "gate_after")):
        binding, _ = _binding(root, environment.get(role), f"environment.{role}")
        try:
            receipt = codex_upgrade_arm64_environment_receipt.replay(
                root, str(binding["path"])
            )
        except (
            OSError,
            codex_upgrade_arm64_environment_receipt.Arm64EnvironmentReceiptError,
        ) as error:
            raise GateReceiptError(f"environment.{role} 无法独立重放：{error}") from error
        if (
            receipt.get("status") != "passed"
            or receipt.get("phase") != expected_phase
            or receipt.get("subject_id") != attempt_id
        ):
            raise GateReceiptError(
                f"environment.{role} 与 gate attempt 身份或阶段不一致"
            )
        normalized[role] = binding
        receipts[role] = receipt
    if (
        receipts["before"].get("continuity_identity_sha256")
        != receipts["after"].get("continuity_identity_sha256")
    ):
        raise GateReceiptError("gate attempt 前后 ARM64 环境身份漂移")
    normalized["continuity_identity_sha256"] = receipts["after"][
        "continuity_identity_sha256"
    ]
    return normalized


def _validate_attempt(value: Any) -> dict[str, Any]:
    attempt = _expect(
        value,
        {"attempt_id", "root_cause_id", "previous_receipt"},
        "attempt",
    )
    attempt_id = _safe_id(attempt.get("attempt_id"), "attempt.attempt_id")
    root_cause_id = attempt.get("root_cause_id")
    if root_cause_id is not None:
        root_cause_id = _safe_id(root_cause_id, "attempt.root_cause_id")
    previous = attempt.get("previous_receipt")
    if previous is not None:
        _expect(previous, {"path", "sha256"}, "attempt.previous_receipt")
    return {
        "attempt_id": attempt_id,
        "root_cause_id": root_cause_id,
        "previous_receipt": previous,
    }


def _same_inputs(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    return [
        {key: item[key] for key in ("role", "sha256", "bytes")}
        for item in left
    ] == [
        {key: item[key] for key in ("role", "sha256", "bytes")}
        for item in right
    ]


def build_receipt(
    root: Path,
    facts_relative: str,
    *,
    _seen_receipts: set[str] | None = None,
    _allow_legacy: bool = False,
) -> dict[str, Any]:
    root = _private_root(root)
    facts_path = _relative(root, facts_relative, "facts")
    facts, facts_raw = _load_json(facts_path, "facts")
    facts_schema = facts.get("schema_version")
    legacy = facts_schema == LEGACY_FACTS_SCHEMA
    if legacy and not _allow_legacy:
        raise GateReceiptError("v3 facts 只允许历史收据重放，禁止生成新收据")
    if facts_schema not in {FACTS_SCHEMA, LEGACY_FACTS_SCHEMA}:
        raise GateReceiptError("facts.schema_version 不匹配")
    fields = {
        "schema_version",
        "phase",
        "attempt",
        "subject",
        "inputs",
        "environment",
        "gates",
    }
    if not legacy:
        fields.add("gate_plan")
    _expect(facts, fields, "facts")
    phase = facts.get("phase")
    if phase not in PHASES:
        raise GateReceiptError("facts.phase 非法")
    attempt = _validate_attempt(facts.get("attempt"))
    subject = _validate_subject(facts.get("subject"), phase)
    gate_plan, contracts, require_test_id = _validate_gate_plan(
        root,
        facts.get("gate_plan"),
        phase=phase,
        subject=subject,
        legacy=legacy,
    )
    inputs = _validate_inputs(
        root,
        facts.get("inputs"),
        phase,
        subject,
        gate_plan,
    )
    environment = _validate_environment(root, facts.get("environment"), attempt["attempt_id"])

    previous_binding: dict[str, Any] | None = None
    previous: dict[str, Any] | None = None
    previous_reference = attempt["previous_receipt"]
    expected_executed_ids = sorted(contracts)
    carried: list[dict[str, Any]] = []
    prior_failure_count = 0
    if previous_reference is not None:
        previous_binding, _ = _binding(
            root, previous_reference, "attempt.previous_receipt"
        )
        previous_relative = str(previous_binding["path"])
        seen = set(_seen_receipts or set())
        if previous_relative in seen:
            raise GateReceiptError("门禁 attempt 前序收据形成循环")
        previous = replay(root, previous_relative, _seen_receipts=seen)
        if previous.get("status") != "failed" or not previous.get("failed_gate_ids"):
            raise GateReceiptError("只有失败的前序门禁 attempt 可以补跑")
        if (
            previous.get("phase") != phase
            or previous.get("schema_version")
            != (LEGACY_RECEIPT_SCHEMA if legacy else RECEIPT_SCHEMA)
            or previous.get("subject") != subject
            or not _same_inputs(previous.get("inputs", []), inputs)
            or previous.get("gate_plan") != gate_plan
        ):
            raise GateReceiptError("前序门禁 attempt 身份或输入不连续")
        previous_attempt = previous.get("attempt")
        if not isinstance(previous_attempt, dict):
            raise GateReceiptError("前序门禁 attempt 身份缺失")
        if (
            attempt["root_cause_id"] is None
            or attempt["root_cause_id"] != previous_attempt.get("root_cause_id")
        ):
            raise GateReceiptError("补跑必须承接前序失败的同一 root_cause_id")
        prior_failure_count = int(previous_attempt.get("same_root_cause_failure_count", 0))
        if prior_failure_count >= SAME_ROOT_CAUSE_RETRY_LIMIT:
            raise GateReceiptError("同一根因已连续失败两次，禁止第三次门禁 attempt")
        previous_environment = previous.get("environment")
        if (
            not isinstance(previous_environment, dict)
            or previous_environment.get("continuity_identity_sha256")
            != environment["continuity_identity_sha256"]
        ):
            raise GateReceiptError("前序 after 与本次 before 的 ARM64 环境连续性无法证明")
        expected_executed_ids = list(previous["failed_gate_ids"])
        carried = [
            {**item, "disposition": "carried", "carried_from_attempt": previous_attempt["attempt_id"]}
            for item in previous.get("effective_gates", [])
            if item.get("status") == "passed"
        ]

    gates, completed_at, passed_ids, failed_ids = _validate_gates(
        root,
        facts.get("gates"),
        phase,
        subject["target_architecture"],
        contracts,
        require_test_id=require_test_id,
    )
    executed_ids = [item["gate_id"] for item in gates]
    if executed_ids != expected_executed_ids:
        raise GateReceiptError(
            "门禁补跑集合非法：只能执行前序失败项，禁止重跑已通过项"
        )
    if failed_ids and attempt["root_cause_id"] is None:
        raise GateReceiptError("失败门禁 attempt 必须登记 root_cause_id")
    effective_by_id = {item["gate_id"]: item for item in carried}
    for gate in gates:
        effective_by_id[gate["gate_id"]] = {
            **gate,
            "disposition": "executed",
            "carried_from_attempt": None,
        }
    if sorted(effective_by_id) != sorted(contracts):
        raise GateReceiptError("有效门禁集合未完整覆盖冻结合同")
    effective = [effective_by_id[gate_id] for gate_id in sorted(effective_by_id)]
    effective_failed = [item["gate_id"] for item in effective if item["status"] == "failed"]
    effective_passed = [item["gate_id"] for item in effective if item["status"] == "passed"]
    status_value = "failed" if effective_failed else "passed"
    failure_count = prior_failure_count + 1 if status_value == "failed" else prior_failure_count
    tool_path = Path(__file__).resolve()
    result = {
        "schema_version": LEGACY_RECEIPT_SCHEMA if legacy else RECEIPT_SCHEMA,
        "phase": phase,
        "status": status_value,
        "attempt": {
            "attempt_id": attempt["attempt_id"],
            "root_cause_id": attempt["root_cause_id"],
            "same_root_cause_failure_count": failure_count,
            "previous_receipt": previous_binding,
        },
        "subject": subject,
        "inputs": inputs,
        "environment": environment,
        "executed_gate_ids": executed_ids,
        "carried_gate_ids": sorted(item["gate_id"] for item in carried),
        "passed_gate_ids": effective_passed,
        "failed_gate_ids": effective_failed,
        "effective_gates": effective,
        "completed_at_utc": completed_at,
        "producer": {
            "schema_version": LEGACY_PRODUCER_SCHEMA if legacy else PRODUCER_SCHEMA,
            "tool": str(tool_path),
            "tool_sha256": _sha256_file(tool_path),
            "facts": {
                "path": facts_relative,
                "sha256": _sha256_bytes(facts_raw),
                "bytes": len(facts_raw),
            },
        },
    }
    if not legacy:
        result["gate_plan"] = gate_plan
    return result


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise GateReceiptError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def finalize(root: Path, facts_relative: str, output_relative: str) -> dict[str, Any]:
    root = _private_root(root)
    output = _relative(root, output_relative, "output")
    receipt = build_receipt(root, facts_relative)
    _write_once(output, receipt)
    return receipt


def replay(
    root: Path,
    receipt_relative: str,
    *,
    _seen_receipts: set[str] | None = None,
) -> dict[str, Any]:
    root = _private_root(root)
    seen = set(_seen_receipts or set())
    if receipt_relative in seen:
        raise GateReceiptError("门禁 attempt 收据链形成循环")
    seen.add(receipt_relative)
    receipt_path = _relative(root, receipt_relative, "receipt")
    receipt, raw = _load_json(receipt_path, "receipt")
    receipt_schema = receipt.get("schema_version")
    if receipt_schema not in {RECEIPT_SCHEMA, LEGACY_RECEIPT_SCHEMA}:
        raise GateReceiptError("receipt.schema_version 不匹配")
    producer = receipt.get("producer")
    expected_producer_schema = (
        LEGACY_PRODUCER_SCHEMA
        if receipt_schema == LEGACY_RECEIPT_SCHEMA
        else PRODUCER_SCHEMA
    )
    if (
        not isinstance(producer, dict)
        or producer.get("schema_version") != expected_producer_schema
    ):
        raise GateReceiptError("receipt.producer 不受支持")
    facts = producer.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("path"), str):
        raise GateReceiptError("receipt.producer.facts 缺失")
    expected = build_receipt(
        root,
        facts["path"],
        _seen_receipts=seen,
        _allow_legacy=receipt_schema == LEGACY_RECEIPT_SCHEMA,
    )
    if receipt_schema == LEGACY_RECEIPT_SCHEMA:
        # v3 收据冻结了当时的 producer 路径与工具摘要。升级到 v4 后已无法从
        # 当前文件字节复现旧摘要，但仍可用当前实现完整重放旧 facts、静态合同、
        # 前序链和证据绑定；因此只承接这两个历史 producer 身份字段。
        tool = producer.get("tool")
        tool_sha256 = producer.get("tool_sha256")
        if not isinstance(tool, str) or not tool or not isinstance(
            tool_sha256, str
        ):
            raise GateReceiptError("v3 receipt producer 身份非法")
        _sha256(tool_sha256, "v3 receipt producer.tool_sha256")
        expected["producer"]["tool"] = tool
        expected["producer"]["tool_sha256"] = tool_sha256
    if _canonical(expected) != raw:
        raise GateReceiptError("门禁收据重放结果不一致")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize_parser = subparsers.add_parser("finalize", help="生成不可覆盖门禁收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = subparsers.add_parser("replay", help="从原始事实独立重放门禁收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "finalize":
            result = finalize(arguments.evidence_root, arguments.facts, arguments.output)
        else:
            result = replay(arguments.evidence_root, arguments.receipt)
    except (OSError, GateReceiptError) as error:
        print(f"Codex 升级外部门禁收据失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": result["status"],
                "phase": result["phase"],
                "candidate_id": result["subject"]["candidate_id"],
                "attempt_id": result["attempt"]["attempt_id"],
                "receipt_sha256": _sha256_bytes(_canonical(result)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
