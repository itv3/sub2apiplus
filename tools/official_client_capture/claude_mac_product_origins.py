"""实核产品原生原件的来源签名和生命周期，摘要与自报标志都不授正式权。

系统 openssl 验真实 ECDSA；公钥由固定 root 安装读取器提供。原 helper 仍把
自身观测称为 observed_unqualified，消费端须另外核受管资格、完整生命周期、
独立 NE、产品模块与捕获；本模块单独通过不会登记正式来源。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_client_source as source


class ProductOriginError(ValueError):
    """产品原件签名、原任务、内核身份或末尾屏障不符。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductOriginError(message)


def verify_native_attestation(raw: bytes, envelope: Mapping[str, Any], public_key: bytes,
                              task: Mapping[str, Any]) -> dict[str, Any]:
    """核真实签名的精确前缀；fixture 签名正例仍不具正式安装权。"""
    _require(isinstance(raw, bytes) and raw.endswith(b"\n") and len(raw) <= 67_108_864,
             "原生签名前缀截断或超限")
    _require(isinstance(envelope, Mapping) and set(envelope)
             == {"payload_base64", "signature_base64", "public_key_sha256"}, "产品签名字段非法")
    _require(isinstance(public_key, bytes) and len(public_key) == 65 and public_key[0] == 4
             and envelope["public_key_sha256"] == hashlib.sha256(public_key).hexdigest(), "产品签名公钥不是受管冻结值")
    try:
        payload = base64.b64decode(envelope["payload_base64"], validate=True)
        signature = base64.b64decode(envelope["signature_base64"], validate=True)
        message = json.loads(payload)
    except (ValueError, TypeError) as error:
        raise ProductOriginError("产品来源签名编码非法") from error
    _require(0 < len(payload) <= 65536 and 0 < len(signature) <= 144 and isinstance(message, dict)
             and set(message) == {"schema_version", "context", "record_count", "raw_bytes", "stream_sha256", "first_error"}
             and message["schema_version"] == "claude-mac-product-native-attestation/v1", "产品签名合同非法")
    context = message["context"]
    _require(isinstance(context, dict) and set(context) == {"task_sha256", "run_id", "user_id", "deployment"}
             and context["task_sha256"] == task["task_sha256"]
             and context["run_id"] == task["execution"]["producer_run_id"]
             and context["deployment"] == task["execution"]["source_deployments"]["mac"]
             and type(context["user_id"]) is int and 0 < context["user_id"] <= 0xFFFFFFFF
             and type(message["record_count"]) is int and 2 <= message["record_count"] <= 65536
             and type(message["raw_bytes"]) is int and message["raw_bytes"] == len(raw)
             and message["stream_sha256"] == hashlib.sha256(raw).hexdigest()
             and message["first_error"] is None, "产品签名任务、原件或来源错误不符")
    der = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200") + public_key
    encoded = base64.b64encode(der)
    pem = b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + b"\n-----END PUBLIC KEY-----\n"
    with tempfile.TemporaryDirectory(prefix="claude-product-origin-") as name:
        root = Path(name)
        for relative, data in (("key.pem", pem), ("payload", payload), ("signature", signature)):
            native._new_file(root / relative, data)
        checked = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(root / "key.pem"),
            "-signature", str(root / "signature"), str(root / "payload")], capture_output=True, timeout=10, check=False)
        _require(checked.returncode == 0, "产品原件实际 ECDSA 来源签名不通过")
    return copy.deepcopy(message)


def verify_native_original(task: Mapping[str, Any], raw: bytes, public_key: bytes) -> dict[str, Any]:
    """实读流的最后一帧必须签前面的精确字节，且产品执行已经真实退出。"""
    _require(raw.endswith(b"\n") and len(raw) <= 67_108_864, "原生原件截断或超限")
    lines = raw.splitlines(keepends=True)
    _require(3 <= len(lines) <= 65537, "原生原件缺屏障或记录过多")
    seal = json.loads(lines[-1])
    _require(isinstance(seal, dict) and set(seal) == {"kind", "payload_base64", "signature_base64", "public_key_sha256"}
             and seal["kind"] == "origin_attestation", "原生原件缺真实来源签名尾部")
    prefix = b"".join(lines[:-1])
    envelope = {key: value for key, value in seal.items() if key != "kind"}
    signed = verify_native_attestation(prefix, envelope, public_key, task)
    frames = [native.validate_native_frame(json.loads(line), task) for line in lines[:-1]]
    _require(signed["record_count"] == len(frames) and frames[0]["kind"] == "ready"
             and frames[-1]["kind"] == "closed"
             and all(row["kind"] not in {"ready", "closed", "unavailable", "incomplete"} for row in frames[1:-1]),
             "原生原件启动、失败与尾屏障不闭合")
    closed = frames[-1]
    _require(closed["close_requested"] is True and closed["unsubscribe_result"] == closed["delete_result"] == 0
             and closed["unclosed_instances"] == 0 and closed["first_error"] is None, "原生订阅、退出或尾部没有实收口")
    histories = native.owned_process_lifecycles(frames, task)
    _require(bool(histories) and all(row["end"] is not None for row in histories.values())
             and all(token[1] == signed["context"]["user_id"] for token in histories), "产品原生执行身份没有全部闭合")
    boot = frames[0]["boot_id"]
    _require(all(row["start"]["process"]["boot_id"] == boot for row in histories.values()), "产品原生原件跨 boot")
    sequenced = [row for row in frames if "global_sequence" in row]
    for left, right in zip(sequenced, sequenced[1:]):
        if left["global_sequence"] == right["global_sequence"]:
            _require(left["kind"] == "product_state" and left["event"] == "close" and right["kind"] == "product_file"
                and left["paths"] == [right["path"]] and left["process"] == right["process"]
                and left["event_sequence"] == right["event_sequence"] and left["observed_at_utc"] == right["observed_at_utc"],
                "同一 CLOSE 的两个用途投影不属于同一内核原件")
        else:
            _require(left["global_sequence"] < right["global_sequence"], "原生 owned 流事件重复或倒置")
    return {"signed": signed, "attestation": envelope, "frames": frames, "lifecycles": histories,
            "kernel_event_chain_closed": True, "source_scope": "verified_native_original"}


def read_native_original(task: Mapping[str, Any], bundle_root: Path,
                         installed: authority.ProductInstallationAuthority) -> dict[str, Any]:
    """实读固定原件、受管 public key，再复核完整真实签名链。"""
    key = installed.origin_public_key(task["execution"]["source_deployments"]["mac"])
    raw = authority.read_original(Path(bundle_root) / "native-events.jsonl")
    value = verify_native_original(task, raw, key)
    return {**value, "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)}


def verify_product_originals(task: Mapping[str, Any], bundle_root: Path, *,
                              native_public_key: bytes, flow_public_key: bytes,
                              path_context: Any = None) -> dict[str, Any]:
    """受管桥两端共用的只读原件重验；不读取 ARM64 capture 或授登记权。

    两把公钥须由各侧固定安装读取器提供，不能采用包内自报公钥。模块/产品
    日志只消费恢复前的复制件与已签 CLOSE 的 content_sha256。HTTP ordinal
    完全不在这里形成，必须由 ARM64 独立原生捕获联合匹配。
    """
    from tools.official_client_capture import claude_mac_product_flows as flows
    from tools.official_client_capture import claude_mac_product_logs as logs
    from tools.official_client_capture import claude_mac_product_modules as modules
    from tools.official_client_capture import claude_mac_product_recipe as recipes
    root = Path(bundle_root)
    frozen = source.validate_task_v2(task)
    observed = verify_native_original(frozen, authority.read_original(root/"native-events.jsonl"), native_public_key)
    from tools.official_client_capture import claude_mac_product_state as product_state
    product_state.verify_archived_restore(frozen, root, observed, path_context=path_context)
    envelope = json.loads(authority.read_original(root/"network-attestation.json"))
    try:
        flow_message = json.loads(base64.b64decode(envelope["payload_base64"], validate=True))
    except (KeyError, ValueError, TypeError) as error:
        raise ProductOriginError("网络来源封套编码非法") from error
    context = flows._context(frozen, user_id=observed["signed"]["context"]["user_id"])
    network_raw = authority.read_original(root/"network-events.jsonl")
    signed_flows = flows.verify_flow_attestation(network_raw, envelope, flow_public_key, context)
    _require(signed_flows["first_error"] is None and signed_flows["record_count"] == len(network_raw.splitlines()),
             "网络已签原件有错误或记录数不符")
    network = flows.parse_owned_flows(network_raw, task=frozen, process_frames=observed["frames"], context=context)
    plan = recipes.derive_archived_private_launch_plan(frozen, bundle_root=root,
        materials_receipt=root/"product-materials.json", path_context=path_context)["plan"]
    closed = json.loads(authority.read_original(root/"provider-observations/closed-originals.json"))
    _require(set(closed) == {"closed", "native_closed", "flow_attestation", "module_observation", "unknown_reasons"}
             and closed["closed"] is True and closed["unknown_reasons"] == []
             and closed["native_closed"] == observed["frames"][-1] and closed["flow_attestation"] == envelope,
             "恢复前真实原件封存不闭合")
    module = None
    if frozen["client_id"] == "claude-vscode":
        module = modules.replay_vscode_activation(frozen, bundle_root=root, plan=plan,
            materials_receipt=root/"product-materials.json", native_frames=observed["frames"],
            log_utc_offset_seconds=closed["module_observation"]["log_utc_offset_seconds"])["module_observation"]
        _require(module == closed["module_observation"], "模块原件实算与恢复前封存不同")
    inherited = {sid for row in frozen["scenario_reuse"] for sid in row["scenario_ids"]}
    scenarios = [logs.replay_product_scenario(frozen, bundle_root=root, plan=plan, scenario=scenario,
        native_frames=observed["frames"]) for scenario in frozen["scenarios"] if scenario["id"] not in inherited]
    sessions = {row["session_id"] for value in scenarios for row in value["session_events"]}
    _require(len(sessions) == 1, "本轮没有唯一真实产品会话；不能把其他会话或旧承接换成当前")
    return {"task_sha256": frozen["task_sha256"], "native": observed, "network_flows": network,
        "network_attestation": envelope, "module_observation": module, "scenarios": scenarios,
        "session_id": next(iter(sessions)), "plan": plan}


def _process_session(task: Mapping[str, Any], originals: Mapping[str, Any]) -> tuple[dict[str, Any], dict[tuple[int, ...], str]]:
    """只从已闭合的实际 exec 构造产品角色；未知父链或执行映像拒绝。"""
    _require(originals["task_sha256"] == task["task_sha256"], "产品原件不属于原 task")
    artifacts = {row["role"]: row for row in task["client_artifacts"]}
    executable_roles = {row["content"]["path"]: role for role, row in artifacts.items()
        if role in {"application_executable", "extension_host", "embedded_cli"}}
    histories = originals["native"]["lifecycles"]
    selected = {token: row for token, row in histories.items() if row["start"]["event"] == "exec"
        and row["start"]["process"]["executable"] in executable_roles}
    _require(bool(selected), "缺真实产品 exec")
    identities = {token: source.process_instance_id(row["start"]["process"]["pid"], row["start"]["process"]["boot_id"],
        f's{row["start"]["process"]["start_seconds"]}-u{row["start"]["process"]["start_microseconds"]}-v{token[7]}')
        for token, row in selected.items()}
    processes = []
    for token, history in sorted(selected.items(), key=lambda pair: c.timestamp(pair[1]["start"]["observed_at_utc"])):
        observed = history["start"]["process"]
        artifact_role = executable_roles[observed["executable"]]
        role = "application" if artifact_role == "application_executable" else artifact_role
        parent = tuple(observed["parent_audit_token"])
        _require(history["end"] is not None and history["end_reason"] == "exit",
                 "角色执行尚未有真实 exit；旧 exec 身份不能冒充当前产品角色")
        _require(role == "application" or parent in identities,
                 "产品角色缺真实直接父实例，不能跳过未知中间进程猜父链")
        start_marker = f's{observed["start_seconds"]}-u{observed["start_microseconds"]}-v{token[7]}'
        artifact = artifacts[artifact_role]
        processes.append({"process_instance_id": identities[token], "pid": observed["pid"],
            "boot_id": observed["boot_id"], "start_marker": start_marker,
            "parent_instance_id": None if role == "application" else identities[parent],
            "executable_artifact_id": artifact["artifact_id"], "executable_sha256": artifact["content"]["sha256"],
            "role": role, "session_id": originals["session_id"], "started_at_utc": history["start"]["observed_at_utc"],
            "exited_at_utc": history["end"]["observed_at_utc"]})
    applications = [row for row in processes if row["role"] == "application"]
    _require(len(applications) == 1 and any(row["role"] == "embedded_cli" for row in processes),
             "缺唯一真实应用与内置 CLI 参与")
    modules = []
    if task["client_id"] == "claude-vscode":
        observed = originals["module_observation"]
        host_token = tuple(observed["host_process"]["audit_token"])
        _require(host_token in identities, "成功激活不属于同一实际 extension host")
        modules.append({"module_instance_id": "module-"+c.digest({"task": task["task_sha256"], "observed": observed}),
            "artifact_id": observed["artifact_id"], "host_process_instance_id": identities[host_token],
            "entrypoint": artifacts["extension_entry"]["content"]["path"],
            "loaded_at_utc": observed["loaded_at_utc"], "activated_at_utc": observed["activated_at_utc"]})
    application = applications[0]
    return ({"client_id": task["client_id"], "session_id": originals["session_id"],
        "started_at_utc": application["started_at_utc"], "completed_at_utc": application["exited_at_utc"],
        "processes": processes, "loaded_modules": modules}, identities)


def build_process_session(task: Mapping[str, Any], bundle_root: Path, originals: Mapping[str, Any]) -> dict[str, Any]:
    """首次持久化真实观测投影；同一原件重入只核相同字节，不能覆盖。"""
    root = Path(bundle_root)
    session, identities = _process_session(task, originals)
    directory = root/"product-participation"
    directory.mkdir(mode=0o700, exist_ok=True)
    native._private_directory(directory)
    for kind, values in (("process", session["processes"]), ("module", session["loaded_modules"])):
        for index, value in enumerate(values):
            path = directory/f"{kind}-{index:04d}.json"
            raw = c.canonical(value)
            if path.exists():
                _require(authority.read_original(path) == raw, "真实产品参与投影发生漂移")
            else:
                native._new_file(path, raw)
            value["observation"] = c.binding(path, root)
    path = root/"process-session.json"
    raw = c.canonical(session)
    if path.exists():
        _require(authority.read_original(path) == raw, "真实进程会话原件发生漂移")
    else:
        native._new_file(path, raw)
    return {"process_session": session, "process_instances": identities, "network_flows": originals["network_flows"]}


def verify_process_session(task: Mapping[str, Any], bundle_root: Path, originals: Mapping[str, Any]) -> dict[str, Any]:
    """离线精确重算派生观测，不写原件、读集或资格。"""
    root = Path(bundle_root)
    expected, identities = _process_session(task, originals)
    actual = json.loads(authority.read_original(root/"process-session.json"))
    _require(set(actual) == set(expected) and all(actual[key] == expected[key]
        for key in set(expected)-{"processes", "loaded_modules"}), "产品进程会话不符")
    for name in ("processes", "loaded_modules"):
        _require(len(actual[name]) == len(expected[name]), "产品角色观测数量不符")
        for value, derived in zip(actual[name], expected[name]):
            _require({key: item for key, item in value.items() if key != "observation"} == derived
                     and c.read_json(c.bound_file(root, value["observation"])) == derived, "产品角色投影未命中实际原件")
    return {"process_session": actual, "process_instances": identities, "network_flows": originals["network_flows"]}
