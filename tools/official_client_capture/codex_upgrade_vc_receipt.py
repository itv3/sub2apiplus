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


def _validate_p0_assertions(value: Any) -> dict[str, Any]:
    assertions = _expect(
        value,
        {
            "offline_gates",
            "tool_blockers",
            "campaign_run_rehearsal",
            "rollback_ready",
            "job_rehearsal_sha256",
        },
        "P0 assertions",
    )
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
    if assertions["rollback_ready"] is not True:
        raise VCReceiptError("P0 回退点不可用")
    _sha256(assertions["job_rehearsal_sha256"], "P0 job_rehearsal_sha256")
    assertions["offline_gates"] = normalized
    return assertions


def _validate_implementation_assertions(value: Any) -> dict[str, Any]:
    assertions = _expect(
        value,
        {"git_commit", "source_tree_sha256", "target_architecture", "gates"},
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


def _validate_assertions(kind: str, purpose: str, value: Any) -> dict[str, Any]:
    if kind == "p0_gate":
        return _validate_p0_assertions(value)
    if kind == "implementation_tests":
        return _validate_implementation_assertions(value)
    if kind == "private_archive":
        return _validate_archive_assertions(value)
    if kind == "cleanup_decision":
        return _validate_cleanup_assertions(value)
    return _validate_completion_assertions(kind, purpose, value)


def _expected_roles(kind: str, purpose: str, assertions: Mapping[str, Any]) -> set[str]:
    if kind == "p0_gate":
        return {
            "campaign_run_rehearsal",
            "check_egress_spec",
            "job_rehearsal",
            "rollback",
            "test_capture_tools",
        }
    if kind == "implementation_tests":
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


def validate_receipt(value: Any) -> dict[str, Any]:
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
    assertions = _validate_assertions(kind, subject["campaign_purpose"], receipt["assertions"])
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


def finalize(root: Path, facts_relative: str, output_relative: str) -> dict[str, Any]:
    """绑定 facts 声明的全部小型证明文件并生成收据。"""

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
    receipt = build_receipt(facts, bindings)
    output = evidence_root / _relative_path(output_relative, "output")
    try:
        output.resolve(strict=False).relative_to(evidence_root)
    except ValueError as error:
        raise VCReceiptError("output 越出 evidence-root") from error
    _write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    """重放收据自摘要及其逐文件证据绑定。"""

    evidence_root = _private_root(root)
    receipt_path = _inside(evidence_root, receipt_relative, "receipt")
    receipt = validate_receipt(_read_json(receipt_path, "receipt"))
    for item in receipt["evidence"]:
        path = _inside(evidence_root, item["path"], f"evidence.{item['role']}")
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != item["bytes"]
            or file_sha256(path) != item["sha256"]
        ):
            raise VCReceiptError(f"evidence.{item['role']} 摘要或大小漂移")
    return receipt


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
