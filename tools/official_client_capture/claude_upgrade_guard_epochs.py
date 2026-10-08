"""Guard 进程代际的纯注册链校验；本模块不授予启动完成或阶段通过资格。

调用方先通过固定通道核当前真实进程，再把原件读取器传入本模块。
这里只证明注册链属于同一冻结配置、部署与执行上下文，原生产进程身份
保持原值。启动恢复、readiness、完整来源及当前准入须由后续消费合同完成，
不得用注册链代替这些合同。本模块不连接 RPC、不访问 Store、不读取磁盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
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
READINESS_SCHEMA = "claude-upgrade-delivery-guard-epoch-readiness/v1"
INDEX_SCHEMA = "claude-upgrade-delivery-operations-index/v1"
JOURNAL_SCHEMA = "claude-upgrade-delivery-journal/v1"
PREPARED_SCHEMA = "claude-upgrade-delivery-prepared/v1"
TERMINAL_SCHEMA = "claude-upgrade-delivery-guard-terminal/v1"
READINESS_FIELDS = frozenset({
    "schema_version", "epoch_binding", "recovered_operations_index_binding", "operations", "result",
})
READINESS_OPERATION_FIELDS = frozenset({
    "operation_id", "prepared_binding", "journal_prefix_binding", "terminal_binding", "status",
})
INDEX_OPERATION_FIELDS = frozenset({
    "operation_id", "status", "prepared_binding", "terminal_binding", "bindings",
})
SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CONTINUITY_SCHEMA = "claude-upgrade-delivery-guard-continuity/v1"
CONTINUITY_FIELDS = frozenset({
    "schema_version", "source_service_instance_id", "channel_service_instance_id",
    "stable_deployment_identity_sha256", "epoch_bindings", "readiness_bindings",
    "source_record_binding", "source_json_pointer", "current_operations_index_binding",
})
SOURCE_ORIGIN_FIELDS = frozenset({
    "authority", "source_scope", "configuration_sha256", "service_instance_id", "operations_index_binding",
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


@dataclass(frozen=True)
class VerifiedEpochReadiness:
    """某个启动边界的完整恢复结果；不代表之后没有新的未决操作。"""

    service_instance_id: str
    readiness_binding: EpochBinding
    recovered_operations_index_binding: EpochBinding
    operation_ids: tuple[str, ...]
    read_bindings: tuple[EpochBinding, ...]


@dataclass(frozen=True)
class VerifiedGuardContinuity:
    """保留原 producer 与当前通道的区别，完整原件用于再次验真和归档。"""

    stable_deployment_identity_sha256: str
    configuration_sha256: str
    source_service_instance_id: str
    channel_service_instance_id: str
    source_content_sha256: str
    source_origin_sha256: str
    source_record_binding: EpochBinding
    current_operations_index_binding: EpochBinding
    epoch_bindings: tuple[EpochBinding, ...]
    readiness_bindings: tuple[EpochBinding, ...]
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


def validate_epoch_readiness(
    configuration: Mapping[str, Any],
    context: Mapping[str, Any],
    registered: RegisteredGuardEpochs,
    readiness_binding: Mapping[str, Any],
    read_raw: Callable[[Mapping[str, Any]], bytes],
) -> VerifiedEpochReadiness:
    """纯校验一次启动恢复的完整操作全集、终态和 journal 前缀。

    调用方必须先由真实守卫通道取得并校验注册链。这里核不可变原件与
    已注册进程的归属，不查询磁盘、当前进程或 RPC。启动快照是完整受管
    索引的一次边界；后续准入仍须读取当前索引，不能据此省略未决检查。
    """
    if not isinstance(registered, RegisteredGuardEpochs):
        raise ControlError("readiness 必须消费已验证的完整注册链")
    root = validate_relative_path(configuration["paths"]["guard_root_relative"], "readiness.guard_root")
    readiness = EpochBinding.from_value(readiness_binding, "readiness.binding")
    choices = {
        f"{root}/epochs/{number:06d}/readiness.json": (item, instance)
        for number, (item, instance) in enumerate(zip(
            registered.epoch_bindings, registered.ordered_service_instance_ids, strict=True,
        ), 1)
    }
    if readiness.path not in choices:
        raise ControlError("readiness 不是已注册 epoch 的精确路径")
    epoch_binding, instance = choices[readiness.path]
    seen: dict[str, tuple[EpochBinding, bytes]] = {}

    def read(value: Any, label: str) -> bytes:
        item = EpochBinding.from_value(value, label)
        previous = seen.get(item.path)
        if previous is not None:
            if previous[0] != item:
                raise ControlError("readiness 同一路径引用了不同原件")
            return previous[1]
        raw = read_raw(item.as_value())
        if not isinstance(raw, bytes) or len(raw) != item.bytes or sha256_bytes(raw) != item.sha256:
            raise ControlError("readiness 原件缺失或字节漂移：" + item.path)
        seen[item.path] = (item, raw)
        return raw

    def read_json(value: Any, label: str) -> dict[str, Any]:
        return expect_object(parse_json_bytes(read(value, label)), label)

    def read_binding_values(node: Any, label: str) -> None:
        # 这里只核显式标准绑定，不对无关 JSON 赋予跳过或读取权限。
        if isinstance(node, dict):
            if set(node) == {"path", "sha256", "bytes"}:
                read(node, label)
            else:
                for child in node.values():
                    read_binding_values(child, label)
        elif isinstance(node, list):
            for child in node:
                read_binding_values(child, label)

    def check_producer(identity: Any, label: str) -> None:
        stable = stable_deployment_identity(configuration, context, expect_object(identity, label))
        if (canonical_sha256(stable) != registered.stable_deployment_identity_sha256
                or identity["service_instance_id"] not in registered.ordered_service_instance_ids):
            raise ControlError("readiness 原生产进程不在同一部署注册链中")

    document = _exact(read_json(readiness_binding, "readiness"), READINESS_FIELDS, "readiness")
    if (document["schema_version"] != READINESS_SCHEMA or document["result"] != "pass"
            or document["epoch_binding"] != epoch_binding.as_value()):
        raise ControlError("readiness 版本、结果或 epoch 绑定非法")
    registration = read_json(epoch_binding.as_value(), "readiness.epoch")
    if registration["guard_identity"]["service_instance_id"] != instance:
        raise ControlError("readiness 注册原件与已验证进程不一致")
    index_binding = EpochBinding.from_value(document["recovered_operations_index_binding"], "readiness.index")
    index = _exact(read_json(index_binding.as_value(), "readiness.index"), {
        "schema_version", "configuration_sha256", "operations",
    }, "readiness.index")
    if (index["schema_version"] != INDEX_SCHEMA
            or index["configuration_sha256"] != canonical_sha256(configuration)
            or index_binding.path != f"{root}/index-snapshots/{canonical_sha256(index)}.json"):
        raise ControlError("readiness 索引不是同配置的完整受管快照")
    if not isinstance(index["operations"], list) or not isinstance(document["operations"], list):
        raise ControlError("readiness 操作全集必须为数组")
    indexed = [_exact(value, INDEX_OPERATION_FIELDS, "readiness.index.operation") for value in index["operations"]]
    rows = [_exact(value, READINESS_OPERATION_FIELDS, "readiness.operation") for value in document["operations"]]
    ids = [expect_string(row["operation_id"], "readiness.operation_id") for row in rows]
    if (any(not SAFE_OPERATION_ID.fullmatch(value) for value in ids)
            or ids != sorted(set(ids))
            or ids != [row["operation_id"] for row in indexed]):
        raise ControlError("readiness 遗漏、重复、乱序或伪造了恢复操作")
    stable_context = _stable_context(context)
    for row, indexed_row in zip(rows, indexed, strict=True):
        operation_id = row["operation_id"]
        operation_root = f"{root}/operations/{operation_id}"
        for key in ("operation_id", "prepared_binding", "terminal_binding", "status"):
            if row[key] != indexed_row[key]:
                raise ControlError("readiness 操作与恢复后完整索引不一致")
        if row["status"] not in {"execution_committed", "restored"}:
            raise ControlError("readiness 存在尚未验证恢复的操作")
        if not isinstance(indexed_row["bindings"], list):
            raise ControlError("readiness 索引原件读集必须为数组")
        original_paths = []
        for value in indexed_row["bindings"]:
            item = EpochBinding.from_value(value, "readiness.index.original")
            if not PurePosixPath(item.path).is_relative_to(operation_root):
                raise ControlError("readiness 索引原件越出该操作根")
            original_paths.append(item.path)
            read(value, "readiness.index.original")
        if original_paths != sorted(set(original_paths)):
            raise ControlError("readiness 索引原件重复或乱序")
        prepared_binding = EpochBinding.from_value(row["prepared_binding"], "readiness.prepared")
        if prepared_binding.path != f"{operation_root}/prepared.json":
            raise ControlError("readiness prepared 不是精确操作原件")
        prepared = _exact(read_json(row["prepared_binding"], "readiness.prepared"), {
            "schema_version", "operation_id", "configuration_sha256", "context", "request", "target",
            "rollback", "before_bindings", "guard_identity", "owner", "prepared_at_utc",
        }, "readiness.prepared")
        if (prepared["schema_version"] != PREPARED_SCHEMA or prepared["operation_id"] != operation_id
                or prepared["configuration_sha256"] != canonical_sha256(configuration)
                or prepared["context"] != stable_context):
            raise ControlError("readiness prepared 配置或上下文漂移")
        check_producer(prepared["guard_identity"], "readiness.prepared.guard")
        terminal_binding = EpochBinding.from_value(row["terminal_binding"], "readiness.terminal")
        journal_binding = EpochBinding.from_value(row["journal_prefix_binding"], "readiness.journal")
        terminal_parent = f"{operation_root}/terminals/"
        number_text = terminal_binding.path.removeprefix(terminal_parent).removesuffix(".json")
        if (not re.fullmatch(r"[0-9]{6}", number_text) or int(number_text) < 1
                or terminal_binding.path != terminal_parent + number_text + ".json"
                or journal_binding.path != f"{operation_root}/journal-prefix-{number_text}.json"):
            raise ControlError("readiness terminal 或 journal 前缀坐标非法")
        terminal = _exact(read_json(row["terminal_binding"], "readiness.terminal"), {
            "schema_version", "operation_id", "status", "prepared_sha256", "physical_intent_sha256",
            "physical_intent_binding", "restoration", "guard_identity",
        }, "readiness.terminal")
        if (terminal["schema_version"] != TERMINAL_SCHEMA or terminal["operation_id"] != operation_id
                or terminal["status"] != row["status"] or terminal["prepared_sha256"] != prepared_binding.sha256):
            raise ControlError("readiness terminal 与 prepared 或完整索引不一致")
        check_producer(terminal["guard_identity"], "readiness.terminal.guard")
        restoration = _exact(terminal["restoration"], {"state", "bindings", "reason"}, "readiness.restoration")
        expect_string(restoration["reason"], "readiness.restoration.reason")
        if not isinstance(restoration["bindings"], list):
            raise ControlError("readiness 恢复原件必须为数组")
        if row["status"] == "restored":
            if restoration["state"] != "verified" or not restoration["bindings"]:
                raise ControlError("readiness 恢复终态缺少实测恢复原件")
        elif restoration["state"] != "not_required" or restoration["bindings"] or terminal["physical_intent_binding"] is None:
            raise ControlError("readiness 提交终态缺少原始物理提交意图")
        intent = terminal["physical_intent_binding"]
        if intent is None:
            if terminal["physical_intent_sha256"] is not None:
                raise ControlError("readiness 空提交意图仍有摘要")
        elif _binding(intent, "readiness.intent")["sha256"] != terminal["physical_intent_sha256"]:
            raise ControlError("readiness 提交意图摘要不一致")
        prefix = _exact(read_json(row["journal_prefix_binding"], "readiness.journal"), {
            "schema_version", "operation_id", "records",
        }, "readiness.journal")
        records = prefix["records"]
        if (prefix["schema_version"] != JOURNAL_SCHEMA or prefix["operation_id"] != operation_id
                or not isinstance(records, list) or not records):
            raise ControlError("readiness journal 前缀版本、坐标或记录非法")
        previous = None
        for number, value in enumerate(records, 1):
            record = _exact(value, {"schema_version", "operation_id", "sequence", "event", "payload", "previous_sha256", "record_sha256"}, "readiness.journal.record")
            if (record["schema_version"] != JOURNAL_SCHEMA or record["operation_id"] != operation_id
                    or type(record["sequence"]) is not int or record["sequence"] != number
                    or record["previous_sha256"] != previous
                    or record["record_sha256"] != canonical_sha256({key: item for key, item in record.items() if key != "record_sha256"})):
                raise ControlError("readiness journal 链断裂或原件摘要漂移")
            expect_string(record["event"], "readiness.journal.event")
            expect_object(record["payload"], "readiness.journal.payload")
            # journal 前缀保留完整记录；逐文件字节由 snapshot 的绑定一并封存。
            record_path = f"{operation_root}/journal/{number:06d}.json"
            if record_path not in seen or parse_json_bytes(seen[record_path][1]) != record:
                raise ControlError("readiness journal 前缀与原记录文件不一致")
            previous = record["record_sha256"]
            read_binding_values(record["payload"], "readiness.journal.payload.binding")
        if records[-1]["event"] != row["status"] or records[-1]["payload"].get("terminal_binding") != row["terminal_binding"]:
            raise ControlError("readiness journal 未闭合到恢复后的终态")
        read_binding_values(prepared, "readiness.prepared.binding")
        read_binding_values(terminal, "readiness.terminal.binding")
    return VerifiedEpochReadiness(
        instance, readiness, index_binding, tuple(ids), tuple(seen[path][0] for path in sorted(seen)),
    )


def validate_guard_continuity(
    configuration: Mapping[str, Any],
    context: Mapping[str, Any],
    envelope: Any,
    read_raw: Callable[[Mapping[str, Any]], bytes],
    *,
    source_core: Mapping[str, Any],
    source_origin: Mapping[str, Any],
    channel_identity: Mapping[str, Any],
    purpose: str,
) -> VerifiedGuardContinuity:
    """核同部署注册链、启动恢复、原生产记录与当前通道之间的连续性。

    当前通道 live 身份和材料选择由受管守卫验证；本函数只消费已选定的
    精确原件。旧来源保留原 ID 和结果，不对旧 failed/unknown 倒填成功。
    操作索引没有独立 producer 字段，因此其派生 core 只接受当前通道，
    不能用相同索引字节冒充旧进程曾生成的来源。其他 core 必须逐字等于
    原操作文件或冻结 snapshot 的指定子对象，不能由调用者上传 JSON 授权。
    """
    if purpose not in {"current_execution", "formed_replay"}:
        raise ControlError("guard 连续性消费目的未登记")
    document = _exact(envelope, CONTINUITY_FIELDS, "guard.continuity")
    origin = _exact(dict(source_origin), SOURCE_ORIGIN_FIELDS, "guard.continuity.origin")
    core = expect_object(dict(source_core), "guard.continuity.core")
    seen: dict[str, tuple[EpochBinding, bytes]] = {}

    def read(value: Mapping[str, Any]) -> bytes:
        item = EpochBinding.from_value(value, "guard.continuity.binding")
        previous = seen.get(item.path)
        if previous is not None:
            if previous[0] != item:
                raise ControlError("guard 连续性对同一路径引用不同原件")
            return previous[1]
        raw = read_raw(item.as_value())
        if not isinstance(raw, bytes) or len(raw) != item.bytes or sha256_bytes(raw) != item.sha256:
            raise ControlError("guard 连续性原件缺失或字节漂移：" + item.path)
        seen[item.path] = (item, raw)
        return raw

    registered = validate_registration_chain(
        configuration, context, document["epoch_bindings"], read, channel_identity=channel_identity,
    )
    if (document["schema_version"] != CONTINUITY_SCHEMA
            or document["channel_service_instance_id"] != registered.channel_service_instance_id
            or document["source_service_instance_id"] != origin["service_instance_id"]
            or document["source_service_instance_id"] not in registered.ordered_service_instance_ids
            or document["stable_deployment_identity_sha256"] != registered.stable_deployment_identity_sha256
            or origin["configuration_sha256"] != canonical_sha256(configuration)
            or origin["authority"] != channel_identity["authority"]
            or origin["source_scope"] != channel_identity["source_scope"]):
        raise ControlError("guard 连续性版本、来源、当前通道或稳定部署不一致")
    ready_values = document["readiness_bindings"]
    if not isinstance(ready_values, list):
        raise ControlError("guard 连续性 readiness 必须是显式完整数组")
    ready_bindings = tuple(EpochBinding.from_value(value, "guard.continuity.readiness") for value in ready_values)
    paths = [item.path for item in ready_bindings]
    if paths != sorted(set(paths)):
        raise ControlError("guard 连续性 readiness 重复或乱序")
    ready_instances = set()
    for value in ready_values:
        readiness = validate_epoch_readiness(configuration, context, registered, value, read)
        ready_instances.add(readiness.service_instance_id)
    if registered.channel_service_instance_id not in ready_instances:
        raise ControlError("当前 guard 通道尚未证明启动完整恢复，不能消费连续性")
    if core.get("result") != "failed" and origin["service_instance_id"] not in ready_instances:
        raise ControlError("原通过来源缺 producer 的启动恢复证明，不能承接成功")
    root = validate_relative_path(configuration["paths"]["guard_root_relative"], "guard.continuity.root")
    source_index_binding = EpochBinding.from_value(origin["operations_index_binding"], "guard.continuity.source_index")
    source_index = _exact(parse_json_bytes(read(source_index_binding.as_value())), {
        "schema_version", "configuration_sha256", "operations",
    }, "guard.continuity.source_index")
    if (source_index["schema_version"] != INDEX_SCHEMA
            or source_index["configuration_sha256"] != canonical_sha256(configuration)
            or source_index_binding.path != f"{root}/index-snapshots/{canonical_sha256(source_index)}.json"
            or not isinstance(source_index["operations"], list)):
        raise ControlError("guard 原来源的形成期索引缺失或配置不一致")
    index_binding = EpochBinding.from_value(document["current_operations_index_binding"], "guard.continuity.current_index")
    index = _exact(parse_json_bytes(read(index_binding.as_value())), {
        "schema_version", "configuration_sha256", "operations",
    }, "guard.continuity.current_index")
    if (index["schema_version"] != INDEX_SCHEMA
            or index["configuration_sha256"] != canonical_sha256(configuration)
            or index_binding.path != f"{root}/index-snapshots/{canonical_sha256(index)}.json"
            or not isinstance(index["operations"], list)):
        raise ControlError("guard 连续性当前索引不是完整受管快照")
    current_rows = [_exact(row, INDEX_OPERATION_FIELDS, "guard.continuity.current_index.operation") for row in index["operations"]]
    current_ids = [expect_string(row["operation_id"], "guard.continuity.operation_id") for row in current_rows]
    if current_ids != sorted(set(current_ids)) or any(not SAFE_OPERATION_ID.fullmatch(value) for value in current_ids):
        raise ControlError("guard 连续性当前索引操作重复、乱序或非法")
    for row in current_rows:
        if (not isinstance(row["bindings"], list)
                or row["status"] not in {"armed", "execution_committed", "restored", "restore_failed", "recovery_pending"}
                or _binding(row["prepared_binding"], "guard.continuity.index.prepared")["path"] != f"{root}/operations/{row['operation_id']}/prepared.json"):
            raise ControlError("guard 连续性当前索引条目非法")
        for binding in [row["prepared_binding"], *row["bindings"], *([row["terminal_binding"]] if row["terminal_binding"] is not None else [])]:
            read(binding)
    source_binding = EpochBinding.from_value(document["source_record_binding"], "guard.continuity.source_record")
    record = expect_object(parse_json_bytes(read(source_binding.as_value())), "guard.continuity.source_record")
    pointer = document["source_json_pointer"]
    expected_core = {**core, "origin": origin}
    if any(core.get(key) != context[key] for key in ("campaign_id", "delivery_id")):
        raise ControlError("guard 原来源跨 Campaign 或交付上下文")
    if "guard_identity" in core:
        producer = expect_object(core["guard_identity"], "guard.continuity.core.guard_identity")
        stable = stable_deployment_identity(configuration, context, producer)
        if (producer["service_instance_id"] != origin["service_instance_id"]
                or canonical_sha256(stable) != registered.stable_deployment_identity_sha256):
            raise ControlError("guard 原 core 的 producer 与连续性来源不一致")
    if pointer == "":
        if set(core) == {"campaign_id", "delivery_id", "operation_index_binding"}:
            if (origin["service_instance_id"] != registered.channel_service_instance_id
                    or source_binding != index_binding or core["operation_index_binding"] != index_binding.as_value()
                    or origin["operations_index_binding"] != index_binding.as_value()
                    or any(core[key] != context[key] for key in ("campaign_id", "delivery_id"))):
                raise ControlError("guard 派生索引来源只能绑定已验证的当前通道及当前完整索引")
        else:
            operation_id = core.get("operation_id")
            kind = core.get("contract_version")
            expected_path = None
            if isinstance(operation_id, str) and kind == "claude-delivery-stage-execution/v1":
                expected_path = f"{root}/operations/{operation_id}/evidence-000001.json"
            elif isinstance(operation_id, str) and kind == "claude-delivery-restore/v1":
                terminal = _binding(core.get("terminal_binding"), "guard.continuity.restore.terminal")
                match = re.fullmatch(re.escape(f"{root}/operations/{operation_id}/terminals/") + r"([0-9]{6})\.json", terminal["path"])
                if match is not None and int(match[1]) > 0:
                    expected_path = f"{root}/operations/{operation_id}/restoration-core-{match[1]}.json"
            if (not isinstance(operation_id, str) or not SAFE_OPERATION_ID.fullmatch(operation_id)
                    or operation_id not in current_ids
                    or source_binding.path != expected_path
                    or record != expected_core):
                raise ControlError("guard 原来源不是该操作的完整不可变 core")
    else:
        names = {"configuration", "stable_projection", "freeze", "rollback_state", "zero_model_probes", "deployment_configuration"}
        if (not isinstance(pointer, str) or pointer not in {"/cores/" + name for name in names}
                or not re.fullmatch(re.escape(root) + r"/freezes/[0-9a-f]{64}/[0-9]{6}/snapshot\.json", source_binding.path)):
            raise ControlError("guard 冻结来源 pointer 或原 snapshot 坐标非法")
        _exact(record, {"environment", "rollback", "gates", "cores"}, "guard.continuity.snapshot")
        if expect_object(record["cores"], "guard.continuity.snapshot.cores").get(pointer.rsplit("/", 1)[1]) != expected_core:
            raise ControlError("guard 冻结来源与指定原 core 不相等")
    return VerifiedGuardContinuity(
        registered.stable_deployment_identity_sha256, canonical_sha256(configuration),
        str(origin["service_instance_id"]), registered.channel_service_instance_id,
        canonical_sha256(core), canonical_sha256(origin), source_binding, index_binding,
        registered.epoch_bindings, ready_bindings, tuple(seen[path][0] for path in sorted(seen)),
    )


def same_verified_guard_deployment(left: Any, right: Any) -> bool:
    """跨 producer 的同部署关系只能由两份完整连续性证明授予。"""
    if not isinstance(left, VerifiedGuardContinuity) or not isinstance(right, VerifiedGuardContinuity):
        return False
    return (
        left.stable_deployment_identity_sha256 == right.stable_deployment_identity_sha256
        and left.configuration_sha256 == right.configuration_sha256
        and left.channel_service_instance_id == right.channel_service_instance_id
        and left.epoch_bindings == right.epoch_bindings
    )
