"""历史 Campaign 处置清单（只读）：为一个目标版本的全部 Campaign 给出处置与依据。

方案 A0a-7。输出 ``historical-campaign-disposition/v1``，供 A0b-3 执行、A2.6 的策略
兼容收据引用「34 个 Campaign 按处置清单的归属」。处置只有四种：

* ``reuse_primary``：A3b 复用导入的主来源（方案固定为 fresh）；
* ``reuse_backup``：备用来源（方案固定为 new-window），只在主来源不合格时启用；
* ``read_only_archive``：其余 formal Campaign，内容只读保留，不再执行；
* ``preflight_archive``：preflight_only Campaign，只读保留。

复用来源的合格条件在这里只做判定不做裁决：最新 official attempt 为
``awaiting_receipts`` 且全部 Job ``complete``；若提供项目请求审计收据，则该 Campaign
账务必须 ``complete``；若提供 attempt 审计收据，则必须 ``passed``。不合格时处置不变，
``eligible`` 为假并列出原因，由 A3b 前置检查决定是否切换备用来源。

账本与 Campaign 的对应关系取自账本 ``receipts/vc0-closeout/<formal_campaign_id>`` 目录，
这是 VC-0 收口写入的不可变绑定；未停线的账本列入 ``ledgers_to_close`` 供 A0b-4 关闭。
本模块不写任何证据，只写一份不可覆盖的收据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_live_request_provenance as provenance
from tools.official_client_capture import codex_upgrade_official_attempt_audit as attempt_audit
from tools.official_client_capture import codex_upgrade_time_reconciliation as reconciliation
from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger
from tools.official_client_capture import codex_upgrade_vc0_closeout as closeout

SCHEMA_VERSION = "historical-campaign-disposition/v1"
DISPOSITIONS = ("reuse_primary", "reuse_backup", "read_only_archive", "preflight_archive")
DEFAULT_TARGET_VERSION = "0.154.0"


class DispositionError(ValueError):
    """输入收据不可信或来源指定不合法。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load_receipt(path: Path, *, schema: str, label: str) -> tuple[dict[str, Any], str]:
    resolved = closeout._trusted_file(path, label)
    payload, raw = closeout._load_json(resolved, label)
    if payload.get("schema_version") != schema:
        raise DispositionError(f"{label} schema 不是 {schema}")
    return payload, _sha256(raw)


def _ledger_summaries(data_root: Path) -> list[dict[str, Any]]:
    """读取全部升级计时账本，返回状态与它们绑定的 formal Campaign。"""

    summaries: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for root in reconciliation._ledger_roots(data_root):
        plan, _raw = timing_ledger._load_json(root / "ledger.json", "账本计划")
        if plan.get("schema_version") != timing_ledger.PLAN_SCHEMA:
            continue
        ledger_id = str(root.relative_to(data_root))
        entry: dict[str, Any] = {"ledger_id": ledger_id, "upgrade_id": plan.get("upgrade_id")}
        try:
            events = timing_ledger._load_events(root)
            summary = timing_ledger._summarize(root, plan, events, as_of=now)
            entry.update(
                {
                    "status": summary["status"],
                    "active_phase": summary["active_phase"],
                    "head_sequence": summary["head_sequence"],
                    "head_sha256": summary["head_sha256"],
                    "total_live_request_count": summary["total_live_request_count"],
                }
            )
        except timing_ledger.TimingLedgerError as error:
            entry.update({"status": "unreadable", "error": str(error)})
        bound: list[str] = []
        closeout_root = root / "receipts" / "vc0-closeout"
        if closeout_root.is_dir() and not closeout_root.is_symlink():
            bound = sorted(
                child.name for child in closeout_root.iterdir() if child.is_dir() and not child.is_symlink()
            )
        entry["formal_campaign_ids"] = bound
        summaries.append(entry)
    return summaries


def _attempts(campaign_dir: Path) -> list[dict[str, Any]]:
    attempts_root = campaign_dir / "official" / "attempts"
    attempts: list[dict[str, Any]] = []
    if not attempts_root.is_dir() or attempts_root.is_symlink():
        return attempts
    for child in sorted(attempts_root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        path = child / "attempt.json"
        if path.is_symlink() or not path.is_file():
            continue
        payload, raw = closeout._load_json(closeout._trusted_file(path, "attempt"), "attempt")
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status")) if isinstance(result, Mapping) else "invalid"
            counts[status] = counts.get(status, 0) + 1
        attempts.append(
            {
                "attempt_id": child.name,
                "status": payload.get("status"),
                "attempt_sha256": _sha256(raw),
                "result_count": len(results),
                "result_status_counts": dict(sorted(counts.items())),
                "started_at_utc": payload.get("started_at_utc"),
                "completed_at_utc": payload.get("completed_at_utc"),
            }
        )
    return attempts


def _reuse_eligibility(
    attempts: list[dict[str, Any]],
    audit_entry: Mapping[str, Any] | None,
    attempt_audit_entry: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not attempts:
        reasons.append("没有 official attempt")
    else:
        latest = attempts[-1]
        if latest["status"] != "awaiting_receipts":
            reasons.append(f"最新 attempt 状态是 {latest['status']}，不是 awaiting_receipts")
        counts = latest["result_status_counts"]
        if latest["result_count"] == 0 or set(counts) != {"complete"}:
            reasons.append(f"Job 结果不是全部 complete：{counts}")
    if audit_entry is not None and audit_entry.get("status") != "complete":
        reasons.append(f"项目请求审计状态是 {audit_entry.get('status')}")
    if attempt_audit_entry is not None and attempt_audit_entry.get("status") != "passed":
        reasons.append(
            f"attempt 审计未通过：{attempt_audit_entry.get('failed_sections')}"
        )
    return not reasons, reasons


def build_disposition(
    data_root: Path,
    *,
    target_version: str = DEFAULT_TARGET_VERSION,
    primary_reuse_source: str,
    backup_reuse_source: str | None = None,
    project_audit: Path | None = None,
    attempt_audits: list[Path] | tuple[Path, ...] = (),
    observed_at_utc: str | None = None,
) -> dict[str, Any]:
    root = closeout._trusted_directory(data_root, "宿主数据根")
    campaigns_root = root / "evidence" / "campaigns"
    if not campaigns_root.is_dir() or campaigns_root.is_symlink():
        raise DispositionError("宿主数据根缺少 evidence/campaigns")
    for value, label in ((primary_reuse_source, "主来源"), (backup_reuse_source, "备用来源")):
        if value is not None and not closeout.SAFE_ID_RE.fullmatch(value):
            raise DispositionError(f"{label} Campaign ID 非法")
    if backup_reuse_source is not None and backup_reuse_source == primary_reuse_source:
        raise DispositionError("备用来源不得与主来源相同")

    audit_by_campaign: dict[str, dict[str, Any]] = {}
    audit_binding: dict[str, Any] | None = None
    if project_audit is not None:
        payload, digest = _load_receipt(
            project_audit, schema=provenance.PROJECT_SCHEMA_VERSION, label="项目请求审计收据"
        )
        for entry in payload.get("campaigns", []):
            if isinstance(entry, Mapping):
                audit_by_campaign[str(entry.get("campaign_id"))] = dict(entry)
        audit_binding = {"path": str(project_audit), "sha256": digest, "status": payload.get("status")}
    attempt_audit_by_campaign: dict[str, dict[str, Any]] = {}
    attempt_audit_bindings: list[dict[str, Any]] = []
    for path in attempt_audits:
        payload, digest = _load_receipt(
            path, schema=attempt_audit.SCHEMA_VERSION, label="attempt 审计收据"
        )
        campaign_id = str(payload.get("campaign_id"))
        if campaign_id in attempt_audit_by_campaign:
            raise DispositionError(f"Campaign {campaign_id} 提供了多份 attempt 审计收据")
        entry = {
            "path": str(path),
            "sha256": digest,
            "campaign_id": campaign_id,
            "attempt_id": payload.get("attempt_id"),
            "status": payload.get("status"),
            "failed_sections": payload.get("failed_sections"),
        }
        attempt_audit_by_campaign[campaign_id] = entry
        attempt_audit_bindings.append(entry)

    ledgers = _ledger_summaries(root)
    ledgers_by_campaign: dict[str, list[dict[str, Any]]] = {}
    for ledger in ledgers:
        for campaign_id in ledger["formal_campaign_ids"]:
            ledgers_by_campaign.setdefault(campaign_id, []).append(
                {k: v for k, v in ledger.items() if k != "formal_campaign_ids"}
            )

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for campaign_dir in sorted(p for p in campaigns_root.iterdir() if p.is_dir() and not p.is_symlink()):
        manifest_path = campaign_dir / "campaign.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        manifest, raw = closeout._load_json(closeout._trusted_file(manifest_path, "Campaign 清单"), "Campaign 清单")
        if manifest.get("target_version") != target_version:
            continue
        campaign_id = str(manifest.get("campaign_id"))
        if campaign_id != campaign_dir.name:
            raise DispositionError(f"Campaign 目录 {campaign_dir.name} 与清单 campaign_id 不一致")
        seen_ids.add(campaign_id)
        mode = manifest.get("campaign_mode")
        attempts = _attempts(campaign_dir)
        audit_entry = audit_by_campaign.get(campaign_id)
        attempt_audit_entry = attempt_audit_by_campaign.get(campaign_id)
        if mode == "preflight_only":
            disposition, eligible, reasons = "preflight_archive", True, ["campaign_mode 为 preflight_only"]
        elif mode != "formal":
            raise DispositionError(f"Campaign {campaign_id} 的 campaign_mode 非法：{mode}")
        elif campaign_id == primary_reuse_source:
            disposition = "reuse_primary"
            eligible, reasons = _reuse_eligibility(attempts, audit_entry, attempt_audit_entry)
        elif campaign_id == backup_reuse_source:
            disposition = "reuse_backup"
            eligible, reasons = _reuse_eligibility(attempts, audit_entry, attempt_audit_entry)
        else:
            disposition, eligible = "read_only_archive", True
            if not attempts:
                reasons = ["formal Campaign 没有 official attempt"]
            else:
                reasons = [
                    f"最新 attempt {attempts[-1]['attempt_id']} 状态 {attempts[-1]['status']}，"
                    f"Job 结果 {attempts[-1]['result_status_counts']}；未被选为复用来源"
                ]
        entries.append(
            {
                "campaign_id": campaign_id,
                "campaign_mode": mode,
                "campaign_purpose": manifest.get("campaign_purpose"),
                "created_at_utc": manifest.get("created_at_utc"),
                "manifest_sha256": _sha256(raw),
                "tool_files_sha256": (manifest.get("tool_identity") or {}).get("files_sha256")
                if isinstance(manifest.get("tool_identity"), Mapping)
                else None,
                "attempts": attempts,
                "ledgers": ledgers_by_campaign.get(campaign_id, []),
                "live_request_audit": (
                    {
                        "status": audit_entry.get("status"),
                        "precise_count_after_dedup": audit_entry.get("precise_count_after_dedup"),
                        "estimated_count": audit_entry.get("estimated_count"),
                        "unresolved_job_ids": audit_entry.get("unresolved_job_ids"),
                    }
                    if audit_entry is not None
                    else None
                ),
                "attempt_audit": (
                    {k: attempt_audit_entry[k] for k in ("attempt_id", "status", "failed_sections", "sha256")}
                    if attempt_audit_entry is not None
                    else None
                ),
                "disposition": disposition,
                "eligible": eligible,
                "reasons": reasons,
            }
        )
    if primary_reuse_source not in seen_ids:
        raise DispositionError(f"主来源 {primary_reuse_source} 不在目标版本 Campaign 中")
    if backup_reuse_source is not None and backup_reuse_source not in seen_ids:
        raise DispositionError(f"备用来源 {backup_reuse_source} 不在目标版本 Campaign 中")
    counts: dict[str, int] = {name: 0 for name in DISPOSITIONS}
    for entry in entries:
        counts[entry["disposition"]] += 1
    ledgers_to_close = [
        {k: v for k, v in ledger.items() if k != "formal_campaign_ids"}
        for ledger in ledgers
        if ledger.get("status") not in {"stopped", "complete"}
    ]
    primary_entry = next(e for e in entries if e["campaign_id"] == primary_reuse_source)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at_utc": observed_at_utc or _utc_now(),
        "data_root": str(root),
        "target_version": target_version,
        "primary_reuse_source": primary_reuse_source,
        "backup_reuse_source": backup_reuse_source,
        "primary_reuse_eligible": primary_entry["eligible"],
        "inputs": {
            "project_live_request_audit": audit_binding,
            "attempt_audits": attempt_audit_bindings,
        },
        "campaigns": entries,
        "ledgers": ledgers,
        "ledgers_to_close": ledgers_to_close,
        "summary": {
            "campaign_count": len(entries),
            "dispositions": counts,
            "ledger_count": len(ledgers),
            "ledgers_to_close": len(ledgers_to_close),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成历史 Campaign 处置清单（只读）。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("build-campaign-disposition", help="生成处置清单收据")
    command.add_argument("--data-root", type=Path, required=True)
    command.add_argument("--target-version", default=DEFAULT_TARGET_VERSION)
    command.add_argument("--primary-reuse-source", required=True)
    command.add_argument("--backup-reuse-source")
    command.add_argument("--project-audit", type=Path, help="project-live-request-audit/v2 收据")
    command.add_argument("--attempt-audit", type=Path, action="append", default=[], help="official-attempt-audit/v1 收据，可重复")
    command.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        receipt = build_disposition(
            arguments.data_root,
            target_version=arguments.target_version,
            primary_reuse_source=arguments.primary_reuse_source,
            backup_reuse_source=arguments.backup_reuse_source,
            project_audit=arguments.project_audit,
            attempt_audits=arguments.attempt_audit,
        )
        provenance.write_receipt(receipt, arguments.output)
    except (DispositionError, closeout.VC0CloseoutError, timing_ledger.TimingLedgerError, provenance.ProvenanceError, OSError) as error:
        print(f"处置清单生成失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "summary": receipt["summary"],
                "primary_reuse_eligible": receipt["primary_reuse_eligible"],
                "output": str(arguments.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if receipt["primary_reuse_eligible"] else 3


if __name__ == "__main__":
    sys.exit(main())
