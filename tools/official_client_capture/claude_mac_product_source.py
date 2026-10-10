"""Mac 侧限定的真实产品静态工厂；默认缺现场安装和资格时零启动。

不读 Mac 项目账本、不重开预算；只消费 ARM64 独立捕获的实际签名派发镜像。
root 资格合同授权准确的产品、隔离配方和边界进行尝试，不自报验收成功。
产品进程、成功激活、真实日志与网络流仍逐项验原件，最终请求归属回 ARM64。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import stat
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_broker as broker
from tools.official_client_capture import claude_mac_product_flows as flows
from tools.official_client_capture import claude_mac_product_materials as materials
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_origins as origins
from tools.official_client_capture import claude_mac_product_recipe as recipes

QUALIFICATION_SCHEMA = "claude-mac-product-qualification/v1"
_TOKEN = object()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise authority.ProductAuthorityError(message)


def _dispatch(task: Mapping[str, Any], dispatch: Any, *, live: bool) -> dict[str, Any]:
    from tools.official_client_capture import claude_mac_formal_capture_contract as capture
    return capture.verify_capture_dispatch(task, dispatch, require_live=live)


class _MacDispatchServices:
    """原签名镜像的只读消费端；没有 reserve/admit、HTTP 或账本替身。"""
    def __init__(self, task: Mapping[str, Any], dispatch: Any, *, token: object) -> None:
        _require(token is _TOKEN, "Mac 派发服务须由固定产品工厂拥有")
        self._task = copy.deepcopy(dict(task))
        self._dispatch = dispatch

    def recheck_dispatch(self, task: Mapping[str, Any]) -> None:
        _require(dict(task) == self._task, "Mac 派发服务不接受其他 task 或重新冻结预算")
        _dispatch(self._task, self._dispatch, live=True)


class QualifiedMacProductSource:
    """只拥有本工厂的原实例；同字段的外部 provider/recorder 不能登记 formal。"""
    def __init__(self) -> None:
        self._owners: dict[int, dict[str, Any]] = {}

    def _owner(self, provider: Any, task: Mapping[str, Any], root: Path) -> dict[str, Any]:
        value = self._owners.get(id(provider))
        _require(value is not None and value["provider"] is provider and value["task"] == dict(task)
                 and value["root"] == Path(root), "产品实例不属于固定工厂的同一原 task")
        return value

    def owns_observation_provider(self, provider: Any, task: Mapping[str, Any], root: Path) -> bool:
        try:
            self._owner(provider, task, root)
            return True
        except ValueError:
            return False

    def owns_services(self, value: Any) -> bool:
        return any(row["services"] is value for row in self._owners.values())

    def owns_recorder(self, value: Any) -> bool:
        return any(row.get("recorder") is value for row in self._owners.values())

    def _qualification(self, task: Mapping[str, Any], root: Path, *, installed: Any, network: Any,
                       root_broker: Any, path_context: Any, require_live: bool = True) -> dict[str, Any]:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge
        _require(platform.system() == "Darwin" and platform.machine() == "arm64" and os.geteuid() > 0,
                 "真实产品工厂必须在 Darwin ARM64 原用户下执行")
        _require(task["execution"]["source_scope"] == "formal"
                 and task["execution"]["recorder_id"] == c.MANAGED_RECORDER_ID, "真实产品不接受夹具或任意来源名称")
        bridge.validate_bridge_path_context(task, path_context, bundle_root=root)
        bridge_facts = bridge.require_mac_agent_installation(task)
        deployment = task["execution"]["source_deployments"]["mac"]
        native_facts = installed.revalidate(deployment)
        network_facts = network.revalidate(deployment)
        broker_facts = root_broker.revalidate(deployment)
        installed.origin_public_key(deployment)
        raw = authority.read_original(authority.INSTALL_ROOT/"product-qualification.json", maximum=65536, root_owned=True)
        grant = json.loads(raw)
        _require(isinstance(grant, dict) and "not_after_utc" in grant, "产品登记许可缺合法期限")
        not_after = c.timestamp(grant["not_after_utc"])
        _require(set(grant) == {"schema_version", "source_deployments", "installation_sha256s", "not_after_utc", "subjects"}
                 and grant["schema_version"] == QUALIFICATION_SCHEMA
                 and grant["source_deployments"] == task["execution"]["source_deployments"]
                 and grant["installation_sha256s"] == {"native": native_facts["installation_sha256"],
                     "network": network_facts["installation_sha256"], "broker": broker_facts["installation_sha256"],
                     "bridge": bridge_facts.sha256}
                 and (not require_live or datetime.now(timezone.utc) < not_after),
                 "产品登记许可已失效或安装/双部署不符")
        _require(isinstance(grant["subjects"], list) and 1 <= len(grant["subjects"]) <= 32, "产品许可 subject 数量非法")
        material = materials.read_product_materials(root/"product-materials.json")
        recipe = c.read_json(c.bound_file(root, task["execution"]["isolation_recipe"]))
        subjects = []
        for subject in grant["subjects"]:
            _require(set(subject) == {"client_id", "artifact_identity_sha256", "official_support_sha256", "recipe_id",
                         "state_paths_relative", "credential_refs", "max_timeout_seconds", "max_request_budget",
                         "retry_upper_bound", "max_concurrency", "snapshot_limits"}, "产品许可 subject 字段非法")
            if (subject["client_id"] == task["client_id"] and subject["artifact_identity_sha256"] == recipes._artifact_identity(task)
                    and subject["official_support_sha256"] == recipe["official_support"]["sha256"]
                    and subject["recipe_id"] == recipe["recipe_id"]):
                subjects.append(subject)
        _require(len(subjects) == 1 and material["client_artifacts"] == task["client_artifacts"], "没有唯一准确的已批准产品/配方 subject")
        subject = subjects[0]
        policy, execution = task["execution"]["request_policy"], task["execution"]
        _require(all(type(subject[name]) is int and subject[name] > 0 for name in
                     ("max_timeout_seconds", "max_request_budget", "max_concurrency"))
                 and type(subject["retry_upper_bound"]) is int and subject["retry_upper_bound"] >= 0
                 and execution["timeout_seconds"] <= subject["max_timeout_seconds"] <= 7200
                 and execution["request_budget"] <= subject["max_request_budget"]
                 and policy["retry_upper_bound"] == subject["retry_upper_bound"]
                 and policy["max_concurrency"] == subject["max_concurrency"] == 1
                 and isinstance(subject["credential_refs"], list) and execution["credential_ref"] in subject["credential_refs"],
                 "产品请求、重试、期限、并发或凭据作用域越出冻结许可")
        actual_state = recipes.material_state_paths(task, recipe, root, path_context)
        session = recipes.physical_session_root(task, recipe, root, path_context)
        _require(all(path.is_relative_to(session) and path != session for path in actual_state),
                 "产品状态只允许固定桥认证的本 run 私有 session；不恢复全局用户状态")
        relative = sorted(path.relative_to(session).as_posix() for path in actual_state)
        _require(relative == subject["state_paths_relative"], "产品状态作用域与已批准精确路径全集不一致")
        immutable_roles = {row["role"]: row for row in task["client_artifacts"]}
        immutable = Path(immutable_roles["embedded_cli"]["content"]["path"])
        _require(all(not immutable.is_relative_to(path) and not path.is_relative_to(immutable) for path in actual_state),
                 "不可变的大 CLI 物料不能列作状态备份；须在冻结 recipe 前收窄精确状态全集")
        if task["client_id"] == "claude-vscode":
            _require("user_data/User" in relative and material["facts"].get("vscode_activation_source") is not None,
                     "扩展必须具备实际成功激活来源与准确的私有 User 设置作用域")
        return {"raw_sha256": hashlib.sha256(raw).hexdigest(), "subject": subject, "recipe": recipe}

    def verify_product_qualification(self, task: Mapping[str, Any], root: Path, provider: Any, *, dispatch: bool) -> None:
        owner = self._owner(provider, task, root)
        actual = self._qualification(task, Path(root), installed=owner["native"], network=owner["network"],
             root_broker=owner["broker"], path_context=owner["paths"], require_live=dispatch)
        _require(actual == owner["qualification"], "原产品许可已撤销、更新或替换；不能借当前身份续跑旧实例")
        _dispatch(task, owner["services"]._dispatch, live=dispatch)

    def verify_bridge_path_context(self, task: Mapping[str, Any], root: Path) -> Any:
        owners = [row for row in self._owners.values() if row["task"] == dict(task) and row["root"] == Path(root)]
        _require(len(owners) == 1, "路径映射没有唯一原产品工厂实例")
        from tools.official_client_capture import claude_mac_agent_bridge as bridge
        bridge.validate_bridge_path_context(task, owners[0]["paths"], bundle_root=Path(root))
        return owners[0]["paths"]

    def verify_isolation_recipe(self, task: Mapping[str, Any], recipe: Mapping[str, Any], root: Path) -> None:
        owners = [row for row in self._owners.values() if row["task"] == dict(task) and row["root"] == Path(root)]
        _require(len(owners) == 1 and owners[0]["qualification"]["recipe"] == dict(recipe), "隔离配方没有绑定唯一原工厂")
        self.verify_product_qualification(task, root, owners[0]["provider"], dispatch=False)
        recipes.derive_material_private_launch_plan(task, bundle_root=Path(root),
            materials_receipt=Path(root)/"product-materials.json", path_context=owners[0]["paths"])

    def product_environment(self, task: Mapping[str, Any], provider: Any, plan: Mapping[str, Any]) -> dict[str, str]:
        owner = self._owner(provider, task, provider._root)
        self.verify_product_qualification(task, provider._root, provider, dispatch=True)
        reference = task["execution"]["credential_ref"]
        identity = reference[len("private-ref:"):]
        _require(bool(c.ID_RE.fullmatch(identity)), "凭据引用只能是固定单文件 ID；不接受路径或命令")
        directory = authority.INSTALL_ROOT/"credentials"/str(os.geteuid())
        info = directory.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                 "候选凭据目录不是原用户的私有固定目录")
        path = directory/(identity+".json")
        info = path.lstat()
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600,
                 "候选凭据不是原用户的私有普通文件")
        raw = authority.read_original(path, maximum=8192)
        value = json.loads(raw)
        _require(set(value) == {"credential_ref", "endpoint", "token"} and value["credential_ref"] == reference
                 and value["endpoint"] == task["execution"]["endpoint"] and isinstance(value["token"], str)
                 and 1 <= len(value["token"]) <= 4096 and all(ord(char) >= 32 for char in value["token"]),
                 "候选私有凭据合同或 endpoint 不符")
        _require(plan == provider._plan, "环境只接受原冻结私有计划")
        # 不把 token 写入 provider 原件、WAL、材料、返回日志或原启动计划。
        return {**plan["environment"], "ANTHROPIC_BASE_URL": value["endpoint"], "ANTHROPIC_AUTH_TOKEN": value["token"]}

    def _originals(self, task: Mapping[str, Any], root: Path, provider: Any) -> dict[str, Any]:
        owner = self._owner(provider, task, root)
        self.verify_product_qualification(task, root, provider, dispatch=False)
        return origins.verify_product_originals(task, root, native_public_key=owner["native"].origin_public_key(
            task["execution"]["source_deployments"]["mac"]), flow_public_key=owner["network"].public_key,
            path_context=owner["paths"])

    def export_product_observations(self, task: Mapping[str, Any], provider: Any,
                                    frames: Any, completed: Mapping[str, Any]) -> dict[str, Any]:
        root = provider._root
        actual = self._originals(task, root, provider)
        _require(tuple(actual["native"]["frames"]) == tuple(row for row in frames if row["kind"] != "origin_attestation")
                 and {value["scenario_id"]: value for value in actual["scenarios"]} == dict(completed),
                 "原件重放与运行期实际产品观测不同")
        origins.build_process_session(task, root, actual)
        events = [event for result in actual["scenarios"] for event in result["session_events"]]
        raw = b"".join(c.canonical(event)+b"\n" for event in events)
        _once(root/"session-events.jsonl", raw)
        owner = self._owner(provider, task, root)
        envelope = {"schema_version": "claude-mac-product-origin/v1", "task_sha256": task["task_sha256"],
            "native_attestation": c.binding(root/"product-native-attestation.json", root),
            "network_attestation": c.binding(root/"network-attestation.json", root),
            "dispatch": owner["services"]._dispatch.envelope_binding,
            "bridge_path_mapping": owner["paths"].mapping_binding,
            "qualification_sha256": owner["qualification"]["raw_sha256"]}
        _once(root/"product-origin-attestation.json", c.canonical(envelope))
        origin = {"task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"]["mac"],
            "attestation": c.binding(root/"product-origin-attestation.json", root)}
        _once(root/"source-origin.json", c.canonical(origin))
        return {"files": [c.binding(root/name, root) for name in ("process-session.json", "session-events.jsonl", "source-origin.json")],
                "origin": origin}

    def verify_product_origin(self, task: Mapping[str, Any], root: Path, provider: Any, origin: Mapping[str, Any]) -> None:
        actual = self._originals(task, root, provider)
        owner = self._owner(provider, task, root)
        expected = {"task_sha256": task["task_sha256"], "deployment": task["execution"]["source_deployments"]["mac"],
            "attestation": c.binding(root/"product-origin-attestation.json", root)}
        _require(dict(origin) == expected and c.read_json(root/"source-origin.json") == expected,
                 "产品三字段来源原件不符")
        envelope = c.read_json(c.bound_file(root, origin["attestation"]))
        _require(envelope == {"schema_version": "claude-mac-product-origin/v1", "task_sha256": task["task_sha256"],
            "native_attestation": c.binding(root/"product-native-attestation.json", root),
            "network_attestation": c.binding(root/"network-attestation.json", root),
            "dispatch": owner["services"]._dispatch.envelope_binding,
            "bridge_path_mapping": owner["paths"].mapping_binding,
            "qualification_sha256": owner["qualification"]["raw_sha256"]}, "产品来源必须实核两签名、原派发、映射及准确许可")
        origins.verify_process_session(task, root, actual)
        _require(authority.read_original(root/"session-events.jsonl") == b"".join(c.canonical(event)+b"\n"
            for result in actual["scenarios"] for event in result["session_events"]), "产品会话事件未命中真实原件")


def _once(path: Path, raw: bytes) -> None:
    if path.exists():
        _require(authority.read_original(path) == raw, "产品只写一次原件发生冲突")
    else:
        native._new_file(path, raw)


_MAC_SOURCE = QualifiedMacProductSource()


def create_qualified_source(task: Mapping[str, Any], *, bundle_root: Path, materials_receipt: Path,
                             path_context: Any, dispatch: Any) -> Any:
    """实际安装、资格、原签名派发和物料均通过后构造唯一产品实例；不启动。"""
    from tools.official_client_capture.claude_mac_product_completion import ProductEvidenceRecorder
    from tools.official_client_capture.claude_mac_product_provider import DarwinProductObservationProvider
    from tools.official_client_capture.claude_mac_product_recorder import RecorderStatePlan, StateSetupOperation
    from tools.official_client_capture.claude_mac_state_journal import SnapshotLimits, file_state
    frozen = copy.deepcopy(source.validate_task_v2(task))
    root = Path(bundle_root)
    _require(Path(materials_receipt) == root/"product-materials.json", "产品物料必须属于准确原件根")
    native._private_directory(root)
    _dispatch(frozen, dispatch, live=False)
    installed = authority.load_product_installation()
    network = flows.load_network_installation()
    root_broker = broker.load_product_broker(installed)
    qualification = _MAC_SOURCE._qualification(frozen, root, installed=installed, network=network,
        root_broker=root_broker, path_context=path_context, require_live=False)
    plan = recipes.derive_material_private_launch_plan(frozen, bundle_root=root,
        materials_receipt=Path(materials_receipt), path_context=path_context)
    session = recipes.physical_session_root(frozen, qualification["recipe"], root, path_context)
    native._private_directory(session)
    paths = recipes.material_state_paths(frozen, qualification["recipe"], root, path_context)
    limits = SnapshotLimits(**qualification["subject"]["snapshot_limits"])
    limits.validate()
    _require(set(qualification["subject"]["snapshot_limits"]) == set(asdict(limits)), "恢复上限必须精确冻结，不填默认缺项")
    services = _MacDispatchServices(frozen, dispatch, token=_TOKEN)
    current = c.SOURCE_ADAPTERS.get(c.MANAGED_RECORDER_ID)
    _require(current is None or current is _MAC_SOURCE, "当前进程已有其他正式来源实例，禁止覆盖登记")
    c.SOURCE_ADAPTERS[c.MANAGED_RECORDER_ID] = _MAC_SOURCE
    provider = None
    try:
        provider = DarwinProductObservationProvider(frozen, bundle_root=root, materials_receipt=Path(materials_receipt),
            authority=installed, path_context=path_context)
        owner = {"provider": provider, "task": frozen, "root": root, "native": installed, "network": network,
            "broker": root_broker, "paths": path_context, "services": services, "qualification": qualification}
        _MAC_SOURCE._owners[id(provider)] = owner
        operations = ()
        if frozen["client_id"] == "claude-vscode":
            raw = c.canonical(plan["private_user_data_settings"])
            operations = (StateSetupOperation("private-vscode-settings", {"user_data/User":
                {"type": "directory", "mode": 0o700, "mtime_ns": 1,
                 "children": {"settings.json": file_state(raw, mtime_ns=1)}}}),)
        state_plan = RecorderStatePlan(session, tuple(path.relative_to(session).as_posix() for path in paths), {},
                                       limits, operations, plan["plan"])
        recorder = ProductEvidenceRecorder(frozen, bundle_root=root, services=services, provider=provider,
            state_plan=state_plan, materials_receipt=Path(materials_receipt), path_context=path_context)
        owner["recorder"] = recorder
        recorder._guard(frozen, root)
        return recorder
    except BaseException:
        if provider is not None:
            _MAC_SOURCE._owners.pop(id(provider), None)
        if current is None and not _MAC_SOURCE._owners:
            c.SOURCE_ADAPTERS.pop(c.MANAGED_RECORDER_ID, None)
        raise


def refresh_capture_dispatch(recorder: Any, task: Mapping[str, Any], dispatch: Any) -> None:
    """受管桥刷新准确原任务的最新签名镜像；不能创建新预算或替换产品实例。"""
    _require(_MAC_SOURCE.owns_recorder(recorder) and dict(task) == recorder._task, "派发刷新缺原 recorder 所有权")
    _require(all(recorder._optional(name) is None for name in
                 ("product-origin-attestation.json", "product-preseal.json", "source-manifest.json")),
             "产品原件已经封存，不能替换其原签名派发镜像")
    services = recorder._services
    old = _dispatch(task, services._dispatch, live=False)
    current = _dispatch(task, dispatch, live=True)
    _require(all(old[key] == current[key] for key in
                 ("task_sha256", "ready", "budget_reservation", "capture_deployment", "mac_deployment", "deadline_utc")),
             "派发刷新不得更换 ready、预算、期限或双部署")
    services._dispatch = dispatch
