"""固定产品来源的私有状态恢复证明，不扩展为通用文件系统恢复器。

授权发生于应用派发之前，精确绑定桥映射、原任务、监测 ready 与状态全集。
真实签名的 ES 变化链须证明全部写者属于本次产品，最终签名快照与独立实读
逐项一致。ACL、xattr、硬链接、目录改名等尚无准确恢复合同的变化保持未知。
v1 的受管操作意图与未知写入拒绝语义完全保留。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_origins as origins

SCHEMA = "claude-mac-product-state-observation/v1"


def _require(value: Any, message: str) -> None:
    if not value:
        from tools.official_client_capture.claude_mac_state_journal import StateConflict
        raise StateConflict(message)


def _owner(recorder: Any, journal: Any, *, live: bool = False) -> dict[str, Any]:
    """仅消费固定工厂实际拥有的原实例；没有回调或布尔授权参数。"""
    from tools.official_client_capture import claude_mac_product_source as products
    from tools.official_client_capture.claude_mac_product_completion import ProductEvidenceRecorder
    _require(type(recorder) is ProductEvidenceRecorder and products._MAC_SOURCE.owns_recorder(recorder)
             and c.source_adapter(recorder._task) is products._MAC_SOURCE, "恢复授权对象不是固定正式产品工厂的原实例")
    owner = products._MAC_SOURCE._owner(recorder._provider, recorder._task, recorder._root)
    _require(owner["recorder"] is recorder and recorder._journal is journal
             and journal.identity == recorder._identity and journal.state_root == recorder._state
             and journal.root == recorder._scratch and journal.paths == tuple(sorted(recorder._plan.frozen_paths))
             and journal.limits == recorder._plan.limits and not journal.symlinks,
             "产品恢复只接受原锁、原状态根与精确无链接路径全集")
    products._MAC_SOURCE.verify_product_qualification(recorder._task, recorder._root, recorder._provider, dispatch=live)
    return owner


def scope_context(recorder: Any, journal: Any, *, authorize: bool) -> dict[str, Any]:
    owner = _owner(recorder, journal, live=authorize)
    task = recorder._task
    result = {"schema_version": SCHEMA, "task_sha256": task["task_sha256"], "task_nonce": task["task_nonce"],
        "producer_run_id": task["execution"]["producer_run_id"], "recorder_id": c.MANAGED_RECORDER_ID,
        "deadline_utc": task["execution"]["request_policy"]["deadline_utc"],
        "source_deployments": task["execution"]["source_deployments"], "bundle_root": str(recorder._root),
        "bundle_instance": recorder._root_instance, "state_root": str(journal.state_root),
        "state_instance": recorder._state_instance, "paths": list(journal.paths),
        "path_mapping": owner["paths"].mapping_binding, "qualification_sha256": owner["qualification"]["raw_sha256"],
        "before_sha256": hashlib.sha256(journal._read_private(journal.root/"state-before.bin")).hexdigest(),
        "frozen_state_sha256": hashlib.sha256(journal._read_private(journal.root/"frozen-state.json")).hexdigest(),
        "baseline_sha256": c.digest(journal._known_operations())}
    if authorize:
        _require(recorder._ready and not recorder._instance and not any(row["event"] == "launch_intent"
            for row in recorder._rows()), "产品状态 scope 必须在真实监测 ready 后、任何应用派发前冻结")
        ready = recorder._provider._monitor.recheck()
        _require(ready.get("state_observation_schema") == SCHEMA
                 and ready.get("state_paths") == [str(journal.state_root/path) for path in journal.paths],
                 "真实 ES 监测未订阅本次精确私有状态全集")
        result["native_ready"] = ready
        result["authorized_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return copy.deepcopy(result)


def flatten(states: Mapping[str, Any], state_root: Path) -> dict[str, Any]:
    """展开准确树清单；不按目录前缀推测未观测子对象的归属。"""
    result = {}
    def visit(path: Path, node: Mapping[str, Any]) -> None:
        value = {key: item for key, item in node.items() if key not in {"children", "data_base64"}}
        result[str(path)] = value
        if node["type"] == "directory":
            for name, child in node["children"].items():
                visit(path/name, child)
    for relative, node in states.items():
        visit(Path(state_root)/relative, node)
    return result


def verify_scope_ack(scope: Mapping[str, Any], acknowledgement: Mapping[str, Any]) -> None:
    """确认必须是固定 native 的准确帧，且晚于已持久化的事前 scope。"""
    ack = native.validate_native_frame(dict(acknowledgement), {"task_sha256": scope["task_sha256"],
        "execution": {"producer_run_id": scope["producer_run_id"]}})
    _require(ack["kind"] == "product_scope" and ack["scope_sha256"] == c.digest(scope)
             and c.timestamp(ack["observed_at_utc"]) >= c.timestamp(scope["authorized_at_utc"]),
             "事前 scope 没有准确原生确认，不能事后补造")


def verify_changes(*, task: Mapping[str, Any], state_root: Path, paths: tuple[str, ...],
                   baseline: Mapping[str, Any], actual: Mapping[str, Any], frames: Any,
                   authorized_at_utc: str) -> None:
    """只读重放已验签流；本函数本身不授来源权、不修改状态或 WAL。"""
    old, new = flatten(baseline, state_root), flatten(actual, state_root)
    roots = [Path(state_root)/path for path in paths]
    snapshots = [row for row in frames if row["kind"] == "product_state_snapshot"]
    _require(len(snapshots) == 1 and snapshots[0]["states"] == new,
             "最终原生签名快照与独立实际状态全集不同")
    histories = native.owned_process_lifecycles(frames, task)
    _require(histories and all(row["end"] is not None for row in histories.values()), "产品未全部真实退出，不能恢复")
    snapshot_at = c.timestamp(snapshots[0]["observed_at_utc"])
    _require(all(c.timestamp(row["end"]["observed_at_utc"]) <= snapshot_at for row in histories.values())
             and all(c.timestamp(row["observed_at_utc"]) <= snapshot_at for row in frames
                     if row["kind"] == "product_state"), "最终状态快照早于真实退出或仍有后续写入")
    _require(all(c.timestamp(row["start"]["observed_at_utc"]) >= c.timestamp(authorized_at_utc)
                 for row in histories.values()), "产品执行早于持久化状态授权")
    proof = {path: value for path, value in old.items() if value["type"] == "file"}
    touched, created, removed, parents, mode_paths, time_paths = set(), set(), set(), set(), set(), set()
    unsupported = {"link", "setowner", "setflags", "setattrlist", "setacl", "setextattr", "deleteextattr",
                   "exchangedata", "clone", "copyfile"}
    for frame in frames:
        if frame["kind"] != "product_state":
            continue
        event, values = frame["event"], frame["paths"]
        token = tuple(frame["process"]["audit_token"])
        history = histories.get(token)
        _require(frame["owned"] is True and history is not None
                 and c.timestamp(history["start"]["observed_at_utc"]) <= c.timestamp(frame["observed_at_utc"])
                 <= c.timestamp(history["end"]["observed_at_utc"]), "私有状态含未知外部写者或超出真实执行生命周期的变化")
        _require(all(any(Path(value) == root or Path(value).is_relative_to(root) for root in roots) for value in values),
                 "产品状态变化跨越已授权精确根")
        _require(event not in unsupported, "产品状态含尚无准确恢复合同的操作")
        path = values[0]
        touched.update(values)
        if event in {"create", "unlink", "rename"}:
            parents.update(str(Path(value).parent) for value in values)
        if event == "create":
            created.add(path)
            proof.pop(path, None)
        elif event in {"write", "mmap", "truncate"}:
            proof.pop(path, None)
        elif event == "close":
            fact = frame["file"]
            _require(fact is not None and fact["type"] == "file" and fact["sha256"] is not None,
                     "最后一次产品关闭缺实际稳定字节原件")
            proof[path] = {key: fact[key] for key in ("type", "mode", "mtime_ns", "bytes", "sha256")}
        elif event == "unlink":
            removed.add(path)
            proof.pop(path, None)
        elif event == "rename":
            destination = values[1]
            _require(frame["file"] is not None and frame["file"]["type"] == "file", "目录改名没有逐对象恢复合同")
            removed.add(path)
            created.add(destination)
            previous = proof.pop(path, None)
            proof.pop(destination, None)
            if previous is not None:
                proof[destination] = previous
        elif event == "setmode":
            mode_paths.add(path)
            if path in proof:
                proof[path] = {**proof[path], "mode": frame["requested"]["mode"]}
        elif event == "utimes":
            time_paths.add(path)
            if path in proof:
                proof[path] = {**proof[path], "mtime_ns": frame["requested"]["mtime_ns"]}
        else:
            _require(False, "未知产品状态变化类型")
    for path in set(old) | set(new):
        before, after = old.get(path, {"type": "missing"}), new.get(path, {"type": "missing"})
        if before == after:
            continue
        _require(before["type"] != "symlink" and after["type"] != "symlink", "产品 symlink 变化没有准确恢复合同")
        if after["type"] == "missing":
            _require(path in removed, "消失对象缺准确 owned 删除原件")
        elif after["type"] == "file":
            _require(path in touched and proof.get(path) == after, "新增或变化文件缺最后准确的 owned CLOSE/元数据链")
        elif after["type"] == "directory":
            _require(before["type"] in {"missing", "directory"}, "对象类型变化没有目录恢复合同")
            _require(before["type"] != "missing" or path in created, "新增目录缺准确 owned CREATE 原件")
            _require(before.get("mode") == after["mode"] or path in created or path in mode_paths,
                     "目录权限变化没有准确 owned 来源")
            _require(before.get("mtime_ns") == after["mtime_ns"] or path in created or path in parents or path in time_paths,
                     "目录时间变化缺本目录或直接子项的 owned 原件")
        else:
            _require(False, "产品状态变化包含不受支持的对象")


def verify_observation(recorder: Any, journal: Any, scope: Mapping[str, Any], baseline: Mapping[str, Any],
                       actual: Mapping[str, Any]) -> dict[str, Any]:
    """重放时也实核原签名；哈希材料或当前 after 不能反向授 scope。"""
    owner = _owner(recorder, journal)
    context = scope_context(recorder, journal, authorize=False)
    _require({key: value for key, value in scope.items() if key not in {"native_ready", "authorized_at_utc"}} == context,
             "历史产品 scope 不属于原冻结任务、路径与安装资格")
    observed = origins.read_native_original(recorder._task, recorder._root, owner["native"])
    _require(observed["frames"][0] == scope["native_ready"], "原生签名 ready 与派发前 scope 不符")
    acks = [row for row in observed["frames"] if row["kind"] == "product_scope"]
    _require(len(acks) == 1, "事前 scope 必须有唯一原生签名确认")
    verify_scope_ack(scope, acks[0])
    durable_acks = [row["payload"] for row in journal._rows if row["event"] == "product_scope_ack"]
    _require(durable_acks == [{"scope_sha256": c.digest(scope), "native_ack": acks[0]}],
             "原恢复 WAL 的确认与已签原生 scope 不同")
    _require(all(c.timestamp(row["start"]["observed_at_utc"]) >= c.timestamp(acks[0]["observed_at_utc"])
        for row in observed["lifecycles"].values()), "应用执行早于准确原生 scope 确认")
    stop = [row["payload"] for row in recorder._rows() if row["event"] in {"stop_result", "recovery_owned_result"}]
    _require(stop and all(row["facts"].get("stopped") is True and row["completeness"]["status"] == "complete"
        for row in stop), "没有真实产品停止结果，不能恢复动态写入")
    verify_changes(task=recorder._task, state_root=journal.state_root, paths=journal.paths,
        baseline=baseline, actual=actual, frames=observed["frames"], authorized_at_utc=scope["authorized_at_utc"])
    return {"native": c.binding(recorder._root/"native-events.jsonl", recorder._root),
            "scope_sha256": c.digest(scope), "actual_sha256": c.digest(actual)}


def verify_archived_restore(task: Mapping[str, Any], bundle_root: Path, observed: Mapping[str, Any], *, path_context: Any) -> None:
    """ARM64 只读消费复制的恢复原件；不要求 Mac 活动锁或可注入恢复 callback。"""
    from tools.official_client_capture import claude_mac_product_authority as authority
    from tools.official_client_capture import claude_mac_product_recipe as recipes
    from tools.official_client_capture.claude_mac_state_journal import StateJournal, SnapshotLimits
    root = Path(bundle_root)
    state = root/"state-originals"
    expected_identity = {"task_sha256": task["task_sha256"], "task_nonce": task["task_nonce"],
        "producer_run_id": task["execution"]["producer_run_id"],
        "deadline_utc": task["execution"]["request_policy"]["deadline_utc"]}
    frozen_raw = authority.read_original(state/"frozen-state.json")
    before_raw = authority.read_original(state/"state-before.bin")
    after_raw = authority.read_original(state/"state-after.bin")
    frozen, before, after = json.loads(frozen_raw), json.loads(before_raw), json.loads(after_raw)
    _require(set(frozen) == {"schema_version", "identity", "state_root", "root_instance", "paths", "symlink_targets", "limits"}
        and frozen["schema_version"] == "claude-private-state-journal/v1" and frozen["identity"] == expected_identity
        and frozen["symlink_targets"] == {} and set(before) == set(after) == {"schema_version", "identity", "states"}
        and before["schema_version"] == after["schema_version"] == frozen["schema_version"]
        and before["identity"] == after["identity"] == expected_identity
        and before["states"] == after["states"] and set(before["states"]) == set(frozen["paths"]),
        "复制状态 before/after、原根、身份或精确清单不闭合")
    _require(path_context is not None, "恢复缺原认证 Mac 映射")
    recipe = c.read_json(c.bound_file(root, task["execution"]["isolation_recipe"]))
    state_root = recipes.material_state_root(task, recipe, root, path_context)
    state_paths = recipes.material_state_paths(task, recipe, root, path_context)
    _require(frozen["state_root"] == str(state_root)
        and frozen["paths"] == sorted(path.relative_to(state_root).as_posix() for path in state_paths),
        "恢复原根或准确状态全集不是原认证 Mac 物理映射")
    limits = SnapshotLimits(**frozen["limits"])
    limits.validate()
    _require(set(frozen["limits"]) == set(limits.__dataclass_fields__), "复制状态上限须精确冻结，不能补默认值")
    bounded = StateJournal(expected_identity, root/"unused-readonly-journal", Path(frozen["state_root"]),
                           frozen["paths"], limits=limits)
    def validate_states(values: Mapping[str, Any]) -> None:
        _require(isinstance(values, dict) and set(values) == set(frozen["paths"]), "复制状态路径全集漂移")
        budget = {"entries": 0, "bytes": 0}
        for path, node in values.items():
            bounded._validate_node(node, path, len(Path(path).parts), budget)
    validate_states(before["states"])
    def material(reference: Mapping[str, Any]) -> Any:
        _require(set(reference) == {"path", "sha256", "bytes"} and reference["path"] == "materials/"+reference["sha256"]+".json",
                 "复制材料必须是准确的不可变内容寻址原件")
        raw = authority.read_original(state/reference["path"], maximum=limits.max_wal_bytes)
        _require(hashlib.sha256(raw).hexdigest() == reference["sha256"] and len(raw) == reference["bytes"], "复制状态材料被篡改")
        return json.loads(raw)
    snapshots = []
    for path in sorted(state.glob("journal-*.jsonl")):
        raw = authority.read_original(path, maximum=limits.max_wal_bytes)
        _require(path.name == "journal-"+hashlib.sha256(raw).hexdigest()+".jsonl" and raw.endswith(b"\n"),
                 "复制 WAL 快照不是准确完整内容寻址原件")
        snapshots.append(raw)
    _require(snapshots, "没有原恢复 WAL")
    latest = max(snapshots, key=len)
    _require(all(latest.startswith(raw) for raw in snapshots), "恢复 WAL 出现历史分叉或改写")
    rows = []
    for line in latest.splitlines():
        row = json.loads(line)
        _require(set(row) == {"sequence", "previous_sha256", "identity", "event", "payload", "observed_at_utc", "sha256"}
            and row["identity"] == expected_identity and row["sequence"] == len(rows)+1
            and row["previous_sha256"] == (rows[-1]["sha256"] if rows else "0"*64)
            and row["sha256"] == c.digest({key: value for key, value in row.items() if key != "sha256"}), "复制恢复 WAL 断链")
        c.timestamp(row["observed_at_utc"])
        _require(not rows or c.timestamp(row["observed_at_utc"]) >= c.timestamp(rows[-1]["observed_at_utc"]),
                 "复制恢复 WAL 时间倒置")
        rows.append(row)
    permitted = {"prepared", "operation_intent", "operation_observed", "replacement_intent", "replacement_complete",
                 "product_scope_intent", "product_scope_ack", "product_state_observed", "restored_path",
                 "after_read_intent", "after_observed", "restored"}
    _require(all(row["event"] in permitted for row in rows)
        and rows[0]["event"] == "prepared" and any(row["event"] == "restored" for row in rows), "恢复仍有未知或没有真实完成原件")
    unique_events = ("prepared", "product_scope_intent", "product_scope_ack", "product_state_observed",
                     "after_read_intent", "after_observed", "restored")
    event_positions = {name: [index for index, row in enumerate(rows) if row["event"] == name] for name in unique_events}
    _require(all(len(indices) == 1 for indices in event_positions.values())
             and [event_positions[name][0] for name in unique_events] ==
                 sorted(event_positions[name][0] for name in unique_events), "恢复事前授权、观察与独立 after 阶段不闭合")
    prepared = rows[0]["payload"]["before"]
    _require(prepared == {"path": "state-before.bin", "sha256": hashlib.sha256(before_raw).hexdigest(), "bytes": len(before_raw)},
             "首次 before 绑定漂移")
    baseline = copy.deepcopy(before["states"])
    operations, completed_operations, replacements, completed_replacements, restored_paths = {}, set(), {}, set(), set()
    for row in rows:
        payload = row["payload"]
        if row["event"] in {"operation_intent", "operation_observed"}:
            _require(row["sequence"] < rows[event_positions["product_scope_intent"][0]]["sequence"],
                     "产品 scope 冻结后仍出现受管设置")
        if row["event"] == "operation_intent":
            _require(set(payload) == {"operation_id", "before", "after"} and payload["operation_id"] not in operations,
                     "受管设置意图重复或字段非法")
            expected_before, desired = material(payload["before"]), material(payload["after"])
            _require(isinstance(desired, dict) and desired and set(desired) <= set(baseline)
                     and expected_before == {path: baseline[path] for path in desired}, "受管设置不是准确事前状态")
            operations[payload["operation_id"]] = payload
        elif row["event"] == "operation_observed":
            _require(set(payload) == {"operation_id", "actual"} and payload["operation_id"] not in completed_operations,
                     "受管设置观察重复或字段非法")
            operation = operations.get(row["payload"]["operation_id"])
            _require(operation is not None, "观察结果没有事前操作意图")
            actual = material(row["payload"]["actual"])
            _require(actual == material(operation["after"]), "受管设置实际结果与事前意图不同")
            baseline.update(actual)
            validate_states(baseline)
            completed_operations.add(payload["operation_id"])
        elif row["event"] == "replacement_intent":
            _require(set(payload) == {"replacement_id", "path", "before", "after", "phase", "transaction"}
                     and payload["replacement_id"] not in replacements and payload["path"] in frozen["paths"]
                     and payload["phase"] in {"change", "restore"}, "状态替换意图重复或越界")
            _require((payload["phase"] == "change" and row["sequence"] < rows[event_positions["product_scope_intent"][0]]["sequence"])
                     or (payload["phase"] == "restore" and row["sequence"] > rows[event_positions["product_state_observed"][0]]["sequence"]),
                     "状态替换与事前设置／恢复阶段顺序不同")
            material(payload["before"])
            material(payload["after"])
            replacements[payload["replacement_id"]] = payload
        elif row["event"] == "replacement_complete":
            _require(set(payload) == {"replacement_id"} and payload["replacement_id"] in replacements
                     and payload["replacement_id"] not in completed_replacements, "状态替换完成缺唯一原意图")
            completed_replacements.add(payload["replacement_id"])
        elif row["event"] == "restored_path":
            _require(set(payload) == {"path"} and payload["path"] in frozen["paths"] and payload["path"] not in restored_paths
                     and row["sequence"] > rows[event_positions["product_state_observed"][0]]["sequence"], "路径恢复不闭合")
            restored_paths.add(payload["path"])
    _require(set(operations) == completed_operations and set(replacements) == completed_replacements
             and restored_paths == set(frozen["paths"]), "恢复仍有未完成设置、原子替换或路径")
    scope_rows = [row["payload"] for row in rows if row["event"] == "product_scope_intent"]
    actual_rows = [row["payload"] for row in rows if row["event"] == "product_state_observed"]
    _require(len(scope_rows) == len(actual_rows) == 1, "没有唯一事前产品 scope 和真实观察材料")
    scope = material(scope_rows[0]["scope"])
    actual = material(actual_rows[0]["actual"])
    validate_states(actual)
    scope_fields = {"schema_version", "task_sha256", "task_nonce", "producer_run_id", "recorder_id", "deadline_utc",
        "source_deployments", "bundle_root", "bundle_instance", "state_root", "state_instance", "paths", "path_mapping",
        "qualification_sha256", "before_sha256", "frozen_state_sha256", "baseline_sha256", "native_ready", "authorized_at_utc"}
    _require(set(scope) == scope_fields and scope["schema_version"] == SCHEMA
        and isinstance(scope["qualification_sha256"], str) and bool(c.SHA_RE.fullmatch(scope["qualification_sha256"]))
        and scope["bundle_root"] == str(path_context.staging_bundle_root)
        and material(scope_rows[0]["baseline"]) == baseline and scope["baseline_sha256"] == c.digest(baseline)
        and scope["before_sha256"] == hashlib.sha256(before_raw).hexdigest()
        and scope["frozen_state_sha256"] == hashlib.sha256(frozen_raw).hexdigest()
        and scope["task_sha256"] == task["task_sha256"] and scope["task_nonce"] == task["task_nonce"]
        and scope["producer_run_id"] == task["execution"]["producer_run_id"]
        and scope["recorder_id"] == task["execution"]["recorder_id"]
        and scope["deadline_utc"] == expected_identity["deadline_utc"] and scope["state_root"] == frozen["state_root"]
        and scope["state_instance"] == frozen["root_instance"] and scope["paths"] == frozen["paths"]
        and scope["path_mapping"] == path_context.mapping_binding
        and scope["source_deployments"] == task["execution"]["source_deployments"]
        and scope["native_ready"] == observed["frames"][0], "准确产品 scope 与签名、原 before 或认证桥不同")
    acknowledgements = [row for row in observed["frames"] if row["kind"] == "product_scope"]
    _require(len(acknowledgements) == 1, "产品 scope 必须有唯一原生签名确认")
    verify_scope_ack(scope, acknowledgements[0])
    ack_payload = rows[event_positions["product_scope_ack"][0]]["payload"]
    _require(ack_payload == {"scope_sha256": c.digest(scope), "native_ack": acknowledgements[0]}
             and c.timestamp(scope["authorized_at_utc"]) >= c.timestamp(scope["native_ready"]["observed_at_utc"])
             and scope["native_ready"].get("state_observation_schema") == SCHEMA
             and scope["native_ready"].get("state_paths") == [str(Path(frozen["state_root"])/path) for path in frozen["paths"]],
             "准确 scope 确认、ready 与状态订阅不闭合")
    verify_changes(task=task, state_root=Path(frozen["state_root"]), paths=tuple(frozen["paths"]),
        baseline=baseline, actual=actual, frames=observed["frames"], authorized_at_utc=acknowledgements[0]["observed_at_utc"])
    _require(actual_rows[0]["proof"] == {"native": c.binding(root/"native-events.jsonl", root),
        "scope_sha256": c.digest(scope), "actual_sha256": c.digest(actual)}, "恢复来源证明没有命中准确已签原件")
    _require(authority.read_original(root/"state-before.bin") == before_raw
        and authority.read_original(root/"state-after.bin") == after_raw, "桥复制状态与原 journal 原件不同")
    after_binding = {"path": "state-after.bin", "sha256": hashlib.sha256(after_raw).hexdigest(), "bytes": len(after_raw)}
    _require(rows[event_positions["after_read_intent"][0]]["payload"] == {"origin": "independent_filesystem_read"}
             and rows[event_positions["after_observed"][0]]["payload"] == {"after": after_binding, "origin": "independent_filesystem_read"}
             and rows[event_positions["restored"][0]]["payload"] == {"after": after_binding}, "独立 after 与真实恢复完成原件不同")
