"""原 regression_fixture 后端的固定用途策略；不接受正式身份与正式预算。"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_h2_contract as h2
from tools.official_client_capture import claude_mac_capture_h2_runtime as runtime

SCOPE = c.SCOPE
timestamp = c.timestamp
validate_configuration = c.validate_configuration
contract_versions = c.contract_versions
verify_task_configuration = c.verify_task_configuration
runtime_bindings = c.runtime_bindings
prepare_protocol_snapshot = h2.prepare_snapshot
create_parser = h2.create_parser
verify_export = c.verify_export


def verify_protocol_runtime(configuration: Mapping[str, Any]) -> None:
    runtime.verify_binding(Path(configuration["data_root"]), configuration["runtime_environment_binding"])


def verify_budget(task: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    """保留旧准入语义，项目仍须是 fixture_only，不另建或释放预算。"""
    from tools.official_client_capture import claude_upgrade_mac_budget as budget
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    root = Path(task["execution"]["project_ledger"])
    plan, _raw = ledger._load_plan(root)
    c.require(plan.get("fixture_only") is True, "regression_fixture capture 只能消费 fixture_only 原项目占用")
    row = budget.verify_bound_receipt(root, receipt, task_sha256=task["task_sha256"], dispatch=False)
    c.require(row.get("status") == "reserved", "预算占用已结清或不再可用于此 capture")
    bound = row["bound"]
    for name in ("campaign_id", "candidate_id", "attempt_id", "run_nonce", "candidate_revision"):
        c.require(bound[name] == task[name], "预算占用与原任务坐标不符")
    for name in ("request_budget", "capture_id", "producer_run_id"):
        c.require(bound[name] == task["execution"][name], "预算占用与冻结请求边界不符")
    deadline = task["execution"]["request_policy"]["deadline_utc"]
    c.require(bound["deadline_utc"] == deadline and c.timestamp(c.utc_now()) < c.timestamp(deadline),
              "采集冻结 deadline 已到或预算窗口不符")


def native_extra(configuration: Mapping[str, Any]) -> dict[str, Any]:
    return {"formal_ready": False, "launch_allowed": False}


def verify_request(task: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    """夹具仍只使用原计数门禁，不扩大旧用途。"""
