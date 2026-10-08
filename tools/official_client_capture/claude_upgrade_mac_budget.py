"""原 Claude attempt 的持久请求上界占用，不把预约记成已发请求。

占用和结清都追加到原项目总账。独立捕获预约可以引用这里的不可变收据，但
收据本身不授予产品启动或来源权；正式入口仍须验证原 lease、独立来源与 ready。
未知、截断和到期都不自动释放。结清须先沿原 COMMIT 链核清全部原生请求。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_project_ledger as ledger

RESERVED_EVENT = "claude_live_request_bound_reserved"
SETTLED_EVENT = "claude_live_request_bound_settled"
BOUND_SCHEMA = "claude-upgrade-live-request-bound/v1"
RECEIPT_SCHEMA = "claude-upgrade-live-request-bound-receipt/v1"
BOUND_FIELDS = frozenset({"schema_version", "client", "campaign_id", "candidate_id", "attempt_id", "run_nonce", "candidate_revision", "task_sha256", "producer_run_id", "capture_id", "request_budget", "deadline_utc", "task_binding", "attempt_reservation_binding"})
RESERVATION_FIELDS = frozenset({"campaign_id", "bound", "admission_head", "available_before", "reserved_at_utc"})


def _binding(value: Any, label: str) -> dict[str, Any]:
    """这里只核绑定形状；实际原件在受管写入或来源消费路径独立复核。"""
    ledger._expect(value, {"path", "sha256", "bytes"}, label)
    path = Path(str(value["path"]))
    if not isinstance(value["path"], str) or not value["path"] or path == Path(".") or path.is_absolute() or ".." in path.parts or path.as_posix() != value["path"]:
        raise ledger.ProjectLedgerError(f"{label} 必须是冻结根内的相对路径")
    ledger._sha_field(value["sha256"], label)
    ledger._count(value["bytes"], label)
    return dict(value)


def validate_bound(value: Mapping[str, Any]) -> dict[str, Any]:
    """一份占用绑定原任务和原 attempt；不能把不同 producer 当同一预约。"""
    ledger._expect(value, BOUND_FIELDS, "Mac 请求上界")
    bound = dict(value)
    if bound["schema_version"] != BOUND_SCHEMA or bound["client"] != "claude":
        raise ledger.ProjectLedgerError("Mac 请求上界只属于 Claude v2 总账")
    for key in ("campaign_id", "candidate_id", "attempt_id", "run_nonce", "producer_run_id", "capture_id"):
        ledger._safe_id(bound[key], key)
    ledger._sha_field(bound["task_sha256"], "task_sha256")
    if ledger._count(bound["candidate_revision"], "candidate_revision") < 1 or ledger._count(bound["request_budget"], "request_budget") < 1:
        raise ledger.ProjectLedgerError("候选 revision 和请求上界必须为正整数")
    ledger._timestamp(bound["deadline_utc"], "deadline_utc")
    for key in ("task_binding", "attempt_reservation_binding"):
        bound[key] = _binding(bound[key], key)
    return bound


def reservation_operation_id(task_sha256: str) -> str:
    ledger._sha_field(task_sha256, "task_sha256")
    return "mac-budget:" + task_sha256


def available_for_dispatch(head: Mapping[str, Any], campaign_id: str, reservation_id: str | None = None, *, now: datetime | None = None) -> int | None:
    """竞争派发扣除全部活动占用；原任务复核只可使用自己已经占用的额度。"""
    remaining = head.get("available_live_requests", head.get("remaining_live_requests"))
    if reservation_id is None:
        return remaining
    row = head.get("live_request_bounds", {}).get(reservation_id)
    if not isinstance(row, Mapping) or row.get("status") != "reserved" or row.get("bound", {}).get("campaign_id") != campaign_id:
        raise ledger.ProjectLedgerError("派发复核没有属于原任务 Campaign 的活动预算占用")
    if (now or datetime.now(timezone.utc)) >= ledger._timestamp(row["bound"]["deadline_utc"], "deadline_utc"):
        raise ledger.ProjectLedgerError("原任务预算占用的 deadline 已到，不能派发；占用仍保留")
    if remaining is None:
        return None
    own = int(row["bound"]["request_budget"])
    other = int(head.get("reserved_live_requests", 0)) - own
    usable = max(int(head["remaining_live_requests"]) - other, 0)
    if usable < own:
        raise ledger.ProjectLedgerError("项目实际余额不能覆盖原冻结上界；预算占用仍保留")
    return usable


def validate_new_reservation(plan: Mapping[str, Any], head: Mapping[str, Any], operation_id: str, payload: Mapping[str, Any]) -> None:
    """新追加时核真实账头余额；历史重放不让后来更正改写形成时的准入事实。"""
    if ledger._plan_client(plan) != "claude" or plan["schema_version"] != ledger.PLAN_SCHEMA_V2:
        raise ledger.ProjectLedgerError("Codex 或历史总账不能追加 Claude 请求上界占用")
    ledger._expect(payload, RESERVATION_FIELDS, "预算占用事件")
    bound = validate_bound(payload["bound"])
    if operation_id != reservation_operation_id(bound["task_sha256"]) or payload["campaign_id"] != bound["campaign_id"]:
        raise ledger.ProjectLedgerError("预算占用幂等键未绑定原任务")
    available = available_for_dispatch(head, bound["campaign_id"])
    if payload["available_before"] != available or (available is not None and bound["request_budget"] > available):
        raise ledger.ProjectLedgerError("项目可用额度不足以覆盖冻结请求上界，或准入余额已变化")
    if payload["admission_head"] != {"sequence": head["sequence"], "sha256": head["head_sha256"]}:
        raise ledger.ProjectLedgerError("预算占用必须绑定追加前的原项目账头")


def replay_bound_event(plan: Mapping[str, Any], state: dict[str, Any], event: Mapping[str, Any], events: list[dict[str, Any]], overlays: Mapping[str, Any]) -> None:
    """只派生占用状态，不复制 precise、estimated 或原生请求去重算法。"""
    if ledger._plan_client(plan) != "claude" or plan["schema_version"] != ledger.PLAN_SCHEMA_V2:
        raise ledger.ProjectLedgerError("Claude 请求上界事件不得进入 Codex 或历史总账")
    payload, operation_id = event["payload"], event["operation_id"]
    reservations = state["live_request_bounds"]
    if event["event_type"] == RESERVED_EVENT:
        ledger._expect(payload, RESERVATION_FIELDS, "预算占用事件")
        bound = validate_bound(payload["bound"])
        campaign_id = bound["campaign_id"]
        registered = state["registered_campaigns"].get(campaign_id)
        if not registered or registered.get("client") != "claude" or registered.get("campaign_mode") != "formal" or campaign_id in state["terminal_campaigns"] or campaign_id in state["rejected_campaigns"] or campaign_id in state["paused_campaigns"]:
            raise ledger.ProjectLedgerError("预算占用必须属于当前已注册、未暂停的原 Formal Campaign")
        expected_head = {"sequence": event["sequence"] - 1, "sha256": event["previous_event_sha256"] or plan["plan_sha256"]}
        if operation_id != reservation_operation_id(bound["task_sha256"]) or payload["campaign_id"] != campaign_id or payload["admission_head"] != expected_head or payload["reserved_at_utc"] != event["recorded_at_utc"] or event["source_batch_sha256"] is not None:
            raise ledger.ProjectLedgerError("预算占用任务、形成时间或账头 CAS 绑定不一致")
        available = payload["available_before"]
        if available is not None and ledger._count(available, "available_before") < bound["request_budget"]:
            raise ledger.ProjectLedgerError("形成期可用余额不足以覆盖上界")
        deadline = ledger._timestamp(bound["deadline_utc"], "deadline_utc")
        campaign_deadline = state["effective_campaign_deadlines"].get(campaign_id, registered.get("deadline_at_utc"))
        if ledger._timestamp(payload["reserved_at_utc"], "reserved_at_utc") >= deadline or deadline > ledger._timestamp(state["effective_absolute_deadline_utc"], "project deadline") or (campaign_deadline is not None and deadline > ledger._timestamp(campaign_deadline, "Campaign deadline")):
            raise ledger.ProjectLedgerError("冻结请求上界超过原项目／Campaign 的 deadline")
        for previous in reservations.values():
            old = previous["bound"]
            if old["producer_run_id"] == bound["producer_run_id"] or old["capture_id"] == bound["capture_id"]:
                raise ledger.ProjectLedgerError("不同任务重复占用原 producer／capture 坐标")
        reservations[operation_id] = {"bound": bound, "status": "reserved", "reservation_event_sha256": event["event_sha256"], "reservation_sequence": event["sequence"], "reserved_at_utc": payload["reserved_at_utc"]}
        return
    ledger._expect(payload, {"reservation_operation_id", "reservation_event_sha256", "campaign_id", "task_sha256", "accounting_event", "resolution_event", "identity_keys", "capture_terminal_binding"}, "预算结清事件")
    row = reservations.get(payload["reservation_operation_id"])
    if row is None or row["status"] != "reserved" or row["reservation_event_sha256"] != payload["reservation_event_sha256"] or row["bound"]["campaign_id"] != payload["campaign_id"] or row["bound"]["task_sha256"] != payload["task_sha256"]:
        raise ledger.ProjectLedgerError("预算结清未绑定原活动占用")
    _binding(payload["capture_terminal_binding"], "独立捕获终态")
    keys = _identity_keys(payload["identity_keys"])
    accounting = _referenced_event(payload["accounting_event"], events, before=event["sequence"])
    original = accounting["payload"]
    effective = overlays.get(accounting["operation_id"], original)
    bound = row["bound"]
    if operation_id != "mac-budget-settled:" + bound["task_sha256"] or event["source_batch_sha256"] is not None:
        raise ledger.ProjectLedgerError("预算结清的幂等键或事件来源不合法")
    if accounting["event_type"] != "reconciliation_committed" or accounting["source_batch_sha256"] is None or any(original.get(key) != expected or effective.get(key) != expected for key, expected in {"campaign_id": bound["campaign_id"], "task_sha256": bound["task_sha256"], "subject_kind": "client_capture", "subject_id": bound["producer_run_id"], "candidate_id": bound["candidate_id"], "phase": "candidate"}.items()):
        raise ledger.ProjectLedgerError("预算结清必须引用原任务的真实 COMMIT 入账事件")
    request = effective.get("request", {})
    if payload["resolution_event"] is not None:
        resolved = _referenced_event(payload["resolution_event"], events, before=event["sequence"])
        if resolved["event_type"] != "accounting_resolved" or resolved["payload"].get("resolved_operation_id") != accounting["operation_id"]:
            raise ledger.ProjectLedgerError("预算结清引用了其他 operation 的账务解决事件")
        request = resolved["payload"].get("request", {})
    if request.get("status") != "resolved" or not set(keys) <= set(request.get("identity_keys", [])) or not set(keys) <= state["accounted_identity_index"] | state["initial_identity_keys"] or accounting["operation_id"] in state["unresolved_operation_ids"]:
        raise ledger.ProjectLedgerError("原任务请求尚未全部精确入账，预算占用不得释放")
    row["status"] = "settled"
    row["settlement_event_sha256"] = event["event_sha256"]
    row["settlement"] = dict(payload)


def _identity_keys(value: Any) -> list[str]:
    """只接收已核验的原生请求键全集；不截断到冻结上界。"""
    if not isinstance(value, list) or any(not isinstance(key, str) for key in value):
        raise ledger.ProjectLedgerError("结清原生请求键必须是字符串列表")
    for key in value:
        ledger._sha_field(key, "identity_key")
    if value != sorted(set(value)):
        raise ledger.ProjectLedgerError("结清原生请求键必须排序且唯一")
    return list(value)


def _referenced_event(reference: Any, events: list[dict[str, Any]], *, before: int) -> dict[str, Any]:
    ledger._expect(reference, {"operation_id", "event_sha256", "sequence"}, "项目事件引用")
    matches = [event for event in events if event["operation_id"] == reference["operation_id"]]
    if len(matches) != 1 or matches[0]["event_sha256"] != reference["event_sha256"] or matches[0]["sequence"] != reference["sequence"] or matches[0]["sequence"] >= before:
        raise ledger.ProjectLedgerError("预算引用的项目事件不可达、晚于消费或摘要不符")
    return matches[0]


def _event_ref(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: event[key] for key in ("operation_id", "event_sha256", "sequence")}


def _receipt(root: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema_version": RECEIPT_SCHEMA, "project_ledger": str(root), "bound": row["bound"], "reservation_event": {"operation_id": reservation_operation_id(row["bound"]["task_sha256"]), "event_sha256": row["reservation_event_sha256"], "sequence": row["reservation_sequence"]}, "reserved_at_utc": row["reserved_at_utc"]}


def reserve_bound(admission: ledger.RuntimeAdmission, bound: Mapping[str, Any], *, expected_sequence: int, expected_head_sha256: str) -> dict[str, Any]:
    """锁内持久追加占用；相同原任务只返回同一事件，不刷新 deadline 或余额。"""
    if ledger._PROJECT_LOCK_DEPTH < 1:
        raise ledger.ProjectLedgerError("请求上界占用必须在原项目锁内执行")
    bound = validate_bound(bound)
    plan, _raw = ledger._load_plan(admission.root)
    if plan != admission.plan or ledger._plan_client(plan) != "claude" or admission.campaign_plan["campaign_id"] != bound["campaign_id"]:
        raise ledger.ProjectLedgerError("请求上界占用未复用原 Claude RuntimeAdmission")
    head = admission.refresh()
    operation_id = reservation_operation_id(bound["task_sha256"])
    existing = head.get("live_request_bounds", {}).get(operation_id)
    if existing is not None:
        if existing["bound"] != bound:
            raise ledger.ProjectLedgerError("同一 task 的预算占用内容发生冲突")
        return _receipt(admission.root, existing)
    if not plan["fixture_only"]:
        _verify_formal_reservation_inputs(admission, bound)
    admission.reservation_cas(expected_sequence=expected_sequence, expected_head_sha256=expected_head_sha256)
    problems = ledger._runtime_admission_problems(plan, head, admission.campaign_plan, now=None)
    if problems:
        raise ledger.ProjectLedgerError("预算占用准入拒绝：" + "；".join(problems))
    at = ledger._utc_now()
    payload = {"campaign_id": bound["campaign_id"], "bound": bound, "admission_head": {"sequence": head["sequence"], "sha256": head["head_sha256"]}, "available_before": available_for_dispatch(head, bound["campaign_id"]), "reserved_at_utc": at}
    after, _status = ledger.append_project_event(admission.root, operation_id=operation_id, event_type=RESERVED_EVENT, payload=payload, source_batch_sha256=None, expected_head_sha256=head["head_sha256"], recorded_at_utc=at)
    admission.head = after
    return _receipt(admission.root, after["live_request_bounds"][operation_id])


def verify_bound_receipt(root: Path, receipt: Mapping[str, Any], *, task_sha256: str, dispatch: bool = False) -> dict[str, Any]:
    """只读核真实事件，不取项目锁、不写缓存；派发用途要求调用方已持原项目锁。"""
    ledger._expect(receipt, {"schema_version", "project_ledger", "bound", "reservation_event", "reserved_at_utc"}, "预算占用收据")
    bound = validate_bound(receipt["bound"])
    if receipt["schema_version"] != RECEIPT_SCHEMA or not isinstance(receipt["project_ledger"], str) or Path(receipt["project_ledger"]) != root or bound["task_sha256"] != task_sha256:
        raise ledger.ProjectLedgerError("预算收据未绑定原项目与任务")
    if dispatch and ledger._PROJECT_LOCK_DEPTH < 1:
        raise ledger.ProjectLedgerError("派发预算复核必须持有原项目锁")
    plan, _raw = ledger._load_plan(root)
    head = ledger._replay(root, plan, ledger._load_events(root), rebuild_cache=False)
    row = head.get("live_request_bounds", {}).get(reservation_operation_id(task_sha256))
    if row is None or ledger._canonical(dict(receipt)) != ledger._canonical(_receipt(root, row)):
        raise ledger.ProjectLedgerError("预算收据不等于原项目链中的持久预约")
    if dispatch:
        available_for_dispatch(head, bound["campaign_id"], reservation_operation_id(task_sha256))
    return dict(row)


def settle_bound(admission: ledger.RuntimeAdmission, receipt: Mapping[str, Any], accounting: Mapping[str, Any], *, identity_keys: list[str], capture_terminal_binding: Mapping[str, Any]) -> dict[str, Any]:
    """已有精确 COMMIT 后结清；正式总账另从冻结 task 原件重验来源全集。"""
    if ledger._PROJECT_LOCK_DEPTH < 1:
        raise ledger.ProjectLedgerError("预算结清必须在原项目锁内执行")
    ledger._expect(receipt, {"schema_version", "project_ledger", "bound", "reservation_event", "reserved_at_utc"}, "预算占用收据")
    receipt_bound = validate_bound(receipt["bound"])
    if not isinstance(accounting, Mapping):
        raise ledger.ProjectLedgerError("预算结清的账务收据必须为对象")
    row = verify_bound_receipt(admission.root, receipt, task_sha256=receipt_bound["task_sha256"])
    bound = row["bound"]
    plan, _raw = ledger._load_plan(admission.root)
    if plan != admission.plan or ledger._plan_client(plan) != "claude" or admission.campaign_plan["campaign_id"] != bound["campaign_id"]:
        raise ledger.ProjectLedgerError("预算结清未复用原 Claude RuntimeAdmission")
    keys = _identity_keys(identity_keys)
    terminal_binding = _binding(capture_terminal_binding, "独立捕获终态")
    head = admission.refresh()
    if not plan["fixture_only"]:
        _verify_formal_settlement_inputs(admission, bound, accounting, keys, terminal_binding)
    events = ledger._load_events(admission.root)
    matches = [event for event in events if event["operation_id"] == accounting.get("operation_id")]
    if len(matches) != 1 or matches[0]["source_batch_sha256"] != accounting.get("batch_sha256") or accounting.get("task_sha256") != bound["task_sha256"] or accounting.get("identity_keys_sha256") != ledger._digest(keys) or accounting.get("project_ledger") != str(admission.root):
        raise ledger.ProjectLedgerError("预算结清的账务收据未绑定真实原任务入账事件")
    campaign_dir = Path(head["registered_campaigns"][bound["campaign_id"]]["campaign_dir"])
    batches = [ledger._read_batch(path) for _number, path in ledger._batch_dirs(campaign_dir / ledger.CAMPAIGN_LEDGER_DIR_NAME / "outbox")]
    committed = [batch for batch in batches if batch["committed"] and batch["commit"]["operation_id"] == accounting["operation_id"]]
    if len(committed) != 1 or committed[0]["commit"]["batch_sha256"] != accounting["batch_sha256"] or ledger._merge_batch_payload(committed[0]) != matches[0]["payload"]:
        raise ledger.ProjectLedgerError("预算结清缺真实且内容相同的 Campaign COMMIT")
    if row["status"] == "settled":
        previous = row["settlement"]
        if previous["accounting_event"] != _event_ref(matches[0]) or previous["identity_keys"] != keys or previous["capture_terminal_binding"] != terminal_binding:
            raise ledger.ProjectLedgerError("同一 task 的预算结清原件发生冲突")
        head = admission.refresh()
        return {"status": "duplicate", "head_sequence": head["sequence"], "head_sha256": head["head_sha256"], "available_live_requests": head["available_live_requests"]}
    resolutions = [event for event in events if event["event_type"] == "accounting_resolved" and event["payload"].get("resolved_operation_id") == accounting["operation_id"]]
    payload = {"reservation_operation_id": reservation_operation_id(bound["task_sha256"]), "reservation_event_sha256": row["reservation_event_sha256"], "campaign_id": bound["campaign_id"], "task_sha256": bound["task_sha256"], "accounting_event": _event_ref(matches[0]), "resolution_event": _event_ref(resolutions[-1]) if resolutions else None, "identity_keys": keys, "capture_terminal_binding": terminal_binding}
    after, status = ledger.append_project_event(admission.root, operation_id="mac-budget-settled:" + bound["task_sha256"], event_type=SETTLED_EVENT, payload=payload, source_batch_sha256=None)
    admission.head = after
    return {"status": status, "head_sequence": after["sequence"], "head_sha256": after["head_sha256"], "available_live_requests": after["available_live_requests"]}


def bound_from_task(attempt_root: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    """预算使用原 awaiting_receipts attempt；不建第二个 attempt 或 lease。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract

    task = contract.validate_task(task)
    contract.verify_task_attempt(attempt_root, task)
    attempt = contract.read_json(contract.secure_file(attempt_root, "attempt.json"))
    contract.require(attempt.get("status") == "awaiting_receipts", "预算必须复用原 awaiting_receipts attempt")
    campaign_dir = attempt_root.parents[3]
    expected = campaign_dir / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
    contract.require(attempt_root == expected, "预算占用不能更换原 attempt 坐标")
    task_path = attempt_root / "client-evidence" / task["client_id"] / "tasks" / task["task_nonce"] / "task.json"
    contract.require(contract.read_json(contract.secure_file(campaign_dir, task_path.relative_to(campaign_dir).as_posix())) == task, "预算任务必须是原已冻结 task 原件")
    reservation = contract.secure_file(attempt_root, "reservation.json")
    return validate_bound({"schema_version": BOUND_SCHEMA, "client": "claude", **{key: task[key] for key in contract.COORDINATES}, "candidate_revision": task["candidate_revision"], "task_sha256": task["task_sha256"], "producer_run_id": task["execution"]["producer_run_id"], "capture_id": task["execution"]["capture_id"], "request_budget": task["execution"]["request_budget"], "deadline_utc": task["execution"]["request_policy"]["deadline_utc"], "task_binding": contract.binding(task_path, campaign_dir), "attempt_reservation_binding": contract.binding(reservation, campaign_dir)})


def reserve_task_bound(admission: ledger.RuntimeAdmission, attempt_root: Path, task: Mapping[str, Any]) -> dict[str, Any]:
    """供受监督候选服务接线；仅追加持久占用，不启动产品或捕获服务。"""
    bound = bound_from_task(attempt_root, task)
    if Path(task["execution"]["project_ledger"]) != admission.root:
        raise ledger.ProjectLedgerError("预算任务不能指向原 Campaign 之外的账本")
    return reserve_bound(admission, bound, expected_sequence=admission.head_sequence, expected_head_sha256=admission.head_sha256)


def _task_attempt(head: Mapping[str, Any], task: Mapping[str, Any]) -> tuple[Path, Path]:
    """只从原注册事件定位任务，不能由上传包换 Campaign 或 attempt。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract

    registered = head["registered_campaigns"].get(task["campaign_id"])
    contract.require(registered is not None and registered.get("client") == "claude", "任务的原 Claude Campaign 未注册")
    campaign = Path(registered["campaign_dir"])
    attempt = campaign / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
    return campaign, attempt


def read_task_bound_receipt(project_root: Path, task: Mapping[str, Any]) -> dict[str, Any] | None:
    """只读重建原收据并核整个 bound；不取锁、写缓存、刷新窗口或授准入权。

    首次追加后尚未发布本地收据的断点可以由真实事件重建。反过来，有本地
    收据却缺真实事件、同 producer 被另一个 task 占用或任一绑定漂移，均拒绝，
    不能降级为新任务或无占用的历史导入。
    """
    from tools.official_client_capture import claude_client_receipt_contract as contract

    task = contract.validate_task(task)
    root = Path(project_root)
    contract.require(root == Path(task["execution"]["project_ledger"]), "任务占用不能借其他项目总账")
    plan, _raw = ledger._load_plan(root)
    ledger._require_plan_client(plan, "claude", "Mac 占用只读消费")
    head = ledger._replay(root, plan, ledger._load_events(root), rebuild_cache=False)
    row = head.get("live_request_bounds", {}).get(reservation_operation_id(task["task_sha256"]))
    if row is None:
        for previous in head.get("live_request_bounds", {}).values():
            old = previous["bound"]
            contract.require(all(old[name] != task["execution"][name] for name in ("producer_run_id", "capture_id")),
                             "其他 task 已占用相同 producer 或 capture，不能借用")
        if task["campaign_id"] in head["registered_campaigns"]:
            _campaign, attempt = _task_attempt(head, task)
            local = attempt / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"] / "budget-reservation.json"
            contract.require(not local.exists() and not local.is_symlink(), "本地预算原件缺真实占用事件，禁止降级")
        return None
    campaign, attempt = _task_attempt(head, task)
    contract.require(ledger.find_project_ledger(campaign, client="claude") == root, "原 Campaign 与预算总账不符")
    expected = bound_from_task(attempt, task)
    local = attempt / "client-evidence" / task["client_id"] / "runs" / task["task_nonce"] / "budget-reservation.json"
    contract.require(row["bound"] == expected, "原任务预算 bound 或冻结原件发生漂移")
    receipt = _receipt(root, row)
    verify_bound_receipt(root, receipt, task_sha256=task["task_sha256"])
    if local.exists() or local.is_symlink():
        original = contract.read_json(contract.secure_file(attempt, local.relative_to(attempt).as_posix()))
        contract.require(ledger._canonical(original) == ledger._canonical(receipt), "本地预算原件与真实事件收据冲突")
    return receipt


def accounting_runtime(campaign_dir: Path, task: Mapping[str, Any]) -> ledger.RuntimeAdmission:
    """原锁内重建同一账务数据上下文；不运行 live 准入，不新建 lease 或 attempt。

    到期、暂停或产品失败后仍可精确收账。这不是派发入口，调用方只能将结果
    交给已有结清原语；任何后续派发仍必须经过完整 Candidate 与实时预算门禁。
    """
    from tools.official_client_capture import claude_client_receipt_contract as contract

    contract.require(ledger._PROJECT_LOCK_DEPTH > 0, "原账务上下文必须在项目锁内重建")
    task = contract.validate_task(task)
    campaign_dir = Path(campaign_dir)
    root = ledger.find_project_ledger(campaign_dir, client="claude")
    contract.require(root is not None and root == Path(task["execution"]["project_ledger"]), "账务上下文不是原任务总账")
    plan, _raw = ledger._load_plan(root)
    ledger._require_plan_client(plan, "claude", "Mac 精确收账")
    ledger._check_fixture_only(root, plan, campaign_dir)
    head = ledger._replay(root, plan, ledger._load_events(root), rebuild_cache=False)
    campaign, attempt = _task_attempt(head, task)
    contract.require(campaign == campaign_dir, "账务上下文不能换 Campaign")
    bound_from_task(attempt, task)
    campaign_plan = ledger._campaign_plan(campaign_dir / ledger.CAMPAIGN_LEDGER_DIR_NAME)
    contract.require(campaign_plan is not None and campaign_plan["campaign_id"] == task["campaign_id"], "缺原 Campaign plan")
    return ledger.RuntimeAdmission(root, plan, head, campaign_plan)


def _frozen_task(admission: ledger.RuntimeAdmission, bound: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    """只从已注册原 Campaign 取冻结 task 与原预约，不接收上传包代替原件。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract

    registered = admission.head["registered_campaigns"].get(bound["campaign_id"])
    contract.require(registered is not None, "冻结 task 的原 Campaign 未注册")
    campaign = Path(registered["campaign_dir"])
    task = contract.validate_task(contract.read_json(contract.bound_file(campaign, bound["task_binding"])))
    contract.bound_file(campaign, bound["attempt_reservation_binding"])
    contract.require(task["task_sha256"] == bound["task_sha256"] and task["candidate_revision"] == bound["candidate_revision"] and all(task[key] == bound[key] for key in contract.COORDINATES)
                     and all(task["execution"][key] == bound[key] for key in ("producer_run_id", "capture_id", "request_budget"))
                     and task["execution"]["request_policy"]["deadline_utc"] == bound["deadline_utc"]
                     and Path(task["execution"]["project_ledger"]) == admission.root, "预算绑定的冻结 task 原件或坐标漂移")
    return campaign, task


def _verify_formal_reservation_inputs(admission: ledger.RuntimeAdmission, bound: Mapping[str, Any]) -> None:
    """正式追加低层原语也必须复用真正等待收据的原 attempt。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract

    campaign, task = _frozen_task(admission, bound)
    contract.require(task["execution"]["source_scope"] == "formal", "正式总账不能占用回归夹具任务")
    attempt = campaign / "candidates" / task["candidate_id"] / "attempts" / task["attempt_id"]
    contract.require(bound_from_task(attempt, task) == bound, "正式预算占用不等于原 awaiting_receipts attempt 的冻结绑定")


def _terminal_inputs(task: Mapping[str, Any], *, undispatched: bool) -> tuple[list[str], dict[str, Any]]:
    """静态受管来源核完整请求或未派发零值证明，不由调用者提供请求子集。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract
    from tools.official_client_capture import claude_mac_client_source as source
    from tools.official_client_capture import claude_upgrade_mac_client_capture as control

    contract.require(task["execution"]["source_scope"] == "formal", "预算结清必须重验受管独立来源")
    root = Path(task["execution"]["capture_root"])
    if undispatched:
        contract.require(control._trusted_zero(task, root), "缺真实未派发及零请求证明，预算不得释放")
        keys, terminal = [], contract.secure_file(root, "undispatched.json")
    else:
        requests = source.collect_independent_requests_v2(task, root)
        terminal = contract.secure_file(root, "capture-manifest.json")
        contract.require(contract.read_json(terminal)["status"] in {"complete", "failed"}, "截断捕获保留占用，不能结清")
        keys = sorted(row["identity_key"] for row in requests["requests"])
    return keys, contract.binding(terminal, root)


def _verify_formal_settlement_inputs(admission: ledger.RuntimeAdmission, bound: Mapping[str, Any], accounting: Mapping[str, Any], keys: list[str], terminal_binding: Mapping[str, Any]) -> None:
    """防止低层受管原语被误用：正式结清必须从原 Campaign 冻结原件自行取全集。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract
    from tools.official_client_capture import claude_upgrade_client_receipts as importer

    _campaign, task = _frozen_task(admission, bound)
    contract.require(terminal_binding["path"] in {"capture-manifest.json", "undispatched.json"}, "正式结清须绑定真实捕获终态或未派发原件")
    actual_keys, actual_terminal = _terminal_inputs(task, undispatched=terminal_binding["path"] == "undispatched.json")
    contract.require(keys == actual_keys and terminal_binding == actual_terminal, "正式结清没有覆盖全部独立原生请求与真实终态")
    importer.verify_accounting(accounting, task, actual_keys)


def settle_task_bound(admission: ledger.RuntimeAdmission, task: Mapping[str, Any], receipt: Mapping[str, Any], accounting: Mapping[str, Any], *, undispatched: bool = False) -> dict[str, Any]:
    """受管来源验证全部原件后结清；正式 registry 关闭时不能生成释放权限。"""
    from tools.official_client_capture import claude_client_receipt_contract as contract
    from tools.official_client_capture import claude_upgrade_client_receipts as importer

    task = contract.validate_task(task)
    keys, terminal = _terminal_inputs(task, undispatched=undispatched)
    importer.verify_accounting(accounting, task, keys)
    contract.require(receipt["bound"]["task_sha256"] == task["task_sha256"] and all(receipt["bound"][key] == task[key] for key in contract.COORDINATES) and all(receipt["bound"][key] == task["execution"][key] for key in ("producer_run_id", "capture_id", "request_budget")), "预算结清与原任务坐标不一致")
    return settle_bound(admission, receipt, accounting, identity_keys=keys, capture_terminal_binding=terminal)
