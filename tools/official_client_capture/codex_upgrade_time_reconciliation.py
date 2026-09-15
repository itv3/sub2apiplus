"""升级时间对账（只读）：从可信事件边界把墙钟时间切成可复核的区间。

方案 A0a-6。可信事件边界只取六类来源，全部来自不可变产物：

1. 时间账本 ``evidence/control/*/ledger.json``（早期升级账本）与 ``control/*/ledger.json``
   （Campaign 计时账本）及其 ``events/NNNNNN.json``（序号连续，``previous_event_sha256``
   必须等于前一事件文件字节的 SHA-256）；schema 不是升级计时账本的目录只登记不解析；
2. 监督器事件流 ``control/**/run-<sha>/events.ndjson``（逐行 ``event_sha256`` 可复算，
   ``previous_event_sha256`` 连续，序号连续）；
3. 受管部署收据 ``control/codex-*-supervisor-enable-*.json`` 的 ``created_at_utc``；
4. Campaign 清单 ``evidence/campaigns/*/campaign.json`` 的 ``created_at_utc``；
5. 审计目录 ``audit/*/request.json`` 的 ``requested_at_utc`` 与 ``receipt.json``／
   ``failure.json`` 的完成时间。它与 Campaign 清单同源同信任级别，是 2026-09-13
   之后 VC-0 closeout 执行的唯一收据证据，因此纳入边界；
6. git 提交时间：``--git-log-file``（由 ``git log --format='%H%x09%cI%x09%s'`` 导出）或
   ``--git-repo`` 现场读取。

mtime 与文档时间不参与边界，只能作为人工分类收据的支撑证据。

区间分类规则（确定性、可重放）：

* 时间轴断点 = 窗口两端 + 全部点事件 + 全部证据区间的起止 + 人工分类条目的起止；
  相邻断点构成基本区间。
* 基本区间先按证据区间归类，优先级从高到低：监督器 run（非 bootstrap）按其
  phase 记 ``vc0_execution``／``vc1_execution``／``vc_execution:<phase>``；审计目录
  请求到完成记 ``vc0_execution``（目录名含 ``vc0-closeout``）、``vc1_execution``
  （含 ``vc1``）或 ``vc_execution:audit``；账本某阶段 active 且未停线记该阶段执行。
  这些区间 ``basis.kind = evidence``。
* 无证据覆盖时按基本区间终点的点事件推断（``basis.kind = inferred``）：终点是
  git 提交则 ``tool_repair``，但长于空闲阈值时记 ``idle``；终点是部署收据或
  bootstrap 监督器 run 则 ``deployment``，终点是 Campaign 创建、执行 run 起点或审计
  请求起点则 ``campaign_creation``，这两类都只在不超过创建阈值时成立，超过空闲阈值
  记 ``idle``，介于两者之间不猜；账本处于停线且终点是 ``recovery_verified`` 或新
  账本创建则 ``stop_gap``。
* 其余记 ``unclassified``（``basis.kind = none``）。
* 人工分类收据 ``upgrade-time-manual-classification/v1``：每条必须绑定至少一份存在
  且摘要一致的支撑证据；只能覆盖 ``inferred`` 与 ``none`` 区间，不得覆盖证据区间，
  条目之间不得重叠，不得使用 ``unclassified``。覆盖后的区间 ``basis.kind = manual``。

输出 ``upgrade-time-reconciliation/v1``：来源清单、事件清单、证据区间、分类区间、
按类别与按证据强度的秒数汇总。各类之和必须等于窗口墙钟，否则失败关闭。
本模块不写任何证据，只写一份不可覆盖的收据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts

SCHEMA_VERSION = "upgrade-time-reconciliation/v1"
MANUAL_SCHEMA_VERSION = "upgrade-time-manual-classification/v1"
SUPERVISOR_EVENT_SCHEMA = "codex-upgrade-supervisor-event/v1"
DEPLOY_RECEIPT_SCHEMA = "codex-arm64-supervisor-enable/v1"
FIXED_CATEGORIES = (
    "vc0_execution",
    "vc1_execution",
    "tool_repair",
    "deployment",
    "campaign_creation",
    "analysis_and_planning",
    "stop_gap",
    "idle",
    "unclassified",
)
BASIS_KINDS = ("evidence", "inferred", "manual", "none")
DEFAULT_IDLE_THRESHOLD_MINUTES = 240
DEFAULT_CAMPAIGN_CREATION_THRESHOLD_MINUTES = 30
MAX_NDJSON_BYTES = 64 * 1024 * 1024
MAX_GIT_LOG_BYTES = 16 * 1024 * 1024
GIT_LOG_FORMAT = "%H%x09%cI%x09%s"
COMMIT_LINE_RE = re.compile(r"^([0-9a-f]{40})\t(\S+)\t(.*)$")
RUN_DIR_RE = re.compile(r"^run-[0-9a-f]{64}$")
DEPLOY_RECEIPT_GLOB = "codex-*-supervisor-enable-*.json"
VC_EXECUTION_RE = re.compile(r"^vc_execution:[A-Za-z0-9._-]{1,32}$")
PRIORITY_SUPERVISOR = 1
PRIORITY_AUDIT = 2
PRIORITY_LEDGER = 3
LEDGER_STAGE_OPEN = {"stage_started"}
LEDGER_STAGE_CLOSE = {"stage_completed", "stage_abandoned", "upgrade_completed"}


class TimeReconciliationError(ValueError):
    """来源不可信、链断裂、分类不自洽或人工收据不合规。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TimeReconciliationError(f"{label}缺少时间戳")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise TimeReconciliationError(f"{label}不是 RFC3339 时间戳：{value}") from error
    if parsed.tzinfo is None:
        raise TimeReconciliationError(f"{label}缺少时区：{value}")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _seconds(value: timedelta) -> float:
    return round(value.total_seconds(), 3)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_trusted_json(path: Path, label: str) -> dict[str, Any]:
    payload, _raw = closeout._load_json(closeout._trusted_file(path, label), label)
    return payload


def _phase_category(phase: Any, *, fallback: str) -> str:
    if phase == "VC-0":
        return "vc0_execution"
    if phase == "VC-1":
        return "vc1_execution"
    if isinstance(phase, str) and phase and closeout.SAFE_ID_RE.fullmatch(phase):
        return f"vc_execution:{phase}"
    return fallback


def _valid_category(value: Any) -> bool:
    return isinstance(value, str) and (
        value in FIXED_CATEGORIES or bool(VC_EXECUTION_RE.fullmatch(value))
    )


# ---------------------------------------------------------------------------
# 来源一：时间账本
# ---------------------------------------------------------------------------


LEDGER_PARENTS = (("evidence", "control"), ("control",))


def _ledger_roots(data_root: Path) -> list[Path]:
    """账本目录：``evidence/control/*``（早期升级账本）与 ``control/*``（Campaign 计时账本）。"""

    roots: list[Path] = []
    for parts in LEDGER_PARENTS:
        parent = data_root.joinpath(*parts)
        if not parent.is_dir() or parent.is_symlink():
            continue
        for child in sorted(parent.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            plan_path = child / "ledger.json"
            if plan_path.is_symlink() or not plan_path.is_file():
                continue
            roots.append(child)
    return roots


def _load_ledgers(data_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ledgers: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for child in _ledger_roots(data_root):
        plan_path = child / "ledger.json"
        ledger_id = str(child.relative_to(data_root))
        plan = _load_trusted_json(plan_path, f"账本 {ledger_id} 计划")
        if plan.get("schema_version") != timing_ledger.PLAN_SCHEMA:
            # 其它类型账本（如修复计时账本）不是升级计时账本，只登记不解析。
            ignored.append({"ledger_id": ledger_id, "schema_version": plan.get("schema_version")})
            continue
        created = _timestamp(plan.get("created_at_utc"), f"账本 {ledger_id} created_at_utc")
        try:
            raw_events = timing_ledger._load_events(child)
        except timing_ledger.TimingLedgerError as error:
            raise TimeReconciliationError(f"账本 {ledger_id} 事件不可信：{error}") from error
        events: list[dict[str, Any]] = []
        previous_digest: str | None = None
        for index, (event, raw) in enumerate(raw_events, start=1):
            try:
                normalized = timing_ledger._validate_event_shape(child, event, index)
            except timing_ledger.TimingLedgerError as error:
                raise TimeReconciliationError(f"账本 {ledger_id} event {index} 形状非法：{error}") from error
            if normalized.get("previous_event_sha256") != previous_digest:
                raise TimeReconciliationError(f"账本 {ledger_id} event {index} 摘要链断裂")
            previous_digest = _sha256(raw)
            events.append(normalized)
        if not events:
            raise TimeReconciliationError(f"账本 {ledger_id} 没有事件")
        ledgers.append(
            {
                "ledger_id": ledger_id,
                "path": str(child),
                "upgrade_id": plan.get("upgrade_id"),
                "created_at": created,
                "events": events,
            }
        )
    return ledgers, ignored


def _ledger_points_and_spans(
    ledgers: list[dict[str, Any]], *, until: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """把账本事件展开成点事件、阶段执行区间与停线区间。

    未关闭的阶段与未恢复的停线延续到下一个账本创建时刻（新账本即旧账本弃用），
    没有后继账本时延续到窗口终点。停线期间不计阶段执行。
    """

    creations = sorted(ledger["created_at"] for ledger in ledgers)
    points: list[dict[str, Any]] = []
    active_spans: list[dict[str, Any]] = []
    stopped_spans: list[dict[str, Any]] = []
    for ledger in ledgers:
        ledger_id = ledger["ledger_id"]
        successor = next((t for t in creations if t > ledger["created_at"]), None)
        horizon = min(successor, until) if successor is not None else until
        points.append(
            {
                "time": ledger["created_at"],
                "source": "ledger",
                "kind": "ledger_created",
                "ref": ledger_id,
            }
        )
        open_stage: tuple[str, datetime, int] | None = None
        suspended_stage: tuple[str, int] | None = None
        stop_since: tuple[datetime, int] | None = None
        for event in ledger["events"]:
            time = _timestamp(event["recorded_at_utc"], f"账本 {ledger_id} event")
            event_type = str(event["event_type"])
            points.append(
                {
                    "time": time,
                    "source": "ledger",
                    "kind": f"ledger_event:{event_type}",
                    "ref": f"{ledger_id}#{event['sequence']}",
                    "phase": event.get("phase"),
                }
            )
            if event_type in LEDGER_STAGE_OPEN:
                if open_stage is not None:
                    active_spans.append(_ledger_span(ledger_id, open_stage, time))
                open_stage = (str(event["phase"]), time, int(event["sequence"]))
                suspended_stage = None
            elif event_type in LEDGER_STAGE_CLOSE:
                if open_stage is not None:
                    active_spans.append(_ledger_span(ledger_id, open_stage, time))
                open_stage = None
                suspended_stage = None
            elif event_type == "stop_the_line":
                # 停线期间不计阶段执行：把 active 阶段截断并挂起，恢复后再续。
                if open_stage is not None:
                    active_spans.append(_ledger_span(ledger_id, open_stage, time))
                    suspended_stage = (open_stage[0], open_stage[2])
                    open_stage = None
                if stop_since is None:
                    stop_since = (time, int(event["sequence"]))
            elif event_type == "recovery_verified":
                if stop_since is not None:
                    stopped_spans.append(_stopped_span(ledger_id, stop_since, time))
                    stop_since = None
                if suspended_stage is not None:
                    open_stage = (suspended_stage[0], time, suspended_stage[1])
                    suspended_stage = None
        if open_stage is not None and open_stage[1] < horizon:
            active_spans.append(_ledger_span(ledger_id, open_stage, horizon))
        if stop_since is not None and stop_since[0] < horizon:
            stopped_spans.append(_stopped_span(ledger_id, stop_since, horizon))
    return points, active_spans, stopped_spans


def _ledger_span(ledger_id: str, stage: tuple[str, datetime, int], end: datetime) -> dict[str, Any]:
    phase, start, sequence = stage
    return {
        "start": start,
        "end": end,
        "source": "ledger",
        "kind": "ledger_stage",
        "ref": f"{ledger_id}#{sequence}",
        "phase": phase,
        "category": _phase_category(phase, fallback="vc_execution:ledger"),
        "priority": PRIORITY_LEDGER,
    }


def _stopped_span(ledger_id: str, stop: tuple[datetime, int], end: datetime) -> dict[str, Any]:
    start, sequence = stop
    return {"start": start, "end": end, "source": "ledger", "kind": "ledger_stopped", "ref": f"{ledger_id}#{sequence}"}


# ---------------------------------------------------------------------------
# 来源二：监督器事件流
# ---------------------------------------------------------------------------


def _supervisor_run_dirs(control: Path) -> list[Path]:
    candidates: list[Path] = []
    if not control.is_dir() or control.is_symlink():
        return candidates
    for child in sorted(control.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        if RUN_DIR_RE.fullmatch(child.name):
            candidates.append(child)
            continue
        for grand in sorted(child.iterdir()):
            if not grand.is_symlink() and grand.is_dir() and RUN_DIR_RE.fullmatch(grand.name):
                candidates.append(grand)
    return candidates


def _load_supervisor_runs(data_root: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run_dir in _supervisor_run_dirs(data_root / "control"):
        path = run_dir / "events.ndjson"
        if path.is_symlink() or not path.is_file():
            continue
        resolved = closeout._trusted_file(path, f"监督器事件流 {run_dir.name}", maximum=MAX_NDJSON_BYTES)
        records: list[dict[str, Any]] = []
        previous: str | None = None
        for line in resolved.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise TimeReconciliationError(f"监督器事件流 {run_dir.name} 含损坏 JSON") from error
            if not isinstance(record, dict) or record.get("schema_version") != SUPERVISOR_EVENT_SCHEMA:
                raise TimeReconciliationError(f"监督器事件流 {run_dir.name} 记录 schema 非法")
            digest = record.get("event_sha256")
            unsigned = {key: value for key, value in record.items() if key != "event_sha256"}
            if digest != _sha256(vc_artifacts.canonical_bytes(unsigned)):
                raise TimeReconciliationError(f"监督器事件流 {run_dir.name} 第 {len(records) + 1} 条摘要不一致")
            if record.get("previous_event_sha256") != previous:
                raise TimeReconciliationError(f"监督器事件流 {run_dir.name} 第 {len(records) + 1} 条摘要链断裂")
            if record.get("sequence") != len(records) + 1:
                raise TimeReconciliationError(f"监督器事件流 {run_dir.name} 序号不连续")
            previous = digest
            records.append(record)
        if not records:
            continue
        times = [_timestamp(r.get("recorded_at_utc"), f"监督器事件流 {run_dir.name}") for r in records]
        runs.append(
            {
                "run_id": run_dir.name,
                "path": str(resolved),
                "campaign_id": records[0].get("campaign_id"),
                "phase": records[0].get("phase"),
                "phases": sorted({str(r.get("phase")) for r in records}),
                "start": min(times),
                "end": max(times),
                "event_count": len(records),
                "last_event_type": records[-1].get("event_type"),
            }
        )
    return runs


# ---------------------------------------------------------------------------
# 来源三～五：部署收据、Campaign 清单、审计目录
# ---------------------------------------------------------------------------


def _load_deployments(data_root: Path) -> list[dict[str, Any]]:
    control = data_root / "control"
    deployments: list[dict[str, Any]] = []
    if not control.is_dir() or control.is_symlink():
        return deployments
    for path in sorted(control.glob(DEPLOY_RECEIPT_GLOB)):
        if path.is_symlink() or not path.is_file():
            continue
        receipt = _load_trusted_json(path, f"部署收据 {path.name}")
        if receipt.get("schema_version") != DEPLOY_RECEIPT_SCHEMA:
            raise TimeReconciliationError(f"部署收据 {path.name} schema 非法")
        deployments.append(
            {
                "ref": path.name,
                "time": _timestamp(receipt.get("created_at_utc"), f"部署收据 {path.name} created_at_utc"),
                "status": receipt.get("status"),
                "campaign_id": receipt.get("campaign_id"),
                "tool_files_sha256": receipt.get("tool_files_sha256"),
            }
        )
    return deployments


def _load_campaigns(data_root: Path) -> list[dict[str, Any]]:
    campaigns_root = data_root / "evidence" / "campaigns"
    campaigns: list[dict[str, Any]] = []
    if not campaigns_root.is_dir() or campaigns_root.is_symlink():
        return campaigns
    for child in sorted(campaigns_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        manifest_path = child / "campaign.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        manifest = _load_trusted_json(manifest_path, f"Campaign 清单 {child.name}")
        campaigns.append(
            {
                "ref": child.name,
                "campaign_id": manifest.get("campaign_id"),
                "campaign_mode": manifest.get("campaign_mode"),
                "time": _timestamp(manifest.get("created_at_utc"), f"Campaign {child.name} created_at_utc"),
            }
        )
    return campaigns


def _audit_category(name: str) -> str:
    if "vc0-closeout" in name:
        return "vc0_execution"
    if "vc1" in name:
        return "vc1_execution"
    return "vc_execution:audit"


def _load_audits(data_root: Path, *, until: datetime) -> list[dict[str, Any]]:
    audit_root = data_root / "audit"
    audits: list[dict[str, Any]] = []
    if not audit_root.is_dir() or audit_root.is_symlink():
        return audits
    for child in sorted(audit_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        request_path = child / "request.json"
        if request_path.is_symlink() or not request_path.is_file():
            continue
        request = _load_trusted_json(request_path, f"审计请求 {child.name}")
        start = _timestamp(request.get("requested_at_utc"), f"审计请求 {child.name} requested_at_utc")
        end: datetime | None = None
        outcome = "open"
        for name, field in (("receipt.json", "completed_at_utc"), ("failure.json", "failed_at_utc")):
            path = child / name
            if path.is_symlink() or not path.is_file():
                continue
            payload = _load_trusted_json(path, f"审计结果 {child.name}/{name}")
            candidate = _timestamp(payload.get(field), f"审计结果 {child.name}/{name} {field}")
            if end is None or candidate < end:
                end = candidate
                outcome = "passed" if name == "receipt.json" else "failed"
        if end is None:
            end = until
        if end < start:
            raise TimeReconciliationError(f"审计目录 {child.name} 完成时间早于请求时间")
        audits.append(
            {
                "ref": child.name,
                "formal_campaign_id": request.get("formal_campaign_id"),
                "start": start,
                "end": end,
                "outcome": outcome,
                "category": _audit_category(child.name),
            }
        )
    return audits


# ---------------------------------------------------------------------------
# 来源六：git 提交
# ---------------------------------------------------------------------------


def _parse_commit_lines(text: str, *, label: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = COMMIT_LINE_RE.match(line)
        if not match:
            raise TimeReconciliationError(f"{label}行格式非法：{line[:80]}")
        sha, stamp, subject = match.groups()
        commits.append(
            {
                "ref": sha,
                "time": _timestamp(stamp, f"{label} {sha[:12]} 提交时间"),
                "subject": subject,
                "kind": "commit_registration" if subject.startswith("chore") else "commit_repair",
            }
        )
    return commits


def _load_commits(*, git_log_file: Path | None, git_repo: Path | None, since: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if git_log_file is not None and git_repo is not None:
        raise TimeReconciliationError("--git-log-file 与 --git-repo 只能二选一")
    if git_log_file is not None:
        resolved = closeout._trusted_file(git_log_file, "git 提交清单", maximum=MAX_GIT_LOG_BYTES)
        raw = resolved.read_bytes()
        commits = _parse_commit_lines(raw.decode("utf-8"), label="git 提交清单")
        return commits, {"mode": "file", "path": str(resolved), "sha256": _sha256(raw)}
    if git_repo is not None:
        repo = closeout._trusted_directory(git_repo, "git 仓库")
        completed = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={_iso(since)}", f"--format={GIT_LOG_FORMAT}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise TimeReconciliationError(f"git log 失败：{completed.stderr.strip()[:200]}")
        commits = _parse_commit_lines(completed.stdout, label="git log")
        return commits, {"mode": "repo", "path": str(repo)}
    return [], {"mode": "none"}


# ---------------------------------------------------------------------------
# 人工分类收据
# ---------------------------------------------------------------------------


def _load_manual_classification(
    path: Path | None, *, evidence_root: Path, since: datetime, until: datetime
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if path is None:
        return [], None
    resolved = closeout._trusted_file(path, "人工分类收据")
    payload, raw = closeout._load_json(resolved, "人工分类收据")
    if payload.get("schema_version") != MANUAL_SCHEMA_VERSION:
        raise TimeReconciliationError("人工分类收据 schema 非法")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TimeReconciliationError("人工分类收据 entries 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TimeReconciliationError(f"人工分类条目 {index} 不是对象")
        start = _timestamp(entry.get("start_utc"), f"人工分类条目 {index} start_utc")
        end = _timestamp(entry.get("end_utc"), f"人工分类条目 {index} end_utc")
        if not since <= start < end <= until:
            raise TimeReconciliationError(f"人工分类条目 {index} 区间必须落在窗口内且起点早于终点")
        category = entry.get("category")
        if not _valid_category(category) or category == "unclassified":
            raise TimeReconciliationError(f"人工分类条目 {index} 类别非法")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise TimeReconciliationError(f"人工分类条目 {index} 必须绑定至少一份支撑证据")
        bound: list[dict[str, Any]] = []
        for item in evidence:
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                raise TimeReconciliationError(f"人工分类条目 {index} 证据绑定形状非法")
            candidate = Path(item["path"])
            target = candidate if candidate.is_absolute() else evidence_root / candidate
            if target.is_symlink() or not target.is_file():
                raise TimeReconciliationError(f"人工分类条目 {index} 证据缺失：{item['path']}")
            actual = _sha256(target.read_bytes())
            if actual != item["sha256"]:
                raise TimeReconciliationError(f"人工分类条目 {index} 证据摘要漂移：{item['path']}")
            bound.append({"path": str(target), "sha256": actual, "bytes": target.stat().st_size})
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            raise TimeReconciliationError(f"人工分类条目 {index} note 必须是字符串")
        normalized.append({"index": index, "start": start, "end": end, "category": category, "evidence": bound, "note": note})
    ordered = sorted(normalized, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] < previous["end"]:
            raise TimeReconciliationError(f"人工分类条目 {previous['index']} 与 {current['index']} 重叠")
    return ordered, {"path": str(resolved), "sha256": _sha256(raw), "entries": len(ordered)}


# ---------------------------------------------------------------------------
# 时间轴与分类
# ---------------------------------------------------------------------------


def _clip(span_start: datetime, span_end: datetime, since: datetime, until: datetime) -> tuple[datetime, datetime] | None:
    start = max(span_start, since)
    end = min(span_end, until)
    if end <= start:
        return None
    return start, end


def _build_timeline(
    *,
    since: datetime,
    until: datetime,
    points: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    stopped_spans: list[dict[str, Any]],
    manual: list[dict[str, Any]],
    idle_threshold: timedelta,
    creation_threshold: timedelta,
) -> list[dict[str, Any]]:
    boundaries = {since, until}
    for point in points:
        boundaries.add(point["time"])
    for span in spans:
        boundaries.add(span["start"])
        boundaries.add(span["end"])
    for entry in manual:
        boundaries.add(entry["start"])
        boundaries.add(entry["end"])
    ordered = sorted(boundaries)
    points_by_time: dict[datetime, list[dict[str, Any]]] = {}
    for point in points:
        points_by_time.setdefault(point["time"], []).append(point)
    span_starts: dict[datetime, list[dict[str, Any]]] = {}
    for span in spans:
        span_starts.setdefault(span["start"], []).append(span)
    intervals: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        mid = start + (end - start) / 2
        covering = sorted(
            (span for span in spans if span["start"] <= mid < span["end"]),
            key=lambda span: (span["priority"], span["start"], span["ref"]),
        )
        stopped = any(span["start"] <= mid < span["end"] for span in stopped_spans)
        length = end - start
        ending_points = points_by_time.get(end, [])
        ending_spans = span_starts.get(end, [])
        category = "unclassified"
        basis_kind = "none"
        refs: list[str] = []
        if covering:
            top = covering[0]
            category = top["category"]
            basis_kind = "evidence"
            refs = [f"{span['source']}:{span['ref']}" for span in covering]
        else:
            commits = [p for p in ending_points if p["kind"].startswith("commit_")]
            deploys = [p for p in ending_points if p["kind"] in {"deployment_receipt", "deployment_bootstrap"}]
            creations = [p for p in ending_points if p["kind"] == "campaign_created"]
            recoveries = [p for p in ending_points if p["kind"] in {"ledger_event:recovery_verified", "ledger_created"}]
            if commits:
                category = "idle" if length > idle_threshold else "tool_repair"
                refs = [f"git:{p['ref']}" for p in commits]
            elif deploys:
                # 提交到部署之间超过创建阈值的空档不是部署本身；长于空闲阈值记 idle，
                # 介于两者之间不猜，留给人工分类。
                if length <= creation_threshold:
                    category = "deployment"
                elif length > idle_threshold:
                    category = "idle"
                refs = [f"{p['source']}:{p['ref']}" for p in deploys]
            elif creations or ending_spans:
                if length <= creation_threshold:
                    category = "campaign_creation"
                elif length > idle_threshold:
                    category = "idle"
                refs = [f"campaign:{p['ref']}" for p in creations] + [f"{s['source']}:{s['ref']}" for s in ending_spans]
            elif stopped and recoveries:
                category = "stop_gap"
                refs = [f"{p['source']}:{p['ref']}" for p in recoveries]
            if category != "unclassified":
                basis_kind = "inferred"
            else:
                refs = []
        for entry in manual:
            if entry["start"] <= mid < entry["end"]:
                if basis_kind == "evidence":
                    raise TimeReconciliationError(
                        f"人工分类条目 {entry['index']} 覆盖了证据区间 {_iso(start)}～{_iso(end)}"
                    )
                category = entry["category"]
                basis_kind = "manual"
                refs = [f"manual:{entry['index']}"] + [f"file:{item['path']}" for item in entry["evidence"]]
                break
        intervals.append(
            {
                "start": start,
                "end": end,
                "category": category,
                "basis_kind": basis_kind,
                "refs": sorted(set(refs)),
                "ledger_stopped": stopped,
            }
        )
    return _merge_intervals(intervals)


def _merge_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for interval in intervals:
        if merged:
            last = merged[-1]
            if (
                last["end"] == interval["start"]
                and last["category"] == interval["category"]
                and last["basis_kind"] == interval["basis_kind"]
                and last["ledger_stopped"] == interval["ledger_stopped"]
            ):
                last["end"] = interval["end"]
                last["refs"] = sorted(set(last["refs"]) | set(interval["refs"]))
                continue
        merged.append(dict(interval))
    return merged


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def reconcile_upgrade_time(
    data_root: Path,
    *,
    since_utc: str,
    until_utc: str | None = None,
    git_log_file: Path | None = None,
    git_repo: Path | None = None,
    manual_classification: Path | None = None,
    evidence_root: Path | None = None,
    idle_threshold_minutes: int = DEFAULT_IDLE_THRESHOLD_MINUTES,
    campaign_creation_threshold_minutes: int = DEFAULT_CAMPAIGN_CREATION_THRESHOLD_MINUTES,
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    root = closeout._trusted_directory(data_root, "数据根")
    observed = observed_at_utc or _utc_now()
    since = _timestamp(since_utc, "since")
    until = _timestamp(until_utc, "until") if until_utc else _timestamp(observed, "observed_at_utc")
    if until <= since:
        raise TimeReconciliationError("until 必须晚于 since")
    for name, value in (("空闲阈值", idle_threshold_minutes), ("创建阈值", campaign_creation_threshold_minutes)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise TimeReconciliationError(f"{name}必须是正整数分钟")
    idle_threshold = timedelta(minutes=idle_threshold_minutes)
    creation_threshold = timedelta(minutes=campaign_creation_threshold_minutes)

    ledgers, ignored_ledgers = _load_ledgers(root)
    ledger_points, ledger_spans, stopped_spans = _ledger_points_and_spans(ledgers, until=until)
    runs = _load_supervisor_runs(root)
    deployments = _load_deployments(root)
    campaigns = _load_campaigns(root)
    audits = _load_audits(root, until=until)
    commits, git_source = _load_commits(git_log_file=git_log_file, git_repo=git_repo, since=since)
    manual, manual_source = _load_manual_classification(
        manual_classification, evidence_root=evidence_root or root, since=since, until=until
    )

    points: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for point in ledger_points:
        if since <= point["time"] <= until:
            points.append(point)
    for run in runs:
        if run["phase"] == "bootstrap":
            if since <= run["start"] <= until:
                points.append({"time": run["start"], "source": "supervisor", "kind": "deployment_bootstrap", "ref": run["run_id"], "campaign_id": run["campaign_id"]})
            continue
        clipped = _clip(run["start"], run["end"], since, until)
        if clipped is None:
            continue
        spans.append(
            {
                "start": clipped[0],
                "end": clipped[1],
                "source": "supervisor",
                "kind": "supervisor_run",
                "ref": run["run_id"],
                "phase": run["phase"],
                "campaign_id": run["campaign_id"],
                "category": _phase_category(run["phase"], fallback="vc_execution:supervisor"),
                "priority": PRIORITY_SUPERVISOR,
            }
        )
    for deployment in deployments:
        if since <= deployment["time"] <= until:
            points.append({"time": deployment["time"], "source": "deployment", "kind": "deployment_receipt", "ref": deployment["ref"], "status": deployment["status"]})
    for campaign in campaigns:
        if since <= campaign["time"] <= until:
            points.append({"time": campaign["time"], "source": "campaign", "kind": "campaign_created", "ref": campaign["ref"], "campaign_mode": campaign["campaign_mode"]})
    for audit in audits:
        clipped = _clip(audit["start"], audit["end"], since, until)
        if clipped is None:
            continue
        spans.append(
            {
                "start": clipped[0],
                "end": clipped[1],
                "source": "audit",
                "kind": "audit_directory",
                "ref": audit["ref"],
                "outcome": audit["outcome"],
                "category": audit["category"],
                "priority": PRIORITY_AUDIT,
            }
        )
    for span in ledger_spans:
        clipped = _clip(span["start"], span["end"], since, until)
        if clipped is None:
            continue
        spans.append({**span, "start": clipped[0], "end": clipped[1]})
    clipped_stops: list[dict[str, Any]] = []
    for span in stopped_spans:
        clipped = _clip(span["start"], span["end"], since, until)
        if clipped is not None:
            clipped_stops.append({**span, "start": clipped[0], "end": clipped[1]})
    for commit in commits:
        if since <= commit["time"] <= until:
            points.append({"time": commit["time"], "source": "git", "kind": commit["kind"], "ref": commit["ref"], "subject": commit["subject"]})
    points.sort(key=lambda p: (p["time"], p["source"], p["ref"]))
    spans.sort(key=lambda s: (s["start"], s["priority"], s["ref"]))

    intervals = _build_timeline(
        since=since,
        until=until,
        points=points,
        spans=spans,
        stopped_spans=clipped_stops,
        manual=manual,
        idle_threshold=idle_threshold,
        creation_threshold=creation_threshold,
    )
    totals: dict[str, timedelta] = {}
    basis_totals: dict[str, timedelta] = {kind: timedelta() for kind in BASIS_KINDS}
    for interval in intervals:
        length = interval["end"] - interval["start"]
        totals[interval["category"]] = totals.get(interval["category"], timedelta()) + length
        basis_totals[interval["basis_kind"]] += length
    wall_clock = until - since
    if sum(totals.values(), timedelta()) != wall_clock or intervals[0]["start"] != since or intervals[-1]["end"] != until:
        raise TimeReconciliationError("区间之和与窗口墙钟不一致")
    for previous, current in zip(intervals, intervals[1:]):
        if previous["end"] != current["start"]:
            raise TimeReconciliationError("区间不连续")
    unclassified = totals.get("unclassified", timedelta())
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at_utc": observed,
        "since_utc": _iso(since),
        "until_utc": _iso(until),
        "wall_clock_seconds": _seconds(wall_clock),
        "data_root": str(root),
        "thresholds": {
            "idle_minutes": idle_threshold_minutes,
            "campaign_creation_minutes": campaign_creation_threshold_minutes,
        },
        "sources": {
            "ledgers": [
                {"ledger_id": l["ledger_id"], "upgrade_id": l["upgrade_id"], "created_at_utc": _iso(l["created_at"]), "event_count": len(l["events"])}
                for l in ledgers
            ],
            "ignored_ledgers": ignored_ledgers,
            "supervisor_runs": len(runs),
            "deployments": len(deployments),
            "campaigns": len(campaigns),
            "audits": len(audits),
            "commits": len(commits),
            "git": git_source,
            "manual_classification": manual_source,
        },
        "events": [
            {**{k: v for k, v in point.items() if k != "time"}, "time_utc": _iso(point["time"])}
            for point in points
        ],
        "spans": [
            {
                **{k: v for k, v in span.items() if k not in {"start", "end", "priority"}},
                "start_utc": _iso(span["start"]),
                "end_utc": _iso(span["end"]),
                "seconds": _seconds(span["end"] - span["start"]),
            }
            for span in spans
        ],
        "stopped_spans": [
            {"ref": s["ref"], "start_utc": _iso(s["start"]), "end_utc": _iso(s["end"]), "seconds": _seconds(s["end"] - s["start"])}
            for s in clipped_stops
        ],
        "intervals": [
            {
                "start_utc": _iso(i["start"]),
                "end_utc": _iso(i["end"]),
                "seconds": _seconds(i["end"] - i["start"]),
                "category": i["category"],
                "basis": {"kind": i["basis_kind"], "refs": i["refs"]},
                "ledger_stopped": i["ledger_stopped"],
            }
            for i in intervals
        ],
        "totals_seconds": {category: _seconds(value) for category, value in sorted(totals.items())},
        "basis_seconds": {kind: _seconds(value) for kind, value in basis_totals.items()},
        "unclassified_seconds": _seconds(unclassified),
        "status": "complete" if unclassified == timedelta() else "unclassified_present",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从可信事件边界对账升级墙钟时间（只读）。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("reconcile-upgrade-time", help="生成时间对账收据")
    command.add_argument("--data-root", type=Path, required=True, help="宿主数据根（含 control、evidence、audit）")
    command.add_argument("--since", required=True, help="RFC3339 窗口起点")
    command.add_argument("--until", help="RFC3339 窗口终点，默认当前时间")
    command.add_argument("--git-log-file", type=Path, help="git log --format='%%H%%x09%%cI%%x09%%s' 的导出文件")
    command.add_argument("--git-repo", type=Path, help="现场读取提交时间的 git 仓库")
    command.add_argument("--manual-classification", type=Path, help="人工分类收据")
    command.add_argument("--evidence-root", type=Path, help="人工分类证据相对路径的根，默认数据根")
    command.add_argument("--idle-threshold-minutes", type=int, default=DEFAULT_IDLE_THRESHOLD_MINUTES)
    command.add_argument("--campaign-creation-threshold-minutes", type=int, default=DEFAULT_CAMPAIGN_CREATION_THRESHOLD_MINUTES)
    command.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        receipt = reconcile_upgrade_time(
            arguments.data_root,
            since_utc=arguments.since,
            until_utc=arguments.until,
            git_log_file=arguments.git_log_file,
            git_repo=arguments.git_repo,
            manual_classification=arguments.manual_classification,
            evidence_root=arguments.evidence_root,
            idle_threshold_minutes=arguments.idle_threshold_minutes,
            campaign_creation_threshold_minutes=arguments.campaign_creation_threshold_minutes,
        )
        provenance.write_receipt(receipt, arguments.output)
    except (TimeReconciliationError, closeout.VC0CloseoutError, provenance.ProvenanceError, OSError) as error:
        print(f"时间对账失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "wall_clock_seconds": receipt["wall_clock_seconds"],
                "totals_seconds": receipt["totals_seconds"],
                "unclassified_seconds": receipt["unclassified_seconds"],
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["status"] == "complete" else 3


if __name__ == "__main__":
    sys.exit(main())
