"""Mac 客户端离线任务冻结、包导入与项目账务接入。

本入口永不启动客户端或发送请求。正式来源必须已登记受管适配器；离线夹具仅归档，
不签发 seal 可消费的顶层成功收据。导入先归档来源，再按原 provenance/总账消费者
入账，最后发布成功收据；中断后同一输入继续，不重发客户端请求。
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as contract
from tools.official_client_capture.capturelib.model import ConfigurationError

PLAN_SCHEMA = "claude-upgrade-client-plan/v1"
RUN_SCHEMA = "claude-upgrade-client-imported-run/v1"


def _upgrade() -> Any:
    from tools.official_client_capture import claude_upgrade
    return claude_upgrade


def _mkdir(path: Path) -> None:
    """只建私有目录；既有任一父路径含符号链接即拒绝。"""
    for parent in (path, *path.parents):
        contract.require(not parent.is_symlink(), f"目录包含符号链接：{parent}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    contract.require(path.is_dir() and path.stat().st_mode & 0o077 == 0, f"目录权限不是私有：{path}")


def _write_bytes(path: Path, raw: bytes) -> None:
    """临时文件加独占链接原子发布，重复内容逐字复用。"""
    from tools.official_client_capture import claude_upgrade_capture
    _mkdir(path.parent)
    if path.exists() or path.is_symlink():
        contract.require(not path.is_symlink() and path.is_file() and path.read_bytes() == raw, f"只写一次文件发生冲突：{path}")
        return
    claude_upgrade_capture._exclusive_write_bytes(path, raw)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    _write_bytes(path, contract.canonical(value) + b"\n")


def _context(campaign_dir: Path, candidate_id: str, attempt_id: str) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    upgrade = _upgrade()
    manifest = upgrade._require_formal_campaign(campaign_dir)
    root, attempt = upgrade._load_capture_attempt(campaign_dir, "candidate", candidate_id, attempt_id, _verified_campaign_manifest=manifest)
    contract.require(attempt.get("status") == "awaiting_receipts", "客户端取证只用于等待收据的候选 attempt")
    contract.require(attempt.get("candidate_id") == candidate_id, "候选身份不符")
    return manifest, root, attempt


@contextmanager
def _client_control_lock(campaign_dir: Path) -> Any:
    """遵守项目锁在前、Campaign 锁在后的顺序；离线夹具无项目时仍保留 Campaign 串行性。"""
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger

    project = ledger.find_project_ledger(campaign_dir)
    with (ledger.project_lock(project) if project is not None else nullcontext()), _upgrade()._campaign_lock(campaign_dir):
        yield


def _require_unique_task_producer(campaign_dir: Path, task: Mapping[str, Any]) -> None:
    """同一 producer、捕获根与捕获 ID 不得重新分配给另一个任务。"""
    for path in campaign_dir.glob("candidates/*/attempts/*/client-evidence/*/tasks/*/task.json"):
        previous = contract.validate_task(contract.read_json(path))
        if previous["task_sha256"] == task["task_sha256"]:
            continue
        contract.require(all(previous["execution"][key] != task["execution"][key] for key in ("producer_run_id", "capture_id", "capture_root")), "不同客户端任务重复分配 producer 或独立捕获坐标")


def prepare(campaign_dir: Path, candidate_id: str, attempt_id: str, client_id: str, plan_path: Path) -> dict[str, Any]:
    """冻结任务的状态读取、身份复核与发布均在同一 Campaign 锁内完成。"""
    with _client_control_lock(campaign_dir):
        return _prepare_locked(campaign_dir, candidate_id, attempt_id, client_id, plan_path)


def _prepare_locked(campaign_dir: Path, candidate_id: str, attempt_id: str, client_id: str, plan_path: Path) -> dict[str, Any]:
    """只读核准当前候选冻结输入，再以审核计划的唯一 nonce 写任务；不猜安装版本。"""
    upgrade = _upgrade()
    manifest, attempt_root, attempt = _context(campaign_dir, candidate_id, attempt_id)
    plan = contract.read_json(Path(plan_path))
    fields = {"schema_version", "approved_by", "task_nonce", "issued_at_utc", "catalog_entry_id", "client_artifacts", "scenarios", "scenario_reuse", "execution"}
    contract.require(set(plan) == fields and plan["schema_version"] == PLAN_SCHEMA and bool(str(plan["approved_by"]).strip()), "必须给出已审核、完整的客户端计划")
    current = contract.candidate_context(attempt_root, plan["catalog_entry_id"])
    contract.require(plan["execution"].get("endpoint") == attempt.get("candidate_gateway", {}).get("base_url"), "计划 endpoint 不是该 attempt 的隔离 Candidate")
    task = {"schema_version": contract.TASK_SCHEMA, "client_id": client_id, "campaign_id": manifest["campaign_id"], "candidate_id": candidate_id, "attempt_id": attempt_id, "run_nonce": attempt["run_nonce"], "candidate_revision": current["candidate_revision"], "candidate": current["candidate"], "catalog": current["catalog"], "client_artifacts": plan["client_artifacts"], "scenarios": plan["scenarios"], "execution": plan["execution"], "issued_at_utc": plan["issued_at_utc"], "task_nonce": plan["task_nonce"]}
    task["scenario_reuse"] = plan["scenario_reuse"]
    task["task_sha256"] = contract.digest(task)
    contract.validate_task(task)
    _require_unique_task_producer(campaign_dir, task)
    if task["execution"]["source_scope"] == "formal":
        contract.require(task["execution"]["recorder_id"] in contract.SOURCE_ADAPTERS, "真实 Mac recorder/独立捕获来源尚未登记；不能预约正式客户端运行")
        from tools.official_client_capture import codex_upgrade_project_ledger as ledger
        contract.require(ledger.find_project_ledger(campaign_dir) == Path(task["execution"]["project_ledger"]), "任务项目总账不是本 Campaign 的总账")
        ledger.assert_campaign_admitted(campaign_dir, command="campaign-run", require=True)
    target = attempt_root / "client-evidence" / client_id / "tasks" / task["task_nonce"] / "task.json"
    _write(target, task)
    _write(target.parent / "approved-plan.json", plan)
    return {"status": "prepared", "task": str(target), "task_sha256": task["task_sha256"], "source_scope": task["execution"]["source_scope"], "live_request_count": 0}


def _account_run(campaign_dir: Path, run_root: Path, task: Mapping[str, Any], verified: Mapping[str, Any]) -> dict[str, Any]:
    """调用现有 Campaign provenance、请求去重、outbox 与项目账本消费者，不另建 Mac 计数口径。"""
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    from tools.official_client_capture import codex_upgrade_reconciler as reconciler
    upgrade = _upgrade()
    manifest = upgrade._require_formal_campaign(campaign_dir)
    with reconciler._client_scope(campaign_dir, None):
        project_root = reconciler._project_root(campaign_dir)
        contract.require(project_root == Path(task["execution"]["project_ledger"]), "导入账务不能换成任务之外的项目总账")
        operation = f"client-import:{task['execution']['producer_run_id']}"
        intent_path = run_root / "accounting-intent.json"
        if intent_path.exists() or intent_path.is_symlink():
            intent = contract.read_json(intent_path)
        else:
            plan, head = reconciler._project_facts(project_root)
            subject = f"client-{task['client_id']}-{task['task_nonce']}"
            receipt_dir = reconciler._reconciliation_dir(campaign_dir, subject)
            part, provenance, _path = reconciler._request_part(campaign_dir, manifest, receipt_dir, plan=plan, head=head, project_root=project_root, now=upgrade._utc_now(), phase="candidate")
            intent = {"operation_id": operation, "event_type": "reconciliation_committed", "payload": {"campaign_id": manifest["campaign_id"], "subject_kind": "client_capture", "subject_id": task["execution"]["producer_run_id"], "phase": "candidate", "candidate_id": task["candidate_id"], "request": part, "task_sha256": task["task_sha256"]}, "source": {"kind": contract.SOURCE_KIND, "sha256": provenance["sha256"]}, "receipt_bindings": [provenance]}
            # 先冻结意图；COMMIT 前后任一断点重入都用同一请求集合，不按新 head 重算旧操作。
            _write(intent_path, intent)
        contract.require(set(intent) == {"operation_id", "event_type", "payload", "source", "receipt_bindings"} and intent["operation_id"] == operation and intent["event_type"] == "reconciliation_committed" and intent["payload"].get("task_sha256") == task["task_sha256"] and intent["payload"].get("campaign_id") == task["campaign_id"], "导入账务意图与冻结任务不一致")
        for value in intent["receipt_bindings"]:
            original = contract.secure_file(campaign_dir, value["path"])
            contract.require(contract.binding(original, campaign_dir)["sha256"] == value["sha256"], "导入账务来源副本已漂移")
        batch = reconciler._commit_batch(campaign_dir, **intent)
        # 共享 write_batch 的 reused 只按 operation 查找；本入口进一步核对完整提交内容。
        persisted = ledger._read_batch(Path(batch["batch_dir"]))
        contract.require(persisted["committed"] and ledger._merge_batch_payload(persisted) == intent["payload"] and all(row["source"] == intent["source"] and row["receipt_bindings"] == intent["receipt_bindings"] for row in persisted["entries"]), "既有客户端账务批次与冻结意图冲突")
        ledger.reconcile_project_ledger(project_root, campaign_dir=campaign_dir)
        keys = sorted(row["identity_key"] for row in verified["requests"])
        result = {"schema_version": "claude-upgrade-client-accounting/v1", "status": "accounting_unresolved", "task_sha256": task["task_sha256"], "identity_keys_sha256": contract.digest(keys), "operation_id": operation, "batch_sha256": batch["batch_sha256"], "project_ledger": str(project_root)}
        if _accounting_resolved(result, task, keys):
            result["status"] = "accounted"
        # 未决记录不覆盖；显式 accounting_resolved 之后可补发独立的已决收据。
        _write(run_root / ("accounting.json" if result["status"] == "accounted" else "accounting-pending.json"), result)
        return result


def _accounting_resolved(accounting: Mapping[str, Any], task: Mapping[str, Any], keys: list[str]) -> bool:
    """收据绑定还须在真实项目事件链中存在，不能靠伪造 accounting.json 签成功。"""
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    fields = {"schema_version", "status", "task_sha256", "identity_keys_sha256", "operation_id", "batch_sha256", "project_ledger"}
    contract.require(set(accounting) == fields and accounting["schema_version"] == "claude-upgrade-client-accounting/v1" and accounting["task_sha256"] == task["task_sha256"] and accounting["identity_keys_sha256"] == contract.digest(keys) and accounting["operation_id"] == f"client-import:{task['execution']['producer_run_id']}", "客户端账务收据合同或原生请求键绑定不符")
    root = Path(str(accounting.get("project_ledger", "")))
    contract.require(root == Path(task["execution"]["project_ledger"]), "收据项目总账不等于任务冻结总账")
    with ledger.project_lock(root):
        plan, _raw = ledger._load_plan(root)
        ledger._require_plan_client(plan, "claude", "客户端收据账务")
        events = ledger._load_events(root)
        head = ledger.replay_head(root)
        matches = [event for event in events if event["operation_id"] == accounting.get("operation_id")]
        contract.require(len(matches) == 1, "项目账本没有唯一客户端入账事件")
        event = matches[0]
        payload = event["payload"]
        contract.require(event["event_type"] == "reconciliation_committed" and event.get("source_batch_sha256") == accounting.get("batch_sha256") and payload.get("campaign_id") == task["campaign_id"] and payload.get("task_sha256") == task["task_sha256"] and payload.get("subject_kind") == "client_capture" and payload.get("subject_id") == task["execution"]["producer_run_id"] and payload.get("candidate_id") == task["candidate_id"] and payload.get("phase") == "candidate", "项目入账事件与任务/收据绑定不符")
        overlays, _audit = ledger._correction_overlays(events)
        effective = overlays.get(event["operation_id"], payload)
        contract.require(effective.get("task_sha256") == task["task_sha256"] and effective.get("campaign_id") == task["campaign_id"], "客户端账务更正改变了任务身份")
        resolutions = [row["payload"]["request"] for row in events if row["event_type"] == "accounting_resolved" and row["payload"].get("resolved_operation_id") == event["operation_id"]]
        request = resolutions[-1] if resolutions else effective.get("request", {})
        if request.get("status") != "resolved" or accounting["operation_id"] in head.get("unresolved_operation_ids", []):
            return False
        accounted = set(head.get("accounted_identity_index", {})) | set(ledger._initial_keys(root, plan))
        contract.require(set(keys) <= accounted, "项目总账缺少客户端原生请求键")
        return True


def verify_accounting(accounting: Mapping[str, Any], task: Mapping[str, Any], keys: list[str]) -> None:
    """按追加式更正和账务解决后的有效事件重放，不信任文件中的自报状态。"""
    contract.require(_accounting_resolved(accounting, task, keys), "客户端账务尚未精确解决")


def verify_import_inputs(attempt_root: Path, client_id: str, task_path: Path, bundle_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    task = contract.validate_task(contract.read_json(task_path))
    contract.require(task["client_id"] == client_id, "任务客户端不符")
    contract.bound_file(attempt_root, contract.binding(task_path, attempt_root))
    contract.verify_task_attempt(attempt_root, task)
    verified = contract.verify_client_bundle(task, bundle_root, Path(task["execution"]["capture_root"]))
    if task["execution"]["source_scope"] == "formal":
        contract.verify_origin(task, bundle_root, Path(task["execution"]["capture_root"]))
    return task, verified


def import_bundle(campaign_dir: Path, attempt_root: Path, client_id: str, task_path: Path, bundle_root: Path) -> dict[str, Any]:
    """整包通过完整性核验后归档；失败运行照样入账，只有成功且账务已决才发布成功收据。"""
    upgrade = _upgrade()
    task, verified = verify_import_inputs(attempt_root, client_id, task_path, bundle_root)
    contract.require(task["campaign_id"] == contract.read_json(campaign_dir / "campaign.json")["campaign_id"], "任务 Campaign 不符")
    if task["execution"]["source_scope"] == "formal":
        _manifest, current_attempt, _attempt = _context(campaign_dir, task["candidate_id"], task["attempt_id"])
        contract.require(current_attempt == attempt_root, "正式导入 attempt 不是当前 Campaign 的受管目录")
    run_root = attempt_root / "client-evidence" / client_id / "runs" / task["task_nonce"]
    target_bundle = run_root / "source"
    with _client_control_lock(campaign_dir):
        _require_unique_task_producer(campaign_dir, task)
        if task["execution"]["source_scope"] == "formal":
            _context(campaign_dir, task["candidate_id"], task["attempt_id"])
        source = contract.read_json(bundle_root / "source-manifest.json")
        for value in source["files"]:
            original = contract.bound_file(bundle_root, value)
            _write_bytes(target_bundle / str(value["path"]), original.read_bytes())
        _write_bytes(target_bundle / "source-manifest.json", (bundle_root / "source-manifest.json").read_bytes())
        # 拷贝后再次验证；原件在拷贝期间变化不能发布半真半假的来源清单。
        _, verified = verify_import_inputs(attempt_root, client_id, task_path, target_bundle)
        run = {"schema_version": RUN_SCHEMA, "client_id": client_id, "task": contract.binding(task_path, attempt_root), "source_manifest": contract.binding(target_bundle / "source-manifest.json", attempt_root), "source_scope": task["execution"]["source_scope"], "producer_run_id": task["execution"]["producer_run_id"]}
        _write(run_root / "run.json", run)
        _write(run_root / "verdict.json", verified)
        if task["execution"]["source_scope"] == "regression_fixture":
            return {"status": "regression_fixture_imported", "run": str(run_root), "live_request_count": 0, "recorded_request_count": len(verified["requests"]), "formal_reference_authorized": False}
        accounting = _account_run(campaign_dir, run_root, task, verified)
        if verified["status"] != "verified" or accounting["status"] != "accounted":
            return {"status": "accounting_unresolved" if accounting["status"] != "accounted" else "client_run_failed", "run": str(run_root), "accounting": accounting, "live_request_count": 0}
        receipt = {"schema_version": contract.RECEIPT_SCHEMA, "client_id": client_id, **{k: task[k] for k in contract.COORDINATES}, "status": "success", "task": contract.binding(task_path, attempt_root), "bundle": contract.binding(target_bundle / "source-manifest.json", attempt_root), "verification": verified, "accounting_binding": contract.binding(run_root / "accounting.json", attempt_root), "issued_at_utc": task["issued_at_utc"], "conclusion_scope": "formal"}
        target = attempt_root / "client-evidence" / f"{client_id}.json"
        _write(target, receipt)
        contract.verify_client_receipt(attempt_root, client_id, target)
        return {"status": "imported", "receipt": str(target), "accounting": accounting, "live_request_count": 0}


def collect_campaign_runs(campaign_dir: Path) -> list[dict[str, Any]]:
    """请求总账发现入口：每个已归档来源都重验，含失败运行；未知或截断材料按未决关闭。"""
    results = []
    imported_tasks: set[str] = set()
    campaign_id = contract.read_json(Path(campaign_dir) / "campaign.json").get("campaign_id")
    for path in sorted(Path(campaign_dir).glob("candidates/*/attempts/*/client-evidence/*/runs/*/run.json")):
        attempt_root = path.parents[4]
        job_id = "mac-" + contract.digest(str(path.relative_to(campaign_dir)))[:32]
        record: dict[str, Any] = {"job_id": job_id, "phase": "candidate", "roots": [], "precise_count": 0, "estimated_count": 0, "auxiliary_count": 0, "requests": [], "auxiliary_requests": []}
        try:
            run = contract.read_json(contract.secure_file(attempt_root, path.relative_to(attempt_root).as_posix()))
            contract.require(run.get("schema_version") == RUN_SCHEMA, "导入运行清单 schema 非法")
            task_path = contract.bound_file(attempt_root, run["task"])
            bundle = contract.bound_file(attempt_root, run["source_manifest"]).parent
            task, verified = verify_import_inputs(attempt_root, str(run["client_id"]), task_path, bundle)
            _require_unique_task_producer(Path(campaign_dir), task)
            contract.require(task["campaign_id"] == campaign_id, "客户端来源不是本 Campaign")
            contract.require(run["source_scope"] == task["execution"]["source_scope"] and run["producer_run_id"] == task["execution"]["producer_run_id"], "运行清单身份不符")
            imported_tasks.add(task["task_sha256"])
            if run["source_scope"] == "regression_fixture":
                continue
            record.update(status=verified["accounting_status"], precise_count=len(verified["requests"]), auxiliary_count=len(verified["auxiliary_requests"]), requests=[{**r, "job_id": job_id} for r in verified["requests"]], auxiliary_requests=[{**r, "job_id": job_id} for r in verified["auxiliary_requests"]])
        except (ConfigurationError, ValueError, KeyError, OSError, TypeError) as error:
            record.update(status="unresolved", reason=str(error))
        results.append(record)
    # 已冻结任务对应的 server 预约/捕获存在，Mac 包还没有完整导入：不能静默当作零请求。
    # 来源原件没有闭合时只记账务未决；后继 recorder 的请求前预约仍须先过预算准入。
    for path in sorted(Path(campaign_dir).glob("candidates/*/attempts/*/client-evidence/*/tasks/*/task.json")):
        try:
            task = contract.validate_task(contract.read_json(path))
            if task["execution"]["source_scope"] == "regression_fixture" or task["task_sha256"] in imported_tasks:
                continue
            capture = Path(task["execution"]["capture_root"])
            if not capture.exists() and not capture.is_symlink():
                continue
            reason = "独立客户端捕获已预约或有原件，但完整来源包尚未导入"
        except (ConfigurationError, ValueError, OSError, KeyError, TypeError) as error:
            reason = str(error)
        results.append({"job_id": "mac-unimported-" + contract.digest(str(path.relative_to(campaign_dir)))[:24], "phase": "candidate", "roots": [], "status": "unresolved", "reason": reason, "precise_count": 0, "estimated_count": 0, "auxiliary_count": 0, "requests": [], "auxiliary_requests": []})
    return results


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("client_evidence_action", choices=("prepare", "verify", "import"), nargs="?")
    parser.add_argument("--client", choices=contract.CLIENTS)
    parser.add_argument("--client-plan", type=Path)
    parser.add_argument("--client-task", type=Path)
    parser.add_argument("--bundle", type=Path)


def command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    campaign = Path(arguments.campaign_dir)
    contract.require(arguments.client and arguments.candidate_id and arguments.attempt_id and arguments.client_evidence_action, "client-evidence 必须给出动作、客户端、候选与 attempt")
    if arguments.client_evidence_action == "prepare":
        contract.require(arguments.client_plan is not None, "prepare 需要 --client-plan")
        return prepare(campaign, arguments.candidate_id, arguments.attempt_id, arguments.client, arguments.client_plan), 0
    contract.require(arguments.client_task is not None and arguments.bundle is not None, "verify/import 需要 --client-task 与 --bundle")
    _manifest, attempt_root, _attempt = _context(campaign, arguments.candidate_id, arguments.attempt_id)
    if arguments.client_evidence_action == "verify":
        _task, result = verify_import_inputs(attempt_root, arguments.client, arguments.client_task, arguments.bundle)
        return {**result, "live_request_count": 0}, 0 if result["status"] == "verified" else 2
    result = import_bundle(campaign, attempt_root, arguments.client, arguments.client_task, arguments.bundle)
    return result, 0 if result["status"] in {"imported", "regression_fixture_imported"} else 2
