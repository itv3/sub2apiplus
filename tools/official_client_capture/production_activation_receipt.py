#!/usr/bin/env python3
"""从生产阶段事实生成可重放、不可覆盖的 Codex 激活收据。"""

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
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture import codex_upgrade_gate_receipt

FACTS_SCHEMA = "codex-production-activation-facts/v2"
RECEIPT_SCHEMA = "codex-production-activation-receipt/v2"
PRODUCER_SCHEMA = "codex-production-activation-producer/v2"
STAGE_ORDER = ("canary", "production_switch", "rollback", "target_restore")
TARGET_STAGES = frozenset({"canary", "production_switch", "target_restore"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$"
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ProductionReceiptError(ValueError):
    """生产事实不足以生成或重放可信激活收据。"""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionReceiptError(f"JSON 存在重复键：{key}")
        result[key] = value
    return result


def _canonical(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise ProductionReceiptError(f"输入文件不存在或不可信：{path}")
    content = path.read_bytes()
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProductionReceiptError(f"JSON 无法解析：{path}: {error}") from error
    if not isinstance(payload, dict):
        raise ProductionReceiptError(f"JSON 顶层必须是对象：{path}")
    return payload, content


def _require_private_root(root: Path) -> Path:
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ProductionReceiptError("evidence root 必须是现有的非符号链接绝对目录")
    resolved = root.resolve()
    if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
        raise ProductionReceiptError("evidence root 权限必须是 0700")
    return resolved


def _resolve_relative(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not relative or ".." in path.parts:
        raise ProductionReceiptError(f"证据路径必须是根内相对路径：{relative!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ProductionReceiptError(f"证据路径越过根目录：{relative!r}")
    return resolved


def _binding(root: Path, relative: str, expected_sha256: str | None = None) -> dict[str, Any]:
    path = _resolve_relative(root, relative)
    if not path.is_file() or path.is_symlink():
        raise ProductionReceiptError(f"证据文件不存在或不可信：{relative}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProductionReceiptError(f"证据文件权限必须是 0600：{relative}")
    digest = _sha256_file(path)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ProductionReceiptError(f"证据摘要不一致：{relative}")
    return {"path": relative, "sha256": digest, "bytes": path.stat().st_size}


def _expect_keys(payload: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ProductionReceiptError(f"{label} 字段不闭合：missing={missing}, extra={extra}")


def _require_string(payload: dict[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProductionReceiptError(f"{label}.{key} 必须是非空字符串")
    return value


def _require_sha(payload: dict[str, Any], key: str, label: str) -> str:
    value = _require_string(payload, key, label)
    if not SHA256_RE.fullmatch(value):
        raise ProductionReceiptError(f"{label}.{key} 不是 SHA-256")
    return value


def _validate_identity(payload: dict[str, Any], label: str) -> None:
    _expect_keys(
        payload,
        {
            "version",
            "profile_id",
            "profile_digest",
            "source_tree_sha256",
            "build_id",
            "deployed_version",
            "image_id",
            "image_reference",
        },
        label,
    )
    if not VERSION_RE.fullmatch(_require_string(payload, "version", label)):
        raise ProductionReceiptError(f"{label}.version 非法")
    _require_string(payload, "profile_id", label)
    _require_sha(payload, "profile_digest", label)
    _require_sha(payload, "source_tree_sha256", label)
    _require_string(payload, "build_id", label)
    _require_string(payload, "deployed_version", label)
    if not IMAGE_ID_RE.fullmatch(_require_string(payload, "image_id", label)):
        raise ProductionReceiptError(f"{label}.image_id 非法")
    if not IMAGE_REFERENCE_RE.fullmatch(
        _require_string(payload, "image_reference", label)
    ):
        raise ProductionReceiptError(f"{label}.image_reference 必须固定 registry digest")


def _validate_stage(
    stage: dict[str, Any],
    *,
    expected_name: str,
    target: dict[str, Any],
    rollback: dict[str, Any],
    root: Path,
) -> list[dict[str, Any]]:
    _expect_keys(
        stage,
        {
            "name",
            "started_at_utc",
            "completed_at_utc",
            "host",
            "architecture",
            "status",
            "image_id",
            "checks",
            "evidence",
        },
        f"stages.{expected_name}",
    )
    if stage.get("name") != expected_name or stage.get("status") != "pass":
        raise ProductionReceiptError(f"阶段 {expected_name} 未通过或顺序错误")
    for key in ("started_at_utc", "completed_at_utc"):
        if not RFC3339_RE.fullmatch(_require_string(stage, key, expected_name)):
            raise ProductionReceiptError(f"{expected_name}.{key} 不是 RFC3339")
    _require_string(stage, "host", expected_name)
    _require_string(stage, "architecture", expected_name)

    expected_identity = target if expected_name in TARGET_STAGES else rollback
    if stage.get("image_id") != expected_identity["image_id"]:
        raise ProductionReceiptError(f"阶段 {expected_name} 使用了错误镜像")
    checks = stage.get("checks")
    if not isinstance(checks, dict):
        raise ProductionReceiptError(f"{expected_name}.checks 必须是对象")
    required_checks = {
        "container_status": "running",
        "health": "pass",
        "active_version": expected_identity["version"],
        "profile_digest": expected_identity["profile_digest"],
        "fatal_log_count": 0,
        "guard_failure_count": 0,
    }
    for key, expected in required_checks.items():
        if checks.get(key) != expected:
            raise ProductionReceiptError(
                f"{expected_name}.checks.{key} 必须为 {expected!r}"
            )

    evidence = stage.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ProductionReceiptError(f"{expected_name}.evidence 不能为空")
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ProductionReceiptError(f"{expected_name}.evidence 项必须是对象")
        _expect_keys(item, {"path", "sha256"}, f"{expected_name}.evidence")
        relative = _require_string(item, "path", f"{expected_name}.evidence")
        digest = _require_sha(item, "sha256", f"{expected_name}.evidence")
        if relative in seen:
            raise ProductionReceiptError(f"阶段 {expected_name} 重复绑定证据：{relative}")
        seen.add(relative)
        bindings.append(_binding(root, relative, digest))
    return bindings


def build_receipt(root: Path, facts_relative: str) -> dict[str, Any]:
    root = _require_private_root(root)
    facts_path = _resolve_relative(root, facts_relative)
    facts, _ = _load_json(facts_path)
    _expect_keys(
        facts,
        {
            "schema_version",
            "campaign",
            "promotion",
            "post_promotion_gate",
            "target",
            "rollback",
            "stages",
            "final_state",
        },
        "facts",
    )
    if facts.get("schema_version") != FACTS_SCHEMA:
        raise ProductionReceiptError("facts.schema_version 不匹配")

    campaign = facts.get("campaign")
    if not isinstance(campaign, dict):
        raise ProductionReceiptError("facts.campaign 必须是对象")
    _expect_keys(
        campaign,
        {"id", "candidate_id", "acceptance_path", "acceptance_sha256"},
        "campaign",
    )
    _require_string(campaign, "id", "campaign")
    _require_string(campaign, "candidate_id", "campaign")
    acceptance_path = _require_string(campaign, "acceptance_path", "campaign")
    acceptance_sha256 = _require_sha(campaign, "acceptance_sha256", "campaign")
    acceptance = _binding(root, acceptance_path, acceptance_sha256)
    acceptance_payload, _ = _load_json(_resolve_relative(root, acceptance_path))
    if (
        acceptance_payload.get("status") != "complete"
        or acceptance_payload.get("accepted") is not True
        or acceptance_payload.get("candidate_id") != campaign["candidate_id"]
    ):
        raise ProductionReceiptError("acceptance 未完成、未接受或 candidate_id 不一致")

    target = facts.get("target")
    rollback = facts.get("rollback")
    if not isinstance(target, dict) or not isinstance(rollback, dict):
        raise ProductionReceiptError("target／rollback 必须是对象")
    _validate_identity(target, "target")
    _validate_identity(rollback, "rollback")
    if target["image_id"] == rollback["image_id"]:
        raise ProductionReceiptError("目标镜像与回滚镜像不能相同")

    promotion_reference = facts.get("promotion")
    if not isinstance(promotion_reference, dict):
        raise ProductionReceiptError("facts.promotion 必须是文件引用")
    _expect_keys(promotion_reference, {"path", "sha256"}, "promotion")
    promotion_path = _require_string(promotion_reference, "path", "promotion")
    promotion_sha256 = _require_sha(promotion_reference, "sha256", "promotion")
    promotion = _binding(root, promotion_path, promotion_sha256)
    promotion_payload, _ = _load_json(_resolve_relative(root, promotion_path))
    if (
        promotion_payload.get("schema_version")
        != "official-egress-catalog-promotion/v1"
        or promotion_payload.get("campaign_id") != campaign["id"]
        or promotion_payload.get("acceptance_sha256") != acceptance_sha256
        or promotion_payload.get("target_version") != target["version"]
        or promotion_payload.get("target_profile_digest")
        != target["profile_digest"]
        or promotion_payload.get("rollback_version") != rollback["version"]
        or promotion_payload.get("rollback_profile_digest")
        != rollback["profile_digest"]
        or promotion_payload.get("production_selector_changed") is not True
    ):
        raise ProductionReceiptError("promotion receipt 与验收或目标／回滚身份不一致")

    gate_reference = facts.get("post_promotion_gate")
    if not isinstance(gate_reference, dict):
        raise ProductionReceiptError("facts.post_promotion_gate 必须是文件引用")
    _expect_keys(gate_reference, {"path", "sha256"}, "post_promotion_gate")
    gate_path = _require_string(gate_reference, "path", "post_promotion_gate")
    gate_sha256 = _require_sha(gate_reference, "sha256", "post_promotion_gate")
    post_promotion_gate = _binding(root, gate_path, gate_sha256)
    try:
        gate_payload = codex_upgrade_gate_receipt.replay(root, gate_path)
    except (OSError, codex_upgrade_gate_receipt.GateReceiptError) as error:
        raise ProductionReceiptError(
            f"post-promotion 门禁收据无法独立重放：{error}"
        ) from error
    gate_subject = gate_payload.get("subject")
    candidate_identity = acceptance_payload.get("candidate_identity")
    if not isinstance(gate_subject, dict) or not isinstance(candidate_identity, dict):
        raise ProductionReceiptError("acceptance 或 post-promotion 门禁缺少候选身份")
    expected_gate_subject = {
        "campaign_id": campaign["id"],
        "candidate_id": campaign["candidate_id"],
        "target_version": target["version"],
        "profile_id": target["profile_id"],
        "profile_digest": target["profile_digest"],
        "candidate_package_digest": acceptance_payload.get(
            "candidate_package_digest"
        ),
        "candidate_source_tree_sha256": candidate_identity.get(
            "source_tree_sha256"
        ),
        "candidate_image_id": candidate_identity.get("image_id"),
        "candidate_image_reference": candidate_identity.get("image_reference"),
        "production_tree_sha256": target["source_tree_sha256"],
        "acceptance_sha256": acceptance_sha256,
        "promotion_receipt_sha256": promotion_sha256,
    }
    if (
        gate_payload.get("phase")
        != codex_upgrade_gate_receipt.POST_PROMOTION_PHASE
        or any(
            gate_subject.get(key) != value
            for key, value in expected_gate_subject.items()
        )
    ):
        raise ProductionReceiptError(
            "post-promotion 门禁与 Campaign、candidate、promotion 或 production tree 不一致"
        )

    stages = facts.get("stages")
    if not isinstance(stages, list) or len(stages) != len(STAGE_ORDER):
        raise ProductionReceiptError("stages 必须完整覆盖四阶段")
    normalized_stages: list[dict[str, Any]] = []
    previous_completed: datetime | None = None
    for expected_name, stage in zip(STAGE_ORDER, stages, strict=True):
        if not isinstance(stage, dict):
            raise ProductionReceiptError(f"阶段 {expected_name} 必须是对象")
        evidence = _validate_stage(
            stage,
            expected_name=expected_name,
            target=target,
            rollback=rollback,
            root=root,
        )
        started = datetime.fromisoformat(stage["started_at_utc"].replace("Z", "+00:00"))
        completed = datetime.fromisoformat(
            stage["completed_at_utc"].replace("Z", "+00:00")
        )
        if started > completed or (
            previous_completed is not None and started < previous_completed
        ):
            raise ProductionReceiptError(f"阶段 {expected_name} 时间顺序非法")
        previous_completed = completed
        normalized_stages.append({**stage, "evidence": evidence})
    gate_completed = datetime.fromisoformat(
        gate_payload["completed_at_utc"].replace("Z", "+00:00")
    )
    first_stage_started = datetime.fromisoformat(
        stages[0]["started_at_utc"].replace("Z", "+00:00")
    )
    if gate_completed > first_stage_started:
        raise ProductionReceiptError("post-promotion 门禁必须先于 canary 完成")
    target_architecture = gate_subject.get("target_architecture")
    if any(
        stage["architecture"] != target_architecture
        for stage in stages
        if stage["name"] in TARGET_STAGES
    ):
        raise ProductionReceiptError("post-promotion 目标架构与激活阶段不一致")

    final_state = facts.get("final_state")
    if not isinstance(final_state, dict):
        raise ProductionReceiptError("final_state 必须是对象")
    _expect_keys(
        final_state,
        {
            "candidate_id",
            "image_id",
            "active_version",
            "profile_id",
            "profile_digest",
            "container_status",
            "health",
        },
        "final_state",
    )
    expected_final = {
        "candidate_id": campaign["candidate_id"],
        "image_id": target["image_id"],
        "active_version": target["version"],
        "profile_id": target["profile_id"],
        "profile_digest": target["profile_digest"],
        "container_status": "running",
        "health": "pass",
    }
    if final_state != expected_final:
        raise ProductionReceiptError("final_state 未与目标 candidate 完整对齐")

    facts_binding = _binding(root, facts_relative)
    tool_path = Path(__file__).resolve()
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "campaign": {
            "id": campaign["id"],
            "candidate_id": campaign["candidate_id"],
            "acceptance": acceptance,
        },
        "promotion": promotion,
        "post_promotion_gate": post_promotion_gate,
        "target": target,
        "rollback": rollback,
        "stages": normalized_stages,
        "final_state": final_state,
        "completed_at_utc": stages[-1]["completed_at_utc"],
        "producer": {
            "schema_version": PRODUCER_SCHEMA,
            "tool": str(tool_path),
            "tool_sha256": _sha256_file(tool_path),
            "facts": facts_binding,
        },
    }
    return receipt


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ProductionReceiptError(f"输出已存在，禁止覆盖：{path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
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
    root = _require_private_root(root)
    output = _resolve_relative(root, output_relative)
    receipt = build_receipt(root, facts_relative)
    _write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    root = _require_private_root(root)
    receipt_path = _resolve_relative(root, receipt_relative)
    receipt, content = _load_json(receipt_path)
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ProductionReceiptError("receipt.schema_version 不匹配")
    producer = receipt.get("producer")
    if not isinstance(producer, dict) or producer.get("schema_version") != PRODUCER_SCHEMA:
        raise ProductionReceiptError("receipt.producer 不受支持")
    facts = producer.get("facts")
    if not isinstance(facts, dict):
        raise ProductionReceiptError("receipt.producer.facts 缺失")
    facts_path = _require_string(facts, "path", "receipt.producer.facts")
    expected = build_receipt(root, facts_path)
    if _canonical(expected) != content:
        raise ProductionReceiptError("收据重放结果不一致")
    return receipt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    finalize_parser = subparsers.add_parser("finalize", help="生成不可覆盖的生产激活收据")
    finalize_parser.add_argument("--evidence-root", required=True, type=Path)
    finalize_parser.add_argument("--facts", required=True)
    finalize_parser.add_argument("--output", required=True)
    replay_parser = subparsers.add_parser("replay", help="从原始事实重放并校验收据")
    replay_parser.add_argument("--evidence-root", required=True, type=Path)
    replay_parser.add_argument("--receipt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "finalize":
            receipt = finalize(arguments.evidence_root, arguments.facts, arguments.output)
        else:
            receipt = replay(arguments.evidence_root, arguments.receipt)
    except (OSError, ProductionReceiptError) as error:
        print(f"生产激活收据失败：{error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "complete",
                "candidate_id": receipt["campaign"]["candidate_id"],
                "receipt_sha256": _sha256_bytes(_canonical(receipt)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
