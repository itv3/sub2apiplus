"""追加式项目总账、Campaign outbox batch、admission 与消费者门禁、修复收据。

方案 A0a-9～A0a-13。总账目录 ``<data_root>/upgrade-project-ledger/``：

* ``plan.json`` 只写一次，冻结老板批准的绝对截止时间与估计政策、可选请求预算、
  同根因重试上限、根因枚举表摘要与算法版本、``fixture_only``、初始请求数（精确与估计
  分列）、初始身份键清单摘要（清单本体在 ``initial-identity-keys.json``）、初始根因
  计数、``bootstrap_cutover``（已吸收的 Campaign 账本 head、batch SHA、operation ID、
  身份键清单 SHA，补齐器不得再推送）、三个账本关闭后的 head、时间对账与处置清单收据
  摘要，以及自摘要 ``plan_sha256``。
* ``events/NNNNNN.json`` 摘要链追加，事件类型只有六种：``campaign_registered``、
  ``campaign_registration_rejected``、``reconciliation_committed``、``accounting_resolved``、
  ``root_cause_repaired``、``campaign_terminal``。每个事件带 ``operation_id``，重复推送时
  核对类型与 payload 摘要；每个事件反向绑定来源 batch SHA。
* ``head.json`` 只是缓存：写入时在目录锁内完整重放得到 head，并以 head SHA 做 CAS；
  缓存缺失或落后可重建，超前或同序号不符失败关闭。

请求入账：``reconciliation_committed`` 的请求部分状态为 ``resolved``、``estimated`` 或
``unresolved``；身份键逐条与 ``accounted_identity_index`` 及初始清单比对，重复不计数并
记录；``unresolved`` 把 operation 加入未决集合并置 blocked；``accounting_resolved`` 绑定
原 operation、新 provenance 审计收据、准确身份键清单与 delta，原子补账并移除该 operation，
集合清空才解除 blocked。blocked 时仍允许 ``accounting_resolved``、``root_cause_repaired``、
``reconciliation_committed``、``campaign_terminal``；禁止注册、派发、resume、复用、seal。

Campaign 侧 ``<campaign_dir>/ledger/``：``plan.json`` 记 ``registration_operation_id`` 与
``admission_head_sha256``；``outbox/batch-NNNNNN/`` 内是若干 ``entry-NN.json`` 与 ``COMMIT``。
一个 batch 严格对应一个项目事件，entry 只是该事件 payload 的分片（列表字段拼接、标量
字段必须一致）。补齐器 ``reconcile-project-ledger`` 只推送带 COMMIT 的 batch；COMMIT 后
追加 entry、缺项、断链、篡改即失败关闭；跳过 ``bootstrap_cutover`` 已列出的 batch。

注册事务：锁顺序固定为先项目锁后 Campaign 锁，持有 Campaign 锁时不得再取项目锁。
``plan`` 在项目锁内重放 head 得到 ``admission_head_sha256`` 并执行 admission（未 blocked、
截止未到、剩余预算大于 0、根因未达上限、同版本未终态 formal 数量未达上限），再取
Campaign 锁写 ``ledger/plan.json``、写注册 batch 并 COMMIT、追加 ``campaign_registered``
并 CAS。CAS 失败或中断后，补齐器补写时在当前 head 上重新执行全部 admission；不通过则
追加 ``campaign_registration_rejected``，原 entry 永不修改。只有 ``ledger/plan.json`` 而
无 COMMIT 注册 batch 的 Campaign 视为未创建，被所有消费者拒绝；注册可重复执行，幂等。

消费者门禁 ``assert_campaign_admitted``：``plan``、``reuse-official-evidence``、
``campaign-run``、``resume``、``capture-official seal`` 开始前先执行补齐器，再锁内重放
总账；确认本 Campaign 注册事件存在且无对应拒绝事件与终态事件；总账 blocked、剩余预算
为 0、根因达上限任一成立即拒绝；``fixture_only`` 总账只允许 ``<data_root>/staging`` 下的
Campaign；Campaign deadline 不超过总账绝对截止。总账按祖先目录查找（最多六层），生产
规范布局 ``<data_root>/evidence/campaigns/<id>`` 与 staging 布局都能命中。

修复收据 ``root-cause-repair/v1``：代码缺陷绑定修复提交 SHA、定向回归测试收据、对应
部署收据；环境缺陷绑定环境修复收据与干净环境复核收据。经总账自己的
``repairs/outbox/batch-NNNNNN/`` 推送 ``root_cause_repaired``，不属于任何 Campaign，不激活
历史 Campaign。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

if __package__ in {None, ""}:
    import codex_upgrade_root_cause as root_cause
else:
    from . import codex_upgrade_root_cause as root_cause

PLAN_SCHEMA = "upgrade-project-ledger-plan/v1"
EVENT_SCHEMA = "upgrade-project-ledger-event/v1"
HEAD_SCHEMA = "upgrade-project-ledger-head/v1"
CAMPAIGN_PLAN_SCHEMA = "upgrade-campaign-ledger-plan/v1"
ENTRY_SCHEMA = "upgrade-outbox-entry/v2"
COMMIT_SCHEMA = "upgrade-outbox-commit/v2"
REPAIR_SCHEMA = "root-cause-repair/v1"
RECONCILE_REPORT_SCHEMA = "project-ledger-reconcile/v1"
LEDGER_DIR_NAME = "upgrade-project-ledger"
CAMPAIGN_LEDGER_DIR_NAME = "ledger"
STAGING_DIR_NAME = "staging"
LOCK_NAME = ".project-ledger.lock"
EVENT_TYPES = (
    "campaign_registered",
    "campaign_registration_rejected",
    "reconciliation_committed",
    "accounting_resolved",
    "root_cause_repaired",
    "campaign_terminal",
)
TERMINAL_REASONS = (
    "deadline_wall_clock",
    "deadline_live_requests",
    "root_cause_limit",
    "accounting_unresolved",
    "environment_contaminated",
    "identity_changed",
    "superseded",
)
REQUEST_STATUSES = ("resolved", "estimated", "unresolved")
BLOCKED_ALLOWED_EVENTS = frozenset(
    {"accounting_resolved", "root_cause_repaired", "reconciliation_committed", "campaign_terminal"}
)
CONSUMER_COMMANDS = frozenset({"plan", "reuse-official-evidence", "campaign-run", "resume", "seal"})
ESTIMATION_POLICIES = ("none", "upper_bound_from_sibling", "upper_bound_from_sibling_or_turn_ratio")
REPAIR_KINDS = ("code", "environment")
DEFAULT_RETRY_LIMIT = 2
# 与 codex_upgrade.VC_ARTIFACT_MIN_VERSION 一致：0.154.0 起 formal Campaign 必须在总账内。
PROJECT_LEDGER_MIN_VERSION = (0, 154, 0)
DEFAULT_FORMAL_OPEN_LIMIT = 2
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_ANCESTOR_DEPTH = 6
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
BATCH_DIR_RE = re.compile(r"^batch-(\d{6})$")
ENTRY_FILE_RE = re.compile(r"^entry-(\d{2})\.json$")
LIST_PAYLOAD_FIELDS = frozenset({"identity_keys"})


class ProjectLedgerError(ValueError):
    """总账、batch 或门禁被破坏或拒绝。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    return _sha256(_canonical(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise ProjectLedgerError(f"{label}不是 RFC3339 时间戳")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ProjectLedgerError(f"{label}非法：{value!r}")
    return value


def _sha_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ProjectLedgerError(f"{label}不是小写 SHA-256")
    return value


def _count(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectLedgerError(f"{label}必须是非负整数")
    return value


def _expect(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        missing = sorted(fields - set(value)) if isinstance(value, dict) else sorted(fields)
        extra = sorted(set(value) - fields) if isinstance(value, dict) else []
        raise ProjectLedgerError(f"{label}字段集合不闭合：缺 {missing}，多 {extra}")
    return value


def _private_dir(path: Path, label: str, *, create: bool = False) -> Path:
    if path.is_symlink():
        raise ProjectLedgerError(f"{label}不得是符号链接")
    if not path.exists():
        if not create:
            raise ProjectLedgerError(f"{label}不存在：{path}")
        path.mkdir(mode=0o700)
    if not path.is_dir():
        raise ProjectLedgerError(f"{label}不是目录")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProjectLedgerError(f"{label}权限不得允许 group/other 访问")
    return path


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file():
        raise ProjectLedgerError(f"{label}不是可信普通文件：{path}")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ProjectLedgerError(f"{label}权限或属主非法：{path}")
    if not 0 < metadata.st_size <= MAX_JSON_BYTES:
        raise ProjectLedgerError(f"{label}大小非法：{path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProjectLedgerError(f"{label}不是合法 JSON：{path}") from error
    if not isinstance(payload, dict):
        raise ProjectLedgerError(f"{label}顶层必须是对象：{path}")
    return payload, raw


def _write_once(path: Path, payload: Mapping[str, Any]) -> bytes:
    if path.exists() or path.is_symlink():
        raise ProjectLedgerError(f"输出已存在，禁止覆盖：{path}")
    _private_dir(path.parent, "输出父目录")
    raw = _canonical(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ProjectLedgerError(f"输出已存在，禁止覆盖：{path}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return raw


def _replace_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """head 缓存允许覆盖，但必须原子替换。"""

    _private_dir(path.parent, "缓存父目录")
    raw = _canonical(payload) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _flock(path: Path, label: str) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ProjectLedgerError(f"{label}锁文件无法打开") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ProjectLedgerError(f"{label}锁文件身份不可信")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


_PROJECT_LOCK_DEPTH = 0
_CAMPAIGN_LOCK_HELD = False


@contextlib.contextmanager
def project_lock(root: Path) -> Iterator[None]:
    """项目锁；持有 Campaign 锁时禁止再取项目锁（锁顺序固定先项目后 Campaign）。"""

    global _PROJECT_LOCK_DEPTH
    if _CAMPAIGN_LOCK_HELD:
        raise ProjectLedgerError("持有 Campaign 锁时不得再取项目锁")
    if _PROJECT_LOCK_DEPTH > 0:
        _PROJECT_LOCK_DEPTH += 1
        try:
            yield
        finally:
            _PROJECT_LOCK_DEPTH -= 1
        return
    with _flock(root / LOCK_NAME, "项目总账"):
        _PROJECT_LOCK_DEPTH = 1
        try:
            yield
        finally:
            _PROJECT_LOCK_DEPTH = 0


@contextlib.contextmanager
def campaign_ledger_lock(campaign_dir: Path) -> Iterator[None]:
    global _CAMPAIGN_LOCK_HELD
    ledger_dir = _private_dir(campaign_dir / CAMPAIGN_LEDGER_DIR_NAME, "Campaign 账本目录", create=True)
    with _flock(ledger_dir / ".ledger.lock", "Campaign 账本"):
        previous = _CAMPAIGN_LOCK_HELD
        _CAMPAIGN_LOCK_HELD = True
        try:
            yield ledger_dir
        finally:
            _CAMPAIGN_LOCK_HELD = previous


# ---------------------------------------------------------------------------
# 总账 plan
# ---------------------------------------------------------------------------

PLAN_FIELDS = {
    "schema_version",
    "project_id",
    "started_at_utc",
    "absolute_deadline_utc",
    "deadline_approved_by",
    "deadline_approved_at_utc",
    "live_request_budget",
    "same_root_cause_retry_limit",
    "formal_open_limit",
    "root_cause_codes_sha256",
    "root_cause_algorithm_version",
    "estimation_policy",
    "estimation_policy_approved_by",
    "estimation_policy_approved_at_utc",
    "fixture_only",
    "initial_precise_count",
    "initial_estimated_count",
    "initial_identity_keys_sha256",
    "initial_identity_key_count",
    "initial_root_cause_counts",
    "bootstrap_cutover",
    "closed_ledger_heads",
    "bound_receipts",
    "plan_sha256",
}


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    _expect(plan, PLAN_FIELDS, "总账 plan")
    if plan["schema_version"] != PLAN_SCHEMA:
        raise ProjectLedgerError("总账 plan schema 非法")
    _safe_id(plan["project_id"], "project_id")
    started = _timestamp(plan["started_at_utc"], "started_at_utc")
    deadline = _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc")
    if deadline <= started:
        raise ProjectLedgerError("absolute_deadline_utc 必须晚于 started_at_utc")
    if not isinstance(plan["deadline_approved_by"], str) or not plan["deadline_approved_by"].strip():
        raise ProjectLedgerError("绝对截止时间必须记录批准人")
    _timestamp(plan["deadline_approved_at_utc"], "deadline_approved_at_utc")
    if plan["live_request_budget"] is not None:
        if _count(plan["live_request_budget"], "live_request_budget") == 0:
            raise ProjectLedgerError("live_request_budget 不得为 0")
    if plan["same_root_cause_retry_limit"] != DEFAULT_RETRY_LIMIT:
        raise ProjectLedgerError("同根因重试上限必须固定为 2")
    if _count(plan["formal_open_limit"], "formal_open_limit") == 0:
        raise ProjectLedgerError("formal_open_limit 不得为 0")
    _sha_field(plan["root_cause_codes_sha256"], "root_cause_codes_sha256")
    if not isinstance(plan["root_cause_algorithm_version"], str) or not plan["root_cause_algorithm_version"]:
        raise ProjectLedgerError("root_cause_algorithm_version 非法")
    if plan["estimation_policy"] not in ESTIMATION_POLICIES:
        raise ProjectLedgerError("estimation_policy 非法")
    if not isinstance(plan["estimation_policy_approved_by"], str) or not plan["estimation_policy_approved_by"].strip():
        raise ProjectLedgerError("估计政策必须记录批准人")
    _timestamp(plan["estimation_policy_approved_at_utc"], "estimation_policy_approved_at_utc")
    if not isinstance(plan["fixture_only"], bool):
        raise ProjectLedgerError("fixture_only 必须是布尔")
    _count(plan["initial_precise_count"], "initial_precise_count")
    _count(plan["initial_estimated_count"], "initial_estimated_count")
    _sha_field(plan["initial_identity_keys_sha256"], "initial_identity_keys_sha256")
    _count(plan["initial_identity_key_count"], "initial_identity_key_count")
    counts = plan["initial_root_cause_counts"]
    if not isinstance(counts, dict) or any(
        not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in counts.items()
    ):
        raise ProjectLedgerError("initial_root_cause_counts 非法")
    cutover = _expect(
        plan["bootstrap_cutover"],
        {"campaign_ledgers", "batch_sha256s", "operation_ids", "identity_keys_sha256"},
        "bootstrap_cutover",
    )
    for item in cutover["campaign_ledgers"]:
        _expect(item, {"ledger_id", "head_sha256", "head_sequence"}, "bootstrap_cutover.campaign_ledgers 项")
        _sha_field(item["head_sha256"], "bootstrap_cutover head_sha256")
        _count(item["head_sequence"], "bootstrap_cutover head_sequence")
    for value in cutover["batch_sha256s"]:
        _sha_field(value, "bootstrap_cutover batch_sha256")
    for value in cutover["operation_ids"]:
        _safe_id(value, "bootstrap_cutover operation_id")
    _sha_field(cutover["identity_keys_sha256"], "bootstrap_cutover identity_keys_sha256")
    for item in plan["closed_ledger_heads"]:
        _expect(item, {"ledger_id", "head_sha256", "head_sequence", "status"}, "closed_ledger_heads 项")
        if item["status"] != "stopped":
            raise ProjectLedgerError("closed_ledger_heads 只接受 stopped 账本")
    receipts = plan["bound_receipts"]
    if not isinstance(receipts, dict):
        raise ProjectLedgerError("bound_receipts 必须是对象")
    for name, binding in receipts.items():
        _expect(binding, {"path", "sha256"}, f"bound_receipts.{name}")
        _sha_field(binding["sha256"], f"bound_receipts.{name}.sha256")
    unsigned = {k: v for k, v in plan.items() if k != "plan_sha256"}
    if plan["plan_sha256"] != _digest(unsigned):
        raise ProjectLedgerError("总账 plan 自摘要不一致")
    return plan


MIGRATION_SCHEMA = "root-cause-code-migration/v1"


def _code_migrations(root: Path) -> list[dict[str, Any]]:
    """读取 migrations/NNNNNN.json 映射收据链：from_sha256 → to_sha256 与旧新 ID 映射。"""

    migrations_root = root / "migrations"
    if not migrations_root.exists():
        return []
    _private_dir(migrations_root, "映射收据目录")
    paths = sorted(migrations_root.iterdir())
    expected = [f"{index:06d}.json" for index in range(1, len(paths) + 1)]
    if [path.name for path in paths] != expected:
        raise ProjectLedgerError("映射收据序号不连续")
    migrations: list[dict[str, Any]] = []
    for index, path in enumerate(paths, start=1):
        payload, _raw = _read_json(path, f"映射收据 {path.name}")
        _expect(
            payload,
            {"schema_version", "sequence", "from_codes_sha256", "to_codes_sha256", "from_algorithm_version", "to_algorithm_version", "id_mapping", "approved_by", "approved_at_utc"},
            f"映射收据 {path.name}",
        )
        if payload["schema_version"] != MIGRATION_SCHEMA or payload["sequence"] != index:
            raise ProjectLedgerError(f"映射收据 {path.name} schema 或序号非法")
        _sha_field(payload["from_codes_sha256"], "from_codes_sha256")
        _sha_field(payload["to_codes_sha256"], "to_codes_sha256")
        mapping = payload["id_mapping"]
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise ProjectLedgerError(f"映射收据 {path.name} id_mapping 非法")
        migrations.append(payload)
    return migrations


def _effective_codes_identity(plan: Mapping[str, Any], migrations: list[dict[str, Any]]) -> tuple[str, str, dict[str, str]]:
    """沿映射链从 plan 冻结的枚举表身份走到当前身份，返回合成的旧→新 ID 映射。"""

    codes_sha256 = str(plan["root_cause_codes_sha256"])
    algorithm = str(plan["root_cause_algorithm_version"])
    combined: dict[str, str] = {}
    for migration in migrations:
        if migration["from_codes_sha256"] != codes_sha256 or migration["from_algorithm_version"] != algorithm:
            raise ProjectLedgerError("映射收据链与总账冻结的枚举表身份不衔接")
        mapping = dict(migration["id_mapping"])
        combined = {old: mapping.get(new, new) for old, new in combined.items()}
        for old, new in mapping.items():
            combined.setdefault(old, new)
        codes_sha256 = str(migration["to_codes_sha256"])
        algorithm = str(migration["to_algorithm_version"])
    return codes_sha256, algorithm, combined


def _load_plan(root: Path) -> tuple[dict[str, Any], bytes]:
    plan, raw = _read_json(root / "plan.json", "总账 plan")
    _validate_plan(plan)
    migrations = _code_migrations(root)
    codes_sha256, algorithm, _mapping = _effective_codes_identity(plan, migrations)
    try:
        root_cause.assert_codes_identity(expected_codes_sha256=codes_sha256, expected_algorithm_version=algorithm)
    except root_cause.RootCauseError as error:
        raise ProjectLedgerError(
            "根因枚举表或算法已变更且没有衔接的旧新 ID 映射收据，总账拒绝服务：" + str(error)
        ) from error
    return plan, raw


def create_project_ledger(
    root: Path,
    *,
    project_id: str,
    absolute_deadline_utc: str,
    deadline_approved_by: str,
    estimation_policy: str,
    estimation_policy_approved_by: str,
    fixture_only: bool,
    live_request_budget: int | None = None,
    formal_open_limit: int = DEFAULT_FORMAL_OPEN_LIMIT,
    initial_identity_keys: list[str] | None = None,
    initial_precise_count: int | None = None,
    initial_estimated_count: int = 0,
    initial_root_cause_counts: Mapping[str, int] | None = None,
    bootstrap_cutover: Mapping[str, Any] | None = None,
    closed_ledger_heads: list[Mapping[str, Any]] | None = None,
    bound_receipts: Mapping[str, Mapping[str, str]] | None = None,
    started_at_utc: str | None = None,
    approved_at_utc: str | None = None,
) -> dict[str, Any]:
    """创建只写一次的项目总账；绝对截止时间与估计政策必须显式给出批准人。"""

    if root.exists() or root.is_symlink():
        raise ProjectLedgerError(f"项目总账已存在：{root}")
    _private_dir(root.parent, "项目总账父目录")
    if fixture_only and STAGING_DIR_NAME not in root.parent.resolve(strict=False).parts:
        raise ProjectLedgerError("fixture_only 总账必须位于 staging 目录树内")
    codes = root_cause.load_codes()
    keys = sorted(set(initial_identity_keys or []))
    now = started_at_utc or _utc_now()
    approved = approved_at_utc or now
    cutover = dict(
        bootstrap_cutover
        or {"campaign_ledgers": [], "batch_sha256s": [], "operation_ids": [], "identity_keys_sha256": _digest(keys)}
    )
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "project_id": project_id,
        "started_at_utc": now,
        "absolute_deadline_utc": absolute_deadline_utc,
        "deadline_approved_by": deadline_approved_by,
        "deadline_approved_at_utc": approved,
        "live_request_budget": live_request_budget,
        "same_root_cause_retry_limit": DEFAULT_RETRY_LIMIT,
        "formal_open_limit": formal_open_limit,
        "root_cause_codes_sha256": codes["codes_sha256"],
        "root_cause_algorithm_version": codes["algorithm_version"],
        "estimation_policy": estimation_policy,
        "estimation_policy_approved_by": estimation_policy_approved_by,
        "estimation_policy_approved_at_utc": approved,
        "fixture_only": fixture_only,
        "initial_precise_count": len(keys) if initial_precise_count is None else initial_precise_count,
        "initial_estimated_count": initial_estimated_count,
        "initial_identity_keys_sha256": _digest(keys),
        "initial_identity_key_count": len(keys),
        "initial_root_cause_counts": dict(sorted((initial_root_cause_counts or {}).items())),
        "bootstrap_cutover": cutover,
        "closed_ledger_heads": [dict(item) for item in (closed_ledger_heads or [])],
        "bound_receipts": {name: dict(binding) for name, binding in (bound_receipts or {}).items()},
    }
    plan["plan_sha256"] = _digest(plan)
    _validate_plan(plan)
    root.mkdir(mode=0o700)
    (root / "events").mkdir(mode=0o700)
    (root / "repairs").mkdir(mode=0o700)
    (root / "repairs" / "outbox").mkdir(mode=0o700)
    (root / "repairs" / "receipts").mkdir(mode=0o700)
    _write_once(root / "initial-identity-keys.json", {"schema_version": "upgrade-project-ledger-identity-keys/v1", "identity_keys": keys})
    _write_once(root / "plan.json", plan)
    with project_lock(root):
        head = _replay(root, plan, _load_events(root), rebuild_cache=True)
    return head


# ---------------------------------------------------------------------------
# 事件与 head 重放
# ---------------------------------------------------------------------------

EVENT_FIELDS = {
    "schema_version",
    "sequence",
    "operation_id",
    "recorded_at_utc",
    "event_type",
    "payload",
    "payload_sha256",
    "source_batch_sha256",
    "previous_event_sha256",
    "event_sha256",
}


def _load_events(root: Path) -> list[dict[str, Any]]:
    events_root = root / "events"
    if events_root.is_symlink() or not events_root.is_dir():
        raise ProjectLedgerError("events 目录缺失或不可信")
    paths = sorted(events_root.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ProjectLedgerError("events 目录只能包含普通文件")
    expected = [f"{index:06d}.json" for index in range(1, len(paths) + 1)]
    if [path.name for path in paths] != expected:
        raise ProjectLedgerError("event 序号不连续或存在额外文件")
    events: list[dict[str, Any]] = []
    previous: str | None = None
    for index, path in enumerate(paths, start=1):
        event, _raw = _read_json(path, f"event {path.name}")
        _expect(event, EVENT_FIELDS, f"event {index}")
        if event["schema_version"] != EVENT_SCHEMA or event["sequence"] != index:
            raise ProjectLedgerError(f"event {index} schema 或序号不一致")
        if event["event_type"] not in EVENT_TYPES:
            raise ProjectLedgerError(f"event {index} 事件类型非法")
        _safe_id(event["operation_id"], f"event {index}.operation_id")
        _timestamp(event["recorded_at_utc"], f"event {index}.recorded_at_utc")
        if event["payload_sha256"] != _digest(event["payload"]):
            raise ProjectLedgerError(f"event {index} payload 摘要不一致")
        if event["source_batch_sha256"] is not None:
            _sha_field(event["source_batch_sha256"], f"event {index}.source_batch_sha256")
        if event["previous_event_sha256"] != previous:
            raise ProjectLedgerError(f"event {index} 摘要链断裂")
        unsigned = {k: v for k, v in event.items() if k != "event_sha256"}
        if event["event_sha256"] != _digest(unsigned):
            raise ProjectLedgerError(f"event {index} 自摘要不一致")
        previous = event["event_sha256"]
        events.append(event)
    return events


def _initial_keys(root: Path, plan: Mapping[str, Any]) -> set[str]:
    payload, _raw = _read_json(root / "initial-identity-keys.json", "初始身份键清单")
    keys = payload.get("identity_keys")
    if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ProjectLedgerError("初始身份键清单形态非法")
    if _digest(sorted(set(keys))) != plan["initial_identity_keys_sha256"]:
        raise ProjectLedgerError("初始身份键清单与 plan 摘要不一致")
    return set(keys)


def _apply_request_part(state: dict[str, Any], part: Mapping[str, Any], operation_id: str, label: str) -> None:
    status = part.get("status")
    keys = part.get("identity_keys")
    if status not in REQUEST_STATUSES or not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
        raise ProjectLedgerError(f"{label}请求部分形态非法")
    estimated = _count(part.get("estimated_delta", 0), f"{label}.estimated_delta")
    accounted: set[str] = state["accounted_identity_index"]
    initial: set[str] = state["initial_identity_keys"]
    new_keys = 0
    for key in keys:
        if key in accounted or key in initial:
            state["duplicate_identity_keys"].append({"identity_key": key, "operation_id": operation_id})
            continue
        accounted.add(key)
        new_keys += 1
    state["precise_total"] += new_keys
    state["estimated_total"] += estimated
    if status == "unresolved":
        if operation_id not in state["unresolved_operation_ids"]:
            state["unresolved_operation_ids"].append(operation_id)


def _replay(root: Path, plan: Mapping[str, Any], events: list[dict[str, Any]], *, rebuild_cache: bool) -> dict[str, Any]:
    """从 plan 加 events 重放权威 head；缓存只在锁内由本函数刷新。"""

    state: dict[str, Any] = {
        "precise_total": int(plan["initial_precise_count"]),
        "estimated_total": int(plan["initial_estimated_count"]),
        "accounted_identity_index": set(),
        "initial_identity_keys": _initial_keys(root, plan),
        "duplicate_identity_keys": [],
        "root_cause_counts": dict(plan["initial_root_cause_counts"]),
        "registered_campaigns": {},
        "rejected_campaigns": {},
        "terminal_campaigns": {},
        "unresolved_operation_ids": [],
        "operations": {},
        "repaired_root_causes": [],
    }
    for event in events:
        event_type = event["event_type"]
        payload = event["payload"]
        operation_id = event["operation_id"]
        if operation_id in state["operations"]:
            raise ProjectLedgerError(f"operation_id 重复出现在事件链中：{operation_id}")
        state["operations"][operation_id] = {"event_type": event_type, "payload_sha256": event["payload_sha256"], "sequence": event["sequence"]}
        blocked = bool(state["unresolved_operation_ids"])
        if blocked and event_type not in BLOCKED_ALLOWED_EVENTS:
            raise ProjectLedgerError(f"总账 blocked 期间出现禁止事件：{event_type}")
        if event_type == "campaign_registered":
            campaign_id = _safe_id(payload.get("campaign_id"), "campaign_registered.campaign_id")
            if campaign_id in state["registered_campaigns"]:
                raise ProjectLedgerError(f"Campaign 重复注册：{campaign_id}")
            state["registered_campaigns"][campaign_id] = {
                "operation_id": operation_id,
                "campaign_mode": payload.get("campaign_mode"),
                "target_version": payload.get("target_version"),
                "deadline_at_utc": payload.get("deadline_at_utc"),
                "registration_batch_sha256": payload.get("registration_batch_sha256"),
                "sequence": event["sequence"],
            }
        elif event_type == "campaign_registration_rejected":
            campaign_id = _safe_id(payload.get("campaign_id"), "campaign_registration_rejected.campaign_id")
            state["rejected_campaigns"][campaign_id] = {
                "operation_id": operation_id,
                "reason": payload.get("reason"),
                "registration_batch_sha256": payload.get("registration_batch_sha256"),
            }
        elif event_type == "reconciliation_committed":
            request = payload.get("request")
            cause = payload.get("root_cause")
            if not isinstance(request, dict):
                raise ProjectLedgerError("reconciliation_committed 缺少请求部分")
            _apply_request_part(state, request, operation_id, "reconciliation_committed")
            if cause is not None:
                if not isinstance(cause, dict) or not isinstance(cause.get("root_cause_id"), str):
                    raise ProjectLedgerError("reconciliation_committed 根因部分形态非法")
                rc = cause["root_cause_id"]
                state["root_cause_counts"][rc] = state["root_cause_counts"].get(rc, 0) + 1
        elif event_type == "accounting_resolved":
            resolved = payload.get("resolved_operation_id")
            if resolved not in state["unresolved_operation_ids"]:
                raise ProjectLedgerError(f"accounting_resolved 指向的 operation 不在未决集合：{resolved}")
            request = payload.get("request")
            if not isinstance(request, dict) or request.get("status") == "unresolved":
                raise ProjectLedgerError("accounting_resolved 必须携带已解析的请求部分")
            _apply_request_part(state, request, operation_id, "accounting_resolved")
            state["unresolved_operation_ids"].remove(resolved)
        elif event_type == "root_cause_repaired":
            rc = payload.get("root_cause_id")
            if not isinstance(rc, str):
                raise ProjectLedgerError("root_cause_repaired 缺少 root_cause_id")
            state["root_cause_counts"][rc] = 0
            state["repaired_root_causes"].append({"root_cause_id": rc, "operation_id": operation_id})
        elif event_type == "campaign_terminal":
            campaign_id = _safe_id(payload.get("campaign_id"), "campaign_terminal.campaign_id")
            if payload.get("terminal_reason") not in TERMINAL_REASONS:
                raise ProjectLedgerError("campaign_terminal 的 terminal_reason 非法")
            state["terminal_campaigns"][campaign_id] = {"operation_id": operation_id, "terminal_reason": payload["terminal_reason"]}
    _codes_sha256, _algorithm, mapping = _effective_codes_identity(plan, _code_migrations(root))
    if mapping:
        migrated: dict[str, int] = {}
        for rc, count in state["root_cause_counts"].items():
            target = mapping.get(rc, rc)
            migrated[target] = migrated.get(target, 0) + count
        state["root_cause_counts"] = migrated
    head_sha256 = events[-1]["event_sha256"] if events else plan["plan_sha256"]
    limit = int(plan["same_root_cause_retry_limit"])
    budget = plan["live_request_budget"]
    consumed = state["precise_total"] + state["estimated_total"]
    head = {
        "schema_version": HEAD_SCHEMA,
        "sequence": len(events),
        "head_sha256": head_sha256,
        "plan_sha256": plan["plan_sha256"],
        "precise_total": state["precise_total"],
        "estimated_total": state["estimated_total"],
        "accounted_identity_index": sorted(state["accounted_identity_index"]),
        "accounted_identity_index_sha256": _digest(sorted(state["accounted_identity_index"])),
        "duplicate_identity_keys": state["duplicate_identity_keys"],
        "root_cause_counts": dict(sorted(state["root_cause_counts"].items())),
        "root_causes_at_limit": sorted(rc for rc, n in state["root_cause_counts"].items() if n >= limit),
        "registered_campaigns": state["registered_campaigns"],
        "rejected_campaigns": state["rejected_campaigns"],
        "terminal_campaigns": state["terminal_campaigns"],
        "unresolved_operation_ids": list(state["unresolved_operation_ids"]),
        "blocked": bool(state["unresolved_operation_ids"]),
        "operations": state["operations"],
        "live_request_budget": budget,
        "remaining_live_requests": (None if budget is None else max(int(budget) - consumed, 0)),
        "repaired_root_causes": state["repaired_root_causes"],
    }
    cache_path = root / "head.json"
    if cache_path.exists() or cache_path.is_symlink():
        cached, _raw = _read_json(cache_path, "head 缓存")
        cached_sequence = cached.get("sequence")
        if not isinstance(cached_sequence, int) or cached_sequence > head["sequence"]:
            raise ProjectLedgerError("head 缓存超前于事件链，失败关闭")
        if cached_sequence == head["sequence"] and cached.get("head_sha256") != head["head_sha256"]:
            raise ProjectLedgerError("head 缓存与事件链同序号但摘要不符，失败关闭")
    if rebuild_cache:
        # 缓存内容未变时不重写：只读门禁不应改变任何文件的字节或元数据，
        # 否则演练 inventory 会把一次纯校验误判为漂移。
        expected_raw = _canonical(head) + b"\n"
        if not cache_path.exists() or cache_path.read_bytes() != expected_raw:
            _replace_atomic(cache_path, head)
    return head


def replay_head(root: Path) -> dict[str, Any]:
    """锁内重放权威 head 并刷新缓存。"""

    with project_lock(root):
        plan, _raw = _load_plan(root)
        return _replay(root, plan, _load_events(root), rebuild_cache=True)


def append_project_event(
    root: Path,
    *,
    operation_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    source_batch_sha256: str | None,
    expected_head_sha256: str | None = None,
    recorded_at_utc: str | None = None,
) -> tuple[dict[str, Any], str]:
    """锁内重放后追加一个事件；重复 operation 核对类型与 payload 摘要后幂等返回。"""

    _safe_id(operation_id, "operation_id")
    if event_type not in EVENT_TYPES:
        raise ProjectLedgerError(f"事件类型非法：{event_type}")
    payload_dict = dict(payload)
    payload_sha256 = _digest(payload_dict)
    with project_lock(root):
        plan, _raw = _load_plan(root)
        events = _load_events(root)
        head = _replay(root, plan, events, rebuild_cache=False)
        existing = head["operations"].get(operation_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload_sha256"] != payload_sha256:
                raise ProjectLedgerError(f"operation_id 重复且类型或 payload 不同：{operation_id}")
            return head, "duplicate"
        if expected_head_sha256 is not None and head["head_sha256"] != expected_head_sha256:
            raise ProjectLedgerError("总账 head 已变化，CAS 失败")
        if head["blocked"] and event_type not in BLOCKED_ALLOWED_EVENTS:
            raise ProjectLedgerError(f"总账 blocked，禁止事件：{event_type}")
        sequence = len(events) + 1
        event = {
            "schema_version": EVENT_SCHEMA,
            "sequence": sequence,
            "operation_id": operation_id,
            "recorded_at_utc": recorded_at_utc or _utc_now(),
            "event_type": event_type,
            "payload": payload_dict,
            "payload_sha256": payload_sha256,
            "source_batch_sha256": source_batch_sha256,
            "previous_event_sha256": events[-1]["event_sha256"] if events else None,
        }
        event["event_sha256"] = _digest(event)
        # 先在内存里重放一遍，非法事件不会落盘。
        new_head = _replay(root, plan, [*events, event], rebuild_cache=False)
        _write_once(root / "events" / f"{sequence:06d}.json", event)
        _replace_atomic(root / "head.json", new_head)
        return new_head, "appended"


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


def admission_problems(
    plan: Mapping[str, Any],
    head: Mapping[str, Any],
    *,
    campaign_id: str,
    campaign_mode: str,
    target_version: str,
    now: datetime | None = None,
    allow_registered: bool = False,
) -> list[str]:
    """返回不满足 admission 的原因；空列表即准入。``allow_registered`` 供幂等重注册使用。"""

    problems: list[str] = []
    current = now or datetime.now(timezone.utc)
    if head["blocked"]:
        problems.append(f"总账 blocked：未决账务 {head['unresolved_operation_ids']}")
    if current >= _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
        problems.append("项目绝对截止时间已到")
    if head["remaining_live_requests"] is not None and head["remaining_live_requests"] <= 0:
        problems.append("项目请求预算已耗尽")
    if head["root_causes_at_limit"]:
        problems.append(f"根因已达同根因重试上限：{head['root_causes_at_limit']}")
    if campaign_id in head["registered_campaigns"] and not allow_registered:
        problems.append(f"Campaign 已注册：{campaign_id}")
    if campaign_id in head["rejected_campaigns"]:
        problems.append(f"Campaign 已被拒绝注册：{campaign_id}")
    if campaign_mode == "formal":
        open_formal = [
            cid
            for cid, item in head["registered_campaigns"].items()
            if item.get("campaign_mode") == "formal"
            and item.get("target_version") == target_version
            and cid not in head["terminal_campaigns"]
        ]
        if len(open_formal) >= int(plan["formal_open_limit"]):
            problems.append(f"同版本无终态 formal Campaign 已达上限 {plan['formal_open_limit']}：{open_formal}")
    return problems


# ---------------------------------------------------------------------------
# Campaign 侧 outbox batch v2
# ---------------------------------------------------------------------------


def _batch_dirs(outbox: Path) -> list[tuple[int, Path]]:
    if not outbox.exists():
        return []
    _private_dir(outbox, "outbox 目录")
    found: list[tuple[int, Path]] = []
    for child in sorted(outbox.iterdir()):
        match = BATCH_DIR_RE.fullmatch(child.name)
        if not match or child.is_symlink() or not child.is_dir():
            raise ProjectLedgerError(f"outbox 含非法条目：{child.name}")
        found.append((int(match.group(1)), child))
    expected = list(range(1, len(found) + 1))
    if [n for n, _ in found] != expected:
        raise ProjectLedgerError("outbox batch 序号不连续")
    return found


def _read_batch(batch_dir: Path) -> dict[str, Any]:
    """读取一个 batch：entry 链、COMMIT 绑定；任何缺项、断链、篡改即失败关闭。"""

    entries: list[dict[str, Any]] = []
    entry_paths: list[Path] = []
    for child in sorted(batch_dir.iterdir()):
        if child.name == "COMMIT":
            continue
        match = ENTRY_FILE_RE.fullmatch(child.name)
        if not match or child.is_symlink() or not child.is_file():
            raise ProjectLedgerError(f"batch {batch_dir.name} 含非法文件：{child.name}")
        entry_paths.append(child)
    previous: str | None = None
    for index, path in enumerate(entry_paths, start=1):
        entry, _raw = _read_json(path, f"batch {batch_dir.name} {path.name}")
        _expect(
            entry,
            {"schema_version", "batch_sequence", "entry_sequence", "operation_id", "event_type", "payload_fragment", "source", "receipt_bindings", "previous_entry_sha256", "entry_sha256"},
            f"batch {batch_dir.name} {path.name}",
        )
        if entry["schema_version"] != ENTRY_SCHEMA or entry["entry_sequence"] != index or path.name != f"entry-{index:02d}.json":
            raise ProjectLedgerError(f"batch {batch_dir.name} entry 序号或 schema 非法")
        if entry["previous_entry_sha256"] != previous:
            raise ProjectLedgerError(f"batch {batch_dir.name} entry 链断裂")
        unsigned = {k: v for k, v in entry.items() if k != "entry_sha256"}
        if entry["entry_sha256"] != _digest(unsigned):
            raise ProjectLedgerError(f"batch {batch_dir.name} entry 摘要不一致")
        previous = entry["entry_sha256"]
        entries.append(entry)
    commit_path = batch_dir / "COMMIT"
    if not commit_path.exists():
        return {"batch_dir": batch_dir, "committed": False, "entries": entries}
    commit, _raw = _read_json(commit_path, f"batch {batch_dir.name} COMMIT")
    _expect(
        commit,
        {"schema_version", "batch_sequence", "operation_id", "event_type", "entry_count", "last_entry_sha256", "batch_sha256", "previous_batch_sha256", "committed_at_utc"},
        f"batch {batch_dir.name} COMMIT",
    )
    if commit["schema_version"] != COMMIT_SCHEMA:
        raise ProjectLedgerError(f"batch {batch_dir.name} COMMIT schema 非法")
    if not entries or commit["entry_count"] != len(entries):
        raise ProjectLedgerError(f"batch {batch_dir.name} COMMIT 后 entry 数量与提交不一致（缺项或追加）")
    if commit["last_entry_sha256"] != entries[-1]["entry_sha256"]:
        raise ProjectLedgerError(f"batch {batch_dir.name} COMMIT 末项摘要不一致")
    if any(e["operation_id"] != commit["operation_id"] or e["event_type"] != commit["event_type"] for e in entries):
        raise ProjectLedgerError(f"batch {batch_dir.name} entry 与 COMMIT 的 operation 或事件类型不一致")
    expected_batch_sha256 = _digest([e["entry_sha256"] for e in entries] + [commit["previous_batch_sha256"]])
    if commit["batch_sha256"] != expected_batch_sha256:
        raise ProjectLedgerError(f"batch {batch_dir.name} batch_sha256 不一致")
    return {"batch_dir": batch_dir, "committed": True, "entries": entries, "commit": commit}


def write_batch(
    ledger_dir: Path,
    *,
    operation_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
    receipt_bindings: list[Mapping[str, Any]] | None = None,
    fragment_size: int = 500,
    committed_at_utc: str | None = None,
) -> dict[str, Any]:
    """写一个带 COMMIT 的 outbox batch；调用方须已持有对应目录锁。

    payload 的 ``request.identity_keys`` 或顶层 ``identity_keys`` 超过 ``fragment_size``
    时按片写多个 entry，其余字段写在首个 entry；合并规则见 ``_merge_fragments``。
    """

    _safe_id(operation_id, "operation_id")
    if event_type not in EVENT_TYPES:
        raise ProjectLedgerError(f"事件类型非法：{event_type}")
    outbox = _private_dir(ledger_dir / "outbox", "outbox 目录", create=True)
    existing = _batch_dirs(outbox)
    for _n, batch_dir in existing:
        batch = _read_batch(batch_dir)
        if not batch["committed"]:
            raise ProjectLedgerError(f"存在未 COMMIT 的 batch：{batch_dir.name}，禁止再写新 batch")
        if batch["commit"]["operation_id"] == operation_id:
            return {"batch_dir": str(batch_dir), "batch_sha256": batch["commit"]["batch_sha256"], "reused": True}
    previous_batch_sha256 = _read_batch(existing[-1][1])["commit"]["batch_sha256"] if existing else None
    sequence = len(existing) + 1
    batch_dir = outbox / f"batch-{sequence:06d}"
    batch_dir.mkdir(mode=0o700)
    payload_dict = json.loads(_canonical(payload))
    keys: list[str] = []
    key_owner: str | None = None
    if isinstance(payload_dict.get("identity_keys"), list):
        keys = list(payload_dict.pop("identity_keys"))
        key_owner = "identity_keys"
    elif isinstance(payload_dict.get("request"), dict) and isinstance(payload_dict["request"].get("identity_keys"), list):
        keys = list(payload_dict["request"].pop("identity_keys"))
        key_owner = "request.identity_keys"
    fragments: list[dict[str, Any]] = []
    first = dict(payload_dict)
    if key_owner == "identity_keys":
        first["identity_keys"] = keys[:fragment_size]
    elif key_owner == "request.identity_keys":
        first["request"] = {**first["request"], "identity_keys": keys[:fragment_size]}
    fragments.append(first)
    for start in range(fragment_size, len(keys), fragment_size):
        piece = keys[start : start + fragment_size]
        if key_owner == "identity_keys":
            fragments.append({"identity_keys": piece})
        else:
            fragments.append({"request": {"identity_keys": piece}})
    previous: str | None = None
    entries: list[dict[str, Any]] = []
    for index, fragment in enumerate(fragments, start=1):
        entry = {
            "schema_version": ENTRY_SCHEMA,
            "batch_sequence": sequence,
            "entry_sequence": index,
            "operation_id": operation_id,
            "event_type": event_type,
            "payload_fragment": fragment,
            "source": dict(source),
            "receipt_bindings": [dict(item) for item in (receipt_bindings or [])],
            "previous_entry_sha256": previous,
        }
        entry["entry_sha256"] = _digest(entry)
        _write_once(batch_dir / f"entry-{index:02d}.json", entry)
        previous = entry["entry_sha256"]
        entries.append(entry)
    commit = {
        "schema_version": COMMIT_SCHEMA,
        "batch_sequence": sequence,
        "operation_id": operation_id,
        "event_type": event_type,
        "entry_count": len(entries),
        "last_entry_sha256": entries[-1]["entry_sha256"],
        "previous_batch_sha256": previous_batch_sha256,
        "committed_at_utc": committed_at_utc or _utc_now(),
    }
    commit["batch_sha256"] = _digest([e["entry_sha256"] for e in entries] + [previous_batch_sha256])
    _write_once(batch_dir / "COMMIT", commit)
    return {"batch_dir": str(batch_dir), "batch_sha256": commit["batch_sha256"], "reused": False}


def _merge_batch_payload(batch: Mapping[str, Any]) -> dict[str, Any]:
    entries = batch["entries"]
    merged: dict[str, Any] = {}
    request_keys: list[str] = []
    top_keys: list[str] = []
    for entry in entries:
        fragment = entry["payload_fragment"]
        if not isinstance(fragment, dict):
            raise ProjectLedgerError("payload_fragment 必须是对象")
        for key, value in fragment.items():
            if key == "identity_keys":
                if not isinstance(value, list):
                    raise ProjectLedgerError("identity_keys 分片必须是列表")
                top_keys.extend(value)
            elif key == "request" and isinstance(value, dict):
                part = dict(value)
                piece = part.pop("identity_keys", None)
                if piece is not None:
                    if not isinstance(piece, list):
                        raise ProjectLedgerError("request.identity_keys 分片必须是列表")
                    request_keys.extend(piece)
                if "request" in merged:
                    for k, v in part.items():
                        if merged["request"].get(k, v) != v:
                            raise ProjectLedgerError(f"分片字段 request.{k} 冲突")
                    merged["request"].update(part)
                else:
                    merged["request"] = part
            elif key in merged:
                if merged[key] != value:
                    raise ProjectLedgerError(f"分片标量字段 {key} 冲突")
            else:
                merged[key] = value
    if "request" in merged:
        merged["request"]["identity_keys"] = request_keys
    if top_keys or any("identity_keys" in e["payload_fragment"] for e in entries):
        merged["identity_keys"] = top_keys
    return merged


# ---------------------------------------------------------------------------
# 总账定位、Campaign 注册、补齐器
# ---------------------------------------------------------------------------


def requires_project_ledger(campaign_mode: Any, target_version: Any) -> bool:
    """0.154 起的 formal Campaign 必须在项目总账内创建与执行。"""

    if campaign_mode != "formal" or not isinstance(target_version, str) or not VERSION_RE.fullmatch(target_version):
        return False
    return tuple(int(part) for part in target_version.split(".")) >= PROJECT_LEDGER_MIN_VERSION


def find_project_ledger(start: Path) -> Path | None:
    """从 Campaign 目录向上最多六层查找 ``upgrade-project-ledger/plan.json``。"""

    current = start.resolve(strict=False)
    for _ in range(MAX_ANCESTOR_DEPTH + 1):
        candidate = current / LEDGER_DIR_NAME
        if candidate.is_dir() and not candidate.is_symlink() and (candidate / "plan.json").is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def _data_root_of(ledger_root: Path) -> Path:
    return ledger_root.parent


def _campaign_plan(ledger_dir: Path) -> dict[str, Any] | None:
    path = ledger_dir / "plan.json"
    if not path.exists():
        return None
    payload, _raw = _read_json(path, "Campaign 账本 plan")
    _expect(
        payload,
        {"schema_version", "campaign_id", "campaign_mode", "target_version", "registration_operation_id", "admission_head_sha256", "project_ledger", "deadline_at_utc", "created_at_utc"},
        "Campaign 账本 plan",
    )
    if payload["schema_version"] != CAMPAIGN_PLAN_SCHEMA:
        raise ProjectLedgerError("Campaign 账本 plan schema 非法")
    return payload


def _registration_payload(campaign_plan: Mapping[str, Any], campaign_dir: Path) -> dict[str, Any]:
    return {
        "campaign_id": campaign_plan["campaign_id"],
        "campaign_dir": str(campaign_dir),
        "campaign_mode": campaign_plan["campaign_mode"],
        "target_version": campaign_plan["target_version"],
        "admission_head_sha256": campaign_plan["admission_head_sha256"],
        "deadline_at_utc": campaign_plan["deadline_at_utc"],
    }


def _push_batch(
    root: Path,
    plan: Mapping[str, Any],
    batch: Mapping[str, Any],
    *,
    campaign_dir: Path | None,
    now: datetime | None,
) -> dict[str, Any]:
    """把一个已 COMMIT 的 batch 推成一个项目事件；注册 batch 在当前 head 上重做 admission。"""

    commit = batch["commit"]
    batch_sha256 = commit["batch_sha256"]
    operation_id = commit["operation_id"]
    event_type = commit["event_type"]
    if batch_sha256 in set(plan["bootstrap_cutover"]["batch_sha256s"]) or operation_id in set(plan["bootstrap_cutover"]["operation_ids"]):
        return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": "bootstrap_cutover"}
    payload = _merge_batch_payload(batch)
    if event_type == "campaign_registered":
        head = _replay(root, plan, _load_events(root), rebuild_cache=False)
        if operation_id in head["operations"]:
            _new_head, status = append_project_event(root, operation_id=operation_id, event_type=event_type, payload={**payload, "registration_batch_sha256": batch_sha256}, source_batch_sha256=batch_sha256)
            return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": status}
        rejected_id = f"{operation_id}:rejected"
        if rejected_id in head["operations"]:
            return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": "already_rejected"}
        problems = admission_problems(
            plan,
            head,
            campaign_id=str(payload["campaign_id"]),
            campaign_mode=str(payload["campaign_mode"]),
            target_version=str(payload["target_version"]),
            now=now,
        )
        if problems:
            append_project_event(
                root,
                operation_id=rejected_id,
                event_type="campaign_registration_rejected",
                payload={
                    "campaign_id": payload["campaign_id"],
                    "registration_batch_sha256": batch_sha256,
                    "registration_operation_id": operation_id,
                    "admission_head_sha256": head["head_sha256"],
                    "reason": "; ".join(problems),
                },
                source_batch_sha256=batch_sha256,
            )
            return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": "rejected", "reason": problems}
        _new_head, status = append_project_event(
            root,
            operation_id=operation_id,
            event_type=event_type,
            payload={**payload, "registration_batch_sha256": batch_sha256},
            source_batch_sha256=batch_sha256,
        )
        return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": status}
    _new_head, status = append_project_event(root, operation_id=operation_id, event_type=event_type, payload=payload, source_batch_sha256=batch_sha256)
    return {"batch": str(batch["batch_dir"]), "operation_id": operation_id, "status": status}


def reconcile_project_ledger(root: Path, *, campaign_dir: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """补齐器：把 Campaign outbox（或总账 repairs outbox）中已 COMMIT 的 batch 推成项目事件。"""

    results: list[dict[str, Any]] = []
    with project_lock(root):
        plan, _raw = _load_plan(root)
        sources: list[tuple[str, Path, Path | None]] = []
        if campaign_dir is not None:
            ledger_dir = campaign_dir / CAMPAIGN_LEDGER_DIR_NAME
            if ledger_dir.exists():
                sources.append(("campaign", _private_dir(ledger_dir, "Campaign 账本目录") / "outbox", campaign_dir))
        sources.append(("repairs", root / "repairs" / "outbox", None))
        for kind, outbox, source_campaign in sources:
            for _n, batch_dir in _batch_dirs(outbox):
                batch = _read_batch(batch_dir)
                if not batch["committed"]:
                    results.append({"batch": str(batch_dir), "status": "uncommitted", "kind": kind})
                    break
                result = _push_batch(root, plan, batch, campaign_dir=source_campaign, now=now)
                result["kind"] = kind
                results.append(result)
        head = _replay(root, plan, _load_events(root), rebuild_cache=True)
    return {"schema_version": RECONCILE_REPORT_SCHEMA, "results": results, "head_sequence": head["sequence"], "head_sha256": head["head_sha256"], "blocked": head["blocked"]}


def register_campaign(
    root: Path,
    campaign_dir: Path,
    *,
    campaign_id: str,
    campaign_mode: str,
    target_version: str,
    deadline_at_utc: str | None,
    admission_head_sha256: str,
    operation_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """注册事务的 Campaign 侧与推送侧；须在项目锁内调用（由 admission_scope 保证）。"""

    if _PROJECT_LOCK_DEPTH == 0:
        raise ProjectLedgerError("注册必须在项目锁内执行")
    _safe_id(campaign_id, "campaign_id")
    if campaign_mode not in {"formal", "preflight_only"}:
        raise ProjectLedgerError("campaign_mode 非法")
    if not VERSION_RE.fullmatch(target_version):
        raise ProjectLedgerError("target_version 非法")
    if deadline_at_utc is not None:
        _timestamp(deadline_at_utc, "deadline_at_utc")
    registration_id = operation_id or f"register:{campaign_id}"
    plan, _raw = _load_plan(root)
    if deadline_at_utc is not None and _timestamp(deadline_at_utc, "deadline_at_utc") > _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
        raise ProjectLedgerError("Campaign deadline 超过项目绝对截止时间，拒绝注册")
    with campaign_ledger_lock(campaign_dir) as ledger_dir:
        campaign_plan = _campaign_plan(ledger_dir)
        if campaign_plan is None:
            campaign_plan = {
                "schema_version": CAMPAIGN_PLAN_SCHEMA,
                "campaign_id": campaign_id,
                "campaign_mode": campaign_mode,
                "target_version": target_version,
                "registration_operation_id": registration_id,
                "admission_head_sha256": admission_head_sha256,
                "project_ledger": str(root),
                "deadline_at_utc": deadline_at_utc,
                "created_at_utc": _utc_now(),
            }
            _write_once(ledger_dir / "plan.json", campaign_plan)
        elif campaign_plan["campaign_id"] != campaign_id or campaign_plan["registration_operation_id"] != registration_id:
            raise ProjectLedgerError("Campaign 账本 plan 与注册请求身份不一致")
        payload = _registration_payload(campaign_plan, campaign_dir)
        batch = write_batch(
            ledger_dir,
            operation_id=registration_id,
            event_type="campaign_registered",
            payload=payload,
            source={"kind": "campaign_plan", "sha256": _digest(campaign_plan)},
        )
    head = _replay(root, plan, _load_events(root), rebuild_cache=False)
    if registration_id in head["operations"]:
        status = "duplicate"
    else:
        if head["head_sha256"] != campaign_plan["admission_head_sha256"]:
            raise ProjectLedgerError("总账 head 已变化，注册 CAS 失败；由补齐器在当前 head 上重做 admission")
        _head, status = append_project_event(
            root,
            operation_id=registration_id,
            event_type="campaign_registered",
            payload={**payload, "registration_batch_sha256": batch["batch_sha256"]},
            source_batch_sha256=batch["batch_sha256"],
            expected_head_sha256=campaign_plan["admission_head_sha256"],
        )
    return {"operation_id": registration_id, "batch_sha256": batch["batch_sha256"], "status": status}


class Admission:
    """plan 命令的 admission 上下文：持项目锁，携带 admission head。"""

    def __init__(self, root: Path, plan: Mapping[str, Any], head: Mapping[str, Any]) -> None:
        self.root = root
        self.plan = plan
        self.head = head

    @property
    def admission_head_sha256(self) -> str:
        return str(self.head["head_sha256"])

    def register(self, campaign_dir: Path, *, campaign_id: str, campaign_mode: str, target_version: str, deadline_at_utc: str | None) -> dict[str, Any]:
        return register_campaign(
            self.root,
            campaign_dir,
            campaign_id=campaign_id,
            campaign_mode=campaign_mode,
            target_version=target_version,
            deadline_at_utc=deadline_at_utc,
            admission_head_sha256=self.admission_head_sha256,
        )


@contextlib.contextmanager
def admission_scope(
    campaign_dir: Path,
    *,
    campaign_id: str,
    campaign_mode: str,
    target_version: str,
    require: bool,
    deadline_at_utc: str | None = None,
    now: datetime | None = None,
) -> Iterator[Admission | None]:
    """plan 命令的准入作用域：找不到总账时按 ``require`` 决定拒绝还是放行。"""

    root = find_project_ledger(campaign_dir.parent)
    if root is None:
        if require:
            raise ProjectLedgerError("项目总账不存在，0.154 起 formal Campaign 禁止在总账之外创建")
        yield None
        return
    with project_lock(root):
        plan, _raw = _load_plan(root)
        _check_fixture_only(root, plan, campaign_dir)
        head = _replay(root, plan, _load_events(root), rebuild_cache=True)
        problems = admission_problems(plan, head, campaign_id=campaign_id, campaign_mode=campaign_mode, target_version=target_version, now=now, allow_registered=True)
        if deadline_at_utc is not None and _timestamp(deadline_at_utc, "deadline_at_utc") > _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
            problems.append("Campaign deadline 超过项目绝对截止时间")
        if problems:
            raise ProjectLedgerError("admission 拒绝：" + "；".join(problems))
        yield Admission(root, plan, head)


def _check_fixture_only(root: Path, plan: Mapping[str, Any], campaign_dir: Path) -> None:
    """fixture_only 总账只能服务 staging 树：总账自身必须位于某个 ``staging`` 段之下，
    Campaign 必须位于总账父目录之内。生产规范根 ``<data>/evidence/campaigns`` 永远不满足。"""

    if not plan["fixture_only"]:
        return
    data_root = _data_root_of(root).resolve(strict=False)
    if STAGING_DIR_NAME not in data_root.parts:
        raise ProjectLedgerError("fixture_only 总账必须位于 staging 目录树内")
    try:
        campaign_dir.resolve(strict=False).relative_to(data_root)
    except ValueError as error:
        raise ProjectLedgerError("fixture_only 总账只允许 staging 路径下的 Campaign") from error


def assert_campaign_admitted(campaign_dir: Path, *, command: str, require: bool, now: datetime | None = None) -> dict[str, Any] | None:
    """消费者门禁：补齐器先行，再锁内重放；不满足即拒绝。"""

    if command not in CONSUMER_COMMANDS:
        raise ProjectLedgerError(f"未知消费者命令：{command}")
    root = find_project_ledger(campaign_dir)
    if root is None:
        if require:
            raise ProjectLedgerError(f"{command} 拒绝：项目总账不存在")
        return None
    reconcile_project_ledger(root, campaign_dir=campaign_dir, now=now)
    with project_lock(root):
        plan, _raw = _load_plan(root)
        _check_fixture_only(root, plan, campaign_dir)
        head = _replay(root, plan, _load_events(root), rebuild_cache=True)
        ledger_dir = campaign_dir / CAMPAIGN_LEDGER_DIR_NAME
        campaign_plan = _campaign_plan(ledger_dir) if ledger_dir.exists() else None
        if campaign_plan is None:
            raise ProjectLedgerError(f"{command} 拒绝：Campaign 未在项目总账注册（缺少账本 plan）")
        campaign_id = str(campaign_plan["campaign_id"])
        if campaign_id in head["rejected_campaigns"]:
            raise ProjectLedgerError(f"{command} 拒绝：Campaign {campaign_id} 注册已被追加拒绝：{head['rejected_campaigns'][campaign_id]['reason']}")
        registered = head["registered_campaigns"].get(campaign_id)
        if registered is None:
            raise ProjectLedgerError(f"{command} 拒绝：Campaign {campaign_id} 无注册事件（注册 batch 未 COMMIT 或未推送）")
        if campaign_id in head["terminal_campaigns"]:
            raise ProjectLedgerError(f"{command} 拒绝：Campaign {campaign_id} 已终态：{head['terminal_campaigns'][campaign_id]['terminal_reason']}")
        problems: list[str] = []
        if head["blocked"]:
            problems.append(f"总账 blocked：{head['unresolved_operation_ids']}")
        if head["remaining_live_requests"] is not None and head["remaining_live_requests"] <= 0:
            problems.append("项目请求预算为 0")
        if head["root_causes_at_limit"]:
            problems.append(f"根因达上限：{head['root_causes_at_limit']}")
        current = now or datetime.now(timezone.utc)
        if current >= _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
            problems.append("项目绝对截止时间已到")
        deadline = campaign_plan.get("deadline_at_utc")
        if isinstance(deadline, str) and _timestamp(deadline, "deadline_at_utc") > _timestamp(plan["absolute_deadline_utc"], "absolute_deadline_utc"):
            problems.append("Campaign deadline 超过项目绝对截止时间")
        if problems:
            raise ProjectLedgerError(f"{command} 拒绝：" + "；".join(problems))
        return {"project_ledger": str(root), "campaign_id": campaign_id, "head_sequence": head["sequence"], "head_sha256": head["head_sha256"], "remaining_live_requests": head["remaining_live_requests"]}


# ---------------------------------------------------------------------------
# 修复收据
# ---------------------------------------------------------------------------


def record_root_cause_repair(
    root: Path,
    *,
    root_cause_id: str,
    kind: str,
    bindings: Mapping[str, Any],
    note: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """写 root-cause-repair/v1 收据，经 repairs outbox 推送 root_cause_repaired。"""

    _safe_id(root_cause_id, "root_cause_id")
    if kind not in REPAIR_KINDS:
        raise ProjectLedgerError("修复类型只能是 code 或 environment")
    required = (
        {"fix_commit_sha", "regression_receipt_sha256", "deployment_receipt_sha256"}
        if kind == "code"
        else {"environment_repair_receipt_sha256", "clean_environment_receipt_sha256"}
    )
    if set(bindings) != required:
        raise ProjectLedgerError(f"{kind} 修复收据绑定必须恰好是 {sorted(required)}")
    for key, value in bindings.items():
        if key == "fix_commit_sha":
            if not isinstance(value, str) or not re.fullmatch(r"^[0-9a-f]{40}$", value):
                raise ProjectLedgerError("fix_commit_sha 必须是完整提交 SHA")
        else:
            _sha_field(value, key)
    with project_lock(root):
        plan, _raw = _load_plan(root)
        head = _replay(root, plan, _load_events(root), rebuild_cache=False)
        if root_cause_id not in head["root_cause_counts"]:
            raise ProjectLedgerError(f"根因 {root_cause_id} 未在总账出现过，无从修复")
        receipt = {
            "schema_version": REPAIR_SCHEMA,
            "root_cause_id": root_cause_id,
            "kind": kind,
            "bindings": dict(bindings),
            "note": note,
            "recorded_at_utc": now or _utc_now(),
            "root_cause_codes_sha256": plan["root_cause_codes_sha256"],
        }
        receipt_sha256 = _digest(receipt)
        operation_id = f"repair:{root_cause_id}:{receipt_sha256[:16]}"
        receipts_root = _private_dir(root / "repairs" / "receipts", "修复收据目录", create=True)
        receipt_path = receipts_root / f"{operation_id.replace(':', '-')}.json"
        if not receipt_path.exists():
            _write_once(receipt_path, receipt)
        batch = write_batch(
            _private_dir(root / "repairs", "repairs 目录"),
            operation_id=operation_id,
            event_type="root_cause_repaired",
            payload={"root_cause_id": root_cause_id, "kind": kind, "repair_receipt_sha256": receipt_sha256, "bindings": dict(bindings)},
            source={"kind": "repair_receipt", "sha256": receipt_sha256},
        )
        report = reconcile_project_ledger(root)
    return {"operation_id": operation_id, "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha256, "batch_sha256": batch["batch_sha256"], "head_sequence": report["head_sequence"]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    payload, _raw = _read_json(path, label)
    return payload


FIXTURE_APPROVER = "staging-rehearsal"


def create_fixture_ledger(data_root: Path, *, hours: int = 24) -> Path:
    """在 staging 树内创建 fixture_only 总账（A2.5／演练用），已存在则原样返回。

    fixture_only 总账不冻结老板批准，只服务 staging 路径；``data_root`` 必须位于
    某个 ``staging`` 段之下，否则拒绝，避免被当成生产总账。
    """

    data_root = Path(data_root)
    if STAGING_DIR_NAME not in data_root.resolve(strict=False).parts:
        raise ProjectLedgerError("fixture_only 总账只能创建在 staging 目录树内")
    ledger_root = data_root / LEDGER_DIR_NAME
    if (ledger_root / "plan.json").is_file():
        plan, _raw = _load_plan(ledger_root)
        if not plan["fixture_only"]:
            raise ProjectLedgerError("staging 树内已有非 fixture_only 总账")
        return ledger_root
    from datetime import timedelta

    deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    create_project_ledger(
        ledger_root,
        project_id=f"fixture-{data_root.name}",
        absolute_deadline_utc=deadline,
        deadline_approved_by=FIXTURE_APPROVER,
        estimation_policy="upper_bound_from_sibling_or_turn_ratio",
        estimation_policy_approved_by=FIXTURE_APPROVER,
        fixture_only=True,
        formal_open_limit=64,
    )
    return ledger_root


def register_existing_campaign(campaign_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """按 Campaign 清单在总账内完成 admission 与注册；用于演练夹具与中断后补注册。"""

    manifest, _raw = _read_json(campaign_dir / "campaign.json", "Campaign 清单")
    campaign_id = str(manifest.get("campaign_id") or "")
    campaign_mode = str(manifest.get("campaign_mode") or "")
    target_version = str(manifest.get("target_version") or "")
    deadline = None
    plan_path = campaign_dir / "control" / "vc" / "campaign-plan.json"
    if plan_path.is_file() and not plan_path.is_symlink():
        payload, _plan_raw = _read_json(plan_path, "Campaign 总计划")
        value = payload.get("original_deadline_at_utc")
        deadline = value if isinstance(value, str) else None
    with admission_scope(
        campaign_dir,
        campaign_id=campaign_id,
        campaign_mode=campaign_mode,
        target_version=target_version,
        require=requires_project_ledger(campaign_mode, target_version),
        now=now,
    ) as admission:
        if admission is None:
            return {"status": "no_ledger"}
        return admission.register(
            campaign_dir,
            campaign_id=campaign_id,
            campaign_mode=campaign_mode,
            target_version=target_version,
            deadline_at_utc=deadline,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="追加式项目总账：创建、状态、补齐、修复收据。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-project-ledger", help="创建只写一次的项目总账")
    create.add_argument("--ledger-dir", type=Path, required=True)
    create.add_argument("--project-id", required=True)
    create.add_argument("--absolute-deadline-utc", required=True)
    create.add_argument("--deadline-approved-by", required=True)
    create.add_argument("--estimation-policy", choices=ESTIMATION_POLICIES, required=True)
    create.add_argument("--estimation-policy-approved-by", required=True)
    create.add_argument("--fixture-only", action="store_true")
    create.add_argument("--live-request-budget", type=int)
    create.add_argument("--formal-open-limit", type=int, default=DEFAULT_FORMAL_OPEN_LIMIT)
    create.add_argument("--bootstrap-json", type=Path, help="含 initial_identity_keys、initial_estimated_count、initial_root_cause_counts、bootstrap_cutover、closed_ledger_heads、bound_receipts 的 JSON")
    status = subparsers.add_parser("status", help="锁内重放并输出 head")
    status.add_argument("--ledger-dir", type=Path, required=True)
    reconcile = subparsers.add_parser("reconcile-project-ledger", help="推送已 COMMIT 的 batch")
    reconcile.add_argument("--ledger-dir", type=Path, required=True)
    reconcile.add_argument("--campaign-dir", type=Path)
    repair = subparsers.add_parser("record-root-cause-repair", help="写修复收据并推送 root_cause_repaired")
    repair.add_argument("--ledger-dir", type=Path, required=True)
    repair.add_argument("--root-cause-id", required=True)
    repair.add_argument("--kind", choices=REPAIR_KINDS, required=True)
    repair.add_argument("--binding", action="append", default=[], help="KEY=VALUE，可重复")
    repair.add_argument("--note")
    admitted = subparsers.add_parser("assert-campaign-admitted", help="消费者门禁只读检查")
    admitted.add_argument("--campaign-dir", type=Path, required=True)
    admitted.add_argument("--consumer", choices=sorted(CONSUMER_COMMANDS), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "create-project-ledger":
            bootstrap = _load_json_file(arguments.bootstrap_json, "bootstrap JSON") if arguments.bootstrap_json else {}
            result = create_project_ledger(
                arguments.ledger_dir,
                project_id=arguments.project_id,
                absolute_deadline_utc=arguments.absolute_deadline_utc,
                deadline_approved_by=arguments.deadline_approved_by,
                estimation_policy=arguments.estimation_policy,
                estimation_policy_approved_by=arguments.estimation_policy_approved_by,
                fixture_only=arguments.fixture_only,
                live_request_budget=arguments.live_request_budget,
                formal_open_limit=arguments.formal_open_limit,
                initial_identity_keys=bootstrap.get("initial_identity_keys"),
                initial_precise_count=bootstrap.get("initial_precise_count"),
                initial_estimated_count=int(bootstrap.get("initial_estimated_count", 0)),
                initial_root_cause_counts=bootstrap.get("initial_root_cause_counts"),
                bootstrap_cutover=bootstrap.get("bootstrap_cutover"),
                closed_ledger_heads=bootstrap.get("closed_ledger_heads"),
                bound_receipts=bootstrap.get("bound_receipts"),
            )
            result = {k: v for k, v in result.items() if k not in {"accounted_identity_index", "operations"}}
        elif arguments.command == "status":
            result = replay_head(arguments.ledger_dir)
            result = {k: v for k, v in result.items() if k not in {"accounted_identity_index", "operations"}}
        elif arguments.command == "reconcile-project-ledger":
            result = reconcile_project_ledger(arguments.ledger_dir, campaign_dir=arguments.campaign_dir)
        elif arguments.command == "record-root-cause-repair":
            bindings: dict[str, str] = {}
            for item in arguments.binding:
                key, separator, value = item.partition("=")
                if not separator:
                    raise ProjectLedgerError("--binding 必须为 KEY=VALUE")
                bindings[key] = value
            result = record_root_cause_repair(arguments.ledger_dir, root_cause_id=arguments.root_cause_id, kind=arguments.kind, bindings=bindings, note=arguments.note)
        else:
            result = assert_campaign_admitted(arguments.campaign_dir, command=arguments.consumer, require=True) or {}
    except (ProjectLedgerError, OSError, root_cause.RootCauseError) as error:
        print(f"项目总账失败：{error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
