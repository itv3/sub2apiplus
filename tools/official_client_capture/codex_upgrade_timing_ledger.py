#!/usr/bin/env python3
"""维护并重放 Codex 官方客户端升级的只追加 UpgradeTimingLedger。"""

from __future__ import annotations

import argparse
import fcntl
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


PLAN_SCHEMA = "codex-upgrade-timing-ledger-plan/v1"
EVENT_SCHEMA = "codex-upgrade-timing-ledger-event/v1"
RECEIPT_SCHEMA = "codex-upgrade-timing-ledger-receipt/v1"
PRODUCER_SCHEMA = "codex-upgrade-timing-ledger-producer/v1"
PRODUCER_VERSION = "1"
PHASE_ORDER = ("VC-0", "VC-1", "VC-2", "VC-3", "VC-4", "VC-5", "VC-6")
DEFAULT_STAGE_BUDGETS = {
    "VC-0": 45,
    "VC-1": 75,
    "VC-2": 75,
    "VC-3": 75,
    "VC-4": 90,
    "VC-5": 75,
    "VC-6": 75,
}
DEFAULT_TOTAL_BUDGET_MINUTES = 360
DEFAULT_RETRY_LIMIT = 2
PURPOSES = frozenset({"validation_only", "production_replacement"})
EVIDENCE_DECISIONS = frozenset({"reuse", "recapture"})
EVENT_TYPES = frozenset(
    {
        "stage_started",
        "stage_completed",
        "stage_abandoned",
        "attempt_started",
        "attempt_failed",
        "attempt_completed",
        "receipt_passed",
        "stop_the_line",
        "recovery_verified",
        "upgrade_completed",
    }
)
RECOVERY_ROLES = ("clean_p0", "offline_regression", "tool_fix")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
MAX_JSON_BYTES = 4 * 1024 * 1024
PRODUCER_TOOL_RELATIVE = "tools/official_client_capture/codex_upgrade_timing_ledger.py"
CURRENT_WORKTREE_SUCCESSOR_RELATIVE = (
    "docs/egress/maintenance/codex-cli-0151-worktree-successor.json"
)
PRODUCER_SUCCESSOR_TRANSITIONS = (
    {
        "path": "docs/egress/maintenance/codex-cli-0151-container-path-recovery-tool-successor-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-container-path-recovery-tool-successor-source-transition/v1",
        "base_commit": "432a4dfb9dc612b0343ed217b8dace587698fc37",
        "scope": "codex-cli-0.151-container-path-recovery-tool-successor",
        "result": "passed_codex_cli_0151_container_path_recovery_tool_successor",
    },
    {
        "path": "docs/egress/maintenance/codex-cli-0151-timing-producer-replay-tool-successor-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-timing-producer-replay-tool-successor-source-transition/v1",
        "base_commit": "990c26f955fde57817fe1e0e98862d01c3ec5f7d",
        "scope": "codex-cli-0.151-timing-producer-replay-tool-successor",
        "result": "passed_codex_cli_0151_timing_producer_replay_tool_successor",
    },
    {
        "path": "docs/egress/maintenance/codex-cli-0151-producer-coordinate-decoupling-source-transition.json",
        "schema_version": "sub2apiplus-codex-cli-0151-producer-coordinate-decoupling-source-transition/v1",
        "base_commit": "b4c3b58ea13a3aa85ed22e68586cfe368b0c9d88",
        "scope": "codex-cli-0.151-producer-coordinate-decoupling",
        "result": "passed_codex_cli_0151_producer_coordinate_decoupling",
    },
)
# 通用 freeze successor 也会改变计时工具摘要。这里逐份列出允许参与历史
# UpgradeTimingLedger 重放的收据；运行时只沿这些显式文件中的精确边前进，
# 不扫描 maintenance 目录，也不接受未登记摘要。
PRODUCER_FREEZE_SUCCESSORS = (
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc1-general-toolchain-closeout-20260915-freeze-successor.json",
        "base_commit": "92e82aade5f69ffd253045478e554c13c1fe2ab6",
        "scope": "upstream-codex-0154-vc1-general-toolchain-closeout-20260915-freeze-successor",
        "result": "manual_actions_required",
    },
    {
        "path": "docs/egress/maintenance/upstream-codex-0154-vc1-timing-producer-chain-closeout-20260915-freeze-successor.json",
        "base_commit": "a50cb75af108ac45ce954c43da61b2cdc32263fe",
        "scope": "upstream-codex-0154-vc1-timing-producer-chain-closeout-20260915-freeze-successor",
        "result": "manual_actions_required",
    },
)


class TimingLedgerError(ValueError):
    """计时台账不完整、超时、发生漂移或违反重试纪律。"""


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise TimingLedgerError(f"{label}不是带时区 RFC3339 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TimingLedgerError(f"{label}不是有效时间") from error
    if parsed.tzinfo is None:
        raise TimingLedgerError(f"{label}缺少时区")
    return parsed.astimezone(timezone.utc)


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise TimingLedgerError(f"{label}不是安全标识")
    return value


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TimingLedgerError(f"{label}必须是对象")
    actual = set(value)
    if actual != fields:
        raise TimingLedgerError(
            f"{label}字段不闭合：缺失={sorted(fields - actual)}，"
            f"多余={sorted(actual - fields)}"
        )
    return value


def _private_ledger(path: Path, *, must_exist: bool) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise TimingLedgerError("ledger dir 必须是非符号链接绝对路径")
    if must_exist:
        if not path.is_dir():
            raise TimingLedgerError("ledger dir 不存在")
        resolved = path.resolve(strict=True)
        if stat.S_IMODE(resolved.stat().st_mode) != 0o700:
            raise TimingLedgerError("ledger dir 权限必须是 0700")
        return resolved
    if path.exists():
        raise TimingLedgerError("ledger dir 已存在，禁止覆盖")
    parent = path.parent.resolve(strict=True)
    if path.parent.is_symlink() or not parent.is_dir():
        raise TimingLedgerError("ledger dir 父目录不可信")
    return path


def _relative(root: Path, value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise TimingLedgerError(f"{label}必须是 ledger 内 POSIX 相对路径")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise TimingLedgerError(f"{label}路径不规范")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise TimingLedgerError(f"{label}路径包含符号链接")
    try:
        current.resolve(strict=current.exists()).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TimingLedgerError(f"{label}越过 ledger dir") from error
    return current


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise TimingLedgerError(f"{label}不是可信普通文件")
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise TimingLedgerError(f"{label}权限必须是 0600")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise TimingLedgerError(f"{label}大小非法")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError(f"{label}不是合法 UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise TimingLedgerError(f"{label}顶层必须是对象")
    return payload, raw


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise TimingLedgerError(f"输出已存在，禁止覆盖：{path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise TimingLedgerError("输出父目录不可信")
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise TimingLedgerError("输出父目录权限必须是 0700")
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


def _producer() -> dict[str, str]:
    tool = Path(__file__).resolve()
    return {
        "schema_version": PRODUCER_SCHEMA,
        "tool": str(tool),
        "tool_sha256": _sha256_file(tool),
        "version": PRODUCER_VERSION,
    }


def _repository_file(root: Path, relative: Any, label: str) -> Path:
    """解析并约束 Git 工作树内的只读来源文件。"""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise TimingLedgerError(f"{label}不是规范仓库相对路径")
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or str(parsed) != relative
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise TimingLedgerError(f"{label}不是规范仓库相对路径")
    current = root
    for part in parsed.parts:
        current /= part
        if current.is_symlink():
            raise TimingLedgerError(f"{label}包含符号链接")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise TimingLedgerError(f"{label}越过仓库根或不存在") from error
    metadata = resolved.stat()
    if not resolved.is_file() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise TimingLedgerError(f"{label}不是只读普通文件")
    if metadata.st_size <= 0 or metadata.st_size > MAX_JSON_BYTES:
        raise TimingLedgerError(f"{label}大小非法")
    return resolved


def _load_producer_successor_edge(
    repository_root: Path,
    descriptor: dict[str, str],
) -> tuple[str, str]:
    """重放一个已登记来源 transition 中的计时工具精确摘要边。"""

    transition_path = _repository_file(
        repository_root,
        descriptor["path"],
        "producer successor transition",
    )
    raw = transition_path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("producer successor transition 不是合法 JSON") from error
    transition = _expect(
        payload,
        {
            "schema_version",
            "issued_at_utc",
            "base_commit",
            "scope",
            "predecessor",
            "transitions",
            "additions",
            "verification",
            "safety",
            "result",
            "identity_sha256",
        },
        "producer successor transition",
    )
    for field in ("schema_version", "base_commit", "scope", "result"):
        if transition.get(field) != descriptor[field]:
            raise TimingLedgerError(f"producer successor transition {field} 漂移")
    _timestamp(transition.get("issued_at_utc"), "producer successor issued_at_utc")
    identity = transition.get("identity_sha256")
    unsigned = dict(transition)
    unsigned.pop("identity_sha256")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("producer successor transition 自摘要非法")
    if _sha256_bytes(_canonical(unsigned)) != identity:
        raise TimingLedgerError("producer successor transition 自摘要不一致")
    predecessor = _expect(
        transition.get("predecessor"),
        {"kind", "path", "sha256"},
        "producer successor predecessor",
    )
    predecessor_path = _repository_file(
        repository_root,
        predecessor.get("path"),
        "producer successor predecessor.path",
    )
    predecessor_sha256 = predecessor.get("sha256")
    if (
        not isinstance(predecessor_sha256, str)
        or not SHA256_RE.fullmatch(predecessor_sha256)
        or _sha256_file(predecessor_path) != predecessor_sha256
    ):
        raise TimingLedgerError("producer successor predecessor 摘要不一致")
    safety = _expect(
        transition.get("safety"),
        {
            "live_account_used",
            "online_acceptance_performed",
            "production_config_changed",
            "official_egress_profile_changed",
        },
        "producer successor safety",
    )
    if any(safety.values()):
        raise TimingLedgerError("producer successor transition 超出离线工具修复边界")
    entries = transition.get("transitions")
    if not isinstance(entries, list):
        raise TimingLedgerError("producer successor transitions 不是数组")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("producer successor transition 未唯一登记计时工具")
    edge = _expect(
        matches[0],
        {"path", "from_sha256", "to_sha256", "reason"},
        "producer successor tool edge",
    )
    before = edge.get("from_sha256")
    after = edge.get("to_sha256")
    if (
        not isinstance(before, str)
        or not SHA256_RE.fullmatch(before)
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or before == after
        or not isinstance(edge.get("reason"), str)
        or not edge["reason"].strip()
    ):
        raise TimingLedgerError("producer successor tool edge 非法")
    return before, after


def _load_freeze_successor_edge(
    repository_root: Path,
    descriptor: dict[str, str],
) -> tuple[str, str]:
    """重放一份显式登记的通用 freeze successor 计时工具摘要边。"""

    receipt_path = _repository_file(
        repository_root,
        descriptor["path"],
        "producer freeze successor",
    )
    try:
        payload = json.loads(receipt_path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("producer freeze successor 不是合法 JSON") from error
    receipt = _expect(
        payload,
        {
            "schema_version",
            "issued_at_utc",
            "base_commit",
            "current_commit",
            "scope",
            "mode",
            "extra_worktree_paths",
            "frozen_path_count",
            "frozen_edge_count",
            "changed_path_count",
            "transitions",
            "unregistered_path_count",
            "unregistered_paths",
            "deleted_frozen_paths",
            "required_manual_actions",
            "verification",
            "safety",
            "result",
            "identity_sha256",
        },
        "producer freeze successor",
    )
    expected_fields = {
        "schema_version": "official-egress-upstream-freeze-successor/v1",
        "base_commit": descriptor["base_commit"],
        "scope": descriptor["scope"],
        "mode": "commit",
        "result": descriptor["result"],
    }
    for field, expected in expected_fields.items():
        if receipt.get(field) != expected:
            raise TimingLedgerError(f"producer freeze successor {field} 漂移")
    _timestamp(receipt.get("issued_at_utc"), "producer freeze successor issued_at_utc")
    current_commit = receipt.get("current_commit")
    if (
        not isinstance(current_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", current_commit)
        or current_commit == descriptor["base_commit"]
    ):
        raise TimingLedgerError("producer freeze successor current_commit 非法")
    identity = receipt.get("identity_sha256")
    unsigned = dict(receipt)
    unsigned.pop("identity_sha256")
    compact = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("producer freeze successor 自摘要非法")
    if _sha256_bytes(compact) != identity:
        raise TimingLedgerError("producer freeze successor 自摘要不一致")
    safety = _expect(
        receipt.get("safety"),
        {
            "deployment_performed",
            "live_account_used",
            "official_egress_profile_changed",
            "production_config_changed",
            "wire_or_persona_selection_changed",
        },
        "producer freeze successor safety",
    )
    if any(safety.values()):
        raise TimingLedgerError("producer freeze successor 超出离线工具修复边界")
    if receipt.get("deleted_frozen_paths") != []:
        raise TimingLedgerError("producer freeze successor 删除了冻结路径")
    entries = receipt.get("transitions")
    if not isinstance(entries, list):
        raise TimingLedgerError("producer freeze successor transitions 不是数组")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("producer freeze successor 未唯一登记计时工具")
    edge = _expect(
        matches[0],
        {
            "path",
            "old_path",
            "status",
            "predecessor_sha256s",
            "to_sha256",
            "source_receipts",
            "reason",
        },
        "producer freeze successor tool edge",
    )
    predecessors = edge.get("predecessor_sha256s")
    after = edge.get("to_sha256")
    if (
        edge.get("old_path") != ""
        or edge.get("status") != "M"
        or not isinstance(predecessors, list)
        or len(predecessors) != 1
        or not isinstance(predecessors[0], str)
        or not SHA256_RE.fullmatch(predecessors[0])
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or predecessors[0] == after
        or not isinstance(edge.get("source_receipts"), list)
        or not edge["source_receipts"]
        or not all(isinstance(item, str) and item for item in edge["source_receipts"])
        or not isinstance(edge.get("reason"), str)
        or not edge["reason"].strip()
    ):
        raise TimingLedgerError("producer freeze successor tool edge 非法")
    return predecessors[0], after


def _producer_tool_coordinate(value: Any) -> tuple[str, ...] | None:
    """提取 producer 的规范相对坐标，忽略工作树根目录。"""

    if not isinstance(value, str) or not value or not value.startswith("/"):
        return None
    try:
        parsed = PurePosixPath(value)
        parts = parsed.parts
    except (TypeError, ValueError):
        return None
    relative = tuple(PurePosixPath(PRODUCER_TOOL_RELATIVE).parts)
    if (
        str(parsed) != value
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) < len(relative)
        or parts[-len(relative) :] != relative
    ):
        return None
    return relative


def _load_current_worktree_successor_edge(
    repository_root: Path,
) -> tuple[str, str] | None:
    """读取 0.151 工作区快照对计时工具追加的初始摘要边。"""

    path = _repository_file(
        repository_root,
        CURRENT_WORKTREE_SUCCESSOR_RELATIVE,
        "current worktree successor",
    )
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeError, json.JSONDecodeError) as error:
        raise TimingLedgerError("current worktree successor 不是合法 JSON") from error
    if payload.get("schema_version") != "sub2apiplus-codex-cli-0151-worktree-successor/v1":
        raise TimingLedgerError("current worktree successor schema 漂移")
    identity = payload.get("identity_sha256")
    unsigned = dict(payload)
    unsigned.pop("identity_sha256", None)
    pretty = (json.dumps(unsigned, ensure_ascii=False, indent=2) + "\n").encode()
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise TimingLedgerError("current worktree successor 自摘要非法")
    if _sha256_bytes(pretty) != identity:
        raise TimingLedgerError("current worktree successor 自摘要不一致")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise TimingLedgerError("current worktree successor entries 非法")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") == PRODUCER_TOOL_RELATIVE
    ]
    if len(matches) != 1:
        raise TimingLedgerError("current worktree successor 未唯一登记计时工具")
    entry = matches[0]
    before = (entry.get("before") or {}).get("sha256")
    after = (entry.get("after") or {}).get("sha256")
    if (
        not isinstance(before, str)
        or not SHA256_RE.fullmatch(before)
        or not isinstance(after, str)
        or not SHA256_RE.fullmatch(after)
        or before == after
    ):
        raise TimingLedgerError("current worktree successor 计时工具摘要边非法")
    return before, after


def _producer_identity_matches(frozen: Any, current: dict[str, str]) -> bool:
    """按规范坐标和内容摘要承接历史 producer，不绑定工作树绝对根。"""

    if frozen == current:
        return True
    if not isinstance(frozen, dict) or set(frozen) != set(current):
        return False
    for field in ("schema_version", "version"):
        if frozen.get(field) != current[field]:
            return False
    # 旧台账可能来自已删除的 worktree；绝对根是运行坐标，不是 producer
    # 身份。仍要求两端都落在同一个受管相对路径，避免任意文件被冒充。
    if _producer_tool_coordinate(frozen.get("tool")) is None or _producer_tool_coordinate(
        current.get("tool")
    ) is None:
        return False
    frozen_sha256 = frozen.get("tool_sha256")
    if not isinstance(frozen_sha256, str) or not SHA256_RE.fullmatch(frozen_sha256):
        return False
    if frozen_sha256 == current["tool_sha256"]:
        # 内容相同即可证明同一 producer；工作树根目录仍只按下面的
        # 规范相对坐标校验，不把路径变化误判成工具漂移。
        return True
    tool = Path(current["tool"]).resolve()
    repository_root = tool.parents[2]
    if tool != repository_root / PRODUCER_TOOL_RELATIVE:
        return False
    edges = [
        _load_producer_successor_edge(repository_root, descriptor)
        for descriptor in PRODUCER_SUCCESSOR_TRANSITIONS
    ]
    current_edge = _load_current_worktree_successor_edge(repository_root)
    if current_edge is not None:
        edges.append(current_edge)
    edges.extend(
        _load_freeze_successor_edge(repository_root, descriptor)
        for descriptor in PRODUCER_FREEZE_SUCCESSORS
    )
    if not edges:
        return False
    # 每份旧台账都必须沿已登记的摘要边走到当前工具。历史边可以分叉，
    # 但从一个具体前序摘要出发不得出现两条不同后继。
    successors: dict[str, str] = {}
    for before, after in edges:
        if after == before or (before in successors and successors[before] != after):
            return False
        successors[before] = after
    visited: set[str] = set()
    node = frozen_sha256
    while node != current["tool_sha256"]:
        if node in visited or node not in successors:
            return False
        visited.add(node)
        node = successors[node]
    return True


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    _expect(
        plan,
        {
            "schema_version",
            "upgrade_id",
            "created_at_utc",
            "started_at_utc",
            "baseline_version",
            "target_version",
            "campaign_purpose",
            "evidence_decision",
            "total_budget_minutes",
            "stage_budgets_minutes",
            "same_root_cause_retry_limit",
            "producer",
        },
        "ledger plan",
    )
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise TimingLedgerError("ledger plan schema_version 不匹配")
    _safe_id(plan.get("upgrade_id"), "upgrade_id")
    created = _timestamp(plan.get("created_at_utc"), "created_at_utc")
    started = _timestamp(plan.get("started_at_utc"), "started_at_utc")
    if created != started:
        raise TimingLedgerError("计时必须从台账创建时连续开始")
    for field in ("baseline_version", "target_version"):
        if not isinstance(plan.get(field), str) or not VERSION_RE.fullmatch(plan[field]):
            raise TimingLedgerError(f"{field} 不是三段式版本号")
    if plan["baseline_version"] == plan["target_version"]:
        raise TimingLedgerError("baseline 与 target 版本不得相同")
    if plan.get("campaign_purpose") not in PURPOSES:
        raise TimingLedgerError("campaign_purpose 非法")
    if plan.get("evidence_decision") not in EVIDENCE_DECISIONS:
        raise TimingLedgerError("P0 必须冻结唯一 reuse／recapture 决定")
    total = plan.get("total_budget_minutes")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0 or total > 360:
        raise TimingLedgerError("总墙钟预算必须为 1～360 分钟")
    budgets = plan.get("stage_budgets_minutes")
    if not isinstance(budgets, dict) or list(budgets) != list(PHASE_ORDER):
        raise TimingLedgerError("阶段预算必须按 VC-0～VC-6 完整排序")
    for phase, default in DEFAULT_STAGE_BUDGETS.items():
        value = budgets.get(phase)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > default:
            raise TimingLedgerError(f"{phase} 预算必须为正数且不得宽于文档上限 {default}")
    if plan.get("same_root_cause_retry_limit") != DEFAULT_RETRY_LIMIT:
        raise TimingLedgerError("同根因重试上限必须固定为 2")
    if not _producer_identity_matches(plan.get("producer"), _producer()):
        raise TimingLedgerError("计时台账生成器身份漂移")
    return plan


def _load_plan(root: Path) -> tuple[dict[str, Any], bytes]:
    plan, raw = _load_json(root / "ledger.json", "ledger plan")
    return _validate_plan(plan), raw


def _validate_binding(root: Path, value: Any, label: str) -> dict[str, Any]:
    binding = _expect(value, {"role", "path", "sha256"}, label)
    role = _safe_id(binding.get("role"), f"{label}.role")
    path = _relative(root, binding.get("path"), f"{label}.path")
    expected = binding.get("sha256")
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        raise TimingLedgerError(f"{label}.sha256 非法")
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != expected:
        raise TimingLedgerError(f"{label}引用缺失或摘要漂移")
    return {
        "role": role,
        "path": binding["path"],
        "sha256": expected,
        "bytes": path.stat().st_size,
    }


def _load_events(root: Path, *, limit: int | None = None) -> list[tuple[dict[str, Any], bytes]]:
    events_root = root / "events"
    if not events_root.is_dir() or events_root.is_symlink():
        raise TimingLedgerError("events 目录缺失或不可信")
    paths = sorted(events_root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise TimingLedgerError("events 目录只能包含普通文件")
    expected_names = [f"{index:06d}.json" for index in range(1, len(paths) + 1)]
    if [path.name for path in paths] != expected_names:
        raise TimingLedgerError("event 序号不连续或存在额外文件")
    if limit is not None:
        if limit <= 0 or limit > len(paths):
            raise TimingLedgerError("receipt 绑定的 event head 越界")
        paths = paths[:limit]
    return [_load_json(path, f"event {path.name}") for path in paths]


def _validate_event_shape(root: Path, event: dict[str, Any], sequence: int) -> dict[str, Any]:
    _expect(
        event,
        {
            "schema_version",
            "sequence",
            "event_id",
            "recorded_at_utc",
            "phase",
            "event_type",
            "attempt_id",
            "root_cause_id",
            "live_request_count",
            "receipts",
            "next_action",
            "previous_event_sha256",
        },
        f"event {sequence}",
    )
    if event.get("schema_version") != EVENT_SCHEMA or event.get("sequence") != sequence:
        raise TimingLedgerError(f"event {sequence} schema 或序号不一致")
    _safe_id(event.get("event_id"), f"event {sequence}.event_id")
    _timestamp(event.get("recorded_at_utc"), f"event {sequence}.recorded_at_utc")
    if event.get("phase") not in PHASE_ORDER or event.get("event_type") not in EVENT_TYPES:
        raise TimingLedgerError(f"event {sequence} phase 或 event_type 非法")
    for field in ("attempt_id", "root_cause_id"):
        value = event.get(field)
        if value is not None:
            _safe_id(value, f"event {sequence}.{field}")
    count = event.get("live_request_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise TimingLedgerError(f"event {sequence}.live_request_count 非法")
    receipts = event.get("receipts")
    if not isinstance(receipts, list):
        raise TimingLedgerError(f"event {sequence}.receipts 必须是数组")
    roles = [item.get("role") for item in receipts if isinstance(item, dict)]
    if roles != sorted(roles) or len(set(roles)) != len(roles):
        raise TimingLedgerError(f"event {sequence}.receipts 必须按 role 唯一排序")
    normalized = [_validate_binding(root, item, f"event {sequence}.receipts") for item in receipts]
    if event["event_type"] == "recovery_verified" and tuple(roles) != RECOVERY_ROLES:
        raise TimingLedgerError("recovery_verified 必须绑定工具修复、离线回归和干净 P0 三份收据")
    if event["event_type"] != "recovery_verified" and roles and event["event_type"] not in {"receipt_passed", "stop_the_line", "upgrade_completed"}:
        raise TimingLedgerError(f"{event['event_type']} 不接受 receipts")
    next_action = event.get("next_action")
    if next_action is not None and (not isinstance(next_action, str) or not next_action.strip()):
        raise TimingLedgerError(f"event {sequence}.next_action 非法")
    previous = event.get("previous_event_sha256")
    if sequence == 1:
        if previous is not None:
            raise TimingLedgerError("首个 event 的 previous_event_sha256 必须为空")
    elif not isinstance(previous, str) or not SHA256_RE.fullmatch(previous):
        raise TimingLedgerError(f"event {sequence}.previous_event_sha256 非法")
    return {**event, "receipts": normalized}


def _summarize(
    root: Path,
    plan: dict[str, Any],
    raw_events: list[tuple[dict[str, Any], bytes]],
    *,
    as_of: datetime,
) -> dict[str, Any]:
    if not raw_events:
        raise TimingLedgerError("UpgradeTimingLedger 至少需要一个 event")
    event_ids: set[str] = set()
    attempts: dict[str, dict[str, Any]] = {}
    failure_counts: dict[str, int] = {}
    active_phase: str | None = None
    active_phase_started: datetime | None = None
    stopped = False
    completed = False
    total_live_requests = 0
    last_time: datetime | None = None
    previous_raw: bytes | None = None
    last_successful_receipt: dict[str, Any] | None = None
    last_event: dict[str, Any] | None = None
    for sequence, (event, raw) in enumerate(raw_events, 1):
        normalized = _validate_event_shape(root, event, sequence)
        recorded = _timestamp(normalized["recorded_at_utc"], "event time")
        if recorded < _timestamp(plan["started_at_utc"], "started_at_utc"):
            raise TimingLedgerError("event 早于台账开始时间")
        if last_time is not None and recorded < last_time:
            raise TimingLedgerError("event 时间发生倒退")
        if previous_raw is not None and normalized["previous_event_sha256"] != _sha256_bytes(previous_raw):
            raise TimingLedgerError("event 摘要链断裂")
        if normalized["event_id"] in event_ids:
            raise TimingLedgerError("event_id 重复")
        event_ids.add(normalized["event_id"])
        event_type = normalized["event_type"]
        phase = normalized["phase"]
        if completed:
            raise TimingLedgerError("upgrade_completed 后禁止追加 event")
        if stopped and event_type != "recovery_verified":
            raise TimingLedgerError("stop_the_line 后只能记录 recovery_verified")
        if event_type == "stage_started":
            if active_phase is not None or normalized["attempt_id"] is not None or normalized["root_cause_id"] is not None:
                raise TimingLedgerError("stage_started 身份或阶段状态非法")
            active_phase = phase
            active_phase_started = recorded
        elif event_type == "stage_completed":
            if active_phase != phase or any(item["status"] == "active" for item in attempts.values()):
                raise TimingLedgerError("stage_completed 与当前阶段或 attempt 状态不一致")
            active_phase = None
            active_phase_started = None
        elif event_type == "stage_abandoned":
            if (
                active_phase != phase
                or any(item["status"] == "active" for item in attempts.values())
                or normalized["attempt_id"] is not None
                or normalized["root_cause_id"] is None
                or not normalized["next_action"]
            ):
                raise TimingLedgerError(
                    "stage_abandoned 必须关闭当前阶段、登记根因和唯一下一动作"
                )
            active_phase = None
            active_phase_started = None
        elif event_type == "attempt_started":
            attempt_id = normalized["attempt_id"]
            if active_phase != phase or attempt_id is None or attempt_id in attempts:
                raise TimingLedgerError("attempt_started 与当前阶段或 attempt 身份不一致")
            cause = normalized["root_cause_id"]
            if cause is not None and failure_counts.get(cause, 0) >= plan["same_root_cause_retry_limit"]:
                raise TimingLedgerError("同一根因已连续失败两次，禁止第三次 attempt")
            attempts[attempt_id] = {"status": "active", "root_cause_id": cause}
        elif event_type in {"attempt_failed", "attempt_completed"}:
            attempt_id = normalized["attempt_id"]
            if attempt_id is None or attempts.get(attempt_id, {}).get("status") != "active":
                raise TimingLedgerError(f"{event_type} 没有对应的 active attempt")
            if event_type == "attempt_failed":
                cause = normalized["root_cause_id"]
                if cause is None:
                    raise TimingLedgerError("attempt_failed 必须登记 root_cause_id")
                attempts[attempt_id] = {"status": "failed", "root_cause_id": cause}
                failure_counts[cause] = failure_counts.get(cause, 0) + 1
            else:
                attempts[attempt_id]["status"] = "completed"
        elif event_type == "stop_the_line":
            if not normalized["next_action"]:
                raise TimingLedgerError("stop_the_line 必须冻结唯一下一动作")
            stopped = True
        elif event_type == "recovery_verified":
            cause = normalized["root_cause_id"]
            if not stopped or cause is None or failure_counts.get(cause, 0) < plan["same_root_cause_retry_limit"]:
                raise TimingLedgerError("recovery_verified 没有对应的两次同根因失败停线")
            failure_counts[cause] = 0
            stopped = False
        elif event_type == "upgrade_completed":
            if active_phase != phase or any(item["status"] == "active" for item in attempts.values()):
                raise TimingLedgerError("upgrade_completed 时仍有未关闭阶段或 attempt")
            completed = True
        if normalized["receipts"]:
            last_successful_receipt = normalized["receipts"][-1]
        total_live_requests += normalized["live_request_count"]
        last_time = recorded
        previous_raw = raw
        last_event = normalized
    assert last_event is not None and last_time is not None and previous_raw is not None
    if as_of < last_time:
        raise TimingLedgerError("检查时间早于最新 event")
    started = _timestamp(plan["started_at_utc"], "started_at_utc")
    total_elapsed = max(0, int((as_of - started).total_seconds()))
    total_deadline = started + timedelta(minutes=plan["total_budget_minutes"])
    stage_elapsed = None
    stage_deadline = None
    if active_phase is not None and active_phase_started is not None:
        stage_elapsed = max(0, int((as_of - active_phase_started).total_seconds()))
        stage_deadline = active_phase_started + timedelta(
            minutes=plan["stage_budgets_minutes"][active_phase]
        )
    budget_exceeded = as_of >= total_deadline or (
        stage_deadline is not None and as_of >= stage_deadline
    )
    retry_stop_required = any(
        count >= plan["same_root_cause_retry_limit"] for count in failure_counts.values()
    )
    status = (
        "complete"
        if completed
        else "stopped"
        if stopped
        else "stop_required"
        if budget_exceeded or retry_stop_required
        else "active"
    )
    return {
        "status": status,
        "upgrade_id": plan["upgrade_id"],
        "baseline_version": plan["baseline_version"],
        "target_version": plan["target_version"],
        "campaign_purpose": plan["campaign_purpose"],
        "evidence_decision": plan["evidence_decision"],
        "active_phase": active_phase,
        "head_sequence": len(raw_events),
        "head_sha256": _sha256_bytes(previous_raw),
        "total_elapsed_seconds": total_elapsed,
        "stage_elapsed_seconds": stage_elapsed,
        "total_deadline_at_utc": total_deadline.isoformat(timespec="seconds"),
        "stage_deadline_at_utc": (
            stage_deadline.isoformat(timespec="seconds") if stage_deadline else None
        ),
        "total_live_request_count": total_live_requests,
        "same_root_cause_failures": dict(sorted(failure_counts.items())),
        "last_successful_receipt": last_successful_receipt,
        "last_event_id": last_event["event_id"],
        "next_action": last_event["next_action"],
    }


def inspect_ledger(root: Path, *, now: str | None = None, limit: int | None = None) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, _ = _load_plan(root)
    events = _load_events(root, limit=limit)
    observed = _timestamp(now or _utc_now(), "检查时间")
    return _summarize(root, plan, events, as_of=observed)


def create_ledger(
    root: Path,
    *,
    upgrade_id: str,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
    evidence_decision: str,
    started_at_utc: str | None = None,
    total_budget_minutes: int = DEFAULT_TOTAL_BUDGET_MINUTES,
    stage_budgets_minutes: dict[str, int] | None = None,
) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=False)
    started = started_at_utc or _utc_now()
    plan = {
        "schema_version": PLAN_SCHEMA,
        "upgrade_id": upgrade_id,
        "created_at_utc": started,
        "started_at_utc": started,
        "baseline_version": baseline_version,
        "target_version": target_version,
        "campaign_purpose": campaign_purpose,
        "evidence_decision": evidence_decision,
        "total_budget_minutes": total_budget_minutes,
        "stage_budgets_minutes": stage_budgets_minutes or dict(DEFAULT_STAGE_BUDGETS),
        "same_root_cause_retry_limit": DEFAULT_RETRY_LIMIT,
        "producer": _producer(),
    }
    _validate_plan(plan)
    root.mkdir(mode=0o700)
    (root / "events").mkdir(mode=0o700)
    (root / "receipts").mkdir(mode=0o700)
    _write_once(root / "ledger.json", plan)
    initial = {
        "schema_version": EVENT_SCHEMA,
        "sequence": 1,
        "event_id": "doc-pre-p0-started",
        "recorded_at_utc": started,
        "phase": "VC-0",
        "event_type": "stage_started",
        "attempt_id": None,
        "root_cause_id": None,
        "live_request_count": 0,
        "receipts": [],
        "next_action": "完成 DOC-PRE／P0 并清零工具阻断",
        "previous_event_sha256": None,
    }
    _validate_event_shape(root, initial, 1)
    _write_once(root / "events" / "000001.json", initial)
    return inspect_ledger(root, now=started)


def append_event(
    root: Path,
    *,
    event_id: str,
    phase: str,
    event_type: str,
    attempt_id: str | None = None,
    root_cause_id: str | None = None,
    live_request_count: int = 0,
    receipts: list[dict[str, str]] | None = None,
    next_action: str | None = None,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, _ = _load_plan(root)
    raw_events = _load_events(root)
    recorded_raw = recorded_at_utc or _utc_now()
    recorded = _timestamp(recorded_raw, "recorded_at_utc")
    current = _summarize(root, plan, raw_events, as_of=recorded)
    allowed_while_stopping = {"stop_the_line", "recovery_verified"}
    # 阶段或总预算已经要求停线时，仍必须先把 active 阶段显式废弃，随后才能
    # 写 stop_the_line；否则父编排器失败会永久留下 active/VC-x 假象。
    if current["status"] == "stop_required":
        allowed_while_stopping.add("stage_abandoned")
        # metadata-only attempt_failed：只登记失败与根因，不带收据、不计请求，
        # 不伴随任何 Job 执行。没有它，stop_required 下的 active attempt 永远关不掉，
        # stage_abandoned 也就永远写不进去。
        if event_type == "attempt_failed" and not receipts and live_request_count == 0:
            allowed_while_stopping.add("attempt_failed")
    if current["status"] in {"stop_required", "stopped"} and event_type not in allowed_while_stopping:
        raise TimingLedgerError("计时或重试门禁已要求停线，禁止继续追加执行事件")
    sequence = len(raw_events) + 1
    event = {
        "schema_version": EVENT_SCHEMA,
        "sequence": sequence,
        "event_id": event_id,
        "recorded_at_utc": recorded_raw,
        "phase": phase,
        "event_type": event_type,
        "attempt_id": attempt_id,
        "root_cause_id": root_cause_id,
        "live_request_count": live_request_count,
        "receipts": sorted(receipts or [], key=lambda item: item["role"]),
        "next_action": next_action,
        "previous_event_sha256": _sha256_bytes(raw_events[-1][1]),
    }
    candidate_raw = _canonical(event)
    _summarize(root, plan, [*raw_events, (event, candidate_raw)], as_of=recorded)
    _write_once(root / "events" / f"{sequence:06d}.json", event)
    return inspect_ledger(root, now=recorded_raw)


def build_checkpoint(root: Path, *, observed_at_utc: str | None = None) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    plan, plan_raw = _load_plan(root)
    events = _load_events(root)
    observed = observed_at_utc or _utc_now()
    summary = _summarize(
        root, plan, events, as_of=_timestamp(observed, "observed_at_utc")
    )
    return {
        "schema_version": RECEIPT_SCHEMA,
        "observed_at_utc": observed,
        "ledger_plan": {
            "path": "ledger.json",
            "sha256": _sha256_bytes(plan_raw),
            "bytes": len(plan_raw),
        },
        "event_head": {
            "sequence": summary["head_sequence"],
            "sha256": summary["head_sha256"],
        },
        "summary": summary,
        "producer": _producer(),
    }


def checkpoint(root: Path, output_relative: str) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    output = _relative(root, output_relative, "checkpoint output")
    receipt = build_checkpoint(root)
    _write_once(output, receipt)
    return receipt


def replay(root: Path, receipt_relative: str) -> dict[str, Any]:
    root = _private_ledger(root, must_exist=True)
    path = _relative(root, receipt_relative, "checkpoint receipt")
    receipt, raw = _load_json(path, "checkpoint receipt")
    _expect(
        receipt,
        {"schema_version", "observed_at_utc", "ledger_plan", "event_head", "summary", "producer"},
        "checkpoint receipt",
    )
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise TimingLedgerError("checkpoint receipt schema_version 不匹配")
    plan_binding = _expect(receipt.get("ledger_plan"), {"path", "sha256", "bytes"}, "ledger_plan")
    plan_path = _relative(root, plan_binding.get("path"), "ledger_plan.path")
    if (
        plan_path != root / "ledger.json"
        or plan_binding.get("sha256") != _sha256_file(plan_path)
        or plan_binding.get("bytes") != plan_path.stat().st_size
    ):
        raise TimingLedgerError("checkpoint 绑定的 ledger plan 漂移")
    head = _expect(receipt.get("event_head"), {"sequence", "sha256"}, "event_head")
    sequence = head.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise TimingLedgerError("checkpoint event head 非法")
    plan, plan_raw = _load_plan(root)
    events = _load_events(root, limit=sequence)
    summary = _summarize(
        root,
        plan,
        events,
        as_of=_timestamp(receipt.get("observed_at_utc"), "observed_at_utc"),
    )
    expected = {
        "schema_version": RECEIPT_SCHEMA,
        "observed_at_utc": receipt["observed_at_utc"],
        "ledger_plan": {
            "path": "ledger.json",
            "sha256": _sha256_bytes(plan_raw),
            "bytes": len(plan_raw),
        },
        "event_head": {"sequence": sequence, "sha256": summary["head_sha256"]},
        "summary": summary,
        "producer": receipt.get("producer"),
    }
    if (
        not _producer_identity_matches(receipt.get("producer"), _producer())
        or head.get("sha256") != summary["head_sha256"]
        or _canonical(expected) != raw
    ):
        raise TimingLedgerError("UpgradeTimingLedger checkpoint 重放结果不一致")
    return receipt


def assert_usable(
    root: Path,
    receipt_relative: str,
    *,
    baseline_version: str,
    target_version: str,
    campaign_purpose: str,
    required_phase: str | None = None,
) -> dict[str, Any]:
    """重放冻结 checkpoint，并以当前墙钟确认台账仍可继续。"""

    receipt = replay(root, receipt_relative)
    summary = inspect_ledger(root)
    expected = {
        "baseline_version": baseline_version,
        "target_version": target_version,
        "campaign_purpose": campaign_purpose,
    }
    if any(summary.get(key) != value for key, value in expected.items()):
        raise TimingLedgerError("UpgradeTimingLedger 与 Campaign 版本或用途不一致")
    if summary["status"] != "active":
        raise TimingLedgerError(f"UpgradeTimingLedger 当前状态为 {summary['status']}，必须停线")
    if required_phase is not None and summary["active_phase"] != required_phase:
        raise TimingLedgerError(
            f"UpgradeTimingLedger 当前阶段为 {summary['active_phase']}，要求 {required_phase}"
        )
    if receipt["summary"]["status"] != "active":
        raise TimingLedgerError("冻结 checkpoint 在生成时已非 active")
    return summary


def _receipt_arguments(values: list[str]) -> list[dict[str, str]]:
    receipts: list[dict[str, str]] = []
    for value in values:
        role, separator, path = value.partition("=")
        if not separator or not role or not path:
            raise TimingLedgerError("--receipt 必须为 ROLE=RELATIVE_PATH")
        receipts.append({"role": role, "path": path, "sha256": ""})
    return receipts


LEDGER_CLOSE_SCHEMA = "ledger-close/v1"
PROVENANCE_RECEIPT_SCHEMA = "live-request-provenance/v2"
CLOSE_LOCK_NAME = ".vc0-closeout.lock"
DEFAULT_CLOSE_NEXT_ACTION = (
    "账本已按统一计量口径关闭；后续只能由项目总账登记、复用导入或普通后继 Campaign 承接"
)


@contextlib.contextmanager
def _close_lock(root: Path) -> Iterator[None]:
    """与 VC-0 收口共用同一把账本目录锁，串行化关闭与收口。"""

    lock_path = root / CLOSE_LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise TimingLedgerError("账本锁文件不可信或无法创建") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise TimingLedgerError("账本锁文件身份不可信")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _active_attempts(raw_events: list[tuple[dict[str, Any], bytes]]) -> list[tuple[str, str]]:
    """按事件顺序重放 attempt 状态，返回仍 active 的 (attempt_id, phase)。"""

    active: dict[str, str] = {}
    for event, _raw in raw_events:
        attempt_id = event.get("attempt_id")
        event_type = event.get("event_type")
        if event_type == "attempt_started" and isinstance(attempt_id, str):
            active[attempt_id] = str(event.get("phase"))
        elif event_type in {"attempt_failed", "attempt_completed"} and isinstance(attempt_id, str):
            active.pop(attempt_id, None)
    return sorted(active.items())


def _load_provenance_receipt(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise TimingLedgerError("provenance 收据必须是可信绝对普通文件")
    payload, raw = _load_json(path, "provenance 收据")
    if payload.get("schema_version") != PROVENANCE_RECEIPT_SCHEMA:
        raise TimingLedgerError("provenance 收据 schema 不是 live-request-provenance/v2")
    for field in (
        "formal_campaign_id",
        "status",
        "precise_total",
        "estimated_total",
        "estimation_policy",
        "counting_rule",
        "identity_keys_sha256",
    ):
        if field not in payload:
            raise TimingLedgerError(f"provenance 收据缺少 {field}")
    for field in ("precise_total", "estimated_total"):
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TimingLedgerError(f"provenance 收据 {field} 非法")
    if payload["status"] not in {"complete", "accounting_unresolved"}:
        raise TimingLedgerError("provenance 收据 status 非法")
    return payload, raw


def _publish_once(path: Path, payload: dict[str, Any], label: str) -> None:
    """同一内容重复发布视为幂等；内容不同则失败关闭。"""

    if path.exists():
        existing, _raw = _load_json(path, label)
        if _canonical(existing) != _canonical(payload):
            raise TimingLedgerError(f"{label}已存在且内容不同：{path.name}")
        return
    _write_once(path, payload)


def close_campaign_ledger(
    root: Path,
    *,
    root_cause_id: str,
    provenance_receipt: Path,
    next_action: str | None = None,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """按统一计量口径关闭一个未停线的 Campaign 计时账本（A0a-8）。

    顺序固定且每步幂等：先给仍 active 的 attempt 追加 metadata-only ``attempt_failed``，
    再 ``stage_abandoned`` 关闭 active 阶段，最后 ``stop_the_line`` 绑定 provenance 收据
    与关闭收据。``stop_the_line`` 的 ``live_request_count`` 只记 ``unaccounted_delta``：
    统一口径重算的精确加估计总数减去账本此前累计；重算结果小于旧累计时记 0 并在
    关闭收据里如实标 ``delta_clamped``，历史混合口径数字不再被改写。
    """

    root = _private_ledger(root, must_exist=True)
    _safe_id(root_cause_id, "root_cause_id")
    action = next_action or DEFAULT_CLOSE_NEXT_ACTION
    provenance, provenance_raw = _load_provenance_receipt(Path(provenance_receipt))
    provenance_sha256 = _sha256_bytes(provenance_raw)
    with _close_lock(root):
        plan, _ = _load_plan(root)
        raw_events = _load_events(root)
        now = recorded_at_utc or _utc_now()
        summary = _summarize(root, plan, raw_events, as_of=_timestamp(now, "recorded_at_utc"))
        if summary["status"] in {"stopped", "complete"}:
            return {"status": "already-closed", "ledger_status": summary["status"], "summary": summary}
        previous_count = int(summary["total_live_request_count"])
        precise = int(provenance["precise_total"])
        estimated = int(provenance["estimated_total"])
        recomputed_total = precise + estimated
        delta = recomputed_total - previous_count
        recorded_delta = max(delta, 0)
        close_root = root / "receipts" / "ledger-close"
        if close_root.is_symlink():
            raise TimingLedgerError("关闭收据目录不可信")
        if not close_root.exists():
            close_root.mkdir(mode=0o700)
        elif stat.S_IMODE(close_root.stat().st_mode) != 0o700:
            raise TimingLedgerError("关闭收据目录权限必须是 0700")
        tag = provenance_sha256[:16]
        provenance_copy = close_root / f"provenance-{tag}.json"
        _publish_once(provenance_copy, provenance, "provenance 收据副本")
        close_receipt = {
            "schema_version": LEDGER_CLOSE_SCHEMA,
            "upgrade_id": plan["upgrade_id"],
            "formal_campaign_id": provenance["formal_campaign_id"],
            "root_cause_id": root_cause_id,
            "closed_at_utc": now,
            "counting_rule": provenance["counting_rule"],
            "estimation_policy": provenance["estimation_policy"],
            "provenance_status": provenance["status"],
            "provenance_receipt_sha256": provenance_sha256,
            "identity_keys_sha256": provenance["identity_keys_sha256"],
            "unresolved_job_ids": list(provenance.get("unresolved_job_ids", [])),
            "previous_count": previous_count,
            "precise_count": precise,
            "estimated_count": estimated,
            "recomputed_total": recomputed_total,
            "unaccounted_delta": delta,
            "delta_clamped": delta < 0,
            "live_request_count_recorded": recorded_delta,
            "resulting_total": previous_count + recorded_delta,
        }
        close_path = close_root / f"ledger-close-{tag}.json"
        if close_path.exists():
            existing, _raw = _load_json(close_path, "关闭收据")
            comparable = {k: v for k, v in existing.items() if k != "closed_at_utc"}
            if comparable != {k: v for k, v in close_receipt.items() if k != "closed_at_utc"}:
                raise TimingLedgerError("关闭收据已存在且账务不同，拒绝覆盖")
        else:
            _write_once(close_path, close_receipt)
        bindings = sorted(
            [
                {
                    "role": "ledger_close",
                    "path": close_path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(close_path),
                },
                {
                    "role": "provenance",
                    "path": provenance_copy.relative_to(root).as_posix(),
                    "sha256": _sha256_file(provenance_copy),
                },
            ],
            key=lambda item: item["role"],
        )
        appended: list[str] = []
        existing_ids = {event["event_id"] for event, _raw in raw_events}
        for attempt_id, phase in _active_attempts(raw_events):
            event_id = f"close-attempt-failed-{attempt_id}"
            if event_id in existing_ids:
                continue
            append_event(
                root,
                event_id=event_id,
                phase=phase,
                event_type="attempt_failed",
                attempt_id=attempt_id,
                root_cause_id=root_cause_id,
                next_action=action,
            )
            appended.append(event_id)
        summary = _summarize(root, plan, _load_events(root), as_of=_timestamp(_utc_now(), "now"))
        active_phase = summary.get("active_phase")
        if active_phase is not None:
            event_id = f"close-stage-abandoned-{active_phase}"
            if event_id not in existing_ids:
                append_event(
                    root,
                    event_id=event_id,
                    phase=str(active_phase),
                    event_type="stage_abandoned",
                    root_cause_id=root_cause_id,
                    next_action=action,
                )
                appended.append(event_id)
        raw_events = _load_events(root)
        summary = _summarize(root, plan, raw_events, as_of=_timestamp(_utc_now(), "now"))
        if summary["status"] != "stopped":
            last_phase = str(raw_events[-1][0]["phase"])
            append_event(
                root,
                event_id="close-stop-the-line",
                phase=last_phase,
                event_type="stop_the_line",
                root_cause_id=root_cause_id,
                live_request_count=recorded_delta,
                receipts=bindings,
                next_action=action,
            )
            appended.append("close-stop-the-line")
        final = inspect_ledger(root)
        if final["status"] != "stopped":
            raise TimingLedgerError("关闭后账本状态不是 stopped")
        return {
            "status": "closed",
            "ledger_status": final["status"],
            "appended_event_ids": appended,
            "ledger_close_receipt": bindings[0],
            "provenance_receipt": bindings[1],
            "previous_count": previous_count,
            "unaccounted_delta": delta,
            "resulting_total": int(final["total_live_request_count"]),
            "summary": final,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create", help="创建只写一次的 UpgradeTimingLedger")
    create_parser.add_argument("--ledger-dir", type=Path, required=True)
    create_parser.add_argument("--upgrade-id", required=True)
    create_parser.add_argument("--baseline-version", required=True)
    create_parser.add_argument("--target-version", required=True)
    create_parser.add_argument("--campaign-purpose", choices=sorted(PURPOSES), required=True)
    create_parser.add_argument("--evidence-decision", choices=sorted(EVIDENCE_DECISIONS), required=True)
    create_parser.add_argument("--total-budget-minutes", type=int, default=DEFAULT_TOTAL_BUDGET_MINUTES)
    append_parser = commands.add_parser("append", help="追加一个不可覆盖的计时事件")
    append_parser.add_argument("--ledger-dir", type=Path, required=True)
    append_parser.add_argument("--event-id", required=True)
    append_parser.add_argument("--phase", choices=PHASE_ORDER, required=True)
    append_parser.add_argument("--event-type", choices=sorted(EVENT_TYPES), required=True)
    append_parser.add_argument("--attempt-id")
    append_parser.add_argument("--root-cause-id")
    append_parser.add_argument("--live-request-count", type=int, default=0)
    append_parser.add_argument("--receipt", action="append", default=[])
    append_parser.add_argument("--next-action")
    checkpoint_parser = commands.add_parser("checkpoint", help="封存当前 event head 的可重放 checkpoint")
    checkpoint_parser.add_argument("--ledger-dir", type=Path, required=True)
    checkpoint_parser.add_argument("--output", required=True)
    replay_parser = commands.add_parser("replay", help="独立重放历史 checkpoint")
    replay_parser.add_argument("--ledger-dir", type=Path, required=True)
    replay_parser.add_argument("--receipt", required=True)
    status_parser = commands.add_parser("status", help="按当前墙钟只读检查计时状态")
    status_parser.add_argument("--ledger-dir", type=Path, required=True)
    close_parser = commands.add_parser(
        "close-campaign-ledger", help="按统一计量口径关闭未停线的 Campaign 计时账本"
    )
    close_parser.add_argument("--ledger-dir", type=Path, required=True)
    close_parser.add_argument("--root-cause", required=True, help="A0a-3 结构化根因 ID")
    close_parser.add_argument("--provenance-receipt", type=Path, required=True)
    close_parser.add_argument("--next-action")
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create":
            result = create_ledger(
                arguments.ledger_dir,
                upgrade_id=arguments.upgrade_id,
                baseline_version=arguments.baseline_version,
                target_version=arguments.target_version,
                campaign_purpose=arguments.campaign_purpose,
                evidence_decision=arguments.evidence_decision,
                total_budget_minutes=arguments.total_budget_minutes,
            )
        elif arguments.command == "append":
            bindings = _receipt_arguments(arguments.receipt)
            root = _private_ledger(arguments.ledger_dir, must_exist=True)
            for binding in bindings:
                path = _relative(root, binding["path"], "receipt")
                if not path.is_file() or path.is_symlink():
                    raise TimingLedgerError(f"receipt 不存在：{binding['path']}")
                binding["sha256"] = _sha256_file(path)
            result = append_event(
                root,
                event_id=arguments.event_id,
                phase=arguments.phase,
                event_type=arguments.event_type,
                attempt_id=arguments.attempt_id,
                root_cause_id=arguments.root_cause_id,
                live_request_count=arguments.live_request_count,
                receipts=bindings,
                next_action=arguments.next_action,
            )
        elif arguments.command == "checkpoint":
            result = checkpoint(arguments.ledger_dir, arguments.output)["summary"]
        elif arguments.command == "replay":
            result = replay(arguments.ledger_dir, arguments.receipt)["summary"]
        elif arguments.command == "close-campaign-ledger":
            result = close_campaign_ledger(
                arguments.ledger_dir,
                root_cause_id=arguments.root_cause,
                provenance_receipt=arguments.provenance_receipt.resolve(),
                next_action=arguments.next_action,
            )
        else:
            result = inspect_ledger(arguments.ledger_dir)
    except (OSError, TimingLedgerError) as error:
        print(f"UpgradeTimingLedger 失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
