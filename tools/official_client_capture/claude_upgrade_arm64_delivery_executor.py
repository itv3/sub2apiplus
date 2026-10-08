"""Claude ARM64 隔离候选的固定交付执行器。

配置、数据根、凭据引用、Compose 服务及探针均显式冻结。只有独立守卫
服务能够执行应用切换；客户端封票、维持租约、接收原件并提交复核。
本模块提供能力，不登记正式执行器或来源；缺现场服务或可信回退时拒绝。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import io
import json
import os
import platform
import re
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, parse_json_bytes
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard

CONFIGURATION_SCHEMA = "claude-upgrade-arm64-executor-configuration/v1"
EXECUTOR_VERSION = "claude-upgrade-arm64-delivery-executor/v1"
GUARD_IDENTITY_SCHEMA = "claude-upgrade-delivery-guard-identity/v1"
AUTHORITY = "claude-arm64-recovery-guard"
CONFIGURATION_FIELDS = frozenset({"schema_version", "client", "host_id", "instance_id", "isolation", "source_scope", "paths", "runtime", "candidate", "rollback", "probes", "guard", "limits"})
CONTEXT_FIELDS = frozenset({"campaign_id", "candidate_id", "delivery_id", "segment", "run_nonce", "data_root", "configuration_ref"})
GUARD_IDENTITY_FIELDS = frozenset({"contract_version", "authority", "source_scope", "host_id", "instance_id", "service_name", "service_instance_id", "boot_id", "pid", "process_start", "process_group_id", "independent_service", "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"})
FREEZE_FIELDS = frozenset({"campaign_id", "candidate_id", "delivery_id", "candidate_image_digest", "architecture", "runtime_profile_sha256", "observation_window_seconds", "delivery_gates"})
DELIVERY_GATES = ("compose-diff", "dependency-connectivity", "entry-routing", "evidence-inventory", "health", "image-identity", "observation-window", "rollback-availability", "secret-scan")
EVIDENCE_TYPES = ("configuration", "stable_projection", "freeze", "rollback_state", "zero_model_probes", "deployment_configuration")
DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
SERVICE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


def _integer(value: Any, label: str, minimum: int = 1, maximum: int = 86400) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise guard.GuardError(f"{label} 范围非法")
    return value


def _binding_shape(value: Any, label: str) -> dict[str, Any]:
    item = guard.exact(value, {"path", "sha256", "bytes"}, label)
    guard.relative_path(item["path"], label)
    if not isinstance(item["sha256"], str) or not guard.SHA256.fullmatch(item["sha256"]) or item["sha256"] == "0" * 64:
        raise guard.GuardError(f"{label} 摘要非法")
    _integer(item["bytes"], label + ".bytes", minimum=0, maximum=1024 * 1024 * 1024)
    return item


def _ref_shape(value: Any, category: str, kind: str, label: str) -> dict[str, Any]:
    key = category + "_kind"
    item = guard.exact(value, {key, "sha256"}, label)
    if item[key] != kind or not isinstance(item["sha256"], str) or not guard.SHA256.fullmatch(item["sha256"]) or item["sha256"] == "0" * 64:
        raise guard.GuardError(f"{label} 来源引用非法")
    return item


def _qualification_shape(value: Any, label: str) -> dict[str, Any]:
    from tools.official_client_control.contracts import validate_fact_ref, validate_receipt_ref
    item = guard.exact(value, {"store_relative", "external_root_relative", "campaign_id", "candidate_id", "candidate_ref", "build_receipt_ref", "validation_ref", "acceptance_ref", "sealed_result_binding"}, label)
    for name in ("store_relative", "external_root_relative"):
        guard.relative_path(item[name], label + "." + name)
    for name in ("campaign_id", "candidate_id"):
        if not isinstance(item[name], str) or not guard.SAFE_ID.fullmatch(item[name]):
            raise guard.GuardError(f"{label}.{name} 非法")
    for name in ("candidate_ref", "validation_ref", "acceptance_ref"):
        try:
            fact = validate_fact_ref(item[name], label + "." + name)
        except Exception as error:
            raise guard.GuardError(f"{label}.{name} 不符合标准事实引用") from error
        if fact["campaign_id"] != item["campaign_id"] or fact["dimension"] != "validation":
            raise guard.GuardError("资格事实必须属于冻结Campaign验收维度")
    try:
        receipt = validate_receipt_ref(item["build_receipt_ref"], label + ".build_receipt_ref")
    except Exception as error:
        raise guard.GuardError("构建收据不符合标准引用") from error
    if receipt["receipt_kind"] != "candidate_build":
        raise guard.GuardError("资格构建收据种类不符")
    _binding_shape(item["sealed_result_binding"], label + ".sealed_result_binding")
    return item


def validate_configuration(configuration: Any) -> dict[str, Any]:
    """仅纯合同校验；不读取文件、不查询主机、不改变现场。"""
    value = guard.exact(configuration, CONFIGURATION_FIELDS, "执行器配置")
    if value["schema_version"] != CONFIGURATION_SCHEMA or value["client"] != "claude" or value["host_id"] != "ARM64" or value["isolation"] != "isolated_candidate" or value["source_scope"] not in {"formal", "regression_fixture"}:
        raise guard.GuardError("执行器仅允许 Claude ARM64 隔离候选配置")
    if not isinstance(value["instance_id"], str) or not guard.SAFE_ID.fullmatch(value["instance_id"]) or value["instance_id"].lower().startswith(("production", "dmit", "vircs")):
        raise guard.GuardError("隔离实例 ID 非法")
    paths = guard.exact(value["paths"], {"guard_root_relative", "active_profile_relative", "compose_binding"}, "受管路径")
    guard.relative_path(paths["guard_root_relative"])
    guard.relative_path(paths["active_profile_relative"])
    _binding_shape(paths["compose_binding"], "Compose绑定")
    runtime = guard.exact(value["runtime"], {"docker_binding", "daemon_socket", "compose_project", "application_service", "dependency_services", "profile_container_path", "expected_labels"}, "运行时")
    docker = guard.exact(runtime["docker_binding"], {"absolute_path", "sha256", "bytes"}, "Docker可执行文件")
    if not isinstance(docker["absolute_path"], str) or not Path(docker["absolute_path"]).is_absolute() or str(PurePosixPath(docker["absolute_path"])) != docker["absolute_path"] or ".." in PurePosixPath(docker["absolute_path"]).parts:
        raise guard.GuardError("Docker必须使用规范绝对路径")
    if not isinstance(docker["sha256"], str) or not guard.SHA256.fullmatch(docker["sha256"]) or docker["sha256"] == "0" * 64:
        raise guard.GuardError("Docker可执行文件摘要非法")
    _integer(docker["bytes"], "Docker长度", maximum=1024 * 1024 * 1024)
    if not isinstance(runtime["daemon_socket"], str) or runtime["daemon_socket"] != "/var/run/docker.sock":
        raise guard.GuardError("Docker只允许冻结的本机Unix daemon，不接受DOCKER_HOST")
    for name in ("compose_project", "application_service"):
        if not isinstance(runtime[name], str) or not SERVICE_NAME.fullmatch(runtime[name]) or runtime[name].lower().startswith(("production", "dmit", "vircs")):
            raise guard.GuardError(f"{name} 非法")
    dependencies = runtime["dependency_services"]
    if not isinstance(dependencies, list) or not dependencies or dependencies != sorted(set(dependencies)) or any(not isinstance(item, str) or not SERVICE_NAME.fullmatch(item) for item in dependencies) or runtime["application_service"] in dependencies:
        raise guard.GuardError("依赖服务必须为冻结、唯一且排序的闭集")
    container_path = runtime["profile_container_path"]
    if not isinstance(container_path, str) or not container_path.startswith("/") or str(PurePosixPath(container_path)) != container_path or ".." in PurePosixPath(container_path).parts or not container_path.endswith(".json"):
        raise guard.GuardError("容器Profile路径非法")
    labels = runtime["expected_labels"]
    if not isinstance(labels, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in labels.items()) or labels.get("sub2api.delivery.isolation") != "isolated_candidate" or labels.get("sub2api.delivery.instance") != value["instance_id"]:
        raise guard.GuardError("应用服务缺固定候选隔离标签")
    for name in ("candidate", "rollback"):
        material = guard.exact(value[name], {"image_digest", "runtime_profile_sha256", "profile_binding", "override_binding", "qualification"}, name)
        if not isinstance(material["image_digest"], str) or not guard.IMAGE.fullmatch(material["image_digest"]) or material["image_digest"] == "sha256:" + "0" * 64:
            raise guard.GuardError(f"{name} 镜像digest非法")
        for field in ("profile_binding", "override_binding"):
            _binding_shape(material[field], name + "." + field)
        if material["runtime_profile_sha256"] != material["profile_binding"]["sha256"]:
            raise guard.GuardError(f"{name} 必须绑定真实Profile原件，不能用operational摘要占位")
        _qualification_shape(material["qualification"], name + ".qualification")
        if material["profile_binding"]["path"] == paths["active_profile_relative"]:
            raise guard.GuardError("允许变更的active Profile不能同时充当冻结来源")
    candidate, rollback = value["candidate"], value["rollback"]
    if candidate["image_digest"] == rollback["image_digest"] or candidate["qualification"]["candidate_id"] == rollback["qualification"]["candidate_id"]:
        raise guard.GuardError("回退必须是独立基准Candidate与不同真实镜像")
    for name in ("store_relative", "external_root_relative"):
        if candidate["qualification"][name] != rollback["qualification"][name]:
            raise guard.GuardError("本轮只接受同一受管Store的完整基准来源图")
    probes = guard.exact(value["probes"], {"base_url", "credential_binding", "expected_models", "group_id", "account_ids", "database", "redis", "timeout_seconds", "max_requests"}, "零模型探针")
    url = urlsplit(probes["base_url"] if isinstance(probes["base_url"], str) else "")
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
        raise guard.GuardError("基础探针只连接冻结的宿主回环候选入口")
    try:
        _integer(url.port, "入口端口", maximum=65535)
    except ValueError as error:
        raise guard.GuardError("入口端口非法") from error
    _binding_shape(probes["credential_binding"], "候选专用Key绑定")
    models = probes["expected_models"]
    if not isinstance(models, list) or not models or any(not isinstance(item, str) or not MODEL_NAME.fullmatch(item) for item in models) or models != sorted(set(models)):
        raise guard.GuardError("期望模型须来自冻结真实数据，不允许空列表或重复")
    _integer(probes["group_id"], "候选分组ID", maximum=2**63 - 1)
    accounts = probes["account_ids"]
    if not isinstance(accounts, list) or not accounts or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in accounts) or accounts != sorted(set(accounts)):
        raise guard.GuardError("候选账号闭集非法")
    database = guard.exact(probes["database"], {"service", "user", "name", "api_key_id"}, "独立数据库")
    if database["service"] not in dependencies or any(not isinstance(database[item], str) or not DB_NAME.fullmatch(database[item]) for item in ("user", "name")):
        raise guard.GuardError("数据库服务或只读连接坐标非法")
    _integer(database["api_key_id"], "专用Key ID", maximum=2**63 - 1)
    redis = guard.exact(probes["redis"], {"service"}, "独立Redis")
    if redis["service"] not in dependencies or redis["service"] == database["service"]:
        raise guard.GuardError("独立Redis与数据库服务不闭合")
    _integer(probes["timeout_seconds"], "探针时限", maximum=120)
    _integer(probes["max_requests"], "最大HTTP探针数", minimum=3, maximum=3000)
    recovery = guard.exact(value["guard"], {"service_name", "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "socket_relative", "channel_key_binding"}, "独立守卫")
    if not isinstance(recovery["service_name"], str) or not re.fullmatch(r"claude-delivery-[A-Za-z0-9_-]{1,80}\.service", recovery["service_name"]):
        raise guard.GuardError("守卫只接受Claude候选专属service名")
    for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "channel_key_binding"):
        _binding_shape(recovery[name], "guard." + name)
    guard.relative_path(recovery["socket_relative"])
    if not recovery["socket_relative"].startswith(paths["guard_root_relative"] + "/"):
        raise guard.GuardError("守卫socket必须在冻结私有guard根")
    limits = guard.exact(value["limits"], {"lease_seconds", "command_timeout_seconds", "stage_timeout_seconds", "observation_window_seconds", "poll_seconds"}, "守卫时限")
    for name in limits:
        _integer(limits[name], name, maximum=86400)
    if limits["poll_seconds"] > limits["lease_seconds"] or limits["command_timeout_seconds"] >= limits["stage_timeout_seconds"] or limits["observation_window_seconds"] + 3 * probes["timeout_seconds"] >= limits["stage_timeout_seconds"]:
        raise guard.GuardError("阶段时限不足以覆盖冻结观察窗与有界探针")
    return copy.deepcopy(value)


def validate_execution_context(context: Any, configuration: dict[str, Any]) -> dict[str, Any]:
    value = guard.exact(context, CONTEXT_FIELDS, "执行上下文")
    for name in ("campaign_id", "candidate_id", "delivery_id", "segment", "run_nonce"):
        if not isinstance(value[name], str) or not guard.SAFE_ID.fullmatch(value[name]):
            raise guard.GuardError(f"执行上下文.{name}非法")
    root = value["data_root"]
    if not isinstance(root, str) or not Path(root).is_absolute() or str(PurePosixPath(root)) != root or ".." in PurePosixPath(root).parts or any(part.lower() in {"production", "release", "releases", "dmit", "vircs"} for part in PurePosixPath(root).parts):
        raise guard.GuardError("data_root必须是显式隔离候选绝对根")
    _binding_shape(value["configuration_ref"], "原始配置绑定")
    if value["candidate_id"] != configuration["candidate"]["qualification"]["candidate_id"] or value["campaign_id"] != configuration["candidate"]["qualification"]["campaign_id"]:
        raise guard.GuardError("执行上下文与当前Candidate资格图不符")
    return copy.deepcopy(value)


def stable_environment_projection(compose: Any, inspections: Any, application_service: str) -> dict[str, Any]:
    """稳定投影只消去容器实例坐标和唯一应用阶段身份。

    Docker的实例ID、PID、时间、sandbox/endpoint实例坐标属于容器实例；
    IP、网络ID、依赖镜像、挂载、卷、环境、命令与安全设置保留。
    """
    if not isinstance(compose, dict) or not isinstance(inspections, dict) or application_service not in inspections:
        raise guard.GuardError("稳定投影缺完整Compose或应用实测")
    configuration = copy.deepcopy(compose)
    services = configuration.get("services")
    if not isinstance(services, dict) or set(services) != set(inspections):
        raise guard.GuardError("稳定投影服务闭集不符")
    services[application_service].pop("image", None)
    services[application_service].get("labels", {}).pop("sub2api.delivery.stage", None)
    containers = {}
    for service, document in inspections.items():
        if not isinstance(document, dict):
            raise guard.GuardError("容器inspect必须为对象")
        item = copy.deepcopy(document)
        container_id = item.pop("Id", "")
        for key in ("Created", "RestartCount", "ResolvConfPath", "HostnamePath", "HostsPath", "LogPath"):
            item.pop(key, None)
        state = item.get("State", {})
        for key in ("Pid", "StartedAt", "FinishedAt", "Health"):
            state.pop(key, None)
        network = item.get("NetworkSettings", {})
        for key in ("SandboxID", "SandboxKey"):
            network.pop(key, None)
        for endpoint in network.get("Networks", {}).values():
            endpoint.pop("EndpointID", None)
            if isinstance(endpoint.get("Aliases"), list):
                endpoint["Aliases"] = [alias for alias in endpoint["Aliases"] if alias not in {container_id, str(container_id)[:12]}]
            if isinstance(endpoint.get("DNSNames"), list):
                endpoint["DNSNames"] = [name for name in endpoint["DNSNames"] if name not in {container_id, str(container_id)[:12]}]
        config = item.get("Config", {})
        if config.get("Hostname") in {container_id, str(container_id)[:12]}:
            config["Hostname"] = "<container-instance>"
        if service == application_service:
            item.pop("Image", None)
            item.pop("GraphDriver", None)
            config.pop("Image", None)
            config.get("Labels", {}).pop("sub2api.delivery.stage", None)
        containers[service] = item
    return {"schema_version": "claude-upgrade-stable-environment-projection/v1", "application_service": application_service, "compose": configuration, "containers": containers}


def _external_executable(specification: dict[str, Any]) -> Path:
    path = Path(specification["absolute_path"])
    if path.is_symlink() or not path.is_file():
        raise guard.GuardError("冻结Docker可执行文件不存在或为符号链接")
    raw = path.read_bytes()
    info = path.stat()
    if info.st_uid not in {0, os.getuid()} or info.st_mode & 0o022 or not os.access(path, os.X_OK) or len(raw) != specification["bytes"] or hashlib.sha256(raw).hexdigest() != specification["sha256"]:
        raise guard.GuardError("冻结Docker可执行文件字节、所有者或权限漂移")
    return path


class _QualificationStoreView:
    """严格只读的验收视图，禁止引入交付分支而反向调用当前guard。"""

    def __init__(self, store: Any):
        if store.read_only is not True:
            raise guard.GuardError("验收资格视图必须基于只读Store")
        self._store = store
        self.root = store.root
        self.read_only = True

    def load_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self._store.load_campaign(campaign_id)

    def list_facts(self, campaign_id: str, dimension: str | None = None) -> list[dict[str, Any]]:
        if dimension == "deployment":
            return []
        return [fact for fact in self._store.list_facts(campaign_id, dimension) if fact["dimension"] != "deployment"]

    def load_fact(self, reference: dict[str, Any]) -> dict[str, Any]:
        if reference.get("dimension") == "deployment":
            raise guard.GuardError("验收子图不接受交付或生产激活事实")
        return self._store.load_fact(reference)

    def load_object(self, reference: dict[str, Any]) -> dict[str, Any]:
        if str(reference.get("object_kind", "")).startswith("candidate_delivery"):
            raise guard.GuardError("验收子图不接受交付对象")
        document = self._store.load_object(reference)
        if document.get("payload", {}).get("schema_version") == "official-client-operational-evidence/v2":
            raise guard.GuardError("验收子图不接受真实交付执行证据")
        return document

    def object_bytes(self, reference: dict[str, Any]) -> bytes:
        self.load_object(reference)
        return self._store.object_bytes(reference)

    def list_object_refs(self, kind: str) -> list[dict[str, Any]]:
        if kind.startswith("candidate_delivery"):
            return []
        references = []
        for reference in self._store.list_object_refs(kind):
            document = self._store.load_object(reference)
            if document.get("payload", {}).get("schema_version") != "official-client-operational-evidence/v2":
                references.append(reference)
        return references

    def load_receipt(self, reference: dict[str, Any]) -> dict[str, Any]:
        if reference.get("receipt_kind") == "candidate_delivery":
            raise guard.GuardError("验收子图不接受交付收据")
        return self._store.load_receipt(reference)

    def list_receipt_refs(self, kind: str) -> list[dict[str, Any]]:
        return [] if kind == "candidate_delivery" else self._store.list_receipt_refs(kind)

    def fact_ref(self, fact: dict[str, Any]) -> dict[str, Any]:
        return self._store.fact_ref(fact)


def replay_qualification_graph(store: Any, qualification: dict[str, Any], external_root: Path, data_root: Path) -> dict[str, Any]:
    """形成期重放验收子图，不从guard反向重放本轮交付来源RPC。

    先核完整非deployment事实链，再展开资格根所有可达对象、收据和事实，
    逐条校验语义及外部字节。排除的是本轮物理交付分支，不排除验收证据。
    """
    from tools.official_client_control.contracts import iter_fact_refs, iter_object_refs, iter_receipt_refs
    from tools.official_client_control.gates import WorkflowGates
    from tools.official_client_control.receipts import replay_receipt
    original_store = store
    store = _QualificationStoreView(store)
    campaign = store.load_campaign(qualification["campaign_id"])
    facts = store.list_facts(qualification["campaign_id"])
    gates = WorkflowGates(store)
    gates.replay_campaign(campaign, facts)
    roots = [store.load_fact(qualification[name]) for name in ("candidate_ref", "validation_ref", "acceptance_ref")]
    roots.append(store.load_receipt(qualification["build_receipt_ref"]))
    seen: set[tuple[str, str]] = set()
    objects, receipts, references, external = [], [], [], {}
    pending = [campaign, *facts, *roots]
    while pending:
        document = pending.pop()
        for item in guard.all_bindings(document):
            path = guard.managed_path(external_root, item["path"])
            rebased = guard.binding(data_root, path)
            previous = external.get(item["path"])
            if previous is not None and previous != rebased:
                raise guard.GuardError("验收资格图的同一路径存在不同外部字节")
            original_store._verify_external_binding(external_root, item)
            external[item["path"]] = rebased
        for reference in iter_object_refs(document):
            key = ("object", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            gates.validate_object_graph(reference)
            pending.append(store.load_object(reference))
            objects.append(reference)
        for reference in iter_receipt_refs(document):
            key = ("receipt", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            replay_receipt(store, reference)
            pending.append(store.load_receipt(reference))
            receipts.append(reference)
        for reference in iter_fact_refs(document):
            key = ("fact", reference["sha256"])
            if key in seen:
                continue
            seen.add(key)
            fact = store.load_fact(reference)
            if fact["dimension"] == "deployment":
                raise guard.GuardError("回退验收资格不得依赖本轮物理交付或生产激活事实")
            pending.append(fact)
            references.append(reference)
    return {"schema_version": "claude-upgrade-candidate-qualification-replay/v1", "campaign_id": qualification["campaign_id"], "candidate_id": qualification["candidate_id"], "fact_refs": sorted(references, key=canonical_sha256), "object_refs": sorted(objects, key=canonical_sha256), "receipt_refs": sorted(receipts, key=canonical_sha256), "external_bindings": [external[key] for key in sorted(external)], "result": "pass"}


def guard_identity(configuration: dict[str, Any], context: dict[str, Any], *, formal: bool) -> dict[str, Any]:
    """从当前进程、实际service与已绑定安装字节取得身份，不接受自报pid。"""
    root = Path(context["data_root"])
    settings = configuration["guard"]
    for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding", "channel_key_binding"):
        guard.check_binding(root, settings[name])
    actual = guard.process_identity(os.getpid())
    if formal:
        if platform.system() != "Linux" or platform.machine().lower() not in {"arm64", "aarch64"} or configuration["source_scope"] != "formal":
            raise guard.GuardError("正式守卫必须实际运行在ARM64 Linux")
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != settings["executor_binding"]["sha256"] or hashlib.sha256(Path(guard.__file__).read_bytes()).hexdigest() != settings["guard_binding"]["sha256"]:
            raise guard.GuardError("实际已加载的守卫／执行器源码与部署绑定不符")
        service = settings["service_name"]
        command = "/usr/bin/systemctl" if Path("/usr/bin/systemctl").is_file() else "/bin/systemctl"
        result = subprocess.run([command, "show", service, "--property=MainPID,FragmentPath,KillMode,Restart"], check=True, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        unit = Path(fields.get("FragmentPath", ""))
        if fields.get("MainPID") != str(os.getpid()) or not unit.is_absolute() or unit.is_symlink() or not unit.is_file() or hashlib.sha256(unit.read_bytes()).hexdigest() != settings["service_unit_binding"]["sha256"]:
            raise guard.GuardError("当前守卫不是冻结systemd独立服务的MainPID")
        if fields.get("KillMode") != "control-group" or fields.get("Restart") not in {"always", "on-failure"}:
            raise guard.GuardError("守卫服务必须终止整个旧子进程组并配置异常重启")
        cgroup = Path("/proc/self/cgroup").read_text()
        if not any(line.rsplit(":", 1)[-1].rstrip("/").endswith("/" + service) for line in cgroup.splitlines()):
            raise guard.GuardError("守卫不在独立service生命周期中")
        enabled = subprocess.run([command, "is-enabled", service], check=True, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout.strip()
        if enabled != "enabled":
            raise guard.GuardError("守卫未配置宿主重启自动恢复")
        receipt = guard.read_json(guard.check_binding(root, settings["deployment_receipt_binding"]))
        machine = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
        expected = {"schema_version": "claude-upgrade-delivery-guard-deployment/v1", "host_id": "ARM64", "instance_id": configuration["instance_id"], "service_name": service, "machine_id_sha256": machine, "service_unit_sha256": settings["service_unit_binding"]["sha256"], "executor_sha256": settings["executor_binding"]["sha256"], "guard_sha256": settings["guard_binding"]["sha256"]}
        if any(receipt.get(key) != value for key, value in expected.items()) or not isinstance(receipt.get("restart_recovery_binding"), dict):
            raise guard.GuardError("缺完整受管安装／独立重启恢复部署收据")
        guard.check_binding(root, receipt["restart_recovery_binding"])
    else:
        if configuration["source_scope"] != "regression_fixture":
            raise guard.GuardError("本机fixture身份不能声称formal")
    result = {"contract_version": GUARD_IDENTITY_SCHEMA, "authority": AUTHORITY, "source_scope": configuration["source_scope"], "host_id": "ARM64", "instance_id": configuration["instance_id"], "service_name": settings["service_name"], "service_instance_id": canonical_sha256({"configuration": canonical_sha256(configuration), "process": actual}), **actual, "independent_service": formal, **{key: settings[key] for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")}}
    guard.exact(result, GUARD_IDENTITY_FIELDS, "guard身份")
    return result


class DockerComposeRuntime:
    """固定Docker命令、只读数据库和零模型HTTP；没有任意shell入口。"""

    def __init__(self, configuration: dict[str, Any], context: dict[str, Any]):
        self.configuration = validate_configuration(configuration)
        self.context = validate_execution_context(context, configuration)
        self.root = Path(context["data_root"])
        self.runtime = configuration["runtime"]
        self.probes = configuration["probes"]
        self.docker = _external_executable(self.runtime["docker_binding"])
        self._credential: bytes | None = None
        self._compose_cache: dict[str, Any] = {}

    def _command(self, arguments: list[str], *, checkpoint: Any = None, input_bytes: bytes | None = None) -> bytes:
        _external_executable(self.runtime["docker_binding"])
        prefix = [str(self.docker), "--host", "unix://" + self.runtime["daemon_socket"]]
        process = subprocess.Popen(prefix + arguments, shell=False, cwd=self.root, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + self.configuration["limits"]["command_timeout_seconds"]
        first = True
        try:
            while True:
                if checkpoint:
                    checkpoint()
                if time.monotonic() >= deadline:
                    raise guard.GuardError("固定Docker操作超时")
                try:
                    output, error = process.communicate(input=input_bytes if first else None, timeout=0.2)
                    if process.returncode != 0:
                        # 不将stderr或凭据回显进公共材料；调用坐标在固定原件中。
                        raise guard.GuardError(f"固定Docker操作失败，退出码{process.returncode}")
                    if len(output) > 32 * 1024 * 1024:
                        raise guard.GuardError("Docker原件超出冻结长度限制")
                    return output
                except subprocess.TimeoutExpired:
                    first = False
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
            raise

    def _compose(self, material: str = "candidate") -> list[str]:
        base = guard.check_binding(self.root, self.configuration["paths"]["compose_binding"])
        override = guard.check_binding(self.root, self.configuration[material]["override_binding"])
        return ["compose", "--env-file", "/dev/null", "--project-name", self.runtime["compose_project"], "--file", str(base), "--file", str(override)]

    def _compose_document(self, material: str) -> dict[str, Any]:
        raw = self._command(self._compose(material) + ["config", "--format", "json"])
        value = parse_json_bytes(raw)
        if not isinstance(value, dict):
            raise guard.GuardError("Compose完整配置不是对象")
        services = value.get("services")
        expected = {self.runtime["application_service"], *self.runtime["dependency_services"]}
        if not isinstance(services, dict) or set(services) != expected:
            raise guard.GuardError("Compose服务闭集不符")
        app = services[self.runtime["application_service"]]
        if app.get("image", "").rsplit("@", 1)[-1] != self.configuration[material]["image_digest"]:
            raise guard.GuardError("Compose应用镜像没有冻结digest")
        if any(app.get("labels", {}).get(key) != item for key, item in self.runtime["expected_labels"].items()):
            raise guard.GuardError("Compose应用隔离标签不符")
        for name, service in services.items():
            if service.get("privileged") is True or service.get("network_mode") == "host" or service.get("pid") == "host" or service.get("ipc") == "host" or service.get("cap_add"):
                raise guard.GuardError("候选服务存在未允许的宿主权限")
            for mount in service.get("volumes", []):
                if not isinstance(mount, dict):
                    raise guard.GuardError("Compose挂载必须解析为完整对象")
                if mount.get("type") == "bind":
                    source = Path(mount.get("source", ""))
                    try:
                        relative = source.relative_to(self.root).as_posix()
                    except ValueError as error:
                        raise guard.GuardError("候选bind挂载越出隔离data_root") from error
                    guard.managed_path(self.root, relative)
                elif mount.get("type") != "volume":
                    raise guard.GuardError("未冻结挂载类型")
            if not service.get("networks") or any(not isinstance(network, dict) or not network.get("ipv4_address") for network in service["networks"].values()):
                raise guard.GuardError("所有候选服务必须冻结网络和固定IP")
        for volume in value.get("volumes", {}).values():
            if volume.get("external") or not str(volume.get("name", "")).startswith(self.runtime["compose_project"] + "_"):
                raise guard.GuardError("持久卷不是候选专属卷")
        for network in value.get("networks", {}).values():
            if network.get("external") or not str(network.get("name", "")).startswith(self.runtime["compose_project"] + "_"):
                raise guard.GuardError("网络不是候选专属网络")
        port = urlsplit(self.probes["base_url"]).port
        if not any(str(item.get("published", "")) == str(port) and item.get("host_ip") in {"127.0.0.1", "::1"} for item in app.get("ports", []) if isinstance(item, dict)):
            raise guard.GuardError("冻结HTTP入口没有映射到唯一应用容器")
        return value

    def check_qualification(self) -> dict[str, Any]:
        """只读重放同Store的两份资格图，禁止自报验收、旧Profile占位。"""
        from tools.official_client_control.store import ControlStore
        from tools.official_client_control.receipts import replay_receipt
        first = self.configuration["candidate"]["qualification"]
        store = ControlStore.open_read_only(guard.managed_path(self.root, first["store_relative"]))
        reports = []
        for name in ("candidate", "rollback"):
            material = self.configuration[name]
            q = material["qualification"]
            candidate = store.load_fact(q["candidate_ref"])
            validation = store.load_fact(q["validation_ref"])
            acceptance = store.load_fact(q["acceptance_ref"])
            build = store.load_receipt(q["build_receipt_ref"])
            replay_receipt(store, q["build_receipt_ref"])
            reports.append(replay_qualification_graph(store, q, guard.managed_path(self.root, q["external_root_relative"]), self.root))
            if candidate["fact_kind"] != "candidate_frozen" or validation["fact_kind"] != "validation_completed" or acceptance["fact_kind"] != "acceptance_recorded":
                raise guard.GuardError("资格事实类型不符")
            if any(fact["campaign_id"] != q["campaign_id"] for fact in (candidate, validation, acceptance)):
                raise guard.GuardError("资格图跨Campaign裸引用")
            for fact in (candidate, validation, acceptance):
                if fact["payload"].get("candidate_id") != q["candidate_id"]:
                    raise guard.GuardError("资格图Candidate ID不一致")
            expected = {"candidate_ref": q["candidate_ref"], "candidate_build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"]}
            if any(validation["payload"].get(key) != item for key, item in expected.items()) or validation["payload"].get("result") != "passed":
                raise guard.GuardError("资格图缺完整VC-5验收与封存链")
            if candidate["payload"].get("image_digest") != material["image_digest"] or build.get("image", {}).get("image_digest") != material["image_digest"] or build.get("profile_identity", {}).get("profile_digest") != material["runtime_profile_sha256"]:
                raise guard.GuardError("资格图镜像／真实Profile与配置不符")
            if acceptance["payload"].get("candidate_ref") != q["candidate_ref"] or acceptance["payload"].get("candidate_build_receipt_ref") != q["build_receipt_ref"]:
                raise guard.GuardError("验收来源没有绑定真实构建")
            sealed = guard.read_json(guard.check_binding(self.root, q["sealed_result_binding"]))
            sealed_store = sealed.get("store") if isinstance(sealed.get("store"), dict) else {}
            if sealed.get("schema_version") != "claude-upgrade-acceptance/v1" or sealed.get("candidate_id") != q["candidate_id"] or sealed_store.get("acceptance_ref") != q["acceptance_ref"] or sealed_store.get("validation_completed_ref") != q["validation_ref"] or sealed.get("production_state") != "not_activated":
                raise guard.GuardError("缺独立已封存的候选终态原件")
            guard.check_binding(self.root, material["profile_binding"])
            guard.check_binding(self.root, material["override_binding"])
        credential = guard.read_bytes(guard.check_binding(self.root, self.probes["credential_binding"])).strip()
        if not credential or b"\n" in credential or b"\r" in credential or len(credential) > 256:
            raise guard.GuardError("候选专用Key原件非法")
        self._credential = credential
        candidate_compose = self._compose_document("candidate")
        rollback_compose = self._compose_document("rollback")
        raw_candidate, raw_rollback = copy.deepcopy(candidate_compose), copy.deepcopy(rollback_compose)
        for doc in (candidate_compose, rollback_compose):
            doc["services"][self.runtime["application_service"]].pop("image", None)
            doc["services"][self.runtime["application_service"]].get("labels", {}).pop("sub2api.delivery.stage", None)
        if candidate_compose != rollback_compose:
            raise guard.GuardError("两份Compose仅可改变唯一应用镜像／阶段标签")
        self._compose_cache = candidate_compose
        report = {"schema_version": "claude-upgrade-delivery-qualification/v1", "candidate": self.configuration["candidate"], "rollback": self.configuration["rollback"], "qualification_replays": reports, "candidate_compose": raw_candidate, "rollback_compose": raw_rollback, "normalized_compose": candidate_compose, "allowed_changes": ["application_image", "application_stage_label", "active_runtime_profile"], "result": "pass"}
        self._qualification_report = report
        return report

    def _inspect(self, directory: Path, checkpoint: Any = None) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], str]:
        guard.private_directory(directory)
        inspections, originals = {}, []
        for service in [self.runtime["application_service"], *self.runtime["dependency_services"]]:
            ids = self._command(self._compose() + ["ps", "--all", "--quiet", service], checkpoint=checkpoint).decode("ascii").split()
            if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{12,64}", ids[0]):
                raise guard.GuardError("候选服务必须有唯一真实容器")
            raw = self._command(["container", "inspect", ids[0]], checkpoint=checkpoint)
            parsed = parse_json_bytes(raw)
            if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
                raise guard.GuardError("Docker inspect返回非法")
            labels = parsed[0].get("Config", {}).get("Labels", {})
            if labels.get("com.docker.compose.project") != self.runtime["compose_project"] or labels.get("com.docker.compose.service") != service:
                raise guard.GuardError("实际容器不属于冻结候选project／service")
            if service == self.runtime["application_service"] and any(labels.get(key) != item for key, item in self.runtime["expected_labels"].items()):
                raise guard.GuardError("实际应用缺候选隔离身份")
            path = directory / (service + "-inspect.json")
            guard.write_once(path, raw)
            originals.append(guard.binding(self.root, path))
            inspections[service] = parsed[0]
        app = inspections[self.runtime["application_service"]]
        container_id = app["Id"]
        config_image = app.get("Config", {}).get("Image", "")
        image_raw = self._command(["image", "inspect", app["Image"]], checkpoint=checkpoint)
        image = parse_json_bytes(image_raw)
        image_path = directory / "application-image-inspect.json"
        guard.write_once(image_path, image_raw)
        originals.append(guard.binding(self.root, image_path))
        # 平台只能来自本次真实镜像原件；缺字段或架构不符时不得以宿主名倒填。
        if not isinstance(image, list) or len(image) != 1 or not isinstance(image[0], dict) or image[0].get("Os") != "linux" or image[0].get("Architecture") not in {"arm64", "aarch64"}:
            raise guard.GuardError("真实镜像原件缺少linux ARM64平台，不能继续交付")
        architecture = "arm64" if image[0]["Architecture"] == "aarch64" else image[0]["Architecture"]
        image_platform = image[0]["Os"] + "/" + architecture
        digest = config_image.rsplit("@", 1)[-1]
        if not guard.IMAGE.fullmatch(digest) or not isinstance(image, list) or len(image) != 1 or image[0].get("Id") != app["Image"] or config_image not in image[0].get("RepoDigests", []):
            image_actual = {"state": "unknown", "reason": "容器镜像未取得一致的真实RepoDigest原件"}
        else:
            identity_path = directory / "image-identity.json"
            guard.write_json_once(identity_path, {"image_digest": digest, "container_inspect_binding": originals[0], "image_inspect_binding": guard.binding(self.root, image_path)})
            image_actual = {"state": "known", "value": digest, "direct_binding": guard.binding(self.root, identity_path)}
        tar_bytes = self._command(["container", "cp", container_id + ":" + self.runtime["profile_container_path"], "-"], checkpoint=checkpoint)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) != 1 or not members[0].isfile() or members[0].size > 4 * 1024 * 1024 or PurePosixPath(members[0].name).name != PurePosixPath(self.runtime["profile_container_path"]).name:
                raise guard.GuardError("容器Profile归档结构非法")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise guard.GuardError("真实Profile不可读取")
            profile_bytes = stream.read()
        # 原始Profile也严格解析；其实际字节摘要不由目标倒填。
        parse_json_bytes(profile_bytes, "真实运行Profile")
        profile_path = directory / "runtime-profile.json"
        guard.write_once(profile_path, profile_bytes)
        profile_identity_path = directory / "profile-identity.json"
        guard.write_json_once(profile_identity_path, {"runtime_profile_sha256": hashlib.sha256(profile_bytes).hexdigest(), "profile_binding": guard.binding(self.root, profile_path)})
        actual = {"image": image_actual, "profile": {"state": "known", "value": hashlib.sha256(profile_bytes).hexdigest(), "direct_binding": guard.binding(self.root, profile_identity_path)}}
        originals.extend([guard.binding(self.root, profile_path), guard.binding(self.root, profile_identity_path)])
        if not self._compose_cache:
            self._compose_cache = self._compose_document("candidate")
            self._compose_cache["services"][self.runtime["application_service"]].pop("image", None)
            self._compose_cache["services"][self.runtime["application_service"]].get("labels", {}).pop("sub2api.delivery.stage", None)
        projection = stable_environment_projection(self._compose_cache, inspections, self.runtime["application_service"])
        projection_path = directory / "stable-projection.json"
        guard.write_json_once(projection_path, projection)
        originals.append(guard.binding(self.root, projection_path))
        environment = {"host_id": "ARM64", "architecture": image_platform, "compose_sha256": self.configuration["paths"]["compose_binding"]["sha256"], "environment_sha256": canonical_sha256(projection), "configuration_sha256": canonical_sha256(self.configuration), "network_sha256": canonical_sha256({name: item.get("NetworkSettings", {}).get("Networks", {}) for name, item in projection["containers"].items()})}
        return actual, environment, originals, container_id

    def before(self, directory: Path, expected_environment: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        actual, environment, originals, _ = self._inspect(directory / "before")
        if expected_environment is not None and environment != expected_environment:
            raise guard.GuardError("armed前实测环境与冻结计划漂移")
        allowed = {(self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"]) for name in ("candidate", "rollback")}
        if actual["image"].get("state") != "known" or actual["profile"].get("state") != "known" or (actual["image"].get("value"), actual["profile"].get("value")) not in allowed:
            raise guard.GuardError("armed前应用身份不属于两份已验证Candidate")
        active = guard.managed_path(self.root, self.configuration["paths"]["active_profile_relative"])
        path = directory / "before" / "host-active-profile.json"
        guard.write_once(path, guard.read_bytes(active))
        if hashlib.sha256(guard.read_bytes(path)).hexdigest() != actual["profile"]["value"]:
            raise guard.GuardError("armed前宿主Profile与容器实测不符")
        originals.append(guard.binding(self.root, path))
        return originals

    def failed_observation(self, fallback: dict[str, Any], directory: Path) -> dict[str, Any]:
        """只读取本次已取得原件，异常时不重新运行或倒填目标实测。"""
        during = directory / "during"
        if not during.exists():
            return fallback
        for name, filename, field in (("image", "image-identity.json", "image_digest"), ("profile", "profile-identity.json", "runtime_profile_sha256")):
            candidates = sorted(during.rglob(filename))
            if candidates:
                path = candidates[-1]
                raw = guard.read_json(path)
                fallback["actual"][name] = {"state": "known", "value": raw[field], "direct_binding": guard.binding(self.root, path)}
        projections = sorted(during.rglob("stable-projection.json"))
        if projections:
            projection = guard.read_json(projections[-1])
            fallback["environment"] = {"host_id": "ARM64", "architecture": "arm64", "compose_sha256": self.configuration["paths"]["compose_binding"]["sha256"], "environment_sha256": canonical_sha256(projection), "configuration_sha256": canonical_sha256(self.configuration), "network_sha256": canonical_sha256({name: item.get("NetworkSettings", {}).get("Networks", {}) for name, item in projection["containers"].items()})}
        request_records = []
        for path in sorted(during.rglob("http-*-request.json"), key=lambda item: int(item.name.split("-")[1])):
            request = guard.read_json(path)
            response_path = path.with_name(path.name.replace("-request.json", "-response.json"))
            request_records.append({"method": request["method"], "path": request["path"], "request_binding": guard.binding(self.root, path), "response_binding": guard.binding(self.root, response_path) if response_path.exists() else None})
        fallback["request_records"] = request_records
        fallback["during_bindings"] = [guard.binding(self.root, path) for path in sorted(during.rglob("*")) if path.is_file()]
        fallback["after_bindings"] = all_bindings_for_records(request_records)
        return fallback

    def _http(self, directory: Path, authenticated: bool, path: str, records: list[dict[str, Any]], checkpoint: Any = None) -> tuple[int, Any]:
        if path not in {"/health", "/v1/models"} or (authenticated and path != "/v1/models"):
            raise guard.GuardError("HTTP探针不在固定method/path闭集")
        if len(records) >= self.probes["max_requests"]:
            raise guard.GuardError("HTTP探针达到冻结次数上限")
        if checkpoint:
            checkpoint()
        url = urlsplit(self.probes["base_url"])
        number = len(records) + 1
        request_path = directory / f"http-{number:06d}-request.json"
        request = {"method": "GET", "path": path, "base_url": self.probes["base_url"], "credential": "candidate_private_ref" if authenticated else "none"}
        guard.write_json_once(request_path, request)
        record = {"method": "GET", "path": path, "request_binding": guard.binding(self.root, request_path), "response_binding": None}
        records.append(record)
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=self.probes["timeout_seconds"])
        try:
            headers = {"Accept": "application/json", "User-Agent": "Claude-Candidate-Delivery/1"}
            if authenticated:
                if self._credential is None:
                    raise guard.GuardError("候选专用凭据未通过准入")
                headers["Authorization"] = "Bearer " + self._credential.decode("ascii")
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            content = response.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024 or (self._credential and self._credential in content):
                raise guard.GuardError("HTTP响应过大或回显私有凭据")
            parsed = parse_json_bytes(content) if content else None
            response_path = directory / f"http-{number:06d}-response.json"
            # 只保存固定安全响应字段，不录Set-Cookie、Authorization或凭据明文。
            document = {"status": response.status, "body": parsed, "redirect_followed": False, "location_present": bool(response.getheader("Location"))}
            guard.write_json_once(response_path, document)
            record["response_binding"] = guard.binding(self.root, response_path)
            if 300 <= response.status < 400:
                raise guard.GuardError("候选入口返回redirect；禁止跟随")
            if checkpoint:
                checkpoint()
            return response.status, parsed
        finally:
            connection.close()

    def _database(self, directory: Path, checkpoint: Any = None) -> tuple[bool, list[dict[str, Any]]]:
        db = self.probes["database"]
        group_id, key_id = self.probes["group_id"], db["api_key_id"]
        # 只允许固定只读事务；账号credentials、extra和原始key不进入响应。
        sql = f"""BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5000ms';
SELECT json_build_object(
 'group', (SELECT json_build_object('id', id, 'status', status, 'model_allowlist', model_allowlist) FROM groups WHERE id={group_id} AND deleted_at IS NULL),
 'accounts', (SELECT coalesce(json_agg(json_build_object('id', a.id, 'platform', a.platform, 'status', a.status) ORDER BY a.id), '[]'::json) FROM accounts a JOIN account_groups ag ON ag.account_id=a.id WHERE ag.group_id={group_id} AND a.deleted_at IS NULL),
 'key', (SELECT json_build_object('id', id, 'group_id', group_id, 'status', status, 'key_sha256', encode(sha256(convert_to(key, 'UTF8')), 'hex')) FROM api_keys WHERE id={key_id} AND deleted_at IS NULL));
ROLLBACK;
""".encode("ascii")
        raw = self._command(self._compose() + ["exec", "--no-TTY", db["service"], "psql", "--username", db["user"], "--dbname", db["name"], "--no-password", "--no-psqlrc", "--tuples-only", "--no-align", "--set", "ON_ERROR_STOP=1", "--quiet"], input_bytes=sql, checkpoint=checkpoint)
        document = parse_json_bytes(raw.strip(), "只读数据库事实")
        if not isinstance(document, dict):
            raise guard.GuardError("只读数据库响应非法")
        sql_path = directory / "database-readonly.sql"
        data_path = directory / "database-facts.json"
        guard.write_once(sql_path, sql)
        guard.write_json_once(data_path, document)
        group = document.get("group") or {}
        accounts = document.get("accounts") or []
        key = document.get("key") or {}
        allowlist = group.get("model_allowlist")
        expected = self.probes["expected_models"]
        models = sorted(allowlist["models"]) if isinstance(allowlist, dict) and allowlist.get("enabled") is True and isinstance(allowlist.get("models"), list) and all(isinstance(model, str) for model in allowlist["models"]) else []
        success = group.get("id") == self.probes["group_id"] and group.get("status") == "active" and models == expected and isinstance(accounts, list) and sorted(item.get("id") for item in accounts) == self.probes["account_ids"] and all(item.get("status") == "active" and item.get("platform") == "anthropic" for item in accounts) and key.get("id") == key_id and key.get("group_id") == self.probes["group_id"] and key.get("status") == "active" and self._credential is not None and key.get("key_sha256") == hashlib.sha256(self._credential).hexdigest()
        return bool(success), [guard.binding(self.root, sql_path), guard.binding(self.root, data_path)]

    def _business(self, directory: Path, records: list[dict[str, Any]], checkpoint: Any = None) -> tuple[dict[str, bool], list[dict[str, Any]]]:
        guard.private_directory(directory)
        health_status, health_body = self._http(directory, False, "/health", records, checkpoint)
        models_status, models_body = self._http(directory, True, "/v1/models", records, checkpoint)
        rejected_status, _ = self._http(directory, False, "/v1/models", records, checkpoint)
        models = models_body.get("data") if isinstance(models_body, dict) else None
        model_pass = models_status == 200 and isinstance(models, list) and all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in models) and sorted(item["id"] for item in models) == self.probes["expected_models"] and models_body.get("object") == "list"
        data_pass, bindings = self._database(directory, checkpoint)
        redis_raw = self._command(self._compose() + ["exec", "--no-TTY", self.probes["redis"]["service"], "redis-cli", "PING"], checkpoint=checkpoint)
        redis_path = directory / "redis-ping.txt"
        guard.write_once(redis_path, redis_raw)
        bindings.append(guard.binding(self.root, redis_path))
        return {"health": health_status == 200 and isinstance(health_body, dict) and health_body.get("status") in {"ok", "healthy"}, "auth": model_pass and rejected_status in {401, 403}, "data": data_pass, "dependencies": data_pass and redis_raw.strip() == b"PONG", "entry-routing": model_pass and data_pass, "smoke": model_pass and data_pass and rejected_status in {401, 403}}, bindings

    def _switch(self, material: str, checkpoint: Any = None) -> None:
        if checkpoint:
            checkpoint()
        source = guard.check_binding(self.root, self.configuration[material]["profile_binding"])
        active = guard.managed_path(self.root, self.configuration["paths"]["active_profile_relative"])
        guard.private_directory(active.parent)
        temporary = active.parent / (".profile-" + hashlib.sha256(guard.read_bytes(source)).hexdigest())
        guard.write_once(temporary, guard.read_bytes(source))
        os.replace(temporary, active)
        guard.sync_directory(active.parent)
        if checkpoint:
            checkpoint()
        self._command(self._compose(material) + ["up", "--no-deps", "--detach", "--no-build", "--force-recreate", self.runtime["application_service"]], checkpoint=checkpoint)

    def _observe(self, stage: str, target: dict[str, Any], expected_environment: dict[str, Any], directory: Path, checkpoint: Any = None) -> dict[str, Any]:
        started = time.monotonic()
        records: list[dict[str, Any]] = []
        actual, environment, originals, _ = self._inspect(directory / "identity", checkpoint)
        business, business_bindings = self._business(directory / "business", records, checkpoint)
        originals.extend(business_bindings)
        image_pass = actual["image"].get("state") == "known" and actual["image"].get("value") == target["image_digest"]
        profile_pass = actual["profile"].get("state") == "known" and actual["profile"].get("value") == target["runtime_profile_sha256"]
        stable_pass = environment == expected_environment
        values = {**business, "image-identity": image_pass, "profile": profile_pass, "identity": image_pass and profile_pass and stable_pass, "release-isolation": stable_pass, "safety": stable_pass and all(business.values()), "state": stable_pass and all(business.values())}
        if stage == "stable_observed":
            deadline = started + self.configuration["limits"]["observation_window_seconds"]
            number = 0
            while time.monotonic() < deadline:
                if checkpoint:
                    checkpoint()
                time.sleep(min(self.configuration["limits"]["poll_seconds"], max(0, deadline - time.monotonic())))
                number += 1
                current_actual, current_environment, more, _ = self._inspect(directory / f"window-{number:06d}" / "identity", checkpoint)
                current_business, more_business = self._business(directory / f"window-{number:06d}" / "business", records, checkpoint)
                originals.extend(more + more_business)
                stable_pass = stable_pass and current_environment == expected_environment
                identity_pass = all(current_actual[key].get("state") == "known" and current_actual[key].get("value") == target[target_key] for key, target_key in (("image", "image_digest"), ("profile", "runtime_profile_sha256")))
                values["identity"] = values["identity"] and identity_pass and stable_pass
                values["data"] = values["data"] and current_business["data"]
                values["release-isolation"] = values["release-isolation"] and stable_pass
                values["safety"] = values["safety"] and all(current_business.values()) and stable_pass
                values["state"] = values["state"] and stable_pass and identity_pass
                actual, environment = current_actual, current_environment
        proof_bindings = sorted(guard.all_bindings([originals, records]), key=lambda item: item["path"])
        check_report = {"schema_version": "claude-upgrade-delivery-stage-checks/v1", "stage": stage, "target": target, "actual": actual, "expected_environment": expected_environment, "actual_environment": environment, "probe_results": business, "result_by_check": {name: values[name] for name in guard.STAGE_CHECKS[stage]}, "bindings": proof_bindings}
        check_path = directory / "checks.json"
        guard.write_json_once(check_path, check_report)
        check_binding = guard.binding(self.root, check_path)
        originals.append(check_binding)
        checks = [{"id": name, "result": "pass" if values[name] else "failed", "facts": {"source": "fixed-readonly-probes", "check_report_binding": check_binding, "bindings": proof_bindings}} for name in guard.STAGE_CHECKS[stage]]
        passed = image_pass and profile_pass and stable_pass and all(item["result"] == "pass" for item in checks)
        duration = time.monotonic() - started
        timing_path = directory / "timing.json"
        guard.write_json_once(timing_path, {"stage": stage, "actual_elapsed_seconds": duration, "observation_window_seconds": self.configuration["limits"]["observation_window_seconds"] if stage == "stable_observed" else 0})
        originals.append(guard.binding(self.root, timing_path))
        return {"stage": stage, "target": target, "actual": actual, "environment": environment, "checks": checks, "observed_seconds": int(duration) if stage == "stable_observed" else 0, "live_request_count": 0, "request_records": records, "before_bindings": [], "during_bindings": originals, "after_bindings": all_bindings_for_records(records), "result": "pass" if passed else "failed"}

    def execute(self, stage: str, target: dict[str, Any], environment: dict[str, Any], directory: Path, checkpoint: Any) -> dict[str, Any]:
        qualification = self.check_qualification()
        qualification_path = directory / "qualification.json"
        guard.write_json_once(qualification_path, qualification)
        started = time.monotonic()
        def bounded() -> None:
            checkpoint()
            if time.monotonic() - started > self.configuration["limits"]["stage_timeout_seconds"]:
                raise guard.GuardError("单阶段超过冻结时限")
        if stage != "stable_observed":
            self._switch("rollback" if stage == "rollback_verified" else "candidate", bounded)
        observation = self._observe(stage, target, environment, directory / "during", bounded)
        observation["during_bindings"].append(guard.binding(self.root, qualification_path))
        return observation

    def restore(self, target: dict[str, Any], environment: dict[str, Any], directory: Path) -> dict[str, Any]:
        # 恢复不依赖失效worker租约；仍受每个固定命令与完整阶段时限约束。
        started = time.monotonic()
        def bounded() -> None:
            if time.monotonic() - started > self.configuration["limits"]["stage_timeout_seconds"]:
                raise guard.GuardError("恢复超过冻结时限")
        self._credential = guard.read_bytes(guard.check_binding(self.root, self.probes["credential_binding"])).strip()
        self._switch("rollback", bounded)
        observation = self._observe("rollback_verified", target, environment, directory / "observed", bounded)
        return {"target": target, "actual": observation["actual"], "checks": observation["checks"], "bindings": [*observation["during_bindings"], *observation["after_bindings"]], "result": observation["result"]}

    def freeze(self, request: dict[str, Any], directory: Path) -> dict[str, Any]:
        qualification = self.check_qualification()
        qualification_path = directory / "qualification.json"
        guard.write_json_once(qualification_path, qualification)
        guard.check_binding(self.root, self.context["configuration_ref"])
        if guard.read_json(guard.check_binding(self.root, self.context["configuration_ref"])) != self.configuration:
            raise guard.GuardError("raw配置binding与有效配置不一致")
        actual, environment, originals, _ = self._inspect(directory / "before")
        records: list[dict[str, Any]] = []
        business, business_bindings = self._business(directory / "business", records)
        originals.extend(business_bindings + all_bindings_for_records(records))
        qualification_binding = guard.binding(self.root, qualification_path)
        originals.append(qualification_binding)
        snapshot = guard.read_json(directory / "before" / "stable-projection.json")
        common = {"campaign_id": self.context["campaign_id"], "delivery_id": self.context["delivery_id"], "result": "pass"}
        inventory_path = directory / "inventory.json"
        guard.write_json_once(inventory_path, {"bindings": sorted(guard.all_bindings(originals), key=lambda item: item["path"]), "all_bytes_revalidated": all(guard.check_binding(self.root, item) for item in guard.all_bindings(originals))})
        inventory_binding = guard.binding(self.root, inventory_path)
        originals.append(inventory_binding)
        rollback = self.configuration["rollback"]
        q = rollback["qualification"]
        from tools.official_client_control.store import ControlStore
        store = ControlStore.open_read_only(guard.managed_path(self.root, q["store_relative"]))
        acceptance = store.load_fact(q["acceptance_ref"])
        probe_recipe = {"schema_version": "claude-upgrade-zero-model-probe-recipe/v1", "allowed_requests": [{"method": "GET", "path": "/health", "authentication": "none"}, {"method": "GET", "path": "/v1/models", "authentication": "candidate_private_ref"}, {"method": "GET", "path": "/v1/models", "authentication": "none"}], "group_id": self.probes["group_id"], "account_ids": self.probes["account_ids"], "expected_models": self.probes["expected_models"], "max_requests": self.probes["max_requests"], "timeout_seconds": self.probes["timeout_seconds"], "redirect": "reject", "database": "fixed_read_only_transaction", "redis": "PING"}
        recipe_path = directory / "probe-recipe.json"
        guard.write_json_once(recipe_path, probe_recipe)
        cores = {
            "configuration": {"configuration": self.configuration, "configuration_sha256": canonical_sha256(self.configuration), "binding": self.context["configuration_ref"]},
            "stable_projection": {"projection": snapshot, "projection_sha256": canonical_sha256(snapshot), "binding": guard.binding(self.root, directory / "before" / "stable-projection.json")},
            "freeze": {"environment": environment, "before_bindings": originals, "gates": [], "guard_identity": {}},
            "rollback_state": {"candidate_ref": q["candidate_ref"], "build_receipt_ref": q["build_receipt_ref"], "acceptance_ref": q["acceptance_ref"], "evidence_package_ref": acceptance["payload"]["candidate_evidence_package_ref"], "image_digest": rollback["image_digest"], "runtime_profile_sha256": rollback["runtime_profile_sha256"], "bindings": [rollback["profile_binding"], rollback["override_binding"], q["sealed_result_binding"]]},
            "zero_model_probes": {"mode": "recipe", "recipe": probe_recipe, "request_records": [], "bindings": [guard.binding(self.root, recipe_path)]},
            "deployment_configuration": {"configuration_binding": self.context["configuration_ref"], "deployment_sha256": self.configuration["guard"]["deployment_receipt_binding"]["sha256"], "bindings": [self.configuration["guard"][name] for name in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding")]},
        }
        for kind, core in cores.items():
            core.update(common)
            core["contract_version"] = f"claude-delivery-{kind.replace('_', '-')}/v1"
        key = guard.read_bytes(guard.check_binding(self.root, self.configuration["guard"]["channel_key_binding"]))
        public_bytes = canonical_json_bytes(cores)
        http_public = [guard.read_bytes(guard.check_binding(self.root, item)) for item in all_bindings_for_records(records)]
        scanned = [public_bytes, *http_public]
        no_leak = all(secret not in data for secret in (self._credential, key) if secret for data in scanned)
        scan_path = directory / "public-secret-scan.json"
        guard.write_json_once(scan_path, {"schema_version": "claude-upgrade-public-secret-scan/v1", "scope": "public_core_and_http_records", "public_core_sha256": hashlib.sha256(public_bytes).hexdigest(), "http_bindings": all_bindings_for_records(records), "private_secret_fingerprints": [hashlib.sha256(secret).hexdigest() for secret in (self._credential, key) if secret], "result": "pass" if no_leak else "failed"})
        scan_binding = guard.binding(self.root, scan_path)
        allowed = {(self.configuration[name]["image_digest"], self.configuration[name]["runtime_profile_sha256"]) for name in ("candidate", "rollback")}
        gate_values = {"compose-diff": qualification["candidate_compose"]["services"][self.runtime["application_service"]]["image"] != qualification["rollback_compose"]["services"][self.runtime["application_service"]]["image"] and qualification["result"] == "pass", "dependency-connectivity": business["dependencies"], "entry-routing": business["entry-routing"], "evidence-inventory": bool(originals), "health": business["health"], "image-identity": actual["image"].get("state") == "known" and actual["profile"].get("state") == "known" and (actual["image"].get("value"), actual["profile"].get("value")) in allowed, "observation-window": request["observation_window_seconds"] == self.configuration["limits"]["observation_window_seconds"], "rollback-availability": qualification["qualification_replays"][1]["result"] == "pass", "secret-scan": no_leak}
        gates = [{"id": name, "result": "pass" if gate_values[name] else "failed", "facts": {"source": "fixed-arm64-readonly-freeze", "bindings": [qualification_binding, inventory_binding, scan_binding, *originals]}} for name in DELIVERY_GATES]
        cores["freeze"]["gates"] = gates
        if any(item["result"] != "pass" for item in gates):
            for core in cores.values():
                core["result"] = "failed"
        return {"environment": environment, "rollback": {"image_digest": rollback["image_digest"], "runtime_profile_sha256": rollback["runtime_profile_sha256"]}, "gates": gates, "cores": cores}


def all_bindings_for_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return guard.all_bindings(records)


class Arm64DeliveryExecutor:
    """受管工厂能力实例。构造不会启动守卫或修改现场。"""

    contract_version = EXECUTOR_VERSION

    def __init__(self, configuration: dict[str, Any], execution_context: dict[str, Any]):
        self.configuration = validate_configuration(configuration)
        self.context = validate_execution_context(execution_context, self.configuration)
        self.data_root = Path(self.context["data_root"])
        guard.private_directory(self.data_root)
        raw_path = guard.check_binding(self.data_root, self.context["configuration_ref"])
        if guard.read_json(raw_path) != self.configuration:
            raise guard.GuardError("冻结raw配置不等于factory有效配置")
        for item in guard.all_bindings(configuration):
            guard.check_binding(self.data_root, item)
        key_path = guard.check_binding(self.data_root, configuration["guard"]["channel_key_binding"])
        if key_path.stat().st_mode & 0o077:
            raise guard.GuardError("守卫通道密钥必须为私有600文件")
        self.client = guard.GuardClient(self.data_root, configuration["guard"]["socket_relative"], configuration["guard"]["channel_key_binding"], configuration["limits"]["stage_timeout_seconds"] + 30)
        self.owner = guard.process_identity(os.getpid())
        self._heartbeats: dict[str, tuple[threading.Event, threading.Thread]] = {}

    def _identity(self, identity: Any, *, require_live: bool = True) -> dict[str, Any]:
        item = guard.exact(identity, GUARD_IDENTITY_FIELDS, "guard身份")
        expected = {"contract_version": GUARD_IDENTITY_SCHEMA, "authority": AUTHORITY, "source_scope": self.configuration["source_scope"], "host_id": "ARM64", "instance_id": self.configuration["instance_id"], "service_name": self.configuration["guard"]["service_name"]}
        if any(item[key] != value for key, value in expected.items()) or item["process_group_id"] == self.owner["process_group_id"]:
            raise guard.GuardError("guard来源、实例或独立进程组不符")
        if self.configuration["source_scope"] == "formal" and item["independent_service"] is not True:
            raise guard.GuardError("formal执行器缺实际独立service证明")
        for key in ("service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding"):
            if item[key] != self.configuration["guard"][key]:
                raise guard.GuardError("守卫实际部署字节与冻结配置不符")
            guard.check_binding(self.data_root, item[key])
        if require_live and not guard.owner_alive({key: item[key] for key in ("boot_id", "pid", "process_start", "process_group_id")}):
            raise guard.GuardError("守卫实例已消失或PID被重用")
        return item

    def freeze(self, request: dict[str, Any]) -> dict[str, Any]:
        guard.exact(request, FREEZE_FIELDS, "freeze请求")
        expected = {"campaign_id": self.context["campaign_id"], "candidate_id": self.context["candidate_id"], "delivery_id": self.context["delivery_id"], "candidate_image_digest": self.configuration["candidate"]["image_digest"], "runtime_profile_sha256": self.configuration["candidate"]["runtime_profile_sha256"], "observation_window_seconds": self.configuration["limits"]["observation_window_seconds"]}
        if any(request[key] != value for key, value in expected.items()) or request["architecture"] not in {"arm64", "aarch64", "linux/arm64"} or request["delivery_gates"] != list(DELIVERY_GATES):
            raise guard.GuardError("freeze请求与配置或门禁闭集不符")
        self._status()
        snapshot = self.client.call("inspect", request)
        guard.exact(snapshot, {"environment", "rollback", "gates", "cores"}, "freeze返回")
        guard.exact(snapshot["cores"], EVIDENCE_TYPES, "冻结证据闭集")
        self._identity(snapshot["cores"]["freeze"]["guard_identity"])
        return snapshot

    def _start_heartbeat(self, ticket: dict[str, Any]) -> None:
        operation_id = ticket["operation_id"]
        if operation_id in self._heartbeats:
            return
        stop = threading.Event()
        def heartbeat() -> None:
            while not stop.wait(max(0.1, self.configuration["limits"]["lease_seconds"] / 3)):
                try:
                    self.client.call("heartbeat", {"ticket": ticket, "owner": self.owner})
                except Exception:
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        self._heartbeats[operation_id] = (stop, thread)
        thread.start()

    def prepare_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        self._status()
        ticket = self.client.call("prepare", {"request": request, "owner": self.owner})
        self._identity(ticket["guard_identity"], require_live=False)
        guard.check_binding(self.data_root, ticket["prepared_binding"])
        self._start_heartbeat(ticket)
        return ticket

    def run_stage(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return self.client.call("run", ticket)

    def finish_stage(self, ticket: dict[str, Any], verified_decision: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.client.call("finish", {"ticket": ticket, "decision": verified_decision})
        finally:
            existing = self._heartbeats.pop(ticket["operation_id"], None)
            if existing:
                existing[0].set()
                existing[1].join(timeout=1)

    def recover_pending(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.client.call("recover", request)

    def _status(self) -> dict[str, Any]:
        result = self.client.call("status", {})
        self._identity(result["guard_identity"])
        expected = {key: value for key, value in self.context.items() if key not in {"segment", "run_nonce"}}
        if result["execution_context"] != expected:
            raise guard.GuardError("固定守卫服务属于另一Campaign或delivery")
        return result

    def inventory(self) -> dict[str, Any]:
        self._status()
        return self.client.call("inventory", {})

    def operations_index(self) -> dict[str, Any]:
        inventory = self.inventory()
        return {"operation_index_binding": inventory["operations_index_binding"], "origin": inventory["origin"]}

    def verify_origin_evidence(self, content: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
        raw = self.client.call("export", {"content": dict(content), "origin": dict(origin)})
        self._identity(raw["guard_identity"])
        if raw["content_sha256"] != canonical_sha256(content) or raw["deployment_sha256"] != self.configuration["guard"]["deployment_receipt_binding"]["sha256"]:
            raise guard.GuardError("受管来源返回的内容或部署摘要不匹配")
        originals = {path: base64.b64decode(encoded, validate=True) for path, encoded in raw["bytes_by_path"].items()}
        for item in guard.all_bindings({"content": content, "origin": origin}):
            data = originals.get(item["path"])
            if data is None or len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise guard.GuardError("受管来源原件不闭合")
        if raw["configuration_sha256"] != canonical_sha256(self.configuration) or raw["trusted_data_root"] != str(self.data_root) or raw["service_instance_id"] != origin["service_instance_id"]:
            raise guard.GuardError("认证通道的来源配置、实例或受管根不一致")
        return {"content_sha256": raw["content_sha256"], "deployment_sha256": raw["deployment_sha256"], "bytes_by_path": originals, "configuration_sha256": raw["configuration_sha256"], "service_instance_id": raw["service_instance_id"], "trusted_data_root": self.data_root}


def create_executor(name: str, configuration: dict[str, Any], execution_context: dict[str, Any]) -> Arm64DeliveryExecutor:
    if name != "arm64-isolated":
        raise guard.GuardError("只提供固定arm64-isolated执行器能力；不动态import")
    return Arm64DeliveryExecutor(configuration, execution_context)


def verify_origin_evidence(executor: Arm64DeliveryExecutor, content: Mapping[str, Any], origin: Mapping[str, Any]) -> dict[str, Any]:
    """固定来源通道能力；调用者须由可信登记选中实例，不从证据创建配置。"""
    if not isinstance(executor, Arm64DeliveryExecutor):
        raise guard.GuardError("来源读取须使用固定执行器实例")
    return executor.verify_origin_evidence(content, origin)
