"""录制夹具的预算携带：保留原失败世界，仅投影剩余额度，不提供正式账本迁移。

原项目账和时间账只读重放；新账以实际创建时刻起算，原起点、阶段消耗、请求键、根因和已批准延期
在不可变收据中保留。创建失败后不能换起点重获额度；后继只消费原 CLI 双账闭合的项目、Campaign 和阶段追加延期。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from tools.official_client_capture import codex_upgrade_project_ledger as project
from tools.official_client_capture import codex_upgrade_timing_ledger as timing
from tools.official_client_capture.tests import claude_recorded_chain_source as source

SCHEMA = "claude-recorded-budget-carry/v1"
INITIALIZATION_SCHEMA = "claude-recorded-budget-initialization/v1"
PURPOSE = "recorded_fixture_remaining_budget_projection"
INITIALIZATION_RELATIVE = "control/recorded-budget-initialization.json"


def _now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise source.replay.ReplayError("预算携带时刻必须有时区")
    return value.astimezone(timezone.utc)


def _at(value: str) -> datetime:
    return _now(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _data(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or "staging" not in path.parts:
        raise source.replay.ReplayError("预算携带仅允许无链接的独立 staging 数据根")
    return path


def _bind(path: Path) -> dict[str, Any]:
    binding = source.binding(path)
    source.bound(binding)
    return binding


def _phase_costs(events: Iterable[Mapping[str, Any]], observed: datetime) -> dict[str, float]:
    """当前录制夹具每阶段只开启一段；不把暂停当停止计时，不支持任意历史状态迁移。"""

    costs = dict.fromkeys(timing.PHASE_ORDER, 0.0)
    opened: dict[str, datetime] = {}
    seen: set[str] = set()
    for event in events:
        phase, kind = str(event["phase"]), event["event_type"]
        when = _at(str(event["recorded_at_utc"]))
        if when > observed:
            raise source.replay.ReplayError("预算携带时刻早于原事件")
        if kind in {"stage_abandoned", "evaluation_reopened"}:
            raise source.replay.ReplayError("本录制预算夹具不承接阶段放弃或多段评估状态")
        if kind == "stage_started":
            if phase in seen:
                raise source.replay.ReplayError("本录制预算夹具不承接同阶段多段历史")
            seen.add(phase)
            opened[phase] = when
        elif kind == "stage_completed":
            if phase not in opened:
                raise source.replay.ReplayError("阶段完成缺少真实起点")
            costs[phase] += (when - opened.pop(phase)).total_seconds()
    for phase, started in opened.items():
        costs[phase] += (observed - started).total_seconds()
    return costs


def _snapshot(data: Path, campaign_id: str, observed: datetime) -> dict[str, Any]:
    """直接重放完整权威事件和有效延期；不写锁、缓存或历史事件。"""

    data = _data(data)
    time_root, project_root = data / "control/timing", data / project.ledger_dir_name("claude")
    time_plan, _ = timing._load_plan(time_root)
    project_plan, _ = project._load_plan(project_root)
    project_events = project._load_events(project_root)
    head = project._replay(project_root, project_plan, project_events, rebuild_cache=False)
    summary = timing.inspect_ledger(time_root, now=observed.isoformat())
    if (not project_plan["fixture_only"] or project._plan_client(project_plan) != "claude"
            or timing.plan_client(time_plan) != "claude" or time_plan["target_version"] != source.VERSION
            or time_plan["campaign_purpose"] != "validation_only"):
        raise source.replay.ReplayError("预算携带只接受2.1.226 Claude validation_only录制夹具")
    registered = head["registered_campaigns"].get(campaign_id)
    if (not registered or registered["target_version"] != source.VERSION
            or campaign_id in head["terminal_campaigns"] or campaign_id in head["rejected_campaigns"]):
        raise source.replay.ReplayError("预算携带原 Campaign 未合法注册或已经终态")
    if any(_at(event["recorded_at_utc"]) > observed for event in project_events):
        raise source.replay.ReplayError("预算携带时刻早于项目事件")
    if (head["unresolved_operation_ids"] or head["estimated_total"]
            or any(row["status"] == "reserved" for row in head.get("live_request_bounds", {}).values())
            or project.root_causes_at_limit_for(head, source.VERSION)
            or summary["status_before_pause"] in {"stopped", "stop_required", "complete", "abandoned"}):
        raise source.replay.ReplayError("预算携带原账有未决、估计、预约、根因上限或不可恢复终态")
    for extension in head["deadline_extensions"]:
        if extension["campaign_id"] == campaign_id or extension["scope"] == "project":
            if not project.deadline_extension_applied(extension, head):
                raise source.replay.ReplayError("预算携带原延期尚未双账提交")
            if (extension["campaign_id"] == campaign_id
                    and extension not in summary["deadline_extensions"]):
                raise source.replay.ReplayError("预算携带原时间账未消费同一延期")
    events = timing._load_events(time_root)
    costs = _phase_costs((row[0] for row in events), observed)
    allowances = {phase: float(time_plan["stage_budgets_minutes"][phase] * 60) for phase in timing.PHASE_ORDER}
    for extension in summary["deadline_extensions"]:
        if extension["scope"] == "stage":
            allowances[extension["phase"]] += (_at(extension["new_deadline_at_utc"])
                                              - _at(extension["original_deadline_at_utc"])).total_seconds()
    keys = sorted(project._initial_keys(project_root, project_plan) | set(head["accounted_identity_index"]))
    if head["precise_total"] != len(keys):
        raise source.replay.ReplayError("预算携带原精确计数与完整身份键不闭合")
    bindings = [_bind(time_root / "ledger.json"), _bind(project_root / "plan.json"),
                _bind(project_root / "initial-identity-keys.json")]
    bindings.extend(_bind(time_root / "events" / f"{row[0]['sequence']:06d}.json") for row in events)
    bindings.extend(_bind(project_root / "events" / f"{row['sequence']:06d}.json") for row in project_events)
    for event, _raw in events:
        bindings.extend(_bind(time_root / receipt["path"]) for receipt in event["receipts"])
    migrations = project_root / "migrations"
    if migrations.exists():
        bindings.extend(_bind(path) for path in sorted(migrations.iterdir()))
    formal_open = sum(row["campaign_mode"] == "formal" and cid not in head["terminal_campaigns"]
                      and row["target_version"] == source.VERSION for cid, row in head["registered_campaigns"].items())
    return {
        "source_bindings": bindings,
        "timing_head": {"sequence": summary["head_sequence"], "sha256": summary["head_sha256"]},
        "project_head": {"sequence": head["sequence"], "sha256": head["head_sha256"]},
        "timing_plan_sha256": bindings[0]["sha256"], "project_plan_sha256": bindings[1]["sha256"],
        "original_started_at_utc": time_plan["started_at_utc"],
        "total_deadline_at_utc": summary["total_deadline_at_utc"],
        "project_deadline_at_utc": head["effective_absolute_deadline_utc"],
        "active_stage_deadline_at_utc": summary["stage_deadline_at_utc"],
        "phase_costs_seconds": costs, "phase_allowances_seconds": allowances,
        "active_phase": summary["active_phase"],
        "deadline_extensions": summary["deadline_extensions"],
        "initial_identity_keys": keys, "initial_precise_count": head["precise_total"],
        "initial_estimated_count": head["estimated_total"],
        "initial_root_cause_counts": project.root_cause_counts_for(head, source.VERSION),
        "live_request_budget": head.get("effective_live_request_budget", head["live_request_budget"]),
        "formal_open_available": project_plan["formal_open_limit"] - formal_open,
    }


def _execution_deadline(snapshot: Mapping[str, Any]) -> datetime:
    """原活动阶段仍受绝对截止约束，后继重验前段不能推迟原 VC-5 的批准窗口。"""

    deadlines = [_at(snapshot["total_deadline_at_utc"]), _at(snapshot["project_deadline_at_utc"])]
    if snapshot["active_stage_deadline_at_utc"] is not None:
        deadlines.append(_at(snapshot["active_stage_deadline_at_utc"]))
    return min(deadlines)


def _projection(snapshot: Mapping[str, Any], observed: datetime) -> dict[str, Any]:
    deadline = _execution_deadline(snapshot)
    total = int((deadline - observed).total_seconds() // 60)
    budgets = {phase: min(total, int((snapshot["phase_allowances_seconds"][phase]
                                     - snapshot["phase_costs_seconds"][phase]) // 60)) for phase in timing.PHASE_ORDER}
    failures = (["总预算"] if total < 1 else []) + [phase for phase, minutes in budgets.items() if minutes < 1]
    if snapshot["formal_open_available"] < 1:
        failures.append("Formal名额")
    request_budget = snapshot["live_request_budget"]
    if request_budget is not None and request_budget <= snapshot["initial_precise_count"]:
        failures.append("请求额度")
    if failures:
        raise source.replay.ReplayError("预算携带剩额不足，先在原Campaign合法延期：" + "、".join(failures))
    return {"started_at_utc": observed.isoformat(), "total_budget_minutes": total,
            "stage_budgets_minutes": budgets, "predecessor_phase_costs_seconds": snapshot["phase_costs_seconds"],
            "predecessor_elapsed_seconds": (observed - _at(snapshot["original_started_at_utc"])).total_seconds()}


def create(output: Path, *, data_root: Path, predecessor_data: Path, predecessor_campaign_id: str,
           expected_timing_plan_sha256: str, expected_timing_head: Mapping[str, Any],
           expected_project_plan_sha256: str, expected_project_head: Mapping[str, Any],
           preserved_bindings: Mapping[str, Mapping[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    """冻结真实原账及保留对象；必须由调用者提供外持两账锚点，不自行刷新信任起点。"""

    data_root, predecessor_data, observed = _data(data_root), _data(predecessor_data), _now(now)
    if (output.parent != data_root / "control" or data_root.is_relative_to(predecessor_data)
            or predecessor_data.is_relative_to(data_root)):
        raise source.replay.ReplayError("预算携带收据须在独立后继control目录内")
    if set(preserved_bindings) != {"store_campaign", "batch19_commit"}:
        raise source.replay.ReplayError("预算携带须显式绑定原Store Campaign和第19批COMMIT")
    for binding in preserved_bindings.values():
        source.bound(binding, root=predecessor_data)
    snapshot = _snapshot(predecessor_data, predecessor_campaign_id, observed)
    if (snapshot["timing_plan_sha256"] != expected_timing_plan_sha256
            or snapshot["project_plan_sha256"] != expected_project_plan_sha256
            or snapshot["timing_head"] != dict(expected_timing_head)
            or snapshot["project_head"] != dict(expected_project_head)):
        raise source.replay.ReplayError("预算携带原两账外持锚点漂移")
    _projection(snapshot, observed)
    document = {"schema_version": SCHEMA, "fixture_only": True, "purpose": PURPOSE,
                "formal_ledger_migration": False, "target_version": source.VERSION,
                "data_root": str(data_root), "predecessor_data_root": str(predecessor_data),
                "predecessor_campaign_id": predecessor_campaign_id, "observed_at_utc": observed.isoformat(),
                "preserved_bindings": dict(preserved_bindings), "snapshot": snapshot}
    source.write(output, document)
    return _bind(output)


def load(binding: Mapping[str, Any], *, data_root: Path | None = None) -> dict[str, Any]:
    """外持摘要、原账完整前缀、有效延期与保留对象逐项重验；不接受自称成功的摘要。"""

    path = source.bound(binding, root=data_root)
    document = source.read(path)
    required = {"schema_version", "fixture_only", "purpose", "formal_ledger_migration", "target_version", "data_root",
                "predecessor_data_root", "predecessor_campaign_id", "observed_at_utc", "preserved_bindings", "snapshot"}
    if (set(document) != required or document["schema_version"] != SCHEMA or document["fixture_only"] is not True
            or document["purpose"] != PURPOSE or document["formal_ledger_migration"] is not False
            or document["target_version"] != source.VERSION):
        raise source.replay.ReplayError("预算携带仅接受明确的录制fixture合同")
    data, predecessor = _data(Path(document["data_root"])), _data(Path(document["predecessor_data_root"]))
    if (path.parent != data / "control" or (data_root is not None and data != _data(data_root))
            or data.is_relative_to(predecessor) or predecessor.is_relative_to(data)):
        raise source.replay.ReplayError("预算携带后继坐标漂移或与原世界重叠")
    if set(document["preserved_bindings"]) != {"store_campaign", "batch19_commit"}:
        raise source.replay.ReplayError("预算携带保留对象不闭合")
    for original in document["preserved_bindings"].values():
        source.bound(original, root=predecessor)
    if _snapshot(predecessor, document["predecessor_campaign_id"], _at(document["observed_at_utc"])) != document["snapshot"]:
        raise source.replay.ReplayError("预算携带原两账或有效延期漂移")
    return document


def create_ledgers(data_root: Path, *, carry_binding: Mapping[str, Any], upgrade_id: str,
                   baseline_version: str, target_version: str, now: datetime | None = None) -> dict[str, Path]:
    """集中校验后创建两账；不可变初始化收据固定真实新起点，部分创建失败也不得重新计时。"""

    data, observed = _data(data_root), _now(now)
    document = load(carry_binding, data_root=data)
    if observed < _at(document["observed_at_utc"]):
        raise source.replay.ReplayError("后继创建时刻早于携带收据")
    if target_version != source.VERSION:
        raise source.replay.ReplayError("录制预算携带目标版本漂移")
    initialization = data / INITIALIZATION_RELATIVE
    identity = {"carry_binding": dict(carry_binding), "upgrade_id": upgrade_id,
                "baseline_version": baseline_version, "target_version": target_version}
    if initialization.exists():
        frozen = source.read(initialization)
        if (frozen.get("schema_version") != INITIALIZATION_SCHEMA or frozen.get("identity") != identity
                or frozen.get("fixture_only") is not True):
            raise source.replay.ReplayError("预算携带初始化绑定漂移，禁止重新创建")
        started = _at(frozen["projection"]["started_at_utc"])
        snapshot = _snapshot(Path(document["predecessor_data_root"]), document["predecessor_campaign_id"], started)
        if frozen["projection"] != _projection(snapshot, started):
            raise source.replay.ReplayError("预算携带初始化额度漂移")
    else:
        if any(path.exists() for path in (data / "control/timing", data / project.ledger_dir_name("claude"))):
            raise source.replay.ReplayError("后继已有无携带初始化凭证的账本，禁止覆盖")
        snapshot = _snapshot(Path(document["predecessor_data_root"]), document["predecessor_campaign_id"], observed)
        frozen = {"schema_version": INITIALIZATION_SCHEMA, "fixture_only": True, "identity": identity,
                  "projection": _projection(snapshot, observed)}
        source.write(initialization, frozen)
    projection = frozen["projection"]
    started = _at(projection["started_at_utc"])
    project_root, time_root = data / project.ledger_dir_name("claude"), data / "control/timing"
    if (observed < started or (observed >= _execution_deadline(snapshot)
            and not (project_root.exists() and time_root.exists()))):
        raise source.replay.ReplayError("预算携带创建前总截止已到，禁止重置起点")
    # 已存在的两账由下方只读重放核验追加延期；缺任一本都不能越过原创建窗口重建。
    if not project_root.exists():
        project.create_project_ledger(project_root, project_id="claude-recorded-budget-successor",
            absolute_deadline_utc=snapshot["project_deadline_at_utc"], deadline_approved_by="recorded-fixture-carry",
            estimation_policy="none", estimation_policy_approved_by="recorded-fixture-carry", fixture_only=True,
            live_request_budget=snapshot["live_request_budget"], formal_open_limit=1,
            initial_identity_keys=snapshot["initial_identity_keys"], initial_precise_count=snapshot["initial_precise_count"],
            initial_estimated_count=0, initial_root_cause_counts=snapshot["initial_root_cause_counts"],
            bound_receipts={"recorded_budget_carry": {key: carry_binding[key] for key in ("path", "sha256")},
                            "recorded_budget_initialization": {key: _bind(initialization)[key] for key in ("path", "sha256")}},
            started_at_utc=projection["started_at_utc"], approved_at_utc=projection["started_at_utc"], client="claude")
    if not time_root.exists():
        timing.create_ledger(time_root, upgrade_id=upgrade_id, baseline_version=baseline_version, target_version=target_version,
            campaign_purpose="validation_only", evidence_decision="recapture", started_at_utc=projection["started_at_utc"],
            total_budget_minutes=projection["total_budget_minutes"], stage_budgets_minutes=projection["stage_budgets_minutes"],
            project_ledger_dir=project_root, client="claude")
    verify_budget(data, carry_binding=carry_binding, now=observed)
    return {"project_ledger": project_root, "ledger": time_root, "initialization": initialization}


def verify_accounting(project_root: Path, *, carry_binding: Mapping[str, Any], current_identity_keys: Iterable[str],
                      project_head: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """原初始键与当前Campaign键去重联合核验；不改计数、不补账、不写缓存。"""

    document = load(carry_binding, data_root=project_root.parent)
    plan, _ = project._load_plan(project_root)
    snapshot = document["snapshot"]
    initial = project._initial_keys(project_root, plan)
    if (not plan["fixture_only"] or initial != set(snapshot["initial_identity_keys"])
            or plan["initial_precise_count"] != snapshot["initial_precise_count"] or plan["initial_estimated_count"] != 0
            or plan["bound_receipts"].get("recorded_budget_carry") != {k: carry_binding[k] for k in ("path", "sha256")}):
        raise source.replay.ReplayError("后继项目账没有携带原完整身份键和计数")
    head = project_head if project_head is not None else project._replay(
        project_root, plan, project._load_events(project_root), rebuild_cache=False)
    expected = initial | set(current_identity_keys)
    if (initial | set(head["accounted_identity_index"]) != expected or head["precise_total"] != len(expected)
            or head["estimated_total"] != 0 or head["unresolved_operation_ids"]):
        raise source.replay.ReplayError("后继初始身份键与当前请求联合计账不闭合")
    return {"initial_identity_keys": sorted(initial), "precise_identity_keys": sorted(expected),
            "initial_precise_count": len(initial), "precise_total": head["precise_total"], "fixture_only": True}


def verify_budget(data_root: Path, *, carry_binding: Mapping[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """核原累计成本与真实后继消耗及固定两账绑定；当前完成不能覆盖原失败或签发正式迁移认证。"""

    data, observed = _data(data_root), _now(now)
    document = load(carry_binding, data_root=data)
    initialization = data / INITIALIZATION_RELATIVE
    frozen = source.read(initialization)
    if (frozen.get("schema_version") != INITIALIZATION_SCHEMA or frozen.get("fixture_only") is not True
            or frozen.get("identity", {}).get("carry_binding") != dict(carry_binding)):
        raise source.replay.ReplayError("后继预算初始化凭证漂移")
    projection = frozen["projection"]
    started = _at(projection["started_at_utc"])
    snapshot = _snapshot(Path(document["predecessor_data_root"]), document["predecessor_campaign_id"], started)
    if projection != _projection(snapshot, started) or observed < started:
        raise source.replay.ReplayError("后继预算投影漂移或时间倒退")
    project_root, time_root = data / project.ledger_dir_name("claude"), data / "control/timing"
    plan, _ = project._load_plan(project_root)
    time_plan, _ = timing._load_plan(time_root)
    if (project._plan_client(plan) != "claude" or not plan["fixture_only"] or plan["formal_open_limit"] != 1
            or plan["absolute_deadline_utc"] != snapshot["project_deadline_at_utc"]
            or plan["started_at_utc"] != projection["started_at_utc"]
            or plan["live_request_budget"] != snapshot["live_request_budget"]
            or plan["initial_root_cause_counts"] != snapshot["initial_root_cause_counts"]
            or project._initial_keys(project_root, plan) != set(snapshot["initial_identity_keys"])
            or plan["initial_precise_count"] != snapshot["initial_precise_count"]
            or plan["initial_estimated_count"] != snapshot["initial_estimated_count"]
            or plan["bound_receipts"].get("recorded_budget_carry") != {k: carry_binding[k] for k in ("path", "sha256")}
            or plan["bound_receipts"].get("recorded_budget_initialization") != {k: _bind(initialization)[k] for k in ("path", "sha256")}
            or timing.plan_client(time_plan) != "claude" or time_plan["campaign_purpose"] != "validation_only"
            or time_plan["evidence_decision"] != "recapture"
            or any(time_plan[key] != frozen["identity"][key] for key in ("upgrade_id", "baseline_version", "target_version"))
            or time_plan["started_at_utc"] != projection["started_at_utc"]
            or time_plan["total_budget_minutes"] != projection["total_budget_minutes"]
            or time_plan["stage_budgets_minutes"] != projection["stage_budgets_minutes"]
            or time_plan.get("project_ledger_binding", {}).get("plan_sha256") != _bind(project_root / "plan.json")["sha256"]):
        raise source.replay.ReplayError("后继两账预算、根因或初始化绑定漂移")
    summary = timing.inspect_ledger(time_root, now=observed.isoformat())
    head = project._replay(project_root, plan, project._load_events(project_root), rebuild_cache=False)
    extensions = summary["deadline_extensions"]
    # 延期仍由原 CLI 写入；此处只消费同一后继真实两账的追加链。冻结的原账、初始化和计划不改写，
    # 全部等待成本继续计入；项目、Campaign、阶段各自批准，任何一层都不能替代另一层或消除其他停线。
    deadline = min(_at(summary["total_deadline_at_utc"]), _at(head["effective_absolute_deadline_utc"]))
    additional = dict.fromkeys(timing.PHASE_ORDER, 0.0)
    if head["deadline_extensions"] != extensions:
        raise source.replay.ReplayError("后继延期尚未双账闭合或混入其他Campaign延期")
    for extension in extensions:
        owner = head["registered_campaigns"].get(extension["campaign_id"])
        if (extension["scope"] not in {"project", "campaign", "stage"}
                or (extension["scope"] == "stage" and extension["phase"] not in additional)
                or (extension["scope"] != "stage" and extension["phase"] is not None)
                or not owner or owner["campaign_mode"] != "formal" or owner["target_version"] != source.VERSION
                or owner.get("campaign_dir") != str(data / "evidence/campaigns" / extension["campaign_id"])
                or extension["project_ledger_path"] != str(project_root)
                or not started <= _at(extension["approved_at_utc"]) <= observed):
            raise source.replay.ReplayError("后继只接受同一正式Campaign当前双账批准的延期")
        if not project.deadline_extension_applied(extension, head):
            raise source.replay.ReplayError("后继延期尚未双账提交")
        if extension["scope"] == "stage":
            if _at(extension["new_deadline_at_utc"]) > deadline:
                raise source.replay.ReplayError("后继阶段延期超过当前双账批准的总窗口")
            additional[extension["phase"]] += (_at(extension["new_deadline_at_utc"])
                                               - _at(extension["original_deadline_at_utc"])).total_seconds()
    if observed >= deadline:
        raise source.replay.ReplayError("后继已到有效总截止或活动阶段截止，须由原CLI追加对应层级的双账延期")
    if summary["status"] == "deadline_paused":
        raise source.replay.ReplayError("后继当前预算已暂停；须对实际待续阶段使用原CLI合法延期")
    if summary["status_before_pause"] in {"stopped", "stop_required", "abandoned"}:
        raise source.replay.ReplayError("后继其他停线或终态不能用预算延期解除")
    current_costs = _phase_costs((row[0] for row in timing._load_events(time_root)), observed)
    combined = {phase: projection["predecessor_phase_costs_seconds"][phase] + current_costs[phase]
                for phase in timing.PHASE_ORDER}
    allowances = {phase: snapshot["phase_allowances_seconds"][phase] + additional[phase] for phase in timing.PHASE_ORDER}
    if any(combined[phase] > allowances[phase] for phase in timing.PHASE_ORDER):
        raise source.replay.ReplayError("后继与原阶段的联合耗时超过原批准额度")
    if summary["stage_deadline_at_utc"] is not None:
        deadline = min(deadline, _at(summary["stage_deadline_at_utc"]))
    total = (observed - _at(snapshot["original_started_at_utc"])).total_seconds()
    return {"fixture_only": True, "formal_ledger_migration": False,
            "original_started_at_utc": snapshot["original_started_at_utc"],
            "original_execution_deadline_at_utc": _execution_deadline(snapshot).isoformat(),
            "predecessor_elapsed_seconds": projection["predecessor_elapsed_seconds"],
            "successor_elapsed_seconds": (observed - started).total_seconds(), "combined_elapsed_seconds": total,
            "combined_phase_costs_seconds": combined, "original_deadline_extensions": snapshot["deadline_extensions"],
            "combined_phase_allowances_seconds": allowances, "successor_deadline_extensions": extensions,
            "active_phase": summary["active_phase"], "status": summary["status"],
            "effective_deadline_at_utc": deadline.isoformat(), "remaining_seconds": (deadline - observed).total_seconds(),
            "initialization": _bind(initialization), "timing_head": summary["head_sha256"]}
