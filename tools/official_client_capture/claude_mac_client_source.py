"""Mac v2 来源合同：制品、宿主、模块和独立服务端原件分别验证。

本模块不启动产品、不读取凭据、不连接远端。来源可信性由受管静态登记的适配器
验证；上传包中的摘要只用于完整性，不能代替受管部署和真实观测证明。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c

TASK_SCHEMA = "claude-upgrade-client-task/v2"
BUNDLE_SCHEMA = "claude-upgrade-client-source-manifest/v2"
CAPTURE_SCHEMA = "claude-upgrade-client-server-capture/v2"
RESERVATION_SCHEMA = "claude-upgrade-client-reservation/v2"
READY_SCHEMA = "claude-upgrade-client-capture-ready/v2"
RUN_SCHEMA = "claude-upgrade-client-imported-run/v2"
ARTIFACT_KINDS = frozenset({"app_bundle", "host_executable", "embedded_cli", "extension_package", "extension_entry"})
DEPLOYMENT_FIELDS = frozenset({"deployment_id", "tool_identity_sha256"})
EXECUTION_FIELDS = frozenset({"endpoint", "credential_ref", "timeout_seconds", "request_budget", "recorder_id", "capture_id", "producer_run_id", "source_scope", "capture_root", "project_ledger", "source_deployments", "isolation_recipe", "request_policy"})


def _fields(value: Any, names: set[str] | frozenset[str], label: str) -> dict[str, Any]:
    c.require(isinstance(value, dict) and set(value) == names, f"{label} 字段闭集非法")
    return value


def _id(value: Any, label: str) -> None:
    c.require(isinstance(value, str) and bool(c.ID_RE.fullmatch(value)), f"{label} 身份非法")


def _sha(value: Any, label: str) -> None:
    c.require(isinstance(value, str) and bool(c.SHA_RE.fullmatch(value)), f"{label} 摘要非法")


def _file_binding(value: Any, label: str) -> None:
    _fields(value, {"path", "sha256", "bytes"}, label)
    _sha(value["sha256"], label)
    c.require(type(value["bytes"]) is int and value["bytes"] >= 0 and bool(value["path"]), f"{label} 路径或字节数非法")


def validate_task_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    """冻结产品版本、内置 CLI 版本和执行预算；不把离线 prepare 当 live 预约。"""
    task = _fields(dict(document), c.TASK_FIELDS, "v2 任务")
    c.require(task["schema_version"] == TASK_SCHEMA and task["client_id"] in c.CLIENTS, "当前任务必须为完整 v2")
    for key in (*c.COORDINATES, "task_nonce"):
        _id(task[key], key)
    c.require(type(task["candidate_revision"]) is int and task["candidate_revision"] > 0, "候选 revision 非法")
    c.require(task["task_sha256"] == c.digest({k: v for k, v in task.items() if k != "task_sha256"}), "任务摘要不符")
    c.timestamp(task["issued_at_utc"])
    candidate = _fields(task["candidate"], {"source_tree_sha256", "build_receipt_sha256", "image_digest", "profile_digest", "gateway_sha256"}, "候选身份")
    for value in candidate.values():
        _sha(str(value).removeprefix("sha256:"), "候选身份")
    catalog = _fields(task["catalog"], {"sha256", "entry"}, "Catalog")
    _sha(catalog["sha256"], "Catalog")
    entry = catalog["entry"]
    product = "claude-desktop" if task["client_id"] == "claude-desktop" else "claude-code-vscode"
    c.require(isinstance(entry, dict) and all(entry.get(k) for k in ("client_id", "product", "version", "platform", "binary_sha256")), "Catalog 独立客户端条目缺失")
    c.require(entry["product"] == product and str(entry["platform"]).startswith("darwin/"), "Catalog 客户端产品或平台不符")
    _sha(entry["binary_sha256"], "Catalog 内置 CLI")
    artifacts = task["client_artifacts"]
    c.require(isinstance(artifacts, list) and bool(artifacts), "未冻结已审核的客户端制品")
    identities, roles = set(), set()
    for artifact in artifacts:
        _fields(artifact, {"artifact_id", "kind", "product", "role", "version", "platform", "content", "official_source"}, "制品")
        _id(artifact["artifact_id"], "制品")
        c.require(artifact["artifact_id"] not in identities and artifact["role"] not in roles, "制品 ID 或角色重复")
        identities.add(artifact["artifact_id"])
        roles.add(artifact["role"])
        c.require(artifact["kind"] in ARTIFACT_KINDS and artifact["product"] == product and artifact["platform"] == entry["platform"], "制品类型、产品或平台非法")
        version = _fields(artifact["version"], {"kind", "value"}, "独立版本")
        c.require(version["kind"] in {"product", "embedded_cli", "extension_package", "host"} and isinstance(version["value"], str) and bool(version["value"]), "独立版本非法")
        content = _fields(artifact["content"], {"kind", "path", "sha256", "bytes"}, "制品内容")
        _sha(content["sha256"], "制品内容")
        expected_kind = "directory" if artifact["kind"] in {"app_bundle", "extension_package"} else "file"
        c.require(content["kind"] == expected_kind and Path(str(content["path"])).is_absolute() and ".." not in Path(str(content["path"])).parts and type(content["bytes"]) is int and content["bytes"] >= 0, "制品目录树与可执行文件绑定混淆")
        _file_binding(artifact["official_source"], "官方来源")
    required = {"application", "application_executable", "embedded_cli"}
    if task["client_id"] == "claude-vscode":
        required |= {"extension_host", "extension_package", "extension_entry"}
    c.require(roles == required, "应用包、宿主、扩展 package／入口和内置 CLI 必须分别冻结")
    by_role = {row["role"]: row for row in artifacts}
    kinds = {"application": "app_bundle", "application_executable": "host_executable", "extension_host": "host_executable", "extension_package": "extension_package", "extension_entry": "extension_entry", "embedded_cli": "embedded_cli"}
    c.require(all(by_role[role]["kind"] == kinds[role] for role in required), "制品角色与类型不符")
    embedded = by_role["embedded_cli"]
    c.require(embedded["version"] == {"kind": "embedded_cli", "value": entry["version"]} and embedded["content"]["sha256"] == entry["binary_sha256"], "内置 CLI 与候选实际 Catalog 不一致")
    c.require(by_role["application"]["version"]["kind"] == "product", "应用版本不能当作内置 CLI 版本")
    if task["client_id"] == "claude-vscode":
        c.require(by_role["extension_package"]["version"]["kind"] == "extension_package", "扩展 package 版本类型不符")
    c.require(isinstance(task["scenario_reuse"], list), "场景承接清单必须为数组")
    c.require(isinstance(task["scenarios"], list) and bool(task["scenarios"]), "没有冻结客户端场景")
    seen, challenges = set(), set()
    for row in task["scenarios"]:
        _fields(row, {"id", "model", "challenge", "response_marker", "minimum_requests"}, "场景")
        _id(row["id"], "场景")
        c.require(row["id"] not in seen and row["challenge"] not in challenges and isinstance(row["model"], str) and bool(row["model"]) and isinstance(row["challenge"], str) and len(row["challenge"]) >= 16 and bool(row["response_marker"]), "场景身份或挑战非法")
        c.require(type(row["minimum_requests"]) is int and row["minimum_requests"] > 0, "场景请求下限非法")
        seen.add(row["id"])
        challenges.add(row["challenge"])
    execution = _fields(task["execution"], EXECUTION_FIELDS, "执行冻结")
    c.require(execution["source_scope"] in {"formal", "regression_fixture"}, "来源用途非法")
    c.require(isinstance(execution["endpoint"], str) and execution["endpoint"].startswith(("http://", "https://")) and "@" not in execution["endpoint"], "候选 endpoint 非法")
    c.require(isinstance(execution["credential_ref"], str) and execution["credential_ref"].startswith("private-ref:") and "\n" not in execution["credential_ref"], "仅接受私有凭据引用")
    for key in ("timeout_seconds", "request_budget"):
        c.require(type(execution[key]) is int and execution[key] > 0, "超时或预算非法")
    for key in ("recorder_id", "capture_id", "producer_run_id"):
        _id(execution[key], key)
    for key in ("capture_root", "project_ledger"):
        c.require(Path(str(execution[key])).is_absolute() and ".." not in Path(str(execution[key])).parts, f"{key} 必须提前冻结绝对路径")
    deployments = _fields(execution["source_deployments"], {"mac", "capture"}, "两端受管部署")
    for value in deployments.values():
        _fields(value, DEPLOYMENT_FIELDS, "受管部署")
        _id(value["deployment_id"], "受管部署")
        _sha(value["tool_identity_sha256"], "受管工具")
    _file_binding(execution["isolation_recipe"], "产品隔离方法原件")
    policy = _fields(execution["request_policy"], {"methods", "model_paths", "retry_upper_bound", "max_concurrency", "deadline_utc"}, "请求边界")
    c.require(policy["methods"] == ["POST"] and policy["model_paths"] == sorted(c.MODEL_ENDPOINTS), "模型请求边界未冻结")
    c.require(type(policy["retry_upper_bound"]) is int and policy["retry_upper_bound"] >= 0 and type(policy["max_concurrency"]) is int and policy["max_concurrency"] > 0, "请求并发或重试上界非法")
    c.require(c.timestamp(policy["deadline_utc"]) > c.timestamp(task["issued_at_utc"]), "请求 deadline 非法")
    return task


def process_instance_id(pid: int, boot_id: str, start_marker: str) -> str:
    """PID 仅在同一 boot/start 内标识同一进程，不能以 PID 单值承接。"""
    return c.digest({"pid": pid, "boot_id": boot_id, "start_marker": start_marker})


def _json_lines(path: Path, label: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]
    except (ValueError, UnicodeError) as error:
        raise c.ConfigurationError(f"Mac 客户端证据：{label} 原件非法") from error
    c.require(all(isinstance(row, dict) for row in rows), f"{label} 逐条原件必须是对象")
    return rows


def _observation(root: Path, reference: Any, expected: Mapping[str, Any], label: str) -> None:
    value = c.read_json(c.bound_file(root, reference))
    c.require(value == dict(expected), f"{label} 独立观测与摘要清单不符")


def _participation(task: Mapping[str, Any], root: Path, files: Mapping[str, Path]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    identity = c.read_json(files["client-identity.json"])
    c.require(identity == {"client_id": task["client_id"], "artifacts": task["client_artifacts"]}, "客户端制品、版本或产品身份不符")
    artifacts = {row["artifact_id"]: row for row in task["client_artifacts"]}
    for artifact in artifacts.values():
        origin = c.read_json(c.bound_file(root, artifact["official_source"]))
        c.require(origin.get("artifact_id") == artifact["artifact_id"] and origin.get("content") == artifact["content"] and origin.get("version") == artifact["version"], "官方制品来源未绑定冻结内容与独立版本")
    session = _fields(c.read_json(files["process-session.json"]), {"client_id", "session_id", "started_at_utc", "completed_at_utc", "processes", "loaded_modules"}, "产品进程会话")
    c.require(session["client_id"] == task["client_id"], "产品进程会话来源不符")
    _id(session["session_id"], "会话")
    start, end = c.timestamp(session["started_at_utc"]), c.timestamp(session["completed_at_utc"])
    c.require(c.timestamp(task["issued_at_utc"]) <= start <= end and (end-start).total_seconds() <= task["execution"]["timeout_seconds"] and end <= c.timestamp(task["execution"]["request_policy"]["deadline_utc"]), "会话运行窗口非法")
    c.require(isinstance(session["processes"], list) and bool(session["processes"]), "没有应用进程来源，CLI headers 不能代替产品参与")
    processes, live_pids = {}, {}
    for item in session["processes"]:
        _fields(item, {"process_instance_id", "pid", "boot_id", "start_marker", "parent_instance_id", "executable_artifact_id", "executable_sha256", "role", "session_id", "started_at_utc", "exited_at_utc", "observation"}, "进程实例")
        c.require(type(item["pid"]) is int and item["pid"] > 0, "PID 非法")
        _id(item["boot_id"], "boot")
        _id(item["start_marker"], "start")
        instance = process_instance_id(item["pid"], item["boot_id"], item["start_marker"])
        c.require(item["process_instance_id"] == instance and instance not in processes, "PID 的 boot/start 实例不符或重复")
        c.require(item["session_id"] == session["session_id"] and item["role"] in {"application", "extension_host", "embedded_cli"}, "进程角色或会话不符")
        a = artifacts.get(item["executable_artifact_id"])
        c.require(a is not None and a["kind"] in {"host_executable", "embedded_cli"} and a["content"]["sha256"] == item["executable_sha256"], "应用包／模块不能代替宿主 executable")
        role = "application_executable" if item["role"] == "application" else item["role"]
        c.require(a["role"] == role, "进程与冻结 executable 角色不符")
        pstart, pend = c.timestamp(item["started_at_utc"]), c.timestamp(item["exited_at_utc"])
        c.require(pstart <= pend <= end and pend >= start and (item["role"] != "application" or pstart <= start), "进程窗口不在会话内或未收口")
        key = (item["boot_id"], item["pid"])
        for old_start, old_end in live_pids.get(key, []):
            c.require(pend <= old_start or old_end <= pstart, "同 PID 的不同启动实例窗口重叠")
        live_pids.setdefault(key, []).append((pstart, pend))
        parent = item["parent_instance_id"]
        c.require(parent is None and item["role"] == "application" or parent in processes, "进程父实例链不闭合")
        if parent is not None:
            p = processes[parent]
            c.require(p["boot_id"] == item["boot_id"] and c.timestamp(p["started_at_utc"]) <= pstart and pend <= c.timestamp(p["exited_at_utc"]), "父子进程生命周期不闭合")
        _observation(root, item["observation"], {k: v for k, v in item.items() if k != "observation"}, "进程")
        processes[instance] = item
    c.require(sum(row["role"] == "application" for row in processes.values()) == 1, "必须有唯一真实应用宿主")
    c.require(task["client_id"] != "claude-vscode" or any(row["role"] == "extension_host" for row in processes.values()), "缺少 VS Code extension host")
    c.require(isinstance(session["loaded_modules"], list), "加载模块必须是数组")
    modules = {}
    for item in session["loaded_modules"]:
        _fields(item, {"module_instance_id", "artifact_id", "host_process_instance_id", "entrypoint", "loaded_at_utc", "activated_at_utc", "observation"}, "真实加载模块")
        _id(item["module_instance_id"], "加载模块")
        a, p = artifacts.get(item["artifact_id"]), processes.get(item["host_process_instance_id"])
        c.require(item["module_instance_id"] not in modules and a is not None and a["kind"] in {"extension_entry", "embedded_cli"} and p is not None, "模块缺宿主、重复或把 package 当入口")
        c.require(item["entrypoint"] == a["content"]["path"], "真实 entrypoint 与冻结制品不符")
        loaded, active = c.timestamp(item["loaded_at_utc"]), c.timestamp(item["activated_at_utc"])
        c.require(c.timestamp(p["started_at_utc"]) <= loaded <= active <= c.timestamp(p["exited_at_utc"]), "模块没有在宿主窗口加载并激活")
        if a["kind"] == "extension_entry":
            c.require(p["role"] == "extension_host", "扩展必须实际加载进 extension host")
        _observation(root, item["observation"], {k: v for k, v in item.items() if k != "observation"}, "模块加载")
        modules[item["module_instance_id"]] = item
    roles = {artifacts[row["artifact_id"]]["role"] for row in modules.values()} | {row["role"] for row in processes.values()}
    c.require("embedded_cli" in roles and (task["client_id"] != "claude-vscode" or "extension_entry" in roles), "缺少真实内置 CLI／扩展加载与参与")
    return session, processes, modules


def verify_reservation_body_v2(task: Mapping[str, Any], capture_root: Path) -> dict[str, Any]:
    """先验预约本体，允许真实捕获服务尚未发布 ready；来源及预算另经受管适配器核验。"""
    task = validate_task_v2(task)
    root = Path(capture_root)
    c.require(root == Path(task["execution"]["capture_root"]), "捕获预约根与任务冻结根不符")
    reservation = _fields(c.read_json(c.secure_file(root, "reservation.json")), {"schema_version", "task_sha256", "candidate", "capture_id", "producer_run_id", "endpoint", "request_budget", "reserved_at_utc", "deadline_utc", "budget_reservation", "source_deployments"}, "独立捕获预约")
    c.require(reservation["schema_version"] == RESERVATION_SCHEMA and reservation["task_sha256"] == task["task_sha256"] and reservation["candidate"] == task["candidate"] and reservation["source_deployments"] == task["execution"]["source_deployments"], "独立捕获预约未绑定当前任务、候选和部署")
    for key in ("capture_id", "producer_run_id", "endpoint", "request_budget"):
        c.require(reservation[key] == task["execution"][key], f"独立捕获预约 {key} 不符")
    c.require(reservation["deadline_utc"] == task["execution"]["request_policy"]["deadline_utc"] and c.timestamp(task["issued_at_utc"]) <= c.timestamp(reservation["reserved_at_utc"]) < c.timestamp(reservation["deadline_utc"]), "预约时间边界非法")
    c.bound_file(root, reservation["budget_reservation"])
    return reservation


def verify_reservation_v2(task: Mapping[str, Any], capture_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """派发与成功消费仍要求独立 ready；预约本体校验不能代替真实就绪原件。"""
    reservation = verify_reservation_body_v2(task, capture_root)
    root = Path(capture_root)
    ready = _fields(c.read_json(c.secure_file(root, "ready.json")), {"schema_version", "task_sha256", "capture_id", "reservation", "deployment", "ready_at_utc", "observation"}, "独立捕获 ready")
    c.require(ready["schema_version"] == READY_SCHEMA and ready["task_sha256"] == task["task_sha256"] and ready["capture_id"] == reservation["capture_id"] and ready["deployment"] == task["execution"]["source_deployments"]["capture"], "独立 ready 来源或任务不符")
    c.require(c.bound_file(root, ready["reservation"]).name == "reservation.json", "ready 未绑定准确预约原件")
    c.require(c.timestamp(reservation["reserved_at_utc"]) <= c.timestamp(ready["ready_at_utc"]) < c.timestamp(reservation["deadline_utc"]), "ready 时间非法")
    _observation(root, ready["observation"], {k: v for k, v in ready.items() if k != "observation"}, "独立捕获 ready")
    return reservation, ready


def _request_links(task: Mapping[str, Any], root: Path, capture_root: Path, files: Mapping[str, Path], session: Mapping[str, Any], processes: Mapping[str, Any], modules: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    capture = _fields(c.read_json(c.secure_file(capture_root, "capture-manifest.json")), {"schema_version", "task_sha256", "capture_id", "producer_run_id", "candidate", "endpoint", "status", "requests", "connections", "transport_map", "reservation", "ready", "source_origin"}, "独立捕获清单")
    c.require(capture["schema_version"] == CAPTURE_SCHEMA, "当前任务不能消费旧 server capture")
    c.require(capture["task_sha256"] == task["task_sha256"] and capture["candidate"] == task["candidate"], "独立捕获未绑定本轮任务与候选")
    for key in ("capture_id", "producer_run_id", "endpoint"):
        c.require(capture[key] == task["execution"][key], f"独立捕获 {key} 不符")
    c.require(capture["status"] in {"complete", "failed", "truncated"}, "独立捕获终态缺失")
    reservation, ready = verify_reservation_v2(task, capture_root)
    c.require(c.bound_file(capture_root, capture["reservation"]).name == "reservation.json" and c.bound_file(capture_root, capture["ready"]).name == "ready.json", "捕获清单没有绑定独立预约／ready")
    c.require(c.timestamp(ready["ready_at_utc"]) <= c.timestamp(session["started_at_utc"]), "独立 ready 晚于产品启动窗口")
    connections = {}
    for row in _json_lines(c.bound_file(capture_root, capture["connections"]), "服务端连接"):
        _fields(row, {"connection_id", "client_endpoint", "server_endpoint", "opened_at_utc", "closed_at_utc", "observation"}, "服务端原生连接")
        _id(row["connection_id"], "服务端原生连接")
        c.require(row["connection_id"] not in connections and c.timestamp(ready["ready_at_utc"]) <= c.timestamp(row["opened_at_utc"]) <= c.timestamp(row["closed_at_utc"]) <= c.timestamp(session["completed_at_utc"]), "服务端连接重复或运行窗口非法")
        _observation(capture_root, row["observation"], {k: v for k, v in row.items() if k != "observation"}, "服务端连接")
        connections[row["connection_id"]] = row
    calls, ordinal_links, call_roles = {}, {}, {}
    for row in _json_lines(files["socket-calls.jsonl"], "本机 socket／调用"):
        _fields(row, {"socket_call_id", "process_instance_id", "module_instance_ids", "session_id", "scenario_id", "client_endpoint", "server_endpoint", "opened_at_utc", "closed_at_utc", "request_ordinals", "observation"}, "本机 socket／调用")
        _id(row["socket_call_id"], "socket 调用")
        process = processes.get(row["process_instance_id"])
        c.require(row["socket_call_id"] not in calls and process is not None and row["session_id"] == session["session_id"], "socket 调用未绑定准确进程实例")
        opened, closed = c.timestamp(row["opened_at_utc"]), c.timestamp(row["closed_at_utc"])
        c.require(c.timestamp(process["started_at_utc"]) <= opened <= closed <= c.timestamp(process["exited_at_utc"]), "socket 调用窗口与进程启动实例不符")
        ancestors, at = set(), row["process_instance_id"]
        while at is not None:
            ancestors.add(at)
            at = processes[at]["parent_instance_id"]
        c.require(isinstance(row["module_instance_ids"], list) and len(set(row["module_instance_ids"])) == len(row["module_instance_ids"]), "socket 模块调用重复或非法")
        artifact_roles = {a["artifact_id"]: a["role"] for a in task["client_artifacts"]}
        invoked_roles = {process["role"]}
        for module_id in row["module_instance_ids"]:
            module = modules.get(module_id)
            c.require(module is not None and module["host_process_instance_id"] in ancestors and c.timestamp(module["activated_at_utc"]) <= opened, "请求没有真实模块调用／激活关联")
            invoked_roles.add(artifact_roles[module["artifact_id"]])
        c.require(row["scenario_id"] is None or row["scenario_id"] in {s["id"] for s in task["scenarios"]}, "socket 场景不在任务闭集")
        c.require(isinstance(row["request_ordinals"], list) and bool(row["request_ordinals"]) and len(set(row["request_ordinals"])) == len(row["request_ordinals"]) and all(type(n) is int and n > 0 for n in row["request_ordinals"]), "socket 原生 ordinal 闭集非法")
        _observation(root, row["observation"], {k: v for k, v in row.items() if k != "observation"}, "本机 socket／调用")
        calls[row["socket_call_id"]] = row
        call_roles[row["socket_call_id"]] = invoked_roles
    mapped = set()
    for row in _json_lines(c.bound_file(capture_root, capture["transport_map"]), "代理／NAT 映射"):
        _fields(row, {"socket_call_id", "connection_id", "hops", "observed_at_utc", "observation"}, "联合连接映射")
        call, connection = calls.get(row["socket_call_id"]), connections.get(row["connection_id"])
        c.require(call is not None and connection is not None and row["socket_call_id"] not in mapped, "联合映射缺本机／独立原件或重复")
        c.require(isinstance(row["hops"], list), "代理／NAT hops 必须为数组")
        flow = {k: call[k] for k in ("client_endpoint", "server_endpoint")}
        for hop in row["hops"]:
            _fields(hop, {"before", "after"}, "代理／NAT hop")
            _fields(hop["before"], {"client_endpoint", "server_endpoint"}, "映射前连接")
            _fields(hop["after"], {"client_endpoint", "server_endpoint"}, "映射后连接")
            c.require(hop["before"] == flow, "代理／NAT 映射链有缺口")
            flow = hop["after"]
        c.require(flow == {k: connection[k] for k in flow}, "本机与服务端连接不能闭合，不能伪加客户端 header")
        c.require(c.timestamp(call["opened_at_utc"]) <= c.timestamp(row["observed_at_utc"]) <= c.timestamp(call["closed_at_utc"]), "联合映射观测时间非法")
        _observation(capture_root, row["observation"], {k: v for k, v in row.items() if k != "observation"}, "代理／NAT 映射")
        for ordinal in call["request_ordinals"]:
            key = (row["connection_id"], ordinal)
            c.require(key not in ordinal_links, "一个原生请求关联到多个本机调用")
            ordinal_links[key] = call
        mapped.add(row["socket_call_id"])
    c.require(mapped == set(calls), "本机调用缺独立服务端关联")
    requests, seen = [], set()
    for row in _json_lines(c.bound_file(capture_root, capture["requests"]), "服务端逐请求"):
        _fields(row, {"connection_id", "request_ordinal", "observed_at_utc", "host", "method", "path", "request_body_base64", "response_body_base64"}, "服务端原生请求")
        _id(row["connection_id"], "原生请求连接")
        c.require(type(row["request_ordinal"]) is int and row["request_ordinal"] > 0, "原生请求 ordinal 非法")
        key = (row["connection_id"], row["request_ordinal"])
        call, connection = ordinal_links.get(key), connections.get(key[0])
        c.require(key not in seen and call is not None and connection is not None, "原生请求重复或缺真实进程联合关联")
        model_request = row["method"] == "POST" and str(row["path"]).split("?", 1)[0] in c.MODEL_ENDPOINTS
        if model_request:
            roles = call_roles[call["socket_call_id"]]
            c.require(call["scenario_id"] is not None and "embedded_cli" in roles and (task["client_id"] != "claude-vscode" or "extension_entry" in roles), "模型请求没有关联真实内置 CLI／扩展参与或冻结场景")
        at = c.timestamp(row["observed_at_utc"])
        c.require(c.timestamp(call["opened_at_utc"]) <= at <= c.timestamp(call["closed_at_utc"]) and c.timestamp(connection["opened_at_utc"]) <= at <= c.timestamp(connection["closed_at_utc"]), "请求时间不在两端连接共同窗口")
        try:
            body_raw = base64.b64decode(row["request_body_base64"], validate=True)
            response_raw = base64.b64decode(row["response_body_base64"], validate=True)
        except ValueError as error:
            raise c.ConfigurationError("Mac 客户端原始请求或响应字节非法") from error
        # 请求体非法、产品失败或代理拒绝都不减少已发 POST 的计量。
        body, response = body_raw.decode("utf-8", errors="replace"), response_raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(body_raw) if body_raw else {}
            payload_valid = isinstance(payload, dict)
        except (ValueError, UnicodeError):
            payload, payload_valid = {}, False
        requests.append({**row, "scenario_id": call["scenario_id"], "process_instance_id": call["process_instance_id"], "decoded_body": body, "decoded_response": response, "payload": payload if payload_valid else {}, "payload_valid": payload_valid})
        seen.add(key)
    c.require(seen == set(ordinal_links), "本机声明的已发请求缺独立原件，账务不能按零收口")
    return capture, requests


def verify_client_bundle_v2(task: Mapping[str, Any], bundle_root: Path, capture_root: Path, *, depth: int = 0) -> dict[str, Any]:
    """结构与原件完整重放；保留失败、截断和超额的全部真实请求。"""
    task = validate_task_v2(task)
    c.require(depth < 32, "客户端场景承接链过深或循环")
    root, capture_root = Path(bundle_root), Path(capture_root)
    c.require(capture_root == Path(task["execution"]["capture_root"]), "服务端原件必须来自任务预约的独立捕获根")
    c.require(not capture_root.is_relative_to(root) and not root.is_relative_to(capture_root), "服务端原件不能来自 Mac 上传包")
    source = _fields(c.read_json(c.secure_file(root, "source-manifest.json")), {"schema_version", "task_sha256", "run_id", "source_scope", "status", "files"}, "来源清单")
    c.require(source["schema_version"] == BUNDLE_SCHEMA and source["task_sha256"] == task["task_sha256"] and source["source_scope"] == task["execution"]["source_scope"], "来源清单 schema、任务或用途不符")
    _id(source["run_id"], "来源运行")
    c.require(source["status"] in {"complete", "failed", "truncated"}, "来源运行终态非法")
    files = c._inventory(root, source)
    c.require({"client-identity.json", "process-session.json", "session-events.jsonl", "socket-calls.jsonl", "recorder-execution.json", "recorder-journal.jsonl", "monitor-ready.json", "state-restoration.json", "state-before.bin", "state-after.bin", "source-origin.json"} <= set(files), "缺产品参与、恢复或受管来源原件")
    c.bound_file(root, task["execution"]["isolation_recipe"])
    session, processes, modules = _participation(task, root, files)
    recorder = _fields(c.read_json(files["recorder-execution.json"]), {"recorder_id", "task_sha256", "run_id", "status", "deployment", "monitor_started_at_utc", "journal"}, "记录器执行")
    c.require(recorder["recorder_id"] == task["execution"]["recorder_id"] and recorder["task_sha256"] == task["task_sha256"] and recorder["run_id"] == source["run_id"] and recorder["status"] == source["status"] and recorder["deployment"] == task["execution"]["source_deployments"]["mac"], "记录器执行记录不闭合")
    c.require(c.timestamp(task["issued_at_utc"]) <= c.timestamp(recorder["monitor_started_at_utc"]) <= min(c.timestamp(row["started_at_utc"]) for row in processes.values()), "进程监测晚于产品启动")
    monitor = _fields(c.read_json(files["monitor-ready.json"]), {"task_sha256", "deployment", "started_at_utc"}, "请求前监测 ready")
    c.require(monitor == {"task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"]["mac"], "started_at_utc": recorder["monitor_started_at_utc"]}, "监测 ready 未绑定同一任务与启动前部署")
    journal_path = c.bound_file(root, recorder["journal"])
    journal = _json_lines(journal_path, "记录器恢复 journal")
    c.require(bool(journal), "缺少请求前准备与恢复 journal")
    seen_events, previous_at = [], c.timestamp(task["issued_at_utc"])
    for event in journal:
        _fields(event, {"event", "task_sha256", "observed_at_utc", "observation"}, "记录器 journal 事件")
        at = c.timestamp(event["observed_at_utc"])
        c.require(event["task_sha256"] == task["task_sha256"] and previous_at <= at, "journal 任务不符或顺序倒置")
        _observation(root, event["observation"], {k: v for k, v in event.items() if k != "observation"}, "记录器 journal")
        seen_events.append(event["event"])
        previous_at = at
    c.require(seen_events in (["prepared", "monitor_ready", "product_started", "product_stopped", "state_restored", "closed"], ["prepared", "monitor_ready", "product_started", "product_stopped", "state_restore_failed", "closed"]), "记录器监测／回收／恢复事件链不闭合")
    first_start = min(c.timestamp(row["started_at_utc"]) for row in processes.values())
    c.require(c.timestamp(journal[0]["observed_at_utc"]) <= c.timestamp(journal[1]["observed_at_utc"]) == c.timestamp(recorder["monitor_started_at_utc"]) <= first_start and c.timestamp(journal[2]["observed_at_utc"]) == first_start and c.timestamp(session["completed_at_utc"]) <= c.timestamp(journal[3]["observed_at_utc"]), "journal 未证明启动前准备／监测和运行后回收")
    restoration = _fields(c.read_json(files["state-restoration.json"]), {"task_sha256", "status", "before", "after", "journal"}, "产品隔离恢复")
    c.require(restoration["task_sha256"] == task["task_sha256"] and restoration["status"] in {"restored", "failed", "unknown"}, "恢复任务或终态非法")
    c.require((seen_events[-2] == "state_restored") == (restoration["status"] == "restored"), "恢复结论与 journal 终态冲突")
    before, after = c.bound_file(root, restoration["before"]), c.bound_file(root, restoration["after"])
    c.require(before == root / "state-before.bin" and after == root / "state-after.bin", "恢复前后原件必须独立绑定，不能拿 before 冒充 after")
    c.require(c.bound_file(root, restoration["journal"]) == journal_path, "恢复记录必须绑定同一不可变 journal")
    restored = restoration["status"] == "restored" and before.read_bytes() == after.read_bytes()
    capture, requests = _request_links(task, root, capture_root, files, session, processes, modules)
    for origin_root, path, side in ((root, files["source-origin.json"], "mac"), (capture_root, c.bound_file(capture_root, capture["source_origin"]), "capture")):
        origin = _fields(c.read_json(path), {"task_sha256", "deployment", "attestation"}, "受管来源证明")
        c.require(origin["task_sha256"] == task["task_sha256"] and origin["deployment"] == task["execution"]["source_deployments"][side], "来源证明未绑定两端受管部署")
        c.bound_file(origin_root, origin["attestation"])
    counted, auxiliary = [], []
    for index, row in enumerate(requests):
        path = str(row["path"]).split("?", 1)[0]
        model = row["method"] == "POST" and path in c.MODEL_ENDPOINTS
        oauth = row["host"] == "platform.claude.com" and row["method"] == "POST" and path == "/v1/oauth/token"
        if model or oauth:
            coordinate = {"capture_id": capture["capture_id"], "connection_id": row["connection_id"], "request_ordinal": row["request_ordinal"]}
            item = {"identity_key": c.digest({"producer_run_id": capture["producer_run_id"], "source_kind": c.SOURCE_KIND, "native_coordinate": coordinate}), "producer_run_id": capture["producer_run_id"], "source_kind": c.SOURCE_KIND, "native_coordinate": coordinate, "source_path": str(capture_root / capture["requests"]["path"]), "source_sha256": capture["requests"]["sha256"], "record_index": index, "host": row["host"], "method": row["method"], "path": path, "model": row["payload"].get("model"), "scenario_id": row["scenario_id"]}
            (counted if model else auxiliary).append(item)
    inherited = _inherited_scenarios(task, depth)
    c.require(not ({row["scenario_id"] for row in requests} & set(inherited)), "新运行不得重发已批准复用的场景")
    events, results = _json_lines(files["session-events.jsonl"], "会话事件"), []
    for scenario in task["scenarios"]:
        if scenario["id"] in inherited:
            results.append(inherited[scenario["id"]])
            continue
        matching = [row for row in requests if row["scenario_id"] == scenario["id"] and row["method"] == "POST" and str(row["path"]).split("?", 1)[0] in c.MODEL_ENDPOINTS]
        relevant = [event for event in events if event.get("session_id") == session["session_id"] and event.get("scenario_id") == scenario["id"]]
        passed = len(matching) >= scenario["minimum_requests"] and all(row["payload_valid"] and scenario["challenge"] in row["decoded_body"] and row["payload"].get("model") == scenario["model"] for row in matching)
        passed = passed and any(scenario["response_marker"] in row["decoded_response"] for row in matching)
        passed = passed and any(event.get("kind") == "input" and scenario["challenge"] in str(event.get("text", "")) for event in relevant) and any(event.get("kind") == "completed" and scenario["response_marker"] in str(event.get("text", "")) for event in relevant)
        results.append({"scenario_id": scenario["id"], "status": "pass" if passed else "failed", "request_count": len(matching)})
    resolved = source["status"] != "truncated" and capture["status"] != "truncated"
    succeeded = resolved and restored and len(counted) <= task["execution"]["request_budget"] and source["status"] == capture["status"] == "complete" and all(row["status"] == "pass" for row in results)
    return {"status": "verified" if succeeded else "failed", "accounting_status": "resolved" if resolved else "unresolved", "source_scope": source["source_scope"], "run_id": source["run_id"], "task_sha256": task["task_sha256"], "requests": counted, "auxiliary_requests": auxiliary, "scenario_results": results, "source_manifest": c.binding(root / "source-manifest.json", root), "capture_manifest": c.binding(capture_root / "capture-manifest.json", capture_root)}


def _inherited_scenarios(task: Mapping[str, Any], depth: int) -> dict[str, Any]:
    inherited = {}
    scenarios = {row["id"]: row for row in task["scenarios"]}
    for reuse in task["scenario_reuse"]:
        _fields(reuse, {"root", "task", "source_manifest", "scenario_ids"}, "场景复用")
        previous_root = Path(str(reuse["root"]))
        previous_task = validate_task_v2(c.read_json(c.bound_file(previous_root, reuse["task"])))
        c.verify_task_attempt(previous_root, previous_task)
        previous_bundle = c.bound_file(previous_root, reuse["source_manifest"]).parent
        c.require(c.timestamp(previous_task["issued_at_utc"]) < c.timestamp(task["issued_at_utc"]), "场景复用必须指向更早冻结的任务")
        for key in (*c.COORDINATES, "client_id", "candidate", "catalog", "client_artifacts", "candidate_revision"):
            c.require(previous_task[key] == task[key], f"场景复用 {key} 已变化，必须重新取证")
        c.require(previous_task["execution"]["source_scope"] == task["execution"]["source_scope"], "夹具不能承接为正式来源")
        previous = verify_client_bundle_v2(previous_task, previous_bundle, Path(previous_task["execution"]["capture_root"]), depth=depth+1)
        if previous_task["execution"]["source_scope"] == "formal":
            from tools.official_client_capture import claude_upgrade_mac_client_capture
            c.verify_imported_run(previous_root, previous_task["client_id"], previous_bundle.parent / "run.json", previous_task, previous_bundle / "source-manifest.json")
            claude_upgrade_mac_client_capture.require_registered_reservation(previous_root, previous_task)
            c.verify_origin(previous_task, previous_bundle, Path(previous_task["execution"]["capture_root"]))
            from tools.official_client_capture import claude_upgrade_client_receipts
            claude_upgrade_client_receipts.verify_accounting(c.read_json(previous_bundle.parent / "accounting.json"), previous_task, sorted(row["identity_key"] for row in previous["requests"]))
        c.require(previous["accounting_status"] == "resolved", "账务未决运行不能承接场景")
        c.require(isinstance(reuse["scenario_ids"], list) and bool(reuse["scenario_ids"]), "场景复用闭集为空")
        previous_scenarios = {row["id"]: row for row in previous_task["scenarios"]}
        for scenario_id in reuse["scenario_ids"]:
            matches = [row for row in previous["scenario_results"] if row["scenario_id"] == scenario_id and row["status"] == "pass"]
            c.require(scenario_id not in inherited and scenario_id in scenarios and previous_scenarios.get(scenario_id) == scenarios[scenario_id] and len(matches) == 1, "只能承接身份／条件不变且已通过的场景")
            inherited[scenario_id] = {**matches[0], "reused_from_task_sha256": previous_task["task_sha256"]}
    return inherited


def verify_mac_source_v2(task: Mapping[str, Any], bundle_root: Path, capture_root: Path) -> dict[str, Any]:
    """正式读侧唯一来源入口；默认 registry 为空，所以模板清单不能签真机成功。"""
    result = verify_client_bundle_v2(task, bundle_root, capture_root)
    c.require(task["execution"]["source_scope"] == "formal", "夹具不能取得当前正式来源权")
    verify_isolation_recipe_v2(task, bundle_root)
    c.verify_origin(task, bundle_root, capture_root)
    return result


def collect_independent_requests_v2(task: Mapping[str, Any], capture_root: Path) -> dict[str, Any]:
    """未导入 Mac 包时仍发现独立捕获的全部请求；不授予产品验收成功权。"""
    validate_task_v2(task)
    root = Path(capture_root)
    verify_reservation_v2(task, root)
    adapter = c.source_adapter(task)
    c.require(adapter is not None and callable(getattr(adapter, "verify_capture_origin", None)), "独立捕获受管来源尚未登记")
    adapter.verify_capture_origin(task, root)
    capture = _fields(c.read_json(c.secure_file(root, "capture-manifest.json")), {"schema_version", "task_sha256", "capture_id", "producer_run_id", "candidate", "endpoint", "status", "requests", "connections", "transport_map", "reservation", "ready", "source_origin"}, "未导入独立捕获清单")
    c.require(capture.get("schema_version") == CAPTURE_SCHEMA and capture.get("task_sha256") == task["task_sha256"] and capture.get("candidate") == task["candidate"], "独立捕获版本或候选不符")
    for key in ("capture_id", "producer_run_id", "endpoint"):
        c.require(capture.get(key) == task["execution"][key], "独立捕获坐标与任务不符")
    c.require(capture["status"] in {"complete", "failed", "truncated"}, "独立捕获终态非法")
    origin = _fields(c.read_json(c.bound_file(root, capture["source_origin"])), {"task_sha256", "deployment", "attestation"}, "独立捕获来源")
    c.require(origin["task_sha256"] == task["task_sha256"] and origin["deployment"] == task["execution"]["source_deployments"]["capture"], "独立捕获来源未绑定受管部署")
    c.bound_file(root, origin["attestation"])
    path = c.bound_file(root, capture["requests"])
    requests, auxiliary, seen = [], [], set()
    for index, row in enumerate(_json_lines(path, "未导入的服务端逐请求")):
        _fields(row, {"connection_id", "request_ordinal", "observed_at_utc", "host", "method", "path", "request_body_base64", "response_body_base64"}, "服务端原生请求")
        _id(row["connection_id"], "原生连接")
        c.require(type(row["request_ordinal"]) is int and row["request_ordinal"] > 0, "原生 ordinal 非法")
        coordinate = {"capture_id": capture["capture_id"], "connection_id": row["connection_id"], "request_ordinal": row["request_ordinal"]}
        c.require(c.digest(coordinate) not in seen, "独立捕获重复原生请求坐标")
        seen.add(c.digest(coordinate))
        try:
            body = base64.b64decode(row["request_body_base64"], validate=True)
        except ValueError as error:
            raise c.ConfigurationError("Mac 未导入运行的请求原件非法") from error
        try:
            payload = json.loads(body) if body else {}
            payload = payload if isinstance(payload, dict) else {}
        except (ValueError, UnicodeError):
            payload = {}
        native_path = str(row["path"]).split("?", 1)[0]
        model = row["method"] == "POST" and native_path in c.MODEL_ENDPOINTS
        oauth = row["host"] == "platform.claude.com" and row["method"] == "POST" and native_path == "/v1/oauth/token"
        if model or oauth:
            item = {"identity_key": c.digest({"producer_run_id": capture["producer_run_id"], "source_kind": c.SOURCE_KIND, "native_coordinate": coordinate}), "producer_run_id": capture["producer_run_id"], "source_kind": c.SOURCE_KIND, "native_coordinate": coordinate, "source_path": str(path), "source_sha256": capture["requests"]["sha256"], "record_index": index, "host": row["host"], "method": row["method"], "path": native_path, "model": payload.get("model"), "scenario_id": None}
            (requests if model else auxiliary).append(item)
    return {"requests": requests, "auxiliary_requests": auxiliary}


def verify_isolation_recipe_v2(task: Mapping[str, Any], bundle_root: Path, *,
                               bridge_context: Any = None) -> dict[str, Any]:
    """核验原隔离合同；跨机路径只接受固定桥认证过的原件映射。

    任务和 recipe 始终保留 ARM64 冻结的原字节。普通来源继续要求会话属于
    当前 run；固定正式桥在 Mac 上使用经过认证的物理目录，读侧重验原 mapping，
    不接受外部 dataclass、任意转根或改写后的 recipe 冒充同一个任务。
    """
    validate_task_v2(task)
    root = Path(bundle_root)
    recipe = c.read_json(c.bound_file(root, task["execution"]["isolation_recipe"]))
    fields = {"schema_version", "client_id", "deployment", "official_support", "endpoint", "credential_ref", "private_session_root", "state_paths", "recipe_id", "request_policy"}
    c.require(set(recipe) == fields and recipe["schema_version"] == "claude-upgrade-client-isolation-recipe/v2", "缺真实产品隔离方法合同")
    c.require(recipe["client_id"] == task["client_id"] and recipe["deployment"] == task["execution"]["source_deployments"]["mac"] and recipe["endpoint"] == task["execution"]["endpoint"] and recipe["credential_ref"] == task["execution"]["credential_ref"] and recipe["request_policy"] == task["execution"]["request_policy"], "产品隔离方法与候选／受管部署不符")
    c.bound_file(root, recipe["official_support"])
    session_root = Path(str(recipe["private_session_root"]))
    c.require_plain_path(session_root)
    adapter = c.source_adapter(task)
    c.require(adapter is not None and callable(getattr(adapter, "verify_isolation_recipe", None)), "真实产品隔离方法尚未验收；禁止猜启动参数")
    c.require(isinstance(recipe["state_paths"], list) and bool(recipe["state_paths"])
              and all(Path(str(value)).is_absolute() and ".." not in Path(str(value)).parts
                      for value in recipe["state_paths"]), "原状态位置未完整冻结")
    fixed_bridge = (task["execution"]["recorder_id"] == c.MANAGED_RECORDER_ID
                    and task["execution"]["source_scope"] == "formal")
    if fixed_bridge:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge

        if bridge_context is None:
            verify_context = getattr(adapter, "verify_bridge_path_context", None)
            c.require(callable(verify_context), "固定正式来源缺路径映射验证入口")
            bridge_context = verify_context(task, root)
        physical_session = bridge.validate_bridge_path_context(task, bridge_context, bundle_root=root)
        c.require(str(bridge_context.logical_session_root) == str(session_root)
                  and {str(logical) for logical, _physical in bridge_context.state_paths}
                  == set(recipe["state_paths"]),
                  "路径映射未绑定原私有会话与状态路径全集")
        c.require_plain_path(Path(physical_session))
    else:
        c.require(bridge_context is None, "普通来源不得消费跨机路径映射权限")
        c.require(session_root.is_relative_to(root.parent), "私有会话目录未冻结在原 run")
    adapter.verify_isolation_recipe(task, recipe, root)
    return recipe
