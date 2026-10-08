"""Guard 进程代际的纯注册链校验；本模块不授予启动完成或阶段通过资格。

调用方先通过固定通道核当前真实进程，再把原件读取器传入本模块。
这里只证明注册链属于同一冻结配置、部署与执行上下文，原生产进程身份
保持原值。启动恢复、readiness、完整来源及当前准入须由后续消费合同完成，
不得用注册链代替这些合同。本模块不连接 RPC、不访问 Store、不读取磁盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from tools.official_client_control.canonical import (
    canonical_sha256,
    expect_non_negative_int,
    expect_object,
    expect_sha256,
    expect_string,
    parse_json_bytes,
    sha256_bytes,
    validate_external_binding,
    validate_relative_path,
)
from tools.official_client_control.errors import ControlError

EPOCH_SCHEMA = "claude-upgrade-delivery-guard-epoch/v1"
IDENTITY_SCHEMA = "claude-upgrade-delivery-guard-identity/v1"
PROCESS_FIELDS = frozenset({"boot_id", "pid", "process_start", "process_group_id"})
DEPLOYMENT_FIELDS = (
    "service_unit_binding", "executor_binding", "guard_binding", "deployment_receipt_binding",
)
IDENTITY_FIELDS = frozenset({
    "contract_version", "authority", "source_scope", "host_id", "instance_id", "service_name",
    "service_instance_id", "independent_service", *PROCESS_FIELDS, *DEPLOYMENT_FIELDS,
})
STABLE_CONTEXT_FIELDS = frozenset({
    "campaign_id", "candidate_id", "delivery_id", "data_root", "configuration_ref",
})
EPOCH_FIELDS = frozenset({
    "schema_version", "epoch_sequence", "configuration_binding", "configuration_sha256",
    "context", "stable_deployment_identity_sha256", "guard_identity", "previous_epoch_binding",
})


def _exact(value: Any, fields: Any, label: str) -> dict[str, Any]:
    result = expect_object(value, label)
    if set(result) != set(fields):
        raise ControlError(f"{label} 字段不闭合")
    return result


def _binding(value: Any, label: str) -> dict[str, Any]:
    item = validate_external_binding(value, label)
    if "\\" in item["path"] or "\0" in item["path"] or item["sha256"] == "0" * 64:
        raise ControlError(f"{label} 路径或摘要非法")
    return dict(item)


def _stable_context(context: Mapping[str, Any]) -> dict[str, Any]:
    fields = set(context)
    if fields not in (set(STABLE_CONTEXT_FIELDS), set(STABLE_CONTEXT_FIELDS) | {"segment", "run_nonce"}):
        raise ControlError("epoch 执行上下文字段不闭合")
    result = {key: context[key] for key in STABLE_CONTEXT_FIELDS}
    for key in ("campaign_id", "candidate_id", "delivery_id"):
        expect_string(result[key], "epoch.context." + key)
    root = expect_string(result["data_root"], "epoch.context.data_root")
    path = PurePosixPath(root)
    if not path.is_absolute() or path.as_posix() != root or ".." in path.parts or "\\" in root or "\0" in root:
        raise ControlError("epoch 数据根必须为规范绝对路径")
    result["configuration_ref"] = _binding(result["configuration_ref"], "epoch.context.configuration_ref")
    return result


def stable_deployment_identity(
    configuration: Mapping[str, Any], context: Mapping[str, Any], guard_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """核真实身份的数学绑定，并返回不含进程四字段的稳定部署投影。

    本函数没有当前进程存活或来源授权能力。调用方不能将任意上传 JSON
    传进来后据此宣称存在真实守卫；必须先完成固定通道与部署原件核验。
    """
    identity = _exact(guard_identity, IDENTITY_FIELDS, "epoch.guard_identity")
    if (configuration.get("client") != "claude" or configuration.get("host_id") != "ARM64"
            or configuration.get("isolation") != "isolated_candidate"):
        raise ControlError("epoch 仅接受 Claude ARM64 隔离候选配置")
    settings = expect_object(configuration.get("guard"), "epoch.configuration.guard")
    expected = {
        "contract_version": IDENTITY_SCHEMA,
        "authority": "claude-arm64-recovery-guard",
        "host_id": "ARM64",
        "source_scope": configuration.get("source_scope"),
        "instance_id": configuration.get("instance_id"),
        "service_name": settings.get("service_name"),
    }
    if expected["source_scope"] not in {"formal", "regression_fixture"}:
        raise ControlError("epoch 来源用途非法")
    for key in ("instance_id", "service_name"):
        expect_string(expected[key], "epoch." + key)
    if any(identity[key] != value for key, value in expected.items()):
        raise ControlError("epoch 守卫不属于冻结配置或 authority")
    if type(identity["independent_service"]) is not bool or (
        expected["source_scope"] == "formal" and identity["independent_service"] is not True
    ):
        raise ControlError("epoch 正式守卫缺独立服务身份")
    for key in ("boot_id", "process_start"):
        expect_string(identity[key], "epoch.guard_identity." + key)
    for key in ("pid", "process_group_id"):
        if expect_non_negative_int(identity[key], "epoch.guard_identity." + key) < 1:
            raise ControlError("epoch 进程 PID 或进程组非法")
    configuration_sha256 = canonical_sha256(configuration)
    process = {key: identity[key] for key in PROCESS_FIELDS}
    if identity["service_instance_id"] != canonical_sha256({"configuration": configuration_sha256, "process": process}):
        raise ControlError("epoch 进程实例摘要不等于真实四字段与配置绑定")
    deployment = {}
    for key in DEPLOYMENT_FIELDS:
        actual = _binding(identity[key], "epoch.guard_identity." + key)
        if actual != settings.get(key):
            raise ControlError("epoch 部署绑定与冻结配置不一致")
        deployment[key] = actual
    return {
        "configuration_sha256": configuration_sha256,
        "context": _stable_context(context),
        **expected,
        "independent_service": identity["independent_service"],
        **deployment,
    }


@dataclass(frozen=True)
class EpochBinding:
    """以不可变标量保存已验证绑定，避免调用者随后改动输入字典。"""

    path: str
    sha256: str
    bytes: int

    @classmethod
    def from_value(cls, value: Any, label: str) -> EpochBinding:
        parsed = _binding(value, label)
        return cls(**parsed)

    def as_value(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True)
class RegisteredGuardEpochs:
    """只有注册连续性；没有 readiness 或当前成功资格旗标。"""

    stable_deployment_identity_sha256: str
    channel_service_instance_id: str
    ordered_service_instance_ids: tuple[str, ...]
    epoch_bindings: tuple[EpochBinding, ...]
    read_bindings: tuple[EpochBinding, ...]


def validate_registration_chain(
    configuration: Mapping[str, Any],
    context: Mapping[str, Any],
    epoch_bindings: Any,
    read_raw: Callable[[Mapping[str, Any]], bytes],
    *,
    channel_identity: Mapping[str, Any],
) -> RegisteredGuardEpochs:
    """严格核完整有序注册链；该结果不能代替启动恢复或来源凭证。

    当前通道的真实性、live进程和根授权由调用方验证。读取器只接受显式
    标准绑定；原字节、重复路径和配置原件在这里逐项核验，不从 JSON
    自报绝对根扩权，也不反向调用任何上层验证器。
    """
    if not isinstance(epoch_bindings, list) or not epoch_bindings:
        raise ControlError("epoch 注册链必须为非空完整数组")
    stable = stable_deployment_identity(configuration, context, channel_identity)
    stable_sha256 = canonical_sha256(stable)
    frozen_context = stable["context"]
    root = validate_relative_path(configuration["paths"]["guard_root_relative"], "epoch.guard_root")
    seen: dict[str, tuple[EpochBinding, bytes]] = {}

    def read(value: Any, label: str) -> bytes:
        item = EpochBinding.from_value(value, label)
        previous = seen.get(item.path)
        if previous is not None:
            if previous[0] != item:
                raise ControlError("epoch 同一路径绑定不同原字节")
            return previous[1]
        raw = read_raw(item.as_value())
        if not isinstance(raw, bytes) or len(raw) != item.bytes or sha256_bytes(raw) != item.sha256:
            raise ControlError("epoch 原件缺失或字节漂移：" + item.path)
        seen[item.path] = (item, raw)
        return raw

    configuration_ref = frozen_context["configuration_ref"]
    if parse_json_bytes(read(configuration_ref, "epoch.configuration_ref")) != configuration:
        raise ControlError("epoch 冻结配置原件不等于有效配置")
    for key in DEPLOYMENT_FIELDS:
        read(stable[key], "epoch.deployment." + key)
    previous_binding = None
    instances = []
    verified_bindings = []
    last_identity = None
    for number, value in enumerate(epoch_bindings, 1):
        item = EpochBinding.from_value(value, "epoch.registration")
        if item.path != f"{root}/epochs/{number:06d}/registration.json":
            raise ControlError("epoch 注册路径缺失、乱序或不是冻结根")
        document = _exact(parse_json_bytes(read(value, "epoch.registration")), EPOCH_FIELDS, "epoch.registration")
        if document["schema_version"] != EPOCH_SCHEMA or type(document["epoch_sequence"]) is not int or document["epoch_sequence"] != number:
            raise ControlError("epoch 注册版本或序号非法")
        if document["previous_epoch_binding"] != previous_binding:
            raise ControlError("epoch 前序链断裂或遗漏中间注册")
        if (document["configuration_binding"] != configuration_ref
                or document["configuration_sha256"] != stable["configuration_sha256"]
                or document["context"] != frozen_context
                or expect_sha256(document["stable_deployment_identity_sha256"], "epoch.stable_identity") != stable_sha256):
            raise ControlError("epoch 配置、部署或执行上下文漂移")
        identity = document["guard_identity"]
        if stable_deployment_identity(configuration, frozen_context, identity) != stable:
            raise ControlError("epoch 不属于同一冻结部署")
        instance = identity["service_instance_id"]
        if instance in instances:
            raise ControlError("epoch 重复登记了同一进程实例")
        instances.append(instance)
        verified_bindings.append(item)
        previous_binding = item.as_value()
        last_identity = identity
    if last_identity != channel_identity:
        raise ControlError("epoch 链未闭合到已验证的当前通道进程")
    return RegisteredGuardEpochs(
        stable_sha256, str(channel_identity["service_instance_id"]), tuple(instances),
        tuple(verified_bindings), tuple(seen[path][0] for path in sorted(seen)),
    )
