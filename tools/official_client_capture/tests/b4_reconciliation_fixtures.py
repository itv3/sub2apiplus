"""B4-1 第 31 项测试夹具：为失败父 run 写一份形态合法的 supervisor-run 对账收据，并在祖先项目总账登记
``reconcile-supervisor-run:<run>`` operation。

字段集合是 ``verify_supervisor_run_reconciliation_binding`` 核对的那部分（与 ``reconciler.reconcile_supervisor_run``
的产出同形），不冒充完整收据；总账事件 payload 与 reconciler 的 ``reconciliation_committed`` 同形。监督器测试与
tool_evolution 的候选续跑协议测试共用，避免两份夹具漂移。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests import project_ledger_fixture


def write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """0700 目录、0600 文件，与监督器写盘权限一致。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def bind_supervisor_run_reconciliation(
    campaign_dir: Path,
    run_dir: Path,
    prior_state: Mapping[str, Any],
    prior_manifest: Mapping[str, Any],
    *,
    failure_class: str = "execution-failure",
) -> Path:
    """写最小合法的 supervisor-run 对账收据并登记总账 operation；Campaign 目录与祖先总账不存在时一并创建。"""

    campaign_dir = Path(campaign_dir).resolve()
    campaign_path = campaign_dir / "campaign.json"
    if not campaign_path.is_file():
        write_private_json(campaign_path, {"campaign_id": str(prior_manifest["campaign_id"])})
    ledger_root = supervisor.project_ledger.find_project_ledger(campaign_dir)
    if ledger_root is None:
        ledger_root = project_ledger_fixture.install_fixture_ledger(campaign_dir.parent)
    receipt = {
        "schema_version": supervisor.SUPERVISOR_RUN_RECONCILIATION_SCHEMA,
        "campaign_id": str(prior_manifest["campaign_id"]),
        "campaign_manifest_sha256": supervisor._sha256(campaign_path.read_bytes()),
        "run": {
            "run_dir": str(run_dir.resolve()),
            "run_id": run_dir.name,
            "state": prior_state["state"],
            "phase": prior_manifest["phase"],
            "batch_id": prior_manifest["batch_id"],
            "batch_sequence": prior_manifest["batch_sequence"],
            "batch_sha256": prior_manifest["batch_sha256"],
            "execute_items": list(prior_manifest["execute_items"]),
            "reuse_items": list(prior_manifest["reuse_items"]),
            "failure_class": failure_class,
        },
        "failure_class": failure_class,
        "root_cause": {"root_cause_id": "b4-fixture-01"},
        "reservation_exists": False,
        "live_request_count": 0,
        "scanned_bytes": 0,
    }
    receipt_path = campaign_dir / "control" / "reconciliation" / f"run-{run_dir.name}" / "supervisor-run-reconciliation.json"
    write_private_json(receipt_path, receipt)
    supervisor.project_ledger.append_project_event(
        ledger_root,
        operation_id=f"reconcile-supervisor-run:{run_dir.name}",
        event_type="reconciliation_committed",
        payload={
            "campaign_id": str(prior_manifest["campaign_id"]),
            "subject_kind": "supervisor_run",
            "subject_id": run_dir.name,
            "phase": prior_manifest["phase"],
            "request": {"status": "resolved", "identity_keys": [], "estimated_delta": 0, "estimated_sources": []},
            "reconciliation_receipt_sha256": supervisor._sha256(receipt_path.read_bytes()),
        },
        source_batch_sha256=None,
    )
    return receipt_path
