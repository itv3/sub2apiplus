#!/usr/bin/env python3
"""生成并重放 Codex VC-0～VC-6 的小型阶段收据。

本工具只读取调用方明确列出的证明文件，不扫描原始抓包目录，也不执行网络、
部署或删除动作。不同阶段共用一个封闭信封，具体断言和证据角色由 ``kind``
决定，防止操作员用一张结构相似但语义不同的收据跨阶段顶替。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


FACTS_SCHEMA = "codex-upgrade-vc-receipt-facts/v1"
RECEIPT_SCHEMA = "codex-upgrade-vc-receipt/v1"
KINDS = frozenset(
    {
        "p0_gate",
        "implementation_tests",
        "private_archive",
        "cleanup_decision",
        "vc5_completion",
        "vc6_completion",
    }
)
PURPOSES = frozenset({"validation_only", "production_replacement"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


class VCReceiptError(ValueError):
    """阶段收据字段、证据或语义不可信。"""


def _canonical_bytes(value: Any) -> bytes:
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
    """返回稳定 JSON SHA-256。"""

    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        actual = set(value) if isinstance(value, Mapping) else set()
        raise VCReceiptError(
            f"{label}字段不闭合：缺少={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return dict(value)


def _safe_id(value: Any, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise VCReceiptError(f"{label}不是安全标识")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise VCReceiptError(f"{label}不是小写 SHA-256")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise VCReceiptError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VCReceiptError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise VCReceiptError(f"{label}缺少时区")
    return value


def _subject(value: Any, kind: str) -> dict[str, Any]:
    subject = _expect(
        value,
        {
            "upgrade_id",
            "campaign_id",
            "campaign_purpose",
            "baseline_version",
            "target_version",
            "candidate_id",
            "attempt_id",
        },
        "subject",
    )
    _safe_id(subject["upgrade_id"], "subject.upgrade_id")
    _safe_id(subject["campaign_id"], "subject.campaign_id", nullable=True)
    if subject["campaign_purpose"] not in PURPOSES:
        raise VCReceiptError("subject.campaign_purpose 非法")
    for field in ("baseline_version", "target_version"):
        if not isinstance(subject[field], str) or not VERSION_RE.fullmatch(subject[field]):
            raise VCReceiptError(f"subject.{field} 不是三段式版本")
    _safe_id(subject["candidate_id"], "subject.candidate_id", nullable=True)
    _safe_id(subject["attempt_id"], "subject.attempt_id", nullable=True)
    if kind == "p0_gate":
        if any(subject[field] is not None for field in ("campaign_id", "candidate_id", "attempt_id")):
            raise VCReceiptError("P0 收据不得预填 Campaign、Candidate 或 attempt 身份")
    elif kind == "implementation_tests":
        if subject["campaign_id"] is None or subject["candidate_id"] is None or subject["attempt_id"] is not None:
            raise VCReceiptError("VC-4 测试收据必须绑定 Campaign／Candidate，且不得预填 attempt")
    else:
        if subject["campaign_id"] is None or subject["candidate_id"] is None or subject["attempt_id"] is None:
            raise VCReceiptError(f"{kind} 必须绑定 Campaign／Candidate／attempt")
    return subject


def _command_gate(value: Any, label: str) -> dict[str, Any]:
    gate = _expect(
        value,
        {
            "gate_id",
            "kind",
            "command",
            "exit_code",
            "passed",
            "failed",
            "approved_skip",
            "unexpected_skip",
        },
        label,
    )
    _safe_id(gate["gate_id"], f"{label}.gate_id")
    if gate["kind"] not in {"public", "affected"}:
        raise VCReceiptError(f"{label}.kind 非法")
    command = gate["command"]
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 64
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise VCReceiptError(f"{label}.command 非法")
    for field in ("exit_code", "passed", "failed", "approved_skip", "unexpected_skip"):
        item = gate[field]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise VCReceiptError(f"{label}.{field} 非法")
    if gate["exit_code"] != 0 or gate["failed"] != 0 or gate["unexpected_skip"] != 0:
        raise VCReceiptError(f"{label}未通过")
    return gate


# C3 起签发的 P0 断言：离线门禁、工具阻断、回退点与发布认证文件摘要。
P0_ASSERTION_FIELDS = frozenset(
    {"offline_gates", "tool_blockers", "rollback_ready", "release_certification_sha256"}
)
# C3 之前签发的历史 P0 断言：直接绑定完整 Job 演练收据摘要与 campaign-run 演练结论。
# 历史收据只允许重放（读取 0.154 首轮 Campaign 的冻结控制），不允许再用同形状 facts 签发。
HISTORICAL_P0_ASSERTION_FIELDS = frozenset(
    {"offline_gates", "tool_blockers", "campaign_run_rehearsal", "rollback_ready", "job_rehearsal_sha256"}
)
P0_EVIDENCE_ROLES = frozenset({"check_egress_spec", "release_certification", "rollback", "test_capture_tools"})
HISTORICAL_P0_EVIDENCE_ROLES = frozenset(
    {"campaign_run_rehearsal", "check_egress_spec", "job_rehearsal", "rollback", "test_capture_tools"}
)


def _validate_p0_offline_gates(assertions: Mapping[str, Any]) -> list[dict[str, Any]]:
    """两种 P0 断言形状共用的离线门禁、工具阻断与回退点校验。"""

    gates = assertions["offline_gates"]
    if not isinstance(gates, list) or len(gates) != 2:
        raise VCReceiptError("P0 必须且只能登记两项离线门禁")
    normalized = [_command_gate(item, f"P0 gate[{index}]") for index, item in enumerate(gates, 1)]
    expected = {
        "test-capture-tools": ["make", "test-capture-tools"],
        "check-egress-spec": ["make", "check-egress-spec"],
    }
    if (
        [item["gate_id"] for item in normalized] != sorted(expected)
        or any(item["kind"] != "public" or item["command"] != expected[item["gate_id"]] for item in normalized)
    ):
        raise VCReceiptError("P0 离线门禁名称、顺序或字面命令不一致")
    if assertions["tool_blockers"] != []:
        raise VCReceiptError("P0 工具阻断不为零")
    if assertions["rollback_ready"] is not True:
        raise VCReceiptError("P0 回退点不可用")
    return normalized


def _validate_historical_p0_assertions(value: Any) -> dict[str, Any]:
    """只读重放 C3 之前的历史 P0 断言（Job 演练摘要与 campaign-run 演练结论）。"""

    assertions = _expect(value, set(HISTORICAL_P0_ASSERTION_FIELDS), "P0 assertions")
    normalized = _validate_p0_offline_gates(assertions)
    rehearsal = _expect(
        assertions["campaign_run_rehearsal"],
        {
            "multi_batch_passed",
            "original_deadline_inherited",
            "frozen_jobs_passed",
            "live_request_count",
        },
        "P0 campaign_run_rehearsal",
    )
    if rehearsal != {
        "multi_batch_passed": True,
        "original_deadline_inherited": True,
        "frozen_jobs_passed": True,
        "live_request_count": 0,
    }:
        raise VCReceiptError("P0 campaign-run、原始 deadline 或冻结 Job 演练未通过")
    _sha256(assertions["job_rehearsal_sha256"], "P0 job_rehearsal_sha256")
    assertions["offline_gates"] = normalized
    return assertions


def _validate_p0_assertions(value: Any, *, allow_historical: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping) and set(value) == HISTORICAL_P0_ASSERTION_FIELDS:
        if not allow_historical:
            raise VCReceiptError("P0 断言是 C3 之前的历史形状，只能重放不能签发")
        return _validate_historical_p0_assertions(value)
    assertions = _expect(value, set(P0_ASSERTION_FIELDS), "P0 assertions")
    normalized = _validate_p0_offline_gates(assertions)
    # C3：Job rehearsal、campaign-run rehearsal 与 atomic-double 已合成进发布认证，
    # P0 只绑定发布认证收据的文件摘要。
    _sha256(assertions["release_certification_sha256"], "P0 release_certification_sha256")
    assertions["offline_gates"] = normalized
    return assertions


def _validate_implementation_assertions(value: Any) -> dict[str, Any]:
    optional = {key for key in ("build_inputs", "reuse") if isinstance(value, Mapping) and key in value}
    assertions = _expect(
        value,
        {"git_commit", "source_tree_sha256", "target_architecture", "gates"} | optional,
        "VC-4 assertions",
    )
    if not isinstance(assertions["git_commit"], str) or not re.fullmatch(
        r"[0-9a-f]{40,64}", assertions["git_commit"]
    ):
        raise VCReceiptError("VC-4 git_commit 非法")
    _sha256(assertions["source_tree_sha256"], "VC-4 source_tree_sha256")
    if not isinstance(assertions["target_architecture"], str) or not re.fullmatch(
        r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", assertions["target_architecture"]
    ):
        raise VCReceiptError("VC-4 target_architecture 非法")
    gates = assertions["gates"]
    if not isinstance(gates, list) or not gates:
        raise VCReceiptError("VC-4 必须包含 check-egress-spec")
    normalized = [_command_gate(item, f"VC-4 gate[{index}]") for index, item in enumerate(gates, 1)]
    ids = [item["gate_id"] for item in normalized]
    if ids != sorted(set(ids)):
        raise VCReceiptError("VC-4 gate 必须按 ID 唯一排序")
    public = [item for item in normalized if item["gate_id"] == "check-egress-spec"]
    if (
        len(public) != 1
        or public[0]["kind"] != "public"
        or public[0]["command"] != ["make", "check-egress-spec"]
    ):
        raise VCReceiptError("VC-4 未闭合 check-egress-spec 公共门禁")
    assertions["gates"] = normalized
    if "build_inputs" in optional:
        from . import codex_upgrade_candidate_build as build
        try:
            inputs = build.validate_implementation_inputs(assertions["build_inputs"])
        except build.CandidateBuildError as error:
            raise VCReceiptError(str(error)) from error
        if inputs["source_tree_sha256"] != assertions["source_tree_sha256"] or inputs["target_architecture"] != assertions["target_architecture"]:
            raise VCReceiptError("实现测试输入与源码树／架构断言不同")
    if "reuse" in optional:
        if "build_inputs" not in optional:
            raise VCReceiptError("复用实现测试必须冻结完整构建输入")
        reuse = _expect(assertions["reuse"], {"schema_version", "reused_from", "target_revision", "source_build_receipt",
            "source_implementation", "mode", "changed_inputs", "execute_gate_ids", "reuse_gate_ids"}, "实现测试复用")
        if reuse["schema_version"] != "codex-implementation-test-reuse/v1" or reuse["mode"] not in {"all", "target_platform"}:
            raise VCReceiptError("实现测试复用模式非法")
        if not re.fullmatch(r"r[1-9][0-9]*", str(reuse["reused_from"])):
            raise VCReceiptError("实现测试 reused_from 非法")
        for key in ("execute_gate_ids", "reuse_gate_ids", "changed_inputs"):
            rows = reuse[key]
            if not isinstance(rows, list) or any(not isinstance(item, str) for item in rows) or rows != sorted(set(rows)):
                raise VCReceiptError(f"实现测试复用 {key} 未闭合")
        if (set(reuse["execute_gate_ids"]) & set(reuse["reuse_gate_ids"])
                or sorted(reuse["execute_gate_ids"] + reuse["reuse_gate_ids"]) != ids):
            raise VCReceiptError("实现测试执行／复用集合未精确覆盖门禁")
    return assertions


def _validate_archive_assertions(value: Any) -> dict[str, Any]:
    assertions = _expect(
        value,
        {
            "archive_uri",
            "restore_location",
            "inventory_sha256",
            "file_count",
            "total_bytes",
            "unpack_verified",
            "digest_verified",
            "critical_receipts_replayed",
        },
        "归档 assertions",
    )
    for field in ("archive_uri", "restore_location"):
        if not isinstance(assertions[field], str) or not assertions[field].strip():
            raise VCReceiptError(f"归档 {field} 为空")
    if assertions["archive_uri"] == assertions["restore_location"]:
        raise VCReceiptError("归档恢复验证必须位于另一存储位置")
    _sha256(assertions["inventory_sha256"], "归档 inventory_sha256")
    for field in ("file_count", "total_bytes"):
        if not isinstance(assertions[field], int) or isinstance(assertions[field], bool) or assertions[field] < 1:
            raise VCReceiptError(f"归档 {field} 非法")
    for field in ("unpack_verified", "digest_verified", "critical_receipts_replayed"):
        if assertions[field] is not True:
            raise VCReceiptError(f"归档 {field} 未通过")
    return assertions


def _validate_cleanup_assertions(value: Any) -> dict[str, Any]:
    assertions = _expect(
        value,
        {
            "decision",
            "reason",
            "target_count",
            "target_manifest_sha256",
            "dry_run_verified",
            "production_dependencies_zero",
            "unique_evidence_copy_preserved",
            "approved",
            "execution_verified",
        },
        "清理 assertions",
    )
    if assertions["decision"] not in {"executed", "deferred"}:
        raise VCReceiptError("清理 decision 非法")
    if not isinstance(assertions["reason"], str) or not assertions["reason"].strip():
        raise VCReceiptError("清理 reason 为空")
    if not isinstance(assertions["target_count"], int) or isinstance(assertions["target_count"], bool) or assertions["target_count"] < 0:
        raise VCReceiptError("清理 target_count 非法")
    _sha256(assertions["target_manifest_sha256"], "清理 target_manifest_sha256")
    for field in ("dry_run_verified", "production_dependencies_zero", "unique_evidence_copy_preserved"):
        if assertions[field] is not True:
            raise VCReceiptError(f"清理 {field} 未通过")
    if assertions["decision"] == "executed":
        if assertions["approved"] is not True or assertions["execution_verified"] is not True:
            raise VCReceiptError("已执行清理缺少批准或执行后复验")
    elif assertions["execution_verified"] is not False:
        raise VCReceiptError("延期清理不得伪装为已经执行")
    return assertions


def _validate_completion_assertions(kind: str, purpose: str, value: Any) -> dict[str, Any]:
    if kind == "vc5_completion":
        assertions = _expect(
            value,
            {"acceptance_passed", "vc5_pending_count", "canonical_handoff"},
            "VC-5 completion assertions",
        )
        expected_handoff = "not_required" if purpose == "validation_only" else "complete"
        if assertions != {
            "acceptance_passed": True,
            "vc5_pending_count": 0,
            "canonical_handoff": expected_handoff,
        }:
            raise VCReceiptError("VC-5 完成断言未闭合")
        return assertions
    assertions = _expect(
        value,
        {
            "production_tree_closed",
            "final_image_reverified",
            "archive_recoverable",
            "cleanup_decision_recorded",
            "vc6_pending_count",
        },
        "VC-6 completion assertions",
    )
    expected = {
        "production_tree_closed": purpose == "production_replacement",
        "final_image_reverified": purpose == "production_replacement",
        "archive_recoverable": purpose == "production_replacement",
        "cleanup_decision_recorded": purpose == "production_replacement",
        "vc6_pending_count": 0,
    }
    if assertions != expected:
        raise VCReceiptError("VC-6 完成断言与用途不一致")
    return assertions


def _validate_assertions(
    kind: str, purpose: str, value: Any, *, allow_historical: bool = False
) -> dict[str, Any]:
    if kind == "p0_gate":
        return _validate_p0_assertions(value, allow_historical=allow_historical)
    if kind == "implementation_tests":
        return _validate_implementation_assertions(value)
    if kind == "private_archive":
        return _validate_archive_assertions(value)
    if kind == "cleanup_decision":
        return _validate_cleanup_assertions(value)
    return _validate_completion_assertions(kind, purpose, value)


def _expected_roles(kind: str, purpose: str, assertions: Mapping[str, Any]) -> set[str]:
    if kind == "p0_gate":
        # 历史 P0 收据（C3 之前）按其自身角色集合重放；新收据只绑定发布认证。
        if "job_rehearsal_sha256" in assertions:
            return set(HISTORICAL_P0_EVIDENCE_ROLES)
        return set(P0_EVIDENCE_ROLES)
    if kind == "implementation_tests":
        if "reuse" in assertions:
            return set() if assertions["reuse"]["mode"] == "all" else {"implementation_tests"}
        return {"check_egress_spec", "implementation_tests"}
    if kind == "private_archive":
        return {"archive_inventory", "restore_replay"}
    if kind == "cleanup_decision":
        roles = {"cleanup_manifest", "dry_run"}
        if assertions.get("decision") == "executed":
            roles.add("execution")
        return roles
    if kind == "vc5_completion":
        roles = {"acceptance_fact"}
        if purpose == "production_replacement":
            roles.add("canonical_checkpoint")
        return roles
    roles = {"delivery_receipt"}
    if purpose == "production_replacement":
        roles |= {"cleanup_decision", "private_archive"}
    return roles


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise VCReceiptError(f"{label}不是安全相对路径")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VCReceiptError(f"{label}不是安全相对路径")
    return value


def _validate_evidence(
    kind: str,
    purpose: str,
    assertions: Mapping[str, Any],
    value: Any,
    *,
    bound: bool,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise VCReceiptError("evidence 必须是数组")
    expected_fields = {"role", "path", "sha256", "bytes"} if bound else {"role", "path"}
    evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(value, 1):
        item = _expect(raw, expected_fields, f"evidence[{index}]")
        _safe_id(item["role"], f"evidence[{index}].role")
        _relative_path(item["path"], f"evidence[{index}].path")
        if bound:
            _sha256(item["sha256"], f"evidence[{index}].sha256")
            if not isinstance(item["bytes"], int) or isinstance(item["bytes"], bool) or not 1 <= item["bytes"] <= MAX_EVIDENCE_BYTES:
                raise VCReceiptError(f"evidence[{index}].bytes 非法")
        evidence.append(item)
    roles = [item["role"] for item in evidence]
    expected_roles = _expected_roles(kind, purpose, assertions)
    if roles != sorted(expected_roles) or set(roles) != expected_roles:
        raise VCReceiptError(
            f"{kind} evidence 角色未闭合：期望={sorted(expected_roles)}，实际={roles}"
        )
    return evidence


def validate_facts(value: Any) -> dict[str, Any]:
    facts = _expect(value, {"schema_version", "kind", "subject", "assertions", "evidence"}, "facts")
    kind = facts["kind"]
    if facts["schema_version"] != FACTS_SCHEMA or kind not in KINDS:
        raise VCReceiptError("facts schema_version 或 kind 非法")
    subject = _subject(facts["subject"], kind)
    assertions = _validate_assertions(kind, subject["campaign_purpose"], facts["assertions"])
    evidence = _validate_evidence(
        kind,
        subject["campaign_purpose"],
        assertions,
        facts["evidence"],
        bound=False,
    )
    return {**facts, "subject": subject, "assertions": assertions, "evidence": evidence}


def build_receipt(
    facts: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    *,
    issued_at_utc: str | None = None,
) -> dict[str, Any]:
    """从已校验事实和逐文件绑定生成只写一次阶段收据。"""

    normalized_facts = validate_facts(facts)
    kind = normalized_facts["kind"]
    subject = normalized_facts["subject"]
    assertions = normalized_facts["assertions"]
    bound = _validate_evidence(
        kind,
        subject["campaign_purpose"],
        assertions,
        list(evidence),
        bound=True,
    )
    status = "passed" if kind in {"p0_gate", "implementation_tests"} else "complete"
    payload = {
        "schema_version": RECEIPT_SCHEMA,
        "kind": kind,
        "status": status,
        "subject": subject,
        "assertions": assertions,
        "evidence": bound,
        "issued_at_utc": issued_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    payload["receipt_digest"] = digest(payload)
    return validate_receipt(payload)


def validate_receipt(value: Any, *, allow_historical: bool = False) -> dict[str, Any]:
    """校验阶段收据；``allow_historical`` 只在重放时打开，允许读取 C3 之前的历史 P0 形状。"""

    receipt = _expect(
        value,
        {
            "schema_version",
            "kind",
            "status",
            "subject",
            "assertions",
            "evidence",
            "issued_at_utc",
            "receipt_digest",
        },
        "receipt",
    )
    kind = receipt["kind"]
    if receipt["schema_version"] != RECEIPT_SCHEMA or kind not in KINDS:
        raise VCReceiptError("receipt schema_version 或 kind 非法")
    expected_status = "passed" if kind in {"p0_gate", "implementation_tests"} else "complete"
    if receipt["status"] != expected_status:
        raise VCReceiptError(f"{kind} status 非法")
    subject = _subject(receipt["subject"], kind)
    assertions = _validate_assertions(
        kind, subject["campaign_purpose"], receipt["assertions"], allow_historical=allow_historical
    )
    evidence = _validate_evidence(
        kind,
        subject["campaign_purpose"],
        assertions,
        receipt["evidence"],
        bound=True,
    )
    _timestamp(receipt["issued_at_utc"], "receipt.issued_at_utc")
    recorded = _sha256(receipt["receipt_digest"], "receipt.receipt_digest")
    unsigned = dict(receipt)
    unsigned.pop("receipt_digest")
    if digest(unsigned) != recorded:
        raise VCReceiptError("阶段收据自摘要不一致")
    return {**receipt, "subject": subject, "assertions": assertions, "evidence": evidence}


def _private_root(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise VCReceiptError("evidence-root 必须是可信绝对目录")
    root = path.resolve(strict=True)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise VCReceiptError("evidence-root 权限不得允许 group/other 访问")
    return root


def _inside(root: Path, relative: str, label: str) -> Path:
    _relative_path(relative, label)
    try:
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise VCReceiptError(f"{label}越出 evidence-root") from error
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise VCReceiptError(f"{label}包含符号链接")
        cursor = cursor.parent
    return path


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VCReceiptError(f"{label}不是可信普通文件")
    size = path.stat().st_size
    if not 1 <= size <= MAX_JSON_BYTES:
        raise VCReceiptError(f"{label}大小非法")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VCReceiptError(f"{label}不是有效 JSON") from error
    if not isinstance(value, dict):
        raise VCReceiptError(f"{label}必须是对象")
    return value


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise VCReceiptError(f"输出已存在，禁止覆盖：{path}")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise VCReceiptError("输出父目录不存在或不可信")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise VCReceiptError(f"输出已存在，禁止覆盖：{path}") from error
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# E3-03：P0 离线门禁证据的新形状（以执行器的运行清单为证据）
# ---------------------------------------------------------------------------
#
# 入口门禁在全集通过模式下会承接以往运行的正式执行记录（E3-01）：门禁项的单元一部分本次执行、一部分承接，
# 不再是「make 命令的一次运行」。这种运行的两份离线门禁证据写成 v2：登记执行器的运行清单（run_id、自摘要、
# 模式）、记录库位置、门禁项的命令单元与测试组、本次执行／承接的单元数，以及测试树的工具五摘要。签发
# （finalize）与 VC-0 收口按下面的规则逐条重验，任何一条不符即拒绝：
#   · 清单已发布进记录库、自摘要与证据登记的相符；两份证据引用同一份清单、同一记录库、同一工具五摘要；
#   · 闭合：check-egress-spec 的单元等于清单规划的全部 egress-spec: 子检查单元；test-capture-tools 的成员是
#     前置检查单元加测试组的全部单元，测试 ID 并集等于清单冻结的测试组全集，没有缺报、重复或全集之外的 ID；
#   · 逐条记录：在库、自摘要与文件名相符、是该单元的正式执行、规格与输入摘要等于清单、调度策略／环境指纹／
#     执行器与清单相同、按原始字段判通过（测试单元逐个测试结论在通过集合内）、日志在库且摘要相符；本次执行的
#     记录来自本次运行；承接的记录来自别的运行、原运行清单把它列为正式执行、完成时间在本次清单的承接期限内；
#   · 计数按记录重算（通过、跳过、本次执行、承接），与证据和收据断言一致；
#   · 工具五摘要等于发布认证登记的身份。
# v1（make 命令的一次运行）与更早的手写证据不读内容，照旧只按字节绑定（两条字面 make 命令的现行形状继续可用）。
# 重放（replay）只核对字节绑定、不读记录库：历史收据，以及记录库清理之后的收据，都照常可重放。
# 本节只依赖标准库；执行记录与清单的摘要算法、路径规则与 tools/ci/unit_records.py 相同（那边改了这里要跟着改）。
P0_GATE_EVIDENCE_V2_SCHEMA = "codex-p0-offline-gate-evidence/v2"
# 证据角色 → 门禁项
P0_GATE_EVIDENCE_ROLES = {"check_egress_spec": "check-egress-spec", "test_capture_tools": "test-capture-tools"}
P0_V2_COMMON_FIELDS = frozenset({
    "schema_version", "gate_id", "command", "working_directory", "git_commit", "status", "exit_code", "passed", "failed",
    "approved_skip", "unexpected_skip", "elapsed_seconds", "raw_errors", "temporary_asset_inventory", "executed_as",
    "executor_summary", "unit_manifest", "record_store", "units", "test_group", "unit_counts", "tool_identity",
})
P0_V2_GATE_FIELDS = {
    "check-egress-spec": frozenset({"checks"}),
    "test-capture-tools": frozenset({"expected_tests", "reported_tests", "skipped"}),
}
P0_IDENTITY_FIELDS = ("policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "tool_files_sha256")
EGRESS_SPEC_UNIT_PREFIX = "egress-spec:"
CAPTURE_TEST_GROUP = "capture-tools"
# test-capture-tools 门禁项的命令单元：Makefile 里 test-capture-tools 的前置检查（入口门禁 plan 的同名单元）。
CAPTURE_COMMAND_UNITS = ("capture:prerequisites",)
UNIT_RECORD_SCHEMA = "unit-execution-record/v1"
UNIT_MANIFEST_SCHEMA = "unit-execution-manifest/v1"
UNIT_FULL_SET_PASS = "full-set-pass"
UNIT_MODES = frozenset({UNIT_FULL_SET_PASS, "re-execute"})
UNIT_MAX_AGE_HOURS = 168.0
UNIT_PASSING_OUTCOMES = frozenset({"passed", "skipped", "expected_failure"})
UNIT_RUN_ID_RE = re.compile(r"^[0-9A-Za-z._-]{1,128}$")
# 承接记录的完成时间允许比清单的判定时间晚这么多秒（与 unit_records.check_record 的 0.1 小时相同）。
UNIT_CLOCK_SKEW_SECONDS = 360


def _unit_sha256(value: Any) -> str:
    """执行记录与运行清单的摘要算法：与 unit_records.canonical 相同（紧凑分隔、排序键、不转义、不加换行）。"""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _unit_sealed(payload: Mapping[str, Any], key: str) -> bool:
    body = {name: value for name, value in payload.items() if name != key}
    return payload.get(key) == _unit_sha256(body)


def _unit_utc(value: Any) -> float | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _optional_json(path: Path) -> dict[str, Any] | None:
    """读一份证据文件；不是 JSON 对象（例如手写 P0 的日志证据）时返回 None，由调用方当作旧形状。"""

    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class _UnitRecordStore:
    """记录库的只读访问：路径规则与 unit_records.RecordStore 相同（记录按单元 ID 摘要前 16 位分桶，日志与运行清单
    按名字存放）。读出的文件拒绝符号链接与超限大小；运行清单只增不改，同一次重验里按 run_id 缓存。"""

    def __init__(self, root: Any) -> None:
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            raise VCReceiptError("P0 v2 证据登记的记录库不是绝对路径")
        path = Path(root)
        if path.is_symlink() or not path.is_dir():
            raise VCReceiptError(f"P0 v2 证据登记的记录库不存在或不可信：{root}")
        self.root = path.resolve(strict=True)
        self._manifests: dict[str, dict[str, Any] | None] = {}
        self.verified_logs: set[str] = set()

    def record(self, unit_id: str, record_sha256: Any) -> dict[str, Any] | None:
        if not isinstance(record_sha256, str) or not SHA256_RE.fullmatch(record_sha256):
            return None
        bucket = hashlib.sha256(unit_id.encode("utf-8")).hexdigest()[:16]
        return _optional_json(self.root / "records" / bucket / f"{record_sha256}.json")

    def manifest(self, run_id: Any) -> dict[str, Any] | None:
        if not isinstance(run_id, str) or not UNIT_RUN_ID_RE.fullmatch(run_id):
            return None
        if run_id not in self._manifests:
            self._manifests[run_id] = _optional_json(self.root / "runs" / f"{run_id}.json")
        return self._manifests[run_id]

    def log_ok(self, digest: Any) -> bool:
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            return False
        if digest not in self.verified_logs:
            path = self.root / "logs" / f"{digest}.log"
            if path.is_symlink() or not path.is_file() or file_sha256(path) != digest:
                return False
            self.verified_logs.add(digest)
        return True


def _unit_record_passes(record: Mapping[str, Any]) -> tuple[bool, str]:
    """按执行记录的原始字段重判结论，与 unit_records.derived_pass 同一规则（不只看 passed 字段）。"""

    if record.get("exit_code") != 0 or record.get("signal") is not None or record.get("timed_out") is not False:
        return False, "退出状态不是成功"
    if record.get("unit_type") == "test":
        tests, ids = record.get("tests"), record.get("test_ids")
        if (not isinstance(tests, dict) or not isinstance(ids, list) or not ids
                or not all(isinstance(test_id, str) for test_id in ids) or len(ids) != len(set(ids)) or set(tests) != set(ids)):
            return False, "测试结果与测试 ID 集合不符"
        if any(not isinstance(item, Mapping) or item.get("outcome") not in UNIT_PASSING_OUTCOMES for item in tests.values()):
            return False, "有测试没通过"
    elif record.get("unit_type") != "command":
        return False, "单元类型不认识"
    if record.get("passed") is not True:
        return False, "记录结论不是通过"
    return True, ""


def _p0_v2_manifest(store: _UnitRecordStore, reference: Any) -> dict[str, Any]:
    """证据登记的运行清单：已发布进记录库、自摘要与模式相符，规划单元与单元项一一对应、不重复。"""

    reference = _expect(reference, {"run_id", "manifest_sha256", "mode"}, "P0 v2 证据 unit_manifest")
    manifest = store.manifest(reference["run_id"])
    if (manifest is None or manifest.get("schema_version") != UNIT_MANIFEST_SCHEMA or not _unit_sealed(manifest, "manifest_sha256")
            or manifest.get("run_id") != reference["run_id"]):
        raise VCReceiptError("P0 v2 证据引用的运行清单没有发布到记录库，或自摘要不符（清单自检没通过的运行不能签 P0）")
    if manifest.get("manifest_sha256") != reference["manifest_sha256"] or manifest.get("mode") != reference["mode"]:
        raise VCReceiptError("P0 v2 证据登记的清单摘要或模式与记录库里的运行清单不符")
    if manifest.get("mode") not in UNIT_MODES:
        raise VCReceiptError(f"运行清单的模式不认识：{manifest.get('mode')!r}")
    planned, entries = manifest.get("planned_units"), manifest.get("units")
    if (not isinstance(planned, list) or not isinstance(entries, list) or not all(isinstance(item, str) for item in planned)
            or not all(isinstance(item, Mapping) for item in entries)):
        raise VCReceiptError("运行清单缺少规划单元或单元项")
    ids = [str(entry.get("unit_id")) for entry in entries]
    if len(set(planned)) != len(planned) or len(set(ids)) != len(ids) or set(ids) != set(planned):
        raise VCReceiptError("运行清单的单元项与规划单元不是一一对应（有缺、有多或有重复）")
    return manifest


def _p0_v2_record(store: _UnitRecordStore, manifest: Mapping[str, Any], entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """核验运行清单里的一个单元项，返回（执行记录，问题）。问题非空即拒绝签发。"""

    unit_id = str(entry.get("unit_id"))
    disposition = entry.get("disposition")
    if disposition not in ("executed", "inherited"):
        return None, [f"{unit_id}：处置不是本次执行或承接（{disposition!r}）"]
    if disposition == "inherited" and manifest.get("mode") != UNIT_FULL_SET_PASS:
        return None, [f"{unit_id}：重新执行全集的运行清单不得有承接项"]
    digest = entry.get("record_sha256")
    record = store.record(unit_id, digest)
    if record is None:
        return None, [f"{unit_id}：执行记录不在记录库里"]
    if record.get("schema_version") != UNIT_RECORD_SCHEMA or not _unit_sealed(record, "record_sha256") or record.get("record_sha256") != digest:
        return None, [f"{unit_id}：执行记录自摘要不符（被改过）"]
    if record.get("unit_id") != unit_id or record.get("kind") != "formal" or record.get("unit_type") != entry.get("unit_type"):
        return None, [f"{unit_id}：执行记录不是该单元的正式执行"]
    problems: list[str] = []
    if record.get("spec_sha256") != entry.get("spec_sha256") or record.get("inputs_sha256") != entry.get("inputs_sha256"):
        problems.append(f"{unit_id}：执行记录的单元规格或输入摘要与运行清单不符")
    executor = record.get("executor") if isinstance(record.get("executor"), Mapping) else {}
    current_executor = manifest.get("executor") if isinstance(manifest.get("executor"), Mapping) else {}
    if (record.get("policy_sha256") != manifest.get("policy_sha256") or record.get("environment_sha256") != manifest.get("environment_sha256")
            or not executor.get("sha256") or executor.get("sha256") != current_executor.get("sha256")):
        problems.append(f"{unit_id}：执行记录的调度策略、环境指纹或执行器与运行清单不同")
    passed, why = _unit_record_passes(record)
    if not passed:
        problems.append(f"{unit_id}：执行记录不是通过（{why}）")
    run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
    run_id = run.get("run_id")
    if disposition == "executed":
        if run_id != manifest.get("run_id"):
            problems.append(f"{unit_id}：登记为本次执行，记录却来自别的运行")
    else:
        if run_id == manifest.get("run_id"):
            problems.append(f"{unit_id}：承接项的记录来自本次运行")
        origin = store.manifest(run_id)
        if (origin is None or origin.get("schema_version") != UNIT_MANIFEST_SCHEMA or not _unit_sealed(origin, "manifest_sha256")
                or origin.get("run_id") != run_id):
            problems.append(f"{unit_id}：原运行的清单缺失或自摘要不符")
        elif not any(isinstance(item, Mapping) and item.get("unit_id") == unit_id and item.get("disposition") == "executed"
                     and item.get("record_sha256") == digest for item in origin.get("units") or []):
            problems.append(f"{unit_id}：原运行的清单没有把这条记录列为该单元的正式执行")
        basis = entry.get("basis") if isinstance(entry.get("basis"), Mapping) else {}
        if basis.get("run_id") != run_id or basis.get("completed_at_utc") != record.get("completed_at_utc"):
            problems.append(f"{unit_id}：运行清单登记的承接依据与执行记录不符")
        completed, decided = _unit_utc(record.get("completed_at_utc")), _unit_utc(manifest.get("decided_at_utc"))
        try:
            limit = min(float(manifest.get("inheritance_max_age_hours")), UNIT_MAX_AGE_HOURS)
        except (TypeError, ValueError):
            limit = 0.0
        if completed is None or decided is None or not -UNIT_CLOCK_SKEW_SECONDS <= decided - completed <= limit * 3600:
            problems.append(f"{unit_id}：承接的记录超过运行清单的承接期限，或时间不可读")
    log = record.get("log") if isinstance(record.get("log"), Mapping) else {}
    if not store.log_ok(log.get("sha256")):
        problems.append(f"{unit_id}：日志不在记录库里或摘要不符")
    return record, problems


def _count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise VCReceiptError(f"{label}不是非负整数")
    return value


def _verify_p0_gate_v2(
    store: _UnitRecordStore,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    gate_id: str,
    release_identity: Mapping[str, str],
) -> dict[str, Any]:
    """一份 v2 门禁证据的逐条重验（规则见本节说明），返回按记录重算的结果。"""

    evidence = _expect(payload, set(P0_V2_COMMON_FIELDS | P0_V2_GATE_FIELDS[gate_id]), f"P0 {gate_id} v2 证据")
    if evidence["gate_id"] != gate_id or evidence["command"] != ["make", gate_id]:
        raise VCReceiptError(f"P0 {gate_id} v2 证据的门禁项或字面命令不对")
    if (evidence["status"] != "passed" or evidence["exit_code"] != 0 or evidence["failed"] != 0
            or evidence["unexpected_skip"] != 0):
        raise VCReceiptError(f"P0 {gate_id} v2 证据不是通过")
    identity = _expect(evidence["tool_identity"], set(P0_IDENTITY_FIELDS), f"P0 {gate_id} v2 证据 tool_identity")
    if identity != dict(release_identity):
        raise VCReceiptError(f"P0 {gate_id} v2 证据的工具五摘要与发布认证登记的身份不一致")
    units = evidence["units"]
    if not isinstance(units, list) or not all(isinstance(item, str) for item in units) or len(set(units)) != len(units):
        raise VCReceiptError(f"P0 {gate_id} v2 证据的 units 非法")
    members = [entry for entry in manifest["units"] if isinstance(entry.get("gates"), list) and gate_id in entry["gates"]]
    if not members:
        raise VCReceiptError(f"运行清单里没有门禁项 {gate_id} 的单元")
    if gate_id == "check-egress-spec":
        planned = sorted(unit for unit in manifest["planned_units"] if unit.startswith(EGRESS_SPEC_UNIT_PREFIX))
        checks = evidence["checks"]
        if not isinstance(checks, list) or not all(isinstance(item, Mapping) for item in checks):
            raise VCReceiptError("P0 check-egress-spec v2 证据的 checks 非法")
        targets = sorted(f"{EGRESS_SPEC_UNIT_PREFIX}{item.get('target')}" for item in checks)
        if (not planned or sorted(str(entry["unit_id"]) for entry in members) != planned or sorted(units) != planned
                or targets != planned or evidence["test_group"] is not None):
            raise VCReceiptError("check-egress-spec 的单元与运行清单规划的 egress-spec 子检查不闭合")
        if any(item.get("passed") is not True or item.get("exit_code") != 0 for item in checks):
            raise VCReceiptError("P0 check-egress-spec v2 证据里有子检查不是通过")
    else:
        if evidence["test_group"] != CAPTURE_TEST_GROUP or sorted(units) != sorted(CAPTURE_COMMAND_UNITS):
            raise VCReceiptError("P0 test-capture-tools v2 证据的测试组或命令单元不对")
        group_ids = sorted(str(entry["unit_id"]) for entry in manifest["units"] if entry.get("test_group") == CAPTURE_TEST_GROUP)
        member_tests = sorted(str(entry["unit_id"]) for entry in members if entry.get("unit_type") == "test")
        member_commands = sorted(str(entry["unit_id"]) for entry in members if entry.get("unit_type") != "test")
        if not group_ids or member_tests != group_ids or member_commands != sorted(CAPTURE_COMMAND_UNITS):
            raise VCReceiptError("test-capture-tools 在运行清单里的成员与前置检查加测试组全部单元不闭合")
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    for entry in members:
        record, found = _p0_v2_record(store, manifest, entry)
        problems.extend(found)
        if record is not None:
            records.append(record)
    if problems:
        more = f"（另有 {len(problems) - 5} 条）" if len(problems) > 5 else ""
        raise VCReceiptError(f"P0 {gate_id} v2 证据的执行记录重验不通过：{'；'.join(problems[:5])}{more}")
    counts = {
        "executed": sum(1 for entry in members if entry.get("disposition") == "executed"),
        "inherited": sum(1 for entry in members if entry.get("disposition") == "inherited"),
    }
    if evidence["unit_counts"] != counts:
        raise VCReceiptError(f"P0 {gate_id} v2 证据登记的本次执行／承接单元数与运行清单重算的不一致")
    if gate_id == "check-egress-spec":
        passed, skipped = len(members), 0
    else:
        expected = (manifest.get("test_groups") or {}).get(CAPTURE_TEST_GROUP)
        if not isinstance(expected, list) or not expected or len(set(expected)) != len(expected):
            raise VCReceiptError("运行清单没有冻结测试组 capture-tools 的测试 ID 全集")
        reported = [test_id for record in records if record.get("unit_type") == "test" for test_id in record["tests"]]
        missing, extra = set(expected) - set(reported), set(reported) - set(expected)
        if missing or extra or len(reported) != len(set(reported)):
            raise VCReceiptError(f"测试组 capture-tools 记录里的测试 ID 并集与全集不符（缺 {len(missing)}、多 {len(extra)}、"
                                 f"重复 {len(reported) - len(set(reported))}）")
        outcomes = [item for record in records if record.get("unit_type") == "test" for item in record["tests"].values()]
        passed = sum(1 for item in outcomes if item.get("outcome") in {"passed", "expected_failure"})
        skipped = sum(1 for item in outcomes if item.get("outcome") == "skipped")
        skipped_rows = sorted(
            ({"test_id": test_id, "reason": str(item.get("reason", ""))}
             for record in records if record.get("unit_type") == "test"
             for test_id, item in record["tests"].items() if item.get("outcome") == "skipped"),
            key=lambda row: row["test_id"],
        )
        if (_count(evidence["expected_tests"], "expected_tests") != len(expected)
                or _count(evidence["reported_tests"], "reported_tests") != len(reported) or evidence["skipped"] != skipped_rows):
            raise VCReceiptError("P0 test-capture-tools v2 证据的测试数或跳过清单与执行记录重算的不一致")
    if _count(evidence["passed"], "passed") != passed or _count(evidence["approved_skip"], "approved_skip") != skipped:
        raise VCReceiptError(f"P0 {gate_id} v2 证据的通过数或跳过数与执行记录重算的不一致")
    return {"gate_id": gate_id, "units": len(members), **counts, "passed": passed, "approved_skip": skipped}


def verify_p0_manifest_evidence(root: Path, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """P0 两份离线门禁证据的形状判定与 v2 重验（E3-03）。

    ``payload`` 是已校验的 P0 facts 或收据（只用 ``evidence`` 的角色与路径、``assertions`` 的离线门禁），``root``
    是证据根。两份证据都不是 v2 时返回 None（v1 与手写证据照旧只按字节绑定）；一份是 v2 另一份不是时拒绝；都是
    v2 时按本节规则逐条重验，并核对收据断言里两项门禁的通过数与跳过数等于按记录重算的，返回重验摘要。"""

    if payload.get("kind") != "p0_gate":
        raise VCReceiptError("只有 P0 收据有离线门禁证据")
    evidence_root = _private_root(root)
    paths = {
        str(item["role"]): _inside(evidence_root, item["path"], f"evidence.{item['role']}")
        for item in payload.get("evidence") or []
        if isinstance(item, Mapping)
    }
    if not set(P0_GATE_EVIDENCE_ROLES) <= set(paths):
        return None  # 历史形状（角色集合不同）由 validate_receipt 管，这里不读内容
    shapes = {}
    for role in P0_GATE_EVIDENCE_ROLES:
        document = _optional_json(paths[role])
        shapes[role] = document if document is not None and document.get("schema_version") == P0_GATE_EVIDENCE_V2_SCHEMA else None
    if all(document is None for document in shapes.values()):
        return None
    if any(document is None for document in shapes.values()):
        raise VCReceiptError("P0 两份离线门禁证据必须同一形状：一份是 v2（运行清单）另一份不是")
    egress, capture = shapes["check_egress_spec"], shapes["test_capture_tools"]
    if any(egress.get(key) != capture.get(key) for key in ("unit_manifest", "record_store", "tool_identity")):
        raise VCReceiptError("P0 两份 v2 证据引用的运行清单、记录库或工具五摘要不同（必须来自入口门禁同一次运行）")
    release = _optional_json(paths["release_certification"]) if "release_certification" in paths else None
    identity = release.get("identity") if release is not None else None
    if not isinstance(identity, Mapping) or any(
        not isinstance(identity.get(name), str) or not SHA256_RE.fullmatch(identity[name]) for name in P0_IDENTITY_FIELDS
    ):
        raise VCReceiptError("发布认证没有登记工具五摘要，无法核对 P0 v2 证据的工具身份")
    release_identity = {name: identity[name] for name in P0_IDENTITY_FIELDS}
    store = _UnitRecordStore(egress.get("record_store"))
    manifest = _p0_v2_manifest(store, egress.get("unit_manifest"))
    results = {
        gate_id: _verify_p0_gate_v2(store, manifest, shapes[role], gate_id, release_identity)
        for role, gate_id in P0_GATE_EVIDENCE_ROLES.items()
    }
    assertions = payload.get("assertions") if isinstance(payload.get("assertions"), Mapping) else {}
    declared = {
        str(item.get("gate_id")): item for item in assertions.get("offline_gates") or [] if isinstance(item, Mapping)
    }
    for gate_id, result in results.items():
        gate = declared.get(gate_id)
        if gate is None or gate.get("passed") != result["passed"] or gate.get("approved_skip") != result["approved_skip"]:
            raise VCReceiptError(f"P0 断言里 {gate_id} 的通过数或跳过数与 v2 证据按记录重算的不一致")
    return {
        "schema_version": P0_GATE_EVIDENCE_V2_SCHEMA,
        "run_id": manifest["run_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "mode": manifest["mode"],
        "record_store": str(store.root),
        "gates": results,
    }


def finalize(root: Path, facts_relative: str, output_relative: str) -> dict[str, Any]:
    """绑定 facts 声明的全部小型证明文件并生成收据。P0 的两份离线门禁证据是 v2（运行清单）时，签发前按记录库
    逐条重验（E3-03，见 ``verify_p0_manifest_evidence``）。"""

    evidence_root = _private_root(root)
    facts_path = _inside(evidence_root, facts_relative, "facts")
    facts = validate_facts(_read_json(facts_path, "facts"))
    bindings: list[dict[str, Any]] = []
    for item in facts["evidence"]:
        path = _inside(evidence_root, item["path"], f"evidence.{item['role']}")
        if not path.is_file() or path.is_symlink():
            raise VCReceiptError(f"evidence.{item['role']} 不是可信普通文件")
        size = path.stat().st_size
        if not 1 <= size <= MAX_EVIDENCE_BYTES:
            raise VCReceiptError(f"evidence.{item['role']} 大小非法")
        bindings.append(
            {
                "role": item["role"],
                "path": item["path"],
                "sha256": file_sha256(path),
                "bytes": size,
            }
        )
    if facts["kind"] == "p0_gate":
        verify_p0_manifest_evidence(evidence_root, facts)
    receipt = build_receipt(facts, bindings)
    output = evidence_root / _relative_path(output_relative, "output")
    try:
        output.resolve(strict=False).relative_to(evidence_root)
    except ValueError as error:
        raise VCReceiptError("output 越出 evidence-root") from error
    _write_once(output, receipt)
    return receipt


def _file_binding(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise VCReceiptError(f"复用来源必须是可信绝对普通文件：{path}")
    return {"path": str(path), "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _bound_json(binding: Any, label: str) -> tuple[Path, dict[str, Any]]:
    binding = _expect(binding, {"path", "sha256", "bytes"}, label)
    path = Path(str(binding["path"]))
    if _file_binding(path) != binding:
        raise VCReceiptError(f"{label} 摘要或大小漂移")
    return path, _read_json(path, label)


def validate_build_input_binding(binding: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """构建读侧交叉核对小收据中的输入；完整证据重放仍由既有读侧执行。"""

    root = _private_root(Path(binding["evidence_root"]))
    path = _inside(root, binding["receipt"]["path"], "实现测试收据")
    expected = {**binding["receipt"], "path": str(path)}
    if _file_binding(path) != expected:
        raise VCReceiptError("实现测试收据文件绑定漂移")
    receipt = validate_receipt(_read_json(path, "实现测试收据"), allow_historical=True)
    if (receipt["receipt_digest"] != binding["receipt_digest"] or receipt["kind"] != "implementation_tests"
            or receipt["assertions"].get("build_inputs") != inputs):
        raise VCReceiptError("实现测试收据未绑定本构建的完整输入")
    return receipt


def _reuse_context(target_revision_path: Path, current_inputs: Mapping[str, Any], seen: set[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    """从已 COMMIT revision 的直接前序取得可信测试，不执行旧镜像或重新装配旧制品。"""

    from . import codex_upgrade_vc_artifacts as artifacts
    from . import codex_upgrade_candidate_build as build
    try:
        target_binding = _file_binding(target_revision_path)
        target = artifacts.validate_candidate_revision(_read_json(target_revision_path, "目标 revision"))
        campaign = target_revision_path.parents[4]
        if target_revision_path != campaign / "control/vc/revisions" / f"r{target['revision']}" / "revision.json":
            raise VCReceiptError("目标 revision 路径不符合 Campaign 边界")
        supersedes = target["supersedes"]
        if not isinstance(supersedes, Mapping):
            raise VCReceiptError("r1 没有可复用的前序 revision")
        old_id = supersedes["candidate_id"]
        previous_path = campaign / "control/vc/revisions" / f"r{supersedes['revision']}" / "revision.json"
        previous = artifacts.validate_candidate_revision(_read_json(previous_path, "前序 revision"))
        for path, record in ((target_revision_path, target), (previous_path, previous)):
            commit_path = path.with_name("COMMIT")
            _file_binding(commit_path)
            commit = artifacts.validate_candidate_revision_commit(_read_json(commit_path, "revision COMMIT"))
            if any(commit[key] != record[key] for key in ("campaign_id", "revision", "candidate_id", "record_sha256")):
                raise VCReceiptError("实现测试复用的 revision 尚未 COMMIT 或绑定不一致")
        if (previous["record_sha256"] != target["previous_revision_sha256"]
                or previous["candidate_id"] != old_id or previous["campaign_id"] != target["campaign_id"]
                or previous["vc3_stage_receipt"] != target["vc3_stage_receipt"]):
            raise VCReceiptError("实现测试复用的前序 revision 或 VC-3 绑定漂移")
        invalidation_path = campaign / "candidates" / old_id / "invalidation.json"
        if (supersedes["invalidation_receipt"]["path"] != invalidation_path.relative_to(campaign).as_posix()
                or file_sha256(invalidation_path) != supersedes["invalidation_receipt"]["sha256"] or invalidation_path.is_symlink()):
            raise VCReceiptError("实现测试复用的 invalidation 绑定漂移")
        invalidation = artifacts.validate_candidate_invalidation(_read_json(invalidation_path, "前序作废记录"))
        if (invalidation["campaign_id"] != target["campaign_id"] or invalidation["candidate_id"] != old_id
                or invalidation["revision"] != previous["revision"]):
            raise VCReceiptError("实现测试复用的作废对象与前序 revision 不一致")
        build_path = campaign / "candidates" / old_id / "build-receipt.json"
        build_binding = _file_binding(build_path)
        if invalidation["identity_snapshot"]["build_receipt_sha256"] != build_binding["sha256"]:
            raise VCReceiptError("前序构建收据与作废时冻结摘要不同")
        previous_build = artifacts.validate_candidate_build_receipt(_read_json(build_path, "前序构建收据"))
        implementation = previous_build["implementation_tests"]
        source = _replay(Path(implementation["evidence_root"]), implementation["receipt"]["path"], seen)
        previous_inputs = previous_build["build"].get("inputs")
        if previous_inputs is None:
            raise VCReceiptError("历史构建没有完整输入证明，必须重跑实现测试")
        validate_build_input_binding(implementation, previous_inputs)
        if (source["subject"]["candidate_id"] != old_id or previous_build["candidate_id"] != old_id
                or source["subject"]["campaign_id"] != target["campaign_id"]
                or previous_build["campaign_id"] != target["campaign_id"]):
            raise VCReceiptError("前序实现测试的 Candidate／Campaign 身份不一致")
        plan = build.implementation_retest_plan(previous_inputs, current_inputs,
            [row["gate_id"] for row in source["assertions"]["gates"]])
        proof = {"schema_version": "codex-implementation-test-reuse/v1", "reused_from": f"r{supersedes['revision']}",
            "target_revision": target_binding, "source_build_receipt": build_binding,
            "source_implementation": implementation, **plan}
        return proof, source
    except (artifacts.VCArtifactError, build.CandidateBuildError, KeyError, IndexError) as error:
        raise VCReceiptError(f"实现测试复用来源不可信：{error}") from error


def plan_implementation_reuse(target_revision_path: Path, current_inputs: Mapping[str, Any]) -> dict[str, Any]:
    """返回真实输入决定的执行／复用集合，调用方不得自行删减 execute。"""

    return _reuse_context(target_revision_path, current_inputs, set())[0]


def build_reused_implementation_facts(target_revision_path: Path, current_inputs: Mapping[str, Any],
                                     executed_gates: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """给当前 Candidate 签发显式承接 facts，保留原测试的身份、结果和证据引用。"""

    proof, source = _reuse_context(target_revision_path, current_inputs, set())
    if proof["mode"] == "full":
        raise VCReceiptError("源码、依赖或工具链输入变化，必须重跑全部实现测试")
    target = _read_json(target_revision_path, "目标 revision")
    old_gates = {row["gate_id"]: row for row in source["assertions"]["gates"]}
    executed = {row["gate_id"]: dict(row) for row in executed_gates}
    if len(executed) != len(executed_gates) or sorted(executed) != proof["execute_gate_ids"]:
        raise VCReceiptError("当前成功门禁未精确覆盖目标平台重跑集合")
    for gate_id, row in executed.items():
        _command_gate(row, gate_id)
        if any(row[key] != old_gates[gate_id][key] for key in ("kind", "command")):
            raise VCReceiptError("目标平台门禁命令与批准集合不同")
    gates = [executed.get(gate_id, old_gates[gate_id]) for gate_id in sorted(old_gates)]
    assertions = {key: source["assertions"][key] for key in ("git_commit", "source_tree_sha256", "target_architecture")}
    assertions.update({"gates": gates, "build_inputs": dict(current_inputs), "reuse": proof})
    return validate_facts({"schema_version": FACTS_SCHEMA, "kind": "implementation_tests",
        "subject": {**source["subject"], "candidate_id": target["candidate_id"]}, "assertions": assertions,
        "evidence": [] if proof["mode"] == "all" else [{"role": "implementation_tests", "path": "logs/implementation.log"}]})


def _replay(root: Path, receipt_relative: str, seen: set[Path]) -> dict[str, Any]:
    """重放收据自摘要及其逐文件证据绑定。"""

    evidence_root = _private_root(root)
    receipt_path = _inside(evidence_root, receipt_relative, "receipt")
    if receipt_path in seen or len(seen) >= 128:
        raise VCReceiptError("实现测试复用链循环或超出上限")
    seen = seen | {receipt_path}
    receipt = validate_receipt(_read_json(receipt_path, "receipt"), allow_historical=True)
    for item in receipt["evidence"]:
        path = _inside(evidence_root, item["path"], f"evidence.{item['role']}")
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != item["bytes"]
            or file_sha256(path) != item["sha256"]
        ):
            raise VCReceiptError(f"evidence.{item['role']} 摘要或大小漂移")
    if receipt["kind"] == "implementation_tests" and "reuse" in receipt["assertions"]:
        reuse = receipt["assertions"]["reuse"]
        target_path, target = _bound_json(reuse["target_revision"], "目标 revision")
        expected, source = _reuse_context(target_path, receipt["assertions"]["build_inputs"], seen)
        if expected != reuse or expected["mode"] == "full":
            raise VCReceiptError("实现测试复用声明与前序来源或输入差异不同")
        subject = {**source["subject"], "candidate_id": target["candidate_id"]}
        if subject != receipt["subject"]:
            raise VCReceiptError("实现测试复用未绑定当前 Candidate 或同一 Campaign")
        for key in ("git_commit", "source_tree_sha256", "target_architecture"):
            if receipt["assertions"][key] != source["assertions"][key]:
                raise VCReceiptError("实现测试复用改变了源码或平台")
        old_gates = {row["gate_id"]: row for row in source["assertions"]["gates"]}
        for row in receipt["assertions"]["gates"]:
            prior = old_gates[row["gate_id"]]
            if row["gate_id"] in reuse["reuse_gate_ids"]:
                if row != prior:
                    raise VCReceiptError("复用门禁结果与前序收据不同")
            elif any(row[key] != prior[key] for key in ("kind", "command")):
                raise VCReceiptError("目标平台门禁命令发生变化")
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    """重放原始证据及显式复用链；旧收据继续走原有逐文件绑定校验。"""

    return _replay(root, receipt_relative, set())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    finalize_parser = commands.add_parser("finalize", help="从阶段 facts 生成不可覆盖收据")
    finalize_parser.add_argument("--evidence-root", type=Path, required=True)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放阶段收据")
    replay_parser.add_argument("--evidence-root", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "finalize":
            receipt = finalize(arguments.evidence_root, arguments.facts, arguments.output)
        else:
            receipt = replay(arguments.evidence_root, arguments.receipt)
    except (OSError, VCReceiptError) as error:
        print(f"VC 阶段收据失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "kind": receipt["kind"],
                "receipt_digest": receipt["receipt_digest"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
