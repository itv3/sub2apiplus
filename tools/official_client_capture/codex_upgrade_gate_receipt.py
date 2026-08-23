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
from typing import Any


FACTS_SCHEMA = "codex-upgrade-external-gate-facts/v1"
RECEIPT_SCHEMA = "codex-upgrade-external-gate-receipt/v1"
PRODUCER_SCHEMA = "codex-upgrade-external-gate-producer/v1"
CANDIDATE_PHASE = "candidate_external"
POST_PROMOTION_PHASE = "post_promotion"
PHASES = frozenset({CANDIDATE_PHASE, POST_PROMOTION_PHASE})
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
POST_PROMOTION_COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "check-egress-spec": (".", ("make", "check-egress-spec")),
    "full-regression": (".", ("make", "test")),
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
    "target-platform": (".", ("make", "test")),
    "version-leak": (".", ("python3", "tools/check_version_leak.py")),
    "version-leak-self-test": (
        ".",
        ("python3", "tools/check_version_leak.py", "--self-test"),
    ),
}


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
            "candidate_id",
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
        "candidate_id": _safe_id(subject.get("candidate_id"), "subject.candidate_id"),
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


def _validate_inputs(
    root: Path,
    values: Any,
    phase: str,
    subject: dict[str, Any],
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
        if (
            acceptance.get("status") != "complete"
            or acceptance.get("accepted") is not True
            or acceptance.get("candidate_id") != subject["candidate_id"]
            or acceptance.get("target_version") != subject["target_version"]
            or acceptance.get("profile_id") != subject["profile_id"]
            or acceptance.get("profile_digest") != subject["profile_digest"]
            or acceptance.get("candidate_package_digest")
            != subject["candidate_package_digest"]
        ):
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
    return normalized


def _validate_gates(
    root: Path,
    values: Any,
    phase: str,
    architecture: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(values, list) or not values:
        raise GateReceiptError("gates 不能为空")
    contracts = CANDIDATE_COMMANDS if phase == CANDIDATE_PHASE else POST_PROMOTION_COMMANDS
    ids = [item.get("gate_id") for item in values if isinstance(item, dict)]
    if ids != sorted(contracts):
        raise GateReceiptError(f"{phase} gates 必须唯一且完整覆盖 {sorted(contracts)}")
    normalized: list[dict[str, Any]] = []
    latest: datetime | None = None
    latest_raw = ""
    for item in values:
        gate = _expect(
            item,
            {
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
            },
            "gate",
        )
        gate_id = gate["gate_id"]
        expected_cwd, expected_command = contracts[gate_id]
        if gate.get("working_directory") != expected_cwd or gate.get("command") != list(expected_command):
            raise GateReceiptError(f"门禁 {gate_id} 命令或工作目录不符合冻结合同")
        gate_architecture = gate.get("architecture")
        if not isinstance(gate_architecture, str) or not gate_architecture:
            raise GateReceiptError(f"门禁 {gate_id} architecture 为空")
        if gate_id == "target-platform" and gate_architecture != architecture:
            raise GateReceiptError("target-platform 未在候选目标架构执行")
        started = _rfc3339(gate.get("started_at_utc"), f"{gate_id}.started_at_utc")
        completed = _rfc3339(gate.get("completed_at_utc"), f"{gate_id}.completed_at_utc")
        if started > completed:
            raise GateReceiptError(f"门禁 {gate_id} 时间顺序非法")
        if (
            gate.get("exit_code") != 0
            or gate.get("status") != "passed"
            or not isinstance(gate.get("passed_count"), int)
            or isinstance(gate.get("passed_count"), bool)
            or gate.get("passed_count") <= 0
            or gate.get("failed_count") != 0
            or gate.get("skipped_count") != 0
        ):
            raise GateReceiptError(f"门禁 {gate_id} 未通过、存在失败或非预期跳过")
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
    return normalized, latest_raw


def build_receipt(root: Path, facts_relative: str) -> dict[str, Any]:
    root = _private_root(root)
    facts_path = _relative(root, facts_relative, "facts")
    facts, facts_raw = _load_json(facts_path, "facts")
    _expect(facts, {"schema_version", "phase", "subject", "inputs", "gates"}, "facts")
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise GateReceiptError("facts.schema_version 不匹配")
    phase = facts.get("phase")
    if phase not in PHASES:
        raise GateReceiptError("facts.phase 非法")
    subject = _validate_subject(facts.get("subject"), phase)
    inputs = _validate_inputs(root, facts.get("inputs"), phase, subject)
    gates, completed_at = _validate_gates(
        root,
        facts.get("gates"),
        phase,
        subject["target_architecture"],
    )
    tool_path = Path(__file__).resolve()
    return {
        "schema_version": RECEIPT_SCHEMA,
        "phase": phase,
        "subject": subject,
        "inputs": inputs,
        "gates": gates,
        "completed_at_utc": completed_at,
        "producer": {
            "schema_version": PRODUCER_SCHEMA,
            "tool": str(tool_path),
            "tool_sha256": _sha256_file(tool_path),
            "facts": {
                "path": facts_relative,
                "sha256": _sha256_bytes(facts_raw),
                "bytes": len(facts_raw),
            },
        },
    }


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


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    root = _private_root(root)
    receipt_path = _relative(root, receipt_relative, "receipt")
    receipt, raw = _load_json(receipt_path, "receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise GateReceiptError("receipt.schema_version 不匹配")
    producer = receipt.get("producer")
    if not isinstance(producer, dict) or producer.get("schema_version") != PRODUCER_SCHEMA:
        raise GateReceiptError("receipt.producer 不受支持")
    facts = producer.get("facts")
    if not isinstance(facts, dict) or not isinstance(facts.get("path"), str):
        raise GateReceiptError("receipt.producer.facts 缺失")
    expected = build_receipt(root, facts["path"])
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
                "status": "complete",
                "phase": result["phase"],
                "candidate_id": result["subject"]["candidate_id"],
                "receipt_sha256": _sha256_bytes(_canonical(result)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
