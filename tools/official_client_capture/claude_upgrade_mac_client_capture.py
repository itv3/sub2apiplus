"""Mac client-run 的受监督接线、请求前索引与未导入运行发现。

本模块复用候选 service 的准入、预算 reservation 和 lease；不另造预算或产品命令。
正式来源未登记时入口关闭。live 原件只保存在原 attempt 的 client-evidence，恢复时
按原 producer 读同一索引，绝不从整个历史库猜来源，也不自动重发未知执行。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source

INDEX_SCHEMA = "claude-upgrade-client-reservation-index/v2"
INDEX_FIELDS = frozenset({"schema_version", "client_id", *c.COORDINATES, "task", "task_sha256", "capture_root", "capture_id", "producer_run_id", "reservation", "source_deployments"})


class CandidateClientServices(Protocol):
    """由既有候选监督 service 提供；不能来自计划中的动态模块或任意命令。"""

    def admit(self, task: Mapping[str, Any], attempt_root: Path) -> None: ...
    def reserve(self, task: Mapping[str, Any], attempt_root: Path) -> None: ...
    def wait_capture_ready(self, task: Mapping[str, Any]) -> None: ...
    def recorder(self, task: Mapping[str, Any], bundle_root: Path) -> Any: ...
    def close_capture(self, task: Mapping[str, Any]) -> None: ...


def _adapter(task: Mapping[str, Any]) -> Any:
    adapter = c.SOURCE_ADAPTERS.get(task["execution"]["recorder_id"])
    c.require(adapter is not None, "真实来源适配器尚未登记；client-run 保持关闭")
    return adapter


def _capture_terminal_exists(task: Mapping[str, Any]) -> bool:
    root = Path(task["execution"]["capture_root"])
    return any(path.exists() or path.is_symlink() for path in
               (root / "capture-manifest.json", root / "undispatched.json", root / "native-capture-manifest.json"))


def register_reservation(campaign_dir: Path, attempt_root: Path, task_path: Path, reservation_binding: Mapping[str, Any]) -> dict[str, Any]:
    """调用方处于项目锁→Campaign 锁内，独立预约完成后、任何产品启动前登记。"""
    from tools.official_client_capture import claude_upgrade_client_receipts as importer
    campaign_dir, attempt_root = Path(campaign_dir), Path(attempt_root)
    task_path = c.bound_file(attempt_root, c.binding(Path(task_path), attempt_root))
    task = c.validate_task(c.read_json(task_path))
    c.verify_task_attempt(attempt_root, task)
    c.require(attempt_root == campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"] and c.read_json(campaign_dir / "campaign.json")["campaign_id"] == task["campaign_id"], "预约索引必须归属原受管 attempt")
    importer._require_unique_task_producer(campaign_dir, task)
    capture_root = Path(task["execution"]["capture_root"])
    c.require(c.bound_file(capture_root, reservation_binding) == capture_root / "reservation.json", "索引未绑定准确独立预约")
    # 请求前索引必须先于 ready 发布；完整成功消费仍调用预约＋ready 验证器。
    source.verify_reservation_body_v2(task, capture_root)
    adapter = _adapter(task)
    c.require(callable(getattr(adapter, "verify_capture_origin", None)), "受管捕获来源验证未实现")
    adapter.verify_capture_origin(task, capture_root)
    index = {"schema_version": INDEX_SCHEMA, "client_id": task["client_id"], **{key: task[key] for key in c.COORDINATES}, "task": c.binding(task_path, attempt_root), "task_sha256": task["task_sha256"], "capture_root": str(capture_root), "capture_id": task["execution"]["capture_id"], "producer_run_id": task["execution"]["producer_run_id"], "reservation": dict(reservation_binding), "source_deployments": task["execution"]["source_deployments"]}
    path = attempt_root / "client-evidence" / "reservations" / f"{index['producer_run_id']}.json"
    importer._write(path, index)
    return {"path": str(path), "binding": c.binding(path, attempt_root), "index": index}


def validate_reservation_index(attempt_root: Path, path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    index = c.read_json(c.secure_file(attempt_root, path.relative_to(attempt_root).as_posix()))
    c.require(set(index) == INDEX_FIELDS and index["schema_version"] == INDEX_SCHEMA, "预约索引 schema 或闭集非法")
    task = c.validate_task(c.read_json(c.bound_file(attempt_root, index["task"])))
    c.verify_task_attempt(attempt_root, task)
    c.require(all(index[key] == task[key] for key in (*c.COORDINATES, "client_id", "task_sha256")), "预约索引与冻结任务身份不符")
    c.require(all(index[key] == task["execution"][key] for key in ("capture_root", "capture_id", "producer_run_id", "source_deployments")), "预约索引与独立捕获坐标不符")
    c.require(path == attempt_root / "client-evidence" / "reservations" / f"{index['producer_run_id']}.json", "预约索引位置与原 producer 不符")
    return index, task


def require_registered_reservation(attempt_root: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    path = attempt_root / "client-evidence" / "reservations" / f"{task['execution']['producer_run_id']}.json"
    index, actual = validate_reservation_index(attempt_root, path)
    c.require(actual == dict(task), "正式导入的 task 与请求前登记不符")
    root = Path(index["capture_root"])
    c.require(c.bound_file(root, index["reservation"]) == root / "reservation.json", "预约索引绑定已漂移")
    source.verify_reservation_v2(task, root)
    adapter = _adapter(task)
    c.require(callable(getattr(adapter, "verify_capture_origin", None)), "独立捕获未完成受管来源校验")
    adapter.verify_capture_origin(task, root)
    return index


def _empty_record(job_id: str, reason: str) -> dict[str, Any]:
    return {"job_id": job_id, "phase": "candidate", "roots": [], "status": "unresolved", "reason": reason, "precise_count": 0, "estimated_count": 0, "auxiliary_count": 0, "requests": [], "auxiliary_requests": []}


def _trusted_zero(task: Mapping[str, Any], capture_root: Path) -> bool:
    """只有独立捕获适配器验证未派发及零请求原件才可零收口。"""
    adapter = _adapter(task)
    verify = getattr(adapter, "verify_undispatched", None)
    c.require(callable(verify), "缺可信未派发证明")
    proof = c.read_json(c.secure_file(capture_root, "undispatched.json"))
    fields = {"schema_version", "task_sha256", "producer_run_id", "capture_id", "status", "request_count", "requests", "attestation"}
    c.require(set(proof) == fields and proof["schema_version"] == "claude-upgrade-client-undispatched/v2" and proof["task_sha256"] == task["task_sha256"] and proof["producer_run_id"] == task["execution"]["producer_run_id"] and proof["capture_id"] == task["execution"]["capture_id"] and proof["status"] == "undispatched" and type(proof["request_count"]) is int and proof["request_count"] == 0, "未派发证明身份或零值不符")
    c.bound_file(capture_root, proof["attestation"])
    c.require(not source._json_lines(c.bound_file(capture_root, proof["requests"]), "未派发零请求"), "零请求证明与已发原件冲突")
    manifest = capture_root / "capture-manifest.json"
    if manifest.exists() or manifest.is_symlink():
        current = c.read_json(manifest)
        c.require(current.get("schema_version") == source.CAPTURE_SCHEMA and current.get("task_sha256") == task["task_sha256"] and current.get("requests") == proof["requests"], "未派发证明与独立捕获清单冲突")
        c.bound_file(capture_root, current["requests"])
    verify(task, capture_root)
    return True


def discover_reservations(campaign_dir: Path, *, imported_task_shas: set[str] | None = None) -> list[dict[str, Any]]:
    """独立索引先于本地 run/task glob 发现；缺目录、通道中断和未导入不能消失。"""
    campaign_dir = Path(campaign_dir)
    imported = imported_task_shas or set()
    results, indexed = [], set()
    for path in sorted(campaign_dir.glob("candidates/*/attempts/*/client-evidence/reservations/*.json")):
        attempt_root = path.parents[2]
        record = _empty_record("mac-reserved-" + c.digest(path.relative_to(campaign_dir).as_posix())[:24], "独立捕获已预约，完整 Mac 来源尚未导入")
        try:
            index, task = validate_reservation_index(attempt_root, path)
            c.require(task["campaign_id"] == c.read_json(campaign_dir / "campaign.json")["campaign_id"], "预约不属于本 Campaign")
            indexed.add(task["task_sha256"])
            if task["task_sha256"] in imported:
                continue
            capture_root = Path(index["capture_root"])
            c.bound_file(capture_root, index["reservation"])
            source.verify_reservation_v2(task, capture_root)
            # 本地 intent 不存在不足以证明产品未启动；权威捕获方的未派发证明必需。
            intent = attempt_root / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"] / "dispatch-intent.json"
            if not intent.exists() and not intent.is_symlink() and (capture_root / "undispatched.json").is_file() and _trusted_zero(task, capture_root):
                record.update(status="resolved", reason="受管独立原件证明未派发且零请求")
            else:
                observed = source.collect_independent_requests_v2(task, capture_root)
                record.update(precise_count=len(observed["requests"]), auxiliary_count=len(observed["auxiliary_requests"]), requests=[{**row, "job_id": record["job_id"]} for row in observed["requests"]], auxiliary_requests=[{**row, "job_id": record["job_id"]} for row in observed["auxiliary_requests"]])
        except (ValueError, KeyError, TypeError, OSError) as error:
            record["reason"] = str(error)
        results.append(record)
    for path in sorted(campaign_dir.glob("candidates/*/attempts/*/client-evidence/*/tasks/*/task.json")):
        try:
            raw = c.read_json(path)
            # 旧 task 仅供只读账务发现；缺本地原件仍为未决，不取得当前正式权。
            task = c._validate_task_v1(raw) if raw.get("schema_version") == c.HISTORICAL_TASK_SCHEMA else c.validate_task(raw)
            if task["execution"]["source_scope"] == "regression_fixture" or task["task_sha256"] in imported | indexed:
                continue
            reason = "任务缺请求前预约索引；无本地 capture 目录也不能推断零请求"
        except (ValueError, KeyError, TypeError, OSError) as error:
            reason = str(error)
        results.append(_empty_record("mac-unimported-" + c.digest(path.relative_to(campaign_dir).as_posix())[:24], reason))
    return results


def run_client(task_path: Path, attempt_root: Path, *, services: CandidateClientServices) -> dict[str, Any]:
    """由既有受监督入口调用；同一 producer 的未知执行只重放／对账，不能重启产品。"""
    from tools.official_client_capture import claude_upgrade_client_receipts as importer
    from tools.official_client_capture import claude_mac_recorder
    from tools.official_client_capture import claude_upgrade_mac_budget as budget
    task_path, attempt_root = Path(task_path), Path(attempt_root)
    task = c.validate_task(c.read_json(c.bound_file(attempt_root, c.binding(task_path, attempt_root))))
    c.require(task["execution"]["source_scope"] == "formal", "正式 client-run 不接受离线夹具用途")
    adapter = _adapter(task)
    c.require(callable(getattr(adapter, "owns_services", None)) and adapter.owns_services(services), "监督 service 不是受管来源登记的服务实例")
    campaign_dir = next(parent for parent in attempt_root.parents if (parent / "campaign.json").is_file())
    run_root = attempt_root / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"]
    bundle_root = run_root / "source"
    already_imported = False
    reservation_failure = None
    with importer._client_control_lock(campaign_dir):
        c.verify_task_attempt(attempt_root, task)
        if (run_root / "run.json").exists() or (run_root / "run.json").is_symlink():
            already_imported = True
        elif (run_root / "dispatch-intent.json").exists() or (run_root / "dispatch-intent.json").is_symlink():
            return {"status": "accounting_unresolved", "reason": "原 producer 已进入派发窗口，先同步来源／恢复／导入，不能重新启动产品", "producer_run_id": task["execution"]["producer_run_id"]}
        else:
            receipt = budget.read_task_bound_receipt(Path(task["execution"]["project_ledger"]), task)
            if receipt is None:
                local = run_root / "budget-reservation.json"
                c.require(not local.exists() and not local.is_symlink(), "本地预算原件缺真实占用事件，禁止降级")
            row = budget.verify_bound_receipt(Path(task["execution"]["project_ledger"]), receipt,
                                             task_sha256=task["task_sha256"]) if receipt is not None else None
            if _capture_terminal_exists(task) or (row is not None and row["status"] == "settled"):
                return {"status": "accounting_unresolved", "reason": "原预算或捕获已有终态但缺完整导入记录，只收账不得重新启动产品", "producer_run_id": task["execution"]["producer_run_id"]}
            services.admit(task, attempt_root)
            capture_root = Path(task["execution"]["capture_root"])
            try:
                services.reserve(task, attempt_root)
                # 预约已经存在即登记；登记或 ready 失败都必须收口原捕获方。
                register_reservation(campaign_dir, attempt_root, task_path, c.binding(capture_root / "reservation.json", capture_root))
                services.wait_capture_ready(task)
                # intent 写入失败也处于预约收口范围；不能留下无人处理的原 hold。
                importer._write(run_root / "dispatch-intent.json", {"schema_version": "claude-upgrade-client-dispatch-intent/v2", "task_sha256": task["task_sha256"], "producer_run_id": task["execution"]["producer_run_id"], "capture_id": task["execution"]["capture_id"], "source_deployments": task["execution"]["source_deployments"]})
            except BaseException as reservation_error:
                # 保存原故障，退出项目→Campaign 锁后再收口，避免重复 flock 同一
                # Campaign。关闭失败不能覆盖最初故障或伪造零请求。
                reservation_failure = reservation_error
    if reservation_failure is not None:
        try:
            services.close_capture(task)
        except BaseException as close_error:
            raise reservation_failure from close_error
        raise reservation_failure
    if already_imported:
        # 离开锁后进入 import 自己的锁顺序，避免同进程重复获取文件锁。
        result = importer.import_bundle(campaign_dir, attempt_root, task["client_id"], task_path, bundle_root)
        return {key: value for key, value in result.items() if key != "live_request_count"}
    execution_error = None
    close_attempted = False

    def close_before_seal() -> None:
        nonlocal close_attempted
        # 标记的是已尝试收口；未知关闭不能在 finally 中重发或假装完成。
        close_attempted = True
        services.close_capture(task)

    try:
        recorder = services.recorder(task, bundle_root)
        claude_mac_recorder.record_product(task, bundle_root, recorder=recorder,
                                          before_seal=close_before_seal)
    except Exception as error:
        execution_error = error
    finally:
        # 工厂或启动前验证失败时尚未到屏障，也要收口原预约；屏障已尝试则
        # 保留其真实结果，恢复另走同 producer 同步入口，不在此重复关闭。
        if not close_attempted:
            try:
                close_before_seal()
            except Exception as error:
                execution_error = execution_error or error
    if execution_error is not None:
        # 失败先落盘，再交给同一预约索引的 provenance 收账；不能先导入并签成功后才发现失败。
        importer._write(run_root / "client-run-failure.json", {
            "schema_version": "claude-upgrade-client-run-failure/v2",
            "task_sha256": task["task_sha256"], "producer_run_id": task["execution"]["producer_run_id"],
            "error_type": type(execution_error).__name__,
        })
        return {"status": "accounting_unresolved", "reason": "产品执行或独立捕获关闭失败，先按预约索引收账",
                "execution_error": str(execution_error), "producer_run_id": task["execution"]["producer_run_id"]}
    try:
        result = importer.import_bundle(campaign_dir, attempt_root, task["client_id"], task_path, bundle_root)
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {"status": "accounting_unresolved", "reason": str(error), "execution_error": str(execution_error) if execution_error else None, "producer_run_id": task["execution"]["producer_run_id"]}
    # 导入动作的零请求值不能代表整次 client-run；实际请求只由原 provenance 入账。
    return {key: value for key, value in result.items() if key != "live_request_count"}


def command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """仅在候选父批次中执行；离线导入入口与真实请求入口分别准入。"""
    from tools.official_client_capture import claude_upgrade
    from tools.official_client_capture import claude_upgrade_client_receipts as importer
    from tools.official_client_capture import claude_upgrade_mac_candidate_services
    from tools.official_client_capture import claude_upgrade_mac_budget as budget
    from tools.official_client_capture import codex_upgrade_project_ledger as ledger
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor

    c.require(os.environ.get(supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1",
              "client-run 只能由候选父批次监督派发")
    c.require(getattr(arguments, "acknowledge_live_requests", False),
              "client-run 必须明确确认真实请求")
    c.require(arguments.client in c.CLIENTS and arguments.candidate_id and arguments.attempt_id
              and arguments.client_task is not None, "client-run 缺客户端、候选、attempt 或冻结任务")
    c.require(arguments.client_evidence_action is None and arguments.client_plan is None
              and arguments.bundle is None, "client-run 不接受离线 prepare、verify 或 import 参数")
    campaign_dir = Path(arguments.campaign_dir)
    claude_upgrade._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    _manifest, attempt_root, _attempt = importer._context(
        campaign_dir, arguments.candidate_id, arguments.attempt_id)
    task_path = c.bound_file(attempt_root, c.binding(Path(arguments.client_task), attempt_root))
    task = c.validate_task(c.read_json(task_path))
    c.require(task["client_id"] == arguments.client and task["candidate_id"] == arguments.candidate_id
              and task["attempt_id"] == arguments.attempt_id, "client-run 参数与冻结任务不一致")
    c.require(task["execution"]["source_scope"] == "formal", "client-run 只接受正式任务")
    # 缺正式来源时在项目总账和监督服务之前停止；不生成预约或派发意图。
    _adapter(task)
    run_root = attempt_root / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"]
    reservation_id = None
    with importer._client_control_lock(campaign_dir):
        receipt = budget.read_task_bound_receipt(Path(task["execution"]["project_ledger"]), task)
        if receipt is None:
            local = run_root / "budget-reservation.json"
            c.require(not local.exists() and not local.is_symlink(), "本地预算原件缺真实占用事件，禁止降级")
        row = budget.verify_bound_receipt(Path(task["execution"]["project_ledger"]), receipt,
                                         task_sha256=task["task_sha256"]) if receipt is not None else None
        cleanup = any(path.exists() or path.is_symlink() for path in
                      (run_root / "run.json", run_root / "dispatch-intent.json")) or _capture_terminal_exists(task) or (row is not None and row["status"] == "settled")
        if cleanup:
            admission = budget.accounting_runtime(campaign_dir, task)
        elif row is not None:
            reservation_id = budget.reservation_operation_id(task["task_sha256"])
    if cleanup:
        # 原截止已到仍可重放精确账务；没有新的 lease、factory 或派发。
        services = claude_upgrade_mac_candidate_services.CandidateMacClientServices(campaign_dir, admission)
        result = run_client(task_path, attempt_root, services=services)
        return result, 0 if result.get("status") == "imported" else 2
    active = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
    c.require(active is not None and active._attached_parent and active._supervisor is not None
              and claude_upgrade._lease_identity_matches(
                  active, campaign_dir, phase="candidate", candidate_id=arguments.candidate_id),
              "client-run 未附着同一候选的原父监督租约")
    active.check("mac-client:entry")
    # 取得原总账准入上下文后释放外层项目锁；run_client 自己按项目→Campaign 取锁。
    with ledger.runtime_admission_scope(campaign_dir, require=True, command="campaign-run",
                                        reservation_operation_id=reservation_id) as admission:
        c.require(admission is not None, "client-run 缺既有项目总账准入")
        services = claude_upgrade_mac_candidate_services.CandidateMacClientServices(campaign_dir, admission)
    result = run_client(task_path, attempt_root, services=services)
    return result, 0 if result.get("status") == "imported" else 2
